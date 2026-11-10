"use strict";

// Manual planning-file import: upload -> dry run -> explicit approval -> sync.
//
// The Excel processing itself is done by the planning agent (Python package
// `plan_agent` in this repository). The server stores the uploaded file outside
// the public /uploads directory, runs the agent as a child process and lets the
// agent write through this server's own agent API - so manual uploads and a
// future automatic file-delivery agent use exactly the same pipeline.

const express = require("express");
const multer = require("multer");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");
const { v4: uuidv4 } = require("uuid");
const store = require("../store");

const router = express.Router();
const REPO_ROOT = path.join(__dirname, "..", "..");
const IMPORT_DIR = path.resolve(process.env.PLAN_IMPORT_DIR || "./data/plan-imports");
const STATE_DIR = path.join(IMPORT_DIR, "state");
const FILES_DIR = path.join(IMPORT_DIR, "files");
const PYTHON = process.env.PLAN_AGENT_PYTHON || "python3";
const MAX_BYTES = Number(process.env.MAX_PLAN_UPLOAD_BYTES || 50 * 1024 * 1024);
const RUN_TIMEOUT_MS = Number(process.env.PLAN_AGENT_TIMEOUT_MS || 10 * 60 * 1000);
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: MAX_BYTES } });

// One agent run at a time (manual uploads and the file-source monitor share it),
// so imports are applied in order and never overlap.
const jobs = { busy: false, owner: null, since: null };
function tryLock(owner) {
  if (jobs.busy) return false;
  Object.assign(jobs, { busy: true, owner, since: new Date().toISOString() });
  return true;
}
function unlock() {
  Object.assign(jobs, { busy: false, owner: null, since: null });
}
function nowIso() {
  return new Date().toISOString();
}

function rowToApi(r) {
  if (!r) return null;
  const copy = JSON.parse(JSON.stringify(r));
  delete copy.stored_path;          // never expose server paths
  return copy;
}
function getImport(id) {
  return store.read((s) => {
    const r = s.plan_imports.find((x) => x.id === id);
    return r ? JSON.parse(JSON.stringify(r)) : null;
  });
}
function updateImport(id, fields) {
  store.tx((s) => Object.assign(s.plan_imports.find((x) => x.id === id), fields));
}

function runAgent(args) {
  return new Promise((resolve) => {
    const port = process.env.PORT || 3000;
    const env = Object.assign({}, process.env, {
      PM_API_URL: `http://127.0.0.1:${port}`,
      PM_AGENT_API_KEY: process.env.AGENT_API_KEY || "",
      PLAN_AGENT_STATE_DIR: STATE_DIR,
      // The agent talks to this very server; deployment verification of a
      // remote production host does not apply to the in-app import.
      PLAN_AGENT_REQUIRE_VERIFIED_DEPLOYMENT: "0",
      PYTHONIOENCODING: "utf-8"
    });
    const child = spawn(PYTHON, ["-m", "plan_agent"].concat(args), { cwd: REPO_ROOT, env });
    let out = "";
    let err = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), RUN_TIMEOUT_MS);
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { err += d; });
    child.on("error", (e) => { clearTimeout(timer); resolve({ code: -1, out, err: String(e), json: null }); });
    child.on("close", (code) => {
      clearTimeout(timer);
      const line = out.trim().split("\n").reverse().find((l) => l.startsWith("{"));
      let json = null;
      try { json = line ? JSON.parse(line) : null; } catch (e) { json = null; }
      resolve({ code, out, err, json });
    });
  });
}

function readReport(json) {
  const out = {};
  try {
    if (json && json.report_json_path) out.report = JSON.parse(fs.readFileSync(json.report_json_path, "utf-8"));
    if (json && json.analysis_path && fs.existsSync(json.analysis_path)) out.analysis_md = fs.readFileSync(json.analysis_path, "utf-8");
    if (json && json.report_path && fs.existsSync(json.report_path)) out.report_text = fs.readFileSync(json.report_path, "utf-8");
  } catch (e) {
    out.read_error = String(e);
  }
  return out;
}

