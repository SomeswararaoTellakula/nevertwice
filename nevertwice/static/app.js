/* NeverTwice front-end — vanilla JS, no build step. All server text is inserted as text nodes (no innerHTML). */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, String(value));
    }
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}
/** Safe append: skips null/false and flattens arrays (native Element.append would print "null"). */
function add(el, ...kids) { return append(el, kids); }
function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); return el; }
function svg(tag, attrs, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, String(v));
  append(el, children);
  return el;
}

async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw new Error("Cannot reach the NeverTwice server. Is `python run.py` still running?");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (HTTP ${res.status})`);
  return data;
}

/** POST and read a Server-Sent Events stream, calling onEvent for each JSON event. */
async function stream(path, body, onEvent) {
  let res;
  try {
    res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch (err) {
    throw new Error("Cannot reach the NeverTwice server. Is `python run.py` still running?");
  }
  if (!res.ok || !res.body) {
    let msg = `Request failed (HTTP ${res.status})`;
    try { const data = await res.json(); if (data.error) msg = data.error; } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        try { onEvent(JSON.parse(line.slice(6))); } catch (err) { console.error("bad event", err, line); }
      }
    }
  }
}

function fmtDate(iso, withTime = false) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  const opts = { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toLocaleString("en-IN", opts) + (withTime ? " IST" : "");
}
function relDays(iso, ref) {
  const a = new Date(iso), b = ref ? new Date(ref) : new Date();
  const days = Math.round((b - a) / 86400000);
  if (Number.isNaN(days)) return "";
  if (days <= 0) return "same day";
  if (days === 1) return "1 day earlier";
  if (days < 45) return `${days} days earlier`;
  return `${Math.round(days / 30)} months earlier`;
}
const INC_RE = /INC-\d{3,6}/gi;
const SHOW_NOTES = new URLSearchParams(location.search).has("notes"); // presenter notes: open /?notes=1

// ------------------------------------------------------------------ state
const state = {
  alerts: [],
  customAlerts: [],
  services: [],
  incidents: [],
  incidentIndex: {},
  selected: null,
  runs: {},
  busy: false,
  status: null,
};
function newLane() {
  return { steps: [], warnings: [], error: null, memories: [], runbooks: [], report: null, meta: null, id: null, done: false, started: false };
}
function allAlerts() { return [...state.alerts, ...state.customAlerts]; }
function currentAlert() { return allAlerts().find((a) => a.id === state.selected) || null; }

// ------------------------------------------------------------------ markdown (safe)
function inline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|INC-\d{3,6})/gi;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(h("strong", null, tok.slice(2, -2)));
    else if (tok.startsWith("`")) out.push(h("code", null, tok.slice(1, -1)));
    else out.push(incidentChip(tok.toUpperCase()));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function markdown(src) {
  const root = h("div", { class: "md" });
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  let list = null, para = [], i = 0;
  const flushPara = () => { if (para.length) { root.appendChild(h("p", null, inline(para.join(" ")))); para = []; } };
  const flushList = () => { list = null; };
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      flushPara(); flushList();
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      root.appendChild(h("pre", null, h("code", null, code.join("\n"))));
      i++;
      continue;
    }
    if (trimmed.startsWith("|")) {
      flushPara(); flushList();
      const table = h("table", { class: "log" });
      let header = true;
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        i++;
        if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
        table.appendChild(h("tr", null, cells.map((c) => h(header ? "th" : "td", null, inline(c)))));
        header = false;
      }
      root.appendChild(table);
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    const bullet = /^[-*•]\s+(.*)$/.exec(trimmed);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(trimmed);
    if (!trimmed) { flushPara(); flushList(); }
    else if (heading) { flushPara(); flushList(); root.appendChild(h(heading[1].length <= 2 ? "h3" : "h4", null, inline(heading[2]))); }
    else if (bullet || numbered) {
      flushPara();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) { list = h(tag); root.appendChild(list); }
      list.appendChild(h("li", null, inline((bullet || numbered)[1])));
    } else { flushList(); para.push(trimmed); }
    i++;
  }
  flushPara();
  return root;
}

// ------------------------------------------------------------------ icons, copy, toast
function icon(name, size = 16) {
  const s = (tag, attrs) => svg(tag, { fill: "none", stroke: "currentColor", "stroke-width": 1.6, "stroke-linecap": "round", ...attrs });
  const shapes = {
    rings: [s("circle", { cx: 6.2, cy: 8, r: 4.4, opacity: 0.5 }), s("circle", { cx: 9.8, cy: 8, r: 4.4 })],
    check: [s("circle", { cx: 8, cy: 8, r: 6.4 }), s("path", { d: "M5.2 8.3l1.9 1.9 3.8-4" })],
    cross: [s("circle", { cx: 8, cy: 8, r: 6.4 }), s("path", { d: "M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" })],
    none: [s("circle", { cx: 8, cy: 8, r: 6.4 }), s("path", { d: "M3.6 12.4L12.4 3.6" })],
    flag: [s("path", { d: "M4 14V2.5M4 3h7.5l-1.6 2.6L11.5 8H4" })],
  };
  return svg("svg", { class: "icon", viewBox: "0 0 16 16", width: size, height: size, "aria-hidden": "true" }, shapes[name] || []);
}
let toastTimer = null;
function toast(message) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = h("textarea", { style: "position:fixed;opacity:0" });
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (_e) { /* ignore */ }
    ta.remove();
  }
  toast("Copied");
}
function copyButton(text, label = "Copy") {
  return h("button", { class: "copy-btn", type: "button", onclick: (e) => { e.stopPropagation(); copyText(text); } }, label);
}
function cmdBlock(command) {
  return h("div", { class: "cmd-wrap" }, h("pre", { class: "cmd" }, command), copyButton(command));
}
function istTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
}

// ------------------------------------------------------------------ chips + drawer
function incidentChip(id, opts = {}) {
  const known = state.incidentIndex[id];
  const cls = "chip" + (opts.unverified ? " unverified" : "");
  const title = opts.unverified
    ? "Not found in memory — the model may have invented this ID"
    : known ? `${known.title} (${fmtDate(known.started_at)})` : "Open incident";
  return h("button", { class: cls, type: "button", title, onclick: () => openIncident(id) }, id);
}
function refChip(ref, laneKey) {
  return h("button", {
    class: "chip ref", type: "button", title: "Show the recalled memory",
    onclick: () => focusMemory(laneKey, ref),
  }, ref);
}
function focusMemory(laneKey, ref) {
  const box = document.querySelector(`[data-lane-memories="${laneKey}"]`);
  if (!box) return;
  box.open = true;
  const row = box.querySelector(`[data-ref="${ref}"]`);
  if (row) { row.scrollIntoView({ behavior: "smooth", block: "center" }); row.classList.add("cited"); }
}

