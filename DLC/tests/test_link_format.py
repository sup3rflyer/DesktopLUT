"""Live display-link depth: the ctypes DisplayConfig probe (mocked — no real Windows calls), the
monitor match, the mechanical assessment, and the orchestrator's ``preflight:link-depth`` seam +
the refine's output-quantization floor following the MEASURED link (2026-09-26: a BenQ PD2700U
profiled 10-bit ran an 8 bpc HDMI link and nothing caught it)."""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from dlc import link_format as lf
from dlc.adjudication import SEAM_LINK_DEPTH
from dlc.calibrate import (
    AdjudicationRequired, AutoAdjudicator, CalibrationAborted, Decision, MappingAdjudicator,
)
from test_calibrate import _make


# ---------------------------------------------------------------------------
# ctypes probe over a fake user32 (structs are real ctypes; only the calls are faked)
# ---------------------------------------------------------------------------

class _FakeApi:
    """Two active paths on adapter (72348, 0): DP target 4353 = 10 bpc RGB (\\\\.\\DISPLAY1) and
    HDMI target 4352 = 8 bpc RGB (\\\\.\\DISPLAY2) — the rig's real 2026-09-26 readout."""

    def __init__(self, *, info2_ok=True, targets=None, sizes_rc=0, insufficient_once=False):
        self.targets = targets or {
            4353: {"src": 0, "tech": 10, "bpc": 10, "enc": 0, "gdi": r"\\.\DISPLAY1", "name": "PA32UCXR"},
            4352: {"src": 1, "tech": 5, "bpc": 8, "enc": 0, "gdi": r"\\.\DISPLAY2", "name": "BenQ PD2700U"},
        }
        self.info2_ok = info2_ok
        self.sizes_rc = sizes_rc
        self.insufficient_once = insufficient_once
        self.calls: list[int] = []

    def buffer_sizes(self, flags):
        assert flags == lf.QDC_ONLY_ACTIVE_PATHS
        return self.sizes_rc, len(self.targets), 2 * len(self.targets)

    def query(self, flags, n_paths, n_modes):
        if self.insufficient_once:
            self.insufficient_once = False
            return lf.ERROR_INSUFFICIENT_BUFFER, []
        out = []
        for tid, t in self.targets.items():
            p = lf.PathInfo()
            p.sourceInfo.adapterId.LowPart, p.sourceInfo.adapterId.HighPart = 72348, 0
            p.sourceInfo.id = t["src"]
            p.targetInfo.adapterId.LowPart, p.targetInfo.adapterId.HighPart = 72348, 0
            p.targetInfo.id = tid
            p.targetInfo.outputTechnology = t["tech"]
            out.append(p)
        return 0, out

    def device_info(self, pkt):
        kind = pkt.header.type
        self.calls.append(kind)
        assert pkt.header.size == ctypes.sizeof(pkt)
        if kind == lf.DEVICE_INFO_GET_SOURCE_NAME:
            t = next(t for t in self.targets.values() if t["src"] == pkt.header.id)
            pkt.viewGdiDeviceName = t["gdi"]
            return 0
        t = self.targets[pkt.header.id]
        if kind == lf.DEVICE_INFO_GET_TARGET_NAME:
            pkt.monitorFriendlyDeviceName = t["name"]
            return 0
        if kind == lf.DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 and not self.info2_ok:
            return 87   # ERROR_INVALID_PARAMETER: pre-24H2 build
        pkt.bitsPerColorChannel = t["bpc"]
        pkt.colorEncoding = t["enc"]
        return 0


def test_struct_sizes_match_wingdi():
    import ctypes
    assert ctypes.sizeof(lf.PathInfo) == 72 and ctypes.sizeof(lf.ModeInfo) == 64
    assert ctypes.sizeof(lf.AdvancedColorInfo) == 32 and ctypes.sizeof(lf.AdvancedColorInfo2) == 36


def test_probe_reads_bpc_encoding_connector_and_gdi_name():
    got = lf.probe_link_formats(_FakeApi())
    assert got["available"] is True
    by_target = {p["target_id"]: p for p in got["paths"]}
    dp, hdmi = by_target[4353], by_target[4352]
    assert (dp["bpc"], dp["encoding"], dp["connector"], dp["device_name"]) == \
        (10, "RGB", "DISPLAYPORT_EXTERNAL", r"\\.\DISPLAY1")
    assert (hdmi["bpc"], hdmi["encoding"], hdmi["connector"], hdmi["device_name"]) == \
        (8, "RGB", "HDMI", r"\\.\DISPLAY2")
    assert hdmi["friendly_name"] == "BenQ PD2700U" and hdmi["query"] == "displayconfig2"
    assert hdmi["adapter_id"] == {"low": 72348, "high": 0}


