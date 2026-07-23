"""Pydantic models shared across the pipeline."""

from typing import List, Optional

from pydantic import BaseModel, Field


# ---- Criteria (user-defined, per job) --------------------------------------

class Criterion(BaseModel):
    id: int
    text: str
    weight: int = Field(ge=1, le=10, default=5)


class Criteria(BaseModel):
    must_have: List[Criterion] = Field(default_factory=list)
    nice_to_have: List[Criterion] = Field(default_factory=list)

    def all(self) -> List[Criterion]:
        return self.must_have + self.nice_to_have


# ---- Penalty rules (user-defined, per job) ---------------------------------

class PenaltyRule(BaseModel):
    """A user-defined scoring penalty rule.

    Categories:
      - education:      penalty if candidate lacks a degree level
      - experience:     penalty if years_experience < threshold
      - skills:         penalty if a skill keyword is missing
      - certifications: penalty if a certification is missing
    """
    id: int
    category: str          # "education" | "experience" | "skills" | "certifications"
    condition: str         # human-readable, e.g. "No Bachelor's degree"
    field_value: str       # value to check: "bachelor", "3", "Python", "AWS"
    points: float = Field(default=5.0, ge=0.0, le=50.0)
    enabled: bool = True


class PenaltyResult(BaseModel):
    """Result of applying one penalty rule to a candidate."""
    rule_id: int
    category: str
    condition: str
    applied: bool = False
    points_deducted: float = 0.0
    reason: str = ""


# ---- Structured resume (LLM output, call #1) -------------------------------

class ExperienceItem(BaseModel):
    title: str = ""
    company: str = ""
    start_date: str = ""
    end_date: str = ""
    duration: str = ""
    summary: str = ""


class EducationItem(BaseModel):
    degree: str = ""
    field: str = ""
    institution: str = ""
    year: str = ""


class StructuredResume(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    years_experience: float = 0
    summary: str = ""
    skills: List[str] = Field(default_factory=list)
    experience: List[ExperienceItem] = Field(default_factory=list)
    education: List[EducationItem] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)


# ---- Scoring (LLM output, call #2) -----------------------------------------

class CriterionScore(BaseModel):
    criterion_id: int
    criterion_text: str = ""
    kind: str = ""            # "must_have" | "nice_to_have"
    met: float = Field(ge=0.0, le=1.0, default=0.0)
    evidence: str = ""        # quote/paraphrase from the resume
    reasoning: str = ""       # why this score


# ---- Final per-resume result ------------------------------------------------

class ResumeResult(BaseModel):
    filename: str
    candidate_name: str = ""
    score: float = 0                       # 0-100, after all penalties
    raw_score: float = 0                   # 0-100, before any penalty
    structured: Optional[StructuredResume] = None
    criterion_scores: List[CriterionScore] = Field(default_factory=list)
    must_have_gaps: List[str] = Field(default_factory=list)
    employment_gaps: List[str] = Field(default_factory=list)
    gap_penalty: float = 0                 # points subtracted for gaps
    penalty_results: List["PenaltyResult"] = Field(default_factory=list)
    criteria_penalty: float = 0            # total from penalty rules
    had_cover_letter: bool = False
    error: str = ""                        # non-empty if processing failed

    @property
    def email(self) -> str:
        return self.structured.email if self.structured else ""


# ---- MCQ assessment (LLM output + user-edited) -------------------------------

class MCQQuestion(BaseModel):
    question: str = ""
    options: List[str] = Field(default_factory=list)   # exactly 4
    correct_index: int = Field(ge=0, le=3, default=0)
    explanation: str = ""
    category: str = ""       # blueprint bucket, e.g. "Data" / "AI" / "General"


class MCQTest(BaseModel):
    """The question POOL for a job, plus how much of it each candidate sits.

    `questions_per_candidate` of 0 means everyone gets the whole pool; set it
    lower than len(questions) and every candidate's link draws its own paper.
    """
    difficulty: str = "medium"             # easy | medium | hard
    questions: List[MCQQuestion] = Field(default_factory=list)
    custom_questions: List[MCQQuestion] = Field(default_factory=list)
    custom_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    questions_per_candidate: int = Field(default=0, ge=0)
    blueprint: dict = Field(default_factory=dict)   # {category: how_many}
    approved: bool = False
