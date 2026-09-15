"""FALD profiling fit rules (hardening 2026-09-15): the Stage-A drive-curve seeding + tmin/k consistency loop, the
knots gate, the drive-floor grid, the fade report, the SDR export fade/estimate seams, and the synthetic SDR truth."""
from __future__ import annotations

import json
import math
from argparse import Namespace

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald import profile as P  # noqa: E402
from dlc.fald.model import FaldParams  # noqa: E402
from dlc.runs import create_run  # noqa: E402
from dlc.stages import _common, fald_profile  # noqa: E402


def _geo(**kw):
    base = dict(width=1920, height=1080, cols=24, rows=24, diagonal_in=16.0, meter=(975, 555), white_nits=1000.0)
    base.update(kw)
    return P.PanelGeometry.from_diagonal(**base)


def _sdr_geo(**kw):
    return _geo(transfer="gamma", bit_depth=8, white_nits=120.0, sdr_gamma=2.2, **kw)


# ----------------------------------------------------------------------------- Stage-A seeding + consistency loop
def _stub_stages(monkeypatch, *, k_seq, tmin_seq, a0_stage_a=3000.0, a0_stage_b=500.0):
    """Stage stubs recording what each call saw; Stage B returns the next k of ``k_seq``, Stage A the next tmin."""
    calls: list[dict] = []

    def fake_a(ev, items, *, quick=False, log=print, x0=None, active=P.STAGE_A_PARAMS):
        i = sum(1 for c in calls if c["stage"] == "A")
        calls.append({"stage": "A", "curve": [tuple(x) for x in ev.params.drive_curve], "x0": dict(x0) if x0 else None,
                      "active": tuple(active), "a0": ev.params.stat_area0_px2})
        vals = dict(x0) if x0 else {**P.STAGE_A_X0, "stat_area0_px2": a0_stage_a}
        vals["tmin"] = tmin_seq[i]
        ev.set(**vals)
        return {**vals, "rms": 0.01, "frozen": [n for n in P.STAGE_A_PARAMS if n not in active]}

    def fake_b(ev, items, *, quick=False, log=print, fit_drive_k=False, fit_area0=True, warm=False, k_start=P.DRIVE_K0):
        i = sum(1 for c in calls if c["stage"] == "B")
        calls.append({"stage": "B", "warm": warm, "k_start": k_start, "fit_drive_k": fit_drive_k, "fit_area0": fit_area0,
                      "curve": [tuple(x) for x in ev.params.drive_curve], "a0": ev.params.stat_area0_px2})
        kw = {"est_scale_mm": 10.0, "drive_dim": 0.1, "est_phase_px": -20.0, "est_phase_py": 0.0, "est_aniso": 1.0,
              "stat_area0_px2": a0_stage_b if fit_area0 else ev.params.stat_area0_px2}
        out = dict(kw)
        if fit_drive_k:
            kw["drive_curve"] = P.power_drive_curve(ev.params.white_nits, k_seq[i])
            out["drive_k"] = k_seq[i]
        ev.set(est_kind="exp", **kw)
        return {**out, "rms": 0.005}

    monkeypatch.setattr(P, "fit_stage_a", fake_a)
    monkeypatch.setattr(P, "fit_stage_b", fake_b)
    monkeypatch.setattr(P, "group_report", lambda ev, items: {})
    monkeypatch.setattr(P, "level_report", lambda ev, items: [])
    return calls


_STUB_ITEMS = [{"group": "peak", "name": "DRV:peak40", "base": None, "y": 50.0, "w": 1.0},
               {"group": "hole", "name": "DRV:hole300", "base": None, "y": 0.2, "w": 1.0},
               {"group": "rings", "name": "RING10:R240", "base": [((40, 40, 40), P.FULL)], "y": 1.02, "w": 1.0}]


