"""Hiring manager email discovery: Hunter.io API, pattern guessing, SMTP verification."""

import logging
import re
import smtplib
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx  # pyright: ignore[reportMissingImports]

log = logging.getLogger(__name__)

# Subdomains to strip when deriving company domain from job URL (order matters for strip)
_COMMON_SUBDOMAINS = ("www.", "careers.", "jobs.", "apply.", "talent.", "recruiting.", "hire.")

# Hunter.io departments/titles that look like hiring decision-makers
_HUNTER_RELEVANT = (
    "recruit", "talent", "hr", "human resource", "people", "hiring",
    "manager", "director", "lead", "head of", "recruiter",
)


def _domain_from_url(url: str | None) -> str | None:
    """Extract company domain from job URL (e.g. careers.stripe.com -> stripe.com)."""
    if not url or not url.strip():
        return None
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        netloc = (parsed.netloc or url).lower().strip()
        if not netloc:
            return None
        for prefix in _COMMON_SUBDOMAINS:
            if netloc.startswith(prefix):
                netloc = netloc[len(prefix) :]
                break
        # netloc may now be "stripe.com" or "company.co.uk"
        if "." in netloc and len(netloc) > 3:
            return netloc
        return None
    except Exception:
        return None


def _hunter_domain_search(domain: str, api_key: str, limit: int = 10) -> list[dict]:
    """Call Hunter.io domain-search API; return list of email records."""
    try:
        resp = httpx.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "limit": limit, "api_key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        emails = (data.get("data") or {}).get("emails") or []
        return list(emails)
    except Exception as e:
        log.debug("Hunter domain-search failed for %s: %s", domain, e)
        return []


def _pick_best_hunter_email(emails: list[dict]) -> dict | None:
    """Choose one email from Hunter results (prefer recruiting/hiring roles)."""
    if not emails:
        return None
    scored = []
    for e in emails:
        pos = (e.get("position") or "").lower()
        conf = e.get("confidence", 0) or 0
        score = 0
        if any(kw in pos for kw in _HUNTER_RELEVANT):
            score += 10
        score += min(conf // 10, 5)
        scored.append((score, e))
    scored.sort(key=lambda x: (-x[0], -x[1].get("confidence", 0)))
    return scored[0][1] if scored else None


def _smtp_verify(email: str, domain: str) -> bool:
    """Verify mailbox exists via SMTP RCPT TO (best-effort; many servers reject verification)."""
    if not email or "@" not in email or not domain:
        return False
    import socket
    for mx_candidate in (f"mail.{domain}", domain, f"smtp.{domain}"):
        try:
            with smtplib.SMTP(timeout=10) as smtp:
                smtp.set_debuglevel(0)
                smtp.connect(mx_candidate, 25)
                smtp.helo("applypilot.local")
                smtp.mail("")
                code, _ = smtp.rcpt(email)
                if 250 <= code < 260:
                    return True
        except (socket.gaierror, OSError, smtplib.SMTPException):
            continue
    return False


def _name_from_description(description: str | None) -> tuple[str | None, str | None]:
    """Heuristic: try to extract a contact/hiring manager name from job description."""
    if not description:
        return None, None
    text = (description or "").lower()
    # "contact: Jane Doe", "hiring manager: John Smith", "reach out to Alice"
    for pattern in (
        r"contact\s*[:\-]\s*([A-Za-z]+\s+[A-Za-z]+)",
        r"hiring\s+manager\s*[:\-]\s*([A-Za-z]+\s+[A-Za-z]+)",
        r"reach\s+out\s+to\s+([A-Za-z]+\s+[A-Za-z]+)",
        r"email\s+([A-Za-z]+\s+[A-Za-z]+)\s+at",
        r"([A-Za-z]+)\s+([A-Za-z]+)\s+[\(\[]?(?:recruit|hiring|talent|hr)",
    ):
        m = re.search(pattern, description, re.IGNORECASE)
        if m:
            g = m.groups()
            if len(g) >= 2:
                return g[0].strip(), g[1].strip()
            if len(g) == 1:
                parts = g[0].strip().split()
                if len(parts) >= 2:
                    return parts[0], parts[-1]
    return None, None


def _pattern_guess_emails(domain: str, first_name: str, last_name: str) -> list[str]:
    """Generate common email patterns for first_name + last_name at domain."""
    f = (first_name or "").lower().strip()
    l = (last_name or "").lower().strip()
    if not f and not l:
        return []
    if not f:
        f = l
    if not l:
        l = f
    candidates = [
        f"{f}.{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{f}@{domain}",
        f"{f[0]}{l}@{domain}" if f and l else None,
        f"{f}{l[0]}@{domain}" if f and l else None,
    ]
    return [c for c in candidates if c and "@" in c]


def find_hiring_manager_email(
    job: dict,
    *,
    hunter_api_key: str | None = None,
    verify_smtp: bool = True,
) -> dict | None:
    """Try to find a hiring manager email for this job.

    Uses Hunter.io if api_key is set, then pattern guessing + SMTP verification.
    Job dict should have: url, title, (optional) full_description, site.

    Returns:
        {"name": str, "email": str, "source": "hunter"|"pattern"|"manual"} or None if not found.
    """
    url = job.get("url") or ""
    domain = _domain_from_url(url)
    if not domain:
        log.debug("No domain from job url: %s", url[:80])
        return None

    # 1) Hunter.io
    if hunter_api_key and domain:
        emails = _hunter_domain_search(domain, hunter_api_key)
        best = _pick_best_hunter_email(emails)
        if best:
            email = best.get("value")
            if email:
                first = best.get("first_name") or ""
                last = best.get("last_name") or ""
                name = " ".join((first, last)).strip() or None
                return {"name": name, "email": email, "source": "hunter"}

    # 2) Pattern guessing from name in description
    first, last = _name_from_description(job.get("full_description") or job.get("description"))
    if first or last:
        candidates = _pattern_guess_emails(domain, first or "", last or "")
        for candidate in candidates:
            if verify_smtp and not _smtp_verify(candidate, domain):
                continue
            name = " ".join((first or "", last or "")).strip() or None
            return {"name": name, "email": candidate, "source": "pattern"}

    return None


def run_finder_for_applied_jobs(
    conn,
    *,
    hunter_api_key: str | None = None,
    limit: int = 50,
) -> tuple[int, int]:
    """Find hiring manager emails for applied jobs that don't have one yet.

    Updates jobs with hm_name, hm_email, hm_email_source, hm_found_at, outreach_status.

    Returns:
        (found_count, skipped_count)
    """
    from applypilot.database import get_connection

    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        """
        SELECT url, title, description, full_description, site
        FROM jobs
        WHERE applied_at IS NOT NULL
          AND hm_email IS NULL
          AND (outreach_status IS NULL OR outreach_status = 'pending')
        ORDER BY applied_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return 0, 0
    columns = rows[0].keys()
    jobs = [dict(zip(columns, row)) for row in rows]
    now = datetime.now(timezone.utc).isoformat()
    found = 0
    for job in jobs:
        result = find_hiring_manager_email(job, hunter_api_key=hunter_api_key, verify_smtp=True)
        if result:
            conn.execute(
                """
                UPDATE jobs
                SET hm_name = ?, hm_email = ?, hm_email_source = ?, hm_found_at = ?, outreach_status = 'pending'
                WHERE url = ?
                """,
                (result.get("name"), result["email"], result["source"], now, job["url"]),
            )
            found += 1
        # If not found, leave hm_email NULL and outreach_status NULL so user can set manually
    conn.commit()
    return found, len(jobs) - found
