"use strict";

// Read-only *and* candidate-finding queries over the personal Kanban board (server/docStore.js
// "tasks") for the Telegram NLU layer. Finding a candidate task is as far as this module goes -
// nothing here writes to a task; applying a change is always done by
// server/telegram/personalBot.js's own patchTask()/tasks.update(), after the owner confirms (see
// assistant.js), so every mutation still goes through the one code path that already has
// idempotency and version-conflict handling.

const { createDocStore } = require("../../docStore");
const dateUtils = require("../dateUtils");
const { looseContains } = require("./normalize");

const tasks = createDocStore("tasks");

function activeTasks() {
  return tasks.list().filter((t) => !t.done);
}

function isOverdue(t) {
  return dateUtils.isOverdueDue(t.due, t.done);
}

function dueYmd(t) {
  if (!t.due) return null;
  return String(t.due).slice(0, 10);
}

function overdueTasks() {
  return activeTasks().filter(isOverdue);
}

function dueToday() {
  const today = dateUtils.localToday();
  return activeTasks().filter((t) => dueYmd(t) === today);
}

function dueThisWeek() {
  const { start, end } = dateUtils.weekRange(dateUtils.localToday());
  return activeTasks().filter((t) => {
    const d = dueYmd(t);
    return d && d >= start && d <= end;
  });
}

function byResponsible(name) {
  return activeTasks().filter((t) => t.responsible && looseContains(t.responsible, name));
}

/** Order-independent substring search across title/description/responsible/direction - used both
 *  for an explicit "покажи задачи по X" query and to find the task a change refers to
 *  ("отметь задачу по HAVAL выполненной", "перенеси задачу TIREI на пятницу"). */
function findCandidates(query) {
  if (!query || !String(query).trim()) return [];
  return activeTasks().filter((t) => {
    const hay = [t.title, t.description, t.responsible, t.direction].filter(Boolean).join(" ");
    return looseContains(hay, query);
  });
}

module.exports = { activeTasks, isOverdue, overdueTasks, dueToday, dueThisWeek, byResponsible, findCandidates, tasks };
