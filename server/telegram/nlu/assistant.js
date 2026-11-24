"use strict";

// Top-level entry point of the rule-based natural-language layer. server/telegram/personalBot.js
// calls interpret(text, ctx) exactly once, only after every explicit command and every active
// step-by-step dialog step has already had first refusal (see personalBot.js's dispatcher) - this
// module never sees a message that belongs to the create-task wizard or any other scripted prompt.
//
// interpret() never touches the database for anything but reading - a task is only ever created,
// moved or completed by personalBot.js itself, after the owner presses "Да" (see
// docs/TELEGRAM_ASSISTANT.md "Changing a task"). This module decides *what* to propose; it never
// applies it.
//
// Pluggability (ТЗ §5 "architecture allowing a later LLM, without rewriting commands/checks"):
// intents.classify() is the only thing this file would swap out for an LLM-backed classifier later
// - everything downstream (search, context, confirmation, mutation) stays the same either way. See
// llmHook.js, which this file calls as a documented no-op extension point today.

const intents = require("./intents");
const projectsQuery = require("./projectsQuery");
const tasksQuery = require("./tasksQuery");
const dates = require("./dates");
const llmHook = require("./llmHook");
const { normalizeText, tokenize, looseContains } = require("./normalize");

const NLU_CONTEXT_TTL_MS = 10 * 60 * 1000;
const MAX_LIST = 20;

const ORDINAL_WORDS = { "первый": 1, "первую": 1, "первое": 1, "второй": 2, "вторую": 2, "вторая": 2,
  "третий": 3, "третью": 3, "четвертый": 4, "четвёртый": 4, "четвертую": 4, "пятый": 5, "пятую": 5 };

// Plain substring checks, not \b-bounded regex - JS's \b is defined via \w, which is ASCII-only
// and does not recognise a Cyrillic letter as a "word" character at all.
const CONTINUATION_MARKERS = ["из них", "них", "эти", "этих", "тоже"];
function looksLikeContinuation(normalized) {
  return normalized.startsWith("а ") || normalized.startsWith("а,") ||
    CONTINUATION_MARKERS.some((m) => normalized.includes(m));
}

function freshContext(dialog) {
  const c = dialog && dialog.nlu_context;
  if (!c || !c.at) return null;
  if (Date.now() - Date.parse(c.at) > NLU_CONTEXT_TTL_MS) return null;
  return c;
}

function projectContextPatch(filters, items) {
  return { at: new Date().toISOString(), entity: "project", filters, result_ids: items.map((p) => p.id) };
}

/** A bare ordinal ("второй") or number ("2") referring to the Nth item of the last project list
 *  shown, if `dialog`'s remembered context is still fresh and was a project list. Returns the
 *  matching project row, or null if this message isn't that kind of reference at all (so the
 *  caller can keep trying other interpretations) - distinct from "was a reference but out of
 *  range", which is reported back as {type:"explain"}. */
function resolveOrdinalPick(text, dialog) {
  const ctx = freshContext(dialog);
  if (!ctx || ctx.entity !== "project" || !Array.isArray(ctx.result_ids) || !ctx.result_ids.length) return undefined;
  const n = normalizeText(text);
  let idx = null;
  const bare = n.match(/^(?:номер\s*)?(\d{1,2})$/);
  if (bare) idx = Number(bare[1]);
  else {
    const word = tokenize(text).find((w) => ORDINAL_WORDS[w]);
    if (word) idx = ORDINAL_WORDS[word];
  }
  if (idx === null) return undefined;
  if (idx < 1 || idx > ctx.result_ids.length) return { outOfRange: true };
  const id = ctx.result_ids[idx - 1];
  const items = projectsQuery.activeProjects();
  return { project: items.find((p) => p.id === id) || null };
}

function mergeProjectFilters(result, dialog, originalText) {
  const ctx = freshContext(dialog);
  if (!ctx || ctx.entity !== "project" || !looksLikeContinuation(normalizeText(originalText))) return result;
  return Object.assign({}, result, {
    brand: result.brand || (ctx.filters && ctx.filters.brand) || null,
    stage: result.stage || (ctx.filters && ctx.filters.stage) || null,
    responsible: result.responsible || (ctx.filters && ctx.filters.responsible) || null
  });
}

function stageLabel(key) {
  return projectsQuery.STAGE_LABELS[key] || key;
}

function handleListProjects(result, dialog, originalText) {
  const merged = mergeProjectFilters(result, dialog, originalText);
  const items = projectsQuery.search(merged);
  const titleParts = [];
  if (merged.longInStage) titleParts.push(`долго на одной стадии (от ${projectsQuery.LONG_IN_STAGE_DAYS} дн.)`);
  if (merged.brand) titleParts.push(merged.brand);
  if (merged.stage) titleParts.push(stageLabel(merged.stage).toLowerCase());
  if (merged.responsible) titleParts.push(`у ${merged.responsible}`);
  if (merged.freeText) titleParts.push(`«${merged.freeText}»`);
  const title = titleParts.length ? `Проекты: ${titleParts.join(", ")}` : "Проекты";
  return { type: "projects", title, items: items.slice(0, MAX_LIST),
    contextPatch: projectContextPatch(merged, items) };
}

