"""Adaptive, self-healing measurement loop (v2-design-notes §6, HANDOFF §0.5).

Turns a thermally-ordered patch set (:mod:`dlc.engine.patches`) into a clean
``.ti3`` (final *accepted* reads, for the LUT engines) **plus** a streaming
``measurements.ndjson`` (every probe read, append-only) — surviving panel drift
via three mechanisms the design calls for:

1. **Warm-up-settle** — hold a neutral stimulus biased toward the cold channel
   (blue, from the profile ``quirks``) and re-read until consecutive reads agree
   within ``settle_threshold`` for ``settle_required`` reads → "panel warm". The
   last settled read becomes the live *drift reference*.
2. **Per-patch repeatability gate (immediate re-measure)** — a patch whose
   confirm read disagrees with its first read beyond ``repeat_threshold`` (a
   transient glitch) is re-read on the spot up to ``max_repeats`` times; only the
   converged read is accepted.
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

from .drift import Channel, coldest_channel_from_xyz, evaluate_drift
from .engine.patches import Patch, Transfer, to_signal
from .events import EventWriter
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
    """Loop tunables. The defaults are sane *core* values; the genuinely
    judgment-bearing ones (``settle_threshold``, ``settle_required``,
    ``repeat_threshold``, ``max_repeats``, ``remeasure_cap``) are **LLM-deferred**
    — the orchestrator may override per run. *Physical facts* (``cold_channel``,
    the settle tolerance) come from the profile, never hardwired here.
    """

    # Warm-up-settle ---------------------------------------------------------
    warmup_signal: float = 0.5          # neutral grey level for warm-up / reference
    warmup_bias_signal: float = 0.02    # extra signal on the cold channel
    cold_channel: Optional[Channel] = None  # from profile quirks; None → auto-detect
    settle_threshold: float = 0.003     # channel-balance Δ between consecutive reads
    settle_required: int = 3            # consecutive in-tolerance reads ⇒ "warm"
    max_warmup_reads: int = 24          # cap; not settled ⇒ escalate to the LLM

    # Interleaved drift reference (appended re-measure) ----------------------
    neutral_interval: int = 8           # measurement patches between neutral re-reads
    drift_threshold: float = 0.004      # channel-balance Δ vs the warm reference

    # Per-patch repeatability (immediate re-measure) -------------------------
    confirm_reads: int = 2              # reads/patch; ≥2 enables the repeatability gate
    repeat_threshold: float = 0.5       # dE2000 agreement tolerance between reads
    max_repeats: int = 3               # extra immediate re-reads on disagreement

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
    ) -> None:
        self.transfer = transfer
        self.measure = measure
        self.cfg = config
        self.ndjson = ndjson
        self.events = events

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

    def _emit_event(self, level: str, event: str, **data: Any) -> None:
        if self.events is not None:
            self.events.write(level, "measure_loop", event, **data)

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
        return reading

    def _update_white(self, xyz: tuple[float, float, float]) -> None:
        if self.white_xyz is None or xyz[1] > self.white_xyz[1]:
            self.white_xyz = xyz

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

    # -- one measurement patch (with the immediate repeatability gate) -----

    def measure_patch(self, patch: MeasurePatch, *, phase: str, disposition: Optional[str] = None) -> AcceptedRead:
        cfg = self.cfg
        first = self._read(patch, phase=phase, read_index=0, accepted=True, disposition=disposition)
        reads = 1
        immediate = 0
        unstable = False
        note = None

        accepted_xyz = first.xyz
        accepted_yxy = first.yxy

        if accepted_xyz is None:
            # A failed read is itself a transient — retry within the gate.
            unstable = True

        if cfg.confirm_reads >= 2 or accepted_xyz is None:
            prev_xyz = accepted_xyz
            # Confirm reads until two consecutive agree, or max_repeats hit.
            for attempt in range(1, cfg.max_repeats + 1):
                confirm = self._read(
                    patch,
                    phase=phase,
                    read_index=attempt,
                    accepted=True,
                    disposition="immediate",
                )
                reads += 1
                immediate += 1
                if confirm.xyz is None:
                    continue
                de = None
                if prev_xyz is not None:
                    de = _agreement_de(prev_xyz, confirm.xyz, self.white_xyz or confirm.xyz)
                accepted_xyz = confirm.xyz
                accepted_yxy = confirm.yxy
                if de is not None and de <= cfg.repeat_threshold:
                    unstable = False
                    note = None
                    break
                prev_xyz = confirm.xyz
                unstable = True
                note = f"repeatability {de:.3f} dE > {cfg.repeat_threshold:.3f}" if de is not None else "read failed"
            else:
                if accepted_xyz is not None and immediate:
                    self._emit_event("WARN", "patch_unstable", label=patch.label, reads=reads)

        if accepted_xyz is None:
            # Never got a usable read — record a sentinel so the patch is visible
            # downstream as a hole rather than silently missing.
            accepted_xyz = (0.0, 0.0, 0.0)
            unstable = True
            note = "no usable read"

        self._update_white(accepted_xyz)
        record = self.accepted.get(patch.label)
        if record is None:
            record = AcceptedRead(
                patch=patch,
                xyz=accepted_xyz,
                yxy=accepted_yxy,
                reads_taken=reads,
                immediate_remeasures=immediate,
                unstable=unstable,
                note=note,
            )
            self.accepted[patch.label] = record
        else:
            # Overwrite in place (re-measure): keep only the final accepted read.
            record.xyz = accepted_xyz
            record.yxy = accepted_yxy
            record.reads_taken += reads
            record.immediate_remeasures += immediate
            record.unstable = unstable
            record.note = note
        return record

    # -- main pass ---------------------------------------------------------

    def main_pass(self) -> None:
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
                # Re-settle so the rest of the pass measures warm.
                self.warm_up(phase="main", existing_reference=True)
        else:
            pending.clear()

    # -- selective re-measure (appended queue) -----------------------------

    def drain_appended(self) -> list[str]:
        cfg = self.cfg
        unresolved: list[str] = []
        if not self.appended_queue:
            return unresolved

        # Re-settle once before redoing warm-up casualties.
        self.warm_up(phase="warmup", existing_reference=True)

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
) -> MeasureLoopResult:
    """Run the adaptive measurement loop over ``patches`` (code-value triples,
    already thermally ordered by the caller via :mod:`dlc.engine.patches`).

    ``measure`` presents *and* reads one patch (the only display/meter seam):
    use :func:`make_spotread_meter` live, or a :class:`SyntheticPanel` in tests.
    Writes a clean ``.ti3`` (accepted reads) and ``measurements.ndjson`` (every
    read) when the paths are given. Returns a :class:`MeasureLoopResult` whose
    ``digest`` is the LLM-facing boundary object.
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
    )

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
            "loosen the repeatability tolerance and retry?"
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
) -> MeasureFn:
    """Compose a :class:`Presenter` + Argyll ``spotread`` into a :data:`MeasureFn`.

    Presents the patch, runs one ``spotread``, parses ``XYZ``/``Yxy``. Mirrors
    :func:`dlc.measure_rgbw._measure_patch` but generalized to any patch label and
    decoupled from ``RgbwPatch``. The instrument ``port`` should be resolved by
    :func:`dlc.measure_rgbw.resolve_spotread_instrument_port` before the meter
    phase (ports are not stable across probe swaps)."""

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
        completed = spotread.run_spotread_once(request)
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
        self.reads = 0
        self._flaky_seen: dict[str, int] = {}

    def _rand(self) -> float:
        # Tiny LCG → uniform [0,1); only used when noise>0 (kept deterministic).
        self._rng_state = (1103515245 * self._rng_state + 12345) & 0x7FFFFFFF
        return self._rng_state / 0x7FFFFFFF

    def __call__(self, patch: MeasurePatch) -> Reading:
        self.reads += 1
        # Warm a little on every read toward fully warm (1.0). A slow tau means
        # the panel is still creeping when the per-read delta first dips under a
        # settle threshold — the exact "taken cold" failure mode.
        self.temp += (1.0 - self.temp) * self.warm_tau
        blue_gain = self.cold_blue_gain + (1.0 - self.cold_blue_gain) * self.temp

        r, g, b = patch.signal
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
