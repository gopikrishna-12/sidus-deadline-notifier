"""Main entry point for Sidus Portal deadline notifications."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import SESSION_FILE, STATE_FILE, Settings, load_settings, setup_logging
from notifier import (
    TelegramError,
    format_assessment_message,
    format_due_ist,
    format_remaining,
    process_telegram_commands,
    send_message,
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
                "url": item.url,
                "sent_reminders": covered,
            }
            continue

        prev_due_iso = existing.get("due_at")
        sent_reminders: list[int] = list(existing.get("sent_reminders") or [])

        if prev_due_iso != due_iso:
            old_due_display = prev_due_iso or "Unknown"
            try:
                old_dt = datetime.fromisoformat(str(prev_due_iso))
                old_due_display = format_due_ist(old_dt.astimezone(settings.timezone))
            except ValueError:
                pass
            msg = format_assessment_message(
                "📅 Deadline Changed",
                item,
                now,
                extra_line=f"<b>Previous Due:</b> {old_due_display}",
            )
            alerts.append(("deadline_changed", msg))
            # Re-evaluate thresholds against the updated deadline
            sent_reminders = [h for h in settings.reminder_hours if remaining_hours <= h]
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
    send_message(settings, msg)
    logger.info("Sent test Telegram notification successfully.")
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
    for kind, msg in alerts:
        try:
            send_message(settings, msg)
            logger.info("Sent %s notification.", kind)
        except TelegramError as exc:
            logger.error("Failed to send %s notification: %s", kind, exc)
            return 1

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

    secrets = [settings.telegram_bot_token] if settings.telegram_bot_token else []
    logger = setup_logging(secrets=secrets, verbose=args.verbose)

    if args.test:
        try:
            return run_test_notification(settings, logger)
        except TelegramError as exc:
            logger.error("Test notification failed: %s", exc)
            return 1

    return run_check(settings, logger, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

