"use strict";

const express = require("express");
const projectsModel = require("../projectsModel");

const router = express.Router();

// GET /api/projects — list, for the dashboard. Optional filters mirror the
// board's own filter bar (make/model/product/engineer/priority/status/q),
// applied server-side as a convenience for future callers; the current
// frontend still filters client-side over the full list, which is fine at
// this project's scale (see MIGRATION_REPORT.md).
router.get("/", (req, res) => {
  let list = projectsModel.listProjects();
  const { make, model, product, engineer, priority, status, q } = req.query;
  if (make) list = list.filter((p) => p.manufacturer === make);
  if (model) list = list.filter((p) => p.vehicle_model === model);
  if (product) list = list.filter((p) => p.product_type === product);
  if (engineer) list = list.filter((p) => p.assigned_engineer === engineer);
  if (priority) list = list.filter((p) => p.priority === priority);
  if (status) list = list.filter((p) => p.status === status);
  if (q) {
    const needle = String(q).toLowerCase();
    list = list.filter((p) =>
      [p.manufacturer, p.vehicle_model, p.product_type, p.project_id].join(" ").toLowerCase().includes(needle)
    );
  }
  res.json(list);
});

router.get("/:id", (req, res) => {
  const project = projectsModel.getProject(req.params.id);
  if (!project) return res.status(404).json({ error: "Not found" });
  res.json(project);
});

module.exports = router;
