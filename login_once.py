"""One-time interactive login helper that saves Playwright session state."""

from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

from config import SESSION_FILE, load_settings


def main() -> int:
    """Open a visible browser for manual login and save session.json."""
    settings = load_settings(require_telegram=False)
    login_url = f"{settings.portal_origin}/login"

    print("Opening Sidus Portal in Chromium...")
    print(f"Login URL: {login_url}")
    print("Please sign in manually in the browser window.")
    print("Waiting until login completes (dashboard reached)...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(login_url, wait_until="domcontentloaded")

        deadline = time.monotonic() + 300  # 5 minutes timeout
        logged_in = False
        while time.monotonic() < deadline:
            current_url = page.url
            cookies = context.cookies(settings.api_base_url)
            has_token = any(c.get("name") == "token" and c.get("value") for c in cookies)
            if has_token and "/login" not in current_url:
                logged_in = True
                break
            page.wait_for_timeout(1000)

        if not logged_in:
            print("Timed out waiting for login. Please run login_once.py again.")
            browser.close()
            return 1

        # Give Next.js a moment to finish setting any remaining session metadata
        page.wait_for_timeout(1500)
        context.storage_state(path=str(SESSION_FILE))
        browser.close()

    print(f"Session saved successfully to {SESSION_FILE.name}.")
    print("Do NOT share or commit session.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

