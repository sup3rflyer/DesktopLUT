"""HW-10 — the MHC correction-grayscale pipe round trip (charter Session 1 §1.0; HANDOFF §0 D2 / fable
Phase 9 T3). PIPE ONLY: no meter, no dogegen, nothing painted.

What it proves on the 2026-09-28 build: ``state.get`` → ``mhc[<mon>:<MODE>].correction_grayscale`` (the
decomposition DesktopLUT STORES — points already carry the luminance / main-slider scale, deviations the
per-channel balance, ``enabled`` = the same C++ bool as ``layers[key].grayscale``) can be handed back
VERBATIM through ``controller.set_correction_grayscale_raw`` and restores the curve EXACTLY — including a
luminance component — and that a switched-off curve stays off once ``enabled`` is put back with
``layers.set`` (``ApplyGrayscalePayload`` forces it true).

Per case: read the block (the case's original) → apply a small KNOWN touch (``--touch`` 0.002 added to the
red deviation at the middle slot) with ``set_correction_grayscale_raw`` → read back and check that exactly
that slot moved by the touch → revert along DLC's PRODUCTION revert path (calibrate.py's grayscale-wb
restore): the saved block verbatim + ``layers.set grayscale=<saved enabled>`` + ``mhc.apply`` → read back and
assert BIT-EXACT equality with the case's original (point_count, points, deviations, luminance when
present, enabled; plus layers[key].grayscale and the MHC's ``active_perm``). Cases: the original as found;
with ``--also-disabled`` also the other ``enabled`` state (switched with layers.set first). Whatever
happens, the ORIGINAL (block + enabled) is restored the same way in ``finally`` and compared bit-exactly.

Why the ``mhc.apply``: in the C++ ``mhc.set_correction_grayscale`` (DoMhcSetGrayscale) only STAGES the curve
— no re-bake, no SaveSettings — and ``layers.set`` re-bakes / saves only when the enabled bit CHANGES; so a
raw write + an unchanged enabled bit would read back exact over state.get while the live ICM and the ini
still held the other curve. ``mhc.apply`` (GenerateAndInstallMhcProfile + SaveSettings) is what DLC's revert
calls, so it is what HW-10 validates (only when the MHC is applied; an unapplied MHC is staging-only and
is recorded as such). The touch itself is only STAGED (never baked or saved): an interruption before the
revert leaves nothing in the ini, and the final restore re-stages + re-bakes the original.

Refuses when: the build has no ``correction_grayscale`` (pre-T3 — the revert would degrade to identity),
no MHC entry exists for the key, or a calibration session is open. Copies DesktopLUT.ini into the run dir
FIRST (the first state-changing step of Session 1). Records HW-11 evidence: ``contract_version``,
``maintenance.verify_mhc`` for the key, and ``query_monitors`` link_bpc / encoding / colour space for every
monitor. Each re-bake (the revert's mhc.apply, the --also-disabled flip) is a brief live change on that
display; ``profile_name`` churns — not a failure.

Session 1 §1.0 command lines (DLC root, DesktopLUT running on the 09-28 build):
    python probe_hw10_grayscale_roundtrip.py --monitor 1 --mode SDR --also-disabled
    python probe_hw10_grayscale_roundtrip.py --monitor 0 --mode SDR              (optional: the PA's SDR curve)
Dry run: add --simulate (the mock seeds a luminance-scaled curve). Output: runs/probes/<ts>_hw10_.../
hw10.json + events.jsonl + evidence.json + the DesktopLUT.ini copy. Expected duration: < 1 min (each mhc.apply /
enabled flip re-bakes the ICM, a second or two each).

Decision it drives: HW-10 passes ⇒ the Design-B grayscale-wb revert (restore the user's PRIOR correction,
incl. the main slider and a switched-off curve) is trustworthy on hardware; a mismatch ⇒ the revert must
not be relied on (clear-to-identity semantics) and the C++ HandleStateGet / ApplyGrayscalePayload pair
needs a ticket.
"""
from __future__ import annotations

import copy
import json
import sys
from typing import Any, Optional

import probe_hw_common as hc
from dlc.controller import CalibrationController
from dlc.stages import _common

PROBE = "hw10_grayscale_roundtrip"
COMPARE_KEYS = ("point_count", "points", "deviations", "luminance", "enabled")


# ----------------------------------------------------------------------------- pure helpers
def block_of(state: dict, key: str) -> Optional[dict]:
    """The ``correction_grayscale`` block of ``key`` (None when the mhc entry or the field is absent)."""
    entry = (state.get("mhc") or {}).get(key)
    if not isinstance(entry, dict):
        return None
    cg = entry.get("correction_grayscale")
    return copy.deepcopy(cg) if isinstance(cg, dict) else None


