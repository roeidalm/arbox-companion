/* Arbox frontend — vanilla JS, reads the server's SQLite-backed API.
   The API key (needed for actions) lives in localStorage after setup. */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = {
  apiKey: null,
  weekStart: startOfWeek(new Date()),   // the calendar anchor (any date)
  calMode: "week",                       // "day" | "week" | "month"
  coach: null,
  category: null,
  daypart: null,          // "morning" | "noon" | "afternoon" | "evening"
  facets: { coaches: [], categories: [] },
  view: "schedule",
  autoRefreshed: false,
  alarms: [],
  watchlist: new Set(),
  memberships: [],
  membershipOverrides: new Map(),
  blocked: [],
  studios: [],
  selectedStudioId: null,
  // the studio's timezone, from /api/health — every date shown is measured
  // against the studio's clock, not the device's
  serverTz: undefined,
  lastSchedule: null,
  settingsPane: "profile",
  logLevel: null,
  logSource: null,
  journalMode: "sessions",
  journalData: null,
  journalEntry: null,
  journalFocus: null,
  exercisePacks: [],
  exerciseCatalogTarget: "editor",
  exerciseCatalogTimer: null,
  journalFilters: { search: "", period: "all", category: "", coach: "", exercise: "", feedback: "" },
};

const REASON_LABELS = {
  work: "עבודה", illness: "מחלה", injury: "פציעה",
  personal: "אילוץ אישי/משפחתי", fatigue: "עייפות/התאוששות",
  plans_changed: "שינוי תוכניות", other: "אחר", none: "ללא סיבה",
};

function confirmSchedule(message) {
  const dialog = $("#scheduleConfirmDialog");
  const [title, ...body] = String(message || "").split("\n");
  $("#scheduleConfirmTitle").textContent = title || "אישור תזמון";
  $("#scheduleConfirmCopy").textContent = body.join("\n").trim();
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => {
    resolve(dialog.returnValue === "confirm");
  }, { once: true }));
}

function chooseReason(title, hint = "") {
  const dialog = $("#reasonDialog");
  const select = $("#reasonCode");
  const other = $("#reasonOther");
  $("#reasonTitle").textContent = title;
  $("#reasonHint").textContent = hint;
  select.value = "";
  other.value = "";
  other.setCustomValidity("");
  other.required = false;
  $("#reasonOtherWrap").hidden = true;
  dialog.returnValue = "";
  select.onchange = () => {
    const show = select.value === "other";
    $("#reasonOtherWrap").hidden = !show;
    other.required = show;
    if (show) other.focus();
  };
  other.oninput = () => other.setCustomValidity("");
  $("#reasonForm").onsubmit = (event) => {
    if (select.value === "other" && !other.value.trim()) {
      event.preventDefault();
      other.setCustomValidity("צריך לכתוב סיבה");
      other.reportValidity();
    }
  };
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      if (dialog.returnValue !== "confirm") { resolve(null); return; }
      resolve({ reason_code: select.value,
                reason_text: select.value === "other" ? other.value.trim() : null });
    }, { once: true });
  });
}

async function loadWatchlist() {
  try {
    const d = await api("/api/watchlist");
    state.watchlist = new Set(
      d.watchlist.filter((w) => !w.result).map((w) => w.schedule_id));
    state.membershipOverrides = new Map(
      d.watchlist.filter((w) => !w.result && w.membership_user_id)
        .map((w) => [w.schedule_id, w.membership_user_id]));
  } catch (e) { /* view still renders without the bells lit */ }
}

async function ensureMemberships() {
  if (!state.apiKey || state.memberships.length) return;
  try {
    const s = await api("/api/settings");
    state.memberships = s.available_memberships || [];
    state.preferredMembershipId = s.preferred_membership_id ||
      (state.memberships[0] && state.memberships[0].id);
    await loadWatchlist();
  } catch (e) { /* booking still works with the server-side default */ }
}

async function loadStudioSwitch() {
  if (!state.apiKey) return;
  const host = $("#studioSwitch");
  try {
    const d = await api("/api/studios");
    state.studios = d.studios || [];
    state.selectedStudioId = Number(d.selected_studio_id);
    host.innerHTML = "";
    if (!state.studios.length) { host.hidden = true; return; }
    const select = document.createElement("select");
    select.setAttribute("aria-label", "סטודיו נבחר");
    select.title = state.studios.length === 1
      ? "זה הסטודיו הפעיל היחיד שלך"
      : "החלפת סטודיו";
    for (const studio of state.studios) {
      const option = document.createElement("option");
      option.value = studio.id;
      option.textContent = `${studio.id === Number(d.default_studio_id) ? "⭐" : "📍"} ${studio.name}`;
      select.appendChild(option);
    }
    select.value = String(state.selectedStudioId);
    select.disabled = state.studios.length === 1;
    if (select.disabled) select.classList.add("studio-fixed-select");
    select.onchange = async () => {
      const previous = state.selectedStudioId;
      select.disabled = true;
      try {
        await api("/api/studios/select", {
          method: "POST", body: JSON.stringify({ studio_id: Number(select.value) }),
        });
        state.selectedStudioId = Number(select.value);
        state.memberships = [];
        state.membershipOverrides.clear();
        state.watchlist.clear();
        state.facets = { coaches: [], categories: [] };
        toast(`עברנו ל-${state.studios.find((s) => s.id === state.selectedStudioId)?.name}`);
        await Promise.all([loadFacets(), ensureMemberships()]);
        showView(state.view, false);
      } catch (e) {
        select.value = String(previous);
        toast("החלפת הסטודיו נכשלה: " + e.message);
      } finally {
        select.disabled = state.studios.length === 1;
      }
    };
    host.appendChild(select);
    host.hidden = false;
  } catch (e) {
    host.hidden = true;
  }
}

function showFixedStudio(name, title = "הסטודיו הנבחר") {
  const host = $("#studioSwitch");
  if (!name) { host.hidden = true; return; }
  host.innerHTML = "";
  const fixed = document.createElement("span");
  fixed.className = "studio-fixed";
  fixed.textContent = `📍 ${name}`;
  fixed.title = title;
  host.appendChild(fixed);
  host.hidden = false;
}

function membershipName(id) {
  const m = state.memberships.find((item) => item.id === Number(id));
  return m ? (m.plan || `מנוי ${id}`) : "ברירת מחדל";
}

function eligibleMemberships(s) {
  const day = s && s.date;
  return state.memberships.filter((m) => m.active !== false && (!day ||
    ((!m.start || m.start <= day) && (!m.end || m.end >= day))));
}

function defaultMembershipForSession(s) {
  const eligible = eligibleMemberships(s);
  return eligible.find((m) => m.id === Number(state.preferredMembershipId)) ||
    eligible.sort((a, b) => (a.end || "9999-12-31").localeCompare(
      b.end || "9999-12-31"))[0];
}

function chooseSessionMembership(s) {
  const dialog = $("#membershipDialog");
  const select = $("#sessionMembership");
  select.innerHTML = "";
  const defaultMembership = defaultMembershipForSession(s);
  const def = document.createElement("option");
  def.value = "";
  def.textContent = "בחירה אוטומטית לפי התאמה ומכסה";
  select.appendChild(def);
  for (const m of eligibleMemberships(s)) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = (m.plan || m.id) +
      (m.sessions_left != null ? ` · נותרו ${m.sessions_left}` : "") +
      (m.end ? ` · עד ${m.end}` : "");
    select.appendChild(o);
  }
  select.value = state.membershipOverrides.get(s.schedule_id) || "";
  $("#membershipDialogHint").textContent =
    `${s.category_name || "שיעור"} · ${s.date} ${s.start_time}. ` +
    "הבחירה חלה רק על האימון הזה ולא משנה את ברירת המחדל.";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => {
    if (dialog.returnValue !== "confirm") { resolve(false); return; }
    if (select.value) state.membershipOverrides.set(s.schedule_id, Number(select.value));
    else state.membershipOverrides.delete(s.schedule_id);
    resolve(true);
  }, { once: true }));
}

async function toggleWatch(s, btn) {
  if (!state.apiKey) { toast("צריך מפתח API כדי לסמן שיעורים"); return; }
  const on = state.watchlist.has(s.schedule_id);
  btn.disabled = true;
  try {
    if (on) {
      await api(`/api/watchlist/${s.schedule_id}`, { method: "DELETE" });
      state.watchlist.delete(s.schedule_id);
      toast("הסימון בוטל");
    } else {
      const membershipId = state.membershipOverrides.get(s.schedule_id) || null;
      const request = { schedule_id: s.schedule_id, allow_standby: true,
                        membership_user_id: membershipId };
      let r;
      // Vacation and quota are independent gates. A class can need both
      // confirmations, and neither preflight is allowed to store the pin.
      for (let attempts = 0; attempts < 3; attempts++) {
        r = await api("/api/watchlist", { method: "POST",
          body: JSON.stringify(request) });
        if (!r.needs_confirm) break;
        if (!await confirmSchedule(r.conflict)) { btn.disabled = false; return; }
        if (r.confirm_kind === "quota") request.confirm_over_quota = true;
        else request.ignore_vacation = true;
      }
      if (!r || r.needs_confirm) throw new Error("האישור לא הושלם");
      state.watchlist.add(s.schedule_id);
      toast(r.overrode_vacation
        ? `🔔 תוזמן למרות החופשה — ${r.note || ""}`
        : `🔔 סומן — ${r.note || "ייתפס כשההרשמה תיפתח"}`, 7000);
    }
    if (state.view === "mine") loadMine(); else loadSchedule();
  } catch (e) {
    toast("נכשל: " + e.message, 6000);
    btn.disabled = false;
  }
}

function fmtAlarm(mins) {
  if (mins % 1440 === 0) { const d = mins / 1440; return d === 1 ? "יום לפני" : `${d} ימים לפני`; }
  if (mins % 60 === 0) { const h = mins / 60; return h === 1 ? "שעה לפני" : `${h} שעות לפני`; }
  return `${mins} דק׳ לפני`;
}

function renderBlockedList() {
  const el = $("#blockedList");
  if (!el) return;
  el.innerHTML = "";
  if (!state.blocked.length) {
    const s = document.createElement("span");
    s.className = "hint";
    s.textContent = "אין קטגוריות חסומות";
    el.appendChild(s);
    return;
  }
  for (const name of state.blocked) {
    const chip = document.createElement("button");
    chip.className = "chip active";
    chip.type = "button";
    chip.textContent = `🚫 ${name} ✕`;
    chip.title = "הסר — נוכל לנסות אותה שוב";
    chip.onclick = () => {
      state.blocked = state.blocked.filter((x) => x !== name);
      renderBlockedList();
    };
    el.appendChild(chip);
  }
}

function renderAlarmList() {
  const el = $("#alarmList");
  el.innerHTML = "";
  if (!state.alarms.length) {
    const s = document.createElement("span");
    s.className = "hint";
    s.textContent = "אין תזכורות";
    el.appendChild(s);
    return;
  }
  for (const m of [...state.alarms].sort((a, b) => b - a)) {
    const chip = document.createElement("button");
    chip.className = "chip active";
    chip.textContent = `⏰ ${fmtAlarm(m)} ✕`;
    chip.title = "הסר";
    chip.onclick = () => {
      state.alarms = state.alarms.filter((x) => x !== m);
      renderAlarmList();
    };
    el.appendChild(chip);
  }
}

try { state.apiKey = localStorage.getItem("arbox_api_key"); } catch (e) {}

/* theme: auto (follow the OS) -> light -> dark -> auto */
const THEMES = ["auto", "light", "dark"];
const THEME_ICON = { auto: "🌗", light: "☀️", dark: "🌙" };
const THEME_LABEL = { auto: "לפי המערכת", light: "בהיר", dark: "כהה" };

function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  const b = document.querySelector("#themeToggle");
  if (b) {
    b.textContent = THEME_ICON[t];
    b.title = `מצב תצוגה: ${THEME_LABEL[t]} — לחיצה מחליפה`;
  }
  try { localStorage.setItem("arbox_theme", t); } catch (e) {}
}

function currentTheme() {
  try { return localStorage.getItem("arbox_theme") || "auto"; } catch (e) { return "auto"; }
}

applyTheme(currentTheme());
document.addEventListener("DOMContentLoaded", () => {
  applyTheme(currentTheme());
  document.querySelector("#themeToggle").addEventListener("click", () => {
    applyTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
  });
});

const WEEKDAYS_HE = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"];
// rule weekdays use Python convention: 0=Mon..6=Sun
const RULE_DAYS = [
  { py: 6, name: "ראשון", short: "א׳" }, { py: 0, name: "שני", short: "ב׳" },
  { py: 1, name: "שלישי", short: "ג׳" }, { py: 2, name: "רביעי", short: "ד׳" },
  { py: 3, name: "חמישי", short: "ה׳" }, { py: 4, name: "שישי", short: "ו׳" },
  { py: 5, name: "שבת", short: "ש׳" },
];

function startOfWeek(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  x.setDate(x.getDate() - x.getDay()); // Sunday-first week
  return x;
}
function iso(d) {
  // local date, NOT toISOString (which is UTC and shifts the day before 03:00)
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
/* The studio's clock, not this device's. Every date in the schedule is a
   studio-local wall-clock date; read through a browser in another timezone
   they land on the wrong day — "today" highlights the wrong column, and a
   class that finished last night still reads as upcoming. The app has a
   vacation feature, so being abroad is an expected state, not an edge case. */
function studioToday() {
  try {
    return new Date().toLocaleDateString("en-CA", { timeZone: state.serverTz });
  } catch (e) { return iso(new Date()); }
}
function studioNowStamp() {
  try {
    const time = new Date().toLocaleTimeString("en-GB", {
      timeZone: state.serverTz, hour: "2-digit", minute: "2-digit" });
    return `${studioToday()} ${time}`;
  } catch (e) {
    const d = new Date();
    return `${iso(d)} ${String(d.getHours()).padStart(2, "0")}:` +
           `${String(d.getMinutes()).padStart(2, "0")}`;
  }
}
function startOfMonth(d) {
  const x = new Date(d); x.setDate(1); x.setHours(0, 0, 0, 0); return x;
}
function addMonths(d, n) {
  // anchored to day 1 so ±1 month can never skip February
  const x = startOfMonth(d); x.setMonth(x.getMonth() + n); return x;
}
function visibleRange(mode, anchor) {
  if (mode === "day") return { from: new Date(anchor), to: new Date(anchor) };
  if (mode === "month") {
    const from = startOfMonth(anchor);
    return { from, to: addDays(addMonths(from, 1), -1) };
  }
  const from = startOfWeek(anchor);
  return { from, to: addDays(from, 6) };
}
function rangeLabel(mode, anchor) {
  if (mode === "day") {
    const d = new Date(anchor);
    return `${WEEKDAYS_HE[d.getDay()]} ${fmtDate(d)}`;
  }
  if (mode === "month")
    return anchor.toLocaleDateString("he-IL", { month: "long", year: "numeric" });
  const { from, to } = visibleRange("week", anchor);
  return `${fmtDate(from)} – ${fmtDate(to)}`;
}

function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.apiKey) headers["X-Api-Key"] = state.apiKey;
  const resp = await fetch(path, { ...opts, headers });
  let body = null;
  try { body = await resp.json(); } catch (e) {}
  if (!resp.ok) {
    const detail = body && body.detail ? body.detail : resp.status;
    throw new Error(detail);
  }
  return body;
}

function calendarLinks(scheduleId) {
  // two paths on purpose: the .ics is what a phone handles natively, the
  // Google link is what actually works in a desktop browser
  const wrap = document.createElement("span");
  wrap.style.cssText = "display:flex; gap:4px;";
  const mk = (href, label, title) => {
    const a = document.createElement("a");
    a.href = href;
    a.className = "act";
    a.style.cssText = "background:var(--muted); text-decoration:none;";
    a.textContent = label;
    a.title = title;
    return a;
  };
  wrap.append(
    mk(`/api/calendar/event/${scheduleId}.ics`, "📅 קובץ",
       "קובץ יומן — כולל את התזכורות והמיקום שהגדרת. בטלפון נפתח בבורר היומנים"),
  );
  const g = mk(`/api/calendar/event/${scheduleId}/google`, "🗓️ Google",
               "פותח אירוע מוכן ב-Google Calendar. שים לב: גוגל לא מקבלת " +
               "תזכורות דרך קישור — תחול ברירת המחדל של היומן שלך");
  g.target = "_blank";
  g.rel = "noopener";
  wrap.append(g);
  return wrap;
}

function toastCalendar(text, icsUrl, googleUrl) {
  // booking succeeded — offer both calendar routes while the toast is up
  const t = $("#toast");
  t.textContent = text + "  ";
  const link = (href, label, blank) => {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = label;
    a.style.cssText = "color:inherit; text-decoration:underline; font-weight:600; margin-inline-start:8px;";
    if (blank) { a.target = "_blank"; a.rel = "noopener"; }
    return a;
  };
  t.appendChild(link(icsUrl, "📅 קובץ"));
  if (googleUrl) t.appendChild(link(googleUrl, "🗓️ Google", true));
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 15000);
}

function toast(text, ms = 3500) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms);
}

/* ---------------------------------------------------------------- views */

const VIEW_PATHS = { schedule: "/schedule", mine: "/mine", rules: "/automations",
                     journal: "/journal", system: "/system", settings: "/settings" };

function viewFromPath() {
  const hit = Object.entries(VIEW_PATHS).find(([, p]) => p === location.pathname);
  return hit ? hit[0] : "schedule";
}

function initScheduleFromURL() {
  const q = new URLSearchParams(location.search);
  const mode = q.get("mode");
  if (["day", "week", "month"].includes(mode)) state.calMode = mode;
  const d = q.get("d");
  if (d && /^\d{4}-\d{2}-\d{2}$/.test(d)) {
    const parsed = new Date(`${d}T00:00`);
    if (!isNaN(parsed)) state.weekStart = parsed;
  }
  setCalModeUI();
}

function showView(name, push = true) {
  state.view = name;
  $$(".view").forEach((v) => { v.hidden = true; });
  $(`#view-${name}`).hidden = false;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  // the URL mirrors the view, so refresh and back/forward keep your place
  // leaving the schedule must also drop its ?mode=&d= query
  if (push && (location.pathname !== VIEW_PATHS[name] ||
               (name !== "schedule" && location.search))) {
    history.pushState({ view: name }, "", VIEW_PATHS[name]);
  }
  if (name === "schedule") loadSchedule();
  if (name === "mine") loadMine();
  if (name === "rules") loadRules();
  if (name === "journal") loadJournal();
  if (name === "system") loadLog();
  if (name === "settings") { loadSettings(); showPane(state.settingsPane); }
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".subtab").forEach((b) =>
    b.addEventListener("click", () => showPane(b.dataset.pane)));
  const addBlocked = () => {
    const v = $("#blockedNew").value.trim();
    if (!v || state.blocked.includes(v)) { $("#blockedNew").value = ""; return; }
    state.blocked = [...state.blocked, v];
    $("#blockedNew").value = "";
    renderBlockedList();
  };
  $("#blockedAdd").addEventListener("click", addBlocked);
  $("#backfillBtn").addEventListener("click", async () => {
    const btn = $("#backfillBtn"), msg = $("#backfillMsg");
    btn.disabled = true;
    msg.textContent = "מייבא… (עד דקה)";
    try {
      const r = await api("/api/history/backfill", { method: "POST" });
      msg.textContent = `יובאו ${r.inserted} שיעורים (${r.fetched} נסרקו)` +
        (r.already_ran ? " · ריצה חוזרת" : "");
    } catch (e) {
      msg.textContent = "נכשל: " + e.message;
    }
    btn.disabled = false;
  });
  $("#blockedNew").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addBlocked(); }
  });
});

