"use strict";

// Import of personal tasks exported from the old Claude Artifact board ("Экспорт задач в JSON",
// shturman_tasks_export_*.json) into the existing `tasks` collection (server/docStore.js layout).
//
//   analyze(payload)              - dry run: counts, new / already imported / conflicts, category
//                                   mapping, fields that cannot be carried over losslessly, plan_hash
//   apply(payload, {planHash})    - ONE store.tx: every new task + the import record, or nothing.
//                                   Refuses unless plan_hash matches a fresh analysis (the person saw
//                                   exactly this plan, and nothing changed since).
//   rollback(importId, {force})   - removes only tasks created by that import; a task edited after
//                                   the import is kept (reported) unless force=true. Idempotent.
//
// Guarantees: the source id is the task id (re-import never duplicates); an existing task is never
// modified or overwritten; no priority/description/project link is invented; no Telegram call is
// made; done tasks stay done; original dates are kept (createdAt = the export's creation date).

const crypto = require("crypto");
const { v4: uuidv4 } = require("uuid");
const store = require("./store");

const QUADRANTS = { inbox: "Распредели", do: "Срочно", plan: "Мои задачи", delegate: "Отдай другому", drop: "Отложить/убрать" };
const CARRIED = ["id", "title", "quadrant", "done", "direction", "responsible", "from", "due", "subtasks", "source",
  "telegramNotified", "calendarSynced", "created_at", "createdAt", "updated_at", "updatedAt", "doneAt", "done_at"];
const SUBTASK_FIELDS = ["id", "title", "responsible", "due", "done", "createdAt"];
const MAX_TASKS = 5000;

function nowIso() {
  return new Date().toISOString();
}
function sha256(text) {
  return crypto.createHash("sha256").update(text).digest("hex");
}
function canonical(v) {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
  return JSON.stringify(v === undefined ? null : v);
}
function isIso(v) {
  return typeof v === "string" && !Number.isNaN(Date.parse(v));
}
function normTitle(v) {
  return String(v || "").toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ").trim();
}

function parse(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return { error: "Файл должен быть JSON-объектом экспорта" };
  if (!Array.isArray(payload.tasks)) return { error: 'В файле нет массива "tasks" — это не экспорт задач' };
  if (payload.tasks.length > MAX_TASKS) return { error: `Слишком много записей (${payload.tasks.length})` };
  return { tasks: payload.tasks, meta: payload.export_meta || null };
}

/** The task document this import would create (no invented fields). */
function toDoc(t, ctx) {
  const extra = {};
  Object.keys(t).forEach((k) => { if (!CARRIED.includes(k)) extra[k] = t[k]; });
  const createdSrc = t.created_at || t.createdAt || null;
  const updatedSrc = t.updated_at || t.updatedAt || null;
  const quadrant = QUADRANTS[t.quadrant] ? t.quadrant : "inbox";
  const data = {
    title: typeof t.title === "string" ? t.title : String(t.title || ""),
    quadrant,
    done: t.done === true,
    direction: t.direction !== undefined ? t.direction : null,
    responsible: t.responsible !== undefined ? t.responsible : null,
    from: t.from !== undefined ? t.from : null,
    due: t.due !== undefined ? t.due : null,
    subtasks: Array.isArray(t.subtasks) ? JSON.parse(JSON.stringify(t.subtasks)) : [],
    telegramNotified: !!t.telegramNotified,
    calendarSynced: !!t.calendarSynced
  };
  if (t.source !== undefined) data.source = t.source;
  if (createdSrc) { data.createdAt = createdSrc; data.importedCreatedAt = createdSrc; }
  if (updatedSrc) data.importedUpdatedAt = updatedSrc;
  if (t.doneAt || t.done_at) data.doneAt = t.doneAt || t.done_at;
  data.import = { import_id: ctx.importId, source_id: String(t.id), source_sha256: ctx.sha, imported_at: ctx.at,
    source_file: ctx.fileName || null };
  if (!QUADRANTS[t.quadrant]) data.import.original_quadrant = t.quadrant === undefined ? null : t.quadrant;
  if (Object.keys(extra).length) data.import.extra = extra;
  return { id: String(t.id), data, rowCreatedAt: isIso(createdSrc) ? new Date(createdSrc).toISOString() : ctx.at };
}

