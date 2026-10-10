"""OLED / public-user safety: the idle park (never leave a bright patch up while the run idles) and the
patch-window size (the daemon reports it, the run records it, preflight guards it).

The developer's LCDs (mini-LED / white-LED IPS — not emissive, no profile window, no DIP stamp) must keep
today's default path: mid-grey park, no new seam. Everything here runs against mocks — no pipe, meter,
dogegen window or display is touched."""

from __future__ import annotations

import dataclasses
import json
import socket
import threading
from pathlib import Path
from typing import Any, Optional

import pytest

from dlc import calibration_profile as cp
from dlc.adjudication import (
    SEAM_PATCH_WINDOW,
    AdjudicationRequired,
    AutoAdjudicator,
    Decision,
    MappingAdjudicator,
)
from dlc.calibrate import Calibration, CalibrationAborted, main
from dlc.controller import CalibrationController
from dlc.dip import DisplayInstrumentProfile
from dlc.dogegen_server import dispatch, patch_size_for_area, window_area_pct, window_reply
from dlc.hook_routing import park_probe_presenter
from dlc.measure_loop import (
    DogegenPresenter,
    MeasurePatch,
    SocketPresenter,
    idle_park_patch,
    park_presenter,
    parse_window_reply,
)
from dlc.runs import create_run, open_run
from test_calibrate import _CHAR, _DATE, _OPT, _SMALL, _fake_launch, _perfect_panel

LCD_TECHS = ("mini-LED IPS", "White-LED IPS", None)
OLED_TECHS = ("WOLED", "QD-OLED", "oled")


def _display(tech: Optional[str] = "mini-LED IPS", *, window: Any = None,
             quirks: Optional[dict] = None) -> cp.DisplayConfig:
    raw = {"name": "Panel", "desktoplut_monitor": 0, "panel": {"tech": tech},
           "quirks": quirks or {}}
    if window is not None:
        raw["patch_window_area_pct"] = window
    return cp._display_config(raw)


# ---------------------------------------------------------------------------
# A. the emissive / idle-park / expected-window policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tech", OLED_TECHS)
def test_oled_tech_is_emissive_and_parks_black(tech):
    d = _display(tech)
    assert d.is_emissive and d.idle_park == "black"


@pytest.mark.parametrize("tech", LCD_TECHS)
def test_lcd_tech_is_not_emissive_and_parks_mid(tech):
    d = _display(tech)
    assert not d.is_emissive and d.idle_park == "mid"


def test_idle_park_quirk_overrides_and_a_bad_value_is_ignored():
    assert _display("WOLED", quirks={"idle_park": "hold"}).idle_park == "hold"
    assert _display("mini-LED IPS", quirks={"idle_park": "Black"}).idle_park == "black"
    assert _display("WOLED", quirks={"idle_park": "sparkle"}).idle_park == "black"
    assert _display("mini-LED IPS", quirks={"idle_park": 3}).idle_park == "mid"


def test_expected_window_parses_a_number_a_per_mode_map_and_absence():
    assert _display().expected_window("SDR") is None
    both = _display(window=10)
    assert both.expected_window("SDR") == 10.0 and both.expected_window("hdr") == 10.0
    per = _display(window={"hdr": 10, "SDR": 100})
    assert per.expected_window("HDR") == 10.0 and per.expected_window("SDR") == 100.0
    assert _display(window={"HDR": 10}).expected_window("SDR") is None
    for bad in (0, 150, "x", {"XDR": 10}):
        with pytest.raises(ValueError):
            _display(window=bad)


# ---------------------------------------------------------------------------
# B/C. the park patch, the park helper, the hook-routing probe's park
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bits", (8, 10))
def test_mid_park_is_byte_identical_to_the_historical_pause_neutral(bits):
    mid = int(round(0.5 * ((1 << bits) - 1)))
    old = MeasurePatch(label="pause-neutral", rgb=(mid, mid, mid), signal=(0.5, 0.5, 0.5),
                       role="neutral_ref", bit_depth=bits)
    assert idle_park_patch("mid", bits) == old
    black = idle_park_patch("black", bits)
    assert black.rgb == (0, 0, 0) and black.signal == (0.0, 0.0, 0.0)
    assert idle_park_patch("hold", bits) is None


