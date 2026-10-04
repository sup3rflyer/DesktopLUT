"""The offline motion simulator (dlc.fald.motion): exact coverage, the full-resolution zone peak, the panel-truth
statistics, streaming runs, the camera aids; and the TPG client's present-log bookkeeping (dlc.fald.motion_tpg)."""
import math
from dataclasses import replace

import numpy as np
import pytest

from dlc.fald.model import FaldModel, FaldParams
from dlc.fald.motion import (MotionModel, MovingShape, Scene, coverage_disc, coverage_rect, grey, render_full_patch,
                             render_reduced, score, simulate_scene, static_mask)
from dlc.fald.motion_tpg import Present, infer_refreshes, presented_schedule, scene_text
from dlc.fald.paneltime import PanelDriveState, PanelTimeLaw
from dlc.fald.temporal import MODE_OFF, DriveState

# a 12 x 12-zone panel with the PA32UCXR's 80 x 45-px zones (fast; raster 192 x 108 at scale 5)
P = FaldParams(width=960, height=540, cols=12, rows=12)


def _bar(x=400.3, vx=0.0, nits=1000.0, w=40.0, h=135.0, y=270.0):
    return MovingShape("rect", x, y, grey(nits), w=w, h=h, vx=vx)


def test_rect_coverage_is_the_exact_area():
    s = _bar(x=123.37, w=40.6, h=70.2, y=88.81)
    cov = coverage_rect(s, 0.0, 0, 0, 300, 200)
    assert cov.sum() == pytest.approx(40.6 * 70.2, rel=1e-12)
    assert cov.max() == 1.0 and cov.min() == 0.0


def test_disc_coverage_area_and_rim():
    s = MovingShape("disc", 150.4, 100.7, grey(1.0), r=33.3)
    cov = coverage_disc(s, 0.0, 0, 0, 300, 200)
    assert cov.sum() == pytest.approx(math.pi * 33.3 ** 2, rel=2e-3)
    assert set(np.unique(cov[cov > 0])) - {1.0}                     # anti-aliased rim exists


def test_reduced_render_conserves_light_and_keeps_the_full_res_peak():
    # right edge 0.5 px into the next full-res pixel column: the reduced pixel there averages, submax keeps the level
    sc = Scene("t", grey(5.0), (_bar(x=420.0 - 20.0 + 0.5, w=40.0),))     # spans 380.5 .. 420.5
    img, pk = render_reduced(sc, 0, P.scale, P.width, P.height)
    full = render_full_patch(sc, 0, 0, 0, P.width, P.height)
    assert img.sum() * P.scale ** 2 == pytest.approx(full.sum(), rel=1e-12)
    col = 420 // P.scale                                                   # reduced column holding x 420 .. 425
    row = 270 // P.scale
    assert img[0, row, col] == pytest.approx(5.0 + 995.0 * 0.5 / P.scale)
    assert pk[:, row, col] == pytest.approx([5.0 + 995.0 * 0.5] * 3)


def test_motion_model_without_context_is_the_plain_model():
    sc = Scene("t", grey(5.0), (_bar(),))
    img, _ = render_reduced(sc, 0, P.scale, P.width, P.height)
    a = FaldModel(P).cell_drives(img)
    b = MotionModel(P, "area").cell_drives(img)
    assert np.array_equal(a, b)


def test_level_truth_drives_a_zone_on_one_pixel_column_area_truth_by_area():
    # one full-res column (x 480 .. 481) inside zone column 6 (480 .. 560), the bar's body in zone column 5
    sc = Scene("t", grey(5.0), (_bar(x=481.0 - 20.0, w=40.0),))           # spans 441 .. 481
    img, pk = render_reduced(sc, 0, P.scale, P.width, P.height)
    lv, ar = MotionModel(P, "level"), MotionModel(P, "area")
    for m in (lv, ar):
        m.set_peak(pk)
    r = 270 // 45
    d_level, d_area = lv.cell_drives(img)[r, 6], ar.cell_drives(img)[r, 6]
    assert d_level == pytest.approx(float(lv.drive_of(np.array([1000.0]))[0]))
    tot = 5.0 * 45 * 80 + 995.0 * 45 * 1                                    # the zone's lit sum (bg counts: 5 nit > floor)
    assert d_area == pytest.approx(float(ar.drive_of(np.array([min(1000.0, tot / P.stat_area0_px2)]))[0]), rel=1e-9)
    assert d_level > 2 * d_area


