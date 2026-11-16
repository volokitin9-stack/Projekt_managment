"use strict";
// Safe snapshot of a workbook that is open in Excel on a shared folder.
// Run by tests/test_snapshot.py with two valid synthetic workbooks A and B.
// Usage: node tests/js/snapshot_unit.js <scratch dir> <workbook A> <workbook B>
//
// Windows sharing violations are simulated by making fs.promises.open fail with
// EBUSY (the code Node/libuv reports for ERROR_SHARING_VIOLATION). This is NOT a
// live SMB/Excel test.

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");
const { createSource, SourceError } = require("../../server/planSource/adapters");

const [dir, fileA, fileB] = process.argv.slice(2);
const A = fs.readFileSync(fileA);
const B = fs.readFileSync(fileB);
const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");
const NAME = "AfM СIS+OEM 2026.xlsx";           // Cyrillic «С» kept exactly
const OWNER = "~$AfM СIS+OEM 2026.xlsx";
const share = path.join(dir, "share");
const SRC = path.join(share, NAME);
const results = {};
let n = 0;

function src(opts) {
  return createSource("local", Object.assign({ path: SRC, settleSeconds: 0, retrySeconds: 0, readAttempts: 3 }, opts || {}));
}
function dest() {
  n += 1;
  return path.join(dir, `snap${n}.xlsx`);
}
/** Save like Excel does: write a temp file in the same folder, then rename over the workbook. */
function excelSave(buf) {
  const tmp = path.join(share, `${crypto.randomBytes(4).toString("hex")}.tmp`);
  fs.writeFileSync(tmp, buf);
  fs.renameSync(tmp, SRC);
}
async function code(promise) {
  try {
    await promise;
  } catch (e) {
    assert.ok(e instanceof SourceError, String(e));
    return e;
  }
  throw new Error("expected an error");
}

// Record every open() of the source workbook to prove it is opened read-only only.
const realOpen = fs.promises.open;
const opens = [];
fs.promises.open = function (p, flags, ...rest) {
  if (String(p) === SRC) opens.push(flags);
  return realOpen.call(fs.promises, p, flags, ...rest);
};

