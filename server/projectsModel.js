"use strict";

const { v4: uuidv4 } = require("uuid");
const db = require("./db");

const VALID_PRIORITIES = ["critical", "high", "normal", "low"];

function nowIso() {
  return new Date().toISOString();
}

function validStageKeys() {
  return db.prepare("SELECT key FROM stages ORDER BY sort_order ASC").all().map((r) => r.key);
}

function validateProjectInput(input, { partial = false } = {}) {
  const errors = [];
  const required = ["manufacturer", "vehicle_model", "product_type"];
  if (!partial) {
    required.forEach((field) => {
      if (!input[field] || typeof input[field] !== "string" || !input[field].trim()) {
        errors.push(`"${field}" is required`);
      }
    });
  }
  if (input.priority !== undefined && !VALID_PRIORITIES.includes(input.priority)) {
    errors.push(`"priority" must be one of: ${VALID_PRIORITIES.join(", ")}`);
  }
  if (input.status !== undefined) {
    const stages = validStageKeys();
    if (!stages.includes(input.status)) {
      errors.push(`"status" must be one of the configured stages: ${stages.join(", ")}`);
    }
  }
  return errors;
}

const getStmt = db.prepare("SELECT * FROM projects WHERE id = ?");
const getByExternalStmt = db.prepare("SELECT * FROM projects WHERE external_project_id = ?");
const listStmt = db.prepare("SELECT * FROM projects ORDER BY created_at ASC");
const insertStmt = db.prepare(`
  INSERT INTO projects (id, external_project_id, data, version, needs_review, created_at, updated_at)
  VALUES (@id, @externalId, @data, 1, @needsReview, @createdAt, @updatedAt)
`);
const updateStmt = db.prepare(`
  UPDATE projects SET data = @data, version = version + 1, needs_review = @needsReview, updated_at = @updatedAt
  WHERE id = @id AND version = @expectedVersion
`);

const filesForProjectStmt = db.prepare("SELECT * FROM project_files WHERE project_id = ? ORDER BY uploaded_at ASC");
const activityForProjectStmt = db.prepare("SELECT * FROM project_activity WHERE project_id = ? ORDER BY at ASC");
const insertActivityStmt = db.prepare(`
  INSERT INTO project_activity (id, project_id, action, source, actor, previous_value, new_value, at)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?)
`);

function fileToApi(row) {
  const storage = require("./storage");
  return {
    id: row.id,
    project_id: row.project_id,
    kind: row.kind,
    url: storage.urlForStorageKey(row.storage_key),
    original_filename: row.original_filename,
    mime_type: row.mime_type,
    size: row.size,
    is_primary: !!row.is_primary,
    caption: row.caption,
    source: row.source,
    telegram_message_id: row.telegram_message_id,
    telegram_chat_id: row.telegram_chat_id,
    uploaded_by: row.uploaded_by,
    uploaded_at: row.uploaded_at
  };
}

function rowToApi(row, { includeRelations = true } = {}) {
  const data = JSON.parse(row.data);
  const files = includeRelations ? filesForProjectStmt.all(row.id).map(fileToApi) : [];
  const images = files.filter((f) => f.kind === "image");
  const attachments = files.filter((f) => f.kind !== "image");
  const activity = includeRelations ? activityForProjectStmt.all(row.id) : [];

  const manufacturer = data.manufacturer || "";
  const vehicleModel = data.vehicle_model || "";
  const productType = data.product_type || "";
  const projectName = data.project_name || [manufacturer, vehicleModel].filter(Boolean).join(" ") + (productType ? ` — ${productType}` : "");

  return Object.assign({}, data, {
    // canonical, spec-named fields (section 6 of the migration task)
    id: row.id,
    project_id: row.id,
    external_project_id: row.external_project_id || null,
    project_name: projectName,
    manufacturer,
    vehicle_model: vehicleModel,
    product_type: productType,
    assigned_engineer: data.assigned_engineer || "",
    priority: data.priority || "normal",
    status: data.status || "scanning",
    description: data.description || "",
    source: data.source || "manual",
    comments: data.comments || [],
    images,
    attachments,
    activity,
    needs_review: !!row.needs_review,
    created_at: row.created_at,
    updated_at: row.updated_at,
    version: row.version,
    // short aliases the existing (pre-migration) frontend board code reads directly —
    // kept so public/app.js's rendering functions didn't need to change at all.
    make: manufacturer,
    model: vehicleModel,
    product: productType,
    engineer: data.assigned_engineer || "",
    stage: data.status || "scanning",
    deadline: data.planned_deadline || data.deadline || null
  });
}

