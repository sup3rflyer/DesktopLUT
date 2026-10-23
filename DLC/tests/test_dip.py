"""Tests for the Display+Instrument Profile store (``dlc.dip``).

Covers the JSON round-trip, corruption/missing tolerance, staleness, and the two
read-policy bridges the measure loop relies on: σ interpolation across luminance and
the variance→reads-needed rule (averaging cuts SE by √N).
"""

from __future__ import annotations

import json
import math

from dlc.dip import DipStore, DisplayInstrumentProfile, NoiseBand


def _dip(**kw) -> DisplayInstrumentProfile:
    base = dict(
        display="Test Panel",
        noise_model=[NoiseBand(nits=1.0, sigma_de=1.2, reads=10),
                     NoiseBand(nits=50.0, sigma_de=0.4, reads=10),
                     NoiseBand(nits=120.0, sigma_de=0.15, reads=10)],
        settle_seconds=0.3,
        cold_channel="B",
        made="2026-06-17",
    )
    base.update(kw)
    return DisplayInstrumentProfile(**base)


def test_thermal_and_hdr_fields_round_trip(tmp_path):
    """The closed-loop thermal + HDR characterization fields persist and reload."""
    store = DipStore(tmp_path / "dip_store.json")
    dip = _dip(mode="HDR", thermal_regime="fluctuating", fluctuation_envelope=0.012,
               warmin_magnitude=0.08, thermal_tau_patches=42, eotf_undershoot=-0.061,
               white_vs_luminance=[[100.0, 0.3127, 0.329], [1840.0, 0.328, 0.329]])
    store.record(dip)
    reloaded = DipStore.load(tmp_path / "dip_store.json").get("Test Panel:HDR")
    assert reloaded is not None
    assert reloaded.thermal_regime == "fluctuating"
    assert reloaded.fluctuation_envelope == 0.012
    assert reloaded.warmin_magnitude == 0.08
    assert reloaded.thermal_tau_patches == 42
    assert reloaded.eotf_undershoot == -0.061
    assert reloaded.white_vs_luminance == [[100.0, 0.3127, 0.329], [1840.0, 0.328, 0.329]]


def test_round_trip_persist_and_load(tmp_path):
    store = DipStore(tmp_path / "dip_store.json")
    store.record(_dip())
    again = DipStore.load(tmp_path / "dip_store.json")
    got = again.get("Test Panel")
    assert got is not None
    assert got.cold_channel == "B"
    assert got.settle_seconds == 0.3
    assert [b.nits for b in got.noise_model] == [1.0, 50.0, 120.0]
    assert got.noise_model[0].sigma_de == 1.2


def test_missing_file_is_empty_not_corrupt(tmp_path):
    store = DipStore.load(tmp_path / "nope.json")
    assert store.get("Test Panel") is None
    assert store.corrupt is False


def test_corrupt_file_flagged_not_fatal(tmp_path):
    p = tmp_path / "dip_store.json"
    p.write_text("{ not json", encoding="utf-8")
    store = DipStore.load(p)
    assert store.corrupt is True
    assert store.records() == {}


def test_sigma_interpolates_and_clamps():
    dip = _dip()
    # Clamped flat past the ends.
    assert dip.expected_sigma_de(0.1) == 1.2
    assert dip.expected_sigma_de(500.0) == 0.15
    # Linear midpoint between the 50→120 band (0.4 → 0.15).
    mid = dip.expected_sigma_de(85.0)
    assert math.isclose(mid, 0.4 + 0.5 * (0.15 - 0.4), rel_tol=1e-9)


def test_empty_noise_model_defers_to_live():
    dip = _dip(noise_model=[])
    assert dip.expected_sigma_de(50.0) is None
    assert dip.reads_for_tolerance(50.0, 0.2) is None


def test_reads_for_tolerance_follows_sqrt_n_rule():
    dip = _dip()
    # Bright: σ≈0.15 well under a 0.2 tolerance → a single read suffices.
    assert dip.reads_for_tolerance(120.0, 0.2) == 1
    # Dark: σ=1.2 at 1 nit, tolerance 0.3 → N ≥ (1.2/0.3)² = 16.
    assert dip.reads_for_tolerance(1.0, 0.3) == 16
    # Tolerance 0 (degenerate) → never divides by zero, clamps to 1.
    assert dip.reads_for_tolerance(1.0, 0.0) == 1


def test_staleness_uses_record_then_default():
    fresh = _dip(made="2026-06-10", max_age_days=180)
    assert fresh.is_stale("2026-06-17") is False
    old = _dip(made="2025-01-01", max_age_days=180)
    assert old.is_stale("2026-06-17") is True
    # Per-record max_age_days wins over the default.
    short = _dip(made="2026-06-10", max_age_days=3)
    assert short.is_stale("2026-06-17") is True
    # No made date → treat as stale (force a re-characterization).
    assert _dip(made=None).is_stale("2026-06-17") is True


def test_mode_keys_let_sdr_and_hdr_coexist(tmp_path):
    # A panel's thermal/noise behaviour differs by mode, so an SDR and an HDR DIP must coexist —
    # the store keys by display:mode when a mode is set, bare display name otherwise (back-compat).
    store = DipStore(tmp_path / "dip_store.json")
    store.record(_dip(mode="SDR", settle_seconds=0.3))
    store.record(_dip(mode="HDR", settle_seconds=0.9))
    again = DipStore.load(tmp_path / "dip_store.json")
    assert again.get("Test Panel:SDR").settle_seconds == 0.3
    assert again.get("Test Panel:HDR").settle_seconds == 0.9
    assert again.get("Test Panel:SDR").mode == "SDR"
    assert len(again.records()) == 2          # two distinct profiles for one display
    # a mode-less record still keys by the bare display name
    store.record(_dip(settle_seconds=0.5))
    assert store.get("Test Panel").settle_seconds == 0.5


