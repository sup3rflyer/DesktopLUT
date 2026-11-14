"""repair-base: the balance-only repair of an INSTALLED SDR base 1D LUT (dlc.base_repair + the flow).

The panel is simulated THROUGH scan-out exactly as DesktopLUT applies the profile (mhc_icc.cpp EvalMhcScanout:
sRGB EOTF of the wire, the as-applied matrix row per channel, the sRGB OETF, the base LUT read with linear
interpolation): the measure function reads which base .cube the mock DesktopLUT has installed and drives a native
power-law panel with it. The installed cube carries a per-channel bump that tints greys ~150-156 (the PA32UCXR
2026-09-25 defect shape). No pipe, no meter.
"""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from dlc import base_repair as br
from dlc import calibration_profile as cp
from dlc.calibrate import (AdjudicationRequired, AutoAdjudicator, Calibration, Decision, MappingAdjudicator,
                           PatchSizes, _abort_restore, _rollback_restore, _run_entered_calibration,
                           installed_base_cube, restore_base_repair_prior)
from dlc.colormath import matvec, rgb_to_xyz_matrix
from dlc.controller import CalibrationController
from dlc.liveness import MeterDown
from dlc.measure_loop import Reading
from dlc.mhc import SRGB_PRIMARIES
from dlc.mhc_cube import mhc2_matrix, read_1d_cube, write_1d_cube
from dlc.runs import create_run, open_run

_DATE = datetime.date(2026, 11, 14)
_PRIM = {"rx": 0.696042, "ry": 0.303958, "gx": 0.181786, "gy": 0.750021, "bx": 0.151213, "by": 0.064799}
_WHITE = (0.313393, 0.326755)
_PEAK = 121.0334
_APPLIED = mhc2_matrix(_PRIM, _WHITE, SRGB_PRIMARIES, (0.3127, 0.3290))
_P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"], _PRIM["bx"], _PRIM["by"],
                       _WHITE[0], _WHITE[1], white_Y=_PEAK)
N = 1024


def _bell(j: float, centre: float, half: float) -> float:
    u = (j - centre) / half
    return 0.5 * (1.0 + math.cos(math.pi * u)) if abs(u) < 1.0 else 0.0


def _tinted_cube() -> dict[str, list[float]]:
    """Identity, plus a +1.6 % drive bump whose channels sit entries apart (r/b at their post-matrix index,
    g 1.5 entries later, g taller) — a green-side tint over wire greys ~148-158."""
    out = {ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}
    for j in range(N):
        out["r"][j] *= 1.0 + 0.010 * _bell(j, 152.0, 5.0)
        out["g"][j] *= 1.0 + 0.018 * _bell(j, 153.5, 5.0)
        out["b"][j] *= 1.0 + 0.011 * _bell(j, 152.0, 5.0)
    return out


def _tinted_cube_round_trip() -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.cube"
        write_1d_cube(p, _tinted_cube(), title="test base")
        return read_1d_cube(p)


class ScanoutPanel:
    """measure fn: the installed MHC (matrix + base cube) through scan-out onto a native power-law panel."""

    def __init__(self, ctrl, *, sawtooth: float = 0.0, period: float = 9.0, noise: float = 0.0005):
        import random
        self.ctrl, self.sawtooth, self.period = ctrl, sawtooth, period
        self.noise, self.rng = noise, random.Random(7)      # a colorimeter-class 0.05 % per-component read noise
        self.cache: dict[str, dict] = {}
        self.calib = None
        self.on_phase = None          # (phase, fn) fired once on the first read in that phase
        self.fired = False

    def curves(self) -> dict:
        path, _ = installed_base_cube(self.ctrl, 0, "SDR")
        if path not in self.cache:
            self.cache[path] = read_1d_cube(Path(path))
        return self.cache[path]

    def __call__(self, patch):
        if (self.on_phase and not self.fired and self.calib is not None
                and self.calib.runlog.phase == self.on_phase[0]):
            self.fired = True
            self.on_phase[1](self.calib)
        curves = self.curves()
        lin = [br.srgb_eotf(v) for v in patch.signal]
        drive = []
        for c, ch in enumerate("rgb"):
            m = sum(_APPLIED[c][k] * lin[k] for k in range(3))
            drive.append(br.lut_at(curves[ch], br.srgb_oetf(m)))
        light = [max(d, 0.0) ** 2.2 for d in drive]
        if self.sawtooth:
            mean = sum(drive) / 3 * 1023
            f = 1.0 + self.sawtooth * ((mean / self.period) % 1.0 - 0.5)
            light = [v * f for v in light]
        xyz = tuple(v * (1.0 + self.rng.gauss(0.0, self.noise)) for v in matvec(_P, light))
        tot = sum(xyz)
        return Reading(xyz=xyz, yxy=(xyz[1], xyz[0] / tot, xyz[1] / tot) if tot > 0 else (0, 0, 0), ok=True)


