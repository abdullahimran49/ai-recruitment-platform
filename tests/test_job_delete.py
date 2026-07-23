"""The job cascade must cover every child table.

This has broken twice. Both portals delete jobs and each carried its own copy
of the cascade; the Streamlit copy silently lacked proctor_events, and later
ai_interviews. Deleting a job whose candidates had been proctored or
interviewed failed with an IntegrityError — because the relationships have no
delete cascade, so SQLAlchemy tries to NULL a NOT NULL foreign key instead.

There is now ONE implementation (core/job_delete.py). This file is the guard:
it builds a job with a row in EVERY child table and deletes it. Any new child
table that is forgotten will fail here rather than in production.
"""

from sqlalchemy import func, select

import db_bridge
from conftest import answer_paper, make_pool
from core import bank
from core.db import session
from core.models import (
    AIInterview, AssignmentQuestion, Candidate, CandidateAnswer, EmailTemplate,
    Interview, InterviewEvent, Job, ProctorEvent, Question,
    QuestionBankCategory, QuestionBankItem, Test, TestAssignment)

# Every table that hangs off a job, and how to count its rows for one job.
CHILD_TABLES = [
    ("candidates", Candidate),
    ("tests", Test),
    ("questions", Question),
    ("test_assignments", TestAssignment),
    ("candidate_answers", CandidateAnswer),
    ("assignment_questions", AssignmentQuestion),
    ("proctor_events", ProctorEvent),
    ("ai_interviews", AIInterview),
    ("interview_events", InterviewEvent),
    ("interviews", Interview),
    ("question_bank_items", QuestionBankItem),
    ("question_bank_categories", QuestionBankCategory),
    ("email_templates", EmailTemplate),
]


def _fully_populated_job(client, admin_headers, make_job, make_candidate):
    """A job with at least one row in every single child table."""
    job = make_job()
    cand = make_candidate(job)

    # bank category + item
    with session() as s:
        cat = bank.add_category(s, job, "Data")
        bank.add_item(s, job, "Bank question?", ["a", "b", "c", "d"], 0,
                      "why", "medium", cat.id)

    # test + questions + assignment + drawn paper + answers
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(6), [cand], duration_minutes=30, pass_score=60,
        proctored=True, questions_per_candidate=3)
    token = entries[0]["token"]
    # proctor event
    from core import security
    auth = {"Authorization": f"Bearer {security.candidate_token(token)}"}
    client.post(f"/api/portal/assignment/{token}/proctor-event", headers=auth,
                json={"event_type": "tab_switch", "detail": "left the tab"})
    answer_paper(client, token, correct_ratio=1.0)

    # ai interview + event
    with session() as s:
        iv = AIInterview(candidate_uuid=cand, job_uuid=job,
                         scheduled_at=__import__("datetime").datetime.utcnow())
        s.add(iv)
        s.flush()
        s.add(InterviewEvent(interview_uuid=iv.uuid, event_type="tab_switch",
                             detail="left the tab"))

    # human interview
    client.post("/api/admin/interviews", headers=admin_headers,
                json={"candidate_uuid": cand, "job_uuid": job,
                      "interview_type": "video",
                      "scheduled_at": "2026-09-01T10:00:00",
                      "duration_minutes": 30, "location": "meet.example/x"})

    # email template override
    client.put(f"/api/admin/jobs/{job}/email-templates/onsite_interview",
               headers=admin_headers,
               json={"subject": "s", "body": "Hi {{candidate_name}}"})
    return job, cand


