#!/usr/bin/env node
"use strict";

// PreToolUse guard for the `shturman-ko-analyst` subagent (.claude/agents/shturman-ko-analyst.md),
// registered in .claude/settings.json and active only for that subagent's tool calls.
// Technical enforcement of the agent's hard rules, so they do not depend on the model obeying its
// prompt (or on a chat message that tries to talk it out of them):
//   * the КО Telegram chat is read-only: no call to the Telegram Bot API at all;
//   * no paid/external LLM API (Anthropic API, Yandex AI Studio, OpenAI, ...);
//   * no writes to Доска проектов except the proposals queue (POST /api/agent/ko/proposals) and the
//     processing cursor (PUT /api/agent/ko/cursor);
//   * no deploy / Railway CLI / git push / Volume /data / shturman.json access;
//   * files may be written only inside .ko-analyst/.
// Contract (Claude Code hooks): JSON on stdin; exit 0 = allow, exit 2 = block (stderr shown to the
// model). Pure function `check()` is exported for tests (tests/test_ko_analyst.py).

const path = require("path");

const DENY_BASH = [
  [/api\.telegram\.org|\bt\.me\/bot|telegram\.org\/bot/i, "Чат КО только для чтения: обращения к Telegram Bot API запрещены"],
  [/\b(send(Message|Photo|Document|MediaGroup|Video|Audio|Voice|Sticker|ChatAction|Poll|Location|Contact)|forwardMessage|copyMessage|editMessage\w*|deleteMessage|setMessageReaction|pinChatMessage|setWebhook|deleteWebhook|getUpdates)\b/,
    "Методы Telegram Bot API запрещены (только чтение через «Штурман»)"],
  [/\b\d{8,10}:[A-Za-z0-9_-]{30,}\b/, "В команде похоже на токен Telegram-бота — запрещено"],
  [/api\.anthropic\.com|ANTHROPIC_API_KEY|aiClient|\bllm\.api\.cloud\.yandex\.net|\bai\.api\.cloud\.yandex\.net|foundationModels|api\.openai\.com|generativelanguage\.googleapis\.com|api\.mistral\.ai|api\.deepseek\.com|openrouter\.ai/i,
    "Внешние LLM API запрещены: анализ выполняется самим Claude Code"],
  [/(^|[\s;&|(])railway(\s|$)/, "Railway CLI запрещён: никаких деплоев и изменения переменных/Volume"],
  [/\bgit\s+(push|commit|reset|rebase|merge|checkout|restore|clean|stash)\b/, "Агент не меняет репозиторий"],
  [/(^|[\s'"=:(])\/data(\/|\s|$|['"])/, "Volume /data недоступен агенту"],
  [/shturman\.json|\.shturman\.lock/, "Прямой доступ к файлу данных «Штурмана» запрещён"],
  [/\b(npm\s+(start|run)|node\s+(server\/|scripts\/)|python3?\s+-m\s+plan_agent|plan_agent\/)/, "Запуск сервера, скриптов миграции и Excel-агента запрещён"],
  [/\b(rm\s+-rf?\s+[^.]|chmod|chown|sudo)\b/, "Опасная системная команда"]
];

const HTTP_CLIENT = /\b(curl|wget|http|httpie|Invoke-WebRequest|fetch\()/i;
const MUTATING = /(-X\s*['"]?(POST|PUT|PATCH|DELETE)\b|--request\s+['"]?(POST|PUT|PATCH|DELETE)\b|\s(-d|--data[\w-]*|-F|--form[\w-]*|-T|--upload-file|--json)(\s|=)|method\s*:\s*['"](POST|PUT|PATCH|DELETE))/i;
const ALLOWED_WRITE_PATH = /\/api\/agent\/ko\/(proposals|cursor)(\?|['"\s]|$)/;
const AGENT_PATH = /\/api\/(agent|agent-jobs|plan-source|plan-imports|telegram|projects|tasks|globals|review-queue)\b/;
const ALLOWED_READ_PATH = /\/api\/agent\/ko\/(messages|files|projects|proposals|cursor)\b/;

function checkBash(command) {
  const cmd = String(command || "");
  for (const [rx, reason] of DENY_BASH) if (rx.test(cmd)) return reason;
  if (HTTP_CLIENT.test(cmd)) {
    const urls = cmd.match(/https?:\/\/[^\s'"]+/g) || [];
    if (MUTATING.test(cmd) && !(urls.length && urls.every((u) => ALLOWED_WRITE_PATH.test(u)))) {
      return "Запись разрешена только в очередь предложений (POST /api/agent/ko/proposals) и курсор (PUT /api/agent/ko/cursor)";
    }
    for (const u of urls) {
      if (AGENT_PATH.test(u) && !ALLOWED_READ_PATH.test(u)) {
        return `Разрешены только эндпоинты /api/agent/ko/*; запрошено: ${u.replace(/\?.*/, "")}`;
      }
    }
  }
  return null;
}

function checkWrite(filePath, projectDir) {
  const root = path.resolve(projectDir || process.cwd());
  const allowed = path.join(root, ".ko-analyst") + path.sep;
  const target = path.resolve(root, String(filePath || ""));
  if (!target.startsWith(allowed)) return `Запись файлов разрешена только в .ko-analyst/ (запрошено: ${target})`;
  return null;
}

const AGENT = "shturman-ko-analyst";

/** Registered project-wide in .claude/settings.json (frontmatter hooks of subagents were found not
 *  to run in Claude Code cloud sessions), so it sees every tool call of every session in this repo.
 *  It enforces the rules only for calls made by the shturman-ko-analyst subagent - Claude Code puts
 *  `agent_type` into the hook input for subagent calls and omits it for the main session. */
function appliesTo(input) {
  return !!input && input.agent_type === AGENT;
}

function check(input, projectDir) {
  if (!appliesTo(input)) return null;
  const tool = input && input.tool_name;
  const ti = (input && input.tool_input) || {};
  if (tool === "Bash") return checkBash(ti.command);
  if (tool === "Write" || tool === "Edit" || tool === "NotebookEdit") return checkWrite(ti.file_path || ti.notebook_path, projectDir);
  return null;
}

module.exports = { check, checkBash, checkWrite, appliesTo, AGENT };

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => { raw += c; });
  process.stdin.on("end", () => {
    let input;
    try {
      input = JSON.parse(raw || "{}");
    } catch (e) {
      process.stderr.write("ko-analyst-guard: не удалось разобрать вход хука — действие заблокировано\n");
      process.exit(2);
    }
    const reason = check(input, process.env.CLAUDE_PROJECT_DIR || input.cwd);
    if (reason) {
      process.stderr.write(`ko-analyst-guard: ЗАБЛОКИРОВАНО — ${reason}\n`);
      process.exit(2);
    }
    process.exit(0);
  });
}
