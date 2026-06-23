"""The dashboard's brain — fold the event spine into a renderable state.

The browser is deliberately dumb: every number it shows is computed here. This module
ingests the run's :class:`~dlc.events.Event` stream and maintains one
:class:`DashboardState` — the status header, the current phase/stage, progress counters,
timers, rolling rates + ETA, the liveness verdict (judged from *data freshness*, not a
socket), the dE big-numbers (from the scoring stage's ``metrics_scored``), and the live
white-point CCT/Duv (enriched here, Python-side, from each patch's xy).

It is intentionally pure and clock-injectable so it can be tested without threads, files,
or sockets: :meth:`DashboardState.snapshot` takes the current time as an argument.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Optional

from ..events import Ev, Event
from .colorimetry import neutral_metrics, patch_delta_e, planckian_locus_xy

# The target gamut the charts draw the reference triangle against (sRGB / Rec.709 primaries).
_SRGB_PRIMARIES = {"r": [0.64, 0.33], "g": [0.30, 0.60], "b": [0.15, 0.06]}
_REC2020_PRIMARIES = {"r": [0.708, 0.292], "g": [0.170, 0.797], "b": [0.131, 0.046]}
# Rec.709/sRGB luminance weights — enough to compute a colour patch's TARGET relative
# luminance (Kr·r^γ + Kg·g^γ + Kb·b^γ) without the full primary matrix, so the Colour
# Luminance chart is live + dependency-free. The authoritative dE still comes from scoring.
_LUMA = (0.2126, 0.7152, 0.0722)
_LUMA_REC2020 = (0.2627, 0.6780, 0.0593)
# Patch-family ordering for the Colour Luminance bar chart (matches HCFR's R/G/B/C/M/Y sweep).
_FAMILY_ORDER = {"R": 0, "Y": 1, "G": 2, "C": 3, "B": 4, "M": 5, "mix": 6}
# Native-gamut overlay (visualization only): a primary candidate this far below the brightest
# primary read — or below this absolute Y — is a sub-noise (near-black) read whose chromaticity
# is meaningless, so it must never be plotted as the panel's native primary.
_NATIVE_PRIMARY_Y_FLOOR_FRAC = 0.01
_NATIVE_PRIMARY_Y_FLOOR_ABS = 0.1


def _sig_hex(sig) -> str:
    """A colour patch's approximate on-screen colour (its normalised code values as sRGB),
    for colouring the CIE dots + the luminance bars."""
    try:
        r, g, b = (max(0, min(255, int(round(float(c) * 255)))) for c in sig[:3])
        return f"#{r:02x}{g:02x}{b:02x}"
    except (TypeError, ValueError, IndexError):
        return "#888888"


def _classify_color(sig) -> tuple[str, int]:
    """Classify a colour patch into a hue family (R/G/B/C/M/Y/mix) + a saturation percent,
    for the Colour Luminance bar labels (e.g. 'R75'). Heuristic — a monitoring label, not
    a colorimetric identity."""
    try:
        r, g, b = (float(sig[0]), float(sig[1]), float(sig[2]))
    except (TypeError, ValueError, IndexError):
        return ("mix", 0)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx <= 0:
        return ("mix", 0)
    # bucket saturation to the nearest 25% so the many tube/volumetric patches collapse into
    # a readable R25/R50/R75/R100-style sweep (matching HCFR), not hundreds of bars.
    sat = max(0, min(100, round((mx - mn) / mx * 100 / 25) * 25))
    hi = tuple(1 if v >= 0.5 * mx else 0 for v in (r, g, b))
    family = {(1, 0, 0): "R", (0, 1, 0): "G", (0, 0, 1): "B",
              (1, 1, 0): "Y", (0, 1, 1): "C", (1, 0, 1): "M"}.get(hi, "mix")
    return (family, sat)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a spine timestamp to a tz-NAIVE datetime. The spine writes naive local
    wall-clock, and snapshot() compares against a naive ``datetime.now()`` — but a
    foreign/drifted producer could write a tz-aware string (``...Z`` / ``+00:00``),
    which 3.11+ accepts. Mixing naive and aware datetimes raises TypeError, and that
    would be swallowed by the server's loop guard → the dashboard silently freezes (the
    worst failure for a liveness tool). So normalise any aware value to naive local."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _xy_to_uv76(x: float, y: float) -> tuple[Optional[float], Optional[float]]:
    """CIE 1931 xy → CIE 1976 u'v' (the chroma space for saturation distance). None if degenerate."""
    denom = -2.0 * x + 12.0 * y + 3.0
    if abs(denom) < 1e-12:
        return (None, None)
    return (4.0 * x / denom, 9.0 * y / denom)


def _is_neutral(rgb: Optional[list]) -> bool:
    """A grayscale/white patch: all channels equal and non-black."""
    if not rgb or len(rgb) < 3:
        return False
    return rgb[0] == rgb[1] == rgb[2] and rgb[0] > 0


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _is_hdr_header(header: dict[str, Any]) -> bool:
    return bool(header.get("is_hdr")) or str(header.get("mode", "")).upper() == "HDR" \
        or str(header.get("transfer", "")).lower() == "pq"


def _pq_eotf(signal: float, peak: Optional[float]) -> float:
    """ST.2084 EOTF from normalized signal to absolute nits, clamped to target peak."""
    s = max(0.0, min(1.0, float(signal)))
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    p = s ** (1.0 / m2)
    nits = 10000.0 * (max(p - c1, 0.0) / max(c2 - c3 * p, 1e-9)) ** (1.0 / m1)
    return min(nits, float(peak)) if peak and peak > 0 else nits


# Run lifecycle as the dashboard sees it. "running" is the only live state; the rest are
# terminal/observed and drive the liveness light's colour.
RUN_IDLE = "idle"
RUN_RUNNING = "running"
RUN_STALLED = "stalled"
_TERMINAL_BY_STATUS = {
    "completed": "completed",
    "reverted": "reverted",
    "aborted": "aborted",
}


@dataclass
class DashboardState:
    """Everything the dashboard shows, folded from the spine. One per run."""

    # -- identity / status bar ------------------------------------------------
    header: dict[str, Any] = field(default_factory=dict)
    schema_version: Optional[int] = None
    run_id: Optional[str] = None

    # -- phase / stage --------------------------------------------------------
    phase: Optional[str] = None
    stage: Optional[str] = None
    run_status: str = RUN_IDLE

    # -- timing (ISO wall-clock from the producer; freshness math is in snapshot) -
    run_started_iso: Optional[str] = None
    stage_started_iso: Optional[str] = None
    last_event_iso: Optional[str] = None
    last_read_iso: Optional[str] = None
    last_progress_iso: Optional[str] = None   # last event that meant real forward progress
    ended_iso: Optional[str] = None

    # -- liveness inputs ------------------------------------------------------
    # The producer's authoritative progress-age (monotonic, reset by soak blocks too) rides
    # the heartbeat. Tracking it lets the light tell "alive AND progressing" from "alive but
    # WEDGED" — the exact 53-min failure shape — before the producer's own stall fires.
    last_heartbeat: dict[str, Any] = field(default_factory=dict)
    awaiting_decision: bool = False           # paused at a seam (NOT a stall) — don't show red

    # -- counters -------------------------------------------------------------
    patches_done: int = 0
    patches_total: int = 0
    reads: int = 0
    reads_ok: int = 0
    reads_failed: int = 0

    # -- dE big-numbers (from the scoring stage; authoritative) --------------
    de: dict[str, Any] = field(default_factory=dict)
    de_history: list[dict[str, Any]] = field(default_factory=list)

    # -- live colour readout (enriched here from patch xy) -------------------
    last_white: dict[str, Any] = field(default_factory=dict)
    last_read: dict[str, Any] = field(default_factory=dict)
    # live per-patch ΔE for the header (vs each patch's target), keyed by patch identity so a
    # RE-READ overwrites rather than double-counts (latest-wins, like the chart accumulators);
    # reset per stage. Distinct from the per-stage authoritative `de` from metrics_scored.
    _live_de: dict[Any, float] = field(default_factory=dict)

    # -- optimizer / seams / anomalies / stall ------------------------------
    optimizer: dict[str, Any] = field(default_factory=dict)
    last_seam: dict[str, Any] = field(default_factory=dict)
    last_anomaly: dict[str, Any] = field(default_factory=dict)
    last_check_in: dict[str, Any] = field(default_factory=dict)
    stall: dict[str, Any] = field(default_factory=dict)

    events_seen: int = 0

    # rolling windows for rates/ETA (not serialised raw — distilled in snapshot)
    _read_times: Deque[datetime] = field(default_factory=lambda: deque(maxlen=80))
    _progress_marks: Deque[tuple] = field(default_factory=lambda: deque(maxlen=80))

    # chart accumulators (bounded; served via /api/charts, OFF the fast SSE path so the 2 s
    # state push stays lean while the scatter can grow to thousands of points). The "current
    # state" charts (CIE / grayscale / EOTF / colour-luminance) are kept PER MEASUREMENT STAGE
    # so the dashboard shows the LATEST stage (the corrected result) instead of raw + post-MHC +
    # verify + build-probe reads overlaid into one unreadable cloud. Warm-up + 3D-LUT build-probe
    # reads are excluded entirely (panel-conditioning / transient, not a settled measurement).
    _cie_by_stage: dict = field(default_factory=dict)       # stage → deque[point]
    _gray_by_stage: dict = field(default_factory=dict)      # stage → {level: latest neutral sample}
    _color_by_stage: dict = field(default_factory=dict)     # stage → {signal: latest colour sample}
    _stage_seq: list = field(default_factory=list)          # measurement stages, first-seen order
    # Cross-stage white-DRIFT time series. Primary = the dedicated neutral_ref checkpoints (a
    # FIXED neutral re-measured over the run — the clean thermal-drift signal). Fallback = white-
    # level (signal>=0.9) measurement neutrals, used only when a run emits no neutral_ref. Mid/low
    # grayscale levels are deliberately kept OUT: their CCT is noisy and buries the drift in a cloud.
    _white_track: Deque[dict] = field(default_factory=lambda: deque(maxlen=600))
    _white_fallback: Deque[dict] = field(default_factory=lambda: deque(maxlen=600))
    _optimizer_history: list = field(default_factory=list)

    # ----------------------------------------------------------------------
    def ingest(self, ev: Event) -> dict[str, Any]:
        """Fold one event into the state; return its enriched wire form (for the log)."""
        self.events_seen += 1
        if ev.time:
            self.last_event_iso = ev.time
        if self.run_started_iso is None and ev.time:
            self.run_started_iso = ev.time
        if ev.phase:
            self.phase = ev.phase

        name = ev.event
        data = ev.data or {}

        if name == Ev.RUN_HEADER:
            self.header = dict(data)
            self.schema_version = data.get("schema_version", self.schema_version)
            self.run_id = data.get("run_id", self.run_id)
            # A run_header marks a run process (re)starting. Promote to RUNNING even from a
            # prior TERMINAL status — a resume appends a fresh run_header into the same events
            # stream after an earlier run_done (e.g. an aborted run resumed from a memoised
            # stage); without this the dashboard stays latched on the stale "aborted" while
            # live build events keep arriving (half-live view). A real end re-terminates via
            # the next run_done. (_clear_alarm deliberately does NOT resurrect terminal runs;
            # run_header is the stronger, explicit restart signal that may.)
            self.run_status = RUN_RUNNING
        elif name == Ev.PHASE:
            self.phase = data.get("phase_name", self.phase)
        elif name == Ev.STAGE_START:
            self.stage = ev.stage
            self.stage_started_iso = ev.time
            # A stage boundary is real progress and resets the per-stage rate window — the
            # patch counter restarts at ~0, so an ETA computed across the reset is garbage.
            self._mark_progress(ev.time)
            self._progress_marks.clear()
            self._live_de.clear()       # the live ΔE header tracks the CURRENT stage's patches
            self._clear_alarm()
        elif name == Ev.STAGE_ABORTED:
            # keep self.stage as the failing stage for the header
            pass
        elif name == Ev.PROGRESS:
            # patches_done/total are per-stage (the bar shows the CURRENT stage's progress);
            # the cumulative read total is tracked in _ingest_patch_read so it stays
            # consistent with reads_ok/reads_failed (PROGRESS.reads resets each stage).
            self.patches_done = _as_int(data.get("patches_done"), self.patches_done)
            self.patches_total = _as_int(data.get("patches_total"), self.patches_total)
            self._mark_progress(ev.time)
            self._clear_alarm()
            mark_t = _parse_iso(ev.time)
            if mark_t is not None:
                self._progress_marks.append((mark_t, self.patches_done))
        elif name == Ev.PATCH_READ:
            self._ingest_patch_read(ev, data)
        elif name == Ev.HEARTBEAT:
            # The producer's authoritative progress-age (monotonic) — the one signal that
            # survives a syscall wedge AND a silent soak (soak blocks reset it producer-side).
            self.last_heartbeat = {"time": ev.time,
                                   "since_progress_s": _as_float(data.get("since_progress_s")),
                                   "stall_after_s": _as_float(data.get("stall_after_s"))}
        elif name == "metrics_scored":
            self._ingest_metrics(ev, data)
            self._mark_progress(ev.time)
        elif name == Ev.OPTIMIZER_ITER:
            self.optimizer = dict(data)
            self._optimizer_history.append(dict(data))
            if len(self._optimizer_history) > 256:
                self._optimizer_history = self._optimizer_history[-256:]
            self._mark_progress(ev.time)
            self._clear_alarm()
        elif name == Ev.SEAM:
            self.last_seam = {"stage": ev.stage, "time": ev.time, **data}
            # A paused seam is a healthy human-in-the-loop wait, NOT a stall — but heartbeats
            # stop (the run process exits to await the decision), so without this flag the
            # light would drift to red and read identically to a hang. Latch it; any later
            # progress (the resuming run) clears it.
            self.awaiting_decision = (data.get("status") == "paused")
        elif name == Ev.ANOMALY:
            self.last_anomaly = {"stage": ev.stage, "time": ev.time, **data}
        elif name == Ev.CHECK_IN:
            self.last_check_in = {"stage": ev.stage, "time": ev.time, **data}
        elif name == Ev.STALL:
            self.run_status = RUN_STALLED
            self.stall = {"stage": ev.stage, "time": ev.time, **data}
        elif name == Ev.RUN_DONE:
            status = data.get("status", "completed")
            self.run_status = _TERMINAL_BY_STATUS.get(status, status)
            self.ended_iso = ev.time
            self.awaiting_decision = False

        return self._wire(ev, data)

    def _mark_progress(self, iso: Optional[str]) -> None:
        if iso:
            self.last_progress_iso = iso

    def _clear_alarm(self) -> None:
        """Fresh forward progress arrived → the run is moving again. Clear a paused seam,
        promote IDLE→RUNNING (the run is observably live), and un-latch a prior stall (if a
        stall guard self-recovered within the stage) — so the light reflects the data, not a
        stale status. Never resurrects a TERMINAL run (completed/reverted/aborted)."""
        self.awaiting_decision = False
        if self.run_status in (RUN_IDLE, RUN_STALLED):
            self.run_status = RUN_RUNNING

    def _ingest_patch_read(self, ev: Event, data: dict[str, Any]) -> None:
        ok = bool(data.get("ok"))
        if ok:
            self.reads_ok += 1
        else:
            self.reads_failed += 1
        self.reads = self.reads_ok + self.reads_failed  # cumulative; consistent with ok/fail
        read_t = _parse_iso(ev.time)
        if read_t is not None:
            self._read_times.append(read_t)
            self.last_read_iso = ev.time

        xy = data.get("xy")
        Y = data.get("Y")
        has_xy = bool(xy) and len(xy) >= 2 and _as_float(xy[0]) is not None and _as_float(xy[1]) is not None
        enriched: dict[str, Any] = {}
        if ok and has_xy:
            enriched = neutral_metrics(float(xy[0]), float(xy[1]))
            self._mark_progress(ev.time)   # a good read is forward progress
            self._clear_alarm()
            self._accumulate_charts(ev, data, float(xy[0]), float(xy[1]), Y, enriched)
        de = self._patch_delta_e(data) if (ok and has_xy) else None
        self.last_read = {
            "seq": data.get("seq"), "role": data.get("role"), "label": data.get("label"),
            "rgb": data.get("rgb"), "signal": data.get("signal"), "Y": Y, "xy": xy, "ok": ok,
            "neutral": _is_neutral(data.get("rgb")), "de": de,
            "disposition": data.get("disposition"), **enriched,
        }
        # Live ΔE (header reading): only settled MEASUREMENT reads, not warm-up/probe. Keyed by
        # patch identity (signal → else label/seq) so a re-read overwrites instead of double-counting.
        if de is not None and data.get("role") not in ("warmup", "probe") \
                and data.get("disposition") != "probe":
            sig = data.get("signal")
            key = tuple(round(float(c), 4) for c in sig[:3]) if (sig and len(sig) >= 3) \
                else ("seq", data.get("label"), data.get("seq"))
            self._live_de[key] = de
        # The most recent neutral read drives the live white-point readout (needs a usable xy
        # so the readout shape stays consistent with the enrichment gate above).
        if ok and has_xy and _is_neutral(data.get("rgb")):
            self.last_white = {"xy": xy, "Y": Y, "rgb": data.get("rgb"), **enriched}

    def _live_de_summary(self) -> dict[str, Any]:
        """Rolling per-patch ΔE for the LIVE header (current stage). avg/max over the window +
        the run's metric label, so the header updates every patch (distinct from the per-stage
        authoritative ``de`` from metrics_scored)."""
        vals = list(self._live_de.values())
        metric = "dE_ITP" if _is_hdr_header(self.header) else "CIEDE2000"
        if not vals:
            return {"avg": None, "max": None, "n": 0, "metric": metric}
        return {"avg": round(sum(vals) / len(vals), 3), "max": round(max(vals), 3),
                "n": len(vals), "metric": metric}

    def _patch_delta_e(self, data: dict[str, Any]) -> Optional[float]:
        """Per-patch ΔE vs the patch's ideal target at the resolved white — dE_ITP for HDR,
        CIEDE2000 for SDR (dashboard.colorimetry.patch_delta_e, cross-validated against the
        spine's scorer). Needs the patch's normalised ``signal`` + a good xy/Y; ``None`` otherwise."""
        xy, Y, sig = data.get("xy"), data.get("Y"), data.get("signal")
        if not (xy and len(xy) >= 2 and Y is not None and sig and len(sig) >= 3):
            return None
        x, y = _as_float(xy[0]), _as_float(xy[1])
        big_y = _as_float(Y)
        if x is None or y is None or big_y is None:
            return None
        white = (self.header.get("white") or {}).get("xy") or (0.3127, 0.3290)
        lum = _as_float(self.header.get("luminance"))
        gamma = _as_float(self.header.get("gamma")) or 2.2
        try:
            de = patch_delta_e([float(c) for c in sig[:3]], x, y, big_y,
                               is_hdr=_is_hdr_header(self.header), white_xy=white,
                               luminance=lum, gamma=gamma)
        except Exception:  # noqa: BLE001 — a monitoring number must never break ingest
            return None
        return round(de, 3) if de is not None else None

    def _elapsed_at(self, iso: Optional[str]) -> Optional[float]:
        t, start = _parse_iso(iso), _parse_iso(self.run_started_iso)
        if t is None or start is None:
            return None
        return round(max(0.0, (t - start).total_seconds()), 1)

    def _chart_stage(self, ev: Event, data: dict[str, Any]) -> Optional[str]:
        """The measurement-stage bucket a read belongs to for the 'current state' charts, or
        ``None`` if it must be EXCLUDED. Warm-up + drift-reference reads (panel conditioning, not
        target patches) and 3D-LUT build-probe reads (transient mid-convergence, not a settled
        measurement) are dropped; everything else is keyed by the run phase (e.g.
        ``measure:post-mhc``) so each stage is its own dataset and the dashboard shows the latest."""
        role = data.get("role")
        if role in ("warmup", "neutral_ref", "probe") or data.get("disposition") == "probe":
            return None
        return ev.phase or ev.stage or "measure"

    def _accumulate_charts(self, ev: Event, data: dict[str, Any], x: float, y: float,
                           Y: Any, enriched: dict[str, Any]) -> None:
        """Fold a good read into the bounded chart datasets (served via /api/charts)."""
        role = data.get("role")
        disposition = data.get("disposition")
        rgb = data.get("rgb")
        sig = data.get("signal")
        neutral = _is_neutral(rgb)
        is_probe = role == "probe" or disposition == "probe"
        # Drift series (cross-stage TIME series). The dedicated neutral_ref drift checkpoints
        # are a FIXED neutral re-measured over time — the clean signal. The whole grayscale ramp
        # (every measurement neutral, all levels) is NOT drift: dark/mid steps have noisy CCT and
        # turn the chart into a cloud, so they're excluded. A white-level (>=0.9) measurement
        # neutral is a legitimate fallback for runs that emit no neutral_ref checkpoints.
        if not is_probe:
            sig_level = _as_float(sig[0]) if (sig and len(sig) >= 1) else None
            sample = {"elapsed_s": self._elapsed_at(ev.time), "signal": sig_level,
                      "cct": enriched.get("cct"), "duv": enriched.get("duv"), "Y": Y}
            if role == "neutral_ref":
                self._white_track.append(sample)
            elif neutral and role != "warmup" and sig_level is not None and sig_level >= 0.9:
                self._white_fallback.append(sample)
        # Snapshot charts (latest measurement stage only): exclude warm-up, drift-ref, build-probe.
        stage = self._chart_stage(ev, data)
        if stage is None:
            return
        if stage not in self._cie_by_stage:
            self._cie_by_stage[stage] = deque(maxlen=5000)
            self._gray_by_stage[stage] = {}
            self._color_by_stage[stage] = {}
            self._stage_seq.append(stage)
        self._cie_by_stage[stage].append({"x": round(x, 5), "y": round(y, 5),
                                          "role": role, "neutral": neutral,
                                          "c": (None if neutral else _sig_hex(sig)) if sig else None})
        level = _as_float(sig[0]) if (neutral and sig and len(sig) >= 1) else None
        if level is not None:
            # latest measurement at this grayscale level wins (re-measures overwrite)
            self._gray_by_stage[stage][round(level, 5)] = {
                "signal": round(level, 5), "Y": Y, "x": round(x, 5), "y": round(y, 5),
                "cct": enriched.get("cct"), "duv": enriched.get("duv")}
        elif not neutral and sig and len(sig) >= 3 and Y is not None:
            # a colour patch: keep the latest measured Y per distinct signal for the
            # Colour Luminance chart (luminance error vs target is derived in charts()).
            try:
                key = (round(float(sig[0]), 4), round(float(sig[1]), 4), round(float(sig[2]), 4))
                self._color_by_stage[stage][key] = {"signal": list(key), "Y": Y,
                                                    "x": round(x, 5), "y": round(y, 5)}
            except (TypeError, ValueError):
                pass

    def _ingest_metrics(self, ev: Event, data: dict[str, Any]) -> None:
        entry = {
            "phase": data.get("label") or data.get("phase") or ev.stage,
            "iteration": data.get("iteration"),
            "metric": data.get("metric"),
            "avg": data.get("avg_de2000"),
            "p95": data.get("p95_de2000"),
            "p99": data.get("p99_de2000"),
            "max": data.get("max_de2000"),
            "white": data.get("white_de2000"),
            "grayscale": data.get("grayscale_avg_de2000"),
            "colour": data.get("colour_avg_de2000"),
        }
        self.de = entry
        self.de_history.append(entry)
        if len(self.de_history) > 64:
            self.de_history = self.de_history[-64:]

    # ----------------------------------------------------------------------
    def _latest_chart_stage(self) -> Optional[str]:
        """The most recent measurement stage with charted reads — the one the snapshot charts
        show (so the CIE/grayscale/EOTF/colour-luminance tiles track the latest correction
        state: raw while profiling, verify once verified)."""
        return self._stage_seq[-1] if self._stage_seq else None

    def charts(self) -> dict[str, Any]:
        """Chart-ready datasets, built from the bounded accumulators. Served via
        /api/charts (NOT the SSE state) so the heavy scatter stays off the fast path.
        The snapshot charts reflect the LATEST measurement stage (not all stages overlaid);
        the drift chart is the cross-stage time series. Everything is already numeric/derived
        — the browser only draws SVG."""
        stage = self._latest_chart_stage()
        cie_points = list(self._cie_by_stage.get(stage, ())) if stage else []
        gray_map = self._gray_by_stage.get(stage, {}) if stage else {}
        gray = [gray_map[k] for k in sorted(gray_map)]
        color_map = self._color_by_stage.get(stage, {}) if stage else {}
        white = (self.header.get("white") or {}).get("xy")
        gamma = self.header.get("gamma") or 2.2
        hdr = _is_hdr_header(self.header)
        luminance = self.header.get("luminance")
        return {
            "stage": stage,                       # the measurement stage these snapshot charts reflect
            "stages": list(self._stage_seq),
            "cie": {
                "points": cie_points,
                "white": white,
                "primaries": _REC2020_PRIMARIES if hdr else _SRGB_PRIMARIES,
                "gamut_label": "Rec.2020" if hdr else "Rec.709 / sRGB",
                # The panel's MEASURED native gamut (from the raw pure-channel peaks), so the
                # frontend can overlay native coverage on the STANDARD target gamut — the
                # "standard vs native" view (#20). None when no saturated primaries were measured.
                "native": self._native_primaries(),
                "locus": [[round(x, 5), round(y, 5)] for (x, y) in planckian_locus_xy()],
            },
            "grayscale": gray,
            "eotf": {
                "kind": "pq" if hdr else "power",
                "gamma": gamma,
                "luminance": luminance,
                "reference": self._eotf_reference(hdr=hdr, gamma=gamma, luminance=luminance),
                "points": [{"signal": g["signal"], "Y": g["Y"]} for g in gray if g.get("Y") is not None],
            },
            "color_lum": self._color_luminance(color_map, gray, gamma, hdr=hdr, luminance=luminance),
            "saturation": self._saturation(color_map, white),
            "optimizer": list(self._optimizer_history),
            "white_track": [w for w in self._drift_series() if w.get("elapsed_s") is not None],
        }

    def _drift_series(self) -> Deque[dict]:
        """The white-drift time series: the dedicated neutral_ref checkpoints when the run has
        them (a fixed neutral re-measured over time — the clean signal), else the white-level
        measurement-neutral fallback. Never the full grayscale ramp (its low/mid CCT is noise)."""
        return self._white_track if self._white_track else self._white_fallback

    def _eotf_reference(self, *, hdr: bool, gamma: float, luminance: Any) -> list[list[float]]:
        out = []
        peak = _as_float(luminance)
        for i in range(41):
            s = i / 40.0
            y = (_pq_eotf(s, peak) / peak) if (hdr and peak and peak > 0) else (s ** float(gamma or 2.2))
            out.append([round(s, 5), round(max(0.0, min(1.0, y)), 6)])
        return out

    def _color_luminance(self, color_map: dict, gray: list[dict], gamma: float, *,
                         hdr: bool = False, luminance: Any = None) -> list[dict[str, Any]]:
        """Per-colour-patch luminance error vs target, as fractions (-0.1 = 5% dim, etc.).
        Target relative luminance = Σ Kc·signal_c^γ (Rec.709 weights); measured relative =
        measured Y / the brightest neutral Y. Computed here (not at read time) against the
        best-known white, so it tracks even if white was measured at a different moment."""
        white_y = max((g["Y"] for g in gray if g.get("Y") is not None), default=None)
        if white_y is None:
            white_y = (self.last_white or {}).get("Y")
        if not white_y or white_y <= 0:
            return []
        # Aggregate per (family, saturation-bucket): many patches share a label, so average
        # their luminance error into one bar (the representative colour = the brightest patch).
        groups: dict[str, dict[str, Any]] = {}
        for c in color_map.values():
            sig, Y = c.get("signal"), c.get("Y")
            if Y is None or not sig:
                continue
            if hdr:
                peak = _as_float(luminance)
                weights = _LUMA_REC2020
                if peak and peak > 0:
                    target_y = sum(k * _pq_eotf(float(s), peak) for k, s in zip(weights, sig))
                    target_rel = target_y / peak
                else:
                    target_rel = sum(k * (max(0.0, s) ** gamma) for k, s in zip(weights, sig))
            else:
                target_rel = sum(k * (max(0.0, s) ** gamma) for k, s in zip(_LUMA, sig))
            if target_rel <= 1e-4:
                continue
            family, sat = _classify_color(sig)
            label = f"{family}{sat}"
            err = Y / white_y / target_rel - 1.0
            mag = max(sig)
            grp = groups.setdefault(label, {"family": family, "sat": sat, "errs": [],
                                            "color": _sig_hex(sig), "mag": mag})
            grp["errs"].append(err)
            if mag >= grp["mag"]:
                grp["color"], grp["mag"] = _sig_hex(sig), mag
        out = [{"label": lbl, "family": g["family"], "sat": g["sat"],
                "error": round(sum(g["errs"]) / len(g["errs"]), 4),
                "n": len(g["errs"]), "color": g["color"]}
               for lbl, g in groups.items()]
        out.sort(key=lambda d: (_FAMILY_ORDER.get(d["family"], 9), d["sat"]))
        return out

    def _native_primaries(self) -> Optional[dict[str, list[float]]]:
        """The panel's measured native R/G/B chromaticities — the true full-drive primary per
        hue from the RAW (first-measured) stage, so it's the native gamut independent of which
        corrected stage the scatter currently shows. ``None`` until all three primaries have a
        usable (above-noise) saturated patch (e.g. a neutral-only raw ramp).

        Selection is by *highest luminance*, not saturation: every pure-channel patch
        ([0,g,0] …) classifies as ~100% saturated, so picking by saturation alone can keep a
        near-black read whose chromaticity is pure noise (observed: green plotted at a wild
        (0.232, 0.466) from a Y=0.027-nit read instead of the real ~(0.183, 0.749) primary).
        Within the most-saturated class we therefore take the brightest read — mirroring the
        engine's ``channel_model().peak_xyz`` (highest-Y sample), which the build/scoring use."""
        if not self._stage_seq:
            return None
        color_map = self._color_by_stage.get(self._stage_seq[0], {})
        # family -> (saturation, Y, [x, y]); prefer the most-saturated class and, within it,
        # the highest-luminance (true full-drive) read.
        best: dict[str, tuple[int, float, list[float]]] = {}
        for c in color_map.values():
            sig, x, y, Y = c.get("signal"), c.get("x"), c.get("y"), c.get("Y")
            if x is None or y is None or Y is None or not sig:
                continue
            family, sat = _classify_color(sig)
            if family not in ("R", "G", "B") or sat <= 0:
                continue
            prev = best.get(family)
            if prev is None or sat > prev[0] or (sat == prev[0] and Y > prev[1]):
                best[family] = (sat, float(Y), [round(x, 5), round(y, 5)])
        if not all(f in best for f in ("R", "G", "B")):
            return None
        # Suppress sub-noise primaries: a chosen read far below the brightest primary (or below
        # an absolute floor) has unreliable chromaticity — hide the whole overlay rather than
        # plot the native gamut at a meaningless point.
        brightest = max(best[f][1] for f in ("R", "G", "B"))
        floor = max(_NATIVE_PRIMARY_Y_FLOOR_ABS, brightest * _NATIVE_PRIMARY_Y_FLOOR_FRAC)
        if any(best[f][1] < floor for f in ("R", "G", "B")):
            return None
        return {f.lower(): best[f][2] for f in ("R", "G", "B")}

    def _saturation(self, color_map: dict, white_xy: Any) -> list[dict[str, Any]]:
        """Saturation tracking per hue family: measured chroma (CIE 1976 u'v' distance from the
        target white) vs the commanded saturation, normalised so each family's 100%-saturation
        patch = 1.0. Ideal tracking is the identity line (commanded → measured). Dependency-free
        geometry; no dE — a monitoring view of how saturation builds, not the authoritative score."""
        if not white_xy or len(white_xy) < 2:
            return []
        wu, wv = _xy_to_uv76(white_xy[0], white_xy[1])
        if wu is None:
            return []
        fams: dict[str, list[dict[str, Any]]] = {}
        for c in color_map.values():
            sig, x, y = c.get("signal"), c.get("x"), c.get("y")
            if x is None or y is None or not sig:
                continue
            family, sat = _classify_color(sig)
            if family == "mix" or sat <= 0:
                continue
            uv = _xy_to_uv76(x, y)
            if uv[0] is None:
                continue
            chroma = ((uv[0] - wu) ** 2 + (uv[1] - wv) ** 2) ** 0.5
            fams.setdefault(family, []).append({"sat": sat, "chroma": chroma, "color": _sig_hex(sig)})
        out: list[dict[str, Any]] = []
        for family, pts in fams.items():
            cmax = max((p["chroma"] for p in pts), default=0.0)
            if cmax <= 0:
                continue
            for p in pts:
                out.append({"family": family, "target": round(p["sat"] / 100.0, 4),
                            "measured": round(p["chroma"] / cmax, 4), "color": p["color"]})
        out.sort(key=lambda d: (_FAMILY_ORDER.get(d["family"], 9), d["target"]))
        return out

    # ----------------------------------------------------------------------
    def _wire(self, ev: Event, data: dict[str, Any]) -> dict[str, Any]:
        """One event as the browser's event-log sees it. For patch reads we fold the
        CCT/Duv in so the log can show the colour readout without any JS colour math."""
        out: dict[str, Any] = {
            "time": ev.time, "level": ev.level, "stage": ev.stage,
            "event": ev.event, "phase": ev.phase, "tier": ev.effective_tier,
            "data": data,
        }
        if ev.event == Ev.PATCH_READ:
            xy = data.get("xy")
            if data.get("ok") and xy and len(xy) >= 2:
                x, y = _as_float(xy[0]), _as_float(xy[1])
                if x is not None and y is not None:
                    derived = neutral_metrics(x, y)
                    de = self._patch_delta_e(data)        # per-patch ΔE for the event log
                    if de is not None:
                        derived["de"] = de
                    out["derived"] = derived
        return out

    # ----------------------------------------------------------------------
    def _rolling_s_per_read(self) -> Optional[float]:
        times = list(self._read_times)
        if len(times) < 2:
            return None
        diffs = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
        diffs = [d for d in diffs if 0.0 < d < 600.0]  # drop pauses/soak gaps
        return _median(diffs)

    def _rolling_s_per_patch(self) -> Optional[float]:
        marks = list(self._progress_marks)
        if len(marks) < 2:
            return None
        (t0, p0), (t1, p1) = marks[0], marks[-1]
        dp = p1 - p0
        dt = (t1 - t0).total_seconds()
        if dp <= 0 or dt <= 0:
            return None
        return dt / dp

    def _progress_age(self, now: datetime) -> Optional[float]:
        """Seconds since the run last made REAL forward progress (a good read, a completed
        iteration, a soak block, a stage boundary). Distinct from event-age: heartbeats keep
        event-age fresh during a wedge, but progress-age keeps growing — that gap is exactly
        the 53-min "alive but stuck" failure. Two estimates of the same quantity; take the
        smaller (the more recent progress wins):

        * **events** — ``now - last_progress_iso`` (fresh, but blind to the silent soak,
          which emits no patch/progress events).
        * **heartbeat** — the producer's monotonic ``since_progress_s`` (authoritative; reset
          by soak blocks too) carried forward by the age of that heartbeat.
        """
        candidates: list[float] = []
        lp = _parse_iso(self.last_progress_iso)
        if lp is not None:
            candidates.append(max(0.0, (now - lp).total_seconds()))
        hb = self.last_heartbeat
        hb_t = _parse_iso(hb.get("time")) if hb else None
        since = hb.get("since_progress_s") if hb else None
        if hb_t is not None and since is not None:
            candidates.append(max(0.0, since + (now - hb_t).total_seconds()))
        return min(candidates) if candidates else None

    def _liveness(self, now: datetime) -> dict[str, Any]:
        """The light, judged from DATA FRESHNESS (not the socket). Order matters:
        terminal → done; a paused seam → a calm 'paused' (NOT red — a healthy decision
        wait, even though heartbeats stop); a tripped stall → red; otherwise weigh
        event-age (is the process alive at all?) against progress-age (is it actually
        advancing?). The progress-age arm is what catches an alive-but-wedged run before
        the producer's own stall fires."""
        event_age = None
        last = _parse_iso(self.last_event_iso)
        if last is not None:
            event_age = max(0.0, (now - last).total_seconds())
        prog_age = self._progress_age(now)

        if self.run_status in _TERMINAL_BY_STATUS.values():
            light = "done"
        elif self.awaiting_decision:
            light = "paused"
        elif self.run_status == RUN_STALLED:
            light = "stalled"
        elif event_age is None:
            light = "unknown"
        else:
            spr = self._rolling_s_per_read()
            # Event budget: heartbeats arrive ≤15 s apart, so any larger silence means the
            # process itself is gone — go red. Floored generously against read jitter.
            event_budget = max(45.0, (spr or 10.0) * 4.0)
            # Progress budget: prefer the producer's own stall threshold (from the heartbeat)
            # so the dashboard warns in step with the guard; else a generous default.
            stall_after = (self.last_heartbeat or {}).get("stall_after_s")
            prog_budget = stall_after if (stall_after and stall_after > 0) else max(120.0, (spr or 10.0) * 12.0)
            if event_age > event_budget:
                light = "stalled"            # no heartbeat even → process likely dead
            elif prog_age is not None and prog_age > prog_budget:
                light = "stalled"            # alive but past the stall threshold (guard should fire ~now)
            elif prog_age is not None and prog_age > 0.5 * prog_budget:
                light = "slow"               # alive but not advancing — the early wedge warning
            else:
                light = "live"
        return {"light": light,
                "age_s": round(event_age, 1) if event_age is not None else None,
                "progress_age_s": round(prog_age, 1) if prog_age is not None else None,
                "last_event_iso": self.last_event_iso, "last_read_iso": self.last_read_iso}

    def snapshot(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """The full renderable state. ``now`` is injected for testability; the server
        passes ``datetime.now()`` so timers/liveness advance even with no new events."""
        if now is None:
            now = datetime.now()

        started = _parse_iso(self.run_started_iso)
        run_elapsed = (now - started).total_seconds() if started else None
        ended = _parse_iso(self.ended_iso)
        if ended and started:
            run_elapsed = (ended - started).total_seconds()  # freeze the clock when done

        stage_started = _parse_iso(self.stage_started_iso)
        stage_elapsed = (now - stage_started).total_seconds() if stage_started else None
        if ended and stage_started:
            stage_elapsed = (ended - stage_started).total_seconds()

        spr = self._rolling_s_per_read()
        spp = self._rolling_s_per_patch()
        remaining = max(0, self.patches_total - self.patches_done) if self.patches_total else 0
        eta = remaining * spp if (spp and remaining and self.run_status == RUN_RUNNING) else None

        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "header": self.header,
            "phase": self.phase,
            "stage": self.stage,
            "run_status": self.run_status,
            "events_seen": self.events_seen,
            "counters": {
                "patches_done": self.patches_done,
                "patches_total": self.patches_total,
                "reads": self.reads,
                "reads_ok": self.reads_ok,
                "reads_failed": self.reads_failed,
            },
            "timers": {
                "run_elapsed_s": round(run_elapsed, 1) if run_elapsed is not None else None,
                "stage_elapsed_s": round(stage_elapsed, 1) if stage_elapsed is not None else None,
                "s_per_read": round(spr, 2) if spr is not None else None,
                "s_per_patch": round(spp, 2) if spp is not None else None,
                "eta_s": round(eta, 1) if eta is not None else None,
                "run_started_iso": self.run_started_iso,
                "ended_iso": self.ended_iso,
            },
            "de": self.de,
            "de_history": self.de_history,
            "live_de": self._live_de_summary(),
            "last_white": self.last_white,
            "last_read": self.last_read,
            "optimizer": self.optimizer,
            "seam": self.last_seam,
            "anomaly": self.last_anomaly,
            "check_in": self.last_check_in,
            "stall": self.stall,
            "liveness": self._liveness(now),
        }
