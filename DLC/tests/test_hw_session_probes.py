"""The 2026-10 HW-session probes (DLC root: probe_hw_common.py, probe_near_black.py, probe_ld_additivity.py,
probe_full_stack_spots.py, probe_hw10_grayscale_roundtrip.py): pure helpers (code math, geometry, patch
lists, settle detection, .ti3, stack diff, additivity bookkeeping, Probe C port, HW-10 comparisons) and the
--simulate end-to-end paths (mock pipe + synthetic panel on a virtual clock — no hardware)."""
from __future__ import annotations

import json
import math
import random
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import probe_full_stack_spots as fs  # noqa: E402
import probe_hw10_grayscale_roundtrip as hw10  # noqa: E402
import probe_hw_common as hc  # noqa: E402
import probe_ld_additivity as ld  # noqa: E402
import probe_near_black as nb  # noqa: E402

PA_ADDITIVITY = ROOT / ld.ANALYSIS_SCRIPT


# ----------------------------------------------------------------------------- code math
def test_pq_codes_match_the_charter_values():
    assert [hc.pq_code(n) for n in (1, 5, 100, 1000)] == [153, 254, 520, 769]
    assert hc.pq_nits(837) == pytest.approx(1837.34, abs=0.01)
    for c in (1, 99, 316, 553, 749, 921, 1023):
        assert hc.pq_code(hc.pq_nits(c)) == c


def test_sdr_codes_round_trip_and_clip():
    for c in (1, 25, 64, 128, 255):
        assert hc.sdr_code(hc.sdr_nits(c, 120.0), 120.0) == c
    assert hc.sdr_code(500.0, 120.0) == 255
    assert hc.sdr_code(0.0, 120.0) == 0
    assert hc.sdr_code(5.0, 120.0, bit_depth=10) == pytest.approx(4 * hc.sdr_code(5.0, 120.0), abs=4)


def test_hdr_to_sdr_rule_keeps_linear_ratios():
    assert hc.hdr_to_sdr_code(837) == 255
    codes = [hc.hdr_to_sdr_code(c, bit_depth=10) for c in ld.p2_component_codes()]
    assert codes == sorted(codes) and len(set(codes)) == len(codes)
    for mix in ld.P2_MIXES:
        sdr = tuple(ld.map_code(c, "SDR", 10, 2.2) for c in mix)
        assert ld.mix_ratio(sdr, "SDR", 10, 2.2) == pytest.approx(ld.mix_ratio(mix, "HDR", 10, 2.2), rel=0.03)


# ----------------------------------------------------------------------------- geometry / patches
def test_geometry_zones_gaps_and_normalisation():
    g = hc.Geometry(3840, 2160, (1950, 1110), 48, 48, 32.0)
    assert (g.cell_w, g.cell_h) == (80.0, 45.0)
    assert g.zone_of(*g.meter) == (24, 24)
    assert g.window_pct(100) == (0.0, 0.0, 3840.0, 2160.0)
    assert g.gap_px(g.centred(100, 100)) == 0.0
    assert g.gap_px((2080, 1035, 160, 135)) == pytest.approx(130.0)
    assert g.norm((-10, -10, 100, 100)) == (0.0, 0.0, round(90 / 3840, 6), round(90 / 2160, 6))
    bw, bh = g.body_px()
    assert 190 < bw < 210 and 340 < bh < 360            # the 199x353-px footprint of the FALD sessions


def test_patch_refuses_multi_rect_frames():
    with pytest.raises(ValueError):
        hc.Patch("x", "p", [((0, 0, 0), (0, 0, 1, 1))] * 3, (0, 0, 0))
    p = hc.Patch("x", "p", hc.framed(hc.Geometry(100, 100, (50, 50)), (1, 1, 1), (2, 2, 2), (40, 40, 20, 20)), (2, 2, 2))
    assert len(p.shapes) == 2 and p.field == (2, 2, 2)


def test_reads_needed_rule():
    assert hc.reads_needed(0.05) == 5 and hc.reads_needed(None) == 5
    assert hc.reads_needed(1.5) == 3 and hc.reads_needed(500.0, min_reads=6) == 6


# ----------------------------------------------------------------------------- settle detection
def _series(fn, n, dt=1.0):
    t = [i * dt for i in range(n)]
    return t, [(fn(x) * 0.95, fn(x), fn(x) * 1.09) for x in t]


