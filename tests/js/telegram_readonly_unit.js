"use strict";
// Critical requirement #1 ("Дополнение к ТЗ Telegram-интеграции «Штурмана»"): the engineering chat
// is strictly read-only at the server level - no outbound Telegram API call may ever be addressed
// to it, from any method, whether or not that method exists in the codebase today. Run by
// tests/test_telegram.py. Usage: node tests/js/telegram_readonly_unit.js <fake telegram base url>
const assert = require("assert");

const ENG_CHAT = "-2000";
const OWNER_CHAT = "1000";

process.env.TELEGRAM_BOT_TOKEN = "tg-bot-test-key";
process.env.TELEGRAM_ENGINEERING_CHAT_ID = ENG_CHAT;
process.env.TELEGRAM_API_BASE_URL = process.argv[2];

const botApi = require("../../server/telegram/botApi");

async function expectRefused(promise) {
  try {
    await promise;
  } catch (e) {
    assert.strictEqual(e.code, "engineering_chat_is_read_only", `wrong error code: ${e.code} (${e.message})`);
    return;
  }
  throw new Error("expected the call to be refused, but it resolved");
}

const results = {};

(async () => {
  // sendMessage, the one real call site today
  await expectRefused(botApi.sendMessage(ENG_CHAT, "hello"));
  results.sendMessage_refused = true;

  // any other outbound method, present or hypothetical, addressed to the same chat
  await expectRefused(botApi.call("sendPhoto", { chat_id: ENG_CHAT, photo: "x" }));
  await expectRefused(botApi.call("sendDocument", { chat_id: ENG_CHAT, document: "x" }));
  await expectRefused(botApi.call("sendAnimation", { chat_id: ENG_CHAT, animation: "x" }));
  await expectRefused(botApi.call("anyFutureMethodNobodyHasWrittenYet", { chat_id: ENG_CHAT }));
  results.every_outbound_method_refused = true;

  // numeric vs string chat_id must both be caught (Telegram updates give numbers; env vars are strings)
  await expectRefused(botApi.call("sendMessage", { chat_id: Number(ENG_CHAT), text: "x" }));
  results.numeric_chat_id_refused = true;

  // getFile/downloadFile (reading, not sending) must still work - this is a read-only chat, not an
  // unreachable one; only outbound SENDS are banned
  const info = await botApi.call("getFile", { file_id: "f1" });
  assert.strictEqual(info.file_path, "photos/f1.jpg");
  results.getFile_still_allowed = true;

  // the owner chat must be entirely unaffected by the guard
  const sent = await botApi.sendMessage(OWNER_CHAT, "hi owner");
  assert.ok(sent && sent.message_id);
  results.owner_chat_unaffected = true;

  // a call with no chat_id at all (e.g. getMe) must not be mistaken for a banned call
  await botApi.call("getFile", { file_id: "f2" });
  results.no_chat_id_not_mistaken_for_banned = true;

  // if TELEGRAM_ENGINEERING_CHAT_ID is unset, there is nothing to compare against - no guard, no crash
  delete process.env.TELEGRAM_ENGINEERING_CHAT_ID;
  const sent2 = await botApi.sendMessage(ENG_CHAT, "no eng chat id configured");
  assert.ok(sent2 && sent2.message_id);
  results.unset_env_var_does_not_crash = true;

  console.log(JSON.stringify(results));
  process.exit(0);
})().catch((e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
