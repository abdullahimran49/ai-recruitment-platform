from datetime import datetime, timedelta, timezone
import json
import os
import re

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from livekit import api as lk_api
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select

from core import attempts, bank, mailer, security, templates
import os

from core.models import (
    AIInterview,
    Applicant,
    AssignmentQuestion,
    Candidate,
    CandidateAnswer,
    Department,
    EmailTemplate,
    Interview,
    InterviewEvent,
    Job,
    PipelineStage,
    ProctorEvent,
    Question,
    QuestionBankCategory,
    QuestionBankItem,
    Scorecard,
    Test,
    TestAssignment,
    User,
)
from core.job_delete import cascade_delete_job
from core.question_sets import assign_questions, questions_for

_PORTAL_BASE = os.getenv("PORTAL_BASE_URL", "http://localhost:3000").rstrip("/")
from portal.backend.deps import current_admin, get_db, super_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---- Auth ---------------------------------------------------------------------

# Note: plain str + "@" check, not EmailStr — internal domains like
# superadmin@ats.local are valid identifiers here, and EmailStr rejects
# reserved TLDs.
class _EmailField(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _basic_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or len(v) < 5:
            raise ValueError("invalid email address")
        return v


class LoginIn(_EmailField):
    password: str


@router.post("/login")
def login(body: LoginIn, db=Depends(get_db)):
    user = db.execute(select(User).where(
        User.email == body.email.lower())).scalar_one_or_none()
    if not user or not security.verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password.")
    return {
        "token": security.admin_token(user.uuid, user.role, user.department_id),
        "name": user.name,
        "role": user.role,
        "department": user.department.name if user.department else None,
    }


@router.get("/me")
def me(user: User = Depends(current_admin)):
    return {"name": user.name, "email": user.email, "role": user.role,
            "department": user.department.name if user.department else None,
            "department_id": user.department_id}


# ---- Departments ----------------------------------------------------------------

class DeptIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)


@router.get("/departments")
def list_departments(user: User = Depends(current_admin), db=Depends(get_db)):
    q = select(Department)
    if user.role != "super_admin":
        q = q.where(Department.id == user.department_id)
    return [{"id": d.id, "name": d.name}
            for d in db.execute(q).scalars().all()]


@router.post("/departments")
def create_department(body: DeptIn, _: User = Depends(super_admin),
                      db=Depends(get_db)):
    if db.execute(select(Department).where(
            Department.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, "Department already exists.")
    d = Department(name=body.name)
    db.add(d)
    db.flush()
    return {"id": d.id, "name": d.name}


@router.delete("/departments/{dept_id}")
def delete_department(dept_id: int, _: User = Depends(super_admin),
                      db=Depends(get_db)):
    d = db.get(Department, dept_id)
    if not d:
        raise HTTPException(404, "No such department.")
    in_use = db.execute(select(func.count(Job.uuid)).where(
        Job.department_id == dept_id)).scalar()
    if in_use:
        raise HTTPException(409, f"Department has {in_use} job(s); move or "
                                 "delete them first.")
    db.delete(d)
    return {"deleted": True}


# ---- Admin users (super admin CRUD) ---------------------------------------------

class AdminIn(_EmailField):
    name: str
    password: str = Field(min_length=8)
    department_id: int | None = None
    role: str = "admin"  # admin | super_admin


@router.get("/users")
def list_admins(_: User = Depends(super_admin), db=Depends(get_db)):
    users = db.execute(select(User)).scalars().all()
    return [{"uuid": u.uuid, "name": u.name, "email": u.email, "role": u.role,
             "department": u.department.name if u.department else None}
            for u in users]


@router.post("/users")
def create_admin(body: AdminIn, _: User = Depends(super_admin),
                 db=Depends(get_db)):
    if body.role not in ("admin", "super_admin"):
        raise HTTPException(422, "role must be admin or super_admin")
    if body.role == "admin" and body.department_id is None:
        raise HTTPException(422, "A normal admin needs a department_id.")
    if db.execute(select(User).where(
            User.email == body.email.lower())).scalar_one_or_none():
        raise HTTPException(409, "A user with that email already exists.")
    u = User(name=body.name, email=body.email.lower(),
             password_hash=security.hash_password(body.password),
             role=body.role, department_id=body.department_id)
    db.add(u)
    db.flush()
    return {"uuid": u.uuid, "email": u.email}


@router.delete("/users/{user_uuid}")
def delete_admin(user_uuid: str, me_user: User = Depends(super_admin),
                 db=Depends(get_db)):
    if user_uuid == me_user.uuid:
        raise HTTPException(409, "You cannot delete your own account.")
    u = db.get(User, user_uuid)
    if not u:
        raise HTTPException(404, "No such user.")
    db.delete(u)
    return {"deleted": True}


# ---- Jobs & candidates (department-scoped) --------------------------------------

def _require_job_access(user: User, job: Job):
    if user.role != "super_admin" and job.department_id != user.department_id:
        raise HTTPException(403, "This job belongs to another department.")


@router.get("/jobs")
def list_jobs(user: User = Depends(current_admin), db=Depends(get_db)):
    q = select(Job)
    if user.role != "super_admin":
        q = q.where(Job.department_id == user.department_id)
    out = []
    for j in db.execute(q.order_by(Job.created_at.desc())).scalars().all():
        n_cand = db.execute(select(func.count(Candidate.uuid)).where(
            Candidate.job_uuid == j.uuid)).scalar()
        n_tested = db.execute(
            select(func.count(TestAssignment.uuid))
            .join(Candidate, TestAssignment.candidate_uuid == Candidate.uuid)
            .where(Candidate.job_uuid == j.uuid,
                   TestAssignment.status == "submitted")).scalar()
        out.append({
            "uuid": j.uuid, "title": j.title,
            "department": j.department.name,
            "department_id": j.department_id,
            "pass_threshold": j.pass_threshold,
            "candidates": n_cand, "tests_submitted": n_tested,
            "is_published": j.is_published,
            "application_deadline": (j.application_deadline.isoformat()
                                     if j.application_deadline else None),
            "location": j.location,
            "employment_type": j.employment_type,
            "openings": j.openings,
            "created_at": j.created_at.isoformat(),
        })
    return out


@router.get("/jobs/{job_uuid}")
def job_detail(job_uuid: str, user: User = Depends(current_admin),
               db=Depends(get_db)):
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    return {"uuid": j.uuid, "title": j.title, "department": j.department.name,
            "department_id": j.department_id,
            "jd_text": j.jd_text, "pass_threshold": j.pass_threshold,
            "is_published": j.is_published,
            "application_deadline": (j.application_deadline.isoformat()
                                     if j.application_deadline else None),
            "location": j.location,
            "employment_type": j.employment_type,
            "openings": j.openings}


class JobUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    jd_text: str | None = None
    pass_threshold: int | None = Field(default=None, ge=0, le=100)
    department_id: int | None = None
    # Public job-portal controls.
    is_published: bool | None = None
    application_deadline: datetime | None = None
    location: str | None = Field(default=None, max_length=300)
    employment_type: str | None = Field(default=None, max_length=60)
    openings: int | None = Field(default=None, ge=1, le=9999)


class JobCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    department_id: int
    jd_text: str = ""
    pass_threshold: int = Field(default=60, ge=0, le=100)
    location: str = Field(default="", max_length=300)
    employment_type: str = Field(default="", max_length=60)
    openings: int = Field(default=1, ge=1, le=9999)
    application_deadline: datetime | None = None
    is_published: bool = False


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.post("/jobs")
def create_job(body: JobCreate, user: User = Depends(current_admin),
               db=Depends(get_db)):
    """Create (post) a new job. Admins can post into their own department;
    only a super admin can post into any department."""
    if user.role != "super_admin" and body.department_id != user.department_id:
        raise HTTPException(403, "You can only post jobs in your own department.")
    if not db.get(Department, body.department_id):
        raise HTTPException(404, "Department does not exist.")
    j = Job(
        title=body.title.strip(),
        department_id=body.department_id,
        jd_text=body.jd_text,
        pass_threshold=body.pass_threshold,
        location=body.location.strip(),
        employment_type=body.employment_type.strip(),
        openings=body.openings,
        application_deadline=_naive_utc(body.application_deadline),
        is_published=body.is_published,
    )
    db.add(j)
    db.flush()
    return {"uuid": j.uuid, "title": j.title, "department": j.department.name,
            "is_published": j.is_published}


@router.patch("/jobs/{job_uuid}")
def update_job(job_uuid: str, body: JobUpdate,
               user: User = Depends(current_admin), db=Depends(get_db)):
    """Edit a job's title, description, pass threshold, or (super admin only)
    its department."""
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)

    if body.title is not None:
        j.title = body.title.strip()
    if body.jd_text is not None:
        j.jd_text = body.jd_text
    if body.pass_threshold is not None:
        j.pass_threshold = body.pass_threshold
    if body.is_published is not None:
        j.is_published = body.is_published
    if body.application_deadline is not None:
        # A naive datetime is stored as-is (the DB column is naive UTC); an
        # aware one is normalised to naive UTC to match the rest of the app.
        dl = body.application_deadline
        if dl.tzinfo is not None:
            dl = dl.astimezone(timezone.utc).replace(tzinfo=None)
        j.application_deadline = dl
    if body.location is not None:
        j.location = body.location.strip()
    if body.employment_type is not None:
        j.employment_type = body.employment_type.strip()
    if body.openings is not None:
        j.openings = body.openings
    if body.department_id is not None and body.department_id != j.department_id:
        # Reassigning a job across departments is a super-admin action.
        if user.role != "super_admin":
            raise HTTPException(403, "Only a super admin can move a job to "
                                     "another department.")
        if not db.get(Department, body.department_id):
            raise HTTPException(404, "Target department does not exist.")
        j.department_id = body.department_id

    return {"uuid": j.uuid, "title": j.title,
            "department": j.department.name,
            "department_id": j.department_id,
            "pass_threshold": j.pass_threshold,
            "is_published": j.is_published,
            "application_deadline": (j.application_deadline.isoformat()
                                     if j.application_deadline else None),
            "location": j.location,
            "employment_type": j.employment_type,
            "openings": j.openings}