function handleListTasks(result) {
  let items;
  let title;
  if (result.filter === "overdue") { items = tasksQuery.overdueTasks(); title = "Просроченные задачи"; }
  else if (result.filter === "today") { items = tasksQuery.dueToday(); title = "Задачи на сегодня"; }
  else if (result.filter === "tomorrow") {
    const tomorrow = dates.extractRelativeDate("завтра");
    items = tasksQuery.activeTasks().filter((t) => t.due && String(t.due).slice(0, 10) === tomorrow);
    title = "Задачи на завтра";
  } else if (result.filter === "week") { items = tasksQuery.dueThisWeek(); title = "Задачи на этой неделе"; }
  else if (result.filter === "responsible") { items = tasksQuery.byResponsible(result.responsible); title = `Задачи: ${result.responsible}`; }
  else { items = tasksQuery.activeTasks(); title = "Активные задачи"; }
  return { type: "tasks", title, items: items.slice(0, MAX_LIST) };
}

function handleCreateTask(result, message) {
  return { type: "task_create_confirm", draft: { title: result.title, due: result.due || null },
    originMessageId: message && message.message_id };
}

function pickOrAmbiguous(mutationKind, candidates, extra) {
  if (candidates.length === 0) return { type: "task_not_found", mutationKind, query: extra && extra.targetQuery };
  if (candidates.length === 1) return null; // caller proceeds straight to confirmation
  return { type: "task_ambiguous", mutationKind, candidates: candidates.slice(0, MAX_LIST), extra };
}

function handleMoveTask(result) {
  const candidates = tasksQuery.findCandidates(result.targetQuery);
  const ambiguous = pickOrAmbiguous("move", candidates, { due: result.due, targetQuery: result.targetQuery });
  if (ambiguous) return ambiguous;
  return { type: "task_move_confirm", task: candidates[0], due: result.due };
}

function handleCompleteTask(result) {
  const candidates = tasksQuery.findCandidates(result.targetQuery).filter((t) => !t.done);
  const ambiguous = pickOrAmbiguous("complete", candidates, { targetQuery: result.targetQuery });
  if (ambiguous) return ambiguous;
  return { type: "task_complete_confirm", task: candidates[0] };
}

/** Resolves a reply to an earlier {type:"task_ambiguous"} prompt: a number (1-based, in the order
 *  the candidates were shown) or enough of the title to be unambiguous on its own. Returns the
 *  chosen task, or null (caller re-asks). */
function resolveAmbiguousPick(text, candidateIds) {
  const tasks = candidateIds.map((id) => tasksQuery.tasks.get(id)).filter(Boolean);
  const n = normalizeText(text).match(/^\d{1,2}$/);
  if (n) {
    const idx = Number(n[0]);
    return idx >= 1 && idx <= tasks.length ? tasks[idx - 1] : null;
  }
  const hits = tasks.filter((t) => looseContains(t.title || "", text));
  return hits.length === 1 ? hits[0] : null;
}

/** text: the raw message. dialog: this chat's current dialog row (server/telegram/
 *  personalBot.js's getDialog()), used only to read nlu_context - the caller is responsible for
 *  persisting whatever `contextPatch`/dialog change the result carries.
 *  Returns null when nothing here recognises the message at all (the caller's existing
 *  "offer to create a task" fallback then takes over, unchanged). */
function interpret(text, { dialog, message } = {}) {
  const pick = resolveOrdinalPick(text, dialog);
  if (pick !== undefined) {
    if (pick.outOfRange) return { type: "explain" };
    if (pick.project) return { type: "project_detail", project: pick.project };
    return { type: "explain" };
  }

  let result = intents.classify(text);
  if (!result) {
    // Nothing locally recognised this exact message - a documented, inert hook for a future LLM
    // classifier (never called today; see llmHook.js). Kept so adding one later needs no change
    // to personalBot.js's dispatcher, confirmation flow or role checks.
    result = llmHook.classify(text);
  }
  if (!result) return null;

  if (result.intent === "list_projects") return handleListProjects(result, dialog, text);
  if (result.intent === "list_tasks") return handleListTasks(result);
  if (result.intent === "create_task") return handleCreateTask(result, message);
  if (result.intent === "move_task") return handleMoveTask(result);
  if (result.intent === "complete_task") return handleCompleteTask(result);
  if (result.intent === "project_mutation_blocked") return { type: "project_mutation_blocked" };
  return { type: "explain" };
}

module.exports = { interpret, resolveAmbiguousPick, NLU_CONTEXT_TTL_MS };