def test_settle_accepts_noise_and_rejects_trends():
    rng = random.Random(3)
    t, flat = _series(lambda x: 100.0 * (1 + rng.gauss(0, 0.001)), 4)
    assert hc.tail_settled(t, flat)[0]
    t, dark = _series(lambda x: 0.05 * (1 + rng.gauss(0, 0.03)), 4, dt=7.0)      # sub-nit: scatter >> 0.5 %
    assert hc.tail_settled(t, dark)[0]
    t, ramp = _series(lambda x: 100.0 * (1 + 0.01 * x), 4)                       # an LED ramp: 3 % over the tail
    assert not hc.tail_settled(t, ramp)[0]
    t, decay = _series(lambda x: 100.0 * (1 - 0.1 * math.exp(-x / 1.0)), 4)      # early exponential approach
    assert not hc.tail_settled(t, decay)[0]
    t2 = [x + 8 for x in t]
    _t, late = _series(lambda x: 100.0 * (1 - 0.1 * math.exp(-(x + 8) / 1.0)), 4)
    assert hc.tail_settled(t2, late)[0]
    assert not hc.tail_settled([0, 1], [(1, 1, 1)] * 2)[0]


@pytest.mark.parametrize("ys", [[70, 100, 100, 100], [90, 99.6, 99.99, 100], [100, 100, 100, 90],
                                [0.060, 0.054, 0.054, 0.054]])
def test_settle_rejects_single_read_steps(ys):
    # the review's counter-examples: a 2-dof self-estimated SE scaled with the outlier and accepted them all
    t = [0.0, 1.0, 2.0, 3.0]
    assert not hc.tail_settled(t, [(y, y, y) for y in ys])[0]


def test_settle_min_span_and_growing_suffix():
    t = [0.0, 0.4, 0.8, 1.2]
    xyz = [(100.0, 100.0, 100.0)] * 4
    assert hc.tail_settled(t, xyz)[0] and not hc.tail_settled(t, xyz, min_span_s=2.0)[0]
    ts = [0.4 * i for i in range(8)]
    xs = [(70.0, 70.0, 70.0)] + [(100.0, 100.0, 100.0)] * 7       # a contaminated first read drops out
    assert hc.settled_tail(ts, xs, min_span_s=2.0) == 2               # the shortest suffix spanning >= 2 s
    assert hc.settled_tail(ts[:4], xs[:4]) is None


def test_reader_never_uses_a_spotread_flagged_read():
    from dlc.measure_loop import Reading
    pres = hc.SimPresenter(hc.VirtualClock(), 0.0)
    rd = hc.make_reader(pres, lambda patch: Reading(xyz=(1.0, 2.0, 3.0), ok=False, error="unreliable"), 8)
    assert rd("x", hc.full((1, 1, 1)), (1, 1, 1)) == ((1.0, 2.0, 3.0), False, "unreliable", None)
    rd = hc.make_reader(pres, lambda patch: Reading(xyz=None, ok=False, error="dead", raw={"meter_fault": "closed"}), 8)
    assert rd("x", hc.full((1, 1, 1)), (1, 1, 1))[3] == "closed"


def _sim_session(tmp_path, **kw):
    args = Namespace(monitor=1, mode="SDR", simulate=True, run=str(tmp_path / "s"), tag="", bit_depth=8,
                     present_dwell_s=0.25, settle_rel_tol=hc.SETTLE_REL_TOL, settle_abs_tol=hc.SETTLE_ABS_TOL,
                     settle_min_span_s=None, settle_max_s=60.0, settle_max_reads=200, idle_between="black", **kw)
    s = hc.ProbeSession(args, "unit")
    g = hc.Geometry(3840, 2160, (1920, 1080))
    s.open_meter(g, panel=hc.SimPanel("lcd", "SDR", 8, g, white_nits=107.0))
    return s


def test_measure_patch_flags_and_stops_on_a_dead_meter(tmp_path):
    s = _sim_session(tmp_path)
    p = hc.Patch("w", "t", hc.full((255, 255, 255)), (255, 255, 255))
    r = s.measure_patch(p)
    assert r.settled and r.n_kept >= 3 and r.y == pytest.approx(107.0, rel=0.02)
    s.read_fn = lambda *a: ((1.0, 1.0, 1.0), False, "under range", None)
    for _ in range(2):
        assert s.measure_patch(p).xyz is None
    with pytest.raises(hc.MeterDown):
        s.measure_patch(p)                                            # 3 patches without a usable read
    s.read_fn = lambda *a: (None, False, "dead", "self_heal_exhausted")
    with pytest.raises(hc.MeterDown):
        s.measure_patch(p)
    assert {a["kind"] for a in s.anomalies} >= {"flagged_reads", "no_read"}


