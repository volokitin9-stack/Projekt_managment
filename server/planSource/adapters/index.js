"use strict";

// File-source adapters. Every adapter implements:
//
//   describe()            -> { type, location, ... }   (no secrets)
//   async probe()         -> { name, size, mtime_ms, mtime, ... }  or throws SourceError
//   async fetchTo(dest, probe) -> probe + { sha256, bytes }   copies a stable snapshot to dest
//
// Only "local" is implemented in this version. A network share is used through
// "local" once it is mounted on the server. HTTP(S) and cloud storage adapters
// register here with the same interface; credentials for them must come from
// environment variables, never from the repository.

const { SourceError } = require("./errors");
const { LocalFileSource } = require("./local");

const NOT_YET = {
  http: "HTTP(S)-источник",
  https: "HTTP(S)-источник",
  smb: "сетевая папка SMB (подключите её как локальную папку и используйте PLAN_SOURCE_TYPE=local)",
  sharepoint: "SharePoint/OneDrive",
  s3: "облачное хранилище S3",
  gdrive: "Google Drive"
};

const registry = {
  local: (o) => new LocalFileSource(o)
};

function createSource(type, options) {
  const t = String(type || "").toLowerCase();
  if (!t) throw new SourceError("not_configured", "PLAN_SOURCE_TYPE не задан");
  if (registry[t]) return registry[t](options);
  if (NOT_YET[t]) throw new SourceError("not_implemented", `${NOT_YET[t]}: адаптер ещё не реализован`);
  throw new SourceError("not_configured", `неизвестный PLAN_SOURCE_TYPE «${type}» (доступно: ${Object.keys(registry).join(", ")})`);
}

function registerSource(type, factory) {
  registry[String(type).toLowerCase()] = factory;
}

module.exports = { createSource, registerSource, SourceError };
