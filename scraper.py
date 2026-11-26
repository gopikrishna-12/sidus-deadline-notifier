"""Fetch and filter active Sidus Portal assessments using saved session cookies."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import SESSION_FILE, Settings

ASSESSMENTS_ENDPOINT = "/api/v1/student-portal/me/assessments"
DONE_COMPUTED_STATUSES = {"SUBMITTED", "PUBLISHED"}
COMPLETED_ATTEMPT_STATUSES = {"SUBMITTED", "COMPLETED", "GRADED", "EVALUATED"}


class SessionExpiredError(RuntimeError):
    """Raised when session.json is missing, invalid, or rejected by the server."""


class ScraperError(RuntimeError):
    """Raised when the portal API returns unexpected data or fails."""


@dataclass(frozen=True)
class Assessment:
    """Normalized assessment record returned by the scraper."""

    id: str
    title: str
    course: str
    course_code: str
    course_name: str
    method: str
    computed_status: str
    due_at: datetime
    url: str
    is_extension: bool = False
    submissions_not_open_yet: bool = False
    opens_at: datetime | None = None


def parse_iso_datetime(value: str | None, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO-8601 timestamp string into the configured timezone."""
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz)


def build_assessment_url(portal_origin: str, assessment_id: str, method: str) -> str:
    """Construct the direct portal URL for a given assessment method."""
    if method == "QUIZ":
        return f"{portal_origin}/modules/assessments/assessments/{assessment_id}/quiz"
    if method == "REFLECTIVE_JOURNAL":
        return f"{portal_origin}/modules/reflective-journal/{assessment_id}"
    return f"{portal_origin}/modules/assessments/assessments"


def _load_cookies_from_session(session_path: Path) -> list[dict[str, Any]]:
    """Load cookies from Playwright storage_state JSON without logging values."""
    if not session_path.exists():
        raise SessionExpiredError("Session file missing, run login_once.py")
    try:
        raw = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionExpiredError("Session file unreadable, run login_once.py") from exc

    cookies = raw.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise SessionExpiredError("Session has no cookies, run login_once.py")

    has_auth_token = any(
        isinstance(c, dict) and c.get("name") == "token" and c.get("value")
        for c in cookies
    )
    if not has_auth_token:
        raise SessionExpiredError("Session missing auth token, run login_once.py")

    return cookies


def _build_http_session(cookies: list[dict[str, Any]], portal_origin: str) -> requests.Session:
    """Create a requests.Session preloaded with cookies from session.json."""
    http = requests.Session()
    http.headers.update(
        {
            "Accept": "application/json",
            "Origin": portal_origin,
            "Referer": f"{portal_origin}/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
        }
    )
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        path = cookie.get("path", "/")
        if name and value is not None and domain:
            http.cookies.set(name, value, domain=domain, path=path)
    return http


def _is_already_submitted(item: dict[str, Any]) -> bool:
    """Return True if the assessment has already been submitted or closed."""
    computed_status = str(item.get("computedStatus") or "").upper()
    if computed_status in DONE_COMPUTED_STATUSES:
        return True
    if item.get("notSubmitted") is True:
        return True

    submission = item.get("submission")
    if isinstance(submission, dict):
        if submission.get("submittedAt"):
            return True
        sub_status = str(submission.get("status") or "").upper()
        if sub_status in COMPLETED_ATTEMPT_STATUSES:
            return True

    quiz_attempt = item.get("quizAttempt")
    if isinstance(quiz_attempt, dict):
        if quiz_attempt.get("submittedAt"):
            return True
        attempt_status = str(quiz_attempt.get("status") or "").upper()
        if attempt_status in COMPLETED_ATTEMPT_STATUSES:
            return True

    return False


def parse_assessments_payload(
    payload: Any,
    settings: Settings,
    now: datetime | None = None,
    window_hours: float | None = None,
) -> list[Assessment]:
    """Validate and filter the JSON payload from the Sidus assessments endpoint."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ScraperError("Unexpected API response: missing success=true")

    items = payload.get("data")
    if not isinstance(items, list):
        raise ScraperError("Unexpected API response: 'data' is not a list")

    current_time = now.astimezone(settings.timezone) if now else datetime.now(settings.timezone)
    max_delta = timedelta(hours=window_hours) if window_hours is not None else None

    results: list[Assessment] = []
    for item in items:
        if not isinstance(item, dict):
            raise ScraperError("Unexpected API response: assessment entry is not an object")

        assessment_id = item.get("id")
        title = item.get("title")
        if not assessment_id or not title:
            raise ScraperError("Unexpected API response: assessment missing id or title")

        computed_status = str(item.get("computedStatus") or "").upper()
        # Only track ACTIVE assessments that are not already submitted/missed
        if computed_status != "ACTIVE":
            continue
        if _is_already_submitted(item):
            continue

        is_extension = bool(item.get("isExtension")) and bool(item.get("extensionDueDate"))
        due_raw = item.get("extensionDueDate") if is_extension else item.get("dueDate")
        if not due_raw:
            continue

        due_at = parse_iso_datetime(str(due_raw), settings.timezone)
        if due_at is None:
            continue

        remaining = due_at - current_time
        # Ignore past-due assessments and optionally enforce an upper deadline window
        if remaining <= timedelta(0):
            continue
        if max_delta is not None and remaining >= max_delta:
            continue

        subject = item.get("subject") or {}
        if not isinstance(subject, dict):
            subject = {}
        course_code = str(subject.get("code") or "").strip()
        course_name = str(subject.get("name") or "").strip()
        if course_code and course_name:
            course = f"{course_code} - {course_name}"
        else:
            course = course_name or course_code or "Unknown Course"

        method = str(item.get("method") or "").upper()
        opens_at = parse_iso_datetime(
            item.get("opensAt") or item.get("quizStartTime"),
            settings.timezone,
        )

        results.append(
            Assessment(
                id=str(assessment_id),
                title=str(title).strip(),
                course=course,
                course_code=course_code,
                course_name=course_name,
                method=method,
                computed_status=computed_status,
                due_at=due_at,
                url=build_assessment_url(settings.portal_origin, str(assessment_id), method),
                is_extension=is_extension,
                submissions_not_open_yet=bool(item.get("submissionsNotOpenYet")),
                opens_at=opens_at,
            )
        )

    results.sort(key=lambda a: a.due_at)
    return results


def fetch_assessments(
    settings: Settings,
    session_path: Path = SESSION_FILE,
    now: datetime | None = None,
    window_hours: float | None = None,
) -> list[Assessment]:
    """Fetch active, unsubmitted, non-past-due assessments from Sidus API."""
    cookies = _load_cookies_from_session(session_path)
    http = _build_http_session(cookies, settings.portal_origin)
    url = f"{settings.api_base_url}{ASSESSMENTS_ENDPOINT}"

    try:
        response = http.get(url, timeout=20)
    except requests.RequestException as exc:
        raise ScraperError(f"Network error reaching Sidus API: {exc}") from exc

    if response.status_code in (401, 403):
        raise SessionExpiredError("Session expired, run login_once.py")

    if response.status_code != 200:
        raise ScraperError(
            f"Unexpected HTTP {response.status_code} from Sidus assessments API"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ScraperError("Sidus API returned non-JSON response (portal may have changed)") from exc

    return parse_assessments_payload(payload, settings, now=now, window_hours=window_hours)