def test_stack_diff_counts_an_active_perm_change():
    st = {"mhc": {"1:SDR": {"applied": True, "active_perm": 2}}}
    pre = hc.stack_snapshot(st, 1)
    st["mhc"]["1:SDR"]["active_perm"] = 0
    assert [c["what"] for c in hc.stack_diff(pre, hc.stack_snapshot(st, 1))["changed"]] == ["active_perm"]


# ----------------------------------------------------------------------------- ti3 / stack
def test_ti3_round_trips_through_dlc_parse(tmp_path):
    from dlc.mhc import parse_ti3
    rows = [((0, 0, 0), (0.05, 0.054, 0.08)), ((255, 0, 0), (40.0, 21.0, 2.0)), ((749, 553, 553), (1.0, 2.0, 3.0))]
    p = hc.write_ti3(tmp_path / "a" / "x.ti3", rows[:2], bit_depth=8, title="t", notes=["n"])
    s = parse_ti3(p)
    assert len(s) == 2 and s[1].xyz[1] == pytest.approx(21.0)
    q = hc.write_ti3(tmp_path / "y.ti3", rows[2:], bit_depth=10, title="t")
    (rgb, xyz), = hc.read_ti3_rows(q)
    assert [round(v * 1023) for v in rgb] == [749, 553, 553] and xyz == [1.0, 2.0, 3.0]


def test_stack_diff_separates_profile_churn_from_changes():
    st = {"mhc": {"0:HDR": {"applied": True, "profile_name": "a.icm", "source_file": "s.cube"}},
          "runtime": {"0:HDR": {"cube_path": "c.cube"}},
          "layers": {"0:HDR": {"fald": True, "white_balance": False, "fald_params_path": "p.bin"}}}
    pre = hc.stack_snapshot(st, 0)
    churn = json.loads(json.dumps(st))
    churn["mhc"]["0:HDR"]["profile_name"] = "b.icm"
    d = hc.stack_diff(pre, hc.stack_snapshot(churn, 0))
    assert not d["changed"] and d["churn"]
    gone = json.loads(json.dumps(st))
    gone["runtime"]["0:HDR"] = {}
    gone["layers"]["0:HDR"]["fald"] = False
    what = {c["what"] for c in hc.stack_diff(pre, hc.stack_snapshot(gone, 0))["changed"]}
    assert what == {"cube_path", "layer:fald"}


# ----------------------------------------------------------------------------- near-black plans
def _nb_args(**kw):
    a = dict(bit_depth=8, surround_code=64, hwa_holes="600,1200", hole_px=600.0, hwb_codes=",".join(map(str, nb.HWB_CODES)),
             hwb_surround="full", hwc_codes=",".join(map(str, nb.HWC_CODES)), hwc_colours="R,G,B,C,M,Y", meter=None,
             diagonal_in=27.0, mode="SDR")
    a.update(kw)
    return Namespace(**a)


def test_hwA_geometry_keeps_lit_content_away_and_covers_the_frame():
    g = nb.geometry(_nb_args(), 3840, 2160)
    pats = nb.plan_hwA(_nb_args(), g)
    lit = [p for p in pats if p.cond.startswith("lit")]
    assert lit and all(p.meta["keepout_px"] >= hc.KEEPOUT_PX and p.meta["lit_fraction"] >= 0.40 for p in lit)
    assert {p.cond for p in pats} == {"full", "lit600", "lit1200", "black600"}
    assert all(p.field in ((0, 0, 0), (1, 1, 1), (3, 3, 3)) for p in pats)
    with pytest.raises(hc.Refusal):
        nb.check_hole(g, 200.0)                         # 100 px from the meter < 120


def test_hwB_has_what_teardrop_fit_needs():
    pats = nb.plan_hwB(_nb_args(), nb.geometry(_nb_args(), 3840, 2160))
    fields = {p.field for p in pats}
    assert (0, 0, 0) in fields
    for k in range(3):
        ramp = sorted(p.field[k] for p in pats if p.field[k] > 0 and sum(1 for c in p.field if c) == 1)
        assert ramp[0] == 2 and ramp[-1] == 255 and 40 in ramp           # dense bottom + the full-drive anchor
    assert {(1, 1, 1), (3, 3, 3)} <= fields
    assert all(len(p.shapes) == 1 for p in pats)                        # full field = the raw.ti3 condition