class _ShowOnly:
    """A presenter with ONLY the real Presenter API (``show``/``close``) — no ``present``."""

    def __init__(self, fail: bool = False):
        self.shown: list[MeasurePatch] = []
        self.fail = fail

    def show(self, patch: MeasurePatch) -> None:
        if self.fail:
            raise ConnectionResetError("daemon gone")
        self.shown.append(patch)

    def close(self) -> None:
        pass


def test_park_presenter_records_what_it_showed_and_never_raises():
    pres = _ShowOnly()
    assert park_presenter(pres, "black", 10) == {"level": "black", "parked": True, "rgb": [0, 0, 0]}
    assert pres.shown[-1].rgb == (0, 0, 0)
    assert park_presenter(pres, "hold", 10) == {"level": "hold", "parked": False}
    assert len(pres.shown) == 1                                       # hold leaves the last patch up
    rec = park_presenter(_ShowOnly(fail=True), "mid", 8)             # a dead daemon never masks the exit
    assert rec["parked"] is False and "ConnectionResetError" in rec["error"]
    assert park_presenter(None, "black", 8)["parked"] is False


def test_hook_routing_probe_parks_through_show():
    # Regression: the probe's park called a non-existent `presenter.present(...)` and swallowed the
    # AttributeError, so the panel was never parked. A show-only presenter must actually get the park.
    pres = _ShowOnly()
    rec = park_probe_presenter(pres, "black", 10)
    assert rec["parked"] is True and [p.rgb for p in pres.shown] == [(0, 0, 0)]
    assert park_probe_presenter(_ShowOnly(), "mid", 10)["rgb"] == [512, 512, 512]


# ---------------------------------------------------------------------------
# D. the daemon's `window` command (backward compatible)
# ---------------------------------------------------------------------------

def test_window_area_maths():
    assert window_area_pct(100) == 100.0 and window_area_pct(120) == 100.0
    assert window_area_pct(10) == pytest.approx(0.5625)                       # 16:9 fallback
    assert window_area_pct(42) == pytest.approx(9.9225)                       # the TV-cal "10 % window"
    assert window_area_pct(50, (0, 0, 1000, 1000)) == pytest.approx(25.0)     # a square monitor rect
    assert patch_size_for_area(10) == 42 and patch_size_for_area(100) == 100


def test_dispatch_window_reply_and_the_mode_reply_is_unchanged():
    show = lambda *a: None  # noqa: E731
    assert dispatch("window", show=show, window_info=window_reply(42)) == ("window 42 9.9225", True)
    assert dispatch("window", show=show) == ("err window unknown", True)
    assert dispatch("mode", show=show, mode_info="mode SDR 8",
                    window_info=window_reply(100)) == ("mode SDR 8", True)


def test_parse_window_reply_including_an_old_daemons_error():
    assert parse_window_reply("window 100 100.0000") == {"patch_size_pct": 100, "area_pct": 100.0}
    assert parse_window_reply("window 42 9.9225") == {"patch_size_pct": 42, "area_pct": 9.9225}
    for junk in ("err bad command: 'window'", "", "window", "window x 1", "window 42 0", "ok"):
        assert parse_window_reply(junk) is None


class _LineDaemon:
    """In-process daemon stand-in: answers each line via ``dispatch`` (``window_info=None`` mimics a
    daemon that predates the command: its real reply is ``err bad command: 'window'``)."""

    def __init__(self, window_info: Optional[str]):
        self.received: list[str] = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.window_info = window_info
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        conn, _ = self._srv.accept()
        with conn:
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode().strip()
                    self.received.append(cmd)
                    if cmd == "window" and self.window_info is None:
                        reply = f"err bad command: {cmd!r}"           # the pre-`window` daemon's reply
                    else:
                        reply, _ = dispatch(cmd, show=lambda *a: None, mode_info="mode HDR 10",
                                            window_info=self.window_info)
                    conn.sendall((reply + "\n").encode())

    def stop(self):
        self._srv.close()


