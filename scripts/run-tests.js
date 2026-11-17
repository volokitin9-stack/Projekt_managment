"use strict";
// npm test: runs pytest with the same interpreter the application uses for the
// planning agent (PLAN_PYTHON_EXECUTABLE, project .venv, verified system Python),
// so it also works on Windows where "python3" may be the Microsoft Store alias.
require("../server/env");   // .env from the project folder, backslashes kept literally
const { spawnSync } = require("child_process");
const { getPython } = require("../server/pythonResolver");

const py = getPython();
if (!py.ok) {
  console.error(`${py.error}\nПроверено: ${py.attempted.join("; ")}`);
  process.exit(2);
}
console.log(`pytest via ${py.executable} (${py.source})`);
const r = spawnSync(py.executable, ["-m", "pytest", "-q"].concat(process.argv.slice(2)),
  { stdio: "inherit", shell: false, cwd: require("path").join(__dirname, "..") });
process.exit(r.status == null ? 1 : r.status);
