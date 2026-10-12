const test = require('node:test'), assert = require('node:assert/strict');
const {create} = require('../frontend/attendees.js');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.events = {}; this.attributes = {}; this.textContent = ''; this.open = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, fn) { this.events[key] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
  focus() {}
  remove() { this.removed = true; }
}
function widget(request) {
  let studio = 10;
  const document = {body: new Element('body'), createElement: tag => new Element(tag)};
  const ui = create({document, request, getStudio: () => studio});
  const dialog = document.body.children[0];
  return {ui, dialog, status: dialog.children[2], list: dialog.children[3], retry: dialog.children[4],
    setStudio: value => { studio = value; }, open: (id = 42) => ui.open({schedule_id: id}, studio, 'Flex · שני 09:30')};
}
const result = (extra = {}) => ({status: 'available', studio_id: 10, schedule_id: 42,
  registered: 1, attendees: [{name: 'דנה לוי'}], ...extra});
const pending = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };

test('closed by default, only fetches on opening, and clears names on close', async () => {
  let calls = 0;
  const w = widget(async () => { calls++; return result(); });
  assert.equal(calls, 0); assert.equal(w.dialog.open, false);
  await w.open(); assert.equal(calls, 1); assert.equal(w.list.children[0].children[1].textContent, 'דנה לוי');
  w.ui.close(); assert.equal(w.list.children.length, 0); assert.equal(w.dialog.open, false);
  await w.open(); assert.equal(calls, 2);
});
test('late answers cannot repopulate a closed dialog or replace a newer workout', async () => {
  const first = pending(), second = pending(); let signal, calls = 0;
  const w = widget((id, studio, s) => { signal = s; return ++calls === 1 ? first.promise : second.promise; });
  const old = w.open(); w.ui.close(); assert.equal(signal.aborted, true);
  const current = w.open(43);
  second.resolve(result({schedule_id: 43, attendees: [{name: 'Current'}]})); await current;
  first.resolve(result()); await old;
  assert.deepEqual(w.list.children.map(x => x.children[1].textContent), ['Current']);
});
test('studio changes and wrong response identities cannot show names', async () => {
  const p = pending(), w = widget(() => p.promise);
  const loading = w.open(); w.setStudio(20); p.resolve(result()); await loading;
  assert.equal(w.list.children.length, 0);
  const bad = widget(async () => result({studio_id: 20})); await bad.open();
  assert.equal(bad.list.children.length, 0); assert.equal(bad.retry.hidden, false);
});
test('hidden and unavailable names differ from an empty roster; failures can retry', async () => {
  for (const status of ['hidden', 'unavailable']) {
    const w = widget(async () => result({status})); await w.open();
    assert.equal(w.list.children.length, 0); assert.doesNotMatch(w.status.textContent, /אין נרשמים/);
    assert.equal(w.retry.hidden, true);
  }
  const empty = widget(async () => result({registered: 0, attendees: []})); await empty.open();
  assert.match(empty.status.textContent, /אין נרשמים/);
  let calls = 0;
  const w = widget(async () => { if (!calls++) throw Error('offline'); return result(); });
  await w.open(); assert.equal(w.status.textContent, 'offline');
  await w.retry.onclick(); assert.equal(w.retry.hidden, true); assert.equal(w.list.children.length, 1);
});
test('names are text, and a partial list shows the total without fabricating people', async () => {
  const name = '<img src=x onerror=alert(1)>';
  const w = widget(async () => result({registered: 5, attendees: [{name}]})); await w.open();
  const label = w.list.children[0].children[1];
  assert.equal(label.textContent, name); assert.equal(label.children.length, 0);
  assert.match(w.status.textContent, /1.*5/);
});

test('photos load on demand with initials until loaded and recover on error', async () => {
  const image_url = 'https://res.cloudinary.com/arbox/image/upload/member.jpg';
  const w = widget(async () => result({attendees: [{name: 'דנה לוי', image_url}, {name: 'יובל'}]}));
  assert.equal(w.list.children.length, 0);
  await w.open();
  const avatar = w.list.children[0].children[0];
  const [initials, img] = avatar.children;
  assert.equal(avatar.attributes['aria-hidden'], 'true');
  assert.equal(initials.textContent, 'דל');
  assert.equal(img.src, image_url); assert.equal(img.className, 'attendees-photo');
  assert.equal(img.alt, ''); assert.equal(img.referrerPolicy, 'no-referrer');
  assert.equal(img.loading, 'lazy');
  img.onload(); assert.equal(initials.hidden, true); assert.equal(img.className, 'attendees-photo is-loaded');
  img.onerror(); assert.equal(initials.hidden, false); assert.deepEqual(avatar.children, [initials]);
  const noPhoto = w.list.children[1].children[0];
  assert.equal(noPhoto.children.length, 1); assert.equal(noPhoto.children[0].textContent, 'י');
  w.ui.close(); assert.equal(w.list.children.length, 0);
});

test('HA can mount in a shadow root and destroy the roster while a request is pending', async () => {
  const mount = new Element('shadow-root'), document = {body: new Element('body'), createElement: tag => new Element(tag)};
  const pendingRead = pending(); let signal;
  const ui = create({document, mount, getStudio: () => 10, request: (_, studio, s) => {signal = s; return pendingRead.promise;}});
  assert.equal(document.body.children.length, 0);
  const dialog = mount.children[0];
  assert.equal(dialog.dir, 'rtl');
  const opening = ui.open({schedule_id: 42}, 10, 'Class');
  ui.destroy();
  assert.equal(signal.aborted, true); assert.equal(dialog.removed, true);
  pendingRead.resolve(result()); await opening;
  assert.equal(dialog.children[3].children.length, 0);
});
