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
    assert verdict["out"] == [] and verdict["inside"] > 0 and verdict["beside"] > 0
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
    calib = _make(tmp_path, "revert", ctrl, source=src, decision_overrides={"repair-base:keep": Decision("revert")})
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
