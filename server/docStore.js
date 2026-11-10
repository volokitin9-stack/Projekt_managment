"use strict";

// Generic JSON-document collection, modeled directly on how the original
// Artifact's ArtifactData store worked: an arbitrary-shaped JSON object per
// row, addressed by id, with a `version` that must be echoed back on write
// (optimistic concurrency) instead of a blind overwrite. `tasks` and
// `globals` are both exactly this with no fixed schema, which is why one
// implementation serves both. Storage: server/store.js (JSON file).

const { v4: uuidv4 } = require("uuid");
const store = require("./store");

function nowIso() {
  return new Date().toISOString();
}

function createDocStore(collection) {
  function rowToDoc(row) {
    return Object.assign({}, JSON.parse(JSON.stringify(row.data)), { id: row.id, version: row.version });
  }
  const find = (s, id) => s.docs.find((d) => d.collection === collection && d.id === id);

  return {
    list() {
      return store.read((s) => s.docs.filter((d) => d.collection === collection)
        .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at))).map(rowToDoc));
    },
    get(id) {
      return store.read((s) => {
        const row = find(s, id);
        return row ? rowToDoc(row) : null;
      });
    },
    create(data) {
      const id = (data && data.id) || uuidv4();
      const ts = nowIso();
      const clean = JSON.parse(JSON.stringify(data || {}));
      delete clean.id;
      delete clean.version;
      store.tx((s) => {
        if (find(s, id)) throw Object.assign(new Error("duplicate id"), { status: 409 });
        s.docs.push({ collection, id, data: clean, version: 1, created_at: ts, updated_at: ts });
      });
      return this.get(id);
    },
    update(id, patch) {
      const result = store.read((s) => (find(s, id) ? null : { error: "not_found" }));
      if (result) return result;
      try {
        store.tx((s) => {
          const row = find(s, id);
          const expectedVersion = patch && patch.version != null ? Number(patch.version) : row.version;
          if (expectedVersion !== row.version) throw Object.assign(new Error("version_conflict"), { code: "version_conflict" });
          const clean = JSON.parse(JSON.stringify(patch || {}));
          delete clean.id;
          delete clean.version;
          row.data = Object.assign({}, row.data, clean);
          row.version += 1;
          row.updated_at = nowIso();
        });
      } catch (e) {
        if (e.code === "version_conflict") return { error: "version_conflict", current: this.get(id) };
        throw e;
      }
      return { doc: this.get(id) };
    },
    delete(id) {
      return store.tx((s) => {
        const i = s.docs.findIndex((d) => d.collection === collection && d.id === id);
        if (i === -1) return false;
        s.docs.splice(i, 1);
        return true;
      });
    }
  };
}

module.exports = { createDocStore };
