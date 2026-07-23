"""The per-job question bank."""

import pytest
from sqlalchemy import select

import db_bridge
import mcq as mcq_mod
from conftest import make_pool
from core import bank
from core.db import session
from core.models import QuestionBankItem
from schemas import MCQQuestion, MCQTest


def _add(job, n=3, cat=None, prefix="Q"):
    with session() as s:
        cid = bank.add_category(s, job, cat).id if cat else None
        for i in range(n):
            bank.add_item(s, job, f"{prefix}{i} for {cat or 'none'}?",
                          [f"opt{j}" for j in range(4)], i % 4, "why",
                          "medium", cid)
        return cid


def test_categories_dedupe_case_insensitively(client, admin_headers, make_job):
    """'Data' and 'data' as two buckets would split a blueprint quota in half."""
    job = make_job()
    with session() as s:
        a = bank.add_category(s, job, "Data")
        b = bank.add_category(s, job, "data")
        c = bank.add_category(s, job, "  DATA  ")
    assert a.id == b.id == c.id


def test_deleting_a_category_keeps_its_questions(client, admin_headers,
                                                 make_job):
    """Tidying a label must not destroy hand-written work."""
    job = make_job()
    cid = _add(job, 3, "Data")
    r = client.delete(f"/api/admin/bank/categories/{cid}", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["questions_kept"] == 3

    b = client.get(f"/api/admin/jobs/{job}/bank", headers=admin_headers).json()
    assert len(b["items"]) == 3
    assert all(i["category_id"] is None for i in b["items"])


def test_bulk_add_skips_duplicates(make_job):
    """Topping the bank up repeatedly must not fill it with the same question."""
    job = make_job()
    qs = [MCQQuestion(question="Same question?",
                      options=["a", "b", "c", "d"], correct_index=0)]
    with session() as s:
        assert len(bank.add_items(s, job, qs)) == 1
    with session() as s:
        assert len(bank.add_items(s, job, qs)) == 0


def test_add_item_rejects_malformed_questions(make_job):
    job = make_job()
    with session() as s:
        with pytest.raises(ValueError):
            bank.add_item(s, job, "q?", ["a", "b", "c"], 0)          # 3 options
        with pytest.raises(ValueError):
            bank.add_item(s, job, "q?", ["a", "b", "c", ""], 0)      # blank
        with pytest.raises(ValueError):
            bank.add_item(s, job, "", ["a", "b", "c", "d"], 0)       # no text
        with pytest.raises(ValueError):
            bank.add_item(s, job, "q?", ["a", "b", "c", "d"], 9)     # bad index


def test_picking_copies_rather_than_references(client, admin_headers, make_job):
    """Editing or deleting a bank item must never rewrite a paper already sat."""
    job = make_job()
    _add(job, 2, "Data")
    with session() as s:
        ids = [i.id for i in bank.list_items(s, job)]
    picked = db_bridge.bank_pick(job, ids)
    assert len(picked) == 2

    # Nuke the bank; the picked copies must be unaffected.
    for i in ids:
        db_bridge.bank_delete_item(i)
    assert all(p.question for p in picked)
    b = client.get(f"/api/admin/jobs/{job}/bank", headers=admin_headers).json()
    assert b["items"] == []


def test_picked_questions_carry_the_category_name(make_job):
    """Names, not ids: a test must stay readable after its source category is
    renamed or deleted."""
    job = make_job()
    _add(job, 2, "Data")
    with session() as s:
        ids = [i.id for i in bank.list_items(s, job)]
    assert all(p.category == "Data" for p in db_bridge.bank_pick(job, ids))


def test_picking_increments_usage(make_job):
    job = make_job()
    _add(job, 1, "Data")
    with session() as s:
        iid = bank.list_items(s, job)[0].id
    db_bridge.bank_pick(job, [iid])
    db_bridge.bank_pick(job, [iid])
    with session() as s:
        assert s.get(QuestionBankItem, iid).times_used == 2


def test_retired_questions_are_not_offered_for_new_tests(make_job):
    job = make_job()
    _add(job, 3, "Data")
    with session() as s:
        items = bank.list_items(s, job)
        bank.update_item(s, items[0].id, active=False)
    with session() as s:
        assert len(bank.list_items(s, job, active_only=True)) == 2
        assert len(bank.list_items(s, job, active_only=False)) == 3


def test_editing_an_item_round_trips(client, admin_headers, make_job):
    job = make_job()
    _add(job, 1, "Data")
    with session() as s:
        iid = bank.list_items(s, job)[0].id
    r = client.patch(f"/api/admin/bank/items/{iid}", headers=admin_headers,
                     json={"question": "Corrected?", "correct_index": 2,
                           "options": ["w", "x", "y", "z"]})
    assert r.status_code == 200, r.text
    with session() as s:
        it = s.get(QuestionBankItem, iid)
        assert it.question == "Corrected?"
        assert it.correct_index == 2
        assert it.options_json == ["w", "x", "y", "z"]


def test_edit_rejects_a_malformed_option_set(client, admin_headers, make_job):
    job = make_job()
    _add(job, 1, "Data")
    with session() as s:
        iid = bank.list_items(s, job)[0].id
    r = client.patch(f"/api/admin/bank/items/{iid}", headers=admin_headers,
                     json={"options": ["only", "three", "here"]})
    assert r.status_code == 422


def test_counts_by_category_drives_the_blueprint_ui(make_job):
    job = make_job()
    _add(job, 3, "Data", prefix="D")
    _add(job, 2, "AI", prefix="A")
    with session() as s:
        assert bank.counts_by_category(s, job) == {"Data": 3, "AI": 2}


def test_bank_to_paper_honours_the_blueprint(client, make_job, make_candidate):
    """End to end: bank -> pool -> per-candidate paper with a category mix."""
    job = make_job()
    _add(job, 4, "Data", prefix="D")
    _add(job, 4, "AI", prefix="A")
    with session() as s:
        ids = [i.id for i in bank.list_items(s, job)]
    picked = db_bridge.bank_pick(job, ids)

    cands = [make_candidate(job, key=f"bp{i}") for i in range(3)]
    entries, _ = db_bridge.create_test_with_assignments(
        job, MCQTest(difficulty="medium", questions=picked, approved=True),
        cands, duration_minutes=30, pass_score=60, proctored=False,
        questions_per_candidate=3, blueprint={"Data": 1, "AI": 1})

    from core.models import AssignmentQuestion, Question
    with session() as s:
        for e in entries:
            cats = [c for c in s.execute(
                select(Question.category).join(
                    AssignmentQuestion,
                    AssignmentQuestion.question_id == Question.id)
                .where(AssignmentQuestion.assignment_uuid == e["token"])
            ).scalars().all()]
            assert cats.count("Data") >= 1, cats
            assert cats.count("AI") >= 1, cats
            assert len(cats) == 3


# ---- generation (needs a live model) -----------------------------------------

@pytest.mark.llm
def test_suggest_categories_reads_the_jd_not_the_fallback():
    """Regression: Groq's JSON mode 400s unless the prompt contains the word
    'json'. The category prompt did not, so this failed 100% of the time and
    the except-swallow returned generic buckets. It LOOKED like it worked."""
    got = mcq_mod.suggest_categories(
        "Senior Data & AI Engineer. Build ETL pipelines in Python and SQL, "
        "train and deploy ML models.")
    assert got != mcq_mod.FALLBACK_CATEGORIES, (
        "got the generic fallback verbatim — the LLM call is failing and "
        "being swallowed")
    joined = " ".join(got).lower()
    assert any(k in joined for k in ("data", "ml", "ai", "sql", "python"))


@pytest.mark.llm
def test_suggest_categories_always_includes_general():
    """The non-technical bucket is a guarantee. It was silently dropped when
    the model returned 6 names: General was appended, then truncated away."""
    got = mcq_mod.suggest_categories(
        "Data & AI Engineer: Python, SQL, ETL, ML, cloud, Kubernetes, "
        "Spark, Airflow, dbt, Terraform, and stakeholder communication.")
    assert any(n.lower() == "general" for n in got), got


@pytest.mark.llm
def test_every_chat_json_prompt_contains_the_word_json():
    """A prompt without it 400s on Groq. Cheap guard, no tokens spent."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    bad = []
    for f in root.glob("*.py"):
        src = f.read_text(encoding="utf-8", errors="replace")
        if "chat_json" not in src:
            continue
        for m in re.finditer(r'^(_?[A-Z][A-Z0-9_]*(?:SYSTEM|PROMPT))\s*=\s*'
                             r'(?:"""|f""")', src, re.M):
            body = src[m.end():src.find('"""', m.end())]
            if "json" not in body.lower():
                bad.append(f"{f.name}:{m.group(1)}")
    assert not bad, f"prompts missing the word 'json' (will 400 on Groq): {bad}"


@pytest.mark.llm
def test_regenerate_keeps_the_kept_and_replaces_the_rest():
    jd = "Data Engineer: Python, SQL, ETL pipelines."
    kept = ["What does GROUP BY do in SQL?", "What is an ETL pipeline?"]
    gen = mcq_mod.generate_mcqs(jd, "medium", 3, avoid=kept)
    assert gen.questions
    got = {q.question.strip().lower() for q in gen.questions}
    assert not (got & {k.strip().lower() for k in kept}), (
        "regeneration reproduced a question the user chose to keep")
