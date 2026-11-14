"""grayscale-wb in HDR (review of 3374b91, item 7): where the HDR held-out greys go, and that the
before/after judgment runs end to end with an HDR panel that renders through the curve with the C++ HDR
law (src/mhc.cpp ``EvalGrayscaleHDR_Channel``: slot index = pq / pqPeak * (n-1), linear interpolation of
points[i] * deviation[i] in PQ). The mock shows the same table in the live preview and in the bake; on
hardware the HDR preview differs from the bake (calibrate.py, the grayscale-wb commit), which this test
cannot reproduce - see the report for what that means for the comparison."""

from __future__ import annotations

import datetime
import math
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import calibration_profile as cp  # noqa: E402
from dlc.calibrate import Calibration, PatchSizes  # noqa: E402
from dlc.controller import CalibrationController  # noqa: E402
from dlc.engine.patches import Transfer  # noqa: E402
from dlc.grayscale_wb import base_positions  # noqa: E402
from dlc.measure_loop import SyntheticPanel  # noqa: E402
from dlc.optimize import OptimizeConfig  # noqa: E402
from dlc.runs import create_run  # noqa: E402

import test_gswb_heldout as H  # noqa: E402

_PQ = Transfer.pq(bit_depth=10)


def _cxx_hdr(cg: dict, pq: float, pq_peak: float, ch: str) -> float:
    """src/mhc.cpp EvalGrayscaleHDR_Channel, transcribed (below the peak)."""
    if pq <= 0 or pq_peak <= 0:
        return pq
    pts = cg["points"]
    dev = cg["deviations"][ch]
    n = len(pts)
    x = min(pq / pq_peak, 1.0) * (n - 1)
    i0 = int(math.floor(x))
    i1 = min(i0 + 1, n - 1)
    t = x - i0
    v0, v1 = pts[i0] * dev[i0], pts[i1] * dev[i1]
    return (v0 + (v1 - v0) * t) * pq_peak


class _HdrPanel:
    def __init__(self, ctrl, *, wb=(1.02, 0.99, 1.01)):
        self.ctrl, self.wb, self.calib = ctrl, wb, None
        self.panel = SyntheticPanel(transfer=_PQ, start_temp=1.0, cold_blue_gain=1.0, noise=0.0006, seed=5)

    def __call__(self, patch):
        st = (self.ctrl.state().get("mhc") or {}).get("0:HDR") or {}
        cg = st.get("correction_grayscale") or {}
        on = bool((st.get("gs_preview_active") or st.get("gs_committed")) and cg.get("points"))
        cap = (self.calib._patch_max_cv() if self.calib is not None else None) or _PQ.max_cv
        peak = cap / _PQ.max_cv
        sig = []
        for s, ch, k in zip(patch.signal, "rgb", self.wb):
            out = _cxx_hdr(cg, s, peak, ch) if on else s
            nits = _PQ.cv_to_nits(out * _PQ.max_cv) * k
            sig.append(min(1.0, _PQ.nits_to_cv(nits) / _PQ.max_cv))
        return self.panel(replace(patch, signal=tuple(sig)))


def test_hdr_held_out_greys_sit_at_the_pq_midpoints_and_a_white_balance_fix_applies(tmp_path: Path):
    ctrl = CalibrationController.mock()
    ctrl.set_primaries(0, "HDR", {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctrl.apply_mhc(0, "HDR")
    panel = _HdrPanel(ctrl)
    judge = H._Judge()
    ctx = create_run("HDR", display="synthetic", run_dir=tmp_path / "hdr")
    calib = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                        mode="HDR", controller=ctrl, measure=panel, adjudicator=judge,
                        optimize_config=OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0),
                        patch_sizes=H._SMALL, run_date=datetime.date(2026, 11, 14), bit_depth=10)
    panel.calib = calib
    result = calib.run("grayscale-wb")
    touch = H._touch(calib)
    cap = int(calib._patch_max_cv())
    held = touch["held_out"]["codes"]
    assert len(held) >= 20
    for code in held:                  # HDR: the curve is linear in PQ, so its midpoints are PQ-code midpoints
        u = base_positions(code, max_cv=1023, cap=cap, n=32, kind="hdr")["g"]
        assert abs(u - (math.floor(u) + 0.5)) <= 0.5 * 31 / cap + 1e-9, (code, u)
    ho = H._verify(calib)["gswb_held_out"]
    assert ho["available"] and ho["n_judged"] == len(held)
    assert ho["n_regressed"] == 0, ho["worst"]
    assert judge.request("verify:accept").recommendation == "apply"
    assert result.status == "completed"
