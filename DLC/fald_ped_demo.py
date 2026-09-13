"""Per-channel pedestal DEMO (eye test, no meter): a frame built to make the toggle's effect as large as it can be.

Why a plain desktop shows nothing: the toggle only changes the COLOUR of the pedestal term, which is ~1-2 nits
as-if-white next to a highlight, and on grey both modes subtract nearly the same amount. The difference is largest
on DIM SATURATED colours with one channel at zero next to a bright object: the white rule cannot subtract anything
there (the darkest channel is 0, so the common factor is 0), the per-channel rule subtracts the red/green excess.

Frame (PQ PNG, 3840x2160): three full-white (code 1023, ~1842-nit) bars (200 px wide, at x = 960 / 1920 / 2880) and between them four
zones at 0.5 / 1 / 2 / 5 nits (the dark regime the colour part is about); eight horizontal colour rows (270 px each) run across the whole width: grey, yellow,
orange, red, green, cyan, magenta, blue (max channel = the zone level, other channels 0 or 0.4), so every colour
touches every bar. The influence reaches ~5 cells (400 px) from a bar, i.e. most of each 760-px zone.

What to look at (layer ON, debug view 0): with the toggle OFF the dim yellow/orange/red/green stripes next to the bar
carry the panel's blue leak on top of their own light and look lifted and desaturated toward the bar; with the toggle
ON their red/green content is pulled down by the modelled leak excess (they get slightly darker and cleaner near the
bar), the blue leak itself cannot be removed (physics). Debug view 6 shows |per-channel - white| x100 directly: black
= the toggle changes nothing; the coloured glow around the bar is where and how much (1 nit shows as 100 nits).
Debug view 5 shows the pedestal term that is actually applied in the current mode (x100).

Usage (DesktopLUT running on a build >= 2026-09-13 evening, HDR, overlay path, monitor 0):
  PYTHONPATH="src;." python fald_ped_demo.py show            build the frame, present it fullscreen (mpv), set the
                                                              chanped panel file + layer ON; leaves mpv open
  PYTHONPATH="src;." python fald_ped_demo.py ab [sec] [n]    toggle ped 0/1 every sec (3) s, n (6) cycles, prints state
  PYTHONPATH="src;." python fald_ped_demo.py view <0..6>     set the debug view (5 pedestal term, 6 influence)
  PYTHONPATH="src;." python fald_ped_demo.py predict         offline: the model's influence image (|req_ch - req_wh| x100)
                                                              and per-stripe numbers -> results/.../ped_colour/demo_*
  PYTHONPATH="src;." python fald_ped_demo.py chroma <gain> [lo hi]
                                                              re-export the chanped panel file IN PLACE with the colour
                                                              part of the pedestal term at <gain> x model strength and its
                                                              own pixel-luminance fade lo..hi nits (omit = NO fade); the
                                                              running app rebuilds within ~2 s. 1 = model strength (about
                                                              3x what the 0.5-nit dark-halo rows measured); try 3, 10.
  PYTHONPATH="src;." python fald_ped_demo.py restore         put the previous panel file back, layer as it was
The demo does NOT enter native (the other layers stay as they are: this is an eye test through the normal stack).
Env: FALD_MPV_SCREEN (0), FALD_DEMO_BIN (the FLD2 panel file), FALD_OUT.
"""
import json, os, sys, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "results/fald_native_2026-09-11/sim"))

R = ROOT / "results/fald_native_2026-09-11"
OUT = Path(os.environ.get("FALD_OUT", str(R / "ped_colour"))); OUT.mkdir(parents=True, exist_ok=True)
BIN = Path(os.environ.get("FALD_DEMO_BIN", str(R / "pa32ucxr_fald_panel_chanped.bin")))
FIT = R / "fald_fit_result_area_chanped.json"
STATE = OUT / "demo_prev_state.json"
W, H = 3840, 2160
MON = 0
BAR_NITS = 1842.0                                                     # code 1023 = native peak: the strongest leak
BARS = ((960, 1160), (1920, 2120), (2880, 3080))                        # x0, x1 of the three white bars
ZONES = ((0.5, 0, 960), (1.0, 1160, 1920), (2.0, 2120, 2880), (5.0, 3080, 3840))   # level, x0, x1 (the dark regime)
STRIPES = [("grey", (1, 1, 1)), ("yellow", (1, 1, 0)), ("orange", (1, 0.4, 0)), ("red", (1, 0, 0)),
           ("green", (0, 1, 0)), ("cyan", (0, 1, 1)), ("magenta", (1, 0, 1)), ("blue", (0, 0, 1))]
