const test = require('node:test'), assert = require('node:assert/strict');
const {render, decorate} = require('../frontend/friends.js');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(k, v) { this.attributes[k] = v; }
  removeAttribute(k) { delete this.attributes[k]; }
  remove() { this.removed = true; }
}
const document = {createElement: tag => new Element(tag)};
function setup() {
  const host = new Element('main'), opened = [];
  const people = [{user_id: 1, name: 'דנה'}, {user_id: 2, name: 'דנה אחרת', image_url: 'photo'}];
  const sessions = [{schedule_id: 42, date: '2026-10-25', start_time: '09:30', connections: [people[0]]}];
  render({document, host, data: {status: 'available', people, sessions}, openSession: s => opened.push(s)});
  return {host, opened, input: host.children[2], cards: host.children[3].children, empty: host.children[4]};
}
test('friend classes start collapsed and use IDs, with explicit normal booking navigation', () => {
  const w = setup();
  assert.equal(w.cards[0].open, undefined);
  assert.equal(w.cards[0].children[1].children[1].textContent, 'פרטי האימון והרשמה');
  w.cards[0].children[1].children[1].onclick();
  assert.equal(w.opened[0].schedule_id, 42);
  assert.match(w.cards[1].children[1].textContent, /לא נמצאו/);
  assert.match(w.cards[0].children[0].children[2].textContent, /לא מאומת/);
});
test('filter only existing relationships, missing and failed avatars retain fallback', () => {
  const w = setup();
  w.input.value = 'אחרת'; w.input.oninput();
  assert.equal(w.cards[0].hidden, true); assert.equal(w.cards[1].hidden, false);
  const avatar = w.cards[1].children[0].children[0], img = avatar.children[1];
  img.onload(); assert.equal(avatar.children[0].hidden, true);
  img.onerror(); assert.equal(avatar.children[0].hidden, false); assert.equal(img.removed, true);
  w.input.value = 'missing'; w.input.oninput(); assert.equal(w.empty.hidden, false);
});
test('badges reset on new data and never duplicate or match a different session', () => {
  const button = new Element('button'); button.textContent = 'נרשמים'; button.dataset.socialSchedule = '42';
  const host = {querySelectorAll: () => [button]};
  const data = {sessions: [{schedule_id: 42, connections: [{name: 'דנה'}]}]};
  decorate(host, data); decorate(host, data);
  assert.equal(button.textContent, 'נרשמים · 1 קשרים רשומים');
  decorate(host, {sessions: [{schedule_id: 43, connections: [{name: 'דנה'}]}]});
  assert.equal(button.textContent, 'נרשמים');
});
test('withheld list differs from no connections', () => {
  const host = new Element('main');
  render({document, host, data: {status: 'unavailable'}, openSession() {}});
  assert.match(host.children[2].textContent, /אינה זמינה/);
});
