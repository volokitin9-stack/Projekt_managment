"use strict";

// Agent-facing API (Authorization: Bearer <AGENT_API_KEY>).
//
// Two request styles are supported on the project endpoints:
// * legacy flat body (the original Штурман agent contract, still valid):
//     POST  /api/agent/projects       { external_project_id, manufacturer, ... }   -> upsert
//     PATCH /api/agent/projects/:id   { priority: "high", status: "prototype" }
// * envelope used by the planning agent:
//     POST  /api/agent/projects       { project: {...}, activities: [...], activity_scope: {plan_month},
//                                       sync: {run_id, source_file}, create_only: true }
//     PATCH /api/agent/projects/:id   { changes: {...}, expected_version: n, activities: [...],
//                                       activity_scope: {...}, sync: {...} }
// Unknown fields are rejected (400), never silently dropped.

const express = require("express");
const multer = require("multer");
const { v4: uuidv4 } = require("uuid");
const store = require("../store");
const storage = require("../storage");
const projectsModel = require("../projectsModel");
const reviewQueue = require("../reviewQueueModel");
const { requireAgentAuth } = require("../auth");

const router = express.Router();
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: storage.MAX_UPLOAD_BYTES } });

router.use(requireAgentAuth);

function nowIso() {
  return new Date().toISOString();
}
const actorOf = (req) => req.get("x-agent-id") || "agent";

function validationError(res, errors, unknown) {
  return res.status(400).json({ error: "Validation failed", code: unknown && unknown.length ? "unsupported_fields" : "invalid_value",
    details: errors, fields: unknown || [] });
}

/** Existing projects with the same identity (external id, article, or make+model+product within a category). */
function findDuplicates(data, excludeId = null) {
  const all = projectsModel.allRows();
  const ids = new Set();
  const ext = data.external_project_id;
  if (ext) all.filter((r) => r.external_project_id === ext).forEach((r) => ids.add(r.id));
  if (data.article_number) {
    const art = projectsModel.normKey(data.article_number);
    all.filter((r) => art && projectsModel.normKey((r.data || {}).article_number) === art).forEach((r) => ids.add(r.id));
  }
  if (!ids.size && !ext && !data.article_number && data.manufacturer && data.vehicle_model && data.product_type) {
    const cat = data.record_category || "main_project";
    const want = [data.manufacturer, data.vehicle_model, data.product_type].map(projectsModel.normKey).join("|");
    all.filter((r) => {
      const d = r.data || {};
      return (r.record_category || "main_project") === cat &&
        [d.manufacturer, d.vehicle_model, d.product_type].map(projectsModel.normKey).join("|") === want;
    }).forEach((r) => ids.add(r.id));
  }
  ids.delete(excludeId);
  return Array.from(ids);
}

// ---- GET /api/agent/schema - what the backend accepts (the agent never sends anything else) ----
router.get("/schema", (req, res) => {
  res.json({
    version: 2,
    app: "shturman",
    schema_version: store.schemaVersion(),
    writable_fields: projectsModel.AGENT_WRITABLE,
    activity_fields: projectsModel.ACTIVITY_AGENT_FIELDS,
    record_categories: projectsModel.CATEGORIES,
    stages: projectsModel.validStageKeys(),
    priorities: projectsModel.VALID_PRIORITIES,
    activity_types: projectsModel.ACTIVITY_TYPES,
    progress_statuses: projectsModel.PROGRESS,
    supports: ["expected_version", "duplicate_detection", "field_sources", "history", "sync_runs", "activities",
      "review_queue", "stage_transitions", "telegram_photos", "create_only"]
  });
});

// ---- GET /api/agent/projects - full project list incl. activities, versions, field sources ----
router.get("/projects", (req, res) => {
  res.json({ projects: projectsModel.listProjects({ category: req.query.category || null }) });
});

