const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.disabled = false; this.value = ''; this.listeners = {}; }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
  addEventListener(name, cb) { this.listeners[name] = cb; }
  showModal() { this.open = true; }
  close(value) { this.returnValue = value; this.open = false; this.listeners.close(); }
  remove() { this.removed = true; }
}
const walk = node => [node, ...node.children.flatMap(walk)];
const find = (root, text) => walk(root).find(el => el.textContent === text);
const data = () => ({default_studio_id: 1, selected_studio_id: 1, ignored_studio_ids: [], removed_studio_ids: [],
  studios: [{id: 1, name: 'Moveom'}, {id: 2, name: 'Joe'}],
  studio_affiliations: [{id: 1, name: 'Moveom', has_active_membership: true},
    {id: 2, name: 'Joe', has_active_membership: true},
    {id: 3, name: 'Rashty', has_active_membership: false}]});
function harness(d = data(), {confirm, fail = false} = {}) {
  const body = new Element('body'), host = new Element('main'), calls = [];
  body.append(host);
  const context = vm.createContext({document: {body, createElement: tag => new Element(tag)}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/studio-management.js'), 'utf8'), context);
  let refreshed = 0;
  context.StudioManagement.mount(host, d, async (url, opts) => {
    calls.push({url, body: JSON.parse(opts.body)});
    if (fail) throw Error('offline');
    return {};
  }, async () => { refreshed++; }, confirm ? {confirm} : {});
  const remove = id => walk(host).filter(el => el.textContent === 'הסר מהרשימה')[id];
  return {host, body, calls, remove, refreshed: () => refreshed};
}

test('removal opens confirmation; cancel and Escape make no request', async () => {
  for (const value of ['cancel', '']) {
    const h = harness();
    const pending = h.remove(0).onclick();
    assert.equal(h.calls.length, 0);
    const dialog = walk(h.body).find(el => el.tagName === 'dialog');
    assert.equal(dialog.open, true);
    assert.ok(find(dialog, 'להסיר את Moveom מהרשימה?'));
    assert.ok(find(dialog, 'המנוי וההרשמות שכבר בוצעו אצל הסטודיו לא יבוטלו.'));
    dialog.close(value); await pending;
    assert.equal(h.calls.length, 0);
    assert.equal(dialog.removed, true);
  }
});

test('removing default requires explicit replacement and then refreshes', async () => {
  const h = harness();
  const pending = h.remove(0).onclick();
  const dialog = walk(h.body).find(el => el.tagName === 'dialog');
  const confirm = find(dialog, 'הסר מהרשימה');
  assert.equal(confirm.disabled, true);
  const select = walk(dialog).find(el => el.tagName === 'select');
  assert.deepEqual(select.children.map(el => el.textContent), ['בחר סטודיו', 'Joe']);
  select.value = '2'; select.onchange();
  assert.equal(confirm.disabled, false);
  dialog.close('confirm'); await pending;
  assert.deepEqual(h.calls, [{url: '/api/studios/preferences', body: {
    default_studio_id: 2, ignored_studio_ids: [], removed_studio_ids: [1]}}]);
  assert.equal(h.refreshed(), 1);
});

test('nondefault removal preserves default and existing removals; restoring is explicit', async () => {
  const d = data(); d.removed_studio_ids = [3];
  const h = harness(d, {confirm: async () => ({replacement: null})});
  await h.remove(1).onclick();
  assert.deepEqual(h.calls[0].body, {default_studio_id: 1, ignored_studio_ids: [], removed_studio_ids: [3, 2]});
  const restored = harness(d);
  await find(restored.host, 'החזר לרשימה').onclick();
  assert.deepEqual(restored.calls[0].body, {default_studio_id: 1, ignored_studio_ids: [], removed_studio_ids: []});
});

test('failed save leaves controls retryable and never claims removal', async () => {
  const h = harness(data(), {confirm: async () => ({replacement: 2}), fail: true});
  await h.remove(0).onclick();
  assert.equal(h.remove(0).disabled, false);
  assert.ok(find(h.host, 'השינוי לא נשמר: offline'));
  assert.equal(h.refreshed(), 0);
});

test('last active studio is protected while inactive studios can still be removed', () => {
  const d = data(); d.studios = d.studios.slice(0, 1);
  const h = harness(d);
  assert.equal(h.remove(0).disabled, true);
  assert.equal(h.remove(2).disabled, false);
});

test('ignore and cancel ignore only change monitoring, preserving separate removals', async () => {
  const d = data(); d.removed_studio_ids = [3];
  const h = harness(d);
  const ignoreJoe = walk(h.host).filter(el => el.textContent === 'התעלם מסטודיו')[1];
  await ignoreJoe.onclick();
  assert.deepEqual(h.calls[0].body, {default_studio_id: 1, ignored_studio_ids: [2], removed_studio_ids: [3]});
  d.ignored_studio_ids = [2];
  const ignored = harness(d);
  assert.ok(find(ignored.host, 'בהתעלמות · נשאר ברשימה, המעקב מושהה'));
  await find(ignored.host, 'ביטול התעלמות').onclick();
  assert.deepEqual(ignored.calls[0].body, {default_studio_id: 1, ignored_studio_ids: [], removed_studio_ids: [3]});
});

test('ignoring default asks for a replacement without removing it', async () => {
  const h = harness();
  const pending = find(h.host, 'התעלם מסטודיו').onclick();
  const dialog = walk(h.body).find(el => el.tagName === 'dialog');
  assert.ok(find(dialog, 'להתעלם מ־Moveom?'));
  const select = walk(dialog).find(el => el.tagName === 'select');
  select.value = '2'; select.onchange();
  dialog.close('confirm'); await pending;
  assert.deepEqual(h.calls[0].body, {default_studio_id: 2, ignored_studio_ids: [1], removed_studio_ids: []});
});

test('removing and restoring an ignored studio never resumes its monitoring implicitly', async () => {
  const d = data(); d.ignored_studio_ids = [2];
  const h = harness(d, {confirm: async () => ({replacement: null})});
  await h.remove(1).onclick();
  assert.deepEqual(h.calls[0].body, {default_studio_id: 1, ignored_studio_ids: [2], removed_studio_ids: [2]});
  d.removed_studio_ids = [2];
  const restored = harness(d);
  await find(restored.host, 'החזר לרשימה').onclick();
  assert.deepEqual(restored.calls[0].body, {default_studio_id: 1, ignored_studio_ids: [2], removed_studio_ids: []});
});
