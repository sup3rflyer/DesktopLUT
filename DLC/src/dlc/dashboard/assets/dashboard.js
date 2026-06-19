/* DLC mission control — the dumb renderer.
 *
 * All numbers come from the server (it does the colour math + aggregation); this file
 * only paints them. It keeps a capped in-memory log and renders a bounded window of rows
 * so a 10k-patch run can't blow the DOM. */
"use strict";

const MAX_LOG = 20000;      // ring-buffer cap for retained events
const RENDER_CAP = 700;     // most-recent matching rows actually put in the DOM
const $ = (id) => document.getElementById(id);

let logData = [];           // {time, level, stage, event, phase, tier, data, derived?}
let knownStages = new Set();
let renderQueued = false;
let lastState = null;
let csrfToken = null;

/* ── helpers ─────────────────────────────────────────────────── */
const LEVEL_RANK = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function num(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(d);
}
function clockTime(iso) {
  if (!iso) return "—";
  const t = iso.indexOf("T");
  return t >= 0 ? iso.slice(t + 1, t + 12) : iso;
}
function fmtDur(s) {
  if (s === null || s === undefined) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}
function deClass(v) {
  if (v === null || v === undefined) return "";
  if (v <= 1.0) return "de-ok";
  if (v <= 2.3) return "de-warn";
  return "de-bad";
}
function setDe(id, v, d = 2) {
  const el = $(id);
  el.textContent = num(v, d);
  el.className = deClass(v);
}
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 2600);
}

async function ensureCsrfToken() {
  if (csrfToken) return csrfToken;
  const r = await fetch("/api/snapshot", { cache: "no-store" });
  const j = await r.json();
  csrfToken = j.csrf_token;
  if (j.state) renderState(j.state);
  if (j.backlog) seedLog(j.backlog);
  return csrfToken;
}

async function postJson(path) {
  const token = await ensureCsrfToken();
  const r = await fetch(path, {
    method: "POST",
    headers: { "X-DLC-CSRF-Token": token },
  });
  return r.json();
}

/* ── status bar + sidebar (from `state`) ─────────────────────── */
function renderState(s) {
  lastState = s;
  const h = s.header || {};
  $("f-run").textContent = h.run_id || s.run_id || "—";
  $("f-display").textContent = h.display || "—";
  $("f-monitor").textContent = (h.monitor !== undefined && h.monitor !== null) ? h.monitor : "—";
  $("f-mode").textContent = h.mode || "—";
  $("f-flow").textContent = h.flow || "—";
  $("f-bits").textContent = h.bit_depth ? `${h.bit_depth}-bit` : "—";
  $("f-target").textContent = h.target || "—";
  const w = h.white || {};
  const wxy = w.xy ? `${num(w.xy[0], 4)},${num(w.xy[1], 4)}` : "";
  $("f-white").textContent = w.cct ? `${Math.round(w.cct)}K${wxy ? " · " + wxy : ""}` : (wxy || "—");
  $("f-ccmx").textContent = h.ccmx || "—";
  $("f-spd").textContent = h.spd || "—";

  // liveness
  const lv = s.liveness || {};
  const dot = $("live-dot");
  const light = lv.light || "unknown";
  dot.className = "dot " + light;
  const LIVE_LABEL = { live: "live", slow: "not advancing", stalled: "STALLED",
                       paused: "paused (awaiting decision)", done: "done", unknown: "—" };
  $("live-text").textContent = LIVE_LABEL[light] || light;
  $("live-age").textContent = (lv.age_s !== null && lv.age_s !== undefined) ? `${num(lv.age_s, 0)}s ago` : "";

  // phase header
  $("ph-phase").textContent = s.phase || "—";
  $("ph-stage").textContent = s.stage || "—";
  const st = s.run_status || "idle";
  $("ph-status").innerHTML = `<span class="badge ${esc(st)}">${esc(st)}</span>`;

  // progress
  const c = s.counters || {};
  const pct = c.patches_total ? Math.min(100, 100 * c.patches_done / c.patches_total) : 0;
  $("prog-fill").style.width = pct + "%";
  $("prog-patches").textContent = `${c.patches_done || 0} / ${c.patches_total || 0}`;
  $("prog-reads").textContent = c.reads || 0;
  $("prog-okfail").innerHTML = `<span class="ok">${c.reads_ok || 0}</span> / <span class="${(c.reads_failed) ? "nok" : ""}">${c.reads_failed || 0}</span>`;

  // timers
  const t = s.timers || {};
  $("t-run").textContent = fmtDur(t.run_elapsed_s);
  $("t-stage").textContent = fmtDur(t.stage_elapsed_s);
  $("t-eta").textContent = (st === "running") ? fmtDur(t.eta_s) : "—";
  $("t-sread").textContent = (t.s_per_read != null) ? num(t.s_per_read, 2) + "s" : "—";
  $("t-spatch").textContent = (t.s_per_patch != null) ? num(t.s_per_patch, 1) + "s" : "—";
  // "since progress" — the wedge tell: it keeps growing if the run is alive but stuck,
  // and is coloured by the liveness verdict so a syscall wedge is visible before the stall.
  const tp = $("t-progress");
  tp.textContent = (lv.progress_age_s != null) ? fmtDur(lv.progress_age_s) : "—";
  tp.className = light === "stalled" ? "prog-stalled" : (light === "slow" ? "prog-slow" : "");

  // dE big-numbers (from the scoring stage)
  const de = s.de || {};
  $("de-source").textContent = de.phase ? `${de.phase}${de.iteration != null ? " #" + de.iteration : ""}` : "";
  setDe("de-avg", de.avg); setDe("de-p95", de.p95); setDe("de-p99", de.p99); setDe("de-max", de.max);
  setDe("de-white", de.white);
  setDe("de-gray", de.grayscale); setDe("de-colour", de.colour);

  // live white point
  const lw = s.last_white || {};
  $("w-cct").textContent = lw.cct ? `${Math.round(lw.cct)} K` : "—";
  $("w-duv").textContent = (lw.duv != null) ? num(lw.duv, 4) : "—";
  $("w-xy").textContent = lw.xy ? `${num(lw.xy[0], 4)}, ${num(lw.xy[1], 4)}` : "—";
  $("w-Y").textContent = (lw.Y != null) ? num(lw.Y, 2) : "—";

  // attention flags
  flag("flag-stall", s.stall, (d) => d.message || d.via || "tripped", true);
  flag("flag-seam", s.seam, (d) => `${d.key || d.stage || ""} ${d.status || ""}`.trim());
  flag("flag-anomaly", s.anomaly, (d) => d.message || d.reason || "—");
  flag("flag-checkin", s.check_in, (d) => d.message
    || (d.progress != null ? `${Math.round(d.progress * 100)}% (${d.patches_done || 0}/${d.patches_total || 0})` : "—"));
}

