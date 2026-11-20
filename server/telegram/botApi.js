"use strict";

// Minimal Telegram Bot API client: send a reply, and download a photo/document's bytes for
// server/telegram/engineeringChannel.js to attach to a project. One shared bot token
// (TELEGRAM_BOT_TOKEN) for both channels, per the ТЗ (§3: "допускается использование одного
// Telegram-бота для двух каналов").
//
// TELEGRAM_API_BASE_URL is overridable (default https://api.telegram.org) for the same reason as
// server/telegram/aiClient.js's ANTHROPIC_API_BASE_URL - tests point this at a local fake server.

function baseUrl() {
  return (process.env.TELEGRAM_API_BASE_URL || "https://api.telegram.org").replace(/\/$/, "");
}

function token() {
  return process.env.TELEGRAM_BOT_TOKEN || "";
}

function configured() {
  return !!token();
}

async function call(method, body) {
  if (!configured()) throw Object.assign(new Error("TELEGRAM_BOT_TOKEN is not configured"), { code: "not_configured" });
  const res = await fetch(`${baseUrl()}/bot${token()}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {})
  });
  const json = await res.json().catch(() => null);
  if (!res.ok || !json || json.ok !== true) {
    const desc = (json && json.description) || `HTTP ${res.status}`;
    throw Object.assign(new Error(`Telegram API ${method} failed: ${desc}`), { code: "telegram_api_error", status: res.status });
  }
  return json.result;
}

function sendMessage(chatId, text, extra = {}) {
  return call("sendMessage", Object.assign({ chat_id: chatId, text }, extra));
}

/** Downloads a file by file_id and returns {buffer, mimeType, originalname, size} - the shape
 *  projectsModel.attachProjectFile() expects (the same one multer gives the multipart routes). */
async function downloadFile(fileId, { mimeType, originalname } = {}) {
  const info = await call("getFile", { file_id: fileId });
  const url = `${baseUrl()}/file/bot${token()}/${info.file_path}`;
  const res = await fetch(url);
  if (!res.ok) throw Object.assign(new Error(`Telegram file download failed: HTTP ${res.status}`), { code: "telegram_api_error" });
  const buffer = Buffer.from(await res.arrayBuffer());
  return { buffer, mimeType: mimeType || "image/jpeg", originalname: originalname || info.file_path.split("/").pop(), size: buffer.length };
}

module.exports = { configured, sendMessage, downloadFile, call };
