"use strict";
// Browser check of the readable journal + stage-duration section on a project card, for the
// administrator and a ko_viewer manager (run by tests/test_history_journal.py).
// Usage: node tests/js/history_journal_browser.js <server url> <project id> <session_cookie>
// session_cookie: "shturman_session=<value>" - admin or manager, same script either way.
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
const projectId = process.argv[3];
const cookieArg = process.argv[4] || "";

(async () => {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (e) {
    console.log("chromium not available: " + e.message);
    process.exit(3);
  }
  const context = await browser.newContext({ viewport: { width: 1360, height: 900 } });
  if (cookieArg) {
    const eq = cookieArg.indexOf("=");
    await context.addCookies([{ name: cookieArg.slice(0, eq), value: cookieArg.slice(eq + 1), url }]);
  }
  const page = await context.newPage();
  const D3_STUB = "(function(){var f=new Proxy(function(){},{get:function(t,k){" +
    "if(k===Symbol.toPrimitive)return function(){return 0;};if(k===Symbol.iterator)return [][Symbol.iterator];" +
    "if(k==='length')return 0;return f;},apply:function(){return f;},construct:function(){return f;}});window.d3=f;})();";
  await page.route(/cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com/, (r) =>
    r.fulfill({ status: 200, contentType: "application/javascript", body: D3_STUB }));
  await page.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.fulfill({ status: 200, body: "" }));
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto(url + "/?eng=" + encodeURIComponent(projectId), { waitUntil: "networkidle" });
  await page.waitForSelector("#detailPanel.open", { timeout: 15000 });

  const result = {};
  result.stage_section_text = await page.locator("#treeDetailBody").innerText();
  const lower = result.stage_section_text.toLowerCase();
  result.has_stage_duration_heading = lower.includes("длительность по стадиям");
  result.has_history_heading = lower.includes("история проекта");

  // every change line under "Фактические часы" style rows must show a plain value, never braces
  // or quotes that would betray a raw JSON dump
  result.looks_like_raw_json = /[{[]"[a-z_]+"\s*:/i.test(result.stage_section_text);

  // Expand every outer event <details> first - the per-change lines (and the inner diagnostic
  // toggle, if any) are only in the DOM's accessible/visible tree once their own <details> is open.
  await page.evaluate(() => {
    document.querySelectorAll("#treeDetailBody details.eng-hist-event").forEach((d) => { d.open = true; });
  });
  result.visible_change_labels = await page.locator(".eng-hist-change .k").allInnerTexts();
  result.technical_markers = await page.locator(".eng-hist-change.technical .v").allInnerTexts();
  result.admin_raw_toggle_count = await page.locator(".eng-hist-raw summary").count();
  if (result.admin_raw_toggle_count) {
    await page.evaluate(() => {
      document.querySelectorAll("#treeDetailBody details.eng-hist-raw").forEach((d) => { d.open = true; });
    });
    result.raw_json_after_click = await page.locator(".eng-hist-raw pre").first().innerText();
  }

  result.page_errors = errors;
  console.log(JSON.stringify(result));
  await browser.close();
})().catch(async (e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
