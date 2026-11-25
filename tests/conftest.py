"""Shared fakes: a Gmail mailbox speaking imaplib's response shapes, an SMTP
server that records messages, and a launcher that runs `sleep` instead of a
terminal. Nothing in the test suite touches the network or opens windows."""

from __future__ import annotations

import imaplib
import itertools
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest

from agent_relay.config import Config
from agent_relay.gmail import Gmail
from agent_relay.relay import Relay
from agent_relay.replies import MESSAGE_PREFIX, Replier
from agent_relay.sessions import Launcher
from agent_relay.state import State

OWNER = "owner@example.com"
PLAIN = b'("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 5 1 NIL NIL NIL NIL)'


@dataclass
class Mail:
    labels: bytes
    received: float
    headers: bytes
    body: bytes
    structure: bytes = PLAIN


@dataclass
class FakeMailbox:
    """Answers the IMAP commands AgentRelay sends, in imaplib's response format."""

    mails: dict[str, Mail] = field(default_factory=dict)
    commands: list[tuple[str, str]] = field(default_factory=list)
    _uids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def add(
        self,
        subject: str,
        body: str = "do it",
        *,
        sender: str = OWNER,
        sent_label: bool = True,
        extra_headers: str = "",
        age_hours: float = 0,
    ) -> str:
        uid = str(next(self._uids))
        headers = f"From: {sender}\r\nSubject: {subject}\r\nMessage-ID: <m{uid}@example.com>\r\n{extra_headers}\r\n"
        labels = b"(\\Sent)" if sent_label else b"()"
        self.mails[uid] = Mail(labels, time.time() - age_hours * 3600, headers.encode(), body.encode())
        return uid

    # imaplib.IMAP4 surface
    def select(self, *args: Any, **kwargs: Any) -> tuple[str, list[bytes]]:
        return "OK", [str(len(self.mails)).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.commands.append((command, " ".join(args)))
        if command == "SEARCH":
            return "OK", [" ".join(self.mails).encode()]
        target, items = args
        if "HEADER.FIELDS" in items:
            out: list[Any] = []
            for uid in target.split(","):
                m = self.mails[uid]
                date = imaplib.Time2Internaldate(m.received).encode()
                head = b"%s (X-GM-LABELS %s INTERNALDATE %s UID %s BODY[HEADER.FIELDS (...)] {%d}" % (
                    uid.encode(),
                    m.labels,
                    date,
                    uid.encode(),
                    len(m.headers),
                )
                out += [(head, m.headers), b")"]
            return "OK", out
        m = self.mails[target]
        if items == "(BODYSTRUCTURE)":
            return "OK", [b"1 (UID " + target.encode() + b" BODYSTRUCTURE " + m.structure + b")"]
        return "OK", [(b"1 (UID %s BODY[1]<0> {%d}" % (target.encode(), len(m.body)), m.body), b")"]


class FakeSMTP:
    sent: ClassVar[list[EmailMessage]] = []

    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def send_message(self, msg: EmailMessage) -> None:
        FakeSMTP.sent.append(msg)


class FakeLauncher(Launcher):
    """Runs `sleep` as the "terminal" so the session can be ended on demand."""

    def __init__(self, sessions_dir: Path, *, tool_installed: bool = True):
        super().__init__(sessions_dir)
        self.tool_installed = tool_installed
        self.auto_start = True  # the agent takes its prompt as soon as it opens
        self.screen: str | None = None  # what capture() returns
        self._clock = time.time()  # hook files get strictly increasing times
        self.procs: list[subprocess.Popen[bytes]] = []
        self.commands: list[list[str]] = []
        self._n = itertools.count()

    def find_tool(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if self.tool_installed else None

    def new_folder(self, tool: str) -> Path:
        folder = self.sessions_dir / f"{tool}_session_{next(self._n)}"
        folder.mkdir(parents=True)
        return folder

    def open(self, folder: Path, cmd: list[str]) -> subprocess.Popen[bytes] | None:
        self.commands.append(cmd)
        proc = subprocess.Popen(["sleep", "60"])
        self.procs.append(proc)
        (folder / ".agent-relay" / "agent.pid").write_text(str(proc.pid))
        if self.auto_start:
            self._hook_file("started")
        return proc

    def capture(self, folder: Path) -> str | None:
        return self.screen

    def close(self, folder: Path) -> bool:
        self.procs[-1].kill()
        self.procs[-1].wait()
        return True

    def _hook_file(self, name: str, text: str = "") -> None:
        """What an agent hook does: write a file in the session's .agent-relay folder."""
        folder = sorted(self.sessions_dir.iterdir())[-1]
        path = folder / ".agent-relay" / name
        path.write_text(text)
        self._clock += 1
        os.utime(path, (self._clock, self._clock))

    def agent_works(self) -> None:
        self._hook_file("active")

    def agent_finishes(self, message: str, **extra: object) -> None:
        self._hook_file("stop.json", json.dumps({"last_assistant_message": message, **extra}))

    def take_prompt(self) -> None:
        self._hook_file("started")

    def end_session(self) -> None:
        proc = self.procs[-1]
        proc.kill()
        proc.wait()


@dataclass
class Harness:
    relay: Relay
    mailbox: FakeMailbox
    launcher: FakeLauncher
    state: State

    @property
    def gmail(self) -> Gmail:
        return Gmail(self.mailbox)  # type: ignore[arg-type]

    @property
    def replies(self) -> list[str]:
        """First line of each reply, without the "AgentRelay Message: " prefix."""
        return [m.get_content().splitlines()[0].removeprefix(MESSAGE_PREFIX) for m in FakeSMTP.sent]

    @property
    def full_replies(self) -> list[str]:
        """Each reply's whole text, without the prefix or the final newline."""
        return [m.get_content().removeprefix(MESSAGE_PREFIX).rstrip("\n") for m in FakeSMTP.sent]

    def cycle(self) -> None:
        """One pass of the listener loop."""
        self.relay.finish_session_if_closed()
        self.relay.check_started()
        self.relay.check_finished()
        self.relay.check_mail(self.gmail)
        self.relay.start_next()

    def restart(self) -> Harness:
        """A fresh listener process using the same saved state."""
        state = State.load(self.state.path, self.state.limits)
        replier = Replier(OWNER, "pw", state, smtp_factory=FakeSMTP)  # type: ignore[arg-type]
        return Harness(Relay(self.relay.cfg, state, replier, self.launcher), self.mailbox, self.launcher, state)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        gmail_user=OWNER, app_password="pw", allowed_senders=frozenset({OWNER}), sessions_dir=tmp_path / "sessions"
    )


@pytest.fixture
def harness(cfg: Config, tmp_path: Path) -> Any:
    FakeSMTP.sent = []
    state = State.load(tmp_path / "state.json", cfg.limits)
    replier = Replier(OWNER, "pw", state, smtp_factory=FakeSMTP)  # type: ignore[arg-type]
    launcher = FakeLauncher(cfg.sessions_dir)
    h = Harness(Relay(cfg, state, replier, launcher), FakeMailbox(), launcher, state)
    yield h
    for p in launcher.procs:
        p.kill()
        p.wait()
