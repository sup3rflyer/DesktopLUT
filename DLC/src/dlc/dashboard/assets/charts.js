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
  const fmtSignedDuv = (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(4);
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

  // JND colour class for a ΔE value (same bands as the readout cards: <1 ok, <3 warn, ≥3 bad)
  const deCls = (v) => v == null ? "" : (Math.abs(v) < 1 ? "ch-de-ok" : (Math.abs(v) < 3 ? "ch-de-warn" : "ch-de-bad"));
  // A near-black point's honest hover ROWS (structured "\t" label/value — the tooltip renders
  // them as a compact tile, never one overflowing line): CCT/Duv is noise there, but "can you
  // see the tint?" has an answer — the ΔE vs the neutral target. Content passes through esc(),
  // so plain "<1" here (a literal entity would double-escape and display as "&lt;1").
  const dimRows = (p) => {
    const rows = [];
    if (p.Y != null) rows.push(`Y\t${fmt(p.Y, 3)} nit`);
    rows.push(p.de == null ? "near-black\t<1 nit — CCT unreliable"
      : `ΔE vs neutral\t${fmt(p.de, 2)} · ${p.de < 1 ? "tint not visible" : p.de < 3 ? "borderline" : "visible tint"}`);
    return rows;
  };
  // Norm corridor: the shaded "normal" band. The scale is never allowed tighter than the band,
  // so a trace inside the corridor reads as healthy at ANY zoom; when data escapes it the caller
  // shows an explicit alert tag — autoscale can stretch, but never silently.
  const bandY = (P, lo, hi) =>
    `<rect x="${fmt(P.m.l, 1)}" y="${fmt(P.py(hi), 1)}" width="${fmt(P.W - P.m.l - P.m.r, 1)}" height="${fmt(Math.max(0, P.py(lo) - P.py(hi)), 1)}" class="ch-norm-band"/>`;
  const alertTag = (P, msg, anchor, row) => {
    const mid = anchor === "mid";
    return `<text x="${fmt(mid ? P.W / 2 : P.W - P.m.r, 1)}" y="${fmt(P.m.t + 12 * (row || 1), 1)}" text-anchor="${mid ? "middle" : "end"}" class="ch-alert">▲ ${esc(msg)}</text>`;
  };
  // Uncertainty whisker for a level with re-reads: the retained sample spread, drawn only when
  // it's wide enough to matter on this scale.
  const whisker = (P, x, lo, hi) =>
    `<line x1="${fmt(P.px(x), 1)}" y1="${fmt(P.py(lo), 1)}" x2="${fmt(P.px(x), 1)}" y2="${fmt(P.py(hi), 1)}" class="ch-whisker"/>`;

  const DLCCharts = {};
  // Render options the dashboard toggles. `measured` overlays the panel's measured primaries (the
  // full-drive RGB corners of the current stage) on the CIE tile (the standard target gamut is
  // always shown — see the gamut toggle, #20).
  DLCCharts.opts = { measured: true };

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
    // Measured-primaries overlay: the panel's actual full-drive RGB corners for the current stage
    // (post-MHC while profiling, verify once verified) vs the standard target gamut.
    const meas = d.measured;
    if (DLCCharts.opts.measured && meas && meas.r && meas.g && meas.b) {
      const npoly = [meas.r, meas.g, meas.b].map((p) => `${fmt(P.px(p[0]), 1)},${fmt(P.py(p[1]), 1)}`).join(" ");
      P.add(`<polygon points="${npoly}" class="ch-gamut-measured">${hov("measured primaries (full-drive corners)")}</polygon>`);
    }
    // gamut legend: standard target (always) + measured overlay (when shown + available)
    P.add(`<text x="${fmt(P.m.l + 4, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">▱ ${esc(d.gamut_label || "target")}</text>`);
    if (DLCCharts.opts.measured && meas && meas.r) {
      P.add(`<text x="${fmt(P.m.l + 4, 1)}" y="${fmt(P.m.t + 24, 1)}" class="ch-note-measured">▱ measured primaries</text>`);
    }
    const pts = d.points || [];
    const cap = 2500, step = pts.length > cap ? Math.ceil(pts.length / cap) : 1;
    let carried = 0;
    for (let i = 0; i < pts.length; i += step) {
      const p = pts[i];
      if (p.carried) carried++;
      // inline style beats the CSS class fill, so a per-patch colour actually shows
      const fill = p.c ? ` style="fill:${esc(p.c)}"` : "";
      // target chromaticity (where this patch SHOULD sit): data-tx/ty in viewBox px let the hover
      // draw the error vector. The hover content is STRUCTURED ("\n" rows, "\t" label/value) so the
      // tooltip renders as a tile — header, then ΔE / Measured / Target rows.
      const hasT = p.tx != null && p.ty != null;
      const tAttr = hasT ? ` data-tx="${fmt(P.px(p.tx), 1)}" data-ty="${fmt(P.py(p.ty), 1)}"` : "";
      const rows = [(p.label || (p.neutral ? "neutral" : "colour")) + (p.carried ? " · prev stage" : "")];
      if (p.de != null) rows.push(`ΔE\t${fmt(p.de, 2)}`);
      rows.push(`Measured\t${fmt(p.x, 4)}, ${fmt(p.y, 4)}`);
      if (hasT) rows.push(`Target\t${fmt(p.tx, 4)}, ${fmt(p.ty, 4)}`);
      if (p.carried) rows.push(`Status\tawaiting re-measure`);
      P.add(`<circle cx="${fmt(P.px(p.x), 1)}" cy="${fmt(P.py(p.y), 1)}" r="1.7" class="${p.neutral ? "ch-pt-n" : "ch-pt"}${p.carried ? " ch-carried" : ""}"${fill}${tAttr}>${hov(rows.join("\n"))}</circle>`);
    }
    if (carried) P.add(`<text x="${fmt(P.m.l + 4, 1)}" y="${fmt(P.H - P.m.b - 6, 1)}" class="ch-note">◌ ${carried} from previous stage</text>`);
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
    pts.forEach((p) => P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.Y / ymax), 1)}" r="2.2" class="ch-dot${p.carried ? " ch-carried" : ""}">${hov(`signal ${fmt(p.signal, 3)} | measured ${fmt(p.Y / ymax, 4)} | target ${fmt(refAt(p.signal), 4)}${p.carried ? " | prev stage — awaiting re-measure" : ""}`)}</circle>`));
    const eotfCarried = pts.filter((p) => p.carried).length;
    if (eotfCarried) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">◌ ${eotfCarried} from previous stage</text>`);

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
  // Norm-banded scale: a ±150 K corridor around the target is always visible and the scale never
  // zooms tighter than it — a trace inside the shaded band is healthy at any zoom, and when data
  // escapes it the chart says so explicitly instead of silently rescaling.
  // Near-black neutrals (server-flagged `dim`, off the autoscale) are judged in the ΔE domain:
  // CCT is a ratio of noise down there, but "can you see the tint?" still has an answer, so the
  // dot is coloured by its ΔE vs the neutral target rather than dismissed as unreliable.
  DLCCharts.grayscaleCct = function (d, targetCct) {
    const NORM_K = 150;
    const all = (d || []).filter((p) => p.cct != null);
    if (!all.length) return empty("no grayscale CCT yet");
    const bright = all.filter((p) => !p.dim);
    const dim = all.filter((p) => p.dim);
    const scaleSrc = bright.length ? bright : dim;     // autoscale from real reads; dim-only as fallback
    const ccts = scaleSrc.map((p) => p.cct);
    let lo = Math.min(...ccts), hi = Math.max(...ccts);
    if (targetCct) { lo = Math.min(lo, targetCct - NORM_K); hi = Math.max(hi, targetCct + NORM_K); }
    const pad = Math.max(40, (hi - lo) * 0.08);
    lo -= pad; hi += pad;
    const P = Plot({ xmin: 0, xmax: 1, ymin: lo, ymax: hi });
    if (targetCct) P.add(bandY(P, targetCct - NORM_K, targetCct + NORM_K));
    P.gridX([0, 0.5, 1], (t) => fmt(t, 1));
    P.gridY(niceTicks(lo, hi, 4), (t) => Math.round(t));
    if (targetCct) {
      P.pathLine([[0, targetCct], [1, targetCct]], "ch-ref");
      P.add(`<text x="${P.W - 16}" y="${fmt(P.py(targetCct) - 5, 1)}" text-anchor="end" class="ch-note">target ${Math.round(targetCct)}K ±${NORM_K}</text>`);
      if (bright.some((p) => Math.abs(p.cct - targetCct) > NORM_K))
        P.add(alertTag(P, `exceeds ±${NORM_K} K`));
    }
    bright.forEach((p) => {
      if (p.cct_lo != null && p.cct_hi != null && p.cct_hi - p.cct_lo > (hi - lo) * 0.015)
        P.add(whisker(P, p.signal, p.cct_lo, p.cct_hi));
    });
    P.pathLine(bright.map((p) => [p.signal, p.cct]), "ch-line");
    bright.forEach((p) => {
      const rows = [`signal ${fmt(p.signal, 3)}${p.carried ? " · prev stage" : ""}`,
                    `CCT\t${Math.round(p.cct)} K`];
      if (p.n > 1) rows.push(`median of ${p.n}\t${Math.round(p.cct_lo)}–${Math.round(p.cct_hi)} K`);
      if (p.carried) rows.push("status\tawaiting re-measure");
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.cct), 1)}" r="2.2" class="ch-dot${p.carried ? " ch-carried" : ""}">${hov(rows.join("\n"))}</circle>`);
    });
    const cctCarried = bright.filter((p) => p.carried).length;
    if (cctCarried) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.H - P.m.b - 6, 1)}" class="ch-note">◌ ${cctCarried} from previous stage</text>`);
    dim.forEach((p) => {
      const off = p.cct < lo || p.cct > hi;
      const cy = P.py(Math.max(lo, Math.min(hi, p.cct)));
      const rows = [`signal ${fmt(p.signal, 3)} · near-black`,
                    `CCT\t${Math.round(p.cct)} K (noise)${off ? " · off-scale" : ""}`,
                    ...dimRows(p)];
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(cy, 1)}" r="2.0" class="ch-dot-dim ${deCls(p.de)}${p.carried ? " ch-carried" : ""}">${hov(rows.join("\n"))}</circle>`);
    });
    if (dim.length) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">${dim.length} near-black · dot colour = ΔE visibility</text>`);
    return P.svg();
  };

  // ── Grayscale Duv vs signal (zero = on the Planckian locus; +green above / −magenta below) ──
  // Norm-banded AROUND THE TARGET WHITE'S OWN DUV: D65 sits ≈ +0.003 above the Planckian locus,
  // so a perfect D65 panel reads +0.003 — the corridor centres on the target, not on zero, and
  // the scale never zooms tighter than it. Near-black reads (server `dim`, off the autoscale)
  // are judged in the ΔE domain — coloured by tint visibility, not dismissed.
  DLCCharts.grayscaleDuv = function (d, targetDuv) {
    const NORM = 0.003;
    const t0 = targetDuv != null ? targetDuv : 0;
    const all = (d || []).filter((p) => p.duv != null);
    if (!all.length) return empty("no grayscale Duv yet");
    const bright = all.filter((p) => !p.dim);
    const dim = all.filter((p) => p.dim);
    const scaleSrc = bright.length ? bright : dim;
    const span = Math.max(Math.abs(t0) + NORM * 1.25,
                          Math.max(...scaleSrc.map((p) => Math.abs(p.duv))) * 1.2);
    const P = Plot({ xmin: 0, xmax: 1, ymin: -span, ymax: span });
    // Tint the half-planes so the SIGN reads as a colour cast relative to the TARGET:
    // above the target's Duv = greener than intended, below = more magenta.
    const xL = P.px(0), xR = P.px(1), y0 = P.py(t0);
    P.add(`<rect x="${fmt(xL, 1)}" y="${fmt(P.m.t, 1)}" width="${fmt(xR - xL, 1)}" height="${fmt(y0 - P.m.t, 1)}" class="ch-band-green"/>`);
    P.add(`<rect x="${fmt(xL, 1)}" y="${fmt(y0, 1)}" width="${fmt(xR - xL, 1)}" height="${fmt((P.H - P.m.b) - y0, 1)}" class="ch-band-magenta"/>`);
    P.add(bandY(P, t0 - NORM, t0 + NORM));
    P.gridX([0, 0.5, 1], (t) => fmt(t, 1));
    P.gridY([-span, 0, span], (t) => fmt(t, 3));
    P.pathLine([[0, t0], [1, t0]], "ch-ref");
    if (t0) P.add(`<text x="${P.W - 16}" y="${fmt(P.py(t0) - 4, 1)}" text-anchor="end" class="ch-note">target ${fmtSignedDuv(t0)} (locus offset of the target white)</text>`);
    if (bright.some((p) => Math.abs(p.duv - t0) > NORM)) P.add(alertTag(P, `exceeds target ±${NORM}`, "mid", 2));
    bright.forEach((p) => {
      if (p.duv_lo != null && p.duv_hi != null && p.duv_hi - p.duv_lo > span * 0.03)
        P.add(whisker(P, p.signal, p.duv_lo, p.duv_hi));
    });
    P.pathLine(bright.map((p) => [p.signal, p.duv]), "ch-line");
    bright.forEach((p) => {
      const dev = p.duv - t0;                          // cast vs the TARGET, not vs the locus
      const cls = dev > 0.0002 ? "ch-dot-green" : (dev < -0.0002 ? "ch-dot-magenta" : "ch-dot");
      const rows = [`signal ${fmt(p.signal, 3)}${p.carried ? " · prev stage" : ""}`,
                    `Duv\t${fmt(p.duv, 5)}`,
                    `vs target\t${fmtSignedDuv(dev)} · ${dev > 0.0002 ? "greener" : dev < -0.0002 ? "more magenta" : "on target"}`];
      if (p.n > 1) rows.push(`median of\t${p.n} reads`);
      if (p.carried) rows.push("status\tawaiting re-measure");
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p.duv), 1)}" r="2.4" class="${cls}${p.carried ? " ch-carried" : ""}">${hov(rows.join("\n"))}</circle>`);
    });
    const duvCarried = bright.filter((p) => p.carried).length;
    if (duvCarried) P.add(`<text x="${fmt(P.W - P.m.r, 1)}" y="${fmt(P.H - P.m.b - 6, 1)}" text-anchor="end" class="ch-note">◌ ${duvCarried} from previous stage</text>`);
    dim.forEach((p) => {
      const off = Math.abs(p.duv) > span;
      const cy = P.py(Math.max(-span, Math.min(span, p.duv)));
      const rows = [`signal ${fmt(p.signal, 3)} · near-black`,
                    `Duv\t${fmt(p.duv, 5)} (noise)${off ? " · off-scale" : ""}`,
                    ...dimRows(p)];
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(cy, 1)}" r="2.0" class="ch-dot-dim ${deCls(p.de)}${p.carried ? " ch-carried" : ""}">${hov(rows.join("\n"))}</circle>`);
    });
    P.add(`<text x="${fmt(xL + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-lab-green">▲ green (+Duv)</text>`);
    P.add(`<text x="${fmt(xL + 5, 1)}" y="${fmt(P.H - P.m.b - 5, 1)}" class="ch-lab-magenta">▼ magenta (−Duv)</text>`);
    if (dim.length) P.add(`<text x="${fmt(P.W - P.m.r, 1)}" y="${fmt(P.m.t + 12, 1)}" text-anchor="end" class="ch-note">${dim.length} near-black by ΔE</text>`);
    return P.svg();
  };

  // ── Grayscale RGB balance: per-channel % deviation from neutral vs signal (0 = neutral) ──
  // Near-black reads (server `dim`) are noisy but shown faded (off the autoscale, clamped to range).
  DLCCharts.rgbBalance = function (d) {
    const all = (d || []).filter((p) => p.r != null && p.g != null && p.b != null);
    if (!all.length) return empty("no grayscale balance yet");
    const bright = all.filter((p) => !p.dim);
    const dim = all.filter((p) => p.dim);
    const NORM = 1.0;                                 // ±1% corridor — the norm band; scale never tighter
    let span = NORM;
    (bright.length ? bright : dim).forEach((p) => { span = Math.max(span, Math.abs(p.r), Math.abs(p.g), Math.abs(p.b)); });
    span *= 1.2;
    const P = Plot({ xmin: 0, xmax: 1, ymin: -span, ymax: span });
    P.add(bandY(P, -NORM, NORM));
    P.gridX([0, 0.25, 0.5, 0.75, 1], (t) => Math.round(t * 100) + "%");
    P.gridY(niceTicks(-span, span, 4), (t) => (t > 0 ? "+" : "") + fmt(t, 1));
    P.pathLine([[0, 0], [1, 0]], "ch-ref");           // neutral axis — the eye keeps all three here
    if (bright.some((p) => Math.max(Math.abs(p.r), Math.abs(p.g), Math.abs(p.b)) > NORM))
      P.add(alertTag(P, "exceeds ±1%", "mid", 2));
    const series = [["r", "ch-bal-r", "R"], ["g", "ch-bal-g", "G"], ["b", "ch-bal-b", "B"]];
    const clamp = (v) => Math.max(-span, Math.min(span, v));
    series.forEach(([k, cls]) => P.pathLine(bright.map((p) => [p.signal, p[k]]), cls));
    series.forEach(([k, cls, lab]) => bright.forEach((p) => {
      const rows = [`signal ${fmt(p.signal, 3)}${p.carried ? " · prev stage" : ""}`,
                    `${lab}\t${(p[k] >= 0 ? "+" : "")}${fmt(p[k], 2)}% · target 0%`];
      if (p.n > 1) rows.push(`median of\t${p.n} reads`);
      if (p.carried) rows.push("status\tawaiting re-measure");
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(p[k]), 1)}" r="1.7" class="${cls}-dot${p.carried ? " ch-carried" : ""}">${hov(rows.join("\n"))}</circle>`);
    }));
    const balCarried = bright.filter((p) => p.carried).length;
    if (balCarried) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.H - P.m.b - 6, 1)}" class="ch-note">◌ ${balCarried} from previous stage</text>`);
    series.forEach(([k, , lab]) => dim.forEach((p) => {
      const rows = [`signal ${fmt(p.signal, 3)} · near-black`,
                    `${lab}\t${(p[k] >= 0 ? "+" : "")}${fmt(p[k], 2)}% (noise)${Math.abs(p[k]) > span ? " · off-scale" : ""}`,
                    ...dimRows(p)];
      P.add(`<circle cx="${fmt(P.px(p.signal), 1)}" cy="${fmt(P.py(clamp(p[k])), 1)}" r="1.6" class="ch-dot-dim">${hov(rows.join("\n"))}</circle>`);
    }));
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">R / G / B balance · target 0%</text>`);
    if (dim.length) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">${dim.length} near-black faded</text>`);
    return P.svg();
  };

  // ── Colour luminance: per-patch luminance error vs target, bars coloured by patch ──
  DLCCharts.colorLum = function (d) {
    const items = d || [];
    if (!items.length) return empty("no colour patches yet");
    const NORM = 0.10;                                // ±10% luminance corridor
    const maxAbs = Math.max(NORM * 1.2, ...items.map((i) => Math.abs(i.error)));
    const span = Math.min(0.5, maxAbs * 1.15);
    const P = Plot({ mb: 50, xmin: 0, xmax: items.length, ymin: -span, ymax: span });
    P.add(bandY(P, -NORM, NORM));
    P.gridY(niceTicks(-span, span, 4), (t) => (t > 0 ? "+" : "") + Math.round(t * 100) + "%");
    if (items.some((i) => Math.abs(i.error) > NORM)) P.add(alertTag(P, "exceeds ±10%", "mid"));
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

  // ── Channel drift: per-channel R/G/B level (% from warm-up start) over elapsed time ──
  // The ColourSpace-style thermal-stability read — exposes WHICH channel wanders as the panel
  // warms (a cool blue channel climbing while R/G hold). Flat = settled; a climbing trace = drifting.
  DLCCharts.channelDrift = function (d) {
    const NORM = 0.5;                                 // ±0.5% — a settled panel stays inside this
    const pts = (d || []).filter((p) => p.elapsed_s != null && p.r != null);
    if (pts.length < 2) return empty("no drift series yet");
    const xs = pts.map((p) => p.elapsed_s);
    let lo = -NORM, hi = NORM;
    pts.forEach((p) => { lo = Math.min(lo, p.r, p.g, p.b); hi = Math.max(hi, p.r, p.g, p.b); });
    const pad = Math.max(0.15, (hi - lo) * 0.12);
    lo -= pad; hi += pad;
    const xmax = Math.max(...xs) || 1;
    const P = Plot({ xmin: 0, xmax, ymin: lo, ymax: hi });
    P.add(bandY(P, -NORM, NORM));
    P.gridX(niceTicks(0, xmax, 4), (t) => Math.round(t) + "s");
    P.gridY(niceTicks(lo, hi, 4), (t) => (t > 0 ? "+" : "") + fmt(t, 1));
    P.pathLine([[0, 0], [xmax, 0]], "ch-ref");        // baseline = the first (coldest) reading
    if (pts.some((p) => Math.max(Math.abs(p.r), Math.abs(p.g), Math.abs(p.b)) > NORM))
      P.add(alertTag(P, "drift exceeds ±0.5%", "mid", 2));
    const series = [["r", "ch-bal-r", "R"], ["g", "ch-bal-g", "G"], ["b", "ch-bal-b", "B"]];
    series.forEach(([k, cls]) => P.pathLine(pts.map((p) => [p.elapsed_s, p[k]]), cls));
    series.forEach(([k, cls, lab]) => pts.forEach((p) =>
      P.add(`<circle cx="${fmt(P.px(p.elapsed_s), 1)}" cy="${fmt(P.py(p[k]), 1)}" r="1.6" class="${cls}-dot">${hov(`${Math.round(p.elapsed_s)}s | ${lab} ${(p[k] >= 0 ? "+" : "")}${fmt(p[k], 2)}% | CCT ${p.cct != null ? Math.round(p.cct) + "K" : "—"}`)}</circle>`)));
    // per-channel legend so the wandering channel is identifiable at a glance
    ["R", "G", "B"].forEach((lab, i) => P.add(`<text x="${fmt(P.m.l + 5 + i * 16, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-bal-${lab.toLowerCase()}-lab">${lab}</text>`));
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">R/G/B drift vs warm-up start</text>`);
    return P.svg();
  };

  // ── Shadow tracking: the EOTF in log/log — where the low-light story actually lives ──
  // The linear EOTF tile gives the bottom 5% of the curve 5% of the pixels; content lives in the
  // mids and shadows, so this tile gives the shadows the room. Straight reference line (power
  // gamma is linear in log/log); dots coloured by their ΔE vs neutral where near black.
  DLCCharts.shadowEotf = function (d) {
    d = d || {};
    const lg = Math.log10;
    const raw = (d.points || []).filter((p) => p.Y != null && p.signal > 0).sort((a, b) => a.signal - b.signal);
    if (raw.length < 2) return empty("no grayscale reads yet");
    const ymaxY = Math.max(...raw.map((p) => p.Y)) || 1;
    const pts = raw.map((p) => ({ s: p.signal, yr: p.Y / ymaxY, de: p.de, carried: p.carried }))
      .filter((p) => p.yr > 0);
    if (pts.length < 2) return empty("no above-black reads yet");
    const X0 = -3;                                     // 0.1% … 100% signal
    const ys = pts.map((p) => lg(p.yr));
    const Y0 = Math.max(-5, Math.min(Math.floor(Math.min(...ys)) - 0.4, -2));
    const P = Plot({ ml: 56, xmin: X0, xmax: 0, ymin: Y0, ymax: 0 });
    const pctLab = (t) => { const v = Math.pow(10, t) * 100; return v >= 1 ? Math.round(v) + "%" : (v >= 0.1 ? v.toFixed(1) : v.toFixed(2)) + "%"; };
    P.gridX([-3, -2, -1, 0], pctLab);
    const yts = []; for (let t = Math.ceil(Y0); t <= 0; t++) yts.push(t);
    P.gridY(yts, pctLab);
    const g = d.gamma || 2.2;
    if (d.kind === "pq") {
      const ref = (d.reference || []).filter((r) => r[0] > 0 && r[1] > 0).map((r) => [lg(r[0]), lg(r[1])]);
      P.pathLine(ref.filter((r) => r[0] >= X0 && r[1] >= Y0), "ch-ref");
    } else {
      const xs = Math.max(X0, Y0 / g);                 // clip the straight γ line to the plot
      P.pathLine([[xs, xs * g], [0, 0]], "ch-ref");
    }
    P.pathLine(pts.map((p) => [Math.max(X0, lg(p.s)), Math.max(Y0, lg(p.yr))]), "ch-line");
    pts.forEach((p) => {
      const dark = p.yr * ymaxY < 1.0;                 // sub-1-nit: colour by tint visibility
      const cls = `ch-dot${dark ? " " + deCls(p.de) : ""}${p.carried ? " ch-carried" : ""}`;
      P.add(`<circle cx="${fmt(P.px(Math.max(X0, lg(p.s))), 1)}" cy="${fmt(P.py(Math.max(Y0, lg(p.yr))), 1)}" r="2.2" class="${cls}">${hov(`signal ${pctLab(lg(p.s))} | Y ${pctLab(lg(p.yr))} of white (${fmt(p.yr * ymaxY, 3)} nit)${p.de != null ? ` | ΔE ${fmt(p.de, 2)}` : ""}${p.carried ? " | prev stage" : ""}`)}</circle>`);
    });
    const below = raw.length - pts.length;
    if (below) P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">${below} read${below === 1 ? "" : "s"} at 0 nit (below meter floor)</text>`);
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">log/log · ${d.kind === "pq" ? "PQ" : "γ " + fmt(g, 2)}</text>`);
    return P.svg();
  };

  // ── ΔE distribution (ECDF): the whole verdict, not three summary points ──
  // "94% under 1 JND, 3 visible misses" is what avg/p95/max are trying to say. JND bands shaded;
  // carried (previous-stage) points excluded — this is THIS stage's distribution.
  DLCCharts.deDist = function (cie) {
    const des = (((cie || {}).points) || []).filter((p) => p.de != null && !p.carried)
      .map((p) => p.de).sort((a, b) => a - b);
    if (des.length < 3) return empty("no per-patch ΔE yet");
    const xmax = Math.max(3.2, des[des.length - 1] * 1.08);
    const P = Plot({ xmin: 0, xmax, ymin: 0, ymax: 1 });
    const stripX = (a, b, cls) =>
      P.add(`<rect x="${fmt(P.px(a), 1)}" y="${fmt(P.m.t, 1)}" width="${fmt(Math.max(0, P.px(Math.min(b, xmax)) - P.px(a)), 1)}" height="${fmt(P.H - P.m.t - P.m.b, 1)}" class="${cls}"/>`);
    stripX(0, 1, "ch-zone-ok"); stripX(1, 3, "ch-zone-warn"); stripX(3, xmax, "ch-zone-bad");
    P.gridX(niceTicks(0, xmax, 4), (t) => fmt(t, 1));
    P.gridY([0, 0.25, 0.5, 0.75, 1], (t) => Math.round(t * 100) + "%");
    let path = `M${fmt(P.px(0), 1)} ${fmt(P.py(0), 1)}`;
    des.forEach((v, i) => {
      path += ` L${fmt(P.px(Math.min(v, xmax)), 1)} ${fmt(P.py(i / des.length), 1)}`
        + ` L${fmt(P.px(Math.min(v, xmax)), 1)} ${fmt(P.py((i + 1) / des.length), 1)}`;
    });
    path += ` L${fmt(P.px(xmax), 1)} ${fmt(P.py(1), 1)}`;
    P.add(`<path d="${path}" class="ch-line" fill="none"/>`);
    // median + p95 markers, hoverable
    [[0.5, "median"], [0.95, "95th"]].forEach(([q, lab]) => {
      const v = des[Math.min(des.length - 1, Math.floor(q * des.length))];
      P.add(`<circle cx="${fmt(P.px(Math.min(v, xmax)), 1)}" cy="${fmt(P.py(q), 1)}" r="2.6" class="ch-dot">${hov(`${lab} ΔE ${fmt(v, 2)}`)}</circle>`);
    });
    const under1 = des.filter((v) => v < 1).length, over3 = des.filter((v) => v >= 3).length;
    P.add(`<text x="${fmt(P.m.l + 5, 1)}" y="${fmt(P.m.t + 12, 1)}" class="ch-note">${Math.round(under1 / des.length * 100)}% &lt; 1 JND · ${over3 ? `${over3} visible (≥3)` : "none visible"} · ${des.length} patches</text>`);
    return P.svg();
  };

  // ── Convergence: ΔE avg/max over the whole run (build iterations + scored passes) ──
  // The "watch it get better" chart — each build iteration and each scored verify lands as a
  // point, so improvement (or a regression) is a shape, not a memory.
  DLCCharts.convergence = function (d) {
    const pts = (d || []).filter((p) => p.elapsed_s != null && (p.avg != null || p.max != null));
    if (pts.length < 2) return empty("no scored iterations yet");
    const xmax = Math.max(...pts.map((p) => p.elapsed_s)) || 1;
    const vals = pts.flatMap((p) => [p.avg, p.max]).filter((v) => v != null);
    const ymax = Math.max(3.2, Math.max(...vals) * 1.15);
    const P = Plot({ xmin: 0, xmax, ymin: 0, ymax });
    const stripY = (a, b, cls) =>
      P.add(`<rect x="${fmt(P.m.l, 1)}" y="${fmt(P.py(Math.min(b, ymax)), 1)}" width="${fmt(P.W - P.m.l - P.m.r, 1)}" height="${fmt(Math.max(0, P.py(a) - P.py(Math.min(b, ymax))), 1)}" class="${cls}"/>`);
    stripY(0, 1, "ch-zone-ok"); stripY(1, 3, "ch-zone-warn"); stripY(3, ymax, "ch-zone-bad");
    P.gridX(niceTicks(0, xmax, 4), (t) => Math.round(t) + "s");
    P.gridY(niceTicks(0, ymax, 4), (t) => fmt(t, 1));
    P.pathLine(pts.filter((p) => p.max != null).map((p) => [p.elapsed_s, Math.min(p.max, ymax)]), "ch-line-max");
    P.pathLine(pts.filter((p) => p.avg != null).map((p) => [p.elapsed_s, p.avg]), "ch-line");
    pts.forEach((p) => {
      const build = p.kind === "build";
      if (p.max != null) P.add(`<circle cx="${fmt(P.px(p.elapsed_s), 1)}" cy="${fmt(P.py(Math.min(p.max, ymax)), 1)}" r="1.8" class="${build ? "ch-dot-hollow" : "ch-dot-max"}">${hov(`${p.label || p.kind} | max ΔE ${fmt(p.max, 2)}${build ? " (build probe)" : " (scored)"}`)}</circle>`);
      if (p.avg != null) P.add(`<circle cx="${fmt(P.px(p.elapsed_s), 1)}" cy="${fmt(P.py(p.avg), 1)}" r="${build ? 2.0 : 2.6}" class="${build ? "ch-dot-hollow" : "ch-dot"}">${hov(`${p.label || p.kind} | avg ΔE ${fmt(p.avg, 2)}${build ? " (build probe)" : " (scored)"}`)}</circle>`);
    });
    P.add(`<text x="${P.W - 16}" y="22" text-anchor="end" class="ch-note">avg (amber) · max (gray) · ○ build iterations</text>`);
    return P.svg();
  };

  // ── Worst patches: intended vs measured, side by side — the miss you can SEE ──
  // The intuitive kernel of "patch chart" displays without fake geometry: the largest ΔEs of the
  // CURRENT stage as split swatches. Measured colour is an approximation for the eye; the ΔE
  // number stays the authority.
  DLCCharts.offenders = function (list) {
    const items = (list || []).filter((o) => o.de != null).slice(0, 6);
    if (!items.length) return empty("no scored patches yet");
    const W = VB.w, H = VB.h, left = 16, top = 40, sw = 62;
    const rh = Math.min(42, (H - top - 8) / items.length);
    const parts = [
      `<text x="${left}" y="18" class="ch-note">largest ΔE this stage — lower rows should be rare</text>`,
      `<text x="${left}" y="${top - 7}" class="ch-tick">intended</text>`,
      `<text x="${left + sw + 4}" y="${top - 7}" class="ch-tick">measured</text>`,
    ];
    items.forEach((o, i) => {
      const y = top + i * rh, h = rh - 7;
      const name = o.label || (o.neutral ? "neutral" : "colour");
      parts.push(`<rect x="${left}" y="${fmt(y, 1)}" width="${sw}" height="${fmt(h, 1)}" rx="2" class="ch-sw" style="fill:${esc(o.sc || "#444")}">${hov(`${name}\nintended\t${o.sc || "—"}\nΔE\t${fmt(o.de, 2)}`)}</rect>`);
      parts.push(`<rect x="${left + sw + 4}" y="${fmt(y, 1)}" width="${sw}" height="${fmt(h, 1)}" rx="2" class="ch-sw" style="fill:${esc(o.mc || "#444")}">${hov(`${name}\nmeasured (approx.)\t${o.mc || "—"}\nΔE\t${fmt(o.de, 2)}`)}</rect>`);
      parts.push(`<text x="${left + 2 * sw + 18}" y="${fmt(y + h / 2 + 4, 1)}" class="ch-off-lab">${esc(name)}</text>`);
      parts.push(`<text x="${W - 18}" y="${fmt(y + h / 2 + 4, 1)}" text-anchor="end" class="ch-off-de ${deCls(o.de)}">${fmt(o.de, 2)}</text>`);
    });
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">${parts.join("")}</svg>`;
  };

  // One SVG per data-chart key — the live dashboard and the report both call this.
  DLCCharts.build = function (key, charts, header) {
    const wcct = header && header.white && header.white.cct;
    switch (key) {
      case "cie": return DLCCharts.cie(charts.cie);
      case "eotf": return DLCCharts.eotf(charts.eotf);
      case "shadow": return DLCCharts.shadowEotf(charts.eotf);
      case "graycct": return DLCCharts.grayscaleCct(charts.grayscale, wcct);
      case "grayduv": return DLCCharts.grayscaleDuv(charts.grayscale, charts.target_duv);
      case "graybalance": return DLCCharts.rgbBalance(charts.grayscale);
      case "colorlum": return DLCCharts.colorLum(charts.color_lum);
      case "dist": return DLCCharts.deDist(charts.cie);
      case "offenders": return DLCCharts.offenders(charts.offenders);
      case "convergence": return DLCCharts.convergence(charts.convergence);
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
