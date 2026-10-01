"""PA32UCXR HDR reads THROUGH the applied full stack (MHC2 + 3D LUT + whatever viewing layers are on) —
charter Session 3 §3.4; ledger HANDOFF §0 B5 + B6. OWNER-APPROVED EXCEPTION (2026-10-01) to the
all-layers-off hard rule: every measuring phase needs ``--through-stack``; the layers audit still runs FIRST
and is recorded, and the probe changes NO DesktopLUT state — at the end it reads the state back and asserts
the stack (MHC identity, cube path, layers, FALD params) is exactly the pre-probe one.

Phases (``--phase``, comma list; default ``blue,probec``):
  plan    patch lists + estimated duration (nothing touched).
  aid     the i1D3 body footprint as a DIM rectangle on a dim field at the meter spot (no meter).
  blue    (B5) pure blue at signal 0.90 and 0.95 (PQ codes 921 / 972), full field (``--window-pct``), read
          ``--blue-reps`` (6, >= 5) presentations each, the two signals alternating, every presentation from
          the dim idle frame and ``--blue-gap-s`` (20 s) apart — spread over ~4–5 minutes. Every read is
          logged with its wall time and its time since the frame was painted, and every presentation with the
          pipe's hook / overlay state at that moment, so a TRANSIENT bypass (the 148-nit read) shows as a
          first read off the settled value or one presentation off the others (flagged, evidence only).
  probec  (B6) Probe C on the full stack, read back to back (no idle frame between levels): the
          agent_probe_grey_ramp.py ramp (``--probec-levels`` 60 grey
          levels equally spaced in PQ signal between ``--probec-lo`` 100 and ``--probec-hi`` 1000 nit,
          ASCENDING; ``--probec-desc`` = the order control), a discarded reference read at cv 512 first, and
          its summary (adjacent-level |dx| mean/max, |dy|, sign flips, worst stripe pairs: stripes ≈ |dx| >=
          0.003 alternating; smooth ≈ < 0.001). agent_probe_grey_ramp.py itself changes no state, so it does
          run on the full stack — but it has no audit, no simulate path, parks on the bright cv-512 grey and
          reads once per level; this port keeps its ramp + analysis bit-for-bit and adds the scaffold
          (audit, settle detection, >= 3 reads, park on black, events, .ti3).
  spots   white + mid-grey spot reads of the applied stack (the charter's pre-/post-session spot reads;
          HDR default PQ 769 ≈ 1000 nit + PQ 520 ≈ 100 nit full field, SDR max + 50 % — ``--spot-codes``).

Outputs (run dir ``runs/probes/<ts>_full_stack_spots_hdr_mon0/``): blue/blue.ti3 + blue.json (per
presentation: timing, hook/overlay state, first vs settled read), probec/probec.ti3 + probec.json (with the
Probe C summary), spots/spots.ti3, reads.jsonl (every read), events.jsonl (check-ins), evidence.json (audit,
the through-stack approval, the pre/post stack + the unchanged assertion).

Session 3 §3.4 command lines (DLC root; daemon in its own terminal, HDR 10-bit, full-field patches):
    python -m dlc.dogegen_server --mode HDR --bit-depth 10 --monitor 0
    python probe_full_stack_spots.py --phase plan
    python probe_full_stack_spots.py --phase spots --through-stack --dogegen-server 127.0.0.1:28930   (pre-session)
    python probe_full_stack_spots.py --phase blue,probec --through-stack --dogegen-server 127.0.0.1:28930
    python runs/_watch_events.py runs/probes/<run>/events.jsonl
Dry run: add --simulate. Cancel: {"action": "cancel"} in <run>/control.json.

Expected duration (``--phase plan``): blue ≈ 4 min (12 presentations, mostly the 20-s spacing), probec ≈ 3
min (61 levels, a >= 2-s settled tail each), spots < 1 min — ≈ 7 min in all (charter budget ~15 min).

Decisions the outputs drive: blue → does the DOGEGEN presentation path ever bypass the stack (a presentation
or first read near the frozen 148-nit MHC-only level instead of the held 107, with the hook / overlay state
that went with it)? NOTE: the 148-nit read came from ColourSpace's fullscreen window (suspected direct-scanout
/ independent flip); a clean dogegen result means "the dogegen path is clean", NOT "B5 closed" — B5 closes
only with a repeat in ColourSpace's window (owner), or becomes a presentation-path ticket if either path
shows it. probec → does the full stack still stripe on a grey ramp (the 09-03 saw-tooth was MHC-only) — B6.
"""
from __future__ import annotations

