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


def test_sdr_build_mhc_derives_a_base_1dlut_cube(tmp_path):
    # SDR now rides the same 1D-LUT-base mechanism as HDR (set_base_lut → sourceIs1DCube), NOT the
    # user-editable correctionGrayscale slot ([[dlc-must-not-own-mhc-user-layers]]). Build emits a
    # full-resolution per-channel .cube; the 32-point base grayscale is harmless identity plumbing.
    from dlc.simulation import write_synthetic_ti3
    from dlc.mhc_cube import read_1d_cube

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    ti3 = write_synthetic_ti3(tmp_path / "raw_sdr.ti3")
    result = build_mhc.build(_ns(ctx, mode="SDR", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    params = _common.load_dlc_state(ctx)["mhc_params"]
    base_lut = params["base_lut"]
    assert base_lut and Path(base_lut["cube_path"]).exists()
    assert base_lut["peak_nits"] == 0.0                  # SDR LUT carries no HDR luminance metadata
    curves = read_1d_cube(Path(base_lut["cube_path"]))
    assert len(curves["r"]) >= 256                       # full-resolution per-channel cube
    # The 32-point base grayscale is now identity plumbing (the cube is authoritative).
    base = params["base_grayscale"]
    assert all(abs(v - 1.0) < 1e-9 for ch in ("r", "g", "b") for v in base["deviations"][ch])


def test_sdr_build_mhc_smooths_unstable_levels_via_noise_sidecar(tmp_path):
    # End-to-end: a sidecar marking gray levels unstable flows through build_mhc's SDR cube build and
    # holds those levels ~identity in the cube (the same _dark_level_trust → dark_trust_weights path HDR
    # uses), instead of baking a tint. Replaces the old _sdr_grayscale_noise→propose path.
    import json

    from dlc.measure_loop import noise_sidecar_path
    from dlc.simulation import write_synthetic_ti3
    from dlc.mhc_cube import read_1d_cube

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    ti3 = write_synthetic_ti3(tmp_path / "raw_sdr.ti3")
    noise_sidecar_path(ti3).write_text(
        json.dumps({"by_level": {"0.25": {"unstable": True}, "0.5": {"unstable": True}}}),
        encoding="utf-8")

    result = build_mhc.build(_ns(ctx, mode="SDR", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    assert any("smoothed toward identity" in a for a in result.actions_taken)
    # The two unstable levels are held ~identity (cube ≈ input signal) in the baked base cube.
    curves = read_1d_cube(Path(_common.load_dlc_state(ctx)["mhc_params"]["base_lut"]["cube_path"]))
    n = len(curves["r"])
    for sig in (0.25, 0.5):
        j = round(sig * (n - 1))
        for ch in ("r", "g", "b"):
            assert abs(curves[ch][j] - sig) < 1.5e-2, (ch, sig, curves[ch][j])


def _write_sdr_drifted_dark_ti3(path: Path, *, drift_dy: float = 0.021) -> Path:
    """A synthetic SDR raw TI3 (γ2.2, ~D65 native, 100-nit peak) whose 0.1-signal gray
    (~0.63 nits) carries a REAL chromaticity drift (+drift_dy in y) — the correctable disease.
    All other grays track the native white exactly."""
    from dlc.colormath import rgb_to_xyz_matrix, xy_to_XYZ

    peak = 100.0
    prim = {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}
    P = rgb_to_xyz_matrix(prim["rx"], prim["ry"], prim["gx"], prim["gy"],
                          prim["bx"], prim["by"], 0.3127, 0.3290, white_Y=peak)
    rows = []

    def emit_row(rgb, xyz):
        rows.append(f"{rgb[0] * 100:.6f} {rgb[1] * 100:.6f} {rgb[2] * 100:.6f} "
                    f"{xyz[0]:.6f} {xyz[1]:.6f} {xyz[2]:.6f}")

    for s in (0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0):
        frac = s ** 2.2
        xyz = tuple(sum(P[r][c] * frac for c in range(3)) for r in range(3))
        if s == 0.1:                                      # real, repeatable greenish dark drift
            xyz = xy_to_XYZ(0.3127, 0.3290 + drift_dy, xyz[1])
        emit_row((s, s, s), xyz)
    for c in range(3):                                    # per-channel ramps for the primaries
        for s in (0.5, 1.0):
            rgb = [0.0, 0.0, 0.0]
            rgb[c] = s
            frac = s ** 2.2
            xyz = tuple(P[r][c] * frac for r in range(3))
            emit_row(tuple(rgb), xyz)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        "CTI3", "# Synthetic SDR raw with a real (stable) dark drift",
        "BEGIN_DATA_FORMAT", "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z", "END_DATA_FORMAT",
        f"NUMBER_OF_SETS {len(rows)}", "BEGIN_DATA", *rows, "END_DATA", "",
    ]), encoding="utf-8")
    return path


def test_sdr_build_mhc_stable_dark_drift_is_corrected_not_smoothed(tmp_path):
    # Phase 4 (σ-aware adaptive dark floor): a REAL, repeatable dark drift — strayed chromaticity
    # with a tiny measured σ in the noise sidecar — must NOT raise the adaptive floor and smooth
    # its own correction to identity. Without the sidecar (single-read run) the same drift keeps
    # the old conservative behaviour: the floor rises over it and the cube holds ~identity there.
    import json

    from dlc.measure_loop import noise_sidecar_path
    from dlc.mhc_cube import read_1d_cube

    # -- with a sidecar proving the drifted level is STABLE (σ tiny over 4 reads) --
    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run_sigma")
    ti3 = _write_sdr_drifted_dark_ti3(tmp_path / "raw_drift.ti3")
    noise_sidecar_path(ti3).write_text(
        json.dumps({"by_level": {f"{0.1:.6f}": {"chroma_sigma": 0.001, "reads": 4}}}),
        encoding="utf-8")
    result = build_mhc.build(_ns(ctx, mode="SDR", source_ti3=str(ti3)), ctx)
    assert result.status == "ran"
    df = _common.load_dlc_state(ctx)["mhc_params"]["dark_floor"]
    assert df["nits"] == 0.1 and df["n_real_drift"] >= 1, df   # floor stays at the low bound
    curves = read_1d_cube(Path(_common.load_dlc_state(ctx)["mhc_params"]["base_lut"]["cube_path"]))
    n = len(curves["g"])
    j = round(0.1 * (n - 1))
    corrected_dev = abs(curves["g"][j] - 0.1)                  # green pulled to fix the green drift

    # -- same panel, NO sidecar: noise and drift are indistinguishable -> conservative floor --
    ctx2 = create_run("SDR", display="test", run_dir=tmp_path / "run_bare")
    ti32 = _write_sdr_drifted_dark_ti3(tmp_path / "raw_drift2.ti3")
    build_mhc.build(_ns(ctx2, mode="SDR", source_ti3=str(ti32)), ctx2)
    df2 = _common.load_dlc_state(ctx2)["mhc_params"]["dark_floor"]
    assert df2["nits"] > 0.5, df2                              # floor rose over the ~0.63-nit read
    curves2 = read_1d_cube(Path(_common.load_dlc_state(ctx2)["mhc_params"]["base_lut"]["cube_path"]))
    held_dev = abs(curves2["g"][j] - 0.1)

    assert corrected_dev > 0.0025, corrected_dev              # the stable drift IS corrected
    assert held_dev < corrected_dev / 3, (held_dev, corrected_dev)   # σ-less stays held ~identity


def test_hdr_build_mhc_reports_wrgb_gate_diagnostics(tmp_path):
    # The WRGB gate (2026-09-02): the build reports the DRIVE-MATCHED non-additivity (grey vs
    # additive RGB at the same drive), the chosen cap policy, and whether the ceiling is full-drive
    # grounded. A perfectly-additive synthetic panel must read ~1.0 and stay on the additive cap
    # (NOT be misclassified WRGB) — the FALD regression guard.
    ctx = create_run("HDR", display="test", run_dir=tmp_path / "run")
    ti3 = _write_hdr_raw_ti3(tmp_path / "raw_hdr.ti3")
    build_mhc.build(_ns(ctx, mode="HDR", is_hdr=True, source_ti3=str(ti3)), ctx)
    pc = _common.load_dlc_state(ctx)["mhc_params"]["peak_chroma"]
    assert pc["drive_matched_nonadditivity"] is not None
    assert abs(pc["drive_matched_nonadditivity"] - 1.0) < 0.05    # additive ⇒ ~1
    assert pc["wrgb_nonadditive"] is False                        # NOT misclassified WRGB
    assert pc["cap_policy"] in ("additive-d65-cap", "ceiling-within-cap")
    # The synthetic ramp reaches full drive (grey at cv max), so the ceiling is grounded.
    assert pc["full_drive_grounded"] is True
    assert pc["full_drive_white_nits"] is not None


def test_cube_max_drive_is_peak_code_under_additive_policy_only():
    from dlc.mhc_cube import pq_oetf
    from dlc.stages.build_mhc import cube_max_drive
    assert cube_max_drive(resolved_peak_nits=1835.1, wrgb_nonadditive=False) == pq_oetf(1835.1 / 10000.0)
    assert cube_max_drive(resolved_peak_nits=1835.1, wrgb_nonadditive=True) is None
    assert cube_max_drive(resolved_peak_nits=None, wrgb_nonadditive=False) is None


def test_build_hdr_cube_max_drive_pins_top_on_a_saturating_plateau():
    """A FALD-style raw ramp that saturates at the cap (flat +-1-nit plateau above the peak code):
    unbounded, the plateau noise can send the top drive above the peak code; with ``max_drive`` the
    top is pinned at the peak code while the mid-tones are untouched."""
    from dlc.mhc import Ti3Sample
    from dlc.mhc_cube import build_hdr_cube, matvec, pq_eotf, pq_oetf, rgb_to_xyz_matrix
    prim = {"rx": 0.685, "ry": 0.309, "gx": 0.183, "gy": 0.750, "bx": 0.152, "by": 0.065}
    wxy = (0.3185, 0.3287)
    P = rgb_to_xyz_matrix(prim["rx"], prim["ry"], prim["gx"], prim["gy"], prim["bx"], prim["by"],
                          wxy[0], wxy[1], white_Y=1.0)
    cap = 1757.0
    peak_sig = pq_oetf(1835.0 / 10000.0)
    samples = []
    for i in range(1, 41):
        sig = i / 40.0
        y = min(pq_eotf(sig) * 10000.0, cap)
        if sig > peak_sig:
            y = cap + (1.0 if i % 2 else -1.0)   # plateau noise +-1 nit above the peak code
        samples.append(Ti3Sample((sig, sig, sig), tuple(v * y for v in matvec(P, (1.0, 1.0, 1.0)))))
    free, _ = build_hdr_cube(samples, prim, wxy, cap, dark_floor_nits=0.1)
    pinned, summary = build_hdr_cube(samples, prim, wxy, cap, dark_floor_nits=0.1, max_drive=peak_sig)
    n = len(pinned["r"])
    assert summary["max_drive"] == round(peak_sig, 6)
    for ch in ("r", "g", "b"):
        assert pinned[ch][n - 1] <= peak_sig + 1e-9
        assert free[ch][n - 1] >= pinned[ch][n - 1]
        assert abs(pinned[ch][n // 2] - free[ch][n // 2]) < 1e-9
