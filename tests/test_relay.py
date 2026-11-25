"""The relay's decisions: what starts, what queues, what gets a reply."""

import time
from pathlib import Path

from conftest import FakeLauncher, FakeSMTP, Harness

from agent_relay.gmail import REPLY_HEADER


def test_task_starts_a_session_and_replies(harness: Harness) -> None:
    harness.mailbox.add("claude: summarise the report", "Please read report.pdf")
    harness.cycle()

    assert harness.relay.running is not None
    folder = harness.relay.running.folder
    prompt = "Task from email: summarise the report\n\nPlease read report.pdf"
    assert (folder / "prompt.md").read_text() == prompt + "\n"
    assert harness.launcher.commands == [["/usr/bin/claude", prompt]]
    assert harness.replies == [f"Started claude in {folder}"]
    reply = FakeSMTP.sent[0]
    assert reply["To"] == "owner@example.com"
    assert reply["Subject"] == "Re: claude: summarise the report"
    assert reply["In-Reply-To"] == "<m1@example.com>"
    assert reply[REPLY_HEADER] == "reply"


def test_mail_while_busy_is_queued_with_position(harness: Harness) -> None:
    harness.mailbox.add("claude: first")
    harness.cycle()
    harness.mailbox.add("codex: second")
    harness.mailbox.add("claude: third")
    harness.cycle()

    assert [t.title for t in harness.state.queue] == ["second", "third"]
    assert harness.replies[1:] == [
        "Queued: position 1 of at most 5. It will start when the sessions ahead of it have closed.",
        "Queued: position 2 of at most 5. It will start when the sessions ahead of it have closed.",
    ]


def test_closing_a_session_replies_with_files_and_starts_the_next(harness: Harness) -> None:
    harness.mailbox.add("claude: first")
    harness.mailbox.add("codex: second")
    harness.cycle()
    first = harness.relay.running
    assert first is not None
    (first.folder / "report.md").write_text("x")
    (first.folder / ".claude").mkdir()
    (first.folder / ".claude" / "settings.json").write_text("{}")

    harness.launcher.end_session()
    harness.cycle()

    closed = next(m for m in FakeSMTP.sent if m.get_content().startswith("Session closed"))
    assert closed["Subject"] == "Re: claude: first"
    assert "  report.md" in closed.get_content()
    assert "settings.json" not in closed.get_content()
    assert "prompt.md" not in closed.get_content()
    assert harness.relay.running is not None
    assert harness.relay.running.task.tool == "codex"


def test_empty_task_gets_a_reply_and_no_session(harness: Harness) -> None:
    harness.mailbox.add("claude:", "")
    harness.cycle()
    assert harness.relay.running is None
    assert harness.replies == [
        "This email has no task in it. Put the task after 'claude:' in the subject, or in the body, and send it again."
    ]


def test_duplicate_task_is_ignored_silently(harness: Harness) -> None:
    harness.mailbox.add("claude: first")
    harness.cycle()
    harness.mailbox.add("codex: same", "body")
    harness.mailbox.add("codex: same", "body")
    harness.cycle()
    assert len(harness.state.queue) == 1
    assert len(harness.replies) == 2  # started + one queued


def test_untrusted_and_forged_mail_never_get_replies(harness: Harness) -> None:
    harness.mailbox.add("claude: forged", sent_label=False)  # From is right but unverified
    harness.mailbox.add("claude: stranger", sender="stranger@example.org")
    harness.cycle()
    assert harness.relay.running is None
    assert FakeSMTP.sent == []


def test_own_replies_and_non_matching_subjects_are_ignored(harness: Harness) -> None:
    harness.mailbox.add("Re: claude: first", extra_headers=f"{REPLY_HEADER}: reply\r\n")
    harness.mailbox.add("claude: marked as ours", extra_headers=f"{REPLY_HEADER}: reply\r\n")
    harness.mailbox.add("thoughts about claude")
    harness.cycle()
    assert harness.relay.running is None
    assert FakeSMTP.sent == []


