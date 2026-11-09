"use strict";

// Local-disk storage adapter. This is the one file that knows where uploaded
// bytes physically live. Swapping to an external object store (S3-compatible,
// GCS, etc.) later should only require rewriting the three functions below —
// nothing in routes/ should ever touch `UPLOADS_DIR` or `fs` directly.

const fs = require("fs");
const path = require("path");
const { v4: uuidv4 } = require("uuid");

const UPLOADS_DIR = path.resolve(process.env.UPLOADS_DIR || "./uploads");

const ALLOWED_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
  "application/pdf"
]);

const MAX_UPLOAD_BYTES = Number(process.env.MAX_UPLOAD_BYTES || 10 * 1024 * 1024);

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function extensionFor(originalFilename, mimeType) {
  const fromName = path.extname(originalFilename || "").toLowerCase();
  if (fromName) return fromName;
  const byMime = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf"
  };
  return byMime[mimeType] || "";
}

/**
 * Validate a file's declared mime type and size before accepting it.
 * Never trust the filename extension alone (per the migration spec) — the
 * mime type here comes from multer, which reads the multipart Content-Type
 * the client declared for the part, not the filename.
 */
function validateFile(file) {
  if (!file) return { ok: false, error: "No file provided" };
  if (!ALLOWED_MIME_TYPES.has(file.mimetype)) {
    return { ok: false, error: `Unsupported file type: ${file.mimetype}` };
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return { ok: false, error: `File exceeds the ${MAX_UPLOAD_BYTES} byte limit` };
  }
  return { ok: true };
}

/**
 * Persist an uploaded file's buffer under a project-scoped directory.
 * Returns { storageKey, absolutePath } — storageKey is what gets recorded in
 * the database and is what maps to a servable URL (see server/index.js's
 * static /uploads mount).
 */
function saveProjectFile(projectId, file) {
  const dir = path.join(UPLOADS_DIR, String(projectId));
  ensureDir(dir);
  const ext = extensionFor(file.originalname, file.mimetype);
  const filename = `${uuidv4()}${ext}`;
  const storageKey = path.posix.join(String(projectId), filename);
  const absolutePath = path.join(UPLOADS_DIR, storageKey);
  fs.writeFileSync(absolutePath, file.buffer);
  return { storageKey, absolutePath };
}

function deleteStoredFile(storageKey) {
  const absolutePath = path.join(UPLOADS_DIR, storageKey);
  if (fs.existsSync(absolutePath)) fs.unlinkSync(absolutePath);
}

function urlForStorageKey(storageKey) {
  return `/uploads/${storageKey.split(path.sep).join("/")}`;
}

module.exports = {
  UPLOADS_DIR,
  MAX_UPLOAD_BYTES,
  ALLOWED_MIME_TYPES,
  validateFile,
  saveProjectFile,
  deleteStoredFile,
  urlForStorageKey
};
