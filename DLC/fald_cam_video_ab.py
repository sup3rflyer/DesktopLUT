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

Camera counts are NOT linear light (§32a finding 2): `analyze` linearises every stack with a tone curve
measured from the A/B exposure pairs (×4 light) of all frames of the same camera session (one Camera open;
capture_meta `cam_session`, or `camera_sessions.json` for older stacks — the C922 re-opened after the
capture4 crash came up with a different black pedestal) before any ratio is formed (`build_curves`,
`tone_curve_from_hist`; the per-frame fit is logged as an audit); `--raw` keeps the encoded-count path for
comparison. The model prediction per frame is cached in `model_cache.npz` (`model_maps`, ~60 s to rebuild).

Array order: `fald_cam.Camera.grab` returns pygrabber's RGB24 buffer unswapped, which is **BGR**
(finding 9). Every `cap_*.npy` on disk is therefore BGR (H, W, 3) — the stored convention is kept; the
analysis uses channel index 1 (green, order-independent) and the PNG previews are written as BGR
(cv2.imwrite's native order) / shown as `[:, :, ::-1]` in matplotlib.

State: refuses unless the stack audit passes (FALD_NATIVE=1 enters neutral + identity MHC, restored on exit).
Usage:  PYTHONPATH="src;." FALD_NATIVE=1 python fald_cam_video_ab.py capture [t1 t2 ...]
        PYTHONPATH="src;." python fald_cam_video_ab.py dumps [t1 t2 ...]
        PYTHONPATH="src;." python fald_cam_video_ab.py analyze [--raw] [t1 t2 ...]
        PYTHONPATH="src;." python fald_cam_video_ab.py recheck        (offline: meter camera column + model disc/square)
Env: FALD_OUT (results dir), FALD_MPV_SCREEN (0), FALD_VIDEO, FALD_STACK (48 frames), FALD_METER (x,y).
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
CAP_KEYS = ("off_A", "id_A", "on_A", "off2_A", "off_B", "id_B", "on_B")
SAT = 248                      # a channel at/above this (of 255) = clipped; VALID_MIN = counts a ratio denominator needs
VALID_MIN = 6

BT709_TO_BT2020 = np.array([[0.6274040, 0.3292820, 0.0433136],
                            [0.0690970, 0.9195400, 0.0113612],
                            [0.0163916, 0.0880132, 0.8955950]])


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def tsname(t):
    return f"t{t:07.2f}".replace(".", "_")


def set_exposure(cam, expo, gain=0):
    """set_manual + read-back (§32a follow-up 8): the UVC control can silently keep the previous value (the
    capture4 crash left an ON stack at the wrong exposure with nothing to catch it). Returns the value the
    camera reports, or None if the read-back failed; logs a WARNING on a mismatch."""
    cam.set_manual(exposure=expo, gain=gain)
    try:
        got = cam.ranges()["exposure"]["value"]
    except Exception as exc:  # noqa: BLE001
        log(f"   [cam] WARNING exposure read-back failed after set_manual({expo}): {exc}"); return None
    if got != expo:
        log(f"   [cam] WARNING exposure read-back {got} != requested {expo} — the stack about to be captured is at the WRONG exposure")
    return got


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
    cam = Camera(); cam_session = time.strftime("cam%Y%m%d_%H%M%S")     # one Camera open = one camera state (the re-opened C922 came up with a different pedestal on 2026-09-12)
    prev = json.load(open(OUT / "capture_meta.json")) if (OUT / "capture_meta.json").exists() else {}
    meta = {"video": VIDEO, "native": native, "layers_at_start": layers, "nstack": NSTACK, "frames": prev.get("frames", {})}
    try:
        set_state(False, 0)
        pres.cmd("loadfile", str(grey), "replace"); pres.wait_loaded(str(grey)); time.sleep(SETTLE + 1.0)
        set_exposure(cam, -6)
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
            set_exposure(cam, expo)
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
            caps = {}; mismatch = []
            for label, on, dbg in STATES:
                set_state(on, dbg)
                # the overlay path re-renders only on a new desktop frame: a paused video gives none after the
                # debug-mode switch (first run: 9/16 ON captures were stale identity renders) → force a redraw
                pres.cmd("frame-step"); time.sleep(0.5); pres.cmd("frame-step")
                time.sleep(SETTLE)
                for expo, tag in ((expo_a, "A"), (expo_b, "B")):
                    if label == "off2" and tag == "B": continue
                    got = set_exposure(cam, expo)
                    if got != expo: mismatch.append({"stack": f"{label}_{tag}", "requested": expo, "readback": got})
                    raw = cam.grab(NSTACK)
                    w = warp(raw, corners, CW, CH).astype(np.float32)        # BGR (see module docstring)
                    np.save(d / f"cap_{label}_{tag}.npy", w.astype(np.float16))
                    cv2.imwrite(str(d / f"cap_{label}_{tag}.png"), np.clip(w, 0, 255).astype(np.uint8))   # BGR in → BGR out
                    caps[f"{label}_{tag}"] = w
                    log(f"   [cam] {name} {label:<4} {tag} expo {expo}: mean {w.mean():6.1f}  p99.9 {np.percentile(w.mean(axis=2), 99.9):5.0f}")
            set_state(False, 0)
            ga = caps["off_A"].mean(axis=2); gb = caps["on_A"].mean(axis=2); g2 = caps["off2_A"].mean(axis=2)
            drift = float(np.median(g2[ga > 20] / ga[ga > 20])) if (ga > 20).any() else float("nan")
            meta["frames"][name] = {"t": t, "time_pos": pos, "expo_a": expo_a, "expo_b": expo_b, "drift_off2_over_off": drift,
                                    "exposure_readback_mismatch": mismatch, "cam_session": cam_session}
            log(f"[ab] {name} done: exposures A {expo_a} / B {expo_b}; OFF repeat / OFF median ratio {drift:.4f}"
                + (f"; EXPOSURE MISMATCH on {[m['stack'] for m in mismatch]}" if mismatch else ""))
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
    the chapters are still frames), verify the dumped frame against the video.
    Refuses when tonemap / desktop_gamma / white_balance / grayscale is ON (§32a finding 11): the dumped texture
    is post-main-pass, so a dump through those layers is not the frame the model must see. fald itself ON is fine."""
    import agent_fald_leak_probe as P
    from fald_ramp_probe import MpvPresenter, SETTLE
    from dlc.controller import CalibrationController
    import shutil
    ctrl = CalibrationController.connect()
    layers = P.audit_or_refuse(ctrl, allow_stack=True)     # prints the stack; the refusal below is the dump-specific one
    on = [k for k in ("tonemap", "desktop_gamma", "white_balance", "grayscale") if layers.get(k)]
    if on:
        raise SystemExit(f"[dump] REFUSING: DesktopLUT layers ON for {MON}:HDR: {on} — the dumped texture is post-main-pass, "
                         "so the dump would be the frame AFTER these layers, not the one the model predicts; turn them off "
                         "(FALD_NATIVE=1 capture/meter enters neutral) and re-run")

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
        set_exposure(cam, -6)
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
    model's prediction at the spot (the model's fitted aperture DISC via `model_spot`; the old 120-px square kept as
    `*_square`) and the camera's ON/ID ratio at the same spot (`camera_spot`: validity-gated, exposure B else A)."""
    import agent_fald_leak_probe as P
    from fald_ramp_probe import MpvPresenter, SETTLE
    from dlc.controller import CalibrationController
    from dlc.measure_loop import MeasurePatch, make_persistent_spotread_meter
    from dlc.measure_rgbw import resolve_spotread_instrument_port
    from dlc.argyll import Argyll, SpotreadRequest
    import dlc.calibration_profile as cp
    from dlc.calibrate import active_correction, correction_store_path
    from dlc.correction_store import CorrectionStore
    mx, my = meter_xy()
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
    out = {}; curves = {}
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
            # model prediction at the spot (fitted aperture disc + the 120-px square), camera ON/ID at the spot
            mm = model_maps(OUT / name, mx, my)
            m = model_spot(mm, mx, my) if mm is not None else None
            d = OUT / name; cam = None
            if (d / "cap_on_A.npy").exists():
                if name not in curves:
                    curves.update(build_curves([tsname(x) for x in ts], log_each=False)[0])
                cam = camera_spot(d, mx, my, curves.get(name))
            Y = lambda k: (r[k][1] if r.get(k) else float("nan"))
            out[name] = {"t": t, "meter_xy": [mx, my], "xyz": r, "model": m, "camera": cam,
                         "camera_on_id": cam["ratio"] if cam else None}
            log(f"[meter] {name}: OFF {Y('off'):8.3f}  ID {Y('id'):8.3f}  ON {Y('on'):8.3f}  OFF2 {Y('off2'):8.3f} nits | ON/ID {Y('on')/Y('id'):.4f}  ID/OFF {Y('id')/Y('off'):.4f}"
                + (f" | model ON/OFF disc {m['y_on']/max(m['y_off'],1e-6):.4f} square {m['y_on_square']/max(m['y_off_square'],1e-6):.4f} (OFF {m['y_off']:.2f} nits)" if m else "")
                + (f" | camera ON/ID {cam['ratio']:.4f} (exposure {cam['exposure']}" + (f", linear {cam['ratio_lin']:.4f})" if cam['ratio_lin'] else ")")
                   if cam and cam["valid"] else " | camera ON/ID n/a (window saturated or at the noise floor at both exposures)"))
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