function analyzeIn(s, payload, { fileName = null } = {}) {
  const p = parse(payload);
  if (p.error) return { error: p.error };
  const raw = canonical(payload);
  const sha = sha256(raw);
  const existing = new Map(s.docs.filter((d) => d.collection === "tasks").map((d) => [d.id, d]));
  const titles = new Map();
  existing.forEach((d) => titles.set(normTitle((d.data || {}).title), d.id));

  const report = {
    source: { file_name: fileName, sha256: sha, export_meta: p.meta, tasks: p.tasks.length, active: 0, done: 0,
      subtasks: 0, subtasks_done: 0 },
    target: { existing_tasks: existing.size },
    new: [], already_imported: [], conflicts: [], title_matches: [], warnings: [],
    quadrant_mapping: {}, lossy_fields: [], new_subtasks: 0
  };
  const seen = new Set();
  const extraCounts = {};
  const subExtra = {};
  let noCreated = 0;
  let updatedCount = 0;
  let notifiedFlags = 0;
  let calendarFlags = 0;

  p.tasks.forEach((t, i) => {
    if (!t || typeof t !== "object") { report.conflicts.push({ index: i, type: "not_an_object" }); return; }
    const subs = Array.isArray(t.subtasks) ? t.subtasks : [];
    report.source.subtasks += subs.length;
    report.source.subtasks_done += subs.filter((x) => x && x.done === true).length;
    if (t.done === true) report.source.done += 1; else report.source.active += 1;
    const q = t.quadrant === undefined ? "(нет)" : String(t.quadrant);
    const m = report.quadrant_mapping[q] || (report.quadrant_mapping[q] = {
      target: QUADRANTS[t.quadrant] ? t.quadrant : "inbox", target_label: QUADRANTS[t.quadrant] || QUADRANTS.inbox,
      count: 0, active: 0, done: 0, lossless: !!QUADRANTS[t.quadrant] });
    m.count += 1;
    if (t.done === true) m.done += 1; else m.active += 1;

    if (t.id === undefined || t.id === null || t.id === "") { report.conflicts.push({ index: i, type: "missing_id", title: t.title || null }); return; }
    const id = String(t.id);
    if (seen.has(id)) { report.conflicts.push({ index: i, id, type: "duplicate_id_in_file" }); return; }
    seen.add(id);
    if (typeof t.title !== "string" || !t.title.trim()) report.conflicts.push({ index: i, id, type: "missing_title" });
    if (t.due != null && !isIso(t.due)) report.warnings.push({ id, type: "unparseable_due", value: t.due });
    if (!(t.created_at || t.createdAt)) noCreated += 1;
    if (t.updated_at || t.updatedAt) updatedCount += 1;
    if (t.telegramNotified) notifiedFlags += 1;
    if (t.calendarSynced) calendarFlags += 1;
    Object.keys(t).forEach((k) => { if (!CARRIED.includes(k)) extraCounts[k] = (extraCounts[k] || 0) + 1; });
    subs.forEach((x, j) => {
      if (!x || typeof x !== "object") { report.conflicts.push({ id, type: "bad_subtask", index: j }); return; }
      if (!x.id) report.warnings.push({ id, type: "subtask_without_id", index: j });
      Object.keys(x).forEach((k) => { if (!SUBTASK_FIELDS.includes(k)) subExtra[k] = (subExtra[k] || 0) + 1; });
    });

    const ex = existing.get(id);
    if (ex) {
      const imp = (ex.data || {}).import;
      const legacy = !imp && ((ex.data || {}).importedCreatedAt !== undefined || normTitle((ex.data || {}).title) === normTitle(t.title));
      if (imp && imp.source_id === id) report.already_imported.push({ id, modified_since_import: ex.version !== 1 });
      else if (legacy) report.already_imported.push({ id, legacy: true, modified_since_import: null });
      else report.conflicts.push({ index: i, id, type: "id_used_by_existing_task", existing_title: (ex.data || {}).title || null });
      return;
    }
    if (report.conflicts.some((c) => c.id === id)) return;
    const hit = titles.get(normTitle(t.title));
    if (hit) report.title_matches.push({ source_id: id, existing_id: hit, title: t.title });
    report.new.push(id);
    report.new_subtasks += subs.length;
  });

  // fields that cannot be carried over without loss (or are kept but not shown on the board)
  const lossy = report.lossy_fields;
  Object.entries(report.quadrant_mapping).forEach(([q, m]) => {
    if (!m.lossless) lossy.push({ field: "quadrant", value: q, count: m.count, how: `неизвестная категория → «${QUADRANTS.inbox}», исходное значение сохраняется в import.original_quadrant` });
  });
  if (updatedCount) lossy.push({ field: "updated_at", count: updatedCount, how: "сохраняется как importedUpdatedAt; служебная дата изменения доски — момент импорта" });
  if (noCreated) lossy.push({ field: "created_at", count: noCreated, how: "в экспорте нет даты создания — дата не придумывается, «В работе N дн.» не показывается" });
  if (notifiedFlags || calendarFlags) lossy.push({ field: "telegramNotified / calendarSynced", count: notifiedFlags + calendarFlags, how: "флаги переносятся как есть; уведомления и календарь при импорте не запускаются" });
  Object.entries(extraCounts).forEach(([k, n]) => lossy.push({ field: k, count: n, how: "нет такого поля на доске — сохраняется в import.extra, на доске не отображается" }));
  Object.entries(subExtra).forEach(([k, n]) => lossy.push({ field: `subtasks[].${k}`, count: n, how: "сохраняется внутри подзадачи как есть, на доске не отображается" }));
  lossy.push({ field: "история изменений артефакта", count: null, how: "в экспорте её нет — переносится только текущее состояние задач" });

  const blocking = report.conflicts.filter((c) => c.type !== "id_used_by_existing_task");
  report.can_apply = report.new.length > 0 && blocking.length === 0;
  report.plan_hash = sha256(canonical({ sha, new: report.new.slice().sort(), already: report.already_imported.map((x) => x.id).sort(),
    conflicts: report.conflicts, existing: existing.size }));
  return { report, tasks: p.tasks, sha };
}

