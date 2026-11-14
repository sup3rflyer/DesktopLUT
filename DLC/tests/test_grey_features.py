"""Narrow grey features and the SDR base 1D LUT (dlc.grey_features + mhc_cube.build_sdr_cube).

A simulated additive panel with per-channel tone drift and, optionally, a NARROW native dip (a
raised-cosine notch in every channel's drive response, ~9 codes wide, ~2.7 % in light — the PA32UCXR
SDR case of 2026-11-14). The raw grey ramp is the production one (``build_ramp_set`` greys). The pipeline
under test is the build's own: judge the raw ramp (``model_grey_samples``) -> plan the densify pass ->
read it from the panel (with the anchors re-read) -> fold it in (``merge_dense_greys``) -> judge again ->
``build_sdr_cube``. The LUT is then evaluated THROUGH the panel at every code: light vs the γ target and
the channel balance (equal linear shares = native white, which is what the base targets).
"""

from __future__ import annotations

import math

import pytest

from dlc import grey_features as gf
from dlc import mhc_cube as mc
from dlc.colormath import matvec, rgb_to_xyz_matrix
from dlc.engine.patches import Transfer
from dlc.mhc import Ti3Sample
from dlc.patch_sets import PatchSizes, build_ramp_set

_PRIM = {"rx": 0.6960, "ry": 0.3040, "gx": 0.1818, "gy": 0.7500, "bx": 0.1512, "by": 0.0648}
_WHITE = (0.3134, 0.3268)
_PEAK = 121.0
_GAMMA = 2.2
_MAX_CV = 1023
_P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"], _PRIM["bx"], _PRIM["by"],
                       _WHITE[0], _WHITE[1], white_Y=_PEAK)


def _eotf(s: float) -> float:
    return s ** _GAMMA


class SimPanel:
    """Per-channel drive -> linear share, additive through ``_P``. Smooth per-channel tone drift (red
    a little high, blue a little low in the shadows, like the PA32UCXR), an optional narrow dip in every
    channel's response centred on ``dip_code`` (raised cosine, full width ``dip_width`` codes, depth
    ``dip_depth`` in light), and optional one-off glitches ``{code: factor}`` on the FIRST read of a grey."""

    def __init__(self, *, dip_code=None, dip_depth=0.027, dip_width=9.0, glitch=None):
        self.dip_code, self.dip_depth, self.dip_width = dip_code, dip_depth, dip_width
        self.glitch = dict(glitch or {})

    def _dip(self, d: float) -> float:
        if self.dip_code is None:
            return 1.0
        u = (d * _MAX_CV - self.dip_code) / (self.dip_width / 2.0)
        return 1.0 - self.dip_depth * 0.5 * (1.0 + math.cos(math.pi * u)) if abs(u) < 1.0 else 1.0

    def shares(self, drive) -> list[float]:
        tilt = (1.02, 1.0, 0.99)
        out = []
        for c, d in enumerate(drive):
            d = min(max(d, 0.0), 1.0)
            k = 1.0 + (tilt[c] - 1.0) * (1.0 - d)          # drift vanishes at full drive (white = native)
            out.append((d ** _GAMMA) * k * self._dip(d))
        return out

    def xyz(self, drive) -> tuple[float, float, float]:
        return tuple(matvec(_P, self.shares(drive)))

    def read_grey(self, code: int) -> Ti3Sample:
        s = code / _MAX_CV
        xyz = self.xyz((s, s, s))
        if code in self.glitch:
            f = self.glitch.pop(code)                       # a transient: the first read only
            xyz = tuple(v * f for v in xyz)
        return Ti3Sample(rgb=(s, s, s), xyz=xyz)


def _raw_codes() -> list[int]:
    t = Transfer.power(gamma=_GAMMA, peak_nits=_PEAK, bit_depth=10)
    return sorted({p[0] for p in build_ramp_set(PatchSizes(), t) if p[0] == p[1] == p[2]})


