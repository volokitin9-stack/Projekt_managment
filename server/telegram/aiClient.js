"use strict";

// Thin Anthropic Messages API client. Used only from server/telegram/personalChannel.js and
// server/telegram/engineeringChannel.js to turn one Telegram message (plus a little context) into
// a structured, schema-constrained object - never to execute anything itself. The API key lives
// only in ANTHROPIC_API_KEY on the server; it is never sent to, or reachable from, the dashboard
// or any client-side code.
//
// ANTHROPIC_API_BASE_URL is overridable (default https://api.anthropic.com) so tests can point
// this at a local fake HTTP server instead of mocking fetch() - the same approach already used
// for a staged Railway export (server/migrate.js) and the fake backend in
// tests/test_deployment.py.

const API_VERSION = "2023-06-01";

function baseUrl() {
  return (process.env.ANTHROPIC_API_BASE_URL || "https://api.anthropic.com").replace(/\/$/, "");
}

function model() {
  return process.env.TELEGRAM_AI_MODEL || "claude-haiku-5-5";
}

function timeoutMs() {
  return Number(process.env.TELEGRAM_AI_TIMEOUT_MS || 20000);
}

function configured() {
  return !!process.env.ANTHROPIC_API_KEY;
}

/**
 * Calls the Messages API with tool_choice forced to `toolName`, so the reply is always one
 * structured tool_use block matching `toolSchema` - never free text that would need heuristic
 * parsing. Returns the tool's `input` object.
 *
 * Throws on any failure (missing key, network, non-2xx, malformed body, no matching tool_use
 * block) with an `error.code` of "not_configured" | "network_error" | "api_error" | "bad_response".
 * Callers (personalChannel.js / engineeringChannel.js) decide what "the AI is unavailable" means
 * for their flow - the one rule everywhere is: queue the message for later, never drop it, never
 * guess an action in its place.
 */
async function classify({ system, prompt, toolName, toolSchema }) {
  if (!configured()) throw Object.assign(new Error("ANTHROPIC_API_KEY is not configured"), { code: "not_configured" });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs());
  let res;
  try {
    res = await fetch(`${baseUrl()}/v1/messages`, {
      method: "POST",
      headers: { "x-api-key": process.env.ANTHROPIC_API_KEY, "anthropic-version": API_VERSION, "content-type": "application/json" },
      body: JSON.stringify({
        model: model(), max_tokens: 1024, system, messages: [{ role: "user", content: prompt }],
        tools: [{ name: toolName, description: `Return a ${toolName}`, input_schema: toolSchema }],
        tool_choice: { type: "tool", name: toolName }
      }),
      signal: controller.signal
    });
  } catch (e) {
    throw Object.assign(new Error(`Anthropic API request failed: ${e.message}`), { code: "network_error" });
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  if (!res.ok) {
    throw Object.assign(new Error(`Anthropic API returned HTTP ${res.status}: ${text.slice(0, 300)}`),
      { code: "api_error", status: res.status });
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch (e) {
    throw Object.assign(new Error("Anthropic API returned invalid JSON"), { code: "bad_response" });
  }
  const block = (body.content || []).find((b) => b.type === "tool_use" && b.name === toolName);
  if (!block) throw Object.assign(new Error("Anthropic API did not return the expected tool_use block"), { code: "bad_response" });
  return block.input;
}

module.exports = { configured, classify, model, baseUrl };
