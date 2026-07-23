"""Email templates and the AI interviewer's CV brief."""

import pytest

from core import cv_brief, templates
from core.db import session
from core.models import Candidate


# ---- template rendering (pure) -----------------------------------------------

def test_render_substitutes_known_tokens():
    out = templates.render("Hi {{name}}, welcome to {{job}}.",
                           {"name": "Jane", "job": "Data Eng"})
    assert out == "Hi Jane, welcome to Data Eng."


def test_render_tolerates_whitespace_in_tokens():
    assert templates.render("Hi {{ name }}", {"name": "Jane"}) == "Hi Jane"


def test_unknown_token_is_left_visible_not_blanked():
    """A recruiter's typo must be obvious in the preview, not silently email a
    blank where the candidate's name should be."""
    out = templates.render("Hi {{nmae}}", {"name": "Jane"})
    assert out == "Hi {{nmae}}"


def test_render_is_not_a_template_engine():
    """Recruiters type into a textarea; stray syntax must not execute or raise."""
    for hostile in ("{% for x in y %}", "{{ 7*7 }}", "{{", "}}{{",
                    "${name}", "{{name.__class__}}"):
        templates.render(hostile, {"name": "Jane"})   # must not raise


# ---- template resolution (DB) ------------------------------------------------

def test_untouched_job_gets_the_builtin_wording(client, admin_headers,
                                                make_job):
    job = make_job()
    t = client.get(f"/api/admin/jobs/{job}/email-templates/onsite_interview",
                   headers=admin_headers).json()
    assert t["is_default"] is True
    assert t["source"] == "builtin"
    assert "We are pleased to invite you" in t["body"]


def test_saving_then_resetting_round_trips(client, admin_headers, make_job):
    job = make_job()
    url = f"/api/admin/jobs/{job}/email-templates/onsite_interview"
    client.put(url, headers=admin_headers,
               json={"subject": "S", "body": "Hi {{candidate_name}}"})
    t = client.get(url, headers=admin_headers).json()
    assert t["source"] == "job" and t["is_default"] is False

    client.delete(url, headers=admin_headers)
    t = client.get(url, headers=admin_headers).json()
    assert t["is_default"] is True
    assert "We are pleased to invite you" in t["body"]


def test_the_saved_template_is_what_actually_gets_sent(
        client, admin_headers, make_job, make_candidate, outbox):
    job = make_job()
    cand = make_candidate(job)
    client.put(f"/api/admin/jobs/{job}/email-templates/onsite_interview",
               headers=admin_headers,
               json={"subject": "{{job_title}} chat",
                     "body": "Yo {{candidate_name}}, {{date_time}}. "
                             "{{location_line}}"})
    outbox.clear()
    r = client.post("/api/admin/interviews", headers=admin_headers,
                    json={"candidate_uuid": cand, "job_uuid": job,
                          "interview_type": "in_person",
                          "scheduled_at": "2026-09-01T10:00:00",
                          "duration_minutes": 45,
                          "location": "Level 4"})
    assert r.status_code == 200, r.text
    assert r.json()["template_source"] == "job"
    mail = outbox[-1]
    assert mail["body"].startswith("Yo Pytest Candidate,")
    assert "Level 4" in mail["body"]
    assert "{{" not in mail["body"]


def test_empty_optional_lines_vanish_cleanly(client, admin_headers, make_job,
                                             make_candidate, outbox):
    """location_line/notes_block exist so recruiters never write conditionals."""
    job = make_job()
    cand = make_candidate(job)
    client.put(f"/api/admin/jobs/{job}/email-templates/onsite_interview",
               headers=admin_headers,
               json={"subject": "s",
                     "body": "A{{location_line}}{{notes_block}}B"})
    outbox.clear()
    client.post("/api/admin/interviews", headers=admin_headers,
                json={"candidate_uuid": cand, "job_uuid": job,
                      "interview_type": "phone",
                      "scheduled_at": "2026-09-01T10:00:00",
                      "duration_minutes": 30, "location": "", "notes": ""})
    assert outbox[-1]["body"] == "AB"