window.addEventListener("popstate", () => {
  const v = viewFromPath();
  if (v === "schedule") initScheduleFromURL();
  showView(v, false);
});

$$(".tab").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
$$("[data-journal-mode]").forEach((b) => b.addEventListener("click", () => {
  state.journalMode = b.dataset.journalMode;
  state.journalFocus = null;
  renderJournal();
}));
$("#journalSearch").addEventListener("input", (event) => {
  state.journalFilters.search = event.target.value; renderJournal();
});
for (const [selector, key] of [["#journalPeriod", "period"], ["#journalExercise", "exercise"],
                               ["#journalFeedbackFilter", "feedback"]]) {
  $(selector).addEventListener("change", (event) => {
    state.journalFilters[key] = event.target.value; renderJournal();
  });
}
$("#journalClearFilters").addEventListener("click", () => {
  state.journalFilters = {
    search: "", period: "all", category: "", coach: "", exercise: "", feedback: "",
  };
  $("#journalSearch").value = "";
  $("#journalPeriod").value = "all";
  populateJournalFilters();
  $("#journalFeedbackFilter").value = "";
  renderJournal();
});

/* ---------------------------------------------------------------- boot */

/** The server did not answer. Say so on the page, and offer a way back.
    Every view starts hidden, so a toast alone left an empty screen: during a
    restart or a redeploy the page just went blank with nothing to press. */
function showBootError(err) {
  $$(".view").forEach((v) => { v.hidden = true; });
  let box = $("#bootError");
  if (!box) {
    box = document.createElement("div");
    box.id = "bootError";
    box.className = "card";
    document.querySelector("main").appendChild(box);
  }
  box.hidden = false;
  box.innerHTML = "";
  const h = document.createElement("h2");
  h.textContent = "השרת לא מגיב";
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = (err && err.message ? err.message + " · " : "") +
    "אם השרת מתעדכן כרגע, זה ייקח כמה שניות. אנסה שוב אוטומטית.";
  const b = document.createElement("button");
  b.className = "rbtn";
  b.textContent = "נסה שוב";
  b.onclick = () => { box.hidden = true; boot(); };
  box.append(h, p, b);
  clearTimeout(showBootError._t);
  showBootError._t = setTimeout(() => { box.hidden = true; boot(); }, 5000);
}

async function boot() {
  let health;
  try { health = await api("/api/health"); }
  catch (e) { showBootError(e); return; }
  state.serverTz = health.timezone || undefined;
  showFixedStudio(health.studio);

  if (!health.configured) {
    $$(".view").forEach((v) => { v.hidden = true; });
    $("#view-setup").hidden = false;
    return;
  }
  if (!state.apiKey) {
    $$(".view").forEach((v) => { v.hidden = true; });
    $("#view-key").hidden = false;
    return;
  }
  loadFacets(); // fills the chips when it lands; schedule doesn't wait on it
  await loadStudioSwitch(); // Establish the studio before starting context-bound reads.
  initScheduleFromURL();   // restore ?mode=&d= before the first render
  showView(viewFromPath(), false);
  window.dispatchEvent(new CustomEvent("arbox:ready", { detail: health }));
}

$("#setupBtn").addEventListener("click", async () => {
  const msg = $("#setupMsg");
  msg.classList.remove("error");
  msg.textContent = "מתחבר…";
  try {
    const r = await api("/api/setup", {
      method: "POST",
      body: JSON.stringify({
        whitelabel: $("#setupWhitelabel").value.trim() || "Arbox",
        email: $("#setupEmail").value.trim(),
        password: $("#setupPassword").value,
      }),
    });
    state.apiKey = r.api_key;
    try { localStorage.setItem("arbox_api_key", r.api_key); } catch (e) {}
    msg.textContent = "מחובר! מושך לוח שיעורים…";
    setTimeout(boot, 1500);
  } catch (e) {
    msg.classList.add("error");
    msg.textContent = "נכשל: " + e.message;
  }
});

$("#keyBtn").addEventListener("click", () => {
  state.apiKey = $("#keyInput").value.trim();
  try { localStorage.setItem("arbox_api_key", state.apiKey); } catch (e) {}
  boot();
});
$("#keySkip").addEventListener("click", async () => {
  await loadFacets();
  initScheduleFromURL();
  showView(viewFromPath(), false);
});

/* ------------------------------------------------------------- schedule */

async function loadFacets() {
  try { state.facets = await api("/api/facets"); } catch (e) {}
  renderFacetChips();
}

// Boundaries picked around this studio's actual grid (08:00-20:15):
// a class belongs to the part its START falls in.
const DAYPARTS = [
  { value: "morning",   label: "🌅 בוקר",    from: "00:00", to: "11:59" },
  { value: "noon",      label: "☀️ צהריים",  from: "12:00", to: "15:59" },
  { value: "afternoon", label: "🌇 אחה״צ",  from: "16:00", to: "18:59" },
  { value: "evening",   label: "🌙 ערב",     from: "19:00", to: "23:59" },
];

function inDaypart(s, part) {
  const p = DAYPARTS.find((x) => x.value === part);
  if (!p) return true;
  const t = (s.start_time || "").slice(0, 5);
  return t >= p.from && t <= p.to;
}

function renderFacetChips() {
  const facets = state.facets || { coaches: [], categories: [] };
  renderChips($("#daypartChips"),
    DAYPARTS.map((p) => ({ label: p.label, value: p.value })),
    // daypart is filtered here in the browser and never sent to the server,
    // so re-rendering from what we already hold is the whole operation
    state.daypart, (v) => { state.daypart = v; renderFacetChips(); renderSchedule(); });
  renderFacetSelect($('#coachChips'), 'מאמן/ת', facets.coaches, state.coach,
    v => { state.coach = v; renderSchedule(); });
  renderFacetSelect($('#categoryChips'), 'סוג שיעור', facets.categories, state.category,
    v => { state.category = v; renderSchedule(); });
}

function renderChips(el, items, active, onPick) {
  el.innerHTML = "";
  const all = document.createElement("button");
  all.className = "chip" + (active === null ? " active" : "");
  all.textContent = "הכל";
  all.onclick = () => onPick(null);
  el.appendChild(all);
  for (const it of items) {
    const b = document.createElement("button");
    b.className = "chip" + (active === it.value ? " active" : "");
    b.textContent = it.label;
    if (it.color) b.style.borderColor = it.color;
    b.onclick = () => onPick(active === it.value ? null : it.value);
    el.appendChild(b);
  }
}

function renderDayColumn(d, sessions, todayIso) {
  const day = document.createElement("div");
  day.className = "day";
  const hd = document.createElement("div");
  hd.className = "day-header" + (iso(d) === todayIso ? " today" : "");
  hd.textContent = `${WEEKDAYS_HE[d.getDay()]} · ${fmtDate(d)}`;
  day.appendChild(hd);
  if (!sessions.length) {
    const e = document.createElement("div");
    e.className = "empty-day";
    e.textContent = "אין שיעורים";
    day.appendChild(e);
  }
  for (const s of sessions) day.appendChild(renderSession(s));
  return day;
}

function gotoDay(d) {
  state.calMode = "day";
  state.weekStart = new Date(d);
  setCalModeUI();
  loadSchedule();
}

function calendarStatus(s) {
  if (s.user_in_standby != null && s.user_booked == null) return {cls:'standby',title:'בהמתנה',icon:'◷'};
  if (s.user_booked == null && s.planning && s.planning.state !== 'ready') return {cls:'review',title:'דורש בדיקה',icon:'⚠'};
  if (s.user_booked != null) return {
    icon: "✓", cls: "booked", title: "רשום/ה לשיעור",
  };
  if (s.watched || state.watchlist.has(s.schedule_id)) return {
    icon: "⏳", cls: "scheduled", title: "הרשמה מתוזמנת",
  };
  if (s.automation_skipped) return {
    icon: "⏭️", cls: "vacation", title: "דולג רק במועד הזה — אפשר להחזיר דרך שלי",
  };
  if (s.autobook_blocked_by_vacation) return {
    icon: "🏖️", cls: "vacation",
    title: "לא יוזמן אוטומטית — התאריך נמצא בחופשה",
  };
  if (s.autobook_match || s.planning_source === "autobook") return {
    icon: "🤖", cls: "autobook",
    title: `מיועד להזמנה אוטומטית${(s.autobook_rule_names || []).length ?
      " · " + s.autobook_rule_names.join(", ") : ""}`,
  };
  return null;
}

function monthPreviewSessions(sessions, limit = 3) {
  // A month cell is a personal overview, not a miniature copy of the public
  // timetable. Always surface the classes that carry a personal state first;
  // otherwise a late class that is scheduled or automatic disappears behind
  // three ordinary morning classes and looks as if it was never configured.
  // Keep the chosen rows in their original time order so the cell still scans
  // naturally from morning to evening.
  const indexed = sessions.map((session, index) => ({
    session, index, status: calendarStatus(session),
  }));
  const chosen = indexed.filter((item) => item.status).slice(0, limit);
  if (chosen.length < limit) {
    chosen.push(...indexed.filter((item) => !item.status)
      .slice(0, limit - chosen.length));
  }
  return chosen.sort((a, b) => a.index - b.index)
    .map((item) => item.session);
}

function renderMonthGrid(grid, byDate, anchor) {
  const wrap = document.createElement("div");
  wrap.className = "month-grid";
  // grid auto-placement follows the document's RTL, so ראשון lands on the
  // right with no direction tricks — do not set direction:ltr here
  for (const name of WEEKDAYS_HE) {
    const h = document.createElement("div");
    h.className = "mg-head";
    h.textContent = name;
    wrap.appendChild(h);
  }
  const first = startOfMonth(anchor);
  const todayIso = studioToday();
  for (let i = 0; i < first.getDay(); i++) {
    const pad = document.createElement("div");
    pad.className = "mg-cell mg-out";
    wrap.appendChild(pad);
  }
  const daysIn = addDays(addMonths(first, 1), -1).getDate();
  for (let dd = 1; dd <= daysIn; dd++) {
    const d = addDays(first, dd - 1);
    const dIso = iso(d);
    const sessions = byDate[dIso] || [];
    const cell = document.createElement("div");
    cell.className = "mg-cell" +
      (dIso === todayIso ? " mg-today" : dIso < todayIso ? " mg-past" : "");
    cell.onclick = () => gotoDay(d);
    const num = document.createElement("div");
    num.className = "mg-num";
    num.textContent = String(dd);
    cell.appendChild(num);
    const preview = monthPreviewSessions(sessions);
    for (const s of preview) {
      const it = document.createElement("button");
      it.className = "mg-item";
      it.type = "button";
      it.style.borderInlineStartColor = s.category_color || "var(--line)";
      const status = calendarStatus(s);
      it.textContent = `${status ? status.icon + " " : ""}` +
        `${(s.start_time || "").slice(0, 5)} ${s.category_name || ""}`;
      it.classList.toggle("has-status", Boolean(status));
      if (status) it.classList.add(`status-${status.cls}`);
      it.title = `${s.category_name || ""}${s.coach_name ? " · " + s.coach_name : ""}` +
        (status ? `\n${status.title}` : "");
      // navigation, not action — booking happens in the day view's full card
      it.onclick = (e) => { e.stopPropagation(); gotoDay(d); };
      cell.appendChild(it);
    }
    if (sessions.length > preview.length) {
      const more = document.createElement("div");
      more.className = "mg-more";
      more.textContent = `+${sessions.length - preview.length} עוד`;
      cell.appendChild(more);
    }
    // phone form: the cell is too narrow for text, so a count plus one dot
    // per category is rendered too — CSS decides which of the two shows
    if (sessions.length) {
      const compact = document.createElement("div");
      compact.className = "mg-compact";
      const seen = new Set();
      for (const s of sessions) {
        if (seen.has(s.category_color) || seen.size >= 4) continue;
        seen.add(s.category_color);
        const dot = document.createElement("span");
        dot.className = "mg-dot";
        // backgroundColor, not the `background` shorthand: the shorthand
        // accepts url(), so a category colour set upstream could make every
        // viewer's browser call out to a third party
        dot.style.backgroundColor = s.category_color || "var(--muted)";
        compact.appendChild(dot);
      }
      const statuses = [...new Map(sessions.map((s) => calendarStatus(s))
        .filter(Boolean).map((x) => [x.cls, x])).values()];
      for (const status of statuses.slice(0, 3)) {
        const mark = document.createElement("span");
        mark.className = `mg-compact-status status-${status.cls}`;
        mark.textContent = status.icon;
        mark.title = status.title;
        compact.prepend(mark);
      }
      compact.append(String(sessions.length));
      cell.appendChild(compact);
    }
    wrap.appendChild(cell);
  }
  grid.appendChild(wrap);
}

function updateScheduleURL() {
  if (state.view !== "schedule") return;
  // replaceState on purpose: browsing months must not bloat back-history
  history.replaceState(history.state, "",
    `/schedule?mode=${state.calMode}&d=${iso(state.weekStart)}`);
}

async function loadSchedule(refresh = false) {
  // Only the newest call may render. Clicking "next" twice on a slow link
  // let the first response land last, painting week N+1's sessions under
  // week N+2's headers — which looks like a week with no classes at all.
  const seq = (loadSchedule._seq = (loadSchedule._seq || 0) + 1);
  const { from, to } = visibleRange(state.calMode, state.weekStart);
  $("#weekLabel").textContent = rangeLabel(state.calMode, state.weekStart);
  const params = new URLSearchParams({ date_from: iso(from), date_to: iso(to) });
  if (refresh) params.set("refresh", "1");
  let data;
  try { data = await api("/api/schedule?" + params); }
  catch (e) { toast("שגיאה בטעינה: " + e.message); return; }
  if (seq !== loadSchedule._seq) return;
  await ensureMemberships();

  // the schedule already carries a `watched` flag per row, so the separate
  // watchlist round-trip this used to await before every render was both
  // redundant and serialised in front of the data the user is waiting for
  state.watchlist = new Set(
    data.sessions.filter((s) => s.watched).map((s) => s.schedule_id));
  state.lastSchedule = data;

  $("#syncInfo").textContent = data.last_sync
    ? "עודכן " + fmtSince(data.last_sync) : "";

  // entering with stale data (>15 min) -> refresh from Arbox once, automatically.
  // Only with a key: making the server call Arbox is an action, not a view, so
  // a view-only browser would just collect a 401 and a toast it cannot act on.
  if (state.apiKey && !state.autoRefreshed && data.last_sync &&
      Date.now() - new Date(data.last_sync).getTime() > 15 * 60 * 1000) {
    state.autoRefreshed = true;
    refreshNow(null);
  }

  renderSchedule();
}

/** Paint the grid from the sessions already in hand. */
function renderSchedule() {
  const data = state.lastSchedule;
  if (!data) return;
  const { from, to } = visibleRange(state.calMode, state.weekStart);
  const byDate = {};
  const selected = value => Array.isArray(value) ? value : value ? [value] : [];
  for (const s of data.sessions) {
    if (selected(state.coach).length && !selected(state.coach).includes(s.coach_name)) continue;
    if (selected(state.category).length && !selected(state.category).includes(s.category_name)) continue;
    if (state.daypart && !inDaypart(s, state.daypart)) continue;
    (byDate[s.date] ||= []).push(s);
  }

  const grid = $("#scheduleGrid");
  grid.innerHTML = "";
  grid.classList.toggle("grid--day", state.calMode === "day");
  grid.classList.toggle("grid--month", state.calMode === "month");
  const todayIso = studioToday();

  if (state.calMode === "month") {
    renderMonthGrid(grid, byDate, state.weekStart);
    const horizon = addDays(new Date(), 14);
    if (to > horizon) {
      const bar = document.createElement("div");
      bar.className = "month-fetch";
      const b = document.createElement("button");
      b.className = "rbtn";
      b.textContent = "רענן את החודש הזה מול Arbox";
      b.onclick = () => {
        const lo = new Date() > from ? new Date() : from;
        const hi62 = addDays(new Date(), 62);
        refreshNow({ date_from: iso(lo), date_to: iso(to < hi62 ? to : hi62) }, b);
      };
      bar.appendChild(b);
      grid.appendChild(bar);
    }
    if (iso(from) < iso(addDays(new Date(), -30))) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = "ימים ריקים בתחילת החודש = מחוץ לחלון השמירה";
      grid.appendChild(hint);
    }
  } else if (state.calMode === "day") {
    grid.appendChild(renderDayColumn(new Date(state.weekStart),
                                     byDate[iso(state.weekStart)] || [], todayIso));
  } else {
    const start = startOfWeek(state.weekStart);
    for (let i = 0; i < 7; i++) {
      const d = addDays(start, i);
      grid.appendChild(renderDayColumn(d, byDate[iso(d)] || [], todayIso));
    }
  }
  updateScheduleURL();
}

function renderSession(s) {
  const el = document.createElement("div");
  el.className = "session" + (s.booking_option === "past" ? " past" : "");
  if (s.category_color) el.style.borderInlineStartColor = s.category_color;
  const personal = calendarStatus(s);
  if (personal) el.classList.add(`schedule-${personal.cls}`);

  const row1 = document.createElement("div");
  row1.className = "row";
  const time = document.createElement("span");
  time.className = "time";
  time.dir = "ltr"; // keep 08:00–09:15 reading left-to-right inside the RTL card
  time.textContent = `${s.start_time}–${s.end_time || ""}`;
  row1.appendChild(time);
  const right = document.createElement("span");
  right.style.cssText = "display:flex; align-items:center; gap:6px;";
  // always offered — a hidden button reads as a bug when the studio simply
  // hasn't written a description for that class
  if (s.category_bio) el.title = s.category_bio; // desktop hover bonus
  const info = document.createElement("button");
  info.className = "info-btn";
  info.textContent = "ℹ️";
  info.style.opacity = s.category_bio ? "1" : ".45";
  info.setAttribute("aria-label", "תיאור השיעור");
  info.onclick = () => {
    let bio = el.querySelector(".bio");
    if (bio) { bio.remove(); return; }
    bio = document.createElement("div");
    bio.className = "bio";
    bio.textContent = s.category_bio ||
      "הסטודיו לא הוסיף תיאור לשיעור הזה 🤷";
    el.appendChild(bio);
  };
  right.appendChild(info);
  const badge = statusBadge(s);
  if (badge) right.appendChild(badge);
  row1.appendChild(right);
  el.appendChild(row1);

  const cat = document.createElement("div");
  cat.className = "cat";
  cat.textContent = s.category_name || "שיעור";
  el.appendChild(cat);

  if (s.coach_name) {
    const c = document.createElement("div");
    c.className = "coach";
    c.textContent = s.coach_name;
    el.appendChild(c);
  }

  const row2 = document.createElement("div");
  row2.className = "row";
  const cap = document.createElement("span");
  cap.className = "cap";
  cap.textContent = `${s.registered ?? "?"}/${s.max_users ?? "?"} רשומים` +
    (s.free > 0 ? ` · ${s.free} פנויים` : "");
  row2.appendChild(cap);
  const btns = document.createElement("span");
  btns.style.cssText = "display:flex; gap:4px;";
  const auto = document.createElement("button");
  auto.className = "info-btn";
  auto.textContent = "🔁";
  auto.title = "צור אוטומציה חוזרת מהשיעור הזה (כל שיעור דומה בעתיד — לא רק זה)";
  auto.onclick = () => startAutomationFromSession(s);
  btns.appendChild(auto);
  const act = actionButton(s);
  const pick = membershipPicker(s, act);
  if (pick) btns.appendChild(pick);
  if (act) btns.appendChild(act);
  row2.appendChild(btns);
  el.appendChild(row2);
  return el;
}

