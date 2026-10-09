"""Offline tests for the phone evidence trail: generic camera-request parsing (every key, vendor tags, multi-line
arrays, CRLF, previous-session blocks), the clip-to-clip evidence diff, and the Blackmagic ISO walk.

Background: on 2026-10-08 the camera's response curve changed mid-session and the manifests could not say why - they
kept a few HAL fields, not the request's processing modes or vendor tags. These tests pin the fix.
"""

from __future__ import annotations

from dlc.phone import evidence as ev
from dlc.phone.blackmagic import iso_walk
from dlc.phone.camservice import REQUEST_KEYS, parse_camera_dump

# A dump shaped like CameraMetadata::dump (verbosity 2): preamble lines, byte enums, multi-value arrays wrapping over
# several "[...]" lines, rationals, vendor tags (resolved and unresolved), a physical-camera request, then a section
# header, then the PREVIOUS session (different values) which must be ignored. Line endings are CRLF.
LIVE = """== Camera device 0 dynamic info: ==
  10-08 18:03:11 : Device 0 is open. Client instance dump:
    Client package: com.blackmagicdesign.android.blackmagiccam
  Device dump:
    Operation mode: NORMAL (0)
    Stream[0]: Output
      Dims: 3840 x 2160, format 0x22, dataspace 0x8c20000
      Physical camera id: 5
    Logical request settings:
      Dumping camera metadata array: 6 / 142 entries, 300 / 4000 bytes of extra data.
        Version: 1, Flags: 00000000
        android.control.aeMode (10003): byte[1]
          [OFF ]
        android.control.aeTargetFpsRange (10005): int32[2]
          [60 60 ]
        android.sensor.exposureTime (e0000): int64[1]
          [16666666 ]
        android.sensor.sensitivity (e0002): int32[1]
          [400 ]
        android.tonemap.mode (1a0003): byte[1]
          [FAST ]
        android.colorCorrection.gains (40002): float[4]
          [1.87500000 1.00000000 1.00000000 2.13281250 ]
        android.colorCorrection.transform (40001): rational[9]
          [(128 / 128) (0 / 128) ]
          [(0 / 128) (0 / 128) ]
          [(128 / 128) (0 / 128) ]
          [(0 / 128) (0 / 128) ]
          [(128 / 128) ]
        android.tonemap.curveRed (1a0000): float[10]
          [0.00000000 0.00000000 0.25000000 0.31000000 0.50000000 0.55000000 0.75000000 0.80000000 ]
          [1.00000000 1.00000000 ]
        samsung.android.control.liveHdrState (80020003): int32[1]
          [2 ]
        com.samsung.android.control.metaMode (80030001): int32[4]
          [0 1 0 7 ]
        unknownSection.unknownTag (8004000a): byte[2]
          [1 0 ]
        unknownSection.unknownTag (8004000b): byte[1]
          [5 ]
    Physical request settings for camera id 5:
      Dumping camera metadata array: 2 / 50 entries, 0 / 100 bytes of extra data.
        Version: 1, Flags: 00000000
        android.sensor.sensitivity (e0002): int32[1]
          [400 ]
        android.noiseReduction.mode (e0000): byte[1]
          [HIGH_QUALITY ]
    Frame processor:
      android.sensor.sensitivity (e0002): int32[1]
          [9999 ]
== Camera service events log (most recent at top): ==
  10-08 18:03:10 : CONNECT device 0 client for package com.blackmagicdesign.android.blackmagiccam
**********Dumpsys from previous open session**********
    Operation mode: CONSTRAINED_HIGH_SPEED (1)
      Dims: 640 x 480, format 0x22, dataspace 0x1
    Logical request settings:
        android.sensor.sensitivity (e0002): int32[1]
          [3200 ]
        android.tonemap.mode (1a0003): byte[1]
          [HIGH_QUALITY ]
        samsung.android.control.liveHdrState (80020003): int32[1]
          [0 ]
""".replace("\n", "\r\n")