def test_sdr_stage_a_starts_on_the_power_law_and_refits_tmin_under_stage_b_k(monkeypatch):
    g = _sdr_geo()
    white = g.white_nits
    # round 1: tmin moves 26 % -> warm Stage B; round 2: tmin moves 1.5 % -> converged, no second warm Stage B
    calls = _stub_stages(monkeypatch, k_seq=[0.40, 0.41], tmin_seq=[1.0e-3, 1.3e-3, 1.32e-3])
    res = P.run_fit(g.base_params(), list(_STUB_ITEMS), knots="never", fit_drive_k=True, log=lambda *a: None)
    assert [c["stage"] for c in calls] == ["A", "B", "A", "B", "A"]
    a1, b1, a2, b2, a3 = calls
    assert a1["curve"] == P.power_drive_curve(white, 0.55) and a1["x0"] is None and a1["active"] == P.STAGE_A_PARAMS
    assert b1["warm"] is False and b1["k_start"] == 0.55 and b1["fit_drive_k"]
    assert a2["curve"] == P.power_drive_curve(white, 0.40) and a2["active"] == ("tmin",)
    assert a2["a0"] == 3000.0 and a2["x0"]["stat_area0_px2"] == 3000.0          # Stage A's OWN A0, not Stage B's 500
    assert b2["warm"] is True and b2["k_start"] == 0.40 and b2["a0"] == 500.0    # Stage B's A0 restored
    assert a3["curve"] == P.power_drive_curve(white, 0.41) and a3["active"] == ("tmin",)
    cons = res["drive_k_consistency"]
    assert cons["converged"] and cons["rounds"] == 2 and cons["k_start"] == 0.55 and cons["k_final"] == 0.41
    assert cons["tmin_rounds"] == [1.0e-3, 1.3e-3, 1.32e-3]
    assert res["stage_a"]["tmin"] == 1.32e-3
    assert res["area0_stage_a"] == 3000.0 and res["area0_stage_b"] == 500.0
    assert res["params"]["stat_area0_px2"] == 500.0 and res["params"]["tmin"] == 1.32e-3
    assert [tuple(x) for x in res["params"]["drive_curve"]] == P.power_drive_curve(white, 0.41)


def test_sdr_no_warm_stage_b_when_tmin_does_not_move(monkeypatch):
    g = _sdr_geo()
    calls = _stub_stages(monkeypatch, k_seq=[0.40], tmin_seq=[1.0e-3, 1.04e-3])      # 3.9 % < 5 %
    res = P.run_fit(g.base_params(), list(_STUB_ITEMS), knots="never", fit_drive_k=True, log=lambda *a: None)
    assert [c["stage"] for c in calls] == ["A", "B", "A"]
    cons = res["drive_k_consistency"]
    assert cons["converged"] and cons["rounds"] == 1 and "not re-run" in cons["reason"]


def test_sdr_consistency_loop_is_bounded_and_reports_non_convergence(monkeypatch):
    g = _sdr_geo()
    calls = _stub_stages(monkeypatch, k_seq=[0.40, 0.60, 0.40, 0.60], tmin_seq=[1e-3, 2e-3, 1e-3, 2e-3])
    res = P.run_fit(g.base_params(), list(_STUB_ITEMS), knots="never", fit_drive_k=True, log=lambda *a: None)
    assert [c["stage"] for c in calls] == ["A", "B"] + ["A", "B"] * P.MAX_ROUNDS
    cons = res["drive_k_consistency"]
    assert cons["converged"] is False and cons["rounds"] == P.MAX_ROUNDS and "not converged" in cons["reason"]


def test_sdr_k0_multistart_seeds_both_stages(monkeypatch):
    g = _sdr_geo()
    calls = _stub_stages(monkeypatch, k_seq=[0.705], tmin_seq=[1e-3])                  # already consistent with k0
    res = P.run_fit(g.base_params(), list(_STUB_ITEMS), knots="never", fit_drive_k=True, k0=0.70, log=lambda *a: None)
    assert [c["stage"] for c in calls] == ["A", "B"]
    assert calls[0]["curve"] == P.power_drive_curve(g.white_nits, 0.70) and calls[1]["k_start"] == 0.70
    assert res["drive_k_consistency"]["converged"] and res["drive_k_consistency"]["rounds"] == 0


def test_hdr_path_is_one_stage_a_and_one_stage_b_with_the_measured_curve(monkeypatch):
    g = _geo()
    measured = [(18.4, 0.078), (55.2, 0.136), (184.8, 0.265), (551.6, 0.493), (1102.5, 0.740), (1000.0 * 1.85, 1.0)]
    base = g.base_params(drive_curve=list(measured))
    calls = _stub_stages(monkeypatch, k_seq=[], tmin_seq=[1e-3])
    res = P.run_fit(base, list(_STUB_ITEMS), knots="never", fit_drive_k=False, k0=0.40, log=lambda *a: None)
    assert [c["stage"] for c in calls] == ["A", "B"]
    a, b = calls
    assert a["x0"] is None and a["active"] == P.STAGE_A_PARAMS and a["curve"] == measured
    assert b["warm"] is False and b["fit_drive_k"] is False and b["curve"] == measured
    assert res["drive_k_consistency"] is None
    assert [tuple(x) for x in res["params"]["drive_curve"]] == measured


# ----------------------------------------------------------------------------- Stage A / B parameter handling
def _cheap_residuals(monkeypatch, target_tmin=2e-3):
    def res(ev, items):
        return np.array([math.log(ev.params.tmin) - math.log(target_tmin)] * len(items)) + 1e-3 * ev.params.kernel_pnorm
    monkeypatch.setattr(P, "residuals", res)


