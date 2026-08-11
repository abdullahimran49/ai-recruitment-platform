"""Public job-portal flow: register -> browse -> apply -> track.

The apply path normally calls the LLM screening pipeline; here it is patched to
a deterministic ResumeResult so the test stays fast and offline. The point of
these tests is the plumbing — CNIC uniqueness, the application landing as a
Candidate HR can see, and the tracking view — not the scoring itself (that is
covered by test_papers/scoring paths).
"""

import random
import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

import portal.backend.routers.portal as portal_mod
from core.db import session
from core.models import (Applicant, AssignmentQuestion, Candidate,
                         CandidateAnswer, Job, PipelineStage, ProctorEvent,
                         Scorecard, Test, TestAssignment)
from schemas import ResumeResult, StructuredResume

TEST_DOMAIN = "ats.local"


def _cnic() -> str:
    return str(random.randint(10 ** 12, 10 ** 13 - 1))


@pytest.fixture(autouse=True)
def _isolate_resume_dir(tmp_path, monkeypatch):
    """Write uploaded resumes to a temp dir, never the real uploads/ folder."""
    monkeypatch.setattr(portal_mod, "_RESUME_DIR", str(tmp_path / "resumes"))


@pytest.fixture(autouse=True)
def _stub_screening(monkeypatch):
    """No LLM: apply gets a fixed score and a parsed resume."""
    def fake_screen(job, resume_text, filename):
        return ResumeResult(
            filename=filename, candidate_name="Parsed Name", score=77.0,
            raw_score=80.0,
            structured=StructuredResume(name="Parsed Name",
                                        email="parsed@example.com",
                                        years_experience=4),
            criterion_scores=[], must_have_gaps=[], employment_gaps=[])
    monkeypatch.setattr(portal_mod, "screen_resume_for_job", fake_screen)
    monkeypatch.setattr(portal_mod.pdf, "extract_text",
                        lambda data: "resume text")


@pytest.fixture
def make_applicant(client):
    created = []

    def _make(cnic=None, email=None, password="password123",
              name="Portal Tester", phone="03001234567"):
        cnic = cnic or _cnic()
        email = email or f"applicant-{uuid.uuid4().hex[:6]}@{TEST_DOMAIN}"
        r = client.post("/api/portal/register", json={
            "name": name, "email": email, "cnic": cnic,
            "phone": phone, "password": password})
        assert r.status_code == 200, r.text
        data = r.json()
        uid = data["applicant"]["uuid"]
        created.append(uid)
        return {
            "uuid": uid, "email": email, "cnic": cnic, "password": password,
            "token": data["token"],
            "headers": {"Authorization": f"Bearer {data['token']}"},
        }

    yield _make

    # Order-independent teardown: clear the child chain under each applicant's
    # candidates before the candidate, then the applicant. (make_job's cascade
    # may or may not have run first, depending on fixture teardown order.)
    with session() as s:
        for uid in created:
            cand_ids = list(s.execute(select(Candidate.uuid).where(
                Candidate.applicant_uuid == uid)).scalars())
            if cand_ids:
                asg_ids = list(s.execute(select(TestAssignment.uuid).where(
                    TestAssignment.candidate_uuid.in_(cand_ids))).scalars())
                if asg_ids:
                    s.execute(delete(CandidateAnswer).where(
                        CandidateAnswer.assignment_uuid.in_(asg_ids)))
                    s.execute(delete(ProctorEvent).where(
                        ProctorEvent.assignment_uuid.in_(asg_ids)))
                    s.execute(delete(AssignmentQuestion).where(
                        AssignmentQuestion.assignment_uuid.in_(asg_ids)))
                    s.execute(delete(TestAssignment).where(
                        TestAssignment.uuid.in_(asg_ids)))
                s.execute(delete(Scorecard).where(
                    Scorecard.candidate_uuid.in_(cand_ids)))
                s.execute(delete(Candidate).where(
                    Candidate.uuid.in_(cand_ids)))
            s.execute(delete(Applicant).where(Applicant.uuid == uid))


def _publish(job_uuid, deadline_days=7):
    with session() as s:
        j = s.get(Job, job_uuid)
        j.is_published = True
        j.location = "Karachi"
        j.employment_type = "Full-time"
        if deadline_days is not None:
            j.application_deadline = datetime.utcnow() + timedelta(days=deadline_days)
        else:
            j.application_deadline = None


def _pdf_upload():
    return {"resume": ("cv.pdf", b"%PDF-1.4 fake bytes", "application/pdf")}


# ---- registration + auth ----------------------------------------------------

