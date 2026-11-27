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

/** Which project (if any) a proposal's photos are meant for right now - resolved_project_id for a
 *  kind that needed one at submit time, or the project a *later* "Создать проект" action made
 *  (critическое требование: photos follow the same project link the text/evidence already do). */
function photoTargetProjectId(p) {
  return p.resolved_project_id ||
    (p.project_created_later && p.project_created_later.result && p.project_created_later.result.project_id) ||
    (p.decision && p.decision.result && p.decision.result.project_id) || null;
}

/** Критическое требование: "во вкладке предложения показывать статус: привязаны / ожидают
 *  привязки / оригинал недоступен" - computed fresh on every read (never cached, never guessed
 *  from the miniature alone) by checking the actual archive file status and the actual project's
 *  attached files, not anything stored at submit time. Returns null when the proposal has no
 *  photos at all. */
function photoStatus(item) {
  const p = item.payload || {};
  if (!Array.isArray(p.photos) || !p.photos.length) return null;
  const projectId = photoTargetProjectId(p);
  const attachedKeys = projectId
    ? new Set(store.read((s) => s.project_files.filter((f) => f.project_id === projectId))
        .map((f) => `${f.telegram_chat_id}:${f.telegram_message_id}`))
    : new Set();
  return p.photos.map((ph) => {
    const meta = ph.file_id ? archive.getFileMeta(ph.file_id) : null;
    // Критическое требование: "не предполагать наличие оригиналов по одной лишь миниатюре" -
    // meta.status alone only says a download once succeeded; archive.readFile() is the one
    // function that actually re-opens the bytes on disk (server/ko/archive.js's filePath() calls
    // fs.existsSync), which is the only way to know the original is still really there.
    const available = !!(meta && meta.status === "stored" && archive.readFile(ph.file_id));
    const key = meta ? `${meta.chat_id}:${meta.message_id}` : null;
    const attached = !!(projectId && key && attachedKeys.has(key));
    let status;
    if (attached) status = "attached";
    else if (!available) status = "unavailable";
    else if (!projectId) status = "pending_no_project";
    else status = "pending_unattached";
    return { file_id: ph.file_id, status, project_id: attached ? projectId : null };
  });
}

