"""Telegram Bot API notifier and lightweight command handler."""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta
from typing import Any

import requests

from config import Settings
from scraper import Assessment

TELEGRAM_API_BASE = "https://api.telegram.org"

METHOD_LABELS = {
    "QUIZ": "Quiz",
    "FILE_UPLOAD": "File Upload",
    "REFLECTIVE_JOURNAL": "Reflective Journal",
    "SIMPLE_ASSESSMENT": "Assignment",
}


class TelegramError(RuntimeError):
    """Raised when sending or polling Telegram messages fails."""


def format_remaining(delta: timedelta) -> str:
    """Format a positive timedelta into a readable countdown string."""
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def format_due_ist(dt: datetime) -> str:
    """Format a timezone-aware datetime concisely for Telegram messages."""
    return dt.strftime("%d %b %Y, %I:%M %p %Z")


def format_assessment_message(
    header: str,
    assessment: Assessment,
    now: datetime,
    extra_line: str | None = None,
) -> str:
    """Build an HTML-formatted Telegram notification for an assessment."""
    remaining = format_remaining(assessment.due_at - now)
    method_label = METHOD_LABELS.get(assessment.method, assessment.method.title() or "Assessment")

    lines = [
        f"<b>{html.escape(header)}</b>",
        f"<b>Title:</b> {html.escape(assessment.title)} ({html.escape(method_label)})",
        f"<b>Course:</b> {html.escape(assessment.course)}",
        f"<b>Due:</b> {html.escape(format_due_ist(assessment.due_at))}",
        f"<b>Time Left:</b> {html.escape(remaining)}",
    ]
    if extra_line:
        lines.append(extra_line)
    if assessment.is_extension:
        lines.append("<i>Note: Extended deadline applied</i>")
    if assessment.submissions_not_open_yet and assessment.opens_at:
        lines.append(
            f"<i>Submissions open: {html.escape(format_due_ist(assessment.opens_at))}</i>"
        )
    lines.append(f'<a href="{html.escape(assessment.url, quote=True)}">Open Assessment</a>')
    return "\n".join(lines)


def _redact_token(text: str, token: str) -> str:
    """Ensure bot token never leaks into raised exceptions or logs."""
    return text.replace(token, "[REDACTED]") if token else text


def send_message(settings: Settings, text: str, disable_preview: bool = True) -> None:
    """Send an HTML-formatted message to the configured Telegram chat."""
    url = f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        safe_msg = _redact_token(str(exc), settings.telegram_bot_token)
        raise TelegramError(f"Telegram request failed: {safe_msg}") from None

    if response.status_code != 200:
        raise TelegramError(
            f"Telegram API error HTTP {response.status_code}: {response.text[:300]}"
        )


def send_plain_alert(settings: Settings, text: str) -> None:
    """Send a plain text system/error alert wrapped safely in HTML."""
    send_message(settings, html.escape(text))


def format_assessment_list(
    heading: str,
    assessments: list[Assessment],
    now: datetime,
    empty_message: str,
) -> str:
    """Format a summary list of assessments for /today and /upcoming commands."""
    if not assessments:
        return f"<b>{html.escape(heading)}</b>\n{html.escape(empty_message)}"

    lines = [f"<b>{html.escape(heading)}</b> ({len(assessments)})"]
    for idx, item in enumerate(assessments, start=1):
        rem = format_remaining(item.due_at - now)
        lines.append(
            f"\n{idx}. <b>{html.escape(item.title)}</b> — {html.escape(item.course)}\n"
            f"   Due: {html.escape(format_due_ist(item.due_at))} ({html.escape(rem)} left)\n"
            f'   <a href="{html.escape(item.url, quote=True)}">Open</a>'
        )
    return "\n".join(lines)


def process_telegram_commands(
    settings: Settings,
    assessments: list[Assessment],
    last_update_id: int,
    now: datetime,
) -> int:
    """Check recent Telegram messages for /today or /upcoming and reply if found."""
    url = f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/getUpdates"
    params: dict[str, Any] = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }
    if last_update_id > 0:
        params["offset"] = last_update_id + 1

    try:
        response = requests.get(url, params=params, timeout=10)
    except requests.RequestException as exc:
        safe_msg = _redact_token(str(exc), settings.telegram_bot_token)
        raise TelegramError(f"Telegram getUpdates failed: {safe_msg}") from None

    if response.status_code == 409:
        # Telegram returns 409 Conflict when a webhook (e.g. Cloudflare Worker) is active
        return last_update_id

    if response.status_code != 200:
        raise TelegramError(f"Telegram getUpdates HTTP {response.status_code}")

    data = response.json()
    updates = data.get("result", [])
    newest_id = last_update_id

    for update in updates:
        update_id = int(update.get("update_id", 0))
        if update_id > newest_id:
            newest_id = update_id

        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id != str(settings.telegram_chat_id):
            continue

        text = str(message.get("text") or "").strip().split("@")[0].lower()
        if text == "/today":
            today_date = now.date()
            due_today = [a for a in assessments if a.due_at.date() == today_date]
            reply = format_assessment_list(
                "📅 Due Today",
                due_today,
                now,
                "No active assessments due today.",
            )
            send_message(settings, reply)
        elif text == "/upcoming":
            reply = format_assessment_list(
                f"📋 Upcoming Active Assessments (< {int(settings.deadline_window_hours)}h)",
                assessments,
                now,
                "No active assessments due soon.",
            )
            send_message(settings, reply)

    if newest_id > last_update_id:
        try:
            requests.get(
                url,
                params={"timeout": 0, "offset": newest_id + 1},
                timeout=10,
            )
        except requests.RequestException:
            pass

    return newest_id