def test_probe_falls_back_to_legacy_query_before_24h2():
    api = _FakeApi(info2_ok=False)
    got = lf.probe_link_formats(api)
    assert {p["query"] for p in got["paths"]} == {"displayconfig"}
    assert lf.DEVICE_INFO_GET_ADVANCED_COLOR_INFO in api.calls
    assert sorted(p["bpc"] for p in got["paths"]) == [8, 10]


def test_probe_zero_bpc_and_ycbcr_are_reported_as_unknown_and_named():
    api = _FakeApi(targets={7: {"src": 0, "tech": 17, "bpc": 0, "enc": 3, "gdi": r"\\.\DISPLAY9", "name": ""}})
    (p,) = lf.probe_link_formats(api)["paths"]
    assert p["bpc"] is None and p["encoding"] == "YCBCR420" and p["connector"] == "INDIRECT_VIRTUAL"


def test_probe_retries_on_insufficient_buffer_and_reports_failures():
    assert lf.probe_link_formats(_FakeApi(insufficient_once=True))["available"] is True
    bad = lf.probe_link_formats(_FakeApi(sizes_rc=5))
    assert bad["available"] is False and "rc=5" in bad["error"]

    class Boom:
        def buffer_sizes(self, flags):
            raise OSError("no user32")

    assert lf.probe_link_formats(Boom())["available"] is False   # never raises


# ---------------------------------------------------------------------------
# matching a DesktopLUT monitor entry to its link
# ---------------------------------------------------------------------------

_ENTRY = {"index": 1, "device_name": r"\\.\DISPLAY2", "target_id": 4352,
          "adapter_id": {"low": 72348, "high": 0}}


def test_pipe_link_fields_win_and_probe_is_not_called():
    def probe():
        raise AssertionError("must not probe when the pipe reports the link")

    got = lf.link_format_for_monitor({**_ENTRY, "link_bpc": 8, "link_color_encoding": "RGB",
                                      "link_connector": "HDMI"}, probe)
    assert (got["bpc"], got["encoding"], got["source"]) == (8, "RGB", "pipe")


def test_old_build_falls_back_to_ctypes_matched_by_adapter_and_target():
    got = lf.link_format_for_monitor(_ENTRY, lambda: lf.probe_link_formats(_FakeApi()))
    assert (got["bpc"], got["source"], got["matched_by"], got["connector"]) == \
        (8, "ctypes", "adapter_id+target_id", "HDMI")


def test_ctypes_match_falls_back_to_gdi_device_name():
    entry = {"index": 0, "device_name": r"\\.\display1"}     # no ids (older payload)
    got = lf.link_format_for_monitor(entry, lambda: lf.probe_link_formats(_FakeApi()))
    assert (got["bpc"], got["matched_by"]) == (10, "device_name")


def test_unmatched_or_unprobeable_is_unmeasured_not_a_guess():
    stranger = {"index": 3, "device_name": r"\\.\DISPLAY7", "target_id": 1, "adapter_id": {"low": 1, "high": 0}}
    assert lf.link_format_for_monitor(stranger, lambda: lf.probe_link_formats(_FakeApi()))["bpc"] is None
    assert lf.link_format_for_monitor(_ENTRY, None)["bpc"] is None
    assert lf.link_format_for_monitor(None, None)["bpc"] is None
    down = lf.link_format_for_monitor(_ENTRY, lambda: {"available": False, "error": "not Windows"})
    assert down["bpc"] is None and "not Windows" in down["reason"]


# ---------------------------------------------------------------------------
# the mechanical assessment (lists disagreements + a suggestion; decides nothing)
# ---------------------------------------------------------------------------

def _codes(a):
    return {r["code"] for r in a["reasons"]}


def test_benq_case_10bit_run_and_profile_on_8bpc_link_suggests_abort():
    a = lf.assess_link_depth({"bpc": 8, "encoding": "RGB", "source": "ctypes"},
                             run_bits=10, profile_bits=10, mode="SDR")
    assert a["mismatch"] and _codes(a) == {"pattern_exceeds_link", "profile_disagrees"}
    assert a["suggested"] == "abort"


def test_stale_profile_only_suggests_the_link_for_the_floor():
    a = lf.assess_link_depth({"bpc": 8, "encoding": "RGB"}, run_bits=8, profile_bits=10, mode="SDR")
    assert _codes(a) == {"profile_disagrees"} and a["suggested"] == "use-link"