function analyze(payload, opts) {
  return store.read((s) => {
    const r = analyzeIn(s, payload, opts);
    return r.error ? { error: r.error } : r.report;
  });
}

function apply(payload, { planHash, actor = null, fileName = null } = {}) {
  return store.tx((s) => {
    const a = analyzeIn(s, payload, { fileName });
    if (a.error) return { status: 400, body: { error: a.error } };
    const rep = a.report;
    if (!planHash || planHash !== rep.plan_hash) {
      return { status: 409, body: { error: "План импорта изменился или не подтверждён — выполните dry-run заново", report: rep } };
    }
    if (rep.conflicts.some((c) => c.type !== "id_used_by_existing_task")) {
      return { status: 409, body: { error: "В файле есть ошибки — импорт невозможен", conflicts: rep.conflicts } };
    }
    if (!rep.new.length) return { status: 200, body: { imported: 0, report: rep, message: "Новых задач нет — ничего не записано" } };
    const at = nowIso();
    const importId = `imp-${at.replace(/[^0-9]/g, "").slice(0, 14)}-${uuidv4().slice(0, 8)}`;
    const want = new Set(rep.new);
    const created = [];
    a.tasks.forEach((t) => {
      if (!t || t.id == null || !want.has(String(t.id))) return;
      const doc = toDoc(t, { importId, sha: a.sha, at, fileName });
      s.docs.push({ collection: "tasks", id: doc.id, data: doc.data, version: 1, created_at: doc.rowCreatedAt, updated_at: at });
      created.push({ id: doc.id, version: 1, subtasks: doc.data.subtasks.length });
      want.delete(doc.id);
    });
    s.task_imports.push({ id: importId, imported_at: at, actor, source_file: fileName, source_sha256: a.sha,
      export_meta: rep.source.export_meta, counts: { tasks: created.length, subtasks: created.reduce((n, c) => n + c.subtasks, 0),
        already_imported: rep.already_imported.length, conflicts_skipped: rep.conflicts.length },
      created: created.map((c) => ({ id: c.id, version: c.version })), rolled_back_at: null, rollback: null });
    return { status: 201, body: { import_id: importId, imported: created.length, subtasks: created.reduce((n, c) => n + c.subtasks, 0),
      skipped_already_imported: rep.already_imported.length, skipped_conflicts: rep.conflicts.length } };
  });
}

function list() {
  return store.read((s) => s.task_imports.slice().reverse().map((x) => JSON.parse(JSON.stringify(x))));
}

/** dryRun: report only. Kept: tasks edited after the import (version != imported), unless force. */
function rollback(importId, { force = false, dryRun = false, actor = null } = {}) {
  const run = (s) => {
    const rec = s.task_imports.find((x) => x.id === importId);
    if (!rec) return { status: 404, body: { error: "Импорт не найден" } };
    const remove = [];
    const keptModified = [];
    const gone = [];
    rec.created.forEach((c) => {
      const d = s.docs.find((x) => x.collection === "tasks" && x.id === c.id);
      if (!d || !(d.data && d.data.import && d.data.import.import_id === importId)) { gone.push(c.id); return; }
      if (d.version !== c.version && !force) keptModified.push({ id: c.id, title: (d.data || {}).title || null });
      else remove.push(c.id);
    });
    const body = { import_id: importId, would_remove: dryRun ? remove : undefined, removed: dryRun ? undefined : remove.length,
      kept_modified: keptModified, already_gone: gone, dry_run: !!dryRun };
    if (dryRun) return { status: 200, body };
    const ids = new Set(remove);
    for (let i = s.docs.length - 1; i >= 0; i -= 1) {
      if (s.docs[i].collection === "tasks" && ids.has(s.docs[i].id)) s.docs.splice(i, 1);
    }
    if (remove.length || !rec.rolled_back_at) {
      rec.rolled_back_at = nowIso();
      rec.rollback = { by: actor, removed: (rec.rollback ? rec.rollback.removed : 0) + remove.length, kept_modified: keptModified.map((k) => k.id), force: !!force };
    }
    return { status: 200, body };
  };
  return dryRun ? store.read(run) : store.tx(run);
}

module.exports = { analyze, apply, rollback, list, QUADRANTS, toDoc };