def _build(panel: SimPanel, *, densify: bool = True, max_reads: int = 96):
    raw = [panel.read_grey(c) for c in _raw_codes()]
    sigma = 1e-3                                            # a measured-class repeatability (0.1 %)
    kw = dict(eotf=_eotf, max_cv=_MAX_CV, y_floor=0.1, luminance_sigma=sigma)
    model, report = gf.model_grey_samples(raw, _PRIM, _WHITE, _PEAK, **kw)
    info = None
    if densify:
        plan = gf.densification_plan(report, max_cv=_MAX_CV, existing_codes=_raw_codes(),
                                     max_reads=max_reads)
        if plan["codes"] or plan["anchors"]:
            seq = plan["anchors"] + plan["codes"] + plan["anchors"]
            dense = [panel.read_grey(c) for c in seq]
            merged, info = gf.merge_dense_greys(raw, dense, max_cv=_MAX_CV, luminance_sigma=sigma)
            model, report = gf.model_grey_samples(merged, _PRIM, _WHITE, _PEAK, **kw)
    curves, _ = mc.build_sdr_cube(model, _PRIM, _WHITE, _PEAK, gamma=_GAMMA, dark_floor_nits=0.1)
    return curves, report, info, raw


def _through(panel: SimPanel, curves, code: int) -> tuple[float, float, float]:
    """(light / target - 1, r/g - 1, b/g - 1) of wire grey ``code`` through the LUT (identity matrix)."""
    s = code / _MAX_CV
    n = len(curves["r"])
    drive = []
    for ch in "rgb":
        x = s * (n - 1)
        j = min(int(x), n - 2)
        drive.append(curves[ch][j] + (curves[ch][j + 1] - curves[ch][j]) * (x - j))
    sh = panel.shares(drive)
    y = matvec(_P, sh)[1]
    return y / (_PEAK * _eotf(s)) - 1.0, sh[0] / sh[1] - 1.0, sh[2] / sh[1] - 1.0


def _old_build(raw):
    curves, _ = mc.build_sdr_cube(raw, _PRIM, _WHITE, _PEAK, gamma=_GAMMA, dark_floor_nits=0.1)
    return curves


# --- smooth panel: no feature, bit-identical LUT ------------------------------------------------

def test_smooth_panel_flags_nothing_and_builds_the_identical_lut():
    panel = SimPanel()
    curves, report, info, raw = _build(panel)
    assert report["features"] == [] and info is None            # nothing flagged, nothing re-measured
    old = _old_build(raw)
    for ch in "rgb":
        assert curves[ch] == old[ch]                            # bit-identical, not merely close


def test_model_passes_unflagged_samples_through_as_the_same_objects():
    raw = [SimPanel().read_grey(c) for c in _raw_codes()]
    model, report = gf.model_grey_samples(raw, _PRIM, _WHITE, _PEAK, eotf=_eotf, max_cv=_MAX_CV,
                                          y_floor=0.1, luminance_sigma=1e-3)
    assert not report["features"]
    assert all(a is b for a, b in zip(model, raw))


# --- a narrow native dip ON a raw grey ----------------------------------------------------------

@pytest.mark.parametrize("dip_code", [165, 157])
def test_narrow_dip_is_densified_and_corrected_inside_and_beside(dip_code):
    panel = SimPanel(dip_code=dip_code)
    curves, report, info, raw = _build(panel)
    assert info is not None and info["added_levels"] >= 8, info     # the span was measured densely
    assert dip_code in info["confirmed_levels"]                     # the re-read reproduced it: real
    inside = range(dip_code - 4, dip_code + 5)
    beside = [c for c in range(dip_code - 30, dip_code + 31) if abs(c - dip_code) >= 6]
    for c in inside:
        dl, rg, bg = _through(panel, curves, c)
        assert abs(dl) < 0.003, (c, dl)                              # light within 0.3 % (was -2.7 %)
        # balance inside a 9-code notch: the channels are driven ~1 code apart there, so a 2-code
        # sampled notch leaves a few tenths of a percent (the old build: ~1 %)
        assert abs(rg) < 0.004 and abs(bg) < 0.004, (c, rg, bg)
    for c in beside:
        dl, rg, bg = _through(panel, curves, c)
        assert abs(dl) < 0.001, (c, dl)                              # beside: on target
        assert abs(rg) < 0.001 and abs(bg) < 0.001, (c, rg, bg)
    # the OLD build (piecewise-linear through the sparse raw greys) spreads the dip's correction over
    # the neighbouring segments: well off target beside the dip.
    old = _old_build(raw)
    worst_old = max(abs(_through(panel, old, c)[0]) for c in beside)
    assert worst_old > 0.008, worst_old


