"""grayscale-wb between the nodes (2026-11-14 hardware finding).

On the PA32UCXR the installed base calibration has a feature ~9 codes wide exactly at node 12 (code
153: +2 % light, green-side). The touch-up corrected node 12 against all its neighbours, the baked
curve's interpolation spread that correction over codes ~130-179 (148: +0.5 % -> -7.6 % light), and
a verify that re-read only the 32 node codes recommended "apply". These tests run the whole flow
in-process on the mock DesktopLUT and a simulated panel that renders THROUGH the mock's correction
curve with the C++ interpolation law (src/mhc.cpp EvalGrayscaleSDR_Channel, re-implemented here
independently of dlc.grayscale_wb), so a node's value reaches the greys between the nodes exactly as
on hardware. No pipe, no meter, no patch window.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import calibration_profile as cp
from dlc.adjudication import AutoAdjudicator
from dlc.calibrate import Calibration, Decision, PatchSizes
from dlc.controller import CalibrationController
from dlc.engine.patches import Transfer
from dlc.grayscale_wb import (
    GrayTouchupConfig,
    apply_node_holds,
    find_node_outliers,
    heldout_code,
    identity_payload,
    interpolated_gain,
    judge_held_out,
    slot_position,
)
from dlc.measure_loop import SyntheticPanel
from dlc.optimize import OptimizeConfig
from dlc.patch_sets import (
    build_grayscale_wb_heldout_set,
    build_grayscale_wb_set,
    build_grayscale_wb_verify_set,
    flow_patch_counts,
)
from dlc.runs import create_run

_DATE = datetime.date(2026, 11, 14)
_SMALL = PatchSizes(raw_ramp_steps=9, cube_size=3, tube_size=5, tube_radius=1, neutral_steps=9)
_OPT = OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0)
_SDR = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)

# Tonight's feature: at code 153, green-side, ~9 codes wide (148 and 160 are normal). Two amplitudes:
#  * _FEATURE_RUN — sized so the touch-up SOLVES node 12 as tonight's three runs did (composed gain off
#    its neighbours' trend: r -1.5 %, g -4.4 %, b -2.2 %; the runs' round-1 read there was Y +5.7 %,
#    xy (-0.0016, +0.0035) off the neighbours). That solved correction is what the curve smeared over
#    codes 130-179, so it is what reproduces the between-node damage.
#  * _FEATURE_SPOT — the owner's later spot read with the touch-up off (153: +2.2 %, green-side).
_FEATURE_CODE = 153
_FEATURE_HALF_WIDTH = 4.5
_FEATURE_RUN = (0.033, 0.105, 0.05)
_FEATURE_SPOT = (0.012, 0.026, 0.010)
# A plain uniform white-balance error like tonight's, the same at every level: red-heavy, light gains that
# tonight's solved curve undoes (signal r 0.992, g 1.003, b 0.997 -> light x1.018, x0.9934, x1.0066).
_RED_HEAVY = (1.018, 0.9934, 1.0066)


def _sawtooth(seed: int = 1114) -> list[float]:
    """The PA32UCXR's full-field light in the 10-bit code with its base LUT bypassed (dense reads
    118-190, 2026-11-14): within a tooth the light rises normally, then at a boundary it does not rise
    or falls back — teeth of 2-4 % every 4-12 codes; chromaticity flat. Light multiplier per code."""
    import random
    rng = random.Random(seed)
    out: list[float] = []
    while len(out) < 1024:
        width = rng.randint(4, 12)
        amp = rng.uniform(0.02, 0.04)
        out += [1.0 + amp * (j / (width - 1) - 0.5) for j in range(width)]
    return out[:1024]


def _cxx_channel(cg: dict, signal: float, ch: str) -> float:
    """src/mhc.cpp EvalGrayscaleSDR_Channel, transcribed: slot index = sqrt(signal)*(n-1), sqrt-domain
    linear interpolation of points[i]*deviation[i], squared."""
    if signal <= 0.0:
        return 0.0
    pts = cg["points"]
    dev = cg["deviations"][ch]
    n = max(2, min(32, len(pts)))
    idx = math.sqrt(min(1.0, max(0.0, signal))) * (n - 1)
    i0 = int(math.floor(idx))
    i1 = min(i0 + 1, n - 1)
    t = idx - math.floor(idx)
    s0 = math.sqrt(max(pts[i0] * dev[i0], 0.0))
    s1 = math.sqrt(max(pts[i1] * dev[i1], 0.0))
    v = s0 + (s1 - s0) * t
    return v * v


class _CurvePanel:
    """A warm SDR panel (120 nits, gamma 2.2) with a per-code light error, seen through the mock's
    correction curve whenever DesktopLUT shows it (the live preview during the touch-up, the bake
    once committed). Records the curve on screen at every read of a grey code."""

    # noise: +-0.03 % per XYZ component per read (uniform) — a good colorimeter on a bright grey, and
    # inside the DIP-less loop's 0.3 % settle / 0.4 % drift tolerances.
    def __init__(self, ctrl, *, wb=_RED_HEAVY, feature=None, noise: float = 0.0006, seed: int = 11,
                 sawtooth: bool = False):
        self.ctrl = ctrl
        self.wb = wb
        self.feature = feature
        self.sawtooth = _sawtooth() if sawtooth else None
        self.panel = SyntheticPanel(transfer=_SDR, start_temp=1.0, cold_blue_gain=1.0, noise=noise, seed=seed)
        self.calib = None
        self.reads: list[tuple[str, int, bool]] = []   # (phase, code, curve-is-identity)

    def light_gain(self, code: int) -> tuple[float, float, float]:
        g = list(self.wb)
        if self.feature and abs(code - _FEATURE_CODE) <= _FEATURE_HALF_WIDTH:
            w = math.cos(math.pi * (code - _FEATURE_CODE) / (2 * _FEATURE_HALF_WIDTH)) ** 2
            g = [gi * (1.0 + a * w) for gi, a in zip(g, self.feature)]
        return g[0], g[1], g[2]

    def __call__(self, patch):
        st = (self.ctrl.state().get("mhc") or {}).get("0:SDR") or {}
        cg = st.get("correction_grayscale") or {}
        on = bool((st.get("gs_preview_active") or st.get("gs_committed")) and cg.get("points"))
        code = int(round(max(patch.signal) * _SDR.max_cv))
        if self.calib is not None and patch.rgb[0] == patch.rgb[1] == patch.rgb[2]:
            ident = (not on) or all(abs(v - 1.0) < 1e-9 for ch in "rgb" for v in cg["deviations"][ch]) and all(
                abs(p - (i / (len(cg["points"]) - 1)) ** 2) < 1e-6 for i, p in enumerate(cg["points"]))
            self.reads.append((self.calib.runlog.phase, code, bool(ident)))
        sig = []
        for s, ch, k in zip(patch.signal, "rgb", self.light_gain(code)):
            v = _cxx_channel(cg, s, ch) if on else s
            sig.append(min(1.0, max(0.0, v * k ** (1.0 / 2.2))))
        if self.sawtooth is not None:
            # light only, in the code the PANEL receives (the green drive: the luminance), same factor on
            # every channel — the teeth move with the curve's output, the chromaticity stays flat
            tooth = self.sawtooth[min(1023, max(0, int(round(sig[1] * 1023))))]
            sig = [min(1.0, v * tooth ** (1.0 / 2.2)) for v in sig]
        return self.panel(replace(patch, signal=tuple(sig)))


class _Judge(AutoAdjudicator):
    """Takes the core's recommendation unless a choice is scripted; records every request."""

    def __init__(self, choices=None):
        self.choices = dict(choices or {})
        self.requests = []

    def adjudicate(self, request):
        self.requests.append(request)
        choice = self.choices.get(request.key, request.recommendation)
        return Decision(choice, note="test judge", payload=request.recommended_payload)

    def request(self, key):
        return next((r for r in self.requests if r.key == key), None)


