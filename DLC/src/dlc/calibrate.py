"""The scripted calibration orchestrator (v2-design-notes §1,3,4,5,7,11; item 5).

The v2 pivot: a **deterministic scripted core owns ALL the mechanics** (display
mapping, patch sets, measurement sequencing, the loops, integrity gates, LUT
generation) and a **thin LLM sits only at the seams** — it never tails a stream,
it judges *digests* at boundaries. This module is that core.

A run is a **named flow** (``full`` / ``3dlut-only`` / ``mhc-only``; ``hdr`` is the
later goal) expressed as an ordered list of stage methods. The pipeline is
**MHC ICC (matrix + 1D base + closed-loop D65 grayscale refine) → 3D LUT**: the MHC
is a STANDALONE D65 foundation that owns the neutral axis (matrix = native→D65, base
1D LUT = native-white tone, the closed-loop refine = the per-level D65 residual), and
the 3D LUT does the volumetric/colour refinement on top (1+1+1 layering). The former
post-3D-LUT GS+WB tweak is removed — it re-corrected the MHC-owned neutral a 3rd time.

**The LLM seam = the** :class:`Adjudicator`. At each ``⚑`` point the core hands the
adjudicator a structured :class:`AdjudicationRequest` (a digest + a question + the
allowed choices + the *core's recommendation*) and gets back a :class:`Decision`.

* :class:`AutoAdjudicator` rubber-stamps the recommendation → the whole flow runs
  to completion in one process (tests, ``--simulate``, CI). Use it where there is
  genuinely no LLM/human to consult and a deterministic, reproducible run is the
  point — NOT for an unattended *hardware* run, where a safety-critical seam should
  reach a judge (see below).
* :class:`MappingAdjudicator` answers from a decisions map and **raises**
  :class:`AdjudicationRequired` on the first un-decided seam → the live LLM-driven
  pause/resume model: the CLI catches it, emits the digest+question, the LLM
  decides, and re-running with that decision recorded fast-forwards (every completed
  stage is **memoised** in the run-record, so measurements are never repeated) to
  the seam and proceeds. The memoisation also gives free crash-recovery.
* :class:`SupervisedAdjudicator` is the middle ground for an *unattended hardware*
  run: it auto-accepts **benign** recommendations (a clean run never pauses) but
  **escalates safety-critical seams to the LLM** — exactly when the core's own
  recommendation turns non-benign (``abort``/``revert``/``retry``/…) or the digest
  flags a severe/critical state. This is the answer to "the overnight run had no LLM
  at the seams": auto-mode is *safe* only if recommendations are conservative, but
  supervised-mode is *judged* at the boundaries that matter.

**Where the LLM judges (the seam) vs what the core decides (mechanics).** *Detecting*
an anomaly — a collapsed post-foundation luminance envelope, an optimizer floor, a
failed verify — is mechanics and stays deterministic. *Deciding what to do about it*
(abort / retry the foundation / accept and continue) is a **boundary**, so it goes
through :meth:`Calibration.adjudicate` with a conservative recommendation, never a
unilateral ``raise`` that the LLM can't see. The core surfaces a strong default; the
judge gets the final call (and full digest) when one is present.

The display/meter and the re-measure probe are single injectable seams (a
:data:`~dlc.measure_loop.MeasureFn` and a :data:`~dlc.optimize.ProbeFn`), so the
orchestrator itself touches neither a display nor a meter and runs deterministically
in tests against a :class:`~dlc.measure_loop.SyntheticPanel` + the in-process mock
controller.

Engine-tier (imports :mod:`dlc.optimize` → numpy/scipy/colour); the dependency-free
spine never imports it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from argparse import Namespace
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

import numpy as np

from . import calibration_profile as cp
from .characterize import CharacterizeConfig, run_characterization
from .controller import CalibrationController, normalize_mode
from .correction_store import CorrectionRecord, CorrectionStore
from . import gamut
from .dip import DipStore, DisplayInstrumentProfile
from .engine.patches import (
    Transfer,
    cube_patches,
    gamut_patches,
    near_neutral_tube_patches,
    ramp_patches,
    shadow_levels,
    sort_patches,
    target_anchor_patches,
    tube_patches,
    uniform_levels,
)
from .events import Ev, EventWriter, RunLog
from .liveness import Liveness, RunCancelled, RunStalled
from .measure_loop import (
    MeasureFn,
    MeasureLoopConfig,
    MeasurePatch,
    MeasureLoopResult,
    Reading,
    run_measure_loop,
)
from .decisions import hdr_metric_thresholds
from .metrics import percentile, score_samples, score_samples_hdr, summarize_metrics
from .mhc import SRGB_PRIMARIES, parse_ti3
from .optimize import (DegenerateMeasurements, OptimizeConfig, ProbeFn, SDR_CORRECTION_CAP,
                       optimize_cube)
from . import patch_evidence
from .paths import RUNS_DIR, atomic_write_text
from .runs import RunContext, create_run, open_run
from .stages import _common, build_mhc

__all__ = [
    "Decision",
    "AdjudicationRequest",
    "AdjudicationRequired",
    "Adjudicator",
    "AutoAdjudicator",
    "MappingAdjudicator",
    "SupervisedAdjudicator",
    "StageOutcome",
    "CalibrationResult",
    "Calibration",
    "FLOWS",
    "run_calibration",
    "descriptive_cube_name",
]

# Stable seam ids — the ``⚑`` points where the LLM judges (v2-design-notes §5).
SEAM_PLAN = "plan_veto"            # state the resolved flow + target; allow veto
SEAM_SPD = "spd_staleness"         # correction past policy → tell, don't ask (§10)
SEAM_PROBE_MATCH = "probe_match"   # build-correction: operator runs ccxxmake at the box (§10)
SEAM_BRIGHTNESS = "brightness"     # white luminance to target (human turns OSD)
SEAM_MEASURE = "measure"           # loop didn't settle / unresolved patches (§6)
SEAM_OPTIMIZE = "optimize_floor"   # physical floor / budget-limited points (§7)
SEAM_VERIFY = "verify"             # final score vs quality targets
SEAM_STACK = "require_stack"       # 3dlut-only precondition unmet
SEAM_CHARACTERIZE = "characterize" # characterization surfaced abnormal panel/meter behaviour
SEAM_PLANNING = "adaptive_planning" # opt-in LLM patch-strategy investigation seam (§6a; #47/#49)
SEAM_FOUNDATION = "foundation_collapse"  # a foundation install collapsed bright-neutral luminance (§7)
SEAM_HARDWARE_READY = "hardware_readiness"  # one live gate before the first meter/presenter read
SEAM_MONITOR_MAP = "monitor_map"   # profile monitor↔Argyll↔panel map disagrees with live enumeration
_D65_XY = (0.3127, 0.3290)          # the standard-source white the MHC matrix maps the panel to
# NOTE: there is deliberately NO check-in seam. A §12 check-in is a NON-BLOCKING evidence packet
# for the LLM (see _maybe_timed_checkin), never an adjudicated yes/no — it must never gate the spine.

# The benign, happy-path choice at a seam — the recommendation a clean run carries. A
# recommendation OUTSIDE this set means the deterministic core wants to stop/redo something.
# CAUTION (Design Law, see module header): "benign" means *the core has a sensible default*,
# NOT *no LLM needed*. A benign ``accept`` on a passing verify is still a judgment the LLM must
# make. ``SupervisedAdjudicator`` currently auto-accepts these — the known divergence Task #1
# fixes (escalate benign judgment seams to the LLM). The default ``MappingAdjudicator`` already
# routes every seam, benign or not, to the LLM. (Check-ins are NOT seams — they are non-blocking
# evidence packets the LLM consumes out of band; see ``_maybe_timed_checkin``.)
_BENIGN_RECOMMENDATIONS = frozenset({"approve", "proceed", "accept", "apply", "done", "continue"})
# Digest flags that, even under a benign recommendation, mark a seam worth a judge's eyes.
# ``gate_failed`` is the verify gate's "outside quality targets" signal: a benign ``apply``
# recommendation on a FAILED verify must still escalate (the recommendation alone is benign, so
# without this flag SupervisedAdjudicator would silently apply a sub-quality calibration).
_SEVERITY_FLAGS = ("severe_floor", "severe_failure", "foundation_critical", "critical",
                   "compromised", "gate_failed", "read_anomaly", "score_anomaly")


# ---------------------------------------------------------------------------
# Adjudication — the LLM seam
#
# DESIGN LAW (do not regress): DLC is not a scripted program — it is scripts tied
# together by a spine the LLM adjudicates. There is NO headless/unattended/autonomous
# hardware run; every run is LLM-overseen throughout. Anything that is not a
# 100%-deterministic yes/no goes to the LLM to decide or to raise with the user; only
# provably-mechanical facts stay in code. Auto-accepting / rubber-stamping / silently
# logging a non-trivial decision is a VIOLATION. A "benign" recommendation is a default
# the LLM may take, not a licence for code to skip the LLM. ``AutoAdjudicator`` is sim/CI
# ONLY. The ``SupervisedAdjudicator`` benign-auto-accept below is a KNOWN DIVERGENCE from
# this law (it silently accepts judgment seams like a passing verify); the fix — escalate
# those to the LLM — is tracked as Task #1.
#
# CHECK-INS ARE NOT SEAMS. A §12 check-in (``_maybe_timed_checkin`` / the measure-loop
# quartile ping) NEVER pauses the spine and carries NO recommendation. It is the spine
# collecting the evidence since the last check-in — warnings, the max ΔE actually read,
# re-read/repeated patches, non-stopper anomalies — and emitting it for the LLM to JUDGE
# ("ΔE high but that's the panel limit"; "re-read twice but the latest is normal → self-
# corrected"). The LLM consumes it from the running (background) spine and intervenes only
# if it sees a real problem. Run-stoppers are the adjudicated seams above; check-ins are
# pure data for LLM intelligence — the point of DLC, not a deterministic program.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """The judgment returned at a seam. ``choice`` is one of the request's
    ``options``; ``note`` carries the LLM's reasoning for the audit trail.

    ``payload`` carries a **structured decision** for seams that need more than a
    one-of-N choice — notably the adaptive-planning seam, where the LLM returns a
    full patch strategy (shadow/volumetric tiers + validated overrides). It is
    persisted in the decision record and replayed verbatim on resume, so the run
    re-applies the exact plan the LLM chose."""

    choice: str
    note: Optional[str] = None
    payload: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"choice": self.choice, "note": self.note}
        if self.payload is not None:
            d["payload"] = self.payload
        return d


@dataclass(frozen=True)
class AdjudicationRequest:
    """What the core hands the LLM at a seam — a digest, a question, the allowed
    choices, and the core's own recommendation (so :class:`AutoAdjudicator` can
    rubber-stamp it and a human/LLM has a sensible default)."""

    key: str                       # stable, unique per occurrence (stage-scoped)
    seam: str                      # the seam type (SEAM_*)
    stage: str
    question: str
    options: tuple[str, ...]
    recommendation: str
    digest: dict[str, Any] = field(default_factory=dict)
    # The structured fallback the core would apply if no LLM answers (e.g. the
    # conservative patch-strategy planner). AutoAdjudicator returns it verbatim, so
    # an autonomous run still gets the structured decision the seam expects.
    recommended_payload: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        d = {"key": self.key, "seam": self.seam, "stage": self.stage,
             "question": self.question, "options": list(self.options),
             "recommendation": self.recommendation, "digest": self.digest}
        if self.recommended_payload is not None:
            d["recommended_payload"] = self.recommended_payload
        return d


class AdjudicationRequired(Exception):
    """Raised by :class:`MappingAdjudicator` when a seam has no recorded decision —
    the pause point of the live LLM-driven run."""

    def __init__(self, request: AdjudicationRequest) -> None:
        super().__init__(f"adjudication required at seam {request.key!r}: {request.question}")
        self.request = request


class Adjudicator(Protocol):
    """How a seam is answered. Three implementations, selected by CLI flag — note the trap
    that the one you want for a real run has NO flag (it's the default). See ../docs/NAMING.md §5.

        CLI flag        class                   use
        (none)          MappingAdjudicator      LIVE hardware — every seam pauses for the LLM
        --auto          AutoAdjudicator         sim/CI rubber-stamp only
        --supervised    SupervisedAdjudicator   benign-auto-accept (known divergence, Task #1)
    """

    def adjudicate(self, request: AdjudicationRequest) -> Decision: ...


class AutoAdjudicator:
    """**CLI: ``--auto``.** Deterministic: take the core's recommendation. **Sim/CI ONLY —
    never a hardware run** (see the Design Law above). It consults no LLM, so using it on
    hardware is a headless run, which by design does not exist. For a real run use
    ``MappingAdjudicator`` (every seam reaches the LLM)."""

    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        return Decision(request.recommendation, note="auto: accepted core recommendation",
                        payload=request.recommended_payload)


class MappingAdjudicator:
    """**CLI: the DEFAULT (neither ``--auto`` nor ``--supervised``).** Answer from a decisions
    map; **raise** on the first un-decided seam. This is the real hardware mode.

    The live LLM pause/resume seam: seed it with the decisions made so far (loaded
    from the run-record on resume); the first seam without a recorded decision
    raises :class:`AdjudicationRequired`, which the CLI surfaces to the LLM."""

    def __init__(self, decisions: Optional[dict[str, Decision]] = None) -> None:
        self.decisions = dict(decisions or {})

    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        if request.key in self.decisions:
            return self.decisions[request.key]
        raise AdjudicationRequired(request)


class SupervisedAdjudicator:
    """**CLI: ``--supervised``.** Escalate non-benign seams to a live judge; auto-accept benign ones.

    **KNOWN DIVERGENCE from the Design Law (see the module header + Task #1).** This was
    conceived as the mode for an "unattended hardware run" — but per the law *there is no
    unattended run*. A benign ``continue``/``accept`` is still a judgment the LLM must make,
    so silently auto-accepting benign **judgment** seams (timed check-ins, a passing verify)
    is the bug the law corrects: those must reach the LLM, not land silently on the spine.

    What it does today: a recorded decision replays; otherwise a seam **raises**
    :class:`AdjudicationRequired` only when the core's recommendation is non-benign
    (``abort``/``revert``/``retry``/…) or the digest flags a severe/critical state — every
    *benign* seam is auto-accepted. That half is correct (it closes the gap that sank the
    first HDR run, where ``--auto`` plowed through a foundation collapse for hours); the
    other half (auto-accepting benign judgment seams) is what Task #1 makes escalate.

    Until Task #1 lands, prefer the default ``MappingAdjudicator`` for a hardware run so
    every seam genuinely reaches the LLM. Seed with decisions-so-far (loaded on resume) so a
    recorded judgment replays verbatim and only a genuinely new seam pauses."""

    def __init__(self, decisions: Optional[dict[str, Decision]] = None) -> None:
        self.decisions = dict(decisions or {})

    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        if request.key in self.decisions:
            return self.decisions[request.key]
        if self._needs_a_judge(request):
            raise AdjudicationRequired(request)
        return Decision(request.recommendation,
                        note="supervised: auto-accepted benign recommendation",
                        payload=request.recommended_payload)

    @staticmethod
    def _needs_a_judge(request: AdjudicationRequest) -> bool:
        """A seam escalates when the core's own recommendation is non-benign, or the
        digest carries a severity flag even under a benign default."""
        if request.recommendation not in _BENIGN_RECOMMENDATIONS:
            return True
        digest = request.digest or {}
        return any(bool(digest.get(flag)) for flag in _SEVERITY_FLAGS)


# ---------------------------------------------------------------------------
# Stage / run results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PatchSizes:
    """Patch-set sizes AND sequence knobs per stage — the user/agent's lever over the
    run's time/size. Every field maps onto the ported ColorCalibration generator
    (:mod:`dlc.engine.patches`); the defaults reproduce the original preset. Override
    per-run via the CLI patch flags, or durably per-display via the profile's
    ``patches:`` block (CLI wins over profile). So a run is never stuck with a preset:
    a quick shakedown and a dense overnight pass are both just different sizes here.

    All sets are drift-ordered by ``order`` (``thermal`` by default)."""

    # raw ramp = the MHC FOUNDATION set (matrix + base 1D). The MHC fit consumes the grey ramp
    # (base grayscale 1D) + the R/G/B ramps (per-channel curves + primaries); it cannot fit the
    # C/M/Y secondaries, so they're EXCLUDED here by default and left to the volumetric 3D-LUT set.
    raw_ramp_steps: int = 32        # steps per channel (grey + R/G/B): >=32 ⇒ a dense neutral + per-channel foundation
    raw_saturations: tuple[float, ...] = (1.0,)   # primary saturation shells (breadth)
    raw_include_secondaries: bool = False   # add C/M/Y ramps too? Off ⇒ grey + R/G/B only (the foundation)
    raw_spacing: str = "uniform"    # uniform | perceptual (even-signal vs even-perceptual)

    # near-neutral tube (MHC FOUNDATION): off-axis samples around the grey axis (R≠G≠B but close
    # to neutral) along the six hue directions. Characterizes the OFF-AXIS non-additivity / white-
    # balance region the matrix + per-channel 1D LUT correct THROUGH — which the grey diagonal +
    # per-channel ramps cannot reveal. Off by default (0); the ICC-characterization sequence turns
    # it on. DIP-independent (on-axis/near-neutral is in-gamut for any panel).
    icc_tube_levels: int = 0        # grey anchor levels for the tube (0 ⇒ no tube)
    icc_tube_offsets: tuple[float, ...] = (0.06, 0.15)   # chroma offsets as a fraction of the level

    # volumetric set (3D-LUT build, post-MHC). ``tube`` mode hits all three goals at once: the
    # CUBE covers the ENTIRE gamut (boundary anchoring), the neutral TUBE concentrates density on
    # the practical near-neutral region where content lives, and the full-resolution GREY AXIS gives
    # the grayscale its own dense sampling. The 3D-LUT thus optimises the whole volume while focusing
    # where it matters (denser samples ⇒ more optimiser attention there).
    volumetric_mode: str = "tube"   # tube | cube | gamut
    cube_size: int = 9              # volumetric cube axis (entire-gamut coverage)
    tube_size: int = 33             # neutral-axis + tube-core resolution (grayscale + practical density)
    tube_radius: int = 2            # Manhattan radius of the neutral tube (practical near-neutral region)
    grid_type: str = "cub"          # cub | bcc (tube/cube)
    spines: bool = False            # tube: add RGBCMY gamut-edge spines (saturated edges — mostly clip/rare,
    #                                 so off by default; the cube already anchors the gamut corners)
    gamut_lum_steps: int = 17       # gamut mode: luminance axis
    gamut_hues: int = 12            # gamut mode: hue angles per shell
    gamut_lum_bias: float = 1.3     # gamut mode: shadow density bias

    # verify = a LIGHTER sanity set (not the dense build set): grey + RGBCMY at full + half saturation
    # — confirms grayscale tracking + the gamut hues at practical & saturated levels, normal-sized.
    verify_steps: int = 13          # verify ramp steps per channel
    verify_saturations: tuple[float, ...] = (1.0, 0.5)   # saturated + practical mid-saturation
    # verify floors COLOUR above the shadow band (normalized signal): sub-nit chroma is
    # meaningless to meter + panel, so the grayscale toe (low_light_steps) carries the EOTF
    # there and colour ramps start above it. Just above low_light_signal so the two don't overlap.
    verify_color_min_signal: float = 0.25

    # grey-axis ramp measured by the MHC closed-loop D65 grayscale refine (writes the
    # MHC correctionGrayscale layer — see ../docs/NAMING.md §2). NOT the removed overlay
    # GS+WB tweak. This is a count of grey PATCHES to measure, independent of the MHC
    # curve's point count (which the C++ side constrains to {10,20,32}).
    neutral_steps: int = 17         # grey-axis ramp steps

    # additive shadow density: preserve ordinary whole-range anchors, then add extra low-light
    # samples where the eye is most sensitive and the meter/panel are most nonlinear.
    low_light_steps: int = 9         # extra ramp/tube-axis levels inside the shadow band
    low_light_cube_size: int = 5     # small dark mini-cube for 3D-LUT build coverage
    low_light_signal: float = 0.20   # shadow band upper bound as normalized signal
    low_light_bias: float = 2.0      # >1 packs extra levels toward black

    # ordering for every stage (drift prevention)
    order: str = "thermal"          # thermal | luminance | random

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> "PatchSizes":
        """Build from a profile ``patches:`` block — only known keys, coerced to the
        field types (saturations → tuple of floats); unknown keys are ignored."""
        d = dict(raw or {})
        kw: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in d or d[f.name] is None:
                continue
            v = d[f.name]
            if f.name in ("raw_saturations", "verify_saturations", "icc_tube_offsets"):
                kw[f.name] = tuple(float(x) for x in v)
            elif f.name in ("spines", "raw_include_secondaries"):
                kw[f.name] = bool(v)
            elif f.name in ("gamut_lum_bias", "low_light_signal", "low_light_bias",
                            "verify_color_min_signal"):
                kw[f.name] = float(v)
            elif f.name in ("raw_spacing", "volumetric_mode", "grid_type", "order"):
                kw[f.name] = str(v)
            else:
                kw[f.name] = int(v)
        return cls(**kw)

    def merged(self, **overrides: Any) -> "PatchSizes":
        """Return a copy with only the **non-None** overrides applied (CLI flags that
        were actually passed). ``raw_saturations`` is coerced to a tuple."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        for sat_key in ("raw_saturations", "verify_saturations", "icc_tube_offsets"):
            if sat_key in clean:
                clean[sat_key] = tuple(float(x) for x in clean[sat_key])
        return replace(self, **clean)


@dataclass
class StageOutcome:
    stage: str
    status: str                    # done | escalated | aborted
    digest: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)   # JSON-friendly handoff
    artifacts: list[str] = field(default_factory=list)

    def as_record(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "digest": self.digest,
                "data": self.data, "artifacts": self.artifacts}

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "StageOutcome":
        return cls(stage=rec["stage"], status=rec["status"], digest=rec.get("digest", {}),
                   data=rec.get("data", {}), artifacts=rec.get("artifacts", []))


@dataclass
class CalibrationResult:
    flow: str
    monitor: int
    mode: str
    target: Optional[str]
    status: str                    # completed | escalated | aborted
    stages: list[str]
    results_dir: Optional[str]
    report_path: Optional[str]
    digest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"flow": self.flow, "monitor": self.monitor, "mode": self.mode,
                "target": self.target, "status": self.status, "stages": self.stages,
                "results_dir": self.results_dir, "report_path": self.report_path,
                "digest": self.digest}


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------

class CalibrationAborted(Exception):
    """A flow ended early at the core's own invariant (e.g. 3dlut-only with no MHC
    stack present, or HDR in an SDR-first build). Carries the partial result."""

    def __init__(self, outcome: StageOutcome) -> None:
        super().__init__(outcome.digest.get("message", outcome.stage))
        self.outcome = outcome


def _reading_xy(reading: Any) -> Optional[list[float]]:
    """The (x, y) chromaticity of a meter reading for the spine, from Yxy when present
    else derived from XYZ. ``None`` for a failed/black read (the dashboard skips it)."""
    yxy = getattr(reading, "yxy", None)
    if yxy is not None:
        return [round(yxy[1], 5), round(yxy[2], 5)]
    xyz = getattr(reading, "xyz", None)
    if xyz is not None and sum(xyz) > 0:
        tot = sum(xyz)
        return [round(xyz[0] / tot, 5), round(xyz[1] / tot, 5)]
    return None


def resolve_run_spec(ctx: RunContext, state: Mapping[str, Any], *, mode: str,
                     bit_depth: Optional[int]
                     ) -> tuple[str, Optional[int], list[dict[str, Any]]]:
    """Reconcile the requested (mode, bit_depth) against the PERSISTED run record.

    A run's mode/bit_depth are fixed when it is CREATED; on every later invocation (a
    resume after an adjudication seam) the CLI args default back to SDR/8-bit and must NOT
    silently override the persisted spec — doing so mislabels every digest AND (because
    self.mode drives stage_resolve_target) re-resolves the WRONG target onto a fresh run.
    So the persisted record is authoritative: ``manifest.mode`` (the immutable run mode,
    mirrored as ``state['mode']``) and the persisted ``bit_depth``.

    Returns ``(mode, bit_depth, conflicts)``. ``bit_depth`` is the persisted value (resume)
    or the explicit arg (fresh + ``--bit-depth``), or ``None`` when there is nothing to
    restore/override — the caller then applies its OWN fresh-run default (the orchestrator's
    panel depth vs main()'s ``10 if HDR else 8`` differ, and unifying them here would change
    live behavior). ``conflicts`` lists each field the args disagreed with (never silent)."""
    conflicts: list[dict[str, Any]] = []
    arg_mode = normalize_mode(mode)
    manifest_mode = getattr(getattr(ctx, "manifest", None), "mode", None)
    persisted_mode = normalize_mode(manifest_mode) if manifest_mode else (
        normalize_mode(state["mode"]) if state.get("mode") else None)
    eff_mode = persisted_mode or arg_mode
    if persisted_mode and persisted_mode != arg_mode:
        conflicts.append({"field": "mode", "requested": arg_mode, "persisted": eff_mode})

    persisted_bd = state.get("bit_depth")
    if persisted_bd is None:
        persisted_bd = (state.get("calib") or {}).get("bit_depth")
    if persisted_bd is not None:
        eff_bd: Optional[int] = int(persisted_bd)
        if bit_depth is not None and int(bit_depth) != eff_bd:
            conflicts.append({"field": "bit_depth", "requested": int(bit_depth), "persisted": eff_bd})
    elif bit_depth is not None:
        eff_bd = int(bit_depth)
    else:
        eff_bd = None             # nothing persisted/explicit → caller keeps its own default
    return eff_mode, eff_bd, conflicts


def resolve_run_flow(state: Mapping[str, Any], flow: str) -> tuple[str, Optional[dict[str, Any]]]:
    """Reconcile the requested flow against the persisted ``calib.flow`` (the flow chosen
    when the run started). On resume the CLI ``--flow`` defaults to ``full`` and must not
    overwrite the persisted flow. Returns ``(flow, conflict | None)``."""
    persisted_flow = (state.get("calib") or {}).get("flow")
    if persisted_flow and flow and persisted_flow != flow:
        return persisted_flow, {"field": "flow", "requested": flow, "persisted": persisted_flow}
    return (persisted_flow or flow), None


class Calibration:
    """One calibration run: a flow over a monitor/mode, driving the injected
    controller + measure/probe seams + adjudicator, memoising every stage in the
    run-record (``dlc_state.json['calib']``)."""

    def __init__(
        self,
        *,
        ctx: RunContext,
        profile: cp.Profile,
        monitor: int,
        mode: str,
        controller: CalibrationController,
        measure: MeasureFn,
        adjudicator: Adjudicator,
        probe: Optional[ProbeFn] = None,
        bit_depth: Optional[int] = None,
        loop_config: Optional[MeasureLoopConfig] = None,
        optimize_config: Optional[OptimizeConfig] = None,
        characterize_config: Optional[CharacterizeConfig] = None,
        run_date: Optional[date] = None,
        force: bool = False,
        dummy_icc: str = "sRGB.icm",
        patch_sizes: Optional[PatchSizes] = None,
        white_fn: Optional[cp.WhiteFn] = None,
        probe_launcher: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
        decision_overrides: Optional[dict[str, "Decision"]] = None,
        adaptive_planning: bool = False,
        stall_kill_hook: Optional[Callable[[], None]] = None,
        pause_handler: Optional[Callable[[Mapping[str, Any]], None]] = None,
        enable_watchdog: bool = False,
        checkin_interval_s: float = 600.0,
        require_hardware_readiness: bool = False,
        neutral_min_reads: Optional[int] = None,
        neutral_chroma_span: Optional[float] = None,
        neutral_floor_min_nits: Optional[float] = None,
        dark_min_reads: Optional[int] = None,
        dark_floor_max_nits: Optional[float] = None,
    ) -> None:
        self.ctx = ctx
        self.profile = profile
        self.monitor = monitor
        # Provisional — reconciled against the persisted run record once _state is loaded
        # below (a resume must not let the CLI default override the run's fixed mode).
        self.mode = normalize_mode(mode)
        self.controller = controller
        self.measure = measure
        self.adjudicator = adjudicator
        self._probe = probe
        self.display = profile.display_for(monitor)
        # Provisional — reconciled against the persisted run record below (resolve_run_spec),
        # alongside self.mode, so a resume restores the run's fixed bit depth.
        self.bit_depth = bit_depth if bit_depth is not None else self.display.panel.bit_depth
        self.loop_config = loop_config
        # Near-neutral read FLOOR: guarantee the chroma-critical grey-ramp+tube region is averaged
        # (the matrix/WB/non-additivity derivation is sensitive to single-read chromaticity noise
        # there). Folded into the DIP-derived loop config in `_loop_config_for`; the DIP still
        # escalates above the floor. None ⇒ leave the MeasureLoopConfig default (off).
        self.neutral_min_reads = neutral_min_reads
        self.neutral_chroma_span = neutral_chroma_span
        self.neutral_floor_min_nits = neutral_floor_min_nits
        # Dark near-neutral read FLOOR: take several reads on dim near-neutral patches so their
        # read-to-read CHROMATICITY spread can be estimated — that spread drives the dark-level trust
        # (how much to smooth a dark correction to identity). None ⇒ MeasureLoopConfig default (off).
        self.dark_min_reads = dark_min_reads
        self.dark_floor_max_nits = dark_floor_max_nits
        self.optimize_config = optimize_config or OptimizeConfig()
        self.characterize_config = characterize_config
        self.run_date = run_date or date.today()
        self.force = force
        self.dummy_icc = dummy_icc
        self.patch_sizes = patch_sizes or PatchSizes()
        self._white_fn = white_fn
        # Opt-in: the LLM patch-strategy investigation seam (#47/#49). OFF ⇒ the deterministic
        # patch plan, no seam, no evidence gathering (an ordinary run is unchanged).
        self.adaptive_planning = adaptive_planning
        # Explicit per-key decision overrides (the CLI's --decide flags). Unlike the
        # adjudicator's seed map, these take precedence over an ALREADY-recorded decision, so
        # a resumed run can change a recorded seam (e.g. verify:accept apply↔revert) without
        # --force re-measuring everything. See adjudicate().
        self.decision_overrides: dict[str, Decision] = dict(decision_overrides or {})
        # The correction-build launches Argyll ccxxmake in its own console (live only); an
        # injectable seam keeps tests/sim from spawning a real process.
        self._probe_launcher = probe_launcher or self._default_launch_ccxxmake
        self._pause_handler = pause_handler
        self.require_hardware_readiness = require_hardware_readiness

        # ---- The run-record state (dlc_state.json) — the calibration's persisted memory -------
        # Two levels. `self._state` is the top-level run-record; `self.calib` (its "calib" sub-dict)
        # is the orchestrator's own memo store. A resume reloads this and fast-forwards over any
        # stage whose record is present (memoisation = crash-recovery + pause/resume).
        #
        #   self._state (top level)                  written by
        #     monitor / mode / bit_depth ........... this ctor (the run's fixed spec)
        #     mhc_params ........................... build/refine MHC stages (the matrix + 1D params)
        #     correction_grayscale ................. the D65 refine (point_count + per-channel devs)
        #     score_history ........................ stage_score (append-only verify/intermediate scores)
        #     refine_history ....................... refine-grayscale stage (per-round residuals)
        #     stages_emitted ....................... _common (stage start/done log for the state tool)
        #     calib ⌄ .............................. this orchestrator (below)
        #
        #   self.calib (the "calib" sub-dict)
        #     stages ............................... {stage_key: StageOutcome.as_record()} — the memo
        #     decisions ............................ {seam_key: Decision.as_dict()} — recorded --decide
        #     flow / target ........................ the resolved flow name + target name
        #     white ................................ resolve_white() result (xy + provenance)
        #     patch_plan ........................... the approved patch plan + its fingerprint
        #     hdr_target ........................... the resolved HDR target (peak/curve)
        #     backup ............................... the pre-run durable settings backup (for rollback)
        #     inplace_baseline ..................... 3dlut-only rollback baseline (prior cube_path)
        #     adaptive_plan ........................ opt-in --adaptive-planning decision
        #     checkin_seq .......................... monotonic check-in counter
        self._state = _common.load_dlc_state(ctx)
        self.calib: dict[str, Any] = self._state.setdefault("calib", {})
        self.calib.setdefault("stages", {})
        self.calib.setdefault("decisions", {})
        self.target_name: Optional[str] = self.calib.get("target")
        # Reconcile mode + bit depth against the persisted run record: a resume's CLI args
        # default to SDR/8-bit and must NOT override the run's fixed spec (which both
        # mislabels every digest and re-resolves the wrong target). The persisted record
        # wins; a disagreement is recorded and surfaced in run() (never silently switched).
        self.mode, _eff_bd, self._spec_conflicts = resolve_run_spec(
            ctx, self._state, mode=mode, bit_depth=bit_depth)
        # eff_bd is None only when nothing was persisted/explicit → keep the long-standing
        # constructor default (the panel's native depth). main() applies its own fresh-run
        # default (10 if HDR else 8) and passes the resolved value in, so the two never diverge.
        self.bit_depth = _eff_bd if _eff_bd is not None else self.display.panel.bit_depth

        # The unified event spine: every phase change, stage boundary, seam, and (via the
        # measure loop / optimizer) every patch read + heartbeat lands in events.jsonl, the
        # one log the dashboard tails and the LLM reads (as a digest projection). This is
        # what makes a run's liveness visible — its absence is why the 53-min stall hid.
        self.runlog = RunLog(ctx.events_path)
        self._last_header: dict[str, Any] = {}   # change-detection so the header isn't re-spammed
        # The self-acting stall guard (§12). The checkpoint guard always runs (cheap, thread-free);
        # the watchdog thread is opt-in (live runs set enable_watchdog) so tests don't spin threads.
        # stall_kill_hook force-kills a wedged meter/presenter so the watchdog can unblock a main
        # thread stuck in a syscall (the CLI wires it to the persistent meter + presenter).
        # The watchdog also polls control.json (off the main thread) so an LLM/operator can
        # CANCEL a run it's watching — the actionable half of mid-run gating. A latched cancel
        # becomes a clean RunCancelled abort at the next checkpoint.
        self.liveness = Liveness(self.runlog, on_stall=stall_kill_hook,
                                 control_check=self._control_on_disk,
                                 on_pause=self._pause_requested,
                                 on_resume=self._resume_requested)
        self._enable_watchdog = enable_watchdog
        # §12 timed check-in: a coarse wall-clock floor (0 disables) past which the next safe
        # checkpoint (a stage boundary, an optimizer iteration) surfaces a rich "status — continue?"
        # so a multi-hour run never goes dark. monotonic so it is immune to wall-clock changes;
        # reset on each resume (a fresh process), which is fine — a resume is itself a status point.
        self._checkin_interval_s = max(0.0, float(checkin_interval_s))
        self._last_checkin_monotonic: Optional[float] = None
        self._last_checkin_tally: dict[str, int] = {}
        self._last_checkin_pos: int = 0   # events.jsonl byte offset at the last check-in (evidence window)
        self._run_started_monotonic: Optional[float] = None
        # Latest live metrics, snapshotted as they happen, so a check-in carries them without
        # re-deriving from artifacts: the most recent intermediate score + the last optimizer iter.
        self._last_scored: dict[str, Any] = {}
        self._last_optimizer: dict[str, Any] = {}
        self._last_refine: dict[str, Any] = {}

    # -- persistence ------------------------------------------------------
    def _save(self) -> None:
        self._state["calib"] = self.calib
        self._state.setdefault("monitor", self.monitor)
        self._state.setdefault("mode", self.mode)
        # Persist the resolved bit depth so a resume restores it instead of re-deriving from
        # the CLI default (the run spec must survive across invocations — see resolve_run_spec).
        self._state.setdefault("bit_depth", self.bit_depth)
        _common.save_dlc_state(self.ctx, self._state)

    # -- cooperative cancel (the actionable half of mid-run gating) --------
    def _control_path(self) -> Path:
        return self.ctx.root / "control.json"

    def _control_on_disk(self) -> Optional[Mapping[str, Any]]:
        try:
            p = self._control_path()
            if not p.exists():
                return None
            ctrl = json.loads(p.read_text(encoding="utf-8"))
            return ctrl if isinstance(ctrl, dict) else None
        except Exception:  # noqa: BLE001 - a bad control file never crashes the run
            return None

    def _cancel_requested_on_disk(self) -> bool:
        """Cooperative cancel: an LLM/operator wrote ``control.json`` (via
        ``dlc-calibrate --cancel --run <dir>``) asking this run to stop. Polled by the
        watchdog thread AND at every stage boundary. Best-effort — a half-written file or
        a read race just reads as 'no cancel' and is retried on the next poll."""
        ctrl = self._control_on_disk()
        return str((ctrl or {}).get("action", "")).strip().lower() == "cancel"

    def _pause_requested(self, ctrl: Mapping[str, Any]) -> None:
        if self._pause_handler is not None:
            self._pause_handler(ctrl)

    def _resume_requested(self, _ctrl: Mapping[str, Any]) -> None:
        self._consume_control()

    def _consume_control(self) -> None:
        """Delete the control file once a cancel is acted on, so a later resume of the same
        run dir isn't killed by a stale cancel. Best-effort."""
        try:
            self._control_path().unlink()
        except OSError:
            pass

    def _poll_cancel(self) -> None:
        """Honour a cooperative cancel at a stage boundary — covers a run with no watchdog
        thread (tests / autonomous) and a cancel issued while the run was paused between
        invocations (resume picks it up at the first boundary)."""
        ctrl = self._control_on_disk()
        action = str((ctrl or {}).get("action", "")).strip().lower()
        if action == "pause":
            try:
                self.liveness.check(self.runlog.phase or "run")
            except RunCancelled as exc:
                self._consume_control()
                raise CalibrationAborted(StageOutcome(
                    self.runlog.phase or "run", "aborted",
                    digest={"message": str(exc), "cancelled": True})) from exc
            return
        if action == "cancel":
            self._consume_control()
            raise CalibrationAborted(StageOutcome(
                self.runlog.phase or "run", "aborted",
                digest={"message": "run cancelled by operator/LLM (control.json)", "cancelled": True}))

    def _header_data(self) -> dict[str, Any]:
        """The dashboard status-bar payload: who/what is being calibrated, against what
        target, with which correction. Gathered defensively — a missing piece (target not
        yet resolved, no correction on file) just omits that key, never blocks."""
        data: dict[str, Any] = {
            "run_id": self.ctx.root.name,
            "display": self.display.name,
            "monitor": self.monitor,
            "mode": self.mode,
            "flow": self.calib.get("flow"),
            "bit_depth": self.bit_depth,
        }
        if self.target_name:
            data["target"] = self.target_name
            try:
                # Target gamma + luminance — the dashboard's EOTF chart draws the reference
                # curve from these. Defensive: the spec may not resolve yet at first emit.
                spec = self._spec()
                data["gamma"] = spec.gamma
                data["luminance"] = self._hdr_target().peak_nits if spec.is_hdr else spec.luminance_nits
                data["is_hdr"] = spec.is_hdr
                data["colorspace"] = spec.colorspace
                data["transfer"] = "pq" if spec.is_hdr else "power"
            except Exception:  # noqa: BLE001 - advisory chart metadata, never blocks
                pass
        white = self.calib.get("white")
        if white:
            data["white"] = white   # dict: xy, provenance, cct, duv, …
        try:
            store = self._correction_store()
            ccmx = active_correction(self.profile, store, self.display.name)
            if ccmx:
                data["ccmx"] = Path(ccmx).name
            rec = store.get(self.display.name)
            if rec and getattr(rec, "spd_file", None):
                data["spd"] = Path(rec.spd_file).name
        except Exception:  # noqa: BLE001 - the status bar is advisory, never blocks the run
            pass
        return data

    def _emit_header(self) -> None:
        """Emit the run header, but only when it actually changed (it's enriched as the
        target → white → correction become known), so the digest isn't spammed."""
        data = self._header_data()
        if data == self._last_header:
            return
        self._last_header = data
        self.runlog.header(**data)

    def _stage(self, key: str, run_fn: Callable[[], StageOutcome]) -> StageOutcome:
        """Run (or replay) a memoised stage. A recorded ``done`` stage is returned
        from the record without re-doing the work — so a resume after an
        adjudication pause never re-measures.

        Every stage announces itself on the spine: the phase becomes ``key`` (the
        dashboard's phase header), a ``stage_start`` opens it, and a ``stage_done`` /
        ``stage_aborted`` closes it — so the run is never opaque between digests."""
        self._poll_cancel()           # honour a cooperative cancel before opening the stage
        self.runlog.set_phase(key)
        self.runlog.stage_start(key)
        self.liveness.progress(key)   # reset the stall clock at every stage boundary (no cross-stage false trips)
        rec = self.calib["stages"].get(key)
        if rec and rec.get("status") == "done" and not self.force:
            outcome = StageOutcome.from_record(rec)
            self.runlog.stage_done(key, status=outcome.status, replayed=True)
            return outcome
        try:
            outcome = run_fn()
        except RunStalled as exc:
            # The guard tripped mid-stage. The stall event is already on the spine; turn it
            # into a clean abort so the run rolls back instead of grinding silently — the
            # whole point of this work (the 53-min wedge becomes a clean, surfaced failure).
            self.runlog.stage_aborted(key, message=str(exc), stalled=True)
            raise CalibrationAborted(StageOutcome(
                key, "aborted", digest={"message": str(exc), "stalled": True}))
        except RunCancelled as exc:
            # The LLM/operator cancelled mid-stage (checkpoint guard raised it). Consume the
            # control file so a resume isn't re-cancelled, then abort cleanly + roll back.
            self._consume_control()
            self.runlog.stage_aborted(key, message=str(exc), cancelled=True)
            raise CalibrationAborted(StageOutcome(
                key, "aborted", digest={"message": str(exc), "cancelled": True}))
        except CalibrationAborted as exc:
            self.runlog.stage_aborted(exc.outcome.stage,
                                      message=(exc.outcome.digest or {}).get("message"))
            raise
        self.calib["stages"][key] = outcome.as_record()
        self._save()
        self.runlog.stage_done(key, status=outcome.status)
        self._emit_header()   # target/white/correction may have just become known
        # A freshly-completed stage is a natural checkpoint — emit a §12 evidence packet for the
        # LLM if the floor has elapsed. Emit-only (never gates): the stage is already recorded
        # done. Replayed stages (the early-return above) never reach here, so a resume doesn't
        # re-fire check-ins.
        self._maybe_timed_checkin(key)
        return outcome

    # -- the seam ---------------------------------------------------------
    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        """Ask the adjudicator and persist the decision (audit trail + resume
        seed). Propagates :class:`AdjudicationRequired` to pause a live run.

        Precedence: an explicit ``--decide`` override (``self.decision_overrides``) wins over
        an already-recorded decision, so a resumed run can change a recorded seam (notably the
        terminal ``verify:accept`` apply↔revert gate) without ``--force`` discarding all stage
        memoisation. A recorded decision is otherwise replayed as-is; only an un-decided seam
        consults the adjudicator (which may pause the run)."""
        override = self.decision_overrides.get(request.key)
        if override is not None and not self.force:
            recorded = self.calib["decisions"].get(request.key)
            if (recorded is None or recorded.get("choice") != override.choice
                    or recorded.get("note") != override.note
                    or recorded.get("payload") != override.payload):
                # NOTE: a targeted override is safe for terminal/leaf seams (verify:accept has
                # no downstream stages) and for re-deciding an aborted seam (an abort left no
                # downstream stages memoised). The adaptive-planning seam DOES inject a value
                # (the patch plan) that later memoised stages consume — it invalidates those
                # caches itself, keyed on its plan fingerprint (see stage_adaptive_planning).
                self._record_decision(request, override, overridden=recorded is not None)
            return Decision(override.choice, override.note, payload=override.payload)
        if request.key in self.calib["decisions"] and not self.force:
            d = self.calib["decisions"][request.key]
            return Decision(d["choice"], d.get("note"), payload=d.get("payload"))
        try:
            decision = self.adjudicator.adjudicate(request)   # may raise AdjudicationRequired
        except AdjudicationRequired:
            # The run is pausing for a human/LLM decision — make the pause visible on the
            # spine (the dashboard shows "waiting at <seam>"; the LLM digest sees the ask).
            self.runlog.seam(request.stage, key=request.key, status="paused",
                             question=request.question, options=list(request.options))
            raise
        self._record_decision(request, decision)
        return decision

    def _record_decision(self, request: AdjudicationRequest, decision: Decision,
                         *, overridden: bool = False) -> None:
        rec = {**decision.as_dict(), "seam": request.seam, "question": request.question}
        if overridden:
            rec["overridden"] = True
        self.calib["decisions"][request.key] = rec
        self.ctx.log(f"seam {request.key}: {decision.choice}"
                     + (" (override)" if overridden else "")
                     + (f" ({decision.note})" if decision.note else ""))
        self.runlog.seam(request.stage, key=request.key, status="decided",
                         choice=decision.choice, note=decision.note,
                         question=request.question, overridden=overridden)
        self._save()

    def _abort_if(self, decision: Decision, *, stage: str, message: str) -> Decision:
        """Honour an LLM 'abort' verdict at a seam — end the flow cleanly. (The
        AutoAdjudicator never returns 'abort', so autonomous runs are unaffected.)"""
        if decision.choice == "abort":
            raise CalibrationAborted(StageOutcome(
                stage, "aborted", digest={"message": message, "decision_note": decision.note}))
        return decision

    # -- backup / restore (rollback guard) --------------------------------
    def _resolve_desktoplut_ini(self) -> Optional[Path]:
        """Locate the user's DesktopLUT.ini (the complete persisted settings) from the
        profile's ``paths.desktoplut_ini`` (absolute, or relative to the cwd). Returns None
        if unset/missing — the backup then falls back to the lighter state.get JSON."""
        configured = self.profile.paths.get("desktoplut_ini")
        if not configured:
            return None
        p = Path(configured)
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            return p if p.exists() else None
        except Exception:  # noqa: BLE001
            return None

    def _capture_user_backup(self, state: dict[str, Any]) -> dict[str, Any]:
        """Save the user's complete pre-run DesktopLUT setup to the run dir so a failed or
        cancelled run can be rolled back. Copies the whole ``DesktopLUT.ini`` (every setting:
        MHC, 3D LUTs, corrections, WB, tonemap — all of it) BEFORE ``enter-neutral``'s own
        SaveSettings overwrites it, plus a small ``state.get`` JSON noting the active profile.
        The live rollback still uses DesktopLUT's in-memory snapshot; this file copy is the
        complete durable safety net. Captured once."""
        existing = self.calib.get("backup")
        if existing and existing.get("captured"):
            return existing
        record: dict[str, Any] = {"captured": False}
        try:
            mhc = (state or {}).get("mhc") or {}
            key = f"{self.monitor}:{self.mode}"
            active_profile = (mhc.get(key) or {}).get("profile_name")
            path = self.ctx.root / "desktoplut_backup.json"
            atomic_write_text(path, json.dumps(state, indent=2))   # the durable pre-run safety net
            record = {"captured": True, "path": str(path),
                      "active_profile": active_profile,
                      "had_mhc": bool(active_profile)}
            # The complete settings file — the real durable backup.
            ini = self._resolve_desktoplut_ini()
            if ini is not None:
                dest = self.ctx.root / "desktoplut_settings_backup.ini"
                shutil.copy2(ini, dest)
                record["ini_backup"] = str(dest)
                record["ini_source"] = str(ini)
                self.ctx.log(f"backed up full DesktopLUT settings: {ini} → {dest.name}")
            else:
                record["ini_backup"] = None
                self.ctx.log("DesktopLUT.ini not found — set paths.desktoplut_ini in the profile "
                             "for a complete settings backup (state.get JSON saved as a fallback)")
            self.ctx.log(f"backed up user's DesktopLUT state → {path.name}"
                         + (f" (active MHC: {active_profile})" if active_profile else " (no MHC active)"))
        except Exception as exc:  # noqa: BLE001 - backup is best-effort, never blocks the run
            record = {"captured": False, "error": f"{type(exc).__name__}: {exc}"}
            self.ctx.log(f"could not back up DesktopLUT state: {exc}")
        self.calib["backup"] = record
        self._save()
        return record

    def _entered_calibration(self) -> bool:
        """True once ``enter-neutral`` ran (persisted in the run-record, so it holds
        across the pause/resume invocations even though the stage is memoised)."""
        return "enter-neutral" in (self.calib.get("stages") or {})

    def _restore_user_setup(self, *, why: str) -> bool:
        """Roll DesktopLUT back to the user's pre-run setup: restore the snapshot taken
        at ``calibration.enter`` (which re-installs their original MHC) and leave
        calibration mode. Best-effort; returns whether the restore call succeeded."""
        try:
            self.controller.exit_calibration(restore_snapshot=True)
            self.ctx.log(f"restored the user's previous DesktopLUT setup ({why})")
            return True
        except Exception as exc:  # noqa: BLE001
            bak = (self.calib.get("backup") or {}).get("path")
            self.ctx.log(f"restore failed ({why}): {exc}"
                         + (f"; manual backup at {bak}" if bak else ""))
            return False

    def _capture_inplace_baseline(self) -> dict[str, Any]:
        """The in-place flow (``3dlut-only``) tunes the *installed* stack directly — it never
        enters calibration mode, so there is no C++ snapshot to revert to. Record what IS
        restorable over the pipe (the runtime 3D-LUT cube) BEFORE we mutate, so a 'revert' at
        the apply gate can put the prior cube back. (The C++ ``HandleStateGet`` only reports
        ``applied``/``cube_path``, so only the cube is auto-revertible; the durable settings
        backup captured at preflight is the fallback for anything else.) Captured once
        (persists across a pause/resume in the run-record)."""
        existing = self.calib.get("inplace_baseline")
        if existing is not None:
            return existing
        ck = f"{self.monitor}:{self.mode}"
        try:
            state = self.controller.state()
            cube = ((state.get("runtime") or {}).get(ck) or {}).get("cube_path")
            record: dict[str, Any] = {"captured": True, "cube_path": cube}
        except Exception as exc:  # noqa: BLE001 - a down pipe shouldn't crash the flow
            record = {"captured": False, "error": f"{type(exc).__name__}: {exc}"}
        self.calib["inplace_baseline"] = record
        self._save()
        return record

    def _revert_inplace(self) -> str:
        """Revert the in-place refinement (``3dlut-only``). Its only display mutation is the
        runtime cube, so it is fully restorable — put the prior cube back (or clear it if there
        was none). If even that fails, surface the durable settings backup for a manual restore.
        Returns the terminal status (``reverted`` when the display was put back, else
        ``revert_unavailable``)."""
        baseline = self.calib.get("inplace_baseline") or {}
        flow = self.calib.get("flow")
        if flow == "3dlut-only" and baseline.get("captured"):
            prev = baseline.get("cube_path")
            try:
                if prev:
                    self.controller.set_3dlut(self.monitor, self.mode, prev)
                    self.ctx.log(f"reverted: restored the previous 3D LUT ({prev})")
                else:
                    self.controller.clear_3dlut(self.monitor, self.mode)
                    self.ctx.log("reverted: cleared the 3D LUT (none was installed before this run)")
                return "reverted"
            except Exception as exc:  # noqa: BLE001 - fall through to the manual-backup guidance
                self.ctx.log(f"3D-LUT revert failed ({type(exc).__name__}: {exc}); see settings backup")
        bak = self.calib.get("backup") or {}
        ref = bak.get("ini_backup") or bak.get("path")
        self.ctx.log(
            "could not auto-revert this in-place refinement over the pipe. Restore manually "
            "from the pre-run settings backup"
            + (f" ({ref})" if ref else " in the run folder") + ", or run a full calibration.")
        return "revert_unavailable"

    def _commit_calibration(self) -> None:
        """Keep the freshly-built calibration and leave calibration mode cleanly (no
        snapshot restore). Best-effort."""
        try:
            self.controller.exit_calibration(restore_snapshot=False)
            self.ctx.log("applied the new calibration (left calibration mode, profile kept)")
        except Exception as exc:  # noqa: BLE001
            self.ctx.log(f"commit (exit calibration) failed: {exc}")

    def _install_durable_cube(self, cube_path: Optional[str]) -> None:
        """Re-point DesktopLUT at the DURABLE deliverable cube (under ``results/``) rather
        than leaving it aimed at the run-dir build artifact (``runs/<run>/generated/final_*.cube``,
        which is gitignored/ephemeral — if the run folder is cleaned, the live calibration breaks
        and DesktopLUT persists that dead path across restarts). The deliverable is a byte-identical
        copy assembled by ``stage_report``, so the displayed image does not change — only the
        persisted path becomes stable. Apply path only, and a no-op for flows that built no cube
        (``mhc-only`` leaves ``deliverable_cube`` None). Best-effort: a failure leaves
        the working run-dir cube installed, which is no worse than before this re-point existed."""
        if not cube_path:
            return
        try:
            self.controller.set_3dlut(self.monitor, self.mode, cube_path)
            self.ctx.log(f"installed the durable 3D LUT (DesktopLUT now points at {cube_path})")
        except Exception as exc:  # noqa: BLE001 - durability nicety, never a gate on the run
            self.ctx.log(
                f"could not re-point at the durable cube ({type(exc).__name__}: {exc}); the run-dir "
                f"cube stays installed — re-load the results/ cube in DesktopLUT if you clean the run folder")

    # ====================================================================
    # Stage helpers
    # ====================================================================
    def _spec(self) -> cp.TargetSpec:
        assert self.target_name is not None
        return self.profile.target(self.target_name)

    def _transfer(self) -> Transfer:
        return self.profile.transfer_for(self.target_name, bit_depth=self.bit_depth)

    def _engine_target(self):
        # The 3D-LUT correction targets the SAME resolved white the MHC stages do.
        return self.profile.engine_target(self.target_name, white_xy=self._white_xy())

    def _reachable_primaries(self) -> Optional[dict]:
        """The panel's MEASURED native primaries — THIS run's (from the raw stage's channel model,
        persisted to ``mhc_params`` at build), falling back to the prior DIP — used to clamp the
        optimizer/verify target onto the physically reachable gamut (#C3) AND to cap the gamut-aware
        verify ramp's saturation. A saturated target the panel can't render is scored as a clip, not
        chased toward an unreachable Rec.2020 corner. ``None`` ⇒ no clamp/cap (prior behaviour).

        HDR-ONLY by design. #C3 is the wide-gamut (Rec.2020) hazard; sRGB is inside any real panel,
        so an SDR clamp is a no-op there — AND the SDR *verify* (``score_samples``, CIEDE2000/Lab) has
        no clamp, so clamping only the SDR *build* would desync build vs verify on a narrow panel. The
        HDR build and HDR verify (``score_hdr``) BOTH clamp, so HDR stays consistent end to end."""
        if self.mode != "HDR":
            return None
        # Prefer THIS run's freshly-measured native primaries (raw-stage channel model, persisted
        # to mhc_params at build) over the prior DIP — same session, current thermal state, and no
        # stale-DIP dependency for the gamut-aware verify caps + the #C3 clamp. The raw stage runs
        # before verify, so by then this is populated; fall back to the DIP before the build has run
        # (or a no-build flow), then None. (Self-contained gamut awareness without a probe stage —
        # the literal post-warmup probe is only needed once RAW generation is gamut-aware too.)
        prim: Optional[dict] = None
        mp = (self._state.get("mhc_params") or {}).get("primaries")
        if mp and all(k in mp for k in ("rx", "ry", "gx", "gy", "bx", "by")):
            prim = {"R": [float(mp["rx"]), float(mp["ry"])],
                    "G": [float(mp["gx"]), float(mp["gy"])],
                    "B": [float(mp["bx"]), float(mp["by"])]}
        if prim is None:
            dip = self._dip()
            if dip is None or not dip.native_primaries:
                return None
            prim = {ch: [float(xy[0]), float(xy[1])]
                    for ch, xy in dip.native_primaries.items() if xy and len(xy) >= 2}
        if len(prim) != 3:
            return None
        # Guard a degenerate (collinear) primary triangle — it would make the native NPM singular and
        # crash inside the clamp. Real panel primaries are never collinear; a near-zero area ⇒ skip.
        (rx, ry), (gx, gy), (bx, by) = prim["R"], prim["G"], prim["B"]
        area = abs((gx - rx) * (by - ry) - (bx - rx) * (gy - ry)) / 2.0
        return prim if area > 1e-6 else None

    def _hdr_target(self):
        """The chosen HDR target (peak/undershoot/knee/fixed white) for an HDR run,
        resolved from this display+mode's DIP and the run's resolved white
        (``docs/hdr-target-design.md``). Memoised in the run-record so a resumed verify/
        report sees the same peak the build targeted. Only meaningful for a PQ target."""
        cached = self.calib.get("hdr_target")
        if cached:
            from .hdr_target import HdrTarget

            try:
                return HdrTarget(
                    peak_nits=cached["peak_nits"], white_xy=tuple(cached["white_xy"]),
                    undershoot_gain=cached["undershoot_gain"],
                    knee_start_nits=cached["knee_start_nits"],
                    container_nits=cached.get("container_nits", 10000.0),
                    provenance=cached.get("provenance", {}))
            except (KeyError, TypeError, ValueError):
                # A truncated / hand-edited dlc_state.json must not crash the run with an
                # opaque KeyError — re-derive from the DIP + resolved white and overwrite.
                pass
        tgt = self.profile.resolve_hdr_target(self.target_name, dip=self._dip(),
                                              white_xy=self._white_xy())
        self.calib["hdr_target"] = tgt.as_dict()
        self._save()
        return tgt

    # -- white-point resolution (HANDOFF item 7) --------------------------
    def _correction_store(self) -> CorrectionStore:
        """The cross-run, per-display correction store (profile-adjacent / runs-parent)."""
        return CorrectionStore.load(correction_store_path(self.profile, self.ctx.root))

    # -- Display+Instrument Profile (DIP) — produced by characterize -------
    def _dip_store(self) -> DipStore:
        """The cross-run, per-display DIP store (profile-adjacent / runs-parent), produced
        by the ``characterize`` flow and consumed by the measure loop's read policy."""
        return DipStore.load(dip_store_path(self.profile, self.ctx.root))

    def _dip_key(self) -> str:
        """The per-display, per-MODE DIP key — panel thermal/noise behaviour differs by mode
        (SDR converges to a steady temperature; HDR is content-driven and never settles), so an
        SDR and an HDR profile for one panel must coexist."""
        return f"{self.display.name}:{self.mode}"

    def _dip(self) -> Optional[DisplayInstrumentProfile]:
        """This display+mode's DIP, if one has been characterized (else ``None`` → the measure
        loop falls back to its single-read default; runs are leaner, just not noise-aware). Falls
        back to a mode-less record for back-compat with DIPs written before mode-keying."""
        store = self._dip_store()
        return store.get(self._dip_key()) or store.get(self.display.name)

    def _loop_config_for(self, dip: Optional[DisplayInstrumentProfile]) -> MeasureLoopConfig:
        """Build the measure-loop config, preferring DIP-*measured* values over the profile's
        learned-fact fallbacks (the cold channel; the interleaved drift reference's interval
        + threshold). The per-patch read budget is NOT set here — it is decided per patch from
        the DIP's noise model inside the loop (single read by default, escalate on measured σ)."""
        cold = self.display.temperamental_channel or (dip.cold_channel if dip else None)
        kw: dict[str, Any] = {"cold_channel": cold,
                              "settle_threshold": (self.display.settle_delta_de or 0.3) / 100.0}
        if dip is not None:
            if dip.recommended_neutral_interval:
                kw["neutral_interval"] = dip.recommended_neutral_interval
            thr = dip.recommended_drift_threshold
            # Headroom over the characterize-soak band: the soak's read-noise/creep-derived
            # threshold is measured over a SHORT window, but a full calibration runs far longer
            # and sweeps the whole gamut, so it wanders more (≈2x observed, 2026-06-19). Scale the
            # *measured* threshold up rather than hardwiring an absolute dE, so the watch tolerates
            # expected long-run wander but still trips on a genuine excursion. (Per-display
            # learnable via the profile quirk; default DEFAULT_DRIFT_HEADROOM.)
            if thr:
                thr = thr * self.display.drift_headroom
            # Envelope-aware run-time drift watch: a fluctuating panel ALWAYS wanders within its
            # measured fluctuation_envelope, so the interleaved drift reference must tolerate that
            # band — it re-references frequently (the small neutral_interval the DIP recommends for
            # fluctuating) but only FLAGs / re-warms when drift LEAVES the envelope, never thrashing
            # re-measures on the known wander. The envelope is the panel's DEMONSTRATED wander, so it
            # is a hard floor (NOT inflated by the headroom — that would over-loosen a fluctuating
            # watch); a no-op on a convergent panel, whose envelope is ~read noise.
            if dip.fluctuation_envelope:
                thr = max(thr or 0.0, dip.fluctuation_envelope)
            if thr:
                kw["drift_threshold"] = round(thr, 6)
        # Near-neutral read floor (chroma-critical region). Opt-in per run; the DIP still escalates
        # ABOVE it on luminance SNR. Set independently of the DIP so it's also the no-DIP fixed-N
        # fallback for the grey ramp + tube.
        if self.neutral_min_reads is not None:
            kw["neutral_min_reads"] = self.neutral_min_reads
        if self.neutral_chroma_span is not None:
            kw["neutral_chroma_span"] = self.neutral_chroma_span
        if self.neutral_floor_min_nits is not None:
            kw["neutral_floor_min_nits"] = self.neutral_floor_min_nits
        if self.dark_min_reads is not None:
            kw["dark_min_reads"] = self.dark_min_reads
        if self.dark_floor_max_nits is not None:
            kw["dark_floor_max_nits"] = self.dark_floor_max_nits
        return MeasureLoopConfig(**kw)

    def _resolve_white_now(self) -> cp.WhitePointResolution:
        """Resolve the target white, preferring a white SPD captured by a probe-match
        build (item 9) recorded in the store over the profile's ``display.white_spd``."""
        rec = self._correction_store().get(self.display.name)
        spd_override = rec.spd_file if rec else None
        return self.profile.resolve_white(self.monitor, self.target_name,
                                          white_fn=self._white_fn, spd_override=spd_override)

    def _resolved_white(self) -> cp.WhitePointResolution:
        """The run's resolved target white (memoised in the run-record by
        :meth:`stage_whitepoint`; resolved on demand if a stage reaches for it first
        — e.g. a resumed run before that stage replays)."""
        cached = self.calib.get("white")
        if cached:
            return cp.WhitePointResolution.from_dict(cached)
        res = self._resolve_white_now()
        self.calib["white"] = res.as_dict()
        self._save()
        return res

    def _white_xy(self) -> tuple[float, float]:
        return self._resolved_white().xy

    def _measure_set(self, patches: Sequence[tuple[int, int, int]], *, role: str,
                     ti3_name: str, ndjson_name: str) -> MeasureLoopResult:
        transfer = self._transfer()
        dip = self._dip()
        cfg = self.loop_config or self._loop_config_for(dip)
        meas_dir = self.ctx.root / "measurements"
        # Pass the DIP through: the loop reads a single adaptive-integration read by default
        # and escalates to more averaged reads only where the DIP's measured noise model says
        # this luminance needs SNR — never a fixed count, never a silent cap.
        self.liveness.set_stall_after(self._liveness_threshold(dip))
        return run_measure_loop(
            patches=patches, transfer=transfer, measure=self.measure, config=cfg,
            ti3_path=meas_dir / ti3_name, ndjson_path=meas_dir / ndjson_name,
            runlog=self.runlog, liveness=self.liveness, dip=dip,
            checkin_interval_s=self._checkin_interval_s,
        )

    def _liveness_threshold(self, dip: Optional[Any]) -> float:
        """The no-progress stall threshold, derived from the measured panel+meter timing
        when characterized — never a bare magic number. A patch can legitimately take a
        settle plus a budget of slow dark-patch reads (each capped at the meter's per-read
        ceiling), so the bound is a generous multiple of that worst case with a floor; with
        no DIP it falls back to a conservative fixed bound (still active — the stalled panel
        may well be uncharacterized)."""
        floor = 180.0
        if dip is None:
            return 600.0
        settle = dip.settle_seconds or 0.0
        per_read = max(dip.read_overhead_s or 2.0, 2.0)
        budget = 8          # a generous per-patch read budget (the loop flags, never hard-caps)
        return max(floor, 4.0 * (settle + budget * per_read))

    def _probe_fn(self) -> ProbeFn:
        """The re-measure probe for the correction machine. Injected in tests;
        otherwise present each driven signal (code values, no LUT) and read it via
        the measure seam — the fidelity-ladder tier-2 path."""
        if self._probe is not None:
            return self._probe
        transfer = self._transfer()
        max_cv = transfer.max_cv
        batch = {"n": 0}   # outer-iteration counter so the build's progress bar pulses per pass

        def probe(signals: np.ndarray) -> np.ndarray:
            sig = np.clip(np.asarray(signals, dtype=float).reshape(-1, 3), 0.0, 1.0)
            out = np.zeros((len(sig), 3), dtype=float)
            batch["n"] += 1
            total = len(sig)
            # The optimizer's per-iteration compute (model + cube build) precedes this batch;
            # reset the stall clock so that bounded compute is never mistaken for a stall.
            self.liveness.progress("build-install-3dlut")
            for i, s in enumerate(sig):
                rgb = tuple(int(round(c * max_cv)) for c in s)
                patch = MeasurePatch(label=f"probe{i:04d}", rgb=rgb,  # type: ignore[arg-type]
                                     signal=(float(s[0]), float(s[1]), float(s[2])),
                                     role="measurement", bit_depth=transfer.bit_depth)
                # The build probe re-measures off the meter, bypassing the measure loop — so it
                # arms the same stall guard itself (this is the exact loop that wedged for 53 min).
                self.liveness.activity("build-install-3dlut")
                self.liveness.check("build-install-3dlut")
                reading = self._probe_read(patch)
                ok = reading.ok and reading.xyz is not None
                if ok:
                    self.liveness.progress("build-install-3dlut")
                # Mirror the probe read onto the spine so the build (the loop that stalled
                # for 53 min) is LIVE on the dashboard — the build probe re-measures off the
                # measure loop, so without this the longest phase was invisible.
                self.runlog.patch_read(
                    "build-install-3dlut", seq=i, role="probe", label=patch.label,
                    rgb=list(rgb), signal=[round(float(c), 5) for c in s],
                    Y=(round(reading.xyz[1], 4) if ok else None),
                    xy=_reading_xy(reading), ok=ok, disposition="probe")
                if not ok:
                    # NEVER fold a failed read as (0,0,0): optimize_cube folds the probe's
                    # response back into the TRAINING set (optimize.py), so one black reading
                    # permanently poisons the model and every subsequent cube. Abort cleanly
                    # instead — a missing correction beats a black-poisoned one.
                    raise CalibrationAborted(StageOutcome(
                        "build-install-3dlut", "aborted",
                        digest={"message": (f"build probe could not read signal "
                                            f"{[round(float(c), 4) for c in s]} after retries "
                                            f"({reading.error}); aborting rather than folding a black "
                                            f"reading into the cube."),
                                "probe_failure": True,
                                "failed_signal": [round(float(c), 4) for c in s]}))
                out[i] = reading.xyz
                # Drive the dashboard's progress bar DURING the build — it would otherwise sit
                # frozen at the post-MHC count for the whole (longest) stage. Progress-driven,
                # restarting each outer pass so the bar visibly pulses = clearly alive.
                self.runlog.progress("build-install-3dlut", patches_done=i + 1,
                                     patches_total=total, iteration=batch["n"])
            return out

        return probe

    def _probe_read(self, patch: MeasurePatch, *, retries: int = 2) -> Reading:
        """Read one probe patch with a small retry ladder — a transient glitch (a single
        garbled/under-range read) is common and recoverable, so re-trigger before giving
        up. Each retry is surfaced as an ``anomaly`` (digest tier) so the dashboard + LLM
        see the meter struggling. An unrecoverable read is left for the caller to abort on
        (never folded as black)."""
        reading = self.measure(patch)
        attempt = 0
        while (not reading.ok or reading.xyz is None) and attempt < retries:
            attempt += 1
            self.runlog.anomaly("build-install-3dlut", label=patch.label, attempt=attempt,
                                error=reading.error or "no reading")
            reading = self.measure(patch)
        return reading

    def _on_optimize_iteration(self, result: Any) -> None:
        """Stream each outer correction-machine iteration to the spine (digest tier):
        the convergence curve (mean/p95/max dE), the budget, and the model-vs-reality
        gap — so the LLM and the dashboard both watch the build converge or stall."""
        # A completed outer iteration is real progress — reset the stall clock before the
        # next iteration's compute span.
        self.liveness.progress("build-install-3dlut")
        try:
            data = result.as_dict()
            self._last_optimizer = dict(data)
            self.runlog.optimizer_iteration(**data)
            # The optimizer is the long pole (it can run for hours). Emit a §12 evidence packet
            # between iterations so a multi-hour optimize never goes dark for the LLM.
            self._maybe_timed_checkin("build-install-3dlut")
        except Exception:  # noqa: BLE001 - telemetry must never break the build
            pass

    # -- §12 timed check-in (NON-BLOCKING evidence packet) ------------------
    def _maybe_timed_checkin(self, trigger: str) -> None:
        """Emit a rich evidence packet for the overseeing LLM once the wall-clock floor has
        elapsed (§12). Disabled at interval 0; the first checkpoint only anchors the clock.

        DESIGN LAW (do not regress): a check-in NEVER pauses the spine and carries NO
        recommendation/accept for anyone to rubber-stamp. It is the spine collecting the
        evidence since the last check-in — warnings, the max ΔE actually read, re-read /
        repeated patches, non-stopper anomalies — and handing it to the LLM EVERY TIME so the
        LLM applies judgment ("ΔE high but that's the panel limit"; "patch re-read twice but
        the latest read is normal → self-corrected") and intervenes ONLY if it sees a real
        problem. Run-stoppers are a SEPARATE mechanism (adjudicated seams). The LLM consumes
        this from the running (background) spine out of band — emit-only, no exit-10 gate. This
        is the point of DLC: LLM intelligence consuming tools+data, not a deterministic program.
        """
        import time
        if self._checkin_interval_s <= 0 or self.runlog is None:
            return
        now = time.monotonic()
        if self._last_checkin_monotonic is None:
            # First checkpoint just anchors the clock — no immediate ping at second 0.
            self._last_checkin_monotonic = now
            self._last_checkin_tally = dict(self.runlog.tally)
            self._last_checkin_pos = self._events_size()
            return
        if now - self._last_checkin_monotonic < self._checkin_interval_s:
            return
        elapsed_since = now - self._last_checkin_monotonic
        seq = int(self.calib.get("checkin_seq", 0)) + 1
        self.calib["checkin_seq"] = seq
        digest = self._checkin_digest(trigger, seq=seq, elapsed_since_checkin_s=round(elapsed_since, 1))
        # Reset the window AFTER building the digest, BEFORE emitting, so the next window starts
        # clean and the check_in event itself isn't counted into it.
        self._last_checkin_monotonic = now
        self._last_checkin_tally = dict(self.runlog.tally)
        self._last_checkin_pos = self._events_size()
        self.runlog.check_in(trigger, **digest)   # EMIT-ONLY: evidence for the LLM, never a gate

    def _checkin_digest(self, trigger: str, *, seq: int = 0,
                        elapsed_since_checkin_s: float = 0.0) -> dict[str, Any]:
        """The rich check-in payload: the run overview, what happened since the last check-in,
        and the latest live metrics — exactly what a supervising LLM needs to judge "continue?"."""
        return {
            "seq": seq,
            "elapsed_since_checkin_s": elapsed_since_checkin_s,
            "overview": self._run_overview(trigger),
            "since_last": self._events_since_last_checkin(),
            "evidence": self._checkin_evidence(),
            "metrics": self._latest_checkin_metrics(),
        }

    def _events_size(self) -> int:
        """Current byte size of events.jsonl (the check-in evidence window high-water mark)."""
        try:
            return self.runlog.path.stat().st_size if self.runlog else 0
        except OSError:
            return 0

    def _checkin_evidence(self) -> dict[str, Any]:
        """The REAL evidence since the last check-in, read back from the events.jsonl window:
        every warning/anomaly (with detail), the max ΔE actually read + which patch, and the
        read count. This is data for the LLM to JUDGE — deliberately NOT a verdict and NOT a
        recommendation. The full firehose is always on disk; this is the at-a-glance packet."""
        import json as _json
        out: dict[str, Any] = {"reads": 0, "max_dE": None, "max_dE_patch": None, "warnings": []}
        if self.runlog is None:
            return out
        try:
            with self.runlog.path.open("r", encoding="utf-8") as fh:
                fh.seek(self._last_checkin_pos or 0)
                lines = fh.readlines()
        except OSError:
            return out
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = _json.loads(ln)
            except ValueError:
                continue
            ev = e.get("event")
            data = e.get("data") or {}
            if ev == "patch_read":
                out["reads"] += 1
                de = data.get("dE")
                if isinstance(de, (int, float)) and (out["max_dE"] is None or de > out["max_dE"]):
                    out["max_dE"] = round(de, 3)
                    out["max_dE_patch"] = data.get("label") or data.get("role") or data.get("signal")
            elif ev in ("anomaly", "read_plausibility_anomaly", "stall"):
                w = {"event": ev, "stage": e.get("stage")}
                for k in ("kind", "label", "reason", "message", "detail", "attempt"):
                    if k in data:
                        w[k] = data[k]
                out["warnings"].append(w)
        # Cap the inline warning list so the packet stays readable; the full log is on disk.
        if len(out["warnings"]) > 25:
            extra = len(out["warnings"]) - 25
            out["warnings"] = out["warnings"][:25] + [{"truncated": extra, "note": "see events.jsonl"}]
        return out

    def _run_overview(self, trigger: str) -> dict[str, Any]:
        import time
        stages = self.calib.get("stages") or {}
        done = [k for k, v in stages.items() if (v or {}).get("status") == "done"]
        elapsed = None
        if self._run_started_monotonic is not None:
            elapsed = round(time.monotonic() - self._run_started_monotonic, 1)
        return {
            "run": self.ctx.root.name,
            "flow": self.calib.get("flow"),
            "mode": self.mode,
            "target": self.target_name,
            "phase": self.runlog.phase if self.runlog else None,
            "stage": trigger,
            "stages_done": len(done),
            "completed": done,
            "elapsed_s": elapsed,
        }

    def _events_since_last_checkin(self) -> dict[str, int]:
        """Per-event-name counts emitted since the previous check-in (anomalies, seams, reads,
        optimizer iterations, …) — the spine delta, computed from the RunLog tally, no disk read."""
        cur = self.runlog.tally if self.runlog else {}
        prev = self._last_checkin_tally or {}
        return {name: cur[name] - prev.get(name, 0)
                for name in cur if cur[name] - prev.get(name, 0) > 0}

    def _latest_checkin_metrics(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self._last_scored:
            out["last_scored"] = self._last_scored
        if self._last_optimizer:
            out["optimizer"] = self._last_optimizer
        if self._last_refine:
            out["refine"] = self._last_refine
        return out

    def _monitor_map_check(self) -> dict[str, Any]:
        """Mechanically verify the profile's monitor↔Argyll↔panel mapping against LIVE enumeration,
        BEFORE hours of measurement. ``query_monitors`` reports each display's ``index`` (the
        DesktopLUT monitor), ``device_name`` (``\\\\.\\DISPLAYn`` in ARGYLL order ⇒ ``n`` == the
        Argyll ``-d`` number) and ``hardware_id`` (EDID). A wrong ``desktoplut_monitor`` /
        ``argyll_display`` in the YAML otherwise sails through preflight and is only caught after the
        panel reads collapse — wasting the whole run on the wrong display.

        Best-effort detection, definite-only verdict: a query that fails or omits fields CANNOT prove
        a mismatch, so it yields ``checked=False`` (a tell), never a false abort. ``mismatch=True``
        only on POSITIVE evidence — the configured index is absent, the device's Argyll number
        disagrees with ``argyll_display``, or a recorded EDID (``quirks['hardware_id']``) differs."""
        try:
            monitors = (self.controller.query_monitors() or {}).get("monitors") or []
        except Exception as exc:  # noqa: BLE001 — can't verify ⇒ tell, never a false abort
            return {"checked": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not monitors:
            return {"checked": False, "reason": "no monitor topology available"}
        present = sorted(m.get("index") for m in monitors if m.get("index") is not None)
        out: dict[str, Any] = {"checked": True, "desktoplut_monitor": self.monitor,
                               "argyll_display": self.display.argyll_display,
                               "present_indices": present, "mismatch": False}
        target = next((m for m in monitors if m.get("index") == self.monitor), None)
        if target is None:
            out["mismatch"] = True
            out["reason"] = (
                f"profile desktoplut_monitor={self.monitor} ({self.display.name}) is not among the "
                f"live displays {present} — wrong monitor index, or the display is unplugged/asleep. "
                f"Every patch would be presented/measured on the wrong panel.")
            return out
        dev = target.get("device_name")
        out["device_name"] = dev
        out["hardware_id"] = target.get("hardware_id")
        argyll_n = argyll_display_from_device_name(dev)
        out["device_argyll_display"] = argyll_n
        if argyll_n is not None and argyll_n != self.display.argyll_display:
            out["mismatch"] = True
            out["reason"] = (
                f"monitor {self.monitor} is {dev} (Argyll display {argyll_n}), but the profile maps it "
                f"to argyll_display={self.display.argyll_display}. spotread/ccxxmake drive the display "
                f"by that Argyll number (`-d {self.display.argyll_display}`), so the correction would be "
                f"built against the WRONG display.")
            return out
        want_hw = str(self.display.quirks.get("hardware_id") or "").strip()
        live_hw = str(target.get("hardware_id") or "").strip()
        if want_hw and live_hw and want_hw != live_hw:
            out["mismatch"] = True
            out["reason"] = (
                f"monitor {self.monitor} reports EDID {live_hw!r}, but the profile expects {want_hw!r} "
                f"(quirks.hardware_id) — a DIFFERENT panel is at this index than the one configured.")
        return out

    def _patch_window_guard(self) -> dict[str, Any]:
        """Assert the dogegen patch window will land on the calibration target monitor.

        dogegen renders on the Windows primary and has no monitor-select CLI (the window is
        moved/fullscreened by hand). If the target monitor isn't the primary, every patch
        would be measured on the wrong panel. Cross-check the topology via ``query_monitors``
        and surface a precise, actionable warning when they differ — best-effort (a monitor
        query that fails or omits ``primary`` just yields no warning, never blocks the run)."""
        try:
            monitors = (self.controller.query_monitors() or {}).get("monitors") or []
        except Exception as exc:  # noqa: BLE001 - advisory only; never blocks
            return {"checked": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not monitors:
            return {"checked": False, "reason": "no monitor topology available"}
        primary = next((m for m in monitors if m.get("primary")), None)
        target = next((m for m in monitors if m.get("index") == self.monitor), None)
        primary_idx = primary.get("index") if primary else None
        target_is_primary = primary_idx is not None and primary_idx == self.monitor
        live_cs = (target or {}).get("color_space")
        guard: dict[str, Any] = {
            "checked": True, "target_monitor": self.monitor, "primary_monitor": primary_idx,
            "target_is_primary": target_is_primary,
            "target_device": (target or {}).get("device_name"),
            "target_rect": (target or {}).get("rect"),
            "target_color_space": live_cs,
            "requested_mode": self.mode,
        }
        # Display-mode match: a run in --mode HDR measured on a still-SDR panel (or vice
        # versa) reads the wrong colorspace on every patch. Tell the operator to flip the
        # panel first (`dlc-calibrate --set-hdr on/off --monitor N`) rather than measure
        # blindly. Best-effort: an unknown color_space yields no warning, never blocks.
        if live_cs is not None:
            want_hdr = self.mode == "HDR"
            if want_hdr != color_space_is_hdr(live_cs):
                guard["mode_warning"] = (
                    f"display mode mismatch: calibration runs in {self.mode} but monitor "
                    f"{self.monitor} is currently {live_cs}. Flip the panel to {self.mode} first — "
                    f"`dlc-calibrate --set-hdr {'on' if want_hdr else 'off'} --monitor {self.monitor}` "
                    f"(and start the dogegen daemon in the matching mode) — or every patch is "
                    f"measured in the wrong colorspace.")
        if primary_idx is not None and not target_is_primary:
            dev = (target or {}).get("device_name") or f"monitor {self.monitor}"
            guard["warning"] = (
                f"patch-window placement: calibration targets monitor {self.monitor} ({dev}), "
                f"but the Windows primary is monitor {primary_idx}. dogegen opens its pattern "
                f"window on the primary and has NO monitor-select flag — move/Alt+Enter-fullscreen "
                f"it onto monitor {self.monitor} BEFORE measuring, or every patch lands on the "
                f"wrong panel and all readings are silently wrong.")
        return guard

    def _transport_tell(self) -> dict[str, Any]:
        """Advisory (never a gate): a 3D-LUT flow measured below the panel's bit depth, or on a
        local-dimming panel without a fullscreen patch, risks contaminated VOLUMETRIC reads —
        the very data the 3D LUT is built from. The orchestrator can't see the presenter
        transport (wired in the CLI) but it knows the run's bit depth + the panel, so it surfaces
        the risk + the fix: an ACM/FP16 SDR scanout is 10-bit-live (an 8-bit windowed read
        under-samples it), and mini-LED local dimming contaminates a non-fullscreen patch."""
        flow = self.calib.get("flow")
        if flow not in ("full", "3dlut-only") or self.mode != "SDR":
            return {"checked": False, "reason": "not an SDR 3D-LUT flow"}
        panel = self.display.panel
        panel_bits = panel.bit_depth or 8
        tech = (panel.tech or "").lower()
        local_dimming = bool(panel.backlight_zones) or any(t in tech for t in ("mini", "fald", "local"))
        guard: dict[str, Any] = {"checked": True, "bit_depth": self.bit_depth,
                                 "panel_bit_depth": panel_bits, "local_dimming": local_dimming}
        if self.bit_depth < 10 and (panel_bits >= 10 or local_dimming):
            guard["warning"] = (
                f"3D-LUT flow measuring at {self.bit_depth}-bit on a {panel_bits}-bit"
                f"{' mini-LED/local-dimming' if local_dimming else ''} panel in SDR: an ACM/FP16 SDR "
                f"scanout is 10-bit-live (an 8-bit windowed read under-samples it)"
                f"{' and local dimming contaminates a non-fullscreen patch' if local_dimming else ''}. "
                f"Run with `--bit-depth 10` over the persistent fullscreen dogegen daemon "
                f"(`--dogegen-server HOST:PORT`), DesktopLUT hook ON — or the volumetric reads "
                f"feeding the 3D LUT may be silently wrong.")
        return guard

    def _target_colorspace(self) -> Optional[str]:
        """The target colour space for this run, resilient to preflight running BEFORE
        resolve-target sets ``target_name`` (fall back to the display's per-mode target)."""
        name = self.target_name or self.display.target_name(self.mode)
        if not name:
            return None
        try:
            return self.profile.target(name).colorspace
        except (KeyError, AttributeError):
            return None

    def _gamut_tell(self) -> dict[str, Any]:
        """Advisory (never a gate): does the panel's MEASURED native gamut (the DIP's
        ``native_primaries``) cover the target colour space? A target primary OUTSIDE the native
        RGB triangle is physically unreachable — the build will CLIP there no matter what — so
        surface it up front (coverage %, which primaries, by how much) and inform gamut-map vs
        clip, instead of it emerging patch-by-patch in the cube residuals. Consumes
        ``native_primaries`` (measured by characterize, previously unused by calibration)."""
        dip = self._dip()
        if dip is None or not dip.native_primaries:
            return {"checked": False, "reason": "no characterized native primaries"}
        native = {ch: (float(xy[0]), float(xy[1]))
                  for ch, xy in dip.native_primaries.items() if xy and len(xy) >= 2}
        if not {"R", "G", "B"} <= set(native):
            return {"checked": False, "reason": "incomplete native primaries"}
        colorspace = self._target_colorspace()
        tgt = gamut.target_primaries(colorspace)
        if tgt is None:
            return {"checked": False, "reason": f"unknown target colourspace {colorspace!r}"}
        cov = gamut.gamut_coverage(native, tgt)
        tell: dict[str, Any] = {"checked": True, "colorspace": colorspace,
                                "coverage_ratio": round(cov["coverage_ratio"], 4),
                                "reachable": cov["reachable"], "shortfall": cov["shortfall"],
                                "native_primaries": native}
        unreachable = [ch for ch, ok in cov["reachable"].items() if not ok]
        if unreachable:
            chans = "/".join(unreachable)
            tell["warning"] = (
                f"native gamut covers ~{cov['coverage_ratio'] * 100:.1f}% of {colorspace}: the "
                f"target {chans} primar{'y is' if len(unreachable) == 1 else 'ies are'} OUTSIDE the "
                f"panel's gamut (unreachable — the build will hard-CLIP there). Consider perceptual "
                f"gamut-mapping rather than clipping, or a target the panel can cover.")
        elif cov["coverage_ratio"] < 0.99:
            tell["warning"] = (f"native gamut covers ~{cov['coverage_ratio'] * 100:.1f}% of "
                               f"{colorspace} — minor under-coverage near the gamut boundary.")
        return tell

    def _panel_limits_tell(self) -> dict[str, Any]:
        """Advisory (never a gate): the panel's MEASURED native white / black (the DIP) vs the
        target — contrast (raised black ⇒ limited shadows/black level) and, for HDR, peak
        headroom (measured peak below the target peak ⇒ the build must roll off / lower the
        ceiling). Consumes ``native_white_nits`` / ``native_black_nits`` (measured by
        characterize, previously unused). SDR white luminance is OSD-set by the brightness stage,
        so it's reported but not warned on here."""
        dip = self._dip()
        if dip is None or dip.native_white_nits is None:
            return {"checked": False, "reason": "no characterized native white/black"}
        white = float(dip.native_white_nits)
        black = float(dip.native_black_nits) if dip.native_black_nits is not None else None
        contrast = (white / black) if (black and black > 0) else None
        colorspace = self._target_colorspace()
        try:
            spec = self.profile.target(self.target_name or self.display.target_name(self.mode))
            is_hdr = spec.is_hdr
            # HDR target = the resolved MAX-SUSTAINED peak (already clamped to the native ceiling),
            # NOT the profile's viewing peak_luminance_nits (owner 2026-06-24, Task C — that moved to
            # DesktopLUT's tonemap). So this headroom tell never fires a spurious "roll off to native"
            # in the normal case; it only speaks if a pinned override somehow exceeds the panel. SDR =
            # the OSD-set white luminance.
            target_nits = self._hdr_target().peak_nits if is_hdr else spec.luminance_nits
        except (KeyError, AttributeError, ValueError):
            target_nits, is_hdr = None, (self.mode == "HDR")
        tell: dict[str, Any] = {"checked": True, "native_white_nits": round(white, 2),
                                "native_black_nits": (round(black, 5) if black is not None else None),
                                "contrast": (round(contrast) if contrast else None),
                                "target_nits": target_nits, "mode": self.mode}
        msgs: list[str] = []
        # HDR peak headroom: the measured peak IS the panel's HDR ceiling (not OSD-adjustable),
        # so a target peak above it can't be hit — the build must roll off / drop the ceiling.
        if is_hdr and target_nits and white < target_nits * 0.95:
            msgs.append(f"resolved sustained peak {target_nits:g} nits exceeds the measured native "
                        f"ceiling {white:.0f} — the calibration is capped to ~{white:.0f}")
        # Raised black / low contrast (advisory threshold, not panel-specific).
        if contrast is not None and contrast < 200:
            msgs.append(f"measured contrast ~{contrast:.0f}:1 (raised black {black:.3f} nits) — "
                        f"black level + shadow accuracy will be limited")
        if msgs:
            tell["warning"] = "; ".join(msgs)
        return tell

    # ====================================================================
    # Stages
    # ====================================================================
    def stage_preflight(self) -> StageOutcome:
        def run() -> StageOutcome:
            # Verify the profile's display mapping against what the controller sees.
            mapping_ok = True
            seen_monitors: list[int] = []
            try:
                state = self.controller.state()
                for key in (state.get("mhc") or {}).keys():
                    seen_monitors.append(int(str(key).split(":")[0]))
                for key in (state.get("runtime") or {}).keys():
                    seen_monitors.append(int(str(key).split(":")[0]))
                mapping_ok = (not seen_monitors) or (self.monitor in set(seen_monitors))
            except Exception as exc:  # noqa: BLE001 - surfaced in the digest
                state = {"error": f"{type(exc).__name__}: {exc}"}
                mapping_ok = False
            # The persistent per-display store supplies the correction's real build
            # date when present (a refresh recorded since the profile was written),
            # so staleness ages from when the correction was actually made (§10).
            corr_store = self._correction_store()
            store_rec = corr_store.get(self.display.name)
            store_made = store_rec.correction_made if store_rec else None
            # Consult the SAME correction the meter is actually wired to (store overrides the
            # profile YAML — active_correction), not the (possibly empty) profile YAML, so the
            # tell can't report "no correction" while the meter is in fact corrected.
            staleness = self.profile.correction_staleness(
                today=self.run_date, made_override=store_made,
                file_override=active_correction(self.profile, corr_store, self.display.name))
            # Patch-window placement guard (M3): dogegen has NO monitor-select CLI — its window
            # opens on the Windows primary and is positioned/fullscreened by hand. If the
            # calibration target isn't the primary, patches would land on the WRONG panel and
            # every measurement would be silently wrong. Assert the topology instead of assuming it.
            # Monitor↔Argyll↔panel map vs LIVE enumeration (#5): catch a wrong desktoplut_monitor /
            # argyll_display BEFORE measuring, not after the reads collapse. Mechanical detection here;
            # the DECISION on a mismatch is a seam below (the LLM/operator aborts to fix, or proceeds).
            monitor_map = self._monitor_map_check()
            if monitor_map.get("mismatch"):
                self.ctx.log("monitor map mismatch: " + monitor_map.get("reason", ""))
            patch_window = self._patch_window_guard()
            if patch_window.get("warning"):
                self.ctx.log(patch_window["warning"])
            if patch_window.get("mode_warning"):
                self.ctx.log(patch_window["mode_warning"])
            # Measurement-transport adequacy for 3D-LUT flows (advisory): bit depth + panel.
            transport = self._transport_tell()
            if transport.get("warning"):
                self.ctx.log(transport["warning"])
            # Panel-capability tells from the DIP (advisory, never gates): does the measured
            # native gamut cover the target, and do native white/black/contrast fit it? These
            # consume the DIP's display axis (native primaries / white / black) up front, so an
            # unreachable target gamut or a raised black is surfaced before the build, not in the
            # cube residuals afterward.
            gamut_tell = self._gamut_tell()
            if gamut_tell.get("warning"):
                self.ctx.log(gamut_tell["warning"])
            panel_limits = self._panel_limits_tell()
            if panel_limits.get("warning"):
                self.ctx.log(panel_limits["warning"])
            # Display+Instrument Profile staleness *tell* (never a gate): the measure loop
            # works without a DIP (single-read default), but a fresh one makes reads noise-aware.
            # Surface present/stale/missing so the LLM can choose to `--flow characterize` first.
            dip = self._dip()
            dip_status = {"present": dip is not None,
                          "stale": (dip.is_stale(self.run_date.isoformat()) if dip else None),
                          "made": (dip.made if dip else None),
                          "bands": (len(dip.noise_model) if dip else 0)}
            if dip is None or dip_status["stale"]:
                self.ctx.log("no fresh Display+Instrument Profile for this display — run "
                             "`--flow characterize` to learn panel+meter behaviour "
                             "(calibration falls back to a single adaptive-integration read meanwhile).")
            # Save the user's current DesktopLUT state BEFORE we touch anything, so a
            # failed/cancelled run can be rolled back to exactly this. preflight is the
            # first stage and read-only, so this captures the pristine pre-run setup.
            backup = self._capture_user_backup(state)
            digest = {"monitor": self.monitor, "mode": self.mode, "display": self.display.name,
                      "argyll_display": self.display.argyll_display, "mapping_ok": mapping_ok,
                      "seen_monitors": sorted(set(seen_monitors)),
                      "monitor_map": monitor_map,
                      "correction": staleness.as_dict(),
                      "correction_from_store": store_made is not None,
                      "patch_window": patch_window,
                      "transport": transport,
                      "gamut": gamut_tell,
                      "panel_limits": panel_limits,
                      "dip": dip_status,
                      "backup": backup}
            return StageOutcome("preflight", "done", digest=digest,
                                data={"stale": staleness.stale, "mapping_ok": mapping_ok})

        outcome = self._stage("preflight", run)
        # Monitor↔Argyll↔panel map mismatch (#5): a wrong index/display number wastes the WHOLE run on
        # the wrong panel, so adjudicate it BEFORE measuring — recommend abort (fix the profile), but
        # let the operator/LLM proceed if the live topology is the surprise (e.g. a transient unplug).
        monitor_map = outcome.digest.get("monitor_map", {})
        if monitor_map.get("mismatch"):
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="preflight:monitor-map", seam=SEAM_MONITOR_MAP, stage="preflight",
                question=monitor_map.get("reason", "the profile's monitor↔Argyll↔panel mapping "
                         "disagrees with the live displays — abort and fix the profile, or proceed?"),
                options=("abort", "proceed"), recommendation="abort", digest=monitor_map)),
                stage="preflight", message="aborted on a monitor↔Argyll↔panel map mismatch")
        staleness = outcome.digest.get("correction", {})
        # The build-correction flow IS the refresh, so don't ask about staleness there.
        if staleness.get("stale") and self.calib.get("flow") != "build-correction":
            decision = self._abort_if(self.adjudicate(AdjudicationRequest(
                key="preflight:spd", seam=SEAM_SPD, stage="preflight",
                question=staleness.get("message", "colorimeter correction is stale — refresh or proceed?"),
                options=("proceed", "refresh", "abort"), recommendation="proceed",
                digest=staleness)), stage="preflight", message="aborted on stale colorimeter correction")
            if decision.choice == "refresh":
                # The meter for THIS run is already wired to the current correction, so a
                # mid-flow refresh can't apply — direct the operator to the build first.
                raise CalibrationAborted(StageOutcome(
                    "preflight", "aborted",
                    digest={"message": "correction refresh requested — run `--flow build-correction` first "
                                       "(it mints a fresh CCMX via ccxxmake at the box and records it as the "
                                       "active correction), then re-run this calibration."}))
        return outcome

    def stage_resolve_target(self) -> StageOutcome:
        # This stage owns its own adjudication (the plan seam) and so bypasses _stage —
        # announce it on the spine directly so the dashboard phase header still tracks it.
        self.runlog.set_phase("resolve-target")
        self.runlog.stage_start("resolve-target")
        target = self.display.target_name(self.mode)
        if not target:
            raise CalibrationAborted(StageOutcome(
                "resolve-target", "aborted",
                digest={"message": f"display {self.monitor} has no {self.mode} target configured"}))
        spec = self.profile.target(target)
        self.target_name = target
        self.calib["target"] = target
        self._save()
        # HDR (PQ): resolve the chosen target (peak off the ladder, undershoot gain + knee,
        # fixed white) from the DIP now, so the plan digest reports the real peak and the
        # patch sets are capped to it (docs/hdr-target-design.md). SDR is unaffected.
        hdr = self._hdr_target() if spec.is_hdr else None
        flow = self.calib.get("flow")
        # Surface the run's SIZE up front (patch counts per measured stage), so the operator/LLM
        # approves the plan knowing the time cost — and can abort + re-run with different patch
        # flags if it's too long/short. This is the "no reservations about deciding time" lever.
        patch_plan = self._patch_plan_record(flow)
        existing_plan = self.calib.get("patch_plan")
        if (
            isinstance(existing_plan, dict)
            and existing_plan.get("approved")
            and existing_plan.get("fingerprint") != patch_plan.get("fingerprint")
            and not self.force
        ):
            raise CalibrationAborted(StageOutcome(
                "resolve-target", "aborted",
                digest={
                    "message": "approved patch plan changed since this run was approved; start a fresh run or resume with --force",
                    "approved_fingerprint": existing_plan.get("fingerprint"),
                    "current_fingerprint": patch_plan.get("fingerprint"),
                    "approved_patch_plan": existing_plan,
                    "current_patch_plan": patch_plan,
                }))
        self.calib["patch_plan"] = {**patch_plan, "approved": bool(
            isinstance(existing_plan, dict)
            and existing_plan.get("approved")
            and existing_plan.get("fingerprint") == patch_plan.get("fingerprint")
        )}
        self._save()
        transfer_label = "PQ (ST.2084)" if spec.is_hdr else f"power γ{spec.gamma}"
        target_nits = hdr.peak_nits if hdr else spec.luminance_nits
        nits_label = f"{target_nits:g} nit peak" if spec.is_hdr else f"{target_nits:g} nits"
        digest = {"flow": flow, "target": target,
                  "colorspace": spec.colorspace, "transfer": transfer_label,
                  "white": f"{spec.white.intent} ({spec.white.method})",
                  "white_nits": target_nits, "patch_plan": self.calib["patch_plan"]}
        if hdr:
            digest["hdr_target"] = hdr.as_dict()
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key="resolve-target:plan", seam=SEAM_PLAN, stage="resolve-target",
            question=(f"Plan: {flow} calibration of monitor {self.monitor} "
                      f"({self.display.name}) to target '{target}' "
                      f"({transfer_label}, {spec.white.intent}, {nits_label}) — "
                      f"{patch_plan['total_patches']} patches "
                      f"({patch_plan['volumetric_mode']} volumetric, {patch_plan['order']} order). Proceed?"),
            options=("approve", "abort"), recommendation="approve", digest=digest)),
            stage="resolve-target", message="plan vetoed by the operator")
        self.calib["patch_plan"] = {**patch_plan, "approved": True}
        self._save()
        self.runlog.stage_done("resolve-target", target=target)
        self._emit_header()   # the target is now known — enrich the dashboard status bar
        digest["patch_plan"] = self.calib["patch_plan"]
        return StageOutcome("resolve-target", "done", digest=digest,
                            data={"target": target, "patch_plan": self.calib["patch_plan"]})

    def stage_whitepoint(self) -> StageOutcome:
        """Resolve the calibration-target white + its provenance (§9, §10; item 7) and
        persist it to the cross-run per-display correction store. SPD/white-point
        promoted to a **first-class early stage**: the SPD does double duty (the
        colorimeter correction *and* the SPD-derived "CRT-like" D65), and the resolved
        white flows into the MHC matrix (and its closed-loop D65 grayscale refine) and the
        3D-LUT target — both aim at the *same* white. No new ⚑ seam: the correction-staleness *tell*
        already fired in preflight; the white is reported (in the digest + report), not
        asked. Falls back to numeric D65 when no SPD is on hand, so it never blocks."""
        def run() -> StageOutcome:
            res = self._resolve_white_now()
            self.calib["white"] = res.as_dict()
            # Persist the white provenance WITHOUT clobbering a correction/SPD a
            # probe-match build (item 9) recorded: keep the prior store record's
            # correction_file/made/spd_file unless this run has newer data.
            store = self._correction_store()
            prior = store.get(self.display.name)
            corr = self.profile.meter.correction
            store.record(CorrectionRecord(
                display=self.display.name,
                correction_file=active_correction(self.profile, store, self.display.name),
                correction_made=(prior.correction_made if prior and prior.correction_made else corr.made),
                spd_file=res.spd_file or (prior.spd_file if prior else None) or self.display.white_spd,
                white_xy=[res.xy[0], res.xy[1]], white_provenance=res.provenance,
                observer=res.observer, anchor=res.anchor, strength=res.strength,
                updated=self.run_date.isoformat()))
            digest = {"white_xy": [round(res.xy[0], 5), round(res.xy[1], 5)],
                      "provenance": res.provenance, "method": res.method, "strength": res.strength,
                      "observer": res.observer, "anchor": res.anchor,
                      "cct": round(res.cct, 1) if res.cct is not None else None,
                      "duv": round(res.duv, 5) if res.duv is not None else None,
                      "spd_file": res.spd_file, "note": res.note}
            return StageOutcome("whitepoint", "done", digest=digest, data={"resolution": res.as_dict()})

        outcome = self._stage("whitepoint", run)
        # Cache the resolution for downstream stages (also after a memoised replay).
        if "white" not in self.calib and outcome.data.get("resolution"):
            self.calib["white"] = outcome.data["resolution"]
            self._save()
        return outcome

    # ====================================================================
    # Probe-match (SPD-correlation) GENERATION (item 9) — the build-correction step
    # ====================================================================
    def _probe_match_commands(self) -> dict[str, Any]:
        """Prepare the exact Argyll commands for a correction build, from the display's
        ``probe_match`` recipe — faithful to the proven ``create_ccmx.bat`` recipe
        (``ccxxmake -v -d N -y n -H -F -t s -I -E``) plus an optional white-SPD capture
        (double-duty for the SPD-derived white). The correction lands in the (durable)
        Argyll bin dir so it survives ``runs/`` prunes and is auto-discoverable."""
        pm = self.display.probe_match
        argyll_dir = self.profile.paths.get("argyll")
        bindir = Path(argyll_dir) if argyll_dir else (self.ctx.root / "probe_match")
        bindir.mkdir(parents=True, exist_ok=True)
        ccxxmake = str(Path(argyll_dir) / "ccxxmake.exe") if argyll_dir else "ccxxmake.exe"
        spotread = str(Path(argyll_dir) / "spotread.exe") if argyll_dir else "spotread.exe"
        name = pm.display_name or self.display.name
        safe = name.replace(" ", "_").replace("/", "_")
        mode_tag = "" if self.mode == "SDR" else f"_{self.mode}"
        suffix = ".ccss" if pm.kind == "ccss" else ".ccmx"
        ccmx_out = bindir / f"{safe}{mode_tag}-ColorChecker-i1Display3{suffix}"
        white_sp = bindir / f"{safe}{mode_tag}_white.sp"
        desc = f"DLC {name} {self.mode} {pm.kind.upper()} (ColorChecker Studio x i1 DisplayPro)"
        # ccxxmake — proven create_ccmx.bat flag set, plus per-panel patch-scale/settle
        # (mini-LED needs a ~fullscreen patch so local-dimming zones stay lit, and a settle
        # delay so the backlight/pixels stabilise before each read).
        cc: list[Any] = [ccxxmake, "-v", "-d", self.display.argyll_display]
        if pm.colorimeter_display_type:
            cc += ["-y", pm.colorimeter_display_type]
        if pm.high_res:
            cc.append("-H")
        cc.append("-F")
        if pm.patch_scale:                                  # -P ho,vo,ss: centered, scaled large ⇒ ~fullscreen
            cc += ["-P", f"0.5,0.5,{pm.patch_scale:g}"]
        if pm.settle_seconds and pm.settle_seconds > 0:
            # ccxxmake runs -C each time a colour is SET (before measuring); use it as a
            # per-patch settle. ping is the reliable Windows sleep (n pings ≈ n-1 s).
            pings = max(2, int(round(pm.settle_seconds)) + 1)
            # -w 1000 holds the delay even if loopback pings fail; >nul 2>&1 keeps the console clean.
            cc += ["-C", f"ping -n {pings} -w 1000 127.0.0.1 >nul 2>&1"]
        cc += ["-t", pm.display_tech]
        if pm.kind == "ccss":
            cc.append("-S")
        cc += ["-I", name, "-E", desc, str(ccmx_out)]
        # white-SPD capture with the spectrometer (one high-res emissive read on a white field).
        sp: list[Any] = [spotread, "-c", pm.spectro_port, "-e", "-x", "-H", "-O", str(white_sp)]
        return {"ccxxmake_argv": [str(a) for a in cc], "ccxxmake": _render_cmd(cc),
                "ccmx_out": str(ccmx_out), "white_spd_argv": [str(a) for a in sp],
                "white_spd_cmd": _render_cmd(sp), "white_sp": str(white_sp),
                "kind": pm.kind, "display_tech": pm.display_tech, "spectro_port": pm.spectro_port}

    def _ingest_correction(self, data: dict[str, Any]) -> None:
        """Ingest the operator-produced ``.ccmx`` (+ optional ``white.sp``) and persist it
        to the correction store as the **active** correction (overrides the profile)."""
        ccmx = Path(data["ccmx_out"])
        if not ccmx.exists() or ccmx.stat().st_size == 0:
            raise CalibrationAborted(StageOutcome(
                "probe-match", "aborted",
                digest={"message": f"expected correction not found at {ccmx} — did ccxxmake finish? "
                                   "Resume with --decide probe-match:build=done after it writes the file, "
                                   "or --decide probe-match:build=skip to keep the current correction."}))
        white_sp = Path(data.get("white_sp") or "")
        spd_ok = False
        if str(white_sp) and white_sp.exists() and white_sp.stat().st_size > 0:
            try:
                from .engine.whitepoint import load_sp
                load_sp(white_sp)   # validate it parses before we trust it
                spd_ok = True
            except Exception as exc:  # noqa: BLE001 - bad SPD is non-fatal; just skip it
                self.ctx.log(f"white SPD {white_sp} present but did not parse ({exc}); ignoring")
        store = self._correction_store()
        prior = store.get(self.display.name)
        store.record(CorrectionRecord(
            display=self.display.name,
            correction_file=str(ccmx),
            correction_made=self.run_date.isoformat(),
            spd_file=(str(white_sp) if spd_ok else (prior.spd_file if prior else None)),
            white_xy=(prior.white_xy if prior else None),
            white_provenance=(prior.white_provenance if prior else None),
            observer=(prior.observer if prior else None),
            anchor=(prior.anchor if prior else None),
            strength=(prior.strength if prior else None),
            updated=self.run_date.isoformat()))
        self.ctx.log(f"ingested correction {ccmx.name}"
                     + (f" + white SPD {white_sp.name} (SPD double-duty)" if spd_ok else ""))

    def stage_probe_match(self) -> StageOutcome:
        """Build (refresh) the colorimeter correction via Argyll ``ccxxmake`` — the
        SPD/probe-match GENERATION step. ``ccxxmake`` needs ONE continuous calibrated
        session (the spectrometer's white-tile calibration is held only while its process
        is open) and walks the operator through the instrument swap, so the core **launches
        it in its own console** (the operator types nothing) — the panel was already cleared
        to native by :meth:`stage_clear_native`. The operator follows the window's
        place→calibrate→measure→swap→measure prompts; on resume the core **ingests** the
        produced ``.ccmx`` → persists it to the store as the active correction. ``done``
        ingests, ``skip`` keeps the current correction, ``abort`` ends the build."""
        def run() -> StageOutcome:
            cmds = self._probe_match_commands()
            launch = self._probe_launcher(cmds)
            opened = bool(launch.get("launched"))
            lead = ("A measurement window has opened on this display — the core already cleared "
                    "DesktopLUT to native and started Argyll ccxxmake. You type nothing."
                    if opened else
                    f"Couldn't auto-open the measurement window ({launch.get('error', 'unknown')}). "
                    f"Fallback — run this once from the DLC dir: {cmds['ccxxmake']}")
            checklist = [
                lead,
                "Place the ColorChecker Studio spectrometer on its calibration tile; calibrate when the window prompts.",
                f"Set it to measurement (SENSOR) mode, lay it flat on the patch window on monitor "
                f"{self.monitor} ({self.display.name}); press the key the window asks for to measure RGBW.",
                "When the window says to SWAP, lift the spectrometer and set the i1 DisplayPro on the SAME spot, "
                f"then measure again — it writes {cmds['ccmx_out']}.",
                "Tell me when the window reports it's done — I'll ingest + record the correction "
                "(resume probe-match:build=done; =skip keeps the current correction).",
            ]
            digest = {"kind": cmds["kind"], "display_tech": cmds["display_tech"],
                      "spectro_port": cmds["spectro_port"], "ccxxmake": cmds["ccxxmake"],
                      "launched": opened, "launch": launch, "ccmx_out": cmds["ccmx_out"],
                      "white_sp": cmds["white_sp"], "checklist": checklist}
            return StageOutcome("probe-match", "done", digest=digest, data=cmds)

        outcome = self._stage("probe-match", run)
        decision = self._abort_if(self.adjudicate(AdjudicationRequest(
            key="probe-match:build", seam=SEAM_PROBE_MATCH, stage="probe-match",
            question=("Building the colorimeter correction: a ccxxmake window has opened (panel already "
                      "cleared to native). Operator places the spectrometer → calibrates → measures → "
                      "SWAPS to the i1 → measures. The place/calibrate/swap checklist is in digest.checklist. "
                      "Resume =done once the .ccmx is written and I'll ingest it."),
            options=("done", "skip", "abort"), recommendation="done", digest=outcome.digest)),
            stage="probe-match", message="aborted at the correction build")
        if decision.choice == "done":
            self._ingest_correction(outcome.data or outcome.digest)
        return outcome

    def _default_launch_ccxxmake(self, cmds: dict[str, Any]) -> dict[str, Any]:
        """Launch Argyll ``ccxxmake`` in its OWN console window so the operator interacts with
        its place/calibrate/measure/swap prompts directly — the core opens it; nothing is typed
        by hand. ``ccxxmake`` outlives this (paused) orchestrator and writes the ``.ccmx``; the
        operator then resumes and the core ingests it. Best-effort: a spawn failure is surfaced
        in the digest (the rendered command is the fallback) rather than crashing the build."""
        import subprocess
        argv = [str(a) for a in cmds["ccxxmake_argv"]]
        root = Path(self.profile.source_path).parent if self.profile.source_path else Path.cwd()
        try:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            proc = subprocess.Popen(argv, cwd=str(root), creationflags=flags)
            self.ctx.log(f"launched ccxxmake (pid {proc.pid}) in a new console; cwd={root}")
            return {"launched": True, "pid": proc.pid, "new_console": bool(flags), "cwd": str(root)}
        except Exception as exc:  # noqa: BLE001 - surfaced; the rendered command is the fallback
            self.ctx.log(f"could not launch ccxxmake ({type(exc).__name__}: {exc}); see digest fallback")
            return {"launched": False, "error": f"{type(exc).__name__}: {exc}"}

    def stage_clear_native(self) -> StageOutcome:
        """Clear DesktopLUT's corrections on this monitor so the correction is built against the
        panel's NATIVE emission — the operator never clears by hand, the core does it over the
        calibration pipe. Best-effort: if the pipe is unreachable it's surfaced (the panel may
        already be native / unmanaged), not fatal, so the build can still proceed."""
        def run() -> StageOutcome:
            try:
                res = self.controller.enter_neutral(self.monitor, self.mode, self.dummy_icc,
                                                    reason="DLC build-correction: native panel for CCMX")
                return StageOutcome("clear-native", "done",
                                    digest={"cleared": True, "via": "enter_neutral"},
                                    data={"raw": _jsonable(res)})
            except Exception as exc:  # noqa: BLE001 - a down pipe shouldn't crash the build
                return StageOutcome("clear-native", "done",
                                    digest={"cleared": False, "error": f"{type(exc).__name__}: {exc}",
                                            "note": "calibration pipe unreachable — verify the panel is at "
                                                    "native (OSD standard/native mode, no external LUT) before measuring."},
                                    data={})
        return self._stage("clear-native", run)

    def stage_enter_neutral(self) -> StageOutcome:
        def run() -> StageOutcome:
            res = self.controller.enter_neutral(self.monitor, self.mode, self.dummy_icc,
                                                reason="DLC v2 calibration")
            return StageOutcome("enter-neutral", "done",
                                digest={"entered": True}, data={"raw": _jsonable(res)})
        return self._stage("enter-neutral", run)

    # ====================================================================
    # Characterize (Display+Instrument Profile GENERATION) — the learning run
    # ====================================================================
    def stage_characterize(self) -> StageOutcome:
        """Learn how THIS panel + meter behave together and persist a Display+Instrument
        Profile (DIP) — the run that *produces* the priors the measure loop's read policy
        consumes. NOT a calibration: nothing is built or applied (the panel was already
        cleared to native by :meth:`stage_clear_native`). Measures the three axes via the
        single measure seam — instrument noise vs luminance, display settle + native
        white/black/primaries, warm-up/drift — writes ``characterize.ndjson``, stamps the
        DIP with this display/date/instrument/correction and upserts it into the store.
        Abnormal panel/meter behaviour is FLAGGED for review (a non-aborting seam), never
        silently capped or swallowed."""
        def run() -> StageOutcome:
            transfer = self._transfer()
            cfg = self.characterize_config or CharacterizeConfig()
            meas_dir = self.ctx.root / "measurements"
            # Characterize reads the panel directly (not via the instrumented measure loop), so
            # without this wrapper its long warm-up sweep never resets the stall clock and the
            # live-CLI watchdog would force-kill the meter mid-characterization (~20 min). Wrap
            # the meter so each read arms the guard (real stall still aborts) and each good read
            # registers progress. The threshold is generous: characterize HAS no DIP yet (it's
            # producing one) and warm-up dwells can be long between reads.
            self.liveness.set_stall_after(max(self.liveness.stall_after_s, 900.0))
            live = self.liveness

            def instrumented_measure(patch: MeasurePatch) -> Reading:
                live.activity("characterize")
                live.check("characterize")
                reading = self.measure(patch)
                if reading.ok:
                    live.progress("characterize")
                return reading

            result = run_characterization(
                measure=instrumented_measure, transfer=transfer, config=cfg,
                cold_channel=self.display.temperamental_channel,
                display=self.display.name,
                events=EventWriter(self.ctx.events_path),
                ndjson_path=meas_dir / "characterize.ndjson")
            # Stamp the provenance the store keys staleness + meter-pairing on, then persist.
            store = self._dip_store()
            dip = replace(result.dip,
                          display=self.display.name,
                          mode=self.mode,           # store keyed by display:mode (SDR/HDR coexist)
                          instrument=self.profile.meter.model,
                          correction_file=active_correction(self.profile, store, self.display.name),
                          made=self.run_date.isoformat(),
                          updated=self.run_date.isoformat())
            # NOTE: the DIP keeps its OWN staleness clock (DisplayInstrumentProfile.is_stale's
            # 180-day default) — panel+meter behaviour drift is a different clock than the
            # colorimeter correction's age, so we deliberately do NOT inherit correction.max_age_days.
            store.record(dip)
            self.ctx.log(f"characterized {self.display.name}: {len(dip.noise_model)} noise band(s), "
                         f"cold channel {dip.cold_channel}, "
                         f"settle {dip.settle_seconds}s — DIP → {store.path.name}")
            return StageOutcome("characterize", "done", digest=result.digest,
                                data={"needs_adjudication": result.needs_adjudication,
                                      "question": result.question, "flags": result.flags,
                                      "ndjson": result.ndjson_path,
                                      "dip_store": str(store.path)},
                                artifacts=[p for p in (result.ndjson_path,) if p])

        outcome = self._stage("characterize", run)
        if outcome.data.get("needs_adjudication"):
            # Non-aborting: the learned DIP is still useful priors (the loop flags per-patch
            # regardless), so surface the abnormality for judgment without discarding it. An
            # explicit 'abort' decision is honoured; the default accepts the profile.
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="characterize:review", seam=SEAM_CHARACTERIZE, stage="characterize",
                question=outcome.data.get("question")
                or "characterization surfaced abnormal panel/meter behaviour — accept the learned profile or recharacterize?",
                options=("accept", "abort"), recommendation="accept",
                # This seam is ONLY reached when characterization flagged abnormal panel/meter
                # behaviour, so mark it compromised: an unattended (supervised) run must escalate
                # rather than auto-accept a bad DIP that becomes every future run's read policy.
                digest={**{k: outcome.digest.get(k) for k in
                           ("flags", "warm", "cold_channel", "settle_seconds", "noise_floor_nits")},
                        "compromised": True})),
                stage="characterize", message="characterization rejected at review")
        return outcome

    def stage_brightness(self) -> StageOutcome:
        """Brightness-to-target: the core reads white; the human turns the OSD until
        in range (DesktopLUT can't drive the backlight). The seam kicks it off and is
        told the result — in auto/sim there's no human, so the current reading stands."""
        def run() -> StageOutcome:
            import time
            transfer = self._transfer()
            white_patch = MeasurePatch(label="white", rgb=(transfer.max_cv,) * 3,
                                       signal=(1.0, 1.0, 1.0), role="measurement",
                                       bit_depth=transfer.bit_depth)
            # enter-neutral reconfigures the scanout (ICC / calibration mode), which can briefly
            # blank the panel OUTPUT even though the patch window stays white — so this single
            # read can land in that transient → 0.0. Re-read until real light returns (the
            # streamed measure stages have their own warm-up; this one-shot needs its own).
            reading = self.measure(white_patch)
            nits = reading.nits or 0.0
            attempts = 1
            while nits <= 1.0 and attempts < 6:
                time.sleep(2.0)
                reading = self.measure(white_patch)
                nits = reading.nits or 0.0
                attempts += 1
            target = self._spec().luminance_nits
            # HDR peak luminance is fixed by the panel (PQ is absolute-luminance-encoded; the
            # OSD backlight cannot retarget a single point), so there is nothing for the human
            # to adjust — the brightness seam does not apply. The panel-limits tell already
            # surfaces a peak below the target. SDR: the human drives the OSD to the target.
            in_range = True if self._spec().is_hdr else abs(nits - target) <= max(3.0, 0.05 * target)
            # A near-zero white means the panel is dark/asleep (off / wrong input / patch window
            # not showing) — caught even for HDR, where in_range is otherwise forced true (the OSD
            # can't retarget a PQ peak) and a 0-nit white would slip straight through to measuring.
            panel_dark = nits is not None and nits < 1.0
            # A gross luminance miss (>25% off target) flags compromised so a supervised run
            # escalates — there is no OSD operator unattended, so a wildly-wrong backlight is a
            # judge's abort/accept call, not an auto-accept. A modest miss (the panel just can't
            # land exactly) stays benign and auto-accepts.
            gross_miss = bool(not in_range and target and abs(nits - target) > 0.25 * target)
            digest = {"white_nits": round(nits, 2), "target_nits": target,
                      "in_range": in_range, "read_attempts": attempts, "panel_dark": panel_dark,
                      "hdr_fixed_peak": self._spec().is_hdr, "compromised": gross_miss or panel_dark}
            return StageOutcome("brightness", "done", digest=digest,
                                data={"white_nits": nits, "in_range": in_range, "panel_dark": panel_dark})

        outcome = self._stage("brightness", run)
        panel_dark = bool(outcome.data.get("panel_dark"))
        if panel_dark:
            self.runlog.anomaly(
                "brightness", panel_dark=True, white_nits=outcome.digest.get("white_nits"),
                message=(f"white reads {outcome.digest.get('white_nits')} nits — panel appears "
                         "dark/asleep (off / wrong input / patch window not showing)"))
        if panel_dark or not outcome.data.get("in_range"):
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="brightness:adjust", seam=SEAM_BRIGHTNESS, stage="brightness",
                question=((f"white reads {outcome.digest['white_nits']} nits — the panel appears "
                           "dark/asleep, nothing to calibrate. Wake it / check the input + that the "
                           "patch window is showing, then retry — or abort?")
                          if panel_dark else
                          (f"white reads {outcome.digest['white_nits']} nits vs target "
                           f"{outcome.digest['target_nits']:g} — have the human set the OSD backlight, "
                           "or accept this level?")),
                options=("accept", "abort"),
                recommendation=("abort" if panel_dark else "accept"), digest=outcome.digest)),
                stage="brightness",
                message=("aborted — panel dark at brightness" if panel_dark
                         else "aborted on out-of-range white luminance"))
        return outcome

    def stage_hardware_readiness(self) -> StageOutcome:
        """One operator/LLM gate before the first live meter read."""
        key = "hardware-readiness"
        if not self.require_hardware_readiness:
            return StageOutcome(key, "done", digest={"required": False})

        def run() -> StageOutcome:
            decision = self.adjudicate(AdjudicationRequest(
                key=f"{key}:confirm", seam=SEAM_HARDWARE_READY, stage=key,
                question=(
                    "Before the first meter read: is the meter aimed at the patch area, "
                    "is DogeGen visible/foregrounded on the target display, and are there "
                    "no windows or overlays covering the sensor? Choose ready to begin, "
                    "or abort to fix the setup."
                ),
                options=("ready", "abort"), recommendation="ready",
                digest={"required": True, "monitor": self.monitor, "mode": self.mode,
                        "bit_depth": self.bit_depth, "dogegen_required": True}))
            if decision.choice == "abort":
                raise CalibrationAborted(StageOutcome(
                    key, "aborted",
                    digest={"message": "hardware readiness aborted by operator/LLM",
                            "decision_note": decision.note}))
            return StageOutcome(key, "done",
                                digest={"required": True, "confirmed": True,
                                        "decision_note": decision.note})

        return self._stage(key, run)

    def stage_measure(self, *, role: str, patches: Sequence[tuple[int, int, int]],
                      ti3_name: str, ndjson_name: str) -> StageOutcome:
        key = f"measure:{role}"

        def run() -> StageOutcome:
            res = self._measure_set(patches, role=role, ti3_name=ti3_name, ndjson_name=ndjson_name)
            return StageOutcome(key, "done", digest=res.digest,
                                data={"ti3": res.ti3_path, "ndjson": res.ndjson_path,
                                      "white_xyz": list(res.white_xyz) if res.white_xyz else None,
                                      "needs_adjudication": res.needs_adjudication,
                                      "question": res.question, "warm": res.warm},
                                artifacts=[p for p in (res.ti3_path, res.ndjson_path) if p])

        outcome = self._stage(key, run)
        collapse = self._measurement_foundation_collapse(role, outcome)
        if collapse is not None:
            # DETECT is mechanics; the DECISION is a seam. A collapsed post-foundation
            # luminance envelope means the 3D-LUT would optimize on top of a broken state —
            # recommend abort (so --auto/supervised stop the disaster) but let a live judge
            # retry/accept with the full digest (it may know the read was a transient).
            self.runlog.anomaly(key, **collapse)
            # Options are abort/accept only: "retry the foundation" is not offered because the
            # foundation stages are already memoised done and nothing re-installs them on resume,
            # so a retry would re-abort forever. A judge that believes the read was a transient
            # accepts (and the next fresh run re-measures); otherwise abort and fix the foundation.
            decision = self.adjudicate(AdjudicationRequest(
                key=f"{key}:foundation", seam=SEAM_FOUNDATION, stage=key,
                question=(collapse["message"] + " — abort, or accept and continue?"),
                options=("abort", "accept"), recommendation="abort",
                digest={**collapse, "foundation_critical": True}))
            if decision.choice == "abort":
                self.runlog.stage_aborted(key, message=collapse["message"])
                raise CalibrationAborted(StageOutcome(
                    key, "aborted", digest={**collapse, "decision_note": decision.note}))
            self.ctx.log(f"foundation collapse at {key} ACCEPTED at the seam: {decision.note}")
        # Before→after dE trend on the spine (#8): score the INTERMEDIATE measures so the
        # dashboard's ΔE panel + de_history show the run converging (native → after ICC → after
        # 3D LUT) instead of a single verify point. verify does its own richer scoring at the gate.
        if role in ("raw", "post-mhc") and outcome.status == "done":
            score_anomaly = self._score_stage(
                role, outcome.data.get("ti3"),
                label="raw (native)" if role == "raw" else "after ICC")
            if score_anomaly:
                outcome.digest["score_anomaly"] = True
                outcome.digest["score_anomaly_detail"] = score_anomaly
                outcome.digest["read_anomaly"] = True
                reasons = list(outcome.digest.get("anomaly_reasons") or [])
                if "score_anomaly" not in reasons:
                    reasons.append("score_anomaly")
                outcome.digest["anomaly_reasons"] = reasons
                outcome.data["needs_adjudication"] = True
                base_q = outcome.data.get("question")
                score_q = (
                    f"measured patch set has catastrophic {score_anomaly['metric']} errors "
                    f"(avg {score_anomaly['avg_de2000']}, p95 {score_anomaly['p95_de2000']}, "
                    f"max {score_anomaly['max_de2000']}); data needs adjudication"
                )
                outcome.data["question"] = f"{score_q}; {base_q}" if base_q else score_q
        if outcome.data.get("needs_adjudication"):
            panel_dark = bool(outcome.digest.get("panel_dark"))
            if panel_dark:
                # Loud, immediate anomaly on the spine (dashboard ATTENTION + LLM digest) — a dark
                # panel must never be a silent "accept", in any mode.
                self.runlog.anomaly(
                    key, panel_dark=True, reference_nits=outcome.digest.get("dark_reference_nits"),
                    message=("panel appears dark/asleep — mid-grey reference read "
                             f"{outcome.digest.get('dark_reference_nits')} cd/m²; no patches measured"))
            preheat_compromised = bool(outcome.digest.get("preheat_compromised"))
            measurement_path_compromised = bool(outcome.digest.get("measurement_path_compromised"))
            score_anomaly = bool(outcome.digest.get("score_anomaly"))
            # A dark panel, compromised preheat, or a blown remeasure/drift budget is non-benign:
            # recommend retry (not accept), offer it, and flag compromised so SupervisedAdjudicator
            # escalates rather than rubber-stamping black/garbage data.
            retry_recommended = bool(outcome.digest.get("remeasure_budget_exceeded")
                                     or outcome.digest.get("drift_density_exceeded")
                                     or panel_dark
                                     or preheat_compromised
                                     or measurement_path_compromised)
            options = (("accept", "suppress", "remeasure", "retry", "abort")
                       if (retry_recommended or score_anomaly) else ("accept", "suppress", "abort"))
            decision = self.adjudicate(AdjudicationRequest(
                key=f"{key}:escalation", seam=SEAM_MEASURE, stage=key,
                question=outcome.data.get("question") or "measurement did not fully settle - accept or retry?",
                options=options,
                recommendation=("retry" if retry_recommended else "accept"),
                digest={**outcome.digest,
                        "compromised": (panel_dark or preheat_compromised
                                        or measurement_path_compromised
                                        or score_anomaly)}))
            if decision.choice == "remeasure":
                self.calib["stages"].pop(key, None)
                self.calib.get("decisions", {}).pop(f"{key}:escalation", None)
                self.decision_overrides.pop(f"{key}:escalation", None)
                self._save()
                return self.stage_measure(role=role, patches=patches,
                                          ti3_name=ti3_name, ndjson_name=ndjson_name)
            if decision.choice == "retry":
                raise CalibrationAborted(StageOutcome(
                    key, "aborted",
                            digest={"message": "measurement retry requested at LLM seam",
                                    "retry_requested": True, **outcome.digest}))
            self._abort_if(decision, stage=key, message="aborted on unsettled measurement")
        return outcome

    def _measurement_foundation_collapse(self, role: str, outcome: StageOutcome) -> Optional[dict[str, Any]]:
        """Detect a collapsed correction foundation before the long 3D-LUT build.

        After a foundation install the measured bright neutral must stay in the same luminance
        envelope as raw/brightness/target; if it does not, downstream 3D-LUT work would optimize
        on top of a broken hardware/profile state. Returns the **evidence digest** when the
        envelope collapsed (the caller adjudicates a :data:`SEAM_FOUNDATION` seam), else ``None``.
        Detection only — the decision is the seam's, not a unilateral abort here.
        """
        if role != "post-mhc" or outcome.status != "done":
            return None
        digest = outcome.digest or {}
        white = _as_float_local(digest.get("white_nits"))
        if white is None or white <= 0:
            return None
        refs = self._foundation_reference_nits()
        if not refs:
            return None
        ref = max(refs)
        ratio = white / ref if ref > 0 else 1.0
        preheat = digest.get("preheat") or {}
        compromised = bool(preheat.get("compromised"))
        baseline_distance = _as_float_local(preheat.get("baseline_distance")) or 0.0
        critical = ratio < 0.55 or (compromised and ratio < 0.75) or (compromised and baseline_distance >= 0.08)
        if not critical:
            return None
        return {
            "message": (f"post-foundation white collapsed to {white:.1f} nits "
                        f"({ratio:.2f}x of {ref:.1f} nits reference) before 3D-LUT build"),
            "white_nits": round(white, 3),
            "reference_white_nits": round(ref, 3),
            "white_ratio": round(ratio, 4),
            "preheat_compromised": compromised,
            "baseline_distance": baseline_distance,
        }

    def _score_stage(self, role: str, ti3_path: Optional[str], *, label: str) -> Optional[dict[str, Any]]:
        """Score an intermediate measure stage against the resolved target and put a
        ``metrics_scored`` digest on the spine — so the dashboard's ΔE panel + de_history show
        the calibration converging stage by stage (a single verify point was uninformative).
        Advisory: a missing/empty TI3 or any scoring hiccup is swallowed, never breaks the flow."""
        if not ti3_path:
            return None
        try:
            p = Path(ti3_path)
            if not p.exists():
                return None
            samples = parse_ti3(p)
            if not samples:
                return None
            spec = self._spec()
            wx, wy = self._white_xy()
            # HDR scores dE_ITP vs PQ/Rec.2020; SDR CIEDE2000 vs γ-power. Scoring HDR PQ data
            # as an SDR power target (the old unconditional path) produced garbage dE2000 (~30+
            # at mid-gray) on the dashboard's convergence trend — branch like stage_verify.
            if spec.is_hdr:
                metrics, lum = score_samples_hdr(samples, white_xy=(wx, wy),
                                                 peak_nits=self._hdr_target().peak_nits,
                                                 reachable_primaries=self._reachable_primaries())
                metric_name = "dE_ITP"
            else:
                metrics, lum = score_samples(samples, gamma=spec.gamma, white_xy=(wx, wy))
                metric_name = "CIEDE2000"
            summary = summarize_metrics(phase=label, iteration=0, source=p,
                                        patch_metrics=metrics, target_luminance=lum, metric=metric_name)
            colour_de = [m.de2000 for m in metrics if not m.grayscale]
            all_de = [m.de2000 for m in metrics]
            p99 = percentile(all_de, 99.0)
            # Snapshot for the timed check-in's live metrics (most recent intermediate score).
            self._last_scored = {"label": label, "metric": metric_name,
                                 "avg": round(summary.avg_de2000, 3), "max": round(summary.max_de2000, 3),
                                 "white": round(summary.white_de2000, 3)}
            self.runlog.metrics_scored(
                f"measure:{role}", label=label, iteration=0, metric=metric_name,
                avg_de2000=round(summary.avg_de2000, 3), p95_de2000=round(summary.p95_de2000, 3),
                p99_de2000=round(p99, 3),
                max_de2000=round(summary.max_de2000, 3), white_de2000=round(summary.white_de2000, 3),
                grayscale_avg_de2000=round(summary.grayscale_avg_de2000, 3),
                colour_avg_de2000=(round(sum(colour_de) / len(colour_de), 3) if colour_de else None),
                patch_count=summary.patch_count, grayscale_count=summary.grayscale_count)
            worst = sorted(metrics, key=lambda m: m.de2000, reverse=True)[:5]
            high_spikes = [m for m in metrics if m.de2000 >= 100.0]
            high_fraction = len(high_spikes) / len(metrics) if metrics else 0.0
            catastrophic_distribution = (
                bool(high_spikes)
                and (summary.avg_de2000 >= 100.0 or high_fraction >= 0.25)
            )
            patch_spike = bool(high_spikes) and not catastrophic_distribution
            if catastrophic_distribution or patch_spike:
                reason = "catastrophic_delta_e_distribution"
                if patch_spike:
                    reason = ("single_patch_delta_e_spike" if len(high_spikes) == 1
                              else "localized_patch_delta_e_spike")
                anomaly = {
                    "reason": reason,
                    "role": role,
                    "label": label,
                    "metric": metric_name,
                    "avg_de2000": round(summary.avg_de2000, 3),
                    "p95_de2000": round(summary.p95_de2000, 3),
                    "p99_de2000": round(p99, 3),
                    "max_de2000": round(summary.max_de2000, 3),
                    "white_de2000": round(summary.white_de2000, 3),
                    "high_spike_count": len(high_spikes),
                    "high_spike_fraction": round(high_fraction, 4),
                    "patch_count": summary.patch_count,
                    "worst": [{"rgb": [round(c, 4) for c in m.rgb],
                               "de2000": round(m.de2000, 3)} for m in worst],
                }
                self.runlog.anomaly(
                    f"measure:{role}",
                    kind="score_anomaly",
                    **anomaly,
                    message=(
                        "measured patch set is catastrophically far from the target; "
                        "the measurement path requires adjudication"
                    ),
                )
                return anomaly
            return None
        except Exception:  # noqa: BLE001 - advisory telemetry; a scoring hiccup never breaks the flow
            return None

    def stage_build_install_mhc(self, raw_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            spec = self._spec()
            # Derive MHC params from the raw TI3 (reuses the proven build-mhc stage:
            # measured primaries + native-white→target-white matrix + tone-only base 1D).
            args = Namespace(run=self.ctx.root, monitor=self.monitor, mode=self.mode,
                             simulate=False, gamma=spec.gamma, source_ti3=raw_ti3,
                             is_hdr=spec.is_hdr)
            if spec.is_hdr:
                # ONE SOURCE OF TRUTH (Task C): hand build-mhc the SAME resolved max-sustained peak
                # patch bounding uses (_patch_max_cv → _hdr_target().peak_nits), so the MHC cube
                # ceiling + the C++ set_base_lut handoff agree with the patch set instead of
                # re-deriving an independent raw-TI3 max. Skip an ungrounded cold-start placeholder
                # (no DIP measured yet) — there the stage's own raw-TI3 max is the real measurement.
                hdr = self._hdr_target()
                if (hdr.provenance.get("peak") or {}).get("grounded", True):
                    args.resolved_peak_nits = hdr.peak_nits
            derive = build_mhc.build(args, self.ctx)
            self.ctx.log(f"build-mhc: {derive.status}")
            if derive.status == "failed":
                raise CalibrationAborted(StageOutcome(
                    "build-install-mhc", "aborted",
                    digest={"message": f"build-mhc failed: {derive.anomalies}"}))
            # build-mhc persisted mhc_params into dlc_state — reload it.
            self._state = _common.load_dlc_state(self.ctx)
            self.calib = self._state.setdefault("calib", self.calib)
            params = self._state.get("mhc_params") or {}
            base = params["base_grayscale"]
            base_lut = params.get("base_lut")
            # Install through the controller (set primaries/white → base correction → apply → verify).
            install_primaries = params["primaries"]
            # OBSOLETE — native targeting is now the C++ DEFAULT for HDR. As of 2026-06-23,
            # GenerateMHC2Profile (mhc_icc.cpp) sets srcPrim=native (panel's MEASURED primaries) for
            # HDR, so MHC2 = inv(displayToXYZ)·srcToXYZ is already a pure diagonal white-only move
            # (native white → D65, gamut identity) computed in the *native* basis — strictly better
            # than this hook's BT.2020-basis approximation. So the normal path (pushing the measured
            # native primaries below) now produces the native target directly. The DLC_SRC_NATIVE=1
            # validation hook (which pushed BT.2020 as the *display* primaries on the old hardcoded-
            # Rec.2020-src C++) is RETIRED: it would shadow and degrade the now-correct C++ default.
            # Kept only as a logged tripwire so a stale env var can't silently change behavior.
            # See the mhc-blue-red-channel-collapse memo + GenerateMHC2Profile's source-primaries note.
            if spec.is_hdr and os.environ.get("DLC_SRC_NATIVE") == "1":
                self.ctx.log("DLC_SRC_NATIVE=1 is OBSOLETE and now a NO-OP: native targeting is the "
                             "C++ default (mhc_icc.cpp GenerateMHC2Profile). Ignoring; installing the "
                             "measured native primaries (the correct native-basis target). Unset the var.")
            self.controller.set_primaries(self.monitor, self.mode, install_primaries)
            white = self._resolved_white()
            wx, wy = white.xy
            # set_white populates DesktopLUT's customPrimaries.W — the MEASURED *display*
            # characterization white, NOT the target. The MHC matrix is
            # srcToXYZ(standard @ D65) · inv(displayToXYZ(measured primaries, displayPrim.W)),
            # so white adaptation is the normalization difference between the fixed src white
            # (D65, baked into g_bt2020/g_srgb srcPrim) and displayPrim.W. Sending the TARGET
            # (D65) here makes displayPrim.W == src white ⇒ ZERO white adaptation ⇒ the panel's
            # native white passes straight through (HW evidence 2026-06-20: peak white stayed at
            # native ~0.324 in both HDR runs). The matrix can only correct native→D65 if it knows
            # the panel's measured white — so BOTH modes now send it (aligning with the standalone
            # install_mhc.py). The 1+1+1 standalone-D65 design (Task B / #C1): the MATRIX owns the
            # bulk native→D65 move (robust 3×3, no full-input channel clamp), the native-white base
            # 1D LUT/grayscale owns per-channel tone, and the closed-loop refine (stage_refine_mhc_*)
            # corrects the per-level non-additivity RESIDUAL toward D65. (SDR previously sent the
            # target white here, leaving the whole white move on the grayscale — the open-loop limp
            # the closed-loop refine now replaces.) See mhc_icc.cpp ComputeMHC2Matrix.
            mw = (params.get("measured_white") or {})
            if mw.get("x") is not None and mw.get("y") is not None:
                self.controller.set_white(self.monitor, self.mode, mw["x"], mw["y"])
            else:
                self.controller.set_white(self.monitor, self.mode, wx, wy)
            # Base EOTF/tone rides a full-resolution per-channel 1D .cube (set_base_lut → 4096-entry HDR /
            # 1024-entry SDR MHC2 LUT). BOTH modes now use it (2026-06-24): the cube is a DLC-owned base
            # artifact that locks DesktopLUT's grayscale editor + Reset button, so the closed-loop refine
            # never squats in the user-editable correctionGrayscale slot ([[dlc-must-not-own-mhc-user-layers]]).
            # The 32-point set_base_grayscale table survives only as the fallback when no cube was built
            # (e.g. <2 neutral patches).
            if base_lut and base_lut.get("cube_path"):
                # HDR peak_nits = the cube's post-cap NEUTRAL ceiling (achievable-D65 Peak-Chroma cap),
                # the number a future DesktopLUT `tonemapTargetPeak` IPC (Task E4) tracks. SDR's 1024-entry
                # LUT carries no HDR luminance metadata, so peak_nits is 0.0 there.
                self.controller.set_base_lut(self.monitor, self.mode, base_lut["cube_path"],
                                             base_lut.get("peak_nits", 0.0))
                # Clear any legacy non-identity correctionGrayscale (a prior SDR run's refine slot): the
                # bake stacks it INDEPENDENTLY of the cube, and the cube now owns the whole neutral correction.
                ncg = 32
                gridcg = [j / (ncg - 1) for j in range(ncg)]
                self.controller.set_correction_grayscale(
                    self.monitor, self.mode, ncg, gridcg,
                    {ch: [1.0] * ncg for ch in ("r", "g", "b")}, gamma=spec.gamma)
            else:
                self.controller.set_base_grayscale(self.monitor, self.mode, base["point_count"],
                                                   base["points"], base["deviations"], gamma=spec.gamma)
            applied = self.controller.apply_mhc(self.monitor, self.mode)
            verified = self.controller.verify_mhc(self.monitor, self.mode)
            params["white"] = {"x": round(wx, 6), "y": round(wy, 6)}
            params["white_source"] = white.provenance
            self._state["mhc_params"] = params
            _common.save_dlc_state(self.ctx, self._state)
            profile_name = applied.get("profile_name") if isinstance(applied, dict) else None
            verify_ok = bool(verified.get("verified")) if isinstance(verified, dict) else False
            digest = {"primaries": params["primaries"], "white_xy": [wx, wy],
                      "white_provenance": white.provenance,
                      "measured_white": params.get("measured_white"),
                      "white_de_vs_d65": derive.metrics.get("measured_white_de2000_vs_d65"),
                      "profile_name": profile_name, "verified": verify_ok}
            if spec.is_hdr and params.get("peak_chroma"):
                # Standalone-D65 evidence: the cold-channel-limited Peak-Chroma luminance the
                # closed-loop refine will hold D65 to (see stage_refine_mhc_cube).
                digest["peak_chroma"] = params["peak_chroma"]
            sanity = self._mhc_foundation_sanity_check()
            if sanity:
                digest["sanity"] = sanity
            return StageOutcome("build-install-mhc", "done", digest=digest,
                                data={"profile_name": profile_name, "verified": verify_ok})

        outcome = self._stage("build-install-mhc", run)
        sanity = (outcome.digest or {}).get("sanity") or {}
        if sanity.get("critical"):
            # The MHC install succeeded (memoised done) but its immediate bright-neutral read
            # collapsed — DETECT here, DECIDE at the seam. Recommend abort so --auto/supervised
            # stop before the cube build; a live judge can accept if it knows the read was a
            # transient (e.g. the scanout reconfigured mid-read).
            decision = self.adjudicate(AdjudicationRequest(
                key="build-install-mhc:foundation", seam=SEAM_FOUNDATION, stage="build-install-mhc",
                question=((sanity.get("message") or "the MHC foundation read collapsed bright-neutral luminance")
                          + " — abort and recheck the MHC, or accept and continue?"),
                options=("abort", "accept"), recommendation="abort",
                digest={**{k: outcome.digest.get(k) for k in ("profile_name", "white_xy", "verified")},
                        "sanity": sanity, "foundation_critical": True}))
            if decision.choice == "abort":
                self.runlog.stage_aborted("build-install-mhc", message=sanity.get("message"))
                raise CalibrationAborted(StageOutcome(
                    "build-install-mhc", "aborted",
                    digest={**outcome.digest, "message": sanity.get("message"),
                            "recommendation": "abort_and_recheck_mhc", "decision_note": decision.note}))
            self.ctx.log(f"MHC foundation sanity critical but ACCEPTED at the seam: {decision.note}")
        return outcome

    def _mhc_foundation_sanity_check(self) -> dict[str, Any]:
        """Immediately read a bright neutral after MHC apply.

        This is a cheap invariant check before the dense post-MHC measurement: applying a
        foundation profile must not collapse the display's bright-neutral luminance.
        """
        transfer = self._transfer()
        cv = self._patch_max_cv() or transfer.max_cv
        signal = cv / transfer.max_cv if transfer.max_cv else 1.0
        patch = MeasurePatch(label="post-mhc-white-sanity", rgb=(cv, cv, cv),
                             signal=(signal, signal, signal), role="neutral_ref",
                             bit_depth=transfer.bit_depth)
        try:
            reading = self.measure(patch)
        except Exception as exc:  # noqa: BLE001
            return {"checked": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = bool(reading.ok and reading.xyz is not None)
        self.runlog.patch_read(
            "build-install-mhc", seq=-1, role="neutral_ref", label=patch.label,
            rgb=list(patch.rgb), signal=[round(signal, 5)] * 3,
            Y=(round(reading.xyz[1], 4) if ok else None),
            xy=_reading_xy(reading), ok=ok, disposition="foundation_sanity")
        if not ok:
            return {"checked": True, "ok": False, "critical": True,
                    "message": f"MHC sanity read failed: {reading.error or 'no XYZ reading'}"}
        nits = float(reading.xyz[1])
        refs = self._foundation_reference_nits()
        if not refs:
            return {"checked": True, "ok": True, "white_nits": round(nits, 3)}
        ref = max(refs)
        ratio = nits / ref if ref > 0 else 1.0
        critical = ratio < 0.55
        message = (
            f"MHC sanity white collapsed to {nits:.1f} nits ({ratio:.2f}x of "
            f"{ref:.1f} nits reference)"
        ) if critical else None
        return {"checked": True, "ok": True, "white_nits": round(nits, 3),
                "reference_white_nits": round(ref, 3), "white_ratio": round(ratio, 4),
                "critical": critical, "message": message}

    def _foundation_reference_nits(self) -> list[float]:
        # The post-foundation white is read at the (HDR-capped) target drive level, so its
        # reference must be a SAME-LEVEL bright neutral. measure:raw is measured at that same
        # capped level. brightness is measured at FULL signal (= the panel's NATIVE peak); for an
        # HDR panel whose native peak far exceeds the capped target (a common mini-LED case) that
        # would make a perfectly healthy post-MHC white look "collapsed" (e.g. 999 vs 1840 nits ⇒
        # ratio 0.54), so brightness is NOT a valid HDR reference — use raw + the target peak only.
        spec = None
        try:
            spec = self._spec()
        except Exception:  # noqa: BLE001
            spec = None
        is_hdr = bool(spec and spec.is_hdr)
        refs: list[float] = []
        for stage_key in (("measure:raw",) if is_hdr else ("measure:raw", "brightness")):
            d = ((self.calib["stages"].get(stage_key) or {}).get("digest") or {})
            ref = _as_float_local(d.get("white_nits"))
            if ref and ref > 0:
                refs.append(ref)
        try:
            target = self._hdr_target().peak_nits if is_hdr else (spec.luminance_nits if spec else None)
            if target and target > 0:
                refs.append(float(target))
        except Exception:  # noqa: BLE001
            pass
        return refs

    def stage_adaptive_planning(self, *, raw_ti3: Optional[str]) -> None:
        """The **opt-in LLM patch-strategy investigation seam** (#47/#49), post-ICC.

        OFF unless ``--adaptive-planning`` ⇒ the deterministic plan, no seam, no evidence
        gathering. ON: assemble an evidence packet of raw facts (DIP, gamut, raw-tone, ICC
        residual, plan/time estimate, prior runs, cache state), then let the **LLM** decide
        the shadow + volumetric patch strategy (it investigates with ``python -m
        dlc.patch_evidence`` and returns a structured decision via ``--plan-decision-file``).
        For autonomous (``--auto``) runs with no LLM, a conservative low-confidence fallback
        decides. The decision is **validated against bounds** (the ICC/raw foundation is not
        overridable), applied, and the resulting plan **fingerprinted** — a change invalidates
        the now-stale post-MHC measurement + everything built/scored against the cube.

        Bypasses ``_stage`` so it re-applies on every resume (the chosen knobs must be live on
        ``self.patch_sizes`` before the post-MHC measure generates patches)."""
        if not self.adaptive_planning:
            return
        self.runlog.set_phase("adaptive-planning")
        self.runlog.stage_start("adaptive-planning")
        flow = self.calib.get("flow")
        base = asdict(self.patch_sizes)
        mhc_digest = (self.calib["stages"].get("build-install-mhc") or {}).get("digest", {})
        evidence = patch_evidence.gather_evidence(
            dip=self._dip(),
            target_primaries=gamut.target_primaries(self._target_colorspace()),
            target_colorspace=self._target_colorspace(),
            raw_ti3=raw_ti3,
            mhc_digest=mhc_digest if isinstance(mhc_digest, dict) else {},
            patch_sizes=base, transfer=self._transfer(), flow=flow,
            prior_runs=patch_evidence.list_prior_runs(self.ctx.root.parent, self.display.name),
            cache_state={k: (self.calib["stages"].get(k) or {}).get("status")
                         for k in ("measure:post-mhc", "build-install-3dlut")},
        )
        fallback = evidence["conservative_fallback"]
        # Persist the packet so the paused LLM can drill into it with `python -m dlc.patch_evidence`.
        atomic_write_text(self.ctx.root / "adaptive_evidence.json",
                          json.dumps(evidence, indent=2, default=str))
        decision = self.adjudicate(AdjudicationRequest(
            key="adaptive-planning:plan", seam=SEAM_PLANNING, stage="adaptive-planning",
            question=("Investigate the panel/run and choose the patch strategy "
                      "(shadow_treatment + volumetric_density [+ patch_size_overrides]). "
                      f"Tools: `python -m dlc.patch_evidence --run {self.ctx.root} --what ...`; "
                      "answer with `--plan-decision-file <json>`."),
            options=("apply",), recommendation="apply",
            digest={"evidence": evidence, "decision_schema": patch_evidence.DECISION_SCHEMA},
            recommended_payload=fallback))
        payload = decision.payload if isinstance(decision.payload, dict) else fallback
        knobs, normalized = patch_evidence.validate_decision(payload, base)
        if knobs:
            self.patch_sizes = self.patch_sizes.merged(**knobs)
        # Fingerprint the RESULTING plan; a change since the last applied plan means the
        # memoised post-MHC measure (and everything built/scored on its cube) is stale.
        new_fp = self._patch_plan_record(flow).get("fingerprint")
        prior = self.calib.get("adaptive_plan")
        prior_fp = prior.get("fingerprint") if isinstance(prior, dict) else None
        if prior_fp != new_fp:
            for stale in ("measure:post-mhc", "build-install-3dlut",
                          "measure:verify", "verify"):
                self.calib["stages"].pop(stale, None)
        self.calib["adaptive_plan"] = {"fingerprint": new_fp, "decision": normalized,
                                       "worth_investigating": evidence["worth_investigating"]}
        self._save()
        self.runlog.stage_done(
            "adaptive-planning",
            strategy=f"{normalized['shadow_treatment']}/{normalized['volumetric_density']}",
            source=normalized.get("source"), confidence=normalized.get("confidence"))

    def _dark_noise_entries(self, ti3_path: Optional[str]) -> list:
        """``[(gray level, trust-noise), ...]`` from the measure loop's noise sidecar beside
        ``ti3_path`` — trust-noise is the standard error of the mean chromaticity (per-read σ /
        √reads), or +inf for an unstable level. Empty when single-read / absent (the refine then
        trusts every level — the σ-driven dark smoothing simply isn't engaged)."""
        if not ti3_path:
            return []
        from .measure_loop import read_noise_sidecar
        return read_noise_sidecar(Path(ti3_path))

    def _grey_de_vs_white(self, samples, white_xy: tuple[float, float]) -> dict[str, Any]:
        """Average/max dE_ITP of the GRAYSCALE patches against the target white (D65) at the
        resolved HDR peak — the closed-loop refine's convergence metric. Returns
        ``{"avg","max","n","gamma_err_pct"}``: ``gamma_err_pct`` is the worst luminance-tracking
        error along the grey ramp (measured Y vs target Y, the grayscale EOTF/"gamma" axis dE_ITP
        folds chroma into) over patches above a 1-nit floor — None/0 if no grey / scoring failed."""
        try:
            metrics, _lum = score_samples_hdr(samples, white_xy=white_xy,
                                              peak_nits=self._hdr_target().peak_nits,
                                              reachable_primaries=self._reachable_primaries())
            grey = [m for m in metrics if m.grayscale]
            if not grey:
                return {"avg": None, "max": None, "n": 0, "gamma_err_pct": None}
            de = [m.de2000 for m in grey]
            # Luminance-tracking error: |measured_Y - target_Y| / target_Y, worst over the lit
            # ramp (target_Y > 1 nit avoids near-black noise blowing up the ratio). This is the
            # grayscale "gamma" axis on its own — the LLM judges EOTF tracking apart from chroma.
            lum_errs = [abs(m.measured_xyz[1] - m.target_xyz[1]) / m.target_xyz[1]
                        for m in grey if m.target_xyz[1] > 1.0]
            gamma_err = round(100.0 * max(lum_errs), 2) if lum_errs else None
            return {"avg": round(sum(de) / len(de), 3), "max": round(max(de), 3), "n": len(de),
                    "gamma_err_pct": gamma_err}
        except Exception:  # noqa: BLE001 — advisory metric; a scoring hiccup must not crash the loop
            return {"avg": None, "max": None, "n": 0, "gamma_err_pct": None}

    def stage_refine_mhc_cube(self, *, target_de: float = 2.0,
                              min_improvement: float = 0.3, regress_tol: float = 0.5,
                              floor_patience: int = 2, safety_max_rounds: int = 40
                              ) -> StageOutcome:
        """Closed-loop grayscale refine of the HDR MHC base cube toward STANDALONE D65.

        Each round: measure the neutral ramp with the current cube applied, score grey vs D65
        (dE_ITP), and — unless already floored — pull the cube toward D65 at the Peak-Chroma cap
        (``mhc_cube.refine_hdr_cube``) and reinstall. This makes the ICC a self-sufficient D65
        foundation (see [[mhc-standalone-d65-peakchroma]] / [[dlc-corrections-stack-independently]]),
        independent of the optional 3D LUT.

        **No arbitrary round cap (DESIGN LAW).** Mirrors the SDR sibling
        (:meth:`stage_refine_mhc_grayscale`): the loop runs to the panel's *physical floor* — it
        converges to ``target_de`` OR stops improving beyond noise for ``floor_patience`` consecutive
        rounds (a single noisy sub-``min_improvement`` step is not the floor) — or REGRESSES (revert +
        LLM seam). ``safety_max_rounds`` is a backstop for a pathological panel: NOT a silent cap — it
        reverts to best and raises a seam. A UNIFIED best-revert reinstalls the best measured cube on
        EVERY terminal exit (not just regression). Each round emits a non-blocking check-in the LLM
        consumes (and may cancel via ``control.json``); the FINAL acceptance is the verify seam. HDR
        only; SDR / non-1D-LUT base ⇒ no-op.
        """
        def run() -> StageOutcome:
            spec = self._spec()
            params = self._state.get("mhc_params") or {}
            base_lut = params.get("base_lut") or {}
            cube_path = base_lut.get("cube_path")
            peak_chroma = params.get("peak_chroma") or {}
            cap_nits = peak_chroma.get("cube_peak_nits") or peak_chroma.get("cap_nits")  # refine to the cube's actual top (Option 1)
            channel_peak_xyz = params.get("channel_peak_xyz")
            native_white = params.get("measured_white") or {}
            nwx, nwy = native_white.get("x"), native_white.get("y")
            # Adaptive dark floor derived at build time from the measured dark-read chroma drift
            # (build_mhc / mhc_cube.adaptive_dark_floor); fall back to 1.0 nit if absent.
            dark_floor = float((params.get("dark_floor") or {}).get("nits") or 1.0)
            if not (spec.is_hdr and cube_path and cap_nits and channel_peak_xyz
                    and nwx is not None and nwy is not None):
                return StageOutcome(
                    "refine-mhc-cube", "done",
                    digest={"skipped": True, "reason": (
                        "closed-loop refine is HDR-only and needs a 1D-LUT base cube + Peak-Chroma "
                        "cap + per-channel peaks (SDR or missing inputs)")},
                    data={"rounds": 0})

            from .mhc_cube import mhc2_matrix, read_1d_cube, refine_hdr_cube, write_1d_cube

            # Post-matrix neutral drive per channel (M @ (1,1,1)) — the signal Windows applies the
            # cube at. The installed MHC2 now targets the NATIVE gamut (C++ default 2026-06-23), so
            # the matrix is the native-basis white-only move (a diagonal native-white→D65 gain), NOT
            # the old Rec.2020-source matrix. The refine's abscissa MUST match what's installed, so
            # compute the SAME native-target matrix here (target primaries = native too) — else the
            # rowsums (cube abscissa) mismatch the installed diagonal matrix and the closed loop
            # converges to the wrong post-matrix signal.
            matrix = mhc2_matrix(params["primaries"], (nwx, nwy),
                                 params["primaries"], _D65_XY)
            rowsums = [sum(matrix[r]) for r in range(3)]
            wx, wy = self._white_xy()                       # target white (resolved D65)
            gen = self.ctx.root / "generated"

            # Idempotence: ALWAYS refine from the build's base cube, never a prior refine's output.
            # build-install-mhc writes mhc_base_<mode>.cube; a successful refine repoints
            # base_lut.cube_path at its own mhc_base_<mode>.refineN.cube. Re-running THIS stage in
            # isolation (e.g. the reuse-raw technique pops it) would otherwise read the already-refined
            # cube and compound the correction. Reset to the base cube up front (reinstall if needed).
            base_cube = gen / f"mhc_base_{self.mode.lower()}.cube"
            if base_cube.exists() and Path(cube_path).resolve() != base_cube.resolve():
                self.controller.set_base_lut(self.monitor, self.mode,
                                             str(base_cube.resolve()), cap_nits)
                self.controller.apply_mhc(self.monitor, self.mode)
                cube_path = str(base_cube)

            scores: list[float] = []
            rounds_log: list[dict[str, Any]] = []
            installed = cube_path
            best_path, best_avg = cube_path, float("inf")
            flags: dict[str, bool] = {}
            floor_streak = 0           # consecutive rounds with sub-noise improvement (→ monitor floor)

            rnd = 0
            while True:
                rnd += 1
                res = self._measure_set(self._neutral_patches(), role=f"refine{rnd}",
                                        ti3_name=f"refine_{rnd}.ti3",
                                        ndjson_name=f"refine_{rnd}.ndjson")
                samples = parse_ti3(Path(res.ti3_path)) if res.ti3_path else []
                grey = [s for s in samples
                        if abs(s.rgb[0] - s.rgb[1]) < 1e-6 and abs(s.rgb[1] - s.rgb[2]) < 1e-6]
                de = self._grey_de_vs_white(samples, (wx, wy))
                rounds_log.append({"round": rnd, "grey_avg_de_itp": de["avg"],
                                   "grey_max_de_itp": de["max"], "grey_n": de["n"],
                                   "gamma_err_pct": de["gamma_err_pct"], "cube": Path(installed).name})
                # Feed the round's grayscale quality to the timed check-in's live metrics so a
                # multi-round refine isn't metric-blind mid-run (the optimizer path already does
                # this via _last_optimizer). ``since_last_round`` = improvement over the previous
                # round (prev_avg - this_avg; +ve = converging, -ve = regressing) so the LLM reads
                # the trend, not a bare number it has to diff against the last check-in by hand.
                prev_avg = scores[-1] if scores else None   # scores not yet appended this round
                # best_avg is updated AFTER this block, so fold this round in for the snapshot.
                cur_best = (min(best_avg, de["avg"]) if de["avg"] is not None else best_avg)
                self._last_refine = {
                    "round": rnd, "grey_avg_de_itp": de["avg"], "grey_max_de_itp": de["max"],
                    "gamma_err_pct": de["gamma_err_pct"], "grey_n": de["n"],
                    "best_avg_de_itp": (round(cur_best, 3) if cur_best != float("inf") else None),
                    "since_last_round": (round(prev_avg - de["avg"], 3)
                                         if prev_avg is not None and de["avg"] is not None else None)}
                self._maybe_timed_checkin("refine-mhc-cube")
                if de["avg"] is None:
                    flags["unscored"] = True
                    break
                scores.append(de["avg"])
                if de["avg"] < best_avg:
                    best_avg, best_path = de["avg"], installed

                # --- stop conditions. The loop runs to the PANEL'S PHYSICAL FLOOR, not an arbitrary
                # round count (DESIGN LAW). Each just sets a flag + breaks; the UNIFIED best-revert
                # after the loop reinstalls the best measured cube on EVERY exit. ---
                if len(scores) >= 2 and scores[-1] > scores[-2] + regress_tol:
                    flags["regressed"] = True            # a round made grey WORSE → revert + LLM seam
                    break
                if de["avg"] <= target_de:
                    flags["converged"] = True            # reached the panel-limited target
                    break
                # Monitor floor: improvement below measurement noise for `floor_patience` consecutive
                # rounds (a single noisy sub-threshold step is not the floor).
                if len(scores) >= 2 and (scores[-2] - scores[-1]) < min_improvement:
                    floor_streak += 1
                    if floor_streak >= floor_patience:
                        flags["floored"] = True
                        break
                else:
                    floor_streak = 0
                # Backstop for a pathological non-converging panel: NOT a silent cap — revert to best
                # and raise a seam (handled after the stage) so the LLM adjudicates rather than code.
                if rnd >= safety_max_rounds:
                    flags["safety_ceiling"] = True
                    break

                # --- one refine step toward D65 at the Peak-Chroma cap, then reinstall ---
                # Attach each level's measurement noise (SE of the mean chromaticity, or +inf if the
                # level was flagged unstable; from the noise sidecar, matched by nearest signal) so the
                # refine smooths a noisy/unstable dark level's correction toward identity.
                from .measure_loop import match_level_noise
                noise_entries = self._dark_noise_entries(res.ti3_path)
                measured_neutral = []
                for s in grey:
                    noise = match_level_noise(noise_entries, s.rgb[0]) if noise_entries else None
                    entry = (s.rgb[0], tuple(s.xyz))
                    measured_neutral.append(entry + (noise,) if noise is not None else entry)
                new_curves = refine_hdr_cube(
                    read_1d_cube(Path(installed)), measured_neutral, channel_peak_xyz, rowsums,
                    peak_cap_nits=cap_nits, target_white_xy=(wx, wy), dark_floor_nits=dark_floor)
                new_path = gen / f"mhc_base_{self.mode.lower()}.refine{rnd}.cube"
                write_1d_cube(new_path, new_curves,
                              title=f"DLC HDR MHC standalone-D65 refine r{rnd} (mon {self.monitor})")
                self.controller.set_base_lut(self.monitor, self.mode,
                                             str(new_path.resolve()), cap_nits)
                self.controller.apply_mhc(self.monitor, self.mode)
                installed = str(new_path)

            # Unified best-revert: reinstall the BEST measured cube on EVERY terminal exit (not just
            # regression). A converged/floored/safety exit might otherwise strand a marginally-worse-
            # than-best cube (an uptick within regress_tol never trips the regression gate); reinstalling
            # best guarantees the standalone foundation never regresses below what was actually measured
            # best (≥ the build base cube, since round 1 measures it).
            if best_path != installed:
                self.controller.set_base_lut(self.monitor, self.mode,
                                             str(Path(best_path).resolve()), cap_nits)
                self.controller.apply_mhc(self.monitor, self.mode)
                installed = best_path

            # Point the foundation at the final (best measured) cube so the deliverable + any
            # resume install reference the refined result.
            if installed != cube_path:
                base_lut["cube_path"] = str(installed)
                params["base_lut"] = base_lut
                self._state["mhc_params"] = params
                _common.save_dlc_state(self.ctx, self._state)

            final_avg = scores[-1] if scores else None
            digest = {"rounds": len(rounds_log), "round_log": rounds_log,
                      "grey_avg_de_itp": final_avg, "best_grey_avg_de_itp": (
                          round(best_avg, 3) if best_avg != float("inf") else None),
                      "cap_nits": cap_nits, "binding_channel": peak_chroma.get("binding_channel"),
                      "target_de_itp": target_de, "final_cube": Path(installed).name, **flags}
            return StageOutcome("refine-mhc-cube", "done", digest=digest,
                                data={"rounds": len(rounds_log), "regressed": bool(flags.get("regressed")),
                                      "safety_ceiling": bool(flags.get("safety_ceiling")),
                                      "final_avg": final_avg})

        outcome = self._stage("refine-mhc-cube", run)
        if outcome.data.get("regressed"):
            # A refine round made grayscale WORSE — the best cube is already reinstalled; the LLM
            # judges whether to accept it, extend the loop, or recheck the panel. (Not a unilateral
            # abort: the reverted cube is still the measured-best foundation.)
            self.adjudicate(AdjudicationRequest(
                key="refine-mhc-cube:regression", seam=SEAM_OPTIMIZE, stage="refine-mhc-cube",
                question=("the closed-loop grayscale refine regressed (a round made grey worse); "
                          "the best measured cube was restored — accept it, or recheck the panel?"),
                options=("accept", "abort"), recommendation="accept",
                digest=outcome.digest))
        elif outcome.data.get("safety_ceiling"):
            # The backstop fired: many rounds without reaching the floor or target (a pathological /
            # unstable panel). NOT silently capped — the best measured cube is installed and the LLM
            # decides whether that foundation is good enough or the panel needs a recheck.
            self.adjudicate(AdjudicationRequest(
                key="refine-mhc-cube:safety-ceiling", seam=SEAM_OPTIMIZE, stage="refine-mhc-cube",
                question=(f"the HDR grayscale refine ran {safety_max_rounds} rounds without reaching the "
                          "monitor floor or the target — the best measured cube is installed; accept it, "
                          "or recheck the panel?"),
                options=("accept", "abort"), recommendation="accept",
                digest=outcome.digest))
        return outcome

    def _grey_de_sdr(self, samples, white_xy: tuple[float, float]) -> dict[str, Any]:
        """SDR analog of :meth:`_grey_de_vs_white` — average/max **CIEDE2000** of the GRAYSCALE
        patches vs the resolved target white, plus the worst luminance-tracking ("gamma") error
        along the lit grey ramp. The SDR closed-loop refine's convergence metric. Advisory only —
        a scoring hiccup returns None rather than crashing the loop."""
        try:
            metrics, _lum = score_samples(samples, gamma=self._spec().gamma, white_xy=white_xy)
            grey = [m for m in metrics if m.grayscale]
            if not grey:
                return {"avg": None, "max": None, "n": 0, "gamma_err_pct": None}
            de = [m.de2000 for m in grey]
            lum_errs = [abs(m.measured_xyz[1] - m.target_xyz[1]) / m.target_xyz[1]
                        for m in grey if m.target_xyz[1] > 0.5]
            gamma_err = round(100.0 * max(lum_errs), 2) if lum_errs else None
            return {"avg": round(sum(de) / len(de), 3), "max": round(max(de), 3), "n": len(de),
                    "gamma_err_pct": gamma_err}
        except Exception:  # noqa: BLE001 — advisory metric; a scoring hiccup must not crash the loop
            return {"avg": None, "max": None, "n": 0, "gamma_err_pct": None}

    def stage_refine_mhc_grayscale(self, *, target_de: float = 0.5,
                                   min_improvement: float = 0.1, regress_tol: float = 0.3,
                                   floor_patience: int = 2, safety_max_rounds: int = 40
                                   ) -> StageOutcome:
        """Closed-loop grayscale refine of the **SDR** MHC **base 1D-LUT cube** toward STANDALONE D65.

        The SDR sibling of :meth:`stage_refine_mhc_cube` (Task B / backlog #C1). Each round: measure the
        neutral ramp with the current MHC applied, score grey vs the resolved white (CIEDE2000), and —
        unless already floored — pull the per-channel **base cube** toward D65 at the POST-matrix abscissa
        (``mhc_cube.refine_sdr_cube``) and reinstall over ``set_base_lut``. As of 2026-06-24 this drives a
        DLC-owned 1D-LUT base, NOT the user-editable ``correctionGrayscale`` slot (a user "Reset Grayscale"
        wiped it; a loaded cube locks that editor) — see [[dlc-must-not-own-mhc-user-layers]]. This makes
        the SDR ICC a self-sufficient D65 foundation (the matrix owns native→D65, the base 1D LUT owns
        native-white tone + the per-level non-additivity residual) — independent of the 3D LUT
        (see [[sdr-violates-1plus1plus1-hdr-upholds]] / [[dlc-corrections-stack-independently]]).

        **No arbitrary round cap (DESIGN LAW).** The loop runs until it reaches the panel's *physical
        floor* — it converges to ``target_de`` OR stops improving beyond measurement noise for
        ``floor_patience`` consecutive rounds (a single noisy sub-``min_improvement`` step is NOT the
        floor). A REGRESSION (a refine made grey worse than ``regress_tol``) reverts to the best measured
        cube and raises a seam for the LLM. ``safety_max_rounds`` is a backstop for a pathological
        non-converging panel: it does NOT silently cap — it reverts to best and raises a seam so the LLM
        adjudicates (accept the best foundation, or recheck the panel). Each round emits a NON-BLOCKING
        check-in the LLM consumes from the running spine (and may cancel via ``control.json``); the FINAL
        acceptance is the verify seam. SDR only; HDR uses its own base-cube refine. The mock panel ignores
        the installed correction, so sim proves WIRING + math only.
        """
        def run() -> StageOutcome:
            spec = self._spec()
            params = self._state.get("mhc_params") or {}
            primaries = params.get("primaries")
            native_white = params.get("measured_white") or {}
            nwx, nwy = native_white.get("x"), native_white.get("y")
            peak = params.get("target_luminance")
            dark_floor = float((params.get("dark_floor") or {}).get("nits") or 0.5)
            if spec.is_hdr or not (primaries and nwx is not None and nwy is not None and peak):
                return StageOutcome(
                    "refine-mhc-grayscale", "done",
                    digest={"skipped": True, "reason": (
                        "SDR-only closed-loop base-cube refine (HDR uses its own base-cube refine; "
                        "or missing primaries/measured-white/peak inputs)")},
                    data={"rounds": 0})

            from .measure_loop import match_level_noise
            from .mhc_cube import mhc2_matrix, read_1d_cube, refine_sdr_cube, write_1d_cube

            # Installed SDR MHC2 matrix: src = sRGB (the C++ SDR srcPrim), display = native primaries +
            # MEASURED native white (set_white sends native white) → M performs native→D65. rowsums
            # = M@(1,1,1) = the native-channel neutral drive that reproduces D65 — the POST-matrix signal
            # the per-channel base-cube LUT is keyed at. MUST match the install or the loop converges to
            # the wrong abscissa (the HDR 3151c50 lesson, SDR edition).
            matrix = mhc2_matrix(primaries, (nwx, nwy), SRGB_PRIMARIES, _D65_XY)
            rowsums = [sum(matrix[r]) for r in range(3)]
            wx, wy = self._white_xy()                       # resolved target white (D65 or SPD-derived)
            gamma = float(spec.gamma)
            gen = self.ctx.root / "generated"
            base_lut = params.get("base_lut") or {}
            base_cube = gen / f"mhc_base_{self.mode.lower()}.cube"
            if not base_cube.exists():
                bp = base_lut.get("cube_path")               # fall back to the recorded cube path
                if bp and Path(bp).exists():
                    base_cube = Path(bp)
                else:
                    return StageOutcome(
                        "refine-mhc-grayscale", "done",
                        digest={"skipped": True, "reason": "no SDR base 1D-LUT cube to refine (run build-mhc)"},
                        data={"rounds": 0})

            # Neutralize the legacy user-editable correctionGrayscale slot: a prior SDR run may have left a
            # non-identity refine there, and the bake stacks it INDEPENDENTLY of the cube (gui_mhc.cpp).
            # The cube now owns the whole neutral correction — see [[dlc-must-not-own-mhc-user-layers]].
            n_points = 32
            grid = [j / (n_points - 1) for j in range(n_points)]
            ident = {ch: [1.0] * n_points for ch in ("r", "g", "b")}
            self.controller.set_correction_grayscale(self.monitor, self.mode, n_points, grid, ident, gamma=gamma)

            # Idempotence: ALWAYS refine from the build's base cube, never a prior refine's output (re-running
            # this stage in isolation must not compound the correction). Reinstall the base cube up front.
            self.controller.set_base_lut(self.monitor, self.mode, str(base_cube.resolve()), 0.0)
            self.controller.apply_mhc(self.monitor, self.mode)

            scores: list[float] = []
            rounds_log: list[dict[str, Any]] = []
            installed_path = str(base_cube)                    # currently applied base cube
            best_path, best_avg = str(base_cube), float("inf")
            flags: dict[str, bool] = {}
            floor_streak = 0           # consecutive rounds with sub-noise improvement (→ monitor floor)

            rnd = 0
            while True:
                rnd += 1
                res = self._measure_set(self._neutral_patches(), role=f"refine{rnd}",
                                        ti3_name=f"refine_{rnd}.ti3",
                                        ndjson_name=f"refine_{rnd}.ndjson")
                samples = parse_ti3(Path(res.ti3_path)) if res.ti3_path else []
                grey = [s for s in samples
                        if abs(s.rgb[0] - s.rgb[1]) < 1e-6 and abs(s.rgb[1] - s.rgb[2]) < 1e-6]
                de = self._grey_de_sdr(samples, (wx, wy))
                rounds_log.append({"round": rnd, "grey_avg_de2000": de["avg"],
                                   "grey_max_de2000": de["max"], "grey_n": de["n"],
                                   "gamma_err_pct": de["gamma_err_pct"]})
                # Feed the round's grayscale quality to the timed check-in's live metrics (non-blocking
                # evidence) so a multi-round refine isn't metric-blind mid-run; since_last_round = the
                # round-over-round improvement (prev - this; +ve = converging).
                prev_avg = scores[-1] if scores else None
                cur_best = (min(best_avg, de["avg"]) if de["avg"] is not None else best_avg)
                self._last_refine = {
                    "round": rnd, "grey_avg_de2000": de["avg"], "grey_max_de2000": de["max"],
                    "gamma_err_pct": de["gamma_err_pct"], "grey_n": de["n"],
                    "best_avg_de2000": (round(cur_best, 3) if cur_best != float("inf") else None),
                    "since_last_round": (round(prev_avg - de["avg"], 3)
                                         if prev_avg is not None and de["avg"] is not None else None)}
                self._maybe_timed_checkin("refine-mhc-grayscale")
                if de["avg"] is None:
                    flags["unscored"] = True
                    break
                scores.append(de["avg"])
                if de["avg"] < best_avg:
                    best_avg, best_path = de["avg"], installed_path

                # --- stop conditions. The loop runs to the PANEL'S PHYSICAL FLOOR, not an arbitrary
                # round count (DESIGN LAW: adapt until the monitor can give no more). Each just sets a
                # flag + breaks; the UNIFIED best-revert below leaves the best measured deviations
                # installed on EVERY exit — so a within-tolerance uptick that doesn't trip the regression
                # gate can't strand a worse-than-best (even worse-than-identity) correction (round 1
                # always measures identity, so best is identity-or-better). ---
                if len(scores) >= 2 and scores[-1] > scores[-2] + regress_tol:
                    flags["regressed"] = True            # a round made grey WORSE → revert + LLM seam
                    break
                if de["avg"] <= target_de:
                    flags["converged"] = True            # reached the panel-limited target
                    break
                # Monitor floor: improvement has fallen below measurement noise. Require it to hold for
                # `floor_patience` consecutive rounds so a single noisy sub-threshold step (or a tiny
                # within-tolerance uptick) doesn't end convergence prematurely.
                if len(scores) >= 2 and (scores[-2] - scores[-1]) < min_improvement:
                    floor_streak += 1
                    if floor_streak >= floor_patience:
                        flags["floored"] = True
                        break
                else:
                    floor_streak = 0
                # Backstop for a pathological non-converging panel: NOT a silent cap — revert to best and
                # raise a seam (handled after the stage) so the LLM adjudicates rather than the code.
                if rnd >= safety_max_rounds:
                    flags["safety_ceiling"] = True
                    break

                # --- one refine step toward D65 at the post-matrix abscissa, then reinstall ---
                # Attach each level's measurement noise (SE of the mean chromaticity, or +inf if the
                # level was flagged unstable; matched by nearest signal) so the refine smooths a
                # noisy/unstable dark level's correction toward identity.
                noise_entries = self._dark_noise_entries(res.ti3_path)
                measured_neutral = []
                for s in grey:
                    noise = match_level_noise(noise_entries, s.rgb[0]) if noise_entries else None
                    entry = (s.rgb[0], tuple(s.xyz))
                    measured_neutral.append(entry + (noise,) if noise is not None else entry)
                new_curves = refine_sdr_cube(
                    read_1d_cube(Path(installed_path)), measured_neutral, primaries, (nwx, nwy),
                    peak, rowsums, gamma=gamma, target_white_xy=(wx, wy), dark_floor_nits=dark_floor)
                new_path = gen / f"mhc_base_{self.mode.lower()}.refine{rnd}.cube"
                write_1d_cube(new_path, new_curves,
                              title=f"DLC SDR MHC standalone-D65 refine r{rnd} (mon {self.monitor})")
                self.controller.set_base_lut(self.monitor, self.mode, str(new_path.resolve()), 0.0)
                self.controller.apply_mhc(self.monitor, self.mode)
                installed_path = str(new_path)

            # Unified best-revert: reinstall the BEST measured cube on every terminal exit. A
            # converged/floored/safety exit might otherwise strand a marginally-worse-than-best cube (an
            # uptick within regress_tol never trips the regression gate); reinstalling best guarantees the
            # standalone foundation never regresses below what was actually measured best (≥ the build base
            # cube, since round 1 measures it).
            if best_path != installed_path:
                self.controller.set_base_lut(self.monitor, self.mode, str(Path(best_path).resolve()), 0.0)
                self.controller.apply_mhc(self.monitor, self.mode)
                installed_path = best_path

            # Point the SDR foundation at the final (best measured) cube so the deliverable + any resume
            # install reference the refined result (mirrors stage_refine_mhc_cube). The cube owns the whole
            # neutral correction; correctionGrayscale stays identity (the deprecated user slot).
            if installed_path != base_lut.get("cube_path"):
                base_lut["cube_path"] = str(installed_path)
                params["base_lut"] = base_lut
                self._state["mhc_params"] = params
            self._state["correction_grayscale"] = {
                "point_count": n_points, "points": grid, "deviations": ident}
            _common.save_dlc_state(self.ctx, self._state)

            final_avg = scores[-1] if scores else None
            digest = {"rounds": len(rounds_log), "round_log": rounds_log,
                      "grey_avg_de2000": final_avg, "best_grey_avg_de2000": (
                          round(best_avg, 3) if best_avg != float("inf") else None),
                      "target_de2000": target_de, "rowsums": [round(v, 5) for v in rowsums],
                      **flags}
            return StageOutcome("refine-mhc-grayscale", "done", digest=digest,
                                data={"rounds": len(rounds_log),
                                      "regressed": bool(flags.get("regressed")),
                                      "safety_ceiling": bool(flags.get("safety_ceiling")),
                                      "final_avg": final_avg})

        outcome = self._stage("refine-mhc-grayscale", run)
        if outcome.data.get("regressed"):
            # A refine round made grayscale WORSE — the best deviations are already reinstalled; the
            # LLM judges whether to accept, extend, or recheck the panel. (Not a unilateral abort: the
            # reverted deviations are still the measured-best foundation.)
            self.adjudicate(AdjudicationRequest(
                key="refine-mhc-grayscale:regression", seam=SEAM_OPTIMIZE, stage="refine-mhc-grayscale",
                question=("the SDR closed-loop grayscale refine regressed (a round made grey worse); "
                          "the best measured correctionGrayscale was restored — accept it, or recheck "
                          "the panel?"),
                options=("accept", "abort"), recommendation="accept",
                digest=outcome.digest))
        elif outcome.data.get("safety_ceiling"):
            # The backstop fired: many rounds without reaching the floor or target (a pathological /
            # unstable panel). NOT silently capped — the best measured deviations are installed and the
            # LLM decides whether that foundation is good enough or the panel needs a recheck.
            self.adjudicate(AdjudicationRequest(
                key="refine-mhc-grayscale:safety-ceiling", seam=SEAM_OPTIMIZE,
                stage="refine-mhc-grayscale",
                question=(f"the SDR grayscale refine ran {safety_max_rounds} rounds without reaching the "
                          "monitor floor or the target — the best measured correctionGrayscale is "
                          "installed; accept it, or recheck the panel?"),
                options=("accept", "abort"), recommendation="accept",
                digest=outcome.digest))
        return outcome

    def _cube_optimize_config(self) -> OptimizeConfig:
        """The 3D-LUT correction config, with a MODE-AWARE correction-budget ceiling.

        The budget is seeded from the MEASURED residual (:func:`seed_correction_budget`, already
        gamut-aware) and auto-escalates toward ``max_correction_cap``; the ceiling only matters when
        the panel demands a big correction. HDR keeps the default 0.25 (the cube is a small post-MHC
        residual — the 1D MHC base owns the neutral EOTF + per-level WB, the matrix owns native→D65).
        SDR raises it to :data:`SDR_CORRECTION_CAP`: there the MHC does gamut+white ONLY, so the cube
        owns ALL the colour (the whole native→target gamut compression) and 0.25 starves the seed on
        a wide-gamut panel (HANDOFF item H; offline CV: saturated-corner benefit plateaus ~0.5).

        Only the DEFAULT ceiling is lifted — a caller that pinned a custom cap is respected as-is."""
        cfg = self.optimize_config
        if self.mode != "HDR" and cfg.max_correction_cap == OptimizeConfig.max_correction_cap:
            return replace(cfg, max_correction_cap=SDR_CORRECTION_CAP)
        return cfg

    def stage_build_install_3dlut(self, post_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            target = self._engine_target()
            samples = parse_ti3(Path(post_ti3))
            signals = np.array([s.rgb for s in samples], dtype=float)
            measured = np.array([s.xyz for s in samples], dtype=float)
            cube_path = str(self.ctx.root / "generated" / f"final_{self.mode.lower()}.cube")
            try:
                result = optimize_cube(target=target, probe=self._probe_fn(), signals=signals,
                                       measured_xyz=measured, config=self._cube_optimize_config(),
                                       on_iteration=self._on_optimize_iteration,
                                       reachable_primaries=self._reachable_primaries())
            except DegenerateMeasurements as exc:
                # The RBF model can't be built from this patch set (degenerate/collinear) —
                # surface a clear, actionable abort instead of crashing with a numpy traceback.
                raise CalibrationAborted(StageOutcome(
                    "build-install-3dlut", "aborted",
                    digest={"message": f"3D-LUT correction could not be built: {exc.detail}",
                            "degenerate": True, "measurement_count": int(len(signals))}))
            result.write(cube_path, title=f"DLC {self.mode} 3D LUT")
            self.controller.set_3dlut(self.monitor, self.mode, cube_path)
            digest = {**result.digest, "cube_path": cube_path}
            return StageOutcome("build-install-3dlut", "done", digest=digest,
                                data={"cube_path": cube_path,
                                      "needs_adjudication": result.needs_adjudication,
                                      "question": result.question,
                                      "floor_points": result.floor_points[:8]},
                                artifacts=[cube_path])

        outcome = self._stage("build-install-3dlut", run)
        if outcome.data.get("needs_adjudication"):
            severe = self._severe_optimizer_floor(outcome)
            recommendation = "abort" if severe else "accept"
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="build-install-3dlut:floor", seam=SEAM_OPTIMIZE, stage="build-install-3dlut",
                question=outcome.data.get("question") or "the correction machine hit a floor — accept or loosen?",
                options=("accept", "loosen_target", "abort"), recommendation=recommendation,
                digest={**{k: outcome.digest.get(k) for k in
                           ("best_max_de", "best_mean_de", "above_threshold", "physical_floor",
                            "budget_limited", "converged", "probe_total",
                            "neutral_count", "neutral_mean_de", "neutral_max_de")},
                        "severe_floor": severe,
                        "recommendation": recommendation})),
                stage="build-install-3dlut", message="aborted at the 3D-LUT correction floor")
        return outcome

    def _severe_optimizer_floor(self, outcome: StageOutcome) -> bool:
        d = outcome.digest or {}
        if d.get("converged") is True:
            return False
        total = _as_float_local(d.get("probe_total") or d.get("best_probed_patches"))
        floor = _as_float_local(d.get("physical_floor")) or 0.0
        budget = _as_float_local(d.get("budget_limited")) or 0.0
        mean_de = _as_float_local(d.get("best_mean_de")) or 0.0
        max_de = _as_float_local(d.get("best_max_de")) or 0.0
        floor_frac = (floor / total) if total else 0.0
        budget_frac = (budget / total) if total else 0.0
        # Floor points and even large model residuals can be harmless if the generated cube
        # later verifies cleanly. Auto-abort only when a large share of probes still need more
        # correction than the budget can express; that is an in-flight invariant violation.
        # NB: the neutral-axis dE is surfaced in the seam digest (neutral_mean_de/neutral_max_de)
        # for the LLM to JUDGE — it is deliberately NOT auto-escalated here, because a neutral axis
        # that is off at a PHYSICAL floor (e.g. a dim channel that can't reach D65) is an ordinary
        # panel limit, indistinguishable from a cube-induced wreck by magnitude alone. Telling those
        # two apart (and acting on a cube wreck) is the #C2 neutral-pin follow-up.
        if self._spec().is_hdr:
            return budget_frac >= 0.20 and mean_de >= 30.0
        return budget_frac >= 0.20 and mean_de >= 20.0

    def stage_verify(self, verify_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            spec = self._spec()
            samples = parse_ti3(Path(verify_ti3))
            if not samples:
                # A fully-failed verify measure can leave a TI3 with zero usable rows. Scoring
                # raises on an empty set; turn it into a CLEAN abort (→ stage_aborted + a
                # terminal run_done the dashboard sees) instead of an uncaught exception that
                # would escape _run_flow with the spine still showing "running".
                raise CalibrationAborted(StageOutcome(
                    "verify", "aborted",
                    digest={"message": "verify TI3 has no usable measurements to score "
                                       "(all reads failed?) — aborting before the quality gate."}))
            # Score against the SAME resolved white the pipeline targeted (MHC matrix + grayscale
            # refine, 3D-LUT target) — not textbook D65 — so a non-zero white strength is the goal
            # here, not scored as white error. SDR scores CIEDE2000 against γ-power/sRGB; HDR
            # scores dE_ITP against PQ/Rec.2020 (the metric the cube converges in — CIEDE2000's
            # Lab is meaningless at HDR absolute luminance), with looser, LLM-negotiated targets.
            wx, wy = self._white_xy()
            if spec.is_hdr:
                hdr = self._hdr_target()
                metrics, lum = score_samples_hdr(samples, white_xy=(wx, wy), peak_nits=hdr.peak_nits,
                                                  reachable_primaries=self._reachable_primaries())
                metric_name = "dE_ITP"
                # Advisory HDR defaults (dE_ITP); the assistant negotiates the real target
                # at the verify seam after the first refinement round (design §7).
                q = hdr_metric_thresholds()
            else:
                metrics, lum = score_samples(samples, gamma=spec.gamma, white_xy=(wx, wy))
                metric_name = "CIEDE2000"
                q = self.profile.quality
            summary = summarize_metrics(phase="verification", iteration=0, source=Path(verify_ti3),
                                        patch_metrics=metrics, target_luminance=lum, metric=metric_name)
            within = (summary.avg_de2000 <= q.avg_de2000 and summary.p95_de2000 <= q.p95_de2000
                      and summary.max_de2000 <= q.max_de2000 and summary.white_de2000 <= q.white_de2000)
            worst = sorted(metrics, key=lambda m: m.de2000, reverse=True)[:5]
            # Put the scored dE summary on the spine so the dashboard's ΔE big-numbers
            # panel (and the LLM digest) get it — the rich digest below only reaches the
            # adjudicator, not events.jsonl. One event carries the whole panel: the
            # percentiles plus the grayscale-vs-colour split.
            colour_de = [m.de2000 for m in metrics if not m.grayscale]
            self.runlog.metrics_scored(
                "verify", label="verification", iteration=0, metric=metric_name,
                avg_de2000=round(summary.avg_de2000, 3), p95_de2000=round(summary.p95_de2000, 3),
                p99_de2000=round(percentile([m.de2000 for m in metrics], 99.0), 3),
                max_de2000=round(summary.max_de2000, 3), white_de2000=round(summary.white_de2000, 3),
                grayscale_avg_de2000=round(summary.grayscale_avg_de2000, 3),
                colour_avg_de2000=(round(sum(colour_de) / len(colour_de), 3) if colour_de else None),
                patch_count=summary.patch_count, grayscale_count=summary.grayscale_count)
            digest = {"avg_de2000": round(summary.avg_de2000, 3), "p95_de2000": round(summary.p95_de2000, 3),
                      "max_de2000": round(summary.max_de2000, 3), "white_de2000": round(summary.white_de2000, 3),
                      "grayscale_avg_de2000": round(summary.grayscale_avg_de2000, 3),
                      "patch_count": summary.patch_count, "within_quality": within,
                      # Only the dE acceptance targets — not the iteration-control knobs that
                      # share MetricThresholds — so the verify seam (the LLM's judgment surface)
                      # sees quality criteria, not loop knobs.
                      "quality_targets": q.acceptance_targets(),
                      "metric": metric_name, "optimize_metric": "dE_ITP",
                      "target_white_xy": [round(wx, 5), round(wy, 5)],
                      "white_provenance": self._resolved_white().provenance,
                      "worst": [{"rgb": [round(c, 3) for c in m.rgb], "de2000": round(m.de2000, 2)} for m in worst]}
            return StageOutcome("verify", "done", digest=digest,
                                data={"within_quality": within, "metrics": {
                                    "avg_de2000": summary.avg_de2000, "p95_de2000": summary.p95_de2000,
                                    "max_de2000": summary.max_de2000, "white_de2000": summary.white_de2000}})

        outcome = self._stage("verify", run)
        d = outcome.digest
        within = outcome.data.get("within_quality")
        severe = self._severe_verify_failure(outcome)
        self.adjudicate(AdjudicationRequest(
            key="verify:accept", seam=SEAM_VERIFY, stage="verify",
            question=(f"The new calibration reads avg {d.get('metric', 'ΔE')} {d.get('avg_de2000')} "
                      f"(white {d.get('white_de2000')}, max {d.get('max_de2000')}) — "
                      f"{'within' if within else 'outside'} the quality targets. "
                      "Apply this calibration, or revert to the previous display setup?"),
            options=("apply", "revert"),
            recommendation=("revert" if severe else "apply"),
            # severe → recommend revert (auto/sim reverts a catastrophic result). gate_failed flag
            # → even a NON-severe quality-gate miss escalates under SupervisedAdjudicator, so an
            # unattended run never silently applies a sub-quality calibration at this terminal gate.
            digest={**outcome.digest, "severe_failure": severe, "gate_failed": not bool(within)}))
        return outcome

    def _severe_verify_failure(self, outcome: StageOutcome) -> bool:
        if outcome.data.get("within_quality"):
            return False
        d = outcome.digest or {}
        avg = _as_float_local(d.get("avg_de2000")) or 0.0
        p95 = _as_float_local(d.get("p95_de2000")) or 0.0
        max_de = _as_float_local(d.get("max_de2000")) or 0.0
        white = _as_float_local(d.get("white_de2000")) or 0.0
        if d.get("metric") == "dE_ITP":
            return avg >= 30.0 or p95 >= 60.0 or max_de >= 100.0 or white >= 100.0
        return avg >= 20.0 or p95 >= 40.0 or max_de >= 100.0 or white >= 50.0

    # ====================================================================
    # Report + deliverable folder (§11)
    # ====================================================================
    def _results_dir(self) -> Path:
        out = Path(self.profile.paths.get("output", "results"))
        if not out.is_absolute():
            # profile paths are relative to the DLC root (where the profile lives)
            root = Path(self.profile.source_path).resolve().parent if self.profile.source_path else self.ctx.root.parents[1]
            out = root / out
        safe_display = self.display.name.replace(" ", "_").replace("/", "_")
        folder = out / f"{safe_display}_{self.run_date.isoformat()}_{self.mode}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def stage_report(self, *, analysis: Optional[str] = None) -> StageOutcome:
        """Assemble the clean deliverable folder (§11) + report.json/html. The report
        ends with an **LLM display-analysis slot** — the orchestrator leaves it empty
        (None) and exposes the whole-run digest so the LLM writes the analysis at
        report time; the HTML renders it when present."""
        results_dir = self._results_dir()
        stages = self.calib["stages"]

        def sd(key: str) -> dict[str, Any]:
            return (stages.get(key) or {}).get("digest", {})

        def sdat(key: str) -> dict[str, Any]:
            return (stages.get(key) or {}).get("data", {})

        # Copy the in-run build artifact into the deliverable folder under its DESCRIPTIVE name
        # (<date>_DLC_<display>_<mode>_<gamut>_<transfer>_<lum>n.cube) — the durable cube the user
        # keeps and that _finish re-points DesktopLUT at, so the name is self-describing in
        # DesktopLUT's UI (which shows the filename, not the folder).
        cube_src = sdat("build-install-3dlut").get("cube_path")
        cube_out = None
        if cube_src and Path(cube_src).exists():
            spec = self._spec()
            # The HDR deliverable is labelled with the CALIBRATED peak (the resolved max-sustained
            # ceiling), not the profile's 1600 viewing peak — the cube IS the calibration to that
            # peak (Task C / one source of truth). SDR uses its OSD-set white luminance.
            label_nits = self._hdr_target().peak_nits if spec.is_hdr else spec.luminance_nits
            cube_out = results_dir / descriptive_cube_name(
                date=self.run_date.isoformat(), display=self.display.short_name, mode=self.mode,
                colorspace=_gamut_label(spec.colorspace, is_hdr=spec.is_hdr, gamma=spec.gamma),
                transfer=_transfer_token(is_hdr=spec.is_hdr, gamma=spec.gamma),
                luminance_nits=label_nits)
            shutil.copy2(cube_src, cube_out)
        # Copy the verification TI3.
        verify_ti3 = sdat("measure:verify").get("ti3")
        ti3_out = None
        if verify_ti3 and Path(verify_ti3).exists():
            ti3_out = results_dir / "measurements.ti3"
            shutil.copy2(verify_ti3, ti3_out)

        payload = {
            "flow": self.calib.get("flow"), "monitor": self.monitor, "mode": self.mode,
            "display": self.display.name, "target": self.target_name, "date": self.run_date.isoformat(),
            "whitepoint": sd("whitepoint") or None,
            "mhc": sd("build-install-mhc") or None,
            "lut3d": {k: sd("build-install-3dlut").get(k) for k in
                      ("converged", "best_max_de", "best_mean_de", "above_threshold",
                       "physical_floor", "cube_path")} if sd("build-install-3dlut") else None,
            "verification": sd("verify") or None,
            "decisions": self.calib.get("decisions", {}),
            "deliverables": {"cube": str(cube_out) if cube_out else None,
                             "profile_name": sdat("build-install-mhc").get("profile_name"),
                             "measurements_ti3": str(ti3_out) if ti3_out else None},
            "display_analysis": analysis,   # the LLM fills this at report time
        }
        report_json = results_dir / "report.json"
        report_html = results_dir / "report.html"
        report_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        report_html.write_text(_render_report_html(payload), encoding="utf-8")
        return StageOutcome("report", "done",
                            digest={"results_dir": str(results_dir), "report": str(report_json),
                                    "verification": payload["verification"], "lut3d": payload["lut3d"]},
                            data={"results_dir": str(results_dir), "report_path": str(report_json),
                                  "deliverable_cube": str(cube_out) if cube_out else None},
                            artifacts=[str(report_json), str(report_html)])

    # ====================================================================
    # Flows
    # ====================================================================
    def _publish_active_pointer(self) -> None:
        """Point ``runs/active.json`` at this run's spine so the mission-control
        dashboard follows it (and the next run) without being told which folder. Purely
        advisory — a failure here must never touch the run, so it's swallowed. The
        pointer is left in place after the run ends (the dash keeps showing the last
        run until another starts)."""
        try:
            pointer = {
                "run": str(self.ctx.root),
                "events": str(self.ctx.events_path),
                "flow": self.calib.get("flow"),
                "updated": datetime.now().isoformat(timespec="seconds"),
            }
            atomic_write_text(RUNS_DIR / "active.json", json.dumps(pointer, indent=2))
        except Exception:  # noqa: BLE001 - the pointer is a convenience, never a gate
            pass

    def run(self, flow: str) -> CalibrationResult:
        import time
        self._run_started_monotonic = time.monotonic()   # check-in elapsed anchor (this process)
        # Reconcile the flow against the persisted run record (a resume's --flow defaults to
        # `full` and must not overwrite the flow the run actually started with).
        flow, flow_conflict = resolve_run_flow(self._state, flow)
        self.calib["flow"] = flow
        self._save()
        # Surface any run-spec disagreement (mode/bit_depth from the constructor, flow here):
        # the persisted spec is authoritative, but a CLI that asked for something else is a real
        # signal (a mis-issued resume command) the LLM should see — never a silent switch.
        conflicts = list(self._spec_conflicts) + ([flow_conflict] if flow_conflict else [])
        if conflicts:
            self.runlog.anomaly(
                "run", run_spec_conflict=True, conflicts=conflicts,
                message=("resume requested " + ", ".join(
                    f"{c['field']}={c['requested']}" for c in conflicts) +
                    " but the persisted run spec is " + ", ".join(
                    f"{c['field']}={c['persisted']}" for c in conflicts) +
                    " — kept the persisted spec (the run's mode/flow/bit_depth are fixed at creation)."))
        self._publish_active_pointer()   # let the dashboard find this run (and the next)
        self._emit_header()   # open the spine with what we know; enriched as the run proceeds
        if self._enable_watchdog:
            self.liveness.start()   # backstop thread (live runs only; tests don't spin threads)
        try:
            return self._run_flow(flow)
        finally:
            if self._enable_watchdog:
                self.liveness.stop()

    def _run_flow(self, flow: str) -> CalibrationResult:
        try:
            if flow == "full":
                return self._flow_full()
            if flow == "mhc-only":
                return self._flow_mhc_only()
            if flow == "3dlut-only":
                return self._flow_3dlut_only()
            if flow == "build-correction":
                return self._flow_build_correction()
            if flow == "characterize":
                return self._flow_characterize()
            if flow == "hdr":
                raise CalibrationAborted(StageOutcome(
                    "resolve-target", "aborted",
                    digest={"message": "HDR is the post-v1 goal; v1 is SDR-first. Use 'full' on an SDR target."}))
            raise ValueError(f"unknown flow {flow!r} (have: {sorted(FLOWS)})")
        except CalibrationAborted as exc:
            self.calib["stages"][exc.outcome.stage] = exc.outcome.as_record()
            self._save()
            self.runlog.run_done("aborted", aborted_at=exc.outcome.stage,
                                 message=(exc.outcome.digest or {}).get("message"))
            return CalibrationResult(
                flow=flow, monitor=self.monitor, mode=self.mode, target=self.target_name,
                status="aborted", stages=list(self.calib["stages"].keys()),
                results_dir=None, report_path=None,
                digest={"aborted_at": exc.outcome.stage, "message": exc.outcome.digest.get("message"),
                        "reason": str(exc)})

    def _warm_tau(self) -> Optional[int]:
        """The panel's measured thermal time constant (in patches) for the warm-start
        ordering rotation — from the DIP if characterized, else ``None`` (engine default)."""
        dip = self._dip()
        return dip.thermal_tau_patches if dip else None

    def _patch_max_cv(self) -> Optional[int]:
        """For an HDR run, cap patch generation at the target peak's code value so every
        measured patch is within the panel's reachable sub-peak range — no patch above the
        target peak, which a ~1840-nit panel would read as a clipped highlight and the verify
        would score as huge error (the roll-off region above the peak is handled separately,
        ``docs/hdr-target-design.md`` §4). SDR ⇒ ``None`` (the full bit-depth range)."""
        if not self._spec().is_hdr:
            return None
        return self._transfer().nits_to_cv(self._hdr_target().peak_nits)

    def _ramp_patches(self, *, gamut_aware: bool = False) -> list[tuple[int, int, int]]:
        # gamut_aware=True (VERIFY only): cap colour-ramp saturation to the panel's reachable gamut
        # so saturated verify patches land where the panel can render. RAW stays uncapped (it needs
        # full-saturation pure channels to characterize the panel — see build_ramp_set).
        caps = self._hue_sat_caps() if gamut_aware else None
        return build_ramp_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                              max_cv=self._patch_max_cv(), hue_sat_caps=caps)

    def _hue_sat_caps(self) -> Optional[dict]:
        """Per-primary-hue signal-saturation caps from the panel's MEASURED native gamut (DIP) vs
        the target colour space — so the VERIFY ramp's saturated patches land on/inside the panel's
        reachable gamut instead of at an unreachable target primary (wasted 55-dE reads). Reuses the
        #C3 ``_reachable_primaries`` (HDR-only, degenerate-guarded); ``None`` ⇒ no cap. Never blocks
        a run — any failure in the (lazy) engine cap computation falls back to the uncapped ramp."""
        native = self._reachable_primaries()
        cs = self._target_colorspace()
        if not native or not cs:
            return None
        try:
            from .engine.model import TargetSpace, signal_saturation_caps
            return signal_saturation_caps(TargetSpace(self._engine_target()), native)
        except Exception:  # noqa: BLE001 — generation must never crash on an optional refinement
            return None

    def _volumetric_patches(self) -> list[tuple[int, int, int]]:
        # Gamut-aware build (HDR / wide-gamut): project the bulk onto the panel's reachable gamut +
        # add the target-gamut anchor foundation. _reachable_primaries is None for SDR (sRGB ⊂ panel)
        # → degrades to the un-projected bulk + neutral/dark, identical to the projection-free plan
        # preview (flow_patch_counts), so the previewed count and the fingerprint stay stable.
        return build_volumetric_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                    max_cv=self._patch_max_cv(), target=self._engine_target(),
                                    reachable_primaries=self._reachable_primaries())

    def _neutral_patches(self) -> list[tuple[int, int, int]]:
        return build_neutral_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                 max_cv=self._patch_max_cv())

    def _verify_patches(self, *, gamut_aware: bool = True) -> list[tuple[int, int, int]]:
        # The "cover all bases" QC set (see build_verify_set): dense grey/PQ + shadow toe, colour
        # only above the shadow band, gamut-capped. gamut_aware caps saturated hues to the panel's
        # reachable gamut (HDR; None for SDR/degenerate — falls back to uncapped).
        caps = self._hue_sat_caps() if gamut_aware else None
        return build_verify_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                max_cv=self._patch_max_cv(), hue_sat_caps=caps)

    def flow_patch_counts(self, flow: str) -> dict[str, Any]:
        return flow_patch_counts(flow, self.patch_sizes, self._transfer(),
                                 max_cv=self._patch_max_cv())

    def _patch_plan_record(self, flow: str) -> dict[str, Any]:
        transfer = self._transfer()
        plan = self.flow_patch_counts(flow)
        record = {
            **plan,
            "flow": flow,
            "bit_depth": self.bit_depth,
            "patch_sizes": asdict(self.patch_sizes),
            "transfer": {
                "kind": transfer.kind,
                "gamma": transfer.gamma,
                "peak_nits": transfer.peak_nits,
                "bit_depth": transfer.bit_depth,
            },
            # The HDR peak cap (None for SDR) is part of the plan identity: changing the target
            # peak changes which patches are measured, so it must invalidate an approved plan.
            "patch_max_cv": self._patch_max_cv(),
        }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return {**record, "fingerprint": hashlib.sha256(payload).hexdigest()[:16]}

    def _finish(self, *, analysis: Optional[str] = None) -> CalibrationResult:
        rep = self.stage_report(analysis=analysis)
        status = "completed"
        # Honour the apply/revert gate. Two rollback regimes:
        #  - flows that ENTERED calibration mode (full / mhc-only) have a real C++ snapshot:
        #    'revert' restores it, 'apply' (or anything non-revert) commits the new profile.
        #  - the in-place flow (3dlut-only) never entered calibration mode; the change is
        #    already live. 'revert' is honoured as far as the pipe allows (the 3D-LUT cube is
        #    restorable — see _revert_inplace). Either way we must NOT silently report
        #    'completed' on a revert.
        choice = (self.calib.get("decisions") or {}).get("verify:accept", {}).get("choice")
        if self._entered_calibration():
            if choice == "revert":
                self._restore_user_setup(why="operator chose revert at the apply gate")
                status = "reverted"
            else:
                self._commit_calibration()
        elif self.calib.get("inplace_baseline") is not None:
            if choice == "revert":
                status = self._revert_inplace()
            # else: the in-place refinement is already applied; nothing to commit.
        if status == "completed":
            # Apply path: re-point DesktopLUT at the DURABLE deliverable cube so a cleaned
            # run folder can't break the live calibration (the build artifact lives under the
            # gitignored run dir). No-ops when this flow built no cube (mhc-only).
            self._install_durable_cube(rep.data.get("deliverable_cube"))
        self.runlog.run_done(status, results_dir=rep.data.get("results_dir"),
                             report_path=rep.data.get("report_path"))
        return CalibrationResult(
            flow=self.calib.get("flow"), monitor=self.monitor, mode=self.mode, target=self.target_name,
            status=status, stages=list(self.calib["stages"].keys()),
            results_dir=rep.data.get("results_dir"), report_path=rep.data.get("report_path"),
            digest=rep.digest)

    def _flow_full(self) -> CalibrationResult:
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self.stage_enter_neutral()
        self.stage_hardware_readiness()
        self.stage_brightness()
        raw = self.stage_measure(role="raw", patches=self._ramp_patches(),
                                 ti3_name="raw.ti3", ndjson_name="raw.ndjson")
        self.stage_build_install_mhc(raw.data["ti3"])
        # Standalone-D65 foundation (1+1+1): pull the MHC to D65 BEFORE the optional 3D LUT refines
        # the off-gray volume — the MHC owns the neutral axis. HDR refines the base 1D cube; SDR
        # refines the correctionGrayscale layer (Task B / #C1). The MHC is now the SOLE neutral-axis
        # owner; the former post-3D-LUT GS+WB tweak (which re-corrected neutral a 3rd time) is removed.
        if self._spec().is_hdr:
            self.stage_refine_mhc_cube()
        else:
            self.stage_refine_mhc_grayscale()
        self.stage_adaptive_planning(raw_ti3=raw.data["ti3"])   # opt-in LLM investigation seam (#47/#49)
        post = self.stage_measure(role="post-mhc", patches=self._volumetric_patches(),
                                  ti3_name="post_mhc.ti3", ndjson_name="post_mhc.ndjson")
        self.stage_build_install_3dlut(post.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._verify_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_mhc_only(self) -> CalibrationResult:
        """ICC only — MHC matrix + base 1D LUT + the closed-loop D65 grayscale refine, then verify +
        report. NO 3D LUT (that is what makes ``full`` long), so this is the fast end-to-end path that
        proves the orchestration + hardware before committing to a dense run. The MHC alone is the
        standalone D65 *foundation*; without the volumetric/colour refinement the 3D LUT adds, verify
        may sit above the final quality targets on saturated colour — that's expected for an ICC-only
        pass (accept it as a shakedown, judge it on the before/after + the grayscale axis)."""
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self.stage_enter_neutral()
        self.stage_hardware_readiness()
        self.stage_brightness()
        raw = self.stage_measure(role="raw", patches=self._ramp_patches(),
                                 ti3_name="raw.ti3", ndjson_name="raw.ndjson")
        self.stage_build_install_mhc(raw.data["ti3"])
        # The mhc-only flow IS the standalone-ICC path — refine the MHC to D65 so verify scores the
        # foundation as a self-sufficient D65 layer (no 3D LUT to lean on). HDR: base 1D cube; SDR:
        # the correctionGrayscale layer (Task B / #C1).
        if self._spec().is_hdr:
            self.stage_refine_mhc_cube()
        else:
            self.stage_refine_mhc_grayscale()
        ver = self.stage_measure(role="verify", patches=self._verify_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_3dlut_only(self) -> CalibrationResult:
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self._require_stack(need_mhc=True, need_lut=False)
        self._capture_inplace_baseline()   # rollback point before set_3dlut mutates the live cube
        self.stage_hardware_readiness()
        self.stage_adaptive_planning(raw_ti3=None)   # opt-in LLM investigation seam (no raw ramp here)
        post = self.stage_measure(role="post-mhc", patches=self._volumetric_patches(),
                                  ti3_name="post_mhc.ti3", ndjson_name="post_mhc.ndjson")
        self.stage_build_install_3dlut(post.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._verify_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_build_correction(self) -> CalibrationResult:
        """Mint (refresh) the colorimeter correction via ccxxmake, standalone — run this
        BEFORE a calibration when the correction is stale/missing (the calibration's meter
        is wired at flow start, so the fresh correction must be recorded first). Persists
        the result to the correction store as the active correction (+ optional white SPD)."""
        self.stage_preflight()
        self.stage_clear_native()      # core clears DesktopLUT to native over the pipe (not the operator's job)
        self.stage_probe_match()       # launches ccxxmake in its own console; ingests the .ccmx on resume
        store = self._correction_store()
        rec = store.get(self.display.name)
        # Terminal marker on the spine (this flow doesn't go through _finish): without it the
        # dashboard liveness light never leaves "running" on a completed build-correction.
        self.runlog.run_done("completed", flow="build-correction",
                             correction=(rec.correction_file if rec else None))
        return CalibrationResult(
            flow="build-correction", monitor=self.monitor, mode=self.mode, target=None,
            status="completed", stages=list(self.calib["stages"].keys()),
            results_dir=None, report_path=None,
            digest={"correction": rec.correction_file if rec else None,
                    "correction_made": rec.correction_made if rec else None,
                    "white_spd": rec.spd_file if rec else None,
                    "probe_match": (self.calib["stages"].get("probe-match") or {}).get("digest")})

    def _flow_characterize(self) -> CalibrationResult:
        """Learn this panel+meter's behaviour and persist a DIP — run this BEFORE a
        calibration when the DIP is stale/missing (the measure loop's read policy consumes
        it). NOT a calibration: it clears to native, measures the three axes, restores the
        user's setup, and applies nothing. The plan-veto confirms the (hardware) run; the
        per-display DIP store is the deliverable, not a results folder."""
        self.stage_preflight()
        # Resolve the target only for its transfer (bit depth + signal↔code-value map) — the
        # native panel is driven at code values, so the target's white/gamma don't matter here.
        target = self.display.target_name(self.mode)
        if not target:
            raise CalibrationAborted(StageOutcome(
                "characterize", "aborted",
                digest={"message": f"display {self.monitor} has no {self.mode} target — needed only "
                                   "for the patch transfer (bit depth); add one to the profile."}))
        spec = self.profile.target(target)
        # HDR characterization IS allowed (unlike HDR *calibration*, which is post-v1): it only
        # measures the native panel + restores, and HDR is exactly where learning the thermal
        # regime matters most (the backlight is content-driven and may never reach steady state).
        if spec.is_hdr:
            self.ctx.log("characterizing in HDR — measurement-only (no calibration is built/applied); "
                         "learning the panel's HDR thermal behaviour.")
        self.target_name = target
        self.calib["target"] = target
        self._save()
        # Plan veto: a hardware run worth confirming (its own seam — NOT the calibration plan).
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key="characterize:plan", seam=SEAM_PLAN, stage="characterize",
            question=(f"Characterize monitor {self.monitor} ({self.display.name}) — LEARN how the "
                      f"panel and meter behave (read noise vs luminance, settle, warm-up/drift). "
                      f"This is NOT a calibration: nothing is built or applied, and your setup is "
                      f"restored afterwards. Proceed?"),
            options=("approve", "abort"), recommendation="approve",
            digest={"flow": "characterize", "display": self.display.name, "monitor": self.monitor,
                    "dip_store": str(self._dip_store().path)})),
            stage="characterize", message="characterization vetoed by the operator")
        self.stage_clear_native()      # measure the NATIVE panel (no corrections in the path)
        self.stage_hardware_readiness()
        try:
            outcome = self.stage_characterize()
        except CalibrationAborted:
            # Review rejected this characterization: drop the just-written DIP so a bad profile
            # is never left silently active, then RESTORE the display before re-raising — the
            # operator's setup must come back even on the abort path (clear-native put it in
            # native). A live AdjudicationRequired pause is NOT a CalibrationAborted, so it skips
            # this and propagates to the pause (the panel stays native for the resuming run).
            self._dip_store().remove(self._dip_key())
            self._restore_user_setup(why="characterization rejected at review — nothing applied")
            raise
        # Leave the display exactly as we found it (clear-native entered native via the pipe).
        self._restore_user_setup(why="characterization complete — panel learned, nothing applied")
        dip = self._dip()
        # Terminal marker on the spine (this flow doesn't go through _finish) so the dashboard
        # flips to 'done' instead of hanging on "running" after a completed characterization.
        self.runlog.run_done("completed", flow="characterize",
                             dip_store=str(self._dip_store().path))
        return CalibrationResult(
            flow="characterize", monitor=self.monitor, mode=self.mode, target=self.target_name,
            status="completed", stages=list(self.calib["stages"].keys()),
            results_dir=None, report_path=None,
            digest={"characterize": outcome.digest,
                    "stored_display": dip.display if dip else None,
                    "dip_store": str(self._dip_store().path)})

    def _require_stack(self, *, need_mhc: bool, need_lut: bool) -> None:
        """3dlut-only assumes an installed stack. If it's missing, escalate
        ('nothing to tune — do a full calibration first') rather than silently
        building from nothing."""
        try:
            state = self.controller.state()
        except Exception as exc:  # noqa: BLE001
            raise CalibrationAborted(StageOutcome(
                "require-stack", "aborted",
                digest={"message": f"cannot read DesktopLUT state: {type(exc).__name__}: {exc}"}))
        ck = f"{self.monitor}:{self.mode}"
        mhc_entry = (state.get("mhc") or {}).get(ck) or {}
        # C++ reports enabled/profile_name; the mock just keys the entry on any set_*.
        has_mhc = bool(mhc_entry.get("enabled") or mhc_entry.get("profile_name")
                       or mhc_entry.get("applied") or mhc_entry)
        has_lut = bool((state.get("runtime") or {}).get(ck, {}).get("cube_path"))
        missing = []
        if need_mhc and not has_mhc:
            missing.append("MHC profile")
        if need_lut and not has_lut:
            missing.append("3D LUT")
        if missing:
            digest = {"missing": missing, "has_mhc": has_mhc, "has_lut": has_lut,
                      "message": f"nothing to tune: {', '.join(missing)} not installed — run a full calibration first"}
            decision = self.adjudicate(AdjudicationRequest(
                key="require-stack:missing", seam=SEAM_STACK, stage="require-stack",
                question=digest["message"], options=("abort", "proceed_anyway"),
                recommendation="abort", digest=digest))
            if decision.choice != "proceed_anyway":
                raise CalibrationAborted(StageOutcome("require-stack", "aborted", digest=digest))


