"""The relay's decisions: what starts, what queues, what gets a reply."""

import time
from dataclasses import replace
from pathlib import Path

from conftest import FakeLauncher, FakeSMTP, Harness

from agent_relay.gmail import REPLY_HEADER


def test_task_starts_a_session_and_replies(harness: Harness) -> None:
    harness.launcher.auto_start = False
    harness.mailbox.add("claude: summarise the report", "Please read report.pdf")
    harness.cycle()

    assert harness.relay.running is not None
    folder = harness.relay.running.folder
    prompt = "Task from email: summarise the report\n\nPlease read report.pdf"
    assert (folder / "prompt.md").read_text() == prompt + "\n"
    cmd = harness.launcher.commands[0]
    assert cmd[:2] == ["sh", "-c"]  # records the agent's process ID first
    assert cmd[4] == "/usr/bin/claude"
    assert cmd[5] == "--settings" and "UserPromptSubmit" in cmd[6]
    assert cmd[-1] == prompt
    assert harness.replies == []  # not until the agent takes the prompt
    harness.launcher.take_prompt()
    harness.cycle()
    assert harness.replies == [f"Started claude in {folder}"]
    reply = FakeSMTP.sent[0]
    assert reply["From"] == "AgentRelay <owner@example.com>"
    assert reply.get_content().startswith(f"Started claude in {folder}")
    assert reply["To"] == "owner@example.com"
    assert reply["Subject"] == "Re: claude: summarise the report"
    assert reply["In-Reply-To"] == "<m1@example.com>"
    assert reply[REPLY_HEADER] == "reply"


def test_flags_from_settings_go_before_the_prompt(harness: Harness) -> None:
    harness.relay.cfg = replace(harness.relay.cfg, tool_args={"codex": ("--yolo", "-m", "o3")})
    harness.mailbox.add("codex: fix it", "the failing test")
    harness.cycle()
    assert harness.launcher.commands[0][4:10] == [
        "/usr/bin/codex", "-c", "check_for_update_on_startup=false", "--yolo", "-m", "o3",
    ]  # fmt: skip
    assert harness.launcher.commands[0][-1] == "Task from email: fix it\n\nthe failing test"


def test_codex_gets_a_trusted_prompt_hook(harness: Harness) -> None:
    harness.mailbox.add("codex: fix it", "the failing test")
    harness.cycle()
    cmd = harness.launcher.commands[0]
    assert "--dangerously-bypass-hook-trust" in cmd
    hook = next(a for a in cmd if a.startswith("hooks.UserPromptSubmit="))
    assert hook.endswith('/.agent-relay/started"}]}]')
    assert not any("codex_hooks" in a for a in cmd)  # deprecated; hooks are on by default


def test_session_that_never_takes_its_prompt_is_closed(harness: Harness) -> None:
    harness.launcher.auto_start = False
    harness.mailbox.add("claude: stuck", "on a login screen")
    harness.mailbox.add("codex: next", "waiting")
    harness.cycle()
    r = harness.relay.running
    assert r is not None
    assert [x[:7] for x in harness.replies] == ["Queued:"]  # for the codex task; no "Started" yet
    r.started -= 179
    harness.cycle()
    assert r.phase == "starting"  # not yet

    r.started -= 2
    harness.cycle()
    assert r.phase == "abandoned"
    assert harness.replies[-1].startswith("Didn't start: claude hadn't taken the prompt after 3 min")
    assert r.task.hash not in harness.state.recent  # can be sent again straight away
    r.proc.wait(5)
    harness.launcher.auto_start = True
    harness.cycle()  # the window has closed: no "Session closed" reply; the next task starts
    assert not any(x.startswith("Session closed") for x in harness.replies)
    assert harness.relay.running is not None and harness.relay.running.task.tool == "codex"
    assert harness.replies[-1].startswith("Started codex")


def test_window_closed_before_the_prompt_was_taken(harness: Harness) -> None:
    harness.launcher.auto_start = False
    harness.mailbox.add("claude: x", "y")
    harness.cycle()
    harness.launcher.end_session()
    harness.cycle()
    assert harness.replies == ["Closed before claude took the prompt; nothing ran."]


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

    closed = next(m for m in FakeSMTP.sent if "Session closed" in m.get_content())
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


def test_stuck_session_reply_includes_its_screen(harness: Harness) -> None:
    harness.launcher.auto_start = False
    harness.launcher.screen = "\n".join(f"line {i}" for i in range(100)) + "\nPlease log in: /login"
    harness.mailbox.add("claude: x", "y")
    harness.cycle()
    assert harness.relay.running is not None
    harness.relay.running.started -= 181
    harness.cycle()
    reply = harness.full_replies[-1]
    assert reply.startswith("Didn't start")
    assert reply.endswith("line 99\nPlease log in: /login")
    assert "line 40\n" not in reply  # only the last 60 lines


def test_finished_agent_is_closed_with_its_last_message(harness: Harness) -> None:
    harness.mailbox.add("claude: write it", "please")
    harness.mailbox.add("codex: next one", "please")
    harness.cycle()
    r = harness.relay.running
    assert r is not None
    (r.folder / "notes.md").write_text("x")
    harness.launcher.agent_works()
    harness.cycle()
    assert r.phase == "running"  # working, no finished hook yet

    harness.launcher.agent_finishes("All done: wrote notes.md")
    harness.cycle()
    assert r.phase == "finished" and r.proc.poll() is not None
    finished = next(x for x in harness.full_replies if x.startswith("Finished"))
    assert "Last message from claude:\n\nAll done: wrote notes.md" in finished
    assert "notes.md" in finished.split("Files in")[1]
    harness.cycle()  # the window has closed: the next task starts, with no "Session closed" reply
    assert not any(x.startswith("Session closed") for x in harness.replies)
    assert harness.relay.running is not None and harness.relay.running.task.tool == "codex"


