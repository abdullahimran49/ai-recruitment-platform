"""Candidate-facing test flow: OTP login -> timed shuffled test -> submit.

Security model:
- The assignment uuid in the link is the first factor (unguessable uuid4).
- The candidate must also prove control of the invited email via OTP.
- correct_index never leaves the server; options are sent shuffled but each
  carries its original index, which reveals nothing about correctness.
- The timer is enforced server-side (deadline = started_at + duration + grace).
"""

import os
import random
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from core import mailer, otps, security
from core.auto_invite import maybe_auto_invite
from core.models import (
    Candidate,
    CandidateAnswer,
    ProctorEvent,
    Question,
    Test,
    TestAssignment,
)
from core.question_sets import questions_for
from portal.backend.deps import candidate_assignment_id, get_db

router = APIRouter(prefix="/api/portal", tags=["candidate"])

_PORTAL_BASE = os.getenv("PORTAL_BASE_URL", "http://localhost:3000").rstrip("/")

SUBMIT_GRACE_SECONDS = 30

# Proctoring: default violations before automatic termination (each test can
# override via tests.max_warnings) and the accepted client-reported event
# types (anything else is rejected).
PROCTOR_MAX_WARNINGS = 3
PROCTOR_EVENT_TYPES = {
    "no_face", "multiple_faces", "gaze_away", "head_turn_away",
    "voice_detected", "camera_off",
    "tab_switch", "window_blur", "fullscreen_exit", "screen_share_stopped",
    "copy_paste", "devtools_key", "multi_monitor", "check_passed",
    "periodic_snapshot",
}
# Informational events that never count toward termination.
PROCTOR_INFO_TYPES = {"check_passed", "multi_monitor", "periodic_snapshot"}
_EVIDENCE_MAX_CHARS = 150_000  # ~110 KB base64 JPEG

# OTP anti-abuse: max codes issued per email inside the window.
OTP_RATE_LIMIT = 3
OTP_RATE_WINDOW_MIN = 10


def _test_max_warnings(a: TestAssignment) -> int:
    return a.test.max_warnings or PROCTOR_MAX_WARNINGS


class EmailIn(BaseModel):
    email: str


class VerifyIn(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)


class AnswerIn(BaseModel):
    question_id: int
    selected_index: int = Field(ge=0, le=3)


class SubmitIn(BaseModel):
    answers: list[AnswerIn] = Field(default_factory=list)
    terminated_reason: str | None = Field(default=None, max_length=200)


class ProctorEventIn(BaseModel):
    event_type: str
    detail: str = Field(default="", max_length=400)
    evidence: str | None = None  # base64 data-url JPEG snapshot


class DraftIn(BaseModel):
    answers: list[AnswerIn] = Field(default_factory=list)


def _get_assignment(db, token: str) -> TestAssignment:
    a = db.get(TestAssignment, token)
    if not a:
        raise HTTPException(404, "This assessment link is not valid.")
    finalize_if_expired(a, db)
    return a


def _score_and_close(a: TestAssignment, db, chosen: dict[int, int],
                     status: str, completed_at: datetime,
                     reason: str | None = None) -> tuple[int, int]:
    """Persist one complete answer sheet and close an active assignment."""
    paper = questions_for(a)
    correct = 0
    for q in paper:
        selected = int(chosen.get(q.id, -1))
        is_correct = selected == q.correct_index
        correct += int(is_correct)
        db.add(CandidateAnswer(
            assignment_uuid=a.uuid, question_id=q.id,
            selected_index=selected, is_correct=is_correct))
    a.status = status
    a.submitted_at = completed_at
    a.test_score = round(100 * correct / max(1, len(paper)), 1)
    a.candidate.status = ("test_terminated" if status == "terminated"
                          else "tested")
    a.terminated_reason = reason if status == "terminated" else None
    return len(chosen), len(paper)


def _email_termination(a: TestAssignment, reason: str) -> None:
    try:
        mailer.send_email(
            a.candidate.email, f"Assessment ended — {a.test.job.title}",
            f"Hi {a.candidate.name or 'there'},\n\nYour assessment for the "
            f"{a.test.job.title} role was ended automatically because the "
            f"proctoring system recorded repeated violations ({reason}).\n\n"
            "The answers you provided up to that point were recorded. The "
            "recruiting team will review the session and be in touch.\n")
    except Exception:  # noqa: BLE001 - finalization must not depend on email
        pass


