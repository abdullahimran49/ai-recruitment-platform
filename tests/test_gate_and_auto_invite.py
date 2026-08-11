"""The test-pass gate, and auto-invite on pass.

Auto-invite is the only feature in this system that contacts a candidate with
no human in the loop, so it carries the strictest rules:
  - OFF unless the job opted in
  - it may NEVER break the submission it rides on
  - it must not invite the same person twice
"""

from sqlalchemy import select

import db_bridge
from conftest import answer_paper, make_pool
from core.db import session
from core.models import AIInterview, Candidate, TestAssignment

BASE_CFG = {
    "resume_weight": 30, "test_weight": 30, "interview_weight": 40,
    "invite_threshold": 60, "onsite_threshold": 70, "onsite_top_n": 5,
    "require_test_pass": True, "auto_invite_on_pass": False,
    "auto_invite_delay_hours": 48, "auto_invite_duration_minutes": 20,
    "auto_invite_num_questions": 5,
}


def _cfg(client, admin_headers, job, **over):
    cfg = {**BASE_CFG, **over}
    r = client.put(f"/api/admin/jobs/{job}/merit-config", headers=admin_headers,
                   json=cfg)
    assert r.status_code == 200, r.text
    return r


def _invite(client, admin_headers, job, cand):
    return client.post("/api/admin/ai-interviews", headers=admin_headers,
                       json={"candidate_uuid": cand, "job_uuid": job,
                             "scheduled_at": "2026-09-01T10:00:00",
                             "duration_minutes": 20, "num_questions": 5})


def _sit(client, job, cand, ratio, pass_score=60):
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(10), [cand], duration_minutes=30, pass_score=pass_score,
        proctored=False, questions_per_candidate=4)
    return answer_paper(client, entries[0]["token"], correct_ratio=ratio)


# ---- the gate ----------------------------------------------------------------

def test_untested_candidate_cannot_be_invited(client, admin_headers, make_job,
                                              make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job)
    r = _invite(client, admin_headers, job, make_candidate(job))
    assert r.status_code == 409
    assert "has not taken the test" in r.text


def test_failed_candidate_cannot_be_invited(client, admin_headers, make_job,
                                            make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job)
    cand = make_candidate(job)
    _sit(client, job, cand, ratio=0.25)
    r = _invite(client, admin_headers, job, cand)
    assert r.status_code == 409
    assert "has not passed the test" in r.text


def test_passing_candidate_can_be_invited(client, admin_headers, make_job,
                                          make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job)
    cand = make_candidate(job)
    _sit(client, job, cand, ratio=1.0)
    r = _invite(client, admin_headers, job, cand)
    assert r.status_code == 200, r.text


def test_terminated_test_is_not_a_pass(client, admin_headers, make_job,
                                       make_candidate):
    """A high partial score on a test ended for cheating must not open the
    interview door."""
    job = make_job()
    _cfg(client, admin_headers, job)
    cand = make_candidate(job)
    entries, _ = db_bridge.create_test_with_assignments(
        job, make_pool(10), [cand], duration_minutes=30, pass_score=60,
        proctored=True, questions_per_candidate=4)
    token = entries[0]["token"]
    from core import security
    auth = {"Authorization": f"Bearer {security.candidate_token(token)}"}
    served = client.get(f"/api/portal/assignment/{token}/test",
                        headers=auth).json()["questions"]
    answers = [{"question_id": q["id"],
                "selected_index": int(q["question"].rsplit("option ", 1)[1]
                                      .rstrip(")"))} for q in served]
    client.post(f"/api/portal/assignment/{token}/submit", headers=auth,
                json={"answers": answers,
                      "terminated_reason": "multiple faces detected"})

    r = _invite(client, admin_headers, job, cand)
    assert r.status_code == 409
    assert "terminated" in r.text.lower()


def test_a_pass_that_was_reset_is_no_longer_a_pass(client, admin_headers,
                                                   make_job, make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job)
    cand = make_candidate(job)
    _sit(client, job, cand, ratio=1.0)
    with session() as s:
        token = s.execute(select(TestAssignment.uuid).where(
            TestAssignment.candidate_uuid == cand)).scalars().first()
    client.put(f"/api/admin/assignments/{token}/reset", headers=admin_headers,
               json={"expires_at": None, "notify": False})

    r = _invite(client, admin_headers, job, cand)
    assert r.status_code == 409, (
        "a superseded pass still counted — the candidate has no live result")


def test_the_gate_can_be_switched_off(client, admin_headers, make_job,
                                      make_candidate):
    """A job with no test would otherwise be un-interviewable forever."""
    job = make_job()
    _cfg(client, admin_headers, job, require_test_pass=False)
    r = _invite(client, admin_headers, job, make_candidate(job))
    assert r.status_code == 200, r.text


