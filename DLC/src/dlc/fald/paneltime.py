"""The PA32UCXR's MEASURED temporal law, as a forward simulator (phone camera 2026-09-19/20, work guide idea board T5).

This is the PANEL side only — what the monitor does over time with the frames it is sent. It is not the shader's
drive filter (:mod:`dlc.fald.temporal`, a first-order per-frame filter the measurement rejected); it exists so
temporal corrections can be designed and scored offline against the law the camera found:

* the local-dimming engine updates every SECOND panel refresh — the tick is REFRESH ÷ 2 (30 Hz at 60 Hz, 24 Hz at
  48 Hz: measured at both, results/phone_camera_2026-09-20/parity_lock/summary.json), not a fixed 30 Hz. Which
  refresh (``parity``) the software does not know TODAY: the parity is locked to the actually PRESENTED frame index
  (48/48 on every run, stable over 13 min) and re-rolls on display mode sets, so using it needs the presented
  refresh index plus a one-bit calibration after every display event (stage 3, not built; default ``parity`` −1 =
  the mean of both clocks);
* a tick uses content that is at least ``latency_frames`` old, so the first LED step shows 1 or 2 frames after the
  LCD data (camera at 60 Hz: 18–21 or 34–39 ms, the two parity classes);
* at a tick every zone closes ``closure`` of the remaining gap to its target drive, the same up and down
  (black-card control: 0.63 / 0.87 / 0.95 from LEDs-off, 0.78 / 0.92 / 1.00 in the lit regime; no overshoot);
* the panel's own LCD compensation (its backlight estimate) follows the LED state ``est_lag_frames`` later (one
  frame) — for that frame unchanged pixels are shown through the OLD LCD opening under the NEW backlight: the
  1–2-frame flash (rise) / dip (fall) on constant grey beside a changing object;
* no scene-cut bypass, no dependence on step size, start level or area.

The LCD itself is treated as instant (a simplification: its 12–30 ms transitions span 1–2 frames of the grid used
here and are NOT modelled — the camera's flash spike is ~1.5 frames wide for that reason).

The shader's form of the law (temporal mode 3 "panel clock", work guide C13; ``src/fald.cpp`` ``FaldPanelClockFactors``,
``src/fald_shader.h`` ``g_faldPanelClockSource``; GPU-order twin :class:`dlc.fald.gpuemu.GpuPanelDriveState`): a frame
stays on the panel for k REFRESHES (Desktop Duplication delivers frames on change only, 24-fps video holds a frame
2–3 refreshes), and k refreshes with the same target collapse into one blend — :func:`clock_ticks` /
:func:`blend_factors`; ``refreshes=k`` on :meth:`PanelClock.step` / :meth:`PanelDriveState.commit` is the same thing
as k single steps. k comes from a refresh grid PHASE-LOCKED to the runs (:class:`RefreshGrid`, the twin of C++
``FaldPanelClockStep``: no drift although the real period is off the nominal one), and k = 0 is real — the render loop
may run more than once per refresh of this monitor (a faster display elsewhere on the desktop): a frame replaced inside
its refresh never reached the panel (``refreshes=0`` records nothing). A long static pause is no reset: the reference simply
steps through it; the shader's form takes the blends as exactly 1 beyond ``MAX_REFRESHES``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import math

import numpy as np

from .model import FaldModel

MODE_PANEL = 3                  # FaldSettings::temporalMode / runtime.fald_temporal temporal_mode (C++ FALD_TEMPORAL_PANEL)
CLOSURE_DEFAULT = 0.72          # C++ FALD_CLOCK_CLOSURE_DEFAULT
CLOSURE_MIN, CLOSURE_MAX = 0.05, 1.0   # the range the settings / pipe / CB enforce (C++ FALD_CLOCK_CLOSURE_MIN / _MAX)
MAX_REFRESHES = 64              # more elapsed refreshes than this between two frames: the panel has settled on the earlier
                                # one, the shader's blends are exactly 1 — NOT a reset (C++ FALD_CLOCK_MAX_REFRESHES)
SETTLE_RESIDUAL = 0.0005        # the settle hold ends when this share of a DRIVE step is left (0.005 left the output moving
                                # ~1.4 % per frame after a 5 -> 1842-nit step: the round-1 drives feed back through the correction)
SETTLE_MAX = 120                # cap of the settle hold in refreshes (C++ FALD_CLOCK_SETTLE_MAX): a low closure must not
                                # re-render a static desktop for seconds
LOCK_GAIN = 0.08                # steady gain of the refresh grid's phase lock, per run (C++ FALD_CLOCK_LOCK_GAIN)


@dataclass(frozen=True)
class PanelTimeLaw:
    tick_frames: int = 2        # the dimming engine runs every n-th panel frame: refresh ÷ 2 (30 Hz at 60, 24 Hz at 48)
    closure: float = 0.72       # share of the remaining drive gap closed per tick (measured 0.63–0.78)
    latency_frames: int = 1     # a tick sees content at least this many frames old
    est_lag_frames: int = 1     # the LCD compensation follows the LED state this many frames later


class RefreshGrid:
    """Elapsed panel refreshes k per run from the run TIMES — the twin of the time law in C++ ``FaldPanelClockStep``
    (``src/fald.h`` above ``FALD_TEMPORAL_PANEL``). The grid has the NOMINAL period and a centre time ``grid_ms`` of the
    last run's refresh; a run at ``t`` gives x = (t − grid) / period, k = round(x) (never negative), residual r = x − k,
    and the grid follows the runs: grid += (k + a r) period with a = 1 / (runs since the seed + 1), not below
    ``LOCK_GAIN``. The dominant run phase so sits at the grid centre, half a period from the rounding boundary — a
    period error of 1000 ppm leaves a lag of about 1 % of a period instead of a grid that drifts onto the boundary and
    dithers k = 0 / 2; two run populations half a period apart (a render loop twice as fast as this monitor) lock to
    ∓¼: a stable 1 / 0 alternation. A pause of more than ``MAX_REFRESHES`` re-opens the acquisition gain.

    :meth:`step` returns ``(seeded, k)``: ``seeded`` = the run (re-)seeds the state — the first run, a run still inside
    the seeding refresh, time more than two periods backwards."""

    def __init__(self, period_ms: float):
        self.period = float(period_ms)
        self.reset()

    def reset(self) -> None:
        self.valid = False
        self.grid_ms = self.residual = self.gain = 0.0
        self.runs = self.n = 0

    def step(self, t_ms: float) -> tuple[bool, int]:
        x = (float(t_ms) - self.grid_ms) / self.period if (self.valid and self.period > 0.0) else 0.0
        if not self.valid or not (self.period > 0.0) or x < -2.0:
            self.reset()
            self.valid, self.grid_ms = self.period > 0.0, float(t_ms)
            return True, 0
        k = max(int(math.floor(x + 0.5)), 0)
        r = min(max(x - k, -0.5), 0.5)
        self.runs = min(self.runs + 1, 1000000)
        a = max(1.0 / (self.runs + 1.0), LOCK_GAIN)
        if k > MAX_REFRESHES:
            self.runs = 0
        self.grid_ms += (k + a * r) * self.period
        self.residual, self.gain = r, a
        self.n += k
        return (self.n == 0), (0 if self.n == 0 else k)


def clock_ticks(n_a: int, k: int, parity: int, tick_frames: int = 2) -> tuple[int, int]:
    """Ticks of the clock with this ``parity`` (it ticks at refresh n when ``(n + parity) % tick_frames == 0``) between
    a frame first shown at refresh ``n_a`` and the next one at ``n_b = n_a + k``: ``(tS, tP)`` = ticks in
    ``n_a+1 … n_b`` (they make the LED state OF refresh n_b) and in ``n_a+1 … n_b−1`` (the state one refresh earlier,
    which the panel's compensation uses at n_b). Every one of them targets the drives of the frame shown at n_a.
    Mirrors C++ ``FaldPanelClockTicks``."""
    if k < 1 or n_a < 0:
        raise ValueError("k >= 1 and n_a >= 0")
    upto = lambda n: (n + parity) // tick_frames          # ticks at refreshes <= n (up to a constant)
    return upto(n_a + k) - upto(n_a), upto(n_a + k - 1) - upto(n_a)


def blend_factors(n_a: int, k: int, closure: float, parity: int, tick_frames: int = 2) -> tuple[float, float]:
    """``(aS, aP)`` with ``a = 1 − (1 − closure)^ticks``: S(n_b) = S + aS (d_prev − S), S(n_b − 1) = S + aP (d_prev − S)
    — t ticks toward one target are one blend. Beyond ``MAX_REFRESHES`` the shader's form takes both as exactly 1."""
    if k > MAX_REFRESHES:
        return 1.0, 1.0
    ts, tp = clock_ticks(n_a, k, parity, tick_frames)
    return 1.0 - (1.0 - closure) ** ts, 1.0 - (1.0 - closure) ** tp


def settle_refreshes(closure: float) -> int:
    """ELAPSED REFRESHES (not runs) the layer keeps re-rendering after the last content change: m =
    ceil(ln(residual) / ln(1 − closure)) ticks (at least one) leave ``SETTLE_RESIDUAL`` of a step, the m-th tick is at
    most 2 m refreshes away, the compensation follows one later: 2 m + 2 (closure 0.72 -> 14), capped at ``SETTLE_MAX``.
    Mirrors C++ ``FaldPanelClockSettleFrames``."""
    c = min(max(float(closure), CLOSURE_MIN), CLOSURE_MAX)
    m = 1 if c >= 1.0 else max(1, int(math.ceil(math.log(SETTLE_RESIDUAL) / math.log(1.0 - c) - 1e-9)))
    return min(2 * m + 2, SETTLE_MAX)


class PanelClock:
    """Per-zone LED drive state of the panel over consecutive 60-Hz frames."""

    def __init__(self, law: PanelTimeLaw = PanelTimeLaw(), parity: int = 0):
        self.law, self.parity = law, int(parity)
        self.n = 0
        self._targets: list[np.ndarray] = []        # instantaneous drive maps of the last frames (newest last)
        self._true: list[np.ndarray] = []           # LED state of the last frames (newest last)

    def peek(self) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """(LED state, compensation state) of the UPCOMING frame — it depends on past frames only (``latency_frames``
        ≥ 1), so a correction can read it before it decides what to send. None before the first frame."""
        law = self.law
        assert law.latency_frames >= 1, "peek needs a causal law"
        if not self._true:
            return None
        s = self._true[-1]
        if (self.n + self.parity) % law.tick_frames == 0:
            s = s + law.closure * (self._targets[-law.latency_frames] - s)
        return s, (self._true + [s])[-(law.est_lag_frames + 1):][0]

    def step(self, drives: np.ndarray, refreshes: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """Feed the instantaneous zone drives of the frame now on the LCD; returns (LED state, the state the panel's
        compensation uses) for this frame. The first frame finds the panel settled on it. ``refreshes`` = k: the frame
        stays on the panel for k refreshes (k single steps with the same drives); the returned pair is the one of its
        FIRST refresh — what :meth:`peek` announced and a correction's fields describe. k = 0: the frame was replaced
        inside its refresh and never reached the panel — nothing is recorded (the pair is still the announced one)."""
        if int(refreshes) < 0:
            raise ValueError(f"refreshes must be >= 0, got {refreshes!r}")
        law = self.law
        d = np.asarray(drives, dtype=np.float64)
        first = None
        if int(refreshes) == 0:
            pk = self.peek()
            return (d.copy(), d.copy()) if pk is None else pk
        for _ in range(int(refreshes)):
            pk = self.peek()
            s = d.copy() if pk is None else pk[0]
            self._targets = (self._targets + [d])[-max(law.latency_frames, 1):]
            self._true = (self._true + [s])[-(law.est_lag_frames + 1):]
            self.n += 1
            if first is None:
                first = (s, self._true[0])
        return first


def simulate(model: FaldModel, frames: Sequence[np.ndarray], law: PanelTimeLaw = PanelTimeLaw(), parity: int = 0,
             sent: Optional[Sequence[np.ndarray]] = None,
             drives_of: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> list[dict]:
    """Displayed luminance of consecutive frames. ``frames`` = what the content wants (as-if-white nits, (3, h, w));
    ``sent`` = what is actually sent to the panel (a correction's output; default = ``frames``, the native panel).
    ``drives_of`` overrides the zone statistic (default ``model.cell_drives``). Returns per frame ``y`` (3, h, w),
    ``b_true``, ``b_est``, ``s_true``, ``s_est``."""
    p = model.p
    clock = PanelClock(law, parity)
    w = np.array(p.chan_weights)[:, None, None]
    lmax = p.white_nits * w
    out = []
    for i, want in enumerate(frames):
        img = want if sent is None else sent[i]
        d = (drives_of or model.cell_drives)(img)
        s_true, s_est = clock.step(d)
        b_true, _ = model.backlights(s_true, boost=model.led_boost(img))
        _, b_est = model.backlights(s_est)
        t = np.clip(img * w / np.maximum(lmax * np.maximum(b_est, 1e-6)[None], 1e-9), 0.0, 1.0)
        y = lmax * b_true[None] * (t + p.tmin_vec()[:, None, None])
        out.append({"y": y, "b_true": b_true, "b_est": b_est, "s_true": s_true, "s_est": s_est, "t": t})
    return out


class PanelDriveState:
    """The correction-side twin of :class:`PanelClock` — a drop-in for :class:`dlc.fald.temporal.DriveState` in
    :func:`dlc.fald.correct.correct_image` (``drive_filter=state.fields``) and :func:`dlc.fald.temporal.correct_sequence`.

    ``parity`` 0 / 1: the tick parity is known (one clock, exact). ``None``: unknown — both clocks run and the kernels see
    the MEAN of the two LED states and of the two compensation states. The backlight fields are linear in the drives, so
    this costs no extra kernel pass, and the gain it yields is the arithmetic-mean stand-in for the geometric mean of the
    two hypotheses' gains (B_est / mean(B_true) vs B_est / sqrt(B_true0 · B_true1): 6 % apart for a x2 step)."""

    def __init__(self, law: PanelTimeLaw = PanelTimeLaw(), parity: Optional[int] = None):
        self.law, self.parity = law, parity
        self.clocks = [PanelClock(law, p) for p in ((0, 1) if parity is None else (int(parity),))]

    @property
    def active(self) -> bool:
        return True

    def fields(self, drives: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(drives for B_true, drives for B_est) of the frame being corrected. Causal: the states of this frame depend on
        the frames already sent, never on ``drives`` — except the very first frame, which finds the panel settled on it."""
        pk = [c.peek() for c in self.clocks]
        if pk[0] is None:
            d = np.asarray(drives, dtype=np.float64)
            return d, d
        return sum(p[0] for p in pk) / len(pk), sum(p[1] for p in pk) / len(pk)

    def commit(self, drives: np.ndarray, refreshes: int = 1) -> np.ndarray:
        """Advance by the frame that was actually sent (its instantaneous zone drives), on the panel for ``refreshes``
        refreshes until the next one. Returns the mean LED state (of its first refresh)."""
        return sum(c.step(drives, refreshes)[0] for c in self.clocks) / len(self.clocks)

    def settle_frames(self) -> int:
        return settle_refreshes(self.law.closure)