# Flow registry (the named flows the front door maps an intent onto). HDR is the
# later goal; v1 is SDR-first.
FLOWS: dict[str, str] = {
    "full": "neutral → raw → MHC + D65 grayscale refine → post-MHC → 3D LUT → verify → report",
    "mhc-only": "raw → MHC (matrix + 1D + D65 grayscale refine) → verify → report (ICC only; no 3D LUT — shakedown)",
    "3dlut-only": "verify MHC present → measure → 3D LUT → verify → report",
    "build-correction": "preflight → prepare ccxxmake → operator runs it → ingest .ccmx (+white.sp) → store",
    "characterize": "preflight → plan → clear-native → learn panel+meter (noise/settle/drift) → DIP store → restore",
    "hdr": "(post-v1) Rec.2020/PQ — SDR-first in v1",
}


# ---------------------------------------------------------------------------
# Display mode (SDR <-> HDR)
# ---------------------------------------------------------------------------

def argyll_display_from_device_name(device_name: Optional[str]) -> Optional[int]:
    """The Argyll display number encoded in a ``query_monitors`` ``device_name``. Windows device
    names are ``\\\\.\\DISPLAYn`` and the controller reports them in ARGYLL ORDER (per the IPC
    contract), so ``n`` is the Argyll ``-d`` display number. Returns ``n`` or ``None`` if the name
    doesn't carry one."""
    if not device_name:
        return None
    m = re.search(r"DISPLAY(\d+)", str(device_name), re.IGNORECASE)
    return int(m.group(1)) if m else None


