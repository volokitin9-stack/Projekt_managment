"use strict";

// Channel 2: background monitoring of the construction-team group chat. Default behaviour is
// "update engineering project history only" (ТЗ §2, channel 2: "по умолчанию сообщения рабочего
// чата используются для обновления информации об инженерных проектах") - a Kanban task is never
// created from this channel unless the message is an explicit, unambiguous assignment (§2:
// "только при явно выраженном поручении"), and a project's confirmed stage is never changed
// automatically - only proposed, through the existing review queue (the dashboard already renders
// a recommended_stage/current_stage review item with a one-click "Подтвердить" button that calls
// the normal POST /api/projects/:id/stage - see public/index.html's engReviewTableHtml()).

const projectsModel = require("../projectsModel");
const reviewQueue = require("../reviewQueueModel");
const store = require("../store");
const storage = require("../storage");
const { createDocStore } = require("../docStore");
const aiClient = require("./aiClient");
const botApi = require("./botApi");
const telegramLog = require("./log");
const koArchive = require("../ko/archive");

/** TELEGRAM_ENGINEERING_AI_ENABLED=false turns off the paid AI call for THIS channel only: messages
 *  and photos are still archived (server/ko/archive.js) and analysed later by the
 *  shturman-ko-analyst agent through the review queue. Unset = previous behaviour. The personal
 *  channel is not affected. */
function aiEnabled() {
  const v = String(process.env.TELEGRAM_ENGINEERING_AI_ENABLED || "").trim().toLowerCase();
  return !["false", "0", "no", "off"].includes(v);
}

const TOOL_SCHEMA = {
  type: "object",
  properties: {
    relevant: { type: "boolean", description: "false if this has nothing to do with engineering project work" },
    manufacturer: { type: ["string", "null"] },
    vehicle_model: { type: ["string", "null"] },
    product_type: { type: ["string", "null"] },
    article_number: { type: ["string", "null"] },
    event_type: { type: ["string", "null"], enum: ["measurement", "prototype_install", "test", "adjustment", "problem", "note", null] },
    note: { type: ["string", "null"], description: "a short factual summary of what the message says happened - extracted, never invented" },
    stage_hint: { type: ["string", "null"], description: "a stage the message implies reaching, verbatim as implied, for the server to match - never guessed beyond what's stated" },
    explicit_task_assignment: { type: "boolean", description: "true ONLY for a direct, unambiguous instruction to a named person to do something" },
    task_title: { type: ["string", "null"] },
    task_responsible: { type: ["string", "null"] },
    task_due: { type: ["string", "null"] },
    confidence: { type: "string", enum: ["high", "low"] }
  },
  required: ["relevant", "confidence"]
};

const SYSTEM = "Ты анализируешь сообщения рабочего Telegram-чата конструкторского отдела для системы «Доска проектов». " +
  "Извлекай марку, модель, тип изделия, артикул, упоминания этапа и суть произошедшего (замер, тестовый образец, " +
  "испытание, доработка, проблема) - только то, что явно написано, никогда не придумывай. explicit_task_assignment " +
  "- true только если это прямое, однозначное поручение конкретному человеку. relevant:false, если сообщение не об " +
  "инженерном проекте (бытовой разговор и т.п.).";

function matchStage(hint) {
  if (!hint) return null;
  const q = hint.toLowerCase();
  const stages = store.read((s) => s.stages);
  const hit = stages.filter((s) => s.key.toLowerCase().includes(q) || s.label.toLowerCase().includes(q));
  return hit.length === 1 ? hit[0] : null;
}

function candidateProjects({ manufacturer, vehicle_model, product_type, article_number }) {
  const { matches } = projectsModel.matchByIdentifiers({ manufacturer, vehicle_model, product_type, article_number });
  return matches;
}

async function downloadAndQueueUnassignedPhoto(message, fileId, { mediaGroupId, caption }) {
  const file = await botApi.downloadFile(fileId);
  const { storageKey } = storage.saveProjectFile("_unassigned", file);
  const url = storage.urlForStorageKey(storageKey);
  const payload = { url, caption: caption || null, media_group_id: mediaGroupId || null,
    telegram_chat_id: String(message.chat.id), telegram_message_id: String(message.message_id),
    from: (message.from || {}).username || (message.from || {}).first_name || null, date: message.date };
  const dedupeKey = `telegram:${message.chat.id}:${message.message_id}`;
  return reviewQueue.upsert({ kind: "telegram_photo", payload,
    reason: caption ? `Фото без надёжной привязки к проекту: ${caption}` : "Фото без подписи и без распознанного проекта",
    source: "telegram", dedupeKey });
}

/** Once a project is confidently matched for a media-group message, any earlier photos from the
 *  same group that were queued without a caption (and so without a match) get attached too -
 *  ТЗ §7 "поддерживать Telegram media groups": the caption usually arrives on only one message of
 *  the group, never necessarily the first. */
async function attachStragglers(mediaGroupId, projectId, source) {
  if (!mediaGroupId) return;
  const items = reviewQueue.list({ kind: "telegram_photo" }).filter((r) => (r.payload || {}).media_group_id === mediaGroupId);
  for (const item of items) {
    // Nothing besides the url/caption was kept for an unassigned photo - re-download by file id is
    // not possible from the stored payload alone, so this attaches what we already fetched (the
    // review item's own stored copy is left in place, same precedent as the existing
    // /api/agent/images/unassigned flow, which never moves a file once stored).
    reviewQueue.resolve(item.id, "auto_attached_to_" + projectId);
    projectsModel.recordActivity(projectId, "comment:telegram_media_group", source, "telegram",
      null, { note: `Фото из той же группы Telegram, ранее без привязки: ${(item.payload || {}).url}` });
  }
}

