"""How predictable is a test, given the pool depth and the paper size?

Drawing k questions per candidate from a pool of N makes each link different,
but "different" is not automatically "unpredictable" — and the difference is
invisible to whoever sets the numbers. A pool of 12 drawing 10 feels
randomised and shares 8 of 10 questions between any two candidates.

The maths:
  P(a given question is on one paper)      = k/N
  expected shared questions between two    = k * k/N
  fraction of the pool exposed by c papers = 1 - (1 - k/N)^c

That last one is the one that bites: candidates talk, and a shallow pool is
fully public after a handful of them.

Mirrored in the dashboard's predictability() helper — keep them in step.
"""

_TARGET_POOL_MULTIPLE = 4          # pool >= 4x the paper is a good default
_LEAK_SAMPLE = 5                   # "after 5 candidates compare notes"


def assess(pool_size: int, per_candidate: int,
           candidates: int = _LEAK_SAMPLE) -> dict:
    """Overlap + exposure for a pool/paper combination."""
    n = max(1, int(pool_size))
    k = max(0, min(int(per_candidate), n))
    if k == 0:
        return {"level": "none", "overlap": 0.0, "overlap_pct": 0,
                "exposed_pct": 0, "candidates": candidates,
                "suggested_pool": n, "identical": False}

    overlap = k * k / n
    overlap_pct = round(100 * overlap / k)
    exposed_pct = round(100 * (1 - (1 - k / n) ** max(1, candidates)))

    if k >= n:
        level = "none"           # everyone sits the whole pool
    elif overlap / k > 0.5:
        level = "weak"
    elif overlap / k > 0.25:
        level = "ok"
    else:
        level = "good"

    return {
        "level": level,
        "identical": k >= n,
        "overlap": round(overlap, 1),
        "overlap_pct": overlap_pct,
        "exposed_pct": exposed_pct,
        "candidates": candidates,
        "suggested_pool": max(n, k * _TARGET_POOL_MULTIPLE),
    }


def summary(pool_size: int, per_candidate: int,
            candidates: int = _LEAK_SAMPLE) -> str:
    """One plain-English line for the person choosing the numbers."""
    a = assess(pool_size, per_candidate, candidates)
    if a["identical"]:
        return (f"Every candidate sits the whole pool of {pool_size} - all "
                f"links are identical. Lower questions-per-candidate to "
                f"randomise.")
    line = (f"Any two candidates share about {a['overlap']} of "
            f"{per_candidate} questions ({a['overlap_pct']}%). After "
            f"{a['candidates']} candidates compare notes, roughly "
            f"{a['exposed_pct']}% of the pool is exposed.")
    if a["level"] == "weak" or a["exposed_pct"] > 80:
        line += f" Consider growing the pool to {a['suggested_pool']}+."
    return line
