"""Thermal-state alignment of one measured stage to ONE reference state (plan item 3).

The problem (PA32UCXR, 2026-09-03, QD mini-LED): the panel's channel balance integrates its
load history — the interleaved neutral reference drifted ~0.003–0.005 x over a stage while the
individual patch reads were fine. A stage measured across that drift bakes every patch at a
DIFFERENT thermal state into one dataset, and a build fitted to it carries the drift as ripple
(the grey-ramp "stripes"). The measure loop already interleaves a fixed neutral reference read
throughout the stage (``role`` ``neutral_ref`` / ``warmup`` in the stage's NDJSON), so the drift
is *observed* — this module turns that track into a per-read correction:

1. Express every read in the panel's linear native-RGB basis (``P⁻¹ · XYZ``).
2. From the reference reads, build the **balance track** — the normalized per-channel share
   at each reference time (a pure function of the panel's thermal state at that moment).
3. For each measurement read, interpolate the balance at its timestamp, compute the per-channel
   gain that maps it to the CHOSEN state (``end`` / ``start`` / ``mid`` of the track), scale the
   read's linear RGB, and go back to XYZ.

A per-channel gain in the native basis is the physics of a backlight/emitter balance shift (it
is what a global thermal state does to every colour), so the correction is exact for the
balance term and first-order for everything else. Reads below ``min_nits`` are left alone
(noise floor). Post-MHC reads (measured THROUGH the matrix) are corrected in the same native
basis: the panel's balance shift is upstream of the matrix, and the reference reads see it
through the same stack, so the gain is still the right operator.

The choice of state is NOT mechanical — it is what the calibration is aligned to, and the seam
(:meth:`Calibration._thermal_align_gate`) decides it with the evidence :func:`evaluate` packs:
the track's span vs its own read noise, and the correction magnitude each option implies.

Offline form (the 2026-09-03 gate run): ``agentexp_thermal_align_raw.py``; this module is the
same maths, generalized to any stage (raw / post-mhc), any bit depth, and idempotent apply.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from .colormath import invert3x3, matvec, rgb_to_xyz_matrix

REF_ROLES = ("neutral_ref", "warmup")
ALIGN_CHOICES = ("end", "start", "mid")
DEFAULT_MIN_NITS = 1.0
DEFAULT_MIN_SPAN_X = 0.0008     # below this the balance track is flat for any purpose
DEFAULT_NOISE_MULT = 3.0        # ...or within this many robust σ of the reference read noise

_TI3_RE = re.compile(r"BEGIN_DATA_FORMAT\s+(.*?)\s+END_DATA_FORMAT.*?BEGIN_DATA[ \t]*\r?\n(.*?)END_DATA", re.S)


# ---------------------------------------------------------------------------
# basis + reads
# ---------------------------------------------------------------------------

def basis_from_primaries(primaries: dict[str, Any], white_xy: Sequence[float]) -> tuple[list[list[float]], list[list[float]]]:
    """``(P, P⁻¹)`` for a linear-RGB basis from ``{rx,ry,gx,gy,bx,by}`` (or ``{R:[x,y],...}``)
    + a white. Any consistent basis near the panel's works (the correction is a per-channel gain
    in it); the panel's native primaries are the natural choice."""
    if "rx" in primaries:
        p = {k: float(primaries[k]) for k in ("rx", "ry", "gx", "gy", "bx", "by")}
    else:
        p = {"rx": float(primaries["R"][0]), "ry": float(primaries["R"][1]),
             "gx": float(primaries["G"][0]), "gy": float(primaries["G"][1]),
             "bx": float(primaries["B"][0]), "by": float(primaries["B"][1])}
    P = rgb_to_xyz_matrix(p["rx"], p["ry"], p["gx"], p["gy"], p["bx"], p["by"],
                          float(white_xy[0]), float(white_xy[1]), white_Y=1.0)
    return P, invert3x3(P)


def load_reads(ndjson_path: Path | str) -> list[dict[str, Any]]:
    """Every read with an XYZ + timestamp: measurements and the interleaved references."""
    rows: list[dict[str, Any]] = []
    text = Path(ndjson_path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict) or not e.get("xyz") or not e.get("t"):
            continue
        if e.get("role") not in REF_ROLES and e.get("role") != "measurement":
            continue
        try:
            e["ts"] = datetime.fromisoformat(str(e["t"])).timestamp()
        except ValueError:
            continue
        rows.append(e)
    return rows