def _install(ctrl, tmp_path: Path, curves=None, name="installed.cube") -> Path:
    path = tmp_path / name
    write_1d_cube(path, curves or _tinted_cube(), title="test base")
    ctrl.set_primaries(0, "SDR", _PRIM)
    ctrl.set_white(0, "SDR", *_WHITE)
    ctrl.set_base_lut(0, "SDR", str(path), 0.0)
    ctrl.apply_mhc(0, "SDR")
    return path


def _source_run(tmp_path: Path, cube: Path) -> Path:
    root = tmp_path / "source_run"
    root.mkdir(exist_ok=True)
    (root / "dlc_state.json").write_text(json.dumps({"mhc_params": {
        "mode": "SDR", "primaries": _PRIM, "measured_primaries": _PRIM,
        "measured_white": {"x": _WHITE[0], "y": _WHITE[1]}, "target_luminance": _PEAK,
        "base_lut": {"cube_path": str(cube)}}}), encoding="utf-8")
    return root


def _make(tmp_path: Path, name: str, ctrl, *, panel=None, adjudicator=None, source=None,
          decision_overrides=None) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run("SDR", display="synthetic", run_dir=run_dir)
    panel = panel or ScanoutPanel(ctrl)
    calib = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                        mode="SDR", controller=ctrl, measure=panel, adjudicator=adjudicator or AutoAdjudicator(),
                        patch_sizes=PatchSizes(), run_date=_DATE, source_run=source,
                        decision_overrides=decision_overrides)
    panel.calib = calib
    return calib


def _setup(tmp_path: Path, **kw):
    ctrl = CalibrationController.mock()
    cube = _install(ctrl, tmp_path)
    src = _source_run(tmp_path, cube)
    return ctrl, cube, src


def _balance(curves, c):
    d = br.scanout_grey_drive(curves, _APPLIED, c / 1023)
    lr, lg, lb = (max(v, 0.0) ** 2.2 for v in d)
    sh = matvec([[1, 0, 0], [0, 1, 0], [0, 0, 1]], [lr, lg, lb])
    return sh[0] / sh[1], sh[2] / sh[1], sum(matvec(_P, [lr, lg, lb])[1:2])


# ---------------------------------------------------------------------------
# scan-out placement (mhc_icc.cpp EvalMhcScanout)
# ---------------------------------------------------------------------------

def test_grey_lut_index_is_where_scanout_reads_each_channel():
    for c in (60, 148, 153, 160, 600):
        s = c / 1023
        lin = br.srgb_eotf(s)
        for k in range(3):
            full = br.srgb_oetf(sum(_APPLIED[k][j] * lin for j in range(3)))   # the whole matrix row
            assert br.grey_lut_index(_APPLIED, k, s) == pytest.approx(full, abs=1e-12)
    # PA32UCXR rowsums: wire 153 reads r and b ~1.6 entries below g (the power-law shortcut said 1.3)
    idx = [br.grey_lut_index(_APPLIED, k, 153 / 1023) * 1023 for k in range(3)]
    assert idx[1] - idx[0] == pytest.approx(1.62, abs=0.05) and abs(idx[0] - idx[2]) < 0.01


def test_identity_profile_passes_the_wire_through_scanout():
    ident = {ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}
    eye = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    for c in range(0, 1024, 7):
        assert br.scanout_grey_drive(ident, eye, c / 1023) == pytest.approx([c / 1023] * 3, abs=2e-6)


def test_slope_safe_never_leaves_a_flat_run():
    old = [j / 99 for j in range(100)]
    new = list(old)
    for j in range(40, 50):
        new[j] = old[40] - 0.002                      # a correction that would dip below its neighbours
    br._slope_safe(new, old, 40, 49)
    steps = [new[j + 1] - new[j] for j in range(38, 52)]
    assert min(steps) > 0.0, steps