def meter_xy():
    return tuple(int(v) for v in os.environ.get("FALD_METER", "1988,1120").split(","))


# ----------------------------------------------------------------------------------------------- analysis
def load_dump_meta(dd):
    """Key/value header of a dump (`fald_dump.txt`): width/height/cols/rows/params/frames_run/..."""
    return dict(l.split(" ", 1) for l in (dd / "fald_dump.txt").read_text().splitlines() if " " in l)


def load_dump_frame(dd):
    meta = load_dump_meta(dd)
    w, h = int(meta["width"]), int(meta["height"])
    frame = np.fromfile(dd / "fald_frame.rgba16f", dtype=np.float16).reshape(h, w, 4)[:, :, :3].astype(np.float64)
    rec = np.einsum("ij,hwj->hwi", BT709_TO_BT2020, frame)
    return np.ascontiguousarray(np.maximum(rec, 0).transpose(2, 0, 1) * 80.0)


def model_prediction(dd):
    """Model OFF / ON panel luminance (nits, 3840x2160) for the dumped frame + the model; None if no dump."""
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
    return img, y_off, y_on, model


def aperture_disc(shape, x, y, r):
    """Boolean disc of radius r px centred on (x, y) — the same pixel-centre formula as FaldModel.aperture_mask
    at scale 1 (asserted equal when the cache is built), so cached crops give the same disc means."""
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return ((xx + 0.5 - x) ** 2 + (yy + 0.5 - y) ** 2) <= r * r