def test_hwC_block_scales_codes_and_includes_black():
    pats = nb.plan_hwC_block(_nb_args(bit_depth=10), "C", "cube")
    assert pats[0].field == (0, 0, 0)
    assert pats[1].field == (0, nb.s8(4, 10), nb.s8(4, 10)) and nb.s8(255, 10) == 1023


# ----------------------------------------------------------------------------- LD additivity plans
def _ld_args(**kw):
    p = ld.build_parser()
    a = p.parse_args(["--phase", "plan"] + [x for k, v in kw.items() for x in (f"--{k.replace('_', '-')}", str(v))])
    a.mode = a.mode.upper()
    return a


def test_p2_plan_has_every_single_twice_and_the_listed_mixes():
    a = _ld_args()
    g = ld.geometry(a, 3840, 2160)
    pats = ld.plan_p2(a, g)
    singles = [p.field for p in pats if p.group == "single"]
    for c in ld.p2_component_codes():
        for k in range(3):
            v = [0, 0, 0]
            v[k] = c
            assert singles.count(tuple(v)) == 2                       # ascending before, descending after the mixes
    mixes = [p.field for p in pats if p.group == "mix"]
    assert sorted(mixes) == sorted(list(ld.P2_MIXES) + list(ld.P2_HIGH_MIXES))   # high-ratio mixes are the default
    a0 = _ld_args()
    a0.p2_high_ratio = False                                          # --no-p2-high-ratio
    assert sorted(p.field for p in ld.plan_p2(a0, g) if p.group == "mix") == sorted(ld.P2_MIXES)
    assert sorted(p.field[0] for p in pats if p.group == "grey") == sorted(ld.P2_GREYS)
    assert pats[0].field == pats[-1].field == (0, 0, 0)
    a2 = _ld_args()
    a2.p2_rotations, a2.p2_high_ratio = True, True
    mixes2 = [p.field for p in ld.plan_p2(a2, g) if p.group == "mix"]
    assert len(mixes2) == 3 * (len(ld.P2_MIXES) + len(ld.P2_HIGH_MIXES))
    assert max(p.meta["ratio_hdr"] for p in ld.plan_p2(a2, g) if p.group == "mix") > 0.6


def test_p1_hdr_levels_and_sdr_fallback():
    a = _ld_args()
    pats = ld.plan_p1(a, ld.geometry(a, 3840, 2160))
    codes = [p.meta["code"] for p in pats if p.name.startswith("P1:L")]
    assert codes == [hc.pq_code(n) for n in ld.P1_NITS]
    assert sum(1 for p in pats if p.meta.get("drift")) == 3
    levels, dropped = ld.p1_levels("SDR", 8, sdr_white=120.0)
    assert dropped == [300.0, 1000.0] and levels[-1]["code"] == 255
    assert [lv["nits"] for lv in levels[:-1]] == [1, 2, 5, 10, 25, 60, 100]


def test_p0_window_is_lattice_aligned_beside_the_meter():
    a = _ld_args()
    g = ld.geometry(a, 3840, 2160)
    rect, info = ld.p0_window(a, g)
    assert info["window_zones"] == [26, 23, 2, 3] and info["gap_px"] >= hc.KEEPOUT_PX
    assert rect[0] % g.cell_w == 0 and rect[1] % g.cell_h == 0
    pats = ld.plan_p0(a, g)
    assert all(p.field == (0, 0, 0) for p in pats)                    # the meter always sits on black
    win = [p for p in pats if p.group != "floor"]
    assert len(win) == 2 * 3 and {p.ti3_rgb for p in win} == {(749, 749, 749), (749, 0, 0), (0, 0, 749)}   # 749 default
    assert all(p.min_reads >= 5 for p in win)                         # the glow 2 columns out is ~<= 0.1 nit
    a_both = _ld_args(p0_codes="553,749")
    assert len([p for p in ld.plan_p0(a_both, g) if p.group != "floor"]) == 2 * 6
    a.p0_col_offset = 1
    with pytest.raises(hc.Refusal):
        ld.p0_window(a, g)                                            # 50 px from the meter


def test_ld_state_follows_the_dimming_speed():
    assert ld.ld_state(Namespace(dimming_speed="fast", ld=None)) == "on"
    assert ld.ld_state(Namespace(dimming_speed="off", ld="off")) == "off"
    with pytest.raises(SystemExit):
        ld.ld_state(Namespace(dimming_speed="gradual", ld="off"))


