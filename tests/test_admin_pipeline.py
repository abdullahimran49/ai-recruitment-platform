"""Admin-side hiring pipeline: job posting, stages, per-candidate stage moves,
sending a test to any candidate (portal applicants included), resume viewing,
and the cross-role applications inbox.

The Kanban board and stage CRUD were intentionally removed (candidate stages
now advance automatically on pipeline events, with a manual override in the
applications inbox), so this file no longer tests a drag-and-drop board.

Uses the shared `client` + `admin_headers` (super admin) fixtures. Jobs created
through the API are cleaned up via the real DELETE cascade.
"""

import uuid

import db_bridge
from core import bank
from core.db import session
from core.models import Candidate, Test, TestAssignment


def _dept_id(client, headers):
    depts = client.get("/api/admin/departments", headers=headers).json()
    return depts[0]["id"]


def test_post_and_delete_job(client, admin_headers):
    dept = _dept_id(client, admin_headers)
    title = f"pytest posted {uuid.uuid4().hex[:8]} (throwaway)"
    r = client.post("/api/admin/jobs", headers=admin_headers, json={
        "title": title, "department_id": dept, "jd_text": "Python, SQL.",
        "location": "Karachi", "employment_type": "Full-time",
        "openings": 3, "is_published": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_published"] is True
    job_uuid = body["uuid"]

    jobs = client.get("/api/admin/jobs", headers=admin_headers).json()
    row = next(j for j in jobs if j["uuid"] == job_uuid)
    assert row["is_published"] is True
    assert row["openings"] == 3

    d = client.delete(f"/api/admin/jobs/{job_uuid}", headers=admin_headers)
    assert d.status_code == 200


def test_pipeline_stages_seeded(client, admin_headers):
    stages = client.get("/api/admin/pipeline-stages", headers=admin_headers).json()
    names = [s["name"] for s in stages]
    assert "Applied" in names and "Hired" in names and "Rejected" in names
    orders = [s["sort_order"] for s in stages]
    assert orders == sorted(orders)


def test_move_candidate_stage(client, admin_headers, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job, name="Stage Mover", score=71)
    stages = client.get("/api/admin/pipeline-stages", headers=admin_headers).json()
    target = stages[2]["id"]  # e.g. "Test"

    mv = client.patch(f"/api/admin/candidates/{cand}/stage",
                      headers=admin_headers, json={"stage_id": target})
    assert mv.status_code == 200
    assert mv.json()["stage_id"] == target

    # The inbox reflects the new stage.
    apps = client.get(f"/api/admin/applications?job_uuid={job}",
                      headers=admin_headers).json()["applications"]
    row = next(a for a in apps if a["uuid"] == cand)
    assert row["stage_id"] == target


def test_move_rejects_unknown_stage(client, admin_headers, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job)
    r = client.patch(f"/api/admin/candidates/{cand}/stage",
                     headers=admin_headers, json={"stage_id": 99999})
    assert r.status_code == 404


def test_applications_inbox_filters(client, admin_headers, make_job, make_candidate):
    job = make_job(title=f"pytest inbox {uuid.uuid4().hex[:8]} (throwaway)")
    cand = make_candidate(job, name="Zebra Uniquename", score=88)

    allapps = client.get("/api/admin/applications",
                         headers=admin_headers).json()["applications"]
    assert any(a["uuid"] == cand for a in allapps)

    byjob = client.get(f"/api/admin/applications?job_uuid={job}",
                       headers=admin_headers).json()["applications"]
    assert byjob and all(a["job_uuid"] == job for a in byjob)
    assert byjob[0]["source"] == "upload"  # make_candidate default

    byq = client.get("/api/admin/applications?q=Zebra Uniquename",
                     headers=admin_headers).json()["applications"]
    assert any(a["uuid"] == cand for a in byq)

    portal_only = client.get("/api/admin/applications?source=portal&job_uuid=" + job,
                             headers=admin_headers).json()["applications"]
    assert all(a["uuid"] != cand for a in portal_only)


# ---- Sending a test to a candidate (closes the portal-applicant loop) -------

def _seed_bank(job_uuid, n=3):
    with session() as s:
        for i in range(n):
            bank.add_item(s, job_uuid, f"Bank Q{i}?",
                          [f"opt{i}a", f"opt{i}b", f"opt{i}c", f"opt{i}d"], i % 4)


def test_send_test_from_bank(client, admin_headers, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job, name="Test Taker")
    _seed_bank(job, n=3)

    r = client.post(f"/api/admin/candidates/{cand}/send-test",
                    headers=admin_headers, json={"duration_minutes": 15})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["num_questions"] == 3
    assert body["stage"] == "Test"
    assert body["assignment_uuid"]

    # Stage auto-advanced to Test, visible in the inbox.
    apps = client.get(f"/api/admin/applications?job_uuid={job}",
                      headers=admin_headers).json()["applications"]
    row = next(a for a in apps if a["uuid"] == cand)
    assert row["stage"] == "Test"

    # A second send is blocked while a live link exists.
    again = client.post(f"/api/admin/candidates/{cand}/send-test",
                        headers=admin_headers, json={})
    assert again.status_code == 409


def test_send_test_without_bank_400(client, admin_headers, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job)
    r = client.post(f"/api/admin/candidates/{cand}/send-test",
                    headers=admin_headers, json={})
    assert r.status_code == 400


def test_bulk_send_test(client, admin_headers, make_job, make_candidate):
    job = make_job()
    c1 = make_candidate(job, name="Bulk One")
    c2 = make_candidate(job, name="Bulk Two")
    _seed_bank(job, n=3)

    r = client.post(f"/api/admin/jobs/{job}/send-tests", headers=admin_headers,
                    json={"candidate_uuids": [c1, c2], "duration_minutes": 12})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent_count"] == 2
    assert all(item["num_questions"] == 3 for item in body["sent"])

    # Re-sending to the same people skips both (they now have live links).
    again = client.post(f"/api/admin/jobs/{job}/send-tests",
                        headers=admin_headers,
                        json={"candidate_uuids": [c1, c2]})
    assert again.json()["skipped_count"] == 2

    # Empty selection is a 400.
    empty = client.post(f"/api/admin/jobs/{job}/send-tests",
                        headers=admin_headers, json={"candidate_uuids": []})
    assert empty.status_code == 400


# ---- Resume viewing ---------------------------------------------------------

# ---- Recruiter app: load a job's actual resume PDFs (db_bridge) -------------

def test_load_job_resume_files_excludes_tested(make_job, make_candidate, tmp_path):
    """Loading a saved job returns the actual portal PDFs of its applicants,
    but hides anyone already sent a test (or withdrawn)."""
    job = make_job()
    fresh = make_candidate(job, name="Fresh Applicant", key="fresh", score=72)
    tested = make_candidate(job, name="Already Tested", key="tested", score=88)
    withdrawn = make_candidate(job, name="Gone", key="gone", score=60)

    with session() as s:
        for cid, fn in [(fresh, "fresh.pdf"), (tested, "tested.pdf"),
                        (withdrawn, "gone.pdf")]:
            p = tmp_path / fn
            p.write_bytes(b"%PDF-1.4 pytest resume")
            s.get(Candidate, cid).resume_path = str(p)
        s.get(Candidate, withdrawn).status = "withdrawn"
        t = Test(job_uuid=job, duration_minutes=20)
        s.add(t)
        s.flush()
        s.add(TestAssignment(test_uuid=t.uuid, candidate_uuid=tested,
                             status="pending"))

    files, hidden = db_bridge.load_job_resume_files(job)
    loaded = {f["candidate_uuid"] for f in files}
    assert fresh in loaded
    assert tested not in loaded       # already invited -> hidden
    assert withdrawn not in loaded    # withdrawn -> hidden
    assert hidden >= 2

    f = next(f for f in files if f["candidate_uuid"] == fresh)
    assert f["bytes"].startswith(b"%PDF")           # the real file
    assert f["email"].endswith("@ats.local")


def test_resume_missing_returns_404(client, admin_headers, make_job, make_candidate):
    job = make_job()
    cand = make_candidate(job)  # make_candidate stores no resume file
    r = client.get(f"/api/admin/candidates/{cand}/resume", headers=admin_headers)
    assert r.status_code == 404


def test_resume_served_via_header_and_token(client, admin_headers, make_job,
                                            make_candidate, tmp_path):
    job = make_job()
    cand = make_candidate(job)
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 pytest resume")
    with session() as s:
        s.get(Candidate, cand).resume_path = str(pdf)

    # Via Authorization header.
    r = client.get(f"/api/admin/candidates/{cand}/resume", headers=admin_headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"

    # Via ?token= query (so it opens in a new browser tab).
    tok = admin_headers["Authorization"].split(" ", 1)[1]
    r2 = client.get(f"/api/admin/candidates/{cand}/resume?token={tok}")
    assert r2.status_code == 200

    # No auth at all is rejected.
    r3 = client.get(f"/api/admin/candidates/{cand}/resume")
    assert r3.status_code == 401