def test_scene_round_trip_text_and_gray_code():
    sc = Scene("t", grey(5.0), (_bar(vx=4.0), MovingShape("disc", 700.0, 300.0, grey(200.0), r=30.0, vx=-2.0)),
               pre=3, move=10, post=2, cadence=(3, 2), sync=(50, 250, 60, 60, 10, 30), code=(40, 480, 20, 8, 2, 12))
    assert Scene.from_dict(sc.to_dict()) == sc
    txt = scene_text(sc)
    for key in ("name t", "pre 3", "move 10", "post 2", "cadence 3 2", "rect ", "disc ", "sync ", "code "):
        assert key in txt
    codes = [tuple(v for *_, v in sc.aid_rects(i)[1:]) for i in range(sc.frames)]
    for a, b in zip(codes, codes[1:]):
        assert sum(x != y for x, y in zip(a, b)) == 1                      # Gray code: one cell per new frame
    assert sc.aid_rects(sc.pre - 1)[0][4] == 10 and sc.aid_rects(sc.pre)[0][4] == 30
    assert sc.aid_rects(sc.pre + sc.move)[0][4] == 10


@pytest.mark.parametrize("sync", [(380.0, 250.0, 40.0, 40.0, 10.0, 30.0), (52.0, 61.0, 13.0, 9.0, 2.0, 30.0)])
def test_reduced_render_is_exact_with_aids_anywhere(sync):
    # an aid over the moving shape, aids sharing blocks, an aid DARKER than the background: block mean and block peak
    # must equal the full-resolution truth
    sc = Scene("t", grey(5.0), (_bar(x=400.3, vx=3.3),), pre=1, move=4, post=1, sync=sync,
               code=(41.0, 480.0, 7.0, 6, 2.0, 12.0), digits=(100.0, 470.0, 24.0, 3, 2.0, 12.0))
    for i in (0, 2, sc.frames - 1):
        img, pk = render_reduced(sc, i, P.scale, P.width, P.height)
        full = render_full_patch(sc, i, 0, 0, P.width, P.height)
        blocks = full.reshape(3, img.shape[1], P.scale, img.shape[2], P.scale)
        assert np.allclose(img, blocks.mean(axis=(2, 4)), rtol=1e-12, atol=1e-12)
        assert np.allclose(pk.max(axis=0), blocks.max(axis=0).max(axis=(1, 3)), rtol=1e-12, atol=1e-12)


def test_correct_image_peak_companion_follows_the_knee_per_pixel():
    from dlc.fald.correct import correct_image
    sc = Scene("t", grey(5.0), (_bar(x=421.7, w=40.0, nits=1500.0),))
    img, pk = render_reduced(sc, 0, P.scale, P.width, P.height)
    m = MotionModel(P, "area")
    plain = correct_image(FaldModel(P), img)
    same = correct_image(m, img, peak=img)                                 # a raster without sub-pixel content
    assert np.array_equal(same["req"], plain["req"]) and np.array_equal(same["req_peak"], plain["req"])
    res = correct_image(m, img, peak=pk)
    # where a block is uniform its peak request IS its request (same fields, same rule); edge blocks carry their own
    # level through the rule (with the owner's HDR fit the knee binds there — review 2026-10-04: bar_v4 edge block mean
    # 234 / max 1000 nit, gain 1.59, ceiling 517: the shortcut peak·req/mean said 1594, the rule gives 1000)
    uni = np.isclose(pk.max(axis=0), img.max(axis=0))
    assert (~uni).any()
    assert np.allclose(res["req_peak"][:, uni], res["req"][:, uni])
    assert np.isfinite(res["req_peak"]).all() and (res["req_peak"] >= 0).all()