// Where the pin is displayed as a status rather than as the button's own
// label. "My classes" lists commitments, so every row there reads badge=state
// / button=action; the schedule has no badge column, so there the button
// carries the state instead. One predicate, so the two can never disagree and
// render both a "מתוזמן ⏳" badge and a "✓ מתוזמן" button on the same card.
const pinIsStatus = () => state.view === "mine";

function membershipPicker(s, act) {
  const eligible = eligibleMemberships(s);
  if (!act || eligible.length < 2 ||
      ["cancelScheduleUser", "cancelWaitList"].includes(s.booking_option)) return null;
  const pick = document.createElement("button");
  const chosen = state.membershipOverrides.get(s.schedule_id);
  pick.className = "info-btn";
  pick.textContent = chosen ? "🎟️✓" : "🎟️";
  pick.title = chosen
    ? `לאימון הזה: ${membershipName(chosen)} — לחיצה לשינוי`
    : `ברירת מחדל: ${membershipName(defaultMembershipForSession(s)?.id)} — לחיצה לבחירת מנוי אחר`;
  pick.onclick = async () => {
    const studio = state.selectedStudioId;
    const before = state.membershipOverrides.get(s.schedule_id);
    if (!await chooseSessionMembership(s)) return;
    if (studio !== state.selectedStudioId) { toast('הסטודיו השתנה. פתחו את האימון מחדש'); return; }
    const selected = state.membershipOverrides.get(s.schedule_id) || null;
    try {
      if (state.watchlist.has(s.schedule_id) || s.watched) {
        await api(`/api/watchlist/${s.schedule_id}/membership`, {
          method: "PUT", headers: {'X-Arbox-Studio-Id':String(studio)}, body: JSON.stringify({ membership_user_id: selected }),
        });
      }
      toast(selected ? `האימון ישתמש ב-${membershipName(selected)}` :
            "האימון חזר לברירת המחדל");
      if (state.view === "mine") loadMine(); else renderSchedule();
    } catch (e) {
      if (before) state.membershipOverrides.set(s.schedule_id, before);
      else state.membershipOverrides.delete(s.schedule_id);
      toast("נכשל: " + e.message);
    }
  };
  return pick;
}

function statusBadge(s) {
  const b = document.createElement("span");
  b.className = "badge";
  if (s.planning && s.planning.state !== 'ready' && s.user_booked == null && s.user_in_standby == null) {
    b.className += ' blocked'; b.textContent = '⚠ דורש בדיקה'; b.title = s.planning.reason; return b;
  }
  if (s.user_booked != null) { b.classList.add("booked"); b.textContent = "רשום ✓"; }
  else if (s.user_in_standby != null) {
    b.classList.add("standby");
    b.textContent = `המתנה${s.stand_by_position ? " · " + s.stand_by_position : ""}`;
  } else if (pinIsStatus() && (s.watched || state.watchlist.has(s.schedule_id))) {
    b.classList.add("scheduled");
    b.textContent = "מתוזמן ⏳";
  } else if (s.automation_skipped) {
    b.classList.add("full");
    b.textContent = "⏭️ דולג הפעם";
  } else if (s.autobook_match && !(s.watched || state.watchlist.has(s.schedule_id))) {
    b.classList.add("autobook");
    b.textContent = "🤖 אוטומטי";
    b.title = (s.autobook_rule_names || []).length
      ? `כלל: ${s.autobook_rule_names.join(", ")}` : "מיועד להזמנה אוטומטית";
  } else if (s.booking_option === "insertStandby") {
    b.classList.add("full"); b.textContent = "מלא";
  } else return null;
  return b;
}

function actionButton(s) {
  // One control, five states. A separate bell was a second button doing what
  // this one should: when the window is shut, the same button schedules the
  // booking instead of being replaced by a locked label.
  const mk = (cls, txt, title, onClick) => {
    const b = document.createElement("button");
    b.className = "act " + cls;
    b.textContent = txt;
    if (title) b.title = title;
    b.onclick = onClick;
    return b;
  };
  const note = s.registration_note || "";

  if (s.planning?.state === 'uncertain') {
    const b = mk('blocked', 'נדרש בירור', 'בדקו את מצב ההזמנה בלשונית שלי לפני פעולה נוספת', () => {});
    b.disabled = true; return b;
  }

  if (s.blocked) {
    const b = mk("blocked", "לא זמין", "המנוי שלך לא כולל את הקטגוריה הזו — " +
                 "אפשר להסיר את החסימה בהגדרות ← מתקדם", () => {});
    b.disabled = true;
    return b;
  }

  switch (s.booking_option) {
    case "cancelScheduleUser":
      return mk("cancel", "בטל", "ביטול ההרשמה שלך", (e) => doAction("cancel", s, e.target));
    case "cancelWaitList":
      return mk("cancel", "צא מהמתנה", "יציאה מרשימת ההמתנה",
                (e) => doAction("cancel", s, e.target));
    case "insertScheduleUser":
      if (s.registration_open === false) return scheduledButton(s, "הזמנה");
      return mk("book", "הזמן", "יש מקום — הרשמה מיידית",
                (e) => doAction("book", s, e.target));
    case "insertStandby":
      if (s.registration_open === false) return scheduledButton(s, "המתנה");
      return mk("standby", "המתנה", "השיעור מלא — כניסה לרשימת ההמתנה",
                (e) => doAction("standby", s, e.target));
    default:
      return null;
  }
}

function scheduledButton(s, what) {
  // window still shut: the button queues the action for the opening moment
  const pinned = state.watchlist.has(s.schedule_id) || s.watched;
  const asAction = pinned && pinIsStatus();
  const b = document.createElement("button");
  b.className = "act " + (asAction ? "cancel" : pinned ? "scheduled" : "future");
  b.textContent = asAction ? "בטל תזמון" : pinned ? "✓ מתוזמן" : "⏳ תזמן";
  b.title = pinned
    ? `${s.registration_note} — נתפוס ${what} אוטומטית ברגע שייפתח. לחיצה מבטלת`
    : `${s.registration_note} — לחיצה תתזמן ${what} אוטומטית לרגע הפתיחה`;
  b.onclick = () => toggleWatch(s, b);
  return b;
}

async function doAction(kind, s, btn, lateCancel = false, cancelReason = null) {
  if (!state.apiKey) { toast("צריך מפתח API כדי לבצע פעולות"); return; }
  // cancelling is the one irreversible action here, so it does not get the
  // same neutral one-liner as booking: it says what is actually lost
  const when = `${s.category_name || "שיעור"} · ${s.date} ${s.start_time}`;
  if (kind === "cancel" && !cancelReason) {
    cancelReason = await chooseReason(
      s.booking_option === "cancelWaitList" ? "למה לצאת מרשימת ההמתנה?" : "למה לבטל?",
      "הסיבה נשמרת בהיסטוריה שלך בלבד ולא נשלחת ל-Arbox.");
    if (!cancelReason) return;
  }
  const ask = kind === "cancel"
    ? `לבטל את ${when}?\n\nהפעולה לא הפיכה — המקום משתחרר לאחרים, ואם השיעור מלא ייתכן שלא תוכל/י לחזור אליו.`
    : `${kind === "book" ? "להזמין" : "להיכנס להמתנה"} — ${when}?`;
  if (!lateCancel && !confirm(ask)) return;
  btn.disabled = true;
  try {
    const body = { schedule_id: s.schedule_id };
    if (kind !== "cancel" && state.membershipOverrides.has(s.schedule_id)) {
      body.membership_user_id = state.membershipOverrides.get(s.schedule_id);
    }
    if (kind === "cancel") {
      body.late_cancel = lateCancel;
      Object.assign(body, cancelReason);
    }
    const res = await api(`/api/${kind}`, { method: "POST", body: JSON.stringify(body) });
    if (res && res.calendar_url) {
      toastCalendar("הוזמן ✓" + (res.quota_note ? ` · ⚠️ ${res.quota_note}` : ""),
                    res.calendar_url, res.google_url);
    } else {
      let t = kind === "cancel" ? "בוטל ✓" : "נכנסת להמתנה ⏳";
      if (res && res.quota_note) t += ` · ⚠️ ${res.quota_note}`;
      toast(t, res && res.quota_note ? 9000 : 3500);
    }
    if (kind === "cancel") {
      historyData = null;
      if ($("#historyDetails").open) {
        historyData = await api("/api/history");
        renderHistory();
      }
    }
    if (state.view === "mine") loadMine(); else loadSchedule();
  } catch (e) {
    if (String(e.message).startsWith("late_cancel_required")) {
      btn.disabled = false;
      if (confirm("שים לב: הביטול ייחשב ביטול מאוחר (בתוך חלון ה-12 שעות). לבטל בכל זאת?")) {
        return doAction(kind, s, btn, true, cancelReason);
      }
      return;
    }
    toast("נכשל: " + e.message, 6000);
    btn.disabled = false;
  }
}