# ---------------------------------------------------------------------------
# the core on the simulated installed defect
# ---------------------------------------------------------------------------

def _audit(curves, codes, panel_ctrl=None, sawtooth=0.0):
    class _C:
        def __init__(self, c):
            self.c = c

        def state(self):
            return {"mhc": {"0:SDR": {"source_file": "x"}}}
    p = ScanoutPanel(_C(curves), sawtooth=sawtooth, noise=0.0)
    p.curves = lambda: curves

    class _Patch:
        def __init__(self, s):
            self.signal = (s, s, s)
    return {c: p(_Patch(c / 1023)).xyz for c in codes}


@pytest.mark.parametrize("sawtooth", [0.0, 0.03])
def test_core_repairs_the_balance_keeps_the_light_and_touches_nothing_else(sawtooth):
    tinted = _tinted_cube()
    runs = br.balance_census(tinted, _APPLIED)
    assert len(runs) == 1 and runs[0]["lo"] <= 152 <= runs[0]["hi"], runs
    windows = br.audit_codes(runs)
    plan = br.split_fit_held(windows)
    reads = _audit(tinted, plan["codes"], sawtooth=sawtooth)
    fixed, rep = br.repair_balance(tinted, reads, plan, applied=_APPLIED, primaries=_PRIM,
                                   native_white_xy=_WHITE, peak_luminance=_PEAK, luminance_sigma=1e-3,
                                   suspect=[(r["lo"], r["hi"]) for r in runs])
    assert len(rep["spans"]) == 1
    lo_e = min(v["entries"][0] for v in rep["spans"][0]["changed"].values())
    hi_e = max(v["entries"][1] for v in rep["spans"][0]["changed"].values())
    for ch in "rgb":                                   # bit-identical outside the rewritten entries
        assert all(fixed[ch][j] == tinted[ch][j] for j in range(N) if j < lo_e or j > hi_e)
    ref = _balance({ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}, 140)
    for c in range(130, 175):                          # every WIRE grey, fit and held-out alike
        rg0, bg0, y0 = _balance(tinted, c)
        rg, bg, y = _balance(fixed, c)
        assert abs(rg / ref[0] - 1) < 0.002 and abs(bg / ref[1] - 1) < 0.002, (c, rg / ref[0], bg / ref[1])
        assert abs(y / y0 - 1) < 0.003, (c, y / y0)     # light kept (balance-only)
    before = max(abs(_balance(tinted, c)[0] / ref[0] - 1) for c in range(140, 165))
    assert before > 0.004                               # the defect was there


def test_core_with_the_power_law_shortcut_would_leave_a_tint():
    """Placing the correction at rowsum^(1/2.2) * s (the DLC refine's shortcut) instead of scan-out's exact
    index misplaces the channels by ~0.2 entry: measurably worse on a steep defect."""
    tinted = _tinted_cube()
    runs = br.balance_census(tinted, _APPLIED)
    plan = br.split_fit_held(br.audit_codes(runs))
    reads = _audit(tinted, plan["codes"])
    kw = dict(primaries=_PRIM, native_white_xy=_WHITE, peak_luminance=_PEAK, luminance_sigma=1e-3,
              suspect=[(r["lo"], r["hi"]) for r in runs])
    exact, _ = br.repair_balance(tinted, reads, plan, applied=_APPLIED, **kw)
    orig = br.grey_lut_index
    try:
        br.grey_lut_index = lambda A, k, s: (sum(A[k]) ** (1 / 2.2)) * s
        approx, _ = br.repair_balance(tinted, reads, plan, applied=_APPLIED, **kw)
    finally:
        br.grey_lut_index = orig
    ref = _balance({ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}, 140)
    err = lambda cv: max(abs(_balance(cv, c)[0] / ref[0] - 1) for c in range(140, 165))  # noqa: E731
    assert err(exact) < err(approx)


# ---------------------------------------------------------------------------
# the flow, end to end on the mock
# ---------------------------------------------------------------------------

