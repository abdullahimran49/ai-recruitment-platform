"""Email sender shared by the Streamlit app and the portal backend.

Two transports, selected by EMAIL_MODE in .env:
  - "smtp"  : classic SMTP (STARTTLS). Blocked by some ISPs.
  - "brevo" : Brevo (ex-Sendinblue) REST API over HTTPS port 443 — works on
              networks where the SMTP protocol is filtered.

Kept dependency-light (no LLM imports) so the portal can send OTP emails
without dragging in the screening stack.
"""

import os
import smtplib
from email.message import EmailMessage

import requests
from dotenv import load_dotenv

load_dotenv()

EMAIL_MODE = os.getenv("EMAIL_MODE", "smtp").lower()

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)
FROM_NAME = os.getenv("FROM_NAME", "ATS Recruiting")

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def smtp_configured() -> bool:
    """True when the active transport has everything it needs to send."""
    if EMAIL_MODE == "brevo":
        return bool(BREVO_API_KEY and FROM_EMAIL)
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def send_email(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    """Send one plain-text email via the configured transport."""
    if not to_addr or "@" not in to_addr:
        return False, f"Invalid recipient address: {to_addr!r}"
    if not smtp_configured():
        return False, ("Email not configured — set EMAIL_MODE plus "
                       "BREVO_API_KEY or SMTP_* in .env.")
    if EMAIL_MODE == "brevo":
        return _send_brevo(to_addr, subject, body)
    return _send_smtp(to_addr, subject, body)


def _send_brevo(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    try:
        r = requests.post(
            _BREVO_URL,
            headers={"api-key": BREVO_API_KEY,
                     "content-type": "application/json"},
            json={
                "sender": {"email": FROM_EMAIL, "name": FROM_NAME},
                "to": [{"email": to_addr}],
                "subject": subject,
                "textContent": body,
            },
            timeout=30,
        )
        if r.status_code in (200, 201, 202):
            return True, f"Sent to {to_addr}"
        try:
            detail = r.json().get("message", r.text[:200])
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        return False, f"Brevo rejected the send ({r.status_code}): {detail}"
    except requests.RequestException as e:
        return False, f"Send failed for {to_addr}: {e}"


def _send_smtp(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True, f"Sent to {to_addr}"
    except Exception as e:  # noqa: BLE001 - report, don't crash the caller
        return False, f"Send failed for {to_addr}: {e}"
