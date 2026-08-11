"""Public job-portal API: browse jobs, register/login as an applicant, apply
(with resume + CNIC), and track application status.

This is the candidate-facing front door. Everything it writes lands in the same
``ats_screener`` database the recruiter (Streamlit) app and the admin dashboard
read, so an application is visible to HR the moment it is submitted — that
shared DB is the "sync across all portals".

Applicant identity is keyed by CNIC (unique): one human, one account, applying
to many jobs. Passwords are bcrypt-hashed; sessions are applicant JWTs.
"""

from __future__ import annotations

import os
import re
import uuid as uuidlib
from datetime import datetime, timedelta

from fastapi import (APIRouter, Depends, File, Form, HTTPException, UploadFile)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import pdf
from core import mailer, otps, security
from core.models import (AIInterview, Applicant, Candidate, Job,
                         PipelineStage, TestAssignment)
from core.screening import screen_resume_for_job
from portal.backend.deps import current_applicant, get_db

router = APIRouter(prefix="/api/portal", tags=["portal"])

_PORTAL_BASE = os.getenv("PORTAL_BASE_URL", "http://localhost:3000").rstrip("/")
# Optional: a single HR inbox/distribution address that gets a heads-up whenever
# a new application arrives. Unset -> no HR email is sent (keeps tests quiet and
# avoids blasting every admin). Set it in .env to switch notifications on.
_HR_NOTIFY = os.getenv("HR_NOTIFY_EMAIL", "").strip()
_RESUME_DIR = os.getenv(
    "RESUME_UPLOAD_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "uploads",
                 "resumes"))
_MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB


# ---- helpers -----------------------------------------------------------------

def _norm_cnic(raw: str) -> str:
    """Digits-only CNIC. Pakistani CNIC is 13 digits (xxxxx-xxxxxxx-x)."""
    return re.sub(r"\D", "", raw or "")


def _job_is_open(job: Job) -> bool:
    if not job.is_published:
        return False
    if job.application_deadline and job.application_deadline < datetime.utcnow():
        return False
    return True


def _applied_stage_id(db) -> int | None:
    st = db.execute(select(PipelineStage).where(
        PipelineStage.department_id.is_(None),
        PipelineStage.name == "Applied")).scalars().first()
    return st.id if st else None


def _job_public(job: Job, *, detail: bool = False) -> dict:
    out = {
        "uuid": job.uuid,
        "title": job.title,
        "location": job.location,
        "employment_type": job.employment_type,
        "openings": job.openings,
        "department": job.department.name if job.department else "",
        "application_deadline": (job.application_deadline.isoformat()
                                 if job.application_deadline else None),
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
    if detail:
        out["jd_text"] = job.jd_text
    return out


# ---- job listing -------------------------------------------------------------

@router.get("/jobs")
def list_jobs(db=Depends(get_db)):
    """All published jobs still inside their application window."""
    # == True, not .is_(True): SQLAlchemy renders is_() as "is_published IS 1"
    # which SQL Server rejects (see candidate.py / core.bank.py).
    jobs = db.execute(select(Job).where(Job.is_published == True)  # noqa: E712
                      ).scalars().all()
    now = datetime.utcnow()
    open_jobs = [j for j in jobs
                 if not (j.application_deadline and j.application_deadline < now)]
    open_jobs.sort(key=lambda j: j.created_at or now, reverse=True)
    return {"jobs": [_job_public(j) for j in open_jobs]}


@router.get("/jobs/{job_uuid}")
def job_detail(job_uuid: str, db=Depends(get_db)):
    job = db.get(Job, job_uuid)
    if not job or not job.is_published:
        raise HTTPException(404, "Job not found")
    data = _job_public(job, detail=True)
    data["is_open"] = _job_is_open(job)
    return data


# ---- auth --------------------------------------------------------------------

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Plain str, not EmailStr: the rest of the system stores emails as plain
    # strings and the test suite uses @ats.local, which email-validator rejects
    # as a reserved special-use domain. We do a light sanity check instead.
    email: str = Field(min_length=3, max_length=255)
    cnic: str = Field(min_length=13, max_length=25)
    phone: str = Field(default="", max_length=50)
    password: str = Field(min_length=8, max_length=200)


class LoginIn(BaseModel):
    email: str
    password: str


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _applicant_out(a: Applicant) -> dict:
    return {"uuid": a.uuid, "name": a.name, "email": a.email,
            "cnic": security.masked_cnic(a.cnic), "phone": a.phone}


@router.post("/register")
def register(body: RegisterIn, db=Depends(get_db)):
    cnic_digits = _norm_cnic(body.cnic)
    if len(cnic_digits) != 13:
        raise HTTPException(422, "CNIC must be 13 digits")
    cnic = security.protect_cnic(cnic_digits)
    email = body.email.strip().lower()
    if not _valid_email(email):
        raise HTTPException(422, "Enter a valid email address")

    # Friendly duplicate messages before hitting the unique constraint.
    if db.execute(select(Applicant).where(
            Applicant.cnic == cnic)).scalars().first():
        raise HTTPException(409, "An account with this CNIC already exists")
    if db.execute(select(Applicant).where(
            Applicant.email == email)).scalars().first():
        raise HTTPException(409, "An account with this email already exists")

    applicant = Applicant(
        cnic=cnic, email=email, name=body.name.strip(),
        phone=body.phone.strip(),
        password_hash=security.hash_password(body.password))
    db.add(applicant)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An account with this CNIC or email already "
                                 "exists")
    return {"token": security.applicant_token(applicant.uuid),
            "applicant": _applicant_out(applicant)}


