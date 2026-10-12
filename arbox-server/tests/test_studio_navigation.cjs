const test = require('node:test');
const assert = require('node:assert/strict');
const {load} = require('../frontend/studio-navigation.js');

function server(selected = 20, defaultId = 10) {
  const calls = [];
  const api = async (url, options) => {
    calls.push({url, options});
    if (options?.method === 'POST') selected = JSON.parse(options.body).studio_id;
    return {selected_studio_id: selected, default_studio_id: defaultId,
      studios: [{id: 10}, {id: 20}]};
  };
  return {api, calls};
}

test('fresh opening returns to default even when the server remembers another studio', async () => {
  const s = server();
  assert.equal((await load(s.api, {useDefault: true})).selected_studio_id, 10);
  assert.deepEqual(s.calls.map(c => c.url), ['/api/studios', '/api/studios/select', '/api/studios']);
  assert.deepEqual(JSON.parse(s.calls[1].options.body), {studio_id: 10});
});

test('ordinary selector refresh preserves the studio chosen during current use', async () => {
  const s = server();
  assert.equal((await load(s.api)).selected_studio_id, 20);
  assert.equal(s.calls.length, 1);
});

test('already selected default and installations without a default do not switch', async () => {
  for (const s of [server(10, 10), server(20, null)]) {
    await load(s.api, {useDefault: true});
    assert.equal(s.calls.length, 1);
  }
});

test('unavailable default fails visibly instead of silently showing a different studio', async () => {
  const s = server(20, 99);
  await assert.rejects(load(s.api, {useDefault: true}), /אינו זמין/);
  assert.equal(s.calls.length, 1);
});

test('failed switch does not continue as though the default was selected', async () => {
  const s = server();
  await assert.rejects(load(async (url, options) => {
    if (options) throw Error('offline');
    return s.api(url);
  }, {useDefault: true}), /offline/);
});

test('concurrent selection change is reported instead of displaying mixed studio data', async () => {
  const s = server();
  await assert.rejects(load(async (url, options) => s.api('/api/studios'),
    {useDefault: true}), /השתנתה/);
});
