const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const context = { URL, selectedValues:value=>Array.isArray(value)?value:value?[value]:[] };
vm.runInNewContext(
  fs
    .readFileSync(
      path.resolve(
        __dirname,
        "../../custom_components/arbox/frontend/panel-journal.js",
      ),
      "utf8",
    )
    .replaceAll("export ", "")
    .replace(/^import .*filter-picker.*;$/m, '')
    .replaceAll(
      "import.meta.url",
      JSON.stringify("https://ha.example/arbox_frontend/panel-journal.js"),
    ) +
    "\nglobalThis.api={journalRows,filterJournalRows,journalStats,exerciseSeries,defaultJournalFilters};",
  context,
);
const {
  journalRows,
  filterJournalRows,
  journalStats,
  exerciseSeries,
  defaultJournalFilters,
} = context.api;
const rows = [
  {
    schedule_id: 1,
    date: "2026-09-01",
    status: "attended",
    category_name: "Movement",
    coach_name: "Dana",
    coach_feedback: "positive",
    notes: "הרגשתי טוב",
    exercises: [
      { name: "Squat", metric_type: "strength", sets: 3, reps: 8, weight: 60 },
      { name: "Squat", metric_type: "strength", sets: 1, reps: 5, weight: 70 },
    ],
  },
  {
    schedule_id: 2,
    date: "2026-08-01",
    status: "missed",
    category_name: "Pilates",
    coach_name: "Noa",
    reason_code: "illness",
  },
  {
    schedule_id: 3,
    date: "2026-09-02",
    status: "pending",
    category_name: "Movement",
    coach_name: "Dana",
  },
  {
    schedule_id: 4,
    date: "2026-09-03",
    status: "cancelled_safe",
    category_name: "Pilates",
  },
];
test("journal rows merge saved content with attendance without duplicate workouts", () => {
  const result = journalRows(rows, [{ schedule_id: 1, notes: "updated" }]);
  assert.equal(result.length, 4);
  const first = result.find((r) => r.schedule_id === 1);
  assert.equal(first.status, "attended");
  assert.equal(first.notes, "updated");
  assert.equal(result[0].schedule_id, 4);
});
test("compound filters include exercise notes, coach rating, dates and attendance", () => {
  const filtered = filterJournalRows(
    rows,
    {
      ...defaultJournalFilters(),
      search: "טוב",
      coach: "Dana",
      category: "Movement",
      exercise: "Squat",
      attendance: "attended",
      feedback: "coach_positive",
      date_from: "2026-09-01",
      date_to: "2026-09-01",
    },
    "2026-09-05",
  );
  assert.equal(filtered.length, 1);
  assert.equal(filtered[0].schedule_id, 1);
});
test("period cutoff and feedback filters are inclusive and deterministic in studio date", () => {
  const recent = filterJournalRows(rows, { period: "30" }, "2026-09-05");
  assert.equal(recent.length, 3);
  const pending = filterJournalRows(
    rows,
    { feedback: "pending", attendance: "pending" },
    "2026-09-05",
  );
  assert.equal(pending.length, 1);
  assert.equal(pending[0].schedule_id, 3);
});
test("progress excludes missed/cancelled workouts without inventing attendance", () => {
  const stats = journalStats(rows);
  assert.equal(stats.total, 4);
  assert.equal(stats.eligible, 2);
  assert.equal(stats.documented, 1);
  assert.equal(stats.attended, 1);
  assert.equal(stats.coaches.find((c) => c.name === "Dana").positive, 1);
});
test("exercise stats count sessions once and retain each measurement and volume", () => {
  const stat = journalStats(rows).exercises[0];
  assert.equal(stat.sessions, 1);
  assert.equal(stat.points.length, 2);
  assert.equal(stat.points[0].reps_total, 24);
  assert.equal(stat.points[0].volume, 1440);
});
test("trend never mixes kilograms with repetitions and preserves zero readings", () => {
  const points = [
    { date: "2026-09-01", weight: 0, reps_total: 10 },
    { date: "2026-09-02", reps_total: 15 },
    { date: "2026-09-03", weight: 20, reps_total: 8 },
  ];
  const values = exerciseSeries(points, "weight");
  assert.deepEqual(
    Array.from(values, (p) => p.value),
    [0, 20],
  );
  assert.equal(values.length, 2);
});
