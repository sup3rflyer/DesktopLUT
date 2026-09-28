"""Tests for the i1d3 count-quantum learner (``dlc.meter_quantum``).

A synthetic counting meter stands in for the i1 Display3 under Argyll's adaptive scheme: above a
count threshold every channel is frequency-counted — the read is ``Q·n`` with integer counts ``n``
(printed at 6 decimals, like spotread) — below it the read is period-measured (continuous). ``Q`` is
the PA32UCXR HDR lattice recovered offline from 27 recorded runs (2026-09-28 investigation).
"""

from __future__ import annotations

import math
import random

import pytest

from dlc.meter_quantum import (
    LATTICE_TOL,
    CountQuantum,
    learn_count_quantum,
    level_count_quantised,
    validate_count_quantum,
)

# The recurring single-count steps of the ProArt HDR correction (columns of Q).
Q_A = (0.083688, 0.031480, -0.000197)
Q_B = (0.035229, 0.068796, 0.000812)
Q_C = (0.037449, 0.000949, 0.176918)
PROART = CountQuantum(steps=(Q_A, Q_B, Q_C), min_counted_nits=15.0)


def _d65(nits: float) -> tuple[float, float, float]:
    return (0.3127 / 0.3290 * nits, nits, (1.0 - 0.3127 - 0.3290) / 0.3290 * nits)


def _print(xyz):
    return tuple(round(v, 6) for v in xyz)


class CountingMeter:
    """Frequency-counted (on the lattice) at/above ``threshold_nits``, period-measured below."""

    def __init__(self, q: CountQuantum, *, threshold_nits: float = 15.0, seed: int = 3,
                 count_jitter: float = 0.35, period_noise: float = 2e-4) -> None:
        self.q = q
        self.threshold = threshold_nits
        self.rng = random.Random(seed)
        self.jitter = count_jitter
        self.period_noise = period_noise

    def read(self, true_xyz):
        if true_xyz[1] >= self.threshold:
            n = self.q.counts(true_xyz)
            k = [round(c + self.rng.uniform(-self.jitter, self.jitter)) for c in n]
            s = self.q.steps
            return _print(tuple(sum(s[j][i] * k[j] for j in range(3)) for i in range(3)))
        return _print(tuple(v * (1.0 + self.rng.gauss(0.0, self.period_noise)) for v in true_xyz))


def _stimuli(rng: random.Random, n: int):
    """Greys + coloured patches spanning 0.5-600 nit."""
    out = []
    for i in range(n):
        y = 0.5 * (1200.0 ** (i / max(1, n - 1)))
        tint = (1.0 + rng.uniform(-0.4, 0.4), 1.0, 1.0 + rng.uniform(-0.4, 0.4))
        d = _d65(y)
        out.append((d[0] * tint[0], d[1], d[2] * tint[2]))
    return out


def _session(meter: CountingMeter, *, patches: int = 80, reads: int = 3, seed: int = 5):
    rng = random.Random(seed)
    return [[meter.read(t) for _ in range(reads)] for t in _stimuli(rng, patches)]


def _same_lattice(a: CountQuantum, b: CountQuantum) -> bool:
    """``a`` and ``b`` generate the same lattice: each's steps are integer combinations of the
    other's with a unimodular change of basis."""
    U = [b.counts(s) for s in a.steps]
    if any(abs(c - round(c)) > 1e-3 for row in U for c in row):
        return False
    M = [[round(c) for c in row] for row in U]
    det = (M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1])
           - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0])
           + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]))
    return abs(det) == 1


def test_learns_the_count_lattice_from_a_sessions_repeated_reads():
    groups = _session(CountingMeter(PROART))
    q, ev = learn_count_quantum(groups)
    assert q is not None, ev
    assert ev["status"] == "learned"
    assert _same_lattice(q, PROART) and _same_lattice(PROART, q)
    # proven counted only at/above the synthetic frequency/period threshold
    assert q.min_counted_nits >= 15.0
    assert ev["log10_p_chance"] < -6
    # every counted read is on the learned lattice; period-measured reads are not
    for g in groups:
        for x in g:
            assert q.on_lattice(x) == (x[1] >= 15.0)


