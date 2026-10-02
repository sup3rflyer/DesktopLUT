"""Held-out verify (V1) — how much of the verify headline is measured where the calibration was
TRAINED, and fresh held-out verify signals that provably were not.

Why (adversarial review of the PA32UCXR SDR run ``20261002_012945``, 2026-10-02): the 3D-LUT
build's closed loop probes AT the post-MHC training signals and folds every probe read back into
its training, so a verify signal that coincides with a training signal — or whose cube DRIVE
coincides with a training signal / probe drive — scores the correction where it was fitted
(in-sample). That run: 141 unique verify signals, 77 within 1 code of a post-MHC training signal;
held-out (> 4 codes) per-signal avg 0.320 vs coincident 0.296 at its measured white (0.313 vs 0.271
when scored at the nominal 120 nits).

Pure mechanics, evidence only — the code measures distances and splits stats; the LLM judges the
verify seam:

* :func:`load_training` / :func:`load_probe_drives` — a run's TRAINING set: every signal of its
  non-verify measurement TI3s (``measurements/*.ti3`` except ``verify*``) plus the LIVE build's
  probe DRIVES (``measurements/build_probes.ndjson``, appended per read and tagged with the build
  attempt, so a superseded attempt's drives drop out; runs from before the tag fall back to every
  recorded probe read incl. the ``events.jsonl`` rows — a conservative superset).
  :func:`training_context` assembles it from a run record; :func:`training_key` fingerprints it.
* :func:`classify_signals` — per verify signal, the Chebyshev distance in OUTPUT CODES at the run's
  bit depth of the signal (``d_in``) and of its cube drive ``sample_cube(cube, s)`` (``d_drive``,
  only when the verify measured through a cube) to the training set; ``held_out`` ⇔
  ``min(d_in, d_drive) > 4`` codes, ``coincident`` ⇔ ``≤ 1``, ``near`` between; ``strict_held_out``
  ⇔ held-out AND off the cube lattice (not every channel within 1 code of a lattice node).
* :func:`held_out_summary` — per-signal stats per class over the gate's population.
* :func:`draw_held_out_signals` — N fresh verify signals per run, seeded by the run id (a resume
  re-draws the identical set), hue-stratified over the six sextants, with a saturation/value spread
  so dim and half-saturated
  colours are represented, rejection-filtered ≥ 8 codes from every training signal / probe drive
  (and, through a cube, in drive space too), off-lattice, never a duplicate. **SDR only for now** —
  HDR draws need the gamut-aware hue caps and a PQ value floor (follow-up).
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import random
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from .metrics import HELD_OUT_GATE_MIN_SIGNALS, bucket_stats
from .mhc import parse_ti3

__all__ = [
    "HELD_OUT_CODES", "COINCIDENT_CODES", "LATTICE_CODES", "DEFAULT_LATTICE_SIZE",
    "GATE_MIN_SIGNALS", "DRAW_MIN_CODES", "DRAW_SATURATION", "PROBES_FILE",
    "run_seed", "to_codes", "min_chebyshev", "lattice_distance_codes", "load_training",
    "load_probe_drives", "append_probe_drives", "load_cube", "classify_signals",
    "held_out_summary", "thresholds", "training_context", "training_key", "held_out_view",
    "draw_held_out_signals",
]

HELD_OUT_CODES = 4.0          # held-out ⇔ min(d_in, d_drive) > 4 output codes
COINCIDENT_CODES = 1.0        # coincident ⇔ min(d_in, d_drive) <= 1 code (the same stimulus ± rounding)
LATTICE_CODES = 1.0           # on-lattice ⇔ every channel within 1 code of a cube lattice coordinate
DEFAULT_LATTICE_SIZE = 33     # the production cube grid (a run's own cube says otherwise)
GATE_MIN_SIGNALS = HELD_OUT_GATE_MIN_SIGNALS   # below this the held-out bucket is reported, never gated
DRAW_MIN_CODES = 8.0          # a fresh draw sits >= 8 codes from every training signal / probe drive
DRAW_SATURATION = (0.15, 1.0)  # signal-space saturation spread of the draws ((max-min)/max)
PROBES_FILE = "build_probes.ndjson"
_SEXTANTS = 6                 # the draws are hue-stratified over the six HSV sextants
_SEXTANT_NAMES = ("R-Y", "Y-G", "G-C", "C-B", "B-M", "M-R")
_CLASSES = ("held_out", "strict_held_out", "near", "coincident")


def run_seed(run_id: str) -> int:
    """The fresh-draw seed of a run — a pure function of its id (the run folder name), so every
    invocation of one run (a resume included) draws the identical set, and two runs draw apart."""
    digest = hashlib.sha256(f"dlc-verify-held-out:{run_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def to_codes(signals: Any, max_cv: int) -> np.ndarray:
    """Signals in [0, 1] → the integer output codes the display is driven at, (N, 3)."""
    arr = np.asarray(signals, dtype=float).reshape(-1, 3)
    return np.rint(np.clip(arr, 0.0, 1.0) * float(max_cv)).astype(np.int64)


def _unique_codes(codes: Any) -> np.ndarray:
    arr = np.asarray(codes, dtype=np.int64).reshape(-1, 3)
    return np.unique(arr, axis=0) if len(arr) else arr


def min_chebyshev(points: Any, refs: Any) -> np.ndarray:
    """Per row of ``points`` (codes), the Chebyshev (max-abs-channel) distance to the nearest row
    of ``refs`` (codes). ``inf`` when there are no refs."""
    pts = np.asarray(points, dtype=np.int64).reshape(-1, 3)
    ref = np.asarray(refs, dtype=np.int64).reshape(-1, 3)
    if not len(ref):
        return np.full(len(pts), np.inf)
    out = np.empty(len(pts), dtype=float)
    for start in range(0, len(pts), 256):          # bounded memory: 256 × refs × 3
        block = pts[start:start + 256]
        out[start:start + len(block)] = np.abs(block[:, None, :] - ref[None, :, :]).max(axis=2).min(axis=1)
    return out


def lattice_distance_codes(signals: Any, lattice_size: int, max_cv: int) -> np.ndarray:
    """Per signal, the largest per-channel distance (in output codes) to the nearest coordinate of
    an ``lattice_size``-node cube axis — ``<= LATTICE_CODES`` on every channel ⇒ on a lattice NODE
    (the cube output there is a solved node value, not an interpolation)."""
    n = int(lattice_size)
    x = np.clip(np.asarray(signals, dtype=float).reshape(-1, 3), 0.0, 1.0) * (n - 1)
    return np.max(np.abs(x - np.rint(x)), axis=1) * float(max_cv) / (n - 1)


def load_training(run_root: Path) -> dict[str, Any]:
    """Every signal of a run's NON-verify measurement TI3s (``measurements/*.ti3`` except
    ``verify*``): the post-MHC build set, the raw MHC foundation, the refine rounds, a grayscale-wb
    tune — whatever this run measured to build what the verify measures. ``signals`` (N, 3) in
    [0, 1]; ``sources`` lists each file and its read count (an unreadable file is listed, not fatal)."""
    meas = Path(run_root) / "measurements"
    signals: list[tuple[float, float, float]] = []
    sources: list[dict[str, Any]] = []
    for path in sorted(meas.glob("*.ti3")) if meas.is_dir() else []:
        if path.name.lower().startswith("verify"):
            continue
        try:
            rows = [s.rgb for s in parse_ti3(path)]
        except (OSError, ValueError) as exc:
            sources.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        signals.extend(rows)
        sources.append({"file": path.name, "reads": len(rows)})
    return {"signals": np.asarray(signals, dtype=float).reshape(-1, 3), "sources": sources}


def append_probe_drives(path: Path, codes: Iterable[Sequence[int]], **extra: Any) -> None:
    """Append build-probe drives (integer codes) to the run's probe ledger, one NDJSON row each —
    incremental, so a crash mid-build keeps every drive already read."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for code in codes:
            fh.write(json.dumps({"rgb": [int(c) for c in code], **extra}, separators=(",", ":")) + "\n")


