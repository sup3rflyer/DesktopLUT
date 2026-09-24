"""In-process DesktopLUT API simulator for DLC development."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .desktoplut_client import CONTRACT_VERSION, DesktopLutCommand, DesktopLutResponse, DesktopLutTransport


class _MockApiError(Exception):
    """Internal: a request the C++ server would reject (mirrored error text)."""


def _fald_file_has_boost(path: Path) -> bool:
    """C++ FaldPanelFileHasBoost: an FLD4 header whose word 48 (boost step count) is 1..24."""
    import struct
    try:
        head = path.read_bytes()[: 49 * 4]
    except OSError:
        return False
    if len(head) != 49 * 4 or struct.unpack("<I", head[:4])[0] != 0x464C4434:
        return False
    return 1 <= struct.unpack("<I", head[48 * 4:49 * 4])[0] <= 24


def _fald_file_transfer(path: Path) -> str | None:
    """C++ FaldPanelFileTransfer: 'pq' for FLD1/FLD2, FLD3/FLD4 word 40 (0 pq / 1 gamma), None when unreadable."""
    import struct
    try:
        head = path.read_bytes()[: 41 * 4]
    except OSError:
        return None
    if len(head) < 4:
        return None
    magic = struct.unpack("<I", head[:4])[0]
    if magic in (0x464C4431, 0x464C4432):
        return "pq"
    if magic in (0x464C4433, 0x464C4434) and len(head) == 41 * 4:
        code = struct.unpack("<I", head[40 * 4:41 * 4])[0]
        return {0: "pq", 1: "gamma"}.get(code)
    return None


# runtime.fald_starfield (C++ DoFaldStarfield, work guide S1): pipe key -> (lo, hi, integer, refusal text). The order
# and the texts are the C++ table's, word for word; the defaults are dlc.fald.starfield.StarfieldParams'.
_FALD_STAR_KEYS: dict[str, tuple[float, float, bool, str]] = {
    "even": (0.0, 1.0, False, "even must be 0..1"),
    "lift": (0.0, 1.0, False, "lift must be 0..1"),
    "target_gain": (0.05, 2.0, False, "target_gain must be 0.05..2"),
    "even_reach": (0.0, 12.0, True, "even_reach must be an integer 0..12"),
    "cap_nits": (0.0, 10000.0, False, "cap_nits must be 0..10000 (0 = none)"),
    "strength": (0.0, 1.0, False, "strength must be 0..1"),
    "area_lo": (0.0, 1.0e6, False, "area_lo must be 0..1000000 px^2"),
    "area_hi": (0.0, 1.0e6, False, "area_hi must be 0..1000000 px^2"),
    "peak_hi": (0.0, 10000.0, False, "peak_hi must be 0..10000 (0 = no limit)"),
    "reach": (0.0, 4.0, True, "reach must be an integer 0..4"),
    "nb_lo": (0.0, 1.0, False, "nb_lo must be 0..1"),
    "nb_hi": (0.0, 1.0, False, "nb_hi must be 0..1"),
    "target_sigma": (0.0, 4.0, False, "target_sigma must be 0..4"),
    "keep_nits": (0.0, 10000.0, False, "keep_nits must be 0..10000 (0 = no floor)"),
}
_FALD_STAR_DEFAULTS: dict[str, Any] = {"enabled": False, "even": 0.8, "lift": 0.0, "target_gain": 1.0, "target_sigma": 0.0, "keep_nits": 100.0, "even_reach": 8,
                                       "cap_nits": 0.0, "strength": 1.0, "area_lo": 40.0, "area_hi": 160.0, "peak_hi": 0.0,
                                       "reach": 2, "nb_lo": 0.15, "nb_hi": 0.30}


def _fald_star(entry: dict[str, Any] | None) -> dict[str, Any]:
    """The pair's starfield settings (defaults where never set), ints for the two reaches."""
    st = {**_FALD_STAR_DEFAULTS, **((entry or {}).get("star") or {})}
    return {k: (bool(v) if k == "enabled" else int(v) if k in ("even_reach", "reach") else float(v)) for k, v in st.items()}


