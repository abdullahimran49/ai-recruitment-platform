"""Persist saved jobs (job description + criteria) to a local JSON file so a
recruiter can reuse a role without re-entering it."""

import json
import os

_STORE = os.path.join(os.path.dirname(__file__), "jobs.json")


def load_jobs() -> dict:
    """Return {job_name: {jd, must, nice}}. Empty dict if nothing saved."""
    if not os.path.exists(_STORE):
        return {}
    try:
        with open(_STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_job(name: str, jd: str, must: list, nice: list,
             penalties: list | None = None,
             settings: dict | None = None) -> None:
    """Save (or overwrite) a job. `must`/`nice` are lists of {criterion, weight}.

    `penalties` is a list of penalty rule dicts (category, condition, value,
    points, enabled). `settings` holds per-job screening options (pass
    threshold, gap penalty toggle, penalty max, etc.) and is stored as-is.
    """
    jobs = load_jobs()
    jobs[name] = {"jd": jd, "must": must, "nice": nice,
                  "penalties": penalties or [],
                  "settings": settings or {}}
    with open(_STORE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)



def delete_job(name: str) -> None:
    jobs = load_jobs()
    jobs.pop(name, None)
    with open(_STORE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
