"use strict";

// Password hashing for individual manager accounts (server/usersModel.js) - критическое
// требование: "Пароли хранить только в виде стойких хешей: Argon2id или scrypt." Uses scrypt via
// Node's own built-in `crypto` module - no new npm dependency (this repo's existing auth, e.g.
// server/dashboardAuth.js's session HMAC, already only ever uses Node's built-in crypto; adding a
// native-binding package like `argon2` here would be the first of its kind and would complicate
// the Railway/Railpack build this session's earlier work went to some lengths to get right).
//
// Parameters follow OWASP's scrypt guidance for an interactive login (N=2^15, r=8, p=1), with the
// cost factor stored alongside the hash so it can be raised later without invalidating existing
// password hashes (verify() always uses whatever N/r/p the stored hash itself recorded).

const crypto = require("crypto");

const VERSION = "scrypt1";
const DEFAULT_LOG2N = 15; // N = 2^15 = 32768
const R = 8;
const P = 1;
const KEYLEN = 64;
const SALT_BYTES = 16;

function scryptAsync(password, salt, keylen, options) {
  return new Promise((resolve, reject) => {
    crypto.scrypt(password, salt, keylen, options, (err, derivedKey) => {
      if (err) reject(err);
      else resolve(derivedKey);
    });
  });
}

/** Returns a self-describing hash string: "scrypt1$<log2N>$<r>$<p>$<saltHex>$<hashHex>". */
async function hashPassword(password) {
  if (typeof password !== "string" || password.length < 1) {
    throw Object.assign(new Error("Password must be a non-empty string"), { code: "invalid_password" });
  }
  const salt = crypto.randomBytes(SALT_BYTES);
  const n = 2 ** DEFAULT_LOG2N;
  const derived = await scryptAsync(password, salt, KEYLEN, { N: n, r: R, p: P, maxmem: 256 * n * R });
  return [VERSION, DEFAULT_LOG2N, R, P, salt.toString("hex"), derived.toString("hex")].join("$");
}

/** Constant-time comparison against a stored hash string. Never throws on a malformed/legacy
 *  hash - returns false, so a corrupted record fails closed rather than crashing the login route. */
async function verifyPassword(password, stored) {
  try {
    const parts = String(stored || "").split("$");
    if (parts.length !== 6 || parts[0] !== VERSION) return false;
    const log2n = Number(parts[1]), r = Number(parts[2]), p = Number(parts[3]);
    const salt = Buffer.from(parts[4], "hex");
    const expected = Buffer.from(parts[5], "hex");
    if (!Number.isFinite(log2n) || !Number.isFinite(r) || !Number.isFinite(p) || !salt.length || !expected.length) return false;
    const n = 2 ** log2n;
    const derived = await scryptAsync(String(password), salt, expected.length, { N: n, r, p, maxmem: 256 * n * r });
    return derived.length === expected.length && crypto.timingSafeEqual(derived, expected);
  } catch (e) {
    return false;
  }
}

module.exports = { hashPassword, verifyPassword };