def test_wider_link_than_panel_is_evidence_not_a_mismatch():
    # a 12 bpc HDMI TV link on a 10-bit panel: min(link, panel) is physics, not a judgment
    a = lf.assess_link_depth({"bpc": 12, "encoding": "RGB"}, run_bits=10, profile_bits=10, mode="HDR")
    assert not a["mismatch"] and a["link_wider_than_profile"] is True


def test_wider_link_keeps_the_profile_floor_without_a_seam(tmp_path: Path):
    calib = _make(tmp_path, "ld_wide", mode="HDR", bit_depth=10, adjudicator=MappingAdjudicator({}))
    _with_link(calib, 12)
    calib.stage_preflight()
    assert calib._output_bits() == 10
    assert calib.calib["output_depth"]["source"] == "profile (narrower than the link)"


def test_relaunch_advice_only_when_the_pattern_depth_is_the_problem(tmp_path: Path):
    calib = _make(tmp_path, "ld_advice", bit_depth=8, adjudicator=MappingAdjudicator({}))
    _with_link(calib, 8)                          # stale profile only (10 vs 8), patterns fine
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_preflight()
    assert "relaunch" not in exc.value.request.question
    assert exc.value.request.recommendation == "use-link"


def test_hdr_on_8bpc_and_ycbcr_are_flagged():
    a = lf.assess_link_depth({"bpc": 8, "encoding": "YCBCR422"}, run_bits=8, profile_bits=8, mode="HDR")
    assert _codes(a) == {"hdr_below_10bpc", "non_rgb_encoding"} and a["suggested"] == "abort"


def test_8bit_patterns_on_10bpc_link_is_not_a_mismatch():
    # the default composited SDR path — under-sampling is the transport tell's business, not a seam
    a = lf.assess_link_depth({"bpc": 10, "encoding": "RGB"}, run_bits=8, profile_bits=10, mode="SDR")
    assert a["checked"] and not a["mismatch"]


def test_unmeasured_link_is_unchecked_never_a_mismatch():
    a = lf.assess_link_depth({"bpc": None, "reason": "pipe down"}, run_bits=10, profile_bits=10, mode="SDR")
    assert a["checked"] is False and a["mismatch"] is False and a["reason"] == "pipe down"


# ---------------------------------------------------------------------------
# orchestrator: preflight seam + output depth
# ---------------------------------------------------------------------------

def _with_link(calib, bpc, *, encoding="RGB", drop=False):
    orig = calib.controller.query_monitors

    def qm():
        r = orig()
        m0 = r["monitors"][0]
        if drop:     # a DesktopLUT build that predates the link_* fields
            for k in ("link_bpc", "link_color_encoding", "link_connector", "link_format_source"):
                m0.pop(k, None)
        else:
            m0["link_bpc"], m0["link_color_encoding"] = bpc, encoding
        return r

    calib.controller.query_monitors = qm


def test_matching_link_passes_preflight_and_sets_the_output_depth_from_the_link(tmp_path: Path):
    calib = _make(tmp_path, "ld_ok", adjudicator=MappingAdjudicator({}))   # mock: 10 bpc, profile 10
    out = calib.stage_preflight()
    assert out.digest["link_depth"]["checked"] and not out.digest["link_depth"]["mismatch"]
    assert calib.calib["output_depth"]["bits"] == 10 and calib.calib["output_depth"]["source"] == "link"
    assert out.digest["transport"].get("panel_bit_depth_source") in (None, "link")


def test_mismatch_pauses_at_the_link_depth_seam_with_the_evidence(tmp_path: Path):
    calib = _make(tmp_path, "ld_seam", adjudicator=MappingAdjudicator({}), bit_depth=10)
    _with_link(calib, 8)
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_preflight()
    req = exc.value.request
    assert req.seam == SEAM_LINK_DEPTH and req.key == "preflight:link-depth"
    assert req.options == ("abort", "use-link", "use-profile") and req.recommendation == "abort"
    assert req.digest["link_bpc"] == 8 and req.digest["run_bit_depth"] == 10
    assert req.digest["profile_bit_depth"] == 10 and req.digest["link_source"] == "pipe"
    assert "8 bpc" in req.question and "use-profile" in req.question
    assert "--bit-depth 8" in req.question and "relaunch" in req.question