async function openIncident(id) {
  const drawer = $("#drawer"), inner = $("#drawer-inner");
  add(clear(inner), h("div", { class: "drawer-top" }, closeBtn(), h("p", { class: "small", style: "margin:0" }, `Loading ${id}…`)));
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("show");
  let inc;
  try { inc = await api(`/api/incidents/${encodeURIComponent(id)}`); }
  catch (err) { add(clear(inner), h("div", { class: "drawer-top" }, closeBtn()), h("div", { class: "drawer-body" }, h("p", { class: "error-box" }, err.message))); return; }
  const body = h("div", { class: "drawer-body" });
  add(clear(inner),
    h("div", { class: "drawer-top" },
      closeBtn(),
      h("div", { class: "alert-meta" }, h("span", { class: `sev ${inc.severity}` }, inc.severity), h("span", { class: "svc", style: "color:inherit" }, inc.service),
        h("span", null, fmtDate(inc.started_at, true)), inc.source === "live" ? h("span", { class: "tag learn" }, "learned in NeverTwice") : null),
      h("h2", null, `${inc.id} · ${inc.title}`)),
    body);
  add(body,
    inc.impact ? h("p", null, inc.impact) : null,
    h("p", { class: "block-title" }, "Root cause"),
    h("p", { style: "margin-top:0" }, inline(inc.root_cause || "")));
  if ((inc.remediations || []).length) {
    add(body, h("p", { class: "block-title" }, "What the team tried"));
    for (const r of inc.remediations) {
      add(body, h("div", { class: `action ${r.outcome === "worked" ? "do" : "dont"}` },
        h("div", { class: "a-title" }, h("span", { class: `outcome ${r.outcome}` }, { worked: "worked", failed: "didn't work", made_worse: "made it worse" }[r.outcome] || r.outcome), h("span", null, r.action)),
        r.command ? cmdBlock(r.command) : null,
        r.result ? h("div", { class: "why" }, r.result) : null));
    }
  }
  if ((inc.timeline || []).length) {
    add(body, h("p", { class: "block-title" }, "Timeline (IST)"), h("ul", { class: "tl" }, inc.timeline.map((t) => h("li", null, h("span", { class: "t" }, t.at), h("span", null, t.event)))));
  }
  if ((inc.action_items || []).length) {
    add(body, h("p", { class: "block-title" }, "Action items"), h("ul", null, inc.action_items.map((a) => h("li", null, `${a.item} `, h("span", { class: a.status === "open" ? "outcome failed" : "outcome worked" }, a.status)))));
  }
  if (inc.notes) add(body, h("p", { class: "block-title" }, "Notes"), h("p", null, inc.notes));
  if (inc.logs) add(body, h("p", { class: "block-title" }, "Log signature"), h("pre", { class: "code" }, inc.logs));
}
function closeBtn() {
  return h("button", { class: "btn quiet close", type: "button", onclick: closeDrawer, "aria-label": "Close" }, "Close");
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("show");
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  const box = $("#status");
  try {
    const s = await api("/api/status");
    state.status = s;
    clear(box);
    const hs = s.hindsight;
    add(box,
      h("span", { class: "pill", title: hs.error || `${hs.base_url} · bank ${hs.bank_id}` },
        h("span", { class: `dot ${hs.ok ? "memory" : "bad"}` }),
        hs.ok ? `Hindsight · ${hs.total_memories ?? "?"} memories` : "Hindsight unreachable"),
      h("span", { class: "pill model-pill", title: `${s.llm.base_url}\nfallback: ${s.llm.fallback_model || "none"}` },
        h("span", { class: `dot ${s.llm.configured ? "ok" : "bad"}` }), s.llm.model),
    );
    const problems = [...(s.config_problems || [])];
    if (!hs.ok && hs.error) problems.push(hs.error);
    else if (hs.ok && !hs.total_memories) problems.push("The memory bank is empty. Run `python scripts/seed_memory.py` to load the incident history.");
    showBanner(problems);
  } catch (err) {
    add(clear(box), h("span", { class: "pill" }, h("span", { class: "dot bad" }), "Server offline"));
    showBanner([err.message]);
  }
}
function showBanner(problems) {
  const banner = $("#banner");
  if (!problems.length) { banner.classList.add("hidden"); return; }
  add(clear(banner), h("strong", null, "Setup: "), problems.map((p, i) => [i ? " · " : "", inline(p)]));
  banner.classList.remove("hidden");
}

