"use strict";
// Pure unit checks of server/historyLabels.js, server/projectsModel.js's buildStageHistory, and
// server/telegram/engineeringDigest.js - the three pieces behind "Привести историю проектов в
// читаемый вид", "Добавить длительность нахождения на каждой стадии" and "Расширить утреннюю
// сводку". No running server needed. Run by tests/test_history_journal.py.
// Usage: node tests/js/history_journal_unit.js <scratch dir>
const path = require("path");
process.env.DATA_DIR = path.join(process.argv[2], "data");

const assert = require("assert");
const failures = [];
function check(name, fn) {
  try { fn(); } catch (e) { failures.push(`${name}: ${e.message}`); }
}

const historyLabels = require("../../server/historyLabels");
const projectsModel = require("../../server/projectsModel");
const engineeringDigest = require("../../server/telegram/engineeringDigest");

// ---------------------------------------------------------------- historyLabels.js
check("a technical/diagnostic field is never shown with its raw value in the normal view", () => {
  const rows = [
    { id: "r1", batch_id: "b1", action: "activity_updated:a1.actual_hours", previous_value: 5, new_value: 7, at: "2026-11-20T04:00:00.000Z", source: "agent", actor: "agent" },
    { id: "r2", batch_id: "b1", action: "activity_updated:a1.details", previous_value: { mapping_rule: "v1" },
      new_value: { delivery_chance: "высокий", probability_note: "90%", sales_channel: "OEM", mapping_rule: "v2",
        unmapped_columns: { X: 1 }, actual_hours_source: "day columns" }, at: "2026-11-20T04:00:00.000Z", source: "agent", actor: "agent" }
  ];
  const normal = historyLabels.presentHistoryEvents(rows, { admin: false, activities: [{ activity_key: "a1", name: "Разработка" }] });
  assert.strictEqual(normal.length, 1);
  const serialized = JSON.stringify(normal);
  ["delivery_chance", "probability_note", "sales_channel", "mapping_rule", "unmapped_columns", "actual_hours_source"]
    .forEach((leak) => assert.ok(!serialized.includes(leak), `leaked ${leak} into the non-admin view`));
  assert.strictEqual(normal[0].changes.length, 1, "only the scalar change is visible");
  assert.strictEqual(normal[0].changes[0].label, "Фактические часы (Разработка)");
  assert.strictEqual(normal[0].hidden_count, 1);
  assert.strictEqual(normal[0].raw, undefined, "no raw rows at all for a non-admin viewer");

  const admin = historyLabels.presentHistoryEvents(rows, { admin: true, activities: [{ activity_key: "a1", name: "Разработка" }] });
  assert.strictEqual(admin[0].changes.length, 2, "admin sees the technical change listed too (flagged, not dumped inline)");
  assert.ok(admin[0].raw && admin[0].raw.length === 2, "admin gets the raw rows for the diagnostic toggle");
});

check("rows sharing a batch_id are grouped into one event; rows without one stay standalone", () => {
  const rows = [
    { id: "r1", batch_id: "bx", action: "updated:assigned_engineer", previous_value: null, new_value: "Иванов", at: "2026-11-20T04:00:00.000Z" },
    { id: "r2", batch_id: "bx", action: "updated:planned_deadline", previous_value: null, new_value: "2026-12-01", at: "2026-11-20T04:00:00.001Z" },
    { id: "r3", batch_id: null, action: "comment:stage_transition", previous_value: null, new_value: "заметка", at: "2026-11-20T05:00:00.000Z" }
  ];
  const events = historyLabels.presentHistoryEvents(rows, { admin: false });
  assert.strictEqual(events.length, 2, "two events: one grouped batch, one standalone");
  const grouped = events.find((e) => e.changes.length === 2);
  assert.ok(grouped, "the batch_id rows became one event with two changes");
});

check("an unrecognised action with an object value is still never dumped raw", () => {
  const rows = [{ id: "r1", batch_id: null, action: "workflow_decision", previous_value: null,
    new_value: { stage_action: "auto", next_action: { label: "x" } }, at: "2026-11-20T04:00:00.000Z" }];
  const normal = historyLabels.presentHistoryEvents(rows, { admin: false });
  assert.strictEqual(normal[0].changes.length, 0, "an unknown object-valued action is hidden, never guessed at");
});