def finalize_if_expired(a: TestAssignment, db) -> bool:
    """Auto-submit a started assignment whose deadline (plus grace) passed.

    Scores from the crash-safe draft answers, so a candidate whose browser
    died mid-test still gets credit for everything they had selected. Runs
    lazily whenever the assignment is touched — no background job needed.
    Returns True if it finalized the assignment.
    """
    if a.status != "started" or not a.started_at:
        return False
    if a.superseded_at:
        # A replaced attempt is frozen as history: auto-submitting it would
        # manufacture a score for a test the candidate was pulled out of.
        return False
    deadline = (a.started_at + timedelta(minutes=a.test.duration_minutes)
                + timedelta(seconds=SUBMIT_GRACE_SECONDS))
    if datetime.utcnow() <= deadline:
        return False

    draft = a.draft_answers or {}
    chosen = {int(key): int(value) for key, value in draft.items()}
    _score_and_close(a, db, chosen, "submitted", deadline)
    db.add(ProctorEvent(
        assignment_uuid=a.uuid, event_type="auto_submitted", is_warning=False,
        detail="Auto-submitted at deadline from saved draft "
               f"({len(draft)} answered) — session ended without an "
               "explicit submit."))
    return True


def _check_open(a: TestAssignment):
    if a.superseded_at:
        raise HTTPException(
            410, "This link has been replaced by a newer one. Please use the "
                 "most recent assessment link sent to your email.")
    if a.status == "submitted":
        raise HTTPException(410, "This assessment has already been submitted.")
    if a.status == "terminated":
        raise HTTPException(
            410, "This assessment was terminated due to proctoring "
                 "violations and cannot be retaken.")
    if a.expires_at and datetime.utcnow() > a.expires_at and not a.started_at:
        raise HTTPException(410, "This assessment link has expired.")


def _check_duplicate(a: TestAssignment, db):
    """Reject if the candidate already completed a LIVE test for the same job.

    Superseded attempts are excluded deliberately: a reset keeps the old
    submitted attempt as history, and counting it here would make every
    replacement link 410 with "you have already completed an assessment" — the
    retake would be dead on arrival.
    """
    job_uuid = a.test.job_uuid
    existing = db.execute(
        select(TestAssignment)
        .join(Test, TestAssignment.test_uuid == Test.uuid)
        .where(
            Test.job_uuid == job_uuid,
            TestAssignment.candidate_uuid == a.candidate_uuid,
            TestAssignment.uuid != a.uuid,
            TestAssignment.superseded_at.is_(None),
            TestAssignment.status.in_(["submitted", "terminated"]),
        )
    ).scalars().first()
    if existing:
        raise HTTPException(
            410, "You have already completed an assessment for this position.")


@router.get("/assignment/{token}/info")
def assignment_info(token: str, db=Depends(get_db)):
    a = _get_assignment(db, token)
    return {
        "job_title": a.test.job.title,
        "candidate_name": a.candidate.name,
        "duration_minutes": a.test.duration_minutes,
        "num_questions": len(questions_for(a)),
        "status": a.status,
        "proctored": bool(a.test.proctored),
        "max_warnings": _test_max_warnings(a),
        "expired": bool(a.expires_at and datetime.utcnow() > a.expires_at
                        and not a.started_at),
    }


@router.post("/assignment/{token}/request-otp")
def request_otp(token: str, body: EmailIn, db=Depends(get_db)):
    a = _get_assignment(db, token)
    _check_open(a)
    email = body.email.strip().lower()
    if email != (a.candidate.email or "").strip().lower():
        raise HTTPException(403, "This email does not match the invitation.")

    # Rate limit: protects the candidate's inbox and the email quota.
    if otps.recent_count(db, email, otps.ASSESSMENT, a.uuid,
                         OTP_RATE_WINDOW_MIN) >= OTP_RATE_LIMIT:
        raise HTTPException(
            429, f"Too many codes requested — wait a few minutes and use "
                 f"the most recent code sent to {email}.")

    code = otps.issue(db, email, otps.ASSESSMENT, a.uuid)
    ok, msg = mailer.send_email(
        email,
        "Your assessment verification code",
        f"Hi {a.candidate.name or 'there'},\n\n"
        f"Your one-time code for the {a.test.job.title} assessment is:\n\n"
        f"    {code}\n\n"
        f"It expires in {security.OTP_TTL_MINUTES} minutes.\n")
    if not ok:
        raise HTTPException(502, f"Could not send the code: {msg}")
    return {"sent": True}


