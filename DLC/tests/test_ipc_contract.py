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
from dlc.desktoplut_mock import MockDesktopLutServer, MockDesktopLutTransport

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


def _write_fald_panel(path: Path, transfer: str, boost_steps: int = 0) -> Path:
    """A header-only stand-in for a FALD panel file: the mock (like the C++ set_fald_params peek) reads only
    the magic + FLD3 word 40 (+ FLD4 word 48). 'pq' -> FLD1, 'gamma' -> FLD3 with transfer 1 / sdr_gamma 2.27;
    boost_steps > 0 -> FLD4 (the 48 FLD3 words + the boost block's step count) with that transfer."""
    import struct
    if boost_steps:
        code, gamma = (1, 2.27) if transfer == "gamma" else (0, 0.0)
        path.write_bytes(struct.pack("<I", 0x464C4434) + bytes(39 * 4) + struct.pack("<If", code, gamma) + bytes(6 * 4)
                         + struct.pack("<I", boost_steps) + bytes(55 * 4))
    elif transfer == "pq":
        path.write_bytes(struct.pack("<I", 0x464C4431) + bytes(31 * 4))
    else:
        path.write_bytes(struct.pack("<I", 0x464C4433) + bytes(39 * 4) + struct.pack("<If", 1, 2.27) + bytes(6 * 4))
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
        ("layers.set", {**mm, "white_balance": False, "grayscale": False}),
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
        ("hook.set_routing", {"action": "confirm"}),   # the DWM-hook twin-routing verb (2026-09-03)
        ("runtime.clear_3dlut", mm),
        ("runtime.set_grayscale_tweak", {**mm, "grayscale_tweak": gs}),
        ("runtime.disable_grayscale_tweak", mm),
        ("runtime.set_fald_params", {"monitor": 0, "mode": "HDR", "params_path": str(cube_3d)}),   # any existing file (unclassifiable: accepted)
        ("runtime.fald_debug", {"monitor": 0, "mode": "HDR", "debug_mode": 1}),
        ("runtime.fald_dump", {"monitor": 0, "mode": "SDR", "dir": str(tmp_path)}),   # the live mode (set_hdr off above)
        ("runtime.set_fald_params", {"monitor": 0, "mode": "SDR", "params_path": str(_write_fald_panel(tmp_path / "sdr.bin", "gamma"))}),
        ("runtime.fald_debug", {"monitor": 0, "mode": "SDR", "debug_mode": 4}),
        ("runtime.fald_temporal", {"monitor": 0, "mode": "SDR", "temporal_mode": 1, "tau_rise_ms": 40, "tau_fall_ms": 120}),
        ("runtime.fald_starfield", {"monitor": 0, "mode": "SDR", "enabled": True, "even": 0.8, "even_reach": 6}),
        ("layers.set", {**mm, "fald": True}),
        ("layers.set", {**mm, "fald": False}),
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
# A wire-method dispatch comparison in the C++: a BARE `m == "x"` / `method == "x"` (the
# Dispatch tables). The negative look-behind excludes member accesses such as the hook-routing
# code's `e.method == "order"` (a routing-entry FIELD, not a wire method) — without it the
# reverse pin reported `order`/`pinned` as unadvertised server methods (2026-09-04).
_CPP_DISPATCH_RE = r'(?<![.\w])(?:m|method)\s*==\s*"([^"]+)"'


def _cpp_text() -> str:
    if not CPP_SERVER.exists():
        pytest.skip(f"C++ IPC server not found at {CPP_SERVER}")
    return CPP_SERVER.read_text(encoding="utf-8", errors="replace")


def test_every_cpp_handled_method_is_advertised():
    """Reverse of Phase 7a's existence pin: a method the C++ serves but the spec
    doesn't advertise is silent contract drift (this is how windows.set_hdr and the
    grayscale live-edit quartet went missing from the spec — fable Phase 9)."""
    text = _cpp_text()
    handled = set(re.findall(_CPP_DISPATCH_RE, text))
    advertised = set(_spec_methods())
    unadvertised = sorted(handled - advertised)
    assert not unadvertised, f"C++ serves methods the spec does not advertise: {unadvertised}"


