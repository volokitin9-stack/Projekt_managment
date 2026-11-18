"use strict";

// Minimal session auth for the dashboard itself, for the Railway deployment (spec: "Railway must
// require authentication for all project data"). The original app had no user accounts by design
// (see MIGRATION_REPORT.md) - this adds the smallest thing that satisfies "authenticated, not open
// to the internet" without inventing a multi-role user system nothing in this codebase asked for:
// one shared operator login (DASHBOARD_USERNAME / DASHBOARD_PASSWORD), a signed, httpOnly session
// cookie (HMAC-SHA256 over username+expiry with DASHBOARD_SESSION_SECRET), no server-side session
// store needed. "Administrative permissions" in this single-operator app means "logged in or not" -
// if real per-user roles are ever needed, that is new product scope, same conclusion the original
// migration reached for AGENT_API_KEY's single shared secret.
//
// Left OFF (no-op) unless DASHBOARD_USERNAME and DASHBOARD_PASSWORD are both set, so the existing
// local/single-machine deployment (docs/WINDOWS.md) keeps working unauthenticated exactly as today -
// this is additive, not a breaking change to the current install.

const crypto = require("crypto");

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

function setSessionCookie(res, username) {
  const token = sign({ u: username, exp: Date.now() + SESSION_MS });
  const secureFlag = process.env.NODE_ENV === "production" || process.env.FORCE_SECURE_COOKIE === "1" ? "; Secure" : "";
  res.setHeader("Set-Cookie", `${COOKIE_NAME}=${encodeURIComponent(token)}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${Math.floor(SESSION_MS / 1000)}${secureFlag}`);
}

function clearSessionCookie(res) {
  res.setHeader("Set-Cookie", `${COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0`);
}

/** Express middleware: 401s API calls, redirects page loads to a login prompt, when dashboard auth
 *  is configured and the request has no valid session. A no-op when DASHBOARD_USERNAME/PASSWORD are
 *  unset - see "configured()" above. */
function requireDashboardSession(req, res, next) {
  if (!configured()) return next();
  const cookies = parseCookies(req.get("cookie"));
  const session = verify(cookies[COOKIE_NAME]);
  if (session) return next();
  if (req.path.startsWith("/api/")) return res.status(401).json({ error: "Not authenticated. POST /api/login first." });
  return res.redirect(302, `/login.html?next=${encodeURIComponent(req.originalUrl)}`);
}

/** POST /api/login { username, password } -> sets the session cookie. Timing-safe comparison on both
 *  fields; a fixed error message either way so a wrong username does not confirm it exists. */
function login(req, res) {
  if (!configured()) return res.status(501).json({ error: "Dashboard auth is not configured on this server" });
  const { username, password } = req.body || {};
  const expectedUser = Buffer.from(String(process.env.DASHBOARD_USERNAME));
  const expectedPass = Buffer.from(String(process.env.DASHBOARD_PASSWORD));
  const gotUser = Buffer.from(String(username || ""));
  const gotPass = Buffer.from(String(password || ""));
  const userOk = gotUser.length === expectedUser.length && crypto.timingSafeEqual(gotUser, expectedUser);
  const passOk = gotPass.length === expectedPass.length && crypto.timingSafeEqual(gotPass, expectedPass);
  if (!userOk || !passOk) return res.status(401).json({ error: "Invalid username or password" });
  setSessionCookie(res, username);
  res.json({ ok: true });
}

function logout(req, res) {
  clearSessionCookie(res);
  res.json({ ok: true });
}

function whoAmI(req, res) {
  if (!configured()) return res.json({ auth_required: false });
  const cookies = parseCookies(req.get("cookie"));
  const session = verify(cookies[COOKIE_NAME]);
  res.json({ auth_required: true, authenticated: !!session, username: session ? session.u : null });
}

module.exports = { configured, requireDashboardSession, login, logout, whoAmI, _verify: verify, _sign: sign };
