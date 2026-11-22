"use strict";

// Permanent archive of the КО (construction-team) Telegram chat for the shturman-ko-analyst agent.
//
// Written by server/telegram/engineeringChannel.js at the very start of handling a message, BEFORE
// any analysis, so a message is never lost whatever happens next (AI disabled/unavailable, project
// not matched, an exception later on). Strictly read-only towards Telegram: the only Bot API call
// made here is getFile + the file download (reading), never anything addressed to a chat.
//
// Storage (both on the persistent DATA_DIR volume, /data on Railway):
//   * metadata - store.js collections ko_messages / ko_files, object ko_archive {last_seq}
//   * photo bytes - <KO_FILES_DIR> (default <DATA_DIR>/ko-files). Deliberately NOT under
//     UPLOADS_DIR: nothing in this directory is ever mounted as static files. Bytes are only served
//     by id through authenticated routes (server/routes/koAgent.js, server/routes/koDashboard.js).
//
// Idempotency:
//   * message - unique (chat_id, message_id); a second archive call is a no-op (Telegram redelivery,
//     on top of the webhook's own update_id ledger);
//   * photo - unique Telegram file_unique_id; the same picture forwarded again reuses the stored
//     file instead of saving a copy.
// Ordering: every newly archived message gets a monotonic `seq` (arrival order) - the agent's
// cursor. Reply chains: reply_to_message_id plus thread_root_id (root of the reply chain, resolved
// from already-archived messages).

const fs = require("fs");
const path = require("path");
const { v4: uuidv4 } = require("uuid");
const store = require("../store");
const botApi = require("../telegram/botApi");

const KO_FILES_DIR = path.resolve(process.env.KO_FILES_DIR || path.join(store.DATA_DIR, "ko-files"));
const MAX_FILE_BYTES = Number(process.env.KO_MAX_FILE_BYTES || 10 * 1024 * 1024);
const ALLOWED = { "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp" };

function nowIso() {
  return new Date().toISOString();
}