@router.post("/login")
def login(body: LoginIn, db=Depends(get_db)):
    email = body.email.strip().lower()
    applicant = db.execute(select(Applicant).where(
        Applicant.email == email)).scalars().first()
    if not applicant or not security.verify_password(
            body.password, applicant.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return {"token": security.applicant_token(applicant.uuid),
            "applicant": _applicant_out(applicant)}


@router.get("/me")
def me(applicant: Applicant = Depends(current_applicant)):
    return _applicant_out(applicant)


# ---- apply -------------------------------------------------------------------

def _save_resume(applicant_uuid: str, filename: str, data: bytes) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or "resume.pdf"))
    folder = os.path.abspath(os.path.join(_RESUME_DIR, applicant_uuid))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{uuidlib.uuid4().hex}_{safe}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _screening_json(result) -> dict:
    """The screening payload shape the whole system expects (mirrors
    db_bridge.save_screening) — the AI interviewer reads `structured`."""
    return {
        "raw_score": result.raw_score,
        "must_have_gaps": result.must_have_gaps,
        "employment_gaps": result.employment_gaps,
        "gap_penalty": result.gap_penalty,
        "criteria": [cs.model_dump() for cs in result.criterion_scores],
        "structured": (result.structured.model_dump()
                       if result.structured else None),
        "screening_error": result.error or "",
    }


async def _read_pdf(resume: UploadFile) -> tuple[bytes, str]:
    """Validate + read an uploaded resume PDF, returning (bytes, extracted_text).
    Raises HTTPException on any problem so callers stay small."""
    data = await resume.read()
    if not data:
        raise HTTPException(422, "Empty resume file")
    if len(data) > _MAX_RESUME_BYTES:
        raise HTTPException(413, "Resume file too large (max 10 MB)")
    if not (resume.filename or "").lower().endswith(".pdf"):
        raise HTTPException(422, "Resume must be a PDF")
    try:
        text = pdf.extract_text(data)
    except Exception:  # noqa: BLE001 - a broken PDF must not 500
        raise HTTPException(422, "Could not read the PDF; please re-upload")
    return data, text


def _send_application_emails(cand: Candidate, job: Job) -> None:
    """Best-effort: confirm receipt to the candidate and (if configured) notify
    HR. Never raises — an application must succeed even if email fails."""
    try:
        mailer.send_email(
            cand.email,
            f"Application received - {job.title}",
            f"Dear {cand.name or 'Candidate'},\n\n"
            f"Thank you for applying for the {job.title} position. Your "
            f"application and resume have been received and shared with our "
            f"hiring team.\n\n"
            f"Track your application status - and any test or interview "
            f"invitations - any time here:\n{_PORTAL_BASE}/portal/dashboard\n\n"
            f"Best regards,\nThe Recruiting Team")
    except Exception:  # noqa: BLE001
        pass
    if _HR_NOTIFY:
        try:
            mailer.send_email(
                _HR_NOTIFY,
                f"New application: {job.title}",
                f"A new candidate has applied for {job.title}.\n\n"
                f"Name:  {cand.name}\n"
                f"Email: {cand.email}\n"
                f"Resume score: {round(cand.resume_score or 0)}/100\n\n"
                f"Review them in the admin dashboard / applications inbox.")
        except Exception:  # noqa: BLE001
            pass


