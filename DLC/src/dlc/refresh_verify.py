"""REFRESH TRIM verify — does the trim DesktopLUT applies hold the light at another refresh rate? (HDR)

The refresh-response flow measures the panel with the trim OFF and records the response in the DIP; DesktopLUT then
bakes refresh-keyed HDR profile variants from it. This is the check of the result: the SAME flow and machinery
(``dlc.refresh_probe``: the display found by hardware id, one consent per switch away from the reference, the mode
switched temporarily and read back exactly, reference -> rate -> reference brackets in two mirrored passes, the exact
recorded mode and the stack put back on every ending, ``--abort`` / ``refresh_probe restore`` for a run that died),
run with the spec's ``"purpose": "verify"``. What differs:

* **The trim stays ON and is never touched.** The run refuses before anything changes when the display's REFRESH TRIM
  is off, when DesktopLUT has no trim data for the mode (no DLC folder / DIP / ``refresh_response`` section, or an
  invalid one), or when a rate to verify is not in the recorded response. ``refresh_trim`` is never sent in a
  ``layers.set``, so no restore can change it either.
* **What is measured** (spec ``verify_stack``). ``as_viewed`` (default, the owner's question "is what I watch at 48 /
  50 Hz the same light as at 60"): the stack exactly as it is — the 3D LUT in the path, every viewing layer left as
  the owner has it (the run changes NO layer; ``verify_layers_off`` names any to switch off for the run). The
  comparison is a ratio at identical codes, so static layers cancel; the plan seam names the layers on and what each
  means for a full-field grey (none is keyed by the refresh rate; the adaptive ones settle). ``mhc_only``: the
  isolated MHC (3D LUT bypassed, viewing layers off) — no DesktopLUT pass processes the frame, so nothing dithers and
  the MHC2 output lands on whole link codes per channel (live 2026-11-14: residuals of 0.3-0.7 % at 0.9-2.3 % per
  code); one link code in % of light (the link depth from ``windows.query_monitors`` ``link_bpc``, the PQ slope at the
  grey's light) is reported per grey, and a residual below it is "not resolvable without dither" (undecided).
* **The trim is read back at every rate**: after each switch has settled, ``state.get`` ``refresh_trim`` must say the
  variant for that rate is on screen (``state`` ``trim`` for the rate or the rate whose curve it uses, ``applied``, and
  the profile's ``active_stamp`` equal to the wanted ``stamp`` — ``applied`` alone is not proof; ``state``
  ``reference`` at the reference rate and at a rate recorded as the same as the reference, following the ``same_as``
  chain as the C++ does; polled for up to ``trim_wait_s``). A leg where it is not is recorded as such and fails its
  rate. Every rate, a same-as-reference one included, is judged by the same light rule.
* **Nothing is recorded.** The run writes ``verify_result.json`` (and the rows / evidence), never ``trim_curves.json``
  or ``refresh_result.json``; ``python -m dlc.refresh_trim record`` refuses a verify run outright.

**The rule** (the owner's, 2026-11-13), per rate and grey, against the reference reads bracketing it in time (the panel
drifts 0.1-0.2 %/min, so each rate read is set against the reference value interpolated linearly in time between
the nearest reference reads before and after it, ``refresh_trim.bracket_ratios``):

* luminance: the combined ratio (inverse-variance mean over the passes, ``refresh_trim.combine``) within
  ``verify_lum_tol`` (0.3 %) of 1, AND every single bracketed read within it too;
* chromaticity: the combined shift (rate minus interpolated reference, mean over the passes) in x and in y, against
  how much that grey's chromaticity moves from one REFERENCE leg to the next with drift included — each interior
  reference read predicted from its neighbours (leave-one-out), the residuals' RMS (:func:`reference_xy_repeatability`;
  a rate read has the same make-up under "no refresh effect"). t = shift / (rms / sqrt(n passes)), Student t with nu =
  the number of residuals, two-sided; a shift fails when p < 0.05 / (2 x the run's grey-rate cells) — Bonferroni, so
  the run has <= 5 % chance of any false chromaticity fail. Every number (shift, se, t, p, critical shift) is in the
  result. (Replaced 2026-11-14 the "within the reference reads' max-min spread" rule: on the live run that spread was
  mostly the reference reads' LINEAR drift over the run, ~0.0003 in x; the leg-to-leg repeatability after drift is
  ~0.00001-0.0001, and the rate shifts of 0.0001-0.003 the mhc_only stack showed are real, i.e. the link rounding.)

Verdict per rate: ``pass`` (every grey passes both, the trim read back on screen on every leg of the rate), ``fail``
(a luminance miss, a significant chromaticity shift, or the trim not on screen), or ``undecided`` — in ``mhc_only`` a
luminance miss below one link code or any chromaticity shift (link rounding: not resolvable without dither), an
unsettled read, a read with no bracketed ratio (no reference read on both sides, no light, implausible — never dropped
silently: named per grey in the result), too few reference legs for a repeatability estimate, or anomalies: not a
deterministic yes/no, so the digest carries the numbers and the overseer judges (nothing is applied or recorded
either way). The run's verdict is the worst of its rates.

Simulation: ``python -m dlc.refresh_verify simulate --out DIR [--verify-stack as_viewed|mhc_only] [--residual-pct P]
[--trim-off] [--no-data] [--stuck]`` runs the whole flow in-process on the mock DesktopLUT + ``refresh_mode.SimDisplayApi``
+ ``probe_sim.ProbeSimPanel`` (no pipe, no meter, no display mode is touched on this machine); ``mhc_only`` rounds the
simulated MHC output to the 10-bit link, ``as_viewed`` does not (:func:`sim_refresh_gain`).
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import refresh_trim as rt

PURPOSE = "verify"
RESULT_FILE = "verify_result.json"
NO_DATA_STATES = ("no_dlc", "no_dip", "no_section", "invalid")   # C++ refresh_trim.cpp; "off" = the switch (refused apart)
__all__ = ["trim_status", "trim_refusals", "trim_leg_check", "analyze_verify", "PURPOSE", "RESULT_FILE"]


# ----------------------------------------------------------------------------- the trim's state (pure)
def trim_status(state: dict[str, Any], key: str) -> dict[str, Any]:
    """``state.get`` ``refresh_trim[<monitor>:HDR]`` (``{}`` when absent) plus the layer switch."""
    st = dict(((state or {}).get("refresh_trim") or {}).get(key) or {})
    st["layer_on"] = bool((((state or {}).get("layers") or {}).get(key) or {}).get("refresh_trim"))
    return st


def dip_rates(lines: Sequence[Any]) -> dict[str, str]:
    """The recorded rates from ``refresh_trim.dip.rates`` — the C++ reports LINES (refresh_trim.cpp RateLines):
    ``"47.952 Hz trim (3 levels)"``, ``"48 Hz = 47.952"`` (a rate that uses another's curve), ``"50 Hz (no levels)"``.
    ``{rate key: the key whose curve it uses}``; a rate without levels is left out (nothing to apply)."""
    out: dict[str, str] = {}
    for ln in lines or ():
        s = str(ln).strip()
        if " Hz" not in s:
            continue
        k, rest = s.split(" Hz", 1)
        k = k.strip()
        if "(no levels" in rest:
            continue
        if rest.strip().startswith("="):
            out[k] = rest.strip()[1:].strip().split(" ")[0]
        else:
            out[k] = k
    return out


def resolve_rate(rate: str, have: dict[str, str], reference: str) -> tuple[Optional[str], str]:
    """Follow ``rate``'s ``same_as`` chain as the C++ does (ChooseRefreshTrim, refresh_trim.cpp:126-140):
    ``("reference", why)`` when it ends at the reference (DesktopLUT then applies NO trim: state ``reference``),
    ``(key, why)`` = the rate whose own curve applies, ``(None, why)`` = not recorded / a broken or circular chain."""
    if rate == reference:
        return "reference", "the reference rate"
    if rate not in have:
        return None, f"{rate} Hz is not recorded"
    cur, seen = rate, {rate}
    for _ in range(16):
        nxt = have.get(cur)
        if nxt is None:
            return None, f"same_as {cur} is not in the section"
        if nxt == cur:
            return cur, f"{rate} Hz uses the {cur} Hz curve" if cur != rate else f"{rate} Hz has its own curve"
        if nxt == reference:
            return "reference", f"{rate} Hz = {reference} Hz (recorded same as the reference: no trim)"
        if nxt in seen:
            return None, f"{rate} Hz: same_as goes round in a circle"
        seen.add(nxt)
        cur = nxt
    return None, f"{rate} Hz: same_as chain too long"


def trim_refusals(state: dict[str, Any], key: str, rates: Sequence[str], reference: Optional[str] = None) -> list[str]:
    """Why a verify must not start (empty = it may). It never switches the trim, so off / no data are refusals."""
    st = trim_status(state, key)
    out = []
    if not st["layer_on"] or st.get("enabled") is False:
        out.append(f"REFRESH TRIM is off for {key}: the verify measures the trim and never switches it — switch it on "
                   "in DesktopLUT (PIPELINE -> GRAYSCALE, HDR side, the REFRESH TRIM row) and run again")
    state_word = st.get("state")
    dip = st.get("dip") or {}
    if not st or state_word in NO_DATA_STATES or not dip:
        out.append(f"DesktopLUT has no refresh-trim data for {key} (state {state_word!r}: {st.get('detail') or 'no DIP '
                   'refresh_response'}): record a refresh response first (--flow refresh-response, then "
                   "python -m dlc.refresh_trim record --write)")
    elif dip.get("stale"):
        out.append(f"the DIP refresh response DesktopLUT uses for {key} is marked stale ({dip.get('record')}, "
                   f"{dip.get('age_days')} days): re-measure before verifying it")
    have = dip_rates(dip.get("rates") or [])
    if dip:
        missing = [r for r in rates if r not in have]
        if missing:
            out.append(f"rates {missing} have no recorded response for {key} (recorded: {have}): the trim has nothing "
                       "to apply there")
        # the chain check needs the reference (the C++ reply does not name the DIP's reference key): skipped without it
        broken = [why for r in rates if r in have and reference
                  for res, why in [resolve_rate(r, have, reference)] if res is None]
        if broken:
            out.append(f"the recorded response for {key} has a broken same_as chain: {broken}")
    return out


def trim_leg_check(status: dict[str, Any], rate: str, reference: str, *,
                   ref_active_stamp: Optional[str] = None) -> dict[str, Any]:
    """Is the trim DesktopLUT means for ``rate`` on screen? ``ok`` True / False — a deterministic read of its own report.

    * the reference, or a rate recorded as the same as the reference (``"59.94 Hz = 60"``; the chain is followed): no
      trim — ``state`` ``reference`` and ``applied``;
    * any other rate: ``state`` ``trim`` for that rate (or the rate whose curve it uses), ``applied``, AND the profile on
      screen carries it. ``applied`` alone is not proof: with the stage bypass PERM_NOGRAY the C++ reports ``applied``
      with no trim baked (refresh_trim.cpp:481-488). Rule ``stamp``: the reply's ``active_stamp`` is non-empty and equals
      its ``stamp`` (the wanted one; both emitted by desktoplut_ipc_server.cpp RefreshTrimJson). Rule ``stamp_differs``
      (a reply without ``stamp``): a non-empty ``active_stamp`` that differs from the one read at the reference."""
    have = dip_rates(((status.get("dip") or {}).get("rates")) or [])
    target, how = resolve_rate(rate, have, reference)
    want = "reference" if target == "reference" else "trim"
    got_state, got_rate = status.get("state"), (str(status.get("rate")) if status.get("rate") is not None else None)
    active, wanted = status.get("active_stamp") or "", status.get("stamp") or ""
    out = {"rate": rate, "resolved": target, "resolved_why": how, "want_state": want, "state": got_state,
           "trim_rate": got_rate, "applied": status.get("applied"), "waiting": status.get("waiting"),
           "refresh_hz": status.get("refresh_hz"), "stamp": status.get("stamp"),
           "active_stamp": status.get("active_stamp"), "gain": status.get("gain")}
    if target is None:
        out.update(ok=False, rule="unresolved", why=how)
        return out
    ok = bool(status.get("applied")) and got_state == want
    if want == "reference":
        out.update(ok=ok, rule="reference")
        return out
    ok = ok and got_rate in (rate, target)
    if wanted:
        rule = "stamp"
        on_screen = bool(active) and active == wanted
    else:
        rule = "stamp_differs"
        on_screen = bool(active) and active != (ref_active_stamp or "")
    out.update(ok=bool(ok and on_screen), rule=rule, on_screen=on_screen)
    if ok and not on_screen:
        out["why"] = (f"DesktopLUT reports the {target} Hz trim applied, but the profile on screen does not carry it "
                      f"(active_stamp {active!r}, wanted {wanted!r}{'' if wanted else ', reference ' + repr(ref_active_stamp)})")
    return out


# ----------------------------------------------------------------------------- the verdict (pure)
def _xy(xyz: Optional[Sequence[float]]) -> tuple[Optional[float], Optional[float]]:
    if not xyz:
        return None, None
    s = sum(float(v) for v in xyz)
    return (float(xyz[0]) / s, float(xyz[1]) / s) if s > 0 else (None, None)


_RANK = {"pass": 0, "undecided": 1, "fail": 2}
CHROMA_ALPHA = 0.05         # family-wise false-alarm rate of the chromaticity test over the whole run (Bonferroni)


def t_two_sided_p(t: float, nu: int) -> float:
    """P(|T| >= |t|) for Student's t with integer ``nu`` >= 1 degrees of freedom — the closed forms of Abramowitz &
    Stegun 26.7.3 (odd nu) / 26.7.4 (even nu); no scipy."""
    t = abs(float(t))
    if math.isinf(t):
        return 0.0
    nu = int(nu)
    th = math.atan(t / math.sqrt(nu))
    c2, s = math.cos(th) ** 2, math.sin(th)
    if nu % 2 == 1:
        acc, term = 0.0, 1.0
        if nu > 1:
            acc, term = 1.0, 1.0
            for k in range(1, (nu - 3) // 2 + 1):
                term *= c2 * (2 * k) / (2 * k + 1)
                acc += term
            a = (2.0 / math.pi) * (th + s * math.cos(th) * acc)
        else:
            a = 2.0 * th / math.pi
    else:
        acc, term = 1.0, 1.0
        for k in range(1, (nu - 2) // 2 + 1):
            term *= c2 * (2 * k - 1) / (2 * k)
            acc += term
        a = s * acc
    return max(0.0, min(1.0, 1.0 - a))


def t_critical(p: float, nu: int) -> float:
    """The |t| whose two-sided p-value is ``p`` (bisection on :func:`t_two_sided_p`)."""
    lo, hi = 0.0, 1.0
    while t_two_sided_p(hi, nu) > p and hi < 1e7:
        hi *= 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_two_sided_p(mid, nu) > p:
            lo = mid
        else:
            hi = mid
    return hi


def reference_xy_repeatability(ref_rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """How much a grey's chromaticity moves from one reference leg to the next, drift included: each interior
    reference read predicted by linear interpolation in time between its neighbours (leave-one-out), the residuals'
    RMS in x and in y, and their count (= the degrees of freedom of that estimate).

    Why this is the null distribution of a rate's shift: a rate read sits between two reference reads exactly as an
    interior reference read sits between its neighbours, so under "no refresh effect" its shift against the
    interpolated reference has the same make-up (its own read noise + both neighbours' noise weighted by the
    interpolation + the drift the interpolation misses). The LOO span is two bracket gaps, so the drift part is if
    anything overstated: the test errs towards passing a real shift, never towards failing a repeatable one."""
    s = sorted((r for r in ref_rows if r.get("xyz") and _xy(r["xyz"])[0] is not None), key=lambda r: float(r["t_s"]))
    ex, ey = [], []
    for k in range(1, len(s) - 1):
        a, m, b = s[k - 1], s[k], s[k + 1]
        ta, tm, tb = float(a["t_s"]), float(m["t_s"]), float(b["t_s"])
        if tb <= ta:
            continue
        w = (tm - ta) / (tb - ta)
        (xa, ya), (xm, ym), (xb, yb) = _xy(a["xyz"]), _xy(m["xyz"]), _xy(b["xyz"])
        ex.append(xm - (xa + (xb - xa) * w))
        ey.append(ym - (ya + (yb - ya) * w))
    rms = (lambda v: math.sqrt(sum(e * e for e in v) / len(v)) if v else None)
    return {"n_ref": len(s), "nu": len(ex), "rms_x": rms(ex), "rms_y": rms(ey), "residuals_x": ex, "residuals_y": ey}


def chroma_test(dx: Optional[float], dy: Optional[float], n_reads: int, rep: dict[str, Any], p_crit: float
                ) -> dict[str, Any]:
    """The combined (mean over ``n_reads`` passes) shift against the reference legs' own repeatability: t = shift /
    (rms_loo / sqrt(n)), Student t with nu = the number of LOO residuals, two-sided; it passes when p >= ``p_crit``
    (the run's Bonferroni share of :data:`CHROMA_ALPHA`)."""
    nu = int(rep.get("nu") or 0)
    if dx is None or dy is None or nu < 2 or n_reads < 1:
        return {"ok": None, "why": f"too few reference legs for a repeatability estimate (nu {nu} < 2)"}
    out: dict[str, Any] = {"nu": nu, "n_reads": n_reads, "p_crit": p_crit}
    ok = True
    for name, d, rms in (("x", dx, rep["rms_x"]), ("y", dy, rep["rms_y"])):
        se = (rms or 0.0) / math.sqrt(n_reads)
        if se <= 0.0:                       # a noiseless reference (simulation): only an exact match passes
            t, p = (0.0, 1.0) if abs(d) <= 1e-12 else (math.inf, 0.0)
            crit = 0.0
        else:
            t = d / se
            p = t_two_sided_p(t, nu)
            crit = t_critical(p_crit, nu) * se
        out[name] = {"shift": d, "se": se, "t": t, "p": p, "critical_shift": crit}
        ok = ok and p >= p_crit
    out["ok"] = ok
    return out


# The PRACTICAL floor of the chromaticity test: a shift the reference legs prove real can still be far below what
# anyone can see when the reads are very repeatable (the statistical tolerance then collapses to ~1e-4 in xy).
# Source: ITU-R BT.2124-0 (2019) normative Annex 1 - dE_ITP 1 is one just noticeable difference "in the most critical
# adaptation state" (standard, conservative; docs/practical-thresholds.md). A display's own between-session white
# repeatability would be the better floor (a shift smaller than what the display does between sessions is not the
# trim's); the DIP has no such section yet - when it does, its larger value should be used here.
PRACTICAL_CHROMA_ITP = 1.0


def practical_chroma(dx: Optional[float], dy: Optional[float], base_xy: Optional[tuple[float, float]],
                     light_nits: Optional[float], *, floor: float = PRACTICAL_CHROMA_ITP) -> dict[str, Any]:
    """The chromaticity shift (dx, dy) of a grey at ``base_xy`` and ``light_nits`` (absolute) as BT.2124 dE_ITP,
    against the practical floor. ``below`` True = statistically real or not, it is below one JND."""
    if dx is None or dy is None or base_xy is None or not light_nits or light_nits <= 0:
        return {"de_itp": None, "floor": floor, "below": None, "why": "no base chromaticity or light"}
    from .colormath import xy_to_XYZ
    from .content_score import xyz_to_itp
    x0, y0 = base_xy
    a, b = xyz_to_itp([xy_to_XYZ(x0, y0, light_nits), xy_to_XYZ(x0 + dx, y0 + dy, light_nits)])
    de = 720.0 * math.sqrt(sum((float(a[k]) - float(b[k])) ** 2 for k in range(3)))
    return {"de_itp": de, "floor": floor, "below": de < floor,
            "source": "ITU-R BT.2124 Annex 1: dE_ITP 1 = one just noticeable difference (the DIP holds no "
                      "between-session white repeatability for this display yet)"}


def link_code_pct(light_nits: Optional[float], link_bpc: Optional[int]) -> Optional[float]:
    """One code of a ``link_bpc`` PQ link at ``light_nits``, in % of that light (the step to the next code up)."""
    if not light_nits or light_nits <= 0 or not link_bpc:
        return None
    from ._pq import eotf_norm, oetf_norm
    m = float((1 << int(link_bpc)) - 1)
    c = oetf_norm(min(light_nits, 10000.0) / 10000.0) * m
    lo, hi = eotf_norm(c / m), eotf_norm(min(c + 1.0, m) / m)
    return (hi / lo - 1.0) * 100.0 if lo > 0 else None


def analyze_verify(rows: Sequence[dict[str, Any]], *, reference: str, lum_tol: float = 0.003,
                   trim_checks: Sequence[dict[str, Any]] = (), anomalies: Sequence[dict[str, Any]] = (),
                   status: str = "ok", stack: str = "as_viewed", link_bpc: Optional[int] = None) -> dict[str, Any]:
    """The verdict from the run's rows (``trim_rows.jsonl``). Pure; see the module docstring for the rule.
    ``stack`` = what was measured (``as_viewed`` / ``mhc_only``); ``link_bpc`` = the link's bits per channel (one link
    code in % of light is reported per grey; in ``mhc_only`` — no DesktopLUT pass, so no dither — a light miss inside one
    link code is "not resolvable without dither", undecided, not fail)."""
    greys = [r for r in rows if r.get("kind") == "grey"]
    usable = [r for r in greys if not r.get("implausible")]
    ratios = rt.bracket_ratios(usable, reference)
    comb = rt.combine(ratios)
    rep = {p: reference_xy_repeatability(rt.series(usable, p, reference)) for p in sorted({r["patch"] for r in usable})}
    rates = sorted({str(r["rate"]) for r in greys if r.get("rate") != reference}, key=lambda k: -rt._rate_hz_nominal(k))
    n_chroma = sum(1 for rate in rates for p in (comb.get(rate) or {}))
    p_crit = CHROMA_ALPHA / max(1, 2 * n_chroma)
    out_rates: dict[str, Any] = {}
    for rate in rates:
        cells, why_fail, why_undecided, notes = {}, [], [], []
        rrows = [x for x in ratios if str(x.get("rate")) == rate]
        for patch in sorted({str(x["patch"]) for x in greys if str(x.get("rate")) == rate},
                            key=lambda p: min(float(x.get("nominal_nits") or 0) for x in greys if x["patch"] == p)):
            prow = [x for x in rrows if x["patch"] == patch]
            c = (comb.get(rate) or {}).get(patch)
            rp_ = rep.get(patch) or {}
            singles = [x for x in prow if x.get("ratio") is not None]
            # every read of this grey at this rate, also those bracket_ratios left out (no light, implausible): a read
            # without a ratio is never dropped silently — it makes the rate undecided, named here
            taken = [x for x in greys if str(x.get("rate")) == rate and x["patch"] == patch]
            used = {(x.get("leg"), x.get("t_s")) for x in singles}
            lost = [{"leg": x.get("leg"), "pass": x.get("pass"), "t_s": x.get("t_s"),
                     "why": ("implausible: " + str(x["implausible"])) if x.get("implausible") else
                            ("no reading" if x.get("y") is None else "no reference read on both sides")}
                    for x in taken if (x.get("leg"), x.get("t_s")) not in used]
            cell: dict[str, Any] = {"nominal_nits": next((x.get("nominal_nits") for x in taken), None),
                                    "reads": len(taken), "bracketed": len(singles), "unbracketed": lost,
                                    "ratios": [round(x["ratio"], 6) for x in singles],
                                    "unsettled": sum(1 for x in prow if x.get("settled") is False),
                                    "reference_repeatability": {k: rp_.get(k) for k in ("n_ref", "nu", "rms_x", "rms_y")}}
            if c is None or not singles:
                cell["verdict"] = "undecided"
                cell["why"] = "no read bracketed by reference reads on both sides"
                why_undecided.append(f"{patch}: {cell['why']}")
                cells[patch] = cell
                continue
            ratio = math.exp(c["ln_ratio"])
            cell.update({"ratio": ratio, "pct": (ratio - 1.0) * 100.0, "u_pct": c["u"] * 100.0,
                         "dx": c.get("dx"), "dy": c.get("dy")})
            lum_ok = abs(ratio - 1.0) <= lum_tol and all(abs(x["ratio"] - 1.0) <= lum_tol for x in singles)
            ct = chroma_test(c.get("dx"), c.get("dy"), len([x for x in singles if x.get("dx") is not None]), rp_, p_crit)
            chroma_ok = ct.get("ok")
            refs = [_xy(x.get("xyz")) for x in rt.series(usable, patch, reference)]
            refs = [r for r in refs if r[0] is not None]
            base_xy = (sum(r[0] for r in refs) / len(refs), sum(r[1] for r in refs) / len(refs)) if refs else None
            practical = practical_chroma(c.get("dx"), c.get("dy"), base_xy,
                                         c.get("y60") or cell.get("nominal_nits"))
            ct["practical"] = practical
            # statistically real but below the practical floor: reported beside the statistical verdict, never
            # silently - the shift is named, with both numbers, in the rate's notes
            below_floor = chroma_ok is False and practical.get("below") is True
            code_pct = link_code_pct(c.get("y60"), link_bpc)
            worst_pct = max([abs(cell["pct"])] + [abs(x["ratio"] - 1.0) * 100.0 for x in singles])
            below_code = code_pct is not None and worst_pct < code_pct
            cell.update(lum_ok=lum_ok, chroma_ok=chroma_ok, chroma=ct, link_code_pct=code_pct,
                        residual_below_one_link_code=below_code if code_pct is not None else None)
            if not lum_ok and stack == "mhc_only" and below_code:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: light {cell['pct']:+.3f} % (worst single {worst_pct:.3f} %) is outside "
                                     f"+/-{lum_tol * 100:g} % but below one {link_bpc}-bit link code here "
                                     f"({code_pct:.3f} %): not resolvable without dither (mhc_only stack: no "
                                     "DesktopLUT pass dithers the MHC output)")
            elif not lum_ok:
                cell["verdict"] = "fail"
                why_fail.append(f"{patch}: light {cell['pct']:+.3f} % (single reads {[round((v - 1) * 100, 3) for v in cell['ratios']]} %) "
                                f"outside +/-{lum_tol * 100:g} %"
                                + (f" (one {link_bpc}-bit link code = {code_pct:.3f} %)" if code_pct is not None else ""))
            elif lost:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: {len(lost)} of {len(taken)} read(s) without a bracketed ratio: {lost}")
            elif below_floor:
                cell["verdict"] = "pass"
                cell["chroma_practical_pass"] = True
                notes.append(f"{patch}: chromaticity shift dx {ct['x']['shift']:+.5f} dy {ct['y']['shift']:+.5f} is "
                             f"statistically real (p {min(ct['x']['p'], ct['y']['p']):.2g} < {p_crit:.2g}) but "
                             f"{practical['de_itp']:.2f} dE_ITP at {c.get('y60') or cell.get('nominal_nits'):.1f} nit, "
                             f"below the practical floor {practical['floor']:g} (one JND, BT.2124): practical pass")
            elif chroma_ok is False and stack == "mhc_only" and code_pct is not None:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: chromaticity shift dx {ct['x']['shift']:+.5f} dy {ct['y']['shift']:+.5f} is "
                                     f"real against the reference legs' repeatability (p {min(ct['x']['p'], ct['y']['p']):.2g}) "
                                     f"but the mhc_only stack has no dither: each channel lands on whole {link_bpc}-bit "
                                     f"link codes ({code_pct:.3f} % of light each here), which moves the chromaticity "
                                     "by itself — not resolvable without dither")
            elif chroma_ok is False:
                cell["verdict"] = "fail"
                why_fail.append(f"{patch}: chromaticity shift dx {ct['x']['shift']:+.5f} (p {ct['x']['p']:.2g}, critical "
                                f"{ct['x']['critical_shift']:.5f}), dy {ct['y']['shift']:+.5f} (p {ct['y']['p']:.2g}, "
                                f"critical {ct['y']['critical_shift']:.5f}) beyond the reference legs' own repeatability "
                                f"(Student t, nu {ct['nu']}, Bonferroni p < {p_crit:.2g})")
            elif chroma_ok is None:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: {ct.get('why')}")
            elif cell["unsettled"]:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: {cell['unsettled']} unsettled read(s)")
            else:
                cell["verdict"] = "pass"
            cells[patch] = cell
        checks = [t for t in trim_checks if str(t.get("rate")) == rate]
        not_applied = [t for t in checks if not t.get("ok")]
        if not checks:
            why_undecided.append("the trim state was never read back at this rate")
        if not_applied:
            why_fail.append(f"the trim variant for {rate} Hz was not on screen on {len(not_applied)} of {len(checks)} "
                            f"leg(s): {[{k: t.get(k) for k in ('state', 'trim_rate', 'applied', 'waiting', 'rule', 'why', 'active_stamp', 'stamp') if t.get(k) is not None} for t in not_applied]}")
        verdict = "fail" if why_fail else ("undecided" if why_undecided else "pass")
        out_rates[rate] = {"verdict": verdict, "greys": cells, "fail": why_fail, "undecided": why_undecided,
                           "practical_notes": notes,
                           "trim_checks": len(checks), "trim_not_applied": len(not_applied),
                           "trim_rules": sorted({str(t.get("rule")) for t in checks if t.get("rule")}),
                           "resolved": sorted({str(t.get("resolved")) for t in checks if t.get("resolved")})}
    ref_checks = [t for t in trim_checks if str(t.get("rate")) == reference]
    run_undecided = []
    if status != "ok":
        run_undecided.append(f"the run ended {status!r}")
    if anomalies:
        run_undecided.append(f"{len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'} recorded: judge them")
    if any(not t.get("ok") for t in ref_checks):
        run_undecided.append("the reference rate's trim state was not 'reference' on every reference leg")
    if not rates:
        run_undecided.append("no rate was measured")
    worst = max([v["verdict"] for v in out_rates.values()] or ["undecided"], key=lambda v: _RANK[v])
    if run_undecided and worst == "pass":
        worst = "undecided"
    return {"verdict": worst, "stack": stack, "link_bpc": link_bpc,
            "rule": {"luminance": f"|ratio - 1| <= {lum_tol * 100:g} % (combined and every single read), "
                                  "time-bracketed against the reference"
                                  + ("; in mhc_only a miss below one link code is 'not resolvable without dither' "
                                     "(undecided)" if stack == "mhc_only" else ""),
                     "chromaticity": (f"the combined shift (mean over the passes) in x and in y against the grey's own "
                                      f"reference-leg repeatability: leave-one-out interpolation residuals of the "
                                      f"reference reads (drift included), t = shift / (rms / sqrt(n)), Student t with nu = "
                                      f"their count; a shift fails when its two-sided p < {CHROMA_ALPHA:g} / "
                                      f"{max(1, 2 * n_chroma)} tests (Bonferroni: <= {CHROMA_ALPHA:g} chance of any false "
                                      "fail in the run)"),
                     "chromaticity_p_crit": p_crit,
                     "trim": "state.get refresh_trim reads the rate's variant on screen on every leg (stamp rule)"},
            "reference": reference, "rates": out_rates, "run_undecided": run_undecided,
            "reference_repeatability": {p: {k: v.get(k) for k in ("n_ref", "nu", "rms_x", "rms_y")} for p, v in rep.items()},
            "reference_trim_checks": len(ref_checks),
            "unbracketed": [{k: x.get(k) for k in ("leg", "rate", "patch")} for x in ratios if x.get("unbracketed")]}


