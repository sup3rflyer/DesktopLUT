"""Camera test of the cell LATTICE the model predicts on busy content (2026-09-12).

The live A/B shows a cell-periodic grid in the layer's gain view and faintly in the corrected image.
The model produces that lattice from cell-to-cell drive variation (phase-shifted, support-limited
estimate); whether the REAL panel has it, and how strongly, has never been measured — the i1D3
aperture (~100-160 px) cannot resolve an 80×45-px lattice, the webcam can (≈2 screen px per camera px).

Frame: a 100-nit field with a 300-nit 40×22-px patch centred in every other cell (checkerboard of
drives). The background between patches is where the model predicts a lattice (OFF) and a flat
field (ON). Camera captures OFF and ON (layer toggled over the pipe) plus black; the background's
80×45-periodic modulation amplitude is measured and compared with the model's prediction.

Prereqs: DesktopLUT overlay mode, HDR, layer configured; webcam sees the whole panel (LG dark);
mpv on PATH. State: FALD_NATIVE=1 recommended (neutral + identity MHC, restored on exit).
Usage: PYTHONPATH="src;." FALD_NATIVE=1 python fald_cam_lattice.py
"""
import json, os, sys, time
from pathlib import Path
import numpy as np, cv2
from dataclasses import replace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "results/fald_native_2026-09-11/sim"))
import agent_fald_leak_probe as P
import fald_sim_frames as F
import fald_ab_frames as AB
from fald_ramp_probe import MpvPresenter, SETTLE
from fald_cam import Camera, find_panel_corners, warp
from dlc.controller import CalibrationController
from dlc.fald.model import FaldModel
from dlc.fald.correct import load_fitted_params, correct_image

OUT = Path(os.environ.get("FALD_OUT", str(ROOT / "results/fald_native_2026-09-11/camera_lattice")))
OUT.mkdir(parents=True, exist_ok=True)
W, H, CW, CH = 3840, 2160, 80, 45
FIELD, PATCH = 100.0, 300.0
MON = 0


def lattice_frame():
    """Checkerboard of WHOLE cells: (r+c) even = PATCH nits, odd = FIELD nits. The plain (FIELD) cells are
    the analysis region; their central 60x33 px window keeps clear of the camera blur of the neighbours."""
    img = np.full((3, H, W), FIELD, np.float64)
    mask = np.zeros((H, W), bool)                     # True = excluded from the background analysis
    for r in range(48):
        for c in range(48):
            x0, y0 = c * CW, r * CH
            if (r + c) % 2 == 0:
                img[:, y0:y0 + CH, x0:x0 + CW] = PATCH
                mask[y0:y0 + CH, x0:x0 + CW] = True
            else:
                mask[y0:y0 + CH, x0:x0 + CW] = True
                mask[y0 + 6:y0 + CH - 6, x0 + 10:x0 + CW - 10] = False   # central window of the plain cell
    return img, mask


def fold_amplitude(y, mask, border_cells=4):
    """Fold every interior PLAIN cell onto one 80x45 cell; returns (asymmetry dict, profile, valid).
    Asymmetry = (right third - left third) / mean and (bottom third - top third) / mean of the folded
    plain-cell window: the signature of the phase-shifted estimate, insensitive to symmetric camera blur."""
    ys, xs = slice(border_cells * CH, H - border_cells * CH), slice(border_cells * CW, W - border_cells * CW)
    sub = y[ys, xs]; m = ~mask[ys, xs]
    fold = np.zeros((CH, CW)); cnt = np.zeros((CH, CW))
    yy, xx = np.mgrid[0:sub.shape[0], 0:sub.shape[1]]
    np.add.at(fold, ((yy[m] + border_cells * CH) % CH, (xx[m] + border_cells * CW) % CW), sub[m])
    np.add.at(cnt, ((yy[m] + border_cells * CH) % CH, (xx[m] + border_cells * CW) % CW), 1)
    prof = fold / np.maximum(cnt, 1)
    valid = cnt > 0
    win = prof[6:CH - 6, 10:CW - 10]
    mean = win.mean(); w3 = win.shape[1] // 3; h3 = win.shape[0] // 3
    asym = {"lr": float((win[:, -w3:].mean() - win[:, :w3].mean()) / mean),
            "tb": float((win[-h3:, :].mean() - win[:h3, :].mean()) / mean),
            "pp": float((win.max() - win.min()) / mean)}
    return asym, prof, valid


