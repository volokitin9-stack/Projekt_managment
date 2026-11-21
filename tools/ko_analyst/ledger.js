#!/usr/bin/env node
"use strict";

// Local, deterministic helper for the `shturman-ko-analyst` subagent. No network, no dependencies.
// The model does the reading/understanding; this script does the parts that must be exact:
// validation of proposals, stable dedupe keys, idempotent merge, cursor, Telegram message links.
//
//   node tools/ko_analyst/ledger.js link <chat_id> <message_id>
//   node tools/ko_analyst/ledger.js seen <chat_id> <message_id>      -> prints "seen" | "new"
//   node tools/ko_analyst/ledger.js merge <proposals.json>           -> writes out/proposals.new.json
//   node tools/ko_analyst/ledger.js status
//
// Working dir: $KO_ANALYST_DIR or ./.ko-analyst (gitignored). Files: ledger.json (dedupe_key ->
// proposal incl. merged evidence), state.json ({last_seq, processed:{"chat:msg":iso}}).
//
// Idempotency model: the same fact seen again (re-run, overlapping export, Telegram redelivery)
// yields the same dedupe_key, so it is either dropped (nothing new) or re-emitted with the SAME key
// and the union of evidence - the server's review-queue upsert by dedupe_key then updates the one
// existing item instead of creating a second one. The local ledger is a convenience for offline
// runs; in online mode the server-side dedupe_key is the source of truth (cloud containers are
// ephemeral).

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const KINDS = ["stage_change", "assignee_change", "deadline_change", "work_done", "problem", "new_task",
  "photo_link", "project_match", "needs_clarification"];
const GATED = ["stage_change", "assignee_change", "deadline_change", "new_task", "photo_link", "project_match"];
const STATE_KINDS = ["stage_change", "assignee_change", "deadline_change"];
const STAGES = ["scanning", "development", "prototype", "measurement", "documentation"];
const CONFIDENCE = ["high", "medium", "low"];

function dir() {
  return path.resolve(process.env.KO_ANALYST_DIR || ".ko-analyst");
}
function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch (e) { if (e.code === "ENOENT") return fallback; throw e; }
}
function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
  fs.renameSync(tmp, file);
}

function norm(v) {
  if (v == null) return "";
  return String(v).toLowerCase().replace(/ё/g, "е").replace(/[\s.\-_/]+/g, " ").trim();
}
function normArticle(v) {
  return v == null ? "" : String(v).toLowerCase().replace(/[^0-9a-zа-я]/g, "");
}

/** Public link for a supergroup message (chat_id -100XXXXXXXXXX). Other chats have no t.me link. */
function link(chatId, messageId) {
  const c = String(chatId);
  if (/^-100\d+$/.test(c)) return `https://t.me/c/${c.slice(4)}/${Number(messageId)}`;
  return `tg-message:${c}:${Number(messageId)}`;
}

function projectIdentity(ref = {}) {
  if (ref.project_id) return `id:${ref.project_id}`;
  if (ref.external_project_id) return `ext:${norm(ref.external_project_id)}`;
  if (ref.article_number) return `art:${normArticle(ref.article_number)}`;
  return `mmp:${[ref.manufacturer, ref.vehicle_model, ref.product_type].map(norm).join("|")}`;
}
function evidenceKey(e) {
  return `${String(e.chat_id)}:${Number(e.message_id)}`;
}

function dedupeKey(p) {
  const proj = projectIdentity(p.project_ref);
  let basis;
  if (STATE_KINDS.includes(p.kind)) {
    basis = [p.kind, proj, norm((p.change || {}).field), norm((p.change || {}).to)];
  } else if (p.kind === "new_task") {
    basis = [p.kind, proj, norm((p.change || {}).title || p.summary)];
  } else if (p.kind === "photo_link") {
    const ph = (p.photos || []).map((x) => x.file_id || `${x.chat_id || ""}:${x.message_id}`).sort();
    basis = [p.kind, proj, ph.join(",")];
  } else {
    basis = [p.kind, proj, (p.evidence || []).map(evidenceKey).sort().join(",")];
  }
  return "ko:v1:" + crypto.createHash("sha256").update(basis.join("␟")).digest("hex").slice(0, 32);
}

