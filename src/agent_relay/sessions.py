"""Session folders and terminal windows running Claude Code or Codex."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PROMPT_FILE = "prompt.md"
# Always passed, whatever config.json says: nobody is there to answer these prompts.
FIXED_ARGS: dict[str, tuple[str, ...]] = {
    "codex": ("-c", "check_for_update_on_startup=false"),
}


# AgentRelay's own files inside a session folder (hidden from the file list).
RELAY_DIR = ".agent-relay"
STARTED_MARKER = "started"  # touched by the agent's prompt hook (each prompt it takes)
ACTIVE_MARKER = "active"  # touched by the agent's tool hook (each tool it runs)
STOP_FILE = "stop.json"  # written by the agent's "finished turn" hook: its input, incl. the last message
PID_FILE = "agent.pid"  # the agent's process ID
TMUX_CONF = "tmux.conf"

# Sessions run inside tmux (when installed) on AgentRelay's own server, so the
# screen can be read and the session closed cleanly. Closing the window ends it.
TMUX_SOCKET = "agent-relay"
TMUX_SETTINGS = """\
set -g status off
set -g destroy-unattached on
set -g mouse on
set -g history-limit 50000
set -s escape-time 10
set -s extended-keys on
"""


def build_command(exe: str, tool: str, extra: tuple[str, ...] = (), prompt: str | None = None) -> list[str]:
    """exe, the fixed flags, the flags from .env, then the prompt."""
    return [exe, *FIXED_ARGS.get(tool, ()), *extra, *([prompt] if prompt is not None else [])]


def hook_args(tool: str, relay_dir: Path) -> list[str]:
    """Flags giving the agent hooks that report to AgentRelay through files in relay_dir:
    - UserPromptSubmit touches STARTED_MARKER: it took a prompt (a login screen,
      update notice or other dialog keeps this from happening);
    - PreToolUse touches ACTIVE_MARKER: it is working;
    - Stop saves its input to STOP_FILE: it finished its turn."""

    def q(name: str) -> str:
        return shlex.quote(str(relay_dir / name))

    hooks = {
        "UserPromptSubmit": f"touch {q(STARTED_MARKER)}",
        "PreToolUse": f"touch {q(ACTIVE_MARKER)}",
        "Stop": f"cat > {q(STOP_FILE)}",
    }
    if tool == "claude":
        settings = {
            "hooks": {event: [{"hooks": [{"type": "command", "command": cmd}]}] for event, cmd in hooks.items()}
        }
        return ["--settings", json.dumps(settings)]
    # Codex: TOML values through -c; JSON strings are valid TOML strings.
    args = ["--dangerously-bypass-hook-trust"]  # otherwise Codex asks before running the hooks
    for event, cmd in hooks.items():
        args += ["-c", f'hooks.{event}=[{{hooks = [{{type = "command", command = {json.dumps(cmd)}}}]}}]']
    return args


def in_tmux(cmd: list[str], name: str, folder: Path, conf: Path) -> list[str]:
    return ["tmux", "-L", TMUX_SOCKET, "-f", str(conf), "new-session", "-s", name, "-c", str(folder), "--", *cmd]


def tmux_capture(name: str) -> str | None:
    """The text currently on the session's screen."""
    try:
        out = subprocess.run(
            ["tmux", "-L", TMUX_SOCKET, "capture-pane", "-p", "-J", "-t", f"={name}:"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return out.rstrip() or None


def tmux_send(name: str, steps: list[tuple[bool, str]]) -> bool:
    """Type into the session. Each step is (literal, text): literal text is
    typed as-is; otherwise text is a tmux key name such as Enter or C-c."""
    try:
        for literal, text in steps:
            if text.endswith(";"):  # tmux reads a trailing ";" as "next command"
                text = text[:-1] + "\\;"
            subprocess.run(
                ["tmux", "-L", TMUX_SOCKET, "send-keys", "-t", f"={name}:", *(["-l"] if literal else []), "--", text],
                capture_output=True, timeout=10, check=True,
            )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def tmux_close(name: str) -> bool:
    try:
        subprocess.run(
            ["tmux", "-L", TMUX_SOCKET, "kill-session", "-t", f"={name}"], capture_output=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def with_pid_file(cmd: list[str], pid_file: Path) -> list[str]:
    """Run cmd through sh so its process ID is written to pid_file first
    (exec keeps the same process)."""
    return ["sh", "-c", 'echo $$ > "$0" && exec "$@"', str(pid_file), *cmd]


def find_tool(name: str) -> str | None:
    for candidate in (shutil.which(name), str(Path.home() / ".local/bin" / name)):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def new_session_folder(base: Path, tool: str) -> Path:
    """Create base/<tool>_session_DDMMYYYY_HHMM (with _2, _3... if taken)."""
    name = datetime.now().strftime(f"{tool}_session_%d%m%Y_%H%M")
    folder = base / name
    n = 2
    while folder.exists():
        folder = base / f"{name}_{n}"
        n += 1
    folder.mkdir(parents=True)
    return folder


def open_in_new_terminal(folder: Path, cmd: list[str]) -> subprocess.Popen[bytes] | None:
    """Open a terminal window running cmd in folder. The returned process lives
    until the window's command exits, so poll() tells you if it's over."""
    if shutil.which("gnome-terminal"):
        term = ["gnome-terminal", "--wait", f"--working-directory={folder}", "--", *cmd]
    elif shutil.which("x-terminal-emulator"):
        term = ["x-terminal-emulator", "-e", *cmd]
    elif shutil.which("xterm"):
        term = ["xterm", "-e", *cmd]
    else:
        return None
    return subprocess.Popen(term, cwd=folder, start_new_session=True)


def list_files(folder: Path, limit: int) -> tuple[list[str], int]:
    """Up to `limit` files the session left behind (not prompt.md or hidden
    folders like .claude), and how many more there were."""
    files = sorted(
        str(rel)
        for p in folder.rglob("*")
        if p.is_file()
        and (rel := p.relative_to(folder)).name != PROMPT_FILE
        and not any(part.startswith(".") for part in rel.parts)
    )
    return files[:limit], max(0, len(files) - limit)


def nothing_created(folder: Path) -> bool:
    """True if the session folder holds only what AgentRelay put there."""
    try:
        return all(p.name in (PROMPT_FILE, RELAY_DIR) for p in folder.iterdir())
    except OSError:
        return False


class Launcher:
    """Starts sessions for the relay. Tests replace this with a fake."""

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir

    def find_tool(self, name: str) -> str | None:
        return find_tool(name)

    def new_folder(self, tool: str) -> Path:
        return new_session_folder(self.sessions_dir, tool)

    def open(self, folder: Path, cmd: list[str]) -> subprocess.Popen[bytes] | None:
        """Open a window running cmd, inside tmux if it's installed."""
        if shutil.which("tmux"):
            conf = folder / RELAY_DIR / TMUX_CONF
            conf.write_text(TMUX_SETTINGS)
            cmd = in_tmux(cmd, folder.name, folder, conf)
        return open_in_new_terminal(folder, cmd)

    def capture(self, folder: Path) -> str | None:
        return tmux_capture(folder.name) if shutil.which("tmux") else None

    def send(self, folder: Path, steps: list[tuple[bool, str]]) -> bool:
        """Type into the session's window (see tmux_send). False if that isn't possible."""
        return bool(shutil.which("tmux")) and tmux_send(folder.name, steps)

    def close(self, folder: Path) -> bool:
        """Close the session's window. False if that isn't possible this way."""
        return bool(shutil.which("tmux")) and tmux_close(folder.name)


def start_interactive(sessions_dir: Path, tool: str, args: tuple[str, ...] = (), *, name: str = "") -> int:
    """`agent-relay new`: a fresh session folder (named after the profile) and
    an interactive session. In a terminal the tool takes it over; otherwise a
    new window opens."""
    exe = find_tool(tool)
    if not exe:
        print(f"Could not find {tool} on PATH.", file=sys.stderr)
        return 1
    folder = new_session_folder(sessions_dir, name or tool)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return 0 if open_in_new_terminal(folder, build_command(exe, tool, args)) else 1
    print(f"Starting {tool} in {folder}")
    os.chdir(folder)
    os.execv(exe, build_command(exe, tool, args))  # replaces this process
