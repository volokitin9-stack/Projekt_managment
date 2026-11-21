"use strict";

// Pure rules for shturman-ko-analyst proposals - no I/O, no store access, no side effects at require
// time. Shared by the server (server/ko/proposals.js) and the agent's local helper
// (tools/ko_analyst/ledger.js) so both compute the SAME dedupe key and apply the SAME structural
// validation - one implementation, not two that could drift.

const crypto = require("crypto");

const KINDS = ["stage_change", "assignee_change", "deadline_change", "work_done", "problem", "new_task",
  "photo_link", "project_match", "needs_clarification"];
// Kinds whose application changes data - always gated by a person's explicit approval.
const GATED = ["stage_change", "assignee_change", "deadline_change", "new_task", "photo_link", "project_match"];
// Kinds that set one project field to a new value. The field is FIXED per kind - a proposal can
// never name an arbitrary field (this is what stops the queue from becoming a generic write path).
const FIELD_BY_KIND = { stage_change: "status", assignee_change: "assigned_engineer", deadline_change: "planned_deadline" };
const STATE_KINDS = Object.keys(FIELD_BY_KIND);
// Kinds that need a resolved project to be applied.
const NEEDS_PROJECT = ["stage_change", "assignee_change", "deadline_change", "work_done", "problem", "photo_link", "project_match"];
const DEFAULT_STAGES = ["scanning", "development", "prototype", "measurement", "documentation"];
const CONFIDENCE = ["high", "medium", "low"];
const DATE_RX = /^\d{4}-\d{2}-\d{2}$/;

const LIMITS = { summary: 500, rationale: 2000, quote: 1000, author: 200, observation: 500, title: 300, value: 200,
  evidence: 20, photos: 20 };

