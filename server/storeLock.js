"use strict";

// Single-writer lock for server/store.js's data directory (<DATA_DIR>/.shturman.lock), fixed for
// Railway's persistent Volume. The data directory outlives the container: it's mounted on a
// Volume, so the lock file from the PREVIOUS container is still sitting there when a new one
// starts. PIDs do not outlive the container - each (re)deploy gets a fresh PID namespace, usually
// starting back near PID 1. A lock file that records only a bare PID can't be safely checked after
// a restart: some unrelated process in the NEW container can legitimately occupy that same PID
// number, and a plain "is this PID alive" check then wrongly reports the lock as still held -
// which is exactly the incident this file fixes ("data directory /data is in use by another
// Штурман process (pid 15)" on a brand new container that never ran a second instance of
// anything).
//
// Fix: the lock file (format: JSON {pid, startMs, acquiredAt}) records not just the PID but *when
// that specific process started*, in epoch ms, read from the kernel via /proc (server/
// procStartTime.js). A later process checks staleness by re-reading /proc for whoever currently
// holds that PID number: if the reported start time doesn't match what's recorded, the PID has
// been recycled for an unrelated process - stale, safe to take over (requirement: never leaves a
// genuinely active process's lock in place, but a restarted container's old lock never blocks the
// new one again). If it matches, it is genuinely the same, still-running process - never touched
// (requirement: two writers are never both let in).
//
// A lock file written before this fix (a bare PID, no start time - the exact shape already stuck
// on a Railway Volume when this was written) is still handled safely without discarding or
// modifying any other data: its mtime is at least as old as when the original process wrote it, so
// any process now alive under that PID in a genuinely different container must have started
// later than that mtime (a new container cannot start before the old one that wrote the lock
// stopped) - also correctly identified as stale. A legacy-format lock whose live PID started at or
// before the lock's own mtime (the one ambiguous case - same container, no way to double check
// identity) is conservatively left alone, exactly like the original plain-PID check always did.
//
// On a platform with no /proc (not Linux - e.g. local macOS/Windows dev), processStartTimeMs()
// always returns null, so every check here falls back to the original plain PID-liveness check -
// unchanged behavior there, full fix only where it's needed (Railway's Linux containers).

const fs = require("fs");
const { processStartTimeMs } = require("./procStartTime");

const IDENTITY_SLACK_MS = 2000; // rounding slack between two independent /proc readings of the same process

function pidAliveFallback(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM"; // alive, just owned by another user - still "alive" for our purposes
  }
}

/** Parses an existing lock file. Returns null if absent/empty/unparseable. */
function readLock(lockPath) {
  if (!fs.existsSync(lockPath)) return null;
  const raw = String(fs.readFileSync(lockPath, "utf-8")).trim();
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && Number.isFinite(parsed.pid)) {
      return { pid: parsed.pid, startMs: Number.isFinite(parsed.startMs) ? parsed.startMs : null };
    }
  } catch (e) { /* not JSON - legacy plain-pid format, handled below */ }
  const pid = Number(raw);
  return Number.isFinite(pid) && pid > 0 ? { pid, startMs: null } : null;
}

/** True if `lock` still protects a genuinely live, same-instance process. */
function isLockActive(lock, lockMtimeMs) {
  if (!lock || lock.pid === process.pid) return false;
  const currentStart = processStartTimeMs(lock.pid);
  if (currentStart == null) return pidAliveFallback(lock.pid);
  if (lock.startMs != null) return Math.abs(currentStart - lock.startMs) < IDENTITY_SLACK_MS;
  return currentStart <= lockMtimeMs;
}

/**
 * Acquire the single-writer lock at `lockPath`, or throw if a genuinely live process already
 * holds it. Registers release on exit/SIGINT/SIGTERM and returns the release function.
 */
function acquire(lockPath) {
  if (fs.existsSync(lockPath)) {
    const lock = readLock(lockPath);
    const lockMtimeMs = fs.statSync(lockPath).mtimeMs;
    if (isLockActive(lock, lockMtimeMs)) {
      throw new Error(`is in use by another Штурман process (pid ${lock.pid})`);
    }
  }
  fs.writeFileSync(lockPath, JSON.stringify({
    pid: process.pid, startMs: processStartTimeMs(process.pid), acquiredAt: new Date().toISOString()
  }));
  const release = () => {
    try {
      const current = readLock(lockPath);
      if (current && current.pid === process.pid) fs.unlinkSync(lockPath);
    } catch (e) { /* ignore */ }
  };
  process.on("exit", release);
  ["SIGINT", "SIGTERM"].forEach((sig) => process.on(sig, () => { release(); process.exit(0); }));
  return release;
}

module.exports = { acquire, readLock, isLockActive, pidAliveFallback };
