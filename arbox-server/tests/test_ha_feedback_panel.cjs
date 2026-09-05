const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const frontend = path.join(root, 'custom_components/arbox/frontend');

function panelHarness(token = 'scoped-token') {
  const mounted = [], listeners = new Map(), calls = [];
  class Element {
    constructor() { this.style = {}; this.isConnected = true; }
    attachShadow() { this.shadowRoot = new Element(); }
    append() {}
    replaceChildren() {}
  }
  let Panel;
  const window = {
    location: {hash: token ? `#${token}` : ''},
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: name => listeners.delete(name),
  };
  const context = {
    HTMLElement: Element, window, URL,
    document: {createElement: () => new Element()},
    customElements: {get: () => undefined, define: (_, ctor) => {Panel = ctor;}},
    feedbackTemplate: '<form></form>',
    mountFeedback: (element, request) => mounted.push(request),
  };
  const source = fs.readFileSync(path.join(frontend, 'panel.js'), 'utf8')
    .replace(/^import .*;\n/, '')
    .replaceAll('import.meta.url', JSON.stringify('https://ha.example/arbox_frontend/panel.js'));
  vm.runInNewContext(source, context);
  const panel = new Panel();
  panel.panel = {config: {}};
  panel.hass = {themes: {darkMode: true}, callWS: async message => {
    calls.push(message);
    return {entry_id: 'studio-1', demo: true};
  }};
  panel.connectedCallback();
  return {panel, window, calls, mounted, listeners};
}

test('HA reads and saves through its authenticated websocket with scoped token and resolved entry', async () => {
  const h = panelHarness();
  assert.equal(h.mounted.length, 1);
  await h.mounted[0]('GET');
  await h.mounted[0]('PUT', {notes: 'Great session'});
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [
    {type: 'arbox/feedback/read', token: 'scoped-token'},
    {type: 'arbox/feedback/save', token: 'scoped-token', entry_id: 'studio-1', feedback: {notes: 'Great session'}},
  ]);
  assert.equal(h.panel.style.colorScheme, 'dark');
});

test('opening sidebar without a notification does not send unauthorised feedback requests', async () => {
  const h = panelHarness('');
  await assert.rejects(h.mounted[0]('GET'), /פתחו את הקישור/);
  assert.equal(h.calls.length, 0);
});

test('a second notification loads its own token without reusing the previous entry', async () => {
  const h = panelHarness();
  await h.mounted[0]('GET');
  h.window.location.hash = '#another-token';
  h.listeners.get('hashchange')();
  assert.equal(h.mounted.length, 2);
  await h.mounted[1]('GET');
  assert.equal(h.calls[1].token, 'another-token');
  assert.equal(h.calls[1].entry_id, undefined);
  h.panel.disconnectedCallback();
  assert.equal(h.listeners.size, 0);
});

test('HACS and server ship exactly the same feedback layout and styles', () => {
  for (const name of ['feedback-form.js', 'feedback.css']) {
    assert.equal(fs.readFileSync(path.join(frontend, name), 'utf8'),
      fs.readFileSync(path.join(root, 'arbox-server/frontend', name), 'utf8'));
  }
});