def test_narrow_dip_without_a_densify_pass_falls_back_to_the_envelope():
    # Budget 0 (or a flow that cannot measure more): the feature is built from its neighbours'
    # envelope — never a correction shaped by the spacing. The dip itself stays uncorrected (at most
    # its own depth), and nothing beside it is pushed off target.
    panel = SimPanel(dip_code=165)
    curves, report, info, raw = _build(panel, densify=False)
    assert info is None and any(abs(f["code"] - 165) < 0.6 for f in report["features"])
    beside = [c for c in range(132, 199) if abs(c - 165) >= 6]
    for c in beside:
        dl, rg, bg = _through(panel, curves, c)
        assert abs(dl) < 0.001, (c, dl)
        assert abs(rg) < 0.0005 and abs(bg) < 0.0005, (c, rg, bg)
    for c in range(161, 170):
        dl, _rg, _bg = _through(panel, curves, c)
        assert -0.0285 < dl < 0.003, (c, dl)                        # never over-corrected


# --- an outlier read must not produce a bump ----------------------------------------------------

def test_a_one_off_outlier_read_produces_no_bump():
    panel = SimPanel(glitch={165: 0.955})                       # one transient -4.5 % read at 165
    curves, report, info, raw = _build(panel)
    assert info is not None and 165 in info["unstable_levels"]   # the re-read did not reproduce it
    for c in range(120, 220):
        dl, rg, bg = _through(panel, curves, c)
        assert abs(dl) < 0.001, (c, dl)
        assert abs(rg) < 0.0005 and abs(bg) < 0.0005, (c, rg, bg)
    # the old build turns that single read into a drive bump (the 2026-09-25 shape)
    old = _old_build(raw)
    assert max(abs(_through(SimPanel(), old, c)[0]) for c in range(140, 170)) > 0.02


def test_outlier_without_densify_is_enveloped_not_baked():
    panel = SimPanel(glitch={165: 0.955})
    curves, report, info, raw = _build(panel, densify=False)
    assert [round(f["code"]) for f in report["features"]] == [165]
    for c in range(120, 220):
        dl, rg, bg = _through(SimPanel(), curves, c)
        assert abs(dl) < 0.001, (c, dl)
        assert abs(rg) < 0.0005 and abs(bg) < 0.0005, (c, rg, bg)


def test_build_inverts_in_the_transfer_domain_between_greys():
    # A smooth panel (per-channel tone drift, no feature): between two raw greys the base LUT lands on
    # target. The pre-2026-11-14 build inverted the shares in LINEAR light, which over-states every
    # convex segment's middle: about -1 % in light midway between two 33-code greys.
    panel = SimPanel()
    curves, *_ = _build(panel, densify=False)
    # above the dark blend (the build blends to identity below 2x the dark-floor signal, code ~81 here)
    worst = max(abs(_through(panel, curves, c)[0]) for c in range(90, 1000))
    assert worst < 0.0005, worst


# --- light structure finer than the sampling (the PA32UCXR sawtooth, dense read 2026-11-14) --------

class SawtoothPanel(SimPanel):
    """Full-field light is a sawtooth in the code (a backlight step every ``period`` codes, the LCD
    compensating imperfectly): every channel's light x (1 + depth * (frac(code / period) - 0.5)), the
    step set by the MEAN drive (one backlight for the field), chromaticity untouched."""

    def __init__(self, *, period=9.0, depth=0.03, **kw):
        super().__init__(**kw)
        self.period, self.depth = period, depth

    def shares(self, drive) -> list[float]:
        base = super().shares(drive)
        m = sum(min(max(d, 0.0), 1.0) for d in drive) / 3.0 * _MAX_CV
        f = 1.0 + self.depth * ((m / self.period) % 1.0 - 0.5)
        return [v * f for v in base]


