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
    SEAM_FOUNDATION,
    SEAM_HARDWARE_READY,
    SEAM_MEASURE,
    SEAM_MONITOR_MAP,
    SEAM_OPTIMIZE,
    SEAM_PIPE,
    SEAM_PLAN,
    SEAM_PLANNING,
    SEAM_PROBE_MATCH,
    SEAM_SPD,
    SEAM_STACK,
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
from .characterize import CharacterizeConfig, run_characterization
from .controller import CalibrationController, normalize_mode
from .desktoplut_client import contract_version_mismatch
from .correction_store import CorrectionRecord, CorrectionStore
from . import gamut
from .dip import DipStore, DisplayInstrumentProfile
from .engine.patches import Transfer
from .events import Ev, EventWriter, RunLog
from .keep_awake import keep_awake
from .liveness import Liveness, RunCancelled, RunStalled
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
from . import neutral_audit
from . import stack_registry
from . import thermal_align
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
    build_verify_set,
    build_volumetric_set,
    flow_patch_counts,
    outside_in_indices,
)
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
        # Thermal-state alignment policy for the raw / post-MHC datasets (plan item 3):
        # 'auto' = evidence every stage, SEAM when the reference track's drift is significant;
        # 'end'/'start'/'mid' = pre-decided (applied, reported, no pause); 'none' = evidence only.
        self.thermal_align = (thermal_align or "auto").lower()
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
        self._last_checkin_monotonic: Optional[float] = None
        self._last_checkin_tally: dict[str, int] = {}
        self._last_checkin_pos: int = 0   # events.jsonl byte offset at the last check-in (evidence window)
        self._run_started_monotonic: Optional[float] = None
        # Latest live metrics, snapshotted as they happen, so a check-in carries them without
        # re-deriving from artifacts: the most recent intermediate score + the last optimizer iter.
        self._last_scored: dict[str, Any] = {}
        self._last_optimizer: dict[str, Any] = {}
        self._last_refine: dict[str, Any] = {}
        self._last_bookend_drift: dict[str, Any] = {}

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
            outcome.replayed = True   # post-stage telemetry keys off this (no re-emit on resume)
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
                (self.calib["stages"].get("build-install-3dlut") or {}).get("digest") or {}).get("cube_path")
            params = self._state.get("mhc_params") or {}
            if flow in ("full", "mhc-only") and params:
                rec = stack_registry.record_from_mhc_params(
                    display=self.display.name, mode=self.mode, monitor=self.monitor,
                    run_id=self.ctx.root.name, profile_name=pipe_profile, mhc_params=params,
                    target_white_xy=self._white_xy())
                if cube:
                    rec.cube = {"cube_path": cube, "run_id": self.ctx.root.name,
                                "applied_at": rec.applied_at}
                reg.record(rec)
                self.ctx.log(f"applied-stack registry: {rec.key} <- run {rec.run_id} "
                             f"(profile {pipe_profile}, top {rec.cube_peak_nits})")
            elif flow == "3dlut-only":
                rec = reg.record_cube(display=self.display.name, mode=self.mode, monitor=self.monitor,
                                      run_id=self.ctx.root.name, cube_path=cube, profile_name=pipe_profile)
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

        Production use is HDR/wide-gamut only. The SDR clamp experiment was CV-gated worse, so SDR
        returns ``None`` and stays on the plain sRGB scoring/build target."""
        if self.mode != "HDR":
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

    _FLOWS_KEEPING_MHC = ("3dlut-only", "grayscale-wb")
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
            **self._plausibility_context(role, dip),
        )

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
                          patches: Sequence[tuple[int, int, int]]) -> Optional[dict[str, Any]]:
        """Compare start-vs-end saturation-sweep bookends before RBF aggregation.

        The bookends serve two jobs: repeated reads become high-confidence RBF knots, but
        the start/end split is also a temporal drift witness. This helper consumes the
        ordered measured rows while that temporal information still exists, emits a digest
        packet/anomaly for the LLM, and only then downstream optimization may average the
        duplicates by signal.
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

        groups: dict[tuple[float, float, float], dict[str, list[tuple[float, float, float]]]] = {}
        order: list[tuple[float, float, float]] = []
        for s0, s1 in zip(start, end):
            k = key(s0.rgb)
            if k not in groups:
                groups[k] = {"start": [], "end": []}
                order.append(k)
            groups[k]["start"].append(s0.xyz)
            groups[k]["end"].append(s1.xyz)

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
                    message=("start/end saturation-sweep bookends drifted beyond the "
                             f"{_BOOKEND_DRIFT_ANOMALY_DE:g} {metric_name} threshold"))
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
            store_rec = corr_store.get(self.display.name)
            store_made = store_rec.correction_made if store_rec else None
            # Consult the SAME correction the meter is actually wired to (store overrides the
            # profile YAML — active_correction), not the (possibly empty) profile YAML, so the
            # tell can't report "no correction" while the meter is in fact corrected.
            self.runlog.note("preflight", "checking colorimeter correction (CCMX/SPD) freshness")
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
                                     "dropped": list(corr_store.dropped)},
                "dip_store": {"corrupt": dip_store.corrupt,
                              "dropped": list(dip_store.dropped)},
            }
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
                      "correction_from_store": store_made is not None,
                      "patch_window": patch_window,
                      "transport": transport,
                      "gamut": gamut_tell,
                      "panel_limits": panel_limits,
                      "dip": dip_status,
                      "store_health": store_health,
                      "backup": backup}
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

    def _reject_mode_target_mismatch(self, stage: str, target: str, spec: cp.TargetSpec) -> None:
        """P12 guard (fable Phase 7a): the run MODE and the resolved target's transfer must
        agree. The refine fork in ``_flow_full``/``_flow_mhc_only`` switches on ``spec.is_hdr``
        while ``_planned_stages`` (the dashboard stepper) and ``_reachable_primaries`` switch on
        ``self.mode`` — a profile that maps a display's SDR slot to a PQ target (or vice versa)
        would otherwise run an incoherent hybrid (HDR refine + SDR gamut clamp, a stepper showing
        the other mode's stages) with nothing surfacing why. Reject it loudly at resolve time —
        the two predicates are then provably interchangeable for the rest of the run."""
        if spec.is_hdr != (self.mode == "HDR"):
            raise CalibrationAborted(StageOutcome(
                stage, "aborted",
                digest={"message": (
                    f"target {target!r} is a {'PQ/HDR' if spec.is_hdr else 'power-law/SDR'} target "
                    f"but the run mode is {self.mode} — the profile maps display {self.monitor}'s "
                    f"{self.mode} slot ({'hdr_target' if self.mode == 'HDR' else 'sdr_target'}) to a "
                    f"mismatched target. Fix the profile before running."),
                    "target": target, "target_is_hdr": spec.is_hdr, "run_mode": self.mode}))

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
        self._reject_mode_target_mismatch("resolve-target", target, spec)
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
        nits_label = f"{target_nits:g} nit peak" if spec.is_hdr else f"{target_nits:g} nits"
        digest = {"flow": flow, "target": target,
                  "colorspace": spec.colorspace, "transfer": transfer_label,
                  "white": f"{spec.white.intent} ({spec.white.method})",
                  "white_nits": target_nits, "patch_plan": self.calib["patch_plan"]}
        plan_warnings: list[str] = []
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
            if (prov.get("peak") or {}).get("sustained_unknown"):
                plan_warnings.append(
                    "the target peak rests on no warm/sustained capture "
                    f"({(prov.get('peak') or {}).get('source', 'unknown source')}) — a peak the "
                    "panel cannot hold bakes in error; a characterize run grounds it")
            if flow in self._FLOWS_KEEPING_MHC:
                stack = self.calib.get("installed_stack") or {}
                digest["installed_stack"] = stack
                if not stack.get("pin_nits"):
                    plan_warnings.append(
                        "the installed MHC's calibrated top is UNKNOWN ("
                        + str(stack.get("reason") or "no registry evidence")
                        + f") — the target peak falls back to {hdr.peak_nits:.0f} nits; if the "
                        "installed MHC caps D65 lower, every patch above its cap reads as a "
                        "plateau the cube cannot lift and the white scores that shortfall. "
                        "Backfill with `python -m dlc.stack_registry import-run --run <applying "
                        "run> --profile-name <pipe profile>` and restart, or approve knowingly")
        if plan_warnings:
            digest["hdr_target_warnings"] = plan_warnings
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key="resolve-target:plan", seam=SEAM_PLAN, stage="resolve-target",
            question=(f"Plan: {flow} calibration of monitor {self.monitor} "
                      f"({self.display.name}) to target '{target}' "
                      f"({transfer_label}, {spec.white.intent}, {nits_label}) — "
                      f"{patch_plan['total_patches']} patches "
                      f"({patch_plan['volumetric_mode']} volumetric, {patch_plan['order']} order). "
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
        """Put the calibrated monitor/mode into a TRUE neutral before the first raw read:
        ``calibration.enter`` (the C++ clears WB / GS / tonemap / Desktop Gamma and removes the
        ICM) FOLLOWED BY the association of an identity MHC2 profile through the normal path
        (:meth:`_associate_identity_profile`) — because Windows keeps the LAST associated MHC2
        transform after a removal, ``enter`` alone left every pre-2026-09-03 raw stage
        measuring through the previously applied stack. The digest + ``calib['neutral_profile']``
        carry ``{monitor, mode, P, P_source, profile_name}``; the stage refuses (:class:`StageError`)
        when the association does not land. HDR and SDR alike."""
        def run() -> StageOutcome:
            # Stale-calibration-mode tell (fable Phase 9): if a PREVIOUS run died without
            # exiting calibration mode, the C++ DoEnterNeutral re-snapshots unconditionally —
            # its single restore slot then holds the already-CLEARED state, so a later
            # exit(restore_snapshot=True) cannot bring back the user's pre-run setup. Surface
            # it so the digest reader knows the preflight settings backup is the authoritative
            # restore for this run. Tell-only; entering is still correct.
            stale_calibration = False
            try:
                stale_calibration = bool(self.controller.calibration_status().get("active"))
            except Exception:  # noqa: BLE001 - status probe is advisory; enter itself will surface a dead pipe
                pass
            if stale_calibration:
                self.ctx.log("DesktopLUT was already in calibration mode (a previous run did not "
                             "exit) — the pipe's restore snapshot now captures that cleared state; "
                             "treat the preflight settings backup as the authoritative restore")
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
            res = self.controller.enter_neutral(self.monitor, self.mode, self.dummy_icc,
                                                reason="DLC v2 calibration")
            # calibration.enter cleared the layers + REMOVED the ICM — but Windows keeps the
            # LAST associated MHC2 transform, so the panel is still driven through whatever
            # was applied before (HW-proven 2026-09-03). Associate an IDENTITY profile
            # through the normal path so the raw stages measure the bare panel.
            neutral_profile = self._associate_identity_profile()
            digest: dict[str, Any] = {"entered": True, "neutral_profile": neutral_profile}
            if stale_calibration:
                digest["stale_calibration_mode"] = True
                digest["note"] = ("snapshot-restore now reflects a cleared state; "
                                  "preflight backup is the authoritative rollback")
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

    def _neutral_state_audit(self) -> dict[str, Any]:
        """The pipe + DesktopLUT.ini neutral-state audit for this monitor/mode (see
        :func:`dlc.neutral_audit.neutral_state_audit`); degrades to notes (never raises) on the
        mock / a profile without ``paths.desktoplut_ini``."""
        audit = neutral_audit.neutral_state_audit(
            self.controller, self.monitor, self.mode, ini_path=self._resolve_desktoplut_ini())
        audit["neutral_profile"] = self.calib.get("neutral_profile")
        return _jsonable(audit)

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
            return StageOutcome(key, "done", digest={"required": False, "neutral_audit": audit,
                                                     "installed_stack": self.calib.get("installed_stack")})

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
                        "bit_depth": self.bit_depth, "dogegen_required": True,
                        "neutral_audit": audit,
                        "installed_stack": self.calib.get("installed_stack")}))
            if decision.choice == "abort":
                raise CalibrationAborted(StageOutcome(
                    key, "aborted",
                    digest={"message": "hardware readiness aborted by operator/LLM",
                            "decision_note": decision.note}))
            return StageOutcome(key, "done",
                                digest={"required": True, "confirmed": True,
                                        "decision_note": decision.note,
                                        "neutral_audit": audit,
                                        "installed_stack": self.calib.get("installed_stack")})

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
            res = self._measure_set(patches, role=role, ti3_name=ti3_name, ndjson_name=ndjson_name)
            bookend_drift = self._bookend_drift_qc(role, res.ti3_path, patches)
            digest = dict(res.digest)
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
            # A dark panel, compromised preheat, or a blown remeasure/drift budget is non-benign:
            # recommend retry (not accept), offer it, and flag compromised so SupervisedAdjudicator
            # escalates rather than rubber-stamping black/garbage data. The RECOMMENDATION (never
            # a decision — the seam still judges) also weighs read-repeatability: stable-but-
            # implausible envelope reads are real panel/correction behaviour a retry would just
            # re-measure and re-fail (item #4, 2026-09-02 C6 run).
            recommendation, basis = _measure_escalation_recommendation(outcome.digest)
            retry_recommended = recommendation == "retry"
            options = (("accept", "suppress", "remeasure", "retry", "abort")
                       if (retry_recommended or score_anomaly or measurement_path_compromised)
                       else ("accept", "suppress", "abort"))
            decision = self.adjudicate(AdjudicationRequest(
                key=f"{key}:escalation", seam=SEAM_MEASURE, stage=key,
                question=outcome.data.get("question") or "measurement did not fully settle - accept or retry?",
                options=options,
                recommendation=recommendation,
                digest={**outcome.digest,
                        **({"recommendation_basis": basis} if basis else {}),
                        "compromised": (panel_dark or preheat_compromised
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
                # closed-loop refine will hold D65 to (see stage_refine_mhc_cube). Carries the
                # WRGB-gate fields (drive_matched_nonadditivity / cap_policy / wrgb_nonadditive /
                # full_drive_grounded) so the adjudicator sees whether the additive cap was applied
                # or bypassed for a W-subpixel panel, and whether the ceiling is full-drive-grounded.
                digest["peak_chroma"] = params["peak_chroma"]
            if params.get("dark_floor"):
                # The σ-aware adaptive dark floor's verdict (Phase 4, F4-1/HW-4): nits + how
                # many strayed dark reads were σ-verified REAL drift (corrected) vs smoothed.
                digest["dark_floor"] = params["dark_floor"]
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
        if outcome.status == "done" and self._spec().is_hdr:
            self._pin_hdr_peak_to_cap(outcome)
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
                rec = self.calib["stages"].pop(stale, None)
                if stale.startswith("measure:"):
                    self._invalidate_thermal_align(stale, ((rec or {}).get("data") or {}).get("ti3"))
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
            self.calib["grayscale_wb_prior"] = prior_snapshot = self._snapshot_correction_grayscale()
            self._save()
            if prior_snapshot is None:
                # Honesty tell (fable Phase 9): None means EITHER no prior correction exists OR
                # this DesktopLUT build doesn't expose correction_grayscale in state.get (the
                # current C++ reports only applied/profile_name — exposing it is a ticketed
                # DesktopLUT change). Either way a later `revert` clears the touch-up to
                # identity rather than restoring a pre-existing correction — say so up front.
                self.ctx.log("no prior correctionGrayscale captured over the pipe (none exists, or "
                             "this DesktopLUT build does not expose it in state.get) — a revert of "
                             "this touch-up will clear to identity, not restore a prior correction")

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
            loop_cfg = self.loop_config or self._loop_config_for(dip)
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
        current correction), or ``None`` if absent/unreadable — the DLC-owned revert snapshot
        for the grayscale touch-up (Design B). Best-effort: a down pipe just yields None (the
        touch-up won't proceed far without the pipe anyway)."""
        try:
            state = self.controller.state()
            key = f"{self.monitor}:{self.mode}"
            cg = (((state.get("mhc") or {}).get(key) or {}).get("correction_grayscale"))
            return dict(cg) if isinstance(cg, dict) and cg.get("points") else None
        except Exception:  # noqa: BLE001 - advisory snapshot; revert falls back to clearing
            return None

    def _restore_correction_grayscale(self) -> bool:
        """Re-apply the DLC-owned pre-begin correctionGrayscale snapshot and regenerate the ICM
        (Design B revert). Restores the user's prior correction if one was captured, else clears
        to identity — either way it does NOT rely on the C++ cancel-after-commit no-op. Returns
        whether the restore call chain succeeded."""
        prior = self.calib.get("grayscale_wb_prior")
        try:
            if prior and prior.get("points"):
                self.controller.set_correction_grayscale(
                    self.monitor, self.mode, prior["point_count"], prior["points"],
                    prior["deviations"], gamma=float(self._spec().gamma))
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
        return cfg

    def stage_build_install_3dlut(self, post_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            target = self._engine_target()
            report_scorer, report_metric = self._optimizer_report_scorer()
            samples = parse_ti3(Path(post_ti3))
            signals = np.array([s.rgb for s in samples], dtype=float)
            measured = np.array([s.xyz for s in samples], dtype=float)
            cube_path = str(self.ctx.root / "generated" / f"final_{self.mode.lower()}.cube")
            try:
                result = optimize_cube(target=target, probe=self._probe_fn(), signals=signals,
                                       measured_xyz=measured, config=self._cube_optimize_config(),
                                       on_iteration=self._on_optimize_iteration,
                                       reachable_primaries=self._reachable_primaries(),
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
            # Score against the SAME resolved white the pipeline targeted (MHC matrix + grayscale
            # refine, 3D-LUT target) — not textbook D65 — so a non-zero white strength is the goal
            # here, not scored as white error. SDR scores CIEDE2000 against γ-power/sRGB; HDR
            # scores dE_ITP against PQ/Rec.2020 (the metric the cube converges in — CIEDE2000's
            # Lab is meaningless at HDR absolute luminance), with looser, LLM-negotiated targets.
            wx, wy = self._white_xy()
            reachable = self._reachable_primaries() if spec.is_hdr else None
            if spec.is_hdr:
                hdr = self._hdr_target()
                metrics, lum = score_samples_hdr(samples, white_xy=(wx, wy), peak_nits=hdr.peak_nits,
                                                  reachable_primaries=reachable)
                metric_name = "dE_ITP"
                # Advisory HDR defaults (dE_ITP), overlaid by the profile's optional
                # ``quality: {hdr: {...}}`` block — the same policy source the stage-CLI
                # scorer reads — then negotiated by the assistant at the verify seam after
                # the first refinement round (design §7).
                q = hdr_metric_thresholds(self.profile.quality_policy)
            else:
                metrics, lum = score_samples(samples, gamma=spec.gamma, white_xy=(wx, wy))
                metric_name = "CIEDE2000"
                q = self.profile.quality
            summary = summarize_metrics(phase="verification", iteration=0, source=Path(verify_ti3),
                                        patch_metrics=metrics, target_luminance=lum, metric=metric_name)
            # The §0 practically-weighted view (metrics.practical_summary): core (Rec.709 ≤
            # ref-white, reachable) is the practical verdict; `clamped` isolates residuals at
            # the panel's gamut floor so they are read as reachability, never calibration error.
            practical = practical_summary(metrics, is_hdr=spec.is_hdr,
                                          gamut_aware=reachable is not None)
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
            return StageOutcome("verify", "done", digest=digest,
                                data={"within_quality": within, "metrics": {
                                    "avg_de2000": summary.avg_de2000, "p95_de2000": summary.p95_de2000,
                                    "max_de2000": summary.max_de2000, "white_de2000": summary.white_de2000}})

        outcome = self._stage("verify", run)
        d = outcome.digest
        within = outcome.data.get("within_quality")
        severe = self._severe_verify_failure(outcome)
        # The question quotes the numbers the GATE scored (practical core/tube/white when
        # available — D3) with the overall avg as context, so the seam's first line no
        # longer leads with an OOG-inflated headline the digest then has to walk back.
        scored = (d.get("gate") or {}).get("scored") or {}
        if scored:
            tube_txt = scored.get('tube_avg') if scored.get('tube_avg') is not None else "— (none measured)"
            reads = (f"core avg {scored.get('core_avg')} (p95 {scored.get('core_p95')}, "
                     f"max {scored.get('core_max')}), tube {tube_txt}, "
                     f"white {scored.get('white')} {d.get('metric', 'ΔE')} "
                     f"(overall avg {d.get('avg_de2000')} incl. gamut-limit/OOG framework)")
        else:
            reads = (f"avg {d.get('metric', 'ΔE')} {d.get('avg_de2000')} "
                     f"(white {d.get('white_de2000')}, max {d.get('max_de2000')})")
        self.adjudicate(AdjudicationRequest(
            key="verify:accept", seam=SEAM_VERIFY, stage="verify",
            question=(f"The new calibration reads {reads} — "
                      f"{'within' if within else 'outside'} the quality targets. "
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

    @staticmethod
    def _quality_gate(summary, practical: dict, q) -> tuple[bool, dict]:
        """The deterministic verify quality gate (D3, 2026-08-14): score the practical
        core/tube/white buckets against the acceptance targets — OOG/limits patches are
        reachability framework, never gate inputs. Returns ``(within, gate_basis)`` where
        ``gate_basis`` records what was scored and each check's verdict (seam evidence).

        * ``core``  — avg/p95/max vs the mode's acceptance targets (the practical verdict).
        * ``tube``  — avg vs the avg target (a neutral cast must not hide behind core colour).
        * ``white`` — the summary white vs the white target (unchanged).
        * Fallback: an empty core bucket (degenerate/truncated set) uses the legacy overall
          summary gate — a gate must never pass vacuously.
        """
        core = (practical or {}).get("core") or {}
        tube = (practical or {}).get("tube") or {}
        if not core.get("n"):
            within = (summary.avg_de2000 <= q.avg_de2000 and summary.p95_de2000 <= q.p95_de2000
                      and summary.max_de2000 <= q.max_de2000
                      and summary.white_de2000 <= q.white_de2000)
            return within, {"basis": "overall (legacy fallback: empty core bucket)",
                            "checks": {"avg": summary.avg_de2000 <= q.avg_de2000,
                                       "p95": summary.p95_de2000 <= q.p95_de2000,
                                       "max": summary.max_de2000 <= q.max_de2000,
                                       "white": summary.white_de2000 <= q.white_de2000}}
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
        basis = {"basis": "practical core+tube+white (D3)", "checks": checks,
                 "scored": {"core_avg": core["avg"], "core_p95": core["p95"],
                            "core_max": core["max"], "core_n": core["n"],
                            "tube_avg": tube.get("avg"), "tube_n": tube.get("n"),
                            "white": summary.white_de2000}}
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
                      ("converged", "best_max_de", "best_mean_de", "best_max_de_report",
                       "best_mean_de_report", "metric", "optimize_metric", "above_threshold",
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
        # Own our own keep-awake for the WHOLE run (not just the measure stages): the
        # compute-bound 3D-LUT build phase presents no patch, so the fullscreen presenter
        # can't be relied on to hold the display lock there — a gap that on this rig's
        # aggressive power plan (display off 5 min, sleep 15 min) blanks the panel mid-run
        # and corrupts the next read. Released in finally (incl. on a seam abort) so the
        # request never leaks past the run; no-op on non-Windows. stage_measure asserts it
        # again per-stage (reentrant) as defence-in-depth for direct/partial-flow callers.
        try:
            with keep_awake(reason=f"dlc calibration run ({flow})"):
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
            if flow == "grayscale-wb":
                return self._flow_grayscale_wb()
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
    "build-correction": ("preflight", "clear-native", "probe-match"),
    "characterize": ("preflight", "clear-native", "hardware-readiness", "characterize"),
}

# Flow registry (the named flows the front door maps an intent onto). HDR is a run
# MODE (--mode HDR), orthogonal to the flow — the "hdr" entry is a signpost stub.
FLOWS: dict[str, str] = {
    "full": "neutral → raw → MHC + D65 grayscale refine → post-MHC → 3D LUT → verify → report",
    "mhc-only": "raw → MHC (matrix + 1D + D65 grayscale refine) → verify → report (ICC only; no 3D LUT — shakedown)",
    "3dlut-only": "verify MHC present → measure → 3D LUT → verify → report",
    "grayscale-wb": "verify MHC present -> patch-by-patch user Grayscale correction -> grey-ramp verify",
    "build-correction": "preflight → prepare ccxxmake → operator runs it → ingest .ccmx (+white.sp) → store",
    "characterize": "preflight → plan → clear-native → learn panel+meter (noise/settle/drift) → DIP store → restore",
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
    forever, so the recommendation flips to accept — with the basis spelled out for the judge."""
    hard = bool(digest.get("panel_dark")
                or digest.get("present_stall")
                or digest.get("preheat_compromised")
                or digest.get("remeasure_budget_exceeded")
                or digest.get("drift_density_exceeded"))
    path_compromised = bool(digest.get("measurement_path_compromised"))
    if hard:
        return "retry", None
    if not path_compromised:
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
                            thermal_align=args.thermal_align)
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
                    controller.exit_calibration(restore_snapshot=True)
                    print(json.dumps({"status": "rolled_back",
                                      "reason": "run did not complete; restored pre-run setup",
                                      "run": str(ctx.root)}, indent=2))
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