# ---------------------------------------------------------------------------
# the reference balance track
# ---------------------------------------------------------------------------

@dataclass
class Track:
    rgb: list[int]                       # the reference patch (code values)
    rows: list[tuple[float, list[float], list[float]]]   # (ts, balance[3], xyz)

    @property
    def t0(self) -> float:
        return self.rows[0][0]

    @property
    def t1(self) -> float:
        return self.rows[-1][0]

    def balance_at(self, ts: float) -> list[float]:
        rows = self.rows
        if ts <= rows[0][0]:
            return rows[0][1]
        if ts >= rows[-1][0]:
            return rows[-1][1]
        for k in range(1, len(rows)):
            if rows[k][0] >= ts:
                (ta, ba, _), (tb, bb, _) = rows[k - 1], rows[k]
                f = (ts - ta) / max(tb - ta, 1e-9)
                return [ba[i] + (bb[i] - ba[i]) * f for i in range(3)]
        return rows[-1][1]

    def targets(self) -> dict[str, list[float]]:
        return {"end": list(self.rows[-1][1]), "start": list(self.rows[0][1]),
                "mid": [statistics.mean(b[i] for _, b, _ in self.rows) for i in range(3)]}


def reference_track(reads: Sequence[dict[str, Any]], Pinv: list[list[float]],
                    *, min_reads: int = 3) -> Optional[Track]:
    """The most-read reference patch's balance over time (``None`` with fewer than
    ``min_reads`` reads — no track, nothing to align to)."""
    refs = [r for r in reads if r.get("role") in REF_ROLES and r.get("rgb")]
    if len(refs) < min_reads:
        return None
    counts: dict[tuple[int, ...], int] = {}
    for r in refs:
        key = tuple(int(v) for v in r["rgb"])
        counts[key] = counts.get(key, 0) + 1
    ref_rgb = max(counts.items(), key=lambda kv: kv[1])[0]
    rows = []
    for r in refs:
        if tuple(int(v) for v in r["rgb"]) != ref_rgb:
            continue
        lin = matvec(Pinv, r["xyz"])
        s = sum(lin)
        if not (s > 0) or any(v < 0 for v in lin) or r["xyz"][1] <= 0:
            continue
        rows.append((float(r["ts"]), [v / s for v in lin], [float(v) for v in r["xyz"]]))
    if len(rows) < min_reads:
        return None
    rows.sort(key=lambda t: t[0])
    return Track(rgb=list(ref_rgb), rows=rows)


def _xy(xyz: Sequence[float]) -> tuple[float, float]:
    s = sum(xyz)
    return (xyz[0] / s, xyz[1] / s) if s > 0 else (0.0, 0.0)


def track_stats(track: Track) -> dict[str, Any]:
    """Span / drift / robust read noise of the reference track in chromaticity x (the axis the
    PA32UCXR's balance drift lives on) + y. ``noise_x`` is the robust σ of one reference read
    (detrended second differences), so the span can be judged against the reference's OWN
    repeatability, not a hard-wired tolerance."""
    xs = [_xy(xyz)[0] for _, _, xyz in track.rows]
    ys = [_xy(xyz)[1] for _, _, xyz in track.rows]
    # Read noise from SECOND differences (a linear trend cancels; a slow monotone drift must
    # not masquerade as noise): σ ≈ 1.4826·MAD(Δ²x)/√6 for i.i.d. noise. Falls back to first
    # differences (/√2) on a 2-point track.
    if len(xs) >= 3:
        d2 = [abs(xs[i + 1] - 2.0 * xs[i] + xs[i - 1]) for i in range(1, len(xs) - 1)]
        noise_x = 1.4826 * statistics.median(d2) / math.sqrt(6.0)
    else:
        diffs = [abs(xs[i] - xs[i - 1]) for i in range(1, len(xs))]
        noise_x = 1.4826 * statistics.median(diffs) / math.sqrt(2.0) if diffs else 0.0
    n = len(xs)
    tail = xs[-min(5, n):]
    return {
        "reference_rgb": list(track.rgb), "n": n,
        "minutes": round((track.t1 - track.t0) / 60.0, 2),
        "start_xy": [round(xs[0], 5), round(ys[0], 5)],
        "end_xy": [round(xs[-1], 5), round(ys[-1], 5)],
        "mean_xy": [round(statistics.mean(xs), 5), round(statistics.mean(ys), 5)],
        "span_x": round(max(xs) - min(xs), 5), "span_y": round(max(ys) - min(ys), 5),
        "drift_x": round(xs[-1] - xs[0], 5),
        "noise_x": round(noise_x, 6),
        "tail_span_x": round(max(tail) - min(tail), 5),
        "luminance_nits": [round(track.rows[0][2][1], 3), round(track.rows[-1][2][1], 3)],
    }