def test_stage_a_freezes_pnorm_with_too_few_items(monkeypatch):
    _cheap_residuals(monkeypatch)
    g = _sdr_geo()
    few = [{"group": "peak"}] * 8                                                 # 8 < 7 params + 3
    out = P.fit_stage_a(P.Evaluator(g.base_params()), few, quick=True, log=lambda *a: None)
    assert out["frozen"] == ["kernel_pnorm"] and out["kernel_pnorm"] == P.STAGE_A_X0["kernel_pnorm"] and "frozen_reason" in out
    enough = [{"group": "peak"}] * 10
    out = P.fit_stage_a(P.Evaluator(g.base_params()), enough, quick=True, log=lambda *a: None)
    assert out["frozen"] == [] and "frozen_reason" not in out


def test_stage_a_tmin_only_warm_start_holds_the_rest_and_clips_inside_bounds(monkeypatch):
    _cheap_residuals(monkeypatch, target_tmin=2e-3)
    g = _sdr_geo()
    ev = P.Evaluator(g.base_params())
    x0 = {"core_mm": 6.1, "tail_mm": 58.0, "tail_frac": 0.41, "tmin": 0.5, "aperture_px": 52.0, "kernel_pnorm": 2.5,
          "stat_area0_px2": 3162.0}                                                # tmin and p-norm sit at/over the bounds
    out = P.fit_stage_a(ev, [{"group": "peak"}] * 8, x0=x0, active=("tmin",), log=lambda *a: None)
    assert set(out["frozen"]) == set(P.STAGE_A_PARAMS) - {"tmin"}
    for n in P.STAGE_A_PARAMS:
        if n != "tmin":
            assert out[n] == pytest.approx(x0[n]) and getattr(ev.params, n) == pytest.approx(min(x0[n], 0.95) if n == "tail_frac" else x0[n])
    assert P.STAGE_A_LO["tmin"] <= out["tmin"] <= P.STAGE_A_HI["tmin"]
    assert abs(math.log(out["tmin"] / 2e-3)) < 0.05


def test_stage_b_warm_skips_the_phase_grid_and_starts_at_k_start(monkeypatch):
    monkeypatch.setattr(P, "residuals", lambda ev, items: np.array([0.01, -0.02]))
    g = _sdr_geo()
    ev = P.Evaluator(g.base_params(est_scale_mm=9.0, drive_dim=0.0, est_phase_px=-26.0, est_phase_py=-21.0, est_aniso=0.99))
    lines: list[str] = []
    out = P.fit_stage_b(ev, [{}, {}], quick=True, log=lines.append, fit_drive_k=True, warm=True, k_start=0.39)
    assert lines and not any("B[ph=" in l for l in lines)                          # no coarse grid
    assert "phase=(-26.0,-21.0)" in lines[0] and "k=0.390" in lines[0]
    assert out["drive_dim"] >= 1e-3                                                # drive_dim 0 clipped inside the bounds
    lines.clear()
    P.fit_stage_b(P.Evaluator(g.base_params()), [{}, {}], quick=True, log=lines.append, fit_drive_k=True)
    assert any("B[ph=" in l for l in lines)


# ----------------------------------------------------------------------------- knots gate on the recorded fits
# (group: n, exp mean |err| pp, knots mean |err| pp, measured ring magnitude pp) — hardenB gate_eval tables 2026-09-15
GATE_HDR = {"comp": (6, 1.7260, 1.3337, 8.9414), "orange": (2, 1.3203, 0.7324, 3.9785), "rings@diag": (4, 0.4510, 1.0404, 9.0847),
            "rings@fine": (18, 1.9383, 1.3338, 9.6721), "rings@held": (12, 1.8665, 1.2799, 7.8227),
            "superpose": (2, 0.4850, 2.3899, 15.1007)}
GATE_SDR_R1 = {"comp": (6, 0.4677, 0.5124, 2.3028), "orange": (2, 0.4629, 0.4167, 1.1497), "rings@diag": (4, 0.0697, 0.0422, 2.0352),
               "rings@fine": (18, 0.4740, 0.1657, 3.6524), "rings@held": (12, 0.3997, 0.3726, 2.3586),
               "superpose": (2, 2.0592, 1.8718, 3.7310)}
GATE_SDR_AUG_OLD = {"comp": (6, 0.8562, 0.7873, 2.3028), "halo": (9, 0.4641, 0.4045, 4.6269), "halo@held": (2, 1.6456, 1.8336, 5.1241),
                    "orange": (2, 0.6220, 0.5617, 1.1497), "rings@diag": (4, 0.5181, 0.4976, 2.0352),
                    "rings@fine": (18, 0.8318, 0.6678, 3.6524), "rings@held": (12, 0.7106, 0.5973, 2.3586),
                    "rings@lowheld": (6, 14.8539, 15.4382, 20.4433), "superpose": (2, 2.2217, 2.0226, 3.7310)}
