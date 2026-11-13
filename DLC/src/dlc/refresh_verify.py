"""REFRESH TRIM verify — does the trim DesktopLUT applies hold the light at another refresh rate? (HDR)

The refresh-response flow measures the panel with the trim OFF and records the response in the DIP; DesktopLUT then
bakes refresh-keyed HDR profile variants from it. This is the check of the result: the SAME flow and machinery
(``dlc.refresh_probe``: the display found by hardware id, one consent per switch away from the reference, the mode
switched temporarily and read back exactly, reference -> rate -> reference brackets in two mirrored passes, the exact
recorded mode and the stack put back on every ending, ``--abort`` / ``refresh_probe restore`` for a run that died),
run with the spec's ``"purpose": "verify"``. What differs:

* **The trim stays ON and is never touched.** The run refuses before anything changes when the display's REFRESH TRIM
  is off, when DesktopLUT has no trim data for the mode (no DLC folder / DIP / ``refresh_response`` section, or an
  invalid one), or when a rate to verify is not in the recorded response. It switches only the 3D LUT bypass (as the
  response measurement did); ``refresh_trim`` is never sent in a ``layers.set``, so no restore can change it either.
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
* chromaticity: the combined |dx| and |dy| (rate minus interpolated reference) within the spread of that grey's own
  reference reads over the run (max - min of x, of y). A spread is noise + drift; a deviation inside it is not
  distinguishable from the reference.

Verdict per rate: ``pass`` (every grey passes both, the trim read back applied on every leg of the rate), ``fail``
(a luminance miss, or the trim not applied), or ``undecided`` — a chromaticity miss with the luminance inside, an
unsettled read, a read with no bracketed ratio (no reference read on both sides, no light, implausible — never dropped
silently: named per grey in the result), or anomalies: not a deterministic yes/no, so the digest carries the numbers and the
overseer judges (nothing is applied or recorded either way). The run's verdict is the worst of its rates.

Simulation: ``python -m dlc.refresh_verify simulate --out DIR [--residual-pct P] [--trim-off] [--no-data]`` runs the
whole flow in-process on the mock DesktopLUT + ``refresh_mode.SimDisplayApi`` + ``probe_sim.ProbeSimPanel`` (no pipe,
no meter, no display mode is touched on this machine).
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


def analyze_verify(rows: Sequence[dict[str, Any]], *, reference: str, lum_tol: float = 0.003,
                   trim_checks: Sequence[dict[str, Any]] = (), anomalies: Sequence[dict[str, Any]] = (),
                   status: str = "ok") -> dict[str, Any]:
    """The verdict from the run's rows (``trim_rows.jsonl``). Pure; see the module docstring for the rule."""
    greys = [r for r in rows if r.get("kind") == "grey"]
    usable = [r for r in greys if not r.get("implausible")]
    ratios = rt.bracket_ratios(usable, reference)
    comb = rt.combine(ratios)
    spread: dict[str, dict[str, Any]] = {}
    for r in usable:
        if r.get("rate") != reference or not r.get("xyz"):
            continue
        x, y = _xy(r["xyz"])
        if x is None:
            continue
        s = spread.setdefault(r["patch"], {"x": [], "y": [], "Y": []})
        s["x"].append(x)
        s["y"].append(y)
        s["Y"].append(float(r["y"]))
    sp = {p: {"n": len(v["x"]), "x": max(v["x"]) - min(v["x"]), "y": max(v["y"]) - min(v["y"]),
              "Y_rel": (max(v["Y"]) - min(v["Y"])) / (sum(v["Y"]) / len(v["Y"]))} for p, v in spread.items()}
    rates = sorted({str(r["rate"]) for r in greys if r.get("rate") != reference}, key=lambda k: -rt._rate_hz_nominal(k))
    out_rates: dict[str, Any] = {}
    for rate in rates:
        cells, why_fail, why_undecided = {}, [], []
        rrows = [x for x in ratios if str(x.get("rate")) == rate]
        for patch in sorted({str(x["patch"]) for x in greys if str(x.get("rate")) == rate},
                            key=lambda p: min(float(x.get("nominal_nits") or 0) for x in greys if x["patch"] == p)):
            prow = [x for x in rrows if x["patch"] == patch]
            c = (comb.get(rate) or {}).get(patch)
            s = sp.get(patch) or {}
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
                                    "spread_ref": {k: s.get(k) for k in ("n", "x", "y", "Y_rel")}}
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
            sx, sy = s.get("x"), s.get("y")
            chroma_ok = None
            if c.get("dx") is not None and c.get("dy") is not None and sx is not None and (s.get("n") or 0) >= 2:
                eps = 1e-9                                  # float round-off only, not a tolerance
                chroma_ok = abs(c["dx"]) <= sx + eps and abs(c["dy"]) <= sy + eps
            cell.update(lum_ok=lum_ok, chroma_ok=chroma_ok)
            if not lum_ok:
                cell["verdict"] = "fail"
                why_fail.append(f"{patch}: light {cell['pct']:+.3f} % (single reads {[round((v - 1) * 100, 3) for v in cell['ratios']]} %) "
                                f"outside +/-{lum_tol * 100:g} %")
            elif lost:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: {len(lost)} of {len(taken)} read(s) without a bracketed ratio: {lost}")
            elif chroma_ok is False:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: chromaticity dx {c['dx']:+.5f} dy {c['dy']:+.5f} outside the reference "
                                     f"reads' own spread (x {sx:.5f}, y {sy:.5f}); the light is inside")
            elif chroma_ok is None:
                cell["verdict"] = "undecided"
                why_undecided.append(f"{patch}: no chromaticity spread (fewer than 2 reference reads)")
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
    return {"verdict": worst, "rule": {"luminance": f"|ratio - 1| <= {lum_tol * 100:g} % (combined and every single read), "
                                                    "time-bracketed against the reference",
                                       "chromaticity": "combined |dx|, |dy| <= the grey's own reference-read spread "
                                                       "(max - min over the run)",
                                       "trim": "state.get refresh_trim reads the rate's variant applied on every leg"},
            "reference": reference, "rates": out_rates, "run_undecided": run_undecided,
            "reference_spread": sp, "reference_trim_checks": len(ref_checks),
            "unbracketed": [{k: x.get(k) for k in ("leg", "rate", "patch")} for x in ratios if x.get("unbracketed")]}


