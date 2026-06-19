"""HDR MHC build (the Advanced-Color dummy ICC in simulation): build_mhc on a PQ/Rec.2020
raw panel derives the primaries + native-white→D65 matrix but leaves the base grayscale
**identity** — the 3D-LUT cube owns HDR tone (v2-design-notes §7/§8). Pure-ish: the panel
reads need no numpy, but build_curves_from_ti3 is stdlib."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from dlc.engine.patches import Transfer
from dlc.measure_loop import MeasurePatch, SyntheticPanel
from dlc.runs import create_run
from dlc.stages import _common, build_mhc
from dlc.stages.simulate import _DEFAULTS

# The ramp structure build_mhc accepts (grays + per-channel ramps + CMY), as in
# simulation.write_synthetic_ti3 — but measured by the PQ panel instead of an sRGB one.
_RGB_ROWS = [
    (0.0, 0.0, 0.0), (0.25, 0.25, 0.25), (0.5, 0.5, 0.5), (0.75, 0.75, 0.75), (1.0, 1.0, 1.0),
    (0.25, 0.0, 0.0), (0.5, 0.0, 0.0), (0.75, 0.0, 0.0), (1.0, 0.0, 0.0),
    (0.0, 0.25, 0.0), (0.0, 0.5, 0.0), (0.0, 0.75, 0.0), (0.0, 1.0, 0.0),
    (0.0, 0.0, 0.25), (0.0, 0.0, 0.5), (0.0, 0.0, 0.75), (0.0, 0.0, 1.0),
    (1.0, 1.0, 0.0), (1.0, 0.0, 1.0), (0.0, 1.0, 1.0),
]


def _write_hdr_raw_ti3(path: Path) -> Path:
    transfer = Transfer.pq(bit_depth=10)
    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0,
                           native_white_nits=1840.0)  # perfect Rec.2020/PQ panel
    max_cv = transfer.max_cv
    rows = []
    for s in _RGB_ROWS:
        rgb = tuple(round(c * max_cv) for c in s)
        x, y, z = panel(MeasurePatch(label="p", rgb=rgb, signal=s, bit_depth=10)).xyz
        rows.append(f"{s[0] * 100:.6f} {s[1] * 100:.6f} {s[2] * 100:.6f} {x:.6f} {y:.6f} {z:.6f}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        "CTI3", "# Synthetic HDR (PQ/Rec.2020) raw measurement",
        "BEGIN_DATA_FORMAT", "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z", "END_DATA_FORMAT",
        f"NUMBER_OF_SETS {len(rows)}", "BEGIN_DATA", *rows, "END_DATA", "",
    ]), encoding="utf-8")
    return path


def _ns(ctx, **over) -> Namespace:
    return Namespace(**{**_DEFAULTS, "run": ctx.root, **over})


def test_hdr_build_mhc_leaves_base_grayscale_identity(tmp_path):
    ctx = create_run("HDR", display="test", run_dir=tmp_path / "run")
    ti3 = _write_hdr_raw_ti3(tmp_path / "raw_hdr.ti3")

    result = build_mhc.build(_ns(ctx, mode="HDR", is_hdr=True, source_ti3=str(ti3)), ctx)
    assert result.status == "ran"

    # The base grayscale is identity (every per-channel deviation ~1.0) — the cube owns tone.
    base = _common.load_dlc_state(ctx)["mhc_params"]["base_grayscale"]
    for ch in ("r", "g", "b"):
        assert all(abs(d - 1.0) < 1e-9 for d in base["deviations"][ch]), (ch, base["deviations"][ch])

    # A Rec.2020 panel does not trip the gamut tell (the reference is Rec.2020, not sRGB).
    assert not any(a.code == "wide_gamut" for a in result.anomalies)
    # The matrix still carries the measured native primaries (~Rec.2020) + a D65 white.
    mp = result.metrics["measured_primaries"]
    assert abs(mp["rx"] - 0.708) < 0.02 and abs(mp["gy"] - 0.797) < 0.02


def test_sdr_build_mhc_still_derives_a_real_base_grayscale(tmp_path):
    # Guard: the HDR branch must not change SDR — an sRGB panel still gets a fitted base.
    from dlc.simulation import write_synthetic_ti3

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    ti3 = write_synthetic_ti3(tmp_path / "raw_sdr.ti3")
    result = build_mhc.build(_ns(ctx, mode="SDR", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    # Perfect sRGB panel → near-identity base, but it went through the real proposal path.
    assert result.metrics["base_grayscale_max_abs_deviation"] is not None
