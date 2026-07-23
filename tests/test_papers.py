"""Per-candidate question papers, and the scoring that depends on them.

THIS IS THE FILE THAT MATTERS MOST. A test is a POOL; each candidate draws
their own paper from it. Every scoring path must therefore read the
candidate's drawn paper (core.question_sets.questions_for), never
test.questions.

Getting this wrong does not raise — it silently marks candidates wrong for
questions they were never shown. A candidate acing their own 5-of-10 paper
scores 50 instead of 100 and nobody notices until someone disputes a
rejection. If a change makes these fail, do not "fix" the test.
"""

import random

import pytest
from sqlalchemy import select

import db_bridge
from conftest import answer_paper, make_pool, served_paper
from core.db import session
from core.models import AssignmentQuestion, TestAssignment
from core.question_sets import questions_for, select_questions


# ---- the draw, in isolation (no DB, no server) -------------------------------

class _Q:
    def __init__(self, qid, category=""):
        self.id = qid
        self.category = category


def test_draw_takes_the_requested_number():
    pool = [_Q(i) for i in range(20)]
    got = select_questions(pool, 5, None, random.Random(1))
    assert len(got) == 5
    assert len({q.id for q in got}) == 5, "a question was drawn twice"


def test_draw_of_zero_means_the_whole_pool():
    """0 = 'everyone sits everything' — the pre-bank behaviour old rows rely on."""
    pool = [_Q(i) for i in range(7)]
    assert len(select_questions(pool, 0, None, random.Random(1))) == 7


def test_draw_larger_than_pool_yields_the_pool():
    pool = [_Q(i) for i in range(3)]
    assert len(select_questions(pool, 10, None, random.Random(1))) == 3


def test_different_seeds_give_different_papers():
    pool = [_Q(i) for i in range(30)]
    a = {q.id for q in select_questions(pool, 10, None, random.Random("a"))}
    b = {q.id for q in select_questions(pool, 10, None, random.Random("b"))}
    assert a != b, "every candidate would sit the same paper"


def test_blueprint_quota_is_honoured():
    pool = ([_Q(i, "Data") for i in range(5)]
            + [_Q(10 + i, "AI") for i in range(5)]
            + [_Q(20 + i, "General") for i in range(5)])
    for seed in range(15):
        got = select_questions(pool, 3, {"Data": 1, "AI": 1, "General": 1},
                               random.Random(seed))
        cats = sorted(q.category for q in got)
        assert cats == ["AI", "Data", "General"], f"seed {seed} gave {cats}"


def test_blueprint_shortfall_degrades_instead_of_raising():
    """A category thinner than its quota must not strand a dispatch."""
    pool = [_Q(0, "Data")] + [_Q(10 + i, "AI") for i in range(5)]
    got = select_questions(pool, 4, {"Data": 3, "AI": 1}, random.Random(1))
    assert len(got) == 4
    assert sum(1 for q in got if q.category == "Data") == 1


def test_blueprint_remainder_is_filled_at_random():
    pool = ([_Q(i, "Data") for i in range(5)]
            + [_Q(10 + i, "AI") for i in range(5)])
    got = select_questions(pool, 5, {"Data": 1}, random.Random(3))
    assert len(got) == 5
    assert sum(1 for q in got if q.category == "Data") >= 1


def test_empty_pool_is_empty_not_an_error():
    assert select_questions([], 5, None, random.Random(1)) == []


# ---- the draw, end to end ----------------------------------------------------

def test_each_candidate_gets_their_own_paper(make_job, make_candidate):
    job = make_job()
    cands = [make_candidate(job, key=f"p{i}") for i in range(3)]
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(12), cands, duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=5)

    assert len(entries) == 3
    papers = {}
    with session() as s:
        for e in entries:
            papers[e["token"]] = set(s.execute(
                select(AssignmentQuestion.question_id).where(
                    AssignmentQuestion.assignment_uuid == e["token"])
            ).scalars().all())

    assert all(len(p) == 5 for p in papers.values())
    assert len({frozenset(p) for p in papers.values()}) > 1, (
        "all candidates drew an identical paper")