def test_sawtooth_light_is_smoothed_not_chased_and_balance_stays_neutral():
    panel = SawtoothPanel()
    raw = [panel.read_grey(c) for c in _raw_codes()]
    curves, report, info, _raw = _build(panel)
    assert report["light_smoothing"]["applied"], report["light_smoothing"]
    old = _old_build(raw)
    codes = range(100, 400)
    new_err = [_through(panel, curves, c) for c in codes]
    old_err = [_through(panel, old, c) for c in codes]

    def rms(v):
        return math.sqrt(sum(x * x for x in v) / len(v))

    # light: never worse than following each read's phase (the teeth themselves stay: +-1.5 %)
    assert rms([e[0] for e in new_err]) <= rms([e[0] for e in old_err]) + 1e-4
    assert max(abs(e[0]) for e in new_err) < 0.025
    # balance: a smooth light correction cannot tint (the old one tints where two reads sit on
    # opposite phases close together)
    assert max(max(abs(e[1]), abs(e[2])) for e in new_err) < 0.0015
    assert max(max(abs(e[1]), abs(e[2])) for e in new_err) <= max(max(abs(e[1]), abs(e[2])) for e in old_err)


def test_smooth_panel_is_not_light_smoothed():
    _curves, report, _info, _raw = _build(SimPanel())
    assert report["light_smoothing"]["applied"] is False


# --- the pieces ---------------------------------------------------------------------------------

def test_flag_series_finds_two_adjacent_features_without_cascade():
    xs = [i / 40 for i in range(1, 40)]
    vs = [0.01 * math.sin(3 * x) for x in xs]                 # smooth
    vs[20] += 0.05
    vs[21] -= 0.05                                           # two adjacent, opposite features
    out = gf.flag_series(xs, vs, [1e-3] * len(xs), [True] * len(xs))
    assert [r["index"] for r in out if r["flagged"]] == [20, 21]


def test_resolution_grows_with_matrix_registration_and_channel_imbalance():
    base = gf.resolution_codes(0.15, (1.0, 1.0, 1.0), max_cv=_MAX_CV, eotf=_eotf)
    assert math.isclose(base, gf.MIN_RESOLUTION_CODES)
    reg = gf.resolution_codes(0.15, (1.0, 1.0, 1.0), max_cv=_MAX_CV, eotf=_eotf,
                              index_scale=lambda c, s: s * (0.9945, 1.00293, 0.99448)[c])
    assert reg > base + 1.0                                  # PA32UCXR rowsums: ~1.3 codes apart at 153
    imb = gf.resolution_codes(0.15, (1.02, 1.0, 1.0), max_cv=_MAX_CV, eotf=_eotf)
    assert imb > base + 1.0


def test_densification_plan_merges_spans_and_respects_the_budget():
    report = {"unresolved": [
        {"signal": 157 / _MAX_CV, "left_signal": 132 / _MAX_CV, "right_signal": 165 / _MAX_CV,
         "resolution_codes": 3.0},
        {"signal": 165 / _MAX_CV, "left_signal": 157 / _MAX_CV, "right_signal": 198 / _MAX_CV,
         "resolution_codes": 4.0}]}
    plan = gf.densification_plan(report, max_cv=_MAX_CV, existing_codes=[132, 157, 165, 198], max_reads=200)
    assert len(plan["spans"]) == 1 and plan["spans"][0]["lo"] == 132 and plan["spans"][0]["hi"] == 198
    assert plan["anchors"] == [132, 157, 165, 198] and plan["coarsened"] == 1.0
    assert all(132 < c < 198 for c in plan["codes"])
    small = gf.densification_plan(report, max_cv=_MAX_CV, existing_codes=[], max_reads=12)
    assert small["reads"] <= 12 and small["coarsened"] > 1.0
    assert gf.densification_plan(report, max_cv=_MAX_CV, existing_codes=[], max_reads=0)["reads"] == 0


