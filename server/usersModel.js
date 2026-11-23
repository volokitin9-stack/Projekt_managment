"use strict";

// Individual manager accounts ("Пользователи" admin section) - критическое дополнение к ТЗ
// "Разграничение доступа к «Доске проектов»". Replaces the single shared manager login with real
// per-person accounts, each with its own hashed password (server/passwords.js) and a role, today
// always "ko_viewer" - see docs/USER_ACCOUNTS.md for the full design and why the existing
// DASHBOARD_USERNAME/PASSWORD admin login (server/dashboardAuth.js) is deliberately left outside
// this collection rather than migrated into it.
//
// Storage: server/store.js's "users" and "users_audit" collections (JSON file, same persistence
// and atomic-write guarantees as every other collection - "users" therefore already survives a
// Railway restart with no extra code, exactly like "projects" does).
//
// Session revocation ("мгновенная блокировка"/"сброс пароля... инвалидировать старые сессии") is
// built on token_version, not a session table: every issued session token embeds the user's
// token_version at login time (server/dashboardAuth.js); disabling, reactivating or resetting a
// password here bumps it, and isSessionLive() re-checks the *current* value on every request - so
// a token signed with an old version stops working on the very next request, from any device,
// with nothing to clean up or expire.

const { v4: uuidv4 } = require("uuid");
const crypto = require("crypto");
const store = require("./store");
const { hashPassword, verifyPassword } = require("./passwords");

const MAX_FAILED_ATTEMPTS = 5;
const LOCKOUT_MS = 15 * 60 * 1000;
const MAX_AUDIT_ENTRIES = 5000;

// In-memory, per-process - deliberate, same reasoning as server/auth.js's existing agent rate
// limiter: Railway runs this app as a single instance (docs/RAILWAY_DEPLOYMENT.md), so this is
// not lost mid-session, and resetting on a restart/redeploy is an acceptable, conservative trade
// (a fresh deploy already implies a brief availability gap; a few extra login attempts right
// after one is not the threat model this guards against - sustained brute-forcing within one
// running process is).
const failedAttempts = new Map();

function nowIso() {
  return new Date().toISOString();
}

function toApi(u) {
  return {
    id: u.id,
    full_name: u.full_name,
    username: u.username,
    role: u.role,
    active: !!u.active,
    created_at: u.created_at,
    updated_at: u.updated_at,
    last_login_at: u.last_login_at || null,
    created_by: u.created_by || null,
    disabled_at: u.disabled_at || null,
    disabled_by: u.disabled_by || null
    // Deliberately never password_hash or token_version - requirement: never show an existing
    // password (there is nothing to show - only a hash - but the hash itself is still not an
    // admin-facing field), and token_version is purely an internal revocation counter.
  };
}

function normalizeUsername(username) {
  return String(username || "").trim();
}

function list() {
  return store.read((s) => s.users.slice()
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
    .map(toApi));
}

/** The raw stored record (has password_hash/token_version) - internal use only (login,
 *  server/dashboardAuth.js's live session check). Routes must use toApi()/list(). */
function getById(id) {
  return store.read((s) => s.users.find((u) => u.id === id) || null);
}

function getByUsername(username) {
  const uname = normalizeUsername(username).toLowerCase();
  if (!uname) return null;
  return store.read((s) => s.users.find((u) => u.username.toLowerCase() === uname) || null);
}

function recordAudit(fields) {
  store.tx((s) => {
    s.users_audit.push(Object.assign({ id: uuidv4(), at: nowIso() }, fields));
    if (s.users_audit.length > MAX_AUDIT_ENTRIES) s.users_audit.splice(0, s.users_audit.length - MAX_AUDIT_ENTRIES);
  });
}

function recentAudit(limit = 200) {
  return store.read((s) => s.users_audit.slice(-limit).reverse());
}

/** A short, typeable, random password - handed to the admin once (create/reset), never stored in
 *  plaintext and never retrievable again afterward. */
function generatePassword() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"; // no 0/O/1/l/I
  const bytes = crypto.randomBytes(14);
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) out += alphabet[bytes[i] % alphabet.length];
  return out;
}

async function create({ full_name, username, password }, actor) {
  const uname = normalizeUsername(username);
  const name = String(full_name || "").trim();
  if (!name) return { error: "full_name_required" };
  if (!uname) return { error: "username_required" };
  if (getByUsername(uname)) return { error: "username_taken" };
  const plainPassword = password && String(password).length >= 8 ? String(password) : generatePassword();
  if (password && String(password).length > 0 && String(password).length < 8) return { error: "weak_password" };
  const password_hash = await hashPassword(plainPassword);
  const ts = nowIso();
  const user = {
    id: uuidv4(), full_name: name, username: uname, password_hash,
    // Критическое требование: this interface can only ever create role ko_viewer - there is no
    // input that produces anything else, which is what "не допускать создания второго
    // администратора через менеджерский интерфейс" means in this architecture (see module header).
    role: "ko_viewer", active: true, token_version: 1,
    created_at: ts, updated_at: ts, last_login_at: null,
    created_by: actor || null, disabled_at: null, disabled_by: null
  };
  store.tx((s) => { s.users.push(user); });
  recordAudit({ type: "user_created", actor, target_user_id: user.id, target_username: uname });
  return { user: toApi(user), generated_password: password ? null : plainPassword };
}