# ----------------------------------------------------------------------------- additivity bookkeeping
def _synthetic_p2(tmp_path, *, pedestal_nits: float, ld_on: bool):
    """A ti3 from the synthetic FALD panel (LD on: the 132412 law + a minor-channel deficit; LD off:
    a constant pedestal) in the pa_additivity.py layout."""
    a = _ld_args()
    g = ld.geometry(a, 3840, 2160)
    panel = hc.SimPanel("fald", "HDR", 10, g, ld_on=ld_on)
    rows = [(p.field, panel.expected(p.shapes, (0.5, 0.5))) for p in ld.plan_p2(a, g) if p.cond != "pedestal"]
    d = tmp_path / ("on" if ld_on else "off")
    hc.write_ti3(d / "measurements" / "raw.ti3", rows, bit_depth=10, title="synthetic")
    return d, rows


def test_measured_black_bookkeeping_is_exact_on_an_additive_panel(tmp_path):
    pytest.importorskip("colour")
    d, rows = _synthetic_p2(tmp_path, pedestal_nits=1.9, ld_on=False)
    rgb = [[c / 1023 for c in r[0]] for r in rows]
    out = ld.additivity_rows(rgb, [r[1] for r in rows], transfer="pq", pedestal="measured_black")
    assert len(out) == len(ld.P2_MIXES) + len(ld.P2_HIGH_MIXES) + len(ld.P2_GREYS)   # --p2-high-ratio is the default
    assert max(r["de"] for r in out) < 0.05 and all(abs(r["yr"] - 1) < 1e-3 for r in out)
    st = ld.residual_stats(out)
    assert st["n_grey"] == 5 and st["grey_de_max"] < 0.05


@pytest.mark.skipif(not PA_ADDITIVITY.exists(), reason="local-only replay script (results/ is gitignored)")
def test_ld_on_law_bookkeeping_equals_pa_additivity_verbatim(tmp_path):
    d, rows = _synthetic_p2(tmp_path, pedestal_nits=0.0, ld_on=True)
    ns, stdout = ld.run_pa_additivity(PA_ADDITIVITY, d)
    ti3 = hc.read_ti3_rows(d / "measurements" / "raw.ti3")              # the same 6-decimal rows the script parses
    mine = ld.additivity_rows([r[0] for r in ti3], [r[1] for r in ti3], transfer="pq", pedestal="ld_on_law")
    theirs = sorted(ns["res"], key=lambda r: tuple(r["code"]))
    mine = sorted(mine, key=lambda r: tuple(r["code"]))
    assert len(mine) == len(theirs) > 0 and "GREYS" in stdout
    for a, b in zip(mine, theirs):
        assert list(a["code"]) == [int(c) for c in b["code"]]
        assert a["de"] == pytest.approx(b["de"], abs=1e-9) and a["yr"] == pytest.approx(b["yr"], abs=1e-12)


def test_residual_trend_slope_sign():
    rows = [{"grey": False, "ratio": r, "de": 1.0 - 2.0 * math.log10(r), "yr": 1.0}   # grows as the ratio drops
            for r in (0.02, 0.05, 0.1, 0.3, 0.6)]
    assert ld.residual_stats(rows)["trend"]["slope_de_per_log10_ratio"] == pytest.approx(-2.0)


# ----------------------------------------------------------------------------- Probe C port / full-stack
def test_probec_levels_equal_the_original_formula():
    from dlc._pq import oetf_norm
    s_lo, s_hi = oetf_norm(100 / 10000.0), oetf_norm(1000 / 10000.0)
    original = sorted({int(round((s_lo + (s_hi - s_lo) * i / 59) * 1023)) for i in range(60)})
    assert fs.probec_codes() == original
    assert fs.probec_codes(desc=True) == original[::-1]
    assert fs.blue_code(0.90, 10) == 921 and fs.blue_code(0.95, 10) == 972


def test_probec_summary_flags_a_sawtooth():
    rows = [{"cv": 500 + i, "Y": 100 + i, "x": 0.3127 + (0.003 if i % 2 else -0.003), "y": 0.329} for i in range(10)]
    s = fs.probec_summary(rows)
    assert s["adj_dx_max"] == pytest.approx(0.006) and s["sign_flips"] == s["of"]


