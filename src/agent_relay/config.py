"""Configuration: a .env file in the project folder, overridable by real
environment variables."""

from __future__ import annotations

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
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / APP
STATE_PATH = STATE_DIR / "state.json"
LOCK_PATH = STATE_DIR / "listener.lock"

PLACEHOLDER_USER = "you@gmail.com"
PLACEHOLDER_PASSWORD = "xxxx xxxx xxxx xxxx"
DEFAULT_SESSIONS_DIR = "~/agent-relay/sessions"

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

# Each session gets a new folder in here.
AGENT_RELAY_SESSIONS_DIR={DEFAULT_SESSIONS_DIR}

# Limits (optional; these are the defaults).
AGENT_RELAY_MAX_AGE_HOURS=24             # ignore task emails received longer ago than this
AGENT_RELAY_QUEUE_MAX=5                  # tasks allowed to wait; more are refused without a reply
AGENT_RELAY_FLOOD_COUNT=10               # more than this many task emails...
AGENT_RELAY_FLOOD_WINDOW_MINUTES=10      # ...within this window pauses the relay until restarted
AGENT_RELAY_REPLY_BUDGET=30              # replies allowed per rolling 24 hours
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


class ConfigError(Exception):
    """The .env file is missing, unreadable or incomplete."""


@dataclass(frozen=True)
class Limits:
    max_age: timedelta = timedelta(hours=24)
    queue_max: int = 5
    flood_count: int = 10
    flood_window: timedelta = timedelta(minutes=10)
    reply_budget: int = 30
    reply_window: timedelta = timedelta(hours=24)
    duplicate_window: timedelta = timedelta(minutes=60)
    processed_keep: timedelta = timedelta(days=7)


@dataclass(frozen=True)
class Config:
    gmail_user: str = ""
    app_password: str = field(default="", repr=False)
    allowed_senders: frozenset[str] = frozenset()
    sessions_dir: Path = field(default_factory=lambda: Path(DEFAULT_SESSIONS_DIR).expanduser())
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


def write_template(path: Path | None = None) -> Path:
    """Create the .env file from the template, readable only by the owner."""
    path = path or ENV_PATH
    if path.exists():
        raise ConfigError(f"{path} already exists; not overwriting it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(TEMPLATE)
    return path


def _positive_int(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(PREFIX + key, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise ConfigError(f"{PREFIX + key} must be a positive whole number, got {raw!r}")
    return int(raw)


def load(path: Path | None = None, *, require_gmail: bool = True) -> Config:
    """Load settings from the .env file, then real environment variables.
    With require_gmail=False a missing or unfinished file is fine (for commands
    that don't touch Gmail)."""
    path = path or ENV_PATH
    values: dict[str, str] = {}
    if path.exists():
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
        values = parse_env(path.read_text(), str(path))
        unknown = sorted(k for k in values if k.startswith(PREFIX) and k not in KNOWN_KEYS)
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
        sessions_dir=Path(values.get(PREFIX + "SESSIONS_DIR", "").strip() or DEFAULT_SESSIONS_DIR).expanduser(),
        limits=Limits(
            max_age=timedelta(hours=_positive_int(values, "MAX_AGE_HOURS", 24)),
            queue_max=_positive_int(values, "QUEUE_MAX", 5),
            flood_count=_positive_int(values, "FLOOD_COUNT", 10),
            flood_window=timedelta(minutes=_positive_int(values, "FLOOD_WINDOW_MINUTES", 10)),
            reply_budget=_positive_int(values, "REPLY_BUDGET", 30),
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