import json
import statistics
import sys
from typing import Any

import probe_hw_common as hc
from dlc._pq import oetf_norm
from dlc.stages import _common

PROBE = "full_stack_spots"
DIAGONAL_IN = 32.0
REF_CV = 512                                    # agent_probe_grey_ramp.py's settle reference (discarded)
# 2026-09-24 ColourSpace check (HANDOFF B5 / the ColourSpace paragraph): pure blue 0.85 / 1.0 / a later 0.898 held
# 107 nit through the stack; 0.90 / 0.95 read 148 nit = the MHC-without-cube level. Frozen here as references.
BLUE_HELD_NITS = 107.0
BLUE_BYPASS_NITS = 148.0


# ----------------------------------------------------------------------------- Probe C port (pure)
def probec_codes(levels: int = 60, lo: float = 100.0, hi: float = 1000.0, *, bit_depth: int = 10, desc: bool = False) -> list[int]:
    """agent_probe_grey_ramp.py's level list: ``levels`` grey codes equally spaced in PQ signal between
    ``lo`` and ``hi`` nits (deduplicated, ascending unless ``desc``)."""
    s_lo, s_hi = oetf_norm(lo / 10000.0), oetf_norm(hi / 10000.0)
    mx = hc.max_code(bit_depth)
    cvs = sorted({int(round((s_lo + (s_hi - s_lo) * i / (levels - 1)) * mx)) for i in range(levels)})
    return cvs[::-1] if desc else cvs


def probec_summary(rows: list[dict]) -> dict[str, Any]:
    """agent_probe_grey_ramp.py's [summary] on rows {cv, Y, x, y}: adjacent-level |dx| / |dy|, sign flips,
    worst stripe pairs (by |dx|)."""
    by = sorted(rows, key=lambda r: r["cv"])
    if len(by) < 4:
        return {"levels": len(by)}
    dx = [by[i + 1]["x"] - by[i]["x"] for i in range(len(by) - 1)]
    dy = [by[i + 1]["y"] - by[i]["y"] for i in range(len(by) - 1)]
    flips = sum(1 for i in range(len(dx) - 1) if dx[i] * dx[i + 1] < 0)
    worst = sorted(range(len(dx)), key=lambda i: -abs(dx[i]))[:6]
    return {"levels": len(by), "adj_dx_mean": statistics.mean(abs(d) for d in dx), "adj_dx_max": max(abs(d) for d in dx),
            "adj_dy_mean": statistics.mean(abs(d) for d in dy), "sign_flips": flips, "of": len(dx) - 1,
            "worst_pairs": [{"cv": [by[i]["cv"], by[i + 1]["cv"]], "Y": [round(by[i]["Y"], 2), round(by[i + 1]["Y"], 2)],
                             "dx": round(dx[i], 5), "dy": round(dy[i], 5)} for i in worst],
            "reading": "stripes ≈ |dx| >= 0.003 with alternating sign; a smooth ramp ≈ < 0.001"}


# ----------------------------------------------------------------------------- patch lists
def blue_code(signal: float, bit_depth: int) -> int:
    return int(round(float(signal) * hc.max_code(bit_depth)))


def plan_blue(args, g: hc.Geometry) -> list[hc.Patch]:
    bd = int(args.bit_depth)
    sigs = [float(v) for v in str(args.blue_signals).split(",") if v.strip()]
    win = g.window_pct(args.window_pct)
    pats = []
    for rep in range(int(args.blue_reps)):
        for sig in (sigs if rep % 2 == 0 else sigs[::-1]):
            code = (0, 0, blue_code(sig, bd))
            shapes = hc.full(code) if args.window_pct >= 100 else hc.framed(g, (0, 0, 0), code, win)
            pats.append(hc.Patch(f"blue{sig:.2f}:rep{rep + 1}", "blue", shapes, code, group=f"blue{sig:.2f}",
                                 meta={"signal": sig, "rep": rep + 1}))
    return pats