@router.post("/assignment/{token}/verify-otp")
def verify_otp(token: str, body: VerifyIn, db=Depends(get_db)):
    a = _get_assignment(db, token)
    _check_open(a)
    email = body.email.strip().lower()
    if email != (a.candidate.email or "").strip().lower():
        raise HTTPException(403, "This email does not match the invitation.")

    ok, reason = otps.verify(
        db, email, body.code, otps.ASSESSMENT, a.uuid)
    if reason == "expired":
        raise HTTPException(401, "Code expired — request a new one.")
    if reason == "attempts":
        raise HTTPException(429, "Too many attempts — request a new code.")
    if not ok:
        raise HTTPException(401, "Incorrect code.")
    return {"token": security.candidate_token(a.uuid)}


@router.get("/assignment/{token}/test")
def get_test(token: str, db=Depends(get_db),
             auth_assignment: str = Depends(candidate_assignment_id)):
    if auth_assignment != token:
        raise HTTPException(403, "Token does not match this assessment.")
    a = _get_assignment(db, token)
    if a.status in ("submitted", "terminated"):
        raise HTTPException(410, "Already completed.")
    _check_duplicate(a, db)

    now = datetime.utcnow()
    if not a.started_at:
        a.started_at = now
        a.status = "started"
    deadline = a.started_at + timedelta(minutes=a.test.duration_minutes)
    remaining = (deadline - now).total_seconds()
    if remaining <= -SUBMIT_GRACE_SECONDS:
        raise HTTPException(410, "Time is up for this assessment.")

    # The paper was drawn (and ordered) per candidate when the link was
    # created; only the options are shuffled here, deterministically, so a
    # reload never reorders the test under the candidate.
    rng = random.Random(token)
    payload = []
    for q in questions_for(a):
        opts = [{"idx": i, "text": t} for i, t in enumerate(q.options_json)]
        rng.shuffle(opts)
        payload.append({"id": q.id, "question": q.question, "options": opts})

    a.last_seen = now
    return {
        "job_title": a.test.job.title,
        "duration_minutes": a.test.duration_minutes,
        "remaining_seconds": max(0, int(remaining)),
        "proctored": bool(a.test.proctored),
        "max_warnings": _test_max_warnings(a),
        "warnings": a.proctor_warnings,
        # Crash-safe resume: previously autosaved selections.
        "draft": a.draft_answers or {},
        "questions": payload,
    }


@router.post("/assignment/{token}/draft")
def save_draft(token: str, body: DraftIn, db=Depends(get_db),
               auth_assignment: str = Depends(candidate_assignment_id)):
    """Autosave the candidate's current selections (also the heartbeat).

    Answers survive a browser crash; the deadline auto-finalize scores from
    this draft. Never fails the test on transient errors.
    """
    if auth_assignment != token:
        raise HTTPException(403, "Token does not match this assessment.")
    a = _get_assignment(db, token)
    if a.status in ("submitted", "terminated"):
        raise HTTPException(410, "Already completed.")
    a.draft_answers = {str(ans.question_id): ans.selected_index
                       for ans in body.answers}
    a.last_seen = datetime.utcnow()
    return {"saved": len(body.answers)}


