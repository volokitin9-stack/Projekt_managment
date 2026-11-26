"use strict";

// shturman-ko-analyst proposals: the ONLY way the agent can influence Штурман data.
//
//   submit()  - agent side (KO_ANALYST_API_KEY). Validates, ties every piece of evidence to a
//               message that really exists in the КО archive (and checks the quote really occurs in
//               it), resolves the project, then upserts ONE review_queue item per dedupe_key
//               (kind "ko_proposal"). Never touches projects, tasks, or project files.
//   apply()   - dashboard side (a person's session + CSRF, server/routes/koDashboard.js). Performs
//               the change through exactly the same model functions and checks as the existing
//               dashboard routes: moveStage (POST /api/projects/:id/stage), validateProjectInput +
//               safeUpdate with MANUAL_EDITABLE (PATCH /api/projects/:id), the comment append
//               (POST /api/projects/:id/comments), attachProjectFile + storage.validateFile (photo
//               upload), docStore("tasks").create (POST /api/tasks). The proposal can only choose a
//               kind and a value - the field is fixed per kind (rules.FIELD_BY_KIND).
//   reject()  - dashboard side. A rejected proposal is never reopened by a re-submission.

const store = require("../store");
const storage = require("../storage");
const projectsModel = require("../projectsModel");
const reviewQueue = require("../reviewQueueModel");
const koNotifications = require("../koNotificationsModel");
const { createDocStore } = require("../docStore");
const rules = require("./rules");
const archive = require("./archive");

const KIND = "ko_proposal";
const SOURCE = "ko_analyst";
const MAX_BATCH = 100;
const ALLOWED_KEYS = ["kind", "project_ref", "change", "summary", "evidence", "photos", "rationale", "confidence",
  "requires_approval", "dedupe_key", "alternatives"];
const REF_KEYS = ["project_id", "external_project_id", "article_number", "manufacturer", "vehicle_model", "product_type", "project_name"];
const CHANGE_KEYS = ["field", "from", "to", "title", "responsible", "due"];

function nowIso() {
  return new Date().toISOString();
}

function engineeringChatId() {
  return String(process.env.TELEGRAM_ENGINEERING_CHAT_ID || "");
}

function pick(obj, keys) {
  const out = {};
  keys.forEach((k) => { if (obj && obj[k] !== undefined && obj[k] !== null) out[k] = obj[k]; });
  return out;
}

/** Resolve a project reference to exactly one project id, or explain why not. */
function resolveProject(ref = {}) {
  if (ref.project_id) {
    const row = projectsModel.getProjectRow(String(ref.project_id));
    return row ? { project_id: row.id, method: "project_id" } : { project_id: null, reason: "project_id не найден" };
  }
  const { matches, method } = projectsModel.matchByIdentifiers({ external_project_id: ref.external_project_id,
    article_number: ref.article_number, manufacturer: ref.manufacturer, vehicle_model: ref.vehicle_model,
    product_type: ref.product_type });
  if (matches.length === 1) return { project_id: matches[0].id, method };
  return { project_id: null, reason: matches.length ? `неоднозначно: ${matches.length} проектов` : "проект не найден",
    candidates: matches.slice(0, 10).map((m) => m.id) };
}

function checkEvidence(p, i) {
  const errs = [];
  const chat = engineeringChatId();
  const evidence = [];
  (p.evidence || []).forEach((e, j) => {
    const at = `#${i}.evidence[${j}]`;
    if (!chat || String(e.chat_id) !== chat) { errs.push(`${at}: сообщение не из чата КО`); return; }
    const m = archive.getMessage(e.chat_id, e.message_id);
    if (!m) { errs.push(`${at}: сообщение ${e.message_id} отсутствует в архиве КО`); return; }
    const body = rules.normText([m.text, m.caption].filter(Boolean).join("\n"));
    const q = rules.normText(e.quote);
    if (q ? !body.includes(q) : !!body) errs.push(`${at}: цитата не найдена в тексте сообщения ${e.message_id}`);
    // Author/date/link always come from the archive, never from the agent.
    evidence.push({ chat_id: m.chat_id, message_id: m.message_id, seq: m.seq, date: m.date,
      author: (m.from || {}).name || null, quote: String(e.quote || ""), link: rules.link(m.chat_id, m.message_id) });
  });
  return { errs, evidence };
}

