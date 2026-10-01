"""BenQ PD2700U near-black / LCD black-pedestal probes HW-A, HW-B, HW-C (HANDOFF §0 section F; charter
docs/hw-session-charter-2026-10-01.md, Session 1 §1.2). Monitor 1, SDR, 8-bit HDMI.

Background (memory lcd-black-pedestal-teardrop, run 20260926_225451 raw.ti3): with no local dimming the
black glow adds the same light under every colour (XYZ(code) = pedestal + own(code)); ONE fitted constant
pedestal explains all three dim-primary ramps (0.0008 u'v' rms) but it is 1.7× the measured 0,0,0 read
(0.092 vs 0.054 nit) and bluer — cause unknown; and the applied cube solves every non-grey shell-1 node
(max code 8) to zero drive (the zero-black SDR target).

Phases (``--phase``; ``hwA,hwB`` may share one invocation — one native entry / restore):
  aid   placement aid only (no meter, no pipe change): the i1D3 body footprint as a DIM rectangle on a
        dim field, centred on the meter spot; then exit. Never a full-signal static frame.
  plan  the patch lists + an estimated duration (no pipe, no meter, nothing painted).
  hwA   NATIVE state. What is the 0,0,0 read? Codes 0 / 1 / 3 under the meter as (a) full field (the
        raw.ti3 condition; code 0 = the full-black frame), (b) a ``--hole-px`` window on a lit dim-grey
        surround (code ``--surround-code`` 64, >= 40 % of the frame, lit content >= 120 px from the meter),
        for every hole in ``--hwa-holes`` (default 600,1200 px: distance dependence), (c) codes 1 / 3 as a
        600-px window on a BLACK surround. Ends with repeats of full 0 and lit 0 (drift). The two
        hypotheses are FROZEN into hwA/expectations.json before the first read:
          H1 black-frame backlight dip — lit-surround 0,0,0 ≈ 0.092 nit (the fitted pedestal) ⇒ the SDR
             target and the dark floor should use the beside-content black;
          H2 glow only with drive (LC crosstalk) — lit-surround 0,0,0 ≈ 0.054 nit (the full-black read)
             ⇒ a pedestal-per-drive model.
        .ti3 per condition: hwA/hwA_full.ti3, hwA_lit600.ti3, hwA_lit1200.ti3, hwA_black600.ti3.
  hwB   NATIVE state. Dense near-black pure-channel ramps (default codes 2,4,6,8,12,16,20,24,28,32,36,40 —
        step 2 to 8, then 4), full-drive anchors 64/128/255 per channel (teardrop_fit.py takes each ramp's
        TOP read as the own-light chromaticity, so the ramp must reach full drive), greys 1,2,3,4,6,8 (does
        the pedestal switch on as a STEP between grey 1 (0.0595) and grey 3 (0.126)?), two-channel mixes
        RG/GB/RB at 8/16/32 (near-black additivity), black at start and end. Full-field patches (the
        raw.ti3 condition) unless ``--hwb-surround lit`` (the same codes as hole windows on the lit
        surround). Writes hwB/hwB.ti3 (or hwB_lit.ti3) and, when the replay script is present, runs
        ``teardrop_fit.py`` on it (stdout -> hwB/teardrop_fit.txt).
  hwC   THROUGH THE APPLIED STACK — owner-approved exception (2026-10-01): needs ``--through-stack``; the
        layers audit still runs and is recorded. Pure R/G/B/C/M/Y at codes 4/8/12/16/24/32 (+ 0,0,0) read
        through the APPLIED production cube and through an IDENTITY cube of the same size (written to the
        run dir), alternating the state order per colour (drift cancels); white + mid-grey spot reads
        before and after; then the applied cube is put back and CONFIRMED by state readback (cube_path
        equal, file sha256 unchanged) and the stack is diffed against the pre-probe snapshot. Expect
        cube-on red@8 ≈ black. .ti3: hwC/hwC_cube.ti3, hwC_identity.ti3, hwC_spot_before.ti3, hwC_spot_after.ti3.
        (The owner's dark-room look at a near-black saturated gradient is NOT drawn here — a shapes frame
        is one rectangle; use a gradient clip with the cube on / off.)

NATIVE = what DLC's raw stage measures: the layers audit runs FIRST (recorded), then calibration.enter
(DesktopLUT snapshots the user's stack) + an identity MHC2 associated (Rec.709 bootstrap in SDR — enter
alone does not clear Windows' last MHC2) + runtime cube cleared + every viewing layer off; the probe
REFUSES unless the audit is then clean; the restore is exit(restore_snapshot=True) + a diff against the
pre-probe snapshot (cube / layers re-set if the restore missed them). Settle is DETECTED per patch (no
fixed settle time — the reads continue until a tail spanning >= 2 s agrees within max(0.5 %, 0.003 nit) +
3·the prior meter σ, no step, no drift); >= 5 kept reads below 1 nit, >= 3 otherwise; spotread-flagged reads
are logged, never used; every read in reads.jsonl. Between patches the probe idles on BLACK (a dim idle
before a 0.054-nit read would contaminate exactly what HW-A measures). A mid-grey 600-px sanity read at the
start refuses on a wrong daemon bit depth / monitor / meter spot.

Session 1 command lines (DLC root; the dogegen daemon in its own terminal — two-shape frames, so the
default Resolve transport works):
    python -m dlc.dogegen_server --mode SDR --bit-depth 8 --monitor 1
    python probe_near_black.py --phase aid  --monitor 1 --mode SDR --bit-depth 8
    python probe_near_black.py --phase plan --monitor 1 --mode SDR --bit-depth 8
    python probe_near_black.py --phase hwA,hwB --monitor 1 --mode SDR --bit-depth 8 --dogegen-server 127.0.0.1:28930
    python probe_near_black.py --phase hwC --through-stack --monitor 1 --mode SDR --bit-depth 8 --dogegen-server 127.0.0.1:28930
    python runs/_watch_events.py runs/probes/<run>/events.jsonl        (consume the check-ins; self-test first)
Cancel: write {"action": "cancel"} to <run>/control.json. Dry run: add --simulate (mock pipe + synthetic panel).

Expected duration (read-time model, i1D3 persistent: ~5-7.5 s per read below 0.4 nit, >= 5 kept reads per
sub-nit patch; ``--phase plan`` prints it): hwA ≈ 8 min (13 patches incl. the end repeats, all sub-nit) ·
hwB ≈ 25 min (62 patches, ~51 sub-nit) · hwC ≈ 40 min (2 × 42 patches, ~82 sub-nit, + 4 spot reads) ≈ 73 min
in all — the charter's "~35 min for HW-A/B/C" is short by ~40 min; trim with --hwb-codes / --hwc-codes /
--hwc-colours / --hwa-holes 600 if the session needs it.

Decisions the outputs drive: hwA → H1 vs H2 (which black the SDR target / dark floor should use, or a
pedestal-per-drive model); hwB → the teardrop below code 29 and the pedestal STEP at grey 1→3 (input to the
low-priority SDR level-edge / black-relative target ticket, with teardrop_fit.py / reach_floor.py); hwC → is
the shell-1 crush real on the panel (cube-on dim saturated ≈ black while identity is not) → the per-node
near-black knee ticket.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import probe_hw_common as hc
from dlc.stages import _common

PROBE = "near_black"
DIAGONAL_IN = 27.0                         # BenQ PD2700U
TEARDROP_FIT = hc.ROOT / "results" / "_replays" / "2026-10-01_lcd_black_pedestal" / "teardrop_fit.py"
HWB_CODES = (2, 4, 6, 8, 12, 16, 20, 24, 28, 32, 36, 40)
HWB_GREYS = (1, 2, 3, 4, 6, 8)
HWB_ANCHORS = (64, 128, 255)
HWB_MIX_CODES = (8, 16, 32)
HWC_CODES = (4, 8, 12, 16, 24, 32)
HWC_COLOURS = {"R": (1, 0, 0), "G": (0, 1, 0), "B": (0, 0, 1), "C": (0, 1, 1), "M": (1, 0, 1), "Y": (1, 1, 0)}
# run 20260926_225451 (teardrop_fit.py, memory lcd-black-pedestal-teardrop) — frozen before any hwA read
EXPECT = {"full_black_read_nits": 0.054, "full_black_xy": [0.276, 0.285], "fitted_pedestal_nits": 0.092,
          "fitted_pedestal_xy": [0.260, 0.260], "grey1_full_nits": 0.0595, "grey3_full_nits": 0.126,
          "source": "run 20260926_225451 raw.ti3 (teardrop_fit.py, 2026-10-01)"}


def s8(code: int, bit_depth: int) -> int:
    """An 8-bit code at the probe's bit depth."""
    return int(round(int(code) * hc.max_code(bit_depth) / 255.0))


