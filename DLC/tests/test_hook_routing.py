"""DWM-hook LUT routing self-check (dlc.hook_routing) — the 2026-09-03 incident guard.

The hook order-matches twin panels on 25H2 and used to re-roll on every set_3dlut, so a whole
3dlut-only run's optimizer probes + verify measured the UNCORRECTED panel. These tests pin the
mechanical proof: a probe cube on the calibrated slot must move the meter; if it does not the
twin assignment is swapped ONCE; if it still does not the run is refused. The previous cube is
restored whatever happens, and the probe never survives the check.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from dlc import hook_routing as hr
from dlc.controller import CalibrationController
from dlc.desktoplut_client import DesktopLutApiError
from dlc.lut_integrity import parse_cube
from dlc.measure_loop import MeasurePatch, Reading

MON, MODE, KEY = 0, "HDR", "0:HDR"
GREY_XYZ = (95.047, 100.0, 108.883)     # D65 mid grey (x 0.3127)
MAGENTA_XYZ = (80.0, 55.0, 95.0)        # the probe's green-halved grey (x 0.348, Y -45 %)


def _twin_hook(*, confirmed=False, method="order", stale=False, monitor_map=(0, 1)) -> dict:
    entries = [{"ctx": f"0x1F3A000{i}", "left": 3840 * i, "top": 0, "method": method, "monitor": m}
               for i, m in enumerate(monitor_map)]
    needs = stale or (method in ("order", "pinned", "replaced") and not confirmed) or method == "provisional"
    return {"active": True, "needs_check": needs,
            "routing": {"session": "4242-1337", "stale": stale, "confirmed": confirmed, "entries": entries}}


class FakeController:
    """A twin-panel host: ``crossed`` = the order-match landed monitor 0's cube on the twin
    (a swap flips it); ``dead`` = the hook paints nothing on this panel whichever way."""

    def __init__(self, hook=None, prev_cube=None, *, crossed=False, dead=False, swap_error=None):
        self.hook = deepcopy(hook) if hook is not None else _twin_hook()
        self.runtime = {KEY: {"cube_path": prev_cube}} if prev_cube else {}
        self.crossed = crossed
        self.dead = dead
        self.swap_error = swap_error
        self.calls: list[tuple] = []

    def state(self):
        st = {"runtime": deepcopy(self.runtime)}
        if self.hook is not None:
            st["hook"] = deepcopy(self.hook)
        return st

    def hook_state(self):
        return deepcopy(self.hook)

    def set_3dlut(self, monitor, mode, cube_path):
        self.calls.append(("set_3dlut", monitor, mode, str(cube_path)))
        self.runtime[f"{monitor}:{mode}"] = {"cube_path": str(cube_path)}
        return {}

    def clear_3dlut(self, monitor, mode):
        self.calls.append(("clear_3dlut", monitor, mode))
        self.runtime.pop(f"{monitor}:{mode}", None)
        return {}

    def set_hook_routing(self, action, monitor=None, entries=None):
        self.calls.append(("set_hook_routing", action, monitor))
        routing = self.hook["routing"]
        if action == "swap":
            if self.swap_error:
                raise DesktopLutApiError(self.swap_error)
            self.crossed = not self.crossed
            for e in routing["entries"]:
                e["method"] = "pinned"
            routing["confirmed"] = False
            self.hook["needs_check"] = True
            return {"hook": deepcopy(self.hook), "reinjected": True}
        if action == "confirm":
            routing["confirmed"] = True
            self.hook["needs_check"] = False
            return {"hook": deepcopy(self.hook), "reinjected": False}
        raise AssertionError(f"unexpected action {action}")

    # -- the optics: does the calibrated slot's cube reach the measured panel? --------
    def probe_reaches_panel(self) -> bool:
        cube = (self.runtime.get(KEY) or {}).get("cube_path") or ""
        return cube.endswith(hr.PROBE_CUBE_NAME) and not self.crossed and not self.dead

    def measure(self, patch: MeasurePatch) -> Reading:
        assert patch.rgb == (512, 512, 512) and patch.signal == (0.5, 0.5, 0.5)
        assert patch.role == "measurement" and patch.bit_depth == 10
        return Reading(xyz=MAGENTA_XYZ if self.probe_reaches_panel() else GREY_XYZ)


def _run(ctrl: FakeController, tmp_path: Path, policy="auto", log=None):
    return hr.run_hook_routing_check(ctrl, MON, MODE, 10, ctrl.measure, tmp_path / "generated",
                                     policy=policy, log=log, settle_s=0)


def _names(ctrl):
    return [c[0] if c[0] != "set_hook_routing" else f"routing:{c[1]}" for c in ctrl.calls]


# ---------------------------------------------------------------------------
# run_hook_routing_check
# ---------------------------------------------------------------------------
def test_correct_routing_confirms_without_a_swap_and_restores_the_previous_cube(tmp_path):
    prev = str(tmp_path / "user_final.cube")
    ctrl = FakeController(prev_cube=prev)
    lines: list[str] = []
    res = _run(ctrl, tmp_path, log=lines.append)
    assert res.checked and res.verdict == "confirmed"
    assert res.confirmed is True and res.swapped is False
    assert len(res.legs) == 1 and res.legs[0]["effect"] is True
    assert res.legs[0]["dx"] > hr.EFFECT_MIN_DX and res.legs[0]["dY_rel"] < -hr.EFFECT_MIN_DY_REL
    assert _names(ctrl) == ["set_3dlut", "clear_3dlut", "routing:confirm", "set_3dlut"]
    assert ctrl.runtime[KEY]["cube_path"] == prev          # previous cube back
    assert res.previous_cube == prev and res.restored is True
    assert Path(res.probe_cube).exists()
    assert res.hook_after["routing"]["confirmed"] is True
    assert any("confirmed" in ln for ln in lines)


def test_crossed_routing_swaps_once_then_confirms(tmp_path):
    prev = str(tmp_path / "user_final.cube")
    ctrl = FakeController(prev_cube=prev, crossed=True)
    res = _run(ctrl, tmp_path)
    assert res.checked and res.swapped is True and res.confirmed is True
    assert res.verdict == "swapped"
    assert [leg["effect"] for leg in res.legs] == [False, True]
    assert res.legs[0]["leg"] == "first" and res.legs[1]["leg"] == "after-swap"
    assert _names(ctrl) == ["set_3dlut", "clear_3dlut", "routing:swap",
                            "set_3dlut", "clear_3dlut", "routing:confirm", "set_3dlut"]
    assert ctrl.calls[2] == ("set_hook_routing", "swap", MON)   # swap names the calibrated monitor
    assert ctrl.crossed is False
    assert ctrl.runtime[KEY]["cube_path"] == prev


def test_dead_hook_refuses_after_one_swap_and_restores(tmp_path):
    prev = str(tmp_path / "user_final.cube")
    ctrl = FakeController(prev_cube=prev, dead=True)
    with pytest.raises(hr.HookRoutingError, match="not rendering a cube") as exc:
        _run(ctrl, tmp_path)
    res = exc.value.result
    assert res.checked and res.verdict == "no_effect"
    assert res.swapped is True and res.confirmed is False
    assert [leg["effect"] for leg in res.legs] == [False, False]
    assert _names(ctrl).count("routing:swap") == 1
    assert "routing:confirm" not in _names(ctrl)
    assert ctrl.runtime[KEY]["cube_path"] == prev            # previous cube restored on refusal
    assert res.restored is True


def test_no_previous_cube_leaves_the_slot_clear(tmp_path):
    ctrl = FakeController()
    res = _run(ctrl, tmp_path)
    assert res.verdict == "confirmed" and res.previous_cube is None
    assert KEY not in ctrl.runtime                             # probe removed, nothing re-installed
    # the OFF read already cleared the slot: no gratuitous extra re-injection
    assert _names(ctrl) == ["set_3dlut", "clear_3dlut", "routing:confirm"]


def test_swap_unavailable_is_a_refusal_with_the_reason(tmp_path):
    ctrl = FakeController(crossed=True, swap_error="monitor 0 has no single same-size twin")
    with pytest.raises(hr.HookRoutingError, match="could not be swapped") as exc:
        _run(ctrl, tmp_path)
    assert exc.value.result.verdict == "swap_failed"
    assert exc.value.result.swapped is False
    assert KEY not in ctrl.runtime


def test_failed_meter_read_is_a_refusal_not_a_false_no_effect(tmp_path):
    ctrl = FakeController()
    ctrl.measure = lambda patch: Reading(xyz=None, ok=False, error="meter timeout")  # type: ignore[assignment]
    with pytest.raises(hr.HookRoutingError, match="read failed") as exc:
        _run(ctrl, tmp_path)
    assert exc.value.result.verdict == "read_failed"
    assert "routing:swap" not in _names(ctrl)                  # a dead meter must not trigger a swap


def test_policy_never_skips_and_unambiguous_report_skips(tmp_path):
    ctrl = FakeController()
    res = _run(ctrl, tmp_path, policy="never")
    assert res.checked is False and ctrl.calls == [] and "never" in res.reason
    ctrl = FakeController(hook=_twin_hook(method="unique"))
    res = _run(ctrl, tmp_path)
    assert res.checked is False and ctrl.calls == [] and "unambiguous" in res.reason
    # 'always' proves it even when the report is unambiguous
    res = _run(ctrl, tmp_path, policy="always")
    assert res.checked is True and res.verdict == "confirmed"


def test_confirm_verb_missing_on_old_build_is_a_note_not_a_failure(tmp_path):
    ctrl = FakeController()

    def no_verb(action, monitor=None, entries=None):
        raise DesktopLutApiError("unknown method: hook.set_routing")
    ctrl.set_hook_routing = no_verb  # type: ignore[assignment]
    res = _run(ctrl, tmp_path)
    assert res.verdict == "confirmed" and res.confirmed is False
    assert any("confirm not recorded" in n for n in res.notes)


# ---------------------------------------------------------------------------
# routing_needs_check decision table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hook, monitor, policy, expected, fragment", [
    (None, 0, "never", False, "never"),
    (None, 0, "always", True, "always"),
    (_twin_hook(method="unique"), 0, "always", True, "always"),
    (None, 0, "auto", True, "old DesktopLUT"),
    ({"active": True, "needs_check": False}, 0, "auto", True, "no routing yet"),
    ({"active": False, "needs_check": False, "routing": {"stale": False, "confirmed": False, "entries": []}},
     0, "auto", True, "not injected"),
    (_twin_hook(stale=True), 0, "auto", True, "STALE"),
    (_twin_hook(method="order"), 0, "auto", True, "order-matched"),
    (_twin_hook(method="pinned"), 0, "auto", True, "pinned-matched"),
    (_twin_hook(method="order", confirmed=True), 0, "auto", False, "confirmed"),
    (_twin_hook(method="replaced"), 0, "auto", True, "replaced-matched"),
    (_twin_hook(method="replaced", confirmed=True), 0, "auto", False, "confirmed"),
    (_twin_hook(method="provisional"), 0, "auto", True, "provisional"),
    (_twin_hook(method="provisional", confirmed=True), 0, "auto", True, "provisional"),
    (_twin_hook(method="unique"), 0, "auto", False, "unambiguous"),
    (_twin_hook(method="unique"), 1, "auto", False, "unambiguous"),
    (_twin_hook(method="unique", monitor_map=(1, None)), 0, "auto", True, "no routing entry maps to monitor 0"),
])
def test_routing_needs_check_table(hook, monitor, policy, expected, fragment):
    needed, reason = hr.routing_needs_check(hook, monitor, policy)
    assert needed is expected, reason
    assert fragment in reason


def test_ambiguous_reason_names_the_entries():
    hook = _twin_hook(method="order")
    needed, reason = hr.routing_needs_check(hook, 1)
    assert needed and "0x1F3A0001" in reason and "(3840,0)" in reason


def test_bad_policy_rejected():
    with pytest.raises(ValueError):
        hr.routing_needs_check(None, 0, "maybe")


# ---------------------------------------------------------------------------
# the probe cube
# ---------------------------------------------------------------------------
def test_probe_cube_parses_and_halves_green(tmp_path):
    path = hr.write_probe_cube(tmp_path / "probe.cube", size=9)
    cube = parse_cube(path)
    assert not cube.parse_errors
    assert cube.size == 9 and len(cube.values) == 9 ** 3
    text = path.read_text(encoding="ascii")
    assert text.startswith("TITLE ") and "LUT_3D_SIZE 9" in text
    # rows are RED-fastest: row 1 is (r=1/8, 0, 0)
    assert cube.values[1] == pytest.approx((0.125, 0.0, 0.0))
    # a grey node sits at index i*(1 + n + n²) and maps to (v, 0.5 v, v)
    n = 9
    for i in range(n):
        v = i / (n - 1)
        assert cube.values[i * (1 + n + n * n)] == pytest.approx((v, 0.5 * v, v), abs=1e-6)


def test_leg_effect_thresholds():
    grey = {"Y": 100.0, "x": 0.3127, "y": 0.3290}
    assert hr.leg_effect(grey, grey) == (False, {"dx": 0.0, "dy": 0.0, "dY_rel": 0.0})
    # meter noise / thermal drift on a mid grey never reaches the thresholds
    assert hr.leg_effect({"Y": 102.0, "x": 0.3150, "y": 0.3300}, grey)[0] is False
    assert hr.leg_effect({"Y": 100.0, "x": 0.3250, "y": 0.3290}, grey)[0] is True
    assert hr.leg_effect({"Y": 85.0, "x": 0.3127, "y": 0.3290}, grey)[0] is True


# ---------------------------------------------------------------------------
# the mock transport's hook state + hook.set_routing round-trip
# ---------------------------------------------------------------------------
def test_mock_hook_state_and_set_routing_round_trip():
    ctrl = CalibrationController.mock()
    transport = ctrl.client.transport
    hook = ctrl.hook_state()
    assert hook["active"] is True and hook["needs_check"] is False
    entries = hook["routing"]["entries"]
    assert [e["method"] for e in entries] == ["unique", "unique"]
    assert [e["monitor"] for e in entries] == [0, 1]
    assert entries[1]["left"] == 3840                  # monitor 1's desktop origin
    assert hr.routing_needs_check(hook, 0) == (False, "monitor 0 routing unambiguous (unique)")
    # without twins there is nothing to swap — the C++ errors the same way
    with pytest.raises(DesktopLutApiError, match="twin"):
        ctrl.set_hook_routing("swap", monitor=0)

    transport.hook_twins = True
    hook = ctrl.hook_state()
    assert [e["method"] for e in hook["routing"]["entries"]] == ["order", "order"]
    assert hook["needs_check"] is True
    assert hr.routing_needs_check(hook, 0)[0] is True

    transport.hook_routing_crossed = True
    out = ctrl.set_hook_routing("swap", monitor=0)
    assert out["reinjected"] is True
    assert transport.hook_routing_crossed is False       # the swap fixed the pairing
    assert [e["method"] for e in out["hook"]["routing"]["entries"]] == ["pinned", "pinned"]
    assert out["hook"]["routing"]["confirmed"] is False and out["hook"]["needs_check"] is True

    out = ctrl.set_hook_routing("confirm")
    assert out["reinjected"] is False
    assert out["hook"]["routing"]["confirmed"] is True and out["hook"]["needs_check"] is False
    assert ctrl.hook_state()["needs_check"] is False     # state.get agrees

    out = ctrl.set_hook_routing("clear")
    assert out["reinjected"] is True and out["hook"]["routing"]["confirmed"] is False
    assert [e["method"] for e in out["hook"]["routing"]["entries"]] == ["order", "order"]

    out = ctrl.set_hook_routing("assign", entries=[{"ctx": "0x1", "left": 0, "top": 0}])
    assert out["reinjected"] is True
    assert [e["method"] for e in out["hook"]["routing"]["entries"]] == ["pinned", "pinned"]
    with pytest.raises(DesktopLutApiError, match="entries"):
        ctrl.set_hook_routing("assign")
    with pytest.raises(DesktopLutApiError, match="action"):
        ctrl.set_hook_routing("wiggle")
    # the wire carries only the params that were given
    routing_reqs = [r for r in transport.requests if r.method == "hook.set_routing"]
    swap_req = [r for r in routing_reqs if r.params["action"] == "swap"][0]
    assert swap_req.params == {"action": "swap", "monitor": 0}
    confirm_req = [r for r in routing_reqs if r.params["action"] == "confirm"][0]
    assert confirm_req.params == {"action": "confirm"}


def test_hook_state_is_none_on_a_build_without_hook_reporting():
    class _OldTransport:
        def request(self, command):
            from dlc.desktoplut_client import DesktopLutResponse
            return DesktopLutResponse(ok=True, result={"running": True})
    ctrl = CalibrationController.with_transport(_OldTransport())
    assert ctrl.hook_state() is None


# ---------------------------------------------------------------------------
# the spine: hardware-readiness carries the verdict; a crossed twin is an anomaly
# ---------------------------------------------------------------------------
def _spine(tmp_path, name, *, controller, measure, policy="auto", flow="3dlut-only"):
    from dlc import calibration_profile as cp
    from dlc.adjudication import AutoAdjudicator
    from dlc.calibrate import Calibration
    from dlc.runs import create_run
    ctx = create_run("HDR", display="synthetic", run_dir=tmp_path / name)
    profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    calib = Calibration(ctx=ctx, profile=profile, monitor=0, mode="HDR", controller=controller,
                        measure=measure, adjudicator=AutoAdjudicator(), bit_depth=10,
                        hook_routing_policy=policy)
    calib.calib["flow"] = flow
    return calib


def _events(calib, kind=None):
    from dlc.events import read_events
    evs = [e for e in read_events(calib.ctx.events_path) if e.event == "anomaly"]
    return [e for e in evs if kind is None or e.data.get("kind") == kind]


def test_readiness_stage_skips_the_optical_check_when_the_mock_routing_is_unambiguous(tmp_path):
    ctrl = CalibrationController.mock()
    reads: list[MeasurePatch] = []

    def measure(patch):
        reads.append(patch)
        return Reading(xyz=GREY_XYZ)
    calib = _spine(tmp_path, "ready_unique", controller=ctrl, measure=measure)
    out = calib.stage_hardware_readiness()
    routing = out.digest["hook_routing"]
    assert routing["checked"] is False and routing["required"] is False
    assert "unambiguous" in routing["reason"]
    assert reads == [] and _events(calib, "hook_routing") == []


def test_readiness_stage_installs_no_probe_for_a_flow_without_a_cube(tmp_path):
    ctrl = CalibrationController.mock()
    ctrl.client.transport.hook_twins = True                 # ambiguous, but mhc-only installs no cube
    calib = _spine(tmp_path, "ready_mhc_only", controller=ctrl, measure=lambda p: Reading(xyz=GREY_XYZ),
                   flow="mhc-only")
    out = calib.stage_hardware_readiness()
    assert out.digest["hook_routing"]["checked"] is False
    assert "installs no cube" in out.digest["hook_routing"]["reason"]
    assert not [r for r in ctrl.client.transport.requests if r.method == "runtime.set_3dlut"]


def test_readiness_stage_swaps_a_crossed_twin_and_raises_the_anomaly(tmp_path):
    ctrl = CalibrationController.mock()
    transport = ctrl.client.transport
    transport.hook_twins = True
    transport.hook_routing_crossed = True

    def panel(patch):
        cube = (ctrl.state()["runtime"].get(KEY) or {}).get("cube_path") or ""
        reaches = cube.endswith(hr.PROBE_CUBE_NAME) and not transport.hook_routing_crossed
        return Reading(xyz=MAGENTA_XYZ if reaches else GREY_XYZ)
    calib = _spine(tmp_path, "ready_crossed", controller=ctrl, measure=panel)
    out = calib.stage_hardware_readiness()
    routing = out.digest["hook_routing"]
    assert routing["checked"] and routing["swapped"] and routing["confirmed"]
    assert routing["verdict"] == "swapped" and routing["restored"] is True
    assert transport.hook_routing_crossed is False
    assert ctrl.hook_state()["needs_check"] is False
    assert "cube_path" not in (ctrl.state()["runtime"].get(KEY) or {})   # probe gone
    anomalies = _events(calib, "hook_routing")
    assert len(anomalies) == 1 and "CROSSED" in anomalies[0].data["message"]
    assert (tmp_path / "ready_crossed" / "generated" / hr.PROBE_CUBE_NAME).exists()


def test_readiness_stage_refuses_when_the_hook_paints_nothing(tmp_path):
    from dlc.calibrate import StageError
    ctrl = CalibrationController.mock()
    ctrl.client.transport.hook_twins = True
    calib = _spine(tmp_path, "ready_dead", controller=ctrl, measure=lambda p: Reading(xyz=GREY_XYZ))
    with pytest.raises(StageError) as exc:
        calib.stage_hardware_readiness()
    digest = exc.value.outcome.digest
    assert "does not render a cube" in digest["message"]
    assert digest["hook_routing"]["verdict"] == "no_effect" and digest["hook_routing"]["swapped"] is True
    assert _events(calib, "hook_routing")
    assert "cube_path" not in (ctrl.state()["runtime"].get(KEY) or {})


def test_live_readiness_probes_only_after_the_operator_says_ready(tmp_path):
    """The optical check is the run's first meter read, so it must wait for the operator's
    'meter aimed at the patch' confirmation: before the seam NO probe is installed and NO read
    is taken (an unaimed meter would read 'no effect' twice and refuse a healthy rig); the
    resume replays the recorded decision and then proves the routing exactly once."""
    from dlc import calibration_profile as cp
    from dlc.adjudication import AdjudicationRequired, Decision, SupervisedAdjudicator
    from dlc.calibrate import Calibration
    from dlc.runs import create_run, open_run

    reads: list[MeasurePatch] = []
    ctrl = CalibrationController.mock()
    transport = ctrl.client.transport
    transport.hook_twins = True

    def panel(patch):
        reads.append(patch)
        cube = (ctrl.state()["runtime"].get(KEY) or {}).get("cube_path") or ""
        return Reading(xyz=MAGENTA_XYZ if cube.endswith(hr.PROBE_CUBE_NAME) else GREY_XYZ)

    def make(adjudicator):
        run_dir = tmp_path / "live_ready"
        ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
            else create_run("HDR", display="synthetic", run_dir=run_dir)
        profile = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
        calib = Calibration(ctx=ctx, profile=profile, monitor=0, mode="HDR", controller=ctrl,
                            measure=panel, adjudicator=adjudicator, bit_depth=10,
                            require_hardware_readiness=True)
        calib.calib["flow"] = "3dlut-only"
        return calib

    with pytest.raises(AdjudicationRequired) as exc:
        make(SupervisedAdjudicator()).stage_hardware_readiness()
    req = exc.value.request
    assert req.key == "hardware-readiness:confirm"
    pending = req.digest["hook_routing_pending"]
    assert pending["will_check"] is True and "order-matched" in pending["reason"]
    assert reads == []                                                  # no read before 'ready'
    assert not [r for r in transport.requests if r.method == "runtime.set_3dlut"]

    resumed = make(SupervisedAdjudicator({
        "hardware-readiness:confirm": Decision("ready", note="meter aimed")}))
    out = resumed.stage_hardware_readiness()
    assert out.status == "done"
    routing = out.digest["hook_routing"]
    assert routing["checked"] and routing["verdict"] == "confirmed" and not routing["swapped"]
    assert [p.label for p in reads] == ["hook-routing-first-cube-on", "hook-routing-first-cube-off"]
    assert ctrl.hook_state()["needs_check"] is False


def test_evidence_after_install_flags_an_ambiguous_report_only(tmp_path):
    ctrl = CalibrationController.mock()
    calib = _spine(tmp_path, "after_install", controller=ctrl, measure=lambda p: Reading(xyz=GREY_XYZ))
    calib._hook_routing_evidence_after_install("build-install-3dlut")
    assert _events(calib, "hook_routing") == []
    ctrl.client.transport.hook_twins = True                 # a DWM restart re-rolled the order-match
    calib._hook_routing_evidence_after_install("build-install-3dlut")
    evs = _events(calib, "hook_routing")
    assert len(evs) == 1 and evs[0].stage == "build-install-3dlut"
    assert "another panel" in evs[0].data["message"]


def test_cli_flag_plumbs_the_policy(tmp_path):
    """The live CLI builds its parser inside main() (no-cover live wiring), so pin the plumbing
    statically the way the contract test pins the controller: the flag is declared with the
    documented choices/default and its dest reaches the orchestrator constructor."""
    import re
    source = (Path(__file__).resolve().parents[1] / "src" / "dlc" / "calibrate.py").read_text(encoding="utf-8")
    flag = re.search(r'parser\.add_argument\("--hook-routing-check",\s*choices=\("auto", "always", "never"\),'
                     r'\s*default="auto",\s*dest="hook_routing_policy"', source)
    assert flag, "--hook-routing-check flag missing or its choices/default/dest changed"
    assert "hook_routing_policy=args.hook_routing_policy" in source
    # and the constructor normalises the policy it is handed
    ctrl = CalibrationController.mock()
    calib = _spine(tmp_path, "policy", controller=ctrl, measure=lambda p: Reading(xyz=GREY_XYZ),
                   policy="NEVER")
    assert calib.hook_routing_policy == "never"
    ctrl.client.transport.hook_twins = True
    out = calib.stage_hardware_readiness()
    assert out.digest["hook_routing"]["checked"] is False and "never" in out.digest["hook_routing"]["reason"]