function checkPhotos(p, i) {
  const errs = [];
  const photos = [];
  (p.photos || []).forEach((ph, j) => {
    const meta = ph.file_id ? archive.getFileMeta(ph.file_id) : null;
    if (!meta || meta.status !== "stored") { errs.push(`#${i}.photos[${j}]: файл ${ph.file_id || "?"} отсутствует в архиве КО`); return; }
    photos.push({ file_id: meta.id, chat_id: meta.chat_id, message_id: meta.message_id,
      observation: typeof ph.observation === "string" ? ph.observation : null });
  });
  return { errs, photos };
}

function normalize(p, i) {
  const errs = [];
  const unknown = Object.keys(p || {}).filter((k) => !ALLOWED_KEYS.includes(k));
  if (unknown.length) errs.push(`#${i}: неподдерживаемые поля: ${unknown.join(", ")}`);
  errs.push(...rules.validate(p, i, { stages: projectsModel.validStageKeys() }));
  if (errs.length) return { errs };
  const ev = checkEvidence(p, i);
  const ph = checkPhotos(p, i);
  if (ev.errs.length || ph.errs.length) return { errs: ev.errs.concat(ph.errs) };
  const ref = pick(p.project_ref || {}, REF_KEYS);
  const change = pick(p.change || {}, CHANGE_KEYS);
  if (rules.FIELD_BY_KIND[p.kind]) change.field = rules.FIELD_BY_KIND[p.kind];
  const resolved = resolveProject(ref);
  const out = {
    kind: p.kind, project_ref: ref, change, summary: p.summary, rationale: p.rationale, confidence: p.confidence,
    evidence: ev.evidence, photos: ph.photos, alternatives: Array.isArray(p.alternatives) ? p.alternatives.slice(0, 10) : [],
    requires_approval: true, resolved_project_id: resolved.project_id, resolution_note: resolved.reason || null
  };
  // The key is computed server-side (never trusted from the client), on the resolved project when
  // there is one, so different spellings of the same project converge on one proposal.
  out.dedupe_key = rules.dedupeKey(Object.assign({}, out, {
    project_ref: resolved.project_id ? { project_id: resolved.project_id } : ref }));
  return { errs: [], proposal: out };
}

function mergeList(prev, next, keyFn) {
  const seen = new Set((prev || []).map(keyFn));
  return (prev || []).concat((next || []).filter((x) => !seen.has(keyFn(x))));
}

/** Agent submission of up to MAX_BATCH proposals. All-or-nothing validation. */
function submit(list) {
  if (!Array.isArray(list)) return { status: 400, body: { error: '"proposals" must be an array' } };
  if (!list.length) return { status: 400, body: { error: "Нет предложений" } };
  if (list.length > MAX_BATCH) return { status: 400, body: { error: `Не больше ${MAX_BATCH} предложений за раз` } };
  const norm = list.map(normalize);
  const errors = norm.flatMap((n) => n.errs);
  if (errors.length) return { status: 400, body: { error: "Validation failed", details: errors } };
  const results = store.tx((s) => norm.map(({ proposal }) => {
    const existing = s.review_queue.find((r) => r.dedupe_key === proposal.dedupe_key);
    if (existing && existing.resolved) {
      return { dedupe_key: proposal.dedupe_key, id: existing.id, status: "already_resolved", resolution: existing.resolution };
    }
    let payload = proposal;
    let status = "created";
    if (existing) {
      const prev = existing.payload || {};
      payload = Object.assign({}, proposal, {
        evidence: mergeList(prev.evidence, proposal.evidence, rules.evidenceKey),
        photos: mergeList(prev.photos, proposal.photos, rules.photoKey),
        first_submitted_at: prev.first_submitted_at || existing.created_at
      });
      status = payload.evidence.length !== (prev.evidence || []).length || payload.photos.length !== (prev.photos || []).length
        ? "updated" : "duplicate";
    } else {
      payload.first_submitted_at = nowIso();
    }
    payload.last_submitted_at = nowIso();
    const r = reviewQueue.upsertIn(s, { kind: KIND, payload, reason: proposal.summary, source: SOURCE,
      dedupeKey: proposal.dedupe_key, severity: "review" });
    // One notification per proposal id, created the first time it is seen - idempotent, so a
    // re-submission that only merges evidence (status "updated"/"duplicate") never creates a second
    // bell entry for the same proposal (критическое требование: "повторное получение предложения
    // не должно создавать дубль").
    koNotifications.ensureForProposalIn(s, r.id);
    return { dedupe_key: proposal.dedupe_key, id: r.id, status, resolved_project_id: proposal.resolved_project_id };
  }));
  const counts = results.reduce((a, r) => { a[r.status] = (a[r.status] || 0) + 1; return a; }, {});
  return { status: counts.created ? 201 : 200, body: { results, counts } };
}

