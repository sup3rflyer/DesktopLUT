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
  // Hover data carrier. Uses <desc>, NOT <title>: <title> triggers the browser's own native
  // tooltip (which covers our custom one); <desc> is invisible metadata with no native tooltip,
  // and the hover code reads it the same way. Content may be multi-row ("\n") with "\t" label/value.
  const hov = (s) => `<desc>${esc(s)}</desc>`;

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
  // Render options the dashboard toggles. `native` overlays the panel's measured native gamut on
  // the CIE tile (the standard target gamut is always shown — see the gamut toggle, #20).
  DLCCharts.opts = { native: true };

  // ── CIE 1931: measured scatter (coloured by patch) vs sRGB gamut + locus + white ──
  DLCCharts.cie = function (d) {
    d = d || {};
    const P = Plot({ xmin: 0, xmax: 0.75, ymin: 0, ymax: 0.85 });
    // A lighter backdrop just for the CIE tile so dark/low-signal measured points (near-black
    // fills) stay visible against the otherwise near-black plot area.
    P.add(`<rect x="${fmt(P.m.l, 1)}" y="${fmt(P.m.t, 1)}" width="${fmt(P.W - P.m.l - P.m.r, 1)}" height="${fmt(P.H - P.m.t - P.m.b, 1)}" class="ch-cie-bg"/>`);
    P.gridX([0, 0.2, 0.4, 0.6], (t) => fmt(t, 1));
    P.gridY([0, 0.2, 0.4, 0.6, 0.8], (t) => fmt(t, 1));
    if (d.locus && d.locus.length) P.pathLine(d.locus, "ch-locus");
    const pr = d.primaries || {};
    if (pr.r && pr.g && pr.b) {
      const poly = [pr.r, pr.g, pr.b].map((p) => `${fmt(P.px(p[0]), 1)},${fmt(P.py(p[1]), 1)}`).join(" ");
      P.add(`<polygon points="${poly}" class="ch-gamut">${hov("target gamut: " + (d.gamut_label || "Rec.709 / sRGB"))}</polygon>`);
      [["R", pr.r], ["G", pr.g], ["B", pr.b]].forEach(([lab, p]) => {
        P.add(`<text x="${fmt(P.px(p[0]), 1)}" y="${fmt(P.py(p[1]) - 5, 1)}" class="ch-note" text-anchor="middle">${lab}</text>`);
      });
    }
    // Measured native gamut overlay (the panel's real coverage vs the standard target gamut).
    const nat = d.native;
    if (DLCCharts.opts.native && nat && nat.r && nat.g && nat.b) {
      const npoly = [nat.r, nat.g, nat.b].map((p) => `${fmt(P.px(p[0]), 1)},${fmt(P.py(p[1]), 1)}`).join(" ");
      P.add(`<polygon points="${npoly}" class="ch-gamut-native">${hov("measured native gamut")}</polygon>`);
    }
    // gamut legend: standard target (always) + native overlay (when shown + available)
    P.add(`<text x="${fmt(P.m.l + 4, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">▱ ${esc(d.gamut_label || "target")}</text>`);
    if (DLCCharts.opts.native && nat && nat.r) {
      P.add(`<text x="${fmt(P.m.l + 4, 1)}" y="${fmt(P.m.t + 24, 1)}" class="ch-note-native">▱ native panel</text>`);
    }
    const pts = d.points || [];
    const cap = 2500, step = pts.length > cap ? Math.ceil(pts.length / cap) : 1;
    for (let i = 0; i < pts.length; i += step) {
      const p = pts[i];
      // inline style beats the CSS class fill, so a per-patch colour actually shows
      const fill = p.c ? ` style="fill:${esc(p.c)}"` : "";
      // target chromaticity (where this patch SHOULD sit): data-tx/ty in viewBox px let the hover
      // draw the error vector. The hover content is STRUCTURED ("\n" rows, "\t" label/value) so the
      // tooltip renders as a tile — header, then ΔE / Measured / Target rows.
      const hasT = p.tx != null && p.ty != null;
      const tAttr = hasT ? ` data-tx="${fmt(P.px(p.tx), 1)}" data-ty="${fmt(P.py(p.ty), 1)}"` : "";
      const rows = [p.label || (p.neutral ? "neutral" : "colour")];
      if (p.de != null) rows.push(`ΔE\t${fmt(p.de, 2)}`);
      rows.push(`Measured\t${fmt(p.x, 4)}, ${fmt(p.y, 4)}`);
      if (hasT) rows.push(`Target\t${fmt(p.tx, 4)}, ${fmt(p.ty, 4)}`);
      P.add(`<circle cx="${fmt(P.px(p.x), 1)}" cy="${fmt(P.py(p.y), 1)}" r="1.7" class="${p.neutral ? "ch-pt-n" : "ch-pt"}"${fill}${tAttr}>${hov(rows.join("\n"))}</circle>`);
    }
    if (d.white && d.white.length >= 2) {
      const wx = P.px(d.white[0]), wy = P.py(d.white[1]);
      // Target white as a crosshair + ring so it reads clearly as the TARGET (not just another point).
      P.add(`<line x1="${fmt(wx - 9, 1)}" y1="${fmt(wy, 1)}" x2="${fmt(wx + 9, 1)}" y2="${fmt(wy, 1)}" class="ch-target-cross"/>`);
      P.add(`<line x1="${fmt(wx, 1)}" y1="${fmt(wy - 9, 1)}" x2="${fmt(wx, 1)}" y2="${fmt(wy + 9, 1)}" class="ch-target-cross"/>`);
      P.add(`<circle cx="${fmt(wx, 1)}" cy="${fmt(wy, 1)}" r="4.5" class="ch-white">${hov(`target white xy ${fmt(d.white[0], 4)}, ${fmt(d.white[1], 4)}`)}</circle>`);
      P.add(`<text x="${fmt(wx + 8, 1)}" y="${fmt(wy - 8, 1)}" class="ch-note">target white</text>`);
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
    pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.Y / ymax), 1)}" r="2.2" class="ch-dot">${hov(`signal ${fmt(p.signal, 3)} | measured ${fmt(p.Y / ymax, 4)} | target ${fmt(refAt(p.signal), 4)}`)}</circle>`));

    // Second interpretation: the gamma/EOTF *tracking* line — flat on the target = perfect tracking.
    // SDR: the local gamma γ(s)=ln(Y_rel)/ln(s); HDR(PQ): measured/target luminance ratio (1.0=ideal).
    // Drawn semi-transparent on its OWN scale so it overlays the absolute EOTF curve without fighting it.
    const isPq = d.kind === "pq";
    const tTarget = isPq ? 1.0 : g;
    const tLo = isPq ? 0.7 : g - 0.6, tHi = isPq ? 1.3 : g + 0.6;
    const ty = (v) => P.py(Math.max(0, Math.min(1, (v - tLo) / (tHi - tLo))));
    const track = [];
    pts.forEach((p) => {
      if (p.signal <= 0.04) return;                  // gamma is ill-defined near black
      const yrel = p.Y / ymax;
      let v = null;
      if (isPq) { const r = refAt(p.signal); v = r > 1e-4 ? yrel / r : null; }
      else if (yrel > 0) { v = Math.log(yrel) / Math.log(p.signal); }
      if (v != null && isFinite(v)) track.push([p.signal, v]);
    });
    P.add(`<line x1="${fmt(P.px(0), 1)}" y1="${fmt(ty(tTarget), 1)}" x2="${fmt(P.px(1), 1)}" y2="${fmt(ty(tTarget), 1)}" class="ch-track-ref"/>`);
    if (track.length) {
      P.add(`<path d="${track.map((p, i) => (i ? "L" : "M") + fmt(P.px(p[0]), 1) + " " + fmt(ty(p[1]), 1)).join(" ")}" class="ch-track" fill="none"/>`);
      track.forEach((p) => P.add(`<circle cx="${fmt(P.px(p[0]), 1)}" cy="${fmt(ty(p[1]), 1)}" r="1.6" class="ch-track-dot">${hov(`signal ${fmt(p[0], 3)} | ${isPq ? "EOTF ratio " + fmt(p[1], 3) : "local γ " + fmt(p[1], 2)} | target ${fmt(tTarget, 2)}`)}</circle>`));
    }
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">${isPq ? "PQ" : "γ " + fmt(g, 2)}</text>`);
    P.add(`<text x="${fmt(P.px(0) + 4, 1)}" y="${fmt(ty(tTarget) - 4, 1)}" class="ch-track-note">${isPq ? "EOTF track ·target" : "γ track @" + fmt(g, 1)}</text>`);
    return P.svg();
  };

  // ── Grayscale CCT vs signal (+ target white CCT reference) ──
  DLCCharts.grayscaleCct = function (d, targetCct) {
    // Near-black neutrals (server-flagged `dim`) have noise-dominated / undefined CCT — keep them
    // off the trace AND the y-autoscale so one wild near-black read can't bury the real variation.
    const all = (d || []).filter((p) => p.cct != null);
    const pts = all.filter((p) => !p.dim);
    const hidden = all.length - pts.length;
    if (!pts.length) return empty(all.length ? "grayscale CCT near-black only — too dark to read" : "no grayscale CCT yet");
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
    pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.cct), 1)}" r="2.2" class="ch-dot">${hov(`signal ${fmt(p.signal, 3)} | CCT ${Math.round(p.cct)}K`)}</circle>`));
    if (hidden) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">${hidden} near-black read${hidden > 1 ? "s" : ""} hidden · CCT undefined &lt;1 nit</text>`);
    return P.svg();
  };

  // ── Grayscale Duv vs signal (zero = on the Planckian locus; +green above / −magenta below) ──
  DLCCharts.grayscaleDuv = function (d) {
    const all = (d || []).filter((p) => p.duv != null);
    const pts = all.filter((p) => !p.dim);           // drop near-black (noise-dominated Duv)
    const hidden = all.length - pts.length;
    if (!pts.length) return empty(all.length ? "grayscale Duv near-black only — too dark to read" : "no grayscale Duv yet");
    const span = Math.max(0.005, Math.max(...pts.map((p) => Math.abs(p.duv))) * 1.2);
    const P = Plot({ xmin: 0, xmax: 1, ymin: -span, ymax: span });
    // Tint the half-planes so the SIGN reads as a colour cast: Duv>0 = green (above the locus),
    // Duv<0 = magenta (below). The eye should keep the trace near the zero line.
    const xL = P.px(0), xR = P.px(1), y0 = P.py(0);
    P.add(`<rect x="${fmt(xL, 1)}" y="${fmt(P.m.t, 1)}" width="${fmt(xR - xL, 1)}" height="${fmt(y0 - P.m.t, 1)}" class="ch-band-green"/>`);
    P.add(`<rect x="${fmt(xL, 1)}" y="${fmt(y0, 1)}" width="${fmt(xR - xL, 1)}" height="${fmt((P.H - P.m.b) - y0, 1)}" class="ch-band-magenta"/>`);
    P.gridX([0, 0.5, 1], (t) => fmt(t, 1));
    P.gridY([-span, 0, span], (t) => fmt(t, 3));
    P.pathLine([[0, 0], [1, 0]], "ch-ref");
    P.pathLine(pts.map((p) => [p.signal, p.duv]), "ch-line");
    pts.forEach((p) => {
      const cls = p.duv > 0.0002 ? "ch-dot-green" : (p.duv < -0.0002 ? "ch-dot-magenta" : "ch-dot");
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.duv), 1)}" r="2.4" class="${cls}">${hov(`signal ${fmt(p.signal, 3)} | Duv ${fmt(p.duv, 5)} | ${p.duv > 0 ? "green" : p.duv < 0 ? "magenta" : "neutral"} | target 0`)}</circle>`);
    });
    P.add(`<text x="${fmt(xL + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-lab-green">▲ green (+Duv)</text>`);
    P.add(`<text x="${fmt(xL + 5, 1)}" y="${fmt(P.H - P.m.b - 5, 1)}" class="ch-lab-magenta">▼ magenta (−Duv)</text>`);
    if (hidden) P.add(`<text x="${fmt(P.W - P.m.r, 1)}" y="${fmt(P.m.t + 12, 1)}" text-anchor="end" class="ch-note">${hidden} near-black hidden</text>`);
    return P.svg();
  };

  // ── Grayscale RGB balance: per-channel % deviation from neutral vs signal (0 = neutral) ──
  DLCCharts.rgbBalance = function (d) {
    const all = (d || []).filter((p) => p.r != null && p.g != null && p.b != null);
    const pts = all.filter((p) => !p.dim);           // drop near-black (balance is noise there)
    const hidden = all.length - pts.length;
    if (!pts.length) return empty(all.length ? "grayscale balance near-black only — too dark to read" : "no grayscale balance yet");
    let span = 1.0;                                   // never tighter than ±1% so tiny errors don't look huge
    pts.forEach((p) => { span = Math.max(span, Math.abs(p.r), Math.abs(p.g), Math.abs(p.b)); });
    span *= 1.2;
    const P = Plot({ xmin: 0, xmax: 1, ymin: -span, ymax: span });
    P.gridX([0, 0.25, 0.5, 0.75, 1], (t) => Math.round(t * 100) + "%");
    P.gridY(niceTicks(-span, span, 4), (t) => (t > 0 ? "+" : "") + fmt(t, 1));
    P.pathLine([[0, 0], [1, 0]], "ch-ref");           // neutral axis — the eye keeps all three here
    const series = [["r", "ch-bal-r", "R"], ["g", "ch-bal-g", "G"], ["b", "ch-bal-b", "B"]];
    series.forEach(([k, cls]) => P.pathLine(pts.map((p) => [p.signal, p[k]]), cls));
    series.forEach(([k, cls, lab]) => pts.forEach((p) =>
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p[k]), 1)}" r="1.7" class="${cls}-dot">${hov(`signal ${fmt(p.signal, 3)} | ${lab} ${(p[k] >= 0 ? "+" : "")}${fmt(p[k], 2)}% | target 0`)}</circle>`)));
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">R / G / B balance · target 0%</text>`);
    if (hidden) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">${hidden} near-black hidden</text>`);
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
      P.add(`<rect x="${fmt(x0, 1)}" y="${fmt(Math.min(yA, yB), 1)}" width="${fmt(w, 1)}" height="${fmt(Math.max(1, Math.abs(yB - yA)), 1)}" fill="${esc(it.color)}" stroke="#000" stroke-width=".3">${hov(`${it.label}: ${(it.error >= 0 ? "+" : "")}${fmt(it.error * 100, 1)}% luminance error, ${it.n || 1} patch(es)`)}</rect>`);
      if (showLabels) {
        const lx = P.px(i) + bw / 2, ly = P.H - P.m.b + 11;
        P.add(`<text x="${fmt(lx, 1)}" y="${fmt(ly, 1)}" transform="rotate(-60 ${fmt(lx, 1)} ${fmt(ly, 1)})" text-anchor="end" class="ch-tick">${esc(it.label)}</text>`);
      }
    });
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">target 0% error</text>`);
    return P.svg();
  };

  // ── Saturation tracking: measured chroma (u'v') vs commanded saturation, per hue family ──
  DLCCharts.saturation = function (d) {
    const items = d || [];
    if (!items.length) return empty("no colour patches yet");
    const P = Plot({ xmin: 0, xmax: 1, ymin: 0, ymax: 1 });
    P.gridX([0, 0.25, 0.5, 0.75, 1], (t) => Math.round(t * 100) + "%");
    P.gridY([0, 0.25, 0.5, 0.75, 1], (t) => Math.round(t * 100) + "%");
    P.pathLine([[0, 0], [1, 1]], "ch-ref");                    // identity = perfect tracking
    // group into per-family polylines so each hue's saturation sweep reads as one line
    const fams = {};
    items.forEach((it) => { (fams[it.family] = fams[it.family] || []).push(it); });
    Object.keys(fams).forEach((fam) => {
      const pts = fams[fam].slice().sort((a, b) => a.target - b.target);
      P.pathLine(pts.map((p) => [p.target, p.measured]), "ch-sat-line");
      pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.target), 1)}" cy="${fmt(P.py(p.measured), 1)}" r="2.4" style="fill:${esc(p.color)}" stroke="#000" stroke-width=".3">${hov(`${esc(fam)} ${Math.round(p.target * 100)}% commanded → ${Math.round(p.measured * 100)}% measured chroma`)}</circle>`));
    });
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">measured vs commanded</text>`);
    P.add(`<text x="${P.m.l + 4}" y="${P.H - P.m.b - 5}" class="ch-note">on the line = perfect tracking</text>`);
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
      P.add(`<circle cx="${fmt(P.px(r.iteration), 1)}" cy="${fmt(P.py(r.measured_max_de), 1)}" r="2" class="ch-dot-max">${hov(`iteration ${r.iteration}: max dE ${fmt(r.measured_max_de, 3)}, above target ${r.above_threshold || 0}`)}</circle>`);
      P.add(`<circle cx="${fmt(P.px(r.iteration), 1)}" cy="${fmt(P.py(r.measured_mean_de), 1)}" r="2" class="ch-dot">${hov(`iteration ${r.iteration}: mean dE ${fmt(r.measured_mean_de, 3)}`)}</circle>`);
    });
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">max / mean ΔE</text>`);
    return P.svg();
  };

  // ── Channel drift: per-channel R/G/B level (% from warm-up start) over elapsed time ──
  // The ColourSpace-style thermal-stability read — exposes WHICH channel wanders as the panel
  // warms (a cool blue channel climbing while R/G hold). Flat = settled; a climbing trace = drifting.
  DLCCharts.channelDrift = function (d) {
    const pts = (d || []).filter((p) => p.elapsed_s != null && p.r != null);
    if (pts.length < 2) return empty("no drift series yet");
    const xs = pts.map((p) => p.elapsed_s);
    let lo = 0, hi = 0;
    pts.forEach((p) => { lo = Math.min(lo, p.r, p.g, p.b); hi = Math.max(hi, p.r, p.g, p.b); });
    const pad = Math.max(0.3, (hi - lo) * 0.12);
    lo -= pad; hi += pad;
    const xmax = Math.max(...xs) || 1;
    const P = Plot({ xmin: 0, xmax, ymin: lo, ymax: hi });
    P.gridX(niceTicks(0, xmax, 4), (t) => Math.round(t) + "s");
    P.gridY(niceTicks(lo, hi, 4), (t) => (t > 0 ? "+" : "") + fmt(t, 1));
    P.pathLine([[0, 0], [xmax, 0]], "ch-ref");        // baseline = the first (coldest) reading
    const series = [["r", "ch-bal-r", "R"], ["g", "ch-bal-g", "G"], ["b", "ch-bal-b", "B"]];
    series.forEach(([k, cls]) => P.pathLine(pts.map((p) => [p.elapsed_s, p[k]]), cls));
    series.forEach(([k, cls, lab]) => pts.forEach((p) =>
      P.add(`<circle cx="${fmt(P.px(p.elapsed_s), 1)}" cy="${fmt(P.py(p[k]), 1)}" r="1.6" class="${cls}-dot">${hov(`${Math.round(p.elapsed_s)}s | ${lab} ${(p[k] >= 0 ? "+" : "")}${fmt(p[k], 2)}% | CCT ${p.cct != null ? Math.round(p.cct) + "K" : "—"}`)}</circle>`)));
    // per-channel legend so the wandering channel is identifiable at a glance
    ["R", "G", "B"].forEach((lab, i) => P.add(`<text x="${fmt(P.m.l + 5 + i * 16, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-bal-${lab.toLowerCase()}-lab">${lab}</text>`));
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">R/G/B drift vs warm-up start</text>`);
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
      case "graybalance": return DLCCharts.rgbBalance(charts.grayscale);
      case "colorlum": return DLCCharts.colorLum(charts.color_lum);
      case "saturation": return DLCCharts.saturation(charts.saturation);
      case "optimizer": return DLCCharts.optimizer(charts.optimizer);
      case "drift": return DLCCharts.channelDrift(charts.channel_drift);
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
