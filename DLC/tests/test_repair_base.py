"""repair-base: the balance-only repair of an INSTALLED SDR base 1D LUT (dlc.base_repair + the flow).

The panel is simulated THROUGH scan-out exactly as DesktopLUT applies the profile (mhc_icc.cpp EvalMhcScanout:
sRGB EOTF of the wire, the as-applied matrix row per channel, the sRGB OETF, the base LUT read with linear
interpolation), as a native panel with FLAT chromaticity and a common light sawtooth in the wire code (the
PA32UCXR's measured behaviour, 2026-11-14). The installed cube is the REAL PA32UCXR SDR base around the defect
(tests/data/pa32_sdr_base_120_190.json: entries 120-190 of mhc_base_sdr.refine1.cube), extended smoothly outside.
No pipe, no meter.
"""

from __future__ import annotations

import datetime
import json
import math
import random
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from dlc import base_repair as br
from dlc import calibration_profile as cp
from dlc import stack_registry
from dlc.calibrate import (AdjudicationRequired, AutoAdjudicator, Calibration, Decision, MappingAdjudicator,
                           PatchSizes, _abort_restore, _parse_repair_span, _rollback_restore,
                           _run_entered_calibration, installed_base_cube, restore_base_repair_prior)
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
SPAN = (133, 171)
_FIX = json.loads((Path(__file__).parent / "data" / "pa32_sdr_base_120_190.json").read_text(encoding="utf-8"))


def _real_cube() -> dict[str, list[float]]:
    """The installed PA32UCXR SDR base over entries 120-190 (its bump at ~149-157), extended outside with each
    channel's out/in ratio held at the slice's end and blended to 1 at full drive."""
    lo = _FIX["first_entry"]
    hi = lo + len(_FIX["r"]) - 1
    out = {}
    for ch in "rgb":
        sl = _FIX[ch]
        r_lo, r_hi = sl[0] / (lo / (N - 1)), sl[-1] / (hi / (N - 1))
        cur = []
        for j in range(N):
            x = j / (N - 1)
            if j < lo:
                cur.append(x * r_lo)
            elif j <= hi:
                cur.append(sl[j - lo])
            else:
                t = (j - hi) / (N - 1 - hi)
                cur.append(min(1.0, x * (r_hi + (1.0 - r_hi) * t)))
        out[ch] = cur
    return out


def _saw(code: int) -> float:
    return 1.0 + 0.03 * (((code * 7) % 12) / 12.0 - 0.5)


def _cube_round_trip(curves) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.cube"
        write_1d_cube(p, curves, title="t")
        return read_1d_cube(p)


class ScanoutPanel:
    """measure fn: the installed MHC (matrix + base cube) through scan-out onto a native panel with flat
    chromaticity, light = drive^gamma per channel times a common sawtooth in the wire code, read noise."""

    def __init__(self, ctrl, *, gamma: float = 2.2, noise: float = 0.0005, seed: int = 1):
        self.ctrl, self.gamma, self.noise, self.rng = ctrl, gamma, noise, random.Random(seed)
        self.cache: dict[str, dict] = {}
        self.calib = None
        self.on_phase = None
        self.fired = False

    def curves(self) -> dict:
        path, _ = installed_base_cube(self.ctrl, 0, "SDR")
        if path not in self.cache:
            self.cache[path] = read_1d_cube(Path(path))
        return self.cache[path]

    def xyz(self, curves, signal) -> tuple:
        lin = [br.srgb_eotf(v) for v in signal]
        code = int(round(sum(signal) / 3 * 1023))
        light = []
        for c, ch in enumerate("rgb"):
            m = sum(_APPLIED[c][k] * lin[k] for k in range(3))
            light.append(max(br.lut_at(curves[ch], br.srgb_oetf(m)), 0.0) ** self.gamma * _saw(code))
        return tuple(v * (1.0 + self.rng.gauss(0.0, self.noise)) for v in matvec(_P, light))

    def __call__(self, patch):
        if (self.on_phase and not self.fired and self.calib is not None
                and self.calib.runlog.phase == self.on_phase[0]):
            self.fired = True
            self.on_phase[1](self.calib)
        xyz = self.xyz(self.curves(), patch.signal)
        tot = sum(xyz)
        return Reading(xyz=xyz, yxy=(xyz[1], xyz[0] / tot, xyz[1] / tot) if tot > 0 else (0, 0, 0), ok=True)


def _install(ctrl, tmp_path: Path, curves=None, name="installed.cube") -> Path:
    path = tmp_path / name
    write_1d_cube(path, curves or _real_cube(), title="test base")
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


