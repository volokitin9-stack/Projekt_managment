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

// ---- message classification -------------------------------------------------------------------
//
// Every archived message gets a `content_type` (its kind) and, for media, a `media` list with the
// Telegram metadata (file_id, file_unique_id, MIME, size, file name, duration, ...). Only images are
// downloaded (a photo, or an image sent as a document - the existing photo pipeline into
// KO_FILES_DIR). Video, voice, audio, other documents, stickers and animations are kept as metadata
// only (`download: "metadata_only"`), so they can be fetched later by a separate, explicit step.
// Records archived before this change keep their original fields (content_type text|photo|other);
// nothing already stored is rewritten.

const KINDS = ["text", "photo", "video", "document", "voice", "audio", "sticker", "animation", "service", "other"];
const SERVICE_EVENTS = ["new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo",
  "group_chat_created", "supergroup_chat_created", "channel_chat_created", "migrate_to_chat_id", "migrate_from_chat_id",
  "pinned_message", "message_auto_delete_timer_changed", "video_chat_scheduled", "video_chat_started", "video_chat_ended",
  "video_chat_participants_invited", "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
  "forum_topic_reopened", "general_forum_topic_hidden", "general_forum_topic_unhidden", "write_access_allowed",
  "users_shared", "chat_shared", "boost_added", "chat_background_set", "proximity_alert_triggered"];
const OTHER_TYPES = ["poll", "location", "venue", "contact", "dice", "game", "story", "invoice", "giveaway", "paid_media"];
const EXCERPT = 500;

function num(v) {
  return v == null || v === "" || Number.isNaN(Number(v)) ? null : Number(v);
}

/** Telegram metadata of one file-bearing object. Keeps file_id (needed for a later download) - it
 *  is stored server-side only and never returned by the API (see mediaToApi). */
function fileMeta(kind, o, extra = {}) {
  return Object.assign({
    kind,
    file_id: o.file_id || null,
    file_unique_id: o.file_unique_id || o.file_id || null,
    mime_type: o.mime_type || null,
    file_size: num(o.file_size),
    file_name: o.file_name || null,
    duration: num(o.duration),
    width: num(o.width),
    height: num(o.height)
  }, extra);
}

function serviceOf(message) {
  const event = SERVICE_EVENTS.find((k) => message[k] !== undefined);
  if (!event) return null;
  const v = message[event];
  const member = (u) => (u ? { id: u.id != null ? String(u.id) : null, is_bot: !!u.is_bot } : null);
  let details = {};
  if (event === "new_chat_members") details = { members: (Array.isArray(v) ? v : []).map(member) };
  else if (event === "left_chat_member") details = { member: member(v) };
  else if (event === "new_chat_title") details = { title: String(v).slice(0, 200) };
  else if (event === "migrate_to_chat_id" || event === "migrate_from_chat_id") details = { chat_id: String(v) };
  else if (event === "pinned_message") details = { message_id: num((v || {}).message_id) };
  else if (event === "message_auto_delete_timer_changed") details = { seconds: num((v || {}).message_auto_delete_time) };
  return { event, details };
}

/** {kind, media[], service, other_type}. Order matters: Telegram sends `animation` together with
 *  `document` for GIFs, and a photo/document may carry a caption. */
