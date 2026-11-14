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
# A strongly green-side feature (the same width): its balance smear between the nodes is far beyond one just
# noticeable difference at that light, so leaving it as solved must end in revert.
_FEATURE_STRONG_GREEN = (0.02, 0.30, 0.05)
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


def _lerp(curve, t: float) -> float:
    n = len(curve)
    x = min(1.0, max(0.0, t)) * (n - 1)
    i0 = int(math.floor(x))
    i1 = min(i0 + 1, n - 1)
    return curve[i0] + (curve[i1] - curve[i0]) * (x - i0)


def _invert_curve(curve, size: int = 4096) -> list[float]:
    """The inverse of a monotone curve on [0, 1], tabulated."""
    out, j = [], 0
    n = len(curve)
    for k in range(size):
        y = k / (size - 1)
        while j < n - 2 and curve[j + 1] < y:
            j += 1
        a, b = curve[j], curve[j + 1]
        f = 0.0 if b <= a else min(1.0, max(0.0, (y - a) / (b - a)))
        out.append((j + f) / (n - 1))
    return out


def _lift_base(amp=(0.05, 0.06, 0.045)) -> dict[str, list[float]]:
    """A base 1D LUT like the owner's around codes 129-180: it lifts the signal 4.5-6 % (per channel)
    around signal 0.15 (code 153) and is identity elsewhere. Monotone."""
    out = {}
    for ch, a in zip("rgb", amp):
        out[ch] = [t * (1.0 + a * math.exp(-((t - 0.15) / 0.05) ** 2)) for t in (j / 1023 for j in range(1024))]
    return out


def _install_base(ctrl, base, path: Path) -> None:
    lines = ["LUT_1D_SIZE 1024"] + [f"{base['r'][j]:.8f} {base['g'][j]:.8f} {base['b'][j]:.8f}" for j in range(1024)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctrl.set_base_lut(0, "SDR", str(path), 0.0)


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
                 sawtooth: bool = False, base=None, gamma24_bake: bool = False):
        self.ctrl = ctrl
        self.wb = wb
        self.feature = feature
        self.sawtooth = _sawtooth() if sawtooth else None
        # The installed BASE 1D LUT (per channel, 1024 entries) and the panel it calibrates: the bake runs
        # base -> fine-tune curve (src/mhc_icc.cpp ComposeMhcScanout), the panel shows base^-1(output)^2.2,
        # so base + identity curve = the target. SDR GAMMA 2.4 reads the base at t^(2.4/2.2) in the BAKE
        # only (the live edit holds it off).
        self.base = base
        self.gamma24_bake = gamma24_bake
        self._inv = {ch: _invert_curve(base[ch]) for ch in "rgb"} if base is not None else None
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
        baked = bool(st.get("gs_committed")) and not st.get("gs_preview_active")
        for s, ch, k in zip(patch.signal, "rgb", self.light_gain(code)):
            t = s ** (2.4 / 2.2) if (self.gamma24_bake and baked and s > 0) else s
            v = _lerp(self.base[ch], t) if self.base is not None else t
            out = _cxx_channel(cg, v, ch) if on else v
            w = _lerp(self._inv[ch], out) if self._inv is not None else out
            sig.append(min(1.0, max(0.0, w * k ** (1.0 / 2.2))))
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


def _run(tmp_path: Path, name: str, *, feature, choices=None, sawtooth: bool = False, base=None,
         gamma24_bake: bool = False):
    ctrl = _controller()
    if base is not None:
        _install_base(ctrl, base, tmp_path / f"{name}_base.cube")
    panel = _CurvePanel(ctrl, feature=feature, sawtooth=sawtooth, base=base, gamma24_bake=gamma24_bake)
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
    from dlc.patch_sets import grayscale_wb_bracket_codes
    bracket = grayscale_wb_bracket_codes(held)
    assert len(bracket) == 8 and set(bracket) <= set(held)
    assert sorted(ver[:-8]) == sorted(nodes + held) and ver[:2] == [0, 1023]
    assert ver[-8:] == bracket                     # the verify bracket: re-presented at the end