@pytest.mark.parametrize("choice,bits,source", [("use-link", 8, "link"), ("use-profile", 10, "profile")])
def test_the_seam_decision_sets_the_output_depth(tmp_path: Path, choice, bits, source):
    calib = _make(tmp_path, f"ld_{choice}", bit_depth=8,
                  adjudicator=MappingAdjudicator({"preflight:link-depth": Decision(choice)}))
    _with_link(calib, 8)
    calib.stage_preflight()
    assert calib._output_bits() == bits
    assert calib.calib["output_depth"]["source"] == source
    assert calib.calib["output_depth"]["decision"] == choice


def test_abort_at_the_seam_aborts_before_anything_is_measured(tmp_path: Path):
    calib = _make(tmp_path, "ld_abort", bit_depth=10,
                  adjudicator=MappingAdjudicator({"preflight:link-depth": Decision("abort")}))
    _with_link(calib, 8)
    with pytest.raises(CalibrationAborted):
        calib.stage_preflight()


def test_old_build_uses_the_injected_ctypes_probe(tmp_path: Path):
    calib = _make(tmp_path, "ld_ctypes", adjudicator=MappingAdjudicator({}), bit_depth=10)
    _with_link(calib, None, drop=True)
    # mock monitor 0 = adapter (0,0) target 0; the fake probe reports that target at 8 bpc over HDMI
    calib.link_probe = lambda: {"available": True, "paths": [
        {"adapter_id": {"low": 0, "high": 0}, "target_id": 0, "device_name": r"\\.\DISPLAY1",
         "bpc": 8, "encoding": "RGB", "connector": "HDMI", "query": "displayconfig2"}]}
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_preflight()
    assert exc.value.request.digest["link_source"] == "ctypes"
    assert exc.value.request.digest["link_connector"] == "HDMI"


def test_unmeasurable_link_falls_back_to_the_profile_without_a_seam(tmp_path: Path):
    calib = _make(tmp_path, "ld_unmeasured", adjudicator=MappingAdjudicator({}))
    _with_link(calib, None, drop=True)          # old build, no probe wired (sim default)
    out = calib.stage_preflight()
    assert out.digest["link_depth"]["checked"] is False
    assert calib.calib["output_depth"]["source"] == "profile (link unmeasured)"
    assert calib._output_bits() == 10


def test_refine_quantization_floor_follows_the_measured_link(tmp_path: Path):
    # The BenQ case with a stale profile: profile says 10, the link is 8, the LLM chose use-link
    # (here: the rubber-stamp takes the use-link suggestion) — the refine's per-level floor must
    # be an 8-bit code, not the profile's 10-bit one.
    calib = _make(tmp_path, "ld_refine", bit_depth=8, adjudicator=AutoAdjudicator())
    _with_link(calib, 8)
    calib.run("mhc-only")
    assert calib.calib["output_depth"] == {**calib.calib["output_depth"], "bits": 8, "source": "link",
                                           "decision": "use-link"}
    params = calib._state["mhc_params"]
    from dlc.colormath import rgb_to_xyz_matrix
    from dlc.mhc import parse_ti3
    prim, nw = params["primaries"], params["measured_white"]
    m = rgb_to_xyz_matrix(prim["rx"], prim["ry"], prim["gx"], prim["gy"], prim["bx"], prim["by"],
                          nw["x"], nw["y"], white_Y=float(params["target_luminance"]))
    peaks = [[m[r][c] for r in range(3)] for c in range(3)]
    samples = parse_ti3(calib.ctx.root / "measurements" / "refine_1.ti3")
    conv = calib._refine_round_analysis(samples, None, None, white_xy=(0.3127, 0.3290),
                                        dark_floor_nits=0.5,
                                        top_nits=float(params["target_luminance"]),
                                        channel_peak_xyz=peaks)
    assert calib.display.panel.bit_depth == 10 and conv["output_bits"] == 8


def test_sdr_white_margin_follows_the_measured_link(tmp_path: Path):
    # One definition of the SDR white's code margin serves the refine AND the T4 brightness
    # forecast: after preflight resolves an 8 bpc link over a 10-bit profile, both must see one
    # 8-bit code at white, not the profile's 10-bit one.
    calib = _make(tmp_path, "ld_margin", bit_depth=8,
                  adjudicator=MappingAdjudicator({"preflight:link-depth": Decision("use-link")}))
    _with_link(calib, 8)
    calib.stage_preflight()
    calib.target_name = calib.display.target_name(calib.mode)
    margin = calib._sdr_white_margin(110.0)
    gamma = float(calib._spec().gamma)
    assert calib.display.panel.bit_depth == 10
    assert margin["output_bits"] == 8 and margin["code_rel"] == pytest.approx(gamma / 255.0)
