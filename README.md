# Sidus Portal Deadline Notifier (Aurora University)

A 100% free Python notification system that checks the **Sidus Portal** (`https://www.sidus.net`) every 30 minutes and sends instant Telegram alerts to your phone for **all active assessments** (new posts, deadline changes, and `24h / 6h / 3h / 2h / 1h` reminders).

## Features
- **New Assessment Alerts**: Instant notification whenever a new `ACTIVE` assessment is posted (regardless of whether the deadline is 2 days or 7+ days away).
- **Deadline Change Alerts**: Notifies you if an existing due date (or extension due date) changes.
- **Configurable Reminders**: Alerts at `24h`, `6h`, `3h`, `2h`, and `1h` before the deadline (`REMINDER_HOURS=24,6,3,2,1`) without duplicating reminders.
- **Smart Filtering**: Tracks all `ACTIVE` unsubmitted assessments (ignoring `UPCOMING`, `SUBMITTED`, `PUBLISHED`, missed, and past-due items), while `/upcoming` shows urgent items due within `< 48` hours (`DEADLINE_WINDOW_HOURS=48`).
- **Session-Reuse Login**: Uses `login_once.py` (Playwright) to save session cookies locally into `session.json`—no password is stored in code or `.env`.
- **Failure Alerts**: Sends `"Session expired, run login_once.py"` if your ~7-day JWT cookie expires, or alerts you if the portal API/layout changes.
- **Telegram Commands**: Supports `/today` and `/upcoming` (checked automatically on each run).

---

## 1. Install Dependencies

Python 3.11+ is required.

```powershell
cd "d:\Notification AI"
pip install -r requirements.txt
playwright install chromium
```

*(Optional)* If you prefer a virtual environment:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

---

## 2. Create Your Telegram Bot & Get Your Chat ID

### Step A: Create a bot via @BotFather
1. Open Telegram and search for **`@BotFather`** (verified blue checkmark).
2. Send `/newbot`.
3. Choose a display name (e.g. `Aurora Deadlines`) and a username ending in `bot` (e.g. `aurora_sidus_deadlines_bot`).
4. `@BotFather` replies with your **Bot Token** (looks like `123456789:ABCdef...`). Copy it.
5. **Important**: Open your new bot in Telegram and click **Start** (or send `hi`) so the bot is allowed to message you.

### Step B: Get your Chat ID
1. After sending `hi` to your bot, open this URL in your browser (replace `<YOUR_BOT_TOKEN>`):
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
2. Look for `"chat":{"id":123456789,...}` in the JSON response. That number is your `TELEGRAM_CHAT_ID`.

---

## 3. Configure `.env`

Copy `.env.example` to `.env` and fill in your values:

```powershell
Copy-Item .env.example .env
notepad .env
```

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=123456789
PORTAL_URL=https://www.sidus.net/dashboard
API_BASE_URL=https://server.sidus.net
DEADLINE_WINDOW_HOURS=48
REMINDER_HOURS=24,6,3,2,1
TIMEZONE=Asia/Kolkata
```

---

## 4. Log In Once (`login_once.py`)

Run the one-time browser login script:

```powershell
python login_once.py
```

1. A visible Chromium window opens to `https://www.sidus.net/login`.
2. Sign in with your Aurora / Sidus credentials.
3. Once you reach the dashboard, `login_once.py` automatically saves your session cookies to `session.json` and closes.

