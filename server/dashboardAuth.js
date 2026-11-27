"use strict";

// Session auth for the dashboard. The original, single-operator design (spec: "Railway must
// require authentication for all project data") was one shared login, DASHBOARD_USERNAME/
// DASHBOARD_PASSWORD - that is now the one and only *administrator* identity, unchanged in every
// respect (same env vars, same credential check, same session cookie format for it). Individual
// manager accounts (role "ko_viewer", server/usersModel.js, "Пользователи" admin section) are a
// second, later identity source this same login/session endpoint also accepts - see login()
// below and docs/USER_ACCOUNTS.md for why the two are kept deliberately separate rather than
// unifying the admin into a "users" row (critical requirement: "сохранить совместимость с
// текущей административной авторизацией и действующим паролем администратора").
//
// Session cookie: a signed, httpOnly token (HMAC-SHA256, DASHBOARD_SESSION_SECRET), no
// server-side session store needed for the admin identity (as before). A manager session
// additionally embeds {uid, tv} (the user's id and token_version at login time); every request
// re-checks that pair against the live record (usersModel.isSessionLive()), which is what makes
// disabling, reactivating or resetting a password take effect immediately, on the very next
// request, from every device - not just on that cookie's natural expiry.
//
// Left OFF (no-op) unless DASHBOARD_USERNAME and DASHBOARD_PASSWORD are both set, so the existing
// local/single-machine deployment (docs/WINDOWS.md) keeps working unauthenticated exactly as today -
// this is additive, not a breaking change to the current install. Manager accounts have no effect
// in that mode either, for the same reason: there is no login wall for them to sit behind.

const crypto = require("crypto");
const usersModel = require("./usersModel");

const COOKIE_NAME = "shturman_session";
const SESSION_MS = Number(process.env.DASHBOARD_SESSION_MS || 12 * 60 * 60 * 1000); // 12h

function configured() {
  return !!(process.env.DASHBOARD_USERNAME && process.env.DASHBOARD_PASSWORD);
}

function secret() {
  const s = process.env.DASHBOARD_SESSION_SECRET;
  if (s) return s;
  // No secret configured: derive one from the password so sessions still verify within this
  // process, but warn loudly - a restart invalidates every session, and this is not safe for more
  // than one server instance. Document DASHBOARD_SESSION_SECRET as required in the Railway guide.
  return crypto.createHash("sha256").update(`fallback:${process.env.DASHBOARD_PASSWORD || ""}`).digest("hex");
}

function sign(payload) {
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const mac = crypto.createHmac("sha256", secret()).update(body).digest("base64url");
  return `${body}.${mac}`;
}

function verify(token) {
  if (!token || typeof token !== "string" || !token.includes(".")) return null;
  const [body, mac] = token.split(".");
  const expected = crypto.createHmac("sha256", secret()).update(body).digest("base64url");
  if (mac.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(mac), Buffer.from(expected))) return null;
  let payload;
  try {
    payload = JSON.parse(Buffer.from(body, "base64url").toString("utf-8"));
  } catch (e) {
    return null;
  }
  if (!payload || !payload.exp || payload.exp < Date.now()) return null;
  return payload;
}

function parseCookies(header) {
  const out = {};
  String(header || "").split(";").forEach((p) => {
    const i = p.indexOf("=");
    if (i === -1) return;
    out[p.slice(0, i).trim()] = decodeURIComponent(p.slice(i + 1).trim());
  });
  return out;
}

/** identity: {username, role, uid?, tokenVersion?} - uid/tokenVersion only for a manager session
 *  (role !== "admin"); their presence is what requireDashboardSession re-validates live. */
function setSessionCookie(res, identity) {
  const payload = { u: identity.username, role: identity.role, exp: Date.now() + SESSION_MS };
  if (identity.uid) {
    payload.uid = identity.uid;
    payload.tv = identity.tokenVersion;
  }
  const token = sign(payload);
  const secureFlag = process.env.NODE_ENV === "production" || process.env.FORCE_SECURE_COOKIE === "1" ? "; Secure" : "";
  res.setHeader("Set-Cookie", `${COOKIE_NAME}=${encodeURIComponent(token)}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${Math.floor(SESSION_MS / 1000)}${secureFlag}`);
}

function clearSessionCookie(res) {
  res.setHeader("Set-Cookie", `${COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0`);
}

/** A CSRF token bound to the current session cookie (HMAC - not guessable without it), or null if
 *  there is no valid session. The frontend reads this once (GET /api/whoami) and must echo it back
 *  as X-CSRF-Token on state-changing requests that reach outside this app (e.g. Telegram's
 *  setWebhook) - defense in depth alongside the session cookie's own SameSite=Lax. */
function csrfToken(req) {
  if (!configured()) return null;
  const cookies = parseCookies(req.get("cookie"));
  const token = cookies[COOKIE_NAME];
  if (!verify(token)) return null;
  return crypto.createHmac("sha256", secret()).update(`csrf:${token}`).digest("base64url");
}

/** Express middleware: requires a matching X-CSRF-Token header (in addition to whatever session
 *  check runs before it). No-op when dashboard auth is unconfigured, same as
 *  requireDashboardSession - there is no session to bind a token to. */
function requireCsrfToken(req, res, next) {
  if (!configured()) return next();
  const expected = csrfToken(req);
  const got = req.get("X-CSRF-Token");
  if (!expected || !got || got.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(got), Buffer.from(expected))) {
    return res.status(403).json({ error: "Missing or invalid CSRF token" });
  }
  next();
}

