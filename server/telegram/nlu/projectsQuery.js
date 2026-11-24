"use strict";

// Read-only project search for the Telegram NLU layer - every function here only reads
// server/projectsModel.js and returns the real rows it finds (or an empty list); nothing here
// invents a project, a stage, a deadline or an engineer's name. Writing to a project is out of
// scope on purpose (see docs/TELEGRAM_ASSISTANT.md §"Limitations" - "viewing only, first stage").

const projectsModel = require("../../projectsModel");
const { looseContains, normalizeText } = require("./normalize");
const { extractBrand } = require("./brands");
const { extractStage, STAGE_LABELS } = require("./stages");

const LONG_IN_STAGE_DAYS = Number(process.env.TELEGRAM_LONG_STAGE_DAYS || 14);

/** Active (not completed) main projects - the same scope personalBot.js's own /projects command
 *  already uses. */
function activeProjects() {
  return projectsModel.listProjects({ category: "main_project", includeRelations: false })
    .filter((p) => !p.actual_completion_date);
}

function matchesBrand(p, canonicalBrand) {
  return normalizeText(p.manufacturer || "") === normalizeText(canonicalBrand);
}

/** Extracts every project-search entity this layer understands out of free text:
 *  - brand (via the synonym dictionary, else null - a brand outside the dictionary is instead
 *    picked up by `freeText` below and matched literally against every field)
 *  - stage (via the stage-root dictionary)
 *  - "long in stage" phrasing ("долго находится", "давно на стадии", "давно висит")
 *  Does not try to pull out model/product/article by themselves from a sentence (too ambiguous
 *  without a query keyword) - those are matched through `freeText`, which is also the fallback
 *  for an explicit /projects <text> search or the step-by-step "🔎 Найти проект" prompt. */
function extractProjectEntities(text) {
  const brand = extractBrand(text);
  const stage = extractStage(text);
  const n = normalizeText(text);
  const longInStage = /долго (находятся|находится|висят|висит)|давно (на стадии|висит|висят|не двигаются|не двигается)/.test(n);
  return { brand: brand ? brand.canonical : null, stage, longInStage };
}

/** Plain, order-independent substring search across every field a person might type free text
 *  against (brand/model/product/article/engineer) - used for /projects <text>, the step-by-step
 *  search prompt, and as a fallback when a sentence names a brand outside brands.js. */
function freeTextSearch(query) {
  return activeProjects().filter((p) => {
    const hay = [p.project_name, p.manufacturer, p.vehicle_model, p.product_type, p.article_number,
      p.assigned_engineer, p.external_project_id].filter(Boolean).join(" ");
    return looseContains(hay, query);
  });
}

/** Projects whose `assigned_engineer` loosely matches `name` (handles Russian case endings, e.g.
 *  a query "Калиткина" matching a stored "Калиткин"). */
function byResponsible(name) {
  return activeProjects().filter((p) => p.assigned_engineer && looseContains(p.assigned_engineer, name));
}

/** Projects that have stayed in their current confirmed stage for at least LONG_IN_STAGE_DAYS
 *  days (env TELEGRAM_LONG_STAGE_DAYS, default 14) - `days_in_stage` is computed server-side
 *  (projectsModel.js), never guessed here. Sorted longest-stuck first. */
function longInStage() {
  return activeProjects()
    .filter((p) => p.status && typeof p.days_in_stage === "number" && p.days_in_stage >= LONG_IN_STAGE_DAYS)
    .sort((a, b) => (b.days_in_stage || 0) - (a.days_in_stage || 0));
}

/** Runs the extracted entities against the data. `extra` (responsible name text, free query text)
 *  comes from intents.js, already split off from the brand/stage match. */
function search({ brand, stage, longInStage: wantLongInStage, responsible, freeText }) {
  if (wantLongInStage) return longInStage();
  if (responsible) return byResponsible(responsible);
  let list = activeProjects();
  if (brand) list = list.filter((p) => matchesBrand(p, brand));
  if (freeText) list = list.filter((p) => looseContains(
    [p.project_name, p.manufacturer, p.vehicle_model, p.product_type, p.article_number].filter(Boolean).join(" "), freeText));
  if (stage) list = list.filter((p) => p.status === stage);
  return list;
}

module.exports = { activeProjects, extractProjectEntities, freeTextSearch, byResponsible, longInStage, search,
  LONG_IN_STAGE_DAYS, STAGE_LABELS };