function list({ status = "open" } = {}) {
  const all = reviewQueue.list({ kind: KIND, includeResolved: status !== "open" });
  if (status === "resolved") return all.filter((r) => r.resolved);
  return all;
}

function getItem(id) {
  return store.read((s) => s.review_queue.find((r) => r.id === String(id) && r.kind === KIND) || null);
}

function evidenceNote(p) {
  return (p.evidence || []).map((e) => `${e.author || "?"}, ${e.date ? e.date.slice(0, 10) : ""}: «${e.quote}» ${e.link}`).join("\n");
}

function fail(status, error, extra) {
  return { status, body: Object.assign({ error }, extra || {}) };
}

/** Mark the item resolved with an audit record (who, when, what was done). */
function close(id, resolution, actor, result) {
  return store.tx((s) => {
    const r = s.review_queue.find((x) => x.id === id && x.kind === KIND && !x.resolved);
    if (!r) return false;
    Object.assign(r, { resolved: true, resolved_at: nowIso(), resolution });
    r.payload = Object.assign({}, r.payload, { decision: { resolution, by: actor, at: r.resolved_at, result: result || null } });
    return true;
  });
}

/**
 * Apply an open proposal on behalf of the person `actor`. opts.version = the project version the
 * person was looking at (required for field/stage changes - the same optimistic-concurrency rule as
 * the dashboard's own PATCH). Synchronous end to end, so two concurrent clicks cannot both apply.
 */
