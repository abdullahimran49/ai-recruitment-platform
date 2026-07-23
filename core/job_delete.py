"""The one implementation of "delete a job and everything under it".

Both portals delete jobs (the admin API by uuid, Streamlit by title) and both
used to carry their own copy of the cascade. They drifted, twice: the Streamlit
copy silently lacked proctor_events and then ai_interviews, so deleting any job
whose candidates had been proctored or interviewed failed with an
IntegrityError. Keep exactly one implementation here.

Order matters. The relationships have no delete cascade, so a parent deleted
while a child still references it makes SQLAlchemy try to NULL the child's
foreign key — which every NOT NULL child column rejects. Always delete
child-first, and use bulk deletes so the ORM does not "helpfully" null
anything on autoflush.
"""

from sqlalchemy import delete, select

from core.models import (
    AIInterview,
    AssignmentQuestion,
    Candidate,
    CandidateAnswer,
    EmailTemplate,
    Interview,
    InterviewEvent,
    Job,
    ProctorEvent,
    Question,
    QuestionBankCategory,
    QuestionBankItem,
    Scorecard,
    Test,
    TestAssignment,
)


def cascade_delete_job(s, job: Job) -> dict:
    """Delete `job` and every row that hangs off it. Returns a count summary."""
    cand_ids = list(s.execute(select(Candidate.uuid).where(
        Candidate.job_uuid == job.uuid)).scalars().all())
    test_ids = list(s.execute(select(Test.uuid).where(
        Test.job_uuid == job.uuid)).scalars().all())

    asg_ids = []
    if cand_ids:
        asg_ids = list(s.execute(select(TestAssignment.uuid).where(
            TestAssignment.candidate_uuid.in_(cand_ids))).scalars().all())
    # Assignments can also hang off this job's tests even if the candidate row
    # has already gone (defensive: keeps a partial earlier delete recoverable).
    if test_ids:
        asg_ids += [a for a in s.execute(select(TestAssignment.uuid).where(
            TestAssignment.test_uuid.in_(test_ids))).scalars().all()
            if a not in asg_ids]

    ai_ids = list(s.execute(select(AIInterview.uuid).where(
        AIInterview.job_uuid == job.uuid)).scalars().all())
    if cand_ids:
        ai_ids += [i for i in s.execute(select(AIInterview.uuid).where(
            AIInterview.candidate_uuid.in_(cand_ids))).scalars().all()
            if i not in ai_ids]

    counts = {"candidates": len(cand_ids), "tests": len(test_ids),
              "assignments": len(asg_ids), "ai_interviews": len(ai_ids)}

    # Level 3: rows hanging off assignments.
    if asg_ids:
        s.execute(delete(CandidateAnswer).where(
            CandidateAnswer.assignment_uuid.in_(asg_ids)))
        s.execute(delete(ProctorEvent).where(
            ProctorEvent.assignment_uuid.in_(asg_ids)))
        s.execute(delete(AssignmentQuestion).where(
            AssignmentQuestion.assignment_uuid.in_(asg_ids)))
        s.execute(delete(TestAssignment).where(
            TestAssignment.uuid.in_(asg_ids)))

    # Level 3: rows hanging off AI interviews.
    if ai_ids:
        s.execute(delete(InterviewEvent).where(
            InterviewEvent.interview_uuid.in_(ai_ids)))
        s.execute(delete(AIInterview).where(AIInterview.uuid.in_(ai_ids)))

    # Level 2: rows hanging off tests / the job / candidates.
    if test_ids:
        s.execute(delete(Question).where(Question.test_uuid.in_(test_ids)))
        s.execute(delete(Test).where(Test.uuid.in_(test_ids)))
    s.execute(delete(Interview).where(Interview.job_uuid == job.uuid))
    if cand_ids:
        s.execute(delete(Interview).where(
            Interview.candidate_uuid.in_(cand_ids)))
    s.execute(delete(EmailTemplate).where(EmailTemplate.job_uuid == job.uuid))
    # Bank items reference their category, so items go first.
    s.execute(delete(QuestionBankItem).where(
        QuestionBankItem.job_uuid == job.uuid))
    s.execute(delete(QuestionBankCategory).where(
        QuestionBankCategory.job_uuid == job.uuid))

    # Level 1. Scorecards hang off candidates and must go before them; the
    # Applicant a portal candidate points to is a shared person record and is
    # deliberately NOT deleted (they may have other applications).
    if cand_ids:
        s.execute(delete(Scorecard).where(
            Scorecard.candidate_uuid.in_(cand_ids)))
        s.execute(delete(Candidate).where(Candidate.uuid.in_(cand_ids)))
    s.execute(delete(Job).where(Job.uuid == job.uuid))
    return counts
