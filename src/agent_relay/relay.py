"""The relay: turns task emails into sessions, one at a time, with safeguards."""

from __future__ import annotations

import hashlib
import imaplib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agent_relay.config import Config
from agent_relay.gmail import MAX_BODY_BYTES, REPLY_HEADER, Fetched, Gmail
from agent_relay.render import pre
from agent_relay.replies import Replier
from agent_relay.sessions import (
    ACTIVE_MARKER,
    PID_FILE,
    PROMPT_FILE,
    RELAY_DIR,
    STARTED_MARKER,
    STOP_FILE,
    Launcher,
    build_command,
    hook_args,
    list_files,
    nothing_created,
    with_pid_file,
)
from agent_relay.state import State, Task
from agent_relay.trust import is_trusted
from agent_relay.watch import FolderWatch

log = logging.getLogger(__name__)

SUBJECT_RE = re.compile(r"^\s*(claude|codex|agent)\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)
# A reply in one of those threads ("Re: claude: ..."): its first line is an agent: command.
REPLY_RE = re.compile(r"^(\s*re\s*:)+", re.IGNORECASE)
AGENT_PREFIX_RE = re.compile(r"^\s*agent\s*:\s*", re.IGNORECASE)
# Commands that act on the session. Sent as a reply in a task's thread, they
# only act if that task's session is the one running.
SESSION_COMMANDS = ("stop", "type", "keys")
# "agent: <command>" emails control AgentRelay itself.
COMMANDS_HELP = """\
Commands (reply to any AgentRelay email with just the command, e.g. 'stop' or
'type: 1', or send 'agent: <command>' as a subject; the colon after the command
is optional):
  agent: status         what is running, the queue, replies left today
  agent: stop           close the running session now; the next task starts
  agent: screen         what is on the running session's screen
  agent: type: <text>   type into the session and press Enter (or put the text
                        on the body's first line); replies with the screen
  agent: keys: <keys>   press keys, e.g. 'keys: down down enter'; replies
                        with the screen. Keys: enter, esc, tab, shift-tab, space,
                        backspace, up, down, left, right, ctrl-<letter>, or
                        any single character
  agent: reset          reset the reply count after the daily limit is reached
  agent: help           this list

Tasks: 'claude: <task>' or 'codex: <task>', with more detail in the body if needed.
A new task closes the open session unless it is working (then it queues)."""
# Key names for "agent: keys", as tmux names them.
KEY_NAMES = {
    "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "shift-tab": "BTab", "space": "Space", "backspace": "BSpace", "delete": "DC",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "PPage", "pagedown": "NPage",
}  # fmt: skip
# Sessions AgentRelay closed itself (it has already replied about them).
CLOSED_BY_RELAY = ("finished", "stopped")
# A session that hasn't taken its prompt this long after opening is stuck on
# something (a login, an update question): say so, with its screen.
WAITING_NOTICE_SECONDS = 10

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
# Most files listed in a "finished" or "session closed" reply.
MAX_FILES_LISTED = 30
# The agent's last message in a closing reply is cut to this many characters,
# and a stuck session's screen to this many lines.
MAX_LAST_MESSAGE = 4000
MAX_SCREEN_LINES = 60

# Exit codes. EXIT_NO_RESTART tells systemd not to restart (see the service unit).
EXIT_OK, EXIT_ERROR, EXIT_NO_RESTART = 0, 1, 2

Connect = Callable[[str, str], AbstractContextManager[Gmail]]


def build_prompt(title: str, body: str) -> str:
    """The subject, a blank line, then the body. Leading hyphens are stripped so
    the prompt can never be read as a command-line flag."""
    return "\n\n".join(part for part in (title, body) if part).lstrip("-").strip()


# Linux refuses a single command-line argument over 128 KiB; longer prompts are
# passed as a pointer to the saved prompt file instead (with room to spare).
MAX_PROMPT_ARG_BYTES = 100_000
PROMPT_POINTER = f"Your task is in {PROMPT_FILE} in this folder. Read it and carry it out."


def prompt_arg(prompt: str) -> str:
    """The prompt itself, or a pointer to PROMPT_FILE if it's too long to pass."""
    return prompt if len(prompt.encode()) <= MAX_PROMPT_ARG_BYTES else PROMPT_POINTER


def task_hash(tool: str, title: str, body: str) -> str:
    return hashlib.sha256(f"{tool}\0{title.strip().lower()}\0{body.strip()}".encode()).hexdigest()


@dataclass
class RunningSession:
    proc: subprocess.Popen[bytes]
    pidfd: int  # becomes readable when the session's window closes
    folder: Path
    task: Task
    started: float
    watch: FolderWatch | None  # wakes the listener when one of the agent's hooks reports
    # "starting" until the agent takes its prompt, then "running"; "idle" when it
    # has finished but is left open for follow-ups (nothing queued); "finished"
    # or "stopped" once AgentRelay has closed it.
    phase: str = "starting"
    done_at: float = 0.0  # time of the "finished turn" already reported
    waiting_sent: bool = False  # the "Waiting for input" reply has gone out
    waiting_logged: float = 0.0  # the finished-turn time already logged as waiting on background work

    @property
    def relay_dir(self) -> Path:
        return self.folder / RELAY_DIR

    def agent_pid(self) -> int | None:
        try:
            return int((self.relay_dir / PID_FILE).read_text())
        except (OSError, ValueError):
            return None

    def read_stop(self) -> dict[str, object]:
        """What the agent's "finished turn" hook received (last message etc.)."""
        try:
            data = json.loads((self.relay_dir / STOP_FILE).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def duration(seconds: float) -> str:
    """45 sec, 12 min, 2 h 5 min."""
    s = int(seconds)
    if s < 60:
        return f"{s} sec"
    if s < 3600:
        return f"{s // 60} min"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _last_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


class Relay:
    def __init__(self, cfg: Config, state: State, replier: Replier, launcher: Launcher, *, dry_run: bool = False):
        self.cfg, self.state, self.replier, self.launcher = cfg, state, replier, launcher
        self.limits = cfg.limits
        self.dry_run = dry_run
        self.seen: set[str] = set()  # UIDs already dealt with this run
        self.arrivals: list[datetime] = []  # received times of trusted task mail (flood breaker)
        self.paused = False  # flood breaker tripped; cleared only by a restart
        self.running: RunningSession | None = None
        self.settle_seconds = 2.0  # after typing into a session, before reading its screen

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
        reply = REPLY_RE.match(subject)
        m = SUBJECT_RE.match(subject[reply.end() :] if reply else subject)
        if not m:
            return  # "claude"/"codex"/"agent" elsewhere in the subject
        tool, title = m.group(1).lower(), m.group(2).strip()
        tool_of_thread = tool
        if reply:  # the command is in the body; read once the sender is trusted
            tool, title = "agent", ""
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
        if tool == "agent":
            if reply:
                first = next((line for line in gmail.fetch_body_text(f.uid).splitlines() if line.strip()), "")
                task.title = AGENT_PREFIX_RE.sub("", first).strip()
                thread = None  # replies in an "agent:" thread act on whatever is running
                if tool_of_thread != "agent":
                    refs = f"{f.msg.get('In-Reply-To', '')} {f.msg.get('References', '')}"
                    thread = set(re.findall(r"<[^<>\s]+>", refs))
                self.run_command(task, thread=thread)
            else:
                self.run_command(task, lambda: gmail.fetch_body_text(f.uid))
            return
        if len(self.state.queue) >= self.limits.queue_max:
            log.warning("  Refused: queue full (%d waiting)", self.limits.queue_max)
            return

        body, task.body_cut = gmail.fetch_body(f.uid)
        prompt = build_prompt(title, body)
        if not prompt:
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

        closed = ""
        r = self.running
        if r is not None and r.phase in ("starting", "idle"):
            how = "stuck" if r.phase == "starting" else "finished"
            log.info("  Closing the %s %s session (%s)", how, r.task.tool, r.task.title[:60])
            closed = f"Closed the {how} {r.task.tool} session ({r.task.title[:80]}).\n\n"
            if r.phase == "starting":
                self._forget(r.task)  # it never ran, so it can be sent again
            self._set_phase(r, "stopped")
            self._close_agent(r)
        busy = (self.running is not None and not closed) or bool(self.state.queue)
        task.prompt = prompt
        self.state.queue.append(task)
        position = len(self.state.queue)
        log.info("  Queued (position %d)", position)
        if busy:
            self.replier.reply(
                task,
                f"{closed}Queued: position {position} of at most {self.limits.queue_max}. "
                "It will start when the sessions ahead of it have closed.",
            )
        else:
            task.preface = closed  # said in its Started / Waiting reply instead

    # ---- agent: commands

    def run_command(self, task: Task, body: Callable[[], str] = lambda: "", thread: set[str] | None = None) -> None:
        """Answer an "agent: <command>" email. Commands count towards the flood
        breaker and the reply budget like tasks, but never queue. `thread` holds
        the Message-IDs a reply refers to, when it was sent in a task's thread."""
        # "type hello", "type: hello" and "type:hello" all work.
        m = re.match(r"\s*([A-Za-z]+)(?:\s*:|\s+|$)(.*)", task.title, re.DOTALL)
        words = task.title.split()
        command, rest = (m.group(1).lower(), m.group(2).strip()) if m else (words[0].lower() if words else "", "")
        log.info("  Command: %s", command or "(none)")
        r = self.running
        if thread is not None and command in SESSION_COMMANDS and (r is None or r.task.msg_id not in thread):
            log.info("  Reply from the thread of a session that has ended; nothing done")
            self.replier.reply(
                task,
                "That session has ended, so nothing was done. Reply in the running session's thread, "
                "or send it with an 'agent:' subject.",
            )
        elif command == "status":
            self.replier.reply(task, self.status_text())
        elif command == "stop":
            self.replier.reply(task, self.stop_session())
        elif command == "screen":
            self.replier.reply(task, self.screen_text())
        elif command == "type":
            if not rest:
                rest = next((line.strip() for line in body().splitlines() if line.strip()), "")
            self.replier.reply(task, self.type_text(rest))
        elif command == "keys":
            self.replier.reply(task, self.press_keys(rest))
        elif command == "reset":
            used = self.state.replies_in_window()
            self.state.replies.clear()
            self.state.save()
            log.info("  Reply count reset (%d used)", used)
            self.replier.reply(
                task, f"Reply count reset: {used} used, now 0 of {self.limits.reply_budget} for the next 24 hours."
            )
        elif command == "help":
            self.replier.reply(task, pre(COMMANDS_HELP))
        else:
            unknown = f"Unknown command '{command}'." if command else "No command given."
            self.replier.reply(task, f"{unknown}\n\n{pre(COMMANDS_HELP)}")

    def status_text(self) -> str:
        lines = []
        if self.paused:
            lines.append("PAUSED after a flood: nothing starts until AgentRelay is restarted.")
        r = self.running
        if r is None:
            lines.append("Running: nothing")
        else:
            notes = {"starting": " (waiting for it to take the prompt)", "idle": " (finished; open for follow-ups)"}
            waiting = notes.get(r.phase, "")
            took = duration(time.time() - r.started)
            lines.append(f"Running: {r.task.tool}: {r.task.title[:80]} ({took}){waiting}\n  {r.folder}")
        lines.append(f"Queue: {len(self.state.queue)} of {self.limits.queue_max}")
        lines += [f"  {i}. {t.tool}: {t.title[:80]}" for i, t in enumerate(self.state.queue, 1)]
        # +1: this reply is about to be sent
        lines.append(
            f"Replies: {self.state.replies_in_window() + 1} of {self.limits.reply_budget} used in the last 24 hours"
        )
        return "\n".join(lines)

    def stop_session(self) -> str:
        r = self.running
        if r is None or r.phase in CLOSED_BY_RELAY:
            return "Nothing is running."
        took = duration(time.time() - r.started)
        log.info("  Stopping %s after %s", r.task.tool, took)
        stop = r.read_stop()
        summary = self._summary(r, stop) if r.phase == "running" else ""
        self._forget(r.task)
        self._set_phase(r, "stopped")
        self._close_agent(r)
        return f"Stopped {r.task.tool} after {took}: {r.task.title[:80]}{summary}"

    def type_text(self, text: str) -> str:
        if not text:
            return "Nothing to type: put the text after 'agent: type', or on the first line of the body."
        return self._send([(True, text), (False, "Enter")], f"Typed '{text}' and pressed Enter.")

    def press_keys(self, spec: str) -> str:
        steps: list[tuple[bool, str]] = []
        for word in spec.split():
            key = word.lower()
            if key in KEY_NAMES:
                steps.append((False, KEY_NAMES[key]))
            elif re.fullmatch(r"ctrl-[a-z]", key):
                steps.append((False, f"C-{key[-1]}"))
            elif len(word) == 1:
                steps.append((True, word))
            else:
                return f"Unknown key '{word}'; nothing was pressed.\n\n{pre(COMMANDS_HELP)}"
        if not steps:
            return f"No keys given.\n\n{pre(COMMANDS_HELP)}"
        return self._send(steps, f"Pressed {spec}.")

    def _send(self, steps: list[tuple[bool, str]], done: str) -> str:
        r = self.running
        if r is None or r.phase in CLOSED_BY_RELAY:
            return "Nothing is running."
        if not self.launcher.send(r.folder, steps):
            return "Couldn't type into the session (is tmux installed?)."
        if r.phase == "idle":  # a follow-up: report again when this turn finishes
            self._set_phase(r, "running")
        log.info("  %s", done)
        time.sleep(self.settle_seconds)  # let the screen catch up before reading it
        return f"{done}\n\n{self.screen_text()}"

    def _forget(self, task: Task) -> None:
        """Drop a task from the duplicate check, so sending it again starts it again."""
        if task.hash:
            self.state.recent.pop(task.hash, None)

    def screen_text(self) -> str:
        r = self.running
        if r is None:
            return "Nothing is running."
        screen = self.launcher.capture(r.folder)
        if not screen:
            return "Couldn't read the screen (is tmux installed?)."
        return f"{r.task.tool}: {r.task.title[:80]}\n\n{pre(_last_lines(screen, MAX_SCREEN_LINES))}"

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
            extra = (*self.cfg.tool_args.get(task.tool, ()), *hook_args(task.tool, relay_dir))
            cmd = with_pid_file(build_command(exe, task.tool, extra, prompt_arg(task.prompt)), relay_dir / PID_FILE)
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
        """Send "Started" as soon as the agent's prompt hook fires. If it hasn't
        within WAITING_NOTICE_SECONDS, the agent is stuck on something (a login,
        an update, some other prompt): send its screen instead and leave it open
        for "agent: type/keys". Nothing more is sent when it then starts."""
        r = self.running
        if r is None or r.phase != "starting":
            return
        if r.watch:
            r.watch.drain()
        if (r.relay_dir / STARTED_MARKER).exists():
            self._set_phase(r, "running")
            log.info("  %s took the prompt", r.task.tool)
            if r.waiting_sent:
                return
            cut = ""
            if r.task.body_cut:
                cut = f"\n\nThe email body was over {MAX_BODY_BYTES // 1_000_000} MB, so only the first part was used."
            self.replier.reply(
                r.task,
                f"{r.task.preface}Started {r.task.tool} in {r.folder}\n"
                f"The prompt is saved there as {PROMPT_FILE}.{cut}",
            )
            return
        if r.waiting_sent or time.time() < r.started + WAITING_NOTICE_SECONDS:
            return
        r.waiting_sent = True
        log.warning("  %s hasn't taken the prompt; waiting for input", r.task.tool)
        screen = self.launcher.capture(r.folder)
        text = (
            f"{r.task.preface}Waiting for input: {r.task.tool} hasn't taken the prompt. "
            "It's probably on a login, an update or another question. Answer it with "
            "'agent: type <text>' or 'agent: keys <keys>', or send a new task to close it and move on. "
            "'agent: stop' closes it too."
        )
        text += f"\n\n{pre(_last_lines(screen, MAX_SCREEN_LINES))}" if screen else "\n\n(Couldn't read its screen.)"
        self.replier.reply(r.task, text)

    def check_finished(self) -> None:
        """Close the session once the agent has finished: its "finished turn" hook
        fired after its last activity, with no background tasks left. (Claude
        lists its background tasks in that hook and starts a new turn when they
        end. Codex reports none, so for Codex the hook alone counts.)"""
        r = self.running
        if r is None or r.phase != "running":
            return
        if r.watch:
            r.watch.drain()
        stopped_at = _mtime(r.relay_dir / STOP_FILE)
        latest = max(_mtime(r.relay_dir / STARTED_MARKER), _mtime(r.relay_dir / ACTIVE_MARKER), r.done_at)
        if stopped_at <= latest:
            return  # still working, or hasn't finished a turn yet
        stop = r.read_stop()
        tasks = stop.get("background_tasks")
        if isinstance(tasks, list) and tasks:
            if r.waiting_logged != stopped_at:
                r.waiting_logged = stopped_at
                log.info("  %s finished its turn with %d background task(s) running; waiting", r.task.tool, len(tasks))
            return
        took = duration(time.time() - r.started)
        r.done_at = stopped_at
        if self.state.queue:  # the next task is waiting for it
            log.info("  %s finished after %s; closing it", r.task.tool, took)
            self._set_phase(r, "finished")
            self._close_agent(r)
            self.replier.reply(r.task, f"Finished after {took}.{self._summary(r, stop)}")
            return
        log.info("  %s finished after %s; left open until the next task", r.task.tool, took)
        self._set_phase(r, "idle")
        self.replier.reply(r.task, f"Finished after {took}.{self._summary(r, stop, closing=False)}")

    def _summary(self, r: RunningSession, stop: dict[str, object], *, closing: bool = True) -> str:
        """The agent's last message and the files it left, for a closing reply."""
        text = ""
        last = stop.get("last_assistant_message")
        if isinstance(last, str) and last.strip():
            last = last.strip()
            if len(last) > MAX_LAST_MESSAGE:
                last = last[:MAX_LAST_MESSAGE].rstrip() + "\n[...cut short]"
            text += f"\n\nLast message from {r.task.tool}:\n\n{pre(last)}"
        if nothing_created(r.folder):
            gone = ", so the session folder has been removed" if closing else ""
            return text + f"\n\nNo files were created{gone}."
        files, more = list_files(r.folder, MAX_FILES_LISTED)
        listing = "\n".join(f"  {f}" for f in files) or "  (no files)"
        if more:
            listing += f"\n  ...and {more} more"
        return text + f"\n\nFiles in {r.folder}:\n{pre(listing)}"

    def _set_phase(self, r: RunningSession, phase: str) -> None:
        r.phase = phase
        if phase in CLOSED_BY_RELAY and r.watch:
            r.watch.close()
            r.watch = None
        if self.state.listener.get("running"):
            self.state.listener["running"]["phase"] = phase
        self.state.save()

    def _close_agent(self, r: RunningSession) -> None:
        """Close the session's window: through tmux, else by stopping the agent
        (recorded process ID), else by stopping the terminal process."""
        if self.launcher.close(r.folder):
            return
        pid = r.agent_pid()
        if pid is None:
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
        self._reply_closed(r)
        self._remove_if_empty(r)

    def _reply_closed(self, r: RunningSession) -> None:
        if r.phase in (*CLOSED_BY_RELAY, "idle"):
            return  # already reported: closed by AgentRelay, or closed after finishing
        if r.phase == "starting":
            log.info("Session closed before it took the prompt")
            self.replier.reply(r.task, f"Closed before {r.task.tool} took the prompt; nothing ran.")
            return
        took = duration(time.time() - r.started)
        log.info("Session closed after %s", took)
        self.replier.reply(r.task, f"Session closed after {took}.{self._summary(r, r.read_stop())}")

    def _remove_if_empty(self, r: RunningSession) -> None:
        """Delete a closed session's folder if the agent created nothing in it,
        so empty folders don't pile up. The conversation itself is kept by the
        agent and can still be resumed."""
        folder = r.folder
        if folder.parent != self.launcher.sessions_dir or not nothing_created(folder):
            return
        shutil.rmtree(folder, ignore_errors=True)
        log.info("  Removed the empty session folder %s", folder)

    def wait_fds(self) -> list[int]:
        """What should wake the listener: the session window closing, or one of
        the agent's hooks reporting."""
        r = self.running
        if r is None:
            return []
        return [r.pidfd] + ([r.watch.fd] if r.watch else [])

    def wait_seconds(self, longest: float) -> float:
        """How long the listener may sleep: no later than the "Waiting for input" notice."""
        r = self.running
        if r is None or r.phase != "starting" or r.waiting_sent:
            return longest
        return max(1.0, min(longest, r.started + WAITING_NOTICE_SECONDS - time.time() + 0.5))

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
                        relay.check_finished()
                        relay.check_mail(gmail)
                        relay.start_next()
                        if once:
                            return EXIT_OK
                        # Sleep until Gmail reports new mail, an agent hook reports, the
                        # session closes, a session check is due, or it's time to refresh.
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
