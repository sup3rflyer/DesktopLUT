"""High-level calibration controller.

A thin, typed wrapper over the DesktopLUT NDJSON client that exposes exactly the
operations the calibration harness needs. Every "apply"/"clear"/"refine" in the
harness goes through here, so the rest of DLC never touches the wire protocol.

Transport is swappable: `CalibrationController.mock()` runs against the in-process
simulator for tests and `--simulate` rehearsals; `CalibrationController.connect()`
talks to the real named pipe once the DesktopLUT C++ IPC server is running.
"""

from __future__ import annotations

from typing import Any, Optional

from .desktoplut_client import (
    DEFAULT_PIPE_NAME,
    DesktopLutClient,
    DesktopLutTransport,
    NamedPipeTransport,
)
from .mhc_grayscale import to_desktoplut_sdr_grayscale, to_desktoplut_sdr_grayscale_decomposed


def normalize_mode(mode: str) -> str:
    m = str(mode).upper()
    if m not in ("SDR", "HDR"):
        raise ValueError(f"mode must be SDR or HDR, got {mode!r}")
    return m


class CalibrationController:
    def __init__(self, client: DesktopLutClient) -> None:
        self.client = client

    # -- construction ------------------------------------------------------
    @classmethod
    def connect(cls, pipe_name: str = DEFAULT_PIPE_NAME, *, timeout_s: float = 75.0) -> "CalibrationController":
        return cls(DesktopLutClient(pipe_name, NamedPipeTransport(pipe_name, timeout_s=timeout_s)))

    @classmethod
    def with_transport(cls, transport: DesktopLutTransport) -> "CalibrationController":
        return cls(DesktopLutClient(transport=transport))

    @classmethod
    def mock(cls) -> "CalibrationController":
        """Controller bound to a fresh in-process simulator (tests / --simulate)."""
        from .desktoplut_mock import MockDesktopLutTransport

        return cls.with_transport(MockDesktopLutTransport())

    # -- low-level ---------------------------------------------------------
    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.client.call(method, params).result or {}

    # -- state / windows ---------------------------------------------------
    def state(self) -> dict[str, Any]:
        return self.call("state.get")

    def query_profiles(self, monitor: int | None = None) -> dict[str, Any]:
        params = {"monitor": monitor} if monitor is not None else {}
        return self.call("windows.query_profiles", params)

    def query_gamma_ramp(self, monitor: int | None = None) -> dict[str, Any]:
        params = {"monitor": monitor} if monitor is not None else {}
        return self.call("windows.query_gamma_ramp", params)

    def query_monitors(self) -> dict[str, Any]:
        r"""Enumerate DesktopLUT monitors for deterministic display mapping.

        Returns ``{"available", "count", "monitors": [...]}`` where each monitor
        carries enough identity (``device_name`` = ``\\.\DISPLAYn``, ``rect``,
        ``primary``, ``hardware_id``, ``hdr_capable``/``hdr_active``/``color_space``)
        to pair a DesktopLUT monitor index with an Argyll DISPLAY and the panel.
        """
        return self.call("windows.query_monitors")

    # -- display mode (SDR <-> HDR) ---------------------------------------
    def set_hdr(self, monitor: int, enable: bool) -> dict[str, Any]:
        """Switch a monitor's OS advanced-color (HDR) state on or off.

        The same flip DesktopLUT's HDR-toggle hotkey performs, but targeted at an
        explicit monitor + explicit desired state so DLC can drive SDR/HDR
        characterize/calibrate modes without the operator touching Windows
        Settings. Idempotent (a no-op when already in the requested state).
        Returns ``{monitor, hdr_capable, was_active, now_active, changed}``.
        """
        return self.call("windows.set_hdr", {"monitor": int(monitor), "enable": bool(enable)})

    def toggle_hdr(self, monitor: int) -> dict[str, Any]:
        """Flip a monitor between SDR and HDR (omit the target to invert current)."""
        return self.call("windows.set_hdr", {"monitor": int(monitor)})

    # -- calibration mode / clearing --------------------------------------
    def enter_neutral(
        self,
        monitor: int,
        mode: str,
        dummy_icc_path: str,
        reason: str = "DLC calibration run",
    ) -> dict[str, Any]:
        return self.call(
            "calibration.enter",
            {
                "monitor": monitor,
                "mode": normalize_mode(mode),
                "dummy_icc_path": dummy_icc_path,
                "reason": reason,
            },
        )

    def calibration_status(self) -> dict[str, Any]:
        return self.call("calibration.status")

    def exit_calibration(self, restore_snapshot: bool = False) -> dict[str, Any]:
        return self.call("calibration.exit", {"restore_snapshot": restore_snapshot})

    def disable_all(self) -> dict[str, Any]:
        return self.call("corrections.disable_all")

    # -- viewing layers (GUI toggles a run must measure WITHOUT) ----------------
    LAYER_NAMES = ("tonemap", "desktop_gamma", "white_balance", "grayscale")

    def set_layers(self, monitor: int, mode: str, **layers: bool) -> dict[str, Any]:
        """Toggle the viewing layers of ``monitor:mode`` exactly as the GUI checkboxes do
        (``layers.set``): the MHC's white balance / correction grayscale / Desktop Gamma
        permutation bits and the HDR tonemap shader flag. Omitted layers are kept. Returns
        ``{monitor_mode, before, after, regenerated, profile_name}`` — the MHC profile is
        re-baked (new name) when an MHC-layer bit changed."""
        params: dict[str, Any] = {"monitor": int(monitor), "mode": normalize_mode(mode)}
        for name, value in layers.items():
            if name not in self.LAYER_NAMES:
                raise ValueError(f"unknown viewing layer {name!r} (have {self.LAYER_NAMES})")
            params[name] = bool(value)
        return self.call("layers.set", params)

    @classmethod
    def layers_from_state(cls, state: dict[str, Any], monitor: int, mode: str) -> Optional[dict[str, bool]]:
        """The four viewing-layer flags for ``monitor:mode`` from a ``state()`` reply, or
        ``None`` on a server that predates ``layers`` (then the ini is the only evidence)."""
        entry = (state.get("layers") or {}).get(f"{int(monitor)}:{normalize_mode(mode)}")
        if not isinstance(entry, dict):
            return None
        return {name: bool(entry.get(name, False)) for name in cls.LAYER_NAMES}

    # -- viewing layers (GUI toggles a run must measure WITHOUT) ----------------
    LAYER_NAMES = ("tonemap", "desktop_gamma", "white_balance", "grayscale")

    def set_layers(self, monitor: int, mode: str, **layers: bool) -> dict[str, Any]:
        """Toggle the viewing layers of ``monitor:mode`` exactly as the GUI checkboxes do
        (``layers.set``): the MHC's white balance / correction grayscale / Desktop Gamma
        permutation bits and the HDR tonemap shader flag. Omitted layers are kept. Returns
        ``{monitor_mode, before, after, regenerated, profile_name}`` — the MHC profile is
        re-baked (new name) when an MHC-layer bit changed."""
        params: dict[str, Any] = {"monitor": int(monitor), "mode": normalize_mode(mode)}
        for name, value in layers.items():
            if name not in self.LAYER_NAMES:
                raise ValueError(f"unknown viewing layer {name!r} (have {self.LAYER_NAMES})")
            params[name] = bool(value)
        return self.call("layers.set", params)

    @classmethod
    def layers_from_state(cls, state: dict[str, Any], monitor: int, mode: str) -> Optional[dict[str, bool]]:
        """The four viewing-layer flags for ``monitor:mode`` from a ``state()`` reply, or
        ``None`` on a server that predates ``layers`` (then the ini is the only evidence)."""
        entry = (state.get("layers") or {}).get(f"{int(monitor)}:{normalize_mode(mode)}")
        if not isinstance(entry, dict):
            return None
        return {name: bool(entry.get(name, False)) for name in cls.LAYER_NAMES}

    # -- viewing layers (GUI toggles a run must measure WITHOUT) ----------------
    LAYER_NAMES = ("tonemap", "desktop_gamma", "white_balance", "grayscale")

    def set_layers(self, monitor: int, mode: str, **layers: bool) -> dict[str, Any]:
        """Toggle the viewing layers of ``monitor:mode`` exactly as the GUI checkboxes do
        (``layers.set``): the MHC's white balance / correction grayscale / Desktop Gamma
        permutation bits and the HDR tonemap shader flag. Omitted layers are kept. Returns
        ``{monitor_mode, before, after, regenerated, profile_name}`` — the MHC profile is
        re-baked (new name) when an MHC-layer bit changed."""
        params: dict[str, Any] = {"monitor": int(monitor), "mode": normalize_mode(mode)}
        for name, value in layers.items():
            if name not in self.LAYER_NAMES:
                raise ValueError(f"unknown viewing layer {name!r} (have {self.LAYER_NAMES})")
            params[name] = bool(value)
        return self.call("layers.set", params)

    @classmethod
    def layers_from_state(cls, state: dict[str, Any], monitor: int, mode: str) -> Optional[dict[str, bool]]:
        """The four viewing-layer flags for ``monitor:mode`` from a ``state()`` reply, or
        ``None`` on a server that predates ``layers`` (then the ini is the only evidence)."""
        entry = (state.get("layers") or {}).get(f"{int(monitor)}:{normalize_mode(mode)}")
        if not isinstance(entry, dict):
            return None
        return {name: bool(entry.get(name, False)) for name in cls.LAYER_NAMES}

    # -- MHC staging + apply ----------------------------------------------
    def set_primaries(self, monitor: int, mode: str, primaries: dict[str, float]) -> dict[str, Any]:
        return self.call(
            "mhc.set_primaries",
            {"monitor": monitor, "mode": normalize_mode(mode), "primaries": dict(primaries)},
        )

    def set_white(self, monitor: int, mode: str, x: float, y: float) -> dict[str, Any]:
        return self.call(
            "mhc.set_white",
            {"monitor": monitor, "mode": normalize_mode(mode), "x": float(x), "y": float(y)},
        )

    def set_base_grayscale(
        self,
        monitor: int,
        mode: str,
        point_count: int,
        points: list[float],
        deviations: dict[str, list[float]],
        gamma: float = 2.2,
    ) -> dict[str, Any]:
        mode = normalize_mode(mode)
        points, deviations = self._bridge_grayscale(mode, points, deviations, gamma)
        point_count = len(points)
        return self.call(
            "mhc.set_base_grayscale",
            {
                "monitor": monitor,
                "mode": mode,
                "point_count": int(point_count),
                "points": [float(p) for p in points],
                "deviations": _coerce_deviations(deviations),
            },
        )

    def set_base_lut(self, monitor: int, mode: str, cube_path: str,
                     peak_nits: float = 0.0) -> dict[str, Any]:
        """Import a full-resolution per-channel 1D ``.cube`` as the MHC base EOTF correction.

        The path ColourSpace/DisplayCal use, reached over the pipe (``mhc.set_base_lut`` ->
        ``sourceIs1DCube`` -> the 4096-entry HDR MHC2 LUT). Replaces the coarse 32-point
        ``set_base_grayscale`` for the HDR base; the matrix (``set_primaries``/``set_white``)
        still owns primaries + white. ``peak_nits`` feeds the HDR MHC2 luminance metadata."""
        params: dict[str, Any] = {
            "monitor": monitor, "mode": normalize_mode(mode), "cube_path": str(cube_path),
        }
        if peak_nits and peak_nits > 0:
            params["peak_nits"] = float(peak_nits)
        return self.call("mhc.set_base_lut", params)

    def set_correction_grayscale(
        self,
        monitor: int,
        mode: str,
        point_count: int,
        points: list[float],
        deviations: dict[str, list[float]],
        gamma: float = 2.2,
    ) -> dict[str, Any]:
        mode = normalize_mode(mode)
        points, deviations = self._bridge_grayscale(mode, points, deviations, gamma)
        point_count = len(points)
        return self.call(
            "mhc.set_correction_grayscale",
            {
                "monitor": monitor,
                "mode": mode,
                "point_count": int(point_count),
                "points": [float(p) for p in points],
                "deviations": _coerce_deviations(deviations),
            },
        )

    @staticmethod
    def _bridge_grayscale(mode, points, deviations, gamma):
        """Translate the DLC's signal-domain grayscale into DesktopLUT's MHC2
        convention. SDR needs sqrt-distributed linear-light points + linear-light
        deviations (see mhc_grayscale); HDR (PQ) already matches and is passed
        through unchanged."""
        if mode != "SDR":
            return list(points), deviations
        new_points, new_dev = to_desktoplut_sdr_grayscale(points, deviations, gamma=gamma)
        return new_points, new_dev

    # -- MHC correction-grayscale LIVE-EDIT (the toggleable third "+1" touch-up) -----------
    # Drives DesktopLUT's main-GUI grayscale editor over the pipe (CODEX_GRAYSCALE_LIVE_EDIT_PROMPT.md):
    # ``live_begin`` engages the preview shader (correction GS stacked on top of MHC+3D-LUT so the meter
    # sees it — render.cpp:346 corrGsPreviewActive gate), ``set_live`` nudges the 32-point editor table
    # live, ``commit`` bakes it into the ICM (the editor's "OK"), ``cancel`` reverts. Same store as
    # ``set_correction_grayscale`` (``MHCSettings::correctionGrayscale``); the core (matrix + base
    # grayscale + 3D LUT) is never touched, and the result is one-toggle revertible to the vanilla ICM.
    def grayscale_live_begin(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("mhc.grayscale_live_begin",
                         {"monitor": monitor, "mode": normalize_mode(mode)})

    def grayscale_set_live(self, monitor: int, mode: str, point_count: int,
                           points: list[float], deviations: dict[str, list[float]],
                           gamma: float = 2.2,
                           *,
                           luminance: list[float] | None = None,
                           rgb: dict[str, list[float]] | None = None) -> dict[str, Any]:
        """Nudge the live editor table. When the solver's DECOMPOSED sliders are given
        (``luminance`` = the common/main slider per point, ``rgb`` = the per-channel
        balance strips), they ride the wire alongside the composed ``deviations``
        (back-compat: deviations == luminance*rgb per point), so the DesktopLUT editor
        shows the same luminance/balance split the solver produced instead of a zero
        main slider with the common mode pushed into all three RGB values."""
        mode = normalize_mode(mode)
        gs: dict[str, Any]
        if luminance is not None and rgb is not None:
            if mode == "SDR":
                points, luminance, rgb = to_desktoplut_sdr_grayscale_decomposed(
                    points, luminance, rgb)
            lum = [float(v) for v in luminance]
            bal = _coerce_deviations(rgb)
            gs = {
                "point_count": int(len(points)),
                "points": [float(p) for p in points],
                "luminance": lum,
                "rgb": bal,
                # Composed from the (bridged) decomposition so the wire invariant
                # deviations == luminance*rgb holds exactly for legacy consumers.
                "deviations": {ch: [lum[i] * bal[ch][i] for i in range(len(lum))]
                               for ch in ("r", "g", "b")},
            }
        else:
            points, deviations = self._bridge_grayscale(mode, points, deviations, gamma)
            gs = {
                "point_count": int(len(points)),
                "points": [float(p) for p in points],
                "deviations": _coerce_deviations(deviations),
            }
        return self.call("mhc.grayscale_set_live", {
            "monitor": monitor, "mode": mode, "grayscale": gs,
        })

    def grayscale_commit(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("mhc.grayscale_commit",
                         {"monitor": monitor, "mode": normalize_mode(mode)})

    def grayscale_cancel(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("mhc.grayscale_cancel",
                         {"monitor": monitor, "mode": normalize_mode(mode)})

    def apply_mhc(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("mhc.apply", {"monitor": monitor, "mode": normalize_mode(mode)})

    def remove_mhc(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("mhc.remove", {"monitor": monitor, "mode": normalize_mode(mode)})

    def verify_mhc(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("maintenance.verify_mhc", {"monitor": monitor, "mode": normalize_mode(mode)})

    # -- runtime 3D LUT ----------------------------------------------------
    def set_3dlut(self, monitor: int, mode: str, cube_path: str) -> dict[str, Any]:
        return self.call(
            "runtime.set_3dlut",
            {"monitor": monitor, "mode": normalize_mode(mode), "cube_path": str(cube_path)},
        )

    def clear_3dlut(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call(
            "runtime.clear_3dlut",
            {"monitor": monitor, "mode": normalize_mode(mode)},
        )

    # -- DWM-hook LUT routing (twin-panel order-match) -------------------------------
    # The hook DLL cannot read a monitor position from the DWM overlay context on 25H2, so
    # two identical panels are matched by FIRST-PRESENT ORDER — a coin toss that used to
    # re-roll on every set_3dlut/clear_3dlut (each one re-injects). On 2026-09-03 a whole
    # 3dlut-only run measured an uncorrected panel because the cube rendered on the twin.
    # The host now keeps the assignment sticky per DWM session, exposes it as
    # state()["hook"], and lets a client SWAP/confirm it; dlc.hook_routing proves the
    # assignment through the meter.
    def hook_state(self) -> Optional[dict[str, Any]]:
        """``state()["hook"]`` — ``{active, needs_check, routing?}`` — or ``None`` on a
        DesktopLUT build that predates hook-routing reporting (then the assignment is
        unknown and a cube flow must self-check)."""
        hook = self.state().get("hook")
        return hook if isinstance(hook, dict) else None

    def set_hook_routing(self, action: str, monitor: int | None = None,
                         entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """``hook.set_routing``: ``swap`` (the calibrated ``monitor`` trades places with its
        single same-size/same-bpc twin; re-injects), ``confirm`` (mark the assignment
        meter-verified; no re-inject), ``clear`` (fresh roll; re-injects) or ``assign``
        (explicit ``entries`` [{ctx,left,top}]; re-injects). Returns ``{hook, reinjected}``."""
        params: dict[str, Any] = {"action": str(action)}
        if monitor is not None:
            params["monitor"] = int(monitor)
        if entries is not None:
            params["entries"] = [dict(e) for e in entries]
        return self.call("hook.set_routing", params)

    # -- runtime OVERLAY grayscale tweak (the Corrections-tab / DWM-hook shader layer) ----
    # IMPORTANT (../docs/NAMING.md §2): this drives ``ColorCorrectionData::grayscale`` — the
    # OVERLAY/Corrections-tab grayscale, a DIFFERENT layer from the MHC ``correctionGrayscale``
    # that DLC's closed-loop D65 refine owns. The orchestrator no longer drives ``set_`` here:
    # the old "GS+WB" flow (removed 2026-06-24, when the MHC matrix + grayscale refine took sole
    # ownership of the neutral axis) was its only caller. Kept for pipe-API completeness and the
    # shader-fast-refine design direction. ``disable_`` IS still used — lut3d.py clears this
    # overlay layer before a standalone 3D-LUT build.
    def set_grayscale_tweak(
        self,
        monitor: int,
        mode: str,
        point_count: int,
        points: list[float],
        deviations: dict[str, list[float]],
        *,
        luminance: list[float] | None = None,
        rgb: dict[str, list[float]] | None = None,
    ) -> dict[str, Any]:
        """Set the runtime OVERLAY grayscale tweak (the Corrections-tab shader layer, NOT the
        MHC grayscale — see ../docs/NAMING.md §2). Applied live without an ICC re-bake.
        Per-channel deviations carry both grayscale tracking and white balance.

        Not driven by the current orchestrator (the removed GS+WB flow was its only caller);
        retained as pipe-API surface for the shader-fast-refine direction.
        """
        return self.call(
            "runtime.set_grayscale_tweak",
            {
                "monitor": monitor,
                "mode": normalize_mode(mode),
                "grayscale_tweak": {
                    "point_count": int(point_count),
                    "points": [float(p) for p in points],
                    **({"luminance": [float(v) for v in luminance]} if luminance is not None else {}),
                    **({"rgb": _coerce_deviations(rgb)} if rgb is not None else {}),
                    "deviations": _coerce_deviations(deviations),
                },
            },
        )

    def disable_grayscale_tweak(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call(
            "runtime.disable_grayscale_tweak",
            {"monitor": monitor, "mode": normalize_mode(mode)},
        )


def _coerce_deviations(deviations: dict[str, list[float]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for key in ("r", "g", "b"):
        col = deviations.get(key, []) if isinstance(deviations, dict) else []
        out[key] = [float(x) for x in col]
    return out
