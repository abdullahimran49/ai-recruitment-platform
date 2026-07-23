"""Shared fixtures for the regression suite.

Design goals, in order:

1. NOTHING LEAVES THE MACHINE. `core.mailer.send_email` is patched out for the
   whole session (autouse, not opt-in) so a test can never email a real
   candidate. Nearly every write path in this app sends mail, so an opt-in
   guard would be one forgotten fixture away from mailing a stranger.
2. NO SERVER NEEDED. The API is driven in-process through TestClient, so
   `pytest` is a single command with nothing to start first.
3. NO LEFTOVER DATA. Every job a test creates is deleted through the real
   cascade on teardown — which doubles as a standing check that the cascade
   still covers every child table.
4. NO LLM BY DEFAULT. Tests needing a live model are marked `llm` and skipped
   unless asked for: the suite must stay fast, free and deterministic.

Runs against the configured DB (local SQL Server) using throwaway rows only.
"""

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import mailer, security  # noqa: E402
from core.db import session  # noqa: E402
from core.models import Candidate, Department, Job, User  # noqa: E402
from schemas import MCQQuestion, MCQTest  # noqa: E402

TEST_EMAIL_DOMAIN = "ats.local"   # never a real inbox


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "llm: needs a live LLM (slow, costs tokens, non-deterministic)")


def pytest_addoption(parser):
    parser.addoption("--llm", action="store_true", default=False,
                     help="also run tests that call a real LLM")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--llm"):
        return
    skip = pytest.mark.skip(reason="needs --llm")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True, scope="session")
def _never_send_real_email():
    """Hard block on outbound mail for the entire suite.

    Autouse + session-scoped on purpose: this is a safety property, not a
    convenience. Individual tests read what "would" have been sent via the
    `outbox` fixture.
    """
    sent = []

    def fake_send(to_addr, subject, body):
        assert to_addr.endswith(f"@{TEST_EMAIL_DOMAIN}"), (
            f"A test tried to email {to_addr!r} — tests must only ever use "
            f"@{TEST_EMAIL_DOMAIN} addresses.")
        sent.append({"to": to_addr, "subject": subject, "body": body})
        return True, f"Sent to {to_addr}"

    real = mailer.send_email
    mailer.send_email = fake_send
    # Modules that imported the symbol directly need rebinding too.
    import core.auto_invite as auto_invite_mod
    import portal.backend.routers.admin as admin_mod
    import portal.backend.routers.candidate as candidate_mod
    for mod in (admin_mod, candidate_mod, auto_invite_mod):
        if hasattr(mod, "mailer"):
            mod.mailer.send_email = fake_send

    yield sent
    mailer.send_email = real


@pytest.fixture
def outbox(_never_send_real_email):
    """The emails this test would have sent. Cleared per test."""
    _never_send_real_email.clear()
    return _never_send_real_email


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from portal.backend.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_headers():
    with session() as s:
        u = s.execute(
            __import__("sqlalchemy").select(User).where(
                User.role == "super_admin")).scalars().first()
        assert u, "No super_admin in the DB — run `python -m core.init_db` first."
        tok = security.admin_token(u.uuid, u.role, u.department_id)
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def make_job():
    """Create throwaway jobs; every one is cascade-deleted on teardown."""
    created = []

    def _make(title=None, jd="Test job: Python, SQL, ETL pipelines, ML models."):
        title = title or f"pytest {uuid.uuid4().hex[:8]} (throwaway)"
        with session() as s:
            dept = s.execute(
                __import__("sqlalchemy").select(Department)).scalars().first()
            j = Job(title=title, department_id=dept.id, jd_text=jd)
            s.add(j)
            s.flush()
            created.append(title)
            return j.uuid

    yield _make

    import db_bridge
    for title in created:
        try:
            db_bridge.delete_job_by_title(title)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                f"Cascade delete failed for {title!r}: {e}\n"
                "A child table is probably missing from core/job_delete.py."
            ) from e


@pytest.fixture
def make_candidate():
    def _make(job_uuid, name="Pytest Candidate", key=None, score=90,
              screening=None):
        key = key or uuid.uuid4().hex[:6]
        with session() as s:
            c = Candidate(job_uuid=job_uuid, name=name,
                          email=f"pytest-{key}@{TEST_EMAIL_DOMAIN}",
                          resume_score=score, screening_json=screening or {})
            s.add(c)
            s.flush()
            return c.uuid
    return _make


def make_pool(n=12, difficulty="medium", categories=None):
    """A deterministic pool. Each question names its own correct option, so a
    test can answer any drawn paper without a lookup table."""
    qs = []
    for i in range(n):
        qs.append(MCQQuestion(
            question=f"Pytest question {i} (correct is option {i % 4})",
            options=[f"q{i} option {j}" for j in range(4)],
            correct_index=i % 4,
            category=(categories[i % len(categories)] if categories else ""),
        ))
    return MCQTest(difficulty=difficulty, questions=qs, approved=True)


def answer_paper(client, token, correct_ratio=1.0):
    """Sit a paper through the real candidate API. Returns the submit response."""
    auth = {"Authorization": f"Bearer {security.candidate_token(token)}"}
    served = client.get(f"/api/portal/assignment/{token}/test",
                        headers=auth).json()["questions"]
    n_right = round(len(served) * correct_ratio)
    answers = []
    for i, q in enumerate(served):
        want = int(q["question"].rsplit("option ", 1)[1].rstrip(")"))
        if i >= n_right:
            want = (want + 1) % 4          # deliberately wrong
        answers.append({"question_id": q["id"], "selected_index": want})
    return client.post(f"/api/portal/assignment/{token}/submit",
                       headers=auth, json={"answers": answers})


def served_paper(client, token):
    auth = {"Authorization": f"Bearer {security.candidate_token(token)}"}
    return client.get(f"/api/portal/assignment/{token}/test",
                      headers=auth).json()["questions"]