def _counts(job, cand):
    """Row counts per child table for this job."""
    out = {}
    with session() as s:
        test_ids = list(s.execute(select(Test.uuid).where(
            Test.job_uuid == job)).scalars().all())
        asg_ids = list(s.execute(select(TestAssignment.uuid).where(
            TestAssignment.candidate_uuid == cand)).scalars().all())
        ai_ids = list(s.execute(select(AIInterview.uuid).where(
            AIInterview.job_uuid == job)).scalars().all())

        def n(model, col, vals):
            if not vals:
                return 0
            return s.execute(select(func.count()).select_from(model).where(
                col.in_(vals))).scalar()

        out["candidates"] = s.execute(select(func.count(Candidate.uuid)).where(
            Candidate.job_uuid == job)).scalar()
        out["tests"] = len(test_ids)
        out["questions"] = n(Question, Question.test_uuid, test_ids)
        out["test_assignments"] = len(asg_ids)
        out["candidate_answers"] = n(CandidateAnswer,
                                     CandidateAnswer.assignment_uuid, asg_ids)
        out["assignment_questions"] = n(AssignmentQuestion,
                                        AssignmentQuestion.assignment_uuid,
                                        asg_ids)
        out["proctor_events"] = n(ProctorEvent, ProctorEvent.assignment_uuid,
                                  asg_ids)
        out["ai_interviews"] = len(ai_ids)
        out["interview_events"] = n(InterviewEvent,
                                    InterviewEvent.interview_uuid, ai_ids)
        out["interviews"] = s.execute(select(func.count(Interview.id)).where(
            Interview.job_uuid == job)).scalar()
        out["question_bank_items"] = s.execute(
            select(func.count(QuestionBankItem.id)).where(
                QuestionBankItem.job_uuid == job)).scalar()
        out["question_bank_categories"] = s.execute(
            select(func.count(QuestionBankCategory.id)).where(
                QuestionBankCategory.job_uuid == job)).scalar()
        out["email_templates"] = s.execute(
            select(func.count(EmailTemplate.id)).where(
                EmailTemplate.job_uuid == job)).scalar()
    return out


def test_the_fixture_really_populates_every_child_table(
        client, admin_headers, make_job, make_candidate):
    """Guard the guard: if this job is not fully populated, the delete test
    below proves nothing."""
    job, cand = _fully_populated_job(client, admin_headers, make_job,
                                     make_candidate)
    before = _counts(job, cand)
    empty = [t for t, n in before.items() if n == 0]
    assert not empty, f"child tables with no rows to delete: {empty}"


def test_deleting_a_fully_populated_job_leaves_nothing_behind(
        client, admin_headers, make_job, make_candidate):
    job, cand = _fully_populated_job(client, admin_headers, make_job,
                                     make_candidate)
    with session() as s:
        title = s.get(Job, job).title

    db_bridge.delete_job_by_title(title)      # the path that kept breaking

    after = _counts(job, cand)
    leftovers = {t: n for t, n in after.items() if n}
    assert not leftovers, f"orphan rows left behind: {leftovers}"
    with session() as s:
        assert s.get(Job, job) is None


def test_admin_api_delete_matches(client, admin_headers, make_job,
                                  make_candidate):
    """The other delete path must behave identically — they share one cascade."""
    job, cand = _fully_populated_job(client, admin_headers, make_job,
                                     make_candidate)
    r = client.delete(f"/api/admin/jobs/{job}", headers=admin_headers)
    assert r.status_code == 200, r.text
    leftovers = {t: n for t, n in _counts(job, cand).items() if n}
    assert not leftovers, f"orphan rows left behind: {leftovers}"


def test_both_delete_paths_use_the_one_cascade():
    """Stop the two implementations re-forking."""
    import inspect

    import portal.backend.routers.admin as admin_mod
    src_admin = inspect.getsource(admin_mod._cascade_delete_job)
    src_bridge = inspect.getsource(db_bridge.delete_job_by_title)
    assert "cascade_delete_job" in src_admin
    assert "cascade_delete_job" in src_bridge
    for src, who in ((src_admin, "admin"), (src_bridge, "db_bridge")):
        assert "delete(ProctorEvent)" not in src, (
            f"{who} has grown its own cascade again — keep it in "
            f"core/job_delete.py")
