#!/usr/bin/env node
// Diagnostic only: append the PreToolUse hook input to .ko-analyst/hook-inputs.jsonl and allow.
const fs = require("fs"); const path = require("path");
let raw = ""; process.stdin.on("data", (c) => { raw += c; });
process.stdin.on("end", () => {
  try {
    const dir = path.join(process.cwd(), ".ko-analyst"); fs.mkdirSync(dir, { recursive: true });
    const j = JSON.parse(raw || "{}");
    fs.appendFileSync(path.join(dir, "hook-inputs.jsonl"), JSON.stringify({ keys: Object.keys(j), agent_id: j.agent_id || null,
      agent_type: j.agent_type || null, tool_name: j.tool_name, cwd: j.cwd, env_project_dir: process.env.CLAUDE_PROJECT_DIR || null }) + "\n");
  } catch (e) { /* never block */ }
  process.exit(0);
});