def _cascade_delete_job(db, job: Job) -> dict:
    """Delete a job and everything hanging off it. Returns a count summary.

    Thin wrapper: the cascade itself is shared with the Streamlit delete path
    (core/job_delete.py) because two copies drifted and broke.
    """
    return cascade_delete_job(db, job)


@router.delete("/jobs/{job_uuid}")
def delete_job(job_uuid: str, user: User = Depends(current_admin),
               db=Depends(get_db)):
    """Permanently delete a job and ALL of its data (candidates, tests,
    assignments, answers, proctoring, and interviews)."""
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    title = j.title
    counts = _cascade_delete_job(db, j)
    return {"deleted": True, "title": title, "removed": counts}


# Priority for which of a candidate's several attempts represents them in the
# list: a finished attempt (so the score/answers are viewable) always wins over
# an unused pending link, even if the pending link was minted more recently.
_ASSIGNMENT_DISPLAY_RANK = {
    "submitted": 4, "terminated": 3, "started": 2, "pending": 1,
}


def _display_assignment(assignments, job):
    """Pick the attempt that best represents the candidate in the list.

    The LIVE attempt (not superseded) always wins — it is the one whose link
    works and whose result is pending. Superseded attempts are history, shown
    via the past-attempts view. When every attempt has been superseded, or the
    rows predate attempt history, fall back to the old ranking: prefer a
    completed attempt so its result stays reachable.
    """
    if not assignments:
        return None

    live = [a for a in assignments if not a.superseded_at]
    pool = live or list(assignments)

    def key(a):
        when = (a.submitted_at or a.started_at or a.created_at
                or a.expires_at or job.created_at)
        return (_ASSIGNMENT_DISPLAY_RANK.get(a.status, 0), when)

    return sorted(pool, key=key)[-1]


@router.get("/jobs/{job_uuid}/candidates")
def job_candidates(job_uuid: str, user: User = Depends(current_admin),
                   db=Depends(get_db)):
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)

    from portal.backend.routers.candidate import finalize_if_expired

    out = []
    for c in j.candidates:
        # Finalize any expired in-progress attempts before choosing what to show.
        for a in c.assignments:
            finalize_if_expired(a, db)
        latest = _display_assignment(c.assignments, j)
        time_taken = _minutes_taken(latest)
        test_passed = None
        if latest and latest.status == "submitted" and latest.test_score is not None:
            test_passed = latest.test_score >= latest.test.pass_score
        # A pending link that is NOT the displayed attempt = a live retake.
        pending_retake = next(
            (a for a in c.assignments
             if a.status == "pending" and not a.superseded_at
             and (not latest or a.uuid != latest.uuid)),
            None)
        past = [a for a in c.assignments if a.superseded_at]
        # Best score across every attempt, so a reset never looks like a
        # regression in the list.
        scored = [a.test_score for a in c.assignments
                  if a.test_score is not None]
        out.append({
            "uuid": c.uuid, "name": c.name, "email": c.email,
            "attempt_no": (latest.attempt_no or 1) if latest else None,
            "past_attempts": len(past),
            "total_attempts": len(c.assignments),
            "best_test_score": max(scored) if scored else None,
            "resume_score": c.resume_score, "status": c.status,
            "passed_screening": c.resume_score >= j.pass_threshold,
            "assignment_uuid": latest.uuid if latest else None,
            "test_status": latest.status if latest else None,
            "pending_retake_uuid": pending_retake.uuid if pending_retake else None,
            "test_score": latest.test_score if latest else None,
            "test_pass_score": latest.test.pass_score if latest else None,
            "test_passed": test_passed,
            "time_taken_min": time_taken,
            "time_taken_seconds": _seconds_taken(latest),
            "proctor_warnings": (latest.proctor_warnings or 0) if latest else 0,
            "max_warnings": latest.test.max_warnings if latest else None,
            "terminated_reason": latest.terminated_reason if latest else None,
            "last_seen": (latest.last_seen.isoformat()
                          if latest and latest.last_seen else None),
            "answered_so_far": (len(latest.draft_answers or {})
                                if latest and latest.status == "started"
                                else None),
            "expires_at": (latest.expires_at.isoformat()
                           if latest and latest.expires_at else None),
            "submitted_at": (latest.submitted_at.isoformat()
                             if latest and latest.submitted_at else None),
            "interview_status": _latest_interview_status(c),
        })
    out.sort(key=lambda r: (r["test_score"] or -1, r["resume_score"]),
             reverse=True)
    return out


def _minutes_taken(assignment) -> float | None:
    if not assignment or not assignment.started_at or not assignment.submitted_at:
        return None
    secs = (assignment.submitted_at - assignment.started_at).total_seconds()
    return round(secs / 60, 1)


def _seconds_taken(assignment) -> int | None:
    if not assignment or not assignment.started_at or not assignment.submitted_at:
        return None
    return int((assignment.submitted_at - assignment.started_at).total_seconds())


@router.get("/assignments/{assignment_uuid}")
def assignment_detail(assignment_uuid: str, user: User = Depends(current_admin),
                      db=Depends(get_db)):
    """Full result for one candidate's test: score, time taken, and every
    question with the candidate's answer beside the correct one."""
    a = db.get(TestAssignment, assignment_uuid)
    if not a:
        raise HTTPException(404, "No such assignment.")
    job = a.test.job
    _require_job_access(user, job)

    answers = {ans.question_id: ans for ans in a.answers}
    letters = "ABCD"
    questions = []
    for q in questions_for(a):
        ans = answers.get(q.id)
        sel = ans.selected_index if ans else -1
        questions.append({
            "question": q.question,
            "options": q.options_json,
            "correct_index": q.correct_index,
            "correct_letter": letters[q.correct_index]
            if 0 <= q.correct_index < 4 else "?",
            "selected_index": sel,
            "selected_letter": letters[sel] if 0 <= sel < 4 else "—",
            "is_correct": bool(ans and ans.is_correct),
            "answered": sel >= 0,
        })

    n_correct = sum(1 for q in questions if q["is_correct"])
    passed = (a.test_score is not None and a.status == "submitted"
              and a.test_score >= a.test.pass_score)
    proctor_events = [{
        "event_type": e.event_type,
        "detail": e.detail,
        "is_warning": e.is_warning,
        "evidence": e.evidence,
        "at": e.created_at.isoformat(),
    } for e in sorted(a.proctor_events, key=lambda e: e.id)]
    return {
        "assignment_uuid": a.uuid,
        "candidate_name": a.candidate.name,
        "candidate_email": a.candidate.email,
        "job_title": job.title,
        "resume_score": a.candidate.resume_score,
        "status": a.status,
        "test_score": a.test_score,
        "pass_score": a.test.pass_score,
        "passed": passed,
        "correct": n_correct,
        "total": len(questions),
        "time_taken_min": _minutes_taken(a),
        "time_taken_seconds": _seconds_taken(a),
        "duration_minutes": a.test.duration_minutes,
        "started_at": a.started_at.isoformat() if a.started_at else None,
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "proctored": bool(a.test.proctored),
        "proctor_warnings": a.proctor_warnings or 0,
        "max_warnings": a.test.max_warnings,
        "terminated_reason": a.terminated_reason,
        "proctor_events": proctor_events,
        "questions": questions,
    }


# ---- Question bank -----------------------------------------------------------

class BankCategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class BankItemIn(BaseModel):
    question: str = Field(min_length=1)
    options: list[str]
    correct_index: int = Field(ge=0, le=3)
    explanation: str = ""
    difficulty: str = "medium"
    category_id: int | None = None


class BankItemUpdate(BaseModel):
    question: str | None = None
    options: list[str] | None = None
    correct_index: int | None = Field(default=None, ge=0, le=3)
    explanation: str | None = None
    difficulty: str | None = None
    category_id: int | None = None
    active: bool | None = None


class BankGenerateIn(BaseModel):
    count: int = Field(default=5, ge=1, le=50)
    difficulty: str = "medium"
    category_id: int | None = None


def _bank_job(db, job_uuid: str, user: User) -> Job:
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    return j


