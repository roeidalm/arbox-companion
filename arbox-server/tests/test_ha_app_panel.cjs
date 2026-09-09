const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
function harness() {
  const file = path.resolve(
    __dirname,
    "../../custom_components/arbox/frontend/app-panel.js",
  );
  let Panel;
  const context = {
    calendarRange: (date) => ({date_from:date,date_to:date}),
    calendarSignature: () => ({}),
    journalSignature: () => ({}),
    HTMLElement: class {
      attachShadow() {}
    },
    customElements: {
      get() {},
      define(_, ctor) {
        Panel = ctor;
      },
    },
    window: {},
    document: { hidden: false },
    URL,
    Intl,
    Date,
    Option: class {},
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(
    fs
      .readFileSync(file, "utf8")
      .replace(/^import .*;\n/gm, "")
      .replaceAll("export ", "")
      .replaceAll(
        "import.meta.url",
        JSON.stringify("https://ha.example/arbox_frontend/app-panel.js"),
      ),
    context,
  );
  const panel = new Panel(),
    calls = [];
  panel._entry = { entry_id: "account-1", can_write: true };
  panel._hass = {
    connected: true,
    callWS: async (message) => {
      calls.push(message);
      return { data: { ok: true } };
    },
  };
  panel.load = async () => {};
  panel.toast = () => {};
  return { panel, calls, context };
}
test("mutations use only HA websocket with frozen studio and entry context", async () => {
  const { panel, calls } = harness();
  await panel.act(
    "book",
    { schedule_id: 41, membership_user_id: 2 },
    { studio_id: 7 },
  );
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    {
      type: "arbox/panel/action",
      entry_id: "account-1",
      action: "book",
      studio_id: 7,
      data: { schedule_id: 41, membership_user_id: 2 },
    },
  ]);
});
test("view-only and offline clients never send a mutation", async () => {
  const { panel, calls } = harness();
  panel._entry.can_write = false;
  await assert.rejects(panel.act("book", { schedule_id: 1 }, { studio_id: 7 }));
  panel._entry.can_write = true;
  panel._hass.connected = false;
  await assert.rejects(panel.act("book", { schedule_id: 1 }, { studio_id: 7 }));
  assert.equal(calls.length, 0);
});
test("uncertain mutation is never retried automatically", async () => {
  const { panel, calls } = harness();
  panel._hass.callWS = async (msg) => {
    calls.push(msg);
    throw { code: "uncertain", message: "connection lost" };
  };
  await assert.rejects(panel.act("book", { schedule_id: 1 }, { studio_id: 7 }));
  assert.equal(calls.length, 1);
  assert.equal(panel._writing, false);
});
test("duplicate click is refused while first write is pending", async () => {
  const { panel } = harness();
  let finish;
  panel._hass.callWS = () => new Promise((resolve) => (finish = resolve));
  const first = panel.act("book", { schedule_id: 1 }, { studio_id: 7 });
  await assert.rejects(panel.act("book", { schedule_id: 1 }, { studio_id: 7 }));
  finish({ data: { ok: true } });
  await first;
});
test("vacation and quota continuation wait for explicit confirmation", async () => {
  const { panel, calls } = harness();
  let confirm;
  panel.confirm = (text, run) => (confirm = run);
  panel._hass.callWS = async (msg) => {
    calls.push(msg);
    return {
      data:
        calls.length === 1
          ? {
              needs_confirm: true,
              confirm_kind: "vacation",
              conflict: "vacation",
            }
          : { ok: true },
    };
  };
  await panel.act("watch", { schedule_id: 1 }, { studio_id: 7 });
  assert.equal(calls.length, 1);
  assert.equal(typeof confirm, "function");
  await confirm();
  assert.equal(calls[1].data.ignore_vacation, true);
  assert.equal(calls[1].studio_id, 7);
});
test("late cancellation waits for explicit confirmation", async () => {
  const { panel, calls } = harness();
  let confirm;
  panel.confirm = (text, run) => (confirm = run);
  panel._hass.callWS = async (msg) => {
    calls.push(msg);
    if (calls.length === 1)
      throw { message: JSON.stringify("late_cancel_required: confirm") };
    return { data: { ok: true } };
  };
  await panel.act(
    "cancel",
    { schedule_id: 1, reason_code: "illness" },
    { studio_id: 7 },
  );
  assert.equal(calls.length, 1);
  await confirm();
  assert.equal(calls[1].data.late_cancel, true);
  assert.equal(calls[1].data.reason_code, "illness");
});
test("hidden pages do not read or redraw", async () => {
  const { panel, context, calls } = harness();
  panel.load = Object.getPrototypeOf(panel).load;
  panel._built = true;
  context.document.hidden = true;
  await panel.load();
  assert.equal(calls.length, 0);
});

