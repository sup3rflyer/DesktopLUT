"""Persistent per-display correction store (v2-design-notes §10; HANDOFF item 7).

The colorimeter correction (CCMX/CCSS) and the SPD-derived white are **per-display
hardware facts that outlive a single run** — the design calls for "a persistent
per-display correction store (with a date), not per-run." This module is that store:
a small JSON file, keyed by **(display, mode)**, recording for each display+mode the
correction in use + its build date, the white SPD it was derived from, and the resolved
target white (chromaticity + provenance). It is the corrections' "medical history".

**Mode-keyed (schema 2).** A colorimeter correction is built against the panel's spectra
*in one mode*: the PA32UCXR's SDR and HDR CCMXs differ by ~1.6 dE2000 on full red. Schema 1
kept ONE record per display, so ingesting an HDR correction silently replaced the SDR one
and every SDR run from 2026-06-19 to 2026-09-25 measured through the HDR CCMX. Each mode
now has its own slot; a run reads only its own mode's slot (``get(display, mode)``) and
never borrows the other mode's file (``calibrate.resolve_correction`` falls back to the
profile YAML instead, visibly).

Legacy schema-1 files load without loss: each flat per-display record is assigned to ONE
mode (never both) from the mode tag in its correction filename (``_HDR`` / ``_SDR`` — the
probe-match naming), or, untagged, to SDR (the probe-match SDR name carries no tag). The
assignment is recorded in ``mode_source`` and surfaced via
:meth:`CorrectionStore.mode_inferences` until a correction is freshly ingested for that slot.

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
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .paths import atomic_write_text

__all__ = ["CorrectionRecord", "CorrectionStore", "MODES", "SCHEMA", "infer_mode_from_filename",
           "MODE_RECORDED", "MODE_LEGACY_TAG", "MODE_LEGACY_UNTAGGED"]

MODES = ("SDR", "HDR")
SCHEMA = 2

# mode_source values: how a record came to sit in its mode slot.
MODE_RECORDED = "recorded"                            # written by mode-aware code for that mode
MODE_LEGACY_TAG = "legacy-filename-tag"               # schema-1 record, mode read from a filename tag
MODE_LEGACY_UNTAGGED = "legacy-untagged-assumed-SDR"  # schema-1 record, no tag → SDR by naming convention

# A mode token in a filename, delimited by start/end or one of _ - . space (so "HDR" inside a
# word does not count). Matches the probe-match names "<display>_HDR-ColorChecker-…".
_MODE_TAG = re.compile(r"(?:^|[_\-. ])(HDR|SDR)(?=[_\-. ]|$)", re.IGNORECASE)

_RECORD_KEYS = frozenset({"display", "correction_file", "correction_made", "spd_file", "white_xy",
                          "white_provenance", "observer", "anchor", "strength", "updated",
                          "mode", "mode_source"})


def infer_mode_from_filename(path: Optional[str]) -> Optional[str]:
    """``"HDR"``/``"SDR"`` when the file's basename carries exactly one kind of mode tag, else
    ``None`` (untagged, or contradictory). Handles Windows and POSIX separators alike."""
    if not path:
        return None
    base = re.split(r"[\\/]", str(path))[-1]
    tags = {m.upper() for m in _MODE_TAG.findall(base)}
    return tags.pop() if len(tags) == 1 else None


def _normalize_mode(mode: Any) -> str:
    m = str(mode).upper()
    if m not in MODES:
        raise ValueError(f"correction mode must be one of {MODES}, got {mode!r}")
    return m


@dataclass
class CorrectionRecord:
    """One (display, mode) slot's persisted correction + white provenance."""

    display: str
    mode: Optional[str] = None                   # SDR | HDR — the slot this record lives in
    correction_file: Optional[str] = None
    correction_made: Optional[str] = None       # YYYY-MM-DD — the staleness clock
    spd_file: Optional[str] = None               # the white SPD the correction/white came from
    white_xy: Optional[list] = None              # [x, y] resolved target white
    white_provenance: Optional[str] = None       # override | spd_crt_like | numeric
    observer: Optional[str] = None
    anchor: Optional[str] = None
    strength: Optional[float] = None
    updated: Optional[str] = None                # YYYY-MM-DD this record was last written
    # recorded | owner-confirmed (a hand migration) | legacy-filename-tag | legacy-untagged-assumed-SDR
    # — only the legacy-* values are surfaced as inferences.
    mode_source: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CorrectionRecord":
        wx = d.get("white_xy")
        return cls(
            display=d["display"],
            mode=(_normalize_mode(d["mode"]) if d.get("mode") else None),
            correction_file=d.get("correction_file"),
            correction_made=d.get("correction_made"),
            spd_file=d.get("spd_file"),
            white_xy=[float(wx[0]), float(wx[1])] if wx else None,
            white_provenance=d.get("white_provenance"),
            observer=d.get("observer"),
            anchor=d.get("anchor"),
            strength=(float(d["strength"]) if d.get("strength") is not None else None),
            updated=d.get("updated"),
            mode_source=d.get("mode_source"),
        )


