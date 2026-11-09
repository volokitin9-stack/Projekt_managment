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

module.exports = { requireAgentAuth };
