"""Content-weighted ("practical") score of a verify set — EVIDENCE ONLY, never a gate.

Why (owner directive 2026-10-09, "ground us back down when we get lost chasing benchmark scores";
study ``results/practical_score_2026-10-09/RESULT.md``): the verify set's patch geography does not
spend its budget where content lives. HDR live action has 52 % of its pixels below 1 nit and 67 % of
its mass in a low-chroma shell, while 81 % of the HDR verify signals sit at high chroma and ~41 % of
the content mass has no verify signal within 20 dE_ITP. A per-zone average therefore answers "how
good is the panel at the patches we chose", not "how good does content look". This module answers
the second question for ANY scored verify set, given a content distribution (user data).

Score definition (study §5.1, reference implementation ``score.py``; distances in dE_ITP units =
720 · |Δ(I, T, P)|, T = Ct / 2, BT.2124):

* every content bin c (ITP grid of the class histogram, mass w_c) gets an expected error
  ``e(c) = Σ_j K(d_cj)·E_j / Σ_j K(d_cj)`` over the verify's UNIQUE signals j within reach R, with a
  Gaussian K of σ = R / 2. E_j is the signal's mean ΔE over its reads — DLC's own per-mode metric
  (dE_ITP vs the reachable target for HDR, CIEDE2000 for SDR), the same grouping as
  :func:`dlc.metrics.per_signal_summary`. Signals are LOCATED at their NOMINAL colour (HDR: the
  unclamped PQ / Rec.2020 target — where content with that signal lives; SDR: the scored target).
* ``score`` = Σ w_c e(c) / covered mass; ``coverage_gap_pct`` = content mass with no signal within R
  ("we did not measure this"); ``score_with_nearest_fallback`` gives uncovered bins their nearest
  signal's error (an extrapolation, shown beside the score, never instead of it).

The distribution is USER DATA (the owner's library survey): it is loaded from a local path
(``--content-distribution PATH[#VARIANT]`` or the profile's ``content_distribution:`` key) and never
committed. Two formats are read:

* the study's ``content_hist_<class>.npz`` (``<variant>_itp_idx`` / ``<variant>_itp_w`` sparse ITP
  histogram + the grid layout keys ``shape_itp``, ``ITP_STEP_I``, ``ITP_STEP_TP``, ``TP_MAX``; the
  label part of the flat index carries the content colour's zone / luminance band / tube);
* a compact JSON export (:data:`CONTENT_JSON_FORMAT`, written by :func:`export_content_json`):
  ``{"format", "class", "variant", "itp": [[I, T, P], ...], "w": [...], optional "zone" / "band" /
  "tube" label lists, "doc"}``.

Read trust rides along: which signals' E rests on a single meter read, on a read below the meter's
noise floor, or on a read the measure loop's dark-level noise machinery flags low-SNR — the study
found ~75 % of the HDR content-weighted score carried by four near-black single reads at the
colorimeter's floor, so that share is always reported.

numpy at import (the orchestrator already depends on it); scipy lazily in :func:`kernel_score`.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

__all__ = [
    "DEFAULT_REACH", "ITP_SCALE", "CONTENT_JSON_FORMAT", "ZONE_LABELS", "BAND_LABELS",
    "ContentDistribution", "parse_content_spec", "load_content_distribution", "export_content_json",
    "xyz_to_itp", "nominal_signal_xyz", "kernel_score", "read_counts_from_ndjson", "reads_from_ndjson",
    "sidecar_se_de", "read_noise_se", "low_snr_signal_keys", "rescore_run",
]

DEFAULT_REACH = 20.0          # dE_ITP; ~ one 33-node PQ cube cell along I (study §5.1)
ITP_SCALE = 720.0             # BT.2124: dE_ITP = 720 · |Δ(I, T, P)|
CONTENT_JSON_FORMAT = "dlc-content-distribution/1"
# The study's content labels (the content colour's own zone / band; zone is relative to the
# surveyed panel's native gamut + cube peak — PA32UCXR, 2026-10-09).
ZONE_LABELS = ("core", "limits", "clamped")
BAND_LABELS = ("<1", "1-10", "10-100", "100-203", ">203")
_TOP_CONTRIBUTORS = 8

# BT.2100 ICtCp (PQ) — the same constants the study's survey binned the content with.
_M1 = 2610.0 / 16384.0
_M2 = 2523.0 / 4096.0 * 128.0
_C1 = 3424.0 / 4096.0
_C2 = 2413.0 / 4096.0 * 32.0
_C3 = 2392.0 / 4096.0 * 32.0
_RGB2LMS = np.array([[1688, 2146, 262], [683, 2951, 462], [99, 309, 3688]], float) / 4096.0
_LMS2ICTCP = np.array([[2048, 2048, 0], [6610, -13613, 7003], [17933, -17390, -543]], float) / 4096.0
_BT2020 = ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046))
_D65 = (0.3127, 0.3290)


def _npm(prim: Sequence[Sequence[float]], white: Sequence[float] = _D65) -> np.ndarray:
    xyz = np.array([[x / y, 1.0, (1 - x - y) / y] for x, y in prim]).T
    wx, wy = white
    s = np.linalg.solve(xyz, np.array([wx / wy, 1.0, (1 - wx - wy) / wy]))
    return xyz * s


_M2020_INV = np.linalg.inv(_npm(_BT2020))


def _pq_oetf(nits: np.ndarray) -> np.ndarray:
    y = np.clip(np.asarray(nits, float) / 10000.0, 0.0, 1.0)
    yp = np.power(y, _M1)
    return np.power((_C1 + _C2 * yp) / (1.0 + _C3 * yp), _M2)


def xyz_to_itp(xyz: Any) -> np.ndarray:
    """Absolute XYZ (cd/m²) → (I, T, P) with T = Ct / 2 (BT.2124), via linear Rec.2020 (D65) —
    the conversion the content histograms were binned with. Negative RGB is clipped to 0."""
    rgb = np.asarray(xyz, float).reshape(-1, 3) @ _M2020_INV.T
    lms = np.clip(rgb, 0.0, None) @ _RGB2LMS.T
    ict = _pq_oetf(lms) @ _LMS2ICTCP.T
    return np.stack([ict[:, 0], ict[:, 1] * 0.5, ict[:, 2]], axis=1)


# ---------------------------------------------------------------------------------------------
# content distributions
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ContentDistribution:
    """One content class's pixel-mass distribution over ITP (USER DATA, loaded from a local path).

    ``itp`` (N, 3) bin centres, ``w`` (N,) mass (any scale — scores are mass ratios). ``zone`` /
    ``band`` / ``tube`` are the content colour's own labels when the source carries them (the study's
    npz does), else ``None`` (breakdowns are then omitted)."""
    name: str
    variant: str
    itp: np.ndarray
    w: np.ndarray
    source: str
    fingerprint: str
    zone: Optional[np.ndarray] = None
    band: Optional[np.ndarray] = None
    tube: Optional[np.ndarray] = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name if self.variant in ("", "main", "json") else f"{self.name}#{self.variant}"

    def provenance(self) -> dict[str, Any]:
        return {"class": self.name, "variant": self.variant, "source": self.source,
                "fingerprint": self.fingerprint, "bins": int(len(self.w)), **self.meta}


def parse_content_spec(spec: str) -> tuple[str, Optional[str]]:
    """``PATH[#VARIANT]`` → (path, variant or None)."""
    text = str(spec).strip()
    if "#" in text:
        path, _, variant = text.rpartition("#")
        if path and variant and "/" not in variant and "\\" not in variant:
            return path, variant.strip()
    return text, None


def _file_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _class_name_from_path(path: Path) -> str:
    stem = path.stem
    return stem[len("content_hist_"):] if stem.startswith("content_hist_") else stem


def load_content_distribution(spec: str | Path, *, variant: Optional[str] = None,
                              content_mode: Optional[str] = None) -> ContentDistribution:
    """Load a content distribution from ``PATH[#VARIANT]`` (npz or the compact JSON export).

    Variant (npz only): an explicit ``#VARIANT`` / ``variant`` wins; else ``desk`` for SDR content when
    the file has it (the gamma 2.2 desktop path — what DLC's SDR verify scores), else ``main``.
    Raises ``ValueError`` (unreadable / malformed / unknown variant)."""
    raw_path, spec_variant = parse_content_spec(str(spec))
    path = Path(raw_path)
    variant = variant or spec_variant
    if not path.is_file():
        raise ValueError(f"content distribution {str(path)!r} not found")
    fp = _file_fingerprint(path)
    if path.suffix.lower() == ".json":
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"content distribution {path.name}: unreadable JSON ({exc})") from exc
        if not isinstance(doc, dict) or doc.get("format") != CONTENT_JSON_FORMAT:
            raise ValueError(f"content distribution {path.name}: not a {CONTENT_JSON_FORMAT} document")
        itp = np.asarray(doc.get("itp") or [], float).reshape(-1, 3)
        w = np.asarray(doc.get("w") or [], float).reshape(-1)
        if not len(w) or len(w) != len(itp) or not np.all(np.isfinite(itp)) or np.any(~np.isfinite(w)) \
                or np.any(w < 0) or w.sum() <= 0:
            raise ValueError(f"content distribution {path.name}: 'itp'/'w' empty, mismatched or invalid")

        def labels(key: str) -> Optional[np.ndarray]:
            v = doc.get(key)
            return np.asarray(v, int).reshape(-1) if isinstance(v, list) and len(v) == len(w) else None
        return ContentDistribution(
            name=str(doc.get("class") or _class_name_from_path(path)), variant=str(doc.get("variant") or "json"),
            itp=itp, w=w, source=str(path), fingerprint=fp, zone=labels("zone"), band=labels("band"),
            tube=labels("tube"), meta={k: doc[k] for k in ("doc", "n_titles") if k in doc})
    try:
        npz = np.load(path, allow_pickle=False)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is the same refusal
        raise ValueError(f"content distribution {path.name}: unreadable npz ({type(exc).__name__}: {exc})") from exc
    with npz:
        files = set(npz.files)
        variants = sorted(k[: -len("_itp_idx")] for k in files if k.endswith("_itp_idx"))
        if not variants:
            raise ValueError(f"content distribution {path.name}: no '<variant>_itp_idx' arrays")
        if variant is None:
            sdr = str(content_mode or "").upper() == "SDR"
            variant = "desk" if (sdr and "desk" in variants) else ("main" if "main" in variants else variants[0])
        if variant not in variants:
            raise ValueError(f"content distribution {path.name}: no variant {variant!r} (has {variants})")
        for k in ("shape_itp", "ITP_STEP_I", "ITP_STEP_TP", "TP_MAX"):
            if k not in files:
                raise ValueError(f"content distribution {path.name}: missing grid layout key {k!r}")
        n_lab, n_i, n_t, n_p = (int(v) for v in np.asarray(npz["shape_itp"]).reshape(-1)[:4])
        step_i, step_tp, tp_max = float(npz["ITP_STEP_I"]), float(npz["ITP_STEP_TP"]), float(npz["TP_MAX"])
        idx = np.asarray(npz[f"{variant}_itp_idx"], np.int64)
        w = np.asarray(npz[f"{variant}_itp_w"], float)
        n_titles = int(npz["n_titles"]) if "n_titles" in files else None
    if len(idx) != len(w) or not len(w):
        raise ValueError(f"content distribution {path.name}: index / weight arrays mismatched or empty")
    pi = idx % n_p
    r = idx // n_p
    ti = r % n_t
    r //= n_t
    ii = r % n_i
    lab = r // n_i
    if np.any(lab >= n_lab) or np.any(idx < 0):
        raise ValueError(f"content distribution {path.name}: flat index outside the declared grid")
    itp = np.stack([(ii + 0.5) * step_i, -tp_max + (ti + 0.5) * step_tp, -tp_max + (pi + 0.5) * step_tp], axis=1)
    meta: dict[str, Any] = {"grid": {"shape_itp": [n_lab, n_i, n_t, n_p], "step_i": step_i,
                                     "step_tp": step_tp, "tp_max": tp_max}}
    if n_titles is not None:
        meta["n_titles"] = n_titles
    return ContentDistribution(name=_class_name_from_path(path), variant=variant, itp=itp, w=w,
                               source=str(path), fingerprint=fp, zone=(lab // 10).astype(int),
                               band=((lab % 10) // 2).astype(int), tube=(lab % 2).astype(int), meta=meta)


def export_content_json(dist: ContentDistribution, out: Path, *, min_mass_fraction: float = 0.0,
                        decimals: int = 5) -> dict[str, Any]:
    """Write ``dist`` as the compact JSON export (:data:`CONTENT_JSON_FORMAT`). Bins below
    ``min_mass_fraction`` of the total are dropped (their mass is stated, not silently lost)."""
    total = float(dist.w.sum())
    keep = dist.w >= float(min_mass_fraction) * total
    doc: dict[str, Any] = {
        "format": CONTENT_JSON_FORMAT, "class": dist.name, "variant": dist.variant,
        "doc": (f"exported from {Path(dist.source).name} ({dist.variant}); itp = (I, T=Ct/2, P) bin centres, "
                "w = pixel mass; zone/band/tube = the content colour's labels (study layout)"),
        "dropped_mass_fraction": round(float(dist.w[~keep].sum()) / total, 8) if total else 0.0,
        "itp": np.round(dist.itp[keep], decimals).tolist(), "w": dist.w[keep].tolist()}
    for key in ("zone", "band", "tube"):
        arr = getattr(dist, key)
        if arr is not None:
            doc[key] = np.asarray(arr)[keep].astype(int).tolist()
    if "n_titles" in dist.meta:
        doc["n_titles"] = dist.meta["n_titles"]
    Path(out).write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    return {"bins": int(keep.sum()), "dropped_mass_fraction": doc["dropped_mass_fraction"], "path": str(out)}


# ---------------------------------------------------------------------------------------------
# signal locations
# ---------------------------------------------------------------------------------------------
def nominal_signal_xyz(rgb: Any, *, is_hdr: bool, white_xy: Optional[Sequence[float]] = None,
                       target_xyz: Any = None) -> np.ndarray:
    """Where content with each signal lives, absolute XYZ. HDR: the UNCLAMPED PQ / Rec.2020 target at
    ``white_xy`` (the run's resolved white) — the scored target may be clamped to the reachable gamut,
    but content carrying that code is the raw colour. SDR: the scored ``target_xyz`` (unclamped in the
    production SDR path)."""
    if not is_hdr:
        if target_xyz is None:
            raise ValueError("nominal_signal_xyz: SDR needs the scored target_xyz")
        return np.asarray(target_xyz, float).reshape(-1, 3)
    from .engine.model import Target, TargetSpace

    wxy = tuple(white_xy) if white_xy is not None else _D65
    return TargetSpace(Target.hdr_rec2020_pq(white_xy=wxy)).ideal_xyz(np.asarray(rgb, float).reshape(-1, 3))


# ---------------------------------------------------------------------------------------------
# the kernel score
# ---------------------------------------------------------------------------------------------
def _wpct(x: np.ndarray, w: np.ndarray, q: float) -> float:
    o = np.argsort(x)
    x, w = x[o], w[o]
    c = np.cumsum(w)
    c = c / c[-1]
    return float(np.interp(q / 100.0, c, x))


def _r(v: Optional[float], nd: int = 3) -> Optional[float]:
    return None if v is None or not math.isfinite(v) else round(float(v), nd)


def kernel_score(content: ContentDistribution, sig_itp: Any, sig_err: Any, *,
                 reach: float = DEFAULT_REACH, sigma: Optional[float] = None,
                 sig_info: Optional[Sequence[Mapping[str, Any]]] = None,
                 top: int = _TOP_CONTRIBUTORS,
                 alt_err: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The study's §5.1 score of one content class against a verify set's unique signals.

    ``sig_itp`` (M, 3) nominal ITP locations, ``sig_err`` (M,) per-signal mean ΔE (the run's metric).
    ``sig_info`` (optional, per signal) rides into the top-contributor rows and the evidence shares:
    ``rgb``, ``nominal_Y``, ``reads``, ``zone`` and the trust flags ``single_read`` / ``at_floor`` /
    ``low_snr`` / ``weak`` / ``noise_limited`` / ``noise_unknown``. ``alt_err`` ({name: (M,)}) scores
    labelled VARIANTS of the per-signal error over the same kernel (e.g. the noise bias-corrected E) —
    returned under ``variants``, beside the raw score, never instead of it. Evidence only — no threshold,
    no verdict."""
    from scipy.spatial import cKDTree

    reach = float(reach)
    sigma = float(sigma) if sigma else reach / 2.0
    loc = np.asarray(sig_itp, float).reshape(-1, 3) * ITP_SCALE
    err = np.asarray(sig_err, float).reshape(-1)
    w = content.w
    total = float(w.sum())
    pts = content.itp * ITP_SCALE
    ct = cKDTree(pts)
    num = np.zeros(len(w))
    den = np.zeros(len(w))
    alts = {str(n): np.asarray(v, float).reshape(-1) for n, v in (alt_err or {}).items()}
    alt_num = {n: np.zeros(len(w)) for n in alts}
    hits = ct.query_ball_point(loc, reach) if len(loc) else []
    for j, ids in enumerate(hits):
        ids = np.asarray(ids, dtype=np.int64)
        if not ids.size:
            continue
        d = np.linalg.norm(pts[ids] - loc[j], axis=1)
        k = np.exp(-0.5 * (d / sigma) ** 2)
        np.add.at(num, ids, k * err[j])
        np.add.at(den, ids, k)
        for n, v in alts.items():
            np.add.at(alt_num[n], ids, k * v[j])
    cov = den > 0
    est = np.where(cov, num / np.where(cov, den, 1.0), np.nan)
    out: dict[str, Any] = {"class": content.label, "reach_dEITP": reach, "sigma_dEITP": sigma,
                           "n_signals": int(len(err)), "content_bins": int(len(w))}
    if not len(loc):
        out.update({"score": None, "coverage_gap_pct": 100.0, "note": "no signals to score"})
        return out
    dn, jn = cKDTree(loc).query(pts)
    est_fb = np.where(cov, est, err[jn])
    cov_mass = float(w[cov].sum())
    out.update({
        "score": _r(float(np.sum(w[cov] * est[cov]) / cov_mass)) if cov_mass > 0 else None,
        "coverage_gap_pct": round(100.0 * float(w[~cov].sum()) / total, 2),
        "score_with_nearest_fallback": _r(float(np.sum(w * est_fb) / total)),
        "p95_covered": _r(_wpct(est[cov], w[cov], 95)) if cov_mass > 0 else None,
        "median_distance_to_nearest_signal": round(_wpct(dn, w, 50), 2),
        "p90_distance_to_nearest_signal": round(_wpct(dn, w, 90), 2)})
    if alts:
        out["variants"] = {}
        for n, v in alts.items():
            a_est = np.where(cov, alt_num[n] / np.where(cov, den, 1.0), np.nan)
            out["variants"][n] = {
                "score": _r(float(np.sum(w[cov] * a_est[cov]) / cov_mass)) if cov_mass > 0 else None,
                "score_with_nearest_fallback": _r(float(np.sum(w * np.where(cov, a_est, v[jn])) / total))}
    contrib_total = float(np.sum(w[cov] * est[cov]))
    if contrib_total <= 1e-9 * max(cov_mass, 1e-300):
        contrib_total = 0.0      # an error-free set: score shares would be float noise — report none

    def bucket(mask: np.ndarray) -> dict[str, Any]:
        m = mask & cov
        mass = float(w[mask].sum())
        wm = float(w[m].sum())
        return {"mass_pct": round(100.0 * mass / total, 2),
                "uncovered_pct_of_bucket": round(100.0 * float(w[mask & ~cov].sum()) / mass, 1) if mass else None,
                "score": _r(float(np.sum(w[m] * est[m]) / wm)) if wm > 0 else None,
                "contribution_pct": (round(100.0 * float(np.sum(w[m] * est[m])) / contrib_total, 1)
                                     if contrib_total else None)}
    if content.zone is not None:
        out["zones"] = {z: bucket(content.zone == i) for i, z in enumerate(ZONE_LABELS)}
    if content.band is not None:
        out["bands"] = {b: bucket(content.band == i) for i, b in enumerate(BAND_LABELS)}
    if content.tube is not None:
        out["tube"] = bucket(content.tube == 1)
    # Which signals carry the score: the content share each represents (its kernel weight's share of
    # every bin it reaches) and its share of the score mass. Second pass (no id lists held in memory).
    share = np.zeros(len(err))
    csum = np.zeros(len(err))
    for j, ids in enumerate(hits):
        ids = np.asarray(ids, dtype=np.int64)
        if not ids.size:
            continue
        d = np.linalg.norm(pts[ids] - loc[j], axis=1)
        frac = np.exp(-0.5 * (d / sigma) ** 2) / den[ids]
        share[j] = float(np.sum(w[ids] * frac))
        csum[j] = float(np.sum(w[ids] * frac * err[j]))
    out["signals_with_no_content_within_reach"] = int(np.sum(share < 1e-6 * total))
    info = list(sig_info) if sig_info is not None else [{} for _ in range(len(err))]
    if info and contrib_total:
        def evidence_share(flag: str) -> dict[str, Any]:
            mask = np.array([bool(i.get(flag)) for i in info])
            return {"content_share_pct": round(100.0 * float(share[mask].sum()) / total, 2),
                    "score_share_pct": round(100.0 * float(csum[mask].sum()) / contrib_total, 1),
                    "n_signals": int(mask.sum())}
        out["evidence"] = {f: evidence_share(f) for f in ("weak", "single_read", "at_floor", "low_snr",
                                                          "single_read_at_floor", "noise_limited",
                                                          "noise_unknown")}
    order = np.argsort(-csum)[:max(0, int(top))]
    rows = []
    for j in order:
        if csum[j] <= 0:
            continue
        row = {k: info[j][k] for k in ("rgb", "code", "nominal_Y", "reads", "zone", "weak", "noise_se",
                                       "noise_limited") if k in info[j]}
        row.update({"dE": _r(float(err[j]), 2), "content_share_pct": round(100.0 * share[j] / total, 2),
                    "score_contribution_pct": round(100.0 * csum[j] / contrib_total, 1)})
        rows.append(row)
    out["top_contributors"] = rows
    return out


# ---------------------------------------------------------------------------------------------
# read evidence (how much each signal's E can be trusted)
# ---------------------------------------------------------------------------------------------
def read_counts_from_ndjson(path: Path, max_cv: int) -> dict[tuple, int]:
    """Meter reads behind each signal of a measure stage, from its NDJSON (one row per read): every
    accepted ``measurement`` row counts, keyed by :func:`dlc.metrics.signal_key` of its code / max_cv.
    Repeats of one signal at several patch indices add up. Empty when absent / unreadable."""
    return {k: len(v) for k, v in reads_from_ndjson(path, max_cv, keep_unreadable=True).items()}


def reads_from_ndjson(path: Path, max_cv: int, *, keep_unreadable: bool = False) -> dict[tuple, list]:
    """The accepted meter reads (absolute XYZ) behind each signal of a measure stage, from its NDJSON —
    keyed like :func:`read_counts_from_ndjson`; the spread of a signal's repeated reads is its read
    noise. A read without a finite XYZ is kept as ``None`` only with ``keep_unreadable`` (counting)."""
    from .metrics import signal_key

    counts: dict[tuple, list] = {}
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("role") != "measurement" or row.get("ok") is False or row.get("accepted") is False:
                    continue
                rgb = row.get("rgb")
                if not isinstance(rgb, list) or len(rgb) != 3:
                    continue
                key = signal_key([float(c) / max_cv for c in rgb])
                xyz = row.get("xyz")
                ok = (isinstance(xyz, list) and len(xyz) == 3
                      and all(isinstance(c, (int, float)) and math.isfinite(c) for c in xyz))
                if ok or keep_unreadable:
                    counts.setdefault(key, []).append(tuple(float(c) for c in xyz) if ok else None)
    except OSError:
        return {}
    return counts


def sidecar_se_de(ti3_path: Path, reps: Iterable[Any]) -> dict[tuple, float]:
    """The measure loop's own SE of the accepted mean (``se_de`` — ΔE2000 relative to its white) per
    grey signal, from ``<ti3>.noise.json`` (multi-read neutral levels only). Empty when absent."""
    from .measure_loop import noise_sidecar_path
    from .metrics import signal_key

    try:
        by = (json.loads(noise_sidecar_path(Path(ti3_path)).read_text(encoding="utf-8")) or {}).get("by_level") or {}
    except (OSError, ValueError):
        return {}
    levels = []
    for k, v in by.items():
        try:
            if isinstance(v, dict) and v.get("se_de") is not None and not v.get("unstable"):
                levels.append((float(k), float(v["se_de"])))
        except (TypeError, ValueError):
            continue
    out: dict[tuple, float] = {}
    for m in reps:
        if not m.grayscale:
            continue
        hit = [se for lvl, se in levels if abs(lvl - float(m.rgb[0])) <= 1e-5]
        if hit:
            out[signal_key(m.rgb)] = hit[0]
    return out


def read_noise_se(reads: Sequence[Sequence[float]], *, rows: int, is_hdr: bool,
                  white_xyz: Optional[Sequence[float]] = None) -> Optional[dict[str, Any]]:
    """The read-noise SE of a signal's scored E, in the run's metric, from the spread of its repeated
    reads: σ_read = √(Σ d_i² / (n−1)) with d_i the metric distance (dE_ITP for HDR, CIEDE2000 relative
    to ``white_xyz`` for SDR) of read i from the signal's mean read; each scored row averages
    ``n / rows`` reads, so SE = σ_read / √(n / rows), floored at the meter's 6-decimal XYZ print
    quantisation. ``None`` below two reads (no spread → no evidence)."""
    pts = [r for r in reads if r is not None]
    n = len(pts)
    if n < 2:
        return None
    arr = np.asarray(pts, float)
    mean = arr.mean(axis=0)
    if is_hdr:
        itp = xyz_to_itp(np.vstack([arr, mean[None, :], (mean + 1e-6)[None, :]])) * ITP_SCALE
        d = np.linalg.norm(itp[:n] - itp[n], axis=1)
        q = float(np.linalg.norm(itp[n + 1] - itp[n]))
    else:
        from .metrics import delta_e2000, xyz_to_lab

        wt = tuple(float(c) for c in (white_xyz if white_xyz is not None else (95.047, 100.0, 108.883)))
        lab_m = xyz_to_lab(tuple(float(c) for c in mean), wt)
        d = np.array([delta_e2000(xyz_to_lab(tuple(float(c) for c in r), wt), lab_m) for r in arr])
        q = float(delta_e2000(xyz_to_lab(tuple(float(c) + 1e-6 for c in mean), wt), lab_m))
    per_row = max(1.0, n / max(1, int(rows)))
    sigma = float(math.sqrt(float(np.sum(d ** 2)) / (n - 1)))
    se = max(sigma, q / math.sqrt(12.0)) / math.sqrt(per_row)
    return {"se": se, "sigma_read": sigma, "n_reads": n, "reads_per_row": round(per_row, 3)}


def low_snr_signal_keys(ti3_path: Path, reps: Iterable[Any]) -> set[tuple]:
    """Grey signals whose measured chromaticity error does not clear the measure loop's own
    repeatability noise — the dark-level noise trust machinery (``<ti3>.noise.json`` per-level SE of the
    mean xy, floored at the meter's quantisation; :func:`dlc.mhc_cube.noise_trust` < 1, i.e. SNR < 3)
    — or whose level the loop flagged ``unstable``. Only multi-read neutral levels carry a sidecar
    entry; single reads are reported as such elsewhere. ``reps`` = per-signal representatives
    (:func:`dlc.metrics.group_per_signal`)."""
    from .measure_loop import match_level_noise, read_noise_sidecar
    from .metrics import signal_key
    from .mhc_cube import floor_level_noise, noise_trust

    entries = read_noise_sidecar(Path(ti3_path))
    if not entries:
        return set()
    out: set[tuple] = set()
    for m in reps:
        if not m.grayscale:
            continue
        noise = match_level_noise(entries, float(m.rgb[0]))
        if noise is None:
            continue
        key = signal_key(m.rgb)
        if math.isinf(noise):
            out.add(key)
            continue
        mx, my, mz = (float(c) for c in m.measured_xyz)
        tx, ty, tz = (float(c) for c in m.target_xyz)
        ms, ts = mx + my + mz, tx + ty + tz
        if ms <= 0 or ts <= 0:
            out.add(key)
            continue
        err = math.hypot(mx / ms - tx / ts, my / ms - ty / ts)
        try:
            floored = floor_level_noise(noise, (mx, my, mz))
            if floored is None:
                continue
            if math.isinf(floored) or noise_trust(err, floored) < 1.0:
                out.add(key)
        except ValueError:
            out.add(key)
    return out


# ---------------------------------------------------------------------------------------------
# offline: re-score a recorded run
# ---------------------------------------------------------------------------------------------
def rescore_run(run_root: Path, specs: Sequence[str], *, reach: float = DEFAULT_REACH) -> dict[str, Any]:
    """The content-weighted block of a RECORDED run's verify (``reports/verification_iter00_*``) —
    the same :func:`dlc.metrics.practical_summary` the live verify computes, from the persisted patch
    rows + the run's resolved white + its verify NDJSON / noise sidecar. Read-only."""
    from .metrics import PatchMetric, ReadEvidence, practical_summary

    root = Path(run_root)
    rows = json.loads((root / "reports" / "verification_iter00_patch_metrics.json").read_text(encoding="utf-8"))
    summ = json.loads((root / "reports" / "verification_iter00_metrics.json").read_text(encoding="utf-8"))
    is_hdr = summ.get("metric") == "dE_ITP"
    state = json.loads((root / "dlc_state.json").read_text(encoding="utf-8"))
    calib = state.get("calib") or {}
    white = (calib.get("white") or {}).get("xy") or (calib.get("hdr_target") or {}).get("white_xy") or _D65
    metrics = [PatchMetric(tuple(r["rgb"]), tuple(c if c is not None else float("nan") for c in r["measured_xyz"]),
                           tuple(r["target_xyz"]), float(r["de2000"]), bool(r["grayscale"]),
                           gamut_clamped=bool(r.get("gamut_clamped"))) for r in rows]
    bit_depth = int(state.get("bit_depth") or (10 if is_hdr else 8))
    ti3 = root / "measurements" / "verify.ti3"
    from .metrics import group_per_signal

    reps = [m for m, _ in group_per_signal(metrics)]
    per_read = reads_from_ndjson(root / "measurements" / "verify.ndjson", (1 << bit_depth) - 1)
    evidence = ReadEvidence(reads=read_counts_from_ndjson(root / "measurements" / "verify.ndjson",
                                                          (1 << bit_depth) - 1) or None,
                            low_snr=frozenset(low_snr_signal_keys(ti3, reps)) if ti3.exists() else frozenset(),
                            read_xyz=per_read or None,
                            loop_se_de=(sidecar_se_de(ti3, reps) or None) if (ti3.exists() and not is_hdr) else None)
    content_mode = "HDR" if is_hdr else "SDR"
    contents = [load_content_distribution(s, content_mode=content_mode) for s in specs]
    practical = practical_summary(metrics, is_hdr=is_hdr, gamut_aware=bool(summ.get("practical", {}).get("gamut_aware")),
                                  read_evidence=evidence, content=contents, content_reach=reach,
                                  white_xy=tuple(white))
    return {"run": root.name, "metric": summ.get("metric"), "white_xy": list(white),
            "content_weighted": practical.get("content_weighted")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m dlc.content_score",
                                 description="Content-weighted (practical) score — evidence only.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rs = sub.add_parser("rescore", help="content-weighted block of a recorded run's verify")
    rs.add_argument("--run", type=Path, required=True)
    rs.add_argument("--content", action="append", required=True, metavar="PATH[#VARIANT]")
    rs.add_argument("--reach", type=float, default=DEFAULT_REACH)
    ex = sub.add_parser("export", help="write an npz content histogram as the compact JSON export")
    ex.add_argument("content", metavar="PATH[#VARIANT]")
    ex.add_argument("out", type=Path)
    ex.add_argument("--min-mass-fraction", type=float, default=0.0)
    args = ap.parse_args(argv)
    if args.cmd == "rescore":
        print(json.dumps(rescore_run(args.run, args.content, reach=args.reach), indent=1, default=str))
    else:
        print(json.dumps(export_content_json(load_content_distribution(args.content), args.out,
                                             min_mass_fraction=args.min_mass_fraction)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
