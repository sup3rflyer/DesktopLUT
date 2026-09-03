"""ONE-OFF HW probe A (Claude, 2026-09-03): does the NEUTRAL (enter-neutral / Argyll Rec2020 dummy)
state clip the PA32UCXR's top end while an MHC2-profile state does not?

Background (docs/pa32ucxr-plan-2026-09-03.md item 1): the raw ramp in enter-neutral read a flat
~1757 nits from cv 868 up (cv 837 = 1708, erratic chroma 950-1700 nits) while the MHC state at a
higher drive read ~1826 nits clean. Every raw-derived MHC quantity is suspect until this is settled.

States read in ONE sitting (same patches, 3 reads each, a 92-nit reference before/after each block):
  A   applied   : today's stack (MHC ICM + 3D cube) - baseline, and the restoration check at the end
  N   neutral   : exactly what a run's enter-neutral does. NOTE (C++ review 2026-09-03): DoEnterNeutral
                  does NOT associate the Argyll dummy - it removes the MHC ICM and clears the layers;
                  'raw' is therefore the UN-PROFILED state (no Advanced-Color profile at all).
  I0  identity  : set_primaries(native) + set_white(D65): the C++ hardcodes the SOURCE white to D65
                  (mhc_icc.cpp:542), so src == display == (native primaries, D65) -> matrix == I,
                  no 1D LUT. A profile IS associated, the colour path is identity, default metadata.
  L   ident+lumi: I0 + an identity 1024-point 1D .cube via set_base_lut(peak_nits=<run cube peak>) so
                  the ICM carries the luminance metadata (lumi/MaxCLL) a run's ICM carries.
  M   matrix    : set_primaries(native) + set_white(native white) = the run's MATRIX state, the small
                  native->D65 diagonal gain (NOT identity - the mistake the first draft made).
  N2  neutral   : repeated at the end (thermal control for N)
Acceptance signature for 'clip reproduced' (from the 03:xx raw data): a razor-flat top (<3 nits across
cv 868..1023) WITH chroma locked to +-0.0002 x, versus the wandering 0.29-0.31 x below it - a digital
ceiling, not a thermal droop.
Decision table:
  N clipped, I0 unclipped            -> a profile association itself lifts the clip (Windows-side:
                                        SDR-white/MaxTML/composition clamp without a profile).
  N clipped, I0 clipped, L unclipped -> the luminance METADATA is the key (panel PQ tone-map / OS).
  N, I0, L clipped, M unclipped      -> the matrix gain matters (drive headroom), not the profile.
  everything clipped                 -> not a profile effect (OS-global, thermal/ABL, meter); N2 == N?
  everything unclipped               -> the 03:xx clip was transient (thermal/ABL); repeat later.
  N2 != N beyond noise               -> thermal drift over the sitting; trust only close-in-time pairs.
  applied2 != applied on ANY patch   -> restoration incomplete; stop and re-verify before anything else.
Confounds the probe does NOT separate: ICC metadata vs the DXGI HDR10 infoframe the panel sees, and a
persisted MaxTML/advanced-colour override (user-toggled in the DesktopLUT GUI, untouched by
enter-neutral): the OWNER confirms the GUI MaxTML state before the sitting; it is not pipe-queryable.
windows.query_profiles is a STUB (always available:false) - the meter is the only association evidence,
so every block starts with a throwaway 'prime' read after the settle sleep.

Run (daemon already up on 28930 fullscreen on mon 0, i1D3 on the panel):
    PYTHONPATH=src python agent_probe_deployed_state.py
Mutates display state; restores the applied stack via exit_calibration(restore_snapshot=True) in
finally and re-reads the baseline patches to prove it. Writes results to runs/probes/<timestamp>/.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dlc import calibration_profile as cp
from dlc.argyll import Argyll, SpotreadRequest
from dlc.calibrate import active_correction, correction_store_path
from dlc.controller import CalibrationController
from dlc.correction_store import CorrectionStore
from dlc.measure_loop import MeasurePatch, SocketPresenter, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port
from dlc.mhc_cube import write_1d_cube
from dlc.profiles import default_dummy_icc

MON, MODE, BIT = 0, "HDR", 10
HOST, PORT = "127.0.0.1", 28930
RUN_PARAMS = Path("runs/20260903_030752_833969_hdr_asus_proart_pa32ucxr/generated/mhc_params_hdr.json")
OUT = Path("runs/probes") / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_deployed_state")

REF = ("ref512", (512, 512, 512))          # the run's drift reference (~92 nits)
PATCHES = [
    ("g785", (785, 785, 785)),
    ("g837", (837, 837, 837)),             # the peak code (1835-nit request)
    ("g868", (868, 868, 868)),
    ("g1023", (1023, 1023, 1023)),
    ("r837", (837, 0, 0)),
    ("b837", (0, 0, 837)),
]
READS = 3
SETTLE_S = 1.7                              # DIP bright settle ~1.66 s


def log(*a):
    print(*a, flush=True)


def xy(X):
    s = X[0] + X[1] + X[2]
    return (X[0] / s, X[1] / s) if s > 1e-9 else (0.0, 0.0)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    disp_name = profile.display_for(MON).name
    ccmx = active_correction(profile, store, disp_name)
    log(f"[setup] spotread port={port} ok={info.get('ok')} display={disp_name!r} ccmx={ccmx}")

    params = json.loads(RUN_PARAMS.read_text(encoding="utf-8"))
    prim = params["primaries"]
    wxy = (params["measured_white"]["x"], params["measured_white"]["y"])
    cube_peak = float(params["peak_chroma"]["cube_peak_nits"])
    log(f"[setup] identity basis: primaries={prim} white={wxy} cube_peak={cube_peak}")

    ctrl = CalibrationController.connect()
    status = ctrl.calibration_status()
    if status.get("active"):
        log(f"[ABORT] DesktopLUT is ALREADY in calibration mode ({status}). Re-entering would snapshot the "
            "already-cleared state as the restore target and the final restore would NOT bring back the "
            "applied stack. Resolve (state()/GUI Calibration control/restart DesktopLUT) before running.")
        return 1
    presenter = SocketPresenter(HOST, PORT, settle_seconds=SETTLE_S)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)
    results: list[dict] = []

    def read(state: str, label: str, cv, n: int = READS):
        sig = tuple(c / ((1 << BIT) - 1) for c in cv)
        rows = []
        for i in range(n):
            p = MeasurePatch(label=label, rgb=cv, signal=sig, role="measurement", bit_depth=BIT, seq=0)
            rd = measure(p)
            t = datetime.now().isoformat(timespec="milliseconds")
            if rd.xyz is None:
                log(f"   {state:10s} {label:6s} NO READ"); continue
            X, Y, Z = rd.xyz; x, y = xy(rd.xyz)
            rows.append({"state": state, "label": label, "cv": list(cv), "t": t, "Y": Y, "x": x, "y": y, "xyz": [X, Y, Z]})
            log(f"   {state:10s} {label:6s} cv={cv}  Y={Y:8.2f}  xy=({x:.4f},{y:.4f})  {t[11:23]}")
        results.extend(rows)
        return rows

    def block(state: str):
        log(f"[{state}] controller.state: {json.dumps({k: v for k, v in ctrl.state().items() if k in ('calibration_mode', 'mhc', 'runtime')}, default=str)[:600]}")
        try:
            # windows.query_profiles is a stub (HandleQueryProfiles: available=false) - logged for the record only
            log(f"[{state}] query_profiles: {json.dumps(ctrl.query_profiles(MON), default=str)[:300]}")
            mons = ctrl.query_monitors().get("monitors", [])
            log(f"[{state}] monitors: {[(m.get('index'), m.get('hdr_active'), m.get('color_space')) for m in mons]}")
        except Exception as exc:  # noqa: BLE001
            log(f"[{state}] query failed: {exc}")
        read("prime-" + state, "g837", (837, 837, 837), n=1)   # throwaway: association-landing check vs the block's g837
        read(state, *REF, n=2)
        for label, cv in PATCHES:
            read(state, label, cv)
        read(state, *REF, n=1)

    entered = False
    try:
        block("applied")
        # ---- N: the run's neutral state -------------------------------------------------
        dummy = str(default_dummy_icc(MODE).path)
        log(f"[neutral] enter_neutral dummy={dummy}")
        ctrl.enter_neutral(MON, MODE, dummy, reason="probe A deployed-state clip")
        entered = True
        time.sleep(3.0)
        block("neutral")
        # ---- I0: TRUE identity (native primaries + D65 white: src == display -> matrix I) ----
        ctrl.set_primaries(MON, MODE, prim)
        ctrl.set_white(MON, MODE, 0.3127, 0.3290)
        ctrl.apply_mhc(MON, MODE)
        time.sleep(4.0)
        block("identity0")
        # ---- L: identity + identity 1D cube carrying the run's peak metadata -------------
        n = 1024
        ident = [i / (n - 1) for i in range(n)]
        cube = OUT / "identity_1d.cube"
        write_1d_cube(cube, {"r": ident, "g": ident, "b": ident}, title="probe A identity 1D")
        ctrl.set_base_lut(MON, MODE, str(cube.resolve()), cube_peak)
        ctrl.apply_mhc(MON, MODE)
        time.sleep(4.0)
        block("ident+lumi")
        # ---- M: the run's MATRIX state (native primaries + measured native white, no cube) ----
        ctrl.remove_mhc(MON, MODE)
        time.sleep(3.0)
        ctrl.set_primaries(MON, MODE, prim)
        ctrl.set_white(MON, MODE, wxy[0], wxy[1])
        ctrl.apply_mhc(MON, MODE)
        time.sleep(4.0)
        block("matrix")
        # ---- N2: neutral again (thermal control) -----------------------------------------
        ctrl.remove_mhc(MON, MODE)
        time.sleep(3.0)
        block("neutral2")
    finally:
        try:
            if entered:
                for attempt in range(3):
                    try:
                        log(f"[restore] exit_calibration(restore_snapshot=True) attempt {attempt + 1}")
                        ctrl.exit_calibration(restore_snapshot=True)
                        break
                    except Exception as exc:  # noqa: BLE001
                        log(f"[restore] FAILED attempt {attempt + 1}: {exc}")
                        if attempt == 2:
                            log("[restore] *** GAVE UP. THE DISPLAY MAY BE LEFT NEUTRAL/UN-PROFILED. Owner: check "
                                "state(), toggle Calibration control in the GUI, or restart DesktopLUT. ***")
                        else:
                            time.sleep(2.0)
                time.sleep(4.0)
                block("applied2")
        finally:
            try:
                read("park", "park512", (512, 512, 512), n=1)
            except Exception:  # noqa: BLE001
                pass
            try:
                meter.close()
            except Exception:  # noqa: BLE001
                pass
            (OUT / "reads.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
            log(f"[done] {len(results)} reads -> {OUT / 'reads.json'}")

    # ---- summary -------------------------------------------------------------------------
    def med(state, label, key):
        v = sorted(r[key] for r in results if r["state"] == state and r["label"] == label)
        return v[len(v) // 2] if v else float("nan")
    states = ["applied", "neutral", "identity0", "ident+lumi", "matrix", "neutral2", "applied2"]
    log("\n[summary] median Y (nits) / x per state")
    log("  patch   " + "  ".join(f"{s:>16s}" for s in states))
    for label, _cv in [REF] + PATCHES:
        log(f"  {label:7s} " + "  ".join(f"{med(s, label, 'Y'):8.1f}/{med(s, label, 'x'):.4f}" for s in states))
    log("\n[signature] flat-top + chroma-lock per state (clipped = top spread < 3 nits and g1023 < 1790):")
    for st in states:
        y868, y1023, y837 = med(st, "g868", "Y"), med(st, "g1023", "Y"), med(st, "g837", "Y")
        xs = [med(st, l, "x") for l in ("g837", "g868", "g1023")]
        if all(v == v for v in (y868, y1023, y837)):
            clipped = abs(y1023 - y868) < 3.0 and y1023 < 1790.0
            log(f"  {st:11s} g837/g868/g1023 {y837:7.1f}/{y868:7.1f}/{y1023:7.1f}  x {xs[0]:.4f}/{xs[1]:.4f}/{xs[2]:.4f}"
                f"  clipped={'YES' if clipped else 'no'}")
    log("[restore-check] every patch, applied vs applied2:")
    for label, _cv in [REF] + PATCHES:
        a1, a2v = med("applied", label, "Y"), med("applied2", label, "Y")
        if a1 == a1 and a2v == a2v and a1 > 0:
            log(f"  {label:7s} Y {a1:8.2f} -> {a2v:8.2f} ({100 * (a2v - a1) / a1:+.2f}%)  x {med('applied', label, 'x'):.4f} -> {med('applied2', label, 'x'):.4f}")
    a, a2 = med("applied", "g837", "Y"), med("applied2", "g837", "Y")
    if a and a2:
        log(f"[restore-check] applied g837 before {a:.1f} after {a2:.1f} nits ({100 * (a2 - a) / a:+.2f}%); "
            f"x {med('applied', 'g837', 'x'):.4f} -> {med('applied2', 'g837', 'x'):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