GATE_SDR_AUG_FIXED = {"comp": (6, 0.8531, 0.8009, 2.3028), "halo@held": (2, 0.4008, 0.3341, 5.1241), "orange": (2, 0.6188, 0.4447, 1.1497),
                      "rings@diag": (4, 0.4390, 0.4286, 2.0352), "rings@held": (12, 0.7430, 0.7235, 2.3586),
                      "rings@lowheld": (6, 7.6280, 7.4908, 20.4433), "superpose": (2, 1.0391, 1.0846, 3.7310),
                      "rings@fine": (18, 1.1978, 1.2589, 3.6524), "halo": (9, 0.6641, 0.7145, 4.6269)}


def _gate(table):
    exp = {g: v[1] for g, v in table.items()}
    kn = {g: v[2] for g, v in table.items()}
    mag = {g: v[3] for g, v in table.items()}
    n = {g: v[0] for g, v in table.items()}
    return P.knots_gate(exp, kn, mag, n)


def test_knots_gate_on_the_recorded_hdr_fit_keeps_knots():
    keep, t = _gate(GATE_HDR)
    assert keep, t
    assert t["near_gain_pp"] == pytest.approx(0.6045, abs=1e-3) and not t["near_worse"]
    assert t["unfit"] == [] and t["veto"] == []                   # rings@diag +0.59 < 1.0 pp; superpose n = 2 cannot veto
    assert t["net_pp"] == pytest.approx(-0.169, abs=2e-3)


def test_knots_gate_on_the_recorded_sdr_run1_fit_keeps_knots():
    keep, t = _gate(GATE_SDR_R1)
    assert keep and t["unfit"] == ["superpose"] and t["veto"] == []
    assert t["net_pp"] == pytest.approx(-0.0108, abs=2e-3)


def test_knots_gate_on_the_recorded_sdr_augmented_fit_keeps_knots():
    keep, t = _gate(GATE_SDR_AUG_OLD)
    assert keep, t
    assert set(t["unfit"]) == {"orange", "rings@lowheld", "superpose"} and t["veto"] == []
    assert t["near_gain_pp"] == pytest.approx(0.2236, abs=1e-3)


def test_knots_gate_on_the_stage_a_fixed_augmented_fit_rejects_knots():
    keep, t = _gate(GATE_SDR_AUG_FIXED)
    assert not keep
    assert t["near_gain_pp"] < 0 and any("near-field gain" in r for r in t["reasons"])


def _synthetic_gate(**over):
    base = {"rings@fine": (18, 1.00, 0.50, 4.0), "rings@held": (12, 0.60, 0.60, 3.0), "comp": (6, 0.80, 0.80, 3.0),
            "rings@diag": (4, 0.40, 0.40, 2.0)}
    base.update(over)
    return base


def test_knots_gate_adversarial_veto_needs_n3_and_a_large_relative_worsening():
    # n >= 3, exp 0.40, worse by 1.1 pp > max(1.0, 0.2) -> veto (net stays small: 12 + 6 unchanged groups dilute it)
    keep, t = _gate(_synthetic_gate(**{"rings@diag": (4, 0.40, 1.50, 2.0)}))
    assert not keep and t["veto"] == ["rings@diag"]
    # the same worsening in a group of 2 cannot veto, and the net stays under +0.1 pp with enough improving groups
    keep, t = _gate(_synthetic_gate(**{"rings@diag": (2, 0.40, 1.50, 2.0), "rings@held": (12, 0.60, 0.40, 3.0)}))
    assert keep and t["veto"] == [], t
    # worse by 1.5 pp on a group whose exp error is 4.0 (limit max(1.0, 2.0) = 2.0): no veto — but not unfit either
    keep, t = _gate(_synthetic_gate(**{"comp": (6, 4.0, 5.5, 12.0), "rings@held": (12, 0.60, 0.10, 3.0)}))
    assert t["veto"] == [] and "comp" not in t["unfit"]


def test_knots_gate_adversarial_unfit_groups_cannot_veto_and_leave_the_net():
    # exp 3.0 >= 0.5 x ring 5.0: unfit — a +5 pp worsening is listed, not a veto, not in the net
    keep, t = _gate(_synthetic_gate(**{"comp": (6, 3.0, 8.0, 5.0)}))
    assert keep and t["unfit"] == ["comp"] and t["veto"] == [] and t["groups"]["comp"]["unfit"]
    assert t["net_pp"] == pytest.approx(0.0)


