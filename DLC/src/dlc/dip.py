"""Persistent per-display **Display+Instrument Profile** (DIP) store.

A DIP is the product of the ``characterize`` flow: a *measured model of how this
panel and this meter behave together* — NOT a correction. It exists to replace the
measure loop's ungrounded constants (``confirm_reads``, ``max_repeats``,
``settle_seconds``, ``repeat_threshold``, ``neutral_interval``, ``drift_threshold``)
with values the hardware actually demonstrated, and to seed an intelligent per-patch
read policy: a single adaptive-integration read by default (as professional tools do),
escalating to more reads only where the *measured* noise model says SNR needs it or an
outlier vs expectation needs confirming — never a fixed count, never a silent cap.

Three axes (see ``stage_characterize``):
  * **instrument** — read-to-read repeatability σ as a function of luminance
    (``noise_model``), the noise floor, per-read overhead.
  * **display** — step-response settle time, native white/black/primaries.
  * **drift** — warm-up reads-to-settle, the discovered cold/temperamental channel,
    post-warm creep rate → recommended ``neutral_interval`` / ``drift_threshold``.

Persisted per-display like :mod:`dlc.correction_store` (a JSON file keyed by display
name, with a ``made`` date for staleness). Local-only / private. Dependency-free
(stdlib only) so importing it is free.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .paths import atomic_write_text

__all__ = ["NoiseBand", "DisplayInstrumentProfile", "DipStore"]


@dataclass
class NoiseBand:
    """The instrument's measured repeatability at one luminance, from N back-to-back
    reads of a held patch. ``sigma_de`` (read-to-read CIEDE2000 std) is the practical
    handle the read policy uses; ``sigma_rel`` (relative luminance std) is kept for the
    audit / SNR math."""

    nits: float
    sigma_de: float
    sigma_rel: Optional[float] = None
    reads: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NoiseBand":
        return cls(
            nits=float(d["nits"]),
            sigma_de=float(d["sigma_de"]),
            sigma_rel=(float(d["sigma_rel"]) if d.get("sigma_rel") is not None else None),
            reads=int(d.get("reads", 0)),
        )


@dataclass
class DisplayInstrumentProfile:
    """One display+meter pair's measured behavioural model. All fields optional so a
    partial characterization (or an older record missing newer fields) still loads."""

    display: str
    mode: Optional[str] = None                    # 'SDR' | 'HDR' — panel behaviour (thermal/noise) differs
    #                                               by mode, so the store is keyed by display:mode (see `key`)
    # -- instrument axis --------------------------------------------------
    noise_model: list[NoiseBand] = field(default_factory=list)   # σ vs luminance, ascending nits
    noise_floor_nits: Optional[float] = None     # below this a read is noise-dominated; don't trust a single read
    read_overhead_s: Optional[float] = None      # measured per-read wall time at white (the practical
    #                                              fast-read floor: ~integration+overhead at the brightest
    #                                              patch, where integration is shortest) — for time estimates
    # -- display axis -----------------------------------------------------
    settle_seconds: Optional[float] = None       # measured step-response settle (the conservative worst case)
    settle_by_level: Optional[dict[str, float]] = None   # {"bright": s, "dark": s}
    native_white_xy: Optional[list[float]] = None
    native_white_nits: Optional[float] = None     # brief full-field peak (the absolute clip ceiling)
    sustained_peak_nits: Optional[float] = None   # HDR: the peak the panel HOLDS under a maintained
    #   thermal load (warm capture) — the DLC calibration peak (hdr_target.choose_peak_nits). Below
    #   native_white_nits (the brief flash eats headroom under sustained load). None until a warm
    #   capture lands → choose_peak_nits falls back to native_white_nits and flags it.
    native_black_nits: Optional[float] = None
    native_primaries: Optional[dict[str, list[float]]] = None    # {"R":[x,y],"G":..,"B":..}
    eotf_undershoot: Optional[float] = None       # HDR: full-field measured-vs-requested luminance error
    #   (e.g. -0.06 = panel renders ~6% below the PQ target across the range — a calibratable gain)
    white_vs_luminance: Optional[list[list[float]]] = None  # HDR: [[nits, x, y], ...] — the panel's
    #   white point is luminance-dependent; this maps it so calibration doesn't assume a fixed white
    # -- drift axis -------------------------------------------------------
    warmup_reads_to_settle: Optional[int] = None
    thermal_tau_patches: Optional[int] = None    # measured thermal time constant in content-read
    #   (≈ measurement-patch) units, from the closed-loop warm-in. Feeds the patch-ordering
    #   warm-start rotation (``dlc.engine.patches.sort_patches`` ``warm_tau``) so the rotation
    #   models THIS panel rather than the built-in default. None ⇒ no warm-in observed (or older
    #   record) → the ordering falls back to its default τ.
    warmup_minutes: Optional[float] = None       # wall-time to thermal stability (convergent panels only)
    fluctuation_envelope: Optional[float] = None  # residual channel-balance wander band once warmed (the
    #   run-time drift watch trips on drift LEAVING this band; the fluctuating regime never reaches 0)
    warmin_magnitude: Optional[float] = None      # total active-channel balance drift observed warming in
    warm_balance: Optional[list[float]] = None    # the converged OPERATING-load channel balance [R,G,B]
    #   (normalized) measured warm — a validated 'this is warm' fingerprint a calibration run can
    #   compare a live read against (Phase-2 fast-path). None on older records / never-converged runs.
    thermal_regime: Optional[str] = None         # 'convergent' | 'fluctuating' | 'warming' — DISCOVERED.
    #   convergent: warms to a steady temperature (SDR). fluctuating: content-driven, never settles —
    #   calibrate by MAINTAINING a consistent thermal load, not by reaching a target (HDR). warming:
    #   still climbing monotonically at the observation bound (warm longer).
    cold_channel: Optional[str] = None           # discovered, NOT assumed
    creep_rate_de_per_min: Optional[float] = None
    recommended_neutral_interval: Optional[int] = None
    recommended_drift_threshold: Optional[float] = None
    # -- metadata ---------------------------------------------------------
    instrument: Optional[str] = None             # meter id (e.g. "X-Rite i1 DisplayPro")
    correction_file: Optional[str] = None        # the .ccmx in force during characterization
    made: Optional[str] = None                   # YYYY-MM-DD — the staleness clock
    max_age_days: Optional[int] = None
    updated: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """The store key: ``display:mode`` when a mode is set (so SDR and HDR profiles for one
        panel coexist), else the bare display name (back-compat with mode-less records)."""
        return f"{self.display}:{self.mode}" if self.mode else self.display

    # -- serialization ----------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["noise_model"] = [b.as_dict() for b in self.noise_model]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DisplayInstrumentProfile":
        bands = [NoiseBand.from_dict(b) for b in (d.get("noise_model") or [])]
        bands.sort(key=lambda b: b.nits)
        return cls(
            display=d["display"],
            mode=d.get("mode"),
            noise_model=bands,
            noise_floor_nits=_opt_float(d.get("noise_floor_nits")),
            read_overhead_s=_opt_float(d.get("read_overhead_s")),
            settle_seconds=_opt_float(d.get("settle_seconds")),
            settle_by_level=(dict(d["settle_by_level"]) if d.get("settle_by_level") else None),
            native_white_xy=_opt_xy(d.get("native_white_xy")),
            native_white_nits=_opt_float(d.get("native_white_nits")),
            sustained_peak_nits=_opt_float(d.get("sustained_peak_nits")),
            native_black_nits=_opt_float(d.get("native_black_nits")),
            native_primaries=(dict(d["native_primaries"]) if d.get("native_primaries") else None),
            eotf_undershoot=_opt_float(d.get("eotf_undershoot")),
            white_vs_luminance=([[float(c) for c in row] for row in d["white_vs_luminance"]]
                                if d.get("white_vs_luminance") else None),
            warmup_reads_to_settle=(int(d["warmup_reads_to_settle"]) if d.get("warmup_reads_to_settle") is not None else None),
            thermal_tau_patches=(int(d["thermal_tau_patches"]) if d.get("thermal_tau_patches") is not None else None),
            warmup_minutes=_opt_float(d.get("warmup_minutes")),
            fluctuation_envelope=_opt_float(d.get("fluctuation_envelope")),
            warmin_magnitude=_opt_float(d.get("warmin_magnitude")),
            warm_balance=([float(c) for c in d["warm_balance"]] if d.get("warm_balance") else None),
            thermal_regime=d.get("thermal_regime"),
            cold_channel=d.get("cold_channel"),
            creep_rate_de_per_min=_opt_float(d.get("creep_rate_de_per_min")),
            recommended_neutral_interval=(int(d["recommended_neutral_interval"]) if d.get("recommended_neutral_interval") is not None else None),
            recommended_drift_threshold=_opt_float(d.get("recommended_drift_threshold")),
            instrument=d.get("instrument"),
            correction_file=d.get("correction_file"),
            made=d.get("made"),
            max_age_days=(int(d["max_age_days"]) if d.get("max_age_days") is not None else None),
            updated=d.get("updated"),
            notes=list(d.get("notes") or []),
        )

    # -- read-policy bridges ---------------------------------------------
    def expected_sigma_de(self, nits: float) -> Optional[float]:
        """Interpolate the measured read-to-read σ (CIEDE2000) at ``nits`` from the
        noise model. Linear between bands, clamped (held flat) past the ends. ``None``
        if the model is empty — the loop then falls back to its own live measurement."""
        bands = self.noise_model
        if not bands:
            return None
        if nits <= bands[0].nits:
            return bands[0].sigma_de
        if nits >= bands[-1].nits:
            return bands[-1].sigma_de
        for lo, hi in zip(bands, bands[1:]):
            if lo.nits <= nits <= hi.nits:
                span = hi.nits - lo.nits
                if span <= 0:
                    return lo.sigma_de
                f = (nits - lo.nits) / span
                return lo.sigma_de + f * (hi.sigma_de - lo.sigma_de)
        return bands[-1].sigma_de

    def reads_for_tolerance(self, nits: float, tolerance_de: float) -> Optional[int]:
        """How many reads to average so the standard error of the mean falls within
        ``tolerance_de`` at ``nits``, given the measured per-read σ. Averaging N reads
        cuts the SE by √N → N ≥ (σ/tol)². Returns ``None`` (defer to live measurement)
        when the noise model is empty; ``1`` when a single read already suffices."""
        sigma = self.expected_sigma_de(nits)
        if sigma is None:
            return None
        if tolerance_de <= 0:
            return 1
        need = (sigma / tolerance_de) ** 2
        return max(1, math.ceil(need))

    def is_stale(self, today_iso: str, *, default_max_age_days: int = 180) -> bool:
        """True if older than ``max_age_days`` (this record's, else the default). A DIP
        with no ``made`` date is treated as stale (force a re-characterization)."""
        if not self.made:
            return True
        from datetime import date
        try:
            made = date.fromisoformat(self.made)
            today = date.fromisoformat(today_iso)
        except ValueError:
            return True
        age = (today - made).days
        return age > (self.max_age_days if self.max_age_days is not None else default_max_age_days)


class DipStore:
    """JSON-backed map ``display name -> DisplayInstrumentProfile``, upserted by display.

    Tolerant of a missing or malformed file (empty store, ``corrupt`` flag set when a
    present file failed to parse) so a first run or a hand-corrupted file never crashes
    a calibration — the DIP is priors, never a gate.
    """

    def __init__(self, path: Path | str,
                 records: Optional[dict[str, DisplayInstrumentProfile]] = None,
                 *, corrupt: bool = False, dropped: Optional[list[str]] = None) -> None:
        self.path = Path(path)
        self._records: dict[str, DisplayInstrumentProfile] = dict(records or {})
        self.corrupt = corrupt
        # Names of records that were PRESENT in the file but failed to parse (schema drift /
        # hand-editing). Distinct from `corrupt` (whole file unparseable): the store stays
        # tolerant (never a gate), but a caller can surface "your DIP for X was dropped"
        # instead of silently running with no priors for that display.
        self.dropped: list[str] = list(dropped or [])

    @classmethod
    def load(cls, path: Path | str) -> "DipStore":
        p = Path(path)
        records: dict[str, DisplayInstrumentProfile] = {}
        corrupt = False
        dropped: list[str] = []
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw, corrupt = {}, True
            for name, rec in (raw.get("displays", {}) or {}).items():
                try:
                    records[name] = DisplayInstrumentProfile.from_dict(
                        {**rec, "display": rec.get("display", name)})
                except (KeyError, TypeError, ValueError):
                    dropped.append(str(name))
                    continue
        return cls(p, records, corrupt=corrupt, dropped=dropped)

    def get(self, display: str) -> Optional[DisplayInstrumentProfile]:
        return self._records.get(display)

    def records(self) -> dict[str, DisplayInstrumentProfile]:
        return dict(self._records)

    def record(self, dip: DisplayInstrumentProfile, *, save: bool = True) -> DisplayInstrumentProfile:
        """Upsert ``dip`` (keyed by ``dip.key`` = ``display:mode``, else bare display) and persist."""
        self._records[dip.key] = dip
        if save:
            self.save()
        return dip

    def remove(self, display: str, *, save: bool = True) -> bool:
        """Drop a display's DIP (e.g. when a characterization is rejected at review, so a
        bad profile is never left silently active). Returns whether a record was removed."""
        existed = self._records.pop(display, None) is not None
        if existed and save:
            self.save()
        return existed

    def save(self) -> None:
        # "schema" is a version stamp for forward drift (loaders today tolerate any shape via
        # per-record try/except + the dropped list; a future breaking change bumps this).
        payload = {"schema": 1,
                   "displays": {name: r.as_dict() for name, r in sorted(self._records.items())}}
        atomic_write_text(self.path, json.dumps(payload, indent=2))


def _opt_float(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _opt_xy(v: Any) -> Optional[list[float]]:
    return [float(v[0]), float(v[1])] if v else None