def compare_blocks(a: dict, b: dict) -> dict[str, Any]:
    """Bit-exact comparison of the round-trip keys: ``{"equal": bool, "diffs": [...], "max_abs": float}``. A key
    absent on BOTH sides is equal (``luminance`` is not on the C++ wire); absent on one side is a diff."""
    diffs, max_abs = [], 0.0
    for k in COMPARE_KEYS:
        va, vb = a.get(k), b.get(k)
        if va is None and vb is None:
            continue
        if k == "deviations":
            for ch in sorted(set((va or {}).keys()) | set((vb or {}).keys())):
                xa, xb = list((va or {}).get(ch) or []), list((vb or {}).get(ch) or [])
                if xa != xb:
                    m = max((abs(p - q) for p, q in zip(xa, xb)), default=float("inf")) if len(xa) == len(xb) else float("inf")
                    max_abs = max(max_abs, m)
                    diffs.append({"key": f"deviations.{ch}", "max_abs": m, "len": [len(xa), len(xb)]})
        elif isinstance(va, list) or isinstance(vb, list):
            xa, xb = list(va or []), list(vb or [])
            if xa != xb:
                m = max((abs(p - q) for p, q in zip(xa, xb)), default=float("inf")) if len(xa) == len(xb) else float("inf")
                max_abs = max(max_abs, m)
                diffs.append({"key": k, "max_abs": m, "len": [len(xa), len(xb)]})
        elif va != vb:
            diffs.append({"key": k, "a": va, "b": vb})
    return {"equal": not diffs, "diffs": diffs, "max_abs": max_abs}


def touched(block: dict, delta: float) -> tuple[dict, dict]:
    """A copy with ``delta`` added to the red deviation at the middle slot, and where it went."""
    out = copy.deepcopy(block)
    devs = out.setdefault("deviations", {})
    r = list(devs.get("r") or [])
    if not r:
        raise ValueError("the block has no red deviations to touch")
    i = len(r) // 2
    r[i] = float(r[i]) + float(delta)
    devs["r"] = r
    return out, {"channel": "r", "index": i, "delta": float(delta)}


def check_touch(before: dict, after: dict, where: dict, tol: float = 1e-6) -> dict[str, Any]:
    """Did exactly the touched slot move by ~delta (float32 storage tolerance), nothing else?"""
    ch, i, d = where["channel"], where["index"], where["delta"]
    rb = list((before.get("deviations") or {}).get(ch) or [])
    ra = list((after.get("deviations") or {}).get(ch) or [])
    moved = (ra[i] - rb[i]) if len(ra) > i and len(rb) > i else None
    rest = compare_blocks({**before, "deviations": {**(before.get("deviations") or {}), ch: rb[:i] + rb[i + 1:]}, "enabled": None},
                          {**after, "deviations": {**(after.get("deviations") or {}), ch: ra[:i] + ra[i + 1:]}, "enabled": None})
    landed = moved is not None and abs(moved - d) <= max(tol, abs(d) * 1e-3)
    return {"moved": moved, "expected": d, "landed": landed, "others_unchanged": rest["equal"], "others": rest["diffs"],
            "enabled_after_write": after.get("enabled")}


def luminance_component(block: dict, is_hdr: bool) -> Optional[float]:
    """max |points − the loader's identity grid| (SDR: (i/(n-1))², HDR: i/(n-1)) — > 0 = the curve carries a
    luminance / main-slider component (the case the old bridge bent)."""
    pts = [float(v) for v in block.get("points") or []]
    n = len(pts)
    if n < 2:
        return None
    grid = [(i / (n - 1)) if is_hdr else (i / (n - 1)) ** 2 for i in range(n)]
    return max(abs(p - q) for p, q in zip(pts, grid))


# ----------------------------------------------------------------------------- the probe
def sim_seed(ctl: CalibrationController, monitor: int, mode: str) -> None:
    """--simulate: an applied MHC with a luminance-scaled, slightly unbalanced correction curve (enabled)."""
    key = f"{monitor}:{mode}"
    if (ctl.state().get("mhc") or {}).get(key):
        return
    ctl.set_primaries(monitor, mode, {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06})
    ctl.set_white(monitor, mode, 0.3127, 0.3290)
    ctl.apply_mhc(monitor, mode)
    n = 20
    grid = [(i / (n - 1)) if mode == "HDR" else (i / (n - 1)) ** 2 for i in range(n)]
    # what the C++ STORES after a main-slider edit: the points carry the luminance scale (1.05), the
    # deviations the balance — sent the way the revert sends it (no luminance / rgb on the wire)
    ctl.set_correction_grayscale_raw(monitor, mode, {
        "points": [1.05 * v for v in grid],
        "deviations": {"r": [1.0 + 0.01 * (i % 3) for i in range(n)], "g": [1.0] * n, "b": [0.99] * n}})
    ctl.set_layers(monitor, mode, grayscale=True)     # the mock keeps the layer flag separate (phase-9 divergence)


