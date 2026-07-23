"""LiveKit voice-agent worker for AI screening interviews.

Run with:
    python interview_agent.py dev          # local development
    python interview_agent.py start        # production

The agent connects to a LiveKit room whose name matches an ai_interviews.uuid.
Room metadata (JSON) carries the interview context: JD snippet, focus areas,
candidate name, etc.  The agent conducts the interview as a warm, professional
AI interviewer, then saves the transcript, runs the LLM evaluation, and emails
the candidate.

Ownership rules (mirrored in portal/backend/routers/interview.py):
  - The AGENT finalizes every interview that ends normally or by the
    candidate leaving: transcript + evaluation + status -> completed + email.
  - The BACKEND owns proctoring termination: it flips status to 'terminated'
    and emails BEFORE deleting the room, so this worker sees the terminated
    status, keeps it, and skips its own email.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Path setup so we can import from the project root (core.*, llm, etc.)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    RoomInputOptions,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import openai as lkopenai  # noqa: E402
from livekit.plugins import silero  # noqa: E402

from core import mailer                                    # noqa: E402
from core.db import session as db_session                  # noqa: E402
from core.models import AIInterview, Candidate             # noqa: E402
from interview_eval import evaluate_transcript             # noqa: E402

logger = logging.getLogger("interview-agent")
logger.setLevel(logging.INFO)

# Seconds the candidate may be disconnected (refresh, network blip) before
# the interview is finalized with what was said so far.
REJOIN_GRACE_SECONDS = 60


# ═══════════════════════════════════════════════════════════════════════════
#  InterviewAgent
# ═══════════════════════════════════════════════════════════════════════════

class InterviewAgent(Agent):
    """Voice-based AI interviewer."""

    def __init__(
        self,
        *,
        interview_uuid: str,
        candidate_name: str,
        job_title: str,
        jd_snippet: str,
        focus: str,
        num_questions: int,
        cv_brief: str = "",
    ) -> None:
        # The CV section is omitted entirely when the brief is empty rather
        # than sent as "(not provided)": an empty heading invites the model to
        # invent a background for the candidate.
        cv_section = (
            f"## This candidate's CV (what THEY claim — verify it)\n"
            f"{cv_brief}\n\n" if cv_brief.strip() else "")
        cv_rule = (
            f"- GROUND YOUR QUESTIONS IN THEIR CV: most of your questions "
            f"must come from the CV section above, not generic role "
            f"questions. Ask them to walk you through a specific project, "
            f"tool or claim they listed, and probe whether they really did "
            f"it: what their own contribution was, what went wrong, what "
            f"they would do differently. If the screening flagged a gap, "
            f"probe it politely once.\n"
            f"- NEVER state anything from their CV as fact you have "
            f"verified, and never read it back at them as a list. It is a "
            f"prompt for your questions, not a script.\n"
            if cv_brief.strip() else "")

        instructions = (
            f"You are Nova, a warm, professional AI interviewer conducting a "
            f"spoken screening interview for the {job_title or 'open'} "
            f"position. You are talking with {candidate_name}.\n\n"
            f"## Job description (excerpt)\n{jd_snippet or '(not provided)'}\n\n"
            f"{cv_section}"
            f"## Focus areas\n{focus or 'General fit for the role'}\n\n"
            f"## Rules\n"
            f"{cv_rule}"
            f"- This is a live voice conversation: keep every reply SHORT "
            f"(1-3 conversational sentences). No lists, no markdown, no "
            f"emoji, no stage directions.\n"
            f"- Greet {candidate_name} by first name, introduce yourself and "
            f"the purpose of the call in one breath, then ask the first "
            f"question.\n"
            f"- Ask ONE question at a time and wait for the answer. Mix "
            f"experience, technical understanding, and situational questions "
            f"grounded in the job description and focus areas.\n"
            f"- Ask {num_questions} main questions in total; track the count "
            f"internally and never mention it to the candidate.\n"
            f"- Speech-to-text is imperfect: interpret answers charitably and "
            f"never mock or correct transcription errors.\n"
            f"- If the candidate asks for the answers or drifts off track, "
            f"politely steer back. Do NOT reveal scores or hiring decisions.\n"
            f"- A recruiter may join and speak; if they address you, answer "
            f"briefly and return to the interview.\n"
            f"- After the final question (or when told time is up), thank "
            f"the candidate warmly, say the team will be in touch, and then "
            f"call the `end_interview` tool.\n"
            f"- If the candidate asks to stop or is unresponsive for a long "
            f"time, say a brief goodbye and call `end_interview`.\n\n"
            f"## Conversation quality — FOLLOW CAREFULLY\n"
            f"- PROBE FOR DEPTH: After each answer, assess whether it has "
            f"specific evidence. If the candidate's response is vague, lacks "
            f"metrics, measurable outcomes, numbers, timelines, or concrete "
            f"examples, ask a targeted follow-up requesting those specifics. "
            f"For example: 'That's interesting — could you share a rough "
            f"number on how much performance improved?' or 'Can you walk me "
            f"through a specific example of when you did that?'\n"
            f"- PICK UP ON RELEVANT DETAILS: When the candidate mentions a "
            f"technology, project, methodology, challenge, or achievement "
            f"that is relevant to the job description or focus areas, pick "
            f"up on it and ask a focused follow-up about that specific "
            f"thing. For example: 'You mentioned migrating to Kubernetes — "
            f"what was the most challenging part of that migration?'\n"
            f"- CONNECT QUESTIONS: Reference what the candidate told you "
            f"earlier when transitioning to a new topic. Build a natural "
            f"conversational thread, not a disconnected checklist. For "
            f"example: 'Earlier you mentioned working with distributed "
            f"systems at Acme — how did you handle monitoring and "
            f"observability there?'\n"
            f"- You may ask up to TWO follow-ups per main question when the "
            f"answer lacks depth or reveals something worth exploring. Track "
            f"main questions separately from follow-ups in your internal "
            f"count.\n"
            f"- React briefly and naturally to what the candidate just said "
            f"before asking your follow-up or next question. Acknowledge "
            f"their answer in a sentence before moving on.\n"
        )
        super().__init__(instructions=instructions)
        self.interview_uuid = interview_uuid
        self.candidate_name = candidate_name
        self.job_title = job_title
        self.jd_snippet = jd_snippet
        self.num_questions = num_questions
        self._interview_ended = False
        self.end_event: asyncio.Event = asyncio.Event()

    @function_tool
    async def end_interview(
        self,
        context: RunContext,
        reason: str = "all_questions_asked",
    ) -> str:
        """End the interview session. Call ONLY after you have already spoken
        your closing message. Reasons: 'all_questions_asked', 'time_up',
        'candidate_request', 'unresponsive'."""
        if self._interview_ended:
            return "Interview already ended."
        self._interview_ended = True
        logger.info("end_interview(%s) for %s", reason, self.interview_uuid)
        self.end_event.set()
        return "Interview ended."


# ═══════════════════════════════════════════════════════════════════════════
#  Post-interview pipeline
# ═══════════════════════════════════════════════════════════════════════════

def _extract_transcript(session: AgentSession) -> list[dict]:
    """AgentSession.history -> the DB transcript format
    [{"role": "interviewer"|"candidate", "text", "at"}]."""
    transcript: list[dict] = []
    for msg in session.history.messages():
        if msg.role not in ("assistant", "user"):
            continue  # skip system/instructions items
        text = (msg.text_content or "").strip()
        if not text:
            continue
        at = datetime.utcnow().isoformat()
        created = getattr(msg, "created_at", None)
        if isinstance(created, (int, float)) and created > 0:
            at = datetime.utcfromtimestamp(created).isoformat()
        transcript.append({
            "role": "interviewer" if msg.role == "assistant" else "candidate",
            "text": text,
            "at": at,
        })
    return transcript


def _count_questions(transcript: list[dict]) -> int:
    """Heuristic for the admin dashboard: interviewer turns ending in '?'."""
    return sum(1 for e in transcript
               if e["role"] == "interviewer"
               and e["text"].rstrip().endswith("?"))


def _current_status(interview_uuid: str) -> str | None:
    """Read the interview's status from the DB (the backend flips it to
    'terminated' on proctoring termination before the room closes)."""
    try:
        with db_session() as db:
            iv = db.get(AIInterview, interview_uuid)
            return iv.status if iv else None
    except Exception:  # noqa: BLE001
        return None


def _save_results(interview_uuid: str, transcript: list[dict],
                  evaluation: dict | None) -> tuple[str | None, str | None]:
    """Persist transcript + evaluation. Returns (candidate_email, final_status);
    email is only returned when THIS save flipped the status to completed
    (the backend already emailed for terminations)."""
    try:
        with db_session() as db:
            iv = db.get(AIInterview, interview_uuid)
            if not iv:
                logger.error("Interview %s not found in DB", interview_uuid)
                return None, None

            iv.transcript = transcript
            iv.questions_asked = _count_questions(transcript)
            if evaluation is not None:
                iv.ai_score = evaluation.get("score")
                iv.ai_summary = evaluation
            iv.last_seen = datetime.utcnow()

            completed_now = iv.status == "started"
            if completed_now:
                iv.status = "completed"
                iv.completed_at = datetime.utcnow()
                cand = db.get(Candidate, iv.candidate_uuid)
                email = None
                if cand:
                    cand.status = "interviewed"
                    email = cand.email
                return email, iv.status
            return None, iv.status
    except Exception:  # noqa: BLE001
        logger.exception("DB save failed for %s", interview_uuid)
        return None, None


async def finalize_interview(agent: InterviewAgent,
                             session: AgentSession,
                             job_title: str) -> None:
    """Transcript -> strict evaluation -> DB -> completion email."""
    logger.info("Finalizing interview %s", agent.interview_uuid)
    transcript = _extract_transcript(session)
    # Terminated interviews (proctoring) are scored on limited evidence — the
    # shared evaluator caps them; pass the current DB status so it knows.
    status_now = await asyncio.to_thread(_current_status, agent.interview_uuid)
    evaluation = await asyncio.to_thread(
        evaluate_transcript, job_title, agent.jd_snippet, transcript,
        num_questions=agent.num_questions,
        terminated=(status_now == "terminated"))
    email, status = await asyncio.to_thread(
        _save_results, agent.interview_uuid, transcript, evaluation)
    logger.info("Interview %s saved: %d transcript turns, status=%s",
                agent.interview_uuid, len(transcript), status)
    if email:
        ok, msg = await asyncio.to_thread(
            mailer.send_email, email,
            f"Interview completed — {job_title}",
            f"Hi {agent.candidate_name or 'there'},\n\nThank you for "
            f"completing your interview for the {job_title} role. "
            "The recruiting team will review it and be in touch about "
            "next steps.\n")
        logger.info("Completion email to %s: %s", email, msg if not ok else "sent")


# ═══════════════════════════════════════════════════════════════════════════
#  DB helpers
# ═══════════════════════════════════════════════════════════════════════════

def _mark_started(interview_uuid: str) -> None:
    """Safety net if the candidate reached the room without /join committing."""
    try:
        with db_session() as db:
            iv = db.get(AIInterview, interview_uuid)
            if iv and iv.status == "scheduled":
                iv.status = "started"
                iv.started_at = datetime.utcnow()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to mark %s started", interview_uuid)


# ═══════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    raw_meta = ctx.room.metadata or "{}"
    try:
        meta = json.loads(raw_meta)
    except json.JSONDecodeError:
        logger.error("Invalid room metadata: %s", raw_meta)
        meta = {}

    interview_uuid: str = meta.get("interview_uuid", ctx.room.name)
    candidate_name: str = meta.get("candidate_name", "Candidate")
    candidate_identity: str = meta.get(
        "candidate_identity", f"candidate_{ctx.room.name[:8]}")
    job_title: str = meta.get("job_title", "")
    jd_snippet: str = meta.get("jd_snippet", "")
    candidate_cv: str = meta.get("cv_brief", "")
    focus: str = meta.get("focus", "")
    num_questions: int = int(meta.get("num_questions", 5))
    duration_minutes: int = int(meta.get("duration_minutes", 20))
    remaining_seconds: int = int(
        meta.get("remaining_seconds", duration_minutes * 60))

    # Wait for the CANDIDATE specifically — a recruiter may be watching
    # from before the candidate arrives.
    participant = await ctx.wait_for_participant(identity=candidate_identity)
    logger.info("Candidate %s joined room %s", participant.identity,
                ctx.room.name)

    await asyncio.to_thread(_mark_started, interview_uuid)

    agent = InterviewAgent(
        interview_uuid=interview_uuid,
        candidate_name=candidate_name,
        job_title=job_title,
        jd_snippet=jd_snippet,
        cv_brief=candidate_cv,
        focus=focus,
        num_questions=num_questions,
    )

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=lkopenai.STT(model="whisper-1", language="en"),
        llm=lkopenai.LLM(model="gpt-4o-mini", temperature=0.6),
        tts=lkopenai.TTS(model="tts-1", voice="nova"),
        # Natural conversation: the candidate may interrupt; give them a
        # moment of silence before we treat a pause as end-of-answer.
        min_endpointing_delay=0.8,
    )

    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=RoomInputOptions(
            participant_identity=candidate_identity,
            audio_enabled=True,
            video_enabled=False,
            close_on_disconnect=False,  # we manage the rejoin grace ourselves
        ),
    )

    # Finalize exactly once, no matter how the job ends. The server closes
    # the room (departure_timeout) if the candidate stays gone, which shuts
    # this job down — the framework awaits shutdown callbacks, so the
    # transcript/evaluation still gets saved on that path.
    finalized = False

    async def _finalize_once() -> None:
        nonlocal finalized
        if finalized:
            return
        finalized = True
        await finalize_interview(agent, session, job_title)

    async def _on_shutdown(reason: str = "") -> None:
        await _finalize_once()

    ctx.add_shutdown_callback(_on_shutdown)

    # The agent speaks first.
    await session.generate_reply(
        instructions=(
            f"Greet {candidate_name} by first name, introduce yourself as "
            f"Nova the AI interviewer for the {job_title or 'role'} "
            f"screening, mention the interview takes about "
            f"{duration_minutes} minutes, and ask your first question."
        ),
    )

    # ---- watchdogs --------------------------------------------------------

    async def duration_timer() -> None:
        warn_at = max(0, remaining_seconds - 120)
        try:
            await asyncio.sleep(warn_at)
            if not agent.end_event.is_set():
                logger.info("Nudging wrap-up for %s", interview_uuid)
                session.generate_reply(
                    instructions=(
                        "Time is up. Do not start a new question: briefly "
                        "thank the candidate, say the team will be in touch, "
                        "and call the `end_interview` tool with reason "
                        "'time_up'."
                    ),
                )
            await asyncio.sleep(max(0, remaining_seconds - warn_at))
            if not agent.end_event.is_set():
                logger.info("Hard time limit for %s", interview_uuid)
                agent.end_event.set()
        except asyncio.CancelledError:
            pass

    grace_task: asyncio.Task | None = None

    async def _grace_then_end() -> None:
        try:
            await asyncio.sleep(REJOIN_GRACE_SECONDS)
            logger.info("Candidate did not rejoin %s — ending", interview_uuid)
            agent.end_event.set()
        except asyncio.CancelledError:
            pass

    def on_participant_disconnected(p) -> None:
        nonlocal grace_task
        if p.identity == candidate_identity and not agent.end_event.is_set():
            logger.info("Candidate left %s — %ds rejoin grace",
                        interview_uuid, REJOIN_GRACE_SECONDS)
            grace_task = asyncio.create_task(_grace_then_end())

    def on_participant_connected(p) -> None:
        nonlocal grace_task
        if p.identity == candidate_identity and grace_task:
            logger.info("Candidate rejoined %s", interview_uuid)
            grace_task.cancel()
            grace_task = None

    ctx.room.on("participant_disconnected", on_participant_disconnected)
    ctx.room.on("participant_connected", on_participant_connected)

    timer_task = asyncio.create_task(duration_timer())

    # ---- wait for the end, let the goodbye finish, finalize ----------------

    try:
        await agent.end_event.wait()
    finally:
        timer_task.cancel()
        if grace_task:
            grace_task.cancel()

    speech = session.current_speech
    if speech:
        try:
            await speech.wait_for_playout()
        except Exception:  # noqa: BLE001
            pass
    await asyncio.sleep(1.0)  # tail so the last audio frames reach the client

    try:
        await session.aclose()
    except Exception:  # noqa: BLE001
        pass

    await _finalize_once()

    # Close the room so the candidate/admin pages get a clean Disconnected.
    try:
        await ctx.delete_room()
    except Exception:  # noqa: BLE001
        pass
    logger.info("Interview %s fully complete", interview_uuid)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
