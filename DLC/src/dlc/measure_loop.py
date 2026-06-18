"""Adaptive, self-healing measurement loop (v2-design-notes §6, HANDOFF §0.5).

Turns a thermally-ordered patch set (:mod:`dlc.engine.patches`) into a clean
``.ti3`` (final *accepted* reads, for the LUT engines) **plus** a streaming
``measurements.ndjson`` (every probe read, append-only) — surviving panel drift
via three mechanisms the design calls for:

1. **Warm-up-settle** — hold a neutral stimulus biased toward the cold channel
   (blue, from the profile ``quirks``) and re-read until consecutive reads agree
   within ``settle_threshold`` for ``settle_required`` reads → "panel warm". The
   last settled read becomes the live *drift reference*.
2. **Per-patch read policy (DIP-driven, no fixed count)** — a *single*
   adaptive-integration read by default (as professional tools do); more *averaged*
   reads only where the Display+Instrument Profile's measured noise model says SNR
   needs it at that luminance; and continued reads until the sample standard error
   falls within ``read_tolerance_de``. A patch that reads abnormally (won't tighten
   within ~2× its DIP target) is **flagged for adjudication — never silently capped.**
   The accepted value is the mean of the valid reads (the averaging IS the SNR win).
3. **Interleaved drift reference (appended re-measure)** — every
   ``neutral_interval`` patches, re-read the neutral reference and compare to the
   warm reference. A slow warm-up creep beats a per-step settle threshold, so the
   *absolute* comparison catches it: patches measured since the last clean
   checkpoint are flagged "taken cold", re-settled, and **redone once stable**.

A few bad patches never cancel the run (selective re-measure, not abort). A point
that won't settle (e.g. blue past its physical ceiling) is surfaced as a
**judgment digest** for the LLM — the loop never silently accepts or silently
gives up.

**Three-consumer model (load-bearing).** The *core* (this loop) reacts per-patch
in real time. The *human* watches mission control (item 3 tails the ndjson). The
*LLM* reads only the ``digest`` at the end / on escalation — it never tails the
stream. ``MeasureLoopResult.digest`` is that boundary object.

This module is **numpy-free** on purpose (it reuses pure-stdlib :mod:`dlc.drift`,
:mod:`dlc.metrics`, and :mod:`dlc.engine.patches`), so ``import dlc.measure_loop``
stays light and the loop runs in tests without the engine extras installed. The
presenter/meter for live runs lives behind a single :data:`MeasureFn` seam, so the
loop itself touches neither a display nor a meter.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from .dip import DisplayInstrumentProfile
from .drift import Channel, coldest_channel_from_xyz, evaluate_drift
from .engine.patches import Patch, Transfer, to_signal
from .events import EventWriter, RunLog
from .liveness import Liveness
from .metrics import delta_e2000, xyz_to_lab

__all__ = [
    "MeasurePatch",
    "Reading",
    "MeasureFn",
    "MeasureLoopConfig",
    "AcceptedRead",
    "MeasureLoopResult",
    "run_measure_loop",
    "biased_neutral",
    "write_ti3",
    "Presenter",
    "DogegenPresenter",
    "SocketPresenter",
    "make_spotread_meter",
    "make_persistent_spotread_meter",
    "SyntheticPanel",
]

_CHANNEL_INDEX: dict[Channel, int] = {"R": 0, "G": 1, "B": 2}


# ---------------------------------------------------------------------------
# Data model — the loop's vocabulary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MeasurePatch:
    """One stimulus to present + read. ``rgb`` are integer code values at
    ``bit_depth``; ``signal`` is the normalized ``[0, 1]`` triple (the LUT /
    target domain). ``role`` is ``measurement`` | ``warmup`` | ``neutral_ref``.
    ``seq`` is the position in the main pass (``-1`` for warm-up / reference)."""

    label: str
    rgb: tuple[int, int, int]
    signal: tuple[float, float, float]
    role: str = "measurement"
    bit_depth: int = 10
    seq: int = -1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Reading:
    """A single probe read of one patch. ``xyz`` is absolute CIE XYZ (cd/m^2 for
    Y); ``yxy`` is Argyll's ``Y x y`` when available. ``raw`` carries provenance
    (command, returncode, spectral file …) for the audit stream."""

    xyz: Optional[tuple[float, float, float]]
    yxy: Optional[tuple[float, float, float]] = None
    ok: bool = True
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def nits(self) -> Optional[float]:
        return self.xyz[1] if self.xyz is not None else None


# Present + read in one call. The loop owns *no* display/meter; this is the seam.
MeasureFn = Callable[[MeasurePatch], Reading]


@dataclass(frozen=True)
class MeasureLoopConfig:
    """Loop tunables. *Physical facts* (``cold_channel``, settle tolerance) come from
    the profile / the Display+Instrument Profile (DIP); the **per-patch read budget is
    deliberately NOT a fixed count.** A single adaptive-integration read by default (as
    professional tools do), escalating to more *averaged* reads only where the DIP's
    measured noise model says SNR needs it, and **escalating-with-a-flag — never a silent
    cap** — when a patch reads abnormally. ``read_tolerance_de`` / ``min_reads`` /
    ``abnormal_reads`` are the only read-policy knobs; how many reads a given patch
    actually takes is decided per patch from the measured noise, not a constant.
    """

    # Warm-up-settle ---------------------------------------------------------
    warmup_signal: float = 0.5          # neutral grey level for warm-up / reference
    warmup_bias_signal: float = 0.02    # extra signal on the cold channel
    cold_channel: Optional[Channel] = None  # from profile quirks / DIP; None → auto-detect
    settle_threshold: float = 0.003     # channel-balance Δ between consecutive reads
    settle_required: int = 3            # consecutive in-tolerance reads ⇒ "warm"
    max_warmup_reads: int = 24          # not settled within this ⇒ escalate to the LLM (a FLAG, not a silent cap)

    # Thermal preheat (soak-into-calibration) -------------------------------
    preheat: str = "auto"               # "auto" (soak any CHARACTERIZED panel — convergent included),
    #                                     "always", or "never". The soak parks the panel at its OPERATING
    #                                     load (this run's own patch set) before measuring, then rides it
    #                                     (the golden-ratio ordering holds the load). It SELF-DEACTIVATES
    #                                     on an already-warm panel, so the convergent/SDR case is a cheap
    #                                     "just to be safe" warm cycle; only an uncharacterized (no-DIP) or
    #                                     compromised panel falls back to the static-grey settle gate.
    preheat_k_start: float = 1.6        # soak luminance overshoot while warm-in is measured (1.0 ⇒ none)
    rewarm_max_blocks: int = 6          # bounded reactive re-soak on a drift episode (the rare fallback,
    #                                     fired only when the interleaved drift checkpoint trips — not an
    #                                     unconditional every-patch interleave)

    # Interleaved drift reference (appended re-measure) ----------------------
    neutral_interval: int = 8           # measurement patches between neutral re-reads (DIP may override)
    drift_threshold: float = 0.004      # channel-balance Δ vs the warm reference (DIP may override)

    # Per-patch read policy (single-read default + DIP-driven escalation) -----
    read_tolerance_de: float = 0.2      # target standard error of the mean (CIEDE2000) per patch
    min_reads: int = 1                  # single adaptive-integration read by default (pro-standard)
    abnormal_reads: int = 16            # reads past ~2× the DIP target ⇒ FLAG for adjudication (never a silent cap)
    outlier_factor: float = 3.0         # a read >factor×σ from the patch median is a glitch ⇒ rejected, not averaged in
    outlier_floor_de: float = 0.5       # never reject within this ΔE of the median (a σ-independent floor)

    # Selective re-measure budget -------------------------------------------
    remeasure_cap: int = 256            # total appended re-measures allowed


@dataclass
class AcceptedRead:
    """The final accepted read for one measurement patch (what the ``.ti3``
    keeps). Re-measures overwrite this in place — never append."""

    patch: MeasurePatch
    xyz: tuple[float, float, float]
    yxy: Optional[tuple[float, float, float]] = None
    reads_taken: int = 1
    immediate_remeasures: int = 0
    appended_remeasures: int = 0
    taken_cold: bool = False
    unstable: bool = False
    usable: bool = True          # False = a sentinel hole (no usable read) — kept OFF the .ti3
    note: Optional[str] = None


@dataclass
class MeasureLoopResult:
    warm: bool
    warmup_reads: int
    reference_xyz: Optional[tuple[float, float, float]]
    patch_count: int
    total_reads: int
    immediate_remeasures: int
    appended_remeasures: int
    drift_episodes: int
    unresolved: list[str]
    white_xyz: Optional[tuple[float, float, float]]
    ti3_path: Optional[str]
    ndjson_path: Optional[str]
    needs_adjudication: bool
    question: Optional[str]
    digest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def biased_neutral(
    signal: float,
    transfer: Transfer,
    *,
    cold_channel: Optional[Channel] = None,
    bias_signal: float = 0.0,
) -> tuple[int, int, int]:
    """Neutral grey at ``signal`` (code values at the transfer's bit depth),
    nudged on the cold channel by ``bias_signal``. Bit-depth-generalized form of
    :func:`dlc.drift.adaptive_gray_patch` (which is 8-bit only)."""

    max_cv = transfer.max_cv
    base = max(0, min(max_cv, round(signal * max_cv)))
    rgb = [base, base, base]
    if cold_channel in _CHANNEL_INDEX and bias_signal:
        idx = _CHANNEL_INDEX[cold_channel]
        rgb[idx] = max(0, min(max_cv, base + round(bias_signal * max_cv)))
    return (rgb[0], rgb[1], rgb[2])


def _agreement_de(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    white: tuple[float, float, float],
) -> float:
    """CIEDE2000 between two reads of the same patch (luminance- *and*
    chromaticity-sensitive, so it catches both glitch kinds). ``white`` is only a
    Lab anchor — both reads share it, so the result is their true difference."""

    wx = white if white and white[1] > 0 else (a[0] or 1.0, max(a[1], 1e-6), a[2] or 1.0)
    return delta_e2000(xyz_to_lab(a, wx), xyz_to_lab(b, wx))


def _mean_xyz(vals: Sequence[tuple[float, float, float]]) -> tuple[float, float, float]:
    """Component-wise mean of XYZ reads — averaging is how repeated reads buy SNR."""
    n = len(vals)
    return (sum(v[0] for v in vals) / n, sum(v[1] for v in vals) / n, sum(v[2] for v in vals) / n)


def _median_xyz(vals: Sequence[tuple[float, float, float]]) -> tuple[float, float, float]:
    """Component-wise median — the outlier-robust anchor for glitch rejection."""
    def med(xs: list[float]) -> float:
        xs = sorted(xs)
        m = len(xs) // 2
        return xs[m] if len(xs) % 2 else 0.5 * (xs[m - 1] + xs[m])
    return (med([v[0] for v in vals]), med([v[1] for v in vals]), med([v[2] for v in vals]))


def write_ti3(
    path: Path,
    accepted: Sequence[AcceptedRead],
    *,
    title: str = "DesktopLUT Calibrator adaptive measurement",
) -> Path:
    """Write the clean CTI3 the LUT engines consume — RGB as 0–100 percent, XYZ
    absolute — matching :func:`dlc.mhc.parse_ti3`. Only final accepted reads."""

    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for item in accepted:
        # A sentinel hole (no usable read → (0,0,0)) must NEVER reach the engine: the MHC
        # matrix / 3D-LUT builders parse this .ti3 with no knowledge of `unstable`, so a black
        # row would poison the build even when the operator/LLM rubber-stamped the escalation.
        # Drop it — a missing training point beats a black-poisoned one (the patch is still
        # flagged in `unresolved` for adjudication).
        if not item.usable:
            continue
        r, g, b = item.patch.signal
        x, y, z = item.xyz
        rows.append(
            " ".join(
                [
                    f"{r * 100:.6f}",
                    f"{g * 100:.6f}",
                    f"{b * 100:.6f}",
                    f"{x:.6f}",
                    f"{y:.6f}",
                    f"{z:.6f}",
                ]
            )
        )
    path.write_text(
        "\n".join(
            [
                "CTI3",
                f"# {title}",
                "BEGIN_DATA_FORMAT",
                "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z",
                "END_DATA_FORMAT",
                f"NUMBER_OF_SETS {len(rows)}",
                "BEGIN_DATA",
                *rows,
                "END_DATA",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


class _NdjsonWriter:
    """Append-only, flat, one JSON object per line — so item 3's renderer can
    ``tail -f`` it. Pins the schema documented in HANDOFF §0.5."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Truncate any stale stream from a prior run of the same name.
            path.write_text("", encoding="utf-8")

    def emit(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

class _Loop:
    """Mutable run state + the orchestration steps. ``run_measure_loop`` is the
    thin public entry point."""

    def __init__(
        self,
        *,
        patches: Sequence[Patch],
        transfer: Transfer,
        measure: MeasureFn,
        config: MeasureLoopConfig,
        ndjson: _NdjsonWriter,
        events: Optional[EventWriter],
        runlog: Optional[RunLog] = None,
        liveness: Optional[Liveness] = None,
        dip: Optional[DisplayInstrumentProfile] = None,
    ) -> None:
        self.transfer = transfer
        self.cfg = config
        self.ndjson = ndjson
        self.events = events
        # The shared run spine (preferred over `events`): mirrors a compact patch_read +
        # progress onto events.jsonl so the dashboard sees the firehose live, phase-stamped.
        self.runlog = runlog
        # The stall guard, shared with the orchestrator. Wrapping `measure` here means EVERY
        # read (warm-up, main, drift, soak) is bracketed by the guard — a stalled loop aborts
        # at its next read, and a wedged read is force-unblocked by the watchdog. Reads that
        # bypass this loop (the build probe) instrument themselves with the same Liveness.
        self.liveness = liveness
        self._live_phase = "measure"
        self.measure = self._instrument(measure)
        # The measured panel+meter model that drives the per-patch read budget. When
        # absent, the loop falls back to the single-read default (trust the instrument's
        # adaptive integration) — variance-based SNR/abnormality needs a DIP to know σ.
        self.dip = dip

        signals = to_signal(patches, transfer)
        width = max(4, len(str(max(0, len(patches) - 1))))
        self.patches: list[MeasurePatch] = [
            MeasurePatch(
                label=f"p{idx:0{width}d}",
                rgb=tuple(int(c) for c in rgb),  # type: ignore[arg-type]
                signal=sig,
                role="measurement",
                bit_depth=transfer.bit_depth,
                seq=idx,
            )
            for idx, (rgb, sig) in enumerate(zip(patches, signals))
        ]

        self.cold_channel: Optional[Channel] = config.cold_channel
        self.reference_xyz: Optional[tuple[float, float, float]] = None
        self.white_xyz: Optional[tuple[float, float, float]] = None

        self.accepted: dict[str, AcceptedRead] = {}
        self.appended_queue: list[MeasurePatch] = []
        self.seq_counter = 0            # running probe-read index (every read)
        self.drift_episodes = 0
        self.remeasure_budget = config.remeasure_cap
        self.warm = False
        self.warmup_reads = 0

    # -- low-level read ----------------------------------------------------

    def _instrument(self, inner: MeasureFn) -> MeasureFn:
        """Bracket every read with the stall guard: ``check`` before (abort if a prior
        stall went unhandled / the watchdog tripped while wedged) and ``progress`` after
        a good read (reset the clock). A failed read is activity, not progress, so a
        failed-read storm still trips the guard. No-op when no liveness is injected."""
        live = self.liveness
        if live is None:
            return inner

        def measured(patch: MeasurePatch) -> Reading:
            live.activity(self._live_phase)
            live.check(self._live_phase)
            reading = inner(patch)
            if reading.ok:
                live.progress(self._live_phase)
            return reading

        return measured

    def _emit_event(self, level: str, event: str, **data: Any) -> None:
        # Prefer the shared spine (phase-stamped, tier-derived); fall back to the legacy
        # event-only writer (characterize / tests) when no runlog was injected.
        if self.runlog is not None:
            self.runlog.emit(level, "measure_loop", event, **data)
        elif self.events is not None:
            self.events.write(level, "measure_loop", event, **data)

    def _mirror_patch_read(self, record: dict[str, Any]) -> None:
        """Mirror one measurements.ndjson read onto the spine as a compact, stream-tier
        ``patch_read`` (the dense record stays in the ndjson). This is the dashboard's
        live firehose + per-read liveness; the LLM digest drops it. No dE here — it's
        target-relative and computed by the scoring/verify stage, not the meter loop."""
        if self.runlog is None:
            return
        xyz = record.get("xyz")
        yxy = record.get("yxy")
        self.runlog.patch_read(
            "measure",
            seq=record.get("seq"),
            role=record.get("role"),
            label=record.get("label"),
            rgb=record.get("rgb"),
            signal=record.get("signal"),
            Y=(round(xyz[1], 4) if xyz else None),
            xy=([round(yxy[1], 5), round(yxy[2], 5)] if yxy else None),
            ok=record.get("ok"),
            disposition=record.get("disposition"),
            read_index=record.get("read_index"),
            drift=record.get("drift"),
        )

    def _emit_progress(self) -> None:
        """A coarse progress tick (stream tier) for the dashboard counters/ETA: how many
        distinct patches are measured out of the total, and the running read count."""
        if self.runlog is None:
            return
        self.runlog.progress(
            "measure",
            patches_done=len(self.accepted),
            patches_total=len(self.patches),
            reads=self.seq_counter,
        )

    def _read(
        self,
        patch: MeasurePatch,
        *,
        phase: str,
        read_index: int,
        accepted: bool,
        agreement_de: Optional[float] = None,
        drift: Optional[dict[str, Any]] = None,
        settle: Optional[dict[str, Any]] = None,
        disposition: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Reading:
        reading = self.measure(patch)
        seq = self.seq_counter
        self.seq_counter += 1
        record: dict[str, Any] = {
            "t": _now(),
            "seq": seq,
            "phase": phase,
            "role": patch.role,
            "label": patch.label,
            "rgb": list(patch.rgb),
            "signal": [round(s, 6) for s in patch.signal],
            "read_index": read_index,
            "xyz": list(reading.xyz) if reading.xyz is not None else None,
            "yxy": list(reading.yxy) if reading.yxy is not None else None,
            "nits": reading.nits,
            "ok": reading.ok and reading.xyz is not None,
            "accepted": accepted,
            "agreement_de": agreement_de,
            "drift": drift,
            "settle": settle,
            "disposition": disposition,
            "note": note,
        }
        if reading.error:
            record["error"] = reading.error
        self.ndjson.emit(record)
        self._mirror_patch_read(record)          # live firehose + per-read liveness on the spine
        if phase != "soak" and read_index == 0 and patch.role == "measurement":
            self._emit_progress()                # one coarse tick per new patch (counters / ETA)
        return reading

    def _update_white(self, xyz: tuple[float, float, float]) -> None:
        if self.white_xyz is None or xyz[1] > self.white_xyz[1]:
            self.white_xyz = xyz

    def _sample_se_de(self, reads: Sequence[tuple[float, float, float]]) -> Optional[float]:
        """Standard error of the mean of these reads, in CIEDE2000. ``None`` for <2
        reads (no spread to estimate). Computed as the RMS perceptual deviation of the
        reads from their mean, divided by √n — so it tightens as reads accumulate, which
        is exactly the statistical stopping signal the read policy waits on."""
        n = len(reads)
        if n < 2:
            return None
        mean = _mean_xyz(reads)
        white = self.white_xyz or mean
        rms = math.sqrt(sum(_agreement_de(r, mean, white) ** 2 for r in reads) / n)
        return rms / math.sqrt(n)

    def _robust_stats(self, reads: Sequence[tuple[float, float, float]],
                      *, sigma: Optional[float] = None):
        """``(robust_mean_xyz, se_de, n_inliers, n_outliers)`` for the reads so far, or
        ``None`` when there are none. With ≥3 reads, a read whose ΔE from the patch
        *median* exceeds ``outlier_factor × σ`` (σ from the DIP, else the sample's own
        median-absolute-deviation) — floored at ``outlier_floor_de`` — is a glitch and
        is **dropped from the mean**, not diluted into it; the SE and inlier count are
        computed on the survivors. Below 3 reads there's nothing to reject against, so
        all count (and a lone read trusts the instrument's adaptive integration)."""
        n = len(reads)
        if n == 0:
            return None
        if n < 3:
            return (_mean_xyz(reads), self._sample_se_de(reads), n, 0)
        med = _median_xyz(reads)
        white = self.white_xyz or med
        spread = sigma
        if spread is None:
            devs = sorted(_agreement_de(r, med, white) for r in reads)
            spread = devs[len(devs) // 2]   # median absolute deviation, in ΔE
        thr = max(self.cfg.outlier_floor_de, self.cfg.outlier_factor * (spread or 0.0))
        inliers = [r for r in reads if _agreement_de(r, med, white) <= thr] or list(reads)
        return (_mean_xyz(inliers), self._sample_se_de(inliers), len(inliers), n - len(inliers))

    # -- warm-up-settle ----------------------------------------------------

    def _warmup_patch(self) -> MeasurePatch:
        rgb = biased_neutral(
            self.cfg.warmup_signal,
            self.transfer,
            cold_channel=self.cold_channel,
            bias_signal=self.cfg.warmup_bias_signal if self.cold_channel else 0.0,
        )
        sig = to_signal([rgb], self.transfer)[0]
        return MeasurePatch(
            label="warmup",
            rgb=rgb,
            signal=sig,
            role="warmup",
            bit_depth=self.transfer.bit_depth,
        )

    def warm_up(self, *, phase: str = "warmup", existing_reference: bool = False) -> tuple[bool, int]:
        """Present the biased-neutral stimulus until ``settle_required``
        consecutive reads agree within ``settle_threshold``. Returns
        ``(settled, reads_used)`` and updates ``self.reference_xyz``.

        ``existing_reference=True`` (a re-settle mid-run) keeps the prior warm
        reference if settling fails, rather than clobbering it with a cold read.
        """
        self._live_phase = phase
        cfg = self.cfg
        prev: Optional[tuple[float, float, float]] = None
        consecutive = 0
        settled = False
        last_good: Optional[tuple[float, float, float]] = None
        reads = 0

        for attempt in range(1, cfg.max_warmup_reads + 1):
            patch = self._warmup_patch()
            consecutive_for_record = consecutive
            reading = self._read(
                patch,
                phase=phase,
                read_index=attempt - 1,
                accepted=False,
                settle={"warm": settled, "consecutive": consecutive_for_record},
            )
            reads += 1
            if reading.xyz is None:
                prev = None
                consecutive = 0
                continue

            last_good = reading.xyz
            # Auto-detect the cold channel from the first usable read, then
            # re-bias subsequent warm-up patches toward it.
            if self.cold_channel is None:
                self.cold_channel = coldest_channel_from_xyz(reading.xyz)
                self._emit_event("INFO", "cold_channel_detected", channel=self.cold_channel)

            if prev is not None:
                ev = evaluate_drift(
                    stabilized_xyz=prev,
                    current_xyz=reading.xyz,
                    delta_threshold=cfg.settle_threshold,
                )
                if not ev.repeat:
                    consecutive += 1
                else:
                    consecutive = 0
                if consecutive >= cfg.settle_required:
                    settled = True
                    prev = reading.xyz
                    break
            prev = reading.xyz

        if last_good is not None:
            self._update_white(last_good)
        if settled and last_good is not None:
            self.reference_xyz = last_good
        elif not existing_reference and last_good is not None:
            # Not settled, but adopt the best read so the main pass has a
            # reference at all (the digest flags warm=False for the LLM).
            self.reference_xyz = last_good

        self.warm = settled
        self.warmup_reads += reads
        # Stream the warm-up's HONEST final verdict to the ndjson (a control marker, not a
        # probe read — no seq increment). The per-read settle records lag the settle by one
        # (the settling read itself breaks before its post-state is recorded), so the human
        # readout — which tails the ndjson, not the events — can't otherwise tell a settled
        # run from an escalated (cap-hit) one. This is what keeps the readout's `warm` honest
        # instead of assuming warm the moment the main pass starts.
        self.ndjson.emit({"t": _now(), "phase": phase, "role": "warmup_complete",
                          "settled": settled, "reads": reads, "cold_channel": self.cold_channel})
        self._emit_event(
            "INFO" if settled else "WARN",
            "warmup_complete",
            settled=settled,
            reads=reads,
            cold_channel=self.cold_channel,
            reference_xyz=list(self.reference_xyz) if self.reference_xyz else None,
        )
        return settled, reads

    # -- thermal preheat: soak-into-calibration ----------------------------

    def _preheat_enabled(self) -> bool:
        """Whether to soak before measuring — ``auto`` ADAPTS to the measured thermal regime.

        A not-yet-stable panel (``fluctuating`` / ``warming``) always soaks. A ``convergent``
        panel is thermally stable, so it soaks ONLY when its measured cold-start warm-in
        (``warmin_magnitude``) actually moves more than the run-time drift tolerance — otherwise
        the static-grey warm-up settle gate already covers the cold-start and the soak is pure
        cost: it runs once PER measure-stage, each block reading slow dark load patches, for no
        thermal benefit on a stable panel (HW-confirmed on a convergent SDR panel, creep
        ~0.01 ΔE/min). ``always`` / ``never`` force it on/off; an UNCHARACTERIZED (no-DIP / no
        regime) or ``compromised`` panel does not soak — it falls back to the static-grey warm-up."""
        mode = self.cfg.preheat
        if mode == "never":
            return False
        if mode == "always":
            return True
        dip = self.dip
        if dip is None or dip.thermal_regime is None:
            return False
        if dip.thermal_regime in ("fluctuating", "warming"):
            return True
        if dip.thermal_regime == "convergent":
            # Stable panel: soak only if the MEASURED warm-in exceeds the drift tolerance, i.e.
            # the panel moves more warming in than the run-time drift watch will tolerate — then
            # parking at the operating load first earns its cost; otherwise warm-up settle suffices.
            warmin = dip.warmin_magnitude or 0.0
            tol = dip.recommended_drift_threshold or self.cfg.drift_threshold
            return warmin > tol
        return False

    def _run_soak(self, *, phase: str, max_blocks: Optional[int] = None):
        """Drive a closed-loop :class:`~dlc.thermal.ThermalController` over THIS run's patch set.

        Feeding the measurement set as the soak content is the whole trick: the controller glides
        to ``k=1`` = the set's own mean backlight energy, so the panel is parked at exactly the load
        the measurement will then sustain — no thermal step at the soak→measure boundary. Returns the
        ``ThermalResult`` (or ``None`` when there's no content to soak with). Per-block records are
        kept off ``measurements.ndjson`` (the readout tails it and expects measurement-shaped rows);
        the caller emits a single summary marker instead."""
        from .thermal import ThermalController, ThermalConfig  # lazy: keep the module import light

        self._live_phase = phase
        content = [p.rgb for p in self.patches]
        if not content:
            return None
        max_cv = self.transfer.max_cv
        ref_nits = self.transfer.cv_to_nits(round(self.cfg.warmup_signal * max_cv))
        # Prefer the DIP's measured channel-balance noise (≈ drift_threshold / 3) so the soak's
        # convergence gate is keyed to this panel+meter; else let the controller self-calibrate.
        balance_noise: Optional[float] = None
        if self.dip is not None and self.dip.recommended_drift_threshold:
            balance_noise = self.dip.recommended_drift_threshold / 3.0
        tcfg_kw: dict[str, Any] = {"k_start": self.cfg.preheat_k_start}
        if max_blocks is not None:
            tcfg_kw["max_blocks"] = max_blocks
        ctrl = ThermalController(
            measure=self.measure, transfer=self.transfer, content=content,
            ref_nits=ref_nits, balance_noise=balance_noise, config=ThermalConfig(**tcfg_kw),
            emit=None, event=self._emit_event,
        )
        res = ctrl.run()
        if res.active_channel and self.cold_channel is None:
            self.cold_channel = res.active_channel
        return res

    def preheat(self) -> Optional[dict[str, Any]]:
        """Soak the panel to its operating equilibrium BEFORE the main pass (regime-gated; see
        :meth:`_preheat_enabled`). Returns a digest, or ``None`` when skipped/empty."""
        if not self._preheat_enabled():
            return None
        res = self._run_soak(phase="preheat")
        if res is None:
            return None
        digest = {"regime": res.regime, "converged": res.converged, "blocks": res.blocks,
                  "content_reads": res.content_reads, "final_k": res.final_k,
                  "compromised": res.compromised, "active_channel": res.active_channel}
        self.ndjson.emit({"t": _now(), "phase": "preheat", "role": "preheat_complete", **digest})
        self._emit_event("INFO" if (res.converged and not res.compromised) else "WARN",
                         "preheat_complete", **digest)
        return digest

    def _rewarm(self, *, phase: str) -> None:
        """Re-establish the warm reference after a drift episode. On a content-driven panel a
        static-grey re-settle can't actually re-warm it, so inject a BOUNDED thermal soak (the rare
        reactive fallback — fired only here, when the interleaved drift checkpoint trips) before
        re-settling. On a convergent/unknown panel this is exactly the prior static-grey re-settle."""
        if self._preheat_enabled():
            res = self._run_soak(phase="rewarm", max_blocks=self.cfg.rewarm_max_blocks)
            if res is not None:
                self.ndjson.emit({"t": _now(), "phase": phase, "role": "rewarm",
                                  "blocks": res.blocks, "content_reads": res.content_reads,
                                  "regime": res.regime})
        self.warm_up(phase=phase, existing_reference=True)

    # -- one measurement patch (DIP-driven read policy) --------------------

    def _abnormal_reads(self, target_n: Optional[int]) -> int:
        """The read count past which a patch is *abnormal* and must be FLAGGED (not
        silently capped). Scaled off the DIP's per-luminance target so a legitimately
        noisy dark patch (large target) isn't flagged at its own target, while a bright
        patch that keeps disagreeing is flagged early. Always ≥ ``cfg.abnormal_reads``."""
        base = self.cfg.abnormal_reads
        return max(base, 2 * (target_n or 1))

    def measure_patch(self, patch: MeasurePatch, *, phase: str, disposition: Optional[str] = None) -> AcceptedRead:
        """Measure one patch with the single-read-default policy: take one
        adaptive-integration read, take *more averaged* reads only where the DIP's
        measured noise says SNR needs it (``target_n``), and keep reading (then FLAG,
        never silently cap) when the sample standard error won't fall within
        ``read_tolerance_de`` — an abnormal patch the LLM must adjudicate."""
        cfg = self.cfg
        reads: list[tuple[float, float, float]] = []
        yxys: list[tuple[float, float, float]] = []
        target_n: Optional[int] = None          # DIP-predicted reads for SNR (set on first valid read)
        sigma: Optional[float] = None           # DIP per-read σ at this luminance (drives outlier rejection)
        read_index = 0
        unstable = False
        usable = True
        note: Optional[str] = None

        while True:
            st = self._robust_stats(reads, sigma=sigma)
            running_se = st[1] if st else None
            r = self._read(
                patch, phase=phase, read_index=read_index, accepted=True,
                agreement_de=(round(running_se, 4) if running_se is not None else None),
                disposition=("immediate" if read_index else disposition),
            )
            read_index += 1
            if r.xyz is not None:
                reads.append(r.xyz)
                if r.yxy is not None:
                    yxys.append(r.yxy)
                self._update_white(r.xyz)
                if target_n is None:
                    # First valid read fixes the SNR target + σ from the DIP at this
                    # patch's luminance; no DIP ⇒ min_reads (the single-read default).
                    nits = r.xyz[1]
                    sigma = self.dip.expected_sigma_de(nits) if self.dip else None
                    dip_n = self.dip.reads_for_tolerance(nits, cfg.read_tolerance_de) if self.dip else None
                    target_n = max(cfg.min_reads, dip_n or cfg.min_reads)

            st = self._robust_stats(reads, sigma=sigma)
            n_inliers = st[2] if st else 0
            se = st[1] if st else None
            outliers = st[3] if st else 0

            # Converge only on a CLEAN cluster: enough inlier reads at the SNR target,
            # agreeing within tolerance, AND not too many reads rejected as glitches. A
            # one-off transient is tolerated (resolved by the surviving inliers), but a
            # patch that is *mostly* outliers — bimodal / ping-ponging / genuinely
            # unstable — must FLAG, not be silently resolved by majority vote.
            if (target_n is not None and n_inliers >= target_n
                    and (se is None or se <= cfg.read_tolerance_de)
                    and outliers <= max(1, read_index // 4)):
                break

            # Abnormal: too many reads for this luminance band ⇒ FLAG, do not cap silently.
            if read_index >= self._abnormal_reads(target_n):
                unstable = True
                note = (f"abnormal: {read_index} reads ({st[3] if st else 0} outlier(s)), SE "
                        + (f"{se:.3f} dE > {cfg.read_tolerance_de:.3f}" if se is not None else "n/a")
                        + " — flagged for adjudication") if reads else "no usable read"
                self._emit_event("WARN", "patch_unstable", label=patch.label,
                                 reads=read_index, se=(round(se, 4) if se is not None else None),
                                 outliers=(st[3] if st else 0))
                break

        # Accept the outlier-rejected MEAN (averaging the inliers IS the SNR win; a gross
        # glitch is dropped, not diluted in); a sentinel hole if nothing usable came back.
        st = self._robust_stats(reads, sigma=sigma)
        if st:
            accepted_xyz = st[0]
            accepted_yxy = _mean_xyz(yxys) if yxys else None
        else:
            accepted_xyz, accepted_yxy, unstable, usable = (0.0, 0.0, 0.0), None, True, False
            note = note or "no usable read"

        self._update_white(accepted_xyz)
        immediate = max(0, read_index - 1)
        record = self.accepted.get(patch.label)
        if record is None:
            record = AcceptedRead(
                patch=patch, xyz=accepted_xyz, yxy=accepted_yxy,
                reads_taken=read_index, immediate_remeasures=immediate,
                unstable=unstable, usable=usable, note=note,
            )
            self.accepted[patch.label] = record
        else:
            # Overwrite in place (re-measure): keep only the final accepted read.
            record.xyz = accepted_xyz
            record.yxy = accepted_yxy
            record.reads_taken += read_index
            record.immediate_remeasures += immediate
            record.unstable = unstable
            record.usable = usable
            record.note = note
        return record

    # -- main pass ---------------------------------------------------------

    def main_pass(self) -> None:
        self._live_phase = "measure"
        cfg = self.cfg
        warmup_patch = self._warmup_patch()
        pending: list[str] = []  # measured since the last clean neutral checkpoint

        for index, patch in enumerate(self.patches, start=1):
            self.measure_patch(patch, phase="main")
            pending.append(patch.label)

            if cfg.neutral_interval > 0 and index % cfg.neutral_interval == 0:
                self._neutral_checkpoint(warmup_patch, pending)

        # Final checkpoint for the tail of the pass.
        if pending:
            self._neutral_checkpoint(warmup_patch, pending, final=True)

    def _neutral_checkpoint(self, warmup_patch: MeasurePatch, pending: list[str], *, final: bool = False) -> None:
        if self.reference_xyz is None:
            pending.clear()
            return
        # One physical read → one enriched ndjson line carrying the drift verdict
        # (we need the XYZ before we can compute the verdict, so emit inline
        # rather than via _read, which emits atomically on measure).
        reading = self.measure(warmup_patch)
        seq = self.seq_counter
        self.seq_counter += 1
        if reading.xyz is None:
            self.ndjson.emit(
                {
                    "t": _now(), "seq": seq, "phase": "main", "role": "neutral_ref",
                    "label": warmup_patch.label, "rgb": list(warmup_patch.rgb),
                    "signal": [round(s, 6) for s in warmup_patch.signal], "read_index": 0,
                    "xyz": None, "yxy": None, "nits": None, "ok": False, "accepted": False,
                    "agreement_de": None, "drift": None, "settle": None,
                    "disposition": None, "note": "drift_checkpoint_failed",
                }
            )
            return
        self._update_white(reading.xyz)
        ev = evaluate_drift(
            stabilized_xyz=self.reference_xyz,
            current_xyz=reading.xyz,
            delta_threshold=self.cfg.drift_threshold,
        )
        self.ndjson.emit(
            {
                "t": _now(),
                "seq": seq,
                "phase": "main",
                "role": "neutral_ref",
                "label": warmup_patch.label,
                "rgb": list(warmup_patch.rgb),
                "signal": [round(s, 6) for s in warmup_patch.signal],
                "read_index": 0,
                "xyz": list(reading.xyz),
                "yxy": list(reading.yxy) if reading.yxy is not None else None,
                "nits": reading.nits,
                "ok": True,
                "accepted": False,
                "agreement_de": None,
                "drift": {
                    "max_delta": round(ev.max_channel_delta, 6),
                    "repeat": ev.repeat,
                    "coldest": ev.coldest_channel,
                },
                "settle": None,
                "disposition": None,
                "note": "drift_checkpoint",
            }
        )

        if ev.repeat:
            # Panel temperature moved: every patch since the last clean checkpoint
            # may have been taken cold. Queue them for an appended re-measure and
            # re-establish the warm reference.
            self.drift_episodes += 1
            cold = [lbl for lbl in pending]
            for lbl in cold:
                rec = self.accepted.get(lbl)
                if rec is not None and not rec.taken_cold:
                    rec.taken_cold = True
                    self.appended_queue.append(rec.patch)
            self._emit_event(
                "WARN",
                "drift_episode",
                max_delta=ev.max_channel_delta,
                coldest=ev.coldest_channel,
                flagged=len(cold),
            )
            pending.clear()
            if not final:
                # Re-warm so the rest of the pass measures warm (soak-then-resettle on a
                # content-driven panel; plain static-grey re-settle on a convergent one).
                self._rewarm(phase="main")
        else:
            pending.clear()

    # -- selective re-measure (appended queue) -----------------------------

    def drain_appended(self) -> list[str]:
        cfg = self.cfg
        unresolved: list[str] = []
        if not self.appended_queue:
            return unresolved

        # Re-warm once before redoing warm-up casualties.
        self._rewarm(phase="warmup")

        # De-dup while preserving order (a patch can be flagged by >1 episode).
        seen: set[str] = set()
        queue = [p for p in self.appended_queue if not (p.label in seen or seen.add(p.label))]
        self.appended_queue = []

        for patch in queue:
            if self.remeasure_budget <= 0:
                unresolved.append(patch.label)
                continue
            self.remeasure_budget -= 1
            rec = self.measure_patch(patch, phase="remeasure", disposition="appended")
            rec.appended_remeasures += 1
            rec.taken_cold = False  # redone while warm
            if rec.unstable:
                unresolved.append(patch.label)
        return unresolved

    # -- assembly ----------------------------------------------------------

    def ordered_accepted(self) -> list[AcceptedRead]:
        return sorted(self.accepted.values(), key=lambda r: r.patch.seq)


def run_measure_loop(
    *,
    patches: Sequence[Patch],
    transfer: Transfer,
    measure: MeasureFn,
    config: Optional[MeasureLoopConfig] = None,
    ti3_path: Optional[Path] = None,
    ndjson_path: Optional[Path] = None,
    events: Optional[EventWriter] = None,
    runlog: Optional[RunLog] = None,
    liveness: Optional[Liveness] = None,
    dip: Optional[DisplayInstrumentProfile] = None,
) -> MeasureLoopResult:
    """Run the adaptive measurement loop over ``patches`` (code-value triples,
    already thermally ordered by the caller via :mod:`dlc.engine.patches`).

    ``measure`` presents *and* reads one patch (the only display/meter seam):
    use :func:`make_spotread_meter` live, or a :class:`SyntheticPanel` in tests.
    Writes a clean ``.ti3`` (accepted reads) and ``measurements.ndjson`` (every
    read) when the paths are given. Returns a :class:`MeasureLoopResult` whose
    ``digest`` is the LLM-facing boundary object.

    ``runlog`` (the shared run spine) makes the loop's progress LIVE: every read
    mirrors a compact ``patch_read`` onto ``events.jsonl`` and ``progress`` ticks
    advance the dashboard's counters/ETA. It's stamped with the orchestrator's
    current phase, so the firehose is dashboard-only (stream tier) while the LLM
    keeps reading just the digest. ``events`` stays as the legacy event-only seam
    (characterize, tests); ``runlog`` supersedes it when both are present.
    """

    cfg = config or MeasureLoopConfig()
    ndjson = _NdjsonWriter(ndjson_path)
    loop = _Loop(
        patches=patches,
        transfer=transfer,
        measure=measure,
        config=cfg,
        ndjson=ndjson,
        events=events,
        runlog=runlog,
        liveness=liveness,
        dip=dip,
    )

    preheat_digest = loop.preheat()
    loop.warm_up()
    loop.main_pass()
    unresolved = loop.drain_appended()

    accepted = loop.ordered_accepted()
    written_ti3: Optional[str] = None
    if ti3_path is not None and accepted:
        write_ti3(ti3_path, accepted)
        written_ti3 = str(ti3_path)

    immediate = sum(r.immediate_remeasures for r in accepted)
    appended = sum(r.appended_remeasures for r in accepted)
    unstable_labels = [r.patch.label for r in accepted if r.unstable]
    # "Unresolved" = a patch the loop could not stabilise (over budget or still
    # unstable after the immediate gate) — these are what the LLM must adjudicate.
    unresolved_all = sorted(set(unresolved) | set(unstable_labels))

    needs_adjudication = (not loop.warm) or bool(unresolved_all)
    question = None
    if needs_adjudication:
        bits = []
        if not loop.warm:
            bits.append(
                f"panel did not settle within {cfg.max_warmup_reads} warm-up reads "
                f"(cold channel {loop.cold_channel})"
            )
        if unresolved_all:
            bits.append(
                f"{len(unresolved_all)} patch(es) would not stabilise: "
                + ", ".join(unresolved_all[:8])
                + ("…" if len(unresolved_all) > 8 else "")
            )
        question = (
            "; ".join(bits)
            + " — accept these as the panel's physical floor/limit, or keep warming / "
            "loosen the read tolerance and retry?"
        )

    digest = {
        "warm": loop.warm,
        "warmup_reads": loop.warmup_reads,
        "cold_channel": loop.cold_channel,
        "reference_xyz": [round(c, 4) for c in loop.reference_xyz] if loop.reference_xyz else None,
        "patch_count": len(accepted),
        "total_reads": loop.seq_counter,
        "immediate_remeasures": immediate,
        "appended_remeasures": appended,
        "drift_episodes": loop.drift_episodes,
        "unresolved": unresolved_all,
        "white_xyz": [round(c, 4) for c in loop.white_xyz] if loop.white_xyz else None,
        "white_nits": round(loop.white_xyz[1], 3) if loop.white_xyz else None,
        "preheat": preheat_digest,
        "needs_adjudication": needs_adjudication,
    }
    if events is not None:
        events.write(
            "INFO" if not needs_adjudication else "WARN",
            "measure_loop",
            "completed",
            **{k: v for k, v in digest.items() if k != "reference_xyz"},
        )

    return MeasureLoopResult(
        warm=loop.warm,
        warmup_reads=loop.warmup_reads,
        reference_xyz=loop.reference_xyz,
        patch_count=len(accepted),
        total_reads=loop.seq_counter,
        immediate_remeasures=immediate,
        appended_remeasures=appended,
        drift_episodes=loop.drift_episodes,
        unresolved=unresolved_all,
        white_xyz=loop.white_xyz,
        ti3_path=written_ti3,
        ndjson_path=str(ndjson_path) if ndjson_path else None,
        needs_adjudication=needs_adjudication,
        question=question,
        digest=digest,
    )


# ---------------------------------------------------------------------------
# Presenter protocol + live spotread meter (the swappable display/meter seam)
# ---------------------------------------------------------------------------

class Presenter(Protocol):
    """Shows a full-screen patch and tears down cleanly. dogegen is the trusted
    primary; a mock/scripted presenter drives tests with no display. Per
    v2-design-notes §6 the live presenter must paint **composited (not
    independent-flip)** so the 3D LUT applies during verify, at exact code
    values, on monitor 0, 10-bit — trust-validate once."""

    def show(self, patch: MeasurePatch) -> None: ...

    def close(self) -> None: ...


class DogegenPresenter:
    """:class:`Presenter` backed by :class:`dlc.dogegen.DogegenPatchDisplay`.

    When ``place_rect`` is given, the spawned window is placed onto that monitor automatically
    (closing dogegen's wrong-panel hazard — it always opens on the Windows primary, which may not
    be the calibration target). ``fullscreen=False`` (the default) *moves* the window but keeps it
    composited so a DWM-hook 3D LUT still applies — correct for the 8-bit SDR / verify path;
    ``fullscreen=True`` borderless-fullscreens it (bypasses the compositor) for corrections-OFF
    bit-accurate 10-bit/HDR measurement. Placement is best-effort and never blocks a spawn."""

    def __init__(self, display: Any, *, patch_size: int = 100, settle_seconds: float = 0.5,
                 place_rect: Any = None, fullscreen: bool = False) -> None:
        self.display = display
        self.patch_size = patch_size
        self.settle_seconds = settle_seconds
        self.place_rect = place_rect
        self.fullscreen = fullscreen
        self.placement: Any = None
        self._proc = None

    def _ensure(self) -> Any:
        if self._proc is None:
            self._proc = self.display.start()
            if self.place_rect is not None:
                from .dogegen_window import place_dogegen
                self.placement = place_dogegen(self._proc.pid, rect=self.place_rect,
                                               fullscreen=self.fullscreen)
        return self._proc

    def show(self, patch: MeasurePatch) -> None:
        proc = self._ensure()
        r, g, b = patch.rgb
        self.display.send(proc, f"window {self.patch_size} {r} {g} {b}", settle_seconds=self.settle_seconds)

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self.display.send(self._proc, "quit", settle_seconds=0.1)
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.terminate()
            except Exception:
                pass
        finally:
            self._proc = None


class SocketPresenter:
    """:class:`Presenter` that drives a PERSISTENT dogegen via :mod:`dlc.dogegen_server`
    over a local socket. The window is started + Alt+Enter-fullscreened **once** and reused
    across every CLI invocation — no respawn, no flash, fullscreen preserved (the enabler
    for accurate 10-bit, which needs a fullscreen window). Patch code values are sent in the
    server's dogegen bit depth, so the run's ``--bit-depth`` must match the server's."""

    def __init__(self, host: str, port: int, *, settle_seconds: float = 0.5,
                 timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.settle_seconds = settle_seconds
        self.timeout = timeout
        self._sock = None

    def _ensure(self):
        if self._sock is None:
            import socket
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            s.settimeout(self.timeout)
            self._sock = s
        return self._sock

    def _recv_line(self, s) -> str:
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(256)
            if not chunk:
                break
            buf += chunk
        return buf.decode("ascii", "ignore").strip()

    def show(self, patch: MeasurePatch) -> None:
        import time
        s = self._ensure()
        r, g, b = patch.rgb
        s.sendall(f"{r} {g} {b}\n".encode("ascii"))
        ack = self._recv_line(s)
        if not ack.startswith("ok"):
            raise RuntimeError(f"dogegen-server did not ack patch ({r},{g},{b}): {ack!r}")
        if self.settle_seconds:
            time.sleep(self.settle_seconds)

    def close(self) -> None:
        # Drop our connection ONLY — the daemon (and its fullscreen window) persists across
        # invocations on purpose (so a pause/resume keeps one fullscreen window). The run's
        # terminal step calls :meth:`shutdown_daemon` to actually stop it.
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def shutdown_daemon(self) -> None:
        """Tell the persistent daemon to quit (closing its dogegen window), then drop the
        socket. Call this once the run reaches a TERMINAL state — never on a pause, where the
        daemon must survive for the resuming invocation. Best-effort and idempotent."""
        try:
            s = self._ensure()
            s.sendall(b"quit\n")
            try:
                self._recv_line(s)  # drain the 'bye' ack; ignore content
            except Exception:
                pass
        except Exception:
            pass  # daemon already gone / unreachable — nothing to stop
        finally:
            self.close()


def make_spotread_meter(
    *,
    presenter: Presenter,
    spotread: Any,
    port: int,
    output_dir: Path,
    spectral: bool = False,
    high_res: bool = False,
    display_type: Optional[str] = None,
    ccmx_or_ccss: Optional[Path] = None,
    read_timeout: float = 60.0,
) -> MeasureFn:
    """Compose a :class:`Presenter` + Argyll ``spotread`` into a :data:`MeasureFn`.

    Presents the patch, runs one ``spotread``, parses ``XYZ``/``Yxy``. Mirrors
    :func:`dlc.measure_rgbw._measure_patch` but generalized to any patch label and
    decoupled from ``RgbwPatch``. The instrument ``port`` should be resolved by
    :func:`dlc.measure_rgbw.resolve_spotread_instrument_port` before the meter
    phase (ports are not stable across probe swaps).

    ``read_timeout`` is the hard per-read ceiling. The whole liveness/stall design
    assumes a read ALWAYS returns bounded — but ``run_spotread_once`` uses
    ``subprocess.run(timeout=…)`` which **raises** :class:`subprocess.TimeoutExpired`
    on a hung meter. Left uncaught (as it was) that crashes the run mid-stage, and the
    checkpoint guard never gets to run. So we catch it here and return ``ok=False``
    (subprocess.run has already killed the hung process), honouring the contract. The
    ceiling is generous vs the ~2 s fast floor / slow dark-patch adaptive integration,
    but far below the 53-min wedge — the real (DIP-derived) stall threshold is the
    guard's job, this is just the per-read backstop."""

    import subprocess  # local: only this factory talks to a one-shot subprocess

    from .argyll import SpotreadRequest, parse_xyz, parse_yxy

    output_dir.mkdir(parents=True, exist_ok=True)

    def measure(patch: MeasurePatch) -> Reading:
        presenter.show(patch)
        request = SpotreadRequest(
            port=port,
            output_sp=(output_dir / f"{patch.label}.sp") if spectral else None,
            logfile=(output_dir / f"{patch.label}_spotread_log.txt") if spectral else None,
            high_res=high_res,
            display_type=display_type,
            ccmx_or_ccss=ccmx_or_ccss,
            # Do NOT pass -N: the i1 DisplayPro reports "Disable initial-calibrate not
            # supported", and that failed -N leaves spotread not taking a reading in a
            # background (console-less) run → callers see 0.0. Letting it auto-calibrate
            # (fast for an emissive colorimeter) reads reliably in foreground AND background.
            skip_calibration=False,
        )
        try:
            completed = spotread.run_spotread_once(request, timeout_seconds=int(read_timeout))
        except subprocess.TimeoutExpired:
            # The hung spotread was killed by subprocess.run's own timeout. Return a
            # bounded failure so the loop reaches its next checkpoint instead of crashing.
            return Reading(xyz=None, yxy=None, ok=False,
                           error=f"spotread one-shot timed out after {read_timeout:.0f}s",
                           raw={"timed_out": True})
        except OSError as exc:
            return Reading(xyz=None, yxy=None, ok=False,
                           error=f"spotread spawn failed: {type(exc).__name__}: {exc}",
                           raw={"spawn_error": True})
        combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
        xyz = parse_xyz(combined)
        yxy = parse_yxy(combined)
        error: Optional[str] = None
        if completed.returncode != 0:
            error = f"spotread exited with {completed.returncode}"
        elif xyz is None and yxy is None:
            error = "spotread output did not contain XYZ/Yxy"
        return Reading(
            xyz=xyz,
            yxy=yxy,
            ok=error is None,
            error=error,
            raw={
                "returncode": completed.returncode,
                "spectral_file": str(request.output_sp) if request.output_sp else None,
            },
        )

    return measure


def make_persistent_spotread_meter(
    *,
    presenter: Presenter,
    persistent: Any,
    settle_seconds: float = 0.0,
) -> MeasureFn:
    """Compose a :class:`Presenter` + a live :class:`dlc.argyll.PersistentSpotread`
    into a :data:`MeasureFn` — the fast path that reuses ONE interactive spotread
    process across the whole pass (calibrate once, one reading per trigger) instead
    of spawning a fresh, self-calibrating process per read like
    :func:`make_spotread_meter`.

    ``persistent`` is a started-or-startable ``PersistentSpotread`` (its
    :meth:`measure` returns a ``SpotreadResult``); the caller owns its lifecycle
    and must ``close()`` it when the pass ends. ``settle_seconds`` is an OPTIONAL
    extra dwell *after* the presenter's own settle and *before* the read — leave it
    at 0 here and let the presenter / measure-loop own settle, so a confirm/repeat
    read of an unchanged patch never re-pays a panel-settle it doesn't need."""

    import time as _time

    def measure(patch: MeasurePatch) -> Reading:
        presenter.show(patch)
        if settle_seconds:
            _time.sleep(settle_seconds)
        res = persistent.measure()
        return Reading(
            xyz=res.xyz,
            yxy=res.yxy,
            ok=res.ok,
            error=res.error,
            raw={"persistent": True, "result": res.raw},
        )

    return measure


# ---------------------------------------------------------------------------
# Deterministic synthetic panel (no hardware) — mirrors the engine's synthetic
# panel tests; exercises warm-up creep, mid-run drift, and a flaky patch.
# ---------------------------------------------------------------------------

# sRGB (Rec.709) primaries → XYZ at D65, white Y normalized to 1.0. Multiplying
# by white_nits gives an absolute white at signal (1,1,1).
_SRGB_TO_XYZ_D65 = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)


class SyntheticPanel:
    """A deterministic, stateful synthetic meter (``MeasureFn``) modelling a QD
    mini-LED with a temperamental blue channel.

    * **Warm-up creep:** blue gain rises with an internal ``temp`` that warms a
      little on every read (time constant ``warm_tau``), so the *consecutive*-read
      delta shrinks (the warm-up loop settles) while the *absolute* blue level is
      still creeping — which the interleaved drift reference then catches.
    * **Flaky patch:** ``flaky_label``'s first read carries a chroma glitch
      (``flaky_chroma``), good on every subsequent read → exercises the immediate
      repeatability gate.

    Pure stdlib + deterministic (optional seeded gaussian noise), so tests run
    without numpy and never flake.
    """

    def __init__(
        self,
        *,
        transfer: Transfer,
        white_nits: float = 120.0,
        gamma: float = 2.2,
        cold_blue_gain: float = 0.90,
        warm_tau: float = 0.06,
        flaky_label: Optional[str] = None,
        flaky_chroma: float = 0.05,
        flaky_persistent: bool = False,
        noise: float = 0.0,
        seed: int = 7,
        start_temp: float = 0.0,
        load_thermal: bool = False,
        thermal_rate: float = 0.05,
    ) -> None:
        self.transfer = transfer
        self.white_nits = white_nits
        self.gamma = gamma
        self.cold_blue_gain = cold_blue_gain
        self.warm_tau = warm_tau
        self.flaky_label = flaky_label
        self.flaky_chroma = flaky_chroma
        self.flaky_persistent = flaky_persistent
        self.noise = noise
        self._rng_state = seed & 0x7FFFFFFF
        self.temp = max(0.0, min(1.0, start_temp))
        self.load_thermal = load_thermal
        self.thermal_rate = thermal_rate
        self.reads = 0
        self._flaky_seen: dict[str, int] = {}

    def _rand(self) -> float:
        # Tiny LCG → uniform [0,1); only used when noise>0 (kept deterministic).
        self._rng_state = (1103515245 * self._rng_state + 12345) & 0x7FFFFFFF
        return self._rng_state / 0x7FFFFFFF

    def __call__(self, patch: MeasurePatch) -> Reading:
        self.reads += 1
        r, g, b = patch.signal
        if self.load_thermal:
            # Load-dependent thermal model (opt-in): the panel relaxes toward an
            # equilibrium temperature set by the CURRENT patch's drive load (max
            # channel), so higher-luminance content heats faster AND dropping the
            # load lets it COOL (overshoot is real). Time constant = thermal_rate.
            # This is what exercises the closed-loop ThermalController in tests.
            load = max(max(0.0, r), max(0.0, g), max(0.0, b)) ** self.gamma
            self.temp += (load - self.temp) * self.thermal_rate
        else:
            # Legacy: warm a little on every read toward fully warm (1.0), load-
            # independent — the "taken cold" warm-up creep model.
            self.temp += (1.0 - self.temp) * self.warm_tau
        self.temp = max(0.0, min(1.0, self.temp))
        blue_gain = self.cold_blue_gain + (1.0 - self.cold_blue_gain) * self.temp

        lr = max(0.0, r) ** self.gamma
        lg = max(0.0, g) ** self.gamma
        lb = (max(0.0, b) ** self.gamma) * blue_gain

        x = self.white_nits * (_SRGB_TO_XYZ_D65[0][0] * lr + _SRGB_TO_XYZ_D65[0][1] * lg + _SRGB_TO_XYZ_D65[0][2] * lb)
        y = self.white_nits * (_SRGB_TO_XYZ_D65[1][0] * lr + _SRGB_TO_XYZ_D65[1][1] * lg + _SRGB_TO_XYZ_D65[1][2] * lb)
        z = self.white_nits * (_SRGB_TO_XYZ_D65[2][0] * lr + _SRGB_TO_XYZ_D65[2][1] * lg + _SRGB_TO_XYZ_D65[2][2] * lb)

        if self.flaky_label is not None and patch.label == self.flaky_label:
            seen = self._flaky_seen.get(patch.label, 0)
            self._flaky_seen[patch.label] = seen + 1
            if self.flaky_persistent:
                # A patch that never repeats: ping-pong the glitch so consecutive
                # reads always disagree → the immediate gate can't converge it.
                sign = 1.0 if seen % 2 == 0 else -1.0
                y *= (1.0 + self.flaky_chroma * sign)
                x *= (1.0 - self.flaky_chroma * 0.5 * sign)
            elif seen == 0:
                # A transient glitch on the first read only; good thereafter.
                y *= (1.0 + self.flaky_chroma)
                x *= (1.0 - self.flaky_chroma * 0.5)

        if self.noise:
            x *= 1.0 + self.noise * (self._rand() - 0.5)
            y *= 1.0 + self.noise * (self._rand() - 0.5)
            z *= 1.0 + self.noise * (self._rand() - 0.5)

        x = max(0.0, x)
        y = max(0.0, y)
        z = max(0.0, z)
        total = x + y + z
        yxy = (y, x / total, y / total) if total > 0 else (0.0, 0.0, 0.0)
        return Reading(xyz=(x, y, z), yxy=yxy, ok=True)
