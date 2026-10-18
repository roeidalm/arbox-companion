const test = require('node:test');
const assert = require('node:assert/strict');
const {load, options} = require('../frontend/studio-navigation.js');

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

test('inactive connected studios stay visible but disabled; removed studios stay hidden', () => {
  const choices = options({studios: [{id: 10, name: 'Active'}, {id: 20, name: 'Joe'}], affiliations: [
    {id: 10, name: 'Active', has_active_membership: true},
    {id: 30, name: 'Rashty', has_active_membership: false},
    {id: 40, name: 'Removed', removed: true},
    {id: 50, name: 'Offline', has_active_membership: null},
    {id: 60, name: 'Ignored', ignored: true, has_active_membership: null},
  ]});
  assert.deepEqual(choices.map(s => s.id), [10, 20, 30, 50, 60]);
  assert.deepEqual(choices.map(s => s.available), [true, true, false, false, false]);
  assert.equal(choices[2].unavailableReason, 'ללא מנוי פעיל');
  assert.equal(choices[3].unavailableReason, 'החיבור אינו זמין');
  assert.equal(choices[4].unavailableReason, 'בהתעלמות · המעקב מושהה');
});
