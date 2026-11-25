"""Parsing IMAP responses and email structure, so only the text part of a
message is ever downloaded."""

from __future__ import annotations

import base64
import html
import quopri
import re
from collections.abc import Iterator
from typing import Any

# A parsed IMAP list: nested lists of strings and None (NIL).
ImapList = list[Any]

_TOKEN = re.compile(rb'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def flatten_response(parts: list[Any]) -> bytes:
    """Join an imaplib FETCH response back into one IMAP string, turning
    {n} literals into quoted strings so it can be parsed in one go."""
    out = b""
    for p in parts:
        if isinstance(p, tuple):
            head, literal = p
            quoted = literal.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
            out += re.sub(rb"\{\d+\}$", b"", head) + b'"' + quoted + b'"'
        elif p:
            out += p
    return out


def parse_imap_list(data: bytes, pos: int) -> ImapList:
    """Parse the parenthesised IMAP list starting at data[pos] == '('."""
    stack: list[ImapList] = [[]]
    while True:
        m = _TOKEN.match(data, pos)
        if not m:
            raise ValueError(f"cannot parse IMAP data at offset {pos}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            done = stack.pop()
            stack[-1].append(done)
            if len(stack) == 1:
                result: ImapList = stack[0][0]
                return result
        elif m.group(3) is not None:
            stack[-1].append(re.sub(rb"\\(.)", rb"\1", m.group(3)).decode("utf-8", "replace"))
        else:
            atom = m.group(4).decode("ascii", "replace")
            stack[-1].append(None if atom.upper() == "NIL" else atom)


def body_parts(node: ImapList, section: str = "") -> Iterator[tuple[str, ImapList]]:
    """Yield (section number, part) for every leaf part of a BODYSTRUCTURE.
    Attached emails (message/rfc822) are leaves: we never look inside them."""
    if isinstance(node[0], list):  # multipart: child parts, then the subtype string
        children = []
        for child in node:
            if not isinstance(child, list):
                break  # the subtype; extension data follows
            children.append(child)
        for i, child in enumerate(children, 1):
            yield from body_parts(child, f"{section}.{i}" if section else str(i))
    else:
        yield section or "1", node


def is_attachment(part: ImapList) -> bool:
    return any(isinstance(x, list) and x and str(x[0]).lower() == "attachment" for x in part[7:])


def find_text_part(structure: ImapList) -> tuple[str, ImapList] | None:
    """Section number and part of the message text: plain if present, else HTML."""
    leaves = list(body_parts(structure))
    for subtype in ("plain", "html"):
        for section, part in leaves:
            if (str(part[0]).lower(), str(part[1]).lower()) == ("text", subtype) and not is_attachment(part):
                return section, part
    return None


def html_to_text(text: str) -> str:
    text = re.sub(r"<(style|script)\b.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(br|/p|/div)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def decode_part(raw: bytes, part: ImapList) -> str:
    """Decode a downloaded body part using its transfer encoding and charset."""
    encoding = str(part[5] or "7bit").lower()
    if encoding == "base64":
        raw = re.sub(rb"\s", b"", raw)
        raw = base64.b64decode(raw[: len(raw) // 4 * 4])  # whole groups only (may be truncated)
    elif encoding == "quoted-printable":
        raw = quopri.decodestring(raw)
    params = part[2] or []
    charset = dict(zip((str(k).lower() for k in params[::2]), params[1::2], strict=False)).get("charset")
    try:
        text = raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n")
    if str(part[1]).lower() == "html":
        text = html_to_text(text)
    return text.strip()
