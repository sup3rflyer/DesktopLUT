"""The user-facing FALD profiling flow (dlc.fald.profile + dlc.stages.fald_profile): plans respect the
meter keep-out and the panel's transfer, the synthetic panel + fit recover a hidden truth, and the stage
tool chains under --simulate with seams (one StageResult per phase) and check-in events."""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

pytest.importorskip("scipy")
from dlc.fald import profile as P  # noqa: E402
from dlc.fald.model import FaldParams  # noqa: E402
from dlc.runs import create_run  # noqa: E402
from dlc.stages import fald_profile  # noqa: E402
from dlc.stages import _common  # noqa: E402


def _geo(**kw):
    base = dict(width=1920, height=1080, cols=24, rows=24, diagonal_in=16.0, meter=(975, 555), white_nits=1000.0)
    base.update(kw)
    return P.PanelGeometry.from_diagonal(**base)


# ----------------------------------------------------------------------------- geometry + plans
def test_geometry_from_diagonal_and_codes():
    g = P.PanelGeometry.from_diagonal(3840, 2160, 48, 48, 32.0, meter=(1950, 1110), white_nits=1842.0)
    assert abs(g.px_mm - 0.1845) < 0.001
    assert g.cell_w == 80 and g.cell_h == 45 and g.meter_cell == (24, 24)
    assert g.code(10.0) == 307                      # PQ 10 nits
    assert g.min_gap_h >= 110 and g.min_gap_v >= 185     # past the i1D3 body


def test_sdr_gamma_codes_round_trip():
    g = P.PanelGeometry.from_diagonal(3840, 2160, 48, 48, 32.0, meter=(1950, 1110), transfer="gamma", bit_depth=8,
                                      white_nits=120.0, sdr_gamma=2.2)
    c = g.code(10.0)
    assert 0 < c < 255 and abs(g.nits(c) - 10.0) < 0.5
    assert g.white == (255, 255, 255) and g.nits(255) == 120.0


