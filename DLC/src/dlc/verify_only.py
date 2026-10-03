"""Helpers for the ``verify-only`` flow — measure an installed (or candidate) stack against a
verify set without building or committing anything.

Why (HANDOFF 2026-09-24): several hardware acceptances only need to MEASURE a stack — the D1
projection-cube acceptance on the PA32UCXR HDR, re-verifying a stack after a change, owner
A/Bs — and the only route used to be a multi-hour ``3dlut-only`` rebuild.

The orchestrator (:mod:`dlc.calibrate`) owns the stages and seams; this module holds the pure,
dependency-free mechanics they share with the CLI (``--preview-patches`` / ``--abort``):

* :func:`load_source_verify` — a recorded run's EXACT verify patch list + its recorded verify
  digest + the scoring basis it was scored under (``--verify-patches-from``). The patch list is
  persisted by every measure stage in ``measurements/verify.ndjson`` (one row per read, each
  measurement read labelled ``p<index>`` with its integer code values) — the primary source;
  ``verify.ti3`` (accepted reads, RGB as 0-100 % signal) is the cross-check / fallback. Both
  are checked against the stage digest's recorded ``patch_count``.
* :func:`source_mismatches` — the provable differences between the source run and this one
  (hard: the codes mean another signal; soft: a judgment for the LLM).
* :func:`compare_verify` — per-bucket deltas (the scorer's headline + ``practical`` buckets +
  luminance bands) and per-patch movers vs the source's recorded verify.
* :func:`restore_candidate` — put the prior runtime cube back after a candidate install; shared
  by the flow's own abort path and the CLI ``--abort`` of a paused run.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

__all__ = [
    "PREHEAT_POLICIES",
    "SourceRunError",
    "load_source_verify",
    "patches_fingerprint",
    "source_mismatches",
    "compare_verify",
    "restore_candidate",
    "candidate_cube_facts",
]

# The thermal preheat policy vocabulary (MeasureLoopConfig.preheat) the --preheat lever exposes.
PREHEAT_POLICIES = ("auto", "always", "never")

# The headline numbers every verify digest carries (the scorer's summary) and the practical
# buckets (metrics.practical_summary): core / limits / clamped / tube + the luminance bands.
_HEADLINE_KEYS = ("avg_de2000", "p95_de2000", "max_de2000", "white_de2000", "grayscale_avg_de2000")
_BUCKETS = ("core", "limits", "clamped", "tube")
_STATS = ("avg", "p95", "max")
_PEAK_REL_TOL = 0.005          # the stack-registry re-pin tolerance (a cap within a code step)
_WHITE_XY_TOL = 1e-4           # resolved-white agreement (well under any perceptible shift)
_MOVERS = 10                   # per-patch movers listed each way


class SourceRunError(Exception):
    """The source run cannot supply a verify set (unreadable record, no completed verify
    measure, a patch list that does not reproduce the recorded count)."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