function list({ status = "open" } = {}) {
  const all = reviewQueue.list({ kind: KIND, includeResolved: status !== "open" }).map((r) => {
    const ps = photoStatus(r);
    return ps ? Object.assign({}, r, { payload: Object.assign({}, r.payload, { photo_status: ps }) }) : r;
  });
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
 * Критическое требование: "Подтверди, что для ранее принятого предложения можно вручную создать
 * новый проект" + "создание проекта не изменяет задним числом историю первоначального решения" -
 * records a project created *after* an already-resolved acknowledgement in its own field, never
 * touching `resolved`/`resolved_at`/`resolution`/`decision` (the exact snapshot of the original
 * "Принять к сведению" decision). Only ever written once per item - the guard in
 * createProjectFromProposal below reads `payload.project_created_later` before this runs, and this
 * is itself the one and only writer of that field, so a double click (or a repeat request after
 * the first one already completed, including across a restart) finds it already set and is
 * refused before this is called again.
 */
function recordLaterProjectCreation(id, actor, result) {
  return store.tx((s) => {
    const r = s.review_queue.find((x) => x.id === id && x.kind === KIND);
    if (!r || !r.resolved || r.payload.project_created_later) return false;
    r.payload = Object.assign({}, r.payload, { project_created_later: { by: actor, at: nowIso(), result } });
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

/**
 * Attach every photo in a proposal's own `photos[]` to a project - the identical
 * validate/store/dedupe path a manual photo upload or the `photo_link` kind already use
 * (storage.validateFile + projectsModel.attachProjectFile, which already dedupes per-project by
 * (telegram_chat_id, telegram_message_id), so calling this twice for the same message never
 * creates a second copy). Never deletes, downloads twice, or otherwise modifies the archived
 * Telegram original (server/ko/archive.js) - read-only towards it, exactly like `photo_link`.
 *
 * Unlike the `photo_link` kind in apply() below (left untouched - критическое требование
 * "сохранить работающую логику предложения photo_link"), this never aborts partway: a missing or
 * rejected original is recorded in `unavailable` and the rest of the photos still get attached
 * (критическое требование: "при отсутствии оригинала явно указывать, что он недоступен", not fail
 * the whole operation over one bad file) - used by project creation and the manual re-attach
 * action below, neither of which has an existing all-or-nothing contract to preserve.
 */
function attachProposalPhotos(projectId, photos, { actor, caption } = {}) {
  const path = require("path");
  const attached = [];
  const unavailable = [];
  (photos || []).forEach((ph) => {
    const meta = ph.file_id ? archive.getFileMeta(ph.file_id) : null;
    if (!meta || meta.status !== "stored") {
      unavailable.push({ file_id: ph.file_id || null, reason: meta ? (meta.error || `статус: ${meta.status}`) : "не найден в архиве КО" });
      return;
    }
    const f = archive.readFile(ph.file_id);
    if (!f) { unavailable.push({ file_id: ph.file_id, reason: "файл отсутствует на диске" }); return; }
    const file = { buffer: f.buffer, originalname: `telegram-${f.meta.message_id}${path.extname(f.meta.storage_key)}`,
      mimetype: f.meta.mime_type, size: f.buffer.length };
    const check = storage.validateFile(file);
    if (!check.ok) { unavailable.push({ file_id: ph.file_id, reason: check.error }); return; }
    const r = projectsModel.attachProjectFile(projectId, file, { source: "telegram", uploadedBy: actor,
      caption: ph.observation || caption || null, telegramMessageId: f.meta.message_id, telegramChatId: f.meta.chat_id,
      telegramRef: rules.link(f.meta.chat_id, f.meta.message_id) });
    attached.push({ file_id: ph.file_id, project_file_id: r.id, duplicate: !!r.duplicate });
  });
  return { attached, unavailable };
}

// Критическое требование: "Не превращать любое needs_clarification или project_match
// автоматически в новый проект" - apply() above never creates a project for any kind; this is a
// separate, explicit action a person must deliberately choose, with its own validated field set
// (never the full PROJECT_FIELDS an agent plan-sync can write) and its own duplicate check, reusing
// the exact same identity match the dashboard/agent project-creation paths already use
// (projectsModel.matchByIdentifiers). Always record_category "main_project" - "main_project только
// после подтверждения администратора" is this action itself, there is no other path into it.
const NEW_PROJECT_FIELDS = ["manufacturer", "vehicle_model", "product_type", "status", "article_number",
  "assigned_engineer", "priority", "description"];

/**
 * Create a brand-new engineering project from a proposal's evidence. `fields` is whatever the
 * person confirmed in the dialog (pre-filled from the proposal, but never trusted unedited) -
 * marka/model/izdeliye and a confirmed stage are mandatory; the rest optional.
 *
 * Works in two cases:
 *   - the proposal is still open (the normal path - closes it as "applied", exactly as before);
 *   - the proposal was already resolved as "acknowledged" (project_match/needs_clarification,
 *     accepted with zero side effect at the time) and no project has been created from it since -
 *     критическое требование "для ранее принятого предложения можно вручную создать новый
 *     проект". The original decision (resolved_at/resolution/payload.decision) is never touched in
 *     this case - recordLaterProjectCreation() writes to its own separate field.
 * Any other already-resolved state (applied to a real change, or rejected) is refused - creating a
 * project is not a way to undo or redo a decision that already did something else.
 *
 * Synchronous end to end like apply()/reject(), so a repeated click (or two concurrent ones, or a
 * repeat request after a restart) cannot create two projects: the very first thing this does is
 * read the current resolved/project_created_later state, and the one write that can change it
 * (close() or recordLaterProjectCreation()) is the last thing this does before handing back to the
 * caller - nothing in between yields to another request.
 */
function createProjectFromProposal(id, { actor, fields = {} } = {}) {
  const item = getItem(id);
  if (!item) return fail(404, "Предложение не найдено");
  const p = item.payload || {};
  let isLate = false;
  if (item.resolved) {
    if (item.resolution !== "acknowledged") return fail(409, `Предложение уже обработано (${item.resolution})`);
    if (p.project_created_later) {
      return fail(409, "Проект по этому предложению уже создан ранее",
        { existing_ids: [p.project_created_later.result.project_id] });
    }
    isLate = true;
  }
  const input = Object.assign({ record_category: "main_project" }, pick(fields, NEW_PROJECT_FIELDS));
  if (!input.status) return fail(400, "Для нового проекта обязательна подтверждённая стадия", { details: ['"status" обязателен'] });
  const v = projectsModel.validateProjectInput(input, { partial: false, allowed: NEW_PROJECT_FIELDS.concat(["record_category"]) });
  if (v.errors.length) return fail(400, "Validation failed", { details: v.errors });
  const dup = projectsModel.matchByIdentifiers({ manufacturer: input.manufacturer, vehicle_model: input.vehicle_model,
    product_type: input.product_type });
  if (dup.matches.length) {
    return fail(409, "Проект с такой маркой/моделью/изделием уже существует — используйте «Применить» на найденном проекте",
      { existing_ids: dup.matches.map((m) => m.id) });
  }
  const project = projectsModel.createProject(input, { source: SOURCE, actor, needsReview: false });
  const note = `Проект создан по предложению агента КО (${p.kind}):\n${p.summary}\n${evidenceNote(p)}`;
  projectsModel.recordActivity(project.id, "ko_proposal_applied", "manual", actor, null,
    { proposal_id: id, kind: p.kind, created: true, late: isLate, links: (p.evidence || []).map((e) => e.link) });
  projectsModel.safeUpdate(project.id, { comments: [{ id: require("uuid").v4(), text: note, source: "manual", author: actor,
    at: nowIso(), ko_proposal_id: id }] }, { source: "manual", actor });
  // Критическое требование: the bug this phase fixes - a proposal's own evidence photos were
  // never attached to a project created from it (createProjectFromProposal had no photo handling
  // at all before this). Reuses the exact same attach path photo_link already uses for an existing
  // project; a missing original is reported, never silently dropped and never failing the creation
  // that already succeeded.
  const photos = attachProposalPhotos(project.id, p.photos, { actor, caption: p.summary });
  const result = { project_id: project.id, created: true, photos };
  const ok = isLate ? recordLaterProjectCreation(id, actor, result) : close(id, "applied", actor, result);
  if (!ok) return fail(409, "Предложение изменилось параллельно — обновите страницу и повторите");
  return { status: 201, body: { id, applied: true, result, late: isLate } };
}

function reject(id, { actor, reason = null } = {}) {
  const item = getItem(id);
  if (!item) return fail(404, "Предложение не найдено");
  if (item.resolved) return fail(409, `Предложение уже обработано (${item.resolution})`);
  close(id, "rejected", actor, { reason: reason ? String(reason).slice(0, 500) : null });
  return { status: 200, body: { id, rejected: true } };
}

/**
 * Критическое требование: "предусмотреть ручную привязку фотографии из предложения к конкретному
 * проекту" - works on a proposal in ANY state (open, acknowledged, applied, rejected: the photos
 * are evidence from a real Telegram message, independent of whatever the proposal's own decision
 * track is) and for ANY project the person picks, no auto-matching involved - this is the explicit
 * override for exactly the cases automatic matching won't touch (ambiguous identity, or a photo
 * this phase's bug left stranded on an already-created project). Never touches
 * resolved/resolved_at/resolution/decision. Reuses attachProposalPhotos, so re-running this for a
 * project that already has these photos attaches nothing new (dedup is per (chat_id, message_id)
 * on the target project, not per call).
 */
function attachPhotosToProject(id, { actor, projectId } = {}) {
  const item = getItem(id);
  if (!item) return fail(404, "Предложение не найдено");
  const p = item.payload || {};
  if (!Array.isArray(p.photos) || !p.photos.length) return fail(400, "У предложения нет фотографий");
  if (!projectId) return fail(400, '"project_id" обязателен');
  if (!projectsModel.getProjectRow(String(projectId))) return fail(404, "Проект не найден");
  const photos = attachProposalPhotos(projectId, p.photos, { actor, caption: p.summary });
  projectsModel.recordActivity(projectId, "ko_proposal_applied", "manual", actor, null,
    { proposal_id: id, kind: p.kind, manual_photo_attach: true, links: (p.evidence || []).map((e) => e.link) });
  store.tx((s) => {
    const r = s.review_queue.find((x) => x.id === String(id) && x.kind === KIND);
    if (!r) return;
    const prior = (r.payload.photo_attachments || []).filter((a) => a.project_id !== projectId);
    r.payload = Object.assign({}, r.payload, { photo_attachments: prior.concat([{ project_id: projectId, by: actor, at: nowIso(), photos }]) });
  });
  return { status: 200, body: { id, project_id: projectId, photos } };
}

/**
 * Критическое требование: "не выполнять массовую автоматическую привязку без предварительного
 * отчёта" - read-only, changes nothing. Scans every resolved ko_proposal with photos and reports,
 * per proposal, whether its photos are already attached to their linked project, could be attached
 * right now (project known, originals available, just never run - this phase's bug), blocked by a
 * missing original, or have no project linked at all yet. An administrator reviews this and then
 * uses apply()/createProjectFromProposal() (if still open) or attachPhotosToProject() (if already
 * resolved) per proposal - never a bulk auto-apply from this report itself.
 */
function photoBacklogReport() {
  const items = reviewQueue.list({ kind: KIND, includeResolved: true }).filter((r) => (r.payload.photos || []).length);
  const rows = items.map((r) => {
    const ps = photoStatus(r);
    const projectId = photoTargetProjectId(r.payload);
    const project = projectId ? projectsModel.getProject(projectId) : null;
    return {
      proposal_id: r.id, kind: r.payload.kind, summary: r.payload.summary, resolved: r.resolved, resolution: r.resolution || null,
      project_id: projectId, project_label: project ? [project.manufacturer, project.vehicle_model, project.product_type].filter(Boolean).join(" ") : null,
      article_number: (r.payload.project_ref || {}).article_number || (project || {}).article_number || null,
      photos: ps
    };
  });
  const bucket = (pred) => rows.filter((r) => r.photos && r.photos.some(pred));
  return {
    generated_at: nowIso(),
    total_proposals_with_photos: rows.length,
    ready_to_attach: bucket((x) => x.status === "pending_unattached"),
    unavailable: bucket((x) => x.status === "unavailable"),
    no_project_yet: bucket((x) => x.status === "pending_no_project"),
    fully_attached: rows.filter((r) => r.photos && r.photos.every((x) => x.status === "attached")),
    rows
  };
}

module.exports = { submit, list, apply, reject, createProjectFromProposal, attachPhotosToProject, photoBacklogReport,
  resolveProject, KIND, SOURCE, MAX_BATCH };