def _make(tmp_path: Path, name: str, ctrl, *, panel=None, adjudicator=None, source=None, span=SPAN,
          decision_overrides=None, mode="SDR") -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run(mode, display="synthetic", run_dir=run_dir)
    panel = panel or ScanoutPanel(ctrl)
    if decision_overrides is None and adjudicator is None:
        # The MECHANICS tests (install, verify, rollback, lineage) go through both seams. On the fixture cube the
        # sized repair is "better, not flat" by the model-free test (its bridge target sits up to ~0.6 % above the
        # neighbours' line even when executed perfectly), so the seams recommend abort / revert; these tests decide
        # apply / keep explicitly. The recommendations themselves are tested below (sizing / review section).
        decision_overrides = {"repair-base:apply": Decision("apply"), "repair-base:keep": Decision("keep")}
    calib = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                        mode=mode, controller=ctrl, measure=panel, adjudicator=adjudicator or AutoAdjudicator(),
                        patch_sizes=PatchSizes(), run_date=_DATE, source_run=source,
                        repair_span=(f"{span[0]}:{span[1]}" if span else None),
                        decision_overrides=decision_overrides)
    panel.calib = calib
    return calib


def _setup(tmp_path: Path):
    ctrl = CalibrationController.mock()
    cube = _install(ctrl, tmp_path)
    return ctrl, cube, _source_run(tmp_path, cube)


def _grey(curves, c, gamma=2.2):
    """(ln r/g, ln b/g of the native linear light, grey Y) of wire grey c through scan-out, noise-free."""
    d = br.scanout_grey_drive(curves, _APPLIED, c / 1023)
    lr, lg, lb = (max(v, 0.0) ** gamma for v in d)
    return math.log(lr / lg), math.log(lb / lg), matvec(_P, [lr, lg, lb])[1]


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
            assert br.wire_for_index(_APPLIED, k, full) == pytest.approx(s, abs=1e-9)
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
        new[j] = old[40] - 0.002
    br._slope_safe(new, old, 40, 49)
    assert min(new[j + 1] - new[j] for j in range(38, 52)) > 0.0


# ---------------------------------------------------------------------------
# spans: required, validated against the cube's own census
# ---------------------------------------------------------------------------

def test_parse_repair_span():
    assert _parse_repair_span("133:171") == [133, 171]
    assert _parse_repair_span([133, 171]) == [133, 171]
    for bad in ("171:133", "133", "a:b", "-1:5", "5:5"):
        with pytest.raises(ValueError):
            _parse_repair_span(bad)


def test_validate_span_on_the_real_cube():
    cube = _real_cube()
    ok = br.validate_span(cube, _APPLIED, SPAN)
    assert ok["ok"] and [(r["lo"], r["hi"]) for r in ok["tints"]] == [(149, 155)]
    cut = br.validate_span(cube, _APPLIED, (152, 175))          # an edge inside the bump: refused
    assert not cut["ok"] and "an edge would cut it" in cut["problem"]
    none = br.validate_span(cube, _APPLIED, (300, 400))
    assert not none["ok"] and "no tint" in none["problem"]


# ---------------------------------------------------------------------------
# the core on the REAL cube: shaped by the cube, confirmed by the reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gamma", [2.0, 2.2, 2.6])
def test_core_on_the_real_cube_removes_the_tint_and_nothing_else(gamma):
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])
    panel = ScanoutPanel(None, gamma=gamma)
    before = {c: panel.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
    fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                   peak_luminance=_PEAK, span=SPAN, luminance_sigma=7e-4)
    assert rep["confirmation"]["confirmed"], rep["confirmation"]
    region = rep["spans"][0]["codes"]
    assert region == [148, 156]
    for c in range(144, 163):
        rg0, bg0, y0 = _grey(cube, c, gamma)
        rg, bg, y = _grey(fixed, c, gamma)
        assert abs(y / y0 - 1) < 0.002, (c, y / y0)                          # light kept
        if c < 148 or c > 157:                                               # neighbours: untouched
            assert (rg, bg) == (rg0, bg0), c
    # the reference a grey inside should sit on: the straight line between the untouched greys 147 and 157
    a, b = _grey(cube, 147, gamma), _grey(cube, 157, gamma)

    def off(cv, c):
        f = (c - 147) / 10.0
        g = _grey(cv, c, gamma)
        return max(abs(g[0] - (a[0] + (b[0] - a[0]) * f)), abs(g[1] - (a[1] + (b[1] - a[1]) * f)))

    worst_before = max(off(cube, c) for c in range(148, 157))
    worst_after = max(off(fixed, c) for c in range(148, 157))
    # ~4-5 % -> about 0.45 % at gamma 2.2 (y ~0.001; scales with the panel's gamma), inside the verify's tolerance
    assert worst_before > 0.03 and worst_after < 0.0025 * gamma, (worst_before, worst_after)
    # every entry outside the region's per-channel range is bit-identical
    for ch, d in rep["spans"][0]["changed"].items():
        lo_e, hi_e = d["entries"]
        assert all(fixed[ch][j] == cube[ch][j] for j in range(N) if j < lo_e or j > hi_e)


def test_core_refuses_when_the_reads_do_not_show_the_cubes_tint():
    """A panel whose reads carry no tint where the cube predicts one (the cube is not what the meter sees):
    the repair is not confirmed."""
    cube = _real_cube()
    clean = {ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}
    plan = br.split_fit_held([SPAN])
    panel = ScanoutPanel(None)
    reads = {c: panel.xyz(clean, (c / 1023,) * 3) for c in plan["codes"]}
    _fixed, rep = br.repair_balance(cube, reads, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                    peak_luminance=_PEAK, span=SPAN, luminance_sigma=7e-4)
    assert rep["confirmation"]["confirmed"] is False