def test_preview_and_plan_report_the_held_out_greys_and_the_time():
    plan = flow_patch_counts("grayscale-wb", PatchSizes(), _SDR)
    assert plan["stages"] == {"grayscale-wb": 32, "grayscale-wb-heldout": 24, "grayscale-wb-verify": 64}
    assert plan["held_out_greys"] == 24 and plan["total_patches"] == 120
    est = plan["time_estimate"]
    assert est["held_out_presentations"] == 2 * 24 + 16
    assert est["held_out_minutes"] == pytest.approx((2 * 24 + 16) * 11.7 / 60, abs=0.05)
    assert "one display" in est["basis"]
    assert est["minutes"][0] > est["without_held_out_minutes"][0]


# ---------------------------------------------------------------------------
# pure statistics
# ---------------------------------------------------------------------------

def _devs(n=32, *, spike=None, pair=None, knee=None):
    base = {"r": 0.992, "g": 1.003, "b": 0.997}
    jitter = [0.0015 * math.sin(2.3 * i) for i in range(n)]
    devs = {ch: [1.0 if i < 8 else base[ch] * (1.0 + jitter[i] * (k + 1) / 3) for i in range(n)]
            for k, ch in enumerate("rgb")}
    if spike is not None:     # tonight's node 12 balance (composed, light already shared): r +2.2, g -0.8, b +1.6 %
        for ch, f in zip("rgb", (1.022, 0.992, 1.016)):
            devs[ch][spike] *= f
    if pair is not None:
        for k in (pair, pair + 1):
            for ch, f in zip("rgb", (1.022, 0.992, 1.016)):
                devs[ch][k] *= f
    if knee is not None:      # a smooth change of slope at node `knee`: red falls 0.6 % per node above it
        for i in range(knee, n):
            devs["r"][i] *= math.exp(-0.006 * (i - knee))
    return devs


_CODES = [round(1023 * (i / 31) ** 2) for i in range(32)]


def test_node_outlier_flags_a_single_spike_with_its_numbers():
    out = find_node_outliers(_devs(spike=12), range(8, 32), codes=_CODES, kind="sdr",
                             materiality_de=0.6, light_exponent=[2.2] * 32)
    assert out["available"] is True
    assert [f["nodes"] for f in out["flagged"]] == [[12]]
    f = out["flagged"][0]
    assert f["code"] == 153 and f["spreads_over_codes"] == [129, 180]
    assert f["solved_vs_neighbours_pct"]["r"][0] > 1.5 and f["hold_changes_grey_de"] >= 0.6
    held, _detail = apply_node_holds({**identity_payload([c / 1023 for c in _CODES]), "deviations": _devs(spike=12),
                                      "luminance": [1.0] * 32, "rgb": _devs(spike=12)}, [12], kind="sdr")
    for ch in "rgb":
        assert held["deviations"][ch][12] == pytest.approx(f["neighbour_interpolation"][ch][0], rel=1e-5)
    assert find_node_outliers(_devs(), range(8, 32), codes=_CODES, kind="sdr")["flagged"] == []


def test_two_adjacent_nodes_are_one_feature_not_a_node_and_its_echo():
    out = find_node_outliers(_devs(pair=12), range(8, 32), codes=_CODES, kind="sdr",
                             materiality_de=0.6, light_exponent=[2.2] * 32)
    assert [f["nodes"] for f in out["flagged"]] == [[12, 13]]
    f = out["flagged"][0]
    assert f["neighbours"]["left"]["index"] == 11 and f["neighbours"]["right"]["index"] == 14


def test_a_smooth_knee_in_the_balance_is_not_a_narrow_feature():
    """A real change of slope (red falling 0.6 % per node above node 16 - 5 % by the top, far more than
    the 0.6 dE materiality): it sits on the line one side draws, so it is a trend, not a feature."""
    devs = _devs(knee=16)
    out = find_node_outliers(devs, range(8, 32), codes=_CODES, kind="sdr", materiality_de=0.6,
                             light_exponent=[2.2] * 32)
    assert out["flagged"] == [], out["flagged"]
    from dlc.grayscale_wb import gain_change_de
    assert gain_change_de({"r": devs["r"][31] / devs["r"][16], "g": 1.0, "b": 1.0}, light_exponent=2.2) >= 0.6


