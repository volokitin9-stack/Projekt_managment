"use strict";
// Unit tests for the shturman-ko-analyst guard hook and ledger. Run by tests/test_ko_analyst.py.
// Usage: node tests/js/ko_analyst_unit.js <empty temp dir>
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const tmp = path.resolve(process.argv[2]);
process.env.KO_ANALYST_DIR = path.join(tmp, ".ko-analyst");
const root = path.join(__dirname, "..", "..");
const guard = require(path.join(root, ".claude/hooks/ko-analyst-guard.js"));
const ledger = require(path.join(root, "tools/ko_analyst/ledger.js"));

const B = "https://shturman.example/api/agent/ko";
const blocked = [
  `curl -s https://api.telegram.org/bot123/sendMessage -d chat_id=-100 -d text=hi`,
  `curl https://api.telegram.org/botXYZ/getUpdates`,
  `node -e "fetch('https://x/' + 'sendMessage')"`,
  `curl -H "x-api-key: $ANTHROPIC_API_KEY" https://api.anthropic.com/v1/messages`,
  `curl https://llm.api.cloud.yandex.net/foundationModels/v1/completion -d '{}'`,
  `curl https://api.openai.com/v1/chat/completions`,
  `curl -X POST https://shturman.example/api/agent/projects -d '{}'`,
  `curl -X POST https://shturman.example/api/agent/projects/p1/stage-transitions -d '{}'`,
  `curl -X PATCH ${B}/proposals/1 https://shturman.example/api/agent/projects/1 -d '{}'`,
  `curl https://shturman.example/api/agent/projects`,
  `curl https://shturman.example/api/telegram/log`,
  `curl -X POST https://shturman.example/api/telegram/webhook/connect`,
  `railway up`,
  `railway variables --set A=1`,
  `git push origin main`,
  `cat /data/shturman.json`,
  `ls /data`,
  `npm start`,
  `node server/index.js`,
  `python -m plan_agent sync`,
  `echo 1234567890:AAAAfakeFAKEfakeFAKEfakeFAKEfake0000`
];
const allowed = [
  `curl -s -H "Authorization: Bearer $KO_ANALYST_API_KEY" "${B}/messages?after=10&limit=200"`,
  `curl -s -H "Authorization: Bearer $KO_ANALYST_API_KEY" ${B}/files/abc -o .ko-analyst/files/abc.jpg`,
  `curl -s -X POST -H "Authorization: Bearer $KO_ANALYST_API_KEY" -H "Content-Type: application/json" --data @.ko-analyst/out/proposals.new.json ${B}/proposals`,
  `node tools/ko_analyst/ledger.js merge .ko-analyst/out/proposals.json`,
  `jq '.messages | length' .ko-analyst/inbox/messages.json`,
  `ls .ko-analyst/inbox/photos`
];
for (const c of blocked) assert.ok(guard.checkBash(c), `must block: ${c}`);
for (const c of allowed) assert.strictEqual(guard.checkBash(c), null, `must allow: ${c}`);
assert.ok(guard.checkWrite("server/index.js", tmp));
assert.ok(guard.checkWrite("/data/shturman.json", tmp));
assert.ok(guard.checkWrite(".ko-analyst/../server/x.js", tmp));
assert.strictEqual(guard.checkWrite(".ko-analyst/out/proposals.json", tmp), null);
assert.ok(guard.check({ tool_name: "Edit", tool_input: { file_path: "README.md" } }, tmp));

// the hook as a process: exit 2 + stderr on block, exit 0 on allow
const hook = path.join(root, ".claude/hooks/ko-analyst-guard.js");
const run = (obj) => spawnSync(process.execPath, [hook], { input: JSON.stringify(obj), env: Object.assign({}, process.env, { CLAUDE_PROJECT_DIR: tmp }) });
let r = run({ tool_name: "Bash", tool_input: { command: blocked[0] } });
assert.strictEqual(r.status, 2); assert.match(r.stderr.toString(), /ЗАБЛОКИРОВАНО/);
r = run({ tool_name: "Bash", tool_input: { command: allowed[0] } });
assert.strictEqual(r.status, 0);
r = run({ tool_name: "Write", tool_input: { file_path: path.join(tmp, "server.js") } });
assert.strictEqual(r.status, 2);
r = spawnSync(process.execPath, [hook], { input: "not json" });
assert.strictEqual(r.status, 2, "unparseable input must fail closed");

