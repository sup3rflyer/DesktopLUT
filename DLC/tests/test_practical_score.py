"""Fable audit Phase 6 — scoring, verify gates, and reporting truth.

Pins the phase's contracts:

* the §0 practically-weighted summary (``metrics.practical_summary``): core/limits/
  at-the-gamut-floor zones, the neutral tube, the Phase 2 luminance bands — with the
  zone classifier SHARED with the dashboard's live ΔE split (one constant, one
  classifier, so the scored number and the live number can never disagree);
* gamut-aware scoring parity between the live verify and the stage CLI (P1) — the
  stage tool clamps against the same run-record native primaries the live path uses,
  and flags targets scored at the panel's gamut boundary;
* the canonical ``metrics_scored`` producer shape (P4) via ``metrics_scored_payload``
  / ``write_metrics``;
* the grid-pitch-derived check-cube neighbour-delta gate (the old fixed 1.0 admitted
  a full-range jump);
* threshold-policy plumbing (profile ``quality: {hdr: ...}`` reaches the HDR gate)
  and the report stage's cross-metric fallback guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dlc import metrics as M
from dlc.mhc import Ti3Sample
from dlc.runs import create_run
from dlc.events import read_events
from dlc.stages import _common

D65 = (0.3127, 0.3290)
# A P3-ish panel inside a Rec.2020 target — saturated Rec.2020 primaries are unreachable.
P3ISH = {"R": [0.680, 0.320], "G": [0.265, 0.690], "B": [0.150, 0.060]}
P3ISH_FLAT = {"rx": 0.680, "ry": 0.320, "gx": 0.265, "gy": 0.690, "bx": 0.150, "by": 0.060}


def _pm(rgb, target_xyz, de, *, grayscale=False, clamped=False) -> M.PatchMetric:
    return M.PatchMetric(rgb=rgb, measured_xyz=target_xyz, target_xyz=target_xyz,
                         de2000=de, grayscale=grayscale, gamut_clamped=clamped)


# ---------------------------------------------------------------------------
# one zone definition, shared with the dashboard
# ---------------------------------------------------------------------------

def test_zone_classifier_is_shared_with_the_dashboard():
    from dlc.dashboard import state as dstate
    # The dashboard's live core/limits split classifies with the SAME function and the
    # SAME reference-white constant the scored practical summary uses — import identity,
    # not a copied value, so drift is impossible (fable Phase 6 / roadmap §0 note).
    assert dstate.is_core_target is M.is_core_target
    assert dstate._HDR_REF_WHITE_NITS == M.HDR_REF_WHITE_NITS == 203.0
    assert dstate._SRGB_PRIMARIES == {ch: list(xy) for ch, xy in zip("rgb", M.SRGB_PRIMARIES)}


def test_is_core_target_zone_edges():
    inside_709 = (0.3127, 0.3290)
    rec2020_green = (0.170, 0.797)      # outside Rec.709
    assert M.is_core_target(inside_709, 100.0) is True
    assert M.is_core_target(inside_709, M.HDR_REF_WHITE_NITS) is True   # ref-white itself is core
    assert M.is_core_target(inside_709, 400.0) is False                # bright in-709 → limits
    assert M.is_core_target(rec2020_green, 50.0) is False              # wide-gamut → limits
    assert M.is_core_target(inside_709, None) is True                  # unknown Y, in-709 → core
    assert M.is_core_target(None, 5.0) is True                         # black/degenerate target → core


# ---------------------------------------------------------------------------
# practical_summary buckets
# ---------------------------------------------------------------------------

def test_practical_summary_hdr_buckets_core_limits_clamped():
    white50 = (47.5, 50.0, 54.4)      # ~D65 at 50 nit → core
    white500 = (475.3, 500.0, 544.1)  # ~D65 at 500 nit → in-709 chromaticity, >203 → limits
    green = (30.0, 90.0, 10.0)        # chromatic target (would be wide-gamut) — marked clamped
    patch_metrics = [
        _pm((0.5, 0.5, 0.5), white50, 0.5, grayscale=True),
        _pm((0.9, 0.9, 0.9), white500, 1.0, grayscale=True),
        _pm((0.1, 0.9, 0.1), green, 8.0, clamped=True),
    ]
    p = M.practical_summary(patch_metrics, is_hdr=True, gamut_aware=True)
    assert p["gamut_aware"] is True
    assert p["core"]["n"] == 1 and p["core"]["avg"] == 0.5
    assert p["limits"]["n"] == 1 and p["limits"]["avg"] == 1.0
    assert p["clamped"]["n"] == 1 and p["clamped"]["avg"] == 8.0
    # the clamped patch NEVER leaks into the core headline
    assert p["core"]["max"] == 0.5
    # tube = the two neutrals; the saturated green is out
    assert p["tube"]["n"] == 2
    # luminance bands (target Y): 50 → 10-100, 500 → >203, 90 → 10-100
    assert p["bands"]["10-100"]["n"] == 2
    assert p["bands"][">203"]["n"] == 1


def test_practical_summary_sdr_everything_reachable_is_core():
    patch_metrics = [
        _pm((1.0, 1.0, 1.0), (95.0, 100.0, 108.9), 0.4, grayscale=True),
        _pm((1.0, 0.0, 0.0), (41.2, 21.3, 1.9), 1.2),      # saturated sRGB red — SDR core
        _pm((0.02, 0.02, 0.02), (0.04, 0.05, 0.05), 2.0, grayscale=True),
    ]
    p = M.practical_summary(patch_metrics, is_hdr=False)
    assert p["core"]["n"] == 3 and p["limits"]["n"] == 0 and p["clamped"]["n"] == 0
    # low-light honesty: the dark neutral lands in the <1 band, visible on its own row
    assert p["bands"]["<1"]["n"] == 1 and p["bands"]["<1"]["max"] == 2.0
    # near-neutral tube keeps the greys, not the red
    assert p["tube"]["n"] == 2


def test_practical_summary_band_edges_use_phase2_bands():
    assert M.PRACTICAL_BAND_EDGES_NITS == (1.0, 10.0, 100.0, 203.0)
    assert M.TUBE_SATURATION_MAX == 0.20


# ---------------------------------------------------------------------------
# gamut-aware scoring (P1): clamp flags + boundary patches score as reachability
# ---------------------------------------------------------------------------

def _ideal_xyz(sig, *, reachable=None):
    """The (optionally gamut-clamped) ideal XYZ for a PQ/Rec.2020 signal — what a
    perfect panel (at its own gamut boundary, when clamped) would emit."""
    from dlc.engine.model import Target, TargetSpace
    import numpy as np
    space = TargetSpace(Target.hdr_rec2020_pq(white_xy=D65), reachable_primaries=reachable)
    return [float(c) for c in space.ideal_xyz(np.asarray([list(sig)]))[0]]


def _boundary_red_xyz():
    """The clamped ideal XYZ for a saturated Rec.2020 red on the P3-ish panel — what a
    PERFECT panel at its own gamut boundary would emit for signal (0.6, 0, 0)."""
    return _ideal_xyz((0.6, 0.0, 0.0), reachable=P3ISH)


def test_score_samples_hdr_flags_gamut_clamped_targets():
    xyz = _boundary_red_xyz()
    samples = [
        Ti3Sample(rgb=(0.6, 0.0, 0.0), xyz=tuple(xyz)),          # unreachable sat red
        Ti3Sample(rgb=(0.5, 0.5, 0.5), xyz=(47.5, 50.0, 54.4)),  # neutral — always reachable
    ]
    scored, _ = M.score_samples_hdr(samples, white_xy=D65, peak_nits=1000.0,
                                    reachable_primaries=P3ISH)
    assert scored[0].gamut_clamped is True
    assert scored[1].gamut_clamped is False
    # A panel sitting exactly ON its gamut boundary scores ~0 there (reachability
    # honoured), where the unclamped target would call the same light a large error.
    assert scored[0].de2000 < 0.5
    unclamped, _ = M.score_samples_hdr(samples, white_xy=D65, peak_nits=1000.0)
    assert unclamped[0].gamut_clamped is False
    assert unclamped[0].de2000 > 5.0


def test_reachable_primaries_from_mhc_params_and_degenerate_guard():
    assert M.reachable_primaries_from_mhc_params({"primaries": P3ISH_FLAT}) == {
        "R": [0.680, 0.320], "G": [0.265, 0.690], "B": [0.150, 0.060]}
    assert M.reachable_primaries_from_mhc_params(None) is None
    assert M.reachable_primaries_from_mhc_params({}) is None
    assert M.reachable_primaries_from_mhc_params({"primaries": {"rx": 0.6}}) is None
    collinear = {"rx": 0.1, "ry": 0.1, "gx": 0.2, "gy": 0.2, "bx": 0.3, "by": 0.3}
    assert M.reachable_primaries_from_mhc_params({"primaries": collinear}) is None
    assert M.sanitize_reachable_primaries({"R": [0.1, 0.1], "G": [0.2, 0.2]}) is None


# ---------------------------------------------------------------------------
# the stage-CLI score is gamut-aware from the run record (P1) and emits the
# canonical artifacts + event (P4)
# ---------------------------------------------------------------------------

def _ti3_text(rows: list[tuple[tuple[float, float, float], tuple[float, float, float]]]) -> str:
    lines = ["CTI3", "BEGIN_DATA_FORMAT", "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z",
             "END_DATA_FORMAT", f"NUMBER_OF_SETS {len(rows)}", "BEGIN_DATA"]
    for rgb, xyz in rows:
        lines.append(" ".join(f"{c * 100.0:.4f}" for c in rgb) + " "
                     + " ".join(f"{c:.5f}" for c in xyz))
    lines.append("END_DATA")
    return "\n".join(lines)


def _score_args(ctx, ti3: Path, **over):
    base = dict(run=ctx.root, monitor=0, mode="HDR", simulate=True, pipe="p",
                stage="3dlut-verification", iteration=1, source_ti3=str(ti3),
                gamma=2.2, luminance=None, target_white_xy=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_stage_score_cli_hdr_is_gamut_aware_from_the_run_record(tmp_path: Path):
    from dlc.stages import score as score_stage

    ctx = create_run("HDR", "synthetic", tmp_path / "run")
    _common.save_dlc_state(ctx, {"mhc_params": {
        "primaries": P3ISH_FLAT,
        "white": {"x": D65[0], "y": D65[1]}, "white_source": "test",
        "target_luminance": 1000.0}})
    ti3 = ctx.root / "measurements" / "verify.ti3"
    ti3.parent.mkdir(parents=True, exist_ok=True)
    xyz = _boundary_red_xyz()
    neutral_xyz = _ideal_xyz((0.5, 0.5, 0.5))   # a perfect neutral read
    ti3.write_text(_ti3_text([((0.6, 0.0, 0.0), tuple(xyz)),
                              ((0.5, 0.5, 0.5), tuple(neutral_xyz))]), encoding="utf-8")

    result = score_stage.build(_score_args(ctx, ti3), ctx)
    assert result.status == "ran"
    assert result.metrics["gamut_aware"] is True
    assert result.metrics["metric"] == "dE_ITP"
    # The boundary patch is scored against the reachable gamut — like the LIVE verify —
    # so the stage CLI no longer reports an unreachable Rec.2020 corner as ~huge error.
    assert result.metrics["max_de2000"] < 5.0
    assert result.metrics["practical"]["clamped"]["n"] == 1
    # worst list marks the at-the-floor patch
    assert any(w["gamut_clamped"] for w in result.raw["worst_patches"])

    # P4: the artifacts exist (including the *_patch_metrics.json the dashboard's
    # /api/patch_metrics globs for) and the canonical event landed on the spine.
    reports = ctx.root / "reports"
    assert list(reports.glob("*_patch_metrics.json")), "patch-rows artifact missing"
    metrics_doc = json.loads(next(iter(reports.glob("*_iter01_metrics.json"))).read_text())
    assert "practical" in metrics_doc and "p99_de2000" in metrics_doc
    scored = [e for e in read_events(ctx.events_path) if e.event == "metrics_scored"]
    assert scored, "stage-CLI score must emit metrics_scored (dashboard ΔE panel)"
    d = scored[-1].data
    for key in ("metric", "avg_de2000", "p95_de2000", "p99_de2000", "max_de2000",
                "white_de2000", "grayscale_avg_de2000", "colour_avg_de2000",
                "patch_count", "practical"):
        assert key in d, f"canonical metrics_scored missing {key}"
    assert scored[-1].effective_tier == "digest"


def test_stage_score_cli_without_native_primaries_says_so(tmp_path: Path):
    from dlc.stages import score as score_stage

    ctx = create_run("HDR", "synthetic", tmp_path / "run")
    _common.save_dlc_state(ctx, {"mhc_params": {
        "white": {"x": D65[0], "y": D65[1]}, "white_source": "test",
        "target_luminance": 1000.0}})
    ti3 = ctx.root / "measurements" / "verify.ti3"
    ti3.parent.mkdir(parents=True, exist_ok=True)
    ti3.write_text(_ti3_text([((0.5, 0.5, 0.5), (47.5, 50.0, 54.4))]), encoding="utf-8")
    result = score_stage.build(_score_args(ctx, ti3), ctx)
    # No build yet ⇒ no clamp — surfaced honestly, never silently different from live.
    assert result.metrics["gamut_aware"] is False
    assert result.metrics["practical"]["clamped"]["n"] == 0


# ---------------------------------------------------------------------------
# check-cube: the neighbour-delta gate is grid-pitch-derived (Phase 5 lead)
# ---------------------------------------------------------------------------

def _cube_text(size: int, jump_at_last_r: bool) -> str:
    sc = size - 1
    lines = ['TITLE "t"', f"LUT_3D_SIZE {size}"]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                out = [r / sc, g / sc, b / sc]
                if jump_at_last_r and r == sc:
                    # Lift the G OUTPUT across the whole r=max plane: a 0.9 jump along the
                    # r axis (whose monotonicity check watches only the R output) while G
                    # stays non-decreasing along its own axis — in-bounds, monotone, a
                    # pure smoothness tear.
                    out[1] = max(0.9, g / sc)
                lines.append(" ".join(f"{v:.6f}" for v in out))
    return "\n".join(lines) + "\n"


def test_neighbor_delta_gate_derived_from_grid_pitch(tmp_path: Path):
    from dlc.lut_integrity import (default_neighbor_delta_allowed, parse_cube,
                                   summarize_lut_integrity)

    # identity passes the derived gate
    clean = tmp_path / "clean.cube"
    clean.write_text(_cube_text(9, jump_at_last_r=False), encoding="utf-8")
    ok = summarize_lut_integrity(cube=parse_cube(clean), phase="3dlut", iteration=1,
                                 integrity_path=tmp_path / "i.json")
    assert ok.ok and ok.max_neighbor_delta_allowed == default_neighbor_delta_allowed(9)

    # a near-full-range tear FAILS the derived gate — the old fixed 1.0 admitted it
    torn = tmp_path / "torn.cube"
    torn.write_text(_cube_text(9, jump_at_last_r=True), encoding="utf-8")
    bad = summarize_lut_integrity(cube=parse_cube(torn), phase="3dlut", iteration=1,
                                  integrity_path=tmp_path / "i2.json")
    assert bad.monotonicity_violations == 0 and bad.out_of_bounds_count == 0
    assert not bad.ok, "a 0.9 neighbour tear must fail the structural gate"
    legacy = summarize_lut_integrity(cube=parse_cube(torn), phase="3dlut", iteration=1,
                                     integrity_path=tmp_path / "i3.json",
                                     max_neighbor_delta_allowed=1.0)
    assert legacy.ok, "regression sentinel: the old default really was toothless"
    # derivation shape: identity pitch ×2 + the max legitimate correction (0.5)
    assert default_neighbor_delta_allowed(33) == pytest.approx(2.0 / 32 + 0.5)
    assert default_neighbor_delta_allowed(1) == 1.0


# ---------------------------------------------------------------------------
# threshold plumbing + report guard + metric labels
# ---------------------------------------------------------------------------

def test_profile_quality_hdr_block_reaches_the_hdr_gate(tmp_path: Path):
    import dlc.calibration_profile as cp
    from dlc.decisions import hdr_metric_thresholds

    yaml_text = """