def plan_probec(args, g: hc.Geometry) -> list[hc.Patch]:
    bd = int(args.bit_depth)
    win = g.window_pct(args.window_pct)

    def pat(name, cv, **meta):
        c = hc.grey(cv)
        shapes = hc.full(c) if args.window_pct >= 100 else hc.framed(g, (0, 0, 0), c, win)
        return hc.Patch(name, "probec", shapes, c, meta={"cv": cv, **meta})
    ref = int(round(REF_CV * hc.max_code(bd) / 1023))
    return [pat("C:ref512", ref, reference=True, discarded=True)] + [
        pat(f"C:g{cv}", cv) for cv in probec_codes(args.probec_levels, args.probec_lo, args.probec_hi, bit_depth=bd,
                                                 desc=args.probec_desc)]


def plan_spots(args, g: hc.Geometry) -> list[hc.Patch]:
    bd = int(args.bit_depth)
    if args.spot_codes:
        codes = [int(v) for v in str(args.spot_codes).split(",") if v.strip()]
    elif args.mode == "HDR":
        codes = [hc.pq_code(1000, bd), hc.pq_code(100, bd)]
    else:
        codes = [hc.max_code(bd), int(round(hc.max_code(bd) / 2))]
    return [hc.Patch(f"spot:g{c}", "spots", hc.full(hc.grey(c)), hc.grey(c), meta={"code": c}) for c in codes]


# ----------------------------------------------------------------------------- simulate
def sim_seed(s: hc.ProbeSession) -> None:
    from dlc.simulation import write_identity_cube
    ctl = s.controller
    if s.mode == "HDR":
        ctl.set_hdr(s.monitor, True)
    if (ctl.state().get("mhc") or {}).get(s.key):
        return
    ctl.set_primaries(s.monitor, s.mode, {"rx": 0.692, "ry": 0.307, "gx": 0.232, "gy": 0.700, "bx": 0.152, "by": 0.051})
    ctl.set_white(s.monitor, s.mode, 0.3127, 0.3290)
    ctl.apply_mhc(s.monitor, s.mode)
    ctl.set_3dlut(s.monitor, s.mode, str(write_identity_cube(s.root / "sim_production.cube", size=17)))
    ctl.set_layers(s.monitor, s.mode, fald=True)


# ----------------------------------------------------------------------------- phases
def do_blue(s: hc.ProbeSession, g: hc.Geometry) -> None:
    pats = plan_blue(s.args, g)
    results, pres_rows = [], []
    s.event("phase", phase="blue", patches=len(pats))
    for i, p in enumerate(pats):
        if s.cancel_requested():
            s.evidence["cancelled"] = True
            break
        try:
            st = s.controller.state() or {}
            pipe = {"hook": st.get("hook"), "overlay": st.get("overlay")}
        except Exception as exc:  # noqa: BLE001 - evidence only
            pipe = {"error": f"{type(exc).__name__}: {exc}"}
        r = s.measure_patch(p)
        results.append(r)
        ok = [x for x in r.reads if x.get("xyz")]
        first_y = ok[0]["xyz"][1] if ok else None
        near = None
        if r.y:
            near = "held_107" if abs(r.y - BLUE_HELD_NITS) <= abs(r.y - BLUE_BYPASS_NITS) else "bypass_148"
        pres_rows.append({"patch": p.name, "signal": p.meta["signal"], "rep": p.meta["rep"], "t": ok[0]["t"] if ok else None,
                          "nearer_reference": near,
                          "t_s": ok[0]["t_s"] if ok else None, "pipe": pipe, "first_y": first_y, "settled_y": r.y,
                          "settled": r.settled, "n_reads": len(r.reads),
                          "reads": [{"since_paint_s": x["since_paint_s"], "y": x["xyz"][1]} for x in ok]})
        if first_y and r.y and abs(first_y / r.y - 1.0) > 0.05:
            s.anomaly("blue_transient", patch=p.name, first_y=first_y, settled_y=r.y,
                      note="first read > 5 % off the settled value — evidence for the transient-bypass question")
        if s.idle_code is not None:
            s.paint(hc.full(s.idle_code), "idle_dim_blue_gap")   # ALWAYS dim between presentations (never the blue)
        s.progress("blue", i, len(pats), results, p.name)
        if i < len(pats) - 1:
            s.clock.sleep(float(s.args.blue_gap_s))
    per_sig: dict[str, Any] = {}
    for sig in sorted({r["signal"] for r in pres_rows}):
        ys = [r["settled_y"] for r in pres_rows if r["signal"] == sig and r["settled_y"] is not None]
        if ys:
            med = statistics.median(ys)
            per_sig[f"{sig:.2f}"] = {"n": len(ys), "median_y": med, "min_y": min(ys), "max_y": max(ys),
                                     "spread_rel": (max(ys) - min(ys)) / med if med else None,
                                     "max_any_read": max(x["y"] for r in pres_rows if r["signal"] == sig for x in r["reads"])}
            if med and (max(ys) - min(ys)) / med > 0.05:
                s.anomaly("blue_rep_inconsistent", signal=sig, ys=ys,
                          note="presentations disagree by > 5 % — a state-dependent (transient) path?")
    hc.phase_outputs(s, "blue", results, bit_depth=int(s.args.bit_depth), title="probe_full_stack_spots blue (THROUGH the stack)",
                     split_by_cond=False, extra={"presentations": pres_rows, "per_signal": per_sig,
                                                 "references_frozen": {"held_nits": BLUE_HELD_NITS,
                                                                       "mhc_only_bypass_nits": BLUE_BYPASS_NITS,
                                                                       "source": "2026-09-24 ColourSpace check (B5)"},
                                                 "path_note": "dogegen's window, not ColourSpace's fullscreen window: a "
                                                              "clean result here = the dogegen path is clean, NOT B5 closed"})


