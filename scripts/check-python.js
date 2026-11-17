"use strict";
// npm run check-python - real check of the planning agent's interpreter on this machine.
// Uses exactly what the server uses: .env from the project folder (server/env.js), the
// resolver (server/pythonResolver.js) and spawn(..., {shell: false}). Prints no secrets.
const { spawnSync } = require("child_process");
const envInfo = require("../server/env").loaded;
const { getPython, REPO_ROOT } = require("../server/pythonResolver");

let failed = false;
const line = (k, v) => console.log(`${k.padEnd(22)} ${v}`);
line("Платформа", `${process.platform} ${process.arch}, Node ${process.version}`);
line("Папка проекта", REPO_ROOT);
line(".env", envInfo.file ? `${envInfo.file} (переменных: ${envInfo.keys.length})` : "не найден (используются переменные окружения)");
const py = getPython(true);
(py.warnings || []).forEach((w) => line("ВНИМАНИЕ", w));
line("Проверенные пути", py.attempted.join(" | ") || "—");
if (!py.ok) {
  line("РЕЗУЛЬТАТ", `ОШИБКА: ${py.error}`);
  process.exit(2);
}
line("Python агента", `${py.executable}  [${py.source}]`);

function run(title, args) {
  const r = spawnSync(py.executable, args, { cwd: REPO_ROOT, shell: false, windowsHide: true, encoding: "utf-8",
    timeout: 60000, env: Object.assign({}, process.env, { PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" }) });
  const out = `${r.stdout || ""}${r.stderr || ""}`.trim().split(/\r?\n/).slice(-3).join(" / ");
  if (r.error || r.status !== 0) {
    failed = true;
    line(title, `ОШИБКА (${r.error ? r.error.code : `exit ${r.status}`}) ${out}`);
  } else {
    line(title, `OK  ${out}`);
  }
}
run("Интерпретатор", ["-c", "import sys; print(sys.executable); print(sys.version.split()[0])"]);
run("Библиотеки", ["-c", "import yaml, openpyxl; print('yaml + openpyxl OK')"]);
run("Агент plan_agent", ["-m", "plan_agent", "--help"]);
line("РЕЗУЛЬТАТ", failed ? "ОШИБКА — см. строки выше" : "Python-агент запускается: OK");
process.exit(failed ? 1 : 0);
