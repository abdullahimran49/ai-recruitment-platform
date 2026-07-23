"""Automated AI voice interview (candidate-facing).

Flow: unique link -> OTP (same identity model as tests) -> proctoring check
-> window-gated join -> the candidate enters a LiveKit room (room name =
ai_interviews.uuid) where a voice agent (interview_agent.py) conducts the
interview; an admin can join the same room to watch and speak. The agent
owns the transcript, the post-interview evaluation, and the completion
email — this router only mints room tokens and handles proctoring
termination.

Proctoring mirrors the MCQ test (warnings, evidence snapshots, termination)
except voice detection — the candidate is supposed to talk.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from livekit import api as lk_api
from pydantic import BaseModel, Field
from sqlalchemy import select

from dotenv import load_dotenv
load_dotenv()

# The shared strict evaluator lives at the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from interview_eval import evaluate_transcript  # noqa: E402

from core import cv_brief, mailer, security  # noqa: E402
from core.models import AIInterview, InterviewEvent, Otp  # noqa: E402
from portal.backend.deps import candidate_assignment_id, get_db  # noqa: E402
from portal.backend.routers.candidate import (  # noqa: E402
    OTP_RATE_LIMIT,
    OTP_RATE_WINDOW_MIN,
    PROCTOR_EVENT_TYPES,
    PROCTOR_INFO_TYPES,
    _EVIDENCE_MAX_CHARS,
)

# During a SPOKEN interview the candidate naturally looks away and down to
# think while talking, so eye-gaze and head-pose are NOT reliable cheating
# signals here — they are logged for the recruiter but never count as warnings
# or terminate the interview. Hard integrity signals (no face, a second
# person, tab/app switching, screen-share stopped, etc.) still terminate.
INTERVIEW_INFO_TYPES = PROCTOR_INFO_TYPES | {"gaze_away", "head_turn_away"}

router = APIRouter(prefix="/api/portal/interview", tags=["ai-interview"])

EARLY_JOIN_MINUTES = 10      # can enter the room this early
LATE_JOIN_MINUTES = 30       # after this, the interview is missed
FINISH_GRACE_SECONDS = 60

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")

class EmailIn(BaseModel):
    email: str


class VerifyIn(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)




class FinishIn(BaseModel):
    terminated_reason: str | None = Field(default=None, max_length=200)


class ProctorEventIn(BaseModel):
    event_type: str
    detail: str = Field(default="", max_length=400)
    evidence: str | None = None


def _get(db, token: str) -> AIInterview:
    iv = db.get(AIInterview, token)
    if not iv:
        raise HTTPException(404, "This interview link is not valid.")
    finalize_if_expired(iv, db)
    return iv


def _window_state(iv: AIInterview) -> str:
    now = datetime.utcnow()
    if iv.status in ("completed", "terminated", "cancelled", "missed"):
        return iv.status
    if iv.status == "started":
        return "started"
    if now < iv.scheduled_at - timedelta(minutes=EARLY_JOIN_MINUTES):
        return "too_early"
    if now > iv.scheduled_at + timedelta(minutes=LATE_JOIN_MINUTES):
        return "missed"
    return "open"


def _check_joinable(iv: AIInterview):
    state = _window_state(iv)
    if state in ("open", "started"):
        return
    msgs = {
        "too_early": "This interview has not opened yet.",
        "missed": "The join window for this interview has passed.",
        "completed": "This interview has already been completed.",
        "terminated": "This interview was terminated due to proctoring "
                      "violations.",
        "cancelled": "This interview was cancelled.",
    }
    raise HTTPException(410, msgs.get(state, "Interview unavailable."))


def _deadline(iv: AIInterview) -> datetime:
    return (iv.started_at + timedelta(minutes=iv.duration_minutes)
            + timedelta(seconds=FINISH_GRACE_SECONDS))


def finalize_if_expired(iv: AIInterview, db) -> bool:
    """Lazily evaluate + close interviews whose time ran out, and mark
    no-shows as missed."""
    now = datetime.utcnow()
    if iv.status == "scheduled" and now > iv.scheduled_at + timedelta(
            minutes=LATE_JOIN_MINUTES):
        iv.status = "missed"
        return True
    if iv.status == "started" and iv.started_at and now > _deadline(iv):
        _evaluate(iv)
        iv.status = "completed"
        iv.completed_at = _deadline(iv)
        db.add(InterviewEvent(
            interview_uuid=iv.uuid, event_type="auto_submitted",
            is_warning=False,
            detail="Interview auto-closed at the time limit."))
        return True
    return False


def _jd_snippet(iv: AIInterview, limit: int = 1500) -> str:
    jd = (iv.job.jd_text or "").strip()
    return jd[:limit]


# ---- LiveKit room helpers ---------------------------------------------------------

def _room_metadata(iv: AIInterview, remaining: int) -> str:
    """Context the voice agent reads when it is dispatched to the room.

    Includes a brief on the candidate's own CV so the interviewer probes what
    they actually claim to have done rather than asking the same generic
    JD questions of everyone. Built server-side and attached to the ROOM — it
    must never go anywhere near the candidate's JWT.
    """
    return json.dumps({
        "interview_uuid": iv.uuid,
        "candidate_name": iv.candidate.name or "Candidate",
        "candidate_identity": f"candidate_{iv.uuid[:8]}",
        "job_title": iv.job.title,
        "jd_snippet": _jd_snippet(iv),
        "cv_brief": cv_brief.build(iv.candidate),
        "focus": iv.focus or "",
        "num_questions": iv.num_questions,
        "duration_minutes": iv.duration_minutes,
        "remaining_seconds": remaining,
    })


async def _ensure_room(iv: AIInterview, remaining: int) -> None:
    """Create the interview room with agent metadata (idempotent: an existing
    room is returned unchanged). Server-side so the metadata never appears in
    the candidate's JWT."""
    lkapi = lk_api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        await lkapi.room.create_room(lk_api.CreateRoomRequest(
            name=iv.uuid,
            metadata=_room_metadata(iv, remaining),
            empty_timeout=600,
            # Must outlive the agent's 60s rejoin grace: the server counts
            # only non-agent participants, so a candidate refresh would
            # otherwise close the room (default 20s) and kill the agent job.
            departure_timeout=90,
            max_participants=6,
        ))
    finally:
        await lkapi.aclose()


