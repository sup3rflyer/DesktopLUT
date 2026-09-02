"""Stage 4a — build-mhc: derive MHC params from the raw-panel TI3.

From a raw (uncorrected) measurement it derives:
  * measured primaries (native panel gamut),
  * the white target (D65, applied by the MHC matrix), and
  * a base grayscale 1D correction.

Design note (matrix vs. 1D LUT split): the MHC matrix carries primaries + the
native-white -> D65 move; the base grayscale 1D LUT carries only per-channel
*tone* correction. To avoid double-counting white balance, the base grayscale is
solved toward the panel's *measured native white* (not D65) — it neutralises the
per-channel transfer-curve shape, and the matrix then rotates native white to
D65. The post-install measure -> refine loop converges the final result toward
D65 empirically, so this split is a starting point, not the last word.

It proposes; it does not install (that is ``install-mhc``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..mhc import (
    SRGB_PRIMARIES,
    build_curves_from_ti3,
    channel_model,
    classify_samples,
    find_stage_artifact,
    parse_ti3,
    resolve_run_path,
    xy_from_xyz,
)
from ..refine import Deviations
from ..runs import RunContext
from ..stage import StageResult
from . import _common

# Rec.2020 primaries — the wide-gamut reference for the HDR gamut-drift tell (an HDR
# panel's native gamut is judged against Rec.2020, not sRGB).
REC2020_PRIMARIES = {
    "rx": 0.708, "ry": 0.292, "gx": 0.170, "gy": 0.797, "bx": 0.131, "by": 0.046,
}


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.run_mode(args, ctx)
    is_hdr = bool(getattr(args, "is_hdr", False))
    result = StageResult("build-mhc")

    if args.source_ti3:
        source = Path(args.source_ti3)
    else:
        source = find_stage_artifact(ctx, "raw-mhc", "ti3")
    if source is None or not Path(source).exists():
        result.fail("no_raw_ti3", "raw-mhc TI3 not found; run `measure --stage raw-mhc` or pass --source-ti3")
        return result
    source = resolve_run_path(ctx, Path(source))
    result.add_artifact(source)

    samples = parse_ti3(source)
    result.action(f"parsed {len(samples)} patches from raw-mhc TI3")

    try:
        # size is irrelevant here (we discard the 1D curve); this call also
        # validates R/G/B ramps and yields native primaries + peak luminance.
        _curves, measured_primaries, target_luminance = build_curves_from_ti3(
            samples, size=2, gamma=args.gamma
        )
    except ValueError as exc:
        result.fail("incomplete_ramps", f"cannot derive primaries: {exc}")
        return result

    white_xy = _common.measured_white_xy(samples)
    gray_patches = _common.gray_patches_from_ti3(samples)
    base_lut = None
    if is_hdr:
        # HDR (PQ): the MHC matrix carries primaries + native-white→D65; the per-channel
        # **EOTF/tone** (neutral axis + per-level grayscale tracking) is carried by a
        # full-resolution per-channel 1D .cube — the path DesktopLUT bakes into its 4096-entry
        # HDR MHC2 LUT (the ColourSpace/DisplayCal import path), reached over the pipe via
        # mhc.set_base_lut. This SUPERSEDES the old "3D LUT owns the HDR neutral axis" design:
        # a 1D LUT does the neutral axis efficiently and leaves only gamut/volumetric to the
        # cube. (The 32-point set_base_grayscale table is far too sparse for a PQ EOTF, so HDR
        # does not use it for the EOTF.) See dlc.mhc_cube for the grounded math.
        from ..mhc_cube import (
            HDR_REFERENCE_WHITE_BAND,
            adaptive_dark_floor,
            build_hdr_cube,
            dark_trust_weights,
            drive_matched_nonadditivity,
            full_drive_neutral_max,
            peak_chroma_luminance,
            resolve_cube_peak,
            write_1d_cube,
        )

        # Per-channel full-drive primary XYZ (R,G,B columns of the additive display matrix, in
        # absolute nits) — the linear-share basis for both the Peak-Chroma cap and the closed-loop
        # refine. Re-derived from the SAME ramps build_curves_from_ti3 validated above.
        groups = classify_samples(samples)
        # Adaptive dark floor: derive the smooth-to-identity luminance from the MEASURED dark-read
        # chroma drift (sensor noise OR panel instability), not a hardcoded 0.3-nit cap. On HDR the
        # reference chromaticity is the stable diffuse-white band (100-203 nits), NOT the brightest
        # patch (the panel in overdrive/ABL/thermal limit — a moving target, not a reference).
        # Each read carries its measured repeatability (noise sidecar) so a REAL, stable dark drift
        # — the disease the cube corrects — is not mistaken for noise and smoothed away.
        grey_reads = _grey_reads_with_noise(source, groups["grey"], xy_from_xyz)
        dark_floor_nits, dark_floor_info = adaptive_dark_floor(
            grey_reads, reference_band=HDR_REFERENCE_WHITE_BAND)
        # Per-level trust from MEASURED repeatability (the noise sidecar beside the raw .ti3): a dark
        # level whose chromaticity is too noisy/unstable to trust is smoothed to identity. Reference =
        # the measured NATIVE white (build_hdr_cube targets native-white shares; the matrix does D65).
        level_trust = _dark_level_trust(source, groups["grey"], white_xy, dark_trust_weights, xy_from_xyz)
        channel_peak_xyz = [
            list(channel_model(groups[name], idx).peak_xyz)
            for name, idx in (("red", 0), ("green", 1), ("blue", 2))
        ]
        # ONE SOURCE OF TRUTH for the HDR peak (Task C / peak-signal-divergence). The cube's
        # neutral-axis ceiling and the C++ handoff peak derive from the orchestrator's RESOLVED
        # **max-sustained** peak — the SAME number patch bounding (calibrate._patch_max_cv →
        # _hdr_target().peak_nits) uses — NOT an independent raw-TI3 max read (which is what caused
        # the mid-pipeline divergence). Clamp to what THIS raw set actually measured (never build a
        # cube above measured drive). Standalone build-mhc (no orchestrator → no resolved peak, or an
        # ungrounded cold-start placeholder) keeps the measured raw max — its own real measurement.
        resolved_peak = getattr(args, "resolved_peak_nits", None)
        if resolved_peak and resolved_peak > 0:
            native_ceiling = min(float(resolved_peak), target_luminance)
            ceiling_source = ("resolved max-sustained peak"
                              + (" (clamped to the measured raw max)" if resolved_peak > target_luminance else ""))
        else:
            native_ceiling = target_luminance
            ceiling_source = "measured raw max (no resolved peak supplied — standalone build-mhc)"

        # Peak-Chroma cap: a SEPARATE, physical constraint — the brightest D65 luminance every
        # channel can render inside full drive (the cold channel binds). OPTION 1 (owner 2026-06-23):
        # build the cube's NEUTRAL axis TO this cap so it holds D65 all the way up instead of warming
        # near the top, and REPORT that post-cube peak (what the cube actually delivers) as
        # `peak_nits` — the number DesktopLUT's tonemapTargetPeak tracks. Content goes brighter (patch
        # bounding → max-sustained); above the cap, neutral chroma-relax/roll-off is DesktopLUT's job
        # (hdr-rolloff-division-of-labour), NOT baked here. The closed-loop refine targets D65 at this
        # same cap, so build + refine agree on the neutral ceiling.
        #
        # WRGB GATE (2026-09-02, LG C6 run 1): Option 1 assumes the RGB-ADDITIVE share model is a
        # fair picture of white — true on the FALD panels it was designed on (cap 1704 vs additive
        # 1734, near-tied), catastrophically false on a WRGB OLED where the W subpixel carries ~half
        # of white luminance (measured white 1.47–1.86× the additive sum; the blue-limited "cap" of
        # 178 nits crushed a 604-nit panel to ~127 → verify white 118 dE_ITP, run REVERTED). The
        # additivity test + policy decision live in mhc_cube.resolve_cube_peak; above the threshold
        # the cap is DIAGNOSTIC-ONLY and the neutral runs to the ceiling (the refine's share-ratio
        # law then holds exact D65 below the achievable knee and drifts gracefully above it).
        cube_peak = native_ceiling
        try:
            cap_nits, binding = peak_chroma_luminance(channel_peak_xyz)
            native_peak = sum(channel_peak_xyz[c][1] for c in range(3))
            # W-subpixel non-additivity gate (WRGB OLED): compare the neutral against the additive
            # RGB sum at the SAME drive level (drive_matched_nonadditivity) — NOT full-drive-white
            # over peak-code-primaries, which conflates the W boost with near-peak EOTF roll-off and
            # narrows the additive-panel margin (adversarial review 2026-09-02 #1). This is robust on
            # a peak-bounded ti3 too (the gate no longer needs the full-drive extension), so a WRGB
            # panel measured without the headroom levels still detects correctly instead of silently
            # re-crushing (#2). full_drive_neutral_max is the explicit grounded-peak witness: None ⇒
            # the ramp never reached full drive, surfaced so the seam sees an ungrounded ceiling.
            nonadd = drive_matched_nonadditivity(samples, channel_peak_xyz)
            full_drive_white = full_drive_neutral_max(samples)
            cube_peak, cap_policy, wrgb_nonadditive = resolve_cube_peak(
                cap_nits, native_ceiling, nonadd)
            peak_chroma = {
                "cap_nits": round(cap_nits, 4),
                "binding_channel": binding,
                "native_peak_nits": round(native_peak, 4),
                "resolved_peak_nits": round(native_ceiling, 4),   # the max-sustained ceiling (one source)
                "ceiling_source": ceiling_source,
                "cube_peak_nits": round(cube_peak, 4),
                "capped": cube_peak < native_ceiling,
                "cap_policy": cap_policy,
                "wrgb_nonadditive": wrgb_nonadditive,
                "drive_matched_nonadditivity": round(nonadd, 4) if nonadd is not None else None,
                "full_drive_white_nits": round(full_drive_white, 4)
                if full_drive_white is not None else None,
                "full_drive_grounded": full_drive_white is not None,
                "headroom_loss_pct": round(100.0 * (1.0 - cap_nits / native_peak), 3)
                if native_peak > 0 else None,
            }
        except ValueError as exc:
            peak_chroma = {"error": str(exc)}

        # Identity 32-point base kept as harmless state plumbing; the cube is authoritative.
        n = 32
        base = {
            "point_count": n,
            "points": [round(i / (n - 1), 6) for i in range(n)],
            "deviations": Deviations.identity(n).as_dict(),
        }
        try:
            cube_curves, cube_summary = build_hdr_cube(
                samples, measured_primaries, white_xy, cube_peak,
                dark_floor_nits=dark_floor_nits, level_trust=level_trust
            )
            cube_summary["dark_floor"] = dark_floor_info
            if level_trust:
                cube_summary["dark_trust_levels"] = len(level_trust)
                cube_summary["dark_trust_min"] = round(min(w for _s, w in level_trust), 4)
            cube_path = ctx.root / "generated" / f"mhc_base_{mode.lower()}.cube"
            write_1d_cube(cube_path, cube_curves, title=f"DLC HDR MHC base (mon {args.monitor})")
            result.add_artifact(cube_path)
            base_lut = {
                "cube_path": str(cube_path),
                "peak_nits": round(cube_peak, 4),   # the POST-CUBE deliverable peak (Option 1 cap)
                "summary": cube_summary,
            }
            result.action(
                f"HDR: built {int(cube_summary['lut_size'])}-point per-channel EOTF .cube "
                f"(white_max {cube_summary['white_max_nits']:.0f} nits) — full-res MHC base"
            )
        except ValueError as exc:
            result.anomaly("hdr_cube_failed",
                           f"could not build HDR EOTF cube ({exc}); base grayscale left identity",
                           "high")
    elif len(gray_patches) < 2:
        result.anomaly("too_few_gray", "fewer than 2 neutral patches; base grayscale set to identity", "high")
        n = max(1, len(gray_patches))
        base = {
            "point_count": n,
            "points": [round(p.level, 6) for p in gray_patches] or [1.0],
            "deviations": Deviations.identity(n).as_dict(),
        }
    else:
        # SDR (γ): same 1D-LUT-base mechanism as HDR. The MHC matrix carries primaries +
        # native-white→D65; the per-channel **tone** (neutral axis + per-level grayscale tracking)
        # rides a DLC-owned full-resolution per-channel 1D .cube delivered over mhc.set_base_lut
        # (sourceIs1DCube → DesktopLUT's 1024-entry SDR MHC2 LUT). This REPLACES the old
        # set_base_grayscale + correctionGrayscale-refine path (2026-06-24): the refine must NOT
        # squat in DesktopLUT's user-editable correctionGrayscale slot (a user "Reset Grayscale"
        # wiped it). A loaded 1D cube locks that editor (+ its Reset button) and leaves RGBW for the
        # matrix — see [[dlc-must-not-own-mhc-user-layers]]. build_sdr_cube is the γ analog of
        # build_hdr_cube; the closed-loop refine (stage_refine_mhc_cube) corrects the per-level residual.
        from ..mhc_cube import adaptive_dark_floor, build_sdr_cube, dark_trust_weights, write_1d_cube

        # Adaptive dark floor (SDR) — same measured-chroma-drift logic as HDR, anchored on the
        # BRIGHTEST neutral (on SDR the peak IS the target white; reference_band=None). Persisted below
        # for the SDR closed-loop refine to smooth dark levels to identity. Reads carry their measured
        # repeatability (noise sidecar) so a real, stable dark drift is corrected, not smoothed away.
        sdr_dark_floor_nits, sdr_dark_floor_info = adaptive_dark_floor(
            _grey_reads_with_noise(source, gray_patches, xy_from_xyz), reference_band=None)
        # Per-level trust from MEASURED repeatability (the noise sidecar): a dark level whose
        # chromaticity is too noisy/unstable to trust is smoothed to identity in the cube. Reference =
        # the measured NATIVE white (build_sdr_cube targets native-white shares; the matrix does D65).
        groups = classify_samples(samples)
        level_trust = _dark_level_trust(source, groups["grey"], white_xy, dark_trust_weights, xy_from_xyz)

        # Identity 32-point base kept as harmless state plumbing; the cube is authoritative (mirrors HDR).
        n = 32
        base = {
            "point_count": n,
            "points": [round(i / (n - 1), 6) for i in range(n)],
            "deviations": Deviations.identity(n).as_dict(),
        }
        try:
            cube_curves, cube_summary = build_sdr_cube(
                samples, measured_primaries, white_xy, target_luminance,
                gamma=args.gamma, dark_floor_nits=sdr_dark_floor_nits, level_trust=level_trust)
            cube_summary["dark_floor"] = sdr_dark_floor_info
            if level_trust:
                cube_summary["dark_trust_levels"] = len(level_trust)
                cube_summary["dark_trust_min"] = round(min(w for _s, w in level_trust), 4)
            cube_path = ctx.root / "generated" / f"mhc_base_{mode.lower()}.cube"
            write_1d_cube(cube_path, cube_curves, title=f"DLC SDR MHC base (mon {args.monitor})")
            result.add_artifact(cube_path)
            base_lut = {
                "cube_path": str(cube_path),
                "peak_nits": 0.0,   # SDR: the 1024-entry LUT carries no HDR luminance metadata
                "summary": cube_summary,
            }
            n_trust = sum(1 for _s, w in (level_trust or []) if w < 1.0)
            trust_note = (f"; {n_trust} dark level(s) smoothed toward identity by measured noise"
                          if level_trust and n_trust else "")
            result.action(
                f"SDR: built {int(cube_summary['lut_size'])}-point per-channel γ{args.gamma} EOTF .cube "
                f"(tone-only, toward native white) — DLC-owned 1D-LUT base" + trust_note)
        except ValueError as exc:
            result.anomaly("sdr_cube_failed",
                           f"could not build SDR EOTF cube ({exc}); base grayscale left identity", "high")

    # White distance from D65 and a CCT readout for the assistant.
    white_de_d65 = _white_de_from_d65(white_xy, target_luminance)
    cct = _common.cct_mccamy(*white_xy)
    try:
        target_white_xy, target_white_source = _common.target_white_from_args(args)
    except ValueError as exc:
        result.fail("invalid_target_white", str(exc))
        return result

    # Gamut sanity vs the target's primaries (Rec.2020 for HDR, sRGB for SDR).
    ref_primaries = REC2020_PRIMARIES if is_hdr else SRGB_PRIMARIES
    ref_label = "Rec.2020" if is_hdr else "sRGB"
    gamut_drift = {
        f"{k}": round(measured_primaries[k] - ref_primaries[k], 4)
        for k in ("rx", "ry", "gx", "gy", "bx", "by")
    }
    wide_gamut = any(abs(v) > 0.03 for v in gamut_drift.values())
    if wide_gamut:
        result.anomaly(
            "wide_gamut",
            f"measured primaries differ from {ref_label} by >0.03; matrix will map a wide gamut to the {ref_label} target",
            "low",
        )

    params = {
        "monitor": args.monitor,
        "mode": mode,
        "primaries": {k: round(measured_primaries[k], 6) for k in ("rx", "ry", "gx", "gy", "bx", "by")},
        "white": {"x": round(target_white_xy[0], 6), "y": round(target_white_xy[1], 6)},
        "white_source": target_white_source,
        "measured_white": {"x": round(white_xy[0], 6), "y": round(white_xy[1], 6)},
        "target_gamma": args.gamma,
        "target_luminance": round(target_luminance, 4),
        "base_grayscale": base,
        "base_lut": base_lut,
    }
    if is_hdr:
        # The linear-share basis + Peak-Chroma cap the closed-loop refine consumes (HDR only).
        params["channel_peak_xyz"] = [[round(v, 6) for v in xyz] for xyz in channel_peak_xyz]
        params["peak_chroma"] = peak_chroma
        params["dark_floor"] = {"nits": round(dark_floor_nits, 4), **dark_floor_info}
    elif len(gray_patches) >= 2:
        # SDR closed-loop refine (stage_refine_mhc_grayscale) consumes the dark floor too.
        params["dark_floor"] = {"nits": round(sdr_dark_floor_nits, 4), **sdr_dark_floor_info}
    params_path = ctx.root / "generated" / f"mhc_params_{mode.lower()}.json"
    params_path.parent.mkdir(parents=True, exist_ok=True)
    params_path.write_text(json.dumps(params, indent=2), encoding="utf-8")
    result.add_artifact(params_path)

    # Persist into the run-record so install-mhc / refine can read primaries + white.
    state = _common.load_dlc_state(ctx)
    state["monitor"] = args.monitor
    state["mode"] = mode
    state["mhc_params"] = {**params, "measured_primaries": params["primaries"]}
    _common.save_dlc_state(ctx, state)

    result.preconditions = {"raw_ti3_present": True, "rgb_ramps_present": True}
    result.metrics = {
        "measured_primaries": params["primaries"],
        "measured_white_xy": params["measured_white"],
        "measured_white_cct": round(cct) if cct else None,
        "measured_white_de2000_vs_d65": round(white_de_d65, 3),
        "target_white_xy": [params["white"]["x"], params["white"]["y"]],
        "target_white_source": target_white_source,
        "target_luminance": params["target_luminance"],
        "gamut_drift_vs_target": gamut_drift,
        "params_path": str(params_path),
    }
    if is_hdr:
        result.metrics["peak_chroma"] = peak_chroma
    result.note(
        "Matrix carries primaries + native-white->target-white; base grayscale is tone-only toward native white. "
        f"Target white is {params['white']['x']},{params['white']['y']} ({target_white_source}); "
        "post-install measure->refine converges the final white empirically."
    )
    result.advice = {
        "default_policy_verdict": "install",
        "reasons": ["candidate MHC params derived from a measured raw panel"],
    }
    return result


def _grey_reads_with_noise(source, grey_samples, xy_from_xyz):
    """``[(nits, x, y, noise), ...]`` for :func:`mhc_cube.adaptive_dark_floor`: each gray read plus
    its measured repeatability from the noise sidecar (SE of the mean chromaticity; ``+inf`` for an
    unstable level; ``None`` when single-read / no sidecar — the floor then stays conservative).
    Accepts ``Ti3Sample`` (``.rgb``) or ``refine.GrayPatch`` (``.level``) gray entries."""
    from ..measure_loop import match_level_noise, read_noise_sidecar
    entries = read_noise_sidecar(Path(source))
    reads = []
    for s in grey_samples:
        level = s.rgb[0] if hasattr(s, "rgb") else s.level
        noise = match_level_noise(entries, level) if entries else None
        x, y = xy_from_xyz(s.xyz)
        reads.append((s.xyz[1], x, y, noise))
    return reads


def _dark_level_trust(source, grey_samples, reference_white_xy, dark_trust_weights, xy_from_xyz):
    """Build per-level trust weights from the measure loop's noise sidecar (``<ti3>.noise.json``),
    if present: each gray level's measurement noise (SE of the mean chromaticity, or +inf if the
    level was flagged unstable) → ``mhc_cube.dark_trust_weights``. Levels are matched to the parsed
    TI3 by NEAREST signal (robust to the ti3 percent roundtrip). Returns ``[(signal, w), ...]`` or
    ``None`` when there's no sidecar / no usable noise (single-read run)."""
    from ..measure_loop import match_level_noise, read_noise_sidecar
    entries = read_noise_sidecar(Path(source))
    if not entries:
        return None
    levels = []
    for s in grey_samples:
        noise = match_level_noise(entries, s.rgb[0])
        x, y = xy_from_xyz(s.xyz)
        levels.append((s.rgb[0], x, y, noise))
    weights = dark_trust_weights(levels, reference_white_xy)
    return weights or None


def _white_de_from_d65(white_xy: tuple[float, float], luminance: float) -> float:
    from ..metrics import xyz_to_lab, delta_e2000
    from ..mhc import white_xyz

    ref = white_xyz(luminance)  # D65 at the same luminance
    meas = (white_xy[0] / white_xy[1] * luminance, luminance, (1 - white_xy[0] - white_xy[1]) / white_xy[1] * luminance)
    return delta_e2000(xyz_to_lab(meas, ref), xyz_to_lab(ref, ref))


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC build-mhc: derive MHC params from raw TI3")
    parser.add_argument("--source-ti3", default=None, dest="source_ti3", help="raw-mhc TI3 (default: latest in run)")
    parser.add_argument("--gamma", type=float, default=2.2, help="target tone gamma (default 2.2)")
    _common.add_target_white_args(parser)
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