function guard(res) {
  if (!process.env.AGENT_API_KEY) {
    res.status(501).json({ error: "AGENT_API_KEY не задан на сервере — импорт плана недоступен (см. .env.example)" });
    return false;
  }
  if (jobs.busy) {
    res.status(409).json({ error: "Идёт обработка другого файла — повторите через минуту" });
    return false;
  }
  return true;
}

// GET /api/plan-imports — recent imports
router.get("/", (req, res) => {
  res.json(store.read((s) => s.plan_imports.slice().sort((a, b) => String(b.uploaded_at).localeCompare(String(a.uploaded_at)))
    .slice(0, 50).map(rowToApi)));
});

router.get("/:id", (req, res) => {
  const r = getImport(req.params.id);
  if (!r) return res.status(404).json({ error: "Not found" });
  const api = rowToApi(r);
  const s = api.summary || {};
  Object.assign(api, readReport(s.dry_run || null));
  if (s.sync) api.sync_report = readReport(s.sync).report || null;
  res.json(api);
});

// ---- core steps (also used by server/planSource) ----

/** Register an already stored file as a new import (status "analyzing"). */
function createImport({ storedPath, name, sha256, size, month, origin, sourceMeta }) {
  const id = uuidv4();
  store.tx((s) => s.plan_imports.push({ id, original_name: name, sha256, stored_path: storedPath, size,
    plan_month: month, status: "analyzing", origin: origin || "upload", source: sourceMeta || null, fingerprint: null,
    dry_run_id: null, sync_run_id: null, summary: null, error: null, uploaded_at: nowIso(), approved_at: null,
    approved_by: null }));
  return id;
}
function newStoredPath(id) {
  fs.mkdirSync(FILES_DIR, { recursive: true });
  return path.join(FILES_DIR, `${id}.upload`);
}

/** Dry run of a registered import. Caller holds the job lock. */
async function analyzeImport(id) {
  const imp = getImport(id);
  const r = await runAgent(["analyze", imp.stored_path, "--month", imp.plan_month, "--name", imp.original_name, "--json"]);
  const json = r.json;
  const ok = !!(json && json.status && json.status !== "Failed" && json.mode !== "none");
  const summary = { dry_run: json, stderr: r.code !== 0 && !json ? r.err.slice(-4000) : undefined };
  updateImport(id, { status: ok ? "analyzed" : "failed", fingerprint: json ? json.fingerprint || null : null,
    dry_run_id: json ? json.run_id : null, summary,
    error: ok ? null : ((json && json.fatal_error) || r.err.slice(-2000) || "analysis failed") });
  return { ok, json, row: getImport(id) };
}

/** Approve the mapping of this analysis (fingerprint-bound) and synchronise.
 *  autoSafe: unattended mode, only safe changes (see plan_agent sync --auto-safe).
 *  Caller holds the job lock. */
async function approveAndSync(imp, by, { autoSafe = false } = {}) {
  if (!autoSafe) {
    const appr = await runAgent(["approve", "--by", by, "--fingerprint", imp.fingerprint || "", "--json"]);
    if (!appr.json || !appr.json.approved) {
      const error = (appr.json && appr.json.error) || appr.err.slice(-2000);
      updateImport(imp.id, { error });
      return { ok: false, code: 409, error: "Не удалось утвердить сопоставление", details: appr.json || error };
    }
    imp.summary = Object.assign({}, imp.summary || {}, { approval: appr.json });
  }
  updateImport(imp.id, { status: "syncing" });
  const args = ["sync", imp.stored_path, "--month", imp.plan_month, "--name", imp.original_name, "--json"];
  if (autoSafe) args.push("--auto-safe");
  const r = await runAgent(args);
  const json = r.json;
  const synced = !!(json && /^Completed/.test(json.status || ""));
  const complete = !!(json && json.status === "Completed");   // every project written without errors
  const summary = Object.assign({}, imp.summary || {}, { sync: json });
  updateImport(imp.id, { status: synced ? "synced" : "sync_failed", sync_run_id: json ? json.run_id : null, summary,
    error: synced ? null : ((json && (json.fatal_error || json.status)) || r.err.slice(-2000) || "sync failed"),
    approved_at: nowIso(), approved_by: by });
  return { ok: synced, complete, code: synced ? 200 : 422, json, row: getImport(imp.id) };
}