// ---- POST /api/agent/projects ----
router.post("/projects", (req, res) => {
  const body = req.body || {};
  const envelope = body.project !== undefined;
  const input = envelope ? body.project : body;
  if (!input || typeof input !== "object" || Array.isArray(input)) return validationError(res, ["project must be an object"]);
  const externalId = input.external_project_id || input.externalProjectId || null;
  const allowed = envelope ? projectsModel.AGENT_WRITABLE : projectsModel.PROJECT_FIELDS;

  if (envelope) {
    const v = projectsModel.validateProjectInput(input, { partial: false, allowed });
    if (v.errors.length) return validationError(res, v.errors, v.unknown);
    const dups = findDuplicates(Object.assign({}, input));
    if (dups.length && body.create_only !== false) {
      return res.status(409).json({ error: "duplicate", code: "duplicate", message: "a project with the same identity already exists",
        existing_ids: dups });
    }
    try {
      const project = projectsModel.createProject(input, { externalId, needsReview: !externalId, source: "agent",
        actor: actorOf(req), activities: body.activities || null, activityScope: body.activity_scope || null,
        sync: body.sync || null });
      return res.status(201).json(Object.assign({ matched: "created" }, project));
    } catch (e) {
      if (e.status === 400) return validationError(res, e.details || [e.message]);
      throw e;
    }
  }

  // legacy flat body: idempotent upsert keyed by external_project_id (original behaviour)
  if (externalId) {
    const existingRow = projectsModel.getProjectByExternalId(externalId);
    if (existingRow) {
      const v = projectsModel.validateProjectInput(input, { partial: true, allowed });
      if (v.errors.length) return validationError(res, v.errors, v.unknown);
      const result = projectsModel.safeUpdate(existingRow.id, Object.assign({}, input, { source: input.source || "agent" }),
        { source: "agent", actor: actorOf(req) });
      if (result.error === "version_conflict") {
        return res.status(409).json({ error: "Project was modified since the version you have", code: "version_conflict", current: result.current });
      }
      return res.status(200).json(Object.assign({ matched: "existing", conflicts: result.conflicts }, result.project));
    }
  }
  const v = projectsModel.validateProjectInput(input, { partial: false, allowed });
  if (v.errors.length) return validationError(res, v.errors, v.unknown);
  const needsReview = !externalId;
  const project = projectsModel.createProject(Object.assign({}, input, { source: input.source || "agent" }), {
    externalId, needsReview, source: "agent", actor: actorOf(req)
  });
  if (needsReview) {
    reviewQueue.upsert({ kind: "project", payload: input, source: "agent",
      reason: "No external_project_id supplied — future agent updates cannot be matched to this project reliably." });
  }
  res.status(201).json(Object.assign({ matched: "created" }, project));
});