// ledger: links
assert.strictEqual(ledger.link("-1001234567890", 4821), "https://t.me/c/1234567890/4821");
assert.strictEqual(ledger.link("-4000", 7), "tg-message:-4000:7");

// ledger: validation
const ev = (id, q) => ({ chat_id: "-1001234567890", message_id: id, date: "2026-11-20T09:14:00Z", author: "Иванов", quote: q });
const stage = { kind: "stage_change", project_ref: { manufacturer: "HAVAL", vehicle_model: "Jolion", product_type: "Защита радиатора" },
  change: { field: "status", from: "development", to: "prototype" }, summary: "Образец установлен",
  evidence: [ev(4821, "поставили образец на джолион")], rationale: "явно сказано", confidence: "high", requires_approval: false };
const work = { kind: "work_done", project_ref: { article_number: "333.0544.1" }, summary: "Замер выполнен",
  evidence: [ev(4830, "замер по 333.0544.1 сделали")], rationale: "артикул в тексте", confidence: "high" };
const bad = [
  Object.assign({}, stage, { evidence: [] }),
  Object.assign({}, stage, { change: { field: "status", to: "flying" } }),
  Object.assign({}, work, { confidence: "low" }),
  Object.assign({}, work, { project_ref: {} }),
  Object.assign({}, work, { rationale: "" })
];
bad.forEach((p, i) => assert.ok(ledger.validate(p, i).length, `invalid #${i} must fail`));
assert.deepStrictEqual(ledger.validate(stage, 0), []);

// ledger: idempotent merge
fs.mkdirSync(path.join(tmp, "in"), { recursive: true });
const f1 = path.join(tmp, "in", "p1.json");
fs.writeFileSync(f1, JSON.stringify([stage, work]));
let m = ledger.merge(f1);
assert.ok(m.ok); assert.deepStrictEqual(m.stats, { input: 2, new: 2, updated: 0, duplicate: 0 });
let out = JSON.parse(fs.readFileSync(m.written, "utf8")).proposals;
assert.strictEqual(out[0].requires_approval, true, "stage_change is always gated");
assert.strictEqual(out[0].evidence[0].link, "https://t.me/c/1234567890/4821");
const key = out[0].dedupe_key;

m = ledger.merge(f1); // re-run on the same input
assert.deepStrictEqual(m.stats, { input: 2, new: 0, updated: 0, duplicate: 2 });
assert.strictEqual(JSON.parse(fs.readFileSync(m.written, "utf8")).proposals.length, 0, "re-run emits nothing");

// same fact, worded/cased differently, with an extra source message -> same key, evidence merged
const f2 = path.join(tmp, "in", "p2.json");
fs.writeFileSync(f2, JSON.stringify([Object.assign({}, stage, {
  project_ref: { manufacturer: "haval", vehicle_model: "JOLION", product_type: "защита  радиатора" },
  evidence: [ev(4821, "поставили образец на джолион"), ev(4840, "образец стоит, всё ок")] })]));
m = ledger.merge(f2);
assert.deepStrictEqual(m.stats, { input: 1, new: 0, updated: 1, duplicate: 0 });
out = JSON.parse(fs.readFileSync(m.written, "utf8")).proposals;
assert.strictEqual(out[0].dedupe_key, key);
assert.strictEqual(out[0].evidence.length, 2);
assert.strictEqual(ledger.seen("-1001234567890", 4840), "seen");
assert.strictEqual(ledger.seen("-1001234567890", 9999), "new");

console.log(JSON.stringify({ ok: true, blocked: blocked.length, allowed: allowed.length }));