def _controller() -> CalibrationController:
    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "SDR")
    return ctrl


def _run(tmp_path: Path, name: str, *, feature, choices=None, sawtooth: bool = False):
    ctrl = _controller()
    panel = _CurvePanel(ctrl, feature=feature, sawtooth=sawtooth)
    judge = _Judge(choices)
    ctx = create_run("SDR", display="synthetic", run_dir=tmp_path / name)
    calib = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                        mode="SDR", controller=ctrl, measure=panel, adjudicator=judge,
                        optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)
    panel.calib = calib
    result = calib.run("grayscale-wb")
    return calib, result, judge, panel


def _touch(calib) -> dict:
    return calib.calib["stages"]["grayscale-wb"]["digest"]


def _verify(calib) -> dict:
    return calib.calib["stages"]["verify"]["digest"]


# ---------------------------------------------------------------------------
# the interpolation law and where the held-out greys go
# ---------------------------------------------------------------------------

def test_interpolated_gain_is_the_cxx_law():
    """dlc.grayscale_wb.interpolated_gain == the transcribed C++ at every code (random curve)."""
    n = 32
    gains = {ch: [1.0 + 0.03 * math.sin(1.7 * i + k) for i in range(n)] for k, ch in enumerate("rgb")}
    cg = {"points": [(i / (n - 1)) ** 2 for i in range(n)], "deviations": gains}
    for code in range(1, 1024, 7):
        s = code / 1023.0
        u = slot_position(code, 1023, n, "sdr")
        for ch in "rgb":
            assert interpolated_gain(gains[ch], u, "sdr") * s == pytest.approx(_cxx_channel(cg, s, ch), rel=1e-9)


