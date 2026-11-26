"use strict";
// Browser check of the КО notification bell (public/index.html's #notifBellWrap), run by
// tests/test_ko_notifications.py. Two modes:
//   node ko_notifications_browser.js <url> admin   <cookie> <stage> <proposal_id>
//   node ko_notifications_browser.js <url> manager <cookie>
// admin mode drives the real bell/panel/click-through UI and proves a Kanban column's scroll
// position survives both the notification panel's own fetch and the tab/subview switch a
// click-through causes. manager mode proves the bell does not exist for a ko_viewer session and
// that the API itself refuses it, even called straight from the page's own origin.
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
const role = process.argv[3];
const cookieArg = process.argv[4] || "";
const stage = process.argv[5] || "scanning";
const proposalId = process.argv[6] || "";

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

  await page.goto(url + "/", { waitUntil: "networkidle" });
  const result = { role, page_errors: errors };

  if (role === "manager") {
    await page.waitForTimeout(300); // let loadWhoAmI()/applyRoleGating() settle
    result.bell_display = await page.evaluate(() => {
      const el = document.getElementById("notifBellWrap");
      return el ? getComputedStyle(el).display : "missing";
    });
    result.api_status = await page.evaluate(async () => {
      const r = await fetch("/api/ko/notifications", { credentials: "same-origin" });
      return r.status;
    });
    console.log(JSON.stringify(result));
    await browser.close();
    return;
  }

  // ---- admin ----
  await page.click("#tabEngBtn");
  const colSel = '.eng-col[data-stage="' + stage + '"] .eng-col-body';
  await page.waitForSelector(colSel, { timeout: 15000 });
  await page.waitForFunction((sel) => {
    const el = document.querySelector(sel);
    return !!el && el.scrollHeight > el.clientHeight + 20;
  }, colSel, { timeout: 15000 });

  await page.evaluate((sel) => {
    document.querySelector(sel).scrollTop = Math.floor(document.querySelector(sel).scrollHeight / 2);
  }, colSel);
  result.scroll_set = await page.evaluate((sel) => document.querySelector(sel).scrollTop, colSel);

  result.bell_visible = await page.evaluate(() => {
    const el = document.getElementById("notifBellWrap");
    return !!el && getComputedStyle(el).display !== "none";
  });

  await page.click("#notifBellBtn");
  await page.waitForSelector("#notifPanel.open", { timeout: 5000 });
  result.badge_before_click = await page.evaluate(() => document.getElementById("notifBadge").textContent);
  result.panel_has_target_item = await page.evaluate((pid) => {
    return !!document.querySelector('#notifPanelList [data-proposal-id="' + pid + '"]');
  }, proposalId);

  // ---- a full board refresh (the 20s poll's own code path) while the panel is open must not
  // move the column the reader is looking at ----
  await page.evaluate(() => document.getElementById("refreshBtn").click());
  await page.waitForTimeout(400);
  result.scroll_after_board_refresh = await page.evaluate((sel) => document.querySelector(sel).scrollTop, colSel);

  // ---- click-through: opens "Предложения агента КО" with this exact proposal, marks the
  // notification read, never touches the project/proposal itself ----
  await page.evaluate((pid) => {
    document.querySelector('#notifPanelList [data-proposal-id="' + pid + '"]').click();
  }, proposalId);
  await page.waitForSelector("#ko-row-" + proposalId, { timeout: 5000 });
  await page.waitForFunction((pid) => {
    const row = document.getElementById("ko-row-" + pid);
    return !!row && row.classList.contains("ko-row-flash");
  }, proposalId, { timeout: 5000 });
  result.panel_closed_after_click = await page.evaluate(() => !document.getElementById("notifPanel").classList.contains("open"));
  result.landed_on_ko_subview = await page.evaluate(() => {
    const btn = document.querySelector('.eng-subtab[data-subview="ko"]');
    return !!btn && btn.classList.contains("active");
  });

  // ---- back to the board: the column must be exactly where the reader left it, even though the
  // notification click just hid and re-showed it via a subview switch ----
  await page.click('.eng-subtab[data-subview="board"]');
  await page.waitForTimeout(150);
  result.scroll_after_notif_click_through = await page.evaluate((sel) => document.querySelector(sel).scrollTop, colSel);

  await page.click("#notifBellBtn");
  await page.waitForSelector("#notifPanel.open", { timeout: 5000 });
  result.badge_after_click = await page.evaluate(() => {
    const b = document.getElementById("notifBadge");
    return getComputedStyle(b).display === "none" ? "0" : b.textContent;
  });
  result.clicked_item_unread_class_after = await page.evaluate((pid) => {
    const el = document.querySelector('#notifPanelList [data-proposal-id="' + pid + '"]');
    return el ? el.classList.contains("unread") : null;
  }, proposalId);

  await page.click("#notifMarkAllBtn");
  await page.waitForTimeout(300);
  result.badge_after_mark_all = await page.evaluate(() => {
    const b = document.getElementById("notifBadge");
    return getComputedStyle(b).display === "none" ? "0" : b.textContent;
  });
  result.scroll_final = await page.evaluate((sel) => document.querySelector(sel).scrollTop, colSel);

  result.page_errors = errors;
  console.log(JSON.stringify(result));
  await browser.close();
})().catch(async (e) => {
  console.error(e && e.stack || e);
  process.exit(1);
});