# ---------------------------------------------------------------------------
# corrections
# ---------------------------------------------------------------------------

def _read_key(r: dict[str, Any]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    sig = r.get("signal")
    if not sig:
        # older NDJSON without the normalized signal: fall back to the code values themselves
        sig = [float(v) for v in r["rgb"]]
    return (tuple(round(float(v), 4) for v in sig), tuple(round(float(v), 6) for v in r["xyz"]))


def corrections(reads: Sequence[dict[str, Any]], P: list[list[float]], Pinv: list[list[float]],
                track: Track, target: Sequence[float], *, min_nits: float = DEFAULT_MIN_NITS
                ) -> tuple[dict[tuple, list[float]], list[float]]:
    """Per-measurement corrected XYZ (keyed by ``(signal, xyz)`` so the TI3 row can be matched)
    + the |Δx| each correction moved its read by."""
    corr: dict[tuple, list[float]] = {}
    mags: list[float] = []
    for r in reads:
        if r.get("role") != "measurement" or float(r["xyz"][1]) < min_nits:
            continue
        b = track.balance_at(float(r["ts"]))
        if any(v <= 0 for v in b):
            continue
        gain = [target[i] / b[i] for i in range(3)]
        lin = matvec(Pinv, r["xyz"])
        xyz2 = matvec(P, [lin[i] * gain[i] for i in range(3)])
        corr[_read_key(r)] = [float(v) for v in xyz2]
        x1, x2 = _xy(r["xyz"])[0], _xy(xyz2)[0]
        mags.append(abs(x2 - x1))
    return corr, mags


def _parse_ti3(text: str):
    m = _TI3_RE.search(text)
    if not m:
        raise ValueError("TI3: no BEGIN_DATA_FORMAT/BEGIN_DATA block")
    fields = m.group(1).split()
    try:
        cols = tuple(fields.index(k) for k in ("RGB_R", "RGB_G", "RGB_B", "XYZ_X", "XYZ_Y", "XYZ_Z"))
    except ValueError as exc:
        raise ValueError(f"TI3: missing column ({exc})") from exc
    return m, fields, cols


def rewrite_ti3_text(text: str, corr: dict[tuple, list[float]]) -> tuple[str, int, int]:
    """Replace the XYZ of every TI3 row whose (signal, XYZ) matches a corrected read. The TI3
    holds the ACCEPTED read per patch (the NDJSON every read), so match by signal + closest XYZ
    within 2 % of Y. Returns ``(new_text, rows_corrected, rows_untouched)``."""
    m, fields, (ir, ig, ib, ix, iy, iz) = _parse_ti3(text)
    by_signal: dict[tuple[float, ...], list[tuple[tuple[float, ...], list[float]]]] = {}
    for (sig, xyz), v in corr.items():
        by_signal.setdefault(sig, []).append((xyz, v))
    out: list[str] = []
    hit = miss = 0
    for ln in m.group(2).splitlines():
        parts = ln.split()
        if len(parts) < len(fields):
            out.append(ln)
            continue
        sig = tuple(round(float(parts[i]) / 100.0, 4) for i in (ir, ig, ib))
        xyz = tuple(float(parts[i]) for i in (ix, iy, iz))
        cands = by_signal.get(sig)
        if not cands:
            miss += 1
            out.append(ln)
            continue
        kxyz, v = min(cands, key=lambda kv: sum((kv[0][i] - xyz[i]) ** 2 for i in range(3)))
        if sum((kxyz[i] - xyz[i]) ** 2 for i in range(3)) > (0.02 * max(xyz[1], 0.01)) ** 2 + 1e-6:
            miss += 1
            out.append(ln)
            continue
        parts[ix], parts[iy], parts[iz] = f"{v[0]:.6f}", f"{v[1]:.6f}", f"{v[2]:.6f}"
        out.append(" ".join(parts))
        hit += 1
    new = text[: m.start(2)] + "\n".join(out) + "\n" + text[m.end(2):]
    return new, hit, miss


# ---------------------------------------------------------------------------
# evidence + apply
# ---------------------------------------------------------------------------

def _note_path(ti3_path: Path) -> Path:
    return ti3_path.with_name(ti3_path.stem + "_thermal_align.json")


def _backup_path(ti3_path: Path) -> Path:
    return ti3_path.with_name(ti3_path.name + ".orig")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def backup_state(ti3_path: Path) -> str:
    """How the on-disk backup/note relate to the CURRENT TI3 content:

    * ``"none"``     — no backup/note (never aligned).
    * ``"aligned"``  — the current TI3 is the aligned output the note describes.
    * ``"original"`` — the current TI3 is the original the backup holds (alignment undone / never rewritten).
    * ``"stale"``    — neither: the TI3 was RE-MEASURED in place after an alignment (same path, new
      content); the backup and note describe data that no longer exists.

    A stale backup must never be used as "the original" and a stale note never replayed — a
    re-measure (escalation ``remeasure`` / adaptive-planning invalidation) overwrites the stage
    files at their fixed path without touching these siblings (adversarial review, 2026-09-03).
    """
    bak, note_p = _backup_path(ti3_path), _note_path(ti3_path)
    if not bak.exists() and not note_p.exists():
        return "none"
    cur = _sha(ti3_path.read_text(encoding="utf-8", errors="replace")) if ti3_path.exists() else None
    note = note_for(ti3_path) or {}
    if cur and note.get("aligned_sha") == cur:
        return "aligned"
    if bak.exists() and cur == _sha(bak.read_text(encoding="utf-8", errors="replace")):
        return "original"
    return "stale"


def discard_backup(ti3_path: Path) -> bool:
    """Remove a backup + note that no longer describe the current TI3 (or after an invalidation
    that will re-measure into the same path). Returns whether anything was removed."""
    removed = False
    for q in (_backup_path(ti3_path), _note_path(ti3_path)):
        if q.exists():
            q.unlink()
            removed = True
    return removed


def original_ti3_text(ti3_path: Path) -> str:
    """The stage's text BEFORE any alignment: the backup when the current file is its aligned
    output (or the backup itself), else the CURRENT file — a stale backup from a superseded
    measurement is discarded, never trusted."""
    state = backup_state(ti3_path)
    if state == "stale":
        discard_backup(ti3_path)
    bak = _backup_path(ti3_path)
    if state in ("aligned", "original") and bak.exists():
        return bak.read_text(encoding="utf-8", errors="replace")
    return ti3_path.read_text(encoding="utf-8", errors="replace")


def evaluate(ndjson_path: Path | str, ti3_path: Path | str, primaries: dict[str, Any],
             white_xy: Sequence[float], *, min_nits: float = DEFAULT_MIN_NITS,
             min_span_x: float = DEFAULT_MIN_SPAN_X, noise_mult: float = DEFAULT_NOISE_MULT
             ) -> dict[str, Any]:
    """The evidence packet for the seam: the reference track's span vs its own noise, and what
    each alignment option would move the dataset by. Never raises for a missing track (an SDR
    stage without references, a short stage) — ``available: False`` + the reason."""
    ndjson_path, ti3_path = Path(ndjson_path), Path(ti3_path)
    out: dict[str, Any] = {"available": False, "reason": None, "significant": False,
                           "ndjson": str(ndjson_path), "ti3": str(ti3_path)}
    if not ndjson_path.exists() or not ti3_path.exists():
        out["reason"] = "stage NDJSON/TI3 missing"
        return out
    try:
        P, Pinv = basis_from_primaries(primaries, white_xy)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        out["reason"] = f"no usable linear basis ({exc})"
        return out
    reads = load_reads(ndjson_path)
    track = reference_track(reads, Pinv)
    if track is None:
        out["reason"] = "fewer than 3 reference reads — no balance track"
        return out
    stats = track_stats(track)
    threshold = max(float(min_span_x), float(noise_mult) * float(stats["noise_x"]))
    significant = stats["span_x"] > threshold
    options: dict[str, Any] = {}
    text = original_ti3_text(ti3_path)
    for name, target in track.targets().items():
        corr, mags = corrections(reads, P, Pinv, track, target, min_nits=min_nits)
        try:
            _new, hit, miss = rewrite_ti3_text(text, corr)
        except ValueError:
            hit, miss = 0, 0
        options[name] = {"target_balance": [round(v, 6) for v in target],
                         "dx_mean": round(statistics.mean(mags), 6) if mags else 0.0,
                         "dx_max": round(max(mags), 6) if mags else 0.0,
                         "reads": len(corr), "rows_matched": hit, "rows_untouched": miss}
    out.update({
        "available": True, "track": stats, "threshold_x": round(threshold, 6),
        "ti3_sha": _sha(text),
        "significant": bool(significant), "options": options, "min_nits": min_nits,
        "basis": ("reference span " + f"{stats['span_x']:.4f} x over {stats['minutes']:.1f} min "
                  + ("EXCEEDS" if significant else "is within") + f" max({min_span_x}, {noise_mult:g}×noise "
                  f"{stats['noise_x']:.5f}) = {threshold:.4f}"),
        # The state the NEXT stage begins in is the end of this one: aligning to it makes the
        # build consistent with what its refine/verify will measure minutes later. 'mid' is
        # the middle-ground state (a stack viewed under average load); 'start' the cold end.
        "recommendation": "end" if significant else None,
    })
    return out


def apply(ti3_path: Path | str, ndjson_path: Path | str, primaries: dict[str, Any],
          white_xy: Sequence[float], align: str, *, min_nits: float = DEFAULT_MIN_NITS,
          decided_by: Optional[str] = None) -> dict[str, Any]:
    """Rewrite the stage TI3 aligned to ``align`` (``end``/``start``/``mid``) — or restore the
    original for ``none``. Idempotent: the first apply backs the original up as ``<ti3>.orig``
    and every apply starts from that original (a changed choice is never compounded). The note
    ``<stage>_thermal_align.json`` records what was done for the run record + report."""
    ti3_path, ndjson_path = Path(ti3_path), Path(ndjson_path)
    note_path = _note_path(ti3_path)
    bak = _backup_path(ti3_path)
    stamp = datetime.now().isoformat(timespec="seconds")
    state = backup_state(ti3_path)
    if state == "stale":
        # The TI3 was re-measured after an alignment: the old backup/note describe data that is
        # gone. Start over from the CURRENT content (never replay the stale note).
        discard_backup(ti3_path)
    if align == "none":
        restored = False
        if bak.exists():
            shutil.copy2(bak, ti3_path)
            restored = True
        note = {"align": "none", "applied": stamp, "restored_original": restored,
                "decided_by": decided_by, "ti3": str(ti3_path)}
        note_path.write_text(json.dumps(note, indent=1), encoding="utf-8")
        return note
    if align not in ALIGN_CHOICES:
        raise ValueError(f"align must be one of {ALIGN_CHOICES + ('none',)}, got {align!r}")
    if state == "aligned" and note_path.exists() and bak.exists():
        # The current file IS the aligned output of this backup (content-verified) — a replay.
        try:
            prev = json.loads(note_path.read_text(encoding="utf-8"))
        except ValueError:
            prev = {}
        if prev.get("align") == align and prev.get("rows_corrected"):
            prev["idempotent_replay"] = True
            return prev
    P, Pinv = basis_from_primaries(primaries, white_xy)
    reads = load_reads(ndjson_path)
    track = reference_track(reads, Pinv)
    if track is None:
        raise ValueError("no reference track to align to")
    target = track.targets()[align]
    corr, mags = corrections(reads, P, Pinv, track, target, min_nits=min_nits)
    if not bak.exists():
        shutil.copy2(ti3_path, bak)
    text = bak.read_text(encoding="utf-8", errors="replace")
    new, hit, miss = rewrite_ti3_text(text, corr)
    ti3_path.write_text(new, encoding="utf-8")
    stats = track_stats(track)
    note = {"align": align, "applied": stamp, "decided_by": decided_by,
            "orig_sha": _sha(text), "aligned_sha": _sha(new),
            "target_balance": [round(v, 6) for v in target], "reference_rgb": list(track.rgb),
            "track_n": stats["n"], "track_minutes": stats["minutes"], "span_x": stats["span_x"],
            "rows_corrected": hit, "rows_untouched": miss, "reads_corrected": len(corr),
            "dx_mean": round(statistics.mean(mags), 6) if mags else 0.0,
            "dx_max": round(max(mags), 6) if mags else 0.0,
            "min_nits": min_nits, "backup": str(bak), "ti3": str(ti3_path)}
    note_path.write_text(json.dumps(note, indent=1), encoding="utf-8")
    return note


def note_for(ti3_path: Path | str) -> Optional[dict[str, Any]]:
    p = _note_path(Path(ti3_path))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
