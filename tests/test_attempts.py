"""Reset keeps history and mints a different link.

Resetting a candidate must never destroy the previous attempt: its answers,
score, proctoring log and drawn paper are the record of what happened, and
hiring decisions get disputed.

The landmine guarded here: `_check_duplicate` rejects anyone who already
submitted for the job. Once a kept attempt-1 submission exists, the
replacement link 410s "you have already completed an assessment" unless
superseded attempts are excluded. The retake is then dead on arrival, and it
looks like the reset silently did nothing.
"""

from sqlalchemy import func, select

import db_bridge
from conftest import answer_paper, make_pool, served_paper
from core.db import session
from core.models import (
    AssignmentQuestion, CandidateAnswer, ProctorEvent, TestAssignment)


def _dispatch(job, cand, pool_n=12, per_cand=5):
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(pool_n), [cand], duration_minutes=30, pass_score=60,
        proctored=False, questions_per_candidate=per_cand)
    return entries[0]["token"]


def _reset(client, admin_headers, token, notify=True, reason=""):
    return client.put(f"/api/admin/assignments/{token}/reset",
                      headers=admin_headers,
                      json={"expires_at": None, "notify": notify,
                            "reason": reason})


def test_reset_records_who_did_it_and_why(client, admin_headers, make_job,
                                          make_candidate):
    """A voided attempt nobody owns is not defensible when a candidate
    challenges their rejection."""
    job = make_job()
    cand = make_candidate(job)
    token = _dispatch(job, cand)
    answer_paper(client, token, correct_ratio=0.4)
    _reset(client, admin_headers, token, notify=False,
           reason="Browser crashed 2 minutes in; candidate emailed support.")

    with session() as s:
        old = s.get(TestAssignment, token)
        assert old.superseded_by, "the reset is anonymous"
        assert "@" in old.superseded_by
        assert "Browser crashed" in old.reset_reason

    rows = client.get(f"/api/admin/candidates/{cand}/attempts",
                      headers=admin_headers).json()["attempts"]
    assert "Browser crashed" in rows[0]["reset_reason"]
    assert rows[0]["superseded_by"]


def test_reset_reason_is_optional(client, admin_headers, make_job,
                                  make_candidate):
    """Auditing must not become a wall someone works around."""
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    r = _reset(client, admin_headers, token, notify=False, reason="")
    assert r.status_code == 200
    with session() as s:
        assert s.get(TestAssignment, token).superseded_by, (
            "even an unexplained reset must record WHO did it")


