"""Fit the panel's LED time constants from a high-frame-rate video of the FALD test clip's toggle segment.

    python -m dlc.fald.led_step_fit clip.mp4 --roi X,Y,W,H [--fps 240] [--skip 0] [--out fit.json]

The clip (results/fald_temporal_2026-09-15/fald_temporal_clip.mp4, segment A) toggles a white block on a 5-nit
grey field once a second. Film the HALO region beside the block (not the block itself: the LCD there is at
full opening either way and saturates the camera) with the layer OFF, exposure and focus locked, at the highest
frame rate the phone offers (240 fps slow-motion), then give this tool the crop around that region. It decodes
the video with ffmpeg (no OpenCV needed), averages the crop's luma per frame, finds every rise and fall, and fits
each with a first-order step (dlc.fald.temporal.fit_step_response). Output: tau_rise / tau_fall in ms with the
instant-step residual for comparison — if the instant fit is not clearly worse, the panel is instant at this
frame rate and the LED-lag filter has no physical basis (leave it off).

Camera caveats: slow-motion modes re-time the clip, so pass --fps as the RECORDING rate; rolling shutter smears
a step over ~1 frame; auto exposure must be locked or the fit sees the camera, not the panel. A first-order fit
cannot see a pure PIPELINE DELAY (the LEDs switching a frame or two after the LCD): without a time reference in the
frame a delayed step fits as "instant" at a later t0. To measure it, give --block-roi (a crop on the block itself;
use a 30-50 % grey block so the sensor does not clip): the tool then reports t0(halo) - t0(block) per edge in panel
frames (--panel-hz) — the LCD data changes at t0(block), the light at t0(halo). The halo step's OVERSHOOT (a dip or
bump beyond the new plateau) is the discriminator between the two shader modes: a monotone step = LEDs and the
panel's own compensation move together (mode 1); an overshoot = the LCD opening moves before the light (mode 2).
Frame indices in the report count from --skip.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import numpy as np

from .temporal import fit_step_response


def read_luma(video: str, roi: tuple[int, int, int, int] | None, skip: int = 0) -> np.ndarray:
    """Mean luma (0..255) of the crop per frame, decoded through ffmpeg (rawvideo gray)."""
    vf = []
    if roi is not None:
        x, y, w, h = roi
        vf.append(f"crop={w}:{h}:{x}:{y}")
    if skip > 0:
        vf.append(f"select=gte(n\\,{skip})")
    cmd = ["ffmpeg", "-loglevel", "error", "-i", video]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", video], capture_output=True, text=True, check=True).stdout.strip()
    vw, vh = (int(v) for v in probe.split(",")[:2])
    w, h = (roi[2], roi[3]) if roi is not None else (vw, vh)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = w * h
    means = []
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n:
            break
        means.append(float(np.frombuffer(buf, dtype=np.uint8).mean()))
    proc.wait()
    return np.asarray(means)


def find_edges(y: np.ndarray, min_gap: int = 20) -> list[tuple[int, str]]:
    """Frame indices where the trace crosses the midpoint between its low and high plateaus, with direction."""
    lo, hi = np.percentile(y, 10), np.percentile(y, 90)
    if hi - lo < 1.0:
        return []
    mid = 0.5 * (lo + hi)
    above = y > mid
    edges = []
    last = -min_gap
    for i in range(1, y.size):
        if above[i] != above[i - 1] and i - last >= min_gap:
            edges.append((i, "rise" if above[i] else "fall"))
            last = i
    return edges


def overshoot(t: np.ndarray, y: np.ndarray, fit: dict) -> float:
    """How far the trace goes BEYOND the new plateau in the step direction, as a fraction of the step height
    (0 = monotone first-order; > ~0.15 = the panel's compensation leads its light, the mode-2 shape)."""
    step = fit["y1"] - fit["y0"]
    if abs(step) < 1e-9:
        return 0.0
    after = y[t >= fit["t0"]]
    if after.size == 0:
        return 0.0
    beyond = (after.max() - fit["y1"]) if step > 0 else (fit["y1"] - after.min())
    return float(max(0.0, beyond) / abs(step))


def fit_edges(y: np.ndarray, fps: float, edges, pre: float = 0.05, post: float = 0.6) -> list[dict]:
    out = []
    n_pre, n_post = int(pre * fps), int(post * fps)
    for i, kind in edges:
        a, b = max(0, i - n_pre), min(y.size, i + n_post)
        t = (np.arange(a, b) - i) / fps
        fit = fit_step_response(t, y[a:b])
        fit.update(kind=kind, frame=i, tau_ms=fit["tau"] * 1000.0, overshoot=overshoot(t, y[a:b], fit))
        out.append(fit)
    return out


def summarise(fits: list[dict], fps: float = 240.0) -> dict:
    res = {}
    for kind in ("rise", "fall"):
        k = [f for f in fits if f["kind"] == kind]
        if not k:
            continue
        taus = np.array([f["tau_ms"] for f in k])
        ratio = np.array([f["instant_rms"] / max(f["rms"], 1e-12) for f in k])
        over = np.array([f.get("overshoot", 0.0) for f in k])
        # a camera-integrated instant step fits tau ~ one sample with a poor instant residual (rolling shutter, exposure
        # time): only a tau of 2-3 samples or more is evidence of lag (review 2026-09-17)
        lag = np.median(ratio) > 2.0 and np.median(taus) > 2.5 * 1000.0 / fps
        res[kind] = {"n": len(k), "tau_ms_median": float(np.median(taus)), "tau_ms_all": [float(v) for v in taus],
                     "instant_over_fit_rms_median": float(np.median(ratio)), "overshoot_median": float(np.median(over)),
                     "verdict": ("lag" if lag else "instant (or unresolved at this frame rate)"),
                     "shape": ("overshoot: compensation leads the light (mode 2 shape)" if np.median(over) > 0.15
                               else "monotone (mode 1 shape)")}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("video")
    ap.add_argument("--roi", help="X,Y,W,H crop (pixels) around the halo region beside the block")
    ap.add_argument("--fps", type=float, default=None, help="recording frame rate (slow-motion clips are re-timed; give the real one)")
    ap.add_argument("--skip", type=int, default=0, help="frames to skip at the start")
    ap.add_argument("--out", help="write the per-edge fits + summary as JSON")
    ap.add_argument("--block-roi", help="X,Y,W,H crop on the toggling block itself: adds the LED-vs-LCD delay estimate")
    ap.add_argument("--panel-hz", type=float, default=60.0, help="the panel's refresh rate, for the delay in panel frames")
    args = ap.parse_args(argv)
    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
    if roi is not None and len(roi) != 4:
        ap.error("--roi needs X,Y,W,H")
    fps = args.fps
    if fps is None:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                            "-of", "csv=p=0", args.video], capture_output=True, text=True, check=True).stdout.strip()
        num, den = r.split("/"); fps = float(num) / float(den)
    y = read_luma(args.video, roi, args.skip)
    edges = find_edges(y)
    fits = fit_edges(y, fps, edges)
    summary = summarise(fits, fps)
    if args.block_roi:
        broi = tuple(int(v) for v in args.block_roi.split(","))
        yb = read_luma(args.video, broi, args.skip)
        bfits = fit_edges(yb, fps, find_edges(yb))
        delays = []
        for f in fits:                                   # pair each halo edge with the nearest block edge of the same kind
            same = [b for b in bfits if b["kind"] == f["kind"]]
            if not same:
                continue
            b = min(same, key=lambda b: abs(b["frame"] - f["frame"]))
            if abs(b["frame"] - f["frame"]) < 0.5 * fps:
                d_s = (f["frame"] + f["t0"] * fps - (b["frame"] + b["t0"] * fps)) / fps
                delays.append({"kind": f["kind"], "frame": f["frame"], "delay_ms": d_s * 1000.0, "delay_panel_frames": d_s * args.panel_hz})
        summary["delay"] = {"n": len(delays), "edges": delays,
                            "delay_panel_frames_median": float(np.median([d["delay_panel_frames"] for d in delays])) if delays else None}
    print(f"{y.size} frames at {fps:.1f} fps, {len(edges)} edges (frame indices count from --skip)")
    for f in fits:
        print(f"  {f['kind']:4s} @ frame {f['frame']:5d}: tau {f['tau_ms']:6.1f} ms  y {f['y0']:.1f} -> {f['y1']:.1f}  "
              f"rms {f['rms']:.3f} (instant step {f['instant_rms']:.3f})  overshoot {f['overshoot']:.2f}")
    for kind in ("rise", "fall"):
        if kind in summary:
            s = summary[kind]
            print(f"{kind}: n {s['n']}, tau median {s['tau_ms_median']:.1f} ms, instant/fit rms {s['instant_over_fit_rms_median']:.2f} "
                  f"-> {s['verdict']}; {s['shape']}")
    if "delay" in summary:
        d = summary["delay"]
        print(f"LED-vs-LCD delay: n {d['n']}, median {d['delay_panel_frames_median']} panel frames"
              + "".join(f"\n  {e['kind']:4s} @ {e['frame']:5d}: {e['delay_ms']:+6.1f} ms = {e['delay_panel_frames']:+.2f} frames" for e in d["edges"]))
    if args.out:
        json.dump({"fps": fps, "frames": int(y.size), "edges": fits, "summary": summary, "trace": y.tolist()}, open(args.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