def test_flow_repairs_installs_verifies_and_keeps(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    calib = _make(tmp_path, "ok", ctrl, source=src)
    result = calib.run("repair-base")
    assert result.status == "completed", result.digest
    now, _ = installed_base_cube(ctrl, 0, "SDR")
    assert now != str(cube) and now.endswith("mhc_base_sdr.repaired.cube")
    assert calib.calib["repair_base_installed"]["kept"] is True
    seed = calib.calib["stages"]["repair-seed"]["digest"]
    assert seed["planned_reads"] == seed["audit_greys"] + seed["verify_greys"] and seed["planned_minutes"] > 0
    verdict = calib.calib["stages"]["repair-verify"]["digest"]
    assert verdict["out"] == [] and verdict["inside"] > 0 and verdict["beside"] > 0
    fixed = read_1d_cube(Path(now))
    ref = _balance({ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}, 140)
    worst = lambda cv: max(abs(_balance(cv, c)[0] / ref[0] - 1) for c in range(140, 165))  # noqa: E731
    # through 0.05 % read noise on single reads: within ~0.3 % (y ~0.0006) of neutral, from ~1.3 %
    assert worst(fixed) < 0.004 and worst(fixed) < worst(_tinted_cube()) / 3
    # the previous cube is byte-identical on disk (the rollback point)
    assert read_1d_cube(cube) == _tinted_cube_round_trip()


def test_flow_with_nothing_tinted_changes_nothing(tmp_path: Path):
    ctrl = CalibrationController.mock()
    clean = {ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}
    cube = _install(ctrl, tmp_path, clean)
    calib = _make(tmp_path, "clean", ctrl, source=_source_run(tmp_path, cube))
    result = calib.run("repair-base")
    assert result.status == "completed" and "no tinted greys" in result.digest["message"]
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)
    assert "measure:repair-audit" not in calib.calib["stages"]


