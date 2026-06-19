"""HDR verify scoring — dE_ITP (BT.2124) against PQ/Rec.2020 at a fixed white, plus the
advisory HDR thresholds. The engine math (numpy/colour) is importorskip-guarded; the
threshold + empty-input logic is pure stdlib."""

from __future__ import annotations

from pathlib import Path

import pytest

from dlc.decisions import HDR_VERIFY_THRESHOLD_DEFAULTS, MetricThresholds, hdr_metric_thresholds
from dlc.metrics import score_samples_hdr, summarize_metrics
from dlc.mhc import Ti3Sample

D65 = (0.3127, 0.3290)


def _engine():
    np = pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from dlc.engine.model import Target, TargetSpace, score_hdr

    return np, score_hdr, Target, TargetSpace


# ---------------------------------------------------------------------------
# engine score_hdr — dE_ITP of measured vs ideal PQ/Rec.2020
# ---------------------------------------------------------------------------

def test_score_hdr_perfect_panel_is_near_zero():
    np, score_hdr, Target, TargetSpace = _engine()
    space = TargetSpace(Target.hdr_rec2020_pq(white_xy=D65))
    signals = np.array([[0.5, 0.5, 0.5], [0.3, 0.3, 0.3], [0.6, 0.2, 0.2], [0.2, 0.6, 0.3]])
    ideal = space.ideal_xyz(signals)            # a panel that exactly hits the target
    res = score_hdr(signals, ideal, white_xy=D65)
    assert float(np.max(res["de_itp"])) < 1e-6


def test_score_hdr_undershoot_is_a_plausible_magnitude():
    np, score_hdr, Target, TargetSpace = _engine()
    space = TargetSpace(Target.hdr_rec2020_pq(white_xy=D65))
    signals = np.array([[0.5, 0.5, 0.5], [0.6, 0.6, 0.6]])
    ideal = space.ideal_xyz(signals)
    measured = ideal * 0.94                     # a 6% luminance undershoot
    de = score_hdr(signals, measured, white_xy=D65)["de_itp"]
    # A 6% luminance miss is clearly visible (>1 JND) but not absurd — brackets the
    # magnitude so a sign flip (→0) or a scale bug (→huge) fails, unlike a bare `>0`.
    assert all(1.0 < float(d) < 20.0 for d in de), list(map(float, de))


def test_score_hdr_sanitizes_non_finite_reads():
    # A dropped/saturated hardware read (NaN/inf XYZ) must score a finite, large error that
    # surfaces in the summary — not a NaN that silently poisons avg/p95 and hides in max().
    np, score_hdr, Target, TargetSpace = _engine()
    space = TargetSpace(Target.hdr_rec2020_pq(white_xy=D65))
    signals = np.array([[0.5, 0.5, 0.5], [0.6, 0.6, 0.6]])
    measured = space.ideal_xyz(signals).copy()
    measured[1] = [float("nan"), float("inf"), float("nan")]   # one garbage read
    de = score_hdr(signals, measured, white_xy=D65)["de_itp"]
    assert np.all(np.isfinite(de))              # no NaN propagation
    assert float(de[0]) < 1e-6                  # the good patch still scores ~0
    assert float(de[1]) > 1.0                   # the garbage patch scores a large finite error


# ---------------------------------------------------------------------------
# spine score_samples_hdr — wraps the engine into PatchMetric/summary plumbing
# ---------------------------------------------------------------------------

def test_score_samples_hdr_builds_metrics_carrying_de_itp():
    np, _score_hdr, Target, TargetSpace = _engine()
    space = TargetSpace(Target.hdr_rec2020_pq(white_xy=D65))
    signals = [(1.0, 1.0, 1.0), (0.5, 0.5, 0.5), (0.6, 0.2, 0.2)]
    ideal = space.ideal_xyz(np.array(signals))
    samples = [Ti3Sample(rgb=signals[i], xyz=tuple(float(c) for c in ideal[i]))
               for i in range(len(signals))]

    metrics, lum = score_samples_hdr(samples, white_xy=D65, peak_nits=1600.0)

    assert lum == 1600.0                                  # peak reported as target luminance
    assert len(metrics) == 3
    # The de2000 field is the generic ΔE carrier — here it holds dE_ITP; perfect → ~0.
    assert max(m.de2000 for m in metrics) < 1e-6
    assert metrics[0].grayscale and metrics[1].grayscale and not metrics[2].grayscale

    summary = summarize_metrics(phase="verification", iteration=0, source=Path("x.ti3"),
                                patch_metrics=metrics, target_luminance=lum, metric="dE_ITP")
    assert summary.metric == "dE_ITP"                     # the label disambiguates the units


def test_score_samples_hdr_empty_raises_before_engine_import():
    # The empty guard runs before the lazy engine import, so this needs no numpy/colour.
    with pytest.raises(ValueError):
        score_samples_hdr([], white_xy=D65, peak_nits=1600.0)


# ---------------------------------------------------------------------------
# HDR thresholds — looser than SDR, policy + override aware (stdlib)
# ---------------------------------------------------------------------------

def test_hdr_thresholds_are_looser_than_sdr_defaults():
    hdr = hdr_metric_thresholds()
    sdr = MetricThresholds()
    assert hdr.avg_de2000 > sdr.avg_de2000
    assert hdr.max_de2000 == HDR_VERIFY_THRESHOLD_DEFAULTS["max_de2000"]
    assert hdr.white_de2000 == HDR_VERIFY_THRESHOLD_DEFAULTS["white_de2000"]


def test_hdr_thresholds_respect_policy_block_and_overrides():
    t = hdr_metric_thresholds({"hdr": {"avg_de2000": 2.0}}, overrides={"max_de2000": 7.0})
    assert t.avg_de2000 == 2.0          # profile policy hdr block
    assert t.max_de2000 == 7.0          # explicit override (e.g. the LLM's negotiated target)
    assert t.p95_de2000 == HDR_VERIFY_THRESHOLD_DEFAULTS["p95_de2000"]  # untouched default
