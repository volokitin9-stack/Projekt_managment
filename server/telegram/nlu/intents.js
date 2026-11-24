"use strict";

// Rule-based intent recognition for the personal Telegram bot's free-text layer. Pure pattern
// matching (regex + the brand/stage lexicons) - no ML, no network call, nothing "fuzzy" beyond
// normalize.js's loose word/stem comparison. See docs/TELEGRAM_ASSISTANT.md for the full design
// note and its limitations; assistant.js is the only caller.
//
// classify(text) never looks at conversation history - that merge (brand from last turn + stage
// from this one, "the second one", etc.) is assistant.js's job, using the context this returns
// (entity/filters) plus whatever was already remembered.

const { normalizeText, tokenize } = require("./normalize");
const projectsQuery = require("./projectsQuery");
const dates = require("./dates");

// Filler/question words stripped before deciding whether a "проект"-shaped sentence has any
// identifying text left over at all (see matchProjectQuery below) - not a stopword list for
// search itself, which still matches on the untouched original text.
const PROJECT_FILLER_WORDS = new Set(["какие", "какой", "какая", "какое", "покажи", "покажите", "дай", "выведи",
  "все", "всех", "проекты", "проект", "изделие", "изделия", "сейчас", "нас", "есть", "работе", "работы", "в",
  "по", "для", "на", "что", "у", "нам", "надо", "это"]);

function stripPrefix(text, re) {
  const m = text.match(re);
  return m ? text.slice(m[0].length).trim() : null;
}

function stripTrailingDatePhrase(text) {
  return text
    .replace(/\s*(?:на|до|к)?\s*(?:сегодня|завтра|послезавтра)\s*\.?\s*$/i, "")
    .replace(/\s*(?:на|до|к)?\s*(?:понедельник[а-я]*|вторник[а-я]*|сред[а-я]*|четверг[а-я]*|пятниц[а-я]*|суббот[а-я]*|воскресень[а-я]*)\s*\.?\s*$/i, "")
    .replace(/\s*(?:на|до|к)?\s*\d{4}-\d{2}-\d{2}\s*\.?\s*$/i, "")
    .replace(/\s*(?:на|до|к)?\s*\d{1,2}[.\-]\d{1,2}(?:[.\-]\d{2,4})?\s*\.?\s*$/i, "")
    .trim();
}

// -------------------------------------------------------------- task mutation (highest priority)

function matchTaskMutation(text) {
  let rest = stripPrefix(text, /^(?:создай|создать|добавь|добавить)\s+задач[уи]\s*:?\s*/i);
  if (rest !== null) {
    const due = dates.extractDate(rest);
    const title = stripTrailingDatePhrase(rest);
    if (!title) return null;
    return { intent: "create_task", title, due };
  }

  rest = stripPrefix(text, /^(?:перенеси|перенести)\s+задач[уи]\s+/i);
  if (rest !== null) {
    const due = dates.extractDate(rest);
    const targetQuery = stripTrailingDatePhrase(rest);
    if (!targetQuery || !due) return null;
    return { intent: "move_task", targetQuery, due };
  }

  rest = stripPrefix(text, /^(?:отметь|отметить)\s+задач[уи]\s+(?:по\s+)?/i);
  if (rest !== null && /выполненн[а-я]*/i.test(rest)) {
    const targetQuery = rest.replace(/\s*(?:как\s+)?выполненн[а-я]*\.?\s*$/i, "").trim();
    if (!targetQuery) return null;
    return { intent: "complete_task", targetQuery };
  }

  rest = stripPrefix(text, /^(?:закрой|закрыть|заверши|завершить|выполни|выполнить)\s+задач[уи]\s+(?:по\s+)?/i);
  if (rest !== null) {
    const targetQuery = rest.replace(/\.\s*$/, "").trim();
    if (!targetQuery) return null;
    return { intent: "complete_task", targetQuery };
  }

  // explicit project mutation through free text is out of scope by design (ТЗ §4: "виewing only
  // on this first stage") - recognised just well enough to answer instead of silently ignoring it.
  if (/^(?:перенеси|перенести|смени|сменить|измени|изменить|поменяй|поменять)(?:\s|$).*(?:проект|издели)/i.test(text)) {
    return { intent: "project_mutation_blocked" };
  }
  return null;
}

// -------------------------------------------------------------------------------- task queries

function matchTaskQuery(text) {
  const n = normalizeText(text);
  if (!/задач/.test(n) && !/нужно сделать|что делать|мои дела|план на (?:сегодня|завтра|неделю)/.test(n)) return null;

  if (/просрочен/.test(n)) return { intent: "list_tasks", filter: "overdue" };
  if (/на этой недел|на неделе|на эту неделю/.test(n)) return { intent: "list_tasks", filter: "week" };
  if (/сегодня/.test(n)) return { intent: "list_tasks", filter: "today" };
  if (/завтра/.test(n)) return { intent: "list_tasks", filter: "tomorrow" };

  // "покажи задачи Фурсы" / "задачи по Фурсе" - a trailing name after the word "задач*"
  const m = n.match(/задач[а-я]*\s+(?:по\s+)?([a-zа-я][a-zа-я-]{2,})\s*\.?$/i);
  if (m && !["просрочен", "сегодня", "завтра", "неделе", "неделю"].includes(m[1])) {
    // recover the original (non-lowercased) spelling for a nicer reply
    const orig = text.match(/задач[а-я]*\s+(?:по\s+)?([^\s.?!]+)\s*[.?!]?$/i);
    return { intent: "list_tasks", filter: "responsible", responsible: orig ? orig[1] : m[1] };
  }

  if (/задач/.test(n)) return { intent: "list_tasks", filter: "active" };
  return null;
}

// ----------------------------------------------------------------------------- project queries

function matchResponsibleProject(text) {
  const m = normalizeText(text).match(/(?:^|\s)у\s+([а-я][а-я-]{2,})/);
  return m ? m[1] : null;
}

function matchProjectQuery(text) {
  const n = normalizeText(text);
  const { brand, stage, longInStage } = projectsQuery.extractProjectEntities(text);
  const responsible = matchResponsibleProject(text);
  const mentionsProjects = /проект|издели/.test(n);
  if (!brand && !stage && !longInStage && !responsible && !mentionsProjects) return null;
  // A brand/stage/responsible/long-in-stage match already explains the sentence on its own - a
  // bare "покажи все проекты" (nothing left after the filler words) means "show everything", same
  // as /projects. Otherwise, any word still left over after stripping question/filler words is
  // probably an attempted model/article/brand reference this dictionary doesn't know - search for
  // it literally rather than silently falling back to "show everything" (ТЗ §3: never invent a
  // match - an unmatched identifier must report "not found", not the full list).
  let freeText = null;
  if (!brand && !stage && !longInStage && !responsible) {
    const leftover = tokenize(text).filter((w) => !PROJECT_FILLER_WORDS.has(w));
    if (leftover.length) freeText = leftover.join(" ");
  }
  return { intent: "list_projects", brand, stage, longInStage, responsible, freeText };
}

// ---------------------------------------------------------------------------------- dispatcher

/** Classifies one message in isolation (no conversation memory - see header). Returns a plain
 *  intent descriptor, or null if nothing here recognises it at all (assistant.js then either
 *  tries a context-based follow-up or gives up, per the message's conversation state). */
function classify(text) {
  const t = String(text || "").trim();
  if (!t) return null;
  return matchTaskMutation(t) || matchTaskQuery(t) || matchProjectQuery(t);
}

module.exports = { classify, matchTaskMutation, matchTaskQuery, matchProjectQuery, stripTrailingDatePhrase };