# ---------------------------------------------------------------------------
# the flow, end to end on the mock with the real cube
# ---------------------------------------------------------------------------

def test_flow_repairs_verifies_keeps_and_tells_the_registry(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    calib = _make(tmp_path, "ok", ctrl, source=src)
    result = calib.run("repair-base")
    assert result.status == "completed", result.digest
    now, _ = installed_base_cube(ctrl, 0, "SDR")
    assert now.endswith("mhc_base_sdr.repaired.cube")
    seed = calib.calib["stages"]["repair-seed"]["digest"]
    assert seed["windows"] == [list(SPAN)] or seed["windows"] == [SPAN]
    assert seed["planned_reads"] == 39 + 19 and seed["census_proposals"]
    verdict = calib.calib["stages"]["repair-verify"]["digest"]
    assert verdict["inside"] > 0 and verdict["beside"] > 0
    # the repair did what the audit predicted (no change / model / light miss); what can be out is the MODEL-FREE
    # flatness: the bridge target of the cube-shaped removal is not the neighbours' line (see the review section)
    assert all("model-free" in m for r in verdict["out"] for m in r["missed"]), verdict["out"]
    for r in verdict["held_out"]:                       # neighbours' chromaticity unchanged within repeatability
        if r.get("where") == "beside":
            assert abs(r["rg_pct"]) < verdict["tolerance_pct"]["r/g"]
    assert read_1d_cube(cube) == _cube_round_trip(_real_cube())          # the rollback point is untouched
    # lineage: this run can seed a later repair, and the registry knows the base changed by a DLC repair
    st = json.loads((calib.ctx.root / "dlc_state.json").read_text(encoding="utf-8"))
    assert st["mhc_params"]["base_lut"]["cube_path"] == now
    assert st["mhc_params"]["base_lut"]["repaired_from"]["cube_path"] == str(cube)
    reg = stack_registry.StackRegistry.load(stack_registry.registry_path(calib.profile, calib.ctx.root))
    rec = reg.get(calib.display.name, "SDR")
    assert rec is not None and rec.mhc["base_lut"] == now and rec.mhc["base_lut_repair"]["run_id"] == calib.ctx.root.name
    ev = stack_registry.check_against_pipe(rec, ctrl.state(), 0, "SDR")
    assert ev["matches"] is not False


def test_refine_mhc_refuses_to_seed_from_a_repair_run(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    rep = _make(tmp_path, "rep", ctrl, source=src)
    assert rep.run("repair-base").status == "completed"
    run_dir = tmp_path / "refine"
    calib = Calibration(ctx=create_run("SDR", display="synthetic", run_dir=run_dir),
                        profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0, mode="SDR",
                        controller=ctrl, measure=ScanoutPanel(ctrl), adjudicator=AutoAdjudicator(),
                        patch_sizes=PatchSizes(), run_date=_DATE, source_run=rep.ctx.root)
    result = calib.run("refine-mhc")
    assert result.status == "aborted" and result.digest["aborted_at"] == "seed-from-run"
    assert "repair-base run" in result.digest["message"] and str(src) in result.digest["message"]


@pytest.mark.parametrize("span,why", [(None, "needs --repair-span"), ((152, 175), "an edge would cut it"),
                                      ((300, 400), "no tint")])
def test_seed_refuses_a_missing_or_bad_span(tmp_path: Path, span, why):
    ctrl, cube, src = _setup(tmp_path)
    result = _make(tmp_path, "span", ctrl, source=src, span=span).run("repair-base")
    assert result.status == "aborted" and result.digest["aborted_at"] == "repair-seed"
    assert why in result.digest["message"]
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)


def test_seed_refuses_when_the_installed_cube_is_not_the_source_runs(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    _install(ctrl, tmp_path, None, name="other.cube")
    result = _make(tmp_path, "wrong", ctrl, source=src).run("repair-base")
    assert result.status == "aborted" and "not the source run's" in result.digest["message"]


def test_seed_refuses_a_3d_lut_that_moves_greys(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    lines = ["LUT_3D_SIZE 3"]
    for b in range(3):
        for g in range(3):
            for r in range(3):
                lines.append(f"{min(1.0, r / 2 * 1.01):.6f} {g / 2:.6f} {b / 2:.6f}")
    p = tmp_path / "warm.cube"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctrl.set_3dlut(0, "SDR", str(p))
    result = _make(tmp_path, "cube", ctrl, source=src).run("repair-base")
    assert result.status == "aborted" and "not identity on the grey axis" in result.digest["message"]


def test_seed_refuses_sdr_gamma_24(tmp_path: Path, monkeypatch):
    ctrl, cube, src = _setup(tmp_path)
    ini = tmp_path / "DesktopLUT.ini"
    ini.write_text("[Monitor0]\nSDR_MHCCorrGS24=1\n", encoding="utf-8")
    calib = _make(tmp_path, "g24", ctrl, source=src)
    monkeypatch.setattr(calib, "_resolve_desktoplut_ini", lambda: ini)
    result = calib.run("repair-base")
    assert result.status == "aborted" and "GAMMA 2.4" in result.digest["message"]


def test_seed_refuses_hdr(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    result = _make(tmp_path, "hdr", ctrl, source=src, mode="HDR").run("repair-base")
    assert result.status == "aborted" and "SDR base 1D LUT only" in result.digest["message"]


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
        assert installed_base_cube(ctrl, 0, "SDR")[0] != str(cube)
        out = _guard(ctrl, calib) if status == "aborted" else \
            _abort_restore(ctrl, calib.calib, monitor=0, mode="SDR", run_root=calib.ctx.root)[1]
        assert out["status"] in ("rolled_back", "reverted"), out
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)


def test_revert_at_the_keep_seam_restores_and_a_resume_refuses(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    calib = _make(tmp_path, "revert", ctrl, source=src, decision_overrides={"repair-base:apply": Decision("apply"),
                                                                           "repair-base:keep": Decision("revert")})
    result = calib.run("repair-base")
    assert result.status == "reverted", result.digest
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube)
    calib._save()
    again = _make(tmp_path, "revert", ctrl, source=src).run("repair-base")
    assert again.status == "aborted" and again.digest["aborted_at"] == "repair-install:rolled-back"


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
                return lambda *a, **k: {}
            return getattr(ctrl, name)

    out = _guard(_Deaf(), calib)
    assert out["status"] == "rollback_unconfirmed" and out["display_may_carry_repair"] is True


def test_restore_refuses_when_someone_else_changed_the_base_in_between(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    panel = ScanoutPanel(ctrl)
    panel.on_phase = ("measure:repair-verify", _cancel)
    calib = _make(tmp_path, "theirs", ctrl, panel=panel, source=src)
    calib.run("repair-base")
    theirs = _install(ctrl, tmp_path, None, name="theirs.cube")            # e.g. the GUI loaded another cube
    out = _guard(ctrl, calib)
    assert out["repair_base_restore"]["outcome"] == "refused"
    assert installed_base_cube(ctrl, 0, "SDR")[0] == str(theirs)          # their change kept


def test_restoring_twice_is_harmless_and_abort_never_takes_a_kept_repair_back(tmp_path: Path):
    ctrl, cube, src = _setup(tmp_path)
    panel = ScanoutPanel(ctrl)
    panel.on_phase = ("measure:repair-verify", _cancel)
    calib = _make(tmp_path, "twice", ctrl, panel=panel, source=src)
    calib.run("repair-base")
    assert _guard(ctrl, calib)["status"] == "rolled_back"
    again = restore_base_repair_prior(ctrl, calib.calib, monitor=0, mode="SDR", why="again")
    assert again["outcome"] == "restored" and again["actions"] == []
    ctrl2, cube2, src2 = _setup(tmp_path / "k")
    kept = _make(tmp_path / "k", "kept", ctrl2, source=src2)
    assert kept.run("repair-base").status == "completed"
    now = installed_base_cube(ctrl2, 0, "SDR")[0]
    code, payload = _abort_restore(ctrl2, kept.calib, monitor=0, mode="SDR", run_root=kept.ctx.root)
    assert payload["repair_base_restore"]["outcome"] == "not_needed" and "KEPT" in payload["note"]
    assert installed_base_cube(ctrl2, 0, "SDR")[0] == now


# ---------------------------------------------------------------------------
# SIZING (2026-11-14 hardware run: the unsized removal was reverted). Simulated displays whose balance response
# to the cube is (a) BLURRED along the wire code (what the PA32UCXR showed: sigma ~2 codes), (b) a pure GAIN of 0.77,
# (c) the panel's OWN opposite structure cancelling 23 % of the tint (indistinguishable from (b) before the
# repair), and whose light has TEETH in the green drive code (common to the channels: light moves when a drive
# crosses a tooth edge, balance does not). Domains: the response is fitted in ln balance (r/g, b/g of native
# shares) against the wire code; teeth in the drive code.
# ---------------------------------------------------------------------------

class ResponsePanel(ScanoutPanel):
    """The real cube through scan-out, then the display's response: each channel's drive balance against green
    (ln d_k/d_g) blurred along the wire code (Gaussian ``sigma``), times ``gain``, plus ``own(code)`` (an extra
    balance structure of the panel itself, ln units, (r/g, b/g)); light = d^gamma x a sawtooth in the GREEN drive
    code (``teeth``) common to the channels; read noise."""

    def __init__(self, ctrl, *, sigma=0.0, gain=1.0, own=None, teeth=0.03, noise=2e-4, seed=3, ref=None,
                 change_scale=1.0, change_shift=0.0):
        super().__init__(ctrl, noise=noise, seed=seed)
        self.sigma, self.gain, self.own, self.teeth = sigma, gain, own, teeth
        # error injection: for any cube other than ``ref`` the display shows ref's balance plus change_scale x the
        # change, displaced by change_shift codes (an effect the audit could not have predicted)
        self.ref, self.change_scale, self.change_shift = ref, change_scale, change_shift

    def _lr(self, curves, c):
        if self.sigma > 0:
            xs = [0.25 * i for i in range(-int(16 * self.sigma), int(16 * self.sigma) + 1)]
            ws = [math.exp(-0.5 * (x / self.sigma) ** 2) for x in xs]
            acc = [0.0, 0.0]
            for x, w in zip(xs, ws):
                dd = br.scanout_grey_drive(curves, _APPLIED, min(max(c + x, 0.0), 1023.0) / 1023)
                acc[0] += w * math.log(dd[0] / dd[1])
                acc[1] += w * math.log(dd[2] / dd[1])
            return [acc[0] / sum(ws), acc[1] / sum(ws)]
        d = br.scanout_grey_drive(curves, _APPLIED, min(max(c, 0.0), 1023.0) / 1023)
        return [math.log(d[0] / d[1]), math.log(d[2] / d[1])]

    def xyz(self, curves, signal):
        s = sum(signal) / 3
        c = s * 1023
        d = br.scanout_grey_drive(curves, _APPLIED, s)
        lr = self._lr(curves, c)
        if self.ref is not None and curves is not self.ref:
            base = self._lr(self.ref, c)
            new, old = self._lr(curves, c - self.change_shift), self._lr(self.ref, c - self.change_shift)
            lr = [base[k] + self.change_scale * (new[k] - old[k]) for k in range(2)]
        extra = self.own(c) if self.own else (0.0, 0.0)
        g = d[1]
        ratio = [math.exp(self.gain * lr[0] + extra[0] / self.gamma), math.exp(self.gain * lr[1] + extra[1] / self.gamma)]
        tooth = 1.0 + self.teeth * (((g * 1023) / 7.0) % 1.0 - 0.5)
        light = [(g * ratio[0]) ** self.gamma * tooth, g ** self.gamma * tooth, (g * ratio[1]) ** self.gamma * tooth]
        return tuple(v * (1.0 + self.rng.gauss(0.0, self.noise)) for v in matvec(_P, light))


def _flat_after(panel, cube, fixed, rep, span=SPAN):
    """The after-balance through the simulated display against the core's TARGET, at every span code (ln %)."""
    from dlc.colormath import invert3x3
    Pinv = invert3x3(_P)
    out = {}
    noise, panel.noise = panel.noise, 0.0
    for c in range(span[0], span[1] + 1):
        sh = matvec(Pinv, list(panel.xyz(fixed, (c / 1023,) * 3)))
        p = rep["predicted"][str(c)]
        out[c] = (100 * (math.log(sh[0] / sh[1]) - p["target_rg"]), 100 * (math.log(sh[2] / sh[1]) - p["target_bg"]))
    panel.noise = noise
    return out


def _tint_rg(panel, cube, span=SPAN):
    from dlc.colormath import invert3x3
    Pinv = invert3x3(_P)
    noise, panel.noise = panel.noise, 0.0
    v = {c: math.log((lambda sh: sh[0] / sh[1])(matvec(Pinv, list(panel.xyz(cube, (c / 1023,) * 3)))))
         for c in range(span[0], span[1] + 1)}
    panel.noise = noise
    ref = [(v[span[0]] * (span[1] - c) + v[span[1]] * (c - span[0])) / (span[1] - span[0]) for c in v]
    return 100 * max(abs(v[c] - r) for c, r in zip(v, ref))


@pytest.mark.parametrize("kind", ["blur", "gain"])
def test_sizing_blurred_or_gain_response_is_sized_right_and_lands_flat(kind):
    """(a) blur sigma 2: the old sharp fit read it as k ~0.77; the response fit finds the blur, gain ~1, alpha ~1.
    (b) a pure gain of 0.77: k ~0.77, and the full removal is still right (the gain scales the change too):
    alpha ~1. Both land flat within the clean scatter, confirmed."""
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])
    panel = ResponsePanel(None, sigma=2.0) if kind == "blur" else ResponsePanel(None, gain=0.77)
    before = {c: panel.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
    fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                   peak_luminance=_PEAK, span=SPAN, luminance_sigma=2e-4)
    conf, resp = rep["confirmation"], rep["response"]
    assert conf["confirmed"], conf
    if kind == "blur":
        assert 1.5 <= resp["sigma_codes"] <= 2.5 and all(0.85 <= k <= 1.2 for k in resp["k"]), resp
    else:
        assert resp["sigma_codes"] <= 0.5 and all(0.7 <= k <= 0.85 for k in resp["k"]), resp
    assert all(abs(a - 1.0) < 0.1 for a in rep["amplitude"]["alpha"]), rep["amplitude"]
    after = _flat_after(panel, cube, fixed, rep)
    assert _tint_rg(panel, cube) > 2.0                               # a tint of several % before
    assert max(max(abs(v[0]), abs(v[1])) for v in after.values()) < 0.25, after   # flat after


def test_sizing_cannot_see_the_panels_own_cancelling_structure_but_the_verify_does(tmp_path):
    """(c) The panel's own balance cancels 23 % of the cube's tint: before the repair this is indistinguishable
    from a gain of 0.77 (same reads), so the core sizes alpha ~1 and predicts a change of 0.77 x the removal; the
    display then changes by the FULL removal and lands magenta. The verify, judging each held-out code against
    its prediction, catches it (and the keep seam recommends revert). Low read noise here (2e-4); at hardware
    noise see test_review_cancelling_structure_at_hardware_noise."""
    ctrl, cube_path, src = _setup(tmp_path)
    cube = _real_cube()
    probe = ResponsePanel(None, noise=0.0)
    from dlc.colormath import invert3x3
    Pinv = invert3x3(_P)

    def drive_bal(c):
        d = br.scanout_grey_drive(cube, _APPLIED, c / 1023)
        return math.log(d[0] / d[1]), math.log(d[2] / d[1])
    base = {c: drive_bal(c) for c in range(100, 200)}
    lo_ref = {c: base[147][0] + (base[157][0] - base[147][0]) * (c - 147) / 10 for c in range(100, 200)}
    lb_ref = {c: base[147][1] + (base[157][1] - base[147][1]) * (c - 147) / 10 for c in range(100, 200)}

    def own(c):
        i = int(round(c))
        if not 147 <= i <= 157:
            return (0.0, 0.0)
        return (-0.23 * 2.2 * (base[i][0] - lo_ref[i]), -0.23 * 2.2 * (base[i][1] - lb_ref[i]))
    panel = ResponsePanel(ctrl, own=own)
    calib = _make(tmp_path, "own", ctrl, panel=panel, source=src,
                  decision_overrides={"repair-base:apply": Decision("apply")})   # keep: the recommendation
    result = calib.run("repair-base")
    build = calib.calib["stages"]["repair-build"]["digest"]
    assert build["confirmation"]["confirmed"] and all(0.7 <= k <= 0.85 for k in build["response"]["k"])
    verdict = calib.calib["stages"]["repair-verify"]["digest"]
    assert verdict["judged_against"].startswith("the audit's prediction")
    missed = [m for r in verdict["out"] for m in r["missed"]]
    assert any("change" in m or "not flat" in m for m in missed), verdict["out"]
    assert result.status == "reverted"                           # AutoAdjudicator takes the recommendation


def test_sizing_refuses_a_tint_the_cube_does_not_explain():
    """The reads carry an extra balance feature inside the tint that the cube's shape cannot produce (a narrow
    +1.2 % r/g bump on the tint's upper flank): not explained by the response, the predicted after is not flat ->
    refused (nothing installed). (A feature among the CLEAN greys instead widens the clean scatter itself — the
    yardstick is what the untouched greys show.)"""
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])

    def own(c):
        return (0.012 * math.exp(-0.5 * ((c - 154.0) / 1.0) ** 2), 0.0)
    panel = ResponsePanel(None, own=own)
    before = {c: panel.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
    _fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                    peak_luminance=_PEAK, span=SPAN, luminance_sigma=2e-4)
    assert rep["confirmation"]["confirmed"] is False
    assert rep["confirmation"]["refused_because"]


