"""Machine-readable DesktopLUT API contract for parent-app implementation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktoplut_client import CONTRACT_VERSION, DEFAULT_PIPE_NAME


@dataclass(frozen=True)
class ApiParamSpec:
    type: str
    required: bool = True
    description: str = ""
    values: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.values is None:
            payload.pop("values")
        return payload


@dataclass(frozen=True)
class ApiMethodSpec:
    method: str
    purpose: str
    params: dict[str, ApiParamSpec]
    result: dict[str, str]
    mutates_state: bool
    gui_thread_required: bool
    # Contract disposition (fable Phase 9): "active" = driven by the current DLC;
    # "legacy" = retained pipe surface with no current DLC caller (kept for
    # completeness / a documented future direction — do not remove server-side
    # without checking this spec's description).
    status: str = "active"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["params"] = {name: spec.as_dict() for name, spec in self.params.items()}
        return payload


def _mode_param() -> ApiParamSpec:
    return ApiParamSpec("string", description="Target DesktopLUT mode.", values=["SDR", "HDR"])


def _monitor_param() -> ApiParamSpec:
    return ApiParamSpec("integer", description="Zero-based DesktopLUT monitor index.")


def build_desktoplut_api_spec() -> dict[str, Any]:
    """Return the API contract DLC expects the DesktopLUT pipe server to expose."""

    methods = [
        ApiMethodSpec(
            "state.get",
            "Return current DesktopLUT automation state.",
            {},
            {
                "running": "boolean",
                "corrections_enabled": "boolean (the OVERLAY-draw flag, NOT 'a correction is live' — "
                                       "false in DWM-hook mode even with a cube loaded; see ../docs/NAMING.md S4)",
                "calibration_mode": "object or null",
                "mhc": "object keyed by '<monitor>:<MODE>'; each entry {applied:bool, profile_name:string}. "
                       "DLC ALSO wants correction_grayscale {point_count,points,deviations} exposed here "
                       "(the Design-B grayscale-wb revert snapshot source) — a DesktopLUT-side ticket; "
                       "until then the snapshot degrades to clear-to-identity on hardware (fable Phase 9)",
                "runtime": "object keyed by '<monitor>:<MODE>'; each entry {cube_path:string}",
                "layers": "object keyed by '<monitor>:<MODE>' for EVERY pair: the viewing layers a run "
                          "must measure WITHOUT — {white_balance, grayscale, desktop_gamma, tonemap: bool"
                          "; HDR adds tonemap_dynamic, tonemap_target_peak}. mhc entries also carry "
                          "source_file (the DLC base 1D .cube the profile was generated from — the "
                          "identity that survives WB/DG/GS permutation re-bakes) and active_perm. "
                          "Absent on pre-2026-09-03 builds (then the ini is the only layer evidence).",
                "contract_version": "integer (optional): the wire-contract version the server speaks. "
                                    "Absent = pre-versioning build = 1. DLC checks this at preflight so a "
                                    "mismatch surfaces as 'update DLC/DesktopLUT', not 'unknown method' "
                                    "mid-run. Server-side field is a DesktopLUT ticket (fable Phase 9).",
                "hook": "object {active:bool (DWM hook DLL injected), needs_check:bool (an entry is "
                        "order/pinned-matched and unconfirmed, or the routing session is stale), "
                        "routing?: {session:'<pid>-<createtime>' (the dwm.exe identity; changes on a "
                        "DWM restart), stale:bool, confirmed:bool (a client confirmed via "
                        "hook.set_routing confirm; the DLL resets it whenever it order-matches a NEW "
                        "context), entries:[{ctx:str, left:int, top:int, method:'unique'|'bpc'|'scan'|"
                        "'pinned'|'order'|'legacy', monitor:int|null}]}}. routing is ABSENT until the "
                        "hook has assigned a twin (unknown). The DLL cannot read a monitor position from "
                        "the DWM overlay context on 25H2, so identical panels are matched by first-present "
                        "ORDER — a coin toss the 2026-09-03 3dlut-only run lost (cube on the twin); the "
                        "assignment is now sticky per DWM session and dlc.hook_routing proves it through "
                        "the meter. Absent on older builds (treat as unknown: cube flows self-check).",
            },
            mutates_state=False,
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "hook.set_routing",
            "Manage the DWM hook's sticky context->monitor LUT routing (state.get 'hook'). "
            "'swap': the calibrated monitor trades positions with its single same-size/same-bpc "
            "twin (error unless exactly one twin) — rewrites the routing file and re-injects, the "
            "DLL then honours the PINNED assignment; 'confirm': mark the assignment meter-verified "
            "(confirmed=true, no re-inject); 'clear': delete the file and re-inject (fresh roll); "
            "'assign': pin explicit entries and re-inject. DLC's hardware-readiness self-check "
            "installs a magenta probe cube, proves through the meter that it changes the calibrated "
            "panel, swaps ONCE if it does not, and refuses the run if it still does not.",
            {
                "action": ApiParamSpec("string", description="Routing operation.",
                                       values=["swap", "confirm", "clear", "assign"]),
                "monitor": ApiParamSpec("integer", required=False,
                                        description="swap only: the calibrated DesktopLUT monitor index."),
                "entries": ApiParamSpec("array", required=False,
                                        description="assign only: [{ctx:str, left:int, top:int}] — the "
                                                    "DWM overlay context handle and the desktop origin it "
                                                    "must paint."),
            },
            {"hook": "object (the same object state.get reports under 'hook', after the change)",
             "reinjected": "boolean (swap/assign/clear re-inject the DLL; confirm does not)"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "corrections.disable_all",
            "Disable all runtime correction layers for a clean measurement baseline.",
            {},
            {"corrections_enabled": "boolean false"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "layers.set",
            "Toggle the viewing layers of one monitor:mode exactly as the GUI checkboxes do: the "
            "MHC's white balance / correction grayscale / Desktop Gamma permutation bits (an MHC "
            "re-bake under a new profile name) and the HDR tonemap shader flag. Omitted layers are "
            "kept. DLC captures the user's layers before a run, measures with them OFF, and "
            "restores them at the run's terminal end — the user never manages corrections around "
            "a pipeline run (plan item 0b, 2026-09-03).",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "white_balance": ApiParamSpec("boolean", required=False, description="MHC white balance bit (optional)"),
                "grayscale": ApiParamSpec("boolean", required=False, description="MHC correction-grayscale bit (optional)"),
                "desktop_gamma": ApiParamSpec("boolean", required=False, description="Desktop Gamma bit, HDR only (optional)"),
                "tonemap": ApiParamSpec("boolean", required=False, description="HDR tonemap shader flag, HDR only (optional)"),
            },
            {"monitor_mode": "string", "before": "object {white_balance,grayscale,desktop_gamma,tonemap}",
             "after": "object (same shape)", "regenerated": "boolean (MHC profile re-baked)",
             "profile_name": "string (the profile now associated)"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "calibration.enter",
            "Enter calibration mode: snapshot the monitor's settings and reset the CALIBRATED "
            "mode's correction layers (MHC removed+disabled, runtime 3D LUT and shader layers "
            "cleared). Other mode:monitor pairs are PRESERVED — builds before 2026-08-14 cleared "
            "both modes of the monitor, permanently dropping the non-calibrated mode's runtime "
            "cube on the apply path (exit without restore); the orchestrator's commit re-applies "
            "dropped pairs as a guard for those builds. The dummy ICC path is RECORDED but not "
            "associated (deferred; neutrality comes from the cleared layers plus DLC's own "
            "dispwin -c). NOT retry-safe: re-entering while a session is active re-snapshots the "
            "already-cleared state (see transport.timeout_and_retries).",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "dummy_icc_path": ApiParamSpec("string", description="Absolute path to the neutral/dummy ICC DLC wants associated."),
                "reason": ApiParamSpec("string", required=False, description="Human-readable reason recorded by DesktopLUT."),
            },
            {
                "active": "boolean true",
                "snapshot_id": "string",
                "monitor": "integer",
                "mode": "string",
                "dummy_icc_path": "string",
                "corrections_reset": "boolean true",
            },
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "calibration.status",
            "Return current calibration-mode bookkeeping.",
            {},
            {"active": "boolean", "state": "object or null"},
            mutates_state=False,
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "calibration.exit",
            "Exit calibration mode, optionally restoring the calibration snapshot.",
            {"restore_snapshot": ApiParamSpec("boolean", required=False, description="Restore the snapshot captured by calibration.enter.")},
            {"active": "boolean false", "restored": "boolean"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_primaries",
            "Set measured/native MHC primaries for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "primaries": ApiParamSpec("object", description="Chromaticity object with rx, ry, gx, gy, bx, by."),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_white",
            "Set MHC white point target or measured white for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "x": ApiParamSpec("number", description="CIE x chromaticity."),
                "y": ApiParamSpec("number", description="CIE y chromaticity."),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_base_grayscale",
            "Set the MHC base grayscale (per-channel 1D tone correction) for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "point_count": ApiParamSpec("integer", description="Number of grayscale control points."),
                "points": ApiParamSpec("array", description="Ascending input levels in [0,1]. Server "
                                       "clamps to 32 points (index-resampled above that); DLC always sends <=32."),
                "deviations": ApiParamSpec(
                    "object", description="Per-channel multiplicative deviations centered at 1.0: {r:[],g:[],b:[]}."
                ),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_base_lut",
            "Import a full-resolution per-channel 1D .cube as the MHC base EOTF correction "
            "(the ColourSpace/DisplayCal path). DesktopLUT bakes it into the 4096-entry HDR "
            "(1024 SDR) MHC2 LUT; the matrix (set_primaries/set_white) still owns primaries + "
            "white. Used for the HDR base where the 32-point table is too sparse for a PQ EOTF.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "cube_path": ApiParamSpec("string", description="Absolute path to a 1D Iridas .cube (LUT_1D_SIZE)."),
                "peak_nits": ApiParamSpec(
                    "number", required=False, description="HDR peak luminance metadata (MaxCLL), nits."
                ),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_correction_grayscale",
            "Set the MHC correction grayscale (the refinement layer composed on top of the base) "
            "for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "point_count": ApiParamSpec("integer", description="Number of grayscale control points."),
                "points": ApiParamSpec("array", description="Ascending input levels in [0,1]. Server "
                                       "clamps to 32 points (index-resampled above that); DLC always sends <=32."),
                "deviations": ApiParamSpec(
                    "object", description="Per-channel multiplicative deviations centered at 1.0: {r:[],g:[],b:[]}."
                ),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.apply",
            "Apply the staged MHC settings to DesktopLUT.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "mhc": "object with applied=true"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.remove",
            "Remove active MHC settings for the target monitor/mode.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "removed": "boolean true"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "maintenance.verify_mhc",
            "Report whether DesktopLUT has a coherent applied MHC state (enabled AND a baked "
            "profile_name — staged-but-unapplied params do not verify).",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"verified": "boolean"},
            mutates_state=False,
            # Served on the pipe thread (Dispatch handles it before the GUI marshal;
            # the settings mutex makes the read safe off-thread) — fable Phase 9 fixed
            # this flag, which wrongly claimed a GUI-thread dependency.
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "runtime.set_3dlut",
            "Load the runtime 3D LUT cube for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "cube_path": ApiParamSpec("string", description="Absolute or run-relative path to a 3D cube file."),
            },
            {"monitor_mode": "string", "runtime": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.clear_3dlut",
            "Clear the runtime 3D LUT for the target monitor/mode.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "runtime": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.set_grayscale_tweak",
            "Set the runtime OVERLAY grayscale tweak — the Corrections-tab / DWM-hook shader "
            "layer (ColorCorrectionData::grayscale; see ../docs/NAMING.md S2), DISTINCT from the "
            "MHC correctionGrayscale that DLC's D65 refine owns. Applied live without an ICC "
            "re-bake. Not driven by the current orchestrator (its only caller was the removed "
            "GS+WB flow); kept as pipe-API surface for the shader-fast-refine direction.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "grayscale_tweak": ApiParamSpec(
                    "object",
                    description=(
                        "Grayscale payload {point_count:int, points:[ascending [0,1]], "
                        "luminance:[] optional common slider, rgb:{r:[],g:[],b:[]} optional "
                        "balance sliders, deviations:{r:[],g:[],b:[]} composed multiplicative "
                        "values centered at 1.0}. The composed deviations carry both grayscale "
                        "tracking (their shape) and white balance (their DC component)."
                    ),
                ),
            },
            {"monitor_mode": "string", "runtime": "object with grayscale_tweak=true"},
            mutates_state=True,
            gui_thread_required=True,
            status="legacy",
        ),
        ApiMethodSpec(
            "runtime.disable_grayscale_tweak",
            "Disable the runtime OVERLAY grayscale tweak layer (the Corrections-tab shader "
            "layer; see ../docs/NAMING.md S2). Used by lut3d to clear it before a 3D-LUT build.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "runtime": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.grayscale_live_begin",
            "Engage the correction-grayscale LIVE-EDIT preview (the GUI editor's 'Edit Points' "
            "over the pipe): the correction GS stacks on top of MHC+3D-LUT so the meter sees it. "
            "Snapshots the pre-begin correctionGrayscale for cancel/abort restore. Errors if a "
            "session (or the GUI editor) is already active for this monitor/mode.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "preview": "boolean true"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.grayscale_set_live",
            "Nudge the live-edit correction grayscale (per-patch, no ICC re-bake); the next frame "
            "reflects it. Errors without an active grayscale_live_begin session.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "grayscale": ApiParamSpec(
                    "object",
                    description="Grayscale payload {point_count:int, points:[ascending [0,1]], "
                                "luminance:[] optional common/main-slider values, rgb:{r:[],g:[],b:[]} "
                                "optional per-channel balance values, deviations:{r:[],g:[],b:[]} "
                                "composed multiplicative centered at 1.0 (back-compat; == "
                                "luminance*rgb when the decomposition is sent)}. When luminance/rgb "
                                "are present they are authoritative: luminance scales the points "
                                "curve (the editor's main slider) and rgb lands on the RGB balance "
                                "values, so the editor shows the solver's split. Server clamps to "
                                "32 points (index-resampled above that).",
                ),
            },
            {"monitor_mode": "string"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.grayscale_commit",
            "The live editor's 'OK': bake the previewed correctionGrayscale into the ICM and tear "
            "down the preview. baked:false when no live session existed (e.g. DesktopLUT restarted "
            "mid-run) so the caller can detect a lost bake. A later cancel is a tolerated no-op.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "baked": "boolean"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.grayscale_cancel",
            "Abort the live-edit preview without baking: restore the PRE-BEGIN correctionGrayscale "
            "(the user's prior correction, not bare identity) and regenerate the vanilla core ICM. "
            "canceled:false (no-op) when no live session exists, including after a commit.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "canceled": "boolean"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "windows.set_hdr",
            "Switch a monitor's OS advanced-color (HDR) state — the HDR-toggle hotkey's flip, "
            "targeted at an explicit monitor + desired state so DLC can drive SDR/HDR "
            "characterize/calibrate modes. Idempotent (no-op when already in the requested "
            "state); omit 'enable' to toggle. Errors when enabling on a non-HDR-capable monitor.",
            {
                "monitor": _monitor_param(),
                "enable": ApiParamSpec("boolean", required=False,
                                       description="Desired HDR state; absent means toggle. Accepts bool or 0/1."),
            },
            {
                "monitor": "integer",
                "hdr_capable": "boolean",
                "was_active": "boolean",
                "now_active": "boolean (re-read after the flip — authoritative, not intent)",
                "changed": "boolean",
            },
            mutates_state=True,
            # Thread-agnostic DisplayConfig calls; runs off the GUI thread in the C++.
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "windows.query_profiles",
            "Return active Windows ICC/profile association data for the target monitor.",
            {"monitor": ApiParamSpec("integer", required=False, description="Zero-based DesktopLUT monitor index.")},
            {"available": "boolean", "profiles": "array", "active_profile": "string or null"},
            mutates_state=False,
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "windows.query_gamma_ramp",
            "Return Windows gamma ramp/VCGT state for the target monitor.",
            {"monitor": ApiParamSpec("integer", required=False, description="Zero-based DesktopLUT monitor index.")},
            {"available": "boolean", "gamma_ramp_loaded": "boolean or null", "vcgt_present": "boolean or null"},
            mutates_state=False,
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "windows.query_monitors",
            "Enumerate DesktopLUT monitors with enough identity for DLC to deterministically "
            "map a monitor index to an Argyll DISPLAY and the physical panel.",
            {},
            {
                "available": "boolean",
                "count": "integer",
                "monitors": (
                    "array of {index:int, device_name:'\\\\.\\DISPLAYn' (Argyll order), friendly_name:string, "
                    "rect:{x,y,width,height}, primary:boolean, device_path:string, hardware_id:string (EDID), "
                    "source_id:int, target_id:int, adapter_id:{low,high}, hdr_capable:boolean, hdr_active:boolean, "
                    "color_space:'SDR'|'ACM_SDR'|'HDR'}"
                ),
            },
            mutates_state=False,
            gui_thread_required=False,
        ),
    ]

    # The per-phase acceptance sequence DLC runs against the live pipe (and the
    # mock). It exercises the 32-point base-grayscale FALLBACK staging path; the
    # current orchestrator prefers mhc.set_base_lut (a dense DLC-owned 1D .cube)
    # and falls back to set_base_grayscale only when no cube was built. Executing
    # the sequence verbatim requires real cube files for the path-validated
    # methods (set_base_lut / set_3dlut check existence server-side AND in the mock).
    _m, _mode = 0, "SDR"
    _grayscale = {"point_count": 2, "points": [0.0, 1.0], "deviations": {"r": [1.0, 1.0], "g": [1.0, 1.0], "b": [1.0, 1.0]}}
    sequence_steps = [
        ("initial_state", "state.get", {}),
        (
            "enter",
            "calibration.enter",
            {"monitor": _m, "mode": _mode, "dummy_icc_path": r"<DLC>\third_party\argyll\3.3.0\ref\sRGB.icm", "reason": "DLC contract check"},
        ),
        ("primaries", "mhc.set_primaries", {"monitor": _m, "mode": _mode, "primaries": {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}}),
        ("white", "mhc.set_white", {"monitor": _m, "mode": _mode, "x": 0.3127, "y": 0.3290}),
        ("base_grayscale", "mhc.set_base_grayscale", {"monitor": _m, "mode": _mode, **_grayscale}),
        ("apply_mhc", "mhc.apply", {"monitor": _m, "mode": _mode}),
        ("verify_mhc", "maintenance.verify_mhc", {"monitor": _m, "mode": _mode}),
        ("runtime_3dlut", "runtime.set_3dlut", {"monitor": _m, "mode": _mode, "cube_path": r"RUN\generated\final.cube"}),
        ("final_state", "state.get", {}),
    ]
    sequence = [{"step": step, "request": {"method": method, "params": params}} for step, method, params in sequence_steps]

    return {
        "name": "DesktopLUT Calibrator API",
        "version": CONTRACT_VERSION,
        "transport": {
            "default_pipe": DEFAULT_PIPE_NAME,
            "framing": "one UTF-8 JSON object per line; one request per named-pipe connection",
            "request_envelope": {"method": "string", "params": "object"},
            "response_envelope": {"ok": "boolean", "result": "object when ok", "error": "string when not ok"},
            "versioning": (
                "state.get result SHOULD carry contract_version (integer; absent = 1, i.e. a "
                "pre-versioning build). The client checks it at preflight (desktoplut_client."
                "contract_version_mismatch) so a mismatch reads 'update DLC/DesktopLUT' instead "
                "of 'unknown method' mid-run. Additive fields never bump the version."
            ),
            "timeout_and_retries": (
                "The pipe is SINGLE-INSTANCE and the client timeout (default 75s) exceeds the "
                "server's GUI marshal timeout (60s), so a client-side timeout usually means the "
                "GUI thread is wedged mid-mutation. The timed-out request may still be APPLIED "
                "server-side, and a retry fails pipe-busy until the orphaned connection drains. "
                "Retry-safety: every mhc.set_*/mhc.apply/runtime.* call is idempotent (same "
                "params => same state); calibration.enter is NOT retry-safe — a re-enter "
                "overwrites the single C++ restore snapshot with the already-cleared state "
                "(DesktopLUT ticket, fable Phase 9), so DLC surfaces a stale active calibration "
                "mode before entering and treats the preflight settings backup as the "
                "authoritative restore; mhc.grayscale_commit retried after a real commit "
                "returns baked:false (detectable, surfaced as a seam)."
            ),
        },
        "threading": {
            "pipe_thread": "decode request and marshal GUI mutations",
            "gui_thread": "perform DesktopLUT state/settings mutations for gui_thread_required methods",
        },
        "methods": [method.as_dict() for method in methods],
        "contract_check_sequence": sequence,
        "final_state_checks": [
            "all commands return ok=true",
            "state.get reports running=true",
            "calibration_mode is active after calibration.enter",
            "corrections_enabled=false after calibration.enter",
            "mhc entry for 0:SDR has applied=true",
            "maintenance.verify_mhc reports verified=true",
            "runtime entry for 0:SDR has a cube_path",
        ],
    }


def write_desktoplut_api_spec(output: Path) -> dict[str, Any]:
    spec = build_desktoplut_api_spec()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec
