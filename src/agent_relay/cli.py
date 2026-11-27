"""The `agent-relay` command."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent_relay import __version__, config
from agent_relay.config import Config, ConfigError
from agent_relay.relay import EXIT_ERROR, EXIT_NO_RESTART, EXIT_OK, Relay, run
from agent_relay.replies import Replier
from agent_relay.sessions import Launcher, start_interactive
from agent_relay.state import AlreadyRunning, State, acquire_lock, lock_holder

log = logging.getLogger("agent_relay")

SERVICE = "agent-relay"
SERVICE_PATH = Path.home() / f".config/systemd/user/{SERVICE}.service"
SERVICE_TEMPLATE = """\
[Unit]
Description=AgentRelay: start Claude Code / Codex sessions from Gmail
Documentation=file://{readme}
# Sessions open terminal windows, so this needs your desktop session.
After=graphical-session.target network-online.target
PartOf=graphical-session.target

[Service]
ExecStart={exe} run
Restart=on-failure
RestartSec=60
# Exit code 2 means a problem a restart can't fix (bad config, wrong password,
# already running): stay stopped and check `journalctl --user -u agent-relay`.
RestartPreventExitStatus=2

[Install]
WantedBy=graphical-session.target
"""


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, cfg.log_level, logging.INFO)
    # The systemd journal timestamps lines itself.
    fmt = "%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(levelname)s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg.log_file:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(cfg.log_file)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handlers.append(file_handler)
    handlers[0].setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    logging.basicConfig(level=level, handlers=handlers, force=True)


# ---------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    if config.ENV_PATH.exists() and not config.PROFILES_PATH.exists():  # set up before config.json existed
        config.write_template(config.PROFILES_PATH, config.PROFILES_TEMPLATE)
        print(f"Created {config.PROFILES_PATH}: the agents ('claude:', 'codex:') and their flags.")
        return EXIT_OK
    path = config.write_template(config.ENV_PATH)
    if not config.PROFILES_PATH.exists():
        config.write_template(config.PROFILES_PATH, config.PROFILES_TEMPLATE)
    print(f"Created {path} (readable only by you), and {config.PROFILES_PATH} (the agents and their flags).\n")
    print("Next:")
    print("  1. Turn on 2-Step Verification for the Google account, then create an app password at")
    print("     https://myaccount.google.com/apppasswords")
    print(f"  2. Edit {path}: set AGENT_RELAY_GMAIL_USER, AGENT_RELAY_APP_PASSWORD and AGENT_RELAY_ALLOWED_SENDERS")
    print("  3. Try it:  agent-relay check --dry-run")
    print("  4. Run it:  agent-relay service install   (runs in the background, and at every login)")
    return EXIT_OK


def _listen(args: argparse.Namespace, *, once: bool) -> int:
    cfg = config.load()
    setup_logging(cfg, args.verbose)
    try:
        lock = acquire_lock(config.LOCK_PATH)
    except AlreadyRunning as e:
        log.error("Another AgentRelay listener is already running (pid %s). Not starting a second one.", e.pid)
        return EXIT_NO_RESTART
    with lock:
        state = State.load(config.STATE_PATH, cfg.limits, dry_run=args.dry_run)
        replier = Replier(cfg.gmail_user, cfg.app_password, state, dry_run=args.dry_run)
        relay = Relay(cfg, state, replier, Launcher(cfg.sessions_dir), dry_run=args.dry_run)
        log.info(
            "AgentRelay %s watching %s for %s mail from %s%s",
            __version__,
            cfg.gmail_user,
            " / ".join(f"'{n}:'" for n in cfg.profiles),
            ", ".join(sorted(cfg.allowed_senders)),
            " (dry run)" if args.dry_run else "",
        )
        return run(relay, once=once)


def cmd_run(args: argparse.Namespace) -> int:
    return _listen(args, once=False)


def cmd_check(args: argparse.Namespace) -> int:
    return _listen(args, once=True)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load(require_gmail=False)
    state = State.load(config.STATE_PATH, cfg.limits)
    pid = lock_holder(config.LOCK_PATH)
    if pid is None:
        print("Listener:  not running")
    else:
        listener = state.listener
        since = f" since {listener['started']}" if listener.get("started") else ""
        print(f"Listener:  running (pid {pid}){since}")
        if listener.get("paused"):
            print("           PAUSED after a flood; restart it to resume")
        running = listener.get("running")
        if running:
            mins = int((time.time() - running["started"]) // 60)
            waiting = {
                "starting": " (waiting for it to take the prompt)",
                "idle": " (finished; open for follow-ups)",
            }.get(str(running.get("phase")), "")
            print(f"Session:   {running['tool']}: {running['title'][:60]} ({mins} min, {running['folder']}){waiting}")
        else:
            print("Session:   none")
    print(f"Queue:     {len(state.queue)} of {cfg.limits.queue_max}")
    for i, task in enumerate(state.queue, 1):
        print(f"  {i}. {task.tool}: {task.title[:70]}")
    print(f"Replies:   {state.replies_in_window()} of {cfg.limits.reply_budget} used in the last 24 hours")
    if state.processed:
        last = max(state.processed.values())[:16].replace("T", " ")
        print(f"Handled:   {len(state.processed)} task email(s) in the last 7 days, latest {last}")
    print(f"Settings:  {config.ENV_PATH}{'' if config.ENV_PATH.exists() else ' (missing: run agent-relay init)'}")
    missing = "" if config.PROFILES_PATH.exists() else " (missing: run agent-relay init)"
    print(f"Agents:    {', '.join(cfg.profiles) or '-'} ({config.PROFILES_PATH}{missing})")
    return EXIT_OK


def cmd_new(args: argparse.Namespace) -> int:
    cfg = config.load(require_gmail=False)
    profile = cfg.profiles.get(args.agent)
    if profile is None:
        names = ", ".join(cfg.profiles) or f"none: run agent-relay init to create {config.PROFILES_PATH}"
        print(f"No '{args.agent}' profile in {config.PROFILES_PATH} (there are: {names}).", file=sys.stderr)
        return EXIT_ERROR
    return start_interactive(cfg.sessions_dir, profile.cli, profile.args, name=profile.name)


def _systemctl(*args: str) -> int:
    try:
        return subprocess.run(["systemctl", "--user", *args]).returncode
    except FileNotFoundError:
        print("systemctl not found: the service needs systemd.", file=sys.stderr)
        return EXIT_ERROR


def _install(force: bool) -> int:
    if SERVICE_PATH.exists() and not force:
        print(f"The service is already installed ({SERVICE_PATH}); use --force to rewrite it.", file=sys.stderr)
        return EXIT_ERROR
    exe = shutil.which("agent-relay") or str(Path(sys.argv[0]).resolve())
    readme = Path(__file__).resolve().parents[2] / "README.md"
    SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_PATH.write_text(SERVICE_TEMPLATE.format(exe=exe, readme=readme))
    code = _systemctl("daemon-reload") or _systemctl("enable", "--now", SERVICE)
    if code == 0:
        print("Installed and started. It also starts at every login.")
    return code


def _uninstall() -> int:
    code = _systemctl("disable", "--now", SERVICE)
    SERVICE_PATH.unlink()
    code = _systemctl("daemon-reload") or code
    if code == 0:
        print("Stopped and removed.")
    return code


def cmd_service(args: argparse.Namespace) -> int:
    action = args.action
    if action == "install":
        return _install(args.force)
    if not SERVICE_PATH.exists():
        print("The service isn't installed: run agent-relay service install", file=sys.stderr)
        return EXIT_ERROR
    if action == "uninstall":
        return _uninstall()
    if action == "status":
        return _systemctl("status", "--no-pager", SERVICE)
    if action == "logs":
        try:
            return subprocess.run(["journalctl", "--user", "-u", SERVICE, "-n", "50", "-f"]).returncode
        except FileNotFoundError:
            print("journalctl not found: the service needs systemd.", file=sys.stderr)
            return EXIT_ERROR
    code = _systemctl(action, SERVICE)  # start, stop, restart
    if code == 0:
        print(
            {"start": "Started.", "stop": "Stopped (it starts again at the next login).", "restart": "Restarted."}[
                action
            ]
        )
    return code


# ---------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-relay",
        description="Start Claude Code or Codex sessions from Gmail, and reply with progress.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("init", help="create .env and config.json from their examples").set_defaults(func=cmd_init)

    for p, func in (
        (sub.add_parser("run"), cmd_run),  # what the service runs; not listed, use `service`
        (sub.add_parser("check", help="check for task emails once, then exit"), cmd_check),
    ):
        p.add_argument("--dry-run", action="store_true", help="show what would happen; start, send and save nothing")
        p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
        p.set_defaults(func=func)

    sub.add_parser("status", help="show the listener, session, queue and reply budget").set_defaults(func=cmd_status)

    p = sub.add_parser("new", help="start a session by hand in a new session folder")
    p.add_argument("agent", nargs="?", default="claude", help="a profile from config.json (default: claude)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("service", help="install, start, stop, restart or remove the background service; see its log")
    actions = p.add_subparsers(dest="action", metavar="ACTION", required=True)
    for name, help_text in (
        ("install", "install the service and start it (it also starts at every login)"),
        ("start", "start the service"),
        ("stop", "stop the service until the next login"),
        ("restart", "restart the service, e.g. after editing .env or config.json"),
        ("status", "show whether the service is running"),
        ("logs", "show the service's log as it happens (Ctrl+C to leave)"),
        ("uninstall", "stop the service and remove it"),
    ):
        a = actions.add_parser(name, help=help_text)
        if name == "install":
            a.add_argument("--force", action="store_true", help="rewrite the service file if it's already installed")
    p.set_defaults(func=cmd_service)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:  # bare `agent-relay`: show the commands
        parser.print_help()
        return EXIT_ERROR
    try:
        code: int = args.func(args)
        return code
    except ConfigError as e:
        print(f"agent-relay: {e}", file=sys.stderr)
        return EXIT_NO_RESTART
    except KeyboardInterrupt:
        return EXIT_OK