def test_score_refuses_a_scene_without_a_rest_frame():
    sc = Scene("t", grey(5.0), (_bar(vx=8.0),), pre=0, move=4, post=2)
    sr = simulate_scene(P, sc, None, panels=(("area", 0),))
    with pytest.raises(ValueError, match="pre >= 1"):
        score(sr.runs[0], sr)


@pytest.mark.parametrize("state", [None, "static"])
def test_a_scene_without_motion_shows_a_constant_frame(state):
    sc = Scene("still", grey(5.0), (_bar(),), pre=2, move=6, post=4)
    st = {None: None, "static": DriveState(MODE_OFF)}[state]
    sr = simulate_scene(P, sc, st, panels=(("area", 0), ("level", 1)))
    for run in sr.runs:
        ys = run.ys.astype(np.float64)
        assert np.allclose(ys, ys[0], rtol=1e-5, atol=1e-6)
        s = score(run, sr)
        assert s["step_max"] < 1e-3 and s["obj_pulse"] < 1e-3


def test_the_clock_state_keeps_iterating_the_inverse_on_a_still_frame_and_settles():
    # a stateful layer takes one Picard step of the inverse per refresh on unchanged content (fald-work-guide item 4a,
    # "side effect worth knowing"); these DEFAULT params ring while doing it (the owner's HDR fit: <= 0.6 % on one
    # refresh, results/fald_motion_2026-10-04) — what must hold is that it converges
    sc = Scene("still", grey(5.0), (_bar(),), pre=2, move=1, post=40)
    sr = simulate_scene(P, sc, PanelDriveState(PanelTimeLaw(), 0), panels=(("area", 0),))
    d = np.abs(np.diff(np.log(sr.runs[0].ys.astype(np.float64)), axis=0)).max(axis=1)
    assert d[-4:].max() < 0.05 * d.max()


def test_a_moving_bar_flashes_the_static_grey_and_the_measured_parity_helps():
    sc = Scene("move", grey(5.0), (_bar(x=420.3, vx=8.0),), pre=6, move=20, post=10)
    native = simulate_scene(P, sc, None, panels=(("area", 0),))
    known = simulate_scene(P, sc, PanelDriveState(PanelTimeLaw(), 0), panels=(("area", 0),))
    s_n, s_k = score(native.runs[0], native), score(known.runs[0], known)
    assert s_n["step_max"] > 10.0                                          # zone transitions show natively
    assert s_k["step_p99"] < s_n["step_p99"]                               # the matched clock removes most of them
    m = static_mask(MotionModel(P), sc)
    assert m.sum() == native.mask.sum() > 0


def _presents(refreshes, intervals):
    return [Present(i + 1, 0, 0, 1, 0, i, 0, iv, i + 1, r) for i, (r, iv) in enumerate(zip(refreshes, intervals))]


def test_present_schedule_infers_only_unambiguous_gaps_and_finds_slips():
    pres = _presents([100, None, None, 103, None, 108], [1, 1, 1, 2, 2, 1])
    assert infer_refreshes(pres) == 2                                      # 100 -> 103 = 1 + 1 + 1: filled
    assert [p.refresh for p in pres[:4]] == [100, 101, 102, 103]
    assert pres[1].inferred and not pres[3].inferred
    assert pres[4].refresh is None                                         # 103 -> 108 = 5 != 2 + 2: a slip inside, left open
    sched = presented_schedule(pres)
    assert sched["reported"] == pytest.approx(3 / 6) and sched["resolved"] == pytest.approx(5 / 6)
    assert sched["slips"] == []                                            # nothing PROVEN: the gap's slip has no owner

    pres = _presents([200, 201, 203, 204, 205], [1, 1, 1, 1, 1])
    sched = presented_schedule(pres)
    assert [row[4] for row in sched["presents"]] == [1, 2, 1, 1, None]
    assert sched["slips"] == [(2, 1, 1, 2)]                                # present 2 (content 1) stayed two refreshes
