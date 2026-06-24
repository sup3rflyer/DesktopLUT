"""Persistent per-display correction store (v2-design-notes §10; HANDOFF item 7).

The colorimeter correction (CCMX/CCSS) and the SPD-derived white are **per-display
hardware facts that outlive a single run** — the design calls for "a persistent
per-display correction store (with a date), not per-run." This module is that store:
a small JSON file, keyed by display name, recording for each display the correction
in use + its build date, the white SPD it was derived from, and the resolved target
white (chromaticity + provenance). It is the corrections' "medical history".

It is **local-only / private** (display- and probe-specific, like the profile) — the
orchestrator writes it next to the profile (or, in tests, next to the run folders).

Why a store *and* a profile? The profile YAML is the human-authored *configuration*;
the store is the machine-maintained *record*. When a correction is refreshed (a new
CCMX built) its real date lands here without editing the YAML, so the staleness
verdict ages from when the correction was actually made (see
:meth:`dlc.calibration_profile.Profile.correction_staleness`'s ``made_override``).

Dependency-free (stdlib JSON only) — importing it is free.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .paths import atomic_write_text

__all__ = ["CorrectionRecord", "CorrectionStore"]


@dataclass
class CorrectionRecord:
    """One display's persisted correction + white provenance."""

    display: str
    correction_file: Optional[str] = None
    correction_made: Optional[str] = None       # YYYY-MM-DD — the staleness clock
    spd_file: Optional[str] = None               # the white SPD the correction/white came from
    white_xy: Optional[list] = None              # [x, y] resolved target white
    white_provenance: Optional[str] = None       # override | spd_crt_like | numeric
    observer: Optional[str] = None
    anchor: Optional[str] = None
    strength: Optional[float] = None
    updated: Optional[str] = None                # YYYY-MM-DD this record was last written

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CorrectionRecord":
        wx = d.get("white_xy")
        return cls(
            display=d["display"],
            correction_file=d.get("correction_file"),
            correction_made=d.get("correction_made"),
            spd_file=d.get("spd_file"),
            white_xy=[float(wx[0]), float(wx[1])] if wx else None,
            white_provenance=d.get("white_provenance"),
            observer=d.get("observer"),
            anchor=d.get("anchor"),
            strength=(float(d["strength"]) if d.get("strength") is not None else None),
            updated=d.get("updated"),
        )


class CorrectionStore:
    """A JSON-backed map ``display name -> CorrectionRecord``, upserted by display.

    Tolerant of a missing or malformed file (returns an empty store) so a first run
    or a hand-corrupted file never crashes a calibration — the store is a convenience
    record, never a gate.
    """

    def __init__(self, path: Path | str, records: Optional[dict[str, CorrectionRecord]] = None,
                 *, corrupt: bool = False) -> None:
        self.path = Path(path)
        self._records: dict[str, CorrectionRecord] = dict(records or {})
        # True iff the file existed but did not parse — distinct from "absent" (a clean first
        # run). Lets a caller surface real corruption (vs silently falling back to the stale
        # YAML correction), while the store itself stays tolerant (never a gate).
        self.corrupt = corrupt

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str) -> "CorrectionStore":
        p = Path(path)
        records: dict[str, CorrectionRecord] = {}
        corrupt = False
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw, corrupt = {}, True   # present but unparseable — surface it (see .corrupt)
            for name, rec in (raw.get("displays", {}) or {}).items():
                try:
                    records[name] = CorrectionRecord.from_dict({**rec, "display": rec.get("display", name)})
                except (KeyError, TypeError, ValueError):
                    continue
        return cls(p, records, corrupt=corrupt)

    # -- access -----------------------------------------------------------
    def get(self, display: str) -> Optional[CorrectionRecord]:
        return self._records.get(display)

    def records(self) -> dict[str, CorrectionRecord]:
        return dict(self._records)

    # -- mutation ---------------------------------------------------------
    def record(self, rec: CorrectionRecord, *, save: bool = True) -> CorrectionRecord:
        """Upsert ``rec`` (keyed by ``rec.display``) and persist by default."""
        self._records[rec.display] = rec
        if save:
            self.save()
        return rec

    def save(self) -> None:
        payload = {"displays": {name: r.as_dict() for name, r in sorted(self._records.items())}}
        # Atomic: a crash mid-write must not truncate the store and silently drop a
        # freshly-minted CCMX/SPD (the load is corruption-tolerant, so a truncated file would
        # fall back to the stale YAML correction with no error). See paths.atomic_write_text.
        atomic_write_text(self.path, json.dumps(payload, indent=2))