def read_block(s: hc.ProbeSession, tag: str) -> tuple[dict, Optional[bool]]:
    st = s.controller.state() or {}
    blk = block_of(st, s.key)
    if blk is None:
        raise RuntimeError(f"{tag}: correction_grayscale vanished from state.get for {s.key}")
    lay = CalibrationController.layers_from_state(st, s.monitor, s.mode)
    s.event("readback", tier="stream", tag=tag, enabled=blk.get("enabled"), layer_grayscale=(lay or {}).get("grayscale"),
            point_count=blk.get("point_count"))
    return blk, (lay or {}).get("grayscale") if lay is not None else None


def write_block(s: hc.ProbeSession, blk: dict, enabled: Optional[bool]) -> dict:
    """DLC's production revert (calibrate.py grayscale-wb restore): the block verbatim, the enabled bit through
    layers.set, then mhc.apply so the live ICM + the ini carry it (set_correction_grayscale only stages)."""
    out: dict[str, Any] = {"raw": s.controller.set_correction_grayscale_raw(s.monitor, s.mode, blk)}
    if enabled is not None:
        out["layers_set"] = s.controller.set_layers(s.monitor, s.mode, grayscale=bool(enabled))
    if s.evidence.get("mhc_applied"):
        out["mhc_apply"] = s.controller.apply_mhc(s.monitor, s.mode)
    else:
        out["mhc_apply"] = "skipped: the MHC is not applied (staging-only round trip)"
    return out


def active_perm(s: hc.ProbeSession) -> Any:
    return (((s.controller.state() or {}).get("mhc") or {}).get(s.key) or {}).get("active_perm")


def run_case(s: hc.ProbeSession, name: str, want_enabled: Optional[bool], touch: float) -> dict[str, Any]:
    case: dict[str, Any] = {"case": name}
    blk, lay = read_block(s, f"{name}:start")
    if want_enabled is not None and bool(blk.get("enabled")) != want_enabled:
        s.controller.set_layers(s.monitor, s.mode, grayscale=want_enabled)
        blk, lay = read_block(s, f"{name}:switched")
    base, base_lay = copy.deepcopy(blk), lay
    base_perm = active_perm(s)
    case.update({"original": base, "layer_grayscale": base_lay, "active_perm": base_perm,
                 "luminance_component": luminance_component(base, s.mode == "HDR")})
    tblk, where = touched(base, touch)
    s.controller.set_correction_grayscale_raw(s.monitor, s.mode, tblk)
    after, _ = read_block(s, f"{name}:touched")
    case["touch"] = check_touch(base, after, where)
    case["revert_replies"] = write_block(s, base, base.get("enabled"))
    back, back_lay = read_block(s, f"{name}:reverted")
    cmp = compare_blocks(base, back)
    case["active_perm_after"] = active_perm(s)
    # layers[key].grayscale is the SAME C++ bool as correction_grayscale.enabled: it must come back too
    case.update({"reverted": back, "revert_compare": cmp, "layer_grayscale_after": back_lay,
                 "layer_matches_enabled": back_lay is None or bool(back_lay) == bool(back.get("enabled")),
                 "pass": bool(cmp["equal"] and case["touch"]["landed"] and case["touch"]["others_unchanged"]
                              and (back_lay is None or base_lay is None or bool(back_lay) == bool(base_lay))
                              and case["active_perm_after"] == base_perm)})
    s.log(f"[{name}] enabled={base.get('enabled')} luminance_component={case['luminance_component']} touch landed="
          f"{case['touch']['landed']} revert exact={cmp['equal']} (max |Δ| {cmp['max_abs']}) -> {'PASS' if case['pass'] else 'FAIL'}")
    s.event("check_in", case=name, passed=case["pass"], revert_exact=cmp["equal"], touch_landed=case["touch"]["landed"],
            diffs=cmp["diffs"][:5])
    return case