class CorrectionStore:
    """A JSON-backed map ``(display, mode) -> CorrectionRecord``, upserted by slot.

    Tolerant of a missing or malformed file (returns an empty store) so a first run
    or a hand-corrupted file never crashes a calibration — the store is a convenience
    record, never a gate. Reads schema 1 (flat per-display) and schema 2 (per mode);
    always writes schema 2.
    """

    def __init__(self, path: Path | str,
                 records: Optional[dict[tuple[str, str], CorrectionRecord]] = None,
                 *, corrupt: bool = False, dropped: Optional[list[str]] = None) -> None:
        self.path = Path(path)
        self._records: dict[tuple[str, str], CorrectionRecord] = dict(records or {})
        # True iff the file existed but did not parse — distinct from "absent" (a clean first
        # run). Lets a caller surface real corruption (vs silently falling back to the stale
        # YAML correction), while the store itself stays tolerant (never a gate).
        self.corrupt = corrupt
        # Names of records present in the file but individually unparseable (schema drift /
        # hand-editing) — dropped, but visibly so, mirroring DipStore.dropped. A bad mode
        # slot is named "display:mode".
        self.dropped: list[str] = list(dropped or [])

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str) -> "CorrectionStore":
        p = Path(path)
        records: dict[tuple[str, str], CorrectionRecord] = {}
        corrupt = False
        dropped: list[str] = []
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw, corrupt = {}, True   # present but unparseable — surface it (see .corrupt)
            displays = (raw.get("displays", {}) or {}) if isinstance(raw, dict) else {}
            for name, entry in displays.items():
                if not isinstance(entry, dict):
                    dropped.append(str(name))
                    continue
                if _RECORD_KEYS & set(entry):
                    # Schema-1 flat record (or a hand-edited one): exactly ONE mode slot.
                    try:
                        rec = cls._legacy_record(str(name), entry)
                    except (KeyError, TypeError, ValueError):
                        dropped.append(str(name))
                        continue
                    records.setdefault((rec.display, rec.mode), rec)
                    continue
                for mode_key, rec_d in entry.items():
                    try:
                        mode = _normalize_mode(mode_key)
                        rec = CorrectionRecord.from_dict(
                            {**rec_d, "display": rec_d.get("display", name), "mode": mode})
                    except (KeyError, TypeError, ValueError, AttributeError):
                        dropped.append(f"{name}:{mode_key}")
                        continue
                    records[(rec.display, mode)] = rec
        return cls(p, records, corrupt=corrupt, dropped=dropped)

    @staticmethod
    def _legacy_record(name: str, entry: dict[str, Any]) -> CorrectionRecord:
        """A schema-1 per-display record, placed in exactly one mode slot. The correction
        file decides (it is what the cross-mode leak was about); only a record without one
        consults the SPD filename. Untagged → SDR (the probe-match SDR naming carries no
        tag), flagged via ``mode_source`` so it is surfaced rather than trusted."""
        rec = CorrectionRecord.from_dict({**entry, "display": entry.get("display", name), "mode": None})
        if entry.get("mode"):
            rec.mode = _normalize_mode(entry["mode"])
            rec.mode_source = entry.get("mode_source") or MODE_RECORDED
            return rec
        tagged = infer_mode_from_filename(rec.correction_file or rec.spd_file)
        rec.mode = tagged or "SDR"
        rec.mode_source = MODE_LEGACY_TAG if tagged else MODE_LEGACY_UNTAGGED
        return rec

    # -- access -----------------------------------------------------------
    def get(self, display: str, mode: str) -> Optional[CorrectionRecord]:
        """The record in ``display``'s ``mode`` slot — never another mode's."""
        return self._records.get((display, _normalize_mode(mode)))

    def modes_for(self, display: str) -> dict[str, CorrectionRecord]:
        """Every mode slot recorded for ``display`` (``{"SDR": rec, "HDR": rec}``)."""
        return {m: r for (d, m), r in self._records.items() if d == display}

    def records(self) -> dict[tuple[str, str], CorrectionRecord]:
        return dict(self._records)

    def mode_inferences(self) -> list[dict[str, Any]]:
        """Records whose mode slot was INFERRED from a schema-1 file rather than recorded
        by mode-aware code — surfaced so a guessed assignment is never silently trusted. An
        SPD whose filename tag names the other mode is flagged as a conflict."""
        out: list[dict[str, Any]] = []
        for (display, mode), r in sorted(self._records.items()):
            if not (r.mode_source or "").startswith("legacy"):
                continue
            note: dict[str, Any] = {"display": display, "mode": mode, "basis": r.mode_source,
                                    "correction_file": r.correction_file}
            spd_tag = infer_mode_from_filename(r.spd_file)
            if spd_tag and spd_tag != mode:
                note["spd_conflict"] = f"spd_file {r.spd_file} is tagged {spd_tag} but sits in the {mode} slot"
            out.append(note)
        return out

    # -- mutation ---------------------------------------------------------
    def record(self, rec: CorrectionRecord, *, save: bool = True) -> CorrectionRecord:
        """Upsert ``rec`` into its ``(display, mode)`` slot and persist by default. The record
        MUST name its mode — a mode-less record is exactly the cross-mode leak."""
        if not rec.mode:
            raise ValueError(f"CorrectionRecord for {rec.display!r} has no mode — "
                             f"corrections are per mode ({'/'.join(MODES)})")
        rec.mode = _normalize_mode(rec.mode)
        if rec.mode_source is None:
            rec.mode_source = MODE_RECORDED
        self._records[(rec.display, rec.mode)] = rec
        if save:
            self.save()
        return rec

    def save(self) -> None:
        # "schema" is a version stamp for forward drift (loaders tolerate unknown shapes via
        # per-record try/except + the dropped list; a breaking change bumps this — schema 2
        # made the store mode-keyed: displays -> {mode -> record}).
        displays: dict[str, dict[str, Any]] = {}
        for (name, mode), r in sorted(self._records.items()):
            displays.setdefault(name, {})[mode] = r.as_dict()
        payload = {"schema": SCHEMA, "displays": displays}
        # Atomic: a crash mid-write must not truncate the store and silently drop a
        # freshly-minted CCMX/SPD (the load is corruption-tolerant, so a truncated file would
        # fall back to the stale YAML correction with no error). See paths.atomic_write_text.
        atomic_write_text(self.path, json.dumps(payload, indent=2))
