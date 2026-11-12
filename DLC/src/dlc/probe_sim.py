"""A synthetic HDR panel + meter for the probes (:mod:`dlc.peak_window`, :mod:`dlc.refresh_probe`), on a virtual clock.

The calibration's :class:`~dlc.measure_loop.SyntheticPanel` has no time and no window: it cannot show a power limiter
acting, a backlight sagging as it heats, or a panel that is brighter at another refresh rate. This wrapper is a
:data:`~dlc.measure_loop.MeasureFn` with exactly those behaviours, each one off unless asked for, so a test can build
an OLED-like panel, a mini-LED-like panel, a drifting one, or one whose meter dies:

* **limiter (ABL)** — :class:`Limiter`: below ``knee_pct`` of the screen at full white nothing is limited; above it
  the sustained light falls as ``(knee / load) ** exponent`` (load = window area x the window's light / peak). A new
  frame starts at the light it had when the limiter was last relaxed and decays to the sustained value with
  ``tau_s``; a black frame relaxes it with ``recover_tau_s``.
* **thermal sag** — :class:`ThermalSag`: a first-order temperature driven by the emitted power (``dT/dt = (load - T)
  / tau_s``) that takes ``sag`` x T off the light (a mini-LED backlight running hot, minutes).
* **drift** — a slow multiplicative drift of the whole panel over the run (``drift_per_hour``), the thing an up/down
  bracket must catch.
* **refresh response** — ``refresh_gain(hz, nits) -> factor``: the light at the rate the display runs at. The rate is
  read from ``hz_fn`` (a :class:`~dlc.refresh_mode.SimDisplayApi` snapshot, or a fake VRR presenter's cadence).
* **meter faults** — ``fail_after_reads`` (every read after the N-th fails: meter loss).

Every read advances the virtual clock by ``read_s``; the flows' own waits go through the same clock
(``probe_clock``), so a 20-minute probe runs in milliseconds and deterministically. Stdlib only.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from ._pq import eotf_norm
from .colormath import rgb_to_xyz_matrix
from .measure_loop import MeasurePatch, Reading

__all__ = ["VirtualClock", "RealClock", "Limiter", "ThermalSag", "ProbeSimPanel", "oled_like", "mini_led_like",
           "period_refresh_gain"]

REC2020 = (0.708, 0.292, 0.170, 0.797, 0.131, 0.046)
D65 = (0.3127, 0.3290)


class VirtualClock:
    """``now()`` / ``sleep(s)`` that only advance on demand (tests and ``--simulate``)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, float(s))


class RealClock:
    """The wall clock (monotonic) for a live run."""

    @staticmethod
    def now() -> float:
        import time
        return time.monotonic()

    @staticmethod
    def sleep(s: float) -> None:
        import time
        if s > 0:
            time.sleep(s)


@dataclass
class Limiter:
    knee_pct: float = 10.0          # window area (% of the screen, at full white) the panel holds unlimited
    exponent: float = 0.6           # how steeply the sustained light falls with the load past the knee
    tau_s: float = 1.5              # how fast the limiter pulls a new bright frame down to its sustained value
    recover_tau_s: float = 4.0      # how fast a black frame relaxes it again
    floor: float = 0.05             # the limiter never takes more than 95 % off

    def sustained_factor(self, load_pct: float) -> float:
        if load_pct <= self.knee_pct or load_pct <= 0:
            return 1.0
        return max(self.floor, (self.knee_pct / load_pct) ** self.exponent)


@dataclass
class ThermalSag:
    sag: float = 0.06               # light lost at full temperature (T = 1)
    tau_s: float = 600.0            # the backlight's thermal time constant
    full_load_pct: float = 100.0    # the load (% area x light / peak) that heats it to T = 1


def period_refresh_gain(effect_at_low: Callable[[float], float], *, low_hz: float = 48000 / 1001,
                        ref_hz: float = 60.0) -> Callable[[float, float], float]:
    """A refresh response linear in frame period: ``ln r = effect_at_low(nits) x (T - T_ref) / (T_low - T_ref)``."""
    def gain(hz: float, nits: float) -> float:
        x = (1000.0 / hz - 1000.0 / ref_hz) / (1000.0 / low_hz - 1000.0 / ref_hz)
        return math.exp(effect_at_low(nits) * x)
    return gain


