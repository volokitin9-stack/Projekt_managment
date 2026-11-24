"use strict";
// Defensive-only check: server/usersModel.js's setActive() refuses to disable the last active
// "admin"-role row, even though no "users" row ever gets that role through the real create()
// API (always hardcoded to "ko_viewer" - see that module's own header comment). Exercises the
// guard directly, since there is no HTTP path that can produce an admin-role row to test through.
// Run by tests/test_user_accounts.py. Usage: node tests/js/users_last_admin_unit.js <scratch dir>
const path = require("path");
process.env.DATA_DIR = path.join(process.argv[2], "data");

const store = require("../../server/store");
const users = require("../../server/usersModel");

(async () => {
  const created = await users.create({ full_name: "Admin Row", username: "adminrow" }, "test");
  store.tx((s) => { s.users.find((u) => u.id === created.user.id).role = "admin"; });
  const result = users.setActive(created.user.id, false, "test");
  console.log(JSON.stringify(result));
  process.exit(result.error === "last_admin" ? 0 : 1);
})().catch((e) => {
  console.error(e && e.stack || e);
  process.exit(2);
});
