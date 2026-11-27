"""Configuration: a .env file in the project folder (overridable by real
environment variables), and config.json beside it holding the agent profiles."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

APP = "agent-relay"
PREFIX = "AGENT_RELAY_"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or default).expanduser()


# The project folder (this file is src/agent_relay/config.py). AGENT_RELAY_ENV_FILE
# points elsewhere if the package is installed outside its repository.
PROJECT_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = Path(os.environ.get("AGENT_RELAY_ENV_FILE") or PROJECT_DIR / ".env").expanduser()
PROFILES_PATH = ENV_PATH.parent / "config.json"
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / APP
STATE_PATH = STATE_DIR / "state.json"
LOCK_PATH = STATE_DIR / "listener.lock"

PLACEHOLDER_USER = "you@gmail.com"
PLACEHOLDER_PASSWORD = "xxxx xxxx xxxx xxxx"
# Outside the project on purpose: a session started by email must not sit
# inside the repository that holds .env and the code that checks senders.
DEFAULT_SESSIONS_DIR = Path("~/agent-relay/sessions").expanduser()

# Kept identical to .env.example (a test checks).
TEMPLATE = f"""\
# AgentRelay settings. Copy to .env (`agent-relay init` does this) and fill in.
# .env holds a password: it is git-ignored and should stay private (mode 600).
# Real environment variables with the same names override this file.

# The Gmail account to watch.
AGENT_RELAY_GMAIL_USER={PLACEHOLDER_USER}
# A Google app password (https://myaccount.google.com/apppasswords), not your normal password.
AGENT_RELAY_APP_PASSWORD={PLACEHOLDER_PASSWORD}
# Only genuine mail from these addresses can start a session (comma-separated).
AGENT_RELAY_ALLOWED_SENDERS={PLACEHOLDER_USER}

# Each session gets a new folder in here. Blank: ~/agent-relay/sessions
AGENT_RELAY_SESSIONS_DIR=

# The agents (subject words such as 'claude:') and their flags are in config.json.

# Limits (optional; these are the defaults).
AGENT_RELAY_MAX_AGE_HOURS=24             # ignore task emails received longer ago than this
AGENT_RELAY_QUEUE_MAX=5                  # tasks allowed to wait; more are refused without a reply
AGENT_RELAY_FLOOD_COUNT=10               # more than this many task emails...
AGENT_RELAY_FLOOD_WINDOW_MINUTES=10      # ...within this window pauses the relay until restarted
AGENT_RELAY_REPLY_BUDGET=100             # replies allowed per rolling 24 hours
AGENT_RELAY_DUPLICATE_WINDOW_MINUTES=60  # the same task again within this window is ignored

