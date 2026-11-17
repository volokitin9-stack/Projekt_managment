"use strict";

// Workflow automation on the server side: validates and applies the agent's
// decision (stage, priority, next action) inside the project's transaction.
//
// Authority (highest first):
//   1. a stage confirmed by a person (stage_source "manual") or by an explicit
//      agent transition ("agent_explicit") - never changed by the automation;
//   2. a stage assigned by an approved deterministic rule ("agent_rule");
//   3. a proposal - stored and shown, never moves a card.
// The server re-checks the rule against config/workflow_automation.json, so an
// agent cannot assign a stage with a rule that is not approved here.
//
// Stage time: an automatically assigned stage gets stage_detected_at (when the
// system recognised it) but stage_entered_at = null - the plan does not tell
// when the project really entered the stage. Only a confirmed transition sets
// stage_entered_at; ordinary updates never reset it.

const fs = require("fs");
const path = require("path");
const reviewQueue = require("./reviewQueueModel");

// WORKFLOW_AUTOMATION_CONFIG: another rules file (e.g. tests, or a staged rule change); default is the repository's.
const CONFIG_FILE = process.env.WORKFLOW_AUTOMATION_CONFIG
  ? path.resolve(process.env.WORKFLOW_AUTOMATION_CONFIG)
  : path.join(__dirname, "..", "config", "workflow_automation.json");
const DASH_STAGES = ["scanning", "development", "prototype", "measurement", "documentation"];
const DECISION_TYPES = ["deterministic", "proposed", "none"];
const CONFIDENCE = ["verified", "probable", "low", "none"];
const PRIORITIES = ["critical", "high", "normal", "low"];
const NEXT_CONFIDENCE = ["verified", "planned", "none"];
const WF_KEYS = ["version", "plan_month", "stage_decision", "source_priority", "source_priority_raw", "system_priority",
  "recommended_priority", "priority_reasons", "next_action", "review_flags"];
const STAGE_CODES = ["stage_undetermined", "ambiguous_stage", "manual_stage_conflict", "backward_transition"];
const STAGE_LABEL = { scanning: "Сканирование", development: "Разработка", prototype: "Тестовый образец",
  measurement: "Замер", documentation: "Оформление" };

let cached = null;
function config() {
  const st = fs.statSync(CONFIG_FILE);
  if (!cached || cached.mtime !== st.mtimeMs) {
    cached = { mtime: st.mtimeMs, cfg: JSON.parse(fs.readFileSync(CONFIG_FILE, "utf-8")) };
  }
  return cached.cfg;
}

function nowIso() {
  return new Date().toISOString();
}
function clone(v) {
  return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
}

/** Validation errors of a workflow block (strict: unknown keys are rejected). */
function validateWorkflow(wf) {
  const e = [];
  if (!wf || typeof wf !== "object" || Array.isArray(wf)) return ["workflow must be an object"];
  Object.keys(wf).filter((k) => !WF_KEYS.includes(k)).forEach((k) => e.push(`workflow.${k}: unsupported field`));
  const d = wf.stage_decision;
  if (!d || typeof d !== "object") {
    e.push("workflow.stage_decision is required");
  } else {
    if (!DECISION_TYPES.includes(d.type)) e.push(`workflow.stage_decision.type must be one of: ${DECISION_TYPES.join(", ")}`);
    if (d.proposed_stage != null && !DASH_STAGES.includes(d.proposed_stage)) {
      e.push(`workflow.stage_decision.proposed_stage must be one of: ${DASH_STAGES.join(", ")}`);
    }
    if (d.type !== "none" && !d.proposed_stage) e.push("workflow.stage_decision.proposed_stage is required unless type is none");
    if (d.confidence != null && !CONFIDENCE.includes(d.confidence)) e.push(`workflow.stage_decision.confidence must be one of: ${CONFIDENCE.join(", ")}`);
    if (d.evidence != null && (!Array.isArray(d.evidence) || d.evidence.length > 50)) e.push("workflow.stage_decision.evidence must be a list (max 50)");
    if (d.type === "deterministic" && !d.rule_id) e.push("workflow.stage_decision.rule_id is required for a deterministic decision");
  }
  ["source_priority", "system_priority", "recommended_priority"].forEach((k) => {
    if (wf[k] != null && !PRIORITIES.includes(wf[k])) e.push(`workflow.${k} must be one of: ${PRIORITIES.join(", ")} or null`);
  });
  if (wf.priority_reasons != null && !Array.isArray(wf.priority_reasons)) e.push("workflow.priority_reasons must be a list");
  const n = wf.next_action;
  if (n != null) {
    if (typeof n !== "object" || typeof n.label !== "string" || !n.label.trim() || n.label.length > 300) {
      e.push("workflow.next_action.label must be a non-empty string (max 300)");
    } else if (n.confidence != null && !NEXT_CONFIDENCE.includes(n.confidence)) {
      e.push(`workflow.next_action.confidence must be one of: ${NEXT_CONFIDENCE.join(", ")}`);
    }
  }
  if (wf.review_flags != null && (!Array.isArray(wf.review_flags) || wf.review_flags.some((f) => !f || typeof f.code !== "string"))) {
    e.push("workflow.review_flags must be a list of {code, message}");
  }
  return e;
}

