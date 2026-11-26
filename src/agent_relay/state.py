"""Persistent state (what has been handled, the queue, reply counts) and the
single-instance lock."""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from agent_relay.config import Limits


@dataclass
class Task:
    """A task email that has been accepted."""

    msg_id: str
    sender: str
    subject: str
    tool: str
    title: str
    prompt: str = ""
    preface: str = ""  # put at the start of its first reply (e.g. a stuck session was closed for it)
    body_cut: bool = False  # the email body was over the download limit; only its start was used
    hash: str = ""  # for duplicate detection; cleared if the session never starts


class State:
    """JSON-backed state. Nothing is written in dry-run mode.

    processed: Message-ID -> ISO time it was handled (so it never runs twice)
    queue:     tasks waiting for a session
    replies:   times replies were sent (for the reply budget)
    recent:    task hash -> time first seen (for duplicate detection)
    listener:  what the running listener is doing, for `agent-relay status`
    """

    def __init__(self, path: Path, data: dict[str, Any], limits: Limits, dry_run: bool = False):
        self.path, self.limits, self.dry_run = path, limits, dry_run
        self.processed: dict[str, str] = data.get("processed", {})
        self.queue: list[Task] = [Task(**t) for t in data.get("queue", [])]
        self.replies: list[float] = data.get("replies", [])
        self.recent: dict[str, float] = data.get("recent", {})
        self.listener: dict[str, Any] = data.get("listener", {})

    @classmethod
    def load(cls, path: Path, limits: Limits, dry_run: bool = False) -> State:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        return cls(path, data, limits, dry_run)

    def replies_in_window(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return sum(1 for t in self.replies if t >= now - self.limits.reply_window.total_seconds())

    def save(self) -> None:
        if self.dry_run:
            return
        now = time.time()
        keep_from = datetime.fromtimestamp(now - self.limits.processed_keep.total_seconds()).isoformat()
        self.processed = {k: v for k, v in self.processed.items() if v >= keep_from}
        self.replies = [t for t in self.replies if t >= now - self.limits.reply_window.total_seconds()]
        self.recent = {k: v for k, v in self.recent.items() if v >= now - self.limits.duplicate_window.total_seconds()}
        data = {
            "processed": self.processed,
            "queue": [asdict(t) for t in self.queue],
            "replies": self.replies,
            "recent": self.recent,
            "listener": self.listener,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)


class AlreadyRunning(Exception):
    def __init__(self, pid: str):
        super().__init__(f"another listener is already running (pid {pid})")
        self.pid = pid


def acquire_lock(path: Path) -> IO[str]:
    """Take the single-instance lock, or raise AlreadyRunning.

    An OS file lock is released automatically when the process exits or
    crashes, so a leftover lock file can never block a restart. Keep the
    returned file open for as long as the lock should be held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "a+")  # noqa: SIM115 - held open for the life of the process
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        pid = lock.read().strip() or "unknown"
        lock.close()
        raise AlreadyRunning(pid) from None
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock


def lock_holder(path: Path) -> str | None:
    """PID of the running listener, or None if no listener holds the lock."""
    if not path.exists():
        return None
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            return f.read().strip() or "unknown"
        fcntl.flock(f, fcntl.LOCK_UN)
        return None
