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
                "mhc": "object keyed by '<monitor>:<MODE>'; each entry {applied:bool, profile_name:string, "
                       "correction_grayscale:{enabled:bool, point_count:int, points:[float], "
                       "deviations:{r:[float],g:[float],b:[float]}}}. correction_grayscale is the "
                       "Design-B grayscale-wb revert snapshot source, in the SAME decomposition "
                       "mhc.set_correction_grayscale stores (points carry the luminance scale, deviations "
                       "the per-channel balance), so handing it straight back reproduces the curve. "
                       "EMPTY points = no correction; the field ABSENT = a build predating the change, "
                       "where a revert degrades to clear-to-identity (fable Phase 9 T3). enabled mirrors "
                       "layers[key].grayscale — the same C++ bool; toggle it with layers.set, not here",
                "runtime": "object keyed by '<monitor>:<MODE>'; each entry {cube_path:string}",
                "layers": "object keyed by '<monitor>:<MODE>' for EVERY pair: the viewing layers a run "
                          "must measure WITHOUT — {white_balance, grayscale, desktop_gamma, tonemap, fald: bool"
                          "; HDR adds tonemap_dynamic, tonemap_target_peak; every pair (HDR and SDR, the FALD "
                          "layer is per mode since 2026-09-14) carries fald_params_path, fald_debug_mode, "
                          "fald_ped_mode, fald_ped_colour_in_file, fald_boost_in_file (the panel file is FLD4 with a "
                          "black-frame LED boost LUT; absent on builds before 2026-09-18), fald_temporal_mode, fald_tau_rise_ms, "
                          "fald_tau_fall_ms, fald_delay_frames, fald_temporal_closure, fald_temporal_parity (runtime.fald_temporal; the last "
                          "two absent on builds before 2026-09-20), fald_starfield (bool) + fald_star_even, "
                          "fald_star_lift, fald_star_target_gain, fald_star_target_sigma, fald_star_keep_nits, fald_star_even_reach, fald_star_cap_nits, fald_star_strength, "
                          "fald_star_area_lo, fald_star_area_hi, fald_star_peak_hi, fald_star_reach, fald_star_nb_lo, "
                          "fald_star_nb_hi (runtime.fald_starfield; absent on builds before 2026-09-19), fald_glowfill (bool) + "
                          "fald_glow_strength, fald_glow_reach, fald_glow_cap_nits (runtime.fald_glowfill; absent on builds "
                          "before the S2 glow fill, 2026-09-20; SDR pairs add fald_glow_note = why the fill is HDR only), and "
                          "fald_file_transfer 'pq'|'gamma' when the "
                          "panel file is readable}. mhc entries also carry "
                          "source_file (the DLC base 1D .cube the profile was generated from — the "
                          "identity that survives WB/DG/GS permutation re-bakes) and active_perm. "
                          "Absent on pre-2026-09-03 builds (then the ini is the only layer evidence).",
                "contract_version": "integer (optional): the wire-contract version the server speaks. "
                                    "Absent = pre-versioning build = 1. DLC checks this at preflight so a "
                                    "mismatch surfaces as 'update DLC/DesktopLUT', not 'unknown method' "
                                    "mid-run. Server-side field is a DesktopLUT ticket (fable Phase 9).",
                "hook": "object {active:bool (DWM hook DLL injected), needs_check:bool (an entry is "
                        "order/pinned/replaced-matched and unconfirmed, or provisional, or the routing "
                        "session is stale), "
                        "routing?: {session:'<pid>-<createtime>' (the dwm.exe identity; changes on a "
                        "DWM restart), stale:bool, confirmed:bool (a client confirmed via "
                        "hook.set_routing confirm; the DLL resets it whenever it order-matches a NEW "
                        "context), entries:[{ctx:str, left:int, top:int, method:'unique'|'bpc'|'scan'|"
                        "'pinned'|'order'|'legacy'|'provisional'|'replaced'|'beacon', monitor:int|null}]}}. "
                        "beacon = identified positively by the host's identity beacon (a colour the DLL read "
                        "from the context's back-buffer corner) - unambiguous, runs after every injection; "
                        "hook.set_routing {action:'identify'} runs one on demand. "
                        "provisional = a twin context that arrived while both positions were held (DWM "
                        "recreated one): the DLL's replacement guess, settled by liveness into "
                        "replaced (pinned; confirmed cleared). routing is ABSENT until the "
                        "hook has assigned a twin (unknown). The DLL cannot read a monitor position from "
                        "the DWM overlay context on 25H2, so identical panels are matched by first-present "
                        "ORDER — a coin toss the 2026-09-03 3dlut-only run lost (cube on the twin); the "
                        "assignment is now sticky per DWM session and dlc.hook_routing proves it through "
                        "the meter. Absent on older builds (treat as unknown: cube flows self-check).",
                "overlay": "object {awake:bool (the overlay render path is running and not auto-asleep), "
                           "dwm_hook_mode:bool}: which path renders what a meter sees. The awake FP16 "
                           "overlay reads 0.5-2.4 % below the sleeping one at low levels (DLC fald-lessons "
                           "item 5); in hook mode the overlay-only layers (tonemap, fald) are off regardless "
                           "of their flags. Recorded in the readiness neutral audit. Absent on pre-2026-09-13 "
                           "builds (None in the audit).",
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
            "'assign': pin explicit entries and re-inject; 'identify': run an identity-beacon session "
            "(blocking, <= 2 s, no re-inject) and report the positive assignment it produced. "
            "DLC's hardware-readiness self-check "
            "installs a magenta probe cube, proves through the meter that it changes the calibrated "
            "panel, swaps ONCE if it does not, and refuses the run if it still does not.",
            {
                "action": ApiParamSpec("string", description="Routing operation.",
                                       values=["swap", "confirm", "clear", "assign", "identify"]),
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
                "fald": ApiParamSpec("boolean", required=False, description="FALD compensation layer (Experimental), overlay path, per mode: HDR, or SDR under Windows ACM (optional)"),
            },
            {"monitor_mode": "string", "before": "object {white_balance,grayscale,desktop_gamma,tonemap,fald}",
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
            "dispwin -c). Retry/crash-safe as of the snapshot-store fix: re-entering while a session "
            "is ALREADY active keeps the ORIGINAL pre-session snapshot rather than capturing "
            "the cleared state, and reports snapshot_retained=true. A build that predates the "
            "fix omits snapshot_retained entirely and still overwrites (see "
            "transport.timeout_and_retries).",
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
                "snapshot_retained": (
                    "boolean: true when this call KEPT an earlier capture of this monitor from the "
                    "same session (a re-enter after a crashed run) instead of snapshotting the "
                    "already-cleared state. Absent = a server predating the fix, which overwrites — "
                    "treat the preflight settings backup as the authoritative restore."
                ),
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
            "runtime.set_fald_params",
            "FALD compensation layer (Experimental, overlay path; per mode since 2026-09-14: HDR, or SDR under "
            "Windows ACM): set the per-panel parameter file produced by `python -m dlc.fald.export` for the target "
            "monitor:mode. The file's transfer must match the mode (FLD1/FLD2 = PQ = HDR fit; FLD3 with transfer "
            "gamma = SDR fit) — a mismatch is refused (`panel file transfer ... does not match mode ...`); a file "
            "the header peek cannot classify is accepted here and refused by the GPU loader. Toggle the layer with "
            "layers.set {fald}. Builds before 2026-09-14 refuse mode SDR (`fald is an HDR-only layer`).",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "params_path": ApiParamSpec("string", description="Absolute path to the panel parameter file (*.bin)."),
            },
            {"monitor_mode": "string", "params_path": "string", "transfer": "'pq' | 'gamma' | 'unknown' (absent on pre-2026-09-14 builds)"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.fald_debug",
            "FALD compensation layer: debug view on the panel (0 corrected image, 1 gain map white/red +/blue -, 2 real "
            "backlight, 3 panel estimate, 4 identity passthrough, 5 pedestal term x100, 6 per-channel-vs-white "
            "influence x100, 7 temporal settling map red rising/blue falling, 8 the black-frame boost's non-black zone "
            "map, 9 the starfield balancing zone map — grey = zone weight, blue = peak pulled down, red = lifted, 10 the "
            "glow fill: the request it adds per pixel x1000, in the pedestal's colour; not "
            "persisted) and/or the pedestal mode "
            "(ped_mode 0 = white pedestal, 1 = the FLD2 panel file's per-channel leak colour; persisted, "
            "= the GUI 'Per-channel pedestal' checkbox). At least one of the two.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "debug_mode": ApiParamSpec("number", required=False, description="0..10 (8 = the black-frame boost's non-black zone map, 9 = starfield balancing zones, 10 = glow fill x1000)"),
                "ped_mode": ApiParamSpec("number", required=False, description="0 | 1"),
            },
            {"monitor_mode": "string", "debug_mode": "number", "ped_mode": "number", "ped_colour_in_file": "boolean"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.fald_temporal",
            "FALD compensation layer: temporal drive state — the shader's per-cell LED-law filter (DLC "
            "dlc/fald/temporal.py is the reference; work guide H5 / item 4a, 2026-09-17). temporal_mode 0 = off (the "
            "stateless layer, byte for byte), 1 = both backlight fields from the filtered drive (the LEDs AND the "
            "panel's own estimate lag: self-consistent firmware), 2 = only B_true from the filtered drive (the LEDs "
            "lag physically, the LCD compensation follows the commanded drive). tau_rise_ms / tau_fall_ms = first-order "
            "time constants per edge (0 = instant on that edge; max 5000); delay_frames = a pipeline delay 0..3 (the "
            "filter is fed the drives of n frames ago: LED-driver latency after the LCD data, the law a first-order "
            "filter cannot represent). The layer keeps re-running its passes on a static desktop for 5 tau + delay "
            "after the last content frame so the state settles. Persisted per mode (= the GUI 'LED lag' row, which "
            "sets both modes). At least one of the six. Default off: the panel's LED law is UNMEASURED — fit it "
            "(python -m dlc.fald.led_step_fit on a high-fps video of the test clip's toggle segment, --block-roi for "
            "the delay) before trusting a setting; a filter on an instant panel makes pans worse, not better, and the "
            "wrong MODE flashes at every handoff (mode 2 only if the halo step overshoots). temporal_mode 3 = the "
            "MEASURED law, 'panel clock' (EXPERIMENT, work guide C13, 2026-09-20; reference dlc/fald/paneltime.py): the "
            "dimming engine samples and holds at half the refresh rate — at a tick every zone closes `closure` (0.05..1, "
            "default 0.72) of the gap to the drive of the frame one refresh earlier, the panel's compensation follows one "
            "refresh later; `parity` = which refresh it ticks on: -1 unknown (default: the mean of both clocks), 0 / 1 "
            "known (experimental — the wrong one is as bad as no time law). tau / delay are not used by mode 3; "
            "settle_frames_60hz is then its settle hold in elapsed refreshes (closure 0.72 -> 14, capped at 120).",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "temporal_mode": ApiParamSpec("number", required=False, description="0 | 1 | 2 | 3"),
                "tau_rise_ms": ApiParamSpec("number", required=False, description="0..5000"),
                "tau_fall_ms": ApiParamSpec("number", required=False, description="0..5000"),
                "delay_frames": ApiParamSpec("number", required=False, description="0..3"),
                "closure": ApiParamSpec("number", required=False, description="0.05..1 (temporal_mode 3)"),
                "parity": ApiParamSpec("number", required=False, description="-1 | 0 | 1 (temporal_mode 3)"),
            },
            {"monitor_mode": "string", "temporal_mode": "number", "tau_rise_ms": "number", "tau_fall_ms": "number",
             "delay_frames": "number", "closure": "number", "parity": "number", "settle_frames_60hz": "number"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.fald_starfield",
            "FALD compensation layer: starfield balancing (EXPERIMENT, default off; work guide ticket S1; the complete "
            "rules = the module docstring of dlc/fald/starfield.py, GPU-order twin dlc/fald/gpuemu.py, HLSL "
            "src/fald_shader.h). On a mini-LED panel a sparse sub-zone highlight sets its zone's LED drive by its REQUESTED "
            "level, so a field of scattered specks drives its zones unevenly (zone-shaped haze patches). A zone with "
            "star-like content (effective lit area ABOVE THE ZONE'S BACKGROUND — its darkest pixel, so a lit or grainy sky "
            "does not count — between area_lo and area_hi px^2; optionally peak below peak_hi) has its peak pulled toward "
            "a spread-aware target — exp(mean + target_sigma x std) of ln peak over the star-like peaks within `even_reach` "
            "zones (tapered window; the spill of a bright star into a neighbour zone does not count), x target_gain, never "
            "below keep_nits (specks up to ~100 nits make no visible haze: a field below it is left bit-identical), "
            "optionally under cap_nits — by `even` in the log domain (defaults: target_sigma 0 = the geometric mean, even "
            "0.8, keep_nits 100; keep_nits is what spares a dim heavy-tailed star field, target_sigma > 0 is a tunable that "
            "compresses the outliers only, even < 1 keeps their order), dim specks optionally lifted (`lift`), all under `strength`. Solid content (drive nb_lo..nb_hi) "
            "protects its neighbourhood through a tapered field — `reach` zones fully, one more at half — interpolated "
            "per pixel. Per pixel one hue-preserving scale, only inside zones that hold a speck, never below the zone's "
            "background; zone fields bilinear between zone centres; untouched content bit-identical. It CHANGES the "
            "content on purpose; the rest of the layer (statistic, boost count, correction) works on the balanced frame. "
            "Partial updates (any subset; at least one), persisted per mode (= the GUI 'Starfield' row, which sets both "
            "modes). Measuring phases must run with it OFF (fald_profile forces it off and restores it).",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "enabled": ApiParamSpec("boolean", required=False, description="the switch (default false)"),
                "even": ApiParamSpec("number", required=False, description="0..1 log-domain pull toward the target (default 0.8)"),
                "target_sigma": ApiParamSpec("number", required=False, description="0..4 standard deviations of ln peak above the local mean (default 0 = geometric mean)"),
                "keep_nits": ApiParamSpec("number", required=False, description="0..10000 as-if-white nits: the target never falls below it (default 100; 0 = no floor)"),
                "lift": ApiParamSpec("number", required=False, description="0..1 (default 0 = cap-only)"),
                "target_gain": ApiParamSpec("number", required=False, description="0.05..2 (default 1)"),
                "even_reach": ApiParamSpec("number", required=False, description="integer 0..12 zones (default 8)"),
                "cap_nits": ApiParamSpec("number", required=False, description="0..10000 as-if-white nits, 0 = none (default 0)"),
                "strength": ApiParamSpec("number", required=False, description="0..1 (default 1)"),
                "area_lo": ApiParamSpec("number", required=False, description="px^2, fully star-like at / below (default 40)"),
                "area_hi": ApiParamSpec("number", required=False, description="px^2, not star-like at / above; >= area_lo (default 160)"),
                "peak_hi": ApiParamSpec("number", required=False, description="0..10000 nits, 0 = no limit (default 0)"),
                "reach": ApiParamSpec("number", required=False, description="integer 0..4 zones of full protection around solid content; the taper adds one at half (default 2)"),
                "nb_lo": ApiParamSpec("number", required=False, description="0..1 solid neighbour drive, full effect at / below (default 0.15)"),
                "nb_hi": ApiParamSpec("number", required=False, description="0..1, no effect at / above; >= nb_lo (default 0.30)"),
            },
            {"monitor_mode": "string", "enabled": "boolean", "even": "number", "lift": "number", "target_gain": "number", "target_sigma": "number", "keep_nits": "number",
             "even_reach": "number", "cap_nits": "number", "strength": "number", "area_lo": "number", "area_hi": "number",
             "peak_hi": "number", "reach": "number", "nb_lo": "number", "nb_hi": "number"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.fald_glowfill",
            "FALD compensation layer: glow fill (EXPERIMENT, default off; work guide ticket S2; the complete rules = the "
            "module docstring of dlc/fald/glowfill.py, GPU-order twin dlc/fald/gpuemu.py, HLSL src/fald_shader.h). A "
            "CALCULATED black lift that evens the LED glow on dark content: per zone the white pedestal white x tmin x "
            "B_true (the correction's own field: LED boost, starfield balancing and the temporal state included), its grey "
            "CLOSING over a (2 reach + 1)^2 zone box — only holes / valleys of the glow that are enclosed by glow are filled "
            "(no skirt around a bright window, no filled letterbox bars) —, a blur under the closing, the zone deficit (dips "
            "below 5 % ignored); per pixel the interpolated deficit x strength, at most cap_nits, minus what the pixel's own "
            "content already shows, x the correction's deep-dark trust in the panel's estimate (no fill where B_est ~ 0), "
            "requested in the pedestal's colour and never above 0.4 x the drive floor / 0.55 x the boost count's LIT level "
            "(0.1925 nit on the PA32UCXR: a factor 1.5 below the one measured 'not LIT' level; no LED is lit). Lit content "
            "stays bit-identical EXCEPT through the panel's black-frame LED boost: a zone filled at >= ~0.0135 nit counts as "
            "non-black for the firmware, so the fill can move the boost staircase (the layer reads the count from the frame "
            "that carries the fill). With a mean-rule panel file the count-threshold BAND keeps every filled zone clear of "
            "the firmware's threshold T: a zone whose predicted statistic would land in [0.8 T, 1.25 T] has its own pixels' "
            "fill scaled down to 0.8 T. HDR ONLY: `enabled: true` is refused for mode SDR (the ceiling's levels are HDR "
            "measurements); the numbers can be set in either mode. Partial updates (any subset; at least one), persisted "
            "per mode (= the GUI 'Glow fill' row). Measuring phases must run with it OFF (fald_profile forces it off and "
            "restores it).",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "enabled": ApiParamSpec("boolean", required=False, description="the switch (default false)"),
                "strength": ApiParamSpec("number", required=False, description="0..1 share of the glow deficit that is filled (default 1)"),
                "reach": ApiParamSpec("number", required=False, description="integer 1..4 zones: holes / valleys up to 2 x reach zones wide are filled (default 2)"),
                "cap_nits": ApiParamSpec("number", required=False, description="0.005..0.5 as-if-white nits: the fill's ceiling (default 0.05)"),
            },
            {"monitor_mode": "string", "enabled": "boolean", "strength": "number", "reach": "number", "cap_nits": "number"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.fald_dump",
            "FALD compensation layer: on the next frame the layer runs, dump its drive map, both "
            "backlight fields and the frame it saw into `dir` (reference comparison against the Python "
            "model, see results/.../sim/fald_compare_dump.py). With starfield balancing on it adds five zone textures, "
            "cols x rows x 4 float32 each: fald_star_stat.f32 (peak, speck-zone flag, sparse, solid), fald_star_bg.f32 "
            "(ln background of the zone's darkest pixel, the brightest pixel's index ly * cellW + lx inside the zone, lit sum, "
            "a_eff above the background), fald_star_w.f32 (wt = the zone's weight in the target average [0 on a flank zone], "
            "wt ln peak, flank flag, speck-zone flag), fald_star_plan.f32 (w0_field, ln target, ln lift, ln peak) and "
            "fald_star_plan2.f32 (ln background, near = the tapered protection field, speck-zone flag, w) — and `starfield ...` lines "
            "in fald_dump.txt; fald_frame.* stays the SOURCE frame. With the glow fill on it adds fald_glow_vz.f32 (the zone "
            "pedestal Vz, cols x rows float32) and fald_glow_env.f32 (cols x rows x 4 float32: envelope Ez, deficit Dz, closing "
            "Cz, Vz) of round 1, fald_glow_k.f32 (the count-threshold band's zone scale of round 0; mean-rule files only) and "
            "`glowfill ...` lines.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "dir": ApiParamSpec("string", description="Existing directory to write fald_*.f32 / fald_frame.rgba16f / fald_dump.txt into."),
            },
            {"monitor_mode": "string", "dir": "string", "note": "string"},
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
                    "color_space:'SDR'|'ACM_SDR'|'HDR' (ACM_SDR = Windows 'Automatically manage color for apps' on: "
                    "FP16 scRGB composition at SDR luminance — the mode the SDR FALD layer needs; read through "
                    "DisplayConfig since 2026-09-14 (work guide C8) because the DXGI colour space cannot see ACM), "
                    "color_mode_source:'dxgi'|'displayconfig2'|'displayconfig' (which query decided; ABSENT on "
                    "builds before 2026-09-14, whose color_space never says ACM_SDR)}"
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
                "params => same state); calibration.enter is retry-safe on a server that reports "
                "snapshot_retained — a re-enter keeps the ORIGINAL pre-session snapshot per "
                "monitor instead of overwriting it with the already-cleared state (fable "
                "Phase 9 T2). On a server that omits the field the old single-slot overwrite "
                "still applies, so DLC surfaces a stale active calibration mode before "
                "entering and treats the preflight settings backup as the authoritative "
                "restore; mhc.grayscale_commit retried after a real commit "
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
