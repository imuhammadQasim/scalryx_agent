"""Outgoing email over SMTP. Without SMTP settings, messages are logged."""

import asyncio
import logging
from email.message import EmailMessage

import aiosmtplib

from ..config import get_settings

log = logging.getLogger("scalryx.mail")

# Keeps fire-and-forget sends alive until they finish.
_pending: set[asyncio.Task] = set()


async def send(to: str, subject: str, text: str, reply_to: str | None = None, bcc: str | None = None) -> bool:
    s = get_settings()
    if not s.email_enabled:
        log.info("Email not configured; would send to %s: %s\n%s", to, subject, text)
        return False

    msg = EmailMessage()
    msg["From"] = s.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    if bcc:
        msg["Bcc"] = bcc
    msg.set_content(text)
    implicit_tls = s.smtp_port == 465

    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user or None,
            password=s.smtp_password or None,
            use_tls=implicit_tls,
            start_tls=s.smtp_starttls and not implicit_tls,
            timeout=20,
        )
        return True
    except (aiosmtplib.SMTPAuthenticationError, aiosmtplib.SMTPServerDisconnected) as exc:
        # Gmail answers a bad login with 535, or for some accounts just hangs up.
        log.error(
            "Email to %s failed: the SMTP login was refused or the server hung up (%s). "
            "Check SMTP_USER, and for Gmail make SMTP_PASSWORD an App Password, not your "
            "normal password: myaccount.google.com/apppasswords",
            to,
            exc,
        )
        return False
    except (aiosmtplib.SMTPException, OSError):
        log.exception("Email to %s failed: %s", to, subject)
        return False


def send_later(to: str, subject: str, text: str, reply_to: str | None = None) -> None:
    """Send without making the visitor wait. Used for alerts to the team."""
    task = asyncio.create_task(send(to, subject, text, reply_to=reply_to))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _check(to: str | None) -> int:
    """`python -m app.integrations.mailer [address]`: send one test email."""
    s = get_settings()
    if not s.email_enabled:
        print("SMTP is not set up yet: put SMTP_USER and SMTP_PASSWORD in .env.")
        return 1
    target = to or s.team_email
    print(f"Sending a test email from {s.mail_from} to {target} via {s.smtp_host}:{s.smtp_port} ...")
    if await send(target, "Scalryx agent: test email", "If you can read this, the agent can send email."):
        print("Sent. Check the inbox (and the spam folder).")
        return 0
    print("Not sent: see the error above.")
    return 1


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(_check(sys.argv[1] if len(sys.argv) > 1 else None)))