def test_knots_gate_adversarial_net_and_near_field_rules():
    # every describable held-out group 0.3 pp worse: no veto, but the net +0.3 pp > +0.1 pp
    keep, t = _gate(_synthetic_gate(**{"rings@held": (12, 0.6, 0.9, 3.0), "comp": (6, 0.8, 1.1, 3.0), "rings@diag": (4, 0.4, 0.7, 2.0)}))
    assert not keep and t["veto"] == [] and t["net_pp"] == pytest.approx(0.3)
    # near-field gain exactly 0.2 pp: not enough (strictly greater)
    keep, _ = _gate(_synthetic_gate(**{"rings@fine": (18, 1.0, 0.8, 4.0)}))
    assert not keep
    # one near group gains 0.6 but the other is 0.25 worse
    keep, t = _gate(_synthetic_gate(**{"rings@fine": (18, 1.0, 0.4, 4.0), "halo": (9, 0.5, 0.75, 4.0)}))
    assert not keep and t["near_worse"] == ["halo"]
    # no near-field group at all
    keep, t = _gate({"rings@held": (12, 0.6, 0.1, 3.0)})
    assert not keep and any("no near-field" in r for r in t["reasons"])


def test_knots_gate_accepts_group_report_rows():
    exp = {"rings@fine": {"n": 18, "mean_abs": 1.0, "rows": []}, "rings@held": {"n": 12, "mean_abs": 0.5, "rows": []}}
    kn = {"rings@fine": {"n": 18, "mean_abs": 0.5, "rows": []}, "rings@held": {"n": 12, "mean_abs": 0.5, "rows": []}}
    keep, t = P.knots_gate(exp, kn, {"rings@fine": 3.0, "rings@held": 2.0}, {})
    assert keep and t["groups"]["rings@held"]["n"] == 12


def test_ring_magnitudes_are_mean_abs_ratio_rings_in_pp():
    items = [{"group": "halo", "base": [((1, 1, 1), P.FULL)], "y": 1.04}, {"group": "halo", "base": [((1, 1, 1), P.FULL)], "y": 0.98},
             {"group": "peak", "base": None, "y": 40.0}]
    assert P.ring_magnitudes(items) == {"halo": pytest.approx(3.0)}


# ----------------------------------------------------------------------------- drive floor grid
class _FloorEv:
    """Evaluator stand-in: predictions a function of the drive floor; records every floor evaluated."""

    def __init__(self, params, pred):
        self.params, self.pred, self.seen = params, pred, []

    def set(self, **kw):
        from dataclasses import replace
        self.params = replace(self.params, **kw)
        if "drive_floor_nits" in kw:
            self.seen.append(kw["drive_floor_nits"])

    def predict(self, item):
        return self.pred(self.params.drive_floor_nits, item)


def _dim_item(g, nits, level):
    c = g.code(nits)
    return {"group": "rings@low", "name": f"LOW{level:g}", "base": [((c, c, c), P.FULL)], "shapes": [], "y": 1.10, "w": 1.0,
            "meter": g.meter, "level": level}


def _pa_sdr_geo():
    """The PA32UCXR SDR run's measured white / gamma: the nominal 0.5 / 1 / 2-nit greys render 0.517 / 1.018 / 2.029."""
    return _geo(transfer="gamma", bit_depth=8, white_nits=121.89873, sdr_gamma=2.2709158)


def test_drive_floor_candidates_stay_strictly_below_the_dimmest_rendered_field():
    g = _pa_sdr_geo()
    prm = g.base_params()
    items = [_dim_item(g, 0.5, 0.5), _dim_item(g, 1.0, 1.0), _dim_item(g, 2.0, 2.0)]
    dimmest = min(P.field_nits(prm, it) for it in items)
    assert 0.5 < dimmest < 0.55                                               # the 0.5-nit grey renders one code higher
    ev = _FloorEv(prm, lambda fl, it: 1.0)                                    # floor-insensitive: unidentifiable
    out = P.drive_floor_grid(ev, items, log=lambda *a: None)
    assert ev.seen[:-1] == [0.05, 0.15, 0.3, 0.5] and all(f < dimmest for f in ev.seen)   # 1.0 would switch 0.517 off
    assert out["chosen_nits"] == 0.5 and out["identified"] is False and ev.params.drive_floor_nits == 0.5
    assert out["dimmest_field_nits"] == pytest.approx(dimmest)


def test_drive_floor_uses_the_rendered_level_not_the_nominal_one():
    g = _sdr_geo()                                                            # 120 nits, gamma 2.2: "0.5" renders 0.494
    items = [_dim_item(g, 0.5, 0.5), _dim_item(g, 1.0, 1.0)]
    ev = _FloorEv(g.base_params(), lambda fl, it: 1.0)
    out = P.drive_floor_grid(ev, items, log=lambda *a: None)
    assert [t["floor_nits"] for t in out["table"]] == [0.05, 0.15, 0.3] and out["chosen_nits"] == 0.3


