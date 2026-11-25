"""The relay: turns task emails into sessions, one at a time, with safeguards."""

from __future__ import annotations

import hashlib
import imaplib
import logging
import os
import re
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
from agent_relay.sessions import PROMPT_FILE, Launcher, build_command, list_files
from agent_relay.state import State, Task
from agent_relay.trust import is_trusted

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
            cmd = build_command(exe, task.tool, self.cfg.tool_args.get(task.tool, ()), task.prompt)
            proc = self.launcher.open(folder, cmd)
            if not proc:
                log.error("  No terminal emulator found")
                self.replier.reply(task, "Couldn't start: no terminal emulator found.")
                continue
            self.running = RunningSession(proc, os.pidfd_open(proc.pid), folder, task, time.time())
            self.state.listener["running"] = {
                "tool": task.tool,
                "title": task.title,
                "folder": str(folder),
                "started": time.time(),
            }
            self.state.save()
            log.info("Started %s in %s", task.tool, folder)
            self.replier.reply(task, f"Started {task.tool} in {folder}\nThe prompt is saved there as {PROMPT_FILE}.")

    def finish_session_if_closed(self) -> None:
        r = self.running
        if r is None or r.proc.poll() is None:
            return
        os.close(r.pidfd)
        self.running = None
        self.state.listener["running"] = None
        self.state.save()
        minutes = int((time.time() - r.started) // 60)
        files, more = list_files(r.folder, MAX_FILES_LISTED)
        listing = "\n".join(f"  {f}" for f in files) or "  (no files)"
        if more:
            listing += f"\n  ...and {more} more"
        log.info("Session closed after %d min", minutes)
        self.replier.reply(r.task, f"Session closed after {minutes} min.\n\nFiles in {r.folder}:\n{listing}")

    def session_fd(self) -> int | None:
        return self.running.pidfd if self.running else None

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
                        relay.check_mail(gmail)
                        relay.start_next()
                        if once:
                            return EXIT_OK
                        # Sleep until Gmail reports new mail, the session closes, or it's time to refresh.
                        gmail.idle_wait(IDLE_SECONDS, relay.session_fd())
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
