"""The LLM seam layer — seam ids, the adjudication request/decision forms, and the
three adjudicators (v2-design-notes §5; moved verbatim out of ``calibrate.py`` in the
fable Phase 7b decomposition — the seam layer is adjudicator-agnostic and dependency-free,
so it stands alone; the orchestrator re-exports every name for back-compat).

**The LLM seam = the** :class:`Adjudicator`. At each ``⚑`` point the deterministic core
hands the adjudicator a structured :class:`AdjudicationRequest` (a digest + a question +
the allowed choices + the *core's recommendation*) and gets back a :class:`Decision`.
See the :mod:`dlc.calibrate` module docstring for how the three implementations map onto
run modes (sim/CI, live pause/resume, supervised), and the DESIGN LAW block below for
what may — and may not — be decided without a judge.

Spine-tier: stdlib only, imports nothing from the rest of DLC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

__all__ = [
    "Decision",
    "AdjudicationRequest",
    "AdjudicationRequired",
    "Adjudicator",
    "AutoAdjudicator",
    "MappingAdjudicator",
    "SupervisedAdjudicator",
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
SEAM_BACKUP = "backup_capture"     # the pre-run durable settings backup could not be captured
SEAM_PIPE = "pipe_down"            # the DesktopLUT calibration pipe is unreachable at preflight
# NOTE: there is deliberately NO check-in seam. A §12 check-in is a NON-BLOCKING evidence packet
# for the LLM (see Calibration._maybe_timed_checkin), never an adjudicated yes/no — it must never
# gate the spine.

# The benign, happy-path choice at a seam — the recommendation a clean run carries. A
# recommendation OUTSIDE this set means the deterministic core wants to stop/redo something.
# CAUTION (Design Law, see below): "benign" means *the core has a sensible default*,
# NOT *no LLM needed*. A benign ``accept`` on a passing verify is still a judgment the LLM must
# make. ``SupervisedAdjudicator`` currently auto-accepts these — the known divergence Task #1
# fixes (escalate benign judgment seams to the LLM). The default ``MappingAdjudicator`` already
# routes every seam, benign or not, to the LLM. (Check-ins are NOT seams — they are non-blocking
# evidence packets the LLM consumes out of band; see ``Calibration._maybe_timed_checkin``.)
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
# CHECK-INS ARE NOT SEAMS. A §12 check-in (``Calibration._maybe_timed_checkin`` / the
# measure-loop quartile ping) NEVER pauses the spine and carries NO recommendation. It is the
# spine collecting the evidence since the last check-in — warnings, the max ΔE actually read,
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

    **KNOWN DIVERGENCE from the Design Law (see above + Task #1).** This was
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
