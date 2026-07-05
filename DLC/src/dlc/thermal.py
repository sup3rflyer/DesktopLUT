"""Closed-loop thermal controller — scaled golden-ratio PREHEAT + regime classification.

A panel (especially HDR) can take tens of minutes to warm in and may never reach a true
steady temperature. Rather than blast a static peak field (overshoots, heats unevenly, only
warms the white point) or hold static white and hope, this drives the SAME diverse
golden-ratio content the calibration will use, scaled to a higher luminance to inject heat
faster, and steers a luminance scale ``k`` closed-loop:

* **proactively glide ``k`` toward 1.0** each block — ease off the gas so we approach the
  operating-load equilibrium from below instead of chasing the high-``k`` equilibrium and
  overshooting;
* **reactively re-inject** (bump ``k`` back up) only when warm-in is **directional beyond read
  noise** in the warm-in direction — debounced (one noisy block can never soak) and re-soak
  rate-limited (a cooled-then-jittered panel can't saw-tooth);
* **converge** when ``k≈1`` AND both tracked scalars — the neutral **luminance Y** and the active
  channel's **balance** — are **zero-slope-within-self-calibrated-read-noise** over a window of
  operating-load reads. The gate is a slope-vs-noise t-test (``|slope| ≤ z·SE(slope)``, robust
  Theil–Sen slope), NOT a fixed magnitude band: the old ``net ≤ 3×balance_noise`` gate sat *on*
  the read-noise floor, so a settled panel's chance excursions read as warm-in and it limit-cycled
  to the bound (HW 2026-06-24). A residual peak-to-peak bound rejects a large oscillation whose
  slope is momentarily flat at a turning point.

The regime is a DESCRIPTOR over the same numbers, plus the budget-boundary verdict:

* **convergent** — slope-flat on both scalars with a bounded residual (a steady temperature exists);
* **fluctuating** — at the budget, NON-directional but never a single steady point (wanders around a
  mean — HDR): calibrate by maintaining a consistent thermal load + frequent drift checks;
* **warming** — at the budget, still DIRECTIONAL (a real ramp the budget cut short).

Self-activating: a warm panel reads zero-slope from the first window, so ``k`` decays straight to
1.0 and it converges fast — no preheat happens. **Per-quantity thresholds self-calibrate from the
within-block back-to-back read scatter (SE-of-the-mean), or the DIP's measured ``balance_noise``,
never a hardcoded constant** — so the same controller is correct on any panel. ABL is data-driven:
when the reference DIMS under soak load (``Y`` falls as load rises), luminance is dropped from the
gate (balance-only) and the run is flagged. Flag-don't-cap: a panel that won't converge inside its
budget (small for a settled panel; extended from soak onset for a real warm-in) is not silently
capped — it returns a **directional-vs-non-directional** evidence packet with ``needs_adjudication``
for the LLM/user to judge, never a silent truncation or glide.

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


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _theil_sen_slope(series: Sequence[float]) -> float:
    """Median of all pairwise slopes over integer abscissa — the robust (breakdown-resistant)
    estimate of per-block drift. Resists quantization steps and a single outlier read that would
    swing an ordinary least-squares fit (numpy-free)."""
    n = len(series)
    if n < 2:
        return 0.0
    slopes = [(series[j] - series[i]) / (j - i)
              for i in range(n) for j in range(i + 1, n)]
    return _median(slopes)


def _slope_stats(series: Sequence[float], sigma_block: float) -> tuple[float, float, float, float]:
    """The drift-vs-noise statistic for one tracked scalar over a window.

    Returns ``(slope_per_block, se_slope, zratio, signed_displacement)`` over integer abscissa
    ``0..n-1``. ``se_slope = sigma_block / sqrt(Σ(t-t̄)²)`` is the standard error of the slope under
    i.i.d. block-mean read noise ``sigma_block`` (the panel-independent self-calibration). The
    ``zratio = |slope| / se_slope`` is the discriminator: ``≤ z`` ⇒ zero-slope-within-read-noise
    (settled); ``≳ z`` ⇒ a directional drift distinguishable from noise (still warming / cooling).
    This replaces the old ``net ≤ threshold`` gate, which sat at the read-noise floor and so could
    not tell a stationary walk's chance excursions from a real ramp."""
    n = len(series)
    if n < 3:
        return 0.0, float("inf"), 0.0, 0.0
    slope = _theil_sen_slope(series)
    txx = n * (n * n - 1) / 12.0          # Σ(t - t̄)² for t = 0..n-1 (evenly spaced)
    if sigma_block > 0 and txx > 0:
        se = sigma_block / math.sqrt(txx)
        z = abs(slope) / se
    else:                                 # no read noise (deterministic) ⇒ any non-zero slope is real
        se = 0.0
        z = float("inf") if abs(slope) > 0 else 0.0
    return slope, se, z, slope * (n - 1)


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
    window_blocks: int = 5          # sliding window for the slope / net-gross judgement (~4-6)
    # CONVERGENCE = zero-slope-within-self-calibrated-read-noise (a t-test on the per-block trend),
    # NOT a fixed magnitude band sitting on the noise floor. ``slope_z`` is the |slope|/SE(slope)
    # cutoff: ≤ it ⇒ flat-in-noise, ≳ it ⇒ directional. Tracked on BOTH neutral luminance Y and the
    # active channel's balance — whichever has the larger normalized slope binds. ---
    slope_z: float = 2.0            # |slope|/SE(slope) ≤ this ⇒ flat-in-noise; ≳ this ⇒ directional
    converge_blocks: int = 2        # consecutive flat operating blocks ⇒ converged
    fast_op_blocks: int = 3         # operating-load blocks needed to land when NOT recently soaked
    # SELF-CALIBRATION of the read-noise the slope SE is keyed to. ``drift_sigma_mult``/``drift_floor``
    # still derive the reported balance ``drift_threshold`` descriptor and floor the balance σ; the
    # luminance σ floors at a small fraction of the commanded ref level (Y is in nits, not a ratio). ---
    drift_sigma_mult: float = 3.0   # balance drift_threshold = max(drift_floor, mult × balance σ)
    drift_floor: float = 0.003      # balance threshold floor when balance_noise is tiny/unknown
    y_rel_floor: float = 0.004      # luminance σ floor as a fraction of the commanded ref nits
    net_gross_ratio: float = 0.5    # DESCRIPTOR ONLY now: net/gross ≥ this ⇒ directional (regime label)
    fluct_gross_mult: float = 5.0   # operating-window peak-to-peak > mult × threshold ⇒ a large oscillation,
    #   not a settled point — blocks the clean 'converged' exit even when the slope is momentarily flat
    #   (a triangle wave is slope-zero at its turning points). A small content-driven wobble passes.
    # SOAK TRIGGER is the symmetric negation of convergence (directional-beyond-noise in the warm-in
    # direction) with DEBOUNCE so one noisy block can never soak, and a re-soak rate limit so a
    # cooled-then-jittered panel doesn't saw-tooth. ---
    soak_debounce: int = 2          # consecutive directional warm-in blocks before the FIRST soak
    resoak_cooldown: int = 3        # blocks since the last soak before another soak may fire
    # BUDGET (flag-don't-cap): a settled panel must converge inside ``min_budget_blocks``; a panel
    # that genuinely soaks earns ``warm_budget_blocks`` more from the soak onset. Exceeding the budget
    # is NOT a silent cap or glide — it emits a directional-vs-non-directional evidence packet and
    # PROCEEDS with needs_adjudication (the LLM/user decides). ``max_blocks`` is the hard backstop. ---
    min_budget_blocks: int = 12     # a settled panel converges well inside this (no soak)
    warm_budget_blocks: int = 40    # extra blocks granted from soak onset for a real warm-in + landing
    max_blocks: int = 60            # hard backstop; exceeding it FLAGS (never a silent cap)
    peak_nits: Optional[float] = None  # clamp scaled luminance here (None ⇒ transfer/display ceiling)
    # reference-read sanity (catch a frozen / wrong display or a dislodged meter) ---
    ref_sanity_low: float = 0.33    # measured ref nits below this × commanded ⇒ implausible
    ref_sanity_high: float = 3.0    # measured ref nits above this × commanded ⇒ implausible
    ref_sanity_blocks: int = 2      # consecutive implausible reads ⇒ display/meter COMPROMISED (abort+flag)
    # LANDING: convergence must be judged under SUSTAINED operating load (k≈1), never during the
    # ramp-down from a soak overshoot — cooling-from-overshoot can cancel warming-from-load into a
    # transient flat point that reads as 'converged'. So once a run has soaked, require a FULL
    # window of operating-load blocks (the overshoot flushed out of the evaluation window) before
    # counting in-band. A never-soaked (already-warm) panel keeps the fast path. ---
    operating_k_tol: float = 0.05   # |k-1| within this ⇒ this block ran at operating load
    # PROTECTION / ABL: while SOAKING (k>1 load) the fixed reference should not DIM below its
    # operating baseline — a falling reference under heavier load is active limiting (ABL / power /
    # thermal throttle), not heat, so the flatness that follows is NOT convergence. FLAG it. ---
    protection_drop_frac: float = 0.12   # ref dims > this fraction below its operating baseline ⇒ limited
    protection_blocks: int = 2           # consecutive limited reads ⇒ FLAG PROTECTION_LIMITED
    # BASELINE FAST-PATH (Phase 2, OFF by default): with a validated ``warm_baseline`` supplied, a
    # block that agrees with it (bracketed, small slope, k==1) could converge in one block. Phase 1
    # only SHADOW-LOGS the distance to gather the evidence to enable this — never short-circuits. ---
    baseline_fast_path: bool = False


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
    tau_patches: Optional[int] = None            # first-order thermal time constant in content-read
    #   (≈ measurement-patch) units, estimated from the warm-in. None when no warm-in was observed
    #   (inert/flat panel) — the patch-ordering rotation then keeps its default τ.
    warm_balance: Optional[dict[Channel, float]] = None   # the converged OPERATING-load channel balance —
    #   a validated 'this is warm' fingerprint a later calibration run compares a live read against
    #   (Phase-2 fast-path). Mean of the last operating-load reads (one noisy read can't define it).
    #   None unless operating-load equilibrium was actually reached.
    protection_limited: bool = False             # the reference DIMMED under soak load (ABL / power /
    #   thermal throttle) — the soak can't inject usable heat; flatness here is limiting, not equilibrium
    baseline_distance: Optional[float] = None    # SHADOW: max channel-balance distance of the first
    #   operating read from a supplied warm_baseline (Phase-1 evidence; never acted on yet)
    reason: Optional[str] = None                 # terminal reason code (converged:<regime> | warming |
    #   fluctuating | protection_limited | compromised) — for the digest / dashboard
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
                 warm_baseline: Optional[dict[Channel, float]] = None,
                 emit: Optional[Callable[[dict[str, Any]], None]] = None,
                 event: Optional[Callable[..., None]] = None) -> None:
        self.measure = measure
        self.transfer = transfer
        self.cfg = config or ThermalConfig()
        self.ref_nits = ref_nits
        self.balance_noise = balance_noise
        # A validated warm balance from a prior characterize run (Phase-1: shadow-logged only).
        self.warm_baseline = warm_baseline
        self.clock = clock or time.monotonic
        self.emit = emit                          # optional ndjson sink (per block)
        self.event = event                        # optional structured-event sink
        # Golden-ratio (thermal) order so the running average load is held steady within a block.
        self.content: list[Patch] = sort_patches(list(content), "thermal", transfer)
        if not self.content:
            raise ValueError("ThermalController needs at least one content patch")
        self._peak = self.cfg.peak_nits or self.transfer.cv_to_nits(self.transfer.max_cv)
        # Absolute nits of the latest reference read (attribute side-channel from _read_ref
        # into the block records/digest); initialized here so it exists before run().
        self._last_ref_nits: Optional[float] = None

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

    def _read_ref(self) -> tuple[Optional[dict[Channel, float]], list[dict[Channel, float]],
                                 Optional[float], list[float]]:
        """Read the fixed neutral sensor ``ref_reads`` times (back-to-back, same temperature).
        Return ``(mean_balance, per_read_balances, mean_Y, per_read_Y)`` — the within-block scatter
        of BOTH tracked scalars (channel balance and luminance Y) is the read noise the per-quantity
        slope SE self-calibrates from. Y (nits) is needed so convergence can require luminance to
        flatten too, not just balance (a uniform warm-in moves Y while balance stays put)."""
        ref = self._ref_patch()
        xyzs = []
        for _ in range(max(1, self.cfg.ref_reads)):
            r = self.measure(ref)
            if r.xyz is not None:
                xyzs.append(r.xyz)
        if not xyzs:
            return None, [], None, []
        n = len(xyzs)
        mean = (sum(v[0] for v in xyzs) / n, sum(v[1] for v in xyzs) / n, sum(v[2] for v in xyzs) / n)
        per_read_y = [v[1] for v in xyzs]
        # carry absolute ref nits for the digest via attribute side-channel
        self._last_ref_nits = mean[1]
        return normalized_channels(mean), [normalized_channels(v) for v in xyzs], mean[1], per_read_y

    # -- the closed loop --------------------------------------------------
    def run(self) -> ThermalResult:
        cfg = self.cfg
        n_ref = max(1, cfg.ref_reads)
        sigma_floor_bal = cfg.drift_floor / cfg.drift_sigma_mult   # block-mean balance σ never below this
        threshold = cfg.drift_floor                                # reported balance descriptor (set per block)
        sigma_bal = sigma_floor_bal
        sigma_y = cfg.y_rel_floor * (self.ref_nits or 1.0)
        noise_bal: list[float] = []                       # within-block balance read scatter (self-calibration)
        noise_y: list[float] = []                         # within-block luminance read scatter
        history: list[dict[Channel, float]] = []          # ref channel balance per block
        ref_y_hist: list[float] = []                      # ref luminance (nits) per block — the 2nd tracked scalar
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
        # --- soak debounce / decaying landing latch / protection / baseline-shadow state ---
        op_balances: list[dict[Channel, float]] = []   # ref balances measured under OPERATING load (k≈1)
        op_ref_y: list[float] = []                      # ref luminance measured under OPERATING load
        op_streak = 0                                   # consecutive operating-load blocks (the landing gate)
        did_soak = False                                # a SOAK overshoot ever happened (digest/τ)
        blocks_since_soak = cfg.resoak_cooldown         # DECAYING latch (no permanent did_soak penalty):
        #   the full-window landing requirement applies only while this < window_blocks (overshoot still
        #   in the evaluation window); after that the fast path resumes. Starts high so the cooldown
        #   gate doesn't block the very first soak.
        directional_streak = 0                          # consecutive directional warm-in blocks (soak debounce)
        ref_op_baseline: Optional[float] = None         # the reference luminance at operating load (ABL anchor)
        protection_hits = 0
        protection_limited = False
        baseline_distance: Optional[float] = None       # first operating read's distance to warm_baseline (shadow)
        emitted_cats: set[str] = set()                  # state-transition digest events emitted (once each)
        soft_budget = cfg.min_budget_blocks             # a SETTLED panel must converge inside this; a real
        #   soak earns warm_budget_blocks more from its onset. Exceeding it FLAGS (never a silent cap).
        budget_exhausted = False

        while block < cfg.max_blocks:
            block += 1
            load_k = k                                  # the scale driving THIS block's load
            load_operating = abs(load_k - 1.0) < cfg.operating_k_tol
            # --- LOAD: scaled diverse content (read-and-discard = heat + cadence) ---
            for _ in range(max(1, cfg.load_reads_per_block)):
                self.measure(self._scaled(self.content[ci % len(self.content)], load_k))
                ci += 1
                content_reads += 1
            # --- PROBE: the fixed neutral warm-in sensor (balance AND luminance) ---
            bal, per_read, y_mean, per_read_y = self._read_ref()
            now = self.clock()
            if t0 is None:
                t0 = now
            if bal is None:
                flags.append(f"thermal block {block}: no usable reference read")
                continue
            if first_bal is None:
                first_bal = bal
            history.append(bal)
            ref_y_hist.append(y_mean if y_mean is not None else 0.0)
            blocks_since_soak += 1
            # Self-calibrate the per-quantity read noise the slope SE is keyed to, from the within-block
            # back-to-back scatter (the read-noise at a fixed temperature) divided by √n (SE-of-the-mean,
            # per the project dark-noise-trust convention) — so the gate is keyed to THIS panel+meter,
            # never a hardcoded constant. ``balance_noise``, when the DIP supplies it, is already a
            # block-scale figure (≈ recommended_drift_threshold/3) so it's used directly.
            if self.balance_noise is not None:
                sigma_bal = max(self.balance_noise, sigma_floor_bal)
            elif len(per_read) >= 2:
                noise_bal.append(max(_pstd([b[ch] for b in per_read]) for ch in CHANNELS))
                sigma_bal = max((sum(noise_bal) / len(noise_bal)) / math.sqrt(n_ref), sigma_floor_bal)
            threshold = cfg.drift_sigma_mult * sigma_bal          # reported balance drift_threshold descriptor
            y_floor = cfg.y_rel_floor * (self.ref_nits or (self._last_ref_nits or 1.0))
            if len(per_read_y) >= 2:
                noise_y.append(_pstd(per_read_y))
                sigma_y = max((sum(noise_y) / len(noise_y)) / math.sqrt(n_ref), y_floor)
            else:
                sigma_y = y_floor
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
            # Operating-load bookkeeping (the landing gate): a block whose LOAD ran at operating
            # level (k≈1) contributes to convergence; soak/glide blocks do not (their probe carries
            # the overshoot transient). op_streak is the run of consecutive operating-load blocks.
            if load_operating:
                op_streak += 1
                op_balances.append(bal)
                op_ref_y.append(self._last_ref_nits if self._last_ref_nits else 0.0)
                if ref_op_baseline is None and self._last_ref_nits:
                    ref_op_baseline = self._last_ref_nits     # the reference luminance at operating load
                # SHADOW (Phase 1): how far is the first operating read from a validated warm
                # baseline? Logged only — it never short-circuits the controller until Phase 2.
                if baseline_distance is None and self.warm_baseline:
                    baseline_distance = max(abs(bal[c] - self.warm_baseline.get(c, bal[c])) for c in CHANNELS)
            else:
                op_streak = 0
                # PROTECTION / ABL: under a soak load (k>1), the fixed reference must not DIM below
                # its operating baseline — a falling reference is active limiting, not injected heat.
                if ref_op_baseline and self._last_ref_nits and \
                        self._last_ref_nits < ref_op_baseline * (1.0 - cfg.protection_drop_frac):
                    protection_hits += 1
                    if protection_hits >= cfg.protection_blocks and not protection_limited:
                        protection_limited = True
                        flags.append(
                            f"reference dimmed to {self._last_ref_nits:.0f} nits under soak load "
                            f"(operating baseline {ref_op_baseline:.0f}) — active limiting (ABL / power / "
                            "thermal throttle), not warm-in; the soak cannot inject usable heat here")
                else:
                    protection_hits = 0
            window = history[-cfg.window_blocks:]
            # The active channel is the biggest mover over the window (net/gross kept as the regime
            # DESCRIPTOR + dashboard tick — NOT the convergence gate any more).
            net_active = gross_active = 0.0
            ratio = None
            active = None
            if len(window) >= 2:
                best_gross = -1.0
                for ch in CHANNELS:
                    net, gross, r = net_over_gross([b[ch] for b in window])
                    if gross > best_gross:
                        best_gross, active = gross, ch
                        net_active, gross_active, ratio = net, gross, r
            # --- SOAK TRIGGER = directional warm-in BEYOND read noise (slope-vs-noise on the active
            #     channel's balance OR the neutral luminance), with DEBOUNCE so one noisy block can
            #     never soak, and a re-soak cooldown so a cooled-then-jittered panel can't saw-tooth.
            #     Direction-aware: only the warm-in direction (cold channel / luminance RISING) soaks;
            #     a cooling panel glides + settles (the slope gate rejects the cooling transient too). ---
            bal_series = [b[active] for b in window] if active else []
            _, _, z_bal, disp_bal = _slope_stats(bal_series, sigma_bal)
            _, _, z_y, disp_y = _slope_stats(ref_y_hist[-cfg.window_blocks:], sigma_y)
            warm_bal = (z_bal >= cfg.slope_z and disp_bal > 0.0)
            warm_y = (z_y >= cfg.slope_z and disp_y > 0.0)
            warming_signal = warm_bal or warm_y
            directional_streak = directional_streak + 1 if warming_signal else 0
            can_soak = (directional_streak >= cfg.soak_debounce
                        and (not did_soak or blocks_since_soak >= cfg.resoak_cooldown))
            if warming_signal and can_soak:
                k = cfg.k_start                                     # SOAK: inject heat
                in_band = 0
                did_soak = True
                blocks_since_soak = 0
                soft_budget = min(cfg.max_blocks, block + cfg.warm_budget_blocks)   # earn warm-in budget
                state = "soak"
            else:
                k = 1.0 + (k - 1.0) * cfg.k_decay                   # GLIDE toward operating load
                state = "glide"
            # --- CONVERGENCE = zero-slope-within-self-calibrated-read-noise on BOTH luminance AND the
            #     active channel's balance, judged over OPERATING-load reads only (the soak/glide probes,
            #     which carry the overshoot/cooling transient, never enter this window). The DECAYING
            #     landing latch requires a full operating window only while the overshoot is still in
            #     range (blocks_since_soak < window); a never-soaked / long-settled panel keeps the fast
            #     path. ABL drops luminance from the gate (balance-only) when the reference dimmed. ---
            recently_soaked = did_soak and blocks_since_soak < cfg.window_blocks
            min_op = cfg.window_blocks if recently_soaked else min(cfg.window_blocks, cfg.fast_op_blocks)
            converged_now = False
            if load_operating and op_streak >= min_op and len(op_balances) >= 3 and active:
                ob_series = [b[active] for b in op_balances[-cfg.window_blocks:]]
                _, _, ob_z, _ = _slope_stats(ob_series, sigma_bal)
                _, _, oy_z, _ = _slope_stats(op_ref_y[-cfg.window_blocks:], sigma_y)
                envelope_op = max(ob_series) - min(ob_series)             # peak-to-peak amplitude
                bal_flat = ob_z <= cfg.slope_z
                y_flat = (oy_z <= cfg.slope_z) or protection_limited      # ABL ⇒ balance-only gate
                bounded = envelope_op <= cfg.fluct_gross_mult * threshold # not a large oscillation
                converged_now = bal_flat and y_flat and bounded
            if converged_now:
                in_band += 1
                state = f"in-band x{in_band}"
            elif load_operating:
                in_band = 0
            if in_band >= cfg.converge_blocks:
                converged = True
                regime = "convergent"                                # the gate already required not-wandering
                state = f"CONVERGED:{regime}"
            self._block_record(block, load_k, net_active, gross_active, ratio, threshold, active, state,
                               op_streak=op_streak, did_soak=did_soak,
                               baseline_distance=baseline_distance, protection_limited=protection_limited)
            # Progress-driven digest check-in: emit each milestone category ONCE (first soak, first
            # landing/in-band, convergence, protection) so the LLM sees the soak's story without the
            # per-block firehose (which goes to the dashboard via ``emit``). Not wall-clock paced.
            cat = state.split()[0].split(":")[0]
            if protection_limited and "protection" not in emitted_cats:
                emitted_cats.add("protection")
                self._state_event("WARN", "protection_limited", block=block, k=round(load_k, 3),
                                  ref_nits=self._last_ref_nits, active_channel=active)
            if cat in ("soak", "in-band", "CONVERGED") and cat not in emitted_cats:
                emitted_cats.add(cat)
                self._state_event("INFO", "thermal_state", block=block, state=state, k=round(load_k, 3),
                                  net=round(net_active, 5), threshold=round(threshold, 6),
                                  active_channel=active, op_streak=op_streak)
            if converged:
                break
            # BUDGET (flag-don't-cap): out of budget without convergence — break and let the post-loop
            # hand the LLM a directional-vs-non-directional evidence packet. Never a silent cap/glide.
            if block >= soft_budget:
                budget_exhausted = True
                break

        minutes = round((self.clock() - t0) / 60.0, 4) if t0 is not None else 0.0
        # The REPORTED active (cold/temperamental) channel is the one that drifted most over the
        # WHOLE warm-in (first→last), not the last window's biggest mover: once the panel has landed
        # and settled, the final window is just noise, so the per-block ``active`` (right for the
        # live soak decision) no longer identifies the channel that actually warmed in.
        if first_bal and len(history) >= 2:
            best_net = -1.0
            for ch in CHANNELS:
                net_ch = abs(history[-1][ch] - first_bal[ch])
                if net_ch > best_net:
                    best_net, active = net_ch, ch
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
        # BUDGET CLASSIFICATION (when not converged): is the residual motion DIRECTIONAL (still
        # warming/cooling — a real ramp the budget cut short) or NON-DIRECTIONAL (settled-but-noisy —
        # a stationary wander)? This is the slope-vs-noise verdict on the FINAL window of the tracked
        # scalars, judged on operating-load reads when we have them. It drives the regime AND the kind
        # of evidence packet the LLM/user adjudicates: directional ⇒ warm longer / accept a moving
        # target; non-directional ⇒ proceeding warm is safe (freeze the warm balance).
        fin_bal = (op_balances or history)[-cfg.window_blocks:]
        fin_y = (op_ref_y or ref_y_hist)[-cfg.window_blocks:]
        _, _, fz_bal, _ = _slope_stats([b[active] for b in fin_bal], sigma_bal) if active else (0.0, 0.0, 0.0, 0.0)
        _, _, fz_y, _ = _slope_stats(fin_y, sigma_y)
        directional = (fz_bal >= cfg.slope_z) or (fz_y >= cfg.slope_z and not protection_limited)
        if compromised:
            regime = "compromised"          # the display/meter was bad — the regime is unknowable
        elif not converged:
            regime = "warming" if directional else "fluctuating"
        # The converged / settled OPERATING-load balance — a validated 'this is warm' fingerprint a
        # later calibration run compares a live read against (Phase-2 fast-path). Mean of the last
        # operating-load reads so a single noisy read can't define it. Frozen only when the panel is
        # genuinely settled (converged, or NON-directional at budget) — never while still ramping.
        warm_balance: Optional[dict[Channel, float]] = None
        if op_balances and not compromised and (converged or not directional):
            owin = op_balances[-cfg.window_blocks:]
            warm_balance = {ch: round(sum(b[ch] for b in owin) / len(owin), 6) for ch in CHANNELS}
        # Flag by regime, NOT just by (non-)convergence: a convergent panel is clean (no flag). A
        # COMPROMISED run already has its flag from the loop. The budget-exhausted packet carries the
        # directional verdict so the LLM knows whether to warm longer or proceed-warm.
        if not compromised and not converged:
            if directional:   # warming — a real ramp the budget cut short
                flags.append(
                    f"panel did not thermally converge within the {soft_budget}-block budget — still "
                    f"warming: DIRECTIONAL drift in progress (active {active} slope z={fz_bal:.1f}, "
                    f"luminance z={fz_y:.1f} vs cutoff {cfg.slope_z}); warm longer / inject more heat, "
                    "or accept calibrating against a moving target")
            else:             # fluctuating — settled but noisy (stationary wander, no steady point)
                flags.append(
                    f"panel settled but NOISY at the {soft_budget}-block budget with NO directional drift "
                    f"(active {active} slope z={fz_bal:.1f}, luminance z={fz_y:.1f}; residual band "
                    f"{round(envelope, 5)}) — thermally DYNAMIC: proceeding warm is safe, but it never "
                    "reaches a steady temperature, so maintain a consistent load (golden-ratio order) + "
                    "aggressive drift checks rather than warming to a target")
        # A first-order estimate of the panel's thermal time constant in content-read (≈ measurement-
        # patch) units: warm-in took ~(block − converge_blocks) blocks before it confirmed in-band,
        # and a first-order system settles in ~3τ, so τ ≈ warm-in reads / 3. Only meaningful when a
        # real warm-in was observed and the loop converged — an inert/flat panel has no τ to report
        # (None → the patch-ordering rotation keeps its default).
        tau_patches: Optional[int] = None
        if converged and active is not None and warmin > threshold:
            warmin_reads = max(0, block - cfg.converge_blocks) * max(1, cfg.load_reads_per_block)
            if warmin_reads > 0:
                tau_patches = max(1, round(warmin_reads / 3.0))
        # Terminal reason code (precedence: a bad display, then active limiting, then the regime).
        reason = ("compromised" if compromised else
                  "protection_limited" if protection_limited else
                  (f"converged:{regime}" if converged else regime))
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
            "tau_patches": tau_patches, "compromised": compromised,
            "protection_limited": protection_limited, "reason": reason,
            "warm_balance": warm_balance,
            "baseline_distance": (round(baseline_distance, 6) if baseline_distance is not None else None),
            # slope-vs-noise evidence the LLM adjudicates at the budget boundary (directional ⇒ warm
            # longer; non-directional ⇒ proceed-warm safe). z-ratios are |slope|/SE on the final window.
            "directional": (False if converged else directional),
            "balance_slope_z": round(fz_bal, 2), "luminance_slope_z": round(fz_y, 2),
            "slope_z_cutoff": cfg.slope_z, "luminance_sigma": round(sigma_y, 5),
            "budget_blocks": soft_budget, "budget_exhausted": budget_exhausted,
        }
        if self.event is not None:
            self.event("INFO" if (converged and not compromised and not protection_limited) else "WARN",
                       "thermal_regime", **digest)
        return ThermalResult(
            regime=regime, converged=converged, blocks=block, content_reads=content_reads,
            warmup_minutes=(minutes if regime == "convergent" else None),
            warmin_magnitude=round(warmin, 5), fluctuation_envelope=round(envelope, 5),
            drift_threshold=round(threshold, 6), active_channel=active, final_k=round(k, 3),
            tau_patches=tau_patches, warm_balance=warm_balance, protection_limited=protection_limited,
            baseline_distance=(round(baseline_distance, 6) if baseline_distance is not None else None),
            reason=reason, compromised=compromised,
            flags=flags, needs_adjudication=needs, question=question, digest=digest)

    def _state_event(self, level: str, event: str, **data: Any) -> None:
        """Emit a digest-tier milestone (first soak / landing / convergence / protection) to the
        structured-event sink, if one is wired. Rate-limited by the caller (once per category) so
        the LLM digest sees the soak's story without the per-block firehose."""
        if self.event is not None:
            self.event(level, event, **{k: v for k, v in data.items() if v is not None})

    def _block_record(self, block: int, k: float, net: float, gross: float,
                      ratio: Optional[float], threshold: float,
                      active: Optional[Channel], state: str, *,
                      op_streak: int = 0, did_soak: bool = False,
                      baseline_distance: Optional[float] = None,
                      protection_limited: bool = False) -> None:
        rec = {"phase": "thermal", "block": block, "k": round(k, 3),
               "ref_nits": (round(self._last_ref_nits, 3) if self._last_ref_nits else None),
               "net": round(net, 5), "gross": round(gross, 5),
               "net_over_gross": ratio, "threshold": round(threshold, 6),
               "active_channel": active, "state": state, "op_streak": op_streak,
               "did_soak": did_soak, "protection_limited": protection_limited}
        if baseline_distance is not None:
            rec["baseline_distance"] = round(baseline_distance, 6)
        if self.emit is not None:
            self.emit(rec)
