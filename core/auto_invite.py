"""Automatic AI-interview invitation when a candidate passes their test.

Fires from the candidate's own submit request, so two rules govern everything
here:

1. It must NEVER break a submission. A candidate who finished their test has
   earned their result; a failing mail server or LLM must not turn that into a
   500. Every failure path returns a reason instead of raising.
2. It must be OFF unless a recruiter turned it on for the job. This sends mail
   on the company's behalf with no human in the loop, so it is opt-in per job
   (jobs.merit_config.auto_invite_on_pass) rather than inherited.
"""

import logging
from datetime import datetime, timedelta

from core import mailer
from core.interview_languages import configured_languages
from core.models import AIInterview

_log = logging.getLogger(__name__)

_OPEN_STATUSES = ("scheduled", "started")


def _cfg(job) -> dict:
    cfg = job.merit_config or {}
    return {
        "enabled": bool(cfg.get("auto_invite_on_pass", False)),
        "delay_hours": int(cfg.get("auto_invite_delay_hours", 48)),
        "duration": int(cfg.get("auto_invite_duration_minutes", 20)),
        "num_questions": int(cfg.get("auto_invite_num_questions", 5)),
        "languages": configured_languages(cfg.get("auto_invite_languages")),
    }


def _draft_body(candidate, job, link, when, duration) -> str:
    """LLM-drafted invite, falling back to a fixed template.

    The fallback is not a nicety: this runs unattended, and a candidate who
    passed must get their invitation even when the drafting model is down.
    """
    intro = ""
    try:
        import emailer
        draft = emailer.draft_email(
            "interview", candidate.name or "Candidate", job.title,
            "our company",
            extra="The candidate has just PASSED the online assessment and is "
                  "being invited to an automated AI voice interview. Do NOT "
                  "include any greeting/salutation or sign-off/signature - the "
                  "template adds those. Do NOT mention the link, date, time or "
                  "duration - those are listed separately. Write only 1-2 warm "
                  "body paragraphs congratulating them on passing and "
                  "explaining the next step.")
        intro = (draft.get("body") or "").strip()
    except Exception as e:  # noqa: BLE001 - drafting is best-effort
        _log.warning("Auto-invite draft failed for %s, using the template: %s",
                     candidate.email, e)

    if not intro:
        intro = (f"Congratulations on passing the online assessment for the "
                 f"{job.title} position. The next step is a short automated "
                 f"voice interview, which you can take at the time below.")

    return (
        f"Dear {candidate.name or 'Candidate'},\n\n"
        f"{intro}\n\n"
        f"Your personal interview link:\n{link}\n\n"
        f"Schedule:\n"
        f"- Date & time: {when}\n"
        f"- Duration: about {duration} minutes\n"
        f"- The link opens 10 minutes before your slot and closes 30 minutes "
        f"after it.\n\n"
        f"How it works:\n"
        f"- An AI interviewer asks questions out loud; you answer by speaking "
        f"naturally.\n"
        f"- Use Chrome or Edge on a computer, in a quiet room, alone.\n"
        f"- You will verify your identity with a code sent to this email.\n"
        f"- Camera, microphone and full-screen sharing are required; the "
        f"session is monitored and repeated violations end the interview.\n\n"
        f"Best regards,\nThe Recruiting Team\n")


def maybe_auto_invite(db, assignment, portal_base: str) -> dict:
    """Invite the candidate to an AI interview if they just passed.

    Returns a dict describing what happened (never raises).
    """
    try:
        a = assignment
        job = a.test.job
        c = a.candidate
        cfg = _cfg(job)

        if not cfg["enabled"]:
            return {"invited": False, "reason": "auto-invite is off for this job"}
        if a.status != "submitted" or a.test_score is None:
            return {"invited": False, "reason": "attempt is not a submission"}
        if a.test_score < a.test.pass_score:
            return {"invited": False, "reason": "did not pass"}
        if not c.email:
            return {"invited": False, "reason": "candidate has no email"}
        # Idempotent: a retake that passes again must not mint a second
        # interview on top of one already scheduled or under way.
        if any(iv.status in _OPEN_STATUSES for iv in c.interviews_ai):
            return {"invited": False,
                    "reason": "an AI interview is already open"}

        sched = datetime.utcnow() + timedelta(hours=cfg["delay_hours"])
        iv = AIInterview(candidate_uuid=c.uuid, job_uuid=job.uuid,
                         scheduled_at=sched,
                         duration_minutes=cfg["duration"],
                         num_questions=cfg["num_questions"],
                         focus="", languages=cfg["languages"])
        db.add(iv)
        db.flush()

        link = f"{portal_base}/interview/{iv.uuid}"
        when = sched.strftime("%A, %B %d, %Y at %I:%M %p (UTC)")
        ok, msg = mailer.send_email(
            c.email, f"{job.title} — AI Interview Invitation",
            _draft_body(c, job, link, when, cfg["duration"]))
        c.status = "ai_interview_invited"
        _log.info("Auto-invited %s for %s (sent=%s)", c.email, job.title, ok)
        return {"invited": True, "interview_uuid": iv.uuid, "link": link,
                "scheduled_at": sched.isoformat(), "emailed": ok,
                "message": msg}
    except Exception as e:  # noqa: BLE001 - must never fail a submission
        _log.exception("Auto-invite failed after a passing submission")
        return {"invited": False, "reason": f"auto-invite errored: {e}"}
