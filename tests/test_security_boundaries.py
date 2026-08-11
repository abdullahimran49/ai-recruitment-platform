"""Regression guards for cross-tenant and candidate-flow security."""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select

import db_bridge
from conftest import make_pool
from core import otps, security
from core.db import session
from core.job_delete import cascade_delete_job
from core.models import (AIInterview, CandidateAnswer, Department, Job, Otp,
                         TestAssignment, User)


def test_otp_cannot_cross_purpose_or_resource():
    email = f"otp-{uuid.uuid4().hex[:8]}@ats.local"
    resource = str(uuid.uuid4())
    other_resource = str(uuid.uuid4())
    try:
        with session() as db:
            code = otps.issue(db, email, otps.ASSESSMENT, resource)
            db.flush()
            assert otps.verify(db, email, code, otps.INTERVIEW,
                               resource)[0] is False
            assert otps.verify(db, email, code, otps.ASSESSMENT,
                               other_resource)[0] is False
            assert otps.verify(db, email, code, otps.ASSESSMENT,
                               resource)[0] is True
    finally:
        with session() as db:
            db.execute(delete(Otp).where(Otp.email == email))


def test_test_proctor_limit_closes_assignment_server_side(
        client, make_job, make_candidate):
    job = make_job()
    candidate = make_candidate(job)
    rows, _ = db_bridge.create_test_with_assignments(
        job, make_pool(4), [candidate], duration_minutes=30,
        proctored=True, max_warnings=1, questions_per_candidate=4)
    token = rows[0]["token"]
    headers = {"Authorization": f"Bearer {security.candidate_token(token)}"}
    paper = client.get(f"/api/portal/assignment/{token}/test",
                       headers=headers).json()["questions"]
    client.post(f"/api/portal/assignment/{token}/draft", headers=headers,
                json={"answers": [{"question_id": paper[0]["id"],
                                    "selected_index": 0}]})
    response = client.post(
        f"/api/portal/assignment/{token}/proctor-event", headers=headers,
        json={"event_type": "tab_switch", "detail": "left tab"})
    assert response.status_code == 200
    assert response.json()["terminate"] is True
    with session() as db:
        assignment = db.get(TestAssignment, token)
        assert assignment.status == "terminated"
        assert assignment.terminated_reason
        assert db.execute(select(func.count(CandidateAnswer.id)).where(
            CandidateAnswer.assignment_uuid == token)).scalar() == 4


def test_interview_proctor_limit_closes_interview_server_side(
        client, make_job, make_candidate):
    job = make_job()
    candidate = make_candidate(job)
    with session() as db:
        interview = AIInterview(
            candidate_uuid=candidate, job_uuid=job,
            scheduled_at=datetime.utcnow(), status="started",
            started_at=datetime.utcnow(), max_warnings=1)
        db.add(interview)
        db.flush()
        token = interview.uuid
    headers = {"Authorization": f"Bearer {security.candidate_token(token)}"}
    response = client.post(
        f"/api/portal/interview/{token}/proctor-event", headers=headers,
        json={"event_type": "tab_switch", "detail": "left tab"})
    assert response.status_code == 200
    assert response.json()["terminate"] is True
    with session() as db:
        interview = db.get(AIInterview, token)
        assert interview.status == "terminated"
        assert interview.terminated_reason


def test_department_admin_cannot_see_or_open_another_department(
        client, make_candidate):
    suffix = uuid.uuid4().hex[:8]
    with session() as db:
        own = Department(name=f"pytest-own-{suffix}")
        other = Department(name=f"pytest-other-{suffix}")
        db.add_all([own, other])
        db.flush()
        admin = User(
            name="Department Admin", email=f"dept-{suffix}@ats.local",
            password_hash=security.hash_password("password123"),
            role="admin", department_id=own.id)
        hidden_job = Job(title=f"pytest hidden {suffix} (throwaway)",
                         department_id=other.id, jd_text="Private")
        db.add_all([admin, hidden_job])
        db.flush()
        admin_uuid, own_id, other_id = admin.uuid, own.id, other.id
        hidden_uuid = hidden_job.uuid
        headers = {"Authorization": "Bearer " + security.admin_token(
            admin.uuid, admin.role, admin.department_id)}
    candidate = make_candidate(hidden_uuid)
    try:
        jobs = client.get("/api/admin/jobs", headers=headers).json()
        assert all(row["uuid"] != hidden_uuid for row in jobs)
        assert client.get(f"/api/admin/jobs/{hidden_uuid}",
                          headers=headers).status_code == 403
        inbox = client.get("/api/admin/applications", headers=headers).json()
        assert all(row["uuid"] != candidate
                   for row in inbox["applications"])
        assert client.get(f"/api/admin/applications?department_id={other_id}",
                          headers=headers).status_code == 403
    finally:
        with session() as db:
            hidden_job = db.get(Job, hidden_uuid)
            if hidden_job:
                cascade_delete_job(db, hidden_job)
            db.execute(delete(User).where(User.uuid == admin_uuid))
            db.execute(delete(Department).where(
                Department.id.in_([own_id, other_id])))