# runtime.fald_glowfill (C++ DoFaldGlowFill, work guide S2): pipe key -> (lo, hi, integer, refusal text), the C++ texts
# word for word; the defaults are dlc.fald.glowfill.GlowFillParams'.
_FALD_GLOW_KEYS: dict[str, tuple[float, float, bool, str]] = {
    "strength": (0.0, 1.0, False, "strength must be 0..1"),
    "reach": (1.0, 4.0, True, "reach must be an integer 1..4"),
    "cap_nits": (0.005, 0.5, False, "cap_nits must be 0.005..0.5"),
}
_FALD_GLOW_DEFAULTS: dict[str, Any] = {"enabled": False, "strength": 1.0, "reach": 2, "cap_nits": 0.05}
# HDR only (C++ FALD_GLOW_SDR_NOTE, word for word): the pipe refuses `enabled: true` for an SDR pair and state.get says why
_FALD_GLOW_SDR_NOTE = ("glow fill is HDR only: the levels behind its request ceiling (drive floor, LIT level, count threshold) "
                       "are HDR measurements")
# Part of the starfield feature (C++ FALD_GLOW_NEEDS_STAR_NOTE, word for word): the switch is stored, the fill runs only
# while starfield balancing is on — state.get's fald_glow_active / runtime.fald_glowfill's `active` say whether it runs
_FALD_GLOW_NEEDS_STAR_NOTE = ("glow fill is part of the starfield feature: the switch is stored, but the fill runs only while "
                              "starfield balancing is on")


def _fald_glow(entry: dict[str, Any] | None) -> dict[str, Any]:
    """The pair's glow-fill settings (defaults where never set), an int for the reach."""
    gl = {**_FALD_GLOW_DEFAULTS, **((entry or {}).get("glow") or {})}
    return {k: (bool(v) if k == "enabled" else int(v) if k == "reach" else float(v)) for k, v in gl.items()}