def test_balance_smoothing_keeps_the_light_and_takes_the_line():
    from dlc.grayscale_wb import apply_balance_smoothing, compose_payload
    n = 32
    lum = [1.0 + 0.001 * i for i in range(n)]
    rgb = {ch: list(v) for ch, v in _devs(spike=12).items()}
    pay = compose_payload([c / 1023 for c in _CODES], lum, rgb)
    out, detail = apply_balance_smoothing(pay, [[12]])
    assert out["luminance"][12] == lum[12]
    for ch in "rgb":
        line = math.exp(0.5 * (math.log(rgb[ch][11]) + math.log(rgb[ch][13])))
        gm = (math.exp(0.5 * (math.log(rgb["r"][11]) + math.log(rgb["r"][13])))
              * math.exp(0.5 * (math.log(rgb["g"][11]) + math.log(rgb["g"][13])))
              * math.exp(0.5 * (math.log(rgb["b"][11]) + math.log(rgb["b"][13])))) ** (1 / 3)
        assert out["rgb"][ch][12] == pytest.approx(line / gm, rel=1e-9)
    assert detail["12"]["between_nodes"] == [11, 13]


# ---------------------------------------------------------------------------
# the flow, end to end on the simulated panel
# ---------------------------------------------------------------------------

def test_tonights_feature_left_as_solved_smears_colour_below_one_jnd(tmp_path: Path):
    """Tonight's feature accepted as solved: with the light committed as shared light, what is left between the
    nodes is node 12's balance smear - it makes 141 and 166 worse in colour, but at 1.4-1.9 nit by about a third of
    one just noticeable difference (BT.2124 dE_ITP): not a visible regression, so apply."""
    calib, result, judge, _panel = _run(tmp_path, "feature_accept_run", feature=_FEATURE_RUN,
                                        choices={"grayscale-wb:node-outlier": "accept"})
    ho = _verify(calib)["gswb_held_out"]
    by = {g["code"]: g for g in ho["greys"]}
    for code in (141, 166):
        assert by[code]["chroma"]["after_itp"] > by[code]["chroma"]["before_itp"]          # worse ...
        assert by[code]["chroma"]["after_de"] > by[code]["chroma"]["before_de"]
        c = by[code]["chroma"]
        assert c["real"] and c["change_de"] > c["threshold_de"] >= 0.6                   # worse beyond the target ...
        assert not c["regressed"] and c["regressed_below_visibility"]                    # ... but not visibly
        assert c["visibility_itp"] < 1.0
    assert {b["code"] for b in ho["regressed_below_visibility"]} == {141, 166}
    req = judge.request("verify:accept")
    assert req.recommendation == "apply"                                       # apply, with the list named
    assert "code 141: colour +" in req.question and "code 166: colour +" in req.question
    assert "dE_ITP" in req.question and "revert when a grey reads worse" in req.question
    assert result.status == "completed"


def test_the_size_seam_judges_the_committed_curve_not_the_solver(tmp_path: Path):
    """A light-only tooth of +14 % at code 153: the solver asks node 12 for a large luminance cut (over the 5 %
    warning); the light-trend rule commits none of it. The size seam measures the COMMITTED curve, so it does
    not fire (2026-11-14 HW: it fired quoting the solver's 6.7 % while the committed light was <= 0.44 %)."""
    calib, result, judge, _panel = _run(tmp_path, "size", feature=(0.14, 0.14, 0.14))
    touch = _touch(calib)
    assert touch["large_correction"] is True and touch["max_abs_luminance"] >= 0.05     # the solver's raw curve
    assert judge.request("grayscale-wb:touchup-size") is None
    from dlc.grayscale_wb import committed_size
    assert committed_size(calib.calib["grayscale_wb_touchup"])["large_correction"] is False
    assert result.status == "completed"