function ruleAllowsAssignment(cfg, ruleId) {
  if (!cfg.auto_assign_stage) return false;
  return (cfg.stage_rules || []).some((r) => r.id === ruleId && r.enabled && r.mode === "assign");
}

/** The source of the current stage; a stage set before this version counts as confirmed by a person. */
function stageSource(row) {
  const status = (row.data || {}).status || null;
  if (!status) return null;
  return row.stage_source || "manual";
}

function stagePayload(row, wf, extra) {
  const d = (wf && wf.stage_decision) || {};
  const data = row.data || {};
  return Object.assign({ project_id: row.id, external_project_id: row.external_project_id || null,
    project_name: data.project_name || null, current_stage: data.status || null, stage_source: stageSource(row),
    recommended_stage: d.proposed_stage || null, decision: d.type || null, rule_id: d.rule_id || null,
    confidence: d.confidence || null, explanation: d.explanation || null, evidence: (d.evidence || []).slice(0, 12),
    source_file: data.source_file || null, source_sheet: data.source_sheet || null, source_rows: data.source_rows || null,
    plan_month: (wf && wf.plan_month) || null }, extra || {});
}

/**
 * Apply a validated workflow block to `row` inside the caller's transaction (state `s`).
 * push(action, source, actor, prev, next) records project history.
 * Returns { changed, stage_action }.
 */
