"""The PA32UCXR's MEASURED temporal law, as a forward simulator (phone camera 2026-09-19/20, work guide idea board T5).

This is the PANEL side only — what the monitor does over time with the frames it is sent. It is not the shader's
drive filter (:mod:`dlc.fald.temporal`, a first-order per-frame filter the measurement rejected); it exists so
temporal corrections can be designed and scored offline against the law the camera found:

* the local-dimming engine updates every SECOND 60-Hz frame (30 Hz). Software cannot know which one: ``parity``;
* a tick uses content that is at least ``latency_frames`` old, so the first LED step shows 1 or 2 frames after the
  LCD data (camera: 18 or 35 ms);
* at a tick every zone closes ``closure`` of the remaining gap to its target drive, the same up and down
  (black-card control: 0.63 / 0.87 / 0.95 from LEDs-off, 0.78 / 0.92 / 1.00 in the lit regime; no overshoot);
* the panel's own LCD compensation (its backlight estimate) follows the LED state ``est_lag_frames`` later (one
  frame) — for that frame unchanged pixels are shown through the OLD LCD opening under the NEW backlight: the
  1–2-frame flash (rise) / dip (fall) on constant grey beside a changing object;
* no scene-cut bypass, no dependence on step size, start level or area.

The LCD itself is treated as instant (its 12–30 ms transitions are below the frame grid used here).

The shader's form of the law (temporal mode 3 "panel clock", work guide C13; ``src/fald.cpp`` ``FaldPanelClockFactors``,
``src/fald_shader.h`` ``g_faldPanelClockSource``; GPU-order twin :class:`dlc.fald.gpuemu.GpuPanelDriveState`): a frame
stays on the panel for k >= 1 REFRESHES (Desktop Duplication delivers frames on change only, 24-fps video holds a frame
2–3 refreshes), and k refreshes with the same target collapse into one blend — :func:`clock_ticks` /
:func:`blend_factors`; ``refreshes=k`` on :meth:`PanelClock.step` / :meth:`PanelDriveState.commit` is the same thing
as k single steps.
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
MAX_REFRESHES = 64              # more elapsed refreshes than this between two frames: the state is reset (C++ FALD_CLOCK_MAX_REFRESHES)
SETTLE_RESIDUAL = 0.005         # the settle hold ends when this share of a step is left


@dataclass(frozen=True)
class PanelTimeLaw:
    tick_frames: int = 2        # the dimming engine runs every n-th panel frame (60 Hz panel -> 30 Hz)
    closure: float = 0.72       # share of the remaining drive gap closed per tick (measured 0.63–0.78)
    latency_frames: int = 1     # a tick sees content at least this many frames old
    est_lag_frames: int = 1     # the LCD compensation follows the LED state this many frames later


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
    — t ticks toward one target are one blend."""
    ts, tp = clock_ticks(n_a, k, parity, tick_frames)
    return 1.0 - (1.0 - closure) ** ts, 1.0 - (1.0 - closure) ** tp


def settle_refreshes(closure: float) -> int:
    """Refreshes the layer keeps re-rendering after the last content change: m = ceil(ln(residual) / ln(1 − closure))
    ticks (at least one) leave ``SETTLE_RESIDUAL`` of a step, the m-th tick is at most 2 m refreshes away, the
    compensation follows one later: 2 m + 2 (closure 0.72 -> 12). Mirrors C++ ``FaldPanelClockSettleFrames``."""
    c = min(max(float(closure), CLOSURE_MIN), CLOSURE_MAX)
    m = 1 if c >= 1.0 else max(1, int(math.ceil(math.log(SETTLE_RESIDUAL) / math.log(1.0 - c) - 1e-9)))
    return 2 * m + 2


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
        FIRST refresh — what :meth:`peek` announced and a correction's fields describe."""
        if int(refreshes) < 1:
            raise ValueError(f"refreshes must be >= 1, got {refreshes!r}")
        law = self.law
        d = np.asarray(drives, dtype=np.float64)
        first = None
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
