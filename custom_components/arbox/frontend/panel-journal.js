// Rich journal views share the server's saved data; these aggregates describe
// the current filter only and never infer booking, attendance or quota rules.
const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (cls) el.className = cls;
  return el;
};
const button = (text, run, cls = "") => {
  const el = node("button", text, cls);
  el.type = "button";
  el.onclick = run;
  return el;
};
const FEEDBACK = {
  positive: "מעולה",
  neutral: "בסדר",
  negative: "פחות",
  not_applicable: "ללא דירוג",
};
const ATTENDANCE = {
  attended: "הגעתי",
  missed: "לא הגעתי",
  pending: "ממתין לאישור",
  cancelled_safe: "בוטל",
  cancelled_late: "ביטול מאוחר",
  standby: "המתנה ללא הרשמה",
  standby_cancelled: "המתנה שבוטלה",
};
export const defaultJournalFilters = () => ({
  search: "",
  period: "all",
  date_from: "",
  date_to: "",
  category: "",
  coach: "",
  feedback: "",
  attendance: "",
  exercise: "",
});
export const journalHasContent = (row) =>
  !!(
    row.class_feedback ||
    row.coach_feedback ||
    row.notes ||
    row.exercises?.length
  );
export function journalRows(history = [], saved = []) {
  const entries = new Map(history.map((row) => [row.schedule_id, { ...row }]));
  for (const row of saved)
    entries.set(row.schedule_id, { ...entries.get(row.schedule_id), ...row });
  return [...entries.values()].sort((a, b) =>
    `${b.date || ""}${b.start_time || ""}`.localeCompare(
      `${a.date || ""}${a.start_time || ""}`,
    ),
  );
}
export function filterJournalRows(rows, filters, today) {
  const f = { ...defaultJournalFilters(), ...filters };
  let cutoff = "";
  if (f.period !== "all") {
    const d = new Date(`${today}T12:00:00Z`);
    d.setUTCDate(d.getUTCDate() - Number(f.period));
    cutoff = d.toISOString().slice(0, 10);
  }
  const needle = f.search.trim().toLocaleLowerCase("he");
  return rows.filter((row) => {
    if (cutoff && (row.date || "") < cutoff) return false;
    if (f.date_from && (row.date || "") < f.date_from) return false;
    if (f.date_to && (row.date || "") > f.date_to) return false;
    if (f.category && row.category_name !== f.category) return false;
    if (f.coach && row.coach_name !== f.coach) return false;
    if (f.attendance && row.status !== f.attendance) return false;
    if (f.exercise && !(row.exercises || []).some((e) => e.name === f.exercise))
      return false;
    if (f.feedback === "documented" && !journalHasContent(row)) return false;
    if (f.feedback === "pending" && journalHasContent(row)) return false;
    if (
      f.feedback === "not_applicable" &&
      row.class_feedback !== "not_applicable" &&
      row.coach_feedback !== "not_applicable"
    )
      return false;
    for (const kind of ["coach", "class"])
      if (
        f.feedback.startsWith(`${kind}_`) &&
        row[`${kind}_feedback`] !== f.feedback.slice(kind.length + 1)
      )
        return false;
    if (
      needle &&
      ![
        row.category_name,
        row.coach_name,
        row.notes,
        ...(row.exercises || []).map((e) => `${e.name} ${e.notes || ""}`),
      ]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase("he")
        .includes(needle)
    )
      return false;
    return true;
  });
}
export function journalStats(rows) {
  const coaches = new Map(),
    exercises = new Map();
  let documented = 0,
    eligible = 0,
    attended = 0,
    notes = 0;
  for (const row of rows) {
    const has = journalHasContent(row);
    if (has) documented++;
    if (has || !row.status || ["attended", "pending"].includes(row.status))
      eligible++;
    if (row.status === "attended") attended++;
    if (row.notes) notes++;
    if (row.coach_name) {
      const stat = coaches.get(row.coach_name) || {
        name: row.coach_name,
        classes: 0,
        positive: 0,
        neutral: 0,
        negative: 0,
        not_applicable: 0,
      };
      stat.classes++;
      if (Object.hasOwn(FEEDBACK, row.coach_feedback))
        stat[row.coach_feedback]++;
      coaches.set(row.coach_name, stat);
    }
    const seen = new Set();
    for (const item of row.exercises || []) {
      const key = item.name;
      const stat = exercises.get(key) || { name: key, sessions: 0, points: [] };
      if (!seen.has(key)) stat.sessions++;
      seen.add(key);
      stat.points.push({
        ...item,
        date: row.date,
        schedule_id: row.schedule_id,
        reps_total: item.reps == null ? null : (item.sets ?? 1) * item.reps,
        volume:
          item.reps != null && item.weight != null
            ? (item.sets ?? 1) * item.reps * item.weight
            : null,
      });
      exercises.set(key, stat);
    }
  }
  for (const stat of exercises.values())
    stat.points.sort((a, b) => (a.date || "").localeCompare(b.date || ""));
  return {
    total: rows.length,
    documented,
    eligible,
    attended,
    notes,
    coaches: [...coaches.values()].sort(
      (a, b) => b.classes - a.classes || a.name.localeCompare(b.name),
    ),
    exercises: [...exercises.values()].sort(
      (a, b) => b.sessions - a.sessions || a.name.localeCompare(b.name),
    ),
  };
}
const METRICS = [
  ["weight", "משקל", "ק״ג"],
  ["reps_total", "חזרות כוללות", "חזרות"],
  ["duration_seconds", "משך", "שניות"],
  ["attempts", "ניסיונות", "ניסיונות"],
  ["distance", "מרחק", "מטר"],
  ["volume", "נפח אימון", "ק״ג × חזרות"],
];
export function exerciseSeries(points, key) {
  return points
    .filter((p) => Number.isFinite(p[key]))
    .map((p) => ({ value: p[key], date: p.date, schedule_id: p.schedule_id }));
}
function state(panel) {
  return (panel._journalState ||= {
    filters: defaultJournalFilters(),
    open: {},
    focus: null,
    metric: {},
  });
}
export function journalSignature(panel) {
  return panel._journalState || null;
}
function activeCount(filters) {
  return Object.entries(filters).filter(
    ([key, value]) => value && !(key === "period" && value === "all"),
  ).length;
}
function select(label, options, value, onchange) {
  const wrap = node("label", label, "field");
  const input = node("select");
  input.dataset.focusKey = `journal-${label}`;
  for (const [v, title] of options) input.append(new Option(title, v));
  input.value = value || "";
  input.onchange = () => onchange(input.value);
  wrap.append(input);
  return wrap;
}
function field(label, type, value, onchange) {
  const wrap = node("label", label, "field");
  const input = node("input");
  input.type = type;
  input.value = value || "";
  input.dataset.focusKey = `journal-${label}`;
  input.oninput = () => onchange(input.value);
  wrap.append(input);
  return wrap;
}
function disclosure(panel, key, title, draw) {
  const s = state(panel),
    details = node("details", null, "journal-disclosure");
  const summary = node("summary", title);
  summary.dataset.focusKey = `journal-disclosure-${key}`;
  const body = node("div", null, "journal-disclosure-body");
  details.append(summary, body);
  let rendered = false;
  const populate = () => {
    if (!rendered) {
      draw(body);
      rendered = true;
    }
  };
  details.open = !!s.open[key];
  if (details.open) populate();
  details.ontoggle = () => {
    s.open[key] = details.open;
    if (details.open) populate();
  };
  return details;
}
function filterControls(panel, rows) {
  const s = state(panel),
    f = s.filters;
  const update = (key, value) => {
    f[key] = value;
    panel.render();
  };
  const count = activeCount(f);
  return disclosure(
    panel,
    "filters",
    `סינון וחיפוש${count ? ` · ${count} פעילים` : ""}`,
    (body) => {
      const grid = node("div", null, "journal-filter-grid");
      grid.append(
        field("חיפוש בהערות, בשיעורים ובתרגילים", "search", f.search, (value) =>
          update("search", value),
        ),
        select(
          "תקופה",
          [
            ["all", "כל ההיסטוריה"],
            ["30", "30 ימים אחרונים"],
            ["90", "90 ימים אחרונים"],
            ["180", "חצי שנה"],
            ["365", "שנה"],
          ],
          f.period,
          (v) => update("period", v),
        ),
        field("מתאריך", "date", f.date_from, (v) => update("date_from", v)),
        field("עד תאריך", "date", f.date_to, (v) => update("date_to", v)),
      );
      for (const [key, label, values] of [
        ["category", "שיעור", rows.map((r) => r.category_name)],
        ["coach", "מאמן/ת", rows.map((r) => r.coach_name)],
        [
          "exercise",
          "תרגיל",
          rows.flatMap((r) => (r.exercises || []).map((e) => e.name)),
        ],
      ])
        grid.append(
          select(
            label,
            [
              ["", "הכול"],
              ...[...new Set(values.filter(Boolean))].sort().map((x) => [x, x]),
            ],
            f[key],
            (v) => update(key, v),
          ),
        );
      const feedback = [
        ["", "הכול"],
        ["documented", "עם משוב"],
        ["pending", "ללא משוב"],
        ["not_applicable", "סומן ללא דירוג"],
      ];
      for (const [kind, title] of [
        ["class", "שיעור"],
        ["coach", "מאמן/ת"],
      ])
        for (const [key, label] of Object.entries(FEEDBACK).filter(
          ([k]) => k !== "not_applicable",
        ))
          feedback.push([`${kind}_${key}`, `${title} · ${label}`]);
      grid.append(
        select("דירוג ומשוב", feedback, f.feedback, (v) =>
          update("feedback", v),
        ),
        select(
          "הגעה",
          [["", "הכול"], ...Object.entries(ATTENDANCE)],
          f.attendance,
          (v) => update("attendance", v),
        ),
      );
      body.append(grid);
      if (count)
        body.append(
          button(
            "ניקוי הסינון",
            () => {
              s.filters = defaultJournalFilters();
              s.focus = null;
              panel.render();
            },
            "text-button",
          ),
        );
    },
  );
}
function summary(panel, stats) {
  const host = node("section", null, "journal-summary");
  const top = node("div", null, "journal-summary-top");
  top.append(
    node("strong", `${stats.total} אימונים בתוצאות`),
    node(
      "span",
      `${stats.attended} הגעות · ${stats.documented} משובים`,
      "muted",
    ),
  );
  host.append(top);
  const progress = node("progress");
  progress.max = Math.max(1, stats.eligible);
  progress.value = stats.documented;
  progress.setAttribute(
    "aria-label",
    `${stats.documented} מתוך ${stats.eligible} אימונים תועדו במשוב`,
  );
  host.append(
    progress,
    node(
      "p",
      stats.eligible
        ? `${stats.documented} מתוך ${stats.eligible} אימונים תועדו · ${Math.round((100 * stats.documented) / stats.eligible)}%`
        : "עדיין אין אימונים שממתינים לתיעוד",
      "muted",
    ),
  );
  return host;
}
function scoreChips(coach) {
  const wrap = node("div", null, "journal-score-chips");
  for (const [key, label] of Object.entries(FEEDBACK)) {
    if (coach[key])
      wrap.append(
        node(
          "span",
          `${label} ${coach[key]}`,
          `badge ${key === "positive" ? "booked" : ""}`,
        ),
      );
  }
  if (!wrap.children.length)
    wrap.append(node("span", "עדיין אין דירוג", "muted"));
  return wrap;
}
function trend(points, label) {
  const host = node("div", null, "journal-trend");
  if (!points.length) {
    host.append(node("p", "המדד הזה עדיין לא תועד.", "muted"));
    return host;
  }
  const start = points[0],
    last = points.at(-1);
  host.append(
    node(
      "p",
      `${label}: ${start.value} ← ${last.value}`,
      "journal-trend-values",
    ),
  );
  if (points.length < 2) {
    host.append(node("p", "נדרשים לפחות שני תיעודים כדי לראות מגמה.", "muted"));
    return host;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 320 88");
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    `${label}, ${points.length} תיעודים, ראשון ${start.value}, אחרון ${last.value}`,
  );
  const min = Math.min(...points.map((p) => p.value)),
    max = Math.max(...points.map((p) => p.value)),
    span = max - min || 1;
  const line = document.createElementNS(svg.namespaceURI, "polyline");
  line.setAttribute(
    "points",
    points
      .map(
        (p, i) =>
          `${8 + (i * 304) / (points.length - 1)},${72 - ((p.value - min) * 56) / span}`,
      )
      .join(" "),
  );
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "3");
  svg.append(line);
  host.append(
    svg,
    node("p", `${start.date || ""} — ${last.date || ""}`, "muted"),
  );
  return host;
}
function coachSection(panel, entries, stats) {
  return disclosure(
    panel,
    "coaches",
    `לפי מאמן/ת · ${stats.coaches.length}`,
    (body) => {
      if (!stats.coaches.length) {
        body.append(node("p", "אין מאמנים בתוצאות הסינון.", "muted"));
        return;
      }
      const grid = node("div", null, "journal-stat-grid");
      for (const coach of stats.coaches) {
        const card = button(
          "",
          () => {
            state(panel).focus = { type: "coach", value: coach.name };
            panel.render();
          },
          "journal-stat-button",
        );
        card.append(
          node("strong", coach.name),
          node("small", `${coach.classes} אימונים`),
          scoreChips(coach),
          node("span", "האימונים והמשובים ←", "journal-link"),
        );
        grid.append(card);
      }
      body.append(grid);
    },
  );
}
function exerciseSection(panel, entries, stats) {
  return disclosure(
    panel,
    "exercises",
    `התקדמות בתרגילים · ${stats.exercises.length}`,
    (body) => {
      if (!stats.exercises.length) {
        body.append(
          node(
            "p",
            "תעדו תרגיל במשוב על אימון כדי לעקוב כאן אחר ההתקדמות.",
            "muted",
          ),
        );
        return;
      }
      const grid = node("div", null, "journal-stat-grid");
      for (const exercise of stats.exercises) {
        const card = button(
          "",
          () => {
            state(panel).focus = { type: "exercise", value: exercise.name };
            panel.render();
          },
          "journal-stat-button",
        );
        card.append(
          node("strong", exercise.name),
          node(
            "small",
            `${exercise.sessions} אימונים · ${exercise.points.length} תיעודים`,
          ),
          node("span", "מדדים ומגמה ←", "journal-link"),
        );
        grid.append(card);
      }
      body.append(grid);
    },
  );
}
function drilldown(panel, rows, stats) {
  const s = state(panel),
    focus = s.focus;
  const wrap = node("section", null, "journal-drill");
  wrap.append(
    button(
      "← חזרה ליומן",
      () => {
        s.focus = null;
        panel.render();
      },
      "text-button",
    ),
    node("h2", focus.value),
  );
  let matched;
  if (focus.type === "coach") {
    matched = rows.filter((r) => r.coach_name === focus.value);
    const coach = stats.coaches.find((c) => c.name === focus.value);
    if (coach) wrap.append(scoreChips(coach));
  } else {
    matched = rows.filter((r) =>
      (r.exercises || []).some((e) => e.name === focus.value),
    );
    const exercise = stats.exercises.find((e) => e.name === focus.value);
    if (exercise) {
      const metrics = METRICS.filter(
        ([key]) => exerciseSeries(exercise.points, key).length,
      );
      const metric =
        metrics.find(([key]) => key === s.metric[exercise.name]) || metrics[0];
      if (metric) {
        wrap.append(
          select(
            "מדד למגמה",
            metrics.map(([key, title, unit]) => [key, `${title} (${unit})`]),
            metric[0],
            (v) => {
              s.metric[exercise.name] = v;
              panel.render();
            },
          ),
          trend(
            exerciseSeries(exercise.points, metric[0]),
            `${metric[1]} (${metric[2]})`,
          ),
        );
        const details = node("details", null, "journal-measurements");
        details.append(node("summary", "כל המדידות"));
        const table = node("table");
        const head = node("tr");
        head.append(
          node("th", "תאריך"),
          node("th", `${metric[1]} (${metric[2]})`),
          node("th", "אימון"),
        );
        table.append(head);
        for (const point of exerciseSeries(exercise.points, metric[0])) {
          const row = node("tr");
          row.append(node("td", point.date), node("td", String(point.value)));
          const cell = node("td");
          const session = matched.find(
            (r) => r.schedule_id === point.schedule_id,
          );
          if (session)
            cell.append(
              button("פתיחה", () => panel.openSession(session), "text-button"),
            );
          row.append(cell);
          table.append(row);
        }
        details.append(table);
        wrap.append(details);
      } else wrap.append(node("p", "לתרגיל הזה תועדו הערות בלבד.", "muted"));
    }
  }
  wrap.append(node("p", `${matched.length} אימונים בתוצאות הסינון`, "muted"));
  panel._content.append(wrap);
  panel.list(matched);
}
export function renderJournal(panel) {
  if (!panel.shadowRoot.querySelector("link[data-journal-style]")) {
    const css = node("link");
    css.rel = "stylesheet";
    css.href = new URL("./panel-journal.css?v=3.1.0", import.meta.url).href;
    css.dataset.journalStyle = "";
    panel.shadowRoot.append(css);
  }
  const s = state(panel);
  const entries = journalRows(
    panel._data.history?.sessions || [],
    panel._data.journal?.entries || [],
  );
  const today = panel.studioNow().slice(0, 10);
  const rows = filterJournalRows(entries, s.filters, today);
  const stats = journalStats(rows);
  panel._content.append(summary(panel, stats), filterControls(panel, entries));
  if (s.focus) {
    drilldown(panel, rows, stats);
    return;
  }
  panel._content.append(
    coachSection(panel, rows, stats),
    exerciseSection(panel, rows, stats),
  );
  if (!entries.length) {
    panel._content.append(
      node("p", "היומן יתמלא באימונים, במשובים ובתרגילים שלך.", "empty"),
    );
    return;
  }
  if (!rows.length) {
    panel._content.append(
      node("p", "לא נמצאו אימונים שמתאימים לסינון הזה.", "empty"),
    );
    return;
  }
  panel.list(rows);
}
