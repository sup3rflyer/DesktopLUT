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

import json
import shutil
from argparse import Namespace
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

import numpy as np

from . import calibration_profile as cp
from .controller import CalibrationController, normalize_mode
from .correction_store import CorrectionRecord, CorrectionStore
from .engine.patches import Transfer, ramp_patches, sort_patches, tube_patches
from .measure_loop import (
    MeasureFn,
    MeasureLoopConfig,
    MeasurePatch,
    MeasureLoopResult,
    run_measure_loop,
)
from .metrics import score_samples, summarize_metrics
from .mhc import parse_ti3
from .optimize import OptimizeConfig, ProbeFn, optimize_cube
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


# ---------------------------------------------------------------------------
# Adjudication — the LLM seam
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """The judgment returned at a seam. ``choice`` is one of the request's
    ``options``; ``note`` carries the LLM's reasoning for the audit trail."""

    choice: str
    note: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"choice": self.choice, "note": self.note}


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

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "seam": self.seam, "stage": self.stage,
                "question": self.question, "options": list(self.options),
                "recommendation": self.recommendation, "digest": self.digest}


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
        return Decision(request.recommendation, note="auto: accepted core recommendation")


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
    """Patch-set sizes per stage — the core's mechanics, tunable for tests (tiny)
    vs real runs (dense). All sets are thermally ordered by the generator."""

    raw_ramp_steps: int = 17        # grey + RGBCMY ramp for the MHC matrix + base 1D
    cube_size: int = 9              # volumetric cube axis for the 3D LUT
    tube_size: int = 17             # neutral-axis core resolution
    tube_radius: int = 2            # Manhattan radius of the neutral tube
    neutral_steps: int = 17         # grey-axis ramp for the GS+WB tweak / gray-wb flow


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
        run_date: Optional[date] = None,
        force: bool = False,
        dummy_icc: str = "sRGB.icm",
        patch_sizes: Optional[PatchSizes] = None,
        white_fn: Optional[cp.WhiteFn] = None,
        probe_launcher: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
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
        self.run_date = run_date or date.today()
        self.force = force
        self.dummy_icc = dummy_icc
        self.patch_sizes = patch_sizes or PatchSizes()
        self._white_fn = white_fn
        # The correction-build launches Argyll ccxxmake in its own console (live only); an
        # injectable seam keeps tests/sim from spawning a real process.
        self._probe_launcher = probe_launcher or self._default_launch_ccxxmake

        self._state = _common.load_dlc_state(ctx)
        self.calib: dict[str, Any] = self._state.setdefault("calib", {})
        self.calib.setdefault("stages", {})
        self.calib.setdefault("decisions", {})
        self.target_name: Optional[str] = self.calib.get("target")

    # -- persistence ------------------------------------------------------
    def _save(self) -> None:
        self._state["calib"] = self.calib
        self._state.setdefault("monitor", self.monitor)
        self._state.setdefault("mode", self.mode)
        _common.save_dlc_state(self.ctx, self._state)

    def _stage(self, key: str, run_fn: Callable[[], StageOutcome]) -> StageOutcome:
        """Run (or replay) a memoised stage. A recorded ``done`` stage is returned
        from the record without re-doing the work — so a resume after an
        adjudication pause never re-measures."""
        rec = self.calib["stages"].get(key)
        if rec and rec.get("status") == "done" and not self.force:
            return StageOutcome.from_record(rec)
        outcome = run_fn()
        self.calib["stages"][key] = outcome.as_record()
        self._save()
        return outcome

    # -- the seam ---------------------------------------------------------
    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        """Ask the adjudicator and persist the decision (audit trail + resume
        seed). Propagates :class:`AdjudicationRequired` to pause a live run."""
        if request.key in self.calib["decisions"] and not self.force:
            d = self.calib["decisions"][request.key]
            return Decision(d["choice"], d.get("note"))
        decision = self.adjudicator.adjudicate(request)   # may raise AdjudicationRequired
        self.calib["decisions"][request.key] = {
            **decision.as_dict(), "seam": request.seam, "question": request.question}
        self.ctx.log(f"seam {request.key}: {decision.choice}"
                     + (f" ({decision.note})" if decision.note else ""))
        self._save()
        return decision

    def _abort_if(self, decision: Decision, *, stage: str, message: str) -> Decision:
        """Honour an LLM 'abort' verdict at a seam — end the flow cleanly. (The
        AutoAdjudicator never returns 'abort', so autonomous runs are unaffected.)"""
        if decision.choice == "abort":
            raise CalibrationAborted(StageOutcome(
                stage, "aborted", digest={"message": message, "decision_note": decision.note}))
        return decision

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

    # -- white-point resolution (HANDOFF item 7) --------------------------
    def _correction_store(self) -> CorrectionStore:
        """The cross-run, per-display correction store (profile-adjacent / runs-parent)."""
        return CorrectionStore.load(correction_store_path(self.profile, self.ctx.root))

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
        cfg = self.loop_config or MeasureLoopConfig(
            cold_channel=self.display.temperamental_channel,
            settle_threshold=(self.display.settle_delta_de or 0.3) / 100.0,
        )
        meas_dir = self.ctx.root / "measurements"
        return run_measure_loop(
            patches=patches, transfer=transfer, measure=self.measure, config=cfg,
            ti3_path=meas_dir / ti3_name, ndjson_path=meas_dir / ndjson_name,
        )

    def _probe_fn(self) -> ProbeFn:
        """The re-measure probe for the correction machine. Injected in tests;
        otherwise present each driven signal (code values, no LUT) and read it via
        the measure seam — the fidelity-ladder tier-2 path."""
        if self._probe is not None:
            return self._probe
        transfer = self._transfer()
        max_cv = transfer.max_cv

        def probe(signals: np.ndarray) -> np.ndarray:
            sig = np.clip(np.asarray(signals, dtype=float).reshape(-1, 3), 0.0, 1.0)
            out = np.zeros((len(sig), 3), dtype=float)
            for i, s in enumerate(sig):
                rgb = tuple(int(round(c * max_cv)) for c in s)
                patch = MeasurePatch(label=f"probe{i:04d}", rgb=rgb,  # type: ignore[arg-type]
                                     signal=(float(s[0]), float(s[1]), float(s[2])),
                                     role="measurement", bit_depth=transfer.bit_depth)
                reading = self.measure(patch)
                out[i] = reading.xyz if reading.xyz is not None else (0.0, 0.0, 0.0)
            return out

        return probe

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
            digest = {"monitor": self.monitor, "mode": self.mode, "display": self.display.name,
                      "argyll_display": self.display.argyll_display, "mapping_ok": mapping_ok,
                      "seen_monitors": sorted(set(seen_monitors)),
                      "correction": staleness.as_dict(),
                      "correction_from_store": store_made is not None}
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
        target = self.display.target_name(self.mode)
        if not target:
            raise CalibrationAborted(StageOutcome(
                "resolve-target", "aborted",
                digest={"message": f"display {self.monitor} has no {self.mode} target configured"}))
        spec = self.profile.target(target)
        if spec.is_hdr:
            # SDR-first in v1: HDR target acknowledged but not finalised here.
            raise CalibrationAborted(StageOutcome(
                "resolve-target", "aborted",
                digest={"message": "HDR finalisation is post-v1 (SDR-first); pick an SDR target or flow."}))
        self.target_name = target
        self.calib["target"] = target
        self._save()
        digest = {"flow": self.calib.get("flow"), "target": target,
                  "colorspace": spec.colorspace, "transfer": f"power γ{spec.gamma}",
                  "white": f"{spec.white.intent} ({spec.white.method})",
                  "white_nits": spec.luminance_nits}
        self._abort_if(self.adjudicate(AdjudicationRequest(
            key="resolve-target:plan", seam=SEAM_PLAN, stage="resolve-target",
            question=(f"Plan: {self.calib.get('flow')} calibration of monitor {self.monitor} "
                      f"({self.display.name}) to target '{target}' "
                      f"(γ{spec.gamma}, {spec.white.intent}, {spec.luminance_nits:g} nits). Proceed?"),
            options=("approve", "abort"), recommendation="approve", digest=digest)),
            stage="resolve-target", message="plan vetoed by the operator")
        return StageOutcome("resolve-target", "done", digest=digest, data={"target": target})

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
        if outcome.data.get("needs_adjudication"):
            self._abort_if(self.adjudicate(AdjudicationRequest(
                key=f"{key}:escalation", seam=SEAM_MEASURE, stage=key,
                question=outcome.data.get("question") or "measurement did not fully settle — accept or retry?",
                options=("accept", "abort"), recommendation="accept", digest=outcome.digest)),
                stage=key, message="aborted on unsettled measurement")
        return outcome

    def stage_build_install_mhc(self, raw_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            spec = self._spec()
            # Derive MHC params from the raw TI3 (reuses the proven build-mhc stage:
            # measured primaries + native-white→D65 matrix + tone-only base 1D).
            args = Namespace(run=self.ctx.root, monitor=self.monitor, mode=self.mode,
                             simulate=False, gamma=spec.gamma, source_ti3=raw_ti3)
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
                                               base["points"], base["deviations"])
            applied = self.controller.apply_mhc(self.monitor, self.mode)
            verified = self.controller.verify_mhc(self.monitor, self.mode)
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

    def stage_build_install_3dlut(self, post_ti3: str) -> StageOutcome:
        def run() -> StageOutcome:
            target = self._engine_target()
            samples = parse_ti3(Path(post_ti3))
            signals = np.array([s.rgb for s in samples], dtype=float)
            measured = np.array([s.xyz for s in samples], dtype=float)
            cube_path = str(self.ctx.root / "generated" / f"final_{self.mode.lower()}.cube")
            result = optimize_cube(target=target, probe=self._probe_fn(), signals=signals,
                                   measured_xyz=measured, config=self.optimize_config)
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
                self.controller.set_correction_grayscale(self.monitor, self.mode, point_count, points, deviations)
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
            metrics, lum = score_samples(samples, gamma=spec.gamma)
            summary = summarize_metrics(phase="verification", iteration=0, source=Path(verify_ti3),
                                        patch_metrics=metrics, target_luminance=lum,
                                        metrics_path=Path(verify_ti3), patches_path=Path(verify_ti3))
            q = self.profile.quality
            within = (summary.avg_de2000 <= q.avg_de2000 and summary.p95_de2000 <= q.p95_de2000
                      and summary.max_de2000 <= q.max_de2000 and summary.white_de2000 <= q.white_de2000)
            worst = sorted(metrics, key=lambda m: m.de2000, reverse=True)[:5]
            digest = {"avg_de2000": round(summary.avg_de2000, 3), "p95_de2000": round(summary.p95_de2000, 3),
                      "max_de2000": round(summary.max_de2000, 3), "white_de2000": round(summary.white_de2000, 3),
                      "grayscale_avg_de2000": round(summary.grayscale_avg_de2000, 3),
                      "patch_count": summary.patch_count, "within_quality": within,
                      "quality_targets": q.as_dict(),
                      "worst": [{"rgb": [round(c, 3) for c in m.rgb], "de2000": round(m.de2000, 2)} for m in worst]}
            return StageOutcome("verify", "done", digest=digest,
                                data={"within_quality": within, "metrics": {
                                    "avg_de2000": summary.avg_de2000, "p95_de2000": summary.p95_de2000,
                                    "max_de2000": summary.max_de2000, "white_de2000": summary.white_de2000}})

        outcome = self._stage("verify", run)
        self.adjudicate(AdjudicationRequest(
            key="verify:accept", seam=SEAM_VERIFY, stage="verify",
            question=("verification " + ("meets" if outcome.data.get("within_quality") else "is outside")
                      + " the quality targets — accept the result, or iterate?"),
            options=("accept", "iterate"),
            recommendation="accept" if outcome.data.get("within_quality") else "iterate",
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
        path = self._tweak_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, indent=2), encoding="utf-8")
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

        # Copy the installed cube into the deliverable folder.
        cube_src = sdat("build-install-3dlut").get("cube_path")
        cube_out = None
        if cube_src and Path(cube_src).exists():
            cube_out = results_dir / f"{results_dir.name}.cube"
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
                            data={"results_dir": str(results_dir), "report_path": str(report_json)},
                            artifacts=[str(report_json), str(report_html)])

    # ====================================================================
    # Flows
    # ====================================================================
    def run(self, flow: str) -> CalibrationResult:
        self.calib["flow"] = flow
        self._save()
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
            if flow == "hdr":
                raise CalibrationAborted(StageOutcome(
                    "resolve-target", "aborted",
                    digest={"message": "HDR is the post-v1 goal; v1 is SDR-first. Use 'full' on an SDR target."}))
            raise ValueError(f"unknown flow {flow!r} (have: {sorted(FLOWS)})")
        except CalibrationAborted as exc:
            self.calib["stages"][exc.outcome.stage] = exc.outcome.as_record()
            self._save()
            return CalibrationResult(
                flow=flow, monitor=self.monitor, mode=self.mode, target=self.target_name,
                status="aborted", stages=list(self.calib["stages"].keys()),
                results_dir=None, report_path=None,
                digest={"aborted_at": exc.outcome.stage, "message": exc.outcome.digest.get("message"),
                        "reason": str(exc)})

    def _ramp_patches(self) -> list[tuple[int, int, int]]:
        return ramp_patches(self._transfer(), steps=self.patch_sizes.raw_ramp_steps,
                            saturations=(1.0,), order="thermal")

    def _volumetric_patches(self) -> list[tuple[int, int, int]]:
        ps = self.patch_sizes
        return tube_patches(self._transfer(), cube_size=ps.cube_size, tube_size=ps.tube_size,
                            tube_radius=ps.tube_radius, order="thermal")

    def _neutral_patches(self) -> list[tuple[int, int, int]]:
        t = self._transfer()
        steps = self.patch_sizes.neutral_steps
        levels = [round(i * t.max_cv / (steps - 1)) for i in range(steps)]
        return sort_patches([(v, v, v) for v in levels], "thermal", t)

    def _finish(self, *, analysis: Optional[str] = None) -> CalibrationResult:
        rep = self.stage_report(analysis=analysis)
        return CalibrationResult(
            flow=self.calib.get("flow"), monitor=self.monitor, mode=self.mode, target=self.target_name,
            status="completed", stages=list(self.calib["stages"].keys()),
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
        post = self.stage_measure(role="post-mhc", patches=self._volumetric_patches(),
                                  ti3_name="post_mhc.ti3", ndjson_name="post_mhc.ndjson")
        self.stage_build_install_3dlut(post.data["ti3"])
        gw = self.stage_measure(role="gray-wb", patches=self._neutral_patches(),
                                ti3_name="gray_wb.ti3", ndjson_name="gray_wb.ndjson")
        self.stage_gswb_tweak(gw.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._volumetric_patches(),
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
        post = self.stage_measure(role="post-mhc", patches=self._volumetric_patches(),
                                  ti3_name="post_mhc.ti3", ndjson_name="post_mhc.ndjson")
        self.stage_build_install_3dlut(post.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._volumetric_patches(),
                                 ti3_name="verify.ti3", ndjson_name="verify.ndjson")
        self.stage_verify(ver.data["ti3"])
        return self._finish()

    def _flow_gray_wb(self) -> CalibrationResult:
        self.stage_preflight()
        self.stage_resolve_target()
        self.stage_whitepoint()
        self._require_stack(need_mhc=True, need_lut=True)
        self.stage_brightness()
        gw = self.stage_measure(role="gray-wb", patches=self._neutral_patches(),
                                ti3_name="gray_wb.ti3", ndjson_name="gray_wb.ndjson")
        self.stage_gswb_tweak(gw.data["ti3"])
        ver = self.stage_measure(role="verify", patches=self._neutral_patches(),
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
        return CalibrationResult(
            flow="build-correction", monitor=self.monitor, mode=self.mode, target=None,
            status="completed", stages=list(self.calib["stages"].keys()),
            results_dir=None, report_path=None,
            digest={"correction": rec.correction_file if rec else None,
                    "correction_made": rec.correction_made if rec else None,
                    "white_spd": rec.spd_file if rec else None,
                    "probe_match": (self.calib["stages"].get("probe-match") or {}).get("digest")})

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
    "hdr": "(post-v1) Rec.2020/PQ — SDR-first in v1",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _xy(xyz: Sequence[float]) -> tuple[float, float]:
    total = sum(xyz)
    if total <= 0:
        return (0.0, 0.0)
    return (xyz[0] / total, xyz[1] / total)


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
        patch_sizes=patch_sizes, run_date=run_date, force=force)
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
    parser.add_argument("--raw-steps", type=int, default=None, dest="raw_steps",
                        help="override the raw/verify ramp step count (MHC patch density; "
                             "default 17 ≈ 113 patches). Lower ⇒ shorter shakedown run.")
    parser.add_argument("--dogegen-server", default=None, dest="dogegen_server",
                        metavar="HOST:PORT",
                        help="drive a PERSISTENT dogegen daemon (dlc.dogegen_server) over a local "
                             "socket instead of spawning a window per step — start it once, Alt+Enter "
                             "it fullscreen, reuse it across the whole run (required for 10-bit).")
    parser.add_argument("--decide", action="append", default=[], metavar="KEY=CHOICE",
                        help="record a seam decision (repeatable) then run/resume")
    parser.add_argument("--auto", action="store_true", help="auto-adjudicate (no pauses)")
    parser.add_argument("--force", action="store_true", help="ignore stage memoisation")
    args = parser.parse_args(argv)

    profile = cp.load_profile(args.profile)
    ctx = open_run(args.run) if args.run and (args.run / "manifest.json").exists() \
        else create_run(normalize_mode(args.mode), display=profile.display_for(args.monitor).name,
                        run_dir=args.run)

    # Seed decisions from the run-record + any new --decide flags.
    state = _common.load_dlc_state(ctx)
    recorded = (state.get("calib", {}) or {}).get("decisions", {})
    decisions = {k: Decision(v["choice"], v.get("note")) for k, v in recorded.items()}
    for spec in args.decide:
        key, _, choice = spec.partition("=")
        decisions[key.strip()] = Decision(choice.strip(), note="cli")
    adjudicator: Adjudicator = AutoAdjudicator() if args.auto else MappingAdjudicator(decisions)

    from .measure_loop import DogegenPresenter, SocketPresenter, make_spotread_meter  # lazy: live only
    from .argyll import Argyll
    from .dogegen import DogegenPatchDisplay
    from .measure_rgbw import resolve_spotread_instrument_port

    controller = CalibrationController.connect()

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
        if args.dogegen_server:
            # Reuse a persistent, operator-fullscreened dogegen window across invocations
            # (no respawn/flash) — the daemon owns the dogegen process + its bit-depth mode.
            host, _, srv_port = args.dogegen_server.partition(":")
            presenter = SocketPresenter(host or "127.0.0.1", int(srv_port or 28930))
        else:
            dogegen_path = profile.paths.get("dogegen")
            if not dogegen_path:
                raise SystemExit("profile paths.dogegen is required for measuring flows "
                                 "(the patch generator executable, e.g. third_party/dogegen/dogegen.exe)")
            presenter = DogegenPresenter(DogegenPatchDisplay(Path(dogegen_path), normalize_mode(args.mode),
                                                             bit_depth=bit_depth))
        # The active correction comes from the store first (a freshly probe-matched .ccmx)
        # then the profile — so a build-correction run is picked up without editing the YAML.
        store = CorrectionStore.load(correction_store_path(profile, ctx.root))
        correction = active_correction(profile, store, profile.display_for(args.monitor).name)
        measure = make_spotread_meter(presenter=presenter, spotread=argyll, port=port,
                                      output_dir=ctx.root / "measurements" / "probe",
                                      ccmx_or_ccss=Path(correction) if correction else None)
    patch_sizes = PatchSizes(raw_ramp_steps=args.raw_steps) if args.raw_steps else None
    calib = Calibration(ctx=ctx, profile=profile, monitor=args.monitor, mode=args.mode,
                        controller=controller, measure=measure, adjudicator=adjudicator,
                        bit_depth=bit_depth, force=args.force, patch_sizes=patch_sizes)
    try:
        try:
            result = calib.run(args.flow)
        except AdjudicationRequired as req:
            print(json.dumps({"status": "adjudication_required", "request": req.request.as_dict(),
                              "run": str(ctx.root)}, indent=2))
            return 10
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.status == "completed" else 1
    finally:
        # Close the presenter so a spawned dogegen never orphans on pause/exit. For the
        # persistent daemon (SocketPresenter) this just drops our socket — the daemon's
        # fullscreen window persists across invocations by design.
        if presenter is not None:
            try:
                presenter.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
