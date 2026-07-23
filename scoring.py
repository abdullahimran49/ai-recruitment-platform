"""Two-call scoring pipeline: structure the resume, then score all criteria.

Splitting into exactly two LLM calls per resume (instead of one call per
criterion) keeps token usage low enough for Groq's free tier while still
producing per-criterion evidence for the explanation view. The final 0-100
score is computed deterministically in Python, not by the model.
"""

import json

import config
import llm
from schemas import (
    Criteria,
    CriterionScore,
    PenaltyRule,
    ResumeResult,
    StructuredResume,
)

# --- System prompts. Keep these BYTE-FOR-BYTE STABLE: Groq caches identical
# system prompts and those tokens then stop counting against the rate limit. ---

_STRUCTURE_SYSTEM = """You are a precise resume parser. Read the resume text \
and extract the facts into JSON. Do not invent anything; leave a field empty \
if the resume does not state it. For each experience entry, extract the \
start_date and end_date exactly as written on the resume (e.g. "Jun 2026", \
"2025", "Present"). Set years_experience to 0 — it will be computed later. \
Respond with ONLY a JSON object of this shape:
{
  "name": str, "email": str, "phone": str,
  "years_experience": 0, "summary": str,
  "skills": [str], "certifications": [str],
  "experience": [{"title": str, "company": str, "start_date": str, "end_date": str, "duration": str, "summary": str}],
  "education": [{"degree": str, "field": str, "institution": str, "year": str}]
}"""

_SCORE_SYSTEM = """You are a fair, evidence-based technical recruiter. You are \
given a candidate profile, a JOB DESCRIPTION, and an optional list of extra \
hiring criteria. Judge the candidate against the job description first, then \
against each criterion, always citing evidence from the profile.

Rules:
- "overall_fit" is 0.0 (wrong fit) to 1.0 (excellent fit) for the job description.
- "met" (per criterion) is 0.0 (not met) to 1.0 (fully met).
- Base every score ONLY on evidence in the profile. No evidence -> score 0.0.
- "evidence" must be 2-4 sentences. Quote or closely paraphrase the specific \
parts of the profile that relate to this criterion. Include job titles, years, \
tools, projects, or certifications when relevant. If no evidence exists, \
write "No relevant experience, skills, or certifications found in the profile."
- "reasoning" must be 2-3 sentences. First explain why you assigned this \
specific score. Then note any strengths that support the candidate or gaps \
that weaken them. Finally, add context such as how the depth/recency of \
experience or related transferable skills affected the score.
- Return a result for every criterion id given (the list may be empty).

Respond with ONLY a JSON object of this shape:
{"overall_fit": number, "overall_evidence": str, "overall_reasoning": str,
 "scores": [{"criterion_id": int, "met": number, "evidence": str, "reasoning": str}]}"""


