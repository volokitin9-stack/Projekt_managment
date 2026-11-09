"use strict";

// Review queue: items that must be confirmed by a person before they become
// (or attach to) a project - unmatched photos, ambiguous planning records,
// projects created without a stable identifier.

const { v4: uuidv4 } = require("uuid");
const db = require("./db");

function nowIso() {
  return new Date().toISOString();
}

function toApi(r) {
  return {
    id: r.id,
    kind: r.kind,
    payload: JSON.parse(r.payload),
    reason: r.reason,
    source: r.source || null,
    dedupe_key: r.dedupe_key || null,
    created_at: r.created_at,
    updated_at: r.updated_at || r.created_at,
    resolved: !!r.resolved,
    resolved_at: r.resolved_at || null,
    resolution: r.resolution || null
  };
}

/** Insert, or update the open item with the same dedupe key (idempotent). */
function upsert({ kind, payload, reason = null, source = null, dedupeKey = null }) {
  if (dedupeKey) {
    const existing = db.prepare("SELECT * FROM review_queue WHERE dedupe_key = ?").get(dedupeKey);
    if (existing) {
      if (!existing.resolved) {
        db.prepare("UPDATE review_queue SET payload = ?, reason = ?, updated_at = ? WHERE id = ?")
          .run(JSON.stringify(payload), reason, nowIso(), existing.id);
      }
      return { id: existing.id, duplicate: true, resolved: !!existing.resolved };
    }
  }
  const id = uuidv4();
  const ts = nowIso();
  db.prepare("INSERT INTO review_queue (id, kind, payload, reason, created_at, dedupe_key, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
    .run(id, kind, JSON.stringify(payload), reason, ts, dedupeKey, source, ts);
  return { id, duplicate: false };
}

function list({ includeResolved = false, kind = null } = {}) {
  let rows = db.prepare("SELECT * FROM review_queue ORDER BY created_at ASC").all();
  if (!includeResolved) rows = rows.filter((r) => !r.resolved);
  if (kind) rows = rows.filter((r) => r.kind === kind);
  return rows.map(toApi);
}

function resolve(id, resolution) {
  const r = db.prepare("UPDATE review_queue SET resolved = 1, resolved_at = ?, resolution = ? WHERE id = ? AND resolved = 0")
    .run(nowIso(), resolution || "resolved", id);
  return r.changes > 0;
}

module.exports = { upsert, list, resolve };