function navSchedule(dir) {
  const a = state.weekStart;
  state.weekStart = state.calMode === "day" ? addDays(a, dir)
    : state.calMode === "month" ? addMonths(a, dir)
    : addDays(startOfWeek(a), dir * 7);
  loadSchedule();
}
function setCalModeUI() {
  $$(".cal-modes .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === state.calMode));
}
$("#prevWeek").onclick = () => navSchedule(-1);
$("#nextWeek").onclick = () => navSchedule(1);
$("#todayBtn").onclick = () => {
  state.weekStart = state.calMode === "week" ? startOfWeek(new Date()) : new Date();
  loadSchedule();
};
$$(".cal-modes .seg-btn").forEach((b) => b.addEventListener("click", () => {
  state.calMode = b.dataset.mode;
  setCalModeUI();
  loadSchedule();
}));

async function refreshNow(range, btn) {
  // range: {date_from, date_to} for a day/span, or null for the full window
  if (btn) { btn.disabled = true; btn.classList.add("spinning"); }
  try {
    const r = await api("/api/refresh", {
      method: "POST",
      body: JSON.stringify(range || {}),
    });
    if (r.skipped) toast("רוענן ממש עכשיו — אין צורך שוב");
    else toast(`עודכן מול Arbox ✓ (${r.synced_sessions} שיעורים)`);
    await loadFacets();
    await loadSchedule();
    if (state.view === "mine") await loadMine();
  } catch (e) {
    toast("רענון נכשל: " + e.message, 6000);
  }
  if (btn) { btn.disabled = false; btn.classList.remove("spinning"); }
}

$$(".rbtn").forEach((b) => b.addEventListener("click", () => {
  let range = null;
  if (b.dataset.days !== undefined) {
    const d = iso(addDays(new Date(), Number(b.dataset.days)));
    range = { date_from: d, date_to: d };
  } else if (b.dataset.week) {
    // normalised, because the grid draws Sunday-to-Saturday via startOfWeek
    // while state.weekStart can be any day after browsing in day mode: the
    // raw value refreshed a span offset from the one on screen and still
    // reported success, leaving the first half of the week stale.
    const ws = startOfWeek(state.weekStart);
    range = { date_from: iso(ws), date_to: iso(addDays(ws, 6)) };
  }
  refreshNow(range, b); // data-all -> null -> full window
}));

$("#refRange").addEventListener("click", () => {
  const from = $("#refFrom").value, to = $("#refTo").value;
  if (!from || !to) { toast("בחר/י שני תאריכים"); return; }
  if (from > to) { toast("טווח הפוך — ״מ־״ אחרי ״עד״"); return; }
  if (to > iso(addDays(new Date(), 62))) {
    toast("אפשר לרענן עד חודשיים קדימה בלבד", 5000); return;
  }
  refreshNow({ date_from: from, date_to: to }, $("#refRange"));
});

/* ----------------------------------------------------------------- mine */

const membershipUI = () => import('/static/membership-ui.js?v=1');
function openStudioMemberships() {
  state.settingsPane = 'studio'; showView('settings');
}
async function renderFacetSelect(host, text, values, selected, change) {
  const ui = await import('/static/filter-picker.js?v=2');
  const previous=host.querySelector('details');
  const picker = ui.filterPicker({label:text, values, selected, change, open:!!previous?.open, search:previous?.querySelector('input[type=search]')?.value || ''});
  host.replaceChildren(picker);
}

function mountSessionMembership(host, session, studio) {
  const label = document.createElement('label'); label.textContent = 'מנוי לאימון הזה';
  const select = document.createElement('select'); select.append(new Option('בחירה אוטומטית של המערכת', ''));
  for (const member of eligibleMemberships(session)) select.append(new Option(member.plan, member.id));
  select.value = state.membershipOverrides.get(session.schedule_id) || '';
  label.append(select);
  const save = document.createElement('button'); save.textContent = 'עדכון המנוי לאימון';
  save.onclick = async () => {
    if (save.disabled) return;
    if (studio !== state.selectedStudioId) { toast('הסטודיו השתנה. פתחו את האימון מחדש'); return; }
    save.disabled = true;
    try {
      const selected = select.value ? Number(select.value) : null;
      if (session.watched || state.watchlist.has(session.schedule_id))
        await api(`/api/watchlist/${session.schedule_id}/membership`, {method:'PUT', headers:{'X-Arbox-Studio-Id':String(studio)}, body:JSON.stringify({membership_user_id:selected})});
      if (studio !== state.selectedStudioId) return;
      if (selected) state.membershipOverrides.set(session.schedule_id, selected); else state.membershipOverrides.delete(session.schedule_id);
      toast('הבחירה עודכנה לאימון הזה'); await loadMine();
    } catch(e) { toast(e.message); save.disabled = false; }
  };
  host.append(label, save);
}

function confirmChangedWorkout(s) {
  const studio = state.selectedStudioId;
  const b = document.createElement('button'); b.className = 'act future';
  b.textContent = 'אישור האימון המעודכן';
  b.onclick = async () => {
    if (b.disabled || !await confirmSchedule(`${s.planning.reason}\n\nלהשאיר את התכנון לאימון המעודכן?`)) return;
    b.disabled = true;
    try {
      const result = await api(`/api/planning/${s.schedule_id}/confirm-change`, {method:'POST',
        headers:{'X-Arbox-Studio-Id':String(studio)}, body:JSON.stringify({expected_token:s.planning.token})});
      toast(result.planning?.reason || 'התכנון עודכן'); await loadMine(); await loadSchedule();
    } catch(e) { toast(e.message); b.disabled = false; }
  };
  return b;
}
async function renderStudioMemberships(profile) {
  const studio = state.selectedStudioId, host = $('#studioMemberships');
  if (host.dataset.studio === String(studio) && host.querySelector('.mu-editor')) return;
  host.dataset.studio = String(studio);
  host.textContent = 'טוענים מנויים…';
  try {
    const [ui, policies, policyUI] = await Promise.all([membershipUI(), api('/api/membership-policies'), import('/static/membership-policy.js?v=3')]);
    if (studio !== state.selectedStudioId) return;
    host.replaceChildren();
    for (const configured of policies.memberships || []) {
      const data = profile.quota?.memberships?.find(x => x.id === configured.id);
      const member = {...configured, ...data};
      const body = document.createElement('div'); body.append(ui.quotaRow(member));
      if (data?.period_start) { const period = document.createElement('small'); period.className = 'hint'; period.textContent = `${data.period_start} – ${data.period_end}`; body.append(period); }
      const editorHost = document.createElement('div');
      const edit = () => {
        if (editorHost.childElementCount) return;
        const editor = policyUI.policyEditor({member: configured, categories: policies.categories,
          save: async values => {
            if (studio !== state.selectedStudioId) throw new Error('הסטודיו השתנה. פתחו את ההגדרה מחדש');
            await api(`/api/membership-policies/${configured.id}`, {method:'PUT', headers:{'X-Arbox-Studio-Id':String(studio)}, body:JSON.stringify(values)});
            toast('הגדרת המנוי נשמרה');
          }, close: () => { editorHost.replaceChildren(); loadProfile(); }});
        editorHost.append(editor); editor.querySelector('input,button')?.focus();
      };
      body.append(policyUI.policySummary(member, state.apiKey ? edit : null), editorHost);
      host.append(ui.membershipDisclosure(member, body));
    }
    if (!host.childElementCount) host.textContent = 'לא נמצאו מנויים בסטודיו הזה';
  } catch(e) { if (studio === state.selectedStudioId) host.textContent = 'לא ניתן לטעון מנויים: ' + e.message; }
}

async function loadMine() {
  const studio = state.selectedStudioId;
  let data;
  try { data = await api("/api/me"); }
  catch (e) { toast("שגיאה: " + e.message); return; }
  if (studio !== state.selectedStudioId) return;
  // /api/me already marks the pinned rows, so the separate watchlist call
  // this used to wait for was a second round-trip for data already here
  state.watchlist = new Set(
    data.sessions.filter((s) => s.watched).map((s) => s.schedule_id));
  if (data.memberships) state.memberships = data.memberships;
  if (state.memberships.length > 1) await loadWatchlist();
  if (studio !== state.selectedStudioId) return;
  loadMessages(); // non-blocking; fills its own card

  if(state.mineFilterStudio!==studio){state.mineFilters={};state.mineFilterStudio=studio;state.mineSummaryOpen=false;}
  renderMineData(data,studio,Promise.all([api('/api/quota'), membershipUI(), import('/static/filter-picker.js?v=2')]));
}

async function renderMineData(data, studio, quotaLoad) {
  const generation = state.mineRenderGeneration = (state.mineRenderGeneration || 0) + 1;
  const current = () => studio === state.selectedStudioId && generation === state.mineRenderGeneration;
  const quotaHost = $("#membershipCard");
  quotaHost.textContent = 'טוענים מכסות…';
  quotaLoad.then(([quota, ui, filters]) => {
    if (!current()) return;
    const combined=document.createElement('div');combined.className='my-overview';
    combined.append(ui.quotaSummary(quota, openStudioMemberships),filters.workoutSummary(allUpcoming,{filters:state.mineFilters,open:state.mineSummaryOpen,toggle:value=>{state.mineSummaryOpen=value;},change:patch=>{state.mineFilters={...state.mineFilters,...patch};renderMineData(data,studio,quotaLoad);}}));
    quotaHost.replaceChildren(combined);
  }).catch(e => {
    if (!current()) return;
    quotaHost.textContent = 'לא ניתן לטעון מכסות: ' + e.message;
  });

  const list = $("#mineList");
  list.innerHTML = "";
  // compared as studio-local strings: the times in these rows are the
  // studio's wall clock, and parsing them as device-local dates put every
  // class hours out for anyone reading this from another timezone
  const nowStamp = studioNowStamp();
  const allUpcoming = data.sessions.filter((s) =>
    s.planning?.state === 'uncertain' || `${s.date} ${(s.end_time || s.start_time || "").slice(0, 5)}` > nowStamp);
  const filters = await import('/static/filter-picker.js?v=2');
  if(!current())return;
  const fields=$('#mineFilters');
  const old=Object.fromEntries([...fields.querySelectorAll('details')].map(p=>[p.dataset.key,{open:p.open,search:p.querySelector('input[type=search]')?.value||''}]));
  fields.replaceChildren();
  for(const[key,label]of [['category_name','שיעורים'],['coach_name','מאמנים']]){
    const picker=filters.filterPicker({label,key,values:[...new Set(allUpcoming.map(r=>r[key]).filter(Boolean))],selected:state.mineFilters?.[key],...old[key],change:values=>{state.mineFilters={...state.mineFilters,[key]:values};renderMineData(data,studio,quotaLoad);}});picker.dataset.key=key;fields.append(picker);
  }
  const upcoming=allUpcoming.filter(row=>filters.matchesFilters(row,state.mineFilters));
  if (!upcoming.length) {
    const c = document.createElement("div");
    c.className = "card";
    c.textContent = allUpcoming.length ? 'אין אימונים שמתאימים לסינון' : "אין שיעורים קרובים — לא רשומ/ה, לא בהמתנה ולא מתוזמן";
    list.appendChild(c);
    return;
  }
  for (const s of upcoming) {
    const c = document.createElement("div");
    c.className = 'card mine-item';
    const personal = calendarStatus(s);
    if (personal) c.classList.add(`mine-${personal.cls}`);
    const grow = document.createElement("div");
    grow.className = "grow";
    const d = new Date(`${s.date}T00:00`);
    grow.innerHTML = "";
    const t1 = document.createElement("div");
    t1.className = "time";
    const span = document.createElement("span");
    span.dir = "ltr";
    span.textContent = `${s.start_time}–${s.end_time || ""}`;
    t1.append(`${WEEKDAYS_HE[d.getDay()]} ${fmtDate(d)} · `, span);
    const t2 = document.createElement("div");
    t2.textContent = (s.category_name || "") + (s.coach_name ? " · " + s.coach_name : "");
    grow.append(t1, t2);
    if (s.planning && s.planning.state !== 'ready') {
      const note = document.createElement('p');
      note.textContent = [s.planning.reason, s.planning.state === 'session_changed' ? '' : membershipName(s.planning.membership_user_id)].filter(Boolean).join(' · ');
      note.className = 'planning-warning';
      grow.append(note);
      if (s.planning.state === 'session_changed') {
        const paused = document.createElement('small'); paused.textContent = 'התכנון מושהה · נדרש אישור מחדש';
        grow.append(paused, confirmChangedWorkout(s));
      }
    }
    const assigned = s.planning?.membership_user_id ?? s.membership_user_id;
    if (assigned) { const membership = document.createElement('small'); membership.className = 'mine-membership'; membership.textContent = membershipName(assigned); grow.append(membership); }
    if (s.planning?.state === 'uncertain') {
      const studio = state.selectedStudioId;
      for (const [label, confirm_not_booked] of [['בדיקת מצב ההזמנה',false],['בדקתי בארבוקס: האימון לא מוזמן',true]]) {
        const check = document.createElement('button'); check.textContent = label;
        check.onclick = async () => {
          if (confirm_not_booked && !confirm('לחדש את התכנון? יש לאשר רק אחרי שבדקתם בארבוקס שאין הרשמה או המתנה לאימון.')) return;
          check.disabled = true;
          try {
            const result = await api(`/api/planning/${s.schedule_id}/reconcile`, {method:'POST',headers:{'X-Arbox-Studio-Id':String(studio)},body:JSON.stringify({confirm_not_booked})});
            toast(result.quota_note); await loadMine();
          } catch(e) { toast(e.message); check.disabled = false; }
        };
        grow.append(check);
      }
    }

    c.appendChild(grow);
    const badge = statusBadge(s);
    if (badge) c.appendChild(badge);
    if (!s.automation_skipped) c.appendChild(calendarLinks(s.schedule_id));
    const uncertain = s.planning?.state === 'uncertain';
    const act = uncertain ? null : s.planning_source === "autobook" ? occurrenceSkipButton(s) : actionButton(s);
    const pick = uncertain || s.planning_source === "autobook" ? null : membershipPicker(s, act);
    if (act) { if (s.user_booked != null) act.textContent = 'ביטול הרשמה'; c.appendChild(act); }
    if (!['uncertain','session_changed'].includes(s.planning?.state) && s.user_booked == null && s.user_in_standby == null && (pick || s.planning)) {
      const check = document.createElement('details'); check.className = 'mine-check';
      const heading = document.createElement('summary'); heading.textContent = s.planning && s.planning.state !== 'ready' ? 'בדיקה כאן' : 'בחירת מנוי לאימון';
      check.append(heading);
      let loaded = false;
      check.ontoggle = async () => {
        if (!check.open || loaded) return; loaded = true;
        try {
          const [q, ui] = await quotaLoad;
          if (studio !== state.selectedStudioId || !check.isConnected) return;
          check.append(ui.quotaSummary(q));
          if (pick) mountSessionMembership(check, s, studio);
          else { const text = document.createElement('p'); text.textContent = 'המערכת בוחרת מנוי מתאים לאוטומציה. הגדרות השיעורים והמכסה נמצאות בסטודיו.'; check.append(text); }
        } catch(e) { loaded = false; toast(e.message); }
      };
      c.append(check);
    }
    list.appendChild(c);
  }
}

function occurrenceSkipButton(s) {
  const b = document.createElement("button");
  b.className = "act " + (s.automation_skipped ? "future" : "cancel");
  b.textContent = s.automation_skipped ? "החזר אוטומציה למועד הזה" : "דלג על השיעור הזה";
  b.title = "השינוי חל רק על המועד הזה; כלל האוטומציה ממשיך כרגיל";
  b.onclick = async () => {
    if (!state.apiKey) { toast("צריך מפתח API כדי לבצע פעולות"); return; }
    b.disabled = true;
    try {
      await api(`/api/automations/occurrences/${s.schedule_id}/skip`, {
        method: s.automation_skipped ? "DELETE" : "PUT",
      });
      toast(s.automation_skipped ? "המועד הוחזר לאוטומציה" : "נדלג רק על המועד הזה — אפשר להחזיר אותו כאן");
      await loadMine();
    } catch (e) { toast("נכשל: " + e.message); b.disabled = false; }
  };
  return b;
}

/* -------------------------------------------------- training history */
// Fetched on each expand: it is local SQLite only, and a class can cross its
// end time while this page stays open.
let historyData = null;
const histSel = { cat: null, coach: null, period: null,
                  from: null, to: null, outcome: null };

const HIST_PERIODS = [
  { value: 30, label: "30 יום" },
  { value: 90, label: "3 חודשים" },
  { value: 182, label: "חצי שנה" },
  { value: 365, label: "שנה" },
];

// One vocabulary for both halves of the timeline: what happened, in a word.
const OUTCOMES = {
  attended:  { label: "✅ הייתי", cls: "o-attended" },
  pending:   { label: "❔ הגעת?", cls: "o-pending" },
  missed:    { label: "❌ לא הגעתי", cls: "o-failed" },
  cancelled_safe: { label: "🟢 בוטל בזמן", cls: "o-cancelled" },
  cancelled_late: { label: "🟠 ביטול מאוחר", cls: "o-skipped" },
  standby_cancelled: { label: "⚪ יצאתי מהמתנה", cls: "o-cancelled" },
  standby:   { label: "⏳ המתנה", cls: "o-standby" },
  vacation:  { label: "🏖️ לא הוזמן — חופשה", cls: "o-skipped" },
  blocked:   { label: "🚫 דולג — קטגוריה חסומה", cls: "o-skipped" },
  expired:   { label: "⌛ פג — התאריך עבר", cls: "o-skipped" },
  full:      { label: "🚪 התמלא — בלי המתנה", cls: "o-skipped" },
  failed:    { label: "❌ נכשל", cls: "o-failed" },
  other:     { label: "❔ אחר", cls: "o-skipped" },
};

const RESULT_TO_OUTCOME = [
  ["skipped — vacation", "vacation"],
  ["blocked category", "blocked"],
  ["expired — class date passed", "expired"],
  ["full, standby not allowed", "full"],
  ["failed", "failed"],
];

function outcomeOf(result) {
  const hit = RESULT_TO_OUTCOME.find(([k]) => (result || "").startsWith(k));
  return hit ? hit[1] : "other";
}

function histDate(x) {
  // a decision whose class row is gone falls back to when it was decided
  return x.date || (x.at || "").slice(0, 10) || null;
}

function inHistPeriod(x) {
  const d = histDate(x);
  if (!d) return true;
  if (histSel.from && d < histSel.from) return false;
  if (histSel.to && d > histSel.to) return false;
  if (!histSel.from && !histSel.to && histSel.period) {
    return d >= iso(addDays(new Date(), -histSel.period));
  }
  return true;
}

// attended classes and automation decisions are one timeline, not two lists:
// "I trained Monday, was skipped Sunday" only reads in order
function mergedHistory() {
  const d = historyData || {};
  const rows = [
    ...(d.sessions || []).map((s) => ({
      ...s, outcome: s.status || (s.standby_only ? "standby" : "attended"),
    })),
    ...(d.decisions || []).map((x) => ({
      ...x, outcome: outcomeOf(x.result),
    })),
  ];
  return rows.sort((a, b) => {
    const da = `${histDate(a) || ""} ${a.start_time || ""}`;
    const db = `${histDate(b) || ""} ${b.start_time || ""}`;
    return db.localeCompare(da);
  });
}

const EVENT_LABELS = {
  booked: "הוזמן",
  rebooked: "הוזמן מחדש",
  standby_joined: "נכנס להמתנה",
  standby_rejoined: "נכנס שוב להמתנה",
  cancelled_safe: "בוטל בזמן",
  cancelled_late: "ביטול מאוחר",
  standby_cancelled: "יצא מהמתנה",
  attended: "סומן שהגעת",
  missed: "סומן שלא הגעת",
  reason_updated: "סיבת אי-הגעה עודכנה",
};

function eventClock(value) {
  return (value || "").slice(11, 16) || "—";
}

function eventStamp(value) {
  if (!value || value.length < 16) return "—";
  return `${value.slice(8, 10)}.${value.slice(5, 7)} · ${value.slice(11, 16)}`;
}

function eventReason(e) {
  if (!e.reason_code || e.reason_code === "none") return "";
  const value = e.reason_code === "other"
    ? (e.reason_text || "אחר") : (REASON_LABELS[e.reason_code] || e.reason_code);
  return ` · סיבה: ${value}`;
}

function eventEntry(e) {
  if (e.counts_entry === 1) return " · נספר ככניסה";
  if (e.counts_entry === 0) return " · כניסה לא נספרה";
  return "";
}

function eventLead(e) {
  if (!e.date || !e.start_time || !e.occurred_at) return "";
  const start = new Date(`${e.date}T${e.start_time}`);
  const happened = new Date(e.occurred_at.replace(" ", "T"));
  const minutes = Math.max(0, Math.round((start - happened) / 60000));
  if (!Number.isFinite(minutes)) return "";
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  const parts = [];
  if (hours) parts.push(hours === 1 ? "שעה" : `${hours} שעות`);
  if (rest || !hours) parts.push(rest === 1 ? "דקה" : `${rest} דקות`);
  return ` · ${parts.join(" ו־")} לפני האימון`;
}

function timelineFor(x) {
  return ((historyData || {}).events || [])
    .filter((e) => Number(e.schedule_id) === Number(x.schedule_id));
}

function hoverSummary(x, events) {
  if (events.length) {
    return events.map((e) =>
      `${eventStamp(e.occurred_at)} · ${EVENT_LABELS[e.event_type] || e.event_type}`
      + (e.event_type.startsWith("cancelled") ? eventLead(e) : "")
      + eventEntry(e) + eventReason(e)).join("\n");
  }
  if (x.outcome === "vacation") return "לא נוצרה הרשמה · כניסה לא נספרה";
  return OUTCOMES[x.outcome].label;
}

async function setAttendance(x, status) {
  if (!state.apiKey) { toast("צריך מפתח API כדי לעדכן הגעה"); return; }
  let reason = {};
  if (status === "missed") {
    reason = await chooseReason("למה לא הגעת?", "אפשר לשנות את הסימון גם בהמשך.");
    if (!reason) return;
  }
  try {
    await api(`/api/history/${x.schedule_id}/attendance`, {
      method: "PUT", body: JSON.stringify({ status, ...reason }),
    });
    historyData = await api("/api/history");
    renderHistory();
    toast(status === "attended" ? "תודה על העדכון ✓" : "הסיבה נשמרה");
  } catch (e) { toast("נכשל: " + e.message, 6000); }
}

$("#historyDetails").addEventListener("toggle", async (e) => {
  if (!e.target.open) return;
  $("#historyList").textContent = "טוען…";
  try { historyData = await api("/api/history"); }
  catch (err) {
    $("#historyList").textContent = "צריך מפתח API כדי לראות היסטוריה";
    return;
  }
  renderHistory();
});

for (const id of ["histFrom", "histTo"]) {
  $("#" + id).addEventListener("change", () => {
    histSel.from = $("#histFrom").value || null;
    histSel.to = $("#histTo").value || null;
    histSel.period = null;
    if (historyData) renderHistory();
  });
}

function renderHistory() {
  const all = mergedHistory();
  const rows = all.filter((x) =>
    (!histSel.cat || x.category_name === histSel.cat) &&
    (!histSel.coach || x.coach_name === histSel.coach) &&
    (!histSel.outcome || x.outcome === histSel.outcome) &&
    inHistPeriod(x));

  const st = (historyData || {}).stats || {};
  const attended = all.filter((x) => x.outcome === "attended").length;
  const missed = all.filter((x) => x.outcome === "missed").length;
  const cancelled = all.filter((x) => x.outcome.startsWith("cancelled") ||
                                         x.outcome === "standby_cancelled").length;
  $("#historyStats").textContent =
    `${attended} אימונים · ${missed} לא הגעתי · ${cancelled} ביטולים` +
    ((st.by_category || []).length
      ? " · " + st.by_category.slice(0, 3).map((c) => `${c.name} ${c.count}`).join(" · ")
      : "");
  $("#historyDetails").querySelector("summary").textContent =
    `🏋️ היסטוריית אימונים${attended ? ` (${attended})` : ""}`;

  // chips are built from the WHOLE timeline: a coach who only appears in a
  // skip must still be offerable, and a filter must narrow, never orphan
  const cats = [...new Set(all.map((x) => x.category_name).filter(Boolean))];
  const coaches = [...new Set(all.map((x) => x.coach_name).filter(Boolean))];
  const outcomes = [...new Set(all.map((x) => x.outcome))];

  renderChips($("#histOutcomeChips"),
    outcomes.map((o) => ({ value: o, label: OUTCOMES[o].label })),
    histSel.outcome, (v) => { histSel.outcome = v; renderHistory(); });
  renderChips($("#histPeriodChips"), HIST_PERIODS,
    histSel.from || histSel.to ? null : histSel.period,
    (v) => { histSel.period = v; histSel.from = histSel.to = null;
             $("#histFrom").value = $("#histTo").value = ""; renderHistory(); });
  renderChips($("#histCatChips"), cats.map((c) => ({ label: c, value: c })),
    histSel.cat, (v) => { histSel.cat = v; renderHistory(); });
  renderChips($("#histCoachChips"), coaches.map((c) => ({ label: c, value: c })),
    histSel.coach, (v) => { histSel.coach = v; renderHistory(); });

  // a collapsed filter panel must never hide the fact that it is filtering
  const active = [histSel.outcome && OUTCOMES[histSel.outcome].label,
                  histSel.cat, histSel.coach,
                  histSel.from || histSel.to ? "טווח תאריכים"
                    : (HIST_PERIODS.find((p) => p.value === histSel.period) || {}).label,
                 ].filter(Boolean);
  const toggle = $(".hist-filter-toggle");
  toggle.textContent = active.length ? `🔎 מסונן: ${active.join(" · ")}` : "🔎 סינון";
  toggle.classList.toggle("on", active.length > 0);

  const list = $("#historyList");
  list.innerHTML = "";
  if (!rows.length) {
    const e = document.createElement("p");
    e.className = "hint";
    e.textContent = all.length ? "אין רשומות בסינון הזה"
                               : "עדיין אין היסטוריה — היא נבנית מכאן והלאה";
    list.appendChild(e);
    return;
  }
  const todayIso = studioToday();
  for (const x of rows) {
    const row = document.createElement("div");
    row.className = "hist-row " + OUTCOMES[x.outcome].cls;

    const when = document.createElement("span");
    when.className = "hist-when";
    const d = histDate(x);
    if (d) {
      const dte = new Date(`${d}T00:00`);
      const clock = document.createElement("span");
      clock.dir = "ltr";
      clock.textContent = x.end_time
        ? `${(x.start_time || "").slice(0, 5)}–${x.end_time.slice(0, 5)}`
        : (x.start_time || "").slice(0, 5);
      when.append(`${WEEKDAYS_HE[dte.getDay()]} ${fmtDate(dte)} · `, clock);
      if (d > todayIso) {
        const soon = document.createElement("span");
        soon.className = "hist-future";
        soon.textContent = " (עתידי)";
        when.appendChild(soon);
      }
    } else {
      when.textContent = "שיעור שכבר לא בלוח";
    }

    const what = document.createElement("span");
    what.className = "hist-what";
    what.textContent = [x.category_name, x.coach_name].filter(Boolean).join(" · ");

    const tag = document.createElement("button");
    tag.type = "button";
    tag.className = "hist-tag";
    tag.textContent = OUTCOMES[x.outcome].label;
    tag.setAttribute("aria-expanded", "false");

    const detail = document.createElement("div");
    detail.className = "hist-detail";
    detail.hidden = true;
    const events = timelineFor(x);
    tag.title = hoverSummary(x, events);
    tag.onclick = () => {
      detail.hidden = !detail.hidden;
      tag.setAttribute("aria-expanded", String(!detail.hidden));
    };

    if (events.length) {
      for (const e of events) {
        const line = document.createElement("div");
        line.className = "hist-event";
        const clock = document.createElement("span");
        clock.className = "hist-event-time";
        clock.textContent = eventStamp(e.occurred_at);
        line.append(clock,
          ` · ${EVENT_LABELS[e.event_type] || e.event_type}`
          + (e.event_type.startsWith("cancelled") ? eventLead(e) : "")
          + eventEntry(e) + eventReason(e));
        if (e.deadline_at && e.event_type.startsWith("cancelled")) {
          line.append(` · מועד אחרון ${eventClock(e.deadline_at)}`);
        }
        detail.appendChild(line);
      }
    } else {
      const line = document.createElement("div");
      line.className = "hist-event";
      line.textContent = x.outcome === "vacation"
        ? "לא נוצרה הרשמה בגלל החופשה · כניסה לא נספרה"
        : OUTCOMES[x.outcome].label;
      if (x.reason_code) line.textContent += eventReason(x);
      detail.appendChild(line);
    }

    const controls = document.createElement("span");
    controls.className = "hist-actions";
    if (["pending", "attended", "missed"].includes(x.outcome)) {
      if (x.outcome !== "attended") {
        const yes = document.createElement("button");
        yes.textContent = "✅ הייתי";
        yes.onclick = () => setAttendance(x, "attended");
        controls.appendChild(yes);
      }
      if (x.outcome !== "missed") {
        const no = document.createElement("button");
        no.textContent = "❌ לא הגעתי";
        no.onclick = () => setAttendance(x, "missed");
        controls.appendChild(no);
      }
      if (x.outcome === "missed") {
        const edit = document.createElement("button");
        edit.textContent = "שנה סיבה";
        edit.onclick = () => setAttendance(x, "missed");
        controls.appendChild(edit);
      }
    }
    if (controls.childNodes.length) detail.appendChild(controls);

    row.append(when, what, tag, detail);
    list.appendChild(row);
  }
}

let messagesData = [];

function msgItem(m) {
  const item = document.createElement("div");
  item.style.cssText = "padding:8px 0;border-bottom:1px solid var(--line)";
  const when = document.createElement("div");
  when.className = "rule-desc";
  when.textContent = (m.created_at || "").slice(0, 10) + (m.has_read ? "" : " · חדש");
  const body = document.createElement("div");
  body.textContent = m.message || m.subject || "";
  body.style.whiteSpace = "pre-wrap";
  item.append(when, body);
  return item;
}

function renderMessagesList() {
  const q = ($("#msgSearch").value || "").trim().toLowerCase();
  const list = $("#messagesList");
  list.innerHTML = "";
  // free text is the only structure these have, so free text is the filter
  const rest = messagesData.slice(1).filter((m) =>
    !q || `${m.message || ""} ${m.subject || ""}`.toLowerCase().includes(q));
  if (!rest.length) {
    const e = document.createElement("p");
    e.className = "hint";
    e.textContent = q ? "אין הודעות שמכילות את זה" : "אין הודעות קודמות";
    list.appendChild(e);
    return;
  }
  for (const m of rest) list.appendChild(msgItem(m));
}

async function loadMessages() {
  let data;
  try { data = await api("/api/messages"); } catch (e) { return; }
  const card = $("#messagesCard");
  messagesData = data.messages || [];
  if (!messagesData.length) { card.hidden = true; return; }
  card.hidden = false;
  // the newest message is the one that matters — it shows on its own, and
  // the backlog stays folded until asked for
  const latest = $("#latestMessage");
  latest.innerHTML = "";
  latest.appendChild(msgItem(messagesData[0]));
  $("#messagesDetails").hidden = messagesData.length < 2;
  $("#messagesDetails").querySelector("summary").textContent =
    `הודעות קודמות (${Math.max(0, messagesData.length - 1)})`;
  if ($("#messagesDetails").open) renderMessagesList();
  const st = $("#messagesStats");
  st.textContent = data.stats && data.stats.future_classes != null
    ? `📊 אימונים: ${data.stats.past_classes} בעבר · ${data.stats.future_classes} קרובים · ממוצע שבועי ${data.stats.weekly_average}`
    : "";
}

$("#messagesDetails").addEventListener("toggle", (e) => {
  if (e.target.open) renderMessagesList();
});
$("#msgSearch").addEventListener("input", renderMessagesList);

/* ------------------------------------------------------ workout journal */

function journalHasContent(entry) {
  return Boolean(entry.coach_feedback || entry.class_feedback || entry.notes ||
    (entry.exercises || []).length);
}

function journalDate(value) {
  if (!value) return "";
  const d = new Date(`${value}T12:00`);
  return `${WEEKDAYS_HE[d.getDay()]} ${d.getDate()}.${d.getMonth() + 1}`;
}

function feedbackMark(value) {
  return value === "positive" ? "🙂" : value === "neutral" ? "😐" :
    value === "negative" ? "🙁" : value === "not_applicable" ? "—" : "";
}

async function loadJournal() {
  const host = $("#journalContent");
  host.textContent = "טוען…";
  try { state.journalData = await api("/api/journal"); }
  catch (e) { host.textContent = "לא ניתן לטעון את היומן: " + e.message; return; }
  populateJournalFilters();
  renderJournal();
}

function populateJournalFilters() {
  const entries = (state.journalData || {}).entries || [];
  const fill = (selector, values, chosen) => {
    const select = $(selector); select.innerHTML = "";
    const all = document.createElement("option"); all.value = ""; all.textContent = "הכול";
    select.appendChild(all);
    [...new Set(values.filter(Boolean))].sort().forEach((value) => {
      const o = document.createElement("option"); o.value = o.textContent = value; select.appendChild(o);
    });
    select.value = chosen;
  };
  for (const [key, field, selector, label] of [
    ['category', 'category_name', '#journalCategory', 'שיעורים'],
    ['coach', 'coach_name', '#journalCoach', 'מאמנים'],
  ]) {
    renderFacetSelect($(selector), label, entries.filter(r => r[field]).map(r => ({name:r[field],color:key==='category'?r.color:null})),
      state.journalFilters[key], values => {state.journalFilters[key]=values; renderJournal();});
  }
  fill("#journalExercise", entries.flatMap((x) => (x.exercises || []).map((item) => item.name)),
    state.journalFilters.exercise);
}

function filteredJournalEntries(data) {
  const f = state.journalFilters;
  const cutoff = f.period === "all" ? null : (() => {
    const d = new Date(`${studioToday()}T12:00`); d.setDate(d.getDate() - Number(f.period)); return iso(d);
  })();
  const needle = f.search.trim().toLocaleLowerCase("he");
  return (data.entries || []).filter((entry) => {
    if (cutoff && entry.date < cutoff) return false;
    for (const [key, field] of [['category','category_name'],['coach','coach_name']]) {
      const values = Array.isArray(f[key]) ? f[key] : f[key] ? [f[key]] : [];
      if (values.length && !values.includes(entry[field])) return false;
    }
    if (f.exercise && !(entry.exercises || []).some((item) => item.name === f.exercise)) return false;
    if (f.feedback === "documented" && !journalHasContent(entry)) return false;
    if (f.feedback === "not_applicable" && entry.coach_feedback !== "not_applicable" &&
        entry.class_feedback !== "not_applicable") return false;
    if (f.feedback.startsWith("coach_") &&
        entry.coach_feedback !== f.feedback.slice("coach_".length)) return false;
    if (f.feedback.startsWith("class_") &&
        entry.class_feedback !== f.feedback.slice("class_".length)) return false;
    if (needle) {
      const haystack = [entry.category_name, entry.coach_name, entry.notes,
        ...(entry.exercises || []).map((x) => `${x.name} ${x.notes || ""}`)]
        .filter(Boolean).join(" ").toLocaleLowerCase("he");
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

function journalStats(entries) {
  const teachers = new Map(), exercises = new Map();
  for (const entry of entries) {
    if (entry.coach_name) {
      const stat = teachers.get(entry.coach_name) || { name: entry.coach_name, classes: 0,
        positive: 0, neutral: 0, negative: 0, not_applicable: 0 };
      stat.classes++;
      if (Object.hasOwn(stat, entry.coach_feedback)) stat[entry.coach_feedback]++;
      teachers.set(entry.coach_name, stat);
    }
    const exerciseNames = new Set();
    for (const item of entry.exercises || []) {
      const stat = exercises.get(item.name) || { name: item.name, metric_type: item.metric_type,
        sessions: 0, points: [] };
      if (!exerciseNames.has(item.name)) stat.sessions++;
      exerciseNames.add(item.name);
      const repsTotal = item.reps == null ? null : (item.sets || 1) * item.reps;
      stat.points.push({ date: entry.date, weight: item.weight, reps_total: repsTotal,
        volume: repsTotal && item.weight ? repsTotal * item.weight : null,
        duration_seconds: item.duration_seconds, attempts: item.attempts, distance: item.distance });
      exercises.set(item.name, stat);
    }
  }
  for (const stat of exercises.values()) {
    stat.points.sort((a, b) => String(a.date).localeCompare(String(b.date)));
  }
  return { teachers: [...teachers.values()].sort((a,b) => b.classes-a.classes),
    exercise_stats: [...exercises.values()].sort((a,b) => b.sessions-a.sessions) };
}

function exerciseMeasure(item) {
  const parts = [];
  if (item.weight != null) parts.push(`${item.weight} ק״ג`);
  if (item.sets != null && item.reps != null) parts.push(`${item.sets}×${item.reps}`);
  else if (item.reps != null) parts.push(`${item.reps} חזרות`);
  if (item.duration_seconds != null) {
    const seconds = Number(item.duration_seconds);
    parts.push(seconds >= 60 && seconds % 60 === 0 ? `${seconds / 60} דקות` : `${seconds} שניות`);
  }
  if (item.attempts != null) parts.push(`${item.attempts} ניסיונות`);
  if (item.distance != null) parts.push(`${item.distance} מטר`);
  if (item.notes) parts.push(item.notes);
  return parts.join(" · ") || "תועד";
}

function addJournalMarks(host, entry) {
  const addMark = (label, value) => {
    if (!value) return;
    const mark = document.createElement("span"); mark.className = `feedback-mark ${value}`;
    mark.textContent = `${label} ${feedbackMark(value)}`; host.appendChild(mark);
  };
  addMark("מורה", entry.coach_feedback);
  addMark("שיעור", entry.class_feedback);
}

function renderJournalDetail(host, entries, stats) {
  const focus = state.journalFocus;
  const isCoach = focus.type === "coach";
  const matched = entries.filter((entry) => isCoach
    ? entry.coach_name === focus.value
    : (entry.exercises || []).some((item) => item.name === focus.value));
  const summary = document.createElement("div"); summary.className = "journal-drill-head card";
  const back = document.createElement("button"); back.type = "button"; back.className = "journal-back";
  back.textContent = isCoach ? "חזרה לכל המורים" : "חזרה לכל התרגילים";
  back.onclick = () => { state.journalFocus = null; renderJournal(); };
  const titleWrap = document.createElement("div");
  const title = document.createElement("h3"); title.textContent = focus.value;
  const count = document.createElement("p"); count.className = "hint";
  count.textContent = `${matched.length} ${matched.length === 1 ? "אימון" : "אימונים"} בתוצאות הסינון`;
  titleWrap.append(title, count);
  summary.append(back, titleWrap);

  if (isCoach) {
    const coach = stats.teachers.find((item) => item.name === focus.value);
    if (coach) {
      const scores = document.createElement("div"); scores.className = "journal-score journal-drill-score";
      for (const [kind, label, value] of [["positive", "התחברתי", coach.positive],
        ["neutral", "ניטרלי", coach.neutral], ["negative", "פחות", coach.negative],
        ["not_applicable", "לא רלוונטי", coach.not_applicable]]) {
        if (!value && kind === "not_applicable") continue;
        const chip = document.createElement("span"); chip.className = `feedback-mark ${kind}`;
        chip.textContent = `${label} ${value}`; scores.appendChild(chip);
      }
      summary.appendChild(scores);
    }
  } else {
    const exercise = stats.exercise_stats.find((item) => item.name === focus.value);
    if (exercise) {
      const chart = document.createElement("div"); chart.className = "journal-drill-chart";
      const label = document.createElement("span"); label.className = "hint";
      label.textContent = "המגמה לפי המדד שתועד";
      chart.append(label, trendSvg(exercise.points)); summary.appendChild(chart);
    }
  }
  host.appendChild(summary);

  if (!matched.length) {
    const empty = document.createElement("div"); empty.className = "card journal-empty";
    empty.textContent = "אין אימונים שמתאימים לסינון הנוכחי."; host.appendChild(empty); return;
  }
  const list = document.createElement("div"); list.className = "journal-detail-list";
  for (const entry of matched) {
    const card = document.createElement("button"); card.type = "button"; card.className = "journal-detail-card";
    const overall = isCoach ? entry.coach_feedback : (entry.class_feedback || entry.coach_feedback);
    if (overall) card.classList.add(`score-${overall}`);
    const top = document.createElement("div"); top.className = "journal-detail-top";
    const identity = document.createElement("div");
    const when = document.createElement("span"); when.className = "journal-when";
    when.textContent = `${journalDate(entry.date)} · ${(entry.start_time || "").slice(0, 5)}`;
    const name = document.createElement("strong"); name.textContent = entry.category_name || "אימון";
    const coach = document.createElement("span"); coach.className = "hint"; coach.textContent = entry.coach_name || "";
    identity.append(when, name, coach);
    const marks = document.createElement("span"); marks.className = "journal-marks";
    addJournalMarks(marks, entry); top.append(identity, marks); card.appendChild(top);
    if (entry.notes) {
      const notes = document.createElement("p"); notes.className = "journal-detail-notes";
      notes.textContent = entry.notes; card.appendChild(notes);
    }
    const exerciseRows = isCoach ? (entry.exercises || [])
      : (entry.exercises || []).filter((item) => item.name === focus.value);
    if (exerciseRows.length) {
      const rows = document.createElement("div"); rows.className = "journal-detail-exercises";
      for (const item of exerciseRows) {
        const row = document.createElement("span");
        row.textContent = `${item.name} · ${exerciseMeasure(item)}`; rows.appendChild(row);
      }
      card.appendChild(rows);
    }
    if (!entry.notes && !exerciseRows.length) {
      const onlyFeedback = document.createElement("span"); onlyFeedback.className = "hint";
      onlyFeedback.textContent = "לא נוסף פירוט לאימון הזה"; card.appendChild(onlyFeedback);
    }
    card.onclick = () => openJournalEditor(entry); list.appendChild(card);
  }
  host.appendChild(list);
}

function renderJournal() {
  const d = state.journalData || { entries: [], teachers: [], exercise_stats: [] };
  const entries = filteredJournalEntries(d);
  const stats = journalStats(entries);
  const host = $("#journalContent");
  host.innerHTML = "";
  const activeFilters = Object.entries(state.journalFilters).filter(([key, value]) =>
    (Array.isArray(value) ? value.length : value) && !(key === "period" && value === "all")).length;
  const filterSummary = $(".journal-filters summary");
  filterSummary.textContent = activeFilters ? `🔎 סינון · ${activeFilters}` : "🔎 סינון";
  filterSummary.classList.toggle("on", activeFilters > 0);
  $$("[data-journal-mode]").forEach((b) =>
    b.classList.toggle("active", b.dataset.journalMode === state.journalMode));
  if (state.journalFocus && ((state.journalFocus.type === "coach" && state.journalMode !== "coaches") ||
      (state.journalFocus.type === "exercise" && state.journalMode !== "exercises"))) {
    state.journalFocus = null;
  }
  if (state.journalFocus) return renderJournalDetail(host, entries, stats);
  if (state.journalMode === "coaches") return renderJournalCoaches(host, stats);
  if (state.journalMode === "exercises") return renderJournalExercises(host, stats);

  if ((d.settings || {}).level === "off" && !d.entries.some(journalHasContent)) {
    const empty = document.createElement("div");
    empty.className = "card journal-empty";
    const icon = document.createElement("div"); icon.className = "journal-empty-icon"; icon.textContent = "📝";
    const h = document.createElement("h2"); h.textContent = "היומן מחכה לך, בלי להציק";
    const p = document.createElement("p"); p.className = "hint";
    p.textContent = "המעקב כבוי כרגע. אפשר להפעיל שאלה קצרה אחרי אימון, או פשוט לפתוח אימון ולכתוב כשמתאים.";
    const b = document.createElement("button"); b.className = "primary"; b.textContent = "הגדר מעקב";
    b.onclick = () => { state.settingsPane = "tracking"; showView("settings"); };
    empty.append(icon, h, p, b); host.appendChild(empty);
  }
  const list = document.createElement("div"); list.className = "journal-list";
  if (!entries.length && d.entries.length) {
    const empty = document.createElement("div");
    empty.className = "card journal-empty";
    empty.textContent = "אין אימונים שמתאימים לסינון הזה.";
    host.appendChild(empty);
    return;
  }
  for (const entry of entries) {
    const card = document.createElement("button"); card.className = "journal-card"; card.type = "button";
    const overall = entry.class_feedback || entry.coach_feedback;
    if (overall) card.classList.add(`score-${overall}`);
    const when = document.createElement("div"); when.className = "journal-when";
    when.textContent = `${journalDate(entry.date)} · ${(entry.start_time || "").slice(0, 5)}`;
    const title = document.createElement("strong"); title.textContent = entry.category_name || "אימון";
    const coach = document.createElement("span"); coach.className = "hint"; coach.textContent = entry.coach_name || "";
    const marks = document.createElement("span"); marks.className = "journal-marks";
    addJournalMarks(marks, entry);
    if ((entry.exercises || []).length) {
      const ex = document.createElement("span"); ex.textContent = `🏋️ ${entry.exercises.length}`; marks.appendChild(ex);
    }
    if (entry.notes) { const note = document.createElement("span"); note.textContent = "📝"; marks.appendChild(note); }
    if (!marks.childNodes.length) marks.textContent = "＋ תיעוד";
    card.append(when, title, coach, marks);
    card.onclick = () => openJournalEditor(entry);
    list.appendChild(card);
  }
  host.appendChild(list);
}

function renderJournalCoaches(host, data) {
  if (!data.teachers.length) {
    host.appendChild(Object.assign(document.createElement("div"), {
      className: "card journal-empty", textContent: "עוד אין מספיק משוב על מורים.",
    })); return;
  }
  const grid = document.createElement("div"); grid.className = "journal-stat-grid";
  for (const coach of data.teachers) {
    const card = document.createElement("button"); card.type = "button";
    card.className = "card journal-stat journal-stat-action";
    const name = document.createElement("strong"); name.textContent = coach.name;
    const count = document.createElement("span"); count.className = "hint";
    count.textContent = `${coach.classes} אימונים`;
    const score = document.createElement("div"); score.className = "journal-score";
    const scoreParts = [
      ["positive", "התחברתי", coach.positive],
      ["neutral", "ניטרלי", coach.neutral],
      ["negative", "פחות", coach.negative],
    ];
    if (coach.not_applicable) scoreParts.push(["not_applicable", "לא רלוונטי", coach.not_applicable]);
    for (const [kind, label, value] of scoreParts) {
      const chip = document.createElement("span");
      chip.className = `feedback-mark ${kind}`;
      chip.textContent = `${label} ${value}`;
      score.appendChild(chip);
    }
    const open = document.createElement("span"); open.className = "journal-open";
    open.textContent = "כל האימונים";
    card.append(name, count, score, open);
    card.onclick = () => { state.journalFocus = { type: "coach", value: coach.name }; renderJournal(); };
    grid.appendChild(card);
  }
  host.appendChild(grid);
}

function trendSvg(points) {
  const values = points.map((p) => p.weight ?? p.reps_total ?? p.duration_seconds ?? p.attempts ?? p.distance)
    .filter((x) => x != null);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 180 48"); svg.classList.add("sparkline");
  if (!values.length) return svg;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const coords = values.map((v, i) => `${8 + i * 164 / Math.max(1, values.length - 1)},${40 - (v-min)*32/span}`);
  const line = document.createElementNS(svg.namespaceURI, "polyline");
  line.setAttribute("points", coords.join(" ")); line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor"); line.setAttribute("stroke-width", "3");
  line.setAttribute("stroke-linecap", "round"); line.setAttribute("stroke-linejoin", "round");
  svg.appendChild(line); return svg;
}

function renderJournalExercises(host, data) {
  if (!data.exercise_stats.length) {
    host.appendChild(Object.assign(document.createElement("div"), {
      className: "card journal-empty", textContent: "אחרי שתתעד/י תרגיל, ההתקדמות שלו תופיע כאן.",
    })); return;
  }
  const grid = document.createElement("div"); grid.className = "journal-stat-grid";
  for (const ex of data.exercise_stats) {
    const card = document.createElement("button"); card.type = "button";
    card.className = "card journal-stat exercise-stat journal-stat-action";
    const name = document.createElement("strong"); name.textContent = ex.name;
    const meta = document.createElement("span"); meta.className = "hint";
    const last = ex.points[ex.points.length - 1] || {};
    meta.textContent = `${ex.sessions} אימונים` + (last.weight != null ? ` · אחרון ${last.weight} ק״ג` :
      last.reps_total != null ? ` · אחרון ${last.reps_total} חזרות` : "");
    const open = document.createElement("span"); open.className = "journal-open";
    open.textContent = "כל התיעודים";
    card.append(name, meta, trendSvg(ex.points), open);
    card.onclick = () => { state.journalFocus = { type: "exercise", value: ex.name }; renderJournal(); };
    grid.appendChild(card);
  }
  host.appendChild(grid);
}

function addExerciseRow(exercise = {}) {
  const row = document.createElement("div"); row.className = "journal-ex-row";
  row.dataset.exerciseId = exercise.id || exercise.exercise_id || "";
  const name = document.createElement("input"); name.className = "ex-name"; name.placeholder = "תרגיל";
  name.value = exercise.name || "";
  name.dataset.catalogName = exercise.name || "";
  name.oninput = () => {
    if (name.value.trim() !== name.dataset.catalogName) row.dataset.exerciseId = "";
  };
  const metric = document.createElement("select"); metric.className = "ex-metric";
  const labels = (state.journalData || {}).metric_labels || {};
  const shortLabels = { strength: "משקל", reps: "חזרות", duration: "זמן",
    attempts: "ניסיונות", distance: "מרחק", note: "הערה" };
  for (const [value, label] of Object.entries(labels)) {
    const o = document.createElement("option"); o.value = value;
    o.textContent = shortLabels[value] || label; metric.appendChild(o);
  }
  metric.value = exercise.metric_type || "note";
  const num = (cls, placeholder, value) => {
    const x = document.createElement("input"); x.type = "number"; x.min = "0";
    x.className = cls; x.placeholder = placeholder; x.value = value ?? ""; return x;
  };
  const sets = num("ex-sets", "סטים", exercise.sets);
  const reps = num("ex-reps", "חזרות", exercise.reps);
  const weight = num("ex-weight", "ק״ג", exercise.weight); weight.step = "0.5";
  const duration = num("ex-duration", "שניות", exercise.duration_seconds);
  const attempts = num("ex-attempts", "ניסיונות", exercise.attempts);
  const distance = num("ex-distance", "מטרים", exercise.distance); distance.step = "0.1";
  const notes = document.createElement("input"); notes.className = "ex-notes"; notes.placeholder = "הערה";
  notes.value = exercise.notes || "";
  const del = document.createElement("button"); del.type = "button"; del.className = "ex-remove"; del.textContent = "×";
  del.onclick = () => row.remove();
  const refresh = () => {
    const m = metric.value;
    sets.hidden = !["strength", "reps"].includes(m); reps.hidden = sets.hidden;
    weight.hidden = m !== "strength"; duration.hidden = m !== "duration";
    attempts.hidden = m !== "attempts"; distance.hidden = m !== "distance";
  };
  metric.onchange = refresh;
  row.append(name, metric, sets, reps, weight, duration, attempts, distance, notes, del);
  $("#journalExerciseRows").appendChild(row); refresh(); name.focus();
}

function openJournalEditor(entry) {
  state.journalEntry = entry;
  $("#journalDialogTitle").textContent = entry.category_name || "אימון";
  $("#journalDialogMeta").textContent = `${journalDate(entry.date)} · ${(entry.start_time || "").slice(0,5)}` +
    (entry.coach_name ? ` · ${entry.coach_name}` : "");
  $("#journalNotes").value = entry.notes || "";
  $$(".feedback-seg").forEach((seg) => {
    const value = seg.dataset.feedback === "coach" ? entry.coach_feedback : entry.class_feedback;
    seg.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.value === value));
  });
  $("#journalExerciseRows").innerHTML = "";
  for (const ex of entry.exercises || []) addExerciseRow(ex);
  const suggestions = $("#journalSuggestions"); suggestions.innerHTML = "";
  const recent = new Map();
  for (const prior of (state.journalData.entries || [])) for (const ex of prior.exercises || []) {
    if (!recent.has(ex.name)) recent.set(ex.name, ex);
  }
  const ordered = [...recent.values(), ...(state.journalData.catalogue || [])]
    .filter((ex, index, all) => all.findIndex((x) => x.name === ex.name) === index);
  const addSuggestion = (ex) => {
    const b = document.createElement("button"); b.type = "button"; b.className = "chip";
    b.dataset.name = ex.name; b.textContent = `＋ ${ex.name}`; b.onclick = () => addExerciseRow(ex);
    suggestions.appendChild(b);
  };
  ordered.slice(0, 8).forEach(addSuggestion);
  if (ordered.length > 8) {
    const more = document.createElement("button"); more.type = "button"; more.className = "chip muted-chip";
    more.textContent = "עוד תרגילים…";
    more.onclick = () => { more.remove(); ordered.slice(8).forEach(addSuggestion); };
    suggestions.appendChild(more);
  }
  const search = document.createElement("button"); search.type = "button";
  search.className = "chip catalog-search-chip"; search.textContent = "⌕ חיפוש בכל המאגר";
  search.onclick = () => openExerciseCatalog("editor"); suggestions.appendChild(search);
  $("#copyLastWorkout").hidden = !(state.journalData.entries || []).some((x) =>
    x.schedule_id !== entry.schedule_id && x.category_name === entry.category_name && (x.exercises || []).length);
  $("#journalDialogMsg").textContent = "";
  $("#journalDialog").showModal();
}

function collectExerciseRows() {
  return $$(".journal-ex-row").map((row) => ({
    exercise_id: row.dataset.exerciseId || null,
    name: row.querySelector(".ex-name").value.trim(),
    metric_type: row.querySelector(".ex-metric").value,
    sets: Number(row.querySelector(".ex-sets").value) || null,
    reps: Number(row.querySelector(".ex-reps").value) || null,
    weight: Number(row.querySelector(".ex-weight").value) || null,
    weight_unit: "kg", duration_seconds: Number(row.querySelector(".ex-duration").value) || null,
    attempts: Number(row.querySelector(".ex-attempts").value) || null,
    distance: Number(row.querySelector(".ex-distance").value) || null, distance_unit: "m",
    notes: row.querySelector(".ex-notes").value.trim() || null,
  })).filter((x) => x.name);
}

$$(".feedback-seg button").forEach((button) => button.addEventListener("click", () => {
  const wasActive = button.classList.contains("active");
  button.parentElement.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
  if (!wasActive) button.classList.add("active");
}));
$("#journalAddExercise").addEventListener("click", () => addExerciseRow());
$("#copyLastWorkout").addEventListener("click", () => {
  const entry = state.journalEntry;
  const prior = (state.journalData.entries || []).find((x) =>
    x.schedule_id !== entry.schedule_id && x.category_name === entry.category_name && (x.exercises || []).length);
  if (!prior) return;
  $("#journalExerciseRows").innerHTML = "";
  prior.exercises.forEach(addExerciseRow);
  toast("הועתק מהאימון הקודם");
});
$("#journalForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitter = event.submitter;
  if (!submitter || submitter.value !== "save") { $("#journalDialog").close(); return; }
  submitter.disabled = true;
  const selected = (kind) => $(`.feedback-seg[data-feedback="${kind}"] .active`)?.dataset.value || null;
  try {
    await api(`/api/journal/${state.journalEntry.schedule_id}`, {
      method: "PUT", body: JSON.stringify({
        coach_feedback: selected("coach"), class_feedback: selected("class"),
        notes: $("#journalNotes").value.trim() || null,
        exercises: collectExerciseRows(),
      }),
    });
    $("#journalDialog").close();
    toast("נשמר ביומן ✓");
    await loadJournal();
  } catch (e) {
    $("#journalDialogMsg").textContent = "נכשל: " + e.message;
  } finally { submitter.disabled = false; }
});
/* ------------------------------------------------------------------ log */

const LEVELS = [
  { value: "info", label: "הצלחות" },
  { value: "warn", label: "אזהרות" },
  { value: "error", label: "שגיאות" },
];
const SOURCES = [
  { value: "watchlist", label: "תזמונים" },
  { value: "autobook", label: "אוטומציות" },
  { value: "booking", label: "הרשמות" },
  { value: "notify", label: "התראות" },
  { value: "sync", label: "סנכרון" },
  { value: "quota", label: "מכסה" },
  { value: "studio", label: "סטודיו" },
  { value: "vacation", label: "חופשות" },
];
const EV_ICON = {
  watchlist: "🎯", autobook: "🤖", booking: "✋", notify: "📨",
  sync: "🔄", quota: "🎟️", studio: "📣", vacation: "🏖️", system: "⚙️",
};

async function loadLog() {
  const params = new URLSearchParams();
  if (state.logLevel) params.set("level", state.logLevel);
  if (state.logSource) params.set("source", state.logSource);
  let d;
  try { d = await api("/api/events?" + params); }
  catch (e) { toast("שגיאה בטעינת פעילות המערכת: " + e.message); return; }

  renderStatusStrip(d.status);
  renderChips($("#logLevels"), LEVELS.map((l) => ({
    ...l,
    label: l.value && d.counts[l.value] ? `${l.label} ${d.counts[l.value]}` : l.label,
  })), state.logLevel, (v) => { state.logLevel = v; loadLog(); });
  renderChips($("#logSources"), SOURCES,
    state.logSource, (v) => { state.logSource = v; loadLog(); });

  const list = $("#logList");
  list.innerHTML = "";
  if (!d.events.length) {
    const empty = document.createElement("div");
    empty.className = "ev";
    empty.textContent = "אין אירועים בסינון הזה";
    list.appendChild(empty);
    return;
  }
  for (const e of d.events) {
    const row = document.createElement("div");
    row.className = "ev " + e.level;
    const icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = EV_ICON[e.source] || "•";
    const body = document.createElement("div");
    body.className = "body";
    const msg = document.createElement("div");
    msg.className = "msg";
    msg.textContent = e.message;
    body.appendChild(msg);
    if (e.detail) {
      const det = document.createElement("div");
      det.className = "det";
      det.textContent = e.detail;
      body.appendChild(det);
    }
    const when = document.createElement("div");
    when.className = "when";
    // "2026-08-27 23:41:02" -> 23:41, with the date only when it is not today
    const [day, clock] = (e.ts || " ").split(" ");
    when.append(
      day === studioToday() ? (clock || "").slice(0, 5)
                              : `${(day || "").slice(5)} ${(clock || "").slice(0, 5)}`);
    const src = document.createElement("div");
    src.className = "src";
    src.textContent = (SOURCES.find((s) => s.value === e.source) || {}).label || e.source;
    when.appendChild(src);
    row.append(icon, body, when);
    list.appendChild(row);
  }
}

function renderStatusStrip(st) {
  const el = $("#logStatus");
  el.innerHTML = "";
  const cell = (k, v, cls) => {
    const c = document.createElement("div");
    c.className = "status-cell";
    const kk = document.createElement("div"); kk.className = "k"; kk.textContent = k;
    const vv = document.createElement("div"); vv.className = "v " + (cls || "");
    vv.textContent = v;
    c.append(kk, vv);
    el.appendChild(c);
  };
  const age = st.sync_age_minutes;
  cell("סנכרון אחרון",
       age == null ? "לא ידוע"
       : age < 60 ? `לפני ${age} דק׳`
       : `לפני ${Math.round(age / 60)} שע׳`,
       age != null && age < 120 ? "ok" : "bad");
  for (const [name, label] of [["telegram", "טלגרם"], ["ha", "Home Assistant"]]) {
    const c = st.channels[name] || {};
    cell(label,
         c.state === "off" ? "כבוי" : c.state === "ok" ? "תקין" : `נכשל ×${c.failures}`,
         c.state === "off" ? "off" : c.state === "ok" ? "ok" : "bad");
  }
  cell("תזמונים ממתינים", String(st.pending_pins ?? 0));
}

/* ---------------------------------------------------------------- rules */

const ruleSel = { coaches: new Set(), categories: new Set(), weekdays: new Set() };
let editingRuleId = null;
let editingEnabled = true;   // editing a disabled rule must not silently re-enable it
let nameIsAuto = true;       // stop auto-naming once the user types their own

function suggestRuleName(r) {
  // "·" rather than "-": category names contain dashes (Flex- Back\Arches),
  // so a dash separator makes the result unreadable
  const parts = [r.mode === "autobook" ? "🤖 אוטומטי" : "🔔 התראה"];

  const cats = r.categories || [];
  const coaches = r.coaches || [];
  if (cats.length === 1) parts.push(cats[0]);
  else if (cats.length === 2) parts.push(cats.join(", "));
  else if (cats.length > 2) parts.push(`${cats[0]} +${cats.length - 1}`);
  else if (coaches.length) parts.push("כל השיעורים");
  else parts.push("כל השיעורים");

  const days = r.weekdays || [];
  if (!days.length || days.length === 7) parts.push("כל יום");
  else parts.push(RULE_DAYS.filter((d) => days.includes(d.py))
                           .map((d) => d.short).join(" "));

  // the coach is already the subject when no category was picked
  if (coaches.length && cats.length) {
    parts.push(coaches.length === 1 ? coaches[0] : `${coaches.length} מאמנים`);
  } else if (coaches.length && !cats.length) {
    parts.push(coaches.length === 1 ? coaches[0] : `${coaches.length} מאמנים`);
  }

  const from = r.time_from, to = r.time_to;
  if (from || to) parts.push(`${from || "00:00"}-${to || "23:59"}`);
  return parts.join(" · ");
}

function currentRuleDraft() {
  return {
    coaches: [...ruleSel.coaches],
    categories: [...ruleSel.categories],
    weekdays: [...ruleSel.weekdays],
    time_from: $("#ruleFrom").value || null,
    time_to: $("#ruleTo").value || null,
    mode: $("#ruleMode").value,
  };
}

function refreshSuggestedName() {
  if (!nameIsAuto) return;
  $("#ruleName").value = suggestRuleName(currentRuleDraft());
}

function resetRuleForm() {
  editingRuleId = null;
  editingEnabled = true;
  nameIsAuto = true;
  $("#ruleName").value = "";
  $("#ruleFrom").value = "";
  $("#ruleTo").value = "";
  $("#ruleMode").value = "notify";
  ruleSel.coaches.clear(); ruleSel.categories.clear(); ruleSel.weekdays.clear();
  $("#ruleFormTitle").textContent = "אוטומציה חדשה";
  $("#ruleSave").textContent = "שמור אוטומציה";
  $("#ruleCancelEdit").hidden = true;
  $("#ruleMsg").textContent = "";
  refreshSuggestedName();
}

async function startAutomationFromSession(s) {
  // one tap on a class -> the automations form arrives pre-filled with that
  // class's coach, category, weekday and a tight time window; all that's
  // left is picking notify/autobook and saving
  showView("rules");
  await new Promise((r) => setTimeout(r, 250)); // let loadRules render chips
  resetRuleForm();
  ruleSel.categories = new Set(s.category_name ? [s.category_name] : []);
  ruleSel.coaches = new Set(s.coach_name ? [s.coach_name] : []);
  const wd = (new Date(s.date + "T00:00").getDay() + 6) % 7; // JS Sun=0 -> py Mon=0
  ruleSel.weekdays = new Set([wd]);
  $("#ruleFrom").value = s.start_time;
  $("#ruleTo").value = s.end_time || s.start_time;
  renderRuleChips();
  refreshSuggestedName();
  $("#ruleMsg").textContent = "מולא מהשיעור — בחר/י התראה או אוטומטי ושמור/י";
  $("#ruleMode").scrollIntoView({ behavior: "smooth", block: "center" });
}

function startEditRule(r) {
  editingRuleId = r.id;
  editingEnabled = r.enabled;
  // a hand-written name is the user's; a generated one keeps tracking edits
  nameIsAuto = r.name === suggestRuleName(r);
  $("#ruleName").value = r.name;
  $("#ruleFrom").value = r.time_from || "";
  $("#ruleTo").value = r.time_to || "";
  $("#ruleMode").value = r.mode;
  ruleSel.coaches = new Set(r.coaches);
  ruleSel.categories = new Set(r.categories);
  ruleSel.weekdays = new Set(r.weekdays);
  $("#ruleFormTitle").textContent =
    `עריכת אוטומציה: ${r.name}` + (r.enabled ? "" : " (מכובה)");
  $("#ruleSave").textContent = "עדכן אוטומציה";
  $("#ruleCancelEdit").hidden = false;
  $("#ruleMsg").textContent = "";
  renderRuleChips();
  $("#ruleName").scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderMultiChips(el, items, sel, onChange) {
  el.innerHTML = "";
  for (const it of items) {
    const b = document.createElement("button");
    b.className = "chip" + (sel.has(it.value) ? " active" : "");
    b.textContent = it.label;
    b.onclick = () => {
      sel.has(it.value) ? sel.delete(it.value) : sel.add(it.value);
      b.classList.toggle("active");
      if (onChange) onChange();
    };
    el.appendChild(b);
  }
}

function renderRuleChips() {
  for(const[key,label,values]of [['coaches','מאמנים',state.facets.coaches],['categories','שיעורים',state.facets.categories]])
    renderFacetSelect($(key==='coaches'?'#ruleCoaches':'#ruleCategories'),label,values,[...ruleSel[key]],chosen=>{ruleSel[key].clear();chosen.forEach(v=>ruleSel[key].add(v));refreshSuggestedName();});
  renderMultiChips($("#ruleWeekdays"),
    RULE_DAYS.map((d) => ({ label: d.name, value: d.py })), ruleSel.weekdays,
    refreshSuggestedName);
}

let editingVacId = null;

function resetVacForm() {
  editingVacId = null;
  $("#vacFrom").value = ""; $("#vacTo").value = "";
  $("#vacNotify").checked = true; $("#vacAutobook").checked = true;
  $("#vacAdd").textContent = "הוסף חופשה";
  $("#vacCancelEdit").hidden = true;
  $("#vacMsg").textContent = "";
}

function startEditVacation(v) {
  editingVacId = v.id;
  $("#vacFrom").value = v.date_from;
  $("#vacTo").value = v.date_to;
  $("#vacNotify").checked = v.block_notify;
  $("#vacAutobook").checked = v.block_autobook;
  $("#vacAdd").textContent = "עדכן חופשה";
  $("#vacCancelEdit").hidden = false;
  $("#vacMsg").textContent = "";
  $("#vacFrom").scrollIntoView({ behavior: "smooth", block: "center" });
}

$("#vacCancelEdit").addEventListener("click", resetVacForm);

async function loadVacations() {
  let data;
  try { data = await api("/api/vacations"); } catch (e) { return; }
  const mk = (v, withDelete) => {
    const row = document.createElement("div");
    row.className = "rule-item";
    const grow = document.createElement("div");
    grow.className = "grow";
    const span = document.createElement("span");
    span.dir = "ltr";
    span.textContent = `${v.date_from} → ${v.date_to}`;
    const incl = document.createElement("span");
    incl.className = "hint";
    incl.textContent = " (כולל)";
    span.appendChild(incl);
    const what = [v.block_notify ? "בלי הצעות הרשמה" : null,
                  v.block_autobook ? "בלי הזמנה אוטומטית" : null]
                 .filter(Boolean).join(" · ");
    const desc = document.createElement("div");
    desc.className = "rule-desc";
    desc.textContent = what;
    grow.append(span, desc);
    row.appendChild(grow);
    if (withDelete) {
      const edit = document.createElement("button");
      edit.textContent = "✏️";
      edit.title = "ערוך";
      edit.onclick = () => startEditVacation(v);
      row.appendChild(edit);
      const del = document.createElement("button");
      del.textContent = "🗑";
      del.onclick = async () => {
        if (!confirm("לבטל את החופשה הזו? האוטומציות יחזרו לפעול בטווח, " +
                     "ושיעורים שדולגו בגללה יחזרו לתור.")) return;
        try { await api(`/api/vacations/${v.id}`, { method: "DELETE" }); loadVacations(); }
        catch (e) { toast("נכשל: " + e.message, 6000); }
      };
      row.appendChild(del);
    }
    return row;
  };
  const list = $("#vacList");
  list.innerHTML = "";
  if (!data.active.length) {
    const s = document.createElement("p");
    s.className = "hint";
    s.textContent = "אין חופשות מתוכננות";
    list.appendChild(s);
  }
  for (const v of data.active) list.appendChild(mk(v, true));
  const hist = $("#vacHistory");
  hist.innerHTML = "";
  $("#vacHistoryWrap").hidden = !data.history.length;
  for (const v of data.history) hist.appendChild(mk(v, false));
}

$("#vacAdd").addEventListener("click", async () => {
  const msg = $("#vacMsg");
  msg.classList.remove("error");
  const from = $("#vacFrom").value, to = $("#vacTo").value;
  if (!from || !to) { msg.classList.add("error"); msg.textContent = "בחר/י שני תאריכים"; return; }
  try {
    const payload = {
      date_from: from, date_to: to,
      block_notify: $("#vacNotify").checked,
      block_autobook: $("#vacAutobook").checked,
    };
    if (editingVacId) payload.id = editingVacId;
    await api("/api/vacations", { method: "POST", body: JSON.stringify(payload) });
    const was = editingVacId !== null;
    resetVacForm();
    $("#vacMsg").textContent = was ? "עודכנה ✓" : "נוספה ✓";
    loadVacations();
  } catch (e) {
    msg.classList.add("error");
    msg.textContent = "נכשל: " + e.message;
  }
});

async function loadRules() {
  loadVacations();          // independent card; fills itself
  await loadFacets();
  renderRuleChips();
  refreshSuggestedName();   // show a name immediately, before any interaction

  let data;
  try { data = await api("/api/rules"); } catch (e) { return; }
  const list = $("#rulesList");
  list.innerHTML = "";
  for (const r of data.rules) {
    const c = document.createElement("div");
    c.className = "card rule-item";
    const grow = document.createElement("div");
    grow.className = "grow";
    const name = document.createElement("div");
    name.textContent = r.name;
    const desc = document.createElement("div");
    desc.className = "rule-desc";
    const parts = [];
    if (r.coaches.length) parts.push("מאמנ/ת: " + r.coaches.join(", "));
    if (r.categories.length) parts.push(r.categories.join(", "));
    if (r.weekdays.length) parts.push(r.weekdays.map((w) =>
      RULE_DAYS.find((d) => d.py === w)?.name).join(", "));
    if (r.time_from || r.time_to) parts.push(`${r.time_from || ""}–${r.time_to || ""}`);
    desc.textContent = parts.join(" · ") || "כל השיעורים";
    grow.append(name, desc);
    const tag = document.createElement("span");
    tag.className = "mode-tag" + (r.mode === "autobook" ? " autobook" : "");
    tag.textContent = r.mode === "autobook" ? "🤖 אוטומטי" : "🔔 התראה";
    const edit = document.createElement("button");
    edit.textContent = "✏️";
    edit.title = "ערוך";
    edit.onclick = () => startEditRule(r);
    const toggle = document.createElement("button");
    toggle.textContent = r.enabled ? "כבה" : "הפעל";
    toggle.onclick = async () => {
      // caught, like the vacation delete beside it: an unhandled rejection
      // here left the automation visibly in its old state with nothing said,
      // so "I turned it off" and "it is still booking classes" looked alike
      try {
        await api("/api/rules", { method: "POST",
          body: JSON.stringify({ ...r, enabled: !r.enabled }) });
        loadRules();
      } catch (e) { toast("נכשל: " + e.message, 6000); }
    };
    const del = document.createElement("button");
    del.textContent = "🗑";
    del.onclick = async () => {
      if (!confirm(`למחוק את האוטומציה "${r.name}"?`)) return;
      try {
        await api(`/api/rules/${r.id}`, { method: "DELETE" });
        loadRules();
      } catch (e) { toast("נכשל: " + e.message, 6000); }
    };
    c.append(grow, tag, edit, toggle, del);
    if (!r.enabled) c.style.opacity = ".5";
    list.appendChild(c);
  }
}

["#ruleFrom", "#ruleTo", "#ruleMode"].forEach((sel) =>
  $(sel).addEventListener("change", refreshSuggestedName));

$("#ruleName").addEventListener("input", () => {
  // typing takes ownership of the name; clearing hands it back
  nameIsAuto = $("#ruleName").value.trim() === "";
  if (nameIsAuto) refreshSuggestedName();
});

$("#ruleCancelEdit").addEventListener("click", () => { resetRuleForm(); loadRules(); });

$("#ruleSave").onclick = async () => {
  const msg = $("#ruleMsg");
  msg.classList.remove("error");
  const name = $("#ruleName").value.trim();
  if (!name) { msg.classList.add("error"); msg.textContent = "צריך שם לאוטומציה"; return; }
  try {
    const payload = {
      name,
      coaches: [...ruleSel.coaches],
      categories: [...ruleSel.categories],
      weekdays: [...ruleSel.weekdays],
      time_from: $("#ruleFrom").value || null,
      time_to: $("#ruleTo").value || null,
      mode: $("#ruleMode").value,
    };
    // an id turns the same endpoint into an update instead of an insert
    if (editingRuleId) {
      payload.id = editingRuleId;
      payload.enabled = editingEnabled;
    }
    await api("/api/rules", { method: "POST", body: JSON.stringify(payload) });
    const wasEditing = editingRuleId !== null;
    resetRuleForm();
    $("#ruleMsg").textContent = wasEditing ? "עודכן ✓" : "נשמר ✓";
    loadRules();
  } catch (e) {
    msg.classList.add("error");
    msg.textContent = "נכשל: " + e.message;
  }
};

/* ------------------------------------------------------------- settings */

function showPane(name) {
  state.settingsPane = name;
  $$(".pane").forEach((p) => { p.hidden = p.dataset.pane !== name; });
  $$(".subtab[data-pane]").forEach(
    (b) => b.classList.toggle("active", b.dataset.pane === name));
  if (name === "profile" || name === "studio") loadProfile();
}

function kvRow(el, label, value, cls) {
  if (value === null || value === undefined || value === "") return;
  const row = document.createElement("div");
  row.className = "kv-row";
  const k = document.createElement("span");
  k.className = "kv-k";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = "kv-v" + (cls ? " " + cls : "");
  if (value instanceof Node) v.appendChild(value); else v.textContent = value;
  row.append(k, v);
  el.appendChild(row);
}

function link(href, text) {
  const a = document.createElement("a");
  a.href = href;
  a.textContent = text;
  a.target = "_blank";
  a.rel = "noopener";
  return a;
}

function cardExpiryWarning(exp) {
  // "MM/YY" -> warn when the standing order would fail within ~2 months
  if (!exp || !/^\d{2}\/\d{2}$/.test(exp)) return false;
  const [mm, yy] = exp.split("/").map(Number);
  const end = new Date(2000 + yy, mm, 0);
  return (end - Date.now()) / 86400000 < 60;
}

function renderStudioPreferences(data, host) {
  const affiliations = data.studio_affiliations || [];
  const available = data.studios || [];
  if (!available.length) return;
  const canManage = affiliations.length > 1;

  const manage = document.createElement("div");
  manage.className = "studio-manage";
  const heading = document.createElement("h3");
  heading.textContent = "בחירה וסינון";
  manage.appendChild(heading);

  const defaultLabel = document.createElement("label");
  defaultLabel.textContent = "סטודיו ברירת מחדל";
  const select = document.createElement("select");
  select.id = "defaultStudio";
  // An ignored studio has unknown membership state because we deliberately
  // do not query it. Keep it selectable so restoring it + making it default
  // stays one action; the server verifies the membership before saving.
  const defaultChoices = (canManage ? affiliations : available).filter(
    (studio) => studio.has_active_membership !== false);
  for (const studio of defaultChoices) {
    const option = document.createElement("option");
    option.value = studio.id;
    option.textContent = studio.name + (studio.ignored ? " · הפעל מחדש" : "");
    select.appendChild(option);
  }
  select.value = String(data.default_studio_id || data.selected_studio_id);
  select.disabled = !canManage;
  if (!canManage) select.classList.add("studio-fixed-select");
  defaultLabel.appendChild(select);
  manage.appendChild(defaultLabel);

  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = "זה הסטודיו שייפתח בכל כניסה. מעבר מהכותרת הוא זמני." +
    (canManage ? "" : " זה הסטודיו הפעיל היחיד שלך ולכן הוא נבחר אוטומטית.");
  manage.appendChild(hint);

  const ignored = new Set((data.ignored_studio_ids || []).map(Number));
  const list = document.createElement("div");
  list.className = "studio-ignore-list";
  const checkboxes = [];
  for (const studio of affiliations) {
    const label = document.createElement("label");
    label.className = "studio-ignore";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.value = studio.id;
    check.checked = ignored.has(Number(studio.id));
    checkboxes.push(check);
    const text = document.createElement("span");
    text.textContent = `התעלם מ-${studio.name}`;
    const note = document.createElement("small");
    note.textContent = studio.ignored ? "לא נמשך ממנו מידע" :
      studio.has_active_membership === false ? "לא נמצא מנוי פעיל" : "";
    label.append(check, text, note);
    list.appendChild(label);
  }
  const guardDefault = () => {
    for (const check of checkboxes) {
      check.disabled = Number(check.value) === Number(select.value);
      if (check.disabled) check.checked = false;
    }
  };
  select.onchange = guardDefault;
  guardDefault();
  manage.appendChild(list);

  const save = document.createElement("button");
  save.className = "primary";
  save.textContent = "שמור בחירת סטודיו";
  save.disabled = !canManage;
  save.onclick = async () => {
    save.disabled = true;
    try {
      await api("/api/studios/preferences", {
        method: "POST",
        body: JSON.stringify({
          default_studio_id: Number(select.value),
          ignored_studio_ids: checkboxes.filter((x) => x.checked)
            .map((x) => Number(x.value)),
        }),
      });
      state.memberships = [];
      state.facets = { coaches: [], categories: [] };
      await Promise.all([loadStudioSwitch(), loadFacets(), ensureMemberships()]);
      await loadProfile();
      toast("בחירת הסטודיו נשמרה");
    } catch (e) {
      toast("השמירה נכשלה: " + e.message);
      save.disabled = false;
    }
  };
  manage.appendChild(save);
  host.appendChild(manage);
}

async function loadProfile() {
  const studio = state.selectedStudioId;
  let d;
  try { d = await api("/api/profile"); }
  catch (e) {
    $("#profAccount").textContent = "צריך מפתח API כדי לראות פרטי חשבון";
    return;
  }
  if (studio !== state.selectedStudioId) return;
  const p = d.profile || {}, m = d.membership || {}, memberships = d.memberships || [],
        a = d.activity || {}, q = d.quota;
  const cur = p.currency || "₪";

  const acc = $("#profAccount"); acc.innerHTML = "";
  kvRow(acc, "שם", p.full_name);
  kvRow(acc, "אימייל", p.email);
  if (p.phone) kvRow(acc, "טלפון", link("tel:" + p.phone, p.phone));
  if (p.member_since) kvRow(acc, "חבר/ה מאז", p.member_since.slice(0, 10));
  kvRow(acc, "אישור רפואי", p.medical_cert ? "✅ בתוקף" : "❌ חסר",
        p.medical_cert ? "" : "warn");
  kvRow(acc, "הצהרת בריאות", p.has_waiver ? "✅ נחתמה" : "❌ חסרה",
        p.has_waiver ? "" : "warn");

  const mem = $("#profMembership"); mem.innerHTML = "";
  kvRow(mem, "תוכנית", m.plan);
  if (m.price != null) kvRow(mem, "מחיר", `${m.price}${cur}${m.recurring ? " · הוראת קבע" : ""}`);
  kvRow(mem, "בתוקף מ", m.start);
  kvRow(mem, "סיום", m.end || "ללא תאריך סיום");
  kvRow(mem, "סטטוס", m.active ? "✅ פעיל" : "❌ לא פעיל", m.active ? "" : "warn");
  if (memberships.length > 1) {
    for (const item of memberships) {
      if (item.id === m.id) continue;
      const left = item.sessions_left != null ? ` · נותרו ${item.sessions_left}` : "";
      const end = item.end ? ` · עד ${item.end}` : "";
      kvRow(mem, "מנוי נוסף", `${item.plan || item.id}${left}${end}`);
    }
  }
  if (m.day_of_payment) kvRow(mem, "יום חיוב", `ה-${m.day_of_payment} בחודש`);
  if (m.card_ends) {
    const warn = cardExpiryWarning(m.card_exp);
    kvRow(mem, "כרטיס בתיק",
          `•••• ${m.card_ends}` + (m.card_exp ? ` · תוקף ${m.card_exp}` : "") +
          (warn ? " ⚠️ פג בקרוב" : ""), warn ? "warn" : "");
  }
  if (m.total_debt != null || p.total_debt != null) {
    const debt = p.total_debt ?? m.total_debt;
    kvRow(mem, "יתרת חוב", `${debt}${cur}`, Number(debt) > 0 ? "warn" : "");
  }

  const act = $("#profActivity"); act.innerHTML = "";
  kvRow(act, "אימונים שהתקיימו", a.attended);
  kvRow(act, "אימונים קרובים", a.upcoming);
  kvRow(act, "ממוצע שבועי", a.weekly_average);
  renderStudioMemberships(d);


  const st = $("#profStudio"); st.innerHTML = "";
  const s = p.studio || {};
  kvRow(st, "סטודיו", s.name);
  if (s.address) kvRow(st, "כתובת",
    link("https://maps.google.com/?q=" + encodeURIComponent(s.address), s.address));
  if (s.phone) kvRow(st, "טלפון", link("tel:" + s.phone, s.phone));
  if (s.email) kvRow(st, "אימייל", link("mailto:" + s.email, s.email));
  renderStudioPreferences(d, st);
}

// A short list beats a free-text field: a typo here silently moves every
// registration grab, and the studio is in exactly one of these.
const TIMEZONES = [
  "Asia/Jerusalem", "Europe/London", "Europe/Berlin", "Europe/Paris",
  "Europe/Moscow", "America/New_York", "America/Chicago", "America/Denver",
  "America/Los_Angeles", "Asia/Dubai", "Australia/Sydney", "UTC",
];

function renderClock(h) {
  const el = $("#clockLine");
  if (!el || !h.server_time) return;
  const server = new Date(h.server_time);
  const drift = Math.abs(Date.now() - server.getTime()) / 60000;
  const shown = server.toLocaleString("he-IL", { dateStyle: "short",
                                                timeStyle: "short" });
  el.textContent = `שעון השרת: ${shown} (${h.timezone || "?"})`;
  // A server clock that is wrong shifts every grab by the same amount and
  // says nothing about it. Two minutes of slack covers ordinary skew.
  el.classList.toggle("clock-bad", drift > 2);
  if (drift > 2) {
    el.textContent += ` ⚠️ סוטה ב-${Math.round(drift)} דק׳ מהשעון של המכשיר הזה` +
      " — תפיסות ייצאו לפועל בזמן הלא נכון";
  }
}

const CATALOG_KIND_LABELS = {
  strength: "כוח ומשקולות", bodyweight: "משקל גוף",
  flexibility: "גמישות ומתיחות", skill: "מיומנות",
  handstand: "עמידות ידיים", movement: "תנועה ואקרובטיקה",
  cardio: "אירובי", custom: "אישי",
};

function exerciseCatalogMeta(item) {
  const parts = [CATALOG_KIND_LABELS[item.kind] || item.kind];
  if (item.target) parts.push(item.target);
  if (item.equipment) parts.push(item.equipment);
  return parts.filter(Boolean).join(" · ");
}

async function updateExerciseShortcut(exercise, action) {
  await api(`/api/journal/exercise-catalog/${encodeURIComponent(exercise.id)}`, {
    method: "POST", body: JSON.stringify({ action }),
  });
}

function exerciseShortcutRow(exercise, removed = false) {
  const row = document.createElement("div"); row.className = "exercise-shortcut";
  const copy = document.createElement("div");
  const name = document.createElement("strong"); name.textContent = exercise.name;
  const meta = document.createElement("small"); meta.textContent = exerciseCatalogMeta(exercise);
  copy.append(name, meta);
  const button = document.createElement("button"); button.type = "button";
  button.className = removed ? "exercise-restore" : "exercise-hide";
  button.textContent = removed ? "החזר" : "×";
  button.title = removed ? "החזר לקיצורי הדרך" : "הסר מקיצורי הדרך";
  button.onclick = async () => {
    button.disabled = true;
    try {
      await updateExerciseShortcut(exercise,
        removed && !exercise.pack_id ? "add" : removed ? "restore" : "remove");
      await loadSettings();
      toast(removed ? "התרגיל הוחזר ✓" : "התרגיל הוסר מהקיצורים ✓");
    } catch (e) { toast("נכשל: " + e.message); button.disabled = false; }
  };
  row.append(copy, button); return row;
}

async function loadExerciseCatalog() {
  const q = $("#exerciseCatalogSearch").value.trim();
  const kind = $("#exerciseCatalogKind").value;
  const equipment = $("#exerciseCatalogEquipment").value;
  const includeHidden = state.exerciseCatalogTarget === "settings" &&
    $("#exerciseCatalogIncludeHidden").checked;
  const params = new URLSearchParams({ q, kind, equipment, limit: "80" });
  if (includeHidden) params.set("include_hidden", "true");
  const status = $("#exerciseCatalogStatus"), host = $("#exerciseCatalogResults");
  status.classList.remove("error");
  status.textContent = "מחפש…"; host.innerHTML = "";
  try {
    const data = await api(`/api/journal/exercise-catalog?${params}`);
    const kindSelect = $("#exerciseCatalogKind");
    if (kindSelect.options.length === 1) for (const [value, label] of Object.entries(data.kinds || {})) {
      const option = document.createElement("option"); option.value = value;
      option.textContent = label; kindSelect.appendChild(option);
    }
    const equipmentSelect = $("#exerciseCatalogEquipment");
    if (equipmentSelect.options.length === 1) for (const value of data.equipment || []) {
      const option = document.createElement("option"); option.value = value;
      option.textContent = value; equipmentSelect.appendChild(option);
    }
    $("#exerciseCatalogMeta").textContent =
      `${data.meta.count.toLocaleString("he-IL")} תרגילים · ללא תמונות כבדות`;
    status.textContent = data.total > data.items.length
      ? `מוצגים ${data.items.length} מתוך ${data.total} — אפשר לצמצם בחיפוש`
      : `${data.total} תרגילים`;
    if (!data.items.length) {
      const empty = document.createElement("p"); empty.className = "journal-empty";
      empty.textContent = "לא נמצא תרגיל. אפשר לשנות את החיפוש או ליצור תרגיל אישי בהגדרות.";
      host.appendChild(empty); return;
    }
    for (const item of data.items) {
      const row = document.createElement("div"); row.className = "exercise-catalog-row";
      if (item.hidden) row.classList.add("is-hidden");
      const copy = document.createElement("div");
      const title = document.createElement("strong"); title.textContent = item.name;
      const meta = document.createElement("small"); meta.textContent = exerciseCatalogMeta(item);
      copy.append(title, meta);
      const button = document.createElement("button"); button.type = "button";
      if (state.exerciseCatalogTarget === "editor") {
        button.textContent = "＋ לאימון";
        button.onclick = () => {
          addExerciseRow(item); button.textContent = "נוסף ✓"; button.disabled = true;
        };
      } else {
        const action = item.hidden ? (item.pack_id ? "restore" : "add")
          : item.shortcut ? "remove" : "add";
        button.textContent = item.hidden ? "החזר" : item.shortcut ? "הסר" : "הוסף";
        button.className = item.shortcut && !item.hidden ? "subtle-danger" : "";
        button.onclick = async () => {
          button.disabled = true;
          try {
            await updateExerciseShortcut(item, action);
            await loadSettings(); await loadExerciseCatalog();
          } catch (e) { toast("נכשל: " + e.message); button.disabled = false; }
        };
      }
      row.append(copy, button); host.appendChild(row);
    }
  } catch (e) {
    status.textContent = "המאגר לא נטען: " + e.message;
    status.classList.add("error");
  }
}

function openExerciseCatalog(target = "editor") {
  state.exerciseCatalogTarget = target;
  const dialog = $("#exerciseCatalogDialog");
  $("#exerciseCatalogTitle").textContent = target === "editor"
    ? "הוספת תרגיל לאימון" : "ניהול מאגר התרגילים";
  $("#exerciseCatalogHiddenWrap").hidden = target !== "settings";
  $("#exerciseCatalogIncludeHidden").checked = false;
  $("#exerciseCatalogSearch").value = "";
  $("#exerciseCatalogKind").value = "";
  $("#exerciseCatalogEquipment").value = "";
  dialog.showModal(); loadExerciseCatalog();
  setTimeout(() => $("#exerciseCatalogSearch").focus(), 0);
}

function renderExerciseSettings(settings) {
  const explicit = settings.exercise_packs;
  state.exercisePacks = [...(explicit == null
    ? (settings.suggested_exercise_packs || []) : explicit)];
  const host = $("#exercisePacks"); host.innerHTML = "";
  for (const pack of settings.exercise_pack_catalog || []) {
    const label = document.createElement("label");
    const check = document.createElement("input"); check.type = "checkbox";
    check.value = pack.id; check.checked = state.exercisePacks.includes(pack.id);
    check.onchange = () => {
      state.exercisePacks = $$("#exercisePacks input:checked").map((x) => x.value);
    };
    const text = document.createElement("span");
    text.textContent = `${pack.icon} ${pack.name}${pack.suggested ? " · מוצע לסטודיו" : ""}`;
    label.append(check, text); host.appendChild(label);
  }
  const shortcuts = $("#exerciseShortcutList"); shortcuts.innerHTML = "";
  const quick = (settings.exercise_shortcuts || []).filter((x) => !x.custom);
  if (quick.length) quick.forEach((exercise) =>
    shortcuts.appendChild(exerciseShortcutRow(exercise)));
  else shortcuts.textContent = "אין קיצורים. אפשר להוסיף מהמאגר.";
  const removed = settings.hidden_exercises || [];
  $("#hiddenExerciseDetails").hidden = !removed.length;
  $("#hiddenExerciseCount").textContent = removed.length;
  const hiddenHost = $("#hiddenExerciseList"); hiddenHost.innerHTML = "";
  removed.forEach((exercise) => hiddenHost.appendChild(exerciseShortcutRow(exercise, true)));
  const custom = $("#customExerciseList"); custom.innerHTML = "";
  for (const exercise of settings.custom_exercises || []) {
    const button = document.createElement("button"); button.className = "chip"; button.type = "button";
    button.textContent = `${exercise.name} ×`; button.title = "הסר מהרשימה האישית";
    button.onclick = async () => {
      try {
        await api(`/api/journal/exercises/${exercise.id}`, { method: "DELETE" });
        await loadSettings();
      } catch (e) { toast("נכשל: " + e.message); }
    };
    custom.appendChild(button);
  }
}

async function loadSettings() {
  api("/api/health").then((h) => {
    $("#versionLine").textContent =
      `גרסה פרוסה: ${h.version || "לא ידוע"}` +
      (h.revision && h.revision !== "unknown" ? ` · ${h.revision.slice(0, 7)}` : "") +
      (h.last_sync ? ` · סונכרן ${fmtSince(h.last_sync)}` : "");
    renderClock(h);
  }).catch(() => {});
  let s;
  try { s = await api("/api/settings"); }
  catch (e) {
    settingsMsg("צריך מפתח API כדי לערוך הגדרות", true);
    return;
  }
  $("#tgEnabled").checked = s.telegram.enabled;
  $("#tgToken").value = s.telegram.bot_token;
  $("#tgChat").value = s.telegram.chat_id;
  $("#haEnabled").checked = s.ha.enabled;
  $("#haWebhook").value = s.ha.webhook_url;
  $("#haFeedbackInHa").checked = s.ha.feedback_in_ha === true;
  $("#journalLevel").value = (s.journal || {}).level || "off";
  $("#journalTelegram").checked = (s.telegram.kinds || []).includes("journal");
  $("#journalHa").checked = (s.ha.kinds || []).includes("journal");
  renderExerciseSettings(s);
  const tzSel = $("#timezone");
  tzSel.innerHTML = "";
  // whatever the server actually has stays selectable even if it is not on
  // the short list — never silently rewrite a working setting
  for (const z of (TIMEZONES.includes(s.timezone) ? TIMEZONES
                                                  : [s.timezone, ...TIMEZONES])) {
    const o = document.createElement("option");
    o.value = o.textContent = z;
    tzSel.appendChild(o);
  }
  tzSel.value = s.timezone || "Asia/Jerusalem";

  const ret = s.retention || {};
  $("#retAttended").value = ret.attended_days ?? 365;
  $("#retPast").value = ret.past_days ?? 30;
  $("#retFuture").value = ret.future_days ?? 30;
  $("#retDecisions").value = ret.decisions_days ?? 180;
  $("#retMessages").value = ret.messages_days ?? 365;

  state.blocked = s.blocked_categories || [];
  renderBlockedList();
  $("#lateCancel").value = s.late_cancel_warning_minutes ?? 60;
  $("#tgLogLevel").value = (s.telegram || {}).log_level || "error";
  $("#haLogLevel").value = (s.ha || {}).log_level || "error";

  $("#digestHour").value = s.digest_hour;
  $("#monthlyQuota").value = s.monthly_quota ?? 0;
  const membershipSelect = $("#preferredMembership");
  membershipSelect.innerHTML = "";
  const memberships = s.available_memberships || [];
  for (const m of memberships) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.plan || `מנוי ${m.id}`;
    membershipSelect.appendChild(o);
  }
  if (memberships.length) {
    membershipSelect.value = String(s.preferred_membership_id || memberships[0].id);
  }
  membershipSelect.disabled = memberships.length < 2;
  $("#membershipOptions").textContent = memberships.map((m) => {
    const amount = m.sessions_on_purchase ?? m.plan_quota;
    return `${m.plan || m.id}` + (amount ? ` · ${amount} כניסות` : "") +
      (m.sessions_left != null ? ` · נותרו ${m.sessions_left}` : "") +
      (m.end ? ` · עד ${m.end}` : "");
  }).join("\n");
  $("#classReminder").value = s.class_reminder_minutes ?? 0;
  const nowMonth = studioToday().slice(0, 7);
  $("#quotaOverride").value =
    s.quota_override && s.quota_override.month === nowMonth ? s.quota_override.quota : "";
  state.alarms = s.calendar_alarms || [];
  renderAlarmList();
  // the browser already knows how this server looks from outside — use that
  // instead of making anyone type it. Skip localhost: a dev address would be
  // useless in a notification opened on a phone.
  const guess = /^(localhost|127\.|\[?::1)/.test(location.hostname) ? "" : location.origin;
  $("#baseUrl").value = s.base_url || guess;
  if (!s.base_url && guess) {
    $("#baseUrl").dataset.autofilled = "1";
    $("#baseUrlHint").textContent = "מולא אוטומטית מהכתובת שדרכה נכנסת. שמור/י כדי לאשר.";
  }
  $("#notifyOrder").value = (s.notify.order || ["telegram"])[0];
  $("#escMinutes").value = s.notify.escalation_minutes ?? 0;
  for (const ch of ["tg", "ha"]) {
    const kinds = (ch === "tg" ? s.telegram : s.ha).kinds || [];
    $$(`.kind-${ch}`).forEach((c) => { c.checked = kinds.includes(c.dataset.kind); });
  }
}

function settingsMsg(text, isError) {
  // one message element per pane; update them all so the confirmation is
  // visible wherever the user pressed save
  $$(".settingsMsg").forEach((m) => {
    m.textContent = text;
    m.classList.toggle("error", !!isError);
  });
}

$$(".journal-test").forEach((button) => button.addEventListener("click", async () => {
  const message = $("#journalTestMsg");
  const level = $("#journalLevel").value;
  if (level === "off") {
    message.textContent = "בחרו רמת מעקב: מהיר, משוב או מלא. אין צורך לשמור כדי לנסות.";
    return;
  }
  $$(".journal-test").forEach((b) => { b.disabled = true; });
  message.textContent = "שולח בדיקה…";
  try {
    await api(`/api/settings/test-journal/${button.dataset.channel}`, {
      method: "POST", body: JSON.stringify({ level }),
    });
    message.textContent = "הבדיקה נשלחה — פתחו את ההתראה וענו על השאלות. שום תשובה לא תישמר ביומן.";
  } catch (error) {
    message.textContent = "הבדיקה נכשלה: " + error.message;
  } finally {
    $$(".journal-test").forEach((b) => { b.disabled = false; });
  }
}));

async function saveSettings() {
  settingsMsg("", false);
  // An empty field is not zero. Number("") is 0, so clearing this box moved
  // the nightly message to midnight and reported "saved ✓" — the one setting
  // whose whole point is arriving at an hour you are awake.
  const dh = $("#digestHour").value;
  if (dh === "" || !(Number(dh) >= 0 && Number(dh) <= 23)) {
    settingsMsg("שעת ההודעה הלילית חסרה או לא חוקית (0–23)", true);
    return false;
  }
  try {
    const tgKinds = $$(".kind-tg").filter((c) => c.checked).map((c) => c.dataset.kind);
    const haKinds = $$(".kind-ha").filter((c) => c.checked).map((c) => c.dataset.kind);
    if ($("#journalTelegram").checked && !tgKinds.includes("journal")) tgKinds.push("journal");
    if ($("#journalHa").checked && !haKinds.includes("journal")) haKinds.push("journal");
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        timezone: $("#timezone").value,
        blocked_categories: state.blocked,
        retention: {
          attended_days: Number($("#retAttended").value) || 365,
          past_days: Number($("#retPast").value) || 30,
          future_days: Number($("#retFuture").value) || 30,
          decisions_days: Number($("#retDecisions").value) || 180,
          messages_days: Number($("#retMessages").value) || 365,
        },
        late_cancel_warning_minutes: Number($("#lateCancel").value) || 0,
        digest_hour: Number(dh),
        monthly_quota: Number($("#monthlyQuota").value) || 0,
        preferred_membership_id: Number($("#preferredMembership").value) || null,
        class_reminder_minutes: Number($("#classReminder").value) || 0,
        quota_override: Number($("#quotaOverride").value) > 0
          ? { month: studioToday().slice(0, 7),
              quota: Number($("#quotaOverride").value) }
          : null,
        calendar_alarms: state.alarms,
        journal: { level: $("#journalLevel").value, delay_minutes: 30 },
        exercise_packs: state.exercisePacks,
        base_url: $("#baseUrl").value.trim(),
        telegram: {
          log_level: $("#tgLogLevel").value,
          enabled: $("#tgEnabled").checked,
          bot_token: $("#tgToken").value,
          chat_id: $("#tgChat").value,
          kinds: tgKinds.filter((k) => k !== "journal" || $("#journalTelegram").checked),
        },
        ha: {
          log_level: $("#haLogLevel").value,
          enabled: $("#haEnabled").checked,
          webhook_url: $("#haWebhook").value.trim(),
          feedback_in_ha: $("#haFeedbackInHa").checked,
          kinds: haKinds.filter((k) => k !== "journal" || $("#journalHa").checked),
        },
        notify: {
          order: $("#notifyOrder").value === "ha" ? ["ha", "telegram"] : ["telegram", "ha"],
          escalation_minutes: Number($("#escMinutes").value) || 0,
        },
      }),
    });
    settingsMsg("נשמר ✓", false);
    return true;
  } catch (e) {
    settingsMsg("נכשל: " + e.message, true);
    return false;
  }
}

