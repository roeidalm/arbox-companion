const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.value = ''; this.disabled = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
  set innerHTML(value) { throw Error('Untrusted connection data must not be parsed as HTML'); }
  focus() {}
  querySelector(tag) { return walk(this).find(el => el.tagName === tag); }
}
const walk = node => [node, ...node.children.flatMap(walk)];
const text = node => walk(node).map(el => el.textContent || '').join(' ');
const click = async (root, label) => {
  const el = walk(root).find(el => el.tagName === 'button' && el.textContent === label);
  assert.ok(el, 'button available: ' + label);
  assert.equal(el.disabled, false, 'button enabled: ' + label);
  return el.onclick();
};
const conn = (id, studios, extra = {}) => ({id, whitelabel: id, status: 'connected', studios, ...extra});
const studio = (id, name, enabled = false, extra = {}) => ({id, name, enabled, has_active_membership: true, ...extra});

async function harness(responses, afterEnable) {
  const context = vm.createContext({document: {createElement: tag => new Element(tag)}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/studio-connections.js'), 'utf8')
    .replace('export function', 'function'), context);
  const calls = [], root = new Element('root');
  const api = async (url, options) => {
    calls.push({url, body: options?.body ? JSON.parse(options.body) : undefined});
    assert.ok(responses.length, 'unexpected request: ' + url);
    const response = responses.shift();
    if (response instanceof Error) throw response;
    return typeof response === 'function' ? response() : response;
  };
  const panel = context.mountConnections(root, api, {afterEnable});
  await panel.ready;
  return {root, panel, calls};
}

test('discovery needs explicit addition and refreshes the existing selector only after enable', async () => {
  const moveom = conn('Moveom', [studio(7315, 'Moveom', true)]);
  const rashty = conn('Arbox', [studio(202, 'Rashty')]);
  let refreshed = 0;
  const h = await harness([
    {connections: [moveom]}, {connections: [moveom, rashty]},
    {connections: [moveom, conn('Arbox', [studio(202, 'Rashty', true)])]},
  ], async () => { refreshed++; });
  await click(h.root, 'חיפוש סטודיואים נוספים');
  assert.equal(refreshed, 0);
  assert.deepEqual(h.calls[1], {url: '/api/connections/discover', body: {whitelabel: 'Arbox'}});
  await click(h.root, 'הוסף לסטודיואים שלי');
  assert.deepEqual(h.calls[2], {url: '/api/connections/enable', body: {connection_id: 'Arbox', studio_id: 202}});
  assert.equal(refreshed, 1);
  assert.match(text(h.root), /Rashty נוסף/);
  assert.equal(h.calls.some(c => c.url.includes('/select')), false);
});

test('failed discovery keeps existing studios and never refreshes or changes selection', async () => {
  const h = await harness([
    {connections: [conn('Moveom', [studio(7315, 'Moveom', true)])]},
    Error('החיבור אינו זמין'),
  ], () => { throw Error('must not change selection'); });
  await click(h.root, 'חיפוש סטודיואים נוספים');
  assert.match(text(h.root), /Moveom/);
  assert.match(text(h.root), /החיבור אינו זמין/);
  assert.equal(walk(h.root).filter(el => el.textContent === '✓ נוסף').length, 1);
  assert.equal(walk(h.root).find(el => el.textContent === 'חיפוש סטודיואים נוספים').disabled, false);
});

test('duplicate studio IDs cannot be added twice across branded connections', async () => {
  const h = await harness([{connections: [
    conn('Moveom', [studio(7315, 'Moveom', true)]),
    conn('Arbox', [studio(7315, 'Moveom'), studio(202, 'Rashty')]),
    conn('Other', [studio(202, 'Rashty'), studio(303, 'Duplicate', false, {duplicate: true})]),
  ]}]);
  assert.equal(walk(h.root).filter(el => el.textContent === 'הוסף לסטודיואים שלי').length, 1);
  assert.match(text(h.root), /מופיע גם בחיבור שלמעלה/);
});

test('brand, studio and error strings render as text; input submits trimmed app name', async () => {
  const unsafe = '<img src=x onerror=alert(1)>';
  const h = await harness([
    {connections: [conn(unsafe, [studio(1, unsafe)], {status: 'error', error: unsafe})]},
    {connections: []},
  ]);
  assert.match(text(h.root), /<img src=x onerror=alert\(1\)>/);
  await click(h.root, '+ חיבור לאפליקציה ממותגת נוספת');
  const input = h.root.querySelector('input'); input.value = '  Moveom  ';
  await h.root.querySelector('form').onsubmit({preventDefault() {}});
  assert.deepEqual(h.calls[1].body, {whitelabel: 'Moveom'});
  assert.equal(walk(h.root).some(el => el.tagName === 'img'), false);
});

test('enable success with picker refresh failure reports added state without repeating enable', async () => {
  const h = await harness([
    {connections: [conn('Arbox', [studio(202, 'Rashty')])]},
    {connections: [conn('Arbox', [studio(202, 'Rashty', true)])]},
  ], async () => { throw Error('network error'); });
  await click(h.root, 'הוסף לסטודיואים שלי');
  assert.match(text(h.root), /הסטודיו נוסף, אך הבורר למעלה לא התרענן/);
  assert.equal(walk(h.root).some(el => el.textContent === 'הוסף לסטודיואים שלי'), false);
  assert.equal(h.calls.filter(c => c.url.endsWith('/enable')).length, 1);
});

test('pending discovery cannot be submitted twice and malformed refresh preserves previous data', async () => {
  let resolve;
  const pending = new Promise(r => { resolve = r; });
  const h = await harness([
    {connections: [conn('Moveom', [studio(7315, 'Moveom', true)])]},
    () => pending,
  ]);
  const search = walk(h.root).find(el => el.textContent === 'חיפוש סטודיואים נוספים');
  const first = search.onclick(); await search.onclick();
  assert.equal(h.calls.length, 2);
  resolve({unexpected: []}); await first;
  assert.match(text(h.root), /Moveom/);
  assert.match(text(h.root), /תשובת החיבורים אינה זמינה/);
});

test('unknown or inactive membership does not offer an enabled add button', async () => {
  const h = await harness([{connections: [conn('Arbox', [
    studio(1, 'Unknown', false, {has_active_membership: null}),
    studio(2, 'Inactive', false, {has_active_membership: false}),
  ])]}]);
  const add = walk(h.root).filter(el => el.textContent === 'הוסף לסטודיואים שלי');
  assert.equal(add.length, 2);
  assert.ok(add.every(el => el.disabled));
  assert.match(text(h.root), /לא ניתן לאמת את המנוי כרגע/);
  assert.match(text(h.root), /לא נמצא מנוי פעיל/);
});