# ----------------------------------------------------------------------------- HW-10 helpers
def test_hw10_compare_touch_and_luminance():
    n = 20
    base = {"enabled": True, "point_count": n, "points": [1.05 * (i / (n - 1)) ** 2 for i in range(n)],
            "deviations": {"r": [1.0] * n, "g": [1.0] * n, "b": [0.99] * n}}
    assert hw10.compare_blocks(base, json.loads(json.dumps(base)))["equal"]
    t, where = hw10.touched(base, 0.002)
    chk = hw10.check_touch(base, t, where)
    assert chk["landed"] and chk["others_unchanged"]
    d = hw10.compare_blocks(base, t)
    assert not d["equal"] and d["diffs"][0]["key"] == "deviations.r"
    assert not hw10.compare_blocks(base, {**base, "enabled": False})["equal"]
    assert not hw10.compare_blocks({**base, "luminance": [1.05] * n}, base)["equal"]
    assert hw10.luminance_component(base, False) == pytest.approx(0.05)
    assert hw10.block_of({"mhc": {"1:SDR": {"applied": True}}}, "1:SDR") is None


# ----------------------------------------------------------------------------- --simulate end to end
def _evidence(stdout: str) -> dict:
    run = Path(json.loads(stdout.strip().splitlines()[-1])["run_dir"])
    return json.loads((run / "evidence.json").read_text(encoding="utf-8")) | {"_run": run}


def test_near_black_native_phases_simulated(capsys):
    assert nb.main(["--phase", "hwA,hwB", "--simulate", "--no-analysis", "--hwb-codes", "2,8,40"]) == 0
    ev = _evidence(capsys.readouterr().out)
    run = ev["_run"]
    assert ev["audits"]["before"]["layers_on"] and not ev["audits"]["after_native"]["layers_on"]
    assert ev["restore"]["unchanged"] and ev["restore"]["snapshot_restore"]["complete"]
    assert (run / "hwA" / "hwA_full.ti3").exists() and (run / "hwA" / "expectations.json").exists()
    assert (run / "hwB" / "hwB.ti3").exists()
    reads = [json.loads(x) for x in (run / "reads.jsonl").read_text(encoding="utf-8").splitlines()]
    assert reads and all({"t", "field_code", "xyz", "shapes_px", "since_paint_s"} <= set(r) for r in reads)
    body = json.loads((run / "hwA" / "hwA.json").read_text(encoding="utf-8"))
    assert all(r["n_kept"] >= 5 for r in body["results"])          # every hwA patch is sub-nit
    events = [json.loads(x) for x in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "check_in" for e in events)


def test_transport_check_refuses_a_wrong_level(capsys):
    assert nb.main(["--phase", "hwA", "--simulate", "--white-nits", "1000"]) == 2   # expects ~218 nit, reads ~23
    ev = _evidence(capsys.readouterr().out)
    assert "transport check" in ev["status"] and ev["restore"]["unchanged"]


def test_near_black_hwC_needs_the_flag_and_restores_the_cube(capsys):
    assert nb.main(["--phase", "hwC", "--simulate"]) == 2
    assert "refused" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"]
    assert nb.main(["--phase", "hwC", "--simulate", "--through-stack", "--hwc-colours", "R,C", "--hwc-codes", "4,8"]) == 0
    ev = _evidence(capsys.readouterr().out)
    assert ev["through_stack"]["approval"].startswith("owner-approved 2026-10-01")
    assert ev["hwC"]["restored_readback"] and ev["hwC"]["sha256_unchanged"] and ev["restore"]["unchanged"]
    body = json.loads((ev["_run"] / "hwC" / "hwC.json").read_text(encoding="utf-8"))
    assert set(body["cube_vs_identity"]["R8"]) == {"cube", "identity"}


