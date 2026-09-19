"""FALD profiling flow hardening (2026-09-15): layer-OFF reads wait for the overlay path, OFF/identity alternate and an
outlier is re-read; the plans follow the panel (near gaps, bar / ramp sizes in mm) while the ProArt set stays
byte-identical; the meter stamp / legacy_meter rules; drift / no-read anomalies and the high → judge_* rule."""
from __future__ import annotations

import hashlib
import json
from argparse import Namespace

import pytest

pytest.importorskip("scipy")
from dlc.controller import CalibrationController  # noqa: E402
from dlc.events import EventWriter  # noqa: E402
from dlc.fald import profile as P  # noqa: E402
from dlc.runs import create_run  # noqa: E402
from dlc.stage import StageResult  # noqa: E402
from dlc.stages import _common, fald_profile  # noqa: E402

FP = fald_profile


# ----------------------------------------------------------------------------- plans
def _canon(pats):
    return [[p.name, p.group, p.kind, p.ref, list(p.field), [[list(c), [round(v, 9) for v in g]] for c, g in p.shapes]] for p in pats]


def _digest(pats) -> str:
    return hashlib.sha256(json.dumps(_canon(pats), separators=(",", ":")).encode()).hexdigest()[:16]


PROART = {
    "hdr": dict(width=3840, height=2160, cols=48, rows=48, diagonal_in=32.0, meter=(1947, 1114), white_nits=1845.926085),
    "sdr": dict(width=3840, height=2160, cols=48, rows=48, diagonal_in=32.0, meter=(1943, 1114), transfer="gamma", bit_depth=8,
                white_nits=121.89873, sdr_gamma=2.2709157966125044),
}
# (count, digest of names + groups + kinds + refs + fields + shapes) of every ProArt plan as the 2026-09-14 runs drew them
# (checked against runs/fald_profile_pa32ucxr_{sdr,hdr}/fald/*.json before the generalisation) — existing run files
# must keep matching the plans
PROART_PINNED = {
    "hdr:register": (45, "7ab85953bfd18f95"), "hdr:grid": (28, "04fb041caeddeade"), "hdr:drive": (40, "3a356d77945ba56e"),
    "hdr:leak": (24, "2f0ea3fc7aa86f25"), "hdr:rings": (70, "e40981ed81166d71"), "hdr:heldout": (20, "b3d7f3b6b4a911ae"),
    "hdr:verify": (14, "4b6d60161c86e4d0"), "hdr:augment": (47, "023a93b8e731fc0b"), "hdr:verify_extended": (24, "a8f4e7d768fc4eef"),
    "sdr:register": (45, "dd275e463d05a1bd"), "sdr:grid": (28, "0f2f632fb181ce8b"), "sdr:drive": (40, "26951599666cf198"),
    "sdr:leak": (24, "48d73ac235b5b6bb"), "sdr:rings": (70, "b72e7dcaa9444b6f"), "sdr:heldout": (20, "d53fcccfdadbd963"),
    "sdr:verify": (14, "71fc81e7eb86459c"), "sdr:augment": (47, "aafed2dc66c8a658"), "sdr:verify_extended": (24, "638204d59be22cfd"),
    # the default nominal meter (x = 1890, 50 px into its cell): every plan as before EXCEPT drive, whose source cell
    # moved from c+2 to c+3 on 2026-09-15 — c+2's full-cell sliver sat 110 px from the sensor, inside the 120-px keep-out
    "hdr1890:register": (45, "d3cce3b70fb283b2"), "hdr1890:grid": (28, "beee8be943af8508"), "hdr1890:drive": (40, "4223e6e01ea49e75"),
    "hdr1890:leak": (24, "c0e13d596bb75f88"), "hdr1890:rings": (70, "8075b8715dface30"), "hdr1890:heldout": (20, "85b0d4a9a153fc13"),
    "hdr1890:verify": (14, "777a16a1ba2a9f3a"), "hdr1890:augment": (47, "9ab25fab523b30ee"),
    "hdr1890:verify_extended": (24, "b91e710f60a1b9b4"),
}
PROART["hdr1890"] = {**PROART["hdr"], "meter": (1890, 1110)}


def _all_plans():
    return {**P.PLANS, "verify_extended": P.plan_verify_extended}


@pytest.mark.parametrize("key", sorted(PROART_PINNED))
def test_proart_plans_are_pinned(key):
    mode, plan = key.split(":")
    g = P.PanelGeometry.from_diagonal(**PROART[mode])
    pats = _all_plans()[plan](g)
    assert (len(pats), _digest(pats)) == PROART_PINNED[key], [p.name for p in pats]


GEOS = {
    "proart_32_4k_48x48": (dict(width=3840, height=2160, cols=48, rows=48, diagonal_in=32.0, meter=(1947, 1114)), [120, 240, 480]),
    "27in_4k_48x24": (dict(width=3840, height=2160, cols=48, rows=24, diagonal_in=27.0, meter=(1940, 1100)), [142, 284, 568]),
    "32in_6k_60x34": (dict(width=6016, height=3384, cols=60, rows=34, diagonal_in=32.0, meter=(3010, 1700)), [187, 374, 748]),
    "16in_1080p_24x24": (dict(width=1920, height=1080, cols=24, rows=24, diagonal_in=16.0, meter=(975, 555)), [120, 240, 480]),
    "27in_1440p_40x25_sdr": (dict(width=2560, height=1440, cols=40, rows=25, diagonal_in=27.0, meter=(1290, 730), transfer="gamma",
                                  bit_depth=8, white_nits=250.0), [96, 192, 384]),
}


def _geo(name):
    kw, _ = GEOS[name]
    kw = dict(kw)
    kw.setdefault("white_nits", 1000.0)
    return P.PanelGeometry.from_diagonal(**kw)


@pytest.mark.parametrize("name", sorted(GEOS))
def test_near_gaps_and_verify_roles_follow_the_panel(name):
    g = _geo(name)
    near = P.near_gaps(g)
    assert near == GEOS[name][1] and near[0] >= g.min_gap_h
    fine = P.fine_gaps(g)
    assert fine[0] == near[0] and len(fine) == P.FINE_STEPS_PER_CELL + 1 and all(b > a for a, b in zip(fine, fine[1:]))
    missing: list = []
    pats = P.plan_verify_extended(g, missing=missing)            # was KeyError 'LOW1:L120' on the 27" 4K / 32" 6K
    assert missing == []
    names = {p.name for p in pats}
    assert {f"VX:LOW1:L{near[0]}", f"VX:LOW1:R{near[0]}", f"VX:BAR2:L{near[0]}", f"VX:BAR5:L{near[0]}", "VX:RAMP:hdown", "VX:RAMP:vdown"} <= names
    aug = {p.name: p for p in P.plan_augment(g)}
    held_bar = aug[f"BAR5:R{near[0]}"]
    assert held_bar.meta["gap"] == near[0] and held_bar.meta["gap_rank"] == 0


@pytest.mark.parametrize("name", sorted(GEOS))
def test_plans_respect_keepout_resolve_refs_and_label_real_gaps(name):
    g = _geo(name)
    mx, my = g.meter
    for plan, fn in _all_plans().items():
        pats = fn(g)
        names = {p.name: p for p in pats}
        for p in pats:
            if p.kind == "ratio":
                assert p.ref in names and names[p.ref].kind == "aux", (plan, p.name)
            # the drawn rect's near edge IS the labelled gap
            side, gap = p.meta.get("side"), p.meta.get("gap")
            if side and gap is not None and len(p.shapes) == 2:
                x, y, w, h = p.shapes[1][1]
                x0, y0, x1, y1 = x * g.width, y * g.height, (x + w) * g.width, (y + h) * g.height
                drawn = {"R": x0 - mx, "L": mx - x1, "D": y0 - my, "U": my - y1}
                if side in drawn:
                    assert abs(drawn[side] - gap) <= 0.51, (plan, p.name, gap, drawn[side])
                else:                                                     # diagonal corner windows: both axes
                    dx = x0 - mx if side[1] == "R" else mx - x1
                    dy = y0 - my if side[0] == "D" else my - y1
                    assert abs(dx - gap) <= 0.51 and abs(dy - gap) <= 0.51, (plan, p.name, gap, dx, dy)
            if plan == "register":
                continue                                                  # sweeps an edge ACROSS the sensor by design
            for code, (x, y, cx, cy) in p.shapes[1:]:
                if max(code) < g.code(0.5 * g.white_nits):
                    continue
                x0, y0, x1, y1 = x * g.width, y * g.height, (x + cx) * g.width, (y + cy) * g.height
                if x0 <= mx <= x1 and y0 <= my <= y1:
                    assert plan == "drive" and (p.group in ("peak", "white", "primaries") or p.name.startswith("DRV:flat")), (plan, p.name)
                    continue
                dx = max(x0 - mx, mx - x1, 0.0)
                dy = max(y0 - my, my - y1, 0.0)
                assert dx >= g.min_gap_h - 1 or dy >= g.min_gap_v - 1, (plan, p.name, dx, dy)


def test_bar_and_ramp_sizes_are_physical():
    pro, six = _geo("proart_32_4k_48x48"), _geo("32in_6k_60x34")
    for g in (pro, six):
        aug = {p.name: p for p in P.plan_augment(g)}
        near = P.near_gaps(g)
        bar = aug[f"BAR20:L{near[1]}"].shapes[1][1]
        w_mm, h_mm = bar[2] * g.width * g.px_mm, bar[3] * g.height * g.px_mm
        assert abs(w_mm - P.BAR_MM[0]) < g.px_mm and abs(h_mm - P.BAR_MM[1]) < g.px_mm
        ramp = aug["RAMP:hup"].shapes
        strips = ramp[2:]
        assert abs(len(strips) * strips[0][1][2] * g.width - (ramp[1][1][0] * g.width - strips[0][1][0] * g.width)) < 1e-6   # strips tile the span
    assert pro.mm_px(P.BAR_MM[0]) == 40 and pro.mm_px(P.BAR_MM[1]) == 600 and pro.mm_px(P.RAMP_STRIP_MM) == 8


def test_plan_augment_has_no_dead_branch():
    import inspect
    assert "if False" not in inspect.getsource(P.plan_augment.__wrapped__)


# ----------------------------------------------------------------------------- overlay wait
class _FakeCtl:
    def __init__(self, *, lag_polls=0, never=False, field=True, start_awake=True):
        self.lag, self.never, self.field, self.awake, self.polls = lag_polls, never, field, start_awake, 0

    def state(self):
        self.polls += 1
        if not self.field:
            return {"layers": {}}
        if not self.never:
            if self.lag <= 0:
                self.awake = False
            self.lag -= 1
        return {"overlay": {"awake": self.awake, "dwm_hook_mode": False}}


def test_wait_overlay_lag_reaches_asleep_then_dwells():
    vc = FP.VirtualClock()
    ctl = _FakeCtl(lag_polls=7)
    w = FP.wait_overlay(ctl, False, sleep=vc.sleep, clock=vc.now)
    assert w["ok"] is True and w["awake"] is False and w["polls"] == 8
    assert abs(vc.now() - (7 * FP.OVERLAY_POLL_S + FP.OVERLAY_DWELL_S)) < 1e-9


def test_wait_overlay_never_sleeps_times_out():
    vc = FP.VirtualClock()
    w = FP.wait_overlay(_FakeCtl(never=True), False, sleep=vc.sleep, clock=vc.now)
    assert w["ok"] is False and w["awake"] is True and FP.OVERLAY_TIMEOUT_S <= w["waited_s"] <= FP.OVERLAY_TIMEOUT_S + 0.2


def test_wait_overlay_old_build_fixed_wait():
    vc = FP.VirtualClock()
    w = FP.wait_overlay(_FakeCtl(field=False), False, sleep=vc.sleep, clock=vc.now)
    assert w["ok"] is None and "before 2026-09-13" in w["reason"] and vc.now() == FP.OVERLAY_OLD_BUILD_WAIT_S


def test_mock_overlay_awake_follows_shader_layers_with_knobs(tmp_path):
    ctl = CalibrationController.mock()
    srv = ctl.client.transport.server
    assert ctl.state()["overlay"]["awake"] is False                  # nothing needs the overlay
    ctl.set_layers(1, "SDR", fald=True)
    assert ctl.state()["overlay"]["awake"] is False                  # a FALD flag without a panel file cannot run (C++)
    panel = tmp_path / "panel.bin"
    panel.write_bytes(b"\0" * 8)                                     # unclassifiable header: accepted, transfer "unknown"
    ctl.call("runtime.set_fald_params", {"monitor": 1, "mode": "HDR", "params_path": str(panel)})
    ctl.set_layers(1, "HDR", fald=True)
    ctl.set_layers(1, "SDR", fald=False)
    assert ctl.state()["overlay"]["awake"] is False                  # the HDR row of a monitor that is live in SDR
    ctl.set_layers(1, "HDR", fald=False)
    ctl.call("runtime.set_fald_params", {"monitor": 1, "mode": "SDR", "params_path": str(panel)})
    ctl.set_layers(1, "SDR", fald=True)
    assert ctl.state()["overlay"]["awake"] is True
    ctl.set_layers(1, "SDR", fald=False)
    assert ctl.state()["overlay"]["awake"] is False                  # no lag: asleep at the next poll
    srv.state.overlay_sleep_lag_polls = 2
    ctl.set_layers(1, "SDR", fald=True)
    assert ctl.state()["overlay"]["awake"] is True
    ctl.set_layers(1, "SDR", fald=False)
    assert [ctl.state()["overlay"]["awake"] for _ in range(4)] == [True, True, False, False]
    srv.state.overlay_keep_awake = True
    assert all(ctl.state()["overlay"]["awake"] for _ in range(5))


# ----------------------------------------------------------------------------- persisted layer options forced off
class _TrackerSession:
    """The three things OverlayTracker needs of a Session."""

    def __init__(self, controller):
        self.controller = controller
        vc = FP.VirtualClock()
        self.sleep, self.now = vc.sleep, vc.now


class _RefusingCtl:
    """A controller whose named verbs fail, state.get answered by the real mock (a build / pipe that refuses them)."""

    def __init__(self, inner, refuse):
        self.inner, self.refuse, self.calls = inner, set(refuse), []

    def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        if method in self.refuse:
            raise RuntimeError(f"unknown method: {method}")
        return self.inner.call(method, params)


def test_overlay_tracker_forces_starfield_balancing_off_and_restores_it():
    """Work guide S1: starfield balancing CHANGES sparse highlights on purpose — identity / ON reads of a measuring phase
    must never go through it. It is a persisted owner setting, so the tracker switches it off and puts it back."""
    ctl = CalibrationController.mock()
    ctl.call("runtime.fald_starfield", {"monitor": 1, "mode": "SDR", "enabled": True, "even": 0.7, "lift": 0.2, "even_reach": 5})
    ctl.call("runtime.fald_temporal", {"monitor": 1, "mode": "SDR", "temporal_mode": 1, "tau_rise_ms": 80})
    tr = FP.OverlayTracker(_TrackerSession(ctl), 1, "SDR")
    layers = ctl.state()["layers"]["1:SDR"]
    assert layers["fald_starfield"] is False and layers["fald_temporal_mode"] == 0      # both forced off for the reads
    assert layers["fald_star_even"] == 0.7 and layers["fald_star_even_reach"] == 5       # the numbers are not touched
    meta = tr.file_meta()
    assert meta["starfield_forced_off"] is True and meta["temporal_forced_off"] is True
    assert meta["starfield_saved"]["enabled"] is True and meta["starfield_saved"]["even"] == 0.7
    assert meta["starfield_saved"]["lift"] == 0.2 and meta["starfield_saved"]["even_reach"] == 5
    assert "forced OFF" in meta["starfield_note"]
    assert ctl.state()["layers"]["1:HDR"]["fald_starfield"] is False                     # the other mode was never on
    tr.restore()
    layers = ctl.state()["layers"]["1:SDR"]
    assert layers["fald_starfield"] is True and layers["fald_star_even"] == 0.7 and layers["fald_temporal_mode"] == 1
    res = StageResult("t")
    tr.report(res, "rings")
    assert not res.anomalies and any("starfield balancing was ON" in n for n in res.notes)


