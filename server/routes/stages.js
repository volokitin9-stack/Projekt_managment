"use strict";

const express = require("express");
const db = require("../db");

const router = express.Router();

// Stages are rows in the database, not a hardcoded list, so they can be
// reordered/renamed/extended later without a code change or migration.
router.get("/", (req, res) => {
  const stages = db.prepare("SELECT key, label, sort_order FROM stages ORDER BY sort_order ASC").all();
  res.json(stages);
});

module.exports = router;
