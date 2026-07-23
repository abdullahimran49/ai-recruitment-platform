"""Email drafting (LLM) for the Streamlit app.

Sending is delegated to core.mailer, which picks the transport from
EMAIL_MODE in .env ("brevo" = HTTPS API, "smtp" = classic SMTP) — so the
Streamlit app and the portal backend always send the same way.
"""

import llm
from core.mailer import send_email, smtp_configured  # noqa: F401 - re-exported

EMAIL_KINDS = {
    "interview_invite": "an invitation to schedule an interview",
    "next_steps": "an update that they have advanced to the next stage",
    "assessment": "an invitation to complete an online MCQ assessment",
    "rejection": "a polite, encouraging rejection",
}

_DRAFT_SYSTEM = """You are a professional, warm HR coordinator writing on \
behalf of a company. Write a concise recruiting email (120-180 words) to a \
candidate. Rules:
- Professional but human tone; no corporate cliches, no placeholders like \
[Company] — use the details given.
- Reference the specific role. If candidate strengths are provided, mention \
one genuinely.
- For rejections: be kind, brief, and do not give false hope.
- Do not invent dates, links, salaries, or details not provided.
- End with the signature block given in SIGN_OFF_AS.

Respond with ONLY a JSON object: {"subject": str, "body": str}"""


def draft_email(kind: str, candidate_name: str, job_title: str,
                company: str, strengths: str = "", extra: str = "",
                sign_off: str = "The Recruiting Team") -> dict:
    """LLM-draft one email. Returns {"subject": str, "body": str}."""
    purpose = EMAIL_KINDS.get(kind, kind)
    user = (
        f"EMAIL PURPOSE: {purpose}\n"
        f"CANDIDATE NAME: {candidate_name}\n"
        f"ROLE: {job_title}\n"
        f"COMPANY: {company}\n"
        f"CANDIDATE STRENGTHS: {strengths or '(not provided)'}\n"
        f"EXTRA INSTRUCTIONS: {extra or '(none)'}\n"
        f"SIGN_OFF_AS: {sign_off}"
    )
    data = llm.chat_json(_DRAFT_SYSTEM, user)
    return {
        "subject": str(data.get("subject", f"Regarding your application — {job_title}")),
        "body": str(data.get("body", "")),
    }