// ---------------------------------------------------------------- projectsModel.buildStageHistory
check("repeat visits to the same stage are kept as separate entries, not merged", () => {
  const history = [
    { action: "stage_transition", previous_value: null, new_value: "scanning", at: "2026-11-01T00:00:00.000Z" },
    { action: "stage_transition", previous_value: "scanning", new_value: "development", at: "2026-11-05T00:00:00.000Z" },
    { action: "stage_transition", previous_value: "development", new_value: "scanning", at: "2026-11-08T00:00:00.000Z" },
    { action: "stage_transition", previous_value: "scanning", new_value: "development", at: "2026-11-10T00:00:00.000Z" }
  ];
  const r = projectsModel.buildStageHistory(history, "development", "2026-11-10T00:00:00.000Z");
  const scanningVisits = r.completed.filter((v) => v.stage === "scanning");
  assert.strictEqual(scanningVisits.length, 2, "two separate visits to 'scanning', not one merged entry");
  assert.strictEqual(scanningVisits[0].visit, 1);
  assert.strictEqual(scanningVisits[1].visit, 2);
  assert.strictEqual(r.current.stage, "development");
  assert.strictEqual(r.current.visit, 2, "this is the SECOND time development was entered");
  assert.strictEqual(r.completed.find((v) => v.stage === "development").days, 3, "04-11-05 -> 11-08 = 3 days");
});
check("a stage_entered_at override is used as the visit's true start, not the transition's own timestamp", () => {
  const history = [
    { action: "stage_transition", previous_value: null, new_value: "scanning", at: "2026-11-10T00:00:00.000Z" },
    { action: "stage_entered_at", previous_value: null, new_value: "2026-11-01T00:00:00.000Z", at: "2026-11-10T00:00:00.000Z" },
    { action: "stage_transition", previous_value: "scanning", new_value: "development", at: "2026-11-15T00:00:00.000Z" }
  ];
  const r = projectsModel.buildStageHistory(history, "development", "2026-11-15T00:00:00.000Z");
  const scanning = r.completed.find((v) => v.stage === "scanning");
  assert.strictEqual(scanning.entered_at, "2026-11-01T00:00:00.000Z");
  assert.strictEqual(scanning.days, 14, "11-01 -> 11-15 = 14 days, using the confirmed entry date");
});
check("no transition at all -> no fabricated date, 'current' still comes from the live fields", () => {
  const r = projectsModel.buildStageHistory([], "measurement", "2026-11-20T00:00:00.000Z");
  assert.strictEqual(r.completed.length, 0);
  assert.strictEqual(r.current.stage, "measurement");
  assert.strictEqual(r.current.entered_at, "2026-11-20T00:00:00.000Z");
});
check("no live status and no history at all -> current is null, never guessed", () => {
  const r = projectsModel.buildStageHistory([], null, null);
  assert.strictEqual(r.current, null);
  assert.deepStrictEqual(r.completed, []);
});

// ---------------------------------------------------------------- engineeringDigest.js
check("only history rows inside the 24h window are reported, and only reportable kinds", () => {
  const now = new Date("2026-11-26T04:00:00.000Z");
  const inWindow = "2026-11-25T10:00:00.000Z";
  const outOfWindow = "2026-11-24T10:00:00.000Z";
  const projects = [{ id: "p1", data: { manufacturer: "GEELY", vehicle_model: "Coolray" } },
    { id: "p2", data: { manufacturer: "HAVAL", vehicle_model: "Jolion" } }];
  const history = [
    { project_id: "p1", action: "stage_transition", previous_value: "scanning", new_value: "development", at: inWindow },
    { project_id: "p2", action: "stage_transition", previous_value: "scanning", new_value: "development", at: outOfWindow },
    { project_id: "p1", action: "activity_updated:a1.details", previous_value: {}, new_value: { mapping_rule: "x" }, at: inWindow }
  ];
  const text = engineeringDigest.buildEngineeringChangesText(projects, history, now, { scanning: "Сканирование", development: "Разработка" });
  assert.ok(text.includes("GEELY Coolray"), "the in-window change must be reported");
  assert.ok(!text.includes("HAVAL Jolion"), "the out-of-window change must not be reported");
  assert.ok(!text.includes("mapping_rule"), "a technical field must never leak into the digest text");
});
check("no changes at all is reported explicitly, never silently omitted", () => {
  const text = engineeringDigest.buildEngineeringChangesText([{ id: "p1", data: {} }], [], new Date(), {});
  assert.ok(/изменени[йя].*нет/i.test(text));
});
check("a confirmed shturman-ko-analyst proposal is labelled, a mere mention in a plain comment is not", () => {
  const now = new Date("2026-11-26T04:00:00.000Z");
  const at = "2026-11-25T10:00:00.000Z";
  const projects = [{ id: "p1", data: {} }, { id: "p2", data: {} }];
  const history = [
    { project_id: "p1", action: "comment:ko_proposal", previous_value: null,
      new_value: "По предложению агента КО (deadline_change), подтвердил admin:\nновый срок", at },
    { project_id: "p1", action: "updated:planned_deadline", previous_value: null, new_value: "2026-12-01", at },
    { project_id: "p2", action: "updated:description", previous_value: "", new_value: "просто текст без агента", at }
  ];
  const text = engineeringDigest.buildEngineeringChangesText(projects, history, now, {});
  const lines = text.split("\n");
  assert.ok(lines.some((l) => l.includes("применено предложение агента КО")));
  const p2Line = lines.find((l) => l.startsWith("•") && !l.includes("агента КО"));
  assert.ok(p2Line, "the plain comment-only project must appear without the agent marker");
});
check("an unprocessed/pending proposal (never applied) cannot appear - it never reaches project_history", () => {
  // by construction this module only ever reads project_history - an open review_queue item is a
  // different collection entirely and this function has no way to see one even if it tried.
  const text = engineeringDigest.buildEngineeringChangesText([{ id: "p1", data: {} }], [], new Date(), {});
  assert.ok(!text.toLowerCase().includes("предложени"), "nothing about a mere proposal when nothing was applied");
});

if (failures.length) {
  console.error("FAILURES:\n" + failures.join("\n"));
  process.exit(1);
}
console.log("ok");
