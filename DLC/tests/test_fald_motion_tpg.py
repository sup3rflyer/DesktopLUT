"""The motion TPG draws what the simulator predicts: its HLSL (tools/motion_tpg, offscreen on WARP) against
dlc.fald.motion.render_full_patch, pixel for pixel — shapes moving at sub-pixel speeds, the sync patch, the Gray-coded
counter and the readable counter. Skipped when the exe is not built (tools/motion_tpg/build.cmd)."""
import subprocess

import numpy as np
import pytest

from dlc.fald.motion import MovingShape, Scene, grey, render_full_patch
from dlc.fald.motion_tpg import EXE_DEFAULT, scene_text

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
