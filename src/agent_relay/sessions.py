"""Session folders and terminal windows running Claude Code or Codex."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

TOOLS = ("claude", "codex")
PROMPT_FILE = "prompt.md"


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


class Launcher:
    """Starts sessions for the relay. Tests replace this with a fake."""

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir

    def find_tool(self, name: str) -> str | None:
        return find_tool(name)

    def new_folder(self, tool: str) -> Path:
        return new_session_folder(self.sessions_dir, tool)

    def open(self, folder: Path, cmd: list[str]) -> subprocess.Popen[bytes] | None:
        return open_in_new_terminal(folder, cmd)


def start_interactive(sessions_dir: Path, tool: str) -> int:
    """`agent-relay new`: a fresh session folder and an interactive session.
    In a terminal the tool takes it over; otherwise a new window opens."""
    exe = find_tool(tool)
    if not exe:
        print(f"Could not find {tool} on PATH.", file=sys.stderr)
        return 1
    folder = new_session_folder(sessions_dir, tool)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return 0 if open_in_new_terminal(folder, [exe]) else 1
    print(f"Starting {tool} in {folder}")
    os.chdir(folder)
    os.execv(exe, [exe])  # replaces this process
