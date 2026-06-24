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
// Every metric rides the 1≈JND scale (ΔEz is JND-normalised server-side), so one learned band set
// applies to all: <1 imperceptible (green), <3 acceptable (yellow), ≥3 visible (red) — the common
// calibration convention, so the user's intuition transfers unchanged across metrics.
function deClass(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  const a = Math.abs(v);
  return a < 1.0 ? "de-ok" : (a < 3.0 ? "de-warn" : "de-bad");
}
function metricDecimals() { return 2; }   // all metrics share the JND scale → 2 dp everywhere
// setDe: a JND-scale ΔE cell (used by both the scoring block and the selectable live/last-patch
// readouts — they all read on the same scale now). The metric arg is accepted for call-site clarity.
function setDe(id, v) {
  const el = $(id);
  el.textContent = num(v, 2);
  el.className = deClass(v);
}
function setDeM(id, metric, v) { setDe(id, v); }
function deMetricLabel(metric) {
  if (!metric) return "ΔE";
  const m = String(metric).toLowerCase();
  if (m.includes("itp")) return "ΔE·ITP";              // HDR (BT.2124)
  if (m.includes("jz")) return "ΔEz";                  // Jzazbz (raw, native scale)
  if (m.includes("2000") || m === "de2000") return "ΔE2000";  // CIEDE2000
  return "ΔE";
}
// Component labels for the lightness/chroma/hue split — ITP is I/C/H, Lab is L*/C*/H*, Jzazbz Jz/C/H.
function compLabels(metric) {
  if (metric === "itp") return ["ΔI", "ΔC", "ΔH"];
  if (metric === "jzazbz") return ["ΔJz", "ΔC", "ΔH"];
  return ["ΔL*", "ΔC*", "ΔH*"];
}
function fmtSigned(metric, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return (v >= 0 ? "+" : "") + Number(v).toFixed(metricDecimals(metric));
}
function fmtSignedFixed(v, d) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return (v >= 0 ? "+" : "") + Number(v).toFixed(d);
}
// Luminance/nits: integers once we're past ~100 (HDR runs to thousands), 2 dp for dim patches.
function fmtY(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(Math.abs(v) >= 100 ? 0 : 2);
}

/* ── selectable view metric (a presentation lens; the scored block stays in the scoring metric) ── */
const DE_VIEW_KEY = "dlc.deView";
let deView = (() => { try { return localStorage.getItem(DE_VIEW_KEY) || null; } catch (e) { return null; } })();
let deViewSig = "";   // signature of the currently-populated <select> options