def model_maps(d, mx, my, crop=200):
    """Model prediction for a frame (its dump), cached in `model_cache.npz` next to the stacks: ON/OFF ratio and
    OFF luminance on the camera grid, full-resolution OFF/ON luminance crops of ±crop px around the meter spot,
    the model's aperture radius and the dump identity. `model_prediction` costs ~60 s per frame at 3840x2160;
    the cache is rebuilt when the dump header (params path, frames_run), the fitted-params file or the
    requested spot (outside the cached crop) changes. Returns None without a dump."""
    dd = d / "dump"
    if not (dd / "fald_frame.rgba16f").exists(): return None
    import fald_sim_frames as F
    dmeta = load_dump_meta(dd)
    key = {"params": dmeta.get("params"), "frames_run": dmeta.get("frames_run"), "fit_mtime": os.path.getmtime(F.PARAMS), "fit": str(F.PARAMS)}
    f = d / "model_cache.npz"
    if f.exists():
        z = np.load(f, allow_pickle=False)
        ox, oy = (int(v) for v in z["origin"])
        fits = ox + 64 <= mx <= ox + 2 * crop - 64 and oy + 64 <= my <= oy + 2 * crop - 64      # disc/square (±60 px) inside the crop
        if json.loads(str(z["key"])) == key and fits and z["crop_off"].shape == (2 * crop, 2 * crop):
            return {"m_ratio": z["m_ratio"], "m_y": z["m_y"].astype(np.float32), "crop_off": z["crop_off"], "crop_on": z["crop_on"],
                    "origin": (ox, oy), "aperture_px": float(z["aperture_px"]), "dump": {"params": key["params"], "frames_run": key["frames_run"]}, "cached": True}
    img, y_off, y_on, model = model_prediction(dd)
    m_ratio = cv2.resize((y_on / np.maximum(y_off, 1e-3)).astype(np.float32), (CW, CH), interpolation=cv2.INTER_AREA)
    m_y = cv2.resize(y_off.astype(np.float32), (CW, CH), interpolation=cv2.INTER_AREA)
    oy, ox = max(my - crop, 0), max(mx - crop, 0)
    crop_off = y_off[oy:oy + 2 * crop, ox:ox + 2 * crop].astype(np.float32); crop_on = y_on[oy:oy + 2 * crop, ox:ox + 2 * crop].astype(np.float32)
    r = float(model.p.aperture_px)
    assert np.array_equal(aperture_disc(y_off.shape, mx, my, r), model.aperture_mask((mx, my))), "aperture_disc != FaldModel.aperture_mask"
    np.savez(f, m_ratio=m_ratio, m_y=m_y.astype(np.float16), crop_off=crop_off, crop_on=crop_on, origin=np.array([ox, oy]), aperture_px=r, key=json.dumps(key))
    np.save(d / "model_ratio_on_off.npy", m_ratio.astype(np.float16))
    return {"m_ratio": m_ratio, "m_y": m_y, "crop_off": crop_off, "crop_on": crop_on, "origin": (ox, oy), "aperture_px": r,
            "dump": {"params": key["params"], "frames_run": key["frames_run"]}, "cached": False}


def model_spot(mm, mx, my, half=60):
    """Model OFF/ON luminance at the meter spot two ways (§32a finding 4), from `model_maps` crops: the model's
    own aperture DISC (`FaldModel.aperture_mask`, fitted radius ≈ 57 px — what the fit calibrated the meter as
    seeing; `aperture_disc` is the same formula, asserted equal at cache build) and the 120-px SQUARE the first
    meter pass used. The disc is the primary value; the square is kept so the difference stays on record
    (review: 0.38 pp mean, 0.95 pp max on the 16 frames)."""
    ox, oy = mm["origin"]; x, y = mx - ox, my - oy
    yo, yn = mm["crop_off"], mm["crop_on"]
    mask = aperture_disc(yo.shape, x, y, mm["aperture_px"])
    sq = (slice(y - half, y + half), slice(x - half, x + half))
    return {"y_off": float(yo[mask].mean()), "y_on": float(yn[mask].mean()),
            "y_off_square": float(yo[sq].mean()), "y_on_square": float(yn[sq].mean()),
            "aperture_px": float(mm["aperture_px"]), "aperture_area_px": int(mask.sum())}


def load_caps(d, keys=CAP_KEYS):
    """The saved float16 stacks of a frame as float32 BGR (H, W, 3)."""
    return {k: np.load(d / f"cap_{k}.npy").astype(np.float32) for k in keys if (d / f"cap_{k}.npy").exists()}


def _pava(y, w):
    """Weighted pool-adjacent-violators: the non-decreasing sequence closest (weighted L2) to y."""
    blocks = []
    for yi, wi in zip(map(float, y), map(float, w)):
        blocks.append([yi, wi, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop(); v1, w1, c1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2])
    return np.concatenate([[v] * c for v, _, c in blocks])


def _wmedian(x, w):
    o = np.argsort(x); cw = np.cumsum(w[o])
    return float(x[o][np.searchsorted(cw, 0.5 * cw[-1])])


def pair_histogram(cap):
    """2-D histogram of the frame's A/B exposure pairs: rows = B count (rounded, 0..255, B unsaturated in every
    channel), columns = A count in quarter counts (0..1023); OFF/ID/ON pairs pooled. Histograms of frames from
    the same camera session add, so one curve can be fitted on the pooled pairs (`tone_curve_from_hist`)."""
    Hh = np.zeros(256 * 1024, np.int64)
    for s in ("off", "id", "on"):
        b, a = cap[f"{s}_B"], cap[f"{s}_A"]
        ok = b.max(axis=2) < SAT
        bi = np.clip(np.round(b[:, :, 1][ok]).astype(np.int64), 0, 255)
        ai = np.clip(np.round(a[:, :, 1][ok] * 4).astype(np.int64), 0, 1023)
        Hh += np.bincount(bi * 1024 + ai, minlength=256 * 1024)
    return Hh.reshape(256, 1024)


