/**
 * Cloudflare Worker: 24/7 instant Telegram webhook for Sidus Portal commands (/today, /upcoming).
 *
 * Required Secrets (Workers -> Settings -> Variables and Secrets):
 *   - TELEGRAM_BOT_TOKEN
 *   - TELEGRAM_CHAT_ID
 *   - SESSION_JSON          (raw contents of session.json from login_once.py)
 *   - WEBHOOK_SECRET        (random secret string passed to Telegram setWebhook secret_token)
 *
 * Optional Variables:
 *   - DEADLINE_WINDOW_HOURS (default: "48")
 *   - PORTAL_ORIGIN         (default: "https://www.sidus.net")
 *   - API_BASE_URL          (default: "https://server.sidus.net")
 */

const ASSESSMENTS_ENDPOINT = "/api/v1/student-portal/me/assessments";
const COMPLETED_STATUSES = new Set(["SUBMITTED", "COMPLETED", "GRADED", "EVALUATED"]);
const IST_TIMEZONE = "Asia/Kolkata";

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatRemaining(ms) {
  const totalMinutes = Math.max(0, Math.floor(ms / 60000));
  const days = Math.floor(totalMinutes / (24 * 60));
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60);
  const minutes = totalMinutes % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (minutes || parts.length === 0) parts.push(`${minutes}m`);
  return parts.join(" ");
}

function formatIstDateTime(date) {
  return (
    new Intl.DateTimeFormat("en-GB", {
      timeZone: IST_TIMEZONE,
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    }).format(date) + " IST"
  );
}

function getIstDateKey(date) {
  // Returns YYYY-MM-DD in Asia/Kolkata
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: IST_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

function buildAssessmentUrl(portalOrigin, id, method) {
  if (method === "QUIZ") {
    return `${portalOrigin}/modules/assessments/assessments/${id}/quiz`;
  }
  if (method === "REFLECTIVE_JOURNAL") {
    return `${portalOrigin}/modules/reflective-journal/${id}`;
  }
  return `${portalOrigin}/modules/assessments/assessments`;
}

function isAlreadySubmitted(item) {
  const computed = String(item.computedStatus || "").toUpperCase();
  if (computed === "SUBMITTED" || computed === "PUBLISHED") return true;
  if (item.notSubmitted === true) return true;

  if (item.submission && typeof item.submission === "object") {
    if (item.submission.submittedAt) return true;
    if (COMPLETED_STATUSES.has(String(item.submission.status || "").toUpperCase())) {
      return true;
    }
  }

  if (item.quizAttempt && typeof item.quizAttempt === "object") {
    if (item.quizAttempt.submittedAt) return true;
    if (COMPLETED_STATUSES.has(String(item.quizAttempt.status || "").toUpperCase())) {
      return true;
    }
  }

  return false;
}

function extractCookieHeader(sessionJsonText) {
  let parsed;
  try {
    parsed = JSON.parse(sessionJsonText);
  } catch {
    throw new Error("SESSION_JSON_INVALID");
  }
  const cookies = Array.isArray(parsed?.cookies) ? parsed.cookies : [];
  const sidusCookies = cookies.filter(
    (c) => c?.name && c?.value != null && String(c?.domain || "").includes("sidus.net")
  );
  if (!sidusCookies.some((c) => c.name === "token" && c.value)) {
    throw new Error("SESSION_EXPIRED");
  }
  return sidusCookies.map((c) => `${c.name}=${c.value}`).join("; ");
}

async function fetchActiveAssessments(env) {
  if (!env.SESSION_JSON) {
    throw new Error("SESSION_EXPIRED");
  }
  const cookieHeader = extractCookieHeader(env.SESSION_JSON);
  const portalOrigin = (env.PORTAL_ORIGIN || "https://www.sidus.net").replace(/\/+$/, "");
  const apiBaseUrl = (env.API_BASE_URL || "https://server.sidus.net").replace(/\/+$/, "");
  const windowHours = Number(env.DEADLINE_WINDOW_HOURS || "48");

  const res = await fetch(`${apiBaseUrl}${ASSESSMENTS_ENDPOINT}`, {
    headers: {
      Accept: "application/json",
      Cookie: cookieHeader,
      Origin: portalOrigin,
      Referer: `${portalOrigin}/`,
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136.0.0.0",
    },
  });

  if (res.status === 401 || res.status === 403) {
    throw new Error("SESSION_EXPIRED");
  }
  if (!res.ok) {
    throw new Error(`API_HTTP_${res.status}`);
  }

  const payload = await res.json();
  if (!payload?.success || !Array.isArray(payload?.data)) {
    throw new Error("API_SCHEMA_CHANGED");
  }

  const nowMs = Date.now();
  const maxMs = windowHours * 3600 * 1000;
  const results = [];

  for (const item of payload.data) {
    if (!item || typeof item !== "object") continue;
    if (String(item.computedStatus || "").toUpperCase() !== "ACTIVE") continue;
    if (isAlreadySubmitted(item)) continue;

    const isExt = Boolean(item.isExtension && item.extensionDueDate);
    const dueRaw = isExt ? item.extensionDueDate : item.dueDate;
    if (!dueRaw) continue;

    const dueDate = new Date(dueRaw);
    const remainingMs = dueDate.getTime() - nowMs;
    if (Number.isNaN(remainingMs) || remainingMs <= 0 || remainingMs >= maxMs) continue;

    const code = String(item.subject?.code || "").trim();
    const name = String(item.subject?.name || "").trim();
    const course = code && name ? `${code} - ${name}` : name || code || "Unknown Course";
    const method = String(item.method || "").toUpperCase();

    results.push({
      id: String(item.id),
      title: String(item.title || "Untitled").trim(),
      course,
      dueDate,
      remainingMs,
      url: buildAssessmentUrl(portalOrigin, String(item.id), method),
    });
  }

  results.sort((a, b) => a.dueDate.getTime() - b.dueDate.getTime());
  return { assessments: results, windowHours };
}

function formatAssessmentList(heading, assessments, emptyMessage) {
  if (assessments.length === 0) {
    return `<b>${escapeHtml(heading)}</b>\n${escapeHtml(emptyMessage)}`;
  }

  const lines = [`<b>${escapeHtml(heading)}</b> (${assessments.length})`];
  assessments.forEach((item, index) => {
    lines.push(
      `\n${index + 1}. <b>${escapeHtml(item.title)}</b> — ${escapeHtml(item.course)}\n` +
        `   Due: ${escapeHtml(formatIstDateTime(item.dueDate))} (${escapeHtml(formatRemaining(item.remainingMs))} left)\n` +
        `   <a href="${escapeHtml(item.url)}">Open</a>`
    );
  });
  return lines.join("\n");
}

async function sendTelegramMessage(env, chatId, htmlText) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: htmlText,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    }
  );
  if (!res.ok) {
    console.error("Telegram sendMessage failed:", res.status);
  }
}

