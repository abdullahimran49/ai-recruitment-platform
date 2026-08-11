"""LLM-generated MCQ assessments based strictly on the job description.

Questions are generated in batches (the output-token budget caps how many
fit in one call) and validated into MCQQuestion models. The user reviews and
edits them in the UI before anything is sent to candidates.
"""

import logging
import random

import llm
from schemas import MCQQuestion, MCQTest

_log = logging.getLogger(__name__)

# Small batches with a generous per-call output budget: hard questions carry
# code snippets and can run 250-350 tokens each, so 6-at-1500-tokens truncated
# the last ones mid-text. 3 questions at 3500 tokens leaves ample headroom.
_BATCH_SIZE = 10
_MCQ_MAX_TOKENS = 8000

_MCQ_SYSTEM = """You are a senior technical interviewer writing a screening \
quiz. Base every question ONLY on skills, tools, and responsibilities named \
in the job description.

VARIETY RULES (strict):
- Each question in a batch must target a DIFFERENT skill/tool/responsibility \
from the job description. Do not write two questions about the same topic.
- Rotate question styles: predict the output of a short code snippet, spot \
the bug, pick the best approach for a realistic work scenario, "what happens \
if...", behavior of a specific tool/API/config.
- BANNED phrasings: "What is the primary purpose of...", "Which of the \
following best describes...", "What is X used for", and any \
definition-recall template. Never start two questions with the same words.

DIFFICULTY RULES (strict):
- easy: everyday basics a beginner learns in their first month. Simple \
one-step questions.
- medium: applied practice — short code snippets, common pitfalls, choosing \
the correct usage among plausible alternatives. Requires having actually \
used the tool.
- hard: expert level. Multi-step reasoning over a code snippet, subtle bugs, \
concurrency/performance/design trade-offs, edge cases and failure modes. A \
junior developer should get hard questions WRONG. Include a short code \
snippet in the question text whenever it sharpens the question.

FORMAT:
- EXACTLY 4 options, one correct. Wrong options must reflect real \
misconceptions a practitioner might hold — no joke options.
- Do not repeat or paraphrase anything in AVOID_REPEATING.
- "explanation": one sentence for the reviewer on why the answer is correct.

Respond with ONLY a JSON object of this shape:
{"questions": [{"question": str, "options": [str, str, str, str],
 "correct_index": int, "explanation": str}]}"""

# Generation needs variety, not repeatability — override the scoring-oriented
# low temperature default.
_MCQ_TEMPERATURE = 0.9


from concurrent.futures import ThreadPoolExecutor, as_completed

