"""Test attempt history.

A candidate can sit a job's test more than once. Every attempt is its own
TestAssignment row with its own link and its own drawn paper; resetting never
edits or deletes an attempt, it supersedes it. So the answers, score,
proctoring log and evidence of every past attempt stay in the database (and
stay viewable) forever.

Invariant: a candidate has at most ONE live attempt per job
(superseded_at IS NULL). Everything else is history.
"""

from datetime import datetime

from sqlalchemy import select

from core.models import Test, TestAssignment


def attempts_for(s, job_uuid: str, candidate_uuid: str) -> list[TestAssignment]:
    """Every attempt this candidate has made at this job, oldest first."""
    rows = list(s.execute(
        select(TestAssignment)
        .join(Test, TestAssignment.test_uuid == Test.uuid)
        .where(Test.job_uuid == job_uuid,
               TestAssignment.candidate_uuid == candidate_uuid)
    ).scalars().all())
    # attempt_no is the intended order; created_at breaks ties for rows that
    # predate it (all of which are attempt 1).
    rows.sort(key=lambda a: (a.attempt_no or 1,
                             a.created_at or datetime.min))
    return rows


def next_attempt_no(s, job_uuid: str, candidate_uuid: str) -> int:
    existing = attempts_for(s, job_uuid, candidate_uuid)
    return max((a.attempt_no or 1) for a in existing) + 1 if existing else 1


def live_attempt(s, job_uuid: str, candidate_uuid: str) -> TestAssignment | None:
    """The one attempt still in play, if any."""
    return next((a for a in reversed(attempts_for(s, job_uuid, candidate_uuid))
                 if not a.superseded_at), None)


def supersede(a: TestAssignment, by: str = "", reason: str = "",
              when: datetime | None = None) -> None:
    """Retire an attempt, freezing it as history.

    Nothing is cleared: the answers, score, timestamps and proctoring events
    are the record of what happened and are what the "past attempts" view
    reads back. `by`/`reason` are the audit trail — a voided attempt that
    nobody owns cannot be defended if the candidate challenges it.
    """
    a.superseded_at = when or datetime.utcnow()
    a.superseded_by = (by or "")[:255]
    a.reset_reason = (reason or "")[:400]


def summarise(a: TestAssignment) -> dict:
    """One past/current attempt, shaped for the admin UI."""
    from core.question_sets import questions_for

    n_q = len(questions_for(a))
    passed = None
    if a.status == "submitted" and a.test_score is not None:
        passed = a.test_score >= a.test.pass_score
    taken = None
    if a.started_at and a.submitted_at:
        taken = int((a.submitted_at - a.started_at).total_seconds())
    return {
        "assignment_uuid": a.uuid,
        "attempt_no": a.attempt_no or 1,
        "status": a.status,
        "superseded": bool(a.superseded_at),
        "superseded_at": a.superseded_at.isoformat() if a.superseded_at else None,
        "superseded_by": a.superseded_by or "",
        "reset_reason": a.reset_reason or "",
        "test_score": a.test_score,
        "pass_score": a.test.pass_score,
        "passed": passed,
        "num_questions": n_q,
        "difficulty": a.test.difficulty,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "started_at": a.started_at.isoformat() if a.started_at else None,
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "time_taken_seconds": taken,
        "proctor_warnings": a.proctor_warnings or 0,
        "terminated_reason": a.terminated_reason,
    }