def test_narrow_feature_left_as_solved_is_flagged_and_recommended_revert(tmp_path: Path):
    calib, result, judge, _panel = _run(tmp_path, "feature_accept", feature=_FEATURE_STRONG_GREEN,
                                        choices={"grayscale-wb:node-outlier": "accept"})
    touch = _touch(calib)
    # (a1) the LIGHT node 12 asked for (the feature's +light) is not shared by the nodes around it
    light = judge.request("grayscale-wb:light-trend")
    assert light is not None and light.recommendation == "trend" and light.options == ("trend", "abort")
    assert 12 in touch["light_trend"]["material"]
    row12 = next(r for r in touch["light_trend"]["nodes"] if r["index"] == 12)
    assert abs(row12["committed_pct"]) <= abs(row12["need_pct"]) and row12["committed_pct"] * row12["need_pct"] >= 0
    assert "node 12 (code 153)" in light.question
    # (a2) what remains at node 12 is its BALANCE (the feature's green-side colour): the seam raises it
    seam = judge.request("grayscale-wb:node-outlier")
    assert seam is not None and seam.recommendation == "smooth"
    assert seam.options == ("smooth", "hold", "accept", "abort")
    flagged = touch["node_outliers"]["flagged"]
    assert [f["nodes"] for f in flagged] == [[12]] and flagged[0]["code"] == 153
    f = flagged[0]
    pct = f["solved_vs_neighbours_pct"]
    assert pct["g"][0] < pct["r"][0] and pct["g"][0] < pct["b"][0]   # green-side: green pulled down most
    assert f["hold_changes_grey_de"] >= touch["target_de2000"]
    assert "node 12 (code 153)" in seam.question and "spreads over codes 129-180" in seam.question
    assert "grayscale_wb_node_holds" not in calib.calib          # accepted as solved
    # (b) the verify digest: the colour smear between the nodes, light untouched (it was not smeared)
    ho = _verify(calib)["gswb_held_out"]
    assert ho["available"] is True and ho["n_judged"] == 24
    assert set(ho["chroma_regressed_codes"]) == {141, 166}, ho["worst"]
    for w in ho["worst"][:2]:
        assert w["code"] in (141, 166) and w["chroma"]["regressed"]
        assert w["chroma"]["visibility_itp"] >= 1.0 and w["chroma"]["change_de"] > w["chroma"]["threshold_de"]
    # (c) the recommendation is revert - a recommendation: the judge's answer stands
    req = judge.request("verify:accept")
    assert req.recommendation == "revert" and req.digest["held_out_regressed"] is True
    assert req.digest["within_quality"] is True                    # the scores alone still "pass"
    assert "2 WORSE than before" in req.question and "code 141" in req.question
    assert result.status == "reverted"


@pytest.mark.parametrize("choice", ["smooth", "hold"])
def test_narrow_feature_smoothed_or_held_is_judged_against_its_prediction_and_applies(tmp_path: Path, choice):
    """The owner's outcome ('smooth', recommended) or 'hold': the greys around node 12 are no longer damaged,
    and code 153 itself - its own defect now uncorrected - does what was PREDICTED; the verify recommends
    apply and says the defect is left for the base LUT."""
    calib, result, judge, _panel = _run(tmp_path, f"feature_{choice}", feature=_FEATURE_RUN,
                                        choices={"grayscale-wb:node-outlier": choice})
    holds = calib.calib["grayscale_wb_node_holds"]
    assert holds["applied"] is True and holds["nodes"] == [12] and holds["mode"] == choice
    baked = calib.calib["grayscale_wb_touchup"]
    if choice == "smooth":
        assert baked["luminance"][12] == pytest.approx(holds["solved_payload"]["luminance"][12])   # light kept
        for ch in "rgb":
            assert baked["rgb"][ch][12] == pytest.approx(holds["detail"]["12"]["to"][ch], rel=1e-5)
    ho = _verify(calib)["gswb_held_out"]
    assert ho["n_regressed"] == 0, ho["worst"]
    by_code = {g["code"]: g for g in ho["greys"]}
    assert by_code[141]["after"]["de2000"] < by_code[141]["before"]["de2000"]
    pred = {p["code"]: p for p in ho["predicted"]}
    assert pred[153]["as_predicted"] is True and pred[153]["kind"].endswith("_node")
    assert "code(s) 153 is left uncorrected" in ho["defect_left_uncorrected"]
    req = judge.request("verify:accept")
    assert req.recommendation == "apply"
    assert "did what was predicted" in req.question and "left uncorrected" in req.question
    assert result.status == "completed"


