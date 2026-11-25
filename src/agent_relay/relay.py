"""The relay: turns task emails into sessions, one at a time, with safeguards."""

from __future__ import annotations

import hashlib
import imaplib
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agent_relay.config import Config
from agent_relay.gmail import REPLY_HEADER, Fetched, Gmail
from agent_relay.replies import Replier
from agent_relay.sessions import (
    PID_FILE,
    PROMPT_FILE,
    RELAY_DIR,
    STARTED_MARKER,
    Launcher,
    build_command,
    list_files,
    started_hook_args,
    with_pid_file,
)
from agent_relay.state import State, Task
from agent_relay.trust import is_trusted
from agent_relay.watch import FolderWatch

log = logging.getLogger(__name__)

SUBJECT_RE = re.compile(r"^\s*(claude|codex)\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)

# Gmail drops IDLE after ~30 min; restart it (and do one check) before that.
IDLE_SECONDS = 10 * 60
# Reconnect waits: start here, double after each failure, never exceed the max.
RETRY_MIN, RETRY_MAX = 30, 30 * 60
# A connection that stayed up this long counts as healthy and resets the wait.
HEALTHY_AFTER = 10 * 60
# Gmail saying "back off" (connection limits, security alerts, outages):
# wait RETRY_MAX straight away instead of retrying quickly.
GMAIL_BACKOFF_MARKERS = (
    "[ALERT]",
    "[WEBALERT",
    "[UNAVAILABLE]",
    "[THROTTLED]",
    "[OVERQUOTA]",
    "simultaneous connections",
)
# Login errors that won't fix themselves: stop instead of retrying.
LOGIN_FAILURE_MARKERS = ("AUTHENTICATIONFAILED", "Invalid credentials", "Application-specific password required")
# Most files listed in a "session closed" reply.
MAX_FILES_LISTED = 30

# Exit codes. EXIT_NO_RESTART tells systemd not to restart (see the service unit).
EXIT_OK, EXIT_ERROR, EXIT_NO_RESTART = 0, 1, 2

Connect = Callable[[str, str], AbstractContextManager[Gmail]]


def build_prompt(title: str, body: str) -> str:
    # Starts with fixed text so the prompt can never be read as a command-line flag.
    parts = [f"Task from email: {title}" if title else "Task from email:"]
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def task_hash(tool: str, title: str, body: str) -> str:
    return hashlib.sha256(f"{tool}\0{title.strip().lower()}\0{body.strip()}".encode()).hexdigest()


@dataclass
class RunningSession:
    proc: subprocess.Popen[bytes]
    pidfd: int  # becomes readable when the session's window closes
    folder: Path
    task: Task
    started: float
    watch: FolderWatch | None  # wakes the listener when the started marker appears
    # "starting" until the agent takes its prompt, then "running"; "abandoned"
    # once AgentRelay has given up on it and closed it.
    phase: str = "starting"

    @property
    def relay_dir(self) -> Path:
        return self.folder / RELAY_DIR


class Relay:
    def __init__(self, cfg: Config, state: State, replier: Replier, launcher: Launcher, *, dry_run: bool = False):
        self.cfg, self.state, self.replier, self.launcher = cfg, state, replier, launcher
        self.limits = cfg.limits
        self.dry_run = dry_run
        self.seen: set[str] = set()  # UIDs already dealt with this run
        self.arrivals: list[datetime] = []  # received times of trusted task mail (flood breaker)
        self.paused = False  # flood breaker tripped; cleared only by a restart
        self.running: RunningSession | None = None

    # ---- intake: decide what to do with each new task email

    def check_mail(self, gmail: Gmail) -> None:
        cutoff = datetime.now() - self.limits.max_age
        # SINCE only takes a date (in Gmail's timezone), so search from the day
        # before and compare exact received times in intake().
        uids = gmail.search_tasks(self.cfg.allowed_senders, (cutoff - timedelta(days=1)).date())
        new = [u for u in uids if u not in self.seen]
        if not new:
            return
        for fetched in gmail.fetch_headers(new):
            if not self.dry_run:
                self.seen.add(fetched.uid)
            self.intake(gmail, fetched, cutoff)
        self.state.save()

    def intake(self, gmail: Gmail, f: Fetched, cutoff: datetime) -> None:
        if f.received is None or f.received < cutoff or f.msg.get(REPLY_HEADER):
            return  # too old, or one of our own replies
        subject = str(f.msg.get("Subject", ""))
        m = SUBJECT_RE.match(subject)
        if not m:
            return  # "claude"/"codex" elsewhere in the subject
        tool, title = m.group(1).lower(), m.group(2).strip()
        msg_id = str(f.msg.get("Message-ID", "")).strip() or f"uid:{f.uid}"
        if msg_id in self.state.processed:
            return
        trusted, sender = is_trusted(f.msg, f.meta, self.cfg.allowed_senders)
        if not trusted:
            log.warning("Ignored '%s:' mail from %r: not an allowed sender or failed authentication", tool, sender)
            return

        # From here every outcome is final for this email.
        self.state.processed[msg_id] = datetime.now().isoformat()
        task = Task(msg_id=msg_id, sender=sender, subject=subject, tool=tool, title=title)
        log.info("Mail from %s: %s: %s", sender, tool, title[:60])

        if self.paused:
            log.info("  Discarded: paused after a flood (restart to resume)")
            return
        self.arrivals = [t for t in self.arrivals if t >= f.received - self.limits.flood_window]
        self.arrivals.append(f.received)
        if len(self.arrivals) > self.limits.flood_count:
            self.trip_flood_breaker(task)
            return
        if len(self.state.queue) >= self.limits.queue_max:
            log.warning("  Refused: queue full (%d waiting)", self.limits.queue_max)
            return

        body = gmail.fetch_body_text(f.uid)
        if not title and not body:
            log.info("  Empty task")
            self.replier.reply(
                task,
                f"This email has no task in it. Put the task after '{tool}:' in the subject, "
                "or in the body, and send it again.",
            )
            return
        h = task_hash(tool, title, body)
        if time.time() - self.state.recent.get(h, 0) < self.limits.duplicate_window.total_seconds():
            log.info("  Duplicate of a recent task; ignored")
            return
        self.state.recent[h] = time.time()
        task.hash = h

        busy = self.running is not None or bool(self.state.queue)
        task.prompt = build_prompt(title, body)
        self.state.queue.append(task)
        position = len(self.state.queue)
        log.info("  Queued (position %d)", position)
        if busy:
            self.replier.reply(
                task,
                f"Queued: position {position} of at most {self.limits.queue_max}. "
                "It will start when the sessions ahead of it have closed.",
            )

    def trip_flood_breaker(self, task: Task) -> None:
        self.paused = True
        dropped = len(self.state.queue)
        self.state.queue.clear()
        self.state.listener["paused"] = True
        minutes = int(self.limits.flood_window.total_seconds() // 60)
        log.error(
            "FLOOD: more than %d task emails within %d minutes; paused and dropped %d queued task(s). "
            "Restart to resume.",
            self.limits.flood_count,
            minutes,
            dropped,
        )
        self.replier.reply(
            task,
            f"Flood detected: more than {self.limits.flood_count} task emails arrived within {minutes} minutes.\n\n"
            f"AgentRelay has paused. It dropped {dropped} queued task(s) and will discard every new task "
            "email, starting nothing and replying to nothing, until it is restarted by hand. "
            "A session already running is left alone.",
        )

    # ---- sessions

    def start_next(self) -> None:
        """Start the oldest queued task, if nothing is running."""
        while self.running is None and self.state.queue and not self.paused:
            task = self.state.queue.pop(0)
            self.state.save()
            if self.dry_run:
                log.info("[dry run] would start %s with prompt:\n%s\n", task.tool, task.prompt)
                continue
            exe = self.launcher.find_tool(task.tool)
            if not exe:
                log.error("  %s not found on PATH", task.tool)
                self.replier.reply(task, f"Couldn't start: {task.tool} isn't installed or isn't on PATH.")
                continue
            folder = self.launcher.new_folder(task.tool)
            (folder / PROMPT_FILE).write_text(task.prompt + "\n")
            relay_dir = folder / RELAY_DIR
            relay_dir.mkdir()
            extra = (*self.cfg.tool_args.get(task.tool, ()), *started_hook_args(task.tool, relay_dir / STARTED_MARKER))
            cmd = with_pid_file(build_command(exe, task.tool, extra, task.prompt), relay_dir / PID_FILE)
            watch = FolderWatch(relay_dir)
            proc = self.launcher.open(folder, cmd)
            if not proc:
                watch.close()
                log.error("  No terminal emulator found")
                self.replier.reply(task, "Couldn't start: no terminal emulator found.")
                continue
            self.running = RunningSession(proc, os.pidfd_open(proc.pid), folder, task, time.time(), watch)
            self.state.listener["running"] = {
                "tool": task.tool,
                "title": task.title,
                "folder": str(folder),
                "started": time.time(),
                "phase": "starting",
            }
            self.state.save()
            log.info("Opened %s in %s; waiting for it to take the prompt", task.tool, folder)
            self.check_started()  # in case it was already quick

    def check_started(self) -> None:
        """Send "Started" once the agent's hook creates the marker. If it hasn't
        within the start timeout, the agent is stuck on something (a login, an
        update, some other prompt): close it, say so, and move on."""
        r = self.running
        if r is None or r.phase != "starting":
            return
        if r.watch:
            r.watch.drain()
        if (r.relay_dir / STARTED_MARKER).exists():
            r.phase = "running"
            self._stop_watching(r)
            log.info("  %s took the prompt", r.task.tool)
            self.replier.reply(
                r.task, f"Started {r.task.tool} in {r.folder}\nThe prompt is saved there as {PROMPT_FILE}."
            )
            return
        timeout = self.limits.start_timeout.total_seconds()
        if time.time() - r.started < timeout:
            return
        r.phase = "abandoned"
        self._stop_watching(r)
        if r.task.hash:  # so the same task can be sent again straight away
            self.state.recent.pop(r.task.hash, None)
        minutes = int(timeout // 60)
        log.warning("  %s didn't take the prompt within %d min; closing it", r.task.tool, minutes)
        self._close_agent(r)
        self.replier.reply(
            r.task,
            f"Didn't start: {r.task.tool} hadn't taken the prompt after {minutes} min, so AgentRelay closed it.\n\n"
            f"It was probably waiting on a login, an update or another prompt; run {r.task.tool} by hand to see. "
            "Send the task again to retry, with 'claude:' or 'codex:'.",
        )

    def _stop_watching(self, r: RunningSession) -> None:
        if r.watch:
            r.watch.close()
            r.watch = None
        if self.state.listener.get("running"):
            self.state.listener["running"]["phase"] = r.phase
        self.state.save()

    def _close_agent(self, r: RunningSession) -> None:
        """Stop the agent, which closes its terminal window. Falls back to the
        terminal process if the agent's process ID was never written."""
        try:
            pid = int((r.relay_dir / PID_FILE).read_text())
        except (OSError, ValueError):
            log.warning("  No agent process ID recorded; closing the terminal process instead")
            r.proc.terminate()
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            for _ in range(50):  # up to 5 seconds
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return

    def finish_session_if_closed(self) -> None:
        r = self.running
        if r is None or r.proc.poll() is None:
            return
        os.close(r.pidfd)
        if r.watch:
            r.watch.close()
        self.running = None
        self.state.listener["running"] = None
        self.state.save()
        if r.phase == "abandoned":
            return  # already replied "Didn't start"
        if r.phase == "starting":
            log.info("Session closed before it took the prompt")
            self.replier.reply(r.task, f"Closed before {r.task.tool} took the prompt; nothing ran.")
            return
        minutes = int((time.time() - r.started) // 60)
        files, more = list_files(r.folder, MAX_FILES_LISTED)
        listing = "\n".join(f"  {f}" for f in files) or "  (no files)"
        if more:
            listing += f"\n  ...and {more} more"
        log.info("Session closed after %d min", minutes)
        self.replier.reply(r.task, f"Session closed after {minutes} min.\n\nFiles in {r.folder}:\n{listing}")

    def wait_fds(self) -> list[int]:
        """What should wake the listener: the session window closing, or the
        started marker appearing."""
        r = self.running
        if r is None:
            return []
        return [r.pidfd] + ([r.watch.fd] if r.watch else [])

    def wait_seconds(self, longest: float) -> float:
        """How long the listener may sleep: no later than the start deadline."""
        r = self.running
        if r is None or r.phase != "starting":
            return longest
        deadline = r.started + self.limits.start_timeout.total_seconds()
        return max(1.0, min(longest, deadline - time.time() + 0.5))

    # ---- listener bookkeeping for `agent-relay status`

    def mark_started(self) -> None:
        self.state.listener = {
            "pid": os.getpid(),
            "started": datetime.now().isoformat(timespec="seconds"),
            "paused": False,
            "running": None,
        }
        self.state.save()

    def mark_stopped(self) -> None:
        self.state.listener = {}
        self.state.save()


def _backoff_messages(error: imaplib.IMAP4.error) -> tuple[bool, bool]:
    text = str(error)
    return any(m in text for m in LOGIN_FAILURE_MARKERS), any(m in text for m in GMAIL_BACKOFF_MARKERS)


def run(
    relay: Relay,
    *,
    once: bool = False,
    connect: Connect = Gmail.connect,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Listen until interrupted (or one pass with once=True). Returns an exit code."""
    cfg = relay.cfg
    retry = RETRY_MIN
    relay.mark_started()
    try:
        while True:
            connected_at: float | None = None
            gmail_said_back_off = False
            try:
                with connect(cfg.gmail_user, cfg.app_password) as gmail:
                    connected_at = clock()
                    while True:
                        relay.finish_session_if_closed()
                        relay.check_started()
                        relay.check_mail(gmail)
                        relay.start_next()
                        if once:
                            return EXIT_OK
                        # Sleep until Gmail reports new mail, the session starts or closes,
                        # the start deadline passes, or it's time to refresh.
                        gmail.idle_wait(relay.wait_seconds(IDLE_SECONDS), relay.wait_fds())
            except imaplib.IMAP4.error as e:
                login_failed, gmail_said_back_off = _backoff_messages(e)
                if login_failed:
                    log.error("Login failed: %s. Check AGENT_RELAY_GMAIL_USER and AGENT_RELAY_APP_PASSWORD.", e)
                    return EXIT_NO_RESTART
                problem = f"IMAP error: {e}"
            except (OSError, EOFError) as e:  # includes timeouts on a dead connection
                problem = f"Connection error: {str(e) or type(e).__name__}"
            if once:
                log.error("%s", problem)
                return EXIT_ERROR

            if gmail_said_back_off:
                retry = RETRY_MAX
            elif connected_at is not None and clock() - connected_at >= HEALTHY_AFTER:
                retry = RETRY_MIN  # the connection had been fine for a while
            log.warning("%s; reconnecting in %dm %ds", problem, retry // 60, retry % 60)
            sleep(retry)
            retry = min(retry * 2, RETRY_MAX)
    finally:
        relay.mark_stopped()