def test_ld_additivity_pilot_simulated(tmp_path, capsys):
    sess = tmp_path / "sess"
    base = ["--simulate", "--run", str(sess)]
    assert ld.main(["--phase", "measure"] + base) == 2                 # no frozen prediction yet
    capsys.readouterr()
    sess.mkdir(exist_ok=True)
    (sess / "frozen_prediction.json").write_text(json.dumps({"thresholds": {}, "source_stats": {},
                                                             "analysis_script": str(PA_ADDITIVITY)}), encoding="utf-8")
    import shutil
    shutil.rmtree(sess / "ld_on", ignore_errors=True)
    assert ld.main(["--phase", "measure", "--dimming-speed", "fast", "--phases", "p1,p2"] + base) == 0
    on = _evidence(capsys.readouterr().out)
    assert on["ld"] == "on" and on["dimming_speed"] == "fast" and on["restore"]["unchanged"]
    assert "FALD compensation layer" in on["audits"]["before"]["layers_on"] and not on["audits"]["after_native"]["layers_on"]
    assert (sess / "ld_on" / "p2" / "measurements" / "raw.ti3").exists()
    assert ld.main(["--phase", "measure", "--dimming-speed", "off", "--meter", "1900,1110"] + base) == 2   # placement moved
    capsys.readouterr()
    shutil.rmtree(sess / "ld_off", ignore_errors=True)
    assert ld.main(["--phase", "measure", "--dimming-speed", "off"] + base) == 0
    capsys.readouterr()
    assert ld.main(["--phase", "analyze"] + base) == 0
    an = json.loads((sess / "analysis.json").read_text(encoding="utf-8"))
    assert set(an["states"]) == {"on", "off"} and an["decision"] is None
    assert an["states"]["off"]["measured_black_pedestal"]["grey_de_max"] < 0.5   # the synthetic LD-off panel is additive
    assert an["states"]["off"]["primary_bookkeeping"] == "fitted_constant"         # LD off is judged by the fitted pedestal


def test_full_stack_spots_simulated(capsys):
    assert fs.main(["--simulate", "--phase", "blue"]) == 2
    capsys.readouterr()
    assert fs.main(["--simulate", "--through-stack", "--phase", "blue,probec", "--probec-levels", "8"]) == 0
    ev = _evidence(capsys.readouterr().out)
    assert ev["stack_unchanged"]["unchanged"] and ev["through_stack"]["flag"] == "--through-stack"
    blue = json.loads((ev["_run"] / "blue" / "blue.json").read_text(encoding="utf-8"))
    assert len(blue["presentations"]) == 12 and all(p["reads"] for p in blue["presentations"])
    probec = json.loads((ev["_run"] / "probec" / "probec.json").read_text(encoding="utf-8"))
    assert probec["summary"]["levels"] == 8


