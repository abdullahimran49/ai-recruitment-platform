"""Deterministic employment-gap detection from parsed resume dates.

LLMs are unreliable at date arithmetic, so gap analysis happens here in
Python: parse each role's start/end date strings, merge overlapping spans,
and report any hole between consecutive spans longer than a configurable
number of months.
"""

import re
from datetime import date

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_PRESENT = {"present", "current", "now", "ongoing", "today"}


def parse_date(text: str) -> date | None:
    """Parse a resume date string to a date (day pinned to 1). None if unknown.

    Handles: "Jun 2026", "June 2026", "06/2026", "2026-06", "2026",
    "Present"/"Current" (-> today).
    """
    if not text:
        return None
    t = text.strip().lower().rstrip(".,")
    if t in _PRESENT:
        today = date.today()
        return date(today.year, today.month, 1)

    # "Jun 2026" / "June 2026" / "Sept 2026"
    m = re.match(r"([a-z]+)\.?\s+(\d{4})$", t)
    if m:
        month = _MONTHS.get(m.group(1)[:4]) or _MONTHS.get(m.group(1)[:3])
        if month:
            return date(int(m.group(2)), month, 1)

    # "06/2026" or "6-2026"
    m = re.match(r"(\d{1,2})[/\-](\d{4})$", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return date(int(m.group(2)), int(m.group(1)), 1)

    # "2026-06" or "2026/6"
    m = re.match(r"(\d{4})[/\-](\d{1,2})$", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return date(int(m.group(1)), int(m.group(2)), 1)

    # Bare year "2026" -> assume January (conservative for gap detection)
    m = re.match(r"(\d{4})$", t)
    if m:
        return date(int(m.group(1)), 1, 1)

    return None


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def detect_gaps(experience, min_gap_months: int = 6):
    """Find employment gaps between roles.

    `experience` is a list of ExperienceItem (needs .start_date/.end_date
    strings). Returns a list of human-readable gap descriptions. Roles whose
    dates can't be parsed are skipped rather than guessed.
    """
    spans = []
    for e in experience:
        start = parse_date(getattr(e, "start_date", "") or "")
        end = parse_date(getattr(e, "end_date", "") or "")
        if start and end and end >= start:
            spans.append((start, end))
    if len(spans) < 1:
        return []

    spans.sort()
    # Merge overlapping/adjacent spans so parallel roles don't fake a gap.
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    gaps = []
    for (_, prev_end), (next_start, _) in zip(merged, merged[1:]):
        months = _months_between(prev_end, next_start)
        if months >= min_gap_months:
            gaps.append(
                f"{months} months between {prev_end.strftime('%b %Y')} "
                f"and {next_start.strftime('%b %Y')}"
            )
    return gaps


def gap_penalty(gaps: list, points_per_gap: float, max_penalty: float) -> float:
    """Deterministic penalty: points per detected gap, capped."""
    return min(len(gaps) * points_per_gap, max_penalty)