def test_claude_background_tasks_come_from_its_hook(harness: Harness) -> None:
    harness.mailbox.add("claude: x", "y")
    harness.cycle()
    r = harness.relay.running
    assert r is not None
    harness.launcher.agent_finishes("Waiting on tests", background_tasks=[{"id": "b1"}])
    harness.cycle()
    assert r.phase == "running"
    harness.launcher.agent_finishes("Tests passed", background_tasks=[])
    harness.cycle()
    assert r.phase == "finished"


def test_closing_the_window_mid_task_reports_the_last_message(harness: Harness) -> None:
    harness.mailbox.add("claude: x", "y")
    harness.cycle()
    harness.launcher.agent_finishes("Half way", background_tasks=[{"id": "b1"}])
    harness.cycle()
    harness.launcher.end_session()
    harness.cycle()
    assert harness.full_replies[-1].startswith("Session closed after 0 sec.\n\nLast message from claude:\n\nHalf way")


def test_durations_read_naturally() -> None:
    from agent_relay.relay import duration

    assert [duration(s) for s in (0, 10.7, 59, 60, 754, 3600, 7500)] == [
        "0 sec", "10 sec", "59 sec", "1 min", "12 min", "1 h", "2 h 5 min",
    ]  # fmt: skip


def test_empty_session_folders_are_removed_when_closed(harness: Harness) -> None:
    harness.mailbox.add("claude: answer a question", "what is 2+2?")
    harness.cycle()
    r = harness.relay.running
    assert r is not None
    harness.launcher.agent_finishes("4")
    harness.cycle()
    assert harness.full_replies[-1].endswith("No files were created, so the session folder has been removed.")
    harness.cycle()
    assert not r.folder.exists()


def test_session_folders_with_files_are_kept(harness: Harness) -> None:
    harness.mailbox.add("claude: write it", "please")
    harness.cycle()
    r = harness.relay.running
    assert r is not None
    (r.folder / ".git").mkdir()  # even a hidden folder the agent made counts
    harness.launcher.agent_finishes("done")
    harness.cycle()
    harness.cycle()
    assert r.folder.exists()


def test_folder_of_a_session_that_never_started_is_removed(harness: Harness) -> None:
    harness.launcher.auto_start = False
    harness.mailbox.add("claude: x", "y")
    harness.cycle()
    r = harness.relay.running
    assert r is not None
    r.started -= 181
    harness.cycle()
    harness.cycle()
    assert not r.folder.exists()


def test_agent_help_and_unknown_commands(harness: Harness) -> None:
    harness.mailbox.add("agent: help", "")
    harness.mailbox.add("agent: dance", "")
    harness.cycle()
    assert harness.full_replies[0].startswith("Commands (send as the email subject")
    assert harness.full_replies[1].startswith("Unknown command 'dance'.\n\nCommands")
    assert harness.relay.running is None and not harness.state.queue  # commands never queue


def test_agent_commands_ignore_case_and_spacing(harness: Harness) -> None:
    for subject in ("agent:help", "AGENT:HELP", "Agent :  Help"):
        harness.mailbox.add(subject, "")
    harness.cycle()
    assert len(harness.full_replies) == 3
    assert all(r.startswith("Commands (send as the email subject") for r in harness.full_replies)


def test_agent_status(harness: Harness) -> None:
    harness.mailbox.add("claude: first task", "x")
    harness.mailbox.add("codex: second task", "y")
    harness.cycle()
    harness.mailbox.add("agent: status", "")
    harness.cycle()
    status = harness.full_replies[-1]
    assert status.startswith("Running: claude: first task (0 sec)")
    assert "Queue: 1 of 5\n  1. codex: second task" in status
    assert status.endswith(f"Replies: {len(harness.replies)} of 30 used in the last 24 hours")


def test_agent_stop_closes_the_session_and_starts_the_next(harness: Harness) -> None:
    harness.mailbox.add("agent: stop", "")
    harness.cycle()
    assert harness.full_replies[-1] == "Nothing is running."

    harness.mailbox.add("claude: going wrong", "x")
    harness.mailbox.add("codex: next", "y")
    harness.cycle()
    r = harness.relay.running
    assert r is not None
    harness.launcher.agent_finishes("Half way through")
    harness.launcher.agent_works()  # still working
    harness.mailbox.add("agent: stop", "")
    harness.cycle()
    stopped = harness.full_replies[-1]
    assert stopped.startswith(
        "Stopped claude after 0 sec: going wrong\n\nLast message from claude:\n\nHalf way through"
    )
    assert r.phase == "stopped" and r.proc.poll() is not None
    harness.cycle()  # closed: no extra reply; the next task starts
    assert not any(x.startswith(("Session closed", "Finished")) for x in harness.replies)
    assert harness.relay.running is not None and harness.relay.running.task.tool == "codex"


def test_agent_screen(harness: Harness) -> None:
    harness.mailbox.add("agent: screen", "")
    harness.cycle()
    assert harness.full_replies[-1] == "Nothing is running."
    harness.mailbox.add("claude: long job", "x")
    harness.cycle()
    harness.launcher.screen = "Building... 42%"
    harness.mailbox.add("agent: screen", "")
    harness.cycle()
    assert harness.full_replies[-1] == "claude: long job\n\nBuilding... 42%"


def test_commands_count_towards_the_flood_breaker(harness: Harness) -> None:
    for i in range(11):
        harness.mailbox.add(f"agent: status {i}", "")
    harness.cycle()
    assert harness.relay.paused