def test_admin_selects_multiple_interview_languages(
        client, admin_headers, make_job, make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job, require_test_pass=False)
    cand = make_candidate(job)
    r = client.post("/api/admin/ai-interviews", headers=admin_headers, json={
        "candidate_uuid": cand,
        "job_uuid": job,
        "scheduled_at": "2026-09-01T10:00:00",
        "duration_minutes": 20,
        "num_questions": 5,
        "languages": ["ur", "en", "ur"],
    })
    assert r.status_code == 200, r.text
    iv_uuid = r.json()["uuid"]

    rows = client.get(f"/api/admin/jobs/{job}/ai-interviews",
                      headers=admin_headers).json()
    row = next(item for item in rows if item["uuid"] == iv_uuid)
    assert [item["code"] for item in row["languages"]] == ["ur", "en"]
    assert row["languages"][0]["tts_locale"] == "ur-PK"

    public_info = client.get(
        f"/api/portal/interview/{iv_uuid}/info").json()
    assert [item["label"] for item in public_info["languages"]] == [
        "Urdu", "English"]


def test_interview_languages_require_a_supported_selection(
        client, admin_headers, make_job, make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job, require_test_pass=False)
    base = {
        "candidate_uuid": make_candidate(job),
        "job_uuid": job,
        "scheduled_at": "2026-09-01T10:00:00",
    }
    empty = client.post("/api/admin/ai-interviews", headers=admin_headers,
                        json={**base, "languages": []})
    unknown = client.post("/api/admin/ai-interviews", headers=admin_headers,
                          json={**base, "languages": ["xx"]})
    assert empty.status_code == 422
    assert unknown.status_code == 422


# ---- auto-invite -------------------------------------------------------------

def test_auto_invite_is_off_by_default(client, admin_headers, make_job,
                                       make_candidate, outbox):
    job = make_job()
    _cfg(client, admin_headers, job)          # auto_invite_on_pass=False
    cand = make_candidate(job)
    outbox.clear()
    r = _sit(client, job, cand, ratio=1.0)

    assert r.json()["auto_invited"] is False
    with session() as s:
        assert s.execute(select(AIInterview).where(
            AIInterview.candidate_uuid == cand)).scalars().all() == []
    assert not any("Interview Invitation" in m["subject"] for m in outbox), (
        "a candidate was emailed an interview invite without the job opting in")


def test_auto_invite_fires_on_a_pass_when_enabled(client, admin_headers,
                                                  make_job, make_candidate,
                                                  outbox):
    job = make_job()
    _cfg(client, admin_headers, job, auto_invite_on_pass=True)
    cand = make_candidate(job)
    outbox.clear()
    r = _sit(client, job, cand, ratio=1.0)

    assert r.json()["auto_invited"] is True
    with session() as s:
        ivs = s.execute(select(AIInterview).where(
            AIInterview.candidate_uuid == cand)).scalars().all()
        assert len(ivs) == 1
        assert s.get(Candidate, cand).status == "ai_interview_invited"
    assert any("Interview Invitation" in m["subject"] for m in outbox)


def test_auto_invite_does_not_fire_on_a_fail(client, admin_headers, make_job,
                                             make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job, auto_invite_on_pass=True)
    cand = make_candidate(job)
    r = _sit(client, job, cand, ratio=0.25)

    assert r.json()["auto_invited"] is False
    with session() as s:
        assert s.execute(select(AIInterview).where(
            AIInterview.candidate_uuid == cand)).scalars().all() == []


def test_auto_invite_is_idempotent_across_a_retake(client, admin_headers,
                                                   make_job, make_candidate):
    job = make_job()
    _cfg(client, admin_headers, job, auto_invite_on_pass=True)
    cand = make_candidate(job)
    _sit(client, job, cand, ratio=1.0)
    with session() as s:
        token = s.execute(select(TestAssignment.uuid).where(
            TestAssignment.candidate_uuid == cand)).scalars().first()
    new = client.put(f"/api/admin/assignments/{token}/reset",
                     headers=admin_headers,
                     json={"expires_at": None, "notify": False}).json()["uuid"]
    answer_paper(client, new, correct_ratio=1.0)

    with session() as s:
        ivs = s.execute(select(AIInterview).where(
            AIInterview.candidate_uuid == cand)).scalars().all()
    assert len(ivs) == 1, f"passing twice minted {len(ivs)} interviews"


def test_auto_invite_never_breaks_the_submission(client, admin_headers,
                                                 make_job, make_candidate,
                                                 monkeypatch):
    """A candidate who finished their test has earned their result. A blown-up
    auto-invite must not turn that into a 500."""
    import core.auto_invite as ai

    def boom(*a, **k):
        raise RuntimeError("mail server on fire")

    monkeypatch.setattr(ai.mailer, "send_email", boom)

    job = make_job()
    _cfg(client, admin_headers, job, auto_invite_on_pass=True)
    cand = make_candidate(job)
    r = _sit(client, job, cand, ratio=1.0)

    assert r.status_code == 200, "a failing auto-invite broke the submission"
    assert r.json()["submitted"] is True
    assert r.json()["auto_invited"] is False
    with session() as s:
        assert s.get(TestAssignment, s.execute(
            select(TestAssignment.uuid).where(
                TestAssignment.candidate_uuid == cand)).scalars().first()
        ).test_score == 100.0, "the score was lost"
