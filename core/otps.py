"""Purpose- and resource-scoped one-time verification codes."""

from datetime import datetime, timedelta

from sqlalchemy import select

from core import security
from core.models import Otp

ASSESSMENT = "assessment"
INTERVIEW = "interview"
PASSWORD_RESET = "password_reset"


def recent_count(db, email: str, purpose: str, resource_id: str,
                 window_minutes: int) -> int:
    since = datetime.utcnow() - timedelta(minutes=window_minutes)
    return len(db.execute(select(Otp).where(
        Otp.email == email,
        Otp.purpose == purpose,
        Otp.resource_id == resource_id,
        Otp.created_at > since,
    )).scalars().all())


def issue(db, email: str, purpose: str, resource_id: str) -> str:
    """Invalidate older codes for the same action and issue a fresh code."""
    old = db.execute(select(Otp).where(
        Otp.email == email,
        Otp.purpose == purpose,
        Otp.resource_id == resource_id,
        Otp.used == False,  # noqa: E712 - SQL Server BIT compatibility
    )).scalars().all()
    for row in old:
        row.used = True

    code = security.generate_otp()
    db.add(Otp(
        email=email,
        purpose=purpose,
        resource_id=resource_id,
        code_hash=security.hash_otp(code, email),
        expires_at=security.otp_expiry(),
    ))
    return code


def verify(db, email: str, code: str, purpose: str,
           resource_id: str) -> tuple[bool, str]:
    """Return (ok, reason) and consume an exact-purpose OTP on success."""
    otp = db.execute(select(Otp).where(
        Otp.email == email,
        Otp.purpose == purpose,
        Otp.resource_id == resource_id,
        Otp.used == False,  # noqa: E712
    ).order_by(Otp.id.desc())).scalars().first()
    if not otp or datetime.utcnow() > otp.expires_at:
        return False, "expired"
    if otp.attempts >= security.OTP_MAX_ATTEMPTS:
        return False, "attempts"
    otp.attempts += 1
    if security.hash_otp(code.strip(), email) != otp.code_hash:
        return False, "incorrect"
    otp.used = True
    return True, "ok"

