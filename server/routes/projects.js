"use strict";

const fs = require("fs");
const path = require("path");
const express = require("express");
const multer = require("multer");
const { v4: uuidv4 } = require("uuid");
const storage = require("../storage");
const projectsModel = require("../projectsModel");
const store = require("../store");
const historyLabels = require("../historyLabels");

function stageLabelMap() {
  const map = {};
  store.read((s) => s.stages).forEach((s) => { map[s.key] = s.label; });
  return map;
}

// Критическое требование (роль ko_viewer) + requirement "служебный JSON — только администратору":
// the raw `history`/`activity` arrays (and each activity's own raw `details` object) carry the
// same plan-import diagnostic metadata (delivery_chance, probability_note, sales_channel,
// mapping_rule, unmapped_columns, actual_hours_source, ...) the readable journal
// (history_events) deliberately leaves out - removing them from a non-administrator's response
// closes the API itself, not just the UI, as the actual boundary. Only ever touches the response
// object; server/projectsModel.js's own stored data is never read differently or changed by this.
function redactTechnicalForRole(project, isAdmin) {
  if (isAdmin) return project;
  delete project.history;
  delete project.activity;
  if (Array.isArray(project.activities)) {
    project.activities = project.activities.map((a) => {
      const clean = Object.assign({}, a);
      delete clean.details;
      return clean;
    });
  }
  return project;
}

const router = express.Router();
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: storage.MAX_UPLOAD_BYTES } });

// Manual edits are attributed to the logged-in administrator (req.authUser, set by
// dashboardAuth.requireDashboardSession) when one is configured, falling back to the actor name
// the dashboard itself sends - unchanged behaviour for the no-accounts-configured local install.
const actorOf = (req) => (req.authUser && req.authUser.username) || (req.body && req.body.actor) ||
  req.get("x-dashboard-user") || "dashboard_user";

// Критическое требование (роль ko_viewer): GET (viewing the board/cards) stays open to both the
// administrator and a manager; every mutation below is administrator-only. One middleware here
// rather than per-handler, so a future route on this router is covered automatically.
router.use((req, res, next) => {
  if (req.method === "GET" || req.method === "HEAD") return next();
  if (req.authRole && req.authRole !== "admin") return res.status(403).json({ error: "Forbidden: read-only access" });
  next();
});

// GET /api/projects — list for the dashboard. Optional filters mirror the board's filter bar.
router.get("/", (req, res) => {
  let list = projectsModel.listProjects({ category: req.query.category || null });
  const { make, model, product, engineer, priority, status, q } = req.query;
  if (make) list = list.filter((p) => p.manufacturer === make);
  if (model) list = list.filter((p) => p.vehicle_model === model);
  if (product) list = list.filter((p) => p.product_type === product);
  if (engineer) list = list.filter((p) => p.assigned_engineer === engineer);
  if (priority) list = list.filter((p) => p.priority === priority);
  if (status) list = list.filter((p) => p.status === status);
  if (q) {
    const needle = String(q).toLowerCase();
    list = list.filter((p) =>
      [p.manufacturer, p.vehicle_model, p.product_type, p.project_id, p.article_number].join(" ").toLowerCase().includes(needle));
  }
  const isAdmin = req.authRole === "admin";
  res.json(list.map((p) => redactTechnicalForRole(p, isAdmin)));
});

router.get("/:id", (req, res) => {
  const project = projectsModel.getProject(req.params.id);
  if (!project) return res.status(404).json({ error: "Not found" });
  const isAdmin = req.authRole === "admin";
  // Критическое требование (роль ko_viewer): the readable journal (Russian labels, grouped
  // events) is available to both roles - only the raw technical fields/rows behind it are
  // admin-only, decided here server-side (req.authRole), never left to the client to hide.
  project.history_events = historyLabels.presentHistoryEvents(project.history,
    { admin: isAdmin, stageLabels: stageLabelMap(), activities: project.activities || [] });
  res.json(redactTechnicalForRole(project, isAdmin));
});

// GET /api/projects/:id/files/:fileId/view — streams a project photo/document inline (not as a
// download). Критическое требование (роль ko_viewer): "отдавать только необходимое изображение,
// без доступа к скачиванию оригинального файла" - no Content-Disposition:attachment, no original
// filename on the response, and this is the one URL that works for a ko_viewer manager at all
// (the raw /uploads static mount 403s them - see server/index.js). Available to the administrator
// too, so the dashboard can use one URL scheme for both roles (projectsModel.js's fileToApi()
// already returns this as `view_url` alongside the raw `url`). This does not, and per the ТЗ's own
// "технически невозможно" note cannot, stop a browser's own save-as/screenshot - it only removes
// the direct, guessable original-file link and any download-oriented header.
router.get("/:id/files/:fileId/view", (req, res) => {
  const row = store.read((s) => s.project_files.find((f) => f.id === req.params.fileId && f.project_id === req.params.id));
  if (!row) return res.status(404).json({ error: "Not found" });
  const absolutePath = path.join(storage.UPLOADS_DIR, row.storage_key);
  if (!fs.existsSync(absolutePath)) return res.status(404).json({ error: "Not found" });
  res.setHeader("Content-Type", row.mime_type || "application/octet-stream");
  res.setHeader("Content-Disposition", "inline");
  res.setHeader("Cache-Control", "private, max-age=0, no-store");
  fs.createReadStream(absolutePath).pipe(res);
});

