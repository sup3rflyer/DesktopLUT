"""Dark-halo meter probe (2026-09-12 late): does the layer OVER-suppress near-black grey next to small bright strokes?

Owner's eye on a YouTube dark-theme page (≈ 0.5–1 nit grey, white text, bright side panel): with the layer ON a dark
band appears around the text and along the bright panel's edge — the corrected grey ends up DARKER than the far grey.
The target is the pixel's own flat-field level, so ON should equal the flat grey; anything below is over-correction.
This regime (grey < 5 nits, small strokes) was never in the fits (greys 5–40 nits, 200+ px windows).

RESULT 2026-09-12 (doc §33): gaps <= 60 px are inside the meter's real acceptance (unusable); 120-px ring over-corrected
+3.5...+7.8 % above flat at 1-20 nits (model ring 3-8 pp too deep); 240/480 px fine; at 0.5 nit the model has no
baseline (drive floor -> 0) and is wrong in sign - the owner's dark band around text on ~0.5-nit grey.

Frames (as-if-white nits, PNG PQ BT.2020 via fald_ab_frames.write_png): for each grey G in GREYS — flat G; a 40-px
wide × 600-px tall bar at BAR nits with its near edge GAP px LEFT of the meter x (gaps in GAPS); a "text" cluster of
five 8-px strokes (16-px pitch, 300 px tall) at BAR nits with its near edge 60 px left of the meter. Per frame the
meter reads OFF (layer off) / ID (layer on, debug 4) / ON (debug 0); each read re-loads the frame in mpv AFTER the
state change (the overlay path renders only on a new desktop frame). Model prediction at the spot = disc aperture
(fitted 57 px) mean of the forward model on the frame (OFF) and on the corrected frame (ON).
Metric per case: OFF/flat − 1 (the panel's raw halo at the spot), ON/flat − 1 (should be 0; < 0 = over-suppression),
model OFF/flat − 1 and ON/flat − 1 (what the model thinks), all at the SAME grey's measured flat.

State: refuses unless the stack audit passes (FALD_NATIVE=1 enters neutral + identity MHC; restored on exit).
Env: FALD_METER=x,y (1988,1120), FALD_OUT, FALD_GREYS="0.5,1,2,5,20", FALD_GAPS="20,60,120,240,480", FALD_BAR=600.
Usage: PYTHONPATH="src;." FALD_NATIVE=1 python fald_dark_halo_probe.py
"""
import json, os, sys, time
from pathlib import Path
import numpy as np
from dataclasses import replace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "results/fald_native_2026-09-11/sim"))
import agent_fald_leak_probe as P
import fald_sim_frames as F
import fald_ab_frames as AB
from fald_ramp_probe import MpvPresenter, SETTLE
from dlc.controller import CalibrationController
from dlc.measure_loop import MeasurePatch, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port
from dlc.argyll import Argyll, SpotreadRequest
import dlc.calibration_profile as cp
from dlc.calibrate import active_correction, correction_store_path
from dlc.correction_store import CorrectionStore
from dlc.fald.model import FaldModel
from dlc.fald.correct import load_fitted_params, correct_image

OUT = Path(os.environ.get("FALD_OUT", str(ROOT / "results/fald_native_2026-09-11/dark_halo")))
OUT.mkdir(parents=True, exist_ok=True)
W, H = 3840, 2160
MON = 0
GREYS = [float(v) for v in os.environ.get("FALD_GREYS", "0.5,1,2,5,20").split(",")]
GAPS = [int(v) for v in os.environ.get("FALD_GAPS", "20,60,120,240,480").split(",")]
BAR = float(os.environ.get("FALD_BAR", "600"))


def frame(mx, my, grey, kind, gap=0):
    img = np.full((3, H, W), grey, np.float64)
    if kind == "bar":
        x1 = mx - gap; x0 = x1 - 40
        img[:, my - 300:my + 300, x0:x1] = BAR
    elif kind == "text":
        x1 = mx - gap
        for k in range(5):
            xa = x1 - 16 * k - 8; img[:, my - 150:my + 150, xa:xa + 8] = BAR
    return img