@router.get("/jobs/{job_uuid}/bank")
def get_bank(job_uuid: str, include_retired: bool = False,
             user: User = Depends(current_admin), db=Depends(get_db)):
    """The job's whole question bank: categories, questions, active counts.

    Retired questions are excluded unless asked for — they are still in the
    bank (and in every paper already sat) but are no longer offered for new
    tests.
    """
    j = _bank_job(db, job_uuid, user)
    return {
        "categories": [{"id": c.id, "name": c.name}
                       for c in bank.list_categories(db, j.uuid)],
        "items": [{
            "id": i.id, "question": i.question,
            "options": list(i.options_json or []),
            "correct_index": i.correct_index,
            "explanation": i.explanation or "",
            "difficulty": i.difficulty,
            "category_id": i.category_id,
            "category": i.category.name if i.category else "",
            "source": i.source, "times_used": i.times_used or 0,
            "active": bool(i.active),
        } for i in bank.list_items(db, j.uuid,
                                   active_only=not include_retired)],
        "counts": bank.counts_by_category(db, j.uuid),
    }


@router.post("/jobs/{job_uuid}/bank/categories")
def add_bank_category(job_uuid: str, body: BankCategoryIn,
                      user: User = Depends(current_admin), db=Depends(get_db)):
    j = _bank_job(db, job_uuid, user)
    try:
        c = bank.add_category(db, j.uuid, body.name)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": c.id, "name": c.name}


@router.post("/jobs/{job_uuid}/bank/categories/suggest")
def suggest_bank_categories(job_uuid: str, user: User = Depends(current_admin),
                            db=Depends(get_db)):
    """Propose categories from the JD and add any that are missing."""
    j = _bank_job(db, job_uuid, user)
    if not (j.jd_text or "").strip():
        raise HTTPException(422, "This job has no description to read.")
    import mcq as mcq_mod
    added = [bank.add_category(db, j.uuid, name)
             for name in mcq_mod.suggest_categories(j.jd_text)]
    return {"categories": [{"id": c.id, "name": c.name} for c in added]}


@router.patch("/bank/categories/{cat_id}")
def rename_bank_category(cat_id: int, body: BankCategoryIn,
                         user: User = Depends(current_admin),
                         db=Depends(get_db)):
    c = db.get(QuestionBankCategory, cat_id)
    if not c:
        raise HTTPException(404, "No such category.")
    _bank_job(db, c.job_uuid, user)
    try:
        bank.rename_category(db, cat_id, body.name)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": c.id, "name": c.name}


@router.delete("/bank/categories/{cat_id}")
def delete_bank_category(cat_id: int, user: User = Depends(current_admin),
                         db=Depends(get_db)):
    c = db.get(QuestionBankCategory, cat_id)
    if not c:
        raise HTTPException(404, "No such category.")
    _bank_job(db, c.job_uuid, user)
    orphaned = bank.delete_category(db, cat_id)
    return {"deleted": True, "questions_kept": orphaned}


@router.post("/jobs/{job_uuid}/bank/items")
def add_bank_item(job_uuid: str, body: BankItemIn,
                  user: User = Depends(current_admin), db=Depends(get_db)):
    j = _bank_job(db, job_uuid, user)
    try:
        it = bank.add_item(db, j.uuid, body.question, body.options,
                           body.correct_index, body.explanation,
                           body.difficulty, body.category_id, source="custom")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": it.id}


@router.post("/jobs/{job_uuid}/bank/generate")
def generate_into_bank(job_uuid: str, body: BankGenerateIn,
                       user: User = Depends(current_admin),
                       db=Depends(get_db)):
    """Generate questions from the JD straight into the bank.

    Existing bank questions are passed to the model as don't-repeat context,
    so topping a bank up never re-mints what is already in it.
    """
    j = _bank_job(db, job_uuid, user)
    if not (j.jd_text or "").strip():
        raise HTTPException(422, "This job has no description to generate from.")
    cat = None
    if body.category_id is not None:
        cat = db.get(QuestionBankCategory, body.category_id)
        if not cat or cat.job_uuid != j.uuid:
            raise HTTPException(404, "No such category for this job.")

    import mcq as mcq_mod
    existing = [i.question for i in bank.list_items(db, j.uuid,
                                                    active_only=False)]
    gen = mcq_mod.generate_mcqs(j.jd_text, body.difficulty, body.count,
                                avoid=existing,
                                category=cat.name if cat else "")
    if not gen.questions:
        raise HTTPException(502, "The generator returned nothing — try again.")
    added = bank.add_items(db, j.uuid, gen.questions,
                           body.category_id, body.difficulty, source="llm")
    return {"added": len(added), "requested": body.count,
            "ids": [i.id for i in added]}