function apply(id, { actor, version = null } = {}) {
  const item = getItem(id);
  if (!item) return fail(404, "Предложение не найдено");
  if (item.resolved) return fail(409, `Предложение уже обработано (${item.resolution})`);
  const p = item.payload || {};
  const kind = p.kind;
  if (!rules.KINDS.includes(kind)) return fail(400, "Неизвестный тип предложения");
  const pid = p.resolved_project_id || null;
  if (rules.NEEDS_PROJECT.includes(kind)) {
    if (!pid) return fail(409, "Проект не определён однозначно — сначала уточните проект, затем отклоните или подайте заново");
    if (!projectsModel.getProjectRow(pid)) return fail(409, "Проект больше не существует");
  }
  const note = `По предложению агента КО (${kind}), подтвердил ${actor}:\n${p.summary}\n${evidenceNote(p)}`;
  const change = p.change || {};
  let result = null;

  if (kind === "stage_change") {
    if (version == null) return fail(400, '"version" проекта обязателен');
    const current = (projectsModel.getProject(pid) || {}).status || null;
    if (change.from && current !== change.from) {
      return fail(409, `Этап проекта уже изменился (сейчас: ${current || "не подтверждён"}) — предложение устарело`);
    }
    const r = projectsModel.moveStage(pid, change.to, { source: "manual", actor, expectedVersion: version, note });
    if (r.error === "version_conflict") return fail(409, "Проект был изменён — обновите страницу");
    if (r.error) return fail(r.error === "not_found" ? 404 : 400, r.error, { details: r.details });
    result = { project_id: pid, stage: change.to, unchanged: !!r.unchanged, version: r.project.version };
  } else if (kind === "assignee_change" || kind === "deadline_change") {
    if (version == null) return fail(400, '"version" проекта обязателен');
    const field = rules.FIELD_BY_KIND[kind];
    const changes = { [field]: change.to };
    const v = projectsModel.validateProjectInput(changes, { partial: true, allowed: projectsModel.MANUAL_EDITABLE });
    if (v.errors.length) return fail(400, "Validation failed", { details: v.errors });
    const r = projectsModel.safeUpdate(pid, changes, { source: "manual", actor, expectedVersion: version });
    if (r.error === "version_conflict") return fail(409, "Проект был изменён — обновите страницу");
    if (r.error) return fail(r.error === "not_found" ? 404 : 400, r.error, { details: r.details });
    projectsModel.recordActivity(pid, "comment:ko_proposal", "manual", actor, null, note);
    result = { project_id: pid, field, value: change.to, version: r.project.version };
  } else if (kind === "work_done" || kind === "problem") {
    const project = projectsModel.getProject(pid);
    const comment = { id: require("uuid").v4(), text: `${kind === "problem" ? "Проблема" : "Выполнено"}: ${p.summary}\n${evidenceNote(p)}`,
      source: "manual", author: actor, at: nowIso(), ko_proposal_id: id };
    const r = projectsModel.safeUpdate(pid, { comments: (project.comments || []).concat([comment]) }, { source: "manual", actor });
    if (r.error) return fail(409, "Проект был изменён — обновите страницу");
    result = { project_id: pid, comment_id: comment.id };
  } else if (kind === "photo_link") {
    const attached = [];
    for (const ph of p.photos || []) {
      const f = archive.readFile(ph.file_id);
      if (!f) return fail(409, `Файл ${ph.file_id} недоступен в архиве`);
      const file = { buffer: f.buffer, originalname: `telegram-${f.meta.message_id}${require("path").extname(f.meta.storage_key)}`,
        mimetype: f.meta.mime_type, size: f.buffer.length };
      const check = storage.validateFile(file);
      if (!check.ok) return fail(400, check.error);
      const r = projectsModel.attachProjectFile(pid, file, { source: "telegram", uploadedBy: actor,
        caption: ph.observation || p.summary, telegramMessageId: f.meta.message_id, telegramChatId: f.meta.chat_id,
        telegramRef: rules.link(f.meta.chat_id, f.meta.message_id) });
      attached.push({ file_id: ph.file_id, project_file_id: r.id, duplicate: !!r.duplicate });
    }
    projectsModel.recordActivity(pid, "comment:ko_proposal", "manual", actor, null, note);
    result = { project_id: pid, attached };
  } else if (kind === "new_task") {
    const doc = createDocStore("tasks").create({ title: change.title, quadrant: "inbox", responsible: change.responsible || null,
      due: change.due || null, done: false, calendarSynced: false, telegramNotified: true, source: SOURCE,
      ko_proposal_id: id, ko_evidence: (p.evidence || []).map((e) => e.link), project_id: pid });
    if (pid) projectsModel.recordActivity(pid, "comment:ko_proposal", "manual", actor, null, note);
    result = { task_id: doc.id };
  } else {
    // project_match / needs_clarification: informational - acknowledging changes nothing.
    result = { acknowledged: true };
  }
  if (pid && kind !== "new_task") {
    projectsModel.recordActivity(pid, "ko_proposal_applied", "manual", actor, null,
      { proposal_id: id, kind, links: (p.evidence || []).map((e) => e.link) });
  }
  close(id, kind === "project_match" || kind === "needs_clarification" ? "acknowledged" : "applied", actor, result);
  return { status: 200, body: { id, applied: true, result } };
}

function reject(id, { actor, reason = null } = {}) {
  const item = getItem(id);
  if (!item) return fail(404, "Предложение не найдено");
  if (item.resolved) return fail(409, `Предложение уже обработано (${item.resolution})`);
  close(id, "rejected", actor, { reason: reason ? String(reason).slice(0, 500) : null });
  return { status: 200, body: { id, rejected: true } };
}

module.exports = { submit, list, apply, reject, resolveProject, KIND, SOURCE, MAX_BATCH };