def test_flow_on_a_blurred_toothed_display_keeps_and_reports_light_as_observation(tmp_path):
    """The whole flow on the blurred display with light teeth in the green drive code (apply / keep decided):
    the change lands as predicted (no change / model / light miss), light at changed codes is judged against the
    audit's cap, the light steps the channels' drive moves cross are named, and the only possible misses are the
    MODEL-FREE flatness ones (the bridge target's own bulge against the neighbours' line)."""
    ctrl, cube_path, src = _setup(tmp_path)
    panel = ResponsePanel(ctrl, sigma=2.0, teeth=0.03)
    calib = _make(tmp_path, "blur", ctrl, panel=panel, source=src)
    result = calib.run("repair-base")
    assert result.status == "completed", calib.calib["stages"].get("repair-verify")
    verdict = calib.calib["stages"]["repair-verify"]["digest"]
    assert verdict["inside"] > 0
    assert all("model-free" in m for r in verdict["out"] for m in r["missed"]), verdict["out"]
    inside = [r for r in verdict["held_out"] if r.get("where") == "inside"]
    assert all(r.get("light_cap_pct") for r in inside)
    assert verdict["tooth_edge_codes"]
    assert verdict["free"]["before_worst_pct"][0] > 2.0 > verdict["free"]["after_worst_pct"][0]
    build = calib.calib["stages"]["repair-build"]["digest"]
    assert 1.5 <= build["response"]["sigma_codes"] <= 2.5
    for c, p in build["predicted"].items():
        assert "post_rg_pct" in p and "tooth_edge" in p


