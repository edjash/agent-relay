"""Deciding whether a task email is genuinely from an allowed sender."""

from __future__ import annotations

import re
from collections.abc import Collection
from email.message import EmailMessage
from email.utils import parseaddr


def sender_of(msg: EmailMessage) -> str:
    return parseaddr(str(msg.get("From", "")))[1].lower()


def sent_from_this_account(fetch_meta: bytes) -> bool:
    """True when Gmail labels the message \\Sent. Only the account owner can
    create such mail, so it can't be forged from outside."""
    m = re.search(rb"X-GM-LABELS \((.*?)\)", fetch_meta)
    return bool(m and re.search(rb'(^|[\s"])\\+Sent\b', m.group(1)))


def passes_dmarc(msg: EmailMessage, sender: str) -> bool:
    """Check the topmost Authentication-Results header, which Gmail adds itself
    on arrival (anything below it could have been written by the sender)."""
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False
    top = str(results[0]).lower()
    domain = sender.rsplit("@", 1)[-1]
    return top.startswith("mx.google.com") and "dmarc=pass" in top and f"header.from={domain}" in top


def is_trusted(msg: EmailMessage, fetch_meta: bytes, allowed: Collection[str]) -> tuple[bool, str]:
    """(trusted, sender). Trusted means From: is an allowed address and the
    mail is provably genuine: sent from this account, or DMARC-verified."""
    sender = sender_of(msg)
    if sender not in allowed:
        return False, sender
    return sent_from_this_account(fetch_meta) or passes_dmarc(msg, sender), sender
