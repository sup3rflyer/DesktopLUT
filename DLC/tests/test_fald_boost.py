"""Black-frame LED boost (work guide "HW 2026-09-18" / P9): the step LUT, the non-black zone count, and how the
forward model and the correction use it (B_true only; off by default and then bit-identical)."""
from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.boost import build_boost_lut, load_boost_lut, load_boost_table  # noqa: E402
from dlc.fald.correct import correct_image, shader_model  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.profile import params_dict, params_from_dict  # noqa: E402

FULL = (0.0, 0.0, 1.0, 1.0)
BLACK = ((0, 0, 0), FULL)
METER = (1943, 1115)
LUT = ((0.0, 1.165), (0.0625, 1.10), (0.1024, 1.0), (0.1111, 1.07), (0.35, 1.0))


def rect(x0, y0, w, h):
    return (x0 / 3840, y0 / 2160, w / 3840, h / 2160)


def window(code=760, size=600):
    return ((code,) * 3, rect(METER[0] - size / 2, METER[1] - size / 2, size, size))


@pytest.fixture(scope="module")
def plain():
    return FaldModel(FaldParams())


@pytest.fixture(scope="module")
def boosted():
    return FaldModel(FaldParams(boost_lut=LUT))


def test_default_has_no_boost_and_is_bit_identical(plain):
    img = plain.render([BLACK, window()])
    assert plain.led_boost(img) == 1.0
    a = plain.forward_img(img)
    b = FaldModel(FaldParams(boost_lut=())).forward_img(img)
    assert np.array_equal(a["y"], b["y"]) and a["boost"] == 1.0


def test_zone_count_follows_the_measured_activation_rules(boosted):
    m = boosted
    n_total = m.p.rows * m.p.cols
    assert m.active_zone_fraction(m.render([BLACK])) == 0.0
    assert m.active_zone_fraction(m.render([((16,) * 3, FULL)])) == 0.0        # PQ10 code 16 surround = black (HW)
    assert m.active_zone_fraction(m.render([((32,) * 3, FULL)])) == 1.0        # code 32 = non-black (HW)
    # the 600-px window at the 2026-09-18 meter spot touches 9 columns x 14 rows
    assert round(m.active_zone_fraction(m.render([BLACK, window()])) * n_total) in range(112, 127)
    # a 0.2-nit top band of ONE zone row adds 48 zones; two-pixel 0.2-nit lines through every zone add none (HW)
    dim = (85, 85, 85)
    base = m.active_zone_fraction(m.render([BLACK, window()]))
    band = m.active_zone_fraction(m.render([BLACK, (dim, rect(0, 0, 3840, 45)), window()]))
    assert round((band - base) * n_total) == 48
    lines = [(dim, rect(0, r * 45 + 21, 3840, 2)) for r in range(48)]
    assert m.active_zone_fraction(m.render([BLACK] + lines + [window()])) == pytest.approx(base, abs=2 / n_total)


def test_step_lookup(boosted):
    f = boosted.boost_of_fraction
    assert f(0.0) == 1.165 and f(0.06) == 1.165 and f(0.0625) == 1.10
    assert f(0.105) == 1.0 and f(0.12) == 1.07 and f(0.9) == 1.0                # the dead band is a step like any other


def test_forward_scales_true_backlight_only(plain, boosted):
    img = plain.render([BLACK, window()])
    a, b = plain.forward_img(img), boosted.forward_img(img)
    assert b["boost"] == 1.165
    assert np.allclose(b["b_true"], 1.165 * a["b_true"]) and np.array_equal(b["b_est"], a["b_est"])
    # light AND pedestal scale (the code-0 leak beside the window read x1.166 / x1.165 on HW)
    assert boosted.meter_y([BLACK, window()], METER) == pytest.approx(1.165 * plain.meter_y([BLACK, window()], METER), rel=1e-9)
    leak = [BLACK, ((760,) * 3, rect(METER[0] + 120, METER[1] - 300, 600, 600))]
    assert boosted.meter_y(leak, METER) == pytest.approx(1.165 * plain.meter_y(leak, METER), rel=1e-9)


def test_a_non_black_frame_is_untouched(plain, boosted):
    frame = [((85,) * 3, FULL), window()]                                        # 0.2-nit surround: every zone counts
    assert boosted.led_boost(boosted.render(frame)) == 1.0
    assert boosted.meter_y(frame, METER) == plain.meter_y(frame, METER)


