from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agent_relay import cli, config
from agent_relay.config import KNOWN_KEYS, PROJECT_DIR, TEMPLATE, ConfigError, load, parse_env, write_template

FILLED = """\
AGENT_RELAY_GMAIL_USER=owner@example.com
AGENT_RELAY_APP_PASSWORD=abcd efgh ijkl mnop
AGENT_RELAY_ALLOWED_SENDERS=Owner@Example.com, other@example.com
AGENT_RELAY_SESSIONS_DIR=/tmp/sessions-test
AGENT_RELAY_QUEUE_MAX=3   # fewer
AGENT_RELAY_FLOOD_WINDOW_MINUTES=5
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "listener.lock")
    for key in KNOWN_KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_env_example_matches_the_template() -> None:
    assert (PROJECT_DIR / ".env.example").read_text() == TEMPLATE


def test_parse_env() -> None:
    text = """
# comment
export A=1
B = two words   # trailing comment
C="quoted # not a comment"
D='single'
E=
F=   # only a comment
"""
    assert parse_env(text) == {
        "A": "1",
        "B": "two words",
        "C": "quoted # not a comment",
        "D": "single",
        "E": "",
        "F": "",
    }


@pytest.mark.parametrize("line", ["no equals sign", 'A="unterminated'])
def test_parse_env_errors_name_the_line(line: str) -> None:
    with pytest.raises(ConfigError, match="line 2"):
        parse_env(f"OK=1\n{line}\n")


def test_template_is_private_and_must_be_filled_in(paths: Path) -> None:
    path = write_template(paths / ".env")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ConfigError, match=r"GMAIL_USER.*APP_PASSWORD.*ALLOWED_SENDERS"):
        load(path)
    with pytest.raises(ConfigError, match="already exists"):
        write_template(path)


def test_template_defaults_load(paths: Path) -> None:
    cfg = load(write_template(paths / ".env"), require_gmail=False)
    assert cfg.sessions_dir == PROJECT_DIR / "sessions"
    assert cfg.limits == config.Limits()
    assert cfg.log_file is None
    assert cfg.log_level == "INFO"


def test_filled_env_loads(paths: Path) -> None:
    path = paths / ".env"
    path.write_text(FILLED)
    cfg = load(path)
    assert cfg.gmail_user == "owner@example.com"
    assert cfg.allowed_senders == {"owner@example.com", "other@example.com"}
    assert cfg.sessions_dir == Path("/tmp/sessions-test")
    assert cfg.limits.queue_max == 3
    assert cfg.limits.flood_window == timedelta(minutes=5)
    assert cfg.limits.reply_budget == 30  # default
    assert "abcd" not in repr(cfg)  # the password never shows up in logs


def test_environment_variables_override_the_file(paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = paths / ".env"
    path.write_text(FILLED)
    monkeypatch.setenv("AGENT_RELAY_APP_PASSWORD", "from-env")
    assert load(path).app_password == "from-env"


def test_loose_permissions_are_tightened(paths: Path) -> None:
    path = paths / ".env"
    path.write_text(FILLED)
    path.chmod(0o644)
    load(path)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (FILLED.replace("QUEUE_MAX=3", "QUEUE_MAX=0"), "AGENT_RELAY_QUEUE_MAX"),
        (FILLED.replace("QUEUE_MAX=3", "QUEUE_MAX=lots"), "AGENT_RELAY_QUEUE_MAX"),
        (FILLED + "AGENT_RELAY_QUEUE_MAXX=4\n", "unknown setting.*QUEUE_MAXX"),
    ],
)
def test_bad_settings_are_explained(paths: Path, text: str, message: str) -> None:
    path = paths / ".env"
    path.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load(path)


def test_cli_init_then_run_refuses_unfinished_settings(paths: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init"]) == 0
    assert (paths / ".env").exists()
    assert cli.main(["run"]) == 2
    assert "Fill in AGENT_RELAY_GMAIL_USER" in capsys.readouterr().err


def test_cli_status_without_listener(paths: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Listener:  not running" in out
    assert "Queue:     0 of 5" in out
    assert "missing: run agent-relay init" in out


def test_cli_without_a_command_shows_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 1
    out = capsys.readouterr().out
    for command in ("init", "run", "check", "status", "new", "install-service"):
        assert command in out


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("agent-relay ")