function classify(message) {
  const media = [];
  let kind;
  const sizes = Array.isArray(message.photo) ? message.photo : [];
  if (sizes.length) {
    const p = sizes[sizes.length - 1];   // Telegram lists sizes smallest -> largest
    kind = "photo";
    media.push(fileMeta("photo", p, { mime_type: "image/jpeg", download: "image" }));
  } else if (message.animation) {
    kind = "animation";
    media.push(fileMeta("animation", message.animation, { download: "metadata_only" }));
  } else if (message.video) {
    kind = "video";
    media.push(fileMeta("video", message.video, { download: "metadata_only" }));
  } else if (message.video_note) {
    kind = "video";
    const v = message.video_note;
    media.push(fileMeta("video_note", v, { mime_type: v.mime_type || "video/mp4", width: num(v.length), height: num(v.length),
      download: "metadata_only" }));
  } else if (message.voice) {
    kind = "voice";
    media.push(fileMeta("voice", message.voice, { download: "metadata_only" }));
  } else if (message.audio) {
    kind = "audio";
    const a = message.audio;
    media.push(fileMeta("audio", a, { performer: a.performer || null, title: a.title || null, download: "metadata_only" }));
  } else if (message.sticker) {
    kind = "sticker";
    const st = message.sticker;
    media.push(fileMeta("sticker", st, {
      mime_type: st.is_video ? "video/webm" : st.is_animated ? "application/x-tgsticker" : "image/webp",
      emoji: st.emoji || null, set_name: st.set_name || null, sticker_type: st.type || null,
      is_animated: !!st.is_animated, is_video: !!st.is_video, download: "metadata_only" }));
  } else if (message.document) {
    kind = "document";
    const d = message.document;
    // An image sent "as a file" is a photo for the КО (uncompressed) - same pipeline as before.
    media.push(fileMeta("document", d, { download: ALLOWED[d.mime_type] ? "image" : "metadata_only" }));
  }
  const service = media.length ? null : serviceOf(message);
  let otherType = null;
  if (!kind) {
    if (typeof message.text === "string") kind = "text";
    else if (service) kind = "service";
    else {
      kind = "other";
      otherType = OTHER_TYPES.find((k) => message[k] !== undefined) || null;
    }
  }
  return { kind, media, service, otherType };
}

/** Minimal context of the message being replied to, kept only when that message is not archived
 *  itself (e.g. it predates the bot joining) - so a reply is still understandable. */
function replyContext(parent) {
  if (!parent) return null;
  const c = classify(parent);
  const body = typeof parent.text === "string" ? parent.text : typeof parent.caption === "string" ? parent.caption : null;
  const f = parent.from || {};
  return { message_id: num(parent.message_id), date: parent.date ? new Date(Number(parent.date) * 1000).toISOString() : null,
    from: { id: f.id != null ? String(f.id) : null, name: displayName(f), is_bot: !!f.is_bot },
    kind: c.kind, text_excerpt: body ? body.slice(0, EXCERPT) : null,
    media: c.media.map((x) => ({ kind: x.kind, mime_type: x.mime_type, file_name: x.file_name })) };
}

function forwardOf(message) {
  const o = message.forward_origin;
  if (o) return { type: o.type || null, date: o.date ? new Date(Number(o.date) * 1000).toISOString() : null };
  if (message.forward_date || message.forward_from || message.forward_from_chat) {
    return { type: message.forward_from_chat ? "chat" : "user",
      date: message.forward_date ? new Date(Number(message.forward_date) * 1000).toISOString() : null };
  }
  return null;
}

/** API view of a media item: no Telegram file_id; download state resolved from ko_files. */
function mediaToApi(x, filesByUnique) {
  const out = { kind: x.kind, file_unique_id: x.file_unique_id, has_file_id: !!x.file_id, mime_type: x.mime_type,
    file_size: x.file_size, file_name: x.file_name, duration: x.duration, width: x.width, height: x.height };
  ["performer", "title", "emoji", "set_name", "sticker_type", "is_animated", "is_video"].forEach((k) => {
    if (x[k] !== undefined) out[k] = x[k];
  });
  if (x.download === "image") {
    const f = filesByUnique.get(x.file_unique_id);
    out.download = f ? f.status : "pending";
    out.archive_file_id = f && f.status === "stored" ? f.id : null;
  } else {
    out.download = "metadata_only";
    out.archive_file_id = null;
  }
  return out;
}