def test_generic_request_parse_keeps_every_key():
    s = parse_camera_dump(LIVE)
    r = s.request
    assert s.open and s.client.startswith("com.blackmagicdesign") and s.opmode == "NORMAL"
    assert not s.high_speed                                         # previous session's HS opmode ignored
    assert [x["w"] for x in s.streams] == [3840]                    # previous session's 640x480 stream ignored
    # curated accessors still work
    assert s.iso() == 400 and s.exposure_s() == 16666666e-9 and s.target_fps() == (60, 60) and s.manual_exposure()
    assert r["tonemap.mode"] == "FAST"                              # NOT the previous session's HIGH_QUALITY
    # multi-value on one line, and entries wrapping over several value lines (joined, whitespace-normalised)
    assert r["colorCorrection.gains"] == "1.87500000 1.00000000 1.00000000 2.13281250"
    assert r["colorCorrection.transform"].split(") (")[0] == "(128 / 128"
    assert r["colorCorrection.transform"].count("/") == 9
    assert len(r["tonemap.curveRed"].split()) == 10 and r["tonemap.curveRed"].endswith("1.00000000 1.00000000")
    # vendor tags kept with their full names; unresolved tags kept apart by tag id
    assert r["samsung.android.control.liveHdrState"] == "2"
    assert r["com.samsung.android.control.metaMode"] == "0 1 0 7"
    assert r["unknownSection.unknownTag@8004000a"] == "1 0" and r["unknownSection.unknownTag@8004000b"] == "5"
    # no CR left anywhere, no preamble lines taken as keys, nothing after the block (Frame processor's 9999)
    assert not any("\r" in k or "\r" in v for k, v in r.items())
    assert len(r) == 12 and s.iso() != 9999
    # the physical camera's own request is parsed the same way
    assert s.physical_requests == {"5": {"sensor.sensitivity": "400", "noiseReduction.mode": "HIGH_QUALITY"}}
    assert set(s.curated()) <= set(REQUEST_KEYS) and s.curated()["tonemap.mode"] == "FAST"


def test_previous_session_only_means_no_live_request():
    prev_only = LIVE.split("**********Dumpsys")[1]
    s = parse_camera_dump("== Camera device 0 dynamic info: ==\r\n  Device 0 is closed, no client instance\r\n"
                          "**********Dumpsys" + prev_only)
    assert not s.open and s.request == {} and s.iso() is None and s.streams == []


def _state(**req):
    base = {"control.aeMode": "OFF", "sensor.sensitivity": "400", "sensor.exposureTime": "16666666",
            "tonemap.mode": "FAST", "samsung.android.control.liveHdrState": "2"}
    base.update(req)
    return dict(hal=dict(open=True, client="bm", opmode="NORMAL", streams=[{"w": 3840}], request=base,
                         physical_requests={"5": {"sensor.sensitivity": base["sensor.sensitivity"]}}),
                app={"/video/iso": {"iso": int(base["sensor.sensitivity"])}, "/video/shutter": {"shutterSpeed": 60},
                     "/video/whiteBalance": {"whiteBalance": 5600, "tint": 0},
                     "/system/format": {"codec": "H265", "recordResolution": {"width": 3840, "height": 2160}}})


def test_evidence_diff_excludes_requested_exposure_only():
    a = ev.flatten(_state())
    assert a["hal.request.tonemap.mode"] == "FAST" and a["app./system/format.recordResolution.width"] == 3840
    b = ev.flatten(_state(**{"sensor.sensitivity": "3200", "sensor.exposureTime": "4166666"}))
    b["app./video/shutter.shutterSpeed"] = 240
    assert ev.diff(a, b) == {}                                      # exposure moved on purpose: not evidence
    c = ev.flatten(_state(**{"samsung.android.control.liveHdrState": "0", "tonemap.mode": "HIGH_QUALITY"}))
    c["app./video/whiteBalance.tint"] = 3
    d = ev.diff(a, c)
    assert d == {"hal.request.samsung.android.control.liveHdrState": ["2", "0"],
                 "hal.request.tonemap.mode": ["FAST", "HIGH_QUALITY"], "app./video/whiteBalance.tint": [0, 3]}
    line = ev.describe(d)
    assert "tonemap.mode: FAST -> HIGH_QUALITY" in line and "liveHdrState: 2 -> 0" in line


def test_evidence_diff_closed_hal_is_one_change_not_hundreds():
    a = ev.flatten(_state())
    closed = ev.flatten(dict(hal=dict(open=False, request={}), app=_state()["app"]))
    assert ev.diff(a, closed) == {"hal.open": [True, False]}
    assert ev.diff({}, a) == {} and ev.diff(a, {}) == {}            # nothing to compare -> no noise


def test_iso_walk_third_stop_steps():
    opts = [25, 32, 40, 50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 2500,
            3200]
    usable = [v for v in opts if v >= 40]
    assert iso_walk(3200, 400, usable) == [2500, 2000, 1600, 1250, 1000, 800, 640, 500, 400]
    assert iso_walk(400, 800, usable) == [500, 640, 800]
    assert iso_walk(396, 400, usable) == []                         # HAL gain quantisation: already there
    assert iso_walk(379, 400, usable) == [400]                      # stalled ISO (HAL 379 for 400): re-send once
    assert iso_walk(3182, 40, usable)[-1] == 40 and iso_walk(3182, 40, usable)[0] == 2500   # AE value snapped first
    assert iso_walk(None, 800, usable) == [800]                     # unknown current: a single PUT