@router.post("/jobs/{job_uuid}/apply")
async def apply(job_uuid: str,
                resume: UploadFile = File(...),
                phone: str = Form(default=""),
                applicant: Applicant = Depends(current_applicant),
                db=Depends(get_db)):
    job = db.get(Job, job_uuid)
    if not job or not job.is_published:
        raise HTTPException(404, "Job not found")
    if not _job_is_open(job):
        raise HTTPException(409, "Applications for this job are closed")

    # One application per person per job.
    if db.execute(select(Candidate).where(
            Candidate.job_uuid == job_uuid,
            Candidate.applicant_uuid == applicant.uuid)).scalars().first():
        raise HTTPException(409, "You have already applied to this job")

    data, resume_text = await _read_pdf(resume)
    resume_path = _save_resume(applicant.uuid, resume.filename, data)
    result = screen_resume_for_job(job, resume_text, resume.filename or "resume.pdf")

    if phone.strip():
        applicant.phone = phone.strip()

    cand = Candidate(
        job_uuid=job.uuid,
        applicant_uuid=applicant.uuid,
        name=applicant.name or result.candidate_name,
        email=applicant.email,
        phone=applicant.phone,
        resume_score=result.score,
        screening_json=_screening_json(result),
        status="screened",
        source="portal",
        resume_path=resume_path,
        stage_id=_applied_stage_id(db),
    )
    db.add(cand)
    db.flush()
    _send_application_emails(cand, job)
    return {"application_uuid": cand.uuid, "job_title": job.title,
            "resume_score": result.score,
            "screening_error": result.error or ""}


# ---- password reset (forgot password) ---------------------------------------

class ForgotIn(BaseModel):
    email: str


@router.post("/forgot-password")
def forgot_password(body: ForgotIn, db=Depends(get_db)):
    """Email a 6-digit reset code. Always returns the same response whether or
    not the account exists, so the endpoint can't be used to probe for emails."""
    email = body.email.strip().lower()
    applicant = db.execute(select(Applicant).where(
        Applicant.email == email)).scalars().first()
    if applicant:
        if otps.recent_count(db, email, otps.PASSWORD_RESET,
                             applicant.uuid, 10) < 5:
            code = otps.issue(db, email, otps.PASSWORD_RESET, applicant.uuid)
            try:
                mailer.send_email(
                    email, "Reset your password",
                    f"Hi {applicant.name or 'there'},\n\n"
                    f"Your password reset code is:\n\n    {code}\n\n"
                    f"It expires in {security.OTP_TTL_MINUTES} minutes. If you "
                    f"did not request this, you can ignore this email.\n")
            except Exception:  # noqa: BLE001
                pass
    return {"sent": True}


class ResetIn(BaseModel):
    email: str
    code: str
    new_password: str = Field(min_length=8, max_length=200)


@router.post("/reset-password")
def reset_password(body: ResetIn, db=Depends(get_db)):
    email = body.email.strip().lower()
    applicant = db.execute(select(Applicant).where(
        Applicant.email == email)).scalars().first()
    if not applicant:
        raise HTTPException(400, "Invalid or expired code — request a new one.")
    ok, reason = otps.verify(
        db, email, body.code, otps.PASSWORD_RESET, applicant.uuid)
    if reason == "attempts":
        raise HTTPException(429, "Too many attempts — request a new code.")
    if reason == "expired":
        raise HTTPException(400, "Invalid or expired code — request a new one.")
    if not ok:
        raise HTTPException(400, "Incorrect code.")
    applicant.password_hash = security.hash_password(body.new_password)
    # Sign them straight in so they don't have to log in again.
    return {"reset": True, "token": security.applicant_token(applicant.uuid),
            "applicant": _applicant_out(applicant)}


# ---- manage an application (resume update / withdraw) -----------------------

def _owned_application(db, applicant: Applicant, candidate_uuid: str) -> Candidate:
    cand = db.get(Candidate, candidate_uuid)
    if not cand or cand.applicant_uuid != applicant.uuid:
        raise HTTPException(404, "Application not found")
    return cand


def _has_live_assignment(cand: Candidate) -> bool:
    return any(a.superseded_at is None for a in cand.assignments)


@router.post("/applications/{candidate_uuid}/resume")
async def update_resume(candidate_uuid: str,
                        resume: UploadFile = File(...),
                        applicant: Applicant = Depends(current_applicant),
                        db=Depends(get_db)):
    """Replace the resume on an application and re-run screening. Only allowed
    before an assessment has been assigned (and not once withdrawn)."""
    cand = _owned_application(db, applicant, candidate_uuid)
    if cand.status == "withdrawn":
        raise HTTPException(409, "This application has been withdrawn.")
    if _has_live_assignment(cand):
        raise HTTPException(
            409, "Your resume is locked because an assessment has already been "
                 "assigned for this application.")
    job = db.get(Job, cand.job_uuid)
    data, resume_text = await _read_pdf(resume)
    resume_path = _save_resume(applicant.uuid, resume.filename, data)
    result = screen_resume_for_job(job, resume_text,
                                   resume.filename or "resume.pdf")
    cand.resume_path = resume_path
    cand.resume_score = result.score
    cand.screening_json = _screening_json(result)
    return {"application_uuid": cand.uuid, "resume_score": result.score,
            "screening_error": result.error or ""}