def patches_fingerprint(patches: Iterable[Sequence[int]]) -> str:
    """A stable identity for an ordered code-value patch list (plan fingerprint input)."""
    payload = json.dumps([[int(c) for c in p] for p in patches], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _patches_from_ndjson(path: Path) -> Optional[list[tuple[int, int, int]]]:
    """The ordered patch list from a measure stage's NDJSON: every main-pass measurement read is
    labelled ``p<index>`` and carries the integer code values the presenter drove. Re-reads of
    one patch repeat its label (they must agree). ``None`` when the file is unreadable or the
    indices are not a contiguous 0..N-1 run (a truncated pass)."""
    labels: dict[int, tuple[int, int, int]] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("role") != "measurement":
                    continue
                m = re.fullmatch(r"p(\d+)", str(row.get("label") or ""))
                rgb = row.get("rgb")
                if not m or not isinstance(rgb, list) or len(rgb) != 3:
                    continue
                idx, code = int(m.group(1)), tuple(int(c) for c in rgb)
                if labels.setdefault(idx, code) != code:
                    return None     # one label, two different patches: not a patch list
    except OSError:
        return None
    if not labels or sorted(labels) != list(range(len(labels))):
        return None
    return [labels[i] for i in range(len(labels))]


def _patches_from_ti3(path: Path, bit_depth: int) -> Optional[list[tuple[int, int, int]]]:
    """The ordered patch list from an accepted-read TI3 (RGB as 0-100 % signal, 6 decimals):
    exact back to integer codes at <= 16 bits. Rows dropped as sentinel holes are simply absent,
    so the caller checks the count against the recorded patch count."""
    max_cv = (1 << int(bit_depth)) - 1
    rows: list[tuple[int, int, int]] = []
    inside = False
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line == "BEGIN_DATA":
                inside = True
                continue
            if line == "END_DATA":
                break
            if inside and line:
                parts = line.split()
                rows.append(tuple(int(round(float(v) / 100.0 * max_cv)) for v in parts[:3]))  # type: ignore[misc]
    except (OSError, ValueError):
        return None
    return rows or None


def load_source_verify(src_root: Path) -> dict[str, Any]:
    """Everything ``--verify-patches-from`` needs from a recorded run (read-only).

    Returns ``patches`` (the ordered code-value list, exact), ``patch_source`` (ndjson / ti3),
    the run's identity (mode / monitor / bit depth / display / EDID / target / correction), its
    recorded verify digest (``verify``) and the scoring basis it was scored under
    (``scoring_basis``: hdr_target, oog_mapping, oog_level_edge, mhc_params, white). Raises
    :class:`SourceRunError` when the run cannot supply a verify set."""
    src_root = Path(src_root)
    try:
        state = _read_json(src_root / "dlc_state.json")
    except (OSError, ValueError) as exc:
        raise SourceRunError(f"cannot read the source run's dlc_state.json ({type(exc).__name__}: {exc})",
                             source_run=str(src_root)) from exc
    calib = state.get("calib") or {}
    stages = calib.get("stages") or {}
    measure = stages.get("measure:verify") or {}
    if measure.get("status") != "done":
        raise SourceRunError("the source run has no completed measure:verify stage (still running, "
                             "aborted before verify, or a flow without a verify set)",
                             source_run=str(src_root), status=measure.get("status"))
    try:
        manifest = _read_json(src_root / "manifest.json")
    except (OSError, ValueError):
        manifest = {}
    bit_depth = state.get("bit_depth")
    recorded = (measure.get("digest") or {}).get("patch_count")
    # The files beside THIS source dir, never the absolute paths the record stored: a moved or
    # copied run folder must not silently read another folder's measurements.
    ndjson = src_root / "measurements" / "verify.ndjson"
    ti3 = src_root / "measurements" / "verify.ti3"
    patches = _patches_from_ndjson(ndjson) if ndjson.exists() else None
    source = "ndjson"
    ti3_rows = _patches_from_ti3(ti3, int(bit_depth)) if (ti3.exists() and bit_depth) else None
    cross_check: Optional[bool] = None
    if patches is not None and ti3_rows is not None and len(ti3_rows) == len(patches):
        cross_check = ti3_rows == patches
        if cross_check is False:
            raise SourceRunError("the source run's verify NDJSON and TI3 disagree on the patch list "
                                 "(hand-edited or mixed files?)", source_run=str(src_root))
    if patches is None:
        patches, source = ti3_rows, "ti3"
    if not patches:
        raise SourceRunError("no verify patch list in the source run (measurements/verify.ndjson "
                             "and verify.ti3 are missing or unreadable)", source_run=str(src_root))
    if recorded is not None and int(recorded) != len(patches):
        raise SourceRunError(
            f"the source run's {source} yields {len(patches)} verify patches but its measure:verify "
            f"recorded {recorded} — not provably the set it measured (a TI3 drops unusable reads)",
            source_run=str(src_root), recovered=len(patches), recorded=recorded)
    preflight = (stages.get("preflight") or {}).get("digest") or {}
    verify_rec = stages.get("verify") or {}
    verify_digest = verify_rec.get("digest") if verify_rec.get("status") == "done" else None
    white = calib.get("white") or {}
    return {
        "run": str(src_root),
        "run_id": src_root.name,
        "flow": calib.get("flow"),
        "mode": str(state.get("mode") or manifest.get("mode") or "").upper() or None,
        # what the codes ARE (verify-only --content-mode; == mode for every other run)
        "content_mode": str(calib.get("content_mode") or state.get("mode") or manifest.get("mode") or "").upper()
        or None,
        "monitor": state.get("monitor"),
        "bit_depth": int(bit_depth) if bit_depth is not None else None,
        "target": calib.get("target"),
        "display": preflight.get("display") or manifest.get("display"),
        "hardware_id": (preflight.get("monitor_map") or {}).get("hardware_id"),
        "correction_file": (preflight.get("correction") or {}).get("file"),
        "patches": [list(p) for p in patches],
        "patch_count": len(patches),
        "patch_source": source,
        "patch_cross_check": cross_check,
        "patches_fingerprint": patches_fingerprint(patches),
        "patch_max_cv": (calib.get("patch_plan") or {}).get("patch_max_cv"),
        "verify": verify_digest,
        "verify_decision": ((calib.get("decisions") or {}).get("verify:accept") or {}).get("choice"),
        "patch_metrics_path": str(src_root / "reports" / "verification_iter00_patch_metrics.json"),
        "scoring_basis": {
            "hdr_target": calib.get("hdr_target"),
            "oog_mapping": calib.get("oog_mapping"),
            "oog_level_edge": calib.get("oog_level_edge"),
            "mhc_params": state.get("mhc_params"),
            "white_xy": white.get("xy"),
        },
    }


def _norm_file(value: Any) -> Optional[str]:
    if not value:
        return None
    return str(value).replace("\\", "/").lower().rstrip("/")


def source_mismatches(source: Mapping[str, Any], *, mode: str, bit_depth: int, display: Optional[str],
                      hardware_id: Optional[str], target: Optional[str],
                      correction_file: Optional[str], pin_nits: Optional[float],
                      display_mode: Optional[str] = None) -> dict[str, Any]:
    """What differs between the source run and this one.

    ``mode`` is the CONTENT mode (what the codes are) and ``display_mode`` the display's (``None`` =
    the same as ``mode``). ``hard`` — the recorded code values would mean a different SIGNAL here
    (content mode / bit depth): measuring them is not a like-for-like verify, whatever the judge
    prefers. ``soft`` — a judgment for the LLM (another display mode — the same SDR codes composited
    into HDR, or the reverse / display / panel EDID / target / colorimeter correction / an installed
    calibrated top that is not the one the source scored against)."""
    hard: list[str] = []
    soft: list[str] = []
    src_content = source.get("content_mode") or source.get("mode")
    if src_content and str(src_content).upper() != str(mode).upper():
        hard.append(f"source content mode {src_content} != this run's {mode} (the codes are another transfer)")
    disp = str(display_mode or mode).upper()
    if source.get("mode") and str(source["mode"]).upper() != disp:
        soft.append(f"source display mode {source['mode']} != this run's {disp} — the same codes reach the "
                    "panel through another path (e.g. SDR codes composited into HDR at the SDR white level "
                    "through the HDR stack): compare the numbers as a different stack, not a re-verify")
    if source.get("bit_depth") is not None and int(source["bit_depth"]) != int(bit_depth):
        hard.append(f"source bit depth {source['bit_depth']} != this run's {bit_depth} (the codes "
                    "are another quantization)")
    if source.get("display") and display and source["display"] != display:
        soft.append(f"source display {source['display']!r} != this run's {display!r}")
    if source.get("hardware_id") and hardware_id and source["hardware_id"] != hardware_id:
        soft.append(f"source panel EDID {source['hardware_id']} != the panel now measured ({hardware_id})")
    if source.get("target") and target and source["target"] != target:
        soft.append(f"source target {source['target']!r} != this run's {target!r} (the recorded "
                    "verify was scored against another target)")
    if _norm_file(source.get("correction_file")) != _norm_file(correction_file):
        soft.append(f"the colorimeter correction differs (source {source.get('correction_file')!r}, "
                    f"now {correction_file!r}) — the meter reads the panel through another matrix")
    peak = ((source.get("scoring_basis") or {}).get("hdr_target") or {}).get("peak_nits")
    if pin_nits and peak:
        try:
            if abs(float(pin_nits) - float(peak)) > _PEAK_REL_TOL * float(peak):
                soft.append(f"the installed MHC's calibrated top ({float(pin_nits):.1f} nits, stack "
                            f"registry) != the peak the source scored against ({float(peak):.1f}) — "
                            "the installed MHC is not the source's; the source's peak is kept for "
                            "like-for-like scoring")
        except (TypeError, ValueError):
            pass
    return {"hard": hard, "soft": soft}


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _delta_row(now: Any, src: Any, *, ndigits: int = 3) -> dict[str, Any]:
    a, b = _num(now), _num(src)
    return {"now": None if a is None else round(a, ndigits),
            "source": None if b is None else round(b, ndigits),
            "delta": None if a is None or b is None else round(a - b, ndigits)}


def _bucket_delta(now: Mapping[str, Any], src: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: _delta_row(now.get(k), src.get(k)) for k in _STATS}
    out["n"] = {"now": now.get("n"), "source": src.get("n")}
    return out


bucket_delta = _bucket_delta   # {avg, p95, max: {now, source, delta}, n} of two practical buckets (public)


def _patch_rows(rows: Iterable[Mapping[str, Any]]) -> dict[tuple, Mapping[str, Any]]:
    """Key per-patch rows by (rounded signal, occurrence) — robust to a source whose TI3
    dropped a hole (index alignment would then shift every later patch)."""
    seen: dict[tuple, int] = {}
    keyed: dict[tuple, Mapping[str, Any]] = {}
    for row in rows:
        rgb = tuple(round(float(c), 5) for c in (row.get("rgb") or ())[:3])
        n = seen.get(rgb, 0)
        seen[rgb] = n + 1
        keyed[(rgb, n)] = row
    return keyed


def _movers(now_rows: Sequence[Mapping[str, Any]], src_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    now_k, src_k = _patch_rows(now_rows), _patch_rows(src_rows)
    pairs = []
    for key, row in now_k.items():
        other = src_k.get(key)
        a, b = _num(row.get("de2000")), _num((other or {}).get("de2000"))
        if other is None or a is None or b is None:
            continue
        pairs.append({"rgb": list(key[0]), "now": round(a, 3), "source": round(b, 3),
                      "delta": round(a - b, 3), "gamut_clamped": bool(row.get("gamut_clamped"))})
    pairs.sort(key=lambda r: r["delta"])
    deltas = [p["delta"] for p in pairs]
    return {"matched": len(pairs), "now_patches": len(now_rows), "source_patches": len(src_rows),
            "mean_delta": round(sum(deltas) / len(deltas), 3) if deltas else None,
            "worse": list(reversed(pairs[-_MOVERS:])) if pairs else [],
            "better": pairs[:_MOVERS]}


def compare_verify(now: Mapping[str, Any], source: Mapping[str, Any], *,
                   now_patch_rows: Optional[Sequence[Mapping[str, Any]]] = None,
                   source_patch_rows: Optional[Sequence[Mapping[str, Any]]] = None,
                   basis_now: Optional[Mapping[str, Any]] = None,
                   basis_source: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Per-bucket deltas (``now - source``) of this verify vs the source run's RECORDED verify:
    the headline numbers, the gate's scored core/tube/white, every ``practical`` bucket
    (core / limits / clamped / tube) and luminance band, plus per-patch movers when both sides'
    per-patch metrics are at hand. ``comparability`` lists every scoring-basis difference
    (metric, white, peak, OOG policy, gamut awareness) — deltas across a changed basis measure
    the scorer, not the stack. Pure arithmetic; the judgment is the LLM's."""
    now_p = now.get("practical") or {}
    src_p = source.get("practical") or {}
    bands_now = now_p.get("bands") or {}
    bands_src = src_p.get("bands") or {}
    out: dict[str, Any] = {
        "metric": now.get("metric"),
        "headline": {k: _delta_row(now.get(k), source.get(k)) for k in _HEADLINE_KEYS},
        "buckets": {b: _bucket_delta(now_p.get(b) or {}, src_p.get(b) or {}) for b in _BUCKETS
                    if (now_p.get(b) or {}).get("n") or (src_p.get(b) or {}).get("n")},
        "bands": {label: _bucket_delta(bands_now.get(label) or {}, bands_src.get(label) or {})
                  for label in list(dict.fromkeys(list(bands_now) + list(bands_src)))
                  if (bands_now.get(label) or {}).get("n") or (bands_src.get(label) or {}).get("n")},
        "within_quality": {"now": now.get("within_quality"), "source": source.get("within_quality")},
    }
    diffs: list[str] = []
    if source.get("metric") and now.get("metric") and source["metric"] != now["metric"]:
        diffs.append(f"metric {source['metric']} -> {now['metric']}")
    if bool(source.get("gamut_aware")) != bool(now.get("gamut_aware")):
        diffs.append(f"gamut_aware {bool(source.get('gamut_aware'))} -> {bool(now.get('gamut_aware'))}")
    sw, nw = source.get("target_white_xy"), now.get("target_white_xy")
    if sw and nw and max(abs(float(a) - float(b)) for a, b in zip(sw, nw)) > _WHITE_XY_TOL:
        diffs.append(f"target white {list(sw)} -> {list(nw)}")
    bn, bs = dict(basis_now or {}), dict(basis_source or {})
    pn, ps = _num(bn.get("peak_nits")), _num(bs.get("peak_nits"))
    if pn and ps and abs(pn - ps) > _PEAK_REL_TOL * ps:
        diffs.append(f"HDR peak {ps:.1f} -> {pn:.1f} nits")
    if bn.get("oog_mapping") and bs.get("oog_mapping") and bn["oog_mapping"] != bs["oog_mapping"]:
        diffs.append(f"oog_mapping {bs['oog_mapping']} -> {bn['oog_mapping']}")
    if now.get("patch_count") != source.get("patch_count"):
        diffs.append(f"scored patches {source.get('patch_count')} -> {now.get('patch_count')}")
    out["comparability"] = {"like_for_like": not diffs, "differences": diffs}
    if now_patch_rows is not None and source_patch_rows is not None:
        out["patches"] = _movers(now_patch_rows, source_patch_rows)
    return out


def candidate_cube_facts(path: Path) -> dict[str, Any]:
    """The mechanical facts about a candidate 3D LUT (``--verify-cube``): parse it the way the
    integrity check does. ``ok`` False names why it cannot be installed as a verify candidate."""
    from .lut_integrity import parse_cube

    facts: dict[str, Any] = {"path": str(path), "exists": path.exists(), "ok": False}
    if not path.exists():
        facts["error"] = "file does not exist"
        return facts
    try:
        data = path.read_bytes()
        facts["sha256"] = hashlib.sha256(data).hexdigest()[:16]
        facts["bytes"] = len(data)
        cube = parse_cube(path)
    except OSError as exc:
        facts["error"] = f"unreadable ({type(exc).__name__}: {exc})"
        return facts
    facts["title"] = cube.title
    facts["size"] = cube.size
    expected = cube.size ** 3 if cube.size > 0 else 0
    facts["entries"] = len(cube.values)
    problems = list(cube.parse_errors[:3])
    if cube.size > 0 and len(cube.values) != expected:
        problems.append(f"expected {expected} entries, found {len(cube.values)}")
    if problems:
        facts["error"] = "; ".join(problems)
        return facts
    facts["ok"] = True
    return facts


def restore_candidate(controller: Any, calib: Optional[dict[str, Any]], *, why: str,
                      log: Optional[Callable[[str], None]] = None,
                      terminal: bool = False) -> Optional[dict[str, Any]]:
    """Put the runtime cube that was live before a ``--verify-cube`` candidate install back (or
    clear the slot when there was none). No-op (returns the record unchanged, or ``None``)
    unless a candidate is installed and neither restored nor kept. Mutates + returns the
    ``calib['verify_candidate']`` record; the caller persists it. Best-effort: a failed restore
    is recorded (``restore_error``), never raised — teardown must not mask the original exit.

    A prior whose FILE is gone cannot be re-applied (DesktopLUT rejects the path): the slot is
    CLEARED instead — what DesktopLUT could render from that path anyway — so the candidate is
    never stranded (decided up front at ``install-candidate:prior-missing`` when known then).
    ``terminal`` (the CLI ``--abort``) marks the record ``aborted``: a resume must not re-install."""
    calib = calib if isinstance(calib, dict) else {}
    rec = calib.get("verify_candidate")
    if isinstance(rec, dict) and terminal and rec.get("installed") and not rec.get("kept"):
        rec["aborted"] = True
    if not isinstance(rec, dict) or not rec.get("installed") or rec.get("restored") or rec.get("kept"):
        return rec if isinstance(rec, dict) else None
    mon, mode = rec.get("monitor"), rec.get("mode")
    prior = rec.get("prior_cube")
    clear_instead = bool(prior) and (bool(rec.get("restore_clears")) or not Path(str(prior)).exists())
    try:
        if prior and not clear_instead:
            controller.set_3dlut(int(mon), str(mode), str(prior))
        else:
            controller.clear_3dlut(int(mon), str(mode))
        live = None
        try:
            live = (((controller.state() or {}).get("runtime") or {}).get(f"{mon}:{mode}") or {}).get("cube_path")
        except Exception:  # noqa: BLE001 - the restore call itself succeeded; the read-back is evidence
            live = "unknown"
        rec["restored"] = True
        rec["restored_to"] = None if clear_instead else prior
        if clear_instead:
            rec["restore_note"] = f"the prior cube file is missing ({prior}) — the slot was cleared instead"
        rec["restore_why"] = why
        rec["restore_readback"] = live
        if log:
            log(f"verify candidate: restored the prior 3D LUT "
                f"({rec['restored_to'] or 'none - slot cleared'}) — {why}")
    except Exception as exc:  # noqa: BLE001 - surfaced on the record + log, never fatal to teardown
        rec["restored"] = False
        rec["restore_error"] = f"{type(exc).__name__}: {exc}"
        if log:
            log(f"verify candidate: could NOT restore the prior 3D LUT ({prior or 'none'}): "
                f"{rec['restore_error']} — re-apply it in DesktopLUT by hand")
    calib["verify_candidate"] = rec
    return rec