async def _delete_room(room_name: str) -> None:
    """Kick everyone (candidate, admin, agent) out of the room."""
    lkapi = lk_api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        await lkapi.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
    except Exception:  # noqa: BLE001 - room may already be gone
        pass
    finally:
        await lkapi.aclose()


def _evaluate(iv: AIInterview):
    """Post-interview evaluation via the shared strict evaluator; never
    raises (stores a fallback instead)."""
    result = evaluate_transcript(
        iv.job.title, _jd_snippet(iv), iv.transcript,
        num_questions=iv.num_questions or 5,
        terminated=(iv.status == "terminated"))
    iv.ai_score = result["score"]
    iv.ai_summary = {k: result[k] for k in
                     ("summary", "strengths", "concerns", "per_question")}


# ---- endpoints -----------------------------------------------------------------

@router.get("/{token}/info")
def info(token: str, db=Depends(get_db)):
    iv = _get(db, token)
    state = _window_state(iv)
    return {
        "job_title": iv.job.title,
        "candidate_name": iv.candidate.name,
        "scheduled_at": iv.scheduled_at.isoformat() + "Z",
        "duration_minutes": iv.duration_minutes,
        "status": iv.status,
        "window": state,
        "opens_at": (iv.scheduled_at - timedelta(minutes=EARLY_JOIN_MINUTES)
                     ).isoformat() + "Z",
        "max_warnings": iv.max_warnings,
    }


@router.post("/{token}/request-otp")
def request_otp(token: str, body: EmailIn, db=Depends(get_db)):
    iv = _get(db, token)
    _check_joinable(iv)
    email = body.email.strip().lower()
    if email != (iv.candidate.email or "").strip().lower():
        raise HTTPException(403, "This email does not match the invitation.")
    window_start = datetime.utcnow() - timedelta(minutes=OTP_RATE_WINDOW_MIN)
    recent = db.execute(select(Otp).where(
        Otp.email == email, Otp.created_at > window_start)).scalars().all()
    if len(recent) >= OTP_RATE_LIMIT:
        raise HTTPException(429, "Too many codes requested — use the most "
                                 "recent code or wait a few minutes.")
    code = security.generate_otp()
    db.add(Otp(email=email, code_hash=security.hash_otp(code, email),
               expires_at=security.otp_expiry()))
    ok, msg = mailer.send_email(
        email, "Your interview verification code",
        f"Hi {iv.candidate.name or 'there'},\n\nYour one-time code for the "
        f"{iv.job.title} interview is:\n\n    {code}\n\n"
        f"It expires in {security.OTP_TTL_MINUTES} minutes.\n")
    if not ok:
        raise HTTPException(502, f"Could not send the code: {msg}")
    return {"sent": True}


@router.post("/{token}/verify-otp")
def verify_otp(token: str, body: VerifyIn, db=Depends(get_db)):
    iv = _get(db, token)
    _check_joinable(iv)
    email = body.email.strip().lower()
    if email != (iv.candidate.email or "").strip().lower():
        raise HTTPException(403, "This email does not match the invitation.")
    otp = db.execute(
        select(Otp).where(Otp.email == email, Otp.used == False)  # noqa: E712
        .order_by(Otp.id.desc())).scalars().first()
    if not otp or datetime.utcnow() > otp.expires_at:
        raise HTTPException(401, "Code expired — request a new one.")
    if otp.attempts >= security.OTP_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many attempts — request a new code.")
    otp.attempts += 1
    if security.hash_otp(body.code.strip(), email) != otp.code_hash:
        raise HTTPException(401, "Incorrect code.")
    otp.used = True
    return {"token": security.candidate_token(iv.uuid)}


