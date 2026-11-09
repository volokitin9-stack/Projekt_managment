"use strict";

const express = require("express");
const db = require("../db");

const router = express.Router();

// Read-only listing of items the agent couldn't confidently match to a
// project (unassigned images, projects created without a stable external
// id). There is no UI for this yet — see MIGRATION_REPORT.md — but the data
// is safely captured and inspectable via this endpoint or `sqlite3 data/app.sqlite`.
router.get("/", (req, res) => {
  const rows = db.prepare("SELECT * FROM review_queue WHERE resolved = 0 ORDER BY created_at ASC").all();
  res.json(
    rows.map((r) => ({
      id: r.id,
      kind: r.kind,
      payload: JSON.parse(r.payload),
      reason: r.reason,
      created_at: r.created_at
    }))
  );
});

module.exports = router;