def test_merge_scales_the_dense_pass_onto_the_raw_pass():
    raw = [SimPanel().read_grey(c) for c in (132, 165, 198)]
    brighter = [Ti3Sample(rgb=s.rgb, xyz=tuple(v * 1.01 for v in s.xyz))   # the panel drifted +1 %
                for s in (SimPanel().read_grey(c) for c in (132, 150, 165, 198))]
    merged, info = gf.merge_dense_greys(raw, brighter, max_cv=_MAX_CV, luminance_sigma=1e-3)
    assert math.isclose(info["scale_k"], 1 / 1.01, rel_tol=1e-6)
    added = [s for s in merged if round(s.rgb[0] * _MAX_CV) == 150]
    assert len(added) == 1
    assert math.isclose(added[0].xyz[1], SimPanel().read_grey(150).xyz[1], rel_tol=1e-9)
    assert info["unstable_levels"] == [] and info["confirmed_levels"] == [132, 165, 198]


def test_luminance_repeatability_from_the_measure_log():
    rows = [
        {"phase": "main", "rgb": [100, 100, 100], "read_index": 0, "nits": 1.000, "label": "a"},
        {"phase": "main", "rgb": [100, 100, 100], "read_index": 1, "nits": 1.002, "label": "a"},
        {"phase": "main", "rgb": [200, 200, 200], "read_index": 0, "nits": 4.000, "label": "b"},
        {"phase": "main", "rgb": [200, 200, 200], "read_index": 1, "nits": 3.996, "label": "b"},
    ] + [{"phase": "main", "rgb": [512, 512, 532], "role": "neutral_ref", "nits": v}
         for v in (26.25, 26.28, 26.28, 26.28)]
    rep = gf.luminance_repeatability(rows)
    assert rep["pairs"] == 2 and rep["refs"] == 4
    assert 1e-4 < rep["sigma"] < 5e-3
    assert gf.luminance_repeatability([]) is None


def test_held_out_check_flags_a_narrow_error_between_fit_levels():
    panel = SimPanel()
    report = {"features": [], "fit_codes": _raw_codes()}
    points = gf.held_out_codes(report["fit_codes"], report, max_cv=_MAX_CV, floor_code=60, uniform=6,
                               max_points=6)
    assert points and all(p["lo"] < p["code"] < p["hi"] for p in points)
    reads = {c: panel.xyz((c / _MAX_CV,) * 3) for p in points for c in (p["code"], p["lo"], p["hi"])}
    kw = dict(max_cv=_MAX_CV, eotf=_eotf, primaries=_PRIM, white_xy=_WHITE, peak_luminance=_PEAK,
              luminance_sigma=1e-3, structure_pct={"luminance": 0.1, "balance_rg": 0.05, "balance_bg": 0.05})
    clean = gf.held_out_check(points, reads, **kw)
    assert clean["out"] == [], clean
    bad = dict(reads)
    c0 = points[0]["code"]
    bad[c0] = tuple(v * 1.022 for v in reads[c0])                  # +2.2 % at a held-out grey
    res = gf.held_out_check(points, bad, **kw)
    assert [r["code"] for r in res["out"]] == [c0] and "luminance" in res["out"][0]["missed"]


# --- repair of an INSTALLED base from a dense neutral audit (dlc.base_repair) ------------------

def _audit_reads(panel: SimPanel, curves, codes) -> dict:
    out = {}
    n = len(curves["r"])
    for c in codes:
        s = c / _MAX_CV
        drive = []
        for ch in "rgb":
            x = s * (n - 1)
            j = min(int(x), n - 2)
            drive.append(curves[ch][j] + (curves[ch][j + 1] - curves[ch][j]) * (x - j))
        out[c] = panel.xyz(drive)
    return out


_REPAIR_KW = dict(primaries=_PRIM, native_white_xy=_WHITE, peak_luminance=_PEAK, max_cv=_MAX_CV, eotf=_eotf)


