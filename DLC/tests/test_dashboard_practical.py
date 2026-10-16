"""The practical view behind the DLC tab's charts (dashboard/practical.py + the fold's new per-patch fields)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dlc.dashboard.colorimetry import lightness_jnd_pct, patch_deltas
from dlc.dashboard.practical import HDR_BANDS, SURVEY_SHARE, band_of, describe, signal_hue, summarize
from dlc.dashboard.publish import load_content_weights
from dlc.dashboard.state import DashboardState
from dlc.events import Event


def test_jnd_yardstick_grows_toward_black():
    assert lightness_jnd_pct(100.0) == pytest.approx(1.39, abs=0.05)
    assert lightness_jnd_pct(0.05) == pytest.approx(6.9, abs=0.2)
    assert lightness_jnd_pct(0.0) is None


@pytest.mark.parametrize("hdr", [True, False])
def test_tint_direction_signs(hdr):
    # Lower y than D65 at the same x leans blue-ish (cooler) and magenta (u' up): warm < 0, magenta > 0.
    kw = {"is_hdr": True} if hdr else {"is_hdr": False, "luminance": 120.0}
    d = patch_deltas([0.5, 0.5, 0.5], 0.3135, 0.3236, 92.0 if hdr else 26.0, **kw)
    assert d["tint"]["warm"] < 0 and d["tint"]["magenta"] > 0
    # Higher y: yellower / greener.
    d = patch_deltas([0.5, 0.5, 0.5], 0.3127, 0.3350, 92.0 if hdr else 26.0, **kw)
    assert d["tint"]["warm"] > 0 and d["tint"]["magenta"] < 0
    assert d["dY_pct"] is not None and d["jnd_pct"] > 0


def test_band_and_hue_helpers():
    assert band_of(0.5, HDR_BANDS) == 0 and band_of(150.0, HDR_BANDS) == 3 and band_of(5000.0, HDR_BANDS) == 4
    assert signal_hue([0.5, 0.5, 0.5]) == (None, 0.0)
    assert signal_hue([1.0, 0.0, 0.0])[0] == 0          # red
    assert signal_hue([0.0, 0.0, 1.0])[0] == 8          # blue
    assert describe({"tY": 0.35, "sig": [0.9, 0.45, 0.2]}, True).startswith("0.35 nit · orange")


def _pt(tY, de, *, L=0.0, C=0.0, H=0.0, w=None, zone="core", sig=(0.5, 0.5, 0.5), carried=False):
    return {"tY": tY, "de": de, "L": L, "C": C, "H": H, "w": w, "zone": zone, "sig": list(sig), "carried": carried,
            "sc": "#808080", "mc": "#808080", "label": "p"}


def test_summarize_survey_weighting_uses_film_shares():
    pts = [_pt(0.5, 2.0, L=-1.0), _pt(5.0, 1.0), _pt(50.0, 1.0), _pt(150.0, 1.0), _pt(1000.0, 8.0, zone="peak")]
    v = summarize(pts, [], hdr=True)
    assert v["weighting"] == "survey"
    assert [b["share"] for b in v["bands"]] == list(SURVEY_SHARE["hdr"])
    # the < 1 nit band carries half the content: its 2.0 dominates; the 8.0 peak grey barely moves the score
    expected = sum(s * m for s, m in zip(SURVEY_SHARE["hdr"], (2.0, 1.0, 1.0, 1.0, 8.0)))
    assert v["score"] == pytest.approx(expected, abs=1e-3)
    assert v["worst"][0]["de"] == 2.0                     # the peak grey is filed apart, not first
    assert v["worst_apart"][0]["zone"] == "peak"
    assert v["offset_L"] == pytest.approx(0.0)            # only patches >= 1 nit count toward the offset
    assert v["bands"][0]["L"] == pytest.approx(-1.0) and v["bands"][0]["Lr"] == pytest.approx(1.0)   # signed / RMS


def test_summarize_content_weighting_and_carried_points():
    pts = [_pt(0.5, 2.0, w=0.75), _pt(50.0, 1.0, w=0.25), _pt(50.0, 9.0, carried=True)]
    v = summarize(pts, [], hdr=True)
    assert v["weighting"] == "content"
    assert v["n"] == 2                                     # carried (earlier stage) reads excluded
    assert v["score"] == pytest.approx(0.75 * 2.0 + 0.25 * 1.0)
    shares = {b["label"]: b["share"] for b in v["bands"]}
    assert shares["<1"] == pytest.approx(0.75) and shares["10-100"] == pytest.approx(0.25)


def test_limits_from_greys():
    greys = [{"Y": 1476.0, "tY": 1725.0, "tw": -0.2, "tm": 7.0, "de": 14.0},
             {"Y": 397.0, "tY": 401.0, "tw": 0.01, "tm": -0.05, "de": 0.8},
             {"Y": 200.0, "tY": 203.0, "tw": 0.2, "tm": -0.1, "de": 1.0, "cct": 6499, "duv": 0.0035, "dY": -1.3},
             {"Y": 0.05, "tY": 0.045, "tw": -0.5, "tm": -0.8, "de": 1.9}]
    v = summarize([], greys, hdr=True, target_peak=1729.0, black_floor=0.0024)
    lim = v["limits"]
    assert lim["peak"] == 1476.0 and lim["target_peak"] == 1729.0
    assert lim["neutral_peak"] == 397.0                    # the 1476 grey leans magenta: not neutral
    assert lim["black"] == 0.0024
    assert lim["white"]["tY"] == 203.0                     # HDR: the grey nearest 203 nit


def test_fold_publishes_practical_fields(tmp_path: Path):
    s = DashboardState()
    s.ingest(Event(level="INFO", stage="run", event="run_header",
                   data={"mode": "SDR", "luminance": 120.0, "gamma": 2.2, "flow": "verify-only"}))
    s.ingest(Event(level="INFO", stage="measure:verify", event="stage_start"))
    s.ingest(Event(level="INFO", stage="measure:verify", event="patch_read", tier="stream",
                   data={"seq": 1, "role": "measurement", "label": "G50", "rgb": [128, 128, 128],
                         "signal": [0.5, 0.5, 0.5], "Y": 26.0, "xy": [0.3135, 0.3236], "ok": True}))
    c = s.charts()
    p = c["cie"]["points"][0]
    for k in ("Y", "tY", "L", "C", "tw", "tm", "dY", "jnd", "zone", "lsc", "lmc"):
        assert p.get(k) is not None, k
    g = c["grayscale"][0]
    assert g["tw"] < 0 and g["tm"] > 0 and g["dY"] is not None
    pr = c["practical"]
    assert pr["weighting"] == "survey" and pr["n"] == 1
    assert c["stages_practical"][0]["stage"] == "measure:verify"


def test_content_weights_from_the_patch_file_named_by_the_header(tmp_path: Path):
    seq = tmp_path / "seq.json"
    seq.write_text(json.dumps({"codes": [[10, 20, 30], [512, 512, 512], [10, 20, 30]],
                               "content_weight": [0.4, 0.0, 0.0], "role": ["content", "anchor", "content+bookend"]}))
    w = load_content_weights(seq)
    assert w[(10, 20, 30)] == (0.4, "content")             # the bookend repeat keeps the first weight
    assert w[(512, 512, 512)] == (0.0, "anchor")
    assert load_content_weights(tmp_path / "missing.json") == {}
    # The fold learns the file from the header and applies the weights at chart time, whenever they arrive.
    s = DashboardState()
    s.ingest(Event(level="INFO", stage="run", event="run_header",
                   data={"mode": "SDR", "luminance": 120.0, "gamma": 2.2, "verify_patches_file": str(seq)}))
    assert s.verify_patches_file == str(seq)
    s.ingest(Event(level="INFO", stage="measure:verify", event="stage_start"))
    s.ingest(Event(level="INFO", stage="measure:verify", event="patch_read", tier="stream",
                   data={"seq": 1, "role": "measurement", "label": "x", "rgb": [10, 20, 30],
                         "signal": [10 / 1023, 20 / 1023, 30 / 1023], "Y": 0.4, "xy": [0.25, 0.26], "ok": True}))
    s.content_weights = load_content_weights(s.verify_patches_file)   # what the publisher does
    pt = s.charts()["cie"]["points"][0]
    assert pt["w"] == 0.4 and pt["crole"] == "content"


def test_colour_by_level_families():
    pts = [_pt(ty, 1.0, L=-0.5, C=-1.0 if ty < 1 else 0.2, sig=(1.0, 0.1, 0.1))     # a red ramp: 5 per half decade
           for ty in [0.2] * 5 + [2.0] * 5 + [20.0] * 5]
    pts.append(_pt(5.0, 1.0, C=0.5, sig=(0.9, 0.2, 0.9)))                            # one magenta
    pts.append(_pt(5.0, 9.0, sig=(0.1, 0.1, 1.0), zone="clamped"))                   # a clamped blue: left out
    pts.append(_pt(5.0, 0.4, sig=(0.5, 0.5, 0.5)))                                   # a grey: no family
    v = summarize(pts, [], hdr=True)
    assert [f["key"] for f in v["families"]] == ["R", "Y", "G", "C", "B", "M"]
    fam = {f["key"]: f for f in v["families"]}
    r = fam["R"]
    assert r["n"] == 15 and not r["thin"]
    assert [p["n"] for p in r["points"]] == [5, 5, 5]
    assert r["points"][0]["C"] == pytest.approx(-1.0) and r["points"][2]["C"] == pytest.approx(0.2)
    assert r["points"][0]["L"] == pytest.approx(-0.5)
    assert r["points"][0]["Y"] == pytest.approx(0.2, rel=1e-3)                       # the bin's (log) mean level
    assert fam["M"]["n"] == 1 and fam["M"]["thin"]
    assert fam["B"]["n"] == 0 and fam["B"]["points"] == []


def test_colour_by_level_merges_single_patch_bins():
    pts = [_pt(ty, 1.0, C=0.3, sig=(0.1, 1.0, 0.1)) for ty in (0.1, 1.0, 10.0, 100.0, 1000.0)]   # one per bin
    g = {f["key"]: f for f in summarize(pts, [], hdr=True)["families"]}["G"]
    assert sum(p["n"] for p in g["points"]) == 5
    assert [p["n"] for p in g["points"]] == [5]                                  # five lone readings: one level


def test_worst_rows_carry_signed_components_and_hue_direction():
    pts = [_pt(0.35, 7.4, L=-1.2, C=-0.8, H=6.9, sig=(1.0, 0.55, 0.2), w=0.004),     # orange turning toward yellow
           _pt(5.0, 1.0, L=0.2, w=0.996)]
    v = summarize(pts, [], hdr=True)
    assert "grey" in v["worst"][0]["desc"]                                          # the content-heavy grey ranks first
    top = next(r for r in v["worst"] if "orange" in r["desc"])
    assert (top["L"], top["C"], top["H"]) == (-1.2, -0.8, 6.9)
    assert top["hue_to"] == "yellow"
    assert v["worst"][0]["hue_to"] is None                                          # a grey has no hue to turn
    assert v["impact_total"] == pytest.approx(7.4 * 0.004 + 1.0 * 0.996, abs=1e-4)


def test_uv_distance_and_gamut_coverage():
    from dlc.dashboard.practical import P3_D65, coverage_uv, jncd, xy_to_uv
    u, v = xy_to_uv(0.3127, 0.3290)
    assert u == pytest.approx(0.1978, abs=1e-4) and v == pytest.approx(0.4683, abs=1e-4)   # D65
    assert jncd(0.3127, 0.3290, 0.3127, 0.3290) == pytest.approx(0.0)
    r709 = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))
    r2020 = ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046))
    assert coverage_uv(r2020, r709) == pytest.approx(100.0)                  # the wider gamut covers all of the narrower
    assert coverage_uv(P3_D65, r2020) == pytest.approx(72.8, abs=0.3)        # P3 is about 73 % of Rec.2020 in u'v'
    assert coverage_uv(r709, P3_D65) == pytest.approx(79.6, abs=0.3)
    assert coverage_uv(r709[::-1], r2020) == pytest.approx(coverage_uv(r709, r2020))   # either winding


def test_tone_halves_and_tint_spread():
    from dlc.dashboard.practical import tone_and_tint
    greys = [{"signal": 0.2, "Y": 1.1, "tY": 1.0, "dY": 10.0, "tw": 0.5, "tm": 0.0},
             {"signal": 0.3, "Y": 5.0, "tY": 5.0, "dY": -2.0, "tw": -0.5, "tm": 0.0},
             {"signal": 0.8, "Y": 300.0, "tY": 300.0, "dY": 1.0, "tw": 0.0, "tm": 0.2},
             {"signal": 0.95, "Y": 900.0, "tY": 1500.0, "dY": -40.0, "tw": 0.0, "tm": 3.0},   # past the peak: out
             {"signal": 0.05, "Y": 0.02, "tY": 0.01, "dY": 100.0, "tw": 0.0, "tm": 0.0},      # below the meter floor: out
             {"signal": 0.4, "Y": 50.0, "tY": 50.0, "dY": 9.0, "carried": True}]                # an earlier stage: out
    t = tone_and_tint(greys, peak=900.0, black=0.002)
    assert (t["dark"], t["n_dark"]) == (pytest.approx(4.0), 2)
    assert (t["light"], t["n_light"]) == (pytest.approx(1.0), 1)
    assert t["tint_spread"] == pytest.approx(1.0)                            # (0.5, 0) against (-0.5, 0)


def test_fold_publishes_tone_stages_coverage_and_drift_view():
    s = DashboardState()
    s.ingest(Event(level="INFO", stage="run", event="run_header",
                   data={"mode": "SDR", "luminance": 120.0, "gamma": 2.2, "flow": "full"}))
    seq = 0
    for stage, gain in (("measure:raw", 1.1), ("measure:verify", 1.0)):
        s.ingest(Event(level="INFO", stage=stage, event="stage_start"))
        for sig in (0.25, 0.5, 0.75):
            seq += 1
            s.ingest(Event(level="INFO", stage=stage, event="patch_read", tier="stream",
                           data={"seq": seq, "role": "measurement", "label": f"G{sig}", "rgb": [round(sig * 255)] * 3,
                                 "signal": [sig, sig, sig], "Y": 120.0 * sig ** 2.2 * gain,
                                 "xy": [0.3127, 0.3290], "ok": True}))
    c = s.charts()
    assert [t["stage"] for t in c["stages_tone"]] == ["measure:raw", "measure:verify"]
    assert len(c["stages_tone"][0]["points"]) == 3
    assert c["cie"]["points"][0]["jncd"] == pytest.approx(0.0, abs=0.05)
    assert "coverage" in c["cie"] and "drift_view" in c
    assert c["drift_view"]["noise_pct"] is None                              # no drift checkpoints in this run
    assert c["practical"]["tone"]["n_light"] + c["practical"]["tone"]["n_dark"] == 3
