"""The Gmail IMAP connection: searching, fetching only what's needed, and IDLE."""

from __future__ import annotations

import email
import imaplib
import re
import select
import time
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.message import EmailMessage

from agent_relay.mime import decode_part, find_text_part, flatten_response, parse_imap_list

IMAP_HOST = "imap.gmail.com"
# Any single network read/write slower than this means the connection is dead
# (laptop slept, Wi-Fi changed...), which raises TimeoutError so we reconnect.
NETWORK_TIMEOUT = 60
# Largest amount of message text downloaded for a prompt.
MAX_BODY_BYTES = 1_000_000
# Header on our own replies; mail carrying it is never treated as a task.
REPLY_HEADER = "X-Agent-Relay"
# Server-side pre-filter so ordinary mail is never downloaded.
SUBJECT_TERMS = 'OR SUBJECT "claude" OR SUBJECT "codex" SUBJECT "agent"'
# IMAP dates need English month names whatever the system locale is.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class Fetched:
    """The small part of a message fetched before deciding what to do with it."""

    uid: str
    meta: bytes  # FETCH response items: labels, received time...
    msg: EmailMessage  # headers only
    received: datetime | None


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def sender_terms(senders: Collection[str]) -> str:
    """IMAP search terms matching any of the senders."""
    terms = [f'FROM "{a}"' for a in sorted(senders)]
    while len(terms) > 1:
        terms = [f"OR {terms[0]} {terms[1]}", *terms[2:]]
    return terms[0]


class Gmail:
    def __init__(self, imap: imaplib.IMAP4):
        self.imap = imap

    @staticmethod
    @contextmanager
    def connect(user: str, password: str) -> Iterator[Gmail]:
        with imaplib.IMAP4_SSL(IMAP_HOST, timeout=NETWORK_TIMEOUT) as imap:
            imap.login(user, password)
            yield Gmail(imap)

    def search_tasks(self, senders: Collection[str], since: date) -> list[str]:
        """UIDs of inbox mail from the senders, with claude/codex/agent in the subject,
        received on or after `since` (a date in Gmail's timezone)."""
        self.imap.select("INBOX", readonly=True)
        typ, data = self.imap.uid("SEARCH", "SINCE", imap_date(since), sender_terms(senders), SUBJECT_TERMS)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"search failed: {data}")
        return [u.decode() for u in data[0].split()]

    def fetch_headers(self, uids: list[str]) -> list[Fetched]:
        """Labels, received time and the few headers needed, oldest first."""
        fields = f"FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES AUTHENTICATION-RESULTS {REPLY_HEADER.upper()}"
        typ, parts = self.imap.uid(
            "FETCH", ",".join(uids), f"(X-GM-LABELS INTERNALDATE BODY.PEEK[HEADER.FIELDS ({fields})])"
        )
        if typ != "OK":
            raise imaplib.IMAP4.error(f"fetch failed: {parts}")
        results = []
        for i, p in enumerate(parts):
            if not isinstance(p, tuple):
                continue
            meta, headers = p
            after = parts[i + 1] if i + 1 < len(parts) and isinstance(parts[i + 1], bytes) else b""
            meta += after  # items Gmail puts after the headers literal
            m = re.search(rb"UID (\d+)", meta)
            if not m:
                continue
            received_t = imaplib.Internaldate2tuple(meta)
            received = datetime.fromtimestamp(time.mktime(received_t)) if received_t else None
            msg = email.message_from_bytes(headers, policy=policy.default)
            assert isinstance(msg, EmailMessage)
            results.append(Fetched(m.group(1).decode(), meta, msg, received))
        return sorted(results, key=lambda r: int(r.uid))

    def fetch_body_text(self, uid: str) -> str:
        return self.fetch_body(uid)[0]

    def fetch_body(self, uid: str) -> tuple[str, bool]:
        """Download only the message's text part (never attachments), capped at
        MAX_BODY_BYTES. Also says whether it was longer than that and cut off."""
        typ, parts = self.imap.uid("FETCH", uid, "(BODYSTRUCTURE)")
        flat = flatten_response(parts) if typ == "OK" else b""
        at = flat.find(b"BODYSTRUCTURE (")
        if at < 0:
            return "", False
        found = find_text_part(parse_imap_list(flat, at + len(b"BODYSTRUCTURE ")))
        if not found:
            return "", False
        section, part = found
        typ, parts = self.imap.uid("FETCH", uid, f"(BODY.PEEK[{section}]<0.{MAX_BODY_BYTES + 1}>)")
        if typ != "OK":
            return "", False
        raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
        if raw is None:  # tiny bodies may come back as a quoted string
            m = re.search(rb'BODY\[[^\]]*\](?:<\d+>)? "((?:[^"\\]|\\.)*)"', flatten_response(parts))
            raw = re.sub(rb"\\(.)", rb"\1", m.group(1)) if m else b""
        cut = len(raw) > MAX_BODY_BYTES  # one byte more was asked for, to tell
        return decode_part(raw[:MAX_BODY_BYTES], part), cut

    def idle_wait(self, timeout: float, also_fds: Sequence[int] = ()) -> bool:
        """Block in IMAP IDLE until the mailbox changes, one of `also_fds`
        becomes readable (the session started or ended), or `timeout` passes. Returns True if
        Gmail reported a change.

        Python 3.13's imaplib has no idle(), so this speaks RFC 2177 directly.
        """
        imap = self.imap
        tag: bytes = imap._new_tag()
        imap.send(tag + b" IDLE\r\n")
        reply = imap.readline()
        if not reply.startswith(b"+"):
            raise imaplib.IMAP4.error(f"IDLE refused: {reply!r}")

        sock = imap.sock.fileno()
        fds = [sock, *also_fds]
        changed = False
        deadline = time.monotonic() + timeout
        while not changed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(fds, [], [], remaining)
            if not ready or sock not in ready:
                break  # timed out, or the session started or ended
            line = imap.readline()
            if not line:
                raise EOFError("connection closed during IDLE")
            # "* OK ..." is a keepalive; anything else (EXISTS, EXPUNGE...) is a change.
            changed = not line.startswith(b"* OK")

        imap.send(b"DONE\r\n")
        while True:  # drain until IDLE's tagged completion
            line = imap.readline()
            if not line:
                raise EOFError("connection closed ending IDLE")
            if line.startswith(tag):
                if not line[len(tag) :].strip().upper().startswith(b"OK"):
                    raise imaplib.IMAP4.error(f"IDLE failed: {line!r}")
                return changed