function flag(id, obj, fmt, bad) {
  const el = $(id);
  const span = el.querySelector("span");
  const has = obj && Object.keys(obj).length > 0;
  span.textContent = has ? fmt(obj) : "—";
  el.classList.toggle("hot", !!has);
  el.classList.toggle("bad", !!(has && bad));
}

/* ── per-event message formatting ────────────────────────────── */
function fmtMsg(ev) {
  const d = ev.data || {};
  const kv = (k, v) => `<span class="k">${esc(k)}</span>=<span class="v">${esc(v)}</span>`;
  switch (ev.event) {
    case "run_header":
      return [d.target && kv("target", d.target), d.mode && kv("mode", d.mode),
              d.flow && kv("flow", d.flow), d.ccmx && kv("ccmx", d.ccmx)].filter(Boolean).join(" ");
    case "phase": return `→ <span class="v">${esc(d.phase_name || "")}</span>`;
    case "stage_start": return "start";
    case "stage_done": return `done ${kv("status", d.status || "")}${d.replayed ? " (replayed)" : ""}`;
    case "stage_aborted": return `<span class="nok">aborted</span> ${esc(d.message || "")}`;
    case "patch_read": {
      const okc = d.ok ? '<span class="ok">ok</span>' : '<span class="nok">FAIL</span>';
      const rgb = d.rgb ? `[${d.rgb.join(",")}]` : "";
      const xy = d.xy ? `(${num(d.xy[0], 4)},${num(d.xy[1], 4)})` : "";
      const der = ev.derived || {};
      const cct = der.cct ? ` ${kv("cct", Math.round(der.cct) + "K")}` : "";
      return `${esc(d.role || "")} ${esc(d.label || "")} ${kv("rgb", rgb)} ${kv("Y", num(d.Y, 2))} ${kv("xy", xy)}${cct} ${okc}`;
    }
    case "progress": return `${kv("patches", (d.patches_done || 0) + "/" + (d.patches_total || 0))} ${kv("reads", d.reads || 0)}`;
    case "heartbeat": return `alive ${kv("elapsed", num(d.elapsed_s, 0) + "s")} ${kv("age", num(d.since_progress_s, 0) + "s")}`;
    case "optimizer_iteration":
      return Object.entries(d).slice(0, 5).map(([k, v]) =>
        kv(k, typeof v === "number" ? num(v, 3) : v)).join(" ");
    case "seam": return `${kv("key", d.key || "")} ${kv("status", d.status || "")}`;
    case "anomaly": return `<span class="v">${esc(d.message || d.reason || "")}</span>`;
    case "check_in":
      return d.message ? `<span class="v">${esc(d.message)}</span>`
        : `${kv("progress", d.progress != null ? Math.round(d.progress * 100) + "%" : "")} `
          + `${kv("patches", (d.patches_done || 0) + "/" + (d.patches_total || 0))}`;
    case "stall": return `<span class="nok">STALL</span> ${esc(d.message || "")} ${kv("via", d.via || "")}`;
    case "metrics_scored":
      return `${kv("avg", num(d.avg_de2000))} ${kv("p95", num(d.p95_de2000))} ${kv("max", num(d.max_de2000))} ${kv("white", num(d.white_de2000))}`;
    case "run_done": return `${kv("status", d.status || "")} ${esc(d.message || "")}`;
    case "note": return `<span class="v">${esc(d.message || "")}</span>`;
    case "run_created": return "run created";
    default: {
      const parts = Object.entries(d).slice(0, 6).map(([k, v]) =>
        kv(k, typeof v === "object" ? JSON.stringify(v) : v));
      return parts.join(" ");
    }
  }
}