# ---------------------------------------------------------------------------
# REVIEW of 1c2527e: power at HARDWARE noise, model-free flatness, light gating, degenerate series.
# Read noise 6e-4 per XYZ component gives a model-free single-read balance scatter of 0.11-0.26 % on this
# simulated display (the 2026-11-14 audit: 0.12-0.16 %, upper bound 0.16 / 0.17 %). 8 fixed seeds per case.
# Measured catch rates (scratch e7_power.py, these seeds): x0.60 8/8, x0.70 6/8, x0.75 4/8, x0.80-0.90 1/8,
# x1.10 5/8, x1.15-1.20 6/8, x1.30-1.40 8/8; shift +0.5 6/8, +1 / +1.5 / +2 8/8, -1 4/8, -2 8/8.
# SMALLEST ERROR CAUGHT IN EVERY SEED: x0.6 (40 % short), x1.3 (30 % over), +1 code, -2 codes.
# Perfect execution (x1.00): the model checks never trip; the MODEL-FREE check trips in 3/8 seeds, because the
# cube-shaped removal's bridge target itself sits up to 0.64 % (r/g) above the neighbours' line on this cube
# (noise-free, perfectly executed) — "better, not flat" by construction until the target changes.
# ---------------------------------------------------------------------------

_HW_NOISE = 6e-4
_HW_SEEDS = range(1, 9)


