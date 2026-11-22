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

  // a mis-formatted TELEGRAM_ENGINEERING_CHAT_ID (as pasted into Railway Variables: trailing
  // space/newline, or surrounding quotes) must neither disable the guard nor break routing - both go
  // through the same normalised value (server/telegram/envIds.js)
  const access = require("../../server/telegram/access");
  const envIds = require("../../server/telegram/envIds");
  for (const raw of [`${ENG_CHAT} `, `${ENG_CHAT}\n`, ` ${ENG_CHAT}`, `"${ENG_CHAT}"`, `'${ENG_CHAT}'`, ` "${ENG_CHAT}" `]) {
    process.env.TELEGRAM_ENGINEERING_CHAT_ID = raw;
    await expectRefused(botApi.sendMessage(ENG_CHAT, "must not be sent"));
    await expectRefused(botApi.call("sendMessage", { chat_id: Number(ENG_CHAT), text: "x" }));
    await expectRefused(botApi.call("sendPhoto", { chat_id: ENG_CHAT, photo: "x" }));
    const r = access.resolveChannel({ chat: { id: Number(ENG_CHAT) }, from: { id: 501 } });
    assert.deepStrictEqual([r.channel, r.allowed], ["engineering", true], `routing broken for ${JSON.stringify(raw)}`);
    assert.ok(envIds.warnings().some((w) => w.startsWith("TELEGRAM_ENGINEERING_CHAT_ID")), `no warning for ${JSON.stringify(raw)}`);
  }
  results.misformatted_eng_chat_id_still_refused_and_routed = true;

  // a clean value produces no warning
  process.env.TELEGRAM_ENGINEERING_CHAT_ID = ENG_CHAT;
  assert.deepStrictEqual(envIds.warnings(), []);
  results.clean_value_no_warning = true;

  // only ONE pair of quotes is stripped, and mismatched quotes are left alone - those values must
  // not match (and are flagged/left as is), rather than being "fixed" by guesswork
  assert.strictEqual(envIds.normalizeId(`""${ENG_CHAT}""`), `"${ENG_CHAT}"`);
  assert.strictEqual(envIds.normalizeId(`"${ENG_CHAT}'`), `"${ENG_CHAT}'`);
  results.only_one_matching_quote_pair_stripped = true;

  // TELEGRAM_ENGINEERING_ALLOWED_USER_IDS entries are normalised the same way
  process.env.TELEGRAM_ENGINEERING_ALLOWED_USER_IDS = `"501", '502' , 503 `;
  for (const uid of [501, 502, 503]) {
    assert.strictEqual(access.resolveChannel({ chat: { id: Number(ENG_CHAT) }, from: { id: uid } }).allowed, true, `user ${uid}`);
  }
  assert.strictEqual(access.resolveChannel({ chat: { id: Number(ENG_CHAT) }, from: { id: 999 } }).allowed, false);
  assert.ok(envIds.warnings().some((w) => w.startsWith("TELEGRAM_ENGINEERING_ALLOWED_USER_IDS")));
  process.env.TELEGRAM_ENGINEERING_ALLOWED_USER_IDS = `"501,502"`;
  assert.deepStrictEqual(envIds.engineeringAllowedUserIds(), ["501", "502"]);
  process.env.TELEGRAM_ENGINEERING_ALLOWED_USER_IDS = "501, 502";
  assert.deepStrictEqual(envIds.warnings(), [], "plain spaces after commas were always fine - no warning");
  delete process.env.TELEGRAM_ENGINEERING_ALLOWED_USER_IDS;
  results.allowed_user_ids_normalised = true;

  // owner ids too
  process.env.TELEGRAM_OWNER_CHAT_ID = ` "${OWNER_CHAT}"`;
  process.env.TELEGRAM_OWNER_USER_ID = `${OWNER_CHAT} `;
  const o = access.resolveChannel({ chat: { id: Number(OWNER_CHAT) }, from: { id: Number(OWNER_CHAT) } });
  assert.deepStrictEqual([o.channel, o.allowed], ["personal", true]);
  delete process.env.TELEGRAM_OWNER_CHAT_ID;
  delete process.env.TELEGRAM_OWNER_USER_ID;
  results.owner_ids_normalised = true;

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