function norm(v) {
  if (v == null) return "";
  return String(v).toLowerCase().replace(/ё/g, "е").replace(/[\s.\-_/]+/g, " ").trim();
}
function normArticle(v) {
  return v == null ? "" : String(v).toLowerCase().replace(/[^0-9a-zа-я]/g, "");
}
/** Whitespace/case/ё-insensitive form used to check that a quote really occurs in a message. */
function normText(v) {
  return v == null ? "" : String(v).toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ").trim();
}
function isValidDate(s) {
  if (!DATE_RX.test(String(s))) return false;
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

/** Public link for a supergroup message (chat_id -100XXXXXXXXXX). Other chats have no t.me link. */
function link(chatId, messageId) {
  const c = String(chatId);
  if (/^-100\d+$/.test(c)) return `https://t.me/c/${c.slice(4)}/${Number(messageId)}`;
  return `tg-message:${c}:${Number(messageId)}`;
}

function projectIdentity(ref = {}) {
  if (ref.project_id) return `id:${ref.project_id}`;
  if (ref.external_project_id) return `ext:${norm(ref.external_project_id)}`;
  if (ref.article_number) return `art:${normArticle(ref.article_number)}`;
  return `mmp:${[ref.manufacturer, ref.vehicle_model, ref.product_type].map(norm).join("|")}`;
}
function evidenceKey(e) {
  return `${String(e.chat_id)}:${Number(e.message_id)}`;
}
function photoKey(p) {
  return p.file_id ? String(p.file_id) : `${p.chat_id || ""}:${p.message_id}`;
}

/**
 * Stable identity of a proposal: the same fact seen again (re-run, overlapping export, Telegram
 * redelivery) yields the same key.
 *   state changes  -> project + field + new value (repeated mentions merge their evidence)
 *   new_task       -> project + normalised title
 *   photo_link     -> project + photo ids
 *   other facts    -> project + the source messages
 */
function dedupeKey(p) {
  const proj = projectIdentity(p.project_ref);
  let basis;
  if (STATE_KINDS.includes(p.kind)) {
    basis = [p.kind, proj, FIELD_BY_KIND[p.kind], norm((p.change || {}).to)];
  } else if (p.kind === "new_task") {
    basis = [p.kind, proj, norm((p.change || {}).title || p.summary)];
  } else if (p.kind === "photo_link") {
    basis = [p.kind, proj, (p.photos || []).map(photoKey).sort().join(",")];
  } else {
    basis = [p.kind, proj, (p.evidence || []).map(evidenceKey).sort().join(",")];
  }
  return "ko:v1:" + crypto.createHash("sha256").update(basis.join("␟")).digest("hex").slice(0, 32);
}

function str(v, max) {
  return typeof v === "string" && v.trim() && v.length <= max;
}

/** Structural validation (no store access). Returns a list of human-readable errors. */
function validate(p, i = 0, { stages = DEFAULT_STAGES } = {}) {
  const errs = [];
  const at = `#${i}`;
  if (!p || typeof p !== "object" || Array.isArray(p)) return [`${at}: не объект`];
  if (!KINDS.includes(p.kind)) errs.push(`${at}: kind должен быть одним из ${KINDS.join(", ")}`);
  if (!CONFIDENCE.includes(p.confidence)) errs.push(`${at}: confidence = high|medium|low`);
  if (p.confidence === "low" && p.kind !== "needs_clarification") errs.push(`${at}: low-уверенность допустима только для needs_clarification`);
  if (!str(p.summary, LIMITS.summary)) errs.push(`${at}: нужен summary (до ${LIMITS.summary} символов)`);
  if (!str(p.rationale, LIMITS.rationale)) errs.push(`${at}: нужно обоснование rationale (до ${LIMITS.rationale} символов)`);
  if (!Array.isArray(p.evidence) || !p.evidence.length) errs.push(`${at}: нужен хотя бы один evidence`);
  else if (p.evidence.length > LIMITS.evidence) errs.push(`${at}: не больше ${LIMITS.evidence} evidence`);
  (Array.isArray(p.evidence) ? p.evidence : []).forEach((e, j) => {
    if (!e || typeof e !== "object" || e.chat_id == null || !Number.isInteger(Number(e.message_id))) {
      errs.push(`${at}.evidence[${j}]: нужны chat_id и message_id`);
    } else if (typeof e.quote !== "string" || e.quote.length > LIMITS.quote) {
      errs.push(`${at}.evidence[${j}]: нужна цитата quote (строка до ${LIMITS.quote} символов)`);
    }
  });
  if (p.photos != null && (!Array.isArray(p.photos) || p.photos.length > LIMITS.photos)) errs.push(`${at}: photos - массив до ${LIMITS.photos}`);
  (Array.isArray(p.photos) ? p.photos : []).forEach((ph, j) => {
    if (!ph || typeof ph !== "object" || (!ph.file_id && !Number.isInteger(Number(ph.message_id)))) errs.push(`${at}.photos[${j}]: нужен file_id`);
    if (ph && ph.observation != null && !(typeof ph.observation === "string" && ph.observation.length <= LIMITS.observation)) {
      errs.push(`${at}.photos[${j}]: observation до ${LIMITS.observation} символов`);
    }
  });
  if (NEEDS_PROJECT.includes(p.kind)) {
    const r = p.project_ref || {};
    const hasId = r.project_id || r.external_project_id || r.article_number || (r.manufacturer && r.vehicle_model);
    if (!hasId) errs.push(`${at}: project_ref должен однозначно указывать проект`);
  }
  if (STATE_KINDS.includes(p.kind)) {
    const c = p.change || {};
    if (c.field != null && c.field !== FIELD_BY_KIND[p.kind]) errs.push(`${at}: для ${p.kind} поле только ${FIELD_BY_KIND[p.kind]}`);
    if (c.to == null || c.to === "") errs.push(`${at}: change.to обязателен`);
    if (p.kind === "stage_change" && !stages.includes(c.to)) errs.push(`${at}: change.to должен быть этапом ${stages.join("|")}`);
    if (p.kind === "stage_change" && c.from && c.from === c.to) errs.push(`${at}: этап не меняется`);
    if (p.kind === "assignee_change" && !str(c.to, LIMITS.value)) errs.push(`${at}: ответственный - строка до ${LIMITS.value} символов`);
    if (p.kind === "deadline_change" && !isValidDate(c.to)) errs.push(`${at}: срок в формате YYYY-MM-DD`);
  }
  if (p.kind === "new_task") {
    const c = p.change || {};
    if (!str(c.title, LIMITS.title)) errs.push(`${at}: для new_task нужен change.title (до ${LIMITS.title} символов)`);
    if (c.responsible != null && !str(c.responsible, LIMITS.value)) errs.push(`${at}: change.responsible - строка`);
    if (c.due != null && !isValidDate(c.due)) errs.push(`${at}: change.due в формате YYYY-MM-DD`);
  }
  if (p.kind === "photo_link" && !(Array.isArray(p.photos) && p.photos.length)) errs.push(`${at}: photo_link без photos`);
  return errs;
}

module.exports = { KINDS, GATED, FIELD_BY_KIND, STATE_KINDS, NEEDS_PROJECT, CONFIDENCE, LIMITS, DEFAULT_STAGES,
  norm, normArticle, normText, isValidDate, link, projectIdentity, evidenceKey, photoKey, dedupeKey, validate };
