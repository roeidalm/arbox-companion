import { feedbackTemplate, mountFeedback } from "./feedback-form.js?v=3.1.0";
import {renderCalendar, calendarRange, calendarSignature} from './panel-calendar.js?v=3.2.0';
import {renderJournal, journalSignature} from './panel-journal.js?v=3.1.0';
import {policySummary, policyDialog} from './membership-policy.js';

const TABS = [
  ["overview", "◈", "סקירה"],
  ["schedule", "▦", "לוח"],
  ["mine", "♡", "שלי"],
  ["journal", "▤", "יומן"],
  ["automations", "↻", "אוטומציות"],
];
const REASONS = {
  none: "ללא סיבה",
  work: "עבודה",
  illness: "מחלה",
  injury: "פציעה",
  personal: "אילוץ אישי / משפחתי",
  fatigue: "עייפות / התאוששות",
  plans_changed: "שינוי תוכניות",
  other: "אחר",
};
const STATUS = {
  attended: "הגעתי",
  missed: "לא הגעתי",
  pending: "ממתין לאישור הגעה",
  cancelled_safe: "בוטל",
  cancelled_late: "ביטול מאוחר",
  standby_cancelled: "המתנה שבוטלה",
  standby: "המתנה ללא הרשמה",
};
const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const button = (text, run, cls = "") => {
  const n = node("button", text, cls);
  n.type = "button";
  n.onclick = (event) => {
    // Action handlers display errors in the panel; rejected writes are never replayed.
    Promise.resolve(run(event)).catch(() => {});
  };
  return n;
};
const iso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const plus = (date, days) => {
  const d = new Date(`${date}T12:00:00`);
  d.setDate(d.getDate() + days);
  return iso(d);
};
const fmtDate = (date) =>
  date
    ? new Date(`${date}T12:00:00`).toLocaleDateString("he-IL", {
        weekday: "long",
        day: "numeric",
        month: "numeric",
      })
    : "";
const hasFeedback = (row) =>
  !!(
    row.class_feedback ||
    row.coach_feedback ||
    row.notes ||
    row.exercises?.length
  );
