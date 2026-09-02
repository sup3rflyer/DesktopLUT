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
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ContextManager, Optional, Protocol, Sequence

from .dip import DisplayInstrumentProfile
from .drift import CHANNELS, Channel, coldest_channel_from_xyz, evaluate_drift, normalized_channels
from .engine.patches import Patch, Transfer, to_signal
from .events import EventWriter, RunLog
from .liveness import Liveness
from .metrics import SRGB_TO_XYZ_D65, delta_e2000, xyz_to_lab

__all__ = [
    "MeasurePatch",
    "Reading",
    "MeasureFn",
    "MeasureLoopConfig",
    "AcceptedRead",
    "MeasureLoopResult",
    "IncrementalMeasureSession",
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
    ``seq`` is the position in the main pass (``-1`` for warm-up / reference).
    ``settle_bump_s`` is EXTRA presenter dwell for this one presentation, on top of
    the presenter's own settle — set by the loop's luminance-jump settle bump when
    the presented luminance drops sharply (FALD zone decay/glow after a bright
    patch); presenters honor it, synthetic measure fns are free to ignore it."""

    label: str
    rgb: tuple[int, int, int]
    signal: tuple[float, float, float]
    role: str = "measurement"
    bit_depth: int = 10
    seq: int = -1
    settle_bump_s: float = 0.0

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
    # Dark-panel floor: the warm-up patch is a MID-grey (warmup_signal 0.5), so on any live panel
    # it reads tens-to-hundreds of cd/m². A reference reading below this floor means the panel is
    # emitting ~no light (asleep / off / wrong input) — settle agreement on black (0≈0) is NOT
    # "warm". Catch it loudly in a couple of reads instead of "settling" on darkness and then
    # hanging the meter per patch. This is a minimum cd/m² floor; the live guard also applies a
    # small fraction of the transfer's expected warm-up luminance. Set to 0 to disable the guard.
    dark_floor_nits: float = 1.0
    dark_floor_fraction: float = 0.05  # fraction of expected warm-up Y; catches HDR mid-code ≈1 nit
    dark_required: int = 2              # consecutive sub-floor reference reads ⇒ declare the panel dark

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
    adaptive_neutral_min: int = 4       # tightest interval after repeated drift exits the envelope
    drift_density_window: int = 5       # recent neutral checkpoints considered for dense-drift gating
    drift_density_limit: int = 4        # repeats in that window => LLM review (continued, not aborted)

    # Per-patch read policy (single-read default + DIP-driven escalation) -----
    read_tolerance_de: float = 0.2      # target standard error of the mean (CIEDE2000) per patch
    min_reads: int = 1                  # single adaptive-integration read by default (pro-standard)
    abnormal_reads: int = 16            # reads past ~2× the DIP target ⇒ FLAG for adjudication (never a silent cap)
    outlier_factor: float = 3.0         # a read >factor×σ from the patch median is a glitch ⇒ rejected, not averaged in
    outlier_floor_de: float = 0.5       # never reject within this ΔE of the median (a σ-independent floor)

    # Near-neutral read FLOOR (chroma-critical region) ----------------------
    # The matrix (white/chromaticity) + per-channel WB + non-additivity terms are derived from the
    # grey ramp + the off-axis near-neutral TUBE, where a tiny chromaticity error in a single read
    # biases the whole fit. The DIP noise model sizes reads for *luminance* SNR — which already
    # covers the dark/noisy end — but it does NOT know that a BRIGHT near-neutral patch (low σ, so
    # the DIP would take one read) is chroma-critical. ``neutral_min_reads`` is a per-patch read
    # FLOOR applied ONLY to near-neutral patches so that region is always averaged; the DIP still
    # escalates ABOVE the floor where σ demands it (``target_n = max(floor, dip_n)``). It is also the
    # fixed-N fallback when there is no DIP. Default 1 ⇒ off (behaviour unchanged). NOT a cap — the
    # abnormal-read FLAG is unaffected. ``neutral_chroma_span`` is the discriminator: a patch is
    # near-neutral when ``(max-min) <= span * max`` of its signal (gray ⇒ 0; the 0.06/0.15 tube ⇒
    # ≤0.26; any pure-channel/secondary ramp has a zero channel ⇒ 1.0, excluded). Widen it if the
    # tube uses larger chroma offsets.
    neutral_min_reads: int = 1
    neutral_chroma_span: float = 0.35
    # Luminance GATE on the floor: only raise reads on near-neutral patches whose EXPECTED
    # luminance is at or above this (0 ⇒ no gate). The measured noise model shows σ is SMALLEST
    # at the dim end (it grows with luminance), so dark patches benefit from averaging the LEAST —
    # while being the SLOWEST to read (long meter integration at low light) and the most thermally
    # risky (extended dwell at low backlight load cools the panel → drift churn). Gating the floor
    # to brighter near-neutral patches puts the extra reads where σ is largest and reads are fast,
    # and leaves the slow dim patches at a single read.
    neutral_floor_min_nits: float = 0.0
    # Dark near-neutral read floor (CHROMA trust). The note above spares dim patches because their
    # *luminance* σ is smallest — but near black the *chromaticity* is the unreliable axis (meter
    # chroma noise + backlight contamination + coarse PQ codes), and we can only know HOW unreliable
    # by reading the level several times. Take ≥ ``dark_min_reads`` reads on near-neutral patches at
    # or below ``dark_floor_max_nits`` so the read-to-read chromaticity spread can be estimated; that
    # spread drives the dark-level trust (mhc_cube.dark_trust_weights → how much to smooth to
    # identity). Default 1 ⇒ off; complementary to neutral_min_reads (the BRIGHT region).
    dark_min_reads: int = 1
    dark_floor_max_nits: float = 1.0
    # Early stop for the DARK read floor: once this many reads AGREE (a clean cluster — no outliers,
    # SE within read_tolerance_de — and the DIP's own SNR target is met) the floor is satisfied and
    # the remaining ``dark_min_reads`` reads are skipped. The floor exists to ESTIMATE the read-to-read
    # chromaticity spread; two agreeing reads already give one (σ/√2 — slightly LESS trust than
    # σ/√3, i.e. the conservative direction), while a third identical ~8 s low-light read adds no
    # information (LG C6 2026-09-02: 44 dark patches × 2 redundant reads ≈ 12 min of a 50-min raw
    # pass; the owner had already flagged the "redundant 3× stable reads"). Disagreeing reads keep
    # reading to the floor and beyond exactly as before. 0 ⇒ always take the full floor.
    dark_agree_reads: int = 2

    # Luminance-jump settle bump (gs-wb outside-in ordering, D4) ------------
    # The outside-in alternating orders swing dark↔bright on nearly every transition. A local-
    # dimming panel needs a moment after a sharp luminance DROP before a dark read is clean
    # (FALD zone decay / residual glow from the bright patch contaminates the first dark read;
    # the rise direction responds fast). When the newly-presented patch's EXPECTED luminance
    # falls by more than ``jump_settle_ratio`` from the previously-presented one (and the
    # previous patch was at least ``jump_settle_floor_nits`` — a drop between two already-dark
    # patches has no glow worth waiting out), the presentation carries ``jump_settle_s`` of
    # extra dwell via ``MeasurePatch.settle_bump_s`` (presenters honor it; synthetic fns
    # ignore it). Repeat reads of an unchanged patch never pay it. 0 disables.
    jump_settle_s: float = 1.0
    jump_settle_ratio: float = 8.0
    jump_settle_floor_nits: float = 10.0

    plausible_luminance_floor_nits: float = 1.0
    plausible_luminance_reference_floor_fraction: float = 0.04
    plausible_luminance_low_expected_nits: float = 10.0
    plausible_luminance_low_signal: float = 0.20
    plausible_luminance_high_reference_fraction: float = 0.25
    plausible_luminance_high_expected_factor: float = 4.0

    # Selective re-measure budget -------------------------------------------
    remeasure_cap: int = 256            # advisory threshold for appended re-measures; crossing it flags

    # Cross-patch read-integrity guard --------------------------------------
    # The per-read stall clock resets on ANY ok read, so two failure classes are invisible to it: a
    # FROZEN presenter (a stuck frame reads fine + repeatably — the ~111-min dogegen freeze) and a panel
    # that slept MID-RUN (valid ~0-nit reads). Inspect the last few MEASUREMENT reads for both signatures
    # and surface either as a measurement-path anomaly the LLM consumes from the running spine (never a
    # silent abort). 0 disables.
    integrity_window: int = 4                      # consecutive measurement reads inspected for a stuck frame
    integrity_frozen_expected_ratio: float = 2.0   # commanded peak-nits span that SHOULD move the read…
    integrity_frozen_y_tol: float = 0.05           # …yet every measured Y within ±5% of their mean…
    integrity_frozen_xy_tol: float = 0.003         # …and chromaticity within this xy radius ⇒ stuck frame
    integrity_dark_run: int = 4                     # consecutive LIT patches reading sub-floor ⇒ panel dark

    # Fast present-stall (stuck-frame) detector — RUN-STOPPER (2026-09-02 C6 run, item #2) ----
    # The TV's auto-power-off froze the presented frame mid-verify; reads kept completing (the
    # stall clock never trips) and the frozen_presenter window above needs a commanded-LUMINANCE
    # span it may never see. The specific stuck-frame signal is stronger: consecutive reads that
    # are IDENTICAL to within meter noise while the commanded COLOURS genuinely differ. Nothing
    # on a working present path produces that outside near-black — a luminance clamp (peak cap /
    # ABL) can equalize same-direction commands, but only a stuck frame equalizes XYZ across
    # different commanded chromaticity directions. Fires ONCE, halts the pass (further reads are
    # garbage), and surfaces as a run-stopper anomaly the escalation seam adjudicates.
    #   stall_reads             — flat-streak length before judging (3: two could conceivably be a
    #                             metameric/clamp coincidence; three across distinct colours cannot)
    #   stall_distinct_commands — meaningfully-different commanded directions the streak must span
    #   stall_direction_delta   — per-channel delta (max-normalized signal) that makes two
    #                             commands "different colours" (0.25 keeps the whole grey ramp +
    #                             near-neutral tube as ONE direction — clamp-safe)
    #   tolerance               — reads count as identical within max(stall_floor_de,
    #                             stall_sigma_factor × DIP per-read σ at that luminance): the
    #                             meter-noise band, scaled from the MEASURED noise model
    #   stall_min_nits          — near-black exclusion: below this, distinct colours legitimately
    #                             read indistinguishably (sub-noise). 0/negative stall_reads disables.
    stall_reads: int = 3
    stall_distinct_commands: int = 2
    stall_direction_delta: float = 0.25
    stall_sigma_factor: float = 4.0
    stall_floor_de: float = 0.5
    stall_min_nits: float = 5.0

    # Channel-aware plausibility envelope (2026-09-02 C6 run, item #3) ----------------------
    # With measured per-channel peaks (mhc_params.channel_peak_xyz via the orchestrator), the
    # envelope's expected luminance is an ADDITIVE combination of the commanded drives against
    # per-channel peak Y — a full-drive blue on a WOLED whose blue peaks at 18.6 nits is
    # expected near 18.6, never near the 604-nit white peak. The lower plausibility bound then
    # tightens from the container fraction (2%) to this fraction of the channel-aware expected
    # (wide band: a plausibility envelope, not a model). Without channel peaks the envelope
    # falls back to the container behaviour above.
    plausible_channel_low_fraction: float = 0.25

    # Read-anomaly repeatability (escalation-recommendation evidence, item #4) --------------
    # Envelope-anomalous reads that REPEAT (same commanded RGB re-read across the pass /
    # bookends within this relative luminance spread) are stable-but-implausible — real panel
    # or correction behaviour, not a transient fault. Rides the digest as evidence; the
    # orchestrator only uses it to pick the seam's RECOMMENDATION (accept vs retry) — the LLM
    # still judges.
    anomaly_stable_spread: float = 0.05


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
    # Measured repeatability (≥2 reads): the standard error in dE and the read-to-read chromaticity
    # spread in xy. These quantify sensor-noise + short-term display fluctuation, and feed the
    # dark-level trust (mhc_cube.dark_trust_weights) — how much to smooth a dark correction to identity.
    se_de: Optional[float] = None
    chroma_sigma: Optional[float] = None
    # Reads behind ``chroma_sigma`` THIS round (NOT the accumulated ``reads_taken``, which sums across
    # appended re-measures). The dark-trust SE-of-mean is ``chroma_sigma / √noise_reads`` — σ and n must
    # describe the same set of reads, else an appended re-measure divides a single-round σ by an inflated
    # n, understating noise and OVER-trusting the dark correction the gate exists to suppress.
    noise_reads: Optional[int] = None


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


def noise_sidecar_path(ti3_path: Path) -> Path:
    """The per-level measured-noise sidecar beside a ``.ti3`` (``<ti3>.noise.json``)."""
    return Path(str(ti3_path) + ".noise.json")


def read_noise_sidecar(ti3_path: Path) -> list[tuple[float, Optional[float]]]:
    """Parse ``<ti3>.noise.json`` → ``[(gray level, trust-noise), ...]`` sorted by level, where
    trust-noise is the **standard error of the mean** chromaticity (per-read σ / √reads) — it shrinks
    as reads accumulate, so more readings → tighter → more trust. An ``unstable`` level (the loop
    couldn't pin it after many reads = genuine fluctuation, not averageable) returns ``+inf`` ⇒ never
    trust it. ``<2`` reads ⇒ ``None`` (no spread; trust the adaptive integration). Empty list when no
    sidecar / unreadable."""
    p = noise_sidecar_path(ti3_path)
    if not p.exists():
        return []
    try:
        by = (json.loads(p.read_text(encoding="utf-8")) or {}).get("by_level") or {}
    except (OSError, ValueError):
        return []
    out: list[tuple[float, Optional[float]]] = []
    for k, v in by.items():
        try:
            lvl = float(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        if v.get("unstable"):
            noise: Optional[float] = math.inf
        else:
            sg = v.get("chroma_sigma")
            n = v.get("reads") or 0          # reads behind THIS σ (per-round), not lifetime reads_taken
            noise = (sg / math.sqrt(n)) if (sg is not None and n >= 2) else None
        out.append((lvl, noise))
    out.sort(key=lambda e: e[0])
    return out


def match_level_noise(entries: Sequence[tuple[float, Optional[float]]], level: float,
                      *, tol: float = 1e-5) -> Optional[float]:
    """Nearest sidecar level's trust-noise to ``level`` within ``tol`` — robust to the ``.ti3``
    ×100/÷100 percent roundtrip (which perturbs the value ~1e-7), while ``tol`` stays far below the
    minimum gray-level spacing (≥2.4e-4 even at 12-bit), so it can never match the wrong level.
    ``None`` when nothing is within ``tol`` (or the matched level has no usable noise)."""
    best: Optional[float] = None
    best_d = tol
    for lvl, noise in entries:
        d = abs(lvl - level)
        if d <= best_d:
            best, best_d = noise, d
    return best


def _write_noise_sidecar(ti3_path: Path, accepted: Sequence[AcceptedRead]) -> None:
    """Persist per-NEUTRAL-LEVEL measured repeatability (chroma σ + SE + reads) beside the ``.ti3``,
    keyed by gray level (signal). Consumed by ``build_mhc`` → ``mhc_cube.dark_trust_weights`` to
    decide how much to smooth each dark level's correction to identity. No file when nothing has
    ≥2 reads (single-read run ⇒ no spread ⇒ the trust gate simply isn't engaged)."""
    by_level: dict[str, dict[str, Any]] = {}
    for r in accepted:
        if not r.usable or r.chroma_sigma is None:
            continue
        s = r.patch.signal
        if not (abs(s[0] - s[1]) < 1e-6 and abs(s[1] - s[2]) < 1e-6):
            continue                       # neutral gray levels only (the cube's neutral axis)
        by_level[f"{s[0]:.6f}"] = {
            "chroma_sigma": round(r.chroma_sigma, 6),
            "se_de": (round(r.se_de, 4) if r.se_de is not None else None),
            # The read count that PRODUCED chroma_sigma (this round), so the consumer's σ/√n is the SE
            # of the same reads. reads_taken accumulates across appended re-measures — wrong divisor here.
            "reads": (r.noise_reads if r.noise_reads is not None else r.reads_taken),
            "unstable": bool(r.unstable),
        }
    if not by_level:
        return
    noise_sidecar_path(ti3_path).write_text(
        json.dumps({"schema": 1, "by_level": by_level}, indent=2), encoding="utf-8")


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
        checkin_interval_s: float = 0.0,
        reference_guard: Optional[Callable[[], ContextManager[None]]] = None,
        channel_peak_y: Optional[tuple[float, float, float]] = None,
        white_peak_y: Optional[float] = None,
        correction_max_nits: Optional[float] = None,
        correction_channel_scale: Optional[tuple[float, float, float]] = None,
    ) -> None:
        self.transfer = transfer
        self.cfg = config
        self.ndjson = ndjson
        self.events = events
        # Plausibility-envelope context (all optional; None ⇒ container fallback):
        #   channel_peak_y          — measured full-drive per-channel Y (R, G, B), cd/m²
        #   white_peak_y            — measured REAL white peak (WRGB non-additive headroom on
        #                             near-neutral drives; the W subpixel exceeds the RGB sum)
        #   correction_max_nits     — an installed correction's luminance cap (e.g. the MHC
        #                             Peak-Chroma cap): commanded targets above it legitimately
        #                             read at/near the cap
        #   correction_channel_scale — per-channel linear transmittance of an installed
        #                             correction, when known (attenuation shrinks the envelope)
        self.channel_peak_y = channel_peak_y
        self.white_peak_y = white_peak_y
        self.correction_max_nits = correction_max_nits
        self.correction_channel_scale = correction_channel_scale
        # Optional caller-supplied context manager held around every read that ESTABLISHES
        # or COMPARES AGAINST the warm/drift reference (warm-up settle, re-settle after a
        # drift episode, the interleaved neutral checkpoint). A caller that mutates the
        # display between reads (the grayscale touch-up live-editing the correction table)
        # uses it to present those reads through a FIXED display state (identity table), so
        # its own edits can never masquerade as panel drift (2026-08-14 HDR run: nudging
        # the mid-ramp grey moved the drift reference and tripped a false excursion).
        self.reference_guard = reference_guard
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
        # §12 wall-clock backstop for the in-measure check-in (emit-only) — see _maybe_checkin.
        self._checkin_interval_s = max(0.0, float(checkin_interval_s))
        self._last_checkin_monotonic = time.monotonic()

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
        self.drift_checkpoints: list[dict[str, Any]] = []
        self.drift_density_exceeded = False
        self.drift_density_event_emitted = False
        self.drift_regime = "unknown"
        self.neutral_interval_current = max(0, config.neutral_interval)
        self.neutral_interval_adjustments = 0
        self.remeasure_budget = config.remeasure_cap
        self.remeasure_budget_exceeded = False
        self.warm = False
        self.warmup_reads = 0
        self.panel_dark = False                       # warm-up reference read ~no light (asleep/off)
        self.dark_reference_nits: Optional[float] = None
        self.measurement_path_compromised = False
        self.read_anomalies: list[dict[str, Any]] = []
        self._checkin_quartiles: set[float] = set()   # progress-driven digest check-ins emitted
        # §12 measure check-in window high-water marks. Each check-in reports the DELTA since the
        # previous one (reads / anomalies / drift that are NEW), not the cumulative totals — a
        # check-in is "what happened since I last looked", never a restatement of old evidence.
        self._checkin_reads_at_last = 0
        self._checkin_anomalies_at_last = 0
        self._checkin_drift_at_last = 0
        self._checkin_warm_at_last = False
        # Cross-patch read-integrity state (frozen-frame / mid-run-dark detection).
        self._integrity_recent: list[tuple[float, tuple[float, float, float]]] = []
        self._integrity_dark_streak = 0
        self._integrity_flagged: set[str] = set()      # each signature surfaced at most once
        # Present-stall (stuck-frame) state: the running flat streak of bright reads that agree
        # within meter noise — (commanded direction, xyz, label) — and the run-stopper latch.
        self._stall_streak: list[tuple[tuple[float, float, float], tuple[float, float, float], str]] = []
        self.present_stall = False
        # Luminance-jump settle bump state: the last PRESENTED patch (label, expected nits)
        # across every presentation funnel (_read + the drift checkpoint), and how many
        # presentations carried the bump.
        self._last_presented: Optional[tuple[str, float]] = None
        self.jump_settles = 0

    def _reference_read_guard(self) -> ContextManager[None]:
        """The caller-supplied fixed-display-state guard for reference reads (identity
        editor table during the grayscale touch-up), or a no-op when none was given."""
        return self.reference_guard() if self.reference_guard is not None else nullcontext()

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

    def _maybe_checkin(self, index: int) -> None:
        """A coarse digest check-in DURING a long measure stage, so the LLM's digest
        projection shows forward motion (per-patch ``patch_read`` + ``heartbeat`` are
        stream-tier and dropped from the digest — without this a 30-min measure looks like
        ``stage_start`` then silence until ``stage_done``).

        PRIMARY trigger = MEASURED progress at quartiles (the owner's no-magic-cadence rule).
        BACKSTOP = a coarse wall-clock floor (§12 ``--checkin-interval``), so a slow stage —
        or a measure-only flow (``mhc-only``: no optimizer ticking §12 at the orchestrator) —
        never goes dark over a long run ("never 0 check-ins in a 3-hour run").
        EMIT-ONLY: this never gates or pauses (the §12 stage-boundary seam owns the
        mode-driven continue? decision); here we only surface status. Every emit (quartile or
        backstop) resets the floor so a quartile crossing doesn't immediately re-trigger it."""
        if self.runlog is None:
            return
        total = len(self.patches)
        if total <= 0:
            return
        frac = index / total
        for q in (0.25, 0.5, 0.75):
            if q not in self._checkin_quartiles and frac >= q:
                self._checkin_quartiles.add(q)
                self._emit_measure_checkin(index, total, frac)
        if self._checkin_interval_s > 0 \
                and time.monotonic() - self._last_checkin_monotonic >= self._checkin_interval_s:
            self._emit_measure_checkin(index, total, frac)

    def _maybe_checkin_backstop(self) -> None:
        """The wall-clock arm ALONE, callable from ANY read path — warm-up, preheat/rewarm
        soak blocks, re-measures, incremental sessions — not just the main pass (fable
        Phase 8, the owner's NO-DARK-WINDOW rule: an adjudicated run must never go
        ``checkin_interval_s`` without an evidence packet while the meter is working).
        ``_maybe_checkin`` (main pass) keeps the progress-quartile arm on top of this.
        Progress is reported as accepted-so-far over the planned set — approximate during
        warm-up/soak (0 of N), exact once the main pass runs."""
        if self.runlog is None or self._checkin_interval_s <= 0:
            return
        if time.monotonic() - self._last_checkin_monotonic < self._checkin_interval_s:
            return
        total = max(1, len(self.patches))
        done = len(self.accepted)
        self._emit_measure_checkin(done, total, done / total)

    def _emit_measure_checkin(self, index: int, total: int, frac: float) -> None:
        now = time.monotonic()
        elapsed_since = round(now - self._last_checkin_monotonic, 1)
        self._last_checkin_monotonic = now
        # EMIT-ONLY evidence packet for the LLM (never gates): forward motion + what's NEW since
        # the last check-in. ``since_last`` is the spine delta — reads/anomalies/drift accrued in
        # THIS window only, so a clean stretch reads as zeros instead of re-stating the running
        # totals every time (the LLM diffing cumulative counters by hand is the bug this avoids).
        # ``drift_episodes`` = neutral re-reads that left the envelope; ``anomalies`` =
        # non-stopper read-plausibility flags. The new-anomaly details ride along (not just the
        # latest-ever one) so the LLM can judge "two new flags this window" vs "flagged once long
        # ago, quiet since". Totals stay as ``*_total`` for the one-glance running tally.
        new_anomalies = self.read_anomalies[self._checkin_anomalies_at_last:]
        # Keep the packet readable if a window flagged a storm; the full list is on disk / in state.
        new_anomalies_inline = new_anomalies[-25:] if len(new_anomalies) > 25 else new_anomalies
        since_last: dict[str, Any] = {
            "reads": self.seq_counter - self._checkin_reads_at_last,
            "anomalies": len(new_anomalies),
            "drift_episodes": self.drift_episodes - self._checkin_drift_at_last,
        }
        if self.warm and not self._checkin_warm_at_last:
            since_last["became_warm"] = True   # the warm-up→warm transition happened this window
        self.runlog.check_in(
            "measure", progress=round(frac, 2),
            patches_done=index, patches_total=total,
            elapsed_since_checkin_s=elapsed_since,
            since_last=since_last,
            new_anomalies=(new_anomalies_inline or None),   # the actual NEW flags, for the LLM to judge
            warm=self.warm,                          # current state (position, not repeated evidence)
            white_nits=(round(self.white_xyz[1], 2) if self.white_xyz else None),
            reads_total=self.seq_counter,
            anomalies_total=len(self.read_anomalies),
            drift_episodes_total=self.drift_episodes)
        # Advance the window high-water marks AFTER emitting, so the next check-in's delta is clean.
        self._checkin_reads_at_last = self.seq_counter
        self._checkin_anomalies_at_last = len(self.read_anomalies)
        self._checkin_drift_at_last = self.drift_episodes
        self._checkin_warm_at_last = self.warm

    def _jump_settle_bump(self, patch: MeasurePatch) -> float:
        """Extra presenter dwell for THIS presentation of ``patch`` (0.0 when none is
        warranted), and update the last-presented tracking. A bump fires only when the
        presented patch CHANGES and its expected luminance drops sharply from the previous
        presentation (``jump_settle_ratio``), from a bright-enough level for FALD zone
        decay/glow to matter (``jump_settle_floor_nits``) — the dark↔bright swings the
        outside-in alternating order introduces. Repeat reads of an unchanged patch and
        rising transitions never pay it."""
        cfg = self.cfg
        expected = self._expected_patch_nits(patch)
        prev = self._last_presented
        self._last_presented = (patch.label, expected)
        if cfg.jump_settle_s <= 0 or prev is None:
            return 0.0
        prev_label, prev_nits = prev
        if prev_label == patch.label:
            return 0.0
        if prev_nits < cfg.jump_settle_floor_nits:
            return 0.0
        if prev_nits < cfg.jump_settle_ratio * max(expected, 1e-6):
            return 0.0
        self.jump_settles += 1
        return cfg.jump_settle_s

    def _present_and_measure(self, patch: MeasurePatch) -> tuple[Reading, float]:
        """The single presentation funnel: apply the luminance-jump settle bump (as
        ``settle_bump_s`` on the presented patch — honored by the real presenters,
        ignored by synthetic measure fns) and read. Returns ``(reading, bump_s)``."""
        bump = self._jump_settle_bump(patch)
        presented = replace(patch, settle_bump_s=bump) if bump > 0.0 else patch
        return self.measure(presented), bump

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
        reading, jump_settle_s = self._present_and_measure(patch)
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
        if jump_settle_s > 0.0:
            record["jump_settle_s"] = jump_settle_s
        if reading.error:
            record["error"] = reading.error
        self.ndjson.emit(record)
        self._mirror_patch_read(record)          # live firehose + per-read liveness on the spine
        if phase != "soak" and read_index == 0 and patch.role == "measurement":
            self._emit_progress()                # one coarse tick per new patch (counters / ETA)
        # NO-DARK-WINDOW backstop on the loop's single read funnel: warm-up, re-measures,
        # and incremental sessions tick the §12 clock too, not just the main pass.
        self._maybe_checkin_backstop()
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

    def _chroma_sigma(self, reads: Sequence[tuple[float, float, float]],
                      *, sigma: Optional[float] = None) -> Optional[float]:
        """RMS read-to-read chromaticity spread in ``xy`` — the dark-level noise estimate (sensor
        noise + short-term display fluctuation both show up here). Computed over the SAME
        outlier-rejected INLIERS as the accepted mean (a gross glitch is dropped, not allowed to
        inflate σ ~200× and collapse the level's trust). ``None`` for <2 inlier reads."""
        n = len(reads)
        if n < 2:
            return None
        if n < 3:
            inliers = list(reads)
        else:
            med = _median_xyz(reads)
            white = self.white_xyz or med
            spread = sigma
            if spread is None:
                devs = sorted(_agreement_de(r, med, white) for r in reads)
                spread = devs[len(devs) // 2]
            thr = max(self.cfg.outlier_floor_de, self.cfg.outlier_factor * (spread or 0.0))
            inliers = [r for r in reads if _agreement_de(r, med, white) <= thr] or list(reads)
        xy = [(X / t, Y / t) for (X, Y, Z) in inliers if (t := X + Y + Z) > 0.0]
        if len(xy) < 2:
            return None
        mx = sum(p[0] for p in xy) / len(xy)
        my = sum(p[1] for p in xy) / len(xy)
        return math.sqrt(sum((p[0] - mx) ** 2 + (p[1] - my) ** 2 for p in xy) / len(xy))

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

    def _dark_floor_for(self, patch: MeasurePatch) -> float:
        if self.cfg.dark_floor_nits <= 0:
            return 0.0
        expected = 0.0
        try:
            expected = self.transfer.cv_to_nits(max(patch.rgb))
        except Exception:
            expected = 0.0
        dynamic = max(0.0, expected * max(0.0, self.cfg.dark_floor_fraction))
        return max(self.cfg.dark_floor_nits, dynamic)

    def _expected_patch_nits(self, patch: MeasurePatch) -> float:
        try:
            return max(self.transfer.cv_to_nits(c) for c in patch.rgb)
        except Exception:
            return 0.0

    def _plausible_expected_nits(self, patch: MeasurePatch) -> tuple[float, str]:
        """The plausibility envelope's expected luminance for ``patch`` and where it came from
        (``"channel_peaks"`` | ``"container"``).

        With measured per-channel peaks (item #3, 2026-09-02 C6 run): an ADDITIVE combination of
        the commanded per-channel drives against per-channel peak Y — the commanded absolute track
        (``cv_to_nits``) apportioned to each channel by its share of the additive white, capped at
        that channel's peak. A full-drive blue on a WOLED whose blue peaks at 18.6 nits expects
        ~18.6, never the 604-nit white peak. On near-neutral drives the W subpixel exceeds the
        additive RGB sum, so ``white_peak_y`` headroom is allowed in proportion to how fully all
        three channels are driven (WRGB non-additivity). An installed correction bounds the
        commanded track (``correction_max_nits`` — targets above the cap read at the cap) and may
        attenuate channels (``correction_channel_scale``). This is an ENVELOPE, not a model — the
        caller wraps it in wide tolerance bands. Without channel peaks: the container expectation
        (max per-channel ``cv_to_nits``), i.e. the previous behaviour."""
        container = self._expected_patch_nits(patch)
        peaks = self.channel_peak_y
        if not peaks or len(peaks) != 3 or min(peaks) <= 0.0:
            return container, "container"
        total = sum(peaks)
        scale = self.correction_channel_scale or (1.0, 1.0, 1.0)
        cap = self.correction_max_nits
        contribs: list[float] = []
        for c in range(3):
            try:
                track = self.transfer.cv_to_nits(patch.rgb[c])
            except Exception:
                track = 0.0
            if cap is not None and cap > 0.0:
                track = min(track, cap)
            ceiling = peaks[c] * max(0.0, scale[c])
            contribs.append(min(track * (peaks[c] / total) * max(0.0, scale[c]), ceiling))
        expected = sum(contribs)
        # WRGB headroom: only as neutral as the LEAST-driven channel allows (a pure primary gets
        # none; a fully-driven white gets the whole measured-white surplus over the additive sum).
        if self.white_peak_y is not None and self.white_peak_y > total:
            neutralness = 1.0
            for c in range(3):
                ceiling = peaks[c] * max(0.0, scale[c])
                neutralness = min(neutralness, (contribs[c] / ceiling) if ceiling > 0.0 else 0.0)
            expected += max(0.0, neutralness) * (self.white_peak_y - total)
        if cap is not None and cap > 0.0:
            expected = min(expected, cap)     # a capped correction never emits above its cap
        return min(expected, container), "channel_peaks"

    def _read_plausibility_anomaly(
        self,
        patch: MeasurePatch,
        xyz: tuple[float, float, float],
        *,
        phase: str,
        read_index: int,
    ) -> Optional[dict[str, Any]]:
        if patch.role != "measurement":
            return None
        measured = float(xyz[1])
        expected, envelope = self._plausible_expected_nits(patch)
        reference = float(self.reference_xyz[1]) if self.reference_xyz else 0.0
        signal_peak = max(patch.signal) if patch.signal else 0.0

        def detail(reason: str, threshold: float) -> dict[str, Any]:
            return {
                "reason": reason,
                "measure_phase": phase,
                "read_index": read_index,
                "label": patch.label,
                "rgb": list(patch.rgb),
                "signal": [round(s, 6) for s in patch.signal],
                "expected_nits": round(expected, 4),
                "envelope": envelope,
                "measured_nits": round(measured, 4),
                "reference_nits": round(reference, 4),
                "threshold_nits": round(threshold, 4),
            }

        low_expected_cut = max(
            self.cfg.plausible_luminance_low_expected_nits,
            reference * self.cfg.plausible_luminance_reference_floor_fraction,
        )
        high_for_low_cut = max(
            self.cfg.plausible_luminance_low_expected_nits,
            reference * self.cfg.plausible_luminance_high_reference_fraction,
            expected * self.cfg.plausible_luminance_high_expected_factor
            + self.cfg.plausible_luminance_floor_nits,
        )
        low_drive = (
            expected <= low_expected_cut
            or signal_peak <= self.cfg.plausible_luminance_low_signal
        )
        if low_drive and measured >= high_for_low_cut:
            return detail("low_drive_high_luminance", high_for_low_cut)

        lit_cut = max(
            self.cfg.plausible_luminance_low_expected_nits * 2.0,
            reference * 0.5,
        )
        # Container expectations wildly overestimate a gamut-limited channel (a full-drive blue
        # "expects" the panel white), so the container fraction must be tiny (2%). A channel-aware
        # expectation is honest, so the bound tightens to plausible_channel_low_fraction of it —
        # a genuinely dark primary is caught while a legitimately dim one (blue on a blue-weak
        # WOLED) passes: its expectation IS dim.
        low_fraction = (self.cfg.plausible_channel_low_fraction
                        if envelope == "channel_peaks" else 0.02)
        low_for_lit_cut = max(
            self.cfg.plausible_luminance_floor_nits,
            reference * self.cfg.plausible_luminance_reference_floor_fraction,
            expected * low_fraction,
        )
        if expected >= lit_cut and measured <= low_for_lit_cut:
            return detail("lit_drive_low_luminance", low_for_lit_cut)

        return None

    def _flag_read_plausibility_anomaly(self, anomaly: dict[str, Any]) -> None:
        self.measurement_path_compromised = True
        self.read_anomalies.append(anomaly)
        if anomaly.get("reason") == "present_stall":
            message = (
                "presented frame appears STUCK: consecutive reads across different commanded "
                "colours returned identical XYZ (TV sleep / screensaver / frozen presenter); "
                "measurement halted — run-stopper, adjudicate before trusting this run"
            )
        else:
            message = (
                "meter reading is outside the plausible luminance envelope for the presented patch; "
                "measurement path requires adjudication"
            )
        self._emit_event(
            "WARN",
            "read_plausibility_anomaly",
            **anomaly,
            message=message,
        )
        if self.runlog is not None:
            self.runlog.anomaly(
                "measure",
                kind="read_plausibility_anomaly",
                **anomaly,
                message=message,
            )

    def _check_present_stall(self, patch: MeasurePatch, xyz: tuple[float, float, float]) -> None:
        """Fast stuck-frame detector — RUN-STOPPER (2026-09-02 C6 auto-power-off, item #2).

        A frozen presented frame keeps producing *valid, repeatable* reads, so the per-read stall
        clock and the luminance-span frozen_presenter window can both stay quiet (the real event:
        a stuck ~402-nit white read "successfully" for dim magenta patches, then poisoned the
        drift checkpoint). The unambiguous signature is a streak of reads IDENTICAL to within
        meter noise while the commanded colours genuinely differ in chromaticity DIRECTION —
        a luminance clamp (Peak-Chroma cap / ABL) can equalize same-direction commands, but only
        a stuck frame equalizes XYZ across different commanded directions. Near-black is excluded
        (distinct colours legitimately read indistinguishably there). Fires at most once, latches
        ``present_stall`` (the pass halts — further reads are garbage) and flags a run-stopper
        anomaly for the escalation seam. Fed by every accepted measurement read AND the
        drift-checkpoint reference read (which in the real event was the third stuck read)."""
        cfg = self.cfg
        if cfg.stall_reads <= 0 or self.present_stall:
            return
        mx = max(patch.signal) if patch.signal else 0.0
        if mx <= 0.0:
            return   # a commanded black has no chromaticity direction — the low_drive_high
            #          envelope owns "black reads bright"; it neither joins nor breaks a streak
        y = float(xyz[1])
        if y < cfg.stall_min_nits:
            self._stall_streak = []
            return
        direction = tuple(s / mx for s in patch.signal)
        entry = (direction, (float(xyz[0]), float(xyz[1]), float(xyz[2])), patch.label)
        if self._stall_streak:
            anchor = self._stall_streak[0][1]
            white = self.white_xyz or anchor
            sigma = self.dip.expected_sigma_de(y) if self.dip else None
            tol = max(cfg.stall_floor_de, cfg.stall_sigma_factor * (sigma or 0.0))
            if _agreement_de(entry[1], anchor, white) > tol:
                self._stall_streak = [entry]     # the frame moved — a fresh streak starts here
                return
        self._stall_streak.append(entry)
        if len(self._stall_streak) < cfg.stall_reads:
            return
        # Meaningfully-distinct commanded directions in the streak (greedy clustering: a command
        # joins the first cluster within stall_direction_delta per channel, else founds one).
        clusters: list[tuple[float, float, float]] = []
        for d, _, _ in self._stall_streak:
            if all(max(abs(d[i] - c[i]) for i in range(3)) >= cfg.stall_direction_delta
                   for c in clusters):
                clusters.append(d)
        if len(clusters) < cfg.stall_distinct_commands:
            # A legitimate clamp plateau (same-direction commands equalized by a cap) may run
            # long; keep the streak bounded rather than growing without limit.
            if len(self._stall_streak) > 4 * cfg.stall_reads:
                self._stall_streak.pop(0)
            return
        self.present_stall = True
        # The streak's accepted values are reads of a frozen frame, not of their patches —
        # flag them unstable so they surface in `unresolved` (they stay in the .ti3 only if
        # the seam's judge explicitly accepts a stalled run, which the digest argues against).
        for _, _, lbl in self._stall_streak:
            rec = self.accepted.get(lbl)
            if rec is not None:
                rec.unstable = True
                rec.note = "present_stall: read a stuck frame, value untrustworthy"
        ys = [e[1][1] for e in self._stall_streak]
        self._flag_read_plausibility_anomaly({
            "reason": "present_stall",
            "severity": "run_stopper",
            "measure_phase": self._live_phase,
            "label": patch.label,
            "reads": len(self._stall_streak),
            "distinct_commands": len(clusters),
            "labels": [e[2] for e in self._stall_streak][-8:],
            "rgb": list(patch.rgb),
            "measured_nits_mean": round(sum(ys) / len(ys), 4),
            "measured_nits_span": round(max(ys) - min(ys), 4),
        })

    def _check_read_integrity(self, patch: MeasurePatch, xyz: tuple[float, float, float]) -> None:
        """Cross-patch read-integrity guard for failures the per-read stall clock cannot see. The
        stall clock resets on ANY ok read, so a FROZEN presenter (a stuck frame reads fine and
        repeatably) and a panel that slept MID-RUN (valid ~0-nit reads) both keep it happy forever —
        exactly the ~111-min dogegen-freeze class. Inspect the last few MEASUREMENT reads for two
        signatures the stall guard is blind to and surface either as a measurement-path anomaly the
        LLM consumes from the running spine (never a silent abort — the LLM/operator cancels)."""
        if patch.role != "measurement":
            return
        # (C) Present-stall (stuck frame): the FAST run-stopper signature — identical XYZ across
        # genuinely different commanded colours. Checked first; the window guards below are the
        # slower, luminance-span-based belt-and-braces.
        self._check_present_stall(patch, xyz)
        if self.cfg.integrity_window <= 0:
            return
        expected, _envelope = self._plausible_expected_nits(patch)
        measured = float(xyz[1])
        self._integrity_recent.append((expected, (float(xyz[0]), float(xyz[1]), float(xyz[2]))))
        if len(self._integrity_recent) > self.cfg.integrity_window:
            self._integrity_recent.pop(0)

        # (B) Panel went dark mid-run: a run of LIT patches all reading at/under the dark floor.
        # Channel-aware expected: a gamut-limited primary (dim by physics) never counts as "lit",
        # so it can't stack a false dark streak on a WOLED whose blue peaks in the teens.
        reference = float(self.reference_xyz[1]) if self.reference_xyz else 0.0
        lit_cut = max(self.cfg.plausible_luminance_low_expected_nits * 2.0, reference * 0.5)
        dark_floor = max(self.cfg.plausible_luminance_floor_nits, expected * 0.02)
        if expected >= lit_cut and measured <= dark_floor:
            self._integrity_dark_streak += 1
        else:
            self._integrity_dark_streak = 0
        if (self._integrity_dark_streak >= self.cfg.integrity_dark_run
                and "panel_dark_mid_run" not in self._integrity_flagged):
            self._integrity_flagged.add("panel_dark_mid_run")
            self._flag_read_plausibility_anomaly({
                "reason": "panel_dark_mid_run",
                "measure_phase": self._live_phase,
                "label": patch.label,
                "consecutive_lit_dark_reads": self._integrity_dark_streak,
                "expected_nits": round(expected, 4),
                "measured_nits": round(measured, 4),
            })

        # (A) Frozen presenter: a full window of patches whose commanded peak luminance SHOULD differ
        # a lot, yet every measured read sits within meter noise of the others (one stuck frame).
        if (len(self._integrity_recent) >= self.cfg.integrity_window
                and "frozen_presenter" not in self._integrity_flagged):
            exps = [e for e, _ in self._integrity_recent if e > 0.0]
            ys = [c[1] for _, c in self._integrity_recent]
            mean_y = sum(ys) / len(ys)
            if exps and min(exps) > 0.0 and mean_y > self.cfg.plausible_luminance_floor_nits:
                span = max(exps) / min(exps)
                y_flat = all(abs(y - mean_y) <= self.cfg.integrity_frozen_y_tol * mean_y for y in ys)
                xys = [(c[0] / t, c[1] / t) for _, c in self._integrity_recent if (t := sum(c)) > 0.0]
                mx = sum(p[0] for p in xys) / len(xys) if xys else 0.0
                my = sum(p[1] for p in xys) / len(xys) if xys else 0.0
                xy_flat = bool(xys) and all(
                    ((p[0] - mx) ** 2 + (p[1] - my) ** 2) ** 0.5 <= self.cfg.integrity_frozen_xy_tol
                    for p in xys)
                if span >= self.cfg.integrity_frozen_expected_ratio and y_flat and xy_flat:
                    self._integrity_flagged.add("frozen_presenter")
                    self._flag_read_plausibility_anomaly({
                        "reason": "frozen_presenter",
                        "measure_phase": self._live_phase,
                        "label": patch.label,
                        "window": self.cfg.integrity_window,
                        "expected_nits_span": round(span, 2),
                        "measured_nits_mean": round(mean_y, 4),
                    })

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
        dark_reads = 0
        settled = False
        last_good: Optional[tuple[float, float, float]] = None
        reads = 0

        # Reference reads run under the caller's fixed-display-state guard (identity
        # editor table during the touch-up) so the reference is comparable with the
        # later drift-checkpoint reads regardless of in-session edits.
        with self._reference_read_guard():
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

                # Dark-panel guard: a MID-grey reference reading ~no light means the panel is
                # asleep / off / on the wrong input. Settle "agreement" on black (0≈0) is
                # meaningless, so don't adopt it as the reference and don't let it count toward
                # warm. After a couple of sub-floor reads, declare the panel dark, flag it
                # loudly, and stop — the caller escalates instead of falsely settling and then
                # metering a dark panel for minutes.
                dark_floor = self._dark_floor_for(patch)
                if dark_floor > 0 and reading.xyz[1] < dark_floor:
                    dark_reads += 1
                    self.dark_reference_nits = float(reading.xyz[1])
                    if dark_reads >= cfg.dark_required:
                        self.panel_dark = True
                        self._emit_event("WARN", "panel_dark",
                                         reference_nits=round(float(reading.xyz[1]), 4),
                                         floor_nits=round(dark_floor, 4), reads=reads)
                        break
                    prev = None
                    consecutive = 0
                    continue
                dark_reads = 0

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
        """Whether to run the closed-loop thermal controller before measuring.

        ``auto`` (the default) runs it on ANY characterized panel and lets the controller decide
        from LIVE measured state: it self-activates — a panel already at operating equilibrium reads
        in-band and converges in a couple of blocks (no soak); a cold panel soaks to equilibrium; a
        content-driven one maintains the load. This is the unified, regime-AGNOSTIC path — the
        SDR/HDR difference falls out of the live measurement + the classifier-derived drift handling,
        NOT a branch here.

        It deliberately REPLACES the earlier gate that skipped ``convergent`` panels based on the
        *characterize-time* ``warmin_magnitude`` — a stale snapshot that mis-fired on the
        cold-next-morning case: a panel characterized warm would skip the soak and silently begin
        measuring while still warming in (a moving target). Deciding live closes that hole; a warm
        panel still confirms-and-skips cheaply (the controller's fast self-deactivation).

        ``always`` / ``never`` force it; an UNCHARACTERIZED panel (no DIP / no measured regime) has
        no priors to seed the controller, so it falls back to the static-grey warm-up."""
        mode = self.cfg.preheat
        if mode == "never":
            return False
        if mode == "always":
            return True
        dip = self.dip
        # Characterized (a measured regime exists, and it wasn't a known-bad 'compromised' run) ⇒
        # run the self-limiting controller, decided live. 'compromised' means the characterize
        # display/meter was bad, so its priors aren't trustworthy — fall back to the static warm-up.
        return dip is not None and dip.thermal_regime not in (None, "compromised")

    def _run_soak(self, *, phase: str, max_blocks: Optional[int] = None):
        """Drive a closed-loop :class:`~dlc.thermal.ThermalController` over a NEUTRAL stand-in for
        THIS run's patch set.

        The soak parks the panel at the load the measurement will sustain — but the controller's
        warm-in/convergence is judged on an interleaved NEUTRAL drift reference, so feeding *coloured*
        content (e.g. the verify QC set) loads one channel unevenly and makes that reference read a
        phantom channel-balance "drift" that never settles (HW 2026-06-24: verify preheat glided 17+
        blocks on R). So we soak with a per-patch **max-channel grey** ``(m,m,m)`` instead: on a
        white-LED/FALD panel the backlight level tracks the brightest subpixel demand, so max-channel
        grey reproduces the SAME backlight energy (the thermal driver) while staying neutral — no
        phantom drift, no thermal step at the soak→measure boundary. Applies to all modes. Returns the
        ``ThermalResult`` (or ``None`` when there's no content to soak with). Per-block records are
        kept off ``measurements.ndjson`` (the readout tails it and expects measurement-shaped rows);
        the caller emits a single summary marker instead."""
        from .thermal import ThermalController, ThermalConfig  # lazy: keep the module import light

        self._live_phase = phase
        content = [(m, m, m) for p in self.patches for m in (max(p.rgb),)]
        if not content:
            return None
        max_cv = self.transfer.max_cv
        ref_nits = self.transfer.cv_to_nits(round(self.cfg.warmup_signal * max_cv))
        # Prefer the DIP's measured channel-balance noise (≈ drift_threshold / 3) so the soak's
        # convergence gate is keyed to this panel+meter; else let the controller self-calibrate.
        balance_noise: Optional[float] = None
        if self.dip is not None and self.dip.recommended_drift_threshold:
            balance_noise = self.dip.recommended_drift_threshold / 3.0
        # The DIP's validated warm balance, passed for SHADOW logging only (Phase 1): the controller
        # records how far the first operating read is from it — evidence for the Phase-2 fast-path —
        # but never short-circuits on it yet.
        warm_baseline: Optional[dict[Channel, float]] = None
        if self.dip is not None and self.dip.warm_balance and len(self.dip.warm_balance) >= len(CHANNELS):
            warm_baseline = {ch: float(self.dip.warm_balance[i]) for i, ch in enumerate(CHANNELS)}
        tcfg_kw: dict[str, Any] = {"k_start": self.cfg.preheat_k_start}
        if max_blocks is not None:
            tcfg_kw["max_blocks"] = max_blocks
        ctrl = ThermalController(
            measure=self.measure, transfer=self.transfer, content=content,
            ref_nits=ref_nits, balance_noise=balance_noise, warm_baseline=warm_baseline,
            config=ThermalConfig(**tcfg_kw),
            emit=self._soak_block_emit, event=self._emit_event,
        )
        res = ctrl.run()
        if res.active_channel and self.cold_channel is None:
            self.cold_channel = res.active_channel
        return res

    def _soak_block_emit(self, rec: dict[str, Any]) -> None:
        """Mirror a thermal soak block onto the spine as a STREAM-tier progress tick, so the
        dashboard shows the soak ADVANCING (block counter + warm-in trajectory) instead of a silent
        stretch — the soak is otherwise the longest spell with no per-read mirror. The LLM digest
        drops stream tier and keeps only the milestone ``thermal_state`` / ``thermal_regime`` events.
        Per-block records stay OFF measurements.ndjson (the readout expects measurement-shaped rows)."""
        if self.runlog is not None:
            self.runlog.progress(self._live_phase, **{k: rec.get(k) for k in
                ("block", "k", "net", "gross", "state", "ref_nits", "active_channel",
                 "op_streak", "protection_limited")})
        # The soak's ThermalController reads the meter directly (bypassing _read), and its
        # per-block mirror above is STREAM tier — dropped from the LLM digest. Without this
        # backstop a long preheat is the one spell that can go digest-dark past the §12
        # floor (NO-DARK-WINDOW rule, fable Phase 8).
        self._maybe_checkin_backstop()

    def preheat(self) -> Optional[dict[str, Any]]:
        """Run the closed-loop thermal controller to bring the panel to its operating equilibrium
        BEFORE the main pass (gated by :meth:`_preheat_enabled`; the controller self-deactivates on
        an already-warm panel, so this is cheap when there's nothing to warm). Returns a digest, or
        ``None`` when skipped/empty."""
        if not self._preheat_enabled():
            return None
        res = self._run_soak(phase="preheat")
        if res is None:
            return None
        # SHADOW (Phase 1): would a one-block warm_baseline fast-path have fired, and does the
        # controller's own outcome agree it was warm? Recorded for the Phase-2 decision; not acted on.
        would_fast_path = (res.baseline_distance is not None
                           and res.baseline_distance <= res.drift_threshold)
        # The converged operating-load balance + the threshold it was judged against ride the digest
        # too: they are the evidence a SESSION warm-baseline fast-path (stage N+1 anchored on stage N's
        # converged balance, not the cold characterize baseline the Phase-1 shadow compared against —
        # LG C6 2026-09-02: distance 0.12-0.15 vs threshold, "would_fast_path" False on a panel that
        # then converged in the minimum 4 blocks every stage) needs before it can be enabled.
        digest = {"regime": res.regime, "reason": res.reason, "converged": res.converged,
                  "blocks": res.blocks, "content_reads": res.content_reads, "final_k": res.final_k,
                  "compromised": res.compromised, "protection_limited": res.protection_limited,
                  "active_channel": res.active_channel, "baseline_distance": res.baseline_distance,
                  "shadow_would_fast_path": would_fast_path,
                  "warm_balance": ({str(k): round(float(v), 6) for k, v in res.warm_balance.items()}
                                   if res.warm_balance else None),
                  "drift_threshold": (round(float(res.drift_threshold), 6)
                                      if res.drift_threshold is not None else None)}
        self.ndjson.emit({"t": _now(), "phase": "preheat", "role": "preheat_complete", **digest})
        self._emit_event("INFO" if (res.converged and not res.compromised and not res.protection_limited)
                         else "WARN", "preheat_complete", **digest)
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

    def _is_near_neutral(self, patch: MeasurePatch) -> bool:
        """Whether ``patch`` sits in the chroma-critical near-neutral region (grey ramp +
        off-axis tube) that the matrix / WB / non-additivity derivation is sensitive to.
        Relative-span test: balanced channels ⇒ near-neutral; a pure-channel or secondary
        ramp patch has a zero channel ⇒ span == max ⇒ excluded. (See ``neutral_chroma_span``.)"""
        sig = patch.signal
        mx = max(sig)
        if mx <= 0.0:
            return False
        return (mx - min(sig)) <= self.cfg.neutral_chroma_span * mx

    def _read_floor_for(self, patch: MeasurePatch) -> int:
        """The per-patch minimum read count: the global ``min_reads``, raised to
        ``neutral_min_reads`` on a near-neutral patch (gated to ``neutral_floor_min_nits`` and
        brighter) so the chroma-critical region is averaged where it pays off — fast, larger-σ
        reads — without multiplying the slow dim patches. The DIP still escalates above this where
        measured σ demands it."""
        floor = self.cfg.min_reads
        if self._is_near_neutral(patch):
            nits = self._expected_patch_nits(patch)
            # Bright near-neutral floor (largest luminance σ, fast reads).
            if self.cfg.neutral_min_reads > floor and nits >= self.cfg.neutral_floor_min_nits:
                floor = self.cfg.neutral_min_reads
            # Dark near-neutral floor (estimate the CHROMA spread that drives dark-level trust).
            if self.cfg.dark_min_reads > floor and nits <= self.cfg.dark_floor_max_nits:
                floor = self.cfg.dark_min_reads
        return floor

    def _dark_floor_binds(self, patch: MeasurePatch) -> bool:
        """True when the DARK read floor is what raised this patch's read count above the global
        minimum — the only floor ``dark_agree_reads`` may satisfy early (the bright near-neutral
        floor averages for SNR and is never shortened)."""
        if not self._is_near_neutral(patch):
            return False
        nits = self._expected_patch_nits(patch)
        if not (self.cfg.dark_min_reads > self.cfg.min_reads and nits <= self.cfg.dark_floor_max_nits):
            return False
        bright_floor = (self.cfg.neutral_min_reads
                        if nits >= self.cfg.neutral_floor_min_nits else self.cfg.min_reads)
        return self.cfg.dark_min_reads > bright_floor

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
        dip_n: Optional[int] = None             # the DIP's own SNR read target (None ⇒ no DIP)
        dark_bound = False                      # the DARK floor set target_n (⇒ dark_agree_reads may satisfy it early)
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
                anomaly = self._read_plausibility_anomaly(
                    patch, r.xyz, phase=phase, read_index=read_index - 1
                )
                if anomaly is not None:
                    self._flag_read_plausibility_anomaly(anomaly)
                    unstable = True
                    note = f"read plausibility anomaly: {anomaly['reason']}"
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
                    # Floor (global min_reads, raised on near-neutral patches), with the DIP free to
                    # escalate ABOVE it where measured σ demands more averaging for SNR.
                    floor = self._read_floor_for(patch)
                    target_n = max(floor, dip_n or floor)
                    # The dark floor is the binding target only when the DIP itself wanted fewer.
                    dark_bound = self._dark_floor_binds(patch) and (dip_n or 0) < floor

            st = self._robust_stats(reads, sigma=sigma)
            n_inliers = st[2] if st else 0
            se = st[1] if st else None
            outliers = st[3] if st else 0

            # Dark-floor early stop: the floor's job is a chroma-spread ESTIMATE, which two agreeing
            # reads already provide (conservatively). Requires a clean cluster (no outliers), the
            # DIP's own SNR target met, and agreement within tolerance — a disagreeing pair keeps
            # reading to the floor and beyond exactly as before. Never shortens the BRIGHT floor.
            if (dark_bound and cfg.dark_agree_reads > 0
                    and n_inliers >= cfg.dark_agree_reads
                    and n_inliers >= (dip_n or 0)
                    and outliers == 0
                    and se is not None and se <= cfg.read_tolerance_de):
                break

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
        # Measured repeatability for the dark-level trust: the per-patch standard error (dE) and the
        # read-to-read chromaticity spread (xy). Both need ≥2 reads (None otherwise).
        accepted_se = st[1] if st else None
        accepted_chroma_sigma = self._chroma_sigma(reads, sigma=sigma)
        immediate = max(0, read_index - 1)
        round_usable = usable                       # whether THIS round produced a usable value
        record = self.accepted.get(patch.label)
        if record is None:
            record = AcceptedRead(
                patch=patch, xyz=accepted_xyz, yxy=accepted_yxy,
                reads_taken=read_index, immediate_remeasures=immediate,
                unstable=unstable, usable=usable, note=note,
                se_de=accepted_se, chroma_sigma=accepted_chroma_sigma,
                noise_reads=read_index,
            )
            self.accepted[patch.label] = record
        elif not round_usable and record.usable:
            # A re-measure round that produced NOTHING usable (e.g. the meter died mid-queue)
            # must not destroy the prior accepted read: a cold-but-real value beats a sentinel
            # hole that drops the patch from the .ti3 entirely. Keep the prior XYZ + its noise
            # stats, accumulate the read count, and FLAG the patch (unstable → unresolved →
            # adjudication) so the failure is loud, not silently data-erasing.
            record.reads_taken += read_index
            record.unstable = True
            record.note = "re-measure produced no usable read; prior accepted value retained"
        else:
            # Overwrite in place (re-measure): keep only the final accepted read. reads_taken sums
            # across rounds (lifetime), but se_de/chroma_sigma/noise_reads describe THIS round only —
            # so the dark-trust σ/√n divides by the matching per-round count, not the inflated lifetime.
            record.xyz = accepted_xyz
            record.yxy = accepted_yxy
            record.reads_taken += read_index
            record.immediate_remeasures += immediate
            record.unstable = unstable
            record.usable = usable
            record.note = note
            record.se_de = accepted_se
            record.chroma_sigma = accepted_chroma_sigma
            record.noise_reads = read_index
        if round_usable:
            self._check_read_integrity(patch, accepted_xyz)
        return record

    # -- main pass ---------------------------------------------------------

    def main_pass(self) -> None:
        self._live_phase = "measure"
        warmup_patch = self._warmup_patch()
        pending: list[str] = []  # measured since the last clean neutral checkpoint
        next_checkpoint = self.neutral_interval_current if self.neutral_interval_current > 0 else None

        for index, patch in enumerate(self.patches, start=1):
            self.measure_patch(patch, phase="main")
            if self.present_stall:
                # Run-stopper: the presented frame is stuck — every further read would be the
                # same frozen frame (the 2026-09-02 event corrupted the verify tail + poisoned
                # the drift checkpoint this way). Halt the pass HERE; the escalation seam
                # adjudicates immediately instead of after N more garbage patches.
                self._emit_event("WARN", "present_stall_halt",
                                 measured=len(self.accepted), total=len(self.patches),
                                 label=patch.label)
                return
            pending.append(patch.label)
            self._maybe_checkin(index)   # coarse digest check-in at quartiles (LLM visibility)

            if next_checkpoint is not None and index >= next_checkpoint:
                self._neutral_checkpoint(warmup_patch, pending, patch_index=index)
                if self.present_stall:
                    self._emit_event("WARN", "present_stall_halt",
                                     measured=len(self.accepted), total=len(self.patches),
                                     label=patch.label)
                    return
                next_checkpoint = index + self.neutral_interval_current if self.neutral_interval_current > 0 else None

        # Final checkpoint for the tail of the pass.
        if pending:
            self._neutral_checkpoint(warmup_patch, pending, final=True, patch_index=len(self.patches))

    def _recent_drift_summary(self) -> dict[str, Any]:
        window = max(1, self.cfg.drift_density_window)
        recent = self.drift_checkpoints[-window:]
        repeats = [s for s in recent if s.get("repeat")]
        max_delta = max((float(s.get("max_delta", 0.0)) for s in recent), default=0.0)
        return {
            "checkpoints": len(self.drift_checkpoints),
            "window": window,
            "recent_count": len(recent),
            "recent_repeats": len(repeats),
            "repeat_density": (round(len(repeats) / len(recent), 3) if recent else 0.0),
            "max_delta": round(max_delta, 6),
            "regime": self.drift_regime,
            "neutral_interval": self.neutral_interval_current,
        }

    def _classify_drift_regime(self) -> str:
        window = max(1, self.cfg.drift_density_window)
        recent = self.drift_checkpoints[-window:]
        if not recent:
            return "unknown"
        repeats = [s for s in recent if s.get("repeat")]
        if not repeats:
            return "bounded_fluctuation"
        pairs = [(s.get("dominant_channel"), s.get("direction")) for s in repeats
                 if s.get("direction") != "flat"]
        if not pairs:
            return "excursion"
        top = max(pairs, key=pairs.count)
        consistency = pairs.count(top) / len(repeats)
        dense = len(repeats) >= max(1, self.cfg.drift_density_limit)
        if consistency >= 0.75:
            return "directional_warm_in" if len(repeats) >= 2 else "excursion"
        return "chaotic" if dense else "excursion"

    def _maybe_tighten_neutral_interval(self, summary: dict[str, Any]) -> None:
        current = self.neutral_interval_current
        if current <= 0:
            return
        min_interval = max(1, self.cfg.adaptive_neutral_min)
        if current <= min_interval:
            return
        if int(summary.get("recent_repeats", 0)) < 2:
            return
        new_interval = max(min_interval, max(1, current // 2))
        if new_interval >= current:
            return
        self.neutral_interval_current = new_interval
        self.neutral_interval_adjustments += 1
        self._emit_event(
            "INFO",
            "neutral_interval_adjusted",
            previous=current,
            current=new_interval,
            reason="repeated drift checkpoints exceeded the runtime envelope",
            recent_repeats=summary.get("recent_repeats"),
            window=summary.get("window"),
            drift_regime=self.drift_regime,
        )

    def _record_drift_checkpoint(
        self,
        *,
        ev,
        current_xyz: tuple[float, float, float],
        pending_count: int,
        final: bool,
        patch_index: Optional[int],
    ) -> dict[str, Any]:
        reference = normalized_channels(self.reference_xyz) if self.reference_xyz is not None else None
        current = normalized_channels(current_xyz)
        signed = ({ch: current[ch] - reference[ch] for ch in CHANNELS}
                  if reference is not None else {ch: 0.0 for ch in CHANNELS})
        dominant = max(CHANNELS, key=lambda ch: abs(signed[ch]))
        dom_value = signed[dominant]
        direction = "positive" if dom_value > 0 else "negative" if dom_value < 0 else "flat"
        sample = {
            "checkpoint": len(self.drift_checkpoints) + 1,
            "patch_index": patch_index,
            "pending_count": pending_count,
            "final": final,
            "repeat": ev.repeat,
            "max_delta": round(ev.max_channel_delta, 6),
            "threshold": round(self.cfg.drift_threshold, 6),
            "coldest": ev.coldest_channel,
            "dominant_channel": dominant,
            "direction": direction,
            "channel_deltas": {ch: round(signed[ch], 6) for ch in CHANNELS},
        }
        self.drift_checkpoints.append(sample)
        self.drift_regime = self._classify_drift_regime()
        sample["regime"] = self.drift_regime
        summary = self._recent_drift_summary()
        sample["recent_repeats"] = summary["recent_repeats"]
        sample["repeat_density"] = summary["repeat_density"]

        if ev.repeat:
            self._maybe_tighten_neutral_interval(summary)
            summary = self._recent_drift_summary()
            if int(summary["recent_repeats"]) >= max(1, self.cfg.drift_density_limit):
                self.drift_density_exceeded = True
                if not self.drift_density_event_emitted:
                    self.drift_density_event_emitted = True
                    self._emit_event(
                        "WARN",
                        "drift_density_exceeded",
                        recent_repeats=summary["recent_repeats"],
                        window=summary["window"],
                        repeat_density=summary["repeat_density"],
                        max_delta=summary["max_delta"],
                        drift_regime=self.drift_regime,
                        neutral_interval=self.neutral_interval_current,
                    )
        return sample

    def _neutral_checkpoint(
        self,
        warmup_patch: MeasurePatch,
        pending: list[str],
        *,
        final: bool = False,
        patch_index: Optional[int] = None,
    ) -> None:
        if self.reference_xyz is None:
            pending.clear()
            return
        # One physical read → one enriched ndjson line carrying the drift verdict
        # (we need the XYZ before we can compute the verdict, so emit inline
        # rather than via _read, which emits atomically on measure). The read runs
        # under the caller's fixed-display-state guard so an in-session edit of the
        # patch it sits on cannot masquerade as panel drift.
        # The drift-ref read pays the same luminance-jump settle bump as a measurement
        # read: under an alternating order the checkpoint often lands right after a
        # bright patch, and reading the mid-grey through residual zone glow would
        # register as phantom drift.
        with self._reference_read_guard():
            reading, _ = self._present_and_measure(warmup_patch)
        seq = self.seq_counter
        self.seq_counter += 1
        if reading.xyz is None:
            record = {
                "t": _now(), "seq": seq, "phase": "main", "role": "neutral_ref",
                "label": warmup_patch.label, "rgb": list(warmup_patch.rgb),
                "signal": [round(s, 6) for s in warmup_patch.signal], "read_index": 0,
                "xyz": None, "yxy": None, "nits": None, "ok": False, "accepted": False,
                "agreement_de": None, "drift": None, "settle": None,
                "disposition": "drift_ref", "note": "drift_checkpoint_failed",
            }
            self.ndjson.emit(record)
            self._mirror_patch_read(record)   # a failed drift checkpoint is still a (failed) read
            return
        self._update_white(reading.xyz)
        # The drift-reference read joins the present-stall streak: in the 2026-09-02 event it
        # WAS the third stuck read (mid-grey commanded, same frozen ~402-nit white measured) —
        # and a stuck frame makes the drift verdict below garbage (the real event queued 7 good
        # patches for cold-remeasure off the frozen reference). On a stall: record the read for
        # the audit stream, skip the drift evaluation, and let the run-stopper seam decide.
        self._check_present_stall(warmup_patch, reading.xyz)
        if self.present_stall:
            record = {
                "t": _now(), "seq": seq, "phase": "main", "role": "neutral_ref",
                "label": warmup_patch.label, "rgb": list(warmup_patch.rgb),
                "signal": [round(s, 6) for s in warmup_patch.signal], "read_index": 0,
                "xyz": list(reading.xyz),
                "yxy": list(reading.yxy) if reading.yxy is not None else None,
                "nits": reading.nits, "ok": True, "accepted": False,
                "agreement_de": None, "drift": None, "settle": None,
                "disposition": "drift_ref", "note": "present_stall",
            }
            self.ndjson.emit(record)
            self._mirror_patch_read(record)
            return
        ev = evaluate_drift(
            stabilized_xyz=self.reference_xyz,
            current_xyz=reading.xyz,
            delta_threshold=self.cfg.drift_threshold,
        )
        drift_sample = self._record_drift_checkpoint(
            ev=ev,
            current_xyz=reading.xyz,
            pending_count=len(pending),
            final=final,
            patch_index=patch_index,
        )
        record = {
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
                "channel_deltas": drift_sample["channel_deltas"],
                "dominant": drift_sample["dominant_channel"],
                "direction": drift_sample["direction"],
                "regime": drift_sample["regime"],
                "recent_repeats": drift_sample["recent_repeats"],
                "repeat_density": drift_sample["repeat_density"],
                "neutral_interval": self.neutral_interval_current,
            },
            "settle": None,
            "disposition": "drift_ref",
            "note": "drift_checkpoint",
        }
        self.ndjson.emit(record)
        # Mirror onto the spine: the interleaved neutral re-read is the CLEANEST white-drift
        # signal (a fixed neutral re-measured over time), so it belongs on the dashboard's
        # drift chart + the read count — without this it lived only in the ndjson, invisible.
        self._mirror_patch_read(record)

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
                drift_regime=self.drift_regime,
                recent_repeats=drift_sample["recent_repeats"],
                repeat_density=drift_sample["repeat_density"],
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

        for idx, patch in enumerate(queue):
            if self.remeasure_budget <= 0 and not self.remeasure_budget_exceeded:
                self.remeasure_budget_exceeded = True
                self._emit_event(
                    "WARN",
                    "remeasure_budget_exceeded",
                    cap=cfg.remeasure_cap,
                    queued=len(queue),
                    remaining=len(queue) - idx,
                )
            if self.remeasure_budget > 0:
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


class IncrementalMeasureSession:
    """A persistent measurement loop for interactive patch-by-patch workflows.

    ``run_measure_loop`` owns ordinary batch measurement. The Grayscale touch-up
    flow needs a different rhythm: warm/preheat once, measure a point, let the
    caller mutate DesktopLUT's editor state, re-measure the same point, then move
    on. This wrapper reuses the same private ``_Loop`` so the warm reference,
    drift checkpoints, adaptive neutral interval, and rewarm behavior stay
    continuous across those caller-driven reads.
    """

    def __init__(
        self,
        *,
        patches: Sequence[Patch],
        transfer: Transfer,
        measure: MeasureFn,
        config: Optional[MeasureLoopConfig] = None,
        ndjson_path: Optional[Path] = None,
        events: Optional[EventWriter] = None,
        runlog: Optional[RunLog] = None,
        liveness: Optional[Liveness] = None,
        dip: Optional[DisplayInstrumentProfile] = None,
        checkin_interval_s: float = 0.0,
        reference_guard: Optional[Callable[[], ContextManager[None]]] = None,
        channel_peak_y: Optional[tuple[float, float, float]] = None,
        white_peak_y: Optional[float] = None,
        correction_max_nits: Optional[float] = None,
        correction_channel_scale: Optional[tuple[float, float, float]] = None,
    ) -> None:
        self.cfg = config or MeasureLoopConfig()
        self.ndjson = _NdjsonWriter(ndjson_path)
        self.loop = _Loop(
            patches=patches,
            transfer=transfer,
            measure=measure,
            config=self.cfg,
            ndjson=self.ndjson,
            events=events,
            runlog=runlog,
            liveness=liveness,
            dip=dip,
            checkin_interval_s=checkin_interval_s,
            reference_guard=reference_guard,
            channel_peak_y=channel_peak_y,
            white_peak_y=white_peak_y,
            correction_max_nits=correction_max_nits,
            correction_channel_scale=correction_channel_scale,
        )
        self.preheat_digest: Optional[dict[str, Any]] = None
        self.preheat_compromised = False
        self.started = False
        self.measure_count = 0
        self.pending: list[str] = []
        self._warmup_patch: Optional[MeasurePatch] = None

    def start(self) -> dict[str, Any]:
        if self.started:
            return self.digest()
        self.preheat_digest = self.loop.preheat()
        self.preheat_compromised = bool(self.preheat_digest and self.preheat_digest.get("compromised"))
        self.loop.warm_up()
        self._warmup_patch = self.loop._warmup_patch()
        self.started = True
        return self.digest()

    def measure_index(self, index: int) -> AcceptedRead:
        if not self.started:
            self.start()
        if self.loop.panel_dark:
            raise RuntimeError("panel dark during incremental measurement session")
        patch = self.loop.patches[index]
        self.loop.measure_patch(patch, phase="main")
        accepted = self.loop.accepted[patch.label]
        self.pending.append(patch.label)
        self.measure_count += 1
        self.loop._maybe_checkin(min(self.measure_count, max(1, len(self.loop.patches))))
        interval = self.loop.neutral_interval_current
        if interval > 0 and self.measure_count % interval == 0 and self._warmup_patch is not None:
            self.loop._neutral_checkpoint(self._warmup_patch, self.pending,
                                          patch_index=self.measure_count)
        return accepted

    def finish(self) -> dict[str, Any]:
        if self.started and self.pending and self._warmup_patch is not None:
            self.loop._neutral_checkpoint(self._warmup_patch, self.pending,
                                          final=True, patch_index=self.measure_count)
        # In a caller-mutated session, a drift episode means the prior editor
        # measurements may no longer describe the same display state. Keep the
        # evidence instead of silently appending re-measures under different
        # correction settings; the caller/LLM adjudicates from this digest.
        return self.digest()

    def digest(self) -> dict[str, Any]:
        drift_summary = self.loop._recent_drift_summary()
        needs_adjudication = (
            self.loop.panel_dark
            or self.preheat_compromised
            or self.loop.measurement_path_compromised
            or (not self.loop.warm)
            or self.loop.remeasure_budget_exceeded
            or self.loop.drift_density_exceeded
            or self.loop.drift_episodes > 0
        )
        return {
            "warm": self.loop.warm,
            "preheat_compromised": self.preheat_compromised,
            "panel_dark": self.loop.panel_dark,
            "present_stall": self.loop.present_stall,
            "dark_reference_nits": (round(self.loop.dark_reference_nits, 4)
                                    if self.loop.dark_reference_nits is not None else None),
            "warmup_reads": self.loop.warmup_reads,
            "reference_xyz": [round(c, 4) for c in self.loop.reference_xyz] if self.loop.reference_xyz else None,
            "read_count": self.loop.seq_counter,
            "patch_measurements": self.measure_count,
            # The alternating-order watch pair (D4): how often reads within a patch had to
            # repeat (glow/noise churn shows up here first) and how many presentations paid
            # the luminance-jump settle bump.
            "immediate_remeasures": sum(r.immediate_remeasures
                                        for r in self.loop.accepted.values()),
            "jump_settles": self.loop.jump_settles,
            "drift_episodes": self.loop.drift_episodes,
            "drift_checkpoints": drift_summary["checkpoints"],
            "drift_recent_repeats": drift_summary["recent_repeats"],
            "drift_repeat_density": drift_summary["repeat_density"],
            "drift_regime": self.loop.drift_regime,
            "drift_density_exceeded": self.loop.drift_density_exceeded,
            "neutral_interval_initial": self.cfg.neutral_interval,
            "neutral_interval_final": self.loop.neutral_interval_current,
            "neutral_interval_adjustments": self.loop.neutral_interval_adjustments,
            "preheat": self.preheat_digest,
            "needs_adjudication": needs_adjudication,
        }


# The non-stopper plausibility-envelope reasons (item #4): repeatability can argue "accept" only
# for these. lit_drive_low is the one a correction/gamut limit legitimately produces; a stable
# TOO-BRIGHT read (low_drive_high) is never panel physics (attenuation only reduces light) — it
# is classified for evidence but the orchestrator never downgrades retry on it. Run-stopper
# reasons (present_stall, panel_dark_mid_run, frozen_presenter) are excluded entirely: a stuck
# frame is perfectly "repeatable".
_ENVELOPE_ANOMALY_REASONS = ("lit_drive_low_luminance", "low_drive_high_luminance")


def _read_anomaly_repeatability(
    anomalies: Sequence[dict[str, Any]],
    accepted: dict[str, AcceptedRead],
    cfg: MeasureLoopConfig,
) -> Optional[dict[str, Any]]:
    """Repeatability evidence over the ENVELOPE anomalies for the escalation seam (item #4,
    2026-09-02 C6 run): group flagged reads by commanded RGB (verify bookends re-read the same
    stimulus at start + end, so a real limit shows up as the SAME implausible value again) and
    measure the relative luminance spread within each group; a singleton group falls back to the
    patch's own multi-read standard error when it has one. ``classification``:

    * ``stable``  — every judged group repeats within ``anomaly_stable_spread`` (and at least one
      group could be judged): stable-but-implausible ⇒ real panel/correction behaviour.
    * ``noisy``   — any judged group diverges: transient meter/display fault; retry should clear it.
    * ``mixed``   — stable evidence next to unjudgeable singletons.
    * ``unknown`` — no group re-read and no per-patch spread: nothing to argue from.

    Evidence for the LLM (and the seam's RECOMMENDATION) — never a decision. ``None`` when no
    envelope anomalies were flagged."""
    env = [a for a in anomalies if a.get("reason") in _ENVELOPE_ANOMALY_REASONS]
    if not env:
        return None
    groups: dict[tuple[int, ...], list[dict[str, Any]]] = {}
    for a in env:
        groups.setdefault(tuple(a.get("rgb") or ()), []).append(a)
    out_groups: list[dict[str, Any]] = []
    verdicts: list[str] = []
    for rgb, items in groups.items():
        ys = [float(a.get("measured_nits") or 0.0) for a in items]
        labels = sorted({a.get("label") for a in items if a.get("label")})
        mean = sum(ys) / len(ys)
        spread = max(ys) - min(ys)
        rel = (spread / mean) if mean > 0.0 else math.inf
        group: dict[str, Any] = {
            "rgb": list(rgb),
            "flagged_reads": len(ys),
            "mean_nits": round(mean, 4),
            "spread_nits": round(spread, 4),
            "rel_spread": (round(rel, 4) if math.isfinite(rel) else None),
            "labels": labels[:6],
        }
        if len(ys) >= 2:
            group["verdict"] = "stable" if rel <= cfg.anomaly_stable_spread else "noisy"
        else:
            rec = accepted.get(labels[0]) if labels else None
            if rec is not None and rec.se_de is not None and (rec.noise_reads or 0) >= 2:
                group["se_de"] = round(rec.se_de, 4)
                group["verdict"] = ("stable" if rec.se_de <= 2.0 * cfg.read_tolerance_de
                                    else "noisy")
            else:
                group["verdict"] = "unknown"
        verdicts.append(group["verdict"])
        out_groups.append(group)
    if "noisy" in verdicts:
        classification = "noisy"
    elif "stable" in verdicts:
        classification = "stable" if "unknown" not in verdicts else "mixed"
    else:
        classification = "unknown"
    return {
        "classification": classification,
        "all_low_luminance": all(a.get("reason") == "lit_drive_low_luminance" for a in env),
        "stable_spread_threshold": cfg.anomaly_stable_spread,
        "groups": out_groups[:8],
    }


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
    checkin_interval_s: float = 0.0,
    channel_peak_y: Optional[tuple[float, float, float]] = None,
    white_peak_y: Optional[float] = None,
    correction_max_nits: Optional[float] = None,
    correction_channel_scale: Optional[tuple[float, float, float]] = None,
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

    ``channel_peak_y`` / ``white_peak_y`` / ``correction_max_nits`` /
    ``correction_channel_scale`` are the OPTIONAL plausibility-envelope context (measured
    per-channel peaks, WRGB white headroom, an installed correction's cap/attenuation) — see
    :meth:`_Loop._plausible_expected_nits`. Absent ⇒ the container fallback (previous behaviour).
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
        checkin_interval_s=checkin_interval_s,
        channel_peak_y=channel_peak_y,
        white_peak_y=white_peak_y,
        correction_max_nits=correction_max_nits,
        correction_channel_scale=correction_channel_scale,
    )

    preheat_digest = loop.preheat()
    preheat_compromised = bool(preheat_digest and preheat_digest.get("compromised"))
    loop.warm_up()
    if loop.panel_dark:
        # The panel is emitting ~no light (asleep/off/wrong input). Skip the main pass entirely —
        # metering it just yields black data or hangs the meter per patch (the 8-minute silent
        # spin this guard exists to prevent). Surface it for adjudication instead.
        unresolved: list[str] = []
    else:
        loop.main_pass()
        # A present-stall halts the pass mid-way; the appended queue was built against a live
        # panel and re-measuring it through a frozen frame just multiplies garbage — skip it
        # and let the run-stopper seam decide (retry re-measures everything anyway).
        unresolved = loop.drain_appended() if not loop.present_stall else []

    accepted = loop.ordered_accepted()
    written_ti3: Optional[str] = None
    if ti3_path is not None and accepted:
        write_ti3(ti3_path, accepted)
        written_ti3 = str(ti3_path)
        _write_noise_sidecar(ti3_path, accepted)

    immediate = sum(r.immediate_remeasures for r in accepted)
    appended = sum(r.appended_remeasures for r in accepted)
    unstable_labels = [r.patch.label for r in accepted if r.unstable]
    # "Unresolved" = a patch the loop could not stabilise after the immediate/appended gates.
    # The appended remeasure cap is advisory: crossing it triggers adjudication, but the loop
    # keeps remeasuring the finite queue so the downstream engines get the best data available.
    unresolved_all = sorted(set(unresolved) | set(unstable_labels))
    # Per-patch noise context for the escalation seam (fable Phase 8, digest-sufficiency):
    # "would not stabilise" is judgeable only with the numbers next to it — the observed
    # standard error vs the loop's tolerance, the DIP's expected per-read σ at that
    # luminance (is this patch noisier than this panel+meter normally IS here, or is the
    # DIP itself predicting a noisy band?), and how many reads were burned trying.
    unresolved_detail: list[dict[str, Any]] = []
    for r in accepted:
        if not r.unstable or len(unresolved_detail) >= 8:
            continue
        nits = float(r.xyz[1]) if r.xyz else None
        unresolved_detail.append({
            "label": r.patch.label,
            "nits": (round(nits, 3) if nits is not None else None),
            "observed_se_de": (round(r.se_de, 4) if r.se_de is not None else None),
            "tolerance_de": cfg.read_tolerance_de,
            "dip_expected_sigma_de": (round(loop.dip.expected_sigma_de(nits), 4)
                                      if loop.dip and nits is not None else None),
            "reads_taken": r.reads_taken,
            "note": r.note,
        })
    drift_summary = loop._recent_drift_summary()
    # Escalation-recommendation evidence (item #4): are the envelope-anomalous reads REPEATABLE
    # (stable-but-implausible ⇒ real panel/correction behaviour) or divergent (transient fault)?
    anomaly_repeatability = _read_anomaly_repeatability(loop.read_anomalies, loop.accepted, cfg)

    needs_adjudication = (
        loop.panel_dark
        or preheat_compromised
        or loop.measurement_path_compromised
        or (not loop.warm)
        or bool(unresolved_all)
        or loop.remeasure_budget_exceeded
        or loop.drift_density_exceeded
    )
    anomaly_reasons = [
        name for name, active in (
            ("panel_dark", loop.panel_dark),
            ("present_stall", loop.present_stall),
            ("preheat_compromised", preheat_compromised),
            ("measurement_path_compromised", loop.measurement_path_compromised),
            ("not_warm", not loop.warm),
            ("unresolved", bool(unresolved_all)),
            ("remeasure_budget_exceeded", loop.remeasure_budget_exceeded),
            ("drift_density_exceeded", loop.drift_density_exceeded),
        )
        if active
    ]
    question = None
    if needs_adjudication:
        bits = []
        if loop.present_stall:
            st = next((a for a in loop.read_anomalies if a.get("reason") == "present_stall"), {})
            bits.append(
                f"PRESENT-STALL (run-stopper): {st.get('reads', '?')} consecutive reads across "
                f"{st.get('distinct_commands', '?')} different commanded colours returned identical "
                f"XYZ (~{st.get('measured_nits_mean', '?')} cd/m^2) — the presented frame appears "
                f"STUCK (TV auto-sleep/screensaver/frozen presenter); the pass was halted at "
                f"{len(accepted)}/{len(loop.patches)} patches; wake/fix the display path, then retry"
            )
        if loop.panel_dark:
            ref = loop.dark_reference_nits if loop.dark_reference_nits is not None else 0.0
            bits.append(
                f"panel appears DARK/asleep — the mid-grey reference read {ref:.2f} cd/m² "
                f"(floor {cfg.dark_floor_nits}); no patches were measured (wake the panel / "
                "check the input + that the patch window is showing, then retry)"
            )
        if preheat_compromised:
            bits.append(
                "preheat classified the display/meter path as COMPROMISED before measurement "
                "(wrong colorspace/frozen patch/meter issue); measurement continued and this "
                "data needs adjudication"
            )
        if loop.measurement_path_compromised and not loop.present_stall:
            anomaly = loop.read_anomalies[0] if loop.read_anomalies else {}
            bits.append(
                "a patch read was outside the plausible luminance envelope "
                f"({anomaly.get('reason', 'read_plausibility_anomaly')} at "
                f"{anomaly.get('label', 'unknown patch')}: measured "
                f"{anomaly.get('measured_nits', 'n/a')} cd/m^2 vs expected "
                f"{anomaly.get('expected_nits', 'n/a')} cd/m^2); measurement continued "
                "and this data needs adjudication"
            )
            # Repeatability verdict rides the question so the seam's judge sees WHY the
            # recommendation leans accept (stable ⇒ real behaviour) or retry (divergent ⇒
            # transient) — evidence, not a decision.
            if anomaly_repeatability is not None:
                cls = anomaly_repeatability["classification"]
                if cls == "stable":
                    bits.append(
                        "the anomalous reads are REPEATABLE across re-reads of the same stimulus "
                        "(stable-but-implausible) — usually real panel/correction behaviour, not "
                        "a transient fault; a retry would re-measure the same values"
                    )
                elif cls == "noisy":
                    bits.append(
                        "the anomalous reads are DIVERGENT across re-reads of the same stimulus — "
                        "consistent with a transient meter/display fault; a retry should clear it"
                    )
        if not loop.warm and not loop.panel_dark:
            bits.append(
                f"panel did not settle within {cfg.max_warmup_reads} warm-up reads "
                f"(cold channel {loop.cold_channel})"
            )
        if loop.remeasure_budget_exceeded:
            bits.append(
                f"appended remeasures exceeded the advisory budget "
                f"({cfg.remeasure_cap} cap, {appended} performed); the loop continued "
                "the queued remeasures and is surfacing this for review"
            )
        if loop.drift_density_exceeded:
            bits.append(
                f"drift repeatedly exceeded the runtime envelope "
                f"({drift_summary['recent_repeats']}/{drift_summary['window']} recent checkpoints, "
                f"regime {loop.drift_regime}, max delta {drift_summary['max_delta']}); "
                f"neutral interval tightened to {loop.neutral_interval_current}"
            )
        if unresolved_all:
            bits.append(
                f"{len(unresolved_all)} patch(es) would not stabilise: "
                + ", ".join(unresolved_all[:8])
                + ("..." if len(unresolved_all) > 8 else "")
            )
        question = (
            "; ".join(bits)
            + " - accept the completed measurements, retry with adjusted thermal/drift settings, "
            "or abort?"
        )

    digest = {
        "warm": loop.warm,
        "panel_dark": loop.panel_dark,
        "present_stall": loop.present_stall,
        "preheat_compromised": preheat_compromised,
        "measurement_path_compromised": loop.measurement_path_compromised,
        "read_anomalies": loop.read_anomalies[:8],
        "read_anomaly_repeatability": anomaly_repeatability,
        "dark_reference_nits": (round(loop.dark_reference_nits, 4)
                                if loop.dark_reference_nits is not None else None),
        "warmup_reads": loop.warmup_reads,
        "cold_channel": loop.cold_channel,
        "reference_xyz": [round(c, 4) for c in loop.reference_xyz] if loop.reference_xyz else None,
        "patch_count": len(accepted),
        "total_reads": loop.seq_counter,
        "immediate_remeasures": immediate,
        "appended_remeasures": appended,
        "jump_settles": loop.jump_settles,
        "remeasure_cap": cfg.remeasure_cap,
        "remeasure_budget_remaining": max(0, loop.remeasure_budget),
        "remeasure_budget_exceeded": loop.remeasure_budget_exceeded,
        "drift_episodes": loop.drift_episodes,
        "drift_checkpoints": drift_summary["checkpoints"],
        "drift_recent_repeats": drift_summary["recent_repeats"],
        "drift_repeat_density": drift_summary["repeat_density"],
        "drift_regime": loop.drift_regime,
        "drift_density_exceeded": loop.drift_density_exceeded,
        "neutral_interval_initial": cfg.neutral_interval,
        "neutral_interval_final": loop.neutral_interval_current,
        "neutral_interval_adjustments": loop.neutral_interval_adjustments,
        "unresolved": unresolved_all,
        "unresolved_detail": unresolved_detail,
        "white_xyz": [round(c, 4) for c in loop.white_xyz] if loop.white_xyz else None,
        "white_nits": round(loop.white_xyz[1], 3) if loop.white_xyz else None,
        "preheat": preheat_digest,
        "needs_adjudication": needs_adjudication,
        "read_anomaly": needs_adjudication,
        "anomaly_reasons": anomaly_reasons,
    }
    if events is not None:
        events.write(
            "INFO" if not needs_adjudication else "WARN",
            "measure_loop",
            "completed",
            **{k: v for k, v in digest.items() if k != "reference_xyz"},
        )
    # Land the measure stage's OUTCOME (warm, drift episodes, reads, white, unresolved) on
    # the shared spine as a digest-tier event — otherwise the LLM's digest projection sees a
    # measure stage's start/end but never its result (the rich digest used to reach only the
    # legacy `events` writer, which the orchestrator doesn't pass). The dashboard already
    # reconstructs white from patch reads; this is the LLM-facing summary.
    if runlog is not None:
        runlog.emit(
            "INFO" if not needs_adjudication else "WARN",
            "measure_loop", "completed", tier="digest",
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
    composited so a DWM-hook 3D LUT still applies — correct for a composited verify / the
    legacy-8-bit SDR path; ``fullscreen=True`` borderless-fullscreens it (avoids mini-LED
    local-dimming contamination; HDR/ACM-SDR composite in FP16, so bit depth is already
    preserved — fullscreen only *buys* 10-bit on a legacy-8-bit SDR desktop). Placement is
    best-effort and never blocks a spawn."""

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
        # settle_bump_s: per-presentation extra dwell from the loop's luminance-jump
        # settle bump (FALD zone decay/glow after a sharp drop) — 0.0 normally.
        self.display.send(proc, f"window {self.patch_size} {r} {g} {b}",
                          settle_seconds=self.settle_seconds + patch.settle_bump_s)

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
        # per-presentation extra dwell from the loop's luminance-jump settle bump
        dwell = self.settle_seconds + patch.settle_bump_s
        if dwell:
            time.sleep(dwell)

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
# by white_nits gives an absolute white at signal (1,1,1). One canonical copy
# (Phase 1 audit): metrics.py owns the literal.
_SRGB_TO_XYZ_D65 = SRGB_TO_XYZ_D65

# Rec.2020 primaries → XYZ at D65, white Y normalized to 1.0 (canonical NPM; matches
# colour's ITU-R BT.2020 to ~1e-7). The HDR synthetic panel emits through these — the
# wide native gamut the PQ/Rec.2020 target aims at. PQ linear light is already absolute
# nits (from the ST.2084 EOTF), so unlike sRGB no extra white_nits scaling is applied.
_REC2020_TO_XYZ_D65 = (
    (0.6369580, 0.1446169, 0.1688810),
    (0.2627002, 0.6779981, 0.0593017),
    (0.0000000, 0.0280727, 1.0609851),
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

    **Transfer-aware.** With a ``power`` transfer it is the SDR sRGB/γ-power panel
    (the original behaviour, unchanged). With a ``pq`` transfer it models an HDR
    panel: the PQ (ST.2084) EOTF decodes each channel's signal to **absolute nits**,
    emitted through **Rec.2020** primaries; ``native_white_nits`` clips the panel's
    physical peak and ``eotf_undershoot`` makes it render a fixed fraction under the
    PQ reference (the calibratable gain the HDR consumer + cube correct). A perfect
    HDR panel (undershoot 0, warm) reads the PQ/Rec.2020 ideal — the analogue of the
    perfect SDR panel the orchestrator tests use for a clean wiring run.

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
        native_white_nits: Optional[float] = None,
        eotf_undershoot: float = 0.0,
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
        # HDR-only knobs (ignored for a power transfer): the panel's physical peak
        # (clips PQ above it) and its EOTF undershoot → measured = (1+undershoot)×PQ.
        self.native_white_nits = native_white_nits
        self.eotf_undershoot = eotf_undershoot
        self.eotf_gain = 1.0 + float(eotf_undershoot)

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

        if self.transfer.kind == "pq":
            x, y, z = self._read_pq_xyz(r, g, b, blue_gain)
        else:
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

    def _read_pq_xyz(self, r: float, g: float, b: float,
                     blue_gain: float) -> tuple[float, float, float]:
        """HDR read: PQ (ST.2084) EOTF → absolute per-channel nits through Rec.2020
        primaries. The shared :class:`Transfer` math decodes the signal (``cv_to_nits``
        on the code value), so this matches the engine's PQ ideal; the panel then clips
        at its physical peak (``native_white_nits``) and under-renders by ``eotf_gain``
        (= 1 + ``eotf_undershoot``) — the calibratable deficit. Blue carries the same
        thermal gain as the SDR path (the temperamental channel)."""
        max_cv = self.transfer.max_cv

        def lin(signal: float) -> float:
            nits = self.transfer.cv_to_nits(max(0.0, signal) * max_cv)
            if self.native_white_nits is not None:
                nits = min(nits, self.native_white_nits)   # the panel can't exceed its peak
            return nits * self.eotf_gain                    # under-render vs the PQ reference

        lr, lg, lb = lin(r), lin(g), lin(b) * blue_gain
        m = _REC2020_TO_XYZ_D65
        x = m[0][0] * lr + m[0][1] * lg + m[0][2] * lb
        y = m[1][0] * lr + m[1][1] * lg + m[1][2] * lb
        z = m[2][0] * lr + m[2][1] * lg + m[2][2] * lb
        return x, y, z