test("overlapping refreshes coalesce without invalidating slow reads", async () => {
  const { panel } = harness();
  panel.load = Object.getPrototypeOf(panel).load;
  panel._built = true;
  panel._data = { summary: {} };
  panel._error = {};
  panel._sync = {};
  panel.render = () => {};
  let resolve;
  const waiting = new Promise((r) => (resolve = r));
  const reads = [];
  panel.read = async (resource) => {
    reads.push(resource);
    await waiting;
    return { data: {}, context: { studio_id: 8 }, can_write: true };
  };
  const first = panel.load();
  const firstBatch = [...reads];
  assert.ok(firstBatch.includes("watchlist"));
  assert.equal(new Set(firstBatch).size, firstBatch.length);
  const second = panel.load();
  assert.deepEqual(reads, firstBatch, "overlapping refresh must reuse the pending batch");
  resolve();
  await Promise.all([first, second]);
  assert.deepEqual(reads, firstBatch, "completion must not trigger duplicate reads");
  assert.deepEqual(Object.keys(panel._data), firstBatch);
  assert.equal(panel.context().studio_id, 8);
});

test("switching studio clears cached data from inactive tabs while preserving open draft", async () => {
  const { panel } = harness();
  panel.load = Object.getPrototypeOf(panel).load;
  panel._built = true;
  panel._data = { summary: {}, rules: { rules: ["old studio"] } };
  panel._currentContext = { studio_id: 7 };
  panel._error = {};
  panel._sync = {};
  panel.render = () => {};
  const draft = { open: true, notes: "keep this" };
  panel._dialog = draft;
  panel.read = async () => ({
    data: {},
    context: { studio_id: 8 },
    can_write: true,
  });
  await panel.load();
  assert.equal(panel._data.rules, undefined);
  assert.equal(panel._dialog, draft);
  assert.equal(panel.context().studio_id, 8);
});

test("a mixed studio batch is refused instead of merging accounts", async () => {
  const { panel } = harness();
  panel.load = Object.getPrototypeOf(panel).load;
  panel._built = true;
  panel._data = { summary: { old: true } };
  let error;
  panel.connectionError = (message) => (error = message);
  panel.read = async (resource) => ({
    data: {},
    context: { studio_id: resource === "summary" ? 7 : 8 },
  });
  await panel.load();
  assert.ok(error);
  assert.equal(panel._data.summary.old, true);
});

test("unchanged polling data does not detach focused DOM controls", () => {
  const { panel, context } = harness();
  context.document.createElement = () => ({});
  panel.shadowRoot = { activeElement: null };
  panel._meta = {};
  let replacements = 0;
  panel._content = { replaceChildren: () => replacements++ };
  panel.render_overview = () => {};
  panel.render();
  panel.render();
  assert.equal(replacements, 1);
  panel._data.summary = { last_sync: "2026-09-05T12:00:00" };
  panel.render();
  assert.equal(replacements, 2);
});

test('My history is read lazily and keeps cancelled and accepted changes visible', async () => {
  const { panel, context } = harness();
  const make = tag => ({tag, childNodes:[], append(...children){this.childNodes.push(...children);}});
  context.document.createElement = make;
  const content = n => typeof n === 'string' ? n : [n.textContent || '', ...n.childNodes.map(content)].join(' ');
  panel._content = make('main');
  panel._tab = 'mine';
  panel._data.history = {sessions:[], changes:[{
    schedule_id:1, date:'2026-09-22', start_time:'20:15', category_name:'Modern dance', coach_name:'Noa',
    status:'planning_change_cancelled', reason_text:'Cancelled after HS changed to Modern dance',
  }, {
    schedule_id:2, date:'2026-09-24', start_time:'10:00', category_name:'Updated class',
    status:'planning_change_accepted', reason_text:'Accepted change from Original class',
  }], events:[]};
  panel.renderMineHistory();
  assert.ok(!content(panel._content).includes('Modern dance'));
  const section = panel._content.childNodes[0];
  let loads = 0; panel.load = async () => loads++;
  section.isConnected = true; section.open = true; section.ontoggle();
  assert.equal(loads, 1);
  panel._content = make('main');
  panel.renderMineHistory();
  const visible = content(panel._content);
  for (const text of ['Modern dance','Noa','HS','Original class','השינוי אושר','בוטל בעקבות שינוי'])
    assert.ok(visible.includes(text), text);
});