$("#alarmAdd").addEventListener("click", () => {
  const amount = Number($("#alarmAmount").value);
  const unit = Number($("#alarmUnit").value);
  if (!amount || amount < 1) { toast("בחר/י כמות"); return; }
  const mins = amount * unit;
  if (mins > 10080) { toast("מקסימום שבוע לפני"); return; }
  if (!state.alarms.includes(mins)) state.alarms.push(mins);
  renderAlarmList();
});

$("#customExerciseAdd").addEventListener("click", async () => {
  const name = $("#customExerciseName").value.trim();
  if (!name) { toast("צריך שם לתרגיל"); return; }
  const button = $("#customExerciseAdd"); button.disabled = true;
  try {
    await api("/api/journal/exercises", { method: "POST", body: JSON.stringify({
      name, metric_type: $("#customExerciseMetric").value,
    }) });
    $("#customExerciseName").value = "";
    await loadSettings();
    toast("התרגיל נוסף ✓");
  } catch (e) { toast("נכשל: " + e.message); }
  button.disabled = false;
});

$("#manageExerciseCatalog").addEventListener("click", () => openExerciseCatalog("settings"));
$("#exerciseCatalogSearch").addEventListener("input", () => {
  clearTimeout(state.exerciseCatalogTimer);
  state.exerciseCatalogTimer = setTimeout(loadExerciseCatalog, 180);
});
for (const id of ["#exerciseCatalogKind", "#exerciseCatalogEquipment",
                  "#exerciseCatalogIncludeHidden"]) {
  $(id).addEventListener("change", loadExerciseCatalog);
}
$("#exerciseCatalogSearch").addEventListener("keydown", (event) => {
  if (event.key === "Enter") event.preventDefault();
});

