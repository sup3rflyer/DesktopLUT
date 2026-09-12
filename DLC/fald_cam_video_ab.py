"""Webcam A/B of the FALD layer on real video frames (2026-09-12, owner request).

mpv shows the ACES HDR test video paused at chapter frames (HDR passthrough, fullscreen on the ProArt);
per frame the C922 captures 48-frame stacks in three DesktopLUT states — OFF (layer off), ID (layer on,
debug mode 4 = identity passthrough: the overlay path without the correction) and ON (debug 0) — at two
exposures (A = highlights mostly unclipped, B = +2 stops for the dark surround where halos live), plus an
OFF repeat for drift. The frame the layer saw is dumped once per timestamp in a separate pass (`dumps`: runtime.fald_dump +
an mpv frame-step, because a paused video gives the layer no new frame to dump) and verified against the
video, so the Python model can predict OFF/ON panel output for the same frame.

Lens flare / veiling glare of the webcam is identical in all three states, so only DIFFERENCES between
states are judged: ON/ID ratio maps (the layer's effect through the real panel), ID/OFF (overlay-path
cost), contrast-stretched crops around the brightest blobs, and halo profiles across highlight edges,
next to the model's predicted ON/OFF ratio for the same frame.

State: refuses unless the stack audit passes (FALD_NATIVE=1 enters neutral + identity MHC, restored on exit).
Usage:  PYTHONPATH="src;." FALD_NATIVE=1 python fald_cam_video_ab.py capture [t1 t2 ...]
        PYTHONPATH="src;." python fald_cam_video_ab.py dumps [t1 t2 ...]
        PYTHONPATH="src;." python fald_cam_video_ab.py analyze [t1 t2 ...]
Env: FALD_OUT (results dir), FALD_MPV_SCREEN (0), FALD_VIDEO, FALD_STACK (48 frames).
"""
import json, os, sys, time
from pathlib import Path
import numpy as np, cv2
from dataclasses import replace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "results/fald_native_2026-09-11/sim"))

VIDEO = os.environ.get("FALD_VIDEO", r"M:\Media\Test_Videos\ACES HDR Test v4 - No Gamut Compress.mp4")
OUT = Path(os.environ.get("FALD_OUT", str(ROOT / "results/fald_native_2026-09-11/camera_ab")))
OUT.mkdir(parents=True, exist_ok=True)
W, H = 3840, 2160
CW, CH = 1920, 1080            # capture grid (camera native; ≈2 screen px per camera px after the warp)
MON = 0
NSTACK = int(os.environ.get("FALD_STACK", "48"))
# chapter midpoints (chapter k starts ≈ 5(k-1) s; contact sheet index → +2.5 s)
DEFAULT_TS = [2.5, 12.5, 72.5, 82.5, 87.5, 167.5, 262.5, 387.5, 537.5, 577.5, 632.5, 682.5]
STATES = (("off", False, 0), ("id", True, 4), ("on", True, 0), ("off2", False, 0))

BT709_TO_BT2020 = np.array([[0.6274040, 0.3292820, 0.0433136],
                            [0.0690970, 0.9195400, 0.0113612],
                            [0.0163916, 0.0880132, 0.8955950]])


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def tsname(t):
    return f"t{t:07.2f}".replace(".", "_")