// ---- PATCH /api/agent/projects/:id (internal id or external_project_id) ----
router.patch("/projects/:id", (req, res) => {
  const row = projectsModel.findRow(req.params.id);
  if (!row) return res.status(404).json({ error: "Not found" });
  const body = req.body || {};
  const envelope = body.changes !== undefined || body.activities !== undefined;
  const changes = envelope ? (body.changes || {}) : body;
  const allowed = envelope ? projectsModel.AGENT_WRITABLE : projectsModel.PROJECT_FIELDS;
  const v = projectsModel.validateProjectInput(changes, { partial: true, allowed });
  if (v.errors.length) return validationError(res, v.errors, v.unknown);
  if (envelope && (changes.external_project_id || changes.article_number)) {
    const dups = findDuplicates({ external_project_id: changes.external_project_id, article_number: changes.article_number }, row.id);
    if (dups.length) return res.status(409).json({ error: "duplicate", code: "duplicate", existing_ids: dups });
  }
  const result = projectsModel.safeUpdate(row.id, envelope ? changes : Object.assign({}, changes, { source: changes.source || "agent" }), {
    source: "agent", actor: actorOf(req),
    expectedVersion: envelope ? body.expected_version : null,
    activities: envelope ? body.activities || null : null,
    activityScope: envelope ? body.activity_scope || null : null,
    sync: envelope ? body.sync || null : null
  });
  if (result.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (result.error === "version_conflict") {
    return res.status(409).json({ error: "version_conflict", code: "version_conflict",
      message: "Project was modified since the version you have", current_version: result.current && result.current.version,
      current: result.current });
  }
  if (result.error) return validationError(res, result.details || [result.error]);
  res.json(Object.assign({}, result.project, { conflicts: result.conflicts, activity_summary: result.activity_summary }));
});

// ---- POST /api/agent/projects/:id/stage-transitions - explicit, confirmed stage change ----
router.post("/projects/:id/stage-transitions", (req, res) => {
  const b = req.body || {};
  if (b.confirmed !== true) {
    return validationError(res, ['"confirmed": true is required - a planned activity is not a confirmed stage']);
  }
  if (b.at && Number.isNaN(Date.parse(b.at))) return validationError(res, ['"at" must be an ISO timestamp']);
  const r = projectsModel.moveStage(req.params.id, b.stage === undefined ? null : b.stage, {
    source: "agent", actor: actorOf(req), expectedVersion: b.expected_version, at: b.at || null, note: b.note || null });
  if (r.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (r.error === "version_conflict") return res.status(409).json({ error: "version_conflict", code: "version_conflict" });
  if (r.error) return validationError(res, r.details || [r.error]);
  res.json(Object.assign({ unchanged: !!r.unchanged }, r.project));
});

// ---- images / attachments ---------------------------------------------------------------
function storeProjectFile(row, req, { source, uploadedBy }) {
  const telegramMessageId = req.body.telegram_message_id || null;
  const telegramChatId = req.body.telegram_chat_id || null;
  if (telegramMessageId && telegramChatId) {
    const existing = store.read((s) => s.project_files.find((f) => f.telegram_chat_id === String(telegramChatId) &&
      f.telegram_message_id === String(telegramMessageId)));
    if (existing) return { duplicate: true, id: existing.id };
  }
  if (req.body.event_at && Number.isNaN(Date.parse(req.body.event_at))) {
    const err = new Error('"event_at" must be an ISO timestamp');
    err.status = 400;
    throw err;
  }
  const { storageKey } = storage.saveProjectFile(row.id, req.file);
  const fileId = uuidv4();
  const kind = req.file.mimetype === "application/pdf" ? "attachment" : "image";
  const isPrimary = String(req.body.is_primary || "").toLowerCase() === "true";
  const caption = req.body.caption || req.body.description || null;
  store.tx((s) => {
    if (isPrimary) s.project_files.filter((f) => f.project_id === row.id).forEach((f) => { f.is_primary = false; });
    s.project_files.push({ id: fileId, project_id: row.id, kind, storage_key: storageKey,
      original_filename: req.file.originalname || null, mime_type: req.file.mimetype, size: req.file.size,
      is_primary: isPrimary, caption, source: req.body.source || source,
      telegram_message_id: telegramMessageId ? String(telegramMessageId) : null,
      telegram_chat_id: telegramChatId ? String(telegramChatId) : null, uploaded_by: uploadedBy, uploaded_at: nowIso(),
      technical_comment: req.body.technical_comment || null, event_at: req.body.event_at || null,
      telegram_ref: req.body.telegram_ref || null });
    projectsModel.pushHistory(s, row.id, "file_uploaded", source, uploadedBy, null,
      { fileId, kind, caption, telegram_message_id: telegramMessageId });
  });
  const updated = projectsModel.getProject(row.id);
  return updated.images.concat(updated.attachments).find((f) => f.id === fileId);
}

router.post("/projects/:id/images", upload.single("file"), (req, res) => {
  const row = projectsModel.findRow(req.params.id);
  if (!row) return res.status(404).json({ error: "Not found" });
  const check = storage.validateFile(req.file);
  if (!check.ok) return res.status(400).json({ error: check.error });
  try {
    const file = storeProjectFile(row, req, { source: "agent", uploadedBy: actorOf(req) });
    if (file.duplicate) return res.status(200).json(Object.assign({ duplicate: true }, file));
    return res.status(201).json(file);
  } catch (e) {
    if (e.status === 400) return res.status(400).json({ error: e.message });
    throw e;
  }
});

function queueImage(req, reason, hints) {
  const { storageKey } = storage.saveProjectFile("_unassigned", req.file);
  const payload = Object.assign({}, hints, { storage_key: storageKey, url: storage.urlForStorageKey(storageKey),
    original_filename: req.file.originalname || null, mime_type: req.file.mimetype, size: req.file.size, uploaded_at: nowIso() });
  const key = hints.telegram_chat_id && hints.telegram_message_id ? `tg:${hints.telegram_chat_id}:${hints.telegram_message_id}` : null;
  return reviewQueue.upsert({ kind: "image", payload, reason, source: "agent", dedupeKey: key });
}

function hintsOf(req) {
  const b = req.body || {};
  return {
    external_project_id: b.external_project_id || null,
    article_number: b.article_number || null,
    manufacturer: b.manufacturer || b.make || null,
    vehicle_model: b.vehicle_model || b.model || null,
    product_type: b.product_type || b.product || null,
    caption: b.caption || b.description || null,
    technical_comment: b.technical_comment || null,
    message_text: b.message_text || null,
    event_at: b.event_at || null,
    telegram_message_id: b.telegram_message_id || null,
    telegram_chat_id: b.telegram_chat_id || null,
    telegram_ref: b.telegram_ref || null
  };
}

// An image the agent itself could not match - stored for manual confirmation, never guessed.
router.post("/images/unassigned", upload.single("file"), (req, res) => {
  const check = storage.validateFile(req.file);
  if (!check.ok) return res.status(400).json({ error: check.error });
  const r = queueImage(req, req.body.reason || "Agent could not confidently match this image to a project.", hintsOf(req));
  res.status(r.duplicate ? 200 : 201).json({ id: r.id, status: "queued_for_review", duplicate: r.duplicate });
});

/**
 * POST /api/agent/telegram/photos - receiver for the future Telegram agent.
 * Matching uses reliable identifiers only, in this order:
 *   external_project_id -> article number (unique) -> manufacturer + model + product (unique, main projects).
 * Anything else (no match, several matches) goes to the review queue. A photo is never attached to a guessed project.
 */
router.post("/telegram/photos", upload.single("file"), (req, res) => {
  const check = storage.validateFile(req.file);
  if (!check.ok) return res.status(400).json({ error: check.error });
  const h = hintsOf(req);
  if (!(h.telegram_message_id && h.telegram_chat_id)) {
    return res.status(400).json({ error: '"telegram_chat_id" and "telegram_message_id" are required (idempotency)' });
  }
  const rows = projectsModel.allRows();
  let matches = [];
  let method = null;
  if (h.external_project_id) {
    matches = rows.filter((r) => r.external_project_id === h.external_project_id);
    method = "external_project_id";
  }
  if (!matches.length && h.article_number) {
    const art = projectsModel.normKey(h.article_number);
    matches = rows.filter((r) => art && projectsModel.normKey((r.data || {}).article_number) === art);
    method = "article_number";
  }
  if (!matches.length && h.manufacturer && h.vehicle_model && h.product_type) {
    const want = [h.manufacturer, h.vehicle_model, h.product_type].map(projectsModel.normKey).join("|");
    matches = rows.filter((r) => {
      const d = r.data || {};
      return (r.record_category || "main_project") === "main_project" &&
        [d.manufacturer, d.vehicle_model, d.product_type].map(projectsModel.normKey).join("|") === want;
    });
    method = "manufacturer+model+product";
  }
  if (matches.length !== 1) {
    const reason = matches.length ? `ambiguous: ${matches.length} projects match by ${method}` : "no project matches the identifiers";
    const r = queueImage(req, reason, h);
    return res.status(r.duplicate ? 200 : 202).json({ status: "queued_for_review", id: r.id, reason, duplicate: r.duplicate });
  }
  try {
    req.body.source = req.body.source || "telegram";
    const file = storeProjectFile(matches[0], req, { source: "telegram", uploadedBy: actorOf(req) });
    return res.status(file.duplicate ? 200 : 201).json(Object.assign({ status: "attached", project_id: matches[0].id,
      matched_by: method }, file));
  } catch (e) {
    if (e.status === 400) return res.status(400).json({ error: e.message });
    throw e;
  }
});

// ---- POST /api/agent/projects/:id/events - idempotent by event_id (original behaviour) ----
router.post("/projects/:id/events", (req, res) => {
  const row = projectsModel.findRow(req.params.id);
  if (!row) return res.status(404).json({ error: "Not found" });
  const eventId = req.body.event_id || req.body.eventId;
  if (!eventId) return res.status(400).json({ error: '"event_id" is required for idempotent processing' });
  const existing = store.read((s) => s.agent_events.find((e) => e.event_id === eventId));
  if (existing) return res.status(200).json({ duplicate: true, event_id: eventId, received_at: existing.received_at });
  const eventType = req.body.type || req.body.event_type || "note";
  store.tx((s) => s.agent_events.push({ event_id: eventId, project_id: row.id, event_type: eventType,
    payload: req.body.payload || {}, received_at: nowIso() }));
  if (eventType === "comment" && req.body.payload && req.body.payload.text) {
    const project = projectsModel.getProject(row.id);
    const comments = (project.comments || []).concat([{ id: uuidv4(), text: req.body.payload.text, source: "agent", at: nowIso() }]);
    projectsModel.safeUpdate(row.id, { comments }, { source: "agent", actor: actorOf(req) });
  }
  projectsModel.recordActivity(row.id, `event:${eventType}`, "agent", actorOf(req), null, req.body.payload || null);
  res.status(201).json({ event_id: eventId, status: "recorded" });
});

// ---- review queue (idempotent by dedupe_key) ----
router.post("/review-queue", (req, res) => {
  const b = req.body || {};
  const unknown = Object.keys(b).filter((k) => !["dedupe_key", "kind", "payload", "reason", "source"].includes(k));
  if (unknown.length) return validationError(res, [`unsupported fields: ${unknown.join(", ")}`], unknown);
  if (!b.kind || typeof b.payload !== "object") return validationError(res, ['"kind" and "payload" (object) are required']);
  const r = reviewQueue.upsert({ kind: b.kind, payload: b.payload, reason: b.reason || null, source: b.source || "agent",
    dedupeKey: b.dedupe_key || null });
  res.status(r.duplicate ? 200 : 201).json(r);
});

// ---- synchronisation runs ----
const SYNC_FIELDS = ["id", "source_file", "file_sha256", "plan_month", "mode", "status", "started_at", "finished_at",
  "counts", "report", "error", "review_status"];
router.post("/sync-runs", (req, res) => {
  const b = req.body || {};
  const unknown = Object.keys(b).filter((k) => !SYNC_FIELDS.includes(k));
  if (unknown.length) return validationError(res, [`unsupported fields: ${unknown.join(", ")}`], unknown);
  if (!b.id || !b.mode || !b.status || !b.started_at) return validationError(res, ['"id", "mode", "status", "started_at" are required']);
  store.tx((s) => {
    const run = { id: b.id, source_file: b.source_file || null, file_sha256: b.file_sha256 || null,
      plan_month: b.plan_month || null, mode: b.mode, status: b.status, started_at: b.started_at,
      finished_at: b.finished_at || null, counts: b.counts || null, error: b.error || null,
      review_status: b.review_status || null };
    const i = s.sync_runs.findIndex((r) => r.id === b.id);
    if (i === -1) s.sync_runs.push(run); else s.sync_runs[i] = Object.assign(s.sync_runs[i], run);
  });
  if (b.report) store.saveRunReport(b.id, b.report);
  res.status(201).json({ id: b.id });
});

router.get("/sync-runs", (req, res) => {
  res.json({ sync_runs: listSyncRuns() });
});

function listSyncRuns() {
  return store.read((s) => s.sync_runs.slice().sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))
    .slice(0, 100).map((r) => JSON.parse(JSON.stringify(r))));
}

module.exports = router;
module.exports.listSyncRuns = listSyncRuns;
