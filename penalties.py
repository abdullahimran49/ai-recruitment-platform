"""Deterministic penalty evaluation against user-defined rules.

Each PenaltyRule targets a category (education, experience, skills,
certifications) and specifies a condition. The engine checks the
StructuredResume and deducts points when the condition is not met.
Total penalty is capped at a user-configurable maximum.
"""

from schemas import PenaltyResult, PenaltyRule, StructuredResume

# Degree-level hierarchy for education checks.
# Higher index = higher degree.  A candidate with "master" satisfies
# a "bachelor" requirement.
_DEGREE_LEVELS = {
    "high school": 0, "diploma": 0, "ged": 0,
    "associate": 1, "associates": 1,
    "bachelor": 2, "bachelors": 2, "bsc": 2, "ba": 2, "bs": 2, "btech": 2, "be": 2,
    "master": 3, "masters": 3, "msc": 3, "ma": 3, "ms": 3, "mba": 3, "mtech": 3, "me": 3,
    "phd": 4, "doctorate": 4, "doctoral": 4, "dphil": 4,
}


def _degree_level(text: str) -> int:
    """Return the degree level (0-4) from a free-text degree string, or -1."""
    t = text.lower().replace(".", "").replace("'", "").replace("'", "")
    for key, level in sorted(_DEGREE_LEVELS.items(), key=lambda kv: -len(kv[0])):
        if key in t:
            return level
    return -1


def _has_degree_at_least(resume: StructuredResume, required: str) -> tuple[bool, str]:
    """Check whether the candidate has at least the required degree level."""
    req_level = _degree_level(required)
    if req_level < 0:
        return True, f"Unknown degree level '{required}'; skipping."

    best_level = -1
    best_degree = "None listed"
    for edu in resume.education:
        lvl = _degree_level(edu.degree)
        if lvl > best_level:
            best_level = lvl
            best_degree = edu.degree

    if best_level >= req_level:
        return True, f"Has '{best_degree}' (meets {required} requirement)."
    if best_level < 0:
        return False, "No recognizable degree found in education section."
    return False, f"Highest degree '{best_degree}' is below {required} level."


def _experience_above(resume: StructuredResume, threshold_str: str) -> tuple[bool, str]:
    """Check whether years_experience >= threshold."""
    try:
        threshold = float(threshold_str)
    except (ValueError, TypeError):
        return True, f"Invalid experience threshold '{threshold_str}'; skipping."

    yrs = resume.years_experience
    if yrs < 0:
        return False, "Could not compute experience years from resume dates."
    if yrs >= threshold:
        return True, f"Has {yrs} years experience (≥ {threshold})."
    return False, f"Only {yrs} years experience (requires ≥ {threshold})."


def _has_skill(resume: StructuredResume, skill: str) -> tuple[bool, str]:
    """Check whether a skill keyword appears in the candidate's skills list."""
    target = skill.lower().strip()
    for s in resume.skills:
        if target in s.lower():
            return True, f"Skill '{s}' matches '{skill}'."
    return False, f"Skill '{skill}' not found in candidate's skills list."


def _has_certification(resume: StructuredResume, cert: str) -> tuple[bool, str]:
    """Check whether a certification keyword appears in certifications."""
    target = cert.lower().strip()
    for c in resume.certifications:
        if target in c.lower():
            return True, f"Certification '{c}' matches '{cert}'."
    return False, f"Certification '{cert}' not found in candidate's certifications."


_CHECKERS = {
    "education": _has_degree_at_least,
    "experience": _experience_above,
    "skills": _has_skill,
    "certifications": _has_certification,
}


def evaluate_penalties(
    resume: StructuredResume,
    rules: list[PenaltyRule],
    max_penalty: float = 30.0,
) -> tuple[float, list[PenaltyResult]]:
    """Evaluate all enabled penalty rules against a structured resume.

    Returns (total_penalty_capped, list_of_results).
    """
    results: list[PenaltyResult] = []
    total = 0.0

    for rule in rules:
        if not rule.enabled:
            results.append(PenaltyResult(
                rule_id=rule.id, category=rule.category,
                condition=rule.condition, applied=False,
                points_deducted=0.0, reason="Rule disabled.",
            ))
            continue

        checker = _CHECKERS.get(rule.category)
        if not checker:
            results.append(PenaltyResult(
                rule_id=rule.id, category=rule.category,
                condition=rule.condition, applied=False,
                points_deducted=0.0,
                reason=f"Unknown category '{rule.category}'.",
            ))
            continue

        passed, reason = checker(resume, rule.field_value)
        if passed:
            results.append(PenaltyResult(
                rule_id=rule.id, category=rule.category,
                condition=rule.condition, applied=False,
                points_deducted=0.0, reason=reason,
            ))
        else:
            deducted = rule.points
            total += deducted
            results.append(PenaltyResult(
                rule_id=rule.id, category=rule.category,
                condition=rule.condition, applied=True,
                points_deducted=deducted, reason=reason,
            ))

    capped = min(total, max_penalty)
    # If we capped, scale individual deductions proportionally so they sum
    # to the capped total (keeps the breakdown meaningful).
    if total > max_penalty and total > 0:
        scale = max_penalty / total
        for r in results:
            if r.applied:
                r.points_deducted = round(r.points_deducted * scale, 1)

    return round(capped, 1), results