def _cpp_handler_bodies() -> dict[str, str]:
    """Map wire method -> its C++ handler function body (best-effort static parse)."""
    text = _cpp_text()
    # Dispatch tables: `if (method == "x") { HandleX(...)` and `if (m == "x") DoX(...)`.
    method_to_fn: dict[str, str] = {}
    for method, fn in re.findall(_CPP_DISPATCH_RE + r'\s*\)\s*\{?\s*(\w+)\(', text):
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


def test_fald_layer_is_per_mode_with_transfer_check(tmp_path):
    """2026-09-14 (work guide P7/C8): the FALD layer runs in HDR and in SDR under ACM. The mock mirrors the
    C++: set_fald_params accepts both modes but refuses a panel file whose transfer does not match (an HDR
    PQ fit on the SDR row or the reverse), layers.set toggles fald per mode, state.get reports it per pair,
    and query_monitors tells ACM_SDR from SDR (which the DXGI colour space could not)."""
    server = MockDesktopLutServer()
    client = DesktopLutClient(transport=MockDesktopLutTransport(server))
    pq = _write_fald_panel(tmp_path / "hdr_pq.bin", "pq")
    gamma = _write_fald_panel(tmp_path / "sdr_gamma.bin", "gamma")
    ok = client.call("runtime.set_fald_params", {"monitor": 0, "mode": "SDR", "params_path": str(gamma)})
    assert ok.ok and ok.result["transfer"] == "gamma" and ok.result["monitor_mode"] == "0:SDR"
    ok = client.call("runtime.set_fald_params", {"monitor": 0, "mode": "HDR", "params_path": str(pq)})
    assert ok.ok and ok.result["transfer"] == "pq"
    bad = client.send(DesktopLutCommand("runtime.set_fald_params", {"monitor": 0, "mode": "SDR", "params_path": str(pq)}),
                      raise_on_error=False)
    assert bad.ok is False and bad.error == "panel file transfer pq (HDR fit) does not match mode SDR"   # C++ text
    bad = client.send(DesktopLutCommand("runtime.set_fald_params", {"monitor": 0, "mode": "HDR", "params_path": str(gamma)}),
                      raise_on_error=False)
    assert bad.ok is False and bad.error == "panel file transfer gamma (SDR fit) does not match mode HDR"
    # per-mode toggle + state
    r = client.call("layers.set", {"monitor": 0, "mode": "SDR", "fald": True})
    assert r.ok and r.result["after"]["fald"] is True and r.result["regenerated"] is False
    assert r.result["after"]["fald_params_path"] == str(gamma)          # C++ LayersJson carries the path
    st = client.call("state.get", {}).result
    assert st["layers"]["0:SDR"]["fald"] is True and st["layers"]["0:HDR"]["fald"] is False
    # the C++ reports the fald settings inside layers[key] for every pair — never under runtime
    assert st["layers"]["0:SDR"]["fald_params_path"] == str(gamma) and st["layers"]["0:SDR"]["fald_file_transfer"] == "gamma"
    assert st["layers"]["0:HDR"]["fald_params_path"] == str(pq) and st["layers"]["0:HDR"]["fald_file_transfer"] == "pq"
    assert "fald_file_transfer" not in st["layers"]["1:SDR"] and st["layers"]["1:SDR"]["fald_params_path"] == ""
    assert "0:SDR" not in st["runtime"]
    # black-frame LED boost (C12): an FLD4 file is classified by its transfer word like FLD3, and state.get says
    # whether the configured file carries a boost LUT (every pair; false without a file / for FLD1-3 / step count 0)
    assert st["layers"]["0:HDR"]["fald_boost_in_file"] is False and st["layers"]["0:SDR"]["fald_boost_in_file"] is False
    assert st["layers"]["1:SDR"]["fald_boost_in_file"] is False
    pq4 = _write_fald_panel(tmp_path / "hdr_pq_boost.bin", "pq", boost_steps=15)
    ok = client.call("runtime.set_fald_params", {"monitor": 0, "mode": "HDR", "params_path": str(pq4)})
    assert ok.ok and ok.result["transfer"] == "pq"
    bad = client.send(DesktopLutCommand("runtime.set_fald_params", {"monitor": 0, "mode": "SDR", "params_path": str(pq4)}),
                      raise_on_error=False)
    assert bad.ok is False and bad.error == "panel file transfer pq (HDR fit) does not match mode SDR"
    st4 = client.call("state.get", {}).result
    assert st4["layers"]["0:HDR"]["fald_boost_in_file"] is True and st4["layers"]["0:HDR"]["fald_file_transfer"] == "pq"
    assert st4["layers"]["0:SDR"]["fald_boost_in_file"] is False
    empty4 = _write_fald_panel(tmp_path / "hdr_pq_boost0.bin", "pq", boost_steps=25)      # out of range: not a boost file
    client.call("runtime.set_fald_params", {"monitor": 0, "mode": "HDR", "params_path": str(empty4)})
    assert client.call("state.get", {}).result["layers"]["0:HDR"]["fald_boost_in_file"] is False
    client.call("runtime.set_fald_params", {"monitor": 0, "mode": "HDR", "params_path": str(pq)})
    # temporal drive state (2026-09-17): persisted per mode, reported in layers[key], refusals word for word
    tmp = client.call("runtime.fald_temporal", {"monitor": 0, "mode": "SDR", "temporal_mode": 1, "tau_rise_ms": 40, "tau_fall_ms": 120})
    assert tmp.ok and tmp.result["temporal_mode"] == 1 and tmp.result["tau_rise_ms"] == 40.0 and tmp.result["tau_fall_ms"] == 120.0
    assert tmp.result["settle_frames_60hz"] == 36                                      # ceil(5 * 120 / 16.667)
    only_tau = client.call("runtime.fald_temporal", {"monitor": 0, "mode": "SDR", "tau_fall_ms": 0})
    assert only_tau.ok and only_tau.result["temporal_mode"] == 1 and only_tau.result["tau_fall_ms"] == 0.0
    st = client.call("state.get", {}).result
    assert st["layers"]["0:SDR"]["fald_temporal_mode"] == 1 and st["layers"]["0:SDR"]["fald_tau_rise_ms"] == 40.0
    assert st["layers"]["0:HDR"]["fald_temporal_mode"] == 0 and st["layers"]["0:HDR"]["fald_tau_rise_ms"] == 0.0   # per mode
    bad = client.send(DesktopLutCommand("runtime.fald_temporal", {"monitor": 0, "mode": "SDR", "temporal_mode": 4}), raise_on_error=False)
    assert not bad.ok and bad.error == "temporal_mode must be 0 (off), 1 (both fields), 2 (B_true only) or 3 (panel clock)"
    bad = client.send(DesktopLutCommand("runtime.fald_temporal", {"monitor": 0, "mode": "SDR", "tau_rise_ms": 9000}), raise_on_error=False)
    assert not bad.ok and bad.error == "tau_rise_ms must be 0..5000 ms"
    bad = client.send(DesktopLutCommand("runtime.fald_temporal", {"monitor": 0, "mode": "SDR"}), raise_on_error=False)
    assert not bad.ok and bad.error.startswith("missing parameter: temporal_mode")
    dl = client.call("runtime.fald_temporal", {"monitor": 0, "mode": "SDR", "delay_frames": 2})
    assert dl.ok and dl.result["delay_frames"] == 2 and dl.result["settle_frames_60hz"] == 12 + 2   # tau_fall is 0 now: 5*40/16.667 = 12, + delay
    assert client.call("state.get", {}).result["layers"]["0:SDR"]["fald_delay_frames"] == 2
    bad = client.send(DesktopLutCommand("runtime.fald_temporal", {"monitor": 0, "mode": "SDR", "delay_frames": 4}), raise_on_error=False)
    assert not bad.ok and bad.error == "delay_frames must be 0..3"
    # temporal mode 3 "panel clock" (2026-09-20, work guide C13): closure + parity, defaults 0.72 / -1 (unknown), per mode
    st3 = client.call("state.get", {}).result["layers"]
    assert st3["0:HDR"]["fald_temporal_closure"] == 0.72 and st3["0:HDR"]["fald_temporal_parity"] == -1
    pc = client.call("runtime.fald_temporal", {"monitor": 0, "mode": "HDR", "temporal_mode": 3, "closure": 0.72, "parity": -1})
    assert pc.ok and pc.result["temporal_mode"] == 3 and pc.result["closure"] == 0.72 and pc.result["parity"] == -1
    assert pc.result["settle_frames_60hz"] == 12                                        # mode 3: 2 ceil(ln 0.005 / ln 0.28) + 2 refreshes
    only_parity = client.call("runtime.fald_temporal", {"monitor": 0, "mode": "HDR", "parity": 1})
    assert only_parity.ok and only_parity.result["temporal_mode"] == 3 and only_parity.result["parity"] == 1
    only_closure = client.call("runtime.fald_temporal", {"monitor": 0, "mode": "HDR", "closure": 0.5})
    assert only_closure.ok and only_closure.result["closure"] == 0.5 and only_closure.result["settle_frames_60hz"] == 18
    st3 = client.call("state.get", {}).result["layers"]
    assert st3["0:HDR"]["fald_temporal_mode"] == 3 and st3["0:HDR"]["fald_temporal_closure"] == 0.5 and st3["0:HDR"]["fald_temporal_parity"] == 1
    assert st3["0:SDR"]["fald_temporal_mode"] == 1 and st3["0:SDR"]["fald_temporal_closure"] == 0.72                # per mode
    bad = client.send(DesktopLutCommand("runtime.fald_temporal", {"monitor": 0, "mode": "HDR", "closure": 0.01}), raise_on_error=False)
    assert not bad.ok and bad.error == "closure must be 0.05..1"
    bad = client.send(DesktopLutCommand("runtime.fald_temporal", {"monitor": 0, "mode": "HDR", "parity": 2}), raise_on_error=False)
    assert not bad.ok and bad.error == "parity must be -1 (unknown), 0 or 1"
    assert client.call("state.get", {}).result["layers"]["0:HDR"]["fald_temporal_closure"] == 0.5                   # a refusal stores nothing
    client.call("runtime.fald_temporal", {"monitor": 0, "mode": "HDR", "temporal_mode": 0, "closure": 0.72, "parity": -1})
    # ... and the C++ handler carries the same refusals word for word
    if CPP_SERVER.exists():
        cpp = CPP_SERVER.read_text(encoding="utf-8", errors="replace")
        for text in ("temporal_mode must be 0 (off), 1 (both fields), 2 (B_true only) or 3 (panel clock)", "closure must be 0.05..1",
                     "parity must be -1 (unknown), 0 or 1",
                     "missing parameter: temporal_mode (0|1|2|3), tau_rise_ms, tau_fall_ms (0..5000), delay_frames (0..3), closure (0.05..1) or parity (-1|0|1)"):
            assert text in cpp, text
    # starfield balancing (2026-09-19, work guide S1): partial updates, persisted per mode, reported in layers[key]
    st0 = client.call("state.get", {}).result["layers"]["0:SDR"]
    assert st0["fald_starfield"] is False and st0["fald_star_even"] == 0.8 and st0["fald_star_lift"] == 0.0
    assert st0["fald_star_target_sigma"] == 0.0 and st0["fald_star_keep_nits"] == 100.0  # geometric mean + absolute floor (round 7)
    assert st0["fald_star_even_reach"] == 8 and st0["fald_star_reach"] == 2 and st0["fald_star_strength"] == 1.0
    assert (st0["fald_star_area_lo"], st0["fald_star_area_hi"], st0["fald_star_nb_lo"], st0["fald_star_nb_hi"]) == (40.0, 160.0, 0.15, 0.30)
    assert st0["fald_star_target_gain"] == 1.0 and st0["fald_star_cap_nits"] == 0.0 and st0["fald_star_peak_hi"] == 0.0
    sf = client.call("runtime.fald_starfield", {"monitor": 0, "mode": "SDR", "enabled": True, "even": 0.7, "even_reach": 6})
    assert sf.ok and sf.result["enabled"] is True and sf.result["even"] == 0.7 and sf.result["even_reach"] == 6
    assert sf.result["lift"] == 0.0 and sf.result["reach"] == 2 and sf.result["area_hi"] == 160.0      # untouched fields = defaults
    one = client.call("runtime.fald_starfield", {"monitor": 0, "mode": "SDR", "lift": 0.5})             # a partial update
    assert one.ok and one.result["enabled"] is True and one.result["even"] == 0.7 and one.result["lift"] == 0.5
    sig = client.call("runtime.fald_starfield", {"monitor": 0, "mode": "SDR", "target_sigma": 1.5, "keep_nits": 60})
    assert sig.ok and sig.result["target_sigma"] == 1.5 and sig.result["keep_nits"] == 60.0 and sig.result["even"] == 0.7
    assert client.call("state.get", {}).result["layers"]["0:SDR"]["fald_star_keep_nits"] == 60.0
    assert client.call("state.get", {}).result["layers"]["0:SDR"]["fald_star_target_sigma"] == 1.5
    assert client.call("state.get", {}).result["layers"]["0:HDR"]["fald_star_target_sigma"] == 0.0   # per mode
    st = client.call("state.get", {}).result
    assert st["layers"]["0:SDR"]["fald_starfield"] is True and st["layers"]["0:SDR"]["fald_star_even_reach"] == 6
    assert st["layers"]["0:SDR"]["fald_star_lift"] == 0.5
    assert st["layers"]["0:HDR"]["fald_starfield"] is False and st["layers"]["0:HDR"]["fald_star_even_reach"] == 8   # per mode
    for bad_params, text in (({"even": 1.5}, "even must be 0..1"), ({"lift": -0.1}, "lift must be 0..1"),
                             ({"target_gain": 0.01}, "target_gain must be 0.05..2"),
                             ({"target_sigma": 4.5}, "target_sigma must be 0..4"), ({"target_sigma": -1}, "target_sigma must be 0..4"),
                             ({"keep_nits": 20000}, "keep_nits must be 0..10000 (0 = no floor)"),
                             ({"even_reach": 13}, "even_reach must be an integer 0..12"),
                             ({"even_reach": 2.5}, "even_reach must be an integer 0..12"),
                             ({"cap_nits": 20000}, "cap_nits must be 0..10000 (0 = none)"),
                             ({"strength": 2}, "strength must be 0..1"), ({"reach": 5}, "reach must be an integer 0..4"),
                             ({"peak_hi": -1}, "peak_hi must be 0..10000 (0 = no limit)"),
                             ({"nb_lo": 1.5}, "nb_lo must be 0..1"), ({"nb_hi": -0.2}, "nb_hi must be 0..1"),
                             ({"area_lo": -1}, "area_lo must be 0..1000000 px^2"),
                             ({"area_hi": 10}, "area_hi must be >= area_lo"),              # the stored area_lo is 40
                             ({"nb_lo": 0.5}, "nb_hi must be >= nb_lo"),                   # the stored nb_hi is 0.30
                             ({"enabled": 1}, "enabled must be a boolean")):
        bad = client.send(DesktopLutCommand("runtime.fald_starfield", {"monitor": 0, "mode": "SDR", **bad_params}), raise_on_error=False)
        assert not bad.ok and bad.error == text, (bad_params, bad.error)
    bad = client.send(DesktopLutCommand("runtime.fald_starfield", {"monitor": 0, "mode": "SDR"}), raise_on_error=False)
    assert not bad.ok and bad.error.startswith("missing parameter: enabled, even, lift")
    # a refused call stores NOTHING (the valid `even` next to the bad `reach` is dropped too)
    bad = client.send(DesktopLutCommand("runtime.fald_starfield", {"monitor": 0, "mode": "SDR", "even": 0.1, "reach": 9}), raise_on_error=False)
    assert not bad.ok and client.call("state.get", {}).result["layers"]["0:SDR"]["fald_star_even"] == 0.7
    pair = client.call("runtime.fald_starfield", {"monitor": 0, "mode": "SDR", "area_lo": 200, "area_hi": 300, "enabled": False})
    assert pair.ok and pair.result["area_lo"] == 200.0 and pair.result["area_hi"] == 300.0 and pair.result["enabled"] is False
    dbg9 = client.call("runtime.fald_debug", {"monitor": 0, "mode": "SDR", "debug_mode": 12})
    assert dbg9.ok and dbg9.result["debug_mode"] == 9                                    # clamped to the last view
    dbg = client.call("runtime.fald_debug", {"monitor": 0, "mode": "SDR", "debug_mode": 4})
    assert dbg.ok and dbg.result["debug_mode"] == 4
    assert client.call("state.get", {}).result["layers"]["0:SDR"]["fald_debug_mode"] == 4
    # fald_dump is of the live mode (the monitor is in SDR here)
    bad = client.send(DesktopLutCommand("runtime.fald_dump", {"monitor": 0, "mode": "HDR", "dir": str(tmp_path)}),
                      raise_on_error=False)
    assert bad.ok is False and bad.error == "monitor is in SDR, not HDR"
    assert client.call("runtime.fald_dump", {"monitor": 0, "mode": "SDR", "dir": str(tmp_path)}).ok
    # calibration.enter clears the flag of the calibrated pair but keeps the panel file (C++ DoEnterNeutral)
    client.call("calibration.enter", {"monitor": 0, "mode": "SDR", "dummy_icc_path": "C:/dlc/sRGB.icm"})
    st = client.call("state.get", {}).result
    assert st["layers"]["0:SDR"]["fald"] is False and st["layers"]["0:SDR"]["fald_params_path"] == str(gamma)
    client.call("calibration.exit", {"restore_snapshot": True})
    st = client.call("state.get", {}).result
    assert st["layers"]["0:SDR"]["fald"] is True and set(server.state.layers["0:SDR"]) <= set(server.LAYER_NAMES)
    # desktop_gamma / tonemap stay HDR-only in SDR
    bad = client.send(DesktopLutCommand("layers.set", {"monitor": 0, "mode": "SDR", "tonemap": True}), raise_on_error=False)
    assert bad.ok is False and "HDR-only" in (bad.error or "")
    # disable_all clears the shader flags in both modes
    assert client.call("corrections.disable_all", {}).ok
    assert client.call("state.get", {}).result["layers"]["0:SDR"]["fald"] is False
    # ACM detection (C8): the pipe now distinguishes ACM_SDR from a plain SDR desktop
    mons = client.call("windows.query_monitors", {}).result["monitors"]
    assert mons[0]["color_space"] == "SDR" and "color_mode_source" in mons[0]
    server.state.acm[0] = True
    mons = client.call("windows.query_monitors", {}).result["monitors"]
    assert mons[0]["color_space"] == "ACM_SDR" and mons[0]["hdr_active"] is False
    client.call("windows.set_hdr", {"monitor": 0, "enable": True})
    assert client.call("windows.query_monitors", {}).result["monitors"][0]["color_space"] == "HDR"


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


