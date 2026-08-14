"""Fable Phase 9 — the DesktopLUT IPC wire contract, pinned three ways.

1. **mock ⇄ spec**: every advertised method is served by the in-process simulator,
   and each response carries at least the spec's declared result keys (the mock may
   add simulated extras — DLC must never rely on a key the spec doesn't declare).
2. **spec ⇄ C++** (static, skips when the C++ tree isn't checked out alongside):
   every advertised method has a Dispatch handler (Phase 7a's existence pin), every
   C++-handled method is advertised (the reverse — a server method DLC's spec doesn't
   know about is contract drift), and each handler's ``result.set("...")`` keys cover
   the spec's declared result keys (shape conformance, the deepest check possible
   without a Windows build).
3. **behavioural fidelity pins** for semantics a ``--simulate`` run's correctness
   depends on: verify_mhc requires an APPLIED profile, cube paths are validated
   server-side, monitor/mode vocabulary is enforced, the re-enter snapshot hazard,
   and the contract-version handshake.
"""

from __future__ import annotations

import re
from argparse import Namespace
from pathlib import Path

import pytest

from dlc.controller import CalibrationController
from dlc.desktoplut_api_spec import build_desktoplut_api_spec
from dlc.desktoplut_client import (
    CONTRACT_VERSION,
    DesktopLutApiError,
    DesktopLutClient,
    DesktopLutCommand,
    contract_version_mismatch,
)
from dlc.desktoplut_mock import MockDesktopLutTransport

CPP_SERVER = Path(__file__).resolve().parents[1].parent / "src" / "desktoplut_ipc_server.cpp"

# Spec result keys the C++ does not emit yet — each entry is a DESKTOPLUT TICKET
# (docs/audits/fable/phase-9.md §5). Remove the entry when the C++ lands it, so this
# test starts enforcing it.
CPP_TICKETED_RESULT_KEYS = {
    "state.get": {"contract_version"},
}


def _spec_methods() -> dict[str, dict]:
    return {m["method"]: m for m in build_desktoplut_api_spec()["methods"]}


def _write_1d_cube(path: Path) -> Path:
    path.write_text("LUT_1D_SIZE 2\n0 0 0\n1 1 1\n", encoding="utf-8")
    return path