(async () => {
  fs.mkdirSync(share, { recursive: true });
  excelSave(A);
  const before = fs.statSync(SRC);

  // 1. workbook closed
  let s = await src().fetchTo(dest());
  assert.strictEqual(s.sha256, sha(A));
  assert.strictEqual(s.name, NAME);
  assert.strictEqual(s.open_in_excel, false);
  assert.ok(fs.readFileSync(path.join(dir, `snap${n}.xlsx`)).equals(A));
  results.closed = true;

  // 2. workbook open in Excel: the ~$ owner file exists -> read the saved version anyway
  fs.writeFileSync(path.join(share, OWNER), "Иванов И.И.");
  const ownerBefore = fs.readFileSync(path.join(share, OWNER));
  s = await src().fetchTo(dest());
  assert.strictEqual(s.sha256, sha(A));
  assert.strictEqual(s.open_in_excel, true);
  assert.ok(fs.readFileSync(path.join(share, OWNER)).equals(ownerBefore));    // owner file untouched
  results.open_with_owner_file = true;

  // 3. open, saved contents unchanged -> same SHA-256 (the owner file does not change the hash)
  assert.strictEqual((await src().fetchTo(dest())).sha256, sha(A));
  results.open_unchanged = true;

  // 4. open, a colleague saves -> the new saved version is read
  excelSave(B);
  s = await src().fetchTo(dest());
  assert.strictEqual(s.sha256, sha(B));
  results.open_updated = true;

  // 8. owner file appearing / disappearing does not change the result
  fs.unlinkSync(path.join(share, OWNER));
  assert.strictEqual((await src().fetchTo(dest())).sha256, sha(B));
  fs.writeFileSync(path.join(share, OWNER), "Петров П.П.");
  assert.strictEqual((await src().fetchTo(dest())).sha256, sha(B));
  results.owner_file_toggling = true;

  // 5 + 10. saved during the copy: snapshot discarded, retried, then the new version is processed
  excelSave(A);
  let saved = false;
  fs.promises.open = async function (p, flags, ...rest) {
    const h = await realOpen.call(fs.promises, p, flags, ...rest);
    if (String(p) === SRC) {
      opens.push(flags);
      const read = h.read.bind(h);
      h.read = async (...args) => {
        const r = await read(...args);
        if (!saved) { saved = true; excelSave(B); }         // a colleague presses Ctrl+S mid-copy
        return r;
      };
    }
    return h;
  };
  const leftover = dest();
  s = await src().fetchTo(leftover);
  assert.strictEqual(s.sha256, sha(B));
  assert.strictEqual(s.attempts, 2);
  assert.strictEqual(s.retried, "unstable");
  results.modified_during_copy_then_retry = true;

  // ... and if it keeps changing on every attempt: postponed, nothing processed
  let flip = false;
  fs.promises.open = async function (p, flags, ...rest) {
    const h = await realOpen.call(fs.promises, p, flags, ...rest);
    if (String(p) === SRC) {
      opens.push(flags);
      const read = h.read.bind(h);
      h.read = async (...args) => { const r = await read(...args); flip = !flip; excelSave(flip ? A : B); return r; };
    }
    return h;
  };
  const d5 = dest();
  let e = await code(src().fetchTo(d5));
  assert.strictEqual(e.code, "unstable");
  assert.ok(e.message.startsWith("Файл изменяется, повторная попытка"), e.message);
  assert.ok(e.transient);
  assert.ok(!fs.existsSync(d5), "a discarded snapshot must not remain");
  results.keeps_changing = true;

  // 6. real sharing violation (EBUSY = ERROR_SHARING_VIOLATION): retried, then succeeds
  excelSave(A);
  let busy = 2;
  fs.promises.open = async function (p, flags, ...rest) {
    if (String(p) === SRC) {
      opens.push(flags);
      if (busy > 0) { busy -= 1; const x = new Error("EBUSY: resource busy or locked"); x.code = "EBUSY"; throw x; }
    }
    return realOpen.call(fs.promises, p, flags, ...rest);
  };
  s = await src().fetchTo(dest());
  assert.strictEqual(s.sha256, sha(A));
  assert.strictEqual(s.attempts, 3);
  assert.strictEqual(s.retried, "sharing_violation");
  // persistent sharing violation: postponed with a clear message, not a crash
  busy = 99;
  e = await code(src().fetchTo(dest()));
  assert.strictEqual(e.code, "sharing_violation");
  assert.ok(e.message.startsWith("Не удалось прочитать сохранённую версию Excel"), e.message);
  assert.ok(e.transient);
  // a real permission problem is not retried and not hidden
  fs.promises.open = async function (p, flags, ...rest) {
    if (String(p) === SRC) { const x = new Error("EACCES"); x.code = "EACCES"; throw x; }
    return realOpen.call(fs.promises, p, flags, ...rest);
  };
  e = await code(src().fetchTo(dest()));
  assert.strictEqual(e.code, "permission_denied");
  results.sharing_violation = true;

  // 7. corrupted local snapshot (e.g. a bad network read): detected, discarded, retried
  let corrupt = 1;
  fs.promises.open = async function (p, flags, ...rest) {
    const h = await realOpen.call(fs.promises, p, flags, ...rest);
    if (String(p) === SRC) {
      opens.push(flags);
      const read = h.read.bind(h);
      h.read = async (buf, off, len, pos) => {
        const r = await read(buf, off, len, pos);
        if (corrupt > 0 && r.bytesRead) { corrupt -= 1; buf.fill(0, Math.floor(r.bytesRead / 2), r.bytesRead); }
        return r;
      };
    }
    return h;
  };
  s = await src().fetchTo(dest());
  assert.strictEqual(s.sha256, sha(A));
  assert.strictEqual(s.retried, "corrupt_snapshot");
  corrupt = 99;
  e = await code(src().fetchTo(dest()));
  assert.strictEqual(e.code, "corrupt");
  assert.ok(e.message.startsWith("Не удалось прочитать сохранённую версию Excel"), e.message);
  fs.promises.open = async function (p, flags, ...rest) {
    if (String(p) === SRC) opens.push(flags);
    return realOpen.call(fs.promises, p, flags, ...rest);
  };
  // a truly damaged workbook (truncated) is reported, never processed
  const trunc = path.join(dir, "trunc", NAME);
  fs.mkdirSync(path.dirname(trunc), { recursive: true });
  fs.writeFileSync(trunc, A.subarray(0, Math.floor(A.length / 2)));
  e = await code(createSource("local", { path: trunc, settleSeconds: 0, retrySeconds: 0 }).fetchTo(dest()));
  assert.strictEqual(e.code, "corrupt");
  results.corrupted_snapshot = true;

  // just saved (younger than the settle time): postponed, not read half-written
  const now = Date.now();
  e = await code(src({ settleSeconds: 5, readAttempts: 1, now: () => now }).fetchTo(dest(), {
    file: SRC, name: NAME, size: A.length, mtime_ms: now - 1000, mtime: "" }));
  assert.strictEqual(e.code, "unstable");
  results.settle = true;

  // 9. many colleagues: 20 open read handles + owner file + periodic saves by another process
  excelSave(A);
  const handles = [];
  for (let i = 0; i < 20; i += 1) handles.push(await realOpen.call(fs.promises, SRC, "r"));
  const writer = spawn(process.execPath, ["-e", `
    const fs = require("fs"); const path = require("path");
    const [src, a, b] = process.argv.slice(1); const A = fs.readFileSync(a), B = fs.readFileSync(b);
    let i = 0; setInterval(() => { const t = path.join(path.dirname(src), "w" + (i++) + ".tmp");
      fs.writeFileSync(t, i % 2 ? B : A); fs.renameSync(t, src); }, 15);`, SRC, fileA, fileB], { stdio: "ignore" });
  const startM = fs.statSync(SRC).mtimeMs;
  for (let w = 0; w < 200 && fs.statSync(SRC).mtimeMs === startM; w += 1) await new Promise((r) => setTimeout(r, 10));
  const seen = new Set();
  let postponed = 0;
  const ROUNDS = 40;
  for (let i = 0; i < ROUNDS; i += 1) {
    await new Promise((r) => setTimeout(r, 7 + (i % 5) * 4));
    try {
      const r = await src({ readAttempts: 10 }).fetchTo(dest());
      assert.ok([sha(A), sha(B)].includes(r.sha256), "snapshot must equal one complete saved version");
      seen.add(r.sha256);
    } catch (x) {
      assert.strictEqual(x.code, "unstable");
      postponed += 1;
    }
  }
  writer.kill();
  await new Promise((r) => setTimeout(r, 50));
  for (const h of handles) await h.close();
  assert.strictEqual(seen.size, 2, "both saved versions were read while colleagues kept saving");
  results.many_users = true;
  results.many_users_detail = { consistent_snapshots: ROUNDS - postponed, postponed, distinct_versions: seen.size };

  // 13. the source workbook is never opened for writing and never modified by a read
  assert.ok(opens.length > 0 && opens.every((f) => f === "r"), `flags used: ${[...new Set(opens)]}`);
  excelSave(A);
  const st1 = fs.statSync(SRC);
  for (let i = 0; i < 3; i += 1) await src().fetchTo(dest());
  const st2 = fs.statSync(SRC);
  assert.strictEqual(st2.mtimeMs, st1.mtimeMs);
  assert.strictEqual(st2.size, st1.size);
  assert.ok(fs.readFileSync(SRC).equals(A));
  assert.ok(fs.existsSync(path.join(share, OWNER)));                         // owner file never deleted
  assert.ok(before.size > 0);
  results.source_preserved = true;

  console.log(JSON.stringify(results));
  process.exit(0);
})().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
