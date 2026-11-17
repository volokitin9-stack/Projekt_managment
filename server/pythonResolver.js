"use strict";

// Which Python runs the planning agent (python -m plan_agent).
//
// Resolution order:
//   1. PLAN_PYTHON_EXECUTABLE - used exactly as given; if it does not exist the
//      agent is NOT started and a diagnostic names the path (no silent fallback).
//      Surrounding quotes are removed; backslashes are literal (see server/env.js).
//      PLAN_AGENT_PYTHON (earlier versions) is honoured only with a full path:
//      its old default "python3" is ignored - on Windows that name is the
//      Microsoft Store alias, and .venv\Scripts contains no python3.exe.
//   2. project-local virtual environment: .venv\Scripts\python.exe (Windows),
//      .venv/bin/python (Linux/macOS)
//   3. a verified system interpreter: candidates are searched on PATH, the
//      Microsoft Store "App Execution Alias" folder (...\Microsoft\WindowsApps)
//      is skipped, and each candidate must run `import sys; print(sys.executable)`
//      successfully; the real executable it reports is used.
//
// The agent is always started with spawn(executable, [args...], {shell: false}),
// so paths with spaces are passed safely and nothing is concatenated into a
// shell command.

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const REPO_ROOT = path.join(__dirname, "..");
const STORE_ALIAS_DIR = /[\\/]microsoft[\\/]windowsapps([\\/]|$)/i;

function isFile(p) {
  try {
    return fs.statSync(p).isFile();
  } catch (e) {
    return false;
  }
}

function defaultVerify(exe, args) {
  const r = spawnSync(exe, (args || []).concat(["-c", "import sys; print(sys.executable)"]),
    { encoding: "utf-8", timeout: 15000, windowsHide: true, shell: false });
  if (r.error || r.status !== 0) return null;
  const out = String(r.stdout || "").trim().split(/\r?\n/).pop();
  return out || null;
}

/** Remove surrounding whitespace and one pair of surrounding quotes ("C:\..." or 'C:\...'). */
function cleanValue(v) {
  let x = String(v == null ? "" : v).trim();
  if (x.length >= 2 && ((x[0] === '"' && x.endsWith('"')) || (x[0] === "'" && x.endsWith("'")))) x = x.slice(1, -1).trim();
  return x;
}

/** Find a bare command (e.g. "python") on PATH, skipping the Microsoft Store alias folder. */
function findOnPath(name, { env, platform, exists }) {
  const P = platform === "win32" ? path.win32 : path.posix;
  const dirs = String(env.PATH || env.Path || "").split(platform === "win32" ? ";" : ":").map(cleanValue).filter(Boolean);
  const names = platform === "win32" && !/\.exe$/i.test(name) ? [`${name}.exe`, name] : [name];
  for (const d of dirs) {
    if (platform === "win32" && STORE_ALIAS_DIR.test(d)) continue;
    for (const n of names) {
      const cand = P.join(d, n);
      if (exists(cand)) return cand;
    }
  }
  return null;
}

/**
 * @returns {{ok: boolean, executable?: string, source?: string, error?: string, code?: string,
 *            attempted: string[], warnings: string[]}}
 */
