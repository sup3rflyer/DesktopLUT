from dlc.colormath import xy_to_XYZ
from dlc.grayscale_wb import (
    GrayTouchupConfig,
    GrayTouchupPatch,
    identity_payload,
    update_point,
)


def test_touchup_moves_luminance_and_balance():
    points = [0.0, 0.5, 1.0]
    payload = identity_payload(points)
    cfg = GrayTouchupConfig(per_iteration_cap=0.10)
    patch = GrayTouchupPatch(
        level=0.5,
        measured_xyz=xy_to_XYZ(0.3000, 0.3400, 24.0),
        target_y=30.0,
    )

    updated, digest = update_point(payload, 1, patch, cfg)

    assert digest["large_luminance_correction"] is True
    assert updated["luminance"][1] > 1.0
    assert any(abs(updated["rgb"][ch][1] - 1.0) > 1e-4 for ch in "rgb")
    for ch in "rgb":
        assert updated["deviations"][ch][1] == updated["luminance"][1] * updated["rgb"][ch][1]


def test_touchup_caps_large_correction():
    points = [0.0, 1.0]
    payload = identity_payload(points)
    cfg = GrayTouchupConfig(per_iteration_cap=0.50, max_abs_delta=0.02)
    patch = GrayTouchupPatch(
        level=1.0,
        measured_xyz=xy_to_XYZ(0.2700, 0.3800, 70.0),
        target_y=120.0,
    )

    updated, digest = update_point(payload, 1, patch, cfg)

    assert digest["capped"] is True
    assert max(abs(updated["deviations"][ch][1] - 1.0) for ch in "rgb") <= 0.0200001