def test_step_vectors_are_the_reduced_basis_not_whatever_was_seen_first():
    # The covariance must be a property of the LATTICE: two sessions of the same meter with different
    # stimuli learn the SAME step vectors (the shortest basis, {q_a-q_b, q_b, q_c} here).
    q1, _ = learn_count_quantum(_session(CountingMeter(PROART, seed=1), seed=11))
    q2, _ = learn_count_quantum(_session(CountingMeter(PROART, seed=2), seed=12))
    canon = lambda q: sorted(tuple(round(abs(c), 5) for c in s) for s in q.steps)  # noqa: E731
    assert canon(q1) == canon(q2)
    expected = sorted(tuple(round(abs(c), 5) for c in s)
                      for s in ((Q_A[0] - Q_B[0], Q_A[1] - Q_B[1], Q_A[2] - Q_B[2]), Q_B, Q_C))
    assert canon(q1) == expected


def test_continuous_reads_prove_no_lattice():
    # Every read period-measured (threshold above the whole range): no lattice, and the reason says so.
    groups = _session(CountingMeter(PROART, threshold_nits=1e9))
    q, ev = learn_count_quantum(groups)
    assert q is None
    assert ev["status"] == "none" and ev["reason"] in ("no_lattice", "not_significant", "too_few_differences")


def test_single_reads_and_empty_input_are_harmless():
    assert learn_count_quantum([])[0] is None
    assert learn_count_quantum([[(1.0, 2.0, 3.0)], [None], [("x", 1, 2)]])[0] is None


def test_on_lattice_rejects_means_and_the_origin():
    x = _print(tuple(sum(s[i] * k for s, k in zip(PROART.steps, (140, 484, 228))) for i in range(3)))
    y = _print(tuple(a + b for a, b in zip(x, Q_A)))                 # one count apart
    assert PROART.on_lattice(x) and PROART.on_lattice(y)
    mean = tuple((a + b) / 2 for a, b in zip(x, y))
    assert not PROART.on_lattice(mean)                             # a mean is not a read
    assert not PROART.on_lattice((0.0, 1e-6, 0.0))                 # ~0 counts: no test at all
    assert level_count_quantised([x, y], PROART)
    assert not level_count_quantised([x, _print((x[0] + 0.02, x[1], x[2]))], PROART)   # ~¼ count off
    assert not level_count_quantised([], PROART) and not level_count_quantised([x], None)


def test_xy_sigma_matches_uniform_count_rounding_monte_carlo():
    xyz = _d65(40.0)
    sigma = PROART.xy_sigma(xyz)
    assert 2.2e-4 < sigma < 2.4e-4                                  # the offline evidence: 2.29e-4
    rng = random.Random(7)
    s0 = sum(xyz)
    d2 = []
    for _ in range(20000):
        e = [rng.uniform(-0.5, 0.5) for _ in range(3)]
        p = tuple(xyz[i] + sum(PROART.steps[j][i] * e[j] for j in range(3)) for i in range(3))
        s1 = sum(p)
        d2.append((p[0] / s1 - xyz[0] / s0) ** 2 + (p[1] / s1 - xyz[1] / s0) ** 2)
    assert math.sqrt(sum(d2) / len(d2)) == pytest.approx(sigma, rel=0.03)
    # ~1/luminance
    assert PROART.xy_sigma(_d65(400.0)) == pytest.approx(sigma / 10.0, rel=1e-9)
    assert PROART.xy_sigma((0.0, 0.0, 0.0)) == math.inf