function resolvePython({ env = process.env, platform = process.platform, root = REPO_ROOT,
  exists = isFile, verify = defaultVerify } = {}) {
  const P = platform === "win32" ? path.win32 : path.posix;
  const attempted = [];
  const warnings = [];
  const ctx = { env, platform, exists };

  // A full path given in an older variable is honoured; a bare "python3"/"python" there is the
  // old default copied from .env.example and is ignored (on Windows it is the Store alias).
  let configured = cleanValue(env.PLAN_PYTHON_EXECUTABLE);
  let name = "PLAN_PYTHON_EXECUTABLE";
  if (!configured) {
    const legacy = cleanValue(env.PLAN_AGENT_PYTHON);
    if (legacy && /[\\/]/.test(legacy)) {
      configured = legacy;
      name = "PLAN_AGENT_PYTHON";
      warnings.push("PLAN_AGENT_PYTHON устарела — переименуйте её в PLAN_PYTHON_EXECUTABLE");
    } else if (legacy) {
      warnings.push(`PLAN_AGENT_PYTHON=${legacy} (устаревшая настройка из старого .env.example) не используется — ` +
        "интерпретатор выбирается автоматически; удалите эту строку из .env");
    }
  }
  if (configured) {
    attempted.push(configured);
    if (/[\r\n\t\0]/.test(configured)) {
      return { ok: false, code: "python_bad_value", attempted, warnings, source: name,
        error: `${name} содержит управляющие символы — в .env указан путь в двойных кавычках с последовательностью вроде \\n? ` +
          "Запишите путь без кавычек: PLAN_PYTHON_EXECUTABLE=C:\\...\\.venv\\Scripts\\python.exe" };
    }
    if (/[\\/]/.test(configured)) {
      if (!exists(configured)) {
        return { ok: false, code: "python_not_found", attempted, warnings, source: name,
          error: `${name}=${configured}: файл не найден — укажите полный путь к python.exe виртуального окружения` };
      }
      if (platform === "win32" && STORE_ALIAS_DIR.test(configured)) {
        return { ok: false, code: "python_store_alias", attempted, warnings, source: name,
          error: `${name}=${configured}: это псевдоним Microsoft Store, а не интерпретатор Python` };
      }
      return { ok: true, executable: configured, source: name, attempted, warnings };
    }
    // A bare command name: resolved to a full path on PATH (never the Store alias) and verified.
    const full = findOnPath(configured, ctx);
    if (full) attempted.push(full);
    const real = full ? verify(full) : null;
    if (!real) {
      return { ok: false, code: "python_not_runnable", attempted, warnings, source: name,
        error: `${name}=${configured}: команда не найдена или не запускает Python — укажите полный путь к интерпретатору` };
    }
    return { ok: true, executable: full, source: name, attempted, warnings };
  }

  const venv = platform === "win32" ? P.join(root, ".venv", "Scripts", "python.exe") : P.join(root, ".venv", "bin", "python");
  attempted.push(venv);
  if (exists(venv)) return { ok: true, executable: venv, source: "project .venv", attempted, warnings };

  const names = platform === "win32" ? [["py.exe", ["-3"]], ["python.exe", []], ["python3.exe", []]]
    : [["python3", []], ["python", []]];
  const dirs = String(env.PATH || env.Path || "").split(platform === "win32" ? ";" : ":").map(cleanValue).filter(Boolean);
  if (platform === "win32") {
    const win = env.SystemRoot || env.windir || "C:\\Windows";
    dirs.push(win);                                   // py.exe launcher lives in C:\Windows
  }
  for (const [exe, args] of names) {
    for (const d of dirs) {
      const cand = P.join(d, exe);
      if (platform === "win32" && STORE_ALIAS_DIR.test(d)) continue;   // never the Store alias
      if (!exists(cand)) continue;
      attempted.push(cand);
      const real = verify(cand, args);
      if (real && !(platform === "win32" && STORE_ALIAS_DIR.test(real))) {
        return { ok: true, executable: real, source: `system (${exe})`, attempted, warnings };
      }
    }
  }
  return { ok: false, code: "python_not_found", attempted, warnings,
    error: "Python для агента планирования не найден: задайте PLAN_PYTHON_EXECUTABLE (полный путь к python.exe) " +
      "или создайте виртуальное окружение .venv в папке проекта" };
}

let cache = null;
/** Resolved once per configuration; call with refresh=true to re-check. */
function getPython(refresh = false) {
  const key = `${process.env.PLAN_PYTHON_EXECUTABLE || ""}|${process.env.PLAN_AGENT_PYTHON || ""}`;
  if (!cache || cache.key !== key || refresh || !cache.result.ok) cache = { key, result: resolvePython() };
  return cache.result;
}

module.exports = { resolvePython, getPython, cleanValue, STORE_ALIAS_DIR, REPO_ROOT };