def test_held_out_greys_sit_at_the_interpolation_midpoints_over_the_corrected_range():
    nodes = [p[0] for p in build_grayscale_wb_set(PatchSizes(), _SDR)]
    held = [p[0] for p in build_grayscale_wb_heldout_set(PatchSizes(), _SDR)]
    floor = GrayTouchupConfig().dark_floor_nits
    assert len(held) == 24
    assert not set(held) & set(nodes)
    for i in range(31):
        inside = [c for c in held if nodes[i] < c < nodes[i + 1]]
        if _SDR.cv_to_nits(nodes[i + 1]) >= floor:      # an interval the touch-up can correct
            assert inside == [heldout_code(i, 1023, 32, "sdr")]
            assert abs(slot_position(inside[0], 1023, 32, "sdr") - (i + 0.5)) < 0.02
        else:
            assert inside == []
    # tonight's band: the greys either side of node 12 (code 153)
    assert 141 in held and 166 in held
    # the verify measures both sets once each
    ver = [p[0] for p in build_grayscale_wb_verify_set(PatchSizes(), _SDR)]
    assert sorted(ver) == sorted(nodes + held) and ver[:2] == [0, 1023]


def test_preview_and_plan_report_the_held_out_greys_and_the_time():
    plan = flow_patch_counts("grayscale-wb", PatchSizes(), _SDR)
    assert plan["stages"] == {"grayscale-wb": 32, "grayscale-wb-heldout": 24, "grayscale-wb-verify": 56}
    assert plan["held_out_greys"] == 24 and plan["total_patches"] == 112
    est = plan["time_estimate"]
    assert est["held_out_minutes"] == pytest.approx(2 * 24 * 11.7 / 60, abs=0.05)
    assert est["minutes"][0] > est["without_held_out_minutes"][0]


# ---------------------------------------------------------------------------
# pure statistics
# ---------------------------------------------------------------------------

def test_node_outlier_flags_a_single_spike_and_not_its_neighbours():
    n = 32
    codes = [round(1023 * (i / 31) ** 2) for i in range(n)]
    base = {"r": 0.992, "g": 1.003, "b": 0.997}
    jitter = [0.0015 * math.sin(2.3 * i) for i in range(n)]
    devs = {ch: [1.0 if i < 8 else base[ch] * (1.0 + jitter[i] * (k + 1) / 3) for i in range(n)]
            for k, ch in enumerate("rgb")}
    # tonight's node 12, composed (luminance x balance)
    devs["r"][12], devs["g"][12], devs["b"][12] = 0.9776, 0.9596, 0.9770
    out = find_node_outliers(devs, range(8, n), codes=codes, kind="sdr")
    assert out["available"] is True
    assert [f["index"] for f in out["flagged"]] == [12]
    f = out["flagged"][0]
    assert f["code"] == 153 and f["spreads_over_codes"] == [129, 180]
    assert f["solved_vs_neighbours_pct"]["g"] < -3.0
    held, detail = apply_node_holds({**identity_payload([c / 1023 for c in codes]),
                                     "deviations": devs, "luminance": [1.0] * n,
                                     "rgb": devs}, [12], kind="sdr")
    for ch in "rgb":
        assert held["deviations"][ch][12] == pytest.approx(f["neighbour_interpolation"][ch], rel=1e-5)
    # a smooth curve (no spike) flags nothing
    devs["r"][12], devs["g"][12], devs["b"][12] = base["r"], base["g"], base["b"]
    assert find_node_outliers(devs, range(8, n), codes=codes, kind="sdr")["flagged"] == []