// ------------------------------------------------------------------ alerts
async function loadAlerts() {
  const [a, inc] = await Promise.all([api("/api/alerts"), api("/api/incidents")]);
  state.alerts = a.alerts;
  state.services = a.services;
  state.incidents = inc.incidents;
  state.incidentIndex = Object.fromEntries(inc.incidents.map((i) => [i.id, i]));
  renderAlertList();
  const sel = $("#af-service");
  add(clear(sel), state.services.map((s) => h("option", { value: s }, s)));
}
function renderAlertList() {
  const list = clear($("#alert-list"));
  const alerts = allAlerts();
  $("#alert-count").textContent = `${alerts.length} open`;
  const stagger = !state.listShown;
  state.listShown = true;
  alerts.forEach((alert, index) => {
    const demo = alert.learning_demo;
    const triaged = (alert.triaged_modes || []).includes("memory") || (state.runs[alert.id] && state.runs[alert.id].lanes.memory);
    list.appendChild(h("button", {
      class: `alert-card sev-${alert.severity}${stagger ? " enter" : ""}`, style: `--i:${index + 2}`, type: "button",
      "aria-current": alert.id === state.selected ? "true" : "false",
      onclick: () => selectAlert(alert.id),
    },
    h("div", { class: "meta" }, h("span", { class: `sev ${alert.severity}` }, alert.severity), h("span", { class: "svc" }, alert.service),
      h("span", { class: "time" }, istTime(alert.fired_at))),
    h("div", { class: "title" }, alert.title),
    demo || alert.resolved || triaged
      ? h("div", { class: "tags" },
        demo ? h("span", { class: "tag learn", title: demo.label }, demo.step === 1 ? "Learning demo 1/2 · new failure" : "Learning demo 2/2 · after teaching") : null,
        alert.resolved ? h("span", { class: "tag done" }, "✓ resolved") : null,
        triaged && !alert.resolved ? h("span", { class: "tag" }, "triaged") : null)
      : null));
  });
}
function selectAlert(id) {
  if (state.selected !== id) state.animateHead = true;
  state.selected = id;
  renderAlertList();
  renderTriage();
  if (window.matchMedia("(max-width: 860px)").matches) $("#triage-main").scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------------ triage view
function renderTriage() {
  const main = clear($("#triage-main"));
  const alert = currentAlert();
  if (!alert) {
    main.appendChild(h("div", { class: "panel empty" }, h("h2", null, "Pick an alert to triage"),
      h("p", null, "NeverTwice recalls every past incident from Hindsight memory before it answers.")));
    return;
  }
  const logLines = alert.logs ? alert.logs.split("\n").filter(Boolean).length : 0;
  const head = h("div", { class: `panel alert-head sev-${alert.severity}` },
    h("div", { class: "alert-meta" }, h("span", { class: `sev ${alert.severity}` }, alert.severity), h("span", { class: "svc" }, alert.service),
      h("span", null, `fired ${fmtDate(alert.fired_at, true)}`), alert.id ? h("span", { class: "mono small" }, alert.id) : null),
    h("h1", null, alert.title),
    h("dl", { class: "facts" },
      alert.source ? [h("dt", null, "Source"), h("dd", null, alert.source)] : null,
      alert.metrics ? [h("dt", null, "Metrics"), h("dd", null, alert.metrics)] : null,
      alert.recent_changes ? [h("dt", null, "Recent changes"), h("dd", { class: "change-callout" }, alert.recent_changes)] : null),
    alert.logs ? h("div", { class: "terminal" },
      h("div", { class: "terminal-bar" }, h("span", null, `Log lines · ${logLines}`), copyButton(alert.logs)),
      h("pre", null, alert.logs)) : null,
    alert.demo_note && SHOW_NOTES ? h("p", { class: "demo-note" }, "Presenter note: ", alert.demo_note) : null,
    actionButtons());
  if (state.animateHead) { head.classList.add("enter"); state.animateHead = false; }
  main.appendChild(head);
  main.appendChild(h("div", { id: "run-area" }));
  renderRun();
}

function stripState(alert, run) {
  const memLane = run && run.lanes.memory;
  const recalled = new Set();
  if (memLane) for (const m of memLane.memories) for (const id of m.incident_ids || []) recalled.add(id);
  const cited = new Set(memLane && memLane.meta ? memLane.meta.evidence.verified : []);
  const scanning = Boolean(memLane && memLane.started && !memLane.memories.length && !memLane.error && !memLane.done);
  return { memLane, recalled, cited, scanning };
}

// ------------------------------------------------------------------ motion helpers
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
function motionOK() { return !reduceMotion.matches; }
/** Animate a number from 0 to `to` (keeps a suffix such as "%"). */
function countUp(el, to, { suffix = "", duration = 900, delay = 0 } = {}) {
  const target = Number(to);
  if (!Number.isFinite(target) || !motionOK() || target === 0) { el.textContent = `${to}${suffix}`; return; }
  const decimals = String(to).includes(".") ? String(to).split(".")[1].length : 0;
  el.textContent = `${(0).toFixed(decimals)}${suffix}`;
  const start = performance.now() + delay;
  const step = (now) => {
    const t = Math.min(1, Math.max(0, (now - start) / duration));
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = `${(target * eased).toFixed(decimals)}${suffix}`;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ------------------------------------------------------------------ run area (updated in place)
function renderRun() {
  const area = $("#run-area");
  if (!area) return;
  const alert = currentAlert();
  const run = alert && state.runs[alert.id];
  let stripSlot = $("#strip-slot"), lanesSlot = $("#lanes-slot");
  if (!stripSlot || !lanesSlot) {
    clear(area);
    stripSlot = h("div", { id: "strip-slot" });
    lanesSlot = h("div", { id: "lanes-slot" });
    add(area, stripSlot, lanesSlot);
  }
  // Everything below is built once and then updated in place, so animations are never restarted
  // or cut short by the stream of events that follows.
  const key = `${alert ? alert.id : ""}|${state.incidents.length}`;
  if (stripSlot.dataset.key !== key || !stripSlot.firstChild) {
    clear(stripSlot).appendChild(renderTimeline(alert));
    stripSlot.dataset.key = key;
  }
  updateTimeline(stripSlot.firstChild, alert, run);
  const memWorking = Boolean(run && run.lanes.memory && !run.lanes.memory.report && !run.lanes.memory.error && !run.lanes.memory.done);
  document.body.classList.toggle("recalling", memWorking);
  if (!run) { clear(lanesSlot); delete lanesSlot.dataset.run; return; }
  if (lanesSlot.dataset.run !== run.id || !lanesSlot.firstChild) {
    clear(lanesSlot);
    lanesSlot.dataset.run = run.id;
    const lanes = h("div", { class: `lanes ${run.mode === "compare" ? "compare" : ""}` });
    for (const k of ["baseline", "memory"]) if (run.lanes[k]) lanes.appendChild(laneShell(k, run.lanes[k]));
    lanesSlot.appendChild(lanes);
  }
  for (const k of ["baseline", "memory"]) if (run.lanes[k]) updateLane(k, run.lanes[k]);
  const anyReport = Object.values(run.lanes).some((l) => l.report);
  const bar = lanesSlot.querySelector(".resolve-bar");
  if (anyReport && !state.busy && !bar) {
    const animate = !run.barShown;
    run.barShown = true;
    lanesSlot.appendChild(h("div", { class: `panel resolve-bar${animate ? " enter" : ""}` },
      h("div", { class: "lane-icon" }, icon("rings", 20)),
      h("div", { class: "text" }, h("strong", null, "Incident over? Teach NeverTwice what fixed it."),
        "The resolution is retained in Hindsight, so the next on-call engineer gets it instantly."),
      h("button", { class: "btn primary", type: "button", onclick: () => openResolve(alert, run.lanes.memory) }, alert.resolved ? "Resolve again" : "Resolve & teach NeverTwice")));
  } else if (bar && state.busy) {
    bar.remove();
  }
}

function laneShell(key, lane) {
  const isMem = key === "memory";
  const ui = { slots: {}, stepMap: new Map(), warnCount: 0 };
  ui.head = h("div", { class: "lane-head" },
    h("div", { class: "lane-icon" }, icon(isMem ? "rings" : "none", 20)),
    h("div", null, h("h2", null, isMem ? "With Hindsight memory" : "Without memory"),
      h("p", { class: "lane-sub" }, isMem ? "Recalls similar incidents, fixes that worked and fixes that backfired" : "Same model, same prompt, no history — a stateless chatbot")));
  ui.el = h("section", { class: `panel lane ${isMem ? "memory-lane" : "baseline-lane"}`, "aria-live": "polite" }, ui.head);
  for (const name of ["impact", "steps", "warnings", "error", "thinking", "report", "memories"]) {
    ui.slots[name] = h("div", { class: `slot slot-${name}` });
    ui.el.appendChild(ui.slots[name]);
  }
  lane.ui = ui;
  if (!lane.seen) lane.seen = new Set();
  if (!lane.seen.has("shell")) { lane.seen.add("shell"); ui.el.classList.add("enter"); }
  return ui.el;
}

function setStep(li, s) {
  li.className = `${s.status}${li.dataset.enter ? " enter" : ""}`;
  add(clear(li), h("span", { class: "state", "aria-hidden": "true" }), h("span", null, s.label), s.detail ? h("span", { class: "detail" }, `· ${s.detail}`) : null);
}

function thinkingLabel(isMem, lane) {
  if (!lane.started) return "Queued — starts when the no-memory run finishes";
  if (!isMem) return "Reasoning with no history…";
  if (!lane.memories.length) return "Recalling from Hindsight…";
  return `Reasoning over ${lane.memories.length} recalled memories…`;
}

function updateLane(key, lane) {
  const ui = lane.ui;
  if (!ui) return;
  const isMem = key === "memory";
  const once = (k) => { if (lane.seen.has(k)) return false; lane.seen.add(k); return true; };

  if (lane.report && !ui.conf) {
    ui.conf = h("span", { class: `confidence ${lane.report.confidence}${once("conf") ? " pop" : ""}` }, `${lane.report.confidence} confidence`);
    ui.head.appendChild(ui.conf);
  }
  if (lane.report && !ui.impact) {
    ui.impact = renderImpact(isMem, lane, once("impact"));
    ui.slots.impact.appendChild(ui.impact);
  }
  if (!ui.stepsList) { ui.stepsList = h("ul", { class: "steps" }); ui.slots.steps.appendChild(ui.stepsList); }
  for (const s of lane.steps) {
    let li = ui.stepMap.get(s.id);
    if (!li) {
      li = h("li");
      if (once(`step:${s.id}`)) li.dataset.enter = "1";
      ui.stepMap.set(s.id, li);
      ui.stepsList.appendChild(li);
    }
    setStep(li, s);
  }
  if (lane.report && !ui.stepsFolded && lane.steps.length) {
    ui.stepsFolded = true;
    const n = lane.steps.length;
    add(clear(ui.slots.steps), h("details", { class: "steps-box" }, h("summary", null, `How NeverTwice got here (${n} step${n === 1 ? "" : "s"})`), ui.stepsList));
  }
  while (ui.warnCount < lane.warnings.length) {
    const i = ui.warnCount++;
    ui.slots.warnings.appendChild(h("div", { class: `warning-line${once(`warn:${i}`) ? " enter" : ""}` }, lane.warnings[i]));
  }
  if (lane.error && !ui.error) {
    ui.error = h("div", { class: `error-box${once("error") ? " enter" : ""}` }, lane.error);
    ui.slots.error.appendChild(ui.error);
  }
  const waiting = !lane.report && !lane.error && !lane.done;
  if (waiting) {
    if (!ui.thinking) {
      ui.thinkingText = h("span", null);
      ui.thinking = h("div", { class: "thinking" },
        h("div", { class: "thinking-head" }, h("span", { class: "orbit", "aria-hidden": "true" }, h("i"), h("i")), ui.thinkingText),
        h("div", { class: "sk w92" }), h("div", { class: "sk w78" }), h("div", { class: "sk w85" }), h("div", { class: "sk w45" }));
      ui.slots.thinking.appendChild(ui.thinking);
    }
    ui.thinkingText.textContent = thinkingLabel(isMem, lane);
  } else if (ui.thinking) {
    ui.thinking.remove();
    ui.thinking = null;
  }
  ui.el.classList.toggle("working", waiting && lane.started);
  ui.el.classList.toggle("queued", waiting && !lane.started);
  if (lane.report && !ui.report) {
    ui.report = renderReport(key, lane);
    if (once("report")) {
      ui.report.classList.add("reveal");
      Array.from(ui.report.children).forEach((child, i) => child.style.setProperty("--i", String(Math.min(i, 16))));
    }
    ui.slots.report.appendChild(ui.report);
  }
  if (isMem && lane.memories.length && (lane.report || lane.done) && !ui.memories) {
    ui.memories = renderMemories(key, lane);
    ui.slots.memories.appendChild(ui.memories);
  }
}

function renderImpact(isMem, lane, animate) {
  const meta = lane.meta || {};
  const verified = new Set(meta.evidence ? meta.evidence.verified : []);
  const flagged = isMem
    ? lane.report.avoid_actions.filter((a) => (a.evidence || []).some((e) => verified.has(String(e).toUpperCase()))).length
    : 0;
  const cited = isMem ? verified.size : 0;
  const recalled = meta.memories_recalled || 0;
  const cells = [];
  const cell = (value, label, hot, i) => {
    const b = h("b", null, String(value));
    cells.push([b, value, i]);
    return h("div", { class: `${hot ? "hot" : ""}${animate ? " enter" : ""}`, style: `--i:${i}` }, b, h("span", null, label));
  };
  const box = h("div", { class: "impact" },
    cell(recalled, "memories recalled", isMem && recalled > 0, 0),
    cell(cited, `past incident${cited === 1 ? "" : "s"} cited`, isMem && cited > 0, 1),
    cell(flagged, `backfired fix${flagged === 1 ? "" : "es"} flagged`, isMem && flagged > 0, 2));
  if (animate) for (const [b, value, i] of cells) countUp(b, value, { delay: 120 + i * 90 });
  return box;
}

function timelineScale(alert) {
  const incs = state.incidents.filter((i) => i.started_at);
  const times = incs.map((i) => new Date(i.started_at).getTime());
  const nowT = alert && alert.fired_at ? new Date(alert.fired_at).getTime() : Date.now();
  const min = new Date("2026-04-01T00:00:00+05:30").getTime();
  const max = Math.max(nowT, ...times) + 5 * 86400000;
  const pct = (t) => Math.max(0, Math.min(100, ((t - min) / (max - min)) * 100));
  return { incs, nowT, min, max, pct, nowPct: pct(nowT) };
}

function renderTimeline(alert) {
  const panel = h("div", { class: "panel strip" });
  const { incs, min, max, pct, nowPct } = timelineScale(alert);
  if (!incs.length) return panel;
  add(panel, h("div", { class: "strip-head" },
    h("div", null, h("h3", null), h("p", null)),
    h("div", { class: "legend" }, h("span", null, h("i"), "in memory"), h("span", null, h("i", { class: "lg-recalled" }), "recalled"), h("span", null, h("i", { class: "lg-cited" }), "cited as evidence"))));
  const track = h("div", { class: "track", role: "list", "aria-label": "Incident history timeline" });
  let alt = false;
  for (let m = new Date(min); m.getTime() < max; m = new Date(m.getFullYear(), m.getMonth() + 1, 1)) {
    const next = new Date(m.getFullYear(), m.getMonth() + 1, 1).getTime();
    const left = pct(m.getTime()), width = pct(Math.min(next, max)) - left;
    track.appendChild(h("div", { class: `band${alt ? " alt" : ""}`, style: `left:${left}%;width:${width}%` }));
    if (width > 5) track.appendChild(h("span", { class: "month", style: `left:${left}%` }, m.toLocaleString("en-IN", { month: "short" })));
    alt = !alt;
  }
  add(track, h("div", { class: "axis" }), h("div", { class: "future", style: `left:${nowPct}%` }));
  if (alert) add(track, h("span", { class: "now", style: `left:${nowPct}%` }), h("span", { class: "now-label", style: `left:${nowPct}%` }, "this alert"));
  add(track, h("span", { class: "beam", style: `--from:${nowPct}%` }));
  for (const inc of incs) {
    const p = pct(new Date(inc.started_at).getTime());
    const delay = nowPct > 0 ? Math.max(0, (nowPct - p) / nowPct) * 1.1 : 0;
    track.appendChild(h("button", {
      class: ["tick", inc.severity === "SEV1" ? "s1" : "", inc.source === "live" ? "live" : ""].join(" ").trim(),
      type: "button", role: "listitem", style: `left:${p}%;--d:${delay.toFixed(2)}s`, dataset: { id: inc.id, pos: p.toFixed(3), delay: delay.toFixed(2) },
      title: `${inc.id} · ${inc.title} (${fmtDate(inc.started_at)})`, "aria-label": `${inc.id}: ${inc.title}`,
      onclick: () => openIncident(inc.id),
    }));
  }
  panel.appendChild(track);
  return panel;
}

function updateTimeline(panel, alert, run) {
  if (!panel || !panel.querySelector(".track")) return;
  const track = panel.querySelector(".track");
  const { memLane, recalled, cited, scanning } = stripState(alert, run);
  const incs = state.incidents.filter((i) => i.started_at);
  const services = new Set(incs.map((i) => i.service)).size;
  let title, sub;
  if (scanning) {
    title = "Looking back through six months of incidents…";
    sub = "Hindsight is recalling similar incidents, fixes that worked, fixes that backfired and on-call rules";
  } else if (memLane && memLane.memories.length) {
    title = `Looked back at ${recalled.size} past incident${recalled.size === 1 ? "" : "s"}${cited.size ? ` · cited ${cited.size} as evidence` : ""}`;
    sub = `${memLane.memories.length} memories recalled from Hindsight${memLane.meta && memLane.meta.recall_ms >= 100 ? ` in ${(memLane.meta.recall_ms / 1000).toFixed(1)}s` : ""}`;
  } else if (memLane && memLane.done) {
    title = "Nothing relevant in memory for this alert";
    sub = "Resolve it with “Resolve & teach NeverTwice” and the next similar alert will be answered from experience";
  } else {
    title = `${incs.length} incidents in the team's memory`;
    sub = `April to September 2026 · ${services} services · press Triage with memory to look back`;
  }
  panel.querySelector(".strip-head h3").textContent = title;
  panel.querySelector(".strip-head p").textContent = sub;
  track.classList.toggle("scanning", scanning);
  if (run && run.revealPending && recalled.size) {
    run.revealPending = false;
    track.classList.remove("reveal");
    void track.offsetWidth; // restart the sweep if it already ran for a previous triage
    track.classList.add("reveal");
  } else if (!memLane || !memLane.memories.length) {
    track.classList.remove("reveal");
  }
  for (const tick of track.querySelectorAll(".tick")) {
    const id = tick.dataset.id;
    tick.classList.toggle("cited", cited.has(id));
    tick.classList.toggle("recalled", recalled.has(id) && !cited.has(id));
  }
  for (const old of track.querySelectorAll(".tick-label")) old.remove();
  const labelled = Array.from(track.querySelectorAll(".tick.cited")).map((t) => [Number(t.dataset.pos), t.dataset.id, t.dataset.delay]);
  let lastPos = -100;
  for (const [pos, id, delay] of labelled.sort((a, b) => a[0] - b[0])) {
    if (pos - lastPos < 7) continue;
    track.appendChild(h("span", { class: "tick-label", style: `left:${pos}%;--d:${delay}s` }, id));
    lastPos = pos;
  }
}

function evidenceChips(tokens, lane, laneKey, alreadyShown = "") {
  const unverified = new Set(lane.meta ? lane.meta.evidence.unverified : []);
  const shown = new Set((String(alreadyShown).match(INC_RE) || []).map((x) => x.toUpperCase()));
  const chips = [];
  for (const tok of tokens || []) {
    const t = String(tok).toUpperCase();
    if (shown.has(t)) continue;
    if (/^M\d+$/.test(t)) { if (laneKey === "memory") chips.push(refChip(t, laneKey)); }
    else if (/^INC-\d+$/.test(t)) chips.push(incidentChip(t, { unverified: unverified.has(t) || laneKey !== "memory" }));
    else chips.push(h("span", { class: "chip plain" }, tok));
  }
  return chips.length ? h("div", { class: "evidence" }, chips) : null;
}

function renderReport(key, lane) {
  const r = lane.report, meta = lane.meta || {};
  const box = h("div", { class: "report" });
  if (r.summary) box.appendChild(h("p", { class: "summary" }, inline(r.summary)));
  if (r.likely_root_cause) {
    box.appendChild(h("div", { class: "rootcause" }, h("p", { class: "label" }, "Likely root cause"), h("div", null, inline(r.likely_root_cause))));
  }
  if (r.recommended_actions.length) {
    box.appendChild(h("p", { class: "block-title do" }, icon("check"), "Do this"));
    r.recommended_actions.forEach((a, i) => {
      box.appendChild(h("div", { class: "action do" },
        h("div", { class: "a-title" }, h("span", { class: "a-num" }, `${i + 1}`), h("span", null, inline(a.title)), a.requires_approval ? h("span", { class: "approval" }, "needs IC approval") : null),
        a.command ? cmdBlock(a.command) : null,
        a.why ? h("div", { class: "why" }, inline(a.why)) : null,
        evidenceChips(a.evidence, lane, key, `${a.title} ${a.why}`)));
    });
  }
  if (r.avoid_actions.length) {
    box.appendChild(h("p", { class: "block-title dont" }, icon("cross"), "Don't do this"));
    const guarded = new Set((meta.guard || []).map((g) => g.shown_as || g.title));
    if (guarded.size) {
      box.appendChild(h("div", { class: "guard-note" }, icon("flag"),
        h("span", null, h("strong", null, "Backfire guard: "),
          `the AI suggested ${guarded.size} step${guarded.size === 1 ? "" : "s"} that memory shows failed before, so NeverTwice moved ${guarded.size === 1 ? "it" : "them"} here.`)));
    }
    for (const a of r.avoid_actions) {
      box.appendChild(h("div", { class: `action dont${guarded.has(a.title) ? " guarded" : ""}` },
        h("div", { class: "a-title" }, h("span", null, inline(a.title)), guarded.has(a.title) ? h("span", { class: "guard-badge" }, "caught by guard") : null),
        a.why ? h("div", { class: "why" }, inline(a.why)) : null,
        evidenceChips(a.evidence, lane, key, `${a.title} ${a.why}`)));
    }
  }
  if (r.similar_incidents.length) {
    box.appendChild(h("p", { class: "block-title seen" }, icon("rings"), "Seen this before"));
    const unverified = new Set(meta.evidence ? meta.evidence.unverified : []);
    for (const s of r.similar_incidents) {
      const ids = (s.incident_id.match(INC_RE) || []).map((x) => x.toUpperCase());
      const known = ids.map((id) => state.incidentIndex[id]).find(Boolean);
      box.appendChild(h("div", { class: "seen-row" },
        ids.length ? ids.map((id) => incidentChip(id, { unverified: unverified.has(id) || key !== "memory" })) : h("span", { class: "chip plain" }, s.incident_id),
        " ", known && currentAlert() ? h("span", { class: "muted small" }, `${relDays(known.started_at, currentAlert().fired_at)} · `) : null,
        inline(s.similarity || "")));
    }
  }
  if (r.escalate_to) box.appendChild(h("p", { class: "escalate" }, h("strong", null, "Escalate to: "), r.escalate_to));
  if (r.open_questions.length) {
    box.appendChild(h("p", { class: "block-title" }, icon("flag"), "Check before acting"));
    box.appendChild(h("ul", null, r.open_questions.map((q) => h("li", null, inline(q)))));
  }
  const bits = [];
  if (meta.model) bits.push(`model ${meta.model}`);
  if (meta.latency_ms) bits.push(`${(meta.latency_ms / 1000).toFixed(1)}s total`);
  if (meta.runbooks_used && meta.runbooks_used.length) bits.push(`runbooks: ${meta.runbooks_used.join(", ")}`);
  if (meta.tool_calls && meta.tool_calls.length) bits.push(`${meta.tool_calls.length} extra memory lookup${meta.tool_calls.length > 1 ? "s" : ""}`);
  if (meta.evidence && meta.evidence.unverified.length && key === "memory") bits.push(`unverified IDs: ${meta.evidence.unverified.join(", ")}`);
  if (bits.length) box.appendChild(h("div", { class: "meta-row" }, bits.map((b) => h("span", null, b))));
  return box;
}

function renderMemories(key, lane) {
  const cited = new Set((lane.meta && lane.meta.memory_refs_cited) || []);
  const details = h("details", { class: "recall-box", dataset: { laneMemories: key } },
    h("summary", null, `What Hindsight recalled (${lane.memories.length} memories${lane.runbooks.length ? `, ${lane.runbooks.length} runbook excerpts` : ""})`));
  const groups = [["failed", "Fixes that failed or backfired"], ["worked", "Fixes that worked"], ["similar", "Similar incidents & changes"], ["conventions", "On-call rules"]];
  for (const [g, label] of groups) {
    const items = lane.memories.filter((m) => m.group === g);
    if (!items.length) continue;
    details.appendChild(h("p", { class: "mem-group-title" }, label));
    for (const m of items) {
      details.appendChild(h("div", { class: `mem ${cited.has(m.ref) ? "cited" : ""}`, dataset: { ref: m.ref } },
        h("span", { class: "ref" }, m.ref),
        h("div", null, h("div", null, inline(m.text)),
          h("div", { class: "ctx" }, [m.type ? h("span", { class: `type-badge ${m.type}` }, m.type) : null, m.date || "", m.context ? ` · ${m.context}` : ""]))));
    }
  }
  for (const rb of lane.runbooks) {
    details.appendChild(h("p", { class: "mem-group-title" }, `Living runbook: ${rb.name}`));
    details.appendChild(h("div", { class: "runbook-excerpt" }, inline(rb.excerpt)));
  }
  return details;
}

function alertPayload(alert) {
  const { id, title, service, severity, fired_at, source, metrics, logs, recent_changes } = alert;
  return { id, title, service, severity, fired_at, source, metrics, logs, recent_changes };
}

async function runTriage(mode) {
  const alert = currentAlert();
  if (!alert || state.busy) return;
  state.busy = true;
  state.busyMode = mode;
  const run = { id: `run-${Date.now()}`, mode, lanes: {}, revealPending: false };
  if (mode === "compare" || mode === "baseline") run.lanes.baseline = newLane();
  if (mode === "compare" || mode === "memory") run.lanes.memory = newLane();
  state.runs[alert.id] = run;
  renderTriage();
  const body = alert.id && !alert.id.startsWith("CUSTOM-") ? { alert_id: alert.id, mode } : { alert: alertPayload(alert), mode };
  try {
    await stream("/api/triage", body, (ev) => handleTriageEvent(run, ev));
  } catch (err) {
    for (const lane of Object.values(run.lanes)) if (!lane.report && !lane.error) lane.error = err.message;
  } finally {
    state.busy = false;
    state.busyMode = null;
    for (const lane of Object.values(run.lanes)) {
      lane.done = true;
      for (const s of lane.steps) if (s.status === "running") s.status = lane.error ? "error" : "done";
    }
    if (state.selected === alert.id) { renderAlertList(); renderRun(); refreshActions(); }
    document.body.classList.remove("recalling");
    loadStatus();
  }
}
function actionButtons() {
  const busy = state.busy;
  const label = (mode, text) => (busy && state.busyMode === mode
    ? [h("span", { class: "btn-spinner", "aria-hidden": "true" }), mode === "baseline" ? "Thinking…" : "Recalling…"]
    : text);
  return h("div", { class: "actions" },
    h("button", { class: `btn memory${busy && state.busyMode === "memory" ? " busy" : ""}`, type: "button", disabled: busy, onclick: () => runTriage("memory") },
      label("memory", [icon("rings"), "Triage with memory"])),
    h("button", { class: `btn${busy && state.busyMode === "compare" ? " busy" : ""}`, type: "button", disabled: busy, onclick: () => runTriage("compare") },
      label("compare", "Compare with no memory")),
    h("button", { class: "btn quiet", type: "button", disabled: busy, onclick: () => runTriage("baseline") }, label("baseline", "No-memory only")));
}
function refreshActions() {
  const old = $(".alert-head .actions");
  if (old) old.replaceWith(actionButtons());
}

function handleTriageEvent(run, ev) {
  const lane = ev.lane ? run.lanes[ev.lane] : null;
  if (ev.type === "done") return;
  if (!lane) { if (ev.type === "error") for (const l of Object.values(run.lanes)) l.error = l.error || ev.message; renderRun(); return; }
  switch (ev.type) {
    case "start": lane.started = true; break;
    case "step": {
      const existing = lane.steps.find((s) => s.id === ev.id);
      if (existing) Object.assign(existing, { status: ev.status, detail: ev.detail || existing.detail, label: ev.label || existing.label });
      else lane.steps.push({ id: ev.id, label: ev.label, status: ev.status, detail: ev.detail || "" });
      break;
    }
    case "memories":
      lane.memories = ev.items || []; lane.runbooks = ev.runbooks || [];
      if (ev.lane === "memory") run.revealPending = true;
      break;
    case "warning": lane.warnings.push(ev.message); break;
    case "error": lane.error = ev.message; break;
    case "report": lane.report = ev.report; lane.meta = ev.meta; lane.id = ev.id; break;
    default: break;
  }
  if (currentAlert() && state.runs[currentAlert().id] === run) renderRun();
}

// ------------------------------------------------------------------ resolve dialog
function linesOf(text) { return String(text || "").split("\n").map((s) => s.trim()).filter(Boolean); }

function openResolve(alert, memLane) {
  const dlg = $("#resolve-dialog"), body = clear($("#resolve-body")), foot = clear($("#resolve-foot"));
  const sug = alert.suggested_resolution;
  const top = memLane && memLane.report && memLane.report.recommended_actions.length
    ? (memLane.report.recommended_actions.find((a) => !/^(check|verify|confirm|inspect|look|review|investigate)\b/i.test(a.title)) || memLane.report.recommended_actions[0]).title
    : "";
  add(body, 
    h("h2", { id: "resolve-title" }, "Resolve & teach NeverTwice"),
    h("p", { class: "muted small" }, "This becomes a postmortem in Hindsight. Fixes are stored separately by outcome, so next time NeverTwice can say what worked and what backfired."),
    sug ? h("button", { class: "btn", type: "button", onclick: () => fill(sug) }, "Use the suggested resolution (demo)") : null,
    field("rs-title", "Incident title", h("input", { id: "rs-title", required: true, minlength: 3, value: alert.title })),
    field("rs-root", "Root cause", h("textarea", { id: "rs-root", required: true, minlength: 10, placeholder: "What actually caused it?" })),
    field("rs-worked", "What fixed it", h("textarea", { id: "rs-worked", placeholder: "One action per line" }), "one per line"),
    h("div", { class: "row2" },
      field("rs-failed", "Tried, didn't help", h("textarea", { id: "rs-failed", placeholder: "One per line" })),
      field("rs-worse", "Made it worse", h("textarea", { id: "rs-worse", placeholder: "One per line" }))),
    field("rs-notes", "Notes for next time", h("textarea", { id: "rs-notes", placeholder: "Anything the next on-call engineer should check first" }), "optional"),
    field("rs-responder", "Responder", h("input", { id: "rs-responder", value: "On-call engineer" })),
    top ? h("div", { class: "field" }, h("label", null, "Was NeverTwice's first recommendation right?"),
      h("p", { class: "small muted", style: "margin:0" }, `“${top}”`),
      h("div", { class: "radio-row" }, ["yes", "partly", "no"].map((v) => h("label", null, h("input", { type: "radio", name: "rs-fb", value: v }), ` ${v}`)))) : null,
    h("div", { id: "rs-progress", class: "progress" }),
  );
  function fill(s) {
    $("#rs-title").value = s.title || alert.title;
    $("#rs-root").value = s.root_cause || "";
    $("#rs-worked").value = (s.worked || []).join("\n");
    $("#rs-failed").value = (s.failed || []).join("\n");
    $("#rs-worse").value = (s.made_worse || []).join("\n");
    $("#rs-notes").value = s.notes || "";
    $("#rs-responder").value = s.responder || "On-call engineer";
    const no = body.querySelector('input[name="rs-fb"][value="no"]');
    if (no && !body.querySelector('input[name="rs-fb"]:checked')) no.checked = true;
  }
  const cancel = h("button", { class: "btn", value: "cancel", formnovalidate: true, type: "submit" }, "Cancel");
  const submit = h("button", { class: "btn primary", type: "submit", value: "save" }, "Save to memory");
  resolveCtx = { alert, memLane, submit, cancel };
  add(foot, submit, cancel); // primary first: Enter in a field saves instead of cancelling
  dlg.showModal();
}
let resolveCtx = null;
function setupResolveForm() {
  $("#resolve-form").addEventListener("submit", (e) => {
    const action = e.submitter ? e.submitter.value : "save";
    if (action !== "save") return; // cancel / done close the dialog
    e.preventDefault();
    if (resolveCtx && !resolveCtx.submit.disabled) submitResolve(resolveCtx.alert, resolveCtx.memLane, resolveCtx.submit, resolveCtx.cancel);
  });
}
function field(id, label, input, hint) {
  return h("div", { class: "field" }, h("label", { for: id }, label, hint ? h("span", { class: "hint" }, ` (${hint})`) : null), input);
}

async function submitResolve(alert, memLane, submit, cancel) {
  const progress = clear($("#rs-progress"));
  const root = $("#rs-root").value.trim(), title = $("#rs-title").value.trim();
  if (title.length < 3 || root.length < 10) {
    progress.appendChild(h("div", { class: "error-box" }, "Add a title and a root cause (at least 10 characters)."));
    return;
  }
  const fb = document.querySelector('input[name="rs-fb"]:checked');
  const payload = {
    alert_id: alert.id && !alert.id.startsWith("CUSTOM-") ? alert.id : "",
    triage_id: memLane && memLane.id ? memLane.id : "",
    title, service: alert.service, severity: alert.severity || "SEV2",
    started_at: alert.fired_at || undefined,
    root_cause: root,
    worked: linesOf($("#rs-worked").value), failed: linesOf($("#rs-failed").value), made_worse: linesOf($("#rs-worse").value),
    notes: $("#rs-notes").value.trim(), responder: $("#rs-responder").value.trim() || "On-call engineer",
    recommendation_feedback: fb ? fb.value : "",
  };
  submit.disabled = true; cancel.disabled = true;
  const steps = [];
  const list = h("ul", { class: "steps" });
  const bar = h("p", { class: "small muted" });
  const fill = h("i");
  const meter = h("div", { class: "xbar indeterminate", role: "progressbar", "aria-label": "Saving to Hindsight" }, fill);
  add(progress, meter, list, bar);
  const draw = () => {
    add(clear(list), steps.map((s) => h("li", { class: s.status }, h("span", { class: "state" }), h("span", null, s.label), s.detail ? h("span", { class: "detail" }, `· ${s.detail}`) : null)));
  };
  let resolved = null, error = null;
  try {
    await stream("/api/resolve", payload, (ev) => {
      if (ev.type === "incident") bar.textContent = `Saving as ${ev.incident_id} (${ev.items} memory items)…`;
      else if (ev.type === "step") {
        const s = steps.find((x) => x.id === ev.id);
        if (s) Object.assign(s, ev); else steps.push({ ...ev });
        draw();
      } else if (ev.type === "progress") {
        bar.textContent = `Hindsight extraction ${ev.done}/${ev.total} operations done`;
        if (ev.total) { meter.classList.remove("indeterminate"); fill.style.width = `${Math.max(8, (ev.done / ev.total) * 100)}%`; }
      }
      else if (ev.type === "resolved") resolved = ev;
      else if (ev.type === "error") error = ev.message;
    });
  } catch (err) { error = err.message; }
  cancel.disabled = false;
  meter.classList.remove("indeterminate");
  fill.style.width = "100%";
  meter.classList.add(error || !resolved ? "failed" : "complete");
  if (error || !resolved) {
    submit.disabled = false;
    progress.appendChild(h("div", { class: "error-box" }, error || "The server closed the stream before finishing."));
    return;
  }
  const inc = resolved.incident;
  const next = alert.learning_demo && alert.learning_demo.step === 1 ? state.alerts.find((a) => a.learning_demo && a.learning_demo.step === 2) : null;
  const check = svg("svg", { class: "check-draw", viewBox: "0 0 24 24", width: 22, height: 22, "aria-hidden": "true" },
    svg("circle", { cx: 12, cy: 12, r: 10.5, fill: "none", stroke: "currentColor", "stroke-width": 1.8 }),
    svg("path", { d: "M7 12.4l3.2 3.2L17 8.8", fill: "none", stroke: "currentColor", "stroke-width": 2.2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  const done = h("div", { class: "success enter", role: "status" }, check,
    h("strong", null, `Stored as ${inc.id}. `),
    inc.memory_status === "stored" ? "Hindsight has extracted the facts; NeverTwice will recall this on the next similar alert." : "Hindsight is still processing in the background; it will be recallable shortly.");
  progress.appendChild(done);
  done.scrollIntoView({ block: "nearest" });
  add(clear($("#resolve-foot")), 
    next ? h("button", { class: "btn memory nudge", type: "button", onclick: () => { $("#resolve-dialog").close(); selectAlert(next.id); runTriage("memory"); } }, icon("rings"), `Now triage ${next.id}`) : null,
    h("button", { class: "btn", type: "submit", value: "done" }, "Close"));
  await loadAlerts();
  renderTriage();
  loadStatus();
}

// ------------------------------------------------------------------ custom alert
function setupCustomAlert() {
  const dlg = $("#alert-dialog");
  $("#new-alert").addEventListener("click", () => { $("#alert-form").reset(); dlg.showModal(); });
  $("#alert-form").addEventListener("submit", (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    const title = $("#af-title").value.trim();
    if (title.length < 3) { e.preventDefault(); $("#af-title").focus(); return; }
    const alert = {
      id: `CUSTOM-${state.customAlerts.length + 1}`,
      title, service: $("#af-service").value, severity: $("#af-sev").value,
      fired_at: new Date().toISOString(), source: "Written in NeverTwice", metrics: $("#af-metrics").value.trim(),
      logs: $("#af-logs").value.trim(), recent_changes: $("#af-changes").value.trim(),
    };
    state.customAlerts.unshift(alert);
    selectAlert(alert.id);
  });
}

// ------------------------------------------------------------------ ask view
const SUGGESTIONS = [
  "What keeps breaking checkout-api, and what actually fixes it?",
  "Which fixes have backfired on us, and why?",
  "Which postmortem action items are still open, and what risk do they leave?",
  "What should a new engineer know before their first payments on-call shift?",
  "Which incidents were triggered by a deploy or config change?",
];
function setupAsk() {
  const box = $("#ask-suggestions");
  for (const q of SUGGESTIONS) box.appendChild(h("button", { class: "suggestion", type: "button", onclick: () => { $("#ask-input").value = q; ask(q); } }, q));
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#ask-input").value.trim(); if (q.length >= 3) ask(q); });
}
async function ask(question) {
  const out = clear($("#ask-answer"));
  const btn = $("#ask-btn");
  btn.disabled = true;
  const started = Date.now();
  const waiting = h("div", { class: "panel answer" }, h("ul", { class: "steps" }, h("li", { class: "running" }, h("span", { class: "state" }), h("span", null, "Hindsight is reflecting over runbooks, observations and facts…"), h("span", { class: "detail", id: "ask-timer" }))));
  out.appendChild(waiting);
  const timer = setInterval(() => { const t = $("#ask-timer"); if (t) t.textContent = `· ${Math.round((Date.now() - started) / 1000)}s`; }, 1000);
  try {
    const data = await api("/api/ask", { method: "POST", body: { question } });
    clear(out);
    const panel = h("div", { class: "panel answer" }, h("p", { class: "small muted" }, `Answered in ${(data.latency_ms / 1000).toFixed(1)}s by Hindsight reflect`), markdown(data.text));
    const src = h("div", { class: "sources" });
    if (data.mental_models.length) add(src, h("p", { class: "block-title" }, "Runbooks consulted"), h("div", { class: "evidence" }, data.mental_models.map((m) => h("span", { class: "chip plain" }, m.name || m.id))));
    if (data.directives.length) add(src, h("p", { class: "block-title" }, "Directives applied"), h("div", { class: "evidence" }, data.directives.map((d) => h("span", { class: "chip plain" }, d.name || d.id))));
    if (data.memories.length) {
      add(src, h("p", { class: "block-title" }, `Evidence (${data.memories.length} memories)`));
      for (const m of data.memories) {
        src.appendChild(h("div", { class: "fact" }, h("span", { class: `type-badge ${m.type || ""}` }, m.type || "fact"), inline(m.text || ""),
          h("div", { class: "ctx" }, [m.date, m.context].filter(Boolean).join(" · "))));
      }
    }
    if (src.childNodes.length) panel.appendChild(src);
    out.appendChild(panel);
  } catch (err) {
    clear(out).appendChild(h("div", { class: "error-box" }, err.message));
  } finally {
    clearInterval(timer);
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ memory view
async function loadMemory() {
  const models = clear($("#models")), counts = clear($("#counts")), directives = clear($("#directives"));
  models.appendChild(h("p", { class: "muted" }, "Loading from Hindsight…"));
  let data;
  try { data = await api("/api/memory/overview"); }
  catch (err) { clear(models).appendChild(h("div", { class: "error-box" }, err.message)); return; }
  $("#bank-id").textContent = data.bank_id;
  if (data.error) { add(clear(models), h("div", { class: "error-box" }, `Could not read the memory bank: ${data.error}`)); return; }
  const c = data.counts || {};
  const tiles = [["world", "world facts"], ["experience", "experience facts"], ["observation", "observations"]];
  for (const [k, label] of tiles) counts.appendChild(h("div", { class: `count${k === "observation" ? " hot" : ""}` }, h("b", null, c[k] ?? "–"), h("span", null, label)));
  counts.appendChild(h("div", { class: "count hot" }, h("b", null, data.mental_models.length), h("span", null, "living runbooks")));
  requestAnimationFrame(() => animateNumbers("#counts .count b"));
  counts.appendChild(h("div", { class: "count" }, h("b", null, data.directives.length), h("span", null, "directives")));
  clear(models);
  if (!data.mental_models.length) models.appendChild(h("div", { class: "panel empty" }, h("h2", null, "No runbooks yet"), h("p", null, "Run `python scripts/seed_memory.py` to create them.")));
  for (const m of data.mental_models) {
    const refreshBtn = h("button", { class: "btn quiet", type: "button" }, "Refresh");
    refreshBtn.addEventListener("click", async () => {
      refreshBtn.disabled = true; refreshBtn.textContent = "Refreshing…";
      try { await api(`/api/memory/mental-models/${encodeURIComponent(m.id)}/refresh`, { method: "POST", body: {} }); refreshBtn.textContent = "Refresh queued"; toast("Hindsight is regenerating this runbook"); }
      catch (err) { refreshBtn.textContent = "Refresh failed"; refreshBtn.title = err.message; refreshBtn.disabled = false; }
    });
    models.appendChild(h("article", { class: "panel model-card" },
      h("header", null, h("h3", null, h("span", { style: "color:var(--memory)" }, icon("rings")), m.name || m.id), h("span", { class: "small muted" }, m.last_refreshed_at ? `updated ${fmtDate(m.last_refreshed_at, true)}` : ""), refreshBtn),
      m.source_query ? h("p", { class: "q" }, `Question it answers: ${m.source_query}`) : null,
      m.content ? markdown(m.content) : h("p", { class: "muted" }, "Being generated by Hindsight…")));
  }
  if (!data.directives.length) directives.appendChild(h("p", { class: "muted small" }, "No directives."));
  for (const d of data.directives) directives.appendChild(h("div", { class: "directive" }, h("b", null, d.name), inline(d.content || "")));
  browse();
}
async function browse() {
  const out = clear($("#facts"));
  const q = $("#browse-q").value.trim(), type = $("#browse-type").value;
  out.appendChild(h("p", { class: "muted small" }, "Searching…"));
  try {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (type) params.set("type", type);
    const data = await api(`/api/memory/list?${params}`);
    clear(out).appendChild(h("p", { class: "small muted" }, `${data.total} matching facts${data.total > data.items.length ? ` (showing ${data.items.length})` : ""}`));
    for (const f of data.items) {
      out.appendChild(h("div", { class: "fact" }, h("span", { class: `type-badge ${f.type || ""}` }, f.type || "fact"), inline(f.text || ""),
        h("div", { class: "ctx" }, [f.date, f.context].filter(Boolean).join(" · "))));
    }
  } catch (err) { clear(out).appendChild(h("div", { class: "error-box" }, err.message)); }
}

// ------------------------------------------------------------------ learning view
async function loadLearning() {
  const evalCard = clear($("#eval-card")), fbCard = clear($("#feedback-card")), logBox = clear($("#triage-log"));
  let data;
  try { data = await api("/api/learning"); }
  catch (err) { evalCard.appendChild(h("div", { class: "error-box" }, err.message)); return; }
  renderEval(evalCard, data.evaluation);
  renderQuality(clear($("#quality-card")), data.quality);
  requestAnimationFrame(() => animateNumbers("#view-learning .bignum b"));
  const s = data.summary;
  add(fbCard, h("h2", { class: "section-title", style: "margin-left:0" }, "Responder feedback in this app"),
    h("p", { class: "small muted" }, "When an incident is resolved, the responder rates NeverTwice's first recommendation."),
    h("div", { class: "bignums" },
      bignum(s.memory.accuracy, "with memory: rated correct", true, s.memory.rated),
      bignum(s.baseline.accuracy, "without memory: rated correct", false, s.baseline.rated)),
    h("div", { class: "bignums" },
      h("div", { class: "bignum bignum-mem" }, h("b", null, s.memory.avg_memories), h("span", null, "memories recalled per triage")),
      h("div", { class: "bignum" }, h("b", null, s.memory.triages + s.baseline.triages), h("span", null, "triages run"))));
  const rows = data.entries.slice().reverse();
  if (!rows.length) { logBox.appendChild(h("p", { class: "muted" }, "No triages yet. Pick an alert and press “Triage with memory”.")); return; }
  logBox.appendChild(h("div", { class: "table-scroll" }, h("table", { class: "log" },
    h("thead", null, h("tr", null, ["When", "Alert", "Mode", "Memories", "Confidence", "Cited", "Feedback"].map((t) => h("th", null, t)))),
    h("tbody", null, rows.map((e) => h("tr", null,
      h("td", null, fmtDate(e.ts, true)), h("td", null, h("span", { class: "svc" }, e.service), " ", e.title),
      h("td", null, h("span", { class: `mode-badge ${e.mode}` }, e.mode)), h("td", null, e.memories_recalled),
      h("td", null, e.confidence), h("td", null, (e.incidents_cited || []).map((i) => incidentChip(i))), h("td", null, e.feedback || "–")))))));
}
function bignum(value, label, mem, rated) {
  return h("div", { class: `bignum ${mem ? "bignum-mem" : ""}` }, h("b", null, value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`),
    h("span", null, `${label}${rated ? ` (${rated} rated)` : " (no ratings yet)"}`));
}
function renderEval(card, ev) {
  add(card, h("h2", { class: "section-title", style: "margin-left:0" }, "Replay: learning curve over the incident history"));
  if (!ev || !ev.points || !ev.points.length) {
    add(card, h("p", null, "Not measured yet. Run the replay to see how often the fix that actually worked appears in NeverTwice's top two actions as memory grows:"),
      h("pre", { class: "code" }, "python scripts/eval_learning_curve.py --baseline"),
      h("p", { class: "small muted" }, "It replays the 15 historical incidents into a separate evaluation bank, one at a time, and triages each before it is remembered."));
    return;
  }
  const sm = ev.summary || {};
  add(card, h("div", { class: "bignums" },
    h("div", { class: "bignum bignum-mem" }, h("b", null, pctOf(sm.repeat_memory_hit_rate)), h("span", null, `recurring incidents, with memory (${sm.repeats})`)),
    h("div", { class: "bignum" }, h("b", null, pctOf(sm.repeat_baseline_hit_rate)), h("span", null, "recurring incidents, without memory")),
    h("div", { class: "bignum bignum-mem" }, h("b", null, pctOf(sm.memory_hit_rate)), h("span", null, "all incidents, with memory")),
    h("div", { class: "bignum" }, h("b", null, pctOf(sm.baseline_hit_rate)), h("span", null, "all incidents, without memory"))));
  const pts = ev.points;
  const W = 640, H = 230, L = 36, R = 12, T = 14, B = 46;
  const x = (i) => L + (pts.length === 1 ? 0 : (i * (W - L - R)) / (pts.length - 1));
  const y = (v) => T + (1 - v) * (H - T - B);
  const cumulative = (key) => { let hit = 0, n = 0; return pts.map((p) => { if (typeof p[key] === "boolean") { n++; if (p[key]) hit++; } return n ? hit / n : null; }); };
  const chart = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Cumulative fix-found rate with and without memory" });
  for (const v of [0, 0.5, 1]) add(chart, svg("line", { class: "grid", x1: L, x2: W - R, y1: y(v), y2: y(v) }), svg("text", { x: 4, y: y(v) + 4 }, `${v * 100}%`));
  const path = (vals) => vals.map((v, i) => (v === null ? "" : `${i && vals[i - 1] !== null ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join(" ");
  const base = cumulative("baseline_hit"), mem = cumulative("memory_hit");
  if (base.some((v) => v !== null)) chart.appendChild(svg("path", { class: "base-line", d: path(base) }));
  const memPath = svg("path", { class: "mem-line", d: path(mem) });
  chart.appendChild(memPath);
  pts.forEach((p, i) => {
    const fill = p.memory_hit ? "var(--memory-glow)" : "var(--surface)";
    const dot = svg("circle", { class: "dot-in", style: `--i:${i}`, cx: x(i), cy: H - B + 14, r: 5, fill, stroke: p.repeat ? "var(--memory-strong)" : "var(--ink-3)", "stroke-width": p.repeat ? 2.5 : 1.5 });
    dot.appendChild(svg("title", null, `${p.incident_id} ${p.repeat ? "(recurring)" : "(first time)"}: memory ${p.memory_hit ? "found the fix" : "missed"}${typeof p.baseline_hit === "boolean" ? `, baseline ${p.baseline_hit ? "found it" : "missed"}` : ""}`));
    chart.appendChild(dot);
  });
  add(chart, svg("text", { x: L, y: H - 6 }, "incidents already in memory →"), svg("text", { x: W - R - 170, y: T + 10 }, "— with memory   - - without"));
  requestAnimationFrame(() => {
    try { memPath.style.setProperty("--len", String(Math.ceil(memPath.getTotalLength()))); memPath.classList.add("draw"); } catch (_) { /* not rendered */ }
  });
  add(card, chart, h("p", { class: "small muted" }, `Lines: cumulative rate at which a top-2 action contains the fix that actually worked. Dots: each incident in date order (thick ring = recurring failure mode; filled = memory found the fix). Model ${ev.model}, measured ${fmtDate(ev.generated_at, true)}.`));
}
function renderQuality(card, q) {
  add(card, h("h2", { class: "section-title", style: "margin-left:0" }, "Answer quality: with vs without memory"));
  if (!q || !q.results || !q.results.length) {
    add(card, h("p", null, "Not measured yet. Grade every demo alert against its checklist (must recommend / must not recommend / must cite):"),
      h("pre", { class: "code" }, "python scripts/eval_alerts.py --baseline --runs 3"));
    return;
  }
  const sm = q.summary || {};
  const bar = (label, s, mem) => {
    if (!s || !s.total) return null;
    const pct = Math.round((s.passed / s.total) * 100);
    return h("div", { class: "qrow" },
      h("div", { class: "qlabel" }, h("strong", null, label), h("span", { class: "muted small" }, ` ${s.passed}/${s.total} passed`)),
      h("div", { class: `qbar${mem ? " qmem" : ""}` }, h("i", { style: `width:${Math.max(pct, 2)}%` })),
      h("div", { class: `qpct${mem ? " qmem" : ""}` }, `${pct}%`),
      h("div", { class: "qbad" }, `known-bad fix recommended in ${s.recommended_bad_fix}/${s.total}`));
  };
  add(card, bar("With Hindsight memory", sm.memory, true), bar("Without memory", sm.baseline, false));
  const alerts = [...new Set(q.results.map((r) => r.alert))];
  const cell = (alert, mode) => {
    const rows = q.results.filter((r) => r.alert === alert && r.mode === mode && r.passed !== null && r.passed !== undefined);
    if (!rows.length) return h("td", null, "–");
    const passed = rows.filter((r) => r.passed).length;
    const bad = rows.find((r) => (r.recommended_bad_fix || []).length);
    return h("td", { title: bad ? `Recommended: ${bad.recommended_bad_fix[0]}` : "" },
      h("span", { class: `qchip ${passed === rows.length ? "ok" : passed ? "mid" : "bad"}` }, `${passed}/${rows.length}`),
      bad ? h("div", { class: "muted small" }, `e.g. “${bad.recommended_bad_fix[0].slice(0, 60)}”`) : null);
  };
  add(card, h("div", { class: "table-scroll" }, h("table", { class: "log qtable" },
    h("thead", null, h("tr", null, ["Alert", "With memory", "Without memory"].map((t) => h("th", null, t)))),
    h("tbody", null, alerts.map((a) => {
      const al = state.alerts.find((x) => x.id === a);
      return h("tr", null, h("td", null, h("span", { class: "mono small" }, a), " ", h("span", { class: "qtitle" }, al ? al.title : "")), cell(a, "memory"), cell(a, "baseline"));
    })))),
    h("p", { class: "small muted" }, `Model ${q.model}, measured ${fmtDate(q.generated_at, true)}. Checklists: data/alert_checks.json.`));
}
function pctOf(v) { return v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`; }

function animateNumbers(selector) {
  $$(selector).forEach((b, i) => {
    const m = /^(\d+(?:\.\d+)?)(%?)$/.exec(b.textContent.trim());
    if (m) countUp(b, m[1], { suffix: m[2], delay: i * 70, duration: 800 });
  });
}

// ------------------------------------------------------------------ tabs, theme, boot
function setupTabs() {
  for (const tab of $$(".tab")) {
    tab.addEventListener("click", () => {
      for (const t of $$(".tab")) t.setAttribute("aria-selected", String(t === tab));
      for (const v of $$(".view")) v.classList.toggle("active", v.id === `view-${tab.dataset.view}`);
      if (tab.dataset.view === "memory") loadMemory();
      if (tab.dataset.view === "learning") loadLearning();
      if (tab.dataset.view === "ask") $("#ask-input").focus();
    });
  }
  $("#browse-form").addEventListener("submit", (e) => { e.preventDefault(); browse(); });
}
function setupTheme() {
  const root = document.documentElement;
  try { const saved = localStorage.getItem("nevertwice-theme"); if (saved) root.dataset.theme = saved; } catch (_) { /* storage unavailable */ }
  $("#theme-toggle").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("nevertwice-theme", root.dataset.theme); } catch (_) { /* ignore */ }
  });
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
document.addEventListener("click", (e) => {
  const drawer = $("#drawer");
  if (!drawer.classList.contains("open")) return;
  if (drawer.contains(e.target) || e.target.closest(".chip, .tick")) return;
  closeDrawer();
});
document.addEventListener("DOMContentLoaded", async () => {
  setupTabs();
  setupTheme();
  setupAsk();
  setupCustomAlert();
  setupResolveForm();
  loadStatus();
  try {
    await loadAlerts();
    const params = new URLSearchParams(location.search);
    const first = params.get("alert") || (state.alerts[0] && state.alerts[0].id);
    if (first) selectAlert(first);
  } catch (err) {
    showBanner([err.message]);
  }
});