def geometry(args, width: int, height: int) -> hc.Geometry:
    meter = hc.parse_xy(args.meter) or (width // 2, height // 2)
    return hc.Geometry(width, height, meter, diagonal_in=float(args.diagonal_in))


def check_hole(g: hc.Geometry, hole: float) -> dict:
    lit_frac = 1.0 - min(hole, g.width) * min(hole, g.height) / float(g.width * g.height)
    gap = hole / 2.0
    if gap < hc.KEEPOUT_PX:
        raise hc.Refusal(f"--hole-px {hole}: lit content would sit {gap:.0f} px from the meter (< {hc.KEEPOUT_PX})")
    if lit_frac < 0.40:
        raise hc.Refusal(f"--hole-px {hole}: the lit surround covers only {lit_frac:.0%} of the frame (< 40 %)")
    return {"hole_px": hole, "lit_fraction": round(lit_frac, 4), "keepout_px": gap}


def plan_hwA(args, g: hc.Geometry) -> list[hc.Patch]:
    bd = int(args.bit_depth)
    sur = hc.grey(s8(args.surround_code, bd))
    holes = [float(h) for h in str(args.hwa_holes).split(",") if h.strip()]
    for h in holes:
        check_hole(g, h)
    black_hole = holes[0]
    pats: list[hc.Patch] = []
    for c8 in (0, 1, 3):
        c = hc.grey(s8(c8, bd))
        pats.append(hc.Patch(f"A:full:g{c8}", "hwA", hc.full(c), c, cond="full", meta={"code8": c8, "surround": "full"}))
        for h in holes:
            pats.append(hc.Patch(f"A:lit{h:.0f}:g{c8}", "hwA", hc.framed(g, sur, c, g.centred(h, h)), c,
                                 cond=f"lit{h:.0f}", meta={"code8": c8, "surround": "lit", **check_hole(g, h),
                                                           "surround_code": list(sur)}))
        if c8:
            pats.append(hc.Patch(f"A:black{black_hole:.0f}:g{c8}", "hwA", hc.framed(g, (0, 0, 0), c, g.centred(black_hole, black_hole)),
                                 c, cond=f"black{black_hole:.0f}", meta={"code8": c8, "surround": "black", "window_px": black_hole}))
    z = hc.grey(0)
    pats.append(hc.Patch("A:full:g0_end", "hwA", hc.full(z), z, cond="full", meta={"code8": 0, "repeat": True}))
    pats.append(hc.Patch(f"A:lit{holes[0]:.0f}:g0_end", "hwA", hc.framed(g, sur, z, g.centred(holes[0], holes[0])), z,
                         cond=f"lit{holes[0]:.0f}", meta={"code8": 0, "repeat": True, **check_hole(g, holes[0])}))
    return pats


def _hwb_frame(args, g, code, sur):
    if args.hwb_surround == "lit":
        h = float(args.hole_px)
        check_hole(g, h)
        return hc.framed(g, sur, code, g.centred(h, h))
    return hc.full(code)


def plan_hwB(args, g: hc.Geometry) -> list[hc.Patch]:
    bd = int(args.bit_depth)
    sur = hc.grey(s8(args.surround_code, bd))
    cond = "lit" if args.hwb_surround == "lit" else ""
    codes = [int(v) for v in str(args.hwb_codes).split(",") if v.strip()]
    pats: list[hc.Patch] = []

    def add(name, code8, group):
        code = tuple(s8(c, bd) for c in code8)
        pats.append(hc.Patch(name, "hwB", _hwb_frame(args, g, code, sur), code, group=group, cond=cond,
                             meta={"code8": list(code8)}))

    add("B:black", (0, 0, 0), "black")
    for c in HWB_GREYS:
        add(f"B:grey{c}", (c, c, c), "grey")
    for c in codes:                                    # channels interleaved per code: drift is shared
        for k, ch in enumerate("RGB"):
            v = [0, 0, 0]
            v[k] = c
            add(f"B:{ch}{c}", tuple(v), f"ramp_{ch}")
    for c in HWB_ANCHORS:
        for k, ch in enumerate("RGB"):
            v = [0, 0, 0]
            v[k] = c
            add(f"B:{ch}{c}", tuple(v), f"anchor_{ch}")
    for c in HWB_MIX_CODES:
        for name, v in (("RG", (c, c, 0)), ("GB", (0, c, c)), ("RB", (c, 0, c))):
            add(f"B:{name}{c}", v, "mix")
    add("B:black_end", (0, 0, 0), "black")
    return pats


def plan_hwC_block(args, colour: str, state: str, g: Optional[hc.Geometry] = None) -> list[hc.Patch]:
    """One colour block of HW-C. ``--hwc-surround lit`` (DEFAULT, 2026-10-01): every patch is a ``--hole-px``
    window in the lit dim-grey surround. HW-A found a black-frame backlight dip (frames whose max is ≲ code 2 read
    ×1/1.67): with the cube ON the crushed shell-1 nodes output ~1.3 codes, so a FULL-FIELD read would drop the
    panel into the dipped state exactly on the reads that test the crush (identity keeps them in the content
    state) and inflate the cube-vs-identity difference by the dip. With lit content on screen the backlight never
    dips — the real-use condition. ``full`` keeps the old full-field reads."""
    bd = int(args.bit_depth)
    codes = [int(v) for v in str(args.hwc_codes).split(",") if v.strip()]
    lit = getattr(args, "hwc_surround", "lit") == "lit"
    if lit and g is None:
        raise ValueError("--hwc-surround lit needs the geometry")
    sur = hc.grey(s8(args.surround_code, bd))
    meta0 = {"colour": colour, "surround": "lit" if lit else "full"}
    if lit:
        meta0.update(check_hole(g, float(args.hole_px)))

    def frame(code):
        return hc.framed(g, sur, code, g.centred(float(args.hole_px), float(args.hole_px))) if lit else hc.full(code)
    pats = [hc.Patch(f"C:{colour}:black", "hwC", frame((0, 0, 0)), (0, 0, 0), group=colour, cond=state,
                     meta={**meta0, "code8": 0})]
    for c in codes:
        code = tuple(s8(c * m, bd) for m in HWC_COLOURS[colour])
        pats.append(hc.Patch(f"C:{colour}{c}", "hwC", frame(code), code, group=colour, cond=state,
                             meta={**meta0, "code8": c}))
    return pats


def spot_patches(args, tag: str) -> list[hc.Patch]:
    bd = int(args.bit_depth)
    mx = hc.max_code(bd)
    w, m = hc.grey(mx), hc.grey(int(round(mx / 2)))
    return [hc.Patch(f"spot:white:{tag}", "hwC", hc.full(w), w, cond=f"spot_{tag}"),
            hc.Patch(f"spot:midgrey:{tag}", "hwC", hc.full(m), m, cond=f"spot_{tag}")]


# ----------------------------------------------------------------------------- simulate helpers
def sim_seed(s: hc.ProbeSession) -> None:
    """--simulate: give the mock a production stack (applied MHC, a cube, a viewing layer on) so the
    audit / native entry / restore / cube-swap paths all have something real to do."""
    from dlc.simulation import write_identity_cube
    ctl = s.controller
    if (ctl.state().get("mhc") or {}).get(s.key):
        return
    ctl.set_primaries(s.monitor, s.mode, {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctl.set_white(s.monitor, s.mode, 0.3127, 0.3290)
    ctl.apply_mhc(s.monitor, s.mode)
    cube = write_identity_cube(s.root / "sim_production.cube", size=17, title="sim production cube")
    ctl.set_3dlut(s.monitor, s.mode, str(cube))
    ctl.set_layers(s.monitor, s.mode, white_balance=True)


def sim_panel(s: hc.ProbeSession, g: hc.Geometry) -> hc.SimPanel:
    def stack():
        cube = ((s.controller.state().get("runtime") or {}).get(s.key) or {}).get("cube_path") or ""
        return {"cube": None if not cube else ("identity" if "identity" in Path(cube).name.lower() else "production")}
    return hc.SimPanel("lcd", s.mode, int(s.args.bit_depth), g, white_nits=107.0, stack=stack)


# ----------------------------------------------------------------------------- analysis (evidence, no verdict)
def summarise_hwA(results) -> dict:
    by = {r.patch.name: r for r in results}

    def y(name):
        r = by.get(name)
        return r.y if r else None
    out = {"rows": {r.patch.name: {"y": r.y, "sd_y": r.sd_y, "n": r.n_kept, "settled": r.settled} for r in results}}
    full0 = [v for v in (y("A:full:g0"), y("A:full:g0_end")) if v is not None]
    lit_keys = sorted({r.patch.cond for r in results if r.patch.cond.startswith("lit")})
    for cond in lit_keys:
        lit0 = [v for v in (y(f"A:{cond}:g0"), y(f"A:{cond}:g0_end")) if v is not None]
        if full0 and lit0:
            f, l_ = sum(full0) / len(full0), sum(lit0) / len(lit0)
            near = "H1 (black-frame dip)" if abs(l_ - EXPECT["fitted_pedestal_nits"]) < abs(l_ - EXPECT["full_black_read_nits"]) \
                else "H2 (glow only with drive)"
            out[cond] = {"full_black_nits": f, "lit_surround_black_nits": l_, "ratio_lit_over_full": l_ / f if f else None,
                         "nearest_hypothesis_by_level": near}
    out["note"] = "evidence for the LLM: compare the ratio with 1.70 (H1) vs 1.00 (H2) given the reads' sd; no verdict here"
    return out


def run_teardrop_fit(s: hc.ProbeSession, ti3: str) -> None:
    if not TEARDROP_FIT.exists():
        s.evidence["warnings"].append(f"teardrop_fit.py not found at {TEARDROP_FIT}; skipped")
        return
    try:
        cp = subprocess.run([sys.executable, str(TEARDROP_FIT), ti3], capture_output=True, text=True, timeout=300)
        out = Path(ti3).parent / "teardrop_fit.txt"
        out.write_text(cp.stdout + ("\n[stderr]\n" + cp.stderr if cp.stderr.strip() else ""), encoding="utf-8")
        s.evidence.setdefault("analysis", {})["teardrop_fit"] = {"returncode": cp.returncode, "output": str(out)}
        s.log(f"[hwB] teardrop_fit.py -> {out} (rc {cp.returncode})")
    except Exception as exc:  # noqa: BLE001 - analysis only
        s.evidence["warnings"].append(f"teardrop_fit.py failed: {type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------------- phases
def do_native(s: hc.ProbeSession, phases: list[str], g: hc.Geometry) -> None:
    bd = int(s.args.bit_depth)
    if "hwA" in phases:
        pats = plan_hwA(s.args, g)
        d = s.root / "hwA"
        d.mkdir(parents=True, exist_ok=True)
        hc.atomic_write_text(d / "expectations.json", json.dumps({"frozen_before_first_read": True, **EXPECT,
            "H1": "black-frame backlight dip: lit-surround 0,0,0 ≈ fitted pedestal (×1.7 the full-black read)",
            "H2": "glow only with drive: lit-surround 0,0,0 ≈ the full-black read", "plan": [p.as_dict(g) for p in pats]},
            indent=1))
        s.event("phase", phase="hwA", patches=len(pats))
        res = s.run_patches("hwA", pats)
        hc.phase_outputs(s, "hwA", res, bit_depth=bd, title="probe_near_black HW-A (native)",
                         extra={"summary": summarise_hwA(res)},
                         notes=["RGB = the code under the meter; the surround is in hwA.json (cond)"])
    if "hwB" in phases and not s.evidence.get("cancelled"):
        pats = plan_hwB(s.args, g)
        s.event("phase", phase="hwB", patches=len(pats))
        res = s.run_patches("hwB", pats)
        body = hc.phase_outputs(s, "hwB", res, bit_depth=bd, title="probe_near_black HW-B (native)",
                                notes=["teardrop_fit.py / reach_floor.py input: pure ramps + full-drive anchors + 0,0,0"])
        ti3 = next(iter(body["ti3"].values()), None)
        if ti3 and not s.args.no_analysis:
            run_teardrop_fit(s, ti3)


def set_cube(s: hc.ProbeSession, path: str) -> None:
    s.controller.set_3dlut(s.monitor, s.mode, path)
    got = ((s.controller.state().get("runtime") or {}).get(s.key) or {}).get("cube_path")
    if not hc.same_path(got, path):
        raise RuntimeError(f"cube readback mismatch: asked {path}, state.get says {got}")
    if s.presenter is not None:
        s.presenter.invalidate()                     # a DesktopLUT setting change is not a new frame
    s.clock.sleep(1.0)
    s.event("cube_state", tier="stream", cube=path)


def do_hwC(s: hc.ProbeSession, g: hc.Geometry) -> None:
    from dlc.simulation import write_identity_cube
    bd = int(s.args.bit_depth)
    applied = (s.pre_state.get(s.key) or {}).get("cube_path")
    if not applied:
        raise hc.Refusal(f"HW-C compares the APPLIED cube with identity, but no runtime cube is loaded for {s.key}")
    size = hc.lut3d_size(Path(applied)) or 33
    ident = write_identity_cube(s.root / "hwC" / f"identity_{size}.cube", size=size, title=f"DLC probe identity {size}")
    sha_before = hc.sha256_file(Path(applied))
    rec = {"applied_cube": applied, "applied_sha256": sha_before, "identity_cube": str(ident), "size": size}
    s.evidence["hwC"] = rec
    colours = [c for c in str(s.args.hwc_colours).split(",") if c.strip()]
    results: list = []
    try:
        s.run_patches("hwC", spot_patches(s.args, "before"), results=results)
        for k, colour in enumerate(colours):
            order = (("cube", applied), ("identity", str(ident)))
            for state, path in (order if k % 2 == 0 else order[::-1]):
                if s.cancel_requested():
                    break
                set_cube(s, path)
                s.run_patches("hwC", plan_hwC_block(s.args, colour, state, s.geometry), state=state, results=results)
            if s.evidence.get("cancelled"):
                break
    finally:
        try:
            set_cube(s, applied)
            rec["restored_readback"] = True
        except Exception as exc:  # noqa: BLE001
            rec["restored_readback"] = False
            rec["restore_error"] = f"{type(exc).__name__}: {exc}"
            s.anomaly("cube_not_restored", applied=applied, error=rec["restore_error"],
                      note="put the applied cube back by hand (DesktopLUT: Set 3D LUT) — the path is in evidence.json")
        rec["sha256_unchanged"] = hc.sha256_file(Path(applied)) == sha_before
        if s.read_fn is not None and not s.evidence.get("cancelled"):
            s.run_patches("hwC", spot_patches(s.args, "after"), results=results)
    spots = {r.patch.name: r.y for r in results if r.patch.cond.startswith("spot_")}
    table = {}
    for r in results:
        m = r.patch.meta
        if "colour" in m and r.state:
            table.setdefault(f"{m['colour']}{m['code8']}", {})[r.state] = {"y": r.y, "xy": r.as_dict()["xy"], "sd_y": r.sd_y}
    hc.phase_outputs(s, "hwC", results, bit_depth=bd, title="probe_near_black HW-C (THROUGH the applied stack)",
                     extra={"cube": rec, "spots": spots, "cube_vs_identity": table,
                            "through_stack": s.evidence.get("through_stack")},
                     notes=["through the APPLIED MHC; cond = cube | identity | spot_before | spot_after"])


def main(argv=None) -> int:
    p = _common.base_parser("BenQ near-black probes HW-A / HW-B / HW-C (charter Session 1)")
    hc.add_common_args(p, mode="SDR", bit_depth=8, monitor=1)
    # near-black reads idle on BLACK between patches (a dim idle frame before a 0.054-nit read is exactly the
    # contamination HW-A must not have); the start / end park is black anyway
    p.set_defaults(idle_between="black")
    p.add_argument("--phase", required=True, help="aid | plan | hwA | hwB | hwA,hwB | hwC")
    p.add_argument("--through-stack", action="store_true", dest="through_stack",
                   help="owner-approved exception (2026-10-01) — required for hwC")
    p.add_argument("--diagonal-in", type=float, default=DIAGONAL_IN, dest="diagonal_in")
    p.add_argument("--surround-code", type=int, default=64, dest="surround_code", help="lit surround, 8-bit code")
    p.add_argument("--white-nits", type=float, default=107.0, dest="white_nits",
                   help="the panel's white for the transport-check expectation (BenQ SDR ≈ 107)")
    p.add_argument("--hole-px", type=float, default=600.0, dest="hole_px")
    p.add_argument("--hwa-holes", default="600,1200", dest="hwa_holes")
    p.add_argument("--hwb-codes", default=",".join(map(str, HWB_CODES)), dest="hwb_codes")
    p.add_argument("--hwb-surround", choices=("full", "lit"), default="full", dest="hwb_surround")
    p.add_argument("--hwc-codes", default=",".join(map(str, HWC_CODES)), dest="hwc_codes")
    p.add_argument("--hwc-surround", choices=("lit", "full"), default="lit", dest="hwc_surround",
                   help="HW-C patch presentation: lit (default) = --hole-px windows in the lit surround, so the "
                        "black-frame backlight dip never confounds the crushed cube-on reads; full = full field")
    p.add_argument("--hwc-colours", default=",".join(HWC_COLOURS), dest="hwc_colours")
    p.add_argument("--no-analysis", action="store_true", dest="no_analysis", help="skip teardrop_fit.py after hwB")
    args = p.parse_args(argv)
    phases = [x.strip() for x in str(args.phase).split(",") if x.strip()]
    if not set(phases) <= {"aid", "plan", "hwA", "hwB", "hwC"}:
        p.error(f"unknown phase in {phases}")
    if "hwC" in phases and len(phases) > 1:
        p.error("hwC runs alone (it measures THROUGH the stack; hwA/hwB measure native)")
    args.mode = str(args.mode).upper()

    if phases == ["plan"]:
        w, h = hc.parse_xy(args.screen) or (3840, 2160)
        g = geometry(args, w, h)
        panel = hc.SimPanel("lcd", args.mode, args.bit_depth, g, white_nits=107.0)
        tot = hc.print_plan("hwA", plan_hwA(args, g), panel, g)
        tot += hc.print_plan("hwB", plan_hwB(args, g), panel, g)
        cblock = [q for c in str(args.hwc_colours).split(",") for q in plan_hwC_block(args, c, "cube", g)]
        tot += hc.print_plan("hwC (x2 states)", cblock, panel, g, states=2) + 4 * hc.est_patch_s(100.0)
        print(f"== total est {hc.fmt_min(tot)} (+ owner eye check)")
        return 0
    if phases == ["aid"]:
        return do_aid(args)

    s = hc.ProbeSession(args, PROBE)
    status = "ok"
    try:
        s.connect()
        if s.simulate:
            sim_seed(s)
        s.preflight()
        w, h = s.screen()
        g = geometry(args, w, h)
        s.geometry = g
        s.evidence["geometry"] = g.as_dict()
        s.audit("before")                                   # HARD RULE: audit first, recorded
        s.pre_state = s.snapshot()
        s.evidence["pre_state"] = s.pre_state
        if phases == ["hwC"]:
            s.require_through_stack("HW-C (applied cube vs identity cube)")
            if _common.stale_calibration_session(s.controller):
                raise hc.Refusal("DesktopLUT is in calibration mode — HW-C reads the APPLIED stack; exit that session first")
        else:
            s.enter_native()                                # refuses unless clean
        s.idle_code = hc.grey(s8(40, int(args.bit_depth)))  # ≈ 1.8 nit, only with --idle-between dim
        s.open_meter(g, panel=sim_panel(s, g) if s.simulate else None)
        s.park()
        hc.run_transport_check(s, sdr_white_nits=args.white_nits)
        if phases == ["hwC"]:
            do_hwC(s, g)
        else:
            do_native(s, phases, g)
        if s.evidence.get("cancelled"):
            status = "cancelled"
    except hc.Refusal as exc:
        status = f"refused: {exc}"
        s.log(f"REFUSING: {exc}")
    except KeyboardInterrupt:
        status = "interrupted (Ctrl+C)"
        s.log(status)
    except Exception as exc:  # noqa: BLE001
        status = f"error: {type(exc).__name__}: {exc}"
        s.log(status)
    finally:
        s.close()
        if s.controller is not None:
            if phases != ["hwC"]:
                s.restore()
            elif s.pre_state is not None:
                s.evidence["restore"] = s.verify_unchanged(fix=True)
    restore = s.evidence.get("restore") or {}
    note = ("stack verified unchanged vs the pre-probe snapshot" if restore.get("unchanged")
            else "CHECK evidence.json 'restore' — the stack differs from the pre-probe snapshot" if restore else "")
    return hc.finish(s, status, operator_note=note)


def do_aid(args) -> int:
    """Dim body-footprint placement aid (no meter): the i1D3 body as a ~25 % rectangle on a ~6 % field."""
    w, h = hc.parse_xy(args.screen) or (3840, 2160)
    g = geometry(args, w, h)
    bw, bh = g.body_px()
    bd = int(args.bit_depth)
    frame = [(hc.grey(s8(40, bd)), (0.0, 0.0, 1.0, 1.0)), (hc.grey(s8(110, bd)), g.norm(g.centred(bw, bh)))]
    if args.simulate:
        print(json.dumps({"aid": [[list(c), list(r)] for c, r in frame], "meter": list(g.meter)}))
        return 0
    host, _, port = str(args.dogegen_server).partition(":")
    pres = hc.make_hw_presenter(host or "127.0.0.1", int(port or 28930), hc.RealClock(), 0.0)
    try:
        pres.paint(frame)
    finally:
        pres.close()
    print(f"[aid] i1D3 body {bw:.0f}x{bh:.0f} px (dim) centred on {g.meter}; run a measuring phase next "
          "(it parks on black first)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
