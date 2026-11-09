"use strict";

const express = require("express");
const reviewQueue = require("../reviewQueueModel");

const router = express.Router();

// GET /api/review-queue — open items (add ?all=1 to include resolved ones, ?kind=plan_record to filter).
router.get("/", (req, res) => {
  res.json(reviewQueue.list({ includeResolved: req.query.all === "1", kind: req.query.kind || null }));
});

// POST /api/review-queue/:id/resolve — mark an item as handled (nothing else is changed).
router.post("/:id/resolve", (req, res) => {
  const ok = reviewQueue.resolve(req.params.id, (req.body && req.body.resolution) || "resolved");
  if (!ok) return res.status(404).json({ error: "Not found or already resolved" });
  res.json({ id: req.params.id, resolved: true });
});

module.exports = router;