ROW_H = H // len(STRIPES)


def build_frame() -> np.ndarray:
    img = np.zeros((3, H, W), np.float64)
    for si, (_, rgb) in enumerate(STRIPES):
        y0, y1 = si * ROW_H, (si + 1) * ROW_H
        for lvl, x0, x1 in ZONES:
            for c in range(3):
                img[c, y0:y1, x0:x1] = lvl * rgb[c]
    for x0, x1 in BARS:
        img[:, :, x0:x1] = BAR_NITS
    return img


def frame_png() -> Path:
    import fald_ab_frames as AB
    png = OUT / "demo_frame.png"
    if not png.exists():
        AB.write_png(png, build_frame())
    return png


def controller():
    from dlc.controller import CalibrationController
    return CalibrationController.connect()


def layer_state(c):
    return (c.state().get("layers", {}) or {}).get(f"{MON}:HDR", {})


def cmd_show():
    c = controller()
    st = layer_state(c)
    if not STATE.exists():
        json.dump({"fald": st.get("fald"), "params_path": st.get("fald_params_path"), "ped_mode": st.get("fald_ped_mode"),
                   "debug_mode": st.get("fald_debug_mode")}, open(STATE, "w"), indent=1)
        print("[demo] previous layer state saved to", STATE)
    if not BIN.exists():
        raise SystemExit(f"[demo] panel file missing: {BIN} (run: python -m dlc.fald.export {FIT} {BIN})")
    r = c.call("runtime.set_fald_params", {"monitor": MON, "mode": "HDR", "params_path": str(BIN)})
    print("[demo] params ->", r)
    r = c.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": 0, "ped_mode": 1})
    print("[demo] debug/ped ->", r)
    if r.get("ped_colour_in_file") is False:
        raise SystemExit("[demo] the running DesktopLUT reports no leak colour in the panel file: wrong file or an old exe")
    c.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": True})
    from fald_ramp_probe import MpvPresenter
    png = frame_png()
    pres = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")))
    pres.cmd("loadfile", str(png), "replace")
    if not pres.wait_loaded(str(png)):
        raise SystemExit("[demo] mpv did not load the frame")
    print("[demo] frame up on screen", int(os.environ.get("FALD_MPV_SCREEN", "0")), "- layer ON, per-channel ON. "
          "Flip the GUI checkbox or run: fald_ped_demo.py ab 3 6 | view 6")
    print("[demo] state:", json.dumps(layer_state(c)))
    print("[demo] mpv stays open (close it yourself or run 'restore'); this process exits without killing it")
    pres.proc = None            # detach: do not kill mpv on exit


def cmd_ab(period=3.0, n=6):
    c = controller()
    for i in range(n):
        for ped in (0, 1):
            r = c.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "ped_mode": ped})
            tag = "PER-CHANNEL" if ped else "WHITE      "
            note = "" if r.get("ped_colour_in_file") else "   (NO COLOUR IN FILE: no-op)"
            print(f"cycle {i + 1}/{n}: pedestal {tag}{note}", flush=True)
            time.sleep(period)
    c.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "ped_mode": 1})
    print("left PER-CHANNEL")


def cmd_view(mode: int):
    c = controller()
    print(json.dumps(c.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": int(mode)}), indent=1))


def cmd_chroma(gain: float, lo=None, hi=None):
    """Re-export the FLD2 demo file with the colour-part gain/fade and tell the app to reload it."""
    from dataclasses import replace
    from dlc.fald.model import FaldModel
    from dlc.fald.correct import load_fitted_params
    from dlc.fald.export import export_panel_params
    p = load_fitted_params(FIT)
    fade = (0.0, 0.0) if lo is None else (float(lo), float(hi))
    p = replace(p, ped_mode="channel", ped_chroma_gain=float(gain), ped_chroma_lum_fade=fade)
    info = export_panel_params(FaldModel(p), BIN)
    print(f"[demo] exported {BIN.name}: colour part x{gain:g}, fade {fade} ({info['format']}, {info['bytes']} bytes)")
    try:
        c = controller()
        print("[demo] reload ->", c.call("runtime.set_fald_params", {"monitor": MON, "mode": "HDR", "params_path": str(BIN)}))
        print("[demo] ped ->", c.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "ped_mode": 1}))
    except Exception as exc:  # noqa: BLE001
        print("[demo] app not reachable, file written only:", exc)