def test_hw10_round_trip_simulated(capsys):
    assert hw10.main(["--simulate", "--monitor", "1", "--mode", "SDR", "--also-disabled"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    body = json.loads((Path(out["run_dir"]) / "hw10.json").read_text(encoding="utf-8"))
    assert body["passed"] and [c["case"] for c in body["cases"]] == ["as_found", "enabled_flipped"]
    assert all(isinstance(c["revert_replies"]["mhc_apply"], dict) for c in body["cases"])   # the production revert
    assert all(c["revert_compare"]["equal"] and c["luminance_component"] > 0 for c in body["cases"])
    assert body["final"]["compare"]["equal"]


# ----------------------------------------------------------------------------- fitted constant pedestal (LD-off judge)
def _additive_xyz(rgb_norm, P, prim):
    """A perfectly additive panel with a CONSTANT pedestal P: XYZ = P + Σ_k lin(c_k)·prim_k (black reads P)."""
    import numpy as np
    rgb = np.asarray(rgb_norm, float)
    return np.asarray(P)[None, :] + (rgb ** 2.4) @ np.asarray(prim)


def test_fitted_constant_pedestal_sees_through_an_understated_black():
    """The BenQ trap (2026-10-01): the 0,0,0 read under-states the pedestal the colours sit on (1.7x). On a
    perfectly ADDITIVE panel, measured_black bookkeeping then reports false non-additivity; the fitted constant
    pedestal recovers P from the greys alone, scores the mixes out of sample at ~0, and reports the black
    disagreement as evidence."""
    pytest.importorskip("colour")
    import numpy as np
    a = _ld_args()
    g = ld.geometry(a, 3840, 2160)
    rgb = np.asarray([[c / 1023 for c in p.field] for p in ld.plan_p2(a, g)])
    P = np.array([0.09, 0.092, 0.11])
    prim = np.array([[400.0, 210.0, 15.0], [300.0, 640.0, 90.0], [170.0, 70.0, 900.0]])
    xyz = _additive_xyz(rgb, P, prim)
    xyz[rgb.max(1) <= 0] = P / 1.7                                 # the black frame reads LOW (another backlight state)
    fit = ld.fit_constant_pedestal(rgb, xyz)
    assert np.allclose(fit["P_xyz"], P, rtol=1e-6) and fit["n_codes"] == len(ld.P2_GREYS) + len(ld.P2_PEDESTAL_CODES)
    assert fit["measured_black"]["fitted_over_measured_Y"] == pytest.approx(1.7, rel=1e-3)
    good = ld.residual_stats(ld.additivity_rows(rgb, xyz, transfer="pq", pedestal="fitted_constant"))
    bad = ld.residual_stats(ld.additivity_rows(rgb, xyz, transfer="pq", pedestal="measured_black"))
    assert good["grey_de_max"] < 0.05 and max(b["de_median"] for b in good["bins"]) < 0.05
    assert bad["grey_de_max"] > 5 * good["grey_de_max"] + 0.05     # the false "non-additive" verdict it prevents


def test_fitted_constant_pedestal_still_sees_real_non_additivity():
    """Not a white-wash: a minor-channel deficit in the MIXES (intrinsic non-additivity) survives the fit."""
    pytest.importorskip("colour")
    import numpy as np
    a = _ld_args()
    g = ld.geometry(a, 3840, 2160)
    rgb = np.asarray([[c / 1023 for c in p.field] for p in ld.plan_p2(a, g)])
    P = np.array([0.05, 0.05, 0.06])
    prim = np.array([[400.0, 210.0, 15.0], [300.0, 640.0, 90.0], [170.0, 70.0, 900.0]])
    xyz = _additive_xyz(rgb, P, prim)
    lin = rgb ** 2.4
    mix = ((rgb > 0).sum(1) == 3) & (np.ptp(rgb, 1) > 1e-9)
    minor = np.argmin(np.where(rgb > 0, lin, np.inf), axis=1)
    for i in np.where(mix)[0]:                                     # the minor channel delivers only 60 % in a mix
        xyz[i] -= 0.4 * lin[i, minor[i]] * prim[minor[i]]
    st = ld.residual_stats(ld.additivity_rows(rgb, xyz, transfer="pq", pedestal="fitted_constant"))
    assert st["grey_de_max"] < 0.05                                # greys are additive here ...
    assert max(b["de_median"] for b in st["bins"]) > 1.0           # ... the mixes are not, and it shows


def test_pedestal_set_is_its_own_condition_and_dim():
    a = _ld_args()
    g = ld.geometry(a, 3840, 2160)
    ped = [p for p in ld.plan_p2(a, g) if p.cond == "pedestal"]
    assert len(ped) == 4 * len(ld.P2_PEDESTAL_CODES) and all(p.min_reads >= 5 and p.meta["pedestal_only"] for p in ped)
    assert {p.group for p in ped} == {"ped_grey", "ped_single"}
    assert max(max(p.field) for p in ped) < min(ld.P2_GREYS)        # below every comparison grey


def test_fitted_constant_pedestal_is_inverse_variance_weighted():
    """Bright codes' P_m are differences of four several-hundred-nit reads: with read noise ∝ Y they carry noise as
    large as P. The weighted fit (per-row SE) must beat the plain mean and stay consistent with its own SE."""
    pytest.importorskip("colour")
    import numpy as np
    a = _ld_args()
    g = ld.geometry(a, 3840, 2160)
    rgb = np.asarray([[c / 1023 for c in p.field] for p in ld.plan_p2(a, g)])
    P = np.array([1.8, 1.9, 2.2])                                   # a flat-out LD-off backlight pedestal
    prim = np.array([[400.0, 210.0, 15.0], [300.0, 640.0, 90.0], [170.0, 70.0, 900.0]])
    clean = _additive_xyz(rgb, P, prim)
    sd = np.maximum(0.003 * clean[:, 1], 5e-4)                      # 0.3 % per read, 5 reads per patch
    se = sd / np.sqrt(5)
    errs_w, errs_u = [], []
    for seed in range(40):
        noisy = clean + np.random.default_rng(seed).normal(size=clean.shape) * (se / clean[:, 1].clip(1e-9))[:, None] * clean
        w = ld.fit_constant_pedestal(rgb, noisy, se)
        u = ld.fit_constant_pedestal(rgb, noisy)
        errs_w.append(w["P_xyz"][1] - P[1])
        errs_u.append(u["P_xyz"][1] - P[1])
    rms_w, rms_u = float(np.sqrt(np.mean(np.square(errs_w)))), float(np.sqrt(np.mean(np.square(errs_u))))
    assert rms_w < 0.5 * rms_u                                      # the dim codes carry the estimate
    assert rms_w < 3 * w["P_se_Y"]                                  # and its reported SE is honest
    assert w["weighting"].startswith("inverse-variance") and w["constancy"]["chi2_per_dof"] is not None
    assert all(q["P_se_Y"] > 0 for q in w["per_code"])
