"""Per-candidate question papers drawn from a test's question pool.

A Test owns a pool; each TestAssignment draws its own subset, so two
candidates on the same test never sit the same paper and a leaked paper does
not predict the next one. The draw happens once, at assignment creation, and
is persisted in assignment_questions — it is never recomputed on read, so
editing the pool afterwards cannot change a paper someone is already sitting.

The draw honours an optional blueprint ({category: how_many}, e.g. 1 Data +
1 AI + 1 General). Anything the blueprint does not account for is filled at
random from the rest of the pool.
"""

import random

from core.models import AssignmentQuestion


def _bucket(questions):
    out: dict[str, list] = {}
    for q in questions:
        out.setdefault((q.category or "").strip().lower(), []).append(q)
    return out


def select_questions(pool, per_candidate: int, blueprint: dict | None,
                     rng: random.Random) -> list:
    """Choose one candidate's paper from `pool`.

    `per_candidate` of 0 (or more than the pool holds) means the whole pool.
    A category that cannot fill its blueprint quota contributes everything it
    has and the shortfall falls through to the random fill, so a thin bank
    yields a smaller paper rather than an error — the alternative (refusing to
    dispatch) would strand recruiters mid-send.
    """
    pool = list(pool)
    if not pool:
        return []
    target = per_candidate if 0 < per_candidate <= len(pool) else len(pool)

    picked: list = []
    taken: set[int] = set()
    by_cat = _bucket(pool)

    for cat, want in (blueprint or {}).items():
        try:
            want = int(want)
        except (TypeError, ValueError):
            continue
        if want <= 0:
            continue
        avail = [q for q in by_cat.get(str(cat).strip().lower(), [])
                 if id(q) not in taken]
        for q in rng.sample(avail, min(want, len(avail))):
            taken.add(id(q))
            picked.append(q)

    if len(picked) < target:
        rest = [q for q in pool if id(q) not in taken]
        picked.extend(rng.sample(rest, min(target - len(picked), len(rest))))

    del picked[target:]
    rng.shuffle(picked)
    return picked


def assign_questions(s, assignment, pool=None, seed: str | None = None) -> list:
    """Draw this assignment's paper and persist it. Returns the questions.

    `pool` defaults to the test's questions; pass it explicitly when the
    questions were added in the same flush and the relationship is not loaded
    yet. `seed` defaults to the assignment uuid, which is itself uuid4 — so
    papers differ per candidate but a redraw of the same assignment is stable.
    """
    test = assignment.test
    pool = list(test.questions) if pool is None else list(pool)
    rng = random.Random(seed or assignment.uuid)
    picked = select_questions(
        pool, test.questions_per_candidate or 0, test.blueprint_json, rng)
    for i, q in enumerate(picked):
        s.add(AssignmentQuestion(assignment_uuid=assignment.uuid,
                                 question_id=q.id, sort_order=i))
    return picked


def questions_for(assignment) -> list:
    """The questions on this assignment's paper, in their drawn order.

    Falls back to the test's whole pool when no draw was recorded, which keeps
    assignments created before per-candidate papers existed readable and
    scoreable. Every scoring path must go through here: scoring against
    test.questions would mark a candidate wrong for questions they were never
    shown.
    """
    links = assignment.question_links
    if not links:
        return list(assignment.test.questions)
    return [ln.question for ln in sorted(links, key=lambda ln: ln.sort_order)]
