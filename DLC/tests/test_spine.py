"""Tests for the LLM-steered harness spine: controller contract, stage result,
color math, and the MHC grayscale refinement control law (convergence)."""

from __future__ import annotations

import json

import pytest

from dlc.colormath import invert3x3, matvec, rgb_to_xyz_matrix, xy_to_XYZ
from dlc.controller import CalibrationController, normalize_mode
from dlc.refine import (
    Deviations,
    GrayPatch,
    MeasuredPrimaries,
    RefinementTarget,
    propose_correction_grayscale,
)
from dlc.stage import StageResult, sha256_file


# --------------------------------------------------------------------------
# colormath
# --------------------------------------------------------------------------
def test_rgb_to_xyz_reproduces_white():
    # sRGB / Rec.709 primaries with D65.
    M = rgb_to_xyz_matrix(0.64, 0.33, 0.30, 0.60, 0.15, 0.06, 0.3127, 0.3290, white_Y=1.0)
    X, Y, Z = matvec(M, (1.0, 1.0, 1.0))
    d65 = xy_to_XYZ(0.3127, 0.3290, 1.0)
    assert abs(X - d65[0]) < 1e-6
    assert abs(Y - d65[1]) < 1e-6
    assert abs(Z - d65[2]) < 1e-6


def test_invert3x3_identity():
    M = rgb_to_xyz_matrix(0.64, 0.33, 0.30, 0.60, 0.15, 0.06, 0.3127, 0.3290, white_Y=1.0)
    Minv = invert3x3(M)
    prod = [[sum(M[i][k] * Minv[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    for i in range(3):
        for j in range(3):
            assert abs(prod[i][j] - (1.0 if i == j else 0.0)) < 1e-9


# --------------------------------------------------------------------------
# verify scoring targets the resolved white (T1.3) — not hardcoded D65
# --------------------------------------------------------------------------
def test_verify_scores_against_resolved_white_not_hardcoded_d65():
    from dlc.metrics import npm_for_white, score_samples, target_xyz_for_rgb
    from dlc.mhc import Ti3Sample

    # A cooler-than-D65 CRT-like white (what strength>0 SPD resolution aims at).
    white = (0.3080, 0.3250)
    lum, gamma = 120.0, 2.2
    # A *perfect* panel: every patch emits exactly the ideal XYZ for that resolved white.
    matrix = npm_for_white(white)
    rgbs = [(1.0, 1.0, 1.0), (0.5, 0.5, 0.5), (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.2, 0.4, 0.6)]
    samples = [Ti3Sample(rgb=rgb, xyz=target_xyz_for_rgb(rgb, lum, gamma, matrix)) for rgb in rgbs]

    # Scored against the SAME resolved white → ~0 error (the white is the goal).
    correct, _ = score_samples(samples, luminance=lum, gamma=gamma, white_xy=white)
    assert max(m.de2000 for m in correct) < 0.01

    # Scored against hardcoded D65 (the old behaviour) → the intended white offset is
    # wrongly counted as white error and would fail a tight gate.
    d65, _ = score_samples(samples, luminance=lum, gamma=gamma)   # white_xy=None → D65
    white_metric = max(d65, key=lambda m: sum(m.rgb))
    assert white_metric.de2000 > 1.0


def test_xyz_to_lab_clamps_negative_tristimulus():
    # A dark/noisy measurement can read slightly negative XYZ; it must map to legitimate black
    # (finite Lab), not garbage that could corrupt the dE accept/iterate verdict (M5).
    import math

    from dlc.metrics import xyz_to_lab
    white = (95.047, 100.0, 108.883)
    lab = xyz_to_lab((-0.5, -0.3, -0.2), white)
    assert lab == xyz_to_lab((0.0, 0.0, 0.0), white)   # clamped to black
    assert all(math.isfinite(c) for c in lab)


def test_summarize_metrics_paths_default_to_none():
    # Callers that write no metrics/patch JSON must not record a fake artifact path (M8).
    from pathlib import Path

    from dlc.metrics import score_samples, summarize_metrics, target_xyz_for_rgb
    from dlc.mhc import Ti3Sample
    samples = [Ti3Sample(rgb=(1.0, 1.0, 1.0), xyz=target_xyz_for_rgb((1.0, 1.0, 1.0), 100.0, 2.2))]
    metrics, lum = score_samples(samples, luminance=100.0, gamma=2.2)
    summary = summarize_metrics(phase="v", iteration=0, source=Path("x.ti3"),
                                patch_metrics=metrics, target_luminance=lum)
    assert summary.metrics_path is None and summary.patches_path is None


def test_score_samples_default_white_is_unchanged_d65():
    # The legacy (white_xy=None) path must still be textbook D65, byte-for-byte behaviour.
    from dlc.metrics import score_samples, target_xyz_for_rgb
    from dlc.mhc import Ti3Sample

    lum, gamma = 100.0, 2.2
    rgbs = [(1.0, 1.0, 1.0), (0.5, 0.2, 0.8)]
    samples = [Ti3Sample(rgb=rgb, xyz=target_xyz_for_rgb(rgb, lum, gamma)) for rgb in rgbs]
    metrics, _ = score_samples(samples, luminance=lum, gamma=gamma)
    assert max(m.de2000 for m in metrics) < 1e-9   # perfect D65 panel scores ~0 under D65


# --------------------------------------------------------------------------
# StageResult contract
# --------------------------------------------------------------------------
def test_stage_result_shape_and_hashing(tmp_path):
    artifact = tmp_path / "thing.cube"
    artifact.write_text("TITLE \"x\"\n", encoding="utf-8")

    result = (
        StageResult("mhc-verify")
        .action("measured 22-patch ramp")
        .note("white slightly warm")
        .anomaly("white_warm", "CCT 6420 vs 6500", "low")
        .add_artifact(artifact)
    )
    result.metrics = {"avg_de2000": 0.74, "white_de2000": 1.3}
    result.advice = {"default_policy_verdict": "stop", "reasons": ["within thresholds"]}

    payload = json.loads(result.to_json())
    for key in (
        "stage", "status", "preconditions", "actions_taken", "raw",
        "metrics", "deltas", "anomalies", "advice", "artifacts", "notes",
    ):
        assert key in payload
    assert payload["status"] == "ran"
    assert payload["anomalies"][0]["code"] == "white_warm"
    assert payload["artifacts"][0]["sha256"] == sha256_file(artifact)


def test_stage_block_and_fail():
    blocked = StageResult("measure").block("not_neutral", "gamma ramp not identity")
    assert blocked.status == "blocked"
    assert blocked.anomalies[0].severity == "high"

    failed = StageResult("build-3dlut").fail("collink_error", "returncode 1")
    assert failed.status == "failed"


# --------------------------------------------------------------------------
# CalibrationController round-trip against the in-process simulator
# --------------------------------------------------------------------------
def test_controller_full_contract_roundtrip():
    ctrl = CalibrationController.mock()

    state = ctrl.state()
    assert state["running"] is True

    enter = ctrl.enter_neutral(0, "sdr", "C:/dlc/sRGB.icm", reason="unit test")
    assert enter["active"] is True
    assert enter["corrections_reset"] is True
    assert ctrl.calibration_status()["active"] is True

    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctrl.set_white(0, "SDR", 0.3127, 0.3290)
    ctrl.set_base_grayscale(0, "SDR", 3, [0.0, 0.5, 1.0], {"r": [1, 1, 1], "g": [1, 1, 1], "b": [1, 1, 1]})
    ctrl.set_correction_grayscale(0, "SDR", 3, [0.0, 0.5, 1.0], {"r": [1.0, 1.01, 1.0], "g": [1, 1, 1], "b": [1, 0.99, 1]})
    applied = ctrl.apply_mhc(0, "SDR")
    assert applied["mhc"]["applied"] is True
    assert "correction_grayscale" in applied["mhc"]

    assert ctrl.verify_mhc(0, "SDR")["verified"] is True

    ctrl.set_3dlut(0, "SDR", "C:/run/generated/final.cube")
    state = ctrl.state()
    assert state["runtime"]["0:SDR"]["cube_path"].endswith("final.cube")

    assert ctrl.remove_mhc(0, "SDR")["removed"] is True
    assert ctrl.exit_calibration()["active"] is False


def test_mode_normalization():
    assert normalize_mode("sdr") == "SDR"
    assert normalize_mode("Hdr") == "HDR"
    with pytest.raises(ValueError):
        normalize_mode("ACM")


# --------------------------------------------------------------------------
# Refinement control law: closed-loop convergence on a synthetic panel
# --------------------------------------------------------------------------
class _SyntheticPanel:
    """A tinted display with per-channel tone imbalance, driven by per-channel
    input deviations exactly as the real correction-grayscale layer would be."""

    def __init__(self):
        # Native primaries: a wide-ish gamut. White carries only a SMALL residual
        # tint, because correction-grayscale fine-tunes a base whose MHC matrix has
        # already put white near D65 — it is not asked to move white a long way (a
        # large move would be input-ceiling-limited at full white, by design).
        self.prim = MeasuredPrimaries(0.66, 0.33, 0.27, 0.66, 0.15, 0.06, 0.3150, 0.3300)
        self.peak_Y = 120.0
        self.P = rgb_to_xyz_matrix(
            self.prim.rx, self.prim.ry, self.prim.gx, self.prim.gy,
            self.prim.bx, self.prim.by, self.prim.wx, self.prim.wy, white_Y=self.peak_Y,
        )
        self.gamma = (2.35, 2.20, 2.10)  # per-channel imbalance

    def measure(self, levels, dev: Deviations):
        patches = []
        for i, level in enumerate(levels):
            lin = []
            for ch in range(3):
                d = (dev.r, dev.g, dev.b)[ch][i]
                eff = max(0.0, min(1.0, level * d))
                lin.append(eff ** self.gamma[ch])
            xyz = matvec(self.P, lin)
            patches.append(GrayPatch(level, (xyz[0], xyz[1], xyz[2])))
        return patches


def test_refinement_loop_converges():
    panel = _SyntheticPanel()
    levels = [round(i / 10.0, 2) for i in range(11)]  # 0.0 .. 1.0
    target = RefinementTarget(white_x=0.3127, white_y=0.3290, gamma=2.2)

    dev = Deviations.identity(len(levels))
    history = []
    for _ in range(8):
        measured = panel.measure(levels, dev)
        proposal = propose_correction_grayscale(
            measured=measured, target=target, primaries=panel.prim, current=dev, damping=0.7
        )
        history.append(proposal["summary"])
        dev = Deviations.from_obj(proposal["deviations"], len(levels))

    # Re-measure with the final deviations to score the achieved state.
    final = propose_correction_grayscale(
        measured=panel.measure(levels, dev), target=target, primaries=panel.prim, current=dev, damping=0.7
    )["summary"]

    initial = history[0]
    # The loop must substantially improve and land near-perfect.
    assert final["white_de2000"] < initial["white_de2000"]
    assert final["avg_de2000"] < initial["avg_de2000"]
    assert final["white_de2000"] < 1.0, final
    assert final["avg_de2000"] < 1.0, final
    # And it must stay sane (no runaway deviations).
    assert final["max_abs_deviation"] < 2.0, final


def test_refinement_holds_dark_points():
    panel = _SyntheticPanel()
    levels = [0.0, 0.05, 1.0]
    measured = panel.measure(levels, Deviations.identity(3))
    proposal = propose_correction_grayscale(
        measured=measured,
        target=RefinementTarget(),
        primaries=panel.prim,
        current=Deviations.identity(3),
    )
    # The black patch (level 0) is too dark to balance -> held at 1.0.
    assert proposal["deviations"]["r"][0] == 1.0
    assert proposal["residuals"][0]["held_dark"] is True


def test_refinement_noise_trust_holds_correction_within_noise():
    # Per-level noise trust (SDR analogue of the HDR cube's dark_trust_weights): a bright patch
    # whose white sits a small chroma distance off-target is CORRECTED when trusted, but HELD when
    # the supplied per-level noise (σ) swamps that error — don't chase meter noise / a chromaticity
    # the panel won't hold. noise=None must reproduce the original full-step behaviour.
    D65 = (0.3127, 0.3290)

    def xyz_at(x, y, Y):
        return (x / y * Y, Y, (1 - x - y) / y * Y)

    off = (0.3200, 0.3340)  # ~0.009 off D65 in xy, comfortably above the dark floor
    patches = [GrayPatch(level=0.5, xyz=xyz_at(*off, 30.0)),
               GrayPatch(level=1.0, xyz=xyz_at(*off, 120.0))]
    prim = MeasuredPrimaries(0.64, 0.33, 0.30, 0.60, 0.15, 0.06, D65[0], D65[1])
    target = RefinementTarget(white_x=D65[0], white_y=D65[1], gamma=2.2, peak_luminance=120.0)
    cur = Deviations.identity(2)
    err = ((off[0] - D65[0]) ** 2 + (off[1] - D65[1]) ** 2) ** 0.5

    # No noise -> full step: the bright patch is corrected (deviation moves off identity).
    free = propose_correction_grayscale(measured=patches, target=target, primaries=prim, current=cur)
    bright_free = next(r for r in free["residuals"] if abs(r["level"] - 1.0) < 1e-6)
    assert bright_free["noise_trust"] == 1.0
    assert max(abs(free["deviations"][c][1] - 1.0) for c in "rgb") > 1e-3

    # σ >> chroma error -> trust 0 -> the bright patch is held at the current (identity) deviation.
    noisy = propose_correction_grayscale(measured=patches, target=target, primaries=prim,
                                         current=cur, noise={0.5: err * 10, 1.0: err * 10})
    bright_noisy = next(r for r in noisy["residuals"] if abs(r["level"] - 1.0) < 1e-6)
    assert bright_noisy["noise_trust"] == 0.0
    assert all(abs(noisy["deviations"][c][1] - 1.0) < 1e-9 for c in "rgb")

    # An UNSTABLE level (σ = +inf) is also fully held.
    unstable = propose_correction_grayscale(measured=patches, target=target, primaries=prim,
                                            current=cur, noise={1.0: float("inf")})
    bn = next(r for r in unstable["residuals"] if abs(r["level"] - 1.0) < 1e-6)
    assert bn["noise_trust"] == 0.0


def test_sdr_dark_floor_adapts_below_the_fixed_half_nit():
    # SDR uses the adaptive dark floor too: a CLEAN dark region (every read on the target white)
    # drops the floor to the low bound, so a 0.3-nit patch is CORRECTED — the old fixed 0.5-nit
    # floor would have held it. (On SDR the brightest patch IS the stable target white reference.)
    from dlc.refine import GrayPatch
    D65 = (0.3127, 0.3290)

    def xyz(Y):
        return (D65[0] / D65[1] * Y, Y, (1 - D65[0] - D65[1]) / D65[1] * Y)
    patches = [GrayPatch(level=l, xyz=xyz(Y)) for l, Y in
               [(0.05, 0.3), (0.25, 6.0), (0.5, 30.0), (0.8, 80.0), (1.0, 120.0)]]
    prim = MeasuredPrimaries(0.64, 0.33, 0.30, 0.60, 0.15, 0.06, D65[0], D65[1])
    proposal = propose_correction_grayscale(
        measured=patches,
        target=RefinementTarget(white_x=D65[0], white_y=D65[1], gamma=2.2, peak_luminance=120.0),
        primaries=prim, current=Deviations.identity(5))
    dark = next(r for r in proposal["residuals"] if abs(r["level"] - 0.05) < 1e-6)
    assert dark["measured_Y"] < 0.5          # below the OLD fixed 0.5-nit floor
    assert dark["held_dark"] is False        # ...yet corrected, because the adaptive floor dropped


# --------------------------------------------------------------------------
# Item 6: runtime GS+WB tweak (shader proxy) + deterministic display mapping
# --------------------------------------------------------------------------
def test_grayscale_tweak_proxy_roundtrip():
    """The runtime shader grayscale (GS+WB) tweak is the un-deferred proxy tier:
    set it, see it on the runtime layer, disable it, see it gone."""
    ctrl = CalibrationController.mock()
    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm", reason="item6 test")

    points = [0.0, 0.5, 1.0]
    deviations = {"r": [1.0, 1.01, 1.0], "g": [1.0, 1.0, 1.0], "b": [1.0, 0.99, 0.98]}
    res = ctrl.set_grayscale_tweak(0, "SDR", 3, points, deviations)
    assert res["monitor_mode"] == "0:SDR"

    runtime = ctrl.state()["runtime"]["0:SDR"]
    tweak = runtime["grayscale_tweak"]
    assert tweak["point_count"] == 3
    assert tweak["points"] == points
    # Deviations are float-coerced through the controller.
    assert tweak["deviations"]["b"] == pytest.approx([1.0, 0.99, 0.98])

    ctrl.disable_grayscale_tweak(0, "SDR")
    assert "grayscale_tweak" not in ctrl.state()["runtime"].get("0:SDR", {})


def test_grayscale_tweak_independent_of_3dlut():
    """The GS+WB tweak (final, after the 3D LUT) coexists with the runtime cube
    on the same runtime layer without clobbering it."""
    ctrl = CalibrationController.mock()
    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")
    ctrl.set_3dlut(0, "SDR", "C:/run/generated/final.cube")
    ctrl.set_grayscale_tweak(0, "SDR", 2, [0.0, 1.0], {"r": [1.0, 1.0], "g": [1.0, 1.0], "b": [1.0, 0.99]})

    runtime = ctrl.state()["runtime"]["0:SDR"]
    assert runtime["cube_path"].endswith("final.cube")
    assert runtime["grayscale_tweak"]["point_count"] == 2


def test_query_monitors_deterministic_mapping():
    ctrl = CalibrationController.mock()
    info = ctrl.query_monitors()
    assert info["available"] is True
    assert info["count"] == len(info["monitors"]) == 2

    m0 = info["monitors"][0]
    # Fields DLC needs to pair index <-> Argyll DISPLAY <-> physical panel.
    for field in ("index", "device_name", "rect", "primary", "hardware_id", "color_space"):
        assert field in m0, field
    assert m0["index"] == 0
    assert m0["primary"] is True
    assert m0["device_name"].endswith("DISPLAY1")
    assert info["monitors"][1]["primary"] is False
    # Distinct stable hardware ids so cross-run matching is unambiguous.
    assert m0["hardware_id"] != info["monitors"][1]["hardware_id"]


# --------------------------------------------------------------------------
# Item 6: the published API contract carries the un-deferred methods
# --------------------------------------------------------------------------
def test_api_spec_documents_item6_methods():
    from dlc.desktoplut_api_spec import build_desktoplut_api_spec

    spec = build_desktoplut_api_spec()
    methods = {m["method"]: m for m in spec["methods"]}
    assert "windows.query_monitors" in methods
    assert "runtime.set_grayscale_tweak" in methods
    assert "runtime.disable_grayscale_tweak" in methods

    # The grayscale_tweak payload shape is now pinned (was "runtime tweak payload").
    tweak_param = methods["runtime.set_grayscale_tweak"]["params"]["grayscale_tweak"]
    assert "point_count" in tweak_param["description"]
    assert "deviations" in tweak_param["description"]

    qm = methods["windows.query_monitors"]
    assert qm["mutates_state"] is False
    assert "device_name" in qm["result"]["monitors"]


def test_api_spec_methods_all_have_a_cpp_handler():
    # Conformance: every method the spec advertises MUST have a real C++ Dispatch case, so
    # API drift (and phantom methods like the removed state.snapshot/set_1dlut) is caught here
    # — not just by the Python<->mock roundtrip, which passes by construction. Skips cleanly
    # if the C++ source isn't checked out alongside (DLC can live standalone).
    import re
    from pathlib import Path

    from dlc.desktoplut_api_spec import build_desktoplut_api_spec

    # DLC root = tests/.. ; the C++ IPC server lives at <repo>/src (../src from the DLC root).
    cpp = Path(__file__).resolve().parents[1].parent / "src" / "desktoplut_ipc_server.cpp"
    if not cpp.exists():
        pytest.skip(f"C++ IPC server not found at {cpp}")
    text = cpp.read_text(encoding="utf-8", errors="replace")
    # Every handler dispatches on a string literal (`m == "..."` / `method == "..."`).
    handled = set(re.findall(r'(?:\bm|\bmethod)\s*==\s*"([^"]+)"', text))
    advertised = {m["method"] for m in build_desktoplut_api_spec()["methods"]}

    missing = sorted(advertised - handled)
    assert not missing, f"spec advertises methods with no C++ Dispatch handler: {missing}"
