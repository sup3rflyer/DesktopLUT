"""SDR white-luminance band for the MHC grayscale refine + the ``refine-mhc`` flow.

The 2026-09-25 PA32UCXR SDR run: the matrix rowsums were [0.988, 1.006, 0.988] — green asked for
100.6 % drive at D65 — and the refine targeted D65 at the NATIVE full-drive luminance (121.03 nits),
so full white could never reach D65 (it read ~1 dE2000 off) while the greys converged; the band mean
also diluted that single level into "converged" after one round. Owner rule: SDR white may sit
anywhere in 110–120 nits; trade a few nits for an exact D65 white. Covers:

* the physics (``mhc_cube.sdr_white_reach`` / ``choose_sdr_white_nits``) and a closed-loop
  simulation proving the band lets white converge to D65 where the legacy target cannot;
* the convergence judge's top (white) anchor (``refine_convergence.analyse_round``);
* the orchestrator: band resolution, the ``below_band`` seam, HDR untouched;
* the ``refine-mhc`` flow end to end in the simulator.
"""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import calibration_profile as cp
from dlc import refine_convergence as rc
from dlc.calibrate import (
    AutoAdjudicator,
    Calibration,
    CalibrationAborted,
    Decision,
    PatchSizes,
    flow_patch_counts,
)
from dlc.colormath import invert3x3, matvec, rgb_to_xyz_matrix, xy_to_XYZ
from dlc.controller import CalibrationController
from dlc.engine.patches import Transfer
from dlc.events import Ev, read_events
from dlc.measure_loop import SyntheticPanel
from dlc.mhc_cube import (
    choose_sdr_white_nits,
    mhc2_matrix,
    refine_sdr_cube,
    sdr_white_reach,
)
from dlc.optimize import OptimizeConfig
from dlc.runs import create_run, open_run

_D65 = (0.3127, 0.3290)
_SRGB = {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}
# The 2026-09-25 PA32UCXR SDR run's measured native gamut/white/peak (dlc_state mhc_params).
_PA_PRIM = {"rx": 0.696042, "ry": 0.303958, "gx": 0.181786, "gy": 0.750021,
            "bx": 0.151213, "by": 0.064799}
_PA_WHITE = (0.313393, 0.326755)
_PA_PEAK = 121.0334


def _pa_rowsums() -> list[float]:
    return [sum(r) for r in mhc2_matrix(_PA_PRIM, _PA_WHITE, _SRGB, _D65)]


# ---------------------------------------------------------------------------
# physics: reach + band choice
# ---------------------------------------------------------------------------

def test_model_reach_is_peak_over_max_rowsum_and_names_green():
    rows = _pa_rowsums()
    assert rows[1] > 1.0 > rows[0]            # green is the channel asked past full drive
    r = sdr_white_reach(_PA_PRIM, _PA_WHITE, _PA_PEAK, rows)
    assert r["basis"] == "model"
    assert r["limiting_channel"] == "g"
    assert r["reach_nits"] == pytest.approx(_PA_PEAK / max(rows), rel=1e-4)
    assert r["reach_nits"] < _PA_PEAK         # native luminance is NOT reachable at D65


def _additive_panel(prim, white, peak, rowsums, gamma=2.2, gains=(1.0, 1.0, 1.0)):
    """An additive panel behind the SDR MHC2: the matrix lifts a wire neutral s to post-matrix
    linear ``rowsum_c * s**gamma`` (CLIPPED at full drive), the per-channel base LUT maps that
    index to a drive, and channel c emits ``gain_c * drive**gamma`` of its native full-drive XYZ."""
    disp = rgb_to_xyz_matrix(prim["rx"], prim["ry"], prim["gx"], prim["gy"], prim["bx"], prim["by"],
                             white[0], white[1], white_Y=peak)

    def lut(curve, x):
        n = len(curve)
        pos = min(max(x, 0.0), 1.0) * (n - 1)
        k = min(int(pos), n - 2)
        t = pos - k
        return curve[k] + (curve[k + 1] - curve[k]) * t

    def measure(curves, s):
        shares = []
        for c, ch in enumerate("rgb"):
            idx = min(1.0, max(rowsums[c], 0.0) * s ** gamma) ** (1.0 / gamma)
            shares.append(gains[c] * lut(curves[ch], idx) ** gamma)
        return tuple(sum(disp[row][c] * shares[c] for c in range(3)) for row in range(3))

    return measure