# ----------------------------------------------------------------------------------------------- capture
def capture(ts):
    import agent_fald_leak_probe as P
    from fald_ramp_probe import MpvPresenter, SETTLE
    from fald_cam import Camera, find_panel_corners, warp
    from dlc.controller import CalibrationController
    import fald_ab_frames as AB

    ctrl = CalibrationController.connect()
    native = os.environ.get("FALD_NATIVE") == "1"
    if native: P.enter_native(ctrl)
    layers = P.audit_or_refuse(ctrl, allow_stack=os.environ.get("FALD_ALLOW_STACK") == "1")
    if P.stack_layers(ctrl)[2].get("active"):
        raise SystemExit("[ab] DWM hook active — overlay layer cannot run")

    def set_state(on, debug):
        ctrl.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": debug})
        ctrl.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": on})
        time.sleep(0.8)

    grey = OUT / "grey_frame.png"
    if not grey.exists(): AB.write_png(grey, np.full((3, H, W), 90.0))
    pres = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")), extra_args=("--hr-seek=yes", "--pause", "--audio=no"))
    cam = Camera()
    prev = json.load(open(OUT / "capture_meta.json")) if (OUT / "capture_meta.json").exists() else {}
    meta = {"video": VIDEO, "native": native, "layers_at_start": layers, "nstack": NSTACK, "frames": prev.get("frames", {})}
    try:
        set_state(False, 0)
        pres.cmd("loadfile", str(grey), "replace"); pres.wait_loaded(str(grey)); time.sleep(SETTLE + 1.0)
        cam.set_manual(exposure=-6, gain=0)
        corners = find_panel_corners(cam.grab(6))
        np.save(OUT / "panel_corners.npy", corners)
        log(f"[cam] corners {np.round(corners, 1).tolist()}")
        area = cv2.contourArea(corners.reshape(-1, 1, 2))
        if area < 0.25 * 1920 * 1080:
            raise SystemExit(f"[cam] panel covers only {area/(1920*1080)*100:.0f} % of the camera frame — re-aim")

        pres.cmd("loadfile", VIDEO, "replace")
        if not pres.wait_loaded(VIDEO, timeout=15): raise RuntimeError("mpv did not load the video")
        pres.cmd("set_property", "pause", True)

        def seek(t):
            pres.cmd("seek", t, "absolute+exact")
            for _ in range(80):
                r = pres.cmd("get_property", "time-pos"); pos = (r or {}).get("data")
                if pos is not None and abs(float(pos) - t) < 0.15: break
                time.sleep(0.1)
            else:
                log(f"[mpv] WARNING time-pos {pos} after seek {t}")
            time.sleep(SETTLE + 1.0)
            return pos

        def stats(expo):
            cam.set_manual(exposure=expo, gain=0)
            f = warp(cam.grab(4), corners, CW, CH); g = f.mean(axis=2)
            return g, float(np.percentile(g, 99.9)), float(np.percentile(g, 50)), float((g >= 250).mean())

        for t in ts:
            name = tsname(t); d = OUT / name; d.mkdir(exist_ok=True)
            set_state(False, 0)
            pos = seek(t)
            # exposure A: the longest exposure with < 0.3 % of the panel clipped (bright frames clip anyway; the
            # highlight cores are not the target, their surround is). B = A + 2 stops (dark surround lifted).
            expo_a = -10
            for expo in (-5, -6, -7, -8, -9, -10):
                g, p999, p50, clip = stats(expo)
                log(f"   [cam] {name} exposure {expo}: p50 {p50:.0f} p99.9 {p999:.0f} clipped {100*clip:.2f} %")
                expo_a = expo
                if clip < 0.003: break
            expo_b = min(expo_a + 2, -3)
            caps = {}
            for label, on, dbg in STATES:
                set_state(on, dbg)
                # the overlay path re-renders only on a new desktop frame: a paused video gives none after the
                # debug-mode switch (first run: 9/16 ON captures were stale identity renders) → force a redraw
                pres.cmd("frame-step"); time.sleep(0.5); pres.cmd("frame-step")
                time.sleep(SETTLE)
                for expo, tag in ((expo_a, "A"), (expo_b, "B")):
                    if label == "off2" and tag == "B": continue
                    cam.set_manual(exposure=expo, gain=0)
                    raw = cam.grab(NSTACK)
                    w = warp(raw, corners, CW, CH).astype(np.float32)
                    np.save(d / f"cap_{label}_{tag}.npy", w.astype(np.float16))
                    cv2.imwrite(str(d / f"cap_{label}_{tag}.png"), cv2.cvtColor(np.clip(w, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
                    caps[f"{label}_{tag}"] = w
                    log(f"   [cam] {name} {label:<4} {tag} expo {expo}: mean {w.mean():6.1f}  p99.9 {np.percentile(w.mean(axis=2), 99.9):5.0f}")
            set_state(False, 0)
            ga = caps["off_A"].mean(axis=2); gb = caps["on_A"].mean(axis=2); g2 = caps["off2_A"].mean(axis=2)
            drift = float(np.median(g2[ga > 20] / ga[ga > 20])) if (ga > 20).any() else float("nan")
            meta["frames"][name] = {"t": t, "time_pos": pos, "expo_a": expo_a, "expo_b": expo_b, "drift_off2_over_off": drift}
            log(f"[ab] {name} done: exposures A {expo_a} / B {expo_b}; OFF repeat / OFF median ratio {drift:.4f}")
            json.dump(meta, open(OUT / "capture_meta.json", "w"), indent=1)
    finally:
        try: set_state(False, 0)
        except Exception: pass
        cam.stop(); pres.close()
        json.dump(meta, open(OUT / "capture_meta.json", "w"), indent=1)
        if native:
            try: ctrl.exit_calibration(restore_snapshot=True); log("[restore] stack restored")
            except Exception as exc: log(f"[restore] failed: {exc}")
    return 0


def video_frame_luma(t, w=192, h=108):
    """Luminance thumbnail of the video frame at t (ffmpeg decode), for verifying which frame a dump holds."""
    import subprocess
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", VIDEO, "-frames:v", "1", "-vf", f"scale={w}:{h}",
                        "-pix_fmt", "gray", "-f", "rawvideo", "-"], capture_output=True)
    return np.frombuffer(r.stdout, np.uint8)[: w * h].reshape(h, w).astype(np.float64)


def dump_matches_video(dd, t):
    """Correlation between the dumped frame (as-if PQ code) and the ffmpeg frame at t."""
    if not (dd / "fald_frame.rgba16f").exists(): return None
    img = load_dump_frame(dd).max(axis=0)
    thumb = cv2.resize(np.log1p(img).astype(np.float32), (192, 108), interpolation=cv2.INTER_AREA)
    ref = video_frame_luma(t)
    return float(np.corrcoef(thumb.ravel(), ref.ravel())[0, 1])


def dumps(ts):
    """Dump pass (no camera): per timestamp seek, layer ON, request the dump, force a new frame (mpv frame-step:
    the chapters are still frames), verify the dumped frame against the video."""
    import agent_fald_leak_probe as P
    from fald_ramp_probe import MpvPresenter, SETTLE
    from dlc.controller import CalibrationController
    import shutil
    ctrl = CalibrationController.connect()
    P.audit_or_refuse(ctrl, allow_stack=True)     # no meter/camera: the stack does not matter for the dump

    def set_state(on, debug):
        ctrl.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": debug})
        ctrl.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": on}); time.sleep(0.8)
    pres = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")), extra_args=("--hr-seek=yes", "--pause", "--audio=no"))
    try:
        pres.cmd("loadfile", VIDEO, "replace")
        if not pres.wait_loaded(VIDEO, timeout=15): raise RuntimeError("mpv did not load the video")
        pres.cmd("set_property", "pause", True)
        for t in ts:
            name = tsname(t); dd = OUT / name / "dump"
            if dd.exists(): shutil.rmtree(dd)
            dd.mkdir(parents=True)
            set_state(False, 0)
            pres.cmd("seek", t, "absolute+exact"); time.sleep(SETTLE + 1.0)
            set_state(True, 0); time.sleep(SETTLE)
            for attempt in range(3):      # a stale pending request (paused video, no new frame) flushes on a frame-step
                try:
                    ctrl.call("runtime.fald_dump", {"monitor": MON, "mode": "HDR", "dir": str(dd)}); break
                except Exception as exc:
                    log(f"[dump] {name}: request failed ({exc}); frame-stepping to flush a stale request")
                    pres.cmd("frame-step"); time.sleep(1.0)
            for k in range(6):
                pres.cmd("frame-step"); time.sleep(0.7)
                if (dd / "fald_dump.txt").exists(): break
            time.sleep(0.5)
            pos = (pres.cmd("get_property", "time-pos") or {}).get("data")
            c = dump_matches_video(dd, t)
            log(f"[dump] {name}: written={ (dd / 'fald_dump.txt').exists() } after {k+1} frame-steps, time-pos {pos}, corr vs video {c if c is None else round(c, 3)}")
            set_state(False, 0)
    finally:
        try: set_state(False, 0)
        except Exception: pass
        pres.close()
    return 0


def geom(ts=None):
    """Geometry test: does the overlay path (ID) present the image where the direct path (OFF) does? A 100-nit
    line grid (240-px pitch) is captured OFF / ID / ON / OFF; shifts by phase correlation and by line centroids."""
    import agent_fald_leak_probe as P
    from fald_ramp_probe import MpvPresenter, SETTLE
    from fald_cam import Camera, warp
    from dlc.controller import CalibrationController
    ctrl = CalibrationController.connect()
    P.audit_or_refuse(ctrl, allow_stack=True)
    if P.stack_layers(ctrl)[2].get("active"): raise SystemExit("[geom] DWM hook active")
    def set_state(on, debug):
        ctrl.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": debug})
        ctrl.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": on}); time.sleep(0.8)
    corners = np.load(OUT / "panel_corners.npy")
    pres = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")))
    cam = Camera(); caps = {}
    try:
        grid = OUT / "grid_frame.png"
        pres.cmd("loadfile", str(grid), "replace"); pres.wait_loaded(str(grid)); time.sleep(SETTLE + 2.0)
        cam.set_manual(exposure=-6, gain=0)
        for label, on, dbg in (("off", False, 0), ("id", True, 4), ("on", True, 0), ("off2", False, 0), ("id2", True, 4)):
            set_state(on, dbg); time.sleep(SETTLE)
            caps[label] = warp(cam.grab(24), corners, CW, CH).astype(np.float32)[:, :, 1]
            np.save(OUT / f"geom_{label}.npy", caps[label].astype(np.float16))
            log(f"[geom] {label:<4} mean {caps[label].mean():.2f} max {caps[label].max():.0f}")
        set_state(False, 0)
    finally:
        try: set_state(False, 0)
        except Exception: pass
        cam.stop(); pres.close()
    ref = caps["off"]
    def centroid_shift(a, b):
        # column/row profiles of the line grid: shift of the line centroids (robust to level differences)
        out = []
        for axis in (0, 1):
            pa = a.mean(axis=axis); pb = b.mean(axis=axis)
            pa = pa - np.percentile(pa, 20); pb = pb - np.percentile(pb, 20)
            pa = np.maximum(pa, 0); pb = np.maximum(pb, 0)
            n = len(pa); idx = np.arange(n)
            out.append(float((idx * pb).sum() / pb.sum() - (idx * pa).sum() / pa.sum()))
        return [round(v, 3) for v in out]      # [dx, dy]
    res = {}
    for k in ("id", "on", "off2", "id2"):
        (dx, dy), resp = cv2.phaseCorrelate(np.float32(ref - ref.mean()), np.float32(caps[k] - caps[k].mean()))
        res[k] = {"phase_dx_dy": [round(float(dx), 3), round(float(dy), 3)], "phase_resp": round(float(resp), 3), "centroid_dx_dy": centroid_shift(ref, caps[k]),
                  "level_ratio": round(float(caps[k][ref > 30].mean() / ref[ref > 30].mean()), 4)}
        log(f"[geom] {k:<4} vs OFF: phase shift {res[k]['phase_dx_dy']} (resp {res[k]['phase_resp']})  centroid shift {res[k]['centroid_dx_dy']}  line level ratio {res[k]['level_ratio']}  [camera px ≈ 2 screen px]")
    json.dump(res, open(OUT / "geom_result.json", "w"), indent=1)
    return 0


def meter(ts):
    """Meter pass: i1D3 at the spot FALD_METER (default 1988,1120 — placement ±20 px since §31) reads OFF / ID / ON per
    frame (forced redraw after every state change). Gives the TRUE panel change at one point per frame, next to the
    model's prediction averaged over a 120-px aperture there and the camera's ON/ID ratio at the same spot."""
    import agent_fald_leak_probe as P
    from fald_ramp_probe import MpvPresenter, SETTLE
    from dlc.controller import CalibrationController
    from dlc.measure_loop import MeasurePatch, make_persistent_spotread_meter
    from dlc.measure_rgbw import resolve_spotread_instrument_port
    from dlc.argyll import Argyll, SpotreadRequest
    import dlc.calibration_profile as cp
    from dlc.calibrate import active_correction, correction_store_path
    from dlc.correction_store import CorrectionStore
    mx, my = (int(v) for v in os.environ.get("FALD_METER", "1988,1120").split(","))
    ctrl = CalibrationController.connect()
    native = os.environ.get("FALD_NATIVE") == "1"
    if native: P.enter_native(ctrl)
    P.audit_or_refuse(ctrl, allow_stack=os.environ.get("FALD_ALLOW_STACK") == "1")
    if P.stack_layers(ctrl)[2].get("active"): raise SystemExit("[meter] DWM hook active")
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(MON).name)

    class NoShow:                      # the video frame is already on screen; the meter fn only needs .show()
        def show(self, patch): time.sleep(0.3)
    pres = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")), extra_args=("--hr-seek=yes", "--pause", "--audio=no"))
    meterp = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=NoShow(), persistent=meterp)

    def set_state(on, debug):
        ctrl.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": debug})
        ctrl.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": on}); time.sleep(0.8)
        pres.cmd("frame-step"); time.sleep(0.5); pres.cmd("frame-step"); time.sleep(SETTLE)

    def read(label):
        rd = measure(MeasurePatch(label=label, rgb=(512, 512, 512), signal=(0.5, 0.5, 0.5), role="measurement", bit_depth=10, seq=0))
        return list(rd.xyz) if rd.ok and rd.xyz else None
    out = {}
    try:
        pres.cmd("loadfile", VIDEO, "replace")
        if not pres.wait_loaded(VIDEO, timeout=15): raise RuntimeError("mpv did not load the video")
        pres.cmd("set_property", "pause", True)
        for t in ts:
            name = tsname(t)
            set_state(False, 0)
            pres.cmd("seek", t, "absolute+exact"); time.sleep(SETTLE + 1.0)
            r = {}
            for label, on, dbg in (("off", False, 0), ("id", True, 4), ("on", True, 0), ("off2", False, 0)):
                set_state(on, dbg); r[label] = read(f"{name} {label}")
            # model prediction at the spot (120-px aperture), camera ON/ID at the spot (60 camera px)
            pred = model_prediction(OUT / name / "dump"); m = None
            if pred is not None:
                _, yo, yn = pred; a = 60
                m = {"y_off": float(yo[my - a:my + a, mx - a:mx + a].mean()), "y_on": float(yn[my - a:my + a, mx - a:mx + a].mean())}
            cam = None
            d = OUT / name
            if (d / "cap_on_B.npy").exists():
                on_b = np.load(d / "cap_on_B.npy").astype(np.float32); id_b = np.load(d / "cap_id_B.npy").astype(np.float32)
                cy, cx, a = my // 2, mx // 2, 30
                cam = float(on_b[cy - a:cy + a, cx - a:cx + a, 1].mean() / max(id_b[cy - a:cy + a, cx - a:cx + a, 1].mean(), 1e-3))
            Y = lambda k: (r[k][1] if r.get(k) else float("nan"))
            out[name] = {"t": t, "meter_xy": [mx, my], "xyz": r, "model": m, "camera_on_id": cam}
            log(f"[meter] {name}: OFF {Y('off'):8.3f}  ID {Y('id'):8.3f}  ON {Y('on'):8.3f}  OFF2 {Y('off2'):8.3f} nits | ON/ID {Y('on')/Y('id'):.4f}  ID/OFF {Y('id')/Y('off'):.4f}"
                + (f" | model ON/OFF {m['y_on']/max(m['y_off'],1e-6):.4f} (OFF {m['y_off']:.2f} nits)" if m else "") + (f" | camera ON/ID {cam:.4f}" if cam else ""))
            json.dump(out, open(OUT / "meter_result.json", "w"), indent=1)
    finally:
        try: set_state(False, 0)
        except Exception: pass
        pres.close()
        try: meterp.close()
        except Exception: pass
        json.dump(out, open(OUT / "meter_result.json", "w"), indent=1)
        if native:
            try: ctrl.exit_calibration(restore_snapshot=True); log("[restore] stack restored")
            except Exception as exc: log(f"[restore] failed: {exc}")
    return 0


