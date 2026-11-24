"use strict";

// "Пользователи" admin section - критическое дополнение к ТЗ "Разграничение доступа к «Доске
// проектов»". Admin-only (server/index.js mounts this router behind both
// dashboardAuth.requireAdminRole and, for every mutation, requireCsrfToken - see that module's
// header for why both apply). See server/usersModel.js for the data model and
// docs/USER_ACCOUNTS.md for the overall design.

const express = require("express");
const dashboardAuth = require("../dashboardAuth");
const usersModel = require("../usersModel");

const router = express.Router();

function actorOf(req) {
  return (req.authUser && req.authUser.username) || "admin";
}

router.get("/", (req, res) => {
  res.json(usersModel.list());
});

router.get("/audit", (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 200, 1000);
  res.json({ entries: usersModel.recentAudit(limit) });
});

router.post("/", dashboardAuth.requireCsrfToken, async (req, res) => {
  const body = req.body || {};
  const result = await usersModel.create({ full_name: body.full_name, username: body.username, password: body.password },
    actorOf(req));
  if (result.error === "full_name_required") return res.status(400).json({ error: 'full_name_required: "full_name" is required' });
  if (result.error === "username_required") return res.status(400).json({ error: 'username_required: "username" is required' });
  if (result.error === "username_taken") return res.status(409).json({ error: "username_taken: this username is already in use" });
  if (result.error === "weak_password") return res.status(400).json({ error: "weak_password: password must be at least 8 characters, or omitted to generate one" });
  res.status(201).json({ user: result.user, generated_password: result.generated_password });
});

router.patch("/:id", dashboardAuth.requireCsrfToken, (req, res) => {
  const body = req.body || {};
  const result = usersModel.update(req.params.id, { full_name: body.full_name, username: body.username }, actorOf(req));
  if (result.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (result.error === "username_required") return res.status(400).json({ error: 'username_required: "username" is required' });
  if (result.error === "username_taken") return res.status(409).json({ error: "username_taken: this username is already in use" });
  res.json(result.user);
});

router.post("/:id/reset-password", dashboardAuth.requireCsrfToken, async (req, res) => {
  const result = await usersModel.resetPassword(req.params.id, actorOf(req));
  if (result.error === "not_found") return res.status(404).json({ error: "Not found" });
  res.json({ generated_password: result.generated_password });
});

router.post("/:id/disable", dashboardAuth.requireCsrfToken, (req, res) => {
  const result = usersModel.setActive(req.params.id, false, actorOf(req));
  if (result.error === "not_found") return res.status(404).json({ error: "Not found" });
  if (result.error === "last_admin") return res.status(409).json({ error: "last_admin: cannot disable the last active administrator" });
  res.json(result.user);
});

router.post("/:id/activate", dashboardAuth.requireCsrfToken, (req, res) => {
  const result = usersModel.setActive(req.params.id, true, actorOf(req));
  if (result.error === "not_found") return res.status(404).json({ error: "Not found" });
  res.json(result.user);
});

module.exports = router;