export default {
  async fetch(request, env) {
    if (request.method === "GET") {
      return new Response("Sidus Telegram Webhook OK", { status: 200 });
    }
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    if (env.WEBHOOK_SECRET) {
      const incomingSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (incomingSecret !== env.WEBHOOK_SECRET) {
        return new Response("Forbidden", { status: 403 });
      }
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    const message = update?.message;
    const chatId = message?.chat?.id != null ? String(message.chat.id) : "";
    if (!chatId || chatId !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response("OK", { status: 200 });
    }

    const command = String(message?.text || "")
      .trim()
      .split(/\s+/)[0]
      .split("@")[0]
      .toLowerCase();

    if (command === "/start" || command === "/help") {
      await sendTelegramMessage(
        env,
        chatId,
        "<b>Sidus Deadline Bot</b>\nCommands:\n/today — Active assessments due today\n/upcoming — Active assessments due within 48 hours"
      );
      return new Response("OK", { status: 200 });
    }

    if (command !== "/today" && command !== "/upcoming") {
      return new Response("OK", { status: 200 });
    }

    try {
      const { assessments, windowHours } = await fetchActiveAssessments(env);
      if (command === "/today") {
        const todayKey = getIstDateKey(new Date());
        const dueToday = assessments.filter((a) => getIstDateKey(a.dueDate) === todayKey);
        await sendTelegramMessage(
          env,
          chatId,
          formatAssessmentList("📅 Due Today", dueToday, "No active assessments due today.")
        );
      } else {
        await sendTelegramMessage(
          env,
          chatId,
          formatAssessmentList(
            `📋 Upcoming Active Assessments (< ${windowHours}h)`,
            assessments,
            "No active assessments due soon."
          )
        );
      }
    } catch (err) {
      const code = err?.message || "UNKNOWN_ERROR";
      if (code === "SESSION_EXPIRED") {
        await sendTelegramMessage(
          env,
          chatId,
          "Session expired, run login_once.py and update SESSION_JSON."
        );
      } else {
        await sendTelegramMessage(
          env,
          chatId,
          escapeHtml(`⚠️ Could not fetch Sidus assessments (${code}).`)
        );
      }
    }

    return new Response("OK", { status: 200 });
  },
};