def test_grayscale_set_live_carries_decomposed_sliders_on_the_wire():
    """2026-08-14 HDR run defect 1: the solver decomposes luminance (main slider) from
    the chromatic differential (rgb balance), but only the composed deviations rode the
    wire — the DesktopLUT editor showed a zero main slider with the common mode pushed
    into all three RGB values. Pin the extended contract: grayscale_set_live carries
    luminance[] + rgb{r,g,b}[] per point ALONGSIDE deviations (back-compat), with the
    wire invariant deviations == luminance*rgb, and the mock maps luminance onto the
    points curve exactly as the C++ ApplyGrayscalePayload does."""
    ctrl = CalibrationController.mock()
    transport = ctrl.client.transport
    ctrl.grayscale_live_begin(0, "HDR")

    points = [0.0, 0.5, 1.0]
    luminance = [1.0, 1.05, 1.0]
    rgb = {"r": [1.0, 1.02, 1.0], "g": [1.0, 0.99, 1.0], "b": [1.0, 1.0, 1.0]}
    deviations = {ch: [l * v for l, v in zip(luminance, rgb[ch])] for ch in ("r", "g", "b")}
    ctrl.grayscale_set_live(0, "HDR", 3, points, deviations,
                            luminance=luminance, rgb=rgb)

    wire = [r for r in transport.requests if r.method == "mhc.grayscale_set_live"][-1]
    gs = wire.params["grayscale"]
    # HDR passes through unbridged: the decomposition arrives verbatim...
    assert gs["luminance"] == luminance
    assert gs["rgb"] == rgb
    # ...and the composed back-compat deviations satisfy the wire invariant exactly.
    for ch in ("r", "g", "b"):
        for i in range(3):
            assert gs["deviations"][ch][i] == pytest.approx(luminance[i] * rgb[ch][i])
    # Mock fidelity (mirrors C++ ApplyGrayscalePayload): the decomposition is stored and
    # luminance scales the points curve — what the editor's main slider displays.
    cg = ctrl.state()["mhc"]["0:HDR"]["correction_grayscale"]
    assert cg["luminance"] == luminance
    assert cg["rgb"] == rgb
    assert cg["editor_points"] == pytest.approx([p * l for p, l in zip(gs["points"], luminance)])

    # Legacy composed-only call still works (no decomposition on the wire).
    ctrl.grayscale_set_live(0, "HDR", 3, points, deviations)
    wire = [r for r in transport.requests if r.method == "mhc.grayscale_set_live"][-1]
    assert "luminance" not in wire.params["grayscale"]
    assert "rgb" not in wire.params["grayscale"]


