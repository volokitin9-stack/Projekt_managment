"use strict";

// Status and controls of the automatic plan retrieval («Импорт плана» view).
// Like the other dashboard routes, unauthenticated by design of the original
// app - put access control in front of the server before exposing it.

const express = require("express");
const monitor = require("../planSource/monitor");
const scheduler = require("../planSource/scheduler");

function fullStatus() {
  return Object.assign(monitor.status(), { schedule: scheduler.status() });
}

// Button state for the board («Обновить»).
function uiState(run, result) {
  const c = run.counts || {};
  const held = run.held_for_review || 0;
  switch (run.outcome) {
    case "busy":
      return { state: "busy", message: "Обновление уже выполняется — дождитесь завершения" };
    case "unchanged":
      return { state: "no_changes", message: "Изменений нет: файл не изменился" };
    case "synced":
      return { state: "updated", message: `Обновлено: изменено ${c.updated || 0}, без изменений ${c.unchanged || 0}` };
    case "synced_review_required":
      return { state: "review_required", message: `Обновлено ${c.updated || 0}; требуют проверки: ${held} (раздел «На проверке»)` };
    case "review_required":
      return { state: "review_required", message: result.status === "blocked"
        ? "Требуется проверка файла — данные не изменены (см. «Импорт плана»)"
        : "Есть изменения, требующие утверждения — данные не изменены (см. «Импорт плана»)" };
    case "not_configured":
      return { state: "error", message: "Источник файла плана не настроен (PLAN_SOURCE_TYPE / PLAN_SOURCE_PATH)" };
    default:
      return { state: "error", message: `Ошибка обновления: ${run.error || result.status}` };
  }
}

const router = express.Router();

router.get("/status", (req, res) => {
  res.json(fullStatus());
});

// POST /api/plan-source/update - the board's «Обновить» button. Checks the file now, on any day
// and at any time; the same validated pipeline and automatic-sync rules as the scheduled run.
router.post("/update", async (req, res) => {
  const { run, result } = await monitor.runRecorded({ trigger: "manual" });
  const ui = uiState(run, result);
  // Always 200: "busy" and file errors are states of the button, reported in ui.
  res.json(Object.assign({ ui, run, result }, { state: fullStatus() }));
});

router.get("/preview", (req, res) => {
  const p = monitor.preview(req.query.version || null);
  if (!p) return res.status(404).json({ error: "Нет обработанной версии файла" });
  res.json(p);
});

// POST /api/plan-source/check {dry_run: true?} - check the source now.
// dry_run: analyse the current file again even if it is unchanged (never writes).
router.post("/check", async (req, res) => {
  const r = await monitor.check({ trigger: "manual", force: !!(req.body && req.body.dry_run) });
  res.status(r.status === "busy" ? 409 : 200).json(Object.assign({ result: r }, { state: fullStatus() }));
});

// POST /api/plan-source/approve {confirm: true, approved_by} - mode A: approve and sync the pending version.
router.post("/approve", async (req, res) => {
  if (!req.body || req.body.confirm !== true) {
    return res.status(400).json({ error: "Требуется явное подтверждение: {\"confirm\": true}" });
  }
  const by = String(req.body.approved_by || "dashboard_user").slice(0, 80);
  const r = await monitor.approvePending(by);
  const code = r.status === "synced" ? 200 : r.status === "busy" ? 409 : r.status === "nothing_pending" ? 409 : 422;
  res.status(code).json(Object.assign({ result: r }, { state: fullStatus() }));
});

// POST /api/plan-source/auto-sync {enabled: bool, confirm: true}
router.post("/auto-sync", (req, res) => {
  if (!req.body || typeof req.body.enabled !== "boolean" || req.body.confirm !== true) {
    return res.status(400).json({ error: "Ожидается {\"enabled\": true|false, \"confirm\": true}" });
  }
  monitor.setAutoSync(req.body.enabled);
  res.json(fullStatus());
});

// Test hooks: only with PLAN_SCHEDULER_TEST_HOOKS=1 (automated tests); absent in normal operation.
if (scheduler.TEST_HOOKS) {
  router.post("/_test/clock", (req, res) => {
    scheduler.setClock(req.body && req.body.now);
    res.json({ now: scheduler.now().toISOString() });
  });
  router.post("/_test/tick", async (req, res) => {
    const t = req.body && req.body.now ? new Date(req.body.now) : scheduler.now();
    res.json(await scheduler.tick(t));
  });
}

module.exports = router;