def test_repair_removes_an_installed_bump_and_leaves_everything_else_bit_identical():
    from dlc import base_repair as br

    panel = SimPanel()
    installed = _old_build([SimPanel(glitch={165: 0.955}).read_grey(c) for c in _raw_codes()])
    worst_before = max(abs(_through(panel, installed, c)[0]) for c in range(140, 200))
    assert worst_before > 0.02                                   # the bump is there
    # the repair flow's DENSE stage (every code across the coarse audit's suspect region; the fit
    # half is then every other code — a 2-code fit spacing, the LUT's own resolution)
    plan = br.audit_plan(100, 300, step=1)
    fixed, spans = installed, []
    for _round in range(3):                                      # audit -> repair, as the flow loops
        reads = _audit_reads(panel, fixed, plan["codes"])
        if not br.judge_after(reads, plan, luminance_sigma=1e-3, **_REPAIR_KW)["out"] and _round:
            break
        fixed, report = br.repair(fixed, reads, plan, oetf=lambda v: v ** (1 / _GAMMA),
                                  index_scale=lambda c, s: s, luminance_sigma=1e-3, **_REPAIR_KW)
        spans += [s["codes"] for s in report["spans"]]
    assert _round <= 2 and spans
    lo, hi = min(s[0] for s in spans), max(s[1] for s in spans)
    assert lo <= 160 and hi >= 190 and hi - lo < 120, spans
    for c in range(102, 299):
        dl, rg, bg = _through(panel, fixed, c)
        # within the repair's own acceptance (z·σ·√2 = 0.57 % at σ 0.1 %); the bump was +4.4 %
        assert abs(dl) < 0.0057 and abs(rg) < 0.0057 and abs(bg) < 0.0057, (c, dl, rg, bg)
    for ch in "rgb":                                             # outside the span: untouched, exactly
        for j in list(range(0, lo - 1)) + list(range(hi + 2, 1024)):
            assert fixed[ch][j] == installed[ch][j], (ch, j)
    verdict = br.judge_after(_audit_reads(panel, fixed, plan["codes"]), plan, luminance_sigma=1e-3,
                             **_REPAIR_KW)
    assert verdict["out"] == [], verdict["out"][:3]


def test_repair_on_a_sawtooth_display_neutralises_balance_and_keeps_the_light():
    # The PA32UCXR case: full-field light is a sawtooth (teeth everywhere -> a large light structure),
    # chromaticity flat; the installed base (old build) carries tinted bumps where two raw reads sat on
    # opposite phases. The repair must move only the BALANCE (the teeth are the display's, not a defect
    # the repair may chase from single reads) and leave the light where it was.
    from dlc import base_repair as br

    panel = SawtoothPanel(period=6.0, depth=0.04)
    installed = _old_build([panel.read_grey(c) for c in _raw_codes()])
    plan = br.audit_plan(100, 300, step=1)
    before = {c: _through(panel, installed, c) for c in range(100, 301)}
    fixed, report = br.repair(installed, _audit_reads(panel, installed, plan["codes"]), plan,
                              oetf=lambda v: v ** (1 / _GAMMA), index_scale=lambda c, s: s,
                              luminance_sigma=1e-3, **_REPAIR_KW)
    assert report["spans"] and all(s["series_moved"] and "luminance" not in s["series_moved"]
                                   for s in report["spans"]), report
    after = {c: _through(panel, fixed, c) for c in range(100, 301)}
    bal = lambda e: max(abs(e[1]), abs(e[2]))  # noqa: E731
    assert max(bal(e) for e in after.values()) < 0.5 * max(bal(e) for e in before.values())
    assert max(bal(e) for e in after.values()) < 0.005      # was 1.2 %
    for c in range(102, 299):                       # light: what it was (within the balance move's own reach)
        assert abs(after[c][0] - before[c][0]) < 0.004, (c, before[c][0], after[c][0])


def test_repair_leaves_a_clean_installed_base_alone():
    from dlc import base_repair as br

    panel = SimPanel()
    installed = _old_build([panel.read_grey(c) for c in _raw_codes()])
    plan = br.audit_plan(90, 400, step=2)
    fixed, report = br.repair(installed, _audit_reads(panel, installed, plan["codes"]), plan,
                              oetf=lambda v: v ** (1 / _GAMMA), index_scale=lambda c, s: s,
                              luminance_sigma=1e-3, **_REPAIR_KW)
    assert report["spans"] == [] and all(fixed[ch] == list(installed[ch]) for ch in "rgb")