def test_reset_mints_a_different_link(client, admin_headers, make_job,
                                      make_candidate):
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    answer_paper(client, token, correct_ratio=0.8)

    r = _reset(client, admin_headers, token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["uuid"] != token, "reset reused the old link"
    assert body["attempt_no"] == 2
    assert body["link"].endswith(body["uuid"])


def test_reset_keeps_the_old_attempt_intact(client, admin_headers, make_job,
                                            make_candidate):
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    answer_paper(client, token, correct_ratio=0.8)
    with session() as s:
        s.add(ProctorEvent(assignment_uuid=token, event_type="tab_switch",
                           detail="proving the log survives"))

    _reset(client, admin_headers, token)

    with session() as s:
        old = s.get(TestAssignment, token)
        assert old is not None, "the old attempt row was deleted"
        assert old.status == "submitted"
        assert old.test_score == 80.0
        assert old.superseded_at is not None
        assert s.execute(select(func.count(CandidateAnswer.id)).where(
            CandidateAnswer.assignment_uuid == token)).scalar() == 5, \
            "the old answers were deleted"
        assert s.execute(select(func.count(ProctorEvent.id)).where(
            ProctorEvent.assignment_uuid == token)).scalar() == 1, \
            "the old proctoring log was deleted"
        assert s.execute(select(func.count(AssignmentQuestion.id)).where(
            AssignmentQuestion.assignment_uuid == token)).scalar() == 5, \
            "the old drawn paper was deleted"


def test_the_replacement_link_actually_opens(client, admin_headers, make_job,
                                             make_candidate):
    """THE LANDMINE. If this fails, every retake is dead on arrival."""
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    answer_paper(client, token, correct_ratio=1.0)

    new = _reset(client, admin_headers, token).json()["uuid"]
    r = answer_paper(client, new, correct_ratio=1.0)
    assert r.status_code == 200, (
        f"the replacement link was refused ({r.status_code}: {r.text}). "
        "_check_duplicate is probably counting the kept attempt-1 submission.")
    assert r.json()["submitted"] is True


def test_the_old_link_stops_working(client, admin_headers, make_job,
                                    make_candidate):
    """Nobody may sit both attempts."""
    job = make_job()
    cand = make_candidate(job)
    token = _dispatch(job, cand)
    _reset(client, admin_headers, token)

    with session() as s:
        email = s.get(TestAssignment, token).candidate.email
    r = client.post(f"/api/portal/assignment/{token}/request-otp",
                    json={"email": email})
    assert r.status_code == 410
    assert "replaced" in r.text.lower()


def test_the_retake_draws_a_fresh_paper(client, admin_headers, make_job,
                                        make_candidate):
    """A retake must not be the paper they just saw."""
    job = make_job()
    token = _dispatch(job, make_candidate(job), pool_n=20, per_cand=5)
    first = {q["id"] for q in served_paper(client, token)}
    new = _reset(client, admin_headers, token).json()["uuid"]
    second = {q["id"] for q in served_paper(client, new)}
    assert first != second


def test_reset_emails_the_new_link(client, admin_headers, make_job,
                                   make_candidate, outbox):
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    outbox.clear()
    body = _reset(client, admin_headers, token, notify=True).json()
    assert body["emailed"] is True
    mail = outbox[-1]
    assert body["uuid"] in mail["body"], "the new link was not in the email"
    assert token not in mail["body"], "the dead link was emailed"


def test_reset_can_skip_the_email(client, admin_headers, make_job,
                                  make_candidate, outbox):
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    outbox.clear()
    _reset(client, admin_headers, token, notify=False)
    assert outbox == []


def test_a_superseded_attempt_cannot_be_reset_again(client, admin_headers,
                                                    make_job, make_candidate):
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    _reset(client, admin_headers, token)
    r = _reset(client, admin_headers, token)
    assert r.status_code == 409


def test_attempt_history_lists_every_attempt(client, admin_headers, make_job,
                                             make_candidate):
    job = make_job()
    cand = make_candidate(job)
    token = _dispatch(job, cand)
    answer_paper(client, token, correct_ratio=0.8)
    new = _reset(client, admin_headers, token).json()["uuid"]
    answer_paper(client, new, correct_ratio=1.0)

    h = client.get(f"/api/admin/candidates/{cand}/attempts",
                   headers=admin_headers).json()
    rows = h["attempts"]
    assert len(rows) == 2
    assert rows[0]["attempt_no"] == 1 and rows[0]["superseded"] is True
    assert rows[0]["test_score"] == 80.0
    assert rows[1]["attempt_no"] == 2 and rows[1]["superseded"] is False
    assert rows[1]["test_score"] == 100.0


def test_candidate_list_shows_the_live_attempt_and_best_score(
        client, admin_headers, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job)
    token = _dispatch(job, cand)
    answer_paper(client, token, correct_ratio=0.4)      # 40%
    new = _reset(client, admin_headers, token).json()["uuid"]
    answer_paper(client, new, correct_ratio=1.0)        # 100%

    rows = client.get(f"/api/admin/jobs/{job}/candidates",
                      headers=admin_headers).json()
    row = next(r for r in rows if r["uuid"] == cand)
    assert row["assignment_uuid"] == new, "the list is showing a dead attempt"
    assert row["total_attempts"] == 2
    assert row["best_test_score"] == 100.0, (
        "a reset must not look like a regression in the list")


def test_a_superseded_attempt_is_never_auto_submitted(client, admin_headers,
                                                      make_job, make_candidate):
    """Auto-submitting a replaced attempt would manufacture a score for a test
    the candidate was pulled out of."""
    from datetime import datetime, timedelta

    from portal.backend.routers.candidate import finalize_if_expired

    job = make_job()
    token = _dispatch(job, make_candidate(job))
    served_paper(client, token)                    # start it
    _reset(client, admin_headers, token)

    with session() as s:
        a = s.get(TestAssignment, token)
        a.started_at = datetime.utcnow() - timedelta(days=2)   # long past due
        assert finalize_if_expired(a, s) is False
        assert a.status == "started", "a replaced attempt was auto-submitted"
        assert a.test_score is None


def test_old_attempts_paper_stays_viewable(client, admin_headers, make_job,
                                           make_candidate):
    job = make_job()
    token = _dispatch(job, make_candidate(job))
    answer_paper(client, token, correct_ratio=0.8)
    _reset(client, admin_headers, token)

    d = client.get(f"/api/admin/assignments/{token}",
                   headers=admin_headers).json()
    assert d["total"] == 5 and d["correct"] == 4