def test_applies_only_at_or_above_the_proven_counted_luminance():
    assert PROART.applies(_d65(40.0)) and PROART.applies(_d65(15.0))
    assert not PROART.applies(_d65(1.0))


def test_validate_accepts_the_right_lattice_and_rejects_a_foreign_one():
    groups = _session(CountingMeter(PROART), patches=40, reads=2)
    ok, ev = validate_count_quantum(PROART, groups)
    assert ok and ev["patches_on_lattice"] > 5
    other = CountQuantum(steps=(tuple(1.013 * c for c in Q_A), Q_B, Q_C), min_counted_nits=15.0)
    ok, ev = validate_count_quantum(other, groups)
    assert not ok


def test_dict_roundtrip_keeps_the_lattice_exact_and_rejects_degenerate():
    d = PROART.as_dict()
    back = CountQuantum.from_dict(d)
    assert back == PROART
    assert CountQuantum.from_dict({"steps": [Q_A, Q_A, Q_C], "min_counted_nits": 1.0}) is None
    assert CountQuantum.from_dict({"steps": [Q_A, Q_B]}) is None
    assert CountQuantum.from_dict(None) is None


def test_lattice_tolerance_is_far_below_one_count():
    # The per-channel integrality tolerance must separate "one count apart" from "the same read".
    assert LATTICE_TOL < 0.1


def test_near_black_print_noise_never_becomes_a_lattice_or_a_dark_floor():
    # Review 2026-09-28: near-black reads (1e-4..1e-2 nit, 1e-6 prints, half the repeats identical)
    # used to out-rank the real count steps and yield a PRINT-SCALE "lattice" that flagged period
    # dark levels count_quantised with an xy sigma of ~10-100. Now: the true lattice, no dark level.
    seed = 4
    rng = random.Random(seed)
    meter = CountingMeter(PROART, seed=seed)
    bright = [[meter.read(t) for _ in range(3)] for t in _stimuli(rng, 40)]
    dr = random.Random(100 + seed)
    dark = []
    for i in range(80):
        t = _d65(1e-4 * 100 ** (i / 79))
        a = _print(tuple(max(0.0, v + dr.gauss(0, 3e-6)) for v in t))
        b = a if dr.random() < 0.5 else _print(tuple(max(0.0, v + dr.gauss(0, 3e-6)) for v in t))
        dark.append([a, b])
    q, ev = learn_count_quantum(bright + dark)
    assert q is not None and _same_lattice(q, PROART), ev
    assert all(max(abs(c) for c in s) > 1e-3 for s in q.steps)
    assert not any(level_count_quantised(g, q) for g in dark)
    # a lattice made of print-scale steps is refused outright
    tiny = CountQuantum(steps=((2e-6, 0.0, 0.0), (0.0, 2e-6, 0.0), (0.0, 0.0, 2e-6)), min_counted_nits=0.0)
    assert not tiny.on_lattice((0.0, 1e-4, 0.0))                   # one channel ~0: not a test at all


def test_a_coarser_sub_lattice_does_not_win_over_the_true_one():
    # Review 2026-09-28: the triple search's count window favoured an index-2 sub-lattice (smaller
    # counts) — accepted as significant, it left ~40 % of counted reads off the lattice. The leading
    # triples are now refit and judged on all reads, and observed half-steps saturate a sub-lattice.
    for seed in (4, 13, 21, 37, 51):
        rng = random.Random(seed)
        m = CountingMeter(PROART, seed=seed)
        g = [[m.read(t) for _ in range(3)] for t in _stimuli(rng, 80)]
        q, ev = learn_count_quantum(g)
        assert q is not None and _same_lattice(q, PROART), (seed, ev)


def test_a_read_with_a_channel_near_zero_counts_is_not_lattice_evidence():
    x = tuple(sum(s[i] * k for s, k in zip(PROART.steps, (0, 484, 228))) for i in range(3))
    assert not PROART.on_lattice(_print(x))                        # channel a at 0 counts: no free pass