def do_probec(s: hc.ProbeSession, g: hc.Geometry) -> None:
    pats = plan_probec(s.args, g)
    s.event("phase", phase="probec", patches=len(pats))
    res = s.run_patches("probec", pats, idle=False)       # back to back, ascending — as the original read them
    rows = []
    for r in res:
        if r.patch.meta.get("discarded") or not r.xyz or sum(r.xyz) <= 0:
            continue
        X, Y, Z = r.xyz
        rows.append({"cv": r.patch.meta["cv"], "Y": Y, "x": X / (X + Y + Z), "y": Y / (X + Y + Z)})
    summ = probec_summary(rows)
    s.log(f"[probec] adjacent |dx| mean {summ.get('adj_dx_mean')} max {summ.get('adj_dx_max')} flips "
          f"{summ.get('sign_flips')}/{summ.get('of')}")
    hc.phase_outputs(s, "probec", [r for r in res if not r.patch.meta.get("discarded")], bit_depth=int(s.args.bit_depth),
                     title="probe_full_stack_spots Probe C (THROUGH the stack)", split_by_cond=False,
                     extra={"summary": summ, "rows": rows, "order": "desc" if s.args.probec_desc else "asc",
                            "reference_read": next((r.as_dict() for r in res if r.patch.meta.get("discarded")), None)})


def do_spots(s: hc.ProbeSession, g: hc.Geometry) -> None:
    res = s.run_patches("spots", plan_spots(s.args, g))
    hc.phase_outputs(s, "spots", res, bit_depth=int(s.args.bit_depth), title="probe_full_stack_spots spot reads",
                     split_by_cond=False, extra={"spots": {r.patch.name: {"y": r.y, "xy": r.as_dict()["xy"]} for r in res}})


