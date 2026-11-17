"use strict";
// Resolution order of the planning agent's Python (run by tests/test_python_resolver.py).
// Simulated Windows and Linux file systems: exists() and verify() are injected.
const assert = require("assert");
const { resolvePython } = require("../../server/pythonResolver");

const ROOT_WIN = "C:\\Users\\user\\Projekt_managment";
const VENV_WIN = "C:\\Users\\user\\Projekt_managment\\.venv\\Scripts\\python.exe";
const STORE = "C:\\Users\\user\\AppData\\Local\\Microsoft\\WindowsApps";
const results = {};

function win(files, opts = {}) {
  const set = new Set(files.map((f) => f.toLowerCase()));
  return resolvePython(Object.assign({
    platform: "win32", root: ROOT_WIN,
    env: Object.assign({ PATH: `${STORE};C:\\Windows\\system32`, SystemRoot: "C:\\Windows" }, opts.env || {}),
    exists: (p) => set.has(String(p).toLowerCase()),
    verify: opts.verify || ((exe) => (/WindowsApps/i.test(exe) ? null : exe))
  }, {}));
}

// 1. PLAN_PYTHON_EXECUTABLE wins and is used exactly (spaces kept, no quoting added)
const spaced = "C:\\Program Files\\Shturman env\\.venv\\Scripts\\python.exe";
let r = win([spaced, VENV_WIN], { env: { PLAN_PYTHON_EXECUTABLE: spaced } });
assert.deepStrictEqual([r.ok, r.executable, r.source], [true, spaced, "PLAN_PYTHON_EXECUTABLE"]);
results.configured_exact = true;

// configured but missing: diagnostic, NO silent fallback to .venv or system Python
r = win([VENV_WIN], { env: { PLAN_PYTHON_EXECUTABLE: "D:\\nope\\python.exe" } });
assert.strictEqual(r.ok, false);
assert.strictEqual(r.code, "python_not_found");
assert.ok(r.error.includes("D:\\nope\\python.exe") && r.error.includes("PLAN_PYTHON_EXECUTABLE"));
results.configured_missing = true;

// configured to the Store alias itself: refused
const alias = `${STORE}\\python.exe`;
r = win([alias], { env: { PLAN_PYTHON_EXECUTABLE: alias } });
assert.strictEqual(r.code, "python_store_alias");
results.configured_store_alias = true;

// legacy name still accepted
r = win([spaced], { env: { PLAN_AGENT_PYTHON: spaced } });
assert.strictEqual(r.executable, spaced);
results.legacy_name = true;

// 2. project .venv on Windows
r = win([VENV_WIN, `${STORE}\\python.exe`, "C:\\Windows\\py.exe"]);
assert.deepStrictEqual([r.executable, r.source], [VENV_WIN, "project .venv"]);
results.venv_windows = true;

// 3. project .venv on Linux
r = resolvePython({ platform: "linux", root: "/srv/shturman", env: { PATH: "/usr/bin" },
  exists: (p) => p === "/srv/shturman/.venv/bin/python", verify: () => null });
assert.strictEqual(r.executable, "/srv/shturman/.venv/bin/python");
results.venv_linux = true;

// 4. system fallback: Store alias on PATH is skipped, the py launcher is verified and its REAL python used
const real = "C:\\Python313\\python.exe";
r = win([`${STORE}\\python.exe`, `${STORE}\\python3.exe`, "C:\\Windows\\py.exe"],
  { verify: (exe, args) => (exe === "C:\\Windows\\py.exe" && args[0] === "-3" ? real : null) });
assert.deepStrictEqual([r.ok, r.executable, r.source], [true, real, "system (py.exe)"]);
assert.ok(!r.attempted.some((a) => /WindowsApps/i.test(a)), "the Store alias is never even tried");
results.system_fallback_skips_store_alias = true;

// only the Store alias available: clear diagnostic
r = win([`${STORE}\\python.exe`]);
assert.strictEqual(r.ok, false);
assert.ok(r.error.includes("PLAN_PYTHON_EXECUTABLE"));
results.only_store_alias = true;