def _parse_date(s: str):
    """Parse a date string like 'Jun 2026', 'February 2025', '2025', 'Present'.

    Returns a (year, month) tuple or None if unparseable.
    """
    from datetime import date, datetime

    s = s.strip()
    if not s:
        return None
    if s.lower() in ("present", "current", "now", "ongoing", "till date",
                      "till now", "to date"):
        today = date.today()
        return (today.year, today.month)

    # Try "Mon YYYY" / "Month YYYY" formats
    for fmt in ("%b %Y", "%B %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return (dt.year, dt.month)
        except ValueError:
            pass

    # Try "YYYY" alone — treat as Jan of that year
    import re
    m = re.match(r"^(\d{4})$", s.strip())
    if m:
        return (int(m.group(1)), 1)

    return None


def _compute_experience_years(experience) -> float:
    """Deterministically compute total experience in years from date fields.

    Overlapping roles are not deduplicated — this matches recruiter convention
    of summing individual role durations.
    """
    total_months = 0
    any_parsed = False
    for item in experience:
        start = _parse_date(item.start_date)
        end = _parse_date(item.end_date)
        if start and end:
            months = (end[0] - start[0]) * 12 + (end[1] - start[1])
            # Add 1 to be inclusive of the start month (Feb–May = 4 months)
            months = max(0, months)
            total_months += months
            any_parsed = True
    if not any_parsed:
        return -1.0  # signal: could not compute
    return round(total_months / 12.0, 1)


def structure_resume(resume_text: str, cover_letter_text: str = "") -> StructuredResume:
    """LLM call #1: turn raw resume text into a StructuredResume."""
    from datetime import date

    today = date.today().strftime("%B %d, %Y")  # e.g. "July 03, 2026"
    user = f"TODAY'S DATE: {today}\n\nRESUME TEXT:\n{resume_text}"
    if cover_letter_text:
        user += f"\n\nCOVER LETTER (extra context):\n{cover_letter_text}"
    data = llm.chat_json(_STRUCTURE_SYSTEM, user)
    try:
        structured = StructuredResume.model_validate(data)
    except Exception:  # noqa: BLE001 - tolerate loose model output
        return StructuredResume()

    # Override LLM's years_experience with a deterministic Python calculation.
    computed = _compute_experience_years(structured.experience)
    if computed >= 0:
        structured.years_experience = computed
    return structured


def score_resume(
    structured: StructuredResume,
    job_description: str,
    criteria: Criteria,
    cover_letter_text: str = "",
) -> list[CriterionScore]:
    """LLM call #2: score overall job fit + all criteria in one request.

    The returned list always starts with an "overall" CriterionScore (id 0)
    representing fit against the job description; explicit criteria follow.
    """
    crit_lines = []
    kind_by_id = {}
    for c in criteria.must_have:
        crit_lines.append(f'{{"criterion_id": {c.id}, "kind": "must_have", "text": "{_esc(c.text)}"}}')
        kind_by_id[c.id] = ("must_have", c.text)
    for c in criteria.nice_to_have:
        crit_lines.append(f'{{"criterion_id": {c.id}, "kind": "nice_to_have", "text": "{_esc(c.text)}"}}')
        kind_by_id[c.id] = ("nice_to_have", c.text)

    profile = structured.model_dump()
    user = (
        "JOB DESCRIPTION:\n"
        + (job_description.strip() or "(none provided)")
        + "\n\nCANDIDATE PROFILE (JSON):\n"
        + json.dumps(profile, ensure_ascii=False)
    )
    if cover_letter_text:
        user += f"\n\nCOVER LETTER (extra context):\n{cover_letter_text}"
    user += "\n\nEXTRA CRITERIA:\n[" + ",\n".join(crit_lines) + "]"

    data = llm.chat_json(_SCORE_SYSTEM, user)
    if not isinstance(data, dict):
        data = {}
    raw_scores = data.get("scores", [])

    # Overall JD fit always comes first (id 0), when a JD was provided.
    results: list[CriterionScore] = []
    if job_description.strip():
        results.append(CriterionScore(
            criterion_id=0,
            criterion_text="Overall fit for the role (job description)",
            kind="overall",
            met=_clamp01(data.get("overall_fit", 0)),
            evidence=str(data.get("overall_evidence", "")),
            reasoning=str(data.get("overall_reasoning", "")),
        ))

    seen = set()
    for row in raw_scores:
        try:
            cid = int(row.get("criterion_id"))
        except (TypeError, ValueError):
            continue
        if cid not in kind_by_id or cid in seen:
            continue
        seen.add(cid)
        kind, text = kind_by_id[cid]
        results.append(CriterionScore(
            criterion_id=cid,
            criterion_text=text,
            kind=kind,
            met=_clamp01(row.get("met", 0)),
            evidence=str(row.get("evidence", "")),
            reasoning=str(row.get("reasoning", "")),
        ))

    # Any criterion the model skipped counts as unmet (met=0).
    for cid, (kind, text) in kind_by_id.items():
        if cid not in seen:
            results.append(CriterionScore(
                criterion_id=cid, criterion_text=text, kind=kind,
                met=0.0, evidence="none",
                reasoning="No response from model for this criterion.",
            ))
    return results


def aggregate(
    scores: list[CriterionScore], criteria: Criteria
) -> tuple[float, list[str]]:
    """Combine per-criterion scores into a 0-100 total plus must-have gaps.

    Must-haves and nice-to-haves are both weighted, but a must-have scoring
    below the threshold is recorded as a gap and caps the total score so an
    otherwise strong candidate can't hide a missing requirement.
    """
    weight_by_id = {c.id: c.weight for c in criteria.all()}

    earned = 0.0
    total_weight = 0.0
    gaps: list[str] = []
    for s in scores:
        # The overall JD-fit item (id 0) carries the configured fit weight;
        # explicit criteria carry their own.
        w = config.OVERALL_FIT_WEIGHT if s.kind == "overall" else weight_by_id.get(s.criterion_id, 0)
        earned += s.met * w
        total_weight += w
        if s.kind == "must_have" and s.met < config.MUST_HAVE_THRESHOLD:
            gaps.append(s.criterion_text)

    if total_weight == 0:
        return 0.0, []

    score = round(100 * earned / total_weight, 1)
    if gaps:
        # Cap at 60 when any must-have is missing — a hard signal for recruiters.
        score = min(score, 60.0)
    return score, gaps


def process_resume(
    filename: str,
    resume_text: str,
    job_description: str,
    criteria: Criteria,
    cover_letter_text: str = "",
    penalize_gaps: bool = False,
    gap_min_months: int = 6,
    gap_points: float = 5.0,
    gap_max_penalty: float = 15.0,
    penalty_rules: list[PenaltyRule] | None = None,
    penalty_max: float = 30.0,
) -> ResumeResult:
    """Full pipeline for one resume. Never raises — errors land on the result.

    When `penalize_gaps` is on, employment gaps >= `gap_min_months` (detected
    deterministically from parsed dates in gaps.py) subtract `gap_points` each
    from the final score, capped at `gap_max_penalty`.

    When `penalty_rules` is provided, each enabled rule is evaluated against
    the parsed resume (education, experience, skills, certifications) and
    matching penalties are subtracted, capped at `penalty_max`.
    """
    import gaps as gaps_mod
    import penalties as penalties_mod

    try:
        structured = structure_resume(resume_text, cover_letter_text)
        scores = score_resume(structured, job_description, criteria, cover_letter_text)
        raw_total, mh_gaps = aggregate(scores, criteria)

        emp_gaps = gaps_mod.detect_gaps(structured.experience, gap_min_months)
        gap_pen = 0.0
        if penalize_gaps and emp_gaps:
            gap_pen = gaps_mod.gap_penalty(emp_gaps, gap_points, gap_max_penalty)

        criteria_pen = 0.0
        pen_results = []
        if penalty_rules:
            criteria_pen, pen_results = penalties_mod.evaluate_penalties(
                structured, penalty_rules, penalty_max)

        total = max(0.0, round(raw_total - gap_pen - criteria_pen, 1))

        return ResumeResult(
            filename=filename,
            candidate_name=structured.name or filename,
            score=total,
            raw_score=raw_total,
            structured=structured,
            criterion_scores=scores,
            must_have_gaps=mh_gaps,
            employment_gaps=emp_gaps,
            gap_penalty=gap_pen,
            penalty_results=pen_results,
            criteria_penalty=criteria_pen,
            had_cover_letter=bool(cover_letter_text),
        )
    except llm.LLMError as e:
        return ResumeResult(filename=filename, error=str(e))
    except Exception as e:  # noqa: BLE001 - keep the batch alive
        return ResumeResult(filename=filename, error=f"Unexpected error: {e}")


def _clamp01(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