def test_overlay_tracker_leaves_an_off_starfield_alone():
    ctl = _RefusingCtl(CalibrationController.mock(), refuse=())
    tr = FP.OverlayTracker(_TrackerSession(ctl), 1, "SDR")
    meta = tr.file_meta()
    assert meta["starfield_forced_off"] is False and meta["starfield_note"] is None
    assert meta["starfield_saved"]["enabled"] is False and meta["starfield_saved"]["even_reach"] == 8
    tr.restore()
    assert [m for m, _ in ctl.calls if m == "runtime.fald_starfield"] == []               # never switched, never restored
    res = StageResult("t")
    tr.report(res, "rings")
    assert not res.anomalies and not any("starfield" in n for n in res.notes)


def test_overlay_tracker_notes_a_build_without_the_starfield_verb():
    class _OldBuild:
        def call(self, method, params=None):
            assert method == "state.get", method                                          # nothing to switch on an old build
            return {"layers": {"1:SDR": {"fald": False, "fald_temporal_mode": 0}}, "overlay": {"awake": False}}
    tr = FP.OverlayTracker(_TrackerSession(_OldBuild()), 1, "SDR")
    meta = tr.file_meta()
    assert meta["starfield_saved"] is None and meta["starfield_forced_off"] is False
    assert "build without runtime.fald_starfield" in meta["starfield_note"]
    res = StageResult("t")
    tr.report(res, "rings")
    assert not res.anomalies and any("build without runtime.fald_starfield" in n for n in res.notes)


def test_overlay_tracker_flags_a_starfield_it_could_not_switch_off_or_restore():
    inner = CalibrationController.mock()
    inner.call("runtime.fald_starfield", {"monitor": 1, "mode": "SDR", "enabled": True})
    ctl = _RefusingCtl(inner, refuse=("runtime.fald_starfield",))
    tr = FP.OverlayTracker(_TrackerSession(ctl), 1, "SDR")
    meta = tr.file_meta()
    assert meta["starfield_forced_off"] is False and meta["starfield_saved"] is None and "refused" in meta["starfield_note"]
    res = StageResult("t")
    tr.report(res, "verify")
    assert [(a.code, a.severity) for a in res.anomalies] == [("starfield_state", "high")]   # reads went THROUGH the balancing
    # switched off fine, but the restore fails: the owner must hear about it (and the temporal note stays a note)
    ctl2 = _RefusingCtl(inner, refuse=())
    tr2 = FP.OverlayTracker(_TrackerSession(ctl2), 1, "SDR")
    assert tr2.file_meta()["starfield_forced_off"] is True
    ctl2.refuse.add("runtime.fald_starfield")
    tr2.restore()
    res2 = StageResult("t")
    tr2.report(res2, "verify")
    assert [(a.code, a.severity) for a in res2.anomalies] == [("starfield_state", "high")]
    assert "NOT restored" in res2.anomalies[0].detail


# ----------------------------------------------------------------------------- stage helpers
_DEFAULTS = dict(monitor=1, mode="SDR", simulate=True, pipe="", zones="32x18", diagonal_in=32.0, px_mm=None, meter=None,
                 bit_depth=None, white_nits=1000.0, dogegen_server="127.0.0.1:28930", settle=0.0, profile=None,
                 no_native=False, quick=True, knots="never", verbose=False, name="sim", out=None, bin=None, fit_json=None,
                 keep_geometry=False, augment_regime="auto", lum_fade=None, extended=False)


def _ns(ctx, **over):
    return Namespace(**{**_DEFAULTS, "run": ctx.root, **over})


def _run(ctx, phase, **over):
    res = fald_profile.build(_ns(ctx, phase=phase, **over), ctx)
    _common.record_stage(ctx, res)
    return res


def _sim_state(ctx):
    return json.loads((ctx.root / _common.SIM_STATE_FILE).read_text(encoding="utf-8"))


def _set_overlay_knobs(ctx, **knobs):
    p = ctx.root / _common.SIM_STATE_FILE
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw.setdefault("overlay_model", {}).update(knobs)
    p.write_text(json.dumps(raw), encoding="utf-8")