def test_preview_flags_a_mistyped_placeholder(client, admin_headers, make_job):
    job = make_job()
    r = client.post(f"/api/admin/jobs/{job}/email-templates/onsite_interview"
                    "/preview", headers=admin_headers,
                    json={"subject": "s", "body": "Hi {{candidat_name}}"}).json()
    assert r["unknown_placeholders"] == ["candidat_name"]
    assert "{{candidat_name}}" in r["body"]


def test_preview_does_not_save_or_send(client, admin_headers, make_job, outbox):
    job = make_job()
    outbox.clear()
    client.post(f"/api/admin/jobs/{job}/email-templates/onsite_interview"
                "/preview", headers=admin_headers,
                json={"subject": "s", "body": "draft only"})
    assert outbox == []
    t = client.get(f"/api/admin/jobs/{job}/email-templates/onsite_interview",
                   headers=admin_headers).json()
    assert t["is_default"] is True, "preview persisted the draft"


def test_unknown_template_kind_404s(client, admin_headers, make_job):
    job = make_job()
    r = client.get(f"/api/admin/jobs/{job}/email-templates/nonsense",
                   headers=admin_headers)
    assert r.status_code == 404


# ---- CV brief ----------------------------------------------------------------

def _cand(job_uuid, screening):
    with session() as s:
        c = Candidate(job_uuid=job_uuid, name="CV Tester",
                      email="pytest-cv@ats.local", screening_json=screening)
        s.add(c)
        s.flush()
        s.expunge(c)
        return c


def test_brief_is_built_from_the_structured_resume(make_job):
    job = make_job()
    c = _cand(job, {"structured": {
        "years_experience": 6,
        "summary": "Data engineer focused on pipelines.",
        "skills": ["Python", "SQL", "Airflow"],
        "experience": [{"title": "Senior Data Engineer", "company": "Acme",
                        "duration": "3 years",
                        "summary": "Owned the ETL platform."}],
        "education": [{"degree": "BSc", "field": "CS",
                       "institution": "State Uni"}],
        "certifications": ["AWS SA"],
    }})
    brief = cv_brief.build(c)
    assert "Acme" in brief and "Airflow" in brief and "6 years" in brief


def test_brief_falls_back_to_evidence_for_older_candidates(make_job):
    """Candidates screened before the parsed resume was stored must still get
    a CV-grounded interview, not a generic one."""
    job = make_job()
    c = _cand(job, {"criteria": [{
        "criterion_text": "Python & ETL", "met": 0.9,
        "evidence": "Built nightly ETL pipelines in Python at Acme."}]})
    brief = cv_brief.build(c)
    assert "Acme" in brief
    assert "strong" in brief


def test_brief_surfaces_gaps_to_probe(make_job):
    job = make_job()
    c = _cand(job, {"structured": {"skills": ["Python"]},
                    "must_have_gaps": ["No Kubernetes experience"]})
    assert "Kubernetes" in cv_brief.build(c)


def test_no_screening_yields_an_empty_brief(make_job):
    """Empty is meaningful: the agent then asks JD-only questions instead of
    inventing a background for someone."""
    job = make_job()
    assert cv_brief.build(_cand(job, {})) == ""
    assert cv_brief.build(_cand(job, None)) == ""


def test_brief_keeps_its_layout_when_truncated(make_job):
    """It rides in room metadata and lands in a system prompt — it must stay
    bounded, and stay readable."""
    job = make_job()
    c = _cand(job, {"structured": {
        "summary": "x" * 500,
        "skills": [f"skill{i}" for i in range(200)],
        "experience": [{"title": f"Role {i}", "company": f"Co {i}",
                        "summary": "y" * 300} for i in range(10)],
    }})
    brief = cv_brief.build(c)
    assert len(brief) <= 2100
    assert "\n" in brief, "truncation flattened the layout into one line"