def tone_curve_from_hist(Hh, gain=4.0, toe_hi=48.0, min_px=200, fit_bins=(24, 56)):
    """Camera linearisation from A/B exposure pairs (§32a finding 2), from a `pair_histogram` (one frame or a
    camera session's pooled frames).

    B = A + 2 stops = ×4 light on the same scene, so for every B count c the median A count g(c) is the
    count the camera produces for a quarter of the light that produces c. Where the camera is linear the
    pairs are affine, A = c/4 + 3o/4, with o the black PEDESTAL (zero-light count; negative when the camera
    clamps below black). The 2026-09-12 data show two camera states — capture4 stacks (t 2.5/12.5/17.5/72.5
    + the 19:25 t 692.5): o ≈ +5.5…+6.9, linear to ~64 counts; capture5 stacks (the camera RE-OPENED after
    the COM crash): o ≈ −1…−2.5 with a hard clamp at 2 counts — so o is fitted from the data: weighted median
    of (4·g(c) − c)/3 over the populated bins in `fit_bins` (A ≈ 5–13 counts: above the clamp, below the
    knee). Toe: L(c) = c − o for c ≤ toe_hi; above it L(c) = 4·L(g(c)), composed in ascending c on a
    0.25-count grid (g(c) < c, so each value only references already-computed ones); g is made monotone
    (weighted PAVA over the populated bins), interpolated across empty bins and extrapolated linearly above
    the last populated bin (B clips at 248; dark frames have no pairs at the top — `bins` says where the
    curve is measured, which is why frames are POOLED per camera session: a dark frame alone has too few
    pairs at 16–64 counts to fix its own toe). Above ~64 counts the C922 compresses (×4 light → ×1.8–2.5
    counts), so L rises as c^2–c^3 there.
    L is in "A-exposure counts above black": ratios of L are ratios of light. Returns dict(grid, L, pedestal,
    low_slope [free-fit dA/dc over fit_bins, expect 0.25 — an audit of the ×4 assumption], toe_check_max
    [max |ln(4·L(g(c)) / (c − o))| over the populated B bins toe_hi/2..toe_hi — how well the affine toe
    reproduces the pairs the composition rests on; the data: capture4 0.92–1.16 (worst at B = 24, A ≈ 5 above
    the pedestal), capture5 0.97–1.04; pairs with A closer to black are clamp/pedestal-biased (regression to
    the mode) and are not part of the check], bins [first, last populated], pairs [pixel pairs used],
    toe_check_bins [the bin range checked], exponent {30, 60, 120, 200:
    d ln L / d ln c}, b_over_a). Apply with `apply_curve`.
    """
    n = Hh.sum(axis=1).astype(np.float64)
    cs = np.arange(256, dtype=np.float64)
    g = np.full(256, np.nan)
    for c in range(256):
        if n[c] >= min_px:
            cw = np.cumsum(Hh[c]); g[c] = float(np.searchsorted(cw, 0.5 * cw[-1])) / 4.0
    v = ~np.isnan(g) & (cs >= 4)
    if v.sum() < 20:
        raise ValueError(f"only {int(v.sum())} populated B bins (need 20)")
    sel = v & (cs >= fit_bins[0]) & (cs <= fit_bins[1])
    if sel.sum() < 6:
        raise ValueError(f"only {int(sel.sum())} populated bins in the pedestal-fit range {fit_bins}")
    o = _wmedian((gain * g[sel] - cs[sel]) / (gain - 1.0), n[sel])
    a_free = float(np.polyfit(cs[sel], g[sel], 1, w=np.sqrt(n[sel]))[0])
    # monotone g, then a weighted running mean over the sparse upper bins (a few k pairs per bin at 200+, and
    # coloured highlights whose green channel does not follow the grey ×4 relation put dips in g there; PAVA
    # alone turns a dip into a 10-bin plateau = a flat spot in L = a false local exponent 0). The running mean
    # of a monotone sequence stays monotone; the toe below 32 is dense (≥ 1e5 pairs per bin) and left alone.
    x, gi = cs[v], _pava(g[v], n[v])
    gsm = gi.copy()
    for j, c in enumerate(x):
        half = 3 if c < 64 else 7
        if c < 32: continue
        sel_j = (x >= c - half) & (x <= c + half); ww = n[v][sel_j]
        gsm[j] = float((gi[sel_j] * ww).sum() / ww.sum())
    gi = _pava(gsm, n[v])
    gfull = np.interp(cs, x, gi)
    tail = slice(max(len(x) - 12, 0), None)
    slope = np.polyfit(x[tail], gi[tail], 1)[0] if len(x) >= 4 else 1.0 / gain
    hi = cs > x[-1]; gfull[hi] = gi[-1] + slope * (cs[hi] - x[-1])
    gfull = np.minimum(gfull, 0.9 * cs)                       # progress guarantee for the composition
    grid = np.arange(0.0, 255.0 + 1e-9, 0.25)
    gfine = np.interp(grid, cs, gfull)
    L = np.maximum(grid - o, 0.0)
    for i in np.flatnonzero(grid > toe_hi):
        L[i] = gain * np.interp(gfine[i], grid[:i], L[:i])
    L = np.maximum.accumulate(L)
    # residual dips in g (coloured highlights, sparse bins) compose into 1–4-count plateaus in L; a triangular
    # running mean of ln L over ±2 counts above 16 counts turns them into ramps (the toe below is analytic)
    ker = np.concatenate([np.arange(1, 10), np.arange(8, 0, -1)]).astype(np.float64); ker /= ker.sum()
    lnL = np.log(np.maximum(L, 1e-6)); pad = len(ker) // 2
    sm = np.convolve(np.pad(lnL, pad, mode="edge"), ker, mode="valid")
    L = np.where(grid > 16, np.exp(sm), L)
    L = np.maximum.accumulate(L)
    # self-consistency of the affine toe on the pairs the composition relies on: B bins toe_hi/2..toe_hi, whose
    # A side (≈ toe_hi/8..toe_hi/4 above black) is the part of the toe every composed value above toe_hi rests on
    chk = v & (cs >= toe_hi / 2) & (cs <= toe_hi)
    toe_check = float(np.abs(np.log(gain * np.interp(gfull[chk], grid, L) / np.maximum(cs[chk] - o, 1e-3))).max()) if chk.any() else None
    def exponent(c):
        c0, c1 = c / 1.1, c * 1.1
        l0, l1 = np.interp([c0, c1], grid, L)
        return float(np.log(max(l1, 1e-9) / max(l0, 1e-9)) / np.log(c1 / c0))
    return {"grid": grid, "L": L, "pedestal": o, "low_slope": a_free, "toe_check_max": toe_check, "bins": [int(x[0]), int(x[-1])],
            "toe_check_bins": [int(cs[chk][0]), int(cs[chk][-1])] if chk.any() else None,
            "pairs": int(n.sum()), "exponent": {c: exponent(c) for c in (30, 60, 120, 200)},
            "b_over_a": {int(c): float(c / max(g[c], 1e-3)) for c in (8, 16, 32, 64, 128, 200) if v[c]}}