def _fald_state_keys(entry: dict[str, Any] | None, is_hdr: bool = True) -> dict[str, Any]:
    """The fald_* keys C++ HandleStateGet puts into layers[key] for every pair."""
    entry = entry or {}
    path = str(entry.get("params_path") or "")
    out: dict[str, Any] = {"fald_params_path": path, "fald_debug_mode": int(entry.get("debug_mode", 0)),
                           "fald_ped_mode": int(entry.get("ped_mode", 0)), "fald_ped_colour_in_file": False,
                           "fald_boost_in_file": _fald_file_has_boost(Path(path)) if path else False,   # FLD4 boost LUT (C12)
                           # temporal drive state (2026-09-17, work guide H5 / item 4a): persisted like ped_mode
                           "fald_temporal_mode": int(entry.get("temporal_mode", 0)),
                           "fald_tau_rise_ms": float(entry.get("tau_rise_ms", 0.0)),
                           "fald_tau_fall_ms": float(entry.get("tau_fall_ms", 0.0)),
                           "fald_delay_frames": int(entry.get("delay_frames", 0)),
                           # temporal mode 3 "panel clock" (2026-09-20, work guide C13): closure per tick + tick parity
                           "fald_temporal_closure": float(entry.get("closure", 0.72)),
                           "fald_temporal_parity": int(entry.get("parity", -1))}
    # starfield balancing (2026-09-19, work guide S1; runtime.fald_starfield): the switch + every numeric field
    star = _fald_star(entry)
    out["fald_starfield"] = star["enabled"]
    out.update({f"fald_star_{k}": v for k, v in star.items() if k != "enabled"})
    # glow fill (2026-09-20, work guide S2; runtime.fald_glowfill)
    glow = _fald_glow(entry)
    out["fald_glowfill"] = glow["enabled"]
    out.update({f"fald_glow_{k}": v for k, v in glow.items() if k != "enabled"})
    out["fald_glow_active"] = bool(glow["enabled"] and star["enabled"] and is_hdr)
    if not is_hdr:
        out["fald_glow_note"] = _FALD_GLOW_SDR_NOTE
    elif glow["enabled"] and not star["enabled"]:
        out["fald_glow_note"] = _FALD_GLOW_NEEDS_STAR_NOTE
    transfer = _fald_file_transfer(Path(path)) if path else None
    if transfer is not None:
        out["fald_file_transfer"] = transfer
    return out


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
    # Windows ACM ("Automatically manage color for apps") per monitor index (C++ 2026-09-14, work guide
    # C8: query_monitors reads it through DisplayConfig — the DXGI colour space cannot see it). Absent
    # = off. There is no pipe verb for it (a Windows Settings toggle); tests set it on the state directly.
    acm: dict[int, bool] = field(default_factory=dict)
    # Viewing layers per "monitor:MODE" (C++ HandleStateGet "layers"): the MHC's white
    # balance / correction grayscale / Desktop Gamma bits, the HDR tonemap shader flag and the
    # per-mode FALD flag (HDR, or SDR under ACM). Absent key = all OFF (a fresh install).
    layers: dict[str, dict[str, bool]] = field(default_factory=dict)
    # FALD layer settings per "monitor:MODE" that are NOT flags (C++ FaldSettings: paramsPath, debugMode, pedMode).
    # Reported inside layers[key] like the C++ HandleStateGet; kept through calibration.enter / disable_all
    # (the C++ clears only the enabled flag).
    fald: dict[str, dict[str, Any]] = field(default_factory=dict)
    # DWM-hook LUT routing (C++ HandleStateGet "hook"): the sticky per-DWM-session
    # context->monitor assignment. ``hook_twins`` = the first two monitors are same-size/
    # same-bpc so the DLL had to ORDER-match them (the 2026-09-03 coin toss); ``hook_routing_
    # crossed`` = that order-match landed the calibrated monitor's cube on the twin (invisible
    # in the routing report — only the meter can tell, which is why dlc.hook_routing exists);
    # ``hook_pinned`` = a client swap/assign rewrote the file (entries report "pinned");
    # ``hook_confirmed`` = a client confirmed the assignment through the meter.
    hook_twins: bool = False
    hook_routing_crossed: bool = False
    hook_pinned: bool = False
    hook_confirmed: bool = False
    hook_routing_present: bool = True
    hook_session: str = "4242-133700000000000000"
    # FP16 overlay auto-sleep (C++ render.cpp RenderAll: the overlay hides when no monitor needs processing and
    # state.get reports overlay.awake). Awake while a shader layer that can run is on (``overlay_needed``) or a
    # runtime cube is loaded. Test knobs: ``overlay_keep_awake`` = something else needs the overlay (another
    # monitor's cube, analysis, an open editor) so it never sleeps; ``overlay_sleep_lag_polls`` = after its last user
    # went away the overlay still reports awake for this many state.get polls (the render thread's next pass).
    overlay_keep_awake: bool = False
    overlay_sleep_lag_polls: int = 0
    overlay_awake: bool = False
    overlay_lag_left: int = 0
    command_count: int = 0

    def overlay_needed(self) -> bool:
        """C++ RenderAll's anyMonitorNeedsOverlay, reduced to what the sim models: a runtime cube (not passthrough), or a
        shader layer of the monitor's LIVE mode that can run — tonemap in HDR, FALD with a panel file set (the transfer
        check happens at runtime.set_fald_params). A FALD flag without a file, or on the other mode's row, keeps the
        overlay asleep (the C++ never wakes it for a layer that cannot run). ACM is not modelled (SDR FALD = on)."""
        if self.overlay_keep_awake or any((r or {}).get("cube_path") for r in self.runtime.values()):
            return True
        for key, d in self.layers.items():
            mon, _, mode = key.partition(":")
            try:
                live = "HDR" if self.hdr.get(int(mon), False) else "SDR"
            except ValueError:
                continue
            if mode != live:
                continue
            if d.get("tonemap") and mode == "HDR":
                return True
            if d.get("fald") and (self.fald.get(key) or {}).get("params_path"):
                return True
        return False

    def overlay_tick(self, poll: bool) -> bool:
        """Advance the auto-sleep model: a needed overlay is awake now (and re-arms the lag); an unneeded one sleeps
        after ``overlay_sleep_lag_polls`` more polls. ``poll`` = a state.get observation (counts down the lag)."""
        if self.overlay_needed():
            self.overlay_awake = True
            self.overlay_lag_left = int(self.overlay_sleep_lag_polls)
        elif self.overlay_awake and poll:
            if self.overlay_lag_left > 0:
                self.overlay_lag_left -= 1
            else:
                self.overlay_awake = False
        return self.overlay_awake

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "corrections_enabled": self.corrections_enabled,
            "calibration_mode": deepcopy(self.calibration_mode),
            "snapshots": deepcopy(self.snapshots),
            "mhc": deepcopy(self.mhc),
            "runtime": deepcopy(self.runtime),
            "hdr": deepcopy(self.hdr),
            # C++ reports every monitor:mode pair (absent = a fresh install, all OFF), fald settings included
            "layers": {k: {"tonemap": False, "fald": False, "desktop_gamma": False, "white_balance": False,
                           "grayscale": False, **(self.layers.get(k) or {}), **_fald_state_keys(self.fald.get(k), k.endswith(":HDR"))}
                       for k in sorted(set(self.layers) | {f"{m}:{md}" for m in (0, 1) for md in ("SDR", "HDR")})},
            "fald": deepcopy(self.fald),
            "overlay_model": {"keep_awake": self.overlay_keep_awake, "sleep_lag_polls": self.overlay_sleep_lag_polls,
                              "awake": self.overlay_awake, "lag_left": self.overlay_lag_left},
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
                out["hook"] = self.hook_view()
                # C++ 2026-09-13: which path renders the frame (overlay awake unless nothing needs
                # it — MockDesktopLutState.overlay_tick; the sim is never in DWM-hook mode)
                out["overlay"] = {"awake": self.state.overlay_tick(poll=True), "dwm_hook_mode": False}
                out.pop("overlay_model", None)
                return self.ok(out)
            if method == "hook.set_routing":
                return self.handle_hook_set_routing(params)
            if method == "corrections.disable_all":
                self._cleanup_active_gs_live()   # C++ CleanupActiveGsLive runs here too
                self.state.corrections_enabled = False
                # C++ DoDisableAll clears the shader flags of every monitor:mode (tonemap, fald in BOTH modes)
                for cur in self.state.layers.values():
                    cur["tonemap"] = False
                    cur["fald"] = False
                return self.ok({"corrections_enabled": False})
            if method == "layers.set":
                return self.handle_layers_set(params)
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
            acm = bool(self.state.acm.get(m["index"], False))
            m["hdr_active"] = active
            # C++ 2026-09-14 (C8): ACM_SDR comes from DisplayConfig (24H2 activeColorMode WCG, older builds
            # advancedColorEnabled && !HDR); color_mode_source names the query that decided.
            m["color_space"] = "HDR" if active else ("ACM_SDR" if acm else "SDR")
            m["color_mode_source"] = "dxgi" if active else "displayconfig2"
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
        self.state.layers = {k: {n: bool(v) for n, v in (d or {}).items() if n in self.LAYER_NAMES}
                             for k, d in deepcopy(snapshot.get("layers", {})).items()}
        self.state.fald = deepcopy(snapshot.get("fald", {}))
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

    LAYER_NAMES = ("tonemap", "desktop_gamma", "white_balance", "grayscale", "fald")
    SHADER_LAYERS = ("tonemap", "fald")      # shader flags: no MHC re-bake

    def handle_layers_set(self, params: dict[str, Any]) -> DesktopLutResponse:
        """C++ DoLayersSet: set the given layer flags for monitor:mode; an MHC-layer change on
        an APPLIED profile re-bakes it under a new name (permutation churn — the profile_name a
        client saw before the toggle is gone, its source_file identity is not). desktop_gamma and
        tonemap are HDR-only; fald is per mode (HDR, or SDR under ACM — 2026-09-14, work guide P7)."""
        key = self.key(params)
        is_hdr = key.endswith(":HDR")
        cur = dict(self.state.layers.get(key) or {n: False for n in self.LAYER_NAMES})
        before = {**cur, "fald_params_path": str((self.state.fald.get(key) or {}).get("params_path") or "")}   # C++ LayersJson
        mhc_changed = False
        for name in self.LAYER_NAMES:
            if name in params:
                val = bool(params[name])
                if not is_hdr and name in ("desktop_gamma", "tonemap"):
                    if val:
                        return DesktopLutResponse(ok=False, error="desktop_gamma / tonemap are HDR-only layers")
                    continue
                if cur.get(name) != val:
                    cur[name] = val
                    if name not in self.SHADER_LAYERS:
                        mhc_changed = True
        self.state.layers[key] = cur
        entry = self.state.mhc.get(key) or {}
        regenerated = False
        if mhc_changed and entry.get("applied") and entry.get("profile_name"):
            entry["profile_name"] = f"DesktopLUT-sim-{key.replace(':', '-')}-perm{self.state.command_count}.icm"
            regenerated = True
        after = {**cur, "fald_params_path": before["fald_params_path"]}
        return self.ok({"monitor_mode": key, "before": before, "after": after,
                        "regenerated": regenerated, "profile_name": entry.get("profile_name")})

    # -- DWM-hook LUT routing (C++ HandleStateGet "hook" + DoHookSetRouting) ------------------
    def hook_view(self) -> dict[str, Any]:
        """The ``hook`` object of ``state.get``: one DWM overlay context per simulated monitor.
        Twins (``hook_twins``) report ``order`` until a swap/assign pins them; ``needs_check``
        follows the C++ rule — an order/pinned entry that is not confirmed, or a stale session.
        A CROSSED assignment is deliberately invisible here (the entries still claim their own
        monitor): the routing file cannot know which physical panel a context paints, so the
        report is identical either way — exactly the 2026-09-03 failure mode."""
        st = self.state
        view: dict[str, Any] = {"active": True, "needs_check": False}
        if not st.hook_routing_present:
            return view
        entries = []
        for m in self.query_monitors()["monitors"]:
            idx = int(m["index"])
            twin = st.hook_twins and idx in (0, 1)
            method = ("pinned" if st.hook_pinned else "order") if twin else "unique"
            entries.append({"ctx": f"0x{0x1F3A0000 + idx:X}", "left": int(m["rect"]["x"]),
                            "top": int(m["rect"]["y"]), "method": method, "monitor": idx})
        stale = False
        ambiguous = any(e["method"] in ("order", "pinned") for e in entries)
        view["needs_check"] = bool(stale or (ambiguous and not st.hook_confirmed))
        view["routing"] = {"session": st.hook_session, "stale": stale,
                           "confirmed": bool(st.hook_confirmed), "entries": entries}
        return view

    def handle_hook_set_routing(self, params: dict[str, Any]) -> DesktopLutResponse:
        """C++ DoHookSetRouting: ``swap`` trades the calibrated monitor's position with its
        single same-size/same-bpc twin and re-injects (the DLL then honours the PINNED file);
        ``confirm`` marks the assignment meter-verified without re-injecting; ``clear``
        deletes the file and re-injects (a fresh roll); ``assign`` pins explicit entries."""
        st = self.state
        action = str(params.get("action") or "")
        if action == "confirm":
            if not st.hook_routing_present:
                return DesktopLutResponse(ok=False, error="no hook routing to confirm")
            st.hook_confirmed = True
            return self.ok({"hook": self.hook_view(), "reinjected": False})
        if action == "swap":
            if "monitor" not in params:
                return DesktopLutResponse(ok=False, error="missing parameter: monitor")
            mon = int(params["monitor"])
            if mon not in self.HDR_CAPABLE:
                return DesktopLutResponse(ok=False, error="monitor index out of range")
            if not (st.hook_twins and mon in (0, 1)):
                return DesktopLutResponse(
                    ok=False, error=f"monitor {mon} has no single same-size/same-bpc twin to swap with")
            # The swap moves the crossed pairing to the other pairing: the DLL now paints the
            # calibrated monitor's cube on the panel the previous roll gave the twin.
            st.hook_routing_crossed = not st.hook_routing_crossed
            st.hook_pinned = True
            st.hook_confirmed = False
            return self.ok({"hook": self.hook_view(), "reinjected": True})
        if action == "clear":
            st.hook_routing_present = True
            st.hook_pinned = False
            st.hook_confirmed = False
            st.hook_routing_crossed = False    # deterministic "fresh roll" for the simulator
            return self.ok({"hook": self.hook_view(), "reinjected": True})
        if action == "assign":
            entries = params.get("entries")
            if not isinstance(entries, list) or not entries:
                return DesktopLutResponse(ok=False, error="missing parameter: entries")
            for e in entries:
                if not isinstance(e, dict) or not all(k in e for k in ("ctx", "left", "top")):
                    return DesktopLutResponse(ok=False, error="entries must be [{ctx, left, top}]")
            st.hook_routing_present = True
            st.hook_pinned = True
            st.hook_confirmed = False
            return self.ok({"hook": self.hook_view(), "reinjected": True})
        return DesktopLutResponse(ok=False, error="action must be swap, confirm, clear or assign")

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
            # C++ clears WB/GS/DG + tonemap for the pair (the snapshot above keeps the user's)
            self.state.layers[key] = {n: False for n in self.LAYER_NAMES}
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
            had_profile = bool((self.state.mhc.pop(key, None) or {}).get("profile_name"))
            # C++ DoMhcRemove swaps in the identity MHC2 profile before disassociating the real one.
            ident = f"DesktopLUT_Mon{key.split(':')[0]}_{key.split(':')[1]}_Identity.icm" if had_profile else ""
            return self.ok({"monitor_mode": key, "removed": True, "identity_profile": ident})
        else:
            return DesktopLutResponse(ok=False, error=f"unknown method: {method}")
        return self.ok({"monitor_mode": key, "mhc": deepcopy(self.state.mhc.get(key, {}))})

    def handle_fald(self, method: str, key: str, params: dict[str, Any]) -> DesktopLutResponse:
        """C++ DoSetFaldParams / DoFaldDebug / DoFaldDump — per mode since 2026-09-14 (work guide P7)."""
        is_hdr = key.endswith(":HDR")
        fs = self.state.fald.setdefault(key, {})
        if method == "runtime.set_fald_params":
            # the file must exist and not be a directory, and a readable panel file's transfer must match the mode
            # (FLD1/FLD2 = PQ = HDR; FLD3 word 40 1 = gamma = SDR under ACM). A file the header peek cannot classify
            # is accepted (the C++ loader refuses it later, logged once) — the contract test hands over a .cube.
            path = str(params.get("params_path") or "")
            if not path:
                return DesktopLutResponse(ok=False, error="missing parameter: params_path")
            if not Path(path).is_file():
                return DesktopLutResponse(ok=False, error="params_path is not a file")
            transfer = _fald_file_transfer(Path(path))
            if transfer is not None and transfer != ("pq" if is_hdr else "gamma"):
                label = "gamma (SDR fit)" if transfer == "gamma" else "pq (HDR fit)"
                return DesktopLutResponse(ok=False, error=f"panel file transfer {label} does not match mode "
                                                          f"{'HDR' if is_hdr else 'SDR'}")
            fs["params_path"] = path
            return self.ok({"monitor_mode": key, "params_path": path, "transfer": transfer or "unknown"})
        if method == "runtime.fald_debug":
            mode = params.get("debug_mode"); ped = params.get("ped_mode")
            if not isinstance(mode, (int, float)) and not isinstance(ped, (int, float)):
                return DesktopLutResponse(ok=False, error="missing parameter: debug_mode (0..10) or ped_mode (0|1)")
            if isinstance(mode, (int, float)):
                fs["debug_mode"] = int(min(10, max(0, mode)))
            if isinstance(ped, (int, float)):
                fs["ped_mode"] = 1 if ped >= 0.5 else 0      # persisted in the real app (the GUI checkbox)
            return self.ok({"monitor_mode": key, "debug_mode": fs.get("debug_mode", 0), "ped_mode": fs.get("ped_mode", 0),
                            "ped_colour_in_file": False})      # mock: no panel file is ever parsed
        if method == "runtime.fald_temporal":
            # C++ DoFaldTemporal (2026-09-17): the shader's per-cell drive state (LED-lag filter), persisted per mode
            tm = params.get("temporal_mode"); tr = params.get("tau_rise_ms"); tf = params.get("tau_fall_ms")
            df = params.get("delay_frames"); cl = params.get("closure"); pa = params.get("parity")
            num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
            if not (num(tm) or num(tr) or num(tf) or num(df) or num(cl) or num(pa)):
                return DesktopLutResponse(ok=False, error="missing parameter: temporal_mode (0|1|2|3), tau_rise_ms, tau_fall_ms (0..5000), delay_frames (0..3), closure (0.05..1) or parity (-1|0|1)")
            if num(tm) and tm not in (0, 1, 2, 3):
                return DesktopLutResponse(ok=False, error="temporal_mode must be 0 (off), 1 (both fields), 2 (B_true only) or 3 (panel clock)")
            for name, v in (("tau_rise_ms", tr), ("tau_fall_ms", tf)):
                if num(v) and not (0.0 <= v <= 5000.0):
                    return DesktopLutResponse(ok=False, error=f"{name} must be 0..5000 ms")
            if num(df) and df not in (0, 1, 2, 3):
                return DesktopLutResponse(ok=False, error="delay_frames must be 0..3")
            if num(cl) and not (0.05 <= cl <= 1.0):
                return DesktopLutResponse(ok=False, error="closure must be 0.05..1")
            if num(pa) and pa not in (-1, 0, 1):
                return DesktopLutResponse(ok=False, error="parity must be -1 (unknown), 0 or 1")
            if num(tm):
                fs["temporal_mode"] = int(tm)
            if num(tr):
                fs["tau_rise_ms"] = float(tr)
            if num(tf):
                fs["tau_fall_ms"] = float(tf)
            if num(df):
                fs["delay_frames"] = int(df)
            if num(cl):
                fs["closure"] = float(cl)
            if num(pa):
                fs["parity"] = int(pa)
            from dlc.fald.paneltime import MODE_PANEL, settle_refreshes
            from dlc.fald.temporal import settle_frames
            rise = float(fs.get("tau_rise_ms", 0.0)); fall = float(fs.get("tau_fall_ms", 0.0)); delay = int(fs.get("delay_frames", 0))
            mode_now = int(fs.get("temporal_mode", 0)); closure = float(fs.get("closure", 0.72))
            settle = settle_refreshes(closure) if mode_now == MODE_PANEL else settle_frames(rise, fall, 1000.0 / 60.0, delay)
            return self.ok({"monitor_mode": key, "temporal_mode": mode_now, "tau_rise_ms": rise, "tau_fall_ms": fall,
                            "delay_frames": delay, "closure": closure, "parity": int(fs.get("parity", -1)),
                            "settle_frames_60hz": settle})
        if method == "runtime.fald_starfield":
            # C++ DoFaldStarfield (2026-09-19, work guide S1): starfield balancing, partial updates, persisted per mode.
            # Everything is validated before anything is stored; refusals word for word.
            num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
            en = params.get("enabled")
            if "enabled" in params and not isinstance(en, bool):
                return DesktopLutResponse(ok=False, error="enabled must be a boolean")
            given: dict[str, Any] = {}
            for name, (lo, hi, integer, text) in _FALD_STAR_KEYS.items():
                v = params.get(name)
                if not num(v):
                    continue
                if not (lo <= v <= hi) or (integer and v != int(v)):
                    return DesktopLutResponse(ok=False, error=text)
                given[name] = int(v) if integer else float(v)
            if "enabled" not in params and not given:
                return DesktopLutResponse(ok=False, error="missing parameter: enabled, even, lift, target_gain, target_sigma, keep_nits, even_reach, cap_nits, "
                                                          "strength, area_lo, area_hi, peak_hi, reach, nb_lo or nb_hi")
            st = {**_fald_star(fs), **given}
            if "enabled" in params:
                st["enabled"] = en
            if st["area_hi"] < st["area_lo"]:       # the pairs stay ordered after a partial update (the stored partner counts)
                return DesktopLutResponse(ok=False, error="area_hi must be >= area_lo")
            if st["nb_hi"] < st["nb_lo"]:
                return DesktopLutResponse(ok=False, error="nb_hi must be >= nb_lo")
            fs["star"] = st
            return self.ok({"monitor_mode": key, **_fald_star(fs)})
        if method == "runtime.fald_glowfill":
            # C++ DoFaldGlowFill (2026-09-20, work guide S2): the glow fill, partial updates, persisted per mode.
            # Everything is validated before anything is stored; refusals word for word.
            num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
            en = params.get("enabled")
            if "enabled" in params and not isinstance(en, bool):
                return DesktopLutResponse(ok=False, error="enabled must be a boolean")
            given = {}
            for name, (lo, hi, integer, text) in _FALD_GLOW_KEYS.items():
                v = params.get(name)
                if not num(v):
                    continue
                if not (lo <= v <= hi) or (integer and v != int(v)):
                    return DesktopLutResponse(ok=False, error=text)
                given[name] = int(v) if integer else float(v)
            if "enabled" not in params and not given:
                return DesktopLutResponse(ok=False, error="missing parameter: enabled, strength, reach or cap_nits")
            if en is True and not is_hdr:
                return DesktopLutResponse(ok=False, error=_FALD_GLOW_SDR_NOTE)
            gl = {**_fald_glow(fs), **given}
            if "enabled" in params:
                gl["enabled"] = en
            fs["glow"] = gl
            out = {"monitor_mode": key, **_fald_glow(fs)}
            star_on = _fald_star(fs)["enabled"]
            out["active"] = bool(out["enabled"] and star_on and is_hdr)
            if out["enabled"] and not star_on:
                out["note"] = _FALD_GLOW_NEEDS_STAR_NOTE
            return self.ok(out)
        if method == "runtime.fald_dump":
            d = str(params.get("dir") or "")
            if not d:
                return DesktopLutResponse(ok=False, error="missing parameter: dir")
            if not Path(d).is_dir():
                return DesktopLutResponse(ok=False, error="dir does not exist")
            live_hdr = bool(self.state.hdr.get(int(params["monitor"]), False))
            if live_hdr != is_hdr:   # C++: the dump is of the layer that runs, i.e. the live mode
                return DesktopLutResponse(ok=False, error=f"monitor is in {'HDR' if live_hdr else 'SDR'}, "
                                                          f"not {'HDR' if is_hdr else 'SDR'}")
            return self.ok({"monitor_mode": key, "dir": d, "note": "mock: no render thread; nothing is written"})
        return DesktopLutResponse(ok=False, error=f"unknown method: {method}")

    def handle_runtime(self, method: str, params: dict[str, Any]) -> DesktopLutResponse:
        key = self.key(params)
        if method.startswith("runtime.fald_") or method == "runtime.set_fald_params":
            return self.handle_fald(method, key, params)       # C++: FaldSettings, not a runtime entry
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

    # Test-settable hook-routing knobs (see MockDesktopLutState): ``hook_twins`` makes the
    # first two monitors an order-matched twin pair; ``hook_routing_crossed`` is the coin
    # toss landing the calibrated monitor's cube on the twin. A synthetic panel that wants
    # to model the 2026-09-03 failure reads these to decide whether the cube reaches the
    # measured patch; a swap flips ``hook_routing_crossed``.
    @property
    def hook_twins(self) -> bool:
        return self.server.state.hook_twins

    @hook_twins.setter
    def hook_twins(self, value: bool) -> None:
        self.server.state.hook_twins = bool(value)

    @property
    def hook_routing_crossed(self) -> bool:
        return self.server.state.hook_routing_crossed

    @hook_routing_crossed.setter
    def hook_routing_crossed(self, value: bool) -> None:
        self.server.state.hook_routing_crossed = bool(value)