function availMetrics(s) {
  const ld = s && s.live_de;
  return (ld && ld.metrics) ? Object.keys(ld.metrics) : [];
}
function scoringMetric(s) {
  return (s && s.live_de && s.live_de.scoring) || (s && s.de && s.de.metric) || "de2000";
}
// The metric actually shown: the user's pick if it's available this run, else the scoring metric.
function viewMetric(s) {
  const avail = availMetrics(s);
  if (deView && avail.includes(deView)) return deView;
  const sc = scoringMetric(s);
  return avail.includes(sc) ? sc : (avail[0] || sc);
}
function syncDeViewSelect(s) {
  const sel = $("de-view");
  if (!sel) return;
  const avail = availMetrics(s);
  const sig = avail.join(",");
  if (sig !== deViewSig) {                       // re-populate only when the option set changes
    deViewSig = sig;
    sel.innerHTML = avail.map((m) => `<option value="${m}">${deMetricLabel(m)}</option>`).join("");
  }
  sel.value = viewMetric(s);
  sel.disabled = avail.length <= 1;
}
// A patch's approximate on-screen colour from its normalised signal (sRGB-ish), for the swatch.
function sigHex(sig) {
  if (!Array.isArray(sig) || sig.length < 3) return "#1c1c20";
  const c = (v) => Math.max(0, Math.min(255, Math.round(Number(v) * 255)));
  return `#${[c(sig[0]), c(sig[1]), c(sig[2])].map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}
// Duv sign reads as a tint: positive = green above the Planckian locus, negative = magenta below.
function duvTint(duv) {
  if (duv == null) return "";
  if (duv > 0.0002) return "green";
  if (duv < -0.0002) return "magenta";
  return "";
}
function renderLivePatch(lr, header) {
  const ok = lr.ok && lr.xy;
  const role = lr.role || (lr.label ? "" : "—");
  $("lp-role").textContent = [role, lr.disposition && lr.disposition !== role ? lr.disposition : ""]
    .filter(Boolean).join(" · ") || "—";
  $("lp-label").textContent = lr.label || "—";
  const rgb = lr.rgb ? `[${lr.rgb.join(",")}]` : "—";
  $("lp-rgb").textContent = rgb;
  const sw = $("lp-swatch");
  sw.style.background = (lr.signal ? sigHex(lr.signal) : (lr.rgb && header && header.bit_depth
    ? sigHex(lr.rgb.map((v) => v / (Math.pow(2, header.bit_depth) - 1))) : "#1c1c20"));
  sw.classList.toggle("nok", !ok);
  // Measured vs target — chromaticity (x,y) + luminance/nits (Y), the layout calibration tools use.
  const tgt = lr.deltas && lr.deltas.target;
  const mx = ok ? lr.xy[0] : null, my = ok ? lr.xy[1] : null, mY = lr.Y;
  $("lp-mx").textContent = (mx != null) ? num(mx, 4) : "—";
  $("lp-my").textContent = (my != null) ? num(my, 4) : "—";
  $("lp-mY").textContent = (mY != null) ? fmtY(mY) : "—";
  $("lp-tx").textContent = tgt ? num(tgt.x, 4) : "—";
  $("lp-ty").textContent = tgt ? num(tgt.y, 4) : "—";
  $("lp-tY").textContent = tgt ? fmtY(tgt.Y) : "—";
  $("lp-dx").textContent = (tgt && mx != null) ? fmtSignedFixed(mx - tgt.x, 4) : "—";
  $("lp-dy").textContent = (tgt && my != null) ? fmtSignedFixed(my - tgt.y, 4) : "—";
  // ΔY as a percentage of target (the intuitive "how far off in brightness"); guard tiny targets.
  $("lp-dY").textContent = (tgt && mY != null && tgt.Y > 1e-3)
    ? fmtSignedFixed((mY - tgt.Y) / tgt.Y * 100, 1) + "%" : "—";
  // per-patch ΔE vs target (all patches), in the SELECTED view metric, coloured by quality.
  const vm = viewMetric(lastState);
  const comp = lr.deltas && lr.deltas.metrics && lr.deltas.metrics[vm];
  $("lp-de-label").textContent = deMetricLabel(vm);
  setDeM("lp-de", vm, comp ? comp.de : (vm === scoringMetric(lastState) ? lr.de : null));
  // divided deltas: the lightness/chroma/hue split, dominant axis highlighted — so a big ΔE
  // reads as "mostly a luminance miss" vs "a real hue error" at a glance.
  const labs = compLabels(vm);
  $("lp-cl-lab").textContent = labs[0];
  $("lp-cc-lab").textContent = labs[1];
  $("lp-ch-lab").textContent = labs[2];
  const cells = [["lp-cl", "L"], ["lp-cc", "C"], ["lp-ch", "H"]];
  let domI = -1, domV = -1;
  if (comp) cells.forEach(([, k], i) => { const a = Math.abs(comp[k]); if (a > domV) { domV = a; domI = i; } });
  cells.forEach(([id, k], i) => {
    const el = $(id);
    el.textContent = comp ? fmtSigned(vm, comp[k]) : "—";
    el.classList.toggle("dom", !!comp && i === domI);
  });
  // CCT / Duv only make sense for a grayscale patch (a saturated colour has no meaningful CCT).
  const neutral = !!lr.neutral;
  document.querySelectorAll(".rcard-patch .lp-neutral").forEach((el) => el.classList.toggle("off", !neutral));
  if (neutral && ok) {
    $("lp-cct").textContent = lr.cct ? `${Math.round(lr.cct)} K` : "—";
    const tint = duvTint(lr.duv);
    $("lp-duv").innerHTML = (lr.duv != null)
      ? `${num(lr.duv, 4)}${tint ? ` <span class="tint ${tint}">${tint === "green" ? "▲green" : "▼magenta"}</span>` : ""}` : "—";
    const tcct = header && header.white && header.white.cct;
    $("lp-target").textContent = (lr.cct && tcct) ? `${(lr.cct - tcct >= 0 ? "+" : "")}${Math.round(lr.cct - tcct)} K` : "—";
  }
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
  $("btn-pause").textContent = light === "paused" ? "Resume" : "Pause";
  $("btn-pause").title = light === "paused"
    ? "Resume the paused run"
    : "Pause briefly; rolls back automatically after about 3 minutes";

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

  // dE card. The selectable VIEW metric (dropdown) drives the headline label + the live rolling
  // readout + the last-patch tile. The big "scored" numbers come from the scoring stage and stay
  // in the metric the spine actually optimised (dE_ITP for HDR, CIEDE2000 for SDR) — a viewing
  // lens must never be mistaken for a re-score.
  const de = s.de || {};
  const ld = s.live_de || {};
  syncDeViewSelect(s);
  const vm = viewMetric(s);
  $("de-metric").textContent = deMetricLabel(vm);
  $("de-scored-metric").textContent = deMetricLabel(de.metric || scoringMetric(s));
  $("de-source").textContent = de.phase ? `${de.phase}${de.iteration != null ? " #" + de.iteration : ""}` : "";
  // live rolling per-patch ΔE for the CURRENT stage, in the selected view metric — split
  // in-gamut (the quality that matters) vs out-of-gamut (expected clipping, shown muted).
  const lvm = (ld.metrics && ld.metrics[vm]) || {};
  $("de-live-cap").textContent = "live · " + (s.stage || s.phase || "—");
  const ing = lvm.in || {}, oog = lvm.oog || {};
  if (ld.gamut_known) {
    $("de-in-lab").textContent = "in-gamut";
    setDeM("de-in-avg", vm, ing.avg); setDeM("de-in-max", vm, ing.max);
    $("de-oog-lab").textContent = "out-gamut";
    $("de-oog-lab").classList.remove("pending");
    $("de-oog-avg").textContent = num(oog.avg, 2);
    $("de-oog-max").textContent = num(oog.max, 2);
    const ot = oog.n ? `${oog.n} unreachable patch${oog.n === 1 ? "" : "es"} — large ΔE is expected (clips), not a miss` : "no out-of-gamut patches this stage";
    $("de-oog-avg").title = $("de-oog-max").title = ot;
  } else {
    // native gamut not measured yet → show the combined avg/max, mark the split pending
    $("de-in-lab").textContent = "all";
    setDeM("de-in-avg", vm, lvm.avg); setDeM("de-in-max", vm, lvm.max);
    $("de-oog-lab").textContent = "gamut pending";
    $("de-oog-lab").classList.add("pending");
    $("de-oog-avg").textContent = "—"; $("de-oog-max").textContent = "—";
    $("de-oog-avg").title = $("de-oog-max").title = "the in/out-of-gamut split needs the panel's native primaries (read from the raw saturated patches)";
  }
  setDeM("de-live-last", vm, lvm.last);
  $("de-live-n").textContent = ld.n ? `${ld.n} patch${ld.n === 1 ? "" : "es"}` : "";
  setDe("de-avg", de.avg); setDe("de-p95", de.p95); setDe("de-p99", de.p99); setDe("de-max", de.max);
  setDe("de-white", de.white);
  setDe("de-gray", de.grayscale); setDe("de-colour", de.colour);

  // live patch — the last measured patch + a swatch; CCT/Duv only when it's a grayscale patch.
  renderLivePatch(s.last_read || {}, h);

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
      // CCT/Duv only for a grayscale patch — a saturated colour has no meaningful correlated
      // colour temperature, so showing one there is misleading.
      const neutral = Array.isArray(d.rgb) && d.rgb.length >= 3
        && d.rgb[0] === d.rgb[1] && d.rgb[1] === d.rgb[2] && d.rgb[0] > 0;
      const cct = (neutral && der.cct) ? ` ${kv("cct", Math.round(der.cct) + "K")}` : "";
      const de = (der.de != null) ? ` ${kv("ΔE", num(der.de, 2))}` : "";
      return `${esc(d.role || "")} ${esc(d.label || "")} ${kv("rgb", rgb)} ${kv("Y", num(d.Y, 2))} ${kv("xy", xy)}${cct}${de} ${okc}`;
    }
    case "progress": return `${kv("patches", (d.patches_done || 0) + "/" + (d.patches_total || 0))} ${kv("reads", d.reads || 0)}`;
    case "heartbeat": return `alive ${kv("elapsed", num(d.elapsed_s, 0) + "s")} ${kv("age", num(d.since_progress_s, 0) + "s")}`;
    case "optimizer_iteration":
      return Object.entries(d).slice(0, 5).map(([k, v]) =>
        kv(k, typeof v === "number" ? num(v, 3) : v)).join(" ");
    case "seam": return `${kv("key", d.key || "")} ${kv("status", d.status || "")}`;
    case "anomaly": return `<span class="v">${esc(d.message || d.reason || "")}</span>`;
    case "check_in": {
      if (d.message) return `<span class="v">${esc(d.message)}</span>`;
      const sl = d.since_last || {};
      const since = [];
      if (sl.reads) since.push(sl.reads + " reads");
      if (sl.anomalies) since.push(sl.anomalies + " new anomalies");
      if (sl.drift_episodes) since.push(sl.drift_episodes + " drift");
      return `${kv("progress", d.progress != null ? Math.round(d.progress * 100) + "%" : "")} `
        + `${kv("patches", (d.patches_done || 0) + "/" + (d.patches_total || 0))}`
        + (since.length ? ` ${kv("since last", since.join(", "))}` : "");
    }
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
  if (chartsBusy) return;                       // a fetch is already in flight
  // Skip the PERIODIC poll while the tab is backgrounded (saves the fetch) — but always do the
  // first render, and the visibilitychange handler re-renders on return, so a dashboard opened
  // in a hidden/background tab is never stuck chart-blank.
  if (document.hidden && lastCharts) return;
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
  clearHover();
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

/* ── chart hover: snap to the nearest data point and show its readout instantly ─────
 * Works on the grid tiles AND the expanded lightbox. Presentation-only: the cursor is
 * mapped into the hovered chart's viewBox and we pick the nearest titled <circle>/<rect>
 * (geometry in SVG-pixel space — NO colour math), so you get a point's target/measured/
 * deviation immediately and don't have to land on a 2px dot. */
let hoverEl = null;                                    // the currently-highlighted data mark
let hoverVec = null;                                   // the measured→target error vector (CIE tile)
const SVGNS = "http://www.w3.org/2000/svg";

// CIE points carry data-tx/data-ty (their target in viewBox px). On hover, draw the error vector
// from the measured dot to where the patch SHOULD sit, with a ring at the target — so the
// chromaticity miss is visible as a line, not just a number.
function drawErrorVector(el) {
  removeErrorVector();
  const tx = el.getAttribute && el.getAttribute("data-tx");
  const ty = el.getAttribute && el.getAttribute("data-ty");
  if (tx == null || ty == null) return;
  const svg = el.closest("svg");
  if (!svg) return;
  const cx = el.getAttribute("cx"), cy = el.getAttribute("cy");
  const g = document.createElementNS(SVGNS, "g");
  g.setAttribute("class", "ch-error-grp");
  const line = document.createElementNS(SVGNS, "line");
  line.setAttribute("x1", cx); line.setAttribute("y1", cy);
  line.setAttribute("x2", tx); line.setAttribute("y2", ty);
  line.setAttribute("class", "ch-error-line");
  const ring = document.createElementNS(SVGNS, "circle");
  ring.setAttribute("cx", tx); ring.setAttribute("cy", ty); ring.setAttribute("r", "2.6");
  ring.setAttribute("class", "ch-error-target");
  g.appendChild(line); g.appendChild(ring);
  svg.appendChild(g);                                  // appended last → drawn over the scatter
  hoverVec = g;
}
function removeErrorVector() {
  // remove ALL vectors (the grid tile and the lightbox each have their own SVG, so a vector can
  // linger in the other when the cursor crosses between them) — robust against strays.
  document.querySelectorAll(".ch-error-grp").forEach((g) => g.remove());
  hoverVec = null;
}

function clearHover() {
  if (hoverEl) { hoverEl.classList.remove("ch-hover"); hoverEl = null; }
  removeErrorVector();
  const tip = $("chart-tip");
  if (tip) tip.hidden = true;
}

// Distance (in viewBox units) from a point to a data mark: centre distance for a circle,
// 0-inside-else-edge distance for a bar rect. Infinity for anything we can't place.
function markDist(el, lx, ly) {
  const tag = el.tagName.toLowerCase();
  if (tag === "circle") {
    const cx = parseFloat(el.getAttribute("cx")), cy = parseFloat(el.getAttribute("cy"));
    return (Number.isNaN(cx) || Number.isNaN(cy)) ? Infinity : Math.hypot(cx - lx, cy - ly);
  }
  if (tag === "rect") {
    const x = parseFloat(el.getAttribute("x")), y = parseFloat(el.getAttribute("y"));
    const w = parseFloat(el.getAttribute("width")), h = parseFloat(el.getAttribute("height"));
    if ([x, y, w, h].some(Number.isNaN)) return Infinity;
    return Math.hypot(Math.max(x - lx, 0, lx - (x + w)), Math.max(y - ly, 0, ly - (y + h)));
  }
  return Infinity;
}

function nearestMark(svg, clientX, clientY) {
  let ctm;
  try { ctm = svg.getScreenCTM(); } catch (e) { return null; }
  if (!ctm) return null;
  const pt = svg.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  const loc = pt.matrixTransform(ctm.inverse());
  let best = null, bestD = Infinity;
  svg.querySelectorAll("circle, rect").forEach((el) => {
    const t = el.getElementsByTagName("title")[0];     // only data marks carry a <title>
    if (!t || t.parentNode !== el) return;
    const d = markDist(el, loc.x, loc.y);
    if (d < bestD) { bestD = d; best = { el, title: t.textContent }; }
  });
  return best && bestD <= 22 ? best : null;             // ~22 viewBox units (charts are 400×300)
}

function wireChartHover(container) {
  if (!container) return;
  container.addEventListener("mousemove", (e) => {
    const svg = e.target.closest && e.target.closest("svg");
    const hit = svg ? nearestMark(svg, e.clientX, e.clientY) : null;
    if (!hit) { clearHover(); return; }
    if (hit.el !== hoverEl) {
      if (hoverEl) hoverEl.classList.remove("ch-hover");
      hit.el.classList.add("ch-hover");
      hoverEl = hit.el;
      drawErrorVector(hit.el);                          // error vector to target (no-op off the CIE tile)
    }
    const tip = $("chart-tip");
    tip.textContent = hit.title;
    tip.hidden = false;
    const pad = 14, tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;       // fixed-positioned, clamped to viewport
    if (x + tw > window.innerWidth) x = e.clientX - tw - pad;
    if (y + th > window.innerHeight) y = e.clientY - th - pad;
    tip.style.left = Math.max(2, x) + "px";
    tip.style.top = Math.max(2, y) + "px";
  });
  container.addEventListener("mouseleave", clearHover);
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
  // view-metric selector: remember the choice and repaint the dE card + last patch from the
  // cached state (no server round-trip — every metric is already on the wire per patch).
  $("de-view").addEventListener("change", (e) => {
    deView = e.target.value;
    try { localStorage.setItem(DE_VIEW_KEY, deView); } catch (err) { /* private mode: in-memory only */ }
    if (lastState) renderState(lastState);
  });
  // native-gamut overlay toggle: flip the chart option and re-render the (cached) charts
  $("toggle-native").addEventListener("change", (e) => {
    if (window.DLCCharts) DLCCharts.opts.native = e.target.checked;
    const header = lastState ? lastState.header : null;
    if (window.DLCCharts && lastCharts) {
      DLCCharts.renderInto($("charts"), lastCharts, header);
      if (lightboxKey) $("lb-body").innerHTML = DLCCharts.build(lightboxKey, lastCharts, header);
    }
  });
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
  $("btn-pause").addEventListener("click", async () => {
    const paused = lastState && lastState.liveness && lastState.liveness.light === "paused";
    if (!paused && !confirm("Pause briefly? The run parks neutral and rolls back if not resumed within about 3 minutes.")) return;
    $("btn-pause").disabled = true;
    try {
      const j = await postJson(paused ? "/api/resume" : "/api/pause");
      if (j.ok) toast(paused ? "resume requested" : "pause requested — rollback timer started");
      else toast((paused ? "resume" : "pause") + " failed: " + (j.error || ""));
    } catch (e) {
      toast(paused ? "resume failed" : "pause failed");
    } finally {
      $("btn-pause").disabled = false;
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
wireChartHover($("charts"));     // hover readout on the grid tiles
wireChartHover($("lb-body"));    // …and on the expanded lightbox tile
connect();
refreshCharts();
setInterval(refreshCharts, 4000);   // relaxed cadence — charts don't need 2 s latency
// Returning to a backgrounded tab: refresh immediately rather than waiting up to 4 s (and this
// is what fills the charts if the dashboard was first opened while hidden).
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshCharts(); });