router.get("/:id/history", (req, res) => {
  const project = projectsModel.getProject(req.params.id);
  if (!project) return res.status(404).json({ error: "Not found" });
  res.json(project.history);
});

// PATCH /api/projects/:id — manual edit of the editable fields (needs the version you read).
router.patch("/:id", (req, res) => {
  const body = req.body || {};
  const changes = body.changes || Object.fromEntries(Object.entries(body).filter(([k]) => !["version", "actor"].includes(k)));
  const v = projectsModel.validateProjectInput(changes, { partial: true, allowed: projectsModel.MANUAL_EDITABLE });
  if (v.errors.length) return res.status(400).json({ error: "Validation failed", details: v.errors });
  if (body.version == null) return res.status(400).json({ error: '"version" is required (optimistic concurrency)' });
  const r = projectsModel.safeUpdate(req.params.id, changes, { source: "manual", actor: actorOf(req), expectedVersion: body.version });
  if (r.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (r.error === "version_conflict") {
    return res.status(409).json({ error: "Проект был изменён с момента загрузки — обновите страницу", current: r.current });
  }
  if (r.error) return res.status(400).json({ error: r.error, details: r.details });
  res.json(r.project);
});

// POST /api/projects/:id/stage — a person confirms/moves the project to a stage (drag-and-drop or the details panel).
router.post("/:id/stage", (req, res) => {
  const b = req.body || {};
  if (b.stage === undefined) return res.status(400).json({ error: '"stage" is required (a stage key, or null to un-confirm)' });
  const r = projectsModel.moveStage(req.params.id, b.stage, { source: "manual", actor: actorOf(req),
    expectedVersion: b.version != null ? b.version : null, note: b.note || null });
  if (r.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (r.error === "version_conflict") return res.status(409).json({ error: "Проект был изменён — обновите страницу" });
  if (r.error) return res.status(400).json({ error: r.error, details: r.details });
  res.json(Object.assign({ unchanged: !!r.unchanged }, r.project));
});

// POST /api/projects/:id/comments — append an engineering comment.
router.post("/:id/comments", (req, res) => {
  const text = req.body && typeof req.body.text === "string" ? req.body.text.trim() : "";
  if (!text) return res.status(400).json({ error: '"text" is required' });
  const project = projectsModel.getProject(req.params.id);
  if (!project) return res.status(404).json({ error: "Not found" });
  const comment = { id: uuidv4(), text, source: "manual", author: actorOf(req), at: new Date().toISOString() };
  const r = projectsModel.safeUpdate(project.id, { comments: (project.comments || []).concat([comment]) },
    { source: "manual", actor: actorOf(req) });
  if (r.error) return res.status(409).json({ error: "Проект был изменён — обновите страницу" });
  res.status(201).json(comment);
});

// POST /api/projects/:id/files — manual photo/attachment upload from the dashboard.
router.post("/:id/files", upload.single("file"), (req, res) => {
  const row = projectsModel.getProjectRow(req.params.id);
  if (!row) return res.status(404).json({ error: "Not found" });
  const check = storage.validateFile(req.file);
  if (!check.ok) return res.status(400).json({ error: check.error });
  const { storageKey } = storage.saveProjectFile(row.id, req.file);
  const fileId = uuidv4();
  const kind = req.file.mimetype === "application/pdf" ? "attachment" : "image";
  const actor = req.get("x-dashboard-user") || "dashboard_user";
  store.tx((st) => {
    st.project_files.push({ id: fileId, project_id: row.id, kind, storage_key: storageKey,
      original_filename: req.file.originalname || null, mime_type: req.file.mimetype, size: req.file.size, is_primary: false,
      caption: req.body.caption || null, source: "manual", telegram_message_id: null, telegram_chat_id: null,
      uploaded_by: actor, uploaded_at: new Date().toISOString(), technical_comment: req.body.technical_comment || null,
      event_at: null, telegram_ref: null });
    projectsModel.pushHistory(st, row.id, "file_uploaded", "manual", actor, null, { fileId, kind, caption: req.body.caption || null });
  });
  const p = projectsModel.getProject(row.id);
  res.status(201).json(p.images.concat(p.attachments).find((f) => f.id === fileId));
});

// PATCH /api/projects/:id/activities/:aid — manual progress / note / assignee on an activity.
router.patch("/:id/activities/:aid", (req, res) => {
  const body = req.body || {};
  const changes = body.changes || Object.fromEntries(Object.entries(body).filter(([k]) => k !== "actor"));
  const r = projectsModel.updateActivityManual(req.params.id, req.params.aid, changes, actorOf(req));
  if (r.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (r.error) return res.status(400).json({ error: r.error, details: r.details });
  res.json(r.activity);
});

module.exports = router;
