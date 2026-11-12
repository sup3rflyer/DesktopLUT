"""The refresh probe's VRR rate source: a display that stays in its reference mode with VRR active while a presenter
paces frames at a chosen interval, so the panel refreshes at that rate with no mode switch (and no consent per rate).

**What a VRR sweep can and cannot say.** Each extra rate is nearly free, so the sweep does not hardcode a low / a high /
a middle: it starts from the ends of the VRR range (the floor + a margin, the reference) plus the content rates inside
the range, then adds rates where the response bends (a held-out point the neighbours miss by more than ``k`` sigma),
capped. Its verdict (:func:`vrr_model`):

* **interpolable** — the response is a smooth function of frame period inside the measured span: every interior
  point is predicted by its neighbours (piecewise-linear in period) within ``k`` sigma (+ a 0.1 % floor). The
  residuals, their uncertainties and the chi-square go with it.
* **extrapolable** — default NO. Yes only when one straight line in frame period fits every point (chi-square p >=
  0.01) AND it predicts both END points when they are left out. (The 2026-10-23 fixed-mode data on the owner's panel
  were NOT linear in frame period.)
* **VRR = fixed mode?** — an assumption, not a fact: a panel can run other overdrive / backlight timing / gamma per
  mode. At least one **cross-check** rate is measured both ways (a fixed mode the display offers inside the VRR range,
  bracketed with its own consent). Only when they agree within ``k`` sigma (+ floor) may VRR points become fixed-mode
  trims (``refresh_response.rates`` with ``source: "vrr"``, catalogue content rates only); otherwise the VRR curve is
  recorded as its own thing (``refresh_response.vrr`` — still worth a chart: it is what "VRR gamma flicker" looks
  like) and no fixed-mode trim is derived from it.

**Pre-flight refusals** (:meth:`VrrSource.preflight`): no presenter; a VRR range that cannot be read (EDID range
limits / HDMI Forum VRR / AMD FreeSync blocks, read-only from the registry, or the spec's ``vrr_range``); rates below
the range floor (+ margin) are never planned — the driver would frame-double (LFC) and the panel would run at a
multiple; DesktopLUT's DWM hook injected or its overlay awake (either one keeps a full-screen window from getting a
direct / independent flip, and VRR does not engage on a composed window) — the human switches DWM Hook mode off in
DesktopLUT's settings for the sitting and back on afterwards (``calibration.enter`` leaves the hook in place, and
there is no pipe call to change it). Whether VRR is enabled in the driver and the monitor's OSD cannot be read by
code: the plan seam asks the overseer to have the human confirm it, and the proof below catches a sweep that did not
engage.

**Proof that the panel ran at the requested rate** (:func:`judge_proof`), per rate — a sweep that silently measured at
the fixed rate would read as "no effect":

1. the presenter's own statistics (the contract below): the achieved present interval, the DXGI frame statistics'
   refresh count rate (one refresh per present when VRR engaged; the mode rate when it did not), and the presentation
   mode (independent flip / overlay, not composed);
2. the meter: spotread's ``F`` refresh-rate measurement (``argyll.PersistentSpotread.measure_refresh_rate``, i1D3
   ~0.05 Hz) when a persistent meter is wired (``cal.vrr_meter_refresh``);
3. last resort: the run holds the first rate ``osd_hold_s`` and a check-in asks the overseer to have the human read
   the monitor's refresh counter (the LLM cancels if it is wrong); such points are labelled ``osd_unverified`` and are
   never promoted to fixed-mode trims.

**The presenter contract** (``tools/vrr_tpg/vrr_tpg.exe``, NOT BUILT — the native part is a separate ticket; the probe
runs against :class:`FakeVrrPresenter` until then). One process per sitting, started by the probe, stopped by it (and
by its owner's exit):

* start: ``vrr_tpg.exe --hardware-id AUS322A --bits 10 [--hdr]`` (hidden console: CREATE_NO_WINDOW; normal priority,
  never above). It opens a borderless full-screen window on that display only, a flip-model swap chain
  (``DXGI_SWAP_EFFECT_FLIP_DISCARD``, ``DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING``, R10G10B10A2 with HDR10 ST.2084 colour
  space under ``--hdr``), and presents ``Present(0, DXGI_PRESENT_ALLOW_TEARING)`` paced by a high-resolution waitable
  timer + a spin to the target instant. Line protocol on stdin / stdout, one JSON object per reply line.
* ``hello`` -> ``{"ok", "proto": 1, "output": {"device", "hardware_id", "rect"}, "swapchain": {...}, "vrr_range": [lo,
  hi] | null (EDID / DisplayID, informational), "mode_hz": <the mode's refresh>}``
* ``patch R G B AREA`` -> paints code values (0..2^bits-1) as a centred rectangle of the screen's aspect covering AREA
  % of the screen on black (the same frame as ``dlc.measure_loop.window_shapes``); ``{"ok"}`` after the first present
  of the new frame.
* ``cadence HZ`` -> presents every ``1/HZ`` s from now on (``cadence 0`` = present on every vblank, synced); ``{"ok"}``.
* ``stats [WINDOW_S]`` -> over the last WINDOW_S (default 2 s): ``{"ok", "target_hz", "presents", "achieved_hz"
  (presents / s), "interval_ms": {"mean", "sd", "p01", "p99"}, "refresh_hz" (DXGI GetFrameStatistics SyncRefreshCount
  delta / SyncQPCTime delta), "presentation": "independent_flip" | "overlay" | "composed" | "unknown"
  (IDXGISwapChainMedia::GetFrameStatisticsMedia CompositionMode / PresentMon-equivalent), "missed": <presents later
  than 0.5 ms>}``. Timing: interval sd <= 0.2 ms, mean within 0.05 % of the target; a chunk of work never blocks the
  present thread.
* ``quit`` -> ``{"ok"}``, closes the window, exits 0. EOF on stdin = quit. It must leave nothing on screen and never
  present outside its own window; it must never change a display mode.

Stdlib only (the registry read is Windows-only and read-only).
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import refresh_mode as rm
from . import refresh_trim as rt
from .measure_loop import MeasurePatch, Reading
from .probe_kit import Read, jsonable, stack_view

__all__ = ["parse_vrr_range", "edid_for_device_path", "FakeVrrPresenter", "NativeVrrPresenter",
           "native_presenter_factory", "judge_proof", "next_rates", "vrr_model", "cross_check", "VrrSource",
           "vrr_key", "VRR_PREFIX"]

VRR_PREFIX = "vrr:"
FLOOR_LN = 0.001
TOOL = Path(__file__).resolve().parents[2] / "tools" / "vrr_tpg" / "vrr_tpg.exe"


def vrr_key(hz: float) -> str:
    """The row key of a VRR point (``vrr:47.952``): never the same key as a fixed mode's (``47.952``)."""
    return f"{VRR_PREFIX}{hz:.3f}".rstrip("0").rstrip(".")