def _xy(xyz):
    t = sum(xyz)
    return xyz[0] / t, xyz[1] / t


def test_measured_reach_matches_model_on_an_ideal_panel_and_sees_a_weak_channel():
    rows = _pa_rowsums()
    ident = {ch: [j / 1023 for j in range(1024)] for ch in "rgb"}
    ideal = _additive_panel(_PA_PRIM, _PA_WHITE, _PA_PEAK, rows)
    r = sdr_white_reach(_PA_PRIM, _PA_WHITE, _PA_PEAK, rows, measured_top_xyz=ideal(ident, 1.0),
                        current_curves=ident)
    assert r["basis"] == "measured"
    assert r["reach_nits"] == pytest.approx(_PA_PEAK / max(rows), rel=1e-3)
    # A green channel delivering 2 % less light than the additive model: the reach drops with it.
    weak = _additive_panel(_PA_PRIM, _PA_WHITE, _PA_PEAK, rows, gains=(1.0, 0.98, 1.0))
    r2 = sdr_white_reach(_PA_PRIM, _PA_WHITE, _PA_PEAK, rows, measured_top_xyz=weak(ident, 1.0),
                         current_curves=ident)
    assert r2["limiting_channel"] == "g"
    assert r2["reach_nits"] == pytest.approx(0.98 * r["reach_nits"], rel=2e-3)


@pytest.mark.parametrize("reach,band,peak,nits,status", [
    (125.0, (110, 120), 126.0, 120.0, "in_band"),     # headroom: the band top
    (115.0, (110, 120), 121.0, 115.0, "in_band"),     # the minimum dimming for exact D65
    (105.0, (110, 120), 121.0, 110.0, "below_band"),  # exact D65 is below the band -> seam
    (95.0, (110, 120), 100.0, 100.0, "below_band"),   # even lo is above full drive: native peak
    (None, (110, 120), 121.0, 120.0, "unknown_reach"),
])
def test_choose_sdr_white_nits(reach, band, peak, nits, status):
    c = choose_sdr_white_nits(reach, band, peak)
    assert c["white_nits"] == pytest.approx(nits)
    assert c["status"] == status


def test_band_is_a_target_property_with_a_proportional_default():
    spec = cp.TargetSpec(name="srgb_g22_120", white_luminance_nits=120.0)
    assert spec.sdr_white_band == pytest.approx((110.0, 120.0))   # the owner's 2026-09-25 band
    assert spec.sdr_white_band_source == "default_fraction"
    assert cp.TargetSpec(name="x", white_luminance_nits=80.0).sdr_white_band == \
        pytest.approx((80.0 * 11 / 12, 80.0))
    explicit = cp._target_spec("t", {"white_luminance_nits": 120, "white_nits_band": [100, 118]})
    assert explicit.sdr_white_band == (100.0, 118.0)
    assert explicit.sdr_white_band_source == "profile"
    assert cp.parse_white_nits_band("110,120") == (110.0, 120.0)
    with pytest.raises(ValueError):
        cp.parse_white_nits_band([120, 110])
    with pytest.raises(ValueError):
        cp.parse_white_nits_band("abc")


def test_closed_loop_band_reaches_d65_where_the_native_luminance_target_cannot():
    # Closed loop on the additive panel behind the PA32UCXR's real matrix: measure the neutral
    # ramp -> refine_sdr_cube -> repeat. Targeting D65 at the NATIVE luminance (legacy) leaves
    # white off D65 forever (green is clipped at full drive); targeting the band's white (120,
    # just under the 120.26 reach) lands white on D65 at 120 nits.
    rows = _pa_rowsums()
    panel = _additive_panel(_PA_PRIM, _PA_WHITE, _PA_PEAK, rows)
    levels = [0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 1.0]

    def converge(target_white_nits):
        curves = {ch: [j / 1023 for j in range(1024)] for ch in "rgb"}
        for _ in range(12):
            meas = [(s, panel(curves, s)) for s in levels]
            curves = refine_sdr_cube(curves, meas, _PA_PRIM, _PA_WHITE, _PA_PEAK, rows,
                                     target_white_nits=target_white_nits, dark_floor_nits=0.1)
        return panel(curves, 1.0), panel(curves, 0.9)

    white_legacy, _ = converge(None)
    wx, wy = _xy(white_legacy)
    assert math.hypot(wx - _D65[0], wy - _D65[1]) > 5e-4       # stuck off D65 (green-starved)

    choice = choose_sdr_white_nits(sdr_white_reach(_PA_PRIM, _PA_WHITE, _PA_PEAK, rows)["reach_nits"],
                                   (110.0, 120.0), _PA_PEAK)
    assert choice["white_nits"] == pytest.approx(120.0)
    white, grey90 = converge(choice["white_nits"])
    wx, wy = _xy(white)
    assert math.hypot(wx - _D65[0], wy - _D65[1]) < 1e-4       # exact D65 white
    assert white[1] == pytest.approx(120.0, rel=2e-3)            # at the band's white
    assert grey90[1] == pytest.approx(120.0 * 0.9 ** 2.2, rel=3e-3)   # the curve follows the new white