@pytest.mark.parametrize("info,expected", [
    (window_reply(42), {"patch_size_pct": 42, "area_pct": 9.9225}),
    (None, None),                                                    # an older daemon → unknown
])
def test_socket_presenter_query_window_and_the_connection_stays_in_sync(info, expected):
    daemon = _LineDaemon(info)
    try:
        pres = SocketPresenter("127.0.0.1", daemon.port, settle_seconds=0.0)
        assert pres.query_window() == expected
        assert pres.query_mode() == {"mode": "HDR", "bit_depth": 10}  # the next reply is still its own
        pres.close()
    finally:
        daemon.stop()


def test_spawned_presenter_reports_its_own_window():
    assert DogegenPresenter(object()).query_window() == {"patch_size_pct": 100, "area_pct": 100.0}
    assert DogegenPresenter(object(), patch_size=42).query_window()["area_pct"] == pytest.approx(9.9225)


# ---------------------------------------------------------------------------
# E. record + guard at preflight (the preflight:patch-window seam firing matrix)
# ---------------------------------------------------------------------------

def _profile(tmp_path: Path, tech: Optional[str], window: Any = None) -> cp.Profile:
    prof = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    d = prof.displays[0]
    win = cp._patch_window_area(window)
    return dataclasses.replace(prof, displays=(dataclasses.replace(
        d, panel=dataclasses.replace(d.panel, tech=tech), patch_window_area_pct=win),))


def _calib(tmp_path: Path, name: str, *, tech: Optional[str] = "mini-LED IPS", window: Any = None,
           report: Optional[dict] = None, adjudicator=None, mode: str = "SDR",
           bit_depth: Optional[int] = None, characterize_config=None) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run(mode, display="synthetic", run_dir=run_dir)
    return Calibration(
        ctx=ctx, profile=_profile(tmp_path, tech, window), monitor=0, mode=mode,
        controller=CalibrationController.mock(), measure=_perfect_panel(),
        adjudicator=adjudicator or MappingAdjudicator({}), optimize_config=_OPT,
        patch_sizes=_SMALL, run_date=_DATE, probe_launcher=_fake_launch, bit_depth=bit_depth,
        characterize_config=characterize_config, patch_window_size=report)


FULL = {"patch_size_pct": 100, "area_pct": 100.0, "source": "daemon"}
TEN = {"patch_size_pct": 42, "area_pct": 9.9225, "source": "daemon"}
UNKNOWN = {"source": "daemon", "reason": "the dogegen daemon did not report its window"}


@pytest.mark.parametrize("tech,report", [
    ("mini-LED IPS", None),                       # sim / no presenter: recorded unknown, no seam
    ("mini-LED IPS", UNKNOWN),                    # an older daemon on the PA
    ("mini-LED IPS", FULL),                       # the PA's production default (full field)
    ("White-LED IPS", FULL),                      # the BenQ
    ("WOLED", TEN),                               # an OLED at an ABL-safe window
])
def test_default_paths_only_record_the_window(tmp_path: Path, tech, report):
    calib = _calib(tmp_path, "rec", tech=tech, report=report)
    out = calib.stage_preflight()                 # MappingAdjudicator({}): ANY seam would raise
    ws = out.digest["patch_window_size"]
    assert ws["seam"] is False and ws["reasons"] == []
    rec = calib.calib["patch_window_size"]
    assert rec["known"] is bool(report and "area_pct" in report)
    assert rec["area_pct"] == (report or {}).get("area_pct")


@pytest.mark.parametrize("tech,window,report,code,recommendation", [
    ("mini-LED IPS", 10, FULL, "profile_mismatch", "abort"),
    ("WOLED", 10, FULL, "profile_mismatch", "abort"),
    ("WOLED", None, FULL, "emissive_full_field", "abort"),
    ("QD-OLED", None, UNKNOWN, "unknown_on_emissive", "proceed"),
    ("QD-OLED", None, None, "unknown_on_emissive", "proceed"),
])
def test_patch_window_seam_fires(tmp_path: Path, tech, window, report, code, recommendation):
    calib = _calib(tmp_path, "seam", tech=tech, window=window, report=report)
    with pytest.raises(AdjudicationRequired) as exc:
        calib.stage_preflight()
    req = exc.value.request
    assert req.seam == SEAM_PATCH_WINDOW and req.key == "preflight:patch-window"
    assert req.options == ("proceed", "abort") and req.recommendation == recommendation
    assert [r["code"] for r in req.digest["reasons"]] == [code]
    assert req.digest["compromised"] is True and "--patch-size" in req.question


