"""HR-editable email templates with {{placeholder}} substitution.

Resolution order for a given kind: this job's template, then the global
default (job_uuid NULL), then the built-in DEFAULTS below. So an untouched
system behaves exactly as it did before templates existed, and a recruiter can
override one job's wording without touching anyone else's.

Rendering is deliberately dumb string replacement, not a template engine:
these bodies are edited by recruiters in a textarea, and an engine would turn
a typo into a 500 (or worse, let template syntax reach an email).
"""

import re
from datetime import datetime

from sqlalchemy import select

from core.models import EmailTemplate

# kind -> (label, subject, body, [placeholders])
DEFAULTS: dict[str, dict] = {
    "onsite_interview": {
        "label": "Onsite / scheduled interview invitation",
        "subject": "{{job_title}} — Interview Invitation",
        "body": (
            "Dear {{candidate_name}},\n\n"
            "We are pleased to invite you to an interview for the "
            "{{job_title}} position.\n\n"
            "Interview Details:\n"
            "- Type: {{interview_type}}\n"
            "- Date & Time: {{date_time}}\n"
            "- Duration: {{duration}} minutes\n"
            "{{location_line}}"
            "{{notes_block}}\n"
            "Please confirm your availability by replying to this email.\n\n"
            "Best regards,\nThe Recruiting Team\n"
        ),
        "placeholders": [
            "candidate_name", "job_title", "interview_type", "date_time",
            "duration", "location", "location_line", "notes", "notes_block",
        ],
    },
    "stage_change": {
        "label": "Automatic status update notification",
        "subject": "{{job_title}} — Application Status Update",
        "body": (
            "Dear {{candidate_name}},\n\n"
            "This is to inform you that the status of your application for the "
            "{{job_title}} position has been updated to: {{stage_name}}.\n\n"
            "Please log in to your portal dashboard to view the latest details:\n"
            "{{portal_url}}\n\n"
            "If you have any questions, please don't hesitate to reach out.\n\n"
            "Best regards,\nThe Recruiting Team\n"
        ),
        "placeholders": [
            "candidate_name", "job_title", "stage_name", "portal_url",
        ],
    },
    "stage_rejected": {
        "label": "Rejection notification (sent on stage move to Rejected)",
        "subject": "{{job_title}} — Application Update",
        "body": (
            "Dear {{candidate_name}},\n\n"
            "Thank you for your interest in the {{job_title}} position and for "
            "taking the time to go through our selection process.\n\n"
            "After careful consideration, we regret to inform you that we will "
            "not be proceeding with your application at this time.\n\n"
            "We encourage you to apply for future openings that match your "
            "qualifications. You can check available positions on our portal:\n"
            "{{portal_url}}\n\n"
            "We wish you all the best in your career.\n\n"
            "Best regards,\nThe Recruiting Team\n"
        ),
        "placeholders": [
            "candidate_name", "job_title", "portal_url",
        ],
    },
    "stage_hired": {
        "label": "Hiring congratulations (sent on stage move to Hired)",
        "subject": "{{job_title}} — Congratulations!",
        "body": (
            "Dear {{candidate_name}},\n\n"
            "We are delighted to inform you that you have been selected for the "
            "{{job_title}} position. Congratulations!\n\n"
            "Please log in to your portal dashboard for further details and "
            "next steps:\n{{portal_url}}\n\n"
            "We look forward to welcoming you to the team.\n\n"
            "Best regards,\nThe Recruiting Team\n"
        ),
        "placeholders": [
            "candidate_name", "job_title", "portal_url",
        ],
    },
}

_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(text: str, values: dict) -> str:
    """Replace {{token}} with values[token]. Unknown tokens are left as-is.

    Leaving an unknown token visible is intentional: a recruiter who typos
    {{candidat_name}} should see it in the preview, not silently email a
    candidate a blank where their name should be.
    """
    def sub(m):
        key = m.group(1)
        return str(values[key]) if key in values else m.group(0)

    return _TOKEN.sub(sub, text or "")


def get(db, kind: str, job_uuid: str | None) -> dict:
    """The effective template for this kind + job, and where it came from."""
    if kind not in DEFAULTS:
        raise ValueError(f"Unknown template kind '{kind}'.")

    row = None
    if job_uuid:
        row = db.execute(select(EmailTemplate).where(
            EmailTemplate.kind == kind,
            EmailTemplate.job_uuid == job_uuid)).scalars().first()
    source = "job" if row else None
    if not row:
        row = db.execute(select(EmailTemplate).where(
            EmailTemplate.kind == kind,
            EmailTemplate.job_uuid.is_(None))).scalars().first()
        source = "global" if row else "builtin"

    d = DEFAULTS[kind]
    return {
        "kind": kind,
        "label": d["label"],
        "subject": row.subject if row else d["subject"],
        "body": row.body if row else d["body"],
        "placeholders": d["placeholders"],
        "source": source,
        "updated_by": row.updated_by if row else "",
        "updated_at": (row.updated_at.isoformat()
                       if row and row.updated_at else None),
        "is_default": row is None,
    }


def save(db, kind: str, job_uuid: str | None, subject: str, body: str,
         updated_by: str = "") -> EmailTemplate:
    if kind not in DEFAULTS:
        raise ValueError(f"Unknown template kind '{kind}'.")
    if not (subject or "").strip():
        raise ValueError("Subject is required.")
    if not (body or "").strip():
        raise ValueError("Body is required.")

    q = select(EmailTemplate).where(EmailTemplate.kind == kind)
    q = q.where(EmailTemplate.job_uuid == job_uuid if job_uuid
                else EmailTemplate.job_uuid.is_(None))
    row = db.execute(q).scalars().first()
    if not row:
        row = EmailTemplate(kind=kind, job_uuid=job_uuid)
        db.add(row)
    row.subject = subject.strip()
    row.body = body
    row.updated_by = updated_by
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def reset(db, kind: str, job_uuid: str | None) -> bool:
    """Drop the override so the next level down applies again."""
    q = select(EmailTemplate).where(EmailTemplate.kind == kind)
    q = q.where(EmailTemplate.job_uuid == job_uuid if job_uuid
                else EmailTemplate.job_uuid.is_(None))
    row = db.execute(q).scalars().first()
    if not row:
        return False
    db.delete(row)
    return True