class ProbeSimPanel:
    """The probes' synthetic panel (see the module docstring). A :data:`~dlc.measure_loop.MeasureFn`; carries
    ``probe_clock`` (the flows take their clock from the measure fn when it has one)."""

    def __init__(self, *, peak_nits: float = 1000.0, bit_depth: int = 10, clock: Optional[VirtualClock] = None,
                 read_s: float = 0.4, limiter: Optional[Limiter] = None, thermal: Optional[ThermalSag] = None,
                 drift_per_hour: float = 0.0, black_nits: float = 0.0005, noise_rel: float = 0.0,
                 refresh_gain: Optional[Callable[[float, float], float]] = None,
                 hz_fn: Optional[Callable[[], Optional[float]]] = None,
                 fail_after_reads: Optional[int] = None, seed: int = 5) -> None:
        self.peak_nits = float(peak_nits)
        self.max_code = (1 << int(bit_depth)) - 1
        self.probe_clock = clock or VirtualClock()
        self.read_s = float(read_s)
        self.limiter = limiter
        self.thermal = thermal
        self.drift_per_hour = float(drift_per_hour)
        self.black_nits = float(black_nits)
        self.noise_rel = float(noise_rel)
        self.refresh_gain = refresh_gain
        self.hz_fn = hz_fn
        self.fail_after_reads = fail_after_reads
        self.rng = random.Random(seed)
        self.M = rgb_to_xyz_matrix(*REC2020, *D65, white_Y=1.0)
        self.t0 = self.probe_clock.now()
        self.reads = 0
        self.frames: list[dict[str, Any]] = []      # every frame shown (what a test can assert on)
        self._frame: Any = None
        self._t_frame = self.t0
        self._f_start = 1.0             # the limiter factor when the current frame appeared
        self._f_now = 1.0
        self._temp = 0.0
        self._t_state = self.t0
        self._load = 0.0

    # ------------------------------------------------------------------ physics
    def _chan_nits(self, rgb: Sequence[int]) -> list[float]:
        return [min(self.peak_nits, 10000.0 * eotf_norm(max(0, int(c)) / self.max_code)) if c > 0 else 0.0
                for c in rgb]

    def _frame_load(self, rgb: Sequence[int], area: Optional[float]) -> float:
        a = 100.0 if area is None else min(100.0, float(area))
        return a * max(self._chan_nits(rgb)) / self.peak_nits

    def _limiter_factor(self, t_in_frame: float) -> float:
        lim = self.limiter
        if lim is None:
            return 1.0
        if self._load <= 0:
            return min(1.0, 1.0 - (1.0 - self._f_start) * math.exp(-t_in_frame / lim.recover_tau_s))
        f_sus = lim.sustained_factor(self._load)
        return f_sus + (self._f_start - f_sus) * math.exp(-t_in_frame / lim.tau_s)

    def _advance(self, now: float) -> None:
        """Integrate the thermal state over [t_state, now] at the current load (exact first-order step)."""
        th = self.thermal
        dt = now - self._t_state
        if th is not None and dt > 0:
            target = min(1.0, self._load / th.full_load_pct)
            self._temp = target + (self._temp - target) * math.exp(-dt / th.tau_s)
        self._t_state = now

    def _show(self, patch: MeasurePatch) -> None:
        frame = (tuple(patch.rgb), patch.window_area_pct)
        now = self.probe_clock.now()
        if frame == self._frame and not patch.repaint:
            return
        if frame == self._frame:
            return                      # a repaint of the same frame changes no light
        self._advance(now)
        self._f_now = self._limiter_factor(now - self._t_frame)
        self._f_start = self._f_now     # the limiter carries its state into the new frame
        self._frame = frame
        self._t_frame = now
        self._load = self._frame_load(patch.rgb, patch.window_area_pct)
        self.frames.append({"t": round(now - self.t0, 3), "rgb": list(patch.rgb), "area": patch.window_area_pct})

    def __call__(self, patch: MeasurePatch) -> Reading:
        self._show(patch)
        self.probe_clock.sleep(self.read_s)
        now = self.probe_clock.now()
        self.reads += 1
        if self.fail_after_reads is not None and self.reads > self.fail_after_reads:
            return Reading(xyz=None, ok=False, error="spotread exited with 1: instrument not found",
                           raw={"meter_fault": "exited"})
        self._advance(now)
        t_mid = now - self.read_s / 2.0 - self._t_frame
        f = self._limiter_factor(max(0.0, t_mid))
        if self.thermal is not None:
            f *= 1.0 - self.thermal.sag * self._temp
        f *= 1.0 + self.drift_per_hour * (now - self.t0) / 3600.0
        lin = self._chan_nits(patch.rgb)
        if self.refresh_gain is not None and self.hz_fn is not None:
            hz = self.hz_fn()
            if hz:
                lin = [v * self.refresh_gain(hz, v) if v > 0 else 0.0 for v in lin]
        lin = [v * f for v in lin]
        xyz = [sum(self.M[i][j] * lin[j] for j in range(3)) for i in range(3)]
        if sum(lin) <= 0:
            xyz = [self.black_nits * D65[0] / D65[1], self.black_nits, self.black_nits * (1 - sum(D65)) / D65[1]]
        if self.noise_rel:
            k = 1.0 + self.rng.gauss(0.0, self.noise_rel)
            xyz = [v * k for v in xyz]
        return Reading(xyz=(xyz[0], xyz[1], xyz[2]), ok=True, raw={"sim": True})


def oled_like(**kw: Any) -> ProbeSimPanel:
    """An OLED-like panel: ~1000 nit on small windows, a steep power limiter past ~10 % of the screen, fast."""
    kw.setdefault("limiter", Limiter(knee_pct=10.0, exponent=0.62, tau_s=1.2, recover_tau_s=3.0))
    kw.setdefault("peak_nits", 1000.0)
    return ProbeSimPanel(**kw)


def mini_led_like(**kw: Any) -> ProbeSimPanel:
    """A mini-LED-like panel: a flat curve with a mild full-field limit and a slow thermal sag."""
    kw.setdefault("limiter", Limiter(knee_pct=60.0, exponent=0.25, tau_s=3.0, recover_tau_s=6.0))
    kw.setdefault("thermal", ThermalSag(sag=0.06, tau_s=500.0))
    kw.setdefault("peak_nits", 1600.0)
    return ProbeSimPanel(**kw)