def test_drive_floor_identified_when_the_rms_separates():
    g = _pa_sdr_geo()
    items = [_dim_item(g, 0.5, 0.5), _dim_item(g, 2.0, 2.0)]
    ev = _FloorEv(g.base_params(), lambda fl, it: 1.10 * math.exp(abs(fl - 0.15)))   # best at 0.15, the others > 1 % off
    out = P.drive_floor_grid(ev, items, log=lambda *a: None)
    assert [t["floor_nits"] for t in out["table"]] == [0.05, 0.15, 0.3, 0.5]
    assert out["chosen_nits"] == 0.15 and out["identified"] is True


def test_drive_floor_with_no_candidate_below_the_data_never_turns_a_field_off():
    g = _sdr_geo()
    items = [_dim_item(g, 0.03, 0.03)]
    dimmest = P.field_nits(g.base_params(), items[0])
    ev = _FloorEv(g.base_params(), lambda fl, it: 1.0)
    out = P.drive_floor_grid(ev, items, log=lambda *a: None)
    assert out["table"] == [] and out["identified"] is False
    assert out["chosen_nits"] < dimmest and ev.params.drive_floor_nits < dimmest


# ----------------------------------------------------------------------------- fade report + level report flags
def test_fade_report_flags_invented_worse_and_harm(monkeypatch):
    """Stub model: identity ring p by field level; its own correction removes it (q = 0) where the fade weight is 1
    (no fade, or a field at/above the fade's hi) and leaves it (q = p) below."""
    from dlc.fald import correct as C
    ring_p = {1: 0.02, 10: 0.05}

    class FakeModel:
        def __init__(self, p):
            self.p = p

        def render(self, shapes):
            img = np.zeros((3, 1, 2))
            img[0, 0, 0] = shapes[0][0][0]
            img[0, 0, 1] = shapes[1][0][0] if len(shapes) > 1 else shapes[0][0][0]
            return img

        def meter_img(self, img, meter):
            field, win, corrected = img[0, 0, 0], img[0, 0, 1], img[2].max() > 0
            if win == field:
                y = 1.0
            elif not corrected:
                y = 1.0 + ring_p[int(field)]
            else:
                lo, hi = self.p.lum_fade_lo, self.p.lum_fade_hi
                y = 1.0 + (0.0 if (not hi > lo or field >= hi) else ring_p[int(field)])
            return np.array([y, 0.0, 0.0])

    def fake_correct(model, img, **kw):
        req = img.copy()
        req[2, 0, 0] = 1e-3
        return {"req": req}

    monkeypatch.setattr(P, "FaldModel", FakeModel)
    monkeypatch.setattr(C, "correct_image", fake_correct)
    prm = FaldParams(transfer="gamma", code_bits=8, white_nits=255.0, sdr_gamma=1.0)        # code == nits
    field = lambda c: [((c, c, c), P.FULL)]
    items = [
        {"group": "rings@low", "name": "LOW1:R120", "shapes": field(1) + [((255,) * 3, P.FULL)], "base": field(1), "y": 1.00,
         "meter": (0, 0), "level": 1.0, "w": 1.0},                        # panel flat, model +2 %: invented
        {"group": "rings", "name": "RING10:R240", "shapes": field(10) + [((255,) * 3, P.FULL)], "base": field(10), "y": 1.05,
         "meter": (0, 0), "level": 10.0, "w": 1.0},                       # model exact
        {"group": "rings@held", "name": "RING10H:R120", "shapes": field(10) + [((254,) * 3, P.FULL)], "base": field(10), "y": 1.03,
         "meter": (0, 0), "level": 10.0, "w": 1.0},
        {"group": "peak", "name": "DRV:peak40", "shapes": [], "base": None, "y": 40.0, "meter": (0, 0), "w": 1.0},
    ]
    fr = P.fade_report(prm, items, fades=((0.0, 0.0), (2.0, 5.0)))
    assert fr["dimmest_grey_nits"] == 1.0 and len(fr["items"]) == 3
    low = next(r for r in fr["items"] if r["name"] == "LOW1:R120")
    assert low["invented"] and low["by_fade"]["0-0"]["worse"] and not low["by_fade"]["2-5"]["worse"]
    assert low["by_fade"]["0-0"]["harm_pp"] == pytest.approx(100 * (1 - 1 / 1.02), rel=1e-6)
    ring = next(r for r in fr["items"] if r["name"] == "RING10:R240")
    assert not ring["invented"] and ring["by_fade"]["0-0"]["panel_on_pp"] == pytest.approx(0.0, abs=1e-9)
    lv = {(r["split"], r["nits"]): r for r in fr["levels"]}
    assert set(lv) == {("fitted", 1.0), ("fitted", 10.0), ("heldout", 10.0)}
    assert lv[("fitted", 1.0)]["invented"] == 1 and lv[("fitted", 1.0)]["by_fade"]["0-0"]["n_worse"] == 1
    assert lv[("fitted", 1.0)]["by_fade"]["2-5"]["n_worse"] == 0 and lv[("fitted", 1.0)]["raw_mean_abs_pp"] == 0.0
    assert lv[("heldout", 10.0)]["by_fade"]["0-0"]["n_worse"] == 0
    assert set(fr["totals"]) == {"fitted", "heldout"}
    # level_report carries the same flags at the ideal inverse (q = 0)
    ev = type("Ev", (), {"params": prm, "predict": lambda self, it: {"LOW1:R120": 1.02, "RING10:R240": 1.05, "RING10H:R120": 1.05}[it["name"]]})()
    rows = {(r["group"], r["nits"]): r for r in P.level_report(ev, items)}
    assert rows[("rings@low", 1.0)]["invented"] == 1 and rows[("rings@low", 1.0)]["n_worse"] == 1
    assert rows[("rings", 10.0)]["n_worse"] == 0 and rows[("rings@low", 1.0)]["rendered_nits"] == 1.0


