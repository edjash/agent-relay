import email
from email import policy
from email.message import EmailMessage

from agent_relay.trust import is_trusted, sent_from_this_account

ALLOWED = {"owner@example.com"}
DMARC_PASS = "mx.google.com; dkim=pass header.i=@example.com; spf=pass; dmarc=pass (p=NONE) header.from=example.com"
NO_LABELS = b"1 (X-GM-LABELS () UID 1"
SENT = b'1 (X-GM-LABELS (\\Important "\\\\Sent") UID 1'


def message(sender: str, *auth_results: str) -> EmailMessage:
    headers = f"From: Someone <{sender}>\nSubject: claude: x\n"
    headers += "".join(f"Authentication-Results: {a}\n" for a in auth_results)
    msg = email.message_from_string(headers + "\nbody\n", policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


def test_sent_label_detection() -> None:
    assert sent_from_this_account(SENT)
    assert sent_from_this_account(b"1 (X-GM-LABELS (\\Sent) UID 1")
    assert not sent_from_this_account(NO_LABELS)
    assert not sent_from_this_account(b"1 (X-GM-LABELS (Sentimental) UID 1")


def test_sent_from_account_is_trusted() -> None:
    assert is_trusted(message("owner@example.com"), SENT, ALLOWED) == (True, "owner@example.com")


def test_dmarc_verified_mail_is_trusted() -> None:
    assert is_trusted(message("owner@example.com", DMARC_PASS), NO_LABELS, ALLOWED)[0]


def test_forged_from_is_not_trusted() -> None:
    assert not is_trusted(message("owner@example.com"), NO_LABELS, ALLOWED)[0]


def test_dmarc_fail_is_not_trusted() -> None:
    failed = DMARC_PASS.replace("dmarc=pass", "dmarc=fail")
    assert not is_trusted(message("owner@example.com", failed), NO_LABELS, ALLOWED)[0]


def test_only_gmails_own_topmost_header_counts() -> None:
    # A sender can write a fake "pass" header, but Gmail's real one sits above it.
    real = "mx.google.com; spf=fail; dmarc=fail header.from=example.com"
    assert not is_trusted(message("owner@example.com", real, DMARC_PASS), NO_LABELS, ALLOWED)[0]


def test_other_sender_is_not_trusted_even_with_sent_label() -> None:
    assert is_trusted(message("stranger@example.org"), SENT, ALLOWED) == (False, "stranger@example.org")
