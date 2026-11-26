/**
 * Cloudflare Worker: 24/7 Telegram webhook + 5-minute scheduled deadline monitor.
 *
 * Required Secrets (Workers -> Settings -> Variables and Secrets):
 *   - TELEGRAM_BOT_TOKEN
 *   - TELEGRAM_CHAT_ID
 *   - SESSION_JSON          (raw contents of session.json from login_once.py)
 *   - WEBHOOK_SECRET        (random secret string for Telegram webhook & /run-check)
 *
 * Required KV Binding (Workers -> Settings -> Bindings -> KV Namespace):
 *   - SIDUS_STATE           (stores seen assessments, due/open times, and sent reminders)
 *
 * Optional Variables:
 *   - DEADLINE_WINDOW_HOURS (default: "48", used for /upcoming filter)
 *   - REMINDER_HOURS        (default: "24,6,3,2,1")
 *   - PORTAL_ORIGIN         (default: "https://www.sidus.net")
 *   - API_BASE_URL          (default: "https://server.sidus.net")
 *
 * Cron Trigger (Workers -> Settings -> Trigger Events -> Cron Triggers):
 *   - Cron every 5 minutes (star/5 * * * *)
 */

const ASSESSMENTS_ENDPOINT = "/api/v1/student-portal/me/assessments";
const COMPLETED_STATUSES = new Set(["SUBMITTED", "COMPLETED", "GRADED", "EVALUATED"]);
const IST_TIMEZONE = "Asia/Kolkata";
const STATE_KV_KEY = "state";
const ERROR_ALERT_COOLDOWN_MS = 6 * 3600 * 1000;
const STATE_RETENTION_MS = 7 * 24 * 3600 * 1000;

const METHOD_LABELS = {
  QUIZ: "Quiz",
  FILE_UPLOAD: "File Upload",
  REFLECTIVE_JOURNAL: "Reflective Journal",
  SIMPLE_ASSESSMENT: "Assignment",
};

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

function formatIsoIst(isoString) {
  if (!isoString) return "Unknown";
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return String(isoString);
  return formatIstDateTime(d);
}

function getIstDateKey(date) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: IST_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  return `${map.year}-${map.month}-${map.day}`;
}

function parseReminderHours(raw) {
  const text = String(raw || "24,6,3,2,1");
  const values = new Set();
  for (const part of text.split(",")) {
    const n = Number(part.trim());
    if (Number.isInteger(n) && n > 0) values.add(n);
  }
  if (values.size === 0) return [24, 6, 3, 2, 1];
  return Array.from(values).sort((a, b) => b - a);
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
    if (Number.isNaN(remainingMs) || remainingMs <= 0) continue;

    const opensRaw = item.opensAt || item.quizStartTime || null;
    const opensDate = opensRaw ? new Date(opensRaw) : null;
    const validOpens = opensDate && !Number.isNaN(opensDate.getTime()) ? opensDate : null;

    const code = String(item.subject?.code || "").trim();
    const name = String(item.subject?.name || "").trim();
    const course = code && name ? `${code} - ${name}` : name || code || "Unknown Course";
    const method = String(item.method || "").toUpperCase();

    results.push({
      id: String(item.id),
      title: String(item.title || "Untitled").trim(),
      course,
      method,
      dueDate,
      dueIso: dueDate.toISOString(),
      opensDate: validOpens,
      opensIso: validOpens ? validOpens.toISOString() : null,
      isExtension: isExt,
      submissionsNotOpenYet: Boolean(item.submissionsNotOpenYet),
      remainingMs,
      url: buildAssessmentUrl(portalOrigin, String(item.id), method),
    });
  }

  results.sort((a, b) => a.dueDate.getTime() - b.dueDate.getTime());
  return { assessments: results, windowHours };
}

function formatAssessmentMessage(header, item, nowMs, extraHtml = "") {
  const methodLabel = METHOD_LABELS[item.method] || "Assessment";
  const remaining = formatRemaining(item.dueDate.getTime() - nowMs);
  const lines = [
    `<b>${escapeHtml(header)}</b>`,
    `<b>Title:</b> ${escapeHtml(item.title)} (${escapeHtml(methodLabel)})`,
    `<b>Course:</b> ${escapeHtml(item.course)}`,
    `<b>Due:</b> ${escapeHtml(formatIstDateTime(item.dueDate))}`,
    `<b>Time Left:</b> ${escapeHtml(remaining)}`,
  ];
  if (extraHtml) {
    lines.push(extraHtml);
  }
  if (item.isExtension) {
    lines.push("<i>Note: Extended deadline applied</i>");
  }
  if (item.submissionsNotOpenYet && item.opensDate) {
    lines.push(`<i>Submissions open: ${escapeHtml(formatIstDateTime(item.opensDate))}</i>`);
  }
  lines.push(`<a href="${escapeHtml(item.url)}">Open Assessment</a>`);
  return lines.join("\n");
}

