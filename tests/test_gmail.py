"""The IMAP layer: IDLE against a local fake server, dates, search terms."""

from __future__ import annotations

import imaplib
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import date
from typing import ClassVar

import pytest

from agent_relay.gmail import Gmail, imap_date, sender_terms

Step = list[tuple[float, bytes]]  # (delay, line) pairs sent while idling


def serve(sock: socket.socket, steps: list[Step | None]) -> None:
    """A minimal IMAP server: greeting, CAPABILITY, then one IDLE per step.
    A step of None means: accept IDLE, then never answer DONE (dead connection)."""
    conn, _ = sock.accept()
    f = conn.makefile("rwb", buffering=0)
    f.write(b"* OK fake ready\r\n")
    tag = f.readline().split(b" ", 1)[0]
    f.write(b"* CAPABILITY IMAP4rev1 IDLE\r\n" + tag + b" OK done\r\n")
    for step in steps:
        tag = f.readline().split(b" ", 1)[0]
        f.write(b"+ idling\r\n")
        if step is None:
            time.sleep(5)
            return
        for delay, line in step:
            time.sleep(delay)
            f.write(line)
        assert f.readline().strip() == b"DONE"
        f.write(b"* 9 EXISTS\r\n" + tag + b" OK IDLE terminated\r\n")  # an extra line to drain
    time.sleep(0.5)
    conn.close()


@pytest.fixture
def fake_server() -> Iterator[tuple[socket.socket, int]]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield sock, sock.getsockname()[1]
    sock.close()


def connect(fake_server: tuple[socket.socket, int], steps: list[Step | None], timeout: float = 5) -> Gmail:
    sock, port = fake_server
    threading.Thread(target=serve, args=(sock, steps), daemon=True).start()
    return Gmail(imaplib.IMAP4("127.0.0.1", port, timeout=timeout))


def timed(fn: object, *args: object) -> tuple[object, float]:
    start = time.monotonic()
    result = fn(*args)  # type: ignore[operator]
    return result, time.monotonic() - start


def test_idle_wakes_on_new_mail(fake_server: tuple[socket.socket, int]) -> None:
    gmail = connect(fake_server, [[(0.3, b"* 5 EXISTS\r\n")]])
    changed, took = timed(gmail.idle_wait, 10)
    assert changed is True
    assert took < 2


def test_idle_ignores_keepalives_and_times_out(fake_server: tuple[socket.socket, int]) -> None:
    gmail = connect(fake_server, [[(0.2, b"* OK still here\r\n")], []])
    assert timed(gmail.idle_wait, 1)[0] is False
    changed, took = timed(gmail.idle_wait, 0.5)
    assert changed is False
    assert 0.4 < took < 2


def test_idle_wakes_when_the_session_ends(fake_server: tuple[socket.socket, int]) -> None:
    gmail = connect(fake_server, [[]])
    proc = subprocess.Popen(["sleep", "0.5"])
    pidfd = os.pidfd_open(proc.pid)
    try:
        changed, took = timed(gmail.idle_wait, 30, [pidfd])
    finally:
        os.close(pidfd)
        proc.wait()
    assert changed is False
    assert took < 3


def test_dead_connection_times_out_instead_of_hanging(fake_server: tuple[socket.socket, int]) -> None:
    gmail = connect(fake_server, [None], timeout=1)
    with pytest.raises(TimeoutError):
        gmail.idle_wait(0.3)


def test_imap_date_is_english_whatever_the_locale() -> None:
    assert imap_date(date(2026, 11, 3)) == "03-Nov-2026"


def test_sender_terms() -> None:
    assert sender_terms({"a@x"}) == 'FROM "a@x"'
    assert sender_terms({"a@x", "b@x", "c@x"}) == 'OR OR FROM "a@x" FROM "b@x" FROM "c@x"'


class RecordingIMAP:
    calls: ClassVar[list[str]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        RecordingIMAP.calls = []

    def login(self, user: str, password: str) -> None:
        pass

    def logout(self) -> None:
        RecordingIMAP.calls.append("logout")

    def shutdown(self) -> None:
        RecordingIMAP.calls.append("shutdown")


def test_stopping_mid_idle_closes_at_once_instead_of_logging_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """In IDLE, Gmail never answers LOGOUT, so a polite logout hung `service stop`
    until systemd killed the listener 90 seconds later."""
    monkeypatch.setattr(imaplib, "IMAP4_SSL", RecordingIMAP)
    with pytest.raises(KeyboardInterrupt), Gmail.connect("u", "p"):
        raise KeyboardInterrupt
    assert RecordingIMAP.calls == ["shutdown"]
    with Gmail.connect("u", "p"):
        pass
    assert RecordingIMAP.calls == ["logout"]  # a normal exit still logs out