def color_space_is_hdr(color_space: Optional[str]) -> bool:
    """True iff a query_monitors ``color_space`` is HDR. SDR and ACM_SDR are both
    SDR-family (ACM is the FP16 SDR scanout, still an SDR calibration target)."""
    return str(color_space).upper() == "HDR"


def apply_set_hdr(controller: Any, monitor: int, action: str) -> dict[str, Any]:
    """Resolve a ``--set-hdr`` action to a controller call + result.

    ``on``/``off`` set the OS advanced-color (HDR) state explicitly; ``toggle``
    inverts it. This is the same flip DesktopLUT's HDR-toggle hotkey performs,
    exposed so the operator can put the panel in the right mode (and start the
    matching dogegen daemon) before an HDR characterize/calibrate run.
    """
    a = str(action).strip().lower()
    if a in ("toggle", ""):
        return controller.toggle_hdr(monitor) or {}
    if a in ("on", "hdr", "true", "1", "enable"):
        return controller.set_hdr(monitor, enable=True) or {}
    if a in ("off", "sdr", "false", "0", "disable"):
        return controller.set_hdr(monitor, enable=False) or {}
    raise ValueError(f"--set-hdr must be on/off/toggle, got {action!r}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _xy(xyz: Sequence[float]) -> tuple[float, float]:
    total = sum(xyz)
    if total <= 0:
        return (0.0, 0.0)
    return (xyz[0] / total, xyz[1] / total)


def _as_float_local(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_elapsed(seconds: Any) -> str:
    s = _as_float_local(seconds)
    if s is None:
        return "?"
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def _fs_safe(token: str) -> str:
    """Collapse anything outside ``[A-Za-z0-9.+-]`` to single underscores → a
    filesystem-safe token (dependency-free; spaces/slashes/punctuation become ``_``)."""
    out: list[str] = []
    prev_us = False
    for ch in token.strip():
        if ch.isalnum() or ch in ".+-":
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    return "".join(out).strip("_")


def _transfer_token(*, is_hdr: bool, gamma: float) -> str:
    """The EOTF token for a cube name: ``PQ`` for HDR, else ``g<gamma>`` (the dot dropped,
    e.g. γ2.2 → ``g22``). DLC targets pure power γ, so the SDR token names the gamma exactly."""
    return "PQ" if is_hdr else "g" + f"{gamma:.1f}".replace(".", "")


def _gamut_label(colorspace: str, *, is_hdr: bool, gamma: float) -> str:
    """Friendly gamut/standard label for a cube name. sRGB and Rec.709 are the SAME gamut
    (shared primaries) — the colloquial name follows the EOTF, not the primaries: pure power
    γ2.2 reads as ``sRGB``, the BT.1886/γ2.4 broadcast convention reads as ``Rec709`` (owner's
    call). Other gamuts keep their own name with dots dropped (``Rec.2020`` → ``Rec2020``)."""
    cs = (colorspace or "").strip()
    norm = cs.lower().replace(".", "").replace("-", "").replace(" ", "")
    if not is_hdr and norm in ("srgb", "rec709", "bt709"):
        return "Rec709" if gamma >= 2.3 else "sRGB"
    return cs.replace(".", "")


def descriptive_cube_name(*, date: str, display: str, mode: str, colorspace: str,
                          transfer: str, luminance_nits: float) -> str:
    """The descriptive, sortable filename for an installed/deliverable DLC 3D-LUT cube.

    DesktopLUT shows a cube's *filename* (not its containing folder), so the name must be
    self-describing; **date-first** so a folder listing sorts chronologically. Scheme:
    ``<date>_DLC_<display>_<mode>_<colorspace>_<transfer>_<lum>n.cube`` →
    ``2026-06-18_DLC_PA32UCXR_SDR_sRGB_g22_120n.cube``. ``display`` is the short model name,
    ``colorspace`` the target gamut label (verbatim from the profile — not mapped), ``transfer``
    the EOTF token (``g22`` for power γ2.2, ``PQ`` for HDR), ``luminance_nits`` the white (SDR)
    or peak (HDR) level rendered ``<n>n``. Every token is sanitised to filesystem-safe chars.
    This is the canonical name for the durable cube the user keeps and DesktopLUT installs (the
    in-run build artifact under ``runs/<run>/generated/`` stays the generic ``final_<mode>.cube``)."""
    lum = f"{round(luminance_nits)}n"
    tokens = [_fs_safe(date), "DLC", _fs_safe(display), _fs_safe(mode),
              _fs_safe(colorspace), _fs_safe(transfer), lum]
    stem = "_".join(t for t in tokens if t)
    return f"{stem}.cube"


# ---------------------------------------------------------------------------
# Patch-set builders — the one place a flow's stages turn PatchSizes + Transfer into
# an actual sequence (module-level so a pre-run preview can size a run with no live
# Calibration/controller/ctx). The orchestrator's stage builders just delegate here.
# ---------------------------------------------------------------------------

def build_ramp_set(ps: PatchSizes, transfer: Transfer, *,
                   warm_tau: Optional[int] = None,
                   max_cv: Optional[int] = None,
                   hue_sat_caps: Optional[dict] = None) -> list[tuple[int, int, int]]:
    """The MHC FOUNDATION ramp: a dense grey ramp + R/G/B (the matrix+1D fit's inputs); C/M/Y
    only if ``raw_include_secondaries`` (off by default — the volumetric set covers them).

    ``hue_sat_caps`` (verify only): per-primary-hue signal-saturation caps from the panel's
    reachable gamut (:func:`dlc.engine.model.signal_saturation_caps`) — scales each colour ramp
    into the range the panel can render + one clip marker per capped hue. NEVER pass this for the
    RAW characterization ramp: that needs full-saturation pure channels (off=0) to measure the
    panel's primaries + per-channel curves; capping there would destroy the channel model.

    ``max_cv`` caps the top of the generated range (HDR: the target peak's code value, so no
    patch exceeds the reachable sub-peak range); ``None`` ⇒ the full bit-depth range (SDR).

    When ``icc_tube_levels`` > 1 the foundation also carries a near-neutral TUBE (off-axis samples
    around the grey axis) so the matrix + per-channel-1D white-balance correction has the off-axis
    non-additivity data the grey diagonal alone can't provide. The tube is merged into the ramp set
    and the union is re-ordered together for drift safety."""
    ramp = ramp_patches(transfer, steps=ps.raw_ramp_steps, saturations=ps.raw_saturations,
                        spacing=ps.raw_spacing, include_secondaries=ps.raw_include_secondaries,
                        low_light_steps=ps.low_light_steps,
                        low_light_signal=ps.low_light_signal,
                        low_light_bias=ps.low_light_bias,
                        hue_sat_caps=hue_sat_caps,
                        order=ps.order, warm_tau=warm_tau, max_cv=max_cv)
    if not ps.icc_tube_levels or ps.icc_tube_levels <= 1:
        return ramp
    cap = max_cv if max_cv is not None else transfer.max_cv
    tube_levels = uniform_levels(ps.icc_tube_levels, cap)
    tube = near_neutral_tube_patches(transfer, levels=tube_levels, offsets=ps.icc_tube_offsets,
                                     max_cv=cap, order=ps.order, warm_tau=warm_tau)
    seen = set(ramp)
    union = ramp + [p for p in tube if p not in seen]
    return sort_patches(union, ps.order, transfer, warm_tau=warm_tau)   # re-order the whole set


# --- Volumetric BUILD set: gamut-awareness + colorimetric foundation constants -----------------
# Sequencer philosophy (owner): always provide a standard colorimetric foundation (target-gamut
# anchors, per-hue saturation sweeps, grayscale) but concentrate the BULK where it matters for
# practical viewing — ~99 % of content is inside Rec.709 and most of it is in the shadows. The
# foundation below is thin by design; the bulk density still comes from the volumetric mode.
_ANCHOR_LEVEL_FRACS = (0.18, 0.50, 0.85)   # low / near-cusp / high luminance rungs (frac of peak cv)
_ANCHOR_INSET = 0.95                       # just-inside anchor at inset*cap saturation
_FOUNDATION_RAMP_STEPS = 7                 # thin grey + per-hue saturation-sweep ramp
_FOUNDATION_SATURATIONS = (0.5, 1.0)       # sweep shells (capped to reachable); 1.0 carries the clip marker
_FOUNDATION_TUBE_LEVELS = 9                # neutral-tube grey anchors (always present)
_DARK_CHROMA_LEVELS = 4                    # sparse dark near-neutral chroma rungs (dark grey stays dense)
_DARK_CHROMA_OFFSET = 0.15                 # single chroma offset for the sparse dark chroma
_MIN_SEPARATION = 0.012                    # min signal-space spacing for projected bulk (kills micro-dupes)


def _dedup_keep_order(patches: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    seen: set[tuple[int, int, int]] = set()
    out: list[tuple[int, int, int]] = []
    for p in patches:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _volumetric_bulk(ps: PatchSizes, transfer: Transfer, *, warm_tau: Optional[int],
                     max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """The volume-covering bulk: the existing ``tube`` | ``cube`` | ``gamut`` sampling (unchanged)."""
    if ps.volumetric_mode == "cube":
        return cube_patches(transfer, size=ps.cube_size, order=ps.order,
                            low_light_size=ps.low_light_cube_size,
                            low_light_signal=ps.low_light_signal,
                            low_light_bias=ps.low_light_bias,
                            warm_tau=warm_tau, max_cv=max_cv)
    if ps.volumetric_mode == "gamut":
        return gamut_patches(transfer, lum_steps=ps.gamut_lum_steps, hues=ps.gamut_hues,
                             lum_bias=ps.gamut_lum_bias, order=ps.order,
                             low_light_steps=ps.low_light_steps,
                             low_light_signal=ps.low_light_signal,
                             low_light_bias=ps.low_light_bias,
                             warm_tau=warm_tau, max_cv=max_cv)
    if ps.volumetric_mode != "tube":
        raise ValueError(f"unknown volumetric_mode: {ps.volumetric_mode!r} (tube|cube|gamut)")
    return tube_patches(transfer, cube_size=ps.cube_size, tube_size=ps.tube_size,
                        tube_radius=ps.tube_radius, grid_type=ps.grid_type,
                        spines=ps.spines, order=ps.order,
                        low_light_steps=ps.low_light_steps,
                        low_light_cube_size=ps.low_light_cube_size,
                        low_light_signal=ps.low_light_signal,
                        low_light_bias=ps.low_light_bias,
                        warm_tau=warm_tau, max_cv=max_cv)


def _volumetric_neutral_dark(ps: PatchSizes, transfer: Transfer, *, warm_tau: Optional[int],
                             max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """ALWAYS-present neutral tube + dark refinements (A2b). A near-neutral tube around the grey
    axis, a DENSE dark grey ramp (the eye is most sensitive in the shadows), and a SPARSE dark
    near-neutral chroma set (sub-nit chroma is noise-dominated). All in-gamut and PURE STDLIB, so
    they never need gamut projection. They are additive (deduped): largely overlapping ``tube``
    mode's own grey axis + neutral core, and a fuller addition for ``cube`` / ``gamut`` mode."""
    cap = max_cv if max_cv is not None else transfer.max_cv
    out: list[tuple[int, int, int]] = []
    tube_levels = uniform_levels(_FOUNDATION_TUBE_LEVELS, cap)
    out += near_neutral_tube_patches(transfer, levels=tube_levels, offsets=ps.icc_tube_offsets,
                                     max_cv=cap, order=ps.order, warm_tau=warm_tau)
    dark = shadow_levels(ps.low_light_steps, transfer, max_cv=cap,
                         max_signal=ps.low_light_signal, bias=ps.low_light_bias) \
        if ps.low_light_steps and ps.low_light_steps > 1 else []
    out += [(v, v, v) for v in dark if v > 0]
    if dark:
        out += near_neutral_tube_patches(transfer, levels=dark[:_DARK_CHROMA_LEVELS],
                                         offsets=(_DARK_CHROMA_OFFSET,), max_cv=cap,
                                         order=ps.order, warm_tau=warm_tau)
    return out


def _volumetric_foundation(ps: PatchSizes, transfer: Transfer, *, target: Any,
                           reachable_primaries: dict, warm_tau: Optional[int],
                           max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """The reachable colorimetric foundation (HDR / wide-gamut): target-gamut anchors that bracket
    the reachable boundary + a per-hue saturation sweep capped to the reachable gamut + the
    always-present neutral tube & dark refinements. Generated already-reachable, so it is exempt
    from the bulk's gamut projection/thinning (its exact placements are preserved)."""
    from .engine.model import TargetSpace, signal_saturation_caps

    cap_cv = max_cv if max_cv is not None else transfer.max_cv
    # signal_saturation_caps needs the RAW (un-clipped) target space so it can DETECT out-of-gamut
    # chromaticities — a reachable-clipped space pre-projects every patch in-gamut ⇒ caps all 1.0
    # (matches `_hue_sat_caps`, which also passes an un-clipped TargetSpace).
    raw_space = TargetSpace(target)

    # Anchors: per-LEVEL reachable caps (the cap is luminance-dependent under PQ), one bracket per hue.
    anchor_levels = sorted({max(1, int(round(f * cap_cv))) for f in _ANCHOR_LEVEL_FRACS})
    caps_by_level: dict[int, dict[str, float]] = {}
    for V in anchor_levels:
        caps = signal_saturation_caps(raw_space, reachable_primaries, level=V / transfer.max_cv)
        if caps:
            caps_by_level[V] = caps
    anchors = (target_anchor_patches(transfer, levels=anchor_levels, caps_by_level=caps_by_level,
                                     inset=_ANCHOR_INSET, include_secondaries=True,
                                     max_cv=cap_cv, order=ps.order, warm_tau=warm_tau)
               if caps_by_level else [])

    # Per-hue saturation sweep (grey + RGBCMY, capped to reachable) — the "sat sweep" foundation.
    peak_caps = signal_saturation_caps(raw_space, reachable_primaries, level=1.0)
    sweep = ramp_patches(transfer, steps=_FOUNDATION_RAMP_STEPS, saturations=_FOUNDATION_SATURATIONS,
                         spacing=ps.raw_spacing, include_secondaries=True, hue_sat_caps=peak_caps,
                         color_min_signal=ps.low_light_signal, order=ps.order,
                         warm_tau=warm_tau, max_cv=cap_cv)

    return anchors + sweep + _volumetric_neutral_dark(ps, transfer, warm_tau=warm_tau, max_cv=max_cv)


def _project_and_thin(patches: list[tuple[int, int, int]], *, target: Any,
                      reachable_primaries: dict, transfer: Transfer,
                      max_cv: Optional[int]) -> list[tuple[int, int, int]]:
    """Project the bulk stimuli onto the panel's reachable gamut and thin micro-duplicates.

    In-gamut patches round-trip to their own code value (no-op) and are kept unchanged. Out-of-gamut
    patches are pulled to the reachable boundary (``TargetSpace.reachable_signal``); many distinct OOG
    corners collapse onto a thin boundary surface, so the moved set is then thinned to a minimum
    signal-space separation — which (a) kills the "50 patches on 0.001 of blue" waste, (b) makes the
    surviving count scale with reachable gamut volume (fixed spacing × larger volume ⇒ more patches),
    and (c) avoids the near-coincident/collinear train points that would make the RBF singular."""
    if not patches:
        return patches
    import numpy as np
    from .engine.model import TargetSpace

    space = TargetSpace(target, reachable_primaries=reachable_primaries)
    m = float(transfer.max_cv)
    cap_cv = max_cv if max_cv is not None else transfer.max_cv
    orig = np.asarray(patches, dtype=int)
    proj = np.asarray(space.reachable_signal(orig.astype(float) / m), dtype=float)
    cv = np.clip(np.rint(proj * m), 0, cap_cv).astype(int)
    moved = np.any(cv != orig, axis=1)

    kept = [tuple(int(x) for x in row) for row in orig[~moved]]   # in-gamut: untouched, in order
    moved_cv = cv[moved]
    if len(moved_cv):
        order_idx = np.lexsort((moved_cv[:, 2], moved_cv[:, 1], moved_cv[:, 0]))  # deterministic
        moved_sig = moved_cv.astype(float) / m
        min2 = _MIN_SEPARATION ** 2
        kept_sig: list[np.ndarray] = []
        for i in order_idx:
            s = moved_sig[i]
            if kept_sig and float(np.min(np.sum((np.asarray(kept_sig) - s) ** 2, axis=1))) < min2:
                continue
            kept_sig.append(s)
            kept.append(tuple(int(x) for x in moved_cv[i]))
    return _dedup_keep_order(kept)


def build_volumetric_set(ps: PatchSizes, transfer: Transfer, *,
                         warm_tau: Optional[int] = None,
                         max_cv: Optional[int] = None,
                         target: Any = None,
                         reachable_primaries: Optional[dict] = None) -> list[tuple[int, int, int]]:
    """The 3D-LUT sampling set. ``volumetric_mode`` picks HOW the cube interior is sampled:
    a neutral-axis ``tube`` (default; dense where content lives), a uniform ``cube``, or a
    content-weighted ``gamut`` shell set. ``max_cv`` caps the range (HDR peak; see
    :func:`build_ramp_set`).

    The set is always the volume-covering BULK + the always-present neutral tube & dark refinements
    (A2b). When ``target`` AND ``reachable_primaries`` are given (HDR / wide-gamut, from
    :meth:`Calibration._reachable_primaries`), it is additionally made GAMUT-AWARE: the bulk is
    projected onto the panel's physically-reachable gamut + thinned (so the panel is metered where
    it can actually render, not at unreachable Rec.2020 corners), and a reachable colorimetric
    foundation (target-gamut anchors + a per-hue saturation sweep) is unioned in — generated
    already-reachable and exempt from the bulk thinning. ``reachable_primaries=None`` (SDR, or the
    projection-free plan PREVIEW via :func:`flow_patch_counts`) keeps the bulk un-projected and adds
    no anchors — so the preview stays deterministic and the fingerprint stable."""
    bulk = _volumetric_bulk(ps, transfer, warm_tau=warm_tau, max_cv=max_cv)
    if target is None or not reachable_primaries:
        union = bulk + _volumetric_neutral_dark(ps, transfer, warm_tau=warm_tau, max_cv=max_cv)
        return sort_patches(_dedup_keep_order(union), ps.order, transfer, warm_tau=warm_tau)

    bulk = _project_and_thin(bulk, target=target, reachable_primaries=reachable_primaries,
                             transfer=transfer, max_cv=max_cv)
    foundation = _volumetric_foundation(ps, transfer, target=target,
                                        reachable_primaries=reachable_primaries,
                                        warm_tau=warm_tau, max_cv=max_cv)
    union = _dedup_keep_order(foundation + bulk)   # foundation first → its exact placements survive
    return sort_patches(union, ps.order, transfer, warm_tau=warm_tau)


def build_neutral_set(ps: PatchSizes, transfer: Transfer, *,
                      warm_tau: Optional[int] = None,
                      max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The grey-axis ramp measured by the MHC closed-loop D65 grayscale refine (each round
    re-measures this neutral ramp and pulls the MHC correctionGrayscale layer toward D65).
    ``max_cv`` caps the range (HDR peak; see :func:`build_ramp_set`)."""
    cap = max_cv if max_cv is not None else transfer.max_cv
    n = ps.neutral_steps
    levels = uniform_levels(n, cap)
    if ps.low_light_steps > 1:
        levels = sorted(set(levels) | set(shadow_levels(
            ps.low_light_steps, transfer, max_cv=cap,
            max_signal=ps.low_light_signal, bias=ps.low_light_bias)))
    return sort_patches([(v, v, v) for v in levels], ps.order, transfer, warm_tau=warm_tau)


def build_verify_set(ps: PatchSizes, transfer: Transfer, *,
                     warm_tau: Optional[int] = None,
                     max_cv: Optional[int] = None,
                     hue_sat_caps: Optional[dict] = None) -> list[tuple[int, int, int]]:
    """The verify QC set — a purpose-built "cover all bases" check, NOT a clone of the dense build
    ramp. It is *not* the same shape as the run that produced the calibration: it confirms the
    foundation across the range at verification resolution, weighted to what actually matters.

    **One fixed preset shape for BOTH SDR and HDR** (owner directive B): every run is verified by
    the same grayscale-priority + RGBCMY sweep, parametrized only by the PatchSizes ``verify_*``
    defaults (kept as the user's optional lever, not a per-mode branch). The reachable cap
    (``hue_sat_caps``) is the *only* SDR/HDR difference and is a structural no-op for SDR
    (sRGB ⊂ panel ⇒ caps are all 1.0), so the shape is identical across modes.

      * **Grayscale / PQ ramp + shadow toe** (``low_light_steps``) — the EOTF axis, sampled into
        the dark where it matters most. This is the priority.
      * **Colour (RGBCMY) only ABOVE the shadow band** (``verify_color_min_signal``) — sub-nit
        chroma is noise-dominated for both meter and panel, so we don't waste ~7 s/patch
        re-measuring the black floor in every hue/saturation; the grey toe carries the dark EOTF.
      * **Gamut-capped** (``hue_sat_caps``): saturated hues land on/inside the panel's reachable
        gamut (+ one clip marker per capped hue documenting the boundary) — same as the raw verify.

    ``max_cv`` caps the range (HDR peak; so verify never asks for an above-peak highlight that
    would read clipped). Replaces the old heavy mhc-only verify (which re-ran the full build ramp,
    ~45 % sub-nit patches at ~7 s each); the build's own dark model is untouched."""
    return ramp_patches(transfer, steps=ps.verify_steps, saturations=ps.verify_saturations,
                        spacing=ps.raw_spacing, include_secondaries=True,
                        low_light_steps=ps.low_light_steps,
                        low_light_signal=ps.low_light_signal,
                        low_light_bias=ps.low_light_bias,
                        hue_sat_caps=hue_sat_caps,
                        color_min_signal=ps.verify_color_min_signal,
                        order=ps.order, warm_tau=warm_tau, max_cv=max_cv)


# The patch sets each flow MEASURES, keyed by measure-stage role (so a plan/preview can show
# the run's size before any measurement). build-correction measures nothing through spotread.
_FLOW_PATCH_STAGES: dict[str, tuple[str, ...]] = {
    "full": ("raw", "post-mhc", "verify"),
    "mhc-only": ("raw", "verify"),
    "3dlut-only": ("post-mhc", "verify"),
}
_PATCH_BUILDERS = {"raw": build_ramp_set, "verify-ramp": build_ramp_set,
                   "post-mhc": build_volumetric_set, "verify": build_verify_set}


def flow_patch_counts(flow: str, ps: PatchSizes, transfer: Transfer, *,
                      max_cv: Optional[int] = None) -> dict[str, Any]:
    """Per-stage patch counts for ``flow`` from a PatchSizes + Transfer — the run's size, so
    the agent/user can judge time/cost up front. Cheap (pure-stdlib generation); each distinct
    builder is generated once. ``max_cv`` caps the range (HDR peak), so the previewed counts
    match what the run actually measures."""
    roles = _FLOW_PATCH_STAGES.get(flow, ())
    cache: dict[Any, int] = {}
    stages: dict[str, int] = {}
    for role in roles:
        fn = _PATCH_BUILDERS[role]
        if fn not in cache:
            cache[fn] = len(fn(ps, transfer, max_cv=max_cv))
        stages[role] = cache[fn]
    return {"stages": stages, "total_patches": sum(stages.values()),
            "volumetric_mode": ps.volumetric_mode, "order": ps.order}


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def correction_store_path(profile: cp.Profile, ctx_root: Path) -> Path:
    """Where the cross-run per-display correction store lives: profile-adjacent when the
    profile is on disk (durable across ``runs/`` prunes), else beside the run folders
    (tests / synthetic profiles). Shared by the orchestrator and the live CLI so both see
    the same store."""
    if profile.source_path:
        base = Path(profile.source_path).resolve().parent
    else:
        base = ctx_root.parent if ctx_root.parent != ctx_root else ctx_root
    return base / "correction_store.json"


def dip_store_path(profile: cp.Profile, ctx_root: Path) -> Path:
    """Where the cross-run per-display Display+Instrument Profile store lives: alongside the
    profile when it's on disk (durable across ``runs/`` prunes), else beside the run folders
    (tests / synthetic profiles). Mirrors :func:`correction_store_path` so the orchestrator
    and the live CLI agree on one DIP store."""
    if profile.source_path:
        base = Path(profile.source_path).resolve().parent
    else:
        base = ctx_root.parent if ctx_root.parent != ctx_root else ctx_root
    return base / "dip_store.json"


def _render_cmd(argv: Sequence[Any]) -> str:
    """Render an argv list as a copy-pasteable command line (Windows quoting)."""
    import subprocess
    return subprocess.list2cmdline([str(a) for a in argv])


def active_correction(profile: cp.Profile, store: CorrectionStore, display_name: str) -> Optional[str]:
    """The colorimeter correction the meter should actually use: the store's recorded
    correction (e.g. a freshly probe-matched one) overrides the profile YAML; falls back
    to ``profile.meter.correction.file``. The store is the machine-maintained record; the
    profile is the human-authored config (the §2 skill ⊥ user-data boundary)."""
    rec = store.get(display_name)
    if rec and rec.correction_file:
        return rec.correction_file
    return profile.meter.correction.file


def _render_report_html(p: dict[str, Any]) -> str:
    v = p.get("verification") or {}
    lut = p.get("lut3d") or {}
    analysis = p.get("display_analysis")

    # HDR scores dE_ITP, SDR CIEDE2000 — label the deliverable report with the run's actual
    # metric (the verify digest carries it) so dE_ITP numbers are never shown as "dE2000".
    metric = v.get("metric", "CIEDE2000")
    de = "dE_ITP" if metric == "dE_ITP" else "dE2000"

    def metric_row(label: str, key: str) -> str:
        return f"<tr><td>{label}</td><td>{v.get(key, '—')}</td></tr>"

    analysis_block = (f"<h2>Display analysis</h2><p>{analysis}</p>" if analysis
                      else "<h2>Display analysis</h2><p class='muted'>"
                           "(the calibrating assistant adds a short panel analysis here — "
                           "strengths, weaknesses, and why it behaves as it does.)</p>")
    return (
        "<!doctype html><meta charset='utf-8'><title>DLC report</title>"
        "<style>body{font-family:system-ui;margin:2rem;color:#1a1a1a;max-width:48rem}"
        "table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #ccc;padding:.35rem .8rem;text-align:left}"
        "th{background:#f4f4f4}.muted{color:#888}.warn{color:#b54}.ok{color:#393}code{background:#f0f0f0;padding:.1rem .3rem}</style>"
        f"<h1>DesktopLUT Calibrator — {p.get('display')} · {p.get('mode')} · {p.get('flow')}</h1>"
        f"<p>Target <code>{p.get('target')}</code> · {p.get('date')} · "
        f"3D LUT {'converged' if lut.get('converged') else 'best-effort'} "
        f"(max dE {lut.get('best_max_de', '—')})</p>"
        f"<h2>Verification ({metric})</h2><table><tr><th>Metric</th><th>After</th></tr>"
        + metric_row(f"Average {de}", "avg_de2000")
        + metric_row(f"P95 {de}", "p95_de2000")
        + metric_row(f"Max {de}", "max_de2000")
        + metric_row(f"White {de}", "white_de2000")
        + metric_row(f"Grayscale avg {de}", "grayscale_avg_de2000")
        + "</table>"
        + analysis_block
    )


# ---------------------------------------------------------------------------
# Convenience entry + CLI
# ---------------------------------------------------------------------------

def run_calibration(
    *,
    flow: str,
    monitor: int,
    mode: str,
    controller: CalibrationController,
    measure: MeasureFn,
    profile: Optional[cp.Profile] = None,
    probe: Optional[ProbeFn] = None,
    adjudicator: Optional[Adjudicator] = None,
    ctx: Optional[RunContext] = None,
    run_date: Optional[date] = None,
    bit_depth: Optional[int] = None,
    loop_config: Optional[MeasureLoopConfig] = None,
    optimize_config: Optional[OptimizeConfig] = None,
    patch_sizes: Optional[PatchSizes] = None,
    force: bool = False,
    adaptive_planning: bool = False,
    require_hardware_readiness: bool = False,
) -> CalibrationResult:
    """Build a :class:`Calibration` and run a flow. The default adjudicator is
    :class:`AutoAdjudicator` (autonomous). Pass a :class:`MappingAdjudicator` for the
    live LLM pause/resume model."""
    profile = profile or cp.load_profile()
    ctx = ctx or create_run(normalize_mode(mode), display=profile.display_for(monitor).name)
    calib = Calibration(
        ctx=ctx, profile=profile, monitor=monitor, mode=mode, controller=controller,
        measure=measure, adjudicator=adjudicator or AutoAdjudicator(), probe=probe,
        bit_depth=bit_depth, loop_config=loop_config, optimize_config=optimize_config,
        patch_sizes=patch_sizes, run_date=run_date, force=force,
        adaptive_planning=adaptive_planning,
        require_hardware_readiness=require_hardware_readiness)
    return calib.run(flow)


def _auto_on_live_measuring_run(args: Any) -> bool:
    """``--auto`` is a pure rubber-stamp (returns the recommendation verbatim, no LLM) and is sim/CI
    ONLY — never a hardware run (DESIGN LAW). ``main()`` always connects to the live pipe and builds a
    real meter + presenter for any MEASURING flow, so ``--auto`` there is the forbidden autonomous
    hardware run. True ⇒ refuse. ``build-correction`` is operator-driven ccxxmake (no autonomous
    spotread measurement) and an ``--abort`` just reverts, so both are exempt."""
    return bool(getattr(args, "auto", False)) and not getattr(args, "abort", False) \
        and getattr(args, "flow", None) != "build-correction"


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - live wiring
    """Live CLI. Wires the real controller + dogegen/spotread measure seam and runs a
    flow; on an :class:`AdjudicationRequired` pause it prints the request as JSON and
    exits 10 so the LLM can decide and resume (``--decide key=choice --run <dir>``)."""
    import argparse

    parser = argparse.ArgumentParser(prog="dlc-calibrate", description="DLC v2 scripted calibration orchestrator")
    parser.add_argument("--flow", default="full", choices=sorted(FLOWS))
    parser.add_argument("--monitor", type=int, default=0)
    parser.add_argument("--mode", default="SDR")
    parser.add_argument("--run", type=Path, default=None, help="run dir (resume an existing run)")
    parser.add_argument("--profile", type=Path, default=None)
    parser.add_argument("--bit-depth", type=int, default=None, dest="bit_depth")

    # ---- Patch sequence / run-size control (override the profile's `patches:` block) -------
    # Every flag is default=None ⇒ "not set" (keep the profile value, else the built-in
    # default), so a run is never stuck with a preset. --preview-patches prints the resulting
    # per-stage patch counts and exits, so the size/time can be decided BEFORE measuring.
    patch = parser.add_argument_group("patch sequence (run size/time — overrides profile patches:)")
    patch.add_argument("--raw-steps", type=int, default=None, dest="raw_ramp_steps",
                       help="steps per channel for the MHC foundation ramp — grey + R/G/B (default 32).")
    patch.add_argument("--raw-saturations", type=float, nargs="+", default=None, dest="raw_saturations",
                       help="primary saturation shells, e.g. 1.0 0.5 0.25 (default 1.0).")
    patch.add_argument("--raw-secondaries", action="store_true", default=None, dest="raw_include_secondaries",
                       help="also measure C/M/Y ramps in the MHC stage (off by default — the matrix+1D "
                            "can't fit secondaries; the volumetric 3D-LUT set covers them).")
    patch.add_argument("--raw-spacing", choices=["uniform", "perceptual"], default=None, dest="raw_spacing")
    patch.add_argument("--icc-tube-levels", type=int, default=None, dest="icc_tube_levels",
                       help="grey anchor levels for the near-neutral TUBE in the MHC foundation — "
                            "off-axis samples around the grey axis that characterize the white-balance / "
                            "non-additivity region the matrix+1D corrects through (0 ⇒ off, the default).")
    patch.add_argument("--icc-tube-offsets", type=float, nargs="+", default=None, dest="icc_tube_offsets",
                       help="tube chroma offsets as fractions of the level, e.g. 0.06 0.15 (default).")
    patch.add_argument("--volumetric-mode", choices=["tube", "cube", "gamut"], default=None,
                       dest="volumetric_mode", help="how the 3D-LUT set samples the cube (default tube).")
    patch.add_argument("--cube-size", type=int, default=None, dest="cube_size",
                       help="volumetric cube axis (default 9).")
    patch.add_argument("--tube-size", type=int, default=None, dest="tube_size")
    patch.add_argument("--tube-radius", type=int, default=None, dest="tube_radius")
    patch.add_argument("--grid-type", choices=["cub", "bcc"], default=None, dest="grid_type")
    patch.add_argument("--spines", action="store_true", default=None,
                       help="tube: add RGBCMY gamut-edge spines.")
    patch.add_argument("--gamut-lum-steps", type=int, default=None, dest="gamut_lum_steps")
    patch.add_argument("--gamut-hues", type=int, default=None, dest="gamut_hues")
    patch.add_argument("--gamut-lum-bias", type=float, default=None, dest="gamut_lum_bias")
    patch.add_argument("--verify-steps", type=int, default=None, dest="verify_steps",
                       help="verify sanity-ramp steps per channel (default 13 — lighter than the build).")
    patch.add_argument("--verify-saturations", type=float, nargs="+", default=None, dest="verify_saturations",
                       help="verify saturation shells (default 1.0 0.5 — saturated + practical mid-sat).")
    patch.add_argument("--neutral-steps", type=int, default=None, dest="neutral_steps",
                       help="grey-axis ramp steps measured by the MHC D65 grayscale refine "
                            "(the correctionGrayscale closed loop; default 17).")
    patch.add_argument("--low-light-steps", type=int, default=None, dest="low_light_steps",
                       help="extra ramp/tube levels inside the shadow band (default 9).")
    patch.add_argument("--low-light-cube-size", type=int, default=None, dest="low_light_cube_size",
                       help="dark mini-cube axis for extra 3D-LUT shadow samples (default 5).")
    patch.add_argument("--low-light-signal", type=float, default=None, dest="low_light_signal",
                       help="upper bound of the extra shadow band as signal fraction (default 0.20).")
    patch.add_argument("--low-light-bias", type=float, default=None, dest="low_light_bias",
                       help="shadow level bias; >1 packs samples toward black (default 2.0).")
    patch.add_argument("--patch-order", choices=["thermal", "luminance", "random"], default=None,
                       dest="order", help="patch ordering — thermal (drift-safe) default.")
    patch.add_argument("--preview-patches", action="store_true", dest="preview_patches",
                       help="print the per-stage patch counts for the flow (the run's size) and exit, "
                            "WITHOUT measuring — decide the time/size first.")

    # ---- characterize tuning (the `characterize` flow only; default=None ⇒ keep the built-in) ----
    char = parser.add_argument_group("characterize (DIP learning run — overrides the defaults)")
    char.add_argument("--char-noise-levels", type=float, nargs="+", default=None, dest="char_noise_levels",
                      help="signal levels (code-value fractions) to estimate read σ at (default 1.0 0.5 0.18 0.05).")
    char.add_argument("--char-noise-reads", type=int, default=None, dest="char_noise_reads",
                      help="back-to-back reads per noise level (σ-estimation sample; default 20).")
    char.add_argument("--char-black-reads", type=int, default=None, dest="char_black_reads")
    char.add_argument("--char-primary-reads", type=int, default=None, dest="char_primary_reads")
    char.add_argument("--char-creep-reads", type=int, default=None, dest="char_creep_reads")
    char.add_argument("--char-settle-reads", type=int, default=None, dest="char_settle_reads",
                      help="max reads per settle level before flagging (default 40).")
    char.add_argument("--char-warmup-max-minutes", type=float, default=None, dest="char_warmup_max_minutes",
                      help="run/skip toggle for the closed-loop thermal phase (0 ⇒ SKIP it, e.g. a quick "
                           "mechanism check; >0 ⇒ run it — the real bound is --char-thermal-max-blocks).")
    char.add_argument("--char-warmup-stable", type=float, default=None, dest="char_warmup_stable",
                      help="(legacy static-hold knob) windowed creep dE/min for 'stable' (default 0.15).")
    char.add_argument("--char-thermal-max-blocks", type=int, default=None, dest="char_thermal_max_blocks",
                      help="closed-loop thermal observation bound in BLOCKS; exceeding it FLAGs (default 240).")
    char.add_argument("--char-thermal-load-reads", type=int, default=None, dest="char_thermal_load_reads",
                      help="scaled-content reads per thermal block (the heat per block; default 12).")
    char.add_argument("--char-thermal-ref-reads", type=int, default=None, dest="char_thermal_ref_reads",
                      help="neutral-sensor reads per thermal block (warm-in + noise self-cal; default 5).")
    char.add_argument("--char-thermal-window", type=int, default=None, dest="char_thermal_window",
                      help="sliding window (blocks) for the net/gross convergence judgement (default 5).")
    char.add_argument("--char-thermal-k-start", type=float, default=None, dest="char_thermal_k_start",
                      help="SOAK luminance scale while warm-in is measured (1.0 ⇒ no preheat; default 1.6).")
    char.add_argument("--char-eotf-reads", type=int, default=None, dest="char_eotf_reads",
                      help="reads averaged per EOTF/white sweep level (0 ⇒ SKIP the sweep; default 3).")
    char.add_argument("--char-eotf-levels", type=float, nargs="+", default=None, dest="char_eotf_levels",
                      help="signal levels (code-value fractions) for the EOTF + white-vs-luminance "
                           "sweep (default 0.1 0.2 0.3 0.4 0.5 0.65 0.8 0.9 1.0).")

    parser.add_argument("--dogegen-server", default=None, dest="dogegen_server",
                        metavar="HOST:PORT",
                        help="drive a PERSISTENT dogegen daemon (dlc.dogegen_server) over a local "
                             "socket instead of spawning a window per step — start it once, Alt+Enter "
                             "it fullscreen, reuse it across the whole run (required for 10-bit).")
    parser.add_argument("--keep-dogegen-server", action="store_true", dest="keep_dogegen_server",
                        help="do NOT stop the persistent dogegen daemon when the run finishes "
                             "(default: a terminal run sends it `quit`, closing its window). A "
                             "pause/resume never stops it regardless.")
    meter_group = parser.add_mutually_exclusive_group()
    meter_group.add_argument("--persistent-meter", action="store_true", dest="persistent_meter",
                             default=True,
                             help="DEFAULT: drive ONE long-lived interactive spotread across the whole "
                                  "pass (calibrate once, one reading per trigger) instead of re-spawning "
                                  "+ re-calibrating spotread per read. ~4x faster on bright patches, "
                                  "~1.2x on dark; reads agree with the per-spawn path within meter noise "
                                  "(A/B 2026-06-23: dE2000 mean 0.030). This flag is now a no-op kept for "
                                  "back-compat; use --legacy-meter to opt back to per-spawn.")
    meter_group.add_argument("--legacy-meter", "--no-persistent-meter", action="store_false",
                             dest="persistent_meter",
                             help="OPT-OUT fallback: re-spawn + re-calibrate a fresh spotread for EVERY "
                                  "read (the old per-patch path). ~4x slower on bright patches; use only "
                                  "if the persistent meter misbehaves on a given box.")
    parser.add_argument("--decide", action="append", default=[], metavar="KEY=CHOICE",
                        help="record a seam decision (repeatable) then run/resume")
    parser.add_argument("--adaptive-planning", action="store_true", dest="adaptive_planning",
                        help="OPT-IN, EXPERIMENTAL (value unproven — a synthetic A/B found denser "
                             "sampling does not beat the optimizer's fold-back; see patch_evidence.py): "
                             "pause after the ICC and let the LLM investigate the panel/run (evidence "
                             "packet + `python -m dlc.patch_evidence` tools) and choose the patch "
                             "strategy. Autonomous (--auto) runs use a conservative fallback.")
    parser.add_argument("--plan-decision-file", type=Path, default=None, dest="plan_decision_file",
                        help="resume the adaptive-planning seam with a structured decision JSON file "
                             "(keys: shadow_treatment, volumetric_density, patch_size_overrides, "
                             "reason, confidence). Validated + clamped to bounds before it is applied.")
    parser.add_argument("--auto", action="store_true",
                        help="auto-adjudicate EVERY seam by its recommendation (no pauses, no LLM) — "
                             "for sim/CI/reproducible runs, NOT an unattended hardware run")
    parser.add_argument("--supervised", action="store_true",
                        help="autonomous, but PAUSE for a live judge at safety-critical seams "
                             "(foundation collapse / optimizer floor / failed verify) — the mode for "
                             "an unattended HARDWARE run; a clean run never pauses")
    parser.add_argument("--checkin-interval", type=float, default=600.0, dest="checkin_interval",
                        metavar="SECONDS",
                        help="§12 timed check-in floor: past this many seconds, the next safe "
                             "checkpoint EMITS a rich evidence packet (run overview + events since the "
                             "last check-in) for the LLM to consume from the running spine, so a long "
                             "run never goes dark. Default 600 (10 min). A check-in is emit-only — it "
                             "NEVER gates or pauses the spine and carries no recommendation (all modes); "
                             "0 disables.")
    parser.add_argument("--neutral-min-reads", type=int, default=None, dest="neutral_min_reads",
                        metavar="N",
                        help="per-patch read FLOOR on near-neutral patches (grey ramp + tube): average "
                             "at least N reads there so the chroma-critical matrix/WB/non-additivity "
                             "region isn't biased by single-read meter noise. The DIP still escalates "
                             "above N on luminance SNR; this is also the no-DIP fixed-N fallback. "
                             "Default off (1).")
    parser.add_argument("--neutral-chroma-span", type=float, default=None, dest="neutral_chroma_span",
                        metavar="FRAC",
                        help="near-neutral discriminator for --neutral-min-reads: a patch counts as "
                             "near-neutral when (max-min) <= FRAC*max of its signal (default 0.35 — "
                             "covers the 0.06/0.15 tube, excludes pure-channel/secondary ramps).")
    parser.add_argument("--neutral-floor-min-nits", type=float, default=None, dest="neutral_floor_min_nits",
                        metavar="NITS",
                        help="luminance gate on --neutral-min-reads: only floor near-neutral patches at "
                             "or above NITS expected luminance (default 0 = no gate). Dim patches have the "
                             "smallest measured σ (averaging buys least) and are the slowest to read + most "
                             "thermally risky (long dwell at low backlight), so gate the floor to the "
                             "brighter, faster, larger-σ near-neutral patches.")
    parser.add_argument("--dark-min-reads", type=int, default=3, dest="dark_min_reads",
                        metavar="N",
                        help="per-patch read FLOOR on DIM near-neutral patches (≤ --dark-floor-max-nits): "
                             "take ≥N reads so their read-to-read CHROMATICITY spread can be measured — "
                             "that spread drives the dark-level trust (how much to smooth a dark "
                             "correction to identity, since near-black chroma is the unreliable axis). "
                             "Default 3; 1 disables.")
    parser.add_argument("--dark-floor-max-nits", type=float, default=2.0, dest="dark_floor_max_nits",
                        metavar="NITS",
                        help="luminance ceiling for --dark-min-reads (default 2.0): near-neutral patches "
                             "at or below this expected luminance get the dark read floor.")
    parser.add_argument("--force", action="store_true", help="ignore stage memoisation")
    parser.add_argument("--abort", action="store_true",
                        help="cancel: roll DesktopLUT back to the user's pre-run setup "
                             "(restore the calibration snapshot) and exit. Use to bail out of "
                             "a paused/abandoned run without leaving a half-applied profile.")
    parser.add_argument("--cancel", action="store_true",
                        help="signal a RUNNING run (--run <dir>) to stop: writes control.json; the "
                             "live process rolls back to the pre-run setup at its next checkpoint / "
                             "stage boundary. The actionable half of mid-run gating — an LLM/operator "
                             "watching the dashboard can stop a run going wrong without --abort.")
    parser.add_argument("--set-hdr", choices=["on", "off", "toggle"], default=None, dest="set_hdr",
                        help="flip monitor --monitor between SDR and HDR (the same OS advanced-color "
                             "switch as DesktopLUT's HDR-toggle hotkey) and EXIT — no calibration. "
                             "Use before an HDR run: --set-hdr on, then start the HDR dogegen daemon, "
                             "then characterize/calibrate.")
    args = parser.parse_args(argv)

    # Standalone display-mode switch: flip the monitor's OS HDR state and exit. Independent
    # of any profile/run/measure stack (just the pipe) so it works as a quick pre-run step —
    # put the panel in HDR, start the matching dogegen daemon, THEN run characterize.
    if args.set_hdr is not None:
        controller = CalibrationController.connect()
        try:
            res = apply_set_hdr(controller, args.monitor, args.set_hdr)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"status": "set_hdr_failed", "monitor": args.monitor,
                              "action": args.set_hdr, "error": f"{type(exc).__name__}: {exc}",
                              "hint": "needs DesktopLUT running with the calibration pipe armed "
                                      "and a build that supports windows.set_hdr"}, indent=2))
            return 1
        print(json.dumps({"status": "set_hdr", "action": args.set_hdr, **res}, indent=2))
        return 0

    # Cooperative cancel of a RUNNING/paused run: drop control.json into its run dir; the live
    # process (or the next resume) picks it up at a checkpoint/stage boundary and rolls back.
    # No profile or measure stack needed — just the file, so it works even if the live pipe is busy.
    if args.cancel:
        if not args.run:
            print(json.dumps({"status": "cancel_failed",
                              "error": "--cancel needs --run <dir> (the run to stop)"}, indent=2))
            return 1
        ctrl = Path(args.run) / "control.json"
        try:
            ctrl.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(ctrl, json.dumps(
                {"action": "cancel", "requested": datetime.now().isoformat(timespec="seconds")}, indent=2))
        except OSError as exc:
            print(json.dumps({"status": "cancel_failed", "error": f"{type(exc).__name__}: {exc}",
                              "run": str(args.run)}, indent=2))
            return 1
        print(json.dumps({"status": "cancel_requested", "run": str(args.run), "control": str(ctrl),
                          "note": "the running process rolls back at its next checkpoint / stage boundary"},
                         indent=2))
        return 0

    profile = cp.load_profile(args.profile)

    # The run's patch plan: the profile's `patches:` defaults, overridden by any patch CLI
    # flags (each default=None ⇒ unset). One source of run size for both preview and the run,
    # so a run is never stuck with a preset sequence.
    patch_sizes = PatchSizes.from_dict(profile.patches).merged(
        **{f.name: getattr(args, f.name, None) for f in fields(PatchSizes)})

    # HDR foundation ramps use UNIFORM (even PQ-signal) spacing. The per-channel 1D .cube EOTF
    # correction (dlc.mhc_cube) is built from these grey + R/G/B ramps, and PQ (ST.2084) is ALREADY
    # perceptually uniform by design (equal 10-bit code steps ≈ equal Barten perceptual steps) — so
    # even-signal steps give a balanced near-black→peak ladder (e.g. for a 1800-nit cap: ~12 of 32
    # points below 10 nits, ~8 across the 10–100 nit diffuse range, ~6 in highlights). The additive
    # low_light_steps then layer MORE toe density on top. The "perceptual" mode (perceptual_levels,
    # space_gamma≈2.2) is an SDR-gamma construct: layering a 2.2 curve on top of PQ shoves samples
    # into the bright end (measured: 15/32 points above 400 nits, only 3 below 10) — wrong for a PQ
    # panel. SDR (true power-law) is where "perceptual" belongs; HDR stays uniform. An explicit
    # --raw-spacing or a profile `raw_spacing:` still overrides.

    if args.preview_patches:
        # Decide the time/size BEFORE committing: print the per-stage patch counts for the flow
        # and exit. Pure offline sizing — no run folder, controller, dogegen, or meter.
        mode = normalize_mode(args.mode)
        bd = args.bit_depth if args.bit_depth is not None else (10 if mode == "HDR" else 8)
        target = profile.display_for(args.monitor).target_name(mode)
        out: dict[str, Any] = {"flow": args.flow, "monitor": args.monitor, "mode": mode,
                               "patch_sizes": asdict(patch_sizes)}
        out["patch_plan"] = (flow_patch_counts(args.flow, patch_sizes,
                                               profile.transfer_for(target, bit_depth=bd))
                             if target else {"note": f"no {mode} target for monitor {args.monitor}"})
        print(json.dumps(out, indent=2))
        return 0

    ctx = open_run(args.run) if args.run and (args.run / "manifest.json").exists() \
        else create_run(normalize_mode(args.mode), display=profile.display_for(args.monitor).name,
                        run_dir=args.run)

    # Seed decisions from the run-record + any new --decide flags. The --decide flags are ALSO
    # kept as explicit overrides so they win over an already-recorded decision on resume (the
    # seed map alone can't — adjudicate() replays a recorded key before consulting the
    # adjudicator). See Calibration.adjudicate().
    state = _common.load_dlc_state(ctx)
    # The LIVE meter/dogegen stack below is built BEFORE the orchestrator, so it must use the
    # SAME spec the orchestrator will resolve — on a resume the persisted run record (not the
    # CLI defaults) is authoritative. resolve_run_spec/flow are pure + deterministic, so these
    # match Calibration's own reconciliation exactly. The orchestrator is still constructed from
    # the RAW args (below) so it can detect + surface a mis-issued resume command, not silence it.
    # The orchestrator (constructed from the RAW args below) re-derives + SURFACES any conflict,
    # so main only needs the resolved values to build the live stack — discard the conflict list.
    eff_mode, _eff_bd, _ = resolve_run_spec(ctx, state, mode=args.mode, bit_depth=args.bit_depth)
    eff_flow, _ = resolve_run_flow(state, args.flow)
    recorded = (state.get("calib", {}) or {}).get("decisions", {})
    decisions = {k: Decision(v["choice"], v.get("note"), payload=v.get("payload"))
                 for k, v in recorded.items()}
    overrides: dict[str, Decision] = {}
    for spec in args.decide:
        key, _, choice = spec.partition("=")
        overrides[key.strip()] = Decision(choice.strip(), note="cli")
    # The adaptive-planning seam answers with a structured decision file, not a one-of-N choice.
    if args.plan_decision_file is not None:
        try:
            plan_payload = json.loads(Path(args.plan_decision_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": f"could not read --plan-decision-file: {exc}"}))
            return 2
        overrides["adaptive-planning:plan"] = Decision(
            "apply", note="cli plan-decision-file", payload=plan_payload)
    decisions.update(overrides)   # also seed the adjudicator (covers not-yet-recorded keys)
    # DESIGN LAW: --auto is a pure rubber-stamp (no LLM) for sim/CI ONLY — never a live measuring run,
    # which main() always wires (real pipe + meter + presenter). It would optimize for hours on an
    # unadjudicated foundation (the first-HDR-run failure). sim/CI drives the in-process Orchestrator.
    if _auto_on_live_measuring_run(args):
        print(json.dumps({"error": (
            "--auto (pure rubber-stamp, no LLM) must not drive a live measuring run — it would optimize "
            "for hours on an unadjudicated foundation. It is sim/CI only. Run live without --auto (default "
            "MappingAdjudicator routes every seam to the LLM) or with --supervised, and use the in-process "
            "simulator for sim/CI.")}))
        return 2
    if args.auto:
        adjudicator: Adjudicator = AutoAdjudicator()
    elif args.supervised:
        adjudicator = SupervisedAdjudicator(decisions)
    else:
        adjudicator = MappingAdjudicator(decisions)

    from .measure_loop import (  # lazy: live only
        DogegenPresenter, SocketPresenter, make_spotread_meter, make_persistent_spotread_meter,
    )
    from .argyll import Argyll, SpotreadRequest
    from .dogegen import DogegenPatchDisplay
    from .measure_rgbw import resolve_spotread_instrument_port

    controller = CalibrationController.connect()

    # Explicit cancel: restore the user's pre-run setup and exit (no measurement stack needed).
    if args.abort:
        restored = False
        try:
            controller.exit_calibration(restore_snapshot=True)
            restored = True
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"status": "abort_failed", "error": f"{type(exc).__name__}: {exc}",
                              "run": str(ctx.root)}, indent=2))
            return 1
        bak = (state.get("calib", {}) or {}).get("backup", {})
        print(json.dumps({"status": "reverted", "restored_snapshot": restored,
                          "backup": bak, "run": str(ctx.root)}, indent=2))
        return 0

    # The live measurement stack (dogegen patch display + spotread meter) is only needed by
    # flows that MEASURE. build-correction mints a colorimeter correction via interactive
    # ccxxmake — run by the operator at the box — and never measures through spotread here,
    # so it needs neither dogegen nor a meter. Skip the whole stack (keeps the build robust
    # even when dogegen / the live pipe aren't configured) and run with no measure function.
    # One effective bit depth drives BOTH dogegen's mode AND the patch generator, so the code
    # values dogegen renders match what the patches encode (no 8/10 mismatch). SDR defaults to
    # 8-bit (dogegen "mode 8", composited — 3D-LUT-safe); --bit-depth 10 opts into 10-bit
    # ("mode 10"), which needs the TPG window borderless-fullscreened to render accurately.
    # Resolved against the persisted run spec (see above); when nothing is persisted/explicit
    # (_eff_bd is None) keep main()'s long-standing live default: 10-bit HDR, 8-bit SDR.
    bit_depth = _eff_bd if _eff_bd is not None else (10 if eff_mode == "HDR" else 8)
    presenter = None
    persistent_meter = None
    measure: Optional[MeasureFn] = None
    if eff_flow != "build-correction":
        argyll_dir = profile.paths.get("argyll")
        argyll = Argyll(Path(argyll_dir) / "spotread.exe") if argyll_dir else None
        # resolve_spotread_instrument_port returns (port, info) — MUST unpack; passing the
        # whole tuple as the port makes spotread's "-c" a stringified tuple → "out of range"
        # → no reading → 0.0 nits on every read.
        if argyll:
            port, _ = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
        else:
            port = profile.meter.argyll_port
        # Per-patch presenter dwell: prefer the panel's MEASURED step-response settle (from the
        # DIP) over the guessed 0.5 s — a fast panel runs leaner, a slow mini-LED waits long
        # enough. EXCEPT during `characterize` itself, which must observe the raw step response
        # (waiting out a prior settle estimate would hide it), so it keeps the paint-safe default.
        dip_rec = DipStore.load(dip_store_path(profile, ctx.root)).get(
            profile.display_for(args.monitor).name)
        # Floor the dwell so a fast panel (measured settle ≈ 0) still gets a paint-safe wait, while a
        # slow-ABL panel's larger measured settle is honoured. characterize keeps the default (it must
        # observe the raw step response, and its settle measurement is dwell-independent anyway).
        if eff_flow != "characterize" and dip_rec is not None and dip_rec.settle_seconds is not None:
            presenter_settle = max(0.2, dip_rec.settle_seconds)
        else:
            presenter_settle = 0.5
        if args.dogegen_server:
            # Reuse a persistent, operator-fullscreened dogegen window across invocations
            # (no respawn/flash) — the daemon owns the dogegen process + its bit-depth mode.
            host, _, srv_port = args.dogegen_server.partition(":")
            presenter = SocketPresenter(host or "127.0.0.1", int(srv_port or 28930),
                                        settle_seconds=presenter_settle)
        else:
            dogegen_path = profile.paths.get("dogegen")
            if not dogegen_path:
                raise SystemExit("profile paths.dogegen is required for measuring flows "
                                 "(the patch generator executable, e.g. third_party/dogegen/dogegen.exe)")
            # Place the spawned window on the calibration target monitor (dogegen opens on the
            # Windows primary and has no monitor-select CLI). Composited move-only by default so
            # the 3D LUT still applies; best-effort — a pipe hiccup just leaves it on the primary.
            from .dogegen_window import resolve_monitor_rect
            try:
                place_rect = resolve_monitor_rect(
                    (controller.query_monitors() or {}).get("monitors"), args.monitor)
            except Exception:  # noqa: BLE001 - advisory placement; never block the run
                place_rect = None
            presenter = DogegenPresenter(DogegenPatchDisplay(Path(dogegen_path), eff_mode,
                                                             bit_depth=bit_depth),
                                         settle_seconds=presenter_settle, place_rect=place_rect)
        # The active correction comes from the store first (a freshly probe-matched .ccmx)
        # then the profile — so a build-correction run is picked up without editing the YAML.
        store = CorrectionStore.load(correction_store_path(profile, ctx.root))
        correction = active_correction(profile, store, profile.display_for(args.monitor).name)
        ccmx = Path(correction) if correction else None
        # DEFAULT (persistent_meter=True): hold ONE interactive spotread open across the whole
        # pass — calibrate once, one reading per trigger. A/B-validated as a true drop-in for the
        # per-spawn path (2026-06-23, PA32UCXR + i1Display3: dE2000 mean 0.030, max 0.087; no bias
        # from holding spotread open) and ~4x faster on bright patches, so it is the measuring
        # default. `--legacy-meter` opts back to the per-spawn path below.
        #
        # CAVEAT — one USB i1D3 cannot back two live spotread instances: anything that needs a
        # FRESH spotread mid-run (e.g. a ccmx build via ccxxmake) must close this persistent meter
        # first. That is structurally enforced here: the only ccmx-mint path is the
        # `build-correction` flow, which never reaches this branch (it is gated out at
        # `args.flow != "build-correction"` above and measures nothing through spotread) and runs
        # ccxxmake from a PAUSED orchestrator — i.e. a separate invocation where this meter is
        # already closed in the finally. So the persistent meter and ccxxmake never coexist.
        if args.persistent_meter:
            # Identical instrument config to the one-shot (same port + correction) so it is a true
            # drop-in. The caller owns its lifecycle: closed in finally + _stall_kill.
            if argyll is None:
                raise SystemExit("measuring flows require profile paths.argyll (the spotread executable)")
            persistent_meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=ccmx))
            measure = make_persistent_spotread_meter(presenter=presenter, persistent=persistent_meter)
        else:
            # --legacy-meter: re-spawn + re-calibrate spotread per read (the old per-spawn path).
            measure = make_spotread_meter(presenter=presenter, spotread=argyll, port=port,
                                          output_dir=ctx.root / "measurements" / "probe",
                                          ccmx_or_ccss=ccmx)
    # Characterize tuning: start from the defaults, override only the --char-* flags that were set.
    char_overrides = {
        "noise_levels": (tuple(args.char_noise_levels) if args.char_noise_levels else None),
        "noise_reads": args.char_noise_reads,
        "black_reads": args.char_black_reads,
        "primary_reads": args.char_primary_reads,
        "creep_reads": args.char_creep_reads,
        "settle_observe_reads": args.char_settle_reads,
        "warmup_max_minutes": args.char_warmup_max_minutes,
        "warmup_stable_de_per_min": args.char_warmup_stable,
        "thermal_max_blocks": args.char_thermal_max_blocks,
        "thermal_load_reads_per_block": args.char_thermal_load_reads,
        "thermal_ref_reads": args.char_thermal_ref_reads,
        "thermal_window_blocks": args.char_thermal_window,
        "thermal_k_start": args.char_thermal_k_start,
        "eotf_reads": args.char_eotf_reads,
        "eotf_levels": (tuple(args.char_eotf_levels) if args.char_eotf_levels else None),
    }
    char_overrides = {k: v for k, v in char_overrides.items() if v is not None}
    characterize_config = replace(CharacterizeConfig(), **char_overrides) if char_overrides else None

    def _stall_kill() -> None:
        # The watchdog tripped on a wedge (a read/present blocked in a syscall the checkpoint
        # can't reach). Force the blocking resources down so the main thread returns and aborts
        # at its checkpoint. Best-effort + idempotent (the finally below closes them again).
        if persistent_meter is not None:
            try:
                persistent_meter.close()   # escalates terminate→kill on the spotread child
            except Exception:  # noqa: BLE001
                pass
        if presenter is not None:
            try:
                # A SocketPresenter.close() only drops OUR socket and leaves the persistent daemon +
                # its fullscreen window running on purpose — so for a WEDGED present (main thread
                # blocked in recv) that is a no-op against the actual blocker. The watchdog only fires
                # on a terminal abort, so tell the daemon to quit: dogegen closes, the daemon drops the
                # connection, and the main thread's recv returns → it unblocks and aborts at its
                # checkpoint. A spawned (non-socket) DogegenPresenter has no daemon, so just close it.
                shutdown_daemon = getattr(presenter, "shutdown_daemon", None)
                if callable(shutdown_daemon):
                    shutdown_daemon()
                else:
                    presenter.close()      # kills a spawned window
            except Exception:  # noqa: BLE001
                pass

    def _pause_park(_ctrl: Mapping[str, Any]) -> None:
        if presenter is None:
            return
        max_cv = (1 << bit_depth) - 1
        mid = int(round(0.5 * max_cv))
        patch = MeasurePatch(label="pause-neutral", rgb=(mid, mid, mid),
                             signal=(0.5, 0.5, 0.5), role="neutral_ref",
                             bit_depth=bit_depth)
        presenter.show(patch)

    # Pass the RAW CLI mode (Calibration re-runs the same deterministic reconciliation and,
    # seeing the raw request, surfaces a mis-issued flagless resume that asked for SDR on an HDR
    # run instead of silently switching). bit_depth is the already-RESOLVED value so the
    # orchestrator's patch encoding matches the meter/dogegen stack built above AND gets persisted
    # as the run's depth — main()'s fresh default (8-bit SDR) and the orchestrator's panel-depth
    # default differ, so passing the resolved value is what keeps the live path consistent.
    calib = Calibration(ctx=ctx, profile=profile, monitor=args.monitor, mode=args.mode,
                        controller=controller, measure=measure, adjudicator=adjudicator,
                        bit_depth=bit_depth, force=args.force, patch_sizes=patch_sizes,
                        characterize_config=characterize_config, decision_overrides=overrides,
                        adaptive_planning=args.adaptive_planning,
                        stall_kill_hook=_stall_kill, pause_handler=_pause_park,
                        enable_watchdog=True, checkin_interval_s=args.checkin_interval,
                        require_hardware_readiness=True,
                        neutral_min_reads=args.neutral_min_reads,
                        dark_min_reads=args.dark_min_reads,
                        dark_floor_max_nits=args.dark_floor_max_nits,
                        neutral_chroma_span=args.neutral_chroma_span,
                        neutral_floor_min_nits=args.neutral_floor_min_nits)
    result = None
    paused = False
    try:
        try:
            result = calib.run(args.flow)
        except AdjudicationRequired as req:
            paused = True  # daemon must survive for the resuming invocation
            print(json.dumps({"status": "adjudication_required", "request": req.request.as_dict(),
                              "run": str(ctx.root)}, indent=2))
            return 10
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.status == "completed" else 1
    finally:
        # Tear down the presenter so dogegen never orphans. On a PAUSE we only drop our socket
        # (the persistent daemon + its fullscreen window must survive for resume); on a TERMINAL
        # exit (completed/failed, not paused) we stop the daemon too — unless the operator opted
        # to keep it for reuse. A spawned (non-persistent) DogegenPresenter always quits on close.
        if presenter is not None:
            try:
                terminal = not paused
                if (terminal and not args.keep_dogegen_server
                        and hasattr(presenter, "shutdown_daemon")):
                    presenter.shutdown_daemon()
                else:
                    presenter.close()
            except Exception:  # noqa: BLE001
                pass
        # The interactive spotread is a child of THIS process — it can't outlive the CLI exit
        # (even a pause), so always close it; a resume re-opens (one calibration per invocation,
        # still far cheaper than per-patch).
        if persistent_meter is not None:
            try:
                persistent_meter.close()
            except Exception:  # noqa: BLE001
                pass
        # Rollback guard: a clean run reaches a 'completed' (applied), 'reverted', or
        # 'revert_unavailable' terminal state — all of which _finish already settled (commit,
        # snapshot restore, in-place cube restore, or an honest surface of the manual backup
        # when the in-place MHC tweak can't be undone over the pipe). Anything else on a
        # non-paused exit — an abort or an unexpected exception — means we may have left a
        # half-applied profile, so roll DesktopLUT back to the user's pre-run snapshot.
        if not paused:
            handled = result is not None and getattr(result, "status", None) in (
                "completed", "reverted", "revert_unavailable")
            if not handled:
                try:
                    controller.exit_calibration(restore_snapshot=True)
                    print(json.dumps({"status": "rolled_back",
                                      "reason": "run did not complete; restored pre-run setup",
                                      "run": str(ctx.root)}, indent=2))
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