*(Sidus session tokens last about 7 days. When one expires, you'll get a Telegram message: `Session expired, run login_once.py`.)*

---

## 5. Test Before Scheduling

1. **Test Telegram delivery** (sends a sample alert without scraping):
   ```powershell
   python main.py --test
   ```
2. **Dry run** (scrapes real portal data and prints what would be sent, without sending messages or modifying `state.json`):
   ```powershell
   python main.py --dry-run
   ```
3. **Live run** (fetches, alerts, and saves `state.json`):
   ```powershell
   python main.py
   ```

Logs are written to `app.log`.

---

## 6. Run Automatically Every 30 Minutes

### Option A (Recommended): Windows Task Scheduler

You can register the task in one command (runs `run.bat` every 30 minutes):

```powershell
schtasks /Create /TN "SidusDeadlineNotifier" /TR "\"d:\Notification AI\run.bat\"" /SC MINUTE /MO 30 /F
```

Or import the ready-made task file:
```powershell
schtasks /Create /TN "SidusDeadlineNotifier" /XML "d:\Notification AI\SidusDeadlineNotifier.xml" /F
```

Useful management commands:
```powershell
# Run immediately to verify
schtasks /Run /TN "SidusDeadlineNotifier"

# Check status
schtasks /Query /TN "SidusDeadlineNotifier" /V /FO LIST

# Remove scheduled task
schtasks /Delete /TN "SidusDeadlineNotifier" /F
```

> **Tip**: If you want the task to wake/run reliably on a laptop even on battery, importing `SidusDeadlineNotifier.xml` already sets `DisallowStartIfOnBatteries=false` and `StartWhenAvailable=true`.

---

### Option B (Optional): GitHub Actions (`.github/workflows/check.yml`)

A workflow is included at `.github/workflows/check.yml` (`cron: "*/30 * * * *"`), using `actions/cache` to persist `state.json` between runs.

> **Important Warnings for Option B**:
> - **Cloud IP blocking**: The university portal (`server.sidus.net`) may block or rate-limit shared GitHub Actions cloud IPs.
> - **Manual session refresh**: Because GitHub Actions cannot open a browser for interactive login or OTP, whenever `session.json` expires (~7 days), you must re-run `python login_once.py` locally and paste the new contents of `session.json` into the `SESSION_JSON` GitHub Secret.
> - **Never commit `.env`, `session.json`, or `state.json`** to a public repository.

To configure Option B in **GitHub → Settings → Secrets and variables → Actions**:
- `TELEGRAM_BOT_TOKEN`: your bot token
- `TELEGRAM_CHAT_ID`: your chat ID
- `PORTAL_URL`: `https://www.sidus.net/dashboard`
- `SESSION_JSON`: full raw JSON contents of your local `session.json`

---

### Option C (24/7 Instant `/today` & `/upcoming`): Cloudflare Worker Webhook

GitHub Actions only runs every ~30 minutes. To get **instant (< 1 second) 24/7 replies** to `/today` and `/upcoming` with **$0 and no credit card**:

1. Create a free account at `https://dash.cloudflare.com`.
2. Go to **Workers & Pages → Create → Create Worker → Deploy** (or deploy `cloudflare-worker/` with Wrangler).
3. Click **Edit code**, paste the entire contents of `cloudflare-worker/worker.js`, and click **Deploy**.
4. Open your Worker's **Settings → Variables and Secrets** and add 4 **Secrets**:
   - `TELEGRAM_BOT_TOKEN`: your bot token
   - `TELEGRAM_CHAT_ID`: your chat ID (`7386674154`)
   - `SESSION_JSON`: full raw contents of `session.json` (remember to update it alongside GitHub Secrets whenever your ~7-day session expires)
   - `WEBHOOK_SECRET`: any random password/string you invent (letters/numbers/underscores, no spaces)
5. Register the webhook by opening this URL once in your browser (replace the 3 placeholders):
   ```
   https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<YOUR_WORKER>.workers.dev&secret_token=<WEBHOOK_SECRET>
   ```
   You should see `{"ok":true,"result":true,"description":"Webhook was set"}`.

*(GitHub Actions will keep sending your 30-minute new-assessment, deadline-change, and reminder alerts, and `notifier.py` automatically skips `getUpdates` when a webhook is active.)*

---

## Project Structure
- `login_once.py` — One-time visible browser login to generate `session.json`
- `scraper.py` — Authenticated JSON API fetcher (`GET /api/v1/student-portal/me/assessments`) and deadline filter
- `notifier.py` — Telegram HTTP client (`sendMessage`, `/today`, `/upcoming`)
- `state.py` — Atomic `state.json` persistence and pruning
- `main.py` — Orchestrator, diffing, reminders, `--test`, `--dry-run`, error alerting
- `config.py` — `.env` loader and token-redacting logger (`app.log`)
- `run.bat` / `SidusDeadlineNotifier.xml` — Windows Task Scheduler runner and task definition
- `.github/workflows/check.yml` — Optional GitHub Actions cron workflow
- `cloudflare-worker/worker.js` — Optional 24/7 instant Telegram webhook for `/today` & `/upcoming`
- `tests/test_notifier.py` — Unit tests for filtering, reminder deduplication, deadline changes, and session expiry alerts

