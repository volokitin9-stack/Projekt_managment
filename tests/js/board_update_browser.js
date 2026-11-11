"use strict";
// Browser check of the board's «Обновить» button (run by tests/test_schedule.py).
// Usage: node tests/js/board_update_browser.js <server url>
// Exit code 3 = Playwright/Chromium not available (test is skipped).
let chromium;
try {
  ({ chromium } = require("playwright"));
} catch (e) {
  try {
    ({ chromium } = require(require("child_process").execSync("npm root -g").toString().trim() + "/playwright"));
  } catch (e2) {
    console.log("playwright not installed");
    process.exit(3);
  }
}

const url = process.argv[2];

(async () => {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (e) {
    console.log("chromium not available: " + e.message);
    process.exit(3);
  }
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  // External CDN scripts are not reachable in CI: serve a chainable no-op D3 stub (the Kanban board
  // does not use D3; only the «Глобально» map does).
  const D3_STUB = "(function(){var f=new Proxy(function(){},{get:function(t,k){" +
    "if(k===Symbol.toPrimitive)return function(){return 0;};if(k===Symbol.iterator)return [][Symbol.iterator];" +
    "if(k==='length')return 0;return f;},apply:function(){return f;},construct:function(){return f;}});window.d3=f;})();";
  await page.route(/cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com/, (r) =>
    r.fulfill({ status: 200, contentType: "application/javascript", body: D3_STUB }));
  await page.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.fulfill({ status: 200, body: "" }));
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  let projectLoads = 0;
  let clicked = false;
  let reloadedAfterClick = false;
  page.on("request", (req) => {
    if (/\/api\/projects(\?|$)/.test(req.url())) {
      projectLoads += 1;
      if (clicked) reloadedAfterClick = true;
    }
  });
  await page.goto(url + "/", { waitUntil: "networkidle" });
  await page.click("#tabEngBtn");
  const btn = page.locator("#engUpdateBtn");
  await btn.waitFor({ state: "visible" });
  await page.waitForFunction(() => /Последняя проверка/.test(document.getElementById("engUpdateStatus").textContent));
  const result = {
    initial: (await btn.textContent()).trim(),
    toolbar_has_button: await page.locator(".eng-toolbar #engUpdateBtn").count() === 1,
    kanban_columns: await page.locator("#engBoardView .eng-col, #engBoardView [data-stage-col], #engBoardView .kanban-col").count()
  };
  // slow the update response down a little so the busy state is observable
  await page.route("**/api/plan-source/update", async (r) => {
    await new Promise((res) => setTimeout(res, 600));
    r.continue();
  });
  clicked = true;
  await btn.click();
  result.during = (await btn.textContent()).trim();
  result.disabled_during = await btn.isDisabled();
  await page.waitForFunction(() => document.getElementById("engUpdateBtn").getAttribute("data-state") !== "busy",
    null, { timeout: 120000 });
  result.final = (await btn.textContent()).trim();
  result.note = (await page.locator("#engUpdateNote").textContent()).trim();
  await page.waitForTimeout(500);
  result.projects_reloaded_after_click = reloadedAfterClick;
  result.status_text = (await page.locator("#engUpdateStatus").textContent()).trim();
  await page.screenshot({ path: process.env.BOARD_SHOT || "/dev/null", fullPage: false }).catch(() => {});
  // second click: the file is unchanged now
  await page.evaluate(() => { document.getElementById("engUpdateBtn").disabled = false; });
  await btn.click();
  await page.waitForFunction(() => document.getElementById("engUpdateBtn").getAttribute("data-state") !== "busy",
    null, { timeout: 120000 });
  result.second = (await btn.textContent()).trim();
  result.page_errors = errors;
  console.log(JSON.stringify(result));
  await browser.close();
})().catch(async (e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
