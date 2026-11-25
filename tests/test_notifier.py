"""Unit tests for Sidus deadline scraper, state diffing, and notifications."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from config import Settings
from main import evaluate_assessments, run_check
from notifier import format_assessment_message, process_telegram_commands
from scraper import SessionExpiredError, parse_assessments_payload
from state import default_state, load_state, save_state

IST = ZoneInfo("Asia/Kolkata")


def make_settings() -> Settings:
    return Settings(
        telegram_bot_token="12345:FAKE_TOKEN",
        telegram_chat_id="999999",
        portal_url="https://www.sidus.net/dashboard",
        portal_origin="https://www.sidus.net",
        api_base_url="https://server.sidus.net",
        deadline_window_hours=48.0,
        reminder_hours=(24, 6, 3, 2, 1),
        timezone_name="Asia/Kolkata",
        timezone=IST,
    )


def make_raw_item(
    assessment_id: str = "a1",
    title: str = "Lab Quiz 1",
    computed_status: str = "ACTIVE",
    due_utc: str = "2026-11-26T18:29:00.000Z",
    method: str = "QUIZ",
    is_extension: bool = False,
    extension_due_utc: str | None = None,
    not_submitted: bool = False,
    submission: dict | None = None,
) -> dict:
    return {
        "id": assessment_id,
        "title": title,
        "method": method,
        "computedStatus": computed_status,
        "dueDate": due_utc,
        "isExtension": is_extension,
        "extensionDueDate": extension_due_utc,
        "notSubmitted": not_submitted,
        "submission": submission,
        "quizAttempt": None,
        "submissionsNotOpenYet": False,
        "opensAt": "2026-11-25T03:30:00.000Z",
        "subject": {"id": "s1", "code": "CS209", "name": "Advanced Data Structures"},
    }


class ScraperFilteringTests(unittest.TestCase):
    def test_filters_only_active_within_48_hours(self) -> None:
        settings = make_settings()
        now = datetime(2026, 11, 25, 12, 0, tzinfo=IST)

        payload = {
            "success": True,
            "data": [
                # 1. Active, due in ~36h -> INCLUDED
                make_raw_item("keep-1", "Quiz Due Soon", due_utc="2026-11-26T18:29:00.000Z"),
                # 2. Active, due in > 48h -> EXCLUDED
                make_raw_item("skip-far", "Too Far", due_utc="2026-11-29T18:29:00.000Z"),
                # 3. Past due -> EXCLUDED
                make_raw_item("skip-past", "Past Due", due_utc="2026-11-25T05:00:00.000Z"),
                # 4. UPCOMING status even if close -> EXCLUDED
                make_raw_item(
                    "skip-upcoming",
                    "Upcoming Only",
                    computed_status="UPCOMING",
                    due_utc="2026-11-26T12:00:00.000Z",
                ),
                # 5. SUBMITTED status -> EXCLUDED
                make_raw_item(
                    "skip-submitted",
                    "Already Done",
                    computed_status="SUBMITTED",
                    due_utc="2026-11-26T12:00:00.000Z",
                ),
                # 6. Extension moves deadline inside 48h -> INCLUDED with extensionDueDate
                make_raw_item(
                    "keep-ext",
                    "Extended Report",
                    method="FILE_UPLOAD",
                    due_utc="2026-11-24T18:29:00.000Z",
                    is_extension=True,
                    extension_due_utc="2026-11-26T10:00:00.000Z",
                ),
            ],
        }

        results = parse_assessments_payload(payload, settings, now=now)
        self.assertEqual([a.id for a in results], ["keep-ext", "keep-1"])
        self.assertEqual(
            results[1].url,
            "https://www.sidus.net/modules/assessments/assessments/keep-1/quiz",
        )
        self.assertTrue(results[0].is_extension)


class StateAndReminderTests(unittest.TestCase):
    def test_new_deadline_change_and_non_duplicate_reminders(self) -> None:
        settings = make_settings()
        state = default_state()
        t0 = datetime(2026, 11, 25, 12, 0, tzinfo=IST)
        due = datetime(2026, 11, 26, 15, 0, tzinfo=IST)  # 27h away

        payload = {
            "success": True,
            "data": [make_raw_item("a1", "Lab Quiz 1", due_utc=due.astimezone(ZoneInfo("UTC")).isoformat())],
        }
        items = parse_assessments_payload(payload, settings, now=t0)

        # First seen at 27h remaining -> only New Assessment
        alerts = evaluate_assessments(items, state, settings, t0)
        self.assertEqual([k for k, _ in alerts], ["new"])

        # At 23.5h remaining -> 24h reminder fires once
        t1 = due - timedelta(hours=23, minutes=30)
        alerts = evaluate_assessments(items, state, settings, t1)
        self.assertEqual([k for k, _ in alerts], ["reminder_24h"])

        # Re-running at 23h remaining -> no duplicate 24h alert
        t2 = due - timedelta(hours=23)
        alerts = evaluate_assessments(items, state, settings, t2)
        self.assertEqual(alerts, [])

        # Missed 6h & 3h window, now at 2.5h remaining -> fires single urgent 3h alert and marks 6h+3h
        t3 = due - timedelta(hours=2, minutes=30)
        alerts = evaluate_assessments(items, state, settings, t3)
        self.assertEqual([k for k, _ in alerts], ["reminder_3h"])
        self.assertEqual(sorted(state["assessments"]["a1"]["sent_reminders"]), [3, 6, 24])

        # Deadline extended by 12 hours -> fires deadline_changed and resets future reminders
        new_due = due + timedelta(hours=12)
        payload2 = {
            "success": True,
            "data": [make_raw_item("a1", "Lab Quiz 1", due_utc=new_due.astimezone(ZoneInfo("UTC")).isoformat())],
        }
        items2 = parse_assessments_payload(payload2, settings, now=t3)
        alerts = evaluate_assessments(items2, state, settings, t3)
        self.assertEqual([k for k, _ in alerts], ["deadline_changed"])
        self.assertEqual(state["assessments"]["a1"]["sent_reminders"], [24])

    def test_html_escaping_in_message(self) -> None:
        settings = make_settings()
        now = datetime(2026, 11, 25, 12, 0, tzinfo=IST)
        payload = {
            "success": True,
            "data": [make_raw_item("a1", "C++ <Templates> & Pointers", due_utc="2026-11-26T12:00:00.000Z")],
        }
        item = parse_assessments_payload(payload, settings, now=now)[0]
        msg = format_assessment_message("🆕 New Assessment", item, now)
        self.assertIn("C++ &lt;Templates&gt; &amp; Pointers", msg)

    def test_session_expired_sends_telegram_alert(self) -> None:
        settings = make_settings()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            session_path = Path(tmp) / "missing_session.json"
            sent: list[str] = []

            with patch("main.send_plain_alert", side_effect=lambda _s, text: sent.append(text)):
                import logging

                rc = run_check(
                    settings,
                    logging.getLogger("test"),
                    dry_run=False,
                    session_path=session_path,
                    state_path=state_path,
                )
            self.assertEqual(rc, 1)
            self.assertEqual(sent, ["Session expired, run login_once.py"])
            saved = load_state(state_path)
            self.assertIsNotNone(saved["last_session_alert_at"])

    def test_webhook_409_skips_getupdates_cleanly(self) -> None:
        settings = make_settings()
        now = datetime(2026, 11, 25, 12, 0, tzinfo=IST)
        fake_resp = Mock(status_code=409)
        with patch("notifier.requests.get", return_value=fake_resp):
            newest = process_telegram_commands(settings, [], 42, now)
        self.assertEqual(newest, 42)


if __name__ == "__main__":
    unittest.main()