def camera_tone_curve(cap, **kw):
    """Tone curve from ONE frame's pairs (the per-frame audit; `build_curves` pools per camera session)."""
    return tone_curve_from_hist(pair_histogram(cap), **kw)


def camera_session_of(name, meta, sessions):
    """Which Camera open a frame's stacks came from: capture_meta `cam_session` (written by capture() since
    the review), else camera_sessions.json in OUT (hand-reconstructed for older stacks), else "all"."""
    return meta.get("frames", {}).get(name, {}).get("cam_session") or sessions.get(name) or "all"


def build_curves(names, meta=None, log_each=True):
    """Per-frame pair histograms -> one tone curve per camera session (pooled) + the per-frame audit curve.
    Returns (curve_by_name, audit_by_name, session_by_name). Frames without stacks are skipped."""
    meta = meta if meta is not None else (json.load(open(OUT / "capture_meta.json")) if (OUT / "capture_meta.json").exists() else {"frames": {}})
    sessions = json.load(open(OUT / "camera_sessions.json")) if (OUT / "camera_sessions.json").exists() else {}
    pooled, audit, sess_of = {}, {}, {}
    for name in names:
        d = OUT / name
        if not (d / "cap_on_B.npy").exists(): continue
        Hh = pair_histogram(load_caps(d))
        s = camera_session_of(name, meta, sessions); sess_of[name] = s
        pooled[s] = pooled.get(s, 0) + Hh
        try:
            a = tone_curve_from_hist(Hh)
            audit[name] = {k: v for k, v in a.items() if k not in ("grid", "L")}
            if log_each: log(f"[curve] {name} (session {s}) own pairs: {curve_summary(a)}")
        except Exception as exc:
            audit[name] = {"error": str(exc)}
            if log_each: log(f"[curve] {name} (session {s}) own pairs: no curve ({exc})")
    curves = {}
    for s, Hh in pooled.items():
        try:
            curves[s] = tone_curve_from_hist(Hh)
            log(f"[curve] session {s} POOLED ({sum(1 for k in sess_of if sess_of[k] == s)} frames): {curve_summary(curves[s])}")
        except Exception as exc:
            log(f"[curve] session {s}: no pooled curve ({exc})")
    return {k: curves.get(sess_of[k]) for k in sess_of}, audit, sess_of


def apply_curve(counts, curve):
    """Counts (any shape) → relative linear light on the frame's curve (A-exposure counts above black)."""
    return np.interp(counts, curve["grid"], curve["L"]).astype(np.float32)


def curve_summary(curve):
    e = curve["exponent"]
    tc = curve["toe_check_max"]
    return (f"pedestal {curve['pedestal']:+.2f} counts, free slope {curve['low_slope']:.3f} (expect 0.25), toe check {tc if tc is None else round(tc, 3)}, B bins {curve['bins'][0]}..{curve['bins'][1]} ({curve['pairs']/1e6:.1f}M pairs), "
            f"local exponent @30 {e[30]:.2f} @60 {e[60]:.2f} @120 {e[120]:.2f} @200 {e[200]:.2f}, "
            "B/A " + " ".join(f"{c}:{r:.2f}" for c, r in curve["b_over_a"].items()))


def camera_spot(d, mx, my, curve=None, half=30):
    """Camera ON/ID at the meter spot (§32a finding 3). 60-camera-px window at (mx/2, my/2); the window passes
    the ratio_map validity rule when no channel of any pixel of either stack reaches SAT (248) and both
    green means exceed VALID_MIN (6 counts); exposure B is used if valid there (dark-surround exposure, best
    SNR), else A, else the column is n/a (valid=False). `ratio` = raw-count ratio of window means (the first
    meter pass's definition, now gated); `ratio_lin` = the same through the frame's tone curve when given."""
    cy, cx = my // 2, mx // 2
    sl = (slice(cy - half, cy + half), slice(cx - half, cx + half))
    tried = {}
    for tag in ("B", "A"):
        fo, fi = d / f"cap_on_{tag}.npy", d / f"cap_id_{tag}.npy"
        if not (fo.exists() and fi.exists()): continue
        on = np.load(fo).astype(np.float32)[sl]; idn = np.load(fi).astype(np.float32)[sl]
        mx_ = float(max(on.max(), idn.max())); mean_id = float(idn[:, :, 1].mean()); mean_on = float(on[:, :, 1].mean())
        tried[tag] = {"max": mx_, "mean_id": mean_id, "mean_on": mean_on}
        if mx_ >= SAT or mean_id <= VALID_MIN or mean_on <= VALID_MIN: continue
        rl = None
        if curve is not None:
            rl = float(apply_curve(on[:, :, 1], curve).mean() / max(apply_curve(idn[:, :, 1], curve).mean(), 1e-6))
        return {"ratio": mean_on / mean_id, "ratio_lin": rl, "exposure": tag, "valid": True, "window": tried}
    return {"ratio": None, "ratio_lin": None, "exposure": None, "valid": False, "window": tried}


