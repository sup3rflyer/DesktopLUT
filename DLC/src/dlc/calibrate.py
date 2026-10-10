"""The scripted calibration orchestrator (v2-design-notes §1,3,4,5,7,11; item 5).

The v2 pivot: a **deterministic scripted core owns ALL the mechanics** (display
mapping, patch sets, measurement sequencing, the loops, integrity gates, LUT
generation) and a **thin LLM sits only at the seams** — it never tails a stream,
it judges *digests* at boundaries. This module is that core.

A run is a **named flow** (``full`` / ``3dlut-only`` / ``mhc-only`` / …) over a run
MODE (SDR or HDR — the mode picks the target/transfer/refine stages; there is no
separate ``hdr`` flow) expressed as an ordered list of stage methods. The pipeline is
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
  run: it takes **benign** recommendations without pausing (a clean run never pauses)
  but **escalates safety-critical seams to the LLM** — exactly when the core's own
  recommendation turns non-benign (``abort``/``revert``/``retry``/…) or the digest
  flags a severe/critical state. Every benign default it takes is emitted as a
  **vetoable judgment packet** on the digest (seam ``status="auto_accepted"`` with the
  full request + the veto lever), so the observing LLM still sees — and can override —
  every judgment (Task #1, resolved fable Phase 8). This is the answer to "the
  overnight run had no LLM at the seams": auto-mode is *safe* only if recommendations
  are conservative, but supervised-mode is *judged* at the boundaries that matter.

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
import math
import os
import re
import shutil
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np

from .adjudication import (
    SEAM_BACKUP,
    SEAM_BRIGHTNESS,
    SEAM_CHARACTERIZE,
    SEAM_CORRECTION,
    SEAM_FOUNDATION,
    SEAM_HARDWARE_READY,
    SEAM_MEASURE,
    SEAM_LINK_DEPTH,
    SEAM_MONITOR_MAP,
    SEAM_OPTIMIZE,
    SEAM_PIPE,
    SEAM_PLAN,
    SEAM_PLANNING,
    SEAM_PROBE_MATCH,
    SEAM_SPD,
    SEAM_STACK,
    SEAM_THERMAL_STATE,
    SEAM_VERIFY,
    AdjudicationRequest,
    AdjudicationRequired,
    Adjudicator,
    AutoAdjudicator,
    Decision,
    MappingAdjudicator,
    SupervisedAdjudicator,
)
from . import calibration_profile as cp
from . import checkin
from . import refine_convergence
from .characterize import CharacterizeConfig, run_characterization
from .controller import CalibrationController, normalize_mode
from .desktoplut_client import contract_version_mismatch
from .correction_store import MODE_RECORDED, CorrectionRecord, CorrectionStore
from . import gamut
from .dip import DipStore, DisplayInstrumentProfile
from .engine.patches import Transfer
from .events import Ev, EventWriter, RunLog
from .keep_awake import keep_awake
from .liveness import Liveness, MeterDown, RunCancelled, RunStalled
from .measure_loop import (
    IncrementalMeasureSession,
    MeasureFn,
    MeasureLoopConfig,
    MeasurePatch,
    MeasureLoopResult,
    Reading,
    run_measure_loop,
)
from .decisions import hdr_metric_thresholds
from . import metrics as metrics_mod
from .metrics import (delta_e2000, metrics_scored_payload, percentile, practical_summary,
                      score_samples, score_samples_hdr, summarize_metrics, xyz_to_lab)
from .mhc import SRGB_PRIMARIES, parse_ti3, white_xyz
from . import hook_routing
from . import neutral_audit
from . import stack_registry
from . import thermal_align
from . import verify_holdout
from . import verify_only
from . import viewing_thermal
from .optimize import (DegenerateMeasurements, OptimizeConfig, ProbeFn, SDR_CORRECTION_CAP,
                       optimize_cube)
from . import patch_evidence
from .patch_sets import (
    PatchSizes,
    # the bookend generator is shared with _bookend_drift_qc, which must expect EXACTLY
    # the sweep the builders prepend/append (start-vs-end drift witness)
    _saturation_sweep_bookend,
    build_grayscale_wb_set,
    build_neutral_set,
    build_ramp_set,
    build_refine_verify_set,
    build_verify_set,
    build_volumetric_set,
    flow_patch_counts,
    held_out_draws_apply,
    insert_held_out_draws,
    outside_in_indices,
)
from .paths import atomic_write_text, runs_dir
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

_D65_XY = (0.3127, 0.3290)          # the standard-source white the MHC matrix maps the panel to
_BOOKEND_DRIFT_ANOMALY_DE = 1.0  # one JND-ish start-vs-end drift across repeated skeleton bookends

# The adjudication layer — the seam ids (SEAM_*), the request/decision forms, the DESIGN LAW
# governing what may be decided without a judge, and the three adjudicators — lives in
# :mod:`dlc.adjudication` (extracted verbatim, fable Phase 7b). Every name is re-imported
# above and re-exported via ``__all__`` so ``from dlc.calibrate import Decision, …`` keeps
# working for every existing caller/test.


# ---------------------------------------------------------------------------
# Stage / run results
# ---------------------------------------------------------------------------

# PatchSizes — the patch-set size/sequence knobs — moved to dlc/patch_sets.py
# (fable Phase 7b) alongside the builders it parameterizes; re-imported above.


@dataclass
class StageOutcome:
    stage: str
    status: str                    # done | escalated | aborted
    digest: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)   # JSON-friendly handoff
    artifacts: list[str] = field(default_factory=list)
    # True when this outcome came back from the run-record memo instead of fresh work (set by
    # Calibration._stage on replay; NOT persisted — a record is by definition not-replayed until
    # it is read back). Lets post-stage telemetry (e.g. the intermediate _score_stage) run once
    # per fresh execution instead of re-emitting on every resume.
    replayed: bool = False

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


class StageError(CalibrationAborted):
    """A stage REFUSED on a provably-mechanical invariant (e.g. the identity MHC association
    did not land, a GUI layer is still ON after enter-neutral). A :class:`CalibrationAborted`
    so the run rolls back through the normal path; the stage is recorded ``aborted`` with a
    clear ``message`` + the evidence that tripped it — never a silent continue."""

    def __init__(self, stage: str, message: str, **digest: Any) -> None:
        super().__init__(StageOutcome(stage, "aborted", digest={"message": message, **digest}))


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


def resolve_content_mode(calib: Mapping[str, Any], requested: Optional[str], display_mode: str
                         ) -> tuple[str, Optional[dict[str, Any]], Optional[str]]:
    """The run's CONTENT mode (verify-only ``--content-mode``) from the persisted ``calib['content_mode']``
    and the request — ONE rule for the orchestrator and ``main()`` (which builds dogegen before it), so the
    two can never present one signal and score another. Returns ``(effective, conflict | None,
    value_to_persist)``. No request: the persisted value (else the display mode). A request on a run with
    memoised stages that differs from what they ran with is a conflict (the persisted value stays and the
    run refuses at resume-args); otherwise the request wins. Asking for the display's own mode is the
    default — persisted as ``None``, never a conflict with an unset record."""
    disp = normalize_mode(display_mode)
    stored = calib.get("content_mode")
    stored_eff = normalize_mode(stored) if stored else disp
    if requested is None:
        return stored_eff, None, stored
    req = normalize_mode(requested)
    if calib.get("stages") and req != stored_eff:
        return stored_eff, {"field": "content_mode", "requested": req, "persisted": stored}, stored
    return req, None, (None if req == disp else req)


# The verify's thermal-state seam (--thermal-state viewing) and the stage whose memo locks its knobs.
THERMAL_STATE_STAGE = "measure:verify"
THERMAL_STATE_DECISION_KEY = f"{THERMAL_STATE_STAGE}:thermal-state"
# The MHC closed-loop refine stages that run in the viewing state under --thermal-state viewing (owner
# decision 2026-10-09: the thermal offset goes into the PROFILE through the MHC refine).
REFINE_THERMAL_STAGES = ("refine-mhc-cube", "refine-mhc-grayscale")


def resolve_thermal_knobs(calib: dict[str, Any], *, thermal_state: Optional[str],
                          viewing_load_nits: Optional[float], viewing_start_nits: Optional[float],
                          viewing_hold_budget_min: Optional[float] = None
                          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the thermal-state knobs to the run record ``calib`` (in place); returns ``(conflicts,
    changes)``. ONE rule, like :func:`resolve_content_mode`:

    * not given → the persisted value (a flagless resume keeps it);
    * ``--thermal-state verify`` IS the default — persisted as unset, so an explicit ``verify`` on a run
      started without the flag is identical to omitting it (never a resume conflict);
    * the knobs shape only the VERIFY measure, so they may change until :data:`THERMAL_STATE_STAGE` is
      memoised (the LLM corrects an assumed start at the thermal-state seam); every change on a run
      that already has stages or a stored value is returned in ``changes`` (recorded, never silent);
    * once the verify is measured, a different value is a conflict (the run refuses at resume-args);
    * ``--thermal-state`` itself also locks once an MHC refine (:data:`REFINE_THERMAL_STAGES`) is memoised
      done — its white/greys were refined in that state (a conflict with a ``reason``)."""
    stages = calib.get("stages") or {}
    locked = THERMAL_STATE_STAGE in stages
    # The STATE itself also locks once an MHC refine is memoised (done): its white/greys were refined in the
    # state it recorded, so switching viewing <-> verify after it would make the verify describe another state
    # than the profile's white (a viewing refine verified "verify", or the reverse) — refused, never silent.
    refined = [k for k in REFINE_THERMAL_STAGES if (stages.get(k) or {}).get("status") == "done"]
    requested: list[tuple[str, Any]] = []
    if thermal_state is not None:
        st = str(thermal_state).strip().lower()
        requested.append(("thermal_state", None if st == viewing_thermal.DEFAULT_THERMAL_STATE else st))
    if viewing_load_nits is not None:
        requested.append(("viewing_load_nits", float(viewing_load_nits)))
    if viewing_start_nits is not None:
        requested.append(("viewing_start_nits", float(viewing_start_nits)))
    if viewing_hold_budget_min is not None:
        requested.append(("viewing_hold_budget_min", float(viewing_hold_budget_min)))
    conflicts: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    def shown(field: str, val: Any) -> Any:
        return (val or viewing_thermal.DEFAULT_THERMAL_STATE) if field == "thermal_state" else val

    for field, want in requested:
        stored = calib.get(field)
        if stored == want:
            continue
        if locked:
            conflicts.append({"field": field, "requested": shown(field, want), "persisted": shown(field, stored)})
            continue
        if field == "thermal_state" and refined:
            conflicts.append({"field": field, "requested": shown(field, want), "persisted": shown(field, stored),
                              "reason": (f"--thermal-state is locked once the MHC refine is memoised "
                                         f"({', '.join(refined)} refined the MHC white/greys in the "
                                         f"{shown(field, stored)!r} state; the verify must describe the same "
                                         "request)")})
            continue
        if stored is not None or stages:
            changes.append({"field": field, "from": shown(field, stored), "to": shown(field, want),
                            "at": datetime.now().isoformat(timespec="seconds"), "stages_done": sorted(stages)})
        if want is None:
            calib.pop(field, None)
        else:
            calib[field] = want
        if field == "viewing_start_nits":
            calib.pop("viewing_start_used_by", None)   # a NEW start answers the next viewing seam
            calib.pop("viewing_start_spent", None)
    return conflicts, changes


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
        thermal_align: str = "auto",
        hook_routing_policy: str = "auto",
        mhc_top_hold: bool = True,
        white_band: Optional[tuple[float, float]] = None,
        source_run: Optional[Path] = None,
        link_probe: Optional[Callable[[], dict[str, Any]]] = None,
        verify_cube: Optional[Path] = None,
        verify_patches_from: Optional[Path] = None,
        verify_patches_file: Optional[Path] = None,
        verify_patches_order: Optional[str] = None,
        content_distribution: Optional[Sequence[str]] = None,
        score_black_floor_nits: Optional[float] = None,
        preheat: Optional[str] = None,
        thermal_state: Optional[str] = None,
        viewing_load_nits: Optional[float] = None,
        viewing_start_nits: Optional[float] = None,
        viewing_hold_budget_min: Optional[float] = None,
        present_stall: Optional[str] = None,
        refine_cube: Optional[str] = None,
        content_mode: Optional[str] = None,
        keep_layers: Optional[Sequence[str]] = None,
        sdr_white_probe: Optional[Callable[[Optional[Mapping[str, Any]]], dict[str, Any]]] = None,
    ) -> None:
        self.ctx = ctx
        self.profile = profile
        self.monitor = monitor
        # Local DisplayConfig link-format probe (dlc.link_format.probe_link_formats) — the fallback
        # when the DesktopLUT build's query_monitors predates link_bpc. The live CLI wires it; the
        # default None keeps sim/tests off the host's real displays.
        self.link_probe = link_probe
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
        # Thermal-state alignment policy for the raw / post-MHC datasets (plan item 3):
        # 'auto' = evidence every stage, SEAM when the reference track's drift is significant;
        # 'end'/'start'/'mid' = pre-decided (applied, reported, no pause); 'none' = evidence only.
        self.thermal_align = (thermal_align or "auto").lower()
        # DWM-hook LUT routing self-check policy (cube flows; 2026-09-03 incident — the hook
        # order-matched twin panels and a whole 3dlut-only run measured an uncorrected display):
        # 'auto' = optical proof only when the hook's routing report is ambiguous/unconfirmed/
        # absent; 'always' = prove it every run; 'never' = operator pre-decided (evidence only).
        self.hook_routing_policy = (hook_routing_policy or "auto").lower()
        # MHC top hold (owner policy 2026-09-23 — "clamp to confirmed gamut edges" replaces "fade to
        # identity"): the HDR base 1D cube holds each channel at its own neutral-cap index, at build
        # and after every refine round, so greys above the calibrated top stay D65 at the cap. The
        # 3D-LUT twin is OptimizeConfig.top_hold. False => the legacy shared-ceiling behaviour.
        self.mhc_top_hold = bool(mhc_top_hold)
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
        # True once stage_enter_neutral associated the identity MHC profile IN THIS PROCESS
        # (a resume replaying the record leaves it False — see stage_hardware_readiness).
        self._neutral_associated_live = False

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
        # Run-level SDR white-band override (--white-band) and the refine-mhc flow's source run
        # (--source-run): persisted in the run record so a flagless resume keeps them. A resume
        # that asks for a DIFFERENT value than the memoised stages were run with is refused in
        # run() (never silently re-targeted under already-recorded stages).
        self._arg_conflicts: list[dict[str, Any]] = []
        has_memo = bool(self.calib.get("stages"))
        requested = (
            ("white_band_override",
             list(cp.parse_white_nits_band(white_band)) if white_band is not None else None),
            ("source_run", str(Path(source_run).resolve()) if source_run is not None else None),
            # verify-only: the candidate 3D LUT to install for the run (--verify-cube) and the
            # recorded run whose EXACT verify set is re-measured (--verify-patches-from).
            ("verify_cube", str(Path(verify_cube).resolve()) if verify_cube is not None else None),
            ("verify_patches_from",
             str(Path(verify_patches_from).resolve()) if verify_patches_from is not None else None),
            # verify-only: a verify list from a FILE (--verify-patches-file — e.g. a content-sampled set
            # with per-patch content weights). Its codes + fingerprint are memoised by the
            # verify-patches-file stage, so a resume measures the identical list.
            ("verify_patches_file",
             str(Path(verify_patches_file).resolve()) if verify_patches_file is not None else None),
            # ... measured in the FILE's order unless a sort is asked for (--verify-patches-order).
            ("verify_patches_order",
             str(verify_patches_order).strip().lower() if verify_patches_order is not None else None),
            # Any flow: the content distribution(s) the verify's content-weighted practical score is
            # computed against (--content-distribution PATH[#VARIANT]; else the profile's key). Evidence only.
            ("content_distribution",
             [_content_spec_resolved(c) for c in content_distribution] if content_distribution else None),
            # HDR verify: the display floor (nit) of the content-weighted score's BLACK-AWARE variant
            # (--score-black-floor-nits; else a raw stage's native near-black floor, else the DIP's native
            # black). Evidence only (dlc.black_aware).
            ("score_black_floor_nits", _black_floor_arg(score_black_floor_nits)),
            # refine-mhc: which 3D LUT the re-refined MHC keeps — the source run's build (default) or
            # the cube INSTALLED now (a later 3dlut-only run over the same MHC lineage).
            ("refine_cube", str(refine_cube).strip().lower() if refine_cube is not None else None),
            # (--content-mode is resolved after the display mode, below: resolve_content_mode)
            # verify-only: viewing layers left as the user has them for the run (--keep-layers).
            ("keep_layers", sorted({str(n).strip().lower() for n in keep_layers}) if keep_layers is not None
             else None))
        # (--thermal-state / --viewing-load-nits / --viewing-start-nits: resolved below by
        # resolve_thermal_knobs — they shape only the VERIFY measure, so they stay changeable until it is
        # memoised, and an explicit `verify` is the default, never a conflict with an unset record)
        if thermal_state is not None and str(thermal_state).strip().lower() not in viewing_thermal.THERMAL_STATES:
            raise ValueError(f"thermal_state must be one of {viewing_thermal.THERMAL_STATES}, got {thermal_state!r}")
        if viewing_load_nits is not None:
            v = float(viewing_load_nits)
            # > 0: a zero target can never converge (its band is [0, 0]). <= the recorded verify band's
            # nit-equivalent: a "viewing" target at/above the meter's own verify load is not a viewing state,
            # and a typo (1000) would soak a ~full-field bright static for up to the cap (FALD probe hygiene).
            if not (math.isfinite(v) and 0.0 < v <= viewing_thermal.VIEWING_LOAD_MAX_NITS):
                raise ValueError(
                    f"viewing_load_nits must be a nit-equivalent in (0, {viewing_thermal.VIEWING_LOAD_MAX_NITS}] "
                    f"(the ceiling is the recorded verify band's nit-equivalent — a hotter target is not a "
                    f"viewing state), got {viewing_load_nits!r}")
        if viewing_start_nits is not None and not (float(viewing_start_nits) >= 0.0
                                                   and math.isfinite(float(viewing_start_nits))):
            raise ValueError(f"viewing_start_nits must be a finite nit-equivalent >= 0, got {viewing_start_nits!r}")
        if viewing_hold_budget_min is not None and not (math.isfinite(float(viewing_hold_budget_min))
                                                        and 0.0 <= float(viewing_hold_budget_min)
                                                        <= viewing_thermal.HOLD_BUDGET_MAX_MIN):
            raise ValueError(f"viewing_hold_budget_min must be in [0, {viewing_thermal.HOLD_BUDGET_MAX_MIN:g}] "
                             f"minutes, got {viewing_hold_budget_min!r}")
        if verify_patches_order is not None and \
                str(verify_patches_order).strip().lower() not in verify_only.PATCH_FILE_ORDERS:
            raise ValueError(f"verify_patches_order must be one of {verify_only.PATCH_FILE_ORDERS}, "
                             f"got {verify_patches_order!r}")
        if keep_layers is not None:
            unknown = sorted({str(n).strip().lower() for n in keep_layers} - set(CalibrationController.LAYER_NAMES))
            if unknown:
                raise ValueError(f"keep_layers: unknown layer(s) {unknown}; known: {CalibrationController.LAYER_NAMES}")
        if self.calib.get("refine_cube") not in (None, "source", "installed") or \
                (refine_cube is not None and str(refine_cube).strip().lower() not in ("source", "installed")):
            raise ValueError(f"refine_cube must be 'source' or 'installed', got {refine_cube!r}")
        for key, val in requested:
            if val is None:
                continue
            stored = self.calib.get(key)
            if has_memo and stored != val:
                self._arg_conflicts.append({"field": key, "requested": val, "persisted": stored})
            else:
                self.calib[key] = val
        # The thermal STATE knobs (--thermal-state / --viewing-load-nits / --viewing-start-nits /
        # --viewing-hold-budget-min): they shape the MHC refine + the VERIFY measure (their thermal-state seams,
        # preconditions and the refine's hold), so a resume may change them until `measure:verify` is memoised
        # — the LLM corrects an assumed start / the dwell budget at a seam — and each change is recorded
        # (thermal_state_changes) and drops the memoised seam decisions (of stages not yet run) whose numbers it
        # changed. After the verify is measured a different value is refused (resume-args).
        knob_conflicts, knob_changes = resolve_thermal_knobs(
            self.calib, thermal_state=thermal_state, viewing_load_nits=viewing_load_nits,
            viewing_start_nits=viewing_start_nits, viewing_hold_budget_min=viewing_hold_budget_min)
        self._arg_conflicts.extend(knob_conflicts)
        if knob_changes:
            self.calib.setdefault("thermal_state_changes", []).extend(knob_changes)
            self._forget_decision(THERMAL_STATE_DECISION_KEY, overrides=False)
            # ...and the MHC refine's thermal-state (+ miss) seam decisions, while that refine is not done —
            # still to run OR aborted (an aborted refine re-runs on resume; its recorded "abort" must not
            # replay over the numbers — start, target, dwell budget — that just changed). A done refine keeps
            # its record.
            for stage in REFINE_THERMAL_STAGES:
                if ((self.calib.get("stages") or {}).get(stage) or {}).get("status") != "done":
                    self._forget_decision(f"{stage}:thermal-state", overrides=False)
                    self._forget_decision(f"{stage}:thermal-miss", overrides=False)
        # Thermal preheat policy (--preheat auto|always|never → MeasureLoopConfig.preheat). None =
        # not asked: the loop config's own policy (auto) — today's behaviour. Persisted so a flagless
        # resume keeps it; unlike the args above it only shapes measure stages NOT yet run (each
        # measure digest records the policy it ran under), so an explicit new value on resume is
        # honoured — visibly (preheat_changes), never silently.
        self._preheat_change: Optional[dict[str, Any]] = None
        if preheat is not None:
            policy = str(preheat).strip().lower()
            if policy not in verify_only.PREHEAT_POLICIES:
                raise ValueError(f"preheat must be one of {verify_only.PREHEAT_POLICIES}, got {preheat!r}")
            stored = self.calib.get("preheat")
            if stored is not None and stored != policy:
                self._preheat_change = {"from": stored, "to": policy,
                                        "at": datetime.now().isoformat(timespec="seconds"),
                                        "stages_done": sorted(self.calib.get("stages") or {})}
                self.calib.setdefault("preheat_changes", []).append(self._preheat_change)
            self.calib["preheat"] = policy
        # --thermal-state viewing: mechanical consistency of the levers it rides on (refused, never
        # silently re-interpreted). The viewing precondition IS the preheat machinery, so --preheat never
        # contradicts it; and a viewing verify keeps a patch file's designed (load-balanced) order.
        if self._thermal_state() == "viewing":
            if self.calib.get("preheat") == "never":
                raise ValueError("--thermal-state viewing needs the preheat machinery (its viewing-load "
                                 "precondition); --preheat never contradicts it")
            if self.calib.get("verify_patches_order") not in (None, "file"):
                raise ValueError("--thermal-state viewing keeps a --verify-patches-file in its designed order "
                                 "(its balanced blocks hold the viewing band); drop --verify-patches-order "
                                 f"{self.calib.get('verify_patches_order')!r}")
        # The stuck-frame (present-stall) detector, per run (--present-stall). It assumes distinct
        # commanded colours can only read identical XYZ on a frozen frame; a deliberate drive sweep
        # whose minor channels the MHC clips to zero (2026-10-02 PA HDR minor-channel brackets) reads
        # identical XYZ legitimately. "off" is an LLM decision for such a run: persisted (a flagless
        # resume keeps it), every change recorded, every measure digest names it.
        if present_stall is not None:
            sw = str(present_stall).strip().lower()
            if sw not in ("on", "off"):
                raise ValueError(f"present_stall must be 'on' or 'off', got {present_stall!r}")
            stored = self.calib.get("present_stall")
            if stored is not None and stored != sw:
                self.calib.setdefault("present_stall_changes", []).append(
                    {"from": stored, "to": sw, "at": datetime.now().isoformat(timespec="seconds"),
                     "stages_done": sorted(self.calib.get("stages") or {})})
            self.calib["present_stall"] = sw
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
        # Audited (fable Phase 7a): the two fallbacks are INTENTIONALLY different, because bit
        # depth is a property of the presenter TRANSPORT, not the panel — the CLI picks it where
        # the presenter is built (composited 8-bit is the 3D-LUT-safe dogegen SDR default) and
        # passes it in; an in-process caller presents through its injected measure fn at the
        # panel's own depth. The persisted run spec makes the choice sticky either way.
        self.bit_depth = _eff_bd if _eff_bd is not None else self.display.panel.bit_depth
        # The CONTENT mode (verify-only --content-mode): what the patches ARE — target, transfer, patch
        # codes, dogegen mode, scoring. Defaults to the display mode (self.mode, which keys every pipe /
        # stack / layer / correction / DIP lookup); persisted in calib so a flagless resume keeps it.
        self.content_mode, cm_conflict, cm_persist = resolve_content_mode(self.calib, content_mode, self.mode)
        if cm_conflict:
            self._arg_conflicts.append(cm_conflict)
        elif content_mode is not None:
            if cm_persist is None:
                self.calib.pop("content_mode", None)
            else:
                self.calib["content_mode"] = cm_persist
        # Live DisplayConfig SDR-white-level reader (dlc.sdr_in_hdr.probe_sdr_white) for SDR content on an
        # HDR display; the CLI wires it, None keeps sim/tests off the host's displays.
        self.sdr_white_probe = sdr_white_probe

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
        # NO-DARK-WINDOW rule (owner, 2026-07-05): an LLM-adjudicated run must never go
        # more than checkin.NO_DARK_WINDOW_CEILING_S without a check-in while the spine
        # executes. A disabled (0) or longer interval is clamped here for any adjudicator
        # but the sim/CI AutoAdjudicator; the wall-clock backstops in the measure loop /
        # probe batch / characterize deliver the cadence inside long phases.
        if not isinstance(adjudicator, AutoAdjudicator) and not (
                0.0 < self._checkin_interval_s <= checkin.NO_DARK_WINDOW_CEILING_S):
            requested = self._checkin_interval_s
            self._checkin_interval_s = checkin.NO_DARK_WINDOW_CEILING_S
            self.ctx.log(
                f"check-in interval {requested:g}s "
                f"{'(disabled)' if requested <= 0 else ''} exceeds the no-dark-window rule "
                f"for an LLM-adjudicated run — clamped to {self._checkin_interval_s:g}s "
                "(only --auto sim/CI runs may disable check-ins)")
        # The ONE §12 evidence window shared by every check-in emitter (this orchestrator AND the
        # measure loop — handed to it in _measure_set / the incremental session), so any packet
        # resets the cadence for both and no two packets land seconds apart. The
        # _last_checkin_* properties read/write it (clock, tally snapshot, events byte offset).
        self._checkin_window = checkin.CheckinWindow()
        # Evidence starts at THIS process: a resumed run appends to the previous processes'
        # events.jsonl, and none of that history is "since the last check-in". (The clock stays
        # unanchored — the first checkpoint anchors it, no ping at second 0.)
        self._checkin_window.pos = checkin.events_size(self)
        self._checkin_window.tally = dict(self.runlog.tally)
        self._run_started_monotonic: Optional[float] = None
        # Latest live metrics, snapshotted as they happen, so a check-in carries them without
        # re-deriving from artifacts: the most recent intermediate score + the last optimizer iter.
        self._last_scored: dict[str, Any] = {}
        self._last_optimizer: dict[str, Any] = {}
        self._last_refine: dict[str, Any] = {}
        self._last_bookend_drift: dict[str, Any] = {}
        self._last_verify_reachable: Any = None   # the gamut the live verify scored against (re-scores reuse it)

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

    # The friendly stepper labels for every stage key the flows can walk. Keys must match the
    # ``_stage(key, ...)`` / ``set_phase(key)`` strings exactly (that's what the dashboard sees as
    # the live stage). ``long`` marks a stage the operator should expect to wait on.
    _STAGE_LABELS = {
        "preflight": ("Preflight", False),
        "resolve-target": ("Resolve target", False),
        "whitepoint": ("White point", False),
        "probe-match": ("Probe match (CCMX)", True),
        "clear-native": ("Clear to native", False),
        "enter-neutral": ("Enter neutral", False),
        "characterize": ("Characterize panel", True),
        "hardware-readiness": ("Hardware readiness", False),
        "brightness": ("Brightness", False),
        "measure:raw": ("Measure · raw panel", True),
        "build-install-mhc": ("Build + install MHC", False),
        "refine-mhc-cube": ("Refine MHC (HDR cube)", True),
        "refine-mhc-grayscale": ("Refine MHC grayscale", True),
        "seed-from-run": ("Seed from source run", False),
        "install-mhc": ("Reinstall MHC", False),
        "reapply-3dlut": ("Re-apply 3D LUT", False),
        "verify-source": ("Load verify set (source run)", False),
        "verify-patches-file": ("Load verify set (file)", False),
        "install-candidate": ("Install candidate 3D LUT", False),
        "adaptive-planning": ("Adaptive planning", False),
        "measure:post-mhc": ("Measure · post-MHC", True),
        "build-install-3dlut": ("Build + install 3D LUT", True),
        "grayscale-wb": ("Grayscale touch-up", True),
        "measure:verify": ("Measure · verify", True),
        "verify": ("Verify + report", False),
    }

    def _planned_stages(self) -> list[dict[str, Any]]:
        """The chosen flow's ordered pipeline (key + friendly label + long-stage hint) for the
        dashboard stepper, from the declarative ``_FLOW_STAGE_SEQUENCES`` table (defined next
        to ``FLOWS``). Empty until the flow is resolved. The HDR/SDR refine fork mirrors the
        ``_flow_*`` methods (``self.mode``; normalize_mode pins it to SDR/HDR)."""
        flow = self.calib.get("flow")
        refine = "refine-mhc-cube" if self.mode == "HDR" else "refine-mhc-grayscale"
        keys = [refine if k == _REFINE_FORK else k
                for k in _FLOW_STAGE_SEQUENCES.get(flow or "", ())]
        # adaptive-planning only announces itself when the opt-in seam is ON (stage_adaptive_planning
        # returns before set_phase otherwise) — don't show the stepper a stage the run never enters.
        if not self.adaptive_planning:
            keys = [k for k in keys if k != "adaptive-planning"]
        # hardware-readiness likewise short-circuits (no phase announced) unless the gate is
        # required — main() always requires it live, so the live stepper is unchanged.
        if not self.require_hardware_readiness:
            keys = [k for k in keys if k != "hardware-readiness"]
        # verify-only's optional stages exist only with their flags (--verify-patches-from /
        # --verify-cube); without them the flow never announces them.
        if not self.calib.get("verify_patches_from"):
            keys = [k for k in keys if k != "verify-source"]
        if not self.calib.get("verify_patches_file"):
            keys = [k for k in keys if k != "verify-patches-file"]
        if not self.calib.get("verify_cube"):
            keys = [k for k in keys if k != "install-candidate"]
        out: list[dict[str, Any]] = []
        for key in keys:
            label, long = self._STAGE_LABELS.get(key, (key, False))
            out.append({"key": key, "label": label, "long": long})
        return out

    def _header_data(self) -> dict[str, Any]:
        """The dashboard status-bar payload: who/what is being calibrated, against what
        target, with which correction. Gathered defensively — a missing piece (target not
        yet resolved, no correction on file) just omits that key, never blocks."""
        data: dict[str, Any] = {
            "run_id": self.ctx.root.name,
            "display": self.display.name,
            "monitor": self.monitor,
            "mode": self.mode,
            "content_mode": self.content_mode,
            "flow": self.calib.get("flow"),
            "bit_depth": self.bit_depth,
        }
        plan = self._planned_stages()
        if plan:
            data["stage_plan"] = plan   # the dashboard stepper's "stage K of N" pipeline
        if self.target_name:
            data["target"] = self.target_name
            try:
                # Target gamma + luminance — the dashboard's EOTF chart draws the reference
                # curve from these. Defensive: the spec may not resolve yet at first emit.
                spec = self._spec()
                data["gamma"] = spec.gamma
                # SDR: the calibrated white once known (the refine's / the installed MHC's), with
                # the nominal alongside — the EOTF reference must be the curve the stack targets.
                data["luminance"] = (self._hdr_target().peak_nits if spec.is_hdr
                                     else (self._sdr_refined_white_nits(capture=False)
                                           or spec.luminance_nits))
                if not spec.is_hdr:
                    data["luminance_nominal"] = spec.luminance_nits
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
            corr = resolve_correction(self.profile, store, self.display.name, self.mode)
            if corr.file:
                data["ccmx"] = Path(corr.file).name
            # Where the ccmx came from (this mode's store slot / the profile YAML / none) and
            # any cross-mode fallback or legacy-inference warning — the header is the one place
            # every run shows which correction the meter is wired to.
            data["ccmx_source"] = f"store:{corr.mode}" if corr.source == "store" else corr.source
            if corr.warning:
                data["ccmx_warning"] = corr.warning
            rec = store.get(self.display.name, self.mode)
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
            outcome.replayed = True   # post-stage telemetry keys off this (no re-emit on resume)
            self.runlog.stage_done(key, status=outcome.status, replayed=True)
            return outcome
        try:
            outcome = run_fn()
        except MeterDown as exc:
            # The meter is provably down on a read path that has no run-stopper seam of its own
            # (the measure loop catches MeterDown and escalates it at the measure seam instead).
            # Abort cleanly + roll back like a stall — but the record says WHY, with the meter's
            # own error text, instead of an anonymous "no progress" timeout.
            self.runlog.stage_aborted(key, message=str(exc), meter_down=True,
                                      meter_error=exc.error, meter_detail=exc.detail or None)
            raise CalibrationAborted(StageOutcome(
                key, "aborted", digest={"message": str(exc), "meter_down": True,
                                        "meter_error": exc.error,
                                        "meter_detail": exc.detail or None}))
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
        consults the adjudicator (which may pause the run).

        Every decision — override, recorded, or adjudicator-returned — is VALIDATED against
        the seam's declared option vocabulary (fable Phase 8): an off-vocabulary choice
        (``--decide verify:accept=aply``) previously fell through each caller's string
        comparisons and silently behaved as whatever the *unmatched* branch did (at the
        verify gate: APPLY). Now it is surfaced on the spine and treated as un-decided —
        the seam pauses (or the adjudicator re-decides) instead of misfiring."""
        override = self.decision_overrides.get(request.key)
        if override is not None and not self.force:
            if not self._valid_choice(request, override, source="--decide override"):
                override = None   # fall through: recorded decision, then the adjudicator
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
            rec = Decision(d["choice"], d.get("note"), payload=d.get("payload"))
            if self._valid_choice(request, rec, source="recorded decision"):
                return rec
            # else: fall through to the adjudicator (a live run pauses; the record stays
            # until a valid decision overwrites it).
        try:
            decision = self.adjudicator.adjudicate(request)   # may raise AdjudicationRequired
        except AdjudicationRequired:
            # The run is pausing for a human/LLM decision — make the pause visible on the
            # spine (the dashboard shows "waiting at <seam>"; the LLM digest sees the ask).
            self.runlog.seam(request.stage, key=request.key, status="paused",
                             question=request.question, options=list(request.options))
            raise
        if not self._valid_choice(request, decision, source="adjudicator"):
            # A seeded/custom adjudicator answered outside the vocabulary — re-asking it
            # would loop, so pause the run for a real judge instead.
            self.runlog.seam(request.stage, key=request.key, status="paused",
                             question=request.question, options=list(request.options))
            raise AdjudicationRequired(request)
        self._record_decision(request, decision)
        return decision

    def _valid_choice(self, request: AdjudicationRequest, decision: Decision, *,
                      source: str) -> bool:
        """Is ``decision.choice`` in the seam's declared option vocabulary? An off-vocabulary
        choice is surfaced LOUDLY on the spine (log + digest-tier seam event) and rejected —
        the callers' string comparisons would otherwise silently route it down whatever branch
        matches nothing (fable Phase 8, decision-durability audit)."""
        if decision.choice in request.options:
            return True
        self.ctx.log(f"seam {request.key}: invalid {source} choice {decision.choice!r} "
                     f"(valid: {', '.join(request.options)}) — ignored")
        self.runlog.seam(request.stage, key=request.key, status="invalid_decision",
                         choice=decision.choice, valid_options=list(request.options),
                         source=source, question=request.question)
        return False

    def _record_decision(self, request: AdjudicationRequest, decision: Decision,
                         *, overridden: bool = False) -> None:
        rec = {**decision.as_dict(), "seam": request.seam, "question": request.question}
        if overridden:
            rec["overridden"] = True
        self.calib["decisions"][request.key] = rec
        self.ctx.log(f"seam {request.key}: {decision.choice}"
                     + (" (override)" if overridden else "")
                     + (f" ({decision.note})" if decision.note else ""))
        if decision.auto_accepted:
            # DESIGN LAW (Task #1, resolved fable Phase 8 — owner-approved): a benign default
            # taken by CODE (SupervisedAdjudicator) is still a judgment the LLM must see, so it
            # goes on the digest as a FULL judgment packet — everything a paused run would have
            # printed (question/options/recommendation/digest) plus the veto lever — not a bare
            # "decided" line. The observing LLM applies judgment out of band and intervenes only
            # if it disagrees; the run does not pause.
            self.runlog.seam(request.stage, key=request.key, status="auto_accepted",
                             choice=decision.choice, note=decision.note,
                             question=request.question, options=list(request.options),
                             recommendation=request.recommendation, digest=request.digest,
                             veto=(f"--cancel mid-run, or --decide {request.key}=<choice> "
                                   f"--run {self.ctx.root.name} on resume (an override beats "
                                   "this recorded decision without --force)"))
        else:
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
        profile's ``paths.desktoplut_ini`` (absolute, or relative to the cwd; else beside
        ``paths.desktoplut_exe``). Returns None if unset/missing — the backup then falls back
        to the lighter state.get JSON, and the neutral-state audit reports the flags unknown.
        One resolution shared with :mod:`dlc.neutral_audit`."""
        try:
            return neutral_audit.resolve_desktoplut_ini(self.profile.paths or {})
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
        # partial=True marks an ini-only capture made while the pipe was dead — re-attemptable
        # (only via the pre-mutation pipe-heal path in stage_preflight, which re-runs preflight
        # fresh; a complete capture is final).
        if existing and existing.get("captured") and not existing.get("partial"):
            return existing
        record: dict[str, Any] = {"captured": False}
        # The complete settings file — the REAL durable backup — needs only the filesystem,
        # never the pipe, so copy it FIRST, unconditionally (adversarial finding F7a-A3: the
        # first honest-backup guard threw this good half away with the garbage state JSON).
        ini_dest: Optional[str] = None
        ini_src: Optional[str] = None
        try:
            ini = self._resolve_desktoplut_ini()
            if ini is not None:
                dest = self.ctx.root / "desktoplut_settings_backup.ini"
                shutil.copy2(ini, dest)
                ini_dest, ini_src = str(dest), str(ini)
                self.ctx.log(f"backed up full DesktopLUT settings: {ini} → {dest.name}")
            else:
                self.ctx.log("DesktopLUT.ini not found — set paths.desktoplut_ini in the profile "
                             "for a complete settings backup")
        except Exception as exc:  # noqa: BLE001 - backup is best-effort, never blocks the run
            self.ctx.log(f"could not copy the DesktopLUT.ini backup: {exc}")
        # A dead pipe yields state == {"error": ...} — writing THAT as the "backup" would
        # record captured=True over garbage (fable Phase 7a). No state ⇒ no state JSON to
        # back up; the ini half above still counts (the pipe-down seam is the decision surface).
        if not state or ("error" in state and "mhc" not in state and "runtime" not in state):
            record = {"captured": bool(ini_dest), "partial": True,
                      "ini_backup": ini_dest, "ini_source": ini_src, "path": None,
                      "error": ("no DesktopLUT state JSON to back up "
                                f"({(state or {}).get('error', 'empty state')})"
                                + ("" if ini_dest else "; no DesktopLUT.ini configured either"))}
            self.calib["backup"] = record
            self._save()
            return record
        try:
            mhc = (state or {}).get("mhc") or {}
            key = f"{self.monitor}:{self.mode}"
            active_profile = (mhc.get(key) or {}).get("profile_name")
            path = self.ctx.root / "desktoplut_backup.json"
            atomic_write_text(path, json.dumps(state, indent=2))   # the durable pre-run safety net
            record = {"captured": True, "path": str(path),
                      "active_profile": active_profile,
                      "had_mhc": bool(active_profile),
                      "ini_backup": ini_dest, "ini_source": ini_src}
            self.ctx.log(f"backed up user's DesktopLUT state → {path.name}"
                         + (f" (active MHC: {active_profile})" if active_profile else " (no MHC active)"))
        except Exception as exc:  # noqa: BLE001 - backup is best-effort, never blocks the run
            record = {"captured": bool(ini_dest), "partial": bool(ini_dest),
                      "ini_backup": ini_dest, "ini_source": ini_src,
                      "error": f"{type(exc).__name__}: {exc}"}
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
        calibration mode. Best-effort; returns whether the SERVER says it put the setup back
        (its ``restored`` flag — a call that merely returned proves nothing: a DesktopLUT that
        restarted mid-run holds no capture and restores nothing). The outcome is recorded in
        ``calib['snapshot_restore']`` and logged in plain words either way."""
        bak = (self.calib.get("backup") or {}).get("path")
        try:
            # Asked only while DesktopLUT holds an open session / capture (a committed or restarted
            # session has nothing of this run); a stale single slot (a build predating the snapshot
            # store that found an earlier session open on this monitor) is never reported complete.
            report = _common.request_snapshot_restore(
                self.controller, entered=True, monitor=self.monitor,
                stale_tell=_enter_stale_tell(self.calib))
        except Exception as exc:  # noqa: BLE001
            self.calib["snapshot_restore"] = {"why": why, "restored": False, "complete": False,
                                              "error": f"{type(exc).__name__}: {exc}",
                                              "summary": f"calibration.exit failed: {exc}"}
            self._save()
            self.ctx.log(f"restore failed ({why}): {exc}"
                         + (f"; manual backup at {bak}" if bak else ""))
            return False
        self.calib["snapshot_restore"] = {"why": why, **report}
        self._save()
        if report["complete"]:
            self.ctx.log(f"restored the user's previous DesktopLUT setup ({why})")
        else:
            self.ctx.log(f"{report['summary']} ({why})" + (f"; manual backup at {bak}" if bak else ""))
        return report["restored"] is True

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
            runtime = ((state.get("runtime") or {}).get(ck) or {})
            cube = runtime.get("cube_path")
            tweak = runtime.get("grayscale_tweak")
            record: dict[str, Any] = {"captured": True, "cube_path": cube,
                                      "grayscale_tweak": tweak}
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
        if flow == "grayscale-wb":
            # Design B revert (fable Phase 7a): the touch-up was BAKED in its stage, so the C++
            # live session is gone and grayscale_cancel would be a no-op. Re-apply the DLC-owned
            # pre-begin snapshot instead (set_correction_grayscale + apply_mhc) — this restores
            # the user's PRE-EXISTING correction (or clears to identity if there was none) and is
            # robust across a DesktopLUT restart (the snapshot is in dlc_state.json). Also cancel
            # any still-open preview first, for the corner where the stage aborted BEFORE baking.
            try:
                self.controller.grayscale_cancel(self.monitor, self.mode)   # no-op after commit
            except Exception:  # noqa: BLE001 - best-effort; the explicit restore below is authoritative
                pass
            if self._restore_correction_grayscale():
                prior = self.calib.get("grayscale_wb_prior")
                self.ctx.log("reverted: restored the pre-existing Grayscale correction"
                             if prior else "reverted: cleared the Grayscale touch-up to identity")
                return "reverted"
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
        self._restore_other_mode_runtime()

    def _restore_other_mode_runtime(self) -> None:
        """Apply-path guard: re-apply runtime layers of NON-calibrated mode:monitor pairs
        that were live before enter-neutral and are gone now. DesktopLUT builds before
        2026-08-14 cleared BOTH modes' runtime layers on the monitor at calibration.enter,
        and the apply path exits WITHOUT the snapshot restore — the 2026-08-14 HDR run
        permanently dropped the user's SDR cube exactly this way. The server now clears
        only the calibrated pair, so on fixed builds this is a no-op. Best-effort: a
        failure is logged (with the path, so the operator can re-apply by hand), never
        fatal to the commit."""
        prior = self.calib.get("runtime_prior") or {}
        if not prior.get("captured"):
            return
        own = f"{self.monitor}:{self.mode}"
        try:
            current = (self.controller.state() or {}).get("runtime") or {}
        except Exception as exc:  # noqa: BLE001
            self.ctx.log(f"could not re-check runtime layers after commit ({exc}); if another "
                         "mode's 3D LUT is missing, re-apply it from the pre-run map in the "
                         "run record (runtime_prior)")
            return
        for pair, entry in (prior.get("runtime") or {}).items():
            if pair == own or not isinstance(entry, dict):
                continue  # the calibrated pair now owns the fresh build — never touch it
            try:
                mon_str, pair_mode = pair.split(":", 1)
                mon = int(mon_str)
            except ValueError:
                continue
            have = current.get(pair) or {}
            cube = entry.get("cube_path")
            if cube and not have.get("cube_path"):
                try:
                    self.controller.set_3dlut(mon, pair_mode, cube)
                    self.ctx.log(f"re-applied the {pair} runtime 3D LUT that calibration.enter "
                                 f"had dropped ({cube})")
                except Exception as exc:  # noqa: BLE001
                    self.ctx.log(f"could not re-apply the {pair} runtime 3D LUT ({cube}): {exc} "
                                 "— re-apply it manually (Set 3D LUT in DesktopLUT)")
            # C++ state.get reports only cube_path, so on hardware there is nothing to
            # restore here; the simulator DOES report the tweak, so put back exactly what
            # was captured (verbatim payload — runtime.set_grayscale_tweak is advertised).
            tweak = entry.get("grayscale_tweak")
            if tweak and not have.get("grayscale_tweak"):
                try:
                    self.controller.call("runtime.set_grayscale_tweak",
                                         {"monitor": mon, "mode": pair_mode,
                                          "grayscale_tweak": tweak})
                    self.ctx.log(f"re-applied the {pair} runtime grayscale tweak that "
                                 "calibration.enter had dropped")
                except Exception as exc:  # noqa: BLE001
                    self.ctx.log(f"could not re-apply the {pair} runtime grayscale tweak: {exc}")

    # -- viewing layers: capture → measure without → restore (plan item 0b) -------------
    def _enter_measurement_layers(self) -> Optional[dict[str, Any]]:
        """Before the first live read: capture the user's viewing layers for the calibrated
        pair (HDR tonemap / Desktop Gamma / GUI white balance / GUI grayscale) from the pipe
        and switch OFF the ones that are on — a calibration measures the stack it builds on,
        never through the user's viewing tweaks (the 19:14 WB permutation of 2026-09-03 would
        have been baked under the cube). The user never manages these around a run: what was
        captured is restored at the run's terminal end (:meth:`_restore_viewing_layers`),
        apply or revert. Memoised in ``calib['viewing_layers']`` (a resume must not re-capture
        the already-cleared state as "the user's"). Servers without ``layers`` in ``state.get``
        (pre-2026-09-03 builds) yield ``supported: False`` — the ini audit stays the evidence."""
        rec = self.calib.get("viewing_layers")
        if isinstance(rec, dict) and rec.get("captured"):
            if rec.get("restored") and rec.get("disabled"):
                # A rollback / abort already PUT THE USER'S LAYERS BACK, and this run is measuring
                # again (a resume after a cancel). Switch the recorded ones off again — never
                # re-capture (the original `before` stays the user's state for the terminal restore)
                # — or the resumed stages would measure THROUGH the viewing layers (2026-10-02: a
                # resumed PA SDR 3dlut-only would have probed its cube through the FALD layer).
                try:
                    res = self.controller.set_layers(self.monitor, self.mode,
                                                     **{n: False for n in rec["disabled"]})
                    rec.update(restored=False, after=res.get("after"),
                               profile_after=res.get("profile_name"),
                               recleared_after_restore=sorted(rec["disabled"]))
                    self.ctx.log(f"viewing layers OFF again after the rollback restore: "
                                 f"{', '.join(rec['disabled'])}")
                except Exception as exc:  # noqa: BLE001 - surfaced; the readiness audit still judges
                    rec["error"] = f"layers.set (re-clear) failed: {type(exc).__name__}: {exc}"
                    self.runlog.anomaly("hardware-readiness", kind="viewing_layers",
                                        message=f"could not switch the viewing layers off again: {rec['error']}")
                self.calib["viewing_layers"] = rec
                self._save()
            return rec
        try:
            state = self.controller.state() or {}
        except Exception as exc:  # noqa: BLE001
            rec = {"captured": False, "supported": None,
                   "error": f"state.get unavailable: {type(exc).__name__}: {exc}"}
            self.calib["viewing_layers"] = rec
            self._save()
            return rec
        before = CalibrationController.layers_from_state(state, self.monitor, self.mode)
        if before is None:
            rec = {"captured": False, "supported": False,
                   "note": "server reports no viewing layers (pre-layers.set build) — the ini flags "
                           "are the only evidence; disable tonemap/DG/WB/GS by hand if ON"}
            self.calib["viewing_layers"] = rec
            self._save()
            return rec
        # --keep-layers (verify-only): left exactly as the user has them — never switched off, never on.
        keep = set(self.calib.get("keep_layers") or ())
        to_clear = {name: False for name, on in before.items() if on and name not in keep}
        # monitor/mode ride the record so a teardown outside this object (the --abort path, the
        # CLI rollback guard) can re-assert the layers without trusting argparse defaults.
        rec = {"captured": True, "supported": True, "before": before, "disabled": sorted(to_clear),
               "restored": False, "monitor": self.monitor, "mode": self.mode}
        if keep:
            rec["kept"] = {n: bool(before.get(n)) for n in sorted(keep)}
            off = [n for n in sorted(keep) if not before.get(n)]
            if off:
                # Asked to keep a layer the user has OFF: it stays off (the run measures the user's
                # state) — a fact for the LLM, who may want it on for the question being asked.
                rec["kept_but_off"] = off
                self.runlog.note("hardware-readiness",
                                 f"--keep-layers {', '.join(off)}: OFF in the user's stack — kept OFF",
                                 kept_but_off=off)
        if to_clear:
            try:
                res = self.controller.set_layers(self.monitor, self.mode, **to_clear)
                rec["after"] = res.get("after")
                rec["regenerated"] = bool(res.get("regenerated"))
                rec["profile_after"] = res.get("profile_name")
                self.ctx.log(f"viewing layers OFF for the run: {', '.join(rec['disabled'])} "
                             f"(captured for restore; profile now {res.get('profile_name')})")
            except Exception as exc:  # noqa: BLE001 - surfaced, the readiness audit still judges
                rec["error"] = f"layers.set failed: {type(exc).__name__}: {exc}"
                self.runlog.anomaly("hardware-readiness", kind="viewing_layers",
                                    message=f"could not switch the viewing layers off: {rec['error']}")
        self.calib["viewing_layers"] = rec
        self._save()
        self.runlog.note("hardware-readiness",
                         "viewing layers captured" + (f"; OFF: {', '.join(rec['disabled'])}" if to_clear
                                                     else " (all already off)"),
                         **{k: rec.get(k) for k in ("before", "disabled", "regenerated")})
        return rec

    def _restore_viewing_layers(self) -> Optional[dict[str, Any]]:
        """Terminal end of the run (completed / reverted / aborted — never a pause): put back
        the viewing layers captured by :meth:`_enter_measurement_layers`. On a revert of a flow
        that entered calibration mode the C++ snapshot already restored them; re-asserting the
        captured values is idempotent. On apply the layers land on the NEW stack (a WB/GS/DG
        change re-bakes the new profile's permutation — the user's viewing preference on top
        of the fresh foundation). Best-effort, recorded on the run record."""
        rec = self.calib.get("viewing_layers")
        if not isinstance(rec, dict) or not rec.get("captured") or rec.get("restored"):
            return rec
        kept = set(rec.get("kept") or ())       # --keep-layers: never changed by the run — never re-set
        want = {name: bool(on) for name, on in (rec.get("before") or {}).items() if on and name not in kept}
        if not want:
            rec["restored"] = True
            rec["restore_note"] = "nothing was on"
            self.calib["viewing_layers"] = rec
            self._save()
            return rec
        try:
            res = self.controller.set_layers(self.monitor, self.mode, **want)
            rec["restored"] = True
            rec["restore_after"] = res.get("after")
            rec["restore_profile"] = res.get("profile_name")
            self.ctx.log(f"viewing layers restored: {', '.join(sorted(want))} back ON "
                         f"(profile now {res.get('profile_name')})")
        except Exception as exc:  # noqa: BLE001
            rec["restored"] = False
            rec["restore_error"] = f"{type(exc).__name__}: {exc}"
            self.ctx.log(f"could not restore the viewing layers ({rec['restore_error']}); "
                         f"re-enable by hand: {', '.join(sorted(want))}")
            self.runlog.anomaly("run", kind="viewing_layers",
                                message=f"viewing layers NOT restored: {rec['restore_error']}; "
                                        f"re-enable {', '.join(sorted(want))} in DesktopLUT")
        self.calib["viewing_layers"] = rec
        self._save()
        return rec

    def _record_applied_stack(self, deliverable_cube: Optional[str]) -> None:
        """Apply path: persist what this run left installed to the per-display applied-stack
        registry (``stack_registry.py``) — the MHC's calibrated top for a later ``3dlut-only`` to
        pin its peak to, and the durable cube path. Best-effort: never a gate on the run."""
        flow = self.calib.get("flow")
        try:
            reg = stack_registry.StackRegistry.load(
                stack_registry.registry_path(self.profile, self.ctx.root))
            try:
                pipe = self.controller.state() or {}
                pipe_profile = ((pipe.get("mhc") or {}).get(f"{self.monitor}:{self.mode}") or {}).get("profile_name")
            except Exception:  # noqa: BLE001
                pipe_profile = None
            cube = deliverable_cube or (
                (self.calib["stages"].get("build-install-3dlut") or {}).get("digest") or {}).get("cube_path") \
                or ((self.calib["stages"].get("reapply-3dlut") or {}).get("data") or {}).get("cube_path")
            params = self._state.get("mhc_params") or {}
            cube_white = self._cube_target_white_nits()
            if flow in ("full", "mhc-only", "refine-mhc") and params:
                rec = stack_registry.record_from_mhc_params(
                    display=self.display.name, mode=self.mode, monitor=self.monitor,
                    run_id=self.ctx.root.name, profile_name=pipe_profile, mhc_params=params,
                    target_white_xy=self._white_xy())
                if cube:
                    rec.cube = {"cube_path": cube, "run_id": self.ctx.root.name,
                                "applied_at": rec.applied_at}
                    if cube_white is not None:
                        rec.cube["target_white_nits"] = cube_white
                reg.record(rec)
                self.ctx.log(f"applied-stack registry: {rec.key} <- run {rec.run_id} "
                             f"(profile {pipe_profile}, top {rec.cube_peak_nits}, "
                             f"sdr white {rec.sdr_white_nits})")
            elif flow == "3dlut-only":
                rec = reg.record_cube(display=self.display.name, mode=self.mode, monitor=self.monitor,
                                      run_id=self.ctx.root.name, cube_path=cube, profile_name=pipe_profile,
                                      target_white_nits=cube_white)
                self.ctx.log(f"applied-stack registry: {rec.key} cube <- run {self.ctx.root.name}")
        except Exception as exc:  # noqa: BLE001 - registry is priors for the next run, never a gate
            self.ctx.log(f"applied-stack registry not updated ({type(exc).__name__}: {exc})")

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
            self._hook_routing_evidence_after_install("apply")
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
        target = self.profile.engine_target(self.target_name, white_xy=self._white_xy())
        # ...and the SAME white luminance: an SDR grayscale refine that chose a white inside (or,
        # adjudicated, below) the white band delivers THAT white, not the target's nominal nits.
        # Targeting the nominal would ask the cube for an unreachable brighter tone curve — every
        # signal above (white/nominal)^(1/γ) clipped at the top and the refined greys lifted off
        # the MHC (BenQ run 20260926_225451: 107.2-nit white vs 120 nominal → 280 "floor" patches).
        if getattr(target, "transfer", None) != "pq":
            white_nits = self._sdr_refined_white_nits()
            if white_nits is not None:
                target = replace(target, peak_nits=white_nits)
        # The OOG policy is memoised in the run record the first time it is used (_oog_mapping):
        # a resume keeps building/scoring/projecting with the policy the run started with.
        cached = (getattr(self, "calib", None) or {}).get("oog_mapping")
        if cached and cached != getattr(target, "oog_mapping", cached):
            target = replace(target, oog_mapping=str(cached))
        return target

    def _sdr_refined_white_nits(self, *, capture: bool = True) -> Optional[float]:
        """The white luminance the SDR MHC under the cube delivers, or ``None`` when unknown (the
        target's nominal nits then stand). See :meth:`_sdr_calibrated_white` for the sources."""
        return self._sdr_calibrated_white(capture=capture)[0]

    def _sdr_calibrated_white(self, *, capture: bool = True) -> tuple[Optional[float], str]:
        """``(white_nits, source)`` of the SDR white the cube sits on:

        * ``refined_this_run`` — THIS run's white-band refine (``mhc_params['sdr_white']``);
        * ``installed_stack`` — ``3dlut-only`` keeps the installed MHC and has no refine of its
          own: the applied-stack registry's recorded white, trusted only when the pipe's profile
          cross-checks (``_installed_stack_evidence``, the HDR cap pin's twin);
        * ``(None, "nominal")`` — neither (the plan seam flags it for 3dlut-only).

        ``capture=False`` only PEEKS at an already-memoised installed-stack record (the dashboard
        header, emitted before preflight, must not snapshot the pipe for the plan seam)."""
        params = (getattr(self, "_state", None) or {}).get("mhc_params") or {}
        white = _as_float_local((params.get("sdr_white") or {}).get("white_nits"))
        if white is not None and white > 0:
            return white, "refined_this_run"
        calib = getattr(self, "calib", None) or {}
        if self._sdr_in_hdr():
            # SDR content on an HDR display: Windows composites it at the live SDR white level — the
            # white the stack delivers to SDR content (preflight's DisplayConfig read; None = unknown).
            white = _as_float_local((calib.get("sdr_white_level") or {}).get("nits"))
            return (white, "windows_sdr_white_level") if white is not None and white > 0 else (None, "nominal")
        if calib.get("flow") in ("3dlut-only", "verify-only") and self.content_mode != "HDR":
            stack = (self._installed_stack_evidence() if capture else calib.get("installed_stack")) or {}
            white = _as_float_local(stack.get("sdr_white_nits"))
            if white is not None and white > 0:
                return white, "installed_stack"
        return None, "nominal"

    def _sdr_in_hdr(self) -> bool:
        """SDR content measured on a display in HDR (verify-only ``--content-mode SDR``)."""
        return getattr(self, "content_mode", None) == "SDR" and self.mode == "HDR"

    def _cube_target_white_nits(self) -> Optional[float]:
        """The SDR white the 3D LUT this run leaves installed was BUILT for: this run's build
        (``build-install-3dlut`` digest ``target_white_nits``), else the kept source cube's when
        its build recorded one (``reapply-3dlut`` ``cube_white``). ``None`` for HDR / unknown."""
        if self.mode == "HDR":
            return None
        stages = self.calib.get("stages") or {}
        built = ((stages.get("build-install-3dlut") or {}).get("digest") or {}).get("target_white_nits")
        if built is not None:
            return _as_float_local(built)
        kept = (((stages.get("reapply-3dlut") or {}).get("digest") or {}).get("cube_white") or {})
        return _as_float_local(kept.get("source_cube_white_nits"))

    def _reachable_primaries(self) -> Optional[dict]:
        """The panel's MEASURED native primaries — THIS run's (from the raw stage's channel model,
        persisted to ``mhc_params`` at build), falling back to the prior DIP — used to clamp the
        optimizer/verify target onto the physically reachable gamut (#C3) AND to cap the gamut-aware
        verify ramp's saturation. A saturated target the panel can't render is scored as a clip, not
        chased toward an unreachable Rec.2020 corner. ``None`` ⇒ no clamp/cap (prior behaviour).

        Production use is HDR/wide-gamut only. The SDR clamp experiment was CV-gated worse, so SDR
        returns ``None`` and stays on the plain sRGB scoring/build target."""
        if self.content_mode != "HDR":
            return None
        # Prefer THIS run's freshly-measured native primaries (raw-stage channel model, persisted
        # to mhc_params at build) over the prior DIP — same session, current thermal state, and no
        # stale-DIP dependency for the gamut-aware verify caps + the #C3 clamp. The raw stage runs
        # before verify, so by then this is populated; fall back to the DIP before the build has run
        # (or a no-build flow), then None. (Self-contained gamut awareness without a probe stage —
        # the literal post-warmup probe is only needed once RAW generation is gamut-aware too.)
        # Conversion + degenerate guard shared with the stage tools (metrics.py), so a
        # stage-CLI score clamps against exactly the same measured gamut this run does (P1).
        # DELIBERATE behaviour change vs the pre-Phase-6 code (verification pass, B5): a
        # DEGENERATE-but-complete mhc_params.primaries (corrupt fresh raw measurement) now
        # falls through to the prior DIP's sane gamut instead of disabling the clamp
        # entirely — a real previous measurement beats clamping against nothing. The stage
        # tools have no DIP access, so they skip the clamp in that corner (surfaced as
        # gamut_aware:false); the corner requires a corrupt build record to reach at all.
        prim = metrics_mod.reachable_primaries_from_mhc_params(self._state.get("mhc_params"))
        if prim is None:
            dip = self._dip()
            if dip is None or not dip.native_primaries:
                return None
            prim = metrics_mod.sanitize_reachable_primaries(
                {ch: [float(xy[0]), float(xy[1])]
                 for ch, xy in dip.native_primaries.items() if xy and len(xy) >= 2})
        return prim

    def _oog_mapping(self) -> str:
        """The run's out-of-gamut target policy (``Target.oog_mapping`` of the engine target —
        profile key ``oog_mapping``, default the owner's 2026-09-23 "vertex" policy), so verify and
        the stage scores clamp exactly as the cube build did."""
        cached = self.calib.get("oog_mapping")
        if cached:
            return str(cached)
        default = "vertex"
        if self.target_name is not None:
            default = str(getattr(self.profile.target(self.target_name), "oog_mapping", "vertex") or "vertex")
        # A record that already measured without a memo predates the policy: keep its legacy clamp
        # (metrics.run_oog_mapping). Memoised in the run record — pinned at resolve-target, before
        # any measurement — so a resume and the stage CLIs (score / report) score against the same
        # policy the cube was built for.
        value = metrics_mod.run_oog_mapping(self.calib, default=default)
        self.calib["oog_mapping"] = value
        return value

    def _optimizer_report_scorer(self):
        """The metric the 3D-LUT optimizer's SURFACED numbers are re-scored into for the LLM/user:
        CIEDE2000 for SDR, dE_ITP for HDR. The cube still CONVERGES in dE_ITP either way (the engine
        is untouched) — this only relabels the build digest/convergence curve so an SDR run never
        feeds dE_ITP as if it were CIEDE2000 (dE_ITP HDR-only / CIEDE2000 SDR-only, owner directive).
        Returns ``(scorer | None, metric_name)``; HDR returns ``(None, "dE_ITP")`` so the optimizer
        keeps its native numbers. The SDR scorer reuses the SAME ``score_samples`` machinery (sRGB
        γ-power, resolved white, no native-gamut clamp) the SDR verify stage uses, so the build and
        verify stages quote one consistent CIEDE2000."""
        target = self._engine_target()
        if getattr(target, "transfer", None) == "pq":
            return None, "dE_ITP"
        from .metrics import score_samples
        from .mhc import Ti3Sample
        wx, wy = self._white_xy()
        gamma = float(getattr(target, "gamma", 2.2))
        luminance = float(getattr(target, "peak_nits", 0.0)) or None  # SDR white nits (None ⇒ infer)

        def scorer(signals, measured_xyz):
            samples = [
                Ti3Sample(rgb=(float(s[0]), float(s[1]), float(s[2])),
                          xyz=(float(x[0]), float(x[1]), float(x[2])))
                for s, x in zip(signals, measured_xyz)
            ]
            metrics, _ = score_samples(samples, luminance=luminance, gamma=gamma, white_xy=(wx, wy))
            return [m.de2000 for m in metrics]

        return scorer, "CIEDE2000"

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
        pin = self._installed_stack_evidence()
        pin_nits = pin.get("pin_nits") if pin else None
        tgt = self.profile.resolve_hdr_target(self.target_name, dip=self._dip(),
                                              white_xy=self._white_xy(),
                                              pinned_peak_nits=pin_nits)
        if pin_nits:
            # Provenance says WHY the peak is the installed cap (the plan seam quotes it).
            prov = dict(tgt.provenance or {})
            prov["peak"] = {**(prov.get("peak") or {}), "source": "installed_mhc_cap",
                            "grounded": True, "sustained_unknown": False,
                            "note": pin.get("reason"),
                            "installed_stack": {k: pin.get(k) for k in
                                                ("run_id", "recorded_profile", "pipe_profile", "matches")}}
            tgt = replace(tgt, provenance=prov)
        self.calib["hdr_target"] = tgt.as_dict()
        self._save()
        return tgt

    _FLOWS_KEEPING_MHC = ("3dlut-only", "grayscale-wb", "verify-only")
    _REPIN_MIN_SHORTFALL = 0.005    # cap must sit > 0.5 % under the resolved peak to re-pin

    def _installed_stack_evidence(self) -> Optional[dict[str, Any]]:
        """For flows that KEEP the installed MHC: what the applied-stack registry recorded for
        this display+mode, cross-checked against the pipe's current profile name — the evidence
        the HDR peak pin rests on (``pin_nits`` set only when the record is trustworthy). Memoised
        in the run record (a resume must see the same pin the plan was approved with). ``None``
        for flows that build their own MHC (the cap is decided in-run and re-pinned there)."""
        if self.calib.get("flow") not in self._FLOWS_KEEPING_MHC:
            return None
        cached = self.calib.get("installed_stack")
        if isinstance(cached, dict):
            return cached
        try:
            reg = stack_registry.StackRegistry.load(
                stack_registry.registry_path(self.profile, self.ctx.root))
            rec = reg.get(self.display.name, self.mode)
        except Exception as exc:  # noqa: BLE001 - priors, never a gate
            rec, reg = None, None
            self.ctx.log(f"stack registry unreadable ({type(exc).__name__}: {exc}); peak not pinned")
        try:
            pipe_state = self.controller.state()
        except Exception:  # noqa: BLE001
            pipe_state = None
        evidence = stack_registry.check_against_pipe(rec, pipe_state, self.monitor, self.mode)
        if reg is not None and (reg.corrupt or reg.dropped):
            evidence["registry_warning"] = (f"registry corrupt={reg.corrupt} dropped={reg.dropped}")
        self.calib["installed_stack"] = evidence
        self._save()
        return evidence

    def _pin_hdr_peak_to_cap(self, outcome: "StageOutcome") -> None:
        """After an HDR MHC build in THIS run: when the Peak-Chroma policy capped the base cube
        below the resolved peak, the stack's calibrated top IS the cap — re-pin the run's HDR
        target to it so every post-MHC stage (volumetric patches, the cube's targets, verify)
        bounds and scores against what the stack holds, not a luminance it deliberately gave up
        (one source of truth, Task C). The plan is re-fingerprinted with the new cap (a
        deterministic consequence of the adjudicated build, recorded as such — not a re-approval
        the LLM never saw). Idempotent on replay."""
        pc = (outcome.digest or {}).get("peak_chroma") or {}
        cap = _as_float_local(pc.get("cube_peak_nits"))
        if cap is None or cap <= 0 or not pc.get("capped"):
            return
        hdr = self._hdr_target()
        # A real cap, not the top patch's PQ quantization: the build reports ``capped`` whenever
        # the cube top sits under the resolved peak at all, and the highest measured code lands
        # ~0.3 % under the peak on a 10-bit PQ ramp. Below _REPIN_MIN_SHORTFALL the stack holds
        # the resolved peak to within a code step — nothing to re-target.
        if cap >= hdr.peak_nits * (1.0 - self._REPIN_MIN_SHORTFALL):
            return
        prior_fp = (self.calib.get("patch_plan") or {}).get("fingerprint")
        prov = dict(hdr.provenance or {})
        prov["peak"] = {**(prov.get("peak") or {}), "source": "mhc_cap", "grounded": True,
                        "resolved_peak_nits": hdr.peak_nits,
                        "cap_policy": pc.get("cap_policy"), "binding_channel": pc.get("binding_channel"),
                        "note": (f"re-pinned from {hdr.peak_nits:.1f} to the MHC's calibrated top "
                                 f"{cap:.1f} nits ({pc.get('cap_policy') or 'cap'}, binding "
                                 f"{pc.get('binding_channel')}) — post-MHC stages target what the stack holds")}
        pinned = replace(hdr, peak_nits=float(cap), knee_start_nits=min(hdr.knee_start_nits, float(cap)),
                         provenance=prov)
        self.calib["hdr_target"] = pinned.as_dict()
        plan = self._patch_plan_record(self.calib.get("flow"))
        self.calib["patch_plan"] = {**plan, "approved": True, "repinned_from": prior_fp,
                                    "repin_reason": f"HDR peak {hdr.peak_nits:.1f} -> {cap:.1f} nits (MHC cap)"}
        self._save()
        self.ctx.log(f"HDR target peak re-pinned to the MHC's calibrated top: {hdr.peak_nits:.1f} -> "
                     f"{cap:.1f} nits; post-MHC patch cap cv {self._patch_max_cv()}")
        self.runlog.note("build-install-mhc",
                         f"HDR peak re-pinned {hdr.peak_nits:.1f} -> {cap:.1f} nits (MHC cap)",
                         peak_from=hdr.peak_nits, peak_to=cap, patch_max_cv=self._patch_max_cv(),
                         plan_fingerprint_from=prior_fp, plan_fingerprint_to=self.calib["patch_plan"].get("fingerprint"))

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
        return dip_record_for(self._dip_store(), self.display.name, self.mode)

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

    def _preheat_policy(self) -> Optional[str]:
        """The run's explicit thermal preheat policy (``--preheat``; persisted in the run record),
        or ``None`` = not asked: the measure-loop config's own policy (``auto``) stands."""
        policy = self.calib.get("preheat")
        return str(policy) if policy else None

    def _thermal_state(self) -> str:
        """The run's thermal state (``--thermal-state``; persisted), default ``verify`` = today's
        own-band preheat."""
        return str(self.calib.get("thermal_state") or viewing_thermal.DEFAULT_THERMAL_STATE)

    def _thermal_state_plan(self, key: str, role: str, patches: Sequence[tuple[int, int, int]]
                            ) -> tuple[Optional[viewing_thermal.ViewingPrecondition], dict[str, Any]]:
        """The thermal state a measure stage runs in: ``(viewing precondition or None, digest record)``.

        * ``verify`` (default): today's own-band preheat — the record only names it.
        * ``viewing`` + a BUILD stage (raw / post-MHC = the cube build's training set): measured at its own
          (loaded) band by the OWNER DECISION 2026-10-09 (the thermal offset goes into the profile through the
          MHC refine, which runs in the viewing state — :meth:`_refine_measure`); the record says "viewing
          requested, not applied", with the set's model band, so nothing pretends otherwise.
        * ``viewing`` + the VERIFY: a seam with the target band, the assumed start state, the model's
          precondition time and whether the set holds the band; the LLM chooses precondition /
          measure-now / abort. Nothing here auto-accepts (AutoAdjudicator is sim/CI only)."""
        state = self._thermal_state()
        if state != "viewing":
            return None, {"state": "verify", "basis": "own-band preheat (the set's own load — today's behaviour)"}
        law = viewing_thermal.LoadLaw()
        transfer = self._transfer()
        band = viewing_thermal.set_band(patches, transfer, law)
        if role != "verify":
            return None, {"state": "verify", "requested": "viewing", "applied": False, "policy": "own",
                          "model_band": band,
                          "basis": ("viewing requested, not applied (own band; policy per owner decision "
                                    "2026-10-09: raw + the cube build measure in the loaded own-band state, the "
                                    "MHC closed-loop refine runs in the viewing state, the verify is practical)")}
        target, target_nits, target_src = self._viewing_target(law)
        half = viewing_thermal.BAND_HALFWIDTH_FRAC * target
        start, start_src, start_kind, unmodelled = self._viewing_start(key, band["load"], law)
        minutes = law.minutes_to_band(start, target, target, viewing_thermal.CONVERGE_MARGIN * half)
        hold = viewing_thermal.predict_hold(patches, transfer, law, start_load=target, target_load=target,
                                            halfwidth=half)
        deadline_s, cap_min, capped = self._precondition_budget(minutes)
        holds = bool(hold["in_band_fraction"] is not None and hold["in_band_fraction"] >= 0.95)
        lo, hi = round(target - half, 5), round(target + half, 5)
        digest = {
            "state": "viewing",
            "target": {"load": round(target, 5), "nits_equiv": round(float(target_nits), 2), "band": [lo, hi],
                       "source": target_src},
            "start": {"load": round(start, 5), "nits_equiv": round(law.nits_equiv(start), 2), "source": start_src,
                      "kind": start_kind, **({"unmodelled_stages": unmodelled} if unmodelled else {})},
            "predicted": {"precondition_minutes": (round(minutes, 1) if minutes is not None else None),
                          "stage_minutes": band["minutes"], "set_own_band": band,
                          "hold_from_band": hold, "set_holds_band": holds},
            "precondition_budget": {"deadline_min": round(deadline_s / 60.0, 1), "cap_min": cap_min,
                                    "capped": capped},
            "model": law.as_dict(),
            "basis": viewing_thermal.MODEL_BASIS,
            "caveat": ("model predictions (first-order PA32UCXR fit): absolute loads/times +-~2x; the slope "
                       "gate cannot see a tau~25 min drift, so the model is the precondition's time floor"),
        }
        hold_txt = ("its own order holds the band (model)" if holds else
                    f"its own band is {band['load']} (~{band['nits_equiv']} nit-eq) so it DRIFTS out of the "
                    f"viewing band while measuring (in band {hold['in_band_fraction']} of the time, model) — "
                    "a viewing-state verify needs a content-sampled --verify-patches-file")
        cap_txt = (f"The soak is capped at {round(deadline_s / 60.0, 1)} min"
                   + (f" (the {cap_min:g}-min cap = 4 tau binds: the model needs longer, so expect it flagged unmet)"
                      if capped else f" (model x 1.5 + 5 min; hard cap {cap_min:g} min = 4 tau)")
                   + "; past it the stage is flagged, never silently extended.")
        start_fix = ("" if start_kind == "given" else
                     " If you know what the panel showed before (e.g. it sat at the desktop, or was off), answer "
                     "this seam on a resume with --viewing-start-nits N (allowed until measure:verify is measured): "
                     "the seam re-asks with that start. An under-estimated start lets the model claim 'in band' "
                     "on a hotter panel.")
        question = (
            f"Viewing thermal state for {key}: target band {lo}..{hi} load (~{round(float(target_nits), 1)} "
            f"nit-equivalent; {target_src}). Modelled start {round(start, 4)} ({start_src}).{start_fix} "
            f"precondition = soak a dim neutral stand-in at the viewing load until the MODELLED state is in band "
            f"(~{round(minutes, 1) if minutes is not None else '?'} min predicted, then the "
            f"~{band['minutes']} min set; the numbers then represent the viewing state, per the model). {cap_txt} "
            "measure-now = no soak: the numbers represent whatever state the panel is in (tracked and recorded, "
            f"flagged). abort = stop the run. This set: {hold_txt}.")
        decision = self.adjudicate(AdjudicationRequest(
            key=f"{key}:thermal-state", seam=SEAM_THERMAL_STATE, stage=key, question=question,
            options=("precondition", "measure-now", "abort"), recommendation="precondition", digest=digest))
        if decision.choice == "abort":
            raise CalibrationAborted(StageOutcome(key, "aborted", digest={
                "message": "viewing thermal-state precondition declined (abort)", "thermal_state": digest,
                "decision_note": decision.note}))
        self._spend_given_start(key, start_kind)
        spec = viewing_thermal.ViewingPrecondition(
            target_load=target, halfwidth=half, start_load=start, start_source=start_src,
            target_source=target_src, deadline_s=deadline_s, soak=(decision.choice == "precondition"), law=law)
        return spec, {**digest, "decision": decision.choice, "decision_note": decision.note}

    # -- viewing state: shared resolution (the verify's seam and the MHC refine's) ------------------------
    def _viewing_target(self, law: viewing_thermal.LoadLaw) -> tuple[float, float, str]:
        """The viewing target ``(load, nit-equivalent, source)``: ``--viewing-load-nits``, else the content
        survey's band for this content mode (model nit-equivalent)."""
        hdr = getattr(self, "content_mode", None) == "HDR"
        target_nits = self.calib.get("viewing_load_nits")
        target_src = "--viewing-load-nits"
        if target_nits is None:
            target_nits = viewing_thermal.VIEWING_NITS_EQUIV["HDR" if hdr else "SDR"]
            target_src = ("content survey 2026-10-09 (" + ("HDR" if hdr else "SDR content") + " balanced set band, "
                          "model nit-equivalent)")
        return law.load(float(target_nits)), float(target_nits), target_src

    def _given_start(self, key: str) -> tuple[Optional[float], Optional[str]]:
        """``--viewing-start-nits`` for the seam ``key``: ``(value, None)`` when it applies, ``(None, why)``
        when it was already SPENT by another stage's seam (it answers ONE seam: what the panel showed right
        before it; a later seam's start comes from the run history instead). A new value re-arms it."""
        given = self.calib.get("viewing_start_nits")
        if given is None:
            return None, None
        spent = self.calib.get("viewing_start_spent") or {}
        if spent:
            return None, (f"--viewing-start-nits {float(given):g} answered the {spent.get('by')} seam and was SPENT "
                          f"by its {spent.get('reason')} (the panel has run since: it no longer describes the "
                          "panel); not reused — a new value re-arms it")
        used_by = self.calib.get("viewing_start_used_by")
        if used_by and used_by != key:
            return None, (f"--viewing-start-nits {float(given):g} answered the {used_by} seam (it describes the "
                          "panel before THAT stage); not reused here")
        return float(given), None

    def _spend_given_start(self, key: str, start_kind: str) -> None:
        """A seam decided with a given start has spent it (:meth:`_given_start`)."""
        if start_kind == "given" and self.calib.get("viewing_start_used_by") != key:
            self.calib["viewing_start_used_by"] = key
            self._save()

    def _spend_given_start_on_remeasure(self, key: str, reason: str) -> None:
        """A REMEASURE of the stage whose seam ``key`` took ``--viewing-start-nits`` spends that start for good:
        it described the panel before the FIRST pass, and the panel has run since (the soak, the reads), so
        the re-asked seam must start from this run's history — or assume hot — as the remeasure question says,
        never re-read the same (possibly cool) given start and over-claim "in band" on a hotter panel. A new
        value re-arms it (:func:`resolve_thermal_knobs`). No-op when the start was not this seam's."""
        if self.calib.get("viewing_start_nits") is None or self.calib.get("viewing_start_used_by") != key:
            return
        self.calib["viewing_start_spent"] = {"by": key, "reason": reason,
                                             "at": datetime.now().isoformat(timespec="seconds")}

    def _viewing_start(self, key: str, own_band_load: float, law: viewing_thermal.LoadLaw
                       ) -> tuple[float, str, str, list[str]]:
        """The modelled start state for the viewing seam ``key``: ``(load, source, kind, unmodelled stages)``,
        ``kind`` given | run-history | assumed-hot (:func:`viewing_thermal.start_state`)."""
        given_start, spent = self._given_start(key)
        unmodelled = [] if given_start is not None else self._unmodelled_since_history(key)
        start, start_src = viewing_thermal.start_state(
            history=self.calib.get("thermal_history") or [], own_band_load=own_band_load, law=law,
            start_nits=given_start, unmodelled=unmodelled)
        start_kind = ("given" if given_start is not None else
                      "assumed-hot" if start_src.startswith("assumed HOT") else "run-history")
        if spent:
            start_src = f"{start_src} [{spent}]"
        return start, start_src, start_kind, unmodelled

    @staticmethod
    def _precondition_budget(minutes: Optional[float]) -> tuple[float, float, bool]:
        """The precondition budget ``(deadline_s, cap_min, capped)``: model time-to-band x 1.5 + 5 min,
        CAPPED (viewing_thermal.PRECONDITION_CAP_MIN, 4 tau) — past it the controller's normal flag path
        applies (flagged, never silently extended)."""
        cap_min = viewing_thermal.PRECONDITION_CAP_MIN
        budget_min = (minutes or 0.0) * 1.5 + 5.0
        return min(budget_min, cap_min) * 60.0, cap_min, budget_min > cap_min

    def _note_thermal_history(self, key: str, load: Optional[float], *, basis: str) -> None:
        """Viewing runs only: the load each measure stage left the panel at (+ when, + the stages memoised
        by then), so the next stage's viewing precondition starts from the run's recorded history — but only
        when nothing unmodelled drove the display since (:meth:`_unmodelled_since_history`)."""
        if load is None:
            return
        import time   # local, as elsewhere in this module
        hist = self.calib.setdefault("thermal_history", [])
        hist.append({"stage": key, "load": round(float(load), 5), "ended_epoch": round(time.time(), 1),
                     "basis": basis, "stages_done": sorted(set(self.calib.get("stages") or {}) | {key})})

    def _unmodelled_since_history(self, key: str) -> list[str]:
        """The stages memoised AFTER the last thermal-history entry (other than ``key`` itself). Their display
        load — MHC refine rounds, the cube build's probe reads, anything else that showed patches — is NOT
        counted by the history, so any such stage makes the history-based start untrusted (start_state then
        assumes hot and says why). An entry without a stage snapshot is untrusted too."""
        hist = self.calib.get("thermal_history") or []
        if not hist:
            return []
        snap = hist[-1].get("stages_done")
        if snap is None:
            return ["(history entry without a stage snapshot)"]
        return sorted(set(self.calib.get("stages") or {}) - set(snap) - {key})

    def _forget_decision(self, key: str, *, overrides: bool) -> None:
        """Drop a memoised seam decision so the seam re-asks: the run record's copy, the adjudicator's seed
        (Mapping/Supervised are seeded from the record + --decide at process start) and, with
        ``overrides``, this process's --decide override."""
        (self.calib.get("decisions") or {}).pop(key, None)
        if overrides:
            self.decision_overrides.pop(key, None)
        seed = getattr(self.adjudicator, "decisions", None)
        if isinstance(seed, dict):
            seed.pop(key, None)

    def _verify_thermal_state(self) -> dict[str, Any]:
        """Which thermal state the verify's numbers represent (from its measure stage's record): requested
        vs ACHIEVED. A viewing request is labelled ``viewing`` only when the precondition reached the band
        and the measured pass stayed in it (model); a skipped/unmet precondition or a band exit is an
        evidence flag (``evidence_flags`` + ``needs_adjudication``) and the label says the state was not
        held — never a silently "viewing" verify."""
        rec = (((self.calib.get("stages") or {}).get(THERMAL_STATE_STAGE) or {}).get("digest") or {}).get(
            "thermal_state")
        out = self._verify_thermal_state_core(rec)
        # Whatever the verify itself ran in: when an MHC refine recorded a thermal state, the profile's white
        # was refined in THAT state — its line rides the verify digest (-> verify:accept, the report) always.
        summary = self._thermal_stage_summary() or {}
        mhc_white = summary.get("mhc_white")
        if mhc_white is not None and "mhc_white" not in out:
            out["mhc_white"] = mhc_white
            out["stages"] = summary.get("stages")
            if out.get("requested") != "viewing":
                out["note"] = (f"the verify was measured in the {out.get('state')!r} state; the MHC white was "
                               f"refined in the {mhc_white.get('state')!r} state ({mhc_white.get('stage')})")
            if mhc_white.get("evidence_flags") or out.get("requested") != "viewing":
                out["needs_adjudication"] = True
        return out

    def _verify_thermal_state_core(self, rec: Optional[dict[str, Any]]) -> dict[str, Any]:
        """:meth:`_verify_thermal_state` from the verify measure stage's own record."""
        if not rec:
            return {"state": self._thermal_state()}
        out = {k: rec.get(k) for k in ("state", "requested", "decision") if rec.get(k) is not None}
        if rec.get("requested") == "viewing" and "precondition" in rec:
            pre = rec.get("precondition") or {}
            ach = rec.get("achieved") or None
            flags: list[str] = []
            if pre.get("skipped"):
                flags.append("viewing_precondition_skipped")
            elif not pre.get("reached"):
                flags.append("viewing_precondition_unmet")
            if ach is None:
                flags.append("viewing_state_not_measured")
            elif ach.get("in_band_throughout") is not True:
                flags.append("viewing_band_left")
            esc = (self.calib.get("decisions") or {}).get(f"{THERMAL_STATE_STAGE}:escalation") or {}
            summary = self._thermal_stage_summary() or {}
            mhc_white = summary.get("mhc_white")
            if mhc_white is not None:
                out["mhc_white"] = mhc_white
            if summary.get("stages"):
                out["stages"] = summary["stages"]
            out.update({
                "state": "outside-viewing-band" if flags else (rec.get("state") or "viewing"),
                "achieved": ach, "target": rec.get("target"), "start": rec.get("start"),
                "precondition_reached": pre.get("reached"), "precondition_skipped": pre.get("skipped"),
                "measure": rec.get("measure"), "basis": viewing_thermal.MODEL_BASIS, "caveat": rec.get("caveat"),
                "evidence_flags": flags,
                "needs_adjudication": bool(flags or (mhc_white or {}).get("evidence_flags")),
                **({"escalation_decision": {"choice": esc.get("choice"), "note": esc.get("note")}} if esc else {}),
            })
        return out

    def _with_preheat(self, cfg: MeasureLoopConfig) -> MeasureLoopConfig:
        """``cfg`` with the run's ``--preheat`` policy applied — the one place the lever reaches the
        thermal controller's gate (``MeasureLoopConfig.preheat`` → ``_Loop._preheat_enabled``) for
        every batch measure stage and the grayscale-wb session. The run's ``--present-stall off``
        rides the same path (``stall_reads`` 0 disables the stuck-frame detector)."""
        if self.calib.get("present_stall") == "off" and cfg.stall_reads > 0:
            cfg = replace(cfg, stall_reads=0)
        policy = self._preheat_policy()
        if policy is None or cfg.preheat == policy:
            return cfg
        return replace(cfg, preheat=policy)

    def _resolve_white_now(self) -> cp.WhitePointResolution:
        """Resolve the target white, preferring a white SPD captured by a probe-match
        build (item 9) recorded in the store over the profile's ``display.white_spd``."""
        rec = self._correction_store().get(self.display.name, self.mode)
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
                     ti3_name: str, ndjson_name: str,
                     viewing: Optional[viewing_thermal.ViewingPrecondition] = None) -> MeasureLoopResult:
        transfer = self._transfer()
        dip = self._dip()
        cfg = self._with_preheat(self.loop_config or self._loop_config_for(dip))
        if viewing is not None:
            cfg = replace(cfg, viewing=viewing)
        meas_dir = self.ctx.root / "measurements"
        # Pass the DIP through: the loop reads a single adaptive-integration read by default
        # and escalates to more averaged reads only where the DIP's measured noise model says
        # this luminance needs SNR — never a fixed count, never a silent cap.
        self.liveness.set_stall_after(self._liveness_threshold(dip))
        return run_measure_loop(
            patches=patches, transfer=transfer, measure=self.measure, config=cfg,
            ti3_path=meas_dir / ti3_name, ndjson_path=meas_dir / ndjson_name,
            runlog=self.runlog, liveness=self.liveness, dip=dip,
            checkin_interval_s=self._checkin_interval_s, checkin_window=self._checkin_window,
            patch_min_reads=self._file_min_reads(patches) if role == "verify" else None,
            **self._plausibility_context(role, dip),
        )

    def _file_min_reads(self, patches: Sequence[tuple[int, int, int]]) -> Optional[list[int]]:
        """A verify-only ``--verify-patches-file``'s per-patch read requests aligned to ``patches`` (by
        code — any subset / re-measure keeps its request), else ``None`` (the loop's own policy)."""
        listed = self._verify_patches_file_record() if self.calib.get("flow") == "verify-only" else None
        if listed is None or not listed.get("min_reads"):
            return None
        req = {tuple(int(c) for c in p): int(r or 0) for p, r in zip(listed.get("codes") or (), listed["min_reads"])}
        return [req.get(tuple(int(c) for c in p), 0) for p in patches]

    def _plausibility_context(self, role: str, dip) -> dict[str, Any]:
        """Gamut/correction context for the measure loop's luminance-plausibility envelope
        (2026-09-02 C6 run, item #3): the MEASURED per-channel full-drive peaks from this
        display's ``mhc_params`` (a full-drive blue on a blue-weak WOLED is expected near its
        OWN 18.6-nit peak, never the 604-nit white peak), the DIP's real (WRGB non-additive)
        white for near-neutral headroom, and — once the MHC is installed (every role but the
        raw characterization) — the Peak-Chroma cap, whose clamp legitimately dims commanded
        targets above it. Empty dict (container fallback, previous behaviour) when the panel
        has no measured channel peaks yet — e.g. the first raw pass of a fresh display."""
        params = self._state.get("mhc_params") or {}
        peaks = params.get("channel_peak_xyz")
        try:
            ys = tuple(float(p[1]) for p in peaks) if peaks else ()
        except (TypeError, ValueError, IndexError):
            return {}
        if len(ys) != 3 or min(ys) <= 0.0:
            return {}
        ctxkw: dict[str, Any] = {"channel_peak_y": ys}
        white = _as_float_local(getattr(dip, "native_white_nits", None)) if dip else None
        if white is not None and white > sum(ys):
            ctxkw["white_peak_y"] = white
        if role != "raw":
            cap = _as_float_local((params.get("peak_chroma") or {}).get("cap_nits"))
            if cap is not None and cap > 0.0:
                ctxkw["correction_max_nits"] = cap
        return ctxkw

    def _bookend_drift_qc(self, role: str, ti3_path: Optional[str],
                          patches: Sequence[tuple[int, int, int]],
                          ndjson_path: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Compare start-vs-end saturation-sweep bookends before RBF aggregation.

        The bookends serve two jobs: repeated reads become high-confidence RBF knots, but
        the start/end split is also a temporal drift witness. This helper consumes the
        ordered measured rows while that temporal information still exists, emits a digest
        packet/anomaly for the LLM, and only then downstream optimization may average the
        duplicates by signal.

        The witness is the MAIN-PASS read of each bookend patch (from the stage ndjson) when one
        exists: an appended re-measure overwrites the .ti3 row with a drain-time read, and the more
        drift episodes a stage had, the more of BOTH bookends become drain-time reads that agree
        with each other — hiding exactly the movement the witness exists to show (BenQ 2026-09-27
        verify: 51/84 start and 24/84 end bookend patches re-measured; true max 0.219 vs 0.189 from
        the .ti3). ``remeasured_bookend_patches`` counts them.
        """
        if role not in ("post-mhc", "verify") or not ti3_path:
            return None
        expected = _saturation_sweep_bookend(
            self.patch_sizes, self._transfer(), max_cv=self._patch_max_cv())
        span = len(expected)
        if span <= 0:
            return None
        stage = f"measure:{role}"
        patch_list = list(patches)

        def unavailable(reason: str, **extra: Any) -> dict[str, Any]:
            summary = {"available": False, "role": role, "reason": reason,
                       "bookend_patch_count": span, **extra}
            self._last_bookend_drift = summary
            if self.runlog is not None:
                self.runlog.emit("INFO", stage, "bookend_drift_qc", tier="digest", **summary)
            return summary

        if len(patch_list) < 2 * span:
            return unavailable("patch_sequence_too_short", measured_patch_count=len(patch_list))
        if patch_list[:span] != expected or patch_list[-span:] != expected:
            return unavailable("bookend_sequence_mismatch")

        try:
            samples = parse_ti3(Path(ti3_path))
        except Exception as exc:  # noqa: BLE001 - QC telemetry must not break a completed measure
            return unavailable("ti3_parse_failed", error=f"{type(exc).__name__}: {exc}")
        if len(samples) < 2 * span:
            return unavailable("ti3_too_short", ti3_patch_count=len(samples))

        start = samples[:span]
        end = samples[-span:]

        def key(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
            return tuple(round(float(c), 6) for c in rgb)

        start_keys = [key(s.rgb) for s in start]
        end_keys = [key(s.rgb) for s in end]
        if start_keys != end_keys:
            return unavailable("bookend_signal_mismatch")

        # Main-pass reads per patch label. The measure loop labels PLANNED patch i as
        # p{i:0{width}d} with width from the planned count — NOT the .ti3 row index (write_ti3 drops
        # an unusable row, shifting every later row). The bookends are the first/last `span` PLANNED
        # patches; each witness read must also carry the planned patch's code values, else the
        # .ti3 row stands.
        n_planned = len(patch_list)
        width = max(4, len(str(max(0, n_planned - 1))))
        main_reads, remeasured = _main_pass_reads(ndjson_path)
        bookend_idx = list(range(span)) + list(range(n_planned - span, n_planned))
        remeasured_count = sum(1 for i in bookend_idx if f"p{i:0{width}d}" in remeasured)
        used_main = {"n": 0}

        def witness_xyz(i: int, row) -> tuple[float, float, float]:
            reads = main_reads.get(f"p{i:0{width}d}")
            want = [int(c) for c in patch_list[i]]
            reads = [xyz for xyz, rgb in (reads or []) if rgb is None or list(rgb) == want]
            if not reads:
                return row.xyz
            used_main["n"] += 1
            if len(reads) >= 3:      # robust: a glitch the loop rejected must not move the witness
                arr = np.median(np.asarray(reads, dtype=float), axis=0)
            else:
                arr = np.asarray(reads, dtype=float).mean(axis=0)
            return (float(arr[0]), float(arr[1]), float(arr[2]))

        groups: dict[tuple[float, float, float], dict[str, list[tuple[float, float, float]]]] = {}
        order: list[tuple[float, float, float]] = []
        for j, (s0, s1) in enumerate(zip(start, end)):
            k = key(s0.rgb)
            if k not in groups:
                groups[k] = {"start": [], "end": []}
                order.append(k)
            groups[k]["start"].append(witness_xyz(j, s0))
            groups[k]["end"].append(witness_xyz(n_planned - span + j, s1))
        witness_source = ("main_pass" if used_main["n"] == 2 * span
                          else "mixed" if used_main["n"] else "ti3")

        def mean_xyz(vals: Sequence[tuple[float, float, float]]) -> tuple[float, float, float]:
            arr = np.asarray(vals, dtype=float)
            m = arr.mean(axis=0)
            return (float(m[0]), float(m[1]), float(m[2]))

        spec = self._spec()
        if spec.is_hdr:
            from .engine.model import TargetSpace, de_itp
            space = TargetSpace(self._engine_target())

            def delta_de(a: tuple[float, float, float],
                         b: tuple[float, float, float]) -> float:
                ictcp = space.xyz_to_ictcp(np.asarray([a, b], dtype=float))
                return float(de_itp(ictcp[1] - ictcp[0]))

            metric_name = "dE_ITP"
        else:
            wx, wy = self._white_xy()
            ref_y = max([s.xyz[1] for s in start + end if np.isfinite(s.xyz[1])] or [1.0])
            ref_white = white_xyz(max(ref_y, 1e-6), wx, wy)

            def delta_de(a: tuple[float, float, float],
                         b: tuple[float, float, float]) -> float:
                return float(delta_e2000(xyz_to_lab(a, ref_white), xyz_to_lab(b, ref_white)))

            metric_name = "CIEDE2000"

        per_signal: list[dict[str, Any]] = []
        for k in order:
            start_xyz = mean_xyz(groups[k]["start"])
            end_xyz = mean_xyz(groups[k]["end"])
            d = delta_de(start_xyz, end_xyz)
            per_signal.append({
                "signal": [round(c, 6) for c in k],
                "delta_de": round(d, 4),
                "start_reads": len(groups[k]["start"]),
                "end_reads": len(groups[k]["end"]),
                "start_Y": round(start_xyz[1], 4),
                "end_Y": round(end_xyz[1], 4),
                "delta_Y": round(end_xyz[1] - start_xyz[1], 4),
            })

        deltas = [float(p["delta_de"]) for p in per_signal]
        worst = sorted(per_signal, key=lambda p: p["delta_de"], reverse=True)[:8]
        max_delta = max(deltas) if deltas else 0.0
        summary = {
            "available": True,
            "role": role,
            "metric": metric_name,
            "threshold": _BOOKEND_DRIFT_ANOMALY_DE,
            "bookend_locations": 2,
            "repeats_per_location": int(self.patch_sizes.saturation_sweep_repeats),
            "bookend_patch_count": span,
            "witness_source": witness_source,
            "remeasured_bookend_patches": remeasured_count,
            "unique_signals": len(per_signal),
            "mean_delta_de": round(float(sum(deltas) / len(deltas)), 4) if deltas else 0.0,
            "p95_delta_de": round(float(percentile(deltas, 95.0)), 4) if deltas else 0.0,
            "max_delta_de": round(float(max_delta), 4),
            "worst": worst,
            "per_signal": per_signal,
        }
        self._last_bookend_drift = summary
        if self.runlog is not None:
            self.runlog.emit("INFO", stage, "bookend_drift_qc", tier="digest", **summary)
            if max_delta > _BOOKEND_DRIFT_ANOMALY_DE:
                self.runlog.anomaly(
                    stage, kind="bookend_drift", role=role, metric=metric_name,
                    threshold=_BOOKEND_DRIFT_ANOMALY_DE,
                    max_delta_de=round(float(max_delta), 4),
                    worst=worst,
                    witness_source=witness_source,
                    remeasured_bookend_patches=remeasured_count,
                    message=("start/end saturation-sweep bookends drifted beyond the "
                             f"{_BOOKEND_DRIFT_ANOMALY_DE:g} {metric_name} threshold "
                             f"({witness_source} reads)"))
        return summary

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

    def _probe_fn(self, *, attempt: Optional[int] = None) -> ProbeFn:
        """The re-measure probe for the correction machine. Injected in tests;
        otherwise present each driven signal (code values, no LUT) and read it via
        the measure seam — the fidelity-ladder tier-2 path.

        Every probed DRIVE is appended to the run's probe ledger (``measurements/build_probes.ndjson``,
        per read so a resumed build keeps them), tagged with the build ``attempt``: the probe reads
        are folded into the cube's training, so the verify's held-out classification (V1,
        :mod:`dlc.verify_holdout`) must know them — and only the LIVE attempt's (a re-run build starts
        its training afresh from the post-MHC set; superseded attempts' rows stay as evidence)."""
        ledger = self.ctx.root / "measurements" / verify_holdout.PROBES_FILE
        tag = {"attempt": attempt} if attempt is not None else {}
        if self._probe is not None:
            injected = self._probe
            levels = self._transfer().max_cv

            def recorded(signals: np.ndarray) -> np.ndarray:
                out = injected(signals)
                try:
                    verify_holdout.append_probe_drives(
                        ledger, verify_holdout.to_codes(signals, levels).tolist(), **tag)
                except OSError:   # evidence only — a ledger write never breaks the build
                    pass
                return out

            return recorded
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
                try:   # the held-out ledger (V1): this drive's read may be folded into training
                    verify_holdout.append_probe_drives(ledger, [rgb], **tag, **{"pass": batch["n"]})
                except OSError:   # evidence only — a ledger write never breaks the build
                    pass
                # Drive the dashboard's progress bar DURING the build — it would otherwise sit
                # frozen at the post-MHC count for the whole (longest) stage. Progress-driven,
                # restarting each outer pass so the bar visibly pulses = clearly alive.
                self.runlog.progress("build-install-3dlut", patches_done=i + 1,
                                     patches_total=total, iteration=batch["n"])
                # NO-DARK-WINDOW rule (fable Phase 8): a single probe pass can run for the
                # better part of an hour, and the between-iterations check-in alone left it
                # digest-dark. Tick the §12 clock per read (cheap early-return until due).
                self._maybe_timed_checkin("build-install-3dlut")
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
            fresh_rows = getattr(result, "fresh_rows", None)
            if fresh_rows is not None and getattr(result, "reused_probes", 0):
                # Stream tier: which batch rows were metered (the patch_read seq order) vs answered from
                # the exact-code cache — what an offline replay needs to rebuild the pass.
                self.runlog.emit("INFO", "build-install-3dlut", "probe_reuse", tier="stream",
                                 iteration=data.get("iteration"), fresh_rows=fresh_rows,
                                 probed=data.get("probed_patches"), reused=data.get("reused_probes"))
            # The optimizer is the long pole (it can run for hours). Emit a §12 evidence packet
            # between iterations so a multi-hour optimize never goes dark for the LLM.
            self._maybe_timed_checkin("build-install-3dlut")
        except Exception:  # noqa: BLE001 - telemetry must never break the build
            # …but a persistent failure here silences BOTH the optimizer convergence events and
            # the timed check-ins for the run's longest stage — log the first traceback so a
            # dark build is diagnosable (workflow.log), then stay quiet (no per-iteration spam).
            if not getattr(self, "_optimizer_telemetry_failed", False):
                self._optimizer_telemetry_failed = True
                import traceback
                try:
                    self.ctx.log("optimizer telemetry failed (build continues; convergence events "
                                 "and check-ins may be missing for this stage):\n"
                                 + traceback.format_exc())
                except Exception:  # noqa: BLE001 - the fallback logger must not raise
                    pass

    # -- §12 timed check-in (NON-BLOCKING evidence packet) ------------------
    # -- §12 timed check-ins — assembly lives in dlc.checkin (fable Phase 8, R2) ---------
    # The check-in STATE (window clock, tally snapshot, events byte offset, latest-metric
    # snapshots) stays on this orchestrator where the stages that feed it live; the packet
    # assembly + the DESIGN LAW (emit-only, never a gate) moved to dlc/checkin.py. These
    # delegators keep every call site + test name stable.
    def _maybe_timed_checkin(self, trigger: str) -> None:
        checkin.maybe_timed_checkin(self, trigger)

    def _emit_checkin(self, trigger: str, kind: str) -> None:
        checkin.emit_checkin(self, trigger, kind)

    @property
    def _last_checkin_monotonic(self) -> Optional[float]:
        return self._checkin_window.monotonic

    @_last_checkin_monotonic.setter
    def _last_checkin_monotonic(self, value: Optional[float]) -> None:
        self._checkin_window.monotonic = value

    @property
    def _last_checkin_tally(self) -> dict[str, int]:
        return self._checkin_window.tally

    @_last_checkin_tally.setter
    def _last_checkin_tally(self, value: dict[str, int]) -> None:
        self._checkin_window.tally = value

    @property
    def _last_checkin_pos(self) -> int:
        return self._checkin_window.pos or 0

    @_last_checkin_pos.setter
    def _last_checkin_pos(self, value: int) -> None:
        self._checkin_window.pos = value

    def _checkin_digest(self, trigger: str, *, seq: int = 0,
                        elapsed_since_checkin_s: float = 0.0) -> dict[str, Any]:
        return checkin.checkin_digest(self, trigger, seq=seq,
                                      elapsed_since_checkin_s=elapsed_since_checkin_s)

    def _events_size(self) -> int:
        return checkin.events_size(self)

    def _checkin_evidence(self) -> dict[str, Any]:
        return checkin.checkin_evidence(self)

    def _run_overview(self, trigger: str) -> dict[str, Any]:
        return checkin.run_overview(self, trigger)

    def _events_since_last_checkin(self) -> dict[str, int]:
        return checkin.events_since_last_checkin(self)

    def _latest_checkin_metrics(self) -> dict[str, Any]:
        return checkin.latest_checkin_metrics(self)

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

    def _transport_tell(self, link_bpc: Optional[int] = None) -> dict[str, Any]:
        """Advisory (never a gate): a 3D-LUT flow measured below the panel's bit depth, or on a
        local-dimming panel without a fullscreen patch, risks contaminated VOLUMETRIC reads —
        the very data the 3D LUT is built from. The orchestrator can't see the presenter
        transport (wired in the CLI) but it knows the run's bit depth + the panel, so it surfaces
        the risk + the fix: an ACM/FP16 SDR scanout is 10-bit-live (an 8-bit windowed read
        under-samples it), and mini-LED local dimming contaminates a non-fullscreen patch.
        ``link_bpc`` (the MEASURED live link depth, when known) replaces the profile's
        ``panel.bit_depth`` — an 8 bpc link is not under-sampled by 8-bit patterns."""
        flow = self.calib.get("flow")
        if flow not in ("full", "3dlut-only") or self.mode != "SDR":
            return {"checked": False, "reason": "not an SDR 3D-LUT flow"}
        panel = self.display.panel
        panel_bits = int(link_bpc) if link_bpc else (panel.bit_depth or 8)
        tech = (panel.tech or "").lower()
        local_dimming = bool(panel.backlight_zones) or any(t in tech for t in ("mini", "fald", "local"))
        guard: dict[str, Any] = {"checked": True, "bit_depth": self.bit_depth,
                                 "panel_bit_depth": panel_bits,
                                 "panel_bit_depth_source": "link" if link_bpc else "profile",
                                 "local_dimming": local_dimming}
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

    def _link_depth_check(self) -> dict[str, Any]:
        """Measure the LIVE link format (bpc + encoding) of this run's monitor and list where it
        disagrees with ``--bit-depth`` (the patterns / dogegen daemon) and the profile's
        ``panel.bit_depth``. Mechanical detection only (:mod:`dlc.link_format`); a disagreement is
        the ``preflight:link-depth`` seam, never an auto-fix. Unmeasurable (pipe down, old build
        with no local probe, no matching path) ⇒ ``checked=False`` — a tell, never a false seam."""
        from .link_format import assess_link_depth, link_format_for_monitor

        profile_bits = getattr(self.display.panel, "bit_depth", None)
        try:
            monitors = (self.controller.query_monitors() or {}).get("monitors") or []
            entry = next((m for m in monitors if m.get("index") == self.monitor), None)
            link = link_format_for_monitor(entry, self.link_probe)
        except Exception as exc:  # noqa: BLE001 - unmeasured ⇒ tell, never a false seam
            link = {"bpc": None, "encoding": None, "source": None,
                    "reason": f"{type(exc).__name__}: {exc}"}
        return assess_link_depth(link, run_bits=int(self.bit_depth),
                                 profile_bits=(int(profile_bits) if profile_bits else None),
                                 mode=self.mode)

    def _resolve_output_depth(self, link_depth: Mapping[str, Any],
                              decision: Optional[str]) -> dict[str, Any]:
        """The OUTPUT precision a channel lands on (the refine's per-level quantization floor and
        the SDR white-band code margin): the measured link depth when known — unless the
        ``preflight:link-depth`` seam chose ``use-profile`` — else the profile's
        ``panel.bit_depth``. Persisted in ``calib['output_depth']`` with its provenance."""
        profile_bits = getattr(self.display.panel, "bit_depth", None)
        link_bpc = link_depth.get("link_bpc")
        if link_bpc and decision != "use-profile":
            if profile_bits and int(profile_bits) < int(link_bpc):
                # a wider wire cannot add precision the panel lacks: min(link, panel)
                return {"bits": int(profile_bits), "source": "profile (narrower than the link)",
                        "link_bpc": link_bpc, "profile_bit_depth": profile_bits, "decision": decision}
            return {"bits": int(link_bpc), "source": "link", "link_bpc": link_bpc,
                    "profile_bit_depth": profile_bits, "decision": decision}
        return {"bits": int(profile_bits or 10),
                "source": "profile" if decision == "use-profile" else "profile (link unmeasured)",
                "link_bpc": link_bpc, "profile_bit_depth": profile_bits, "decision": decision}

    def _output_bits(self) -> int:
        """The output bit depth resolved at preflight (``calib['output_depth']``); a Calibration
        that never ran preflight falls back to the profile's ``panel.bit_depth`` (else 10)."""
        rec = self.calib.get("output_depth") or {}
        if rec.get("bits"):
            return int(rec["bits"])
        return int(getattr(getattr(self.display, "panel", None), "bit_depth", None) or 10)

    def _target_colorspace(self) -> Optional[str]:
        """The target colour space for this run, resilient to preflight running BEFORE
        resolve-target sets ``target_name`` (fall back to the display's per-mode target)."""
        name = self.target_name or self.display.target_name(self.content_mode)
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
                                "degenerate": cov.get("degenerate", False),
                                "native_primaries": native}
        if cov.get("degenerate"):
            # A collinear/point native triangle is a CORRUPT characterization, never a real
            # panel — without this branch the all-unreachable result below would read as
            # "target outside the panel's gamut", sending the operator to gamut-map a panel
            # whose measurement is simply broken. Say what it is: re-characterize.
            tell["warning"] = (
                "the stored native primaries are DEGENERATE (collinear/coincident — a corrupt "
                "or botched characterization, not a real panel gamut). Coverage cannot be "
                "assessed; re-run `--flow characterize` before trusting any gamut decision.")
            return tell
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
            spec = self.profile.target(self.target_name or self.display.target_name(self.content_mode))
            is_hdr = spec.is_hdr
            # HDR target = the resolved MAX-SUSTAINED peak (already clamped to the native ceiling),
            # NOT the profile's viewing peak_luminance_nits (owner 2026-06-24, Task C — that moved to
            # DesktopLUT's tonemap). So this headroom tell never fires a spurious "roll off to native"
            # in the normal case; it only speaks if a pinned override somehow exceeds the panel. SDR =
            # the OSD-set white luminance.
            target_nits = self._hdr_target().peak_nits if is_hdr else spec.luminance_nits
        except (KeyError, AttributeError, ValueError):
            target_nits, is_hdr = None, (self.content_mode == "HDR")
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
            # Surface each preflight sub-step on the spine (digest tier) so the dashboard shows what
            # this read-only readiness stage is actually doing, not just a silent start→done.
            self.runlog.note("preflight", "checking display topology + the DesktopLUT pipe")
            # Verify the profile's display mapping against what the controller sees.
            mapping_ok = True
            pipe_ok = True
            pipe_error: Optional[str] = None
            seen_monitors: list[int] = []
            contract_mismatch: Optional[str] = None
            try:
                state = self.controller.state()
                # Wire-contract version check (fable Phase 9): the server MAY advertise
                # contract_version in state.get (absent = pre-versioning v1 build). A
                # mismatch surfaces HERE as "update DLC/DesktopLUT", not as a cryptic
                # `unknown method` failure mid-run. Tell-only: the LLM/operator decides.
                contract_mismatch = contract_version_mismatch(state)
                if contract_mismatch:
                    self.ctx.log(f"pipe contract mismatch: {contract_mismatch}")
                for key in (state.get("mhc") or {}).keys():
                    seen_monitors.append(int(str(key).split(":")[0]))
                for key in (state.get("runtime") or {}).keys():
                    seen_monitors.append(int(str(key).split(":")[0]))
                mapping_ok = (not seen_monitors) or (self.monitor in set(seen_monitors))
            except Exception as exc:  # noqa: BLE001 - surfaced in the digest + the pipe seam below
                pipe_error = f"{type(exc).__name__}: {exc}"
                state = {"error": pipe_error}
                mapping_ok = False
                pipe_ok = False
            # The persistent per-display store supplies the correction's real build
            # date when present (a refresh recorded since the profile was written),
            # so staleness ages from when the correction was actually made (§10).
            corr_store = self._correction_store()
            # Consult the SAME correction the meter is actually wired to (this mode's store slot
            # overrides the profile YAML — resolve_correction), not the (possibly empty) profile
            # YAML, so the tell can't report "no correction" while the meter is in fact corrected.
            # The store's build date only applies when the store's file is the one in use.
            corr_res = resolve_correction(self.profile, corr_store, self.display.name, self.mode)
            store_rec = corr_store.get(self.display.name, self.mode)
            store_made = (store_rec.correction_made
                          if store_rec and corr_res.source == "store" else None)
            if corr_res.warning:
                self.ctx.log(corr_res.warning)
            self.runlog.note("preflight", "checking colorimeter correction (CCMX/SPD) freshness")
            staleness = self.profile.correction_staleness(
                today=self.run_date, made_override=store_made, file_override=corr_res.file)
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
            sdr_white_level = self._probe_sdr_white_level(patch_window) if self._sdr_in_hdr() else None
            if patch_window.get("mode_warning"):
                self.ctx.log(patch_window["mode_warning"])
            # LIVE link format (bpc + encoding) vs --bit-depth and the profile's panel.bit_depth —
            # measured, never assumed; a disagreement is the preflight:link-depth seam below.
            self.runlog.note("preflight", "reading the live display-link bit depth + encoding")
            link_depth = self._link_depth_check()
            if link_depth.get("mismatch"):
                self.ctx.log("link depth mismatch: "
                             + "; ".join(r["detail"] for r in link_depth.get("reasons", [])))
            elif not link_depth.get("checked"):
                self.ctx.log(f"live link depth unmeasured ({link_depth.get('reason')}) — the output "
                             f"quantization floor falls back to the profile's panel.bit_depth")
            # Measurement-transport adequacy for 3D-LUT flows (advisory): bit depth + panel.
            transport = self._transport_tell(link_depth.get("link_bpc"))
            if transport.get("warning"):
                self.ctx.log(transport["warning"])
            # Panel-capability tells from the DIP (advisory, never gates): does the measured
            # native gamut cover the target, and do native white/black/contrast fit it? These
            # consume the DIP's display axis (native primaries / white / black) up front, so an
            # unreachable target gamut or a raised black is surfaced before the build, not in the
            # cube residuals afterward.
            self.runlog.note("preflight", "probing panel capabilities (gamut coverage, white/black, contrast)")
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
            # Store HEALTH (fable Phase 8, from the Phase 3 lead): the stores carry .corrupt
            # (file present but unparseable) and .dropped (individual records lost to schema
            # drift / hand-editing) but nothing outside tests consumed either — so "your DIP
            # was silently dropped, this run measures single-read" was invisible. Surface both
            # in the preflight tell; decision-relevant, never a gate (the stores are tolerant
            # by design).
            dip_store = self._dip_store()
            store_health = {
                "correction_store": {"corrupt": corr_store.corrupt,
                                     "dropped": list(corr_store.dropped),
                                     # schema-1 records whose mode slot was inferred, not recorded
                                     "mode_inferred": corr_store.mode_inferences()},
                "dip_store": {"corrupt": dip_store.corrupt,
                              "dropped": list(dip_store.dropped)},
            }
            for note in store_health["correction_store"]["mode_inferred"]:
                self.ctx.log(f"correction_store: {note['display']} {note['mode']} slot was inferred "
                             f"from a legacy (per-display) record ({note['basis']}) — "
                             f"{note.get('correction_file')}"
                             + (f"; {note['spd_conflict']}" if note.get("spd_conflict") else ""))
            for store_name, health in store_health.items():
                if health["corrupt"]:
                    self.ctx.log(f"{store_name} file is CORRUPT (unparseable) — running as if "
                                 "empty; re-characterize / rebuild the correction to repopulate, "
                                 "or restore the file from backup.")
                elif health["dropped"]:
                    self.ctx.log(f"{store_name} dropped record(s) {health['dropped']} (schema "
                                 "drift / hand-edit?) — those displays run without their stored "
                                 "profile until refreshed.")
            # Save the user's current DesktopLUT state BEFORE we touch anything, so a
            # failed/cancelled run can be rolled back to exactly this. preflight is the
            # first stage and read-only, so this captures the pristine pre-run setup.
            self.runlog.note("preflight", "saving your current DesktopLUT setup for rollback")
            backup = self._capture_user_backup(state)
            digest = {"monitor": self.monitor, "mode": self.mode, "display": self.display.name,
                      "argyll_display": self.display.argyll_display, "mapping_ok": mapping_ok,
                      "pipe_ok": pipe_ok, "pipe_error": pipe_error,
                      "contract_mismatch": contract_mismatch,
                      "seen_monitors": sorted(set(seen_monitors)),
                      "monitor_map": monitor_map,
                      "correction": staleness.as_dict(),
                      "correction_from_store": corr_res.source == "store",
                      "correction_resolution": corr_res.as_dict(),
                      "patch_window": patch_window,
                      "link_depth": link_depth,
                      "transport": transport,
                      "gamut": gamut_tell,
                      "panel_limits": panel_limits,
                      "dip": dip_status,
                      "store_health": store_health,
                      "backup": backup}
            if self._sdr_in_hdr():
                digest["content_mode"] = self.content_mode
                digest["sdr_white_level"] = sdr_white_level
            return StageOutcome("preflight", "done", digest=digest,
                                data={"stale": staleness.stale, "mapping_ok": mapping_ok})

        outcome = self._stage("preflight", run)
        # A dead-pipe preflight must NOT stay memoised (adversarial finding F7a-A1/A2): if it did,
        # a resume after the operator fixes the pipe would replay the "done" record with the stale
        # pipe_ok:false digest — re-firing this seam with a now-false question AND never re-running
        # _capture_user_backup (callable only inside the memoised stage), permanently losing the
        # durable rollback for a run whose pipe is healthy from enter-neutral on. Drop the memo so
        # every invocation re-probes the live pipe and re-attempts the backup until it succeeds;
        # once the pipe is up, preflight memoises normally.
        if outcome.digest.get("pipe_ok") is False:
            self.calib["stages"].pop("preflight", None)
            self._save()
        # Dead pipe (fable Phase 7a, owner-approved early fail): every flow except build-correction
        # needs the pipe for something load-bearing (enter-neutral/install/require-stack/clear-native
        # before a DIP), and without it the run dies one stage later with a raw exception AND no
        # usable rollback backup. Abort here, where nothing has been measured or mutated, unless a
        # judge knows better (e.g. the pipe is momentarily restarting). build-correction is exempt
        # by design — it is deliberately pipe-optional (operator can hold the panel at native).
        if outcome.digest.get("pipe_ok") is False and self.calib.get("flow") != "build-correction":
            flow = self.calib.get("flow")
            # Flow-accurate reason (finding F7a-A6): characterize drives the panel to native over
            # the pipe and restores it (it does not install/enter-neutral); the calibrating flows
            # enter neutral / install / roll back. Both are load-bearing and both lose the durable
            # backup without the pipe.
            need = ("cannot clear the panel to native for a valid characterization, or restore "
                    "your setup afterwards" if flow == "characterize" else
                    "cannot enter neutral, install a correction, or roll back")
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="preflight:pipe", seam=SEAM_PIPE, stage="preflight",
                question=(f"the DesktopLUT calibration pipe is unreachable "
                          f"({outcome.digest.get('pipe_error')}) — this {flow} flow {need} without "
                          "it, and no pre-run backup could be captured. Abort (start DesktopLUT / "
                          "arm the pipe first), or proceed?"),
                options=("abort", "proceed"), recommendation="abort",
                digest={"pipe_error": outcome.digest.get("pipe_error"),
                        "flow": flow,
                        "backup": outcome.digest.get("backup")})),
                stage="preflight", message="aborted — DesktopLUT pipe unreachable at preflight")
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
        # Live link depth vs --bit-depth / profile (2026-09-26: a BenQ profiled 10-bit ran an 8 bpc
        # HDMI link and nothing caught it). Which depth is right — relaunch at the link's depth, trust
        # the link for the output floor, or trust the profile — is a judgment, never an auto-fix.
        # build-correction is exempt: a spectral correction build is bit-depth-agnostic.
        link_depth = outcome.digest.get("link_depth") or {}
        link_choice: Optional[str] = None
        if link_depth.get("mismatch") and self.calib.get("flow") != "build-correction":
            bpc = link_depth.get("link_bpc")
            prof = link_depth.get("profile_bit_depth")
            fix = ((f"relaunch the run AND the dogegen daemon at --bit-depth {min(int(bpc), 10)} "
                    f"(dogegen takes 8 or 10) and/or " if link_depth.get("relaunch_needed") else "")
                   + "fix the link (cable / bandwidth / refresh / GPU colour format) or correct "
                     "panel.bit_depth")
            link_choice = self._abort_if(self.adjudicate(AdjudicationRequest(
                key="preflight:link-depth", seam=SEAM_LINK_DEPTH, stage="preflight",
                question=(
                    f"monitor {self.monitor}'s live link is {bpc} bpc {link_depth.get('link_encoding')}"
                    f" ({link_depth.get('link_connector') or 'connector unknown'}; read via "
                    f"{link_depth.get('link_source')}), but "
                    + "; ".join(r["detail"] for r in link_depth.get("reasons", []))
                    + f". abort = stop now (nothing measured) and {fix}; use-link = proceed with the "
                    f"output-quantization floor at the measured {bpc} bits; use-profile = proceed "
                    f"with it at the profile's {prof} bits (the link reading is wrong or irrelevant). "
                    f"Neither proceed option changes the pattern depth (--bit-depth "
                    f"{link_depth.get('run_bit_depth')}) — it is fixed for the run."),
                options=("abort", "use-link", "use-profile"),
                recommendation=link_depth.get("suggested", "abort"),
                digest=dict(link_depth))),
                stage="preflight", message="aborted on a live link-depth mismatch").choice
        self.calib["output_depth"] = self._resolve_output_depth(link_depth, link_choice)
        self._save()
        # A failed pre-run backup means a failed/cancelled run may have NO durable rollback
        # (the in-memory C++ snapshot is the live net, but it dies with DesktopLUT). That is a
        # judgment call, not a log line (fable Phase 7a, from the BLE001 sweep): recommend
        # proceed (the snapshot usually suffices) but flag compromised so a supervised run
        # escalates and a live judge decides whether to run un-backed-up.
        # (Gated on pipe_ok: a dead pipe already surfaced the missing backup in ITS seam above —
        # one cause must not pause the run twice.)
        backup = outcome.digest.get("backup") or {}
        if backup and not backup.get("captured") and outcome.digest.get("pipe_ok") is not False:
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="preflight:backup", seam=SEAM_BACKUP, stage="preflight",
                question=(f"the pre-run DesktopLUT settings backup could not be captured "
                          f"({backup.get('error', 'unknown error')}) — a failed run would have no "
                          "durable rollback beyond the in-memory snapshot. Proceed without a "
                          "backup, or abort and fix (paths.desktoplut_ini / pipe)?"),
                options=("proceed", "abort"), recommendation="proceed",
                digest={**backup, "compromised": True})),
                stage="preflight", message="aborted — pre-run settings backup could not be captured")
        # Which correction the meter is wired to is a judgment whenever it isn't this mode's own
        # recorded slot: a raw fallback, a borrowed profile-YAML file, or a legacy guess used to
        # surface only as a log line — the 2026-09-26 store upgrade left the PA32UCXR with no HDR
        # slot, and the next HDR run would have metered RAW with nothing pausing it.
        corr_question = self._correction_resolution_question(outcome.digest)
        if corr_question:
            question, recommendation, reason = corr_question
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="preflight:correction", seam=SEAM_CORRECTION, stage="preflight",
                question=question, options=("abort", "proceed"), recommendation=recommendation,
                digest={"reason": reason,
                        "resolution": outcome.digest.get("correction_resolution"),
                        "correction": outcome.digest.get("correction"),
                        "compromised": recommendation == "abort"})),
                stage="preflight", message="aborted on the colorimeter correction for this mode")
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

    def _correction_resolution_question(self, digest: Mapping[str, Any]
                                        ) -> Optional[tuple[str, str, str]]:
        """``(question, recommendation, reason)`` when the preflight's correction resolution needs
        a judge, else ``None``. Only this mode's own recorded store slot — or the profile-YAML file
        on a rig where this is the only display on record (the per-meter file can only be meant for
        it) — is mechanical; everything else is evidence for the LLM:

        * ``missing_file`` — the resolved file is gone from where the meter looks → RAW reads (abort);
        * ``raw_other_mode`` — no correction for this mode while another mode has one → RAW (abort);
        * ``raw_none`` — no correction anywhere for this display → RAW (proceed; build one first?);
        * ``profile_other_mode`` — the profile YAML file stands in while another mode has a slot;
        * ``profile_cross_display`` — the per-meter profile YAML file stands in for a display with no
          recorded correction while other displays are on record: it may be another panel's CCMX/CCSS
          (abort when the store records that very file for another display, else proceed);
        * ``legacy_inferred`` — this mode's slot was only inferred from a legacy schema-1 file.

        build-correction is exempt: it is the flow that mints the correction."""
        if self.calib.get("flow") == "build-correction":
            return None
        res = digest.get("correction_resolution") or {}
        tell = digest.get("correction") or {}
        mode, name = res.get("mode") or self.mode, self.display.name
        others = ", ".join(res.get("other_modes") or ())
        file = res.get("file")
        build = f"`--flow build-correction --mode {mode}`"
        if file and tell.get("present") is False:
            return (f"{tell.get('message') or f'correction {file} is missing on disk'} Abort and restore "
                    f"or rebuild it, or proceed on RAW meter readings?", "abort", "missing_file")
        if res.get("source") == "none":
            if others:
                return (f"no {mode} colorimeter correction is recorded for {name} (the store holds "
                        f"{others} only, which a {mode} run never borrows) and the profile YAML "
                        f"names none — this run would meter RAW. Abort and record/build the {mode} "
                        f"correction ({build}), or proceed raw?", "abort", "raw_other_mode")
            return (f"no colorimeter correction exists for {name} — this run meters RAW (a mini-LED/"
                    f"QD/OLED panel's primaries read wrong without one). Proceed raw, or abort and "
                    f"build one first ({build})?", "proceed", "raw_none")
        if res.get("source") == "profile" and others:
            return (f"no {mode} correction is recorded for {name}; the profile YAML's "
                    f"{Path(str(file)).name} stands in (the store's {others} correction is not used). "
                    f"Proceed only if that file is a {mode} correction for this panel.",
                    "proceed", "profile_other_mode")
        cross = res.get("cross_display") if res.get("source") == "profile" else None
        if cross:
            fname = Path(str(file)).name
            on_record = sorted(set(cross.get("profile_displays") or ()) | set(cross.get("store_displays") or ()))
            recorded_for = list(cross.get("recorded_for") or ())
            return (f"no colorimeter correction is recorded for {name} (in any mode); the meter would use "
                    f"the profile YAML's meter-level {fname} (meter.correction.file), which is NOT this "
                    f"display's own recorded correction — the rig has other displays on record "
                    f"({', '.join(on_record)})"
                    + (f" and the correction store records {fname} as the correction of "
                       f"{', '.join(recorded_for)}" if recorded_for else "")
                    + f". A CCMX/CCSS describes one panel's spectra: another panel's skews every read of "
                    f"this run. abort = stop now (nothing measured) and build this display's own "
                    f"({build}); proceed = meter through {fname}, only if it was made for {name} in {mode}.",
                    "abort" if recorded_for else "proceed", "profile_cross_display")
        if res.get("source") == "store" and str(res.get("mode_source") or "").startswith("legacy"):
            return (f"{res.get('warning') or f'the {mode} correction was assigned by legacy inference'} "
                    f"Proceed if {Path(str(file)).name} is the {mode} correction, or abort and "
                    f"record it ({build}).", "proceed", "legacy_inferred")
        return None

    def _reject_mode_target_mismatch(self, stage: str, target: str, spec: cp.TargetSpec) -> None:
        """P12 guard (fable Phase 7a): the run MODE and the resolved target's transfer must
        agree. The refine fork in ``_flow_full``/``_flow_mhc_only`` switches on ``spec.is_hdr``
        while ``_planned_stages`` (the dashboard stepper) and ``_reachable_primaries`` switch on
        ``self.mode`` — a profile that maps a display's SDR slot to a PQ target (or vice versa)
        would otherwise run an incoherent hybrid (HDR refine + SDR gamut clamp, a stepper showing
        the other mode's stages) with nothing surfacing why. Reject it loudly at resolve time —
        the two predicates are then provably interchangeable for the rest of the run."""
        if spec.is_hdr != (self.content_mode == "HDR"):
            cm = self.content_mode
            raise CalibrationAborted(StageOutcome(
                stage, "aborted",
                digest={"message": (
                    f"target {target!r} is a {'PQ/HDR' if spec.is_hdr else 'power-law/SDR'} target "
                    f"but the run's content mode is {cm} — the profile maps display {self.monitor}'s "
                    f"{cm} slot ({'hdr_target' if cm == 'HDR' else 'sdr_target'}) to a "
                    f"mismatched target. Fix the profile before running."),
                    "target": target, "target_is_hdr": spec.is_hdr, "run_mode": self.mode,
                    "content_mode": cm}))

    def stage_resolve_target(self) -> StageOutcome:
        # This stage owns its own adjudication (the plan seam) and so bypasses _stage —
        # announce it on the spine directly so the dashboard phase header still tracks it.
        self.runlog.set_phase("resolve-target")
        self.runlog.stage_start("resolve-target")
        target = self.display.target_name(self.content_mode)
        if not target:
            raise CalibrationAborted(StageOutcome(
                "resolve-target", "aborted",
                digest={"message": f"display {self.monitor} has no {self.content_mode} target configured"}))
        spec = self.profile.target(target)
        self._reject_mode_target_mismatch("resolve-target", target, spec)
        self.target_name = target
        self.calib["target"] = target
        self._oog_mapping()           # pin the run's OOG target policy before anything is measured
        self._save()
        # HDR (PQ): resolve the chosen target (peak off the ladder, undershoot gain + knee,
        # fixed white) from the DIP now, so the plan digest reports the real peak and the
        # patch sets are capped to it (docs/hdr-target-design.md). SDR is unaffected.
        hdr = self._hdr_target() if spec.is_hdr else None
        flow = self.calib.get("flow")
        if flow == "verify-only":
            # --verify-patches-file: the HDR patch cap is known only now (the target peak) — refuse a
            # file that drives above it BEFORE the plan seam asks anyone to approve measuring it.
            self._refuse_verify_file_above_cap()
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
        # Keep the re-pin provenance (_pin_hdr_peak_to_cap) across resumes — the fresh record
        # has no memory of WHY the fingerprint moved.
        carried = {k: existing_plan[k] for k in ("repinned_from", "repin_reason")
                   if isinstance(existing_plan, dict) and k in existing_plan}
        self.calib["patch_plan"] = {**carried, **patch_plan, "approved": bool(
            isinstance(existing_plan, dict)
            and existing_plan.get("approved")
            and existing_plan.get("fingerprint") == patch_plan.get("fingerprint")
        )}
        self._save()
        transfer_label = "PQ (ST.2084)" if spec.is_hdr else f"power γ{spec.gamma}"
        target_nits = hdr.peak_nits if hdr else spec.luminance_nits
        nits_label = f"{target_nits:g} nit peak" if spec.is_hdr else f"{target_nits:g} nits nominal"
        sdr_white_evidence: Optional[dict[str, Any]] = None
        if not spec.is_hdr and flow == "3dlut-only":
            # The cube's white is the INSTALLED MHC's refined white (registry, pipe-cross-checked)
            # — the SDR twin of the HDR cap pin below; its absence is a judgment, not a fallback.
            self._installed_stack_evidence()
            white_nits, white_source = self._sdr_calibrated_white()
            sdr_white_evidence = {"white_nits": white_nits, "source": white_source,
                                  "nominal_nits": spec.luminance_nits}
            if white_nits is not None:
                target_nits = white_nits
                nits_label = (f"{white_nits:g} nits white (the installed MHC's refined white; "
                              f"nominal {spec.luminance_nits:g})")
        digest = {"flow": flow, "target": target,
                  "colorspace": spec.colorspace, "transfer": transfer_label,
                  "white": f"{spec.white.intent} ({spec.white.method})",
                  "white_nits": target_nits, "patch_plan": self.calib["patch_plan"]}
        if not spec.is_hdr:
            digest["nominal_white_nits"] = spec.luminance_nits
        if self._preheat_policy():
            digest["preheat"] = self._preheat_policy()
        if self.calib.get("present_stall") == "off":
            digest["present_stall"] = "off"
        if flow == "verify-only":
            # What this measurement-only run will read THROUGH (nothing is built or committed).
            digest["verify_only"] = {"verify_cube": self.calib.get("verify_cube"),
                                     "verify_patches_from": self.calib.get("verify_patches_from"),
                                     "installed_stack": self._installed_stack_evidence(),
                                     "scoring_gamut": self._scoring_gamut_source()}
        plan_warnings: list[str] = []
        listed_digest = (((self.calib.get("stages") or {}).get("verify-patches-file") or {}).get("digest") or {}
                         if flow == "verify-only" else {})
        file_plan: Optional[str] = None
        if listed_digest:
            digest["verify_only"]["verify_patches_file"] = {
                k: listed_digest.get(k) for k in ("file", "n", "patches_fingerprint", "measurement_order",
                                                  "min_reads", "read_rule", "content_class", "weighted",
                                                  "held_out_check")}
            held = listed_digest.get("held_out_check") or {}
            planned = self._file_planned_reads()
            digest["verify_only"]["verify_patches_file"]["planned_reads"] = planned
            file_plan = self._file_plan_text(listed_digest, planned, held)
            if held.get("n_fail"):
                plan_warnings.append(
                    f"{held['n_fail']} of the file's {listed_digest.get('n')} patches sit < "
                    f"{held.get('min_codes')} codes from the installed stack's training signals / probe "
                    f"drives ({held.get('training_run')}) — they are measured and scored (reported, not "
                    "dropped), but their ΔE is partly in-sample")
        if sdr_white_evidence is not None:
            stack = self.calib.get("installed_stack") or {}
            digest["installed_stack"] = stack
            digest["sdr_white"] = sdr_white_evidence
            if sdr_white_evidence["white_nits"] is None:
                plan_warnings.append(
                    "the installed MHC's refined SDR white is UNKNOWN ("
                    + str(stack.get("sdr_white_reason") or "no registry evidence")
                    + f") — the cube falls back to the nominal {spec.luminance_nits:g} nits; if the "
                    "installed refine delivered a dimmer white (e.g. an exact-D65 white accepted "
                    "below the band), every signal above it clips at the top and the refined greys "
                    "are lifted off the MHC. Backfill with `python -m dlc.stack_registry import-run "
                    "--run <applying run> --profile-name <pipe profile>` and restart, or approve knowingly")
        if hdr:
            digest["hdr_target"] = hdr.as_dict()
            # Surface the HdrTarget provenance FLAGS in the seam question itself, not three
            # levels deep in the digest — each one means "re-characterize", and the plan
            # seam is the veto point where that is still cheap (fable Phase 6, Phase 2 lead).
            prov = hdr.provenance or {}
            if (prov.get("undershoot") or {}).get("clamped"):
                plan_warnings.append(
                    "the measured EOTF undershoot implies an implausible boost — the gain was "
                    f"CLAMPED to {hdr.undershoot_gain:.3f}×; suspect characterization, consider "
                    "re-measuring before calibrating to it")
            adopted_peak = (prov.get("peak") or {}).get("source") == "verify_patches_from"
            if adopted_peak:
                # verify-only --verify-patches-from: the peak is DELIBERATELY the one the source
                # scored against (like-for-like); an installed cap that differs was the
                # verify-source seam's business. Grounding warnings are for a build, not here.
                digest["hdr_peak_note"] = (f"HDR peak {hdr.peak_nits:g} nits = the source run's scored "
                                           "peak (adopted for a like-for-like verify)")
            if (prov.get("peak") or {}).get("sustained_unknown") and not adopted_peak:
                plan_warnings.append(
                    "the target peak rests on no warm/sustained capture "
                    f"({(prov.get('peak') or {}).get('source', 'unknown source')}) — a peak the "
                    "panel cannot hold bakes in error; a characterize run grounds it")
            if flow in self._FLOWS_KEEPING_MHC:
                stack = self.calib.get("installed_stack") or {}
                digest["installed_stack"] = stack
                if not stack.get("pin_nits") and not adopted_peak:
                    plan_warnings.append(
                        "the installed MHC's calibrated top is UNKNOWN ("
                        + str(stack.get("reason") or "no registry evidence")
                        + f") — the target peak falls back to {hdr.peak_nits:.0f} nits; if the "
                        "installed MHC caps D65 lower, every patch above its cap reads as a "
                        "plateau the cube cannot lift and the white scores that shortfall. "
                        "Backfill with `python -m dlc.stack_registry import-run --run <applying "
                        "run> --profile-name <pipe profile>` and restart, or approve knowingly")
        if plan_warnings:
            digest["hdr_target_warnings" if hdr else "sdr_white_warnings"] = plan_warnings
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key="resolve-target:plan", seam=SEAM_PLAN, stage="resolve-target",
            question=(f"Plan: {flow} calibration of monitor {self.monitor} "
                      f"({self.display.name}) to target '{target}' "
                      f"({transfer_label}, {spec.white.intent}, {nits_label}) — "
                      + (f"{patch_plan['total_patches']} patches "
                         f"({patch_plan['volumetric_mode']} volumetric, {patch_plan['order']} order). "
                         if file_plan is None else file_plan)
                      + ("".join(f"⚠ {w}. " for w in plan_warnings))
                      + "Proceed?"),
            options=("approve", "abort"), recommendation="approve", digest=digest)),
            stage="resolve-target", message="plan vetoed by the operator")
        self.calib["patch_plan"] = {**carried, **patch_plan, "approved": True}
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
            # Only THIS mode's slot is touched, and its correction is carried from the slot
            # itself — never snapshotted from the profile-YAML fallback (that would mint a
            # store "correction" this mode never built, and pin a later YAML edit out).
            store = self._correction_store()
            prior = store.get(self.display.name, self.mode)
            has_corr = bool(prior and prior.correction_file)
            # verify-only is measurement-only: it resolves the white it scores against but never
            # rewrites the cross-run store (not even its metadata).
            record = store.record if self.calib.get("flow") != "verify-only" else (lambda _rec: None)
            record(CorrectionRecord(
                display=self.display.name, mode=self.mode,
                correction_file=(prior.correction_file if has_corr else None),
                correction_made=(prior.correction_made if has_corr else None),
                spd_file=res.spd_file or (prior.spd_file if prior else None) or self.display.white_spd,
                white_xy=[res.xy[0], res.xy[1]], white_provenance=res.provenance,
                observer=res.observer, anchor=res.anchor, strength=res.strength,
                updated=self.run_date.isoformat(),
                mode_source=(prior.mode_source if prior else None)))
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
        # Written to THIS mode's slot only — ingesting an HDR correction must never replace
        # the SDR one (the schema-1 leak: SDR runs measured through the HDR CCMX for months).
        store = self._correction_store()
        prior = store.get(self.display.name, self.mode)
        store.record(CorrectionRecord(
            display=self.display.name, mode=self.mode, mode_source=MODE_RECORDED,
            correction_file=str(ccmx),
            correction_made=self.run_date.isoformat(),
            spd_file=(str(white_sp) if spd_ok else (prior.spd_file if prior else None)),
            white_xy=(prior.white_xy if prior else None),
            white_provenance=(prior.white_provenance if prior else None),
            observer=(prior.observer if prior else None),
            anchor=(prior.anchor if prior else None),
            strength=(prior.strength if prior else None),
            updated=self.run_date.isoformat()))
        self.ctx.log(f"ingested {self.mode} correction {ccmx.name}"
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
        """Put the calibrated monitor/mode into a TRUE neutral before the first raw read:
        ``calibration.enter`` (the C++ clears WB / GS / tonemap / Desktop Gamma and removes the
        ICM) FOLLOWED BY the association of an identity MHC2 profile through the normal path
        (:meth:`_associate_identity_profile`) — because Windows keeps the LAST associated MHC2
        transform after a removal, ``enter`` alone left every pre-2026-09-03 raw stage
        measuring through the previously applied stack. The digest + ``calib['neutral_profile']``
        carry ``{monitor, mode, P, P_source, profile_name}``; the stage refuses (:class:`StageError`)
        when the association does not land. HDR and SDR alike."""
        def run() -> StageOutcome:
            # Capture the user's viewing layers BEFORE calibration.enter clears them (the C++
            # snapshot restores them only on revert; the apply path used to leave them off).
            self._enter_measurement_layers()
            # Stale-calibration tell (fable Phase 9): a PREVIOUS run died without exiting
            # calibration mode, so its display is already cleared. Probed BEFORE the enter; what
            # it costs is judged AFTER it — a server with the snapshot store keeps the original
            # capture (snapshot_retained), an older one overwrote its single slot, and a stale
            # session on another monitor/mode changes what a restore or a commit does. The shared
            # _common helper owns the words (the stage tools and fald-profile use the same one).
            stale = _common.stale_calibration_session(self.controller)
            # Capture the PRE-ENTER runtime layer map (every mode:monitor pair), persisted in
            # the run record. DesktopLUT builds before 2026-08-14 cleared BOTH modes' runtime
            # layers on this monitor at calibration.enter, and the apply path exits WITHOUT the
            # snapshot restore — so the 2026-08-14 HDR run permanently dropped the user's SDR
            # cube. _commit_calibration re-applies any non-calibrated pair the server dropped;
            # on fixed builds (per-mode clear) that restore is a no-op.
            if self.calib.get("runtime_prior") is None:
                try:
                    state = self.controller.state()
                    self.calib["runtime_prior"] = {
                        "captured": True, "runtime": _jsonable(state.get("runtime") or {})}
                except Exception as exc:  # noqa: BLE001 - advisory; the run must not die here
                    self.calib["runtime_prior"] = {
                        "captured": False, "error": f"{type(exc).__name__}: {exc}"}
                self._save()
            try:
                res = self.controller.enter_neutral(self.monitor, self.mode, self.dummy_icc,
                                                    reason="DLC v2 calibration")
            except Exception:
                failed_tell = _common.note_stale_calibration(None, stale, None,
                                                             monitor=self.monitor, mode=self.mode)
                if failed_tell is not None:
                    self.ctx.log(f"stale calibration session: {failed_tell['detail']}")
                raise
            stale_tell = _common.note_stale_calibration(None, stale, res,
                                                        monitor=self.monitor, mode=self.mode)
            if stale_tell is not None:
                self.ctx.log(f"stale calibration session ({stale_tell['severity']}): {stale_tell['detail']}")
            # calibration.enter cleared the layers + REMOVED the ICM — but Windows keeps the
            # LAST associated MHC2 transform, so the panel is still driven through whatever
            # was applied before (HW-proven 2026-09-03). Associate an IDENTITY profile
            # through the normal path so the raw stages measure the bare panel.
            neutral_profile = self._associate_identity_profile()
            digest: dict[str, Any] = {
                "entered": True, "neutral_profile": neutral_profile,
                # True = the server kept an earlier capture of this display; None = a build that
                # predates the snapshot store (its restore slot is overwritten on every enter).
                "snapshot_retained": res.get("snapshot_retained") if isinstance(res, dict) else None,
            }
            if stale_tell is not None:
                digest["stale_calibration_mode"] = True
                # severity + detail + the stale session's monitor/mode pairs and any mismatch
                # with this run — evidence for the reader, no verdict.
                digest["stale_calibration"] = stale_tell
            return StageOutcome("enter-neutral", "done",
                                digest=digest, data={"raw": _jsonable(res)})
        outcome = self._stage("enter-neutral", run)
        # Did THIS process associate the identity profile (vs a resume replaying the record)?
        # The readiness refusal on "no profile associated" is mechanical only right after a
        # live association; on a resume the pipe's state is re-read and surfaced as evidence.
        self._neutral_associated_live = not outcome.replayed
        return outcome

    def _associate_identity_profile(self) -> dict[str, Any]:
        """Bake + associate an IDENTITY MHC2 profile for this monitor/mode (``set_primaries(P)``
        → ``set_white(D65)`` → ``apply``) so a TRUE neutral replaces the stale transform Windows
        keeps after ``calibration.enter``. ``P`` per :func:`dlc.neutral_audit.identity_primaries`:
        HDR uses the DIP's measured ``native_primaries`` (the C++ sets src = P, so the matrix is
        identity and the 1D LUT is identity with no base LUT staged; HW-verified: the identity
        leg read the native white), Rec.2020 bootstrap without a DIP; SDR MUST push Rec.709
        (the C++ pins src = sRGB — the DIP native there would bake a real gamut matrix).

        Records ``self.calib['neutral_profile']`` = ``{monitor, mode, P, P_source, profile_name}``
        (``profile_name`` read back from ``state()`` after apply, NOT trusted from the apply
        reply). Raises :class:`StageError` when ``state()`` shows no MHC profile for the key
        afterwards — a silent continue here would re-create the very bug this fixes."""
        dip = self._dip()
        native = dip.native_primaries if dip is not None else None
        primaries, source = neutral_audit.identity_primaries(self.mode, native)
        self.controller.set_primaries(self.monitor, self.mode, primaries)
        self.controller.set_white(self.monitor, self.mode, *neutral_audit.D65_XY)
        applied = self.controller.apply_mhc(self.monitor, self.mode)
        key = f"{self.monitor}:{self.mode}"
        try:
            state = self.controller.state() or {}
        except Exception as exc:  # noqa: BLE001 - the association can't be confirmed → refuse
            raise StageError("enter-neutral",
                             f"identity MHC association for {key} could not be confirmed: "
                             f"state.get failed ({type(exc).__name__}: {exc})",
                             identity_primaries=primaries, primaries_source=source) from exc
        entry = (state.get("mhc") or {}).get(key) or {}
        profile_name = entry.get("profile_name")
        if not (profile_name or entry.get("applied") or entry.get("enabled")):
            raise StageError("enter-neutral",
                             f"identity MHC association for {key} did not land: state() shows no "
                             f"MHC profile after mhc.apply (reply: {_jsonable(applied)!r}) — the panel "
                             "is still driven through the last MHC2 transform Windows kept; not neutral",
                             identity_primaries=primaries, primaries_source=source,
                             mhc_entry=_jsonable(entry))
        record = {"monitor": self.monitor, "mode": self.mode,
                  "P": primaries, "P_source": source, "profile_name": profile_name}
        self.calib["neutral_profile"] = record
        self._save()
        self.ctx.log(f"identity MHC associated for {key}: P={source} "
                     f"({'DIP native' if source == 'dip' else 'bootstrap ' + ('Rec.2020' if self.mode == 'HDR' else 'Rec.709')})"
                     f", white=D65, profile={profile_name or '(unnamed)'}")
        return record

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
                # NO-DARK-WINDOW rule (fable Phase 8): characterize reads the panel directly
                # (not via the instrumented measure loop), and its thermal-observation phase
                # can run for hours — it emitted NO check-ins at all. Tick the §12 clock per
                # read here (cheap early-return until due).
                self._maybe_timed_checkin("characterize")
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
                          correction_file=active_correction(self.profile, self._correction_store(),
                                                            self.display.name, self.mode),
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
            if not panel_dark:
                white_xy = ((reading.yxy[1], reading.yxy[2])
                            if reading.yxy is not None and len(reading.yxy) >= 3 else None)
                digest["white_reach"] = self._early_white_reach(nits, white_xy)
            return StageOutcome("brightness", "done", digest=digest,
                                data={"white_nits": nits, "in_range": in_range, "panel_dark": panel_dark})

        outcome = self._stage("brightness", run)
        panel_dark = bool(outcome.data.get("panel_dark"))
        if panel_dark:
            self.runlog.anomaly(
                "brightness", panel_dark=True, white_nits=outcome.digest.get("white_nits"),
                message=(f"white reads {outcome.digest.get('white_nits')} nits — panel appears "
                         "dark/asleep (off / wrong input / patch window not showing)"))
        wr = (outcome.digest or {}).get("white_reach") or {}
        asked_raise = self._last_brightness_raise()
        adjust_needed = not outcome.data.get("in_range")
        if adjust_needed and not panel_dark:
            if wr.get("status") == "below_band":
                adjust_needed = False       # the white-reach seam asks for the OSD, with the exact target
            elif asked_raise and asked_raise.get("recommended_native_white_nits"):
                want = float(asked_raise["recommended_native_white_nits"])
                nits_now = float(outcome.data.get("white_nits") or 0.0)
                # the white now sits at the raise this stage itself asked for: not a new question
                adjust_needed = abs(nits_now - want) > max(3.0, 0.05 * want)
        if panel_dark or adjust_needed:
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
        if not panel_dark:
            raised = self._white_reach_seam(outcome)
            if raised is not None:
                return raised
        return outcome

    # -- early white-reach (SDR) -----------------------------------------------------------------
    def _sdr_white_reach_forecast(self, primaries: dict[str, float], native_white_xy: tuple[float, float],
                                  white_nits: float) -> dict[str, Any]:
        """The white the SDR refine will deliver, forecast from native primaries + a native white:
        the exact-target-white reach (:func:`mhc_cube.sdr_white_reach`, the refine's own model), less
        the refine's physical margin, placed in the white band (:func:`choose_sdr_white_nits`). The
        reach scales with the backlight (the model is linear in the white luminance), so the native
        white needed for the band's lower edge is ``white * lo / usable``."""
        from .mhc_cube import choose_sdr_white_nits, sdr_white_reach

        spec = self._spec()
        gamma = float(spec.gamma)
        band, band_source = self._sdr_white_band()
        m = self._sdr_white_margin(float(white_nits))
        margin_rel = m["rel"]
        reach = sdr_white_reach(primaries, native_white_xy, float(white_nits), (1.0, 1.0, 1.0),
                                gamma=gamma, target_white_xy=self._white_xy())
        choice = choose_sdr_white_nits(reach.get("reach_nits"), band, float(white_nits), margin_rel=margin_rel)
        usable = choice.get("usable_nits")
        # The reach is linear in the backlight: white * lo / usable lands the margined exact white ON
        # the band edge; one more margin of slack keeps a panel that settles a little lower in band.
        needed = (float(white_nits) * float(band[0]) / float(usable)
                  if usable and choice["status"] == "below_band" else None)
        recommended = needed * (1.0 + margin_rel) if needed else None
        return {"status": choice["status"], "white_nits": round(float(white_nits), 3),
                "native_white_xy": [round(float(native_white_xy[0]), 5), round(float(native_white_xy[1]), 5)],
                "reach_nits": reach.get("reach_nits"), "limiting_channel": reach.get("limiting_channel"),
                "per_channel_nits": reach.get("per_channel_nits"), "usable_nits": usable,
                "delivered_white_nits": choice.get("white_nits"),
                "band": [float(band[0]), float(band[1])], "band_source": band_source,
                "margin_rel": round(margin_rel, 6),
                "margin": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()},
                "needed_native_white_nits": (round(needed, 1) if needed else None),
                "recommended_native_white_nits": (round(recommended, 1) if recommended else None),
                "raise_pct": (round(100.0 * (recommended / float(white_nits) - 1.0), 1) if recommended else None)}

    def _sdr_white_margin(self, nits: float) -> dict[str, Any]:
        """The SDR white's physical margin below the reach — ONE definition for the refine and the
        brightness forecast: the white read's repeatability (DIP noise model) ⊕ the settled thermal
        wander (the run's thermal-alignment evidence; none yet at brightness time — flagged) ⊕ one
        output code at white (the output depth preflight resolved from the live link)."""
        from .mhc_cube import sdr_white_margin_rel

        gamma = float(self._spec().gamma)
        out_bits = self._output_bits()      # the MEASURED link depth resolved at preflight
        code_rel = gamma / float(2 ** out_bits - 1)
        meter_rel = self._meter_lum_sigma_rel(float(nits))
        thermal = self.calib.get("thermal_align")
        drift_rel = refine_convergence.panel_floor_from_thermal(thermal).drift_rel
        rel = sdr_white_margin_rel(meter_rel=meter_rel, drift_rel=drift_rel, code_rel=code_rel)
        return {"rel": rel, "meter_rel": meter_rel, "drift_rel": drift_rel,
                "drift_assumed_zero": not bool(thermal), "code_rel": code_rel, "output_bits": out_bits}

    def _last_brightness_raise(self) -> Optional[dict[str, Any]]:
        hist = self.calib.get("brightness_raises") or []
        return hist[-1] if hist else None

    def _early_white_reach(self, white_nits: float, white_xy: Optional[tuple[float, float]]) -> dict[str, Any]:
        """The brightness stage's forecast of the SDR white: the DIP's native primaries + the white
        just read (its chromaticity when the meter gave one, else the DIP's native white). Skipped for
        HDR (the OSD cannot retarget a PQ peak) and without a DIP (nothing to forecast from — the
        refine's white-band seam still judges the real white)."""
        try:
            spec = self._spec()
            if spec.is_hdr:
                return {"skipped": True, "reason": "HDR: the OSD cannot retarget a PQ peak"}
            dip = self._dip()
            prim = dip.native_primaries if dip is not None else None
            if not prim or not all(k in prim for k in ("R", "G", "B")) or not white_nits or white_nits <= 0:
                return {"skipped": True, "reason": "no DIP native primaries for this display+mode — the "
                                                   "refine's white-band seam judges the measured white"}
            nw = white_xy or (tuple(dip.native_white_xy) if dip.native_white_xy else None)
            if not nw:
                return {"skipped": True, "reason": "no native white chromaticity (meter or DIP)"}
            primaries = {"rx": prim["R"][0], "ry": prim["R"][1], "gx": prim["G"][0], "gy": prim["G"][1],
                         "bx": prim["B"][0], "by": prim["B"][1]}
            out = self._sdr_white_reach_forecast(primaries, (float(nw[0]), float(nw[1])), float(white_nits))
            today = datetime.now().date().isoformat()
            out["basis"] = "dip_primaries+measured_white"
            out["white_xy_source"] = "meter" if white_xy else "dip"
            out["dip"] = {"updated": getattr(dip, "updated", None), "made": getattr(dip, "made", None),
                          "native_white_nits": getattr(dip, "native_white_nits", None),
                          "stale": bool(dip.is_stale(today))}
            return out
        except Exception as exc:  # noqa: BLE001 - a forecast must never break the brightness stage
            return {"skipped": True, "reason": f"forecast failed: {type(exc).__name__}: {exc}"}

    def _pop_decision(self, key: str) -> None:
        """Forget a recorded decision everywhere it can replay from (run record, --decide overrides,
        the adjudicator's seed) — so the NEXT time this seam is reached it pauses for a fresh one."""
        self.calib.get("decisions", {}).pop(key, None)
        self.decision_overrides.pop(key, None)
        seed = getattr(self.adjudicator, "decisions", None)
        if isinstance(seed, dict):
            seed.pop(key, None)

    def _white_reach_seam(self, outcome: StageOutcome) -> Optional[StageOutcome]:
        """OPTIONAL early brightness seam (owner request 2026-09-27): when the forecast says the exact
        target white will land BELOW the SDR white band, ask for more backlight BEFORE ~35 min of raw +
        MHC are measured at this one (BenQ PD2700U: 114.3 native → exact D65 blue-limited at 108.1 →
        107.2 delivered vs band [110, 120]; the refine's white-band seam only saw it 35 min in).
        ``raised`` (the human raised the OSD) re-reads white and forecasts again; ``continue`` keeps
        this backlight (the refine's white-band seam still judges the real white); ``abort``. The
        recommendation ``raised`` is non-benign: ``--supervised`` always pauses here."""
        wr = (outcome.digest or {}).get("white_reach") or {}
        if wr.get("status") != "below_band":
            return None
        lo, hi = (wr.get("band") or [None, None])[:2]
        prev = self._last_brightness_raise()
        # A 'raised' that did not change the white (within the white's own margin) is not a raise:
        # re-ask with 'continue' suggested + flagged, so no adjudicator can loop on it (the sim/CI
        # AutoAdjudicator takes recommendations) and a supervised run pauses on the flag.
        unchanged = bool(prev and prev.get("white_nits") and wr.get("white_nits")
                         and abs(float(wr["white_nits"]) - float(prev["white_nits"]))
                         <= max(float(wr.get("margin_rel") or 0.0), 1e-6) * float(prev["white_nits"]))
        question = (
            f"white reads {wr.get('white_nits')} nits; with this panel's primaries an EXACT target white "
            f"is only reachable up to {wr.get('reach_nits')} nits ({wr.get('limiting_channel')} channel "
            f"at full drive), so the refine would deliver ~{wr.get('delivered_white_nits')} nits — below "
            f"the SDR white band [{lo}, {hi}]. Raising the OSD brightness to about "
            f"{wr.get('recommended_native_white_nits')} nits native white (+{wr.get('raise_pct')} %) would "
            "put the exact white in the band. 'raised' = the backlight was raised (white is re-read and "
            "forecast again; drop a --decide for this seam once consumed); 'continue' = keep this "
            "backlight (the refine's white-band seam still judges the real white); 'abort'.")
        if unchanged:
            question = (f"the white did NOT change after 'raised' ({prev.get('white_nits')} → "
                        f"{wr.get('white_nits')} nits) — the OSD may already be at maximum. " + question)
        key = "brightness:white-reach"
        decision = self.adjudicate(AdjudicationRequest(
            key=key, seam=SEAM_BRIGHTNESS, stage="brightness", question=question,
            options=("raised", "continue", "abort"),
            recommendation=("continue" if unchanged else "raised"),
            digest={**outcome.digest, "white_reach": wr,
                    "brightness_raises": self.calib.get("brightness_raises") or [],
                    **({"compromised": True, "raise_had_no_effect": True} if unchanged else {})}))
        self._abort_if(decision, stage="brightness",
                       message="aborted at the white-reach seam (exact white below the SDR band)")
        if decision.choice != "raised":
            return None
        downstream = [k for k in ("measure:raw", "build-install-mhc", "refine-mhc-grayscale",
                                  "measure:post-mhc", "build-install-3dlut", "measure:verify")
                      if k in (self.calib.get("stages") or {})]
        if downstream:
            # A 'raised' can only be honoured BEFORE anything is measured at this backlight (a
            # --decide override re-deciding this seam later, or a stale flag re-passed on resume):
            # re-reading white now would leave raw + the MHC built for the old backlight. Say so.
            self._pop_decision(key)
            self.runlog.anomaly("brightness", kind="late_white_reach_raise", downstream=downstream,
                                message=("'raised' at brightness:white-reach after " + ", ".join(downstream)
                                         + " were measured at the old backlight — not re-read"))
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="brightness:white-reach-late", seam=SEAM_BRIGHTNESS, stage="brightness",
                question=("'raised' was decided for brightness:white-reach, but " + ", ".join(downstream)
                          + " were already measured at the old backlight. Raising the OSD now needs a "
                          "fresh run ('abort', then re-run with the backlight raised); 'keep_backlight' "
                          "continues at the backlight everything was measured at."),
                options=("keep_backlight", "abort"), recommendation="keep_backlight",
                digest={"white_reach": wr, "downstream": downstream})),
                stage="brightness", message="aborted — raise the backlight and re-run")
            return None
        # The human changed the backlight: this stage's white (and any decision taken on it) is stale.
        # Keep the audit trail, forget both, and read again — a still-short forecast pauses afresh.
        self.calib.setdefault("brightness_raises", []).append({
            "white_nits": wr.get("white_nits"), "reach_nits": wr.get("reach_nits"),
            "delivered_white_nits": wr.get("delivered_white_nits"),
            "recommended_native_white_nits": wr.get("recommended_native_white_nits"),
            "decided": getattr(decision, "note", None)})
        self.calib["stages"].pop("brightness", None)
        for k in ("brightness:adjust", key):
            self._pop_decision(k)
        self._save()
        return self.stage_brightness()

    def _post_raw_white_reach(self, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The same forecast from the RAW-measured primaries + native white (what the refine will
        actually use), set beside the brightness-stage forecast. Evidence for every SDR run."""
        prim = params.get("primaries") or {}
        mw = params.get("measured_white") or {}
        peak = params.get("target_luminance")
        if not (prim and mw.get("x") is not None and mw.get("y") is not None and peak):
            return None
        try:
            out = self._sdr_white_reach_forecast(prim, (float(mw["x"]), float(mw["y"])), float(peak))
        except Exception as exc:  # noqa: BLE001 - evidence must never break the build
            return {"error": f"{type(exc).__name__}: {exc}"}
        early = (((self.calib.get("stages") or {}).get("brightness") or {}).get("digest") or {}).get("white_reach")
        out["basis"] = "raw_primaries+raw_white"
        if isinstance(early, dict):
            out["brightness_forecast"] = {k: early.get(k) for k in ("status", "reach_nits", "delivered_white_nits",
                                                                     "limiting_channel", "skipped", "reason")}
            if early.get("reach_nits") and out.get("reach_nits"):
                out["forecast_error_rel"] = round(float(early["reach_nits"]) / float(out["reach_nits"]) - 1.0, 5)
        return out

    def _post_raw_white_reach_seam(self, outcome: StageOutcome) -> None:
        """A seam at the MHC build ONLY when the brightness forecast missed (it said in-band, or
        could not forecast) and the raw-measured primaries put the exact white below the band.
        Raising the backlight now means re-measuring raw, so the options are keep / abort."""
        wr = (outcome.digest or {}).get("white_reach_after_raw") or {}
        if wr.get("status") != "below_band":
            return
        early = wr.get("brightness_forecast") or {}
        if early.get("status") == "below_band":
            return      # already asked at the brightness seam (and answered there)
        lo, hi = (wr.get("band") or [None, None])[:2]
        had = ("could not forecast it (" + str(early.get("reason")) + ")" if early.get("skipped")
               else f"forecast {early.get('delivered_white_nits')} nits ({early.get('status')})")
        question = (
            f"from the RAW-measured primaries an exact target white is only reachable up to "
            f"{wr.get('reach_nits')} nits ({wr.get('limiting_channel')} limiting) — the refine will "
            f"deliver ~{wr.get('delivered_white_nits')} nits, BELOW the SDR white band [{lo}, {hi}]; the "
            f"brightness stage {had}. Raising the OSD now (to ~{wr.get('recommended_native_white_nits')} nits "
            "native white) means RE-MEASURING raw: 'abort' and re-run with the backlight raised, or "
            "'keep_below_band' to continue at this backlight (the refine's white-band seam still judges "
            "the real white).")
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key="build-install-mhc:white-reach", seam=SEAM_BRIGHTNESS, stage="build-install-mhc",
            question=question, options=("keep_below_band", "abort"), recommendation="keep_below_band",
            digest={"white_reach_after_raw": wr})),
            stage="build-install-mhc",
            message="aborted at the post-raw white-reach seam — re-run with the backlight raised")

    def _neutral_state_audit(self) -> dict[str, Any]:
        """The pipe + DesktopLUT.ini neutral-state audit for this monitor/mode (see
        :func:`dlc.neutral_audit.neutral_state_audit`); degrades to notes (never raises) on the
        mock / a profile without ``paths.desktoplut_ini``."""
        audit = neutral_audit.neutral_state_audit(
            self.controller, self.monitor, self.mode, ini_path=self._resolve_desktoplut_ini())
        audit["neutral_profile"] = self.calib.get("neutral_profile")
        return _jsonable(audit)

    # verify-only measures THROUGH the installed (or a candidate) cube: a crossed hook routing would
    # score an uncorrected panel, so it gets the same optical self-check as the cube-building flows.
    _CUBE_FLOWS = ("full", "3dlut-only", "refine-mhc", "verify-only")

    def _hook_routing_pending(self) -> dict[str, Any]:
        """Pre-read evidence for the readiness seam: what the hook reports now and whether the
        optical self-check will run once the operator says ``ready`` (no meter read here)."""
        try:
            hook = self.controller.hook_state()
        except Exception as exc:  # noqa: BLE001
            return {"hook": None, "will_check": None, "reason": f"hook state unavailable: {exc}"}
        flow = self.calib.get("flow")
        needed, reason = hook_routing.routing_needs_check(hook, self.monitor, self.hook_routing_policy)
        return {"hook": hook, "will_check": bool(needed and flow in self._CUBE_FLOWS and self.measure is not None),
                "reason": reason, "policy": self.hook_routing_policy}

    def _hook_routing_self_check(self, key: str) -> dict[str, Any]:
        """The DWM-hook LUT-routing self-check for cube-installing flows (dlc.hook_routing).

        2026-09-03: the hook order-matches twin panels and re-rolled on every set_3dlut, so a
        3dlut-only run's probes + verify measured the UNCORRECTED panel. Mechanics here: when the
        hook's own report cannot prove the assignment (policy ``auto``: order/pinned-matched and
        unconfirmed, stale, absent, or an old build) a magenta probe cube is installed on the
        calibrated slot and the meter must see it move; no effect => swap the twin pairing once;
        still none => :class:`StageError` (a cube flow must not measure through a cube that is not
        there). A needed swap is an ANOMALY the LLM sees at the readiness seam + check-ins (the
        previous roll was wrong — anything measured under it is suspect); the verdict lands in the
        stage digest either way. Flows that install no cube get the decision only (no reads)."""
        flow = self.calib.get("flow")
        try:
            hook = self.controller.hook_state()
        except Exception as exc:  # noqa: BLE001 - old build / mock without state: evidence, not a crash
            self.ctx.log(f"{key}: hook state unavailable ({type(exc).__name__}: {exc})")
            hook = None
        needed, reason = hook_routing.routing_needs_check(hook, self.monitor, self.hook_routing_policy)
        if flow not in self._CUBE_FLOWS:
            return {"checked": False, "required": False, "flow": flow,
                    "reason": f"flow {flow!r} installs no cube ({reason})", "hook": hook}
        if not needed:
            self.runlog.note(key, f"hook routing self-check not needed: {reason}", hook=hook)
            return {"checked": False, "required": False, "flow": flow, "reason": reason, "hook": hook}
        if self.measure is None:
            self.runlog.anomaly(key, kind="hook_routing", hook=hook,
                                message=f"hook routing needs an optical check ({reason}) but this run has "
                                        "no meter — the cube may render on another panel; judge before "
                                        "trusting any cube-flow result")
            return {"checked": False, "required": True, "flow": flow, "reason": reason, "hook": hook}
        self.runlog.note(key, f"hook routing self-check: {reason}", hook=hook)
        try:
            result = hook_routing.run_hook_routing_check(
                self.controller, self.monitor, self.mode, self.bit_depth, self.measure,
                self.ctx.root / "generated", policy=self.hook_routing_policy,
                log=lambda msg: self.ctx.log(f"{key}: hook routing: {msg}"))
        except hook_routing.HookRoutingError as exc:
            digest = exc.result.as_dict()
            self.runlog.anomaly(key, kind="hook_routing", message=str(exc), hook_routing=digest)
            raise StageError(
                key, f"DWM hook does not render a cube on the calibrated display: {exc}",
                hook_routing=digest) from exc
        digest = result.as_dict()
        digest["required"] = True
        if result.swapped:
            self.runlog.anomaly(
                key, kind="hook_routing", hook_routing=digest,
                message=(f"hook routing was CROSSED for monitor {self.monitor}: the probe cube did not "
                         "reach the calibrated panel until the twin assignment was swapped (now "
                         f"{'confirmed' if result.confirmed else 'swapped but unconfirmed'}). Anything "
                         "measured under the previous assignment rendered on the other panel."))
        elif result.checked:
            self.runlog.note(key, f"hook routing proven through the meter ({result.verdict})",
                             hook_routing=digest)
        for note in result.notes:
            self.runlog.note(key, f"hook routing: {note}")
        return digest

    def _hook_routing_evidence_after_install(self, stage: str, *, action: str = "install") -> None:
        """Evidence only, right after a ``set_3dlut`` (or a ``clear_3dlut``, ``action="clear"``):
        every install/clear re-injects the hook DLL, and
        if the DWM session changed underneath (a dwm.exe restart re-rolls the twin order-match)
        the report flips back to ambiguous — the cube may now render on the other panel. Never
        blocks; the LLM judges it from the check-in stream. Degrades to a log line on a build
        without hook reporting."""
        try:
            hook = self.controller.hook_state()
        except Exception as exc:  # noqa: BLE001
            self.ctx.log(f"{stage}: hook state unavailable after the cube {action} ({type(exc).__name__}: {exc})")
            return
        if hook is None:
            self.ctx.log(f"{stage}: no hook routing report after the cube {action} (old DesktopLUT build?)")
            return
        if hook.get("needs_check"):
            self.runlog.anomaly(
                stage, kind="hook_routing", hook=hook,
                message=f"hook routing became ambiguous/unconfirmed after a cube {action} (DWM session "
                        "changed?) — the cube may be rendering on another panel")

    def stage_hardware_readiness(self) -> StageOutcome:
        """One operator/LLM gate before the first live meter read.

        Carries the neutral-state audit in its digest — the identity MHC profile name and the
        GUI-layer flags (tonemap / Desktop Gamma / WB / GS) read from the live DesktopLUT.ini —
        so the LLM sees what the meter is about to measure THROUGH. After an ``enter-neutral``
        in this run the stage REFUSES (:class:`StageError`) on the mechanical violations: a GUI
        layer still ON for the calibrated mode, or no MHC profile associated (the identity
        association did not land ⇒ Windows is still driving the last MHC2 transform). Flows
        that keep the user's stack (3dlut-only / grayscale-wb) get the same audit as evidence
        only. When the operator gate is not required (sim/CI) the audit + refusal still run
        (pipe + ini reads only — no seam)."""
        key = "hardware-readiness"
        self._enter_measurement_layers()

        def audit_and_refuse() -> dict[str, Any]:
            audit = self._neutral_state_audit()
            stages = self.calib.get("stages") or {}
            after_neutral = (stages.get("enter-neutral") or {}).get("status") == "done"
            audit["after_enter_neutral"] = after_neutral
            # "No profile associated" is a MECHANICAL refusal only right after a live
            # association in this process; on a resume (enter-neutral replayed) the pipe's
            # current state is evidence the seam judges (a restarted DesktopLUT loses it).
            live_assoc = bool(getattr(self, "_neutral_associated_live", False))
            violations = neutral_audit.neutral_violations(audit, require_profile=live_assoc) \
                if after_neutral else []
            if violations:
                for v in violations:
                    self.runlog.anomaly(key, kind="neutral_state", message=v)
                raise StageError(
                    key, "panel is NOT neutral after enter-neutral — refusing the first read: "
                    + "; ".join(violations), neutral_audit=audit, violations=violations)
            warnings: list[str] = []
            if audit.get("gui_layers_enabled"):
                # Not after enter-neutral (the user's stack is deliberately live) — evidence only.
                warnings.append("GUI layers ON for the calibrated mode: "
                                + ", ".join(audit["gui_layers_enabled"]))
            if after_neutral and not audit.get("mhc_associated"):
                warnings.append("resumed run: state() shows NO MHC profile associated for "
                                f"{audit.get('key')} — the identity neutral may have been lost "
                                "(DesktopLUT restarted?); judge before the first read")
            if warnings:
                audit["warning"] = "; ".join(warnings)
            return audit

        if not self.require_hardware_readiness:
            audit = audit_and_refuse()
            routing = self._hook_routing_self_check(key)
            return StageOutcome(key, "done", digest={"required": False, "neutral_audit": audit,
                                                     "hook_routing": routing,
                                                     "installed_stack": self.calib.get("installed_stack"),
                                                     "viewing_layers": self.calib.get("viewing_layers")})

        def run() -> StageOutcome:
            audit = audit_and_refuse()
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
                        "content_mode": self.content_mode,
                        "bit_depth": self.bit_depth, "dogegen_required": True,
                        "neutral_audit": audit,
                        "hook_routing_pending": self._hook_routing_pending(),
                        "installed_stack": self.calib.get("installed_stack"),
                        "viewing_layers": self.calib.get("viewing_layers")}))
            if decision.choice == "abort":
                raise CalibrationAborted(StageOutcome(
                    key, "aborted",
                    digest={"message": "hardware readiness aborted by operator/LLM",
                            "decision_note": decision.note}))
            # The optical routing proof (a probe cube on a mid grey) is the run's FIRST meter
            # read, so it runs AFTER the operator confirmed the meter is aimed at the patch —
            # probing an unaimed meter would read "no effect" twice and refuse a healthy rig.
            # A resume replays the recorded decision, so a live run pays for this exactly once;
            # the verdict lands in this stage's digest and a needed swap is an anomaly in the
            # spine (check-ins) before the first raw measure.
            routing = self._hook_routing_self_check(key)
            return StageOutcome(key, "done",
                                digest={"required": True, "confirmed": True,
                                        "decision_note": decision.note,
                                        "neutral_audit": audit, "hook_routing": routing,
                                        "installed_stack": self.calib.get("installed_stack"),
                                        "viewing_layers": self.calib.get("viewing_layers")})

        return self._stage(key, run)

    def stage_measure(self, *, role: str, patches: Sequence[tuple[int, int, int]],
                      ti3_name: str, ndjson_name: str) -> StageOutcome:
        # Assert the keep-awake around EVERY measure stage (not just the whole-run wrap in
        # run()) so a direct stage_measure / partial-flow caller — and any compute gap right
        # before this read — can't let the box sleep mid-measure. Reentrant: when run() already
        # holds it this is a cheap no-op; released here (incl. on a seam abort) regardless.
        with keep_awake(reason=f"dlc measure ({role})"):
            return self._stage_measure(role=role, patches=patches,
                                       ti3_name=ti3_name, ndjson_name=ndjson_name)

    def _stage_measure(self, *, role: str, patches: Sequence[tuple[int, int, int]],
                       ti3_name: str, ndjson_name: str) -> StageOutcome:
        key = f"measure:{role}"

        def run() -> StageOutcome:
            # post-MHC = the cube build's training set and the seeds its probe reuse answers from: it must
            # read through the probes' path (no runtime cube — an earlier build's included).
            probe_path = self._ensure_probe_path(key, reads="post-MHC reads") if role == "post-mhc" else None
            viewing, thermal_rec = self._thermal_state_plan(key, role, patches)
            # (the default call is unchanged: ``viewing`` is passed only when a viewing precondition exists)
            res = self._measure_set(patches, role=role, ti3_name=ti3_name, ndjson_name=ndjson_name,
                                    **({"viewing": viewing} if viewing is not None else {}))
            bookend_drift = self._bookend_drift_qc(role, res.ti3_path, patches, res.ndjson_path)
            digest = dict(res.digest)
            # Which thermal state these numbers represent (+ for viewing: target, start, decision, the
            # precondition result and the modelled/observed state of the measured pass).
            digest["thermal_state"] = {**thermal_rec, **(res.digest.get("thermal_state") or {})}
            if viewing is not None:
                # The state the pass LEFT the panel in: the hotter of the load it showed (observed) and the
                # modelled end state — conservative, since the next start relaxes from it.
                loop_ts = res.digest.get("thermal_state") or {}
                left = [v for v in ((loop_ts.get("measure") or {}).get("observed_load"),
                                    (loop_ts.get("final") or {}).get("modelled_load")) if v is not None]
                self._note_thermal_history(key, max(left) if left else None,
                                           basis="max(observed load of the measured pass, modelled end state)")
            elif thermal_rec.get("requested") == "viewing":
                self._note_thermal_history(key, (thermal_rec.get("model_band") or {}).get("load"),
                                           basis="model band of the set (own-band preheat)")
            # The thermal preheat policy this measure ran under (--preheat, else the loop config's
            # own) — evidence beside the controller's own `preheat` digest (null when it skipped).
            run_cfg = self._with_preheat(self.loop_config or self._loop_config_for(self._dip()))
            digest["preheat_policy"] = run_cfg.preheat
            if run_cfg.stall_reads <= 0:
                digest["present_stall_detect"] = "off"   # --present-stall off: no stuck-frame guard
            if probe_path is not None:
                digest["probe_path"] = probe_path
            if bookend_drift is not None:
                digest["bookend_drift_qc"] = bookend_drift
            return StageOutcome(key, "done", digest=digest,
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
        # Fresh executions only: a memoised replay already put its metrics_scored on the spine in
        # the invocation that measured it (events.jsonl is append-only across resumes), so
        # re-scoring here would duplicate the convergence history on every resume.
        if role in ("raw", "post-mhc") and outcome.status == "done" and not outcome.replayed:
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
                # Persist the mutated record BEFORE adjudicating (fable Phase 7a): the escalation
                # seam below may PAUSE the run (AdjudicationRequired exits the process without a
                # save), and a resume replays this stage from the record WITHOUT re-scoring (the
                # replayed gate above) — an unpersisted anomaly would silently skip the seam the
                # LLM never answered. _stage stored this same outcome's record, so re-recording
                # is a cheap idempotent overwrite carrying the anomaly flags.
                self.calib["stages"][key] = outcome.as_record()
                self._save()
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
            # A dead meter (the loop's read guard halted the pass) is a run-stopper: never a
            # benign auto-accept — flag it compromised so every adjudicator escalates.
            meter_down = bool(outcome.digest.get("meter_down"))
            # A dark panel, compromised preheat, or a blown remeasure/drift budget is non-benign:
            # recommend retry (not accept), offer it, and flag compromised so SupervisedAdjudicator
            # escalates rather than rubber-stamping black/garbage data. The RECOMMENDATION (never
            # a decision — the seam still judges) also weighs read-repeatability: stable-but-
            # implausible envelope reads are real panel/correction behaviour a retry would just
            # re-measure and re-fail (item #4, 2026-09-02 C6 run).
            recommendation, basis = _measure_escalation_recommendation(outcome.digest)
            retry_recommended = recommendation == "retry"
            # A viewing-state miss (precondition unmet/skipped, or the pass left the band) is re-runnable:
            # `remeasure` re-runs the viewing precondition + the pass — its thermal-state seam re-asks
            # (the memoised decision is dropped below) with the start now from this run's history.
            viewing_miss = any(r in (outcome.digest.get("anomaly_reasons") or ())
                               for r in ("viewing_precondition_unmet", "viewing_band_left"))
            options = (("accept", "suppress", "remeasure", "retry", "abort")
                       if (retry_recommended or score_anomaly or measurement_path_compromised)
                       else ("accept", "suppress", "remeasure", "abort") if viewing_miss
                       else ("accept", "suppress", "abort"))
            decision = self.adjudicate(AdjudicationRequest(
                key=f"{key}:escalation", seam=SEAM_MEASURE, stage=key,
                question=outcome.data.get("question") or "measurement did not fully settle - accept or retry?",
                options=options,
                recommendation=recommendation,
                digest={**outcome.digest,
                        **({"recommendation_basis": basis} if basis else {}),
                        "compromised": (meter_down or panel_dark or preheat_compromised
                                        or measurement_path_compromised
                                        or score_anomaly)}))
            if decision.choice == "remeasure":
                self._invalidate_thermal_align(key, outcome.data.get("ti3"))
                self.calib["stages"].pop(key, None)
                self.calib.get("decisions", {}).pop(f"{key}:escalation", None)
                self.decision_overrides.pop(f"{key}:escalation", None)
                # Also drop the copy in the adjudicator's SEED map (Mapping/Supervised are seeded
                # from the run-record + --decide at process start). Without this, a re-measure
                # that STILL escalates re-answers itself "remeasure" from the seed — an unbounded
                # silent hardware re-measure loop that never re-reaches the LLM. One remeasure
                # decision buys exactly one re-measure; a second escalation pauses again.
                seed = getattr(self.adjudicator, "decisions", None)
                if isinstance(seed, dict):
                    seed.pop(f"{key}:escalation", None)
                # A viewing-state stage: the re-measure re-runs the PRECONDITION too, so its thermal-state
                # seam must re-ask (new start from the run history, new predicted time) — never replay the
                # memoised choice over numbers the LLM has not seen — and a --viewing-start-nits that seam took
                # is spent (the panel has run since). No-op for any other stage.
                self._spend_given_start_on_remeasure(key, "escalation remeasure")
                self._forget_decision(f"{key}:thermal-state", overrides=True)
                self._save()
                return self.stage_measure(role=role, patches=patches,
                                          ti3_name=ti3_name, ndjson_name=ndjson_name)
            if decision.choice == "retry":
                raise CalibrationAborted(StageOutcome(
                    key, "aborted",
                            digest={"message": "measurement retry requested at LLM seam",
                                    "retry_requested": True, **outcome.digest}))
            self._abort_if(decision, stage=key, message="aborted on unsettled measurement")
        if role in ("raw", "post-mhc") and outcome.status == "done":
            outcome = self._thermal_align_gate(key, role, outcome)
        return outcome

    # -- thermal-state alignment (plan item 3) ----------------------------
    def _invalidate_thermal_align(self, key: str, ti3: Optional[str] = None) -> None:
        """A measure stage is about to be RE-MEASURED into the same files (escalation
        ``remeasure`` / adaptive-planning invalidation): drop its alignment record, the memoised
        seam decision (the LLM must judge the NEW data), and the on-disk backup/note — otherwise
        the fresh dataset is consumed unaligned while the record claims alignment (adversarial
        review, 2026-09-03)."""
        store = self.calib.get("thermal_align") or {}
        store.pop(key, None)
        self.calib["thermal_align"] = store
        dkey = f"{key}:thermal-align"
        (self.calib.get("decisions") or {}).pop(dkey, None)
        self.decision_overrides.pop(dkey, None)
        seed = getattr(self.adjudicator, "decisions", None)
        if isinstance(seed, dict):
            seed.pop(dkey, None)
        if ti3:
            try:
                thermal_align.discard_backup(Path(ti3))
            except OSError as exc:
                self.ctx.log(f"could not discard the stale thermal-align backup for {key}: {exc}")

    def _thermal_align_basis(self) -> Optional[tuple[dict[str, Any], tuple[float, float]]]:
        """The linear basis the alignment gains live in: this run's measured MHC primaries +
        native white when the build has run, else the DIP's native primaries, else the target
        colour space (any consistent basis near the panel's works — the gain is per channel)."""
        params = self._state.get("mhc_params") or {}
        prim = params.get("primaries")
        mw = params.get("measured_white") or {}
        if prim and all(k in prim for k in ("rx", "ry", "gx", "gy", "bx", "by")) and mw.get("x"):
            return dict(prim), (float(mw["x"]), float(mw["y"]))
        dip = self._dip()
        if dip is not None and dip.native_primaries and getattr(dip, "native_white_xy", None):
            npr = dip.native_primaries
            if all(ch in npr and npr[ch] and len(npr[ch]) >= 2 for ch in ("R", "G", "B")):
                return ({"rx": npr["R"][0], "ry": npr["R"][1], "gx": npr["G"][0], "gy": npr["G"][1],
                         "bx": npr["B"][0], "by": npr["B"][1]},
                        (float(dip.native_white_xy[0]), float(dip.native_white_xy[1])))
        cs = gamut.STANDARD_PRIMARIES["Rec.2020" if self.mode == "HDR" else "Rec.709"]
        try:
            white = self._white_xy()
        except Exception:  # noqa: BLE001 - no resolved target yet (direct stage use): D65 basis
            white = (0.3127, 0.3290)
        return ({"rx": cs["R"][0], "ry": cs["R"][1], "gx": cs["G"][0], "gy": cs["G"][1],
                 "bx": cs["B"][0], "by": cs["B"][1]}, white)

    def _thermal_align_gate(self, key: str, role: str, outcome: StageOutcome) -> StageOutcome:
        """Evidence every time, a SEAM when it matters (Design Law): the stage's interleaved
        reference track is turned into an alignment evidence packet (span vs the reference's own
        read noise, and the |Δx| each option would move the dataset by). Below the significance
        threshold the packet rides the record/check-in and nothing is touched; above it the LLM
        chooses the state the dataset is aligned to (end / mid / start / none) — or the operator
        pre-decided it with ``--thermal-align``. The chosen alignment rewrites the stage TI3 in
        place (original kept as ``.orig``, idempotent) BEFORE the build consumes it."""
        ti3, nd = outcome.data.get("ti3"), outcome.data.get("ndjson")
        if not ti3 or not nd:
            return outcome
        basis = self._thermal_align_basis()
        if basis is None:
            return outcome
        store = self.calib.setdefault("thermal_align", {})
        rec = dict(store.get(key) or {})
        evidence = rec.get("evidence")
        # Belt and braces: the memoised evidence/decision must describe THIS file. If the stage
        # was re-measured into the same path by a route the invalidation sites do not cover,
        # the content no longer matches the evidence's sha nor an aligned output of it.
        if isinstance(evidence, dict) and evidence.get("available"):
            state = thermal_align.backup_state(Path(ti3))
            cur_sha = None
            try:
                cur_sha = thermal_align._sha(Path(ti3).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
            if state == "stale" or (state == "none" and cur_sha and evidence.get("ti3_sha")
                                    and cur_sha != evidence.get("ti3_sha")):
                self.ctx.log(f"{key}: dataset changed since the thermal-align evidence was taken — "
                             "re-evaluating (stale record/backup discarded)")
                self._invalidate_thermal_align(key, ti3)
                rec, evidence = {}, None
        if not isinstance(evidence, dict):
            try:
                evidence = thermal_align.evaluate(nd, ti3, basis[0], basis[1])
            except Exception as exc:  # noqa: BLE001 - evidence must never crash the spine
                evidence = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
            rec["evidence"] = evidence
            store[key] = rec
            self._save()
        if not evidence.get("available"):
            outcome.digest["thermal_align"] = {"available": False, "reason": evidence.get("reason")}
            return outcome
        track = evidence.get("track") or {}
        policy = self.thermal_align
        if policy in ("end", "start", "mid", "none"):
            choice, decided_by = policy, "cli"
        elif evidence.get("significant"):
            opts = evidence.get("options") or {}

            def _opt(name: str) -> str:
                o = opts.get(name) or {}
                return f"{name}: |dx| mean {o.get('dx_mean', 0):.4f} max {o.get('dx_max', 0):.4f}"

            decision = self.adjudicate(AdjudicationRequest(
                key=f"{key}:thermal-align", seam=SEAM_MEASURE, stage=key,
                question=(f"The {role} dataset was measured across a thermal drift: the interleaved "
                          f"reference (rgb {track.get('reference_rgb')}) moved {track.get('drift_x'):+.4f} x "
                          f"(span {track.get('span_x'):.4f}) over {track.get('minutes')} min, vs its own read "
                          f"noise {track.get('noise_x'):.5f} (threshold {evidence.get('threshold_x'):.4f}). "
                          "Aligning rewrites each read to ONE reference state before the build "
                          f"({_opt('end')}; {_opt('mid')}; {_opt('start')}). align-end = the state the "
                          "next stage starts in (build consistent with its refine/verify minutes later); "
                          "align-mid = the middle-ground state (a stack viewed under average load); "
                          "align-start = the cold end; none = build on the unaligned data (the drift "
                          "bakes into the correction as ripple along the ramp). Which state?"),
                options=("align-end", "align-mid", "align-start", "none"),
                recommendation="align-" + str(evidence.get("recommendation") or "end"),
                digest={"role": role, **evidence}))
            choice = decision.choice.replace("align-", "")
            decided_by = "seam"
        else:
            choice, decided_by = "none", "auto:flat"
        applied = rec.get("applied")
        if choice != "none" or applied:
            try:
                applied = thermal_align.apply(ti3, nd, basis[0], basis[1], choice,
                                              decided_by=decided_by)
            except Exception as exc:  # noqa: BLE001
                self.runlog.anomaly(key, kind="thermal_align",
                                    message=f"thermal alignment '{choice}' failed: {type(exc).__name__}: {exc}")
                applied = {"align": choice, "error": f"{type(exc).__name__}: {exc}"}
        rec.update({"choice": choice, "decided_by": decided_by, "applied": applied})
        store[key] = rec
        summary = {"choice": choice, "decided_by": decided_by,
                   "significant": bool(evidence.get("significant")),
                   "span_x": track.get("span_x"), "drift_x": track.get("drift_x"),
                   "noise_x": track.get("noise_x"), "threshold_x": evidence.get("threshold_x"),
                   "minutes": track.get("minutes")}
        if applied:
            summary["rows_corrected"] = applied.get("rows_corrected")
            summary["dx_mean"] = applied.get("dx_mean")
            summary["dx_max"] = applied.get("dx_max")
        outcome.digest["thermal_align"] = summary
        self.calib["stages"][key] = outcome.as_record()
        self._save()
        self.runlog.note(key, f"thermal-align {role}: {choice} ({decided_by}); reference span "
                              f"{track.get('span_x')} x over {track.get('minutes')} min"
                              + (f"; {applied.get('rows_corrected')} rows re-aligned" if applied and applied.get("rows_corrected") else ""),
                         **summary)
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
        if digest.get("meter_down"):
            # A meter-down halt leaves a PARTIAL pass (its brightest read may be a dim patch):
            # that is not a collapsed foundation, and the meter-down run-stopper seam (with the
            # meter's error text) is the one the judge must see.
            return None
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
            reachable = self._reachable_primaries() if spec.is_hdr else None
            if spec.is_hdr:
                metrics, lum = score_samples_hdr(samples, white_xy=(wx, wy),
                                                 oog_mapping=self._oog_mapping(),
                                                 peak_nits=self._hdr_target().peak_nits,
                                                 reachable_primaries=reachable)
                metric_name = "dE_ITP"
            else:
                metrics, lum = score_samples(samples, gamma=spec.gamma, white_xy=(wx, wy))
                metric_name = "CIEDE2000"
            summary = summarize_metrics(phase=label, iteration=0, source=p,
                                        patch_metrics=metrics, target_luminance=lum, metric=metric_name)
            practical = practical_summary(metrics, is_hdr=spec.is_hdr,
                                          gamut_aware=reachable is not None)
            # Snapshot for the timed check-in's live metrics (most recent intermediate score).
            self._last_scored = {"label": label, "metric": metric_name,
                                 "avg": round(summary.avg_de2000, 3), "max": round(summary.max_de2000, 3),
                                 "white": round(summary.white_de2000, 3)}
            # Persist the compact per-stage score in the run record so the TERMINAL verify seam
            # can show the before→after trajectory (raw → after ICC → verify) in ITS digest —
            # "avg 1.9" reads differently when raw was 8.4 vs when raw was 2.0 (fable Phase 8,
            # digest-sufficiency). Durable across resume; same metric branch as stage_verify.
            self.calib.setdefault("stage_scores", {})[role] = {
                "label": label, "metric": metric_name,
                "avg": round(summary.avg_de2000, 3), "p95": round(summary.p95_de2000, 3),
                "max": round(summary.max_de2000, 3), "white": round(summary.white_de2000, 3)}
            self._save()
            # Canonical event shape (metrics.metrics_scored_payload, P4) — same keys every
            # producer emits, so the dashboard ΔE panel renders live and stage-CLI runs alike.
            self.runlog.metrics_scored(
                f"measure:{role}",
                **metrics_scored_payload(summary, label=label, practical=practical))
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
                    "p99_de2000": round(summary.p99_de2000, 3),
                    "max_de2000": round(summary.max_de2000, 3),
                    "white_de2000": round(summary.white_de2000, 3),
                    "high_spike_count": len(high_spikes),
                    "high_spike_fraction": round(high_fraction, 4),
                    "patch_count": summary.patch_count,
                    "worst": [{"rgb": [round(c, 4) for c in m.rgb],
                               "de2000": round(m.de2000, 3),
                               "gamut_clamped": m.gamut_clamped} for m in worst],
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
            # …but this guard swallowed a real NameError during Phase 6's own development, and it
            # protects the score-anomaly escalation — the exact signal it can eat. Log the full
            # traceback (workflow.log) and put a WARN on the spine so the LLM/dashboard see that
            # the intermediate score is MISSING, instead of the failure vanishing without a trace.
            import traceback
            self.ctx.log(f"intermediate scoring for measure:{role} failed (advisory, flow continues):\n"
                         + traceback.format_exc())
            try:
                self.runlog.note(f"measure:{role}",
                                 "intermediate scoring failed — no metrics_scored for this stage; "
                                 "traceback in workflow.log", level="WARN",
                                 error=traceback.format_exc(limit=3))
            except Exception:  # noqa: BLE001 - the fallback logger must not raise
                pass
            return None

    def stage_build_install_mhc(self, raw_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            spec = self._spec()
            # Derive MHC params from the raw TI3 (reuses the proven build-mhc stage:
            # measured primaries + native-white→target-white matrix + tone-only base 1D).
            args = Namespace(run=self.ctx.root, monitor=self.monitor, mode=self.mode,
                             simulate=False, gamma=spec.gamma, source_ti3=raw_ti3,
                             is_hdr=spec.is_hdr, top_hold=self.mhc_top_hold)
            if spec.is_hdr:
                # ONE SOURCE OF TRUTH (Task C): hand build-mhc the SAME resolved max-sustained peak
                # patch bounding uses (_patch_max_cv → _hdr_target().peak_nits), so the MHC cube
                # ceiling + the C++ set_base_lut handoff agree with the patch set instead of
                # re-deriving an independent raw-TI3 max. Skip an ungrounded cold-start placeholder
                # (no DIP measured yet) — there the stage's own raw-TI3 max is the real measurement.
                hdr = self._hdr_target()
                if (hdr.provenance.get("peak") or {}).get("grounded", True):
                    args.resolved_peak_nits = hdr.peak_nits
                # The level edge (D4) is anchored on the white the ENGINE target uses (the resolved white).
                args.level_edge_white_xy = list(self._white_xy())
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
            base_lut = params.get("base_lut")
            # Install through the controller (set primaries/white → base correction → apply → verify).
            applied, verified, white = self._install_mhc_params(params, spec)
            wx, wy = white.xy
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
                # closed-loop refine will hold D65 to (see stage_refine_mhc_cube). Carries the
                # WRGB-gate fields (drive_matched_nonadditivity / cap_policy / wrgb_nonadditive /
                # full_drive_grounded) so the adjudicator sees whether the additive cap was applied
                # or bypassed for a W-subpixel panel, and whether the ceiling is full-drive-grounded.
                digest["peak_chroma"] = params["peak_chroma"]
            if params.get("dark_floor"):
                # The σ-aware adaptive dark floor's verdict (Phase 4, F4-1/HW-4): nits + how
                # many strayed dark reads were σ-verified REAL drift (corrected) vs smoothed.
                digest["dark_floor"] = params["dark_floor"]
            if spec.is_hdr and params.get("level_edge"):
                # The luminance-dependent confirmed gamut edge (D4) fitted from this raw set — evidence here; the
                # run only USES it if the profile enables it and it survives falsification at the 3D-LUT build.
                digest["level_edge"] = build_mhc.level_edge_digest(params["level_edge"])
            top_hold = ((base_lut or {}).get("summary") or {}).get("top_hold")
            if top_hold:
                # Per-channel neutral-cap hold (owner policy 2026-09-23): where each channel's base
                # LUT goes flat and at what drive — the evidence that greys above the calibrated top
                # stay at the D65 cap white instead of walking back toward native.
                digest["top_hold"] = top_hold
            sanity = self._mhc_foundation_sanity_check()
            if sanity:
                digest["sanity"] = sanity
            if not spec.is_hdr:
                reach = self._post_raw_white_reach(params)
                if reach is not None:
                    digest["white_reach_after_raw"] = reach
            return StageOutcome("build-install-mhc", "done", digest=digest,
                                data={"profile_name": profile_name, "verified": verify_ok})

        outcome = self._stage("build-install-mhc", run)
        self._foundation_seam(outcome, stage="build-install-mhc")
        if outcome.status == "done" and not self._spec().is_hdr:
            self._post_raw_white_reach_seam(outcome)
        if outcome.status == "done" and self._spec().is_hdr:
            self._pin_hdr_peak_to_cap(outcome)
        return outcome

    def _install_mhc_params(self, params: dict[str, Any], spec: cp.TargetSpec
                            ) -> tuple[Any, Any, Any]:
        """Install a derived MHC (``mhc_params``) through the controller: set primaries + the
        MEASURED native white -> base 1D-LUT cube (or the 32-point base table) -> apply -> verify.
        Shared by ``build-install-mhc`` and the refine-only flow's ``install-mhc`` (which reinstalls
        a completed run's MHC). Returns ``(applied, verified, resolved_white)``."""
        base = params["base_grayscale"]
        base_lut = params.get("base_lut")
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
        return applied, verified, white

    def _foundation_seam(self, outcome: StageOutcome, *, stage: str) -> None:
        """The MHC install's immediate bright-neutral sanity read collapsed: DETECT in the stage,
        DECIDE here at a seam (``<stage>:foundation``)."""
        sanity = (outcome.digest or {}).get("sanity") or {}
        if sanity.get("critical"):
            # The MHC install succeeded (memoised done) but its immediate bright-neutral read
            # collapsed — DETECT here, DECIDE at the seam. Recommend abort so --auto/supervised
            # stop before the cube build; a live judge can accept if it knows the read was a
            # transient (e.g. the scanout reconfigured mid-read).
            decision = self.adjudicate(AdjudicationRequest(
                key=f"{stage}:foundation", seam=SEAM_FOUNDATION, stage=stage,
                question=((sanity.get("message") or "the MHC foundation read collapsed bright-neutral luminance")
                          + " — abort and recheck the MHC, or accept and continue?"),
                options=("abort", "accept"), recommendation="abort",
                digest={**{k: outcome.digest.get(k) for k in ("profile_name", "white_xy", "verified")},
                        "sanity": sanity, "foundation_critical": True}))
            if decision.choice == "abort":
                self.runlog.stage_aborted(stage, message=sanity.get("message"))
                raise CalibrationAborted(StageOutcome(
                    stage, "aborted",
                    digest={**outcome.digest, "message": sanity.get("message"),
                            "recommendation": "abort_and_recheck_mhc", "decision_note": decision.note}))
            self.ctx.log(f"MHC foundation sanity critical but ACCEPTED at the seam: {decision.note}")

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
                rec = self.calib["stages"].pop(stale, None)
                if stale.startswith("measure:"):
                    self._invalidate_thermal_align(stale, ((rec or {}).get("data") or {}).get("ti3"))
            # The fresh held-out draws were drawn against the training this re-plan just discarded
            # (the training-key check in _held_out_draw_record would catch it too — be explicit).
            self.calib.pop("verify_held_out_draws", None)
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
                                              oog_mapping=self._oog_mapping(),
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

    def _refine_round_analysis(self, samples, ti3_path: Optional[str],
                               previous: Optional[dict[str, Any]], *, white_xy: tuple[float, float],
                               dark_floor_nits: float, top_nits: float,
                               channel_peak_xyz: Sequence[Sequence[float]],
                               materiality: float = refine_convergence.MATERIAL_GAIN_JND,
                               top_anchor: bool = False
                               ) -> dict[str, Any]:
        """Judge one closed-loop grayscale-refine round on physics (:mod:`dlc.refine_convergence`):
        the correctable band (dark floor → the refine's top), each level's physical floor (meter
        repeatability from the round's noise sidecar, the panel's between-rounds wander from the
        run's thermal-alignment track, output quantization at this bit depth through this panel's
        measured primaries), the removable error above it, and the predicted gain of another round
        discounted by the refine's measured efficacy. Replaces the fixed ``target_de`` stop. HDR in
        dE_ITP (absolute PQ targets, top = the Peak-Chroma cap); SDR in CIEDE2000 (power-law
        targets at the SDR white-band luminance ``top_nits``; ``top_anchor`` also judges the white
        level on its own — see :func:`dlc.refine_convergence.analyse_round`). Tests monkeypatch this
        method to script a round."""
        from ._pq import eotf_norm as _pq_eotf, oetf_norm as _pq_oetf
        from .colormath import xy_to_XYZ
        from .measure_loop import match_level_noise

        spec = self._spec()
        hdr = bool(spec.is_hdr)
        # The OUTPUT precision a channel lands on — the MEASURED live link depth (preflight,
        # ``calib['output_depth']``), not the test-pattern depth (``self.bit_depth`` is dogegen's;
        # SDR patterns default to 8-bit while the MHC LUT output still reaches a 10-bit link at its
        # own precision) and not the profile's claim (a 10-bit-profiled panel on an 8 bpc HDMI link).
        bits = self._output_bits()
        code = 1.0 / float(2 ** bits - 1)
        wx, wy = white_xy
        noise = self._dark_noise_entries(ti3_path)
        gamma = float(spec.gamma or 2.2)
        lum_se = self._meter_lum_sigma_rel

        levels: list[refine_convergence.GreyLevel] = []
        below = above = 0
        for smp in samples:
            r, g, b = smp.rgb
            if abs(r - g) > 1e-6 or abs(g - b) > 1e-6:
                continue
            sig = float(r)
            if hdr:
                t_nits = _pq_eotf(sig) * 10000.0
                v = _pq_oetf(min(t_nits, 10000.0) / 10000.0)
                light = _pq_eotf(v)
                rel_step = (_pq_eotf(min(1.0, v + code)) / light - 1.0) if light > 0 else 0.0
            else:
                t_nits = top_nits * max(sig, 0.0) ** gamma
                rel_step = (((sig + code) / sig) ** gamma - 1.0) if sig > 0 else 0.0
            if t_nits < dark_floor_nits:
                below += 1
                continue
            if t_nits > top_nits * (1.0 + 1e-6):
                above += 1       # held above the refine's top by design — not correctable
                continue
            if not smp.xyz or smp.xyz[1] <= 0.0:
                continue
            target = xy_to_XYZ(wx, wy, t_nits)
            q_xy, q_rel = refine_convergence.channel_quantization(target, channel_peak_xyz, rel_step)
            levels.append(refine_convergence.GreyLevel(
                signal=sig, measured_xyz=tuple(float(c) for c in smp.xyz),
                target_xyz=tuple(target), quant_xy=q_xy, quant_rel=q_rel,
                meter_se_xy=(match_level_noise(noise, sig) if noise else None),
                meter_se_rel=lum_se(t_nits)))

        if hdr:
            import numpy as np
            from .engine.model import TargetSpace, de_itp

            def de_fn(m, t) -> float:
                d = TargetSpace.xyz_to_ictcp(np.asarray([m], dtype=float)) \
                    - TargetSpace.xyz_to_ictcp(np.asarray([t], dtype=float))
                return float(de_itp(d)[0])
        else:
            ref = xy_to_XYZ(wx, wy, top_nits)

            def de_fn(m, t) -> float:
                return float(delta_e2000(xyz_to_lab(tuple(m), ref), xyz_to_lab(tuple(t), ref)))

        floor = refine_convergence.panel_floor_from_thermal(self.calib.get("thermal_align"))
        out = refine_convergence.analyse_round(levels, de_fn=de_fn, floor=floor, previous=previous,
                                               materiality=materiality, top_anchor=top_anchor)
        out["excluded"] = {"below_dark_floor": below, "above_top": above}
        out["band_nits"] = [round(dark_floor_nits, 3), round(top_nits, 1)]
        out["output_bits"] = bits
        return out

    def _meter_lum_sigma_rel(self, nits: float) -> Optional[float]:
        """The meter's per-read luminance repeatability (relative) at ``nits`` — the DIP's
        measured noise model, interpolated; per-read, not SE (the conservative side when a
        level's read count is unknown). ``None`` without a DIP noise model."""
        dip = self._dip()
        bands = [b for b in (dip.noise_model if dip else []) if b.sigma_rel is not None]
        if not bands:
            return None
        if nits <= bands[0].nits:
            return bands[0].sigma_rel
        for lo, hi in zip(bands, bands[1:]):
            if lo.nits <= nits <= hi.nits and hi.nits > lo.nits:
                f = (nits - lo.nits) / (hi.nits - lo.nits)
                return lo.sigma_rel + f * (hi.sigma_rel - lo.sigma_rel)
        return bands[-1].sigma_rel

    def _refine_round_judgment(self, *args, **kwargs) -> dict[str, Any]:
        """:meth:`_refine_round_analysis`, guarded: an analysis failure (the HDR path lazy-loads
        the numpy/colour engine) must not abort the refine — it stops the loop as ``unjudged``
        and the LLM decides at the seam, with the error text."""
        try:
            return self._refine_round_analysis(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — surfaced to the LLM via the unjudged seam
            return {"decision": "unjudged", "band_avg": None,
                    "reason": f"convergence analysis failed: {type(exc).__name__}: {exc}"}

    def _clear_refine_seams(self, stage: str) -> None:
        """A refine stage that actually (re-)runs starts with no recorded exit verdicts: a
        decision about a previous execution's exit must never replay onto a new one. (A resume
        replays the memoised stage without re-running it, so a pending verdict still lands.)"""
        for k in ("regression", "safety-ceiling", "floored", "unjudged", "white-band", "thermal-miss"):
            self.calib["decisions"].pop(f"{stage}:{k}", None)

    def _refine_exit_seam(self, outcome: StageOutcome, *, stage: str, label: str,
                          safety_max_rounds: int) -> None:
        """The refine loops' non-routine exits are judgments for the LLM, not code. The best
        measured cube is already installed on every exit; the LLM accepts it or aborts (an
        'abort' ends the flow — it is not a note)."""
        d = outcome.data
        reason = (outcome.digest.get("convergence") or {}).get("reason")
        if d.get("regressed"):
            kind, what = "regression", "regressed (a round made grey worse)"
        elif d.get("safety_ceiling"):
            kind, what = "safety-ceiling", (f"ran {safety_max_rounds} rounds without reaching the "
                                            "panel's physical floor")
        elif d.get("floored"):
            kind, what = "floored", f"stopped removing error: {reason or 'residual above the floor'}"
        elif d.get("unjudged"):
            kind, what = "unjudged", f"could not judge convergence: {reason or 'no evidence'}"
        else:
            return
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key=f"{stage}:{kind}", seam=SEAM_OPTIMIZE, stage=stage,
            question=(f"the {label} closed-loop grayscale refine {what} — the best measured cube "
                      "is installed; accept it, or recheck the panel?"),
            options=("accept", "abort"), recommendation="accept", digest=outcome.digest)),
            stage=stage, message=f"{stage}: aborted at the {kind} seam")

    @staticmethod
    def _refine_round_summary(conv: dict[str, Any]) -> dict[str, Any]:
        """The compact convergence evidence that rides in the round log + the round check-in."""
        keep = ("decision", "reason", "band_avg", "band_max", "predicted_after_avg", "raw_gain",
                "efficacy", "predicted_gain", "cast_xy", "cast_sigma", "cast_real", "lum_gain",
                "lum_gain_real", "band_n")
        out = {k: conv.get(k) for k in keep if k in conv}
        fl = conv.get("floor") or {}
        out["floor_xy"] = fl.get("floor_xy_median")
        if conv.get("top") is not None:
            out["top"] = conv.get("top")       # the white anchor (SDR): its own de / gain / floor
        return out

    # -- the MHC refine in the viewing state (--thermal-state viewing; owner decision 2026-10-09) -----------
    def _refine_measure(self, stage: str, patches: Sequence[tuple[int, int, int]], rnd: int, *,
                        ti3_name: str, ndjson_name: str) -> MeasureLoopResult:
        """One MHC closed-loop refine round's measure. Default (``verify``): exactly today's call. With
        ``--thermal-state viewing`` the refine runs in the VIEWING state (policy ``viewing-refine``): round 1
        asks the thermal-state seam (:meth:`_refine_viewing_plan`); every round then preconditions to the
        viewing band (the start carried from the previous round's modelled end state) and HOLDS it through
        its reads (dim-neutral dwells between ~45 s blocks, bounded by the LLM-chosen dwell budget), and its
        requested-vs-achieved state is recorded (:meth:`_refine_round_record`)."""
        if self._thermal_state() != "viewing":
            return self._measure_set(patches, role=f"refine{rnd}", ti3_name=ti3_name, ndjson_name=ndjson_name)
        plans = self.__dict__.setdefault("_refine_viewing", {})
        if rnd == 1 or stage not in plans:
            plans[stage] = self._refine_viewing_plan(stage, patches)
        st = plans[stage]
        spec = self._refine_round_spec(st, rnd)
        res = self._measure_set(patches, role=f"refine{rnd}", ti3_name=ti3_name, ndjson_name=ndjson_name,
                                viewing=spec)
        self._refine_round_record(st, rnd, spec, res)
        return res

    def _refine_viewing_plan(self, stage: str, patches: Sequence[tuple[int, int, int]]) -> dict[str, Any]:
        """The MHC refine's thermal-state SEAM (``<stage>:thermal-state``): target band, the modelled start
        (+ source), the precondition time, the refine set's own band, the HOLD prediction (dwell per round
        and the total for a typical refine) and the dwell budget (``--viewing-hold-budget-min``, else the
        default shown). precondition = soak to the band + hold every round; measure-now = no soak, no hold
        (tracked, flagged); abort. Nothing here auto-accepts (AutoAdjudicator is sim/CI only)."""
        import time as _time   # local, as elsewhere in this module
        law = viewing_thermal.LoadLaw()
        transfer = self._transfer()
        band = viewing_thermal.set_band(patches, transfer, law)
        target, target_nits, target_src = self._viewing_target(law)
        half = viewing_thermal.BAND_HALFWIDTH_FRAC * target
        start, start_src, start_kind, unmodelled = self._viewing_start(stage, band["load"], law)
        edge = viewing_thermal.CONVERGE_MARGIN * half
        minutes = law.minutes_to_band(start, target, target, edge)
        deadline_s, cap_min, capped = self._precondition_budget(minutes)
        # The hold, modelled with the SAME policy the loop runs: round 1 enters from where a soak from a hotter
        # start lands (the converge edge), later rounds from the held state (~the target).
        first = viewing_thermal.predict_refine_hold(patches, transfer, law, target_load=target, halfwidth=half,
                                                    start_load=(target + edge if start > target else target))
        steady = viewing_thermal.predict_refine_hold(patches, transfer, law, target_load=target, halfwidth=half,
                                                     start_load=target)
        unheld = viewing_thermal.predict_hold(patches, transfer, law, start_load=target, target_load=target,
                                              halfwidth=half)
        n_rounds = viewing_thermal.REFINE_EXPECTED_ROUNDS
        dwell_total = round(first["dwell_min"] + steady["dwell_min"] * (n_rounds - 1), 2)
        default_budget = round(dwell_total * 1.5 + 5.0, 1)
        given_budget = self.calib.get("viewing_hold_budget_min")
        budget_min = float(given_budget) if given_budget is not None else default_budget
        budget_src = ("--viewing-hold-budget-min" if given_budget is not None
                      else "default: the predicted dwell total x 1.5 + 5 min")
        lo, hi = round(target - half, 5), round(target + half, 5)
        tol = self._refine_target_tolerance(target, half)
        digest: dict[str, Any] = {
            "state": "viewing", "requested": "viewing", "policy": viewing_thermal.REFINE_POLICY,
            "owner_decision": ("2026-10-09: the thermal offset goes into the PROFILE through the MHC refine "
                               "(the sole neutral-axis owner), measured in the viewing state; raw + the cube "
                               "build stay at their own (loaded) band; DesktopLUT's White Balance is not used"),
            "target": {"load": round(target, 5), "nits_equiv": round(target_nits, 2), "band": [lo, hi],
                       "source": target_src, "tolerance": tol},
            "start": {"load": round(start, 5), "nits_equiv": round(law.nits_equiv(start), 2), "source": start_src,
                      "kind": start_kind, **({"unmodelled_stages": unmodelled} if unmodelled else {})},
            "predicted": {"precondition_minutes": (round(minutes, 1) if minutes is not None else None),
                          "round_read_minutes": band["minutes"], "set_own_band": band,
                          "unheld_round_from_band": unheld,
                          "hold_round_1": first, "hold_round_n": steady,
                          "hold_dwell_total_min": dwell_total, "rounds_assumed": n_rounds,
                          "note": ("each round also re-runs the viewing-load soak (the per-round preheat, "
                                   "converging in its minimum blocks from a held state); the soak converges at "
                                   "the band's edge, then the dwell field SETTLES the state to the target before "
                                   "the first read (hold_round_1.settle_min) and the hold keeps it there")},
            "precondition_budget": {"deadline_min": round(deadline_s / 60.0, 1), "cap_min": cap_min,
                                    "capped": capped},
            "hold_budget": {"budget_min": budget_min, "source": budget_src, "default_min": default_budget,
                            "max_min": viewing_thermal.HOLD_BUDGET_MAX_MIN,
                            "block_s": viewing_thermal.HOLD_BLOCK_S, "dwell_nits": viewing_thermal.HOLD_DWELL_NITS},
            "model": law.as_dict(), "basis": viewing_thermal.MODEL_BASIS,
            "caveat": ("model predictions (first-order PA32UCXR fit): absolute loads/times +-~2x; the round count "
                       "is unknown up front (the refine stops on the panel's physical floor)"),
        }
        start_fix = ("" if start_kind == "given" else
                     " If you know what the panel showed before (e.g. it sat at the desktop), answer this seam on a "
                     "resume with --viewing-start-nits N (it answers THIS seam only); an under-estimated start "
                     "lets the model claim 'in band' on a hotter panel.")
        question = (
            f"Viewing thermal state for the MHC closed-loop refine ({stage}; policy {viewing_thermal.REFINE_POLICY}, "
            f"owner decision 2026-10-09: the MHC's white/greys are refined at the VIEWING load, so the profile "
            f"carries the thermal offset). Target band {lo}..{hi} load (~{round(target_nits, 1)} nit-equivalent; "
            f"{target_src}). Modelled start {round(start, 4)} ({start_src}).{start_fix} "
            f"precondition = soak a dim neutral stand-in until the MODELLED state is in band "
            f"(~{round(minutes, 1) if minutes is not None else '?'} min predicted; capped at "
            f"{round(deadline_s / 60.0, 1)} min"
            f"{' — the 4-tau cap binds, expect it flagged unmet' if capped else ''}), "
            f"then SETTLE it to the target (the soak converges at the band's edge; the "
            f"{viewing_thermal.HOLD_DWELL_NITS:g}-nit dwell field cools it to the target before the first read, "
            f"~{first.get('settle_min')} min in round 1, model) and HOLD it there through every round: dim-neutral "
            f"dwells between ~{viewing_thermal.HOLD_BLOCK_S:g} s read blocks so block + dwell sit at the viewing "
            f"load, and a block that heats the state past +{tol['load']} load is cut and cooled back to the "
            f"target. The refine counts as AT the target when its reads' mean modelled offset is within "
            f"+-{tol['load']} load ({tol['why_short']}). This refine "
            f"set's own band is {band['load']} (~{band['nits_equiv']} nit-eq, ~{band['minutes']} min of reads per "
            f"round): predicted dwell {first['dwell_min']} min in round 1, {steady['dwell_min']} min per later "
            f"round, ~{dwell_total} min for a {n_rounds}-round refine (model). Dwell budget {budget_min:g} min "
            f"({budget_src}; change it with --viewing-hold-budget-min N on the resume that answers this seam, max "
            f"{viewing_thermal.HOLD_BUDGET_MAX_MIN:g}); past it the rounds ride unheld and a band exit is flagged. "
            "measure-now = no soak and no hold: the refine measures whatever state the panel is in (tracked, and "
            "the MHC's white flagged as refined outside the viewing band). abort = stop the run.")
        decision = self.adjudicate(AdjudicationRequest(
            key=f"{stage}:thermal-state", seam=SEAM_THERMAL_STATE, stage=stage, question=question,
            options=("precondition", "measure-now", "abort"), recommendation="precondition", digest=digest))
        if decision.choice == "abort":
            raise CalibrationAborted(StageOutcome(stage, "aborted", digest={
                "message": "MHC refine viewing thermal-state precondition declined (abort)", "thermal_state": digest,
                "decision_note": decision.note}))
        self._spend_given_start(stage, start_kind)
        return {"law": law, "target": target, "half": half, "target_src": target_src, "start": start,
                "start_src": start_src, "deadline_s": deadline_s, "choice": decision.choice,
                "budget_s": budget_min * 60.0, "used_s": 0.0, "rounds": [], "carried": None, "t_end": None,
                "left_load": None, "clock": getattr(self.measure, "sim_clock", None) or _time.monotonic,
                "digest": {**digest, "decision": decision.choice, "decision_note": decision.note}}

    def _refine_round_spec(self, st: dict[str, Any], rnd: int) -> viewing_thermal.ViewingPrecondition:
        """Round ``rnd``'s precondition + hold: round 1 from the seam's start; later rounds from the previous
        round's modelled end state, relaxed over the compute/install gap toward the reference grey the loop
        left on screen (its final neutral checkpoint). The hold gets the budget the earlier rounds left."""
        law, target, half = st["law"], st["target"], st["half"]
        if rnd == 1 or st["carried"] is None:
            start, src, deadline_s = st["start"], st["start_src"], st["deadline_s"]
        else:
            gap = max(0.0, st["clock"]() - st["t_end"])
            cfg = self._with_preheat(self.loop_config or self._loop_config_for(self._dip()))
            transfer = self._transfer()
            ref_load = law.load(transfer.cv_to_nits(round(cfg.warmup_signal * transfer.max_cv)))
            start = law.relax(float(st["carried"]), ref_load, gap)
            src = (f"carried from refine round {rnd - 1} (its modelled end state"
                   + (f", relaxed over a {gap:.0f} s compute/install gap at the reference grey" if gap > 0 else "")
                   + "; model)")
            minutes = law.minutes_to_band(start, target, target, viewing_thermal.CONVERGE_MARGIN * half)
            deadline_s = self._precondition_budget(minutes)[0]
        soak = st["choice"] == "precondition"
        return viewing_thermal.ViewingPrecondition(
            target_load=target, halfwidth=half, start_load=start, start_source=src, target_source=st["target_src"],
            deadline_s=deadline_s, soak=soak, law=law, hold=soak,
            hold_budget_s=max(0.0, st["budget_s"] - st["used_s"]))

    @staticmethod
    def _viewing_flags(ts: dict[str, Any]) -> list[str]:
        """Requested-vs-achieved evidence flags of one viewing measure pass (the loop's ``thermal_state``)."""
        pre = ts.get("precondition") or {}
        ach = ts.get("achieved")
        flags: list[str] = []
        if pre.get("skipped"):
            flags.append("viewing_precondition_skipped")
        elif not pre.get("reached"):
            flags.append("viewing_precondition_unmet")
        if ach is None:
            flags.append("viewing_state_not_measured")
        elif ach.get("in_band_throughout") is not True:
            flags.append("viewing_band_left")
        return flags

    def _refine_round_record(self, st: dict[str, Any], rnd: int, spec: viewing_thermal.ViewingPrecondition,
                             res: MeasureLoopResult) -> None:
        ts = res.digest.get("thermal_state") or {}
        pre = ts.get("precondition") or {}
        hold = ts.get("hold") or {}
        flags = self._viewing_flags(ts)
        # Where the round's READS were taken relative to the target (model): in band is not enough — a refine
        # read at the band's edge carries that edge's thermal offset into the MHC white.
        tol = self._refine_target_tolerance(st["target"], st["half"])
        at = (ts.get("measure") or {}).get("at_reads")
        offset = None
        if at:
            offset = {**at, "tolerance_load": tol["load"],
                      "near_target": abs(float(at["mean_offset_load"])) <= tol["load"] + 1e-9,
                      "model_white_shift_de_itp": round(
                          viewing_thermal.WHITE_SHIFT_DE_ITP_PER_LOAD * float(at["mean_offset_load"]), 3)}
            if not offset["near_target"]:
                flags.append("viewing_off_target")
        # the budget is counted in the hold's real elapsed seconds (not the rounded minutes)
        st["used_s"] += float(hold["dwell_s"]) if hold.get("dwell_s") is not None else \
            60.0 * float(hold.get("dwell_min") or 0.0)
        final = ts.get("final") or {}
        st["carried"] = final.get("modelled_load")
        st["t_end"] = st["clock"]()
        left = [v for v in ((ts.get("measure") or {}).get("observed_load"), final.get("modelled_load"))
                if v is not None]
        st["left_load"] = max(left) if left else st["left_load"]
        st["rounds"].append({
            "round": rnd, "state": ts.get("state"), "evidence_flags": flags,
            "start": {"load": round(spec.start_load, 5), "source": spec.start_source},
            "precondition": {k: pre.get(k) for k in ("reached", "skipped", "converged", "elapsed_min",
                                                     "modelled_load") if k in pre},
            "achieved": ts.get("achieved"), "measure": ts.get("measure"), "offset_from_target": offset,
            "hold": ({k: hold.get(k) for k in ("blocks", "dwells", "dwell_reads", "dwell_min", "dwell_s",
                                               "budget_min", "budget_exhausted", "exhausted_at_block",
                                               "early_blocks", "settle", "read_capped", "budget_overrun_s")
                      if k in hold} if hold else {"policy": "none (measure-now: no hold)"})})

    def _refine_round_thermal(self, stage: str) -> Optional[dict[str, Any]]:
        """The last refine round's thermal state for its round check-in (viewing runs; else ``None``)."""
        st = (self.__dict__.get("_refine_viewing") or {}).get(stage)
        if self._thermal_state() != "viewing" or not st or not st["rounds"]:
            return None
        r = st["rounds"][-1]
        return {"policy": viewing_thermal.REFINE_POLICY, "round": r["round"], "state": r["state"],
                "evidence_flags": r["evidence_flags"],
                "modelled_range": (r.get("measure") or {}).get("modelled_range"),
                "offset_from_target": r.get("offset_from_target"),
                "band": st["digest"]["target"]["band"], "hold": r.get("hold"),
                "dwell_used_min": round(st["used_s"] / 60.0, 2),
                "dwell_budget_min": round(st["budget_s"] / 60.0, 2)}

    @staticmethod
    def _refine_target_tolerance(target: float, half: float) -> dict[str, Any]:
        """The refine's TARGET tolerance (:data:`viewing_thermal.HOLD_AIM_FRAC` of the half-width): the band
        says the state was viewing-like; this says the refine's reads were taken AT the viewing target."""
        tol = viewing_thermal.HOLD_AIM_FRAC * half
        de = viewing_thermal.WHITE_SHIFT_DE_ITP_PER_LOAD * tol
        return {"load": round(tol, 5), "frac_of_halfwidth": viewing_thermal.HOLD_AIM_FRAC,
                "frac_of_target": round(tol / target, 3) if target > 0 else None,
                "model_white_shift_de_itp": round(de, 3),
                "why_short": f"~{de:.2f} dE_ITP of white shift at the study's scale, model",
                "why": (f"{viewing_thermal.HOLD_AIM_FRAC:g} x the half-width = {tol / target:.0%} of the target "
                        f"load, ~{de:.2f} dE_ITP of content-weighted white shift at the study's PA HDR scale "
                        f"(~{viewing_thermal.WHITE_SHIFT_DE_ITP_PER_LOAD:g} dE_ITP per load unit, model): under half "
                        "the median verify bookend drift the band was sized against — finer than this the "
                        "first-order model (+-~2x absolute) cannot resolve; coarser and the refine's thermal "
                        "offset is no longer small next to the read drift"),
                "basis": viewing_thermal.MODEL_BASIS}

    def _refine_offset_summary(self, st: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The refine's reads vs the target over every round (read-weighted mean modelled offset; model)."""
        rows = [r["offset_from_target"] for r in st["rounds"] if r.get("offset_from_target")]
        n = sum(int(o["reads"]) for o in rows)
        if not n:
            return None
        mean = sum(float(o["mean_offset_load"]) * int(o["reads"]) for o in rows) / n
        mean_abs = sum(float(o["mean_abs_offset_load"]) * int(o["reads"]) for o in rows) / n
        tol = self._refine_target_tolerance(st["target"], st["half"])
        law = st["law"]
        return {"reads": n, "mean_offset_load": round(mean, 5), "mean_abs_offset_load": round(mean_abs, 5),
                "max_abs_offset_load": max(float(o["max_abs_offset_load"]) for o in rows),
                "mean_nits_equiv": round(law.nits_equiv(st["target"] + mean), 2),
                "target_nits_equiv": round(law.nits_equiv(st["target"]), 2),
                "model_white_shift_de_itp": round(viewing_thermal.WHITE_SHIFT_DE_ITP_PER_LOAD * mean, 3),
                "tolerance_load": tol["load"], "tolerance_why": tol["why"],
                "near_target": abs(mean) <= tol["load"] + 1e-9,
                "per_round": [round(float(o["mean_offset_load"]), 5) for o in rows],
                "basis": viewing_thermal.MODEL_BASIS}

    def _refine_thermal_finish(self, stage: str) -> Optional[dict[str, Any]]:
        """The MHC refine stage's ``thermal_state`` (viewing runs; ``None`` otherwise / nothing measured):
        policy ``viewing-refine``, requested vs ACHIEVED over every round. Labelled ``viewing`` only when every
        round's precondition reached the band and its reads stayed in it (model); anything else is
        ``outside-viewing-band`` with the flags, and the MHC's white is flagged as refined outside the band —
        carried to the verify and ``verify:accept`` (never a silently "viewing" profile). Notes the state the
        refine left the panel in on the run's thermal history."""
        st = (self.__dict__.get("_refine_viewing") or {}).get(stage)
        if self._thermal_state() != "viewing" or not st or not st["rounds"]:
            return None
        flags = sorted({f for r in st["rounds"] for f in r["evidence_flags"]})
        band_flags = [f for f in flags if f != "viewing_off_target"]
        rounds_in_band = sum(1 for r in st["rounds"] if not [f for f in r["evidence_flags"]
                                                             if f != "viewing_off_target"])
        rounds_on_target = sum(1 for r in st["rounds"] if not r["evidence_flags"])
        exhausted = next((r["round"] for r in st["rounds"] if (r.get("hold") or {}).get("budget_exhausted")), None)
        overrun = round(sum(float((r.get("hold") or {}).get("budget_overrun_s") or 0.0) for r in st["rounds"]), 1)
        offset = self._refine_offset_summary(st)
        off_txt = ("no measurement read to place against the target" if offset is None else
                   f"mean modelled offset from the target {offset['mean_offset_load']:+.4f} load "
                   f"(~{offset['model_white_shift_de_itp']:+.2f} dE_ITP of white shift at the study's scale), "
                   f"tolerance +-{offset['tolerance_load']}")
        if band_flags:
            state = "outside-viewing-band"
            white = (f"refined OUTSIDE the viewing band ({', '.join(flags)}; {off_txt}; model) — the MHC's "
                     "white/greys describe another thermal state than viewing")
        elif flags:
            state = "viewing-band-off-target"
            white = (f"refined in the viewing band but OFF its target (viewing_off_target; {off_txt}; model) — the "
                     "MHC's white/greys carry that offset's thermal shift")
        else:
            state = "viewing"
            white = f"refined in the viewing band at its target ({off_txt}; model)"
        rec = {
            **st["digest"],
            "state": state,
            "achieved": {"rounds": len(st["rounds"]), "rounds_in_band": rounds_in_band,
                         "rounds_on_target": rounds_on_target, "all_rounds_in_band": not band_flags,
                         "all_rounds_on_target": not flags, "basis": viewing_thermal.MODEL_BASIS},
            "offset_from_target": offset,
            "hold_total": {"dwell_min": round(st["used_s"] / 60.0, 2),
                           "budget_min": round(st["budget_s"] / 60.0, 2),
                           "budget_exhausted_in_round": exhausted,
                           **({"budget_overrun_s": overrun} if overrun > 0 else {})},
            "rounds": st["rounds"], "evidence_flags": flags, "needs_adjudication": bool(flags),
            "mhc_white": white,
        }
        self._note_thermal_history(stage, st["left_load"],
                                   basis="max(observed load, modelled end state) of the last refine round")
        return rec

    def _refine_thermal_miss_seam(self, outcome: StageOutcome, *, stage: str) -> bool:
        """A refine that asked for the viewing state but ran (partly) outside it is a judgment for the LLM:
        accept (keep the MHC, flagged — carried to verify:accept + the report), remeasure (re-run the refine;
        its thermal-state seam re-asks with the start from this run's history — returns True) or abort.
        Not raised when the LLM itself chose measure-now (it knowingly measured the state as-is; the flags
        still ride every digest)."""
        ts = (outcome.digest or {}).get("thermal_state") or {}
        flags = ts.get("evidence_flags") or []
        if not flags or ts.get("decision") == "measure-now":
            return False
        key = f"{stage}:thermal-miss"
        ach, held = ts.get("achieved") or {}, ts.get("hold_total") or {}
        off = ts.get("offset_from_target") or {}
        where = ("ran outside it" if ts.get("state") == "outside-viewing-band" else
                 "stayed in its band but read OFF its target")
        question = (
            f"the MHC closed-loop refine ({stage}) asked for the VIEWING thermal state but {where} "
            f"({', '.join(flags)}; {ach.get('rounds_in_band')}/{ach.get('rounds')} rounds in band, "
            f"{ach.get('rounds_on_target')}/{ach.get('rounds')} at the target; mean modelled offset from the target "
            f"{off.get('mean_offset_load')} load vs tolerance +-{off.get('tolerance_load')}; dwell "
            f"{held.get('dwell_min')} of {held.get('budget_min')} min budget; model). The installed MHC's white/greys "
            "were refined in another state than the viewing target. accept = keep this MHC, flagged (carried to "
            "verify:accept and the report); remeasure = re-run the refine from the "
            "build's base cube — its thermal-state seam re-asks (start from this run's history; answer it with "
            "--viewing-start-nits / --viewing-hold-budget-min on that resume to change them); abort = stop the run.")
        decision = self._abort_if(self.adjudicate(AdjudicationRequest(
            key=key, seam=SEAM_THERMAL_STATE, stage=stage, question=question,
            options=("accept", "remeasure", "abort"), recommendation="accept",
            # read_anomaly: like the measure loop's own viewing miss, a judgment --supervised must escalate
            # (never a benign auto-accept of a profile refined in another state than the one requested)
            digest={**ts, "read_anomaly": True})),
            stage=stage, message=f"{stage}: aborted at the viewing thermal-state miss seam")
        if decision.choice != "remeasure":
            return False
        # One remeasure buys exactly one re-run: drop the stage memo + both decisions (record, seed, override)
        # so the re-run re-asks its thermal-state seam over fresh numbers and a second miss pauses again —
        # with the start from this run's history: a --viewing-start-nits this seam took is spent for good.
        self._spend_given_start_on_remeasure(stage, "thermal-miss remeasure")
        self.calib["stages"].pop(stage, None)
        self._forget_decision(key, overrides=True)
        self._forget_decision(f"{stage}:thermal-state", overrides=True)
        self._save()
        return True

    def _thermal_stage_summary(self) -> Optional[dict[str, Any]]:
        """Which stage ran in which thermal state (viewing runs; ``None`` when no stage recorded a viewing
        request): the build stages (own band, viewing not applied), the MHC refine (policy viewing-refine,
        requested vs achieved) and the verify (practical). For the verify digest and the final report."""
        stages = self.calib.get("stages") or {}
        rows: list[dict[str, Any]] = []
        for key, rec in stages.items():
            ts = (rec.get("digest") or {}).get("thermal_state")
            if key == "verify" or not isinstance(ts, dict) or ts.get("requested") != "viewing":
                continue
            policy = ts.get("policy") or ("practical (viewing precondition)" if key == THERMAL_STATE_STAGE
                                          else "viewing")
            flags = ts.get("evidence_flags")
            if flags is None and ts.get("policy") != "own":
                flags = self._viewing_flags(ts)
            rows.append({"stage": key, "requested": "viewing", "policy": policy, "state": ts.get("state"),
                         "applied": ts.get("applied", True), "decision": ts.get("decision"),
                         "evidence_flags": flags or []})
        if not rows:
            return None
        verify_ts = ((stages.get(THERMAL_STATE_STAGE) or {}).get("digest") or {}).get("thermal_state")
        verify_viewing = any(r["stage"] == THERMAL_STATE_STAGE for r in rows)
        if THERMAL_STATE_STAGE in stages and not verify_viewing:
            # the verify did NOT request viewing although an earlier stage did: say so (never "practical")
            rows.append({"stage": THERMAL_STATE_STAGE, "requested": (verify_ts or {}).get("requested") or "verify",
                         "policy": "own band (thermal-state verify)",
                         "state": (verify_ts or {}).get("state") or "verify", "applied": True,
                         "decision": (verify_ts or {}).get("decision"), "evidence_flags": []})
        refine = next((r for r in rows if r["stage"] in REFINE_THERMAL_STAGES), None)
        refine_ts = (((stages.get(refine["stage"]) or {}).get("digest") or {}).get("thermal_state") or {}
                     if refine else {})
        verify_txt = ("the verify practical (viewing precondition)" if verify_viewing else
                      "the verify at its own band (thermal-state verify)" if THERMAL_STATE_STAGE in stages else
                      "the verify not measured yet")
        return {"requested": "viewing",
                "owner_policy": ("2026-10-09: raw + the cube build in the loaded own-band state (viewing not "
                                 "applied); the MHC closed-loop refine in the viewing state (viewing-refine: "
                                 f"precondition + settle + hold at the target); {verify_txt}"),
                "stages": rows,
                "mhc_white": (None if refine is None else
                              {"stage": refine["stage"], "state": refine["state"],
                               "evidence_flags": refine["evidence_flags"],
                               **({"line": refine_ts["mhc_white"]} if refine_ts.get("mhc_white") else {}),
                               **({"offset_from_target": refine_ts["offset_from_target"]}
                                  if refine_ts.get("offset_from_target") else {})}),
                "basis": viewing_thermal.MODEL_BASIS}

    def stage_refine_mhc_cube(self, *, materiality: float = refine_convergence.MATERIAL_GAIN_JND,
                              regress_tol: float = 0.5, safety_max_rounds: int = 40
                              ) -> StageOutcome:
        """Closed-loop grayscale refine of the HDR MHC base cube toward STANDALONE D65.

        Each round: measure the neutral ramp with the current cube applied, score grey vs D65
        (dE_ITP), and — unless already floored — pull the cube toward D65 at the Peak-Chroma cap
        (``mhc_cube.refine_hdr_cube``) and reinstall. This makes the ICC a self-sufficient D65
        foundation (see [[mhc-standalone-d65-peakchroma]] / [[dlc-corrections-stack-independently]]),
        independent of the optional 3D LUT.

        **No fixed target, no arbitrary round cap (DESIGN LAW).** Mirrors the SDR sibling
        (:meth:`stage_refine_mhc_grayscale`): each round is judged on physics
        (:meth:`_refine_round_analysis` / :mod:`dlc.refine_convergence`) — it stops when another
        round is predicted to gain less than ``materiality`` (a quarter JND) because what remains is
        within the panel's physical floor (``converged``), or when a real, material residual remains
        that the refine demonstrably can't remove (``floored`` → LLM seam) — or REGRESSES (revert +
        LLM seam). (The old fixed 2.0 target accepted round 1 of the 2026-09-24 run at 1.26 and left
        a 6σ uniform cool cast.) ``safety_max_rounds`` is a backstop for a pathological panel: NOT a
        silent cap — it reverts to best and raises a seam. A UNIFIED best-revert reinstalls the best
        measured cube (by the correctable-band ΔE) on EVERY terminal exit. Each round emits a
        non-blocking check-in carrying the round's evidence + decision (the LLM may cancel via
        ``control.json``); the FINAL acceptance is the verify seam. HDR only; SDR / non-1D-LUT base
        ⇒ no-op.
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

            # The neutral ramp every round measures: the uniform ramp + data-driven pins just under the
            # cap where the BUILD's base cube bends most (the refine interpolates its factors linearly
            # between pins, so the steep near-peak segment needs denser pins). Derived from the build's
            # cube (mhc_base_<mode>.cube, stable across rounds/resume) — never a refine output.
            refine_patches, top_pins = self._refine_neutral_patches(
                base_cube if base_cube.exists() else Path(cube_path), rowsums, cap_nits)

            self._clear_refine_seams("refine-mhc-cube")
            scores: list[float] = []
            rounds_log: list[dict[str, Any]] = []
            installed = cube_path
            best_path, best_avg = cube_path, float("inf")
            flags: dict[str, bool] = {}
            conv: Optional[dict[str, Any]] = None       # the previous round's physics judgment

            rnd = 0
            while True:
                rnd += 1
                res = self._refine_measure("refine-mhc-cube", refine_patches, rnd,
                                           ti3_name=f"refine_{rnd}.ti3",
                                           ndjson_name=f"refine_{rnd}.ndjson")
                samples = parse_ti3(Path(res.ti3_path)) if res.ti3_path else []
                grey = [s for s in samples
                        if abs(s.rgb[0] - s.rgb[1]) < 1e-6 and abs(s.rgb[1] - s.rgb[2]) < 1e-6]
                de = self._grey_de_vs_white(samples, (wx, wy))
                # The physics judgment of this round (correctable band, floor, removable error,
                # predicted gain × measured efficacy) — the loop's stop rule and its score.
                conv = self._refine_round_judgment(
                    samples, res.ti3_path, conv, white_xy=(wx, wy), dark_floor_nits=dark_floor,
                    top_nits=float(cap_nits), channel_peak_xyz=channel_peak_xyz,
                    materiality=materiality)
                score = conv.get("band_avg") if conv.get("band_avg") is not None else de["avg"]
                summary = self._refine_round_summary(conv)
                rounds_log.append({"round": rnd, "grey_avg_de_itp": de["avg"],
                                   "grey_max_de_itp": de["max"], "grey_n": de["n"],
                                   "gamma_err_pct": de["gamma_err_pct"], "cube": Path(installed).name,
                                   "convergence": summary})
                # Feed the round's grayscale quality + the physics judgment to the round check-in so
                # the LLM judges each round as it lands (not metric-blind mid-run). ``since_last_round``
                # = improvement of the correctable-band ΔE over the previous round (+ve = converging).
                prev_avg = scores[-1] if scores else None   # scores not yet appended this round
                cur_best = (min(best_avg, score) if score is not None else best_avg)
                self._last_refine = {
                    "round": rnd, "grey_avg_de_itp": de["avg"], "grey_max_de_itp": de["max"],
                    "gamma_err_pct": de["gamma_err_pct"], "grey_n": de["n"],
                    "band_avg_de_itp": score,
                    "best_avg_de_itp": (round(cur_best, 3) if cur_best != float("inf") else None),
                    "since_last_round": (round(prev_avg - score, 3)
                                         if prev_avg is not None and score is not None else None),
                    "convergence": summary}
                round_ts = self._refine_round_thermal("refine-mhc-cube")   # viewing runs only
                if round_ts is not None:
                    self._last_refine["thermal_state"] = round_ts
                # A round's result is itself new evidence — emitted as it lands, not on the timer.
                self._emit_checkin("refine-mhc-cube", "refine_round")
                if score is None:
                    flags["unscored"] = True
                    break
                if conv.get("decision") == "unjudged":
                    flags["unjudged"] = True             # no trustworthy evidence → LLM seam
                    break
                scores.append(score)
                if score < best_avg:
                    best_avg, best_path = score, installed

                # --- stop conditions: the PANEL'S PHYSICAL FLOOR decides, not a fixed target or an
                # arbitrary round count (DESIGN LAW). Each just sets a flag + breaks; the UNIFIED
                # best-revert after the loop reinstalls the best measured cube on EVERY exit. ---
                if len(scores) >= 2 and scores[-1] > scores[-2] + regress_tol:
                    flags["regressed"] = True            # a round made grey WORSE → revert + LLM seam
                    break
                if conv.get("decision") == "converged":
                    flags["converged"] = True            # the rest is within the floor / immaterial
                    break
                if conv.get("decision") == "floored":
                    flags["floored"] = True              # real residual the refine can't remove → seam
                    break
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
                    peak_cap_nits=cap_nits, target_white_xy=(wx, wy), dark_floor_nits=dark_floor,
                    top_hold=self.mhc_top_hold)
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

            final_avg = rounds_log[-1]["grey_avg_de_itp"] if rounds_log else None
            digest = {"rounds": len(rounds_log), "round_log": rounds_log,
                      "grey_avg_de_itp": final_avg,
                      "band_avg_de_itp": scores[-1] if scores else None,
                      "best_band_avg_de_itp": (
                          round(best_avg, 3) if best_avg != float("inf") else None),
                      "convergence": conv, "materiality": materiality,
                      "cap_nits": cap_nits, "binding_channel": peak_chroma.get("binding_channel"),
                      "final_cube": Path(installed).name,
                      "top_hold": self.mhc_top_hold, "top_pins": top_pins,
                      "neutral_patches_per_round": len(refine_patches), **flags}
            thermal = self._refine_thermal_finish("refine-mhc-cube")      # viewing runs only
            if thermal is not None:
                digest["thermal_state"] = thermal
            return StageOutcome("refine-mhc-cube", "done", digest=digest,
                                data={"rounds": len(rounds_log), "regressed": bool(flags.get("regressed")),
                                      "safety_ceiling": bool(flags.get("safety_ceiling")),
                                      "floored": bool(flags.get("floored")),
                                      "unjudged": bool(flags.get("unjudged")),
                                      "final_avg": final_avg})

        outcome = self._stage("refine-mhc-cube", run)
        if self._refine_thermal_miss_seam(outcome, stage="refine-mhc-cube"):
            return self.stage_refine_mhc_cube(materiality=materiality, regress_tol=regress_tol,
                                              safety_max_rounds=safety_max_rounds)
        self._refine_exit_seam(outcome, stage="refine-mhc-cube", label="HDR",
                               safety_max_rounds=safety_max_rounds)
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

    def stage_refine_mhc_grayscale(self, *,
                                   materiality: float = refine_convergence.MATERIAL_GAIN_JND,
                                   regress_tol: float = 0.3, safety_max_rounds: int = 40
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

        **No fixed target, no arbitrary round cap (DESIGN LAW).** Each round is judged on physics
        (:meth:`_refine_round_analysis` / :mod:`dlc.refine_convergence`, CIEDE2000 here): the loop stops
        when another round is predicted to gain less than ``materiality`` (a quarter JND) because what
        remains is within the panel's physical floor (``converged``), or when a real, material residual
        remains that the refine demonstrably can't remove (``floored`` → LLM seam). A REGRESSION (a
        refine made grey worse than ``regress_tol``) reverts to the best measured cube and raises a
        seam for the LLM. ``safety_max_rounds`` is a backstop for a pathological
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
            from .mhc_cube import (choose_sdr_white_nits, mhc2_matrix, read_1d_cube, refine_sdr_cube,
                                   retarget_sdr_white, sdr_white_reach, write_1d_cube)

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

            self._clear_refine_seams("refine-mhc-grayscale")
            scores: list[float] = []
            rounds_log: list[dict[str, Any]] = []
            installed_path = str(base_cube)                    # currently applied base cube
            best_path, best_avg = str(base_cube), float("inf")
            flags: dict[str, bool] = {}
            conv: Optional[dict[str, Any]] = None       # the previous round's physics judgment
            channel_peak_xyz = params.get("channel_peak_xyz")
            if not channel_peak_xyz:
                # SDR records no per-channel peaks: the native primaries balanced to the measured
                # native white at the target luminance give the same linear-share basis.
                from .colormath import rgb_to_xyz_matrix
                m = rgb_to_xyz_matrix(primaries["rx"], primaries["ry"], primaries["gx"],
                                      primaries["gy"], primaries["bx"], primaries["by"],
                                      nwx, nwy, white_Y=float(peak))
                channel_peak_xyz = [[m[row][c] for row in range(3)] for c in range(3)]

            # --- SDR WHITE BAND (owner rule 2026-09-25): white may sit anywhere in [lo, hi] nits;
            # the refine targets the brightest white at which EVERY channel has the headroom to hit
            # the target white (the minimum dimming), clamped into the band. Without it the target
            # white was the native full-drive luminance (target_luminance), which is unreachable at
            # D65 whenever a rowsum exceeds 1 (a channel asked for more than full drive — the
            # PA32UCXR 2026-09-25 green 1.0065): white stayed ~1 dE off while the greys converged.
            # The target sits a PHYSICAL margin below the reach (white-read σ ⊕ settled thermal
            # wander ⊕ one output code), so the limiting channel never sits at exactly full drive.
            # The model reach (rowsums) seeds it; the MEASURED reach (every round's white) refines
            # it through a noise-robust estimate (retarget_sdr_white: the mean of the reads, moved
            # only on a > 3σ change), so one noisy read neither ratchets white down nor resets the
            # judge. Exact target white below the band is not code's call: ``below_band`` -> the
            # white-band seam (never auto-accepted). ---
            band, band_source = self._sdr_white_band()
            margin_terms = self._sdr_white_margin(float(peak))   # shared with the brightness forecast
            code_rel = margin_terms["code_rel"]            # one output code's light step at white
            meter_rel = margin_terms["meter_rel"]
            drift_rel = margin_terms["drift_rel"]
            margin_rel = margin_terms["rel"]
            model_reach = sdr_white_reach(primaries, (nwx, nwy), float(peak), rowsums,
                                          gamma=gamma, target_white_xy=(wx, wy))
            wb_choice = choose_sdr_white_nits(model_reach["reach_nits"], band, float(peak),
                                              margin_rel=margin_rel)
            white_nits = float(wb_choice["white_nits"])
            white_band: dict[str, Any] = {
                "band": [band[0], band[1]], "band_source": band_source,
                "hi_effective": wb_choice.get("hi_effective"),
                "native_peak_nits": round(float(peak), 4), "model_reach": model_reach,
                "reach_nits": wb_choice.get("reach_nits"), "reach_basis": model_reach.get("basis"),
                "limiting_channel": model_reach.get("limiting_channel"),
                "white_nits": round(white_nits, 4), "status": wb_choice["status"],
                "dim_pct_vs_native": wb_choice.get("dim_pct_vs_native"),
                "margin": {"rel": round(margin_rel, 6),
                           "meter_rel": (round(meter_rel, 6) if meter_rel is not None else None),
                           "drift_rel": round(drift_rel, 6), "code_rel": round(code_rel, 6)},
                "retargets": 0, "reach_log": []}
            measured_reaches: list[float] = []

            rnd = 0
            while True:
                rnd += 1
                res = self._refine_measure("refine-mhc-grayscale", self._neutral_patches(), rnd,
                                           ti3_name=f"refine_{rnd}.ti3",
                                           ndjson_name=f"refine_{rnd}.ndjson")
                samples = parse_ti3(Path(res.ti3_path)) if res.ti3_path else []
                grey = [s for s in samples
                        if abs(s.rgb[0] - s.rgb[1]) < 1e-6 and abs(s.rgb[1] - s.rgb[2]) < 1e-6]
                de = self._grey_de_sdr(samples, (wx, wy))
                # The measured white refines the reach (see the white-band block above).
                top_s = max(grey, key=lambda smp: smp.rgb[0]) if grey else None
                if (top_s is not None and top_s.rgb[0] >= 1.0 - 1e-6 and top_s.xyz
                        and top_s.xyz[1] > 0.0):
                    try:
                        mreach = sdr_white_reach(
                            primaries, (nwx, nwy), float(peak), rowsums, gamma=gamma,
                            target_white_xy=(wx, wy), measured_top_xyz=tuple(top_s.xyz),
                            top_signal=float(top_s.rgb[0]),
                            current_curves=read_1d_cube(Path(installed_path)))
                    except Exception as exc:  # noqa: BLE001 - evidence; the model reach stands
                        mreach = {"reach_nits": None, "basis": "error",
                                  "error": f"{type(exc).__name__}: {exc}"}
                    log = {"round": rnd, "white_Y": round(top_s.xyz[1], 4), **mreach}
                    if mreach.get("basis") == "measured" and mreach.get("reach_nits"):
                        measured_reaches.append(float(mreach["reach_nits"]))
                        first = not white_band.get("measured")
                        rt = retarget_sdr_white(measured_reaches, white_nits, band, float(peak),
                                                meter_rel=meter_rel, drift_rel=drift_rel,
                                                code_rel=code_rel, first=first)
                        log["retarget"] = {k: rt.get(k) for k in
                                           ("retarget", "estimate_nits", "n", "sigma_nits",
                                            "delta_nits", "threshold_nits", "white_nits", "status")}
                        if rt["retarget"]:
                            new_nits = float(rt["white_nits"])
                            if not first and new_nits != white_nits:
                                # A SIGNIFICANT change of the reach estimate: earlier rounds were
                                # judged against a different white — restart the judgment + best
                                # tracking from here.
                                white_band["retargets"] += 1
                                conv, scores = None, []
                                best_path, best_avg = installed_path, float("inf")
                            white_nits = new_nits
                            white_band.update(
                                measured=True, white_nits=round(white_nits, 4),
                                status=rt["status"], reach_nits=rt.get("reach_nits"),
                                reach_basis="measured", reach_n=rt.get("n"),
                                reach_sigma_nits=rt.get("sigma_nits"),
                                usable_nits=rt.get("usable_nits"),
                                limiting_channel=mreach.get("limiting_channel"),
                                dim_pct_vs_native=rt.get("dim_pct_vs_native"))
                    white_band["reach_log"].append(log)
                conv = self._refine_round_judgment(
                    samples, res.ti3_path, conv, white_xy=(wx, wy), dark_floor_nits=dark_floor,
                    top_nits=float(white_nits), channel_peak_xyz=channel_peak_xyz,
                    materiality=materiality, top_anchor=True)
                score = conv.get("band_avg") if conv.get("band_avg") is not None else de["avg"]
                summary = self._refine_round_summary(conv)
                rounds_log.append({"round": rnd, "grey_avg_de2000": de["avg"],
                                   "grey_max_de2000": de["max"], "grey_n": de["n"],
                                   "gamma_err_pct": de["gamma_err_pct"], "convergence": summary})
                # Feed the round's grayscale quality + the physics judgment to the round check-in
                # (non-blocking evidence) so a multi-round refine isn't metric-blind mid-run;
                # since_last_round = the correctable-band improvement (prev - this; +ve = converging).
                prev_avg = scores[-1] if scores else None
                cur_best = (min(best_avg, score) if score is not None else best_avg)
                self._last_refine = {
                    "round": rnd, "grey_avg_de2000": de["avg"], "grey_max_de2000": de["max"],
                    "gamma_err_pct": de["gamma_err_pct"], "grey_n": de["n"],
                    "band_avg_de2000": score,
                    "best_avg_de2000": (round(cur_best, 3) if cur_best != float("inf") else None),
                    "since_last_round": (round(prev_avg - score, 3)
                                         if prev_avg is not None and score is not None else None),
                    "convergence": summary,
                    "white_nits": round(white_nits, 3), "white_band_status": white_band["status"]}
                round_ts = self._refine_round_thermal("refine-mhc-grayscale")   # viewing runs only
                if round_ts is not None:
                    self._last_refine["thermal_state"] = round_ts
                self._emit_checkin("refine-mhc-grayscale", "refine_round")
                if score is None:
                    flags["unscored"] = True
                    break
                if conv.get("decision") == "unjudged":
                    flags["unjudged"] = True             # no trustworthy evidence → LLM seam
                    break
                scores.append(score)
                if score < best_avg:
                    best_avg, best_path = score, installed_path

                # --- stop conditions: the PANEL'S PHYSICAL FLOOR decides, not a fixed target or an
                # arbitrary round count (DESIGN LAW: adapt until the monitor can give no more). Each just
                # sets a flag + breaks; the UNIFIED best-revert below leaves the best measured cube
                # installed on EVERY exit — so a within-tolerance uptick that doesn't trip the regression
                # gate can't strand a worse-than-best (even worse-than-identity) correction (round 1
                # always measures identity, so best is identity-or-better). ---
                if len(scores) >= 2 and scores[-1] > scores[-2] + regress_tol:
                    flags["regressed"] = True            # a round made grey WORSE → revert + LLM seam
                    break
                if conv.get("decision") == "converged":
                    flags["converged"] = True            # the rest is within the floor / immaterial
                    break
                if conv.get("decision") == "floored":
                    flags["floored"] = True              # real residual the refine can't remove → seam
                    break
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
                    peak, rowsums, gamma=gamma, target_white_xy=(wx, wy), dark_floor_nits=dark_floor,
                    target_white_nits=white_nits)
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

            final_avg = rounds_log[-1]["grey_avg_de2000"] if rounds_log else None
            digest = {"rounds": len(rounds_log), "round_log": rounds_log,
                      "grey_avg_de2000": final_avg,
                      "band_avg_de2000": scores[-1] if scores else None,
                      "best_band_avg_de2000": (
                          round(best_avg, 3) if best_avg != float("inf") else None),
                      "convergence": conv, "materiality": materiality,
                      "rowsums": [round(v, 5) for v in rowsums],
                      "white_band": white_band, **flags}
            if white_band["status"] == "below_band":
                digest["white_band_below"] = True
            thermal = self._refine_thermal_finish("refine-mhc-grayscale")  # viewing runs only
            if thermal is not None:
                digest["thermal_state"] = thermal
            # The white the refine delivered (what the 3D LUT / verify now sit on): recorded on the
            # MHC params so a later flow (and the applied-stack registry) sees the chosen luminance.
            params["sdr_white"] = {"white_nits": white_band["white_nits"],
                                   "status": white_band["status"], "band": white_band["band"],
                                   "reach_nits": white_band.get("reach_nits")}
            self._state["mhc_params"] = params
            _common.save_dlc_state(self.ctx, self._state)
            return StageOutcome("refine-mhc-grayscale", "done", digest=digest,
                                data={"rounds": len(rounds_log),
                                      "regressed": bool(flags.get("regressed")),
                                      "safety_ceiling": bool(flags.get("safety_ceiling")),
                                      "floored": bool(flags.get("floored")),
                                      "unjudged": bool(flags.get("unjudged")),
                                      "white_band_below": white_band["status"] == "below_band",
                                      "white_nits": white_band["white_nits"],
                                      "final_avg": final_avg})

        outcome = self._stage("refine-mhc-grayscale", run)
        if self._refine_thermal_miss_seam(outcome, stage="refine-mhc-grayscale"):
            return self.stage_refine_mhc_grayscale(materiality=materiality, regress_tol=regress_tol,
                                                   safety_max_rounds=safety_max_rounds)
        self._refine_exit_seam(outcome, stage="refine-mhc-grayscale", label="SDR",
                               safety_max_rounds=safety_max_rounds)
        self._white_band_seam(outcome)
        return outcome

    def _sdr_white_band(self) -> tuple[tuple[float, float], str]:
        """The SDR white-luminance band ``(lo, hi)`` + its source: a run-level override
        (``--white-band``, persisted in the run record so a resume keeps it) beats the target's
        ``white_nits_band`` profile key, which beats the default fraction of the nominal white."""
        override = self.calib.get("white_band_override")
        if override:
            return (float(override[0]), float(override[1])), "run_override"
        spec = self._spec()
        return spec.sdr_white_band, spec.sdr_white_band_source

    def _white_band_seam(self, outcome: StageOutcome) -> None:
        """Exact target white exists only BELOW the SDR white band: the refine delivered it there
        (the margined reach) because no in-band luminance can give it. Keeping that below-band
        white — trading more luminance than the owner's band authorizes — is the owner's call, so
        the recommendation is deliberately NON-benign (``accept_below_band``): the seam always
        pauses for the LLM, even under ``--supervised`` (never an auto-accept). ``abort`` ends the
        flow; a different band is a re-run with ``--white-band LO,HI``."""
        wb = (outcome.digest or {}).get("white_band") or {}
        if wb.get("status") != "below_band":
            return
        top = ((outcome.digest.get("convergence") or {}).get("top") or {})
        lo, hi = (wb.get("band") or [None, None])[:2]
        question = (
            f"an exact target white is only reachable at {wb.get('reach_nits')} nits "
            f"({wb.get('limiting_channel')} channel at full drive, {wb.get('reach_basis')} reach) — "
            f"BELOW the SDR white band [{lo}, {hi}] nits (native peak {wb.get('native_peak_nits')}). "
            f"The refine delivered white at {wb.get('white_nits')} nits (the reach less a "
            f"{(wb.get('margin') or {}).get('rel')} relative physical margin; white now "
            f"{top.get('de')} dE2000 off target). 'accept_below_band' keeps this dimmer, exact "
            "white (the verify/apply gate still follows); 'abort' ends the run with nothing applied "
            "— re-run with --white-band LO,HI to choose a different band.")
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key="refine-mhc-grayscale:white-band", seam=SEAM_OPTIMIZE, stage="refine-mhc-grayscale",
            question=question, options=("accept_below_band", "abort"),
            recommendation="accept_below_band",
            digest={**outcome.digest, "owner_band_exceeded": True})),
            stage="refine-mhc-grayscale",
            message="refine-mhc-grayscale: aborted at the white-band seam (exact white is below the band)")

    def stage_grayscale_wb_touchup(self, *, target_de: float = 0.6,
                                   max_rounds_per_point: int = 6) -> StageOutcome:
        """Patch-by-patch main-GUI Grayscale touch-up — automates DesktopLUT's live editor.

        Mirrors the manual "Edit Points → adjust → OK" workflow over the pipe: ``grayscale_live_begin``
        engages the preview shader (the MHC ``correctionGrayscale`` stacks live on top of MHC+3D-LUT so
        the meter SEES it — render.cpp:346 ``corrGsPreviewActive`` gate), then per editor grey point we
        measure → nudge the point's R/G/B live → re-measure, move on; ``grayscale_commit`` (the
        editor's "OK") bakes the result into the ICM at the END of this stage so ``measure:verify``
        scores the REAL baked result (the live preview is only bit-identical to the bake on the SDR
        realization-A path — HDR previews light that differs, so verifying the preview would ship an
        unverified deliverable). Revert is DLC-owned (fable Phase 7a, Design B): the pre-begin
        correctionGrayscale is snapshotted to dlc_state before the edit, and ``verify:accept =
        revert`` re-applies it (``_restore_correction_grayscale``) rather than relying on the C++
        ``grayscale_cancel`` (a no-op once commit has run) — robust across a DesktopLUT restart.
        This is the toggleable third "+1": the core (matrix + base grayscale + 3D LUT) is never
        touched, and the result is one-toggle revertible to the user's prior correction. Requires the live preview path
        (CODEX_GRAYSCALE_LIVE_EDIT_PROMPT.md); the old ``set_grayscale_tweak`` overlay was a no-op
        under an active MHC (it wrote the wrong store, ``cc.grayscale``, suppressed by render.cpp:346).
        """
        def run() -> StageOutcome:
            from .grayscale_wb import (
                GrayTouchupConfig,
                GrayTouchupPatch,
                compose_payload,
                identity_payload,
                point_error,
                summarize_errors,
                update_point,
            )

            patches = self._grayscale_wb_patches()
            if not patches:
                return StageOutcome("grayscale-wb", "done",
                                    digest={"skipped": True, "reason": "no grayscale patches"})
            transfer = self._transfer()
            cap = self._patch_max_cv() or transfer.max_cv
            # The slot abscissa = each patch's SIGNAL level (code/cap), so the grayscale bridge node-
            # aligns correction[i] onto the slot whose luminance we actually measured (see
            # build_grayscale_wb_set). The old uniform [i/(n-1)] index was the mis-mapping that made
            # every per-point correction land on the wrong slot → flat reads.
            points = [patch[0] / cap for patch in patches]
            payload = identity_payload(points)
            spec = self._spec()
            cfg = GrayTouchupConfig(white_xy=self._white_xy(), gamma=float(spec.gamma))

            # DLC-OWNED revert snapshot (fable Phase 7a, Design B): read the user's PRE-BEGIN
            # correctionGrayscale off the live state and persist it BEFORE we touch anything, so a
            # `revert` at the verify gate restores exactly that — independent of the C++ cancel path
            # (which is a no-op once commit has run) and durable across a DesktopLUT restart (the
            # snapshot lives in dlc_state.json, not DesktopLUT's in-memory GsLiveState). None ⇒ no
            # prior correction (revert clears to identity).
            # Captured ONCE per run: a stage re-run after DLC died mid-touch-up would read the
            # half-finished edit (the live session writes straight into correctionGrayscale; the
            # true pre-begin curve then lives only in DesktopLUT's in-memory GsLiveState) and
            # persist THAT as the "prior" a revert restores.
            if "grayscale_wb_prior_source" in self.calib:
                prior_snapshot = self.calib.get("grayscale_wb_prior")
                self.ctx.log("keeping this run's first pre-touch-up correctionGrayscale snapshot "
                             f"({self.calib.get('grayscale_wb_prior_source')}) — a re-run stage must not "
                             "re-capture a half-finished edit as the prior")
            else:
                self.calib["grayscale_wb_prior"] = prior_snapshot = self._snapshot_correction_grayscale()
            self._save()
            if prior_snapshot is None:
                # Honesty tell (fable Phase 9): a revert will clear the touch-up to identity
                # rather than restore a pre-existing correction — say so up front, and say WHY,
                # because "you had none" and "this build can't tell me" are different problems
                # (T3 made state.get expose the curve; older builds still don't).
                why = {
                    "none": "DesktopLUT reports an empty correction curve for this monitor/mode",
                    "unsupported": "this DesktopLUT build does not expose correction_grayscale in "
                                   "state.get — update DesktopLUT to make the revert restore your curve",
                    "unreadable": "DesktopLUT reported no MHC entry for this monitor/mode, or the pipe "
                                  "read failed",
                }.get(self.calib.get("grayscale_wb_prior_source") or "", "reason unknown")
                self.ctx.log(f"no prior correctionGrayscale captured over the pipe ({why}) — a revert "
                             "of this touch-up will clear to identity, not restore a prior correction")

            # Engage the live grayscale editor (the "Edit Points" path): this strips any prior
            # correction-grayscale from the active MHC permutation and shows a live, measurable
            # preview on top of the unchanged core (matrix + base grayscale + 3D LUT). Equivalent to
            # the operator opening the editor with the correction reset to identity before retuning.
            try:
                self.controller.grayscale_live_begin(self.monitor, self.mode)
            except Exception as exc:  # noqa: BLE001 - surface a clear, non-crashing abort
                return StageOutcome(
                    "grayscale-wb", "done",
                    digest={"message": f"could not engage the live Grayscale editor: "
                                       f"{type(exc).__name__}: {exc}",
                            "preview_unavailable": True, "measurement_compromised": True},
                    data={"payload": payload})

            has_3dlut = bool(self._active_runtime_cube())
            dip = self._dip()
            loop_cfg = self._with_preheat(self.loop_config or self._loop_config_for(dip))
            # Bright-point read averaging (2026-08-14 HDR run): high-luminance points on a
            # local-dimming panel oscillate read-to-read far beyond the DIP's luminance-σ
            # model (zone behaviour, not shot noise), and a single read per round had the
            # tuner chasing that noise for its whole round budget. Raise the per-round read
            # FLOOR to 3 on the bright portion of the ramp (gated by expected nits so the
            # slow dim reads stay single); the DIP still escalates above the floor.
            peak_nits = transfer.cv_to_nits(cap)
            bright_floor_nits = 0.25 * peak_nits
            if loop_cfg.neutral_floor_min_nits > 0:
                bright_floor_nits = min(loop_cfg.neutral_floor_min_nits, bright_floor_nits)
            loop_cfg = replace(
                loop_cfg,
                neutral_min_reads=max(loop_cfg.neutral_min_reads, 3),
                neutral_floor_min_nits=bright_floor_nits,
            )
            self.liveness.set_stall_after(self._liveness_threshold(dip))

            # Drift-ref self-perturbation guard (2026-08-14 HDR run): the drift/neutral
            # reference patch renders THROUGH the live editor table being tuned, so nudging
            # the point the reference sits on (the mid-ramp grey) moved the reference read
            # and tripped a false 'excursion' drift episode → measurement_compromised.
            # Present every reference-establishing/-comparing read through the IDENTITY
            # table — the state the warm reference was established in — then restore the
            # current live table. Edits therefore never masquerade as panel drift.
            ident = identity_payload(points)

            @contextmanager
            def reference_identity_guard():
                self.controller.grayscale_set_live(
                    self.monitor, self.mode, ident["point_count"], ident["points"],
                    ident["deviations"], luminance=ident["luminance"], rgb=ident["rgb"])
                try:
                    yield
                finally:
                    self.controller.grayscale_set_live(
                        self.monitor, self.mode, payload["point_count"], payload["points"],
                        payload["deviations"], luminance=payload["luminance"],
                        rgb=payload["rgb"])

            session = IncrementalMeasureSession(
                patches=patches,
                transfer=transfer,
                measure=self.measure,
                config=loop_cfg,
                ndjson_path=self.ctx.root / "measurements" / "grayscale_wb.ndjson",
                runlog=self.runlog,
                liveness=self.liveness,
                dip=dip,
                checkin_interval_s=self._checkin_interval_s,
                checkin_window=self._checkin_window,
                reference_guard=reference_identity_guard,
            )
            session_start = session.start()
            if session_start.get("panel_dark"):
                return StageOutcome(
                    "grayscale-wb", "done",
                    digest={"message": "panel appears dark/asleep during Grayscale touch-up warmup",
                            "measurement_compromised": True, **session_start},
                    data={"payload": payload})

            per_point: list[dict[str, Any]] = []
            before_errors: list[dict[str, Any]] = []
            after_errors: list[dict[str, Any]] = []
            all_updates: list[dict[str, Any]] = []
            unreachable_targets: list[dict[str, Any]] = []
            noise_floor_stops = 0
            regression_holds = 0
            any_capped = False
            any_large = False
            any_large_y = False
            any_unsettled = False

            # Outside-in alternating visit order (owner directive D4, 2026-08-14): 0, n-1,
            # 1, n-2, … keeps the running-average APL roughly flat (static band-stabilizer —
            # the old luminance-ascending sweep spent ~5 min in the dark cooling the panel
            # before the bright tail re-heated it) AND measures full drive second, so the
            # achievable ceiling below can bound every later bright point from round one.
            # The patches/points/payload lists stay ascending — only the visit order changes.
            achievable_ceiling_y: float | None = None
            for pos, idx in enumerate(outside_in_indices(len(patches))):
                patch = patches[idx]
                target_y = transfer.cv_to_nits(patch[0])
                at_full_drive = int(patch[0]) >= int(cap)
                point_log: dict[str, Any] = {
                    "index": idx,
                    "point": round(points[idx], 6),
                    "code": int(patch[0]),
                    "target_Y": round(target_y, 5),
                    "rounds": [],
                }
                # Achievable-ceiling bound (D4 extension of the top-point cap): the panel
                # cannot out-shine its own measured full-drive output at ANY lower drive, so
                # a bright point whose resolved target exceeds that ceiling is unreachable
                # for the same physics reason as the top point — bound it to the ceiling up
                # front instead of ramping the slider against it for the round budget.
                if (achievable_ceiling_y is not None and not at_full_drive
                        and achievable_ceiling_y + max(0.15, target_y * 0.01) < target_y):
                    info = {
                        "index": idx,
                        "code": int(patch[0]),
                        "requested_target_Y": round(target_y, 5),
                        "achievable_Y": round(achievable_ceiling_y, 5),
                        "shortfall_pct": round(100.0 * (1.0 - achievable_ceiling_y / target_y), 3),
                        "bounded_by_ceiling": True,
                    }
                    unreachable_targets.append(info)
                    point_log["unreachable_target"] = info
                    target_y = achievable_ceiling_y
                    self.runlog.anomaly(
                        "grayscale-wb", kind="unreachable_target", **info,
                        message=("grey point target exceeds the panel's measured full-drive "
                                 "ceiling — target bounded to the achievable ceiling; tuning "
                                 "chroma against the achievable target"))
                # F12 (2026-08-14 HW): a point must never END worse than it was FOUND. Capture
                # the pre-tune editor values so a regressed point can be restored (the tuner
                # traded chroma for ΔY against a thermally-shifted bright end and left an
                # already-good ramp worse, 1.39 → 2.42 avg).
                pre_lum = float(payload["luminance"][idx])
                pre_rgb = {ch: float(payload["rgb"][ch][idx]) for ch in ("r", "g", "b")}
                first_error: dict[str, Any] | None = None
                latest_error: dict[str, Any] | None = None
                prev_xyz: tuple[float, float, float] | None = None
                for rnd in range(1, max_rounds_per_point + 1):
                    try:
                        accepted = session.measure_index(idx)
                    except RuntimeError as exc:
                        point_log["rounds"].append({"round": rnd, "error": str(exc)})
                        any_unsettled = True
                        break
                    if not accepted.usable:
                        point_log["rounds"].append({"round": rnd, "unusable": True,
                                                    "note": accepted.note})
                        any_unsettled = True
                        break
                    y_tol = max(0.15, target_y * 0.01)
                    if rnd == 1 and at_full_drive:
                        # First-round full-drive measurement IS the panel's achievable
                        # ceiling — visited second under the outside-in order, so it
                        # anchors the bright-point target bounds above from round one.
                        achievable_ceiling_y = float(accepted.xyz[1])
                    if rnd == 1 and at_full_drive and accepted.xyz[1] + y_tol < target_y:
                        # Unreachable top-point target (2026-08-14 HDR run, the D2
                        # ungrounded-peak issue in a second flow): the resolved target asks
                        # for more light than the panel is delivering AT FULL DRIVE — a
                        # positive luminance correction cannot exceed 100% drive, so chasing
                        # it just ramps the slider to its cap against physics (the warm
                        # panel's sustained ceiling sits under the resolved cold ceiling).
                        # Hold luminance at what the panel actually achieves — first-round
                        # measured IS the achievable ceiling here — and keep tuning chroma
                        # against that achievable target instead of burning the round budget.
                        capped_y = float(accepted.xyz[1])
                        info = {
                            "index": idx,
                            "code": int(patch[0]),
                            "requested_target_Y": round(target_y, 5),
                            "achievable_Y": round(capped_y, 5),
                            "shortfall_pct": round(100.0 * (1.0 - capped_y / target_y), 3),
                        }
                        unreachable_targets.append(info)
                        point_log["unreachable_target"] = info
                        target_y = capped_y
                        y_tol = max(0.15, target_y * 0.01)
                        self.runlog.anomaly(
                            "grayscale-wb", kind="unreachable_target", **info,
                            message=("top grey point target exceeds the panel's achievable "
                                     "luminance at full drive — luminance correction held at "
                                     "measured; tuning chroma against the achievable target"))
                    gpatch = GrayTouchupPatch(level=points[idx], measured_xyz=tuple(accepted.xyz),
                                              target_y=target_y)
                    latest_error = point_error(gpatch, cfg)
                    if rnd == 1:
                        before_errors.append(latest_error)
                        first_error = latest_error
                    point_log["rounds"].append({"round": rnd, **latest_error})
                    if (latest_error["de2000"] <= target_de
                            and abs(latest_error["delta_Y"]) <= y_tol):
                        break
                    if rnd >= max_rounds_per_point:
                        any_unsettled = True
                        break
                    # Noise-floor stop (2026-08-14 HDR run): when the previous nudge moved
                    # the measurement by no more than this round's measured repeatability,
                    # further rounds are chasing read noise (bright local-dimming points
                    # oscillated ±dE at the zone level), not correcting — stop and record
                    # why instead of burning the round budget.
                    if prev_xyz is not None:
                        repeat_floor = accepted.se_de
                        if repeat_floor is None and dip is not None:
                            sigma = dip.expected_sigma_de(accepted.xyz[1])
                            if sigma:
                                repeat_floor = sigma / max(1, accepted.noise_reads) ** 0.5
                        if repeat_floor:
                            ref = white_xyz(max(target_y, accepted.xyz[1], prev_xyz[1], 1e-6),
                                            cfg.white_xy[0], cfg.white_xy[1])
                            round_delta = delta_e2000(xyz_to_lab(tuple(accepted.xyz), ref),
                                                      xyz_to_lab(prev_xyz, ref))
                            if round_delta <= 2.0 * repeat_floor:
                                point_log["noise_floor_stop"] = {
                                    "round": rnd,
                                    "round_delta_de": round(round_delta, 5),
                                    "repeatability_de": round(float(repeat_floor), 5),
                                }
                                noise_floor_stops += 1
                                break
                    prev_xyz = tuple(accepted.xyz)
                    payload, upd = update_point(payload, idx, gpatch, cfg)
                    point_log["rounds"][-1]["update"] = upd
                    all_updates.append(upd)
                    if upd.get("held_dark"):
                        # Below the dark floor: update_point holds the point (no correction is
                        # possible at this luminance), so re-measuring can't improve it — break
                        # instead of burning the whole round budget on an unchanged table.
                        break
                    # Live-set the editor table — the preview shader applies it next frame, so
                    # the very next read reflects this nudge. The DECOMPOSED sliders ride the
                    # wire (luminance = the editor's main slider, rgb = the balance strips)
                    # alongside the composed deviations (back-compat), so the editor shows the
                    # solver's split instead of common-mode R/G/B under a zero main slider.
                    self.controller.grayscale_set_live(
                        self.monitor, self.mode, payload["point_count"], payload["points"],
                        payload["deviations"], luminance=payload["luminance"],
                        rgb=payload["rgb"])
                    any_capped = any_capped or bool(upd.get("capped"))
                    any_large = any_large or bool(upd.get("large_correction"))
                    any_large_y = any_large_y or bool(upd.get("large_luminance_correction"))
                    if upd.get("capped"):
                        any_unsettled = True
                        break
                # F12 hold-on-regression: if the point ends measurably worse than its
                # round-1 state, restore the pre-tune editor values for this point and
                # live-set them — the restored state's error IS the round-1 measurement.
                # (Restoring is never harmful: it returns exactly what round 1 measured.)
                if (first_error is not None and latest_error is not None
                        and latest_error is not first_error
                        and latest_error["de2000"] > first_error["de2000"]):
                    lum = list(payload["luminance"])
                    rgb = {ch: list(payload["rgb"][ch]) for ch in ("r", "g", "b")}
                    lum[idx] = pre_lum
                    for ch in ("r", "g", "b"):
                        rgb[ch][idx] = pre_rgb[ch]
                    payload = compose_payload(payload["points"], lum, rgb)
                    self.controller.grayscale_set_live(
                        self.monitor, self.mode, payload["point_count"], payload["points"],
                        payload["deviations"], luminance=payload["luminance"],
                        rgb=payload["rgb"])
                    point_log["held_regression"] = {
                        "round1_de2000": round(first_error["de2000"], 5),
                        "final_de2000": round(latest_error["de2000"], 5),
                    }
                    regression_holds += 1
                    latest_error = first_error
                if latest_error is not None:
                    after_errors.append(latest_error)
                per_point.append(point_log)
                self._last_refine = {
                    "stage": "grayscale-wb",
                    # progress = visit position (outside-in), not the ascending slot index
                    "point": pos + 1,
                    "index": idx,
                    "points": len(patches),
                    "latest": latest_error,
                    "capped": any_capped,
                    "large_correction": any_large,
                }
                self._maybe_timed_checkin("grayscale-wb")

            session_digest = session.finish()

            # Ensure the final table is live in the preview even if every point was already within
            # target — it is baked into the ICM by the grayscale_commit at the end of this stage
            # (Design B), so measure:verify then scores the real result. Decomposed sliders ride
            # along so the committed editor state shows the luminance/balance split.
            self.controller.grayscale_set_live(
                self.monitor, self.mode, payload["point_count"], payload["points"],
                payload["deviations"], luminance=payload["luminance"], rgb=payload["rgb"])
            self.calib["grayscale_wb_touchup"] = payload
            self._state["grayscale_wb_touchup"] = payload
            self._save()

            max_abs_delta = max(
                [abs(v - 1.0) for col in payload["deviations"].values() for v in col] or [0.0])
            max_lum_delta = max([abs(v - 1.0) for v in payload["luminance"]] or [0.0])
            digest = {
                "point_count": payload["point_count"],
                "mode": self.mode,
                "stack": "mhc+3dlut" if has_3dlut else "mhc-only",
                "hdr_peak_code": (patches[-1][0] if self._spec().is_hdr else None),
                "target_de2000": target_de,
                "max_rounds_per_point": max_rounds_per_point,
                "before": summarize_errors(before_errors),
                "after": summarize_errors(after_errors),
                "max_abs_deviation": round(max_abs_delta, 6),
                "max_abs_luminance": round(max_lum_delta, 6),
                "large_correction": any_large,
                "large_luminance_correction": any_large_y,
                "capped": any_capped,
                "unsettled": any_unsettled,
                "unreachable_targets": unreachable_targets,
                "noise_floor_stops": noise_floor_stops,
                "regression_holds": regression_holds,
                "session": session_digest,
                "measurement_compromised": bool(session_digest.get("needs_adjudication")),
                "compromised": bool(any_capped or (has_3dlut and any_large_y)
                                    or session_digest.get("needs_adjudication")),
                "per_point": per_point,
                "updates": all_updates,
            }
            return StageOutcome("grayscale-wb", "done", digest=digest, data={"payload": payload})

        outcome = self._stage("grayscale-wb", run)
        if outcome.digest.get("measurement_compromised"):
            decision = self.adjudicate(AdjudicationRequest(
                key="grayscale-wb:measurement", seam=SEAM_MEASURE, stage="grayscale-wb",
                question=("the Grayscale touch-up measurement session had warmup/drift/preheat "
                          "evidence that may compromise the patch edits; accept the touch-up, "
                          "or abort and rerun after the panel settles?"),
                options=("accept", "abort"), recommendation="abort",
                digest=outcome.digest))
            if decision.choice == "abort":
                self._revert_inplace()
                raise CalibrationAborted(StageOutcome(
                    "grayscale-wb", "aborted",
                    digest={"message": "aborted on Grayscale touch-up measurement session",
                            "decision_note": decision.note, **outcome.digest}))
        if outcome.digest.get("large_correction") or outcome.digest.get("capped"):
            recommendation = "abort" if outcome.digest.get("capped") else "accept"
            decision = self.adjudicate(AdjudicationRequest(
                key="grayscale-wb:touchup-size", seam=SEAM_OPTIMIZE, stage="grayscale-wb",
                question=("the Grayscale correction is large enough to risk invalidating the "
                          "current constants/3D LUT; accept this touch-up, or abort and redo "
                          "the calibration constants instead?"),
                options=("accept", "abort"), recommendation=recommendation,
                digest=outcome.digest))
            if decision.choice == "abort":
                self._revert_inplace()
                raise CalibrationAborted(StageOutcome(
                    "grayscale-wb", "aborted",
                    digest={"message": "aborted on large Grayscale touch-up",
                            "decision_note": decision.note, **outcome.digest}))
        # Bake the touch-up into the ICM NOW (Design B, fable Phase 7a) — so measure:verify
        # measures the REAL baked result, not the live preview. The preview is only bit-identical
        # to the bake for the SDR realization-A path; HDR (and any SDR full-preview fallback)
        # previews light that provably differs from the bake, so verifying the preview would ship
        # an unverified deliverable. Committing here also makes the touch-up durable across a
        # DesktopLUT restart (it is in the ICM, not the in-memory preview). `revert` at the verify
        # gate does NOT depend on the C++ cancel-after-commit (a no-op): _revert_inplace re-applies
        # the DLC-owned pre-begin snapshot captured above. Skip when nothing was previewed.
        if not (outcome.digest.get("skipped") or outcome.digest.get("preview_unavailable")):
            # F13 (2026-08-14 HW): the bake outcome is MEMOISED in the run record. This block
            # sits outside the memoised stage, so it re-runs on every resume — and after the
            # verify-seam pause the process exits, the C++ live session is already committed
            # and closed, so a re-issued grayscale_commit truthfully reports no live session.
            # Without the record, the resume misread ALREADY-BAKED as bake-lost and the
            # only-option-abort seam forced abandoning a valid bake regardless of the verify
            # decision. A recorded successful bake short-circuits the re-check; bake-lost only
            # escalates when the record AND the C++ agree no bake happened.
            if (self.calib.get("grayscale_wb_baked") or {}).get("baked"):
                self.ctx.log("Grayscale touch-up already baked this run (memoised) — not re-committing")
            else:
                baked = self.controller.grayscale_commit(self.monitor, self.mode)
                # The C++ returns baked:false if the live session was lost (e.g. DesktopLUT
                # restarted mid-run) — surface it as a compromised seam rather than logging a
                # bake that did not happen. A dict without an explicit baked:false is treated as
                # success (older builds may omit the key). (gs-wb adversarial finding: the flag
                # was previously unread.)
                if isinstance(baked, dict) and baked.get("baked") is False:
                    self.runlog.anomaly(
                        "grayscale-wb", bake_lost=True,
                        message="grayscale_commit reported no live session to bake (DesktopLUT "
                                "restarted mid-run?) — the touch-up was NOT applied")
                    self._abort_if(self.adjudicate(AdjudicationRequest(
                        key="grayscale-wb:bake-lost", seam=SEAM_MEASURE, stage="grayscale-wb",
                        question=("the Grayscale touch-up could not be baked — DesktopLUT reported no "
                                  "live edit session (it may have restarted mid-run). Re-run the "
                                  "touch-up after restarting it, or abort?"),
                        options=("abort",), recommendation="abort",
                        digest={**outcome.digest, "bake_lost": True, "compromised": True})),
                        stage="grayscale-wb", message="grayscale touch-up bake lost (no live session)")
                else:
                    self.calib["grayscale_wb_baked"] = {
                        "baked": True,
                        "response": baked if isinstance(baked, dict) else None,
                    }
                    self._save()
                    self.ctx.log("baked the Grayscale touch-up into the ICM")
        return outcome

    def _snapshot_correction_grayscale(self) -> Optional[dict[str, Any]]:
        """The live correctionGrayscale for this monitor/mode from ``state.get`` (the user's
        current correction), or ``None`` — the DLC-owned revert snapshot for the grayscale
        touch-up (Design B). Best-effort: a down pipe just yields None (the touch-up won't
        proceed far without the pipe anyway).

        Keeps exactly the wire block ``{enabled, point_count, points, deviations}`` — the
        decomposition DesktopLUT stores, handed back verbatim by the revert. Records WHY it is
        None in ``calib['grayscale_wb_prior_source']`` (fable Phase 9 T3), because the reasons need
        different words to the user:

        * ``prior`` — the user's curve (identity included) was captured; a revert restores it.
        * ``none`` — the field is there but its points are empty.
        * ``unsupported`` — the mhc entry carries no ``correction_grayscale`` at all: a DesktopLUT
          predating the field. A revert can only clear to identity.
        * ``unreadable`` — no mhc entry for this pair, or the pipe failed.
        """
        try:
            state = self.controller.state()
            key = f"{self.monitor}:{self.mode}"
            entry = (state.get("mhc") or {}).get(key)
            if not isinstance(entry, dict):
                self.calib["grayscale_wb_prior_source"] = "unreadable"
                return None
            cg = entry.get("correction_grayscale")
            if not isinstance(cg, dict):
                self.calib["grayscale_wb_prior_source"] = "unsupported"
                return None
            if not cg.get("points"):
                self.calib["grayscale_wb_prior_source"] = "none"
                return None
            self.calib["grayscale_wb_prior_source"] = "prior"
            devs = cg.get("deviations") or {}
            # The run switched the grayscale LAYER off before this snapshot (hardware-readiness),
            # so the live bit reads False for a user whose curve was ON; the user's own on/off is
            # the viewing-layer capture taken before anything was switched.
            enabled = cg.get("enabled")
            vl = self.calib.get("viewing_layers")
            if (isinstance(vl, dict) and vl.get("captured") and isinstance(vl.get("before"), dict)
                    and "grayscale" in vl["before"] and vl.get("monitor") in (None, self.monitor)
                    and str(vl.get("mode") or self.mode) == str(self.mode)):
                enabled = bool(vl["before"]["grayscale"])
            return {
                "enabled": enabled,
                "point_count": int(cg.get("point_count") or len(cg["points"])),
                "points": [float(p) for p in cg["points"]],
                "deviations": {ch: [float(v) for v in (devs.get(ch) or [])] for ch in ("r", "g", "b")},
            }
        except Exception:  # noqa: BLE001 - advisory snapshot; revert falls back to clearing
            self.calib["grayscale_wb_prior_source"] = "unreadable"
            return None

    def _restore_correction_grayscale(self) -> bool:
        """Re-apply the DLC-owned pre-begin correctionGrayscale snapshot and regenerate the ICM
        (Design B revert). Restores the user's prior correction if one was captured, else clears
        to identity — either way it does NOT rely on the C++ cancel-after-commit no-op. Returns
        whether the restore call chain succeeded.

        The prior block goes back VERBATIM (``set_correction_grayscale_raw``): it is already in
        DesktopLUT's stored decomposition, so the signal-domain bridge would bend a curve that
        carries a luminance (main-slider) component. ``ApplyGrayscalePayload`` then forces the
        curve ENABLED, so the prior on/off state is put back through ``layers.set`` — a curve the
        user had switched off stays off."""
        prior = self.calib.get("grayscale_wb_prior")
        try:
            if prior and prior.get("points"):
                self.controller.set_correction_grayscale_raw(self.monitor, self.mode, prior)
                if prior.get("enabled") is not None:
                    self.controller.set_layers(self.monitor, self.mode, grayscale=bool(prior["enabled"]))
            else:
                n = 32
                grid = [j / (n - 1) for j in range(n)]
                self.controller.set_correction_grayscale(
                    self.monitor, self.mode, n, grid,
                    {ch: [1.0] * n for ch in ("r", "g", "b")}, gamma=float(self._spec().gamma))
            self.controller.apply_mhc(self.monitor, self.mode)
            return True
        except Exception as exc:  # noqa: BLE001 - fall through to the manual-backup guidance
            self.ctx.log(f"grayscale touch-up revert failed ({type(exc).__name__}: {exc}); "
                         "see settings backup")
            return False

    def _active_runtime_cube(self) -> Optional[str]:
        try:
            state = self.controller.state()
            key = f"{self.monitor}:{self.mode}"
            return (((state.get("runtime") or {}).get(key) or {}).get("cube_path") or None)
        except Exception:  # noqa: BLE001 - advisory, never block a touch-up
            return None

    def _probe_path_cube(self) -> Optional[str]:
        """The runtime cube the MHC-only reads (the post-MHC set + the cube build's probes) are taken
        through: NONE — except a ``3dlut-only`` run that started with a cube installed (its in-place
        baseline), which keeps that cube live for both. That is the flow's existing semantics (it never
        enters calibration mode, its post-MHC scores are the apply gate's 'revert' evidence, and nothing
        restores a cleared cube on ``--abort``); changing it is the owner's call, so it is surfaced as
        evidence (:meth:`_ensure_probe_path`), not changed here."""
        baseline = self.calib.get("inplace_baseline") or {}
        if self.calib.get("flow") == "3dlut-only" and baseline.get("captured") and baseline.get("cube_path"):
            return str(baseline["cube_path"])
        return None

    # Backoff before the unverified seam: the calibration pipe is single-instance, so one state.get can
    # collide with another client (an operator's orientation `state()` from a second shell).
    _PROBE_PATH_STATE_RETRY_S: tuple[float, ...] = (0.25, 0.75)

    def _ensure_probe_path(self, stage: str, *, reads: str) -> dict[str, Any]:
        """Make this monitor+mode's runtime 3D-LUT slot provably hold :meth:`_probe_path_cube` before
        the MHC-only reads: EMPTY — except 3dlut-only's own installed cube.

        The post-MHC set and the build probes present DRIVEN codes, and the build's exact-code probe
        reuse (``OptimizeConfig.probe_code_levels``) answers a probe from the post-MHC read of the same
        code — both must read through ONE path. A different cube sits there when an earlier attempt of
        this run installed its build and the stage was re-run (BenQ 2026-09-27: attempt A installed its
        cube, B/C re-ran the build and probed through it) or an adaptive-planning change re-measures
        post-MHC after a build. Mechanics: read the slot, clear it (or re-install the 3dlut-only
        baseline), read it back; a failed call or a slot still wrong afterwards is a :class:`StageError`.
        A correction is an ANOMALY (anything this run measured under the stale cube is suspect), kept in
        ``calib['probe_path_corrections']`` so it outlives a seam pause (``earlier_corrections``).

        A state that stays unreadable after a short retry cannot prove the path either way — a
        judgment, so a seam (``<stage>:probe-path-unverified``: continue unverified / abort); resuming
        once the pipe is back simply re-reads it. 3dlut-only with the user's cube in the path is an
        anomaly once per boundary: the build REPLACES the cube its reads went through, so it is
        modelled on MHC + that cube — evidence for the LLM."""
        import time

        key = f"{self.monitor}:{self.mode}"
        want = self._probe_path_cube()
        wanted = want or "empty"

        def read_slot() -> tuple[bool, Optional[str], Optional[str]]:
            error = None
            for delay in (0.0, *self._PROBE_PATH_STATE_RETRY_S):
                if delay:
                    time.sleep(delay)
                try:
                    state = self.controller.state() or {}
                except Exception as exc:  # noqa: BLE001 - retried, then judged
                    error = f"{type(exc).__name__}: {exc}"
                    continue
                cube = ((state.get("runtime") or {}).get(key) or {}).get("cube_path")
                return True, (str(cube) if cube else None), None
            return False, None, error

        earlier = [c for c in self.calib.get("probe_path_corrections") or [] if c.get("stage") == stage]
        block: dict[str, Any] = {"verified": True, "path_cube": want, "corrected": None}
        if earlier:
            block["earlier_corrections"] = earlier
        if stage != "measure:post-mhc":
            block["post_mhc_probe_path"] = (
                (self.calib["stages"].get("measure:post-mhc") or {}).get("digest") or {}).get("probe_path")
        ok, found, error = read_slot()
        if not ok:
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key=f"{stage}:probe-path-unverified", seam=SEAM_PIPE, stage=stage,
                question=(f"cannot read DesktopLUT state ({error}) to prove {key}'s runtime 3D LUT is {wanted} "
                          f"before the {reads} — a cube left there by an earlier attempt of this run would sit "
                          "in their path. 'continue' takes them unverified (the build then re-reads every probe: "
                          "no reuse of post-MHC reads); 'abort' stops the run. Resuming after the pipe is back "
                          "re-reads the slot, and no decision is needed then."),
                options=("continue", "abort"), recommendation="continue",
                digest={"error": error, "reads": reads, "path_cube": want,
                        "earlier_corrections": earlier})),
                stage=stage, message=f"aborted: {key}'s runtime 3D LUT could not be verified before the {reads}")
            block.update(verified=False, error=error)
            self.runlog.anomaly(stage, kind="probe_path_unverified", error=error, path_cube=want,
                                message=f"the {reads} proceed with {key}'s runtime 3D LUT UNVERIFIED ({error})")
            return block
        if found != want:
            verb = "runtime.clear_3dlut" if want is None else "runtime.set_3dlut (the 3dlut-only baseline)"
            try:
                if want is None:
                    self.controller.clear_3dlut(self.monitor, self.mode)
                else:
                    self.controller.set_3dlut(self.monitor, self.mode, want)
            except Exception as exc:  # noqa: BLE001 - refused with the cause
                raise StageError(
                    stage, f"{key}'s runtime 3D LUT is {found or 'empty'}, not {wanted}, and {verb} failed "
                    f"({type(exc).__name__}: {exc}) — refusing the {reads}", stale_cube=found) from exc
            ok, left, error = read_slot()
            if not ok or left != want:
                raise StageError(
                    stage, f"{verb} did not leave {key}'s runtime 3D LUT {wanted} "
                    f"(it is {'unreadable: ' + str(error) if not ok else left or 'empty'}) — refusing the {reads}",
                    stale_cube=found, installed=left, error=error)
            self._hook_routing_evidence_after_install(stage, action="clear" if want is None else "install")
            fix = {"stage": stage, "from": found, "to": want}
            block["corrected"] = fix
            self.calib.setdefault("probe_path_corrections", []).append(dict(fix))
            self._save()
            tail = ""
            if stage != "measure:post-mhc":
                tail = (" (the post-MHC set was verified on this path when measured)"
                        if (block["post_mhc_probe_path"] or {}).get("verified") else
                        " (the post-MHC set is not verified on this path — judge whether it was measured "
                        "through the stale cube; probe reuse is off for this build)")
            self.runlog.anomaly(
                stage, kind="stale_runtime_cube", cube_path=found, **block,
                message=(f"{key}'s runtime 3D LUT was {found or 'empty'} before the {reads}, not {wanted} (an "
                         "earlier attempt's build left by a stage re-run?); corrected so they read the path the "
                         f"post-MHC set is measured on. Anything this run measured under it is suspect{tail}"))
        flagged = self.calib.setdefault("inplace_cube_in_probe_path", [])
        if want is not None and stage not in flagged:
            flagged.append(stage)
            self._save()
            self.runlog.anomaly(
                stage, kind="inplace_cube_in_probe_path", cube_path=want,
                message=(f"3dlut-only takes the {reads} through the 3D LUT already installed on {key} ({want}): "
                         "the post-MHC set and the probes agree, but the new build REPLACES that cube, so it is "
                         "modelled on MHC + the installed cube — unless that cube is near-identity the applied "
                         "result is off by its effect (clear it before a 3dlut-only run, or run the full flow)"))
        return block

    @staticmethod
    def _probe_seeding_proven(probe_path: Mapping[str, Any]) -> bool:
        """May the build's probe reuse answer probes from post-MHC reads? Only when both sides are on
        ONE path: the build's own check verified; a post-MHC record, if any, verified; and after ANY
        correction for this build (this invocation or one before a seam pause) the post-MHC set proven
        on the corrected path. A legacy post-MHC set (no record) with no correction keeps seeding."""
        post = probe_path.get("post_mhc_probe_path")
        corrected = probe_path.get("corrected") or probe_path.get("earlier_corrections")
        if not probe_path.get("verified"):
            return False
        if post is not None and not post.get("verified"):
            return False
        return not corrected or bool((post or {}).get("verified"))

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
        # `== OptimizeConfig.max_correction_cap` compares to the frozen-dataclass class default
        # (0.25) ⇒ "the caller left the cap at the default". `!= "HDR"` (not `== "SDR"`) routes any
        # non-HDR mode to the SDR ceiling; normalize_mode guarantees mode ∈ {SDR, HDR}.
        if self.mode != "HDR" and cfg.max_correction_cap == OptimizeConfig.max_correction_cap:
            return replace(cfg, max_correction_cap=SDR_CORRECTION_CAP)
        # Top hold (owner policy 2026-09-23): the calibrated top IS the HDR patch cap (every post-MHC
        # patch is bounded by it), so pin it explicitly rather than trusting the data's max channel.
        if cfg.top_hold and cfg.top_hold_signal is None and self.target_name is not None:
            # (No resolved target yet ⇒ leave it None: optimize_cube then derives the top from the
            # measured stimuli, which the HDR patch cap bounds anyway.)
            cap_cv = self._patch_max_cv()
            max_cv = self._transfer().max_cv
            if cap_cv and max_cv and cap_cv < max_cv:
                return replace(cfg, top_hold_signal=cap_cv / max_cv)
        return cfg

    def _cube_oog_solve(self, cfg: OptimizeConfig, target, signals, measured
                        ) -> tuple[OptimizeConfig, Optional[dict[str, Any]]]:
        """The run's out-of-gamut node solve for the 3D-LUT build (``OptimizeConfig.oog_solve``), memoised in the
        run record like the OOG mapping so a resume builds the way the run started. The projection solve rests
        on a premise — the monitor decodes Rec.2020 colorimetrically inside its native gamut — which is checked
        on THIS run's post-MHC reads (:func:`dlc.engine.cube_quality.premise_check`) before it is used; a failed
        or undecidable check is a judgment for the LLM (seam), never silently accepted or silently downgraded."""
        memo = self.calib.get("oog_solve")
        if memo in ("direct", "projection") and memo != cfg.oog_solve:
            cfg = replace(cfg, oog_solve=memo)
        self.calib["oog_solve"] = cfg.oog_solve
        reach = self._reachable_primaries()
        if cfg.oog_solve != "projection" or reach is None:
            return cfg, None
        from .engine.cube_quality import premise_check
        cap = float(self._hdr_target().peak_nits) if self.mode == "HDR" else float(target.peak_nits)
        premise = premise_check(signals, measured, target, reach, self._white_xy(), cap)
        if premise.get("passed") is True:
            return cfg, premise
        decision = self._abort_if(self.adjudicate(AdjudicationRequest(
            key="build-install-3dlut:oog-premise", seam=SEAM_OPTIMIZE, stage="build-install-3dlut",
            question=(
                "the out-of-gamut projection solve assumes the monitor decodes Rec.2020 colorimetrically "
                "inside its native gamut, but this run's post-MHC reads do not confirm it "
                + (f"({premise['colorimetric_closer']} of {premise['n']} saturated in-gamut reads closer to "
                   f"the colorimetric model, p = {premise['p_value']:.2g})"
                   if premise.get("passed") is False else f"({premise.get('reason')})")
                + " — build with projection anyway, fall back to the direct solve, or abort?"),
            options=("projection", "direct", "abort"), recommendation="direct",
            digest={"premise": premise})),
            stage="build-install-3dlut", message="aborted at the out-of-gamut solve premise seam")
        if decision.choice == "direct":
            cfg = replace(cfg, oog_solve="direct")
            self.calib["oog_solve"] = "direct"
        return cfg, premise

    # -- level edge (design D4): the luminance-dependent confirmed gamut edge ------------------------------
    def _level_edge_switch(self) -> str:
        """The profile's ``level_edge`` switch for the resolved target ("off" default | "auto")."""
        if self.target_name is None:
            return "off"
        return str(getattr(self.profile.target(self.target_name), "level_edge", "off") or "off")

    def _reachable_gamut(self, *, stage: str = "verify") -> Any:
        """The reachable target the 3D-LUT build's optimizer, the verify score and the stage CLIs clamp against: the
        run's level-edge gamut when its memo (``calib["oog_level_edge"]``, pinned at the first 3D-LUT build) enables
        it, else :meth:`_reachable_primaries` (the full-drive triangle). EVERYTHING ELSE — patch sets, saturation /
        verify-ramp caps, intermediate stage scores — keeps ``_reachable_primaries`` (sampling never changes).

        A pinned edge whose key no longer matches ``mhc_params.level_edge`` (re-fitted after the cube was built)
        cannot reproduce the target the cube was built for: the ``level_edge_key_mismatch`` seam decides."""
        prim = self._reachable_primaries()
        memo = self.calib.get("oog_level_edge")
        if prim is None or self.mode != "HDR" or not (isinstance(memo, dict) and memo.get("enabled")):
            return prim
        params = self._state.get("mhc_params") or {}
        white = self._white_xy()
        gamut, rec = metrics_mod.run_level_edge(self.calib, params, oog_mapping=self._oog_mapping(), white_xy=white)
        if gamut is not None:
            return gamut
        block = metrics_mod.level_edge_block(params)
        current_ok = bool(block and block.get("status") == "ok" and not rec.get("white_mismatch"))
        options = ("use_current", "disable", "abort") if current_ok else ("disable", "abort")
        if current_ok:
            why = "re-fitted"
        elif rec.get("white_mismatch"):
            why = f"anchored on white {(block or {}).get('white_xy')}, the target now uses {list(white)}"
        else:
            why = f"not usable: {(block or {}).get('reason')}"
        decision = self._abort_if(self.adjudicate(AdjudicationRequest(
            key=f"{stage}:level_edge_key_mismatch", seam=(SEAM_VERIFY if stage == "verify" else SEAM_OPTIMIZE),
            stage=stage,
            question=(
                f"the 3D LUT was built for the level edge {memo.get('key')}, but the run record's level edge is now "
                f"{rec.get('current_key')} ({why}"
                ") — 'disable' scores against the full-drive native triangle (the pre-level-edge target; the cube's "
                "low-luminance out-of-gamut targets will read as misses there)"
                + (", 'use_current' scores against the current edge (close to, but not exactly, the one the cube "
                   "aimed at)" if current_ok else "")
                + ", 'abort' stops the run."),
            options=options, recommendation="disable",
            digest={"pinned_key": memo.get("key"), "current_key": rec.get("current_key"),
                    "white_mismatch": bool(rec.get("white_mismatch")),
                    "current_status": (block or {}).get("status"), "current_reason": (block or {}).get("reason"),
                    "memo": memo})),
            stage=stage, message="aborted at the level-edge key-mismatch seam")
        if decision.choice == "use_current" and current_ok:
            self.calib["oog_level_edge"] = {**memo, "key": block.get("key"),
                                            "reason": "re-pinned to the current edge at the key-mismatch seam"}
            self._save()
            return metrics_mod.stage_level_gamut(self.calib, params, white_xy=white)[0] or prim
        self.calib["oog_level_edge"] = {**memo, "enabled": False,
                                        "reason": "disabled at the level_edge_key_mismatch seam"}
        self._save()
        return prim

    def _cube_level_edge(self, cfg: OptimizeConfig, target, signals, measured
                         ) -> tuple[Any, Optional[dict[str, Any]]]:
        """Decide + pin (``calib["oog_level_edge"]``) whether THIS 3D-LUT build maps out-of-gamut targets onto the
        level edge (``metrics.run_level_edge`` — HDR ∧ vertex ∧ projection ∧ profile ``level_edge: auto`` ∧ the MHC
        build's gates passed ∧ this run's post-MHC reads do not falsify it). A falsified (or undecidable) edge is a
        judgment for the LLM (``level_edge_falsified`` seam: disable / enable anyway / abort), never silently applied
        or silently dropped. Returns ``(reachable for optimize_cube, digest block | None)`` — the block only when
        the profile asked for the edge (a run with the switch off keeps its build digest unchanged)."""
        reach = self._reachable_primaries()
        if reach is None or self.mode != "HDR":
            return reach, None
        params = self._state.get("mhc_params") or {}
        switch = self._level_edge_switch()
        # A memo from an earlier attempt at THIS build (interrupted, or its inputs invalidated) is re-decided on
        # this build's data — deterministic, and a recorded seam decision replays by key. (With a memo the record
        # is not legacy, so the build/verify stage entries must not trip the legacy rule either.)
        view = self.calib
        if isinstance(self.calib.get("oog_level_edge"), dict):
            view = {k: v for k, v in self.calib.items() if k not in ("oog_level_edge", "stages")}
        from .engine.cube_quality import level_edge_falsification

        def falsify(gamut):
            return level_edge_falsification(gamut, signals, measured, floor_nits=gamut.floor_nits)
        gamut, rec = metrics_mod.run_level_edge(view, params, switch=switch, oog_mapping=self._oog_mapping(),
                                                oog_solve=cfg.oog_solve, is_hdr=True, falsify=falsify,
                                                white_xy=self._white_xy())
        if rec.get("falsified"):
            fals = rec.get("falsification") or {}
            block = metrics_mod.level_edge_block(params) or {}
            undecided = fals.get("passed") is None
            worst = fals.get("worst") or {}
            decision = self._abort_if(self.adjudicate(AdjudicationRequest(
                key="build-install-3dlut:level_edge_falsified", seam=SEAM_OPTIMIZE, stage="build-install-3dlut",
                question=(
                    "the luminance-dependent gamut edge (level edge) fitted from this run's raw ramps "
                    + ("could not be checked against its post-MHC reads (none at or above the "
                       f"{fals.get('floor_nits')}-nit floor)" if undecided else
                       f"is contradicted by its post-MHC reads: {fals.get('over_jnd')} of {fals.get('n')} reads lie "
                       f"more than 1 JND outside it at their own luminance (worst {fals.get('max_de')} dE_ITP at "
                       f"{worst.get('Y')} nits, signal {worst.get('signal')})")
                    + " — 'disable' builds and verifies against the full-drive native triangle (the pre-level-edge "
                    "target), 'enable' maps out-of-gamut targets onto the level edge anyway (the dim ones aim at the "
                    "fitted edge, which the panel may exceed there), 'abort' stops the run."),
                options=("disable", "enable", "abort"), recommendation="disable",
                digest={"falsification": fals, "key": rec.get("key"),
                        "pedestal": {k: (block.get("pedestal") or {}).get(k)
                                     for k in ("K_xy", "c", "gamma", "fit_rms_duv", "identifiable")},
                        "floor_nits": block.get("floor_nits")})),
                stage="build-install-3dlut", message="aborted at the level-edge falsification seam")
            if decision.choice == "enable":
                rec.update(enabled=True, reason="enabled at the level_edge_falsified seam (judgment)")
            else:
                rec.update(enabled=False, reason="disabled at the level_edge_falsified seam")
                gamut = None
        self.calib["oog_level_edge"] = {k: rec.get(k) for k in ("enabled", "key", "reason", "falsification")}
        use = gamut if (rec.get("enabled") and gamut is not None) else reach
        if switch != "auto":
            return use, None
        out: dict[str, Any] = {"enabled": bool(rec.get("enabled")), "key": rec.get("key"),
                               "reason": rec.get("reason"), "falsification": rec.get("falsification")}
        if rec.get("enabled") and gamut is not None:
            out.update(self._level_edge_build_evidence(gamut, target, cfg.grid_size))
        return use, out

    def _level_edge_build_evidence(self, gamut, target, grid_size: int) -> dict[str, Any]:
        """Build-digest evidence for an enabled level edge: how the lattice's clamp set changes vs the full-drive
        triangle (newly clamped / un-clamped nodes) and how far the planned verify patches' targets move."""
        try:
            from .engine.model import TargetSpace, de_itp
            raw = TargetSpace(target)
            full = TargetSpace(target, reachable_primaries=gamut.full_primaries)
            level = TargetSpace(target, reachable_primaries=gamut)
            axis = np.linspace(0.0, 1.0, grid_size)
            B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
            grid = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)
            raw_xyz = raw.ideal_xyz(grid)
            cl_level = level.level_clamped(raw_xyz)
            tol = 1e-9 + 1e-6 * np.sum(np.abs(raw_xyz), axis=1)
            cl_full = np.any(np.abs(full.ideal_xyz(grid) - raw_xyz) > tol[:, None], axis=1)
            max_cv = self._transfer().max_cv
            sig = np.asarray(self._verify_patches(), dtype=float) / float(max_cv)
            d = de_itp(raw.xyz_to_ictcp(level.ideal_xyz(sig)) - raw.xyz_to_ictcp(full.ideal_xyz(sig)))
            moved = d > 1e-6
            return {"lattice": {"grid_size": int(grid_size), "clamped_level_edge": int(cl_level.sum()),
                                "clamped_full_drive": int(cl_full.sum()),
                                "newly_clamped": int(np.sum(cl_level & ~cl_full)),
                                "un_clamped": int(np.sum(~cl_level & cl_full))},
                    "verify_plan": {"patches": int(len(sig)), "targets_moved": int(moved.sum()),
                                    "mean_move_de_itp": round(float(d[moved].mean()), 3) if moved.any() else 0.0,
                                    "max_move_de_itp": round(float(d.max()), 3) if d.size else 0.0}}
        except Exception as exc:  # noqa: BLE001 - evidence must never break the build
            return {"evidence_error": f"{type(exc).__name__}: {exc}"}

    def _level_edge_verify_evidence(self, samples, metrics, q, within, *, white_xy, peak_nits,
                                    gamut) -> dict[str, Any]:
        """Verify-digest evidence when the level edge is the primary target (never a new pause): the same reads
        scored against the full-drive target, the patches whose practical bucket differs between the two
        definitions, the edge's falsification on the verify reads, and whether the gate verdict depends on it."""
        from .engine.cube_quality import level_edge_falsification
        full_metrics, _ = score_samples_hdr(samples, white_xy=white_xy, peak_nits=peak_nits,
                                            oog_mapping=self._oog_mapping(),
                                            reachable_primaries=gamut.full_primaries)
        full_practical = practical_summary(full_metrics, is_hdr=True, gamut_aware=True)
        full_summary = summarize_metrics(phase="verification", iteration=0, source=Path("."),
                                         patch_metrics=full_metrics, target_luminance=peak_nits, metric="dE_ITP")
        within_full, _basis = self._quality_gate(full_summary, full_practical, q)

        def bucket(m) -> str:
            x, y, z = m.target_xyz
            tot = x + y + z
            if m.gamut_clamped:
                return "clamped"
            return "core" if metrics_mod.is_core_target((x / tot, y / tot) if tot > 1e-9 else None, y) else "limits"
        moved = [{"rgb": [round(c, 4) for c in a.rgb], "level_edge": bucket(a), "full_drive": bucket(b),
                  "de_level_edge": round(a.de2000, 3), "de_full_drive": round(b.de2000, 3)}
                 for a, b in zip(metrics, full_metrics) if bucket(a) != bucket(b)]
        allv = [m.de2000 for m in full_metrics]
        full_view = {k: full_practical.get(k) for k in ("core", "limits", "clamped", "tube")}
        full_view["all"] = {"avg": round(sum(allv) / len(allv), 3), "p95": round(percentile(allv, 95), 3),
                            "max": round(max(allv), 3), "n": len(allv)}
        fals = level_edge_falsification(gamut, np.array([s.rgb for s in samples]),
                                        np.array([s.xyz for s in samples]), floor_nits=gamut.floor_nits)
        return {"level_edge": {"key": gamut.key(), "memo": self.calib.get("oog_level_edge")},
                "full_drive_target": full_view,
                "reclassified": moved[:50], "reclassified_count": len(moved),
                "level_edge_falsification": fals,
                "verdict_depends_on_level_edge": bool(within_full) != bool(within),
                "within_quality_full_drive": bool(within_full)}

    def stage_build_install_3dlut(self, post_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            # The probes read DRIVEN codes through the post-MHC set's path: an earlier attempt's installed
            # cube (a re-run of this stage) must not sit in it — corrected, provably, before the first probe.
            probe_path = self._ensure_probe_path("build-install-3dlut", reads="build probes")
            self._oog_mapping()       # the cube, verify and the stage CLIs share one OOG policy
            target = self._engine_target()
            report_scorer, report_metric = self._optimizer_report_scorer()
            samples = parse_ti3(Path(post_ti3))
            signals = np.array([s.rgb for s in samples], dtype=float)
            measured = np.array([s.xyz for s in samples], dtype=float)
            cube_path = str(self.ctx.root / "generated" / f"final_{self.mode.lower()}.cube")
            cfg, premise = self._cube_oog_solve(self._cube_optimize_config(), target, signals, measured)
            if self._probe is None and cfg.probe_code_levels is None:
                # The live probe drives integer codes of this transfer (``_probe_fn``) through the same
                # MHC-only path the post-MHC set was measured on: an identical code triple is an identical
                # stimulus, answered from the earlier read (OptimizeConfig.probe_code_levels) — while the
                # per-pass sentinels prove the display is still in that state. A post-MHC set thermally
                # re-aligned to its start/middle no longer holds reads of the CURRENT state: no seeding. Nor
                # when the two reads are not proven on one path (_probe_seeding_proven).
                align = ((self.calib.get("thermal_align") or {}).get("measure:post-mhc") or {}).get("choice")
                cfg = replace(cfg, probe_code_levels=int(self._transfer().max_cv),
                              probe_reuse_seed=(align not in ("start", "mid")
                                                and self._probe_seeding_proven(probe_path)))
            # The level edge (D4) is decided + pinned here, after the solve mode (it needs the projection solve).
            reachable, level_edge = self._cube_level_edge(cfg, target, signals, measured)
            # A fresh build attempt (a re-run / resumed build starts its training afresh from the
            # post-MHC set): its probe-ledger rows carry this id, so the held-out classification
            # reads only the live attempt's drives (V1).
            probe_attempt = int(self.calib.get("build_probe_attempts") or 0) + 1
            self.calib["build_probe_attempts"] = probe_attempt
            self._save()
            try:
                result = optimize_cube(target=target, probe=self._probe_fn(attempt=probe_attempt),
                                       signals=signals,
                                       measured_xyz=measured, config=cfg,
                                       on_iteration=self._on_optimize_iteration,
                                       reachable_primaries=reachable,
                                       report_scorer=report_scorer, report_metric=report_metric)
            except DegenerateMeasurements as exc:
                # The RBF model can't be built from this patch set (degenerate/collinear) —
                # surface a clear, actionable abort instead of crashing with a numpy traceback.
                raise CalibrationAborted(StageOutcome(
                    "build-install-3dlut", "aborted",
                    digest={"message": f"3D-LUT correction could not be built: {exc.detail}",
                            "degenerate": True, "measurement_count": int(len(signals))}))
            result.write(cube_path, title=f"DLC {self.mode} 3D LUT")
            self.controller.set_3dlut(self.monitor, self.mode, cube_path)
            self._hook_routing_evidence_after_install("build-install-3dlut")
            digest = {**result.digest, "cube_path": cube_path, "probe_path": probe_path,
                      "probe_attempt": probe_attempt}
            if getattr(target, "transfer", None) != "pq":
                # The white this cube's tone curve was built for — the evidence a later flow that
                # KEEPS this cube (refine-mhc) compares its own refined white against.
                white_nits, white_source = self._sdr_calibrated_white()
                digest["target_white_nits"] = round(float(target.peak_nits), 4)
                digest["target_white_source"] = white_source if white_nits is not None else "nominal"
                digest["nominal_white_nits"] = self._spec().luminance_nits
                # V3: the build's surfaced CIEDE2000 (*_report) is scored against this ABSOLUTE target
                # white; the verify scores relative to its MEASURED white (sdr_white.scored_white_nits).
                digest["report_scored_white"] = ("the cube's target white (target_white_nits, absolute) — "
                                                 "the verify scores relative to its measured white")
            if premise is not None:
                digest["oog_premise"] = premise
            if level_edge is not None:
                digest["level_edge"] = level_edge
            return StageOutcome("build-install-3dlut", "done", digest=digest,
                                data={"cube_path": cube_path, "probe_attempt": probe_attempt,
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
                # Options are accept/abort only (fable Phase 8): the seam previously offered
                # "loosen_target" but NO code path honoured it — the string fell through every
                # comparison and silently behaved as accept (a phantom option is worse than a
                # missing one at a judgment surface). Quality targets are advisory and the
                # verify seam is where acceptance is negotiated; an abort here is the lever
                # for re-running with a raised cap / different target.
                question=outcome.data.get("question") or "the correction machine hit a floor — accept or abort?",
                options=("accept", "abort"), recommendation=recommendation,
                # Surface the report-metric numbers (CIEDE2000 for SDR / dE_ITP for HDR) under the
                # keys the LLM reads, tagged by `metric`; the cube CONVERGED in `optimize_metric`
                # (dE_ITP), whose values stay available under *_itp. (_severe_optimizer_floor reads the
                # full outcome.digest, which keeps best_*_de in dE_ITP — its thresholds are ITP-scaled.)
                digest={"metric": outcome.digest.get("metric"),
                        "optimize_metric": outcome.digest.get("optimize_metric"),
                        "best_max_de": outcome.digest.get("best_max_de_report"),
                        "best_mean_de": outcome.digest.get("best_mean_de_report"),
                        "neutral_mean_de": outcome.digest.get("neutral_mean_de_report"),
                        "neutral_max_de": outcome.digest.get("neutral_max_de_report"),
                        # Worst floor points WITH zone context (kind/boundary/near_black/
                        # neutral) so in-gamut core damage vs a reachability corner is
                        # decidable from the digest alone (fable Phase 8).
                        "floor_offenders": outcome.digest.get("floor_offenders"),
                        **{k: outcome.digest.get(k) for k in
                           ("above_threshold", "physical_floor", "budget_limited", "converged",
                            "probe_total", "neutral_count")},
                        "best_max_de_itp": outcome.digest.get("best_max_de"),
                        "best_mean_de_itp": outcome.digest.get("best_mean_de"),
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
            wx, wy = self._white_xy()
            scored_set = self._score_verify_samples(samples)
            metrics, lum = scored_set["metrics"], scored_set["lum"]
            metric_name, q, reachable = scored_set["metric"], scored_set["q"], scored_set["reachable"]
            self._last_verify_reachable = reachable
            hdr = self._hdr_target() if spec.is_hdr else None
            summary = summarize_metrics(phase="verification", iteration=0, source=Path(verify_ti3),
                                        patch_metrics=metrics, target_luminance=lum, metric=metric_name)
            # The §0 practically-weighted view (metrics.practical_summary): core (Rec.709 ≤
            # ref-white, reachable) is the practical verdict; `clamped` isolates residuals at
            # the panel's gamut floor so they are read as reachability, never calibration error.
            # Its ``per_signal`` block (V2) is the same split over UNIQUE signals — the gate's basis.
            # With per-patch content weights (--verify-patches-file) or a content distribution
            # (--content-distribution / the profile), it OPENS with ``content_weighted`` — the
            # practical number content sees (score + coverage gap). EVIDENCE ONLY: the gate below
            # never reads it; the LLM weighs it at this seam.
            content_kw, content_errors = self._content_practical_kwargs(verify_ti3, metrics)
            practical = practical_summary(metrics, is_hdr=spec.is_hdr,
                                          gamut_aware=reachable is not None, **content_kw)
            # HELD-OUT view (V1): how much of the verify sits where the calibration was trained
            # (training TI3 signals ∪ build-probe drives, signal + drive space) vs provably not.
            held_rows: Optional[str] = None
            try:
                practical["held_out"], held_rows = self._held_out_evidence(metrics, is_hdr=spec.is_hdr)
            except Exception as exc:  # noqa: BLE001 - evidence must never break the verify gate
                practical["held_out"] = {"available": False,
                                         "reason": f"classification failed ({type(exc).__name__}: {exc})"}
            # QUALITY GATE (owner directive D3, 2026-08-14): OOG patches are a FRAMEWORK, not
            # the meat — the deterministic gate scores the practical core/tube/white buckets,
            # never the OOG-inflated overall. On the first full HDR run the overall avg (6.77)
            # could NEVER pass the 3.0 target because 217/303 verify patches were limits/
            # clamped Rec.2020 targets, while the core sat at 1.01 — the gate said "fail" about
            # reachability, not calibration. SDR: core == overall (every unclamped SDR target is
            # core) but the TUBE check is deliberately NEW for SDR too — a grey-ramp cast is the
            # most visible defect and must not hide behind a colour-diluted average (adversarial
            # review 2026-08-14; escalation-only: recommendation stays apply). `limits` (reachable
            # wide-gamut) is quality-ungated but catastrophe-checked in _severe_verify_failure.
            # Fallback to the legacy overall gate if core is empty (a degenerate set — e.g. a
            # truncated verify — must not vacuously pass).
            within, gate_basis = self._quality_gate(summary, practical, q)
            worst = sorted(metrics, key=lambda m: m.de2000, reverse=True)[:5]
            # Persist the scored evidence (reports/verification_iter00_{metrics,patch_metrics}.json —
            # the artifact the dashboard's /api/patch_metrics serves) via the one shared writer;
            # the event is emitted below through the phase-stamped runlog instead (emit_event=False).
            metrics_mod.write_metrics(
                ctx=self.ctx, phase="verification", iteration=0, source=Path(verify_ti3),
                patch_metrics=metrics, target_luminance=lum, metric=metric_name,
                practical=practical, emit_event=False)
            # Put the scored dE summary on the spine so the dashboard's ΔE big-numbers
            # panel (and the LLM digest) get it — the rich digest below only reaches the
            # adjudicator, not events.jsonl. One event carries the whole panel, in the
            # canonical shape every producer emits (metrics.metrics_scored_payload, P4).
            self.runlog.metrics_scored(
                "verify", **metrics_scored_payload(summary, label="verification",
                                                   practical=practical))
            digest = {"avg_de2000": round(summary.avg_de2000, 3), "p95_de2000": round(summary.p95_de2000, 3),
                      "max_de2000": round(summary.max_de2000, 3), "white_de2000": round(summary.white_de2000, 3),
                      "grayscale_avg_de2000": round(summary.grayscale_avg_de2000, 3),
                      "patch_count": summary.patch_count, "within_quality": within,
                      # What the gate actually scored (practical core/tube/white vs legacy
                      # overall) + the per-check verdicts, so the seam shows WHY, not just
                      # pass/fail (D3, 2026-08-14).
                      "gate": gate_basis,
                      # Only the dE acceptance targets — not the iteration-control knobs that
                      # share MetricThresholds — so the verify seam (the LLM's judgment surface)
                      # sees quality criteria, not loop knobs.
                      "quality_targets": q.acceptance_targets(),
                      "metric": metric_name, "optimize_metric": "dE_ITP",
                      "target_white_xy": [round(wx, 5), round(wy, 5)],
                      "white_provenance": self._resolved_white().provenance,
                      "gamut_aware": reachable is not None,
                      "practical": practical,
                      "worst": [{"rgb": [round(c, 3) for c in m.rgb], "de2000": round(m.de2000, 2),
                                 "gamut_clamped": m.gamut_clamped} for m in worst]}
            lead = (practical.get("content_weighted") or {}).get("headline")
            if lead:
                # Practical numbers LEAD the verify digest (owner 2026-10-09) — evidence, not a gate.
                digest = {"content_weighted": lead, **digest}
            if content_errors:
                digest["content_distribution_errors"] = content_errors
            # V2: the per-UNIQUE-signal view (each signal's ΔE = the mean of its reads) beside the
            # read-weighted headline (``avg_de2000`` stays read-weighted for continuity).
            per = (practical.get("per_signal") or {})
            overall = per.get("overall") or {}
            digest.update({"per_signal_avg": overall.get("avg"), "per_signal_p95": overall.get("p95"),
                           "per_signal_max": overall.get("max"), "n_signals": per.get("n_signals"),
                           "n_reads": per.get("n_reads")})
            # V1: the held-out split (+ the fresh draws this run measured, seed and list).
            digest["held_out"] = practical.get("held_out")
            # The thermal state these numbers represent (--thermal-state; verify = the set's own load).
            digest["thermal_state"] = self._verify_thermal_state()
            if held_rows:
                digest["held_out_rows"] = held_rows   # per-signal rows (class, distances, ΔE)
            draws = self.calib.get("verify_held_out_draws")
            if isinstance(draws, dict):
                measured = {tuple(int(c) for c in row) for row in verify_holdout.to_codes(
                    [s.rgb for s in samples], self._transfer().max_cv)}
                listed = [[int(c) for c in p] for p in draws.get("signals") or ()]
                digest["held_out_draws"] = {
                    **{k: draws.get(k) for k in ("seed", "run_id", "n_requested", "n_drawn", "min_codes",
                                                 "value_range_codes", "saturation_range", "lattice_size",
                                                 "drive_space_checked", "attempts", "rejected")},
                    "n_measured": sum(1 for p in listed if tuple(p) in measured),
                    "signals": listed,
                    "note": "SDR only — HDR fresh draws are a follow-up (gamut-aware hue caps + PQ floor)"}
            # The PRESET verify set without the per-run draws: the population every earlier run's
            # headline was scored over — the run-to-run comparable numbers (the headline above now
            # includes this run's own random draws).
            digest["preset_set"] = self._preset_set_evidence(
                metrics, lum=lum, metric_name=metric_name, is_hdr=spec.is_hdr,
                gamut_aware=reachable is not None, source=Path(verify_ti3))
            if not spec.is_hdr:
                # The white luminance the stack was calibrated to (the refine's / the installed
                # MHC's), the cube's build white, and the nominal — so the verify seam can tell a
                # tone mismatch at the top from a calibration error.
                white_nits, white_source = self._sdr_calibrated_white()
                digest["sdr_white"] = {"calibrated_white_nits": white_nits, "source": white_source,
                                       "cube_target_white_nits": self._cube_target_white_nits(),
                                       "nominal_white_nits": spec.luminance_nits,
                                       # V3: the white the ΔE above is RELATIVE to, stated.
                                       **self._scored_white_evidence(samples, lum, white_nits)}
                kept = ((self.calib["stages"].get("reapply-3dlut") or {}).get("digest") or {}).get("cube_white")
                if kept:
                    digest["cube_white"] = kept
                if self._sdr_in_hdr():
                    try:
                        digest["sdr_in_hdr"] = self._sdr_in_hdr_evidence(samples, lum)
                    except Exception as exc:  # noqa: BLE001 - evidence must never break the verify gate
                        digest["sdr_in_hdr"] = {"error": f"{type(exc).__name__}: {exc}"}
            if spec.is_hdr and hasattr(reachable, "full_primaries"):
                # Level edge primary (D4): the full-drive view, the reclassified patches, the edge's falsification
                # on these reads, and whether the verdict depends on the edge — evidence only, no new pause.
                try:
                    digest.update(self._level_edge_verify_evidence(
                        samples, metrics, q, within, white_xy=(wx, wy), peak_nits=hdr.peak_nits, gamut=reachable))
                except Exception as exc:  # noqa: BLE001 - evidence must never break the verify gate
                    digest["level_edge_evidence_error"] = f"{type(exc).__name__}: {exc}"
            if self.calib.get("flow") == "verify-only":
                # What was measured (installed stack / candidate) and, with --verify-patches-from,
                # the per-bucket deltas vs the source run's RECORDED verify — evidence, no pause.
                digest.update(self._verify_only_evidence(digest, metrics))
            return StageOutcome("verify", "done", digest=digest,
                                data={"within_quality": within, "metrics": {
                                    "avg_de2000": summary.avg_de2000, "p95_de2000": summary.p95_de2000,
                                    "max_de2000": summary.max_de2000, "white_de2000": summary.white_de2000}})

        outcome = self._stage("verify", run)
        if self.calib.get("flow") == "verify-only":
            # verify-only built nothing, so there is no apply/revert gate: the score is evidence
            # (result + report), and a candidate cube's fate is its own seam (verify:candidate).
            return outcome
        d = outcome.digest
        within = outcome.data.get("within_quality")
        severe = self._severe_verify_failure(outcome)
        # The question quotes the numbers the GATE scored (practical core/tube/white when
        # available — D3) with the overall avg as context, so the seam's first line no
        # longer leads with an OOG-inflated headline the digest then has to walk back.
        scored = (d.get("gate") or {}).get("scored") or {}
        if scored:
            tube_txt = scored.get('tube_avg') if scored.get('tube_avg') is not None else "— (none measured)"
            basis_txt = (f"per-signal over {scored.get('n_signals')} signals / {scored.get('n_reads')} reads"
                         if scored.get("basis") == "per_signal" else "read-weighted")
            reads = (f"core avg {scored.get('core_avg')} (p95 {scored.get('core_p95')}, "
                     f"max {scored.get('core_max')}; {basis_txt}), "
                     f"{self._held_out_question_text(d)}, tube {tube_txt}, "
                     f"white {_round3(scored.get('white'))} {d.get('metric', 'ΔE')} "
                     f"(read-weighted overall avg {d.get('avg_de2000')} incl. repeats + "
                     f"gamut-limit/OOG framework{self._preset_question_text(d)})")
        else:
            reads = (f"avg {d.get('metric', 'ΔE')} {d.get('avg_de2000')} "
                     f"(white {d.get('white_de2000')}, max {d.get('max_de2000')})")
        ts = d.get("thermal_state") or {}
        thermal_txt = (f" Thermal state: VIEWING was requested but NOT held ({', '.join(ts['evidence_flags'])}; "
                       "model) — these numbers describe a hotter/other state than real viewing."
                       if ts.get("evidence_flags") else "")
        mw = ts.get("mhc_white") or {}
        if mw.get("line"):
            thermal_txt += f" MHC white: {mw['line']} ({mw.get('stage')})."
        elif mw.get("evidence_flags"):
            thermal_txt += (f" MHC white: refined OUTSIDE the viewing band ({', '.join(mw['evidence_flags'])}; "
                            f"{mw.get('stage')}, model) — the profile's white/greys describe another thermal state "
                            "than viewing.")
        elif mw:
            thermal_txt += f" MHC white ({mw.get('stage')}): refined in the {mw.get('state')!r} state (model)."
        if ts.get("note"):
            thermal_txt += f" Note: {ts['note']}."
        self.adjudicate(AdjudicationRequest(
            key="verify:accept", seam=SEAM_VERIFY, stage="verify",
            question=(f"The new calibration reads {_content_lead_text(d)}{reads} — "
                      f"{'within' if within else 'outside'} the quality targets.{thermal_txt} "
                      "Apply this calibration, or revert to the previous display setup?"),
            options=("apply", "revert"),
            recommendation=("revert" if severe else "apply"),
            # severe → recommend revert (auto/sim reverts a catastrophic result). gate_failed flag
            # → even a NON-severe quality-gate miss escalates under SupervisedAdjudicator, so an
            # unattended run never silently applies a sub-quality calibration at this terminal gate.
            # before_scores: the persisted raw/post-mhc intermediate scores, so apply-vs-revert is
            # judged on the TRAJECTORY (did the calibration improve the panel?), not one absolute
            # number (fable Phase 8, digest-sufficiency).
            digest={**outcome.digest, "severe_failure": severe, "gate_failed": not bool(within),
                    "before_scores": self.calib.get("stage_scores") or None}))
        return outcome

    def _score_verify_samples(self, samples: Sequence[Any], *, reachable: Any = None,
                              reuse_reachable: bool = False) -> dict[str, Any]:
        """Score verify samples exactly as ``stage_verify`` does — the ONE verify scorer (also used
        to re-score a source run's verify.ti3 like-for-like in verify-only, V3).

        Score against the SAME resolved white the pipeline targeted (MHC matrix + grayscale refine,
        3D-LUT target) — not textbook D65 — so a non-zero white strength is the goal here, not scored
        as white error. SDR scores CIEDE2000 against γ-power/sRGB RELATIVE TO THE MEASURED WHITE
        (``luminance=None`` → :func:`metrics.infer_target_luminance`: the brightest full-white read —
        the ColourSpace/CalMAN convention; the digest states it, V3); HDR scores dE_ITP against
        PQ/Rec.2020 (the metric the cube converges in — CIEDE2000's Lab is meaningless at HDR absolute
        luminance; PQ is absolute, no white normalisation), with looser, LLM-negotiated targets."""
        spec = self._spec()
        wx, wy = self._white_xy()
        # The target the cube was BUILT for: the run's level edge when its memo enables it (D4), else the
        # full-drive triangle (_reachable_gamut); every bucket below is deterministic under it.
        # ``reuse_reachable`` (a re-score inside the same verify) passes the gamut the live verify
        # resolved, so its level-edge seam is never asked twice.
        if not reuse_reachable:
            reachable = self._reachable_gamut(stage="verify") if spec.is_hdr else None
        if spec.is_hdr:
            hdr = self._hdr_target()
            metrics, lum = score_samples_hdr(list(samples), white_xy=(wx, wy), peak_nits=hdr.peak_nits,
                                             oog_mapping=self._oog_mapping(),
                                             reachable_primaries=reachable)
            # Advisory HDR defaults (dE_ITP), overlaid by the profile's optional
            # ``quality: {hdr: {...}}`` block — the same policy source the stage-CLI
            # scorer reads — then negotiated by the assistant at the verify seam after
            # the first refinement round (design §7).
            return {"metrics": metrics, "lum": lum, "metric": "dE_ITP", "reachable": reachable,
                    "q": hdr_metric_thresholds(self.profile.quality_policy)}
        metrics, lum = score_samples(list(samples), gamma=spec.gamma, white_xy=(wx, wy))
        return {"metrics": metrics, "lum": lum, "metric": "CIEDE2000", "reachable": None,
                "q": self.profile.quality}

    def _measured_draw_codes(self) -> set[tuple[int, int, int]]:
        """The fresh held-out draws this verify measured: this run's memo, plus — a verify-only run
        re-measuring a source's exact list (``--verify-patches-from``) — the source run's draws."""
        codes = {tuple(int(c) for c in p)
                 for p in (self.calib.get("verify_held_out_draws") or {}).get("signals") or ()}
        source = self._verify_source_record() if self.calib.get("flow") == "verify-only" else None
        if source is not None:
            try:
                src_state = json.loads((Path(str(source.get("run"))) / "dlc_state.json")
                                       .read_text(encoding="utf-8"))
                codes |= {tuple(int(c) for c in p) for p in
                          ((src_state.get("calib") or {}).get("verify_held_out_draws") or {}).get("signals") or ()}
            except (OSError, ValueError, TypeError):
                pass
        return codes  # type: ignore[return-value]

    def _preset_set_evidence(self, metrics: Sequence[Any], *, lum: float, metric_name: str,
                             is_hdr: bool, gamut_aware: bool, source: Path) -> dict[str, Any]:
        """The verify scored over the PRESET set only — every read of a fresh held-out draw removed
        (V1 follow-up): the headline ``avg_de2000`` and the read-weighted buckets now include this
        run's random draws, so a run-to-run comparison of headlines would mix different colour sets.
        These numbers are over the same preset population every run (and every pre-V1 run) measured.
        Same scored reads, same white — nothing is re-scored."""
        draws = self._measured_draw_codes()
        codes = verify_holdout.to_codes([m.rgb for m in metrics], self._transfer().max_cv)
        keep = [m for m, code in zip(metrics, codes) if tuple(int(c) for c in code) not in draws]
        out: dict[str, Any] = {"note": "the preset verify set without the per-run fresh draws — the "
                                       "run-to-run comparable population",
                               "draw_reads_excluded": len(metrics) - len(keep), "n_reads": len(keep)}
        if not keep:
            out["available"] = False
            return out
        s = summarize_metrics(phase="verification-preset", iteration=0, source=source,
                              patch_metrics=list(keep), target_luminance=lum, metric=metric_name)
        p = practical_summary(list(keep), is_hdr=is_hdr, gamut_aware=gamut_aware)
        per = p["per_signal"]
        out.update({"available": True, "n_signals": per["n_signals"],
                    "avg_de2000": round(s.avg_de2000, 3), "p95_de2000": round(s.p95_de2000, 3),
                    "max_de2000": round(s.max_de2000, 3), "white_de2000": round(s.white_de2000, 3),
                    "per_signal_avg": per["overall"]["avg"], "per_signal_p95": per["overall"]["p95"],
                    "per_signal_max": per["overall"]["max"],
                    "core": {"read_weighted": p["core"], "per_signal": per["core"]},
                    "tube": {"read_weighted": p["tube"], "per_signal": per["tube"]}})
        return out

    @staticmethod
    def _scored_white_evidence(samples: Sequence[Any], scored_nits: float,
                               calibrated_nits: Optional[float]) -> dict[str, Any]:
        """V3 — the SDR scoring white, stated: the luminance every CIEDE2000 above is relative to
        (the MEASURED white — :func:`metrics.infer_target_luminance`, the industry convention), where
        it came from (how many full-white reads; their mean beside the max the rule takes), and how
        far it sits from the white the stack was calibrated to."""
        whites = [float(s.xyz[1]) for s in samples
                  if min(s.rgb) >= 0.99 and math.isfinite(float(s.xyz[1])) and float(s.xyz[1]) > 0.0]
        source = {"kind": "measured",
                  "rule": ("max Y of the full-white reads (metrics.infer_target_luminance)" if whites
                           else "no full-white read: the brightest grey / patch (infer_target_luminance "
                                "fallback)"),
                  "n_white_reads": len(whites),
                  "mean_white_nits": round(sum(whites) / len(whites), 4) if whites else None}
        cal = _as_float_local(calibrated_nits)
        return {"scored_white_nits": round(float(scored_nits), 4), "scored_white_source": source,
                "white_luminance_vs_calibrated_pct": (round(100.0 * (float(scored_nits) / cal - 1.0), 3)
                                                      if cal else None)}

    @staticmethod
    def _preset_question_text(digest: Mapping[str, Any]) -> str:
        """The preset-set clause of the verify:accept question: when fresh draws were measured, the
        headline includes them, so quote the run-to-run comparable preset-only numbers beside it."""
        preset = digest.get("preset_set") or {}
        if not preset.get("available") or not preset.get("draw_reads_excluded"):
            return ""
        return (f"; preset set without the {preset['draw_reads_excluded']} draw reads: read-weighted "
                f"avg {preset.get('avg_de2000')}, per-signal avg {preset.get('per_signal_avg')}")

    @staticmethod
    def _held_out_question_text(digest: Mapping[str, Any]) -> str:
        """The held-out clause of the verify:accept question (V1): the held-out per-signal avg next
        to the full per-signal avg, and whether it was gated."""
        held = digest.get("held_out") or {}
        gate = (digest.get("gate") or {}).get("held_out_gate") or {}
        if not held.get("available"):
            return f"held-out — n/a ({held.get('reason') or 'not classified'})"
        ho = held.get("held_out") or {}
        strict = held.get("strict_held_out") or {}
        coin = held.get("coincident") or {}
        th = held.get("thresholds") or {}
        txt = (f"held-out avg {ho.get('avg')} over {ho.get('n', 0)} signals > "
               f"{th.get('held_out_gt_codes', verify_holdout.HELD_OUT_CODES):g} codes from training "
               f"(strict off-lattice {strict.get('avg')}, n {strict.get('n', 0)}; coincident "
               f"{coin.get('avg')}, n {coin.get('n', 0)})")
        fresh = held.get("fresh_draws") or {}
        if fresh.get("n"):
            txt += f" incl. {fresh.get('n')} fresh draws avg {fresh.get('avg')}"
        return txt + ("" if gate.get("gated") else f" [reported, not gated: {gate.get('reason')}]")

    @staticmethod
    def _quality_gate(summary, practical: dict, q) -> tuple[bool, dict]:
        """The deterministic verify quality gate (D3, 2026-08-14): score the practical
        core/tube/white buckets against the acceptance targets — OOG/limits patches are
        reachability framework, never gate inputs. Returns ``(within, gate_basis)`` where
        ``gate_basis`` records what was scored and each check's verdict (seam evidence).

        * ``core``  — avg/p95/max vs the mode's acceptance targets (the practical verdict).
        * ``tube``  — avg vs the avg target (a neutral cast must not hide behind core colour).
        * ``white`` — the summary white vs the white target (unchanged).
        * core/tube are scored PER UNIQUE SIGNAL (V2, ``practical["per_signal"]``) — a signal read
          7× counts once — falling back to the read-weighted buckets when absent;
          ``scored["basis"]`` records which.
        * ``held_out_avg`` (V1) — the held-out per-signal core avg vs the avg target, only when the
          held-out bucket holds >= ``metrics.HELD_OUT_GATE_MIN_SIGNALS`` signals; below that it is
          reported, never gated (``held_out_gate`` says why).
        * Fallback: an empty core bucket (degenerate/truncated set) uses the legacy overall
          summary gate — a gate must never pass vacuously.
        """
        practical = practical or {}
        core = practical.get("core") or {}
        tube = practical.get("tube") or {}
        if not core.get("n"):
            within = (summary.avg_de2000 <= q.avg_de2000 and summary.p95_de2000 <= q.p95_de2000
                      and summary.max_de2000 <= q.max_de2000
                      and summary.white_de2000 <= q.white_de2000)
            return within, {"basis": "overall (legacy fallback: empty core bucket)",
                            "checks": {"avg": summary.avg_de2000 <= q.avg_de2000,
                                       "p95": summary.p95_de2000 <= q.p95_de2000,
                                       "max": summary.max_de2000 <= q.max_de2000,
                                       "white": summary.white_de2000 <= q.white_de2000}}
        # The shared bucket selection (metrics.practical_gate_view) — the stage-CLI advisory verdict
        # (stages._common.policy_advice) reads the very same view.
        view = metrics_mod.practical_gate_view(practical)
        core, tube, read_core = view["core"], view["tube"], view["read_core"]
        per_signal = view["basis"] == "per_signal"
        checks = {
            "core_avg": core["avg"] <= q.avg_de2000,
            "core_p95": core["p95"] <= q.p95_de2000,
            "core_max": core["max"] <= q.max_de2000,
            # No tube bucket (a colour-only set) must not vacuously pass the cast check —
            # but DLC sequences always carry the neutral tube, so treat missing as pass
            # only when core itself covered neutrals is unknowable; be strict instead.
            "tube_avg": bool(tube.get("n")) and tube["avg"] <= q.avg_de2000,
            "white": summary.white_de2000 <= q.white_de2000,
        }
        scored: dict[str, Any] = {"basis": view["basis"],
                                  "core_avg": core["avg"], "core_p95": core["p95"],
                                  "core_max": core["max"], "core_n": core["n"],
                                  "tube_avg": tube.get("avg"), "tube_n": tube.get("n"),
                                  "white": summary.white_de2000}
        if per_signal:
            scored.update(n_signals=view["n_signals"], n_reads=view["n_reads"],
                          read_weighted_core_avg=read_core.get("avg"))
        # V1 held-out check: gated only on a bucket big enough to mean something.
        held_gate = view["held_out_gate"]
        if held_gate["gated"]:
            checks["held_out_avg"] = held_gate["held_out_avg"] <= q.avg_de2000
            scored.update(held_out_avg=held_gate["held_out_avg"], held_out_n=held_gate["held_out_n"])
        label = "practical core+tube+white (D3)" + (" + held-out avg (V1)" if held_gate["gated"] else "")
        basis = {"basis": label, "checks": checks, "scored": scored, "held_out_gate": held_gate}
        return all(checks.values()), basis

    def _severe_verify_failure(self, outcome: StageOutcome) -> bool:
        """Is a failed verify CATASTROPHIC (recommend revert) rather than merely
        sub-quality (recommend apply, escalate via gate_failed)? These are not quality
        thresholds — they answer "is the panel visibly WORSE than uncalibrated?".

        Provenance (fable audit Phase 6, P3): each constant is ~10× its mode's advisory
        acceptance target (SDR avg 20 vs gate 1.5→severe at ~13×, p95 40 vs 3.0; HDR avg
        30 vs 3.0, p95 60 vs 6.0) — an order of magnitude past the gate is a broken
        install (wrong LUT, collapsed channel, scoring mismatch), never a marginal miss.
        The recorded hardware baselines sit two orders below (SDR 0.41 avg; HDR 3.26
        grayscale WITH gamut-floor patches counted, pre-P1). max/white at 100 ≈ "a
        primary/white read as a different colour entirely" (full-scale Lab/ITP error);
        SDR white at 50 is tighter because a mid-double-digit ΔE2000 white cast is
        already unmistakably broken on any SDR desktop. Deliberately blunt: the severe
        path only flips the RECOMMENDATION to revert — the seam still decides."""
        if outcome.data.get("within_quality"):
            return False
        d = outcome.digest or {}
        # Judge severity on the SAME basis the gate scored (D3): when the practical gate
        # ran, an OOG/CLAMPED residual must not read as "catastrophic install" while the
        # core is fine — but `limits` is REACHABLE territory (wide-gamut/bright targets
        # inside the measured native gamut), i.e. honest calibration signal, so the
        # catastrophe check spans core AND limits (adversarial finding, 2026-08-14: a
        # poisoned cube whose wreck lives entirely outside Rec.709-core must not pass as
        # non-severe). Only `clamped` — expected clip markers — stays out. White stays
        # the summary white either way.
        reach: dict = {}
        if str((d.get("gate") or {}).get("basis", "")).startswith("practical"):
            practical = d.get("practical") or {}
            # The gate's own statistic: per unique signal when it scored that (V2).
            if ((d.get("gate") or {}).get("scored") or {}).get("basis") == "per_signal":
                practical = practical.get("per_signal") or practical
            buckets = [b for b in (practical.get("core"), practical.get("limits"))
                       if b and b.get("n")]
            if buckets:
                reach = {k: max(_as_float_local(b.get(k)) or 0.0 for b in buckets)
                         for k in ("avg", "p95", "max")}
        avg = reach.get("avg", _as_float_local(d.get("avg_de2000")) or 0.0)
        p95 = reach.get("p95", _as_float_local(d.get("p95_de2000")) or 0.0)
        max_de = reach.get("max", _as_float_local(d.get("max_de2000")) or 0.0)
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
        name = f"{safe_display}_{self.run_date.isoformat()}_{self.mode}"
        if self.calib.get("flow") == "refine-mhc":
            # Its own folder, stamped with THIS run's id (stable across resume): a same-day
            # refine-mhc must never overwrite the source run's report/deliverable (its 3D-LUT build
            # record would be lost — even on a revert).
            stamp = "_".join(self.ctx.root.name.split("_")[:2])
            name += f"_refine-mhc_{stamp}"
        elif self.calib.get("flow") == "verify-only":
            # Keyed by THIS run's full id (date_time_micro): repeated verifies of one stack — same
            # day, back to back — must never overwrite each other's report / measurements.
            stamp = "_".join(self.ctx.root.name.split("_")[:3])
            name += f"_verify-only_{stamp}"
        folder = out / name
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
        # refine-mhc keeps the source run's cube (re-applied, not rebuilt) — it is the deliverable too.
        cube_src = sdat("build-install-3dlut").get("cube_path") or sdat("reapply-3dlut").get("cube_path")
        cube_out = None
        if cube_src and Path(cube_src).exists():
            spec = self._spec()
            # The HDR deliverable is labelled with the CALIBRATED peak (the resolved max-sustained
            # ceiling), not the profile's 1600 viewing peak — the cube IS the calibration to that
            # peak (Task C / one source of truth). SDR uses the white the cube was BUILT for (the
            # refined / installed white; a kept cube with an unrecorded white, the nominal).
            label_nits = (self._hdr_target().peak_nits if spec.is_hdr
                          else (self._cube_target_white_nits() or spec.luminance_nits))
            cube_out = results_dir / descriptive_cube_name(
                date=self.run_date.isoformat(), display=self.display.short_name, mode=self.mode,
                colorspace=_gamut_label(spec.colorspace, is_hdr=spec.is_hdr, gamma=spec.gamma),
                transfer=_transfer_token(is_hdr=spec.is_hdr, gamma=spec.gamma),
                luminance_nits=label_nits)
            # A kept cube may already BE this deliverable (same display/date/label) — never copy a
            # file onto itself (shutil.SameFileError).
            if Path(cube_src).resolve() != cube_out.resolve():
                shutil.copy2(cube_src, cube_out)
        # Copy the verification TI3.
        verify_ti3 = sdat("measure:verify").get("ti3")
        ti3_out = None
        if verify_ti3 and Path(verify_ti3).exists():
            ti3_out = results_dir / "measurements.ti3"
            shutil.copy2(verify_ti3, ti3_out)

        payload = {
            "flow": self.calib.get("flow"), "monitor": self.monitor, "mode": self.mode,
            "content_mode": self.content_mode,
            "display": self.display.name, "target": self.target_name, "date": self.run_date.isoformat(),
            "whitepoint": sd("whitepoint") or None,
            "mhc": sd("build-install-mhc") or sd("install-mhc") or None,
            "mhc_refine": sd("refine-mhc-grayscale") or sd("refine-mhc-cube") or None,
            "source_run": self.calib.get("source_run"),
            "lut3d": ({k: sd("build-install-3dlut").get(k) for k in
                       ("converged", "best_max_de", "best_mean_de", "best_max_de_report",
                        "best_mean_de_report", "metric", "optimize_metric", "above_threshold",
                        "physical_floor", "cube_path")} if sd("build-install-3dlut")
                      # refine-mhc KEEPS the source run's cube: carry that cube's build record.
                      else (dict(sd("seed-from-run").get("source_lut3d") or {},
                                 kept_from_run=((sd("seed-from-run").get("kept_installed_cube") or {})
                                                .get("cube_run") or sd("seed-from-run").get("source_run")),
                                 kept_cube_path=sd("reapply-3dlut").get("cube_path"))
                            if sd("seed-from-run") else None)),
            "verification": sd("verify") or None,
            "decisions": self.calib.get("decisions", {}),
            "deliverables": {"cube": str(cube_out) if cube_out else None,
                             "profile_name": (sdat("build-install-mhc").get("profile_name")
                                              or sdat("install-mhc").get("profile_name")),
                             "measurements_ti3": str(ti3_out) if ti3_out else None},
            "display_analysis": analysis,   # the LLM fills this at report time
        }
        thermal_summary = self._thermal_stage_summary()
        if thermal_summary is not None:
            # --thermal-state viewing: which stage ran in which thermal state (requested vs achieved).
            payload["thermal_state"] = thermal_summary
        if self.calib.get("flow") == "verify-only":
            # A measurement of an installed / candidate stack — nothing built, nothing committed.
            payload["verify_only"] = {
                "measured_stack": (sd("verify").get("verify_only") or {}).get("measured_stack"),
                "candidate": self.calib.get("verify_candidate"),
                "verify_source": sd("verify-source") or None,
                "preheat": self._preheat_policy() or "auto",
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
            atomic_write_text(runs_dir() / "active.json", json.dumps(pointer, indent=2))
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
        if self._arg_conflicts:
            msg = ("resume requested " + ", ".join(
                f"{c['field']}={c['requested']}" for c in self._arg_conflicts)
                + " but this run's memoised stages were made with " + ", ".join(
                f"{c['field']}={c['persisted']}" for c in self._arg_conflicts)
                + " — refusing to diverge from them: resume without the flag (or with the recorded "
                "value), or start a NEW run for the new value."
                + "".join(f" ({c['reason']})" for c in self._arg_conflicts if c.get("reason")))
            self.runlog.anomaly("run", run_arg_conflict=True, conflicts=self._arg_conflicts,
                                message=msg)
            self.runlog.run_done("aborted", aborted_at="resume-args", message=msg)
            return CalibrationResult(
                flow=flow, monitor=self.monitor, mode=self.mode, target=self.target_name,
                status="aborted", stages=list(self.calib["stages"].keys()), results_dir=None,
                report_path=None, digest={"aborted_at": "resume-args", "message": msg,
                                          "conflicts": self._arg_conflicts})
        bad = self._content_mode_problem(flow)
        if bad:
            self.runlog.anomaly("run", kind="content_mode", message=bad)
            self.runlog.run_done("aborted", aborted_at="run-args", message=bad)
            return CalibrationResult(
                flow=flow, monitor=self.monitor, mode=self.mode, target=self.target_name,
                status="aborted", stages=list(self.calib["stages"].keys()), results_dir=None,
                report_path=None, digest={"aborted_at": "run-args", "message": bad})
        self._publish_active_pointer()   # let the dashboard find this run (and the next)
        self._emit_header()   # open the spine with what we know; enriched as the run proceeds
        if self._preheat_change:
            self.runlog.note("run", f"--preheat changed on resume: {self._preheat_change['from']} -> "
                                    f"{self._preheat_change['to']} (measure stages not yet run use the "
                                    "new policy; each measure digest records its own)",
                             preheat_change=self._preheat_change)
        if self._enable_watchdog:
            self.liveness.start()   # backstop thread (live runs only; tests don't spin threads)
        # Own our own keep-awake for the WHOLE run (not just the measure stages): the
        # compute-bound 3D-LUT build phase presents no patch, so the fullscreen presenter
        # can't be relied on to hold the display lock there — a gap that on this rig's
        # aggressive power plan (display off 5 min, sleep 15 min) blanks the panel mid-run
        # and corrupts the next read. Released in finally (incl. on a seam abort) so the
        # request never leaks past the run; no-op on non-Windows. stage_measure asserts it
        # again per-stage (reentrant) as defence-in-depth for direct/partial-flow callers.
        try:
            with keep_awake(reason=f"dlc calibration run ({flow})"):
                result = self._run_flow(flow)
        except AdjudicationRequired:
            raise          # a PAUSE: the run continues later in the measurement state
        except BaseException:
            self._restore_viewing_layers()   # any other exit is terminal for this run
            raise
        finally:
            if self._enable_watchdog:
                self.liveness.stop()
        self._restore_viewing_layers()       # completed / reverted / aborted (result returned)
        return result

    def _run_flow(self, flow: str) -> CalibrationResult:
        try:
            if flow == "full":
                return self._flow_full()
            if flow == "mhc-only":
                return self._flow_mhc_only()
            if flow == "3dlut-only":
                return self._flow_3dlut_only()
            if flow == "grayscale-wb":
                return self._flow_grayscale_wb()
            if flow == "refine-mhc":
                return self._flow_refine_mhc()
            if flow == "verify-only":
                return self._flow_verify_only()
            if flow == "build-correction":
                return self._flow_build_correction()
            if flow == "characterize":
                return self._flow_characterize()
            if flow == "hdr":
                # P13 (fable Phase 7a): HDR is a MODE, not a flow — `--mode HDR` on the normal
                # flows runs the full HDR pipeline (PQ target, HDR refine, dE_ITP verify). This
                # signpost stub explains that instead of the stale "post-v1" claim it used to
                # carry; it deliberately does NOT auto-route, because the run's mode is fixed at
                # creation (the manifest) and silently switching it here would be run-spec drift.
                raise CalibrationAborted(StageOutcome(
                    "resolve-target", "aborted",
                    digest={"message": (
                        "'hdr' is not a flow — HDR is a MODE. Run the normal flows in HDR: "
                        "`--mode HDR --flow full` (or mhc-only / 3dlut-only / characterize). "
                        "The mode selects the display's hdr_target, the PQ transfer, and the "
                        "HDR refine stages.")}))
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

    def _raw_extend_to_cv(self) -> Optional[int]:
        """Full-drive headroom extension for the RAW characterization ramp (HDR only): the code
        range between the target-peak cap and full drive, measured sparsely (GREY only) so the
        build sees the panel's native near-peak NEUTRAL roll-off. On a panel that renders the peak
        CODE below the peak NITS (LG C6 2026-09-02: 603.6-nit code → 477 read, full drive → 575),
        a cube fitted only up to the peak code can never invert the roll-off (``invert_monotone``
        is bounded by the measured signal range) and the measured ceiling is bound too low.
        ``None`` when the ramp is already unbounded (SDR) — nothing to extend."""
        if self._patch_max_cv() is None:
            return None
        return self._transfer().max_cv

    def _ramp_patches(self, *, gamut_aware: bool = False,
                      extend_full_drive: bool = False) -> list[tuple[int, int, int]]:
        # gamut_aware=True (VERIFY only): cap colour-ramp saturation to the panel's reachable gamut
        # so saturated verify patches land where the panel can render. RAW stays uncapped (it needs
        # full-saturation pure channels to characterize the panel — see build_ramp_set) and passes
        # extend_full_drive=True (the headroom extension above the peak-code cap; _raw_extend_to_cv).
        caps = self._hue_sat_caps() if gamut_aware else None
        extend = self._raw_extend_to_cv() if extend_full_drive else None
        return build_ramp_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                              max_cv=self._patch_max_cv(), hue_sat_caps=caps,
                              extend_to_cv=extend)

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
        except Exception as exc:  # noqa: BLE001 — generation must never crash on an optional refinement
            # …but a SILENT fallback is invisible in every digest (fable Phase 8, from the 7a
            # lead): an HDR verify ramp losing its reachable-saturation cap means saturated
            # patches land at unreachable target primaries and read as inflated frontier dE.
            # Tell the spine once so the LLM/dashboard can attribute the frontier numbers
            # (per-patch gamut_clamped flags still label them in scoring).
            if not getattr(self, "_caps_unavailable_noted", False):
                self._caps_unavailable_noted = True
                self.runlog.note(
                    self.runlog.phase or "run",
                    "caps_unavailable: reachable-saturation caps could not be computed "
                    f"({type(exc).__name__}: {exc}) — saturated ramp/verify patches are UNCAPPED "
                    "this run; expect inflated dE at unreachable target primaries (reachability, "
                    "not calibration error)", level="WARN")
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

    def _refine_neutral_patches(self, base_cube_path: Path, rowsums: Sequence[float],
                                cap_nits: float) -> tuple[list[tuple[int, int, int]], list[float]]:
        """The HDR closed-loop refine's neutral ramp: the uniform ramp (:meth:`_neutral_patches`)
        PLUS ``patch_sizes.neutral_top_pins`` data-driven pins in the last ``neutral_top_band`` of the
        range below the cap, placed where the BUILD's base cube bends most
        (:func:`dlc.mhc_cube.refine_top_pins`) — the refine interpolates its correction factors
        linearly between pins, and the panel's near-peak roll-off lives in exactly that segment
        (run 120740: +5 % luminance / y +0.011 at 0.80 between the 0.7605 and 0.8113 pins). Each pin
        is ONE extra (bright, fast) neutral read per refine round. Returns ``(patches, top_pins)``
        (``top_pins`` as signals, for the digest). Any failure falls back to the uniform ramp — the
        pins are a refinement, never a precondition."""
        base = self._neutral_patches()
        ps = self.patch_sizes
        if ps.neutral_top_pins <= 0:
            return base, []
        try:
            from .mhc_cube import read_1d_cube, refine_top_pins

            transfer = self._transfer()
            max_cv = transfer.max_cv
            cap_cv = self._patch_max_cv() or max_cv
            existing = sorted({p[0] / max_cv for p in base})
            pins = refine_top_pins(read_1d_cube(Path(base_cube_path)), rowsums, float(cap_nits),
                                   existing=existing, count=ps.neutral_top_pins,
                                   band=ps.neutral_top_band)
        except Exception as exc:  # noqa: BLE001 - a refinement, never a precondition
            self.ctx.log(f"refine top pins skipped ({type(exc).__name__}: {exc}); uniform ramp only")
            return base, []
        have = {p[0] for p in base}
        extra = sorted({min(cap_cv, max(0, int(round(s * max_cv)))) for s in pins} - have)
        if not extra:
            return base, []
        patches = build_neutral_set(ps, transfer, warm_tau=self._warm_tau(), max_cv=self._patch_max_cv(),
                                    extra_levels=extra)
        return patches, [round(v / max_cv, 6) for v in extra]

    def _grayscale_wb_patches(self) -> list[tuple[int, int, int]]:
        return build_grayscale_wb_set(self.patch_sizes, self._transfer(), max_cv=self._patch_max_cv())

    def _grayscale_wb_verify_patches(self) -> list[tuple[int, int, int]]:
        """The grey-ramp verify sequence: the SAME points as the tune set, visited in the
        outside-in alternating order (D4) so the verify holds the same roughly-flat APL as
        the tune instead of cooling through an ascending dark half. Scoring is per-patch
        (order-independent), so only the measurement rhythm changes."""
        patches = self._grayscale_wb_patches()
        return [patches[i] for i in outside_in_indices(len(patches))]

    def _verify_patches(self, *, gamut_aware: bool = True) -> list[tuple[int, int, int]]:
        # The "cover all bases" QC set (see build_verify_set): dense grey/PQ + shadow toe, colour
        # only above the shadow band, gamut-capped. gamut_aware caps saturated hues to the panel's
        # reachable gamut (HDR; None for SDR/degenerate — falls back to uncapped).
        caps = self._hue_sat_caps() if gamut_aware else None
        return build_verify_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                max_cv=self._patch_max_cv(), hue_sat_caps=caps)

    def _verify_measure_patches(self) -> list[tuple[int, int, int]]:
        """The verify set a flow MEASURES: the standard QC set (:meth:`_verify_patches`) plus this
        run's fresh held-out draws (V1, SDR) spread through its core. The draws need the training
        set, which exists by the time ``measure:verify`` runs."""
        base = self._verify_patches()
        rec = self._held_out_draw_record(base)
        draws = [tuple(int(c) for c in p) for p in (rec or {}).get("signals") or ()]
        if not draws:
            return base
        return insert_held_out_draws(base, draws, self.patch_sizes, self._transfer(),  # type: ignore[arg-type]
                                     max_cv=self._patch_max_cv())

    def _held_out_draw_record(self, base: Sequence[tuple[int, int, int]]) -> Optional[dict[str, Any]]:
        """This run's fresh held-out verify draws (V1): ``patch_sizes.verify_held_out_draws`` colours
        drawn with a seed derived from the run id, >= 8 codes from every training signal / probe
        drive (in drive space too through this run's cube), off-lattice, no duplicate of ``base``.

        Memoised in the run record WITH the fingerprint of the training set it was drawn against
        (:func:`verify_holdout.training_key`): a resume, a remeasure of the verify or a crash replay
        re-uses the identical list while that training stands; when it changed — an adaptive
        re-plan, a forced / resumed re-build (a new probe attempt), a re-measured post-MHC set — the
        memo is superseded and the draw re-made against the live training (same seed). A verify that
        was already measured keeps exactly what it measured (never drawn retroactively; ``--force``
        re-measures, so it re-checks). ``None`` when off (knob 0 / HDR — follow-up)."""
        n = held_out_draws_apply(self.patch_sizes, self._transfer())
        memo = self.calib.get("verify_held_out_draws")
        memo = memo if isinstance(memo, dict) and isinstance(memo.get("signals"), list) else None
        measured = ((self.calib.get("stages") or {}).get("measure:verify") or {}).get("status") == "done"
        if measured and not self.force:
            return memo
        if n <= 0:
            if memo is not None:   # the knob went to 0: this verify measures no draws — say so
                self.calib.pop("verify_held_out_draws", None)
                self._save()
            return None
        transfer = self._transfer()
        max_cv = transfer.max_cv
        training = self._held_out_training()
        key = verify_holdout.training_key(training, max_cv=max_cv)
        if memo is not None and memo.get("training_key") == key and memo.get("n_requested") == n:
            return memo
        excl = [verify_holdout.to_codes(training["training_signals"], max_cv),
                training["probe_drives"]] if training.get("available") else []
        rec = verify_holdout.draw_held_out_signals(
            n, seed=verify_holdout.run_seed(self.ctx.root.name), max_cv=max_cv,
            value_floor_cv=int(round(self.patch_sizes.verify_color_min_signal * max_cv)),
            cap_cv=self._patch_max_cv() or max_cv,
            exclude_codes=np.vstack(excl) if excl else None, existing=base,
            cube=training.get("cube"), lattice_size=int(training.get("lattice_size")
                                                         or verify_holdout.DEFAULT_LATTICE_SIZE))
        rec["run_id"] = self.ctx.root.name
        rec["training_key"] = key
        rec["exclusion"] = (training.get("provenance") if training.get("available")
                            else {"none": training.get("reason")})
        if memo is not None:
            rec["superseded"] = {"training_key": memo.get("training_key"), "n_drawn": memo.get("n_drawn"),
                                 "reason": "the training set the earlier draws were drawn against changed "
                                           "(re-plan / re-built cube / re-measured post-MHC / knob)"}
        self.calib["verify_held_out_draws"] = rec
        self._save()
        if self.runlog is not None:
            self.runlog.emit("INFO", "measure:verify", "held_out_draws", tier="digest",
                             **{k: rec.get(k) for k in ("seed", "n_requested", "n_drawn", "attempts",
                                                        "rejected", "min_codes", "drive_space_checked",
                                                        "training_key", "superseded")})
        return rec

    def _held_out_training(self) -> dict[str, Any]:
        """The TRAINING set the verify is classified against (V1) — :func:`verify_holdout.training_context`
        over this run's record (this run's TI3s + probe drives, the source run of a kept cube)."""
        return verify_holdout.training_context(self.ctx.root, self.calib,
                                               max_cv=self._transfer().max_cv)

    def _held_out_evidence(self, metrics: Sequence[Any], *, is_hdr: bool) -> tuple[dict[str, Any],
                                                                                   Optional[str]]:
        """The verify's held-out view (V1, :func:`verify_holdout.held_out_view` — the same pure
        function the score CLI uses): per-signal ΔE stats per class over the gate's population, the
        fresh draws' own stats; the per-signal rows are persisted to
        ``reports/verification_iter00_held_out.json`` (run-relative path returned) for the judge to
        dig into. Evidence only."""
        summary, rows = verify_holdout.held_out_view(
            list(metrics), run_root=self.ctx.root, calib=self.calib,
            bit_depth=self._transfer().bit_depth, is_hdr=is_hdr, draws=self._measured_draw_codes())
        if rows is None:
            return summary, None
        try:
            path = self.ctx.root / "reports" / "verification_iter00_held_out.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, allow_nan=False),
                            encoding="utf-8")
            return summary, str(path.relative_to(self.ctx.root)).replace("\\", "/")
        except (OSError, ValueError) as exc:   # evidence only — never break the verify
            return summary, f"(not written: {type(exc).__name__}: {exc})"

    def flow_patch_counts(self, flow: str) -> dict[str, Any]:
        return flow_patch_counts(flow, self.patch_sizes, self._transfer(),
                                 max_cv=self._patch_max_cv(),
                                 raw_extend_to_cv=self._raw_extend_to_cv())

    def _patch_plan_record(self, flow: str) -> dict[str, Any]:
        transfer = self._transfer()
        plan = self.flow_patch_counts(flow)
        record = {
            **plan,
            "flow": flow,
            "bit_depth": self.bit_depth,
            "patch_sizes": self._fingerprinted_patch_sizes(),
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
        source = self._verify_source_record() if flow == "verify-only" else None
        if source is not None:
            # --verify-patches-from: the run measures the SOURCE's exact verify list, not this run's
            # preset — the plan's size and identity are that list (a different source = a new plan).
            n = len(source.get("patches") or ())
            record["stages"] = {"verify": n}
            record["total_patches"] = n
            record["verify_source"] = {"run": source.get("run"),
                                       "patches_fingerprint": source.get("patches_fingerprint"),
                                       "patch_source": source.get("patch_source")}
            record.pop("verify_held_out_draws", None)   # the source's exact list: no fresh draws
        listed = self._verify_patches_file_record() if flow == "verify-only" else None
        if listed is not None:
            # --verify-patches-file: the file's list IS the plan (a different file = a new plan).
            n = len(listed.get("codes") or ())
            record["stages"] = {"verify": n}
            record["total_patches"] = n
            reads = [int(r or 0) for r in listed.get("min_reads") or ()]
            record["verify_patches_file"] = {"path": listed.get("path"),
                                             "patches_fingerprint": listed.get("patches_fingerprint"),
                                             "order": listed.get("order") or "file",
                                             "min_reads_total": sum(max(1, r) for r in reads) if reads else None}
            record.pop("verify_held_out_draws", None)   # the file's exact list: no fresh draws
        ident = dict(record)
        draws = int(ident.pop("verify_held_out_draws", 0) or 0)
        if draws and self.patch_sizes.verify_held_out_draws == PatchSizes().verify_held_out_draws:
            # The fresh held-out draws (V1) arrived after plans were approved: at the default knob they
            # are left out of the plan IDENTITY (the counts it hashes exclude them) so an in-flight
            # run's approved fingerprint survives the upgrade — the record itself still counts them.
            # A non-default knob is in patch_sizes and so changes the identity as any override does.
            ident["stages"] = {**ident["stages"], "verify": ident["stages"]["verify"] - draws}
            ident["total_patches"] = ident["total_patches"] - draws
        payload = json.dumps(ident, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return {**record, "fingerprint": hashlib.sha256(payload).hexdigest()[:16]}

    # PatchSizes knobs added after plans were already approved: left out of the plan identity while
    # at their defaults, so an in-flight run's approved fingerprint survives the upgrade (a
    # non-default override still invalidates the plan, as any patch-size change does).
    _FINGERPRINT_DEFAULT_EXEMPT = ("neutral_top_pins", "neutral_top_band", "verify_held_out_draws")

    def _fingerprinted_patch_sizes(self) -> dict[str, Any]:
        sizes = asdict(self.patch_sizes)
        defaults = asdict(PatchSizes())
        for key in self._FINGERPRINT_DEFAULT_EXEMPT:
            if sizes.get(key) == defaults.get(key):
                sizes.pop(key, None)
        return sizes

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
                # the SERVER's restored flag decides the terminal status: a revert DesktopLUT could
                # not honour (no capture — e.g. it restarted mid-run) is revert_unavailable, the
                # same honest terminal state the in-place flow uses, never "reverted"
                self._restore_user_setup(why="operator chose revert at the apply gate")
                status = _revert_status(self.calib.get("snapshot_restore") or {})
            else:
                self._commit_calibration()
        elif self.calib.get("inplace_baseline") is not None:
            if choice == "revert":
                status = self._revert_inplace()
            # else: the in-place refinement is already applied; nothing to commit. The
            # grayscale-wb touch-up was baked in its stage (Design B, fable Phase 7a) so
            # measure:verify scored the real result; apply keeps it, revert (above →
            # _revert_inplace) re-applies the DLC-owned pre-begin snapshot.
        if status == "completed":
            # Apply path: re-point DesktopLUT at the DURABLE deliverable cube so a cleaned
            # run folder can't break the live calibration (the build artifact lives under the
            # gitignored run dir). No-ops when this flow built no cube (mhc-only).
            self._install_durable_cube(rep.data.get("deliverable_cube"))
            self._record_applied_stack(rep.data.get("deliverable_cube"))
        self.runlog.run_done(status, results_dir=rep.data.get("results_dir"),
                             report_path=rep.data.get("report_path"))
        digest = rep.digest
        if choice == "revert" and self.calib.get("snapshot_restore") is not None and isinstance(digest, dict):
            digest = {**digest, "snapshot_restore": self.calib["snapshot_restore"]}
        return CalibrationResult(
            flow=self.calib.get("flow"), monitor=self.monitor, mode=self.mode, target=self.target_name,
            status=status, stages=list(self.calib["stages"].keys()),
            results_dir=rep.data.get("results_dir"), report_path=rep.data.get("report_path"),
            digest=digest)

    def _flow_full(self) -> CalibrationResult:
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self.stage_enter_neutral()
        self.stage_hardware_readiness()
        self.stage_brightness()
        raw = self.stage_measure(role="raw", patches=self._ramp_patches(extend_full_drive=True),
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
        ver = self.stage_measure(role="verify", patches=self._verify_measure_patches(),
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
        raw = self.stage_measure(role="raw", patches=self._ramp_patches(extend_full_drive=True),
                                 ti3_name="raw.ti3", ndjson_name="raw.ndjson")
        self.stage_build_install_mhc(raw.data["ti3"])
        # The mhc-only flow IS the standalone-ICC path — refine the MHC to D65 so verify scores the
        # foundation as a self-sufficient D65 layer (no 3D LUT to lean on). HDR: base 1D cube; SDR:
        # the correctionGrayscale layer (Task B / #C1).
        if self._spec().is_hdr:
            self.stage_refine_mhc_cube()
        else:
            self.stage_refine_mhc_grayscale()
        ver = self.stage_measure(role="verify", patches=self._verify_measure_patches(),
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
        ver = self.stage_measure(role="verify", patches=self._verify_measure_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_grayscale_wb(self) -> CalibrationResult:
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self._require_stack(need_mhc=True, need_lut=False)
        self._capture_inplace_baseline()
        self.stage_hardware_readiness()
        self.stage_grayscale_wb_touchup()
        ver = self.stage_measure(role="verify", patches=self._grayscale_wb_verify_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_refine_mhc(self) -> CalibrationResult:
        """Re-run ONLY the SDR MHC grayscale refine on a completed run's MHC, keeping its 3D LUT.

        The cheap fix for a foundation whose refine left something the band/judge now corrects
        (the 2026-09-25 PA32UCXR white ~1 dE off D65) without re-measuring the raw ramp or rebuilding
        the cube: seed the source run's derived MHC (``mhc_params`` + the BUILD's base cube) ->
        enter neutral (calibration mode, exactly like full/mhc-only, so revert restores the user's
        setup) -> reinstall that MHC -> re-refine the grayscale (with the SDR white band) -> re-apply
        the source run's cube -> a SHORT verify (the refine's greys + an RGBCMY sanity subset:
        :func:`build_refine_verify_set`) -> the normal verify/apply gate. SDR only: the HDR base-cube
        refine is untouched (``refine-mhc`` aborts cleanly on an HDR run)."""
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self.stage_seed_from_run()
        self.stage_enter_neutral()
        self.stage_hardware_readiness()
        self.stage_install_mhc()
        self.stage_refine_mhc_grayscale()
        self.stage_reapply_3dlut()
        ver = self.stage_measure(role="verify", patches=self._refine_verify_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _refine_verify_patches(self) -> list[tuple[int, int, int]]:
        return build_refine_verify_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                       max_cv=self._patch_max_cv())

    def _installed_lineage_cube(self, src_root: Path) -> dict[str, Any]:
        """``--refine-cube installed``: the 3D LUT INSTALLED now for this display + mode (the stack
        registry's record), kept across the re-refine instead of the source run's build — the routine
        "the MHC drifted, re-centre it under the current cube" case (the cube came from a later
        3dlut-only run). Only over the SAME MHC lineage: the registry's MHC must be the source run's
        own or a refine-mhc seeded from it; anything else is a refusal (``problem``), never a guess.
        ``build_digest`` is the cube's own build digest (its build white), read from its run."""
        try:
            reg = stack_registry.StackRegistry.load(
                stack_registry.registry_path(self.profile, self.ctx.root))
            rec = reg.get(self.display.name, self.mode)
        except Exception as exc:  # noqa: BLE001 - no registry = nothing provably installed
            return {"path": None, "source": "installed", "problem": f"stack registry unreadable ({exc})"}
        if rec is None or not (rec.cube or {}).get("cube_path"):
            return {"path": None, "source": "installed",
                    "problem": "the stack registry records no installed 3D LUT for this display + mode"}
        cube = rec.cube or {}
        mhc_run = str(rec.run_id or "")
        lineage = mhc_run == src_root.name
        if not lineage and mhc_run:
            try:
                mstate = json.loads((src_root.parent / mhc_run / "dlc_state.json").read_text(encoding="utf-8"))
                mcal = mstate.get("calib") or {}
                seeded = str(mcal.get("source_run") or "")
                lineage = mcal.get("flow") == "refine-mhc" and Path(seeded).name == src_root.name
            except (OSError, ValueError):
                lineage = False
        if not lineage:
            return {"path": None, "source": "installed",
                    "problem": (f"the installed MHC (run {mhc_run or '?'}) is not the source run's own or a "
                                f"refine-mhc seeded from {src_root.name} — the installed cube was built over "
                                "another MHC lineage")}
        path = str(cube["cube_path"])
        if not Path(path).exists():
            return {"path": None, "source": "installed", "missing": path}
        build: dict[str, Any] = {}
        cube_run = cube.get("run_id")
        if cube_run:
            try:
                cstate = json.loads((src_root.parent / str(cube_run) / "dlc_state.json").read_text(encoding="utf-8"))
                build = (((cstate.get("calib") or {}).get("stages") or {}).get("build-install-3dlut")
                         or {}).get("digest") or {}
            except (OSError, ValueError):
                build = {}
        return {"path": path, "source": "installed", "cube_run": cube_run, "mhc_run": mhc_run,
                "target_white_nits": _as_float_local(cube.get("target_white_nits")),
                "build_digest": build}

    def _source_run_cube(self, src_root: Path, src_calib: Mapping[str, Any]) -> dict[str, Any]:
        """The 3D LUT the source run left applied: the applied-stack registry's DURABLE deliverable
        when the registry says that run applied it, else the run's own build artifact. ``path`` is
        None when the source run built no cube (an mhc-only run); ``missing`` names a cube the
        source DID build that is gone from disk. ``--refine-cube installed`` keeps the cube installed
        now instead (:meth:`_installed_lineage_cube`)."""
        if self.calib.get("refine_cube") == "installed":
            return self._installed_lineage_cube(src_root)
        run_id = src_root.name
        try:
            reg = stack_registry.StackRegistry.load(
                stack_registry.registry_path(self.profile, self.ctx.root))
            rec = reg.get(self.display.name, self.mode)
            cube = (rec.cube or {}) if rec is not None else {}
            if cube.get("run_id") == run_id and cube.get("cube_path") and Path(cube["cube_path"]).exists():
                return {"path": str(cube["cube_path"]), "source": "stack_registry",
                        "target_white_nits": _as_float_local(cube.get("target_white_nits"))}
        except Exception:  # noqa: BLE001 - the registry is a convenience; the run record decides
            pass
        rec3d = (src_calib.get("stages") or {}).get("build-install-3dlut") or {}
        built = (rec3d.get("data") or {}).get("cube_path") or (rec3d.get("digest") or {}).get("cube_path")
        if built and Path(built).exists():
            return {"path": str(built), "source": "source_run_build"}
        if built:
            return {"path": None, "source": "source_run_build", "missing": str(built)}
        return {"path": None, "source": "none"}

    @staticmethod
    def _source_cube_white(cube: Mapping[str, Any], src_3d: Mapping[str, Any],
                           src_params: Mapping[str, Any], spec: "cp.TargetSpec",
                           src_refine: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """The SDR white the source run's kept 3D LUT was BUILT for. Its build digest records it
        (``target_white_nits``, since 2026-09-27), else the registry's cube entry. A cube built
        before that is ``unrecorded``: depending on the build's code it targeted the nominal white
        or the source's refined white — both are listed as ``candidates`` (never guessed).
        ``margin_rel`` is the source refine's white-luminance noise margin (meter ⊕ drift ⊕ code)."""
        margin = _as_float_local((((src_refine or {}).get("white_band") or {}).get("margin") or {}).get("rel"))
        recorded = _as_float_local(src_3d.get("target_white_nits"))
        source = "source_build"
        if recorded is None:
            recorded, source = _as_float_local(cube.get("target_white_nits")), "stack_registry"
        if recorded is not None and recorded > 0:
            return {"nits": recorded, "provenance": source, "margin_rel": margin,
                    "source_run_target_source": src_3d.get("target_white_source")}
        candidates = {"nominal": float(spec.luminance_nits)}
        refined = _as_float_local((src_params.get("sdr_white") or {}).get("white_nits"))
        if refined is not None and refined > 0:
            candidates["source_refined_white"] = refined
        return {"nits": None, "provenance": "unrecorded", "candidates": candidates,
                "margin_rel": margin,
                "note": "the source cube's build predates target_white_nits recording — it targeted "
                        "one of the candidates (the nominal before 1011c28, the source refine's "
                        "white after)"}

    # The kept cube's white vs the new refine's white is material when it is BOTH visible — a
    # quarter JND of WHITE lightness (CIEDE2000, the SDR report metric; the refine judge's
    # materiality) — AND real: outside the two refines' combined white-luminance noise margin
    # (each the quadrature of meter σ, settled drift and one code at white), so two refines of
    # the same panel that differ only by read noise never raise the seam.
    _CUBE_WHITE_MATERIAL_DE = refine_convergence.MATERIAL_GAIN_JND

    @staticmethod
    def _white_lightness_de(cube_white: float, delivered_white: float) -> float:
        """CIEDE2000 between the delivered white (L*=100) and the cube's white target expressed
        relative to it — the tone-curve disagreement at the top of a cube built for another white."""
        r = max(float(cube_white), 1e-9) / max(float(delivered_white), 1e-9)
        return float(delta_e2000((100.0, 0.0, 0.0), (116.0 * r ** (1.0 / 3.0) - 16.0, 0.0, 0.0)))

    def _kept_cube_white_evidence(self) -> Optional[dict[str, Any]]:
        """refine-mhc: the kept (source) cube's build white vs the white THIS run's refine
        delivered, with a deterministic materiality flag. ``None`` when no cube is kept."""
        seed = (self.calib["stages"].get("seed-from-run") or {}).get("data") or {}
        src = seed.get("source_cube_white")
        if not isinstance(src, dict):
            return None
        refined = self._sdr_refined_white_nits()
        here = _as_float_local(((((self.calib["stages"].get("refine-mhc-grayscale") or {})
                                  .get("digest") or {}).get("white_band") or {}).get("margin") or {})
                               .get("rel"))
        there = _as_float_local(src.get("margin_rel"))
        known = [m for m in (here, there) if m is not None and m >= 0]
        # One side unknown: assume it matches the known side (the same panel + meter chain).
        noise_rel = (math.sqrt(sum(m * m for m in known) * (2.0 / len(known))) if known else 0.0)
        ev: dict[str, Any] = {"source_cube_white_nits": src.get("nits"),
                              "source_cube_white_provenance": src.get("provenance"),
                              "refined_white_nits": refined,
                              "nominal_white_nits": self._spec().luminance_nits,
                              "material_de2000": self._CUBE_WHITE_MATERIAL_DE,
                              "noise_rel": round(noise_rel, 6)}
        if refined is None:
            ev["material"] = None
            ev["note"] = "this run's refine recorded no delivered white — cannot compare"
            return ev
        cands = ({"source_build": src["nits"]} if src.get("nits") is not None
                 else dict(src.get("candidates") or {}))
        des = {k: round(self._white_lightness_de(v, refined), 3) for k, v in cands.items()}
        rels = {k: round(float(v) / refined - 1.0, 5) for k, v in cands.items()}
        ev["white_de2000"] = des
        ev["white_rel_diff"] = rels
        ev["material"] = any(des[k] >= self._CUBE_WHITE_MATERIAL_DE and abs(rels[k]) > noise_rel
                             for k in cands)
        if src.get("nits") is None:
            ev["candidates"] = cands
        return ev

    def stage_seed_from_run(self) -> StageOutcome:
        """Seed THIS run with a completed run's derived SDR MHC (mechanics only; any mismatch is a
        clean refusal): its ``mhc_params`` (measured primaries + native white + native peak + the
        adaptive dark floor), the BUILD's base 1D cube copied into this run (the refine always
        restarts from the build base — never compounds a previous refine), the source's 3D LUT to
        re-apply, and its thermal-alignment evidence as the refine judge's between-rounds floor
        prior (labelled ``source:``). The source run dir is only READ."""
        def run() -> StageOutcome:
            spec = self._spec()

            def refuse(msg: str, **extra: Any) -> None:
                raise CalibrationAborted(StageOutcome(
                    "seed-from-run", "aborted", digest={"message": msg, **extra}))

            if spec.is_hdr:
                refuse("refine-mhc is SDR-only (the HDR base-cube refine is not re-runnable in "
                       "isolation) — use --flow mhc-only or full for HDR")
            src = self.calib.get("source_run")
            if not src:
                refuse("refine-mhc needs --source-run <completed run dir> (the run whose MHC + 3D LUT "
                       "to keep)")
            src_root = Path(src)
            try:
                src_state = json.loads((src_root / "dlc_state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                refuse(f"cannot read the source run's dlc_state.json ({type(exc).__name__}: {exc})",
                       source_run=str(src_root))
            src_calib = src_state.get("calib") or {}
            params = src_state.get("mhc_params") or {}
            stages = src_calib.get("stages") or {}
            problems: list[str] = []
            if str(src_state.get("mode") or "").upper() != self.mode:
                problems.append(f"source mode {src_state.get('mode')!r} != this run's {self.mode}")
            if src_state.get("monitor") is not None and int(src_state["monitor"]) != self.monitor:
                problems.append(f"source monitor {src_state.get('monitor')} != {self.monitor}")
            if src_calib.get("target") and src_calib.get("target") != self.target_name:
                problems.append(f"source target {src_calib.get('target')!r} != {self.target_name!r}")
            for need in ("build-install-mhc", "build-install-3dlut", "verify"):
                if (stages.get(need) or {}).get("status") != "done":
                    problems.append(f"the source run has not completed {need} (still running, "
                                    "aborted, or not a full run)")
            if not (src_calib.get("decisions") or {}).get("verify:accept"):
                problems.append("the source run never passed its verify/apply gate (still live?)")
            # DISPLAY identity, not just the monitor index (indices were remapped 2026-09): the
            # display name + the EDID hardware id this run's preflight read vs the source's.
            here = ((self.calib["stages"].get("preflight") or {}).get("digest") or {})
            there = ((stages.get("preflight") or {}).get("digest") or {})
            here_name = here.get("display") or self.display.name
            there_name = there.get("display")
            try:
                there_name = there_name or json.loads(
                    (src_root / "manifest.json").read_text(encoding="utf-8")).get("display")
            except (OSError, ValueError):
                pass
            if there_name != here_name:
                problems.append(f"source display {there_name!r} != this run's {here_name!r}")
            here_hw = (here.get("monitor_map") or {}).get("hardware_id")
            there_hw = (there.get("monitor_map") or {}).get("hardware_id")
            if here_hw and there_hw and here_hw != there_hw:
                problems.append(f"source panel EDID {there_hw} != the panel now at monitor "
                                f"{self.monitor} ({here_hw}) — a different physical display")
            nw = params.get("measured_white") or {}
            if not (params.get("primaries") and nw.get("x") is not None and nw.get("y") is not None
                    and params.get("target_luminance") and params.get("base_grayscale")):
                problems.append("the source mhc_params lack primaries / measured white / "
                                "target_luminance / base_grayscale")
            base_src = src_root / "generated" / f"mhc_base_{self.mode.lower()}.cube"
            base_note = "build base cube"
            if not base_src.exists():
                alt = ((params.get("base_lut") or {}).get("cube_path"))
                if alt and Path(alt).exists():
                    base_src, base_note = Path(alt), "the source's FINAL (refined) cube — build base missing"
                else:
                    problems.append("no SDR base 1D cube in the source run")
            cube = self._source_run_cube(src_root, src_calib)
            if cube.get("missing"):
                problems.append(f"the source run's 3D LUT is gone from disk: {cube['missing']}")
            if cube.get("problem"):
                problems.append(f"--refine-cube installed: {cube['problem']}")
            if problems:
                refuse("cannot seed refine-mhc from the source run: " + "; ".join(problems),
                       source_run=str(src_root), problems=problems)

            gen = self.ctx.root / "generated"
            gen.mkdir(parents=True, exist_ok=True)
            base_dst = gen / f"mhc_base_{self.mode.lower()}.cube"
            shutil.copy2(base_src, base_dst)
            seeded = json.loads(json.dumps(params))           # deep copy; the source stays untouched
            base_lut = dict(seeded.get("base_lut") or {})
            base_lut["cube_path"] = str(base_dst)
            seeded["base_lut"] = base_lut
            seeded.pop("sdr_white", None)                     # this run's refine decides its own white
            seeded["seeded_from"] = {"run": str(src_root), "base_cube": str(base_src),
                                     "base_note": base_note,
                                     "source_final_cube": (params.get("base_lut") or {}).get("cube_path")}
            self._state["mhc_params"] = seeded
            ta = src_calib.get("thermal_align") or {}
            if ta and not self.calib.get("thermal_align"):
                self.calib["thermal_align"] = {f"source:{k}": v for k, v in ta.items()}
            _common.save_dlc_state(self.ctx, self._state)
            src_refine = (stages.get("refine-mhc-grayscale") or {}).get("digest") or {}
            src_verify = (stages.get("verify") or {}).get("digest") or {}
            src_3d = (stages.get("build-install-3dlut") or {}).get("digest") or {}
            # the kept cube's OWN build digest decides its build white (the installed cube's run,
            # not the source's, under --refine-cube installed)
            cube_3d = cube.get("build_digest") if cube.get("source") == "installed" else src_3d
            cube_white = (self._source_cube_white(cube, cube_3d or {}, params, spec, src_refine)
                          if cube.get("path") else None)
            here_ccmx = (here.get("correction") or {}).get("file")
            there_ccmx = (there.get("correction") or {}).get("file")

            def _norm(f: Any) -> Optional[str]:
                return os.path.normcase(os.path.normpath(str(f))) if f else None

            judge: dict[str, Any] = {}
            if _norm(here_ccmx) != _norm(there_ccmx):
                judge["correction_differs"] = {"source": there_ccmx, "now": here_ccmx}
            if not (here_hw and there_hw):
                judge["identity_unverified"] = {"source_hardware_id": there_hw,
                                                "now_hardware_id": here_hw}
            digest = {"source_run": str(src_root), "source_flow": src_calib.get("flow"),
                      "base_cube": str(base_dst), "base_note": base_note,
                      "cube_path": cube.get("path"), "cube_source": cube.get("source"),
                      **({"kept_installed_cube": {"cube_run": cube.get("cube_run"),
                                                  "installed_mhc_run": cube.get("mhc_run")}}
                         if cube.get("source") == "installed" else {}),
                      "native_peak_nits": params.get("target_luminance"),
                      "measured_white": nw,
                      "source_refine": {k: src_refine.get(k) for k in
                                        ("rounds", "band_avg_de2000", "rowsums", "white_band",
                                         "converged", "floored")},
                      "source_verify": {k: src_verify.get(k) for k in
                                        ("white_de2000", "grayscale_avg_de2000", "avg_de2000",
                                         "max_de2000")} if src_verify else None,
                      "thermal_prior": bool(ta),
                      "identity": {"display": here_name, "hardware_id": here_hw,
                                   "source_hardware_id": there_hw},
                      "correction": {"now": here_ccmx, "source": there_ccmx},
                      "source_lut3d": {k: src_3d.get(k) for k in
                                       ("converged", "best_max_de", "best_mean_de", "metric",
                                        "optimize_metric", "physical_floor", "cube_path")},
                      "source_cube_white": cube_white,
                      "needs_judgment": judge or None}
            if not cube.get("path"):
                digest["note"] = "the source run left no 3D LUT — the MHC is re-refined alone"
            return StageOutcome("seed-from-run", "done", digest=digest,
                                data={"cube_path": cube.get("path"), "base_cube": str(base_dst),
                                      "source_run": str(src_root), "source_cube_white": cube_white})
        outcome = self._stage("seed-from-run", run)
        judge = (outcome.digest or {}).get("needs_judgment") or {}
        if judge:
            # Not provably the same measurement chain: a different colorimeter correction (the
            # re-refine would read the panel through another ccmx than the kept cube was built
            # under), or a panel identity that can't be confirmed. A judgment, recommended abort
            # (non-benign: always pauses, even under --supervised).
            what = []
            if "correction_differs" in judge:
                cd = judge["correction_differs"]
                what.append(f"the active colorimeter correction differs from the source run's "
                            f"({cd.get('now')!r} vs {cd.get('source')!r})")
            if "identity_unverified" in judge:
                what.append("the panel's EDID identity could not be compared (a hardware id is "
                            "missing on one side)")
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="seed-from-run:mismatch", seam=SEAM_STACK, stage="seed-from-run",
                question=("refine-mhc: " + "; ".join(what) + ". Proceed anyway (the re-refined MHC "
                          "and the kept 3D LUT may disagree), or abort?"),
                options=("abort", "proceed_anyway"), recommendation="abort",
                digest=outcome.digest)),
                stage="seed-from-run", message="refine-mhc: aborted at the source-run mismatch seam")
        return outcome

    def stage_install_mhc(self) -> StageOutcome:
        """Reinstall the seeded MHC (no derivation — ``seed-from-run`` supplied ``mhc_params``)
        through the SAME install path as ``build-install-mhc`` (:meth:`_install_mhc_params`), then
        the same immediate bright-neutral sanity read + foundation seam."""
        def run() -> StageOutcome:
            spec = self._spec()
            params = self._state.get("mhc_params") or {}
            if not params.get("primaries"):
                raise CalibrationAborted(StageOutcome(
                    "install-mhc", "aborted",
                    digest={"message": "no seeded mhc_params to install (seed-from-run did not run?)"}))
            applied, verified, white = self._install_mhc_params(params, spec)
            wx, wy = white.xy
            params["white"] = {"x": round(wx, 6), "y": round(wy, 6)}
            params["white_source"] = white.provenance
            self._state["mhc_params"] = params
            _common.save_dlc_state(self.ctx, self._state)
            profile_name = applied.get("profile_name") if isinstance(applied, dict) else None
            verify_ok = bool(verified.get("verified")) if isinstance(verified, dict) else False
            digest = {"primaries": params["primaries"], "white_xy": [wx, wy],
                      "white_provenance": white.provenance,
                      "measured_white": params.get("measured_white"),
                      "base_cube": (params.get("base_lut") or {}).get("cube_path"),
                      "profile_name": profile_name, "verified": verify_ok,
                      "seeded_from": params.get("seeded_from")}
            if params.get("dark_floor"):
                digest["dark_floor"] = params["dark_floor"]
            sanity = self._mhc_foundation_sanity_check()
            if sanity:
                digest["sanity"] = sanity
            return StageOutcome("install-mhc", "done", digest=digest,
                                data={"profile_name": profile_name, "verified": verify_ok})

        outcome = self._stage("install-mhc", run)
        self._foundation_seam(outcome, stage="install-mhc")
        return outcome

    def stage_reapply_3dlut(self) -> StageOutcome:
        """Put the source run's 3D LUT back over the re-refined MHC (``enter-neutral`` cleared the
        calibrated pair's runtime layers). Mechanics only — whether that cube is still valid over
        the new foundation is what the verify that follows measures (and the verify seam judges)."""
        def run() -> StageOutcome:
            seed = (self.calib["stages"].get("seed-from-run") or {}).get("data") or {}
            cube = seed.get("cube_path")
            if not cube:
                return StageOutcome("reapply-3dlut", "done",
                                    digest={"skipped": True, "reason": "the source run left no 3D LUT"},
                                    data={"cube_path": None})
            if not Path(cube).exists():
                raise CalibrationAborted(StageOutcome(
                    "reapply-3dlut", "aborted",
                    digest={"message": f"the source run's 3D LUT vanished: {cube}"}))
            self.controller.set_3dlut(self.monitor, self.mode, str(cube))
            self._hook_routing_evidence_after_install("reapply-3dlut")
            digest: dict[str, Any] = {"cube_path": str(cube)}
            white = self._kept_cube_white_evidence()
            if white is not None:
                digest["cube_white"] = white
            return StageOutcome("reapply-3dlut", "done", digest=digest,
                                data={"cube_path": str(cube)})
        outcome = self._stage("reapply-3dlut", run)
        white = (outcome.digest or {}).get("cube_white") or {}
        if white and white.get("material") is None:
            # A cube is kept but this refine recorded no delivered white: whether the kept cube
            # fits is unknowable from the record — the LLM decides, never a silent keep.
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="reapply-3dlut:white-unknown", seam=SEAM_STACK, stage="reapply-3dlut",
                question=("refine-mhc: the kept 3D LUT's white cannot be compared with this refine's "
                          "(the refine recorded no delivered white). Keep the cube (the verify "
                          "measures it), or abort?"),
                options=("keep_cube", "abort"), recommendation="keep_cube",
                digest=dict(outcome.digest or {}))),
                stage="reapply-3dlut", message="refine-mhc: aborted — kept-cube white unknown")
        elif white.get("material"):
            # The kept cube's tone curve was built for a different white than this refine chose:
            # brighter ⇒ the top clips and the greys lift off the MHC; dimmer ⇒ the top is
            # compressed. Whether to keep it (verify measures it; a 3dlut-only after apply
            # retargets the cube to the new white, carried by the registry) is a judgment.
            src_nits = white.get("source_cube_white_nits")
            src_label = (f"{src_nits:g} nits" if src_nits is not None else
                         "an unrecorded white (candidates "
                         + ", ".join(f"{k} {v:g}" for k, v in (white.get("candidates") or {}).items())
                         + ")")
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="reapply-3dlut:white-mismatch", seam=SEAM_STACK, stage="reapply-3dlut",
                question=(f"refine-mhc: the kept 3D LUT was built for {src_label} but this refine "
                          f"delivers white at {white.get('refined_white_nits'):g} nits "
                          f"(white ΔE2000 {white.get('white_de2000')}, material ≥ "
                          f"{self._CUBE_WHITE_MATERIAL_DE:g} and beyond the ±{white.get('noise_rel'):.2%} "
                          "white noise margin). Keep the cube (the verify measures the "
                          "mismatch; run 3dlut-only after apply to retarget it), or abort?"),
                options=("keep_cube", "abort"), recommendation="keep_cube",
                digest={**(outcome.digest or {}),
                        "seeded_cube_white": ((self.calib["stages"].get("seed-from-run") or {})
                                              .get("digest") or {}).get("source_cube_white")})),
                stage="reapply-3dlut", message="refine-mhc: aborted at the kept-cube white mismatch")
        return outcome

    def _flow_build_correction(self) -> CalibrationResult:
        """Mint (refresh) the colorimeter correction via ccxxmake, standalone — run this
        BEFORE a calibration when the correction is stale/missing (the calibration's meter
        is wired at flow start, so the fresh correction must be recorded first). Persists
        the result to the correction store as the active correction (+ optional white SPD)."""
        self.stage_preflight()
        self.stage_clear_native()      # core clears DesktopLUT to native over the pipe (not the operator's job)
        self.stage_probe_match()       # launches ccxxmake in its own console; ingests the .ccmx on resume
        store = self._correction_store()
        rec = store.get(self.display.name, self.mode)
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
        # Same P12 coherence guard as resolve-target: characterize drives the native panel
        # through this target's TRANSFER (bit depth + signal↔nits map), so a mismatched slot
        # (e.g. an HDR slot pointing at a power-law target) would measure every level at the
        # wrong code↔luminance mapping and bake it into the DIP.
        self._reject_mode_target_mismatch("characterize", target, spec)
        # HDR characterization is routine (like HDR calibration via --mode HDR): it only
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
        display_restored = self._restore_user_setup(
            why="characterization complete — panel learned, nothing applied")
        dip = self._dip()
        # Terminal marker on the spine (this flow doesn't go through _finish) so the dashboard
        # flips to 'done' instead of hanging on "running" after a completed characterization.
        # The characterization completed either way; whether the display came back is its own fact.
        self.runlog.run_done("completed", flow="characterize",
                             dip_store=str(self._dip_store().path), display_restored=display_restored)
        return CalibrationResult(
            flow="characterize", monitor=self.monitor, mode=self.mode, target=self.target_name,
            status="completed", stages=list(self.calib["stages"].keys()),
            results_dir=None, report_path=None,
            digest={"characterize": outcome.digest,
                    "stored_display": dip.display if dip else None,
                    "dip_store": str(self._dip_store().path),
                    "snapshot_restore": self.calib.get("snapshot_restore")})

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

    # ====================================================================
    # verify-only — MEASURE an installed (or candidate) stack; build/commit nothing
    # ====================================================================
    def _flow_verify_only(self) -> CalibrationResult:
        """Score the INSTALLED stack (or a candidate 3D LUT over it) against a verify set — no MHC,
        cube or registry change of its own. The owed hardware acceptances (D1 projection cube,
        re-verifying a stack after a change, owner A/Bs) only need a measurement, not a multi-hour
        ``3dlut-only`` rebuild.

        preflight → [verify-source] → resolve-target (plan seam) → whitepoint → require-stack →
        hardware-readiness (the stack stays installed: no enter-neutral; viewing layers off for the
        run, restored at its end) → [install-candidate] → measure:verify → verify (score + gate +
        report) → finish. ``--verify-patches-from RUN`` re-measures that run's EXACT verify list and
        scores it under its basis (per-bucket deltas vs its recorded verify); ``--verify-cube PATH``
        installs a candidate cube for the run, and ``verify:candidate`` (restore / keep) decides its
        fate. Any other end — abort, cancel, error — puts the prior cube back; a pause keeps the
        measurement state (``--abort`` of a paused run restores it too)."""
        try:
            self.stage_preflight()
            self.stage_verify_source()
            self.stage_verify_patches_file()
            self._adopt_installed_stack_basis()
            self.stage_resolve_target()
            self.stage_whitepoint()
            self._require_stack(need_mhc=True, need_lut=False)
            self.stage_hardware_readiness()
            self.stage_install_candidate()
            ver = self.stage_measure(role="verify", patches=self._verify_only_patches(),
                                     ti3_name="verify.ti3", ndjson_name="verify.ndjson")
            self.stage_verify(ver.data["ti3"])
            return self._finish_verify_only()
        except AdjudicationRequired:
            raise   # a PAUSE: the candidate stays live for the resuming invocation
        except BaseException:
            self._restore_verify_candidate(
                why="the run ended before verify:candidate was decided (abort / cancel / error)")
            raise

    def _adopt_installed_stack_basis(self) -> None:
        """No ``--verify-patches-from`` (HDR): score against the INSTALLED MHC's own measured gamut —
        the primaries its build recorded in the stack registry — so the gamut-aware verify clamps
        exactly as that stack's build verify did, not against a separately characterized DIP. Only
        when the registry record is trusted for this stack (the pipe's profile cross-checks); else
        ``_reachable_primaries`` keeps its DIP fallback. The seeded ``mhc_params`` carry only the
        primaries + ``seeded_from`` and are NEVER installed. Memoised (first write wins)."""
        if (self.content_mode != "HDR" or self._state.get("mhc_params") or self.calib.get("verify_patches_from")
                or self.calib.get("verify_basis_checked")):
            return
        # Decided ONCE per run (before the plan): a registry edited between resumes must not
        # change the gamut the approved plan's verify caps were derived from.
        self.calib["verify_basis_checked"] = True
        self._save()
        evidence = self._installed_stack_evidence() or {}
        if not (evidence.get("pin_nits") or evidence.get("matches") is True):
            return
        try:
            reg = stack_registry.StackRegistry.load(
                stack_registry.registry_path(self.profile, self.ctx.root))
            rec = reg.get(self.display.name, self.mode)
        except Exception:  # noqa: BLE001 - priors, never a gate
            return
        prim = dict(((rec.mhc if rec is not None else None) or {}).get("primaries") or {})
        if not metrics_mod.reachable_primaries_from_mhc_params({"primaries": prim}):
            return
        self._state["mhc_params"] = {
            "primaries": prim,
            "seeded_from": {"registry": str(reg.path), "run_id": rec.run_id,
                            "purpose": "verify-only scoring basis (the installed MHC's measured gamut) "
                                       "— not installed"}}
        self._save()

    def _scoring_gamut_source(self) -> Optional[str]:
        """Where the verify's reachable gamut (the OOG clamp) came from — evidence for the LLM."""
        if self.content_mode != "HDR":
            return None
        seeded = (self._state.get("mhc_params") or {}).get("seeded_from") or {}
        if seeded.get("run"):
            return f"source run {Path(str(seeded['run'])).name} (its MHC build's measured primaries)"
        if seeded.get("registry"):
            return f"installed stack (registry record of run {seeded.get('run_id')})"
        if self._reachable_primaries() is not None:
            return "DIP native primaries (no trusted installed-stack record)"
        return None

    def _verify_source_record(self) -> Optional[dict[str, Any]]:
        """The memoised ``verify-source`` data (the source's patch list, recorded verify, basis),
        or ``None`` without ``--verify-patches-from`` / before the stage ran."""
        rec = (self.calib.get("stages") or {}).get("verify-source") or {}
        if rec.get("status") != "done":
            return None
        return rec.get("data") or None

    def _content_mode_problem(self, flow: str) -> Optional[str]:
        """Mechanical coherence of ``--content-mode`` / ``--keep-layers`` (a refusal, not a judgment):
        only verify-only measures content of another mode than the display's, only SDR-on-HDR exists
        (Windows composites SDR into HDR; the reverse is not a thing), and only verify-only may keep a
        viewing layer on (a calibration must measure the stack it builds without the user's tweaks)."""
        if self.content_mode != self.mode:
            if flow != "verify-only":
                return (f"--content-mode {self.content_mode} on a {self.mode} display belongs to --flow "
                        f"verify-only (this run is {flow})")
            if not self._sdr_in_hdr():
                return (f"--content-mode {self.content_mode} on a {self.mode} display: only SDR content on an "
                        "HDR display exists (Windows composites SDR into HDR)")
        if self.calib.get("keep_layers") and flow != "verify-only":
            return ("--keep-layers belongs to --flow verify-only (a calibration measures without the "
                    "viewing layers)")
        return None

    def _probe_sdr_white_level(self, patch_window: Mapping[str, Any]) -> dict[str, Any]:
        """Preflight, SDR content on an HDR display: Windows' live SDR white level for the target monitor
        (memoised in ``calib['sdr_white_level']``, first read wins) — the white SDR content is composited
        at, the calibrated white this verify is judged against."""
        rec = self.calib.get("sdr_white_level")
        if isinstance(rec, dict) and rec.get("nits"):
            return rec
        if self.sdr_white_probe is None:
            rec = {"nits": None, "source": None,
                   "reason": "no SDR-white-level reader wired (sim / tests): the nominal white stands"}
        else:
            try:
                rec = dict(self.sdr_white_probe(patch_window.get("target_rect")))
            except Exception as exc:  # noqa: BLE001 - evidence; the nominal white stands
                rec = {"nits": None, "source": None, "reason": f"{type(exc).__name__}: {exc}"}
        # What the measured stack is at the start (re-checked at verify): DesktopLUT re-bakes the HDR MHC by
        # itself when the SDR white level changes or a Desktop Gamma whitelist / hotkey swap fires, and
        # verify-only never enters calibration mode — a mid-run re-bake must not pass silently.
        rec["stack_at_preflight"] = self._sdr_in_hdr_stack_facts()
        self.calib["sdr_white_level"] = rec
        self._save()
        self.runlog.note("preflight", f"SDR content on an HDR display: Windows SDR white level "
                                      f"{rec.get('nits')} nit" + (f" ({rec['reason']})" if rec.get("reason") else ""),
                         sdr_white_level={k: rec.get(k) for k in ("nits", "source", "reason")})
        return rec

    def _sdr_in_hdr_stack_facts(self) -> dict[str, Any]:
        """The HDR stack a SDR-in-HDR verify measures, from the pipe: the white Desktop Gamma is baked
        against (``desktop_gamma_sdr_white_nits``; ``None`` before the 2026-10-03 build, which assumed 80),
        the MHC profile and the runtime cube."""
        try:
            st = self.controller.state() or {}
        except Exception as exc:  # noqa: BLE001 - evidence only
            return {"error": f"{type(exc).__name__}: {exc}"}
        key = f"{self.monitor}:{self.mode}"
        lay = (st.get("layers") or {}).get(key) or {}
        # baked = the installed profile's Desktop Gamma stamp (None when it carries no DG; builds after the
        # 2026-10-04 review); the recorded reference level is the fallback on the first SDR-white build
        baked = lay.get("desktop_gamma_baked_sdr_white_nits") if "desktop_gamma_baked_sdr_white_nits" in lay \
            else lay.get("desktop_gamma_sdr_white_nits")
        return {"dg_baked_white_nits": baked,
                "dg_recorded_white_nits": lay.get("desktop_gamma_sdr_white_nits"),
                "desktop_gamma": lay.get("desktop_gamma"),
                "mhc_profile": ((st.get("mhc") or {}).get(key) or {}).get("profile_name"),
                "cube_path": ((st.get("runtime") or {}).get(key) or {}).get("cube_path")}

    def _sdr_in_hdr_evidence(self, samples: Sequence[Any], scored_white: float) -> dict[str, Any]:
        """Verify evidence for SDR content on an HDR display (no verdict): the SDR white Windows declares
        vs the measured one, Desktop Gamma's state for the run, and which tone model the verify greys
        track (``sdr_in_hdr.grey_model_fit``: pure 2.2 / piecewise sRGB / 2.4 / Desktop Gamma's 80-nit
        bake at the declared white)."""
        from . import sdr_in_hdr
        declared = _as_float_local((self.calib.get("sdr_white_level") or {}).get("nits"))
        layers = self.calib.get("viewing_layers") or {}
        before = layers.get("before") or {}
        dg_on = bool(before.get("desktop_gamma")) and "desktop_gamma" not in (layers.get("disabled") or ())
        greys = [(float(s.rgb[0]), float(s.xyz[1])) for s in samples
                 if max(s.rgb) - min(s.rgb) < 1e-6 and math.isfinite(float(s.xyz[1]))]
        fit = sdr_in_hdr.grey_model_fit(greys, white_y=float(scored_white),
                                        declared_white=declared if dg_on else None)
        pre = (self.calib.get("sdr_white_level") or {}).get("stack_at_preflight") or {}
        now = self._sdr_in_hdr_stack_facts()
        changed = [k for k in ("dg_baked_white_nits", "mhc_profile", "cube_path")
                   if "error" not in pre and "error" not in now and pre.get(k) != now.get(k)]
        if changed:
            self.runlog.anomaly(
                "verify", kind="stack_changed_mid_run", changed=changed, at_preflight=pre, at_verify=now,
                message=("the measured HDR stack changed during the run (" + ", ".join(changed) + ") — DesktopLUT "
                         "re-baked or swapped the MHC (SDR white level change, Desktop Gamma whitelist / hotkey); "
                         "the verify reads may mix two stacks"))
        baked = _as_float_local(now.get("dg_baked_white_nits"))
        return {"declared_sdr_white_nits": declared,
                "desktop_gamma_baked_white_nits": baked,
                "stack_stability": {"at_preflight": pre, "at_verify": now, "changed": changed},
                "declared_source": (self.calib.get("sdr_white_level") or {}).get("source"),
                "measured_white_nits": round(float(scored_white), 4),
                "measured_over_declared": (round(float(scored_white) / declared, 4) if declared else None),
                # the white Desktop Gamma is referenced to: the baked one the pipe reports (2026-10-03 builds
                # follow the Windows SDR white level), else the 80 nit every earlier build hard-wired
                "desktop_gamma_on": dg_on,
                "desktop_gamma_reference_white_nits": baked if baked is not None else sdr_in_hdr.DG_WHITE_NITS,
                "desktop_gamma_reference_source": "baked (pipe)" if baked is not None else "legacy 80-nit build",
                "viewing_layers_on": sorted(n for n, v in before.items() if v and n not in (layers.get("disabled") or ())),
                "grey_model_fit": fit}

    def _verify_only_patches(self) -> list[tuple[int, int, int]]:
        """The file's list (``--verify-patches-file``, memoised by its stage), else the source run's
        exact verify list (``--verify-patches-from``), else the standard gamut-aware verify preset
        (the same QC set every flow verifies with)."""
        listed = self._verify_patches_file_record()
        if listed is not None:
            return [tuple(int(c) for c in p) for p in listed.get("codes") or ()]  # type: ignore[misc]
        source = self._verify_source_record()
        if source is not None:
            return [tuple(int(c) for c in p) for p in source.get("patches") or ()]  # type: ignore[misc]
        return self._verify_measure_patches()

    def _adopt_verify_scoring_basis(self, basis: Mapping[str, Any], src: str) -> dict[str, Any]:
        """Score this run's verify exactly as the source run scored its own — the OOG policy +
        level-edge memo, and for HDR the target peak and the reachable gamut its MHC build measured
        (``mhc_params`` → ``_reachable_primaries``). Run-record memos only, first write wins (a
        resume never re-adopts); ``mhc_params`` is tagged ``seeded_from`` and is NEVER installed."""
        adopted: dict[str, Any] = {}
        if self.content_mode == "HDR":
            params = basis.get("mhc_params")
            if isinstance(params, dict) and params and not self._state.get("mhc_params"):
                seeded = json.loads(json.dumps(params))
                seeded["seeded_from"] = {"run": src, "purpose": "verify-only scoring basis (reachable "
                                                             "gamut + plausibility envelope) — not installed"}
                self._state["mhc_params"] = seeded
                adopted["mhc_params"] = "the source run's MHC build (measured primaries / channel peaks)"
            ht = basis.get("hdr_target")
            if isinstance(ht, dict) and ht.get("peak_nits") and not self.calib.get("hdr_target"):
                ht = json.loads(json.dumps(ht))
                prov = dict(ht.get("provenance") or {})
                peak = dict(prov.get("peak") or {})
                peak["source_run_peak_source"] = peak.get("source")
                peak["source"] = "verify_patches_from"
                peak["note"] = (f"the peak run {Path(src).name} scored its verify against — kept so this "
                                "verify is scored like-for-like")
                prov["peak"] = peak
                ht["provenance"] = prov
                self.calib["hdr_target"] = ht
                adopted["hdr_target_peak_nits"] = ht.get("peak_nits")
        if basis.get("oog_mapping") and not self.calib.get("oog_mapping"):
            self.calib["oog_mapping"] = basis["oog_mapping"]
            adopted["oog_mapping"] = basis["oog_mapping"]
        if isinstance(basis.get("oog_level_edge"), dict) and not self.calib.get("oog_level_edge"):
            self.calib["oog_level_edge"] = basis["oog_level_edge"]
            adopted["oog_level_edge"] = bool(basis["oog_level_edge"].get("enabled"))
        self._save()
        # What is IN USE from this source — also when an earlier invocation adopted it (a stage
        # re-run after an abort at the mismatch seam adopts nothing new: first write wins).
        seeded = (self._state.get("mhc_params") or {}).get("seeded_from") or {}
        if seeded.get("run") == src:
            adopted.setdefault("mhc_params", "the source run's MHC build (measured primaries / channel peaks)")
        ht = self.calib.get("hdr_target") or {}
        if ((ht.get("provenance") or {}).get("peak") or {}).get("source") == "verify_patches_from":
            adopted.setdefault("hdr_target_peak_nits", ht.get("peak_nits"))
        if basis.get("oog_mapping") and self.calib.get("oog_mapping") == basis["oog_mapping"]:
            adopted.setdefault("oog_mapping", basis["oog_mapping"])
        if isinstance(basis.get("oog_level_edge"), dict) and self.calib.get("oog_level_edge") == basis["oog_level_edge"]:
            adopted.setdefault("oog_level_edge", bool(basis["oog_level_edge"].get("enabled")))
        return adopted

    def stage_verify_source(self) -> Optional[StageOutcome]:
        """``--verify-patches-from RUN``: load that run's EXACT verify patch list (its
        ``measurements/verify.ndjson``, cross-checked against ``verify.ti3`` and the recorded
        count) + its recorded verify digest, and adopt its scoring basis. Unreadable source = a
        clean refusal; a source that differs from this run is the ``verify-source:mismatch`` seam
        (mode / bit depth: the codes mean another signal — abort only; display / EDID / target /
        correction / installed top: abort recommended, proceed_anyway the judge's call)."""
        src = self.calib.get("verify_patches_from")
        if not src:
            return None
        key = "verify-source"

        def run() -> StageOutcome:
            try:
                source = verify_only.load_source_verify(Path(src))
            except verify_only.SourceRunError as exc:
                raise CalibrationAborted(StageOutcome(
                    key, "aborted", digest={"message": f"--verify-patches-from: {exc}", **exc.detail}))
            here = ((self.calib["stages"].get("preflight") or {}).get("digest") or {})
            stack = self._installed_stack_evidence() or {}
            mism = verify_only.source_mismatches(
                source, mode=self.content_mode, bit_depth=self.bit_depth,
                display=here.get("display") or self.display.name,
                hardware_id=(here.get("monitor_map") or {}).get("hardware_id"),
                target=self.display.target_name(self.content_mode),
                correction_file=(here.get("correction") or {}).get("file"),
                pin_nits=stack.get("pin_nits") if self.content_mode == "HDR" else None,
                display_mode=self.mode)
            if not source.get("verify"):
                mism["soft"].append("the source run's verify was never scored (no recorded numbers): "
                                    "its patch list is reused but there is nothing to compare against")
            basis = source.pop("scoring_basis") or {}
            adopted = {} if mism["hard"] else self._adopt_verify_scoring_basis(basis, str(src))
            src_verify = source.get("verify") or {}
            digest = {"source_run": source["run"], "source_flow": source.get("flow"),
                      "patch_count": source["patch_count"], "patch_source": source["patch_source"],
                      "patch_cross_check": source.get("patch_cross_check"),
                      "patches_fingerprint": source["patches_fingerprint"],
                      "source_patch_max_cv": source.get("patch_max_cv"),
                      "identity": {k: source.get(k) for k in ("display", "hardware_id", "mode", "monitor",
                                                              "bit_depth", "target", "correction_file")},
                      "source_verify": ({k: src_verify.get(k) for k in
                                         ("metric", "avg_de2000", "max_de2000", "white_de2000",
                                          "grayscale_avg_de2000", "within_quality", "gate")}
                                        if src_verify else None),
                      "source_verify_decision": source.get("verify_decision"),
                      "installed_stack": stack or None,
                      "adopted_basis": adopted, "mismatch": mism}
            data = {"run": source["run"], "patches": source["patches"],
                    "patches_fingerprint": source["patches_fingerprint"],
                    "patch_source": source["patch_source"], "verify": source.get("verify"),
                    "patch_metrics_path": source.get("patch_metrics_path"),
                    "basis": {"peak_nits": (basis.get("hdr_target") or {}).get("peak_nits"),
                              "oog_mapping": basis.get("oog_mapping"),
                              "gamut_from_run": bool((basis.get("mhc_params") or {}).get("primaries"))}}
            return StageOutcome(key, "done", digest=digest, data=data)

        outcome = self._stage(key, run)
        mism = (outcome.digest or {}).get("mismatch") or {}
        if mism.get("hard") or mism.get("soft"):
            hard = list(mism.get("hard") or [])
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key=f"{key}:mismatch", seam=SEAM_STACK, stage=key,
                question=("verify-only --verify-patches-from: " + "; ".join(hard + list(mism.get("soft") or []))
                          + (". The recorded codes cannot be re-measured like-for-like here — abort "
                             "(start a run that matches the source)." if hard else
                             ". Proceed anyway (the deltas vs the source are then across that "
                             "difference), or abort?")),
                options=("abort",) if hard else ("abort", "proceed_anyway"),
                recommendation="abort", digest=dict(outcome.digest or {}))),
                stage=key, message="verify-only: refused at the source-run mismatch seam")
        return outcome

    # -- --verify-patches-file (a verify list from a file, e.g. a content-sampled set) ---------------
    def stage_verify_patches_file(self) -> Optional[StageOutcome]:
        """``--verify-patches-file PATH``: load a verify list from a file (e.g. the content-sampled sets
        of ``results/practical_score_2026-10-09``, with a per-patch ``content_weight``) and memoise its
        codes + :func:`verify_only.patches_fingerprint` in the run record, so a resume measures the
        IDENTICAL list whatever happens to the file on disk.

        HARD refusals (mechanical — the codes would mean another signal): unreadable / malformed file,
        content mode or bit depth != the run's, a code outside ``0..max_cv``; (HDR) a code above the
        target-peak cap is refused at resolve-target, before the plan seam (the cap is known there).
        With ``--verify-patches-from`` as well, the source must have measured this exact list (same
        fingerprint) — only then are its deltas like-for-like. The held-out distance rule the fresh draws
        obey (>= 8 codes from every training signal / probe drive, in drive space through the cube) is
        applied to the file's patches against the installed stack's training run and REPORTED — a
        failing patch is measured and scored, never silently dropped. Scoring is unchanged (the run's own
        reachable gamut / OOG basis for HDR, CIEDE2000 for SDR)."""
        path = self.calib.get("verify_patches_file")
        if not path:
            return None
        key = "verify-patches-file"

        def refuse(message: str, **detail: Any) -> CalibrationAborted:
            return CalibrationAborted(StageOutcome(key, "aborted", digest={
                "message": f"--verify-patches-file: {message}", "file": path, **detail}))

        def run() -> StageOutcome:
            try:
                doc = verify_only.load_patches_file(Path(path))
            except verify_only.PatchesFileError as exc:
                raise refuse(str(exc), **exc.detail)
            hard = verify_only.patches_file_problems(doc, content_mode=self.content_mode,
                                                     bit_depth=self.bit_depth)
            if hard:
                raise refuse("; ".join(hard), refused=hard)
            max_cv = (1 << int(self.bit_depth)) - 1
            # ORDER: the file's list order IS the measurement order (a designed sequence must not be
            # silently re-shuffled); only an explicit --verify-patches-order re-sorts it.
            order = str(self.calib.get("verify_patches_order") or "file")
            file_codes = [list(p) for p in doc["codes"]]
            if order == "file":
                measure_codes = file_codes
            else:
                from .engine.patches import sort_patches

                # (before resolve-target: the transfer of the display's configured target for the content)
                transfer = self.profile.transfer_for(self.display.target_name(self.content_mode),
                                                     bit_depth=self.bit_depth)
                measure_codes = [list(p) for p in sort_patches([tuple(p) for p in file_codes], order,
                                                                transfer, warm_tau=self._warm_tau())]
            # READS: the per-patch minimum accepted reads (a duplicate code takes its largest request).
            req: dict[tuple, int] = {}
            for code, r in zip(file_codes, doc.get("reads") or ()):
                if r:
                    req[tuple(code)] = max(req.get(tuple(code), 0), int(r))
            min_reads = [req.get(tuple(p), 0) for p in measure_codes] if req else None
            weights = doc.get("weights")
            weighted = None
            if weights is not None:
                per_key: dict[str, float] = {}
                for code, w in zip(doc["codes"], weights):
                    k = json.dumps(list(metrics_mod.signal_key([c / max_cv for c in code])))
                    per_key[k] = per_key.get(k, 0.0) + float(w)
                weighted = {"weight_sum": round(float(sum(weights)), 6),
                            "n_weighted": sum(1 for w in weights if w > 0),
                            "n_zero_weight": sum(1 for w in weights if w <= 0)}
            source = self._verify_source_record()
            if source is not None and source.get("patches_fingerprint") != verify_only.patches_fingerprint(
                    measure_codes):
                raise refuse(
                    "with --verify-patches-from the source run must have measured this exact list in this order "
                    f"(fingerprint {source.get('patches_fingerprint')} != the {order}-ordered file's "
                    f"{verify_only.patches_fingerprint(measure_codes)}) — the deltas vs its verify would compare "
                    "different patch sets; drop one of the two flags", source_run=source.get("run"))
            try:
                held = self._verify_file_held_out_check(doc["codes"], weights)
            except Exception as exc:  # noqa: BLE001 - evidence must never break the load
                held = {"available": False, "reason": f"check failed ({type(exc).__name__}: {exc})"}
            measured_fp = verify_only.patches_fingerprint(measure_codes)
            reads_rec = ({"n_patches": sum(1 for r in min_reads if r), "max": max(min_reads),
                          "total_min_reads": int(sum(max(1, r) for r in min_reads))}
                         if min_reads else None)
            digest = {"file": doc["path"], "n": doc["n"], "patches_fingerprint": measured_fp,
                      "file_fingerprint": doc["patches_fingerprint"],
                      "measurement_order": ("file (as listed)" if order == "file"
                                            else f"{order} (re-sorted on request: --verify-patches-order)"),
                      "content_mode": doc["content_mode"], "bit_depth": doc["bit_depth"],
                      "content_class": doc["content_class"], "weighted": weighted, "min_reads": reads_rec,
                      "read_rule": self._file_read_rule(min_reads, len(measure_codes)),
                      "coverage_gap_pct_as_drawn": doc.get("coverage_gap_pct") or None,
                      "held_out_check": held}
            data = {"path": doc["path"], "codes": measure_codes, "patches_fingerprint": measured_fp,
                    "file_fingerprint": doc["patches_fingerprint"], "order": order, "min_reads": min_reads,
                    "content_class": doc["content_class"], "coverage_gap_pct": doc.get("coverage_gap_pct") or {},
                    "weights": per_key if weights is not None else None}
            return StageOutcome(key, "done", digest=digest, data=data)

        outcome = self._stage(key, run)
        if getattr(outcome, "replayed", False):
            # A resume measures the MEMOISED list; say so when the file on disk no longer matches it.
            try:
                now = verify_only.load_patches_file(Path(path))["patches_fingerprint"]
            except verify_only.PatchesFileError:
                now = None
            memo = (outcome.data or {}).get("file_fingerprint") or (outcome.data or {}).get("patches_fingerprint")
            if now != memo:
                self.runlog.emit("WARN", key, "verify_patches_file_changed", tier="digest",
                                 memoised_fingerprint=memo, file_fingerprint_now=now,
                                 note="the file changed (or vanished) since this run loaded it — the memoised "
                                      "list is measured, unchanged")
        return outcome

    def _file_planned_reads(self) -> dict[str, Any]:
        """The verify patches file's planned meter reads: Σ of the loop's per-patch read floor
        (:func:`dlc.measure_loop.planned_read_floors` — the file's ``reads`` where given, else the loop's
        own floors) — a LOWER bound (the DIP's SNR escalation, glitch re-reads and drift re-measures add
        to it) — and how many patches / reads sit below 1 nit nominal (Rec.709 luminance weights over
        the content transfer's per-channel nits for SDR, Rec.2020 for PQ — the slow reads). No per-level
        read-time model exists in DLC, so no duration is estimated. Evidence only; never raises."""
        from .measure_loop import planned_read_floors

        listed = self._verify_patches_file_record() or {}
        codes = [tuple(int(c) for c in p) for p in listed.get("codes") or ()]
        try:
            transfer = self._transfer()
            cfg = self.loop_config or self._loop_config_for(self._dip())
            floors = planned_read_floors(codes, transfer, cfg, patch_min_reads=self._file_min_reads(codes))
            k = (0.2627, 0.6780, 0.0593) if transfer.kind == "pq" else (0.2126, 0.7152, 0.0722)
            dark = [i for i, p in enumerate(codes) if sum(w * transfer.cv_to_nits(c) for w, c in zip(k, p)) < 1.0]
        except Exception as exc:  # noqa: BLE001 - evidence only
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {"available": True, "total_reads_min": int(sum(floors)), "n_patches": len(codes),
                "n_below_1_nit": len(dark), "reads_below_1_nit_min": int(sum(floors[i] for i in dark)),
                "max_reads_per_patch": max(floors) if floors else 0,
                "basis": ("lower bound = Σ per-patch read floors (file `reads` where given, else the loop's own "
                          "floors); the DIP's SNR escalation, glitch re-reads and drift re-measures add to it. "
                          "Below 1 nit = nominal Y (Rec.709 weights, Rec.2020 for PQ). No duration: DLC has no "
                          "per-level read-time model")}

    def _file_plan_text(self, listed_digest: Mapping[str, Any], planned: Mapping[str, Any],
                        held: Mapping[str, Any]) -> str:
        """The plan-seam question's description of a ``--verify-patches-file`` plan: the ACTUAL order,
        the planned read total (and its sub-1-nit share), which rule set the read counts, and the
        held-out check's status when it could not run."""
        order = str(listed_digest.get("measurement_order") or "file (as listed)")
        order_txt = ("measured in the file's listed order" if order.startswith("file")
                     else f"measured in {order}")
        parts = [f"{listed_digest.get('n')} patches from --verify-patches-file, {order_txt}"]
        if planned.get("available"):
            parts.append(f">= {planned['total_reads_min']} meter reads planned ({planned['n_below_1_nit']} "
                         f"patches / >= {planned['reads_below_1_nit_min']} reads below 1 nit)")
        else:
            parts.append(f"planned read total unavailable ({planned.get('reason')})")
        rule = (listed_digest.get("read_rule") or {}).get("summary")
        if rule:
            parts.append(f"read counts: {rule}")
        if held and not held.get("available"):
            why = str(held.get("reason") or "no reason recorded")
            parts.append("held-out check " + (why if why.startswith("unavailable") else f"unavailable: {why}"))
        return "; ".join(parts) + ". "

    def _file_read_rule(self, min_reads: Optional[Sequence[int]], n: int) -> dict[str, Any]:
        """Which rule sets each file patch's read count: a patch whose file entry gives ``reads`` is
        read exactly that many times as a minimum (binding in every stop path) — it REPLACES
        ``dark_min_reads`` for that patch (sequence study 2026-10-09: per-read noise is ~flat down to
        0.005 nit, so extra dark reads buy ~nothing); a patch without ``reads`` keeps the loop's own
        policy (``dark_min_reads`` on dark near-neutrals). The global / bright-neutral floors and the
        DIP's SNR escalation still apply to both; a glitch the loop rejects is re-read."""
        try:   # the config the verify measure will actually run with
            dark = int((self.loop_config or self._loop_config_for(self._dip())).dark_min_reads)
        except Exception:  # noqa: BLE001 - evidence only
            dark = int(self.dark_min_reads if self.dark_min_reads is not None else MeasureLoopConfig().dark_min_reads)
        with_reads = sum(1 for r in (min_reads or ()) if r)
        if with_reads == n and n:
            summary = f"file `reads` sets the read count of all {n} patches (replaces dark_min_reads {dark})"
        elif with_reads:
            summary = (f"file `reads` sets the read count of {with_reads} of {n} patches (replaces "
                       f"dark_min_reads {dark} for them); the other {n - with_reads} keep the loop policy "
                       f"(dark_min_reads {dark} on dark near-neutrals)")
        else:
            summary = f"loop policy for all {n} patches (no file `reads`; dark_min_reads {dark} on dark near-neutrals)"
        return {"summary": summary, "n_file_reads": with_reads, "n_loop_policy": n - with_reads,
                "dark_min_reads": dark,
                "rule": ("a file `reads` value is the patch's read count: a minimum binding in every stop path "
                         "(dark early stop, convergence, drift re-measure) that REPLACES dark_min_reads for the "
                         "patch; the global / bright-neutral floors and the DIP's SNR escalation still apply, "
                         "and a rejected glitch is re-read")}

    def _verify_patches_file_record(self) -> Optional[dict[str, Any]]:
        """The memoised ``verify-patches-file`` data (codes, fingerprint, weights), or ``None``."""
        if not self.calib.get("verify_patches_file"):
            return None
        rec = (self.calib.get("stages") or {}).get("verify-patches-file") or {}
        if rec.get("status") != "done":
            return None
        return rec.get("data") or None

    def _refuse_verify_file_above_cap(self) -> None:
        """HDR ``--verify-patches-file``: refuse codes above the run's patch cap (the target peak) — a
        clipped highlight is not a verify read. Called once the target peak is resolved."""
        listed = self._verify_patches_file_record()
        cap = self._patch_max_cv()
        if listed is None or cap is None:
            return
        hard = verify_only.patches_file_problems(
            {"content_mode": self.content_mode, "bit_depth": self.bit_depth, "codes": listed.get("codes")},
            content_mode=self.content_mode, bit_depth=self.bit_depth, patch_max_cv=cap)
        if hard:
            raise CalibrationAborted(StageOutcome("verify-patches-file", "aborted", digest={
                "message": "--verify-patches-file: " + "; ".join(hard), "refused": hard,
                "patch_max_cv": cap, "file": listed.get("path")}))

    def _verify_file_training_run(self) -> tuple[Optional[Path], Optional[str]]:
        """The run that TRAINED what this verify-only measures: the ``--verify-patches-from`` source,
        else the installed stack's registry record (its run folder beside this one / under runs/)."""
        source = self._verify_source_record()
        if source is not None and source.get("run"):
            return Path(str(source["run"])), None
        try:
            reg = stack_registry.StackRegistry.load(stack_registry.registry_path(self.profile, self.ctx.root))
            rec = reg.get(self.display.name, self.mode)
        except Exception as exc:  # noqa: BLE001 - evidence only
            return None, f"stack registry unreadable ({type(exc).__name__})"
        if rec is None or not rec.run_id:
            return None, "no stack-registry record of the installed stack (its training run is unknown)"
        for root in (self.ctx.root.parent / rec.run_id, runs_dir() / rec.run_id):
            if (root / "dlc_state.json").is_file():
                return root, None
        return None, f"the installed stack's build run {rec.run_id} is not on disk here"

    def _verify_file_held_out_check(self, codes: Sequence[Sequence[int]],
                                    weights: Optional[Sequence[float]]) -> dict[str, Any]:
        """The fresh draws' held-out distance rule (:data:`verify_holdout.DRAW_MIN_CODES` codes from every
        training signal / probe drive, signal space and — through the trained cube — drive space)
        applied to the file's patches. Evidence: the failing patches are LISTED, never dropped.

        UNAVAILABLE when the content mode differs from the display mode (SDR content on an HDR display):
        the installed stack's training signals are the DISPLAY mode's (PQ codes), so distances to the
        file's content-mode codes (8-bit SDR gamma) would be in a different code space."""
        if self.content_mode != self.mode:
            return {"available": False, "reason": (f"unavailable (content mode {self.content_mode} ≠ display "
                                                   f"mode {self.mode}): the installed stack was trained on "
                                                   f"{self.mode} signals, the file lists {self.content_mode} "
                                                   "codes — no common code space to measure distance in")}
        root, why = self._verify_file_training_run()
        if root is None:
            return {"available": False, "reason": why}
        max_cv = (1 << int(self.bit_depth)) - 1
        try:
            calib = (json.loads((root / "dlc_state.json").read_text(encoding="utf-8")) or {}).get("calib") or {}
        except (OSError, ValueError) as exc:
            return {"available": False, "reason": f"training run {root.name} unreadable ({type(exc).__name__})"}
        training = verify_holdout.training_context(root, calib, max_cv=max_cv)
        if not training.get("available"):
            return {"available": False, "reason": f"training run {root.name}: {training.get('reason')}"}
        rows = verify_holdout.classify_signals(
            [[c / max_cv for c in p] for p in codes], max_cv=max_cv, training=training["training_signals"],
            probe_drives=training["probe_drives"], cube=training["cube"], lattice_size=training["lattice_size"])
        min_codes = verify_holdout.DRAW_MIN_CODES
        fails = []
        for i, r in enumerate(rows):
            ds = [d for d in (r.get("d_in"), r.get("d_drive")) if d is not None]
            if ds and min(ds) < min_codes:
                fails.append({"index": i, "code": r["code"], "d_in": r.get("d_in"), "d_drive": r.get("d_drive"),
                              "content_weight": (round(float(weights[i]), 6) if weights is not None else None)})
        total_w = float(sum(weights)) if weights is not None else 0.0
        return {"available": True, "training_run": root.name, "min_codes": min_codes,
                "rule": (f">= {min_codes:g} codes from every training signal / probe drive (signal space"
                         + (", + drive space through the trained cube)" if training["cube"] is not None else ")")),
                "n_checked": len(rows), "n_fail": len(fails),
                "fail_weight_share": (round(sum(f["content_weight"] or 0.0 for f in fails) / total_w, 4)
                                      if weights is not None and total_w > 0 else None),
                "failing": fails[:40], "failing_truncated": len(fails) > 40,
                "note": "reported, NOT dropped — these patches are measured and scored; their ΔE is partly in-sample"}

    # -- content-weighted practical score inputs (evidence only) ----------------------------------
    def _content_distribution_specs(self) -> list[str]:
        """``--content-distribution`` (memoised), else the profile's ``content_distribution`` for the
        content mode."""
        specs = self.calib.get("content_distribution")
        if specs:
            return [str(s) for s in specs]
        try:
            return list(self.profile.content_distribution_for(self.content_mode))
        except AttributeError:
            return []

    def _content_distributions(self) -> tuple[list[Any], list[dict[str, Any]]]:
        """The loaded content distributions (cached per process) + per-spec load errors."""
        from . import content_score

        cache = getattr(self, "_content_cache", None)
        if cache is None:
            cache = self._content_cache = {}
        out, errors = [], []
        for spec in self._content_distribution_specs():
            if spec not in cache:
                try:
                    cache[spec] = content_score.load_content_distribution(spec, content_mode=self.content_mode)
                except Exception as exc:  # noqa: BLE001 - evidence only
                    cache[spec] = {"spec": spec, "error": f"{type(exc).__name__}: {exc}"}
            (errors if isinstance(cache[spec], dict) else out).append(cache[spec])
        return out, errors

    def _verify_content_weights(self) -> Optional[metrics_mod.ContentWeights]:
        listed = self._verify_patches_file_record() if self.calib.get("flow") == "verify-only" else None
        if listed is None or not listed.get("weights"):
            return None
        weights = {tuple(json.loads(k)): float(v) for k, v in listed["weights"].items()}
        return metrics_mod.ContentWeights(weights=weights, label=str(listed.get("content_class") or "file"),
                                          source=listed.get("path"),
                                          coverage_gap_pct=dict(listed.get("coverage_gap_pct") or {}))

    def _verify_read_evidence(self, verify_ti3: str, metrics: Sequence[Any]) -> metrics_mod.ReadEvidence:
        """How much each verify signal's ΔE can be trusted: the meter reads its scored value rests on
        (the measure NDJSON — each patch's FINAL adopted round, only the reads the loop kept:
        :func:`dlc.content_score.final_round_reads`), the loop's own SE (its round records + the noise
        sidecar), the dark-level noise trust flags (the noise sidecar) and the meter floor (the DIP's
        ``noise_floor_nits``, else the documented fallback)."""
        from . import content_score

        ti3 = Path(verify_ti3)
        ndjson = ti3.with_suffix(".ndjson")
        max_cv = self._transfer().max_cv
        final = content_score.final_round_reads(ndjson, max_cv)
        reps = [m for m, _n in metrics_mod.group_per_signal(list(metrics))]
        low = content_score.low_snr_signal_keys(ti3, reps)
        # the loop's own ΔE2000 SE of the mean — same metric family only for an SDR (CIEDE2000) verify
        is_hdr = self._spec().is_hdr
        loop_se = content_score.sidecar_se_de(ti3, reps) if not is_hdr else {}
        kw: dict[str, Any] = {"reads": final.counts or None, "low_snr": frozenset(low),
                              "read_xyz": dict(final.reads) or None, "loop_se_de": loop_se or None,
                              "loop_round_se": (dict(final.loop_se) or None) if not is_hdr else None,
                              "reads_basis": final.describe()}
        meter = self._meter_floor(self._dip())
        kw.update(noise_floor_nits=meter.nits, noise_floor_source=meter.source)
        return metrics_mod.ReadEvidence(**kw)

    @staticmethod
    def _meter_floor(dip: Any) -> metrics_mod.MeterFloor:
        """THE meter floor (:func:`dlc.metrics.resolve_meter_floor`): the DIP's ``noise_floor_nits`` when > 0,
        else the documented fallback. One value for the read evidence AND the black-aware score (its
        ``below_meter_floor`` flag and the raw floor fit's grey flags)."""
        made = getattr(dip, "made", None) if dip is not None else None
        return metrics_mod.resolve_meter_floor(
            getattr(dip, "noise_floor_nits", None) if dip is not None else None,
            where=f"DIP noise_floor_nits{f' (made {made})' if made else ''}")

    def _score_black_floor(self) -> Any:
        """The display floor of the content-weighted score's BLACK-AWARE variant (:mod:`dlc.black_aware`, HDR,
        evidence only): the additive pedestal the score allows as a panel limit. The order:

        1. ``--score-black-floor-nits`` (memoised);
        2. else the NATIVE near-black floor fitted from a raw stage (:func:`dlc.black_aware.fit_native_floor`,
           the intercept over the lowest lit greys; the meter floor = the read evidence's, :meth:`_meter_floor`):
           this run's own ``measure:raw`` when it measured one, then an identity-matched recorded run's
           (:meth:`_raw_floor_fits`);
        3. else the DIP's ``native_black_nits`` (characterize's full-field black read);
        4. else unavailable.

        The pedestal colour: the used raw stage's native white, else this run's own raw white, else the DIP's
        ``native_white_xy``, else D65 (stated as assumed). NOT the DIP ``noise_floor_nits`` as a floor (the
        meter's trust floor), and never the verify's own reads (circular). The BT.2390 variants' source white is
        the run's resolved target peak."""
        from . import black_aware

        explicit = self.calib.get("score_black_floor_nits")
        dip = self._dip()
        recorded = getattr(dip, "native_black_nits", None) if dip is not None else None
        made = getattr(dip, "made", None) if dip is not None else None
        when = f", made {made}" if made else ""
        own = (self.calib.get("stages") or {}).get("measure:raw") or {}
        pedestal = [(black_aware.xy_from_xyz((own.get("data") or {}).get("white_xyz")),
                     "this run's native white (measure:raw white_xyz)"),
                    (getattr(dip, "native_white_xy", None) if dip is not None else None,
                     f"DIP native_white_xy{when}")]
        return black_aware.resolve_black_floor(
            explicit=explicit,
            explicit_source="explicit option (--score-black-floor-nits)",
            raw=self._raw_floor_fits(dip) if explicit is None else (),
            recorded=recorded,
            recorded_source=f"DIP native_black_nits (characterize's full-field black read{when})",
            peak_nits=self._hdr_target().peak_nits, pedestal=pedestal)

    def _floor_identity(self) -> dict[str, Any]:
        """This run's identity for matching a recorded raw floor (:func:`dlc.black_aware.run_identity`): the
        preflight's display / EDID hardware id / correction, the display name and mode as configured."""
        from . import black_aware

        ident = black_aware.run_identity(self._state, self.ctx.root)
        ident["display"] = ident.get("display") or self.display.name
        ident["mode"] = str(self.mode).upper()
        return ident

    def _raw_floor_fits(self, dip: Any = None) -> list[Any]:
        """The raw-stage native floor candidates, in order (:mod:`dlc.black_aware`, evidence only):

        1. this run's own ``measure:raw`` stage, when it measured one (``full`` / ``mhc-only``);
        2. a flow that KEEPS the installed MHC (``3dlut-only`` / ``verify-only`` / ...): the installed stack's
           TRAINING run (the ``--verify-patches-from`` source, else the stack registry's record,
           :meth:`_verify_file_training_run`), then the registry's applying run (the run that built the
           installed MHC and measured its raw ramp) when it differs, e.g. under a ``3dlut-only`` training run;
        3. a flow that BUILT ITS OWN MHC (``full`` / ``mhc-only``): its own raw is this stack's. If it is refused,
           the registry still names the PREVIOUSLY applied stack (the registry records this run only at apply),
           so that run is tried under exactly that label, never as "the installed MHC's applying run".

        Every recorded run must match this run's display / EDID hardware id / mode / correction
        (:meth:`_floor_identity`); a mismatch is a listed refusal. The list stops at the first usable fit. A raw
        stage measures the NATIVE panel (identity MHC, no cube) separately from the verify, so its near-black
        greys are a non-circular floor source. The meter floor is :meth:`_meter_floor`, the read evidence's own.
        Never raises: each refusal says why."""
        from . import black_aware

        kw = {"meter_floor": self._meter_floor(dip)}
        fits: list[Any] = []
        own = (self.calib.get("stages") or {}).get("measure:raw") or {}
        if own:
            data = own.get("data") or {}
            ti3 = data.get("ti3")
            if own.get("status") == "done" and ti3 and Path(str(ti3)).is_file():
                fits.append(black_aware.raw_floor_from_ti3(Path(str(ti3)), run_name=self.ctx.root.name,
                                                           role="this run's raw stage",
                                                           white_xyz=data.get("white_xyz"), **kw))
            else:
                fits.append(black_aware.RawFloorFit(None, f"raw run {self.ctx.root.name} (this run's raw stage)",
                                                    f"this run's raw stage is not usable (status "
                                                    f"{own.get('status')!r}, ti3 {ti3!r})"))
            if fits[-1].available:
                return fits
        flow = self.calib.get("flow")
        keeps_mhc = flow in self._FLOWS_KEEPING_MHC
        candidates: list[tuple[Path, str]] = []
        why: Optional[str] = None
        if keeps_mhc:
            try:
                root, why = self._verify_file_training_run()
            except Exception as exc:  # noqa: BLE001 - evidence only
                root, why = None, f"training run lookup failed ({type(exc).__name__})"
            if root:
                candidates.append((root, "the installed stack's training run"))
        try:
            rec = stack_registry.StackRegistry.load(
                stack_registry.registry_path(self.profile, self.ctx.root)).get(self.display.name, self.mode)
        except Exception:  # noqa: BLE001 - evidence only
            rec = None
        if rec is not None and rec.run_id:
            role = ("the installed MHC's applying run" if keeps_mhc
                    else f"a previously applied stack's run, NOT this run's stack (this {flow} run built its own MHC)")
            for cand in (self.ctx.root.parent / rec.run_id, runs_dir() / rec.run_id):
                if (cand / "dlc_state.json").is_file():
                    candidates.append((cand, role))
                    break
        elif not keeps_mhc:
            why = "no stack-registry record of a previously applied stack"
        if not candidates:
            label = "the installed stack's training run" if keeps_mhc else "a previously applied stack's run"
            fits.append(black_aware.RawFloorFit(None, label, f"{label}: {why or 'not on disk here'}"))
        expect = self._floor_identity()
        seen = {self.ctx.root.resolve()}
        for cand, role in candidates:
            key = Path(cand).resolve()
            if key in seen:
                continue
            seen.add(key)
            fits.append(black_aware.raw_floor_from_run(cand, role=role, mode=self.mode, expect=expect, **kw))
            if fits[-1].available:
                break
        return fits

    def _content_practical_kwargs(self, verify_ti3: str, metrics: Sequence[Any]) -> tuple[dict[str, Any], list]:
        """The ``practical_summary`` content kwargs for this verify (empty without weights / a
        distribution) + distribution load errors. Never raises (evidence only)."""
        kw: dict[str, Any] = {}
        errors: list = []
        try:
            weights = self._verify_content_weights()
            contents, errors = self._content_distributions()
            if weights is None and not contents:
                return {}, errors
            wx, wy = self._white_xy()
            kw = {"content_weights": weights, "content": contents or None, "white_xy": (wx, wy),
                  "read_evidence": self._verify_read_evidence(verify_ti3, metrics)}
            if self._spec().is_hdr:
                try:
                    kw["black_floor"] = self._score_black_floor()
                except Exception as exc:  # noqa: BLE001 - the black-aware variant must never cost the block
                    from .black_aware import BlackFloor

                    kw["black_floor"] = BlackFloor(None, f"unavailable: floor resolution failed "
                                                         f"({type(exc).__name__}: {exc})")
        except Exception as exc:  # noqa: BLE001 - evidence must never break the verify gate
            errors = list(errors) + [{"error": f"content inputs failed ({type(exc).__name__}: {exc})"}]
        return kw, errors

    def stage_install_candidate(self) -> Optional[StageOutcome]:
        """``--verify-cube PATH``: install a candidate 3D LUT on the calibrated slot for this run.
        The prior runtime cube is captured (and persisted) BEFORE the install so every exit path can
        put it back; ``verify:candidate`` at the end decides restore / keep. A prior whose FILE is
        gone (DesktopLUT keeps a dead path after a run folder is cleaned) could never be re-applied —
        the ``install-candidate:prior-missing`` seam decides that before anything is installed. A
        memoised replay re-asserts the candidate if the slot lost it (DesktopLUT restarted during a
        pause)."""
        path = self.calib.get("verify_cube")
        if not path:
            return None
        key = "install-candidate"

        def run() -> StageOutcome:
            facts = verify_only.candidate_cube_facts(Path(path))
            if not facts.get("ok"):
                raise CalibrationAborted(StageOutcome(
                    key, "aborted",
                    digest={"message": f"--verify-cube {path}: {facts.get('error')} — not installable "
                                       "as a verify candidate", "candidate": facts}))
            rec = self.calib.get("verify_candidate") or {}
            if not rec.get("prior_captured"):
                try:
                    state = self.controller.state() or {}
                except Exception as exc:  # noqa: BLE001 - no prior known = no safe restore: refuse
                    raise CalibrationAborted(StageOutcome(
                        key, "aborted",
                        digest={"message": "cannot read the live runtime cube before installing the "
                                           f"candidate ({type(exc).__name__}: {exc}) — refusing: the "
                                           "prior cube could not be restored afterwards"}))
                prior = ((state.get("runtime") or {}).get(f"{self.monitor}:{self.mode}") or {}).get("cube_path")
                rec = {"cube": str(path), "monitor": self.monitor, "mode": self.mode,
                       "prior_captured": True, "prior_cube": prior,
                       "prior_exists": (Path(str(prior)).exists() if prior else None),
                       "installed": False, "restored": False, "kept": False, "facts": facts}
                self.calib["verify_candidate"] = rec
                self._save()     # persisted BEFORE the mutation: an abort mid-install knows the prior
            if rec.get("prior_cube") and rec.get("prior_exists") is False and not rec.get("restore_clears"):
                self._abort_if(self.adjudicate(AdjudicationRequest(
                    key=f"{key}:prior-missing", seam=SEAM_STACK, stage=key,
                    question=(f"the runtime 3D LUT DesktopLUT holds for {self.monitor}:{self.mode} points at "
                              f"a file that no longer exists ({rec.get('prior_cube')}) — it cannot be put "
                              "back after the candidate. clear_on_restore = install the candidate and CLEAR "
                              "the slot at restore / abort (DesktopLUT cannot load the missing file "
                              "anyway); abort = stop now (nothing installed) and fix the installed cube."),
                    options=("abort", "clear_on_restore"), recommendation="abort",
                    digest={"prior_cube": rec.get("prior_cube"), "candidate": facts})),
                    stage=key, message="verify-only: aborted — the prior runtime cube file is missing")
                rec["restore_clears"] = True
                self.calib["verify_candidate"] = rec
                self._save()
            self.controller.set_3dlut(self.monitor, self.mode, str(path))
            rec.update(installed=True, restored=False,
                       installed_at=datetime.now().isoformat(timespec="seconds"))
            self.calib["verify_candidate"] = rec
            self._save()
            self._hook_routing_evidence_after_install(key)
            prior = rec.get("prior_cube")
            same = bool(prior) and os.path.normcase(os.path.abspath(str(prior))) == \
                os.path.normcase(os.path.abspath(str(path)))
            return StageOutcome(key, "done",
                                digest={"candidate": facts, "prior_cube": prior,
                                        "prior_exists": rec.get("prior_exists"),
                                        "restore_clears": bool(rec.get("restore_clears")),
                                        "candidate_is_prior": same},
                                data={"cube_path": str(path), "prior_cube": prior})

        outcome = self._stage(key, run)
        if outcome.replayed:
            self._ensure_candidate_live(key)
        return outcome

    def _redecide_resolved_candidate(self, cand: dict[str, Any]) -> dict[str, Any]:
        """A resumed, already-resolved run given ``--decide verify:candidate=<the other choice>``
        re-decides the terminal gate (as ``verify:accept`` apply↔revert does): keep → restore puts the
        prior back; restore → keep re-installs the candidate. The seam then records the override."""
        override = self.decision_overrides.get("verify:candidate")
        if override is None or self.force or not cand.get("installed"):
            return cand
        if cand.get("kept") and override.choice == "restore":
            cand["kept"] = False
        elif cand.get("restored") and override.choice == "keep" and not cand.get("aborted"):
            self.controller.set_3dlut(self.monitor, self.mode, str(cand.get("cube")))
            cand.update(restored=False, reinstalled_at=datetime.now().isoformat(timespec="seconds"))
            self._hook_routing_evidence_after_install("verify")
        else:
            return cand
        self.calib["verify_candidate"] = cand
        self._save()
        self.runlog.note("verify", f"verify:candidate re-decided on resume -> {override.choice}",
                         verify_candidate=cand)
        return cand

    def _ensure_candidate_live(self, key: str) -> None:
        """Resume guard: the measurement must read THROUGH the candidate. Re-install it when the slot
        no longer holds it (a DesktopLUT restart during a pause, or a resumed aborted run whose
        teardown restored the prior) — never once verify:candidate has decided."""
        rec = self.calib.get("verify_candidate") or {}
        if not rec.get("cube") or rec.get("kept") or (self.calib.get("decisions") or {}).get("verify:candidate"):
            return
        if rec.get("aborted"):
            # `--abort` ended this run for the operator: never re-install its candidate behind them.
            raise CalibrationAborted(StageOutcome(
                key, "aborted",
                digest={"message": "this verify-only run was ended with --abort (the prior cube was put "
                                   "back) — start a NEW verify-only run instead of resuming it",
                        "verify_candidate": rec}))
        try:
            live = (((self.controller.state() or {}).get("runtime") or {}).get(
                f"{self.monitor}:{self.mode}") or {}).get("cube_path")
        except Exception:  # noqa: BLE001 - unknown live state: re-assert (idempotent install)
            live = None
        if live == rec["cube"] and rec.get("installed") and not rec.get("restored"):
            return
        self.controller.set_3dlut(self.monitor, self.mode, str(rec["cube"]))
        rec.update(installed=True, restored=False,
                   reinstalled_at=datetime.now().isoformat(timespec="seconds"))
        self.calib["verify_candidate"] = rec
        self._save()
        self.runlog.anomaly(key, kind="verify_candidate",
                            message=f"the candidate cube was not live on resume (slot held {live!r}) — "
                                    "re-installed before measuring")
        self._hook_routing_evidence_after_install(key)

    def _restore_verify_candidate(self, *, why: str) -> Optional[dict[str, Any]]:
        """Put the prior runtime cube back after a candidate install (no-op without one). Evidence on
        the spine; a failed restore is an anomaly naming the cube to re-apply by hand."""
        before = dict(self.calib.get("verify_candidate") or {})
        rec = verify_only.restore_candidate(self.controller, self.calib, why=why, log=self.ctx.log)
        if not rec or rec == before:
            return rec
        try:
            self._save()
        except Exception:  # noqa: BLE001 - teardown bookkeeping must not mask the original exit
            pass
        if rec.get("restored"):
            self.runlog.note("verify", f"candidate cube restored: prior {rec.get('prior_cube') or 'none'} back "
                                       f"on the slot ({why})", verify_candidate=rec)
            self._hook_routing_evidence_after_install("verify", action="restore")
        else:
            self.runlog.anomaly("verify", kind="verify_candidate", verify_candidate=rec,
                                message=f"candidate cube NOT restored ({rec.get('restore_error')}) — "
                                        f"re-apply {rec.get('prior_cube') or 'no cube (clear the slot)'} "
                                        "in DesktopLUT by hand")
        return rec

    def _keep_verify_candidate(self) -> dict[str, Any]:
        """``verify:candidate=keep``: the candidate stays live and is recorded in the applied-stack
        registry as this display+mode's cube — exactly what a ``3dlut-only`` apply records
        (``record_cube``); the MHC record is untouched."""
        rec = self.calib.get("verify_candidate") or {}
        rec["kept"] = True
        rec["kept_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            reg = stack_registry.StackRegistry.load(
                stack_registry.registry_path(self.profile, self.ctx.root))
            try:
                pipe = self.controller.state() or {}
                pipe_profile = ((pipe.get("mhc") or {}).get(f"{self.monitor}:{self.mode}") or {}).get("profile_name")
            except Exception:  # noqa: BLE001
                pipe_profile = None
            entry = reg.record_cube(display=self.display.name, mode=self.mode, monitor=self.monitor,
                                    run_id=self.ctx.root.name, cube_path=rec.get("cube"),
                                    profile_name=pipe_profile)
            rec["registry"] = {"key": entry.key, "path": str(reg.path)}
            self.ctx.log(f"applied-stack registry: {entry.key} cube <- verify-only candidate {rec.get('cube')}")
        except Exception as exc:  # noqa: BLE001 - the cube stays live either way; say the record failed
            rec["registry_error"] = f"{type(exc).__name__}: {exc}"
            stack_run = (self.calib.get("installed_stack") or {}).get("run_id") or "<the installed MHC's run>"
            self.runlog.anomaly("verify", kind="verify_candidate",
                                message=f"candidate kept live but the stack registry was NOT updated "
                                        f"({rec['registry_error']}) — backfill with `python -m "
                                        f"dlc.stack_registry import-run --run runs/{stack_run} --cube "
                                        f"{rec.get('cube')}`")
        self.calib["verify_candidate"] = rec
        self._save()
        return rec

    def _verify_only_evidence(self, digest: Mapping[str, Any], metrics: Sequence[Any]) -> dict[str, Any]:
        """The verify-only additions to the verify digest: WHAT was measured (the live runtime cube /
        MHC profile, the candidate, the registry's view of the installed stack) and, with
        ``--verify-patches-from``, ``vs_source`` — per-bucket deltas + per-patch movers vs the source
        run's RECORDED verify, with every scoring-basis difference listed."""
        ck = f"{self.monitor}:{self.mode}"
        try:
            state = self.controller.state() or {}
        except Exception as exc:  # noqa: BLE001 - evidence only
            state = {"error": f"{type(exc).__name__}: {exc}"}
        cand = self.calib.get("verify_candidate") or {}
        measured = {"runtime_cube": ((state.get("runtime") or {}).get(ck) or {}).get("cube_path"),
                    "mhc_profile": ((state.get("mhc") or {}).get(ck) or {}).get("profile_name"),
                    "candidate": cand.get("cube") if cand.get("installed") else None,
                    "installed_stack": self.calib.get("installed_stack")}
        out: dict[str, Any] = {"verify_only": {"measured_stack": measured,
                                               "scoring_gamut": self._scoring_gamut_source(),
                                               "preheat_policy": self._preheat_policy() or "auto"}}
        listed = (((self.calib.get("stages") or {}).get("verify-patches-file") or {}).get("digest") or {})
        if self.calib.get("verify_patches_file") and listed:
            out["verify_only"]["verify_patches_file"] = listed
        source = self._verify_source_record()
        if source is None:
            return out
        src_verify = source.get("verify")
        if not src_verify:
            out["vs_source"] = {"source_run": source.get("run"),
                                "note": "the source run's verify was never scored — nothing to compare"}
            return out
        src_rows = None
        try:
            p = Path(source.get("patch_metrics_path") or "")
            if p.is_file():
                src_rows = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            src_rows = None
        now_rows = [{"rgb": list(m.rgb), "de2000": m.de2000, "gamut_clamped": m.gamut_clamped}
                    for m in metrics]
        basis_now = {"peak_nits": self._hdr_target().peak_nits if self.content_mode == "HDR" else None,
                     "oog_mapping": self._oog_mapping()}
        vs = verify_only.compare_verify(digest, src_verify, now_patch_rows=now_rows,
                                        source_patch_rows=src_rows if isinstance(src_rows, list) else None,
                                        basis_now=basis_now, basis_source=source.get("basis"))
        if self.content_mode == "HDR" and (source.get("basis") or {}).get("gamut_from_run") is False:
            # The source run built no MHC: its verify clamped against ITS day's DIP, which cannot be
            # reproduced — the core / limits / clamped split may be re-partitioned here.
            comp = vs.setdefault("comparability", {"like_for_like": True, "differences": []})
            comp["differences"].append("the source run clamped against its day's DIP (it built no MHC); "
                                       "this verify clamps against " + str(self._scoring_gamut_source()))
            comp["like_for_like"] = False
        out["vs_source"] = {"source_run": source.get("run"), **vs}
        # V2/V3 on ONE basis — three independent pieces of evidence, each failing on its own (a
        # partition failure must never discard the per-signal deltas already computed).
        src_metrics, src_lum = None, None
        try:
            src_metrics, src_lum = self._vs_source_rescore(source)
        except Exception as exc:  # noqa: BLE001 - evidence only
            out["vs_source"]["rescore_error"] = f"{type(exc).__name__}: {exc}"
        try:
            out["vs_source"].update(self._vs_source_per_signal(digest, source, src_metrics, src_lum))
        except Exception as exc:  # noqa: BLE001 - evidence only
            out["vs_source"]["per_signal_error"] = f"{type(exc).__name__}: {exc}"
        try:
            out["vs_source"]["held_out"] = self._vs_source_held_out(metrics, source, src_metrics)
        except Exception as exc:  # noqa: BLE001 - evidence only
            out["vs_source"]["held_out"] = {"available": False}
            out["vs_source"]["held_out_error"] = f"{type(exc).__name__}: {exc}"
        return out

    def _vs_source_rescore(self, source: Mapping[str, Any]) -> tuple[Optional[list], Optional[float]]:
        """The source run's verify.ti3 RE-SCORED with this run's scorer (which adopted the source's
        scoring basis — measured white, resolved white xy, OOG policy / peak; the live verify's
        gamut is reused so its level-edge seam is never asked twice) — ``(metrics, white)``, or
        ``(None, None)`` when the TI3 is gone."""
        ti3 = Path(str(source.get("run"))) / "measurements" / "verify.ti3"
        if not ti3.is_file():
            return None, None
        samples = parse_ti3(ti3)
        if not samples:
            return None, None
        rescored = self._score_verify_samples(samples, reachable=self._last_verify_reachable,
                                              reuse_reachable=True)
        return rescored["metrics"], rescored["lum"]

    def _vs_source_per_signal(self, digest: Mapping[str, Any], source: Mapping[str, Any],
                              src_metrics: Optional[list], src_lum: Optional[float]) -> dict[str, Any]:
        """V2/V3 per-unique-signal deltas vs the source on ONE basis: the source side re-scored from
        its verify.ti3 (:meth:`_vs_source_rescore`) rather than mixing a recorded number of another
        convention; the recorded ``per_signal`` is the fallback only when the TI3 is gone. Both
        measured whites are stated (each side scores RELATIVE to its own)."""
        spec = self._spec()
        recorded = ((source.get("verify") or {}).get("practical") or {}).get("per_signal")
        now_per = (digest.get("practical") or {}).get("per_signal") or {}
        if src_metrics is not None:
            src_per = metrics_mod.per_signal_summary(src_metrics, is_hdr=spec.is_hdr)
            basis = "rescored from the source's verify.ti3 with this run's scorer"
        elif recorded:
            src_per, basis = recorded, "the source's recorded per_signal (its verify.ti3 is gone)"
        else:
            return {"per_signal": {"available": False,
                                   "reason": "the source has neither a verify.ti3 nor a recorded per_signal"}}
        out: dict[str, Any] = {"per_signal": {
            "available": True, "source_basis": basis,
            "n_signals": {"now": now_per.get("n_signals"), "source": src_per.get("n_signals")},
            **{b: verify_only.bucket_delta(now_per.get(b) or {}, src_per.get(b) or {})
               for b in ("overall", "core", "tube")}}}
        now_white = (digest.get("sdr_white") or {}).get("scored_white_nits")
        if not spec.is_hdr and now_white is not None and src_lum:
            # Both sides score RELATIVE to their own measured white, which hides an absolute white
            # luminance change between the two measurements — so state it beside the ΔE deltas.
            out["scored_white_nits"] = {"now": now_white, "source": round(float(src_lum), 4),
                                        "delta_pct": round(100.0 * (float(now_white) / float(src_lum) - 1.0), 3)}
        return out

    def _vs_source_held_out(self, metrics: Sequence[Any], source: Mapping[str, Any],
                            src_metrics: Optional[list]) -> dict[str, Any]:
        """V1 held-out deltas vs the source: the partition is the SOURCE run's classification (its
        training TI3s + live probe drives, its cube) applied to both sides — the same signals, so
        the per-class deltas compare the stack, not the partition."""
        if src_metrics is None:
            return {"available": False, "reason": "the source's verify.ti3 is gone"}
        spec = self._spec()
        src_root = Path(str(source.get("run")))
        max_cv = self._transfer().max_cv
        src_state = json.loads((src_root / "dlc_state.json").read_text(encoding="utf-8"))
        training = verify_holdout.training_context(src_root, src_state.get("calib") or {}, max_cv=max_cv)
        if not training.get("available"):
            return {"available": False, "reason": f"source run: {training.get('reason')}"}
        cube = training["cube"]
        src_groups = metrics_mod.group_per_signal(list(src_metrics))
        part_rows = verify_holdout.classify_signals(
            [m.rgb for m, _ in src_groups], max_cv=max_cv, training=training["training_signals"],
            probe_drives=training["probe_drives"], cube=cube, lattice_size=training["lattice_size"])
        partition = {tuple(r["code"]): r for r in part_rows}

        def side(groups: list) -> dict[str, Any]:
            rows = []
            for m, _n in groups:
                key = tuple(int(c) for c in verify_holdout.to_codes([m.rgb], max_cv)[0])
                part = partition.get(key)
                if part is not None:
                    rows.append({"class": part["class"], "strict_held_out": part["strict_held_out"],
                                 "de": m.de2000, "zone": metrics_mod.practical_zone(m, is_hdr=spec.is_hdr)})
            return verify_holdout.held_out_summary(rows)

        now_s = side(metrics_mod.group_per_signal(list(metrics)))
        src_s = side(src_groups)
        held: dict[str, Any] = {
            "available": True,
            "partition": f"source run {src_root.name}'s training (TI3s + probe drives"
                         + (", drive space through its cube)" if cube is not None else ", signal space)")}
        for cls in ("held_out", "strict_held_out", "near", "coincident"):
            held[cls] = verify_only.bucket_delta(now_s.get(cls) or {}, src_s.get(cls) or {})
        return held

    def _finish_verify_only(self) -> CalibrationResult:
        """The no-commit finish: a candidate's fate is the ``verify:candidate`` seam (restore
        recommended; keep = stays live + recorded in the stack registry), then the report. No
        durable-cube re-point, no MHC/registry write of its own."""
        status = "completed"
        cand = self.calib.get("verify_candidate") or {}
        cand = self._redecide_resolved_candidate(cand)
        if (self.calib.get("verify_cube") and cand.get("installed") and not cand.get("restored")
                and not cand.get("kept")):
            verify = ((self.calib["stages"].get("verify") or {}).get("digest") or {})
            scored = (verify.get("gate") or {}).get("scored") or {}
            vs = verify.get("vs_source") or {}
            reads = (f"core avg {scored.get('core_avg')} ({str(scored.get('basis') or 'read_weighted').replace('_', '-')}), "
                     f"tube {scored.get('tube_avg')}, white "
                     f"{_round3(scored.get('white'))} {verify.get('metric', 'ΔE')}" if scored else
                     f"avg {verify.get('avg_de2000')} {verify.get('metric', 'ΔE')}")
            if vs.get("buckets"):
                reads += "; vs " + Path(str(vs.get("source_run"))).name + ": " + ", ".join(
                    f"{b} {row['avg']['delta']:+.3f}" for b, row in vs["buckets"].items()
                    if (row.get("avg") or {}).get("delta") is not None)
            decision = self.adjudicate(AdjudicationRequest(
                key="verify:candidate", seam=SEAM_VERIFY, stage="verify",
                question=(f"The candidate 3D LUT {Path(str(cand.get('cube'))).name} reads "
                          f"{_content_lead_text(verify)}{reads} "
                          f"({'within' if verify.get('within_quality') else 'outside'} the quality "
                          "targets). Restore the prior cube "
                          f"({cand.get('prior_cube') or 'none — clear the slot'}), or keep the "
                          "candidate installed (recorded in the stack registry)?"),
                options=("restore", "keep"), recommendation="restore",
                digest={"candidate": cand, "verify": verify, "vs_source": vs or None,
                        "gate_failed": not bool(verify.get("within_quality")),
                        "keep_records": "stack_registry cube entry for "
                                        f"{self.display.name}:{self.mode} (the MHC record is untouched)"}))
            if decision.choice == "keep":
                self._keep_verify_candidate()
            else:
                rec = self._restore_verify_candidate(why="verify:candidate=restore")
                if not (rec or {}).get("restored"):
                    status = "revert_unavailable"
        rep = self.stage_report()
        self.runlog.run_done(status, results_dir=rep.data.get("results_dir"),
                             report_path=rep.data.get("report_path"))
        return CalibrationResult(
            flow="verify-only", monitor=self.monitor, mode=self.mode, target=self.target_name,
            status=status, stages=list(self.calib["stages"].keys()),
            results_dir=rep.data.get("results_dir"), report_path=rep.data.get("report_path"),
            digest={**rep.digest, "candidate": self.calib.get("verify_candidate")})


# The ordered stage keys each flow walks/announces on the spine — the DECLARATIVE mirror of
# the imperative ``_flow_*`` methods, consumed by ``Calibration._planned_stages`` (the
# dashboard stepper). Pinned equal to the phases each flow actually announces, per flow and
# both modes, by ``test_planned_stages_match_announced_phases_per_flow`` — edit a ``_flow_*``
# method and this table together or that pin trips. ``_REFINE_FORK`` marks the one mode fork
# (HDR refines the MHC base 1D cube, SDR the correctionGrayscale layer). Stages that never
# announce a phase (require-stack, inplace-baseline) are deliberately absent — the stepper
# tracks what the dashboard can see; opt-in stages (adaptive-planning, hardware-readiness)
# are listed here and filtered out by ``_planned_stages`` when their gate is off.
_REFINE_FORK = "{refine}"
_FLOW_STAGE_SEQUENCES: dict[str, tuple[str, ...]] = {
    "full": ("preflight", "resolve-target", "whitepoint", "enter-neutral",
             "hardware-readiness", "brightness", "measure:raw", "build-install-mhc",
             _REFINE_FORK, "adaptive-planning", "measure:post-mhc", "build-install-3dlut",
             "measure:verify", "verify"),
    "mhc-only": ("preflight", "resolve-target", "whitepoint", "enter-neutral",
                 "hardware-readiness", "brightness", "measure:raw", "build-install-mhc",
                 _REFINE_FORK, "measure:verify", "verify"),
    "3dlut-only": ("preflight", "resolve-target", "whitepoint", "hardware-readiness",
                   "adaptive-planning", "measure:post-mhc", "build-install-3dlut",
                   "measure:verify", "verify"),
    "grayscale-wb": ("preflight", "resolve-target", "whitepoint", "hardware-readiness",
                     "grayscale-wb", "measure:verify", "verify"),
    "refine-mhc": ("preflight", "resolve-target", "whitepoint", "seed-from-run", "enter-neutral",
                   "hardware-readiness", "install-mhc", "refine-mhc-grayscale", "reapply-3dlut",
                   "measure:verify", "verify"),
    "build-correction": ("preflight", "clear-native", "probe-match"),
    "characterize": ("preflight", "clear-native", "hardware-readiness", "characterize"),
    # verify-source / install-candidate only with --verify-patches-from / --verify-cube.
    "verify-only": ("preflight", "verify-source", "verify-patches-file", "resolve-target", "whitepoint",
                    "hardware-readiness", "install-candidate", "measure:verify", "verify"),
}

# Flow registry (the named flows the front door maps an intent onto). HDR is a run
# MODE (--mode HDR), orthogonal to the flow — the "hdr" entry is a signpost stub.
FLOWS: dict[str, str] = {
    "full": "neutral → raw → MHC + D65 grayscale refine → post-MHC → 3D LUT → verify → report",
    "mhc-only": "raw → MHC (matrix + 1D + D65 grayscale refine) → verify → report (ICC only; no 3D LUT — shakedown)",
    "3dlut-only": "verify MHC present → measure → 3D LUT → verify → report",
    "grayscale-wb": "verify MHC present -> patch-by-patch user Grayscale correction -> grey-ramp verify",
    "refine-mhc": ("SDR: seed a completed run's MHC (--source-run) → neutral → reinstall MHC → re-refine "
                   "grayscale (white band) → re-apply its 3D LUT → short verify → apply gate"),
    "build-correction": "preflight → prepare ccxxmake → operator runs it → ingest .ccmx (+white.sp) → store",
    "characterize": "preflight → plan → clear-native → learn panel+meter (noise/settle/drift) → DIP store → restore",
    "verify-only": ("MEASURE the installed stack (or a --verify-cube candidate over it) against the verify "
                    "preset or a recorded run's exact set (--verify-patches-from, deltas vs its verify) → "
                    "report; builds/commits nothing (candidate: verify:candidate restore/keep)"),
    "hdr": "(not a flow — signpost) HDR is a MODE: use --mode HDR with full / mhc-only / 3dlut-only",
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


def _round3(value: Any) -> Any:
    """A number rounded to 3 decimals for a seam question (non-numbers pass through)."""
    v = _as_float_local(value)
    return value if v is None else round(v, 3)


# What calibration.exit(restore_snapshot=True) actually did — shared with the stage tools
# (fald-profile reads it the same way).
snapshot_restore_report = _common.snapshot_restore_report


def _restore_hint(calib_state: Optional[dict[str, Any]]) -> str:
    """Where the operator restores from when the pipe put nothing (or not everything) back."""
    calib_state = calib_state or {}
    bak = calib_state.get("backup") or {}
    ref = bak.get("ini_backup") or bak.get("path")
    hint = "restore from the pre-run settings backup" + (f" ({ref})" if ref else " in the run folder")
    cand = calib_state.get("verify_candidate")
    if isinstance(cand, dict) and cand.get("installed") and not cand.get("restored") and not cand.get("kept"):
        hint += ("; the verify-only CANDIDATE cube is still installed — re-apply "
                 + (f"the prior 3D LUT {cand.get('prior_cube')}" if cand.get("prior_cube") else "no cube (clear the slot)")
                 + " in DesktopLUT" + (f" ({cand.get('restore_error')})" if cand.get("restore_error") else ""))
    base = calib_state.get("inplace_baseline")
    if isinstance(base, dict) and base.get("captured"):
        if calib_state.get("flow") == "grayscale-wb":
            hint += ("; this in-place flow never entered calibration mode — its display change is the "
                     "MHC correction grayscale (the pre-touch-up curve is `grayscale_wb_prior` in the run's "
                     "dlc_state.json)")
        else:
            prev = base.get("cube_path")
            hint += ("; this in-place flow never entered calibration mode — its display change is the "
                     "runtime 3D LUT, which was " + (f"{prev} before the run" if prev else "empty before the run"))
    return hint


# Flows that never enter calibration mode: they tune / measure the INSTALLED stack in place, so
# DesktopLUT never takes a snapshot of them (their own undo is the in-place baseline / candidate).
_NON_ENTERING_FLOWS = ("3dlut-only", "grayscale-wb", "verify-only")


def _run_entered_calibration(calib_state: Optional[dict[str, Any]], *,
                             flow: Optional[str] = None) -> Optional[bool]:
    """Did this run enter calibration mode (so DesktopLUT may hold a capture of it)? True once an
    entering stage ran; False for a flow that never enters (``_NON_ENTERING_FLOWS`` — ``flow`` is
    the fallback when the record has none) or a recorded in-place baseline; ``None`` when the run
    record cannot tell (no record — ``--abort`` against a live pipe — or an entering flow paused
    before its enter stage completed)."""
    rec = calib_state if isinstance(calib_state, dict) else {}
    stages = rec.get("stages") or {}
    if "enter-neutral" in stages or "clear-native" in stages:
        return True
    if (rec.get("flow") or flow) in _NON_ENTERING_FLOWS:
        return False
    if rec.get("inplace_baseline") is not None:
        return False
    return None


def _enter_stale_tell(calib_state: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The stale-session tell this run's enter-neutral recorded (None when there was none)."""
    rec = calib_state if isinstance(calib_state, dict) else {}
    digest = (((rec.get("stages") or {}).get("enter-neutral") or {}).get("digest") or {})
    tell = digest.get("stale_calibration")
    return tell if isinstance(tell, dict) else None


def _run_monitor(calib_state: Optional[dict[str, Any]], fallback: Optional[int]) -> Optional[int]:
    rec = calib_state if isinstance(calib_state, dict) else {}
    for src in (rec.get("neutral_profile"), rec.get("viewing_layers")):
        if isinstance(src, dict) and src.get("monitor") is not None:
            return int(src["monitor"])
    return fallback


def _revert_status(report: Mapping[str, Any]) -> str:
    """The terminal status of a snapshot revert, from what DesktopLUT SAID (never "the call
    returned"): complete → reverted; something but not everything back (a display unresolved, an
    MHC reinstall failed, a stale single slot) → reverted_partially; nothing → revert_unavailable;
    a reply that did not say → revert_unconfirmed."""
    if report.get("complete"):
        return "reverted"
    if report.get("restored") is True:
        return "reverted_partially"
    if report.get("restored") is False or report.get("error"):
        return "revert_unavailable"
    return "revert_unconfirmed"


def _abort_restore(controller: Any, calib_state: Optional[dict[str, Any]], *, monitor: Optional[int],
                   mode: Optional[str], run_root: Any, flow: Optional[str] = None,
                   log: Optional[Callable[[str], None]] = None) -> tuple[int, dict[str, Any]]:
    """``--abort``: undo what this run changed and report what was ACTUALLY put back. Returns
    ``(exit_code, payload)`` for the CLI to print.

    * a verify-only candidate cube is put back first (``verify_only.restore_candidate``);
    * the pre-run snapshot is requested only through :func:`_common.request_snapshot_restore` —
      never for a run that did not enter calibration mode, and never when DesktopLUT holds no open
      session / capture (a restarted DesktopLUT, an exited session, a build predating the snapshot
      store whose stale slot would roll an accepted calibration back);
    * the user's viewing layers are re-asserted.
    Status: ``reverted`` / ``partially_reverted`` / ``nothing_restored`` / ``restore_unknown`` (a
    reply that did not say) — a verify-only run is ``reverted`` once its candidate is back."""
    candidate = None
    if isinstance(calib_state, dict):
        candidate = verify_only.restore_candidate(controller, calib_state, why="--abort of a paused run",
                                                  log=log, terminal=True)
    entered = _run_entered_calibration(calib_state, flow=flow)
    mon = _run_monitor(calib_state, monitor)
    try:
        report = _common.request_snapshot_restore(controller, entered=entered, monitor=mon,
                                                  stale_tell=_enter_stale_tell(calib_state))
    except Exception as exc:  # noqa: BLE001
        return 1, {"status": "abort_failed", "error": f"{type(exc).__name__}: {exc}",
                   "verify_candidate": candidate, "run": str(run_root)}
    bak = ((calib_state or {}).get("backup") or {})
    layers = _reassert_viewing_layers(controller, calib_state, monitor=monitor, mode=mode)
    run_flow = (calib_state or {}).get("flow") or flow
    if entered is False and run_flow == "verify-only":
        # verify-only's only display changes are the candidate cube and the viewing layers
        cand_back = (not isinstance(candidate, dict) or not candidate.get("installed")
                     or candidate.get("restored") or candidate.get("kept"))
        status = "reverted" if cand_back else "revert_unavailable"
    elif report.get("complete"):
        status = "reverted"
    elif report.get("restored") is True:
        status = "partially_reverted"
    elif report.get("restored") is False:
        status = "nothing_restored"
    else:
        status = "restore_unknown"
    payload: dict[str, Any] = {"status": status, "restored_snapshot": report.get("restored"),
                               "snapshot_restore": report, "viewing_layers": layers,
                               "verify_candidate": candidate, "backup": bak, "run": str(run_root)}
    if status != "reverted":
        payload["hint"] = _restore_hint(calib_state)
    return 0, payload


def _rollback_restore(controller: Any, calib_state: Optional[dict[str, Any]], *, monitor: Optional[int],
                      mode: Optional[str], run_root: Any,
                      entered_calibration: Optional[bool] = None) -> dict[str, Any]:
    """The CLI rollback guard's restore (a run that ended neither applied nor reverted): request the
    pre-run snapshot through :func:`_common.request_snapshot_restore` (never for a run that did not
    enter calibration mode, never when DesktopLUT holds no open session / capture), re-assert the
    viewing layers when a restore ran, and report what DesktopLUT actually put back. Raises when
    the exit call itself fails (the caller reports ``rollback_failed``).

    A run that never entered (an in-place flow) gets its grayscale-wb live preview cancelled — the
    one pipe-side undo that is safe without a session — and an honest "not rolled back"."""
    if entered_calibration is False and (calib_state or {}).get("flow") == "grayscale-wb" \
            and monitor is not None and mode:
        try:
            controller.grayscale_cancel(int(monitor), str(mode))   # no-op when no preview is live
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
    report = _common.request_snapshot_restore(controller, entered=entered_calibration,
                                              monitor=_run_monitor(calib_state, monitor),
                                              stale_tell=_enter_stale_tell(calib_state))
    # The snapshot predates nothing the user cares about: it was taken after their viewing layers
    # were switched off, so put them back (run() already did, and a restore undoes it).
    layers = (_reassert_viewing_layers(controller, calib_state, monitor=monitor, mode=mode)
              if report.get("requested") else None)
    if report.get("complete"):
        status, reason = "rolled_back", "run did not complete; restored pre-run setup"
    elif report.get("restored") is True:
        status, reason = "rolled_back_partially", "run did not complete; " + report["summary"]
    elif report.get("restored") is False:
        status, reason = "rollback_restored_nothing", "run did not complete; " + report["summary"]
    else:
        status, reason = "rollback_unconfirmed", "run did not complete; " + report["summary"]
    payload: dict[str, Any] = {"status": status, "reason": reason, "snapshot_restore": report,
                               "viewing_layers": layers, "run": str(run_root)}
    if not report.get("complete"):
        payload["backup"] = ((calib_state or {}).get("backup") or {})
        payload["hint"] = _restore_hint(calib_state)
    return payload


def _reassert_viewing_layers(controller: Any, calib_state: Optional[dict[str, Any]], *,
                             monitor: Optional[int] = None,
                             mode: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Put the user's captured viewing layers back ON after a snapshot-restore teardown.

    ``exit_calibration(restore_snapshot=True)`` restores the C++ snapshot taken at
    ``calibration.enter`` — which neutral flows reach AFTER the spine switched the layers off —
    so on the CLI rollback guard (run aborted: ``run()`` had already restored the layers, the
    snapshot switched them off again) and the ``--abort`` path the user was left with their
    tonemap / DG / WB / GS / FALD off (HW 2026-09-23). Monitor/mode come from the record (then
    the neutral profile, then the caller) — never from argparse defaults alone. Best-effort:
    returns ``None`` when nothing was captured, else a record of what was re-asserted."""
    calib_state = calib_state or {}
    rec = calib_state.get("viewing_layers")
    if not isinstance(rec, dict) or not rec.get("captured"):
        return None
    kept = set(rec.get("kept") or ())           # --keep-layers: never changed by the run — never re-set
    want = {name: True for name, on in (rec.get("before") or {}).items() if on and name not in kept}
    if not want:
        return {"reasserted": [], "note": "nothing was on"}
    neutral = calib_state.get("neutral_profile") if isinstance(calib_state.get("neutral_profile"), dict) else {}
    mon = next((m for m in (rec.get("monitor"), neutral.get("monitor"), monitor) if m is not None), None)
    md = next((m for m in (rec.get("mode"), neutral.get("mode"), mode) if m), None)
    if mon is None or not md:
        return {"reasserted": [], "error": "monitor/mode unknown — re-enable by hand",
                "layers": sorted(want)}
    try:
        res = controller.set_layers(int(mon), str(md), **want)
        return {"reasserted": sorted(want), "monitor": int(mon), "mode": str(md),
                "profile": (res or {}).get("profile_name")}
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never fatal to teardown
        return {"reasserted": [], "error": f"{type(exc).__name__}: {exc}", "layers": sorted(want),
                "monitor": int(mon), "mode": str(md)}


def _main_pass_reads(ndjson_path: Optional[str]) -> tuple[dict[str, list[tuple[Any, Any]]], set[str]]:
    """``(label → [(xyz, rgb)] accepted MAIN-pass measurement reads, labels that were
    appended-re-measured)`` from a measure stage's ndjson; empty when there is no readable ndjson
    (the caller falls back to the .ti3)."""
    reads: dict[str, list[tuple[Any, Any]]] = {}
    remeasured: set[str] = set()
    if not ndjson_path:
        return reads, remeasured
    try:
        with open(ndjson_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("role") != "measurement":
                    continue
                label = rec.get("label")
                if rec.get("phase") == "remeasure":
                    if label:
                        remeasured.add(label)
                    continue
                xyz = rec.get("xyz")
                if rec.get("phase") == "main" and label and rec.get("accepted") and xyz:
                    reads.setdefault(label, []).append(
                        ((float(xyz[0]), float(xyz[1]), float(xyz[2])), rec.get("rgb")))
    except OSError:
        return {}, set()
    return reads, remeasured


def _measure_escalation_recommendation(digest: dict[str, Any]) -> tuple[str, Optional[str]]:
    """``(recommendation, basis)`` for the measure escalation seam — a SUGGESTION to the LLM
    judge, never an auto-action (Design Law: the seam decides; ``--auto`` is sim/CI only and
    Supervised still escalates on the unchanged ``compromised`` flag).

    Retry for the non-benign compromise signals (dark panel, present-stall, compromised
    preheat/path, blown remeasure/drift budgets) — EXCEPT the one case retry provably cannot
    fix (item #4, 2026-09-02 C6 run): when the ONLY compromise is plausibility-envelope
    anomalies, all of them too-DIM reads (``lit_drive_low_luminance`` — what a correction or a
    gamut-limited channel legitimately produces; a stable too-BRIGHT read is never panel
    physics), and the flagged reads are REPEATABLE (the digest's read-repeatability evidence:
    the same stimulus re-read to the same implausible value). That is stable-but-implausible =
    real panel/correction behaviour; a retry re-measures the same dim patch and re-fails
    forever, so the recommendation flips to accept — with the basis spelled out for the judge.

    The drift budgets (dense drift / blown re-measure budget) are MECHANICS alarms: the stage's
    own start/end saturation-sweep bookends are the direct evidence of whether the data moved.
    When those are the ONLY compromise and the bookends moved less than the materiality
    (¼ JND — below what a correction round would chase), the data is sound and a retry would
    re-measure the same panel behaviour (BenQ PD2700U 2026-09-27: a state-toggling panel drove
    24 episodes and a dense-drift ``retry`` while the bookends moved ≤ 0.19 ΔE2000) — accept,
    basis spelled out. No bookend evidence (raw/refine stages) keeps the conservative retry."""
    stopper = bool(digest.get("meter_down")
                   or digest.get("panel_dark")
                   or digest.get("present_stall")
                   or digest.get("preheat_compromised"))
    drift_alarm = bool(digest.get("remeasure_budget_exceeded")
                       or digest.get("drift_density_exceeded"))
    path_compromised = bool(digest.get("measurement_path_compromised"))
    if stopper:
        return "retry", None
    if drift_alarm:
        if path_compromised:
            return "retry", None
        bookend = digest.get("bookend_drift_qc") or {}
        moved = _as_float_local(bookend.get("max_delta_de")) if bookend.get("available") else None
        if moved is None or moved > refine_convergence.MATERIAL_GAIN_JND:
            return "retry", None
        return "accept", (
            f"the drift alarms fired, but this stage's own start/end bookends "
            f"({bookend.get('unique_signals')} signals × {bookend.get('repeats_per_location')} reads, "
            f"{bookend.get('witness_source', 'ti3')} reads) moved at most {moved:g} "
            f"{bookend.get('metric', 'ΔE')} (mean {bookend.get('mean_delta_de')}) — below the "
            f"{refine_convergence.MATERIAL_GAIN_JND:g} materiality: start and end agree, so a retry "
            "would most likely re-measure the same panel behaviour (the bookends cannot see an "
            "excursion that returned mid-stage — weigh the drift-reference evidence too)"
        )
    if not path_compromised:
        states = digest.get("reference_states") or {}
        if states.get("perceptible"):
            return "accept", (
                "the drift reference kept returning to previously settled states whose spread is "
                "perceptible — if that is a panel toggle a retry cannot remove it (check the panel "
                "OSD for a dynamic-contrast / eco-dimming feature); if it is drift that returned, "
                "judge from reference_states (levels, flips, settled_span) whether to re-measure"
            )
        return "accept", None
    repeat = digest.get("read_anomaly_repeatability") or {}
    if repeat.get("classification") == "stable" and repeat.get("all_low_luminance") is True:
        return "accept", (
            "anomalous reads are repeatable (stable-but-implausible, all low-luminance) — "
            "consistent with real panel/correction behaviour, e.g. a gamut-limited channel or an "
            "installed correction's attenuation; a retry would re-measure the same values"
        )
    if repeat.get("classification") == "noisy":
        return "retry", (
            "anomalous reads are divergent across re-reads — consistent with a transient "
            "meter/display fault a retry should clear"
        )
    return "retry", None


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


# The patch-set builders (build_ramp_set / build_volumetric_set / build_neutral_set /
# build_grayscale_wb_set / build_verify_set / flow_patch_counts) moved verbatim to
# dlc/patch_sets.py (fable Phase 7b) with the PatchSizes knobs they consume; re-imported
# above so every existing `from dlc.calibrate import build_*` keeps working.


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


def dip_record_for(store: DipStore, display_name: str,
                   mode: Optional[str]) -> Optional[DisplayInstrumentProfile]:
    """Look up a display's DIP the way the store is KEYED: ``display:mode`` first (the
    characterize flow stores mode-keyed records — panel thermal/noise behaviour differs by
    mode), falling back to a bare mode-less record for back-compat. Every consumer must use
    this two-key lookup — a bare ``store.get(name)`` silently misses every mode-keyed DIP
    (Calibration._dip does the same dance; this is the module-level twin for ``main()``)."""
    if mode:
        rec = store.get(f"{display_name}:{mode}")
        if rec is not None:
            return rec
    return store.get(display_name)


def _render_cmd(argv: Sequence[Any]) -> str:
    """Render an argv list as a copy-pasteable command line (Windows quoting)."""
    import subprocess
    return subprocess.list2cmdline([str(a) for a in argv])


@dataclass(frozen=True)
class CorrectionResolution:
    """Which colorimeter correction a run in ``mode`` uses, and WHY — the evidence the
    header / preflight carry so a cross-mode fallback is never silent."""

    file: Optional[str]
    source: str                       # "store" (this mode's slot) | "profile" (YAML) | "none"
    mode: str
    mode_source: Optional[str] = None  # the store slot's provenance (legacy inference is flagged)
    other_modes: tuple[str, ...] = ()  # modes whose store slot holds a correction NOT used here
    warning: Optional[str] = None
    # source "profile" only: why the meter-level YAML file may be ANOTHER display's correction (the
    # profile drives other displays / the store holds other displays' corrections / the store records
    # this very file for another display) — None when this display is the rig's only one
    cross_display: Optional[dict[str, list[str]]] = None

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "source": self.source, "mode": self.mode,
                "mode_source": self.mode_source, "other_modes": list(self.other_modes),
                "warning": self.warning, "cross_display": self.cross_display}


def _profile_file_cross_display(profile: cp.Profile, store: CorrectionStore, display_name: str,
                                file: str) -> Optional[dict[str, list[str]]]:
    """Mechanical evidence that ``profile.meter.correction.file`` — a per-METER setting, not a
    per-display one — may belong to another panel: the profile's other displays, the displays the
    correction store holds corrections for, and those whose slot records this very file (by name).
    ``None`` when nothing else is on record (a single-display rig: the file can only be meant for it)."""
    recs = store.records()
    fname = Path(str(file)).name.lower()
    profile_displays = sorted({d.name for d in profile.displays if d.name != display_name})
    store_displays = sorted({d for (d, _m), r in recs.items() if d != display_name and r.correction_file})
    recorded_for = sorted(f"{d} ({m})" for (d, m), r in recs.items()
                          if d != display_name and r.correction_file
                          and Path(str(r.correction_file)).name.lower() == fname)
    if not (profile_displays or store_displays or recorded_for):
        return None
    return {"profile_displays": profile_displays, "store_displays": store_displays,
            "recorded_for": recorded_for}


def resolve_correction(profile: cp.Profile, store: CorrectionStore, display_name: str,
                       mode: str) -> CorrectionResolution:
    """Resolve the correction for a ``mode`` run: this mode's store slot (e.g. a freshly
    probe-matched CCMX) overrides the profile YAML; with no slot it falls back to
    ``profile.meter.correction.file`` — NEVER to the other mode's stored correction (a
    CCMX is built against one mode's spectra; the PA32UCXR SDR runs 2026-06-19..09-25
    measured through the HDR one, ~1.6 dE2000 on full red). The fallback, and a slot whose
    mode was only inferred from a legacy schema-1 file, carry a ``warning`` for the evidence.
    The YAML file is per-METER: on a rig with other displays on record a fallback carries
    ``cross_display`` (it may be another panel's correction — the ``preflight:correction`` seam)."""
    mode = normalize_mode(mode)
    rec = store.get(display_name, mode)
    others = tuple(m for m, r in sorted(store.modes_for(display_name).items())
                   if m != mode and r.correction_file)
    if rec and rec.correction_file:
        warning = None
        if (rec.mode_source or "").startswith("legacy"):
            warning = (f"{mode} correction {Path(rec.correction_file).name} was assigned to the {mode} "
                       f"slot by legacy inference ({rec.mode_source}), not recorded for {mode} — confirm "
                       f"it is the {mode} correction (a fresh build-correction --mode {mode} records it).")
        return CorrectionResolution(rec.correction_file, "store", mode, rec.mode_source, others, warning)
    fallback = profile.meter.correction.file
    warning = None
    cross = _profile_file_cross_display(profile, store, display_name, fallback) if fallback else None
    if others:
        other_files = ", ".join(f"{m}: {Path(store.get(display_name, m).correction_file).name}" for m in others)
        warning = (f"no {mode} colorimeter correction recorded for {display_name} — falling back to the "
                   f"profile YAML ({fallback or 'none → RAW meter readings'}); the store's other-mode "
                   f"correction ({other_files}) is deliberately NOT used for a {mode} run. Build one with "
                   f"`--flow build-correction --mode {mode}`.")
    elif cross:
        warning = (f"no colorimeter correction recorded for {display_name} — falling back to the profile "
                   f"YAML's meter-level {Path(str(fallback)).name}, which is not this display's own "
                   f"recorded correction (other displays on record: "
                   f"{', '.join(sorted(set(cross['profile_displays']) | set(cross['store_displays'])))}"
                   + (f"; the store records it for {', '.join(cross['recorded_for'])}" if cross["recorded_for"] else "")
                   + f"). Build this display's own with `--flow build-correction --mode {mode}`.")
    return CorrectionResolution(fallback, "profile" if fallback else "none", mode, None, others, warning,
                                cross)


def active_correction(profile: cp.Profile, store: CorrectionStore, display_name: str,
                      mode: str) -> Optional[str]:
    """The colorimeter correction file the meter should use for a ``mode`` run — see
    :func:`resolve_correction` (which also says why). The store is the machine-maintained
    record; the profile is the human-authored config (the §2 skill ⊥ user-data boundary)."""
    return resolve_correction(profile, store, display_name, mode).file


def _content_spec_resolved(spec: Any) -> str:
    """``PATH[#VARIANT]`` with PATH made absolute (the run record must not depend on the cwd)."""
    from .content_score import parse_content_spec

    path, variant = parse_content_spec(str(spec))
    return str(Path(path).resolve()) + (f"#{variant}" if variant else "")


def _black_floor_arg(value: Optional[float]) -> Optional[float]:
    """``--score-black-floor-nits`` validated: a finite, non-negative luminance (nit), or ``None``."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = float("nan")
    if not math.isfinite(f) or f < 0.0:
        raise ValueError(f"score_black_floor_nits must be a finite, non-negative luminance (nit), got {value!r}")
    return f


def _content_lead_text(digest: Mapping[str, Any]) -> str:
    """The seam question's lead: the content-weighted score + its coverage gap, labelled (evidence the
    LLM weighs — never a gate). Empty without one."""
    lead = (digest or {}).get("content_weighted") or {}
    if lead.get("score") is None:
        return ""
    gap = lead.get("coverage_gap_pct")
    weak = lead.get("weak_evidence_score_share_pct")
    bc = lead.get("score_bias_corrected")
    black = bool(lead.get("black_aware"))
    raw = lead.get("score_raw") if black else None
    # beside a black-aware score every variant / share is on the black-aware basis: say so (one basis per line)
    basis = " (black-aware basis)" if black else ""
    return (f"{lead.get('label')} {lead['score']}"
            + (f" [raw {raw}]" if raw is not None else "")
            + (f" [noise bias-corrected variant{basis} {bc}]" if bc is not None and bc != lead["score"] else "")
            + (f" (coverage gap {gap} % of content with no patch within R)" if gap is not None else "")
            + (f", {weak} % of it{basis} resting on weak reads (single / at the meter floor / low-SNR)"
               if weak is not None else "") + "; ")


def _html_text(v: Any) -> str:
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_practical_html(practical: Optional[Mapping[str, Any]], de: str) -> str:
    """The practical block of the deliverable report: the content-weighted score + coverage gap FIRST
    (labelled with the class and R), the practical zones underneath. Evidence — no gate reads it."""
    practical = practical or {}
    cw = practical.get("content_weighted") or {}
    black = cw.get("black_aware") or {}
    rows: list[str] = []
    for name, res in (cw.get("classes") or {}).items():
        if res.get("score") is None:
            rows.append(f"<tr><td>Content-weighted {de} — {name}</td><td class='muted'>"
                        f"{res.get('error') or 'no covered content'}</td></tr>")
            continue
        weak = ((res.get("evidence") or {}).get("weak") or {}).get("score_share_pct")
        nl = ((res.get("evidence") or {}).get("noise_limited") or {}).get("content_share_pct")
        bc = (res.get("bias_corrected") or {}).get("score")
        rows.append(f"<tr><td><b>Content-weighted {de} — {name}, R {res.get('reach_dEITP'):g}</b></td>"
                    f"<td><b>{res.get('score')}</b> · coverage gap {res.get('coverage_gap_pct')} % · "
                    f"nearest-patch fallback {res.get('score_with_nearest_fallback')}"
                    + (f" · {weak} % of the score on weak reads" if weak is not None else "")
                    + (f" · noise bias-corrected variant {bc} ({nl} % of content noise-limited)"
                       if bc is not None else "") + "</td></tr>")
        ba = res.get("black_aware") or {}
        if ba.get("score") is not None:
            toe = (ba.get("vs_toe_point") or {}).get("score")
            band = (ba.get("vs_bt2390_band") or {}).get("score")
            bcb = (ba.get("bias_corrected") or {}).get("score")
            rows.append(f"<tr><td><b>Content-weighted {de} — {name}, BLACK-AWARE</b> (display floor "
                        f"{black.get('floor_nits')} nit from {_html_text(black.get('floor_source'))}; pedestal "
                        f"colour {black.get('pedestal_xy')} from {_html_text(black.get('pedestal_source'))})</td>"
                        f"<td><b>{ba.get('score')}</b> (raw {res.get('score')}) · nearest-patch fallback "
                        f"{ba.get('score_with_nearest_fallback')}"
                        + (f" · noise bias-corrected variant (black-aware basis) {bcb}" if bcb is not None else "")
                        + (f" · superseded BT.2390-band variant {band}" if band is not None else "")
                        + (f" · literal BT.2390-point variant {toe}" if toe is not None else "") + "</td></tr>")
    pw = cw.get("patch_weights") or {}
    if pw.get("score") is not None:
        weak = (pw.get("score_share") or {}).get("weak")
        rows.append(f"<tr><td><b>Content-weighted {de} — patch weights ({pw.get('label')})</b></td>"
                    f"<td><b>{pw.get('score')}</b> over {pw.get('n')} weighted signals"
                    + (f" · gap as drawn {pw.get('coverage_gap_pct_as_drawn')} %"
                       if pw.get("coverage_gap_pct_as_drawn") is not None else "")
                    + (f" · {round(100 * weak, 1)} % of the score on weak reads" if weak is not None else "")
                    + (f" · noise bias-corrected variant {pw.get('score_bias_corrected')}"
                       if pw.get("score_bias_corrected") is not None else "")
                    + "</td></tr>")
        pb = pw.get("black_aware") or {}
        if pb.get("score") is not None:
            weak_b = (pb.get("score_share") or {}).get("weak")
            rows.append(f"<tr><td><b>Content-weighted {de} — patch weights ({pw.get('label')}), BLACK-AWARE</b>"
                        f"</td><td><b>{pb.get('score')}</b> (raw {pw.get('score')})"
                        + (f" · {round(100 * weak_b, 1)} % of it on weak reads (black-aware basis)"
                           if weak_b is not None else "")
                        + (f" · noise bias-corrected variant (black-aware basis) {pb.get('score_bias_corrected')}"
                           if pb.get("score_bias_corrected") is not None else "") + "</td></tr>")
    fl = black.get("floor_limited") or {}
    if (fl.get("raw") or {}).get("n"):
        raw_b, ba_b, band_b = fl["raw"], fl.get("black_aware") or {}, fl.get("vs_bt2390_band") or {}
        rows.append(f"<tr><td>Floor-limited signals ({_html_text(black.get('floor_limited_rule'))})</td><td>raw avg "
                    f"{raw_b.get('avg')} · max {raw_b.get('max')} → black-aware avg {ba_b.get('avg')} · max "
                    f"{ba_b.get('max')}"
                    + (f" (superseded BT.2390-band variant avg {band_b.get('avg')})" if band_b.get("n") else "")
                    + f" (n {raw_b.get('n')}; evidence only — the zones below are unchanged)</td></tr>")
    bmf = black.get("below_meter_floor") or {}
    if black.get("applied") and bmf:
        shares = "; ".join(f"{_html_text(k)} {v.get('content_share_pct')} % of content"
                           for k, v in (bmf.get("content_share_by_class") or {}).items())
        rows.append(f"<tr><td>Near-black evidence</td><td>{black.get('n_signals_scored_vs_black')} signal(s) "
                    f"scored against true black (target below the floor) · {bmf.get('n_signals')} below the "
                    f"{bmf.get('meter_floor_nits'):g}-nit meter floor, target and read"
                    + (f" ({shares})" if shares else "")
                    + f" — still scored ({_html_text(bmf.get('meter_floor_source'))})</td></tr>")
    cont = (black.get("continuity") or {}).get("target_ge_1_nit") or {}
    if cont.get("max_abs_change_dEITP") is not None:
        rows.append(f"<tr><td>Black-aware continuity</td><td>largest per-signal change at PQ target ≥ 1 nit: "
                    f"{cont.get('max_abs_change_dEITP')} {de} (n {cont.get('n')}; no cutoff, the pedestal "
                    f"vanishes at bright levels)</td></tr>")
    elif black and not black.get("applied"):
        rows.append(f"<tr><td>Black-aware score</td><td class='muted'>not applied: "
                    f"{_html_text(black.get('reason'))}</td></tr>")
    per = practical.get("per_signal") or {}
    zones = per if (per.get("core") or {}).get("n") else practical
    for zone, label in (("core", "core (Rec.709 ≤ ref-white)"), ("limits", "limits"),
                        ("clamped", "clamped (gamut floor)"), ("tube", "near-neutral tube")):
        b = zones.get(zone) or {}
        if b.get("n"):
            rows.append(f"<tr><td>Practical {label}</td><td>avg {b.get('avg')} · p95 {b.get('p95')} · "
                        f"max {b.get('max')} (n {b.get('n')})</td></tr>")
    if not rows:
        return ""
    return (f"<h2>Practical ({de})</h2><table><tr><th>View</th><th>Value</th></tr>" + "".join(rows)
            + "</table>" + ("<p class='muted'>Content-weighted = the error content sees (evidence; no gate "
                            "reads it).</p>" if cw else ""))


def _render_report_html(p: dict[str, Any]) -> str:
    v = p.get("verification") or {}
    lut = p.get("lut3d") or {}
    analysis = p.get("display_analysis")

    # HDR scores dE_ITP, SDR CIEDE2000 — label the deliverable report with the run's actual
    # metric (the verify digest carries it) so dE_ITP numbers are never shown as "dE2000".
    metric = v.get("metric", "CIEDE2000")
    de = "dE_ITP" if metric == "dE_ITP" else "dE2000"
    # The 3D-LUT line shows the optimizer's BEST residual in the same report metric (the cube
    # converges in dE_ITP but SDR surfaces CIEDE2000). Fall back to the run metric for old summaries.
    lut_metric = lut.get("metric") or metric
    lut_de = "dE_ITP" if lut_metric == "dE_ITP" else "dE2000"

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
        f"(max {lut_de} {lut.get('best_max_de_report', lut.get('best_max_de', '—'))})</p>"
        + _render_practical_html(v.get("practical"), de)
        + f"<h2>Verification ({metric})</h2><table><tr><th>Metric</th><th>After</th></tr>"
        + metric_row(f"Average {de}", "avg_de2000")
        + metric_row(f"P95 {de}", "p95_de2000")
        + metric_row(f"Max {de}", "max_de2000")
        + metric_row(f"White {de}", "white_de2000")
        + metric_row(f"Grayscale avg {de}", "grayscale_avg_de2000")
        + (f"<tr><td>Per-signal avg {de} ({v.get('n_signals')} signals / {v.get('n_reads')} reads)</td>"
           f"<td>{v.get('per_signal_avg')}</td></tr>" if v.get("per_signal_avg") is not None else "")
        + _render_preset_row(v.get("preset_set"), de)
        + _render_held_out_rows(v.get("held_out"), de)
        + _render_scored_white_row(v.get("sdr_white"))
        + "</table>"
        + _render_vs_source_html(v.get("vs_source"), de)
        + _render_thermal_html(p.get("thermal_state"))
        + analysis_block
    )


def _render_thermal_html(ts: Optional[Mapping[str, Any]]) -> str:
    """--thermal-state viewing: which stage ran in which thermal state (requested vs achieved, model)."""
    if not ts or not ts.get("stages"):
        return ""
    rows = "".join(
        f"<tr><td>{r.get('stage')}</td><td>{r.get('policy')}</td><td>{r.get('state')}"
        + (f" <span class='warn'>({', '.join(r['evidence_flags'])})</span>" if r.get("evidence_flags") else "")
        + "</td></tr>" for r in ts["stages"])
    mw = ts.get("mhc_white") or {}
    white = ("" if not mw else
             f"<p class='{'warn' if mw.get('evidence_flags') else 'ok'}'>MHC white refined in: {mw.get('state')}"
             + (f" ({', '.join(mw['evidence_flags'])})" if mw.get("evidence_flags") else "") + " (model)</p>"
             + (f"<p class='muted'>{mw['line']}</p>" if mw.get("line") else ""))
    return ("<h2>Thermal state (viewing requested)</h2>"
            f"<p class='muted'>{ts.get('owner_policy')}</p>"
            "<table><tr><th>Stage</th><th>Policy</th><th>State</th></tr>" + rows + "</table>" + white)


def _render_preset_row(preset: Optional[Mapping[str, Any]], de: str) -> str:
    """The preset verify set without the fresh draws — the run-to-run comparable average."""
    if not preset or not preset.get("available") or not preset.get("draw_reads_excluded"):
        return ""
    return (f"<tr><td>Preset set avg {de} (without the {preset.get('draw_reads_excluded')} draw reads; "
            f"per-signal {preset.get('per_signal_avg')})</td><td>{preset.get('avg_de2000')}</td></tr>")


def _render_held_out_rows(held: Optional[Mapping[str, Any]], de: str) -> str:
    """V1: the held-out / coincident per-signal avgs (or why there is no classification)."""
    if not held:
        return ""
    if not held.get("available"):
        return f"<tr><td>Held-out {de}</td><td class='muted'>n/a — {held.get('reason')}</td></tr>"
    rows = []
    for key, label in (("held_out", "Held-out"), ("strict_held_out", "Held-out, off-lattice"),
                       ("coincident", "Coincident with training"), ("fresh_draws", "Fresh draws")):
        stats = held.get(key) or {}
        if stats.get("n"):
            rows.append(f"<tr><td>{label} avg {de} (n {stats.get('n')})</td><td>{stats.get('avg')}</td></tr>")
    return "".join(rows)


def _render_scored_white_row(sdr_white: Optional[Mapping[str, Any]]) -> str:
    """V3: the white the SDR ΔE is relative to."""
    if not sdr_white or sdr_white.get("scored_white_nits") is None:
        return ""
    src = sdr_white.get("scored_white_source") or {}
    pct = sdr_white.get("white_luminance_vs_calibrated_pct")
    return (f"<tr><td>Scored relative to white (nits)</td><td>{sdr_white.get('scored_white_nits')} "
            f"({src.get('kind')}, {src.get('n_white_reads')} white reads"
            + (f"; {pct:+.2f}% vs calibrated {sdr_white.get('calibrated_white_nits')}" if pct is not None else "")
            + ")</td></tr>")


def _render_vs_source_html(vs: Optional[Mapping[str, Any]], de: str) -> str:
    """verify-only ``--verify-patches-from``: the per-bucket deltas vs the source run's recorded
    verify (now − source; negative = better)."""
    if not vs:
        return ""
    rows: list[str] = []
    for key, row in (vs.get("headline") or {}).items():
        rows.append(f"<tr><td>{key.replace('_de2000', '')}</td><td>{row.get('source')}</td>"
                    f"<td>{row.get('now')}</td><td>{row.get('delta')}</td></tr>")
    for group in ("buckets", "bands"):
        for name, stats in (vs.get(group) or {}).items():
            avg = stats.get("avg") or {}
            n = stats.get("n") or {}
            rows.append(f"<tr><td>{name} avg (n {n.get('source')}→{n.get('now')})</td>"
                        f"<td>{avg.get('source')}</td><td>{avg.get('now')}</td><td>{avg.get('delta')}</td></tr>")
    for group, prefix in (("per_signal", "per-signal"), ("held_out", "held-out partition")):
        block = vs.get(group) or {}
        if not block.get("available"):
            continue
        for name, stats in block.items():
            if not isinstance(stats, Mapping) or "avg" not in stats:
                continue
            avg = stats.get("avg") or {}
            n = stats.get("n") or {}
            rows.append(f"<tr><td>{prefix} {name} avg (n {n.get('source')}→{n.get('now')})</td>"
                        f"<td>{avg.get('source')}</td><td>{avg.get('now')}</td><td>{avg.get('delta')}</td></tr>")
    comp = vs.get("comparability") or {}
    note = ("like-for-like scoring basis" if comp.get("like_for_like")
            else "scoring basis differs: " + "; ".join(comp.get("differences") or []))
    return (f"<h2>vs source run <code>{vs.get('source_run')}</code> ({de}, now − source)</h2>"
            f"<p class='{'ok' if comp.get('like_for_like') else 'warn'}'>{note}</p>"
            "<table><tr><th>Bucket</th><th>Source</th><th>Now</th><th>Δ</th></tr>"
            + "".join(rows) + "</table>")


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
    mhc_top_hold: bool = True,
    white_band: Optional[tuple[float, float]] = None,
    source_run: Optional[Path] = None,
    verify_cube: Optional[Path] = None,
    verify_patches_from: Optional[Path] = None,
    verify_patches_file: Optional[Path] = None,
    verify_patches_order: Optional[str] = None,
    content_distribution: Optional[Sequence[str]] = None,
    score_black_floor_nits: Optional[float] = None,
    preheat: Optional[str] = None,
    present_stall: Optional[str] = None,
    content_mode: Optional[str] = None,
    keep_layers: Optional[Sequence[str]] = None,
    thermal_state: Optional[str] = None,
    viewing_load_nits: Optional[float] = None,
    viewing_start_nits: Optional[float] = None,
    viewing_hold_budget_min: Optional[float] = None,
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
        require_hardware_readiness=require_hardware_readiness,
        mhc_top_hold=mhc_top_hold, white_band=white_band, source_run=source_run,
        verify_cube=verify_cube, verify_patches_from=verify_patches_from,
        verify_patches_file=verify_patches_file, verify_patches_order=verify_patches_order,
        content_distribution=content_distribution, score_black_floor_nits=score_black_floor_nits,
        preheat=preheat, present_stall=present_stall, content_mode=content_mode, keep_layers=keep_layers,
        thermal_state=thermal_state, viewing_load_nits=viewing_load_nits,
        viewing_start_nits=viewing_start_nits, viewing_hold_budget_min=viewing_hold_budget_min)
    return calib.run(flow)


def parse_decide_flag(spec: str) -> tuple[str, Decision]:
    """Parse one ``--decide`` value: ``KEY=CHOICE`` or ``KEY=CHOICE=REASON``. The optional
    free-text REASON lands in the decision's ``note`` — the audit trail the run record,
    the seam event, and the report's panel analysis all carry (fable Phase 8: the LLM
    should record *why* it decided, not just what). No reason ⇒ the ``"cli"`` marker."""
    key, _, rest = spec.partition("=")
    choice, _, reason = rest.partition("=")
    return key.strip(), Decision(choice.strip(), note=(reason.strip() or "cli"))


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
    parser.add_argument("--source-run", type=Path, default=None, dest="source_run",
                        help="refine-mhc flow: the COMPLETED run whose MHC is re-refined and whose "
                             "3D LUT is kept (read-only; the refine runs in a new run dir)")
    parser.add_argument("--refine-cube", choices=("source", "installed"), default=None, dest="refine_cube",
                        help="refine-mhc flow: the 3D LUT the re-refined MHC keeps — 'source' (default: the "
                             "source run's build) or 'installed' (the cube installed now per the stack "
                             "registry, e.g. from a later 3dlut-only run; refused unless the installed MHC "
                             "is the source's own or a refine-mhc seeded from it)")
    def _white_band_arg(text: str) -> tuple[float, float]:
        try:
            return cp.parse_white_nits_band(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    parser.add_argument("--white-band", type=_white_band_arg, default=None, dest="white_band",
                        metavar="LO,HI",
                        help="SDR white-luminance band in nits for the MHC grayscale refine (overrides "
                             "the target's white_nits_band; default 11/12..1 x the nominal white): the "
                             "refine dims white inside it just enough for an exact target white")
    parser.add_argument("--verify-cube", type=Path, default=None, dest="verify_cube", metavar="CUBE",
                        help="verify-only flow: temporarily install this candidate 3D LUT for the run (the "
                             "prior runtime cube is captured and put back on restore / abort / cancel); "
                             "the verify:candidate seam decides restore (recommended) or keep (stays live, "
                             "recorded in the stack registry)")
    parser.add_argument("--verify-patches-from", type=Path, default=None, dest="verify_patches_from",
                        metavar="RUN_DIR",
                        help="verify-only flow: re-measure EXACTLY this recorded run's verify patch list "
                             "(read-only) and score it under that run's basis; the verify digest + report "
                             "carry per-bucket deltas vs its recorded verify. A source whose mode / bit "
                             "depth / display / target / correction differs is a seam")
    parser.add_argument("--verify-patches-file", type=Path, default=None, dest="verify_patches_file",
                        metavar="JSON",
                        help="verify-only flow: measure THIS verify list (a JSON file: 'codes' at 'bit_depth', "
                             "'content_mode', optional per-patch 'content_weight' / meta[i].content_weight — e.g. a "
                             "content-sampled set). Refused when its content mode / bit depth differ from the run's, "
                             "a code exceeds max_cv or (HDR) the target-peak patch cap. Codes + fingerprint are "
                             "memoised (a resume measures the identical list); per-patch weights yield the "
                             "content-weighted score (evidence, no gate). With --verify-patches-from only when "
                             "the source measured this exact list")
    parser.add_argument("--verify-patches-order", choices=verify_only.PATCH_FILE_ORDERS, default=None,
                        dest="verify_patches_order",
                        help="with --verify-patches-file: the measurement order. file (default) = exactly as "
                             "listed (a designed sequence is never re-shuffled); thermal / luminance / random = "
                             "re-sort with dlc.engine.patches.sort_patches. Recorded in the digest")
    parser.add_argument("--content-distribution", action="append", default=None, dest="content_distribution",
                        metavar="PATH[#VARIANT]",
                        help="any flow: a content distribution (the library survey's content_hist_<class>.npz or "
                             "its JSON export; USER DATA, local path) the verify's content-weighted practical "
                             "score is computed against (kernel score + coverage gap at R=20 dE_ITP, per class; "
                             "repeatable). Default: the profile's content_distribution key for the content mode. "
                             "Evidence only — it leads the practical block; no gate reads it")
    parser.add_argument("--score-black-floor-nits", type=float, default=None, dest="score_black_floor_nits",
                        metavar="NITS",
                        help="HDR verify: the display floor (nit) of the content-weighted score's BLACK-AWARE "
                             "variant: an additive raised black allowed as a panel limit, like out-of-gamut "
                             "colours (every patch scored against the nearest point of [target, target + floor x "
                             "the native white's colour]); the raw score stays recorded. Default: the native "
                             "near-black floor fitted from a RAW stage (this run's, else an identity-matched "
                             "recorded run's: the intercept of measured = F + g x target over its lowest lit "
                             "greys), else the DIP's characterized native black (a FULL-FIELD black read, which "
                             "understates the in-content floor on local-dimming panels: PA32UCXR 0). Evidence "
                             "only: no calibration target, cube or gate changes")
    parser.add_argument("--preheat", choices=("auto", "always", "never"), default=None, dest="preheat",
                        help="thermal preheat policy for every measure stage (the closed-loop soak before "
                             "the main pass). auto (default = today's behaviour): soak any characterized "
                             "panel, self-deactivating when already warm; always: soak even without a DIP; "
                             "never: skip it (a short verify on a panel already at operating temperature). "
                             "Persisted in the run record (a flagless resume keeps it; each measure digest "
                             "records the policy it ran under)")
    parser.add_argument("--thermal-state", choices=viewing_thermal.THERMAL_STATES, default=None,
                        dest="thermal_state",
                        help="the thermal state the MHC refine + VERIFY are measured in. verify (default = "
                             "today's behaviour): the preheat soaks at the set's own (meter test) load. viewing "
                             "(owner decision 2026-10-09): the MHC closed-loop refine and the verify each get a "
                             "seam offering a viewing-load precondition (dim neutral stand-in until the MODELLED "
                             "panel state is inside the content band, ~50 min from a hot panel); the refine's "
                             "reads are SETTLED to the target and HELD there by dim-neutral dwells between ~45 s "
                             "blocks (budget: --viewing-hold-budget-min); the verify keeps a patch file's balanced "
                             "order with no bright filler; raw + the cube build stay at their own (loaded) band. "
                             "Recorded in the run record, every digest and the report. Locked once the MHC refine "
                             "is memoised (its white was refined in that state)")
    parser.add_argument("--viewing-load-nits", type=float, default=None, dest="viewing_load_nits",
                        help="with --thermal-state viewing: the viewing target as a nit-equivalent load "
                             "(default: the content survey's 13.5 HDR / 10.1 SDR; must be > 0 and at most the "
                             "recorded verify band's ~65 nit-eq). Changeable on a resume until measure:verify "
                             "is measured (recorded in thermal_state_changes)")
    parser.add_argument("--viewing-start-nits", type=float, default=None, dest="viewing_start_nits",
                        help="with --thermal-state viewing: the nit-equivalent load the panel showed before the "
                             "precondition (default: this run's load history when nothing unmodelled drove the "
                             "display since, else ASSUMED HOT = the recorded verify band ~65 nit-eq). Give it on "
                             "the resume that answers the thermal-state seam to correct the assumption "
                             "(changeable until measure:verify is measured). It answers ONE seam — the next "
                             "viewing seam to ask (the MHC refine's, then the verify's): a later seam, or that "
                             "seam's re-ask after a remeasure, starts from the run history; a new value re-arms it")
    parser.add_argument("--viewing-hold-budget-min", type=float, default=None, dest="viewing_hold_budget_min",
                        help="with --thermal-state viewing: the total dim-neutral DWELL (minutes) the MHC "
                             "refine's hold may add across all its rounds (default: shown at the refine's "
                             "thermal-state seam = the model's predicted dwell for a typical refine x 1.5 + 5). "
                             "Past it the refine rides unheld and is flagged if it leaves the band or reads off "
                             "its target (counted in real elapsed time; a dwell read starts only if a "
                             "conservative bound on it still fits). Changeable on the resume that answers that seam")
    parser.add_argument("--present-stall", choices=("on", "off"), default=None, dest="present_stall",
                        help="the stuck-frame (present-stall) run-stopper for every measure stage (default on). "
                             "off = an LLM decision for a drive sweep whose distinct commands legitimately read "
                             "identical XYZ (e.g. minor channels the MHC clips to zero). Persisted in the run "
                             "record; each measure digest names it")
    parser.add_argument("--content-mode", type=str.upper, choices=("SDR", "HDR"), default=None, dest="content_mode",
                        help="verify-only: the mode of the CONTENT when it differs from the display's --mode. "
                             "--mode HDR --content-mode SDR measures SDR content (8-bit codes, dogegen mode 8, the "
                             "SDR Rec.709 / gamma target) on a display in HDR — composited by Windows at its SDR "
                             "white level, through the installed HDR stack. Persisted in the run record")
    parser.add_argument("--keep-layers", default=None, dest="keep_layers", metavar="LIST",
                        help="verify-only: comma list of viewing layers left exactly as the user has them for the "
                             "run (e.g. desktop_gamma); every other layer is off for the run and restored after. "
                             "Persisted in the run record")
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
    patch.add_argument("--verify-held-out-draws", type=int, default=None, dest="verify_held_out_draws",
                       help="fresh held-out verify colours drawn per run (seeded by the run id; >= 8 codes "
                            "from every training signal / build-probe drive, off-lattice). SDR only; "
                            "default 24, 0 = off.")
    patch.add_argument("--sat-sweep-levels", type=float, nargs="+", default=None,
                       dest="saturation_sweep_levels",
                       help="3D-LUT confidence skeleton levels as signal fractions (default 0.25 0.5 0.75 1.0).")
    patch.add_argument("--sat-sweep-repeats", type=int, default=None,
                       dest="saturation_sweep_repeats",
                       help="repeated reads per saturation-sweep bookend location "
                            "(default 3: 3 start reads + 3 end reads per skeleton signal).")
    patch.add_argument("--neutral-steps", type=int, default=None, dest="neutral_steps",
                       help="grey-axis ramp steps measured by the MHC D65 grayscale refine "
                            "(the correctionGrayscale closed loop; default 17).")
    patch.add_argument("--neutral-top-pins", type=int, default=None, dest="neutral_top_pins",
                       help="HDR refine: extra neutral pins placed data-driven where the base cube bends "
                            "most in the last --neutral-top-band of the range below the MHC cap (default 3; "
                            "0 = off). Each pin is one extra bright neutral read per refine round.")
    patch.add_argument("--neutral-top-band", type=float, default=None, dest="neutral_top_band",
                       help="HDR refine: the fraction of the signal range below the cap the top pins may "
                            "land in (default 0.10).")
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
    parser.add_argument("--decide", action="append", default=[], metavar="KEY=CHOICE[=REASON]",
                        help="record a seam decision (repeatable) then run/resume. The choice is "
                             "validated against the seam's declared options (an off-vocabulary "
                             "choice pauses the seam instead of silently misrouting). An optional "
                             "free-text =REASON is kept in the audit trail (run record + seam "
                             "event + report), e.g. --decide 'verify:accept=revert=white cast "
                             "visible on the desktop'.")
    parser.add_argument("--adaptive-planning", action="store_true", dest="adaptive_planning",
                        help="OPT-IN, EXPERIMENTAL (value unproven — a synthetic A/B found denser "
                             "sampling does not beat the optimizer's fold-back; see patch_evidence.py): "
                             "pause after the ICC and let the LLM investigate the panel/run (evidence "
                             "packet + `python -m dlc.patch_evidence` tools) and choose the patch "
                             "strategy. Autonomous (--auto) runs use a conservative fallback.")
    parser.add_argument("--thermal-align", choices=("auto", "none", "end", "start", "mid"), default="auto",
                        dest="thermal_align",
                        help="thermal-state alignment of the raw / post-MHC datasets to ONE reference state "
                             "before the build (plan item 3). auto (default): evidence packet every stage, "
                             "a SEAM (measure:<role>:thermal-align) when the interleaved reference's drift "
                             "is significant vs its own noise; end/start/mid: pre-decided (applied without "
                             "a pause, reported); none: evidence only, never rewrite.")
    parser.add_argument("--top-hold", choices=("on", "off"), default="on", dest="top_hold",
                        help="above the calibrated top (HDR: the MHC cap / patch cap), HOLD the last "
                             "confirmed correction in both layers (owner policy 2026-09-23, default on): the "
                             "MHC base cube keeps greys at the D65 cap white, the 3D LUT keeps each colour's "
                             "corrected hue with its luminance clipped at the top. off = the legacy "
                             "behaviour (shared MHC ceiling; 3D LUT fades to identity above the data).")
    parser.add_argument("--oog-solve", choices=("direct", "projection"), default="direct", dest="oog_solve",
                        help="how the 3D LUT solves nodes whose target lies outside the panel's gamut: direct "
                             "(default until a hardware verify accepts the alternative) inverts toward the "
                             "clamped target node by node; projection solves each at its gamut projection "
                             "(smooth, noise-safe lattice; in-gamut nodes identical) after checking that the "
                             "monitor decodes colorimetrically in-gamut (seam if not). A resumed run keeps the "
                             "mode it started with.")
    parser.add_argument("--hook-routing-check", choices=("auto", "always", "never"), default="auto",
                        dest="hook_routing_policy",
                        help="DWM-hook LUT routing self-check for cube flows (full / 3dlut-only). The hook "
                             "order-matches twin panels on 25H2 (2026-09-03: a whole run measured the "
                             "uncorrected panel). auto (default): prove the routing through the meter "
                             "(probe cube on a mid grey) only when the hook's report is ambiguous/"
                             "unconfirmed/absent; always: prove it every run; never: evidence only. No "
                             "effect after one twin swap REFUSES the run.")
    parser.add_argument("--plan-decision-file", type=Path, default=None, dest="plan_decision_file",
                        help="resume the adaptive-planning seam with a structured decision JSON file "
                             "(keys: shadow_treatment, volumetric_density, patch_size_overrides, "
                             "reason, confidence). Validated + clamped to bounds before it is applied.")
    # The adjudicator: one explicit, mutually-exclusive choice. --attended (== the default)
    # exists so the REAL-run mode has a flag of its own (fable Phase 8: the mode you want
    # for a hardware run was previously selectable only by NOT passing the other two —
    # an invisible default is a trap at the one switch that decides who judges the run).
    adj_group = parser.add_mutually_exclusive_group()
    adj_group.add_argument("--attended", action="store_true",
                           help="the DEFAULT (explicit form): every seam without a recorded "
                                "decision PAUSES for the LLM/operator (exit 10 + the request as "
                                "JSON; resume with --decide KEY=CHOICE). The real hardware-run "
                                "mode — use this flag to say so explicitly.")
    adj_group.add_argument("--auto", action="store_true",
                           help="auto-adjudicate EVERY seam by its recommendation (no pauses, no LLM) — "
                                "for sim/CI/reproducible runs, NOT an unattended hardware run "
                                "(refused on live measuring flows)")
    adj_group.add_argument("--supervised", action="store_true",
                           help="autonomous, but PAUSE for a live judge at safety-critical seams "
                                "(foundation collapse / optimizer floor / failed verify) — the mode for "
                                "an unattended HARDWARE run; a clean run never pauses. Benign defaults "
                                "are taken as VISIBLE, vetoable judgment packets on the digest "
                                "(seam status=auto_accepted, full request + veto lever), never silently.")
    parser.add_argument("--checkin-interval", type=float, default=600.0, dest="checkin_interval",
                        metavar="SECONDS",
                        help="§12 timed check-in floor: past this many seconds, the next safe "
                             "checkpoint EMITS a rich evidence packet (run overview + events since the "
                             "last check-in) for the LLM to consume from the running spine, so a long "
                             "run never goes dark. Default 600 (10 min). A check-in is emit-only — it "
                             "NEVER gates or pauses the spine and carries no recommendation (all modes). "
                             "0 disables — on --auto (sim/CI) ONLY: an LLM-adjudicated run "
                             "(--attended/--supervised) enforces the no-dark-window rule, clamping a "
                             "disabled or >1200 s interval to 1200 s (20 min).")
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
        mode = normalize_mode(args.content_mode or args.mode)      # the CONTENT picks target + depth
        bd = args.bit_depth if args.bit_depth is not None else (10 if mode == "HDR" else 8)
        target = profile.display_for(args.monitor).target_name(mode)
        out: dict[str, Any] = {"flow": args.flow, "monitor": args.monitor, "mode": normalize_mode(args.mode),
                               "patch_sizes": asdict(patch_sizes)}
        if args.content_mode:
            out["content_mode"] = mode
        out["patch_plan"] = (flow_patch_counts(args.flow, patch_sizes,
                                               profile.transfer_for(target, bit_depth=bd))
                             if target else {"note": f"no {mode} target for monitor {args.monitor}"})
        if args.flow == "verify-only" and args.verify_patches_from is not None:
            # The run re-measures the SOURCE's exact list — that is its size, not the preset's.
            try:
                src = verify_only.load_source_verify(Path(args.verify_patches_from))
                out["patch_plan"] = {"stages": {"verify": src["patch_count"]},
                                     "total_patches": src["patch_count"],
                                     "verify_source": {k: src.get(k) for k in
                                                       ("run", "mode", "bit_depth", "target", "display",
                                                        "patch_source", "patches_fingerprint")}}
            except verify_only.SourceRunError as exc:
                out["patch_plan"] = {"error": f"--verify-patches-from: {exc}", **exc.detail}
        if args.flow == "verify-only" and args.verify_patches_file is not None:
            # The run measures the FILE's list — that is its size.
            try:
                doc = verify_only.load_patches_file(Path(args.verify_patches_file))
                problems = verify_only.patches_file_problems(doc, content_mode=mode, bit_depth=bd)
                out["patch_plan"] = {"stages": {"verify": doc["n"]}, "total_patches": doc["n"],
                                     "verify_patches_file": {k: doc.get(k) for k in
                                                             ("path", "content_mode", "bit_depth", "content_class",
                                                              "patches_fingerprint")},
                                     **({"refused": problems} if problems else {})}
            except verify_only.PatchesFileError as exc:
                out["patch_plan"] = {"error": f"--verify-patches-file: {exc}", **exc.detail}
        print(json.dumps(out, indent=2))
        return 0

    verify_flags = (args.verify_cube is not None or args.verify_patches_from is not None
                    or args.verify_patches_file is not None)
    if args.verify_patches_order is not None and args.verify_patches_file is None \
            and not (args.run and (args.run / "manifest.json").exists()):
        print(json.dumps({"error": "--verify-patches-order belongs to --verify-patches-file"}))
        return 2
    if (verify_flags and args.flow != "verify-only"
            and not (args.run and (args.run / "manifest.json").exists())):
        print(json.dumps({"error": "--verify-cube / --verify-patches-from / --verify-patches-file belong to "
                                   "--flow verify-only"}))
        return 2
    if args.verify_patches_file is not None and not (args.run and (args.run / "manifest.json").exists()):
        # A fresh run: refuse a file it could never measure like-for-like BEFORE a run folder, dogegen or
        # the meter exist (the orchestrator re-checks; the HDR peak cap is checked at resolve-target).
        content = normalize_mode(args.content_mode or args.mode)
        bd = args.bit_depth if args.bit_depth is not None else (10 if content == "HDR" else 8)
        try:
            doc = verify_only.load_patches_file(Path(args.verify_patches_file))
        except verify_only.PatchesFileError as exc:
            print(json.dumps({"error": f"--verify-patches-file: {exc}", **exc.detail}))
            return 2
        problems = verify_only.patches_file_problems(doc, content_mode=content, bit_depth=bd)
        if problems:
            print(json.dumps({"error": "--verify-patches-file: " + "; ".join(problems), "refused": problems}))
            return 2
    # --keep-layers names and a fresh run's --content-mode coherence are refused BEFORE a run folder,
    # dogegen or the meter exist (a resume is re-checked against its persisted spec below).
    if args.keep_layers:
        unknown = sorted({n.strip().lower() for n in str(args.keep_layers).split(",") if n.strip()}
                         - set(CalibrationController.LAYER_NAMES))
        if unknown:
            print(json.dumps({"error": f"--keep-layers: unknown layer(s) {unknown}; known: "
                                       f"{list(CalibrationController.LAYER_NAMES)}"}))
            return 2
    if not (args.run and (args.run / "manifest.json").exists()):
        if (args.content_mode or args.keep_layers) and args.flow != "verify-only":
            print(json.dumps({"error": "--content-mode / --keep-layers belong to --flow verify-only"}))
            return 2
        if args.content_mode and normalize_mode(args.content_mode) != normalize_mode(args.mode) and not (
                normalize_mode(args.content_mode) == "SDR" and normalize_mode(args.mode) == "HDR"):
            print(json.dumps({"error": f"--content-mode {normalize_mode(args.content_mode)} on a "
                                       f"{normalize_mode(args.mode)} display: only SDR content on an HDR display "
                                       "exists (Windows composites SDR into HDR)"}))
            return 2
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
    # The CONTENT mode drives dogegen's mode + the patch depth (the display mode keeps the meter's
    # correction slot and the DIP); the persisted run record wins on a resume, as for mode.
    eff_content, _, _ = resolve_content_mode(state.get("calib") or {}, args.content_mode, eff_mode)
    if (args.content_mode or args.keep_layers) and eff_flow != "verify-only":
        print(json.dumps({"error": f"--content-mode / --keep-layers belong to --flow verify-only (this run's flow "
                                   f"is {eff_flow})"}))
        return 2
    if eff_content != eff_mode and not (eff_content == "SDR" and eff_mode == "HDR"):
        print(json.dumps({"error": f"--content-mode {eff_content} on a {eff_mode} display: only SDR content on an "
                                   "HDR display exists (Windows composites SDR into HDR)"}))
        return 2
    if verify_flags and eff_flow != "verify-only":
        print(json.dumps({"error": (f"--verify-cube / --verify-patches-from / --verify-patches-file belong to "
                                    f"--flow verify-only (this run's flow is {eff_flow}) — nothing else would "
                                    "use them")}))
        return 2
    recorded = (state.get("calib", {}) or {}).get("decisions", {})
    decisions = {k: Decision(v["choice"], v.get("note"), payload=v.get("payload"))
                 for k, v in recorded.items()}
    overrides: dict[str, Decision] = {}
    for spec in args.decide:
        key, decision = parse_decide_flag(spec)
        overrides[key] = decision
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
            "for hours on an unadjudicated foundation. It is sim/CI only. Run live with --attended (the "
            "default: every seam pauses for the LLM) or with --supervised, and use the in-process "
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
    from .link_format import probe_link_formats
    from .sdr_in_hdr import probe_sdr_white as _sdr_white_probe

    controller = CalibrationController.connect()

    # Explicit cancel: restore the user's pre-run setup and exit (no measurement stack needed).
    # `restored_snapshot` is the SERVER's restored flag, not "the call returned": a flow that never
    # entered (3dlut-only) or a DesktopLUT restarted mid-run holds no capture, and the payload says so.
    if args.abort:
        # verify-only's candidate cube is put back first, and a flow that never entered calibration
        # mode (verify-only / 3dlut-only / grayscale-wb) never asks DesktopLUT for a snapshot restore:
        # any snapshot it holds would be someone else's — on a build predating the snapshot store its
        # stale LAST slot, i.e. a completed earlier calibration's pre-run setup (see _abort_restore).
        code, payload = _abort_restore(controller, state.get("calib"), monitor=args.monitor,
                                       mode=args.mode, run_root=ctx.root, flow=eff_flow, log=ctx.log)
        if payload.get("verify_candidate") is not None:
            _common.save_dlc_state(ctx, state)   # restore_candidate updated calib['verify_candidate']
        print(json.dumps(payload, indent=2, default=str))
        return code

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
    bit_depth = _eff_bd if _eff_bd is not None else (10 if eff_content == "HDR" else 8)
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
            port, port_evidence = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
            # A failed/empty enumeration silently falls back to the planned port; record it, so
            # a spotread that later cannot open the instrument has its first clue in the run dir.
            if not port_evidence.get("ok", True) or port_evidence.get("changed"):
                ctx.log("meter port resolution: " + json.dumps(port_evidence, default=str))
        else:
            port = profile.meter.argyll_port
        # Per-patch presenter dwell: prefer the panel's MEASURED step-response settle (from the
        # DIP) over the guessed 0.5 s — a fast panel runs leaner, a slow mini-LED waits long
        # enough. EXCEPT during `characterize` itself, which must observe the raw step response
        # (waiting out a prior settle estimate would hide it), so it keeps the paint-safe default.
        dip_rec = dip_record_for(DipStore.load(dip_store_path(profile, ctx.root)),
                                 profile.display_for(args.monitor).name, eff_mode)
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
            # The daemon's mode is set out of band: a daemon in another mode / depth would show these
            # codes as another signal (an HDR daemon renders SDR 8-bit codes as PQ) — refuse, mechanically.
            try:
                daemon_mode = presenter.query_mode()
            except Exception as exc:  # noqa: BLE001 - an unreachable daemon fails at the first patch anyway
                daemon_mode = None
                ctx.log(f"dogegen daemon mode query failed ({type(exc).__name__}: {exc})")
            if daemon_mode is None:
                ctx.log("dogegen daemon did not report its mode (an older daemon?) — make sure it runs "
                        f"--mode {eff_content} --bit-depth {bit_depth}")
            elif (daemon_mode["mode"], daemon_mode["bit_depth"]) != (eff_content, int(bit_depth)):
                presenter.close()
                print(json.dumps({"error": (
                    f"the dogegen daemon runs {daemon_mode['mode']} {daemon_mode['bit_depth']}-bit but this run "
                    f"presents {eff_content} {bit_depth}-bit codes — restart it: python -m dlc.dogegen_server "
                    f"--mode {eff_content} --bit-depth {bit_depth} --monitor {args.monitor}")}))
                return 2
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
            presenter = DogegenPresenter(DogegenPatchDisplay(Path(dogegen_path), eff_content,
                                                             bit_depth=bit_depth),
                                         settle_seconds=presenter_settle, place_rect=place_rect)
        # The active correction comes from the store first (a freshly probe-matched .ccmx)
        # then the profile — so a build-correction run is picked up without editing the YAML.
        store = CorrectionStore.load(correction_store_path(profile, ctx.root))
        correction = active_correction(profile, store, profile.display_for(args.monitor).name, eff_mode)
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

    result = None
    paused = False
    calib = None   # bound inside the try; the finally checks `is not None` (ctor may raise)
    try:
        # Constructed INSIDE the teardown guard (fable Phase 7a): the persistent spotread child
        # + presenter were opened above, and the ctor can raise (a corrupt dlc_state.json fails
        # its bare json.loads on resume) — outside this try that orphaned the spotread process
        # and the dogegen window with no rollback.
        #
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
                            neutral_floor_min_nits=args.neutral_floor_min_nits,
                            thermal_align=args.thermal_align,
                            hook_routing_policy=args.hook_routing_policy,
                            mhc_top_hold=(args.top_hold == "on"),
                            white_band=args.white_band,
                            source_run=args.source_run,
                            verify_cube=args.verify_cube,
                            verify_patches_from=args.verify_patches_from,
                            verify_patches_file=args.verify_patches_file,
                            verify_patches_order=args.verify_patches_order,
                            content_distribution=args.content_distribution,
                            score_black_floor_nits=args.score_black_floor_nits,
                            preheat=args.preheat,
                            thermal_state=args.thermal_state,
                            viewing_load_nits=args.viewing_load_nits,
                            viewing_start_nits=args.viewing_start_nits,
                            viewing_hold_budget_min=args.viewing_hold_budget_min,
                            present_stall=args.present_stall,
                            refine_cube=args.refine_cube,
                            content_mode=args.content_mode,
                            keep_layers=([n for n in str(args.keep_layers).split(",") if n.strip()]
                                         if args.keep_layers else None),
                            sdr_white_probe=_sdr_white_probe,
                            link_probe=probe_link_formats,
                            optimize_config=OptimizeConfig(top_hold=(args.top_hold == "on"),
                                                           oog_solve=args.oog_solve))
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
            # Persist any spotread deaths (with spotread's own dying output) + self-heal respawns
            # to the run's workflow.log — the read paths outside the measure loop (characterize,
            # probes, brightness) have no event of their own for them, and before this the error
            # text of a dead meter was recorded nowhere (2026-09-23 incident).
            # Also its stream-sync counters (timeouts, late results discarded at resync, resync
            # respawns, stale / extra readings) — evidence the reads stayed matched to their patches.
            try:
                meter_summary = persistent_meter.summary()
                if any(meter_summary.values()):
                    ctx.log("persistent meter: " + json.dumps(
                        {**meter_summary, "death_log": persistent_meter.death_log}, default=str))
            except Exception:  # noqa: BLE001 - diagnostics only, never break teardown
                pass
        # Rollback guard: a clean run reaches a 'completed' (applied), 'reverted', or
        # 'revert_unavailable' terminal state — all of which _finish already settled (commit,
        # snapshot restore, in-place cube restore, or an honest surface of the manual backup
        # when the in-place MHC tweak can't be undone over the pipe). Anything else on a
        # non-paused exit — an abort or an unexpected exception — means we may have left a
        # half-applied profile, so roll DesktopLUT back to the user's pre-run snapshot.
        if not paused:
            handled = result is not None and getattr(result, "status", None) in (
                "completed", "reverted", "reverted_partially", "revert_unavailable", "revert_unconfirmed")
            # Nothing to roll back if the run never entered calibration mode / never mutated the
            # display — e.g. a clean early-fail at the pipe/plan/backup seam (finding F7a-A8).
            # Skipping avoids a spurious `rollback_failed` (exit_calibration over the very pipe
            # that was down) on a run the seam itself advertised as "nothing measured or mutated".
            entered = False
            try:
                entered = calib is not None and (
                    calib._entered_calibration()
                    or calib.calib.get("inplace_baseline") is not None)
            except Exception:  # noqa: BLE001 - defensive; fall back to attempting rollback
                entered = True
            if not handled and entered:
                try:
                    # Reports what the SERVER says it restored — a run that never entered
                    # calibration mode (3dlut-only) or a DesktopLUT restarted mid-run gets
                    # restored:false, and that must not print as "restored pre-run setup".
                    rollback = _rollback_restore(
                        controller, calib.calib if calib is not None else None,
                        monitor=args.monitor, mode=args.mode, run_root=ctx.root,
                        entered_calibration=_run_entered_calibration(calib.calib if calib is not None else None,
                                                                     flow=eff_flow))
                    layers = rollback.get("viewing_layers")
                    if calib is not None:
                        try:
                            calib.calib["snapshot_restore"] = {"why": "rollback guard",
                                                               **rollback["snapshot_restore"]}
                            vl = calib.calib.get("viewing_layers")
                            if layers is not None and isinstance(vl, dict):
                                vl["reasserted_after_rollback"] = layers
                            calib._save()
                            if layers is not None:
                                ctx.log("viewing layers after rollback: " + json.dumps(layers, default=str))
                            ctx.log("rollback: " + rollback["snapshot_restore"]["summary"])
                        except Exception:  # noqa: BLE001 - bookkeeping only
                            pass
                    print(json.dumps(rollback, indent=2, default=str))
                except Exception as exc:  # noqa: BLE001 - but a FAILED rollback must never be silent
                    # The one teardown failure that can cost the user their display setup: the
                    # run died half-applied AND the snapshot restore failed. Say so, and point
                    # at the durable backup captured at preflight, instead of exiting mute.
                    bak = (state.get("calib", {}) or {}).get("backup", {})
                    print(json.dumps({
                        "status": "rollback_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "run": str(ctx.root),
                        "backup": bak,
                        "hint": "restore manually: `dlc-calibrate --abort --run <dir>` once the "
                                "pipe is back, or re-import the settings backup from the run dir",
                    }, indent=2))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