def test_profile_window_within_tolerance_is_no_seam(tmp_path: Path):
    calib = _calib(tmp_path, "tol", tech="WOLED", window=10, report=TEN)   # 9.92 % vs 10 %
    assert calib.stage_preflight().digest["patch_window_size"]["seam"] is False


def test_the_decision_is_honoured(tmp_path: Path):
    ok = _calib(tmp_path, "proceed", tech="WOLED", report=FULL,
                adjudicator=MappingAdjudicator({"preflight:patch-window": Decision("proceed")}))
    assert ok.stage_preflight().digest["patch_window_size"]["full_field"] is True
    stop = _calib(tmp_path, "abort", tech="WOLED", report=FULL,
                  adjudicator=MappingAdjudicator({"preflight:patch-window": Decision("abort")}))
    with pytest.raises(CalibrationAborted):
        stop.stage_preflight()


def _seed_dip(calib: Calibration, area: Optional[float]) -> None:
    store = calib._dip_store()
    store.record(DisplayInstrumentProfile(display=calib.display.name, mode=calib.mode, made="2026-06-16",
                                          patch_window_area_pct=area))


def test_dip_window_mismatch_fires_and_a_match_or_unstamped_dip_does_not(tmp_path: Path):
    c = _calib(tmp_path, "dip_mm", report=TEN)
    _seed_dip(c, 100.0)
    with pytest.raises(AdjudicationRequired) as exc:
        c.stage_preflight()
    assert [r["code"] for r in exc.value.request.digest["reasons"]] == ["dip_mismatch"]
    assert exc.value.request.recommendation == "proceed"
    same = _calib(tmp_path, "dip_ok", report=FULL)
    _seed_dip(same, 100.0)
    assert same.stage_preflight().digest["patch_window_size"]["dip_area_pct"] == 100.0
    legacy = _calib(tmp_path, "dip_legacy", report=TEN)
    _seed_dip(legacy, None)                       # a pre-field DIP record: never a mismatch
    assert legacy.stage_preflight().digest["patch_window_size"]["seam"] is False


def test_dip_record_round_trips_the_window_and_old_records_load():
    dip = DisplayInstrumentProfile(display="X", mode="HDR", patch_window_area_pct=9.9225)
    assert DisplayInstrumentProfile.from_dict(dip.as_dict()).patch_window_area_pct == 9.9225
    old = dip.as_dict()
    old.pop("patch_window_area_pct")
    assert DisplayInstrumentProfile.from_dict(old).patch_window_area_pct is None


def test_characterize_stamps_the_dip_with_the_reported_window(tmp_path: Path):
    calib = _calib(tmp_path, "char", report=TEN, adjudicator=AutoAdjudicator(), characterize_config=_CHAR)
    assert calib.run("characterize").status == "completed"
    assert calib._dip().patch_window_area_pct == pytest.approx(9.9225)


def test_transport_tell_never_advises_full_field_for_an_emissive_panel(tmp_path: Path):
    oled = _calib(tmp_path, "tt_oled", tech="WOLED", bit_depth=8)
    oled.calib["flow"] = "full"
    tell = oled._transport_tell(10)
    assert tell["emissive"] is True and "--patch-size 42" in tell["window_advice"]
    assert "fullscreen" not in tell["warning"] and "do NOT measure full field" in tell["warning"]
    lcd = _calib(tmp_path, "tt_lcd", tech="mini-LED IPS", bit_depth=8)
    lcd.calib["flow"] = "full"
    lcd_tell = lcd._transport_tell(10)
    assert "emissive" not in lcd_tell and "fullscreen" in lcd_tell["warning"]   # LCD advice unchanged
    sized = _calib(tmp_path, "tt_sized", tech="WOLED", window={"SDR": 25}, bit_depth=10)
    assert "--patch-size 67" in sized._transport_tell()["window_advice"]