@router.post("/assignment/{token}/proctor-event")
def proctor_event(token: str, body: ProctorEventIn, db=Depends(get_db),
                  auth_assignment: str = Depends(candidate_assignment_id)):
    """Log a proctoring observation from the candidate's browser.

    Warning-type events increment the assignment's warning count. The server
    immediately scores the saved draft and closes a proctored assignment when
    its configured limit is reached; the browser response is only a UI signal.
    """
    if auth_assignment != token:
        raise HTTPException(403, "Token does not match this assessment.")
    a = _get_assignment(db, token)
    if a.status in ("submitted", "terminated"):
        raise HTTPException(410, "Already completed.")
    if body.event_type not in PROCTOR_EVENT_TYPES:
        raise HTTPException(422, f"Unknown event_type '{body.event_type}'.")

    evidence = body.evidence
    if evidence and len(evidence) > _EVIDENCE_MAX_CHARS:
        evidence = None  # drop oversized snapshots rather than reject

    is_warning = body.event_type not in PROCTOR_INFO_TYPES
    db.add(ProctorEvent(
        assignment_uuid=a.uuid, event_type=body.event_type,
        detail=body.detail, evidence=evidence, is_warning=is_warning))
    if is_warning:
        a.proctor_warnings = (a.proctor_warnings or 0) + 1
    a.last_seen = datetime.utcnow()

    limit = _test_max_warnings(a)
    terminate = bool(a.test.proctored and (a.proctor_warnings or 0) >= limit)
    if terminate:
        reason = f"Reached the proctoring limit ({limit} warnings)"
        draft = {int(key): int(value)
                 for key, value in (a.draft_answers or {}).items()}
        _score_and_close(a, db, draft, "terminated", datetime.utcnow(), reason)
        _email_termination(a, reason)
    return {
        "warnings": a.proctor_warnings or 0,
        "max_warnings": limit,
        "terminate": terminate,
    }


@router.post("/assignment/{token}/submit")
def submit(token: str, body: SubmitIn, db=Depends(get_db),
           auth_assignment: str = Depends(candidate_assignment_id)):
    if auth_assignment != token:
        raise HTTPException(403, "Token does not match this assessment.")
    a = _get_assignment(db, token)
    if a.status in ("submitted", "terminated"):
        raise HTTPException(410, "Already completed.")
    if not a.started_at:
        raise HTTPException(400, "Test was never started.")

    now = datetime.utcnow()
    deadline = (a.started_at + timedelta(minutes=a.test.duration_minutes)
                + timedelta(seconds=SUBMIT_GRACE_SECONDS))
    if now > deadline:
        raise HTTPException(410, "Time is up — submission window closed.")

    chosen = {ans.question_id: ans.selected_index for ans in body.answers}
    if not chosen and a.draft_answers:
        # Crash/termination path: fall back to the autosaved draft.
        chosen = {int(k): int(v) for k, v in a.draft_answers.items()}
    terminated = bool(body.terminated_reason)
    answered, total = _score_and_close(
        a, db, chosen, "terminated" if terminated else "submitted", now,
        body.terminated_reason)

    # Confirmation email (non-fatal — never block a valid submission on email).
    # Score is intentionally NOT included; the recruiter decides next steps.
    taken_secs = int((now - a.started_at).total_seconds())
    if taken_secs < 60:
        taken_str = f"{taken_secs} seconds"
    elif taken_secs < 3600:
        mins = taken_secs // 60
        secs = taken_secs % 60
        taken_str = f"{mins} minute{'s' if mins != 1 else ''}"
        if secs:
            taken_str += f" {secs} second{'s' if secs != 1 else ''}"
    else:
        taken_str = f"{taken_secs // 3600}h {(taken_secs % 3600) // 60}m"
    try:
        if terminated:
            _email_termination(a, body.terminated_reason or "policy violation")
        else:
            mailer.send_email(
                a.candidate.email,
                f"Assessment received — {a.test.job.title}",
                f"Hi {a.candidate.name or 'there'},\n\n"
                f"We've received your completed assessment for the "
                f"{a.test.job.title} role. You answered {answered} of "
                f"{total} questions in {taken_str}.\n\n"
                "Thank you for taking the time. The recruiting team will "
                "review your results and be in touch about next steps.\n")
    except Exception:  # noqa: BLE001 - submission already succeeded
        pass

    # Auto-invite to the AI interview if they passed and the job opts in.
    # Never raises — a submission that succeeded stays succeeded.
    auto = {"invited": False, "reason": "not evaluated"}
    if not terminated:
        auto = maybe_auto_invite(db, a, _PORTAL_BASE)

    return {"submitted": True, "terminated": terminated,
            "answered": answered, "total_questions": total,
            "auto_invited": bool(auto.get("invited"))}