// A restart while the agent was running: the run's own writes are per-project
// transactions, so the data is consistent, but the import did not finish.
(function recoverInterrupted() {
  store.tx((s) => s.plan_imports.forEach((i) => {
    if (i.status === "analyzing") Object.assign(i, { status: "failed", error: "прервано перезапуском сервера" });
    if (i.status === "syncing") Object.assign(i, { status: "interrupted", error: "синхронизация прервана перезапуском сервера — её можно повторить" });
  }));
})();

// POST /api/plan-imports — multipart: file, month (YYYY-MM). Runs the dry run only.
router.post("/", upload.single("file"), async (req, res) => {
  if (!guard(res)) return;
  if (!req.file || !req.file.size) return res.status(400).json({ error: "Файл не выбран или пуст" });
  const month = (req.body && req.body.month) || "";
  if (!/^\d{4}-\d{2}$/.test(month)) return res.status(400).json({ error: "Укажите отчётный месяц в формате ГГГГ-ММ" });
  tryLock("upload");
  try {
    const sha = crypto.createHash("sha256").update(req.file.buffer).digest("hex");
    const tmpId = uuidv4();
    const stored = newStoredPath(tmpId);
    fs.writeFileSync(stored, req.file.buffer);
    const name = path.basename(req.file.originalname || "plan.xlsx");
    const id = createImport({ storedPath: stored, name, sha256: sha, size: req.file.size, month, origin: "upload" });
    const r = await analyzeImport(id);
    res.status(r.ok ? 201 : 422).json(Object.assign(rowToApi(r.row), readReport(r.json)));
  } finally {
    unlock();
  }
});

// POST /api/plan-imports/:id/approve — {confirm: true, approved_by}. Approves the mapping of THIS
// analysis (fingerprint-bound) and runs the synchronisation.
router.post("/:id/approve", async (req, res) => {
  if (!guard(res)) return;
  const imp = getImport(req.params.id);
  if (!imp) return res.status(404).json({ error: "Not found" });
  if (!req.body || req.body.confirm !== true) {
    return res.status(400).json({ error: "Требуется явное подтверждение: {\"confirm\": true}" });
  }
  if (!["analyzed", "interrupted", "sync_failed"].includes(imp.status)) {
    return res.status(409).json({ error: `Импорт в статусе «${imp.status}» — синхронизация невозможна` });
  }
  if (!fs.existsSync(imp.stored_path)) return res.status(410).json({ error: "Загруженный файл больше не доступен" });
  const by = String(req.body.approved_by || "dashboard_user").slice(0, 80);
  tryLock("approve");
  try {
    const r = await approveAndSync(imp, by);
    if (!r.row) return res.status(r.code).json({ error: r.error, details: r.details });
    res.status(r.code).json(Object.assign(rowToApi(r.row), { sync_report: readReport(r.json).report || null }));
  } finally {
    unlock();
  }
});

module.exports = router;
module.exports.IMPORT_DIR = IMPORT_DIR;
module.exports.FILES_DIR = FILES_DIR;
module.exports.jobs = jobs;
module.exports.tryLock = tryLock;
module.exports.unlock = unlock;
module.exports.getImport = getImport;
module.exports.rowToApi = rowToApi;
module.exports.readReport = readReport;
module.exports.createImport = createImport;
module.exports.newStoredPath = newStoredPath;
module.exports.analyzeImport = analyzeImport;
module.exports.approveAndSync = approveAndSync;
