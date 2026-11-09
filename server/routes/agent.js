"use strict";

const express = require("express");
const multer = require("multer");
const { v4: uuidv4 } = require("uuid");
const db = require("../db");
const storage = require("../storage");
const projectsModel = require("../projectsModel");
const { requireAgentAuth } = require("../auth");

const router = express.Router();
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: storage.MAX_UPLOAD_BYTES } });

router.use(requireAgentAuth);

function nowIso() {
  return new Date().toISOString();
}

// ---- POST /api/agent/projects — create, or upsert if external_project_id matches ----
// Idempotent: calling this twice with the same external_project_id updates the
// same row instead of creating a duplicate (per migration spec §7/§10).
router.post("/projects", (req, res) => {
  const input = req.body || {};
  const externalId = input.external_project_id || input.externalProjectId || null;

  if (externalId) {
    const existingRow = projectsModel.getProjectByExternalId(externalId);
    if (existingRow) {
      const errors = projectsModel.validateProjectInput(input, { partial: true });
      if (errors.length) return res.status(400).json({ error: "Validation failed", details: errors });
      const result = projectsModel.updateProject(existingRow.id, Object.assign({}, input, { source: "agent" }), {
        source: "agent",
        actor: req.get("x-agent-id") || "agent"
      });
      if (result.error === "version_conflict") {
        return res.status(409).json({ error: "Project was modified since the version you have", current: result.current });
      }
      return res.status(200).json(Object.assign({ matched: "existing" }, result.project));
    }
  }

  const errors = projectsModel.validateProjectInput(input, { partial: false });
  if (errors.length) return res.status(400).json({ error: "Validation failed", details: errors });

  const needsReview = !externalId;
  const project = projectsModel.createProject(Object.assign({}, input, { source: "agent" }), {
    externalId,
    needsReview
  });
  if (needsReview) {
    db.prepare(
      "INSERT INTO review_queue (id, kind, payload, reason, created_at) VALUES (?, 'project', ?, ?, ?)"
    ).run(uuidv4(), JSON.stringify(input), "No external_project_id supplied — future agent updates cannot be matched to this project reliably.", nowIso());
  }
  res.status(201).json(Object.assign({ matched: "created" }, project));
});

