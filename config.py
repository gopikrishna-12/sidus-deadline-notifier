"""Shared configuration and safe logging utilities."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "app.log"

DEFAULT_PORTAL_URL = "https://www.sidus.net/dashboard"
DEFAULT_API_BASE_URL = "https://server.sidus.net"
DEFAULT_DEADLINE_WINDOW_HOURS = 48.0
DEFAULT_REMINDER_HOURS = (24, 6, 3, 2, 1)
DEFAULT_TIMEZONE = "Asia/Kolkata"


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    telegram_bot_token: str
    telegram_chat_id: str
    portal_url: str
    portal_origin: str
    api_base_url: str
    deadline_window_hours: float
    reminder_hours: tuple[int, ...]
    timezone_name: str
    timezone: ZoneInfo


def _parse_reminder_hours(raw: str) -> tuple[int, ...]:
    """Parse comma-separated integer reminder thresholds sorted descending."""
    if not raw.strip():
        return DEFAULT_REMINDER_HOURS
    values: set[int] = set()
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        hours = int(item)
        if hours <= 0:
            raise ValueError(f"Reminder threshold must be positive, got {hours}")
        values.add(hours)
    if not values:
        return DEFAULT_REMINDER_HOURS
    return tuple(sorted(values, reverse=True))


def _origin_from_url(url: str) -> str:
    """Extract scheme://netloc origin from a URL."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def load_settings(require_telegram: bool = True) -> Settings:
    """Load configuration from .env and environment variables."""
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if require_telegram and (not token or not chat_id):
        raise ValueError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env"
        )

    portal_url = os.getenv("PORTAL_URL", DEFAULT_PORTAL_URL).strip() or DEFAULT_PORTAL_URL
    api_base_url = (
        os.getenv("API_BASE_URL", DEFAULT_API_BASE_URL).strip().rstrip("/")
        or DEFAULT_API_BASE_URL
    )
    window_raw = os.getenv("DEADLINE_WINDOW_HOURS", str(DEFAULT_DEADLINE_WINDOW_HOURS)).strip()
    window_hours = float(window_raw)
    if window_hours <= 0:
        raise ValueError("DEADLINE_WINDOW_HOURS must be positive")

    reminder_raw = os.getenv("REMINDER_HOURS", "24,6,3,2,1")
    reminder_hours = _parse_reminder_hours(reminder_raw)

    tz_name = os.getenv("TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    tz = ZoneInfo(tz_name)

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        portal_url=portal_url,
        portal_origin=_origin_from_url(portal_url),
        api_base_url=api_base_url,
        deadline_window_hours=window_hours,
        reminder_hours=reminder_hours,
        timezone_name=tz_name,
        timezone=tz,
    )


class TokenRedactingFilter(logging.Filter):
    """Mask Telegram bot tokens if they ever appear in log messages."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets and isinstance(record.msg, str):
            for secret in self._secrets:
                if secret in record.msg:
                    record.msg = record.msg.replace(secret, "[REDACTED]")
        if self._secrets and record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact_value(arg) for arg in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: self._redact_value(v) for k, v in record.args.items()
                }
        return True

    def _redact_value(self, value: object) -> object:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
        return value


def setup_logging(secrets: list[str] | None = None, verbose: bool = False) -> logging.Logger:
    """Configure file and console logging with secret redaction."""
    logger = logging.getLogger("sidus_notifier")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = TokenRedactingFilter(secrets)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redactor)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger

