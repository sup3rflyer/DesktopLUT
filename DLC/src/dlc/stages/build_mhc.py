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

from ..mhc import SRGB_PRIMARIES, build_curves_from_ti3, find_stage_artifact, parse_ti3, resolve_run_path
from ..refine import Deviations, RefinementTarget, propose_correction_grayscale
from ..runs import RunContext
from ..stage import StageResult
from . import _common

# Rec.2020 primaries — the wide-gamut reference for the HDR gamut-drift tell (an HDR
# panel's native gamut is judged against Rec.2020, not sRGB).
REC2020_PRIMARIES = {
    "rx": 0.708, "ry": 0.292, "gx": 0.170, "gy": 0.797, "bx": 0.131, "by": 0.046,
}


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.normalize_mode(args.mode)
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
    if is_hdr:
        # HDR (PQ): the MHC carries the primaries + native-white→D65 *matrix*; the **tone**
        # (the PQ EOTF along the neutral axis) is owned by the 3D-LUT cube — the RBF
        # error-field in ICtCp (v2-design-notes §7/§8 — "the 3D LUT does the volumetric
        # heavy lifting incl. the neutral axis/grayscale"). A power-γ base 1D fit to PQ
        # data would bake a wrong curve, so the base grayscale is identity; the cube (and
        # the final correctionGrayscale tweak) carry the HDR neutral-axis work. The
        # Advanced-Color dummy-ICC semantics are finalized at hardware bring-up; in
        # simulation the matrix + identity base are stored plumbing.
        n = max(1, len(gray_patches))
        base = {
            "point_count": n,
            "points": [round(p.level, 6) for p in gray_patches] or [1.0],
            "deviations": Deviations.identity(n).as_dict(),
        }
        base_summary = {}
        result.action("HDR: base grayscale set to identity (the 3D LUT owns PQ tone / neutral axis)")
    elif len(gray_patches) < 2:
        result.anomaly("too_few_gray", "fewer than 2 neutral patches; base grayscale set to identity", "high")
        n = max(1, len(gray_patches))
        base = {
            "point_count": n,
            "points": [round(p.level, 6) for p in gray_patches] or [1.0],
            "deviations": Deviations.identity(n).as_dict(),
        }
        base_summary = {}
    else:
        prim = _common.measured_primaries_from(measured_primaries, white_xy)
        # Tone-only base: target the panel's own native white so the matrix owns
        # the native-white -> D65 move.
        proposal = propose_correction_grayscale(
            measured=gray_patches,
            target=RefinementTarget(
                white_x=white_xy[0], white_y=white_xy[1], gamma=args.gamma, peak_luminance=target_luminance
            ),
            primaries=prim,
            current=Deviations.identity(len(gray_patches)),
            damping=1.0,  # initial full estimate; refine loop fine-tunes post-install
        )
        base = {
            "point_count": proposal["point_count"],
            "points": proposal["points"],
            "deviations": proposal["deviations"],
        }
        base_summary = proposal["summary"]
        result.action("derived base grayscale 1D correction (tone-only, toward native white)")

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
    }
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
        "base_grayscale_max_abs_deviation": base_summary.get("max_abs_deviation"),
        "params_path": str(params_path),
    }
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