function rejectSession(req, res) {
  if (req.path.startsWith("/api/")) return res.status(401).json({ error: "Not authenticated. POST /api/login first." });
  return res.redirect(302, `/login.html?next=${encodeURIComponent(req.originalUrl)}`);
}

/** Express middleware: 401s API calls, redirects page loads to a login prompt, when dashboard auth
 *  is configured and the request has no valid, still-live session. A no-op when
 *  DASHBOARD_USERNAME/PASSWORD are unset - see "configured()" above (every request is then treated
 *  as full/admin access, exactly as before this file had any notion of roles).
 *
 *  Sets req.authRole ("admin" | a manager's role, e.g. "ko_viewer") and req.authUser
 *  ({username, uid}) for route-level checks (requireAdminRole below, or a router's own finer-
 *  grained logic such as server/routes/projects.js's read-only-for-non-admin rule). For a manager
 *  session this also re-validates {uid, tv} against the live user record on *every* request
 *  (критическое требование: мгновенная блокировка/сброс пароля инвалидирует сессию без
 *  исключений) - a disabled account's or an old password's session stops working immediately,
 *  not just once its cookie's own expiry happens to arrive. */
function requireDashboardSession(req, res, next) {
  if (!configured()) {
    req.authRole = "admin";
    return next();
  }
  const cookies = parseCookies(req.get("cookie"));
  const session = verify(cookies[COOKIE_NAME]);
  if (!session) return rejectSession(req, res);
  if (session.uid && !usersModel.isSessionLive(session.uid, session.tv)) return rejectSession(req, res);
  req.authRole = session.role || "admin";
  req.authUser = { username: session.u, uid: session.uid || null };
  next();
}

/** Express middleware: 403s anything but the administrator (the DASHBOARD_USERNAME/PASSWORD
 *  identity - never a "users" row; see module header). Must run after requireDashboardSession
 *  (which sets req.authRole), so routes mount it as a second middleware, not standalone. */
function requireAdminRole(req, res, next) {
  if (req.authRole === "admin") return next();
  return res.status(403).json({ error: "Forbidden: administrator access required" });
}

/** POST /api/login { username, password } -> sets the session cookie, for either identity:
 *  - the administrator (DASHBOARD_USERNAME/PASSWORD, timing-safe comparison on both fields,
 *    unchanged from before individual accounts existed), or
 *  - a manager account (server/usersModel.js), tried only once the admin credentials don't match.
 *  A fixed error message either way so a wrong username never confirms which kind it was meant to
 *  be. Both paths share one rate limiter keyed by the attempted username (critical requirement:
 *  "ограничить число попыток входа") - checked before either credential check runs, so a locked-
 *  out username can't be walked past by guessing the admin password correctly mid-lockout. */
async function login(req, res) {
  if (!configured()) return res.status(501).json({ error: "Dashboard auth is not configured on this server" });
  const { username, password } = req.body || {};
  const uname = String(username || "");
  if (!usersModel.loginAllowed(uname)) {
    return res.status(429).json({ error: "Слишком много неудачных попыток входа. Попробуйте позже." });
  }
  const expectedUser = Buffer.from(String(process.env.DASHBOARD_USERNAME));
  const expectedPass = Buffer.from(String(process.env.DASHBOARD_PASSWORD));
  const gotUser = Buffer.from(uname);
  const gotPass = Buffer.from(String(password || ""));
  const userOk = gotUser.length === expectedUser.length && crypto.timingSafeEqual(gotUser, expectedUser);
  const passOk = gotPass.length === expectedPass.length && crypto.timingSafeEqual(gotPass, expectedPass);
  if (userOk && passOk) {
    usersModel.recordLoginSuccess(uname);
    usersModel.recordAudit({ type: "login_success", target_username: uname, detail: "admin" });
    setSessionCookie(res, { role: "admin", username: uname });
    return res.json({ ok: true });
  }
  const result = await usersModel.verifyLogin(uname, password);
  if (result.error === "rate_limited") {
    return res.status(429).json({ error: "Слишком много неудачных попыток входа. Попробуйте позже." });
  }
  if (!result.user) return res.status(401).json({ error: "Invalid username or password" });
  setSessionCookie(res, {
    role: result.user.role, username: result.user.username, uid: result.user.id, tokenVersion: result.user.token_version
  });
  res.json({ ok: true });
}

/** Username of the current dashboard session (null without one / when auth is unconfigured). */
function sessionUser(req) {
  if (!configured()) return null;
  const session = verify(parseCookies(req.get("cookie"))[COOKIE_NAME]);
  return session ? session.u : null;
}

function logout(req, res) {
  clearSessionCookie(res);
  res.json({ ok: true });
}

function whoAmI(req, res) {
  if (!configured()) return res.json({ auth_required: false });
  const cookies = parseCookies(req.get("cookie"));
  const session = verify(cookies[COOKIE_NAME]);
  const live = session && (!session.uid || usersModel.isSessionLive(session.uid, session.tv));
  // full_name is only ever a real, stored manager name (server/usersModel.js, set at account
  // creation) - the administrator identity has no such field, so the UI falls back to the
  // username rather than ever inventing a display name for it.
  const managerRecord = live && session.uid ? usersModel.getById(session.uid) : null;
  res.json({
    auth_required: true, authenticated: !!live, username: live ? session.u : null,
    full_name: managerRecord ? managerRecord.full_name : null,
    role: live ? (session.role || "admin") : null,
    csrf_token: live ? csrfToken(req) : null
  });
}

module.exports = {
  configured, requireDashboardSession, requireAdminRole, requireCsrfToken, csrfToken, sessionUser, login, logout, whoAmI,
  _verify: verify, _sign: sign
};
