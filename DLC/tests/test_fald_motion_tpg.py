"""The motion TPG draws what the simulator predicts: its HLSL (tools/motion_tpg, offscreen on WARP) against
dlc.fald.motion.render_full_patch, pixel for pixel — shapes moving at sub-pixel speeds, the sync patch, the Gray-coded
counter and the readable counter. Skipped when the exe is not built (tools/motion_tpg/build.cmd)."""
import subprocess

import numpy as np
import pytest

from dlc.fald.motion import MovingShape, Scene, grey, render_full_patch
from dlc.fald.motion_image import (PAN_DIR_DEFAULT, ImageLayer, ImagePanScene, from_array, from_pan_scene,
                                   load_pan_scene, render_image_patch)
from dlc.fald.motion_tpg import EXE_DEFAULT, render_offscreen, scene_text

W, H = 480, 270

pytestmark = pytest.mark.skipif(not EXE_DEFAULT.exists(), reason="motion_tpg.exe not built")


def _scene():
    return Scene("parity", (5.0, 4.0, 6.0),
                 (MovingShape("rect", 150.3, 120.7, (1000.0, 800.0, 600.0), w=40.6, h=70.2, vx=3.37, vy=-0.41),
                  MovingShape("disc", 300.4, 160.6, grey(200.0), r=33.3, vx=-2.13),
                  MovingShape("rect", 230.0, 60.0, grey(1842.0), w=8.0, h=8.0, vx=1.5),
                  MovingShape("rect", 420.0, 60.0, grey(300.0), w=30.0, h=20.0, blink=3, blink_phase=1)),
                 pre=3, move=12, post=3, sync=(10.0, 100.0, 30.0, 30.0, 10.0, 30.0), code=(10.0, 240.0, 12.0, 10, 2.0, 12.0),
                 digits=(380.0, 230.0, 24.0, 4, 2.0, 12.0))