def load_probe_drives(run_root: Path, *, attempt: Optional[int] = None) -> tuple[np.ndarray, dict[str, Any]]:
    """The build-probe DRIVES of a run (unique integer codes, (N, 3)) — only successful reads (a
    failed probe aborts the build and is never folded).

    ``attempt`` (the live build's ``probe_attempt``, from its stage record): only the probe-ledger
    rows tagged with it (``measurements/build_probes.ndjson``) — a re-run / resumed build starts its
    training afresh from the post-MHC set, so a superseded attempt's drives are not in the live cube's
    training (they stay in the ledger as evidence; ``superseded_rows`` counts them). Without a live
    tag (a run from before the tagged ledger), or when the live attempt left no row: every recorded
    probe read — the ledger ∪ the ``events.jsonl`` probe ``patch_read`` rows, deduped — a superset of
    the live training, i.e. conservative for the held-out claim (``scope`` says which)."""
    root = Path(run_root)
    live: set[tuple[int, int, int]] = set()
    every: set[tuple[int, int, int]] = set()
    info: dict[str, Any] = {"attempt": attempt, "ledger_rows": 0, "superseded_rows": 0, "event_rows": 0}
    ledger = root / "measurements" / PROBES_FILE
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                rgb = row.get("rgb")
            except (ValueError, AttributeError):
                continue
            if not (isinstance(rgb, list) and len(rgb) == 3):
                continue
            code = tuple(int(c) for c in rgb)
            every.add(code)  # type: ignore[arg-type]
            if attempt is not None and row.get("attempt") == attempt:
                live.add(code)  # type: ignore[arg-type]
                info["ledger_rows"] += 1
            elif attempt is not None:
                info["superseded_rows"] += 1
            else:
                info["ledger_rows"] += 1
    if attempt is not None and live:
        info["scope"] = f"build attempt {attempt} (the live cube's training)"
        info["unique"] = len(live)
        return np.asarray(sorted(live), dtype=np.int64).reshape(-1, 3), info
    events = root / "events.jsonl"
    if events.is_file():
        with events.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"probe"' not in line or "patch_read" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                data = ev.get("data") or {}
                if ev.get("event") != "patch_read" or data.get("role") != "probe" or data.get("ok") is False:
                    continue
                rgb = data.get("rgb")
                if isinstance(rgb, list) and len(rgb) == 3:
                    every.add(tuple(int(c) for c in rgb))  # type: ignore[arg-type]
                    info["event_rows"] += 1
    info["scope"] = ("every recorded probe read (no live-attempt tag) — a conservative superset"
                     if attempt is None else
                     f"build attempt {attempt} left no ledger row — every recorded probe read "
                     "(a conservative superset)")
    info["unique"] = len(every)
    return np.asarray(sorted(every), dtype=np.int64).reshape(-1, 3), info


