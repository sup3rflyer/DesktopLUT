"""Offline tests for dlc.phone.analysis: synthetic clips generated with ffmpeg in tmp_path (lossless, so every code is
known), synthetic series and a rendered + warped fid2 frame. No phone, display, pipe or TPG involved."""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from dlc.phone import analysis as an
from dlc.phone.clip import probe

HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FF, reason="needs ffmpeg + ffprobe")
LEGACY_TONE = (Path(__file__).resolve().parents[1] / "results" / "phone_camera_2026-09-19" / "video_step0"
               / "tone_response.json")


@functools.lru_cache(maxsize=None)
def _has_x265() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


def _write_clip(path: Path, frame_fn, n: int, w: int = 64, h: int = 36, *, fps: int = 120, bits: int = 10,
                drop: tuple[int, ...] = (), keyint: int | None = None) -> Path:
    """Lossless clip whose frame k has Y = frame_fn(k) (chroma mid-grey); ``drop`` removes source frames but keeps
    the survivors' timestamps (a pts gap, like an HS drop)."""
    pf = "yuv420p10le" if bits == 10 else "yuv420p"
    dt = np.uint16 if bits == 10 else np.uint8
    if bits == 10 and _has_x265():
        params = "lossless=1:log-level=error" + (f":keyint={keyint}:min-keyint={keyint}" if keyint else "")
        enc = ["-c:v", "libx265", "-x265-params", params, "-tag:v", "hvc1"]
    else:
        enc = ["-c:v", "libx264", "-qp", "0", "-preset", "ultrafast"] + (["-g", str(keyint)] if keyint else [])
    vf = ["-vf", "select='" + "*".join(f"not(eq(n\\,{d}))" for d in drop) + "'"] if drop else []
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pf, "-s", f"{w}x{h}",
                             "-r", str(fps), "-i", "-", *vf, "-fps_mode", "passthrough", *enc, "-pix_fmt", pf,
                             str(path)], stdin=subprocess.PIPE)
    mid = np.full(((h + 1) // 2, (w + 1) // 2), 512 if bits == 10 else 128, dt)
    for k in range(n):
        y = np.asarray(frame_fn(k), dtype=dt)
        assert y.shape == (h, w)
        proc.stdin.write(y.tobytes() + mid.tobytes() + mid.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return path


def _ramp(k, w=64, h=36):
    """Left half 200 + k, right half 400 + k, a 4x4 block at (60, 32) carries the source frame number."""
    y = np.empty((h, w), np.uint16)
    y[:, : w // 2] = 200 + k
    y[:, w // 2:] = 400 + k
    y[32:36, 60:64] = 10 + k
    return y


# --- frames, pts, ROI series --------------------------------------------------------------------------------


@needs_ffmpeg
def test_iter_frames_exact_codes_on_container_pts_across_a_drop(tmp_path):
    clip = _write_clip(tmp_path / "hs.mp4", _ramp, 24, drop=(10,))
    info = probe(clip)
    assert info.gaps == 1 and info.bits == 10
    got = list(an.iter_frames(info))
    kept = [k for k in range(24) if k != 10]
    assert len(got) == 23 == info.frames
    t = np.array([g[0] for g in got])
    np.testing.assert_allclose(t, np.array(kept) / 120.0, atol=1e-9)         # pts, NOT index/fps (gap at 10)
    np.testing.assert_allclose(t, info.pts, atol=2e-6)                        # same timeline as clip.probe
    for (_, f), k in zip(got, kept):
        assert f.dtype == np.uint16 and f.shape == (36, 64)
        np.testing.assert_array_equal(f, _ramp(k))                            # bit-exact 10-bit codes, no range scaling
    u = next(an.iter_frames(clip, plane="u"))[1]
    assert u.shape == (18, 32) and np.all(u == 512)
    _, (y, u2, v2) = next(an.iter_frames(clip, plane="yuv"))                 # the full-frame pipe (no Y fast path)
    assert y.shape == (36, 64) and u2.shape == v2.shape == (18, 32)
    np.testing.assert_array_equal(y, _ramp(0))
    g = an.iter_frames(clip)                                                  # early stop kills ffmpeg cleanly
    next(g)
    g.close()


@needs_ffmpeg
def test_roi_series_one_pass_mean_std_mask_and_pts(tmp_path):
    clip = _write_clip(tmp_path / "r.mp4", _ramp, 24, drop=(10,))
    mask = np.zeros((10, 20), bool)
    mask[:, 10:] = True                                                       # right half of the straddling ROI
    rs = an.roi_series(clip, {"both": (22, 5, 20, 10), "masked": (22, 5, 20, 10), "id": (60, 32, 4, 4)},
                       masks={"masked": mask})
    k = np.rint(rs.t * 120).astype(int)
    assert 10 not in k and len(rs) == 23
    np.testing.assert_allclose(rs.mean["both"], 300 + k)
    np.testing.assert_allclose(rs.std["both"], 100)
    np.testing.assert_allclose(rs.mean["masked"], 400 + k)
    np.testing.assert_allclose(rs.std["masked"], 0)
    np.testing.assert_allclose(rs.mean["id"], 10 + k)                        # the frame carries its own number
    assert rs.n_px == {"both": 200, "masked": 100, "id": 16} and rs.bits == 10 and rs.tone == "identity"
    s = rs["id"]
    assert isinstance(s, an.Series) and s.t is rs.t
    d = rs.as_dict()
    assert set(d) == {"t", "both", "both_sd", "masked", "masked_sd", "id", "id_sd"}
    npz = np.load(rs.save_npz(tmp_path / "r.npz"))
    assert json.loads(str(npz["_meta"]))["rois"]["id"] == [60, 32, 4, 4]
    # tone applied per pixel before averaging: mean of linear, not linear of mean
    tone = an.ToneCurve.power(black=0, white=1000, gamma=2.0, bits=10)
    rl = an.roi_series(clip, {"both": (22, 5, 20, 10)}, tone=tone, std=False)
    kk = np.rint(rl.t * 120)
    np.testing.assert_allclose(rl.mean["both"], 0.5 * (((200 + kk) / 1000) ** 2 + ((400 + kk) / 1000) ** 2))
    assert rl.std == {} and rl.tone == "power"
    sub = an.roi_series(clip, {"id": (60, 32, 4, 4)}, start_s=5 / 120, dur_s=8 / 120)
    np.testing.assert_allclose(sub.mean["id"], 10 + np.array([5, 6, 7, 8, 9, 11, 12]))


@needs_ffmpeg
def test_display_rotation_applied_by_default_coded_on_request(tmp_path):
    """S25 mount clips carry a 180-degree display matrix: frames come display-oriented (as the 09-xx tools saw them)
    unless autorotate=False; a 90-degree tag swaps width and height. Codes are only permuted."""
    src = _write_clip(tmp_path / "c.mp4", _ramp, 3)
    rot = {}
    for deg in (180, 90):
        rot[deg] = tmp_path / f"r{deg}.mp4"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation:v:0", str(deg), "-i", str(src),
                            "-c", "copy", str(rot[deg])], capture_output=True)
        if r.returncode:
            pytest.skip("this ffmpeg has no -display_rotation")
    assert an.clip_rotation(src) == 0 and abs(an.clip_rotation(rot[180])) == 180 and an.clip_rotation(rot[90]) == 90
    np.testing.assert_array_equal(next(an.iter_frames(rot[180], autorotate=False))[1], _ramp(0))
    np.testing.assert_array_equal(next(an.iter_frames(rot[180]))[1], np.rot90(_ramp(0), 2))
    np.testing.assert_array_equal(next(an.iter_frames(rot[180], plane="yuv"))[1][0], np.rot90(_ramp(0), 2))
    d90 = next(an.iter_frames(rot[90]))[1]
    np.testing.assert_array_equal(d90, np.rot90(_ramp(0), 1))                # counter-clockwise, 36x64 -> 64x36
    rs = an.roi_series(rot[90], {"id": (32, 0, 4, 4), "all": (0, 0, 36, 64)})
    np.testing.assert_allclose(rs.mean["id"], 10 + np.arange(3))
    assert an.mean_frame(rot[90]).shape == (64, 36)


@needs_ffmpeg
def test_roi_and_argument_validation(tmp_path):
    clip = _write_clip(tmp_path / "v.mp4", _ramp, 4)
    with pytest.raises(ValueError, match="outside"):
        an.roi_series(clip, {"a": (60, 30, 10, 10)})
    with pytest.raises(ValueError, match="integer"):
        an.roi_series(clip, {"a": (1.5, 0, 4, 4)})
    with pytest.raises(ValueError, match="shape"):
        an.roi_series(clip, {"a": (0, 0, 4, 4)}, masks={"a": np.ones((3, 4), bool)})
    with pytest.raises(ValueError, match="no pixels"):
        an.roi_series(clip, {"a": (0, 0, 4, 4)}, masks={"a": np.zeros((4, 4), bool)})
    with pytest.raises(ValueError, match="plane"):
        an.iter_frames(clip, plane="r")
    with pytest.raises(ValueError, match="pix_fmt"):
        an.iter_frames(clip, pix_fmt="rgb24")
    with pytest.raises(ValueError, match="no frames"):
        an.mean_frame(clip, 10.0, 1.0)
    with pytest.raises(ValueError, match="10-bit"):                         # an 8-bit curve on 10-bit frames
        an.roi_series(clip, {"a": (0, 0, 4, 4)}, tone=an.ToneCurve.power(16, 235, 2.2, bits=8))


@needs_ffmpeg
def test_window_seek_matches_full_decode_and_overshoot_redecodes(tmp_path, monkeypatch):
    clip = _write_clip(tmp_path / "long.mp4", lambda k: np.full((36, 64), 10 + k), 480, keyint=50)
    info = probe(clip)
    full = [(t, int(f[0, 0])) for t, f in an.iter_frames(info)]
    assert [v for _, v in full] == list(range(10, 490))
    seeks = []
    real = an._decode

    def spy(*a, **kw):
        seeks.append(kw["seek_s"])
        return real(*a, **kw)

    monkeypatch.setattr(an, "_decode", spy)
    for s0, d in ((1.5, 0.3), (2.004, 0.1), (3.9, 1.0), (1.2, None)):
        sub = [(t, int(f[0, 0])) for t, f in an.iter_frames(info, start_s=s0, dur_s=d)]
        exp = [x for x in full if x[0] >= s0 - 1e-9 and (d is None or x[0] < s0 + d - 1e-9)]
        assert sub == exp
    assert all(s is not None for s in seeks)                                 # the seek path was exercised
    dur_only = [t for t, _ in an.iter_frames(info, dur_s=0.1)]
    np.testing.assert_allclose(dur_only, np.arange(12) / 120)

    def overshoot(*a, **kw):                                                 # a seek that lands after the window
        for t, buf in real(*a, **kw):
            if kw["seek_s"] is None or t >= 2.6:
                yield t, buf

    seeks.clear()
    monkeypatch.setattr(an, "_decode", lambda *a, **kw: (seeks.append(kw["seek_s"]), overshoot(*a, **kw))[1])
    sub = [(t, int(f[0, 0])) for t, f in an.iter_frames(info, start_s=2.5, dur_s=0.2)]
    assert sub == [x for x in full if 2.5 - 1e-9 <= x[0] < 2.7 - 1e-9]
    assert seeks[0] is not None and seeks[-1] is None                        # detected and re-decoded from the top


@needs_ffmpeg
def test_8bit_clip_native_codes_and_forced_10bit(tmp_path):
    clip = _write_clip(tmp_path / "bm.mp4", lambda k: np.full((36, 64), 30 + 5 * k), 6, fps=60, bits=8)
    got = list(an.iter_frames(clip))
    assert got[0][1].dtype == np.uint8
    assert [int(f[0, 0]) for _, f in got] == [30, 35, 40, 45, 50, 55]
    np.testing.assert_allclose([t for t, _ in got], np.arange(6) / 60, atol=1e-9)
    t, f10 = next(an.iter_frames(clip, pix_fmt="yuv420p10le"))
    assert f10.dtype == np.uint16 and int(f10[0, 0]) == 30 * 4                # explicit conversion is the caller's ask


@needs_ffmpeg
def test_mean_frame_and_scale(tmp_path):
    clip = _write_clip(tmp_path / "m.mp4", _ramp, 12)
    m, n = an.mean_frame(clip, 2 / 120, 4 / 120, return_n=True)
    assert n == 4 and m.dtype == np.float64
    np.testing.assert_allclose(m, _ramp(0).astype(float) + 3.5)               # frames 2..5
    small = an.mean_frame(clip, scale=(32, 18))
    assert small.shape == (18, 32)
    np.testing.assert_allclose(small[:, :15], 200 + 5.5, atol=0.5)            # area-averaged halves, codes kept
    np.testing.assert_allclose(small[:16, 17:], 400 + 5.5, atol=0.5)


# --- tone curves --------------------------------------------------------------------------------------------


def test_tone_curves_kinds_roundtrip_and_json(tmp_path):
    ident = an.ToneCurve()
    assert ident.is_identity and ident(5) == 5.0 and an.IDENTITY.is_identity
    lg = an.ToneCurve.log10(227.78, 2.604e-4, 3.339e-3, 493.76, bits=10, sat_code=800)
    codes = np.array([300.0, 450.0, 700.0])
    np.testing.assert_allclose(lg.inverse(lg(codes)), codes, rtol=1e-12)
    assert lg.saturated([799, 800]).tolist() == [False, True]
    pw = an.ToneCurve.power(black=64, white=940, gamma=2.4, scale=100.0)
    assert pw(940) == pytest.approx(100.0) and pw(64) == 0.0 and pw(60) < 0  # noise below black stays signed
    np.testing.assert_allclose(pw.inverse(pw(codes)), codes)
    tb = an.ToneCurve.table([100, 200, 400], [1.0, 3.0, 11.0], bits=10)
    np.testing.assert_allclose(tb([100, 150, 300, 500, 50]), [1.0, 2.0, 7.0, 15.0, 0.0])   # linear extrapolation
    np.testing.assert_allclose(tb.inverse([2.0, 15.0]), [150.0, 500.0])
    for tc in (lg, pw, tb):
        back = an.ToneCurve.from_json(tc.to_json(tmp_path / f"{tc.name}.json"))
        np.testing.assert_allclose(back(codes), tc(codes))
        assert (back.kind, back.bits, back.sat_code) == (tc.kind, tc.bits, tc.sat_code)
        assert back.meta["path"].endswith(f"{tc.name}.json")
    fn = an.ToneCurve.from_function(lambda c: c * 2.0, "double", inverse=lambda v: v / 2.0, bits=10)
    assert fn(3) == 6.0 and fn.inverse(6) == 3.0
    with pytest.raises(ValueError, match="serialised"):
        fn.to_dict()
    with pytest.raises(ValueError):
        an.ToneCurve(kind="log10", params={"a": 1})
    with pytest.raises(ValueError):
        an.ToneCurve.table([1, 1, 2], [0, 1, 2])
    an.ToneCurve.identity(bits=8).check_bits(8)
    with pytest.raises(ValueError, match="10-bit codes, the frames are 8-bit"):
        tb.check_bits(8)


def test_tone_curve_loads_the_legacy_0919_shape(tmp_path):
    legacy = {"video": {"mode": "Pro Video FHD120 LOG"}, "sat_code": 800,
              "fit": {"form": "code = a*log10(b*L + c) + d (L = RAW green counts per second, ISO 50 f/1.8)",
                      "a": 227.7835944436849, "b": 0.0002604038582189093, "c": 0.0033390779792421285,
                      "d": 493.75541018899014}}
    p = tmp_path / "tone_response.json"
    p.write_text(json.dumps(legacy))
    tc = an.ToneCurve.from_json(p, bits=10)
    f = legacy["fit"]
    code = np.array([392.18562544903153, 719.4623458109429])
    np.testing.assert_allclose(tc(code), (10 ** ((code - f["d"]) / f["a"]) - f["c"]) / f["b"])  # agent_phonegeo.lin
    assert tc.kind == "log10" and tc.bits == 10 and tc.sat_code == 800 and "RAW green" in tc.units
    assert tc.name == "s25-pro-video-fhd120-log"
    if LEGACY_TONE.exists():                                                  # the real (gitignored) file, if present
        real = an.ToneCurve.from_json(LEGACY_TONE, bits=10)
        np.testing.assert_allclose(real(code), tc(code))
    with pytest.raises(ValueError):
        an.ToneCurve.from_dict({"fit": {"form": "code = a*L^b", "a": 1, "b": 2, "c": 3, "d": 4}})


# --- sync, folding, epochs ----------------------------------------------------------------------------------


def _exposed_square(t_frames, on_times, off_times, shutter, lo=100.0, hi=700.0):
    """Mean level of a square during each frame's exposure [t, t + shutter): fraction-on x contrast."""
    out = []
    for t in t_frames:
        frac = 0.0
        for a, b in zip(on_times, off_times):
            frac += max(0.0, min(t + shutter, b) - max(t, a))
        out.append(lo + (hi - lo) * frac / shutter)
    return np.asarray(out)


def test_sync_edges_subframe_timing_noise_and_drops():
    dt = 1 / 120
    t_full = np.arange(600) * dt
    on, off = [1.0 + 0.37 * dt, 2.5 + 0.81 * dt], [1.6 + 0.12 * dt, 3.4 + 0.5 * dt]
    steps = [on[0], off[0], on[1], off[1]]
    # (a) a 2-frame box exposure puts >= 2 samples on every ramp: the half-height crossing sits exactly half an
    #     exposure before the step (a constant camera offset), on pts, through noise and drops away from the edges
    t = np.delete(t_full, [60, 400])
    e = 2 * dt
    y = _exposed_square(t, on, off, shutter=e) + np.random.default_rng(1).normal(0, 3.0, t.size)
    edges = an.sync_edges(an.Series(t, y))
    assert [ed.kind for ed in edges] == ["rise", "fall", "rise", "fall"]
    np.testing.assert_allclose([ed.t for ed in edges], np.array(steps) - e / 2, atol=0.25e-3)
    assert all(ed.span_s == pytest.approx(dt) for ed in edges)
    fixed = an.sync_edges((t, y), threshold=400.0)
    np.testing.assert_allclose([ed.t for ed in fixed], np.array(steps) - e / 2, atol=0.25e-3)
    lv = an.two_levels(y)
    assert lv.lo == pytest.approx(100, abs=1.5) and lv.hi == pytest.approx(700, abs=1.5) and lv.noise < 5
    # (b) a 1-frame exposure leaves one sample on the ramp: the linear crossing then has a phase-dependent bias
    #     bounded by ~0.09 frame (0.5/f - 1.5 + f over f in [0.5, 1])
    y1 = _exposed_square(t, on, off, shutter=dt)
    e1 = an.sync_edges((t, y1))
    assert np.max(np.abs(np.array([ed.t for ed in e1]) - (np.array(steps) - dt / 2))) <= 0.09 * dt
    # (c) a frame dropped AT an edge: still found, and span_s = 2 frames flags the coarser bracket
    t2 = np.delete(t_full, [120])
    e2 = an.sync_edges((t2, _exposed_square(t2, on, off, shutter=e)))
    assert e2[0].span_s == pytest.approx(2 * dt) and abs(e2[0].t - (on[0] - e / 2)) < dt
    assert [ed.span_s == pytest.approx(dt) for ed in e2[1:]] == [True, True, True]
    # never toggles -> no edges
    assert an.sync_edges((t, np.full(t.size, 300.0))) == []
    assert an.sync_edges((t, np.random.default_rng(2).normal(300, 3, t.size))) == []


def test_fold_frame_slots_and_epochs():
    dt = 1 / 120
    t = np.delete(np.arange(240) * dt, [7, 100, 101])
    y = np.where(np.rint(t / dt) % 2 == 0, 10.0, 20.0)                      # 60-Hz beat on the capture slots
    slots = an.frame_slots(t)
    assert slots[7] == 8 and np.all(np.where(slots % 2 == 0, 10.0, 20.0) == y)
    assert not np.all(np.where(np.arange(t.size) % 2 == 0, 10.0, 20.0) == y)   # index parity breaks at a drop
    fo = an.fold_by_period((t, y), 1 / 60, t0=-dt / 2, bins=2)            # samples at the bin centres
    np.testing.assert_allclose(fo.mean, [10.0, 20.0])
    np.testing.assert_allclose(fo.std, [0.0, 0.0], atol=1e-9)
    assert fo.n.sum() == t.size and fo.cycle[-1] == 119 and np.all((fo.phase >= 0) & (fo.phase < 1))
    assert an.fold_by_period((t, y), 1 / 60, bins=4).n.sum() == t.size
    grid = np.arange(-2, 3) * dt
    E, kept = an.epochs(an.Series(t, t * 100.0), [0.0, 0.5, 1.0, 1.99], grid)
    assert kept.tolist() == [0.5, 1.0] and E.shape == (2, 5)
    np.testing.assert_allclose(E, (np.array([[0.5], [1.0]]) + grid) * 100.0)


@needs_ffmpeg
def test_sync_square_in_a_clip_end_to_end(tmp_path):
    """A sync square switches on at a known sub-frame time; roi_series + sync_edges recover it on pts (a frame
    is dropped just before the edge, so index x 1/fps would be a whole frame off)."""
    dt, n, e = 1 / 120, 120, 2 / 120
    t_on, t_off = 0.4 + 0.3 * dt, 0.75 + 0.6 * dt
    lv = _exposed_square(np.arange(n) * dt, [t_on], [t_off], shutter=e)

    def frame(k):
        y = np.full((36, 64), 64, np.uint16)
        y[8:24, 8:24] = int(round(lv[k]))
        return y

    clip = _write_clip(tmp_path / "sync.mp4", frame, n, drop=(46,))
    rs = an.roi_series(clip, {"sync": (10, 10, 12, 12)})
    edges = an.sync_edges(rs["sync"])
    assert [e.kind for e in edges] == ["rise", "fall"]
    np.testing.assert_allclose([edges[0].t, edges[1].t], [t_on - e / 2, t_off - e / 2], atol=0.1e-3)
    by_index = edges[0].i / 120                                               # what frame-index timing would claim
    assert abs(by_index - rs.t[edges[0].i]) == pytest.approx(dt)


# --- geometry -----------------------------------------------------------------------------------------------


def _render(layout, H, shape, *, ss=3, bg=5.0, level=40.0, noise=0.3, blur=0.8, extra=(), seed=0):
    """Supersampled render of the layout seen through H (screen px -> image px), optics blur + read noise."""
    from scipy import ndimage
    h, w = shape
    o = (np.arange(ss) + 0.5) / ss - 0.5
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    Hi = np.linalg.inv(H)
    acc = np.zeros(shape)
    for dy in o:
        for dx in o:
            q = an.apply_h(Hi, np.c_[(xx + dx).ravel(), (yy + dy).ravel()])
            X, Y = q[:, 0].reshape(shape), q[:, 1].reshape(shape)
            v = np.full(shape, bg)
            for x, y, rw, rh in layout.rects():
                v[(X >= x) & (X < x + rw) & (Y >= y) & (Y < y + rh)] = level
            acc += v
    img = ndimage.gaussian_filter(acc / ss ** 2, blur)
    for cx, cy, r in extra:                                                   # distractors (reflections, dust)
        img[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = level * 0.8
    return img + np.random.default_rng(seed).normal(0.0, noise, shape)


def _sim(s, deg, tx, ty, persp=(4e-6, 6e-6)):
    c, sn = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[s * c, -s * sn, tx], [s * sn, s * c, ty], [persp[0], persp[1], 1.0]])


def _max_dev(fit, H, layout, shape):
    gx, gy = np.meshgrid(np.linspace(0, layout.screen[0], 25), np.linspace(0, layout.screen[1], 15))
    g = np.c_[gx.ravel(), gy.ravel()]
    p = an.apply_h(H, g)
    inside = (p[:, 0] >= 0) & (p[:, 0] < shape[1]) & (p[:, 1] >= 0) & (p[:, 1] < shape[0])
    return float(np.hypot(*(fit.apply(g) - p)[inside].T).max())


def test_homography_dlt_and_roi():
    H = _sim(0.5, 3.0, 12.0, -7.0, persp=(1e-5, -2e-5))
    src = np.array([[0, 0], [3840, 0], [0, 2160], [3840, 2160], [1920, 1080], [500, 1700]], float)
    np.testing.assert_allclose(an.homography(src, an.apply_h(H, src)), H, rtol=1e-8, atol=1e-10)
    with pytest.raises(ValueError):
        an.homography(src[:3], src[:3])
    x, y, w, h = an.roi(np.diag([0.5, 0.5, 1.0]), 100, 200, 300, 260)
    assert (x, y, w, h) == (50, 100, 100, 30)


def test_fit_fiducials_recovers_homography_upright_rotated_and_custom():
    shape = (540, 960)
    for H, rotated in ((_sim(0.23, 1.5, 30, 20), False), (_sim(0.23, 181.0, 930, 520), True)):
        img = _render(an.FID2, H, shape, extra=[(470, 60, 9)])
        fit = an.fit_fiducials(img, an.FID2)
        assert fit.n == fit.n_total == 10 and fit.bar_ok is True and fit.rotated_180 is rotated
        assert fit.err_px < 0.5 and _max_dev(fit, H, an.FID2, shape) < 0.5
        again = an.FiducialFit.from_dict(json.loads(json.dumps(fit.to_dict())))
        np.testing.assert_allclose(again.H, fit.H)
        assert again.roi(340, 970, 560, 1190) == fit.roi(340, 970, 560, 1190)
    # a different layout (5 marks + a bar), passed by the caller
    lay = an.FiducialLayout("tri", (1920, 1080), marks=((0, 0, 80, 80), (1840, 0, 80, 80), (920, 500, 80, 80),
                                                        (0, 1000, 80, 80), (1840, 1000, 80, 80)),
                            bar=(1500, 900, 160, 40))
    H = _sim(0.4, -2.0, 60, 40, persp=(-8e-6, 1e-5))
    fit = an.fit_fiducials(_render(lay, H, shape, seed=3), lay)
    assert fit.n == 6 and fit.bar_ok and not fit.rotated_180 and _max_dev(fit, H, lay, shape) < 0.5
    with pytest.raises(an.FiducialError):
        an.fit_fiducials(np.random.default_rng(0).normal(5, 0.3, shape), an.FID2)


def test_fit_fiducials_bar_off_image_is_unverified_not_false():
    """The 2026-09-19 video geometry: the top row (and the bar) fall outside the 1080p frame."""
    H = np.array([[0.5172617603542257, 0.006620651329421983, -2.9933836962882445],
                  [0.00748400049924328, 0.5170813111373753, -121.25663069750455],
                  [6.671418683869571e-06, 1.2023074083925228e-05, 1.0]])
    shape = (540, 960)
    Hs = np.diag([0.5, 0.5, 1.0]) @ H                                       # same view, half-res for test speed
    fit = an.fit_fiducials(_render(an.FID2, Hs, shape, ss=2), an.FID2)
    assert fit.n == 7 and fit.bar_ok is None and not fit.rotated_180
    assert _max_dev(fit, Hs, an.FID2, shape) < 0.5


def test_lazy_package_exports():
    import dlc.phone as ph
    assert ph.roi_series is an.roi_series and ph.FID2 is an.FID2 and "sync_edges" in ph.__all__
    with pytest.raises(AttributeError):
        ph.not_a_name  # noqa: B018
