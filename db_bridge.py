"""Streamlit-side MySQL writer.

The Streamlit app stays fully functional without MySQL (file/session storage);
when MYSQL_URL is configured this bridge persists screenings and creates
per-candidate test links for the portal.
"""

import os
from datetime import datetime, timedelta

from sqlalchemy import select

from core.attempts import next_attempt_no as _next_attempt_no
from core.db import db_enabled, session
from core.models import Candidate, Job, Question, Test, TestAssignment
from core.question_sets import assign_questions
from schemas import MCQTest, ResumeResult

PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "http://localhost:3000").rstrip("/")


def list_departments() -> list[tuple[int, str]]:
    from core.models import Department
    with session() as s:
        return [(d.id, d.name)
                for d in s.execute(select(Department)).scalars().all()]


def list_saved_jobs() -> dict:
    """Return {job_title: {jd, must, nice, settings, penalties, department_id,
    department_name}} for every job in the database, newest first.

    The full screening config (criteria + settings + penalty rules) is kept
    in Job.criteria_json so loading a job restores everything.
    """
    out: dict = {}
    with session() as s:
        jobs = s.execute(
            select(Job).order_by(Job.created_at.desc())).scalars().all()
        for j in jobs:
            cfg = j.criteria_json or {}
            out[j.title] = {
                "uuid": j.uuid,
                "jd": j.jd_text or "",
                "must": cfg.get("must", []),
                "nice": cfg.get("nice", []),
                "settings": cfg.get("settings", {}),
                "penalties": cfg.get("penalties", []),
                "pass_threshold": j.pass_threshold,
                "department_id": j.department_id,
                "department_name": j.department.name if j.department else "",
            }
    return out