# ----------------------------------------------------------------------------------------------- analysis
def load_dump_frame(dd):
    meta = dict(l.split(" ", 1) for l in (dd / "fald_dump.txt").read_text().splitlines() if " " in l)
    w, h = int(meta["width"]), int(meta["height"])
    frame = np.fromfile(dd / "fald_frame.rgba16f", dtype=np.float16).reshape(h, w, 4)[:, :, :3].astype(np.float64)
    rec = np.einsum("ij,hwj->hwi", BT709_TO_BT2020, frame)
    return np.ascontiguousarray(np.maximum(rec, 0).transpose(2, 0, 1) * 80.0)


def model_prediction(dd):
    """Model OFF / ON panel luminance (nits, 3840x2160) for the dumped frame; None if no dump."""
    if not (dd / "fald_frame.rgba16f").exists(): return None
    import fald_sim_frames as F
    from dlc.fald.model import FaldModel
    from dlc.fald.correct import load_fitted_params, correct_image
    img = load_dump_frame(dd)
    model = FaldModel(replace(load_fitted_params(F.PARAMS), scale=1, width=img.shape[2], height=img.shape[1]))
    img = np.minimum(img, model.p.white_nits)
    y_off = model.forward_img(img)["y"].sum(axis=0)
    req = np.clip(correct_image(model, img, iters=2)["req"], 0, model.p.white_nits)
    y_on = model.forward_img(req)["y"].sum(axis=0)
    return img, y_off, y_on