def main(argv=None) -> int:
    p = _common.base_parser("PA HDR full-stack spot probes (B5 pure blue, B6 Probe C) — owner exception")
    hc.add_common_args(p, mode="HDR", bit_depth=10, monitor=0)
    p.add_argument("--phase", default="blue,probec", help="plan | aid | blue | probec | spots (comma list)")
    p.add_argument("--through-stack", action="store_true", dest="through_stack",
                   help="owner-approved exception (2026-10-01) — required for every measuring phase")
    p.add_argument("--diagonal-in", type=float, default=DIAGONAL_IN, dest="diagonal_in")
    p.add_argument("--window-pct", type=float, default=100.0, dest="window_pct")
    p.add_argument("--blue-signals", default="0.90,0.95", dest="blue_signals")
    p.add_argument("--blue-reps", type=int, default=6, dest="blue_reps")
    p.add_argument("--blue-gap-s", type=float, default=20.0, dest="blue_gap_s")
    p.add_argument("--probec-levels", type=int, default=60, dest="probec_levels")
    p.add_argument("--probec-lo", type=float, default=100.0, dest="probec_lo")
    p.add_argument("--probec-hi", type=float, default=1000.0, dest="probec_hi")
    p.add_argument("--probec-desc", action="store_true", dest="probec_desc")
    p.add_argument("--spot-codes", default=None, dest="spot_codes")
    args = p.parse_args(argv)
    args.mode = str(args.mode).upper()
    phases = [x.strip() for x in str(args.phase).split(",") if x.strip()]
    if not set(phases) <= {"plan", "aid", "blue", "probec", "spots"}:
        p.error(f"unknown phase in {phases}")
    if args.mode != "HDR" and set(phases) & {"blue", "probec"}:
        p.error("blue / probec are PQ (HDR) probes; only spots runs in SDR")
    if args.blue_reps < 5:
        p.error("--blue-reps must be >= 5 (charter: repeat each >= 5 times)")
    w, h = hc.parse_xy(args.screen) or (3840, 2160)

    if phases == ["plan"]:
        g = hc.Geometry(w, h, hc.parse_xy(args.meter) or (w // 2, h // 2), diagonal_in=args.diagonal_in)
        panel = hc.SimPanel("fald", args.mode, int(args.bit_depth), g, ld_on=True)
        tot = hc.print_plan("blue", plan_blue(args, g), panel, g) + (2 * args.blue_reps - 1) * args.blue_gap_s
        tot += hc.print_plan("probec", plan_probec(args, g), panel, g)
        tot += hc.print_plan("spots", plan_spots(args, g), panel, g)
        print(f"== total est {hc.fmt_min(tot)} (blue includes the {args.blue_gap_s:g}-s spacing)")
        return 0
    if phases == ["aid"]:
        g = hc.Geometry(w, h, hc.parse_xy(args.meter) or (w // 2, h // 2), diagonal_in=args.diagonal_in)
        bw, bh = g.body_px()
        bd = int(args.bit_depth)
        lo, hi = (hc.pq_code(8, bd), hc.pq_code(60, bd)) if args.mode == "HDR" else (hc.sdr_code(4, 120, 2.2, bd), hc.sdr_code(30, 120, 2.2, bd))
        frame = [(hc.grey(lo), (0.0, 0.0, 1.0, 1.0)), (hc.grey(hi), g.norm(g.centred(bw, bh)))]
        if args.simulate:
            print(json.dumps({"aid": [[list(c), list(r)] for c, r in frame]}))
            return 0
        host, _, port = str(args.dogegen_server).partition(":")
        pres = hc.make_hw_presenter(host or "127.0.0.1", int(port or 28930), hc.RealClock(), 0.0)
        try:
            pres.paint(frame)
        finally:
            pres.close()
        return 0

    s = hc.ProbeSession(args, PROBE)
    status = "ok"
    try:
        s.connect()
        if s.simulate:
            sim_seed(s)
        s.preflight()
        w, h = s.screen()
        g = hc.Geometry(w, h, hc.parse_xy(args.meter) or (w // 2, h // 2), diagonal_in=args.diagonal_in)
        s.geometry = g
        s.evidence["geometry"] = g.as_dict()
        s.audit("before")                                    # HARD RULE: audit first, recorded
        s.pre_state = s.snapshot()
        s.evidence["pre_state"] = s.pre_state
        s.require_through_stack("full-stack spot reads (pure blue 0.90/0.95, Probe C, spots)")
        if _common.stale_calibration_session(s.controller):
            raise hc.Refusal("DesktopLUT is in calibration mode — these reads must see the APPLIED stack; exit that session first")
        bd = int(args.bit_depth)
        s.idle_code = hc.grey(hc.pq_code(5, bd) if s.mode == "HDR" else hc.sdr_code(5, 120, 2.2, bd))
        s.open_meter(g, panel=hc.SimPanel("fald", s.mode, bd, g, ld_on=True) if s.simulate else None)
        s.park()
        hc.run_transport_check(s)
        for ph in phases:
            if s.evidence.get("cancelled"):
                break
            {"blue": do_blue, "probec": do_probec, "spots": do_spots}[ph](s, g)
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
        if s.controller is not None and s.pre_state is not None:
            chk = s.verify_unchanged(fix=False)            # the probe changes nothing: ASSERT, never "fix"
            s.evidence["stack_unchanged"] = chk
            if not chk["unchanged"] and status == "ok":
                status = "error: the DesktopLUT stack changed during the probe (see evidence.json stack_unchanged)"
    unchanged = (s.evidence.get("stack_unchanged") or {}).get("unchanged")
    note = ("stack asserted unchanged (state readback)" if unchanged else
            "the DesktopLUT stack is NOT the pre-probe one — see evidence.json" if unchanged is False else "")
    return hc.finish(s, status, operator_note=note)


if __name__ == "__main__":
    sys.exit(main())