def test_regression_rule_uses_the_runs_own_repeatability():
    cfg = GrayTouchupConfig()
    ty = 2.0
    good = [ty * 0.9505 / 1.0, ty, ty * 1.089]          # D65 at the target

    def scaled(k):
        return [v * k for v in good]

    pairs = [{"code": 100 + i, "target_Y": ty, "final_xyz": scaled(1.0 + 0.002 * ((-1) ** i)),
              "verify_xyz": scaled(1.0 - 0.002 * ((-1) ** i))} for i in range(10)]
    greys = [{"code": 50, "target_Y": ty, "before_xyz": scaled(1.005)},
             {"code": 51, "target_Y": ty, "before_xyz": scaled(1.005)}]
    out = judge_held_out(greys, {50: scaled(0.93), 51: scaled(1.004)}, pairs, config=cfg)
    assert out["n_judged"] == 2 and out["regressed_codes"] == [50]
    # without any repeatability estimate a grey is reported, never judged
    out = judge_held_out(greys, {50: scaled(0.93), 51: scaled(1.004)}, pairs[:3], config=cfg)
    assert out["available"] is False and all(g["judged"] is False for g in out["greys"])


# ---------------------------------------------------------------------------
# the flow, end to end on the simulated panel
# ---------------------------------------------------------------------------

def test_narrow_feature_left_as_solved_is_flagged_and_recommended_revert(tmp_path: Path):
    calib, result, judge, _panel = _run(tmp_path, "feature_accept", feature=_FEATURE_RUN,
                                        choices={"grayscale-wb:node-outlier": "accept"})
    touch = _touch(calib)
    # (a1) the LIGHT node 12 asked for (the feature's +light) is not shared by its neighbours: the
    # light-trend seam takes it off the node (no isolated light step is committed)
    light = judge.request("grayscale-wb:light-trend")
    assert light is not None and light.recommendation == "trend" and light.options == ("trend", "abort")
    assert 12 in touch["light_trend"]["material"]
    row12 = next(r for r in touch["light_trend"]["nodes"] if r["index"] == 12)
    assert row12["committed_pct"] * row12["need_pct"] >= 0 and abs(row12["committed_pct"]) <= abs(row12["need_pct"])
    assert "node 12 (code 153)" in light.question
    # (a2) what remains at node 12 is its BALANCE (the feature's green-side colour), still off its
    # neighbours' trend: the node-outlier seam raises it with its numbers (size + direction per channel)
    seam = judge.request("grayscale-wb:node-outlier")
    assert seam is not None and seam.recommendation == "hold"
    assert seam.options == ("hold", "accept", "abort")
    flagged = touch["node_outliers"]["flagged"]
    assert [f["index"] for f in flagged] == [12] and flagged[0]["code"] == 153
    f = flagged[0]
    assert f["solved_vs_trend_pct"]["r"] > 1.0 and f["solved_vs_trend_pct"]["b"] > 0.5   # less green
    assert f["solved_vs_trend_pct"]["g"] < 0.0
    assert f["hold_changes_grey_de"] >= touch["target_de2000"]
    assert {e["index"] for e in touch["node_outliers"]["echoes"]} <= {10, 11, 13, 14}
    assert "node 12 (code 153)" in seam.question and "spreads over codes 129-180" in seam.question
    assert "grayscale_wb_node_holds" not in calib.calib          # accepted as solved
    # (b) the verify digest shows the between-node regression, before vs after per grey: the
    # light is no longer smeared (the light trend), the colour is
    ho = _verify(calib)["gswb_held_out"]
    assert ho["available"] is True and ho["n_judged"] == 24
    assert set(ho["regressed_codes"]) == {141, 166}, ho["worst"]
    for w in ho["worst"][:2]:
        assert w["code"] in (141, 166) and w["regressed"]
        assert w["after"]["de2000"] > w["before"]["de2000"] + w["threshold"]
        assert abs(w["after"]["Y_err_pct"] - w["before"]["Y_err_pct"]) < 0.5
        b, a = w["before"]["dxy"], w["after"]["dxy"]
        assert math.hypot(*a) > 1.5 * math.hypot(*b)                         # the colour smear
    # (c) the recommendation is revert - a recommendation: the judge's answer stands
    req = judge.request("verify:accept")
    assert req.recommendation == "revert" and req.digest["held_out_regressed"] is True
    assert req.digest["within_quality"] is True                    # the scores alone still "pass"
    assert "2 WORSE than before" in req.question and "code 141" in req.question
    assert result.status == "reverted"