function toMessageApi(m, filesById, filesByUnique) {
  return {
    seq: m.seq, chat_id: m.chat_id, message_id: m.message_id, date: m.date, from: m.from,
    text: m.text, caption: m.caption, reply_to_message_id: m.reply_to_message_id, thread_root_id: m.thread_root_id,
    message_thread_id: m.message_thread_id, media_group_id: m.media_group_id, forwarded: m.forwarded,
    content_type: m.content_type, archived_at: m.archived_at,
    // fields added with message-type archiving; null/[] on records archived before it
    service: m.service || null, other_type: m.other_type || null, forward: m.forward || null,
    reply_context: m.reply_context || null, quote: m.quote || null, external_reply: m.external_reply || null,
    media: (m.media || []).map((x) => mediaToApi(x, filesByUnique)),
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
    let parentArchived = false;
    if (replyTo) {
      const parent = s.ko_messages.find((m) => m.chat_id === chatId && m.message_id === replyTo);
      parentArchived = !!parent;
      root = parent ? (parent.thread_root_id || parent.message_id) : replyTo;
    }
    const seq = (Number(s.ko_archive.last_seq) || 0) + 1;
    s.ko_archive.last_seq = seq;
    const from = message.from || {};
    const c = classify(message);
    // the existing image pipeline: what gets downloaded into KO_FILES_DIR
    const pending = c.media.filter((x) => x.download === "image").map((x) => ({
      telegram_file_id: x.file_id, file_unique_id: x.file_unique_id, declared_mime: x.mime_type,
      declared_size: x.file_size, width: x.width, height: x.height, kind: x.kind, file_name: x.file_name }));
    const record = {
      seq, chat_id: chatId, message_id: messageId,
      date: message.date ? new Date(Number(message.date) * 1000).toISOString() : null,
      from: { id: from.id != null ? String(from.id) : null, name: displayName(from), username: from.username || null, is_bot: !!from.is_bot },
      text: typeof message.text === "string" ? message.text : null,
      caption: typeof message.caption === "string" ? message.caption : null,
      reply_to_message_id: replyTo, thread_root_id: root,
      reply_context: replyTo && !parentArchived ? replyContext(message.reply_to_message) : null,
      quote: message.quote && typeof message.quote.text === "string" ? message.quote.text.slice(0, EXCERPT) : null,
      external_reply: message.external_reply ? { message_id: num(message.external_reply.message_id),
        chat_id: message.external_reply.chat ? String(message.external_reply.chat.id) : null } : null,
      message_thread_id: message.message_thread_id != null ? Number(message.message_thread_id) : null,
      media_group_id: message.media_group_id ? String(message.media_group_id) : null,
      forwarded: !!forwardOf(message), forward: forwardOf(message),
      content_type: c.kind, service: c.service, other_type: c.otherType, media: c.media,
      update_id: ctx.updateId != null ? Number(ctx.updateId) : null,
      archived_at: nowIso(), files: [], pending_files: pending
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
    const byUnique = new Map(s.ko_files.filter((f) => f.status === "stored").map((f) => [f.file_unique_id, f]));
    s.ko_files.filter((f) => f.status !== "stored").forEach((f) => { if (!byUnique.has(f.file_unique_id)) byUnique.set(f.file_unique_id, f); });
    const all = s.ko_messages.filter((m) => m.seq > a).sort((x, y) => x.seq - y.seq);
    const page = all.slice(0, lim).map((m) => toMessageApi(m, idx, byUnique));
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

function stats() {
  return store.read((s) => ({
    by_type: s.ko_messages.reduce((a, m) => { a[m.content_type] = (a[m.content_type] || 0) + 1; return a; }, {}),
    media_metadata_only: s.ko_messages.reduce((n, m) => n + (m.media || []).filter((x) => x.download === "metadata_only").length, 0),
    messages: s.ko_messages.length,
    last_seq: Number(s.ko_archive.last_seq) || 0,
    files_stored: s.ko_files.filter((f) => f.status === "stored").length,
    files_failed: s.ko_files.filter((f) => f.status !== "stored").length,
    last_archived_at: s.ko_messages.length ? s.ko_messages[s.ko_messages.length - 1].archived_at : null
  }));
}

module.exports = { archiveMessage, listMessages, classify, KINDS, getMessage, getFileMeta, filePath, readFile, stats, sniff,
  KO_FILES_DIR, MAX_FILE_BYTES, ALLOWED_MIME: Object.keys(ALLOWED) };