async function handle(message, ctx) {
  const chatId = ctx.chatId;
  const text = message.text || message.caption || "";
  const photos = message.photo || [];
  const mediaGroupId = message.media_group_id || null;
  const base = { channel: "engineering", chat_id: chatId, user_id: ctx.userId, update_id: ctx.updateId,
    message_id: message.message_id, message_text: text };

  // Archive first, before any analysis: the КО archive is the shturman-ko-analyst agent's source and
  // must not depend on the AI being available. Idempotent; never sends anything to the chat.
  try {
    await koArchive.archiveMessage(message, ctx);
  } catch (e) {
    telegramLog.record(Object.assign({}, base, { outcome: "error", error: `ko archive: ${e.message}` }));
  }
  if (!aiEnabled()) {
    telegramLog.record(Object.assign({}, base, { outcome: "archived", detail: "AI disabled (TELEGRAM_ENGINEERING_AI_ENABLED=false)" }));
    return;
  }

  if (!text && !photos.length && !message.document) {
    telegramLog.record(Object.assign({}, base, { outcome: "ignored", detail: "no text/photo" }));
    return;
  }

  let action = { relevant: false, confidence: "low" };
  if (text) {
    try {
      action = await aiClient.classify({ system: SYSTEM, prompt: `Сообщение: ${JSON.stringify(text)}`,
        toolName: "engineering_event", toolSchema: TOOL_SCHEMA });
    } catch (e) {
      reviewQueue.upsert({ kind: "telegram_pending", payload: base, reason: `ИИ недоступен: ${e.message}`, source: "telegram" });
      telegramLog.record(Object.assign({}, base, { outcome: "queued", detail: e.message }));
      // Photos are still worth capturing even without AI - never lose an attachment because the
      // AI call failed; it just won't be auto-matched to a project yet.
      for (const p of photos.slice(-1)) {
        await downloadAndQueueUnassignedPhoto(message, p.file_id, { mediaGroupId, caption: text || null }).catch(() => {});
      }
      return;
    }
  }

  if (!action.relevant && !photos.length) {
    telegramLog.record(Object.assign({}, base, { outcome: "ignored", detail: "not relevant" }));
    return;
  }

  const candidates = action.relevant ? candidateProjects(action) : [];
  const matched = candidates.length === 1 ? candidates[0] : null;

  // Photos: attach directly on a confident match, otherwise queue for manual confirmation.
  for (const p of photos.slice(-1)) { // Telegram lists sizes smallest->largest; take the largest
    if (matched) {
      try {
        const file = await botApi.downloadFile(p.file_id);
        const r = projectsModel.attachProjectFile(matched.id, file, { source: "telegram",
          uploadedBy: `tg:${base.user_id}`, caption: text || null,
          telegramMessageId: message.message_id, telegramChatId: chatId });
        telegramLog.record(Object.assign({}, base, { outcome: "updated", detail: `photo attached to ${matched.id}` }));
        await attachStragglers(mediaGroupId, matched.id, "telegram");
      } catch (e) {
        telegramLog.record(Object.assign({}, base, { outcome: "error", error: e.message }));
      }
    } else {
      await downloadAndQueueUnassignedPhoto(message, p.file_id, { mediaGroupId, caption: text || null }).catch((e) =>
        telegramLog.record(Object.assign({}, base, { outcome: "error", error: e.message })));
    }
  }

  if (!action.relevant) return; // photo-only message with no extractable project context handled above

  if (!matched) {
    reviewQueue.upsert({ kind: "telegram_ambiguous_project", payload: Object.assign({ note: action.note }, action),
      reason: candidates.length ? `Несколько проектов подходят (${candidates.length})` : "Проект не найден по сообщению",
      source: "telegram", dedupeKey: `telegram:text:${chatId}:${message.message_id}` });
    telegramLog.record(Object.assign({}, base, { outcome: "queued", detail: `project match: ${candidates.length}` }));
    return;
  }

  // Confident match: record the event in the project's history (always), and separately propose a
  // stage change if one was implied (never applied automatically - see module header).
  projectsModel.recordActivity(matched.id, `event:telegram_${action.event_type || "note"}`, "telegram",
    `tg:${base.user_id}`, null, { note: action.note, message: text });
  telegramLog.record(Object.assign({}, base, { outcome: "updated", detail: `event on ${matched.id}` }));

  const stage = matchStage(action.stage_hint);
  if (stage) {
    const current = projectsModel.getProject(matched.id).status;
    if (stage.key !== current) {
      reviewQueue.upsert({ kind: "telegram_stage_suggestion",
        payload: { project_id: matched.id, recommended_stage: stage.key, current_stage: current,
          project_name: (matched.data || {}).project_name, caption: action.note || text },
        reason: `Из чата конструкторов: ${action.note || text}`, source: "telegram",
        dedupeKey: `telegram:stage:${matched.id}:${stage.key}` });
    }
  }

  // Explicit task assignment only - never an ordinary message (ТЗ §2: "только при явно выраженном
  // поручении"), and only when the AI was confident about it.
  if (action.explicit_task_assignment && action.confidence === "high" && action.task_title) {
    createDocStore("tasks").create({ title: action.task_title, quadrant: "inbox",
      responsible: action.task_responsible || null, due: action.task_due || null, done: false,
      calendarSynced: false, telegramNotified: true, source: "telegram",
      direction: (matched.data || {}).manufacturer || null });
    telegramLog.record(Object.assign({}, base, { outcome: "created", detail: "explicit task from engineering chat" }));
  }
}

module.exports = { handle, aiEnabled };
