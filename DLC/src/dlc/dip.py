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
    # -- instrument axis --------------------------------------------------
    noise_model: list[NoiseBand] = field(default_factory=list)   # σ vs luminance, ascending nits
    noise_floor_nits: Optional[float] = None     # below this a read is noise-dominated; don't trust a single read
    read_overhead_s: Optional[float] = None      # fixed per-read cost beyond integration (for time estimates)
    # -- display axis -----------------------------------------------------
    settle_seconds: Optional[float] = None       # measured step-response settle (the conservative worst case)
    settle_by_level: Optional[dict[str, float]] = None   # {"bright": s, "dark": s}
    native_white_xy: Optional[list[float]] = None
    native_white_nits: Optional[float] = None
    native_black_nits: Optional[float] = None
    native_primaries: Optional[dict[str, list[float]]] = None    # {"R":[x,y],"G":..,"B":..}
    # -- drift axis -------------------------------------------------------
    warmup_reads_to_settle: Optional[int] = None
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
            noise_model=bands,
            noise_floor_nits=_opt_float(d.get("noise_floor_nits")),
            read_overhead_s=_opt_float(d.get("read_overhead_s")),
            settle_seconds=_opt_float(d.get("settle_seconds")),
            settle_by_level=(dict(d["settle_by_level"]) if d.get("settle_by_level") else None),
            native_white_xy=_opt_xy(d.get("native_white_xy")),
            native_white_nits=_opt_float(d.get("native_white_nits")),
            native_black_nits=_opt_float(d.get("native_black_nits")),
            native_primaries=(dict(d["native_primaries"]) if d.get("native_primaries") else None),
            warmup_reads_to_settle=(int(d["warmup_reads_to_settle"]) if d.get("warmup_reads_to_settle") is not None else None),
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
                 *, corrupt: bool = False) -> None:
        self.path = Path(path)
        self._records: dict[str, DisplayInstrumentProfile] = dict(records or {})
        self.corrupt = corrupt

    @classmethod
    def load(cls, path: Path | str) -> "DipStore":
        p = Path(path)
        records: dict[str, DisplayInstrumentProfile] = {}
        corrupt = False
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
                    continue
        return cls(p, records, corrupt=corrupt)

    def get(self, display: str) -> Optional[DisplayInstrumentProfile]:
        return self._records.get(display)

    def records(self) -> dict[str, DisplayInstrumentProfile]:
        return dict(self._records)

    def record(self, dip: DisplayInstrumentProfile, *, save: bool = True) -> DisplayInstrumentProfile:
        """Upsert ``dip`` (keyed by ``dip.display``) and persist by default."""
        self._records[dip.display] = dip
        if save:
            self.save()
        return dip

    def save(self) -> None:
        payload = {"displays": {name: r.as_dict() for name, r in sorted(self._records.items())}}
        atomic_write_text(self.path, json.dumps(payload, indent=2))


def _opt_float(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _opt_xy(v: Any) -> Optional[list[float]]:
    return [float(v[0]), float(v[1])] if v else None