# ----------------------------------------------------------------------------- the VRR range (EDID, read-only)
def parse_vrr_range(edid: bytes) -> dict[str, Any]:
    """Every VRR / refresh range the EDID declares: the base block's Display Range Limits (0xFD: the vertical range
    the display accepts — an upper bound for VRR, not proof of it), the CTA-861 HDMI Forum VSDB (VRRmin / VRRmax, HDMI
    2.1 VRR) and the AMD FreeSync VSDB (min / max). ``range`` = the most specific one found (HF-VSDB, then FreeSync,
    then the range limits); ``sources`` keeps them all, so the overseer sees a disagreement."""
    out: dict[str, Any] = {"sources": {}}
    if len(edid) < 128:
        return {**out, "error": "EDID shorter than one block"}
    for off in (54, 72, 90, 108):
        d = edid[off:off + 18]
        if len(d) == 18 and d[0] == 0 and d[1] == 0 and d[3] == 0xFD:
            flags = d[4]
            vmin = d[5] + (255 if flags & 0x01 else 0)
            vmax = d[6] + (255 if flags & 0x02 else 0)
            out["sources"]["range_limits"] = [float(vmin), float(vmax)]
    n_ext = edid[126]
    for b in range(1, n_ext + 1):
        blk = edid[128 * b:128 * (b + 1)]
        if len(blk) < 128 or blk[0] != 0x02:
            continue
        dtd = blk[2] if blk[2] else 4
        i = 4
        while i < min(dtd, 127):
            tag, ln = blk[i] >> 5, blk[i] & 0x1F
            body = blk[i + 1:i + 1 + ln]
            if tag == 3 and ln >= 3:
                oui = (body[2] << 16) | (body[1] << 8) | body[0]
                if oui == 0xC45DD8 and ln >= 10:            # HDMI Forum VSDB: PB10 VRRmin + VRRmax[9:8], PB11 VRRmax
                    vmin = body[9] & 0x3F
                    vmax = ((body[9] & 0xC0) << 2) | (body[10] if ln >= 11 else 0)
                    if vmin and vmax:
                        out["sources"]["hdmi_forum_vrr"] = [float(vmin), float(vmax)]
                elif oui == 0x00001A and ln >= 7:          # AMD FreeSync VSDB: version, caps, min, max
                    if body[5] and body[6]:
                        out["sources"]["amd_freesync"] = [float(body[5]), float(body[6])]
            i += 1 + ln
    for k in ("hdmi_forum_vrr", "amd_freesync", "range_limits"):
        if k in out["sources"]:
            out["range"] = out["sources"][k]
            out["range_source"] = k
            break
    return out


def edid_for_device_path(device_path: str) -> Optional[bytes]:
    """The EDID Windows stored for a monitor device path (``\\\\?\\DISPLAY#AUS322A#5&...&UID4352#{guid}``),
    READ-ONLY from ``HKLM\\SYSTEM\\CurrentControlSet\\Enum\\DISPLAY\\<id>\\<instance>\\Device Parameters\\EDID``."""
    try:
        import winreg
    except ImportError:
        return None
    parts = str(device_path or "").split("#")
    if len(parts) < 3:
        return None
    key = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, "EDID")
            return bytes(val)
    except OSError:
        return None


