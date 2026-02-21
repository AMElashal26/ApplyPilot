"""Gmail SMTP sending for outreach drip emails with rate limiting."""

import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

log = logging.getLogger(__name__)

# Minimum seconds between sends to avoid spam flags
RATE_LIMIT_SECONDS = 30


def _get_smtp_connection(gmail_address: str, app_password: str) -> smtplib.SMTP:
    """Connect to Gmail SMTP and return authenticated connection."""
    smtp = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
    smtp.starttls()
    smtp.login(gmail_address, app_password)
    return smtp


def send_one_email(
    *,
    from_addr: str,
    from_name: str | None,
    to_email: str,
    subject: str,
    body: str,
    app_password: str,
) -> tuple[bool, str | None]:
    """Send a single plain-text email via Gmail SMTP.

    Returns:
        (success, error_message). error_message is None on success.
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((from_name or "ApplyPilot User", from_addr))
        msg["To"] = to_email
        msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
        msg.attach(MIMEText(body, "plain", "utf-8"))

        smtp = _get_smtp_connection(from_addr, app_password)
        try:
            smtp.sendmail(from_addr, [to_email], msg.as_string())
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return True, None
    except Exception as e:
        log.exception("Send failed to %s: %s", to_email, e)
        return False, str(e)


def run_send_due_emails(
    conn,
    *,
    gmail_address: str,
    gmail_app_password: str,
    from_name: str | None = None,
    limit: int = 20,
) -> tuple[int, int]:
    """Send all due outreach emails (scheduled_at <= now, status draft/scheduled).

    Uses jobs.hm_email as recipient. Rate-limits by RATE_LIMIT_SECONDS between sends.
    Returns (sent_count, failed_count).
    """
    from applypilot.database import get_connection

    if conn is None:
        conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """
        SELECT e.id, e.job_url, e.sequence_num, e.subject, e.body, e.status
        FROM outreach_emails e
        JOIN jobs j ON j.url = e.job_url
        WHERE e.status IN ('draft', 'scheduled')
          AND e.scheduled_at <= ?
          AND j.hm_email IS NOT NULL
        ORDER BY e.scheduled_at ASC
        LIMIT ?
        """,
        (now_iso, limit),
    ).fetchall()
    if not rows:
        return 0, 0
    columns = rows[0].keys()
    to_send = [dict(zip(columns, row)) for row in rows]
    sent = 0
    failed = 0
    for i, row in enumerate(to_send):
        if i > 0:
            time.sleep(RATE_LIMIT_SECONDS)
        job_url = row["job_url"]
        j = conn.execute(
            "SELECT hm_email, hm_name FROM jobs WHERE url = ?", (job_url,)
        ).fetchone()
        if not j or not j[0]:
            conn.execute(
                "UPDATE outreach_emails SET status = 'failed', error = ? WHERE id = ?",
                ("No hm_email on job", row["id"]),
            )
            failed += 1
            conn.commit()
            continue
        to_email = j[0]
        ok, err = send_one_email(
            from_addr=gmail_address,
            from_name=from_name,
            to_email=to_email,
            subject=row["subject"] or "",
            body=row["body"] or "",
            app_password=gmail_app_password,
        )
        now = datetime.now(timezone.utc).isoformat()
        if ok:
            conn.execute(
                "UPDATE outreach_emails SET status = 'sent', sent_at = ?, error = NULL WHERE id = ?",
                (now, row["id"]),
            )
            sent += 1
            # If all 3 emails for this job are sent, mark job outreach as completed
            count = conn.execute(
                "SELECT COUNT(*) FROM outreach_emails WHERE job_url = ? AND status = 'sent'",
                (job_url,),
            ).fetchone()[0]
            if count >= 3:
                conn.execute(
                    "UPDATE jobs SET outreach_status = 'completed' WHERE url = ?",
                    (job_url,),
                )
        else:
            conn.execute(
                "UPDATE outreach_emails SET status = 'failed', error = ? WHERE id = ?",
                (err or "Unknown error", row["id"]),
            )
            failed += 1
        conn.commit()
    return sent, failed