# ---------------------------------------------------------------------------
# convergence judge: the top (white) anchor
# ---------------------------------------------------------------------------

def _xy_de(m, t) -> float:
    (mx, my), (tx, ty) = _xy(m), _xy(t)
    return 1000.0 * math.hypot(mx - tx, my - ty)      # 0.001 xy == 1.0 "dE"


def _levels(top_off: float) -> list[rc.GreyLevel]:
    out = []
    for i in range(1, 21):
        s = i / 20.0
        Y = 120.0 * s ** 2.2
        t = tuple(xy_to_XYZ(_D65[0], _D65[1], Y))
        m = t if s < 1.0 else tuple(xy_to_XYZ(_D65[0] - top_off, _D65[1] - top_off, Y))
        out.append(rc.GreyLevel(signal=s, measured_xyz=m, target_xyz=t, meter_se_xy=1e-5))
    return out


def test_band_mean_dilutes_white_but_the_top_anchor_continues():
    lv = _levels(top_off=0.0008)          # white ~1.1 "dE" off; 19 greys perfect
    mean_only = rc.analyse_round(lv, de_fn=_xy_de, floor=rc.PanelFloor())
    assert mean_only["decision"] == "converged"           # the 2026-09-25 failure mode
    anchored = rc.analyse_round(lv, de_fn=_xy_de, floor=rc.PanelFloor(), top_anchor=True)
    assert anchored["decision"] == "continue"
    assert anchored["top"]["signal"] == 1.0
    assert anchored["top"]["predicted_gain"] >= rc.MATERIAL_GAIN_JND
    assert "top level" in anchored["reason"]


def test_top_anchor_floors_when_a_step_did_not_move_white():
    lv = _levels(top_off=0.0008)
    first = rc.analyse_round(lv, de_fn=_xy_de, floor=rc.PanelFloor(), top_anchor=True)
    # The next round reads white exactly as before (a channel pinned at full drive): floored.
    again = rc.analyse_round(lv, de_fn=_xy_de, floor=rc.PanelFloor(), previous=first,
                             top_anchor=True)
    assert again["decision"] == "floored"
    assert again["top"]["realized_gain"] == pytest.approx(0.0, abs=1e-6)
    # ...and once white is on target, the anchor lets the judge converge.
    done = rc.analyse_round(_levels(top_off=0.0), de_fn=_xy_de, floor=rc.PanelFloor(),
                            previous=first, top_anchor=True)
    assert done["decision"] == "converged"


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

_DATE = datetime.date(2026, 9, 25)
_SMALL = PatchSizes(raw_ramp_steps=9, cube_size=3, tube_size=5, tube_radius=1, neutral_steps=9)
_OPT = OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0)


class _Recording(AutoAdjudicator):
    def __init__(self, decisions=None):
        super().__init__()
        self.requests = []
        self._decisions = decisions or {}

    def adjudicate(self, request):  # noqa: D401
        self.requests.append(request)
        if request.key in self._decisions:
            return Decision(self._decisions[request.key], note="test")
        return super().adjudicate(request)


def _perfect_panel(nits: float = 120.0) -> SyntheticPanel:
    return SyntheticPanel(transfer=Transfer.power(gamma=2.2, peak_nits=nits, bit_depth=10),
                          start_temp=1.0, cold_blue_gain=1.0)


def _make(tmp_path: Path, name: str, *, controller=None, adjudicator=None, white_band=None,
          source_run=None, require_hardware_readiness=False, mode="SDR", panel=None,
          bit_depth=None) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run(mode, display="synthetic", run_dir=run_dir)
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    return Calibration(
        ctx=ctx, profile=profile, monitor=0, mode=mode,
        controller=controller or CalibrationController.mock(),
        measure=panel if panel is not None else _perfect_panel(),
        adjudicator=adjudicator or AutoAdjudicator(), optimize_config=_OPT, patch_sizes=_SMALL,
        run_date=_DATE, white_band=white_band, source_run=source_run, bit_depth=bit_depth,
        require_hardware_readiness=require_hardware_readiness)


