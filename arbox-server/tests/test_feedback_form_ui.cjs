const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function harness(allowAbsence = true) {
  class Element {
    constructor() { this.children = []; this.value = ''; this.checked = false; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    focus() {}
  }
  const nodes = new Map();
  const inputs = [new Element(), new Element()];
  const root = {
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, new Element());
      return nodes.get(selector);
    },
    querySelectorAll() { return inputs; },
  };
  const calls = [];
  const context = {
    URL, document: {createElement: () => new Element(), createTextNode: text => text},
    FormData: class { get() { return 'positive'; } },
  };
  const source = fs.readFileSync(path.join(__dirname, '../frontend/feedback-form.js'), 'utf8')
    .replaceAll('export ', '')
    .replaceAll('import.meta.url', JSON.stringify('https://example.test/feedback-form.js'));
  vm.createContext(context);
  vm.runInContext(source, context);
  await context.mountFeedback(root, async (method, body) => {
    if (method === 'GET') return {session: {category_name: 'Movement'}, level: 'full', catalogue: [], metric_labels: {}, allow_absence: allowAbsence};
    calls.push(body);
    return {ok: true, attended: body.attended !== false};
  });
  return {get: id => root.querySelector('#' + id), inputs, calls};
}

test('absence bypasses ratings and incomplete exercise fields and records only absence', async () => {
  const h = await harness();
  assert.equal(h.get('absence-option').hidden, false);
  h.get('exercises').children.push({read() { throw Error('Incomplete exercise must not be read'); }});
  h.get('absent').checked = true;
  h.get('absent').onchange();
  assert.equal(h.get('ratings').hidden, true);
  assert.equal(h.get('more').hidden, true);
  assert.ok(h.inputs.every(input => input.disabled));
  await h.get('feedback').onsubmit({preventDefault() {}, target: {}});
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [{attended: false}]);
  assert.equal(h.get('complete-title').textContent, 'נשמר שלא הגעת לאימון');
});

test('switching back restores feedback; journal edit surfaces do not offer unsupported absence', async () => {
  const h = await harness();
  h.get('absent').checked = true; h.get('absent').onchange();
  h.get('absent').checked = false; h.get('absent').onchange();
  assert.equal(h.get('ratings').hidden, false);
  assert.equal(h.get('more').hidden, false);
  assert.ok(h.inputs.every(input => !input.disabled));
  await h.get('feedback').onsubmit({preventDefault() {}, target: {}});
  assert.equal(h.calls[0].class_feedback, 'positive');
  assert.equal(h.get('complete-title').textContent, 'המשוב נשמר');
  assert.equal((await harness(false)).get('absence-option').hidden, true);
});
