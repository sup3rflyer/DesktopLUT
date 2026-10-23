"""The rank-k separable K_true twin (dlc.fald.rankk; PREREG results/fald_core_speed_2026-10-22/rankk/PREREG_rankk.md
section 5.1): the rank rule on synthetic kernels and the separable form against the full convolution."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dlc.fald import rankk  # noqa: E402


def _gauss_sep(H, W, sy, sx, sub=2):
    """a rank-1 table per slice: g(r) g(c), slices shifted by their sub-offset"""
    k = np.zeros((sub, sub, H, W))
    for oy in range(sub):
        for ox in range(sub):
            r = np.arange(H) - H // 2 + oy / sub
            c = np.arange(W) - W // 2 + ox / sub
            g = np.exp(-0.5 * (r / sy) ** 2)[:, None] * np.exp(-0.5 * (c / sx) ** 2)[None, :]
            k[oy, ox] = g / g.sum()
    return k


def test_rank_one_gaussian_is_chosen_at_k_one():
    k = _gauss_sep(15, 11, 3.0, 2.5)
    ch = rankk.choose_rank(k, rows=12, cols=12)
    assert ch.k == 1 and ch.candidate
    assert ch.err[0] <= rankk.TOL_FIELD                 # only the float32 reorder term remains
    assert ch.taps_full == 15 * 11 and ch.taps_sep == 15 + 11
    assert ch.saving > 6.0


def test_random_non_negative_table_is_not_a_candidate():
    rng = np.random.default_rng(20261023)
    k = rng.random((2, 2, 9, 9))                        # full rank 9 > K_MAX
    ch = rankk.choose_rank(k, rows=12, cols=12)
    assert ch.k == 0 and not ch.candidate and ch.taps_sep == 0
    assert np.all(ch.err > rankk.TOL_FIELD)             # every k <= 8 misses
    # the error falls monotonically with k (more terms kept)
    assert np.all(np.diff(ch.err) <= 1e-12)


def test_separable_form_equals_the_full_convolution_for_a_rank_one_kernel():
    k = _gauss_sep(15, 11, 3.0, 2.5)
    U, V = rankk.separable_tables(k, 1)
    rng = np.random.default_rng(7)
    d = rng.random((12, 16)).astype(np.float32)
    b = rankk.conv_rank(d, U, V)
    f = rankk.conv_full32(d, k)
    assert b.shape == f.shape == (24, 32)
    assert np.max(np.abs(b - f) / np.maximum(np.abs(f), 1e-6)) < 1e-5   # float32 reorder only


def test_full_rank_tables_reproduce_the_full_convolution():
    rng = np.random.default_rng(3)
    k = rng.random((2, 2, 7, 5))
    U, V = rankk.separable_tables(k, 5)                 # rank 5 = min(7, 5): the SVD is exact
    d = rng.random((10, 12)).astype(np.float32)
    b = rankk.conv_rank(d, U, V)
    f = rankk.conv_full32(d, k)
    assert np.max(np.abs(b - f) / np.maximum(np.abs(f), 1e-6)) < 1e-4


def test_truncation_error_bound_holds_on_random_drives():
    """err_s(k) bounds the field's relative error over ANY drive map in [0, 1]: a sample of random maps stays under it."""
    rng = np.random.default_rng(11)
    # an exactly rank-2 table (two separable Gaussians) plus a non-separable part far below the tolerance: k = 2 is chosen
    # with the residual + the float32 reorder term as its error, and the field's error on real drives must stay under it
    base = _gauss_sep(13, 9, 2.0, 1.5) + 0.3 * _gauss_sep(13, 9, 4.0, 3.0)
    k = base + 1e-7 * rng.random(base.shape) * base.max()
    rows, cols = 12, 14
    ch = rankk.choose_rank(k, rows, cols)
    assert ch.k == 2, ch.err
    U, V = rankk.separable_tables(k, ch.k)
    flat_r = rankk.conv_rank(np.ones((rows, cols)), U, V)
    flat_f = rankk.conv_full32(np.ones((rows, cols)), k)
    for _ in range(5):
        d = rng.random((rows, cols)).astype(np.float32)
        b = rankk.conv_rank(d, U, V)
        f = rankk.conv_full32(d, k)
        raw = np.abs(b.astype(np.float64) - f) / np.maximum(flat_f, 1e-30)
        assert raw.max() <= ch.err[ch.k - 1] + 1e-9
        e = rankk.field_error(b, f, flat_r, flat_f)
        assert e["n"] > 0 and e["max_rel"] <= 2.0 * ch.err[ch.k - 1] + 1e-9   # the ratio moves by at most both paths' errors


def test_tables_are_deterministic_in_sign():
    k = _gauss_sep(9, 7, 2.0, 2.0)
    U, V = rankk.separable_tables(k, 1)
    assert np.all(V[..., V.shape[-1] // 2] > 0)         # the largest-magnitude entry (the centre) positive
    U2, V2 = rankk.separable_tables(k, 1)
    assert np.array_equal(U, U2) and np.array_equal(V, V2)