function formatAssessmentList(heading, assessments, emptyMessage) {
  if (assessments.length === 0) {
    return `<b>${escapeHtml(heading)}</b>\n${escapeHtml(emptyMessage)}`;
  }

  const lines = [`<b>${escapeHtml(heading)}</b> (${assessments.length})`];
  assessments.forEach((item, index) => {
    lines.push(
      `\n${index + 1}. <b>${escapeHtml(item.title)}</b> \u2014 ${escapeHtml(item.course)}\n` +
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
    throw new Error(`TELEGRAM_HTTP_${res.status}`);
  }
}

function shouldSendErrorAlert(lastIso, nowMs) {
  if (!lastIso) return true;
  const lastMs = Date.parse(lastIso);
  if (Number.isNaN(lastMs)) return true;
  return nowMs - lastMs >= ERROR_ALERT_COOLDOWN_MS;
}

function evaluateAssessments(assessments, state, reminderHours, nowMs) {
  const alerts = [];
  if (!state.assessments || typeof state.assessments !== "object") {
    state.assessments = {};
  }
  const stored = state.assessments;

  for (const item of assessments) {
    const remainingHours = (item.dueDate.getTime() - nowMs) / 3600000;
    if (remainingHours <= 0) continue;

    const existing = stored[item.id];
    if (!existing || typeof existing !== "object") {
      alerts.push({
        kind: "new",
        html: formatAssessmentMessage("\u{1F195} New Assessment", item, nowMs),
      });
      const covered = reminderHours.filter((h) => remainingHours <= h);
      stored[item.id] = {
        title: item.title,
        course: item.course,
        dueAt: item.dueIso,
        opensAt: item.opensIso,
        submissionsNotOpenYet: item.submissionsNotOpenYet,
        url: item.url,
        sentReminders: covered,
      };
      continue;
    }

    const prevDueIso = existing.dueAt || null;
    const hasPrevOpens = Object.prototype.hasOwnProperty.call(existing, "opensAt");
    const prevOpensIso = existing.opensAt || null;
    const prevLocked = existing.submissionsNotOpenYet;
    let sentReminders = Array.isArray(existing.sentReminders) ? [...existing.sentReminders] : [];

    const dueChanged = prevDueIso !== item.dueIso;
    const opensChanged = hasPrevOpens && prevOpensIso !== item.opensIso;
    const unlockedEarly = prevLocked === true && !item.submissionsNotOpenYet;

    if (dueChanged) {
      const oldDueMs = prevDueIso ? Date.parse(prevDueIso) : NaN;
      const movedEarlier = !Number.isNaN(oldDueMs) && item.dueDate.getTime() < oldDueMs;
      const header = movedEarlier
        ? "\u26A0\uFE0F Deadline Moved EARLIER"
        : "\u{1F4C5} Deadline Extended";
      const extraLines = [`<b>Previous Due:</b> ${escapeHtml(formatIsoIst(prevDueIso))}`];
      if (opensChanged && item.opensDate) {
        extraLines.push(
          `<b>Opens At:</b> ${escapeHtml(formatIstDateTime(item.opensDate))} (was ${escapeHtml(formatIsoIst(prevOpensIso))})`
        );
      }
      alerts.push({
        kind: "deadline_changed",
        html: formatAssessmentMessage(header, item, nowMs, extraLines.join("\n")),
      });
      sentReminders = reminderHours.filter((h) => remainingHours <= h);
    } else if (opensChanged || unlockedEarly) {
      let header = "\u{1F513} Submissions Now Open";
      let extra = "<i>Submissions opened earlier than previously scheduled.</i>";
      if (opensChanged && item.opensDate) {
        const oldOpensMs = prevOpensIso ? Date.parse(prevOpensIso) : NaN;
        const shiftedEarlier = !Number.isNaN(oldOpensMs) && item.opensDate.getTime() < oldOpensMs;
        header = shiftedEarlier
          ? "\u26A0\uFE0F Schedule Moved EARLIER"
          : "\u{1F4C5} Schedule Updated";
        extra =
          `<b>Opens At:</b> ${escapeHtml(formatIstDateTime(item.opensDate))}\n` +
          `<b>Previous Opens:</b> ${escapeHtml(formatIsoIst(prevOpensIso))}`;
      }
      alerts.push({
        kind: "schedule_changed",
        html: formatAssessmentMessage(header, item, nowMs, extra),
      });
    } else {
      const dueThresholds = reminderHours.filter(
        (h) => remainingHours <= h && !sentReminders.includes(h)
      );
      if (dueThresholds.length > 0) {
        const urgentH = Math.min(...dueThresholds);
        const label = urgentH === 1 ? "1 hour" : `${urgentH} hours`;
        alerts.push({
          kind: `reminder_${urgentH}h`,
          html: formatAssessmentMessage(
            `\u23F0 Deadline Reminder (< ${label} left)`,
            item,
            nowMs
          ),
        });
        for (const h of dueThresholds) {
          if (!sentReminders.includes(h)) sentReminders.push(h);
        }
      }
    }

    stored[item.id] = {
      title: item.title,
      course: item.course,
      dueAt: item.dueIso,
      opensAt: item.opensIso,
      submissionsNotOpenYet: item.submissionsNotOpenYet,
      url: item.url,
      sentReminders: Array.from(new Set(sentReminders)).sort((a, b) => b - a),
    };
  }

  // Prune records more than 7 days past due
  const cutoffMs = nowMs - STATE_RETENTION_MS;
  for (const [id, entry] of Object.entries(stored)) {
    const dueMs = entry?.dueAt ? Date.parse(entry.dueAt) : NaN;
    if (!Number.isNaN(dueMs) && dueMs < cutoffMs) {
      delete stored[id];
    }
  }

  return alerts;
}

async function runScheduledCheck(env) {
  if (!env.SIDUS_STATE) {
    console.error("SIDUS_STATE KV binding is missing; skipping scheduled check.");
    return { ok: false, reason: "MISSING_KV_BINDING" };
  }

  const nowMs = Date.now();
  const nowIso = new Date(nowMs).toISOString();
  const rawState = await env.SIDUS_STATE.get(STATE_KV_KEY);
  const state = rawState
    ? JSON.parse(rawState)
    : { assessments: {}, lastSessionAlertAt: null, lastScraperAlertAt: null };
  const beforeJson = JSON.stringify(state);

  let assessments;
  try {
    ({ assessments } = await fetchActiveAssessments(env));
  } catch (err) {
    const code = err?.message || "UNKNOWN_ERROR";
    if (code === "SESSION_EXPIRED") {
      if (shouldSendErrorAlert(state.lastSessionAlertAt, nowMs)) {
        await sendTelegramMessage(
          env,
          env.TELEGRAM_CHAT_ID,
          "Session expired, run login_once.py and update SESSION_JSON."
        );
        state.lastSessionAlertAt = nowIso;
      }
    } else if (shouldSendErrorAlert(state.lastScraperAlertAt, nowMs)) {
      await sendTelegramMessage(
        env,
        env.TELEGRAM_CHAT_ID,
        escapeHtml(`\u26A0\uFE0F Sidus scraper error (${code}).`)
      );
      state.lastScraperAlertAt = nowIso;
    }

    const afterErrorJson = JSON.stringify(state);
    if (afterErrorJson !== beforeJson) {
      await env.SIDUS_STATE.put(STATE_KV_KEY, afterErrorJson);
    }
    return { ok: false, reason: code };
  }

  state.lastSessionAlertAt = null;
  state.lastScraperAlertAt = null;

  const reminderHours = parseReminderHours(env.REMINDER_HOURS);
  const alerts = evaluateAssessments(assessments, state, reminderHours, nowMs);

  for (const alert of alerts) {
    await sendTelegramMessage(env, env.TELEGRAM_CHAT_ID, alert.html);
  }

  const afterJson = JSON.stringify(state);
  if (afterJson !== beforeJson) {
    await env.SIDUS_STATE.put(STATE_KV_KEY, afterJson);
  }

  return { ok: true, activeCount: assessments.length, alertsSent: alerts.length };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runScheduledCheck(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/run-check") {
      const secretParam = url.searchParams.get("secret");
      const headerSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (!env.WEBHOOK_SECRET || (secretParam !== env.WEBHOOK_SECRET && headerSecret !== env.WEBHOOK_SECRET)) {
        return new Response("Forbidden", { status: 403 });
      }
      const result = await runScheduledCheck(env);
      return new Response(JSON.stringify(result), {
        status: result.ok ? 200 : 500,
        headers: { "Content-Type": "application/json" },
      });
    }

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
        "<b>Sidus Deadline Bot</b>\nCommands:\n/today \u2014 Active assessments due today\n/upcoming \u2014 Active assessments due within 48 hours"
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
          formatAssessmentList("\u{1F4C5} Due Today", dueToday, "No active assessments due today.")
        );
      } else {
        const maxMs = windowHours * 3600 * 1000;
        const upcoming = assessments.filter((a) => a.remainingMs < maxMs);
        await sendTelegramMessage(
          env,
          chatId,
          formatAssessmentList(
            `\u{1F4CB} Upcoming Active Assessments (< ${windowHours}h)`,
            upcoming,
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
          escapeHtml(`\u26A0\uFE0F Could not fetch Sidus assessments (${code}).`)
        );
      }
    }

    return new Response("OK", { status: 200 });
  },
};