def test_sdr_refine_digest_carries_the_band_and_judges_white_at_its_luminance(tmp_path, monkeypatch):
    calib = _make(tmp_path, "band_default")
    assert calib.run("mhc-only").status == "completed"
    wb = calib.calib["stages"]["refine-mhc-grayscale"]["digest"]["white_band"]
    assert wb["band"] == [110.0, 120.0] and wb["band_source"] == "default_fraction"
    assert wb["status"] == "in_band"
    assert calib._state["mhc_params"]["sdr_white"]["white_nits"] == wb["white_nits"]

    # An explicit run override lands in the judge (top_nits) AND the refine step (target white).
    calib2 = _make(tmp_path, "band_override", white_band=(100.0, 110.0))
    calib2.run("mhc-only")
    assert calib2.calib["white_band_override"] == [100.0, 110.0]
    calib2.calib["stages"].pop("refine-mhc-grayscale", None)
    seen = {}
    orig = calib2._refine_round_analysis

    def spy(*a, **k):
        seen.setdefault("top_nits", k.get("top_nits"))
        seen.setdefault("top_anchor", k.get("top_anchor"))
        out = orig(*a, **k)
        out = dict(out, decision="continue") if "step" not in seen else out
        return out

    import dlc.mhc_cube as mc
    orig_refine = mc.refine_sdr_cube

    def spy_refine(*a, **k):
        seen["step"] = k.get("target_white_nits")
        return orig_refine(*a, **k)

    monkeypatch.setattr(calib2, "_refine_round_analysis", spy)
    monkeypatch.setattr(mc, "refine_sdr_cube", spy_refine)
    out = calib2.stage_refine_mhc_grayscale()
    assert seen["top_nits"] == pytest.approx(110.0)
    assert seen["top_anchor"] is True
    assert seen["step"] == pytest.approx(110.0)
    assert out.digest["white_band"]["band_source"] == "run_override"


def test_exact_white_below_the_band_raises_the_white_band_seam(tmp_path, monkeypatch):
    adj = _Recording()
    calib = _make(tmp_path, "band_below", adjudicator=adj)
    calib.run("mhc-only")
    calib.calib["stages"].pop("refine-mhc-grayscale", None)
    # The perfect 120-nit panel can't make a 125-130 nit white: exact D65 is BELOW the band.
    monkeypatch.setattr(calib, "_sdr_white_band", lambda: ((125.0, 130.0), "test"))
    before = len(adj.requests)
    out = calib.stage_refine_mhc_grayscale()
    wb = out.digest["white_band"]
    assert wb["status"] == "below_band"
    assert wb["white_nits"] == pytest.approx(wb["native_peak_nits"])   # lo is above full drive
    assert out.digest.get("white_band_below") is True
    seams = [r for r in adj.requests[before:] if r.key == "refine-mhc-grayscale:white-band"]
    assert len(seams) == 1 and seams[0].options == ("accept", "abort")
    assert "BELOW the SDR white band" in seams[0].question

    # An 'abort' verdict ends the flow (a judgment, not a note).
    adj2 = _Recording({"refine-mhc-grayscale:white-band": "abort"})
    calib2 = _make(tmp_path, "band_below_abort", adjudicator=adj2)
    calib2.run("mhc-only")
    calib2.calib["stages"].pop("refine-mhc-grayscale", None)
    monkeypatch.setattr(calib2, "_sdr_white_band", lambda: ((125.0, 130.0), "test"))
    with pytest.raises(CalibrationAborted):
        calib2.stage_refine_mhc_grayscale()


def test_hdr_refine_judge_keeps_the_mean_only_judgment(tmp_path, monkeypatch):
    calls = []
    orig = rc.analyse_round

    def spy(*a, **k):
        calls.append(k.get("top_anchor", False))
        return orig(*a, **k)

    monkeypatch.setattr(rc, "analyse_round", spy)
    panel = SyntheticPanel(transfer=Transfer.pq(bit_depth=10), start_temp=1.0,
                           cold_blue_gain=1.0, native_white_nits=1840.0)
    calib = _make(tmp_path, "hdr_mean_only", mode="HDR", panel=panel, bit_depth=10)
    assert calib.run("mhc-only").status == "completed"
    assert calls and not any(calls)
    assert "white_band" not in calib.calib["stages"]["refine-mhc-cube"]["digest"]