def main():
    mx, my = (int(v) for v in os.environ.get("FALD_METER", "1988,1120").split(","))
    model = FaldModel(replace(load_fitted_params(F.PARAMS), scale=1)); p = model.p
    mask = model.aperture_mask((mx, my))
    specs = []
    for g in GREYS:
        specs.append((f"g{g:g}_flat", g, "flat", 0))
        for d in GAPS: specs.append((f"g{g:g}_bar{d}", g, "bar", d))
        specs.append((f"g{g:g}_text60", g, "text", 60))
    frames = []
    for name, g, kind, gap in specs:
        img = frame(mx, my, g, kind, gap)
        png = OUT / f"{name}.png"
        if not png.exists(): AB.write_png(png, img)
        y_off = model.forward_img(img)["y"].sum(axis=0)
        req = np.clip(correct_image(model, img, iters=2)["req"], 0, p.white_nits)
        y_on = model.forward_img(req)["y"].sum(axis=0)
        frames.append({"name": name, "grey": g, "kind": kind, "gap": gap, "png": str(png),
                       "model_off": float(y_off[mask].mean()), "model_on": float(y_on[mask].mean()),
                       "req_ratio_spot": float(req.sum(axis=0)[mask].mean() / max(img.sum(axis=0)[mask].mean(), 1e-9))})
    P.log(f"[halo] {len(frames)} frames; meter ({mx},{my}); bar {BAR:g} nits; greys {GREYS}; gaps {GAPS}")
    for f in frames:
        P.log(f"   model {f['name']:<14} OFF {f['model_off']:8.4f}  ON {f['model_on']:8.4f}  req/img at spot {f['req_ratio_spot']:.4f}")

    ctrl = CalibrationController.connect()
    native = os.environ.get("FALD_NATIVE") == "1"
    if native: P.enter_native(ctrl)
    layers = P.audit_or_refuse(ctrl, allow_stack=os.environ.get("FALD_ALLOW_STACK") == "1")
    if P.stack_layers(ctrl)[2].get("active"): raise SystemExit("[halo] DWM hook active")
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(MON).name)
    presenter = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")))
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)

    def set_state(on, debug):
        ctrl.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": debug})
        ctrl.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": on}); time.sleep(0.6)

    def read(label, png):
        presenter.pending = png            # presenter.show() re-loads the frame → a fresh overlay render
        rd = measure(MeasurePatch(label=label, rgb=(512, 512, 512), signal=(0.5, 0.5, 0.5), role="measurement", bit_depth=10, seq=0))
        return list(rd.xyz) if rd.ok and rd.xyz else None

    results = []
    try:
        for f in frames:
            r = {}
            for label, on, dbg in (("off", False, 0), ("id", True, 4), ("on", True, 0)):
                set_state(on, dbg); r[label] = read(f"{f['name']} {label}", f["png"])
            Y = lambda k: (r[k][1] if r.get(k) else float("nan"))
            results.append({**f, "xyz": r})
            P.log(f"   {f['name']:<14} OFF {Y('off'):8.4f}  ID {Y('id'):8.4f}  ON {Y('on'):8.4f} nits | ON/ID {Y('on')/Y('id'):.4f}  ID/OFF {Y('id')/Y('off'):.4f} | model ON/OFF {f['model_on']/max(f['model_off'],1e-9):.4f}")
            json.dump({"meter": [mx, my], "bar": BAR, "results": results}, open(OUT / "dark_halo_result.json", "w"), indent=1)
        # per grey: everything relative to that grey's measured flat (ID state = the overlay-path baseline)
        P.log("\n[halo] relative to the flat field of the same grey (ID baseline): OFF = raw panel halo, ON = corrected (target 0)")
        P.log(f"   {'case':<14} {'flat nits':>9} | {'OFF/flat':>9} {'model':>7} | {'ON/flat':>9} {'model':>7} | {'ON/ID':>7} {'model':>7}")
        for g in GREYS:
            flat = next((x for x in results if x["grey"] == g and x["kind"] == "flat"), None)
            if not flat or not flat["xyz"].get("id"): continue
            f_id = flat["xyz"]["id"][1]; f_off = flat["xyz"]["off"][1]; f_m = flat["model_off"]; f_mon = flat["model_on"]
            for x in results:
                if x["grey"] != g or x["kind"] == "flat" or not x["xyz"].get("on"): continue
                off, idv, on = x["xyz"]["off"][1], x["xyz"]["id"][1], x["xyz"]["on"][1]
                pm = lambda a, b: f"{100*(a/b-1):+6.1f}" if b > 1e-9 else "   n/a"      # model flat = 0 below the drive floor (0.5 nit)
                P.log(f"   {x['name']:<14} {f_id:9.4f} | {100*(off/f_off-1):+8.1f} % {pm(x['model_off'], f_m)} | {100*(on/f_id-1):+8.1f} % {pm(x['model_on'], f_mon)} | {100*(on/idv-1):+6.1f} {pm(x['model_on'], x['model_off'])}")
    finally:
        try: set_state(False, 0)
        except Exception: pass
        presenter.close()
        try: meter.close()
        except Exception: pass
        json.dump({"meter": [mx, my], "bar": BAR, "results": results}, open(OUT / "dark_halo_result.json", "w"), indent=1)
        P.log(f"[halo] saved {OUT / 'dark_halo_result.json'}")
        if native:
            try: ctrl.exit_calibration(restore_snapshot=True); P.log("[restore] stack restored")
            except Exception as exc: P.log(f"[restore] failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
