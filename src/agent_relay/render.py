"""Reply text to email bodies: plain text, and HTML with a simple template.

Reply text is plain text in which `pre()` marks blocks to show in a monospace
box (screens, the agent's last message, file lists, the command list). The
HTML version also ends with a short command reference.
"""

from __future__ import annotations

import html
import re

_PRE_START, _PRE_END = "\x02", "\x03"  # control characters never kept in content
_PRE_RE = re.compile(f"{_PRE_START}(.*?){_PRE_END}", re.DOTALL)

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
TEXT, MUTED, BORDER, BOX = "#1f2328", "#656d76", "#d0d7de", "#f6f8fa"

FOOTER_COMMANDS = ("stop", "screen", "status", "type: <text>", "keys: <keys>", "help")


def pre(text: str) -> str:
    """Mark text to show as a monospace block."""
    clean = text.replace(_PRE_START, "").replace(_PRE_END, "")
    return f"{_PRE_START}{clean}{_PRE_END}"


def to_plain(text: str) -> str:
    return text.replace(_PRE_START, "").replace(_PRE_END, "")


def _paragraphs(text: str) -> str:
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if para.strip():
            body = "<br>".join(html.escape(line) for line in para.strip().split("\n"))
            out.append(f'<p style="margin:0 0 12px">{body}</p>')
    return "".join(out)


def _code(text: str) -> str:
    return (
        f'<code style="font-family:{MONO};font-size:12px;background:{BOX};border:1px solid {BORDER};'
        f'border-radius:4px;padding:1px 4px">{html.escape(text)}</code>'
    )


def to_html(text: str) -> str:
    parts: list[str] = []
    pos = 0
    for m in _PRE_RE.finditer(text):
        parts.append(_paragraphs(text[pos : m.start()]))
        parts.append(
            f'<pre style="margin:0 0 12px;padding:12px;background:{BOX};border:1px solid {BORDER};'
            f"border-radius:6px;font-family:{MONO};font-size:12px;line-height:1.4;color:{TEXT};"
            f'white-space:pre-wrap;word-break:break-word">{html.escape(m.group(1).strip(chr(10)))}</pre>'
        )
        pos = m.end()
    parts.append(_paragraphs(text[pos:]))
    body = "".join(parts)
    # The first line is the news (Started, Finished, Waiting...): make it stand out.
    body = body.replace('<p style="margin:0 0 12px">', '<p style="margin:0 0 12px;font-weight:600">', 1)
    commands = " &middot; ".join(_code(c) for c in FOOTER_COMMANDS)
    return (
        f'<div style="font-family:{FONT};font-size:14px;line-height:1.5;color:{TEXT};max-width:760px">'
        f'<div style="font-size:12px;font-weight:600;letter-spacing:.04em;color:{MUTED};'
        f'margin:0 0 12px">AGENTRELAY</div>'
        f"{body}"
        f'<div style="margin-top:20px;padding-top:10px;border-top:1px solid {BORDER};font-size:12px;color:{MUTED}">'
        f"Reply with a command: {commands}</div>"
        "</div>"
    )