def tonemap(y, peak=None):
    """Quick display map of a luminance image: log-ish curve to 8 bit."""
    y = np.maximum(y, 0); peak = peak or max(float(np.percentile(y, 99.9)), 1.0)
    v = np.log1p(y / peak * 40) / np.log1p(40)
    return np.clip(v * 255, 0, 255).astype(np.uint8)


def diverging(ratio_minus_1, lim, valid):
    """Blue (negative) / white (0) / red (positive) map of ratio−1, grey where invalid."""
    v = np.clip(ratio_minus_1 / lim, -1, 1)
    r = np.where(v >= 0, 1.0, 1 + v); g = 1 - np.abs(v); b = np.where(v <= 0, 1.0, 1 - v)
    img = (np.stack([b, g, r], axis=2) * 255).astype(np.uint8)
    img[~valid] = 60
    return img


def ratio_map(num, den, sigma=4.0):
    """Smoothed ratio of two camera stacks (green channel, the least demosaic-affected); valid where both are
    well inside the 8-bit range (den > 6 counts: at 48-frame stacks the noise floor is ~0.5 counts)."""
    n = cv2.GaussianBlur(num[:, :, 1], (0, 0), sigma); d = cv2.GaussianBlur(den[:, :, 1], (0, 0), sigma)
    valid = (d > 6) & (n > 6) & (num.max(axis=2) < 248) & (den.max(axis=2) < 248)
    return np.where(valid, n / np.maximum(d, 1e-3), 1.0), valid