def test_register_and_login(client, make_applicant):
    a = make_applicant()
    # login with the same credentials
    r = client.post("/api/portal/login",
                    json={"email": a["email"], "password": a["password"]})
    assert r.status_code == 200, r.text
    assert r.json()["applicant"]["cnic"] == f"*********{a['cnic'][-4:]}"
    # wrong password
    bad = client.post("/api/portal/login",
                      json={"email": a["email"], "password": "nope"})
    assert bad.status_code == 401


def test_duplicate_cnic_rejected(client, make_applicant):
    a = make_applicant()
    r = client.post("/api/portal/register", json={
        "name": "Someone Else", "email": f"other-{uuid.uuid4().hex[:6]}@{TEST_DOMAIN}",
        "cnic": a["cnic"], "phone": "0300", "password": "password123"})
    assert r.status_code == 409
    assert "cnic" in r.json()["detail"].lower()


def test_duplicate_email_rejected(client, make_applicant):
    a = make_applicant()
    r = client.post("/api/portal/register", json={
        "name": "Someone Else", "email": a["email"],
        "cnic": _cnic(), "phone": "0300", "password": "password123"})
    assert r.status_code == 409


def test_bad_cnic_rejected(client):
    r = client.post("/api/portal/register", json={
        "name": "X", "email": f"x-{uuid.uuid4().hex[:6]}@{TEST_DOMAIN}",
        "cnic": "123", "phone": "", "password": "password123"})
    assert r.status_code == 422


# ---- job listing ------------------------------------------------------------

def test_only_published_open_jobs_listed(client, make_job, make_applicant):
    hidden = make_job(title=f"pytest hidden {uuid.uuid4().hex[:6]} (throwaway)")
    shown = make_job(title=f"pytest shown {uuid.uuid4().hex[:6]} (throwaway)")
    closed = make_job(title=f"pytest closed {uuid.uuid4().hex[:6]} (throwaway)")
    _publish(shown)
    _publish(closed, deadline_days=-1)  # deadline already passed

    listed = {j["uuid"] for j in client.get("/api/portal/jobs").json()["jobs"]}
    assert shown in listed
    assert hidden not in listed
    assert closed not in listed

    # detail 404s for an unpublished job, 200 for a published one
    assert client.get(f"/api/portal/jobs/{hidden}").status_code == 404
    assert client.get(f"/api/portal/jobs/{shown}").status_code == 200


# ---- apply ------------------------------------------------------------------

