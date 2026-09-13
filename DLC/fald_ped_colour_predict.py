"""Per-channel pedestal (work guide H2): frozen model predictions for the HW gate (law 11: predictions before
measurements). No hardware; writes results/fald_native_2026-09-11/ped_colour/predictions.json.

For each pattern the model predicts what the i1D3 at (1988, 1120) reads — X, Y, Z in nits — for OFF (no
correction), ON with the white pedestal (GUI toggle off, the shipped behaviour) and ON with the per-channel
pedestal (toggle on, `pa32ucxr_fald_panel_chanped.bin`). The panel model is the same in all three (the
coloured-pedestal FORWARD model, so OFF carries the blue leak); only the correction differs. Reported per
pattern: luminance ratios ON/OFF for both modes (should agree: the colour does not move Y) and the chromaticity
error u'v' vs the same field's FLAT reading (what the mode is supposed to fix: the far-field grey's own colour).

Patterns (the §33 halo rows + the §4 dim-orange next-to-highlight set, native codes):
  grey g in {2, 5, 20} nits: flat; 600-nit bar at 120 / 240 / 480 px right of the meter
  orange (BT.2020 linear 1.0/0.40/0.10 scaled) at 0.5 / 2 / 5 / 20 nits: flat; 200-px white window 110 px right
Usage: PYTHONPATH=src python fald_ped_colour_predict.py
"""
import json, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from dlc.fald.model import FaldModel                      # noqa: E402
from dlc.fald.correct import load_fitted_params, correct_image   # noqa: E402
from dlc._pq import oetf_norm                             # noqa: E402
from dataclasses import replace                           # noqa: E402

R = ROOT / "results/fald_native_2026-09-11"
OUT = R / "ped_colour"; OUT.mkdir(exist_ok=True)
METER = (1988.0, 1120.0)
FULL = (0.0, 0.0, 1.0, 1.0)
W, H = 3840.0, 2160.0
# panel native primaries (probe log) + grey-field white -> RGB->XYZ for the report (the model works in per-channel nits)
_prim = dict(rx=0.693447, ry=0.301577, gx=0.190593, gy=0.74442, bx=0.152107, by=0.063229); _wx, _wy = 0.3235, 0.3283
_xyY = lambda x, y: np.array([x / y, 1.0, (1 - x - y) / y])
_P = np.stack([_xyY(_prim['rx'], _prim['ry']), _xyY(_prim['gx'], _prim['gy']), _xyY(_prim['bx'], _prim['by'])], axis=1)
RGB2XYZ = _P * np.linalg.solve(_P, _xyY(_wx, _wy))     # R=G=B=1 -> white with Y = 1


def rect(x0, y0, w, h):
    return (x0 / W, y0 / H, w / W, h / H)


def code(nits):
    return int(round(oetf_norm(max(float(nits), 0.0) / 10000.0) * 1023))


def orange_codes(nits):
    lin = np.array([1.0, 0.40, 0.10]); lin = lin / lin.max() * nits
    return tuple(code(v) for v in lin)


def to_xyz(y_rgb, w):
    """per-channel nits (3,) from FaldModel.meter -> XYZ nits. A channel's nits share is w_c of white, so the
    panel-RGB linear value is y_c / w_c (white = 1 at 1 nit total)."""
    rgb = np.asarray(y_rgb) / np.asarray(w)
    return RGB2XYZ @ rgb


def upvp(xyz):
    X, Y, Z = xyz; d = X + 15 * Y + 3 * Z
    return (4 * X / d, 9 * Y / d) if d > 0 else (float("nan"), float("nan"))


def main():
    p_chan = load_fitted_params(R / "fald_fit_result_area_chanped.json")
    p_white = replace(p_chan, ped_mode="white")
    m_chan, m_white = FaldModel(p_chan), FaldModel(p_white)
    forward = m_chan                                     # the PANEL: coloured leak in every case
    w = np.array(p_chan.chan_weights)
    pats = []
    for g in (2.0, 5.0, 20.0):
        c = code(g); bg = ((c, c, c), FULL)
        pats.append((f"g{g:g}_flat", [bg], [bg]))
        for gap in (120, 240, 480):
            pats.append((f"g{g:g}_bar{gap}", [bg, ((code(600),) * 3, rect(1988 + gap, 0, 400, 2160))], [bg]))
    for n in (0.5, 2.0, 5.0, 20.0):
        oc = orange_codes(n); bg = (oc, FULL)
        pats.append((f"orange{n:g}_flat", [bg], [bg]))
        pats.append((f"orange{n:g}_win110", [bg, ((1023,) * 3, rect(1988 + 110, 1120 - 100, 200, 200))], [bg]))
    out = {"meter": METER, "params": "fald_fit_result_area_chanped.json", "tmin_rgb": list(p_chan.tmin_rgb),
           "note": "XYZ nits the meter should read; ON_white = GUI toggle off (shipped rule), ON_channel = toggle on. "
                   "du'v' = vs the same field's FLAT reading (OFF). Luminance ratios must agree between modes.",
           "patterns": {}}
    for name, shapes, flat_shapes in pats:
        img = forward.render(shapes)
        y_off = forward.meter_img(img, METER)
        y_flat = forward.meter(flat_shapes, METER)
        rw = correct_image(m_white, img)["req"]; rc = correct_image(m_chan, img)["req"]
        y_w = forward.meter_img(rw, METER); y_c = forward.meter_img(rc, METER)
        X = {k: to_xyz(v, w) for k, v in (("off", y_off), ("on_white", y_w), ("on_channel", y_c), ("flat", y_flat))}
        uf = upvp(X["flat"])
        row = {k: [round(float(v), 5) for v in X[k]] for k in X}
        row["Y_ratio_on_white"] = round(float(X["on_white"][1] / X["off"][1]), 4) if X["off"][1] > 0 else None
        row["Y_ratio_on_channel"] = round(float(X["on_channel"][1] / X["off"][1]), 4) if X["off"][1] > 0 else None
        for k in ("off", "on_white", "on_channel"):
            u, v = upvp(X[k]); row[f"duv_{k}_vs_flat"] = round(float(np.hypot(u - uf[0], v - uf[1])), 5)
        out["patterns"][name] = row
        print(f"{name:18s} Y off {X['off'][1]:8.3f}  on/off white {row['Y_ratio_on_white']}  channel {row['Y_ratio_on_channel']}  "
              f"du'v' vs flat: off {row['duv_off_vs_flat']:.4f}  white {row['duv_on_white_vs_flat']:.4f}  channel {row['duv_on_channel_vs_flat']:.4f}")
    json.dump(out, open(OUT / "predictions.json", "w"), indent=1)
    print("wrote", OUT / "predictions.json")


if __name__ == "__main__":
    main()