def find_blobs(gA, k=3, min_area=200):
    """Centroids of the k largest bright regions (top 0.5 % of a stack) — highlight cores to profile."""
    thr = np.percentile(gA, 99.5)
    m = (gA >= max(thr, 60)).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    order = sorted(range(1, n), key=lambda i: -st[i, cv2.CC_STAT_AREA])
    return [(cen[i], st[i]) for i in order if st[i, cv2.CC_STAT_AREA] >= min_area][:k]


def analyze(ts):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    summary = {}
    meta = json.load(open(OUT / "capture_meta.json")) if (OUT / "capture_meta.json").exists() else {"frames": {}}
    for t in ts:
        name = tsname(t); d = OUT / name
        if not (d / "cap_on_A.npy").exists():
            log(f"[an] {name}: no capture"); continue
        cap = {k: np.load(d / f"cap_{k}.npy").astype(np.float32) for k in ("off_A", "id_A", "on_A", "off2_A", "off_B", "id_B", "on_B")}
        # sub-pixel shifts between states by phase correlation (recorded for all; on sparse-highlight content the
        # estimate is unreliable at the 1-px level — frame 1 gave opposite signs at the two exposures — so only the
        # ON-vs-ID pair, same overlay path, is registered, and only when the estimate is small and consistent)
        shifts = {}
        for k in ("id_A", "on_A", "off2_A", "id_B", "on_B"):
            ref = cap["off_A" if k.endswith("A") else "off_B"][:, :, 1]
            (dx, dy), resp = cv2.phaseCorrelate(np.float32(ref - ref.mean()), np.float32(cap[k][:, :, 1] - cap[k][:, :, 1].mean()))
            shifts[k] = [round(float(dx), 3), round(float(dy), 3)]
        for k in ("on_A", "on_B"):
            ref = cap["id_" + k[-1]][:, :, 1]
            (dx, dy), resp = cv2.phaseCorrelate(np.float32(ref - ref.mean()), np.float32(cap[k][:, :, 1] - cap[k][:, :, 1].mean()))
            shifts[k + "_vs_id"] = [round(float(dx), 3), round(float(dy), 3)]
            if 0.2 < max(abs(dx), abs(dy)) < 1.5:
                M = np.float32([[1, 0, -dx], [0, 1, -dy]])
                cap[k] = cv2.warpAffine(cap[k], M, (CW, CH), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        # ratio maps (camera): ON/ID = the correction through the panel; ID/OFF = overlay-path cost; OFF2/OFF = drift/noise
        r_on_id_A, v1 = ratio_map(cap["on_A"], cap["id_A"]); r_on_id_B, v2 = ratio_map(cap["on_B"], cap["id_B"])
        r_id_off_B, v3 = ratio_map(cap["id_B"], cap["off_B"]); r_drift, v4 = ratio_map(cap["off2_A"], cap["off_A"])
        # merged ON/ID: exposure B where valid (dark surround), else A
        r_on_id = np.where(v2, r_on_id_B, r_on_id_A); valid = v1 | v2
        # model prediction for the same frame, resampled onto the camera grid
        pred = model_prediction(d / "dump")
        rows = {}
        if pred is not None:
            img, y_off, y_on = pred
            m_ratio = cv2.resize((y_on / np.maximum(y_off, 1e-3)).astype(np.float32), (CW, CH), interpolation=cv2.INTER_AREA)
            m_y = cv2.resize(y_off.astype(np.float32), (CW, CH), interpolation=cv2.INTER_AREA)
            frame_thumb = cv2.resize(tonemap(img.max(axis=0), peak=1000), (CW, CH), interpolation=cv2.INTER_AREA)
            np.save(d / "model_ratio_on_off.npy", m_ratio.astype(np.float16))
            # regions: where the model predicts a change (|ratio-1| > 2 %) split by sign, vs untouched
            # camera veiling glare is a frame-wide additive term that changes when the layer changes the bright
            # content; the median ON/ID over model-untouched pixels estimates that global offset → "norm" columns
            unt = (np.abs(m_ratio - 1) < 0.005) & valid
            glare = float(np.median(r_on_id[unt])) if unt.sum() > 500 else 1.0
            for label, sel in (("model_darken", (m_ratio < 0.98) & valid), ("model_brighten", (m_ratio > 1.02) & valid),
                               ("model_untouched", unt)):
                if sel.sum() < 500: continue
                rows[label] = {"px": int(sel.sum()), "model_mean": float(m_ratio[sel].mean() - 1),
                               "cam_on_id_mean": float(r_on_id[sel].mean() - 1), "cam_on_id_median": float(np.median(r_on_id[sel]) - 1),
                               "cam_on_id_norm_median": float(np.median(r_on_id[sel]) / glare - 1),
                               "cam_id_off_mean": float(r_id_off_B[sel & v3].mean() - 1) if (sel & v3).sum() > 100 else None,
                               "cam_drift_mean": float(r_drift[sel & v4].mean() - 1) if (sel & v4).sum() > 100 else None}
            both = valid & (np.abs(m_ratio - 1) > 0.01)
            corr = float(np.corrcoef(m_ratio[both] - 1, r_on_id[both] - 1)[0, 1]) if both.sum() > 1000 else None
        else:
            m_ratio = None; corr = None; frame_thumb = None
        # halo profiles across the brightest blobs (exposure B, horizontal + vertical lines through the centroid)
        gA = cap["off_A"].mean(axis=2)
        blobs = find_blobs(gA)
        fig, axes = plt.subplots(2, 3, figsize=(18, 9))
        ax = axes.ravel()
        ax[0].imshow(cv2.cvtColor(np.clip(cap["off_B"], 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR)[:, :, ::-1]); ax[0].set_title(f"{name}: camera OFF (exposure B)")
        ax[1].imshow(diverging(r_on_id - 1, 0.15, valid)[:, :, ::-1]); ax[1].set_title("camera ON/ID − 1  (±15 %, red = brighter with layer)")
        if m_ratio is not None:
            ax[2].imshow(diverging(m_ratio - 1, 0.15, np.ones_like(valid))[:, :, ::-1]); ax[2].set_title(f"model ON/OFF − 1 (±15 %)  corr {corr if corr is None else round(corr, 3)}")
        else:
            ax[2].set_title("no dump")
        ax[3].imshow(diverging(r_id_off_B - 1, 0.15, v3)[:, :, ::-1]); ax[3].set_title("camera ID/OFF − 1 (overlay-path cost, ±15 %)")
        ax[4].imshow(diverging(r_drift - 1, 0.15, v4)[:, :, ::-1]); ax[4].set_title("camera OFF repeat / OFF − 1 (drift+noise, ±15 %)")
        prof = ax[5]
        for bi, (c, st) in enumerate(blobs[:2]):
            cx, cy = int(c[0]), int(c[1]); half = max(int(3 * max(st[2], st[3])), 120)
            x0, x1 = max(cx - half, 0), min(cx + half, CW)
            xs = np.arange(x0, x1) - cx
            for k, col in (("off_B", "k"), ("id_B", "b"), ("on_B", "r")):
                line = cap[k][max(cy - 3, 0):cy + 4, x0:x1, 1].mean(axis=0)
                prof.plot(xs, line, col, lw=1, alpha=0.9 if bi == 0 else 0.4, label=f"{k} blob{bi}" if bi == 0 else None)
            if m_ratio is not None:
                prof.plot(xs, m_y[cy, x0:x1] / max(m_y[cy, x0:x1].max(), 1e-3) * 200, "g--", lw=0.8, alpha=0.5, label="model OFF (scaled)" if bi == 0 else None)
            for a in (ax[0], ax[1]): a.add_patch(plt.Rectangle((x0, cy - half), x1 - x0, 2 * half, fill=False, ec="yellow" if bi == 0 else "cyan", lw=1))
        prof.set_yscale("log"); prof.set_ylim(1, 300); prof.set_title("horizontal line through the brightest blob(s), exposure B (camera counts)")
        prof.legend(fontsize=8); prof.grid(alpha=0.3)
        for a in ax[:5]: a.axis("off")
        fig.tight_layout(); fig.savefig(d / "panel.png", dpi=80); plt.close(fig)
        # contrast-stretched crops around blob 0: OFF / ID / ON side by side (exposure B)
        if blobs:
            c, st = blobs[0]; cx, cy = int(c[0]), int(c[1]); half = max(int(3 * max(st[2], st[3])), 150)
            y0, y1, x0, x1 = max(cy - half, 0), min(cy + half, CH), max(cx - half, 0), min(cx + half, CW)
            crops = [cap[k][y0:y1, x0:x1] for k in ("off_B", "id_B", "on_B")]
            lo, hi = 0, max(float(np.percentile(crops[0], 90)), 5)
            stretched = [np.clip((c - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8) for c in crops]
            strip = np.concatenate(stretched, axis=1)
            cv2.putText(strip, "OFF        |        ID        |        ON   (stretched to p90)", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 1)
            cv2.imwrite(str(d / "crop_blob0.png"), cv2.cvtColor(strip, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(d / "ratio_on_id.png"), diverging(r_on_id - 1, 0.15, valid))
        if m_ratio is not None: cv2.imwrite(str(d / "ratio_model.png"), diverging(m_ratio - 1, 0.15, np.ones_like(valid)))
        drift = float(np.median(r_drift[v4]) - 1) if v4.any() else None
        dcorr = dump_matches_video(d / "dump", t)
        summary[name] = {"t": t, "corr_model_vs_camera": corr, "regions": rows, "drift_median": drift, "shifts_px": shifts, "dump_vs_video_corr": dcorr,
                         "on_id_valid_frac": float(valid.mean()), "cam_on_id_p1_p99": [float(np.percentile(r_on_id[valid], q) - 1) for q in (1, 99)] if valid.any() else None,
                         **meta["frames"].get(name, {})}
        log(f"[an] {name}: corr(model, camera) {corr}  drift {drift}  on/id p1..p99 {summary[name]['cam_on_id_p1_p99']}  shifts {shifts}  dump-vs-video corr {dcorr}")
        for k, v in rows.items():
            log(f"      {k:<16} px {v['px']:8d}  model {100*v['model_mean']:+6.2f} %  camera ON/ID mean {100*v['cam_on_id_mean']:+6.2f} % median {100*v['cam_on_id_median']:+6.2f} % glare-norm {100*v['cam_on_id_norm_median']:+6.2f} %"
                f"  ID/OFF {('%+6.2f' % (100*v['cam_id_off_mean'])) if v['cam_id_off_mean'] is not None else '  n/a'} %  drift {('%+6.2f' % (100*v['cam_drift_mean'])) if v['cam_drift_mean'] is not None else '  n/a'} %")
    json.dump(summary, open(OUT / "analysis_summary.json", "w"), indent=1)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
    ts = [float(a) for a in sys.argv[2:]] or DEFAULT_TS
    sys.exit({"capture": capture, "dumps": dumps, "analyze": analyze, "geom": geom, "meter": meter}[cmd](ts))