@router.patch("/bank/items/{item_id}")
def update_bank_item(item_id: int, body: BankItemUpdate,
                     user: User = Depends(current_admin), db=Depends(get_db)):
    it = db.get(QuestionBankItem, item_id)
    if not it:
        raise HTTPException(404, "No such question.")
    _bank_job(db, it.job_uuid, user)
    try:
        bank.update_item(db, item_id, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": item_id}


@router.delete("/bank/items/{item_id}")
def delete_bank_item(item_id: int, user: User = Depends(current_admin),
                     db=Depends(get_db)):
    it = db.get(QuestionBankItem, item_id)
    if not it:
        raise HTTPException(404, "No such question.")
    _bank_job(db, it.job_uuid, user)
    bank.delete_item(db, item_id)
    return {"deleted": True}


# ---- Email templates ---------------------------------------------------------

class TemplateIn(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)


@router.get("/jobs/{job_uuid}/email-templates/{kind}")
def get_email_template(job_uuid: str, kind: str,
                       user: User = Depends(current_admin), db=Depends(get_db)):
    """The effective template for this job, plus the built-in for comparison."""
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    try:
        tpl = templates.get(db, kind, j.uuid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    tpl["builtin"] = {
        "subject": templates.DEFAULTS[kind]["subject"],
        "body": templates.DEFAULTS[kind]["body"],
    }
    return tpl


@router.put("/jobs/{job_uuid}/email-templates/{kind}")
def save_email_template(job_uuid: str, kind: str, body: TemplateIn,
                        user: User = Depends(current_admin),
                        db=Depends(get_db)):
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    try:
        templates.save(db, kind, j.uuid, body.subject, body.body,
                       updated_by=user.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return templates.get(db, kind, j.uuid)


@router.delete("/jobs/{job_uuid}/email-templates/{kind}")
def reset_email_template(job_uuid: str, kind: str,
                         user: User = Depends(current_admin),
                         db=Depends(get_db)):
    """Drop this job's override and fall back to the default wording."""
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    if kind not in templates.DEFAULTS:
        raise HTTPException(404, f"Unknown template kind '{kind}'.")
    removed = templates.reset(db, kind, j.uuid)
    return {"reset": removed, **templates.get(db, kind, j.uuid)}


@router.post("/jobs/{job_uuid}/email-templates/{kind}/preview")
def preview_email_template(job_uuid: str, kind: str, body: TemplateIn,
                           user: User = Depends(current_admin),
                           db=Depends(get_db)):
    """Render the draft against sample values, without saving or sending.

    Lets a recruiter see exactly what lands in the inbox — including any
    placeholder they mistyped, which renders literally rather than vanishing.
    """
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    if kind not in templates.DEFAULTS:
        raise HTTPException(404, f"Unknown template kind '{kind}'.")

    sample = type("C", (), {"name": "Jane Candidate",
                            "email": "jane@example.com"})()
    values = _interview_template_values(
        sample, j, "in_person", datetime.utcnow() + timedelta(days=3), 45,
        "Level 4, 100 Example Street", "Please bring photo ID.")
    used = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", body.subject + body.body))
    return {
        "subject": templates.render(body.subject, values),
        "body": templates.render(body.body, values),
        "unknown_placeholders": sorted(used - set(values.keys())),
    }


class CandidateIn(_EmailField):
    job_uuid: str
    name: str
    phone: str = ""


@router.post("/candidates")
def create_candidate(body: CandidateIn, user: User = Depends(current_admin),
                     db=Depends(get_db)):
    j = db.get(Job, body.job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    c = Candidate(job_uuid=j.uuid, name=body.name,
                  email=body.email.lower(), phone=body.phone,
                  status="added_manually")
    db.add(c)
    db.flush()
    return {"uuid": c.uuid}


# ---- Assignment management (reset / edit expiry / send results) ---------------

def _latest_interview_status(candidate: Candidate) -> str | None:
    """Return the status of the most recent interview for a candidate."""
    if not candidate.interviews:
        return None
    latest = sorted(candidate.interviews,
                    key=lambda i: i.scheduled_at)[-1]
    return latest.status


class ResetIn(BaseModel):
    expires_at: str | None = None  # ISO datetime string
    notify: bool = True            # email the candidate their new link
    reason: str = Field(default="", max_length=400)


@router.put("/assignments/{assignment_uuid}/reset")
def reset_assignment(assignment_uuid: str, body: ResetIn,
                    user: User = Depends(current_admin), db=Depends(get_db)):
    """Give the candidate a fresh attempt WITHOUT destroying the old one.

    The previous attempt is superseded, not wiped: its answers, score and
    proctoring log stay in the database and remain visible as a past attempt.
    A brand-new assignment is inserted, which means a new uuid — so a new,
    different link — and a newly drawn paper from the test's pool. The old
    link stops working (see _check_open) so nobody can sit both.
    """
    a = db.get(TestAssignment, assignment_uuid)
    if not a:
        raise HTTPException(404, "No such assignment.")
    job = a.test.job
    _require_job_access(user, job)

    if a.superseded_at:
        raise HTTPException(
            409, "That attempt has already been replaced. Reset the "
                 "candidate's current attempt instead.")

    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at)
        except ValueError:
            raise HTTPException(422, "Invalid datetime format for expires_at.")

    attempts.supersede(a, by=user.email, reason=body.reason)
    fresh = TestAssignment(
        test_uuid=a.test_uuid, candidate_uuid=a.candidate_uuid,
        expires_at=expires,
        attempt_no=attempts.next_attempt_no(db, job.uuid, a.candidate_uuid))
    db.add(fresh)
    db.flush()
    # A new draw, not a copy: a retake must not be the paper they just saw.
    paper = assign_questions(db, fresh)
    a.candidate.status = "invited"

    link = f"{_PORTAL_BASE}/test/{fresh.uuid}"
    sent, msg = None, ""
    if body.notify and a.candidate.email:
        sent, msg = mailer.send_email(
            a.candidate.email,
            f"{job.title} — New Assessment Link",
            f"Dear {a.candidate.name or 'Candidate'},\n\n"
            f"Your assessment for the {job.title} position has been reset and "
            f"you have a new attempt.\n\n"
            f"Your new assessment link:\n{link}\n\n"
            f"Details:\n"
            f"- {len(paper)} multiple-choice questions\n"
            f"- Time limit: {a.test.duration_minutes} minutes (starts when "
            f"you open the link)\n"
            + (f"- The link expires on "
               f"{expires.strftime('%b %d, %Y at %I:%M %p')}\n"
               if expires else "")
            + "\nAny previous link you were sent no longer works — please use "
              "the one above.\n\n"
              "You will verify your identity with a one-time code sent to "
              "this email address.\n\n"
              "Best regards,\nThe Recruiting Team\n")

    return {
        "uuid": fresh.uuid,
        "status": fresh.status,
        "attempt_no": fresh.attempt_no,
        "link": link,
        "num_questions": len(paper),
        "expires_at": fresh.expires_at.isoformat() if fresh.expires_at else None,
        "previous_attempt": attempts.summarise(a),
        "emailed": sent,
        "email_message": msg,
    }


@router.get("/candidates/{candidate_uuid}/attempts")
def candidate_attempts(candidate_uuid: str, user: User = Depends(current_admin),
                       db=Depends(get_db)):
    """Every attempt this candidate has made at their job's test.

    Past attempts are kept forever — this is what makes a reset non-destructive.
    """
    # Imported here, not at module scope: candidate.py imports from this
    # module's package and a top-level import would be circular.
    from portal.backend.routers.candidate import finalize_if_expired

    c = db.get(Candidate, candidate_uuid)
    if not c:
        raise HTTPException(404, "No such candidate.")
    _require_job_access(user, c.job)
    rows = attempts.attempts_for(db, c.job_uuid, c.uuid)
    for a in rows:
        finalize_if_expired(a, db)
    return {
        "candidate_uuid": c.uuid,
        "candidate_name": c.name,
        "job_title": c.job.title,
        "attempts": [attempts.summarise(a) for a in rows],
    }


class EditExpiryIn(BaseModel):
    expires_at: str  # ISO datetime string


@router.patch("/assignments/{assignment_uuid}")
def edit_assignment(assignment_uuid: str, body: EditExpiryIn,
                   user: User = Depends(current_admin), db=Depends(get_db)):
    """Edit the expiry date of a test assignment."""
    a = db.get(TestAssignment, assignment_uuid)
    if not a:
        raise HTTPException(404, "No such assignment.")
    _require_job_access(user, a.test.job)
    try:
        a.expires_at = datetime.fromisoformat(body.expires_at)
    except ValueError:
        raise HTTPException(422, "Invalid datetime format.")
    return {"uuid": a.uuid,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None}


@router.post("/assignments/{assignment_uuid}/send-results")
def send_results(assignment_uuid: str,
                user: User = Depends(current_admin), db=Depends(get_db)):
    """Email the candidate their test results and per-question breakdown."""
    a = db.get(TestAssignment, assignment_uuid)
    if not a:
        raise HTTPException(404, "No such assignment.")
    _require_job_access(user, a.test.job)
    if a.status != "submitted":
        raise HTTPException(400, "Test has not been submitted yet.")

    answers_map = {ans.question_id: ans for ans in a.answers}
    letters = "ABCD"
    lines = []
    # The paper THEY sat, not the pool: emailing a breakdown built from
    # test.questions would show the candidate questions they were never asked
    # (and leak the rest of the pool to a future retaker).
    paper = questions_for(a)
    for i, q in enumerate(paper, 1):
        ans = answers_map.get(q.id)
        sel = ans.selected_index if ans else -1
        correct = q.correct_index
        is_right = bool(ans and ans.is_correct)
        sel_letter = letters[sel] if 0 <= sel < 4 else "—"
        cor_letter = letters[correct] if 0 <= correct < 4 else "?"
        mark = "✓" if is_right else "✗"
        lines.append(f"{mark} Q{i}. {q.question}")
        lines.append(f"   Your answer: {sel_letter}")
        if not is_right:
            lines.append(f"   Correct answer: {cor_letter}")
        lines.append("")

    time_secs = _seconds_taken(a)
    time_str = _fmt_time(time_secs) if time_secs is not None else "N/A"
    passed = (a.test_score is not None and a.test_score >= a.test.pass_score)

    body_text = (
        f"Hi {a.candidate.name or 'there'},\n\n"
        f"Here are your results for the {a.test.job.title} assessment:\n\n"
        f"Score: {a.test_score}% ({'PASS' if passed else 'FAIL'})\n"
        f"Correct: {sum(1 for ans in a.answers if ans.is_correct)}"
        f" / {len(paper)}\n"
        f"Time taken: {time_str}\n\n"
        f"--- Question Breakdown ---\n\n"
        + "\n".join(lines)
        + "\nThank you for completing the assessment.\n"
    )

    ok, msg = mailer.send_email(
        a.candidate.email,
        f"{a.test.job.title} — Your Assessment Results",
        body_text)
    if not ok:
        raise HTTPException(502, f"Could not send results: {msg}")
    return {"sent": True, "message": msg}


def _fmt_time(seconds: int | None) -> str:
    """Format seconds into a human-readable string."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m" if mins else f"{hours}h"


# ---- Merit Decider ---------------------------------------------------------------
#
# A per-job weighted funnel:
#   Stage 1  resume screening + test  -> weighted "screening merit".
#            Candidates at/above invite_threshold are eligible to be
#            auto-invited to the AI interview.
#   Stage 2  resume + test + AI interview -> weighted "final merit".
#            Candidates at/above onsite_threshold are onsite-eligible; the
#            recruiter shortlists the top N of them for an onsite interview.
#
# All three inputs are already on a 0-100 scale (resume_score, test %, AI
# score), so weighting is a straight weighted average.

MERIT_DEFAULTS = {
    "resume_weight": 30,
    "test_weight": 30,
    "interview_weight": 40,
    "invite_threshold": 60,
    "onsite_threshold": 70,
    "onsite_top_n": 5,
    # No AI interview until the candidate has PASSED a test for this job.
    # On by default (the recruiting policy this was built for), but a job with
    # no test configured would otherwise be un-interviewable forever, so it
    # stays switchable per job.
    "require_test_pass": True,
    # Auto-invite the moment a candidate passes. OFF by default: this makes
    # the server email a candidate with no human in the loop, so it must be a
    # deliberate per-job decision rather than something a job inherits.
    "auto_invite_on_pass": False,
    "auto_invite_delay_hours": 48,
    "auto_invite_duration_minutes": 20,
    "auto_invite_num_questions": 5,
}


class MeritConfigIn(BaseModel):
    resume_weight: int = Field(ge=0, le=100)
    test_weight: int = Field(ge=0, le=100)
    interview_weight: int = Field(ge=0, le=100)
    invite_threshold: int = Field(ge=0, le=100)
    onsite_threshold: int = Field(ge=0, le=100)
    onsite_top_n: int = Field(ge=1, le=100)
    require_test_pass: bool = True
    auto_invite_on_pass: bool = False
    auto_invite_delay_hours: int = Field(default=48, ge=0, le=8760)
    auto_invite_duration_minutes: int = Field(default=20, ge=1)
    auto_invite_num_questions: int = Field(default=5, ge=2, le=15)

    @field_validator("interview_weight")
    @classmethod
    def _weights_sum_100(cls, v, info):
        rw = info.data.get("resume_weight", 0)
        tw = info.data.get("test_weight", 0)
        if rw + tw + v != 100:
            raise ValueError("resume + test + interview weights must sum to 100")
        return v


def _merit_config(job: Job) -> dict:
    cfg = dict(MERIT_DEFAULTS)
    if job.merit_config:
        cfg.update({k: job.merit_config[k]
                    for k in MERIT_DEFAULTS if k in job.merit_config})
    return cfg


def test_gate_block_reason(candidate: Candidate, job: Job,
                           cfg: dict | None = None) -> str | None:
    """Why this candidate may NOT be sent to an AI interview yet, or None.

    Enforces "test before interview": a candidate must have actually sat and
    passed a test for this job. Superseded attempts are ignored — a passing
    attempt that was later reset is no longer a pass. Terminated attempts do
    not count as a pass regardless of their partial score.
    """
    cfg = cfg or _merit_config(job)
    if not cfg.get("require_test_pass", True):
        return None

    finished = [a for a in candidate.assignments
                if not a.superseded_at
                and a.status in ("submitted", "terminated")]
    if not finished:
        return ("has not taken the test yet — an AI interview cannot be sent "
                "until they have sat and passed it")
    passed = [a for a in finished
              if a.status == "submitted" and a.test_score is not None
              and a.test_score >= a.test.pass_score]
    if not passed:
        best = max((a.test_score for a in finished
                    if a.test_score is not None), default=None)
        need = finished[0].test.pass_score
        if any(a.status == "terminated" for a in finished):
            return (f"their test was terminated for proctoring violations "
                    f"(score {best}%) — that is not a pass")
        return (f"has not passed the test (best {best}%, needs {need}%)")
    return None


def _best_test_score(candidate: Candidate) -> float | None:
    """Highest score among the candidate's finished tests. Terminated attempts
    count (with their recorded partial score) so a flagged candidate ranks on
    real signal instead of sitting forever in 'awaiting test'."""
    scores = [a.test_score for a in candidate.assignments
              if a.status in ("submitted", "terminated")
              and a.test_score is not None]
    return max(scores) if scores else None


def _best_interview(candidate) -> "AIInterview | None":
    """The candidate's most informative finished AI interview (highest score;
    a terminated one with no score still counts as attempted)."""
    finished = [iv for iv in candidate.interviews_ai
                if iv.status in ("completed", "terminated")]
    if not finished:
        return None
    return max(finished, key=lambda iv: (iv.ai_score if iv.ai_score is not None
                                         else -1))


def _has_open_ai_interview(candidate) -> bool:
    """True if an AI interview is scheduled/running or already finished — i.e.
    the candidate should not be auto-invited again."""
    return any(iv.status in ("scheduled", "started", "completed", "terminated")
               for iv in candidate.interviews_ai)


def _compute_merit(candidate: Candidate, job: Job, cfg: dict) -> dict:
    rw, tw, iw = (cfg["resume_weight"], cfg["test_weight"],
                  cfg["interview_weight"])
    resume = candidate.resume_score
    test = _best_test_score(candidate)
    best_iv = _best_interview(candidate)
    interview = best_iv.ai_score if best_iv else None

    # Stage 1 — screening merit from resume + test, weights renormalized to
    # the two available inputs (None until the test is completed).
    stage1 = None
    if test is not None:
        denom = rw + tw
        stage1 = round((resume * rw + test * tw) / denom, 1) if denom else 0.0

    # Stage 2 — final merit from ALL available inputs, renormalized to the
    # components that are present.  A candidate who did an AI interview but
    # skipped the test still gets a final merit (resume + interview).
    final = None
    parts: list[tuple[str, float, int]] = [("resume", resume, rw)]
    if test is not None:
        parts.append(("test", test, tw))
    if interview is not None:
        parts.append(("interview", interview, iw))
    final_parts: list[dict] = []
    missing = [n for n in ("test", "interview")
               if n not in {p[0] for p in parts}]
    # Need at least 2 inputs — resume alone is just the resume_score.
    if len(parts) >= 2:
        denom = sum(w for _, _, w in parts)
        if denom:
            final = round(sum(s * w for _, s, w in parts) / denom, 1)
            # Effective percentages actually used (so HR can verify the math
            # when a missing stage rebalanced the configured weights).
            final_parts = [{"name": n, "score": s,
                            "weight_pct": round(100 * w / denom)}
                           for n, s, w in parts]
        else:
            final = 0.0

    has_ai = _has_open_ai_interview(candidate)
    invite_eligible = (stage1 is not None
                       and stage1 >= cfg["invite_threshold"]
                       and not has_ai)
    onsite_eligible = (final is not None
                       and final >= cfg["onsite_threshold"])

    # Stage determination — handle the case where a candidate has an AI
    # interview score but never took a test (e.g. admin scheduled directly).
    if interview is not None and test is None:
        # Interviewed without a test — final merit computed from resume + iv.
        if onsite_eligible:
            stage = "onsite_candidate"
        else:
            stage = "interviewed_no_test"
    elif test is None and not has_ai:
        stage = "awaiting_test"
    elif test is None and has_ai:
        stage = "interview_pending"
    elif stage1 is not None and stage1 < cfg["invite_threshold"]:
        stage = "below_interview_cutoff"
    elif interview is None and not has_ai:
        stage = "invite_to_interview"
    elif interview is None and has_ai:
        stage = "interview_pending"
    elif onsite_eligible:
        stage = "onsite_candidate"
    else:
        stage = "below_onsite_cutoff"

    return {
        "candidate_uuid": candidate.uuid,
        "name": candidate.name,
        "email": candidate.email,
        "status": candidate.status,
        "resume_score": resume,
        "test_score": test,
        "interview_score": interview,
        "interview_status": best_iv.status if best_iv else (
            "scheduled" if has_ai else None),
        "screening_merit": stage1,
        "final_merit": final,
        "final_parts": final_parts,      # effective mix actually used
        "missing_inputs": missing,       # stages not yet completed
        "invite_eligible": invite_eligible,
        "onsite_eligible": onsite_eligible,
        "stage": stage,
    }


@router.get("/jobs/{job_uuid}/merit-config")
def get_merit_config(job_uuid: str, user: User = Depends(current_admin),
                     db=Depends(get_db)):
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    return _merit_config(j)


@router.put("/jobs/{job_uuid}/merit-config")
def set_merit_config(job_uuid: str, body: MeritConfigIn,
                     user: User = Depends(current_admin), db=Depends(get_db)):
    from sqlalchemy.orm.attributes import flag_modified
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    j.merit_config = body.model_dump()
    flag_modified(j, "merit_config")
    return j.merit_config


@router.get("/jobs/{job_uuid}/merit")
def job_merit(job_uuid: str, user: User = Depends(current_admin),
              db=Depends(get_db)):
    """Weighted merit ranking of every candidate for the job."""
    from portal.backend.routers.candidate import finalize_if_expired
    from portal.backend.routers.interview import finalize_if_expired as _fin_iv

    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    cfg = _merit_config(j)

    # Make sure lazily-finalized attempts/interviews are up to date first.
    for c in j.candidates:
        for a in c.assignments:
            finalize_if_expired(a, db)
        for iv in c.interviews_ai:
            _fin_iv(iv, db)

    rows = [_compute_merit(c, j, cfg) for c in j.candidates]

    # Rank: final merit first, then screening merit, then resume — Nones last.
    def sort_key(r):
        return (
            r["final_merit"] if r["final_merit"] is not None else -1,
            r["screening_merit"] if r["screening_merit"] is not None else -1,
            r["resume_score"],
        )
    rows.sort(key=sort_key, reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # Which onsite-eligible candidates make the top-N cut.
    onsite_sorted = [r for r in rows if r["onsite_eligible"]]
    onsite_sorted.sort(key=lambda r: r["final_merit"], reverse=True)
    top_ids = {r["candidate_uuid"] for r in onsite_sorted[:cfg["onsite_top_n"]]}
    for r in rows:
        r["onsite_top_n"] = r["candidate_uuid"] in top_ids

    return {
        "config": cfg,
        "candidates": rows,
        "counts": {
            "total": len(rows),
            "tested": sum(1 for r in rows if r["test_score"] is not None),
            "interviewed": sum(1 for r in rows
                               if r["interview_score"] is not None),
            "invite_eligible": sum(1 for r in rows if r["invite_eligible"]),
            "onsite_eligible": len(onsite_sorted),
            "onsite_selected": len(top_ids),
        },
    }


class MeritAutoInviteIn(BaseModel):
    scheduled_at: str                 # ISO datetime
    scheduled_time_label: str | None = None
    duration_minutes: int = Field(default=20, ge=1)
    num_questions: int = Field(default=5, ge=2, le=15)
    focus: str = Field(default="", max_length=500)
    max_warnings: int = Field(default=3, ge=1, le=10)


@router.post("/jobs/{job_uuid}/merit/auto-invite")
def merit_auto_invite(job_uuid: str, body: MeritAutoInviteIn,
                      user: User = Depends(current_admin), db=Depends(get_db)):
    """Auto-schedule AI interviews for every candidate whose screening merit
    clears the invite threshold and who has no AI interview yet."""
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    cfg = _merit_config(j)
    try:
        sched = datetime.fromisoformat(body.scheduled_at.replace("Z", ""))
    except ValueError:
        raise HTTPException(422, "Invalid datetime for scheduled_at.")

    invited, skipped = [], []
    for c in j.candidates:
        m = _compute_merit(c, j, cfg)
        if not m["invite_eligible"]:
            continue
        if not c.email:
            skipped.append({"name": c.name, "reason": "no email"})
            continue
        blocked = test_gate_block_reason(c, j, cfg)
        if blocked:
            skipped.append({"name": c.name, "reason": blocked})
            continue
        iv = AIInterview(candidate_uuid=c.uuid, job_uuid=j.uuid,
                         scheduled_at=sched,
                         duration_minutes=body.duration_minutes,
                         num_questions=body.num_questions,
                         focus=body.focus, max_warnings=body.max_warnings)
        db.add(iv)
        db.flush()
        link = f"{_PORTAL_BASE}/interview/{iv.uuid}"
        when = body.scheduled_time_label or sched.strftime(
            "%A, %B %d, %Y at %I:%M %p (UTC)")
        ok, msg = mailer.send_email(
            c.email, f"{j.title} — AI Interview Invitation",
            f"Dear {c.name or 'Candidate'},\n\n"
            f"Based on your assessment results you have advanced to the "
            f"automated voice interview for the {j.title} position.\n\n"
            f"Your personal interview link:\n{link}\n\n"
            f"Schedule:\n- Date & time: {when}\n"
            f"- Duration: about {body.duration_minutes} minutes\n"
            f"- The link opens 10 minutes before your slot and closes 30 "
            f"minutes after it.\n\n"
            f"Use Chrome or Edge on a computer, in a quiet room, alone. "
            f"Camera, microphone and full-screen sharing are required.\n\n"
            f"Best regards,\nThe Recruiting Team\n")
        invited.append({"name": c.name, "email": c.email,
                        "sent": ok, "message": msg})
    return {"invited": invited, "skipped": skipped,
            "count": len(invited)}


class MeritShortlistIn(BaseModel):
    notify: bool = True   # email the shortlisted candidates


@router.post("/jobs/{job_uuid}/merit/shortlist-onsite")
def merit_shortlist_onsite(job_uuid: str, body: MeritShortlistIn,
                           user: User = Depends(current_admin),
                           db=Depends(get_db)):
    """Flag the top-N onsite-eligible candidates as shortlisted for an onsite
    interview (and optionally email them). The recruiter then schedules the
    actual onsite via the normal interview flow."""
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    cfg = _merit_config(j)

    ranked = []
    for c in j.candidates:
        m = _compute_merit(c, j, cfg)
        if m["onsite_eligible"]:
            ranked.append((m["final_merit"], c))
    ranked.sort(key=lambda t: t[0], reverse=True)
    top = ranked[:cfg["onsite_top_n"]]

    shortlisted = []
    for _score, c in top:
        c.status = "shortlisted_onsite"
        sent = None
        if body.notify and c.email:
            ok, _msg = mailer.send_email(
                c.email, f"{j.title} — Shortlisted for Onsite Interview",
                f"Dear {c.name or 'Candidate'},\n\n"
                f"Congratulations! Based on your overall performance across "
                f"the screening, assessment and AI interview, you have been "
                f"shortlisted for an onsite interview for the {j.title} "
                f"position.\n\nOur team will contact you shortly to arrange "
                f"a convenient time.\n\nBest regards,\nThe Recruiting Team\n")
            sent = ok
        shortlisted.append({"name": c.name, "email": c.email,
                            "final_merit": _score, "notified": sent})
    return {"shortlisted": shortlisted, "count": len(shortlisted)}


# ---- AI voice interviews ---------------------------------------------------------

class AIInterviewIn(BaseModel):
    candidate_uuid: str
    job_uuid: str
    scheduled_at: str                 # ISO datetime
    duration_minutes: int = Field(default=20, ge=1)
    num_questions: int = Field(default=5, ge=2, le=15)
    focus: str = Field(default="", max_length=500)
    max_warnings: int = Field(default=3, ge=1, le=10)
    scheduled_time_label: str | None = None


@router.post("/ai-interviews")
def create_ai_interview(body: AIInterviewIn, user: User = Depends(current_admin),
                        db=Depends(get_db)):
    j = db.get(Job, body.job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    c = db.get(Candidate, body.candidate_uuid)
    if not c or c.job_uuid != j.uuid:
        raise HTTPException(404, "Candidate not found for this job.")
    if not c.email:
        raise HTTPException(422, "Candidate has no email address.")
    blocked = test_gate_block_reason(c, j)
    if blocked:
        raise HTTPException(
            409, f"{c.name or 'This candidate'} {blocked}. Turn off "
                 f"'Require a passing test before any AI interview' in the "
                 f"'Ranking & setup' tab if this job does not use a test.")
    try:
        sched = datetime.fromisoformat(body.scheduled_at.replace("Z", ""))
    except ValueError:
        raise HTTPException(422, "Invalid datetime for scheduled_at.")

    iv = AIInterview(candidate_uuid=c.uuid, job_uuid=j.uuid,
                     scheduled_at=sched,
                     duration_minutes=body.duration_minutes,
                     num_questions=body.num_questions,
                     focus=body.focus, max_warnings=body.max_warnings)
    db.add(iv)
    db.flush()
    _advance_stage(db, c, "Interview")

    link = f"{_PORTAL_BASE}/interview/{iv.uuid}"
    when = body.scheduled_time_label or sched.strftime("%A, %B %d, %Y at %I:%M %p (UTC)")
    ok, msg = mailer.send_email(
        c.email, f"{j.title} — AI Interview Invitation",
        f"Dear {c.name or 'Candidate'},\n\n"
        f"You are invited to an automated voice interview for the {j.title} "
        f"position.\n\n"
        f"Your personal interview link:\n{link}\n\n"
        f"Schedule:\n"
        f"- Date & time: {when}\n"
        f"- Duration: about {body.duration_minutes} minutes\n"
        f"- The link opens 10 minutes before your slot and closes 30 "
        f"minutes after it.\n\n"
        f"How it works:\n"
        f"- An AI interviewer asks questions out loud; you answer by "
        f"speaking naturally.\n"
        f"- Use Chrome or Edge on a computer, in a quiet room, alone.\n"
        f"- You will verify your identity with a code sent to this email.\n"
        f"- Camera, microphone and full-screen sharing are required; the "
        f"session is monitored and repeated violations end the interview.\n\n"
        f"Best regards,\nThe Recruiting Team\n")
    return {"uuid": iv.uuid, "link": link, "sent": ok, "message": msg}


@router.get("/jobs/{job_uuid}/ai-interviews")
def list_ai_interviews(job_uuid: str, user: User = Depends(current_admin),
                       db=Depends(get_db)):
    from portal.backend.routers.interview import finalize_if_expired as _fin
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    ivs = db.execute(select(AIInterview).where(
        AIInterview.job_uuid == job_uuid)
        .order_by(AIInterview.scheduled_at.desc())).scalars().all()
    for iv in ivs:
        _fin(iv, db)
    return [{
        "uuid": iv.uuid,
        "candidate_name": iv.candidate.name,
        "candidate_email": iv.candidate.email,
        "scheduled_at": iv.scheduled_at.isoformat(),
        "duration_minutes": iv.duration_minutes,
        "status": iv.status,
        "ai_score": iv.ai_score,
        "proctor_warnings": iv.proctor_warnings or 0,
        "questions_asked": iv.questions_asked or 0,
    } for iv in ivs]


@router.get("/ai-interviews/{iv_uuid}")
def ai_interview_detail(iv_uuid: str, user: User = Depends(current_admin),
                        db=Depends(get_db)):
    from portal.backend.routers.interview import finalize_if_expired as _fin
    iv = db.get(AIInterview, iv_uuid)
    if not iv:
        raise HTTPException(404, "No such interview.")
    _require_job_access(user, iv.job)
    _fin(iv, db)
    return {
        "uuid": iv.uuid,
        "candidate_name": iv.candidate.name,
        "candidate_email": iv.candidate.email,
        "job_title": iv.job.title,
        "scheduled_at": iv.scheduled_at.isoformat(),
        "started_at": iv.started_at.isoformat() if iv.started_at else None,
        "completed_at": (iv.completed_at.isoformat()
                         if iv.completed_at else None),
        "duration_minutes": iv.duration_minutes,
        "status": iv.status,
        "focus": iv.focus,
        "num_questions": iv.num_questions,
        "questions_asked": iv.questions_asked or 0,
        "ai_score": iv.ai_score,
        "ai_summary": iv.ai_summary,
        "transcript": iv.transcript or [],
        "proctor_warnings": iv.proctor_warnings or 0,
        "max_warnings": iv.max_warnings,
        "terminated_reason": iv.terminated_reason,
        "events": [{
            "event_type": e.event_type, "detail": e.detail,
            "is_warning": e.is_warning, "evidence": e.evidence,
            "at": e.created_at.isoformat(),
        } for e in sorted(iv.events, key=lambda e: e.id)],
    }


@router.delete("/ai-interviews/{iv_uuid}")
def cancel_ai_interview(iv_uuid: str, user: User = Depends(current_admin),
                        db=Depends(get_db)):
    iv = db.get(AIInterview, iv_uuid)
    if not iv:
        raise HTTPException(404, "No such interview.")
    _require_job_access(user, iv.job)
    if iv.status in ("completed", "terminated"):
        raise HTTPException(409, "Interview already finished — cannot cancel.")
    iv.status = "cancelled"
    when = iv.scheduled_at.strftime("%A, %B %d, %Y at %I:%M %p")
    mailer.send_email(
        iv.candidate.email, f"{iv.job.title} — Interview Cancelled",
        f"Dear {iv.candidate.name or 'Candidate'},\n\n"
        f"Your AI interview scheduled for {when} has been cancelled. "
        f"We will be in touch if there are any updates.\n\n"
        f"Best regards,\nThe Recruiting Team\n")
    return {"cancelled": True}


@router.get("/ai-interviews/{iv_uuid}/join-token")
def admin_join_interview(iv_uuid: str, user: User = Depends(current_admin),
                         db=Depends(get_db)):
    """Mint a LiveKit participant token so an admin can observe (and
    optionally speak into) a live AI interview room."""
    iv = db.get(AIInterview, iv_uuid)
    if not iv:
        raise HTTPException(404, "No such interview.")
    _require_job_access(user, iv.job)
    if iv.status != "started":
        raise HTTPException(409, "Interview is not currently in progress.")

    lk_url = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    lk_key = os.getenv("LIVEKIT_API_KEY", "devkey")
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "secret")

    at = (
        lk_api.AccessToken(lk_key, lk_secret)
        .with_identity(f"admin_{user.uuid[:8]}")
        .with_name(user.name or "Admin")
        .with_metadata(json.dumps({"role": "admin"}))
        .with_grants(lk_api.VideoGrants(
            room_join=True,
            room=iv.uuid,
            can_publish=True,   # audio only — admin can unmute to speak
            can_subscribe=True,
        ))
    )
    return {
        "livekit_url": lk_url,
        "livekit_token": at.to_jwt(),
        "room_name": iv.uuid,
        "candidate_name": iv.candidate.name,
        "job_title": iv.job.title,
    }


# ---- Interview scheduling -------------------------------------------------------

class InterviewIn(BaseModel):
    candidate_uuid: str
    job_uuid: str
    interview_type: str = "video"  # in_person | phone | video
    scheduled_at: str  # ISO datetime string
    duration_minutes: int = Field(default=30, ge=1)
    location: str = ""  # address or video link
    notes: str = ""


class InterviewUpdate(BaseModel):
    interview_type: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int | None = None
    location: str | None = None
    notes: str | None = None
    status: str | None = None


_TYPE_LABELS = {"in_person": "In-Person", "phone": "Phone", "video": "Video"}


@router.post("/interviews")
def create_interview(body: InterviewIn, user: User = Depends(current_admin),
                     db=Depends(get_db)):
    j = db.get(Job, body.job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    c = db.get(Candidate, body.candidate_uuid)
    if not c or c.job_uuid != j.uuid:
        raise HTTPException(404, "Candidate not found for this job.")
    try:
        sched = datetime.fromisoformat(body.scheduled_at)
    except ValueError:
        raise HTTPException(422, "Invalid datetime format for scheduled_at.")
    if body.interview_type not in ("in_person", "phone", "video"):
        raise HTTPException(422, "interview_type must be in_person, phone, or video.")

    interview = Interview(
        candidate_uuid=c.uuid, job_uuid=j.uuid,
        interview_type=body.interview_type, scheduled_at=sched,
        duration_minutes=body.duration_minutes,
        location=body.location, notes=body.notes)
    db.add(interview)
    db.flush()
    _advance_stage(db, c, "Interview")

    # Render the (possibly HR-edited) invitation template.
    tpl = templates.get(db, "onsite_interview", j.uuid)
    values = _interview_template_values(c, j, body.interview_type, sched,
                                        body.duration_minutes, body.location,
                                        body.notes)
    ok, msg = mailer.send_email(
        c.email,
        templates.render(tpl["subject"], values),
        templates.render(tpl["body"], values))

    return {"id": interview.id, "sent": ok, "message": msg,
            "template_source": tpl["source"]}


def _interview_template_values(c, j, interview_type: str, sched: datetime,
                               duration: int, location: str,
                               notes: str) -> dict:
    """Placeholder values for the onsite-interview template.

    `location_line` / `notes_block` are pre-rendered whole lines so a template
    that includes them stays clean when the field is empty — a recruiter
    should not have to write conditionals in a textarea.
    """
    type_label = _TYPE_LABELS.get(interview_type, interview_type)
    loc_label = "Link" if interview_type == "video" else "Location"
    return {
        "candidate_name": c.name or "Candidate",
        "candidate_email": c.email or "",
        "job_title": j.title,
        "interview_type": type_label,
        "date_time": sched.strftime("%A, %B %d, %Y at %I:%M %p"),
        "duration": duration,
        "location": location or "",
        "location_line": f"- {loc_label}: {location}\n" if location else "",
        "notes": notes or "",
        "notes_block": f"\nAdditional notes:\n{notes}\n" if notes else "",
    }


@router.get("/jobs/{job_uuid}/interviews")
def list_interviews(job_uuid: str, user: User = Depends(current_admin),
                    db=Depends(get_db)):
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    interviews = db.execute(
        select(Interview).where(Interview.job_uuid == job_uuid)
        .order_by(Interview.scheduled_at.desc())).scalars().all()
    return [{
        "id": iv.id,
        "candidate_uuid": iv.candidate_uuid,
        "candidate_name": iv.candidate.name,
        "candidate_email": iv.candidate.email,
        "interview_type": iv.interview_type,
        "scheduled_at": iv.scheduled_at.isoformat(),
        "duration_minutes": iv.duration_minutes,
        "location": iv.location,
        "notes": iv.notes,
        "status": iv.status,
    } for iv in interviews]


@router.patch("/interviews/{interview_id}")
def update_interview(interview_id: int, body: InterviewUpdate,
                     user: User = Depends(current_admin), db=Depends(get_db)):
    iv = db.get(Interview, interview_id)
    if not iv:
        raise HTTPException(404, "No such interview.")
    _require_job_access(user, iv.job)

    if body.interview_type is not None:
        if body.interview_type not in ("in_person", "phone", "video"):
            raise HTTPException(422, "Invalid interview_type.")
        iv.interview_type = body.interview_type
    if body.scheduled_at is not None:
        try:
            iv.scheduled_at = datetime.fromisoformat(body.scheduled_at)
        except ValueError:
            raise HTTPException(422, "Invalid datetime format.")
    if body.duration_minutes is not None:
        iv.duration_minutes = body.duration_minutes
    if body.location is not None:
        iv.location = body.location
    if body.notes is not None:
        iv.notes = body.notes
    if body.status is not None:
        if body.status not in ("scheduled", "completed", "cancelled"):
            raise HTTPException(422, "Invalid status.")
        iv.status = body.status

    # Send update email
    c = iv.candidate
    j = iv.job
    type_label = _TYPE_LABELS.get(iv.interview_type, iv.interview_type)
    date_str = iv.scheduled_at.strftime("%A, %B %d, %Y at %I:%M %p")
    location_line = ""
    if iv.location:
        loc_label = "Link" if iv.interview_type == "video" else "Location"
        location_line = f"- {loc_label}: {iv.location}\n"

    email_body = (
        f"Dear {c.name or 'Candidate'},\n\n"
        f"Your interview for the {j.title} position has been updated.\n\n"
        f"Updated Details:\n"
        f"- Type: {type_label}\n"
        f"- Date & Time: {date_str}\n"
        f"- Duration: {iv.duration_minutes} minutes\n"
        f"{location_line}\n"
        f"Please confirm your availability by replying to this email.\n\n"
        f"Best regards,\nThe Recruiting Team\n"
    )
    mailer.send_email(c.email, f"{j.title} — Interview Updated", email_body)

    return {"id": iv.id, "status": iv.status}


@router.delete("/interviews/{interview_id}")
def cancel_interview(interview_id: int, user: User = Depends(current_admin),
                     db=Depends(get_db)):
    iv = db.get(Interview, interview_id)
    if not iv:
        raise HTTPException(404, "No such interview.")
    _require_job_access(user, iv.job)

    c = iv.candidate
    j = iv.job
    date_str = iv.scheduled_at.strftime("%A, %B %d, %Y at %I:%M %p")

    # Send cancellation email
    email_body = (
        f"Dear {c.name or 'Candidate'},\n\n"
        f"We regret to inform you that your interview for the {j.title} "
        f"position scheduled for {date_str} has been cancelled.\n\n"
        f"We will be in touch if there are any updates.\n\n"
        f"Best regards,\nThe Recruiting Team\n"
    )
    mailer.send_email(
        c.email, f"{j.title} — Interview Cancelled", email_body)

    iv.status = "cancelled"
    return {"deleted": True, "id": interview_id}


# ============================================================================
# Hiring pipeline: stages, per-candidate stage moves, sending a test to ANY
# candidate (portal applicants included), resume viewing, and the cross-role
# applications inbox. Stages are global (department_id NULL).
# ============================================================================

def _stage_out(st: PipelineStage) -> dict:
    return {"id": st.id, "name": st.name, "sort_order": st.sort_order,
            "kind": st.kind, "color": st.color}


def _global_stages(db) -> list[PipelineStage]:
    return list(db.execute(
        select(PipelineStage)
        .where(PipelineStage.department_id.is_(None))
        .order_by(PipelineStage.sort_order)).scalars().all())


def _advance_stage(db, candidate: Candidate, name: str) -> None:
    """Move a candidate FORWARD to a named stage on a pipeline event (test
    sent, interview scheduled). Never moves them backwards, so the candidate's
    visible phase tracks reality without anyone dragging cards around."""
    stages = _global_stages(db)
    order = {s.id: s.sort_order for s in stages}
    target = next((s for s in stages if s.name == name), None)
    if not target:
        return
    if target.sort_order > order.get(candidate.stage_id, -1):
        candidate.stage_id = target.id


@router.get("/pipeline-stages")
def list_stages(user: User = Depends(current_admin), db=Depends(get_db)):
    return [_stage_out(s) for s in _global_stages(db)]


class StageMove(BaseModel):
    stage_id: int


@router.patch("/candidates/{candidate_uuid}/stage")
def move_candidate(candidate_uuid: str, body: StageMove,
                   user: User = Depends(current_admin), db=Depends(get_db)):
    """Set a candidate's pipeline stage (the applications inbox dropdown so HR
    can mark Screening/Offer/Hired/Rejected without a Kanban board)."""
    c = db.get(Candidate, candidate_uuid)
    if not c:
        raise HTTPException(404, "No such candidate.")
    job = db.get(Job, c.job_uuid)
    _require_job_access(user, job)
    st = db.get(PipelineStage, body.stage_id)
    if not st or st.department_id is not None:
        raise HTTPException(404, "No such stage.")
    c.stage_id = st.id
    return {"uuid": c.uuid, "stage_id": c.stage_id, "stage": st.name}


class SendTestIn(BaseModel):
    duration_minutes: int = Field(default=20, ge=1, le=240)
    questions_per_candidate: int = Field(default=0, ge=0)  # 0 = whole bank
    pass_score: int = Field(default=60, ge=0, le=100)
    expiry_days: int = Field(default=7, ge=1, le=90)
    proctored: bool = True
    max_warnings: int = Field(default=3, ge=1, le=20)
    difficulty: str = "medium"


def _build_test_pool(db, j, cfg):
    """Create the Test row + its Question pool from the job's question bank.
    Raises 400 if the bank is empty. Shared by single + bulk dispatch."""
    items = bank.list_items(db, j.uuid)
    if not items:
        raise HTTPException(
            400, "This job has no question bank yet. Build one in the "
                 "recruiter app (MCQ tab), then send the test.")
    db_test = Test(job_uuid=j.uuid, difficulty=cfg.difficulty,
                   duration_minutes=cfg.duration_minutes,
                   pass_score=cfg.pass_score, proctored=cfg.proctored,
                   max_warnings=cfg.max_warnings, approved=True,
                   questions_per_candidate=cfg.questions_per_candidate)
    db.add(db_test)
    db.flush()
    pool = []
    for i in items:
        q = bank.to_mcq(i)
        row = Question(test_uuid=db_test.uuid, question=q.question,
                       options_json=list(q.options),
                       correct_index=q.correct_index,
                       category=getattr(q, "category", "") or "")
        db.add(row)
        pool.append(row)
    db.flush()
    return db_test, pool


def _assign_and_email(db, c, j, db_test, pool, cfg) -> dict:
    """Draw one candidate's paper from a built pool, email the link, advance
    their stage. The candidate must already be eligible (checked by callers)."""
    expires_at = datetime.utcnow() + timedelta(days=cfg.expiry_days)
    a = TestAssignment(test_uuid=db_test.uuid, candidate_uuid=c.uuid,
                       expires_at=expires_at,
                       attempt_no=attempts.next_attempt_no(db, j.uuid, c.uuid))
    db.add(a)
    db.flush()
    paper = assign_questions(db, a, pool=pool)
    c.status = "invited"
    _advance_stage(db, c, "Test")

    link = f"{_PORTAL_BASE}/test/{a.uuid}"
    email_body = (
        f"Dear {c.name or 'Candidate'},\n\n"
        f"You have been invited to complete the online assessment for the "
        f"{j.title} position.\n\n"
        f"Start your assessment here:\n{link}\n\n"
        f"Details:\n"
        f"- {len(paper)} multiple-choice questions\n"
        f"- Time limit: {cfg.duration_minutes} minutes (starts when you open "
        f"the test)\n"
        f"- The link expires on "
        f"{expires_at.strftime('%b %d, %Y at %I:%M %p')} and works only once\n"
        f"- You verify your identity with a one-time code sent to this email\n\n"
        f"Best regards,\nThe Recruiting Team")
    sent, msg = mailer.send_email(
        c.email, f"{j.title} - Online Assessment Invitation", email_body)
    return {"candidate_uuid": c.uuid, "name": c.name, "assignment_uuid": a.uuid,
            "link": link, "num_questions": len(paper),
            "email_sent": sent, "email_message": msg}


@router.post("/candidates/{candidate_uuid}/send-test")
def send_test(candidate_uuid: str, body: SendTestIn,
              user: User = Depends(current_admin), db=Depends(get_db)):
    """Build a paper from the job's question bank and email a unique test link
    to one candidate. This is the path that lets HR test PORTAL applicants, who
    never go through the recruiter app's upload+screen batch."""
    c = db.get(Candidate, candidate_uuid)
    if not c:
        raise HTTPException(404, "No such candidate.")
    if not c.email:
        raise HTTPException(400, "Candidate has no email address.")
    j = db.get(Job, c.job_uuid)
    _require_job_access(user, j)
    if attempts.live_attempt(db, j.uuid, c.uuid) is not None:
        raise HTTPException(
            409, "This candidate already has an active test link. Reset it "
                 "from their attempts view before re-sending.")

    db_test, pool = _build_test_pool(db, j, body)
    res = _assign_and_email(db, c, j, db_test, pool, body)
    cur_stage = db.get(PipelineStage, c.stage_id) if c.stage_id else None
    return {**res, "stage": cur_stage.name if cur_stage else "Test"}


class BulkSendTestIn(SendTestIn):
    candidate_uuids: list[str]


@router.post("/jobs/{job_uuid}/send-tests")
def send_tests_bulk(job_uuid: str, body: BulkSendTestIn,
                    user: User = Depends(current_admin), db=Depends(get_db)):
    """Send the same assessment (one shared pool; each candidate still draws
    their own paper) to several candidates of a job at once. Ineligible
    candidates are skipped with a reason rather than failing the whole batch."""
    j = db.get(Job, job_uuid)
    if not j:
        raise HTTPException(404, "No such job.")
    _require_job_access(user, j)
    if not body.candidate_uuids:
        raise HTTPException(400, "No candidates selected.")

    db_test = pool = None
    sent, skipped = [], []
    for cu in body.candidate_uuids:
        c = db.get(Candidate, cu)
        if not c or c.job_uuid != j.uuid:
            skipped.append({"candidate_uuid": cu, "reason": "not on this job"})
            continue
        if not c.email:
            skipped.append({"candidate_uuid": cu, "name": c.name,
                            "reason": "no email address"})
            continue
        if attempts.live_attempt(db, j.uuid, c.uuid) is not None:
            skipped.append({"candidate_uuid": cu, "name": c.name,
                            "reason": "already has an active test link"})
            continue
        if db_test is None:
            db_test, pool = _build_test_pool(db, j, body)  # 400 if no bank
        sent.append(_assign_and_email(db, c, j, db_test, pool, body))

    return {"sent": sent, "skipped": skipped,
            "sent_count": len(sent), "skipped_count": len(skipped)}


def _admin_from_token_or_header(token, authorization, db) -> User:
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1]
    elif token:
        raw = token
    payload = security.read_token(raw) if raw else None
    if not payload or payload.get("kind") != "admin":
        raise HTTPException(401, "Admin authentication required.")
    u = db.get(User, payload.get("sub"))
    if not u:
        raise HTTPException(401, "Unknown user.")
    return u


