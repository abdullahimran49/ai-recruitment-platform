"""SQLAlchemy models for the ATS database (MySQL).

UUID primary keys are CHAR(36) strings. The test_assignments.uuid doubles as
the per-candidate test link token, so it must never be guessable from other
data — always uuid4.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="department")
    jobs: Mapped[list["Job"]] = relationship(back_populates="department")


class User(Base):
    """Portal users: super_admin (all access) or admin (own department only)."""
    __tablename__ = "users"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="admin")  # super_admin | admin
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    department: Mapped["Department | None"] = relationship(back_populates="users")


class Job(Base):
    __tablename__ = "jobs"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    jd_text: Mapped[str] = mapped_column(Text, default="")
    criteria_json: Mapped[dict] = mapped_column(JSON, default=dict)
    pass_threshold: Mapped[int] = mapped_column(Integer, default=60)
    # Merit Decider config: weights (resume/test/interview) + thresholds for
    # auto-inviting to the AI interview and shortlisting for onsite. Nullable;
    # a job with no config uses the defaults in the admin router.
    merit_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # --- Public job-portal fields (added with the candidate-facing portal) ---
    # A job is invisible on the public portal until published, and stops
    # accepting applications after application_deadline (NULL = no deadline).
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    application_deadline: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    location: Mapped[str] = mapped_column(String(300), default="")
    employment_type: Mapped[str] = mapped_column(String(60), default="")  # Full-time | ...
    openings: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    department: Mapped["Department"] = relationship(back_populates="jobs")
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="job")
    tests: Mapped[list["Test"]] = relationship(back_populates="job")
    bank_categories: Mapped[list["QuestionBankCategory"]] = relationship(
        back_populates="job", order_by="QuestionBankCategory.sort_order")
    bank_items: Mapped[list["QuestionBankItem"]] = relationship(
        back_populates="job")


class QuestionBankCategory(Base):
    """A per-job bucket of bank questions, e.g. "Data" / "AI" / "General".

    Free-form per job (an LLM proposes them from the JD; HR edits), because a
    fixed taxonomy cannot describe both a data role and a warehouse role.
    Category names are what tests.blueprint_json keys refer to.
    """
    __tablename__ = "question_bank_categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_uuid: Mapped[str] = mapped_column(ForeignKey("jobs.uuid"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped["Job"] = relationship(back_populates="bank_categories")
    items: Mapped[list["QuestionBankItem"]] = relationship(
        back_populates="category")


class QuestionBankItem(Base):
    """A reusable question saved against a job, ready to be picked into a test.

    The bank is the durable library; Question rows are per-test COPIES taken
    when a test is built. The copy is deliberate: editing or retiring a bank
    item must never rewrite a paper someone has already sat.
    """
    __tablename__ = "question_bank_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_uuid: Mapped[str] = mapped_column(ForeignKey("jobs.uuid"))
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_bank_categories.id"), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[list] = mapped_column(JSON, default=list)  # 4 strings
    correct_index: Mapped[int] = mapped_column(SmallInteger, default=0)
    explanation: Mapped[str] = mapped_column(Text, default="")
    difficulty: Mapped[str] = mapped_column(String(20), default="medium")
    source: Mapped[str] = mapped_column(String(20), default="custom")  # custom | llm
    # Retired questions stay in the bank (and in past papers) but stop being
    # offered for new tests.
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped["Job"] = relationship(back_populates="bank_items")
    category: Mapped["QuestionBankCategory | None"] = relationship(
        back_populates="items")


class Candidate(Base):
    """One application: a person (Applicant) applying to one Job.

    Legacy rows created by the HR resume-upload flow have no applicant_uuid
    (source='upload'); portal applications link back to the Applicant that
    submitted them (source='portal'). A (job_uuid, applicant_uuid) pair is
    unique — one person cannot apply to the same job twice.
    """
    __tablename__ = "candidates"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_uuid: Mapped[str] = mapped_column(ForeignKey("jobs.uuid"))
    # NULL for pre-portal / HR-uploaded candidates.
    applicant_uuid: Mapped[str | None] = mapped_column(
        ForeignKey("applicants.uuid"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    resume_score: Mapped[float] = mapped_column(Float, default=0)
    screening_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="screened")
    # How this application entered the system: upload (HR) | portal | manual.
    source: Mapped[str] = mapped_column(String(20), default="upload")
    # Filesystem path of the uploaded resume (portal applications).
    resume_path: Mapped[str] = mapped_column(String(500), default="")
    # Which Kanban column the candidate sits in. NULL = not yet placed; the
    # legacy `status` string remains the source of truth for automation.
    stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_stages.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped["Job"] = relationship(back_populates="candidates")
    applicant: Mapped["Applicant | None"] = relationship(
        back_populates="candidates")
    stage: Mapped["PipelineStage | None"] = relationship()
    assignments: Mapped[list["TestAssignment"]] = relationship(
        back_populates="candidate")
    interviews: Mapped[list["Interview"]] = relationship(
        back_populates="candidate")
    interviews_ai: Mapped[list["AIInterview"]] = relationship(
        back_populates="candidate")
    scorecards: Mapped[list["Scorecard"]] = relationship(
        back_populates="candidate")


class Test(Base):
    """A test is a POOL of questions plus the rules for drawing from it.

    `questions` is the pool, not the paper: each TestAssignment draws its own
    `questions_per_candidate` subset (see core/question_sets.py), so two
    candidates on the same test never sit the same paper. A pool of 30 with
    questions_per_candidate=10 gives every link a different 10.
    """
    __tablename__ = "tests"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_uuid: Mapped[str] = mapped_column(ForeignKey("jobs.uuid"))
    difficulty: Mapped[str] = mapped_column(String(20), default="medium")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=20)
    pass_score: Mapped[int] = mapped_column(Integer, default=60)  # % to pass
    proctored: Mapped[bool] = mapped_column(Boolean, default=True)
    max_warnings: Mapped[int] = mapped_column(Integer, default=3)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    # How many of the pool each candidate sits. 0 = the whole pool (the
    # pre-question-bank behaviour, and the fallback for old rows).
    questions_per_candidate: Mapped[int] = mapped_column(Integer, default=0)
    # Optional draw plan: {category_name: how_many}, e.g. {"Data": 1, "AI": 1,
    # "General": 1}. NULL/empty = draw the whole paper at random from the pool.
    blueprint_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    job: Mapped["Job"] = relationship(back_populates="tests")
    questions: Mapped[list["Question"]] = relationship(back_populates="test")
    assignments: Mapped[list["TestAssignment"]] = relationship(back_populates="test")


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    test_uuid: Mapped[str] = mapped_column(ForeignKey("tests.uuid"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[list] = mapped_column(JSON, default=list)  # 4 strings
    correct_index: Mapped[int] = mapped_column(SmallInteger, default=0)
    # Which blueprint bucket this question counts toward ("Data", "AI", ...).
    # Empty = uncategorised; only the general fill can pick it.
    category: Mapped[str] = mapped_column(String(120), default="")

    test: Mapped["Test"] = relationship(back_populates="questions")


class TestAssignment(Base):
    """One ATTEMPT by one candidate at one test. The uuid IS the emailed link.

    Resetting a candidate never edits an attempt: it stamps `superseded_at` on
    the old row (freezing its answers, score and proctoring log as a past
    attempt) and inserts a fresh row with a new uuid — hence a new link, and a
    newly drawn paper. A candidate therefore has at most ONE live attempt
    (superseded_at IS NULL) and any number of past ones.
    """
    __tablename__ = "test_assignments"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    test_uuid: Mapped[str] = mapped_column(ForeignKey("tests.uuid"))
    candidate_uuid: Mapped[str] = mapped_column(ForeignKey("candidates.uuid"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending -> started -> submitted | terminated (proctoring violations)
    # 1-based, per candidate per job. Attempt 1 is the original invitation.
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    # Set when a reset replaces this attempt. A superseded link is dead: it
    # cannot be opened, and it is ignored by the "already tested?" checks so
    # the replacement is not blocked by the attempt it replaced.
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    # Who voided this attempt, and why. Candidates dispute rejections, and
    # "your first attempt was cancelled" is exactly what gets challenged — an
    # anonymous reset is not defensible after the fact.
    superseded_by: Mapped[str] = mapped_column(String(255), default="")
    reset_reason: Mapped[str] = mapped_column(String(400), default="")
    # Nullable: rows created before attempt history existed have no value.
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    test_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    proctor_warnings: Mapped[int] = mapped_column(Integer, default=0)
    terminated_reason: Mapped[str | None] = mapped_column(
        String(200), nullable=True)
    # Crash-safe autosave: {"question_id": selected_index, ...}. Doubles as a
    # heartbeat (last_seen) so admins can see live activity and the server can
    # auto-finalize abandoned sessions after the deadline.
    draft_answers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    test: Mapped["Test"] = relationship(back_populates="assignments")
    candidate: Mapped["Candidate"] = relationship(back_populates="assignments")
    answers: Mapped[list["CandidateAnswer"]] = relationship(
        back_populates="assignment")
    proctor_events: Mapped[list["ProctorEvent"]] = relationship(
        back_populates="assignment")
    question_links: Mapped[list["AssignmentQuestion"]] = relationship(
        back_populates="assignment", order_by="AssignmentQuestion.sort_order")


class AssignmentQuestion(Base):
    """One question on one candidate's paper, in the order it was drawn.

    Written once when the assignment is created and never recomputed, so a
    candidate's paper cannot shift under them mid-test even if the pool is
    edited afterwards. Assignments predating this table have no rows and fall
    back to the whole pool — see core.question_sets.questions_for.
    """
    __tablename__ = "assignment_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_uuid: Mapped[str] = mapped_column(
        ForeignKey("test_assignments.uuid"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    assignment: Mapped["TestAssignment"] = relationship(
        back_populates="question_links")
    question: Mapped["Question"] = relationship()


class CandidateAnswer(Base):
    __tablename__ = "candidate_answers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_uuid: Mapped[str] = mapped_column(ForeignKey("test_assignments.uuid"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    selected_index: Mapped[int] = mapped_column(SmallInteger, default=-1)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)

    assignment: Mapped["TestAssignment"] = relationship(back_populates="answers")


class Otp(Base):
    __tablename__ = "otps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProctorEvent(Base):
    """A proctoring violation/observation logged during a candidate's test.

    `evidence` optionally holds a small base64 JPEG snapshot from the
    candidate's camera at the moment of the violation.
    """
    __tablename__ = "proctor_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_uuid: Mapped[str] = mapped_column(
        ForeignKey("test_assignments.uuid"))
    event_type: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(400), default="")
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_warning: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    assignment: Mapped["TestAssignment"] = relationship(
        back_populates="proctor_events")


class AIInterview(Base):
    """A scheduled, fully-automated voice interview. The uuid IS the unique
    link token emailed to the candidate; the link only opens inside a window
    around scheduled_at."""
    __tablename__ = "ai_interviews"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    candidate_uuid: Mapped[str] = mapped_column(ForeignKey("candidates.uuid"))
    job_uuid: Mapped[str] = mapped_column(ForeignKey("jobs.uuid"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=20)
    num_questions: Mapped[int] = mapped_column(Integer, default=5)
    focus: Mapped[str] = mapped_column(String(500), default="")
    max_warnings: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    # scheduled -> started -> completed | terminated | missed | cancelled
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transcript: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # [{"role": "interviewer"|"candidate", "text": str, "at": iso}]
    questions_asked: Mapped[int] = mapped_column(Integer, default=0)
    proctor_warnings: Mapped[int] = mapped_column(Integer, default=0)
    terminated_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {"summary": str, "strengths": [...], "concerns": [...]}
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    candidate: Mapped["Candidate"] = relationship(
        back_populates="interviews_ai")
    job: Mapped["Job"] = relationship()
    events: Mapped[list["InterviewEvent"]] = relationship(
        back_populates="interview")


class InterviewEvent(Base):
    """Proctoring event during an AI interview (mirror of ProctorEvent)."""
    __tablename__ = "interview_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interview_uuid: Mapped[str] = mapped_column(ForeignKey("ai_interviews.uuid"))
    event_type: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(400), default="")
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_warning: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    interview: Mapped["AIInterview"] = relationship(back_populates="events")


class EmailTemplate(Base):
    """An HR-editable email body, optionally overridden per job.

    Lookup is job-specific first, then the global default (job_uuid NULL),
    then the hardcoded text in the router — so a job with no template still
    sends exactly what it sent before this table existed.

    Bodies use {{placeholder}} tokens (see core/templates.py).
    """
    __tablename__ = "email_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL = the organisation-wide default for this kind.
    job_uuid: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.uuid"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))  # onsite_interview | ...
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class Interview(Base):
    """Interview invitation sent by admin to a candidate."""
    __tablename__ = "interviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_uuid: Mapped[str] = mapped_column(ForeignKey("candidates.uuid"))
    job_uuid: Mapped[str] = mapped_column(ForeignKey("jobs.uuid"))
    interview_type: Mapped[str] = mapped_column(
        String(20), default="video")  # in_person | phone | video
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    location: Mapped[str] = mapped_column(
        String(500), default="")  # address or video link
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(20), default="scheduled")  # scheduled | completed | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    candidate: Mapped["Candidate"] = relationship(back_populates="interviews")
    job: Mapped["Job"] = relationship()


class Applicant(Base):
    """A person registered on the public job portal.

    One row per human, keyed by CNIC: the same CNIC can never register twice
    (unique), and a single Applicant can apply to many jobs — each application
    is its own Candidate row referencing this Applicant. This is the "global
    identity across all portals" that CNIC uniqueness is built on.
    """
    __tablename__ = "applicants"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cnic: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    phone: Mapped[str] = mapped_column(String(50), default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    candidates: Mapped[list["Candidate"]] = relationship(
        back_populates="applicant")


class PipelineStage(Base):
    """A customizable column in the hiring Kanban board.

    Ordered by sort_order. Global by default (department_id NULL) so every
    board shares one set of stages; can be scoped to a department later.
    `kind` marks terminal outcomes: active stages are the normal flow, while
    'hired'/'rejected' are the two ways a candidate leaves the pipeline.
    """
    __tablename__ = "pipeline_stages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20), default="active")  # active | hired | rejected
    color: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Scorecard(Base):
    """Structured interview feedback left by a team member on a candidate.

    Multiple team members can each leave one scorecard per candidate; the
    admin dashboard aggregates them (average overall + recommendation split).
    """
    __tablename__ = "scorecards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_uuid: Mapped[str] = mapped_column(ForeignKey("candidates.uuid"))
    author_uuid: Mapped[str] = mapped_column(ForeignKey("users.uuid"))
    author_name: Mapped[str] = mapped_column(String(200), default="")
    overall: Mapped[int] = mapped_column(Integer, default=0)  # 1-5, 0 = unset
    # strong_yes | yes | neutral | no | strong_no
    recommendation: Mapped[str] = mapped_column(String(20), default="")
    scores_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {criterion: 1-5}
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    candidate: Mapped["Candidate"] = relationship(back_populates="scorecards")
