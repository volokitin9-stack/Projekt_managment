"use strict";

// Documented extension point for a *future* LLM-backed classifier (ТЗ §5: "предусмотреть
// архитектуру, позволяющую позднее подключить LLM, не переписывая команды и проверки прав").
//
// Today this is a pure no-op: classify() always returns null synchronously, no network call, no
// API key read, nothing. assistant.js calls it exactly once, only after the rule-based
// intents.classify() has already failed to recognise the message - so turning this into a real
// classifier later changes nothing about how a match gets executed: it must still return the same
// {intent, ...} shape intents.js produces (see its classify()), and every search, confirmation,
// idempotency and owner-only check downstream (assistant.js / personalBot.js) stays exactly as
// it is - an LLM here only ever gets to *propose* an intent, the same as the rule-based matcher;
// it never gets a path that skips confirmation or the role/owner checks already in place.
//
// To actually wire one in later: make classify() call it (keeping the synchronous rule-based path
// first, since that is free and instant), map its output onto the same intent shape, and gate it
// behind its own explicit opt-in env var - never reuse TELEGRAM_ENGINEERING_AI_ENABLED or
// TELEGRAM_PERSONAL_AI_ENABLED (aiClient.js's existing paid-AI flags), so the new path can be
// reviewed and enabled independently of them.

function classify(/* text */) {
  return null;
}

module.exports = { classify };