def digest_lines(res: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rate, v in res["rates"].items():
        out.append({"rate": rate, "verdict": v["verdict"],
                    "pct_by_grey": {p: (round(c["pct"], 3) if c.get("pct") is not None else None)
                                    for p, c in v["greys"].items()},
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


def _simulate(out: Path, *, residual_pct: float, trim_off: bool, no_data: bool, stuck: bool, seed: int) -> dict[str, Any]:
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

    def effect(nits: float) -> float:           # the panel's own response at 47.952 Hz (roughly the owner's)
        return 0.034 - 0.008 * math.log10(max(nits, 0.5) / 0.5)

    def gain(h: float, nits: float) -> float:   # what is left after the trim: residual_pct of the response
        x = (1000.0 / h - 1000.0 / 60.0) / (1000.0 / (48000 / 1001) - 1000.0 / 60.0)
        trim_on = server.state.layers.get("0:HDR", {}).get("refresh_trim") and not stuck and not no_data
        left = (residual_pct / 100.0) if trim_on else 1.0
        return math.exp(effect(nits) * x * left)

    panel = ProbeSimPanel(clock=VirtualClock(), refresh_gain=gain, hz_fn=hz, noise_rel=0.0002, seed=seed)
    cal = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(out / "results")), monitor=0, mode="HDR",
                      controller=ctrl, measure=panel, adjudicator=AutoAdjudicator(), bit_depth=10)
    cal.display_api = api
    cal.probe_spec = {"purpose": PURPOSE, "rates": ["47.952", "50"], "reference": "60", "colours": False,
                      "levels_nits": [0.5, 10.0, 100.0, 500.0]}
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
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rep = _simulate(out, residual_pct=args.residual_pct, trim_off=args.trim_off, no_data=args.no_data,
                    stuck=args.stuck, seed=args.seed)
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