def test_grayscale_set_live_decomposed_sdr_bridge_keeps_invariant():
    """SDR: the decomposed curves are resampled onto DesktopLUT's sqrt-distributed
    signal slots the same way the composed deviations are, and the composed wire
    deviations are re-derived from the RESAMPLED pair — so deviations == luminance*rgb
    holds exactly per slot even after the bridge."""
    ctrl = CalibrationController.mock()
    ctrl.grayscale_live_begin(0, "SDR")
    points = [0.0, 0.25, 1.0]
    luminance = [1.0, 1.08, 1.02]
    rgb = {"r": [1.0, 1.03, 1.0], "g": [1.0, 1.0, 0.98], "b": [1.0, 0.97, 1.0]}
    deviations = {ch: [l * v for l, v in zip(luminance, rgb[ch])] for ch in ("r", "g", "b")}
    ctrl.grayscale_set_live(0, "SDR", 3, points, deviations,
                            luminance=luminance, rgb=rgb)
    wire = [r for r in ctrl.client.transport.requests
            if r.method == "mhc.grayscale_set_live"][-1]
    gs = wire.params["grayscale"]
    n = gs["point_count"]
    assert len(gs["luminance"]) == n and all(len(gs["rgb"][ch]) == n for ch in "rgb")
    # slots sit at signal t² (the SDR editor convention)
    assert gs["points"] == pytest.approx([(i / (n - 1)) ** 2 for i in range(n)])
    for ch in ("r", "g", "b"):
        for i in range(n):
            assert gs["deviations"][ch][i] == pytest.approx(
                gs["luminance"][i] * gs["rgb"][ch][i])


def test_cpp_grayscale_payload_reads_decomposed_sliders():
    """Static C++ pin: ApplyGrayscalePayload must parse the optional luminance[] +
    rgb{r,g,b} decomposition (shared by mhc.grayscale_set_live and
    runtime.set_grayscale_tweak) — removing it silently regresses the editor split
    back to common-mode R/G/B under a zero main slider."""
    text = _cpp_text()
    m = re.search(r'void\s+ApplyGrayscalePayload\([^)]*\)\s*\{(.*?)\n\}', text, re.DOTALL)
    assert m, "ApplyGrayscalePayload not found in the C++ IPC server"
    body = m.group(1)
    assert 'find("luminance")' in body, "C++ no longer reads the luminance[] decomposition"
    assert 'find("rgb")' in body, "C++ no longer reads the rgb{} decomposition"


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