def main():
    img, mask = lattice_frame()
    png = OUT / "lattice_frame.png"; AB.write_png(png, img)
    grey = OUT / "grey_frame.png"; AB.write_png(grey, np.full((3, H, W), 90.0))
    # model prediction (panel output OFF / ON, background modulation)
    model = FaldModel(replace(load_fitted_params(F.PARAMS), scale=1)); p = model.p
    y_off = model.forward_img(img)["y"].sum(axis=0)
    req = np.clip(correct_image(model, img, iters=2)["req"], 0, p.white_nits)
    y_on = model.forward_img(req)["y"].sum(axis=0)
    a_off, prof_off, _ = fold_amplitude(y_off, mask); a_on, prof_on, _ = fold_amplitude(y_on, mask)
    fmt = lambda a: f"L/R {100*a['lr']:+.2f} %  T/B {100*a['tb']:+.2f} %  p-p {100*a['pp']:.2f} %"
    P.log(f"[model] plain-cell asymmetry OFF: {fmt(a_off)} | ON: {fmt(a_on)}  (plain mean OFF {y_off[~mask].mean():.1f} nit)")
    np.save(OUT / "model_prof_off.npy", prof_off); np.save(OUT / "model_prof_on.npy", prof_on)

    ctrl = CalibrationController.connect()
    native = os.environ.get("FALD_NATIVE") == "1"
    if native: P.enter_native(ctrl)
    P.audit_or_refuse(ctrl, allow_stack=os.environ.get("FALD_ALLOW_STACK") == "1")
    if P.stack_layers(ctrl)[2].get("active"):
        raise SystemExit("[lattice] DWM hook active — overlay layer cannot run")

    def set_fald(on):
        ctrl.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": on}); time.sleep(0.8)

    pres = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")))
    cam = Camera()
    res = {}
    try:
        set_fald(False)
        pres.cmd("loadfile", str(grey), "replace"); time.sleep(SETTLE + 1.0)
        cam.set_manual(exposure=-6, gain=0)
        corners = find_panel_corners(cam.grab(6))
        np.save(OUT / "panel_corners.npy", corners)
        P.log(f"[cam] corners {np.round(corners, 1).tolist()}")
        # exposure search on the lattice frame: background must sit mid-range, patches unclipped
        pres.cmd("loadfile", str(png), "replace"); time.sleep(SETTLE + 1.0)
        chosen = -8
        for expo in (-6, -7, -8, -9, -10):
            cam.set_manual(exposure=expo, gain=0)
            t = warp(cam.grab(4), corners, W, H); tl = t.mean(axis=2) if t.ndim == 3 else t
            p99, p50 = np.percentile(tl, 99), np.percentile(tl, 50)
            P.log(f"[cam] exposure {expo}: p50 {p50:.0f} p99 {p99:.0f}")
            chosen = expo
            if p99 < 225 and p50 > 40: break
        cam.set_manual(exposure=chosen, gain=0)
        P.log(f"[cam] using exposure {chosen}")
        caps = {}
        for label, on in (("off", False), ("on", True), ("off2", False)):
            set_fald(on)
            pres.cmd("loadfile", str(png), "replace"); time.sleep(SETTLE + 1.5)
            raw = cam.grab(48)
            w = warp(raw, corners, W, H)      # panel-registered at full resolution (camera ~2 px per screen px)
            np.save(OUT / f"cap_{label}.npy", w.astype(np.float32)); caps[label] = w
            cv2.imwrite(str(OUT / f"cap_{label}.png"), np.clip(w / max(w.max(), 1) * 255, 0, 255).astype(np.uint8))
        set_fald(False)
        for label, w in caps.items():
            # warp() returns a panel-registered image; resample to 3840x2160 if needed
            if w.shape[:2] != (H, W):
                w = cv2.resize(w, (W, H), interpolation=cv2.INTER_LINEAR)
            wl = w.mean(axis=2) if w.ndim == 3 else w
            a, prof, _ = fold_amplitude(wl.astype(np.float64), mask)
            res[label] = {**a, "bg_mean": float(wl[~mask].mean())}
            np.save(OUT / f"cam_prof_{label}.npy", prof)
            P.log(f"[cam] {label:<4} plain-cell asymmetry {fmt(a)}  (camera units, plain mean {wl[~mask].mean():.1f})")
        P.log(f"[verdict] OFF: model {fmt(a_off)} | camera {fmt(res['off'])} / repeat {fmt(res['off2'])}")
        P.log(f"[verdict] ON : model {fmt(a_on)} | camera {fmt(res['on'])}")
    finally:
        try: set_fald(False)
        except Exception: pass
        cam.stop(); pres.close()
        json.dump({"model": {"off": a_off, "on": a_on}, "camera": res, "native": native}, open(OUT / "lattice_result.json", "w"), indent=1, default=float)
        if native:
            try: ctrl.exit_calibration(restore_snapshot=True); P.log("[restore] stack restored")
            except Exception as exc: P.log(f"[restore] failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
