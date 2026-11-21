"use strict";
// Unit checks of server/storeLock.js (and server/procStartTime.js) - the Railway-volume-survives-
// restart lock fix. Run by tests/test_store_lock.py. Usage: node tests/js/store_lock_unit.js <scratch dir>
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const dir = process.argv[2];
fs.mkdirSync(dir, { recursive: true });
const storeLock = require("../../server/storeLock");
const { processStartTimeMs } = require("../../server/procStartTime");

function lockPathFor(name) {
  return path.join(dir, name);
}

function spawnChild() {
  // A real, long-lived child process with its own, real, kernel-reported PID and start time.
  const child = spawn("sleep", ["30"], { stdio: "ignore" });
  return child;
}

function waitExit(child) {
  return new Promise((resolve) => child.on("exit", resolve));
}

const results = {};

(async () => {
  // 1. Fresh directory: no lock file at all -> acquire succeeds, release removes it
  {
    const p = lockPathFor("fresh.lock");
    const release = storeLock.acquire(p);
    assert.ok(fs.existsSync(p));
    const written = JSON.parse(fs.readFileSync(p, "utf-8"));
    assert.strictEqual(written.pid, process.pid);
    release();
    assert.ok(!fs.existsSync(p));
    results.fresh_acquire_and_release = true;
  }

  // 2. Genuinely active lock (real child, correctly recorded) -> refused, and the live child is
  //    never touched (requirement: don't remove/disturb an active process's lock)
  {
    const p = lockPathFor("active.lock");
    const child = spawnChild();
    try {
      await new Promise((r) => setTimeout(r, 50)); // let /proc/<pid>/stat exist
      const startMs = processStartTimeMs(child.pid);
      assert.ok(startMs != null, "expected /proc to report a start time for a real child on Linux");
      fs.writeFileSync(p, JSON.stringify({ pid: child.pid, startMs, acquiredAt: new Date().toISOString() }));
      assert.throws(() => storeLock.acquire(p), /in use by another/);
      assert.strictEqual(child.exitCode, null, "the active lock's own process must not be touched");
      assert.ok(fs.existsSync(p), "an active lock's file must not be removed either");
    } finally {
      child.kill("SIGKILL");
      await waitExit(child);
    }
    results.active_lock_refused_without_disturbing_the_process = true;
  }

  // 3. Dead PID (process exited) -> stale, acquire succeeds and overwrites with our own info
  {
    const p = lockPathFor("dead.lock");
    const child = spawnChild();
    await new Promise((r) => setTimeout(r, 50));
    const startMs = processStartTimeMs(child.pid);
    fs.writeFileSync(p, JSON.stringify({ pid: child.pid, startMs, acquiredAt: new Date().toISOString() }));
    child.kill("SIGKILL");
    await waitExit(child);
    const release = storeLock.acquire(p);
    assert.strictEqual(JSON.parse(fs.readFileSync(p, "utf-8")).pid, process.pid);
    release();
    results.dead_pid_lock_is_stale = true;
  }

  // 4. PID reused by an unrelated process (the exact Railway incident): a real, alive child is
  //    recorded with a deliberately WRONG start time (as if an unrelated earlier process held that
  //    same PID number in a previous container) -> must be recognized as stale, acquired safely,
  //    and the actually-alive, unrelated child must still never be touched
  {
    const p = lockPathFor("reused.lock");
    const child = spawnChild();
    try {
      await new Promise((r) => setTimeout(r, 50));
      const wrongStartMs = processStartTimeMs(child.pid) - 9_999_999; // "a different process, long ago"
      fs.writeFileSync(p, JSON.stringify({ pid: child.pid, startMs: wrongStartMs, acquiredAt: new Date(0).toISOString() }));
      const release = storeLock.acquire(p); // must NOT throw
      assert.strictEqual(JSON.parse(fs.readFileSync(p, "utf-8")).pid, process.pid);
      assert.strictEqual(child.exitCode, null, "the unrelated process at that pid must never be touched");
      release();
    } finally {
      child.kill("SIGKILL");
      await waitExit(child);
    }
    results.reused_pid_with_mismatched_identity_is_stale = true;
  }

  // 5. Legacy format (bare pid, no start time - what's already on a Railway Volume today), with a
  //    live process that started AFTER the lock file's own mtime -> cannot be the original writer
  //    (a process can't write a file before it exists) -> stale, acquire succeeds. This is the
  //    exact shape of the stuck production lock this fix unblocks.
  {
    const p = lockPathFor("legacy_stale.lock");
    fs.writeFileSync(p, "4"); // legacy plain-pid format; pid 4 need not even exist
    const past = new Date(Date.now() - 60 * 60 * 1000);
    fs.utimesSync(p, past, past); // pretend this lock is an hour old
    const child = spawnChild(); // started well after that forced-past mtime
    try {
      await new Promise((r) => setTimeout(r, 50));
      fs.writeFileSync(p, String(child.pid)); // legacy format recording the (coincidentally live) pid
      fs.utimesSync(p, past, past);
      const release = storeLock.acquire(p); // must NOT throw
      assert.strictEqual(JSON.parse(fs.readFileSync(p, "utf-8")).pid, process.pid);
      release();
    } finally {
      child.kill("SIGKILL");
      await waitExit(child);
    }
    results.legacy_lock_newer_than_mtime_is_stale = true;
  }

  // 6. Legacy format, live process that started AT/BEFORE the lock's mtime (same-container,
  //    genuinely active case - the one legacy scenario that must still be protected)
  {
    const p = lockPathFor("legacy_active.lock");
    const child = spawnChild();
    await new Promise((r) => setTimeout(r, 50));
    fs.writeFileSync(p, String(child.pid)); // legacy format
    const future = new Date(Date.now() + 60 * 60 * 1000);
    fs.utimesSync(p, future, future); // the lock's mtime is after the process's real start
    try {
      assert.throws(() => storeLock.acquire(p), /in use by another/);
      assert.ok(fs.existsSync(p));
    } finally {
      child.kill("SIGKILL");
      await waitExit(child);
    }
    results.legacy_lock_at_or_before_mtime_is_active = true;
  }

  console.log(JSON.stringify(results));
  process.exit(0);
})().catch((e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
