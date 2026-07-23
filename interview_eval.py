"""Shared, strict evaluator for AI screening-interview transcripts.

Single source of truth used by BOTH the live agent (interview_agent.py) and
the backend time-limit finalizer (portal/backend/routers/interview.py) so a
candidate is always scored the same way.

Core principle: score ONLY the competency the candidate actually demonstrated.
An interview that was answered barely, or cut short / terminated, cannot earn a
high score — there simply isn't enough evidence. A hard completeness ceiling
enforces this on top of the LLM's judgement so the model can't hand out a
middling "benefit of the doubt" number for a one-answer interview.
"""

import llm

_EVALUATOR_SYSTEM = (
    "You are a fair, experienced technical hiring evaluator scoring a SPOKEN "
    "screening interview from its transcript.\n\n"
    "CRITICAL - the candidate's answers are RAW speech-to-text. Expect false "
    "starts, filler, run-on fragments, and outright misrecognitions (nonsense "
    "phrases the candidate never actually said, such as random unrelated "
    "words). You MUST:\n"
    "- Reconstruct the candidate's INTENDED technical meaning from the messy "
    "text before judging it.\n"
    "- Judge ONLY the substance: relevance to the role, technical correctness, "
    "and the concreteness of the experience demonstrated.\n"
    "- NEVER lower the score for disfluency, brevity, rambling, filler, "
    "grammar, tangents, or transcription garbage. Those reflect the microphone "
    "and speech-to-text, NOT the candidate's ability. Do not describe the "
    "candidate as 'unclear' or 'unstructured' - that is the transcript, not "
    "them.\n\n"
    "How to score:\n"
    "- Give FULL credit for correct concepts and real, specific experience, "
    "even when stated briefly or informally.\n"
    "- Concrete real-world signals are STRONG positives: shipped or real "
    "projects, named tools and frameworks, specific debugging or design "
    "stories, contributing fixes upstream, measurable results.\n"
    "- Only penalize when the actual CONTENT is wrong, irrelevant, or a core "
    "required skill is clearly absent or the candidate says they don't know "
    "it.\n"
    "- Judge the answers that WERE given on their own merits; do not dock a "
    "completed interview for questions that simply weren't asked.\n"
    "- Bands: 0-30 = little relevant or correct content; 30-55 = partial, some "
    "relevant answers; 55-75 = good, solid and relevant, real experience; "
    "75-100 = excellent, strong specific competence with concrete evidence.\n\n"
    "The summary must describe the candidate's demonstrated competence and "
    "must NOT comment on transcript clarity, phrasing, or conciseness.\n\n"
    "Respond with ONLY JSON:\n"
    '{"score": number 0-100, "summary": str (3-4 sentences),\n'
    ' "strengths": [str, ...], "concerns": [str, ...],\n'
    ' "per_question": [{"question": str, "assessment": str}, ...]}'
)

# An interview this thin is treated as "insufficient evidence" and capped.
_INSUFFICIENT_CAP = 25.0
_MIN_WORDS = 25          # total candidate words below this = barely answered


def _candidate_stats(transcript: list[dict]) -> tuple[int, int]:
    """Return (answer_groups, total_words). An answer group is a run of one or
    more consecutive candidate turns — i.e. roughly one question's worth of
    answer — counting only turns with real content."""
    groups = 0
    in_group = False
    words = 0
    for m in transcript:
        if m.get("role") != "candidate":
            in_group = False
            continue
        n = len((m.get("text") or "").split())
        words += n
        if n >= 3:
            if not in_group:
                groups += 1
                in_group = True
        # very short filler ("yeah", "okay") does not start a new group
    return groups, words


def evaluate_transcript(job_title: str, jd_snippet: str,
                        transcript: list[dict] | None, *,
                        num_questions: int = 5,
                        terminated: bool = False) -> dict:
    """Evaluate a transcript. Never raises — returns a fallback dict instead.

    Returns {"score", "summary", "strengths", "concerns", "per_question"}.
    """
    transcript = transcript or []
    has_answer = any(m.get("role") == "candidate"
                     and (m.get("text") or "").strip() for m in transcript)
    if not has_answer:
        return {
            "score": 0.0,
            "summary": "The candidate did not provide any answers, so no "
                       "assessment could be made.",
            "strengths": [], "concerns": ["No responses were recorded."],
            "per_question": [],
        }

    groups, words = _candidate_stats(transcript)
    lines = [f"{'INTERVIEWER' if m['role'] == 'interviewer' else 'CANDIDATE'}: "
             f"{m.get('text', '')}" for m in transcript]
    # Only flag incompleteness when the interview was actually cut short. A
    # normally-completed interview is judged purely on the answers given, with
    # no penalty for the target question count (the agent decides when to wrap).
    context = ""
    if terminated:
        context = ("NOTE: this interview was TERMINATED early before all "
                   "questions were covered, so the evidence is limited; score "
                   "conservatively on the little that was said.\n\n")
    user = (
        f"JOB TITLE: {job_title}\n"
        f"JOB DESCRIPTION:\n{jd_snippet}\n\n"
        + context
        + "TRANSCRIPT:\n" + "\n".join(lines)
    )

    try:
        data = llm.chat_json(_EVALUATOR_SYSTEM, user, max_tokens=1200)
        score = max(0.0, min(100.0, float(data.get("score", 0))))
        summary = str(data.get("summary", ""))
        strengths = [str(s) for s in data.get("strengths", [])][:6]
        concerns = [str(s) for s in data.get("concerns", [])][:6]
        per_question = data.get("per_question", [])[:12]
    except Exception as e:  # noqa: BLE001 - evaluation must never lose the interview
        return {"score": 0.0,
                "summary": f"Automatic evaluation failed: {e}",
                "strengths": [], "concerns": [], "per_question": []}

    # Hard completeness/integrity ceiling. Competency that was never
    # demonstrated cannot be scored — a one-answer or terminated interview is
    # capped so the model can't hand out a middling number.
    insufficient = terminated or groups <= 1 or words < _MIN_WORDS
    if insufficient:
        score = min(score, _INSUFFICIENT_CAP)
        reason = ("The interview was terminated for proctoring violations "
                  "before completion" if terminated
                  else "The candidate answered too little to assess")
        summary = f"[{reason}; scored on limited evidence.] {summary}".strip()
        concerns = (["Insufficient interview evidence to fully assess."]
                    + [c for c in concerns
                       if "insufficient" not in c.lower()])[:6]

    return {"score": round(score, 1), "summary": summary[:1200],
            "strengths": strengths, "concerns": concerns,
            "per_question": per_question}
