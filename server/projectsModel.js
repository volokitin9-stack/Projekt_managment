"use strict";

// Project read/write/validation logic shared by the dashboard routes, the agent
// routes and the planning-file import. Storage: server/store.js (JSON file).
//
// Stage semantics (important):
// * `status` is the CONFIRMED current stage - the Kanban column. It is only set
//   by an explicit, recorded stage transition (a person moving the card, or an
//   explicit agent transition). It is never derived from planned work.
// * `planned_stage` is what a planning file suggests. It is shown as a
//   suggestion and never moves a card by itself.
// * `stage_entered_at` is the timestamp of the confirmed transition into the
//   current stage. null = unknown -> the dashboard shows "Длительность неизвестна".

const { v4: uuidv4 } = require("uuid");
const store = require("./store");

const VALID_PRIORITIES = ["critical", "high", "normal", "low"];
const CATEGORIES = ["main_project", "additional_work", "backlog"];
const ACTIVITY_TYPES = ["scanning", "development", "test_prototype", "measurement", "documentation",
  "testing", "transfer_extension", "engineering_change"];
const PROGRESS = ["planned", "in_progress", "done", "blocked", "cancelled"];
const MONTH_RX = /^\d{4}-\d{2}$/;
const DATE_RX = /^\d{4}-\d{2}-\d{2}$/;

// Fields a project may carry (canonical names), plus the short aliases the
// original frontend and the original agent example use.
const PROJECT_FIELDS = [
  "external_project_id", "record_category", "project_name", "manufacturer", "vehicle_model", "product_type",
  "article_number", "assigned_engineer", "priority", "priority_original", "status", "planned_stage",
  "work_status", "description", "planned_deadline", "planned_start_date", "actual_completion_date", "comments",
  "source", "source_file", "source_sheet", "source_row", "source_rows", "last_plan_month", "extra_attributes",
  "dependencies", "stages"
];
const ALIASES = { make: "manufacturer", model: "vehicle_model", product: "product_type", engineer: "assigned_engineer",
  stage: "status", deadline: "planned_deadline", externalProjectId: "external_project_id" };
// accepted on requests but not stored as project data
const CONTROL_FIELDS = ["version", "actor"];
// Fields an agent planning sync may write. `status` is NOT here: a confirmed
// stage only changes through an explicit transition.
const AGENT_WRITABLE = PROJECT_FIELDS.filter((f) => !["status", "comments", "source"].includes(f));
// Fields a person may edit in the dashboard.
const MANUAL_EDITABLE = ["project_name", "article_number", "assigned_engineer", "priority", "description",
  "planned_deadline", "work_status"];

const ACTIVITY_AGENT_FIELDS = [
  "activity_key", "plan_month", "sequence", "cycle", "name", "activity_type", "stage", "task_type",
  "project_stage_original", "task_group", "assignee", "assignee_original", "planned_hours", "actual_hours",
  "hours_by_date", "progress_status", "effort_ratio", "items_ok", "items_nok", "result_raw", "comment",
  "details", "source_file", "source_sheet", "source_row"
];
const ACTIVITY_MANUAL_FIELDS = ["progress_status", "manual_note", "assignee"];

function nowIso() {
  return new Date().toISOString();
}

function clone(v) {
  return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
}

// Identity normalisation, identical to the planning agent's norm_key():
// case, ё/е, Cyrillic/Latin look-alikes, spaces and punctuation are ignored.
const HOMOGLYPHS = { "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
  "т": "t", "у": "y", "х": "x", "ё": "e" };