export function sessionStatus(s) {
  if (s.status)
    return [STATUS[s.status] || s.status, hasFeedback(s) ? "reviewed" : ""];
  if (s.user_booked) return ["✓ מוזמן", "booked"];
  if (s.user_in_standby) return ["בהמתנה", "waiting"];
  if (s.planning && s.planning.state !== 'ready') return ["⚠ דורש בדיקה", "warning"];
  if (s.watched) return ["⏳ מתוזמן", "planned"];
  if (s.automation_skipped) return ["⏭ דולג הפעם", "skipped"];
  if (s.autobook_blocked_by_vacation) return ["🏖 חופשה", "vacation"];
  if (s.planning_source === "autobook" || s.autobook_match)
    return ["🤖 אוטומטי", "automatic"];
  if (s.status) return [STATUS[s.status] || s.status, ""];
  return [
    s.registration_open
      ? s.free > 0
        ? `${s.free} מקומות פנויים`
        : "מלא"
      : s.registration_note || "ההרשמה טרם נפתחה",
    "",
  ];
}
export class ArboxAppPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = {};
    this._contexts = {};
    this._tab = "overview";
    this._date = iso(new Date());
    this._view = "day";
    this._filters = {};
    this._generation = 0;
    this._onLocation = () => this.navigateFromURL();
    this._visibility = () => {
      if (!document.hidden) this.load();
    };
  }
  set hass(value) {
    const was = this._hass?.connected;
    this._hass = value;
    this.style.colorScheme = value.themes?.darkMode ? "dark" : "light";
    if (this._menu) this._menu.hass = value;
    if (this._built && value.connected === false)
      this.connectionError(
        "החיבור ל־Home Assistant נותק. הנתונים המוצגים הם מהטעינה האחרונה.",
      );
    if (was === false && value.connected !== false && this._built) this.load();
    this.start();
  }
  set panel(value) {
    this._panel = value;
    this.start();
  }
  set narrow(value) {
    this._narrow = value;
    if (this._menu) this._menu.narrow = value;
  }
  connectedCallback() {
    window.addEventListener("hashchange", this._onLocation);
    window.addEventListener("location-changed", this._onLocation);
    document.addEventListener("visibilitychange", this._visibility);
    this._timer = setInterval(() => {
      if (!document.hidden) this.load();
    }, 30000);
    this.start();
  }
  disconnectedCallback() {
    clearInterval(this._timer);
    window.removeEventListener("hashchange", this._onLocation);
    window.removeEventListener("location-changed", this._onLocation);
    document.removeEventListener("visibilitychange", this._visibility);
    this._generation++;
    this._built = false;
  }
  async start() {
    if (this._built || !this.isConnected || !this._hass || !this._panel) return;
    this._built = true;
    this.build();
    try {
      const result = await this._hass.callWS({ type: "arbox/panel/entries" });
      this._entries = result.entries || [];
      if (!this._entries.length)
        throw new Error(
          "אין חיבור Arbox מורשה. מנהל Home Assistant יכול להגדיר הרשאות באפשרויות האינטגרציה.",
        );
      this._entry =
        this._entries.find(
          (e) => e.entry_id === this._panel.config?.entry_id,
        ) || this._entries[0];
      this._entrySelect.replaceChildren(
        ...this._entries.map((e) => new Option(e.title, e.entry_id)),
      );
      this._entrySelect.value = this._entry.entry_id;
      this._entrySelect.hidden = this._entries.length < 2;
      this.navigateFromURL();
    } catch (e) {
      this.connectionError(e.message || "לא ניתן לפתוח את Arbox.");
    }
  }
  build() {
    const css = node("link");
    css.rel = "stylesheet";
    css.href = new URL("./app-panel.css?v=3.1.2", import.meta.url).href;
    this._shell = node("div", null, "app");
    this._shell.dir = "rtl";
    this._shell.lang = "he";
    const header = node("header", null, "toolbar");
    this._menu = document.createElement("ha-menu-button");
    this._menu.hass = this._hass;
    this._menu.narrow = this._narrow;
    header.append(
      this._menu,
      node("b", "Arbox", "brand"),
      node("span", "התנועה שלך, במקום אחד", "tagline"),
    );
    this._entrySelect = node("select");
    this._entrySelect.setAttribute("aria-label", "חשבון Arbox");
    this._entrySelect.onchange = () => {
      if (this._dialog?.open) {
        this._entrySelect.value = this._entry.entry_id;
        this.toast("סגרו את הטופס לפני החלפת חשבון.");
        return;
      }
      this._entry = this._entries.find(
        (e) => e.entry_id === this._entrySelect.value,
      );
      this._data = {};
      this._contexts = {};
      this._currentContext = null;
      this._journalState = null;
      this._filters = {};
      this._generation++;
      this.load();
    };
    header.append(this._entrySelect);
    this._sync = button("↻ רענון", async () => {
      this._sync.disabled = true;
      try {
        await this.act("refresh", {}, this.context());
        this.toast("הסנכרון הושלם. האימונים עודכנו מהמערכת.");
      } catch {
      } finally {
        this._sync.disabled = !this.canWrite();
      }
    });
    header.append(this._sync);
    this._meta = node("p", null, "sync-meta");
    this._error = node("div", null, "error");
    this._error.setAttribute("role", "alert");
    this._error.hidden = true;
    this._notice = node("div", null, "notice");
    this._notice.setAttribute("role", "status");
    this._notice.hidden = true;
    this._content = node("main");
    this._content.id = "content";
    this._nav = node("nav");
    this._nav.setAttribute("aria-label", "Arbox");
    for (const [id, icon, label] of TABS) {
      const b = button("", () => this.navigate(id));
      b.dataset.tab = id;
      b.append(node("span", icon, "nav-icon"), node("span", label));
      this._nav.append(b);
    }
    this._shell.append(
      header,
      this._meta,
      this._error,
      this._notice,
      this._content,
      this._nav,
    );
    this.shadowRoot.replaceChildren(css, this._shell);
  }
  navigate(tab) {
    window.history.replaceState(null, "", `#${tab}`);
    this.navigateFromURL();
  }
  navigateFromURL() {
    const parts = window.location.hash.slice(1).split("/");
    const previousTab = this._tab;
    this._tab = TABS.some((t) => t[0] === parts[0]) ? parts[0] : "overview";
    if (previousTab !== this._tab) this._resetScroll = true;
    this._focusId = Number(parts[1]) || null;
    this._nav
      ?.querySelectorAll("button")
      .forEach((b) =>
        b.setAttribute(
          "aria-current",
          b.dataset.tab === this._tab ? "page" : "false",
        ),
      );
    this.load();
  }
  studioNow() {
    const parts = new Intl.DateTimeFormat("sv-SE", {
      timeZone:
        this.context().timezone ||
        this._data.summary?.timezone ||
        "Asia/Jerusalem",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
    return parts.replace(" ", "T");
  }
  context() {
    return (
      this._currentContext ||
      this._contexts.summary ||
      Object.values(this._contexts)[0] ||
      {}
    );
  }
  canWrite() {
    return this._entry?.can_write === true && this._hass?.connected !== false;
  }
  connectionError(text) {
    this._error.textContent = text;
    this._error.hidden = false;
  }
  toast(text) {
    this._notice.textContent = text;
    this._notice.hidden = false;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => (this._notice.hidden = true), 9000);
  }
  async read(resource, params = {}) {
    const result = await this._hass.callWS({
      type: "arbox/panel/read",
      entry_id: this._entry.entry_id,
      resource,
      params,
    });
    return result;
  }
  async load() {
    const key = `${this._entry?.entry_id}:${this._tab}:${this._date}:${this._view}`;
    if (this._loadKey === key && this._loading) return this._loading;
    this._loadKey = key;
    const pending = this.loadData();
    this._loading = pending;
    try {
      return await pending;
    } finally {
      if (this._loading === pending) this._loading = null;
    }
  }
  async loadData() {
    if (!this._entry || !this._built || document.hidden) return;
    const generation = ++this._generation;
    const tab = this._tab;
    const resources = {
      overview: ["summary", "me", "journal", "membership_policies"],
      schedule: ["summary", "schedule", "facets"],
      mine: ["summary", "me", "membership_policies"],
      journal: ["summary", "history", "journal"],
      automations: ["summary", "rules", "vacations", "facets"],
    }[tab];
    if (!Object.keys(this._data).length)
      this._content.replaceChildren(node("p", "טוענים את האימונים…", "empty"));
    try {
      const results = await Promise.all(
        resources.map(async (resource) => [
          resource,
          await this.read(
            resource,
            resource === "schedule"
              ? calendarRange(this._date, this._view)
              : {},
          ),
        ]),
      );
      if (generation !== this._generation) return;
      const studioIds = new Set(
        results.map(([, result]) => result.context?.studio_id),
      );
      if (studioIds.size !== 1)
        throw new Error("הסטודיו השתנה בזמן הטעינה. רעננו את הנתונים.");
      const nextContext = results[0][1].context || {};
      if (this._currentContext?.studio_id !== nextContext.studio_id) {
        this._data = {};
        this._contexts = {};
        this._journalState = null;
        this._filters = {};
      }
      this._currentContext = nextContext;
      for (const [resource, result] of results) {
        this._data[resource] = result.data;
        this._contexts[resource] = result.context || {};
      }
      if (results.every(([, result]) => typeof result.can_write === "boolean"))
        this._entry.can_write = results.every(([, result]) => result.can_write);
      this._error.hidden = true;
      this._sync.disabled = !this.canWrite();
      this.render();
      if (this._focusId && !this._dialog?.open) {
        const s = this.rows().find(
          (r) => Number(r.schedule_id) === this._focusId,
        );
        this._focusId = null;
        if (s) this.openSession(s);
      }
    } catch (e) {
      if (generation === this._generation) {
        if (e.code === "forbidden") {
          this._data = {};
          this._contexts = {};
          this._currentContext = null;
      this._journalState = null;
      this._filters = {};
          this._entry.can_write = false;
          this._dialog?.close();
          this._content.replaceChildren();
        }
        this.connectionError(
          e.message || "לא ניתן לעדכן את הנתונים. המידע האחרון נשמר על המסך.",
        );
      }
    }
  }
  rows() {
    return (
      (this._tab === "journal"
        ? this._data.history
        : this._tab === "schedule"
          ? this._data.schedule
          : this._data.me
      )?.sessions || []
    );
  }
  render() {
    const last = this._data.summary?.last_sync || this.context().last_sync;
    this._meta.textContent = [
      this.context().name || this.context().studio_name || this._entry.title,
      last
        ? `סנכרון אחרון: ${new Date(last).toLocaleString("he-IL")}`
        : "עדיין אין זמן סנכרון",
      !this.canWrite() ? "צפייה בלבד" : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const signature = JSON.stringify({
      entry: this._entry.entry_id,
      tab: this._tab,
      data: this._data,
      context: this.context(),
      canWrite: this.canWrite(),
      date: this._date,
      view: this._view,
      filters: this._filters,
      selectedDay: this._selectedDay,
      period: this._period,
      feedbackFilter: this._feedbackFilter,
      calendar: calendarSignature(this),
      journal: journalSignature(this),
    });
    if (signature === this._renderSignature) return;
    this._renderSignature = signature;
    const focused = this.shadowRoot.activeElement;
    const restoreFocus =
      !this._resetScroll && focused && this._content.contains(focused);
    const focusKey = restoreFocus ? focused.dataset.focusKey : null;
    const focusLabel = restoreFocus
      ? focused.getAttribute("aria-label") || focused.textContent
      : null;
    const focusTag = restoreFocus ? focused.tagName : null;
    const selection =
      restoreFocus && typeof focused.selectionStart === "number"
        ? [focused.selectionStart, focused.selectionEnd]
        : null;
    const title = {
      overview: "נעים לחזור לתנועה",
      schedule: "לוח האימונים",
      mine: "האימונים שלי",
      journal: "יומן האימונים",
      automations: "התכנון שלי",
    }[this._tab];
    this._content.replaceChildren(
      node("p", fmtDate(this._date), "eyebrow"),
      node("h1", title),
    );
    this[`render_${this._tab}`]();
    if (restoreFocus) {
      const candidate = Array.from(
        this._content.querySelectorAll("button,input,select,summary,a"),
      ).find((el) =>
        focusKey
          ? el.dataset.focusKey === focusKey
          : el.tagName === focusTag &&
            (el.getAttribute("aria-label") || el.textContent) === focusLabel,
      );
      if (candidate) {
        candidate.focus({ preventScroll: true });
        if (selection && candidate.setSelectionRange)
          candidate.setSelectionRange(...selection);
      }
    }
    if (this._resetScroll) {
      this._shell.scrollTop = 0;
      this._resetScroll = false;
    }
  }
  list(rows, parent = this._content) {
    if (!rows.length) {
      parent.append(node("p", "אין אימונים להצגה בתקופה הזו.", "empty"));
      return;
    }
    const list = node("div", null, "session-list");
    let date;
    for (const s of rows) {
      if (date !== s.date) {
        date = s.date;
        list.append(node("h3", fmtDate(date), "date-heading"));
      }
      list.append(this.card(s));
    }
    parent.append(list);
  }
  card(s) {
    const card = button("", () => this.openSession(s), "session-card");
    card.dataset.focusKey = `session-${s.schedule_id}`;
    const time = node("div", null, "session-time");
    time.append(
      node("b", s.start_time?.slice(0, 5) || "—"),
      node("small", s.end_time?.slice(0, 5) || ""),
    );
    const info = node("div", null, "session-copy");
    info.append(
      node("strong", s.category_name || "אימון"),
      node("span", s.coach_name || ""),
    );
    const [label, cls] = sessionStatus(s);
    if (this._tab !== "journal" && cls) card.classList.add(`state-${cls}`);
    if (this._tab !== "journal" && ["planned", "automatic"].includes(cls) && s.registration_note)
      info.append(node("small", s.registration_note, "planning-note"));
    if (this._tab === "journal")
      info.append(
        node("small", hasFeedback(s) ? "משוב נשמר" : "ללא משוב", "muted"),
      );
    card.append(
      time,
      info,
      node("span", label, `badge ${cls}`),
      node("span", "‹", "chevron"),
    );
    if (this._tab === "journal") {
      card.classList.add("journal-card");
      const details = node("div", null, "journal-preview");
      const labels = {
        positive: "מעולה ★",
        neutral: "בסדר",
        negative: "פחות",
        not_applicable: "ללא דירוג",
      };
      for (const [key, title] of [
        ["class_feedback", "שיעור"],
        ["coach_feedback", "מאמן/ת"],
      ]) {
        if (s[key])
          details.append(
            node(
              "span",
              `${title}: ${labels[s[key]] || s[key]}`,
              "badge booked",
            ),
          );
      }
      if (s.notes) details.append(node("p", s.notes, "journal-note"));
      for (const exercise of (s.exercises || []).slice(0, 2)) {
        details.append(
          node(
            "p",
            [
              exercise.name,
              exercise.sets != null ? `${exercise.sets} סטים` : "",
              exercise.reps != null ? `${exercise.reps} חזרות` : "",
              exercise.weight != null ? `${exercise.weight} ק״ג` : "",
              exercise.duration_seconds != null
                ? `${exercise.duration_seconds} שניות`
                : "",
              exercise.distance != null ? `${exercise.distance} מ׳` : "",
              exercise.attempts != null ? `${exercise.attempts} ניסיונות` : "",
            ]
              .filter(Boolean)
              .join(" · "),
            "exercise-preview",
          ),
        );
      }
      details.append(
        node(
          "span",
          this.canWrite()
            ? hasFeedback(s)
              ? "פרטים ועריכת המשוב ←"
              : "פרטים ומילוי משוב ←"
            : "פרטי האימון ←",
          "journal-link",
        ),
      );
      card.append(details);
    }
    return card;
  }
  quota() {
    const q = this._data.summary?.quota;
    const wrap = node("section", null, "quota-card quota-compact");
    const heading = node("div", null, "section-heading");
    heading.append(node("h2", "המנויים והמכסה שלי"));
    if (q) heading.append(node("strong", `${q.used ?? 0} אימונים החודש`, "quota-number"));
    wrap.append(heading);
    const metrics = (values, personal = false) => {
      const labels = personal
        ? [["used", "נוצלו", "used"], ["reserved", "מוזמנים", "reserved"], ["planned", "בתכנון", "scheduled"], ["available_after_planned", "פנויים אחרי התכנון", "available"]]
        : [["used", "נוצלו", "used"], ["reserved", "מוזמנים", "reserved"], ["planned_scheduled", "מתוזמנים", "scheduled"], ["planned_autobook", "אוטומטיים", "automatic"], ["available_after_planned", "פנויים אחרי התכנון", "available"]];
      const row = node("div", null, "quota-legend");
      for (const [key, label, kind] of labels) {
        const cell = node("span", null, `quota-key ${kind}`);
        cell.append(node("b", String(values[key] ?? 0)), document.createTextNode(` ${label}`));
        row.append(cell);
      }
      return row;
    };
    if (q) {
      const bar = node("div", null, "quota-bar");
      bar.setAttribute("aria-hidden", "true");
      const values = [["used", q.used], ["reserved", q.reserved], ["scheduled", q.planned_scheduled], ["automatic", q.planned_autobook], ["available", Math.max(0, q.available_after_planned ?? 0)]];
      for (const [kind, count] of values) if (Number(count) > 0) {
        const part = node("span", null, kind); part.style.flexGrow = String(count); bar.append(part);
      }
      wrap.append(bar, metrics(q));
      if (q.overcommitted) wrap.append(node("p", "יש תכנונים ללא מכסה במנוי המתאים", "warning"));
      if (q.unresolved_plans?.length) wrap.append(node("p", `${q.unresolved_plans.length} תכנונים דורשים השלמה — ההרשמה שלהם מושהית`, "warning"));
      if (q.unattributed_sessions?.length) wrap.append(node("p", `${q.unattributed_sessions.length} אימונים טרם שויכו למנוי. נדרש סנכרון ובירור`, "warning"));
    } else wrap.append(node("p", "לא קיימת מכסה מחושבת למנוי הזה."));
    const members = this._data.summary?.memberships || [];
    const details = node("details", null, "membership-details");
    details.open = this._membershipsOpen ?? true;
    details.ontoggle = () => { this._membershipsOpen = details.open; };
    details.append(node("summary", `פירוט ${members.length} מנויים`));
    for (const m of members) {
      const data = q?.memberships?.find(x => (x.membership_user_id ?? x.id) === m.id);
      const card = node("article", null, "membership-card");
      const title = node("div", null, "section-heading");
      title.append(node("strong", m.plan || "מנוי פעיל"));
      if (data) title.append(node("b", `${data.used ?? 0} / ${data.quota ?? "—"}`, "membership-count"));
      card.append(title);
      card.append(node("small", [m.active === false ? "לא פעיל" : "פעיל", m.recurring ? "מנוי מתחדש" : "כרטיסייה", m.end ? `בתוקף עד ${fmtDate(m.end)}` : ""].filter(Boolean).join(" · "), "muted"));
      if (data) {
        card.append(metrics(data, true));
        card.append(node("small", `תקופת המכסה: ${data.period_start} – ${data.period_end}`, "muted"));
        const configured = this._data.membership_policies?.memberships?.find(x => x.id === m.id) || data;
        card.append(policySummary({...configured, policy: {...configured.policy, ...(data.policy?.state !== "ready" ? {state: data.policy.state, reason: data.policy.reason} : {})}}, this.canWrite() ? () => this.editMembershipPolicy(configured) : null));
      }
      else card.append(node("p", "לא קיימת מכסה מחושבת למנוי הזה.", "muted"));
      details.append(card);
    }
    if (members.length) wrap.append(details);
    return wrap;
  }
  editMembershipPolicy(member) {
    const context = {...this.context()};
    const entry = this._entry.entry_id;
    policyDialog({host: this.shadowRoot, member,
      categories: this._data.membership_policies?.categories || [],
      save: async data => {
        if (entry !== this._entry.entry_id) throw new Error("חיבור Arbox השתנה. פתחו את ההגדרה מחדש");
        return this.act('membership_policy_save', {...data, membership_id: member.id}, context, {close: false});
      }});
  }
  render_overview() {
    const next = this._data.summary?.next_class;
    if (next) {
      const hero = node("section", null, "hero");
      hero.append(
        node("p", "האימון הבא שלך", "eyebrow"),
        node("h2", next.category_name),
        node("p", [fmtDate(next.date), next.start_time?.slice(0, 5), next.coach_name]
          .filter(Boolean).join(" · ")),
        button("לפרטי האימון", () => this.openSession(next), "primary"),
      );
      this._content.append(hero);
    } else {
      const empty = node("section", null, "next-workout-empty");
      empty.append(
        node("span", "אין כרגע אימון מוזמן"),
        button("ללוח האימונים ←", () => this.navigate("schedule"), "text-button"),
      );
      this._content.append(empty);
    }
    this._content.append(this.quota());
    const pending = (this._data.journal?.entries || []).filter(
      (r) => !hasFeedback(r) && !r.dismissed_at,
    );
    if (pending.length) {
      const section = node("section", null, "pending");
      section.append(
        node("h2", `${pending.length} אימונים מחכים למילה שלך`),
        node("p", "רגע קצר של משוב, כדי לזכור מה עבד טוב."),
      );
      for (const row of pending.slice(0, 3))
        section.append(
          button(
            `${row.category_name} · ${fmtDate(row.date)} ←`,
            () =>
              this.canWrite() ? this.openJournal(row) : this.openSession(row),
            "text-button",
          ),
        );
      this._content.append(section);
    }
    this._content.append(node("h2", "האימונים הקרובים"));
    this.list(
      (this._data.me?.sessions || [])
        .filter((s) => !s.automation_skipped)
        .slice(0, 5),
    );
  }
  select(label, options, value, onchange) {
    const wrap = node("label", label, "field");
    const select = node("select");
    select.dataset.focusKey = `filter-${label}`;
    select.append(...options.map(([v, t]) => new Option(t, v)));
    select.value = value || "";
    select.onchange = () => onchange(select.value);
    wrap.append(select);
    return wrap;
  }
  render_schedule() { renderCalendar(this); }
  render_mine() {
    this._content.append(this.quota());
    renderCalendar(this, {mine: true});
  }
  render_journal() { renderJournal(this); }
  render_automations() {
    const heading = node("div", null, "section-heading");
    heading.append(node("h2", "כללים קבועים"));
    if (this.canWrite())
      heading.append(button("＋ כלל חדש", () => this.ruleEditor(), "primary"));
    this._content.append(heading);
    const rules = this._data.rules?.rules || [];
    if (!rules.length)
      this._content.append(
        node(
          "p",
          "אין עדיין כללים. צרו כלל כדי לתכנן את השבוע בדרך שלכם.",
          "empty",
        ),
      );
    for (const r of rules) {
      const card = node("article", null, "rule-card");
      card.append(
        node("h3", r.name),
        node(
          "span",
          r.mode === "autobook" ? "הרשמה אוטומטית" : "התראה",
          "badge",
        ),
        node(
          "p",
          [
            r.enabled ? "פעיל" : "כבוי",
            (r.categories || []).join(", ") || "כל השיעורים",
            (r.coaches || []).join(", ") || "כל המאמנים",
            (r.weekdays || [])
              .map(
                (d) =>
                  ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"][d],
              )
              .join(", ") || "כל הימים",
            [r.time_from, r.time_to].filter(Boolean).join("–"),
          ]
            .filter(Boolean)
            .join(" · "),
        ),
      );
      if (this.canWrite()) {
        card.append(
          button("עריכה", () => this.ruleEditor(r)),
          button(r.enabled ? "כיבוי" : "הפעלה", () =>
            this.act(
              "rule_save",
              this.rulePayload({ ...r, enabled: !r.enabled }),
              this.context(),
            ),
          ),
          button(
            "מחיקה",
            () =>
              this.confirm("למחוק את הכלל הקבוע?", () =>
                this.act("rule_delete", { rule_id: r.id }, this.context()),
              ),
            "danger",
          ),
        );
      }
      this._content.append(card);
    }
    const vh = node("div", null, "section-heading");
    vh.append(node("h2", "חופשות"));
    if (this.canWrite())
      vh.append(button("＋ חופשה", () => this.vacationEditor()));
    this._content.append(vh);
    const vacations = this._data.vacations || {};
    const draw = (v) => {
      const card = node("article", null, "rule-card");
      card.append(
        node("h3", `${fmtDate(v.date_from)} — ${fmtDate(v.date_to)}`),
        node(
          "p",
          [
            v.block_autobook ? "עוצרת הרשמה אוטומטית" : "",
            v.block_notify ? "משהה התראות" : "",
          ]
            .filter(Boolean)
            .join(" · "),
        ),
      );
      if (this.canWrite())
        card.append(
          button("עריכה", () => this.vacationEditor(v)),
          button(
            "מחיקה",
            () =>
              this.confirm("למחוק חופשה ולחדש את התכנון במועדים האלה?", () =>
                this.act("vacation_delete", { vac_id: v.id }, this.context()),
              ),
            "danger",
          ),
        );
      return card;
    };
    if (!vacations.active?.length)
      this._content.append(node("p", "אין חופשות מתוכננות.", "muted"));
    for (const v of vacations.active || []) this._content.append(draw(v));
    if (vacations.history?.length) {
      const d = node("details");
      d.append(node("summary", "חופשות קודמות"));
      for (const v of vacations.history) d.append(draw(v));
      this._content.append(d);
    }
  }
  dialog(title) {
    this._dialog?.close();
    this._dialog?.remove();
    const d = node("dialog", null, "sheet");
    d.dir = "rtl";
    d.lang = "he";
    const h = node("div", null, "sheet-heading");
    const heading = node("h2", title);
    heading.id = "sheet-title";
    h.append(
      heading,
      button("✕", () => d.close(), "close"),
    );
    h.lastChild.setAttribute("aria-label", "סגירה");
    d.setAttribute("aria-labelledby", "sheet-title");
    const body = node("div", null, "sheet-body");
    d.append(h, body);
    d.addEventListener("close", () => {
      d.remove();
      if (this._dialog === d) this._dialog = null;
    });
    this.shadowRoot.append(d);
    this._dialog = d;
    d.showModal();
    return body;
  }
  confirm(text, run) {
    const parent = this._dialog;
    const d = node("dialog", null, "confirm");
    d.dir = "rtl";
    d.lang = "he";
    d.append(node("h2", "אישור פעולה"), node("p", text));
    const b = button(
      "אישור",
      async () => {
        b.disabled = true;
        try {
          await run();
          d.close();
        } catch (e) {
          d.append(node("p", e.message, "error"));
          b.disabled = false;
        }
      },
      "primary",
    );
    d.append(
      b,
      button("חזרה", () => d.close()),
    );
    d.onclose = () => {
      d.remove();
      parent?.querySelector("button")?.focus();
    };
    this.shadowRoot.append(d);
    d.showModal();
  }
  async act(action, data, context, { close = true } = {}) {
    if (!this.canWrite()) throw new Error("אין הרשאה לביצוע פעולה.");
    if (this._writing) throw new Error("פעולה אחרת עדיין מתבצעת.");
    this._writing = true;
    const entry = this._entry.entry_id;
    try {
      const result = await this._hass.callWS({
        type: "arbox/panel/action",
        entry_id: entry,
        action,
        studio_id: context.studio_id,
        data,
      });
      const response = result.data ?? result;
      if (response.needs_confirm) {
        const flag =
          response.confirm_kind === "vacation"
            ? "ignore_vacation"
            : "confirm_over_quota";
        this.confirm(response.conflict, () =>
          this.act(action, { ...data, [flag]: true }, context, { close }),
        );
        return response;
      }
      if (response.ok === false)
        throw new Error(response.error || "הפעולה לא הושלמה.");
      if (close) this._dialog?.close();
      this.toast(response.quota_note || "השינוי נשמר");
      await this._loading;
      await this.load();
      return response;
    } catch (error) {
      let detail = error.message;
      try {
        detail = JSON.parse(detail);
      } catch {}
      const message =
        typeof detail === "string"
          ? detail
          : detail?.message ||
            JSON.stringify(detail) ||
            "לא התקבל אישור מהשרת. בדקו את מצב האימון לפני ניסיון נוסף.";
      if (message.includes("late_cancel_required")) {
        this.confirm(
          "זהו ביטול מאוחר ועלול להיחשב כניסה במנוי. לבטל בכל זאת?",
          () =>
            this.act(action, { ...data, late_cancel: true }, context, {
              close,
            }),
        );
        return;
      }
      this.toast(message);
      throw error;
    } finally {
      this._writing = false;
    }
  }
  actionButton(label, action, data, context) {
    const b = button(
      label,
      async () => {
        b.disabled = true;
        try {
          await this.act(
            action,
            typeof data === "function" ? data() : data,
            context,
          );
        } catch {
        } finally {
          b.disabled = !this.canWrite();
        }
      },
      "primary",
    );
    return b;
  }
  input(parent, label, name, type = "text", value = "") {
    const wrap = node("label", label, "field");
    const input = node("input");
    input.name = name;
    input.type = type;
    input.value = value ?? "";
    wrap.append(input);
    parent.append(wrap);
    return input;
  }
  reasons(parent) {
    const select = node("select");
    select.name = "reason_code";
    for (const [k, v] of Object.entries(REASONS))
      select.append(new Option(v, k));
    const label = node("label", "סיבת ההיעדרות / הביטול", "field");
    label.append(select);
    parent.append(label);
    const other = this.input(parent, "פירוט (לסיבה ״אחר״)", "reason_text");
    other.maxLength = 500;
    other.parentElement.hidden = true;
    select.onchange = () => {
      other.parentElement.hidden = select.value !== "other";
      other.required = select.value === "other";
    };
    return () => ({
      reason_code: select.value,
      reason_text: other.value.trim() || null,
    });
  }
  openSession(s) {
    const body = this.dialog(s.category_name || "פרטי האימון"),
      context = { ...this.context() };
    body.append(
      node(
        "p",
        [
          fmtDate(s.date),
          `${s.start_time || ""}–${s.end_time || ""}`,
          s.coach_name,
        ]
          .filter(Boolean)
          .join(" · "),
        "detail-meta",
      ),
      node("span", sessionStatus(s)[0], "badge"),
      node("p", s.category_bio || "הסטודיו לא הוסיף תיאור לשיעור הזה."),
    );
    const planning = s.planning || this._data.summary?.quota?.plan_states?.[String(s.schedule_id)];
    const past =
      this._tab === "journal" ||
      s.booking_option === "past" ||
      `${s.date}T${s.end_time || s.start_time}` < this.studioNow();
    if (past && planning?.state !== 'uncertain') {
      const saved =
        (this._data.journal?.entries || []).find(
          (r) => r.schedule_id === s.schedule_id,
        ) || s;
      body.append(node("p", STATUS[s.status] || "סטטוס הגעה לא ידוע"));
      const ratings = {
        positive: "מעולה 🤩",
        neutral: "בסדר 🙂",
        negative: "פחות 😕",
        not_applicable: "ללא דירוג",
      };
      if (saved.class_feedback)
        body.append(
          node(
            "p",
            `השיעור: ${ratings[saved.class_feedback] || saved.class_feedback}`,
          ),
        );
      if (saved.coach_feedback)
        body.append(
          node(
            "p",
            `המאמן/ת: ${ratings[saved.coach_feedback] || saved.coach_feedback}`,
          ),
        );
      if (s.reason_code)
        body.append(
          node(
            "p",
            `סיבה: ${s.reason_code === "other" ? s.reason_text : REASONS[s.reason_code] || s.reason_code}`,
          ),
        );
      if (saved.notes) body.append(node("blockquote", saved.notes));
      for (const e of saved.exercises || [])
        body.append(
          node(
            "p",
            [
              e.name,
              e.sets != null ? `${e.sets} סטים` : "",
              e.reps != null ? `${e.reps} חזרות` : "",
              e.weight != null ? `${e.weight} ק״ג` : "",
              e.duration_seconds != null ? `${e.duration_seconds} שניות` : "",
              e.attempts != null ? `${e.attempts} ניסיונות` : "",
              e.distance != null ? `${e.distance} מ׳` : "",
              e.notes,
            ]
              .filter(Boolean)
              .join(" · "),
          ),
        );
      if (this.canWrite()) {
        body.append(
          button(
            hasFeedback(saved) ? "עריכת המשוב" : "מילוי משוב",
            () => this.openJournal({ ...s, ...saved }),
            "primary",
          ),
        );
        const getReason = this.reasons(body);
        body.append(
          this.actionButton(
            "הגעתי",
            "attendance",
            { schedule_id: s.schedule_id, status: "attended" },
            context,
          ),
          this.actionButton(
            "לא הגעתי",
            "attendance",
            () => ({
              schedule_id: s.schedule_id,
              status: "missed",
              ...getReason(),
            }),
            context,
          ),
        );
      }
      return;
    }
    body.append(
      node(
        "p",
        `${s.registered ?? "—"} / ${s.max_users ?? "—"} רשומים${s.free > 0 ? ` · ${s.free} מקומות פנויים` : ""}`,
      ),
      node("p", s.registration_note || ""),
    );
    if (!this.canWrite()) {
      body.append(node("p", "החשבון שלך מוגדר לצפייה בלבד.", "muted"));
      return;
    }
    if (planning) {
      const label = this._data.summary?.memberships?.find(m => m.id === planning.membership_user_id)?.plan;
      body.append(node("p", [planning.reason, label].filter(Boolean).join(" · "), planning.state === "ready" ? "muted" : "warning"));
      if (planning.state === 'uncertain') {
        body.append(button('בדיקת מצב ההזמנה', () => this.act('planning_reconcile', {schedule_id:s.schedule_id}, context)),
          button('בדקתי בארבוקס: האימון לא מוזמן', () => this.confirm('לחדש את התכנון? יש לאשר רק אחרי שבדקתם בארבוקס שאין הרשמה או המתנה לאימון.',
            () => this.act('planning_reconcile', {schedule_id:s.schedule_id, confirm_not_booked:true}, context))));
        return;
      }
    }
    let membership;
    const members = (this._data.summary?.memberships || []).filter(
      (m) =>
        m.active !== false &&
        (!m.start || m.start <= s.date) &&
        (!m.end || m.end >= s.date),
    );
    if (members.length) {
      const wrap = node("label", "באיזה מנוי להשתמש?", "field");
      membership = node("select");
      membership.append(new Option("בחירה אוטומטית של המערכת", ""));
      for (const m of members)
        membership.append(new Option(m.plan || String(m.id), String(m.id)));
      wrap.append(membership);
      body.append(wrap);
    }
    const payload = () => ({
      schedule_id: s.schedule_id,
      membership_user_id: membership?.value ? Number(membership.value) : null,
    });
    if (s.user_booked || s.user_in_standby) {
      const getReason = this.reasons(body);
      body.append(
        button(
          s.user_booked ? "ביטול הרשמה" : "יציאה מההמתנה",
          () =>
            this.confirm("לבטל את ההשתתפות באימון הזה?", () =>
              this.act(
                "cancel",
                { schedule_id: s.schedule_id, ...getReason() },
                context,
              ),
            ),
          "danger",
        ),
      );
    } else if (s.watched)
      body.append(
        this.actionButton(
          "ביטול התזמון",
          "unwatch",
          { schedule_id: s.schedule_id },
          context,
        ),
      );
    else if (s.automation_skipped)
      body.append(
        this.actionButton(
          "החזרת האימון לאוטומציה",
          "restore",
          { schedule_id: s.schedule_id },
          context,
        ),
      );
    else {
      if (s.planning_source === "autobook" || s.autobook_match)
        body.append(
          this.actionButton(
            "דלג על האימון הזה בלבד",
            "skip",
            { schedule_id: s.schedule_id },
            context,
          ),
        );
      if (!s.blocked) {
        if (s.registration_open && s.booking_option === "insertScheduleUser")
          body.append(
            this.actionButton("הרשמה לאימון", "book", payload, context),
          );
        else if (s.registration_open && s.booking_option === "insertStandby")
          body.append(
            this.actionButton("כניסה להמתנה", "standby", payload, context),
          );
        else if (!s.registration_open) {
          const allow = node("label", null, "check");
          const cb = node("input");
          cb.type = "checkbox";
          allow.append(
            cb,
            document.createTextNode("אפשר להצטרף להמתנה כשהאימון מלא"),
          );
          body.append(
            allow,
            this.actionButton(
              "תזמון הרשמה עם פתיחת החלון",
              "watch",
              () => ({ ...payload(), allow_standby: cb.checked }),
              context,
            ),
          );
        }
      }
    }
    body.append(
      button("יצירת כלל קבוע מהאימון", () =>
        this.ruleEditor({
          name: s.category_name,
          categories: [s.category_name],
          coaches: s.coach_name ? [s.coach_name] : [],
          weekdays: [(new Date(`${s.date}T12:00:00`).getDay() + 6) % 7],
          time_from: s.start_time,
          time_to: s.start_time,
          mode: "autobook",
          enabled: true,
        }),
      ),
    );
  }
  async openJournal(row) {
    if (!this.canWrite()) {
      this.openSession(row);
      return;
    }
    const body = this.dialog("משוב על האימון"),
      context = { ...this.context() },
      entry = this._entry.entry_id;
    const root = node("div", null, "feedback-root");
    const shadow = root.attachShadow({ mode: "open" });
    const css = node("link");
    css.rel = "stylesheet";
    css.href = new URL("./feedback.css?v=3.1.0", import.meta.url).href;
    const content = node("div");
    content.dir = "rtl";
    content.innerHTML = feedbackTemplate;
    shadow.append(css, content);
    body.append(root);
    await mountFeedback(content, async (method, feedback) => {
      if (method === "GET") {
        const journal = this._data.journal || (await this.read("journal")).data;
        return {
          session: row,
          saved: row,
          level: "full",
          catalogue: journal.catalogue || [],
          metric_labels: journal.metric_labels || {},
          demo: false,
        };
      }
      if (entry !== this._entry.entry_id)
        throw new Error("החשבון השתנה. פתחו את המשוב מחדש.");
      return this.act(
        "journal_save",
        { schedule_id: row.schedule_id, ...feedback },
        context,
        { close: false },
      );
    });
  }
  rulePayload(r) {
    return Object.fromEntries(
      [
        "id",
        "name",
        "enabled",
        "coaches",
        "categories",
        "weekdays",
        "time_from",
        "time_to",
        "mode",
      ]
        .filter((k) => r[k] !== undefined)
        .map((k) => [k, r[k]]),
    );
  }
  ruleEditor(rule = {}) {
    const body = this.dialog(rule.id ? "עריכת כלל" : "כלל חדש"),
      context = { ...this.context() },
      form = node("form");
    const name = this.input(form, "שם הכלל", "name", "text", rule.name);
    name.required = true;
    name.maxLength = 160;
    const mode = node("select");
    mode.name = "mode";
    mode.append(
      new Option("הרשמה אוטומטית", "autobook"),
      new Option("התראה בלבד", "notify"),
    );
    mode.value = rule.mode || "autobook";
    const ml = node("label", "מה יקרה כשמתפנה מקום?", "field");
    ml.append(mode);
    form.append(ml);
    const selected = {};
    for (const [key, label] of [
      ["categories", "שיעורים"],
      ["coaches", "מאמנים"],
    ]) {
      const field = node("fieldset");
      field.append(node("legend", `${label} · ללא בחירה = הכול`));
      const available = [
        ...new Set([
          ...(this._data.facets?.[key] || []).map((x) =>
            typeof x === "string" ? x : x.name,
          ),
          ...(rule[key] || []),
        ]),
      ].filter(Boolean);
      selected[key] = [];
      for (const value of available) {
        const l = node("label", null, "check"),
          cb = node("input");
        cb.type = "checkbox";
        cb.value = value;
        cb.checked = (rule[key] || []).includes(value);
        selected[key].push(cb);
        l.append(cb, document.createTextNode(value));
        field.append(l);
      }
      if (!available.length) {
        const input = this.input(field, "שמות מופרדים בפסיק", key);
        selected[key] = input;
      }
      form.append(field);
    }
    const days = node("fieldset");
    days.append(node("legend", "ימים · ללא בחירה = כל השבוע"));
    const checks = [];
    for (const [d, label] of [
      [6, "ראשון"],
      [0, "שני"],
      [1, "שלישי"],
      [2, "רביעי"],
      [3, "חמישי"],
      [4, "שישי"],
      [5, "שבת"],
    ]) {
      const cb = node("input");
      cb.type = "checkbox";
      cb.value = String(d);
      cb.checked = (rule.weekdays || []).includes(d);
      checks.push(cb);
      const l = node("label", null, "check");
      l.append(cb, document.createTextNode(label));
      days.append(l);
    }
    form.append(days);
    const from = this.input(form, "משעה", "time_from", "time", rule.time_from),
      to = this.input(form, "עד שעה", "time_to", "time", rule.time_to);
    const enabled = node("input");
    enabled.type = "checkbox";
    enabled.checked = rule.enabled ?? true;
    const label = node("label", null, "check");
    label.append(enabled, document.createTextNode("הכלל פעיל"));
    form.append(label);
    const save = node("button", "שמירת הכלל", "primary");
    save.type = "submit";
    form.append(save);
    form.onsubmit = async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        await this.act(
          "rule_save",
          {
            id: rule.id || null,
            name: name.value.trim(),
            mode: mode.value,
            enabled: enabled.checked,
            categories: Array.isArray(selected.categories)
              ? selected.categories.filter((x) => x.checked).map((x) => x.value)
              : selected.categories.value
                  .split(",")
                  .map((x) => x.trim())
                  .filter(Boolean),
            coaches: Array.isArray(selected.coaches)
              ? selected.coaches.filter((x) => x.checked).map((x) => x.value)
              : selected.coaches.value
                  .split(",")
                  .map((x) => x.trim())
                  .filter(Boolean),
            weekdays: checks
              .filter((x) => x.checked)
              .map((x) => Number(x.value)),
            time_from: from.value || null,
            time_to: to.value || null,
          },
          context,
        );
      } catch {
      } finally {
        save.disabled = false;
      }
    };
    body.append(form);
  }
  vacationEditor(v = {}) {
    const body = this.dialog(v.id ? "עריכת חופשה" : "חופשה חדשה"),
      context = { ...this.context() },
      form = node("form");
    const from = this.input(
        form,
        "מיום",
        "date_from",
        "date",
        v.date_from || this._date,
      ),
      to = this.input(
        form,
        "עד יום",
        "date_to",
        "date",
        v.date_to || this._date,
      );
    from.required = to.required = true;
    const checks = {};
    for (const [key, title] of [
      ["block_autobook", "השהיית הרשמות אוטומטיות"],
      ["block_notify", "השהיית התראות"],
    ]) {
      const cb = node("input");
      cb.type = "checkbox";
      cb.checked = v[key] ?? true;
      checks[key] = cb;
      const label = node("label", null, "check");
      label.append(cb, document.createTextNode(title));
      form.append(label);
    }
    const save = node("button", "שמירת החופשה", "primary");
    save.type = "submit";
    form.append(save);
    form.onsubmit = async (e) => {
      e.preventDefault();
      if (to.value < from.value) {
        to.setCustomValidity("תאריך הסיום צריך להיות אחרי ההתחלה");
        to.reportValidity();
        return;
      }
      to.setCustomValidity("");
      save.disabled = true;
      try {
        await this.act(
          "vacation_save",
          {
            id: v.id || null,
            date_from: from.value,
            date_to: to.value,
            block_notify: checks.block_notify.checked,
            block_autobook: checks.block_autobook.checked,
          },
          context,
        );
      } catch {
      } finally {
        save.disabled = false;
      }
    };
    to.oninput = () => to.setCustomValidity("");
    body.append(form);
  }
}
if (!customElements.get("arbox-app-panel"))
  customElements.define("arbox-app-panel", ArboxAppPanel);
