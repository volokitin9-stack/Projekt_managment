"use strict";

const express = require("express");
const { createDocStore } = require("../docStore");

/**
 * Builds a REST router for a generic JSON-document collection ("tasks" or
 * "globals" — the two collections the original Штурман board used).
 */
function buildDocRouter(collectionName) {
  const store = createDocStore(collectionName);
  const router = express.Router();

  router.get("/", (req, res) => {
    res.json(store.list());
  });

  router.get("/:id", (req, res) => {
    const doc = store.get(req.params.id);
    if (!doc) return res.status(404).json({ error: "Not found" });
    res.json(doc);
  });

  router.post("/", (req, res) => {
    if (!req.body || typeof req.body !== "object" || Array.isArray(req.body)) {
      return res.status(400).json({ error: "Request body must be a JSON object" });
    }
    const doc = store.create(req.body);
    res.status(201).json(doc);
  });

  router.patch("/:id", (req, res) => {
    const result = store.update(req.params.id, req.body || {});
    if (result.error === "not_found") return res.status(404).json({ error: "Not found" });
    if (result.error === "version_conflict") {
      return res.status(409).json({ error: "Document was modified by someone else since you last read it", current: result.current });
    }
    res.json(result.doc);
  });

  router.delete("/:id", (req, res) => {
    const ok = store.delete(req.params.id);
    if (!ok) return res.status(404).json({ error: "Not found" });
    res.status(204).end();
  });

  return router;
}

module.exports = { buildDocRouter };
