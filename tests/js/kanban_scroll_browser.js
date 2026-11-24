"use strict";
// Browser check that scrolling a Kanban column (public/index.html's #engBoard, "Конструкторский
// отдел") survives the board's auto-refresh (run by tests/test_kanban_scroll.py).
// Usage: node tests/js/kanban_scroll_browser.js <server url> [session_cookie] [stage]
//   session_cookie: "shturman_session=<value>" to run as a logged-in manager/admin, or "" for the
//   no-auth default (acts as administrator).
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
const cookieArg = process.argv[3] || "";
const stage = process.argv[4] || "scanning";

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
  // External CDN scripts are not reachable in CI: serve a chainable no-op D3 stub (the Kanban
  // board does not use D3; only the «Глобально» map does).
  const D3_STUB = "(function(){var f=new Proxy(function(){},{get:function(t,k){" +
    "if(k===Symbol.toPrimitive)return function(){return 0;};if(k===Symbol.iterator)return [][Symbol.iterator];" +
    "if(k==='length')return 0;return f;},apply:function(){return f;},construct:function(){return f;}});window.d3=f;})();";
  await page.route(/cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com/, (r) =>
    r.fulfill({ status: 200, contentType: "application/javascript", body: D3_STUB }));
  await page.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.fulfill({ status: 200, body: "" }));
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto(url + "/", { waitUntil: "networkidle" });
  await page.click("#tabEngBtn");

  const colSel = '.eng-col[data-stage="' + stage + '"] .eng-col-body';
  await page.waitForSelector(colSel, { timeout: 15000 });
  await page.waitForFunction((sel) => {
    const el = document.querySelector(sel);
    return !!el && el.scrollHeight > el.clientHeight + 20;
  }, colSel, { timeout: 15000 });

  const result = {};
  result.draggable = await page.locator(colSel + " .eng-card").first().getAttribute("draggable");

  // Scroll the column to its middle, exactly as a reader browsing the list would.
  await page.evaluate((sel) => {
    document.querySelector(sel).scrollTop = Math.floor(document.querySelector(sel).scrollHeight / 2);
  }, colSel);
  result.scroll_set = await page.evaluate((sel) => document.querySelector(sel).scrollTop, colSel);

  // Force several refresh cycles - #refreshBtn calls the same loadAll() the 20s poll timer does,
  // without waiting out the real interval. Clicked via the DOM API (not a simulated pointer
  // click), so it also works for a ko_viewer session where the board tab (and this button along
  // with it) is hidden.
  result.after_refresh = [];
  for (let i = 0; i < 3; i++) {
    await page.evaluate(() => document.getElementById("refreshBtn").click());
    await page.waitForTimeout(400);
    result.after_refresh.push(await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      return el ? el.scrollTop : null;
    }, colSel));
  }

  // Open a project card, close it again, then refresh once more - the column must still be where
  // the reader left it. Clicked via the DOM API, like #refreshBtn above: a real Playwright pointer
  // click would first scroll the (possibly off-screen, since we just scrolled the column ourselves)
  // card into view, which is not the app behaviour under test here.
  await page.evaluate((sel) => document.querySelector(sel).click(), colSel + ' .eng-card[data-action="open-eng-project"]');
  await page.waitForSelector("#detailPanel.open", { timeout: 5000 });
  await page.evaluate(() => document.getElementById("treeDetailCloseBtn").click());
  await page.waitForTimeout(100);
  await page.evaluate(() => document.getElementById("refreshBtn").click());
  await page.waitForTimeout(400);
  result.after_detail_cycle = await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    return el ? el.scrollTop : null;
  }, colSel);

  result.page_errors = errors;
  console.log(JSON.stringify(result));
  await browser.close();
})().catch(async (e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