// a candidate that exists but does not run is not used
r = win(["C:\\Tools\\python.exe"], { env: { PATH: "C:\\Tools" }, verify: () => null });
assert.strictEqual(r.ok, false);
results.unverified_not_used = true;

// --- root cause on the user's PC: .env copied from the old .env.example has PLAN_AGENT_PYTHON=python3,
// PATH contains .venv\Scripts (python.exe, but no python3.exe) and the Store alias python3.exe.
r = win([VENV_WIN, `${STORE}\\python3.exe`, `${STORE}\\python.exe`],
  { env: { PLAN_AGENT_PYTHON: "python3", PATH: `C:\\Users\\user\\Projekt_managment\\.venv\\Scripts;${STORE}` } });
assert.deepStrictEqual([r.ok, r.executable, r.source], [true, VENV_WIN, "project .venv"]);
assert.ok(r.warnings.some((w) => w.includes("PLAN_AGENT_PYTHON=python3")));
results.old_env_python3_ignored = true;

// value with quotes (cmd: set PLAN_PYTHON_EXECUTABLE="C:\...\python.exe") and spaces around it
r = win([VENV_WIN], { env: { PLAN_PYTHON_EXECUTABLE: `  "${VENV_WIN}"  ` } });
assert.deepStrictEqual([r.ok, r.executable], [true, VENV_WIN]);
results.quotes_stripped = true;

// a "\n" that dotenv turned into a line break: clear diagnostic instead of a strange path
r = win([VENV_WIN], { env: { PLAN_PYTHON_EXECUTABLE: "C:\\Users\\user\new\\python.exe" } });
assert.strictEqual(r.code, "python_bad_value");
results.control_chars_reported = true;

// a bare name is resolved on PATH to a full path, never to the Store alias
r = win([`${STORE}\\python.exe`, "C:\\Python313\\python.exe"],
  { env: { PLAN_PYTHON_EXECUTABLE: "python", PATH: `${STORE};C:\\Python313` } });
assert.deepStrictEqual([r.ok, r.executable], [true, "C:\\Python313\\python.exe"]);
r = win([`${STORE}\\python.exe`], { env: { PLAN_PYTHON_EXECUTABLE: "python", PATH: STORE } });
assert.strictEqual(r.code, "python_not_runnable");
results.bare_name_resolved_without_alias = true;

// .env: read from the project folder, backslashes literal, existing variables win, BOM tolerated
const fs = require("fs");
const os = require("os");
const path = require("path");
const { loadEnv } = require("../../server/env");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "shturman-env-"));
fs.writeFileSync(path.join(root, ".env"), "\uFEFF" + [
  "PLAN_PYTHON_EXECUTABLE=C:\\Users\\user\\Projekt_managment\\.venv\\Scripts\\python.exe",
  'Q_DOUBLE="C:\\Users\\user\\new\\python.exe"',
  "Q_SINGLE='C:\\Users\\user\\new\\python.exe'",
  "PLAN_SOURCE_PATH=\\\\s017\\Отделы\\Конструкторский\\1. Планы и отчеты\\AfM СIS+OEM 2026.xlsx",
  "KEEP=from-file"].join("\r\n"), "utf-8");
const target = { KEEP: "from-shell" };
const cwd = process.cwd();
process.chdir(os.tmpdir());                       // loading does not depend on the current directory
const res = loadEnv(root, target);
process.chdir(cwd);
assert.strictEqual(target.PLAN_PYTHON_EXECUTABLE, "C:\\Users\\user\\Projekt_managment\\.venv\\Scripts\\python.exe");
assert.strictEqual(target.Q_DOUBLE, "C:\\Users\\user\\new\\python.exe");
assert.strictEqual(target.Q_SINGLE, "C:\\Users\\user\\new\\python.exe");
assert.strictEqual(target.PLAN_SOURCE_PATH, "\\\\s017\\Отделы\\Конструкторский\\1. Планы и отчеты\\AfM СIS+OEM 2026.xlsx");
assert.strictEqual(target.KEEP, "from-shell");
assert.strictEqual(res.file, path.join(root, ".env"));
results.env_file_literal_backslashes = true;

console.log(JSON.stringify(results));
