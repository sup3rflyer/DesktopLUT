"""In-process DesktopLUT API simulator for DLC development."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .desktoplut_client import CONTRACT_VERSION, DesktopLutCommand, DesktopLutResponse, DesktopLutTransport


class _MockApiError(Exception):
    """Internal: a request the C++ server would reject (mirrored error text)."""


@dataclass
class MockDesktopLutState:
    running: bool = True
    corrections_enabled: bool = True
    calibration_mode: dict[str, Any] | None = None
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    mhc: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    # Live HDR-active state per monitor index (the OS advanced-color flip
    # windows.set_hdr drives). Absent ⇒ SDR. Capability is fixed (see HDR_CAPABLE).
    hdr: dict[int, bool] = field(default_factory=dict)
    command_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "corrections_enabled": self.corrections_enabled,
            "calibration_mode": deepcopy(self.calibration_mode),
            "snapshots": deepcopy(self.snapshots),
            "mhc": deepcopy(self.mhc),
            "runtime": deepcopy(self.runtime),
            "hdr": deepcopy(self.hdr),
            "command_count": self.command_count,
        }


class MockDesktopLutServer:
    """Small command handler matching the DesktopLUT API contract."""

    # Fixed HDR capability per simulated monitor (mirrors query_monitors below):
    # monitor 0 is the HDR-capable primary, monitor 1 is SDR-only.
    HDR_CAPABLE = {0: True, 1: False}

    def __init__(self) -> None:
        self.state = MockDesktopLutState()

    def handle(self, command: DesktopLutCommand) -> DesktopLutResponse:
        self.state.command_count += 1
        params = command.params or {}
        method = command.method
        try:
            if method == "state.get":
                # Wire response = the full simulated state PLUS the contract version the
                # spec defines (optional on the wire; absent = a pre-versioning C++ build).
                out = self.state.as_dict()
                out["contract_version"] = CONTRACT_VERSION
                return self.ok(out)
            if method == "corrections.disable_all":
                self._cleanup_active_gs_live()   # C++ CleanupActiveGsLive runs here too
                self.state.corrections_enabled = False
                return self.ok({"corrections_enabled": False})
            if method.startswith("calibration."):
                return self.handle_calibration(method, params)
            if method.startswith("mhc."):
                return self.handle_mhc(method, params)
            if method.startswith("runtime."):
                return self.handle_runtime(method, params)
            if method == "maintenance.verify_mhc":
                # C++ DoVerifyMhc: verified = enabled && !profileName.empty() — i.e. only a
                # BAKED profile verifies. Staged-but-never-applied params must NOT verify
                # (fable Phase 9 fidelity: the previous dict-non-empty check let a sim run
                # pass a verify gate hardware would fail).
                entry = self.state.mhc.get(self.key(params)) or {}
                return self.ok({"verified": bool(entry.get("applied"))})
            if method == "windows.query_profiles":
                # C++ HandleQueryProfiles is deliberately thin (v1): DLC does the
                # authoritative ICC audit via Argyll. Mirror its shape, note included.
                return self.ok(
                    {
                        "available": False,
                        "simulated": True,
                        "monitor": params.get("monitor"),
                        "profiles": [],
                        "active_profile": None,
                        "note": "use Argyll dispwin for authoritative VCGT/profile state",
                    }
                )
            if method == "windows.query_gamma_ramp":
                # Shaped like the C++ hardware readback of a healthy neutral panel:
                # available with an IDENTITY ramp (fable Phase 9 — was available:false,
                # which made enter-neutral's ramp-evidence branch untestable under sim).
                mon = params.get("monitor", 0)
                known = mon is None or int(mon) in self.HDR_CAPABLE
                return self.ok(
                    {
                        "available": bool(known),
                        "simulated": True,
                        "monitor": mon,
                        "gamma_ramp_loaded": False if known else None,
                        "vcgt_present": False if known else None,
                    }
                )
            if method == "windows.query_monitors":
                return self.ok(self.query_monitors())
            if method == "windows.set_hdr":
                return self.handle_set_hdr(params)
            return DesktopLutResponse(ok=False, error=f"unknown method: {method}")
        except KeyError as exc:
            return DesktopLutResponse(ok=False, error=f"missing parameter: {exc.args[0]}")
        except _MockApiError as exc:
            return DesktopLutResponse(ok=False, error=str(exc))

    def ok(self, result: dict[str, Any]) -> DesktopLutResponse:
        return DesktopLutResponse(ok=True, result=result)

    def query_monitors(self) -> dict[str, Any]:
        """A deterministic two-display layout mirroring the C++ contract shape:
        monitor 0 primary (HDR-capable), monitor 1 secondary (SDR-only). The
        hdr_active/color_space fields track windows.set_hdr so orchestrator/mapping
        tests exercise display-mapping + mode-switch logic with no hardware."""
        monitors = [
            {
                "index": 0,
                "device_name": r"\\.\DISPLAY1",
                "friendly_name": "Simulated Display 0",
                "rect": {"x": 0, "y": 0, "width": 3840, "height": 2160},
                "primary": True,
                "device_path": r"\\?\DISPLAY#SIM0000#0",
                "hardware_id": "SIM0000",
                "source_id": 0,
                "target_id": 0,
                "adapter_id": {"low": 0, "high": 0},
                "hdr_capable": True,
            },
            {
                "index": 1,
                "device_name": r"\\.\DISPLAY2",
                "friendly_name": "Simulated Display 1",
                "rect": {"x": 3840, "y": 0, "width": 2560, "height": 1440},
                "primary": False,
                "device_path": r"\\?\DISPLAY#SIM0001#0",
                "hardware_id": "SIM0001",
                "source_id": 1,
                "target_id": 1,
                "adapter_id": {"low": 0, "high": 0},
                "hdr_capable": False,
            },
        ]
        for m in monitors:
            active = bool(self.state.hdr.get(m["index"], False))
            m["hdr_active"] = active
            m["color_space"] = "HDR" if active else "SDR"
        return {"available": True, "simulated": True, "count": len(monitors), "monitors": monitors}

    def handle_set_hdr(self, params: dict[str, Any]) -> DesktopLutResponse:
        if "monitor" not in params:
            return DesktopLutResponse(ok=False, error="missing parameter: monitor")
        mon = int(params["monitor"])
        if mon not in self.HDR_CAPABLE:
            return DesktopLutResponse(ok=False, error="monitor index out of range")
        capable = self.HDR_CAPABLE[mon]
        current = bool(self.state.hdr.get(mon, False))
        enable = params.get("enable")
        target = (not current) if enable is None else bool(enable)
        if target and not capable:
            return DesktopLutResponse(ok=False, error="monitor does not support HDR")
        changed = target != current
        if changed:
            self.state.hdr[mon] = target
        return self.ok(
            {
                "monitor": mon,
                "hdr_capable": capable,
                "was_active": current,
                "now_active": target,
                "changed": changed,
            }
        )

    def key(self, params: dict[str, Any]) -> str:
        """Validate monitor+mode exactly as the C++ ``ParseMonitorMode`` does (fable
        Phase 9 fidelity: the mock used to accept any monitor index / mode string,
        so a bad mapping only failed on hardware)."""
        mode = str(params["mode"])
        if mode not in ("SDR", "HDR"):
            raise _MockApiError("mode must be SDR or HDR")
        mon = int(params["monitor"])
        if mon not in self.HDR_CAPABLE:
            raise _MockApiError("monitor index out of range")
        return f"{mon}:{mode}"

    def restore(self, snapshot_id: str) -> DesktopLutResponse:
        if snapshot_id not in self.state.snapshots:
            return DesktopLutResponse(ok=False, error=f"unknown snapshot: {snapshot_id}")
        snapshot = deepcopy(self.state.snapshots[snapshot_id])
        self.state.corrections_enabled = bool(snapshot.get("corrections_enabled", True))
        self.state.calibration_mode = deepcopy(snapshot.get("calibration_mode"))
        self.state.mhc = deepcopy(snapshot.get("mhc", {}))
        self.state.runtime = deepcopy(snapshot.get("runtime", {}))
        self.state.hdr = {int(k): bool(v) for k, v in deepcopy(snapshot.get("hdr", {})).items()}
        return self.ok({"snapshot_id": snapshot_id, "restored": True})

    def _cleanup_active_gs_live(self) -> None:
        """Mirror the C++ ``CleanupActiveGsLive``: any monitor/mode with an active live-edit
        preview is reverted to its pre-begin correctionGrayscale and the session torn down.
        Called from ``calibration.exit`` / ``corrections.disable_all`` — the crash-cleanup path."""
        for st in self.state.mhc.values():
            if st.pop("gs_live_active", False):
                saved = st.pop("gs_live_saved", None)
                st["gs_preview_active"] = False
                if saved is not None:
                    st["correction_grayscale"] = saved
                else:
                    st.pop("correction_grayscale", None)

    def handle_calibration(self, method: str, params: dict[str, Any]) -> DesktopLutResponse:
        if method == "calibration.status":
            return self.ok({"active": self.state.calibration_mode is not None, "state": deepcopy(self.state.calibration_mode)})
        if method == "calibration.enter":
            key = self.key(params)  # C++ ParseMonitorMode: validate monitor index + mode vocabulary
            # NOTE (fable Phase 9): mirrors a real C++ hazard — DoEnterNeutral snapshots
            # unconditionally, so a RE-enter while calibration is already active captures the
            # already-cleared state; a later exit(restore_snapshot=True) then restores that
            # cleared state, not the user's pre-run setup (single snapshot slot in C++; here the
            # latest enter's snapshot wins the same way). The preflight settings backup is the
            # authoritative restore. DesktopLUT-side fix ticketed (keep the ORIGINAL snapshot on
            # re-enter); DLC surfaces stale calibration mode before entering.
            snapshot_id = f"snapshot-{len(self.state.snapshots) + 1}"
            self.state.snapshots[snapshot_id] = self.state.as_dict()
            self.state.corrections_enabled = False
            # C++ DoEnterNeutral clears ONLY the calibrated mode:monitor pair's layers.
            # Other pairs are preserved — the mock used to clear everything (and old C++
            # builds cleared both modes of the monitor), which permanently dropped the
            # non-calibrated mode's runtime cube on the apply path (exit without restore):
            # the 2026-08-14 HDR run lost the user's SDR cube exactly this way.
            self.state.mhc.pop(key, None)
            self.state.runtime.pop(key, None)
            self.state.calibration_mode = {
                "active": True,
                "snapshot_id": snapshot_id,
                "monitor": params["monitor"],
                "mode": str(params["mode"]).upper(),
                "dummy_icc_path": params["dummy_icc_path"],
                "reason": params.get("reason", ""),
                "corrections_reset": True,
            }
            return self.ok(deepcopy(self.state.calibration_mode))
        if method == "calibration.exit":
            # C++ DoExitCalibration runs CleanupActiveGsLive() unconditionally first — an
            # orphaned live-edit preview (client died between begin and commit) is reverted to
            # its pre-begin correction so it can't leak past the run (fable Phase 7a fidelity).
            self._cleanup_active_gs_live()
            current = deepcopy(self.state.calibration_mode)
            restore = bool(params.get("restore_snapshot", False))
            if restore and current and current.get("snapshot_id") in self.state.snapshots:
                snapshot_id = str(current["snapshot_id"])
                restored = self.restore(snapshot_id)
                if not restored.ok:
                    return restored
                self.state.calibration_mode = None
                # C++ DoExitCalibration result shape: always {active, restored}.
                return self.ok({"active": False, "restored": True, "snapshot_id": snapshot_id})
            self.state.calibration_mode = None
            return self.ok({"active": False, "restored": False})
        return DesktopLutResponse(ok=False, error=f"unknown method: {method}")

    def handle_mhc(self, method: str, params: dict[str, Any]) -> DesktopLutResponse:
        key = self.key(params)
        state = self.state.mhc.setdefault(key, {})
        if method == "mhc.set_primaries":
            state["primaries"] = deepcopy(params["primaries"])
        elif method == "mhc.set_white":
            state["white"] = {"x": params["x"], "y": params["y"]}
        elif method == "mhc.set_base_grayscale":
            state["base_grayscale"] = {
                "point_count": params.get("point_count"),
                "points": deepcopy(params.get("points", [])),
                "deviations": deepcopy(params.get("deviations", {})),
            }
        elif method == "mhc.set_base_lut":
            # Full-resolution 1D .cube import (HDR base EOTF). Takes precedence over the 32-point
            # base_grayscale at bake time, mirroring DesktopLUT's BuildMHC2Params. The C++
            # validates up-front (existence + Load1DCubeLUT) so a malformed cube fails HERE with
            # a clear error, not silently at apply time — mirror both checks (fable Phase 9;
            # catches phantom-path bugs like the Phase 5 cwd-as-cube class under --simulate).
            cube = str(params.get("cube_path") or "")
            if not cube:
                return DesktopLutResponse(ok=False, error="missing parameter: cube_path")
            cube_file = Path(cube)
            if not cube_file.exists():
                return DesktopLutResponse(ok=False, error="cube_path does not exist")
            try:
                head = cube_file.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                head = ""
            if "LUT_1D_SIZE" not in head:
                return DesktopLutResponse(ok=False, error="cube_path is not a valid 1D .cube LUT")
            state["base_lut"] = {
                "cube_path": params.get("cube_path"),
                "peak_nits": params.get("peak_nits"),
            }
        elif method == "mhc.set_correction_grayscale":
            state["correction_grayscale"] = {
                "point_count": params.get("point_count"),
                "points": deepcopy(params.get("points", [])),
                "deviations": deepcopy(params.get("deviations", {})),
            }
        elif method == "mhc.grayscale_live_begin":
            # Engage the live-edit preview (the editor's "Edit Points"): the correction GS now
            # stacks on top of MHC+3D-LUT and is measurable. No bake yet. Mirrors the C++
            # DoGrayscaleLiveBegin contract (fable Phase 7a): the PRE-BEGIN correctionGrayscale
            # is snapshotted (savedCorrectionGs) so cancel can restore the user's prior
            # correction, and the live-session marker gates set_live/commit/cancel semantics.
            # C++ errors if a session is already active (ipc_server.cpp:1133) — mirror it so the
            # SIGKILL-then-re-run orphaned-preview corner is testable.
            if state.get("gs_live_active"):
                return DesktopLutResponse(
                    ok=False, error="grayscale live preview already active for this monitor/mode")
            state["gs_preview_active"] = True
            state["gs_live_active"] = True
            state["gs_live_saved"] = deepcopy(state.get("correction_grayscale"))
            # C++ DoGrayscaleLiveBegin result shape: {monitor_mode, preview:true}.
            return self.ok({"monitor_mode": key, "preview": True,
                            "mhc": deepcopy(self.state.mhc.get(key, {}))})
        elif method == "mhc.grayscale_set_live":
            # C++ DoGrayscaleSetLive errors without an active begin.
            if not state.get("gs_live_active"):
                return DesktopLutResponse(
                    ok=False, error="no active grayscale live preview (call mhc.grayscale_live_begin first)")
            gs = params.get("grayscale", {})
            staged = {
                "point_count": gs.get("point_count"),
                "points": deepcopy(gs.get("points", [])),
                "deviations": deepcopy(gs.get("deviations", {})),
            }
            # Decomposed editor sliders (C++ ApplyGrayscalePayload): luminance[] is the
            # common/main slider, rgb{r,g,b} the balance strips; when present they are
            # authoritative — luminance scales the points curve (what the editor's main
            # slider shows) and rgb lands on the RGB balance values. Mirror the mapping so
            # a --simulate run exercises the same editor-visible split as hardware.
            lum = gs.get("luminance")
            rgb = gs.get("rgb")
            n = len(staged["points"])
            if isinstance(lum, list) and len(lum) == n:
                staged["luminance"] = deepcopy(lum)
                staged["editor_points"] = [float(p) * float(v)
                                           for p, v in zip(staged["points"], lum)]
            if isinstance(rgb, dict):
                staged["rgb"] = deepcopy(rgb)
            state["correction_grayscale"] = staged
            state["gs_preview_active"] = True
        elif method == "mhc.grayscale_commit":
            # The editor's "OK": bake correctionGrayscale into the ICM, leave it toggled on.
            # C++ DoGrayscaleCommit pops the GsLiveState (savedCorrectionGs is GONE — a later
            # cancel is a tolerated NO-OP) and returns baked:false when there was no live session
            # (e.g. DesktopLUT restarted mid-run) so the caller can detect a lost bake.
            state["gs_preview_active"] = False
            baked = bool(state.pop("gs_live_active", False))
            if baked:
                state.pop("gs_live_saved", None)
                state["gs_committed"] = True
                state["applied"] = True
            return self.ok({"monitor_mode": key, "baked": baked,
                            "mhc": deepcopy(self.state.mhc.get(key, {}))})
        elif method == "mhc.grayscale_cancel":
            # C++ DoGrayscaleCancel: restore the PRE-BEGIN correctionGrayscale (the user's
            # prior correction, not bare identity) and tear down the preview; a cancel with
            # no live session (incl. after a commit) is a tolerated no-op returning canceled:false.
            state["gs_preview_active"] = False
            canceled = bool(state.pop("gs_live_active", False))
            if canceled:
                saved = state.pop("gs_live_saved", None)
                if saved is not None:
                    state["correction_grayscale"] = saved
                else:
                    state.pop("correction_grayscale", None)
            return self.ok({"monitor_mode": key, "canceled": canceled,
                            "mhc": deepcopy(self.state.mhc.get(key, {}))})
        elif method == "mhc.apply":
            # C++ DoMhcApply bakes + installs and reports the profile name inside the mhc
            # object ({applied:true, profile_name}). Carry a simulated name so the DLC-side
            # profile_name plumbing is exercised under --simulate (fable Phase 9).
            state["applied"] = True
            state.setdefault("profile_name", f"DesktopLUT-sim-{key.replace(':', '-')}.icm")
        elif method == "mhc.remove":
            self.state.mhc.pop(key, None)
            return self.ok({"monitor_mode": key, "removed": True})
        else:
            return DesktopLutResponse(ok=False, error=f"unknown method: {method}")
        return self.ok({"monitor_mode": key, "mhc": deepcopy(self.state.mhc.get(key, {}))})

    def handle_runtime(self, method: str, params: dict[str, Any]) -> DesktopLutResponse:
        key = self.key(params)
        state = self.state.runtime.setdefault(key, {})
        if method == "runtime.set_3dlut":
            # C++ DoSet3dlut rejects a nonexistent cube_path up-front — mirror it so a
            # phantom path fails under --simulate too, not just on hardware (fable Phase 9).
            cube = str(params["cube_path"] or "")
            if not cube:
                return DesktopLutResponse(ok=False, error="missing parameter: cube_path")
            if not Path(cube).exists():
                return DesktopLutResponse(ok=False, error="cube_path does not exist")
            state["cube_path"] = params["cube_path"]
        elif method == "runtime.clear_3dlut":
            state.pop("cube_path", None)
        elif method == "runtime.set_grayscale_tweak":
            state["grayscale_tweak"] = deepcopy(params.get("grayscale_tweak", {}))
        elif method == "runtime.disable_grayscale_tweak":
            state.pop("grayscale_tweak", None)
        else:
            return DesktopLutResponse(ok=False, error=f"unknown method: {method}")
        return self.ok({"monitor_mode": key, "runtime": deepcopy(self.state.runtime.get(key, {}))})


class MockDesktopLutTransport:
    def __init__(self, server: MockDesktopLutServer | None = None) -> None:
        self.server = server or MockDesktopLutServer()
        self.requests: list[DesktopLutCommand] = []

    def request(self, command: DesktopLutCommand) -> DesktopLutResponse:
        self.requests.append(command)
        return self.server.handle(command)