def main(argv=None) -> int:
    p = _common.base_parser("HW-10 correction_grayscale pipe round trip (no meter, no dogegen)")
    p.add_argument("--also-disabled", action="store_true", dest="also_disabled",
                   help="also exercise the other `enabled` state of the original (always restored)")
    p.add_argument("--touch", type=float, default=0.002, help="the known touch added to deviations.r[mid]")
    p.add_argument("--tag", default="")
    args = p.parse_args(argv)
    args.mode = str(args.mode).upper()
    s = hc.ProbeSession(args, PROBE, need_meter=False)
    status = "ok"
    original: Optional[dict] = None
    orig_lay: Optional[bool] = None
    cases: list[dict] = []
    try:
        s.connect()
        if s.simulate:
            sim_seed(s.controller, s.monitor, s.mode)
        st = s.controller.state() or {}
        s.evidence["contract_version"] = st.get("contract_version")
        status_cal = s.controller.calibration_status()
        s.evidence["calibration_status"] = status_cal
        if (status_cal or {}).get("active"):
            raise hc.Refusal("a calibration session is open — HW-10 must see the user's settings, not a cleared session")
        if not isinstance((st.get("mhc") or {}).get(s.key), dict):
            raise hc.Refusal(f"no MHC entry for {s.key} in state.get — nothing to round-trip on this display/mode")
        s.evidence["mhc_applied"] = bool(((st.get("mhc") or {}).get(s.key) or {}).get("applied"))
        if block_of(st, s.key) is None:
            raise hc.Refusal("state.get carries no mhc correction_grayscale — a DesktopLUT build before fable Phase 9 T3 "
                             "(e982f24); HW-10 needs the 2026-09-28 build")
        s.backup_ini()                                        # before the first write (charter §1.0 snapshot)
        s.pre_state = s.snapshot()
        s.evidence["pre_state"] = s.pre_state
        try:                                                  # HW-11 evidence (pipe-only, read-only)
            s.evidence["verify_mhc"] = s.controller.verify_mhc(s.monitor, s.mode)
        except Exception as exc:  # noqa: BLE001
            s.evidence["verify_mhc"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            s.evidence["monitors"] = [{k: m.get(k) for k in ("index", "friendly_name", "link_bpc", "link_color_encoding",
                                                             "link_connector", "color_space", "hdr_active")}
                                      for m in (s.controller.query_monitors() or {}).get("monitors") or []]
        except Exception as exc:  # noqa: BLE001
            s.evidence["monitors"] = {"error": f"{type(exc).__name__}: {exc}"}
        original, orig_lay = read_block(s, "original")
        s.evidence["original"] = original
        s.evidence["original_layer_grayscale"] = orig_lay
        if orig_lay is not None and bool(orig_lay) != bool(original.get("enabled")):
            s.anomaly("enabled_vs_layer_inconsistent", enabled=original.get("enabled"), layer_grayscale=orig_lay,
                      note="correction_grayscale.enabled and layers[key].grayscale should be ONE C++ bool — report it")
        cases.append(run_case(s, "as_found", None, args.touch))
        if args.also_disabled:
            cases.append(run_case(s, "enabled_flipped", not bool(original.get("enabled")), args.touch))
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
        final: dict[str, Any] = {}
        if original is not None:
            try:
                now, now_lay = read_block(s, "final:check")
                if (not compare_blocks(original, now)["equal"] or (orig_lay is not None and bool(now_lay) != bool(orig_lay))
                        or active_perm(s) != (s.pre_state.get(s.key) or {}).get("active_perm")):
                    final["rewrite_replies"] = write_block(s, original, original.get("enabled"))
                    final["rewritten"] = True
                now, now_lay = read_block(s, "final")
                final.update({"compare": compare_blocks(original, now), "layer_grayscale": now_lay,
                              "layer_matches": orig_lay is None or bool(now_lay) == bool(orig_lay)})
                final["stack"] = s.verify_unchanged(fix=False)
            except Exception as exc:  # noqa: BLE001
                final["error"] = f"{type(exc).__name__}: {exc}"
            if not (final.get("compare") or {}).get("equal") or not final.get("layer_matches", True):
                s.anomaly("original_not_restored", final=final,
                          note="the ORIGINAL correction grayscale is not back bit-exactly — restore it from the pre-session "
                               "DesktopLUT.ini copy (Session 1 §1.0 snapshot)")
                if status == "ok":
                    status = "error: original not restored bit-exactly"
        s.evidence["final"] = final
        s.evidence["cases"] = cases
    passed = bool(cases) and all(c.get("pass") for c in cases)
    if status == "ok" and not passed:
        status = "fail: round trip not exact (see hw10.json)"
    hc.atomic_write_text(s.root / "hw10.json", json.dumps({"key": s.key, "status": status, "passed": passed, "cases": cases,
                                                            "final": s.evidence.get("final"),
                                                            "contract_version": s.evidence.get("contract_version")},
                                                           indent=1, default=float))
    return hc.finish(s, status, operator_note=("HW-10 PASS — revert is exact" if passed else "HW-10 did NOT pass — see hw10.json"))


if __name__ == "__main__":
    sys.exit(main())
