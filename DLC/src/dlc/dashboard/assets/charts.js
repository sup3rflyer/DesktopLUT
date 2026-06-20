/* DLCCharts — zero-dependency SVG chart builders (HCFR-style, grayscale theme).
 *
 * Each builder takes server-precomputed data (all numbers; the dashboard does no colour
 * math) and returns an SVG string at a SHARED viewBox so every tile is the same size. The
 * same file renders the live dashboard and the exported standalone report. */
"use strict";
(function (global) {
  const VB = { w: 400, h: 300 };   // shared viewBox → equal tiles
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (n, d) => (n == null || Number.isNaN(Number(n))) ? "" : Number(n).toFixed(d == null ? 2 : d);
  const niceTicks = (lo, hi, n) => { const o = []; for (let i = 0; i <= n; i++) o.push(lo + (hi - lo) * i / n); return o; };
  const title = (s) => `<title>${esc(s)}</title>`;

  function Plot(o) {
    const W = o.w || VB.w, H = o.h || VB.h;
    const m = { l: o.ml == null ? 48 : o.ml, r: o.mr == null ? 14 : o.mr,
                t: o.mt == null ? 14 : o.mt, b: o.mb == null ? 30 : o.mb };
    const xr = (o.xmax - o.xmin) || 1, yr = (o.ymax - o.ymin) || 1;
    const px = (x) => m.l + (x - o.xmin) / xr * (W - m.l - m.r);
    const py = (y) => H - m.b - (y - o.ymin) / yr * (H - m.t - m.b);
    const parts = [`<rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" class="ch-area"/>`];
    return {
      W, H, m, px, py,
      add: (s) => parts.push(s),
      gridX(ticks, lab) {
        ticks.forEach((t) => {
          const X = px(t);
          parts.push(`<line x1="${X}" y1="${m.t}" x2="${X}" y2="${H - m.b}" class="ch-grid"/>`);
          parts.push(`<text x="${X}" y="${H - m.b + 14}" class="ch-tick" text-anchor="middle">${esc(lab ? lab(t) : t)}</text>`);
        });
      },
      gridY(ticks, lab) {
        ticks.forEach((t) => {
          const Y = py(t);
          parts.push(`<line x1="${m.l}" y1="${Y}" x2="${W - m.r}" y2="${Y}" class="ch-grid"/>`);
          parts.push(`<text x="${m.l - 6}" y="${Y + 3}" class="ch-tick" text-anchor="end">${esc(lab ? lab(t) : t)}</text>`);
        });
      },
      pathLine(points, cls) {
        if (!points || !points.length) return;
        const d = points.map((p, i) => (i ? "L" : "M") + fmt(px(p[0]), 1) + " " + fmt(py(p[1]), 1)).join(" ");
        parts.push(`<path d="${d}" class="${cls}" fill="none"/>`);
      },
      svg() { return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">${parts.join("")}</svg>`; },
    };
  }
  const empty = (msg) =>
    `<svg viewBox="0 0 ${VB.w} ${VB.h}" class="chart-svg"><text x="${VB.w / 2}" y="${VB.h / 2}" text-anchor="middle" class="ch-empty">${esc(msg || "no data yet")}</text></svg>`;

  const DLCCharts = {};

  // ── CIE 1931: measured scatter (coloured by patch) vs sRGB gamut + locus + white ──
  DLCCharts.cie = function (d) {
    d = d || {};
    const P = Plot({ xmin: 0, xmax: 0.75, ymin: 0, ymax: 0.85 });
    P.gridX([0, 0.2, 0.4, 0.6], (t) => fmt(t, 1));
    P.gridY([0, 0.2, 0.4, 0.6, 0.8], (t) => fmt(t, 1));
    if (d.locus && d.locus.length) P.pathLine(d.locus, "ch-locus");
    const pr = d.primaries || {};
    if (pr.r && pr.g && pr.b) {
      const poly = [pr.r, pr.g, pr.b].map((p) => `${fmt(P.px(p[0]), 1)},${fmt(P.py(p[1]), 1)}`).join(" ");
      P.add(`<polygon points="${poly}" class="ch-gamut">${title("target gamut: " + (d.gamut_label || "Rec.709 / sRGB"))}</polygon>`);
      [["R", pr.r], ["G", pr.g], ["B", pr.b]].forEach(([lab, p]) => {
        P.add(`<text x="${fmt(P.px(p[0]), 1)}" y="${fmt(P.py(p[1]) - 5, 1)}" class="ch-note" text-anchor="middle">${lab}</text>`);
      });
    }
    const pts = d.points || [];
    const cap = 2500, step = pts.length > cap ? Math.ceil(pts.length / cap) : 1;
    for (let i = 0; i < pts.length; i += step) {
      const p = pts[i];
      // inline style beats the CSS class fill, so a per-patch colour actually shows
      const fill = p.c ? ` style="fill:${esc(p.c)}"` : "";
      P.add(`<circle cx="${fmt(P.px(p.x), 1)}" cy="${fmt(P.py(p.y), 1)}" r="1.7" class="${p.neutral ? "ch-pt-n" : "ch-pt"}"${fill}>${title(`${p.neutral ? "neutral" : "colour"} xy ${fmt(p.x, 4)}, ${fmt(p.y, 4)}`)}</circle>`);
    }
    if (d.white && d.white.length >= 2) {
      P.add(`<circle cx="${fmt(P.px(d.white[0]), 1)}" cy="${fmt(P.py(d.white[1]), 1)}" r="4.5" class="ch-white">${title(`target white xy ${fmt(d.white[0], 4)}, ${fmt(d.white[1], 4)}`)}</circle>`);
      P.add(`<text x="${fmt(P.px(d.white[0]) + 7, 1)}" y="${fmt(P.py(d.white[1]) - 7, 1)}" class="ch-note">white</text>`);
    }
    return P.svg();
  };

  // ── Tone response (EOTF): measured luminance vs signal, against target gamma ──
  DLCCharts.eotf = function (d) {
    d = d || {};
    const pts = (d.points || []).filter((p) => p.Y != null).sort((a, b) => a.signal - b.signal);
    if (!pts.length) return empty("no grayscale reads yet");
    const ymax = Math.max(...pts.map((p) => p.Y)) || 1;
    const P = Plot({ xmin: 0, xmax: 1, ymin: 0, ymax: 1 });
    P.gridX([0, 0.25, 0.5, 0.75, 1], (t) => fmt(t, 2));
    P.gridY([0, 0.25, 0.5, 0.75, 1], (t) => fmt(t, 2));
    const g = d.gamma || 2.2;
    const ref = d.reference && d.reference.length ? d.reference : niceTicks(0, 1, 40).map((s) => [s, Math.pow(s, g)]);
    P.pathLine(ref, "ch-ref");
    P.pathLine(pts.map((p) => [p.signal, p.Y / ymax]), "ch-line");
    const refAt = (s) => {
      if (!ref.length) return Math.pow(s, g);
      let best = ref[0];
      for (const r of ref) if (Math.abs(r[0] - s) < Math.abs(best[0] - s)) best = r;
      return best[1];
    };
    pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.Y / ymax), 1)}" r="2.2" class="ch-dot">${title(`signal ${fmt(p.signal, 3)} | measured ${fmt(p.Y / ymax, 4)} | target ${fmt(refAt(p.signal), 4)}`)}</circle>`));
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">${d.kind === "pq" ? "PQ" : "γ " + fmt(g, 2)}</text>`);
    return P.svg();
  };

  // ── Grayscale CCT vs signal (+ target white CCT reference) ──
  DLCCharts.grayscaleCct = function (d, targetCct) {
    const pts = (d || []).filter((p) => p.cct != null);
    if (!pts.length) return empty("no grayscale CCT yet");
    const ccts = pts.map((p) => p.cct).concat(targetCct ? [targetCct] : []);
    let lo = Math.min(...ccts), hi = Math.max(...ccts);
    if (hi - lo < 200) { lo -= 200; hi += 200; }
    const P = Plot({ xmin: 0, xmax: 1, ymin: lo, ymax: hi });
    P.gridX([0, 0.5, 1], (t) => fmt(t, 1));
    P.gridY(niceTicks(lo, hi, 4), (t) => Math.round(t));
    if (targetCct) {
      P.pathLine([[0, targetCct], [1, targetCct]], "ch-ref");
      P.add(`<text x="${P.W - 16}" y="${fmt(P.py(targetCct) - 5, 1)}" text-anchor="end" class="ch-note">target ${Math.round(targetCct)}K</text>`);
    }
    P.pathLine(pts.map((p) => [p.signal, p.cct]), "ch-line");
    pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.cct), 1)}" r="2.2" class="ch-dot">${title(`signal ${fmt(p.signal, 3)} | CCT ${Math.round(p.cct)}K`)}</circle>`));
    return P.svg();
  };

  // ── Grayscale Duv vs signal (zero = on the Planckian locus) ──
  DLCCharts.grayscaleDuv = function (d) {
    const pts = (d || []).filter((p) => p.duv != null);
    if (!pts.length) return empty("no grayscale Duv yet");
    const span = Math.max(0.005, Math.max(...pts.map((p) => Math.abs(p.duv))) * 1.2);
    const P = Plot({ xmin: 0, xmax: 1, ymin: -span, ymax: span });
    P.gridX([0, 0.5, 1], (t) => fmt(t, 1));
    P.gridY([-span, 0, span], (t) => fmt(t, 3));
    P.pathLine([[0, 0], [1, 0]], "ch-ref");
    P.pathLine(pts.map((p) => [p.signal, p.duv]), "ch-line");
    pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.duv), 1)}" r="2.2" class="ch-dot">${title(`signal ${fmt(p.signal, 3)} | Duv ${fmt(p.duv, 5)} | target 0`)}</circle>`));
    P.add(`<text x="${P.W - 16}" y="${fmt(P.py(0) - 5, 1)}" text-anchor="end" class="ch-note">target 0</text>`);
    return P.svg();
  };

  // ── Colour luminance: per-patch luminance error vs target, bars coloured by patch ──
  DLCCharts.colorLum = function (d) {
    const items = d || [];
    if (!items.length) return empty("no colour patches yet");
    const maxAbs = Math.max(0.12, ...items.map((i) => Math.abs(i.error)));
    const span = Math.min(0.5, maxAbs * 1.15);
    const P = Plot({ mb: 50, xmin: 0, xmax: items.length, ymin: -span, ymax: span });
    P.gridY(niceTicks(-span, span, 4), (t) => (t > 0 ? "+" : "") + Math.round(t * 100) + "%");
    [-0.1, 0.1].forEach((gv) => {
      if (Math.abs(gv) <= span)
        P.add(`<line x1="${P.px(0)}" y1="${fmt(P.py(gv), 1)}" x2="${P.px(items.length)}" y2="${fmt(P.py(gv), 1)}" class="ch-guide"/>`);
    });
    P.add(`<line x1="${P.px(0)}" y1="${fmt(P.py(0), 1)}" x2="${P.px(items.length)}" y2="${fmt(P.py(0), 1)}" class="ch-axis0"/>`);
    const bw = P.px(1) - P.px(0);
    const showLabels = items.length <= 48;
    items.forEach((it, i) => {
      const x0 = P.px(i) + bw * 0.15, w = Math.max(1, bw * 0.7);
      const yA = P.py(Math.max(0, it.error)), yB = P.py(Math.min(0, it.error));
      P.add(`<rect x="${fmt(x0, 1)}" y="${fmt(Math.min(yA, yB), 1)}" width="${fmt(w, 1)}" height="${fmt(Math.max(1, Math.abs(yB - yA)), 1)}" fill="${esc(it.color)}" stroke="#000" stroke-width=".3">${title(`${it.label}: ${(it.error >= 0 ? "+" : "")}${fmt(it.error * 100, 1)}% luminance error, ${it.n || 1} patch(es)`)}</rect>`);
      if (showLabels) {
        const lx = P.px(i) + bw / 2, ly = P.H - P.m.b + 11;
        P.add(`<text x="${fmt(lx, 1)}" y="${fmt(ly, 1)}" transform="rotate(-60 ${fmt(lx, 1)} ${fmt(ly, 1)})" text-anchor="end" class="ch-tick">${esc(it.label)}</text>`);
      }
    });
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">target 0% error</text>`);
    return P.svg();
  };

  // ── Optimizer convergence: max / mean dE per outer iteration ──
  DLCCharts.optimizer = function (d) {
    const its = d || [];
    if (!its.length) return empty("no optimizer iterations");
    const xs = its.map((r) => r.iteration);
    const maxde = Math.max(...its.map((r) => r.measured_max_de || 0), 1);
    const xmax = Math.max(...xs, Math.min(...xs) + 1);
    const P = Plot({ xmin: Math.min(...xs), xmax, ymin: 0, ymax: maxde * 1.1 });
    P.gridX(niceTicks(Math.min(...xs), xmax, Math.min(4, Math.max(1, its.length - 1))), (t) => Math.round(t));
    P.gridY(niceTicks(0, maxde * 1.1, 4), (t) => fmt(t, 2));
    P.pathLine(its.map((r) => [r.iteration, r.measured_max_de]), "ch-line-max");
    P.pathLine(its.map((r) => [r.iteration, r.measured_mean_de]), "ch-line");
    its.forEach((r) => {
      P.add(`<circle cx="${fmt(P.px(r.iteration), 1)}" cy="${fmt(P.py(r.measured_max_de), 1)}" r="2" class="ch-dot-max">${title(`iteration ${r.iteration}: max dE ${fmt(r.measured_max_de, 3)}, above target ${r.above_threshold || 0}`)}</circle>`);
      P.add(`<circle cx="${fmt(P.px(r.iteration), 1)}" cy="${fmt(P.py(r.measured_mean_de), 1)}" r="2" class="ch-dot">${title(`iteration ${r.iteration}: mean dE ${fmt(r.measured_mean_de, 3)}`)}</circle>`);
    });
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">max / mean ΔE</text>`);
    return P.svg();
  };

  // ── Drift: white-point CCT over elapsed time ──
  DLCCharts.drift = function (d) {
    const pts = (d || []).filter((p) => p.cct != null && p.elapsed_s != null);
    if (pts.length < 2) return empty("no drift series yet");
    const xs = pts.map((p) => p.elapsed_s), cs = pts.map((p) => p.cct);
    let lo = Math.min(...cs), hi = Math.max(...cs);
    if (hi - lo < 100) { lo -= 100; hi += 100; }
    const P = Plot({ xmin: 0, xmax: Math.max(...xs) || 1, ymin: lo, ymax: hi });
    P.gridX(niceTicks(0, Math.max(...xs) || 1, 4), (t) => Math.round(t) + "s");
    P.gridY(niceTicks(lo, hi, 4), (t) => Math.round(t));
    P.pathLine(pts.map((p) => [p.elapsed_s, p.cct]), "ch-line");
    pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.elapsed_s), 1)}" cy="${fmt(P.py(p.cct), 1)}" r="1.8" class="ch-dot">${title(`${Math.round(p.elapsed_s)}s | CCT ${Math.round(p.cct)}K${p.Y != null ? " | Y " + fmt(p.Y, 2) : ""}`)}</circle>`));
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">white CCT vs time</text>`);
    return P.svg();
  };

  // One SVG per data-chart key — the live dashboard and the report both call this.
  DLCCharts.build = function (key, charts, header) {
    const wcct = header && header.white && header.white.cct;
    switch (key) {
      case "cie": return DLCCharts.cie(charts.cie);
      case "eotf": return DLCCharts.eotf(charts.eotf);
      case "graycct": return DLCCharts.grayscaleCct(charts.grayscale, wcct);
      case "grayduv": return DLCCharts.grayscaleDuv(charts.grayscale);
      case "colorlum": return DLCCharts.colorLum(charts.color_lum);
      case "optimizer": return DLCCharts.optimizer(charts.optimizer);
      case "drift": return DLCCharts.drift(charts.white_track);
      default: return empty();
    }
  };

  DLCCharts.renderInto = function (root, charts, header) {
    if (!root || !charts) return;
    root.querySelectorAll("[data-chart]").forEach((el) => {
      el.innerHTML = DLCCharts.build(el.getAttribute("data-chart"), charts, header);
    });
  };

  global.DLCCharts = DLCCharts;
})(typeof window !== "undefined" ? window : globalThis);
