# AgentRelay

[![CI](https://github.com/edjash/agent-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/edjash/agent-relay/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Email yourself a task; AgentRelay opens an interactive **Claude Code** or **Codex** session for it on your
desktop and replies to the email as it goes.

```
Subject: claude: research Linux accessibility APIs
Body:    Compare AT-SPI with the alternatives and write up the findings.
```

AgentRelay watches a Gmail inbox. For each genuine task email it creates a new session folder, saves the
prompt there as `prompt.md`, and opens a terminal window running `claude "<prompt>"` (or `codex`). A prompt too long to
pass that way (over 100 KB) is replaced by an instruction to read `prompt.md`. Only one session runs at a time;
others wait in a short queue.

It uses only the Python standard library.

## How it behaves

| A task email that is... | What happens | Reply? |
|---|---|---|
| not genuinely from an allowed sender | ignored | never |
| already handled, or older than 24 hours | ignored | no |
| one too many (flood: >10 in 10 minutes) | relay pauses until restarted, queue cleared | one alert |
| beyond the queue limit (5 waiting) | refused | no |
| empty (`claude:` and nothing else) | not started | asks for a task |
| the same task again within an hour | ignored | no |
| accepted while a session is running | queued | queue position |
| accepted and started | session opens; "started" once the agent takes the prompt | "started", with the folder |
| stuck at start (no prompt taken in 10 seconds: login, update, other dialog) | left open for `agent: type` / `keys`; a new task or `agent: stop` closes it | "waiting for input", with its screen (instead of "started") |
| failed to start | dropped | the reason |
| finished (agent's turn over; for Claude, no background tasks left) | if a task is queued: window closed, next task starts; otherwise left open for follow-ups until a new task arrives | "finished", with the agent's last message and the files |
| closed by you | next task starts | "session closed", with the same |

Only one session is open at a time, and **a new task closes the open session** unless its agent is working
(then the task queues). Each agent gets hooks that report when it takes the prompt, uses a tool and finishes
its turn. Claude also reports background tasks it left running (a build, a test run): it doesn't count as
finished until they end and Claude finishes again. Codex reports none, so for Codex the end of its turn counts.
A finished session is closed straight away if a task is waiting; otherwise it stays open, so a reply of
`type <follow-up>` in its thread carries on the conversation (and brings another "finished" email). A closed
session whose agent created no files has its
folder removed, so empty folders don't pile up; the conversation is kept by the agent and can still be resumed.
To carry on with a closed session, run `claude --continue` or `codex resume` in its folder.

**Triggering**: the subject must start with `claude:` or `codex:` (any case). The rest of the subject and the
plain-text body become the prompt. Attachments are never downloaded.

**Replies** go back in the same thread, shown as from "AgentRelay", always to the verified sender (never a claimed `Reply-To`), and carry
an `X-Agent-Relay` header so a reply can never trigger a session. At most 30 replies are sent in any 24 hours;
the 30th says the limit was reached.

### Email commands

Emails with an `agent:` subject control AgentRelay itself (same sender check; only `agent: type` reads the
body). They are answered straight away, never queued, and count towards the flood breaker and reply budget like tasks.

| Subject | Reply |
|---|---|
| `agent: status` | what is running and for how long, the queue, replies left today |
| `agent: stop` | closes the running session (with its last message); the next task starts, and the stopped task can be sent again |
| `agent: screen` | what is on the running session's screen (needs tmux) |
| `agent: type <text>` | types the text into the session and presses Enter, then replies with the screen (needs tmux). Text missing from the subject is taken from the body's first line |
| `agent: keys <keys>` | presses keys, e.g. `agent: keys down down enter`, then replies with the screen (needs tmux). Keys: `enter`, `esc`, `tab`, `shift-tab`, `space`, `backspace`, arrows, `ctrl-<letter>`, or any single character |
| `agent: help` | the list of commands |

You can also **reply** to any AgentRelay email (or task email) with just the command on the first line,
such as `stop`, `screen` or `type 1`; a leading `agent:` is fine too. It works exactly like the `agent:` subject,
and the quoted text below is ignored. A reply is never treated as a new task. `stop`, `type` and `keys` sent in
a task's thread only act on that task's session: in the thread of a session that has ended they do nothing and
say so. (Replies in an `agent:` thread act on whatever is running.)

`type` and `keys` answer whatever a session is stuck on (an update question, a login code, a menu): the
"waiting for input" reply shows it, and `agent: screen` shows it again. A stuck session is never closed by
itself; a new `claude:` or `codex:` task closes it and starts instead. Don't type passwords this way; they would
stay in your sent mail.

## Security model

A task email makes an agent run on your machine, so AgentRelay only accepts mail it can prove is genuine:

- the `From` address must be in `AGENT_RELAY_ALLOWED_SENDERS`, **and**
- Gmail must vouch for it: either the mail was sent from the watched account itself (Gmail's `\Sent` label,
  which outsiders cannot set) or Gmail's own topmost `Authentication-Results` header shows `dmarc=pass`.

A forged `From` header is ignored silently: replying would confirm the relay exists and could mail a faked
address. Sessions run unattended, so nobody is there to answer permission prompts: `.env.example` passes
the agents' skip-permission flags (`AGENT_RELAY_CLAUDE_ARGS`, `AGENT_RELAY_CODEX_ARGS`), and a session can
do anything your user account can. The sender check above is what keeps that safe.

## Install

Requires Linux, Python 3.11+, a desktop session (sessions open terminal windows; GNOME Terminal preferred,
falling back to `x-terminal-emulator` or `xterm`), and `claude` and/or `codex` on your `PATH`.
`tmux` is recommended: sessions then run inside it (status bar hidden), so AgentRelay can include a stuck
session's screen in its reply and close windows cleanly.

```bash
git clone https://github.com/edjash/agent-relay.git ~/Projects/agent-relay && cd ~/Projects/agent-relay
python3 -m venv .venv && .venv/bin/pip install -e .
ln -s "$PWD/.venv/bin/agent-relay" ~/.local/bin/agent-relay
```

## Set up

1. Turn on 2-Step Verification for the Google account and create an **app password** at
   <https://myaccount.google.com/apppasswords>.
2. Create and fill in the settings:
   ```bash
   agent-relay init        # copies .env.example to .env in the project folder (mode 600, git-ignored)
   ```
   Set `AGENT_RELAY_GMAIL_USER`, `AGENT_RELAY_APP_PASSWORD` and `AGENT_RELAY_ALLOWED_SENDERS`.
3. Send yourself a `claude: test` email and see what would happen, without starting or sending anything:
   ```bash
   agent-relay check --dry-run
   ```
4. Run it in the foreground, or as a background service (below):
   ```bash
   agent-relay run
   ```

## Commands

| Command | |
|---|---|
| `agent-relay init` | create `.env` from `.env.example` |
| `agent-relay run [--dry-run] [-v]` | listen until stopped |
| `agent-relay check [--dry-run] [-v]` | one pass, then exit |
| `agent-relay status` | listener, current session, queue, replies used |
| `agent-relay new [claude\|codex]` | start a session by hand in a new session folder |
| `agent-relay install-service [--force]` | install the systemd user service |

Only one listener can run at a time (an OS file lock, released automatically even if it crashes).

## Run as a background service

```bash
agent-relay install-service
systemctl --user daemon-reload
systemctl --user enable --now agent-relay
journalctl --user -u agent-relay -f     # logs
```

To stop it, or remove it completely:

```bash
systemctl --user stop agent-relay                   # stop until the next login
systemctl --user disable --now agent-relay          # stop and don't start at login
rm ~/.config/systemd/user/agent-relay.service       # then remove the service...
systemctl --user daemon-reload                      # ...and tell systemd it's gone
```

It starts with your desktop session and restarts after a crash. Problems a restart can't fix (bad settings,
wrong password, already running) exit with code 2 and the service stays stopped.

## Configuration

Settings live in `.env` in the project folder (git-ignored; `.env.example` documents every one). Real
environment variables with the same names override the file. Unknown `AGENT_RELAY_*` names are rejected, so a
typo can't silently fall back to a default. To keep `.env` somewhere else, set `AGENT_RELAY_ENV_FILE`.

| Variable | Default | |
|---|---|---|
| `AGENT_RELAY_GMAIL_USER` | | the inbox to watch |
| `AGENT_RELAY_APP_PASSWORD` | | Google app password |
| `AGENT_RELAY_ALLOWED_SENDERS` | | comma-separated addresses allowed to send tasks |
| `AGENT_RELAY_SESSIONS_DIR` | `~/agent-relay/sessions` | where session folders are created |
| `AGENT_RELAY_CLAUDE_ARGS` | none (`.env.example`: `--dangerously-skip-permissions`) | extra flags for Claude, shell-style |
| `AGENT_RELAY_CODEX_ARGS` | none (`.env.example`: `--dangerously-bypass-approvals-and-sandbox`) | extra flags for Codex, shell-style |
| `AGENT_RELAY_MAX_AGE_HOURS` | 24 | ignore older task emails |
| `AGENT_RELAY_QUEUE_MAX` | 5 | tasks allowed to wait |
| `AGENT_RELAY_FLOOD_COUNT` / `_FLOOD_WINDOW_MINUTES` | 10 / 10 | flood breaker threshold |
| `AGENT_RELAY_REPLY_BUDGET` | 30 | replies per rolling 24 hours |
| `AGENT_RELAY_DUPLICATE_WINDOW_MINUTES` | 60 | identical task ignored within this window |
| `AGENT_RELAY_LOG_LEVEL` / `_LOG_FILE` | `INFO` / none | |

State (handled emails, queue, reply count) lives in `~/.local/state/agent-relay/state.json`.

## How it talks to Gmail

- One IMAP connection, kept quiet with **IDLE**: Gmail pushes a notification when mail arrives; there is no
  polling. The connection is refreshed every 10 minutes.
- The search runs on Gmail's side (sender and subject word), then only headers are fetched to decide, then only
  the text part of an accepted email (capped at 1 MB; the Started reply says if a body was cut off).
- The inbox is opened read-only: nothing is marked read, moved or deleted.
- Network failures back off 30 s, 1, 2, 4… up to 30 minutes; Gmail refusals (connection limits, security
  alerts) wait the full 30 minutes at once; a dead connection is detected within 60 seconds.

## Limitations

- If the listener is restarted while a session window is open, it doesn't know about that session and may
  start the next task alongside it.
- It needs you logged in to a desktop session to open terminal windows.
- An agent that ends its turn with a question counts as finished: the session closes, and the question is in
  the "finished" reply. Answer it by resuming the session in its folder.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest            # tests use fakes: no network, no windows
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy              # strict
```

Layout: `src/agent_relay/` — `cli` (commands), `relay` (decisions and the run loop), `gmail` (IMAP, IDLE),
`mime` (email structure), `trust` (sender verification), `replies` (SMTP), `sessions` (folders, terminals),
`state` (persistence, lock), `config`.
