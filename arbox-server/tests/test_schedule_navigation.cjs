const test = require('node:test');
const assert = require('node:assert/strict');
const navigation = require('../frontend/schedule-navigation.js');

const today = '2026-10-07';

test('every fresh load opens today while keeping the chosen calendar mode', () => {
  for (const mode of ['day', 'week', 'month']) {
    assert.deepEqual(navigation.initial(`?mode=${mode}&d=2026-09-01`, today), {
      mode, anchor: today,
    });
  }
});

test('default calendar retains the actual day as its anchor, not Sunday', () => {
  assert.deepEqual(navigation.initial('', today), {mode: 'week', anchor: today});
  assert.deepEqual(navigation.initial('?mode=year', today), {mode: 'week', anchor: today});
});

test('back navigation restores an explicitly selected date in every mode', () => {
  for (const mode of ['day', 'week', 'month']) {
    assert.deepEqual(navigation.initial(`?mode=${mode}&d=2026-09-23`, today,
      {restoreDate: true}), {mode, anchor: '2026-09-23'});
  }
});

test('back navigation without a date returns to today', () => {
  assert.deepEqual(navigation.initial('?mode=day', today, {restoreDate: true}), {
    mode: 'day', anchor: today,
  });
});

test('back navigation rejects impossible or malformed dates instead of rolling them over', () => {
  for (const date of ['2026-02-29', '2026-02-31', '2100-02-29', '2026-04-31',
    '2026-13-01', '2026-00-01', '2026-10-00', '2026-10-32', '2026-1-07', 'invalid']) {
    assert.deepEqual(navigation.initial(`?mode=day&d=${date}`, today,
      {restoreDate: true}), {mode: 'day', anchor: today}, date);
  }
});

test('valid leap dates survive back navigation', () => {
  for (const date of ['2028-02-29', '2000-02-29']) {
    assert.equal(navigation.initial(`?d=${date}`, today, {restoreDate: true}).anchor, date);
  }
});

test('today follows the studio timezone across midnight', () => {
  const instant = new Date('2026-10-06T21:30:00Z');
  assert.equal(navigation.today('Asia/Jerusalem', instant), '2026-10-07');
  assert.equal(navigation.today('America/New_York', instant), '2026-10-06');
  assert.equal(navigation.today('UTC', instant), '2026-10-06');
});

test('today follows the studio date across year boundaries', () => {
  const instant = new Date('2026-12-31T23:30:00Z');
  assert.equal(navigation.today('Asia/Jerusalem', instant), '2027-01-01');
  assert.equal(navigation.today('America/New_York', instant), '2026-12-31');
});

test('today URLs retain mode without freezing a date into bookmarks', () => {
  for (const mode of ['day', 'week', 'month']) {
    const url = navigation.url(mode, today, today);
    assert.equal(url, `/schedule?mode=${mode}`);
    assert.deepEqual(navigation.initial(new URL(url, 'https://example.test').search,
      '2026-10-08', {restoreDate: true}), {mode, anchor: '2026-10-08'});
  }
});

test('deliberately browsed dates remain available to back navigation', () => {
  const url = navigation.url('week', '2026-10-21', today);
  assert.equal(url, '/schedule?mode=week&d=2026-10-21');
  const search = new URL(url, 'https://example.test').search;
  assert.deepEqual(navigation.initial(search, today, {restoreDate: true}), {
    mode: 'week', anchor: '2026-10-21',
  });
  assert.deepEqual(navigation.initial(search, today), {mode: 'week', anchor: today});
});