function normKey(v) {
  if (v === null || v === undefined) return "";
  return String(v).normalize("NFKC").toLowerCase().split("").map((ch) => HOMOGLYPHS[ch] || ch).join("")
    .replace(/[\s\-_./\\,;:()"']+/g, "");
}

function validStageKeys() {
  return store.read((s) => s.stages.slice().sort((a, b) => a.sort_order - b.sort_order).map((r) => r.key));
}

function canonical(input) {
  const out = {};
  Object.keys(input || {}).forEach((k) => {
    if (CONTROL_FIELDS.includes(k)) return;
    out[ALIASES[k] || k] = input[k];
  });
  return out;
}

function isEmpty(v) {
  return v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0) ||
    (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0);
}

/**
 * Validate project input.  `strict` rejects unknown fields instead of dropping
 * them silently.  `allowed` restricts which canonical fields may be written.
 */
function validateProjectInput(input, { partial = false, strict = true, allowed = PROJECT_FIELDS, category } = {}) {
  const errors = [];
  const unknown = [];
  Object.keys(input || {}).forEach((k) => {
    if (CONTROL_FIELDS.includes(k)) return;
    const c = ALIASES[k] || k;
    if (!PROJECT_FIELDS.includes(c)) unknown.push(k);
    else if (!allowed.includes(c)) errors.push(`"${k}" may not be written through this endpoint`);
  });
  if (strict && unknown.length) errors.push(`unsupported fields: ${unknown.join(", ")}`);
  const data = canonical(input);
  const cat = data.record_category || category || "main_project";
  if (data.record_category !== undefined && !CATEGORIES.includes(data.record_category)) {
    errors.push(`"record_category" must be one of: ${CATEGORIES.join(", ")}`);
  }
  if (!partial) {
    const required = cat === "main_project" ? ["manufacturer", "vehicle_model", "product_type"] : [];
    required.forEach((field) => {
      if (!data[field] || typeof data[field] !== "string" || !data[field].trim()) errors.push(`"${field}" is required`);
    });
    if (cat !== "main_project" && !(data.product_type || data.project_name)) {
      errors.push('"project_name" or "product_type" is required');
    }
  }
  if (data.priority !== undefined && data.priority !== null && !VALID_PRIORITIES.includes(data.priority)) {
    errors.push(`"priority" must be one of: ${VALID_PRIORITIES.join(", ")} (or null = unspecified)`);
  }
  const stages = validStageKeys();
  ["status", "planned_stage"].forEach((f) => {
    if (data[f] !== undefined && data[f] !== null && !stages.includes(data[f])) {
      errors.push(`"${f}" must be one of the configured stages: ${stages.join(", ")} (or null = unknown)`);
    }
  });
  if (data.last_plan_month !== undefined && data.last_plan_month !== null && !MONTH_RX.test(data.last_plan_month)) {
    errors.push('"last_plan_month" must be YYYY-MM');
  }
  ["planned_deadline", "planned_start_date", "actual_completion_date"].forEach((f) => {
    if (data[f] !== undefined && data[f] !== null && !DATE_RX.test(String(data[f]))) errors.push(`"${f}" must be YYYY-MM-DD`);
  });
  if (data.source_row !== undefined && data.source_row !== null && !Number.isInteger(data.source_row)) {
    errors.push('"source_row" must be an integer');
  }
  if (data.extra_attributes !== undefined && data.extra_attributes !== null &&
      (typeof data.extra_attributes !== "object" || Array.isArray(data.extra_attributes))) {
    errors.push('"extra_attributes" must be an object');
  }
  return { errors, unknown };
}

function validateActivity(a, allowed) {
  const errors = [];
  if (!a || typeof a !== "object" || Array.isArray(a)) return ["activity must be an object"];
  const unknown = Object.keys(a).filter((k) => !allowed.includes(k));
  if (unknown.length) errors.push(`unsupported activity fields: ${unknown.join(", ")}`);
  if (allowed.includes("activity_key") && !(typeof a.activity_key === "string" && a.activity_key)) {
    errors.push("activity_key is required");
  }
  if (a.activity_type != null && !ACTIVITY_TYPES.includes(a.activity_type)) {
    errors.push(`activity_type must be one of: ${ACTIVITY_TYPES.join(", ")}`);
  }
  if (a.stage != null && !validStageKeys().includes(a.stage)) errors.push(`activity stage "${a.stage}" is not a configured stage`);
  if (a.progress_status != null && !PROGRESS.includes(a.progress_status)) {
    errors.push(`progress_status must be one of: ${PROGRESS.join(", ")}`);
  }
  if (a.plan_month != null && !MONTH_RX.test(String(a.plan_month))) errors.push("plan_month must be YYYY-MM");
  ["planned_hours", "actual_hours", "effort_ratio"].forEach((f) => {
    if (a[f] != null && typeof a[f] !== "number") errors.push(`${f} must be a number`);
  });
  return errors;
}

// ---------------------------------------------------------------- lookups
function findRecord(s, idOrExternal) {
  return s.projects.find((r) => r.id === idOrExternal) ||
    s.projects.find((r) => r.external_project_id && r.external_project_id === idOrExternal) || null;
}

function fileToApi(row) {
  const storage = require("./storage");
  return {
    id: row.id,
    project_id: row.project_id,
    kind: row.kind,
    url: storage.urlForStorageKey(row.storage_key),
    original_filename: row.original_filename || null,
    mime_type: row.mime_type || null,
    size: row.size || null,
    is_primary: !!row.is_primary,
    caption: row.caption || null,
    technical_comment: row.technical_comment || null,
    event_at: row.event_at || null,
    source: row.source || null,
    telegram_message_id: row.telegram_message_id || null,
    telegram_chat_id: row.telegram_chat_id || null,
    telegram_ref: row.telegram_ref || null,
    uploaded_by: row.uploaded_by || null,
    uploaded_at: row.uploaded_at
  };
}

function activityToApi(a) {
  const out = clone(a);
  out.field_sources = out.field_sources || {};
  out.removed_from_source = !!out.removed_from_source;
  return out;
}

function workloadSummary(activities) {
  const live = activities.filter((a) => !a.removed_from_source);
  const sum = (xs, f) => xs.reduce((s, a) => s + (Number(a[f]) || 0), 0);
  const byEmployee = {};
  const byDate = {};
  const byMonth = {};
  live.forEach((a) => {
    const who = a.assignee || (a.assignee_original ? `${a.assignee_original} (не подтверждено)` : "Не назначен");
    byEmployee[who] = byEmployee[who] || { planned_hours: 0, actual_hours: 0, activities: 0 };
    byEmployee[who].planned_hours += Number(a.planned_hours) || 0;
    byEmployee[who].actual_hours += Number(a.actual_hours) || 0;
    byEmployee[who].activities += 1;
    const m = a.plan_month || "—";
    byMonth[m] = byMonth[m] || { planned_hours: 0, actual_hours: 0, activities: 0 };
    byMonth[m].planned_hours += Number(a.planned_hours) || 0;
    byMonth[m].actual_hours += Number(a.actual_hours) || 0;
    byMonth[m].activities += 1;
    Object.entries(a.hours_by_date || {}).forEach(([d, h]) => { byDate[d] = (byDate[d] || 0) + (Number(h) || 0); });
  });
  // Remaining work only where it can be estimated: planned hours minus hours
  // already logged, per activity that has a plan. This is effort, not completion.
  const remaining = live.filter((a) => a.planned_hours > 0 && a.progress_status !== "done" &&
    a.progress_status !== "cancelled")
    .reduce((s, a) => s + Math.max((a.planned_hours || 0) - (a.actual_hours || 0), 0), 0);
  return {
    planned_hours: sum(live, "planned_hours"),
    actual_hours: sum(live, "actual_hours"),
    remaining_planned_hours: live.some((a) => a.planned_hours > 0) ? remaining : null,
    activity_count: live.length,
    removed_from_source: activities.length - live.length,
    by_employee: byEmployee,
    by_date: Object.fromEntries(Object.entries(byDate).sort()),
    by_month: byMonth
  };
}

function daysInStage(stageEnteredAt) {
  if (!stageEnteredAt) return null;
  const ms = Date.now() - new Date(stageEnteredAt).getTime();
  return Number.isFinite(ms) ? Math.max(0, Math.floor(ms / 86400000)) : null;
}

function groupBy(list, key) {
  const m = new Map();
  list.forEach((x) => {
    const k = x[key];
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(x);
  });
  return m;
}

function buildIndex(s) {
  return {
    files: groupBy(s.project_files, "project_id"),
    history: groupBy(s.project_history, "project_id"),
    activities: groupBy(s.project_activities, "project_id")
  };
}

function rowToApi(row, { includeRelations = true, index = null } = {}) {
  const idx = includeRelations ? (index || store.read(buildIndex)) : null;
  const data = clone(row.data) || {};
  const files = includeRelations ? (idx.files.get(row.id) || []).slice()
    .sort((a, b) => (a.uploaded_at < b.uploaded_at ? -1 : 1)).map(fileToApi) : [];
  const images = files.filter((f) => f.kind === "image");
  const attachments = files.filter((f) => f.kind !== "image");
  const history = includeRelations ? (idx.history.get(row.id) || []).map(clone) : [];
  const activities = includeRelations ? (idx.activities.get(row.id) || []).slice()
    .sort((a, b) => String(a.plan_month || "").localeCompare(String(b.plan_month || "")) ||
      (a.sequence || 0) - (b.sequence || 0) || String(a.created_at).localeCompare(String(b.created_at)))
    .map(activityToApi) : [];

  const manufacturer = data.manufacturer || "";
  const vehicleModel = data.vehicle_model || "";
  const productType = data.product_type || "";
  const projectName = data.project_name || [manufacturer, vehicleModel].filter(Boolean).join(" ") + (productType ? ` — ${productType}` : "");
  const status = data.status || null;          // confirmed stage; null = not confirmed (never defaulted)
  const priority = data.priority || null;      // null = unspecified (never defaulted)

  return Object.assign({}, data, {
    id: row.id,
    project_id: row.id,
    external_project_id: row.external_project_id || null,
    record_category: row.record_category || "main_project",
    project_name: projectName,
    manufacturer,
    vehicle_model: vehicleModel,
    product_type: productType,
    article_number: data.article_number || null,
    assigned_engineer: data.assigned_engineer || "",
    priority,
    priority_original: data.priority_original !== undefined ? data.priority_original : null,
    status,
    planned_stage: data.planned_stage || null,
    work_status: data.work_status || null,
    planned_deadline: data.planned_deadline || null,
    source_file: data.source_file || null,
    source_sheet: data.source_sheet || null,
    source_row: data.source_row != null ? data.source_row : null,
    extra_attributes: data.extra_attributes || {},
    stage_entered_at: row.stage_entered_at || null,
    days_in_stage: status ? daysInStage(row.stage_entered_at) : null,
    description: data.description || "",
    source: data.source || "manual",
    comments: data.comments || [],
    last_plan_month: row.last_plan_month || null,
    field_sources: clone(row.field_sources || {}),
    images,
    attachments,
    activity: history,          // existing name: the append-only change log
    history,
    activities: includeRelations ? activities : undefined,
    workload: includeRelations ? workloadSummary(activities) : undefined,
    needs_review: !!row.needs_review,
    created_at: row.created_at,
    updated_at: row.updated_at,
    version: row.version,
    // short aliases the existing frontend board code reads directly
    make: manufacturer,
    model: vehicleModel,
    product: productType,
    engineer: data.assigned_engineer || "",
    stage: status,
    deadline: data.planned_deadline || data.deadline || null
  });
}

function getProject(id) {
  return store.read((s) => {
    const row = s.projects.find((r) => r.id === id);
    return row ? rowToApi(row, { index: buildIndex(s) }) : null;
  });
}

function getProjectRow(id) {
  return store.read((s) => clone(s.projects.find((r) => r.id === id) || null));
}

function getProjectByExternalId(externalId) {
  if (!externalId) return null;
  return store.read((s) => clone(s.projects.find((r) => r.external_project_id === externalId) || null));
}

function findRow(idOrExternal) {
  return store.read((s) => clone(findRecord(s, idOrExternal)));
}

function allRows() {
  return store.read((s) => clone(s.projects));
}

function listProjects({ category, includeRelations = true } = {}) {
  return store.read((s) => {
    const index = includeRelations ? buildIndex(s) : null;
    let rows = s.projects.slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    if (category) rows = rows.filter((r) => (r.record_category || "main_project") === category);
    return rows.map((row) => rowToApi(row, { includeRelations, index }));
  });
}

function pushHistory(s, projectId, action, source, actor, previousValue, newValue) {
  s.project_history.push({
    id: uuidv4(),
    project_id: projectId,
    action,
    source: source || null,
    actor: actor || null,
    previous_value: previousValue === undefined ? null : clone(previousValue),
    new_value: newValue === undefined ? null : clone(newValue),
    at: nowIso()
  });
}

function recordActivity(projectId, action, source, actor, previousValue, newValue) {
  store.tx((s) => pushHistory(s, projectId, action, source, actor, previousValue, newValue));
}

// ---------------------------------------------------------------- activities
/**
 * Upsert the activities of ONE plan month.  Other months are never touched.
 * An activity of that month that is missing from the new payload is flagged
 * removed_from_source (never deleted).  Fields last edited manually are kept
 * and reported.  Runs inside the caller's transaction (state `s`).
 */
function applyActivities(s, projectId, activities, scope, sync) {
  if (!Array.isArray(activities)) return { error: "activities must be a list" };
  const month = scope && scope.plan_month;
  if (!month || !MONTH_RX.test(String(month))) return { error: "activity_scope.plan_month (YYYY-MM) is required with activities" };
  const errors = [];
  const seen = new Set();
  activities.forEach((a, i) => {
    validateActivity(a, ACTIVITY_AGENT_FIELDS).forEach((e) => errors.push(`activities[${i}]: ${e}`));
    if (a && a.plan_month != null && a.plan_month !== month) errors.push(`activities[${i}]: outside plan month ${month}`);
    if (a && seen.has(a.activity_key)) errors.push(`activities[${i}]: duplicate activity_key ${a.activity_key}`);
    if (a) seen.add(a.activity_key);
  });
  if (errors.length) return { error: "Validation failed", details: errors };

  const existing = {};
  s.project_activities.filter((r) => r.project_id === projectId && r.plan_month === month)
    .forEach((r) => { existing[r.activity_key] = r; });
  const summary = { added: 0, updated: 0, removed_from_source: 0, kept_manual: [] };
  const ts = nowIso();
  const actor = (sync && sync.actor) || "agent";
  activities.forEach((raw) => {
    const a = Object.assign({}, clone(raw), { plan_month: month });
    const row = existing[a.activity_key];
    if (!row) {
      const fs = {};
      Object.keys(a).forEach((k) => { fs[k] = "agent"; });
      s.project_activities.push(Object.assign({}, a, { id: uuidv4(), project_id: projectId, removed_from_source: false,
        field_sources: fs, created_at: ts, updated_at: ts }));
      pushHistory(s, projectId, "activity_added", "agent", actor, null,
        { activity_key: a.activity_key, name: a.name, plan_month: month, source_file: (sync && sync.source_file) || null,
          sync_run_id: sync && sync.run_id });
      summary.added += 1;
      return;
    }
    const sources = row.field_sources || {};
    let changed = false;
    Object.keys(a).forEach((k) => {
      if (JSON.stringify(row[k] === undefined ? null : row[k]) === JSON.stringify(a[k] === undefined ? null : a[k])) return;
      if (sources[k] === "manual") {
        summary.kept_manual.push({ activity_key: a.activity_key, field: k, dashboard_value: row[k], file_value: a[k] });
        return;
      }
      pushHistory(s, projectId, `activity_updated:${a.activity_key}.${k}`, "agent", actor, row[k], a[k]);
      row[k] = a[k];
      sources[k] = "agent";
      changed = true;
    });
    if (row.removed_from_source) {
      row.removed_from_source = false;
      pushHistory(s, projectId, `activity_updated:${a.activity_key}.removed_from_source`, "agent", actor, true, false);
      changed = true;
    }
    if (changed) {
      row.field_sources = sources;
      row.updated_at = ts;
      summary.updated += 1;
    }
  });
  Object.values(existing).forEach((row) => {
    if (!seen.has(row.activity_key) && !row.removed_from_source) {
      row.removed_from_source = true;
      row.updated_at = ts;
      pushHistory(s, projectId, "activity_removed_from_source", "agent", actor, null, { activity_key: row.activity_key });
      summary.removed_from_source += 1;
    }
  });
  return { summary };
}

function updateActivityManual(projectId, activityId, changes, actor) {
  const errors = validateActivity(changes, ACTIVITY_MANUAL_FIELDS);
  if (errors.length) return { error: "Validation failed", details: errors };
  return store.tx((s) => {
    const row = s.project_activities.find((a) => a.id === activityId && a.project_id === projectId);
    if (!row) return { error: "not_found" };
    const sources = row.field_sources || {};
    let changed = false;
    Object.keys(changes).forEach((k) => {
      if ((row[k] === undefined ? null : row[k]) === changes[k]) return;
      pushHistory(s, projectId, `activity_updated:${row.activity_key}.${k}`, "manual", actor || "dashboard_user", row[k], changes[k]);
      row[k] = changes[k];
      sources[k] = "manual";
      changed = true;
    });
    if (changed) {
      const ts = nowIso();
      row.field_sources = sources;
      row.updated_at = ts;
      const p = s.projects.find((r) => r.id === projectId);
      if (p) { p.version += 1; p.updated_at = ts; }
    }
    return { activity: activityToApi(row) };
  });
}

// ---------------------------------------------------------------- projects
function failWith(status, message, details) {
  const err = new Error(message);
  err.status = status;
  err.details = details;
  return err;
}

function createProject(input, { externalId = null, needsReview = false, source = null, actor = null,
  activities = null, activityScope = null, sync = null } = {}) {
  const data = canonical(clone(input));
  const src = source || data.source || "manual";
  data.source = data.source || src;
  delete data.external_project_id;
  Object.keys(data).forEach((k) => { if (data[k] === undefined) delete data[k]; });
  const id = uuidv4();
  const ts = nowIso();
  const category = data.record_category || "main_project";
  delete data.record_category;
  const lastPlanMonth = data.last_plan_month || null;
  delete data.last_plan_month;
  const fieldSources = {};
  Object.keys(data).forEach((k) => { if (!isEmpty(data[k])) fieldSources[k] = src; });
  const summary = store.tx((s) => {
    s.projects.push({ id, external_project_id: externalId, data, version: 1, needs_review: !!needsReview, created_at: ts,
      updated_at: ts, record_category: category, stage_entered_at: data.status ? ts : null, field_sources: fieldSources,
      last_plan_month: lastPlanMonth });
    pushHistory(s, id, "created", src, actor, null, { record_category: category, source_file: data.source_file || null,
      sync_run_id: sync && sync.run_id });
    if (data.status) pushHistory(s, id, "stage_transition", src, actor, null, data.status);
    if (!activities) return null;
    const r = applyActivities(s, id, activities, activityScope, sync);
    if (r.error) throw failWith(400, r.error, r.details);
    return r.summary;
  });
  const project = getProject(id);
  if (summary) project.activity_summary = summary;
  return project;
}

/**
 * Update a project.  For source "agent": empty values never clear data, and a
 * field last edited manually is kept (reported in `conflicts`).  A change of
 * `status` is a recorded stage transition and restarts the stage timer.
 */
function updateProject(id, patch, { actor = null, source = "manual", expectedVersion = null, activities = null,
  activityScope = null, sync = null, stageAt = null, protectManual = true } = {}) {
  return store.tx((s) => {
    const row = s.projects.find((r) => r.id === id);
    if (!row) return { error: "not_found" };
    const version = expectedVersion != null ? Number(expectedVersion)
      : (patch && patch.version != null ? Number(patch.version) : row.version);
    if (version !== row.version) {
      const err = new Error("version_conflict");
      err.code = "version_conflict";
      throw err;
    }
    const current = row.data || {};
    const incoming = canonical(patch);
    const fieldSources = row.field_sources || {};
    const conflicts = [];
    const changes = [];
    Object.keys(incoming).forEach((k) => {
      const v = incoming[k];
      if (v === undefined || k === "external_project_id") return;
      if (source === "agent" && isEmpty(v)) return;                   // never blank out data
      if (k === "record_category") {
        if (v !== (row.record_category || "main_project")) { changes.push([k, row.record_category, v]); row.record_category = v; }
        return;
      }
      if (k === "last_plan_month") {
        if (v !== (row.last_plan_month || null)) { changes.push([k, row.last_plan_month || null, v]); row.last_plan_month = v; }
        return;
      }
      if (JSON.stringify(current[k] === undefined ? null : current[k]) === JSON.stringify(v)) return;
      if (protectManual && source === "agent" && fieldSources[k] === "manual" && !isEmpty(current[k])) {
        conflicts.push({ field: k, dashboard_value: clone(current[k]), incoming_value: clone(v), resolution: "kept_dashboard",
          reason: "edited manually in the dashboard" });
        return;
      }
      changes.push([k, clone(current[k]), clone(v)]);
      current[k] = clone(v);
      fieldSources[k] = source;
    });
    let summary = null;
    if (activities) {
      const r = applyActivities(s, row.id, activities, activityScope, sync);
      if (r.error) throw failWith(400, r.error, r.details);
      summary = r.summary;
    }
    const statusChange = changes.find((c) => c[0] === "status");
    if (statusChange) row.stage_entered_at = stageAt || nowIso();
    const actChanged = summary && (summary.added || summary.updated || summary.removed_from_source);
    if (changes.length || actChanged) {
      row.data = current;
      row.field_sources = fieldSources;
      row.version += 1;
      row.updated_at = nowIso();
    }
    changes.forEach(([k, prev, next]) => {
      if (k === "status") {
        // logged when it is recorded; a verified earlier entry time is kept as its own event
        pushHistory(s, row.id, "stage_transition", source, actor, prev || null, next);
        if (stageAt) pushHistory(s, row.id, "stage_entered_at", source, actor, null, row.stage_entered_at);
      } else {
        pushHistory(s, row.id, `updated:${k}`, source, actor, prev, next);
      }
    });
    if (sync && sync.run_id && (changes.length || actChanged)) {
      pushHistory(s, row.id, "agent_sync", "agent", actor, null, { sync_run_id: sync.run_id,
        source_file: sync.source_file || null, fields: changes.map((c) => c[0]), activities: summary });
    }
    return { id: row.id, conflicts, activity_summary: summary };
  });
}

function safeUpdate(id, patch, opts) {
  try {
    const r = updateProject(id, patch, opts);
    if (r.error) return r;
    return { project: getProject(r.id), conflicts: r.conflicts, activity_summary: r.activity_summary };
  } catch (e) {
    if (e.code === "version_conflict") return { error: "version_conflict", current: getProject(id) };
    if (e.status === 400) return { error: e.message, details: e.details };
    throw e;
  }
}

/** A confirmed stage transition (person moving a card, or an explicit agent call). */
function moveStage(id, stage, { source = "manual", actor = null, expectedVersion = null, at = null, note = null } = {}) {
  const row = findRow(id);
  if (!row) return { error: "not_found" };
  if (stage !== null && !validStageKeys().includes(stage)) {
    return { error: "Validation failed", details: [`"stage" must be one of: ${validStageKeys().join(", ")}`] };
  }
  if (((row.data || {}).status || null) === stage) return { project: getProject(row.id), unchanged: true };
  // an explicit, confirmed transition is not a planning sync: manual-edit protection does not apply
  const r = safeUpdate(row.id, { status: stage }, { source, actor, expectedVersion, stageAt: at, protectManual: false });
  if (!r.error && note) recordActivity(row.id, "comment:stage_transition", source, actor, null, note);
  return r;
}

module.exports = {
  VALID_PRIORITIES,
  CATEGORIES,
  ACTIVITY_TYPES,
  PROGRESS,
  PROJECT_FIELDS,
  AGENT_WRITABLE,
  MANUAL_EDITABLE,
  ACTIVITY_AGENT_FIELDS,
  ACTIVITY_MANUAL_FIELDS,
  normKey,
  validStageKeys,
  validateProjectInput,
  getProject,
  getProjectRow,
  getProjectByExternalId,
  findRow,
  allRows,
  listProjects,
  createProject,
  updateProject,
  safeUpdate,
  moveStage,
  updateActivityManual,
  recordActivity,
  pushHistory,
  rowToApi,
  workloadSummary
};