def digest_lines(res: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rate, v in res["rates"].items():
        out.append({"rate": rate, "verdict": v["verdict"],
                    "pct_by_grey": {p: (round(c["pct"], 3) if c.get("pct") is not None else None)
                                    for p, c in v["greys"].items()},
                    "link_code_pct_by_grey": {p: (round(c["link_code_pct"], 3) if c.get("link_code_pct") is not None
                                                  else None) for p, c in v["greys"].items()},
                    "fail": v["fail"], "undecided": v["undecided"]})
    return out


# ----------------------------------------------------------------------------- simulation (offline)
def sim_trim_model(api: Any, hardware_id: str, *, rates: Sequence[str] = ("47.952", "50"),
                   same_as: Optional[dict[str, str]] = None, no_data: bool = False, stuck: bool = False,
                   no_gray: bool = False, stampless: bool = False,
                   reference: str = "60") -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A model of DesktopLUT's ``state.get`` ``refresh_trim`` (desktoplut_ipc_server.cpp RefreshTrimJson, refresh_trim.cpp
    ResolveRefreshTrim / the applied rule) for the mock: follows the SimDisplayApi's live rate. ``rates`` have their
    own curves, ``same_as`` maps a rate to another (``{"48": "47.952", "59.94": "60"}``; ending at the reference = no
    trim, state ``reference``). Faults: ``stuck`` = the variant never follows the rate (the reference's stays on screen:
    ``applied`` false); ``no_gray`` = the stage bypass PERM_NOGRAY: ``state`` ``trim`` and ``applied`` TRUE with an empty
    ``active_stamp`` (refresh_trim.cpp:481-488); ``stampless`` = a reply without the wanted ``stamp``."""
    from . import refresh_mode as rm
    same = dict(same_as if same_as is not None else {"48": "47.952"})
    lines = [f"{r} Hz trim (3 levels)" for r in rates] + [f"{k} Hz = {v}" for k, v in same.items()]   # RateLines
    have = dip_rates(lines)

    def model(state: dict[str, Any]) -> dict[str, Any]:
        lay = ((state.get("layers") or {}).get("0:HDR") or {})
        on = bool(lay.get("refresh_trim"))
        d = rm.find_display(api.snapshot(), hardware_id)
        hz = rm.active_hz(d)
        key = next((k for k, (n, dd) in rm.RATES.items() if abs(n / dd - (hz or 0)) < 1e-6), None)
        e: dict[str, Any] = {"enabled": on, "refresh_hz": hz, "refresh_rational": list(d["active"]["vsync"]),
                             "waiting": None, "stamp": None, "active_stamp": ""}
        dip = {"record": "synthetic:HDR", "rates": lines, "stale": False}
        if no_data:
            e.update(state="no_section", detail="the DIP record has no refresh_response", rate=None, applied=False,
                     installed=False, dip=None)
            return {"0:HDR": e}
        target, _ = resolve_rate(key or "", have, reference)
        if not on:
            e.update(state="off", detail="switched off", rate=None, applied=True, installed=True, dip=dip)
        elif target == "reference":
            e.update(state="reference", detail="", rate=None, applied=True, installed=True, dip=dip)
        elif target is None:
            e.update(state="unmeasured", detail=f"{key} Hz is not measured", rate=None, applied=True, installed=True,
                     dip=dip)
        else:
            want = f"rt-{target}"
            active = "" if (stuck or no_gray) else want
            e.update(state="trim", detail="", rate=target, installed=True, dip=dip,
                     stamp=None if stampless else want, active_stamp=active,
                     applied=True if no_gray else active == want)
        return {"0:HDR": e}
    return model


def sim_effect(nits: float) -> float:
    """The simulated panel's own response at 47.952 Hz (ln of the light ratio; roughly the owner's PA32)."""
    return 0.034 - 0.008 * math.log10(max(nits, 0.5) / 0.5)


def sim_refresh_gain(server: Any, *, residual: float = 0.0, link_bpc: int = 10,
                     effect: Callable[[float], float] = sim_effect) -> Callable[[float, float], float]:
    """``ProbeSimPanel.refresh_gain`` for the verify simulation: the panel's response R at the rate (linear in frame
    period), the trim T = R^-(1 - residual) when the mock's trim model says a trimmed variant is on screen, and —
    when the 3D LUT is bypassed (the ``mhc_only`` stack: no DesktopLUT pass, so no dither) — the MHC output n·T
    rounded per channel to the ``link_bpc``-bit PQ link. As viewed (the 3D LUT in the path) the hook's dither keeps
    the fractional code on average: no rounding."""
    from ._pq import eotf_norm, oetf_norm
    m = float((1 << int(link_bpc)) - 1)

    def q(nits: float) -> float:
        return 10000.0 * eotf_norm(round(oetf_norm(min(max(nits, 0.0), 10000.0) / 10000.0) * m) / m)

    def gain(h: float, nits: float) -> float:
        x = (1000.0 / h - 1000.0 / 60.0) / (1000.0 / (48000 / 1001) - 1000.0 / 60.0)
        r = math.exp(effect(nits) * x)
        lay = dict(server.state.layers.get("0:HDR", {}))
        st = (server.refresh_trim_model({"layers": {"0:HDR": lay}}) if server.refresh_trim_model else {}).get("0:HDR", {})
        t = r ** -(1.0 - residual) if (st.get("state") == "trim" and st.get("active_stamp")) else 1.0
        out = nits * t
        if lay.get("bypass_3dlut"):
            out = q(out)
        return r * out / nits if nits > 0 else r
    return gain


def _simulate(out: Path, *, residual_pct: float, trim_off: bool, no_data: bool, stuck: bool, seed: int,
              verify_stack: str = "as_viewed") -> dict[str, Any]:
    import datetime

    from . import calibration_profile as cp
    from . import refresh_mode as rm
    from . import refresh_probe as rp
    from .adjudication import AutoAdjudicator
    from .calibrate import Calibration
    from .controller import CalibrationController
    from .dip import DisplayInstrumentProfile
    from .probe_sim import ProbeSimPanel, VirtualClock
    from .runs import create_run

    import os

    from .paths import RUNS_DIR_ENV
    # a simulated run must never take over the real runs/active.json (DesktopLUT's DLC tab reads it)
    os.environ[RUNS_DIR_ENV] = str((out / "_runs").resolve())
    run_dir = (out / f"refresh_verify_sim_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}").resolve()
    ctx = create_run("HDR", display="synthetic", run_dir=run_dir)
    ctrl = CalibrationController.mock()
    ctrl.set_hdr(0, True)
    api = rp.sim_desk_for_mock()
    server = ctrl.client.transport.server
    server.refresh_trim_model = sim_trim_model(api, "SIM0000", no_data=no_data, stuck=stuck)
    if not trim_off:
        ctrl.call("layers.set", {"monitor": 0, "mode": "HDR", "refresh_trim": True})

    def hz() -> Optional[float]:
        return rm.active_hz(rm.find_display(api.snapshot(), "SIM0000"))

    # an as-viewed stack has a cube in the path (the hook dithers); mhc_only bypasses it (the sim then rounds to the link)
    from .simulation import write_identity_cube
    ctrl.set_3dlut(0, "HDR", str(write_identity_cube(run_dir / "sim_daily.cube", size=17)))
    gain = sim_refresh_gain(server, residual=residual_pct / 100.0)
    panel = ProbeSimPanel(clock=VirtualClock(), refresh_gain=gain, hz_fn=hz, noise_rel=0.0002, seed=seed)
    cal = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(out / "results")), monitor=0, mode="HDR",
                      controller=ctrl, measure=panel, adjudicator=AutoAdjudicator(), bit_depth=10)
    cal.display_api = api
    cal.probe_spec = {"purpose": PURPOSE, "rates": ["47.952", "50"], "reference": "60", "colours": False,
                      "levels_nits": [0.5, 10.0, 100.0, 500.0], "verify_stack": verify_stack}
    cal._dip_store().record(DisplayInstrumentProfile(display=cal.display.name, mode="HDR", made="2026-11-01",
                                                     native_white_nits=1000.0))
    res = cal.run("refresh-response")
    return {"status": res.status, "run": str(run_dir), "digest": res.digest,
            "display_rate_after": list(rm.find_display(api.snapshot(), "SIM0000")["active"]["vsync"]),
            "switch_calls": len(api.calls), "virtual_minutes": round((panel.probe_clock.now() - panel.t0) / 60.0, 1),
            "trim_layer_after": server.state.layers.get("0:HDR", {}).get("refresh_trim")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dlc.refresh_verify",
                                 description="REFRESH TRIM verify (HDR). The live run is `python -m dlc.calibrate "
                                             "--flow refresh-response --mode HDR --probe-spec <spec with "
                                             "\"purpose\": \"verify\">` (spec: python -m dlc.refresh_probe plan "
                                             "--purpose verify --out <file>). This CLI only simulates.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("simulate", help="the whole flow in-process on the mock (no pipe, no meter, no mode switch)")
    sp.add_argument("--out", required=True)
    sp.add_argument("--residual-pct", type=float, default=0.0, dest="residual_pct",
                    help="the share of the panel's refresh response the trim leaves (0 = a perfect trim)")
    sp.add_argument("--trim-off", action="store_true", dest="trim_off", help="REFRESH TRIM off (expect a refusal)")
    sp.add_argument("--no-data", action="store_true", dest="no_data", help="no trim data (expect a refusal)")
    sp.add_argument("--stuck", action="store_true", help="the trim variant never follows the rate (expect fail)")
    sp.add_argument("--seed", type=int, default=5)
    sp.add_argument("--verify-stack", choices=("as_viewed", "mhc_only"), default="as_viewed", dest="verify_stack",
                    help="mhc_only = the 3D LUT bypassed: the sim rounds the MHC output to the 10-bit link (no dither)")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rep = _simulate(out, residual_pct=args.residual_pct, trim_off=args.trim_off, no_data=args.no_data,
                    stuck=args.stuck, seed=args.seed, verify_stack=args.verify_stack)
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
