#!/usr/bin/env node
"use strict";

// Copies DATA_DIR and UPLOADS_DIR into a single timestamped snapshot folder - the backup step
// before any migration or risky operation, and (together with verify_migration.js) the "export"
// half of a migration: since storage is just a JSON file plus plain upload files, moving data
// anywhere is literally copying these two folders (see docs/RAILWAY_DEPLOYMENT.md#migration).
//
// This never touches the live DATA_DIR/UPLOADS_DIR - it only reads from them (store.js's own
// atomic-write .bak files are a separate, always-on safety net for in-place writes; this is for
// point-in-time snapshots you keep elsewhere, e.g. before a migration or a schema change).
//
// Usage:
//   node scripts/snapshot_data.js <data_dir> <uploads_dir> <destination_dir>
//
// destination_dir is created as <destination_dir>/<ISO-timestamp>/{data,uploads}.

const fs = require("fs");
const path = require("path");

function copyRecursive(src, dst) {
  if (!fs.existsSync(src)) return;
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) copyRecursive(s, d);
    else fs.copyFileSync(s, d);
  }
}

function main() {
  const [dataDir, uploadsDir, destRoot] = process.argv.slice(2);
  if (!dataDir || !uploadsDir || !destRoot) {
    console.error("usage: node scripts/snapshot_data.js <data_dir> <uploads_dir> <destination_dir>");
    process.exit(2);
  }
  if (!fs.existsSync(path.join(dataDir, "shturman.json"))) {
    console.error(`no shturman.json found in ${dataDir} - is this the right DATA_DIR?`);
    process.exit(1);
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dest = path.join(destRoot, stamp);
  console.log(`snapshotting ${dataDir} -> ${path.join(dest, "data")}`);
  copyRecursive(dataDir, path.join(dest, "data"));
  console.log(`snapshotting ${uploadsDir} -> ${path.join(dest, "uploads")}`);
  copyRecursive(uploadsDir, path.join(dest, "uploads"));
  console.log(`done: ${dest}`);
  console.log("Verify with: node scripts/verify_migration.js " +
    `${dataDir} ${uploadsDir} ${path.join(dest, "data")} ${path.join(dest, "uploads")}`);
}

main();
