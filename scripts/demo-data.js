"use strict";

// Inspect or remove demonstration projects (source = "seed").
//
//   node scripts/demo-data.js            # list them (default, changes nothing)
//   node scripts/demo-data.js --remove   # delete ONLY seeded projects that were
//                                        # never touched by a person or the agent
//
// A seeded project that has manual or agent changes, files, comments or
// activities is never removed automatically - it is listed instead.

require("dotenv").config();
const db = require("../server/db");

const rows = db.prepare("SELECT * FROM projects").all();
const seeded = rows.filter((r) => JSON.parse(r.data).source === "seed");
const report = seeded.map((r) => {
  const data = JSON.parse(r.data);
  const touched = db.prepare("SELECT COUNT(*) AS n FROM project_activity WHERE project_id = ? AND (source IS NULL OR source != 'seed')").get(r.id).n;
  const files = db.prepare("SELECT COUNT(*) AS n FROM project_files WHERE project_id = ?").get(r.id).n;
  const acts = db.prepare("SELECT COUNT(*) AS n FROM project_activities WHERE project_id = ?").get(r.id).n;
  return { id: r.id, name: `${data.manufacturer} ${data.vehicle_model} — ${data.product_type}`, touched, files, acts,
    removable: touched === 0 && files === 0 && acts === 0 && !(data.comments || []).length };
});
console.log(`${rows.length} projects in the database, ${seeded.length} are demonstration projects (source "seed").`);
report.forEach((r) => console.log(`  ${r.removable ? "removable " : "KEEP      "} ${r.name}` +
  (r.removable ? "" : ` (has changes=${r.touched}, files=${r.files}, activities=${r.acts})`)));
if (process.argv.includes("--remove")) {
  const del = db.prepare("DELETE FROM projects WHERE id = ?");
  const n = db.transaction(() => report.filter((r) => r.removable).map((r) => del.run(r.id)).length)();
  console.log(`removed ${n} untouched demonstration projects`);
}
