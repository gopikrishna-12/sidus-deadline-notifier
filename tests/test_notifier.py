"""Unit tests for Sidus deadline scraper, state diffing, and notifications."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from config import Settings
from main import evaluate_assessments, run_check
from notifier import (
    WhatsAppError,
    format_assessment_message,
    html_to_whatsapp_text,
    notify_all,
    process_telegram_commands,
    send_whatsapp_message,
)
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
    def test_filters_active_unsubmitted_and_optional_window(self) -> None:
        settings = make_settings()
        now = datetime(2026, 11, 25, 12, 0, tzinfo=IST)

        payload = {
            "success": True,
            "data": [
                # 1. Active, due in ~36h -> INCLUDED
                make_raw_item("keep-1", "Quiz Due Soon", due_utc="2026-11-26T18:29:00.000Z"),
                # 2. Active, due in ~7 days -> INCLUDED when window_hours=None
                make_raw_item("keep-7d", "Assignment 1", due_utc="2026-12-02T18:29:00.000Z"),
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
                # 6. Extension moves deadline into future -> INCLUDED with extensionDueDate
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

        all_active = parse_assessments_payload(payload, settings, now=now)
        self.assertEqual([a.id for a in all_active], ["keep-ext", "keep-1", "keep-7d"])
        self.assertEqual(
            all_active[1].url,
            "https://www.sidus.net/modules/assessments/assessments/keep-1/quiz",
        )
        self.assertTrue(all_active[0].is_extension)

        within_48h = parse_assessments_payload(payload, settings, now=now, window_hours=48.0)
        self.assertEqual([a.id for a in within_48h], ["keep-ext", "keep-1"])


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

    def test_seven_day_active_alerts_immediately_without_early_reminders(self) -> None:
        settings = make_settings()
        state = default_state()
        t0 = datetime(2026, 11, 25, 12, 0, tzinfo=IST)
        due_7d = t0 + timedelta(days=7)

        payload = {
            "success": True,
            "data": [
                make_raw_item(
                    "a7",
                    "Assignment 1",
                    due_utc=due_7d.astimezone(ZoneInfo("UTC")).isoformat(),
                )
            ],
        }
        items = parse_assessments_payload(payload, settings, now=t0)
        alerts = evaluate_assessments(items, state, settings, t0)
        self.assertEqual([k for k, _ in alerts], ["new"])
        self.assertEqual(state["assessments"]["a7"]["sent_reminders"], [])

        # Two days later (5 days left) -> still no reminder yet
        t_5d = t0 + timedelta(days=2)
        alerts = evaluate_assessments(items, state, settings, t_5d)
        self.assertEqual(alerts, [])

    def test_whatsapp_formatting_and_fallback_isolation(self) -> None:
        raw_html = (
            '<b>🆕 New Assessment</b>\n'
            '<b>Title:</b> C++ &amp; Pointers\n'
            '<i>Submissions open later</i>\n'
            '<a href="https://www.sidus.net/quiz">Open Assessment</a>'
        )
        wa_text = html_to_whatsapp_text(raw_html)
        self.assertIn("*🆕 New Assessment*", wa_text)
        self.assertIn("C++ & Pointers", wa_text)
        self.assertIn("_Submissions open later_", wa_text)
        self.assertIn("Open Assessment: https://www.sidus.net/quiz", wa_text)

        settings = replace(
            make_settings(),
            whatsapp_phone="+919999999999",
            callmebot_api_key="SECRET_CB_KEY",
        )
        with (
            patch("notifier.send_message") as mock_tg,
            patch("notifier.send_whatsapp_message", side_effect=WhatsAppError("down")),
        ):
            notify_all(settings, "<b>Hello</b>")
            mock_tg.assert_called_once()

        import requests as req_lib

        with patch(
            "notifier.requests.get",
            side_effect=req_lib.RequestException("failed key=SECRET_CB_KEY"),
        ):
            with self.assertRaises(WhatsAppError) as ctx:
                send_whatsapp_message(settings, "test")
            self.assertNotIn("SECRET_CB_KEY", str(ctx.exception))

    def test_deadline_moved_earlier_and_opens_shifted_beforehand(self) -> None:
        settings = make_settings()
        t0 = datetime(2026, 11, 26, 14, 0, tzinfo=IST)
        due_orig = datetime(2026, 11, 29, 23, 59, tzinfo=IST)
        opens_orig = datetime(2026, 11, 28, 17, 0, tzinfo=IST)

        state = {
            "assessments": {
                "q1": {
                    "title": "Lab Quiz 1",
                    "course": "CS209",
                    "due_at": due_orig.isoformat(),
                    "opens_at": opens_orig.isoformat(),
                    "submissions_not_open_yet": True,
                    "url": "https://www.sidus.net/modules/assessments/assessments/q1/quiz",
                    "sent_reminders": [],
                }
            }
        }

        # Case 1: Faculty shifts opens_at earlier without changing due_at
        t1 = datetime(2026, 11, 26, 14, 50, tzinfo=IST)
        opens_earlier = datetime(2026, 11, 26, 15, 0, tzinfo=IST)
        item_shifted = parse_assessments_payload(
            {
                "success": True,
                "data": [
                    {
                        **make_raw_item(
                            "q1",
                            "Lab Quiz 1",
                            due_utc=due_orig.astimezone(ZoneInfo("UTC")).isoformat(),
                        ),
                        "opensAt": opens_earlier.astimezone(ZoneInfo("UTC")).isoformat(),
                        "submissionsNotOpenYet": False,
                    }
                ],
            },
            settings,
            now=t1,
        )
        alerts = evaluate_assessments(item_shifted, state, settings, t1)
        self.assertEqual([k for k, _ in alerts], ["schedule_changed"])
        self.assertIn("Schedule Moved EARLIER", alerts[0][1])

        # Case 2: Faculty cuts deadline from Nov 29 to today 3:50 PM
        due_short = datetime(2026, 11, 26, 15, 50, tzinfo=IST)
        item_shortened = parse_assessments_payload(
            {
                "success": True,
                "data": [
                    {
                        **make_raw_item(
                            "q1",
                            "Lab Quiz 1",
                            due_utc=due_short.astimezone(ZoneInfo("UTC")).isoformat(),
                        ),
                        "opensAt": opens_earlier.astimezone(ZoneInfo("UTC")).isoformat(),
                        "submissionsNotOpenYet": False,
                    }
                ],
            },
            settings,
            now=t1,
        )
        alerts = evaluate_assessments(item_shortened, state, settings, t1)
        self.assertEqual([k for k, _ in alerts], ["deadline_changed"])
        self.assertIn("Deadline Moved EARLIER", alerts[0][1])

    def test_legacy_state_without_opens_at_does_not_false_alert(self) -> None:
        settings = make_settings()
        t0 = datetime(2026, 11, 26, 12, 0, tzinfo=IST)
        due = datetime(2026, 11, 29, 23, 59, tzinfo=IST)
        state = {
            "assessments": {
                "legacy-1": {
                    "title": "Assignment 3",
                    "course": "RE102",
                    "due_at": due.isoformat(),
                    "url": "https://www.sidus.net/modules/assessments/assessments",
                    "sent_reminders": [],
                }
            }
        }
        items = parse_assessments_payload(
            {
                "success": True,
                "data": [
                    make_raw_item(
                        "legacy-1",
                        "Assignment 3",
                        due_utc=due.astimezone(ZoneInfo("UTC")).isoformat(),
                    )
                ],
            },
            settings,
            now=t0,
        )
        alerts = evaluate_assessments(items, state, settings, t0)
        self.assertEqual(alerts, [])
        self.assertIn("opens_at", state["assessments"]["legacy-1"])


if __name__ == "__main__":
    unittest.main()