# Logging (optional).
AGENT_RELAY_LOG_LEVEL=INFO  # DEBUG, INFO, WARNING or ERROR
AGENT_RELAY_LOG_FILE=       # empty means terminal / systemd journal only
"""

KNOWN_KEYS = frozenset(
    PREFIX + k
    for k in (
        "GMAIL_USER", "APP_PASSWORD", "ALLOWED_SENDERS", "SESSIONS_DIR",
        "MAX_AGE_HOURS", "QUEUE_MAX", "FLOOD_COUNT", "FLOOD_WINDOW_MINUTES",
        "REPLY_BUDGET", "DUPLICATE_WINDOW_MINUTES", "LOG_LEVEL", "LOG_FILE",
        "ENV_FILE",
    )
)  # fmt: skip


# Kept identical to config.example.json (a test checks).
PROFILES_TEMPLATE = """\
{
  "claude": {
    "cli": "claude",
    "flags": {"dangerously-skip-permissions": true}
  },
  "codex": {
    "cli": "codex",
    "flags": {"dangerously-bypass-approvals-and-sandbox": true}
  }
}
"""
CLIS = ("claude", "codex")
RESERVED_NAMES = ("agent",)  # "agent:" is for commands
_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_FLAG = re.compile(r"^-{0,2}([A-Za-z0-9][A-Za-z0-9_.-]*)$")


class ConfigError(Exception):
    """The .env file or config.json is missing, unreadable or incomplete."""


@dataclass(frozen=True)
class Profile:
    """A subject word ("claude", "opus"...): which agent it starts, with which flags."""

    name: str
    cli: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Limits:
    max_age: timedelta = timedelta(hours=24)
    queue_max: int = 5
    flood_count: int = 10
    flood_window: timedelta = timedelta(minutes=10)
    reply_budget: int = 100
    reply_window: timedelta = timedelta(hours=24)
    duplicate_window: timedelta = timedelta(minutes=60)
    processed_keep: timedelta = timedelta(days=7)


@dataclass(frozen=True)
class Config:
    gmail_user: str = ""
    app_password: str = field(default="", repr=False)
    allowed_senders: frozenset[str] = frozenset()
    sessions_dir: Path = DEFAULT_SESSIONS_DIR
    # From config.json: subject word -> profile.
    profiles: dict[str, Profile] = field(default_factory=dict)
    limits: Limits = Limits()
    log_level: str = "INFO"
    log_file: Path | None = None


_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str, source: str = ".env") -> dict[str, str]:
    """Parse KEY=value lines. Supports comments, `export`, and single or double
    quotes; an unquoted value ends at ` #`."""
    values: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            raise ConfigError(f"{source} line {n}: expected KEY=value")
        key, value = m.groups()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0:
                raise ConfigError(f"{source} line {n}: missing closing {quote}")
            value = value[1:end]
        else:
            value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def write_template(path: Path | None = None, text: str = TEMPLATE) -> Path:
    """Create the .env file (or config.json) from its template, readable only by the owner."""
    path = path or ENV_PATH
    if path.exists():
        raise ConfigError(f"{path} already exists; not overwriting it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def flags_to_args(flags: object, where: str) -> tuple[str, ...]:
    """{"model": "opus", "yolo": true, "config": ["a=1", "b=2"]} ->
    --model opus --yolo --config a=1 --config b=2. A one-letter key is a short
    flag (-c); false or null leaves the flag out."""
    if not isinstance(flags, dict):
        raise ConfigError(f'{where}: \'flags\' must be an object like {{"model": "opus"}}')
    args: list[str] = []
    for key, value in flags.items():
        m = _FLAG.match(key)
        if not m:
            raise ConfigError(f"{where}: {key!r} isn't a flag name")
        flag = ("-" if len(m.group(1)) == 1 else "--") + m.group(1)
        values = value if isinstance(value, list) else [value]
        for v in values:
            if v is True:
                args.append(flag)
            elif v is False or v is None:
                continue
            elif isinstance(v, (str, int, float)):
                args += [flag, str(v)]
            else:
                raise ConfigError(f"{where}: {key!r} must be text, a number, true/false or a list of them")
    return tuple(args)


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    path = path or PROFILES_PATH
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"No agent profiles at {path}. Run `agent-relay init` to create it.") from None
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if not isinstance(data, dict) or not data:
        raise ConfigError(f'{path}: expected an object of profiles, like {{"claude": {{"cli": "claude"}}}}')
    profiles: dict[str, Profile] = {}
    for name, spec in data.items():
        where = f"{path}: profile {name!r}"
        if not _NAME.match(name) or name in RESERVED_NAMES:
            raise ConfigError(f"{where}: names are lower-case words (letters, digits, - or _), and not 'agent'")
        if not isinstance(spec, dict) or spec.get("cli") not in CLIS:
            raise ConfigError(f'{where}: needs "cli": "claude" or "codex"')
        unknown = sorted(set(spec) - {"cli", "flags"})
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)} (only cli and flags)")
        profiles[name] = Profile(name, spec["cli"], flags_to_args(spec.get("flags", {}), where))
    return profiles


def _positive_int(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(PREFIX + key, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise ConfigError(f"{PREFIX + key} must be a positive whole number, got {raw!r}")
    return int(raw)


def _sessions_dir(value: str) -> Path:
    """Blank means the default. Relative paths are refused: they would depend
    on the folder the listener happens to start in."""
    if not value.strip():
        return DEFAULT_SESSIONS_DIR
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{PREFIX}SESSIONS_DIR must be an absolute path or start with ~ (got {value.strip()!r})")
    return path


def load(path: Path | None = None, *, require_gmail: bool = True, profiles_path: Path | None = None) -> Config:
    """Load settings from the .env file, then real environment variables, and
    the profiles from config.json. With require_gmail=False missing or
    unfinished files are fine (for commands that don't touch Gmail)."""
    path = path or ENV_PATH
    profiles_path = profiles_path or path.parent / PROFILES_PATH.name
    values: dict[str, str] = {}
    if path.exists():
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
        values = parse_env(path.read_text(), str(path))
        unknown = sorted(k for k in values if k.startswith(PREFIX) and k not in KNOWN_KEYS)
        moved = [k for k in unknown if k in (PREFIX + "CLAUDE_ARGS", PREFIX + "CODEX_ARGS")]
        if moved:
            raise ConfigError(
                f"{path}: {', '.join(moved)} moved to {profiles_path.name}: put the flags in each profile's "
                '"flags" and delete the line(s) here.'
            )
        if unknown:
            raise ConfigError(f"{path}: unknown setting(s) {', '.join(unknown)} (typo?)")
    elif require_gmail and not os.environ.get(PREFIX + "GMAIL_USER"):
        raise ConfigError(f"No settings at {path}. Run `agent-relay init` to create it.")
    values.update({k: v for k, v in os.environ.items() if k in KNOWN_KEYS})

    senders = re.split(r"[\s,]+", values.get(PREFIX + "ALLOWED_SENDERS", ""))
    log_file = values.get(PREFIX + "LOG_FILE", "").strip()
    cfg = Config(
        gmail_user=values.get(PREFIX + "GMAIL_USER", "").strip(),
        app_password=values.get(PREFIX + "APP_PASSWORD", ""),
        allowed_senders=frozenset(s.lower() for s in senders if s),
        sessions_dir=_sessions_dir(values.get(PREFIX + "SESSIONS_DIR", "")),
        profiles=load_profiles(profiles_path) if require_gmail or profiles_path.exists() else {},
        limits=Limits(
            max_age=timedelta(hours=_positive_int(values, "MAX_AGE_HOURS", 24)),
            queue_max=_positive_int(values, "QUEUE_MAX", 5),
            flood_count=_positive_int(values, "FLOOD_COUNT", 10),
            flood_window=timedelta(minutes=_positive_int(values, "FLOOD_WINDOW_MINUTES", 10)),
            reply_budget=_positive_int(values, "REPLY_BUDGET", 100),
            duplicate_window=timedelta(minutes=_positive_int(values, "DUPLICATE_WINDOW_MINUTES", 60)),
        ),
        log_level=(values.get(PREFIX + "LOG_LEVEL", "").strip() or "INFO").upper(),
        log_file=Path(log_file).expanduser() if log_file else None,
    )
    if require_gmail:
        _validate(cfg, path)
    return cfg


def _validate(cfg: Config, path: Path) -> None:
    missing = []
    if not cfg.gmail_user or cfg.gmail_user == PLACEHOLDER_USER:
        missing.append(PREFIX + "GMAIL_USER")
    if not cfg.app_password or cfg.app_password == PLACEHOLDER_PASSWORD:
        missing.append(PREFIX + "APP_PASSWORD")
    if not cfg.allowed_senders or PLACEHOLDER_USER in cfg.allowed_senders:
        missing.append(PREFIX + "ALLOWED_SENDERS")
    if missing:
        raise ConfigError(f"Fill in {', '.join(missing)} in {path}.")