meter:
  spotread: spotread
displays:
  - name: Panel
    desktoplut_monitor: 0
    argyll_display: 1
    sdr_target: srgb_g22
targets:
  srgb_g22:
    colorspace: Rec.709
    transfer:
      type: power
      gamma: 2.2
    white_luminance_nits: 120
quality:
  avg_de2000: 1.2
  hdr:
    avg_de2000: 2.5
    max_de2000: 8.0
"""
    p = tmp_path / "profile.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    profile = cp.load_profile(p)
    # the SDR gate keeps its flat override; the raw block is retained for the HDR gate
    assert profile.quality.avg_de2000 == 1.2
    assert profile.quality_policy.get("hdr", {}).get("avg_de2000") == 2.5
    th = hdr_metric_thresholds(profile.quality_policy)
    assert th.avg_de2000 == 2.5 and th.max_de2000 == 8.0
    assert th.p95_de2000 == 6.0    # untouched keys keep the HDR defaults


def test_report_fallback_refuses_a_cross_metric_improvement(tmp_path: Path):
    from dlc.stages import report as report_stage

    ctx = create_run("HDR", "synthetic", tmp_path / "run")
    # a stale SDR-metric score in history, no verification TI3 anywhere
    _common.save_dlc_state(ctx, {"score_history": [
        {"stage": "old", "metric": "CIEDE2000", "avg_de2000": 1.0,
         "max_de2000": 2.0, "white_de2000": 0.5}]})
    args = SimpleNamespace(run=ctx.root, monitor=0, mode="HDR", simulate=True,
                           pipe="p", gamma=2.2)
    result = report_stage.build(args, ctx)
    payload = json.loads((ctx.root / "reports" / "report.json").read_text())
    # the CIEDE2000 history entry must NOT masquerade as a dE_ITP final score
    assert payload["after"] is None
    assert payload["improvement"] is None
    assert any(a.code == "no_final" for a in result.anomalies)


def test_grayscale_wb_summary_labels_its_lab_metric():
    from dlc.grayscale_wb import summarize_errors

    s = summarize_errors([{"de2000": 1.0, "C": 0.5, "delta_L": 0.1, "delta_Y": 0.2}])
    # the touch-up flow is mode-shared but scores in Lab — the label prevents an HDR
    # digest from reading these numbers as dE_ITP (P2/P4)
    assert s["metric"] == "CIEDE2000"