function applyWorkflow(s, row, wf, { actor = null, sync = null, push, at = null } = {}) {
  const cfg = config();
  const ts = at || nowIso();
  const data = row.data || (row.data = {});
  const fieldSources = row.field_sources || (row.field_sources = {});
  const d = wf.stage_decision;
  const proposed = d.proposed_stage || null;
  const current = data.status || null;
  const src = stageSource(row);
  const sev = (code) => (cfg.review || {})[code] || "review";
  const key = (code) => `workflow:${row.id}:${code}`;
  const open = [];          // review codes that apply now
  let stageAction = "none";
  let changed = false;

  const assignable = d.type === "deterministic" && ruleAllowsAssignment(cfg, d.rule_id);
  if (proposed && data.planned_stage !== proposed) {
    data.planned_stage = proposed;          // the suggestion shown on the card
    fieldSources.planned_stage = "agent";
    changed = true;
  }
  if (assignable && proposed && proposed !== current) {
    if (!current || src === "agent_rule") {
      const backward = current && DASH_STAGES.indexOf(proposed) < DASH_STAGES.indexOf(current);
      if (backward && !cfg.allow_backward_transitions) {
        open.push(["backward_transition", `Агент определил возврат «${STAGE_LABEL[current]}» → «${STAGE_LABEL[proposed]}», ` +
          "но автоматический возврат отключён (allow_backward_transitions=false); этап не изменён"]);
        stageAction = "backward_blocked";
      } else {
        data.status = proposed;
        fieldSources.status = "agent";
        row.stage_source = "agent_rule";
        row.stage_entered_at = null;                 // true entry date unknown
        row.stage_detected_at = ts;
        row.stage_entry_confidence = "unknown";
        push(s, row.id, current ? "stage_auto_changed" : "stage_auto_assigned", "agent", actor, current,
          { stage: proposed, rule_id: d.rule_id, explanation: d.explanation || null, direction: backward ? "backward" : "forward",
            evidence: (d.evidence || []).slice(0, 12), sync_run_id: sync && sync.run_id, source_file: (sync && sync.source_file) || null,
            detected_at: ts });
        stageAction = current ? (backward ? "moved_backward" : "moved") : "assigned";
        changed = true;
        if (backward) {
          open.push(["backward_transition", `Возврат на более ранний этап (доработка): «${STAGE_LABEL[current]}» → ` +
            `«${STAGE_LABEL[proposed]}» по правилу ${d.rule_id}`]);
        }
      }
    } else {
      open.push(["manual_stage_conflict", `Этап «${STAGE_LABEL[current]}» подтверждён вручную; по данным плана агент ` +
        `определил «${STAGE_LABEL[proposed]}» (${d.explanation || d.rule_id}). Этап не изменён`]);
      stageAction = "kept_manual";
    }
  } else if (!current) {
    if (d.type === "none") {
      open.push(["stage_undetermined", `Этап не определён: ${d.explanation || "нет данных"}`]);
    } else {
      open.push(["ambiguous_stage", `Предложен этап «${STAGE_LABEL[proposed]}» (${d.confidence || "—"}): ` +
        `${d.explanation || ""}. Подтвердите или выберите этап`]);
    }
    stageAction = "unresolved";
  }
  (wf.review_flags || []).forEach((f) => {
    if (cfg.review && cfg.review[f.code]) open.push([f.code, f.message || f.code]);
  });

  // review items: open the current ones, resolve the ones that no longer apply
  const openCodes = open.map((o) => o[0]);
  open.forEach(([code, reason]) => {
    reviewQueue.upsertIn(s, { kind: `workflow_${code}`, payload: stagePayload(row, wf, { code }), reason,
      source: "workflow", dedupeKey: key(code), severity: sev(code), reopen: true });
  });
  const known = STAGE_CODES.concat(Object.keys(cfg.review || {}));
  reviewQueue.resolveKeysIn(s, Array.from(new Set(known)).filter((c) => !openCodes.includes(c)).map(key));

  const prev = row.workflow ? JSON.stringify(Object.assign({}, row.workflow, { received_at: null })) : null;
  const next = Object.assign(clone(wf), { received_at: null });
  if (prev !== JSON.stringify(next)) {
    push(s, row.id, "workflow_decision", "agent", actor, null, {
      stage_decision: { type: d.type, rule_id: d.rule_id || null, proposed_stage: proposed, confidence: d.confidence || null },
      stage_action: stageAction, next_action: wf.next_action ? wf.next_action.label : null,
      source_priority: wf.source_priority || null, system_priority: wf.system_priority || null,
      sync_run_id: sync && sync.run_id });
    changed = true;
  }
  row.workflow = Object.assign(clone(wf), { received_at: ts, stage_action: stageAction });
  return { changed, stage_action: stageAction };
}

/** A confirmed transition (person or explicit agent call): stage authority + stage review items resolved. */
function onConfirmedTransition(s, row, source) {
  row.stage_source = source === "manual" ? "manual" : "agent_explicit";
  row.stage_detected_at = null;
  row.stage_entry_confidence = "confirmed";
  reviewQueue.resolveKeysIn(s, STAGE_CODES.map((c) => `workflow:${row.id}:${c}`), "stage_confirmed");
}

// ------------------------------------------------------------------ read-side (API)
function daysBetween(fromIso, now = new Date()) {
  const t = Date.parse(fromIso);
  return Number.isNaN(t) ? null : Math.max(0, Math.floor((now.getTime() - t) / 86400000));
}