def _export_synthetic_panel(ctx, tmp_path):
    """A fit JSON from the geometry's base params + its exported panel file, recorded in the run like fit + export."""
    from dlc.fald.export import export_panel_params
    from dlc.fald.model import FaldModel
    st = _common.load_dlc_state(ctx)
    g = fald_profile._geometry(st)
    params = g.base_params()
    fit_path = ctx.root / "fald" / "fald_fit_result.json"
    fit_path.parent.mkdir(parents=True, exist_ok=True)
    fit_path.write_text(json.dumps({"params": P.params_dict(params)}, default=float), encoding="utf-8")
    bin_path = (tmp_path / "export" / "sim_sdr_fald_panel.bin").resolve()
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    export_panel_params(FaldModel(params), bin_path)
    st["fald"].update({"fit_path": str(fit_path), "bin_path": str(bin_path), "export_fit_json": str(fit_path)})
    _common.save_dlc_state(ctx, st)


def test_stage_augment_interleaves_off_and_identity_on_the_right_overlay_path(tmp_path, capsys):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    _export_synthetic_panel(ctx, tmp_path)
    _set_overlay_knobs(ctx, sleep_lag_polls=3)                        # the overlay lingers a few polls after the layer goes off
    capsys.readouterr()
    res = _run(ctx, "augment")
    assert res.status == "ran", res.as_dict()
    assert res.metrics["states"] == ["off", "id"]
    off = json.loads((ctx.root / "fald" / "augment.json").read_text(encoding="utf-8"))
    ident = json.loads((ctx.root / "fald" / "augment_id.json").read_text(encoding="utf-8"))
    n = len(off["patterns"])
    assert off["complete"] and ident["complete"] and len(off["reads"]) == n and len(ident["reads"]) == n
    assert off["state"] == "off" and ident["state"] == "id"
    assert off["off_overlay"] == "asleep" and res.metrics["off_overlay"] == "asleep"
    waits = off["overlay_wait"]
    assert waits["off"]["polls"] > waits["off"]["n"] and waits["id"]["polls"] >= waits["id"]["n"] > 0   # the lag was polled out
    assert waits["off"]["timeouts"] == 0 and waits["id"]["timeouts"] == 0
    assert not [a for a in res.anomalies if a.code.startswith("overlay_")]
    # alternating order: pattern 0 off→id, pattern 1 id→off
    labels = [ln.split()[0] + " " + ln.split()[1] for ln in capsys.readouterr().err.splitlines() if "[off]" in ln or "[id]" in ln]
    first, second = off["patterns"][0]["name"], off["patterns"][1]["name"]
    assert labels[:4] == [f"{first} [off]", f"{first} [id]", f"{second} [id]", f"{second} [off]"]
    # left behind: layer off, debug 0
    sim = _sim_state(ctx)
    assert sim["layers"]["1:SDR"]["fald"] is False and sim["fald"]["1:SDR"]["debug_mode"] == 0
    # auto regime → the identity reads feed the fit
    s = fald_profile._open_session(_ns(ctx, phase="fit"), ctx, fald_profile._state(ctx), need_meter=False)
    _, _, _, items = fald_profile._collect_items(s)
    assert s.args.augment_regime == "id" and s.st["fald"]["augment_regime"] == {"requested": "auto", "used": "id"}
    assert {"halo", "ramp", "rings@low"} <= {it["group"] for it in items}


