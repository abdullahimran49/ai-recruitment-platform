"""Automated AI voice interview (candidate-facing).

Flow: unique link -> OTP (same identity model as tests) -> proctoring check
-> window-gated join -> the candidate enters a LiveKit room (room name =
ai_interviews.uuid) where a voice agent (interview_agent.py) conducts the
interview; an admin can join the same room to watch and speak. The agent
owns the transcript, the post-interview evaluation, and the completion
email — this router only mints room tokens and handles proctoring
termination.

Proctoring mirrors the MCQ test (warnings, evidence snapshots, termination)
except voice detection — the candidate is supposed to talk.
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from openai import OpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select

import config
import llm
from dotenv import load_dotenv
load_dotenv()

# The shared strict evaluator lives at the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from interview_eval import evaluate_transcript  # noqa: E402

from core import cv_brief, mailer, otps, security  # noqa: E402
from core.interview_languages import (  # noqa: E402
    LANGUAGES, configured_languages, language_payload,
)
from core.models import AIInterview, InterviewEvent  # noqa: E402
from portal.backend.deps import candidate_assignment_id, get_db  # noqa: E402
from portal.backend.routers.candidate import (  # noqa: E402
    OTP_RATE_LIMIT,
    OTP_RATE_WINDOW_MIN,
    PROCTOR_EVENT_TYPES,
    PROCTOR_INFO_TYPES,
    _EVIDENCE_MAX_CHARS,
)

# During a SPOKEN interview the candidate naturally looks away and down to
# think while talking, so eye-gaze and head-pose are NOT reliable cheating
# signals here — they are logged for the recruiter but never count as warnings
# or terminate the interview. Hard integrity signals (no face, a second
# person, tab/app switching, screen-share stopped, etc.) still terminate.
INTERVIEW_INFO_TYPES = PROCTOR_INFO_TYPES | {"gaze_away", "head_turn_away"}

router = APIRouter(prefix="/api/portal/interview", tags=["ai-interview"])

EARLY_JOIN_MINUTES = 10      # can enter the room this early
LATE_JOIN_MINUTES = 30       # after this, the interview is missed
FINISH_GRACE_SECONDS = 60

# LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
# LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
# LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")

class EmailIn(BaseModel):
    email: str


class VerifyIn(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)




class FinishIn(BaseModel):
    terminated_reason: str | None = Field(default=None, max_length=200)


class ProctorEventIn(BaseModel):
    event_type: str
    detail: str = Field(default="", max_length=400)
    evidence: str | None = None


def _get(db, token: str) -> AIInterview:
    iv = db.get(AIInterview, token)
    if not iv:
        raise HTTPException(404, "This interview link is not valid.")
    finalize_if_expired(iv, db)
    return iv


def _terminate_interview(iv: AIInterview, reason: str) -> None:
    """Close an interview immediately; client follow-up is not required."""
    iv.status = "terminated"
    iv.completed_at = datetime.utcnow()
    iv.terminated_reason = reason
    iv.candidate.status = "interview_terminated"
    try:
        mailer.send_email(
            iv.candidate.email, f"Interview ended — {iv.job.title}",
            f"Hi {iv.candidate.name or 'there'},\n\nYour interview for the "
            f"{iv.job.title} role was ended automatically due to repeated "
            f"proctoring violations ({reason}).\n\nThe recruiting team will "
            "review the session.\n")
    except Exception:  # noqa: BLE001 - finalization must not depend on email
        pass


def _window_state(iv: AIInterview) -> str:
    now = datetime.utcnow()
    if iv.status in ("completed", "terminated", "cancelled", "missed"):
        return iv.status
    if iv.status == "started":
        return "started"
    if now < iv.scheduled_at - timedelta(minutes=EARLY_JOIN_MINUTES):
        return "too_early"
    if now > iv.scheduled_at + timedelta(minutes=LATE_JOIN_MINUTES):
        return "missed"
    return "open"


def _check_joinable(iv: AIInterview):
    state = _window_state(iv)
    if state in ("open", "started"):
        return
    msgs = {
        "too_early": "This interview has not opened yet.",
        "missed": "The join window for this interview has passed.",
        "completed": "This interview has already been completed.",
        "terminated": "This interview was terminated due to proctoring "
                      "violations.",
        "cancelled": "This interview was cancelled.",
    }
    raise HTTPException(410, msgs.get(state, "Interview unavailable."))


def _deadline(iv: AIInterview) -> datetime:
    return (iv.started_at + timedelta(minutes=iv.duration_minutes)
            + timedelta(seconds=FINISH_GRACE_SECONDS))


def finalize_if_expired(iv: AIInterview, db) -> bool:
    """Lazily evaluate + close interviews whose time ran out, and mark
    no-shows as missed."""
    now = datetime.utcnow()
    if iv.status == "scheduled" and now > iv.scheduled_at + timedelta(
            minutes=LATE_JOIN_MINUTES):
        iv.status = "missed"
        return True
    if iv.status == "started" and iv.started_at and now > _deadline(iv):
        _evaluate(iv)
        iv.status = "completed"
        iv.completed_at = _deadline(iv)
        db.add(InterviewEvent(
            interview_uuid=iv.uuid, event_type="auto_submitted",
            is_warning=False,
            detail="Interview auto-closed at the time limit."))
        return True
    return False


def _jd_snippet(iv: AIInterview, limit: int = 1500) -> str:
    jd = (iv.job.jd_text or "").strip()
    return jd[:limit]


# ---- LiveKit room helpers (Commented out for PIA architecture) ---------------------------------------------------------

def _room_metadata(iv: AIInterview, remaining: int) -> str:
    return json.dumps({
        "interview_uuid": iv.uuid,
        "candidate_name": iv.candidate.name or "Candidate",
        "candidate_identity": f"candidate_{iv.uuid[:8]}",
        "job_title": iv.job.title,
        "jd_snippet": _jd_snippet(iv),
        "cv_brief": cv_brief.build(iv.candidate),
        "focus": iv.focus or "",
        "num_questions": iv.num_questions,
        "duration_minutes": iv.duration_minutes,
        "remaining_seconds": remaining,
    })

# async def _ensure_room(iv: AIInterview, remaining: int) -> None:
#     lkapi = lk_api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
#     try:
#         await lkapi.room.create_room(lk_api.CreateRoomRequest(
#             name=iv.uuid,
#             metadata=_room_metadata(iv, remaining),
#             empty_timeout=600,
#             departure_timeout=90,
#             max_participants=6,
#         ))
#     finally:
#         await lkapi.aclose()
# 
# async def _delete_room(room_name: str) -> None:
#     lkapi = lk_api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
#     try:
#         await lkapi.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
#     except Exception:
#         pass
#     finally:
#         await lkapi.aclose()


def _evaluate(iv: AIInterview):
    """Post-interview evaluation via the shared strict evaluator; never
    raises (stores a fallback instead)."""
    result = evaluate_transcript(
        iv.job.title, _jd_snippet(iv), iv.transcript,
        num_questions=iv.num_questions or 5,
        terminated=(iv.status == "terminated"))
    iv.ai_score = result["score"]
    iv.ai_summary = {k: result[k] for k in
                     ("summary", "strengths", "concerns", "per_question")}


# ---- endpoints -----------------------------------------------------------------

@router.get("/{token}/info")
def info(token: str, db=Depends(get_db)):
    iv = _get(db, token)
    state = _window_state(iv)
    return {
        "job_title": iv.job.title,
        "candidate_name": iv.candidate.name,
        "scheduled_at": iv.scheduled_at.isoformat() + "Z",
        "duration_minutes": iv.duration_minutes,
        "status": iv.status,
        "window": state,
        "opens_at": (iv.scheduled_at - timedelta(minutes=EARLY_JOIN_MINUTES)
                     ).isoformat() + "Z",
        "max_warnings": iv.max_warnings,
        "languages": language_payload(iv.languages),
    }


@router.post("/{token}/request-otp")
def request_otp(token: str, body: EmailIn, db=Depends(get_db)):
    iv = _get(db, token)
    _check_joinable(iv)
    email = body.email.strip().lower()
    if email != (iv.candidate.email or "").strip().lower():
        raise HTTPException(403, "This email does not match the invitation.")
    if otps.recent_count(db, email, otps.INTERVIEW, iv.uuid,
                         OTP_RATE_WINDOW_MIN) >= OTP_RATE_LIMIT:
        raise HTTPException(429, "Too many codes requested — use the most "
                                 "recent code or wait a few minutes.")
    code = otps.issue(db, email, otps.INTERVIEW, iv.uuid)
    ok, msg = mailer.send_email(
        email, "Your interview verification code",
        f"Hi {iv.candidate.name or 'there'},\n\nYour one-time code for the "
        f"{iv.job.title} interview is:\n\n    {code}\n\n"
        f"It expires in {security.OTP_TTL_MINUTES} minutes.\n")
    if not ok:
        raise HTTPException(502, f"Could not send the code: {msg}")
    return {"sent": True}


@router.post("/{token}/verify-otp")
def verify_otp(token: str, body: VerifyIn, db=Depends(get_db)):
    iv = _get(db, token)
    _check_joinable(iv)
    email = body.email.strip().lower()
    if email != (iv.candidate.email or "").strip().lower():
        raise HTTPException(403, "This email does not match the invitation.")
    ok, reason = otps.verify(db, email, body.code, otps.INTERVIEW, iv.uuid)
    if reason == "expired":
        raise HTTPException(401, "Code expired — request a new one.")
    if reason == "attempts":
        raise HTTPException(429, "Too many attempts — request a new code.")
    if not ok:
        raise HTTPException(401, "Incorrect code.")
    return {"token": security.candidate_token(iv.uuid)}


@router.post("/{token}/join")
async def join_room(token: str, db=Depends(get_db),
                    auth: str = Depends(candidate_assignment_id)):
    """Candidate joins the LiveKit interview room.

    Creates the room (with metadata containing the interview context) if it
    doesn't exist yet, transitions the interview to 'started', and returns a
    LiveKit participant token so the frontend can connect.
    """
    if auth != token:
        raise HTTPException(403, "Token does not match this interview.")
    iv = _get(db, token)
    _check_joinable(iv)

    # Transition to started on first join
    if iv.status == "scheduled":
        iv.status = "started"
        iv.started_at = datetime.utcnow()
        iv.transcript = []
    iv.last_seen = datetime.utcnow()

    remaining = max(0, int(
        (iv.started_at + timedelta(minutes=iv.duration_minutes)
         - datetime.utcnow()).total_seconds()))

    # await _ensure_room(iv, remaining) # LiveKit disabled

    # at = (
    #     lk_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    #     .with_identity(f"candidate_{iv.uuid[:8]}")
    #     .with_name(iv.candidate.name or "Candidate")
    #     .with_metadata(json.dumps({"role": "candidate"}))
    #     .with_grants(lk_api.VideoGrants(
    #         room_join=True,
    #         room=iv.uuid,
    #         can_publish=True,
    #         can_subscribe=True,
    #     ))
    # )

    protocol = "wss" if os.getenv("PORTAL_URL", "").startswith("https") else "ws"
    # Localhost fallback to ws if no PORTAL_URL or http
    
    return {
        "ws_url": f"/api/portal/interview/{token}/ws",
        "remaining_seconds": remaining,
        "warnings": iv.proctor_warnings or 0,
        "max_warnings": iv.max_warnings,
        "languages": language_payload(iv.languages),
    }


@router.post("/{token}/finish")
async def finish(token: str, body: FinishIn, db=Depends(get_db),
                 auth: str = Depends(candidate_assignment_id)):
    """Explicitly finish an interview, with optional proctor termination."""
    if auth != token:
        raise HTTPException(403, "Token does not match this interview.")
    iv = _get(db, token)
    if iv.status not in ("started",):
        raise HTTPException(410, "The interview is not in progress.")
    if not body.terminated_reason:
        iv.status = "completed"
        iv.completed_at = datetime.utcnow()
        _evaluate(iv)
        db.commit()
        try:
            mailer.send_email(
                iv.candidate.email, f"Interview completed — {iv.job.title}",
                f"Hi {iv.candidate.name or 'there'},\n\nThank you for "
                f"completing your interview for the {iv.job.title} role. The "
                "recruiting team will review it and be in touch.\n")
        except Exception:
            pass
        return {"finished": True, "terminated": False}

    _terminate_interview(iv, body.terminated_reason)
    db.commit()
    return {"finished": True, "terminated": True}


@router.post("/{token}/proctor-event")
def proctor_event(token: str, body: ProctorEventIn, db=Depends(get_db),
                  auth: str = Depends(candidate_assignment_id)):
    if auth != token:
        raise HTTPException(403, "Token does not match this interview.")
    iv = _get(db, token)
    if iv.status not in ("scheduled", "started"):
        raise HTTPException(410, "Interview is not active.")
    if body.event_type not in PROCTOR_EVENT_TYPES:
        raise HTTPException(422, f"Unknown event_type '{body.event_type}'.")
    evidence = body.evidence
    if evidence and len(evidence) > _EVIDENCE_MAX_CHARS:
        evidence = None
    # Gaze/head-pose are informational during a spoken interview (see
    # INTERVIEW_INFO_TYPES) — logged, but never a warning and never terminate.
    is_warning = body.event_type not in INTERVIEW_INFO_TYPES
    db.add(InterviewEvent(
        interview_uuid=iv.uuid, event_type=body.event_type,
        detail=body.detail, evidence=evidence, is_warning=is_warning))
    if is_warning:
        iv.proctor_warnings = (iv.proctor_warnings or 0) + 1
    iv.last_seen = datetime.utcnow()
    terminate = (iv.proctor_warnings or 0) >= iv.max_warnings
    if terminate:
        _terminate_interview(
            iv, f"Reached the proctoring limit ({iv.max_warnings} warnings)")
    return {
        "warnings": iv.proctor_warnings or 0,
        "max_warnings": iv.max_warnings,
        "terminate": terminate,
    }

_WHISPER_LANGUAGE_CODES = {
    "english": "en", "urdu": "ur", "arabic": "ar", "hindi": "hi",
    "punjabi": "pa", "panjabi": "pa", "spanish": "es", "french": "fr",
    "german": "de", "portuguese": "pt", "chinese": "zh",
    "mandarin": "zh",
}


def _detected_language_code(value) -> str | None:
    if not value:
        return None
    raw = str(value).strip().lower().replace("_", "-")
    code = raw.split("-", 1)[0]
    if code in LANGUAGES:
        return code
    return _WHISPER_LANGUAGE_CODES.get(raw)

def _transcribe_audio(client: OpenAI, path: str) -> tuple[str, str | None]:
    with open(path, "rb") as audio_file:
        response = client.audio.transcriptions.create(
            model="whisper-large-v3", file=audio_file,
            response_format="verbose_json")
    if isinstance(response, str):
        return response.strip(), None
    return ((getattr(response, "text", None) or "").strip(),
            _detected_language_code(getattr(response, "language", None)))


def _save_transcript(iv: AIInterview, db, entries: list[dict]) -> None:
    iv.transcript = list(entries)
    iv.questions_asked = sum(
        1 for entry in entries
        if entry.get("role") == "interviewer"
        and any(mark in entry.get("text", "") for mark in ("?", "؟", "？")))
    iv.last_seen = datetime.utcnow()
    db.commit()


@router.websocket("/{token}/ws")
async def interview_ws(websocket: WebSocket, token: str, auth: str = None,
                       db=Depends(get_db)):
    """Run a checkpointed interview; network drops remain resumable."""
    await websocket.accept()
    if not auth:
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            hello = json.loads(first)
            if hello.get("type") == "authenticate":
                auth = hello.get("token")
        except (asyncio.TimeoutError, json.JSONDecodeError,
                WebSocketDisconnect):
            auth = None
    payload = security.read_token(auth) if auth else None
    if (not payload or payload.get("sub") != token
            or payload.get("kind") != "candidate"):
        await websocket.close(code=1008, reason="Token mismatch")
        return
    try:
        iv = _get(db, token)
        _check_joinable(iv)
    except Exception as exc:
        await websocket.close(code=1008, reason=str(exc))
        return

    stt_client = OpenAI(base_url=config.BASE_URL,
                        api_key=config.API_KEY or "none")
    allowed_codes = configured_languages(iv.languages)
    primary_code = allowed_codes[0]
    primary_name = LANGUAGES[primary_code]["label"]
    allowed_names = ", ".join(LANGUAGES[c]["label"] for c in allowed_codes)
    system_prompt = (
        f"You are Nova, a warm professional spoken interviewer for the "
        f"{iv.job.title or 'open'} position, speaking with "
        f"{iv.candidate.name}.\n\nJob description:\n{_jd_snippet(iv)}\n\n"
        f"Candidate background:\n{cv_brief.build(iv.candidate)}\n\nRules:\n"
        f"- Understand the candidate in whatever language they speak (multilingual comprehension).\n"
        f"- ALWAYS respond in English.\n"
        f"- Strictly maintain relevance: allow no off-topic or irrelevant conversations. Redirect the candidate back to the interview if they stray.\n"
        f"- Ensure your dialogue flows naturally and conversationally.\n"
        f"- Keep replies to 1-3 conversational sentences, without markdown.\n"
        f"- Greet the candidate, explain the purpose, and ask one question at a time.\n"
        f"- Conduct {iv.num_questions or 5} main questions focused on "
        f"{iv.focus or 'the job requirements and candidate experience'}.\n"
        f"- Interpret imperfect speech-to-text charitably.\n")

    entries = list(iv.transcript or [])
    messages = [{"role": "system", "content": system_prompt}]
    for entry in entries:
        role = "assistant" if entry.get("role") == "interviewer" else "user"
        content = entry.get("text", "")
        messages.append({"role": role, "content": content})

    finish_requested = False
    timed_out = False
    failed = False
    try:
        for entry in entries:
            code = entry.get("language") or primary_code
            event = {"type": "transcript", "role": entry.get("role"),
                     "text": entry.get("text", ""), "language": code,
                     "historical": True}
            if entry.get("role") == "interviewer":
                event["tts_locale"] = LANGUAGES.get(
                    code, LANGUAGES[primary_code])["tts_locale"]
            await websocket.send_json(event)

        if not entries:
            reply = (await asyncio.to_thread(
                llm.chat_text, messages, max_tokens=250,
                temperature=0.6)).strip()
            messages.append({"role": "assistant", "content": reply})
            entries.append({"role": "interviewer", "text": reply,
                            "language": primary_code,
                            "at": datetime.utcnow().isoformat()})
            _save_transcript(iv, db, entries)
            await websocket.send_json({
                "type": "transcript", "role": "interviewer", "text": reply,
                "language": primary_code,
                "tts_locale": LANGUAGES[primary_code]["tts_locale"]})

        while True:
            seconds_left = (_deadline(iv) - datetime.utcnow()).total_seconds()
            if seconds_left <= 0:
                timed_out = True
                break
            try:
                # Poll every 2 seconds to check if terminated by proctor HTTP event
                incoming = await asyncio.wait_for(
                    websocket.receive(), timeout=min(2.0, seconds_left))
            except asyncio.TimeoutError:
                db.expire(iv, ["status"])
                if iv.status == "terminated":
                    break
                if seconds_left <= 2.0:
                    timed_out = True
                    break
                continue
            if incoming["type"] == "websocket.disconnect":
                break
            if incoming.get("text") is None:
                continue
            
            metadata = {}
            duration_ms = 0
            speech_ms = 0
            try:
                metadata = json.loads(incoming["text"])
                if metadata.get("type") == "finish":
                    finish_requested = True
                    break
                elif metadata.get("type") == "next_question":
                    # Candidate clicked "Next Question" manually.
                    messages.append({"role": "user", "content": "[Internal: The candidate explicitly requested the next question. Acknowledge and move on.]"})
                    reply = (await asyncio.to_thread(
                        llm.chat_text, messages, max_tokens=250,
                        temperature=0.6)).strip()
                    messages.append({"role": "assistant", "content": reply})
                    entries.append({"role": "interviewer", "text": reply,
                                    "language": primary_code,
                                    "at": datetime.utcnow().isoformat()})
                    _save_transcript(iv, db, entries)
                    await websocket.send_json({
                        "type": "transcript", "role": "interviewer", "text": reply,
                        "language": primary_code,
                        "tts_locale": LANGUAGES[primary_code]["tts_locale"]})
                    continue
                duration_ms = int(metadata.get("duration_ms", 0))
                speech_ms = int(metadata.get("speech_ms", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

            is_valid_audio = (
                metadata.get("type") == "audio"
                and 900 <= duration_ms <= 180000
                and 350 <= speech_ms <= duration_ms
            )

            if metadata.get("type") == "audio":
                # We expect a binary frame to follow immediately. Consume it to prevent desync.
                seconds_left = (_deadline(iv) - datetime.utcnow()).total_seconds()
                try:
                    audio_frame = await asyncio.wait_for(
                        websocket.receive(), timeout=max(0.1, min(2.0, seconds_left))
                    )
                except asyncio.TimeoutError:
                    db.expire(iv, ["status"])
                    if iv.status == "terminated":
                        break
                    if seconds_left <= 2.0:
                        timed_out = True
                        break
                    continue
                if audio_frame["type"] == "websocket.disconnect":
                    break

                if not is_valid_audio:
                    continue

                data = audio_frame.get("bytes")
                if not data or len(data) < 4000:
                    continue
            with tempfile.NamedTemporaryFile(suffix=".webm",
                                              delete=False) as temp_audio:
                temp_audio.write(data)
                temp_path = temp_audio.name
            try:
                text, detected_code = await asyncio.to_thread(
                    _transcribe_audio, stt_client, temp_path)
            except Exception as exc:
                print(f"STT Error: {exc}")
                text, detected_code = "", None
                await websocket.send_json({
                    "type": "notice",
                    "message": "I could not hear that clearly. Please try again."})
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if not text:
                continue

            response_code = (detected_code if detected_code in allowed_codes
                             else primary_code)
            detected_name = (LANGUAGES[detected_code]["label"]
                             if detected_code in LANGUAGES else "unknown")
            allowed_note = ("Always respond in English.")
            await websocket.send_json({
                "type": "transcript", "role": "candidate", "text": text,
                "language": detected_code})
            entries.append({"role": "candidate", "text": text,
                            "language": detected_code,
                            "at": datetime.utcnow().isoformat()})
            _save_transcript(iv, db, entries)
            candidate_turns = sum(
                1 for entry in entries if entry.get("role") == "candidate")
            closing_turn = candidate_turns >= (iv.num_questions or 5)
            user_message = (
                f"[Internal speech-language note: detected {detected_name}. "
                f"{allowed_note}]\nCandidate answer: {text}")
            if closing_turn:
                user_message += ("\n[Internal: Planned questions are complete. "
                                 "Thank the candidate and close without asking "
                                 "another question.]")
            messages.append({"role": "user", "content": user_message})
            reply = (await asyncio.to_thread(
                llm.chat_text, messages, max_tokens=250,
                temperature=0.6)).strip()
            messages.append({"role": "assistant", "content": reply})
            entries.append({"role": "interviewer", "text": reply,
                            "language": response_code,
                            "at": datetime.utcnow().isoformat()})
            _save_transcript(iv, db, entries)
            await websocket.send_json({
                "type": "transcript", "role": "interviewer", "text": reply,
                "language": response_code,
                "tts_locale": LANGUAGES[response_code]["tts_locale"]})
            if closing_turn:
                finish_requested = True
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        failed = True
        print(f"Interview WS Error: {exc}")
        try:
            await websocket.send_json({
                "type": "error", "message": "The connection had a problem. "
                "Your answers are saved; reconnect to continue."})
        except Exception:
            pass

    db.expire_all()
    iv = db.get(AIInterview, token)
    completed = False
    if iv and iv.status == "terminated":
        iv.transcript = list(entries)
        _evaluate(iv)
        db.commit()
    elif (iv and iv.status == "started" and not failed
          and (finish_requested or timed_out)):
        iv.transcript = list(entries)
        iv.status = "completed"
        iv.completed_at = datetime.utcnow()
        _evaluate(iv)
        db.commit()
        completed = True
        try:
            mailer.send_email(
                iv.candidate.email, f"Interview completed — {iv.job.title}",
                f"Hi {iv.candidate.name or 'there'},\n\nThank you for "
                f"completing your interview for the {iv.job.title} role. The "
                "recruiting team will review it and be in touch.\n")
        except Exception:
            pass
    if completed:
        try:
            await websocket.send_json({"type": "complete",
                                       "status": "completed"})
            await websocket.close(code=1000)
        except Exception:
            pass


async def _legacy_interview_ws(websocket: WebSocket, token: str,
                               auth: str = None, db=Depends(get_db)):
    await websocket.accept()
    payload = security.read_token(auth) if auth else None
    if not payload or payload.get("sub") != token or payload.get("kind") != "candidate":
        await websocket.close(code=1008, reason="Token mismatch")
        return
        
    try:
        iv = _get(db, token)
        _check_joinable(iv)
    except Exception as e:
        await websocket.close(code=1008, reason=str(e))
        return

    # Chat generation goes through llm.chat_text so Groq failures fall back to
    # local Ollama. Audio transcription remains on Groq Whisper.
    stt_client = OpenAI(base_url=config.BASE_URL,
                        api_key=config.API_KEY or "none")
    allowed_codes = configured_languages(iv.languages)
    primary_code = allowed_codes[0]
    allowed_names = ", ".join(LANGUAGES[c]["label"] for c in allowed_codes)
    primary_name = LANGUAGES[primary_code]["label"]
    
    system_prompt = (
        f"You are Nova, a warm, professional AI interviewer conducting a "
        f"spoken screening interview for the {iv.job.title or 'open'} position. "
        f"You are talking with {iv.candidate.name}.\n\n"
        f"## Job description (excerpt)\n{_jd_snippet(iv)}\n\n"
        f"## Rules\n"
        f"- Understand the candidate in whatever language they speak (multilingual comprehension).\n"
        f"- ALWAYS respond in English.\n"
        f"- Strictly maintain relevance: allow no off-topic or irrelevant conversations. Redirect the candidate back to the interview if they stray.\n"
        f"- Ensure your dialogue flows naturally and conversationally.\n"
        f"- Keep replies short (1-3 conversational sentences). No markdown.\n"
        f"- Greet {iv.candidate.name} by first name, introduce yourself and the purpose of the call, then ask the first question.\n"
        f"- Ask one question at a time and wait for the answer.\n"
        f"- Aim for {iv.num_questions} main questions. Focus areas: "
        f"{iv.focus or 'the job requirements and candidate experience'}.\n"
        f"- Speech-to-text is imperfect: interpret answers charitably.\n"
        f"- If the candidate asks to stop or is unresponsive, say goodbye.\n"
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    transcript_entries = []
    
    try:
        reply = llm.chat_text(
            messages, max_tokens=250, temperature=0.6).strip()
        messages.append({"role": "assistant", "content": reply})
        transcript_entries.append({"role": "interviewer", "text": reply,
                                   "at": datetime.utcnow().isoformat()})
        await websocket.send_json({
            "type": "transcript", "role": "interviewer", "text": reply,
            "language": primary_code,
            "tts_locale": LANGUAGES[primary_code]["tts_locale"],
        })
        
        while True:
            # Every binary audio blob is preceded by a small metadata frame
            # generated after client-side VAD has confirmed real speech.  Do
            # not transcribe clips that are too short to contain an answer.
            incoming = await websocket.receive()
            if incoming["type"] == "websocket.disconnect":
                break
            if incoming.get("text") is None:
                continue
            try:
                metadata = json.loads(incoming["text"])
                duration_ms = int(metadata.get("duration_ms", 0))
                speech_ms = int(metadata.get("speech_ms", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (metadata.get("type") != "audio" or duration_ms < 900
                    or duration_ms > 180000 or speech_ms < 350
                    or speech_ms > duration_ms):
                continue

            audio_frame = await websocket.receive()
            if audio_frame["type"] == "websocket.disconnect":
                break
            data = audio_frame.get("bytes")
            if not data or len(data) < 4000:
                continue
                
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
                
            text = ""
            detected_code = None
            try:
                with open(tmp_path, "rb") as audio_file:
                    stt_resp = stt_client.audio.transcriptions.create(
                        model="whisper-large-v3", # Groq Whisper
                        file=audio_file,
                        response_format="verbose_json"
                    )
                if isinstance(stt_resp, str):
                    text = stt_resp.strip()
                else:
                    text = (getattr(stt_resp, "text", None) or "").strip()
                    detected_code = _detected_language_code(
                        getattr(stt_resp, "language", None))
            except Exception as e:
                print(f"STT Error: {e}")
            finally:
                os.remove(tmp_path)
                
            if not text:
                continue
                
            response_code = (detected_code if detected_code in allowed_codes
                             else primary_code)
            detected_name = (LANGUAGES[detected_code]["label"]
                             if detected_code in LANGUAGES else "unknown")
            allowed_note = ("This is an allowed language. Reply in it."
                            if detected_code in allowed_codes else
                            f"This is not an allowed language. Reply in "
                            f"{primary_name} and ask for {allowed_names}.")
            await websocket.send_json({
                "type": "transcript", "role": "candidate", "text": text,
                "language": detected_code,
            })
            transcript_entries.append({"role": "candidate", "text": text,
                                       "at": datetime.utcnow().isoformat()})
            messages.append({
                "role": "user",
                "content": (f"[Internal speech-language note: detected "
                            f"{detected_name}. {allowed_note}]\n"
                            f"Candidate answer: {text}"),
            })
            
            reply = llm.chat_text(
                messages, max_tokens=250, temperature=0.6).strip()
            messages.append({"role": "assistant", "content": reply})
            transcript_entries.append({"role": "interviewer", "text": reply,
                                       "at": datetime.utcnow().isoformat()})
            await websocket.send_json({
                "type": "transcript", "role": "interviewer", "text": reply,
                "language": response_code,
                "tts_locale": LANGUAGES[response_code]["tts_locale"],
            })
            
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Interview WS Error: {e}")
        try:
            await websocket.close()
        except:
            pass
            
    # Finalize interview if it hasn't been terminated
    db.expire_all() # ensure we have fresh state
    iv = db.get(AIInterview, token)
    if iv and iv.status == "started":
        transcript = transcript_entries
        iv.transcript = transcript
        question_marks = ("?", "؟", "？")
        iv.questions_asked = sum(
            1 for entry in transcript
            if entry["role"] == "interviewer"
            and any(mark in entry["text"] for mark in question_marks))
        
        iv.status = "completed"
        iv.completed_at = datetime.utcnow()
        _evaluate(iv)
        db.commit()
        
        # Email candidate
        try:
            mailer.send_email(
                iv.candidate.email, 
                f"Interview completed — {iv.job.title}",
                f"Hi {iv.candidate.name or 'there'},\n\nThank you for completing your interview for the {iv.job.title} role. The recruiting team will review it and be in touch about next steps.\n"
            )
        except:
            pass