def test_upsert_by_display_name(tmp_path):
    store = DipStore(tmp_path / "dip_store.json")
    store.record(_dip(settle_seconds=0.3))
    store.record(_dip(settle_seconds=0.9))   # same display → overwrite
    assert len(store.records()) == 1
    assert store.get("Test Panel").settle_seconds == 0.9


def test_malformed_record_is_dropped_visibly_not_silently(tmp_path):
    # A per-record parse failure (schema drift / hand-editing) drops ONLY that record —
    # but visibly (store.dropped), so a caller can say "your DIP for X was dropped"
    # instead of silently running with no priors for that display (fable audit F3-5).
    import json
    p = tmp_path / "dip_store.json"
    good = _dip().as_dict()
    bad = {"display": "Bad Panel", "noise_model": [{"nits": "not-a-number"}]}
    p.write_text(json.dumps({"displays": {"Test Panel": good, "Bad Panel": bad}}),
                 encoding="utf-8")
    store = DipStore.load(p)
    assert store.get("Test Panel") is not None       # the good record survives
    assert store.get("Bad Panel") is None
    assert store.dropped == ["Bad Panel"]            # the loss is visible, not silent
    assert store.corrupt is False                    # file-level corruption is a separate flag


def test_save_stamps_a_schema_version_and_reloads_clean(tmp_path):
    import json
    store = DipStore(tmp_path / "dip_store.json")
    store.record(_dip())
    payload = json.loads((tmp_path / "dip_store.json").read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    reloaded = DipStore.load(tmp_path / "dip_store.json")
    assert reloaded.dropped == [] and reloaded.corrupt is False
    assert reloaded.get("Test Panel") is not None


# ----------------------------------------------------------------------------- the refresh response (display axis)
from dlc.dip import REFRESH_RESPONSE_SCHEMA, RefreshLevel, RefreshRate, RefreshResponse  # noqa: E402


def _response() -> RefreshResponse:
    return RefreshResponse(
        measured="2026-10-23", run_id="20261023_180329_refresh_trim_hdr_mon0_trim",
        rates=[RefreshRate(key="47.952", num=48000, den=1001,
                           levels=[RefreshLevel(nits=102.6, ratio=1.0264, u_ln=5e-4, n=2, code=520),
                                   RefreshLevel(nits=0.518, ratio=1.0337, u_ln=7e-4, n=2, code=120)]),
               RefreshRate(key="48", num=48, den=1, levels=[RefreshLevel(nits=102.6, ratio=1.0264)],
                           same_as="47.952"),
               RefreshRate(key="59.94", num=60000, den=1001, levels=[RefreshLevel(nits=102.6, ratio=1.0004)],
                           same_as="60")],
        identity={"hardware_id": "AUS322A", "device_path": r"\?\DISPLAY#AUS322A#5&1&UID4353#{x}"},
        conditions={"mode": "HDR", "layers": {"desktop_gamma": False}}, peak_nits=1800.0, verdict="nonlinear")


def test_refresh_response_round_trips_and_sorts_levels(tmp_path):
    store = DipStore(tmp_path / "dip_store.json")
    store.record(_dip(mode="HDR", refresh_response=_response()))
    got = DipStore.load(tmp_path / "dip_store.json").get("Test Panel:HDR")
    rr = got.refresh_response
    assert rr is not None and rr.schema == REFRESH_RESPONSE_SCHEMA
    assert rr.measured == "2026-10-23" and rr.identity["hardware_id"] == "AUS322A"
    r = rr.rate("47.952")
    assert (r.num, r.den) == (48000, 1001) and r.hz == 48000 / 1001
    assert [lv.nits for lv in r.levels] == [0.518, 102.6]           # ascending, whatever the input order
    assert rr.rate("48").same_as == "47.952" and rr.rate("59.94").same_as == "60"
    assert rr.peak_nits == 1800.0 and rr.interpolable is False


def test_a_record_without_a_refresh_response_is_written_exactly_as_before(tmp_path):
    """Back-compat both ways: old records load (no section), and a record without one serialises with no key."""
    dip = _dip(mode="HDR")
    assert "refresh_response" not in dip.as_dict()
    store = DipStore(tmp_path / "dip_store.json")
    store.record(dip)
    raw = json.loads((tmp_path / "dip_store.json").read_text(encoding="utf-8"))
    assert "refresh_response" not in raw["displays"]["Test Panel:HDR"]
    assert DipStore.load(tmp_path / "dip_store.json").get("Test Panel:HDR").refresh_response is None


def test_an_unreadable_refresh_response_drops_the_section_not_the_record(tmp_path):
    for bad in ({"measured": "2026-10-23", "rates": [{"key": "48", "num": 0, "den": 1}]},       # no rational
                {"measured": "2026-10-23", "rates": [{"key": "48", "num": 48, "den": 1,
                                                       "levels": [{"nits": 1.0, "ratio": float("nan")}]}]},
                {"measured": "2026-10-23", "schema": REFRESH_RESPONSE_SCHEMA + 1},           # from a newer DLC
                {"rates": []}):                                                               # no date
        rec = _dip(mode="HDR").as_dict()
        rec["refresh_response"] = bad
        (tmp_path / "dip_store.json").write_text(json.dumps({"schema": 1, "displays": {"Test Panel:HDR": rec}}),
                                                 encoding="utf-8")
        store = DipStore.load(tmp_path / "dip_store.json")
        got = store.get("Test Panel:HDR")
        assert got is not None and not store.dropped, bad          # the record (its priors) survives
        assert got.refresh_response is None
        assert any("refresh_response dropped" in n for n in got.notes)
