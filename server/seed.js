"use strict";

// Seeds the five workflow stages and the sample engineering projects that the
// original Artifact's "Инж. проекты" preview tab shipped with, so a fresh
// install isn't empty. Safe to run multiple times: every statement is
// idempotent (INSERT OR IGNORE / existence checks).

const db = require("./db");
const projectsModel = require("./projectsModel");

const DEFAULT_STAGES = [
  { key: "scanning", label: "Сканирование", sort_order: 1 },
  { key: "development", label: "Разработка", sort_order: 2 },
  { key: "prototype", label: "Тестовый образец", sort_order: 3 },
  { key: "measurement", label: "Замер", sort_order: 4 },
  { key: "documentation", label: "Оформление", sort_order: 5 }
];

const SAMPLE_PROJECTS = [
  { manufacturer: "LADA", vehicle_model: "Niva Legend", product_type: "Защита картера двигателя", assigned_engineer: "Инженер А.", priority: "critical", status: "scanning", deadline: "2026-11-04" },
  { manufacturer: "HAVAL", vehicle_model: "Jolion", product_type: "Защита радиатора", assigned_engineer: "Инженер Б.", priority: "high", status: "scanning", deadline: "2026-11-18" },
  { manufacturer: "CHERY", vehicle_model: "Tiggo 4", product_type: "Брызговики", assigned_engineer: "Инженер В.", priority: "normal", status: "scanning", deadline: null },
  { manufacturer: "GEELY", vehicle_model: "Monjaro", product_type: "Защита топливного бака", assigned_engineer: "Инженер Г.", priority: "low", status: "scanning", deadline: null },
  { manufacturer: "BMW", vehicle_model: "X3", product_type: "Защита радиатора 333.0544.1", assigned_engineer: "Инженер А.", priority: "high", status: "development", deadline: "2026-11-12" },
  { manufacturer: "AUDI", vehicle_model: "Q5 Long", product_type: "Защита картера", assigned_engineer: "Инженер Б.", priority: "normal", status: "development", deadline: null },
  { manufacturer: "CHANGAN", vehicle_model: "UNI-Z", product_type: "Защита картера", assigned_engineer: "Инженер В.", priority: "critical", status: "development", deadline: "2026-11-09" },
  { manufacturer: "TOYOTA", vehicle_model: "RAV4", product_type: "Пороги", assigned_engineer: "Инженер Г.", priority: "normal", status: "development", deadline: "2026-12-01" },
  { manufacturer: "HAVAL", vehicle_model: "Jolion", product_type: "Буксировочное устройство", assigned_engineer: "Инженер А.", priority: "high", status: "prototype", deadline: "2026-11-20" },
  { manufacturer: "CHERY", vehicle_model: "Tiggo 7 Pro", product_type: "Защита КПП", assigned_engineer: "Инженер Б.", priority: "normal", status: "prototype", deadline: null },
  { manufacturer: "LADA", vehicle_model: "Granta", product_type: "Упор капота", assigned_engineer: "Инженер В.", priority: "low", status: "measurement", deadline: null },
  { manufacturer: "GEELY", vehicle_model: "Coolray", product_type: "Защита картера", assigned_engineer: "Инженер Г.", priority: "high", status: "measurement", deadline: "2026-11-15" },
  { manufacturer: "CHANGAN", vehicle_model: "CS35", product_type: "Пороги", assigned_engineer: "Инженер А.", priority: "normal", status: "documentation", deadline: "2026-11-10" },
  { manufacturer: "TOYOTA", vehicle_model: "Camry", product_type: "Защита радиатора", assigned_engineer: "Инженер Б.", priority: "critical", status: "documentation", deadline: "2026-11-07" }
];

function seedStages() {
  const insert = db.prepare("INSERT OR IGNORE INTO stages (key, label, sort_order) VALUES (?, ?, ?)");
  const tx = db.transaction((stages) => {
    stages.forEach((s) => insert.run(s.key, s.label, s.sort_order));
  });
  tx(DEFAULT_STAGES);
}

// Demonstration projects are inserted ONLY when explicitly requested
// (SEED_DEMO_DATA=true or `npm run seed:demo`) and ONLY into an empty
// database, so a production database never receives them automatically.
// Seeded records carry source "seed", which is how they can be told apart
// from real data later (see scripts/demo-data.js).
function seedProjects({ force = false } = {}) {
  const wanted = force || String(process.env.SEED_DEMO_DATA || "").toLowerCase() === "true";
  if (!wanted) return 0;
  const count = db.prepare("SELECT COUNT(*) AS n FROM projects").get().n;
  if (count > 0) return 0;
  SAMPLE_PROJECTS.forEach((p) => projectsModel.createProject(Object.assign({}, p, { source: "seed" }),
    { source: "seed", actor: "seed" }));
  return SAMPLE_PROJECTS.length;
}

function seed({ demo = false } = {}) {
  seedStages();
  return seedProjects({ force: demo });
}

if (require.main === module) {
  const demo = process.argv.includes("--demo");
  const n = seed({ demo });
  console.log(`Seed complete: stages ensured; ${n} demonstration projects inserted.`);
}

module.exports = { seed, DEFAULT_STAGES, SAMPLE_PROJECTS };