# ---------------------------------------------------------------------------
# the refine-mhc flow
# ---------------------------------------------------------------------------

def _announced_phases(ctx) -> list[str]:
    return [e.data.get("phase_name") for e in read_events(ctx.events_path) if e.event == Ev.PHASE]


def test_refine_mhc_flow_rerefines_and_keeps_the_source_cube(tmp_path, monkeypatch):
    src = _make(tmp_path, "src_full")
    assert src.run("full").status == "completed"
    src_state_before = (src.ctx.root / "dlc_state.json").read_text(encoding="utf-8")
    controller = src.controller
    installs: list[str] = []
    orig_set = controller.set_3dlut
    monkeypatch.setattr(controller, "set_3dlut",
                        lambda mon, mode, path: (installs.append(str(path)),
                                                 orig_set(mon, mode, path))[1])

    calib = _make(tmp_path, "refine_only", controller=controller, source_run=src.ctx.root,
                  require_hardware_readiness=True)
    result = calib.run("refine-mhc")
    assert result.status == "completed", result.digest
    assert result.stages == [
        "preflight", "whitepoint", "seed-from-run", "enter-neutral", "hardware-readiness",
        "install-mhc", "refine-mhc-grayscale", "reapply-3dlut", "measure:verify", "verify"]
    # The dashboard stepper mirrors the flow exactly.
    assert _announced_phases(calib.ctx) == [s["key"] for s in calib._planned_stages()]

    seed = calib.calib["stages"]["seed-from-run"]
    assert seed["data"]["source_run"] == str(src.ctx.root.resolve())
    params = calib._state["mhc_params"]
    assert params["seeded_from"]["run"] == str(src.ctx.root.resolve())
    # The refine restarted from the BUILD base cube copied into THIS run, never the source's file.
    assert Path(params["seeded_from"]["base_cube"]).name == "mhc_base_sdr.cube"
    assert str(calib.ctx.root) in calib.calib["stages"]["install-mhc"]["digest"]["base_cube"]
    assert "white_band" in calib.calib["stages"]["refine-mhc-grayscale"]["digest"]
    # The kept cube was re-applied over the new foundation (and is the deliverable on apply).
    kept = calib.calib["stages"]["reapply-3dlut"]["data"]["cube_path"]
    assert kept and Path(kept).exists()
    assert installs and installs[0] == kept
    # The verify was the SHORT one (greys + RGBCMY sanity), not the full QC sweep.
    n_verify = flow_patch_counts("refine-mhc", _SMALL, calib._transfer())["total_patches"]
    assert calib.calib["stages"]["verify"]["digest"]["patch_count"] == n_verify
    # The source run was only read.
    assert (src.ctx.root / "dlc_state.json").read_text(encoding="utf-8") == src_state_before
    report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert report["flow"] == "refine-mhc" and report["mhc_refine"]


def test_refine_mhc_refuses_without_a_matching_source(tmp_path):
    calib = _make(tmp_path, "no_source")
    res = calib.run("refine-mhc")
    assert res.status == "aborted" and "source-run" in res.digest["message"]

    # A source that is not an SDR MHC run of this display/target: clean refusal, nothing installed.
    bogus = tmp_path / "bogus"
    bogus.mkdir()
    (bogus / "dlc_state.json").write_text(json.dumps({"mode": "HDR", "monitor": 0, "calib": {}}),
                                          encoding="utf-8")
    calib2 = _make(tmp_path, "bad_source", source_run=bogus)
    res2 = calib2.run("refine-mhc")
    assert res2.status == "aborted"
    assert res2.digest["aborted_at"] == "seed-from-run"
    assert "enter-neutral" not in calib2.calib["stages"]


def test_refine_mhc_is_sdr_only(tmp_path):
    panel = SyntheticPanel(transfer=Transfer.pq(bit_depth=10), start_temp=1.0,
                           cold_blue_gain=1.0, native_white_nits=1840.0)
    calib = _make(tmp_path, "hdr_refine_only", mode="HDR", panel=panel, bit_depth=10,
                  source_run=tmp_path)
    res = calib.run("refine-mhc")
    assert res.status == "aborted" and "SDR-only" in res.digest["message"]