/* ── log rendering (filtered + capped) ───────────────────────── */
function passesFilter(ev) {
  const minLvl = LEVEL_RANK[$("filter-level").value] ?? 1;
  if ((LEVEL_RANK[ev.level] ?? 1) < minLvl) return false;
  const stage = $("filter-stage").value;
  if (stage && ev.stage !== stage) return false;
  if ($("filter-digest").checked && ev.tier !== "digest") return false;
  const text = $("filter-text").value.trim().toLowerCase();
  if (text) {
    const hay = `${ev.event} ${ev.stage} ${ev.phase || ""} ${JSON.stringify(ev.data || {})}`.toLowerCase();
    if (!hay.includes(text)) return false;
  }
  return true;
}

function renderLog() {
  renderQueued = false;
  const box = $("log-rows");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const follow = $("autoscroll").checked;

  const matched = [];
  for (let i = logData.length - 1; i >= 0 && matched.length < RENDER_CAP; i--) {
    if (passesFilter(logData[i])) matched.push(logData[i]);
  }
  matched.reverse();

  let html = "";
  for (const ev of matched) {
    html += `<div class="row ev-${esc(ev.event)}">`
      + `<span class="t">${esc(clockTime(ev.time))}</span>`
      + `<span class="lvl ${esc(ev.level)}">${esc(ev.level)}</span>`
      + `<span class="stg">${esc(ev.stage || "")}</span>`
      + `<span class="msg">${fmtMsg(ev)}</span>`
      + `</div>`;
  }
  box.innerHTML = html;
  $("log-count").textContent = `showing ${matched.length} of ${logData.length}`;
  if (follow && (nearBottom || matched.length <= RENDER_CAP)) box.scrollTop = box.scrollHeight;
}

function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(renderLog);
}

function pushEvent(ev) {
  logData.push(ev);
  if (logData.length > MAX_LOG) logData = logData.slice(logData.length - MAX_LOG);
  if (ev.stage && !knownStages.has(ev.stage)) {
    knownStages.add(ev.stage);
    const opt = document.createElement("option");
    opt.value = ev.stage; opt.textContent = ev.stage;
    $("filter-stage").appendChild(opt);
  }
}

function seedLog(events) {
  logData = [];
  knownStages = new Set();
  const sel = $("filter-stage");
  sel.length = 1; // keep "all stages"
  for (const ev of events) pushEvent(ev);
  scheduleRender();
}

/* ── SSE wiring ──────────────────────────────────────────────── */
function connect() {
  const es = new EventSource("/events");
  es.addEventListener("state", (e) => { renderState(JSON.parse(e.data)); });
  es.addEventListener("backlog", (e) => { seedLog(JSON.parse(e.data).events || []); });
  es.addEventListener("append", (e) => {
    const ev = JSON.parse(e.data);
    pushEvent(ev);
    scheduleRender();
  });
  es.addEventListener("reset", (e) => {
    seedLog([]);
    lastCharts = null;
    lightboxKey = null;
    lightboxReturnFocus = null;
    $("charts-stage").textContent = "—";
    $("charts").innerHTML = "";
    $("lb-body").innerHTML = "";
    $("lightbox").hidden = true;
    renderState(JSON.parse(e.data));
    refreshCharts();
    toast("new run — dashboard reset");
  });
  es.onerror = () => {
    $("live-text").textContent = "reconnecting…";
    $("live-dot").className = "dot unknown";
    // EventSource auto-reconnects; nothing else to do.
  };
}

