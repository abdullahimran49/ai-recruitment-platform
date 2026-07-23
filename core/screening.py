"""Single-resume screening for the job portal's apply flow.

The Streamlit recruiter app screens resumes in a batch via
``scoring.process_resume``. When a candidate applies through the public portal
we need the exact same scoring for ONE resume, driven by the job's stored
``criteria_json`` (the config the recruiter saved for that role). Keeping this
in one place means the portal and the recruiter app can never drift into two
different notions of "the score".

``criteria_json`` shape (written by db_bridge.save_screening / app.py)::

    {
      "must":  [{"criterion": str, "weight": 1-10}, ...],
      "nice":  [{"criterion": str, "weight": 1-10}, ...],
      "penalties": [ {id, category, condition, field_value, points, enabled}, ...],
      "settings": {pass_threshold, penalize_gaps, gap_min_months,
                   gap_points, gap_max, penalty_max}
    }

Old/empty configs simply yield empty criteria and a raw score of 0 — the same
thing the recruiter app would produce for a job with no criteria.
"""

from __future__ import annotations

from schemas import Criteria, Criterion, PenaltyRule, ResumeResult
from scoring import process_resume


def criteria_from_config(cfg: dict) -> Criteria:
    """Build a Criteria object from a job's stored criteria_json."""
    cfg = cfg or {}
    must, nice, cid = [], [], 1
    for r in cfg.get("must", []) or []:
        text = str(r.get("criterion", "")).strip()
        if not text:
            continue
        must.append(Criterion(id=cid, text=text,
                              weight=_clamp_weight(r.get("weight", 5))))
        cid += 1
    for r in cfg.get("nice", []) or []:
        text = str(r.get("criterion", "")).strip()
        if not text:
            continue
        nice.append(Criterion(id=cid, text=text,
                             weight=_clamp_weight(r.get("weight", 5))))
        cid += 1
    return Criteria(must_have=must, nice_to_have=nice)


def penalty_rules_from_config(cfg: dict) -> list[PenaltyRule]:
    """Reconstruct penalty rules from a job's stored criteria_json."""
    rules: list[PenaltyRule] = []
    for i, p in enumerate((cfg or {}).get("penalties", []) or [], start=1):
        try:
            rules.append(PenaltyRule(
                id=int(p.get("id", i)),
                category=str(p.get("category", "")),
                condition=str(p.get("condition", "")),
                field_value=str(p.get("field_value", p.get("value", ""))),
                points=float(p.get("points", 5.0)),
                enabled=bool(p.get("enabled", True)),
            ))
        except Exception:  # noqa: BLE001 - a malformed rule must not block apply
            continue
    return rules


def screen_resume_for_job(job, resume_text: str, filename: str) -> ResumeResult:
    """Screen one resume against a Job, using the job's saved config.

    ``job`` is a core.models.Job (or anything with ``jd_text`` and
    ``criteria_json``). Never raises — errors land on ``result.error`` exactly
    like the batch path, so an application is still recorded even if the LLM is
    unavailable (the candidate just scores 0 and HR can re-screen).
    """
    cfg = job.criteria_json or {}
    settings = cfg.get("settings", {}) or {}
    criteria = criteria_from_config(cfg)
    penalty_rules = penalty_rules_from_config(cfg)
    return process_resume(
        filename=filename,
        resume_text=resume_text,
        job_description=job.jd_text or "",
        criteria=criteria,
        penalize_gaps=bool(settings.get("penalize_gaps", False)),
        gap_min_months=int(settings.get("gap_min_months", 6)),
        gap_points=float(settings.get("gap_points", 5.0)),
        gap_max_penalty=float(settings.get("gap_max", 15.0)),
        penalty_rules=penalty_rules or None,
        penalty_max=float(settings.get("penalty_max", 30.0)),
    )


def _clamp_weight(w) -> int:
    try:
        return max(1, min(10, int(w)))
    except (TypeError, ValueError):
        return 5
