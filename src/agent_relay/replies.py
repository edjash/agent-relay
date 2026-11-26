"""Threaded email replies through Gmail SMTP, within a hard reply budget."""

from __future__ import annotations

import logging
import smtplib
import time
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from agent_relay.gmail import NETWORK_TIMEOUT, REPLY_HEADER
from agent_relay.render import to_html, to_plain
from agent_relay.state import State, Task

log = logging.getLogger(__name__)

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
# How replies identify themselves: the sender name shown in Gmail (the address
# stays the account's own).
SENDER_NAME = "AgentRelay"

SmtpFactory = Callable[..., smtplib.SMTP]


class Replier:
    def __init__(
        self,
        user: str,
        password: str,
        state: State,
        *,
        dry_run: bool = False,
        smtp_factory: SmtpFactory = smtplib.SMTP_SSL,
    ):
        self.user, self.password, self.state, self.dry_run = user, password, state, dry_run
        self.smtp_factory = smtp_factory

    def reply(self, task: Task, text: str) -> None:
        """Reply to the task's email. Always addressed to the verified sender,
        never to a Reply-To the email claims. The last reply allowed by the
        budget is replaced by a "limit reached" notice; after that, silence."""
        budget = self.state.limits.reply_budget
        used = self.state.replies_in_window()
        if used >= budget:
            log.warning("Reply budget used up (%d in 24h); not replying", budget)
            return
        if used == budget - 1:
            text = (
                f"Reply limit reached: {budget} replies in the last 24 hours.\n"
                "AgentRelay keeps working but sends no more replies until older ones age out."
            )

        msg = EmailMessage()
        msg["From"] = formataddr((SENDER_NAME, self.user))
        msg["To"] = task.sender
        msg["Subject"] = task.subject if task.subject.lower().startswith("re:") else f"Re: {task.subject}"
        if task.msg_id.startswith("<"):
            msg["In-Reply-To"] = msg["References"] = task.msg_id
        msg["Message-ID"] = make_msgid(domain="agent-relay.local")
        msg[REPLY_HEADER] = "reply"
        msg.set_content(to_plain(text) + "\n")
        msg.add_alternative(to_html(text), subtype="html")

        first_line = to_plain(text).splitlines()[0]
        if self.dry_run:
            log.info("  [dry run] would reply: %s", first_line)
            return
        try:
            with self.smtp_factory(SMTP_HOST, SMTP_PORT, timeout=NETWORK_TIMEOUT) as smtp:
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            log.error("  Reply failed: %s", e)
            return
        self.state.replies.append(time.time())
        self.state.save()
        log.info("  Replied: %s", first_line)
