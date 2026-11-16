"use strict";

// Which Python runs the planning agent (python -m plan_agent).
//
// Resolution order:
//   1. PLAN_PYTHON_EXECUTABLE - used exactly as given; if it does not exist the
//      agent is NOT started and a diagnostic names the path (no silent fallback).
//      (PLAN_AGENT_PYTHON from earlier versions is accepted as a legacy name.)
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

/**
 * @returns {{ok: boolean, executable?: string, source?: string, error?: string, code?: string, attempted: string[]}}
 */
function resolvePython({ env = process.env, platform = process.platform, root = REPO_ROOT,
  exists = isFile, verify = defaultVerify } = {}) {
  const P = platform === "win32" ? path.win32 : path.posix;
  const attempted = [];

  const configured = (env.PLAN_PYTHON_EXECUTABLE || "").trim() || (env.PLAN_AGENT_PYTHON || "").trim();
  if (configured) {
    const name = env.PLAN_PYTHON_EXECUTABLE ? "PLAN_PYTHON_EXECUTABLE" : "PLAN_AGENT_PYTHON";
    const looksLikePath = /[\\/]/.test(configured);
    attempted.push(configured);
    if (looksLikePath) {
      if (!exists(configured)) {
        return { ok: false, code: "python_not_found", attempted, source: name,
          error: `${name}=${configured}: файл не найден — укажите полный путь к python.exe виртуального окружения` };
      }
      if (platform === "win32" && STORE_ALIAS_DIR.test(configured)) {
        return { ok: false, code: "python_store_alias", attempted, source: name,
          error: `${name}=${configured}: это псевдоним Microsoft Store, а не интерпретатор Python` };
      }
      return { ok: true, executable: configured, source: name, attempted };
    }
    // a bare command name (e.g. "python3" on Linux) - accepted only if it really runs
    const real = verify(configured);
    if (!real) {
      return { ok: false, code: "python_not_runnable", attempted, source: name,
        error: `${name}=${configured}: команда не запускает Python — укажите полный путь к интерпретатору` };
    }
    return { ok: true, executable: configured, source: name, attempted };
  }

  const venv = platform === "win32" ? P.join(root, ".venv", "Scripts", "python.exe") : P.join(root, ".venv", "bin", "python");
  attempted.push(venv);
  if (exists(venv)) return { ok: true, executable: venv, source: "project .venv", attempted };

  const names = platform === "win32" ? [["py.exe", ["-3"]], ["python.exe", []], ["python3.exe", []]]
    : [["python3", []], ["python", []]];
  const dirs = String(env.PATH || env.Path || "").split(platform === "win32" ? ";" : ":").filter(Boolean);
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
        return { ok: true, executable: real, source: `system (${exe})`, attempted };
      }
    }
  }
  return { ok: false, code: "python_not_found", attempted,
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

module.exports = { resolvePython, getPython, STORE_ALIAS_DIR };
