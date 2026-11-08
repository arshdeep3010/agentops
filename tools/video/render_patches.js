// Renders corrected caption overlays (same CSS as the recording) as transparent 1080x1350 PNGs.
// Each new caption box is made at least as tall as the one it replaces, so it fully covers it.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const OUT = path.join(__dirname, "out", "patches");
fs.mkdirSync(OUT, { recursive: true });
const spec = JSON.parse(fs.readFileSync(path.join(__dirname, "patches.json"), "utf8"));

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 720, height: 900 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.setContent("<html><body style='margin:0;background:transparent'></body></html>");
  await page.addScriptTag({ path: path.join(__dirname, "overlay.js") });
  await page.waitForTimeout(1500); // fonts
  const meta = {};
  for (const p of spec.captions) {
    await page.evaluate(([k, h]) => { document.getElementById("__cap") && (document.getElementById("__cap").style.minHeight = ""); window.__ui.cap(k, h); }, [p.old[0], p.old[1]]);
    await page.waitForTimeout(700);
    const oldBox = await page.evaluate(() => document.getElementById("__cap").getBoundingClientRect().toJSON());
    await page.evaluate(([k, h, mh]) => { window.__ui.cap(k, h); document.getElementById("__cap").style.minHeight = mh + "px"; document.getElementById("__cap").style.background = "rgb(8,8,8)"; }, [p.new[0], p.new[1], oldBox.height]);
    await page.waitForTimeout(700);
    const box = await page.evaluate(() => document.getElementById("__cap").getBoundingClientRect().toJSON());
    await page.screenshot({ path: path.join(OUT, p.id + ".png"), omitBackground: true });
    meta[p.id] = { top: Math.min(oldBox.top, box.top) };
  }
  await page.evaluate(() => window.__ui.cap(null, null));
  // The result card, re-rendered with the corrected label; build.py pastes only the changed stat box.
  await page.evaluate((html) => window.__ui.card(html), spec.card.html);
  await page.waitForTimeout(2600);
  const r = await page.evaluate((i) => document.querySelectorAll("#__card .stat")[i].getBoundingClientRect().toJSON(), spec.card.stat);
  const pad = 4;
  const clip = { x: r.x - pad, y: r.y - pad, width: r.width + 2 * pad, height: r.height + 2 * pad };
  await page.screenshot({ path: path.join(OUT, "card.png"), clip });
  meta.card = { x: Math.round(clip.x), y: Math.round(clip.y) };
  fs.writeFileSync(path.join(OUT, "meta.json"), JSON.stringify(meta, null, 1));
  console.log("rendered", Object.keys(meta).join(", "));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
