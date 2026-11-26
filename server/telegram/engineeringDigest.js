"use strict";

// КО (engineering project) changes section of the morning Telegram digest - added alongside the
// 09:00 Asia/Yekaterinburg schedule fix (server/telegram/dailyDigest.js). Reads ONLY
// `project_history` for the 24 hours ending at the moment the digest is built - never the
// project's current state - so a change is only ever reported because something was actually
// recorded happening, not because of what a project merely looks like right now (ТЗ item 4: "не
// текущее состояние проекта как доказательство изменения"). Read-only: this module never writes
// anything.
//
// What's reportable, and why each one is detected from project_history directly rather than
// through server/historyLabels.js's journal view: stage change ("stage_transition"), a new
// project ("created"), work closed/completed ("updated:actual_completion_date" with a value),
// responsible changed ("updated:assigned_engineer"), deadline changed ("updated:planned_deadline"),
// a "significant" plan/comment change (a new comment, the description, or the work status
// changing - specifically excluding the plan-import diagnostic fields historyLabels.js already
// hides from the ordinary journal for the same reason), and a confirmed proposal from the
// shturman-ko-analyst agent (server/ko/proposals.js's apply() always records a
// "comment:stage_transition"/"comment:ko_proposal" note starting "По предложению агента КО" -
// that note is the one reliable signal this was agent-proposed-then-person-confirmed, not the
// agent acting alone). A raw, unprocessed Telegram/КО-chat message or an unapplied proposal never
// reaches project_history at all, so it can never appear here - satisfying "не считать
// необработанные сообщения выполненными изменениями" by construction, not by a special case.

const store = require("../store");

const KO_PROPOSAL_MARKER = "агента КО";

function projectLabel(row) {
  const d = row.data || {};
  return d.project_name || [d.manufacturer, d.vehicle_model, d.product_type].filter(Boolean).join(" ") || row.id;
}

function projectLink(id) {
  const base = String(process.env.DASHBOARD_PUBLIC_URL || "").trim().replace(/\/+$/, "");
  return base ? `${base}/?eng=${id}` : null;
}

function inWindow(at, sinceIso, untilIso) {
  return typeof at === "string" && at >= sinceIso && at <= untilIso;
}

/** One project's changes in the window, from its own history rows only, or null if nothing
 *  reportable happened. `stageLabel(key)` resolves a stage key to its configured label. */
function summarizeProject(rows, stageLabel) {
  const out = { created: false, stage: null, closed: false, responsible: null, deadline: null,
    planOrComment: false, koProposalConfirmed: false };
  rows.forEach((h) => {
    const a = h.action || "";
    if (a === "created") out.created = true;
    else if (a === "stage_transition") out.stage = { from: stageLabel(h.previous_value), to: stageLabel(h.new_value) };
    else if (a === "updated:actual_completion_date" && h.new_value) out.closed = true;
    else if (a === "updated:assigned_engineer") out.responsible = { from: h.previous_value || "—", to: h.new_value || "—" };
    else if (a === "updated:planned_deadline") out.deadline = { from: h.previous_value || "—", to: h.new_value || "—" };
    else if (a === "updated:description" || a === "updated:work_status" || a === "updated:comments" ||
             /^activity_updated:.+\.(comment|result_raw)$/.test(a)) out.planOrComment = true;
    else if ((a === "comment:stage_transition" || a === "comment:ko_proposal") &&
             typeof h.new_value === "string" && h.new_value.indexOf(KO_PROPOSAL_MARKER) !== -1) out.koProposalConfirmed = true;
  });
  const hasAny = out.created || out.stage || out.closed || out.responsible || out.deadline || out.planOrComment;
  return hasAny ? out : null;
}

function describeProject(summary) {
  const bits = [];
  if (summary.created) bits.push("новый проект");
  if (summary.stage) bits.push(`этап: ${summary.stage.from} → ${summary.stage.to}`);
  if (summary.closed) bits.push("работы завершены");
  if (summary.responsible) bits.push(`ответственный: ${summary.responsible.from} → ${summary.responsible.to}`);
  if (summary.deadline) bits.push(`срок: ${summary.deadline.from} → ${summary.deadline.to}`);
  if (summary.planOrComment) bits.push("изменения плана/комментариев");
  if (summary.koProposalConfirmed) bits.push("✅ применено предложение агента КО");
  return bits.join(", ");
}

/** Pure, independently testable: the КО-changes section text for the 24h window ending at `at`.
 *  `projects`: raw project rows (server/store.js's `s.projects`). `history`: raw
 *  `s.project_history`. `stageLabels`: {key: label}. */
function buildEngineeringChangesText(projects, history, at, stageLabels = {}) {
  const untilIso = at.toISOString();
  const sinceIso = new Date(at.getTime() - 24 * 60 * 60 * 1000).toISOString();
  const byProject = new Map();
  history.forEach((h) => {
    if (!inWindow(h.at, sinceIso, untilIso)) return;
    if (!byProject.has(h.project_id)) byProject.set(h.project_id, []);
    byProject.get(h.project_id).push(h);
  });
  const stageLabel = (k) => (k ? (stageLabels[k] || k) : "не подтверждён");
  const lines = [];
  byProject.forEach((rows, projectId) => {
    const summary = summarizeProject(rows, stageLabel);
    if (!summary) return;
    const row = projects.find((p) => p.id === projectId);
    if (!row) return; // a project deleted after the change was recorded - nothing left to link to
    const link = projectLink(projectId);
    const label = projectLabel(row);
    lines.push(`• ${label} — ${describeProject(summary)}${link ? `\n  ${link}` : ""}`);
  });
  if (!lines.length) return "Изменения по проектам КО (24 ч): изменений нет.";
  return `Изменения по проектам КО за 24 ч (${lines.length} ${lines.length === 1 ? "проект" : "проекта"}):\n${lines.join("\n")}`;
}

/** Reads the current store state itself - the one impure entry point dailyDigest.js calls. */
function buildEngineeringChangesSection(at) {
  return store.read((s) => {
    const stageLabels = {};
    (s.stages || []).forEach((st) => { stageLabels[st.key] = st.label; });
    return buildEngineeringChangesText(s.projects || [], s.project_history || [], at, stageLabels);
  });
}

module.exports = { buildEngineeringChangesText, buildEngineeringChangesSection };
