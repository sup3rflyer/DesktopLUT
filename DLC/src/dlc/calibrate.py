"""The scripted calibration orchestrator (v2-design-notes §1,3,4,5,7,11; item 5).

The v2 pivot: a **deterministic scripted core owns ALL the mechanics** (display
mapping, patch sets, measurement sequencing, the loops, integrity gates, LUT
generation) and a **thin LLM sits only at the seams** — it never tails a stream,
it judges *digests* at boundaries. This module is that core.

A run is a **named flow** (``full`` / ``3dlut-only`` / ``gray-wb``; ``hdr`` is the
later goal) expressed as an ordered list of stage methods. The pipeline is
**ICC → 3D LUT → GS+WB**, and crucially GS+WB is the *small FINAL tweak after the
3D LUT*, not an MHC refine loop between the two (the 3D LUT does the volumetric
heavy lifting incl. the neutral axis).

**The LLM seam = the** :class:`Adjudicator`. At each ``⚑`` point the core hands the
adjudicator a structured :class:`AdjudicationRequest` (a digest + a question + the
allowed choices + the *core's recommendation*) and gets back a :class:`Decision`.

* :class:`AutoAdjudicator` rubber-stamps the recommendation → the whole flow runs
  to completion in one process (tests, ``--simulate``, autonomous/CI runs).
* :class:`MappingAdjudicator` answers from a decisions map and **raises**
  :class:`AdjudicationRequired` on the first un-decided seam → the live LLM-driven
  pause/resume model: the CLI catches it, emits the digest+question, the LLM
  decides, and re-running with that decision recorded fast-forwards (every completed
  stage is **memoised** in the run-record, so measurements are never repeated) to
  the seam and proceeds. The memoisation also gives free crash-recovery.

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
    ramp_patches,
    shadow_levels,
    sort_patches,
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
from .mhc import parse_ti3
from .optimize import DegenerateMeasurements, OptimizeConfig, ProbeFn, optimize_cube
from . import patch_evidence
from .paths import RUNS_DIR, atomic_write_text
from .refine import Deviations, GrayPatch, MeasuredPrimaries, RefinementTarget, propose_correction_grayscale
from .runs import RunContext, create_run, open_run
from .stages import _common, build_mhc

__all__ = [
    "Decision",
    "AdjudicationRequest",
    "AdjudicationRequired",
    "Adjudicator",
    "AutoAdjudicator",
    "MappingAdjudicator",
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
SEAM_WATCHDOG = "gswb_watchdog"    # tweak magnitude growing large → recal (§3)
SEAM_VERIFY = "verify"             # final score vs quality targets
SEAM_STACK = "require_stack"       # gray-wb/3dlut-only precondition unmet
SEAM_CHARACTERIZE = "characterize" # characterization surfaced abnormal panel/meter behaviour
SEAM_PLANNING = "adaptive_planning" # opt-in LLM patch-strategy investigation seam (§6a; #47/#49)


# ---------------------------------------------------------------------------
# Adjudication — the LLM seam
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
    def adjudicate(self, request: AdjudicationRequest) -> Decision: ...


class AutoAdjudicator:
    """Deterministic: take the core's recommendation. The headless/sim/CI default
    and the policy a human/LLM would otherwise rubber-stamp."""

    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        return Decision(request.recommendation, note="auto: accepted core recommendation",
                        payload=request.recommended_payload)


class MappingAdjudicator:
    """Answer from a decisions map; **raise** on the first un-decided seam.

    The live LLM pause/resume seam: seed it with the decisions made so far (loaded
    from the run-record on resume); the first seam without a recorded decision
    raises :class:`AdjudicationRequired`, which the CLI surfaces to the LLM."""

    def __init__(self, decisions: Optional[dict[str, Decision]] = None) -> None:
        self.decisions = dict(decisions or {})

    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        if request.key in self.decisions:
            return self.decisions[request.key]
        raise AdjudicationRequired(request)


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

    # neutral axis (GS+WB tweak / gray-wb flow)
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
            if f.name in ("raw_saturations", "verify_saturations"):
                kw[f.name] = tuple(float(x) for x in v)
            elif f.name in ("spines", "raw_include_secondaries"):
                kw[f.name] = bool(v)
            elif f.name in ("gamut_lum_bias", "low_light_signal", "low_light_bias"):
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
        for sat_key in ("raw_saturations", "verify_saturations"):
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
    """A flow ended early at the core's own invariant (e.g. gray-wb with no MHC
    stack to tune, or HDR in an SDR-first build). Carries the partial result."""

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
        skip_gswb: bool = False,
        adaptive_planning: bool = False,
        stall_kill_hook: Optional[Callable[[], None]] = None,
        pause_handler: Optional[Callable[[Mapping[str, Any]], None]] = None,
        enable_watchdog: bool = False,
    ) -> None:
        self.ctx = ctx
        self.profile = profile
        self.monitor = monitor
        self.mode = normalize_mode(mode)
        self.controller = controller
        self.measure = measure
        self.adjudicator = adjudicator
        self._probe = probe
        self.display = profile.display_for(monitor)
        self.bit_depth = bit_depth if bit_depth is not None else self.display.panel.bit_depth
        self.loop_config = loop_config
        self.optimize_config = optimize_config or OptimizeConfig()
        self.characterize_config = characterize_config
        self.run_date = run_date or date.today()
        self.force = force
        self.dummy_icc = dummy_icc
        self.patch_sizes = patch_sizes or PatchSizes()
        self._white_fn = white_fn
        # Skip the FINAL GS+WB tweak in `full` (the deferred stage that targets the wrong
        # DesktopLUT layer — see reference-dlc-gswb-target). Yields a cohesive ICC→3D-LUT run
        # (one rollback unit, one verify gate) without baking a known-wrong grayscale tweak.
        self.skip_gswb = skip_gswb
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

        self._state = _common.load_dlc_state(ctx)
        self.calib: dict[str, Any] = self._state.setdefault("calib", {})
        self.calib.setdefault("stages", {})
        self.calib.setdefault("decisions", {})
        self.target_name: Optional[str] = self.calib.get("target")

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

    # -- persistence ------------------------------------------------------
    def _save(self) -> None:
        self._state["calib"] = self.calib
        self._state.setdefault("monitor", self.monitor)
        self._state.setdefault("mode", self.mode)
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
                data["luminance"] = spec.luminance_nits
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
        """In-place flows (gray-wb / 3dlut-only) tune the *installed* stack directly — they
        never enter calibration mode, so there is no C++ snapshot to revert to. Record what
        IS restorable over the pipe (the runtime 3D-LUT cube) BEFORE we mutate, so a 'revert'
        at the apply gate can put the prior cube back. The MHC correction-grayscale/white a
        gray-wb tweak overwrites is NOT exposed by ``state.get`` (the C++ HandleStateGet only
        reports ``applied``/``cube_path``), so that half is not auto-revertible — the durable
        settings backup captured at preflight is the fallback. Captured once (persists across
        a pause/resume in the run-record)."""
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
        """Revert an in-place refinement (gray-wb / 3dlut-only). 3dlut-only's only display
        mutation is the runtime cube, so it is fully restorable — put the prior cube back
        (or clear it if there was none). gray-wb overwrote the MHC correction-grayscale/white,
        which ``state.get`` does not expose, so it cannot be faithfully auto-reverted: surface
        the durable settings backup for a manual restore. Returns the terminal status
        (``reverted`` when the display was put back, else ``revert_unavailable``)."""
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
            "could not auto-revert this in-place refinement: the prior MHC grayscale/white is "
            "not recoverable over the pipe. Restore manually from the pre-run settings backup"
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
        (``mhc-only`` / ``gray-wb`` leave ``deliverable_cube`` None). Best-effort: a failure leaves
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
        # The 3D-LUT correction targets the SAME resolved white the MHC/GS+WB stages do.
        return self.profile.engine_target(self.target_name, white_xy=self._white_xy())

    def _hdr_target(self):
        """The chosen HDR target (peak/undershoot/knee/fixed white) for an HDR run,
        resolved from this display+mode's DIP and the run's resolved white
        (``docs/hdr-target-design.md``). Memoised in the run-record so a resumed verify/
        report sees the same peak the build targeted. Only meaningful for a PQ target."""
        cached = self.calib.get("hdr_target")
        if cached:
            from .hdr_target import HdrTarget

            return HdrTarget(
                peak_nits=cached["peak_nits"], white_xy=tuple(cached["white_xy"]),
                undershoot_gain=cached["undershoot_gain"],
                knee_start_nits=cached["knee_start_nits"],
                container_nits=cached.get("container_nits", 10000.0),
                provenance=cached.get("provenance", {}))
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
            self.runlog.optimizer_iteration(**result.as_dict())
        except Exception:  # noqa: BLE001 - telemetry must never break the build
            pass

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
            target_nits, is_hdr = spec.luminance_nits, spec.is_hdr
        except (KeyError, AttributeError):
            target_nits, is_hdr = None, (self.mode == "HDR")
        tell: dict[str, Any] = {"checked": True, "native_white_nits": round(white, 2),
                                "native_black_nits": (round(black, 5) if black is not None else None),
                                "contrast": (round(contrast) if contrast else None),
                                "target_nits": target_nits, "mode": self.mode}
        msgs: list[str] = []
        # HDR peak headroom: the measured peak IS the panel's HDR ceiling (not OSD-adjustable),
        # so a target peak above it can't be hit — the build must roll off / drop the ceiling.
        if is_hdr and target_nits and white < target_nits * 0.95:
            msgs.append(f"native peak {white:.0f} nits is below the {target_nits:g}-nit target peak — "
                        f"the build must roll off / lower the sustained ceiling to ~{white:.0f}")
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
            store_rec = self._correction_store().get(self.display.name)
            store_made = store_rec.correction_made if store_rec else None
            staleness = self.profile.correction_staleness(today=self.run_date, made_override=store_made)
            # Patch-window placement guard (M3): dogegen has NO monitor-select CLI — its window
            # opens on the Windows primary and is positioned/fullscreened by hand. If the
            # calibration target isn't the primary, patches would land on the WRONG panel and
            # every measurement would be silently wrong. Assert the topology instead of assuming it.
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
        white flows into the MHC matrix, the 3D-LUT target, and the GS+WB tweak — all
        three aim at the *same* white. No new ⚑ seam: the correction-staleness *tell*
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
                digest={k: outcome.digest.get(k) for k in
                        ("flags", "warm", "cold_channel", "settle_seconds", "noise_floor_nits")})),
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
            in_range = abs(nits - target) <= max(3.0, 0.05 * target)
            digest = {"white_nits": round(nits, 2), "target_nits": target,
                      "in_range": in_range, "read_attempts": attempts}
            return StageOutcome("brightness", "done", digest=digest,
                                data={"white_nits": nits, "in_range": in_range})

        outcome = self._stage("brightness", run)
        if not outcome.data.get("in_range"):
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="brightness:adjust", seam=SEAM_BRIGHTNESS, stage="brightness",
                question=(f"white reads {outcome.digest['white_nits']} nits vs target "
                          f"{outcome.digest['target_nits']:g} — have the human set the OSD backlight, "
                          "or accept this level?"),
                options=("accept", "abort"), recommendation="accept", digest=outcome.digest)),
                stage="brightness", message="aborted on out-of-range white luminance")
        return outcome

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
        # Before→after dE trend on the spine (#8): score the INTERMEDIATE measures so the
        # dashboard's ΔE panel + de_history show the run converging (native → after ICC → after
        # 3D LUT) instead of a single verify point. verify does its own richer scoring at the gate.
        if role in ("raw", "post-mhc") and outcome.status == "done":
            self._score_stage(role, outcome.data.get("ti3"),
                              label="raw (native)" if role == "raw" else "after ICC")
        if outcome.data.get("needs_adjudication"):
            retry_recommended = bool(outcome.digest.get("remeasure_budget_exceeded")
                                     or outcome.digest.get("drift_density_exceeded"))
            decision = self.adjudicate(AdjudicationRequest(
                key=f"{key}:escalation", seam=SEAM_MEASURE, stage=key,
                question=outcome.data.get("question") or "measurement did not fully settle - accept or retry?",
                options=(("accept", "retry", "abort") if retry_recommended else ("accept", "abort")),
                recommendation=("retry" if retry_recommended else "accept"),
                digest=outcome.digest))
            if decision.choice == "retry":
                raise CalibrationAborted(StageOutcome(
                    key, "aborted",
                    digest={"message": "measurement retry requested at LLM seam",
                            "retry_requested": True, **outcome.digest}))
            self._abort_if(decision, stage=key, message="aborted on unsettled measurement")
        return outcome

    def _score_stage(self, role: str, ti3_path: Optional[str], *, label: str) -> None:
        """Score an intermediate measure stage against the resolved target and put a
        ``metrics_scored`` digest on the spine — so the dashboard's ΔE panel + de_history show
        the calibration converging stage by stage (a single verify point was uninformative).
        Advisory: a missing/empty TI3 or any scoring hiccup is swallowed, never breaks the flow."""
        if not ti3_path:
            return
        try:
            p = Path(ti3_path)
            if not p.exists():
                return
            samples = parse_ti3(p)
            if not samples:
                return
            spec = self._spec()
            wx, wy = self._white_xy()
            metrics, lum = score_samples(samples, gamma=spec.gamma, white_xy=(wx, wy))
            summary = summarize_metrics(phase=label, iteration=0, source=p,
                                        patch_metrics=metrics, target_luminance=lum)
            colour_de = [m.de2000 for m in metrics if not m.grayscale]
            self.runlog.metrics_scored(
                f"measure:{role}", label=label, iteration=0,
                avg_de2000=round(summary.avg_de2000, 3), p95_de2000=round(summary.p95_de2000, 3),
                p99_de2000=round(percentile([m.de2000 for m in metrics], 99.0), 3),
                max_de2000=round(summary.max_de2000, 3), white_de2000=round(summary.white_de2000, 3),
                grayscale_avg_de2000=round(summary.grayscale_avg_de2000, 3),
                colour_avg_de2000=(round(sum(colour_de) / len(colour_de), 3) if colour_de else None),
                patch_count=summary.patch_count, grayscale_count=summary.grayscale_count)
        except Exception:  # noqa: BLE001 - advisory telemetry; a scoring hiccup never breaks the flow
            pass

    def stage_build_install_mhc(self, raw_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            spec = self._spec()
            # Derive MHC params from the raw TI3 (reuses the proven build-mhc stage:
            # measured primaries + native-white→target-white matrix + tone-only base 1D).
            args = Namespace(run=self.ctx.root, monitor=self.monitor, mode=self.mode,
                             simulate=False, gamma=spec.gamma, source_ti3=raw_ti3,
                             is_hdr=spec.is_hdr)
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
            # Install through the controller (set primaries/white/base grayscale → apply → verify).
            self.controller.set_primaries(self.monitor, self.mode, params["primaries"])
            white = self._resolved_white()
            wx, wy = white.xy
            self.controller.set_white(self.monitor, self.mode, wx, wy)
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
            return StageOutcome("build-install-mhc", "done", digest=digest,
                                data={"profile_name": profile_name, "verified": verify_ok})

        return self._stage("build-install-mhc", run)

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
                          "measure:gray-wb", "gswb-tweak", "measure:verify", "verify"):
                self.calib["stages"].pop(stale, None)
        self.calib["adaptive_plan"] = {"fingerprint": new_fp, "decision": normalized,
                                       "worth_investigating": evidence["worth_investigating"]}
        self._save()
        self.runlog.stage_done(
            "adaptive-planning",
            strategy=f"{normalized['shadow_treatment']}/{normalized['volumetric_density']}",
            source=normalized.get("source"), confidence=normalized.get("confidence"))

    def stage_build_install_3dlut(self, post_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            target = self._engine_target()
            samples = parse_ti3(Path(post_ti3))
            signals = np.array([s.rgb for s in samples], dtype=float)
            measured = np.array([s.xyz for s in samples], dtype=float)
            cube_path = str(self.ctx.root / "generated" / f"final_{self.mode.lower()}.cube")
            try:
                result = optimize_cube(target=target, probe=self._probe_fn(), signals=signals,
                                       measured_xyz=measured, config=self.optimize_config,
                                       on_iteration=self._on_optimize_iteration)
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
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key="build-install-3dlut:floor", seam=SEAM_OPTIMIZE, stage="build-install-3dlut",
                question=outcome.data.get("question") or "the correction machine hit a floor — accept or loosen?",
                options=("accept", "loosen_target", "abort"), recommendation="accept",
                digest={k: outcome.digest.get(k) for k in
                        ("best_max_de", "above_threshold", "physical_floor", "budget_limited", "converged")})),
                stage="build-install-3dlut", message="aborted at the 3D-LUT correction floor")
        return outcome

    def stage_gswb_tweak(self, neutral_ti3: str) -> StageOutcome:
        """The small FINAL GS+WB tweak, authored AFTER the 3D LUT (§4). Computes a
        per-channel correction-grayscale + a residual white move from a neutral-axis
        measurement, bakes them into the editable MHC controls, and runs the
        tweak-drift **watchdog** (§3) on the magnitude + cross-run trend."""
        def run() -> StageOutcome:
            spec = self._spec()
            samples = parse_ti3(Path(neutral_ti3))
            gray = [GrayPatch(level=s.rgb[0], xyz=s.xyz) for s in samples
                    if abs(s.rgb[0] - s.rgb[1]) < 1e-6 and abs(s.rgb[1] - s.rgb[2]) < 1e-6]
            gray.sort(key=lambda p: p.level)
            white_xy = _xy(max(samples, key=lambda s: s.xyz[1]).xyz)
            wx, wy = self._white_xy()
            white_move = float(np.hypot(white_xy[0] - wx, white_xy[1] - wy))
            if len(gray) < 2:
                magnitude = 0.0
                deviations = Deviations.identity(max(1, len(gray))).as_dict()
                points = [round(p.level, 6) for p in gray] or [1.0]
                point_count = len(points)
            else:
                prim_params = (self._state.get("mhc_params") or {}).get("primaries")
                primaries = _measured_primaries(prim_params, white_xy)
                proposal = propose_correction_grayscale(
                    measured=gray,
                    target=RefinementTarget(white_x=wx, white_y=wy, gamma=spec.gamma,
                                            peak_luminance=spec.luminance_nits),
                    primaries=primaries, current=Deviations.identity(len(gray)), damping=1.0)
                deviations = proposal["deviations"]
                points = proposal["points"]
                point_count = proposal["point_count"]
                magnitude = float(proposal["summary"].get("max_abs_deviation", 0.0))
                self.controller.set_correction_grayscale(self.monitor, self.mode, point_count, points, deviations, gamma=spec.gamma)
                self.controller.set_white(self.monitor, self.mode, wx, wy)
                self.controller.apply_mhc(self.monitor, self.mode)
            watchdog = self._watchdog(magnitude)
            digest = {"tweak_magnitude": round(magnitude, 5), "white_move_xy": round(white_move, 5),
                      "points": point_count, "watchdog": watchdog}
            return StageOutcome("gswb-tweak", "done", digest=digest,
                                data={"magnitude": magnitude, "watchdog": watchdog})

        outcome = self._stage("gswb-tweak", run)
        wd = outcome.data.get("watchdog") or {}
        if wd.get("trips"):
            self.adjudicate(AdjudicationRequest(
                key="gswb-tweak:watchdog", seam=SEAM_WATCHDOG, stage="gswb-tweak",
                question=(f"GS+WB tweak magnitude {wd.get('magnitude')} (trend {wd.get('trend')}) is "
                          f"approaching the level that would override the 3D LUT's neutral axis — patch "
                          f"now, or has the panel drifted enough to warrant a full recalibration?"),
                options=("apply", "recommend_recal"), recommendation=wd.get("recommendation", "apply"),
                digest=wd))
        return outcome

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
            # Score against the SAME resolved white the pipeline targeted (MHC matrix, 3D-LUT
            # target, GS+WB tweak) — not textbook D65 — so a non-zero white strength is the goal
            # here, not scored as white error. SDR scores CIEDE2000 against γ-power/sRGB; HDR
            # scores dE_ITP against PQ/Rec.2020 (the metric the cube converges in — CIEDE2000's
            # Lab is meaningless at HDR absolute luminance), with looser, LLM-negotiated targets.
            wx, wy = self._white_xy()
            if spec.is_hdr:
                hdr = self._hdr_target()
                metrics, lum = score_samples_hdr(samples, white_xy=(wx, wy), peak_nits=hdr.peak_nits)
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
                "verify", label="verification", iteration=0,
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
                      "quality_targets": q.as_dict(),
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
        self.adjudicate(AdjudicationRequest(
            key="verify:accept", seam=SEAM_VERIFY, stage="verify",
            question=(f"The new calibration reads avg {d.get('metric', 'ΔE')} {d.get('avg_de2000')} "
                      f"(white {d.get('white_de2000')}, max {d.get('max_de2000')}) — "
                      f"{'within' if within else 'outside'} the quality targets. "
                      "Apply this calibration, or revert to the previous display setup?"),
            options=("apply", "revert"),
            recommendation="apply",
            digest=outcome.digest))
        return outcome

    # ====================================================================
    # Watchdog (§3) — cross-run GS+WB tweak history
    # ====================================================================
    def _tweak_history_path(self) -> Path:
        """Cross-run GS+WB tweak history, persisted next to the run folders (so it
        accumulates across runs, not per-run). The owner's per-display 'medical
        history' the watchdog reads."""
        base = self.ctx.root.parent if self.ctx.root.parent != self.ctx.root else self.ctx.root
        return base / "tweak_history.json"

    def _load_tweak_history(self) -> dict[str, Any]:
        path = self._tweak_history_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
        return {}

    def _watchdog(self, magnitude: float) -> dict[str, Any]:
        """Judge the new GS+WB tweak against the per-display history. A *small* tweak
        is fine; a magnitude (or a growing trend) approaching the level that would
        override the 3D LUT's own neutral-axis correction → recommend a full recal."""
        key = f"{self.display.name}:{self.mode}"
        history = self._load_tweak_history()
        entries = list(history.get(key, []))
        prior = [float(e.get("magnitude", 0.0)) for e in entries]
        # Thresholds (deviation space): a single tweak this large, or an accumulated
        # trend this large, would start fighting the volumetric neutral-axis correction.
        single_limit = 0.06
        trend_limit = 0.12
        trend = round(sum(prior) + magnitude, 5)
        trips = magnitude >= single_limit or trend >= trend_limit
        recommendation = "recommend_recal" if trips else "apply"
        entries.append({"date": self.run_date.isoformat(), "magnitude": round(magnitude, 5)})
        history[key] = entries
        # Atomic: the cross-run tweak history is the watchdog's medical record; a truncated
        # write would corrupt it (load is try/except-tolerant → silently empty → lost trend).
        atomic_write_text(self._tweak_history_path(), json.dumps(history, indent=2))
        return {"magnitude": round(magnitude, 5), "trend": trend, "single_limit": single_limit,
                "trend_limit": trend_limit, "prior_runs": len(prior), "trips": trips,
                "recommendation": recommendation}

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
            cube_out = results_dir / descriptive_cube_name(
                date=self.run_date.isoformat(), display=self.display.short_name, mode=self.mode,
                colorspace=_gamut_label(spec.colorspace, is_hdr=spec.is_hdr, gamma=spec.gamma),
                transfer=_transfer_token(is_hdr=spec.is_hdr, gamma=spec.gamma),
                luminance_nits=spec.luminance_nits)
            shutil.copy2(cube_src, cube_out)
        # Copy the verification TI3.
        verify_ti3 = sdat("measure:verify").get("ti3") or sdat("measure:gray-wb").get("ti3")
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
            "gswb": sd("gswb-tweak") or None,
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
                                    "verification": payload["verification"], "lut3d": payload["lut3d"],
                                    "gswb_watchdog": (payload["gswb"] or {}).get("watchdog")},
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
        self.calib["flow"] = flow
        self._save()
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
            if flow == "gray-wb":
                return self._flow_gray_wb()
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

    def _ramp_patches(self) -> list[tuple[int, int, int]]:
        return build_ramp_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                              max_cv=self._patch_max_cv())

    def _volumetric_patches(self) -> list[tuple[int, int, int]]:
        return build_volumetric_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                    max_cv=self._patch_max_cv())

    def _neutral_patches(self) -> list[tuple[int, int, int]]:
        return build_neutral_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                 max_cv=self._patch_max_cv())

    def _neutral_verify_patches(self) -> list[tuple[int, int, int]]:
        return build_neutral_verify_set(self.patch_sizes, self._transfer(),
                                        warm_tau=self._warm_tau(), max_cv=self._patch_max_cv())

    def _verify_patches(self) -> list[tuple[int, int, int]]:
        return build_verify_set(self.patch_sizes, self._transfer(), warm_tau=self._warm_tau(),
                                max_cv=self._patch_max_cv())

    def flow_patch_counts(self, flow: str) -> dict[str, Any]:
        plan = flow_patch_counts(flow, self.patch_sizes, self._transfer(),
                                 max_cv=self._patch_max_cv())
        if flow == "full" and self.skip_gswb and "gray-wb" in plan["stages"]:
            stages = dict(plan["stages"])
            stages.pop("gray-wb", None)
            plan = {**plan, "stages": stages, "total_patches": sum(stages.values()), "skip_gswb": True}
        return plan

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
            "skip_gswb": self.skip_gswb,
        }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return {**record, "fingerprint": hashlib.sha256(payload).hexdigest()[:16]}

    def _finish(self, *, analysis: Optional[str] = None) -> CalibrationResult:
        rep = self.stage_report(analysis=analysis)
        status = "completed"
        # Honour the apply/revert gate. Two rollback regimes:
        #  - flows that ENTERED calibration mode (full / mhc-only) have a real C++ snapshot:
        #    'revert' restores it, 'apply' (or anything non-revert) commits the new profile.
        #  - in-place flows (gray-wb / 3dlut-only) never entered calibration mode; the change
        #    is already live. 'revert' is honoured as far as the pipe allows (the 3D-LUT cube
        #    is restorable; the MHC grayscale a gray-wb tweak overwrote is not — see
        #    _revert_inplace). Either way we must NOT silently report 'completed' on a revert.
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
            # gitignored run dir). No-ops when this flow built no cube (mhc-only / gray-wb).
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
        self.stage_brightness()
        raw = self.stage_measure(role="raw", patches=self._ramp_patches(),
                                 ti3_name="raw.ti3", ndjson_name="raw.ndjson")
        self.stage_build_install_mhc(raw.data["ti3"])
        self.stage_adaptive_planning(raw_ti3=raw.data["ti3"])   # opt-in LLM investigation seam (#47/#49)
        post = self.stage_measure(role="post-mhc", patches=self._volumetric_patches(),
                                  ti3_name="post_mhc.ti3", ndjson_name="post_mhc.ndjson")
        self.stage_build_install_3dlut(post.data["ti3"])
        if not self.skip_gswb:
            gw = self.stage_measure(role="gray-wb", patches=self._neutral_patches(),
                                    ti3_name="gray_wb.ti3", ndjson_name="gray_wb.ndjson")
            self.stage_gswb_tweak(gw.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._verify_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_mhc_only(self) -> CalibrationResult:
        """ICC only — MHC matrix + base 1D LUT, then verify + report. NO 3D LUT and NO
        GS+WB tweak (those are what make ``full`` long), so this is the fast end-to-end
        path that proves the orchestration + hardware before committing to a dense run.
        The MHC alone is the *foundation*; without the volumetric/neutral refinement the
        3D LUT+GS+WB add, verify may sit above the final quality targets — that's expected
        for an ICC-only pass (accept it as a shakedown, judge it on the before/after)."""
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self.stage_enter_neutral()
        self.stage_brightness()
        raw = self.stage_measure(role="raw", patches=self._ramp_patches(),
                                 ti3_name="raw.ti3", ndjson_name="raw.ndjson")
        self.stage_build_install_mhc(raw.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._ramp_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_3dlut_only(self) -> CalibrationResult:
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self._require_stack(need_mhc=True, need_lut=False)
        self._capture_inplace_baseline()   # rollback point before set_3dlut mutates the live cube
        self.stage_adaptive_planning(raw_ti3=None)   # opt-in LLM investigation seam (no raw ramp here)
        post = self.stage_measure(role="post-mhc", patches=self._volumetric_patches(),
                                  ti3_name="post_mhc.ti3", ndjson_name="post_mhc.ndjson")
        self.stage_build_install_3dlut(post.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._verify_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_gray_wb(self) -> CalibrationResult:
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self._require_stack(need_mhc=True, need_lut=True)
        self._capture_inplace_baseline()   # record the restorable cube before the GS+WB tweak mutates the MHC
        self.stage_brightness()
        gw = self.stage_measure(role="gray-wb", patches=self._neutral_patches(),
                                ti3_name="gray_wb.ti3", ndjson_name="gray_wb.ndjson")
        self.stage_gswb_tweak(gw.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._neutral_verify_patches(),
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
        """gray-wb / 3dlut-only assume an installed stack. If it's missing, escalate
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
    "full": "neutral → raw → MHC → post-MHC → 3D LUT → GS+WB → verify → report",
    "mhc-only": "raw → MHC (matrix + 1D) → verify → report (ICC only; no 3D LUT / GS+WB — shakedown)",
    "3dlut-only": "verify MHC present → measure → 3D LUT → verify → report",
    "gray-wb": "require stack → brightness → measure neutral → GS+WB tweak → verify → report",
    "build-correction": "preflight → prepare ccxxmake → operator runs it → ingest .ccmx (+white.sp) → store",
    "characterize": "preflight → plan → clear-native → learn panel+meter (noise/settle/drift) → DIP store → restore",
    "hdr": "(post-v1) Rec.2020/PQ — SDR-first in v1",
}


# ---------------------------------------------------------------------------
# Display mode (SDR <-> HDR)
# ---------------------------------------------------------------------------

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
                   max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The MHC FOUNDATION ramp: a dense grey ramp + R/G/B (the matrix+1D fit's inputs); C/M/Y
    only if ``raw_include_secondaries`` (off by default — the volumetric set covers them).

    ``max_cv`` caps the top of the generated range (HDR: the target peak's code value, so no
    patch exceeds the reachable sub-peak range); ``None`` ⇒ the full bit-depth range (SDR)."""
    return ramp_patches(transfer, steps=ps.raw_ramp_steps, saturations=ps.raw_saturations,
                        spacing=ps.raw_spacing, include_secondaries=ps.raw_include_secondaries,
                        low_light_steps=ps.low_light_steps,
                        low_light_signal=ps.low_light_signal,
                        low_light_bias=ps.low_light_bias,
                        order=ps.order, warm_tau=warm_tau, max_cv=max_cv)


def build_volumetric_set(ps: PatchSizes, transfer: Transfer, *,
                         warm_tau: Optional[int] = None,
                         max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The 3D-LUT sampling set. ``volumetric_mode`` picks HOW the cube interior is sampled:
    a neutral-axis ``tube`` (default; dense where content lives), a uniform ``cube``, or a
    content-weighted ``gamut`` shell set. ``max_cv`` caps the range (HDR peak; see
    :func:`build_ramp_set`)."""
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


def build_neutral_set(ps: PatchSizes, transfer: Transfer, *,
                      warm_tau: Optional[int] = None,
                      max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The grey-axis ramp for the GS+WB tweak / gray-wb flow. ``max_cv`` caps the range
    (HDR peak; see :func:`build_ramp_set`)."""
    cap = max_cv if max_cv is not None else transfer.max_cv
    n = ps.neutral_steps
    levels = uniform_levels(n, cap)
    if ps.low_light_steps > 1:
        levels = sorted(set(levels) | set(shadow_levels(
            ps.low_light_steps, transfer, max_cv=cap,
            max_signal=ps.low_light_signal, bias=ps.low_light_bias)))
    return sort_patches([(v, v, v) for v in levels], ps.order, transfer, warm_tau=warm_tau)


def build_neutral_verify_set(ps: PatchSizes, transfer: Transfer, *,
                             warm_tau: Optional[int] = None,
                             max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The compact grey-axis sanity ramp for gray-wb verification. ``max_cv`` caps the range."""
    cap = max_cv if max_cv is not None else transfer.max_cv
    levels = uniform_levels(ps.neutral_steps, cap)
    return sort_patches([(v, v, v) for v in levels], ps.order, transfer, warm_tau=warm_tau)


def build_verify_set(ps: PatchSizes, transfer: Transfer, *,
                     warm_tau: Optional[int] = None,
                     max_cv: Optional[int] = None) -> list[tuple[int, int, int]]:
    """The verify sanity set — a LIGHTER ramp (grey + RGBCMY at full + half saturation), NOT the
    dense volumetric build set. It confirms grayscale tracking + the gamut hues at practical and
    saturated levels at a normal verification resolution (and feeds the dashboard's saturation
    sweeps), instead of re-measuring the whole cube the build already used. ``max_cv`` caps the
    range (HDR peak; so verify never asks for an above-peak highlight that would read clipped)."""
    return ramp_patches(transfer, steps=ps.verify_steps, saturations=ps.verify_saturations,
                        spacing=ps.raw_spacing, include_secondaries=True,
                        order=ps.order, warm_tau=warm_tau, max_cv=max_cv)


# The patch sets each flow MEASURES, keyed by measure-stage role (so a plan/preview can show
# the run's size before any measurement). build-correction measures nothing through spotread.
_FLOW_PATCH_STAGES: dict[str, tuple[str, ...]] = {
    "full": ("raw", "post-mhc", "gray-wb", "verify"),
    "mhc-only": ("raw", "verify-ramp"),
    "3dlut-only": ("post-mhc", "verify"),
    "gray-wb": ("gray-wb", "verify-neutral"),
}
_PATCH_BUILDERS = {"raw": build_ramp_set, "verify-ramp": build_ramp_set,
                   "post-mhc": build_volumetric_set, "verify": build_verify_set,
                   "gray-wb": build_neutral_set, "verify-neutral": build_neutral_verify_set}


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


def _measured_primaries(params: Optional[dict[str, float]], white_xy: tuple[float, float]) -> MeasuredPrimaries:
    p = params or {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}
    return MeasuredPrimaries(rx=p["rx"], ry=p["ry"], gx=p["gx"], gy=p["gy"], bx=p["bx"], by=p["by"],
                             wx=white_xy[0], wy=white_xy[1])


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
    gswb = p.get("gswb") or {}
    wd = gswb.get("watchdog") or {}
    analysis = p.get("display_analysis")

    def metric_row(label: str, key: str) -> str:
        return f"<tr><td>{label}</td><td>{v.get(key, '—')}</td></tr>"

    analysis_block = (f"<h2>Display analysis</h2><p>{analysis}</p>" if analysis
                      else "<h2>Display analysis</h2><p class='muted'>"
                           "(the calibrating assistant adds a short panel analysis here — "
                           "strengths, weaknesses, and why it behaves as it does.)</p>")
    watchdog_block = ""
    if wd:
        cls = "warn" if wd.get("trips") else "ok"
        watchdog_block = (f"<p class='{cls}'>GS+WB tweak {wd.get('magnitude')} (trend {wd.get('trend')}) — "
                          f"watchdog: {wd.get('recommendation')}.</p>")
    return (
        "<!doctype html><meta charset='utf-8'><title>DLC report</title>"
        "<style>body{font-family:system-ui;margin:2rem;color:#1a1a1a;max-width:48rem}"
        "table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #ccc;padding:.35rem .8rem;text-align:left}"
        "th{background:#f4f4f4}.muted{color:#888}.warn{color:#b54}.ok{color:#393}code{background:#f0f0f0;padding:.1rem .3rem}</style>"
        f"<h1>DesktopLUT Calibrator — {p.get('display')} · {p.get('mode')} · {p.get('flow')}</h1>"
        f"<p>Target <code>{p.get('target')}</code> · {p.get('date')} · "
        f"3D LUT {'converged' if lut.get('converged') else 'best-effort'} "
        f"(max dE {lut.get('best_max_de', '—')})</p>"
        "<h2>Verification (CIEDE2000)</h2><table><tr><th>Metric</th><th>After</th></tr>"
        + metric_row("Average dE2000", "avg_de2000")
        + metric_row("P95 dE2000", "p95_de2000")
        + metric_row("Max dE2000", "max_de2000")
        + metric_row("White dE2000", "white_de2000")
        + metric_row("Grayscale avg dE2000", "grayscale_avg_de2000")
        + "</table>"
        + watchdog_block
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
        adaptive_planning=adaptive_planning)
    return calib.run(flow)


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
                       help="grey-axis ramp steps for GS+WB / gray-wb (default 17).")
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
    parser.add_argument("--persistent-meter", action="store_true", dest="persistent_meter",
                        help="OPT-IN: drive ONE long-lived interactive spotread across the whole "
                             "pass (calibrate once, one reading per trigger) instead of re-spawning "
                             "+ re-calibrating spotread per read. ~2-3x faster per read; validate the "
                             "raw-pipe transport against the meter once before trusting it.")
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
    parser.add_argument("--auto", action="store_true", help="auto-adjudicate (no pauses)")
    parser.add_argument("--skip-gswb", action="store_true", dest="skip_gswb",
                        help="full flow: skip the final GS+WB tweak (deferred stage targets the "
                             "wrong layer) — runs ICC→3D-LUT as one cohesive unit (one rollback "
                             "unit, one verify gate)")
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
    adjudicator: Adjudicator = AutoAdjudicator() if args.auto else MappingAdjudicator(decisions)

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
    bit_depth = args.bit_depth if args.bit_depth is not None else (10 if normalize_mode(args.mode) == "HDR" else 8)
    presenter = None
    persistent_meter = None
    measure: Optional[MeasureFn] = None
    if args.flow != "build-correction":
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
        if args.flow != "characterize" and dip_rec is not None and dip_rec.settle_seconds is not None:
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
            presenter = DogegenPresenter(DogegenPatchDisplay(Path(dogegen_path), normalize_mode(args.mode),
                                                             bit_depth=bit_depth),
                                         settle_seconds=presenter_settle, place_rect=place_rect)
        # The active correction comes from the store first (a freshly probe-matched .ccmx)
        # then the profile — so a build-correction run is picked up without editing the YAML.
        store = CorrectionStore.load(correction_store_path(profile, ctx.root))
        correction = active_correction(profile, store, profile.display_for(args.monitor).name)
        ccmx = Path(correction) if correction else None
        if args.persistent_meter:
            # Fast path: ONE interactive spotread, identical instrument config to the one-shot
            # (same port + correction) so it is a true drop-in to A/B against. Closed in finally.
            if argyll is None:
                raise SystemExit("--persistent-meter requires profile paths.argyll (the spotread executable)")
            persistent_meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=ccmx))
            measure = make_persistent_spotread_meter(presenter=presenter, persistent=persistent_meter)
        else:
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
                presenter.close()          # drops the dogegen socket / kills a spawned window
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

    calib = Calibration(ctx=ctx, profile=profile, monitor=args.monitor, mode=args.mode,
                        controller=controller, measure=measure, adjudicator=adjudicator,
                        bit_depth=bit_depth, force=args.force, patch_sizes=patch_sizes,
                        characterize_config=characterize_config, decision_overrides=overrides,
                        skip_gswb=args.skip_gswb, adaptive_planning=args.adaptive_planning,
                        stall_kill_hook=_stall_kill, pause_handler=_pause_park,
                        enable_watchdog=True)
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