def cmd_restore():
    c = controller()
    if not STATE.exists():
        print("[demo] nothing saved"); return
    prev = json.load(open(STATE))
    if prev.get("params_path"):
        c.call("runtime.set_fald_params", {"monitor": MON, "mode": "HDR", "params_path": prev["params_path"]})
    c.call("runtime.fald_debug", {"monitor": MON, "mode": "HDR", "debug_mode": int(prev.get("debug_mode") or 0),
                                  "ped_mode": int(prev.get("ped_mode") or 0)})
    c.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": bool(prev.get("fald"))})
    STATE.unlink()
    print("[demo] restored:", json.dumps(layer_state(c)))


def cmd_predict():
    """Offline: what the toggle changes in the model, on this frame — the same quantity debug view 6 shows."""
    from dataclasses import replace
    from dlc.fald.model import FaldModel
    from dlc.fald.correct import load_fitted_params, correct_image
    import fald_ab_frames as AB
    p_ch = load_fitted_params(FIT); p_wh = replace(p_ch, ped_mode="white")
    import struct
    if BIN.exists():                                              # mirror the demo file's colour-part knob
        hdr = BIN.read_bytes()[:160]
        if len(hdr) >= 160 and struct.unpack("<I", hdr[:4])[0] == 0x464C4432:
            g, lo, hi = struct.unpack("<3f", hdr[144:156])
            if g > 0: p_ch = replace(p_ch, ped_chroma_gain=g, ped_chroma_lum_fade=(lo, hi))
    print(f"[predict] colour part: gain x{p_ch.ped_chroma_gain:g}, fade {p_ch.chroma_lum_fade()}")
    frame_png()                                                      # the frame itself, next to the prediction
    scale = 5
    m_ch, m_wh = FaldModel(replace(p_ch, scale=scale)), FaldModel(replace(p_wh, scale=scale))
    img = build_frame()[:, ::scale, ::scale]                        # the model works on a reduced grid
    req_ch = correct_image(m_ch, img)["req"]; req_wh = correct_image(m_wh, img)["req"]
    diff = np.abs(req_ch - req_wh)
    influence = np.minimum(diff * 100.0, p_ch.white_nits)          # x100 nits per nit, like debug view 6
    full = np.repeat(np.repeat(influence, scale, axis=1), scale, axis=2)[:, :H, :W]
    AB.write_png(OUT / "demo_influence_model.png", full)
    # per colour row and zone: the mean change per channel over the 400 px of the zone adjacent to a bar (the zone's
    # right edge for the first three zones, the left edge for the last) at the row's vertical centre
    rows = []
    for si, (name, _) in enumerate(STRIPES):
        yc = (si * ROW_H + ROW_H // 2) // scale
        for zi, (lvl, zx0, zx1) in enumerate(ZONES):
            if zi < 3: x0, x1 = (zx1 - 400) // scale, zx1 // scale
            else:      x0, x1 = zx0 // scale, (zx0 + 400) // scale
            d = (req_ch - req_wh)[:, yc - 5:yc + 5, x0:x1].mean(axis=(1, 2))
            a = (req_wh - img)[:, yc - 5:yc + 5, x0:x1].mean(axis=(1, 2))      # what white mode already does there
            rows.append({"stripe": name, "zone": zi, "level": lvl, "req_change_nits_rgb": [round(float(v), 4) for v in d],
                         "white_mode_change_nits_rgb": [round(float(v), 4) for v in a], "abs_max": round(float(np.abs(d).max()), 4)})
            print(f"{name:8s} {lvl:5.1f} nits  white mode adj R G B {a[0]:+.3f} {a[1]:+.3f} {a[2]:+.3f}   "
                  f"per-channel minus white {d[0]:+.3f} {d[1]:+.3f} {d[2]:+.3f}")
    json.dump({"frame": str(OUT / 'demo_frame.png'), "bar_nits": BAR_NITS, "rows": rows,
               "note": "req_change = corrected request per-channel mode minus white mode, mean over the 400 px of the "
                       "stripe nearest the bar; debug view 6 shows |change| x100 on screen"}, open(OUT / "demo_predict.json", "w"), indent=1)
    print("wrote", OUT / "demo_influence_model.png", "and demo_predict.json")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "chroma" and len(argv) == 4: raise SystemExit("chroma <gain> [lo hi]: give both fade bounds or neither")
    if cmd == "show": cmd_show()
    elif cmd == "ab": cmd_ab(float(argv[2]) if len(argv) > 2 else 3.0, int(argv[3]) if len(argv) > 3 else 6)
    elif cmd == "view": cmd_view(int(argv[2]))
    elif cmd == "predict": cmd_predict()
    elif cmd == "chroma": cmd_chroma(float(argv[2]), *(argv[3:5] if len(argv) > 4 else ()))
    elif cmd == "restore": cmd_restore()
    else: raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