# ----------------------------------------------------------------------------- export seams
_DEFAULTS = dict(monitor=1, mode="SDR", simulate=True, pipe="", zones="24x24", diagonal_in=16.0, px_mm=None, meter=None,
                 bit_depth=None, white_nits=None, dogegen_server="127.0.0.1:28930", settle=0.0, profile=None,
                 no_native=False, quick=True, knots="never", verbose=False, name="sim", out=None, bin=None, fit_json=None,
                 keep_geometry=False, augment_regime="off", lum_fade=None, extended=False, estimate="auto", k0=None,
                 fade_report=True)


def _export_run(tmp_path, *, transfer="gamma", fit_extra=None, est_kind="knots"):
    g = _sdr_geo() if transfer == "gamma" else _geo()
    ctx = create_run("SDR" if transfer == "gamma" else "HDR", display="sim", run_dir=tmp_path / "run")
    fdir = ctx.root / "fald"
    fdir.mkdir(parents=True, exist_ok=True)
    pexp = P.params_dict(g.base_params(est_kind="exp"))
    pk = P.params_dict(g.base_params(est_kind="knots", est_knot_logw=(0.0, -0.2, -0.5, -0.9, -1.3, -2.0, -2.8, -3.6)))
    fit = {"params": pk if est_kind == "knots" else pexp, "params_exp": pexp, "params_knots": pk,
           "knots": {"gate": {"keep": est_kind == "knots"}, "gate_keep": est_kind == "knots"},
           "fade_report": {"dimmest_grey_nits": 0.518}, "mode": "SDR" if transfer == "gamma" else "HDR"}
    fit.update(fit_extra or {})
    fit_path = fdir / "fald_fit_result.json"
    fit_path.write_text(json.dumps(fit), encoding="utf-8")
    st = _common.load_dlc_state(ctx)
    st["fald"] = {"geometry": g.as_dict(), "mode": fit["mode"], "phases": {}, "fit_path": str(fit_path)}
    _common.save_dlc_state(ctx, st)
    return ctx


def _export(ctx, tmp_path, **over):
    args = Namespace(**{**_DEFAULTS, "run": ctx.root, "phase": "export", "out": str(tmp_path / "export"), **over})
    res = fald_profile.build(args, ctx)
    exported = None
    if res.status == "ran":
        exported = json.loads((tmp_path / "export" / f"sim_{'sdr' if res.metrics['transfer'] == 'gamma' else 'hdr'}_fald_fit_result.json").read_text(encoding="utf-8"))
    return res, exported


def test_export_sdr_requires_a_fade_choice(tmp_path):
    ctx = _export_run(tmp_path)
    res, _ = _export(ctx, tmp_path)
    assert res.status == "blocked" and res.anomalies[-1].code == "lum_fade_required" and "0.518" in res.anomalies[-1].detail