function validate(p, i) {
  const errs = [];
  const at = `#${i}`;
  if (!p || typeof p !== "object") return [`${at}: не объект`];
  if (!KINDS.includes(p.kind)) errs.push(`${at}: kind должен быть одним из ${KINDS.join(", ")}`);
  if (!CONFIDENCE.includes(p.confidence)) errs.push(`${at}: confidence = high|medium|low`);
  if (p.confidence === "low" && p.kind !== "needs_clarification") errs.push(`${at}: low-уверенность допустима только для needs_clarification`);
  if (!p.summary || typeof p.summary !== "string") errs.push(`${at}: нужен summary`);
  if (!p.rationale || typeof p.rationale !== "string") errs.push(`${at}: нужно обоснование rationale`);
  if (!Array.isArray(p.evidence) || !p.evidence.length) errs.push(`${at}: нужен хотя бы один evidence`);
  (p.evidence || []).forEach((e, j) => {
    if (!e || e.chat_id == null || !Number.isFinite(Number(e.message_id))) errs.push(`${at}.evidence[${j}]: нужны chat_id и message_id`);
    else if (!e.quote) errs.push(`${at}.evidence[${j}]: нужна цитата quote`);
  });
  if (p.kind !== "needs_clarification") {
    const r = p.project_ref || {};
    const hasId = r.project_id || r.external_project_id || r.article_number || (r.manufacturer && r.vehicle_model);
    if (!hasId) errs.push(`${at}: project_ref должен однозначно указывать проект`);
  }
  if (STATE_KINDS.includes(p.kind)) {
    const c = p.change || {};
    if (!c.field || c.to == null) errs.push(`${at}: change.field и change.to обязательны`);
    if (p.kind === "stage_change" && !STAGES.includes(c.to)) errs.push(`${at}: change.to должен быть этапом ${STAGES.join("|")}`);
    if (p.kind === "stage_change" && c.from && c.from === c.to) errs.push(`${at}: этап не меняется`);
    if (p.kind === "deadline_change" && !/^\d{4}-\d{2}-\d{2}$/.test(String(c.to))) errs.push(`${at}: срок в формате YYYY-MM-DD`);
  }
  if (p.kind === "photo_link" && !(Array.isArray(p.photos) && p.photos.length)) errs.push(`${at}: photo_link без photos`);
  return errs;
}

function merge(file) {
  const input = readJson(path.resolve(file), null);
  const list = Array.isArray(input) ? input : (input && input.proposals) || null;
  if (!list) throw new Error(`${file}: ожидается массив предложений или {proposals:[...]}`);
  const errors = list.flatMap(validate);
  if (errors.length) return { ok: false, errors };

  const ledgerFile = path.join(dir(), "ledger.json");
  const stateFile = path.join(dir(), "state.json");
  const ledger = readJson(ledgerFile, { version: 1, proposals: {} });
  const state = readJson(stateFile, { version: 1, last_seq: 0, processed: {} });
  const now = new Date().toISOString();
  const out = [];
  const stats = { input: list.length, new: 0, updated: 0, duplicate: 0 };

  for (const raw of list) {
    const p = JSON.parse(JSON.stringify(raw));
    if (GATED.includes(p.kind)) p.requires_approval = true;
    p.evidence = p.evidence.map((e) => Object.assign({}, e, { message_id: Number(e.message_id), link: e.link || link(e.chat_id, e.message_id) }));
    p.dedupe_key = dedupeKey(p);
    const prev = ledger.proposals[p.dedupe_key];
    if (!prev) {
      ledger.proposals[p.dedupe_key] = Object.assign({}, p, { first_seen_at: now, updated_at: now });
      out.push(p);
      stats.new += 1;
    } else {
      const known = new Set(prev.evidence.map(evidenceKey));
      const added = p.evidence.filter((e) => !known.has(evidenceKey(e)));
      const knownPhotos = new Set((prev.photos || []).map((x) => x.file_id || `${x.chat_id}:${x.message_id}`));
      const addedPhotos = (p.photos || []).filter((x) => !knownPhotos.has(x.file_id || `${x.chat_id}:${x.message_id}`));
      if (!added.length && !addedPhotos.length) { stats.duplicate += 1; continue; }
      prev.evidence = prev.evidence.concat(added);
      if (addedPhotos.length) prev.photos = (prev.photos || []).concat(addedPhotos);
      prev.updated_at = now;
      out.push(Object.assign({}, prev));
      stats.updated += 1;
    }
    p.evidence.forEach((e) => { state.processed[evidenceKey(e)] = state.processed[evidenceKey(e)] || now; });
  }
  writeJson(ledgerFile, ledger);
  writeJson(stateFile, state);
  writeJson(path.join(dir(), "out", "proposals.new.json"), { generated_at: now, proposals: out });
  return { ok: true, stats, written: path.join(dir(), "out", "proposals.new.json") };
}

function seen(chatId, messageId) {
  const state = readJson(path.join(dir(), "state.json"), { processed: {} });
  return state.processed[`${String(chatId)}:${Number(messageId)}`] ? "seen" : "new";
}

module.exports = { link, dedupeKey, validate, merge, seen, projectIdentity };

if (require.main === module) {
  const [cmd, a, b] = process.argv.slice(2);
  try {
    if (cmd === "link" && a && b) console.log(link(a, b));
    else if (cmd === "seen" && a && b) console.log(seen(a, b));
    else if (cmd === "merge" && a) {
      const r = merge(a);
      console.log(JSON.stringify(r, null, 2));
      process.exit(r.ok ? 0 : 1);
    } else if (cmd === "status") {
      const ledger = readJson(path.join(dir(), "ledger.json"), { proposals: {} });
      const state = readJson(path.join(dir(), "state.json"), { last_seq: 0, processed: {} });
      console.log(JSON.stringify({ proposals: Object.keys(ledger.proposals).length, last_seq: state.last_seq,
        messages_processed: Object.keys(state.processed).length }, null, 2));
    } else {
      console.error("usage: ledger.js link|seen <chat_id> <message_id> | merge <proposals.json> | status");
      process.exit(2);
    }
  } catch (e) {
    console.error(`ledger: ${e.message}`);
    process.exit(1);
  }
}
