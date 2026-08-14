"""Adaptive-planning **evidence + tools + validated decision** (#47/#49).

The adaptive patch planner is an *LLM investigation seam*, not a scripted recommender.
When ``--adaptive-planning`` is on, the run pauses after the ICC and hands the LLM an
**evidence packet** of raw facts (not a verdict); the LLM **investigates** with the
bounded read-only tools here (also exposed as ``python -m dlc.patch_evidence`` so a
paused run can be probed from a shell), then returns a **structured decision** which
this module **validates** against bounds before the run applies it.

The division of labour the owner asked for:

* **The LLM is the adjudicator.** It chooses the patch strategy from the evidence.
* **Scripts are guards, not decision-makers.** :func:`gather_evidence` only assembles
  facts + non-binding "worth investigating" flags; :func:`validate_decision` clamps an
  out-of-bounds decision; :func:`conservative_fallback` supplies a *low-confidence*
  default ONLY for autonomous (``--auto``) runs where no LLM is in the loop.
* **The ICC/raw foundation is never adapted** — the validator whitelists only shadow +
  volumetric knobs (:data:`KNOB_BOUNDS`); ``raw_*``/``verify_*`` are not overridable.

Numpy is used only by :func:`analyze_raw_ti3` (a robust tone fit); the rest is stdlib.

**VALUE STATUS — EXPERIMENTAL, unproven (kept opt-in/default-off pending hardware).**
A synthetic A/B (default vs ``denser`` training plan, fixed 157-patch verify yardstick,
``optimize_cube`` with fold-back, three panels) found denser sampling does NOT earn its
~2× measurement cost: the gain was sub-perceptual on an easy panel, did not touch the
dominant error (a physical clip) on a pathological panel, and was *worse* under
measurement noise (more patches → more noise into the RBF model). The architectural
reason: ``optimize_cube``'s fold-back loop already manufactures training density where
the cube operates, so the *initial* density is a non-binding constraint. This stays
behind ``--adaptive-planning`` as an experiment in case real hardware diverges from the
model. To re-validate: A/B ``tube_patches`` default vs denser through ``optimize_cube``
(``synthetic_probe`` or, better, a real-HW run) and compare final verify dE — only ship
it on as the default if denser meaningfully beats default-with-fold-back on real panels.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "SHADOW_TIERS",
    "VOLUMETRIC_TIERS",
    "CONFIDENCE_LEVELS",
    "KNOB_BOUNDS",
    "DECISION_SCHEMA",
    "summarize_dip",
    "analyze_raw_ti3",
    "gamut_overcoverage",
    "estimate_patch_plan",
    "compare_patch_plans",
    "list_prior_runs",
    "inspect_stage_metrics",
    "gather_evidence",
    "conservative_fallback",
    "validate_decision",
    "shadow_knobs",
    "volumetric_knobs",
]

SHADOW_TIERS = ("standard", "extra", "heavy")
VOLUMETRIC_TIERS = ("sparser", "standard", "denser", "custom")
CONFIDENCE_LEVELS = ("low", "medium", "high")

# The bounded structured decision the LLM returns (also the autonomous fallback's shape).
DECISION_SCHEMA: dict[str, Any] = {
    "shadow_treatment": list(SHADOW_TIERS),
    "volumetric_density": list(VOLUMETRIC_TIERS),
    "patch_size_overrides": "object: only whitelisted shadow/volumetric knobs (see KNOB_BOUNDS), clamped to bounds",
    "reason": "string",
    "confidence": list(CONFIDENCE_LEVELS),
}

# The ONLY knobs an adaptive decision may touch (shadow + volumetric density). The ICC/raw
# FOUNDATION and the verify set are deliberately absent — they are never adapted. Numeric
# bounds are (lo, hi) clamps; categoricals are a set of allowed values.
# KNOWN LIMITATION: knobs are validated INDEPENDENTLY. The tiers only ever touch tube/cube/
# tube_radius/low_light_*; the rest (volumetric_mode, grid_type, spines, gamut_*, neutral_steps)
# are reachable only via a raw `patch_size_overrides`, and a `volumetric_mode` switch silently
# makes some other knobs inert (e.g. mode="gamut" ignores tube_size) — cross-knob coherence is
# not checked. Acceptable while the feature is experimental; tighten (or trim the whitelist to
# the ~6 tier knobs) before promoting it.
KNOB_BOUNDS: dict[str, Any] = {
    "tube_size": (5, 129),
    "cube_size": (3, 33),
    "tube_radius": (1, 8),
    "grid_type": {"cub", "bcc"},
    "spines": {True, False},
    "volumetric_mode": {"tube", "cube", "gamut"},
    "gamut_lum_steps": (5, 65),
    "gamut_hues": (4, 48),
    "gamut_lum_bias": (1.0, 3.0),
    "neutral_steps": (5, 65),
    "low_light_steps": (0, 64),
    "low_light_cube_size": (0, 16),
    "low_light_signal": (0.05, 0.40),
    "low_light_bias": (1.0, 4.0),
}

# Conservative-fallback thresholds (NOT the primary intelligence — only autonomous runs).
_CONTRAST_RAISED = 200.0
_CONTRAST_SEVERE = 100.0
_NEAR_BLACK_SIGMA_NOISY = 0.6
_OVERCOVERAGE_WIDE = 1.30
_TONE_NONLINEAR = 0.04
_MHC_RESIDUAL_NOTABLE = 1.5


# ---------------------------------------------------------------------------
# Tier → knob maps (relative to the run's current base; the LLM may override)
# ---------------------------------------------------------------------------

def shadow_knobs(tier: str, base: dict[str, Any]) -> dict[str, Any]:
    """Additive shadow-density knobs for a shadow tier, relative to the run's base
    (so a CLI/profile-customised base is respected). ``standard`` ⇒ no change."""
    s = int(base.get("low_light_steps", 9))
    c = int(base.get("low_light_cube_size", 5))
    if tier == "extra":
        return {"low_light_steps": s + 6, "low_light_cube_size": c + 2}
    if tier == "heavy":
        return {"low_light_steps": s + 12, "low_light_cube_size": c + 3, "low_light_signal": 0.25}
    return {}


def volumetric_knobs(tier: str, base: dict[str, Any]) -> dict[str, Any]:
    """Volumetric-density knobs for a volumetric tier, relative to the base.
    ``standard``/``custom`` ⇒ no preset (custom is driven by overrides)."""
    t = int(base.get("tube_size", 33))
    c = int(base.get("cube_size", 9))
    r = int(base.get("tube_radius", 2))
    if tier == "denser":
        return {"tube_size": t + 8, "cube_size": c + 2, "tube_radius": r + 1}
    if tier == "sparser":
        return {"tube_size": max(9, t - 8), "cube_size": max(5, c - 2)}
    return {}


# ---------------------------------------------------------------------------
# Investigator tools (read-only; also the CLI's workers)
# ---------------------------------------------------------------------------

def summarize_dip(dip: Any) -> dict[str, Any]:
    """A compact, JSON-friendly view of a Display+Instrument Profile (or ``None``):
    native primaries / white / black / contrast, the noise bands (σ vs nits), and the
    thermal time constant — the raw panel facts the LLM weighs for shadow + gamut."""
    if dip is None:
        return {"present": False}
    white = getattr(dip, "native_white_nits", None)
    black = getattr(dip, "native_black_nits", None)
    contrast = (round(white / black) if (white and black and black > 0) else None)
    bands = [{"nits": round(float(b.nits), 4), "sigma_de": round(float(b.sigma_de), 4)}
             for b in (getattr(dip, "noise_model", None) or [])]
    bands.sort(key=lambda b: b["nits"])
    return {
        "present": True,
        "made": getattr(dip, "made", None),
        "native_primaries": getattr(dip, "native_primaries", None),
        "native_white_nits": (round(float(white), 2) if white is not None else None),
        "native_black_nits": (round(float(black), 5) if black is not None else None),
        "contrast": contrast,
        "noise_bands": bands,
        "near_black_sigma_de": (bands[0]["sigma_de"] if bands else None),
        "thermal_tau_patches": getattr(dip, "thermal_tau_patches", None),
        "read_overhead_s": getattr(dip, "read_overhead_s", None),
    }


def analyze_raw_ti3(source: Any) -> dict[str, Any]:
    """Per-channel tone analysis of the raw (pre-MHC) grey ramp: a robust best-fit
    gamma and the **bumpiness** (peak deviation from that power law, in normalized
    luminance). Fitting gamma + scale isolates genuine non-power bumpiness — which the
    cube's neutral tube must resolve — from a mere native-gamma offset (the ICC 1D
    corrects that for free). Robust to glitch reads via MAD outlier rejection. ``source``
    is a ``.ti3`` path or a list of parsed samples. ``available: False`` if too short."""
    import numpy as np
    from .mhc import parse_ti3

    if source is None:
        return {"available": False, "reason": "no raw measurement"}
    try:
        samples = source if isinstance(source, list) else parse_ti3(Path(source))
    except Exception as exc:  # noqa: BLE001 - advisory; never break the seam
        return {"available": False, "reason": f"could not read ti3: {exc}"}
    gray = [(s.rgb[0], s.xyz[1]) for s in samples
            if abs(s.rgb[0] - s.rgb[1]) < 1e-6 and abs(s.rgb[1] - s.rgb[2]) < 1e-6
            and 0.0 < s.rgb[0] <= 1.0 and s.xyz[1] > 0.0]
    if len(gray) < 8:
        return {"available": False, "reason": f"only {len(gray)} usable grey points (<8)"}
    x = np.array([g[0] for g in gray], dtype=float)
    y = np.array([g[1] for g in gray], dtype=float)
    y = y / float(y.max())
    if float(np.ptp(x)) <= 0:
        return {"available": False, "reason": "degenerate grey ramp (no input range)"}
    lx, ly = np.log(x), np.log(y)
    gamma, log_a = np.polyfit(lx, ly, 1)
    log_resid = ly - (gamma * lx + log_a)
    # Reject on deviation from the MEDIAN residual, not from zero: a gross outlier shifts the
    # whole OLS fit, so the clean points share a large common residual — only their spread
    # around the median (the MAD) flags the true glitch.
    median = float(np.median(log_resid))
    mad = float(np.median(np.abs(log_resid - median)))
    keep = np.abs(log_resid - median) <= max(0.15, 4.0 * mad)
    rejected = int(len(x) - int(keep.sum()))
    if int(keep.sum()) < 4:
        return {"available": False, "reason": "too few inliers after glitch rejection"}
    if int(keep.sum()) < len(x):
        gamma, log_a = np.polyfit(lx[keep], ly[keep], 1)
        x, y = x[keep], y[keep]
    y_fit = np.exp(log_a) * x ** gamma
    return {
        "available": True,
        "grey_points": len(gray),
        "fit_gamma": round(float(gamma), 4),
        "bumpiness": round(float(np.max(np.abs(y - y_fit))), 5),
        "rejected_outliers": rejected,
    }


def gamut_overcoverage(dip: Any, target_primaries: Optional[dict[str, Any]]) -> Optional[float]:
    """Native gamut area / target gamut area — how much wider than the target the panel
    is, i.e. how hard the cube must remap saturation. ``None`` when uncharacterized."""
    from . import gamut as gamut_mod

    if dip is None or not getattr(dip, "native_primaries", None) or not target_primaries:
        return None
    native = {ch: (float(xy[0]), float(xy[1]))
              for ch, xy in dip.native_primaries.items() if xy and len(xy) >= 2}
    if not {"R", "G", "B"} <= set(native):
        return None
    cov = gamut_mod.gamut_coverage(native, target_primaries)
    tgt_area = float(cov.get("target_area") or 0.0)
    if tgt_area <= 0:
        return None
    return round(float(cov.get("native_area") or 0.0) / tgt_area, 4)


def estimate_patch_plan(patch_sizes: dict[str, Any], transfer: Any, *,
                        flow: str = "full", read_overhead_s: Optional[float] = None,
                        settle_seconds: Optional[float] = None) -> dict[str, Any]:
    """Per-stage patch counts + total for a hypothetical ``PatchSizes`` (a dict of knob
    overrides on the defaults), plus a rough wall-time estimate when the DIP supplies a
    per-read overhead. Lets the LLM weigh "denser" against the time it costs BEFORE
    committing. Lazily reuses the orchestrator's own generators so the count is exact."""
    from .patch_sets import PatchSizes, flow_patch_counts

    ps = PatchSizes.from_dict(patch_sizes) if isinstance(patch_sizes, dict) else patch_sizes
    counts = flow_patch_counts(flow, ps, transfer)
    per_read = (float(read_overhead_s or 0.0) + float(settle_seconds or 0.0))
    out = {"flow": flow, **counts}
    if per_read > 0:
        out["est_seconds"] = round(counts["total_patches"] * per_read, 1)
        out["est_minutes"] = round(counts["total_patches"] * per_read / 60.0, 1)
    return out


