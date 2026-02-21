"""LLM-generated 3-email drip sequences for hiring manager outreach."""

import logging
import re
from datetime import datetime, timezone, timedelta

from applypilot.config import load_profile, DRIP_SCHEDULE
from applypilot.llm import get_client

log = logging.getLogger(__name__)


def _job_context(job: dict) -> str:
    """Short job context string for prompts."""
    desc = (job.get("full_description") or job.get("description") or "")[:4000]
    return (
        f"Role: {job.get('title') or 'N/A'}\n"
        f"Company/Site: {job.get('site') or 'N/A'}\n"
        f"Location: {job.get('location') or 'N/A'}\n\n"
        f"Description:\n{desc}"
    )


def _profile_context(profile: dict) -> str:
    """Short candidate context from profile."""
    personal = profile.get("personal", {})
    name = personal.get("preferred_name") or personal.get("full_name", "The candidate")
    resume_facts = profile.get("resume_facts", {})
    metrics = resume_facts.get("real_metrics", [])
    projects = resume_facts.get("preserved_projects", [])
    skills = profile.get("skills_boundary", {})
    flat_skills = []
    for v in skills.values() if isinstance(skills, dict) else []:
        if isinstance(v, list):
            flat_skills.extend(v)
    return (
        f"Candidate name: {name}\n"
        f"Relevant metrics/outcomes: {', '.join(metrics[:5]) if metrics else 'N/A'}\n"
        f"Projects to reference: {', '.join(projects[:5]) if projects else 'N/A'}\n"
        f"Skills/tools: {', '.join(flat_skills[:15]) if flat_skills else 'N/A'}"
    )


def _build_system_prompt(profile: dict) -> str:
    """Base system prompt for all outreach emails."""
    return """You write short, human-sounding cold emails to hiring managers after the candidate has already applied.
Rules:
- 3 to 5 sentences per email. No buzzwords. Conversational, not salesy.
- Be specific to the role and company. Reference one concrete thing from the job or company.
- Do NOT use phrases like "I'm excited to apply" or "I would love to discuss." Be direct and substantive.
- Sign off with only the candidate's first name or preferred name.
- Output format: exactly "SUBJECT: your subject line" then a blank line then "BODY:" then a blank line then the email body. No other preamble."""


def _parse_subject_body(text: str) -> tuple[str, str]:
    """Extract SUBJECT: and BODY: from LLM output."""
    subject = ""
    body = ""
    sub_match = re.search(r"SUBJECT:\s*(.+?)(?=\n\n|\nBODY:|\Z)", text, re.DOTALL | re.IGNORECASE)
    if sub_match:
        subject = sub_match.group(1).strip().split("\n")[0].strip()
    body_match = re.search(r"BODY:\s*(.+)\Z", text, re.DOTALL | re.IGNORECASE)
    if body_match:
        body = body_match.group(1).strip()
    if not body and "\n\n" in text:
        parts = text.split("\n\n", 1)
        if not subject and "subject" not in parts[0].lower():
            subject = parts[0].strip().split("\n")[0][:100]
        body = parts[-1].strip()
    return subject or "Following up on my application", body or text.strip()


def generate_email_1(job: dict, profile: dict, hm_name: str | None) -> tuple[str, str]:
    """Generate first email (Day 0): warm intro, role + value prop."""
    client = get_client()
    job_ctx = _job_context(job)
    profile_ctx = _profile_context(profile)
    greeting = f"Hi {hm_name}," if hm_name else "Hi,"
    prompt = f"""Write the FIRST email of a 3-email drip to the hiring manager.

{_build_system_prompt(profile)}

JOB:
{job_ctx}

CANDIDATE:
{profile_ctx}

TASK: Email 1 — Warm intro. Mention the specific role, one sentence on why you're a fit, one sentence on why this company. Use greeting: {greeting}
Output SUBJECT: then BODY: as specified."""

    resp = client.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=512,
    )
    return _parse_subject_body(resp)


def generate_email_2(
    job: dict,
    profile: dict,
    hm_name: str | None,
    prev_subject: str,
    prev_body: str,
) -> tuple[str, str]:
    """Generate second email (Day +2): value-add, no ask."""
    client = get_client()
    job_ctx = _job_context(job)
    profile_ctx = _profile_context(profile)
    greeting = f"Hi {hm_name}," if hm_name else "Hi,"
    prompt = f"""Write the SECOND email of a 3-email drip (follow-up 2 days later).

{_build_system_prompt(profile)}

JOB:
{job_ctx}

CANDIDATE:
{profile_ctx}

PREVIOUS EMAIL (for continuity — do not repeat, add new value):
Subject: {prev_subject}
Body:
{prev_body}

TASK: Email 2 — Value-add. Share one relevant accomplishment or insight about the company/industry. Do NOT ask for a call or reply. Keep it short. Use greeting: {greeting}
Output SUBJECT: then BODY: as specified."""

    resp = client.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=512,
    )
    return _parse_subject_body(resp)


