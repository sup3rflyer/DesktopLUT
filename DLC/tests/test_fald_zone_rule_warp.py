"""Opt-in: the black-frame boost's ZONE RULE (work guide C12b) on a real D3D device — the C++ WARP case
(tests/test_fald.cpp "FALD temporal modes on WARP") fed with a frame file, its dumped zone flags / zone counts / boosts
replayed against the GPU-order twin (gpuemu.Emu). The frame holds the refit's 64 observations, one per zone, on black,
plus a bright 2 x 2-zone block (so round 1 is a CORRECTED frame), on a 12 x 12 lattice of 80 x 45-px zones.

Write the inputs, run the case, replay (PowerShell):
    python tests/test_fald_zone_rule_warp.py <dir> --rule mean          # panel.bin + frame.rgba16f + zone_rule.json + d0..d6
    $env:FALD_TEST_WARP_DIR = "<dir>"; $env:FALD_TEST_WARP_MODE = "0"; bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"
    python -m pytest tests/test_fald_zone_rule_warp.py -n0
`--rule dim` writes the legacy LIT-or-DIM file (word 53 = 0): its dumps must be byte-identical between a build from before
C12b and one after (compare the directories). Without the variable / the marker file the test is skipped."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
W, H, ZW, ZH = 960, 540, 80, 45
# N (of 144) -> boost: steps placed so that the two rules' counts of the frame land on different steps
LUT = ((0.0, 1.178), (20 / 144, 1.15), (40 / 144, 1.12), (46 / 144, 1.09), (50 / 144, 1.06), (54 / 144, 1.03), (100 / 144, 1.0))


def _params(rule: str) -> FaldParams:
    return FaldParams(width=W, height=H, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, tmin=1.5e-3,
                      boost_lut=LUT, boost_rule=rule)


def frame_half(params: FaldParams) -> np.ndarray:
    """(H, W, 4) float16 scRGB: observation i in zone i (row-major), a 500-nit block in zones (8..9, 5..6), alpha 1."""
    from test_fald_zone_rule import OBS
    nits_of = params.code_to_nits(np.arange(1024))
    nits = np.zeros((H, W))
    for i, (_, rects, bg, _, _) in enumerate(OBS):
        zr, zc = divmod(i, 12)
        z = nits[zr * ZH:(zr + 1) * ZH, zc * ZW:(zc + 1) * ZW]
        z[:] = nits_of[bg]
        for x0, y0, w, h, c in rects:
            z[y0:y0 + h, x0:x0 + w] = nits_of[c]
    nits[8 * ZH:10 * ZH, 5 * ZW:7 * ZW] = 500.0
    out = np.ones((H, W, 4), dtype=np.float16)
    out[:, :, :3] = (nits / 80.0).astype(np.float16)[:, :, None]      # grey: as-if-white nits = scRGB x 80 (PQ transfer)
    return out


def write_inputs(root: Path, rule: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    p = _params(rule)
    info = export_panel_params(FaldModel(p), root / "panel.bin")
    frame_half(p).tofile(root / "frame.rgba16f")
    (root / "zone_rule.json").write_text(json.dumps({"rule": rule, "format": info["format"]}))
    for i in range(7):
        (root / f"d{i}").mkdir(exist_ok=True)


@pytest.mark.skipif(not _DIR or not (Path(_DIR) / "zone_rule.json").exists() or not (Path(_DIR) / "d0" / "fald_dump.txt").exists(),
                    reason="FALD_TEST_WARP_DIR with zone-rule WARP dumps not given")
def test_warp_zone_flags_counts_and_boosts_equal_the_twin():
    from test_fald_zone_rule import OBS
    root = Path(_DIR)
    rule = json.loads((root / "zone_rule.json").read_text())["rule"]
    o = read_panel_file(root / "panel.bin")
    assert o["boostRule"] == (1 if rule == "mean" else 0) and o["hasBoost"]
    frame = np.fromfile(root / "frame.rgba16f", dtype=np.float16).reshape(H, W, 4)
    ref = Emu(o, width=W, height=H).run(frame[:, :, :3].astype(np.float32), fp16_out=True)
    for run in (0, 6):                                                # the same frame every run (temporal mode 0)
        d = root / f"d{run}"
        t = (d / "fald_dump.txt").read_text()
        g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)   # noqa: E731
        assert np.array_equal(np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(H, W, 4), frame)
        a0 = np.fromfile(d / "fald_active_r0.f32", dtype=np.float32).reshape(12, 12)
        a1 = np.fromfile(d / "fald_active.f32", dtype=np.float32).reshape(12, 12)
        assert np.array_equal(a0 > 0.5, ref["active0"]) and np.array_equal(a1 > 0.5, ref["active1"])
        assert (int(g("active_zones_r0")), int(g("active_zones_r1"))) == (ref["zones0"], ref["zones1"])
        assert float(np.float32(float(g("boost_r0")))) == pytest.approx(ref["boost0"], rel=1e-5)
        assert float(np.float32(float(g("boost_r1")))) == pytest.approx(ref["boost1"], rel=1e-5)
        if "boost_rule" in t:                                         # builds since C12b
            assert int(g("boost_rule")) == o["boostRule"]
    # the device's verdict on every scored observation (round 0 = the source frame)
    a0 = np.fromfile(root / "d0" / "fald_active_r0.f32", dtype=np.float32).reshape(12, 12) > 0.5
    wrong = [ob[0] for i, ob in enumerate(OBS) if ob[4] != "ambiguous" and bool(a0[divmod(i, 12)]) != ob[3]]
    assert wrong == ([] if rule == "mean" else ["seed40x23_c29", "seed40x23_c37", "seed40x23_c64", "seed57x32_c37"])
    print(f"rule {rule}: N r0 {ref['zones0']} / r1 {ref['zones1']}, boost {ref['boost0']:.4f} / {ref['boost1']:.4f}; wrong: {wrong}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--rule", choices=("dim", "mean"), default="mean")
    a = ap.parse_args()
    write_inputs(Path(a.dir), a.rule)
    print("wrote", a.dir, a.rule)