def meter_camera_column(name, mx, my, meter_json=None, curve=None):
    """Offline recomputation of `meter()`'s camera column for one saved frame: old value (meter_result.json:
    exposure B, ungated) vs the gated/exposure-chosen `camera_spot` value, raw and linearised (`curve` = the
    session tone curve from `build_curves`; built from the frame alone when not given). Prints and returns."""
    d = OUT / name
    old = (meter_json or json.load(open(OUT / "meter_result.json"))).get(name, {}).get("camera_on_id")
    if curve is None:
        curve = build_curves([name], log_each=False)[0].get(name)
    cam = camera_spot(d, mx, my, curve)
    f = lambda r: "   n/a  " if r is None else f"{100*(r-1):+7.2f} %"
    log(f"[recheck] {name}: camera ON/ID old {f(old)}  new {f(cam['ratio'])} (raw, expo {cam['exposure'] or '-'}, valid {cam['valid']})  linear {f(cam['ratio_lin'])}"
        + "".join(f"  [{t}: max {w['max']:.0f} meanID {w['mean_id']:.1f}]" for t, w in cam["window"].items()))
    return {"old": old, "new": cam["ratio"], "new_lin": cam["ratio_lin"], "valid": cam["valid"], "exposure": cam["exposure"], "window": cam["window"]}


def recheck(ts=None):
    """Offline (no hardware): for the frames in meter_result.json, the recomputed camera column
    (`meter_camera_column`) and the model at the spot through the aperture disc vs the 120-px square
    (`model_spot`). Writes meter_camera_recheck.json."""
    mres = json.load(open(OUT / "meter_result.json"))
    curves, audit, sess_of = build_curves(list(mres), log_each=False)
    out = {}
    for name, rec in mres.items():
        mx, my = rec.get("meter_xy", meter_xy())
        row = {"t": rec.get("t"), "camera_session": sess_of.get(name), "camera": meter_camera_column(name, mx, my, mres, curves.get(name))}
        Y = lambda k: rec["xyz"][k][1]
        row["meter_on_id"] = Y("on") / Y("id") if rec.get("xyz", {}).get("on") and rec["xyz"].get("id") else None
        mm = model_maps(OUT / name, mx, my)
        if mm is not None:
            m = model_spot(mm, mx, my); row["model"] = m
            rd, rs = m["y_on"] / m["y_off"], m["y_on_square"] / m["y_off_square"]
            old = rec.get("model") or {}
            log(f"[recheck] {name}: model ON/OFF disc(r={m['aperture_px']:.1f}px) {100*(rd-1):+7.2f} %  square(120px) {100*(rs-1):+7.2f} %  diff {100*(rd-rs):+5.2f} pp"
                f"  | OFF nits disc {m['y_off']:.2f} square {m['y_off_square']:.2f}" + (f"  (meter pass square {old['y_on']/old['y_off']*100-100:+.2f} %)" if old else "")
                + (f"  | meter ON/ID {100*(row['meter_on_id']-1):+7.2f} %" if row["meter_on_id"] else ""))
        out[name] = row
        json.dump(out, open(OUT / "meter_camera_recheck.json", "w"), indent=1)
    return 0


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


def ratio_map(num, den, sigma=4.0, curve=None):
    """Smoothed ratio of two camera stacks (green channel, the least demosaic-affected); valid where both are
    well inside the 8-bit range (den > 6: at 48-frame stacks the noise floor is ~0.5 counts; no channel ≥ 248).
    With `curve` (camera_tone_curve) both stacks are linearised BEFORE the blur and the ratio, and the > 6
    floor applies to the linearised value (counts ABOVE the pedestal — a pixel at the pedestal carries no
    light); saturation is always judged on the raw counts."""
    ng, dg = num[:, :, 1], den[:, :, 1]
    if curve is not None:
        ng, dg = apply_curve(ng, curve), apply_curve(dg, curve)
    n = cv2.GaussianBlur(ng, (0, 0), sigma); d = cv2.GaussianBlur(dg, (0, 0), sigma)
    valid = (d > VALID_MIN) & (n > VALID_MIN) & (num.max(axis=2) < SAT) & (den.max(axis=2) < SAT)
    return np.where(valid, n / np.maximum(d, 1e-3), 1.0), valid


def _phase(ref, img):
    (dx, dy), resp = cv2.phaseCorrelate(np.float32(ref - ref.mean()), np.float32(img - img.mean()))
    return [round(float(dx), 3), round(float(dy), 3)]


