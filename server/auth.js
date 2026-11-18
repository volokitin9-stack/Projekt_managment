"use strict";

// Authentication for the external AI agent. A single shared secret is enough
// here: this dashboard has exactly one trusted machine caller (the agent),
// not multiple human accounts with different roles — see MIGRATION_REPORT.md
// for why per-user permissions were intentionally not built.

function requireAgentAuth(req, res, next) {
  const expected = process.env.AGENT_API_KEY;
  if (!expected) {
    return res.status(501).json({
      error: "AGENT_API_KEY is not configured on the server. Set it in .env before the agent can authenticate."
    });
  }
  const header = req.get("authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  const provided = match ? match[1] : null;
  if (!provided || provided !== expected) {
    return res.status(401).json({ error: "Missing or invalid Authorization: Bearer <AGENT_API_KEY> header" });
  }
  next();
}

// ---- Windows planning-laptop agent: a second, distinct credential (WINDOWS_AGENT_API_KEY) ----
//
// This is deliberately a different secret from AGENT_API_KEY: AGENT_API_KEY is the long-standing
// credential plan_agent's own HTTP client already uses to write project data through /api/agent/*
// (unchanged by this work - see docs/EXTERNAL_AGENT.md). WINDOWS_AGENT_API_KEY instead authorizes
// only the job-queue endpoints in server/routes/agentJobs.js (poll/claim/heartbeat/submit-result).
// Splitting them means the job-queue credential can be rotated or revoked without touching the
// agent's data-write credential, and a leak of one does not hand over the other.
//
// Replay resistance: every agent-job request must carry a timestamp (X-Agent-Timestamp, unix ms)
// within AGENT_REPLAY_WINDOW_MS of server time and a nonce (X-Agent-Nonce, >=16 chars). Nonces seen
// inside the window are rejected on reuse. This is an in-memory set - it resets on a server restart,
// which only re-opens the (short) window that just elapsed, not a meaningful replay risk for a
// single-agent deployment. A production deployment with multiple untrusted callers should move this
// to a shared store (e.g. the same JSON store, or Redis if one is ever added) instead.
//
// Rate limiting: a simple fixed-window counter per credential (there is exactly one agent credential
// in this deployment, so this is effectively a global cap on job-queue traffic) - generous enough for
// a poll every few seconds, tight enough to blunt a credential leak being used to hammer the server.

const REPLAY_WINDOW_MS = Number(process.env.AGENT_REPLAY_WINDOW_MS || 5 * 60 * 1000);
const RATE_LIMIT_MAX = Number(process.env.AGENT_RATE_LIMIT_MAX || 120);
const RATE_LIMIT_WINDOW_MS = Number(process.env.AGENT_RATE_LIMIT_WINDOW_MS || 60 * 1000);

const seenNonces = new Map(); // nonce -> expiry ms
let rateWindowStart = Date.now();
let rateWindowCount = 0;

function sweepNonces(now) {
  if (seenNonces.size < 2000) return; // only bother sweeping once it is worth the O(n) pass
  for (const [nonce, expiry] of seenNonces) {
    if (expiry < now) seenNonces.delete(nonce);
  }
}

function checkRateLimit() {
  const now = Date.now();
  if (now - rateWindowStart >= RATE_LIMIT_WINDOW_MS) {
    rateWindowStart = now;
    rateWindowCount = 0;
  }
  rateWindowCount += 1;
  return rateWindowCount <= RATE_LIMIT_MAX;
}

function requireWindowsAgentAuth(req, res, next) {
  const expected = process.env.WINDOWS_AGENT_API_KEY;
  if (!expected) {
    return res.status(501).json({
      error: "WINDOWS_AGENT_API_KEY is not configured on the server. Set it before the Windows agent can authenticate."
    });
  }
  const header = req.get("authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  const provided = match ? match[1] : null;
  if (!provided || provided !== expected) {
    return res.status(401).json({ error: "Missing or invalid Authorization: Bearer <WINDOWS_AGENT_API_KEY> header" });
  }
  if (!checkRateLimit()) {
    return res.status(429).json({ error: `Rate limit exceeded: max ${RATE_LIMIT_MAX} agent-job requests per ${RATE_LIMIT_WINDOW_MS}ms` });
  }
  const ts = Number(req.get("x-agent-timestamp"));
  const nonce = req.get("x-agent-nonce") || "";
  if (!ts || !Number.isFinite(ts)) {
    return res.status(401).json({ error: "Missing or invalid X-Agent-Timestamp header (unix ms)" });
  }
  if (Math.abs(Date.now() - ts) > REPLAY_WINDOW_MS) {
    return res.status(401).json({ error: `X-Agent-Timestamp is outside the ${REPLAY_WINDOW_MS}ms replay window - check the laptop's clock` });
  }
  if (!nonce || nonce.length < 16) {
    return res.status(401).json({ error: "Missing or too-short X-Agent-Nonce header (>=16 characters)" });
  }
  const now = Date.now();
  sweepNonces(now);
  if (seenNonces.has(nonce)) {
    return res.status(401).json({ error: "X-Agent-Nonce already used - this looks like a replayed request" });
  }
  seenNonces.set(nonce, now + REPLAY_WINDOW_MS);
  next();
}

// Test-only: let the test suite reset in-memory guard state between cases without restarting the process.
function _resetGuardStateForTests() {
  seenNonces.clear();
  rateWindowStart = Date.now();
  rateWindowCount = 0;
}

module.exports = { requireAgentAuth, requireWindowsAgentAuth, _resetGuardStateForTests };
