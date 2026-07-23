"""Password hashing, JWT session tokens, and OTP generation/verification."""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

JWT_SECRET = os.getenv("JWT_SECRET", "")
if not JWT_SECRET:
    # Random per-process secret. Fine for dev; set JWT_SECRET in .env for
    # anything real (otherwise every restart logs everyone out).
    JWT_SECRET = secrets.token_hex(32)

JWT_ALGO = "HS256"
ADMIN_TOKEN_HOURS = 12
CANDIDATE_TOKEN_HOURS = 4
APPLICANT_TOKEN_HOURS = 24 * 14  # portal applicants stay signed in ~2 weeks

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


# ---- Passwords (admins) ------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


# ---- JWT ----------------------------------------------------------------------

def make_token(payload: dict, hours: int) -> str:
    body = dict(payload)
    body["exp"] = datetime.now(timezone.utc) + timedelta(hours=hours)
    return jwt.encode(body, JWT_SECRET, algorithm=JWT_ALGO)


def read_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        return None


def admin_token(user_uuid: str, role: str, department_id: int | None) -> str:
    return make_token(
        {"sub": user_uuid, "kind": "admin", "role": role, "dept": department_id},
        ADMIN_TOKEN_HOURS)


def candidate_token(assignment_uuid: str) -> str:
    return make_token(
        {"sub": assignment_uuid, "kind": "candidate"}, CANDIDATE_TOKEN_HOURS)


def applicant_token(applicant_uuid: str) -> str:
    """Session token for a public job-portal applicant (browse/apply/track)."""
    return make_token(
        {"sub": applicant_uuid, "kind": "applicant"}, APPLICANT_TOKEN_HOURS)


# ---- OTP ------------------------------------------------------------------------

def generate_otp() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def hash_otp(code: str, email: str) -> str:
    # HMAC so a leaked DB row can't be reversed to the 6-digit code offline
    # without also knowing the server secret.
    return hmac.new(JWT_SECRET.encode(),
                    f"{email.lower()}:{code}".encode(),
                    hashlib.sha256).hexdigest()


def otp_expiry() -> datetime:
    return datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)