/** Effective priority: manual > approved system escalation > source priority. */
function priorityView(row) {
  const data = row.data || {};
  const wf = row.workflow || {};
  const manual = (row.field_sources || {}).priority === "manual" && data.priority;
  let effective = null;
  let source = "unspecified";
  if (manual) {
    effective = data.priority;
    source = "manual";
  } else if (wf.system_priority) {
    effective = wf.recommended_priority || wf.system_priority;
    source = "system";
  } else if (wf.source_priority || data.priority) {
    effective = wf.source_priority || data.priority;
    source = "source";
  }
  return { source_priority: wf.source_priority !== undefined ? wf.source_priority : null,
    source_priority_raw: wf.source_priority_raw !== undefined ? wf.source_priority_raw : (data.priority_original || null),
    system_priority: wf.system_priority || null, effective_priority: effective, priority_source: source,
    priority_reasons: (wf.priority_reasons || []).concat(manual ? ["Приоритет установлен вручную в Штурмане"] : []) };
}

function nextActionView(row) {
  const data = row.data || {};
  if ((row.field_sources || {}).next_action === "manual" && data.next_action) {
    return { label: data.next_action, source: "manual", confidence: "verified", evidence: [],
      updated_at: row.next_action_updated_at || null };
  }
  const n = (row.workflow || {}).next_action;
  if (!n) return null;
  return Object.assign(clone(n), { updated_at: (row.workflow || {}).received_at || null });
}

/** Attention indicators; thresholds come from config and are reported as not configured when absent. */
function attentionView(row, activities, now = new Date()) {
  const cfg = config();
  const att = cfg.attention || {};
  const data = row.data || {};
  const status = data.status || null;
  const out = {};
  out.missing_engineer = (row.record_category || "main_project") === "main_project" && !data.assigned_engineer;
  out.confirmed_blocker = activities.some((a) => a.progress_status === "blocked" && (a.field_sources || {}).progress_status === "manual");
  const deadline = data.planned_deadline ? Date.parse(data.planned_deadline) : NaN;
  out.deadline_exceeded = !Number.isNaN(deadline) && deadline < now.getTime() && data.work_status !== "completed";
  const dates = [];
  activities.forEach((a) => Object.entries(a.hours_by_date || {}).forEach(([d, h]) => { if (h) dates.push(d); }));
  const last = dates.sort().pop() || null;
  out.last_recorded_activity = last;
  if (att.no_recent_activity_days == null) {
    out.no_recent_activity = { configured: false };
  } else {
    const days = last ? daysBetween(last, now) : null;
    out.no_recent_activity = { configured: true, days_since: days, flagged: days != null && days > att.no_recent_activity_days };
  }
  const lim = (att.stage_duration_limits || {})[status === "prototype" ? "test_prototype" : status];
  if (!status || !lim) {
    out.stage_duration = { configured: !!lim };
  } else if (!row.stage_entered_at) {
    out.stage_duration = { configured: true, level: "unknown", reason: "дата входа на этап не подтверждена" };
  } else {
    const days = daysBetween(row.stage_entered_at, now);
    const level = lim.critical_days != null && days > lim.critical_days ? "critical"
      : lim.warning_days != null && days > lim.warning_days ? "warning" : "ok";
    out.stage_duration = { configured: true, days, level, warning_days: lim.warning_days, critical_days: lim.critical_days };
  }
  return out;
}

/** Fields added to the project API (read-only). */
function apiFields(row, activities) {
  const status = (row.data || {}).status || null;
  return Object.assign({
    stage_source: stageSource(row),
    stage_detected_at: row.stage_detected_at || null,
    stage_entry_confidence: status ? (row.stage_entered_at ? (row.stage_entry_confidence || "confirmed") : "unknown") : null,
    days_since_stage_detected: status && row.stage_detected_at ? daysBetween(row.stage_detected_at) : null,
    stage_decision: row.workflow ? clone(row.workflow.stage_decision) : null,
    workflow: row.workflow ? clone(row.workflow) : null,
    next_action: nextActionView(row),
    attention: attentionView(row, activities)
  }, priorityView(row));
}

module.exports = { config, validateWorkflow, applyWorkflow, onConfirmedTransition, apiFields, stageSource, STAGE_CODES,
  CONFIG_FILE };