@pytest.fixture(scope="module")
def hw_builds():
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])
    out = []
    for seed in _HW_SEEDS:
        pb = ResponsePanel(None, sigma=2.0, noise=_HW_NOISE, seed=seed)
        before = {c: pb.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
        fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                       peak_luminance=_PEAK, span=SPAN, luminance_sigma=_HW_NOISE)
        out.append((seed, before, fixed, rep))
    return cube, plan, out


def _verdicts(hw_builds, scale, shift):
    cube, plan, builds = hw_builds
    res = []
    for seed, before, fixed, rep in builds:
        pa = ResponsePanel(None, sigma=2.0, noise=_HW_NOISE, seed=100 + seed, ref=cube, change_scale=scale,
                           change_shift=shift)
        after = {c: pa.xyz(fixed, (c / 1023,) * 3) for c in plan["held_out"]}
        res.append(br.judge_after(before, after, rep, plan["held_out"], primaries=_PRIM, native_white_xy=_WHITE,
                                  peak_luminance=_PEAK, luminance_sigma=_HW_NOISE))
    return res


def test_review_hardware_noise_is_what_the_audit_measured(hw_builds):
    _cube, _plan, builds = hw_builds
    for _seed, _b, _f, rep in builds:
        assert all(0.08 <= v <= 0.3 for v in rep["free"]["scatter_pct"]), rep["free"]
        assert rep["confirmation"]["confirmed"]


