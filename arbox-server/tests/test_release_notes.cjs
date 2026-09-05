const assert = require("node:assert/strict");
const test = require("node:test");
const data = require("../frontend/release-notes.json");
const { selectNotes, compareVersions, readSeen, shouldShow } = require("../frontend/release-notes.js");

test("only installed versions receive their own notes; development is labelled", () => {
  assert.equal(selectNotes(data, "1.45.0"), null);
  assert.equal(selectNotes(data, "unknown"), null);
  assert.equal(selectNotes(data, "1.46.0").notes.length, 1);
  assert.equal(selectNotes(data, "v1.47.0").notes.length, 3);
  assert.equal(selectNotes(data, "local-demo").development, true);
  assert.equal(selectNotes(data, "dev").development, true);
  assert.equal(compareVersions("1.100.0", "1.99.0"), 1);
});

test("one acknowledgement per installed version, with later upgrades still shown", () => {
  const model = selectNotes(data, "1.47.0");
  assert.equal(shouldShow(model, { seen: [] }), true);
  assert.equal(shouldShow(model, { seen: ["1.47.0"] }), false);
  assert.equal(shouldShow(selectNotes(data, "1.48.1"), { seen: ["1.47.0"] }), true);
  assert.equal(shouldShow(selectNotes(data, "1.49.0"), { seen: ["1.48.1"] }), true);
  assert.equal(shouldShow(selectNotes(data, "v1.47.0"), { seen: ["1.47.0"] }), false);
  assert.deepEqual(readSeen(null), { seen: [] });
  assert.deepEqual(readSeen({ getItem: () => "bad JSON" }), { seen: [] });
  assert.deepEqual(readSeen({ getItem: () => '{"seen":true}' }), { seen: [] });
});

test("skipped release action items outrank additions; summary remains at most three", () => {
  const fixture = structuredClone(data);
  fixture.releases.push({ version: "1.46.1", notes: [
    { kind: "action", title: "Update configuration", text: "Needed before use" },
    { kind: "removed", title: "Removed option", text: "Replacement available" },
  ] });
  const skipped = selectNotes(fixture, "1.47.0", "1.46.0");
  assert.equal(skipped.notes.length, 3);
  assert.deepEqual(skipped.notes.slice(0, 2).map(n => n.kind), ["action", "removed"]);
  assert.equal(selectNotes(fixture, "1.47.0", "1.46.1").notes[0].kind, "added");
  assert.equal(skipped.available.length, 3);
});


test("expanded history never repeats summary, including skipped-version notices", () => {
  const model = selectNotes(data, "1.47.0");
  assert.deepEqual(model.additional.map(r => r.version), ["1.46.0"]);
  const fixture = structuredClone(data);
  fixture.releases.push({version: "1.46.1", notes: [
    {kind: "action", title: "Required", text: "Do this"},
  ]});
  const skipped = selectNotes(fixture, "1.47.0", "1.46.0");
  assert.ok(skipped.additional.every(r => r.notes.every(n => !skipped.notes.includes(n))));
  assert.equal(skipped.notes.length + skipped.additional.flatMap(r => r.notes).length,
    skipped.available.flatMap(r => r.notes).length);
  assert.equal(selectNotes(data, "1.46.0").additional.length, 0);
});