# ----------------------------------------------------------------------------- presenters (the contract)
class FakeVrrPresenter:
    """The contract, in-process (tests and ``--simulate``). Faults: ``stuck_hz`` (VRR does not engage: the panel
    refreshes at the mode rate whatever the cadence), ``composed`` (the window is composed), ``no_refresh_stats``
    (an older presenter: no DXGI refresh count), ``vrr_range`` (what ``hello`` reports)."""

    simulated = True

    def __init__(self, *, mode_hz: float = 60.0, vrr_range: Optional[Sequence[float]] = (48.0, 60.0),
                 stuck_hz: Optional[float] = None, composed: bool = False, no_refresh_stats: bool = False,
                 hardware_id: str = "SIM0000") -> None:
        self.mode_hz, self.vrr_range = float(mode_hz), (list(vrr_range) if vrr_range else None)
        self.stuck_hz, self.composed, self.no_refresh_stats = stuck_hz, composed, no_refresh_stats
        self.hardware_id = hardware_id
        self.cadence: Optional[float] = None
        self.frame: Optional[tuple] = None
        self.started = False
        self.log: list[str] = []

    def start(self) -> dict[str, Any]:
        self.started = True
        self.log.append("start")
        return self.hello()

    def hello(self) -> dict[str, Any]:
        return {"ok": True, "proto": 1, "output": {"hardware_id": self.hardware_id}, "vrr_range": self.vrr_range,
                "mode_hz": self.mode_hz, "swapchain": {"model": "flip_discard", "tearing": True}}

    def patch(self, rgb: Sequence[int], area: float) -> dict[str, Any]:
        self.frame = (tuple(int(c) for c in rgb), float(area))
        return {"ok": True}

    def set_cadence(self, hz: Optional[float]) -> dict[str, Any]:
        self.cadence = float(hz) if hz else None
        self.log.append(f"cadence {hz}")
        return {"ok": True}

    def refresh_hz(self) -> float:
        """What the panel really refreshes at (the simulator's truth)."""
        if self.stuck_hz is not None or self.composed:
            return float(self.stuck_hz or self.mode_hz)
        return float(self.cadence or self.mode_hz)

    def stats(self, window_s: float = 2.0) -> dict[str, Any]:
        tgt = self.cadence or self.mode_hz
        out = {"ok": True, "target_hz": tgt, "presents": int(tgt * window_s), "achieved_hz": tgt,
               "interval_ms": {"mean": 1000.0 / tgt, "sd": 0.05}, "missed": 0,
               "presentation": "composed" if self.composed else "independent_flip"}
        if not self.no_refresh_stats:
            out["refresh_hz"] = self.refresh_hz()
        return out

    def stop(self) -> None:
        self.started = False
        self.cadence = None
        self.log.append("stop")


class NativeVrrPresenter:
    """The client of ``vrr_tpg.exe`` (the contract above). Never blocks the caller beyond ``timeout``."""

    simulated = False

    def __init__(self, exe: Path, hardware_id: str, *, bits: int = 10, hdr: bool = True, timeout: float = 10.0) -> None:
        self.exe, self.hardware_id, self.bits, self.hdr, self.timeout = Path(exe), hardware_id, bits, hdr, timeout
        self.proc: Optional[subprocess.Popen] = None

    def _call(self, line: str) -> dict[str, Any]:
        if self.proc is None or self.proc.poll() is not None:
            return {"ok": False, "error": "the VRR presenter is not running"}
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            reply = self.proc.stdout.readline()
            return json.loads(reply) if reply.strip() else {"ok": False, "error": "no reply"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def start(self) -> dict[str, Any]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        args = [str(self.exe), "--hardware-id", self.hardware_id, "--bits", str(self.bits)] + (["--hdr"] if self.hdr else [])
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, creationflags=flags)
        return self._call("hello")

    def patch(self, rgb: Sequence[int], area: float) -> dict[str, Any]:
        return self._call(f"patch {int(rgb[0])} {int(rgb[1])} {int(rgb[2])} {float(area):.4f}")

    def set_cadence(self, hz: Optional[float]) -> dict[str, Any]:
        return self._call(f"cadence {float(hz or 0):.6f}")

    def stats(self, window_s: float = 2.0) -> dict[str, Any]:
        return self._call(f"stats {window_s:g}")

    def stop(self) -> None:
        if self.proc is None:
            return
        self._call("quit")
        try:
            self.proc.wait(timeout=self.timeout)
        except Exception:  # noqa: BLE001 - only our own child, by handle
            self.proc.kill()
        self.proc = None


def native_presenter_factory() -> Optional[Callable[[str, int], Any]]:
    """``(hardware_id, bits) -> NativeVrrPresenter`` when the tool is built, else None (the preflight refuses)."""
    if not TOOL.exists():
        return None
    return lambda hw, bits: NativeVrrPresenter(TOOL, hw, bits=bits)