def test_spot_read_amplitude_is_flagged_and_its_smear_judged_against_the_white_balance_gain(tmp_path: Path):
    """At the owner's spot-read amplitude (+2.2 %) node 12 is still flagged, but the small colour smear
    left between the nodes is inside the touch-up's own tolerance while the white-balance error it removes
    there is not: the recommendation stays apply. The verdict follows the measurement, not the flag."""
    calib, result, judge, _panel = _run(tmp_path, "feature_spot", feature=_FEATURE_SPOT,
                                        choices={"grayscale-wb:node-outlier": "accept"})
    assert [f["nodes"] for f in _touch(calib)["node_outliers"]["flagged"]] == [[12]]
    ho = _verify(calib)["gswb_held_out"]
    assert ho["n_regressed"] == 0, ho["worst"]
    by_code = {g["code"]: g for g in ho["greys"]}
    assert abs(by_code[141]["light"]["change_pct"]) < 0.5                                # no light smear
    assert by_code[141]["after"]["de2000"] < by_code[141]["before"]["de2000"]            # net better
    assert judge.request("verify:accept").recommendation == "apply"
    assert result.status == "completed"


def test_plain_white_balance_error_passes_with_apply(tmp_path: Path):
    calib, result, judge, _panel = _run(tmp_path, "plain", feature=None)
    assert judge.request("grayscale-wb:node-outlier") is None
    assert judge.request("grayscale-wb:light-trend") is None       # no light move a read could see
    assert _touch(calib)["node_outliers"]["flagged"] == []
    ho = _verify(calib)["gswb_held_out"]
    assert ho["available"] is True and ho["n_judged"] == 24 and ho["n_regressed"] == 0, ho["worst"]
    assert ho["drift"]["known"] is True and ho["drift"]["rests_on_reads"] == 32
    assert ho["drift"]["session_bracket"]["n"] == 8 and ho["drift"]["verify_bracket"]["n"] == 8
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
    # no base cube on this mock MHC and GAMMA 2.4 unreported: the plan says so
    assert "no base 1D cube" in plan.question and "GAMMA 2.4" in plan.question
    # every held-out before-read took at least two reads (its own repeatability)
    assert all(len(g.get("before_reads") or ()) >= 2 for g in _touch(calib)["held_out"]["greys"])


def test_held_out_greys_sit_at_the_curve_mid_positions_through_the_base(tmp_path: Path):
    """Review item 4: the curve runs on the BASE 1D LUT's output. With a base that lifts the signal ~5 %
    around code 153 the patch codes land 0.3 of a slot off; the held-out greys are placed at the TRUE
    mid-positions, and the digest reports where the node patches really land."""
    from dlc.grayscale_wb import base_positions
    base = _lift_base()
    calib, result, judge, _panel = _run(tmp_path, "based", feature=None, base=base)
    touch = _touch(calib)
    ho = touch["held_out"]
    assert ho["base"]["available"] is True
    plain = {p[0] for p in build_grayscale_wb_heldout_set(PatchSizes(), _SDR)}
    assert set(ho["codes"]) != plain                                   # placed through the base, not code/cap
    nodes = [p[0] for p in build_grayscale_wb_set(PatchSizes(), _SDR)]
    for code in ho["codes"]:
        u = base_positions(code, max_cv=1023, cap=1023, n=32, kind="sdr", base=base)
        i = math.floor(u["g"])
        assert max(abs(v - (i + 0.5)) for v in u.values()) < 0.06, (code, u)
    pos = ho["node_positions"]
    assert pos["base_available"] and abs(pos["max_offset_slots"]) > 0.25 and pos["max_offset_node"] in (11, 12, 13)
    assert set(_verify(calib)["gswb_held_out"]["base"]) >= {"available", "source"}
    assert judge.request("verify:accept").recommendation == "apply"
    assert result.status == "completed"
    # with SDR GAMMA 2.4 baked the same codes would sit near the slots (u off the midpoints)
    near = [max(abs(v - (math.floor(v) + 0.5)) for v in base_positions(c, max_cv=1023, cap=1023, n=32, kind="sdr",
                                                                        base=base, gamma24=True).values())
            for c in ho["codes"]]
    assert max(near) > 0.25


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
