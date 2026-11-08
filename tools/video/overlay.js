
(() => {
  if (window.__ui) return;
  const css = `
  #__o * { box-sizing: border-box; }
  #__o { position: fixed; inset: 0; z-index: 2147483000; pointer-events: none; font-family: Inter, "Segoe UI", system-ui, sans-serif; color: #fff; -webkit-font-smoothing: antialiased; }
  #__top { position: absolute; left: 0; right: 0; top: 0; height: 64px; display: flex; align-items: center; gap: 10px; padding: 0 16px;
    background: linear-gradient(180deg, rgba(0,0,0,.96), rgba(0,0,0,.86)); border-bottom: 1px solid rgba(255,255,255,.12); transform: translateY(-100%); transition: transform .5s cubic-bezier(.23,1,.32,1); }
  #__top.on { transform: none; }
  #__top .b { font: 700 17px/1 "Inter Tight", Inter, sans-serif; letter-spacing: -.02em; white-space: nowrap; margin-right: 4px; }
  #__top .ph { display: flex; gap: 5px; flex: 1; justify-content: flex-end; }
  #__top .p { font: 600 10.5px/1 Inter, sans-serif; padding: 6px 7px; border-radius: 999px; border: 1px solid rgba(255,255,255,.18); color: rgba(255,255,255,.45); transition: all .35s ease; white-space: nowrap; }
  #__top .p.done { color: #000; background: #fff; border-color: #fff; }
  #__top .p.act { color: #fff; border-color: #fff; box-shadow: 0 0 0 3px rgba(255,255,255,.18); animation: __pulse 1.2s ease-in-out infinite; }
  @keyframes __pulse { 50% { box-shadow: 0 0 0 6px rgba(255,255,255,.06); } }
  #__cap { position: absolute; left: 16px; right: 16px; bottom: 18px; padding: 16px 18px 17px; border-radius: 18px; background: rgba(8,8,8,.94);
    box-shadow: 0 20px 60px rgba(0,0,0,.6), 0 0 0 1px rgba(255,255,255,.16); opacity: 0; transform: translateY(24px); transition: opacity .35s ease, transform .45s cubic-bezier(.23,1,.32,1); }
  #__cap.on { opacity: 1; transform: none; }
  #__cap .k { font: 700 11px/1 "JetBrains Mono", Consolas, monospace; letter-spacing: .12em; text-transform: uppercase; color: rgba(255,255,255,.55); margin-bottom: 8px; display: flex; gap: 8px; align-items: center; }
  #__cap .t { font: 650 23px/1.28 "Inter Tight", Inter, sans-serif; letter-spacing: -.015em; }
  #__cap .t em { font-style: normal; background: #fff; color: #000; padding: 0 6px; border-radius: 6px; }
  #__fast { position: absolute; right: 16px; top: 78px; padding: 8px 12px; border-radius: 999px; background: #fff; color: #000; font: 700 13px/1 Inter, sans-serif;
    opacity: 0; transform: scale(.85); transition: all .3s cubic-bezier(.23,1,.32,1); box-shadow: 0 8px 30px rgba(0,0,0,.45); }
  #__fast.on { opacity: 1; transform: none; }
  #__fast i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #000; margin-right: 7px; animation: __blink 1s infinite; }
  @keyframes __blink { 50% { opacity: .2; } }
  #__card { position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: center; padding: 0 44px; background: #050505; opacity: 0; transition: opacity .45s ease; }
  #__card.on { opacity: 1; }
  #__card::before { content: ""; position: absolute; inset: 0; background: radial-gradient(60% 40% at 50% 0%, rgba(255,255,255,.10), transparent 70%),
    linear-gradient(to right, rgba(255,255,255,.035) 1px, transparent 1px), linear-gradient(to bottom, rgba(255,255,255,.035) 1px, transparent 1px);
    background-size: auto, 48px 48px, 48px 48px; }
  #__card > * { position: relative; opacity: 0; transform: translateY(18px); }
  #__card.on > * { animation: __rise .7s cubic-bezier(.23,1,.32,1) forwards; }
  @keyframes __rise { to { opacity: 1; transform: none; } }
  #__card .k { font: 700 13px/1 "JetBrains Mono", Consolas, monospace; letter-spacing: .16em; text-transform: uppercase; color: rgba(255,255,255,.5); margin-bottom: 22px; }
  #__card .h { font: 750 44px/1.06 "Inter Tight", Inter, sans-serif; letter-spacing: -.035em; margin: 0 0 6px; }
  #__card .h.dim { color: rgba(255,255,255,.42); }
  #__card .l { font: 500 21px/1.4 Inter, sans-serif; color: rgba(255,255,255,.82); margin-top: 14px; display: flex; gap: 12px; align-items: baseline; }
  #__card .l b { color: #fff; }
  #__card .l .dot { flex: none; width: 9px; height: 9px; border-radius: 50%; background: #fff; transform: translateY(-2px); }
  #__card .f { margin-top: 34px; font: 600 15px/1.4 "JetBrains Mono", Consolas, monospace; color: rgba(255,255,255,.55); }
  #__card .big { font: 800 64px/1 "Inter Tight", Inter, sans-serif; letter-spacing: -.04em; }
  #__card .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 26px; }
  #__card .stat { padding: 18px; border-radius: 16px; border: 1px solid rgba(255,255,255,.16); background: rgba(255,255,255,.04); }
  #__card .stat .v { font: 750 34px/1 "Inter Tight", Inter, sans-serif; letter-spacing: -.03em; }
  #__card .stat .n { font: 500 14px/1.3 Inter, sans-serif; color: rgba(255,255,255,.6); margin-top: 8px; }
  #__checks { position: absolute; left: 16px; right: 16px; top: 50%; transform: translate(0,-50%) scale(.96); padding: 20px; border-radius: 20px; background: rgba(6,6,6,.97);
    box-shadow: 0 30px 80px rgba(0,0,0,.7), 0 0 0 1px rgba(255,255,255,.18); opacity: 0; transition: all .4s cubic-bezier(.23,1,.32,1); }
  #__checks.on { opacity: 1; transform: translate(0,-50%); }
  #__checks h4 { margin: 0 0 14px; font: 700 21px/1.2 "Inter Tight", Inter, sans-serif; display: flex; justify-content: space-between; align-items: center; }
  #__checks h4 span { font: 700 12px/1 "JetBrains Mono", monospace; letter-spacing: .1em; padding: 6px 9px; border-radius: 999px; }
  #__checks .row { display: flex; align-items: center; gap: 12px; padding: 10px 4px; border-top: 1px solid rgba(255,255,255,.08); font: 500 17px/1.25 Inter, sans-serif; opacity: 0; transform: translateX(-10px); }
  #__checks.on .row { animation: __in .45s cubic-bezier(.23,1,.32,1) forwards; }
  @keyframes __in { to { opacity: 1; transform: none; } }
  #__checks .row .ic { flex: none; width: 24px; height: 24px; border-radius: 50%; display: grid; place-items: center; font: 800 14px/1 Inter, sans-serif; }
  #__checks .row.ok .ic { background: #fff; color: #000; }
  #__checks .row.bad .ic { background: #f87171; color: #000; }
  #__checks .row.bad { color: #fca5a5; }
  #__checks .row small { margin-left: auto; color: rgba(255,255,255,.4); font: 500 12px/1 "JetBrains Mono", monospace; }
  .__tap { position: absolute; width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%; background: rgba(255,255,255,.95); box-shadow: 0 0 0 2px #000;
    animation: __tap .7s cubic-bezier(.23,1,.32,1) forwards; }
  .__tap::after { content: ""; position: absolute; inset: -4px; border-radius: 50%; border: 3px solid #fff; animation: __ring .7s cubic-bezier(.23,1,.32,1) forwards; }
  @keyframes __tap { 0% { transform: scale(.4); opacity: 0; } 25% { transform: scale(1); opacity: 1; } 100% { transform: scale(.8); opacity: 0; } }
  @keyframes __ring { from { transform: scale(.6); opacity: 1; } to { transform: scale(3.2); opacity: 0; } }
  `;
  const PH = ["Requirements", "Design", "Tests", "Build", "Checks", "Review", "Merge"];
  function mount() {
    if (document.getElementById("__o")) return;
    const st = document.createElement("style"); st.textContent = css; document.documentElement.appendChild(st);
    if (!document.querySelector('link[href*="Inter+Tight"]')) {
      const f = document.createElement("link"); f.rel = "stylesheet";
      f.href = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Inter+Tight:wght@600;700;800&family=JetBrains+Mono:wght@500;700&display=swap";
      document.documentElement.appendChild(f);
    }
    const o = document.createElement("div"); o.id = "__o";
    o.innerHTML = '<div id="__top"><span class="b">AgentOps</span><div class="ph">' + PH.map((p) => '<span class="p">' + p + "</span>").join("") +
      '</div></div><div id="__fast"><i></i><span></span></div><div id="__checks"></div><div id="__cap"><div class="k"></div><div class="t"></div></div><div id="__card"></div>';
    document.documentElement.appendChild(o);
  }
  const $ = (id) => { mount(); return document.getElementById(id); };
  window.__ui = {
    top(active) {
      const t = $("__top"); t.classList.toggle("on", active !== null && active !== undefined);
      t.querySelectorAll(".p").forEach((el, i) => { el.className = "p" + (i < active ? " done" : i === active ? " act" : ""); });
    },
    cap(kicker, html) {
      const c = $("__cap");
      if (!html) { c.classList.remove("on"); return; }
      c.classList.remove("on"); void c.offsetWidth;
      c.querySelector(".k").textContent = kicker || ""; c.querySelector(".t").innerHTML = html;
      requestAnimationFrame(() => c.classList.add("on"));
    },
    fast(text) { const f = $("__fast"); f.querySelector("span").textContent = text || ""; f.classList.toggle("on", !!text); },
    card(html) {
      const c = $("__card");
      if (!html) { c.classList.remove("on"); return; }
      c.classList.remove("on"); c.innerHTML = html; void c.offsetWidth;
      [...c.children].forEach((el, i) => { el.style.animationDelay = (0.15 + i * 0.22) + "s"; });
      c.classList.add("on");
    },
    checks(title, badge, rows) {
      const c = $("__checks");
      if (!rows) { c.classList.remove("on"); return; }
      c.innerHTML = '<h4>' + title + '<span style="background:' + (badge.ok ? "#fff;color:#000" : "#f87171;color:#000") + '">' + badge.text + "</span></h4>" +
        rows.map((r, i) => '<div class="row ' + (r.ok ? "ok" : "bad") + '" style="animation-delay:' + (0.25 + i * 0.28) + 's"><span class="ic">' + (r.ok ? "✓" : "✕") + "</span>" + r.label + "<small>" + r.tool + "</small></div>").join("");
      c.classList.remove("on"); void c.offsetWidth; c.classList.add("on");
    },
    tap(x, y) { const d = document.createElement("div"); d.className = "__tap"; d.style.left = x + "px"; d.style.top = y + "px"; $("__o").appendChild(d); setTimeout(() => d.remove(), 800); },
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();