@router.get("/candidates/{candidate_uuid}/resume")
def candidate_resume(candidate_uuid: str, token: str | None = None,
                     authorization: str | None = Header(default=None),
                     db=Depends(get_db)):
    """Stream a candidate's uploaded resume PDF. Accepts the admin token via
    the Authorization header OR a ?token= query param so HR can open it in a
    new browser tab."""
    user = _admin_from_token_or_header(token, authorization, db)
    c = db.get(Candidate, candidate_uuid)
    if not c:
        raise HTTPException(404, "No such candidate.")
    j = db.get(Job, c.job_uuid)
    _require_job_access(user, j)
    if not c.resume_path or not os.path.exists(c.resume_path):
        raise HTTPException(
            404, "No uploaded resume on file (only portal applications store "
                 "the original PDF).")
    filename = f"{(c.name or 'resume').replace(' ', '_')}.pdf"
    return FileResponse(c.resume_path, media_type="application/pdf",
                        filename=filename)


@router.get("/applications")
def applications_inbox(department_id: int | None = None,
                       job_uuid: str | None = None,
                       stage_id: int | None = None,
                       source: str | None = None,
                       q: str | None = None,
                       limit: int = 200,
                       user: User = Depends(current_admin), db=Depends(get_db)):
    """Every application across roles, newest first, with filters. Non-super
    admins only ever see their own department."""
    query = select(Candidate, Job, PipelineStage).join(
        Job, Candidate.job_uuid == Job.uuid).outerjoin(
        PipelineStage, Candidate.stage_id == PipelineStage.id)

    if user.role != "super_admin":
        query = query.where(Job.department_id == user.department_id)
    elif department_id is not None:
        query = query.where(Job.department_id == department_id)
    if job_uuid:
        query = query.where(Candidate.job_uuid == job_uuid)
    if stage_id is not None:
        query = query.where(Candidate.stage_id == stage_id)
    if source:
        query = query.where(Candidate.source == source)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            (Candidate.name.ilike(like)) | (Candidate.email.ilike(like)))

    query = query.order_by(Candidate.created_at.desc()).limit(limit)
    rows = db.execute(query).all()
    return {"applications": [{
        "uuid": c.uuid, "name": c.name or "(no name)", "email": c.email,
        "resume_score": c.resume_score, "status": c.status,
        "source": c.source,
        "has_resume": bool(c.resume_path),
        "stage": st.name if st else None,
        "stage_id": c.stage_id,
        "job_uuid": j.uuid, "job_title": j.title,
        "department": j.department.name,
        "applied_at": c.created_at.isoformat() if c.created_at else None,
    } for c, j, st in rows]}
