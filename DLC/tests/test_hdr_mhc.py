"""HDR MHC build: build_mhc on a PQ/Rec.2020 raw panel derives the primaries +
native-white→D65 matrix AND a full-resolution per-channel 1D .cube EOTF correction
(the MHC base — DesktopLUT bakes it into the 4096-entry HDR MHC2 LUT via set_base_lut).
The 32-point set_base_grayscale table stays identity plumbing (it's too sparse for a PQ
EOTF; reserved for GS+WB post-fixes). This SUPERSEDES the old "3D LUT owns the HDR neutral
axis" design. Pure-ish: the panel reads need no numpy, but build_curves_from_ti3 is stdlib."""

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
    (0.25, 0.0, 0.0), (0.5, 0.0, 0.0), (0.75, 0.0, 0.0), (1.0, 0.0, 0.0),
    (0.0, 0.25, 0.0), (0.0, 0.5, 0.0), (0.0, 0.75, 0.0), (0.0, 1.0, 0.0),
    (0.0, 0.0, 0.25), (0.0, 0.0, 0.5), (0.0, 0.0, 0.75), (0.0, 0.0, 1.0),
    (1.0, 1.0, 0.0), (1.0, 0.0, 1.0), (0.0, 1.0, 1.0),
]


def _write_hdr_raw_ti3(path: Path, *, gray_steps: int = 5) -> Path:
    transfer = Transfer.pq(bit_depth=10)
    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0,
                           native_white_nits=1840.0)  # perfect Rec.2020/PQ panel
    max_cv = transfer.max_cv
    rows = []
    gray = [(i / (gray_steps - 1),) * 3 for i in range(gray_steps)]
    for s in [*gray, *_RGB_ROWS]:
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


def _parse_cube(path: Path):
    size = None
    rows = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith("LUT_1D_SIZE"):
            size = int(ln.split()[1])
        elif ln and ln[0].isdigit():
            rows.append([float(x) for x in ln.split()])
    return size, rows


def test_hdr_build_mhc_builds_per_channel_eotf_cube(tmp_path):
    ctx = create_run("HDR", display="test", run_dir=tmp_path / "run")
    ti3 = _write_hdr_raw_ti3(tmp_path / "raw_hdr.ti3")

    result = build_mhc.build(_ns(ctx, mode="HDR", is_hdr=True, source_ti3=str(ti3)), ctx)
    assert result.status == "ran"

    params = _common.load_dlc_state(ctx)["mhc_params"]

    # The 32-point base grayscale stays identity plumbing (the cube is authoritative).
    base = params["base_grayscale"]
    assert base["point_count"] == 32
    for ch in ("r", "g", "b"):
        assert all(abs(d - 1.0) < 1e-9 for d in base["deviations"][ch]), (ch, base["deviations"][ch])

    # The HDR base EOTF rides a real per-channel 1D .cube (set_base_lut path).
    base_lut = params["base_lut"]
    assert base_lut and base_lut["peak_nits"] > 1000.0
    size, rows = _parse_cube(base_lut["cube_path"])
    assert size and size >= 256 and len(rows) == size
    assert all(len(r) == 3 for r in rows)                      # per-channel R G B
    # A perfect PQ panel → ~identity cube (monotone, spans [0,1], endpoints anchored).
    for ch in range(3):
        col = [r[ch] for r in rows]
        assert col[0] <= 1e-3 and col[-1] >= 0.99
        assert all(col[i] <= col[i + 1] + 1e-6 for i in range(size - 1))

    # Option-1 cube cap: the cube REPORTS the post-cube deliverable peak (= the achievable-D65
    # Peak-Chroma cap when the cold channel binds below target, else the target) — the number that
    # feeds DesktopLUT's tonemapTargetPeak. base_lut.peak_nits must equal that, never exceed native.
    pc = params["peak_chroma"]
    assert "cube_peak_nits" in pc and "capped" in pc
    assert base_lut["peak_nits"] == pc["cube_peak_nits"]                 # reports the post-cube peak
    assert pc["cube_peak_nits"] <= pc["native_peak_nits"] + 1e-6         # never above native peak
    if pc["capped"]:
        assert pc["cube_peak_nits"] == pc["cap_nits"]                    # capped to the D65-achievable peak
        assert abs(base_lut["summary"]["white_max_nits"] - pc["cap_nits"]) < 1.0  # cube tops out there

    # A Rec.2020 panel does not trip the gamut tell (the reference is Rec.2020, not sRGB).
    assert not any(a.code == "wide_gamut" for a in result.anomalies)
    # The matrix still carries the measured native primaries (~Rec.2020) + a D65 white.
    mp = result.metrics["measured_primaries"]
    assert abs(mp["rx"] - 0.708) < 0.02 and abs(mp["gy"] - 0.797) < 0.02