def compare_patch_plans(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Diff two :func:`estimate_patch_plan` results — per-stage and total deltas, so the
    LLM can see exactly what a proposed strategy adds/removes vs. the current plan."""
    stages_a, stages_b = a.get("stages", {}), b.get("stages", {})
    keys = sorted(set(stages_a) | set(stages_b))
    return {
        "stage_delta": {k: int(stages_b.get(k, 0)) - int(stages_a.get(k, 0)) for k in keys},
        "total_delta": int(b.get("total_patches", 0)) - int(a.get("total_patches", 0)),
        "seconds_delta": (round(float(b.get("est_seconds", 0)) - float(a.get("est_seconds", 0)), 1)
                          if "est_seconds" in a and "est_seconds" in b else None),
        "a_total": int(a.get("total_patches", 0)),
        "b_total": int(b.get("total_patches", 0)),
    }


def list_prior_runs(runs_dir: Any, display: Optional[str] = None, *, limit: int = 8) -> list[dict[str, Any]]:
    """Prior runs for this display (newest first): their flow, status, and final verify
    metrics — so the LLM can learn from what density worked (or didn't) before. Reads
    ``manifest.json`` + the recorded ``verify`` stage digest from each run dir."""
    root = Path(runs_dir)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/manifest.json"), reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        dlc = manifest.get("desktoplut", {}) if isinstance(manifest, dict) else {}
        if display and str(dlc.get("display") or manifest.get("display") or "") != display:
            continue
        rec: dict[str, Any] = {"run": manifest_path.parent.name,
                               "mode": manifest.get("mode"), "flow": dlc.get("flow")}
        calib = dlc.get("calib") if isinstance(dlc, dict) else None
        verify = (calib or {}).get("stages", {}).get("verify") if isinstance(calib, dict) else None
        if isinstance(verify, dict):
            dg = verify.get("digest", {})
            rec["verify"] = {k: dg.get(k) for k in
                             ("avg_de2000", "max_de2000", "within_quality") if k in dg}
            # Gate basis disambiguates cross-era comparisons (2026-08-14): a pre-D3 run's
            # within_quality judged the OOG-inflated overall; a post-D3 run judges practical
            # core+tube+white — without the basis, identical panels read as a quality change.
            basis = (dg.get("gate") or {}).get("basis")
            if basis:
                rec["verify"]["gate_basis"] = basis
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def inspect_stage_metrics(run_dir: Any, stage: str) -> dict[str, Any]:
    """The recorded digest of one stage in a run (e.g. ``build-install-mhc`` for the ICC
    residual, or ``measure:raw`` for the raw read). Read-only; ``{}`` if absent."""
    from .stages import _common
    from .runs import open_run

    try:
        ctx = open_run(Path(run_dir))
        state = _common.load_dlc_state(ctx)
    except Exception:  # noqa: BLE001
        return {}
    rec = (state.get("calib", {}).get("stages", {}) or {}).get(stage)
    return rec.get("digest", {}) if isinstance(rec, dict) else {}


# ---------------------------------------------------------------------------
# Evidence packet + conservative fallback
# ---------------------------------------------------------------------------

def gather_evidence(*, dip: Any, target_primaries: Optional[dict[str, Any]],
                    target_colorspace: Optional[str], raw_ti3: Any, mhc_digest: dict[str, Any],
                    patch_sizes: dict[str, Any], transfer: Any, flow: str,
                    prior_runs: list[dict[str, Any]], cache_state: dict[str, Any]) -> dict[str, Any]:
    """Assemble the evidence packet handed to the LLM at the seam — raw facts only, plus
    non-binding "worth investigating" flags and the conservative fallback for reference.
    No verdict: the LLM decides."""
    dip_summary = summarize_dip(dip)
    raw_tone = analyze_raw_ti3(raw_ti3)
    overcoverage = gamut_overcoverage(dip, target_primaries)
    mhc_white_de = mhc_digest.get("white_de_vs_d65") if isinstance(mhc_digest, dict) else None
    plan = estimate_patch_plan(patch_sizes, transfer, flow=flow,
                               read_overhead_s=dip_summary.get("read_overhead_s"))

    flags: list[str] = []
    contrast = dip_summary.get("contrast")
    if contrast is not None and contrast < _CONTRAST_RAISED:
        flags.append(f"raised black (contrast ~{contrast}:1) — shadows may need more density")
    sigma = dip_summary.get("near_black_sigma_de")
    if sigma is not None and sigma >= _NEAR_BLACK_SIGMA_NOISY:
        flags.append(f"noisy near-black reads (σ dE {sigma}) — more shadow samples average it down")
    if overcoverage is not None and overcoverage >= _OVERCOVERAGE_WIDE:
        flags.append(f"wide native gamut (~{overcoverage}× target area) — heavy saturation remapping")
    if raw_tone.get("available") and raw_tone.get("bumpiness", 0.0) >= _TONE_NONLINEAR:
        flags.append(f"bumpy tone response (peak dev {raw_tone['bumpiness']}) — denser neutral tube")
    if mhc_white_de is not None and mhc_white_de >= _MHC_RESIDUAL_NOTABLE:
        flags.append(f"ICC left a {mhc_white_de} ΔE neutral residual for the cube")

    evidence = {
        "flow": flow,
        "target": {"colorspace": target_colorspace, "primaries": target_primaries},
        "dip": dip_summary,
        "gamut_overcoverage": overcoverage,
        "raw_tone": raw_tone,
        "raw_ti3_path": (str(raw_ti3) if isinstance(raw_ti3, (str, Path)) else None),
        "mhc_residual": {"white_de_vs_d65": mhc_white_de},
        "current_plan": plan,
        "base_patch_sizes": patch_sizes,
        # The transfer the run measures under — serialized so the investigator CLI can
        # rebuild it to cost a hypothetical (denser/sparser) plan without re-deriving it.
        "transfer": {"kind": getattr(transfer, "kind", "power"),
                     "gamma": getattr(transfer, "gamma", 2.2),
                     "peak_nits": getattr(transfer, "peak_nits", 120.0),
                     "bit_depth": getattr(transfer, "bit_depth", 10)},
        "prior_runs": prior_runs,
        "cache_state": cache_state,
        "worth_investigating": flags,
    }
    evidence["conservative_fallback"] = conservative_fallback(evidence)
    return evidence


def conservative_fallback(evidence: dict[str, Any]) -> dict[str, Any]:
    """The salvaged heuristic, **demoted to a guard**: a low-confidence default decision
    used ONLY for autonomous (``--auto``) runs where no LLM investigates. Conservative —
    it never proposes ``custom`` and never picks ``sparser`` (it won't trade away accuracy
    on its own)."""
    dip = evidence.get("dip", {})
    contrast = dip.get("contrast")
    sigma = dip.get("near_black_sigma_de")
    overcoverage = evidence.get("gamut_overcoverage")
    raw_tone = evidence.get("raw_tone", {})
    mhc_white_de = (evidence.get("mhc_residual") or {}).get("white_de_vs_d65")

    shadow = "standard"
    reasons: list[str] = []
    if contrast is not None and contrast < _CONTRAST_SEVERE:
        shadow = "heavy"; reasons.append(f"severely raised black (~{contrast}:1)")
    elif (contrast is not None and contrast < _CONTRAST_RAISED) or \
         (sigma is not None and sigma >= _NEAR_BLACK_SIGMA_NOISY):
        shadow = "extra"; reasons.append("raised black / noisy near-black")

    volumetric = "standard"
    if (overcoverage is not None and overcoverage >= _OVERCOVERAGE_WIDE) or \
       (raw_tone.get("available") and raw_tone.get("bumpiness", 0.0) >= _TONE_NONLINEAR) or \
       (mhc_white_de is not None and mhc_white_de >= _MHC_RESIDUAL_NOTABLE):
        volumetric = "denser"; reasons.append("wide gamut / bumpy tone / ICC residual")

    return {
        "shadow_treatment": shadow,
        "volumetric_density": volumetric,
        "patch_size_overrides": {},
        "reason": ("conservative fallback (no LLM): " + "; ".join(reasons)) if reasons
                  else "conservative fallback (no LLM): panel well-behaved — defaults",
        "confidence": "low",
        "source": "fallback",
    }


# ---------------------------------------------------------------------------
# Decision validation (bounds = guard; the LLM can't break the run)
# ---------------------------------------------------------------------------

def _coerce_knob(name: str, value: Any) -> tuple[Optional[Any], Optional[str]]:
    """Coerce + clamp one override to its whitelisted bound. Returns (value, note); a
    non-whitelisted knob or an uncoercible value returns ``(None, reason)`` (dropped)."""
    if name not in KNOB_BOUNDS:
        return None, f"{name}: not an adaptable (shadow/volumetric) knob — dropped"
    bound = KNOB_BOUNDS[name]
    if isinstance(bound, set):
        if value in bound:
            return value, None
        return None, f"{name}={value!r}: not in {sorted(map(str, bound))} — dropped"
    lo, hi = bound
    if isinstance(value, bool):
        # float(True)==1.0 would silently coerce a bool to a nonsense count — drop it.
        return None, f"{name}={value!r}: bool not valid for a numeric knob — dropped"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None, f"{name}={value!r}: not numeric — dropped"
    is_int = isinstance(lo, int) and isinstance(hi, int)
    clamped = max(lo, min(hi, num))
    coerced = int(round(clamped)) if is_int else float(clamped)
    note = (f"{name}: clamped {value} → {coerced}" if float(coerced) != num else None)
    return coerced, note


def validate_decision(decision: dict[str, Any], base_patch_sizes: dict[str, Any]
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate an LLM (or fallback) structured decision into ``PatchSizes.merged`` knobs.

    Guards, never breaks: an unknown tier falls back to ``standard``; a non-whitelisted or
    out-of-bounds override is dropped/clamped (recorded in ``adjustments``). Returns
    ``(knobs, normalized)`` where ``knobs`` is the validated override set to apply and
    ``normalized`` is the decision actually used (with the audit trail)."""
    if not isinstance(decision, dict):
        decision = {}
    adjustments: list[str] = []

    shadow = decision.get("shadow_treatment", "standard")
    if shadow not in SHADOW_TIERS:
        adjustments.append(f"shadow_treatment={shadow!r} invalid → standard")
        shadow = "standard"
    volumetric = decision.get("volumetric_density", "standard")
    if volumetric not in VOLUMETRIC_TIERS:
        adjustments.append(f"volumetric_density={volumetric!r} invalid → standard")
        volumetric = "standard"
    confidence = decision.get("confidence", "low")
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "low"

    knobs: dict[str, Any] = {}
    knobs.update(shadow_knobs(shadow, base_patch_sizes))
    knobs.update(volumetric_knobs(volumetric, base_patch_sizes))

    raw_overrides = decision.get("patch_size_overrides") or {}
    clean_overrides: dict[str, Any] = {}
    if isinstance(raw_overrides, dict):
        for name, value in raw_overrides.items():
            coerced, note = _coerce_knob(name, value)
            if note:
                adjustments.append(note)
            if coerced is not None:
                clean_overrides[name] = coerced
    elif raw_overrides:
        adjustments.append("patch_size_overrides ignored — not an object")
    if volumetric != "custom" and clean_overrides:
        # Overrides are honoured outside 'custom' too, but flag the mismatch for the audit.
        adjustments.append("patch_size_overrides applied without volumetric_density=custom")
    knobs.update(clean_overrides)            # explicit overrides win over the tier presets

    normalized = {
        "shadow_treatment": shadow,
        "volumetric_density": volumetric,
        "patch_size_overrides": clean_overrides,
        "reason": str(decision.get("reason", "")),
        "confidence": confidence,
        "source": decision.get("source", "llm"),
        "knobs": knobs,
        "adjustments": adjustments,
    }
    return knobs, normalized


# ---------------------------------------------------------------------------
# Investigator CLI — the bounded read-only toolkit a paused LLM calls
# ---------------------------------------------------------------------------

def _rebuild_transfer(spec: dict[str, Any]) -> Any:
    from .engine.patches import Transfer
    if (spec or {}).get("kind") == "pq":
        return Transfer.pq(bit_depth=int(spec.get("bit_depth", 10)))
    return Transfer.power(gamma=float(spec.get("gamma", 2.2)),
                          peak_nits=float(spec.get("peak_nits", 120.0)),
                          bit_depth=int(spec.get("bit_depth", 10)))


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m dlc.patch_evidence --run <dir> --what <tool> [--override <json>] [--stage <s>]``.

    Reads the evidence packet the adaptive-planning seam persisted to
    ``<run>/adaptive_evidence.json`` and exposes the bounded investigator tools the paused
    LLM uses to decide the patch strategy. ``estimate-plan`` / ``compare-plans`` accept an
    ``--override`` JSON of knob changes to cost a hypothetical plan before committing."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="dlc.patch_evidence",
        description="Adaptive-planning evidence + investigator tools (read-only).")
    parser.add_argument("--run", type=Path, required=True, help="the run dir (with adaptive_evidence.json)")
    parser.add_argument("--what", default="evidence",
                        choices=["evidence", "dip", "raw-ti3", "estimate-plan",
                                 "compare-plans", "prior-runs", "stage-metrics"])
    parser.add_argument("--override", default=None,
                        help="JSON knob overrides for estimate-plan / compare-plans, e.g. '{\"tube_size\": 41}'")
    parser.add_argument("--stage", default=None, help="stage name for --what stage-metrics")
    args = parser.parse_args(argv)

    packet_path = Path(args.run) / "adaptive_evidence.json"
    if not packet_path.exists():
        print(json.dumps({"error": f"no adaptive_evidence.json in {args.run} "
                                   "(run with --adaptive-planning to the planning seam first)"}))
        return 2
    ev = json.loads(packet_path.read_text(encoding="utf-8"))

    def emit(obj: Any) -> int:
        print(json.dumps(obj, indent=2, default=str))
        return 0

    if args.what == "evidence":
        return emit(ev)
    if args.what == "dip":
        return emit(ev.get("dip"))
    if args.what == "raw-ti3":
        return emit(analyze_raw_ti3(ev.get("raw_ti3_path")) if ev.get("raw_ti3_path")
                    else ev.get("raw_tone"))
    if args.what == "prior-runs":
        return emit(ev.get("prior_runs"))
    if args.what == "stage-metrics":
        if not args.stage:
            return emit({"error": "--stage is required for --what stage-metrics"})
        return emit(inspect_stage_metrics(args.run, args.stage))

    # estimate-plan / compare-plans: rebuild the transfer + base sizes, cost a hypothetical.
    transfer = _rebuild_transfer(ev.get("transfer", {}))
    base = dict(ev.get("base_patch_sizes", {}))
    flow = ev.get("flow", "full")
    try:
        override = json.loads(args.override) if args.override else {}
    except ValueError as exc:
        return emit({"error": f"invalid --override JSON: {exc}"})
    proposed = {**base, **override}
    if args.what == "estimate-plan":
        return emit(estimate_patch_plan(proposed, transfer, flow=flow,
                                        read_overhead_s=ev.get("dip", {}).get("read_overhead_s")))
    base_plan = estimate_patch_plan(base, transfer, flow=flow,
                                    read_overhead_s=ev.get("dip", {}).get("read_overhead_s"))
    prop_plan = estimate_patch_plan(proposed, transfer, flow=flow,
                                    read_overhead_s=ev.get("dip", {}).get("read_overhead_s"))
    return emit({"base": base_plan, "proposed": prop_plan,
                 "delta": compare_patch_plans(base_plan, prop_plan)})


if __name__ == "__main__":  # pragma: no cover - CLI wiring
    import sys
    raise SystemExit(main(sys.argv[1:]))