def generate_email_3(
    job: dict,
    profile: dict,
    hm_name: str | None,
    prev_subjects: list[str],
    prev_bodies: list[str],
) -> tuple[str, str]:
    """Generate third email (Day +5): soft close, suggest call."""
    client = get_client()
    job_ctx = _job_context(job)
    profile_ctx = _profile_context(profile)
    greeting = f"Hi {hm_name}," if hm_name else "Hi,"
    prev_block = "\n\n".join(
        f"Email {i+1} — Subject: {s}\nBody: {b}"
        for i, (s, b) in enumerate(zip(prev_subjects, prev_bodies))
    )
    prompt = f"""Write the THIRD and final email of a 3-email drip (follow-up 5 days after first).

{_build_system_prompt(profile)}

JOB:
{job_ctx}

CANDIDATE:
{profile_ctx}

PREVIOUS EMAILS (for continuity — do not repeat):
{prev_block}

TASK: Email 3 — Soft close. Reiterate interest briefly, suggest a quick call if they're open to it, graceful sign-off. Use greeting: {greeting}
Output SUBJECT: then BODY: as specified."""

    resp = client.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=512,
    )
    return _parse_subject_body(resp)


def generate_sequence(job: dict, profile: dict | None = None) -> list[dict]:
    """Generate full 3-email sequence for one job.

    Returns list of {"sequence_num": 1|2|3, "subject": str, "body": str}.
    """
    profile = profile or load_profile()
    hm_name = job.get("hm_name")
    out = []
    sub1, body1 = generate_email_1(job, profile, hm_name)
    out.append({"sequence_num": 1, "subject": sub1, "body": body1})
    sub2, body2 = generate_email_2(job, profile, hm_name, sub1, body1)
    out.append({"sequence_num": 2, "subject": sub2, "body": body2})
    sub3, body3 = generate_email_3(
        job, profile, hm_name,
        [sub1, sub2], [body1, body2],
    )
    out.append({"sequence_num": 3, "subject": sub3, "body": body3})
    return out


def scheduled_times_for_sequence(applied_at_iso: str | None) -> list[str]:
    """Return list of ISO timestamps for each email (Day 0, +2, +5 from applied_at or now)."""
    if applied_at_iso:
        try:
            base = datetime.fromisoformat(applied_at_iso.replace("Z", "+00:00"))
        except Exception:
            base = datetime.now(timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    out = []
    for days in DRIP_SCHEDULE:
        t = base + timedelta(days=days)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        out.append(t.isoformat())
    return out


def run_composer_for_pending_jobs(conn, limit: int = 20) -> tuple[int, int]:
    """Generate drip sequences for jobs that have hm_email but no outreach_emails yet.

    Inserts into outreach_emails with status='draft' and scheduled_at set.
    Returns (generated_count, error_count).
    """
    from applypilot.database import get_connection

    if conn is None:
        conn = get_connection()
    profile = load_profile()
    rows = conn.execute(
        """
        SELECT j.url, j.title, j.description, j.full_description, j.site, j.location,
               j.hm_name, j.hm_email, j.applied_at
        FROM jobs j
        WHERE j.applied_at IS NOT NULL
          AND j.hm_email IS NOT NULL
          AND (j.outreach_status = 'pending' OR j.outreach_status IS NULL)
          AND NOT EXISTS (SELECT 1 FROM outreach_emails e WHERE e.job_url = j.url)
        ORDER BY j.applied_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return 0, 0
    columns = rows[0].keys()
    jobs = [dict(zip(columns, row)) for row in rows]
    generated = 0
    errors = 0
    for job in jobs:
        try:
            sequence = generate_sequence(job, profile)
            times = scheduled_times_for_sequence(job.get("applied_at"))
            for em, sched in zip(sequence, times):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO outreach_emails
                    (job_url, sequence_num, subject, body, status, scheduled_at)
                    VALUES (?, ?, ?, ?, 'draft', ?)
                    """,
                    (job["url"], em["sequence_num"], em["subject"], em["body"], sched),
                )
            conn.execute(
                "UPDATE jobs SET outreach_status = 'active' WHERE url = ?",
                (job["url"],),
            )
            generated += 1
        except Exception as e:
            log.exception("Composer failed for job %s: %s", job.get("url", "")[:60], e)
            errors += 1
    conn.commit()
    return generated, errors