def test_seed_refuses_when_the_installed_cube_is_not_the_source_runs(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    other = _install(ctrl, tmp_path, None, name="other.cube")
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(other)
    result = _make(tmp_path, "wrong", ctrl, source=src).run("repair-base")
    assert result.status == "aborted" and result.digest["aborted_at"] == "repair-seed"
    assert "not the source run's" in result.digest["message"]


def test_seed_refuses_a_3d_lut_that_moves_greys(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    n = 3
    lines = ["LUT_3D_SIZE 3"]
    for b in range(n):
        for g in range(n):
            for r in range(n):
                lines.append(f"{min(1.0, r / 2 * 1.01):.6f} {g / 2:.6f} {b / 2:.6f}")
    p = tmp_path / "warm.cube"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctrl.set_3dlut(0, "SDR", str(p))
    result = _make(tmp_path, "cube", ctrl, source=src).run("repair-base")
    assert result.status == "aborted" and "not identity on the grey axis" in result.digest["message"]


# ---------------------------------------------------------------------------
# every ending that does not KEEP the candidate puts the previous cube back (confirmed by read-back)
# ---------------------------------------------------------------------------

def _guard(ctrl, calib) -> dict:
    return _rollback_restore(ctrl, calib.calib, monitor=0, mode="SDR", run_root=calib.ctx.root,
                             entered_calibration=_run_entered_calibration(calib.calib, flow="repair-base"))


def _cancel(calib) -> None:
    (calib.ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")


def test_cancel_during_the_audit_needs_no_restore(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    panel = ScanoutPanel(ctrl)
    panel.on_phase = ("measure:repair-audit", _cancel)
    calib = _make(tmp_path, "c_audit", ctrl, panel=panel, source=src)
    assert calib.run("repair-base").status == "aborted"
    out = _guard(ctrl, calib)
    assert "repair_base_restore" not in out and installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)


@pytest.mark.parametrize("ending", ["cancel", "meter", "crash"])
def test_an_ending_after_the_install_restores_the_previous_cube(tmp_path: Path, ending):
    ctrl, cube, src = _setup(tmp_path)
    panel = ScanoutPanel(ctrl)

    def die(_c):
        raise MeterDown("measure:repair-verify", "spotread exited: instrument not found", consecutive_failures=3)

    def crash(_c):
        raise RuntimeError("simulated crash in the verify")

    panel.on_phase = ("measure:repair-verify", {"cancel": _cancel, "meter": die, "crash": crash}[ending])
    calib = _make(tmp_path, ending, ctrl, panel=panel, source=src,
                  adjudicator=MappingAdjudicator({"repair-base:plan": Decision("proceed"),
                                                  "repair-base:apply": Decision("apply")})
                  if ending == "meter" else None)
    if ending == "crash":
        with pytest.raises(RuntimeError):
            calib.run("repair-base")
        assert installed_base_cube(ctrl, 0, "SDR")[0] != str(cube)
        state = json.loads((calib.ctx.root / "dlc_state.json").read_text(encoding="utf-8"))
        code, payload = _abort_restore(ctrl, state["calib"], monitor=0, mode="SDR", run_root=calib.ctx.root,
                                       flow="repair-base")
        assert payload["status"] == "reverted", payload
    else:
        try:
            status = calib.run("repair-base").status
        except AdjudicationRequired:
            status = "paused"
        assert status in ("aborted", "paused")
        assert installed_base_cube(ctrl, 0, "SDR")[0] != str(cube)
        out = _guard(ctrl, calib) if status == "aborted" else \
            dict(zip(("code", "payload"), _abort_restore(ctrl, calib.calib, monitor=0, mode="SDR",
                                                           run_root=calib.ctx.root)))["payload"]
        assert out["status"] in ("rolled_back", "reverted"), out
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)
    assert ending == "crash" or calib.calib["repair_base_restore"]["confirmed"] is True


def test_revert_at_the_keep_seam_restores_and_a_resume_refuses(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    calib = _make(tmp_path, "revert", ctrl, source=src,
                  decision_overrides={"repair-base:keep": Decision("revert")})
    result = calib.run("repair-base")
    assert result.status == "reverted", result.digest
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)
    calib._save()
    again = _make(tmp_path, "revert", ctrl, source=src).run("repair-base")
    assert again.status == "aborted" and again.digest["aborted_at"] == "repair-install:rolled-back"
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)


def test_a_gone_pipe_says_so_and_a_later_abort_finishes_it(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    panel = ScanoutPanel(ctrl)
    panel.on_phase = ("measure:repair-verify", _cancel)
    calib = _make(tmp_path, "dead", ctrl, panel=panel, source=src)
    calib.run("repair-base")

    class _Dead:
        def __getattr__(self, name):
            def fail(*_a, **_k):
                raise OSError("pipe not found")
            return fail

    out = _guard(_Dead(), calib)
    assert out["status"] == "rollback_failed" and out["display_may_carry_repair"] is True
    assert "--abort" in out["hint"] and str(calib.ctx.root) in out["hint"]
    calib._save()
    state = json.loads((calib.ctx.root / "dlc_state.json").read_text(encoding="utf-8"))
    code, payload = _abort_restore(ctrl, state["calib"], monitor=0, mode="SDR", run_root=calib.ctx.root)
    assert payload["status"] == "reverted" and installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)


def test_a_restore_the_read_back_does_not_show_is_unconfirmed(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    panel = ScanoutPanel(ctrl)
    panel.on_phase = ("measure:repair-verify", _cancel)
    calib = _make(tmp_path, "deaf", ctrl, panel=panel, source=src)
    calib.run("repair-base")

    class _Deaf:
        def __getattr__(self, name):
            if name == "set_base_lut":
                return lambda *a, **k: {}            # "ok", stores nothing
            return getattr(ctrl, name)

    out = _guard(_Deaf(), calib)
    assert out["status"] == "rollback_unconfirmed" and out["display_may_carry_repair"] is True


def test_restoring_twice_is_harmless_and_abort_never_takes_a_kept_repair_back(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    panel = ScanoutPanel(ctrl)
    panel.on_phase = ("measure:repair-verify", _cancel)
    calib = _make(tmp_path, "twice", ctrl, panel=panel, source=src)
    calib.run("repair-base")
    assert _guard(ctrl, calib)["status"] == "rolled_back"
    again = restore_base_repair_prior(ctrl, calib.calib, monitor=0, mode="SDR", why="again")
    assert again["outcome"] == "restored" and again["actions"] == []
    # a KEPT repair
    ctrl2, cube2, src2 = _setup(tmp_path / "k")
    kept = _make(tmp_path / "k", "kept", ctrl2, source=src2)
    assert kept.run("repair-base").status == "completed"
    now = installed_base_cube(ctrl2, 0, "SDR")[0]
    code, payload = _abort_restore(ctrl2, kept.calib, monitor=0, mode="SDR", run_root=kept.ctx.root)
    assert payload["repair_base_restore"]["outcome"] == "not_needed" and "KEPT" in payload["note"]
    assert installed_base_cube(ctrl2, 0, "SDR")[0] == now
