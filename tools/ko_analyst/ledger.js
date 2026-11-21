#!/usr/bin/env node
"use strict";

// Local, deterministic helper (validation + dedupe keys shared with the server: server/ko/rules.js) for the `shturman-ko-analyst` subagent. No network, no dependencies.
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

const rules = require("../../server/ko/rules");

const { GATED, link, dedupeKey, validate, evidenceKey, photoKey } = rules;

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
      const knownPhotos = new Set((prev.photos || []).map(photoKey));
      const addedPhotos = (p.photos || []).filter((x) => !knownPhotos.has(photoKey(x)));
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

module.exports = { link, dedupeKey, validate, merge, seen, projectIdentity: rules.projectIdentity };

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