def test_hdr_build_mhc_cube_peak_follows_resolved_peak(tmp_path):
    # Task C / one-source-of-truth: when the orchestrator hands build-mhc a RESOLVED max-sustained
    # peak, the cube's neutral ceiling + the C++ handoff peak derive from it (NOT an independent
    # raw-TI3 max), and the Peak-Chroma cap stays a SEPARATE cap on the neutral axis.
    ctx = create_run("HDR", display="test", run_dir=tmp_path / "run")
    ti3 = _write_hdr_raw_ti3(tmp_path / "raw_hdr.ti3")          # perfect 1840 panel

    build_mhc.build(_ns(ctx, mode="HDR", is_hdr=True, source_ti3=str(ti3),
                        resolved_peak_nits=1700.0), ctx)
    params = _common.load_dlc_state(ctx)["mhc_params"]
    pc, bl = params["peak_chroma"], params["base_lut"]
    assert pc["resolved_peak_nits"] == 1700.0                   # the one resolved max-sustained ceiling
    # The neutral axis tops out at min(resolved peak, the Peak-Chroma cap) — the cap is a separate,
    # physical constraint that can pull the neutral ceiling below the resolved peak (but not above it).
    assert pc["cube_peak_nits"] == round(min(pc["resolved_peak_nits"], pc["cap_nits"]), 4)
    assert pc["cube_peak_nits"] <= pc["resolved_peak_nits"] + 1e-6
    assert bl["peak_nits"] == pc["cube_peak_nits"]              # handoff = the post-cube neutral peak
    assert abs(bl["summary"]["white_max_nits"] - pc["cube_peak_nits"]) < 1.0   # cube built to it


def test_hdr_build_mhc_clamps_resolved_peak_to_measured_raw_max(tmp_path):
    # A resolved peak ABOVE what THIS raw set actually measured is clamped to the raw max — never
    # build a cube above measured drive — and the clamp is recorded in the provenance.
    ctx = create_run("HDR", display="test", run_dir=tmp_path / "run")
    ti3 = _write_hdr_raw_ti3(tmp_path / "raw_hdr.ti3")          # ~1840 measured ceiling
    build_mhc.build(_ns(ctx, mode="HDR", is_hdr=True, source_ti3=str(ti3),
                        resolved_peak_nits=9999.0), ctx)
    pc = _common.load_dlc_state(ctx)["mhc_params"]["peak_chroma"]
    assert pc["resolved_peak_nits"] <= pc["native_peak_nits"] + 1e-6
    assert "clamped" in pc["ceiling_source"]


def test_hdr_build_mhc_standalone_falls_back_to_measured_raw_max(tmp_path):
    # No resolved peak supplied (standalone build-mhc / no orchestrator) → the stage's own measured
    # raw max is the ceiling, exactly as before — the wiring is opt-in, not a behaviour change here.
    ctx = create_run("HDR", display="test", run_dir=tmp_path / "run")
    ti3 = _write_hdr_raw_ti3(tmp_path / "raw_hdr.ti3")
    build_mhc.build(_ns(ctx, mode="HDR", is_hdr=True, source_ti3=str(ti3)), ctx)
    pc = _common.load_dlc_state(ctx)["mhc_params"]["peak_chroma"]
    assert "measured raw max" in pc["ceiling_source"]
    assert pc["resolved_peak_nits"] == pc["native_peak_nits"] or pc["resolved_peak_nits"] > 1000.0