def test_export_sdr_lum_fade_keep_and_values(tmp_path):
    ctx = _export_run(tmp_path)
    res, exp = _export(ctx, tmp_path, lum_fade="keep")
    assert res.status == "ran" and res.metrics["lum_fade"] == [0.5, 5.0] and "keep" in exp["lum_fade_chosen"]["by"]
    res, exp = _export(ctx, tmp_path, lum_fade="1,3")
    assert res.status == "ran" and res.metrics["lum_fade"] == [1.0, 3.0] and exp["params"]["lum_fade_lo"] == 1.0
    res, _ = _export(ctx, tmp_path, lum_fade="0.5,2")                     # the 0.5-nit grey's nominal level: allowed
    assert res.status == "ran" and res.metrics["lum_fade"] == [0.5, 2.0]
    res, _ = _export(ctx, tmp_path, lum_fade="0.3,2")                     # below the dimmest measured grey
    assert res.status == "blocked" and res.anomalies[-1].code == "lum_fade_below_data"
    res, _ = _export(ctx, tmp_path, lum_fade="2,1")
    assert res.status == "blocked" and res.anomalies[-1].code == "lum_fade_arg"
    res, _ = _export(ctx, tmp_path, lum_fade="soft")
    assert res.status == "blocked" and res.anomalies[-1].code == "lum_fade_arg"


def test_export_hdr_does_not_require_a_fade(tmp_path):
    ctx = _export_run(tmp_path, transfer="pq")
    res, exp = _export(ctx, tmp_path)
    assert res.status == "ran" and res.metrics["transfer"] == "pq" and "lum_fade_chosen" not in exp


def test_export_estimate_choice(tmp_path):
    ctx = _export_run(tmp_path, est_kind="knots")
    res, exp = _export(ctx, tmp_path, lum_fade="keep")
    assert res.status == "ran" and res.metrics["estimate"] == "knots" and exp["estimate_chosen"]["requested"] == "auto"
    res, exp = _export(ctx, tmp_path, lum_fade="keep", estimate="exp")
    assert res.status == "ran" and res.metrics["estimate"] == "exp" and exp["params"]["est_kind"] == "exp"
    ctx2 = _export_run(tmp_path / "b", est_kind="exp", fit_extra={"params_knots": None, "knots": None})
    res, _ = _export(ctx2, tmp_path / "b", lum_fade="keep", estimate="knots")
    assert res.status == "blocked" and res.anomalies[-1].code == "estimate_unavailable"


def test_export_estimate_params_derives_from_pre_gate_fit_json():
    logw = [0.0, -0.3, -0.6, -1.0, -1.4, -2.1, -2.9, -3.7]
    old_exp_kept = {"params": {"est_kind": "exp", "est_knot_logw": []}, "knots": {"est_knot_logw": logw, "kept": False}}
    pd, why = fald_profile.export_estimate_params(old_exp_kept, "knots")
    assert pd["est_kind"] == "knots" and pd["est_knot_logw"] == logw and "derived" in why
    old_knots_kept = {"params": {"est_kind": "knots", "est_knot_logw": logw}, "knots": {"est_knot_logw": logw, "kept": True}}
    pd, _ = fald_profile.export_estimate_params(old_knots_kept, "exp")
    assert pd["est_kind"] == "exp" and pd["est_knot_logw"] == []
    assert fald_profile.export_estimate_params(old_knots_kept, "auto")[0]["est_kind"] == "knots"
    assert fald_profile.export_estimate_params({"params": {"est_kind": "exp"}}, "knots")[0] is None


# ----------------------------------------------------------------------------- synthetic SDR truth
def test_synthetic_sdr_truth_uses_a_power_law_drive_curve():
    g = _sdr_geo()
    assert P.SyntheticFaldPanel.hidden(g).params.drive_curve == P.power_drive_curve(g.white_nits, P.SYNTH_SDR_DRIVE_K)
    assert P.SyntheticFaldPanel.hidden(_geo()).params.drive_curve == FaldParams().drive_curve


@pytest.mark.slow
def test_synthetic_sdr_fit_recovers_drive_k_and_tmin():
    g = _sdr_geo()
    panel = P.SyntheticFaldPanel.hidden(g)
    pats, reads = [], {}
    for ph in ("drive", "leak", "rings", "heldout"):
        for p in P.PLANS[ph](g):
            pats.append(p)
            reads[p.name] = P.Read(p.name, panel.read(p.shapes, g.meter))
    dc = P.drive_curve_from_reads(g, pats, reads)
    assert not dc                                                              # the SDR code-0 sweep is at the floor
    base = g.base_params(white_nits=reads["DRV:white"].y, chan_weights=P.chan_weights_from_reads(reads))
    items = P.build_items(pats, reads, g.meter)
    res = P.run_fit(base, items, quick=False, knots="never", fit_drive_k=True, log=lambda *a: None)
    k = res["stage_b"]["drive_k"]
    tmin = res["stage_a"]["tmin"]
    assert abs(k - P.SYNTH_SDR_DRIVE_K) < 0.1, res["drive_k_consistency"]
    assert abs(tmin / panel.params.tmin - 1.0) < 0.3, (tmin, res["drive_k_consistency"])
    assert res["drive_k_consistency"]["converged"]