def register_stacks(cap, agree_px=0.3, min_px=0.2, max_px=1.5):
    """Sub-pixel registration of the state stacks (§32a finding 7). Per exposure: phase-correlation shifts of
    ID / ON (/ OFF2 at A) against OFF. A geometry shift (camera/panel moved between captures) moves ID and ON
    by the SAME amount; a content change (the layer's own darkening/brightening biasing the correlator) does
    not. So: if the OFF→ID and OFF→ON estimates agree within `agree_px` and their mean is in
    [min_px, max_px), ID and ON are both warped onto OFF by that mean shift (branch "common_off_warp"; the
    ON/ID ratio is unchanged by this, ID/OFF becomes registered). If they agree and the shift is below
    min_px nothing is done ("none"). If they DISAGREE the frame is flagged and the previous one-sided
    fallback applies — ON registered onto ID when 0.2 < |shift| < 1.5 px ("on_vs_id_fallback"; this is the
    judgment that moved the sparks row, corr 0.33 → 0.66) — or nothing ("none_disagree"). Warps in place;
    returns (shifts, registration) with the estimates and the branch per exposure."""
    shifts, reg = {}, {}
    for tag in ("A", "B"):
        ref = cap[f"off_{tag}"][:, :, 1]
        for s in ("id", "on") + (("off2",) if tag == "A" else ()):
            shifts[f"{s}_{tag}"] = _phase(ref, cap[f"{s}_{tag}"][:, :, 1])
        shifts[f"on_{tag}_vs_id"] = _phase(cap[f"id_{tag}"][:, :, 1], cap[f"on_{tag}"][:, :, 1])
        d_id, d_on = np.array(shifts[f"id_{tag}"]), np.array(shifts[f"on_{tag}"])
        rel = np.array(shifts[f"on_{tag}_vs_id"])
        agree = float(np.abs(d_id - d_on).max()) <= agree_px
        common = (d_id + d_on) / 2; mag = float(np.abs(common).max())
        r = {"agree": agree, "id_minus_on_px": [round(float(v), 3) for v in (d_id - d_on)], "warp_dx_dy": None, "flagged": not agree}
        if agree and min_px <= mag < max_px:
            M = np.float32([[1, 0, -common[0]], [0, 1, -common[1]]])
            for s in ("id", "on"):
                cap[f"{s}_{tag}"] = cv2.warpAffine(cap[f"{s}_{tag}"], M, (CW, CH), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            r.update(branch="common_off_warp", warp_dx_dy=[round(float(v), 3) for v in common])
        elif agree:
            r.update(branch="none" if mag < min_px else "none_large_shift", flagged=mag >= max_px)
        elif min_px < float(np.abs(rel).max()) < max_px:
            M = np.float32([[1, 0, -rel[0]], [0, 1, -rel[1]]])
            cap[f"on_{tag}"] = cv2.warpAffine(cap[f"on_{tag}"], M, (CW, CH), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            r.update(branch="on_vs_id_fallback", warp_dx_dy=[round(float(v), 3) for v in rel])
        else:
            r.update(branch="none_disagree")
        reg[tag] = r
    return shifts, reg


def find_blobs(gA, k=3, min_area=200):
    """Centroids of the k largest bright regions (top 0.5 % of a stack) — highlight cores to profile."""
    thr = np.percentile(gA, 99.5)
    m = (gA >= max(thr, 60)).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    order = sorted(range(1, n), key=lambda i: -st[i, cv2.CC_STAT_AREA])
    return [(cen[i], st[i]) for i in order if st[i, cv2.CC_STAT_AREA] >= min_area][:k]


def analyze(ts, raw=False):
    """Per frame: linearise (unless raw), register, ratio maps, model comparison by region, panel figure.
    Writes analysis_summary.json (analysis_summary_raw.json with --raw) and rewrites the cap_*.png previews
    from the npy stacks in the correct (BGR) order."""
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    summary = {}; mx, my = meter_xy()
    meta = json.load(open(OUT / "capture_meta.json")) if (OUT / "capture_meta.json").exists() else {"frames": {}}
    mode = "raw counts" if raw else "linear light"
    log(f"[an] camera ratios in {mode}" + ("" if raw else " (tone curve from the A/B pairs pooled per camera session; --raw for encoded counts)"))
    curves, audit, sess_of = ({}, {}, {}) if raw else build_curves([tsname(t) for t in ts], meta)
    for t in ts:
        name = tsname(t); d = OUT / name
        if not (d / "cap_on_A.npy").exists():
            log(f"[an] {name}: no capture"); continue
        cap = load_caps(d)
        for k, arr in cap.items():        # previews: the stacks are BGR, which is cv2.imwrite's native order (finding 9)
            cv2.imwrite(str(d / f"cap_{k}.png"), np.clip(arr, 0, 255).astype(np.uint8))
        # camera tone curve (session-pooled A/B pairs; the frame's own-pairs fit is kept as the audit)
        curve = None if raw else curves.get(name); curve_rec = None
        if not raw:
            if curve is None:
                log(f"[an] {name}: WARNING no tone curve for session {sess_of.get(name)} — this frame falls back to raw counts")
            else:
                curve_rec = {"session": sess_of.get(name), **{k: v for k, v in curve.items() if k not in ("grid", "L")}, "own_pairs": audit.get(name)}
                np.save(d / "cam_tone_curve.npy", np.stack([curve["grid"], curve["L"]]).astype(np.float32))
                e = curve["exponent"]
                log(f"[an] {name}: camera curve (session {sess_of.get(name)}) local exponent @30 {e[30]:.2f} @60 {e[60]:.2f} @120 {e[120]:.2f} @200 {e[200]:.2f}, pedestal {curve['pedestal']:+.2f}")
        # sub-pixel registration (finding 7): common OFF warp when ID and ON agree, else the one-sided fallback
        shifts, reg = register_stacks(cap)
        # ratio maps (camera): ON/ID = the correction through the panel; ID/OFF = overlay-path cost; OFF2/OFF = drift/noise
        r_on_id_A, v1 = ratio_map(cap["on_A"], cap["id_A"], curve=curve); r_on_id_B, v2 = ratio_map(cap["on_B"], cap["id_B"], curve=curve)
        r_id_off_B, v3 = ratio_map(cap["id_B"], cap["off_B"], curve=curve); r_drift, v4 = ratio_map(cap["off2_A"], cap["off_A"], curve=curve)
        # merged ON/ID: exposure B where valid (dark surround), else A
        r_on_id = np.where(v2, r_on_id_B, r_on_id_A); valid = v1 | v2
        id_used = np.where(v2, cap["id_B"][:, :, 1], cap["id_A"][:, :, 1])      # raw ID counts at the exposure the merge used
        # model prediction for the same frame, resampled onto the camera grid (cached per frame: model_maps)
        mm = model_maps(d, mx, my)
        rows = {}; glare = None
        if mm is not None:
            m_ratio, m_y = mm["m_ratio"], mm["m_y"]
            # regions: where the model predicts a change (|ratio-1| > 2 %) split by sign, vs untouched
            # camera veiling glare is a frame-wide additive term that changes when the layer changes the bright
            # content; the median ON/ID over model-untouched pixels estimates that global offset → "norm" columns.
            # Finding 6: glare is additive, the normalisation multiplicative — only meaningful when the untouched
            # set is large and lit (≥ 20k px with ID ≥ 20 counts), never on pillarbox black; raw medians stay primary.
            unt = (np.abs(m_ratio - 1) < 0.005) & valid
            unt_lit = unt & (id_used >= 20)
            glare = float(np.median(r_on_id[unt_lit])) if unt_lit.sum() >= 20000 else None
            for label, sel in (("model_darken", (m_ratio < 0.98) & valid), ("model_brighten", (m_ratio > 1.02) & valid),
                               ("model_untouched", unt)):
                if sel.sum() < 500: continue
                med = float(np.median(r_on_id[sel]))
                rows[label] = {"px": int(sel.sum()), "model_mean": float(m_ratio[sel].mean() - 1), "model_median": float(np.median(m_ratio[sel]) - 1),
                               "cam_on_id_mean": float(r_on_id[sel].mean() - 1), "cam_on_id_median": med - 1,
                               "cam_on_id_norm_median": (med / glare - 1) if glare is not None else None,
                               "cam_id_off_mean": float(r_id_off_B[sel & v3].mean() - 1) if (sel & v3).sum() > 100 else None,
                               "cam_drift_mean": float(r_drift[sel & v4].mean() - 1) if (sel & v4).sum() > 100 else None}
            both = valid & (np.abs(m_ratio - 1) > 0.01)
            corr = float(np.corrcoef(m_ratio[both] - 1, r_on_id[both] - 1)[0, 1]) if both.sum() > 1000 else None
            dump_info = mm["dump"]
            log(f"[an] {name}: dump params {dump_info['params']}  frames_run {dump_info['frames_run']}" + ("  (model from cache)" if mm["cached"] else ""))
        else:
            m_ratio = None; corr = None; dump_info = None
        # halo profiles across the brightest blobs (exposure B, horizontal + vertical lines through the centroid)
        gA = cap["off_A"].mean(axis=2)
        blobs = find_blobs(gA)
        fig, axes = plt.subplots(2, 3, figsize=(18, 9))
        ax = axes.ravel()
        ax[0].imshow(np.clip(cap["off_B"], 0, 255).astype(np.uint8)[:, :, ::-1]); ax[0].set_title(f"{name}: camera OFF (exposure B)")   # BGR → RGB for matplotlib
        ax[1].imshow(diverging(r_on_id - 1, 0.15, valid)[:, :, ::-1]); ax[1].set_title(f"camera ON/ID − 1  (±15 %, red = brighter with layer; {mode})")
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
            strip = np.ascontiguousarray(np.concatenate(stretched, axis=1))
            cv2.putText(strip, "OFF        |        ID        |        ON   (stretched to p90)", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
            cv2.imwrite(str(d / "crop_blob0.png"), strip)          # BGR stacks → written as-is
        cv2.imwrite(str(d / "ratio_on_id.png"), diverging(r_on_id - 1, 0.15, valid))
        if m_ratio is not None: cv2.imwrite(str(d / "ratio_model.png"), diverging(m_ratio - 1, 0.15, np.ones_like(valid)))
        drift = float(np.median(r_drift[v4]) - 1) if v4.any() else None
        dcorr = dump_matches_video(d / "dump", t)
        summary[name] = {"t": t, "camera_linearised": curve is not None, "camera_curve": curve_rec, "corr_model_vs_camera": corr, "regions": rows,
                         "glare_norm_ref": glare, "drift_median": drift, "shifts_px": shifts, "registration": reg,
                         "registration_flagged": any(r["flagged"] for r in reg.values()), "dump": dump_info, "dump_vs_video_corr": dcorr,
                         "on_id_valid_frac": float(valid.mean()), "cam_on_id_p1_p99": [float(np.percentile(r_on_id[valid], q) - 1) for q in (1, 99)] if valid.any() else None,
                         **meta["frames"].get(name, {})}
        log(f"[an] {name}: corr(model, camera) {corr}  drift {drift}  on/id p1..p99 {summary[name]['cam_on_id_p1_p99']}  shifts {shifts}  dump-vs-video corr {dcorr}")
        log(f"      registration A: {reg['A']['branch']} warp {reg['A']['warp_dx_dy']} (ID−ON {reg['A']['id_minus_on_px']} px)  B: {reg['B']['branch']} warp {reg['B']['warp_dx_dy']} (ID−ON {reg['B']['id_minus_on_px']} px)"
            + ("  FLAGGED: OFF→ID and OFF→ON disagree > 0.3 px" if summary[name]["registration_flagged"] else ""))
        pct = lambda v: ("%+6.2f" % (100 * v)) if v is not None else "  n/a "
        for k, v in rows.items():
            log(f"      {k:<16} px {v['px']:8d}  model mean {100*v['model_mean']:+6.2f} % median {100*v['model_median']:+6.2f} %  camera ON/ID mean {100*v['cam_on_id_mean']:+6.2f} % median {100*v['cam_on_id_median']:+6.2f} % glare-norm {pct(v['cam_on_id_norm_median'])} %"
                f"  ID/OFF {pct(v['cam_id_off_mean'])} %  drift {pct(v['cam_drift_mean'])} %")
    json.dump(summary, open(OUT / ("analysis_summary_raw.json" if raw else "analysis_summary.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
    flags = {a for a in sys.argv[2:] if a.startswith("--")}
    ts = [float(a) for a in sys.argv[2:] if not a.startswith("--")] or DEFAULT_TS
    if cmd == "analyze":
        sys.exit(analyze(ts, raw="--raw" in flags))
    sys.exit({"capture": capture, "dumps": dumps, "geom": geom, "meter": meter, "recheck": recheck}[cmd](ts))