def test_hdr_build_mhc_adapts_dense_raw_gray_to_desktoplut_mhc_shape(tmp_path):
    ctx = create_run("HDR", display="test", run_dir=tmp_path / "run")
    ti3 = _write_hdr_raw_ti3(tmp_path / "raw_hdr_dense.ti3", gray_steps=40)

    result = build_mhc.build(_ns(ctx, mode="HDR", is_hdr=True, source_ti3=str(ti3)), ctx)
    assert result.status == "ran"

    params = _common.load_dlc_state(ctx)["mhc_params"]
    base = params["base_grayscale"]
    assert base["point_count"] == 32
    assert base["points"][0] == 0.0 and base["points"][-1] == 1.0
    assert len(base["deviations"]["r"]) == 32
    # The dense gray ramp feeds the cube too (more measured neutral points).
    base_lut = params["base_lut"]
    assert base_lut and base_lut["summary"]["gray_points"] >= 40
    size, rows = _parse_cube(base_lut["cube_path"])
    assert size and len(rows) == size


def test_sdr_build_mhc_still_derives_a_real_base_grayscale(tmp_path):
    # Guard: the HDR branch must not change SDR — an sRGB panel still gets a fitted base.
    from dlc.simulation import write_synthetic_ti3

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    ti3 = write_synthetic_ti3(tmp_path / "raw_sdr.ti3")
    result = build_mhc.build(_ns(ctx, mode="SDR", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    # Perfect sRGB panel → near-identity base, but it went through the real proposal path.
    assert result.metrics["base_grayscale_max_abs_deviation"] is not None


def test_sdr_grayscale_noise_maps_sidecar_to_propose_noise(tmp_path):
    # _sdr_grayscale_noise feeds propose_correction_grayscale's per-level noise from the measure
    # loop's <ti3>.noise.json: an unstable level -> +inf (held to identity), a multi-read level -> a
    # finite SE-of-mean, a level absent from the sidecar -> not in the map (full step), no sidecar -> None.
    import json
    import math

    from dlc.measure_loop import noise_sidecar_path
    from dlc.refine import GrayPatch

    ti3 = tmp_path / "raw.ti3"
    patches = [GrayPatch(level=lvl, xyz=(20.0, 21.0, 22.0)) for lvl in (0.25, 0.5, 0.75, 1.0)]

    assert build_mhc._sdr_grayscale_noise(ti3, patches) is None       # no sidecar -> full step

    noise_sidecar_path(ti3).write_text(json.dumps({"by_level": {
        "0.25": {"unstable": True},
        "0.5": {"chroma_sigma": 0.002, "reads": 4},
        "1.0": {"chroma_sigma": 0.0001, "reads": 9},
        # 0.75 deliberately absent
    }}), encoding="utf-8")

    out = build_mhc._sdr_grayscale_noise(ti3, patches)
    assert out is not None
    assert out[0.25] == math.inf                 # unstable -> never trust
    assert 0.0 < out[0.5] < math.inf             # finite SE of the mean
    assert 0.0 < out[1.0] < math.inf
    assert 0.75 not in out                       # absent -> propose takes the full step


def test_sdr_build_mhc_smooths_unstable_levels_via_noise_sidecar(tmp_path):
    # End-to-end: a sidecar marking gray levels unstable flows through build_mhc's SDR proposal and
    # holds those levels at identity (the SDR analogue of the HDR cube's dark-trust smoothing).
    import json

    from dlc.measure_loop import noise_sidecar_path
    from dlc.simulation import write_synthetic_ti3

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    ti3 = write_synthetic_ti3(tmp_path / "raw_sdr.ti3")
    noise_sidecar_path(ti3).write_text(
        json.dumps({"by_level": {"0.25": {"unstable": True}, "0.5": {"unstable": True}}}),
        encoding="utf-8")

    result = build_mhc.build(_ns(ctx, mode="SDR", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    assert any("smoothed toward identity" in a for a in result.actions_taken)
    # The two unstable levels are held exactly at identity in the baked base grayscale.
    base = _common.load_dlc_state(ctx)["mhc_params"]["base_grayscale"]
    held = [i for i, lvl in enumerate(base["points"]) if abs(lvl - 0.25) < 1e-6 or abs(lvl - 0.5) < 1e-6]
    assert held
    for i in held:
        for ch in ("r", "g", "b"):
            assert base["deviations"][ch][i] == 1.0
