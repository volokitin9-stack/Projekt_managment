"use strict";

// Inspect or remove demonstration projects (source = "seed").
// Stop the server first: the data directory can only be used by one process.
//
//   node scripts/demo-data.js            # list them (default, changes nothing)
//   node scripts/demo-data.js --remove   # delete ONLY seeded projects that were
//                                        # never touched by a person or the agent
//
// A seeded project that has manual or agent changes, files, comments or
// activities is never removed automatically - it is listed instead.

require("dotenv").config();
const store = require("../server/store");

const report = store.read((s) => s.projects.filter((r) => (r.data || {}).source === "seed").map((r) => {
  const d = r.data || {};
  const touched = s.project_history.filter((h) => h.project_id === r.id && h.source !== "seed").length;
  const files = s.project_files.filter((f) => f.project_id === r.id).length;
  const acts = s.project_activities.filter((a) => a.project_id === r.id).length;
  return { id: r.id, name: `${d.manufacturer} ${d.vehicle_model} — ${d.product_type}`, touched, files, acts,
    removable: touched === 0 && files === 0 && acts === 0 && !(d.comments || []).length };
}));
const total = store.read((s) => s.projects.length);
console.log(`${total} projects in the data file, ${report.length} are demonstration projects (source "seed").`);
report.forEach((r) => console.log(`  ${r.removable ? "removable " : "KEEP      "} ${r.name}` +
  (r.removable ? "" : ` (has changes=${r.touched}, files=${r.files}, activities=${r.acts})`)));
if (process.argv.includes("--remove")) {
  const ids = new Set(report.filter((r) => r.removable).map((r) => r.id));
  store.tx((s) => {
    s.projects = s.projects.filter((p) => !ids.has(p.id));
    s.project_history = s.project_history.filter((h) => !ids.has(h.project_id));
  });
  console.log(`removed ${ids.size} untouched demonstration projects`);
}