/** Detects the real image type from the first bytes - the declared type/extension is never trusted. */
function sniff(buffer) {
  if (!buffer || buffer.length < 12) return null;
  if (buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return "image/jpeg";
  if (buffer.slice(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return "image/png";
  if (buffer.slice(0, 4).toString("latin1") === "RIFF" && buffer.slice(8, 12).toString("latin1") === "WEBP") return "image/webp";
  return null;
}

function displayName(from) {
  if (!from) return null;
  const n = [from.first_name, from.last_name].filter(Boolean).join(" ").trim();
  return n || from.username || null;
}

/** Image attachments of a message: the largest size of a photo, or an image sent as a document. */
function attachmentsOf(message) {
  const out = [];
  const sizes = Array.isArray(message.photo) ? message.photo : [];
  if (sizes.length) {
    const p = sizes[sizes.length - 1];   // Telegram lists sizes smallest -> largest
    out.push({ telegram_file_id: p.file_id, file_unique_id: p.file_unique_id || p.file_id, declared_mime: "image/jpeg",
      declared_size: p.file_size || null, width: p.width || null, height: p.height || null, kind: "photo" });
  }
  const d = message.document;
  if (d && d.file_id) {
    out.push({ telegram_file_id: d.file_id, file_unique_id: d.file_unique_id || d.file_id, declared_mime: d.mime_type || null,
      declared_size: d.file_size || null, width: null, height: null, kind: "document", file_name: d.file_name || null });
  }
  return out;
}

function toMessageApi(m, filesById) {
  return {
    seq: m.seq, chat_id: m.chat_id, message_id: m.message_id, date: m.date, from: m.from,
    text: m.text, caption: m.caption, reply_to_message_id: m.reply_to_message_id, thread_root_id: m.thread_root_id,
    message_thread_id: m.message_thread_id, media_group_id: m.media_group_id, forwarded: m.forwarded,
    content_type: m.content_type, archived_at: m.archived_at,
    photos: (m.files || []).map((fid) => {
      const f = filesById.get(fid);
      return f ? { file_id: f.id, status: f.status, mime_type: f.mime_type, size: f.size, width: f.width, height: f.height,
        kind: f.kind, error: f.status === "stored" ? null : f.error } : { file_id: fid, status: "missing" };
    })
  };
}

/** Insert the message record (idempotent). Returns {created, record}. */
function recordMessage(message, ctx = {}) {
  const chatId = String((message.chat || {}).id);
  const messageId = Number(message.message_id);
  return store.tx((s) => {
    const existing = s.ko_messages.find((m) => m.chat_id === chatId && m.message_id === messageId);
    if (existing) return { created: false, record: existing };
    const replyTo = message.reply_to_message ? Number(message.reply_to_message.message_id) : null;
    let root = null;
    if (replyTo) {
      const parent = s.ko_messages.find((m) => m.chat_id === chatId && m.message_id === replyTo);
      root = parent ? (parent.thread_root_id || parent.message_id) : replyTo;
    }
    const seq = (Number(s.ko_archive.last_seq) || 0) + 1;
    s.ko_archive.last_seq = seq;
    const from = message.from || {};
    const att = attachmentsOf(message);
    const record = {
      seq, chat_id: chatId, message_id: messageId,
      date: message.date ? new Date(Number(message.date) * 1000).toISOString() : null,
      from: { id: from.id != null ? String(from.id) : null, name: displayName(from), username: from.username || null, is_bot: !!from.is_bot },
      text: typeof message.text === "string" ? message.text : null,
      caption: typeof message.caption === "string" ? message.caption : null,
      reply_to_message_id: replyTo, thread_root_id: root,
      message_thread_id: message.message_thread_id != null ? Number(message.message_thread_id) : null,
      media_group_id: message.media_group_id ? String(message.media_group_id) : null,
      forwarded: !!(message.forward_origin || message.forward_from || message.forward_from_chat || message.forward_date),
      content_type: message.text ? "text" : att.length ? "photo" : "other",
      update_id: ctx.updateId != null ? Number(ctx.updateId) : null,
      archived_at: nowIso(), files: [], pending_files: att
    };
    s.ko_messages.push(record);
    return { created: true, record };
  });
}

function rejectFile(att, status, error) {
  return Object.assign({}, att, { status, error });
}

async function fetchAttachment(att) {
  if (att.declared_mime && !ALLOWED[att.declared_mime]) {
    return rejectFile(att, "rejected", `тип файла не поддерживается: ${att.declared_mime}`);
  }
  if (att.declared_size && att.declared_size > MAX_FILE_BYTES) {
    return rejectFile(att, "rejected", `файл больше лимита ${MAX_FILE_BYTES} байт`);
  }
  let file;
  try {
    file = await botApi.downloadFile(att.telegram_file_id);
  } catch (e) {
    return rejectFile(att, "download_failed", e.code || "download_error");
  }
  if (!file.buffer || file.buffer.length > MAX_FILE_BYTES) return rejectFile(att, "rejected", `файл больше лимита ${MAX_FILE_BYTES} байт`);
  const mime = sniff(file.buffer);
  if (!mime) return rejectFile(att, "rejected", "содержимое не является изображением JPEG/PNG/WebP");
  const month = nowIso().slice(0, 7);
  const id = uuidv4();
  const storageKey = path.posix.join(month, `${id}${ALLOWED[mime]}`);
  const abs = path.join(KO_FILES_DIR, storageKey);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  const tmp = `${abs}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, file.buffer, { mode: 0o600 });
  fs.renameSync(tmp, abs);
  return Object.assign({}, att, { id, status: "stored", error: null, mime_type: mime, size: file.buffer.length, storage_key: storageKey,
    sha256: require("crypto").createHash("sha256").update(file.buffer).digest("hex") });
}

/** Download and store the message's pending attachments (each at most once, deduplicated by
 *  file_unique_id). Never throws - a failed download is recorded on the file entry. */
async function storeAttachments(record) {
  const chatId = record.chat_id;
  const messageId = record.message_id;
  const pending = (record.pending_files || []).slice();
  for (const att of pending) {
    const known = store.read((s) => s.ko_files.find((f) => f.file_unique_id === att.file_unique_id && f.status === "stored"));
    let entry;
    if (known) {
      entry = { reuse: known.id };
    } else {
      const r = await fetchAttachment(att);
      entry = { file: Object.assign({ id: r.id || uuidv4(), chat_id: chatId, message_id: messageId, received_at: nowIso() }, r) };
      delete entry.file.telegram_file_id;   // not needed after download; keep the record minimal
      delete entry.file.declared_mime;
    }
    store.tx((s) => {
      const m = s.ko_messages.find((x) => x.chat_id === chatId && x.message_id === messageId);
      if (!m) return;
      let fid;
      if (entry.reuse) {
        fid = entry.reuse;
      } else {
        const dup = s.ko_files.find((f) => f.file_unique_id === entry.file.file_unique_id && f.status === "stored");
        if (dup && entry.file.status === "stored") {
          // a concurrent delivery stored the same picture first - drop our extra copy
          try { fs.unlinkSync(path.join(KO_FILES_DIR, entry.file.storage_key)); } catch (e) { /* already gone */ }
          fid = dup.id;
        } else {
          // keep the Telegram file id only for a failed download, so it can be retried later
          if (entry.file.status === "download_failed") entry.file.telegram_file_id = att.telegram_file_id;
          s.ko_files.push(entry.file);
          fid = entry.file.id;
        }
      }
      if (!m.files.includes(fid)) m.files.push(fid);
      m.pending_files = (m.pending_files || []).filter((p) => p.file_unique_id !== att.file_unique_id);
    });
  }
}

/** Archive one КО chat message (idempotent). Returns {created, seq}. */
async function archiveMessage(message, ctx = {}) {
  const { created, record } = recordMessage(message, ctx);
  if ((record.pending_files || []).length) await storeAttachments(record);
  return { created, seq: record.seq };
}

function filesIndex(s) {
  return new Map(s.ko_files.map((f) => [f.id, f]));
}

/** Messages with seq > after, ascending, at most `limit`. */
function listMessages({ after = 0, limit = 200 } = {}) {
  const lim = Math.max(1, Math.min(Number(limit) || 200, 500));
  const a = Number(after) || 0;
  return store.read((s) => {
    const idx = filesIndex(s);
    const all = s.ko_messages.filter((m) => m.seq > a).sort((x, y) => x.seq - y.seq);
    const page = all.slice(0, lim).map((m) => toMessageApi(m, idx));
    return { messages: page, next_after: page.length ? page[page.length - 1].seq : a, has_more: all.length > lim,
      last_seq: Number(s.ko_archive.last_seq) || 0 };
  });
}

function getMessage(chatId, messageId) {
  return store.read((s) => s.ko_messages.find((m) => m.chat_id === String(chatId) && m.message_id === Number(messageId)) || null);
}

function getFileMeta(id) {
  return store.read((s) => s.ko_files.find((f) => f.id === String(id)) || null);
}

/** Absolute path of a stored file, or null. Resolved strictly inside KO_FILES_DIR. */
function filePath(meta) {
  if (!meta || meta.status !== "stored" || !meta.storage_key) return null;
  const abs = path.resolve(KO_FILES_DIR, meta.storage_key);
  if (!abs.startsWith(KO_FILES_DIR + path.sep)) return null;
  return fs.existsSync(abs) ? abs : null;
}

function readFile(id) {
  const meta = getFileMeta(id);
  const abs = filePath(meta);
  if (!abs) return null;
  return { meta, buffer: fs.readFileSync(abs) };
}

/** The analyst's processing cursor (seq of the last message whose analysis was fully submitted).
 *  Kept server-side because each scheduled cloud run starts in a fresh container. Monotonic: it can
 *  only move forward and never past the archive. */
function getCursor() {
  return store.read((s) => ({ cursor: Number(s.ko_archive.analyst_cursor) || 0, last_seq: Number(s.ko_archive.last_seq) || 0,
    updated_at: s.ko_archive.analyst_cursor_at || null, run_id: s.ko_archive.analyst_run_id || null }));
}

function setCursor(value, runId) {
  const v = Number(value);
  if (!Number.isInteger(v) || v < 0) return { error: 400, message: '"cursor" must be a non-negative integer' };
  return store.tx((s) => {
    const cur = Number(s.ko_archive.analyst_cursor) || 0;
    const last = Number(s.ko_archive.last_seq) || 0;
    if (v > last) return { error: 409, message: `cursor ${v} is beyond the archive (last_seq ${last})` };
    if (v < cur) return { error: 409, message: `cursor can only move forward (current ${cur})` };
    s.ko_archive.analyst_cursor = v;
    s.ko_archive.analyst_cursor_at = new Date().toISOString();
    s.ko_archive.analyst_run_id = runId ? String(runId).slice(0, 100) : null;
    return { cursor: v, previous: cur, last_seq: last };
  });
}

function stats() {
  return store.read((s) => ({
    messages: s.ko_messages.length,
    last_seq: Number(s.ko_archive.last_seq) || 0,
    files_stored: s.ko_files.filter((f) => f.status === "stored").length,
    files_failed: s.ko_files.filter((f) => f.status !== "stored").length,
    last_archived_at: s.ko_messages.length ? s.ko_messages[s.ko_messages.length - 1].archived_at : null
  }));
}

module.exports = { archiveMessage, listMessages, getCursor, setCursor, getMessage, getFileMeta, filePath, readFile, stats, sniff,
  KO_FILES_DIR, MAX_FILE_BYTES, ALLOWED_MIME: Object.keys(ALLOWED) };
