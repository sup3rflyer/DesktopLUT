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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import numpy as np

from .model import FaldModel


@dataclass(frozen=True)
class PanelTimeLaw:
    tick_frames: int = 2        # the dimming engine runs every n-th panel frame (60 Hz panel -> 30 Hz)
    closure: float = 0.72       # share of the remaining drive gap closed per tick (measured 0.63–0.78)
    latency_frames: int = 1     # a tick sees content at least this many frames old
    est_lag_frames: int = 1     # the LCD compensation follows the LED state this many frames later


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

    def step(self, drives: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Feed the instantaneous zone drives of the frame now on the LCD; returns (LED state, the state the panel's
        compensation uses) for this frame. The first frame finds the panel settled on it."""
        law = self.law
        d = np.asarray(drives, dtype=np.float64)
        pk = self.peek()
        s = d.copy() if pk is None else pk[0]
        self._targets = (self._targets + [d])[-max(law.latency_frames, 1):]
        self._true = (self._true + [s])[-(law.est_lag_frames + 1):]
        self.n += 1
        return s, self._true[0]


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