# ---------------------------------------------------------------------------
# B. the live CLI parks on a seam exit / a kept daemon at exit (main() over mocks)
# ---------------------------------------------------------------------------

class _FakeSocketPresenter:
    """Stands in for SocketPresenter inside main(): records every show/close/shutdown."""

    window: Optional[dict] = None
    instances: list["_FakeSocketPresenter"] = []

    def __init__(self, host, port, *, settle_seconds=0.5, timeout=30.0):
        self.events: list[tuple] = []
        type(self).instances.append(self)

    def query_mode(self):
        return None

    def query_window(self):
        return type(self).window

    def show(self, patch):
        self.events.append(("show", patch.label, patch.rgb))

    def close(self):
        self.events.append(("close",))

    def shutdown_daemon(self):
        self.events.append(("shutdown",))


def _run_main(tmp_path, monkeypatch, capsys, *, tech, window, extra=()):
    import dlc.link_format
    import dlc.measure_loop
    ctrl = CalibrationController.mock()
    monkeypatch.setattr(cp, "load_profile", lambda *a, **k: _profile(tmp_path, tech))
    monkeypatch.setattr(CalibrationController, "connect", classmethod(lambda cls, *a, **k: ctrl))
    monkeypatch.setattr(dlc.measure_loop, "SocketPresenter", _FakeSocketPresenter)
    monkeypatch.setattr(dlc.link_format, "probe_link_formats", lambda *a, **k: {})
    monkeypatch.setattr(_FakeSocketPresenter, "window", window)
    monkeypatch.setattr(_FakeSocketPresenter, "instances", [])
    rc = main(["--flow", "full", "--monitor", "0", "--mode", "SDR", "--dogegen-server", "127.0.0.1:9",
               "--legacy-meter", "--run", str(tmp_path / "run"), *extra])
    out = capsys.readouterr().out
    return rc, out, _FakeSocketPresenter.instances[-1].events


def test_seam_exit_parks_black_on_an_oled_before_dropping_the_socket(tmp_path, monkeypatch, capsys):
    rc, out, events = _run_main(tmp_path, monkeypatch, capsys, tech="WOLED",
                                window={"patch_size_pct": 100, "area_pct": 100.0})
    assert rc == 10
    assert events[-2:] == [("show", "idle-park-black", (0, 0, 0)), ("close",)]
    payload = json.loads(out[out.index('{\n  "status": "adjudication_required"'):])
    assert payload["idle_park"]["level"] == "black" and payload["idle_park"]["why"] == "seam"
    assert payload["request"]["key"] == "preflight:patch-window"         # full field on an OLED → seam
    state = json.loads((tmp_path / "run" / "dlc_state.json").read_text(encoding="utf-8"))
    assert state["calib"]["patch_window_size"]["area_pct"] == 100.0


def test_seam_exit_on_an_lcd_parks_the_historical_mid_grey(tmp_path, monkeypatch, capsys):
    rc, _out, events = _run_main(tmp_path, monkeypatch, capsys, tech="mini-LED IPS", window=None)
    assert rc == 10
    assert events[-2:] == [("show", "pause-neutral", (128, 128, 128)), ("close",)]


def test_kept_daemon_is_parked_at_a_terminal_exit_and_a_stopped_one_is_not(tmp_path, monkeypatch, capsys):
    abort = ("--decide", "preflight:patch-window=abort")
    full = {"patch_size_pct": 100, "area_pct": 100.0}
    rc, _o, kept = _run_main(tmp_path, monkeypatch, capsys, tech="WOLED", window=full,
                             extra=(*abort, "--keep-dogegen-server"))
    assert rc == 1 and kept[-2:] == [("show", "idle-park-black", (0, 0, 0)), ("close",)]
    rc, _o, stopped = _run_main(tmp_path / "b", monkeypatch, capsys, tech="WOLED", window=full,
                                extra=abort)
    assert rc == 1 and stopped == [("shutdown",)]


# ---------------------------------------------------------------------------
# C. review fixes (2026-10-10): no park through a spawned window; a resume re-checks the window
# ---------------------------------------------------------------------------