/* ── charts (polled off the SSE path; the server precomputes everything) ──── */
let chartsBusy = false;
let lastCharts = null;
let lightboxKey = null;
let lightboxReturnFocus = null;
async function refreshCharts() {
  if (chartsBusy || document.hidden) return;   // skip while a fetch is in flight or tab hidden
  chartsBusy = true;
  try {
    const r = await fetch("/api/charts");
    lastCharts = await r.json();
    const header = lastState ? lastState.header : null;
    // which measurement stage the snapshot charts reflect (raw → post-mhc → verify), so the
    // single-stage scatter is never mistaken for "all stages" — the charts show the latest.
    $("charts-stage").textContent = lastCharts && lastCharts.stage ? lastCharts.stage : "—";
    if (window.DLCCharts) {
      DLCCharts.renderInto($("charts"), lastCharts, header);
      if (lightboxKey) $("lb-body").innerHTML = DLCCharts.build(lightboxKey, lastCharts, header);  // keep the open tile live
    }
  } catch (e) { /* charts are advisory; ignore a missed poll */ }
  finally { chartsBusy = false; }
}

/* ── lightbox (image-viewer-style full view of a tile) ──────────── */
function chartParts(fig) {
  if (!fig) return null;
  const holder = fig.querySelector("[data-chart]");
  if (!holder) return null;
  const cap = fig.querySelector("figcaption");
  return {
    key: holder.getAttribute("data-chart"),
    title: cap ? cap.textContent : "",
  };
}

function openLightbox(key, title, opener) {
  lightboxKey = key;
  lightboxReturnFocus = opener || document.activeElement;
  $("lb-title").textContent = title || key;
  const header = lastState ? lastState.header : null;
  $("lb-body").innerHTML = (window.DLCCharts && lastCharts) ? DLCCharts.build(key, lastCharts, header) : "";
  $("lightbox").hidden = false;
  $("lb-frame").focus();
}
function closeLightbox() {
  const wasOpen = !$("lightbox").hidden;
  lightboxKey = null;
  $("lightbox").hidden = true;
  if (wasOpen && lightboxReturnFocus && document.contains(lightboxReturnFocus)) lightboxReturnFocus.focus();
  lightboxReturnFocus = null;
}

function trapLightboxTab(e) {
  if ($("lightbox").hidden || e.key !== "Tab") return;
  const focusable = Array.from($("lightbox").querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
  )).filter((el) => !el.disabled && el.offsetParent !== null);
  if (focusable.length === 0) {
    e.preventDefault();
    $("lb-frame").focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function wireLightbox() {
  $("charts").addEventListener("click", (e) => {
    const parts = chartParts(e.target.closest(".chart"));
    if (parts) openLightbox(parts.key, parts.title, e.target.closest(".chart"));
  });
  $("charts").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const fig = e.target.closest(".chart");
    const parts = chartParts(fig);
    if (!parts) return;
    e.preventDefault();
    openLightbox(parts.key, parts.title, fig);
  });
  $("lb-close").addEventListener("click", closeLightbox);
  $("lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") closeLightbox(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("lightbox").hidden) closeLightbox();
    trapLightboxTab(e);
  });
}

/* ── controls + filters ──────────────────────────────────────── */
function wireUi() {
  for (const id of ["filter-level", "filter-stage", "filter-digest", "filter-text"]) {
    $(id).addEventListener("input", scheduleRender);
  }
  $("btn-export").addEventListener("click", async () => {
    $("btn-export").disabled = true;
    try {
      const j = await postJson("/api/export");
      toast(j.saved_to ? `report → ${j.saved_to.split(/[\\/]/).pop()}` : "report exported");
    } catch (e) {
      toast("export failed");
    } finally {
      $("btn-export").disabled = false;
    }
  });
  // Cancel: drop control.json into the run; the live process rolls back at its next checkpoint.
  $("btn-cancel").addEventListener("click", async () => {
    if (!confirm("Cancel this run? DesktopLUT rolls back to your pre-run setup at the next checkpoint.")) return;
    $("btn-cancel").disabled = true;
    try {
      const j = await postJson("/api/cancel");
      toast(j.ok ? "cancel requested — rolling back at the next checkpoint" : ("cancel failed: " + (j.error || "")));
    } catch (e) {
      toast("cancel failed");
    } finally {
      $("btn-cancel").disabled = false;
    }
  });
}

wireUi();
wireLightbox();
connect();
refreshCharts();
setInterval(refreshCharts, 4000);   // relaxed cadence — charts don't need 2 s latency