def _write_3d_cube(path: Path) -> Path:
    path.write_text('TITLE "sim"\nLUT_3D_SIZE 2\n' + "0 0 0\n" * 8, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 1. mock ⇄ spec: every advertised method served, response shape ⊇ spec shape
# --------------------------------------------------------------------------
def test_mock_serves_every_spec_method_with_spec_result_shape(tmp_path):
    """Drives all advertised methods against the simulator in one realistic order and
    asserts each ok-response carries at least the spec's declared result keys."""
    client = DesktopLutClient(transport=MockDesktopLutTransport())
    cube_1d = _write_1d_cube(tmp_path / "base.cube")
    cube_3d = _write_3d_cube(tmp_path / "final.cube")
    gs = {"point_count": 2, "points": [0.0, 1.0],
          "deviations": {"r": [1.0, 1.0], "g": [1.0, 1.0], "b": [1.0, 1.0]}}
    mm = {"monitor": 0, "mode": "SDR"}

    calls: list[tuple[str, dict]] = [
        ("state.get", {}),
        ("windows.query_monitors", {}),
        ("windows.query_profiles", {"monitor": 0}),
        ("windows.query_gamma_ramp", {"monitor": 0}),
        ("windows.set_hdr", {"monitor": 0, "enable": True}),
        ("windows.set_hdr", {"monitor": 0, "enable": False}),
        ("corrections.disable_all", {}),
        ("calibration.enter", {**mm, "dummy_icc_path": "C:/dlc/sRGB.icm", "reason": "contract test"}),
        ("calibration.status", {}),
        ("mhc.set_primaries", {**mm, "primaries": {"rx": 0.64, "ry": 0.33, "gx": 0.30,
                                                   "gy": 0.60, "bx": 0.15, "by": 0.06}}),
        ("mhc.set_white", {**mm, "x": 0.3127, "y": 0.3290}),
        ("mhc.set_base_grayscale", {**mm, **gs}),
        ("mhc.set_base_lut", {**mm, "cube_path": str(cube_1d), "peak_nits": 600.0}),
        ("mhc.set_correction_grayscale", {**mm, **gs}),
        ("mhc.apply", mm),
        ("maintenance.verify_mhc", mm),
        ("mhc.grayscale_live_begin", mm),
        ("mhc.grayscale_set_live", {**mm, "grayscale": gs}),
        ("mhc.grayscale_commit", mm),
        ("mhc.grayscale_cancel", mm),           # post-commit cancel: tolerated no-op, canceled:false
        ("runtime.set_3dlut", {**mm, "cube_path": str(cube_3d)}),
        ("runtime.clear_3dlut", mm),
        ("runtime.set_grayscale_tweak", {**mm, "grayscale_tweak": gs}),
        ("runtime.disable_grayscale_tweak", mm),
        ("mhc.remove", mm),
        ("calibration.exit", {"restore_snapshot": False}),
    ]

    spec = _spec_methods()
    exercised = set()
    for method, params in calls:
        response = client.call(method, params)
        assert response.ok, (method, response.error)
        assert method in spec, f"mock/sequence drives {method!r} but the spec does not advertise it"
        missing = set(spec[method]["result"]) - set((response.result or {}).keys())
        assert not missing, f"{method}: mock response missing spec result keys {sorted(missing)}"
        exercised.add(method)

    unexercised = set(spec) - exercised
    assert not unexercised, f"spec methods never exercised against the mock: {sorted(unexercised)}"


def test_controller_only_speaks_advertised_methods():
    """Static: every wire method CalibrationController drives must be in the spec
    (guards controller drift — a new controller call needs a contract entry first)."""
    source = (Path(__file__).resolve().parents[1] / "src" / "dlc" / "controller.py").read_text(encoding="utf-8")
    driven = set(re.findall(r'self\.call\(\s*\n?\s*"([\w.]+)"', source))
    advertised = set(_spec_methods())
    unadvertised = driven - advertised
    assert not unadvertised, f"controller drives methods the spec does not advertise: {sorted(unadvertised)}"


# --------------------------------------------------------------------------
# 2. spec ⇄ C++ (static conformance; skips when the C++ tree is absent)
# --------------------------------------------------------------------------
def _cpp_text() -> str:
    if not CPP_SERVER.exists():
        pytest.skip(f"C++ IPC server not found at {CPP_SERVER}")
    return CPP_SERVER.read_text(encoding="utf-8", errors="replace")


def test_every_cpp_handled_method_is_advertised():
    """Reverse of Phase 7a's existence pin: a method the C++ serves but the spec
    doesn't advertise is silent contract drift (this is how windows.set_hdr and the
    grayscale live-edit quartet went missing from the spec — fable Phase 9)."""
    text = _cpp_text()
    handled = set(re.findall(r'(?:\bm|\bmethod)\s*==\s*"([^"]+)"', text))
    advertised = set(_spec_methods())
    unadvertised = sorted(handled - advertised)
    assert not unadvertised, f"C++ serves methods the spec does not advertise: {unadvertised}"


def _cpp_handler_bodies() -> dict[str, str]:
    """Map wire method -> its C++ handler function body (best-effort static parse)."""
    text = _cpp_text()
    # Dispatch tables: `if (method == "x") { HandleX(...)` and `if (m == "x") DoX(...)`.
    method_to_fn: dict[str, str] = {}
    for method, fn in re.findall(r'(?:\bm|\bmethod)\s*==\s*"([^"]+)"\s*\)\s*\{?\s*(\w+)\(', text):
        method_to_fn.setdefault(method, fn)
    bodies: dict[str, str] = {}
    for method, fn in method_to_fn.items():
        m = re.search(rf'\n(?:void|LRESULT|bool)\s+{fn}\([^)]*\)\s*\{{(.*?)\n\}}', text, re.DOTALL)
        if m:
            bodies[method] = m.group(1)
    return bodies


def test_cpp_handler_result_keys_cover_spec_shapes():
    """Shape conformance: each handler's `result.set("key", ...)` calls must cover the
    spec's declared result keys (minus explicitly ticketed gaps). Static — the deepest
    contract check available without running the Windows build."""
    bodies = _cpp_handler_bodies()
    spec = _spec_methods()
    problems: list[str] = []
    for method, entry in spec.items():
        body = bodies.get(method)
        if body is None:
            problems.append(f"{method}: no handler body found for static shape check")
            continue
        set_keys = set(re.findall(r'result\.set\("([^"]+)"', body))
        ticketed = CPP_TICKETED_RESULT_KEYS.get(method, set())
        missing = set(entry["result"]) - set_keys - ticketed
        if missing:
            problems.append(f"{method}: C++ handler never sets spec result keys {sorted(missing)}")
    assert not problems, "\n".join(problems)


def test_spec_gui_thread_flags_match_cpp_dispatch():
    """gui_thread_required must mirror the C++ Dispatch routing: methods served on the
    pipe thread (before the IsMutatingMethod marshal) are NOT gui-thread methods.
    Pins the fable Phase 9 fix (maintenance.verify_mhc wrongly claimed the GUI thread)."""
    text = _cpp_text()
    dispatch = text[text.find("std::string Dispatch("):text.find("// Pipe server")]
    # Methods dispatched by name BEFORE the IsMutatingMethod(...) marshal run off-thread.
    off_thread = set(re.findall(r'method\s*==\s*"([^"]+)"', dispatch[:dispatch.find("IsMutatingMethod")]))
    for method, entry in _spec_methods().items():
        expected = method not in off_thread
        assert entry["gui_thread_required"] is expected, (
            f"{method}: spec says gui_thread_required={entry['gui_thread_required']} "
            f"but the C++ Dispatch routes it {'off' if not expected else 'onto'} the GUI thread")


# --------------------------------------------------------------------------
# 3. Behavioural fidelity pins (semantics sim correctness depends on)
# --------------------------------------------------------------------------
def test_verify_mhc_requires_an_applied_profile():
    """C++ DoVerifyMhc: verified = enabled && profile baked. Staged-but-unapplied params
    must NOT verify — previously the mock passed any non-empty staged dict, letting a sim
    run pass a verify gate hardware would fail."""
    ctrl = CalibrationController.mock()
    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")
    ctrl.set_primaries(0, "SDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    assert ctrl.verify_mhc(0, "SDR")["verified"] is False  # staged only — no apply yet
    applied = ctrl.apply_mhc(0, "SDR")
    assert applied["mhc"]["applied"] is True
    assert applied["mhc"]["profile_name"]  # C++ reports the baked profile inside the mhc object
    assert ctrl.verify_mhc(0, "SDR")["verified"] is True
    ctrl.remove_mhc(0, "SDR")
    assert ctrl.verify_mhc(0, "SDR")["verified"] is False


def test_cube_path_validation_matches_cpp(tmp_path):
    """C++ validates cube paths up-front (existence for set_3dlut; existence + 1D parse
    for set_base_lut) so a phantom path fails with a clear error — the mock must too,
    or the Phase-5 cwd-as-cube bug class survives --simulate and dies on hardware."""
    ctrl = CalibrationController.mock()
    with pytest.raises(DesktopLutApiError, match="cube_path does not exist"):
        ctrl.set_3dlut(0, "SDR", str(tmp_path / "phantom.cube"))
    with pytest.raises(DesktopLutApiError, match="cube_path does not exist"):
        ctrl.set_base_lut(0, "SDR", str(tmp_path / "phantom.cube"))
    not_1d = _write_3d_cube(tmp_path / "3d.cube")
    with pytest.raises(DesktopLutApiError, match="not a valid 1D"):
        ctrl.set_base_lut(0, "SDR", str(not_1d))
    ok_1d = _write_1d_cube(tmp_path / "1d.cube")
    assert ctrl.set_base_lut(0, "SDR", str(ok_1d))["mhc"]["base_lut"]["cube_path"] == str(ok_1d)


def test_monitor_and_mode_vocabulary_matches_cpp():
    """C++ ParseMonitorMode rejects unknown monitors and non-SDR/HDR modes; the mock
    used to accept anything, so a bad display mapping only failed on hardware."""
    client = DesktopLutClient(transport=MockDesktopLutTransport())
    resp = client.send(DesktopLutCommand("mhc.apply", {"monitor": 5, "mode": "SDR"}),
                       raise_on_error=False)
    assert resp.ok is False and "monitor index out of range" in (resp.error or "")
    resp = client.send(DesktopLutCommand("mhc.apply", {"monitor": 0, "mode": "ACM"}),
                       raise_on_error=False)
    assert resp.ok is False and "mode must be SDR or HDR" in (resp.error or "")
    # calibration.enter validates the same vocabulary (C++ routes it through ParseMonitorMode).
    resp = client.send(DesktopLutCommand(
        "calibration.enter", {"monitor": 9, "mode": "SDR", "dummy_icc_path": "x.icm"}),
        raise_on_error=False)
    assert resp.ok is False and "monitor index out of range" in (resp.error or "")


def test_reenter_overwrites_restore_snapshot_hazard(tmp_path):
    """Documents (and pins the mock mirror of) a real C++ hazard: DoEnterNeutral
    re-snapshots unconditionally, so entering calibration mode while a stale session is
    active captures the already-CLEARED state — exit(restore_snapshot=True) then cannot
    bring back the user's pre-run setup. DesktopLUT ticket: keep the ORIGINAL snapshot on
    re-enter. DLC surfaces the stale session at enter-neutral and treats the preflight
    settings backup as the authoritative restore (fable Phase 9)."""
    ctrl = CalibrationController.mock()
    user_cube = _write_3d_cube(tmp_path / "user.cube")
    ctrl.set_3dlut(0, "SDR", str(user_cube))          # the user's pre-run setup

    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")   # run 1 enters... and crashes (no exit)
    ctrl.enter_neutral(0, "SDR", "C:/dlc/sRGB.icm")   # run 2 enters over the stale session
    out = ctrl.exit_calibration(restore_snapshot=True)
    assert out["restored"] is True
    # The pre-run cube is GONE: the second enter's snapshot captured the cleared state.
    assert "cube_path" not in (ctrl.state().get("runtime", {}).get("0:SDR") or {})


def test_enter_neutral_clears_only_the_calibrated_pair(tmp_path):
    """C++ DoEnterNeutral clears ONLY the calibrated mode:monitor pair's runtime layers
    (2026-08-14 field regression: enter cleared BOTH modes on the monitor and the
    apply-path exit restores nothing, so a clean HDR run permanently dropped the user's
    SDR runtime cube). Other pairs — the same monitor's other mode AND other monitors —
    must survive enter + apply-path exit untouched."""
    ctrl = CalibrationController.mock()
    sdr_cube = _write_3d_cube(tmp_path / "user_sdr.cube")
    hdr_cube = _write_3d_cube(tmp_path / "user_hdr.cube")
    mon1_cube = _write_3d_cube(tmp_path / "mon1_sdr.cube")
    ctrl.set_3dlut(0, "SDR", str(sdr_cube))
    ctrl.set_3dlut(0, "HDR", str(hdr_cube))
    ctrl.set_3dlut(1, "SDR", str(mon1_cube))

    ctrl.enter_neutral(0, "HDR", "C:/dlc/sRGB.icm")
    runtime = ctrl.state()["runtime"]
    assert "cube_path" not in (runtime.get("0:HDR") or {})                 # calibrated pair cleared
    assert (runtime.get("0:SDR") or {}).get("cube_path") == str(sdr_cube)  # other mode preserved
    assert (runtime.get("1:SDR") or {}).get("cube_path") == str(mon1_cube)  # other monitor preserved

    # A fresh build lands, the operator accepts: exit WITHOUT the snapshot restore.
    new_hdr = _write_3d_cube(tmp_path / "new_hdr.cube")
    ctrl.set_3dlut(0, "HDR", str(new_hdr))
    out = ctrl.exit_calibration(restore_snapshot=False)
    assert out["restored"] is False
    runtime = ctrl.state()["runtime"]
    assert (runtime.get("0:HDR") or {}).get("cube_path") == str(new_hdr)
    assert (runtime.get("0:SDR") or {}).get("cube_path") == str(sdr_cube)
    assert (runtime.get("1:SDR") or {}).get("cube_path") == str(mon1_cube)


def test_state_get_carries_contract_version_and_mismatch_helper():
    ctrl = CalibrationController.mock()
    state = ctrl.state()
    assert state["contract_version"] == CONTRACT_VERSION
    assert contract_version_mismatch(state) is None
    # Absent field = pre-versioning C++ build = compatible v1.
    assert contract_version_mismatch({"running": True}) is None
    assert contract_version_mismatch(None) is None
    msg = contract_version_mismatch({"contract_version": CONTRACT_VERSION + 1})
    assert msg and f"v{CONTRACT_VERSION + 1}" in msg and "update DLC" in msg
    msg = contract_version_mismatch({"contract_version": "banana"})
    assert msg and "unparseable" in msg


def test_gamma_ramp_evidence_is_shaped_like_hardware():
    """The simulated panel reports a real (identity) ramp readback so enter-neutral's
    ramp-evidence branch is exercised under sim (was available:false — untestable)."""
    ctrl = CalibrationController.mock()
    ramp = ctrl.query_gamma_ramp(0)
    assert ramp["available"] is True and ramp["simulated"] is True
    assert ramp["gamma_ramp_loaded"] is False and ramp["vcgt_present"] is False
    # Out-of-range monitor mirrors the C++ unavailable path.
    ramp = ctrl.query_gamma_ramp(9)
    assert ramp["available"] is False and ramp["gamma_ramp_loaded"] is None


def test_enter_neutral_surfaces_stale_calibration_mode(tmp_path):
    """A previous run that never exited leaves calibration mode active; entering again
    silently destroys the C++ restore snapshot (see the re-enter hazard test above), so
    the stage must SAY so — the digest reader then knows the preflight settings backup is
    the authoritative restore."""
    from dlc.runs import create_run
    from dlc.stages import enter_neutral

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    args = Namespace(run=ctx.root, monitor=0, mode="SDR", simulate=True, pipe="unused")

    first = enter_neutral.build(args, ctx)
    assert first.metrics["stale_calibration_mode"] is False
    assert "stale_calibration_mode" not in [a.code for a in first.anomalies]

    second = enter_neutral.build(args, ctx)   # the crashed-run-then-rerun corner
    assert second.metrics["stale_calibration_mode"] is True
    assert "stale_calibration_mode" in [a.code for a in second.anomalies]
    # The evidence branch works through real (simulated-identity) ramp data now.
    assert second.metrics["gamma_ramp_loaded"] is False
    assert second.metrics["neutral_confirmed"] is True


# --------------------------------------------------------------------------
# install-mhc: apply confirmation is judged from real evidence, never defaulted
# --------------------------------------------------------------------------
def _install_args(ctx) -> Namespace:
    return Namespace(run=ctx.root, monitor=0, mode="SDR", simulate=True, pipe="unused")


def _seed_mhc_params(ctx) -> None:
    from dlc.stages import _common
    state = _common.load_dlc_state(ctx)
    state["mhc_params"] = {
        "monitor": 0,
        "primaries": {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06},
        "white": {"x": 0.3127, "y": 0.3290},
        "base_grayscale": {"point_count": 2, "points": [0.0, 1.0],
                           "deviations": {"r": [1.0, 1.0], "g": [1.0, 1.0], "b": [1.0, 1.0]}},
        "target_gamma": 2.2,
    }
    _common.save_dlc_state(ctx, state)


def test_install_mhc_confirms_apply_from_evidence(tmp_path):
    """Happy path: the simulator confirms with mhc.applied + profile_name and the stage
    reports both. fable Phase 9: the old install_ok ended with `applied.get("ok") is not
    False`, which is True for ANY dict — install_ok could literally never be False."""
    from dlc.runs import create_run
    from dlc.stages import install_mhc

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    _seed_mhc_params(ctx)
    result = install_mhc.build(_install_args(ctx), ctx)
    assert result.metrics["applied"] is True
    assert result.metrics["verified"] is True
    assert result.metrics["profile_name"]          # simulated name now flows through


def test_install_mhc_flags_unconfirmed_apply(tmp_path, monkeypatch):
    """An ok apply response that does NOT confirm application (no applied, no
    profile_name) must read as NOT installed and raise an anomaly."""
    from dlc.runs import create_run
    from dlc.stages import _common, install_mhc

    class _EvasiveController:
        def calibration_status(self):
            return {"active": True}

        def set_primaries(self, *a, **k):
            return {}

        def set_white(self, *a, **k):
            return {}

        def set_base_grayscale(self, *a, **k):
            return {}

        def set_base_lut(self, *a, **k):
            return {}

        def set_correction_grayscale(self, *a, **k):
            return {}

        def apply_mhc(self, *a, **k):
            return {"monitor_mode": "0:SDR", "mhc": {}}   # ok, but nothing confirmed

        def verify_mhc(self, *a, **k):
            return {"verified": False}

    ctx = create_run("SDR", display="test", run_dir=tmp_path / "run")
    _seed_mhc_params(ctx)
    monkeypatch.setattr(_common, "make_controller", lambda args, ctx: _EvasiveController())
    result = install_mhc.build(_install_args(ctx), ctx)
    assert result.metrics["applied"] is False
    assert result.metrics["verified"] is False
    codes = [a.code for a in result.anomalies]
    assert "apply_unconfirmed" in codes and "verify_failed" in codes
    assert result.advice["default_policy_verdict"] == "investigate"