# ----------------------------------------------------------------------------- proof, rates, model (pure)
def judge_proof(target_hz: float, stats: Optional[dict[str, Any]], meter: Optional[dict[str, Any]], *,
                tol_rel: float = 0.003, mode_hz: Optional[float] = None) -> dict[str, Any]:
    """Did the panel run at ``target_hz``? ``ok`` True / False / None (no evidence: the OSD fallback)."""
    tol = max(0.05, tol_rel * target_hz)
    out: dict[str, Any] = {"target_hz": target_hz, "tol_hz": tol, "checks": {}}
    fails = []
    if stats and stats.get("ok"):
        pres = str(stats.get("presentation") or "unknown")
        out["checks"]["presentation"] = pres
        if pres == "composed":
            fails.append("the presenter's window is composed: VRR cannot engage")
        a = stats.get("achieved_hz")
        if a is not None:
            out["checks"]["achieved_hz"] = a
            if abs(float(a) - target_hz) > tol:
                fails.append(f"presents at {a:.3f} Hz, not {target_hz:.3f}")
        r = stats.get("refresh_hz")
        if r is not None:
            out["checks"]["refresh_hz"] = r
            if abs(float(r) - target_hz) > tol:
                fails.append(f"the display refreshes at {float(r):.3f} Hz, not {target_hz:.3f}"
                             + (" (the mode rate: VRR did not engage)" if mode_hz and abs(float(r) - mode_hz) <= tol
                                else ""))
    if meter and meter.get("ok") and meter.get("hz") is not None:
        out["checks"]["meter_hz"] = meter["hz"]
        if abs(float(meter["hz"]) - target_hz) > max(tol, 0.1):
            fails.append(f"the meter measures {float(meter['hz']):.3f} Hz, not {target_hz:.3f}")
    has_display_evidence = "refresh_hz" in out["checks"] or "meter_hz" in out["checks"]
    out["fails"] = fails
    out["ok"] = False if fails else (True if has_display_evidence else None)
    out["kind"] = "+".join(k for k in ("refresh_hz", "meter_hz") if k in out["checks"]) or "presenter_timing_only"
    return out


def _points(comb: dict[str, dict[str, dict[str, Any]]], ref_hz: float) -> dict[str, list[dict[str, Any]]]:
    """Per grey patch: the VRR points (+ the reference at ln 0) sorted by frame period."""
    by_patch: dict[str, list[dict[str, Any]]] = {}
    for rate, pats in comb.items():
        if not rate.startswith(VRR_PREFIX):
            continue
        for p, v in pats.items():
            if v.get("kind") != "grey":
                continue
            by_patch.setdefault(p, []).append({"rate": rate, "hz": float(v["rate_hz"]), "T": 1000.0 / float(v["rate_hz"]),
                                               "ln": float(v["ln_ratio"]), "u": float(v["u"]), "y60": v["y60"]})
    for p, pts in by_patch.items():
        y60 = pts[0]["y60"] if pts else None
        pts.append({"rate": "ref", "hz": ref_hz, "T": 1000.0 / ref_hz, "ln": 0.0, "u": 1e-4, "y60": y60})
        pts.sort(key=lambda d: d["T"])
    return by_patch