def test_apply_creates_candidate_for_hr(client, make_job, make_applicant):
    job = make_job()
    _publish(job)
    a = make_applicant()

    r = client.post(f"/api/portal/jobs/{job}/apply",
                    headers=a["headers"], files=_pdf_upload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resume_score"] == 77.0

    # It lands as a Candidate HR can see, linked to the applicant, from portal.
    with session() as s:
        cand = s.execute(select(Candidate).where(
            Candidate.uuid == body["application_uuid"])).scalars().one()
        assert cand.applicant_uuid == a["uuid"]
        assert cand.source == "portal"
        assert cand.resume_score == 77.0
        assert cand.email == a["email"]
        stage = s.get(PipelineStage, cand.stage_id)
        assert stage and stage.name == "Applied"


def test_cannot_apply_twice(client, make_job, make_applicant):
    job = make_job()
    _publish(job)
    a = make_applicant()
    first = client.post(f"/api/portal/jobs/{job}/apply",
                        headers=a["headers"], files=_pdf_upload())
    assert first.status_code == 200
    again = client.post(f"/api/portal/jobs/{job}/apply",
                        headers=a["headers"], files=_pdf_upload())
    assert again.status_code == 409


def test_cannot_apply_to_closed_job(client, make_job, make_applicant):
    job = make_job()
    _publish(job, deadline_days=-1)
    a = make_applicant()
    r = client.post(f"/api/portal/jobs/{job}/apply",
                    headers=a["headers"], files=_pdf_upload())
    assert r.status_code == 409


def test_apply_requires_auth(client, make_job):
    job = make_job()
    _publish(job)
    r = client.post(f"/api/portal/jobs/{job}/apply", files=_pdf_upload())
    assert r.status_code == 401


# ---- tracking ---------------------------------------------------------------

def test_track_shows_status_and_links(client, make_job, make_applicant):
    job = make_job()
    _publish(job)
    a = make_applicant()
    apply_r = client.post(f"/api/portal/jobs/{job}/apply",
                          headers=a["headers"], files=_pdf_upload())
    cand_uuid = apply_r.json()["application_uuid"]

    # No test link yet.
    apps = client.get("/api/portal/me/applications",
                      headers=a["headers"]).json()["applications"]
    assert len(apps) == 1
    assert apps[0]["job_title"]
    assert apps[0]["stage"] == "Applied"
    assert apps[0]["test"] is None

    # Give the candidate a live test assignment; tracking should surface it.
    with session() as s:
        test = Test(job_uuid=job, duration_minutes=20)
        s.add(test)
        s.flush()
        asg = TestAssignment(test_uuid=test.uuid, candidate_uuid=cand_uuid,
                             status="pending")
        s.add(asg)
        s.flush()
        asg_uuid = asg.uuid

    apps = client.get("/api/portal/me/applications",
                      headers=a["headers"]).json()["applications"]
    assert apps[0]["test"] is not None
    assert asg_uuid in apps[0]["test"]["link"]


# ---- password reset ---------------------------------------------------------

def test_password_reset_flow(client, make_applicant, outbox):
    a = make_applicant(password="oldpassword123")
    r = client.post("/api/portal/forgot-password", json={"email": a["email"]})
    assert r.status_code == 200

    mail = next(m for m in outbox
                if m["to"] == a["email"] and "reset" in m["subject"].lower())
    code = re.search(r"\b(\d{6})\b", mail["body"]).group(1)

    rr = client.post("/api/portal/reset-password", json={
        "email": a["email"], "code": code, "new_password": "brandnew123"})
    assert rr.status_code == 200
    assert rr.json()["token"]

    # Old password no longer works; the new one does.
    assert client.post("/api/portal/login", json={
        "email": a["email"], "password": "oldpassword123"}).status_code == 401
    assert client.post("/api/portal/login", json={
        "email": a["email"], "password": "brandnew123"}).status_code == 200


def test_forgot_password_unknown_email_is_quiet(client):
    # No account -> still 200 (no user enumeration), no email sent.
    r = client.post("/api/portal/forgot-password",
                    json={"email": f"nobody-{uuid.uuid4().hex[:6]}@ats.local"})
    assert r.status_code == 200


def test_reset_with_bad_code_rejected(client, make_applicant, outbox):
    a = make_applicant()
    client.post("/api/portal/forgot-password", json={"email": a["email"]})
    r = client.post("/api/portal/reset-password", json={
        "email": a["email"], "code": "000000", "new_password": "whatever123"})
    assert r.status_code == 400


# ---- application confirmation email -----------------------------------------

def test_apply_sends_confirmation_email(client, make_job, make_applicant, outbox):
    job = make_job()
    _publish(job)
    a = make_applicant()
    client.post(f"/api/portal/jobs/{job}/apply",
                headers=a["headers"], files=_pdf_upload())
    assert any(m["to"] == a["email"]
               and "application received" in m["subject"].lower()
               for m in outbox)


# ---- resume update + withdraw -----------------------------------------------

def _apply(client, job, a):
    return client.post(f"/api/portal/jobs/{job}/apply",
                       headers=a["headers"], files=_pdf_upload()
                       ).json()["application_uuid"]


def test_update_resume_rescreens(client, make_job, make_applicant):
    job = make_job()
    _publish(job)
    a = make_applicant()
    cand = _apply(client, job, a)
    r = client.post(f"/api/portal/applications/{cand}/resume",
                    headers=a["headers"], files=_pdf_upload())
    assert r.status_code == 200
    assert r.json()["resume_score"] == 77.0  # from the stubbed screener


def test_resume_locked_once_test_assigned(client, make_job, make_applicant):
    job = make_job()
    _publish(job)
    a = make_applicant()
    cand = _apply(client, job, a)
    with session() as s:
        t = Test(job_uuid=job, duration_minutes=20)
        s.add(t)
        s.flush()
        s.add(TestAssignment(test_uuid=t.uuid, candidate_uuid=cand,
                             status="pending"))
    r = client.post(f"/api/portal/applications/{cand}/resume",
                    headers=a["headers"], files=_pdf_upload())
    assert r.status_code == 409


def test_withdraw_application(client, make_job, make_applicant):
    job = make_job()
    _publish(job)
    a = make_applicant()
    cand = _apply(client, job, a)

    r = client.post(f"/api/portal/applications/{cand}/withdraw",
                    headers=a["headers"])
    assert r.status_code == 200

    app = client.get("/api/portal/me/applications",
                     headers=a["headers"]).json()["applications"][0]
    assert app["withdrawn"] is True
    assert app["can_withdraw"] is False
    # Cannot update the resume of a withdrawn application.
    blocked = client.post(f"/api/portal/applications/{cand}/resume",
                          headers=a["headers"], files=_pdf_upload())
    assert blocked.status_code == 409


def test_cannot_touch_another_applicants_application(client, make_job,
                                                     make_applicant):
    job = make_job()
    _publish(job)
    owner = make_applicant()
    cand = _apply(client, job, owner)
    intruder = make_applicant()
    r = client.post(f"/api/portal/applications/{cand}/withdraw",
                    headers=intruder["headers"])
    assert r.status_code == 404