def test_narrow_feature_held_at_the_neighbours_is_judged_too(tmp_path: Path):
    """'hold' (the seam's recommendation) puts node 12 on its neighbours' interpolation: the greys
    around it are no longer damaged, but the feature's own code is left uncorrected - and here, with
    the neighbours' white-balance correction on top of a green-side feature, it reads worse than before.
    The held-out verify judges the held node like any un-fitted grey and says so (revert); whether a
    9-code band ~1 dE worse is worth 24 better greys is the judge's call, not the code's."""
    calib, result, judge, _panel = _run(tmp_path, "feature_hold", feature=_FEATURE_RUN)
    holds = calib.calib["grayscale_wb_node_holds"]
    assert holds["applied"] is True and holds["nodes"] == [12]
    baked = calib.calib["grayscale_wb_touchup"]          # what was committed (the revert put the prior back)
    for ch in "rgb":   # node 12 is its neighbours' interpolation, not the solved spike
        assert baked["deviations"][ch][12] == pytest.approx(holds["detail"]["12"]["to"][ch], rel=1e-5)
        assert holds["solved_payload"]["deviations"][ch][12] != pytest.approx(baked["deviations"][ch][12], rel=1e-3)
    ho = _verify(calib)["gswb_held_out"]
    by_code = {g["code"]: g for g in ho["greys"]}
    assert not by_code[141]["regressed"] and not by_code[166]["regressed"]
    assert by_code[141]["after"]["de2000"] < by_code[141]["before"]["de2000"]
    assert by_code[153]["kind"] == "held_node" and by_code[153]["judged"]
    assert ho["regressed_codes"] == [153]
    req = judge.request("verify:accept")
    assert req.recommendation == "revert" and "held node" in req.question
    assert result.status == "reverted"


def test_spot_read_amplitude_is_flagged_and_its_smear_judged_against_the_white_balance_gain(tmp_path: Path):
    """At the owner's spot-read amplitude (+2.2 %) node 12's light is still taken off the node and its
    balance still flagged at the seam, but the small colour smear left between the nodes is smaller
    than the white-balance error the touch-up removes there: every grey between the nodes reads
    BETTER than before, so the recommendation stays apply. The verdict follows the measurement, not
    the flag."""
    calib, result, judge, _panel = _run(tmp_path, "feature_spot", feature=_FEATURE_SPOT,
                                        choices={"grayscale-wb:node-outlier": "accept"})
    assert 12 in _touch(calib)["light_trend"]["changed"]
    assert [f["index"] for f in _touch(calib)["node_outliers"]["flagged"]] == [12]
    ho = _verify(calib)["gswb_held_out"]
    assert ho["n_regressed"] == 0, ho["worst"]
    by_code = {g["code"]: g for g in ho["greys"]}
    assert abs(by_code[141]["after"]["Y_err_pct"] - by_code[141]["before"]["Y_err_pct"]) < 0.5  # no light smear
    assert by_code[141]["after"]["de2000"] < by_code[141]["before"]["de2000"]               # net better
    assert judge.request("verify:accept").recommendation == "apply"
    assert result.status == "completed"


def test_plain_white_balance_error_passes_with_apply(tmp_path: Path):
    calib, result, judge, _panel = _run(tmp_path, "plain", feature=None)
    assert judge.request("grayscale-wb:node-outlier") is None
    assert _touch(calib)["node_outliers"]["flagged"] == []
    ho = _verify(calib)["gswb_held_out"]
    assert ho["available"] is True and ho["n_judged"] == 24 and ho["n_regressed"] == 0, ho["worst"]
    # the touch-up made the greys between the nodes better, not just the nodes
    improved = [g for g in ho["greys"] if g.get("judged") and g["after"]["de2000"] < g["before"]["de2000"]]
    assert len(improved) == ho["n_judged"]
    req = judge.request("verify:accept")
    assert req.recommendation == "apply" and req.digest["held_out_regressed"] is False
    assert "0 WORSE than before" in req.question
    assert result.status == "completed"
    # the plan seam stated the held-out greys and the time they cost
    plan = judge.request("resolve-target:plan")
    assert "24 held-out greys" in plan.question and "min" in plan.question
    assert plan.digest["patch_plan"]["stages"]["grayscale-wb-heldout"] == 24
    assert plan.digest["patch_plan"]["time_estimate"]["held_out_minutes"] > 0