def test_correction_divides_its_brightening_by_the_boost(plain, boosted):
    img = plain.render([BLACK, window()])
    a, b = correct_image(plain, img), correct_image(boosted, img)
    assert a["boost"] == 1.0 and b["boost"] == 1.165
    mask = plain.aperture_mask(METER)
    ga, gb = float(a["gain"][mask].mean()), float(b["gain"][mask].mean())
    assert gb == pytest.approx(ga / 1.165, rel=0.03)                             # B_est / (boost · B_true), whatever its sign
    # and what the boosted PANEL then shows is closer to the request than with the boost-blind correction
    target = float(img[:, mask].mean(axis=1).sum() / 3.0)
    shown_blind = float(boosted.forward_img(a["req"])["y"][:, mask].mean(axis=1).sum())
    shown_aware = float(boosted.forward_img(b["req"])["y"][:, mask].mean(axis=1).sum())
    assert abs(shown_aware - target) < abs(shown_blind - target)


def test_build_lut_merges_steps_and_clamps_unity():
    pts = [(100, 1.166), (120, 1.164), (150, 1.144), (160, 1.146), (240, 1.001), (250, 0.999), (260, 1.07), (900, 0.998)]
    lut = build_boost_lut(pts, 2304)
    assert [round(b, 3) for _, b in lut] == [1.165, 1.145, 1.0, 1.07, 1.0]
    assert lut[0][0] == 0.0 and lut[1][0] == pytest.approx(135 / 2304) and lut[2][0] == pytest.approx(200 / 2304)
    assert build_boost_lut([], 2304) == ()


def test_table_file_and_params_round_trip(tmp_path):
    path = tmp_path / "boost_table.json"
    path.write_text(json.dumps({"zones_total": 2304, "rows": [{"N": 100, "boost": 1.165}, {"N": 300, "boost": 1.06},
                                                                {"N": 301, "boost": 3.0, "exclude": True},
                                                                {"N": 1000, "boost": 1.0}]}))
    lut = load_boost_lut(path)
    assert [round(b, 3) for _, b in lut] == [1.165, 1.06, 1.0]
    p = replace(FaldParams(), boost_lut=lut, boost_lit_nits=0.7, boost_dim_frac=0.25)
    q = params_from_dict(json.loads(json.dumps(params_dict(p))))
    assert q.boost_lut == lut and q.boost_lit_nits == 0.7 and q.boost_dim_frac == 0.25
    assert params_from_dict({"boost_lut": []}).boost_lut == ()


def test_table_loader_checks_mode_and_lattice(tmp_path):
    path = tmp_path / "boost_table.json"
    path.write_text(json.dumps({"mode": "HDR", "zones_total": 2304, "boost_lit_nits": 0.6, "boost_dim_frac": 0.2,
                                "rows": [{"N": 100, "boost": 1.165}, {"N": 1000, "boost": 1.0}]}))
    kw = load_boost_table(path, mode="HDR", zones_total=2304)
    assert kw["boost_lit_nits"] == 0.6 and kw["boost_dim_frac"] == 0.2 and len(kw["boost_lut"]) == 2
    with pytest.raises(ValueError, match="measured in HDR"):
        load_boost_table(path, mode="SDR", zones_total=2304)
    with pytest.raises(ValueError, match="zones"):
        load_boost_table(path, mode="HDR", zones_total=1152)


def test_activation_rule_on_the_discriminating_reads(boosted):
    """The reads that pin the statistic (review 2026-09-18), at the model's raster: a 3-px 10-nit column activates its
    zones, a 10-px 0.2-nit sliver of an 80-px zone and a code-16 field do not, an 11-px 0.2-nit band does."""
    m = boosted
    n = m.p.rows * m.p.cols
    count = lambda shapes: round(m.active_zone_fraction(m.render(shapes)) * n)
    dim, ten = (85,) * 3, (307,) * 3
    assert count([BLACK, (ten, rect(2400, 0, 3, 2160))]) == 48
    assert count([BLACK, (ten, rect(2400, 0, 1, 2160))]) == 48                      # r9d: a 1-px 10-nit column
    assert count([BLACK, ((153,) * 3, rect(2400, 0, 2, 2160))]) == 48             # r9d: 2 px at 1 nit (PQ10 153)
    assert count([BLACK, (dim, rect(640, 0, 12, 2160))]) == 0 and count([BLACK, (dim, rect(640, 0, 24, 2160))]) == 48
    assert count([BLACK, (dim, rect(640, 0, 10, 2160))]) == 0
    assert count([BLACK, (dim, rect(0, 0, 3840, 11))]) == 48
    assert count([((16,) * 3, FULL)]) == 0 and count([((32,) * 3, FULL)]) == n


def test_shader_model_is_boost_blind_until_the_file_carries_it(plain, boosted):
    assert shader_model(plain) is plain
    sm = shader_model(boosted)
    assert sm.p.boost_lut == () and shader_model(boosted) is sm                  # cached
    img = plain.render([BLACK, window()])
    assert np.array_equal(correct_image(sm, img)["req"], correct_image(plain, img)["req"])