def test_mail_older_than_max_age_is_ignored(harness: Harness) -> None:
    harness.mailbox.add("claude: stale", age_hours=24.1)
    harness.mailbox.add("claude: fresh", age_hours=23.9)
    harness.cycle()
    assert harness.relay.running is not None
    assert harness.relay.running.task.title == "fresh"


def test_queue_limit_refuses_without_reply(harness: Harness) -> None:
    harness.mailbox.add("claude: running")
    harness.cycle()
    for i in range(6):
        harness.mailbox.add(f"claude: task {i}")
    harness.cycle()
    assert [t.title for t in harness.state.queue] == [f"task {i}" for i in range(5)]
    assert len(harness.replies) == 6  # started + 5 queued; the 6th task got nothing


def test_flood_pauses_clears_queue_and_alerts_once(harness: Harness) -> None:
    for i in range(11):
        harness.mailbox.add(f"claude: flood {i}", f"b{i}")
    harness.cycle()
    assert harness.relay.paused
    assert harness.state.queue == []
    assert harness.relay.running is None
    assert harness.replies[-1].startswith("Flood detected: more than 10 task emails")

    sent_before = len(FakeSMTP.sent)
    harness.mailbox.add("claude: after the flood")
    harness.cycle()
    assert len(FakeSMTP.sent) == sent_before
    assert harness.relay.running is None


def test_reply_budget_ends_with_a_notice_then_silence(harness: Harness) -> None:
    harness.state.replies = [time.time()] * 28
    for subject in ("claude:", "codex:", "claude: "):
        harness.mailbox.add(subject, "")
    harness.cycle()
    assert harness.replies == [
        "This email has no task in it. Put the task after 'claude:' in the subject, or in the body, and send it again.",
        "Reply limit reached: 30 replies in the last 24 hours.",
    ]


def test_handled_mail_never_runs_twice_across_restarts(harness: Harness) -> None:
    harness.mailbox.add("claude: once only")
    harness.cycle()
    harness.launcher.end_session()
    harness.cycle()

    restarted = harness.restart()
    restarted.cycle()
    assert restarted.relay.running is None
    assert len(harness.launcher.procs) == 1


def test_queue_survives_a_restart(harness: Harness) -> None:
    harness.mailbox.add("claude: first")
    harness.mailbox.add("codex: waiting")
    harness.cycle()
    restarted = harness.restart()
    assert [t.title for t in restarted.state.queue] == ["waiting"]
    restarted.cycle()
    assert restarted.relay.running is not None
    assert restarted.relay.running.task.title == "waiting"


def test_missing_tool_replies_couldnt_start(harness: Harness, tmp_path: Path) -> None:
    harness.relay.launcher = FakeLauncher(tmp_path / "s2", tool_installed=False)
    harness.mailbox.add("codex: needs codex")
    harness.cycle()
    assert harness.relay.running is None
    assert harness.replies == ["Couldn't start: codex isn't installed or isn't on PATH."]


def test_only_headers_and_the_text_part_are_downloaded(harness: Harness) -> None:
    harness.mailbox.add("claude: wanted")
    harness.mailbox.add("about claude")  # matches the server search, not the subject rule
    harness.cycle()
    fetches = [args for cmd, args in harness.mailbox.commands if cmd == "FETCH"]
    assert len(fetches) == 3
    assert fetches[0].startswith("1,2 (X-GM-LABELS INTERNALDATE BODY.PEEK[HEADER.FIELDS")
    assert fetches[1] == "1 (BODYSTRUCTURE)"
    assert fetches[2] == "1 (BODY.PEEK[1]<0.100000>)"


def test_dry_run_starts_sends_and_saves_nothing(harness: Harness) -> None:
    harness.relay.dry_run = harness.state.dry_run = harness.relay.replier.dry_run = True
    harness.mailbox.add("claude: try it")
    harness.cycle()
    assert harness.relay.running is None
    assert FakeSMTP.sent == []
    assert not harness.state.path.exists()
