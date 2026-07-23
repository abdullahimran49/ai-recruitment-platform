"""Compact CV brief for the AI interviewer.

The voice agent asks questions grounded in the candidate's ACTUAL resume, not
just the job description — "you shipped an ETL pipeline at X, walk me through
it" instead of a generic pipeline question. This builds the brief the agent
reads out of what the screening stored on the candidate.

Two sources, in order of preference:
  1. screening_json["structured"] — the parsed resume (skills, experience,
     education). Stored since 2026-07-17.
  2. screening_json["criteria"][*]["evidence"] — the LLM's per-criterion quotes
     from the resume. Every candidate screened before (1) existed has these,
     and they are real CV content, so an older candidate still gets a
     CV-grounded interview rather than a generic one.

Kept short on purpose: this rides in LiveKit room metadata and lands in the
agent's system prompt, where a whole resume would crowd out its instructions.
"""

_MAX_CHARS = 2000


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and truncate — for inline fields only."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _truncate(text: str, limit: int) -> str:
    """Truncate while KEEPING newlines — the assembled brief is a layout."""
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


def _from_structured(st: dict) -> list[str]:
    lines = []
    if st.get("years_experience"):
        lines.append(f"Experience: about {st['years_experience']} years total.")
    if st.get("summary"):
        lines.append(f"Profile: {_clip(st['summary'], 300)}")
    skills = [str(s).strip() for s in (st.get("skills") or []) if str(s).strip()]
    if skills:
        lines.append("Skills claimed: " + ", ".join(skills[:20]) + ".")

    roles = []
    for e in (st.get("experience") or [])[:4]:
        title = (e.get("title") or "").strip()
        company = (e.get("company") or "").strip()
        dur = (e.get("duration") or "").strip()
        summary = _clip(e.get("summary") or "", 180)
        head = " at ".join(x for x in (title, company) if x) or "Role"
        if dur:
            head += f" ({dur})"
        roles.append(f"- {head}" + (f": {summary}" if summary else ""))
    if roles:
        lines.append("Roles:\n" + "\n".join(roles))

    edu = []
    for e in (st.get("education") or [])[:2]:
        bits = [x for x in ((e.get("degree") or "").strip(),
                            (e.get("field") or "").strip(),
                            (e.get("institution") or "").strip()) if x]
        if bits:
            edu.append(" - ".join(bits))
    if edu:
        lines.append("Education: " + "; ".join(edu) + ".")

    certs = [str(c).strip() for c in (st.get("certifications") or [])
             if str(c).strip()]
    if certs:
        lines.append("Certifications: " + ", ".join(certs[:8]) + ".")
    return lines


def _from_criteria(criteria: list) -> list[str]:
    lines = []
    for c in criteria[:8]:
        ev = _clip(c.get("evidence") or "", 200)
        if not ev:
            continue
        name = (c.get("criterion_text") or "").strip()
        met = c.get("met")
        strength = ""
        if isinstance(met, (int, float)):
            strength = (" [strong]" if met >= 0.75
                        else " [partial]" if met >= 0.4 else " [weak]")
        lines.append(f"- {name}{strength}: {ev}" if name else f"- {ev}")
    return ["From their resume:\n" + "\n".join(lines)] if lines else []


def build(candidate) -> str:
    """A short, factual brief on this candidate's CV. Empty if nothing is known.

    Returning "" is meaningful: the agent then falls back to JD-only questions
    rather than inventing a background for someone.
    """
    sj = candidate.screening_json or {}
    lines: list[str] = []

    structured = sj.get("structured")
    if isinstance(structured, dict):
        lines = _from_structured(structured)
    if not lines:
        lines = _from_criteria(sj.get("criteria") or [])

    gaps = [str(g).strip() for g in (sj.get("must_have_gaps") or [])
            if str(g).strip()]
    if gaps:
        lines.append("Gaps the screening flagged (worth probing): "
                     + "; ".join(gaps[:5]) + ".")

    return _truncate("\n".join(lines), _MAX_CHARS) if lines else ""