@pytest.mark.parametrize("scale,shift", [(0.6, 0.0), (1.3, 0.0), (1.4, 0.0), (1.0, 1.0), (1.0, 1.5), (1.0, 2.0),
                                         (1.0, -2.0)])
def test_review_errors_caught_in_every_seed_at_hardware_noise(hw_builds, scale, shift):
    assert all(v["out"] for v in _verdicts(hw_builds, scale, shift))


@pytest.mark.parametrize("scale,shift,at_least", [(0.7, 0.0, 5), (0.75, 0.0, 3), (1.15, 0.0, 5), (1.2, 0.0, 5),
                                                  (1.0, 0.5, 5), (1.0, -1.0, 3)])
def test_review_errors_caught_in_most_seeds(hw_builds, scale, shift, at_least):
    """The grey zone, stated: these are caught in most but not all seeds at this noise."""
    assert sum(bool(v["out"]) for v in _verdicts(hw_builds, scale, shift)) >= at_least


def test_review_perfect_execution_trips_only_the_model_free_check(hw_builds):
    verdicts = _verdicts(hw_builds, 1.0, 0.0)
    for v in verdicts:
        for r in v["out"]:
            assert all("model-free" in m for m in r["missed"]), r
    # the bridge target is not the neighbours' line: some seeds are "better, not flat" even when perfect
    assert 0 < sum(bool(v["out"]) for v in verdicts) < len(verdicts)
    for v in verdicts:
        assert v["free"]["before_worst_pct"][0] > 2.0 and v["free"]["after_worst_pct"][0] < 1.5


@pytest.mark.parametrize("frac,at_least", [(0.4, 8), (0.3, 6), (0.23, 5)])
def test_review_cancelling_structure_at_hardware_noise(frac, at_least):
    """The panel's own structure cancelling ``frac`` of the tint (before the repair the same reads as a gain):
    at hardware noise caught in every seed from 40 %; 23 % in 6 of 8 seeds (scratch e9_own_cancel.py: 10 % 3/8,
    20 % 6/8, 23 % 6/8, 30 % 7/8, 40 % 8/8). The model checks alone would keep most of them: the catch comes from
    the model-free flatness."""
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])
    probe = ResponsePanel(None, sigma=2.0, noise=0.0)
    ref = {c: probe._lr(cube, c) for c in range(120, 200)}
    line = {c: [ref[147][s] + (ref[157][s] - ref[147][s]) * (c - 147) / 10 for s in range(2)] for c in range(120, 200)}

    def own(c):
        i = int(round(c))
        return (0.0, 0.0) if not 147 <= i <= 157 else tuple(-frac * 2.2 * (ref[i][s] - line[i][s]) for s in range(2))
    caught = 0
    for seed in _HW_SEEDS:
        pb = ResponsePanel(None, sigma=2.0, noise=_HW_NOISE, seed=seed, own=own)
        before = {c: pb.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
        fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                       peak_luminance=_PEAK, span=SPAN, luminance_sigma=_HW_NOISE)
        pa = ResponsePanel(None, sigma=2.0, noise=_HW_NOISE, seed=100 + seed, own=own)
        after = {c: pa.xyz(fixed, (c / 1023,) * 3) for c in plan["held_out"]}
        v = br.judge_after(before, after, rep, plan["held_out"], primaries=_PRIM, native_white_xy=_WHITE,
                           peak_luminance=_PEAK, luminance_sigma=_HW_NOISE)
        caught += bool(v["out"])
    assert caught >= at_least


