"""Per-display **applied-stack registry** — what DLC last APPLIED to each ``display:mode``.

Why this exists (2026-09-03, PA32UCXR): a ``3dlut-only`` run starts with an empty run record
and DesktopLUT's ``state.get`` reports only the profile name and the runtime cube path — NOT
the peak the applying run handed the base 1D LUT. So the cube run re-resolved the HDR target
peak from the DIP's native ceiling (1835 nits) while the installed MHC deliberately holds D65
white at its green-bound Peak-Chroma cap (1805 nits, base cube top drive pinned there). Every
post-MHC stage then chased a top the stack cannot reach: the verify's white "error" (1.78
dE_ITP) was almost entirely that luminance bookkeeping, and the cube optimizer would have
pushed the top greys into a plateau it cannot lift.

The registry is the cross-run hand-off the MHC build already makes to C++ (``set_base_lut``'s
``peak_nits``), persisted DLC-side: the applying run's id, the profile name it associated,
the MHC parameters that matter downstream (primaries, measured white, target white, the
Peak-Chroma / max-drive cap) and the durable cube path. Flows that KEEP the installed MHC
(``3dlut-only`` / ``grayscale-wb``) pin their resolved HDR peak to the recorded cap — with
the pipe's current profile name cross-checked against the record, so a stack changed outside
DLC is surfaced as evidence (unpinned + warned), never silently trusted.

Priors, never a gate: a missing/corrupt file yields an empty registry and the flows fall back
to the DIP-resolved peak (flagged at the plan seam so the LLM sees the stack's cap is unknown).

CLI (backfill / inspection)::

    python -m dlc.stack_registry show  [--profile calibration_profile.yaml]
    python -m dlc.stack_registry import-run --run runs/<run_dir> [--cube <durable .cube>]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .paths import atomic_write_text

REGISTRY_FILE = "stack_registry.json"
SCHEMA = 1


def _opt_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class StackRecord:
    """The stack DLC last applied to one ``display:mode``.

    ``hdr_peak`` (HDR only) carries the calibrated top the MHC build decided:
    ``cube_peak_nits`` is the luminance the base cube holds the target white at (the
    Peak-Chroma cap when capped, else the resolved peak); ``cube_max_drive`` the drive code
    the base cube's top is pinned to; ``resolved_peak_nits`` the run's pre-cap target.
    """

    display: str
    mode: str
    monitor: int
    run_id: str
    applied_at: str
    profile_name: Optional[str] = None
    mhc: dict[str, Any] = field(default_factory=dict)
    hdr_peak: Optional[dict[str, Any]] = None
    cube: Optional[dict[str, Any]] = None
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.display}:{self.mode}"

    @property
    def cube_peak_nits(self) -> Optional[float]:
        """The luminance the installed MHC holds the target white at (the pin for post-MHC
        flows); ``None`` for SDR records or an MHC applied before the cap was recorded."""
        return _opt_float((self.hdr_peak or {}).get("cube_peak_nits"))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StackRecord":
        return cls(display=str(d["display"]), mode=str(d["mode"]), monitor=int(d.get("monitor", 0)),
                   run_id=str(d.get("run_id", "")), applied_at=str(d.get("applied_at", "")),
                   profile_name=d.get("profile_name"), mhc=dict(d.get("mhc") or {}),
                   hdr_peak=(dict(d["hdr_peak"]) if d.get("hdr_peak") else None),
                   cube=(dict(d["cube"]) if d.get("cube") else None),
                   notes=list(d.get("notes") or []))


def record_from_mhc_params(*, display: str, mode: str, monitor: int, run_id: str,
                           profile_name: Optional[str], mhc_params: dict[str, Any],
                           target_white_xy: Optional[tuple[float, float]] = None,
                           applied_at: Optional[str] = None) -> StackRecord:
    """Build the record for an MHC the run built + applied, from the run's ``mhc_params``
    (the build stage's persisted derivation: primaries, measured white, peak_chroma, base)."""
    pc = dict(mhc_params.get("peak_chroma") or {})
    base_lut = mhc_params.get("base_lut") if isinstance(mhc_params.get("base_lut"), dict) else {}
    summary = dict(base_lut.get("summary") or {})
    hdr_peak: Optional[dict[str, Any]] = None
    # The calibrated top, in precedence: the peak the run handed C++ with the FINAL base LUT
    # (refine rounds re-install with the cube's actual top), else the build's peak_chroma.
    top = (_opt_float(base_lut.get("peak_nits")) or _opt_float(pc.get("cube_peak_nits"))
           or _opt_float(pc.get("cap_nits")))
    if top is not None:
        hdr_peak = {
            "cube_peak_nits": top,
            "resolved_peak_nits": _opt_float(pc.get("resolved_peak_nits")),
            "cap_nits": _opt_float(pc.get("cap_nits")),
            "capped": bool(pc.get("capped")),
            "cap_policy": pc.get("cap_policy"),
            "binding_channel": pc.get("binding_channel"),
            "cube_max_drive": _opt_float(pc.get("cube_max_drive") if pc.get("cube_max_drive") is not None
                                         else summary.get("max_drive")),
            "native_peak_nits": _opt_float(pc.get("native_peak_nits")),
        }
    mhc = {
        "primaries": dict(mhc_params.get("primaries") or {}),
        "measured_white": dict(mhc_params.get("measured_white") or {}),
        "target_white_xy": list(target_white_xy) if target_white_xy else None,
        "dark_floor": dict(mhc_params.get("dark_floor") or {}) or None,
        "base_lut": base_lut.get("cube_path"),
    }
    return StackRecord(display=display, mode=mode, monitor=int(monitor), run_id=run_id,
                       applied_at=applied_at or datetime.now().isoformat(timespec="seconds"),
                       profile_name=profile_name, mhc=mhc, hdr_peak=hdr_peak)


class StackRegistry:
    """JSON-backed map ``display:mode -> StackRecord`` (tolerant of a missing/corrupt file)."""

    def __init__(self, path: Path | str, records: Optional[dict[str, StackRecord]] = None,
                 *, corrupt: bool = False, dropped: Optional[list[str]] = None) -> None:
        self.path = Path(path)
        self._records: dict[str, StackRecord] = dict(records or {})
        self.corrupt = corrupt
        self.dropped: list[str] = list(dropped or [])

    @classmethod
    def load(cls, path: Path | str) -> "StackRegistry":
        p = Path(path)
        records: dict[str, StackRecord] = {}
        corrupt = False
        dropped: list[str] = []
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw, corrupt = {}, True
            for key, rec in ((raw.get("stacks") or {}) if isinstance(raw, dict) else {}).items():
                try:
                    records[str(key)] = StackRecord.from_dict(rec)
                except (KeyError, TypeError, ValueError):
                    dropped.append(str(key))
        return cls(p, records, corrupt=corrupt, dropped=dropped)

    def get(self, display: str, mode: str) -> Optional[StackRecord]:
        return self._records.get(f"{display}:{mode}")

    def records(self) -> dict[str, StackRecord]:
        return dict(self._records)

    def record(self, rec: StackRecord, *, save: bool = True) -> StackRecord:
        self._records[rec.key] = rec
        if save:
            self.save()
        return rec

    def record_cube(self, *, display: str, mode: str, monitor: int, run_id: str,
                    cube_path: Optional[str], profile_name: Optional[str] = None,
                    save: bool = True) -> StackRecord:
        """Upsert the 3D-LUT entry on the display's record (an in-place ``3dlut-only`` apply).
        Without a prior MHC record one is created with an UNKNOWN MHC (noted) so the cube is
        still tracked — the next MHC build overwrites it."""
        rec = self.get(display, mode)
        stamp = datetime.now().isoformat(timespec="seconds")
        if rec is None:
            rec = StackRecord(display=display, mode=mode, monitor=int(monitor), run_id="",
                              applied_at=stamp, profile_name=profile_name,
                              notes=["MHC applied outside the registry (cube recorded by a "
                                     "3dlut-only run); its cap is unknown"])
        elif profile_name and rec.profile_name and profile_name != rec.profile_name:
            rec.notes.append(f"{stamp}: pipe profile {profile_name} != recorded MHC "
                             f"{rec.profile_name} at the cube apply (stack changed outside DLC?)")
        rec.cube = {"cube_path": cube_path, "run_id": run_id, "applied_at": stamp}
        return self.record(rec, save=save)

    def save(self) -> None:
        payload = {"schema": SCHEMA,
                   "stacks": {k: r.as_dict() for k, r in sorted(self._records.items())}}
        atomic_write_text(self.path, json.dumps(payload, indent=2))


def registry_path(profile: Any, ctx_root: Path) -> Path:
    """Alongside the profile when it is on disk (durable across ``runs/`` prunes), else beside
    the run folders — mirrors ``dip_store_path`` so the two priors stores sit together."""
    source = getattr(profile, "source_path", None)
    if source:
        base = Path(source).resolve().parent
    else:
        base = ctx_root.parent if ctx_root.parent != ctx_root else ctx_root
    return base / REGISTRY_FILE


def _norm_path(p: Any) -> Optional[str]:
    if not p:
        return None
    return str(p).replace("\\", "/").lower().rstrip("/")


def check_against_pipe(rec: Optional[StackRecord], pipe_state: Optional[dict[str, Any]],
                       monitor: int, mode: str) -> dict[str, Any]:
    """Evidence packet: does the pipe's current profile match the registry's record?

    Returns ``{recorded, pipe_profile, recorded_profile, matches, pin_nits, reason}``.
    ``pin_nits`` is set only when the record is trustworthy for this stack: a record exists,
    it carries a cap, and either the pipe reports the same profile name or the pipe reports no
    name at all (older servers / mock) — a DIFFERENT name means the stack changed outside DLC,
    so the cap is not pinned and the reason says why.
    """
    out: dict[str, Any] = {"recorded": rec is not None, "pipe_profile": None,
                           "recorded_profile": rec.profile_name if rec else None,
                           "matches": None, "pin_nits": None, "reason": None}
    if rec is not None:
        out["run_id"] = rec.run_id
        out["applied_at"] = rec.applied_at
        out["hdr_peak"] = rec.hdr_peak
        out["cube"] = rec.cube
    pipe_profile = None
    pipe_source = None
    if isinstance(pipe_state, dict):
        entry = (pipe_state.get("mhc") or {}).get(f"{monitor}:{mode}") or {}
        pipe_profile = entry.get("profile_name") if isinstance(entry, dict) else None
        pipe_source = entry.get("source_file") if isinstance(entry, dict) else None
    out["pipe_profile"] = pipe_profile
    out["pipe_source_file"] = pipe_source
    if rec is None:
        out["reason"] = "no applied-stack record for this display/mode (MHC applied before the " \
                        "registry existed, or by another tool) — its cap is unknown"
        return out
    if pipe_profile and rec.profile_name and pipe_profile != rec.profile_name:
        # The profile NAME churns with every WB/DG/GS permutation re-bake; the DLC base
        # artifact the profile was generated from (state.get `source_file`, the base 1D
        # .cube handed over set_base_lut) is the stable identity. Same artifact ⇒ same stack.
        rec_base = _norm_path((rec.mhc or {}).get("base_lut"))
        if pipe_source and rec_base and _norm_path(pipe_source) == rec_base:
            out["matches"] = True
            out["matched_by"] = "source_file"
            out["note"] = (f"pipe profile {pipe_profile!r} differs from the recorded "
                           f"{rec.profile_name!r} (a GUI-layer permutation re-bake) but is generated "
                           "from the same DLC base LUT")
        else:
            out["matches"] = False
            out["reason"] = (f"pipe reports MHC {pipe_profile!r} but the registry recorded "
                             f"{rec.profile_name!r} (run {rec.run_id})"
                             + (f" from a different base artifact ({pipe_source})" if pipe_source else
                                " and the server reports no source_file to identify it by")
                             + " — the stack changed outside DLC; not pinning to a possibly stale cap")
            return out
    if out.get("matches") is None:
        out["matches"] = True if (pipe_profile and rec.profile_name) else None
        if out["matches"]:
            out["matched_by"] = "profile_name"
    cap = rec.cube_peak_nits
    if cap is None:
        out["reason"] = "record carries no HDR cap (SDR stack, or an MHC applied before the cap was recorded)"
        return out
    out["pin_nits"] = cap
    out["reason"] = (f"pinned to the installed MHC's calibrated top {cap:g} nits "
                     f"(run {rec.run_id}, {rec.hdr_peak.get('cap_policy') or 'uncapped'}"
                     + (f", binding {rec.hdr_peak.get('binding_channel')}" if rec.hdr_peak.get("binding_channel") else "")
                     + ")")
    return out


# ---------------------------------------------------------------------------
# CLI: inspect / backfill from a run record
# ---------------------------------------------------------------------------

def import_run(run_dir: Path, registry: StackRegistry, *, cube_path: Optional[str] = None,
               profile_name: Optional[str] = None, display_name: Optional[str] = None,
               save: bool = True) -> StackRecord:
    """Backfill a record from a run's ``dlc_state.json`` + ``manifest.json`` (the run must have
    built and applied an MHC: ``build-install-mhc`` done + ``mhc_params`` present)."""
    state = json.loads((run_dir / "dlc_state.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")) \
        if (run_dir / "manifest.json").exists() else {}
    calib = state.get("calib") or {}
    params = state.get("mhc_params") or {}
    if not params:
        raise ValueError(f"{run_dir}: no mhc_params in dlc_state.json (no MHC built here)")
    build = ((calib.get("stages") or {}).get("build-install-mhc") or {})
    if build.get("status") != "done":
        raise ValueError(f"{run_dir}: build-install-mhc is not done")
    digest = build.get("digest") or {}
    display = display_name or manifest.get("display") or calib.get("display") or "unknown"
    mode = str(state.get("mode") or params.get("mode") or manifest.get("mode") or "HDR").upper()
    monitor = int(state.get("monitor") if state.get("monitor") is not None else params.get("monitor", 0))
    white = digest.get("white_xy")
    rec = record_from_mhc_params(
        display=display, mode=mode, monitor=monitor, run_id=run_dir.name,
        profile_name=profile_name or digest.get("profile_name"),
        mhc_params=params, target_white_xy=tuple(white) if white else None,
        applied_at=manifest.get("created") or datetime.now().isoformat(timespec="seconds"))
    rec.notes.append(f"imported from {run_dir.name} by stack_registry import-run"
                     + (" (profile name supplied by the operator)" if profile_name else ""))
    if cube_path:
        rec.cube = {"cube_path": cube_path, "run_id": run_dir.name,
                    "applied_at": rec.applied_at}
    return registry.record(rec, save=save)


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dlc.stack_registry", description=__doc__.split("\n\n")[0])
    ap.add_argument("--registry", default=None, help=f"path to {REGISTRY_FILE} (default: next to the profile)")
    ap.add_argument("--profile", default="calibration_profile.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    imp = sub.add_parser("import-run")
    imp.add_argument("--run", required=True)
    imp.add_argument("--cube", default=None, help="durable .cube applied with this stack (optional)")
    imp.add_argument("--display", default=None, help="override the display name key")
    imp.add_argument("--profile-name", default=None, dest="profile_name",
                     help="the associated MHC profile name (from DesktopLUT state) when the run "
                          "record did not capture it")
    a = ap.parse_args(argv)
    path = Path(a.registry) if a.registry else Path(a.profile).resolve().parent / REGISTRY_FILE
    reg = StackRegistry.load(path)
    if a.cmd == "show":
        print(f"registry: {path}  (corrupt={reg.corrupt}, dropped={reg.dropped})")
        for key, rec in sorted(reg.records().items()):
            peak = rec.cube_peak_nits
            print(f"  {key}: profile={rec.profile_name} run={rec.run_id} applied={rec.applied_at} "
                  f"cap={'%.1f' % peak if peak else '-'} cube={(rec.cube or {}).get('cube_path')}")
        return 0
    run_dir = Path(a.run)
    rec = import_run(run_dir, reg, cube_path=a.cube, profile_name=a.profile_name,
                     display_name=a.display, save=True)
    print(f"recorded {rec.key}: profile={rec.profile_name} cap={rec.cube_peak_nits} -> {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