def generate_mcqs(job_description: str, difficulty: str, count: int,
                  avoid: list[str] | None = None,
                  category: str = "") -> MCQTest:
    """Generate `count` questions at `difficulty` from the JD, in batches concurrently."""
    questions: list[MCQQuestion] = []
    attempts = 0
    max_attempts = (count // _BATCH_SIZE + 2) * 2
    avoid = list(avoid or [])
    
    seen = {a.strip().lower() for a in avoid}
    
    def _fetch_batch(need: int, asked: list[str]) -> list[MCQQuestion]:
        topic = (f"TOPIC (every question must be about this): {category}\n"
                 if category.strip() else "")
        user = (
            f"DIFFICULTY: {difficulty}\n"
            f"{topic}"
            f"NUMBER OF QUESTIONS: {need}\n\n"
            f"JOB DESCRIPTION:\n{job_description.strip()}\n\n"
            "AVOID_REPEATING (existing questions — cover different topics "
            "and use different phrasings):\n"
            + ("\n".join(f"- {q}" for q in asked) or "(none)")
        )
        data = llm.chat_json(_MCQ_SYSTEM, user, temperature=_MCQ_TEMPERATURE, max_tokens=_MCQ_MAX_TOKENS)
        batch_qs = []
        for row in (data.get("questions", []) if isinstance(data, dict) else []):
            q = _validate(row)
            if q:
                q.category = category.strip()
                batch_qs.append(q)
        return batch_qs

    while len(questions) < count and attempts < max_attempts:
        needed = count - len(questions)
        batch_count = (needed + _BATCH_SIZE - 1) // _BATCH_SIZE
        
        # Launch concurrently
        futures = []
        with ThreadPoolExecutor(max_workers=batch_count) as executor:
            for _ in range(batch_count):
                attempts += 1
                if attempts > max_attempts:
                    break
                # Each thread gets the currently 'asked' list so they avoid the same baseline
                asked = avoid + [q.question for q in questions]
                futures.append(executor.submit(_fetch_batch, min(_BATCH_SIZE, needed), asked))
                
            for future in as_completed(futures):
                try:
                    batch_qs = future.result()
                    for q in batch_qs:
                        q_text = q.question.strip().lower()
                        if q_text not in seen:
                            seen.add(q_text)
                            questions.append(q)
                            if len(questions) >= count:
                                break
                except Exception as e:
                    _log.warning("MCQ batch generation failed: %s", e)
                if len(questions) >= count:
                    break

    return MCQTest(difficulty=difficulty, questions=questions[:count])


# NOTE: the phrase "JSON object" is load-bearing, not decoration. Groq's
# response_format=json_object rejects any request whose messages do not contain
# the word "json" (400), so a prompt that only shows the shape silently fails
# and falls back to generic buckets.
_CATEGORY_SYSTEM = """You group screening-quiz questions for one job into \
topic buckets. Read the job description and propose the 3-5 buckets a \
recruiter would actually sort questions into for THIS role.

RULES:
- Name the real skill areas the JD names (e.g. "Data", "AI/ML", "SQL", \
"Cloud"), not generic labels like "Technical".
- Always include exactly one non-technical bucket for general aptitude / \
situational judgement, named "General".
- 1-3 words per name. No duplicates, no overlap.

Respond with ONLY a JSON object of this shape:
{"categories": [str, ...]}"""

# Returned when the JD cannot be read. Deliberately generic — if you see these
# exact three for a detailed JD, the LLM call failed rather than "decided".
FALLBACK_CATEGORIES = ["Technical", "Domain", "General"]


def suggest_categories(job_description: str) -> list[str]:
    """Propose question-bank categories from the JD. HR edits these after.

    Falls back to FALLBACK_CATEGORIES rather than raising: a failed call must
    not leave someone staring at an empty bank with no way forward. The failure
    is logged — swallowing it silently once hid a prompt bug that made this
    function never work at all.
    """
    try:
        data = llm.chat_json(
            _CATEGORY_SYSTEM,
            f"JOB DESCRIPTION:\n{job_description.strip()}",
            temperature=0.4, max_tokens=300)
        names, seen = [], set()
        for c in (data.get("categories", []) if isinstance(data, dict) else []):
            name = str(c).strip()[:120]
            if name and name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
        if names:
            # Trim BEFORE appending General, not after: truncating a list we
            # just appended to would drop the very bucket we guarantee.
            if any(n.lower() == "general" for n in names):
                return names[:6]
            return names[:5] + ["General"]
        _log.warning("Category suggestion returned no usable names: %r", data)
    except Exception as e:  # noqa: BLE001 - a convenience, not a gate
        _log.warning("Category suggestion failed, using generic buckets: %s", e)
    return list(FALLBACK_CATEGORIES)


def _validate(row) -> MCQQuestion | None:
    try:
        opts = [str(o).strip() for o in row.get("options", [])][:4]
        question = str(row.get("question", "")).strip()
        # Reject empty pieces and any option that looks truncated (a batch cut
        # off mid-object leaves a short/blank trailing option).
        if len(opts) != 4 or not question or any(not o for o in opts):
            return None
        ci = int(row.get("correct_index", 0))
        if not 0 <= ci <= 3:
            ci = 0
        return MCQQuestion(
            question=str(row["question"]).strip(),
            options=opts,
            correct_index=ci,
            explanation=str(row.get("explanation", "")).strip(),
        )
    except (TypeError, ValueError, KeyError):
        return None


def merge_and_shuffle(
    custom_questions: list[MCQQuestion],
    llm_questions: list[MCQQuestion],
    total_count: int,
    custom_ratio: float,
) -> list[MCQQuestion]:
    """Pick questions from custom and LLM pools based on ratio, then shuffle.

    `custom_ratio` is 0.0 (all LLM) to 1.0 (all custom).  When a pool has
    fewer questions than required, the shortfall is filled from the other pool.

    Returns a list of at most `total_count` questions in random order.
    """
    n_custom = min(len(custom_questions), round(total_count * custom_ratio))
    n_llm = total_count - n_custom

    # If the LLM pool is short, take more from custom and vice versa.
    if n_llm > len(llm_questions):
        n_llm = len(llm_questions)
        n_custom = min(len(custom_questions), total_count - n_llm)
    if n_custom > len(custom_questions):
        n_custom = len(custom_questions)
        n_llm = min(len(llm_questions), total_count - n_custom)

    picked_custom = random.sample(custom_questions, n_custom) if n_custom else []
    picked_llm = random.sample(llm_questions, n_llm) if n_llm else []

    merged = picked_custom + picked_llm
    random.shuffle(merged)
    return merged


def test_to_email_block(test: MCQTest) -> str:
    """Render the approved test as plain text for the assessment email
    (correct answers and explanations are NOT included)."""
    lines = []
    letters = "ABCD"
    for i, q in enumerate(test.questions, 1):
        lines.append(f"Q{i}. {q.question}")
        for j, opt in enumerate(q.options):
            lines.append(f"   {letters[j]}) {opt}")
        lines.append("")
    return "\n".join(lines)

