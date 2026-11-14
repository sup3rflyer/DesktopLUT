"""grayscale-wb rollback at the NEW points (review of 3374b91, item 7): abort and pause-then-abort at
``grayscale-wb:light-trend`` and ``grayscale-wb:node-outlier``, a cancel and a meter death during a
held-out before-read. Every non-applied ending must leave the user's pre-run curve (10, 20 or 32 points)
on the mock DesktopLUT. In-process mocks only (the reviewer's rollback_probe.py scenarios, kept)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

import test_gswb_heldout as H  # noqa: E402
from dlc import calibration_profile as cp  # noqa: E402
from dlc.calibrate import (AdjudicationRequired, Calibration, Decision, MappingAdjudicator,  # noqa: E402
                           _rollback_restore, _run_entered_calibration)
from dlc.liveness import MeterDown  # noqa: E402
from dlc.runs import create_run, open_run  # noqa: E402


def _user_curve(ctrl, n):
    pts = [i / (n - 1) for i in range(n)]
    r = [1.0] * n
    r[n // 3], r[2 * n // 3] = 1.012, 0.991
    b = [1.0] * n
    b[n // 2] = 1.007
    ctrl.set_correction_grayscale(0, "SDR", n, pts, {"r": r, "g": [1.0] * n, "b": b}, gamma=2.2)
    ctrl.set_layers(0, "SDR", grayscale=True)
    return json.loads(json.dumps(ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]))


def _cur(ctrl):
    return json.loads(json.dumps(ctrl.state()["mhc"]["0:SDR"]["correction_grayscale"]))


class _Trap(H._CurvePanel):
    """Fires ``action`` on the k-th held-out read of the touch-up session (a grey code that is no node)."""

    def __init__(self, ctrl, action, k=3, **kw):
        super().__init__(ctrl, **kw)
        self.action, self.k, self.n = action, k, 0
        self.nodes = {p[0] for p in H.build_grayscale_wb_set(H.PatchSizes(), H._SDR)}

    def __call__(self, patch):
        code = int(round(max(patch.signal) * H._SDR.max_cv))
        if (self.action is not None and self.calib is not None and self.calib.runlog.phase == "grayscale-wb"
                and patch.rgb[0] == patch.rgb[1] == patch.rgb[2] and code not in self.nodes):
            self.n += 1
            if self.n == self.k:
                self.action(self.calib)
        return super().__call__(patch)


def _make(root: Path, ctrl, panel, choices):
    rd = root / "run"
    ctx = open_run(rd) if (rd / "manifest.json").exists() else create_run("SDR", display="synthetic", run_dir=rd)
    adj = MappingAdjudicator({k: Decision(v) for k, v in {"resolve-target:plan": "approve", **choices}.items()})
    c = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(root / "results")), monitor=0, mode="SDR",
                    controller=ctrl, measure=panel, adjudicator=adj, optimize_config=H._OPT,
                    patch_sizes=H._SMALL, run_date=H._DATE)
    panel.calib = c
    return c


def _cancel(c):
    (c.ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")


def _die(_c):
    raise MeterDown("grayscale-wb", "spotread exited: instrument not found", consecutive_failures=3)


_SCENARIOS = {
    "abort@light-trend": ({"grayscale-wb:light-trend": "abort"}, None, None),
    "abort@node-outlier": ({"grayscale-wb:light-trend": "trend", "grayscale-wb:node-outlier": "abort"}, None, None),
    "pause@light-trend then abort": ({}, None, {"grayscale-wb:light-trend": "abort"}),
    "pause@node-outlier then abort": ({"grayscale-wb:light-trend": "trend"}, None,
                                      {"grayscale-wb:light-trend": "trend", "grayscale-wb:node-outlier": "abort"}),
    "pause@node-outlier then smooth, revert@verify": (
        {"grayscale-wb:light-trend": "trend"}, None,
        {"grayscale-wb:light-trend": "trend", "grayscale-wb:node-outlier": "smooth", "verify:accept": "revert"}),
    "cancel during a held-out read": ({}, _cancel, None),
    "meter death during a held-out read": ({"grayscale-wb:light-trend": "trend",
                                            "grayscale-wb:node-outlier": "smooth"}, _die, None),
}


@pytest.mark.parametrize("n_prior", [10, 20, 32])
@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_every_non_applied_ending_puts_the_users_curve_back(tmp_path: Path, name, n_prior):
    choices, trap, resume = _SCENARIOS[name]
    ctrl = H._controller()
    prior = _user_curve(ctrl, n_prior)
    panel = _Trap(ctrl, trap, feature=H._FEATURE_RUN)
    c = _make(tmp_path, ctrl, panel, choices)
    try:
        status = c.run("grayscale-wb").status
    except AdjudicationRequired:
        assert resume is not None, name
        panel.action = None
        c = _make(tmp_path, ctrl, panel, resume)
        status = c.run("grayscale-wb").status
    assert status in ("aborted", "reverted"), (name, status)
    out = _rollback_restore(ctrl, c.calib, monitor=0, mode="SDR", run_root=c.ctx.root,
                            entered_calibration=_run_entered_calibration(c.calib, flow="grayscale-wb"))
    assert out["status"] in ("rolled_back", "rollback_restored_nothing", "nothing_to_roll_back",
                             "reverted", "rollback_not_needed") or _cur(ctrl) == prior, out
    assert _cur(ctrl) == prior, (name, out.get("status"))
    st = ctrl.state()["mhc"]["0:SDR"]
    assert not st.get("gs_live_active") and not st.get("gs_preview_active")