def _tpg_frames(tmp_path, scene, frames):
    sc_file = tmp_path / "s.scene"
    sc_file.write_text(scene_text(scene), encoding="ascii")
    cmds = [f"load {sc_file}"] + [f"dump {i} {i + 1} {tmp_path / f'f{i}.f16'}" for i in frames] + ["quit"]
    r = subprocess.run([str(EXE_DEFAULT), "--rect", f"0,0,{W},{H}", "--scene-width", str(W), "--offscreen", "--warp"],
                       input="\n".join(cmds) + "\n", capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ready offscreen warp" in r.stdout and r.stdout.count("ok dump") == len(frames), r.stdout
    return {i: np.fromfile(tmp_path / f"f{i}.f16", dtype=np.float16).reshape(H, W, 4)[..., :3].astype(np.float64)
                 .transpose(2, 0, 1) * 80.0 for i in frames}


def test_tpg_pixels_equal_the_simulator_render(tmp_path):
    sc = _scene()
    frames = [0, 1, 2, sc.pre, sc.pre + 5, sc.pre + sc.move - 1, sc.frames - 1]   # the blinker is off at 2..4, 8..10, …
    got = _tpg_frames(tmp_path, sc, frames)
    for i in frames:
        want = render_full_patch(sc, i, 0, 0, W, H)
        err = np.abs(got[i] - want) / np.maximum(want, 1.0)
        # FP16 output (relative 2^-11) and float32 shader math on scene coordinates
        assert err.max() < 2e-3, (i, float(err.max()), np.unravel_index(err.argmax(), err.shape))


def test_tpg_counter_changes_with_the_present_number(tmp_path):
    sc = _scene()
    got = _tpg_frames(tmp_path, sc, [7, 8])
    d = np.abs(got[7] - got[8]).max(axis=0)
    x0, y0, h, n, _, _ = sc.digits
    plate = d[int(y0 - h / 8) - 1:int(y0 + h + h / 8) + 2, int(x0 - h / 8) - 1:]
    assert plate.max() > 5.0                                                # 8 -> 9: segments change


# ---------------------------------------------------------------------------------------------- IMAGE scenes (2026-10-08)
# The TPG's image layers against INDEPENDENT renders written here the way results/fald_slowpan_2026-10-06/pan_scenes.py
# renders (2x canvas shifted by whole half pixels, stat * (1 - a) + mov per half-pixel cell, 2x2 mean; the 4K frame
# shifted by whole pixels with its first column repeated). D3D converts the float32 result to FP16 toward zero: < 2^-10.
FP16_REL = 1e-3


def _check(got, want, tag):
    err = np.abs(got - want) / np.maximum(want, 0.05)
    assert err.max() < FP16_REL, (tag, float(err.max()), np.unravel_index(err.argmax(), err.shape))
    # and exactly the FP16 conversion of the same value: within one FP16 step of round-to-nearest
    a = (got / 80.0).astype(np.float16).view(np.int16).astype(np.int32)
    b = (want / 80.0).astype(np.float16).view(np.int16).astype(np.int32)
    assert np.abs(a - b).max() <= 1, (tag, int(np.abs(a - b).max()))


def _canvas_scene():
    """Grid 2 (half-pixel canvas): a per-row frame (clamp), a static viewport layer, a premultiplied grey + alpha
    moving layer clipped to the viewport, half-pixel displacements both ways; camera aids on top."""
    rng = np.random.default_rng(7)
    view = (60, 40, 260, 150)                                         # screen px
    rows = np.linspace(2.0, 400.0, H).astype(np.float32)
    stat = rng.uniform(1.0, 300.0, size=(2 * (view[3] - view[1]), 2 * (view[2] - view[0]))).astype(np.float32)
    al = np.clip(rng.uniform(-0.5, 1.5, size=(300, 600)), 0.0, 1.0).astype(np.float32)
    mov = (rng.uniform(0.0, 1000.0, size=al.shape) * al).astype(np.float32)
    origin = (40, 20)                                                 # screen px of the canvas at zero shift
    v2 = tuple(2 * v for v in view)
    layers = (ImageLayer(np.repeat(rows, 2)[:, None], 0, 0, False, "clamp"),
              ImageLayer(stat, v2[0], v2[1], False, "border", v2),
              ImageLayer(np.stack([mov, al], axis=2), 2 * origin[0], 2 * origin[1], True, "border", v2))
    disp = ((0, 0), (0, 0), (1, 0), (2, 1), (3, -1), (7, 3), (-5, 5), (-5, 5))
    base = Scene("canvas", (5.0, 5.0, 5.0), (), pre=2, move=5, post=1, sync=(10.0, 10.0, 20.0, 20.0, 10.0, 30.0),
                 code=(10.0, 230.0, 8.0, 6, 2.0, 12.0), digits=(300.0, 240.0, 12.0, 3, 2.0, 12.0))
    sc = ImagePanScene(base, layers, disp, grid=2)
    return sc, dict(view=view, rows=rows, stat=stat, mov=mov, al=al, origin=origin)


def _canvas_ref(sc, d, i):
    """pan_scenes.PanScene.window_full's rule, written out: the frame rows outside the viewport, inside it
    stat * (1 - a) + mov of the canvas shifted by whole half pixels, then the 2x2 mean; the aids on top."""
    X0, Y0, X1, Y1 = d["view"]
    hx, hy = sc.disp[i]
    ox, oy = 2 * (X0 - d["origin"][0]) - hx, 2 * (Y0 - d["origin"][1]) - hy
    H2, W2 = 2 * (Y1 - Y0), 2 * (X1 - X0)
    m = d["mov"][oy:oy + H2, ox:ox + W2].astype(np.float64)
    a = d["al"][oy:oy + H2, ox:ox + W2].astype(np.float64)
    win = (d["stat"].astype(np.float64) * (1.0 - a) + m).reshape(H2 // 2, 2, W2 // 2, 2).mean(axis=(1, 3))
    full = np.repeat(d["rows"].astype(np.float64)[:, None], W, axis=1)
    full[Y0:Y1, X0:X1] = win
    return render_full_patch(sc.base, i, 0, 0, W, H, base=np.broadcast_to(full, (3, H, W)).copy())


def test_tpg_image_canvas_equals_the_pan_render():
    sc, d = _canvas_scene()
    frames = list(range(sc.frames))
    got = render_offscreen(sc, frames, (W, H))
    for i in frames:
        want = _canvas_ref(sc, d, i)
        np.testing.assert_allclose(render_image_patch(sc, i, 0, 0, W, H), want, rtol=1e-12, atol=1e-9)
        _check(got[i], want, ("canvas", i, sc.disp[i]))


def test_tpg_image_rgb_clamp_pan_with_crop():
    rng = np.random.default_rng(11)
    im = rng.uniform(0.0, 2000.0, size=(H, W, 3)).astype(np.float16)
    sc = from_array(im, "rgbpan", n_move=6, steps=[(10, 0), (10, 0), (15, 0)], pre=2, hold=2, address="clamp",
                    lead_in=1, lead_out=1, aids=False)
    assert sc.cadence == (2,) and sc.frames == 1 + 2 + 6 + 1
    assert [d[0] for d in sc.disp] == [0, 0, 0, 0, 10, 20, 35, 45, 55, 55]
    x0, y0, w, h = 20, 50, 160, 90
    got = render_offscreen(sc, range(sc.frames), (w, h), origin=(x0, y0))
    f = im.astype(np.float64)
    for k in range(sc.frames):
        dx = sc.disp[k][0]
        cols = np.maximum(np.arange(x0, x0 + w) - dx, 0)              # ImageScene: shift right, first column repeated
        _check(got[k], f[y0:y0 + h][:, cols].transpose(2, 0, 1), ("rgb", k, dx))


_HAVE_STUDY = (PAN_DIR_DEFAULT / "pan_sim.py").exists() and \
    (PAN_DIR_DEFAULT.parent / "fald_slowpan_example_2026-10-06" / "frame_nits_rgb_f16.npy").exists()


@pytest.mark.skipif(not _HAVE_STUDY, reason="slow-pan study (local results/) not present")
def test_tpg_image_equals_the_slowpan_simulator():
    """The study's own scenes through the TPG (WARP) vs the study's own renders: the anime 2:2 pan (whole-pixel steps
    10/10/15, clamp strip) and the desktop / city viewport pans (2x canvas; desktop_h_v0.5 at half-pixel offsets)."""
    lead = 3
    an = load_pan_scene("anime_pan_2to2_r5")
    sc = from_pan_scene(an, lead_in=lead, lead_out=2, aids=False)
    assert sc.cadence == (2,) and sc.frames == lead + 72 + 2
    im = np.load(an.path).astype(np.float64)
    for c, (x0, y0, w, h) in ((0, (1440, 855, 640, 360)), (5, (1440, 855, 640, 360)), (71, (0, 950, 960, 270))):
        k = c + lead
        dx = an.dx_c(c)
        assert sc.disp[k] == (dx, 0)
        got = render_offscreen(sc, [k], (w, h), origin=(x0, y0))[k]
        cols = np.maximum(np.arange(x0, x0 + w) - dx, 0)
        _check(got, im[y0:y0 + h][:, cols].transpose(2, 0, 1), ("anime", c, dx))

    for name, refreshes in (("desktop_h_v0.5", (5, 61)), ("desktop_h_v1", (40,)), ("city_h_v1", (57,))):
        ps = load_pan_scene(name)
        sc = from_pan_scene(ps, lead_in=lead, aids=False)
        X0, Y0, X1, Y1 = ps.meta()["view"]
        x0, y0, w, h = 1200, 700, 1000, 560                           # straddles the viewport's top-left corner
        for i in refreshes:
            k = i + lead
            assert sc.disp[k] == tuple(ps.disp_half(i))
            if name == "desktop_h_v0.5":
                assert ps.disp_half(i)[0] % 2 == 1                    # a half-pixel offset
            got = render_offscreen(sc, [k], (w, h), origin=(x0, y0))[k]
            full = np.repeat(np.asarray(ps.frame_rows, np.float64)[y0:y0 + h, None], w, axis=1)
            full[Y0 - y0:, X0 - x0:] = ps.window_full(i)[:h - (Y0 - y0), :w - (X0 - x0)]
            _check(got, np.broadcast_to(full, (3, h, w)), (name, i, ps.disp_half(i)))