def _loo(pts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i in range(1, len(pts) - 1):
        a, m, b = pts[i - 1], pts[i], pts[i + 1]
        w = (m["T"] - a["T"]) / (b["T"] - a["T"])
        pred = a["ln"] + (b["ln"] - a["ln"]) * w
        u = math.sqrt(m["u"] ** 2 + ((1 - w) * a["u"]) ** 2 + (w * b["u"]) ** 2)
        out.append({"rate": m["rate"], "hz": m["hz"], "resid": m["ln"] - pred, "u": u,
                    "z": (m["ln"] - pred) / max(u, 1e-9), "gap_ms": b["T"] - a["T"]})
    return out


def next_rates(comb: dict[str, dict[str, dict[str, Any]]], ref_hz: float, lo_hz: float, *, k: float = 2.0,
               min_gap_ms: float = 0.4, have: Sequence[float] = (), cap: int = 9) -> list[float]:
    """Where to measure next: the midpoints (in frame period) of the gaps around an interior point its neighbours
    miss by more than ``k`` sigma (+ the floor), wider than ``min_gap_ms``. With no interior point yet: the midpoint
    of the whole span. Empty = the curve is resolved (or the cap is reached)."""
    if len(have) >= cap:
        return []
    pts_by = _points(comb, ref_hz)
    if not pts_by:
        return []
    want: set = set()
    for pts in pts_by.values():
        if len(pts) < 3:
            t_mid = 0.5 * (1000.0 / lo_hz + 1000.0 / ref_hz)
            want.add(round(1000.0 / t_mid, 3))
            continue
        for i, r in enumerate(_loo(pts), start=1):
            if abs(r["resid"]) > max(k * r["u"], FLOOR_LN):
                for a, b in ((pts[i - 1], pts[i]), (pts[i], pts[i + 1])):
                    if b["T"] - a["T"] >= 2 * min_gap_ms:
                        want.add(round(1000.0 / (0.5 * (a["T"] + b["T"])), 3))
    have_set = {round(h, 3) for h in have} | {round(ref_hz, 3)}
    new = sorted(h for h in want if all(abs(h - x) > 0.02 for x in have_set))
    return new[:max(0, cap - len(have))]


def _wls_line(xs: Sequence[float], ys: Sequence[float], us: Sequence[float]) -> dict[str, float]:
    """y = s x through the origin (x = T - T_ref), weighted; chi2 and its p-value."""
    sw = sum((x / u) ** 2 for x, u in zip(xs, us))
    if sw <= 0:
        return {"s": 0.0, "u_s": float("inf"), "chi2": 0.0, "dof": 0, "p": 1.0}
    s = sum(x * y / u ** 2 for x, y, u in zip(xs, ys, us)) / sw
    chi2 = sum(((y - s * x) / u) ** 2 for x, y, u in zip(xs, ys, us))
    dof = max(0, len(xs) - 1)
    return {"s": s, "u_s": 1.0 / math.sqrt(sw), "chi2": chi2, "dof": dof, "p": rt.chi2_sf(chi2, dof) if dof else 1.0}


def vrr_model(comb: dict[str, dict[str, dict[str, Any]]], ref_hz: float, *, k: float = 2.0) -> dict[str, Any]:
    """The verdict on the VRR points (see the module docstring)."""
    by = _points(comb, ref_hz)
    levels, all_ok, lin_ok, end_ok = [], True, True, True
    for patch, pts in sorted(by.items(), key=lambda kv: kv[1][0]["y60"] or 0):
        loo = _loo(pts)
        ok = all(abs(r["resid"]) <= max(k * r["u"], FLOOR_LN) for r in loo)
        T0 = 1000.0 / ref_hz
        meas = [p for p in pts if p["rate"] != "ref"]
        line = _wls_line([p["T"] - T0 for p in meas], [p["ln"] for p in meas], [p["u"] for p in meas])
        l_ok = line["dof"] >= 1 and line["p"] >= 0.01
        ends = []
        if len(meas) >= 3:
            for end in (meas[0], meas[-1]):
                rest = [p for p in meas if p is not end]
                f = _wls_line([p["T"] - T0 for p in rest], [p["ln"] for p in rest], [p["u"] for p in rest])
                pred = f["s"] * (end["T"] - T0)
                u = math.hypot(end["u"], f["u_s"] * abs(end["T"] - T0))
                ends.append({"hz": end["hz"], "resid": end["ln"] - pred, "u": u,
                             "ok": abs(end["ln"] - pred) <= max(k * u, FLOOR_LN)})
        e_ok = bool(ends) and all(e["ok"] for e in ends)
        all_ok &= ok and len(loo) >= 1
        lin_ok &= l_ok
        end_ok &= e_ok
        levels.append({"patch": patch, "y60_nits": pts[0]["y60"], "points": [[p["hz"], p["ln"], p["u"]] for p in pts],
                       "loo": loo, "smooth": ok, "linear_in_period": line, "linear_ok": l_ok, "end_tests": ends})
    hz = sorted({p["hz"] for pts in by.values() for p in pts})
    return {"kind": "piecewise linear in frame period between measured rates",
            "interpolable": bool(levels) and all_ok, "extrapolable": bool(levels) and lin_ok and end_ok,
            "linear_in_period": bool(levels) and lin_ok, "span_hz": [hz[0], hz[-1]] if hz else None,
            "k": k, "floor_ln": FLOOR_LN, "levels": levels,
            "verdict": ("smooth: interpolate inside the measured span" if (levels and all_ok) else
                        "not smooth at this resolution: use measured rates only")
            + ("; one line in frame period fits and predicts the ends: extrapolation supported"
               if (levels and lin_ok and end_ok) else "; no extrapolation outside the measured span")}


def cross_check(comb: dict[str, dict[str, dict[str, Any]]], mode_key: str, *, k: float = 2.0) -> dict[str, Any]:
    """The same rate measured as a fixed mode (``mode_key``) and as a VRR cadence: agree per grey level?"""
    m, v = comb.get(mode_key) or {}, comb.get(vrr_key(rm.rate_hz(mode_key)) if mode_key in rm.RATES else "") or {}
    if not v:
        v = next((pats for r, pats in comb.items() if r.startswith(VRR_PREFIX)
                  and abs(float(next(iter(pats.values()))["rate_hz"]) - rm.rate_hz(mode_key)) < 0.02), {})
    rows = []
    for p, a in m.items():
        if a.get("kind") != "grey" or p not in v:
            continue
        b = v[p]
        d = b["ln_ratio"] - a["ln_ratio"]
        u = math.hypot(a["u"], b["u"])
        rows.append({"patch": p, "y60": a["y60"], "mode_ln": a["ln_ratio"], "vrr_ln": b["ln_ratio"], "diff": d, "u": u,
                     "ok": abs(d) <= max(k * u, FLOOR_LN)})
    return {"rate": mode_key, "levels": rows, "ok": bool(rows) and all(r["ok"] for r in rows),
            "max_abs_diff_ln": max((abs(r["diff"]) for r in rows), default=None),
            "measured": bool(rows)}


# ----------------------------------------------------------------------------- the rate source
class VrrSource:
    """The refresh probe's VRR rate source (``spec.source = "vrr"``)."""

    kind = "vrr"

    def __init__(self, cal: Any, hardware_id: str) -> None:
        self.cal, self.hw = cal, hardware_id
        self.presenter: Any = getattr(cal, "vrr_presenter", None)
        self.meter_refresh: Optional[Callable[[], dict[str, Any]]] = getattr(cal, "vrr_meter_refresh", None)
        self.read_meter: Optional[Callable[[], Reading]] = getattr(cal, "vrr_meter", None)
        self.mode = None
        self.plan: dict[str, Any] = {}
        self.current: Optional[str] = None
        self.current_hz: Optional[float] = None
        self.clock: Any = None
        self.proofs: dict[str, Any] = {}
        self._started = False

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.presenter, "simulated", False))

    # -- preflight ---------------------------------------------------------
    def preflight(self, spec: Any) -> tuple[list[str], dict[str, Any]]:
        out: list[str] = []
        if self.presenter is None:
            fac = getattr(self.cal, "vrr_presenter_factory", None)
            if fac is not None:
                self.presenter = fac(self.hw, int(self.cal.bit_depth))
        if self.presenter is None:
            return (["the VRR presenter (tools/vrr_tpg/vrr_tpg.exe) is not built: use the mode rate source "
                     "(spec source 'mode'), or build the presenter first"], {})
        api = getattr(self.cal, "display_api", None)
        if api is not None:
            from .refresh_probe import ModeSource
            self.mode = ModeSource(self.cal, api, self.hw)
            self.mode.clock = self.clock
            from dataclasses import replace as _replace
            m_ref, mplan = self.mode.preflight(_replace(spec, rates=()))
            out += [r for r in m_ref if "no other rate is reachable" not in r]
            plan = dict(mplan)
        else:
            out.append("no DisplayConfig reader: the VRR source needs it for the mode rate and the cross-check")
            plan = {}
        st = stack_view(self.cal)
        if (st.get("hook") or {}).get("active"):
            out.append("DesktopLUT's DWM hook is injected: VRR cannot engage for a composed window. Switch DWM Hook "
                       "mode off in DesktopLUT's settings for this sitting (and back on afterwards)")
        if (st.get("overlay") or {}).get("awake"):
            out.append("DesktopLUT's overlay is awake (a topmost overlay keeps windows composed): let it sleep "
                       "(no overlay-path layer on, no analysis view open)")
        rng, src = None, None
        if spec.vrr_range:
            rng, src = list(spec.vrr_range), "spec"
        edid = None
        dev_path = None
        try:
            from .probe_kit import monitor_entry
            dev_path = monitor_entry(self.cal).get("device_path")
        except Exception:  # noqa: BLE001
            pass
        if dev_path and not self.simulated:
            raw = edid_for_device_path(dev_path)
            edid = parse_vrr_range(raw) if raw else None
        if rng is None and edid and edid.get("range"):
            rng, src = list(edid["range"]), f"edid:{edid.get('range_source')}"
        if rng is None:
            hello_rng = (self.presenter.hello() or {}).get("vrr_range") if hasattr(self.presenter, "hello") else None
            if hello_rng:
                rng, src = list(hello_rng), "presenter"
        if rng is None:
            out.append("the display's VRR range could not be read (EDID / presenter): give vrr_range in the spec")
            rng = [0.0, 0.0]
        elif edid and edid.get("range") and spec.vrr_range and spec.vrr_range[0] < edid["range"][0]:
            out.append(f"the spec's VRR floor {spec.vrr_range[0]} Hz is below the EDID's {edid['range'][0]} Hz: rates "
                       "below the floor are frame-doubled (LFC) and would measure a multiple")
        ref_hz = None
        rr = plan.get("reference_rational")
        if rr:
            ref_hz = rr[0] / rr[1]
        lo = rng[0] + spec.vrr_floor_margin_hz
        hi = min(rng[1], ref_hz or rng[1])
        if ref_hz and rng[1] and ref_hz > rng[1] + 0.01:
            out.append(f"the reference {ref_hz:.3f} Hz is above the VRR range top {rng[1]} Hz")
        if hi - lo < 2.0:
            out.append(f"the usable VRR span {lo:.2f}..{hi:.2f} Hz is too narrow")
        content = [r for r in rm.CONTENT_RATES if lo <= rm.rate_hz(r) <= hi - 0.01]
        cross = spec.vrr_cross_check or next(
            (r for r in sorted(content, key=lambda r: rm.rate_hz(r))
             if (plan.get("rates") or {}).get(r, {}).get("reachable")), None)
        plan.update({"vrr_range": rng, "vrr_range_source": src, "edid": edid, "usable_hz": [lo, hi],
                     "content_in_range": content, "cross_check": cross,
                     "initial_hz": sorted({round(lo, 3), *[round(rm.rate_hz(r), 3) for r in content]}),
                     "measure": [vrr_key(h) for h in sorted({round(lo, 3), *[round(rm.rate_hz(r), 3) for r in content]},
                                                           reverse=True)]})
        if cross is None:
            plan["cross_check_note"] = ("no fixed mode inside the VRR range is offered: nothing measured both ways, "
                                        "so no fixed-mode trim will be derived from the VRR sweep")
        self.plan = plan
        if plan.get("reference"):
            self.current, self.current_hz = plan["reference"], ref_hz
        return out, plan

    # -- legs ----------------------------------------------------------------
    def legs(self, spec: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
        ref = plan["reference"]
        legs = [{"pass": "A", "rate": ref, "full": False, "kind": "vrr"}]
        for key in plan["measure"]:
            legs += [{"pass": "A", "rate": key, "full": False, "kind": "vrr"},
                     {"pass": "A", "rate": ref, "full": False, "kind": "vrr"}]
        for i, lg in enumerate(legs):
            lg["leg"] = i
        return legs

    def extend(self, probe: Any) -> list[dict[str, Any]]:
        """After the planned legs: adaptive VRR rates, then the mirror pass over every VRR rate, then the
        fixed-mode cross-check (bracketed, with its own consent). Empty = done."""
        st, spec = probe.st, probe.spec
        phase = st.get("vrr_phase", "A")
        ref = st["reference"]
        legs = probe.legs
        nxt = len(legs)

        def bracket(keys, pss, kind):
            out = []
            for k in keys:
                out += [{"pass": pss, "rate": k, "full": False, "kind": kind},
                        {"pass": pss, "rate": ref, "full": False, "kind": "vrr" if kind == "vrr" else "mode"}]
            for j, lg in enumerate(out):
                lg["leg"] = nxt + j
            return out

        if phase == "A":
            rows = rt.load_rows(probe.rows_path)
            comb = rt.combine(rt.bracket_ratios([r for r in rows if not r.get("implausible")], ref))
            have = [float(v["rate_hz"]) for r, pats in comb.items() if r.startswith(VRR_PREFIX)
                    for v in list(pats.values())[:1]]
            new = next_rates(comb, float(st["reference_hz"]), self.plan["usable_hz"][0], k=spec.vrr_tol_k,
                             min_gap_ms=spec.vrr_min_gap_ms, have=have, cap=spec.vrr_max_rates)
            st.setdefault("vrr_adaptive", []).append({"after_legs": nxt, "added_hz": new})
            if new:
                return bracket([vrr_key(h) for h in new], "A", "vrr")
            st["vrr_phase"] = "B"
            phase = "B"
        if phase == "B":
            st["vrr_phase"] = "X"
            if spec.passes == 2:
                keys = sorted({lg["rate"] for lg in legs if lg["rate"].startswith(VRR_PREFIX)},
                              key=lambda k: float(k[len(VRR_PREFIX):]))
                return bracket(keys, "B", "vrr")
            phase = "X"
        if phase == "X":
            st["vrr_phase"] = "done"
            cross = self.plan.get("cross_check")
            if cross and self.mode is not None:
                return bracket([cross], "X", "mode")
        return []

    # -- running -------------------------------------------------------------
    def start(self, spec: Any, st: dict[str, Any]) -> None:
        if not self._started:
            hello = self.presenter.start()
            st.setdefault("vrr_hello", jsonable(hello))
            self._started = True

    def pause(self) -> None:
        self.stop()

    def stop(self) -> None:
        if self._started:
            try:
                self.presenter.stop()
            finally:
                self._started = False

    def would_call(self, rate: str) -> dict[str, Any]:
        return self.mode.would_call(rate) if self.mode is not None else {}

    def set_rate(self, rate: str) -> dict[str, Any]:
        """A VRR key = a cadence on the current mode; a catalogue key = a fixed mode (the cross-check / the return
        to the reference after it), presented synced."""
        mode_hz = float(self.plan.get("reference_rational", [60, 1])[0]) / float(self.plan.get("reference_rational", [60, 1])[1])
        if rate.startswith(VRR_PREFIX):
            hz = float(rate[len(VRR_PREFIX):])
            if self.mode is not None and self.mode.current != self.plan["reference"]:
                back = self.mode.set_rate(self.plan["reference"])
                if not back.get("ok"):
                    return {"rate": rate, "ok": False, "fails": back.get("fails"), "proof": None}
            self.presenter.set_cadence(hz)
        else:
            hz = rm.rate_hz(rate)
            if self.mode is not None and self.mode.current != rate:
                rec = self.mode.set_rate(rate)
                if not rec.get("ok"):
                    rec["proof"] = rec.get("proof")
                    return rec
                mode_hz = hz
            self.presenter.set_cadence(hz)
        stats = self.presenter.stats()
        meter = self.meter_refresh() if self.meter_refresh is not None else None
        proof = judge_proof(hz, stats, meter, tol_rel=getattr(self, "tol_rel", 0.003), mode_hz=mode_hz)
        proof["stats"] = jsonable(stats)
        if meter is not None:
            proof["meter"] = jsonable(meter)
        rec = {"rate": rate, "ok": proof["ok"] is not False, "fails": proof["fails"], "active_hz": hz, "proof": proof}
        self.proofs[rate] = proof
        if rec["ok"]:
            self.current, self.current_hz = rate, hz
        return rec

    def read(self, reader: Any, mp: MeasurePatch, *, row: dict[str, Any]) -> Read:
        """Paint through the VRR presenter, read the meter (the probe's reader keeps liveness / cancel / rows)."""
        if mp.repaint:
            self.presenter.patch(mp.rgb, mp.window_area_pct or 100.0)

        def measure(_p: MeasurePatch) -> Reading:
            return self.read_meter()
        saved = reader.cal.measure
        try:
            reader.cal.measure = measure if self.read_meter is not None else saved
            return reader.read(mp, row=row)
        finally:
            reader.cal.measure = saved

    def restore(self) -> dict[str, Any]:
        self.stop()
        out = {"ok": True, "presenter": "stopped"}
        if self.mode is not None:
            out["mode"] = self.mode.restore()
            out["ok"] = bool(out["mode"].get("ok", True))
        hook = (stack_view(self.cal).get("hook") or {}).get("active")
        out["hook_active_now"] = hook
        out["note"] = "switch DesktopLUT's DWM Hook mode back on if it was on before the sitting"
        return out

    # -- the verdict -----------------------------------------------------------
    def finish(self, res: dict[str, Any], st: dict[str, Any], spec: Any) -> dict[str, Any]:
        """Turn the analysis' combined points into the VRR section; promote VRR content rates to fixed-mode trims
        only when the cross-check passed. Edits ``res["trim"]["rates"]`` in place."""
        comb = res["combined"]
        ref_hz = float(st["reference_hz"])
        model = vrr_model(comb, ref_hz, k=spec.vrr_tol_k)
        cross = self.plan.get("cross_check")
        xc = cross_check(comb, cross, k=spec.vrr_tol_k) if cross else {"measured": False, "ok": False,
                                                                          "note": self.plan.get("cross_check_note")}
        osd = any((p or {}).get("ok") is None for p in self.proofs.values())
        derive = bool(xc.get("ok")) and not osd
        trim = res["trim"]
        rates = trim.get("rates") or {}
        points = []
        for key in [k for k in rates if k.startswith(VRR_PREFIX)]:
            entry = rates.pop(key)
            pr = self.proofs.get(key) or {}
            points.append({"key": key, "hz": entry["hz"], "proof": {k: pr.get(k) for k in ("ok", "kind", "checks", "fails")},
                           "levels": [{"nits": k["y60_nits"], "ratio": k["ratio"], "u_ln": k["u_ln"], "n": k["n_repeats"]}
                                      for k in entry["knots"]]})
            cat = rm.classify_hz(entry["hz"])
            if derive and cat and cat in rm.CONTENT_RATES and cat not in rates and pr.get("ok") is True:
                promoted = dict(entry)
                promoted.update(source="vrr", num=rm.RATES[cat][0], den=rm.RATES[cat][1],
                                proof={"kind": "vrr:" + str(pr.get("kind")), "cross_check": cross,
                                       "ok": True})
                rates[cat] = promoted
        for key, entry in rates.items():
            entry.setdefault("source", "mode")
        section = {"range_hz": self.plan.get("vrr_range"), "range_source": self.plan.get("vrr_range_source"),
                   "usable_hz": self.plan.get("usable_hz"), "points": sorted(points, key=lambda p: -p["hz"]),
                   "model": {k: v for k, v in model.items() if k != "levels"},
                   "model_levels": [{k: lv[k] for k in ("patch", "y60_nits", "smooth", "linear_ok", "loo", "end_tests")}
                                    for lv in model["levels"]],
                   "cross_check": xc, "fixed_mode_trim_derived": derive,
                   "proof_fallback_osd": osd, "adaptive": st.get("vrr_adaptive")}
        trim["vrr"] = jsonable(section)
        digest = {"vrr_digest": {"vrr_range_hz": section["range_hz"], "vrr_points_hz": [p["hz"] for p in section["points"]],
                                 "interpolable": model["interpolable"], "extrapolable": model["extrapolable"],
                                 "model_verdict": model["verdict"], "cross_check": {k: xc.get(k) for k in
                                                                                    ("rate", "ok", "max_abs_diff_ln")},
                                 "fixed_mode_trim_derived": derive}}
        return {"vrr": trim["vrr"], **digest}