class _FakeSpawnedPresenter:
    """A spawned (non-daemon) dogegen window inside main(): it has no ``shutdown_daemon``."""

    instances: list["_FakeSpawnedPresenter"] = []

    def __init__(self, display, *, settle_seconds=0.5, place_rect=None):
        self.events: list[tuple] = []
        type(self).instances.append(self)

    def query_window(self):
        return {"patch_size_pct": 100, "area_pct": 100.0}

    def show(self, patch):
        self.events.append(("show", patch.label, patch.rgb))

    def close(self):
        self.events.append(("close",))


def test_seam_exit_with_a_spawned_window_does_not_open_one_just_to_park_it(tmp_path, monkeypatch, capsys):
    import dlc.link_format
    import dlc.measure_loop
    ctrl = CalibrationController.mock()
    prof = _profile(tmp_path, "WOLED")
    prof = dataclasses.replace(prof, paths={**prof.paths, "dogegen": str(tmp_path / "dogegen.exe")})
    monkeypatch.setattr(cp, "load_profile", lambda *a, **k: prof)
    monkeypatch.setattr(CalibrationController, "connect", classmethod(lambda cls, *a, **k: ctrl))
    monkeypatch.setattr(dlc.measure_loop, "DogegenPresenter", _FakeSpawnedPresenter)   # main() imports it locally
    monkeypatch.setattr(dlc.link_format, "probe_link_formats", lambda *a, **k: {})
    monkeypatch.setattr(_FakeSpawnedPresenter, "instances", [])
    rc = main(["--flow", "full", "--monitor", "0", "--mode", "SDR", "--legacy-meter",
               "--run", str(tmp_path / "run")])
    out = capsys.readouterr().out
    assert rc == 10                                   # OLED full field → preflight:patch-window
    events = _FakeSpawnedPresenter.instances[-1].events
    assert not any(e[0] == "show" for e in events) and events[-1] == ("close",)
    payload = json.loads(out[out.index('{\n  "status": "adjudication_required"'):])
    assert "idle_park" not in payload


def _recorded(tmp_path: Path, name: str, report: dict) -> None:
    first = _calib(tmp_path, name, tech="WOLED", window=10, report=report)
    first.stage_preflight()
    first._save()


def test_a_resume_rechecks_the_window_against_the_one_preflight_recorded(tmp_path: Path):
    _recorded(tmp_path, "resume", TEN)
    # The daemon was restarted at full field between invocations.
    again = _calib(tmp_path, "resume", tech="WOLED", window=10, report=FULL)
    with pytest.raises(AdjudicationRequired) as exc:
        again._patch_window_resume_check()
    req = exc.value.request
    assert req.seam == SEAM_PATCH_WINDOW and req.key == "preflight:patch-window-changed:9.9225:100"
    assert req.options == ("proceed", "abort") and req.recommendation == "abort"
    assert req.digest["recorded_area_pct"] == 9.9225 and req.digest["area_pct"] == 100.0
    # proceed → the record follows the new window; the same window on the next resume is a no-op
    ok = _calib(tmp_path, "resume", tech="WOLED", window=10, report=FULL,
                adjudicator=MappingAdjudicator({req.key: Decision("proceed")}))
    ok._patch_window_resume_check()
    assert ok.calib["patch_window_size"]["area_pct"] == 100.0
    assert ok.calib["patch_window_size"]["changed_from_area_pct"] == 9.9225
    later = _calib(tmp_path, "resume", tech="WOLED", window=10, report=FULL)
    later._patch_window_resume_check()               # MappingAdjudicator({}) would raise on any seam


@pytest.mark.parametrize("report", [TEN, UNKNOWN, None])
def test_resume_check_is_mechanical_when_unchanged_or_unknown(tmp_path: Path, report):
    _recorded(tmp_path, "same", TEN)
    _calib(tmp_path, "same", tech="WOLED", window=10, report=report)._patch_window_resume_check()


def test_resume_check_is_a_noop_before_preflight_recorded_anything(tmp_path: Path):
    _calib(tmp_path, "fresh", tech="WOLED", window=10, report=FULL)._patch_window_resume_check()
