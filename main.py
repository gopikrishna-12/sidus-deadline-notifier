"""Main entry point for Sidus Portal deadline notifications."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import SESSION_FILE, STATE_FILE, Settings, load_settings, setup_logging
from notifier import (
    TelegramError,
    WhatsAppError,
    format_assessment_message,
    format_due_ist,
    format_remaining,
    notify_all,
    process_telegram_commands,
    send_plain_alert,
)
from scraper import Assessment, ScraperError, SessionExpiredError, fetch_assessments
from state import load_state, prune_old_assessments, save_state

ERROR_ALERT_COOLDOWN = timedelta(hours=6)


def _should_send_error_alert(last_sent_iso: str | None, now: datetime) -> bool:
    """Throttle repeated identical failure alerts while always sending the first."""
    if not last_sent_iso:
        return True
    try:
        last_dt = datetime.fromisoformat(last_sent_iso)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=now.tzinfo)
    return (now - last_dt) >= ERROR_ALERT_COOLDOWN


def _format_previous_iso(iso_value: str | None, tz: Any) -> str:
    """Format a stored ISO timestamp for display, falling back cleanly."""
    if not iso_value:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(str(iso_value))
        return format_due_ist(dt.astimezone(tz))
    except ValueError:
        return str(iso_value)


def _parse_iso_safe(iso_value: str | None, tz: Any) -> datetime | None:
    """Parse an ISO timestamp string into the configured timezone or return None."""
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_value))
        return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
    except ValueError:
        return None


def evaluate_assessments(
    assessments: list[Assessment],
    state: dict[str, Any],
    settings: Settings,
    now: datetime,
) -> list[tuple[str, str]]:
    """Compare fetched assessments with saved state and produce pending alerts.

    Returns a list of (kind, html_message) tuples and mutates `state` in place.
    """
    alerts: list[tuple[str, str]] = []
    stored_map: dict[str, Any] = state.setdefault("assessments", {})

    for item in assessments:
        remaining_hours = (item.due_at - now).total_seconds() / 3600.0
        if remaining_hours <= 0:
            continue

        due_iso = item.due_at.isoformat()
        opens_iso = item.opens_at.isoformat() if item.opens_at else None
        existing = stored_map.get(item.id)

        if not isinstance(existing, dict):
            msg = format_assessment_message("🆕 New Assessment", item, now)
            alerts.append(("new", msg))
            # Skip thresholds already passed at discovery time so we don't double-alert
            covered = [h for h in settings.reminder_hours if remaining_hours <= h]
            stored_map[item.id] = {
                "title": item.title,
                "course": item.course,
                "due_at": due_iso,
                "opens_at": opens_iso,
                "submissions_not_open_yet": item.submissions_not_open_yet,
                "url": item.url,
                "sent_reminders": covered,
            }
            continue

        prev_due_iso = existing.get("due_at")
        prev_opens_iso = existing.get("opens_at")
        has_prev_opens = "opens_at" in existing
        prev_locked = existing.get("submissions_not_open_yet")
        sent_reminders: list[int] = list(existing.get("sent_reminders") or [])

        due_changed = prev_due_iso != due_iso
        opens_changed = has_prev_opens and prev_opens_iso != opens_iso
        unlocked_early = prev_locked is True and not item.submissions_not_open_yet

        if due_changed:
            old_dt = _parse_iso_safe(prev_due_iso, settings.timezone)
            moved_earlier = old_dt is not None and item.due_at < old_dt
            header = "⚠️ Deadline Moved EARLIER" if moved_earlier else "📅 Deadline Extended"
            extra_parts = [
                f"<b>Previous Due:</b> {_format_previous_iso(prev_due_iso, settings.timezone)}"
            ]
            if opens_changed and item.opens_at:
                extra_parts.append(
                    f"<b>Opens At:</b> {format_due_ist(item.opens_at)} "
                    f"(was {_format_previous_iso(prev_opens_iso, settings.timezone)})"
                )
            msg = format_assessment_message(
                header,
                item,
                now,
                extra_line="\n".join(extra_parts),
            )
            alerts.append(("deadline_changed", msg))
            # Re-evaluate thresholds against the updated deadline
            sent_reminders = [h for h in settings.reminder_hours if remaining_hours <= h]
        elif opens_changed or unlocked_early:
            old_opens_dt = _parse_iso_safe(prev_opens_iso, settings.timezone)
            if opens_changed and item.opens_at:
                shifted_earlier = old_opens_dt is not None and item.opens_at < old_opens_dt
                header = (
                    "⚠️ Schedule Moved EARLIER"
                    if shifted_earlier
                    else "📅 Schedule Updated"
                )
                extra = (
                    f"<b>Opens At:</b> {format_due_ist(item.opens_at)}\n"
                    f"<b>Previous Opens:</b> {_format_previous_iso(prev_opens_iso, settings.timezone)}"
                )
            else:
                header = "🔓 Submissions Now Open"
                extra = "<i>Submissions opened earlier than previously scheduled.</i>"
            msg = format_assessment_message(header, item, now, extra_line=extra)
            alerts.append(("schedule_changed", msg))
        else:
            # Find thresholds crossed since last check that haven't been notified yet
            due_thresholds = [
                h
                for h in settings.reminder_hours
                if remaining_hours <= h and h not in sent_reminders
            ]
            if due_thresholds:
                # Notify for the most urgent crossed threshold and mark all crossed ones
                urgent_h = min(due_thresholds)
                label = "1 hour" if urgent_h == 1 else f"{urgent_h} hours"
                msg = format_assessment_message(
                    f"⏰ Deadline Reminder (< {label} left)",
                    item,
                    now,
                )
                alerts.append((f"reminder_{urgent_h}h", msg))
                for h in due_thresholds:
                    if h not in sent_reminders:
                        sent_reminders.append(h)

        stored_map[item.id] = {
            "title": item.title,
            "course": item.course,
            "due_at": due_iso,
            "opens_at": opens_iso,
            "submissions_not_open_yet": item.submissions_not_open_yet,
            "url": item.url,
            "sent_reminders": sorted(set(sent_reminders), reverse=True),
        }

    prune_old_assessments(state, now, settings.timezone)
    return alerts


def run_test_notification(settings: Settings, logger: logging.Logger) -> int:
    """Send a synthetic assessment alert to verify Telegram delivery."""
    now = datetime.now(settings.timezone)
    sample = Assessment(
        id="00000000-0000-0000-0000-000000000000",
        title="Test Notification — Sidus Deadline Bot",
        course="CS000 - System Verification",
        course_code="CS000",
        course_name="System Verification",
        method="QUIZ",
        computed_status="ACTIVE",
        due_at=now + timedelta(hours=6, minutes=15),
        url=settings.portal_url,
    )
    msg = format_assessment_message("🧪 Test Alert", sample, now)
    notify_all(settings, msg, logger=logger)
    logger.info("Sent test notification successfully.")
    return 0


def run_check(
    settings: Settings,
    logger: logging.Logger,
    dry_run: bool = False,
    session_path: Path = SESSION_FILE,
    state_path: Path = STATE_FILE,
) -> int:
    """Fetch assessments, compute diffs/reminders, and send alerts."""
    now = datetime.now(settings.timezone)
    state = load_state(state_path)

    try:
        assessments = fetch_assessments(settings, session_path=session_path, now=now)
    except SessionExpiredError as exc:
        logger.error("Session expired: %s", exc)
        if not dry_run and _should_send_error_alert(state.get("last_session_alert_at"), now):
            try:
                send_plain_alert(settings, "Session expired, run login_once.py")
                state["last_session_alert_at"] = now.isoformat()
                save_state(state, state_path)
            except TelegramError as tg_exc:
                logger.error("Failed to send session-expired alert: %s", tg_exc)
        return 1
    except ScraperError as exc:
        logger.exception("Scraper error occurred")
        if not dry_run and _should_send_error_alert(state.get("last_scraper_alert_at"), now):
            try:
                send_plain_alert(
                    settings,
                    f"⚠️ Sidus scraper error (portal layout/API may have changed): {exc}",
                )
                state["last_scraper_alert_at"] = now.isoformat()
                save_state(state, state_path)
            except TelegramError as tg_exc:
                logger.error("Failed to send scraper-error alert: %s", tg_exc)
        return 1

    # Scrape succeeded; clear any previous failure cooldowns
    state["last_session_alert_at"] = None
    state["last_scraper_alert_at"] = None

    logger.info("Fetched %d active assessment(s).", len(assessments))

    if dry_run:
        print(f"\n[DRY RUN] Active assessments ({len(assessments)} found):")
        for item in assessments:
            rem = format_remaining(item.due_at - now)
            print(
                f"  - {item.title} | {item.course} | Due: {format_due_ist(item.due_at)} "
                f"({rem} left) | {item.url}"
            )
        simulated_state = load_state(state_path)
        alerts = evaluate_assessments(assessments, simulated_state, settings, now)
        print(f"\n[DRY RUN] Alerts that would be sent ({len(alerts)}):")
        for kind, msg in alerts:
            print(f"--- [{kind}] ---\n{msg}\n")
        return 0

    alerts = evaluate_assessments(assessments, state, settings, now)
    whatsapp_enabled = bool(settings.whatsapp_phone and settings.callmebot_api_key)
    for idx, (kind, msg) in enumerate(alerts):
        try:
            notify_all(settings, msg, logger=logger)
            logger.info("Sent %s notification.", kind)
        except (TelegramError, WhatsAppError) as exc:
            logger.error("Failed to send %s notification: %s", kind, exc)
            return 1
        if whatsapp_enabled and idx + 1 < len(alerts):
            # Space consecutive CallMeBot requests slightly to reduce rate-limit drops
            time.sleep(2)

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            newest_update_id = process_telegram_commands(
                settings,
                assessments,
                int(state.get("last_update_id") or 0),
                now,
            )
            state["last_update_id"] = newest_update_id
        except TelegramError as exc:
            logger.warning("Could not process Telegram commands: %s", exc)

    save_state(state, state_path)
    logger.info("Check complete. %d alert(s) sent.", len(alerts))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Sidus Portal deadline notifier for Aurora University."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a fake test notification to Telegram and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape assessments and print pending alerts without sending or saving state.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    try:
        settings = load_settings(require_telegram=not args.dry_run)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    secrets = [
        s
        for s in (settings.telegram_bot_token, settings.callmebot_api_key)
        if s
    ]
    logger = setup_logging(secrets=secrets, verbose=args.verbose)

    if args.test:
        try:
            return run_test_notification(settings, logger)
        except (TelegramError, WhatsAppError) as exc:
            logger.error("Test notification failed: %s", exc)
            return 1

    return run_check(settings, logger, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

