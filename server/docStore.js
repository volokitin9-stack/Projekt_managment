"use strict";

// Generic JSON-document collection, modeled directly on how the original
// Artifact's ArtifactData store worked: an arbitrary-shaped JSON object per
// row, addressed by id, with a `version` that must be echoed back on write
// (optimistic concurrency) instead of a blind overwrite. `tasks` and
// `globals` are both exactly this with no fixed schema, which is why one
// implementation serves both.

const { v4: uuidv4 } = require("uuid");
const db = require("./db");

function nowIso() {
  return new Date().toISOString();
}

function createDocStore(collection) {
  const listStmt = db.prepare("SELECT * FROM docs WHERE collection = ? ORDER BY created_at ASC");
  const getStmt = db.prepare("SELECT * FROM docs WHERE collection = ? AND id = ?");
  const insertStmt = db.prepare(
    "INSERT INTO docs (collection, id, data, version, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)"
  );
  const updateStmt = db.prepare(
    "UPDATE docs SET data = ?, version = version + 1, updated_at = ? WHERE collection = ? AND id = ? AND version = ?"
  );
  const deleteStmt = db.prepare("DELETE FROM docs WHERE collection = ? AND id = ?");

  function rowToDoc(row) {
    const data = JSON.parse(row.data);
    return Object.assign({}, data, { id: row.id, version: row.version });
  }

  return {
    list() {
      return listStmt.all(collection).map(rowToDoc);
    },
    get(id) {
      const row = getStmt.get(collection, id);
      return row ? rowToDoc(row) : null;
    },
    create(data) {
      const id = (data && data.id) || uuidv4();
      const ts = nowIso();
      const clean = Object.assign({}, data);
      delete clean.id;
      delete clean.version;
      insertStmt.run(collection, id, JSON.stringify(clean), ts, ts);
      return this.get(id);
    },
    update(id, patch) {
      const row = getStmt.get(collection, id);
      if (!row) return { error: "not_found" };
      const expectedVersion = patch && patch.version != null ? Number(patch.version) : row.version;
      const current = JSON.parse(row.data);
      const clean = Object.assign({}, patch);
      delete clean.id;
      delete clean.version;
      const merged = Object.assign({}, current, clean);
      const result = updateStmt.run(JSON.stringify(merged), nowIso(), collection, id, expectedVersion);
      if (result.changes === 0) {
        return { error: "version_conflict", current: rowToDoc(getStmt.get(collection, id)) };
      }
      return { doc: this.get(id) };
    },
    delete(id) {
      return deleteStmt.run(collection, id).changes > 0;
    }
  };
}

module.exports = { createDocStore };