// ---- PATCH /api/agent/projects/:id — update by internal id or external id ----
router.patch("/projects/:id", (req, res) => {
  const { id } = req.params;
  let row = projectsModel.getProjectRow(id);
  if (!row) row = projectsModel.getProjectByExternalId(id);
  if (!row) return res.status(404).json({ error: "Not found" });

  const errors = projectsModel.validateProjectInput(req.body || {}, { partial: true });
  if (errors.length) return res.status(400).json({ error: "Validation failed", details: errors });

  const result = projectsModel.updateProject(row.id, Object.assign({}, req.body, { source: req.body.source || "agent" }), {
    source: "agent",
    actor: req.get("x-agent-id") || "agent"
  });
  if (result.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (result.error === "version_conflict") {
    return res.status(409).json({ error: "Project was modified since the version you have", current: result.current });
  }
  res.json(result.project);
});

// ---- POST /api/agent/projects/:id/images — attach an image/attachment to a known project ----
router.post("/projects/:id/images", upload.single("file"), (req, res) => {
  const { id } = req.params;
  let row = projectsModel.getProjectRow(id);
  if (!row) row = projectsModel.getProjectByExternalId(id);
  if (!row) return res.status(404).json({ error: "Not found" });

  const check = storage.validateFile(req.file);
  if (!check.ok) return res.status(400).json({ error: check.error });

  const telegramMessageId = req.body.telegram_message_id || null;
  const telegramChatId = req.body.telegram_chat_id || null;

  // Idempotency: the same Telegram message delivered twice must not create two rows.
  if (telegramMessageId && telegramChatId) {
    const existing = db
      .prepare("SELECT * FROM project_files WHERE telegram_chat_id = ? AND telegram_message_id = ?")
      .get(telegramChatId, telegramMessageId);
    if (existing) {
      return res.status(200).json(Object.assign({ duplicate: true }, projectsModel.rowToApi(row).images.find((i) => i.id === existing.id) || { id: existing.id }));
    }
  }

  const { storageKey } = storage.saveProjectFile(row.id, req.file);
  const fileId = uuidv4();
  const kind = req.file.mimetype === "application/pdf" ? "attachment" : "image";
  const isPrimary = String(req.body.is_primary || "").toLowerCase() === "true" ? 1 : 0;

  db.prepare(`
    INSERT INTO project_files
      (id, project_id, kind, storage_key, original_filename, mime_type, size, is_primary, caption, source, telegram_message_id, telegram_chat_id, uploaded_by, uploaded_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    fileId,
    row.id,
    kind,
    storageKey,
    req.file.originalname || null,
    req.file.mimetype,
    req.file.size,
    isPrimary,
    req.body.caption || null,
    req.body.source || "agent",
    telegramMessageId,
    telegramChatId,
    req.get("x-agent-id") || "agent",
    nowIso()
  );

  if (isPrimary) {
    db.prepare("UPDATE project_files SET is_primary = 0 WHERE project_id = ? AND id != ?").run(row.id, fileId);
  }

  projectsModel.recordActivity(row.id, "image_uploaded", "agent", req.get("x-agent-id") || "agent", null, { fileId, kind });

  const updated = projectsModel.getProject(row.id);
  const file = updated.images.concat(updated.attachments).find((f) => f.id === fileId);
  res.status(201).json(file);
});

// ---- POST /api/agent/images/unassigned — an image the agent could not confidently match ----
// Satisfies: "If the project cannot be identified reliably, do not assign it
// automatically. Store it in a review queue for manual confirmation."
router.post("/images/unassigned", upload.single("file"), (req, res) => {
  const check = storage.validateFile(req.file);
  if (!check.ok) return res.status(400).json({ error: check.error });

  const { storageKey } = storage.saveProjectFile("_unassigned", req.file);
  const entryId = uuidv4();
  const hints = {
    manufacturer: req.body.manufacturer || req.body.make || null,
    vehicle_model: req.body.vehicle_model || req.body.model || null,
    product_type: req.body.product_type || req.body.product || null,
    caption: req.body.caption || null,
    message_text: req.body.message_text || null,
    telegram_message_id: req.body.telegram_message_id || null,
    telegram_chat_id: req.body.telegram_chat_id || null,
    storage_key: storageKey,
    original_filename: req.file.originalname || null,
    mime_type: req.file.mimetype,
    size: req.file.size,
    uploaded_at: nowIso()
  };

  db.prepare(
    "INSERT INTO review_queue (id, kind, payload, reason, created_at) VALUES (?, 'image', ?, ?, ?)"
  ).run(entryId, JSON.stringify(hints), req.body.reason || "Agent could not confidently match this image to a project.", nowIso());

  res.status(201).json({ id: entryId, status: "queued_for_review" });
});

// ---- POST /api/agent/projects/:id/events — record an agent-sourced event ----
// Idempotent by event_id: redelivering the same event_id is a no-op that
// returns the already-stored event instead of recording it twice.
router.post("/projects/:id/events", (req, res) => {
  const { id } = req.params;
  let row = projectsModel.getProjectRow(id);
  if (!row) row = projectsModel.getProjectByExternalId(id);
  if (!row) return res.status(404).json({ error: "Not found" });

  const eventId = req.body.event_id || req.body.eventId;
  if (!eventId) return res.status(400).json({ error: '"event_id" is required for idempotent processing' });

  const existing = db.prepare("SELECT * FROM agent_events WHERE event_id = ?").get(eventId);
  if (existing) {
    return res.status(200).json({ duplicate: true, event_id: eventId, received_at: existing.received_at });
  }

  const eventType = req.body.type || req.body.event_type || "note";
  db.prepare(
    "INSERT INTO agent_events (event_id, project_id, event_type, payload, received_at) VALUES (?, ?, ?, ?, ?)"
  ).run(eventId, row.id, eventType, JSON.stringify(req.body.payload || {}), nowIso());

  // A comment-type event also appends to the project's visible comment list.
  if (eventType === "comment" && req.body.payload && req.body.payload.text) {
    const project = projectsModel.getProject(row.id);
    const comments = (project.comments || []).concat([{
      id: uuidv4(),
      text: req.body.payload.text,
      source: "agent",
      at: nowIso()
    }]);
    projectsModel.updateProject(row.id, { comments }, { source: "agent", actor: req.get("x-agent-id") || "agent" });
  }

  projectsModel.recordActivity(row.id, `event:${eventType}`, "agent", req.get("x-agent-id") || "agent", null, req.body.payload || null);

  res.status(201).json({ event_id: eventId, status: "recorded" });
});

module.exports = router;