def test_review_light_is_gated_where_the_lut_did_not_change_and_capped_where_it_did():
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])
    panel = ResponsePanel(None, sigma=2.0, noise=2e-4)
    before = {c: panel.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
    fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                   peak_luminance=_PEAK, span=SPAN, luminance_sigma=2e-4)
    assert rep["light"]["cap_pct"] and rep["light"]["source"].startswith("the display's largest")
    unchanged = [c for c in plan["held_out"] if not rep["predicted"][str(c)]["changed"]]
    changed = [c for c in plan["held_out"] if rep["predicted"][str(c)]["changed"]]
    assert unchanged and changed
    # an unchanged code whose BALANCE prediction is non-zero (the blur reaches it) is still light-gated
    assert any(abs(rep["predicted"][str(c)]["change_rg"]) >= 1e-3 for c in unchanged)
    after = {c: panel.xyz(fixed, (c / 1023,) * 3) for c in plan["held_out"]}
    bump = unchanged[len(unchanged) // 2]
    after[bump] = tuple(v * 1.01 for v in after[bump])                     # +1 % light where nothing changed
    big = changed[0]
    after[big] = tuple(v * (1.0 + 2.0 * rep["light"]["cap_pct"] / 100.0) for v in after[big])  # twice the cap
    v = br.judge_after(before, after, rep, plan["held_out"], primaries=_PRIM, native_white_xy=_WHITE,
                       peak_luminance=_PEAK, luminance_sigma=2e-4)
    misses = {r["code"]: r["missed"] for r in v["out"]}
    assert "light" in misses.get(bump, []), misses
    assert any("light beyond" in m for m in misses.get(big, [])), misses


def test_review_an_unread_held_out_code_is_out():
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])
    panel = ResponsePanel(None, sigma=2.0, noise=2e-4)
    before = {c: panel.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
    fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                   peak_luminance=_PEAK, span=SPAN, luminance_sigma=2e-4)
    after = {c: panel.xyz(fixed, (c / 1023,) * 3) for c in plan["held_out"][1:]}
    v = br.judge_after(before, after, rep, plan["held_out"], primaries=_PRIM, native_white_xy=_WHITE,
                       peak_luminance=_PEAK, luminance_sigma=2e-4)
    assert any(r["code"] == plan["held_out"][0] and r["missed"] == ["unread"] for r in v["out"])


def test_review_a_series_the_reads_cannot_size_is_said_and_never_nan():
    """Reads too noisy to resolve the removal's change in either series: alpha 1 is used and SAID (sized False),
    its error is 0 (never NaN), the flatness numbers stay finite and the repair is refused (no series informative)."""
    cube = _real_cube()
    plan = br.split_fit_held([SPAN])
    panel = ResponsePanel(None, sigma=2.0, noise=2e-4)
    before = {c: panel.xyz(cube, (c / 1023,) * 3) for c in plan["codes"]}
    _fixed, rep = br.repair_balance(cube, before, plan, applied=_APPLIED, primaries=_PRIM, native_white_xy=_WHITE,
                                    peak_luminance=_PEAK, span=SPAN, luminance_sigma=0.2)   # 20 % "noise"
    per = rep["confirmation"]["per_series"]
    assert not any(p["informative"] for p in per) and not any(p["sized"] for p in per)
    assert rep["amplitude"]["alpha"] == [1.0, 1.0] and rep["amplitude"]["se"] == [0.0, 0.0]
    assert math.isfinite(rep["flat"]["worst_over_tolerance"]) and math.isfinite(rep["free"]["predicted_worst_over_tolerance"])
    for p in rep["predicted"].values():
        assert all(math.isfinite(v) for k, v in p.items() if isinstance(v, float))
    assert rep["confirmation"]["confirmed"] is False and rep["recommend_apply"] is False


def test_review_apply_is_recommended_only_when_both_flatness_checks_pass(tmp_path):
    ctrl, cube_path, src = _setup(tmp_path)
    calib = _make(tmp_path, "rec", ctrl, source=src, adjudicator=AutoAdjudicator())
    result = calib.run("repair-base")
    dec = calib.calib["decisions"]["repair-base:apply"]
    if dec["choice"] == "apply":
        assert result.status in ("completed", "reverted")
    else:
        # better, not flat (model-free): the AutoAdjudicator takes the recommendation (abort): nothing installed
        assert dec["choice"] == "abort" and result.status == "aborted"
        assert installed_base_cube(ctrl, 0, "SDR")[0] == str(cube_path)
    # on this cube the bridge target is not the neighbours' line: the recommendation here IS abort
    assert dec["choice"] == "abort"
