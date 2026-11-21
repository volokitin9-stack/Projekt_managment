"use strict";

// Best-effort wall-clock start time (epoch ms) of a process, read from Linux's /proc. Returns null
// if the process doesn't exist, or /proc isn't available (not Linux - e.g. local macOS/Windows
// dev). Used by server/storeLock.js to tell whether a PID currently alive is genuinely the same OS
// process a lock file recorded earlier, or an unrelated process that has since reused that PID
// number (which happens on Railway: every (re)deploy is a new container with its own PID
// namespace, starting back near PID 1 - a plain "is this PID alive" check can't tell the two apart).

const fs = require("fs");

const CLK_TCK = 100; // USER_HZ - standard on every mainstream Linux distro/container image (x86_64, arm64)

function processStartTimeMs(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf-8");
    // Format: "pid (comm) state ppid ... starttime ...". comm can itself contain spaces/parens, so
    // split after the LAST closing paren, which always ends the comm field.
    const afterComm = stat.slice(stat.lastIndexOf(")") + 2).trim();
    const fields = afterComm.split(/\s+/);
    const startTicks = Number(fields[19]); // starttime is field 22 overall; state (field 3) is index 0 here
    if (!Number.isFinite(startTicks)) return null;
    const uptimeSeconds = Number(String(fs.readFileSync("/proc/uptime", "utf-8")).trim().split(/\s+/)[0]);
    if (!Number.isFinite(uptimeSeconds)) return null;
    const bootEpochMs = Date.now() - uptimeSeconds * 1000;
    return Math.round(bootEpochMs + (startTicks / CLK_TCK) * 1000);
  } catch (e) {
    return null; // ESRCH (no such pid), ENOENT (/proc unavailable on this platform), or a parse failure
  }
}

module.exports = { processStartTimeMs };
