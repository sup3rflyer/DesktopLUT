"""Temporal drive state of the FALD layer — the reference for the shader's per-cell LED-law filter
(``src/fald_shader.h`` pass 1b ``g_faldTemporalSource``, ``src/fald.cpp`` ``RunTemporal``; work guide H5 / item 4a).

STATUS 2026-09-20 — LEGACY MODES; THE TEXT BELOW IS THE 2026-09-15/17 DESIGN RECORD, NOT THE PANEL'S LAW.
The PA32UCXR's LED time law was MEASURED on 2026-09-19/20 (phone camera, 120-fps Pro Video LOG, black-card control;
work guide "IDEA BOARD 2026-09-19" findings, results/phone_camera_2026-09-19/led_law/ and
results/phone_camera_2026-09-20/{card,parity_lock}/): the dimming engine is a sample-and-hold that ticks at
REFRESH ÷ 2 (30 Hz at 60 Hz, 24 Hz at 48 Hz); the first LED step shows 1–2 refreshes after the LCD data
(≈ 18–21 / 34–39 ms at 60 Hz, the two tick-parity classes); at each tick every zone closes ≈ 0.72 of the remaining
gap (0.63–0.78 measured with the card), the same up and down, no overshoot, no scene-cut bypass; and the panel's own
LCD compensation follows the LED state ONE refresh later — so the panel DOES flash natively at every hand-off
(1–2 frames, +72…+115 % on unchanged grey beside a 5 → 1000-nit block). The first-order filter of this module
(modes 1 / 2, any τ, incl. the owner's 100 / 100 ms) is REJECTED as the law: mode 1 has no compensation lag, mode 2 has
the lag with the OPPOSITE sign (here B_est leads B_true; on the panel B_est is one refresh BEHIND). The measured law
is temporal mode 3 "panel clock": :mod:`dlc.fald.paneltime` (``MODE_PANEL`` = 3 — NOT in ``MODE_NAMES`` below, which
lists this module's modes only), C++ ``FALD_TEMPORAL_PANEL``. The tick parity is locked to the actually presented
frame index and re-rolls on display mode sets; the software does not know it today (paneltime's ``parity`` −1).
Modes 1 / 2 stay in the shader (default off) as the owner's A/B controls and are pinned by the tests; statements
below that the measurement overtook are tagged [SUPERSEDED].

The stateless layer recomputes every cell's drive from the frame it is given and switches the whole
correction in the frame a cell's statistic crosses its threshold: a bright edge panning into a cell
(A0 ≈ 433 px² → saturated after ~10 px of travel) moves the gain pattern a whole cell in a few frames
(owner's phone video, 2026-09-15). [SUPERSEDED: 433 px² is the SDR refit's A0 — the shipped HDR fit has 1150 px²
(~26 px of travel) — and this area-weighted hand-over is the MODEL's statistic, not the panel: the measured border
behaviour is the LEVEL law (one pixel column inside a zone → ~full drive, ×1.43–1.47 while straddling;
results/phone_camera_2026-09-20/border/border_summary.json).] This module adds the one thing the shader has no notion of: TIME.
Each cell carries a drive STATE that follows the instantaneous drive with a first-order response,

    s' = s + a · (d − s),   a = a_rise when d > s else a_fall,   a = 1 − exp(−dt / τ)

and the kernels see the state instead of the instantaneous drive. An optional PIPELINE DELAY
(``delay_frames``, 0..3) feeds the filter the instantaneous drives of ``n`` frames ago instead of this
frame's (a link/firmware latency between the LCD data and the LED driver; the 2026-09-15 pan simulation
showed a 1-frame delay is the law the first-order filter cannot represent: stateless 5.6 % vs matched
delay 1.1 %, and no τ helps). Two hypotheses about the panel (``mode``):

* ``MODE_BOTH`` (1): the LEDs AND the panel's own estimate follow the filtered drive — self-consistent
  firmware (the panel filters its drive map, then computes its LCD compensation from the filtered map).
  Both fields come from the state.
* ``MODE_TRUE_ONLY`` (2): the LEDs lag physically but the LCD compensation follows the commanded drive —
  B_true from the state, B_est from the instantaneous drive. During a rise the shader then brightens the
  region for the few frames the LEDs are still catching up (and darkens on a fall). Design review
  2026-09-17: physically the less likely shape (firmware that smooths its drive map has the smoothed map in
  hand when it computes the LCD opening → mode 1; a lag AFTER the compensation stage is a delay, not a
  filter) and a panel of this kind would flash natively at every handoff. [SUPERSEDED 2026-09-20: this argument is
  REFUTED by measurement — the PA32UCXR DOES flash natively at every hand-off (its LCD compensation follows the LED
  step one refresh later; status block above). Mode 2 is still not the law: its B_est LEADS B_true, the panel's
  lags it.] Keep it as a diagnostic, not a
  candidate default. It also carries a rest bias: on a static frame round 0 filters the raw frame's drives
  while B_est uses them unfiltered, so the settled state is inconsistent by (1 − a)(d0 − s) (worse than the
  stateless layer, growing with τ) — mode 1's rest error shrinks with a instead.

``MODE_OFF`` (0) is the stateless layer, bit for bit. τ = 0 on an edge means "instant on that edge"
(a = 1); mode 1 with both τ = 0 and no delay equals mode 0.

Per frame, both rounds of the inverse (``correct_image`` iters) see the filter FROM THE COMMITTED STATE
(``peek``: the state is not advanced by round 0), and the state commits once, on the last round's
instantaneous drives (``correct_sequence``; the shader copies its round-1 filtered map into the state
texture). Offline evidence (results/fald_temporal_2026-09-15/pan_sim.py, "matched"): when the panel's
LEDs lag by τ = 3–10 frames, the stateless layer makes per-frame jumps WORSE than the native halo
(p95 3.6–5.0 % vs 1.1–2.2 %) while a matched filter brings them to 0.2–0.4 %. Whether the PA32UCXR
lags, and with which τ, is UNMEASURED: the control ships default-off; :func:`fit_step_response` fits τ
from a phone video of the test clip's toggle segment. [SUPERSEDED 2026-09-20: MEASURED — not a first-order lag at
all but the refresh ÷ 2 sample-and-hold of the status block above (closure ≈ 0.72 per tick, compensation one refresh
late); no τ fits it. The control still ships default-off; :func:`fit_step_response` is kept as a tool, its
first-order model is not the panel's.]

A static desktop delivers no frames (Desktop Duplication): the render loop must keep re-running the layer
until the state has settled — :func:`settle_frames` = 5 τ_max in frames (e⁻⁵ = 0.7 % of a step) plus the
delay. Those settle frames re-run only the FALD passes on the layer's own intermediate (HW 2026-09-17: re-reading
the released Desktop Duplication texture every frame flickered black). At rest, mode 1 keeps iterating the
inverse one round per frame and its output equals the HW-validated stateless output to ≤ 0.1 % on the fitted
PA32UCXR model (design review 2026-09-17, `rest_state.py`; an earlier "more accurate at rest" claim came from an
unfitted test model and is withdrawn).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

MODE_OFF = 0
MODE_BOTH = 1          # LEDs + panel estimate follow the filtered drive
MODE_TRUE_ONLY = 2     # LEDs lag; the panel's LCD compensation follows the commanded (instantaneous) drive
MODE_NAMES = {MODE_OFF: "off", MODE_BOTH: "both", MODE_TRUE_ONLY: "true_only"}
MODE_CODES = {v: k for k, v in MODE_NAMES.items()}

TAU_MAX_MS = 5000.0    # the C++ setters clamp here too (settings.cpp / DoFaldTemporal)
DELAY_MAX_FRAMES = 3   # pipeline delay ring (C++ FALD_DELAY_MAX)
SETTLE_TAUS = 5.0      # settle hold after the last content frame, in time constants


def alpha_from_tau(tau_ms: float, dt_ms: float) -> float:
    """Per-frame blend factor of a first-order response with time constant ``tau_ms`` sampled every ``dt_ms``.
    τ ≤ 0 (or dt ≤ 0) = instant (1). Mirrors C++ ``FaldTemporalAlpha`` (float32 there)."""
    if not (tau_ms > 0.0) or not (dt_ms > 0.0):   # also NaN -> instant, as the C++ `!(tau > 0)`
        return 1.0
    return 1.0 - math.exp(-dt_ms / tau_ms)


def settle_frames(tau_rise_ms: float, tau_fall_ms: float, dt_ms: float, delay_frames: int = 0) -> int:
    """Frames the layer must keep re-rendering after the last content change so the state settles
    (5 τ_max, ceil, plus the pipeline delay). 0 when neither edge has a time constant and there is no delay.
    Mirrors C++ ``FaldSettleFrames``."""
    tau = max(tau_rise_ms, tau_fall_ms, 0.0)
    delay = int(max(0, min(DELAY_MAX_FRAMES, int(delay_frames))))
    if (not (tau > 0.0) or not (dt_ms > 0.0)):
        return delay
    return max(1, int(math.ceil(SETTLE_TAUS * tau / dt_ms - 1e-4))) + delay   # the tolerance keeps exact multiples exact (C++: float32 dt)


class DriveState:
    """Per-cell drive state (rows × cols) with rise/fall time constants. ``peek`` = the filtered map the
    kernels would see for instantaneous drives ``d`` without advancing; ``fields`` = the (drives_true,
    drives_est) pair for :func:`dlc.fald.correct.correct_image`'s ``drive_filter``; ``commit`` advances."""

    def __init__(self, mode: int = MODE_OFF, tau_rise_ms: float = 0.0, tau_fall_ms: float = 0.0,
                 dt_ms: float = 1000.0 / 60.0, delay_frames: int = 0):
        if mode not in MODE_NAMES:
            raise ValueError(f"temporal mode must be one of {sorted(MODE_NAMES)}, got {mode!r}")
        if not (0 <= int(delay_frames) <= DELAY_MAX_FRAMES):
            raise ValueError(f"delay_frames must be 0..{DELAY_MAX_FRAMES}, got {delay_frames!r}")
        self.mode = int(mode)
        self.tau_rise_ms = float(tau_rise_ms)
        self.tau_fall_ms = float(tau_fall_ms)
        self.dt_ms = float(dt_ms)
        self.delay_frames = int(delay_frames)
        self.alpha_rise = alpha_from_tau(self.tau_rise_ms, self.dt_ms)
        self.alpha_fall = alpha_from_tau(self.tau_fall_ms, self.dt_ms)
        self.state: Optional[np.ndarray] = None
        self.hist: list[np.ndarray] = []     # committed instantaneous drives, oldest first (the delay ring)
        self.frames = 0

    @property
    def active(self) -> bool:
        return self.mode != MODE_OFF

    def reset(self) -> None:
        """Forget the state: the next frame initialises it from its own drives (the C++ does this at Build,
        on a mode change and whenever the filter was off)."""
        self.state = None
        self.hist = []
        self.frames = 0

    def delayed(self, drives: np.ndarray) -> np.ndarray:
        """The drive map the filter is fed: this frame's, or the committed one ``delay_frames`` frames ago
        (``hist[-n]``; the ring is filled by ``commit``). Until the ring holds ``n`` maps the current one is used
        (the shader's ring behaves the same: no valid entry -> the instantaneous drive)."""
        d = np.asarray(drives, dtype=float)
        n = self.delay_frames
        if not self.active or n <= 0 or len(self.hist) < n:
            return d
        return self.hist[-n]

    def peek(self, drives: np.ndarray) -> np.ndarray:
        d = self.delayed(drives)
        if not self.active or self.state is None:
            return d
        a = np.where(d > self.state, self.alpha_rise, self.alpha_fall)
        return self.state + a * (d - self.state)

    def fields(self, drives: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        d = np.asarray(drives, dtype=float)
        f = self.peek(d)
        if self.mode == MODE_TRUE_ONLY:
            return f, d
        return f, f

    def commit(self, drives: np.ndarray) -> np.ndarray:
        """Advance on this frame's last-round instantaneous drives; returns the committed state. The delay ring
        receives the INSTANTANEOUS map (what the panel's pipeline received), the state the filtered one."""
        out = self.peek(drives)
        if self.active:
            self.state = np.array(out, dtype=float, copy=True)
            if self.delay_frames > 0:
                self.hist.append(np.array(drives, dtype=float, copy=True))
                del self.hist[:-self.delay_frames]
        self.frames += 1
        return out

    def settle_frames(self) -> int:
        return settle_frames(self.tau_rise_ms, self.tau_fall_ms, self.dt_ms, self.delay_frames) if self.active else 0


def correct_sequence(model, frames: Sequence[np.ndarray], state: DriveState, iters: int = 2,
                     refreshes: Optional[Sequence[int]] = None) -> list[dict]:
    """Run :func:`dlc.fald.correct.correct_image` over consecutive frames with one drive state — exactly the
    shader's per-frame order (both rounds peek from the committed state, commit after the last round).
    Returns the per-frame result dicts (``req``, ``gain``, ``drives`` = last-round instantaneous drives, plus
    ``drives_state`` = the state after the commit). ``refreshes``: per frame, the panel refreshes it stays up (only
    for a state whose ``commit`` takes it: :class:`dlc.fald.paneltime.PanelDriveState`); None = one each."""
    from .correct import correct_image
    out = []
    for i, img in enumerate(frames):
        res = correct_image(model, img, iters=iters, drive_filter=state.fields)
        res["drives_state"] = state.commit(res["drives"]) if refreshes is None else state.commit(res["drives"], refreshes=int(refreshes[i]))
        out.append(res)
    return out


# ---------------------------------------------------------------------------------------------------
# Measuring the panel's own LED law (the number the defaults should come from)
# [2026-09-20: the law has been measured and is NOT first-order — see the module docstring's status block and
#  dlc/fald/paneltime.py. The capture used 120-fps Pro Video LOG (the phone's 240-fps mode has no manual control).
#  These first-order fit helpers are kept as tools only.]
# ---------------------------------------------------------------------------------------------------
def first_order_step(t: np.ndarray, tau: float, y0: float, y1: float, t0: float = 0.0) -> np.ndarray:
    """y(t) of a first-order step from y0 to y1 starting at t0 (y0 before t0)."""
    t = np.asarray(t, dtype=float)
    if tau <= 0.0:
        return np.where(t >= t0, y1, y0)
    return np.where(t >= t0, y1 + (y0 - y1) * np.exp(-(t - t0) / tau), y0)


def fit_step_response(t: np.ndarray, y: np.ndarray, taus: Optional[Sequence[float]] = None) -> dict:
    """Fit ``y(t)`` (a luminance trace of the halo region beside a block that switched at t = 0, e.g. from a
    240-fps phone video of the toggle segment of results/fald_temporal_2026-09-15/fald_temporal_clip.mp4)
    with a first-order step, scanning τ and a step time t0 on a grid (least squares in y0/y1 for each).
    Returns ``{tau, t0, y0, y1, rms, instant_rms}``; ``instant_rms`` is the residual of a pure step
    (τ = 0) so the caller can tell a lagging panel from an instant one (a τ that does not beat the
    instant fit by a clear margin is not evidence of lag)."""
    t = np.asarray(t, dtype=float); y = np.asarray(y, dtype=float)
    if t.ndim != 1 or t.shape != y.shape or t.size < 4:
        raise ValueError("t and y must be 1-D of equal length ≥ 4")
    span = float(t[-1] - t[0])
    if taus is None:
        taus = list(np.geomspace(max(span / 400.0, 1e-6), span / 2.0, 60))
    taus = [0.0] + [float(v) for v in taus if v > 0.0]          # the instant baseline is always evaluated
    # t0 candidates = the sample times of the first half (a pure step must be allowed to sit exactly on a sample,
    # else the instant baseline carries a full-height residual and a small tau "wins" by absorbing the misalignment)
    t0s = t[t <= t[0] + span * 0.5]
    best = None
    instant_rms = None
    for tau in taus:
        for t0 in t0s:
            # y = y0 + (y1 - y0) * g,  g = 1 - exp(-(t - t0)/tau) for t >= t0 else 0  → linear in (y0, y1)
            g = np.where(t >= t0, 1.0 - (np.exp(-(t - t0) / tau) if tau > 0 else 0.0), 0.0)
            A = np.stack([1.0 - g, g], axis=1)
            sol, *_ = np.linalg.lstsq(A, y, rcond=None)
            r = float(np.sqrt(np.mean((A @ sol - y) ** 2)))
            if tau == 0.0 and (instant_rms is None or r < instant_rms):
                instant_rms = r
            if best is None or r < best["rms"]:
                best = {"tau": float(tau), "t0": float(t0), "y0": float(sol[0]), "y1": float(sol[1]), "rms": r}
    best["instant_rms"] = float(instant_rms if instant_rms is not None else best["rms"])
    return best
