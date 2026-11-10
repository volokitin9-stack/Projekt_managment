"use strict";

const express = require("express");
const store = require("../store");

const router = express.Router();

// Stages are rows in the database, not a hardcoded list, so they can be
// reordered/renamed/extended later without a code change or migration.
router.get("/", (req, res) => {
  const stages = store.read((s) => s.stages.slice().sort((a, b) => a.sort_order - b.sort_order)
    .map((r) => ({ key: r.key, label: r.label, sort_order: r.sort_order })));
  res.json(stages);
});

module.exports = router;