def test_scoring_is_against_the_drawn_paper_not_the_pool(
        client, make_job, make_candidate):
    """The regression that matters: 100 means 'aced their own paper'.

    If a scoring path reverts to test.questions, this reads 50 — the paper is
    5 of a 10-question pool.
    """
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(10), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=5)
    token = entries[0]["token"]

    r = answer_paper(client, token, correct_ratio=1.0)
    assert r.status_code == 200, r.text
    assert r.json()["total_questions"] == 5

    with session() as s:
        a = s.get(TestAssignment, token)
        assert a.test_score == 100.0, (
            f"scored {a.test_score} — 50.0 means it scored against the "
            f"10-question pool instead of the candidate's 5")
        assert len(a.answers) == 5


def test_partial_score_uses_the_paper_as_denominator(
        client, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(10), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=4)
    answer_paper(client, entries[0]["token"], correct_ratio=0.5)
    with session() as s:
        assert s.get(TestAssignment, entries[0]["token"]).test_score == 50.0


def test_candidate_is_only_served_their_own_paper(
        client, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(12), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=4)
    token = entries[0]["token"]

    served = served_paper(client, token)
    assert len(served) == 4, "the pool leaked into what the candidate sees"

    info = client.get(f"/api/portal/assignment/{token}/info").json()
    assert info["num_questions"] == 4


def test_reload_does_not_reshuffle_the_paper(client, make_job, make_candidate):
    """A candidate refreshing mid-test must see the same questions in order."""
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(12), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=5)
    token = entries[0]["token"]
    first = [q["id"] for q in served_paper(client, token)]
    second = [q["id"] for q in served_paper(client, token)]
    assert first == second


def test_editing_the_pool_cannot_change_a_paper_in_progress(
        client, make_job, make_candidate):
    """The draw is persisted, not recomputed — deleting a pool question the
    candidate was not given must not disturb them."""
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(12), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=5)
    token = entries[0]["token"]
    before = [q["id"] for q in served_paper(client, token)]

    with session() as s:
        a = s.get(TestAssignment, token)
        drawn = {ln.question_id for ln in a.question_links}
        spare = next(q for q in a.test.questions if q.id not in drawn)
        s.delete(spare)

    assert [q["id"] for q in served_paper(client, token)] == before


def test_legacy_assignment_with_no_draw_falls_back_to_the_pool(
        make_job, make_candidate):
    """Rows created before per-candidate papers existed must still work."""
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(6), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=3)
    token = entries[0]["token"]

    with session() as s:
        s.execute(AssignmentQuestion.__table__.delete().where(
            AssignmentQuestion.assignment_uuid == token))
    with session() as s:
        a = s.get(TestAssignment, token)
        assert len(questions_for(a)) == 6, (
            "an assignment with no recorded draw must fall back to the "
            "whole pool, not to nothing")


def test_admin_result_view_shows_the_sat_paper(
        client, admin_headers, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(10), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=4)
    token = entries[0]["token"]
    answer_paper(client, token, correct_ratio=1.0)

    d = client.get(f"/api/admin/assignments/{token}",
                   headers=admin_headers).json()
    assert d["total"] == 4 and d["correct"] == 4
    assert len(d["questions"]) == 4


def test_results_email_only_contains_the_sat_paper(
        client, admin_headers, make_job, make_candidate, outbox):
    """Regression: send_results built its breakdown from the pool, so it
    emailed candidates questions they never saw (and leaked the rest of the
    pool to a future retaker)."""
    job = make_job()
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(10), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=4)
    token = entries[0]["token"]
    answer_paper(client, token, correct_ratio=1.0)

    with session() as s:
        drawn = {q.question for q in questions_for(s.get(TestAssignment, token))}

    outbox.clear()
    r = client.post(f"/api/admin/assignments/{token}/send-results",
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    body = outbox[-1]["body"]
    assert "Correct: 4 / 4" in body, body[:200]
    for q in drawn:
        assert q in body
    # No question they never sat may appear.
    with session() as s:
        a = s.get(TestAssignment, token)
        unseen = [q.question for q in a.test.questions if q.question not in drawn]
    for q in unseen:
        assert q not in body, f"emailed a question the candidate never sat: {q!r}"
