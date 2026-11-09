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
const db = require("../db");

const router = express.Router();
const REPO_ROOT = path.join(__dirname, "..", "..");
const IMPORT_DIR = path.resolve(process.env.PLAN_IMPORT_DIR || "./data/plan-imports");
const STATE_DIR = path.join(IMPORT_DIR, "state");
const FILES_DIR = path.join(IMPORT_DIR, "files");
const PYTHON = process.env.PLAN_AGENT_PYTHON || "python3";
const MAX_BYTES = Number(process.env.MAX_PLAN_UPLOAD_BYTES || 50 * 1024 * 1024);
const RUN_TIMEOUT_MS = Number(process.env.PLAN_AGENT_TIMEOUT_MS || 10 * 60 * 1000);
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: MAX_BYTES } });

let busy = false;   // one agent run at a time keeps imports ordered

function nowIso() {
  return new Date().toISOString();
}

function rowToApi(r) {
  return Object.assign({}, r, { summary: r.summary ? JSON.parse(r.summary) : null, stored_path: undefined });
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
  if (busy) {
    res.status(409).json({ error: "Идёт обработка другого файла — повторите через минуту" });
    return false;
  }
  return true;
}

// GET /api/plan-imports — recent imports
router.get("/", (req, res) => {
  res.json(db.prepare("SELECT * FROM plan_imports ORDER BY uploaded_at DESC LIMIT 50").all().map(rowToApi));
});

router.get("/:id", (req, res) => {
  const r = db.prepare("SELECT * FROM plan_imports WHERE id = ?").get(req.params.id);
  if (!r) return res.status(404).json({ error: "Not found" });
  const api = rowToApi(r);
  const s = api.summary || {};
  Object.assign(api, readReport(s.dry_run || null));
  if (s.sync) api.sync_report = readReport(s.sync).report || null;
  res.json(api);
});

// POST /api/plan-imports — multipart: file, month (YYYY-MM). Runs the dry run only.
router.post("/", upload.single("file"), async (req, res) => {
  if (!guard(res)) return;
  if (!req.file || !req.file.size) return res.status(400).json({ error: "Файл не выбран или пуст" });
  const month = (req.body && req.body.month) || "";
  if (!/^\d{4}-\d{2}$/.test(month)) return res.status(400).json({ error: "Укажите отчётный месяц в формате ГГГГ-ММ" });
  const id = uuidv4();
  const sha = crypto.createHash("sha256").update(req.file.buffer).digest("hex");
  fs.mkdirSync(FILES_DIR, { recursive: true });
  const stored = path.join(FILES_DIR, `${id}.upload`);
  fs.writeFileSync(stored, req.file.buffer);
  const name = path.basename(req.file.originalname || "plan.xlsx");
  db.prepare(`INSERT INTO plan_imports (id, original_name, sha256, stored_path, size, plan_month, status, uploaded_at)
              VALUES (?, ?, ?, ?, ?, ?, 'analyzing', ?)`).run(id, name, sha, stored, req.file.size, month, nowIso());
  busy = true;
  try {
    const r = await runAgent(["analyze", stored, "--month", month, "--name", name, "--json"]);
    const json = r.json;
    const ok = json && json.status && json.status !== "Failed";
    const summary = { dry_run: json, stderr: r.code !== 0 && !json ? r.err.slice(-4000) : undefined };
    db.prepare("UPDATE plan_imports SET status = ?, fingerprint = ?, dry_run_id = ?, summary = ?, error = ? WHERE id = ?")
      .run(ok ? "analyzed" : "failed", json ? json.fingerprint || null : null, json ? json.run_id : null,
        JSON.stringify(summary), ok ? null : ((json && json.fatal_error) || r.err.slice(-2000) || "analysis failed"), id);
    const row = rowToApi(db.prepare("SELECT * FROM plan_imports WHERE id = ?").get(id));
    res.status(ok ? 201 : 422).json(Object.assign(row, readReport(json)));
  } finally {
    busy = false;
  }
});

// POST /api/plan-imports/:id/approve — {confirm: true, approved_by}. Approves the mapping of THIS
// analysis (fingerprint-bound) and runs the synchronisation.
router.post("/:id/approve", async (req, res) => {
  if (!guard(res)) return;
  const imp = db.prepare("SELECT * FROM plan_imports WHERE id = ?").get(req.params.id);
  if (!imp) return res.status(404).json({ error: "Not found" });
  if (!req.body || req.body.confirm !== true) {
    return res.status(400).json({ error: "Требуется явное подтверждение: {\"confirm\": true}" });
  }
  if (imp.status !== "analyzed") return res.status(409).json({ error: `Импорт в статусе «${imp.status}» — синхронизация невозможна` });
  if (!fs.existsSync(imp.stored_path)) return res.status(410).json({ error: "Загруженный файл больше не доступен" });
  const by = String(req.body.approved_by || "dashboard_user").slice(0, 80);
  busy = true;
  try {
    const appr = await runAgent(["approve", "--by", by, "--fingerprint", imp.fingerprint || "", "--json"]);
    if (!appr.json || !appr.json.approved) {
      db.prepare("UPDATE plan_imports SET error = ? WHERE id = ?").run((appr.json && appr.json.error) || appr.err.slice(-2000), imp.id);
      return res.status(409).json({ error: "Не удалось утвердить сопоставление", details: appr.json || appr.err.slice(-2000) });
    }
    const r = await runAgent(["sync", imp.stored_path, "--month", imp.plan_month, "--name", imp.original_name, "--json"]);
    const json = r.json;
    const synced = json && /^Completed/.test(json.status || "");
    const summary = Object.assign(JSON.parse(imp.summary || "{}"), { approval: appr.json, sync: json });
    db.prepare(`UPDATE plan_imports SET status = ?, sync_run_id = ?, summary = ?, error = ?, approved_at = ?, approved_by = ?
                WHERE id = ?`).run(synced ? "synced" : "sync_failed", json ? json.run_id : null, JSON.stringify(summary),
      synced ? null : ((json && (json.fatal_error || json.status)) || r.err.slice(-2000)), nowIso(), by, imp.id);
    const row = rowToApi(db.prepare("SELECT * FROM plan_imports WHERE id = ?").get(imp.id));
    res.status(synced ? 200 : 422).json(Object.assign(row, { sync_report: readReport(json).report || null }));
  } finally {
    busy = false;
  }
});

module.exports = router;
module.exports.IMPORT_DIR = IMPORT_DIR;
