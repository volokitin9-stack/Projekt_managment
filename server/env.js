"use strict";

// Loads <project root>/.env - always from the project folder, not from the
// current directory, so `npm start` works from anywhere.
//
// * Variables already set in the environment (e.g. `set X=...` in cmd) win,
//   like dotenv.
// * Backslashes are NEVER escape sequences: a double-quoted Windows path such as
//   "C:\Users\user\new\python.exe" keeps "\n" as backslash + n (plain dotenv
//   would turn it into a line break). Unquoted and single-quoted values are
//   literal anyway.

const fs = require("fs");
const path = require("path");
const dotenv = require("dotenv");

const ROOT = path.join(__dirname, "..");
const loaded = { file: null, keys: [] };

function parseLiteral(text) {
  const parsed = dotenv.parse(text);
  // Re-read double-quoted values that contain a backslash: take them literally.
  text.split(/\r?\n/).forEach((line) => {
    const m = /^\s*(?:export\s+)?([\w.-]+)\s*=\s*"((?:[^"]*))"\s*(?:#.*)?$/.exec(line);
    if (m && m[2].includes("\\")) parsed[m[1]] = m[2];
  });
  return parsed;
}

function loadEnv(root = ROOT, target = process.env) {
  const file = path.join(root, ".env");
  if (!fs.existsSync(file)) return { file: null, keys: [] };
  const values = parseLiteral(fs.readFileSync(file, "utf-8").replace(/^\uFEFF/, ""));   // tolerate a UTF-8 BOM (Notepad)
  const keys = [];
  Object.entries(values).forEach(([k, v]) => {
    if (target[k] === undefined) {
      target[k] = v;
      keys.push(k);
    }
  });
  return { file, keys };
}

if (!loaded.file) Object.assign(loaded, loadEnv());

module.exports = { loadEnv, parseLiteral, loaded, ROOT };