def training_key(training: Mapping[str, Any], *, max_cv: int) -> Optional[str]:
    """A content fingerprint of a :func:`training_context` — the training codes, the probe drives and
    the cube. The fresh draws are drawn AGAINST a training set: a different key means the memoised
    draws were drawn against a superseded one (re-plan, forced / resumed re-build, re-measured
    post-MHC). ``None`` for an unavailable context."""
    if not training.get("available"):
        return None
    h = hashlib.sha256()
    h.update(_unique_codes(to_codes(training["training_signals"], max_cv)).tobytes())
    h.update(b"|")
    h.update(_unique_codes(training["probe_drives"]).tobytes())
    h.update(b"|")
    cube = training.get("cube")
    if cube is not None:
        h.update(np.round(np.asarray(cube, dtype=float), 6).tobytes())
    return h.hexdigest()[:16]


def load_cube(path: Optional[Path]) -> Optional[np.ndarray]:
    """A ``.cube`` as an ``(n, n, n, 3)`` array indexed ``[b, g, r]`` (the layout
    :func:`dlc.optimize.sample_cube` takes), or ``None`` when absent / unparseable."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    from .lut_integrity import parse_cube

    data = parse_cube(p)
    n = int(data.size)
    if n < 2 or data.parse_errors or len(data.values) != n ** 3:
        return None
    return np.asarray(data.values, dtype=float).reshape(n, n, n, 3)


def _sampler(sample: Optional[Callable[[np.ndarray, np.ndarray], np.ndarray]]):
    if sample is not None:
        return sample
    from .optimize import sample_cube   # the production sampler (whatever interpolation it uses)

    return sample_cube


def _finite(v: float) -> Optional[float]:
    return None if not np.isfinite(v) else round(float(v), 2)


def thresholds(*, bit_depth: int, lattice_size: int) -> dict[str, Any]:
    """The classification constants, stated (seam evidence)."""
    return {"distance": "chebyshev, output codes", "bit_depth": int(bit_depth),
            "max_cv": (1 << int(bit_depth)) - 1,
            "held_out_gt_codes": HELD_OUT_CODES, "coincident_le_codes": COINCIDENT_CODES,
            "lattice_size": int(lattice_size), "on_lattice_le_codes": LATTICE_CODES,
            "gate_min_signals": GATE_MIN_SIGNALS}


def classify_signals(signals: Any, *, max_cv: int, training: Any = None, probe_drives: Any = None,
                     cube: Optional[np.ndarray] = None, lattice_size: Optional[int] = None,
                     sample: Optional[Callable[[np.ndarray, np.ndarray], np.ndarray]] = None
                     ) -> list[dict[str, Any]]:
    """Classify verify ``signals`` (N, 3 in [0, 1]) against a run's training set.

    The reference set is the TRAINING set = ``training`` signals (to codes) ∪ ``probe_drives``
    (codes). ``d_in`` = Chebyshev code distance of the signal to it; ``d_drive`` = the same for the
    signal's cube drive ``sample(cube, s)`` (``None`` without a cube — an mhc-only verify). ``class``:
    ``coincident`` (min ≤ 1 code), ``held_out`` (min > 4), else ``near``; ``strict_held_out`` =
    held-out AND off the lattice (``lattice_size``, else the cube's grid, else 33). Pure."""
    sig = np.asarray(signals, dtype=float).reshape(-1, 3)
    refs_parts = []
    if training is not None and len(np.asarray(training).reshape(-1, 3)):
        refs_parts.append(to_codes(training, max_cv))
    if probe_drives is not None and len(np.asarray(probe_drives).reshape(-1, 3)):
        refs_parts.append(np.asarray(probe_drives, dtype=np.int64).reshape(-1, 3))
    refs = _unique_codes(np.vstack(refs_parts)) if refs_parts else np.zeros((0, 3), dtype=np.int64)
    sig_codes = to_codes(sig, max_cv)
    d_in = min_chebyshev(sig_codes, refs)
    drive_codes = None
    d_drive = None
    if cube is not None and len(sig):
        drive_codes = to_codes(_sampler(sample)(cube, sig), max_cv)
        d_drive = min_chebyshev(drive_codes, refs)
    n_lat = int(lattice_size or (cube.shape[0] if cube is not None else DEFAULT_LATTICE_SIZE))
    d_lat = lattice_distance_codes(sig, n_lat, max_cv)
    rows: list[dict[str, Any]] = []
    for i in range(len(sig)):
        dmin = float(d_in[i]) if d_drive is None else float(min(d_in[i], d_drive[i]))
        cls = ("coincident" if dmin <= COINCIDENT_CODES
               else "held_out" if dmin > HELD_OUT_CODES else "near")
        on_lattice = bool(d_lat[i] <= LATTICE_CODES)
        rows.append({"code": [int(c) for c in sig_codes[i]],
                     "drive": [int(c) for c in drive_codes[i]] if drive_codes is not None else None,
                     "d_in": _finite(d_in[i]),
                     "d_drive": _finite(d_drive[i]) if d_drive is not None else None,
                     "d_lattice": round(float(d_lat[i]), 2), "on_lattice": on_lattice,
                     "class": cls, "strict_held_out": cls == "held_out" and not on_lattice})
    return rows


def held_out_summary(rows: Sequence[Mapping[str, Any]], *, population: Sequence[str] = ("core",),
                     **context: Any) -> dict[str, Any]:
    """Per-signal ΔE stats per class over the gate's population. ``rows`` are
    :func:`classify_signals` rows enriched with ``de`` (the signal's per-signal ΔE), ``zone`` (its
    practical zone) and optionally ``draw`` (a fresh held-out draw). ``context`` (thresholds,
    training provenance, …) is carried into the result verbatim."""
    pop = [r for r in rows if r.get("zone") in population]
    by: dict[str, list[float]] = {k: [] for k in _CLASSES}
    for r in pop:
        by[str(r["class"])].append(float(r["de"]))
        if r.get("strict_held_out"):
            by["strict_held_out"].append(float(r["de"]))
    out: dict[str, Any] = {"available": True,
                           "population": f"{'/'.join(population)} signals, per-signal ΔE (mean of each "
                                         "signal's reads)",
                           "n_signals": len(pop), **context,
                           **{k: bucket_stats(v) for k, v in by.items()}}
    draws = [r for r in pop if r.get("draw")]
    if draws:
        out["fresh_draws"] = {**bucket_stats([float(r["de"]) for r in draws]),
                              "held_out": sum(1 for r in draws if r["class"] == "held_out")}
    return out


def _live_probe_attempt(root: Path, calib: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The live build's probe attempt from a run record (``calib``; read from ``root`` when None)."""
    if calib is None:
        try:
            calib = json.loads((Path(root) / "dlc_state.json").read_text(encoding="utf-8")).get("calib") or {}
        except (OSError, ValueError, AttributeError):
            return None
    built = ((calib.get("stages") or {}).get("build-install-3dlut") or {})
    if built.get("status") != "done":
        return None
    value = (built.get("data") or {}).get("probe_attempt")
    if value is None:
        value = (built.get("digest") or {}).get("probe_attempt")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def training_context(run_root: Path, calib: Mapping[str, Any], *, max_cv: int) -> dict[str, Any]:
    """The TRAINING set a run's verify is classified against, from its record (``calib`` = the run's
    ``dlc_state.json['calib']``): every non-verify measurement TI3 + build-probe drive of the runs that
    built what the verify measures — this run, plus the source run whose cube ``refine-mhc`` kept —
    and the cube the verify measured through (drive-space distances; none for mhc-only /
    grayscale-wb). A verify-only run built nothing: unavailable, said so (never a vacuous
    "everything is held-out"). Pure (reads files only)."""
    root = Path(run_root)
    flow = calib.get("flow")
    stages = calib.get("stages") or {}
    if flow == "verify-only":
        return {"available": False,
                "reason": "verify-only builds nothing: this run has no training set (the installed "
                          "stack was trained in another run) — no held-out classification"}
    roots: list[Path] = [root]
    cube_path: Optional[str] = None
    cube_from: Optional[str] = None
    built = stages.get("build-install-3dlut") or {}
    kept = stages.get("reapply-3dlut") or {}
    if built.get("status") == "done":
        cube_path = (built.get("data") or {}).get("cube_path") or (built.get("digest") or {}).get("cube_path")
        cube_from = "this run's build"
    elif kept.get("status") == "done":
        cube_path = (kept.get("data") or {}).get("cube_path")
        src = ((stages.get("seed-from-run") or {}).get("data") or {}).get("source_run")
        if src:
            roots.append(Path(src))
            cube_from = f"kept from source run {Path(src).name}"
    signals: list[np.ndarray] = []
    probes: list[np.ndarray] = []
    sources: list[dict[str, Any]] = []
    probe_info: dict[str, Any] = {}
    for r in roots:
        tr = load_training(r)
        signals.append(tr["signals"])
        sources += [{"run": r.name, **s} for s in tr["sources"]]
        drv, info = load_probe_drives(r, attempt=_live_probe_attempt(r, calib if r == root else None))
        probes.append(drv)
        probe_info[r.name] = info
    training = np.vstack(signals) if signals else np.zeros((0, 3))
    drives = np.vstack(probes) if probes else np.zeros((0, 3), dtype=np.int64)
    if not len(training) and not len(drives):
        return {"available": False,
                "reason": "no training measurements in this run (no non-verify TI3, no build probes)"}
    cube = load_cube(Path(cube_path)) if cube_path else None
    if cube is None and cube_path:   # a moved run folder: the cube beside THIS run dir
        cube = load_cube(root / "generated" / Path(cube_path).name)
    provenance: dict[str, Any] = {
        "sources": sources,
        "n_training_signals": int(len(_unique_codes(to_codes(training, max_cv)))) if len(training) else 0,
        "probe_drives": probe_info, "n_probe_drives": int(len(_unique_codes(drives))),
        # run-relative names only: the digest is replay-identical wherever the run folder lives
        "cube": Path(cube_path).name if cube_path else None, "cube_from": cube_from,
        "drive_space": cube is not None, "note": None}
    if cube_path and cube is None:
        provenance["note"] = "the verify's cube could not be read: signal-space distances only"
    if flow == "grayscale-wb":
        provenance["note"] = ("grayscale-wb verifies its own tune points (coincident by design); an "
                              "installed 3D LUT was trained in another run — not in this classification")
    return {"available": True, "training_signals": training, "probe_drives": drives, "cube": cube,
            "lattice_size": int(cube.shape[0]) if cube is not None else DEFAULT_LATTICE_SIZE,
            "provenance": provenance}


def held_out_view(patch_metrics: Sequence[Any], *, run_root: Path, calib: Mapping[str, Any],
                  bit_depth: int, is_hdr: bool,
                  sample: Optional[Callable[[np.ndarray, np.ndarray], np.ndarray]] = None,
                  draws: Optional[Iterable[Sequence[int]]] = None
                  ) -> tuple[dict[str, Any], Optional[list[dict[str, Any]]]]:
    """A scored verify set's held-out view (V1) — the ONE function the live verify and the score
    CLI share: group the reads per unique signal (each signal's ΔE = the mean of its reads), classify
    every signal against :func:`training_context`, and summarise per class over the gate's population
    (the practical core). Returns ``(summary, rows)``; ``rows`` (per signal: code, drive, distances,
    class, ΔE, reads, zone, fresh-draw flag) is ``None`` when there is no training to classify
    against (the summary then says why, plus the fresh draws' own stats when any were measured).
    ``draws`` (codes) flags the fresh held-out draws; default the run's own memo."""
    from .metrics import group_per_signal, practical_zone

    max_cv = (1 << int(bit_depth)) - 1
    groups = group_per_signal(list(patch_metrics))
    training = training_context(run_root, calib, max_cv=max_cv)
    th = thresholds(bit_depth=bit_depth, lattice_size=int(training.get("lattice_size") or DEFAULT_LATTICE_SIZE))
    if draws is None:   # default: the run's own draw memo
        draws = (calib.get("verify_held_out_draws") or {}).get("signals") or ()
    draw_set = {tuple(int(c) for c in p) for p in draws}
    codes = to_codes([m.rgb for m, _ in groups], max_cv) if groups else np.zeros((0, 3), dtype=np.int64)
    zones = [practical_zone(m, is_hdr=is_hdr) for m, _ in groups]
    is_draw = [tuple(int(c) for c in code) in draw_set for code in codes]
    if not training.get("available"):
        out: dict[str, Any] = {"available": False, "reason": training.get("reason"), "thresholds": th,
                               "n_signals": len(groups)}
        fresh = [float(m.de2000) for (m, _), z, d in zip(groups, zones, is_draw) if d and z == "core"]
        if fresh:
            out["fresh_draws"] = bucket_stats(fresh)
        return out, None
    rows = classify_signals([m.rgb for m, _ in groups], max_cv=max_cv,
                            training=training["training_signals"], probe_drives=training["probe_drives"],
                            cube=training["cube"], lattice_size=training["lattice_size"], sample=sample)
    for row, (m, n), zone, draw in zip(rows, groups, zones, is_draw):
        row.update(signal=[round(float(c), 6) for c in m.rgb], de=float(m.de2000),
                   n_reads=n, zone=zone, draw=draw)
    return held_out_summary(rows, thresholds=th, training=training["provenance"]), rows


def draw_held_out_signals(n: int, *, seed: int, max_cv: int, value_floor_cv: int,
                          cap_cv: Optional[int] = None, exclude_codes: Any = None,
                          existing: Iterable[Sequence[int]] = (), cube: Optional[np.ndarray] = None,
                          lattice_size: int = DEFAULT_LATTICE_SIZE, min_codes: float = DRAW_MIN_CODES,
                          saturation: tuple[float, float] = DRAW_SATURATION,
                          sample: Optional[Callable[[np.ndarray, np.ndarray], np.ndarray]] = None,
                          max_attempts: Optional[int] = None) -> dict[str, Any]:
    """Draw up to ``n`` fresh verify signals (integer codes), deterministic in ``seed`` + inputs.

    Each candidate: hue uniform WITHIN a hue sextant (R-Y, Y-G, G-C, C-B, B-M, M-R) — the draws are
    hue-STRATIFIED: each sextant gets an equal quota (24 → 4 each) and candidates cycle over the
    sextants still short of theirs, so no hue family is left unsampled by chance — saturation
    uniform in ``saturation`` (signal-space
    ``(max-min)/max``), value (= the on-channel code) uniform in ``[value_floor_cv, cap_cv]`` — the
    verify's colour floor (above the shadow band) up to its range cap — so dim and half-saturated
    colours are represented, not just the gamut shell. Rejected when: below the floor after rounding,
    a duplicate of an ``existing`` verify signal or an earlier draw, on a cube lattice node (every
    channel within 1 code), closer than ``min_codes`` (Chebyshev) to any ``exclude_codes`` (training
    signals ∪ probe drives) or to an earlier draw, or — with a ``cube`` — its DRIVE closer than
    ``min_codes`` to the exclusion set. Three uniforms per candidate whatever the verdict, so the
    stream (and the result) depends only on the seed and the inputs."""
    cap = int(cap_cv if cap_cv is not None else max_cv)
    floor = max(1, int(value_floor_cv))
    lo_s, hi_s = (float(saturation[0]), float(saturation[1]))
    rng = random.Random(int(seed))
    excl = _unique_codes(exclude_codes) if exclude_codes is not None else np.zeros((0, 3), dtype=np.int64)
    seen = {tuple(int(c) for c in p) for p in existing}
    chosen: list[tuple[int, int, int]] = []
    rejected: dict[str, int] = {}
    limit = int(max_attempts if max_attempts is not None else 500 * max(int(n), 1))
    sampler = _sampler(sample) if cube is not None else None
    attempts = 0      # candidates GENERATED (the stream position; bounded by ``limit``)
    examined = 0      # candidates actually judged (the report)
    quota = [int(n) // _SEXTANTS + (1 if k < int(n) % _SEXTANTS else 0) for k in range(_SEXTANTS)]
    filled = [0] * _SEXTANTS
    cursor = 0

    def reject(why: str) -> None:
        rejected[why] = rejected.get(why, 0) + 1

    while len(chosen) < n and attempts < limit:
        block = []
        sextant_of = []
        open_sextants = [k for k in range(_SEXTANTS) if filled[k] < quota[k]] or list(range(_SEXTANTS))
        for _ in range(min(64, limit - attempts)):
            sextant = open_sextants[cursor % len(open_sextants)]
            cursor += 1
            h_u, s_u, v_u = rng.random(), rng.random(), rng.random()
            h = (sextant + h_u) / _SEXTANTS
            sat = lo_s + (hi_s - lo_s) * s_u
            val = (floor + (cap - floor) * v_u) / float(max_cv)
            rgb = colorsys.hsv_to_rgb(h, sat, val)
            block.append(tuple(int(round(c * max_cv)) for c in rgb))
            sextant_of.append(sextant)
        attempts += len(block)
        cand = np.asarray(block, dtype=np.int64).reshape(-1, 3)
        sig = cand / float(max_cv)
        d_lat = lattice_distance_codes(sig, lattice_size, max_cv)
        d_ex = min_chebyshev(cand, excl)
        d_drive = (min_chebyshev(to_codes(sampler(cube, sig), max_cv), excl)
                   if sampler is not None else None)
        for i, code in enumerate(block):
            if len(chosen) >= n:
                break
            examined += 1
            if filled[sextant_of[i]] >= quota[sextant_of[i]]:
                reject("hue_sextant_full")
            elif max(code) < floor or max(code) > cap:
                reject("value_range")
            elif code in seen:
                reject("duplicate")
            elif d_lat[i] <= LATTICE_CODES:
                reject("on_lattice")
            elif d_ex[i] < min_codes:
                reject("near_training")
            elif d_drive is not None and d_drive[i] < min_codes:
                reject("near_training_drive")
            elif chosen and float(min_chebyshev([code], chosen)[0]) < min_codes:
                reject("near_other_draw")
            else:
                chosen.append(code)
                seen.add(code)
                filled[sextant_of[i]] += 1
    return {"signals": [list(c) for c in chosen], "n_requested": int(n), "n_drawn": len(chosen),
            "seed": int(seed), "attempts": examined, "rejected": rejected,
            "min_codes": float(min_codes), "value_range_codes": [floor, cap],
            "saturation_range": [lo_s, hi_s], "lattice_size": int(lattice_size),
            "drive_space_checked": cube is not None, "n_exclusion_codes": int(len(excl)),
            "per_hue_sextant": {name: filled[k] for k, name in enumerate(_SEXTANT_NAMES)}}
