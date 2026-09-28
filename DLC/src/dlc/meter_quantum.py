"""The colorimeter's COUNT quantum: the XYZ lattice a frequency-counting i1 Display3 reads sit on.

**Physics (offline investigation 2026-09-28, 27 recorded runs, PA32UCXR / BenQ / LG C6).** Under
Argyll's adaptive i1d3 scheme each sensor channel is either *frequency-counted* (edges counted in a
fixed integration window — bright light) or *period-measured* (time for N edges — dim light, near
continuous). A counted channel contributes an INTEGER count, and XYZ is a fixed linear map of the
counts, so above ~15 nit a read is ``XYZ = Q·n`` exactly (``n`` integer, ``Q`` = correction matrix ·
sensor calibration / integration time): repeated reads of a steady patch come back bit-identical or
one count apart, and the SAME step vectors recur across patches and luminances (ProArt HDR:
``q_a=(0.083688,0.031480,-0.000197)``, ``q_b=(0.035229,0.068796,0.000812)``,
``q_c=(0.037449,0.000949,0.176918)``; the BenQ / C6 / ProArt-SDR corrections have their own). One
count is ≈0.009–0.28 ΔE2000 depending on band. Two identical reads there say the spread is below one
count — not that it is zero — so the meter's print quantisation (1e-6 per component,
:data:`dlc.mhc_cube.METER_XYZ_RESOLUTION`) is 10⁴–10⁵× too small a floor above ~15 nit.

``Q`` belongs to THIS meter + correction (.ccmx) + integration time, so it is **learned from the
session's own reads**, never a constant (:func:`learn_count_quantum`):

1. candidate lattice vectors = differences between distinct reads of the SAME patch (a counted
   patch's re-reads differ by small integer count combinations; recurring vectors rank first);
2. the candidate triple under which the most patches have an integer-count read (``Q⁻¹·XYZ``
   integral in all three channels — the crisp test) is the lattice; ``Q`` is then refit by least
   squares over every on-lattice read (so the test stays exact at thousands of counts);
3. it is accepted only if that many on-lattice patches could not arise by chance (a random point
   passes with probability ``(2·LATTICE_TOL)³ = 6.4e-5``; the binomial tail × the number of triples
   searched must be < :data:`SIGNIFICANCE`);
4. the step vectors (the covariance basis) are the unimodular basis of that lattice in which the
   observed read-to-read differences are most often ONE count — the recurring single steps.

A session too thin to learn from (few bright re-reads) falls back to a quantum an earlier session
of the SAME run learned (validated on this session's reads by the same test), else to none — the
caller then skips the count term and records why (``count_quantum.status``).

Stdlib only (the measure spine is dependency-free).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cached_property
from itertools import combinations
from typing import Any, Iterable, Mapping, Optional, Sequence

from .mhc_cube import METER_XYZ_RESOLUTION

Vec3 = tuple[float, float, float]

# |n - round(n)| per channel for a read to count as ON the lattice. The print rounding maps to
# ~1e-5 counts and the least-squares Q is exact to far better, so this is a generous margin; a
# random (period-measured) read passes all three channels with probability (2·tol)³ = 6.4e-5.
LATTICE_TOL = 0.02
# A read is a lattice TEST only when it is far from the origin (``max |n_j| >= MIN_COUNTS``) AND every
# channel carries at least one count (``min |n_j| >= 1``): a channel within ``tol`` of 0 passes the
# integrality test trivially, so a near-black read (every coordinate ≈ 0 but a print-scale one) would
# pass far more often than the (2·tol)³ chance model allows. Frequency-counted reads carry far more
# (the dimmest counted ProArt reads, ~16 nit, are ≥ 26 counts in their largest channel).
MIN_COUNTS = 20.0
# The largest XYZ component (cd/m²) a ONE-count step must reach. An i1d3 frequency count is 1/N of
# the channel's reading with N at most ~10⁴ at the ~15 nit counting threshold (≥ 1.5e-3 cd/m²); the
# recorded steps are 0.03–0.2. Differences below this are print-scale noise of near-black reads
# (1e-6 prints) — a "lattice" built from them is the print grid, not a count lattice.
MIN_STEP_XYZ = 1e-3
# A lattice is accepted only when its on-lattice patch count has chance probability below this after
# multiplying by the number of hypotheses searched.
SIGNIFICANCE = 1e-6

_TRIPLE_SEARCH_COUNTS = 1000.0   # triple scoring only tests reads within this many counts (approx Q)
_TRIPLE_SEARCH_TOL = 0.04        # ... with this looser tolerance (a single-diff Q is ±2.5e-6 per axis)
_REFINE_ROUNDS = 4


def _det(m: Sequence[Sequence[float]]) -> float:
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _inv(m: Sequence[Sequence[float]]) -> Optional[list[list[float]]]:
    """Inverse of a 3×3, ``None`` when (relatively) singular."""
    det = _det(m)
    scale = max(abs(v) for row in m for v in row) or 0.0
    if not (scale > 0.0) or abs(det) <= 1e-9 * scale ** 3:
        return None
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    k = 1.0 / det
    return [[(e * i - f * h) * k, (c * h - b * i) * k, (b * f - c * e) * k],
            [(f * g - d * i) * k, (a * i - c * g) * k, (c * d - a * f) * k],
            [(d * h - e * g) * k, (b * g - a * h) * k, (a * e - b * d) * k]]


def _mv(m: Sequence[Sequence[float]], v: Sequence[float]) -> Vec3:
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def _cols(steps: Sequence[Sequence[float]]) -> list[list[float]]:
    """The 3×3 matrix whose COLUMNS are ``steps``."""
    return [[float(steps[c][r]) for c in range(3)] for r in range(3)]


def _on(n: Sequence[float], tol: float, max_counts: float = math.inf) -> Optional[bool]:
    """Lattice verdict for count coordinates ``n``: ``None`` = not testable (too close to the origin,
    or beyond ``max_counts``), else whether every channel is integral within ``tol``."""
    mags = [abs(v) for v in n]
    if max(mags) < MIN_COUNTS or max(mags) > max_counts or min(mags) < 1.0:
        return None
    return all(abs(v - round(v)) <= tol for v in n)


def _vec(x: Any) -> Optional[Vec3]:
    try:
        v = (float(x[0]), float(x[1]), float(x[2]))
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(c) for c in v) or not (v[1] > 0.0):
        return None
    return v


@dataclass(frozen=True)
class CountQuantum:
    """A learned count lattice. ``steps`` = the XYZ change of ONE count on each channel (the
    columns of ``Q``); ``min_counted_nits`` = the dimmest read of the learning session proven ON the
    lattice (below it the session's channels were period-measured); ``source`` = ``"learned"`` or
    ``"sibling:<sidecar>"`` (adopted from an earlier session of the same run)."""

    steps: tuple[Vec3, Vec3, Vec3]
    min_counted_nits: float
    source: str = "learned"

    @cached_property
    def _inverse(self) -> list[list[float]]:
        inv = _inv(_cols(self.steps))
        if inv is None:
            raise ValueError("degenerate count lattice (steps are linearly dependent)")
        return inv

    def counts(self, xyz: Sequence[float]) -> Vec3:
        """``Q⁻¹·XYZ`` — the (real-valued) per-channel counts of ``xyz`` in this lattice."""
        return _mv(self._inverse, xyz)

    def on_lattice(self, xyz: Sequence[float], tol: float = LATTICE_TOL) -> bool:
        """True when ``xyz`` is an integer-count point of this lattice — i.e. a single read whose
        channels were all frequency-counted. A MEAN of several reads is generally NOT on the lattice;
        test the individual reads. Reads within :data:`MIN_COUNTS` of the origin are never "on"."""
        v = _vec(xyz)
        return v is not None and bool(_on(self.counts(v), tol))

    def applies(self, xyz: Sequence[float]) -> bool:
        """Whether the count floor may apply at ``xyz``'s luminance: at or above the dimmest read the
        learning session PROVED counted. Below it channels are period-measured (near-continuous) and
        the count term must not be applied. Callers holding the level's individual reads should
        test them with :meth:`on_lattice` — the stronger, per-level evidence."""
        try:
            return float(xyz[1]) >= self.min_counted_nits
        except (TypeError, ValueError, IndexError):
            return False

    def xy_sigma(self, xyz: Sequence[float]) -> float:
        """σ of a read's chromaticity (Euclidean ``xy``) from the count quantum at ``xyz``:
        ``σ² = trace(J·C·Jᵀ)``, ``C = Σ_j q_j q_jᵀ/12`` (each channel's count error uniform over one
        count, independent), ``J`` = the ``xy`` Jacobian (``∂x/∂X = (Y+Z)/S²``, ``∂x/∂Y = ∂x/∂Z =
        −X/S²``; ``∂y/∂Y = (X+Z)/S²``, ``∂y/∂X = ∂y/∂Z = −Y/S²``). ``+inf`` when ``S <= 0``."""
        X, Y, Z = (float(v) for v in xyz[:3])
        S = X + Y + Z
        if not (S > 0.0) or not math.isfinite(S):
            return math.inf
        s2 = S * S
        var = 0.0
        for qx, qy, qz in self.steps:
            dx = ((Y + Z) * qx - X * qy - X * qz) / s2
            dy = (-Y * qx + (X + Z) * qy - Y * qz) / s2
            var += dx * dx + dy * dy
        return math.sqrt(var / 12.0)

    def as_dict(self) -> dict[str, Any]:
        return {"steps": [list(s) for s in self.steps],
                "min_counted_nits": self.min_counted_nits, "source": self.source}

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]], *, source: Optional[str] = None
                  ) -> Optional["CountQuantum"]:
        """Rebuild from :meth:`as_dict` output; ``None`` when absent / malformed / degenerate."""
        if not isinstance(d, Mapping):
            return None
        try:
            steps = tuple((float(s[0]), float(s[1]), float(s[2])) for s in d["steps"])
            if len(steps) != 3 or not all(math.isfinite(c) for s in steps for c in s):
                return None
            q = cls(steps=steps, min_counted_nits=float(d.get("min_counted_nits", 0.0)),  # type: ignore[arg-type]
                    source=source or str(d.get("source") or "learned"))
            q._inverse  # noqa: B018 - validates the lattice is non-degenerate
            return q
        except (KeyError, TypeError, ValueError, IndexError):
            return None


# ---------------------------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------------------------

class _Clusters:
    """Difference vectors clustered to the meter's print precision (two prints of one lattice
    vector differ by ≤ 2 print steps per axis), with O(1) neighbour lookup on a grid."""

    def __init__(self, tol: float) -> None:
        self.tol = tol
        self.grid: dict[tuple[int, int, int], list[int]] = {}
        self.items: list[dict[str, Any]] = []     # {"sum", "n", "center", "lum", "groups"}

    def _key(self, d: Vec3) -> tuple[int, int, int]:
        return (int(math.floor(d[0] / self.tol)), int(math.floor(d[1] / self.tol)),
                int(math.floor(d[2] / self.tol)))

    def add(self, d: Vec3, lum: float, group: int) -> None:
        k = self._key(d)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for idx in self.grid.get((k[0] + dx, k[1] + dy, k[2] + dz), ()):
                        c = self.items[idx]["center"]
                        if (abs(c[0] - d[0]) <= self.tol and abs(c[1] - d[1]) <= self.tol
                                and abs(c[2] - d[2]) <= self.tol):
                            it = self.items[idx]
                            it["sum"][0] += d[0]
                            it["sum"][1] += d[1]
                            it["sum"][2] += d[2]
                            it["n"] += 1
                            it["lum"] = max(it["lum"], lum)
                            it["groups"].add(group)
                            return
        self.grid.setdefault(k, []).append(len(self.items))
        self.items.append({"sum": [d[0], d[1], d[2]], "n": 1, "center": d, "lum": lum,
                           "groups": {group}})

    def vectors(self) -> list[tuple[Vec3, int, int, float]]:
        """``(mean vector, observations, distinct patches it was seen on, brightest pair luminance)``."""
        return [((it["sum"][0] / it["n"], it["sum"][1] / it["n"], it["sum"][2] / it["n"]),
                 it["n"], len(it["groups"]), it["lum"]) for it in self.items]


def _canonical(d: Vec3) -> Vec3:
    """Sign-normalise a difference (its largest-magnitude component positive)."""
    i = max(range(3), key=lambda k: abs(d[k]))
    return d if d[i] > 0 else (-d[0], -d[1], -d[2])


def _log_binom_tail(m: int, k: int, p: float) -> float:
    """``log P(Binomial(m, p) >= k)`` (exact sum of the leading terms, log-space)."""
    if k <= 0:
        return 0.0
    if k > m or p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return 0.0
    terms = []
    lp, lq = math.log(p), math.log1p(-p)
    for i in range(k, min(m, k + 60) + 1):
        terms.append(math.lgamma(m + 1) - math.lgamma(i + 1) - math.lgamma(m - i + 1)
                     + i * lp + (m - i) * lq)
    top = max(terms)
    return top + math.log(sum(math.exp(t - top) for t in terms))


def _distinct(group: Iterable[Any]) -> list[Vec3]:
    """The distinct usable reads of one patch, in first-seen order (bit-identical repeats once)."""
    out: list[Vec3] = []
    seen: set[Vec3] = set()
    for x in group:
        v = _vec(x)
        if v is None or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _lattice_evidence(groups: Sequence[Sequence[Vec3]], inv: Sequence[Sequence[float]],
                      tol: float, max_counts: float = math.inf) -> tuple[int, int, int, float, list[Vec3]]:
    """``(patches tested, patches with an on-lattice read, on-lattice reads, mean reads per tested
    patch, the on-lattice reads)`` for the lattice with inverse ``inv``. A patch is one independent
    test: its re-reads differ by lattice vectors (they are on or off TOGETHER)."""
    tested = hit = n_reads = r_total = 0
    on_reads: list[Vec3] = []
    for g in groups:
        verdicts = []
        for v in g:
            ok = _on(_mv(inv, v), tol, max_counts)
            if ok is not None:
                verdicts.append(ok)
                if ok:
                    on_reads.append(v)
        if not verdicts:
            continue
        tested += 1
        r_total += len(verdicts)
        n_reads += sum(verdicts)
        hit += any(verdicts)
    return tested, hit, n_reads, (r_total / tested if tested else 0.0), on_reads


def _log_p_chance(tested: int, hit: int, reads_per_patch: float, tol: float, hypotheses: int) -> float:
    p = min(1.0, max(1.0, reads_per_patch) * (2.0 * tol) ** 3)
    return _log_binom_tail(tested, hit, p) + math.log(max(1, hypotheses))


def _lsq_fit(reads: Sequence[Vec3], inv: Sequence[Sequence[float]]) -> Optional[list[list[float]]]:
    """Least-squares ``Q`` from on-lattice reads: ``Q = (Σ x nᵀ)(Σ n nᵀ)⁻¹`` with ``n`` the rounded
    counts under the current lattice. ``None`` when the reads do not span three channels."""
    A = [[0.0] * 3 for _ in range(3)]
    B = [[0.0] * 3 for _ in range(3)]
    for x in reads:
        n = tuple(float(round(c)) for c in _mv(inv, x))
        for i in range(3):
            for j in range(3):
                A[i][j] += x[i] * n[j]
                B[i][j] += n[i] * n[j]
    Binv = _inv(B)
    if Binv is None:
        return None
    return [[sum(A[i][k] * Binv[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


_UNIMODULAR: Optional[list[tuple[tuple[int, int, int], ...]]] = None


def _unimodular() -> list[tuple[tuple[int, int, int], ...]]:
    """Every 3×3 integer matrix with entries in {-1, 0, 1} and ``|det| = 1`` (as column triples)."""
    global _UNIMODULAR
    if _UNIMODULAR is None:
        vs = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1) if (a, b, c) != (0, 0, 0)]
        _UNIMODULAR = [(u, v, w) for u in vs for v in vs for w in vs if abs(_det((u, v, w))) == 1]
    return _UNIMODULAR


def _reduced_basis(q_mat: Sequence[Sequence[float]]) -> list[list[float]]:
    """The lattice basis with the smallest ``Σ|q_j|²`` (Minkowski-reduced in practice): repeatedly apply
    the {-1,0,1} unimodular change of basis that most shortens the columns until none does. This makes
    the step vectors — and so the covariance ``Σ q qᵀ/12`` — a property of the LATTICE, not of which
    differences a session happened to observe first (a thin session's recurring-step ranking is noise).
    On the ProArt HDR lattice it is ``{q_a−q_b, q_b, q_c}``; the most-recurring basis ``{q_a, q_b, q_c}``
    gives a D65-grey xy σ 7 % lower — immaterial next to the 10⁴× print-floor gap this closes."""
    cur = [list(r) for r in q_mat]
    for _ in range(12):
        cols = [[cur[r][c] for r in range(3)] for c in range(3)]
        gram = [[sum(cols[i][k] * cols[j][k] for k in range(3)) for j in range(3)] for i in range(3)]

        def g(u: tuple[int, int, int]) -> float:
            return sum(u[i] * gram[i][j] * u[j] for i in range(3) for j in range(3))

        base = gram[0][0] + gram[1][1] + gram[2][2]
        best, best_u = base, None
        for U in _unimodular():
            t = g(U[0]) + g(U[1]) + g(U[2])
            if t < best * (1.0 - 1e-9):
                best, best_u = t, U
        if best_u is None:
            break
        cur = [[sum(cur[r][k] * best_u[c][k] for k in range(3)) for c in range(3)] for r in range(3)]
    return cur


def _step_observations(q_mat: Sequence[Sequence[float]],
                       clusters: Sequence[tuple[Vec3, int, int, float]]) -> list[int]:
    """How many observed read-to-read differences were exactly ±ONE count along each step (evidence)."""
    inv = _inv(q_mat)
    out = [0, 0, 0]
    if inv is None:
        return out
    for d, n, _g, _lum in clusters:
        c = _mv(inv, d)
        u = [int(round(v)) for v in c]
        if any(abs(v - r) > 2 * LATTICE_TOL for v, r in zip(c, u)):
            continue
        if sum(abs(v) for v in u) == 1:
            out[[abs(v) for v in u].index(1)] += n
    return out


_TOP_TRIPLES = 6


def _refit(q_mat: Sequence[Sequence[float]], groups: Sequence[Sequence[Vec3]]) -> Optional[list[list[float]]]:
    """Least-squares refit of ``Q`` over every on-lattice read (repeated: each pass admits reads at
    higher counts as ``Q`` sharpens). ``None`` when the start is degenerate."""
    cur = [list(r) for r in q_mat]
    if _inv(cur) is None:
        return None
    for _ in range(_REFINE_ROUNDS):
        inv = _inv(cur)
        _t, _h, _n, _r, on_reads = _lattice_evidence(groups, inv, LATTICE_TOL)
        fit = _lsq_fit(on_reads, inv) if len(on_reads) >= 3 else None
        if fit is None or _inv(fit) is None:
            break
        cur = fit
    return cur


def _pick_finest(fits: Sequence[tuple[tuple[int, int, float], list[list[float]]]]) -> list[list[float]]:
    """Most patches on the lattice, then most reads; among (near-)equals the finest lattice — a
    |det| within 1 % is the same lattice up to fit noise, so it is not a reason to switch."""
    top = max(f[0][:2] for f in fits)
    tied = [f for f in fits if f[0][:2] == top]
    return min(tied, key=lambda f: -f[0][2])[1]


def _superlattice(q_mat: Sequence[Sequence[float]], c: Sequence[float], k: int) -> list[list[float]]:
    """Basis of ``Z³ + Z·c`` (in the coordinates of ``q_mat``) for ``c`` with denominator ``k`` ∈ {2, 3}:
    reduce ``c`` into (−½, ½]³ — its entries are then 0 or ±1/k — and replace a column whose entry is
    ±1/k by ``Q·c``; the new lattice has index ``k`` over the old one and contains it."""
    r = [v - round(v) for v in c]
    j = max(range(3), key=lambda i: abs(r[i]))
    d = _mv(q_mat, r)
    cols = [[q_mat[row][col] for row in range(3)] for col in range(3)]
    cols[j] = list(d)
    return _cols(cols)


def _saturate(q_mat: list[list[float]], groups: Sequence[Sequence[Vec3]],
              clusters: Sequence[tuple[Vec3, int, int, float]], *, rounds: int = 3
              ) -> tuple[list[list[float]], int]:
    """Refine a sub-lattice to the true count lattice using the observed differences: a difference
    whose coordinates are all multiples of 1/2 (or 1/3) but not integral proposes a finer lattice
    ``Z³ + Z·c``. It is adopted only if it puts on the lattice significantly more of the patches that
    were OFF it than chance would (the added patches' binomial tail × candidates tried <
    :data:`SIGNIFICANCE`) — a genuine finer lattice recovers a whole class of counted reads, an
    accidental near-rational difference recovers ~none. Returns ``(Q, candidates tried)``."""
    tried = 0
    for _ in range(rounds):
        inv = _inv(q_mat)
        if inv is None:
            break
        tested, hit, n_on, rpp, _ = _lattice_evidence(groups, inv, LATTICE_TOL)
        proposals: dict[tuple, list[list[float]]] = {}
        for d, _n, _g, _lum in clusters:
            c = _mv(inv, d)
            if all(abs(v - round(v)) <= LATTICE_TOL for v in c):
                continue                                   # already a lattice vector
            for k in (2, 3):
                if all(abs(k * v - round(k * v)) <= LATTICE_TOL * k for v in c):
                    key = (k,) + tuple(int(round(k * v)) % k for v in c)
                    proposals.setdefault(key, _superlattice(q_mat, c, k))
                    break
        if not proposals:
            break
        tried += len(proposals)
        best: Optional[tuple[tuple[int, int], list[list[float]]]] = None
        for m in proposals.values():
            fit = _refit(m, groups)
            if fit is None:
                continue
            _t2, hit2, n_on2, _r2, _ = _lattice_evidence(groups, _inv(fit), LATTICE_TOL)
            added = hit2 - hit
            if added <= 0:
                continue
            if _log_p_chance(max(0, tested - hit), added, rpp, LATTICE_TOL, tried) >= math.log(SIGNIFICANCE):
                continue
            if best is None or (hit2, n_on2) > best[0]:
                best = ((hit2, n_on2), fit)
        if best is None:
            break
        q_mat = best[1]
    return q_mat, tried


def learn_count_quantum(groups: Iterable[Iterable[Any]], *, window: int = 8, max_candidates: int = 15,
                        max_probe_patches: int = 400) -> tuple[Optional[CountQuantum], dict[str, Any]]:
    """Learn the session's count lattice from its reads (see the module docstring for the method).

    ``groups``: the reads of each patch (time order; XYZ triples — anything else is skipped). Returns
    ``(quantum or None, evidence)``; ``evidence["status"]`` is ``"learned"`` or ``"none"`` with a
    ``reason`` (``too_few_differences`` / ``no_lattice`` / ``not_significant``) and the numbers behind
    the verdict (patches tested / on the lattice, the chance probability, the dimmest counted read).
    Never raises on data."""
    tol = 2.5 * METER_XYZ_RESOLUTION
    distinct = [d for d in (_distinct(g) for g in groups) if d]
    clusters = _Clusters(tol)
    n_diffs = 0
    for gi, g in enumerate(distinct):
        for i in range(len(g)):
            for j in range(i + 1, min(len(g), i + 1 + window)):
                a, b = g[i], g[j]
                d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
                if max(abs(c) for c in d) < MIN_STEP_XYZ:
                    continue                            # print-scale: not a count step
                n_diffs += 1
                clusters.add(_canonical(d), min(a[1], b[1]), gi)
    vecs = clusters.vectors()
    ev: dict[str, Any] = {"n_reads": sum(len(g) for g in distinct), "n_patches": len(distinct),
                          "n_differences": n_diffs, "n_clusters": len(vecs),
                          "n_recurring": sum(1 for _v, n, _g, _l in vecs if n > 1)}
    if len(vecs) < 3:
        return None, {**ev, "status": "none", "reason": "too_few_differences"}

    # Probe patches for the triple search: deterministic, evenly spaced subset.
    probe = distinct
    if len(probe) > max_probe_patches:
        step = len(probe) / max_probe_patches
        probe = [distinct[int(i * step)] for i in range(max_probe_patches)]
    # Rank: recurrence across DIFFERENT patches first (the count-step signature — the same vector
    # recurs across patches and luminances), then raw recurrence, then the brightest pair (bright
    # pairs are counted; dim ones may be period-measured, continuous, differences).
    cand = sorted(vecs, key=lambda v: (-v[2], -v[1], -v[3]))[:max_candidates]
    scored: list[tuple[tuple[int, int, float], list[list[float]]]] = []
    hypotheses = 0
    for tri in combinations(cand, 3):
        m = _cols([t[0] for t in tri])
        inv = _inv(m)
        if inv is None:
            continue
        hypotheses += 1
        _t, hit, n_on, _r, _ = _lattice_evidence(probe, inv, _TRIPLE_SEARCH_TOL, _TRIPLE_SEARCH_COUNTS)
        if hit:
            scored.append(((hit, n_on, -abs(_det(m))), m))
    if not scored:
        return None, {**ev, "status": "none", "reason": "no_lattice", "hypotheses": hypotheses}

    # The windowed search above scores on a COUNT window, which a coarser sub-lattice (smaller counts)
    # can game; so the leading triples are each refit on ALL reads and judged there.
    scored.sort(key=lambda t: t[0], reverse=True)
    fits: list[tuple[tuple[int, int, float], list[list[float]]]] = []
    for _key, m in scored[:_TOP_TRIPLES]:
        fit = _refit(m, distinct)
        if fit is not None:
            _t, hit, n_on, _r, _ = _lattice_evidence(distinct, _inv(fit), LATTICE_TOL)
            fits.append(((hit, n_on, -abs(_det(fit))), fit))
    if not fits:
        return None, {**ev, "status": "none", "reason": "no_lattice", "hypotheses": hypotheses}
    q_mat = _pick_finest(fits)
    # Saturate: an observed difference with a small-denominator (½, ⅓) coordinate is a lattice vector
    # the current basis misses (it is a sub-lattice) — adopt the finer lattice when it puts
    # significantly more patches on the lattice than chance would.
    q_mat, n_saturations = _saturate(q_mat, distinct, vecs)
    hypotheses += n_saturations
    inv = _inv(q_mat)
    if inv is None:
        return None, {**ev, "status": "none", "reason": "no_lattice", "hypotheses": hypotheses}
    tested, hit, n_on, rpp, on_reads = _lattice_evidence(distinct, inv, LATTICE_TOL)
    log_p = _log_p_chance(tested, hit, rpp, LATTICE_TOL, hypotheses)
    ev.update({"hypotheses": hypotheses, "patches_tested": tested, "patches_on_lattice": hit,
               "reads_on_lattice": n_on, "log10_p_chance": round(log_p / math.log(10.0), 1),
               "saturations": n_saturations})
    if not on_reads or log_p >= math.log(SIGNIFICANCE):
        return None, {**ev, "status": "none", "reason": "not_significant"}
    q_red = _reduced_basis(q_mat)
    steps = tuple(tuple(q_red[r][c] for r in range(3)) for c in range(3))
    if min(max(abs(c) for c in st) for st in steps) < MIN_STEP_XYZ:
        return None, {**ev, "status": "none", "reason": "print_scale_lattice"}
    step_obs = _step_observations(q_red, vecs)
    residual = max((abs(c - round(c)) for v in on_reads for c in _mv(inv, v)), default=0.0)
    q = CountQuantum(steps=steps, min_counted_nits=min(v[1] for v in on_reads), source="learned")
    ev.update({"status": "learned", "min_counted_nits": round(q.min_counted_nits, 4),
               "step_observations": step_obs, "max_count_residual": round(residual, 5)})
    return q, ev


def validate_count_quantum(quantum: CountQuantum, groups: Iterable[Iterable[Any]], *,
                           hypotheses: int = 1) -> tuple[bool, dict[str, Any]]:
    """Whether THIS session's reads sit on ``quantum``'s lattice beyond chance (same test as
    :func:`learn_count_quantum`; ``hypotheses`` = how many candidate quanta the caller is trying)."""
    distinct = [d for d in (_distinct(g) for g in groups) if d]
    try:
        inv = quantum._inverse
    except ValueError:
        return False, {"reason": "degenerate"}
    tested, hit, n_on, rpp, on_reads = _lattice_evidence(distinct, inv, LATTICE_TOL)
    log_p = _log_p_chance(tested, hit, rpp, LATTICE_TOL, hypotheses)
    ok = bool(on_reads) and log_p < math.log(SIGNIFICANCE)
    return ok, {"patches_tested": tested, "patches_on_lattice": hit, "reads_on_lattice": n_on,
                "log10_p_chance": round(log_p / math.log(10.0), 1),
                "min_counted_nits": (round(min(v[1] for v in on_reads), 4) if on_reads else None)}


def level_count_quantised(reads: Iterable[Any], quantum: Optional[CountQuantum]) -> bool:
    """A measured level is count-quantised when EVERY one of its reads is on the lattice (all its
    channels were frequency-counted). Any period-measured read → False (no count floor there)."""
    if quantum is None:
        return False
    vs = [_vec(x) for x in reads]
    return bool(vs) and all(v is not None and quantum.on_lattice(v) for v in vs)