$$(".settingsSave").forEach((b) => b.addEventListener("click", saveSettings));

$("#e2eBtn").addEventListener("click", async () => {
  const b = $("#e2eBtn"), msg = $("#e2eMsg");
  msg.classList.remove("error");
  msg.textContent = "שומר הגדרות ושולח…";
  b.disabled = true;
  if (await saveSettings()) {
    try {
      const r = await api("/api/settings/test-digest", { method: "POST" });
      msg.textContent = `נשלחה הודעה עם ${r.sessions} שיעורים: ${r.labels.join(" · ")} — לחצ/י על כפתור בהודעה כדי להשלים את הבדיקה`;
    } catch (e) {
      msg.classList.add("error");
      msg.textContent = "נכשל: " + e.message;
    }
  }
  b.disabled = false;
});

$$(".testBtn").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  // save the form first — testing what's on screen, not what was stored
  if (await saveSettings()) {
    try {
      await api(`/api/settings/test/${b.dataset.test}`, { method: "POST" });
      toast("נשלח ✓");
    } catch (e) { toast("נכשל: " + e.message, 6000); }
  }
  b.disabled = false;
}));

/* ----------------------------------------------------------------- misc */

function fmtDate(d) {
  return `${d.getDate()}.${d.getMonth() + 1}`;
}
function fmtSince(isoStr) {
  const mins = Math.round((Date.now() - new Date(isoStr).getTime()) / 60000);
  if (mins < 1) return "עכשיו";
  if (mins < 60) return `לפני ${mins} דק׳`;
  return `לפני ${Math.round(mins / 60)} שע׳`;
}

boot();
