"""Utility — level-edge: compute (and optionally persist) the luminance-dependent confirmed gamut edge for a run.

``python -m dlc.stages.level_edge --run DIR [--write]``

The level edge (design D4, ``engine.level_gamut``) is fitted from the run's RAW (identity-MHC) pure-channel ramps
and the MHC build's per-channel full-drive reads; a new HDR MHC build persists it automatically. This tool computes
the same block for an EXISTING run — e.g. one built before D4 — and reports it with its falsification on every
measured read file of the run (a read outside the edge at its own luminance contradicts it). ``--write`` stores the
block in ``mhc_params["level_edge"]``; it never touches the run's scoring memo (``calib["oog_level_edge"]``), so a
legacy run keeps scoring exactly as it did (its memo stays absent ⇒ edge off). Numbers only; no hardware.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..mhc import find_stage_artifact, parse_ti3, resolve_run_path
from ..runs import RunContext
from ..stage import StageResult
from . import _common
from .build_mhc import level_edge_block, level_edge_digest

_READ_FILES = ("raw", "post_mhc", "verify", "refine_1")


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult("level-edge")
    state = _common.load_dlc_state(ctx)
    params = state.get("mhc_params") or {}
    if _common.run_mode(args, ctx) != "HDR":
        result.fail("not_hdr", "the level edge is an HDR (FALD/LC pedestal) construct; this run is SDR")
        return result
    if not params.get("channel_peak_xyz"):
        result.fail("no_mhc_build", "no HDR MHC build in this run (mhc_params.channel_peak_xyz missing)")
        return result
    if args.source_ti3:
        source = resolve_run_path(ctx, Path(args.source_ti3))      # explicit: used as given, never substituted
    else:
        source = find_stage_artifact(ctx, "raw-mhc", "ti3")
        if source is None or not Path(source).exists():
            source = ctx.root / "measurements" / "raw.ti3"
        source = resolve_run_path(ctx, Path(source))
    if not source.exists():
        result.fail("no_raw_ti3", f"raw TI3 not found ({source}); pass --source-ti3")
        return result
    result.add_artifact(source)
    white, white_source = _common.target_white_from_state(state)
    floor = float((params.get("dark_floor") or {}).get("nits") or 0.0) or None
    block = level_edge_block(parse_ti3(source), params["channel_peak_xyz"], white_xy=white, floor_nits=floor,
                             wrgb_nonadditive=(params.get("peak_chroma") or {}).get("wrgb_nonadditive"))
    result.action(f"fitted the level edge from {source.name} (white {white[0]:.6f},{white[1]:.6f} {white_source}): "
                  f"{block.get('status')}" + (f" — {block['reason']}" if block.get("reason") else ""))
    falsification = {}
    if block.get("status") == "ok":
        import numpy as np

        from ..engine.cube_quality import level_edge_falsification
        from ..engine.level_gamut import LevelGamut
        gamut = LevelGamut.from_params(block)
        for name in _READ_FILES:
            ti3 = ctx.root / "measurements" / f"{name}.ti3"
            if not ti3.exists():
                continue
            samples = parse_ti3(ti3)
            if not samples:
                continue
            falsification[name] = level_edge_falsification(
                gamut, np.array([s.rgb for s in samples]), np.array([s.xyz for s in samples]),
                floor_nits=float(block["floor_nits"]))
        bad = [n for n, f in falsification.items() if f.get("passed") is False]
        if bad:
            result.anomaly("level_edge_falsified",
                           f"measured reads lie > 1 JND outside the edge in {bad} — the edge does not hold here",
                           "medium")
    previous = params.get("level_edge")
    if args.write:
        params["level_edge"] = block
        state["mhc_params"] = params
        _common.save_dlc_state(ctx, state)
        result.action("stored the block in mhc_params.level_edge (the scoring memo is untouched)")
    memo = (state.get("calib") or {}).get("oog_level_edge")
    result.metrics = {
        "level_edge": level_edge_digest(block),
        "falsification": falsification,
        "previous_key": (previous or {}).get("key") if isinstance(previous, dict) else None,
        "key_changed": bool(isinstance(previous, dict) and previous.get("key") != block.get("key")),
        "scoring_memo": memo,
        "written": bool(args.write),
    }
    result.note("numbers only: whether a run USES the edge is decided at its 3D-LUT build (profile level_edge, "
                "falsification seam) — never by this tool")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC level-edge: compute the luminance-dependent confirmed gamut edge for a run")
    parser.add_argument("--source-ti3", default=None, dest="source_ti3", help="raw TI3 (default: the run's raw set)")
    parser.add_argument("--write", action="store_true", help="persist the block into mhc_params.level_edge")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
