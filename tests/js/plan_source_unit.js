"use strict";
// Unit checks of the file-source adapters and month logic (run by tests/test_plan_source.py).
// Usage: node tests/js/plan_source_unit.js <scratch dir>
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const dir = process.argv[2];
process.env.DATA_DIR = path.join(dir, "data");
const { createSource, SourceError } = require("../../server/planSource/adapters");
const { isTempName } = require("../../server/planSource/adapters/local");
const monitor = require("../../server/planSource/monitor");

const XLSX_HEAD = Buffer.from([0x50, 0x4b, 0x03, 0x04, 1, 2, 3, 4, 5, 6]);
const results = {};

async function expectCode(promise, code) {
  try {
    await promise;
  } catch (e) {
    assert.ok(e instanceof SourceError, `expected SourceError, got ${e}`);
    assert.strictEqual(e.code, code, e.message);
    return e;
  }
  throw new Error(`expected ${code}`);
}

(async () => {
  const src = path.join(dir, "src");
  fs.mkdirSync(src, { recursive: true });

  // relative paths resolve against the configured base directory (no absolute path in the code)
  fs.writeFileSync(path.join(src, "plan.xlsx"), XLSX_HEAD);
  const rel = createSource("local", { path: "src/plan.xlsx", baseDir: dir, settleSeconds: 0 });
  const p = await rel.probe();
  assert.strictEqual(p.name, "plan.xlsx");
  const snap = await rel.fetchTo(path.join(dir, "copy1"), p);
  assert.strictEqual(snap.sha256, require("crypto").createHash("sha256").update(XLSX_HEAD).digest("hex"));
  results.relative_path_and_sha256 = true;

  // a modification between probe and the end of the copy = incomplete transfer -> copy discarded
  const p2 = await rel.probe();
  fs.appendFileSync(path.join(src, "plan.xlsx"), Buffer.from("more"));
  await expectCode(rel.fetchTo(path.join(dir, "copy2"), p2), "unstable");
  assert.ok(!fs.existsSync(path.join(dir, "copy2")), "partial copy must be removed");
  results.incomplete_transfer = true;

  // file modified just now: wait until it has settled
  const fresh = createSource("local", { path: path.join(src, "plan.xlsx"), settleSeconds: 3600 });
  await expectCode(fresh.probe(), "unstable");
  results.settle_time = true;

  // Excel lock file
  fs.writeFileSync(path.join(src, "~$plan.xlsx"), "lock");
  await expectCode(rel.probe(), "locked");
  fs.unlinkSync(path.join(src, "~$plan.xlsx"));
  results.locked = true;

  // permission / I/O errors of the location are mapped, not thrown raw
  const origStat = fs.promises.stat;
  fs.promises.stat = async () => { const e = new Error("denied"); e.code = "EACCES"; throw e; };
  await expectCode(rel.probe(), "permission_denied");
  fs.promises.stat = async () => { const e = new Error("io"); e.code = "EIO"; throw e; };
  const io = await expectCode(rel.probe(), "unavailable");
  assert.ok(io.transient);
  fs.promises.stat = origStat;
  results.permission_and_unavailable = true;

  // folder mode: temp files ignored, two candidates = ambiguous, pattern narrows
  const folder = path.join(dir, "folder");
  fs.mkdirSync(folder);
  fs.writeFileSync(path.join(folder, "AfM_plan_2026.xlsx"), XLSX_HEAD);
  fs.writeFileSync(path.join(folder, "~$AfM_plan_2026.xlsx.tmp"), "x");
  fs.writeFileSync(path.join(folder, "upload.xlsx.part"), "x");
  const fsrc = createSource("local", { path: folder, settleSeconds: 0 });
  assert.strictEqual((await fsrc.probe()).name, "AfM_plan_2026.xlsx");
  fs.writeFileSync(path.join(folder, "old_copy.xlsx"), XLSX_HEAD);
  await expectCode(fsrc.probe(), "ambiguous");
  const pat = createSource("local", { path: folder, pattern: "AfM_*.xlsx", settleSeconds: 0 });
  assert.strictEqual((await pat.probe()).name, "AfM_plan_2026.xlsx");
  assert.ok(isTempName("~$x.xlsx") && isTempName("a.crdownload") && !isTempName("plan.xlsx"));
  results.folder_mode = true;

  // missing, empty, corrupt
  await expectCode(createSource("local", { path: path.join(dir, "nope.xlsx"), settleSeconds: 0 }).probe(), "not_found");
  fs.writeFileSync(path.join(dir, "empty.xlsx"), "");
  await expectCode(createSource("local", { path: path.join(dir, "empty.xlsx"), settleSeconds: 0 }).probe(), "empty");
  fs.writeFileSync(path.join(dir, "bad.xlsx"), "this is not a workbook");
  const bad = createSource("local", { path: path.join(dir, "bad.xlsx"), settleSeconds: 0 });
  await expectCode(bad.fetchTo(path.join(dir, "copy3")), "corrupt");
  results.missing_empty_corrupt = true;

  // adapters that are prepared but not implemented, unknown types, missing config
  await expectCode(Promise.resolve().then(() => createSource("https", { path: "x" })), "not_implemented");
  await expectCode(Promise.resolve().then(() => createSource("ftp", { path: "x" })), "not_configured");
  await expectCode(Promise.resolve().then(() => createSource("", {})), "not_configured");
  results.adapter_registry = true;

  // target month: explicit, or the current month in the configured time zone
  const at = new Date("2026-11-30T22:30:00Z");
  assert.strictEqual(monitor.targetMonth({ targetMonth: "auto", timezone: "Europe/Moscow" }, at), "2026-12");
  assert.strictEqual(monitor.targetMonth({ targetMonth: "auto", timezone: "UTC" }, at), "2026-11");
  assert.strictEqual(monitor.targetMonth({ targetMonth: "2026-11", timezone: "UTC" }, at), "2026-11");
  assert.throws(() => monitor.targetMonth({ targetMonth: "2026-13", timezone: "UTC" }, at));
  assert.throws(() => monitor.targetMonth({ targetMonth: "auto", timezone: "Mars/Olympus" }, at));
  results.target_month_timezone = true;

  // dry-run outcome classification
  assert.strictEqual(monitor.classifyDryRun({ status: "Dry run" }), "ready");
  assert.strictEqual(monitor.classifyDryRun({ status: "Approval required (dry run)" }), "awaiting_approval");
  assert.strictEqual(monitor.classifyDryRun({ status: "Verification required (dry run)" }), "blocked");
  assert.strictEqual(monitor.classifyDryRun({ status: "Failed" }), "failed");
  assert.strictEqual(monitor.classifyDryRun(null), "failed");
  results.classification = true;

  console.log(JSON.stringify(results));
  process.exit(0);
})().catch((e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
