"""Display + instrument **characterization** — the run that PRODUCES the DIP.

A characterize pass *learns how this panel and this meter behave together*; it is
**not a calibration** (nothing is built, nothing is applied). Its sole product is a
:class:`~dlc.dip.DisplayInstrumentProfile` (DIP) — the measured model that lets the
adaptive measure loop (:mod:`dlc.measure_loop`) replace its ungrounded constants
(``confirm_reads`` / ``max_repeats`` / ``settle_seconds`` / ``repeat_threshold`` /
``neutral_interval`` / ``drift_threshold``) with values the hardware demonstrated, and
to drive an intelligent per-patch read policy: a single adaptive-integration read by
default (as professional tools do), escalating only where the *measured* noise model
says SNR needs it.

It measures three axes (see :class:`~dlc.dip.DisplayInstrumentProfile`):

* **instrument** — back-to-back reads of a *held* stimulus at several luminances give
  the read-to-read repeatability σ as a function of luminance (the ``noise_model``),
  the noise floor (the luminance below which a single read is noise-dominated), and the
  practical per-read wall-time floor.
* **display** — a forced colour transition sampled until stable gives the step-response
  settle time (bright + dark, which differ on a local-dimming mini-LED), plus the
  native white / black / primaries.
* **drift** — the warm-up reads-to-settle, the *discovered* (never assumed) cold
  channel, and the post-warm creep rate → recommended ``neutral_interval`` /
  ``drift_threshold`` for the measure loop's interleaved drift reference.

**Load-bearing design rule (owner directive):** there is **no fixed cap** anywhere a
panel could legitimately need more reads. A warm-up or settle that won't converge
within a generous *observation* bound is **FLAGGED for the LLM** (``needs_adjudication``
+ a note), never silently truncated — "the scripting exists to inform the reader." The
σ-estimation sample size (``noise_reads``) is the one deliberate fixed count, and it is
a statistical sample to *estimate* the noise, not a cap on a calibration read.

**Numpy-free on purpose** (reuses pure-stdlib :mod:`dlc.drift` / :mod:`dlc.metrics` /
:mod:`dlc.engine.patches` and the measure loop's own warm-up), so importing it is light
and it runs in tests against a :class:`~dlc.measure_loop.SyntheticPanel` with an
injected clock — no display, no meter. The display/meter is the single
:data:`~dlc.measure_loop.MeasureFn` seam.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .dip import DisplayInstrumentProfile, NoiseBand
from .drift import CHANNELS, Channel, normalized_channels
from .engine.patches import Transfer, to_signal
from .events import EventWriter
from .measure_loop import (
    MeasureFn,
    MeasureLoopConfig,
    MeasurePatch,
    Reading,
    _Loop,
    _NdjsonWriter,
    _now,
)
from .metrics import delta_e2000, xyz_to_lab

__all__ = ["CharacterizeConfig", "CharacterizeResult", "run_characterization"]

# A monotonic wall-clock seam (seconds). Real runs use ``time.monotonic``; tests inject
# a deterministic fake so settle / creep timing is reproducible without sleeping.
Clock = Callable[[], float]


@dataclass(frozen=True)
class CharacterizeConfig:
    """Knobs for the characterization pass. Defaults favour *correctness over speed*
    (this runs once, rarely) while staying honest about the one deliberate fixed count
    (``noise_reads`` — a σ-estimation sample, not a per-patch read cap)."""

    # -- instrument (noise vs luminance) ---------------------------------
    noise_levels: tuple[float, ...] = (1.0, 0.5, 0.18, 0.05)
    #   signal levels (code-value fractions) to estimate read-to-read σ at; descending
    #   so the brightest band lands first and anchors ΔE for the rest.
    noise_reads: int = 20               # σ-estimation sample per level (a statistical sample, NOT a cap)
    settle_discard: int = 1             # reads discarded right after a level change (one settle read)
    black_reads: int = 6                # native-black sample (low light → modest sample)
    primary_reads: int = 3              # per-primary reads (R/G/B full) for native chromaticities

    # -- display (step-response settle) ----------------------------------
    settle_levels: dict[str, float] = field(default_factory=lambda: {"bright": 1.0, "dark": 0.05})
    settle_threshold_de: float = 0.15   # consecutive-read ΔE that counts as "stable"
    settle_required: int = 3            # consecutive in-tolerance reads ⇒ settled
    settle_observe_reads: int = 40      # generous bound; exceeding it FLAGS (never a silent cap)

    # -- drift (warm-up + creep) -----------------------------------------
    warmup_observe_reads: int = 60      # warm-up bound; exceeding it FLAGS (never a silent cap)
    warmup_settle_threshold: float = 0.003   # channel-balance Δ for warm-up (normalized space)
    creep_reads: int = 12               # post-warm reference reads to estimate creep
    creep_dwell_s: float = 0.0          # optional extra dwell between creep reads (real runs: spread the window)

    # -- framing ----------------------------------------------------------
    read_tolerance_de: float = 0.2      # the calibration read tolerance the floor / interval are framed around
    abnormal_white_sigma_de: float = 0.5  # white σ above this ⇒ FLAG (meter/correction trouble)


@dataclass
class CharacterizeResult:
    """The product of a characterization pass: the measured DIP (the caller stamps
    ``display`` / ``made`` / ``instrument`` / ``correction_file``), an LLM-facing
    ``digest``, and the honest flags (``needs_adjudication`` when the panel/meter
    behaved abnormally — surfaced for judgment, never silently swallowed)."""

    dip: DisplayInstrumentProfile
    digest: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    needs_adjudication: bool = False
    question: Optional[str] = None
    ndjson_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Small stdlib stats (numpy-free)
# ---------------------------------------------------------------------------

def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _pstd(xs: Sequence[float]) -> float:
    """Population standard deviation (read-to-read spread of a held stimulus)."""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _mean_xyz(reads: Sequence[tuple[float, float, float]]) -> tuple[float, float, float]:
    n = len(reads)
    return (sum(r[0] for r in reads) / n, sum(r[1] for r in reads) / n, sum(r[2] for r in reads) / n)


def _xy(xyz: Sequence[float]) -> tuple[float, float]:
    total = xyz[0] + xyz[1] + xyz[2]
    if total <= 0:
        return (0.0, 0.0)
    return (xyz[0] / total, xyz[1] / total)


def _sigma_de(reads: Sequence[tuple[float, float, float]],
              white: tuple[float, float, float]) -> float:
    """RMS perceptual (CIEDE2000) deviation of held-stimulus reads about their mean —
    the read-to-read repeatability σ the read policy averages against."""
    if len(reads) < 2:
        return 0.0
    mean = _mean_xyz(reads)
    anchor = white if white and white[1] > 0 else (mean[0] or 1.0, max(mean[1], 1e-6), mean[2] or 1.0)
    mean_lab = xyz_to_lab(mean, anchor)
    return math.sqrt(sum(delta_e2000(xyz_to_lab(r, anchor), mean_lab) ** 2 for r in reads) / len(reads))


def _noise_floor_nits(bands: Sequence[NoiseBand], tolerance_de: float,
                      black_nits: Optional[float]) -> Optional[float]:
    """The luminance below which a single read is noise-dominated: where the measured σ
    crosses ``tolerance_de``. Interpolates between bands (ascending nits). If every band
    is already within tolerance, nothing above black is noise-dominated → the floor is
    native black. If even the brightest band exceeds tolerance, the meter is noisy
    everywhere → the floor is that brightest band (and the pass flags it)."""
    if not bands:
        return None
    asc = sorted(bands, key=lambda b: b.nits)
    # All clean → floor at black (nothing above black is noise-dominated). Clamp at the dimmest
    # tested band so a raised black floor can never report a floor *above* a band we proved clean.
    if all(b.sigma_de <= tolerance_de for b in asc):
        return min(black_nits, asc[0].nits) if black_nits is not None else asc[0].nits
    # All noisy → floor at the brightest tested point.
    if all(b.sigma_de > tolerance_de for b in asc):
        return asc[-1].nits
    # Find the crossing: the dimmest band that is clean, interpolate down to its noisy
    # neighbour at the tolerance contour.
    for lo, hi in zip(asc, asc[1:]):
        if lo.sigma_de > tolerance_de >= hi.sigma_de:
            span = hi.sigma_de - lo.sigma_de
            if span == 0:
                return hi.nits
            f = (tolerance_de - lo.sigma_de) / span
            return lo.nits + f * (hi.nits - lo.nits)
    return asc[0].nits


# ---------------------------------------------------------------------------
# The characterizer
# ---------------------------------------------------------------------------

class _Characterizer:
    def __init__(self, *, measure: MeasureFn, transfer: Transfer, config: CharacterizeConfig,
                 ndjson: _NdjsonWriter, events: Optional[EventWriter], clock: Clock,
                 injected_clock: bool, cold_channel: Optional[Channel]) -> None:
        self.measure = measure
        self.transfer = transfer
        self.cfg = config
        self.ndjson = ndjson
        self.events = events
        self.clock = clock
        self.injected_clock = injected_clock   # a test clock controls time → never real-sleep
        self.cold_channel = cold_channel
        self.white_xyz: Optional[tuple[float, float, float]] = None
        self.balance_noise: Optional[float] = None   # channel-balance read noise at white (held stimulus)
        self.flags: list[str] = []
        self._seq = 0

    # -- low-level present+read with an audit record ----------------------
    def _patch(self, signal: float, *, label: str, role: str) -> MeasurePatch:
        max_cv = self.transfer.max_cv
        cv = max(0, min(max_cv, round(signal * max_cv)))
        rgb = (cv, cv, cv)
        sig = to_signal([rgb], self.transfer)[0]
        return MeasurePatch(label=label, rgb=rgb, signal=sig, role=role, bit_depth=self.transfer.bit_depth)

    def _primary_patch(self, channel: Channel, *, label: str) -> MeasurePatch:
        max_cv = self.transfer.max_cv
        rgb = [0, 0, 0]
        rgb[{"R": 0, "G": 1, "B": 2}[channel]] = max_cv
        rgb_t = (rgb[0], rgb[1], rgb[2])
        sig = to_signal([rgb_t], self.transfer)[0]
        return MeasurePatch(label=label, rgb=rgb_t, signal=sig, role="characterize", bit_depth=self.transfer.bit_depth)

    def _read(self, patch: MeasurePatch, *, phase: str, read_index: int,
              note: Optional[str] = None) -> tuple[Reading, float]:
        """Present + read one patch, timing the wall-clock cost, and stream one ndjson
        record (same schema family as the measure loop, so the live readout can tail it)."""
        t0 = self.clock()
        reading = self.measure(patch)
        dt = self.clock() - t0
        seq = self._seq
        self._seq += 1
        if reading.xyz is not None and (self.white_xyz is None or reading.xyz[1] > self.white_xyz[1]):
            self.white_xyz = reading.xyz
        self.ndjson.emit({
            "t": _now(), "seq": seq, "phase": phase, "role": patch.role, "label": patch.label,
            "rgb": list(patch.rgb), "signal": [round(s, 6) for s in patch.signal],
            "read_index": read_index,
            "xyz": list(reading.xyz) if reading.xyz is not None else None,
            "yxy": list(reading.yxy) if reading.yxy is not None else None,
            "nits": reading.nits, "ok": reading.ok and reading.xyz is not None,
            "accepted": False, "read_seconds": round(dt, 4), "note": note,
        })
        return reading, dt

    def _emit_event(self, level: str, event: str, **data: Any) -> None:
        if self.events is not None:
            self.events.write(level, "characterize", event, **data)

    # -- drift: warm-up (reuse the measure loop's own warm_up) ------------
    def warm_up(self) -> dict[str, Any]:
        """Reuse :meth:`dlc.measure_loop._Loop.warm_up` so characterization measures the
        SAME warm-up mechanism calibration uses — harvesting reads-to-settle, the
        *discovered* cold channel, and the warm reference. A panel that won't settle
        within the (generous) observation bound is FLAGGED, not capped."""
        cfg = self.cfg
        warm_cfg = MeasureLoopConfig(
            cold_channel=self.cold_channel,
            settle_threshold=cfg.warmup_settle_threshold,
            settle_required=cfg.settle_required,
            max_warmup_reads=cfg.warmup_observe_reads,
        )
        loop = _Loop(patches=[], transfer=self.transfer, measure=self.measure,
                     config=warm_cfg, ndjson=self.ndjson, events=self.events)
        settled, reads = loop.warm_up(phase="char:warmup")
        # Carry forward what warm-up discovered.
        self.cold_channel = loop.cold_channel or self.cold_channel
        if loop.white_xyz is not None and (self.white_xyz is None or loop.white_xyz[1] > self.white_xyz[1]):
            self.white_xyz = loop.white_xyz
        # Keep the shared ndjson seq monotonic. The reused loop numbered its warm-up reads
        # 0..loop.seq_counter-1; advance our counter past them so our own reads continue
        # without collision. INVARIANT: warm_up runs exactly once, as the FIRST phase (so our
        # counter is still 0 here) — run_characterization enforces that order.
        self._seq = max(self._seq, 0) + loop.seq_counter
        if not settled:
            self.flags.append(f"panel did not settle within {cfg.warmup_observe_reads} warm-up reads "
                              f"(cold channel {loop.cold_channel}) — abnormally slow warm-up or an unstable panel")
        return {"settled": settled, "reads": reads, "cold_channel": loop.cold_channel,
                "reference_xyz": list(loop.reference_xyz) if loop.reference_xyz else None}

    # -- instrument: noise vs luminance ----------------------------------
    def noise_model(self) -> tuple[list[NoiseBand], dict[str, Any]]:
        cfg = self.cfg
        bands: list[NoiseBand] = []
        read_times_bright: list[float] = []
        levels = sorted(set(cfg.noise_levels), reverse=True)   # brightest first → white anchor
        for li, level in enumerate(levels):
            patch = self._patch(level, label=f"noise_{level:.3f}", role="characterize")
            # Discard the post-transition settle read(s), then hold the stimulus and read.
            for d in range(max(0, cfg.settle_discard)):
                self._read(patch, phase="char:noise", read_index=-(d + 1), note="settle-discard")
            reads: list[tuple[float, float, float]] = []
            for ri in range(cfg.noise_reads):
                reading, dt = self._read(patch, phase="char:noise", read_index=ri)
                if reading.xyz is not None:
                    reads.append(reading.xyz)
                    if li == 0:
                        read_times_bright.append(dt)
            if not reads:
                self.flags.append(f"no usable read at signal {level:.3f} during noise characterization")
                continue
            mean = _mean_xyz(reads)
            anchor = self.white_xyz or mean
            sigma_de = _sigma_de(reads, anchor)
            ys = [r[1] for r in reads]
            sigma_rel = (_pstd(ys) / _mean(ys)) if _mean(ys) > 0 else None
            band = NoiseBand(nits=round(mean[1], 6), sigma_de=round(sigma_de, 6),
                             sigma_rel=(round(sigma_rel, 6) if sigma_rel is not None else None),
                             reads=len(reads))
            bands.append(band)
            if li == 0 and len(reads) >= 2:
                # The channel-balance read noise on a HELD white stimulus — the purest floor
                # for the drift threshold (read noise only, no creep), so the interleaved drift
                # reference trips on real temperature movement, not on meter jitter.
                bals = [normalized_channels(r) for r in reads]
                self.balance_noise = round(max(_pstd([b[c] for b in bals]) for c in CHANNELS), 6)
            self._emit_event("INFO", "noise_band", nits=band.nits, sigma_de=band.sigma_de, reads=band.reads)
        bands.sort(key=lambda b: b.nits)
        # White repeatability is the canary for meter/correction trouble.
        if bands and bands[-1].sigma_de > cfg.abnormal_white_sigma_de:
            self.flags.append(f"read-to-read σ at white is {bands[-1].sigma_de:.3f} ΔE "
                              f"(> {cfg.abnormal_white_sigma_de:.3f}) — check the meter seating / correction")
        read_overhead = round(sorted(read_times_bright)[len(read_times_bright) // 2], 4) if read_times_bright else None
        return bands, {"bands": [b.as_dict() for b in bands], "read_overhead_s": read_overhead}

    # -- display: native white/black/primaries ---------------------------
    def native_levels(self) -> dict[str, Any]:
        cfg = self.cfg
        # Native black.
        black_patch = self._patch(0.0, label="native_black", role="characterize")
        black_reads = [r for r in (self._read(black_patch, phase="char:native", read_index=i)[0].xyz
                                   for i in range(cfg.black_reads)) if r is not None]
        black_nits = round(_mean_xyz(black_reads)[1], 6) if black_reads else None
        if not black_reads:
            self.flags.append("no usable native-black read")
        # Native white (re-read the full-white stimulus; the noise pass already anchored it).
        white_patch = self._patch(1.0, label="native_white", role="characterize")
        white_reads = [r for r in (self._read(white_patch, phase="char:native", read_index=i)[0].xyz
                                   for i in range(cfg.primary_reads)) if r is not None]
        white_mean = _mean_xyz(white_reads) if white_reads else self.white_xyz
        white_xy = list(_xy(white_mean)) if white_mean else None
        white_nits = round(white_mean[1], 6) if white_mean else None
        # Native primaries (full R/G/B).
        primaries: dict[str, list[float]] = {}
        for ch in CHANNELS:
            patch = self._primary_patch(ch, label=f"native_{ch}")
            reads = [r for r in (self._read(patch, phase="char:native", read_index=i)[0].xyz
                                 for i in range(cfg.primary_reads)) if r is not None]
            if reads:
                primaries[ch] = [round(c, 6) for c in _xy(_mean_xyz(reads))]
        return {"white_xy": white_xy, "white_nits": white_nits, "black_nits": black_nits,
                "primaries": primaries or None}

    # -- display: step-response settle -----------------------------------
    def settle(self) -> dict[str, Any]:
        cfg = self.cfg
        out: dict[str, float] = {}
        observed: dict[str, dict[str, Any]] = {}
        for name, level in cfg.settle_levels.items():
            # Force a transition FROM a contrasting level so we measure a real step response.
            from_level = 0.0 if level >= 0.5 else 1.0
            self._read(self._patch(from_level, label=f"settle_{name}_from", role="characterize"),
                       phase="char:settle", read_index=-1, note="transition-from")
            patch = self._patch(level, label=f"settle_{name}", role="characterize")
            prev: Optional[tuple[float, float, float]] = None
            consecutive = 0
            settled = False
            t0 = self.clock()
            elapsed = 0.0
            reads = 0
            for ri in range(cfg.settle_observe_reads):
                reading, _dt = self._read(patch, phase="char:settle", read_index=ri)
                reads += 1
                elapsed = self.clock() - t0
                if reading.xyz is None:
                    prev = None
                    consecutive = 0
                    continue
                if prev is not None:
                    anchor = self.white_xyz or reading.xyz
                    de = delta_e2000(xyz_to_lab(prev, anchor), xyz_to_lab(reading.xyz, anchor))
                    if de <= cfg.settle_threshold_de:
                        consecutive += 1
                    else:
                        consecutive = 0
                    if consecutive >= cfg.settle_required:
                        settled = True
                        break
                prev = reading.xyz
            if settled:
                out[name] = round(elapsed, 4)
                observed[name] = {"settled": True, "seconds": round(elapsed, 4), "reads": reads}
            else:
                observed[name] = {"settled": False, "seconds": round(elapsed, 4), "reads": reads}
                self.flags.append(f"{name} settle did not converge within {cfg.settle_observe_reads} reads "
                                  f"({elapsed:.1f}s) — abnormally slow step response (FLAG, not capped)")
            self._emit_event("INFO" if settled else "WARN", "settle", patch_level=name,
                             settled=settled, seconds=observed[name]["seconds"], reads=reads)
        # The conservative worst case is what calibration should wait out.
        settle_seconds = max(out.values()) if out else None
        return {"settle_seconds": settle_seconds, "settle_by_level": (out or None), "observed": observed}

    # -- drift: post-warm creep ------------------------------------------
    def creep(self) -> dict[str, Any]:
        """Post-warm creep: hold ONE neutral stimulus and watch it drift over the window.
        Anchored on the FIRST in-window read (same stimulus throughout) so the ΔE reflects
        only temperature creep — NOT the warm-up reference's cold-channel bias, which would
        otherwise contaminate it (a held warm panel must read 0 creep, not the bias offset)."""
        cfg = self.cfg
        patch = self._patch(0.5, label="creep_ref", role="neutral_ref")
        ref: Optional[tuple[float, float, float]] = None   # first usable read = the in-window anchor
        ref_lab = None
        ref_bal = None
        anchor = self.white_xyz
        t0 = 0.0
        des: list[tuple[float, float]] = []     # (elapsed_min, ΔE vs the first creep read)
        bal_deltas: list[float] = []            # channel-balance deltas vs the first creep read
        for ri in range(cfg.creep_reads):
            if cfg.creep_dwell_s and ri and not self.injected_clock:
                # Real runs spread the creep window over wall time; an injected (test) clock
                # controls time itself, so never sleep there (keeps the clock seam honest).
                time.sleep(cfg.creep_dwell_s)
            reading, _dt = self._read(patch, phase="char:creep", read_index=ri)
            if reading.xyz is None:
                continue
            if ref is None:
                ref = reading.xyz
                t0 = self.clock()
                anchor = self.white_xyz or ref
                ref_lab = xyz_to_lab(ref, anchor)
                ref_bal = normalized_channels(ref)
                continue
            elapsed_min = max(0.0, (self.clock() - t0) / 60.0)
            des.append((elapsed_min, delta_e2000(xyz_to_lab(reading.xyz, anchor), ref_lab)))
            cur_bal = normalized_channels(reading.xyz)
            bal_deltas.append(max(abs(cur_bal[c] - ref_bal[c]) for c in CHANNELS))
        # Creep rate: ΔE accrued over the window, per minute (None if the window had no
        # time span — e.g. a synthetic clock that didn't advance, or too few usable reads).
        creep_rate: Optional[float] = None
        if des:
            last_min, last_de = des[-1]
            creep_rate = round(last_de / last_min, 5) if last_min > 1e-6 else None
        return {"creep_rate_de_per_min": creep_rate, "samples": len(des),
                "max_balance_delta": (round(max(bal_deltas), 6) if bal_deltas else None),
                "max_de": (round(max(d for _, d in des), 4) if des else None)}

    # -- recommendations derived from the measured axes ------------------
    def recommend(self, *, bands: Sequence[NoiseBand], read_overhead_s: Optional[float],
                  settle_seconds: Optional[float], creep: dict[str, Any]) -> dict[str, Any]:
        cfg = self.cfg
        # Drift threshold (channel-balance space, matching evaluate_drift): set above the
        # measured read-to-read *channel-balance* noise on a held white (creep excluded) so
        # real temperature creep trips it but meter jitter does not. Derived ONLY from a
        # balance-space estimate — we deliberately do NOT fall back to relative-luminance σ,
        # which lives in a different space (luminance dispersion, largely common-mode) and
        # would conflate units. None when no balance estimate exists → the loop keeps its
        # conservative default rather than a guess.
        drift_threshold = (round(max(0.003, 3.0 * self.balance_noise), 6)
                           if self.balance_noise and self.balance_noise > 0 else None)
        # Neutral interval: how many patches before accumulated creep would exceed the
        # calibration read tolerance, given the per-patch time (measured read time + the
        # measured settle dwell each patch pays). Framed entirely in ΔE so the units are
        # consistent. Coarse priors the LLM can override.
        creep_rate = creep.get("creep_rate_de_per_min")
        neutral_interval: Optional[int] = None
        if creep_rate is not None:
            per_patch_min = ((read_overhead_s or 2.0) + (settle_seconds or 0.0)) / 60.0
            creep_per_patch = creep_rate * per_patch_min
            if creep_per_patch <= 1e-9:
                neutral_interval = 32
            else:
                neutral_interval = int(max(4, min(32, math.floor(cfg.read_tolerance_de / creep_per_patch))))
        return {"recommended_drift_threshold": drift_threshold,
                "recommended_neutral_interval": neutral_interval}


def run_characterization(
    *,
    measure: MeasureFn,
    transfer: Transfer,
    config: Optional[CharacterizeConfig] = None,
    cold_channel: Optional[Channel] = None,
    display: str = "characterized",
    events: Optional[EventWriter] = None,
    ndjson_path: Optional[Path] = None,
    clock: Optional[Clock] = None,
) -> CharacterizeResult:
    """Run a full characterization pass over the single :data:`MeasureFn` seam and return
    the measured :class:`CharacterizeResult` (whose ``dip`` the caller stamps with the
    display name / build date / instrument / correction). Writes ``characterize.ndjson``
    when ``ndjson_path`` is given. ``clock`` defaults to :func:`time.monotonic`; tests
    inject a deterministic stand-in so settle / creep timing is reproducible."""
    cfg = config or CharacterizeConfig()
    ndjson = _NdjsonWriter(ndjson_path)
    ch = _Characterizer(measure=measure, transfer=transfer, config=cfg, ndjson=ndjson,
                        events=events, clock=clock or time.monotonic,
                        injected_clock=clock is not None, cold_channel=cold_channel)

    warm = ch.warm_up()
    bands, noise = ch.noise_model()
    native = ch.native_levels()
    settle = ch.settle()
    creep = ch.creep()
    rec = ch.recommend(bands=bands, read_overhead_s=noise.get("read_overhead_s"),
                       settle_seconds=settle.get("settle_seconds"), creep=creep)

    noise_floor = _noise_floor_nits(bands, cfg.read_tolerance_de, native.get("black_nits"))

    dip = DisplayInstrumentProfile(
        display=display,
        noise_model=bands,
        noise_floor_nits=(round(noise_floor, 6) if noise_floor is not None else None),
        read_overhead_s=noise.get("read_overhead_s"),
        settle_seconds=settle.get("settle_seconds"),
        settle_by_level=settle.get("settle_by_level"),
        native_white_xy=native.get("white_xy"),
        native_white_nits=native.get("white_nits"),
        native_black_nits=native.get("black_nits"),
        native_primaries=native.get("primaries"),
        warmup_reads_to_settle=(warm.get("reads") if warm.get("settled") else None),
        cold_channel=ch.cold_channel,
        creep_rate_de_per_min=creep.get("creep_rate_de_per_min"),
        recommended_neutral_interval=rec.get("recommended_neutral_interval"),
        recommended_drift_threshold=rec.get("recommended_drift_threshold"),
        notes=list(ch.flags),
    )

    needs = bool(ch.flags)
    question = None
    if needs:
        question = ("characterization surfaced abnormal panel/meter behaviour: "
                    + "; ".join(ch.flags)
                    + " — accept the learned profile (the calibration loop will use it as priors and "
                    "still flag per-patch), or recharacterize?")

    digest = {
        "warm": warm.get("settled"),
        "warmup_reads_to_settle": dip.warmup_reads_to_settle,
        "cold_channel": ch.cold_channel,
        "noise": noise["bands"],
        "noise_floor_nits": dip.noise_floor_nits,
        "read_overhead_s": dip.read_overhead_s,
        "native_white_xy": dip.native_white_xy,
        "native_white_nits": dip.native_white_nits,
        "native_black_nits": dip.native_black_nits,
        "native_primaries": dip.native_primaries,
        "settle_seconds": dip.settle_seconds,
        "settle_by_level": dip.settle_by_level,
        "creep_rate_de_per_min": dip.creep_rate_de_per_min,
        "recommended_neutral_interval": dip.recommended_neutral_interval,
        "recommended_drift_threshold": dip.recommended_drift_threshold,
        "flags": list(ch.flags),
        "needs_adjudication": needs,
    }
    if events is not None:
        events.write("INFO" if not needs else "WARN", "characterize", "completed",
                     bands=len(bands), warm=warm.get("settled"), flags=len(ch.flags))

    return CharacterizeResult(dip=dip, digest=digest, flags=list(ch.flags),
                              needs_adjudication=needs, question=question,
                              ndjson_path=str(ndjson_path) if ndjson_path else None)