def test_choose_scale_exact_and_rescaled():
    assert P.choose_scale(3840, 2160, 48, 48) == (5, 3840, 2160)
    s, w, h = P.choose_scale(2560, 1440, 40, 25)          # 64 × 57.6 px cells: no exact factor
    assert (w // s) % 40 == 0 and (h // s) % 25 == 0
    # a rescaled canvas: the meter follows it and px_mm keeps the panel's physical size
    g = P.PanelGeometry.from_diagonal(2560, 1440, 40, 25, 27.0, meter=(1280, 720))
    prm = g.base_params()
    cm = g.canvas_meter(prm)
    assert abs(cm[0] - prm.width / 2) < 1e-6 and abs(cm[1] - prm.height / 2) < 1e-6
    assert abs(prm.px_mm * prm.width - g.px_mm * g.width) < 1e-6


def test_plans_drop_offpanel_patterns_and_preflight_can_count_them():
    g = _geo(meter=(60, 540))                              # sensor 60 px from the left edge
    dropped = P.plan_dropped(g)
    assert dropped.get("leak") and dropped.get("rings")     # every L-side window is off the panel
    for name, fn in P.PLANS.items():
        for p in fn(g):
            assert all(geo[2] > 0 and geo[3] > 0 for _, geo in p.shapes), (name, p.name)


def test_plans_keep_highlights_outside_the_keepout():
    g = _geo()
    mx, my = g.meter
    for name, fn in P.PLANS.items():
        if name == "register":
            continue                                                # sweeps a window edge ACROSS the sensor by design
        for p in fn(g):
            for code, (x, y, cx, cy) in p.shapes[1:]:
                if max(code) < g.code(0.5 * g.white_nits):
                    continue                                        # dim content may sit anywhere
                x0, y0, x1, y1 = x * g.width, y * g.height, (x + cx) * g.width, (y + cy) * g.height
                if x0 <= mx <= x1 and y0 <= my <= y1:
                    assert name == "drive" and (p.group in ("peak", "white", "primaries") or p.name.startswith("DRV:flat")), (name, p.name)
                    continue
                dx = max(x0 - mx, mx - x1, 0.0)
                dy = max(y0 - my, my - y1, 0.0)
                assert dx >= g.min_gap_h - 1 or dy >= g.min_gap_v - 1, (name, p.name, dx, dy)


def test_ratio_patterns_reference_an_aux_pattern():
    g = _geo()
    for name, fn in P.PLANS.items():
        pats = fn(g)
        names = {p.name for p in pats}
        for p in pats:
            if p.kind == "ratio":
                assert p.ref in names and next(q for q in pats if q.name == p.ref).kind == "aux", (name, p.name)


# ----------------------------------------------------------------------------- reads → items
def _synthetic_reads(g, phases=("register", "drive", "leak", "rings", "heldout")):
    panel = P.SyntheticFaldPanel.hidden(g)
    pats, reads = [], {}
    for ph in phases:
        for p in P.PLANS[ph](g):
            pats.append(p)
            reads[p.name] = P.Read(p.name, panel.read(p.shapes, g.meter))
    return panel, pats, reads


def test_items_use_ring_ratios_and_drop_floor_reads():
    g = _geo()
    _, pats, reads = _synthetic_reads(g, ("rings",))
    items = P.build_items(pats, reads, g.meter)
    assert items and all(it["base"] is not None for it in items)
    assert 0.5 < min(it["y"] for it in items) < max(it["y"] for it in items) < 1.6
    groups = {it["group"] for it in items}
    assert {"rings", "rings@fine", "rings@area", "rings@drive", "rings@held"} <= groups


def test_chan_weights_and_flat_sweep_from_drive_reads():
    g = _geo()
    panel, pats, reads = _synthetic_reads(g, ("drive",))
    cw = P.chan_weights_from_reads(reads)
    assert cw is not None and abs(sum(cw) - 1.0) < 1e-9
    assert all(abs(a - b) < 0.02 for a, b in zip(cw, panel.params.chan_weights))
    sweep = P.flat_sweep(g, pats, reads)
    assert len(sweep) >= 8
    top = [r for r in sweep if r["fraction"] == 1.0][0]
    assert abs(top["measured_nits"] / top["expected_nits"] - 1.0) < 0.05


def test_sdr_gamma_fit_recovers_the_exponent():
    g = _geo(transfer="gamma", bit_depth=8, white_nits=120.0, sdr_gamma=2.2)
    panel, pats, reads = _synthetic_reads(g, ("drive",))
    sweep = P.flat_sweep(g, pats, reads)
    gamma = P.fit_sdr_gamma(sweep, reads["DRV:white"].y, g.max_code)
    assert gamma is not None and abs(gamma - 2.2) < 0.1


def test_register_finds_a_shifted_sensor_without_a_kernel():
    g = _geo()
    truth = (g.meter[0] + 11, g.meter[1] - 7)
    panel = P.SyntheticFaldPanel.hidden(g)
    pats = P.plan_register(g)
    reads = {p.name: P.Read(p.name, panel.read(p.shapes, truth)) for p in pats}
    reg = P.register_sensor(g, pats, reads)                         # no kernel involved
    assert reg["ok"], reg
    # the model's aperture is a disc sampled on a 5-px grid and the sweep steps 10 px: the midpoint lands
    # within ~one reduced pixel of the truth (the ProArt's meter self-registration was good to ~7 px)
    assert abs(reg["sensor_px"][0] - truth[0]) <= 8 and abs(reg["sensor_px"][1] - truth[1]) <= 8, reg["sensor_px"]
    assert 60 <= reg["x"]["width_10_90_px"] <= 140            # ≈ the aperture diameter (2 × 55 px)


def test_grid_step_finds_the_boundary():
    g = _geo()
    panel = P.SyntheticFaldPanel.hidden(g)
    pats = P.plan_grid(g)
    reads = {p.name: P.Read(p.name, panel.read(p.shapes, g.meter)) for p in pats}
    a0 = g.base_params().stat_area0_px2                            # the prior (the truth is 1000): ± ~10 px
    for ax, cell in (("x", g.cell_h), ("y", g.cell_w)):
        stp = P.grid_step(pats, reads, ax, a0, cell)
        assert stp["ok"] and stp["contrast"] > 0.02 and abs(stp["offset_px"]) <= 12, stp


def test_predictions_freeze_and_compare():
    g = _geo()
    panel, pats, reads = _synthetic_reads(g, ("heldout",))
    pred = P.predictions(panel.params, pats, g.meter)              # the truth predicts itself
    score = P.compare_predictions(pats, reads, pred)
    assert score and all(v["mean_abs"] < 1.5 for v in score.values()), score


@pytest.mark.slow
def test_quick_fit_recovers_the_hidden_estimate():
    g = _geo()
    panel, pats, reads = _synthetic_reads(g)
    dc = P.drive_curve_from_reads(g, pats, reads)
    base = g.base_params(white_nits=reads["DRV:white"].y, chan_weights=P.chan_weights_from_reads(reads),
                         **({"drive_curve": dc} if dc else {}))
    items = P.build_items(pats, reads, g.meter)
    res = P.run_fit(base, items, quick=False, knots="never", fit_drive_k=not dc, log=lambda *a: None)
    t = panel.params
    b = res["stage_b"]
    assert abs(b["est_scale_mm"] - t.est_scale_mm) < 4.0
    assert abs(b["est_phase_px"] - t.est_phase_px) < 12.0
    assert res["heldout"]["rings@held"]["mean_abs"] < 2.5
    p2 = P.params_from_dict(res["params"])
    assert isinstance(p2, FaldParams) and p2.transfer == "pq"


# ----------------------------------------------------------------------------- the stage tool under --simulate
_DEFAULTS = dict(monitor=1, mode="SDR", simulate=True, pipe="", zones="32x18", diagonal_in=32.0, px_mm=None, meter=None,
                 bit_depth=None, white_nits=1000.0, dogegen_server="127.0.0.1:28930", settle=0.0, profile=None,
                 no_native=False, quick=True, knots="never", verbose=False, name="sim", out=None, bin=None, fit_json=None)


def _ns(ctx, **over):
    return Namespace(**{**_DEFAULTS, "run": ctx.root, **over})


def _run(ctx, phase, **over):
    res = fald_profile.build(_ns(ctx, phase=phase, **over), ctx)
    _common.record_stage(ctx, res)
    return res


def test_stage_preflight_records_geometry_and_enters_native(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    res = _run(ctx, "preflight")
    assert res.status == "ran", res.as_dict()
    assert res.metrics["width"] == 2560 and res.metrics["cols"] == 32 and res.metrics["cell_px"] == [80.0, 80.0]
    st = _common.load_dlc_state(ctx)
    assert st["fald"]["geometry"]["transfer"] == "gamma" and st["fald"]["geometry"]["bit_depth"] == 8
    assert "entered calibration mode" in " ".join(res.actions_taken)


def test_stage_refuses_measuring_before_preflight(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    res = _run(ctx, "rings")
    assert res.status == "blocked" and res.anomalies[0].code == "no_preflight" and ":" not in res.stage


def test_stage_preflight_rejects_bad_zones(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    res = _run(ctx, "preflight", zones="2304")
    assert res.status == "blocked" and res.anomalies[0].code == "zones_arg"


def test_stage_measuring_phases_emit_checkins_and_honour_cancel(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    reg = _run(ctx, "register")
    assert reg.status == "ran", reg.as_dict()
    assert reg.preconditions["transport_ok"] and "sensor_px" in reg.metrics
    events = [json.loads(l) for l in ctx.events_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    cis = [e for e in events if e["event"] == "check_in" and e["stage"] == "fald-profile"]
    assert cis and cis[0]["phase"] == "register" and "reads" in cis[0]["data"] and cis[0]["tier"] == "digest"
    (ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")
    rings = _run(ctx, "rings")
    assert rings.status == "failed" and rings.anomalies[0].code == "cancelled"


def test_acm_off_is_graded_by_the_pipe_build():
    """C8 (2026-09-14): a build that reads ACM through DisplayConfig (color_mode_source present) and still says
    SDR means ACM is really off -> high; an older build (no color_mode_source) cannot see ACM -> advisory."""
    new_off = fald_profile.acm_off_anomaly("SDR", {"color_space": "SDR", "color_mode_source": "displayconfig2"})
    assert new_off and new_off[0] == "acm_off" and new_off[2] == "high"
    old = fald_profile.acm_off_anomaly("SDR", {"color_space": "SDR"})
    assert old and old[0] == "acm_off" and old[2] == "medium" and "false positive" in old[1]
    assert fald_profile.acm_off_anomaly("SDR", {"color_space": "ACM_SDR", "color_mode_source": "displayconfig2"}) is None
    assert fald_profile.acm_off_anomaly("HDR", {"color_space": "SDR", "color_mode_source": "dxgi"}) is None
    assert fald_profile.acm_off_anomaly("SDR", {"color_space": "HDR"}) is None     # mode_mismatch handles that


@pytest.mark.slow
def test_stage_chain_sdr_to_export_and_verify(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    for ph in ("preflight", "register", "grid", "drive", "leak", "rings"):
        res = _run(ctx, ph)
        assert res.status == "ran", (ph, res.as_dict())
    fit = _run(ctx, "fit")
    assert fit.status == "ran", fit.as_dict()
    assert fit.metrics["stage_b"]["est_scale_mm"] > 0 and "rings@held" in fit.metrics["heldout"]
    held = _run(ctx, "heldout")
    assert held.status == "ran" and (ctx.root / "fald" / "heldout_predictions.json").exists()
    exp = _run(ctx, "export", out=str(tmp_path / "export"))
    assert exp.status == "ran" and exp.metrics["format"] == "FLD3" and (tmp_path / "export" / "sim_sdr_fald_panel.bin").exists()
    assert exp.metrics["transfer"] == "gamma" and any("FLD3" in n for n in exp.notes)
    ver = _run(ctx, "verify")                       # the mock accepts mode SDR since the 2026-09-14 port (work guide P7)
    assert ver.status == "ran", ver.as_dict()
    assert "scorecard" in ver.metrics and ver.raw["set_fald_params"]["transfer"] == "gamma"
    assert _run(ctx, "restore").status == "ran"