function getProject(id) {
  const row = getStmt.get(id);
  return row ? rowToApi(row) : null;
}

function getProjectRow(id) {
  return getStmt.get(id);
}

function getProjectByExternalId(externalId) {
  if (!externalId) return null;
  return getByExternalStmt.get(externalId);
}

function listProjects() {
  return listStmt.all().map((row) => rowToApi(row));
}

function toInternalData(input) {
  // Accept both the canonical spec field names and the short frontend aliases,
  // so both the public create path and the agent path can share this.
  const data = {};
  data.manufacturer = input.manufacturer || input.make || "";
  data.vehicle_model = input.vehicle_model || input.model || "";
  data.product_type = input.product_type || input.product || "";
  data.assigned_engineer = input.assigned_engineer || input.engineer || "";
  data.priority = input.priority || "normal";
  data.status = input.status || input.stage || "scanning";
  data.description = input.description || "";
  data.project_name = input.project_name || undefined;
  data.source = input.source || "manual";
  data.planned_deadline = input.planned_deadline || input.deadline || null;
  data.comments = Array.isArray(input.comments) ? input.comments : [];
  return data;
}

function createProject(input, { externalId = null, needsReview = false } = {}) {
  const id = uuidv4();
  const ts = nowIso();
  insertStmt.run({
    id,
    externalId,
    data: JSON.stringify(toInternalData(input)),
    needsReview: needsReview ? 1 : 0,
    createdAt: ts,
    updatedAt: ts
  });
  recordActivity(id, "created", input.source || "manual", input.actor || null, null, null);
  return getProject(id);
}

function updateProject(id, patch, { actor = null, source = "manual" } = {}) {
  const row = getStmt.get(id);
  if (!row) return { error: "not_found" };
  const current = JSON.parse(row.data);
  const expectedVersion = patch.version != null ? Number(patch.version) : row.version;
  const incoming = toInternalData(Object.assign({}, current, patch));
  // merge rather than replace, so fields the caller didn't mention are preserved
  const merged = Object.assign({}, current, incoming);
  if (patch.comments !== undefined) merged.comments = patch.comments;

  const changedKeys = Object.keys(incoming).filter((k) => JSON.stringify(current[k]) !== JSON.stringify(incoming[k]));

  const result = updateStmt.run({
    id,
    data: JSON.stringify(merged),
    needsReview: row.needs_review,
    updatedAt: nowIso(),
    expectedVersion
  });
  if (result.changes === 0) {
    return { error: "version_conflict", current: getProject(id) };
  }
  changedKeys.forEach((key) => {
    recordActivity(id, `updated:${key}`, source, actor, current[key], incoming[key]);
  });
  return { project: getProject(id) };
}

function recordActivity(projectId, action, source, actor, previousValue, newValue) {
  insertActivityStmt.run(
    uuidv4(),
    projectId,
    action,
    source || null,
    actor || null,
    previousValue == null ? null : JSON.stringify(previousValue),
    newValue == null ? null : JSON.stringify(newValue),
    nowIso()
  );
}

module.exports = {
  VALID_PRIORITIES,
  validStageKeys,
  validateProjectInput,
  getProject,
  getProjectRow,
  getProjectByExternalId,
  listProjects,
  createProject,
  updateProject,
  recordActivity,
  rowToApi
};