@router.post("/{token}/join")
async def join_room(token: str, db=Depends(get_db),
                    auth: str = Depends(candidate_assignment_id)):
    """Candidate joins the LiveKit interview room.

    Creates the room (with metadata containing the interview context) if it
    doesn't exist yet, transitions the interview to 'started', and returns a
    LiveKit participant token so the frontend can connect.
    """
    if auth != token:
        raise HTTPException(403, "Token does not match this interview.")
    iv = _get(db, token)
    _check_joinable(iv)

    # Transition to started on first join
    if iv.status == "scheduled":
        iv.status = "started"
        iv.started_at = datetime.utcnow()
        iv.transcript = []
    iv.last_seen = datetime.utcnow()

    remaining = max(0, int(
        (iv.started_at + timedelta(minutes=iv.duration_minutes)
         - datetime.utcnow()).total_seconds()))

    await _ensure_room(iv, remaining)

    at = (
        lk_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(f"candidate_{iv.uuid[:8]}")
        .with_name(iv.candidate.name or "Candidate")
        .with_metadata(json.dumps({"role": "candidate"}))
        .with_grants(lk_api.VideoGrants(
            room_join=True,
            room=iv.uuid,
            can_publish=True,
            can_subscribe=True,
        ))
    )

    return {
        "livekit_url": LIVEKIT_URL,
        "livekit_token": at.to_jwt(),
        "remaining_seconds": remaining,
        "warnings": iv.proctor_warnings or 0,
        "max_warnings": iv.max_warnings,
    }


@router.post("/{token}/finish")
async def finish(token: str, body: FinishIn, db=Depends(get_db),
                 auth: str = Depends(candidate_assignment_id)):
    """Proctoring termination ONLY. Normal completion is owned by the voice
    agent (it saves the transcript, evaluates, and emails when the room
    closes); a voluntary early exit is just a room disconnect."""
    if auth != token:
        raise HTTPException(403, "Token does not match this interview.")
    iv = _get(db, token)
    if iv.status not in ("started",):
        raise HTTPException(410, "The interview is not in progress.")
    if not body.terminated_reason:
        return {"finished": False, "note": "the interview agent finalizes "
                                           "non-terminated interviews"}

    iv.status = "terminated"
    iv.completed_at = datetime.utcnow()
    iv.terminated_reason = body.terminated_reason
    iv.candidate.status = "interview_terminated"
    candidate_email = iv.candidate.email
    candidate_name = iv.candidate.name
    job_title = iv.job.title
    # Commit BEFORE deleting the room: the agent finalizes on disconnect and
    # must see status=terminated so it keeps it (and skips its own email).
    db.commit()
    await _delete_room(iv.uuid)
    try:
        mailer.send_email(
            candidate_email, f"Interview ended — {job_title}",
            f"Hi {candidate_name or 'there'},\n\nYour interview for "
            f"the {job_title} role was ended automatically due to "
            f"repeated proctoring violations "
            f"({body.terminated_reason}).\n\nThe recruiting team will "
            "review the session.\n")
    except Exception:  # noqa: BLE001
        pass
    return {"finished": True, "terminated": True}


@router.post("/{token}/proctor-event")
def proctor_event(token: str, body: ProctorEventIn, db=Depends(get_db),
                  auth: str = Depends(candidate_assignment_id)):
    if auth != token:
        raise HTTPException(403, "Token does not match this interview.")
    iv = _get(db, token)
    if iv.status not in ("scheduled", "started"):
        raise HTTPException(410, "Interview is not active.")
    if body.event_type not in PROCTOR_EVENT_TYPES:
        raise HTTPException(422, f"Unknown event_type '{body.event_type}'.")
    evidence = body.evidence
    if evidence and len(evidence) > _EVIDENCE_MAX_CHARS:
        evidence = None
    # Gaze/head-pose are informational during a spoken interview (see
    # INTERVIEW_INFO_TYPES) — logged, but never a warning and never terminate.
    is_warning = body.event_type not in INTERVIEW_INFO_TYPES
    db.add(InterviewEvent(
        interview_uuid=iv.uuid, event_type=body.event_type,
        detail=body.detail, evidence=evidence, is_warning=is_warning))
    if is_warning:
        iv.proctor_warnings = (iv.proctor_warnings or 0) + 1
    iv.last_seen = datetime.utcnow()
    return {
        "warnings": iv.proctor_warnings or 0,
        "max_warnings": iv.max_warnings,
        "terminate": (iv.proctor_warnings or 0) >= iv.max_warnings,
    }
