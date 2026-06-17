"""Closed-loop thermal controller — scaled golden-ratio PREHEAT + regime classification.

A panel (especially HDR) can take tens of minutes to warm in and may never reach a true
steady temperature. Rather than blast a static peak field (overshoots, heats unevenly, only
warms the white point) or hold static white and hope, this drives the SAME diverse
golden-ratio content the calibration will use, scaled to a higher luminance to inject heat
faster, and steers a luminance scale ``k`` closed-loop:

* **proactively glide ``k`` toward 1.0** each block — ease off the gas so we approach the
  operating-load equilibrium from below instead of chasing the high-``k`` equilibrium and
  overshooting;
* **reactively re-inject** (bump ``k`` back up) if we reach ``k≈1`` while the panel is still
  warming *directionally* (the active/cold channel still drifting one way);
* **converge** when ``k=1.0`` AND the warm-in drift is **non-directional & in-band** — judged
  from the **net-vs-gross** of the active channel over a sliding window, NOT a single-block
  magnitude (a slow steady directional creep stays under any per-block band yet never settles;
  net/gross catches it).

The regime falls out of the same net/gross numbers:

* **convergent** — sustained net drift falls in-band and gross is also small (a steady
  temperature exists);
* **fluctuating** — net stays in-band but gross is large (wanders around a mean, no steady
  state — HDR): calibrate by maintaining a consistent thermal load + frequent drift checks;
* **warming** — net stays above band and directional at the observation bound (still ramping).

Self-activating: a warm panel reads in-band from the first block, so ``k`` decays straight to
1.0 and it converges fast — no preheat happens. **Thresholds derive from the measured per-panel
``balance_noise`` (≈3×), never a hardcoded constant** — so the same controller is correct on any
panel. Flag-don't-cap: a panel that won't converge within the (generous) observation bound is
FLAGGED with its regime, never silently truncated.

Numpy-free (stdlib only): drives the single :data:`~dlc.measure_loop.MeasureFn` seam and a
monotonic clock, so it runs in tests against :class:`~dlc.measure_loop.SyntheticPanel`
(``load_thermal=True``) with an injected clock — no display, no meter.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .drift import CHANNELS, Channel, normalized_channels
from .engine.patches import Patch, Transfer, sort_patches, to_signal
from .measure_loop import MeasureFn, MeasurePatch

__all__ = ["ThermalConfig", "ThermalResult", "net_over_gross", "ThermalController"]

Clock = Callable[[], float]


def _pstd(xs: Sequence[float]) -> float:
    """Population standard deviation (the read-to-read scatter of a held stimulus)."""
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def net_over_gross(series: Sequence[float]) -> tuple[float, float, Optional[float]]:
    """For a 1-D series: ``net`` (|last − first|, the directional drift), ``gross``
    (Σ|consecutive Δ|, the total motion), and ``net/gross`` (→1 directional, →0 oscillating;
    ``None`` when there's no motion). The discriminator between "still warming" (net≈gross)
    and "wandering in place" (net≪gross)."""
    if len(series) < 2:
        return 0.0, 0.0, None
    net = abs(series[-1] - series[0])
    gross = sum(abs(series[i + 1] - series[i]) for i in range(len(series) - 1))
    ratio = (net / gross) if gross > 1e-12 else None
    return net, gross, ratio


@dataclass(frozen=True)
class ThermalConfig:
    """Knobs for the closed-loop preheat. Defaults favour correctness (this runs once, rarely);
    the load/window counts are the deliberate fixed samples, NOT caps on warming."""

    k_start: float = 1.6            # SOAK scale while warm-in is measured; 1.0 ⇒ no preheat
    k_decay: float = 0.7            # k <- 1 + (k-1)*k_decay each block when gliding back toward 1
    load_reads_per_block: int = 12  # scaled-content reads per block (the heat per block)
    ref_reads: int = 3              # reads of the fixed neutral sensor per block (median-ish)
    window_blocks: int = 5          # sliding window for the net/gross judgement (~4-6)
    net_gross_ratio: float = 0.5    # net/gross ≥ this ⇒ DIRECTIONAL (still warming)
    drift_sigma_mult: float = 3.0   # drift threshold = max(drift_floor, mult × balance_noise)
    drift_floor: float = 0.003      # threshold floor when balance_noise is tiny/unknown
    fluct_gross_mult: float = 2.0   # at convergence, gross > mult × threshold ⇒ FLUCTUATING (not flat)
    converge_blocks: int = 2        # consecutive in-band (net) blocks ⇒ converged
    max_blocks: int = 240           # observation bound; exceeding it FLAGS (never a silent cap)
    peak_nits: Optional[float] = None  # clamp scaled luminance here (None ⇒ transfer/display ceiling)
    # reference-read sanity (catch a frozen / wrong display or a dislodged meter) ---
    ref_sanity_low: float = 0.33    # measured ref nits below this × commanded ⇒ implausible
    ref_sanity_high: float = 3.0    # measured ref nits above this × commanded ⇒ implausible
    ref_sanity_blocks: int = 2      # consecutive implausible reads ⇒ display/meter COMPROMISED (abort+flag)


@dataclass
class ThermalResult:
    regime: str                                  # convergent | fluctuating | warming
    converged: bool
    blocks: int
    content_reads: int                           # scaled-content reads spent warming (the "dose")
    warmup_minutes: Optional[float]
    warmin_magnitude: float                      # cumulative active-channel net drift, start→converge
    fluctuation_envelope: float                  # residual wander band (feeds the run-time drift watch)
    drift_threshold: float                       # the net threshold used (from balance_noise)
    active_channel: Optional[Channel]            # the channel that drifted most (the cold/temperamental one)
    final_k: float
    compromised: bool = False                    # display/meter read implausibly (frozen patch / wrong mode)
    flags: list[str] = field(default_factory=list)
    needs_adjudication: bool = False
    question: Optional[str] = None
    digest: dict[str, Any] = field(default_factory=dict)


class ThermalController:
    def __init__(self, *, measure: MeasureFn, transfer: Transfer,
                 content: Sequence[Patch], ref_nits: float, balance_noise: Optional[float],
                 config: Optional[ThermalConfig] = None,
                 clock: Optional[Clock] = None,
                 emit: Optional[Callable[[dict[str, Any]], None]] = None,
                 event: Optional[Callable[..., None]] = None) -> None:
        self.measure = measure
        self.transfer = transfer
        self.cfg = config or ThermalConfig()
        self.ref_nits = ref_nits
        self.balance_noise = balance_noise
        self.clock = clock or time.monotonic
        self.emit = emit                          # optional ndjson sink (per block)
        self.event = event                        # optional structured-event sink
        # Golden-ratio (thermal) order so the running average load is held steady within a block.
        self.content: list[Patch] = sort_patches(list(content), "thermal", transfer)
        if not self.content:
            raise ValueError("ThermalController needs at least one content patch")
        self._peak = self.cfg.peak_nits or self.transfer.cv_to_nits(self.transfer.max_cv)

    # -- patch builders ---------------------------------------------------
    def _scaled(self, base: Patch, k: float) -> MeasurePatch:
        """Scale a base patch's per-channel luminance by ``k``, clamped to peak, back to cv."""
        rgb = []
        for c in base:
            if c <= 0:
                rgb.append(0)
                continue
            nits = self.transfer.cv_to_nits(c) * k
            rgb.append(self.transfer.nits_to_cv(min(nits, self._peak)))
        rgb_t = (rgb[0], rgb[1], rgb[2])
        sig = to_signal([rgb_t], self.transfer)[0]
        return MeasurePatch(label="thermal_load", rgb=rgb_t, signal=sig,
                            role="warmup", bit_depth=self.transfer.bit_depth)

    def _ref_patch(self) -> MeasurePatch:
        cv = self.transfer.nits_to_cv(self.ref_nits)
        rgb_t = (cv, cv, cv)
        sig = to_signal([rgb_t], self.transfer)[0]
        return MeasurePatch(label="thermal_ref", rgb=rgb_t, signal=sig,
                            role="neutral_ref", bit_depth=self.transfer.bit_depth)

    def _read_ref(self) -> tuple[Optional[dict[Channel, float]], list[dict[Channel, float]]]:
        """Read the fixed neutral sensor ``ref_reads`` times (back-to-back, same temperature).
        Return the mean channel balance AND the per-read balances — the within-block scatter is
        the read noise the threshold self-calibrates from when ``balance_noise`` isn't supplied."""
        ref = self._ref_patch()
        xyzs = []
        for _ in range(max(1, self.cfg.ref_reads)):
            r = self.measure(ref)
            if r.xyz is not None:
                xyzs.append(r.xyz)
        if not xyzs:
            return None, []
        n = len(xyzs)
        mean = (sum(v[0] for v in xyzs) / n, sum(v[1] for v in xyzs) / n, sum(v[2] for v in xyzs) / n)
        # carry absolute ref nits for the digest via attribute side-channel
        self._last_ref_nits = mean[1]
        return normalized_channels(mean), [normalized_channels(v) for v in xyzs]

    # -- the closed loop --------------------------------------------------
    def run(self) -> ThermalResult:
        cfg = self.cfg
        threshold = max(cfg.drift_floor,
                        cfg.drift_sigma_mult * self.balance_noise) if self.balance_noise else cfg.drift_floor
        noise_blocks: list[float] = []                    # within-block ref-read scatter (self-calibration)
        history: list[dict[Channel, float]] = []          # ref channel balance per block
        first_bal: Optional[dict[Channel, float]] = None
        k = 1.0      # start at operating load; the loop raises k to SOAK only when it MEASURES warm-in
        in_band = 0
        content_reads = 0
        ci = 0
        flags: list[str] = []
        t0: Optional[float] = None
        block = 0
        converged = False
        regime = "warming"
        active: Optional[Channel] = None
        net_active = gross_active = 0.0
        ratio: Optional[float] = None
        sane_violations = 0
        compromised = False
        self._last_ref_nits = None

        while block < cfg.max_blocks:
            block += 1
            # --- LOAD: scaled diverse content (read-and-discard = heat + cadence) ---
            for _ in range(max(1, cfg.load_reads_per_block)):
                self.measure(self._scaled(self.content[ci % len(self.content)], k))
                ci += 1
                content_reads += 1
            # --- PROBE: the fixed neutral warm-in sensor ---
            bal, per_read = self._read_ref()
            now = self.clock()
            if t0 is None:
                t0 = now
            if bal is None:
                flags.append(f"thermal block {block}: no usable reference read")
                continue
            if first_bal is None:
                first_bal = bal
            history.append(bal)
            # SANITY: the reference read luminance must be plausibly near the COMMANDED ref level.
            # A wild mismatch (e.g. reading 1156 nits when we asked for ~92) means the display/meter
            # is compromised — a frozen patch (D3D render hang), a wrong colorspace, or a dislodged
            # meter — so the "drift" data is garbage. A frozen display reads identical values that
            # otherwise LOOK like perfect convergence; catch it here and FLAG, never silently accept.
            if self.ref_nits and self._last_ref_nits is not None and self._last_ref_nits > 0:
                rn = self._last_ref_nits / self.ref_nits
                if rn < cfg.ref_sanity_low or rn > cfg.ref_sanity_high:
                    sane_violations += 1
                    if sane_violations >= cfg.ref_sanity_blocks:
                        flags.append(
                            f"reference read {self._last_ref_nits:.0f} nits is wildly off the commanded "
                            f"~{self.ref_nits:.0f} nits ({rn:.1f}x) for {sane_violations} blocks — display/"
                            "meter COMPROMISED (frozen patch / wrong colorspace / meter dislodged); thermal "
                            "data is not trustworthy, abort + investigate")
                        compromised = True
                        self._block_record(block, k, 0.0, 0.0, None, threshold, None, "COMPROMISED")
                        break
                else:
                    sane_violations = 0
            # Self-calibrate the threshold from the within-block read scatter (read noise at a
            # fixed temperature) when no balance_noise was supplied — so the gate is keyed to THIS
            # panel+meter's noise, never a hardcoded constant. Estimated once, after a couple blocks.
            if self.balance_noise is None and len(per_read) >= 2:
                block_sigma = max(_pstd([b[ch] for b in per_read]) for ch in CHANNELS)
                noise_blocks.append(block_sigma)
                if len(noise_blocks) == 2:
                    est = sum(noise_blocks) / len(noise_blocks)
                    threshold = max(cfg.drift_floor, cfg.drift_sigma_mult * est)
            window = history[-cfg.window_blocks:]
            # net/gross per channel over the window; the active channel is the biggest mover.
            net_active = gross_active = 0.0
            ratio = None
            active = None
            if len(window) >= 2:
                best_gross = -1.0
                for ch in CHANNELS:
                    series = [b[ch] for b in window]
                    net, gross, r = net_over_gross(series)
                    if gross > best_gross:
                        best_gross, active = gross, ch
                        net_active, gross_active, ratio = net, gross, r
            # --- CONTROLLER: SOAK while we MEASURE warm-in (the active channel still rising,
            #     directional), else GLIDE toward operating load and converge. Direction-aware:
            #     a panel COOLING past the operating equilibrium (signed_net < 0) is NOT warming,
            #     so it glides/settles instead of re-soaking (no sawtooth, no over-heating). The
            #     warm-in direction = active (cold) channel rising; if a panel drifts the other
            #     way we simply never soak and fall back to plain warm-by-waiting (fails safe). ---
            signed_net = (window[-1][active] - window[0][active]) if (active and len(window) >= 2) else 0.0
            warming = (ratio is not None and ratio >= cfg.net_gross_ratio
                       and net_active > threshold and signed_net > 0.0)
            if warming:
                k = cfg.k_start                                     # SOAK: inject heat
                in_band = 0
                state = "soak"
            else:
                k = 1.0 + (k - 1.0) * cfg.k_decay                   # GLIDE toward operating load
                state = "glide"
            near_one = abs(k - 1.0) < 0.05
            in_band_now = near_one and len(window) >= min(cfg.window_blocks, 2) and net_active <= threshold
            if in_band_now:
                in_band += 1
                state = f"in-band x{in_band}"
            elif near_one:
                in_band = 0
            if in_band >= cfg.converge_blocks:
                converged = True
                # flat vs wandering: gross small ⇒ convergent, gross large ⇒ fluctuating
                regime = ("fluctuating" if gross_active > cfg.fluct_gross_mult * threshold
                          else "convergent")
                state = f"CONVERGED:{regime}"
            self._block_record(block, k, net_active, gross_active, ratio, threshold, active, state)
            if converged:
                break

        minutes = round((self.clock() - t0) / 60.0, 4) if t0 is not None else 0.0
        warmin = (abs(history[-1][active] - first_bal[active])
                  if (active and first_bal and history) else 0.0)
        # Residual wander band over the final window (the fluctuation envelope) — feeds the
        # run-time drift watch's threshold.
        envelope = 0.0
        if history:
            win = history[-cfg.window_blocks:]
            for ch in CHANNELS:
                vals = [b[ch] for b in win]
                envelope = max(envelope, max(vals) - min(vals))
        if compromised:
            regime = "compromised"          # the display/meter was bad — the regime is unknowable
        elif not converged:
            regime = "fluctuating" if (ratio is not None and ratio < cfg.net_gross_ratio) else "warming"
        # Flag by regime, NOT just by (non-)convergence: a fluctuating panel that "converges" (a
        # bounded wander) is the fluctuating STEADY STATE and still needs the maintain-load strategy,
        # so it's surfaced too. A convergent panel is clean (no flag). A COMPROMISED run already has
        # its flag from the loop — don't double-flag it as a (meaningless) regime.
        if not compromised:
            if regime == "fluctuating":
                flags.append(
                    f"panel never reaches a steady temperature (net/gross {ratio}, residual band "
                    f"{round(envelope, 5)}) — thermally DYNAMIC: calibrate by maintaining a consistent "
                    "thermal load (golden-ratio order) + aggressive drift checks, not by warming to a target")
            elif not converged:   # warming
                flags.append(
                    f"panel did not thermally converge within {cfg.max_blocks} blocks "
                    f"(net {net_active:.4f} vs threshold {threshold:.4f}) — still warming; "
                    "warm longer / inject more heat")
        needs = bool(flags)
        question = None
        if needs:
            question = ("thermal characterization flagged: " + "; ".join(flags)
                        + " — accept the learned regime/envelope (the run will drift-watch and "
                        "still flag), or recharacterize?")
        digest = {
            "regime": regime, "converged": converged, "blocks": block,
            "content_reads": content_reads, "warmup_minutes": minutes,
            "warmin_magnitude": round(warmin, 5), "fluctuation_envelope": round(envelope, 5),
            "drift_threshold": round(threshold, 6), "active_channel": active,
            "final_k": round(k, 3), "net_active": round(net_active, 5),
            "gross_active": round(gross_active, 5), "net_over_gross": ratio,
            "compromised": compromised,
        }
        if self.event is not None:
            self.event("INFO" if (converged and not compromised) else "WARN", "thermal_regime", **digest)
        return ThermalResult(
            regime=regime, converged=converged, blocks=block, content_reads=content_reads,
            warmup_minutes=(minutes if regime == "convergent" else None),
            warmin_magnitude=round(warmin, 5), fluctuation_envelope=round(envelope, 5),
            drift_threshold=round(threshold, 6), active_channel=active, final_k=round(k, 3),
            compromised=compromised,
            flags=flags, needs_adjudication=needs, question=question, digest=digest)

    def _block_record(self, block: int, k: float, net: float, gross: float,
                      ratio: Optional[float], threshold: float,
                      active: Optional[Channel], state: str) -> None:
        rec = {"phase": "thermal", "block": block, "k": round(k, 3),
               "ref_nits": (round(self._last_ref_nits, 3) if self._last_ref_nits else None),
               "net": round(net, 5), "gross": round(gross, 5),
               "net_over_gross": ratio, "threshold": round(threshold, 6),
               "active_channel": active, "state": state}
        if self.emit is not None:
            self.emit(rec)
