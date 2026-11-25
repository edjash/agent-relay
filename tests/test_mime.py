import base64

import pytest

from agent_relay.mime import decode_part, find_text_part, flatten_response, parse_imap_list

PLAIN = b'("text" "plain" ("charset" "UTF-8") NIL NIL "quoted-printable" 20 1 NIL NIL NIL NIL)'
ALT = (
    b'(("text" "plain" ("charset" "UTF-8") NIL NIL "7bit" 12 1 NIL NIL NIL NIL)'
    b'("text" "html" ("charset" "UTF-8") NIL NIL "quoted-printable" 40 1 NIL NIL NIL NIL)'
    b' "alternative" ("boundary" "000") NIL NIL NIL)'
)
MIXED_PDF = (
    b"(" + ALT + b'("application" "pdf" ("name" "a.pdf") NIL NIL "base64" 99999 NIL '
    b'("attachment" ("filename" "a.pdf")) NIL NIL) "mixed" ("boundary" "x") NIL NIL NIL)'
)
HTML_WITH_TEXT_ATTACHMENT = (
    b'(("text" "html" ("charset" "iso-8859-1") NIL NIL "base64" 100 2 NIL NIL NIL NIL)'
    b'("text" "plain" ("name" "notes.txt") NIL NIL "base64" 10 1 NIL ("attachment" ("filename" "notes.txt")) NIL NIL)'
    b' "mixed" ("boundary" "y") NIL NIL NIL)'
)


@pytest.mark.parametrize(
    ("structure", "section", "subtype"),
    [
        (PLAIN, "1", "plain"),
        (ALT, "1", "plain"),
        (MIXED_PDF, "1.1", "plain"),
        (HTML_WITH_TEXT_ATTACHMENT, "1", "html"),  # the attached .txt is not the message
    ],
)
def test_find_text_part(structure: bytes, section: str, subtype: str) -> None:
    found = find_text_part(parse_imap_list(structure, 0))
    assert found is not None
    assert found[0] == section
    assert found[1][1].lower() == subtype


def test_no_text_part() -> None:
    only_image = b'("image" "png" NIL NIL NIL "base64" 10 NIL NIL NIL NIL)'
    assert find_text_part(parse_imap_list(only_image, 0)) is None


def test_literal_inside_bodystructure() -> None:
    parts = [
        (
            b'1 (UID 7 BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 5 1 NIL NIL NIL NIL)'
            b'("application" "zip" ("name" {9}',
            b'we"ird.zi',
        ),
        b') NIL NIL "base64" 10 NIL ("attachment" NIL) NIL NIL) "mixed" ("boundary" "z") NIL NIL NIL))',
    ]
    flat = flatten_response(parts)
    structure = parse_imap_list(flat, flat.find(b"BODYSTRUCTURE (") + len(b"BODYSTRUCTURE "))
    assert structure[1][2] == ["name", 'we"ird.zi']
    assert find_text_part(structure) == ("1", structure[0])


def test_decode_quoted_printable() -> None:
    part = parse_imap_list(PLAIN, 0)
    assert decode_part(b"Caf=C3=A9 =E2=82=AC5\r\nline two\r\n", part) == "Café €5\nline two"


def test_decode_base64_latin1_html() -> None:
    part = parse_imap_list(HTML_WITH_TEXT_ATTACHMENT, 0)[0]
    html = "<style>p{}</style><p>Ol\xe1 &amp; bye</p><div>next</div>".encode("latin-1")
    assert decode_part(base64.b64encode(html), part) == "Olá & bye\nnext"


def test_decode_truncated_base64() -> None:
    part = parse_imap_list(b'("text" "plain" NIL NIL NIL "base64" 10 1 NIL NIL NIL NIL)', 0)
    encoded = base64.b64encode(b"hello world, this is long")
    assert decode_part(encoded[:-3], part).startswith("hello world")


def test_unknown_charset_falls_back_to_utf8() -> None:
    part = parse_imap_list(b'("text" "plain" ("charset" "x-made-up") NIL NIL "7bit" 5 1 NIL NIL NIL NIL)', 0)
    assert decode_part("héllo".encode(), part) == "héllo"
