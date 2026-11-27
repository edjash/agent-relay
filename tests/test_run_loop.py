"""Reconnect behaviour: backoff, Gmail refusals, login failures."""

from __future__ import annotations

import imaplib
import logging
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from conftest import Harness

from agent_relay.gmail import Gmail
from agent_relay.relay import EXIT_NO_RESTART, EXIT_OK, run


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def scripted_connect(events: list[object], clock: Clock, harness: Harness):  # type: ignore[no-untyped-def]
    """Each connection attempt takes the next event: an exception to raise at
    login, or a number of seconds the connection stays up before dropping."""
    queue = iter(events)

    @contextmanager
    def connect(user: str, password: str) -> Iterator[Gmail]:
        event = next(queue)
        if isinstance(event, BaseException):
            raise event

        class Dropping(Gmail):
            def search_tasks(self, *args: object) -> list[str]:
                clock.now += float(event)  # type: ignore[arg-type]
                raise OSError(f"dropped after {event}s")

        yield Dropping(harness.mailbox)  # type: ignore[arg-type]

    return connect


def test_backoff_doubles_caps_and_resets(harness: Harness) -> None:
    clock = Clock()
    events: list[object] = [OSError("Network is unreachable")] * 8
    events += [imaplib.IMAP4.error("[ALERT] Too many simultaneous connections. (Failure)")]
    events += [11 * 60, 5]  # a healthy 11-minute connection, then a quick drop
    events += [imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")]
    code = run(harness.relay, connect=scripted_connect(events, clock, harness), sleep=clock.sleep, clock=clock)

    assert code == EXIT_NO_RESTART
    assert clock.sleeps == [30, 60, 120, 240, 480, 960, 1800, 1800, 1800, 30, 60]


def test_once_returns_after_one_pass(harness: Harness) -> None:
    @contextmanager
    def connect(user: str, password: str) -> Iterator[Gmail]:
        yield Gmail(harness.mailbox)  # type: ignore[arg-type]

    harness.mailbox.add("claude: hello")
    assert run(harness.relay, once=True, connect=connect) == EXIT_OK
    assert harness.relay.running is not None
    assert harness.state.listener == {}  # cleared on exit for `status`


def test_a_reconnect_is_logged(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    events: list[object] = [OSError("no network")] * 3 + [5]
    events += [imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")]
    caplog.set_level(logging.INFO, "agent_relay")
    run(harness.relay, connect=scripted_connect(events, clock, harness), sleep=clock.sleep, clock=clock)
    assert "Reconnected after 3 failed attempts" in caplog.messages


def test_sigterm_stops_cleanly_and_says_so(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    closed = []

    @contextmanager
    def connect(user: str, password: str) -> Iterator[Gmail]:
        class Stopped(Gmail):
            def search_tasks(self, *args: object) -> list[str]:
                os.kill(os.getpid(), signal.SIGTERM)  # what `systemctl stop` sends
                return []

        try:
            yield Stopped(harness.mailbox)  # type: ignore[arg-type]
        finally:
            closed.append(True)

    before = signal.getsignal(signal.SIGTERM)
    caplog.set_level(logging.INFO, "agent_relay")
    assert run(harness.relay, connect=connect) == EXIT_OK
    assert closed == [True]  # the IMAP connection was closed
    assert "Stopped (asked to by the system)" in caplog.messages
    assert harness.state.listener == {}
    assert signal.getsignal(signal.SIGTERM) == before
