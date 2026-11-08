// Records a LinkedIn-ready walkthrough of AgentOps: portrait 4:5, animated captions and cards.
// Captures JPEG frames over CDP (720x900; build.py scales them to 1080x1350) plus a timeline of "wait"
// segments, which build.py later speeds up. Usage: node linkedin.js <baseUrl> <outDir>
//
// If the AI reviewer's comments are escalated to the merge approval, the script does NOT approve them
// itself: it waits for a person to decide in the AgentOps page. Set AUTO_APPROVE_ESCALATED=1 to skip that.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://localhost:8820";
const OUT = process.argv[3] || path.join(__dirname, "out", "take");
const FRAMES = path.join(OUT, "frames");
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(FRAMES, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000;
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// ------------------------------------------------------------------ overlay UI injected into every page
const OVERLAY = { path: path.join(__dirname, "overlay.js") };

// AgentOps page tidy-up for a phone-sized video: room for the overlays, no clutter.
const TIDY = `
  if (location.port === "8820" || location.port === "8821") {
    const s = document.createElement("style");
    s.textContent = "body{padding-top:64px!important;padding-bottom:200px!important} #fakeHint{display:none!important} " +
      "section:has(#runsLabel){display:none!important} .grain{display:none!important} nav{height:52px!important}";
    document.addEventListener("DOMContentLoaded", () => document.head.appendChild(s));
  } else {
    const s = document.createElement("style");
    s.textContent = "body{padding-top:64px!important;padding-bottom:200px!important}";
    document.addEventListener("DOMContentLoaded", () => document.head.appendChild(s));
  }
  window.open = () => { const t = {document: {body: {}}, close() {}};
    Object.defineProperty(t, "location", {set(u) { window.location.href = u; }}); return t; };
`;

// ------------------------------------------------------------------ recording state
const segments = [];      // {start, end, kind: "wait"} in wall-clock seconds
let waitStart = null;
const frames = [];        // {t, file}
let lastSaved = 0;
let state = { top: null, cap: null, fast: null };

function beginWait() { if (waitStart === null) waitStart = now(); }
function endWait() { if (waitStart !== null) { segments.push({ start: waitStart, end: now(), kind: "wait" }); waitStart = null; } }

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 720, height: 900 } });
  await context.addInitScript(OVERLAY);
  await context.addInitScript(TIDY);
  const page = await context.newPage();

  const cdp = await context.newCDPSession(page);
  let n = 0;
  cdp.on("Page.screencastFrame", async (f) => {
    cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }).catch(() => {});
    const t = now();
    const minGap = waitStart !== null ? 1 / 6 : 1 / 30;
    if (t - lastSaved < minGap) return;
    lastSaved = t;
    const file = `f${String(n++).padStart(6, "0")}.jpg`;
    fs.writeFileSync(path.join(FRAMES, file), Buffer.from(f.data, "base64"));
    frames.push({ t, file });
  });
  const startCast = () => cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, maxWidth: 1080, maxHeight: 1350, everyNthFrame: 1 }).catch((e) => log("cast", e.message));

  // UI helpers that survive navigation (the overlay is re-mounted, then state re-applied).
  const ui = async (fn, ...args) => page.evaluate(([fn, args]) => window.__ui && window.__ui[fn](...args), [fn, args]).catch(() => {});
  const top = async (i) => { state.top = i; await ui("top", i); };
  const cap = async (k, html) => { state.cap = [k, html]; log("cap:", k, "|", (html || "").replace(/<[^>]+>/g, "")); await ui("cap", k, html); };
  const fast = async (text) => { state.fast = text; await ui("fast", text); };
  const restore = async () => { await ui("top", state.top); if (state.cap) await ui("cap", ...state.cap); await ui("fast", state.fast); };
  page.on("load", async () => { await startCast(); await restore(); });

  async function tapClick(selector) {
    const el = page.locator(selector).first();
    await el.scrollIntoViewIfNeeded().catch(() => {});
    await sleep(250);
    const b = await el.boundingBox();
    if (b) { await ui("tap", b.x + b.width / 2, b.y + b.height / 2); await sleep(380); }
    await el.click();
  }
  const center = (selector) => page.evaluate((s) => document.querySelector(s)?.scrollIntoView({ behavior: "smooth", block: "center" }), selector).catch(() => {});
  const getRun = async (id) => { const r = await page.request.get(`${BASE}/api/runs/${id}`); return r.ok() ? r.json() : null; };
  const getEvents = async (id) => { const r = await page.request.get(`${BASE}/api/runs/${id}/events`); return r.ok() ? r.json() : []; };
  async function latestArtifact(id, name) {
    const arts = await (await page.request.get(`${BASE}/api/runs/${id}/artifacts`)).json();
    const a = arts.filter((x) => x.name === name).pop();
    return a ? (await (await page.request.get(`${BASE}/api/artifacts/${a.id}`)).json()) : null;
  }

  const health = await (await page.request.get(`${BASE}/health`)).json();
  const model = health.llm_mode === "fake" ? "offline replay" : health.model.split("/").pop();
  log("health", health);

  await page.goto(BASE);
  await startCast();
  await page.waitForSelector("#startBtn");
  await sleep(400);

  // ---------------------------------------------------------------- intro
  await ui("card", `<div class="k">Side project · AgentOps</div><div class="h">I built an AI software team</div><div class="h dim">that has to prove its work.</div><div class="f">one sentence in → a tested, reviewed feature out</div>`);
  await sleep(4800);
  await ui("card", `<div class="k">The problem</div><div class="h">AI writes code fast.</div><div class="h dim">Who checks it?</div>
    <div class="l"><span class="dot"></span><span>AgentOps makes the AI follow a real engineering process:</span></div>
    <div class="l"><span class="dot"></span><span><b>requirements → design → tests first → code → 9 checks → review</b></span></div>
    <div class="l"><span class="dot"></span><span>Each rule is enforced by <b>code</b>, not by asking nicely in a prompt.</span></div>`);
  await sleep(7000);
  const live = health.llm_mode !== "fake";
  await ui("card", `<div class="k">${live ? "Live demo · real model" : "Demo · offline replay of the model"}</div><div class="h">Let's add a feature</div><div class="h dim">to a real web shop.</div><div class="f">model: ${model}${live ? " · nothing pre-recorded" : " · the model's replies are a recording"}</div>`);
  await sleep(4000);
  await ui("card", null);

  // ---------------------------------------------------------------- the shop before
  await top(null);
  await cap("The app", "Meet <em>ShopLite</em>: a small online shop. FastAPI backend + one web page.");
  await tapClick("#openOriginal");
  await page.waitForURL(/127\.0\.0\.1:8830/, { timeout: 90_000 });
  await page.waitForSelector("[data-add]", { timeout: 30_000 });
  await sleep(2800);
  await cap("Before", "Add two desk lamps: <em>₹3,798</em>. There's no way to enter a coupon.");
  for (let i = 0; i < 2; i++) { await tapClick('[data-add="lamp"]'); await sleep(700); }
  await center("#total");
  await sleep(3200);

  // ---------------------------------------------------------------- the task
  await page.goto(BASE);
  await page.waitForSelector("#startBtn");
  await cap("The input", "One sentence. That's all the AI gets.");
  const task = await page.inputValue("#task");
  await page.fill("#task", "");
  await center("#task");
  await sleep(600);
  await page.type("#task", task, { delay: 22 });
  await sleep(1500);
  await tapClick("#startBtn");
  await page.waitForURL(/#run=/, { timeout: 30_000 });
  const runId = page.url().split("run=")[1];
  log("run", runId);
  const started = now();

  // ---------------------------------------------------------------- follow the run, event by event
  const PHASE = { load_repo: 0, requirements: 0, approve_requirements: 0, design: 1, approve_design: 1, write_tests: 2, build: 3,
    quality_gates: 4, review: 5, release: 6, approve_merge: 6, merge: 6 };
  const RUNNING = {
    requirements: ["Requirements", "The AI writes <em>user stories</em> and <em>acceptance criteria</em> anyone can check."],
    design: ["Design", "The AI plans the change: which files, exact function signatures, and an architecture decision record."],
    write_tests: ["Tests first", "The AI writes the <em>tests before the code</em>. They must fail now, since the feature doesn't exist yet."],
    build: ["Build", "Now the AI writes the code: backend <em>and</em> the web page."],
    quality_gates: ["9 checks", "Everything runs inside a locked-down <em>Docker sandbox</em>: no network, read-only, non-root."],
    review: ["Review", "A second AI reviews the diff. Every comment must point at a <em>real changed line</em>, or it's dropped."],
    release: ["Pull request", "Writing the PR, with a traceability matrix: requirement → test → exact lines of code."],
  };
  const CHECK = { test_lock: ["Tests untouched (SHA-256 lock)", "sha256"], scope_lock: ["Only approved files changed", "scope"],
    secrets: ["No secrets in the code", "scan"], syntax: ["Code compiles", "py"], tests: ["All tests pass", "pytest"],
    coverage: ["≥80% of changed lines tested", "coverage"], lint: ["Lint clean", "ruff"], types: ["Types check", "mypy"],
    security: ["Security scan", "bandit"] };

  let seen = 0, outcome = null, escalated = false;
  const shownGates = new Set();
  while (true) {
    if (now() - started > 80 * 60) { outcome = "timeout"; break; }
    const evs = await getEvents(runId);
    const fresh = evs.slice(seen); seen = evs.length;
    for (const e of fresh) {
      const k = e.step_key, d = e.data || {};
      if (e.type === "step_started" && RUNNING[k]) {
        await top(PHASE[k]);
        const round = d.round > 1 ? ` · round ${d.round}` : "";
        await cap(RUNNING[k][0] + round, RUNNING[k][1]);
        await center("#graph");
        await sleep(2600);           // let the caption be read at normal speed
        beginWait();
        await fast(`${model} working · sped up`);
      }
      if (e.type === "gate_failed" && k === "write_tests") {
        endWait(); await fast(null);
        const fb = d.feedback || {};
        const why = fb.criteria_without_failing_test ? `${fb.criteria_without_failing_test.join(", ")} had no test that fails yet`
          : (fb.problems || [])[0]?.includes("checks nothing") ? "a test had no assert, so it could never fail"
          : (fb.problems || [])[0] || fb.summary || "rejected";
        await cap("Red check ✕ rejected", `The tests were rejected: <em>${why.slice(0, 110)}</em>. Sent back to the AI with the evidence.`);
        await sleep(5200);
      }
      if (e.type === "step_done" && k === "write_tests") {
        endWait(); await fast(null);
        const r = await getRun(runId);
        const red = r?.steps.write_tests.output?.red || {};
        const nTests = (red.red_tests || []).length, nReg = (red.regression_tests || []).length;
        await cap("Red check ✓ passed", `<em>${nTests} tests fail</em> before any code exists${nReg ? ` (+${nReg} guarding today's behaviour)` : ""}. Now they're <em>locked</em>: the AI can't edit them later.`);
        await sleep(5600);
      }
      if ((e.type === "step_done" || e.type === "gate_failed" || e.type === "step_failed") && k === "quality_gates") {
        const key = e.type + ":" + e.seq;
        if (shownGates.has(key)) continue;
        shownGates.add(key);
        endWait(); await fast(null);
        const rep = await latestArtifact(runId, "gate-report.json");
        let checks = [];
        try { checks = JSON.parse(rep.content).checks; } catch { /* no report */ }
        if (checks.length) {
          const ok = checks.every((c) => c.passed);
          const rows = checks.map((c) => ({ ok: c.passed, label: (CHECK[c.name] || [c.name])[0], tool: (CHECK[c.name] || ["", ""])[1] }));
          await ui("checks", "Quality gates", { ok, text: ok ? "ALL PASSED" : `${checks.filter((c) => !c.passed).length} FAILED` }, rows);
          await sleep(1200 + rows.length * 300);
          await cap(ok ? "9 checks ✓" : "Checks ✕ failed", ok ? "Every check passed. Code, not the AI, decides this."
            : "A check failed, so the code goes <em>back to the AI</em> with the exact failing output. No human needed.");
          await sleep(4800);
          await ui("checks", null, null, null);
        }
      }
      if (e.type === "step_rewound" && k === "build") {
        const by = (d.reason || "").startsWith("review") ? "the reviewer flagged blocking issues" : "the checks failed";
        await cap("Loop", `Back to <em>Build</em> for round ${d.iteration + 1}, because ${by}.`);
        await sleep(3000);
      }
      if (e.type === "gate_escalated" && k === "review") {
        escalated = true;
        endWait(); await fast(null);
        await cap("Review · escalated", "The reviewer is still objecting, so AgentOps <em>stops the loop</em> and hands its comments to a person.");
        await sleep(4500);
      }
      if (e.type === "step_done" && k === "review" && !escalated) {
        endWait(); await fast(null);
        const r = await getRun(runId);
        const rv = r?.steps.review.output || {};
        const blocking = (rv.comments || []).filter((c) => c.severity === "blocking").length;
        const dropped = (rv.dropped_ungrounded || []).length;
        await cap("Review", `Verdict: <em>${rv.verdict || "?"}</em> · ${blocking} blocking, ${(rv.comments || []).length - blocking} suggestion(s)${dropped ? ` · ${dropped} dropped for not pointing at a real line` : ""}.`);
        await sleep(4200);
      }
      if (e.type === "approval_requested") {
        endWait(); await fast(null);
        await top(PHASE[k]);
        await page.waitForSelector("[data-decide=approved]", { timeout: 30_000 }).catch(() => {});
        await center(".approval");
        if (k === "approve_requirements") {
          await cap("Human in the loop", "Nothing moves on until <em>a person approves</em>. Here are the AI's requirements.");
          await sleep(3500);
          await page.evaluate(() => document.querySelector(".approval .md-box")?.scrollBy({ top: 420, behavior: "smooth" })).catch(() => {});
          await sleep(3200);
        } else if (k === "approve_design") {
          await cap("Human in the loop", "The design: files to change, interfaces, risks. <em>Approve</em>.");
          await sleep(3200);
          await page.evaluate(() => document.querySelector(".approval .md-box")?.scrollBy({ top: 520, behavior: "smooth" })).catch(() => {});
          await sleep(3000);
        } else {
          const r = await getRun(runId);
          const gates = (await latestArtifact(runId, "gate-report.json")) || {};
          let passed = false; try { passed = JSON.parse(gates.content).passed; } catch { /* */ }
          if (!passed) {
            await cap("Human decides", "The checks never all passed, so <em>the person rejects the merge</em>. Nothing broken ships.");
            await sleep(5000);
            await tapClick("[data-decide=rejected]");
            continue;
          }
          await cap("Pull request", "The PR it wrote: what changed, all 9 checks, and the traceability matrix.");
          await sleep(3200);
          await page.evaluate(() => document.querySelector(".approval .md-box")?.scrollBy({ top: 520, behavior: "smooth" })).catch(() => {});
          await sleep(3800);
          await page.evaluate(() => document.querySelector(".approval .md-box")?.scrollBy({ top: 600, behavior: "smooth" })).catch(() => {});
          await sleep(3200);
          if (escalated && !process.env.AUTO_APPROVE_ESCALATED) {
            // A person has to read the escalated comments; the script only films the decision.
            await cap("Human in the loop", "The reviewer's open comments are in the PR. <em>A person reads them and decides.</em>");
            log("waiting for a person to approve or reject the merge in the AgentOps page");
            beginWait();
            let decided = null;
            while (!decided) {
              await sleep(2000);
              const r2 = await getRun(runId);
              decided = (r2?.approvals || []).find((a) => a.step_key === "approve_merge" && a.decision)?.decision;
            }
            endWait();
            await cap("Human in the loop", decided === "approved" ? "The person checked the comments and <em>approved the merge</em>."
              : "The person <em>did not approve</em> the merge.");
            await sleep(3500);
            continue;
          }
          await cap("Human in the loop", "Nothing merges without a person. <em>Approve the merge.</em>");
          await sleep(2600);
        }
        await tapClick("[data-decide=approved]");
        await sleep(1500);
      }
      if (["run_done", "run_failed", "run_cancelled"].includes(e.type)) { outcome = e.type; }
    }
    if (outcome) break;
    await sleep(1000);
  }
  endWait(); await fast(null);
  const final = await getRun(runId);
  log("final", final?.status, final?.error || "");

  if (final?.status === "done" && final.steps.merge?.status === "done") {
    await top(7);
    const mins = ((new Date(final.finished_at) - new Date(final.started_at)) / 60000).toFixed(1);
    const tries = final.steps.build.iteration + 1;
    const nTests = (final.steps.write_tests.output?.red?.red_tests || []).length;
    await center("#runView");
    await cap("Merged ✓", `12/12 steps in <em>${mins} min</em> of real time. Now the real test: the shop.`);
    await sleep(3500);
    await tapClick("#openShopBtn");
    await page.waitForURL(/127\.0\.0\.1:8830/, { timeout: 90_000 });
    await page.waitForSelector("[data-add]", { timeout: 30_000 });
    await cap("After", "Same shop, new code. The cart now has a coupon box.");
    await sleep(2500);
    for (let i = 0; i < 2; i++) { await tapClick('[data-add="lamp"]'); await sleep(700); }
    const input = page.locator('#coupon-code, input[id*="coupon" i], input[name*="coupon" i], input[placeholder*="coupon" i]').first();
    let total = "";
    if (await input.count()) {
      await cap("After", "Type the code in <em>lowercase</em>: save10");
      await input.scrollIntoViewIfNeeded(); await tapClick('#coupon-code, input[id*="coupon" i]');
      await input.type("save10", { delay: 140 }); await sleep(600);
      const apply = page.locator('#apply-coupon, button:has-text("Apply")').first();
      if (await apply.count()) await tapClick('#apply-coupon, button:has-text("Apply")'); else await input.press("Enter");
      await sleep(1800);
      // The AI may show the discounted amount in its own element (e.g. #discounted-total) and cross out #total.
      total = await page.evaluate(() => {
        const shown = (el) => el && el.offsetParent !== null && getComputedStyle(el).textDecorationLine !== "line-through"
          && getComputedStyle(el.parentElement).textDecorationLine !== "line-through";
        const els = [...document.querySelectorAll('[id*="total" i]')].filter((el) => !/sub/i.test(el.id));
        const el = els.filter((x) => /discount/i.test(x.id) && shown(x)).pop() || els.find(shown);
        return (el?.textContent || "").trim();
      }).catch(() => "");
      await center("#total");
      await cap(total === "₹3,418.20" ? "It works" : "Check this",
        total === "₹3,418.20" ? "Subtotal ₹3,798.00 → total <em>₹3,418.20</em>. 10% off, typed in lowercase."
          : `The page shows <em>${total || "no total"}</em>, not the expected ₹3,418.20.`);
      await sleep(4500);
      if (await page.locator("#checkout").isEnabled().catch(() => false)) {
        await tapClick("#checkout"); await sleep(1500);
        const msg = ((await page.textContent("#message").catch(() => "")) || "").trim();
        if (msg) { await cap("Checkout", msg); await sleep(3800); }
      }
    } else {
      await cap("After", "The merged page has no coupon box. Showing it honestly.");
      await sleep(4000);
    }
    await cap(null, null); await top(null);
    const approvals = (await getEvents(runId)).filter((x) => x.type === "approval_given").length;
    await ui("card", `<div class="k">Result · ${live ? "real run" : "offline replay"}</div><div class="h">Shipped.</div>
      <div class="stats">
        <div class="stat"><div class="v">${mins} min</div><div class="n">one sentence → merged feature</div></div>
        <div class="stat"><div class="v">${tries}</div><div class="n">build ${tries > 1 ? `tries (sent back ${tries - 1}×)` : "try"}</div></div>
        <div class="stat"><div class="v">${nTests}</div><div class="n">tests written first, then locked</div></div>
        <div class="stat"><div class="v">${approvals}</div><div class="n">human approvals</div></div>
      </div><div class="f">model: ${model}</div>`);
    await sleep(6500);
  } else {
    await cap(null, null); await top(null);
    await ui("card", `<div class="k">Result · ${live ? "real run" : "offline replay"}</div><div class="h">Stopped safely.</div><div class="h dim">Nothing broken was merged.</div><div class="f">${(final?.error || outcome || "").slice(0, 140)}</div>`);
    await sleep(6000);
  }

  await ui("card", `<div class="k">Under the hood</div><div class="h">Built to not lose work.</div>
    <div class="l"><span class="dot"></span><span>Durable engine on <b>Postgres</b>: SKIP LOCKED queue, leases, fencing, idempotent tools</span></div>
    <div class="l"><span class="dot"></span><span>Chaos test: <b>500 runs, 435 random worker kills → 0 lost, 0 duplicate side effects</b></span></div>
    <div class="l"><span class="dot"></span><span>AI-written code only ever runs in a <b>locked Docker sandbox</b></span></div>
    <div class="f">Python · FastAPI · Postgres · Docker · pytest</div>`);
  await sleep(8000);
  await ui("card", `<div class="k">AgentOps</div><div class="big">One sentence in.</div><div class="big" style="color:rgba(255,255,255,.42)">Tested feature out.</div><div class="f">code + write-up in the comments</div>`);
  await sleep(4500);

  await cdp.send("Page.stopScreencast").catch(() => {});
  fs.writeFileSync(path.join(OUT, "timeline.json"), JSON.stringify({ frames, segments, end: now(), runId, status: final?.status, model }, null, 1));
  log("frames", frames.length, "segments", segments.length);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