def load_job_resume_files(job_uuid: str) -> tuple[list, int]:
    """The actual uploaded resume PDFs of everyone who applied to this job but
    has NOT yet been sent a test.

    Returns ``(files, n_hidden)`` where each file is
    ``{filename, bytes, candidate_uuid, email, name}`` — the real PDF, so the
    recruiter app can drop them straight into its normal Screen -> Review ->
    Dispatch flow (HR scores them against the current criteria, exactly like a
    manual upload). ``n_hidden`` counts applicants left out because they already
    have a live test link or have withdrawn — they must not reappear once
    invited. Applicants with no stored PDF on disk are skipped.
    """
    files: list = []
    n_hidden = 0
    with session() as s:
        cands = s.execute(
            select(Candidate).where(Candidate.job_uuid == job_uuid)
            .order_by(Candidate.resume_score.desc())).scalars().all()
        for c in cands:
            if (c.status or "") == "withdrawn":
                n_hidden += 1
                continue
            # Already invited to a test? A live (non-superseded) assignment of
            # ANY status means "already sent a test" — hide them.
            live = s.execute(select(TestAssignment).where(
                TestAssignment.candidate_uuid == c.uuid,
                TestAssignment.superseded_at.is_(None))).scalars().first()
            if live is not None:
                n_hidden += 1
                continue
            if not c.resume_path or not os.path.exists(c.resume_path):
                continue  # HR-uploaded rows / older portal rows have no file
            try:
                with open(c.resume_path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            # The uuid4-prefixed basename is already unique per candidate.
            files.append({
                "filename": os.path.basename(c.resume_path),
                "bytes": data,
                "candidate_uuid": c.uuid,
                "email": c.email,
                "name": c.name,
            })
    return files, n_hidden


def delete_job_by_title(title: str) -> None:
    """Delete a job and every dependent row, by title.

    Delegates to the shared cascade (core/job_delete.py): this used to be a
    second hand-maintained copy and it silently drifted out of sync, so
    deleting a job whose candidates had proctoring events or AI interviews
    failed with an IntegrityError.
    """
    from core.job_delete import cascade_delete_job

    with session() as s:
        job = s.execute(select(Job).where(
            Job.title == title)).scalars().first()
        if job is None:
            return
        cascade_delete_job(s, job)


def save_screening(
    job_title: str,
    department_id: int,
    jd_text: str,
    criteria_json: dict,
    pass_threshold: int,
    results: list[ResumeResult],
) -> tuple[str, dict]:
    """Upsert the job and its screened candidates.

    Returns (job_uuid, {result_filename: candidate_uuid}). Candidates are
    matched by email within the job, so re-running a screening updates
    scores instead of duplicating people.
    """
    mapping: dict[str, str] = {}
    with session() as s:
        job = s.execute(select(Job).where(
            Job.title == job_title,
            Job.department_id == department_id)).scalars().first()
        if job is None:
            job = Job(title=job_title, department_id=department_id)
            s.add(job)
            s.flush()
        job.jd_text = jd_text
        job.criteria_json = criteria_json
        job.pass_threshold = pass_threshold

        for r in results:
            if r.error:
                continue
            email = (r.email or "").strip().lower()
            existing = None
            if email:
                existing = s.execute(select(Candidate).where(
                    Candidate.job_uuid == job.uuid,
                    Candidate.email == email)).scalars().first()
            if existing is None:
                existing = Candidate(job_uuid=job.uuid)
                s.add(existing)
            existing.name = r.candidate_name
            existing.email = email
            existing.phone = (r.structured.phone if r.structured else "") or ""
            existing.resume_score = r.score
            existing.screening_json = {
                "raw_score": r.raw_score,
                "must_have_gaps": r.must_have_gaps,
                "employment_gaps": r.employment_gaps,
                "gap_penalty": r.gap_penalty,
                "criteria": [cs.model_dump() for cs in r.criterion_scores],
                # The parsed CV itself: the AI interviewer builds its questions
                # from this, so it must survive the screening run. Candidates
                # screened before this was stored fall back to the criterion
                # evidence quotes (see core/cv_brief.py).
                "structured": (r.structured.model_dump()
                               if r.structured else None),
            }
            existing.status = "screened"
            s.flush()
            mapping[r.filename] = existing.uuid

        return job.uuid, mapping


# ---- Question bank (Streamlit side) ------------------------------------------

def bank_overview(job_uuid: str, include_retired: bool = False) -> dict:
    """Everything the bank UI needs in one read: categories, their questions,
    and the active counts that constrain a blueprint."""
    from core import bank
    with session() as s:
        cats = [{"id": c.id, "name": c.name}
                for c in bank.list_categories(s, job_uuid)]
        items = [{
            "id": i.id, "question": i.question,
            "options": list(i.options_json or []),
            "correct_index": i.correct_index,
            "explanation": i.explanation or "",
            "difficulty": i.difficulty,
            "category_id": i.category_id,
            "category": i.category.name if i.category else "",
            "source": i.source, "times_used": i.times_used or 0,
            "active": bool(i.active),
        } for i in bank.list_items(s, job_uuid,
                                   active_only=not include_retired)]
        return {"categories": cats, "items": items,
                "counts": bank.counts_by_category(s, job_uuid)}


def bank_update_item(item_id: int, **fields) -> bool:
    """Edit a bank question (text/options/answer key/category/retire)."""
    from core import bank
    with session() as s:
        bank.update_item(s, item_id, **fields)
        return True


def bank_add_category(job_uuid: str, name: str) -> int:
    from core import bank
    with session() as s:
        return bank.add_category(s, job_uuid, name).id


def bank_delete_category(cat_id: int) -> int:
    from core import bank
    with session() as s:
        return bank.delete_category(s, cat_id)


def bank_add_questions(job_uuid: str, questions, category_id: int | None,
                       difficulty: str = "medium",
                       source: str = "custom") -> int:
    from core import bank
    with session() as s:
        return len(bank.add_items(s, job_uuid, questions, category_id,
                                  difficulty, source))


def bank_delete_item(item_id: int) -> bool:
    from core import bank
    with session() as s:
        return bank.delete_item(s, item_id)


def bank_pick(job_uuid: str, item_ids: list[int]) -> list:
    """Copy the chosen bank items out as MCQQuestions for a test pool."""
    from core import bank
    with session() as s:
        return bank.pick(s, job_uuid, item_ids)


def create_test_with_assignments(
    job_uuid: str,
    test: MCQTest,
    candidate_uuids: list[str],
    duration_minutes: int,
    expires_at: datetime | None = None,
    expiry_days: int = 7,
    pass_score: int = 60,
    proctored: bool = True,
    max_warnings: int = 3,
    questions_per_candidate: int = 0,
    blueprint: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """Persist the approved question pool, then create one assignment (= link
    token) per candidate, each with its OWN paper drawn from that pool.

    Returns ([{name, email, link, token, num_questions}, ...], [skipped_names]).
    `questions_per_candidate` of 0 gives everyone the whole pool (the old
    behaviour); any smaller number means every link differs.

    If a candidate already has a submitted assignment for any test under the
    same job, they are skipped (prevents duplicate testing).
    """
    out = []
    skipped = []
    if expires_at is None:
        expires_at = datetime.utcnow() + timedelta(days=expiry_days)
    with session() as s:
        db_test = Test(job_uuid=job_uuid, difficulty=test.difficulty,
                       duration_minutes=duration_minutes,
                       pass_score=pass_score, proctored=proctored,
                       max_warnings=max_warnings, approved=True,
                       questions_per_candidate=questions_per_candidate,
                       blueprint_json=blueprint or None)
        s.add(db_test)
        s.flush()
        pool = []
        for q in test.questions:
            row = Question(test_uuid=db_test.uuid, question=q.question,
                           options_json=list(q.options),
                           correct_index=q.correct_index,
                           category=getattr(q, "category", "") or "")
            s.add(row)
            pool.append(row)
        # Question ids are needed to record each candidate's draw.
        s.flush()

        for cu in candidate_uuids:
            cand = s.get(Candidate, cu)
            if cand is None or not cand.email:
                continue
            # Already sat a LIVE test for this job? Superseded attempts are
            # history from a reset and must not block a fresh dispatch.
            existing = s.execute(
                select(TestAssignment)
                .join(Test, TestAssignment.test_uuid == Test.uuid)
                .where(
                    Test.job_uuid == job_uuid,
                    TestAssignment.candidate_uuid == cu,
                    TestAssignment.superseded_at.is_(None),
                    TestAssignment.status == "submitted",
                )
            ).scalars().first()
            if existing:
                skipped.append(cand.name or cand.email)
                continue
            a = TestAssignment(test_uuid=db_test.uuid, candidate_uuid=cu,
                               expires_at=expires_at,
                               attempt_no=_next_attempt_no(s, job_uuid, cu))
            s.add(a)
            cand.status = "invited"
            s.flush()
            paper = assign_questions(s, a, pool=pool)
            out.append({
                "name": cand.name, "email": cand.email,
                "token": a.uuid,
                "num_questions": len(paper),
                "link": f"{PORTAL_BASE_URL}/test/{a.uuid}",
            })
    return out, skipped