function update(id, { full_name, username }, actor) {
  const existing = getById(id);
  if (!existing) return { error: "not_found" };
  let newUsername;
  if (username !== undefined) {
    newUsername = normalizeUsername(username);
    if (!newUsername) return { error: "username_required" };
    const other = getByUsername(newUsername);
    if (other && other.id !== id) return { error: "username_taken" };
  }
  let updated;
  store.tx((s) => {
    const u = s.users.find((x) => x.id === id);
    if (full_name !== undefined) u.full_name = String(full_name).trim();
    if (newUsername !== undefined) u.username = newUsername;
    u.updated_at = nowIso();
    updated = u;
  });
  recordAudit({ type: "user_updated", actor, target_user_id: id, target_username: updated.username });
  return { user: toApi(updated) };
}

async function resetPassword(id, actor) {
  const existing = getById(id);
  if (!existing) return { error: "not_found" };
  const plainPassword = generatePassword();
  const password_hash = await hashPassword(plainPassword);
  store.tx((s) => {
    const u = s.users.find((x) => x.id === id);
    u.password_hash = password_hash;
    u.token_version = (u.token_version || 1) + 1; // every existing session for this user dies now
    u.updated_at = nowIso();
  });
  recordAudit({ type: "password_reset", actor, target_user_id: id, target_username: existing.username });
  return { generated_password: plainPassword };
}

function setActive(id, active, actor) {
  const existing = getById(id);
  if (!existing) return { error: "not_found" };
  if (!active && existing.role === "admin" && countActiveAdmins() <= 1) {
    return { error: "last_admin" };
  }
  let updated;
  store.tx((s) => {
    const u = s.users.find((x) => x.id === id);
    u.active = !!active;
    u.token_version = (u.token_version || 1) + 1; // kill any existing sessions either way
    if (active) {
      u.disabled_at = null;
      u.disabled_by = null;
    } else {
      u.disabled_at = nowIso();
      u.disabled_by = actor || null;
    }
    u.updated_at = nowIso();
    updated = u;
  });
  recordAudit({ type: active ? "user_activated" : "user_disabled", actor, target_user_id: id, target_username: updated.username });
  return { user: toApi(updated) };
}

/** Defensive only: today no "users" row ever has role "admin" (the one admin identity is the
 *  DASHBOARD_USERNAME/PASSWORD env var, never a row here - see docs/USER_ACCOUNTS.md), so this is
 *  never actually reached by setActive() above in practice. Kept so the guard still holds if the
 *  model is ever extended to store admin accounts as rows too. */
function countActiveAdmins() {
  return store.read((s) => s.users.filter((u) => u.role === "admin" && u.active).length);
}

// ---- login rate limiting - shared by this module's own login and dashboardAuth.js's admin login ----
function loginAllowed(username) {
  const entry = failedAttempts.get(normalizeUsername(username).toLowerCase());
  return !(entry && entry.lockedUntil && entry.lockedUntil > Date.now());
}

function recordLoginFailure(username) {
  const key = normalizeUsername(username).toLowerCase();
  const entry = failedAttempts.get(key) || { count: 0, lockedUntil: 0 };
  entry.count += 1;
  if (entry.count >= MAX_FAILED_ATTEMPTS) entry.lockedUntil = Date.now() + LOCKOUT_MS;
  failedAttempts.set(key, entry);
}

function recordLoginSuccess(username) {
  failedAttempts.delete(normalizeUsername(username).toLowerCase());
}

/** Verifies a manager login attempt (username + plaintext password). Returns {user: <raw record>}
 *  on success, {error} otherwise ("rate_limited" | "invalid_credentials"). Always records an
 *  audit entry, success or failure. */
async function verifyLogin(username, password) {
  const uname = normalizeUsername(username);
  if (!loginAllowed(uname)) {
    recordAudit({ type: "login_failed", target_username: uname, detail: "rate_limited" });
    return { error: "rate_limited" };
  }
  const user = getByUsername(uname);
  if (!user || !user.active) {
    recordLoginFailure(uname);
    recordAudit({ type: "login_failed", target_username: uname, detail: user ? "inactive" : "no_such_user" });
    return { error: "invalid_credentials" };
  }
  const ok = await verifyPassword(password, user.password_hash);
  if (!ok) {
    recordLoginFailure(uname);
    recordAudit({ type: "login_failed", target_username: uname, target_user_id: user.id, detail: "bad_password" });
    return { error: "invalid_credentials" };
  }
  recordLoginSuccess(uname);
  store.tx((s) => { s.users.find((x) => x.id === user.id).last_login_at = nowIso(); });
  recordAudit({ type: "login_success", target_username: uname, target_user_id: user.id });
  return { user: getById(user.id) };
}

/** True if `id` + `tokenVersion` (both embedded in a session token at login time) are still a
 *  live session right now: the user exists, is active, and token_version matches exactly. This is
 *  what makes a disable/reactivate/reset take effect immediately, on the very next request, from
 *  any device holding an older token - nothing to expire or look up in a session table. */
function isSessionLive(id, tokenVersion) {
  const u = getById(id);
  return !!u && u.active && u.token_version === tokenVersion;
}

module.exports = {
  list, getById, getByUsername, create, update, resetPassword, setActive, verifyLogin,
  isSessionLive, countActiveAdmins, recentAudit, recordAudit, toApi,
  loginAllowed, recordLoginFailure, recordLoginSuccess
};