def test_sawtooth_light_is_left_alone_and_the_white_balance_still_fixed(tmp_path: Path):
    """The PA32UCXR's full-field light is a sawtooth in the code (teeth of 2-4 % every 4-12 codes),
    its chromaticity flat. Per-node light corrections would sample one phase each; the flow must commit
    no per-node light step, leave the light between the nodes no worse than before, and still remove a
    uniform white-balance error everywhere."""
    calib, result, judge, _panel = _run(tmp_path, "sawtooth", feature=None, sawtooth=True)
    touch = _touch(calib)
    lt = touch["light_trend"]
    assert judge.request("grayscale-wb:light-trend") is not None
    assert len(lt["teeth"]) >= len(lt["nodes"]) // 2               # most nodes sit on a tooth
    # the committed light: never more than any of the five nodes around it asks for, none at the ends
    rows = {r["index"]: r for r in lt["nodes"]}
    for k, r in rows.items():
        window = [rows.get(j) for j in range(k - 2, k + 3)]
        if any(w is None for w in window):
            assert r["committed_pct"] == 0.0
        elif r["committed_pct"] != 0.0:
            assert all(w["need_pct"] * r["committed_pct"] > 0 and abs(w["need_pct"]) >= abs(r["committed_pct"]) - 1e-9
                       for w in window)
    ho = _verify(calib)["gswb_held_out"]
    assert ho["n_judged"] == 24 and ho["n_regressed"] == 0, ho["worst"]
    rows = [g for g in ho["greys"] if g["judged"]]
    # light between the nodes: no worse than before (per grey against its OWN before read - the teeth are
    # there before and after alike; a balance nudge may move a grey across a tooth edge either way)
    worse = sorted(abs(g["after"]["Y_err_pct"]) - abs(g["before"]["Y_err_pct"]) for g in rows)
    assert worse[len(worse) // 2] <= 0.25                          # the typical grey: unchanged
    assert worse[-1] <= 4.0                                        # never more than one tooth
    # the white balance: fixed between the nodes too
    for g in rows:
        assert math.hypot(*g["after"]["dxy"]) < 0.6 * math.hypot(*g["before"]["dxy"])
        assert g["after"]["de2000"] < g["before"]["de2000"]
    assert sum(math.hypot(*g["after"]["dxy"]) for g in rows) < 0.3 * sum(math.hypot(*g["before"]["dxy"])
                                                                          for g in rows)
    assert judge.request("verify:accept").recommendation == "apply"
    assert result.status == "completed"


def test_before_state_is_the_users_own_curve_when_it_was_on(tmp_path: Path):
    """A revert gives the user's pre-run curve back, so that is the BEFORE state the held-out greys
    are read in (presented through the live editor as its own slots, stored back exactly)."""
    ctrl = _controller()
    n = 10
    r = [1.0] * n
    r[4] = 1.01
    ctrl.set_correction_grayscale(0, "SDR", n, [i / (n - 1) for i in range(n)],
                                  {"r": r, "g": [1.0] * n, "b": [1.0] * n}, gamma=2.2)
    ctrl.set_layers(0, "SDR", grayscale=True)
    prior = ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]
    panel = _CurvePanel(ctrl, feature=None)
    seen: list[dict] = []
    orig = ctrl.grayscale_set_live

    def spy(*a, **k):
        out = orig(*a, **k)
        seen.append(ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"])
        return out

    ctrl.grayscale_set_live = spy
    ctx = create_run("SDR", display="synthetic", run_dir=tmp_path / "prior")
    calib = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                        mode="SDR", controller=ctrl, measure=panel, adjudicator=_Judge(),
                        optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)
    panel.calib = calib
    calib.run("grayscale-wb")
    state = _touch(calib)["held_out"]["before_state"]
    assert state["table"] == "prior" and state["point_count"] == n
    shown = [cg for cg in seen if cg.get("point_count") == n]
    assert shown, "the prior curve was never presented"
    for cg in shown:
        assert cg["points"] == pytest.approx(prior["points"], abs=1e-9)
        for ch in "rgb":
            assert cg["deviations"][ch] == pytest.approx(prior["deviations"][ch], abs=1e-9)