def test_stage_augment_overlay_that_never_sleeps_is_tagged(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    _set_overlay_knobs(ctx, keep_awake=True)                          # something else keeps the overlay awake
    res = _run(ctx, "augment")                                         # no panel file: OFF only
    assert res.status == "ran", res.as_dict()
    off = json.loads((ctx.root / "fald" / "augment.json").read_text(encoding="utf-8"))
    assert off["off_overlay"] == "awake" and off["overlay_wait"]["off"]["timeouts"] > 0
    an = {a.code: a for a in res.anomalies}
    assert an["overlay_never_slept"].severity == "medium"


def test_off_only_augment_retires_the_old_identity_file(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    _export_synthetic_panel(ctx, tmp_path)
    assert _run(ctx, "augment").metrics["states"] == ["off", "id"]
    st = _common.load_dlc_state(ctx)
    st["fald"].pop("bin_path")                                        # e.g. the panel file is gone: this augment reads OFF only
    _common.save_dlc_state(ctx, st)
    res = _run(ctx, "augment")
    assert res.status == "ran" and res.metrics["states"] == ["off"]
    assert {a.code: a.severity for a in res.anomalies}.get("stale_identity_file") == "medium"
    fald = ctx.root / "fald"
    assert not (fald / "augment_id.json").exists() and (fald / "augment_id.stale.json").exists()
    s = fald_profile._open_session(_ns(ctx, phase="fit"), ctx, fald_profile._state(ctx), need_meter=False)
    fald_profile._collect_items(s)
    assert s.args.augment_regime == "off"
    # an identity file put back by hand is still not the CURRENT augment's
    (fald / "augment_id.stale.json").replace(fald / "augment_id.json")
    st2 = fald_profile._state(ctx)
    s2 = fald_profile._open_session(_ns(ctx, phase="fit"), ctx, st2, need_meter=False)
    fald_profile._collect_items(s2)
    assert s2.args.augment_regime == "off" and [a[0] for a in st2["fald"]["_pending_anomalies"]] == ["augment_regime_fallback"]


def test_stage_augment_layer_that_cannot_run_is_never_woke(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    _export_synthetic_panel(ctx, tmp_path)
    p = ctx.root / _common.SIM_STATE_FILE
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["hdr"] = {"1": True}                                          # the monitor went live HDR: the SDR FALD row cannot run
    p.write_text(json.dumps(raw), encoding="utf-8")
    res = _run(ctx, "augment")
    assert res.status == "ran", res.as_dict()
    an = {a.code: a.severity for a in res.anomalies}
    assert an.get("overlay_never_woke") == "high" and "overlay_never_slept" not in an
    assert res.metrics["overlay_wait"]["id"]["timeouts"] == res.metrics["overlay_wait"]["id"]["n"]


def test_fit_records_ref_outliers_of_both_augment_states(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    pats = _ref_block("B", 40)
    fald = ctx.root / "fald"
    fald.mkdir(parents=True, exist_ok=True)
    ys = {"augment": {"B:ref_end": 5.3}, "augment_id": {}}
    for stem, over in ys.items():
        reads = [{"name": q.name, "xyz": [over.get(q.name, 5.0)] * 3, "t_read_s": 0.1, "error": None} for q in pats]
        (fald / f"{stem}.json").write_text(json.dumps({"phase": "augment", "state": "off" if stem == "augment" else "id", "complete": True,
                                                         "meter": [1250, 750], "patterns": [q.as_dict() for q in pats],
                                                         "reads": reads}), encoding="utf-8")
    res = _run(ctx, "fit", augment_regime="off")
    ro = res.metrics["ref_outlier"]
    assert ro["augment"][0]["ref"] == "B:ref" and ro["augment"][0]["rule"] == "consistent_with_other_state" and "augment_id" not in ro


def test_auto_regime_falls_back_to_off_without_complete_identity(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    (ctx.root / "fald").mkdir(parents=True, exist_ok=True)
    (ctx.root / "fald" / "augment_id.json").write_text(json.dumps({"phase": "augment", "complete": False, "meter": [1, 1],
                                                                    "patterns": [], "reads": []}), encoding="utf-8")
    st = fald_profile._state(ctx)
    s = fald_profile._open_session(_ns(ctx, phase="fit"), ctx, st, need_meter=False)
    fald_profile._collect_items(s)
    assert s.args.augment_regime == "off"
    assert [a[0] for a in st["fald"]["_pending_anomalies"]] == ["augment_regime_fallback"]


# ----------------------------------------------------------------------------- _run_patterns unit harness
def _session(ctx, read, *, controller=None, sim=True):
    st = {"fald": {"geometry": {"meter": [100, 100]}, "phases": {}}}
    vc = FP.VirtualClock()
    return FP.Session(Namespace(monitor=1, simulate=True), ctx, st, controller or CalibrationController.mock(), read,
                      EventWriter(ctx.events_path), lambda: None, sim, sleep=vc.sleep, now=vc.now)


def _aux(name, level):
    return P.Pattern(name, "t", [((level, level, level), P.FULL)], (level, level, level), "aux")


def test_off_id_outlier_is_reread_and_the_consistent_pass_kept(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    pats = [_aux(f"P{i}", 50) for i in range(30)]
    jumps_first_pass = {"P10", "P14", "P18", "P22", "P26", "P28"}      # 6 outliers, cap = ceil(10 % of 30) = 3
    persistent = {"P14"}                                                 # this one jumps on the re-read too
    seen: dict[str, int] = {}
    order: list[str] = []

    def read(label, shapes, field, bump=0.0):
        name, st = label.split(" [")[0], label.split("[")[1].rstrip("]")
        order.append(label)
        k = seen[label] = seen.get(label, 0) + 1
        y = 5.0
        if st == "off" and name in jumps_first_pass and (k == 1 or name in persistent):
            y *= 1.0262                                                  # the quantised +2.62 % bimodal jump
        return (0.95 * y, y, 1.09 * y), 0.1, None

    states_set: list = []
    s = _session(ctx, read)
    res = StageResult("t")
    out = FP._run_patterns(s, "t", pats, res, states=("off", "id"), set_state=states_set.append)
    assert order[:4] == ["P0 [off]", "P0 [id]", "P1 [id]", "P1 [off]"]            # alternating per pattern
    assert res.metrics["rereads"] == 3
    rows = {b["name"]: b for b in res.raw["off_read_bimodal"]}
    assert rows["P10"]["rule"] == "reread_consistent" and rows["P14"]["rule"] == "median_of_both" and rows["P18"]["rule"] == "reread_consistent"
    assert {rows[n]["rule"] for n in ("P22", "P26", "P28")} == {"cap_reached"}
    assert out["off"]["P10"].y == pytest.approx(5.0)                              # the bimodal first read is not used
    assert out["off"]["P14"].y == pytest.approx(5.0 * (1 + 0.0262))               # persistent: median of both passes
    f = json.loads((ctx.root / "fald" / "t.json").read_text(encoding="utf-8"))
    rr = {r["name"]: r for r in f["rereads"]}
    assert set(rr) == {"P10", "P14", "P18"} and len(rr["P10"]["reads"]) == 2 and rr["P10"]["reads"][0]["xyz"][1] == pytest.approx(5.0 * 1.0262)
    an = [a for a in res.anomalies if a.code == "off_read_bimodal"]
    assert len(an) == 1 and an[0].severity == "medium"


def test_checkins_count_reads_across_states(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    pats = [_aux(f"P{i}", 50) for i in range(8)]
    s = _session(ctx, lambda label, shapes, field, bump=0.0: ((4.75, 5.0, 5.45), 0.1, None))
    FP._run_patterns(s, "t", pats, StageResult("t"), states=("off", "id"), set_state=lambda st: None)
    cis = [json.loads(l) for l in ctx.events_path.read_text(encoding="utf-8").splitlines() if '"check_in"' in l]
    first = cis[0]["data"]
    assert first["of"] == 16 and first["reads"] == 4 and first["states"] == ["off", "id"]


def _ref_block(prefix, level, n_ratio=4):
    ref = f"{prefix}:ref"
    pats = [_aux(ref, level)]
    for i in range(n_ratio):
        pats.append(P.Pattern(f"{prefix}:r{i}", "t", [((level, level, level), P.FULL), ((255, 255, 255), (0.6, 0.4, 0.1, 0.1))],
                              (level, level, level), "ratio", ref))
    pats.append(_aux(ref + "_end", level))
    return pats


def _drift_run(tmp_path, drifts: dict[str, float], missing: set = frozenset(), n_blocks=6):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    pats = [p for b in range(n_blocks) for p in _ref_block(f"B{b}", 40 + b)]

    def read(label, shapes, field, bump=0.0):
        name = label.split(" [")[0]
        if name in missing:
            return None, 0.1, "timeout"
        y = 5.0 * (1.0 + drifts.get(name, 0.0))
        return (y, y, y), 0.1, None

    res = StageResult("t")
    FP._run_patterns(_session(ctx, read), "t", pats, res)
    return {a.code: a for a in res.anomalies}, res


def test_reference_drift_is_one_anomaly_graded(tmp_path):
    an, res = _drift_run(tmp_path / "a", {"B0:ref_end": 0.05})
    assert an["reference_drift"].severity == "medium" and len([a for a in res.anomalies if a.code == "reference_drift"]) == 1
    an, _ = _drift_run(tmp_path / "b", {"B0:ref_end": 0.09})
    assert an["reference_drift"].severity == "high"
    an, res = _drift_run(tmp_path / "c", {"B0:ref_end": 0.04, "B1:ref_end": 0.04, "B2:ref_end": -0.04})
    assert an["reference_drift"].severity == "high" and len([a for a in res.anomalies if a.code == "reference_drift"]) == 1


def test_ref_outlier_prefers_the_read_consistent_with_the_other_state():
    pats = _ref_block("B", 40)
    off = {p.name: P.Read(p.name, (5.0, 5.0, 5.0)) for p in pats}
    off["B:ref_end"] = P.Read("B:ref_end", (5.3, 5.3, 5.3))                 # +6 %: the bimodal read
    ident = {p.name: P.Read(p.name, (5.0, 5.0, 5.0)) for p in pats}
    lst: list = []
    refs = P.ref_means(pats, off, other=ident, outliers=lst)
    assert refs["B:ref"] == pytest.approx(5.0) and lst[0]["rule"] == "consistent_with_other_state" and lst[0]["kept"] == "start"
    lst2: list = []
    assert P.ref_means(pats, off, outliers=lst2)["B:ref"] == pytest.approx(5.15) and lst2[0]["rule"] == "mean"
    assert P.ref_means(pats, off)["B:ref"] == pytest.approx(5.15)             # the fit path is unchanged


def test_ref_outlier_both_states_drifted_uses_the_mean():
    """OFF drifted 3.5 % and identity 2.9 % over the block: that is drift, not one bimodal read — picking OFF's start
    read would put a fake −1.7 % into OFF vs identity."""
    pats = _ref_block("B", 40)
    off = {p.name: P.Read(p.name, (5.0875,) * 3) for p in pats}
    ident = {p.name: P.Read(p.name, (5.0725,) * 3) for p in pats}
    off["B:ref"], off["B:ref_end"] = P.Read("B:ref", (5.0,) * 3), P.Read("B:ref_end", (5.175,) * 3)
    ident["B:ref"], ident["B:ref_end"] = P.Read("B:ref", (5.0,) * 3), P.Read("B:ref_end", (5.145,) * 3)
    lst: list = []
    refs = P.ref_means(pats, off, other=ident, outliers=lst)
    assert refs["B:ref"] == pytest.approx(5.0875) and lst[0]["rule"] == "mean_not_a_single_outlier" and lst[0]["kept"] is None


def test_ref_outlier_ignores_black_references():
    pats = [_aux("K:ref", 0), _aux("K:ref_end", 0)]
    rd = {"K:ref": P.Read("K:ref", (0.01,) * 3), "K:ref_end": P.Read("K:ref_end", (0.02,) * 3)}   # +100 % of noise
    lst: list = []
    assert P.ref_means(pats, rd, other=rd, outliers=lst)["K:ref"] == pytest.approx(0.015) and lst == []


def test_no_read_is_graded_by_share_and_references(tmp_path):
    an, _ = _drift_run(tmp_path / "a", {}, missing={"B0:r1"}, n_blocks=10)       # 1 of 60 reads, not a reference: 1.7 %
    assert an["no_read"].severity == "medium"
    an, _ = _drift_run(tmp_path / "b", {}, missing={"B0:ref"}, n_blocks=10)      # a reference whose _end twin read: ratios survive
    assert an["no_read"].severity == "medium" and "B0:ref" in an["no_read"].detail and "NO read" not in an["no_read"].detail
    an, _ = _drift_run(tmp_path / "b2", {}, missing={"B0:ref_end"}, n_blocks=10)  # a missing _end twin counts too
    assert an["no_read"].severity == "medium" and "B0:ref_end" in an["no_read"].detail
    an, _ = _drift_run(tmp_path / "d", {}, missing={"B0:ref", "B0:ref_end"}, n_blocks=60)   # both twins: every ratio lost (0.6 %)
    assert an["no_read"].severity == "high" and "NO read" in an["no_read"].detail
    an, _ = _drift_run(tmp_path / "c", {}, missing={"B0:r1", "B1:r1"}, n_blocks=10)   # 3.3 %
    assert an["no_read"].severity == "high"


def test_high_anomaly_turns_the_verdict_into_judge():
    res = StageResult("fald-profile-rings")
    res.advice = {"default_policy_verdict": "proceed_to_fit", "reasons": ["x"]}
    FP._judge_on_high("rings", res)
    assert res.advice["default_policy_verdict"] == "proceed_to_fit"          # no anomaly: untouched
    res.anomaly("reference_drift", "d", "medium")
    FP._judge_on_high("rings", res)
    assert res.advice["default_policy_verdict"] == "proceed_to_fit"
    res.anomaly("no_read", "d", "high")
    FP._judge_on_high("rings", res)
    assert res.advice["default_policy_verdict"] == "judge_rings" and res.advice["overridden_verdict"] == "proceed_to_fit"


# ----------------------------------------------------------------------------- meter stamps
def _unstamped(ctx, phase):
    d = ctx.root / "fald"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{phase}.json").write_text(json.dumps({"phase": phase, "complete": True, "patterns": [], "reads": []}), encoding="utf-8")


def test_legacy_meter_is_recorded_once_never_overwritten_and_ambiguity_flagged(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    pre = list(_common.load_dlc_state(ctx)["fald"]["geometry"]["meter"])
    _unstamped(ctx, "rings")
    reg = _run(ctx, "register")
    assert reg.status == "ran", reg.as_dict()
    st = _common.load_dlc_state(ctx)["fald"]
    assert st["legacy_meter"] == pre and st["legacy_meter_files"] == ["rings.json"]
    sensor = st["geometry"]["meter"]
    rj = json.loads((ctx.root / "fald" / "register.json").read_text(encoding="utf-8"))
    assert rj["meter"] == pre and rj["sensor_px"] == sensor                   # stamp = pre-registration, plus what it found
    # re-register: the same unstamped files were read at legacy_meter by construction — no ambiguity, no overwrite
    reg2 = _run(ctx, "register")
    assert reg2.status == "ran" and "legacy_meter_ambiguous" not in {a.code for a in reg2.anomalies}
    assert _common.load_dlc_state(ctx)["fald"]["legacy_meter"] == pre
    # a NEW unstamped file while the meter has moved: which position is ambiguous
    assert _common.load_dlc_state(ctx)["fald"]["geometry"]["meter"] != pre
    _unstamped(ctx, "leak")
    reg3 = _run(ctx, "register")
    assert "legacy_meter_ambiguous" in {a.code for a in reg3.anomalies}
    assert _common.load_dlc_state(ctx)["fald"]["legacy_meter"] == pre


def test_unstamped_file_without_legacy_meter_raises_meter_unstamped(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    assert _run(ctx, "preflight").status == "ran"
    _unstamped(ctx, "rings")
    st = fald_profile._state(ctx)
    s = fald_profile._open_session(_ns(ctx, phase="fit"), ctx, st, need_meter=False)
    fald_profile._collect_items(s)
    assert [a[0] for a in st["fald"]["_pending_anomalies"]] == ["meter_unstamped"]
    assert st["fald"]["_item_meters"]["rings"] == list(st["fald"]["geometry"]["meter"])
    # with a legacy_meter: used, no anomaly
    st2 = fald_profile._state(ctx)
    st2["fald"]["legacy_meter"] = [11, 22]
    s2 = fald_profile._open_session(_ns(ctx, phase="fit"), ctx, st2, need_meter=False)
    fald_profile._collect_items(s2)
    assert not st2["fald"].get("_pending_anomalies") and st2["fald"]["_item_meters"]["rings"] == [11, 22]
    # through build: the pending anomaly lands on the phase result
    res = _run(ctx, "fit")
    assert "meter_unstamped" in {a.code for a in res.anomalies}


def test_preflight_on_a_measured_run_needs_keep_geometry(tmp_path):
    ctx = create_run("SDR", display="sim", run_dir=tmp_path / "run")
    first = _run(ctx, "preflight", zones="31x18")
    assert first.status == "ran" and "cell_not_integer" in {a.code for a in first.anomalies}
    fixed = _run(ctx, "preflight", zones="32x18")                     # nothing measured yet: the corrected re-run is allowed
    assert fixed.status == "ran" and _common.load_dlc_state(ctx)["fald"]["geometry"]["cols"] == 32
    assert _run(ctx, "register").status == "ran"
    again = _run(ctx, "preflight")
    assert again.status == "blocked" and again.anomalies[0].code == "geometry_exists"
    assert _run(ctx, "preflight", keep_geometry=True).status == "ran"