@router.post("/applications/{candidate_uuid}/withdraw")
def withdraw_application(candidate_uuid: str,
                        applicant: Applicant = Depends(current_applicant),
                        db=Depends(get_db)):
    cand = _owned_application(db, applicant, candidate_uuid)
    if cand.status == "withdrawn":
        return {"withdrawn": True}
    now = datetime.utcnow()
    # Revoke every pending downstream invitation. A withdrawn candidate must
    # not retain a working assessment or interview link.
    for assignment in cand.assignments:
        if (assignment.superseded_at is None
                and assignment.status not in ("submitted", "terminated")):
            assignment.superseded_at = now
            assignment.superseded_by = f"candidate:{applicant.uuid}"
            assignment.reset_reason = "Application withdrawn by candidate"
    for ai_interview in cand.interviews_ai:
        if ai_interview.status in ("scheduled", "started"):
            ai_interview.status = "cancelled"
            ai_interview.completed_at = now
            ai_interview.terminated_reason = "Application withdrawn"
    for human_interview in cand.interviews:
        if human_interview.status == "scheduled":
            human_interview.status = "cancelled"
    cand.status = "withdrawn"
    rejected = db.execute(select(PipelineStage).where(
        PipelineStage.department_id.is_(None),
        PipelineStage.name == "Rejected")).scalars().first()
    if rejected:
        cand.stage_id = rejected.id
    try:
        mailer.send_email(
            applicant.email, f"Application withdrawn — {cand.job.title}",
            f"Hi {applicant.name or 'there'},\n\nYour application for the "
            f"{cand.job.title} role has been withdrawn. Any pending assessment "
            "or interview invitation for this application has been cancelled.\n")
    except Exception:
        pass
    return {"withdrawn": True}


# ---- tracking ----------------------------------------------------------------

def _live_assignment(cand: Candidate):
    live = [a for a in cand.assignments if a.superseded_at is None]
    return live[0] if live else None


def _application_view(cand: Candidate) -> dict:
    stage = cand.stage.name if cand.stage else None

    test = None
    a = _live_assignment(cand)
    if a is not None:
        test = {
            "link": f"{_PORTAL_BASE}/test/{a.uuid}",
            "status": a.status,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "score": a.test_score,
        }

    interview = None
    ivs = sorted(cand.interviews_ai, key=lambda i: i.created_at or datetime.min)
    if ivs:
        active_ivs = [i for i in ivs if i.status != "cancelled"]
        iv = active_ivs[-1] if active_ivs else ivs[-1]
        interview = {
            "link": f"{_PORTAL_BASE}/interview/{iv.uuid}",
            "status": iv.status,
            "scheduled_at": iv.scheduled_at.isoformat() if iv.scheduled_at else None,
            "duration_minutes": iv.duration_minutes,
        }

    human_interview = None
    human_rows = sorted(cand.interviews,
                        key=lambda item: item.created_at or datetime.min)
    if human_rows:
        active_rows = [item for item in human_rows
                       if item.status != "cancelled"]
        row = active_rows[-1] if active_rows else human_rows[-1]
        human_interview = {
            "status": row.status,
            "type": row.interview_type,
            "scheduled_at": (row.scheduled_at.isoformat()
                             if row.scheduled_at else None),
            "duration_minutes": row.duration_minutes,
            "location": row.location or "",
        }

    withdrawn = cand.status == "withdrawn"
    has_live = a is not None
    return {
        "application_uuid": cand.uuid,
        "job_uuid": cand.job_uuid,
        "job_title": cand.job.title if cand.job else "",
        "applied_at": cand.created_at.isoformat() if cand.created_at else None,
        "status": cand.status,
        "stage": stage,
        "resume_score": cand.resume_score,
        "test": test,
        "interview": interview,
        "human_interview": human_interview,
        "withdrawn": withdrawn,
        # Resume can be swapped only before a test is assigned; withdraw is
        # available until the candidate leaves the pipeline.
        "can_update_resume": (not withdrawn and not has_live
                              and stage not in ("Hired", "Rejected")),
        "can_withdraw": not withdrawn and stage not in ("Hired", "Rejected"),
    }


@router.get("/me/applications")
def my_applications(applicant: Applicant = Depends(current_applicant),
                    db=Depends(get_db)):
    cands = db.execute(select(Candidate).where(
        Candidate.applicant_uuid == applicant.uuid)).scalars().all()
    cands.sort(key=lambda c: c.created_at or datetime.min, reverse=True)
    return {"applications": [_application_view(c) for c in cands]}
