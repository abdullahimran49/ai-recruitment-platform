"""Per-job question bank: the durable library HR picks tests from.

Shared by the Streamlit recruiter app (via db_bridge) and the portal admin
API, so the two portals can never drift on what a bank is or how it is drawn
from.

Relationship to tests: bank items are COPIED into Question rows when a test is
built, never referenced. Editing or retiring a bank item must not rewrite a
paper a candidate has already sat.
"""

from sqlalchemy import func, select

from core.models import (
    QuestionBankCategory,
    QuestionBankItem,
)
from schemas import MCQQuestion


# ---- Categories --------------------------------------------------------------

def list_categories(s, job_uuid: str) -> list[QuestionBankCategory]:
    return list(s.execute(
        select(QuestionBankCategory)
        .where(QuestionBankCategory.job_uuid == job_uuid)
        .order_by(QuestionBankCategory.sort_order,
                  QuestionBankCategory.id)).scalars().all())


def add_category(s, job_uuid: str, name: str) -> QuestionBankCategory:
    """Add a category, or return the existing one with that name.

    Names are matched case-insensitively: "Data" and "data" being two buckets
    would silently split a blueprint quota in half.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Category name is required.")
    existing = next((c for c in list_categories(s, job_uuid)
                     if c.name.strip().lower() == name.lower()), None)
    if existing:
        return existing
    n = s.execute(select(func.count(QuestionBankCategory.id)).where(
        QuestionBankCategory.job_uuid == job_uuid)).scalar() or 0
    c = QuestionBankCategory(job_uuid=job_uuid, name=name, sort_order=n)
    s.add(c)
    s.flush()
    return c


def rename_category(s, cat_id: int, name: str) -> QuestionBankCategory:
    c = s.get(QuestionBankCategory, cat_id)
    if not c:
        raise ValueError("No such category.")
    name = (name or "").strip()
    if not name:
        raise ValueError("Category name is required.")
    c.name = name
    return c


def delete_category(s, cat_id: int) -> int:
    """Delete a category; its questions survive as uncategorised.

    Deleting the questions with it would quietly destroy hand-written work
    because someone tidied a label. Returns how many were orphaned.
    """
    c = s.get(QuestionBankCategory, cat_id)
    if not c:
        return 0
    items = list(s.execute(select(QuestionBankItem).where(
        QuestionBankItem.category_id == cat_id)).scalars().all())
    for it in items:
        it.category_id = None
    s.delete(c)
    return len(items)


# ---- Items -------------------------------------------------------------------

def list_items(s, job_uuid: str, category_id: int | None = None,
               active_only: bool = True) -> list[QuestionBankItem]:
    q = select(QuestionBankItem).where(QuestionBankItem.job_uuid == job_uuid)
    if category_id is not None:
        q = q.where(QuestionBankItem.category_id == category_id)
    if active_only:
        # == True, not .is_(True): SQLAlchemy renders is_() as "active IS 1",
        # which is invalid T-SQL on a SQL Server BIT column.
        q = q.where(QuestionBankItem.active == True)  # noqa: E712
    return list(s.execute(q.order_by(QuestionBankItem.id)).scalars().all())


def add_item(s, job_uuid: str, question: str, options: list[str],
             correct_index: int, explanation: str = "",
             difficulty: str = "medium", category_id: int | None = None,
             source: str = "custom") -> QuestionBankItem:
    options = [str(o).strip() for o in (options or [])]
    if len(options) != 4 or not all(options):
        raise ValueError("Exactly four non-empty options are required.")
    if not (question or "").strip():
        raise ValueError("Question text is required.")
    if not 0 <= int(correct_index) <= 3:
        raise ValueError("correct_index must be 0-3.")
    it = QuestionBankItem(
        job_uuid=job_uuid, category_id=category_id,
        question=question.strip(), options_json=options,
        correct_index=int(correct_index), explanation=(explanation or "").strip(),
        difficulty=difficulty or "medium", source=source, active=True)
    s.add(it)
    s.flush()
    return it


def add_items(s, job_uuid: str, questions, category_id: int | None = None,
              difficulty: str = "medium", source: str = "llm") -> list:
    """Bulk-add, skipping anything already in the bank verbatim.

    De-duplication is on question text: regenerating repeatedly against the
    same JD otherwise fills the bank with near-identical rows.
    """
    have = {i.question.strip().lower()
            for i in list_items(s, job_uuid, active_only=False)}
    out = []
    for q in questions:
        if q.question.strip().lower() in have:
            continue
        have.add(q.question.strip().lower())
        out.append(add_item(
            s, job_uuid, q.question, list(q.options), q.correct_index,
            getattr(q, "explanation", ""), difficulty,
            category_id if category_id is not None else None, source))
    return out


def update_item(s, item_id: int, **fields) -> QuestionBankItem:
    it = s.get(QuestionBankItem, item_id)
    if not it:
        raise ValueError("No such question.")
    if "options" in fields and fields["options"] is not None:
        opts = [str(o).strip() for o in fields.pop("options")]
        if len(opts) != 4 or not all(opts):
            raise ValueError("Exactly four non-empty options are required.")
        it.options_json = opts
    for key in ("question", "explanation", "difficulty", "category_id",
                "correct_index", "active"):
        if fields.get(key) is not None:
            setattr(it, key, fields[key])
    return it


def delete_item(s, item_id: int) -> bool:
    it = s.get(QuestionBankItem, item_id)
    if not it:
        return False
    s.delete(it)
    return True


# ---- Bank -> test ------------------------------------------------------------

def to_mcq(item: QuestionBankItem) -> MCQQuestion:
    """Copy a bank item into the pipeline's question shape.

    `category` carries the NAME (not the id) because tests.blueprint_json and
    questions.category are keyed by name — a test must stay readable after its
    source category is renamed or deleted.
    """
    return MCQQuestion(
        question=item.question,
        options=list(item.options_json or []),
        correct_index=item.correct_index,
        explanation=item.explanation or "",
        category=item.category.name if item.category else "",
    )


def pick(s, job_uuid: str, item_ids: list[int]) -> list[MCQQuestion]:
    """Copy the chosen bank items into a question pool, in the given order."""
    by_id = {i.id: i for i in list_items(s, job_uuid, active_only=False)}
    out = []
    for iid in item_ids:
        it = by_id.get(int(iid))
        if it:
            it.times_used = (it.times_used or 0) + 1
            out.append(to_mcq(it))
    return out


def counts_by_category(s, job_uuid: str) -> dict[str, int]:
    """{category_name: how many active questions}, plus "" for uncategorised.

    Drives the blueprint UI, which must not offer a quota the bank cannot fill.
    """
    out: dict[str, int] = {}
    for it in list_items(s, job_uuid, active_only=True):
        name = it.category.name if it.category else ""
        out[name] = out.get(name, 0) + 1
    return out