def test_a_pause_at_the_node_outlier_seam_then_hold_on_resume(tmp_path: Path):
    """The live hardware rhythm: the run pauses at grayscale-wb:node-outlier (exit 10), the judge
    answers hold, the resumed run puts the node on its neighbours' interpolation through the STILL
    OPEN live session, then commits and verifies."""
    from dlc.calibrate import AdjudicationRequired, MappingAdjudicator

    ctrl = _controller()
    panel = _CurvePanel(ctrl, feature=_FEATURE_RUN)
    run_dir = tmp_path / "pause"

    def make(decisions):
        from dlc.runs import open_run
        ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
            else create_run("SDR", display="synthetic", run_dir=run_dir)
        c = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                        mode="SDR", controller=ctrl, measure=panel,
                        adjudicator=MappingAdjudicator({k: Decision(v) for k, v in decisions.items()}),
                        optimize_config=_OPT, patch_sizes=_SMALL, run_date=_DATE)
        panel.calib = c
        return c

    first = make({"resolve-target:plan": "approve"})
    with pytest.raises(AdjudicationRequired) as exc:
        first.run("grayscale-wb")
    assert exc.value.request.key == "grayscale-wb:light-trend"
    second = make({"resolve-target:plan": "approve", "grayscale-wb:light-trend": "trend"})
    with pytest.raises(AdjudicationRequired) as exc:
        second.run("grayscale-wb")
    assert exc.value.request.key == "grayscale-wb:node-outlier"
    assert second.calib["grayscale_wb_light_trend"]["applied"] is True
    assert ctrl.state()["mhc"]["0:SDR"].get("gs_live_active") is True       # nothing baked yet
    resumed = make({"resolve-target:plan": "approve", "grayscale-wb:light-trend": "trend",
                    "grayscale-wb:node-outlier": "hold", "verify:accept": "apply"})
    result = resumed.run("grayscale-wb")
    assert result.status == "completed"
    assert resumed.calib["grayscale_wb_node_holds"]["nodes"] == [12]
    baked = ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]
    n = baked["point_count"]
    for ch in "rgb":
        g12 = baked["points"][12] * baked["deviations"][ch][12] / (12 / (n - 1)) ** 2
        assert g12 == pytest.approx(resumed.calib["grayscale_wb_node_holds"]["detail"]["12"]["to"][ch], rel=2e-3)


def test_held_out_greys_are_excluded_from_the_fit(tmp_path: Path):
    calib, _result, _judge, panel = _run(tmp_path, "excluded", feature=_FEATURE_RUN,
                                         choices={"grayscale-wb:node-outlier": "accept"})
    touch = _touch(calib)
    nodes = {p[0] for p in build_grayscale_wb_set(PatchSizes(), _SDR)}
    held = set(touch["held_out"]["codes"])
    assert len(held) == 24 and not held & nodes
    # the fit visited, nudged and recorded only node codes
    assert {p["code"] for p in touch["per_point"]} == nodes
    assert all(u["index"] < 32 for u in touch["updates"])
    assert all(int(round(u["level"] * 1023)) in nodes for u in touch["updates"])
    # every touch-up read of a held-out grey saw the BEFORE state (the identity curve) ...
    session = [(code, ident) for phase, code, ident in panel.reads if phase == "grayscale-wb" and code in held]
    assert {c for c, _ in session} == held
    assert all(ident for _c, ident in session)
    # ... was one measurement (no tuning rounds), and each came back in the verify, on the bake
    assert all(g.get("before_xyz") and "rounds" not in g for g in touch["held_out"]["greys"])
    verify = {code for phase, code, _i in panel.reads if phase == "measure:verify" and code in held}
    assert verify == held
    assert {g["code"] for g in touch["held_out"]["greys"]} == held
    assert touch["held_out"]["before_state"]["table"] == "identity"
