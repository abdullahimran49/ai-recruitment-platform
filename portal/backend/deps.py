"""Shared FastAPI dependencies: DB session and JWT auth guards."""

from fastapi import Depends, Header, HTTPException

from core import db as core_db
from core import security
from core.models import Applicant, User


def get_db():
    if core_db.SessionLocal is None:
        raise HTTPException(503, "Database not configured (set DB_PROVIDER + "
                                 "connection settings in .env)")
    s = core_db.SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _bearer(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    payload = security.read_token(authorization.split(" ", 1)[1])
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    return payload


def current_admin(authorization: str | None = Header(default=None),
                  db=Depends(get_db)) -> User:
    payload = _bearer(authorization)
    if payload.get("kind") != "admin":
        raise HTTPException(403, "Admin token required")
    user = db.get(User, payload.get("sub"))
    if not user:
        raise HTTPException(401, "Unknown user")
    return user


def super_admin(user: User = Depends(current_admin)) -> User:
    if user.role != "super_admin":
        raise HTTPException(403, "Super admin only")
    return user


def candidate_assignment_id(
        authorization: str | None = Header(default=None)) -> str:
    """Returns the assignment uuid the candidate token is scoped to."""
    payload = _bearer(authorization)
    if payload.get("kind") != "candidate":
        raise HTTPException(403, "Candidate token required")
    return payload["sub"]


def current_applicant(authorization: str | None = Header(default=None),
                      db=Depends(get_db)) -> Applicant:
    """The public job-portal applicant behind an applicant JWT."""
    payload = _bearer(authorization)
    if payload.get("kind") != "applicant":
        raise HTTPException(403, "Applicant token required")
    applicant = db.get(Applicant, payload.get("sub"))
    if not applicant:
        raise HTTPException(401, "Unknown applicant")
    return applicant
