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
from ..gamut import point_in_triangle
from .colorimetry import (
    linear_rgb, neutral_metrics, patch_deltas, planckian_locus_xy, rgb_balance,
)

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
# Grayscale CCT/Duv charts (visualization only): below this absolute luminance a neutral's
# chromaticity is noise-dominated and CCT is undefined as Y→0 (e.g. a 0.006-nit read solving to
# 11000 K). Such points are flagged ``dim``; the charts keep them OFF the y-autoscale (one wild
# read would bury the real ~6500 K variation) but render them in the ΔE domain — coloured by
# their ΔE vs the neutral target, which correctly down-weights chroma near black, so the chart
# answers "is the tint visible?" instead of hiding the read. Matches the build's own near-black
# floor (corrections blend to identity there anyway), so the chart mirrors what's used.
_GRAY_CCT_Y_FLOOR_NITS = 1.0
# Bound on the per-stage CIE scatter (keyed latest-wins; oldest-inserted evicted past the cap).
_CIE_CAP = 5000
# Re-reads kept per grayscale level (median + spread beats a single noisy sample — the
# ColourSpace-style taming of low-light swings, done honestly: the whisker shows the spread).
_GRAY_SAMPLES = 9
# "Core" content zone reference white for HDR (BT.2408 diffuse/graphics white). ~99% of graded
# content lives at or below this and within Rec.709 — the practical-priority ΔE split headline.
_HDR_REF_WHITE_NITS = 203.0
# Warm-up settling: the panel counts as settled when its fastest-moving channel drifts less than
# this (percent per 10 minutes) over the trailing window of drift checkpoints.
_SETTLE_SLOPE_PCT_PER_10MIN = 0.15
_SETTLE_WINDOW_S = 600.0


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


def _is_neutral(rgb: Optional[list]) -> bool:
    """A grayscale/white patch: all channels equal and non-black."""
    if not rgb or len(rgb) < 3:
        return False
    return rgb[0] == rgb[1] == rgb[2] and rgb[0] > 0


def _patch_key(data: dict[str, Any]) -> tuple:
    """A patch's identity for the latest-wins accumulators (live ΔE, CIE scatter): the
    normalised signal when present (stable across re-reads AND across stages, so a verify
    read replaces the same patch's raw read), else label+rgb, else label+seq."""
    sig = data.get("signal")
    if sig and len(sig) >= 3:
        try:
            return ("sig",) + tuple(round(float(c), 4) for c in sig[:3])
        except (TypeError, ValueError):
            pass
    rgb = data.get("rgb")
    if rgb and len(rgb) >= 3:
        return ("rgb", data.get("label"), tuple(rgb[:3]))
    return ("seq", data.get("label"), data.get("seq"))


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _fold_gray_sample(gray_map: dict, level: float, sample: dict) -> None:
    """Fold one neutral read into a level's SAMPLE RING (median-of-N, not latest-wins): repeat
    reads at the same level tame meter noise — the dominant term near black — and the retained
    spread becomes the chart's uncertainty whisker. A fresh read on a ``carried`` entry (seeded
    from the previous stage) starts a NEW ring: a re-measure through a new correction state is a
    different population, never averaged with the old one."""
    cur = gray_map.get(level)
    if cur is None or cur.get("carried"):
        gray_map[level] = {"signal": level, "samples": [sample]}
    else:
        cur["samples"] = (cur.get("samples") or [])[-(_GRAY_SAMPLES - 1):] + [sample]


# Wire precision per metric. One metric per mode (dE_ITP for HDR, CIEDE2000 for SDR); both ride the
# 1≈JND scale, so they share one precision — 3 dp on the wire, 2 dp in the UI.
_DE_DECIMALS = {"itp": 3, "de2000": 3}


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
    # The run's planned, ordered pipeline (from the run_header's ``stage_plan``: the exact stage
    # sequence the chosen flow will walk, so the dashboard can show "stage K of N" with the upcoming
    # steps — not just the current name). Each item is {"key", "label", "long"?}. Empty until the
    # header carries it.
    stage_plan: list[dict[str, Any]] = field(default_factory=list)
    # Stages actually entered (in first-seen order), so the stepper can mark a planned stage done vs
    # current vs upcoming even when a flow skips a step.
    stages_started: list[str] = field(default_factory=list)

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
    # Each value is {"m": {metric: scalar}, "tx", "ty"} (the per-metric ΔEs + the target
    # chromaticity for the in/out-of-gamut split) so the selectable view metric can roll its own
    # stats without a server round-trip.
    _live_de: dict[Any, dict] = field(default_factory=dict)
    # The last MEASUREMENT patch's {metric: scalar} (drives the "last" reading in the dE tile).
    _live_last: dict[str, float] = field(default_factory=dict)

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
    # CONTINUITY: a new stage's buckets are SEEDED from the previous stage's points, each flagged
    # ``carried`` — the charts never restart empty at a stage boundary; every patch visibly
    # morphs from its old reading to its new one as it's re-measured (carried entries render
    # faded and are overwritten latest-wins, keyed by patch identity).
    _cie_by_stage: dict = field(default_factory=dict)       # stage → {patch key: latest point}
    _gray_by_stage: dict = field(default_factory=dict)      # stage → {level: latest neutral sample}
    _color_by_stage: dict = field(default_factory=dict)     # stage → {signal: latest colour sample}
    _stage_seq: list = field(default_factory=list)          # measurement stages, first-seen order
    _carried_from: dict = field(default_factory=dict)       # stage → the stage it was seeded from
    # LIVE BUILD PREVIEW. The 3D-LUT build re-measures the panel through each candidate cube as
    # ``probe`` reads — deliberately EXCLUDED from the settled snapshot charts (transient, adaptively
    # sampled, not a deliverable). But excluding them left the main graphs frozen for the whole
    # (longest) phase. So probes feed a SEPARATE preview bucket here, rendered into the CIE/grayscale/
    # EOTF tiles ONLY while building, clearly badged "build preview — not final"; the settled charts
    # still hold the last real measurement stage and the verify reads replace the preview at the end.
    _preview_cie: dict = field(default_factory=dict)        # probe-stage → {patch key: latest point}
    _preview_gray: dict = field(default_factory=dict)       # probe-stage → {level: latest neutral}
    _preview_seq: list = field(default_factory=list)        # probe (build) stages, first-seen order
    _last_probe_iso: Optional[str] = None                   # newest probe read (build-preview freshness)
    _last_settled_iso: Optional[str] = None                 # newest charted real measurement read
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
            # The planned pipeline (the chosen flow's ordered stage list) rides the header so the
            # stepper can show the WHOLE run, not just the current stage. Latch the first non-empty
            # one (early headers may predate the flow being resolved).
            plan = data.get("stage_plan")
            if isinstance(plan, list) and plan:
                self.stage_plan = [p for p in plan if isinstance(p, dict)]
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
            if ev.stage and ev.stage not in self.stages_started:
                self.stages_started.append(ev.stage)
            # A stage boundary is real progress and resets the per-stage rate window — the
            # patch counter restarts at ~0, so an ETA computed across the reset is garbage.
            self._mark_progress(ev.time)
            self._progress_marks.clear()
            self._live_de.clear()       # the live ΔE header tracks the CURRENT stage's patches
            self._live_last.clear()
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
            self._optimizer_history.append({**data, "elapsed_s": self._elapsed_at(ev.time)})
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
        deltas = self._patch_deltas(data) if (ok and has_xy) else None
        if ok and has_xy:
            enriched = neutral_metrics(float(xy[0]), float(xy[1]))
            self._mark_progress(ev.time)   # a good read is forward progress
            self._clear_alarm()
            # deltas carries the patch's target xy + scoring ΔE → the CIE scatter can draw each
            # point's error vector to where it SHOULD sit and show ΔE on hover.
            self._accumulate_charts(ev, data, float(xy[0]), float(xy[1]), Y, enriched, deltas)
        de = deltas["metrics"][deltas["scoring"]]["de"] if deltas else None
        self.last_read = {
            "seq": data.get("seq"), "role": data.get("role"), "label": data.get("label"),
            "rgb": data.get("rgb"), "signal": data.get("signal"), "Y": Y, "xy": xy, "ok": ok,
            "neutral": _is_neutral(data.get("rgb")), "de": de, "deltas": deltas,
            "disposition": data.get("disposition"), **enriched,
        }
        # Live ΔE (header reading): only settled MEASUREMENT reads, not warm-up/probe. Keyed by
        # patch identity (signal → else label/seq) so a re-read overwrites instead of double-counting.
        # Store every metric's scalar so the selectable view can roll its own avg/max client-side.
        if deltas is not None and data.get("role") not in ("warmup", "probe") \
                and data.get("disposition") != "probe":
            key = _patch_key(data)
            mdict = {m: v["de"] for m, v in deltas["metrics"].items()}
            tgt = deltas.get("target")
            # Carry the target chromaticity + luminance so the rolling summary can classify the
            # patch in- vs out-of-gamut against the panel's measured native gamut, and (HDR)
            # core vs limits against the content zone (done at summary time, so patches read
            # before the native primaries are known get classified retroactively).
            self._live_de[key] = {"m": mdict, "tx": (tgt or {}).get("x"),
                                  "ty": (tgt or {}).get("y"), "tY": (tgt or {}).get("Y")}
            self._live_last = mdict
        # The most recent neutral read drives the live white-point readout (needs a usable xy
        # so the readout shape stays consistent with the enrichment gate above).
        if ok and has_xy and _is_neutral(data.get("rgb")):
            self.last_white = {"xy": xy, "Y": Y, "rgb": data.get("rgb"), **enriched}

    def _live_de_summary(self) -> dict[str, Any]:
        """Rolling per-patch ΔE for the LIVE header (current stage) — updates every patch, distinct
        from the per-stage authoritative ``de`` from metrics_scored. For every available metric it
        carries avg/max over ALL patches, the LAST patch's value, and the same split by whether the
        patch's target is reachable on this panel: ``in`` (the quality that matters) vs ``oog``
        (out-of-gamut, where a large ΔE is expected clipping, not a calibration miss). ``gamut_known``
        is False until the native primaries are measured — the client then shows the combined avg/max
        and marks the OOG split pending rather than mislabelling everything in-gamut."""
        vals = list(self._live_de.values())
        hdr = _is_hdr_header(self.header)
        # One metric per mode — dE_ITP for HDR, CIEDE2000 for SDR (no alternate viewing lens).
        order = ("itp",) if hdr else ("de2000",)
        native = self._native_primaries()
        tri = ((tuple(native["r"]), tuple(native["g"]), tuple(native["b"]))
               if native and all(k in native for k in ("r", "g", "b")) else None)

        def _stats(xs: list[float], dec: int) -> dict[str, Any]:
            return ({"avg": round(sum(xs) / len(xs), dec), "max": round(max(xs), dec), "n": len(xs)}
                    if xs else {"avg": None, "max": None, "n": 0})

        # HDR "core" content zone: within Rec.709 AND at/below the diffuse/graphics reference
        # white (BT.2408, ~203 nits) — where ~99% of graded content lives. The core numbers are
        # the practical verdict; the in-gamut remainder is "limits" (impressive, rarely hit).
        # SDR targets are all sRGB-inside by construction, so the split is HDR-only.
        tri709 = (tuple(_SRGB_PRIMARIES["r"]), tuple(_SRGB_PRIMARIES["g"]),
                  tuple(_SRGB_PRIMARIES["b"]))
        core_y_cap = _HDR_REF_WHITE_NITS * 1.02   # small headroom so ref-white itself is core

        metrics: dict[str, Any] = {}
        for m in order:
            dec = _DE_DECIMALS.get(m, 3)
            allv, ins, oog, core, ext = [], [], [], [], []
            for v in vals:
                de = v["m"].get(m)
                if de is None:
                    continue
                allv.append(de)
                if tri is not None and v.get("tx") is not None and v.get("ty") is not None:
                    txy = (v["tx"], v["ty"])
                    if point_in_triangle(txy, *tri):
                        ins.append(de)
                        if hdr:
                            t_y = v.get("tY")
                            is_core = point_in_triangle(txy, *tri709) \
                                and (t_y is None or t_y <= core_y_cap)
                            (core if is_core else ext).append(de)
                    else:
                        oog.append(de)
            last = self._live_last.get(m)
            metrics[m] = {**_stats(allv, dec), "last": round(last, dec) if last is not None else None,
                          "in": _stats(ins, dec), "oog": _stats(oog, dec)}
            if hdr and tri is not None:
                metrics[m]["core"] = _stats(core, dec)
                metrics[m]["ext"] = _stats(ext, dec)
        return {"scoring": "itp" if hdr else "de2000", "n": len(vals),
                "gamut_known": tri is not None, "metrics": metrics}

    def _patch_deltas(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """All applicable per-patch ΔE metrics (each with its L/C/H split) vs the patch's ideal
        target at the resolved white — dashboard.colorimetry.patch_deltas, whose scoring metric is
        cross-validated against the spine's scorer. Needs the patch's normalised ``signal`` + a
        good xy/Y; ``None`` otherwise. Values are rounded per-metric for the wire."""
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
            d = patch_deltas([float(c) for c in sig[:3]], x, y, big_y,
                             is_hdr=_is_hdr_header(self.header), white_xy=white,
                             luminance=lum, gamma=gamma)
        except Exception:  # noqa: BLE001 — a monitoring number must never break ingest
            return None
        if d is None:
            return None
        tgt = d.get("target")
        return {"scoring": d["scoring"],
                "target": ({"x": round(tgt["x"], 4), "y": round(tgt["y"], 4),
                            "Y": round(tgt["Y"], 3)} if tgt else None),
                "metrics": {
                    m: {k: round(val, _DE_DECIMALS.get(m, 3)) for k, val in metric.items()}
                    for m, metric in d["metrics"].items()}}

    def _patch_delta_e(self, data: dict[str, Any]) -> Optional[float]:
        """The single scoring-metric per-patch ΔE (dE_ITP for HDR, CIEDE2000 for SDR) — the dense
        event log shows one number, not the full split."""
        d = self._patch_deltas(data)
        return None if d is None else d["metrics"][d["scoring"]]["de"]

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
                           Y: Any, enriched: dict[str, Any],
                           deltas: Optional[dict[str, Any]] = None) -> None:
        """Fold a good read into the bounded chart datasets (served via /api/charts)."""
        role = data.get("role")
        disposition = data.get("disposition")
        rgb = data.get("rgb")
        sig = data.get("signal")
        neutral = _is_neutral(rgb)
        is_probe = role == "probe" or disposition == "probe"
        if is_probe:
            # The 3D-LUT build's re-measure-through-the-candidate-cube reads. They never touch the
            # settled snapshot charts or the drift series (transient, adaptively sampled), but they
            # DO feed the live build preview so the main graphs move during the longest phase.
            self._accumulate_preview(ev, data, x, y, Y, enriched, deltas, neutral)
            return
        # Drift series (cross-stage TIME series) for the per-channel thermal-drift chart. A FIXED
        # near-white neutral re-measured over time — the clean signal: the warm-up conditioning
        # reads (when thermal drift is LARGEST) + the dedicated neutral_ref checkpoints. The whole
        # grayscale ramp (every measurement neutral, all levels) is NOT drift: dark/mid steps are
        # noisy and would bury the trace; a white-level (>=0.9) measurement neutral is the fallback
        # for runs that emit no warm-up / neutral_ref reads. xy rides along so the chart can split
        # the read into per-channel linear RGB and track which channel drifts as the panel warms.
        if not is_probe:
            sig_level = _as_float(sig[0]) if (sig and len(sig) >= 1) else None
            sample = {"elapsed_s": self._elapsed_at(ev.time), "signal": sig_level,
                      "cct": enriched.get("cct"), "duv": enriched.get("duv"), "Y": Y,
                      "x": round(x, 5), "y": round(y, 5)}
            if role in ("warmup", "neutral_ref"):
                self._white_track.append(sample)
            elif neutral and sig_level is not None and sig_level >= 0.9:
                self._white_fallback.append(sample)
        # Snapshot charts (latest measurement stage only): exclude warm-up, drift-ref, build-probe.
        stage = self._chart_stage(ev, data)
        if stage is None:
            return
        # A settled measurement read is newer ground truth than any build preview — once verify
        # reads start landing, this advances past the last probe and the preview yields the tiles.
        self._last_settled_iso = ev.time
        if stage not in self._cie_by_stage:
            self._open_stage_bucket(stage)
        tgt = (deltas or {}).get("target")
        de = deltas["metrics"][deltas["scoring"]]["de"] if deltas else None
        cie = self._cie_by_stage[stage]
        key = _patch_key(data)
        if key not in cie and len(cie) >= _CIE_CAP:
            cie.pop(next(iter(cie)))
        cie[key] = {
            "x": round(x, 5), "y": round(y, 5), "role": role, "neutral": neutral,
            "c": (None if neutral else _sig_hex(sig)) if sig else None,
            # intended (signal) + measured display colour → the worst-patches split swatches
            "sc": _sig_hex(sig) if sig else None, "mc": self._measured_hex(x, y, Y),
            # target chromaticity + scoring ΔE → the scatter draws the error vector and shows ΔE on hover
            "tx": round(tgt["x"], 5) if tgt else None, "ty": round(tgt["y"], 5) if tgt else None,
            "de": de, "label": data.get("label")}
        level = _as_float(sig[0]) if (neutral and sig and len(sig) >= 1) else None
        if level is not None:
            _fold_gray_sample(self._gray_by_stage[stage], round(level, 5), {
                "Y": Y, "x": round(x, 5), "y": round(y, 5),
                "cct": enriched.get("cct"), "duv": enriched.get("duv"), "de": de})
        elif not neutral and sig and len(sig) >= 3 and Y is not None:
            # a colour patch: keep the latest measured Y per distinct signal for the
            # Colour Luminance chart (luminance error vs target is derived in charts()).
            try:
                key = (round(float(sig[0]), 4), round(float(sig[1]), 4), round(float(sig[2]), 4))
                self._color_by_stage[stage][key] = {"signal": list(key), "Y": Y,
                                                    "x": round(x, 5), "y": round(y, 5)}
            except (TypeError, ValueError):
                pass

    def _open_stage_bucket(self, stage: str) -> None:
        """Open a new measurement stage's chart buckets, SEEDED from the previous stage's points
        (each flagged ``carried``) — the graphs don't restart at a stage boundary; they morph.
        A fresh read at the same patch identity / grayscale level overwrites its carried twin
        (written without the flag), so the charts visibly converge to the new stage's state and
        the ``continuity`` payload can report how much has been re-measured so far."""
        prev = self._stage_seq[-1] if self._stage_seq else None
        if prev is not None:
            self._cie_by_stage[stage] = {k: {**p, "carried": True}
                                         for k, p in self._cie_by_stage[prev].items()}
            # sample rings are copied, not shared — a late read in the source stage must
            # never silently mutate the carried snapshot
            self._gray_by_stage[stage] = {k: {**g, "samples": list(g.get("samples") or []),
                                              "carried": True}
                                          for k, g in self._gray_by_stage[prev].items()}
            self._color_by_stage[stage] = {k: {**c, "carried": True}
                                           for k, c in self._color_by_stage[prev].items()}
            self._carried_from[stage] = prev
        else:
            self._cie_by_stage[stage] = {}
            self._gray_by_stage[stage] = {}
            self._color_by_stage[stage] = {}
        self._stage_seq.append(stage)

    def _accumulate_preview(self, ev: Event, data: dict[str, Any], x: float, y: float,
                            Y: Any, enriched: dict[str, Any], deltas: Optional[dict[str, Any]],
                            neutral: bool) -> None:
        """Fold one BUILD PROBE read into the live build-preview buckets (CIE scatter + grayscale),
        so the main graphs animate the cube converging during the 3D-LUT build. Keyed by the build
        stage; latest-wins per grayscale level / per CIE point identity, mirroring the snapshot
        accumulators so the SAME chart builders render it. Seeded from the last SETTLED stage
        (flagged ``carried``) so the preview starts from what the cube is correcting, not blank."""
        stage = ev.phase or ev.stage or "build"
        if stage not in self._preview_cie:
            settled = self._stage_seq[-1] if self._stage_seq else None
            self._preview_cie[stage] = ({k: {**p, "carried": True}
                                         for k, p in self._cie_by_stage[settled].items()}
                                        if settled else {})
            self._preview_gray[stage] = ({k: {**g, "samples": list(g.get("samples") or []),
                                              "carried": True}
                                          for k, g in self._gray_by_stage[settled].items()}
                                         if settled else {})
            self._preview_seq.append(stage)
        self._last_probe_iso = ev.time
        sig = data.get("signal")
        tgt = (deltas or {}).get("target")
        de = deltas["metrics"][deltas["scoring"]]["de"] if deltas else None
        cie = self._preview_cie[stage]
        key = _patch_key(data)
        if key not in cie and len(cie) >= _CIE_CAP:
            cie.pop(next(iter(cie)))
        cie[key] = {
            "x": round(x, 5), "y": round(y, 5), "role": "probe", "neutral": neutral,
            "c": (None if neutral else _sig_hex(sig)) if sig else None,
            "sc": _sig_hex(sig) if sig else None, "mc": self._measured_hex(x, y, Y),
            "tx": round(tgt["x"], 5) if tgt else None, "ty": round(tgt["y"], 5) if tgt else None,
            "de": de, "label": data.get("label")}
        level = _as_float(sig[0]) if (neutral and sig and len(sig) >= 1) else None
        if level is not None:
            _fold_gray_sample(self._preview_gray[stage], round(level, 5), {
                "Y": Y, "x": round(x, 5), "y": round(y, 5),
                "cct": enriched.get("cct"), "duv": enriched.get("duv"), "de": de})

    def _measured_hex(self, x: float, y: float, Y: Any) -> Optional[str]:
        """The patch's APPROXIMATE on-screen colour as actually measured (xyY → target-space RGB →
        gamma-encoded hex, normalised to the header's reference luminance). A swatch, not
        colorimetry: good enough to SEE a miss next to the intended colour in the worst-patches
        tile; the numbers next to it stay the authority."""
        big_y = _as_float(Y)
        if big_y is None:
            return None
        hdr = _is_hdr_header(self.header)
        white = (self.header.get("white") or {}).get("xy") or (0.3127, 0.3290)
        ref = _as_float(self.header.get("luminance")) or (1000.0 if hdr else 120.0)
        if hdr:
            ref = min(ref, _HDR_REF_WHITE_NITS)   # swatches keyed to diffuse white, not peak
        lin = linear_rgb(x, y, big_y, is_hdr=hdr, white_xy=tuple(white[:2]))
        if lin is None or ref <= 0:
            return None
        chans = [max(0, min(255, int(round((max(0.0, min(1.0, c / ref)) ** (1 / 2.2)) * 255))))
                 for c in lin]
        return "#{:02x}{:02x}{:02x}".format(*chans)

    def _ingest_metrics(self, ev: Event, data: dict[str, Any]) -> None:
        entry = {
            "phase": data.get("label") or data.get("phase") or ev.stage,
            "elapsed_s": self._elapsed_at(ev.time),
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

    def _gray_rows(self, gray_map: dict, *, hdr: bool, bal_white: tuple) -> list[dict[str, Any]]:
        """Chart-ready grayscale rows from a level→sample-ring map: the MEDIAN of each level's
        retained re-reads (one noisy sample can no longer swing the trace) plus the CCT/Duv
        spread (``*_lo``/``*_hi`` → the uncertainty whisker, only when n > 1) and the level's
        ΔE vs its neutral target — the perceptually honest number for near-black levels, where
        CCT is a ratio of noise but "can you see the tint?" still has an answer."""
        rows: list[dict[str, Any]] = []
        for k in sorted(gray_map):
            e = gray_map[k]
            ss = e.get("samples") or []
            if not ss:
                continue

            def med(key: str) -> Optional[float]:
                return _median([s[key] for s in ss if s.get(key) is not None])

            x, y, Y = med("x"), med("y"), med("Y")
            bal = rgb_balance(x, y, Y, is_hdr=hdr, white_xy=bal_white)
            ccts = [s["cct"] for s in ss if s.get("cct") is not None]
            duvs = [s["duv"] for s in ss if s.get("duv") is not None]
            de = med("de")
            row = {"signal": e["signal"], "Y": round(Y, 4) if Y is not None else None,
                   "x": round(x, 5) if x is not None else None,
                   "y": round(y, 5) if y is not None else None,
                   "cct": round(med("cct"), 1) if ccts else None,
                   "duv": round(med("duv"), 5) if duvs else None,
                   "de": round(de, 3) if de is not None else None,
                   "n": len(ss),
                   "cct_lo": round(min(ccts), 1) if len(ccts) > 1 else None,
                   "cct_hi": round(max(ccts), 1) if len(ccts) > 1 else None,
                   "duv_lo": round(min(duvs), 5) if len(duvs) > 1 else None,
                   "duv_hi": round(max(duvs), 5) if len(duvs) > 1 else None,
                   "dim": (Y is not None and Y < _GRAY_CCT_Y_FLOOR_NITS),
                   "r": round(bal[0], 2) if bal else None,
                   "g": round(bal[1], 2) if bal else None,
                   "b": round(bal[2], 2) if bal else None}
            if e.get("carried"):
                row["carried"] = True
            rows.append(row)
        return rows

    def charts(self) -> dict[str, Any]:
        """Chart-ready datasets, built from the bounded accumulators. Served via
        /api/charts (NOT the SSE state) so the heavy scatter stays off the fast path.
        The snapshot charts reflect the LATEST measurement stage (not all stages overlaid);
        the drift chart is the cross-stage time series. Everything is already numeric/derived
        — the browser only draws SVG."""
        stage = self._latest_chart_stage()
        cie_points = list(self._cie_by_stage.get(stage, {}).values()) if stage else []
        gray_map = self._gray_by_stage.get(stage, {}) if stage else {}
        # Continuity: how much of this stage's view is still carried from the previous stage vs
        # already re-measured — the frontend shows "N re-measured · M from <prev>" and fades the
        # carried marks, so a stage boundary reads as the graphs UPDATING, not restarting.
        carried_n = sum(1 for p in cie_points if p.get("carried"))
        hdr = _is_hdr_header(self.header)
        white = (self.header.get("white") or {}).get("xy")
        bal_white = tuple(white) if (white and len(white) >= 2) else (0.3127, 0.3290)
        gray = self._gray_rows(gray_map, hdr=hdr, bal_white=bal_white)
        color_map = self._color_by_stage.get(stage, {}) if stage else {}
        gamma = self.header.get("gamma") or 2.2
        luminance = self.header.get("luminance")
        return {
            "stage": stage,                       # the measurement stage these snapshot charts reflect
            "stages": list(self._stage_seq),
            # The TARGET white's own Duv. D65 sits ≈ +0.003 ABOVE the Planckian locus, so a
            # perfectly calibrated panel does NOT read Duv 0 — the Duv chart's norm corridor
            # must centre here, or a perfect D65 white gets flagged as a green cast.
            "target_duv": (neutral_metrics(float(white[0]), float(white[1])).get("duv")
                           if white and len(white) >= 2 else None),
            "continuity": {"from": self._carried_from.get(stage) if stage else None,
                           "carried": carried_n, "fresh": len(cie_points) - carried_n},
            "cie": {
                "points": cie_points,
                "white": white,
                "primaries": _REC2020_PRIMARIES if hdr else _SRGB_PRIMARIES,
                "gamut_label": "Rec.2020" if hdr else "Rec.709 / sRGB",
                # The panel's MEASURED primaries for the stage these charts reflect (the full-drive
                # RGB CORNER reads), so the frontend can overlay actual coverage on the STANDARD
                # target gamut — the "standard vs measured" view (#20). This is the CURRENT stage
                # (post-MHC while profiling, verify once verified), matching the scatter — NOT the
                # raw native gamut (which the OOG split below still uses). None until all three
                # corners have an above-noise read.
                "measured": self._corner_primaries(stage),
                "locus": [[round(x, 5), round(y, 5)] for (x, y) in planckian_locus_xy()],
            },
            "grayscale": gray,
            "eotf": {
                "kind": "pq" if hdr else "power",
                "gamma": gamma,
                "luminance": luminance,
                "reference": self._eotf_reference(hdr=hdr, gamma=gamma, luminance=luminance),
                "points": [{"signal": g["signal"], "Y": g["Y"], "de": g.get("de"),
                            "carried": bool(g.get("carried"))}
                           for g in gray if g.get("Y") is not None],
            },
            "color_lum": self._color_luminance(color_map, gray, gamma, hdr=hdr, luminance=luminance),
            # Live build preview (probe reads during the 3D-LUT build) — the main graphs keep moving
            # through the longest phase. ``active`` tells the frontend to render it over the (frozen)
            # snapshot tiles with a "build preview — not final" badge.
            "build_preview": self._build_preview(hdr=hdr, white=white, gamma=gamma, luminance=luminance),
            "optimizer": list(self._optimizer_history),
            "white_track": list(self._drift_series()),
            "channel_drift": self._channel_drift(hdr, white),
            # Worst patches THIS stage (fresh reads only — a carried miss belongs to the previous
            # stage's story): intended vs measured swatch + ΔE, largest first.
            "offenders": sorted(
                ({"label": p.get("label"), "de": p["de"], "sc": p.get("sc"),
                  "mc": p.get("mc"), "neutral": bool(p.get("neutral"))}
                 for p in cie_points if p.get("de") is not None and not p.get("carried")),
                key=lambda o: -o["de"])[:6],
            # ΔE-over-the-run series for the convergence tile: build-iteration measurements +
            # scored verify passes, merged in time order — the "watch it get better" chart.
            "convergence": sorted(
                [{"elapsed_s": o.get("elapsed_s"),
                  "avg": _as_float(o.get("measured_mean_de")),
                  "max": _as_float(o.get("measured_max_de")),
                  "kind": "build", "label": f"iter {o.get('iteration')}"}
                 for o in self._optimizer_history]
                + [{"elapsed_s": d.get("elapsed_s"), "avg": _as_float(d.get("avg")),
                    "max": _as_float(d.get("max")), "kind": "scored",
                    "label": str(d.get("phase") or "scored")}
                   for d in self.de_history],
                key=lambda e: e.get("elapsed_s") or 0.0),
        }

    def _drift_series(self) -> Deque[dict]:
        """The thermal-drift time series: the warm-up + neutral_ref checkpoints when the run has
        them (a fixed near-white neutral re-measured over time — the clean signal), else the
        white-level measurement-neutral fallback. Never the full grayscale ramp (low/mid is noise)."""
        return self._white_track if self._white_track else self._white_fallback

    def _channel_drift(self, hdr: bool, white: Optional[list]) -> list[dict[str, Any]]:
        """Per-channel R/G/B drift (% from the first checkpoint) over elapsed time — the ColourSpace-
        style thermal-stability read that exposes WHICH channel wanders as the panel warms (a cool
        blue channel drifts up while R/G hold). Each drift-series checkpoint is split into its linear
        RGB contributions; every channel is then referenced to its own first reading, so a flat trace
        = settled and a climbing one = still drifting. The shared baseline means overall warm-up shows
        as all three rising together, while a single fluctuating channel separates out."""
        bal_white = tuple(white) if (white and len(white) >= 2) else (0.3127, 0.3290)
        out: list[dict[str, Any]] = []
        base: Optional[tuple] = None
        for s in self._drift_series():
            if s.get("elapsed_s") is None:
                continue
            lin = linear_rgb(s.get("x"), s.get("y"), s.get("Y"), is_hdr=hdr, white_xy=bal_white)
            if lin is None or min(lin) <= 0:
                continue
            if base is None:
                base = lin
            out.append({"elapsed_s": s["elapsed_s"], "cct": s.get("cct"), "Y": s.get("Y"),
                        "r": round((lin[0] / base[0] - 1.0) * 100.0, 3),
                        "g": round((lin[1] / base[1] - 1.0) * 100.0, 3),
                        "b": round((lin[2] / base[2] - 1.0) * 100.0, 3)})
        return out

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

    def _corner_primaries(self, stage: Optional[str]) -> Optional[dict[str, list[float]]]:
        """The panel's measured R/G/B primary chromaticities for ``stage`` — the full-drive pure-
        channel CORNER reads ([1,0,0], [0,1,0], [0,0,1]). ``None`` until all three corners have a
        usable (above-noise) read.

        A "corner" is a pure-hue read (one channel non-zero, the other two ~0). Among the pure-hue
        reads of a channel we take the HIGHEST-DRIVE one (max signal component) and, on ties, the
        brightest — mirroring the engine's ``channel_model().peak_xyz`` (highest-Y sample). Drive
        matters because the gamut visibly CONTRACTS with drive under panel sub-additivity, so a
        partial-drive read sits well inside the true corner; preferring max drive snaps the vertex
        to the real corner as soon as the full-saturation anchor is measured. Selecting by drive
        (not bare saturation) also avoids keeping a near-black read whose chromaticity is pure noise
        (every pure-channel patch classifies as ~100% saturated regardless of level)."""
        if not stage:
            return None
        color_map = self._color_by_stage.get(stage, {})
        # family -> (drive, Y, [x, y]); prefer the highest-drive corner and, on ties, brightest.
        best: dict[str, tuple[float, float, list[float]]] = {}
        for c in color_map.values():
            sig, x, y, Y = c.get("signal"), c.get("x"), c.get("y"), c.get("Y")
            if x is None or y is None or Y is None or not sig or len(sig) < 3:
                continue
            s = [float(v) for v in sig[:3]]
            drive = max(s)
            if drive <= 0:
                continue
            nr, ng, nb = (v / drive for v in s)
            if ng < 0.05 and nb < 0.05 and nr > 0.95:
                family = "R"
            elif nr < 0.05 and nb < 0.05 and ng > 0.95:
                family = "G"
            elif nr < 0.05 and ng < 0.05 and nb > 0.95:
                family = "B"
            else:
                continue
            prev = best.get(family)
            if prev is None or drive > prev[0] + 1e-6 or (abs(drive - prev[0]) <= 1e-6 and Y > prev[1]):
                best[family] = (drive, float(Y), [round(x, 5), round(y, 5)])
        if not all(f in best for f in ("R", "G", "B")):
            return None
        # Suppress sub-noise corners: a chosen read far below the brightest corner (or below an
        # absolute floor) has unreliable chromaticity — hide the whole overlay rather than plot a
        # primary at a meaningless point.
        brightest = max(best[f][1] for f in ("R", "G", "B"))
        floor = max(_NATIVE_PRIMARY_Y_FLOOR_ABS, brightest * _NATIVE_PRIMARY_Y_FLOOR_FRAC)
        if any(best[f][1] < floor for f in ("R", "G", "B")):
            return None
        return {f.lower(): best[f][2] for f in ("R", "G", "B")}

    def _native_primaries(self) -> Optional[dict[str, list[float]]]:
        """The panel's RAW native gamut — the corner primaries from the FIRST-measured stage,
        independent of which corrected stage the scatter currently shows. Used for the OOG ΔE
        split (is a target reachable on this panel?), where the physical native envelope is the
        right reference. For a ``3dlut-only`` run the first stage is already post-MHC (no raw
        stage exists), so it degrades to that panel-through-MHC envelope."""
        return self._corner_primaries(self._stage_seq[0] if self._stage_seq else None)

    def _build_preview(self, *, hdr: bool, white: Any, gamma: Any,
                       luminance: Any) -> dict[str, Any]:
        """The live build-preview payload for the 3D-LUT build phase: the panel measured THROUGH
        the current candidate cube (probe reads), shaped exactly like the snapshot ``cie`` /
        ``grayscale`` / ``eotf`` keys so the same chart builders render it.

        ``active`` is True only while the build is the freshest activity — i.e. the newest probe is
        more recent than the newest settled measurement read. Once the verify stage starts landing
        real reads, ``_last_settled_iso`` overtakes ``_last_probe_iso`` and the preview yields the
        tiles back to the (now-updated) settled charts automatically."""
        stage = self._preview_seq[-1] if self._preview_seq else None
        active = (self._last_probe_iso is not None
                  and (self._last_settled_iso is None or self._last_probe_iso > self._last_settled_iso)
                  and self.run_status == RUN_RUNNING)
        if stage is None:
            return {"active": False, "stage": None}
        cie_points = list(self._preview_cie.get(stage, {}).values())
        gray_map = self._preview_gray.get(stage, {})
        bal_white = tuple(white) if (white and len(white) >= 2) else (0.3127, 0.3290)
        gray = self._gray_rows(gray_map, hdr=hdr, bal_white=bal_white)
        return {
            "active": active,
            "stage": stage,
            "iterations": len(self._optimizer_history),
            "cie": {
                "points": cie_points,
                "white": white,
                "primaries": _REC2020_PRIMARIES if hdr else _SRGB_PRIMARIES,
                "gamut_label": "Rec.2020" if hdr else "Rec.709 / sRGB",
                "measured": self._corner_primaries(self._stage_seq[-1] if self._stage_seq else None),
                "locus": [[round(x, 5), round(y, 5)] for (x, y) in planckian_locus_xy()],
            },
            "grayscale": gray,
            "eotf": {
                "kind": "pq" if hdr else "power",
                "gamma": gamma,
                "luminance": luminance,
                "reference": self._eotf_reference(hdr=hdr, gamma=gamma, luminance=luminance),
                "points": [{"signal": g["signal"], "Y": g["Y"], "de": g.get("de"),
                            "carried": bool(g.get("carried"))}
                           for g in gray if g.get("Y") is not None],
            },
        }

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

    def _read_spread_ratio(self) -> Optional[float]:
        """How much slower the SLOW reads run vs the typical read (p85/median of the recent
        read-interval window, clamped to [1, 2.5]). Scales the point ETA into an honest upper
        bound — a single number implies precision the meter doesn't deliver (retries, dark-patch
        integration, settling all stretch individual reads)."""
        times = list(self._read_times)
        if len(times) < 5:
            return None
        diffs = sorted((times[i] - times[i - 1]).total_seconds() for i in range(1, len(times)))
        diffs = [d for d in diffs if 0.0 < d < 600.0]
        if len(diffs) < 4:
            return None
        med = _median(diffs)
        if not med or med <= 0:
            return None
        p85 = diffs[min(len(diffs) - 1, int(round(0.85 * (len(diffs) - 1))))]
        return max(1.0, min(2.5, p85 / med))

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

    def _warmup_view(self) -> dict[str, Any]:
        """The warm-up SETTLING VERDICT for the phase spotlight: is the panel still moving, and
        how fast? Fits the trailing window of drift checkpoints (the fixed re-read neutral) and
        reports the fastest-moving channel's slope in %/10 min — so the dashboard can say
        "blue still climbing +0.8%/10 min" or "settled" instead of leaving the operator to
        eyeball a flat-ish line. ``active`` (the spotlight trigger) is True while warm-up
        conditioning reads are the run's latest activity."""
        active = (self.run_status == RUN_RUNNING
                  and (self.last_read or {}).get("role") == "warmup")
        hdr = _is_hdr_header(self.header)
        white = (self.header.get("white") or {}).get("xy")
        drift = self._channel_drift(hdr, white)
        if len(drift) < 2:
            return {"active": active, "settled": None}
        t1 = drift[-1]["elapsed_s"]
        window = [p for p in drift if p["elapsed_s"] >= t1 - _SETTLE_WINDOW_S]
        if len(window) < 2:
            window = drift[-2:]
        dt_min = (window[-1]["elapsed_s"] - window[0]["elapsed_s"]) / 60.0
        if dt_min <= 0:
            return {"active": active, "settled": None}
        slopes = {c: (window[-1][c] - window[0][c]) / dt_min * 10.0 for c in ("r", "g", "b")}
        worst = max(slopes, key=lambda c: abs(slopes[c]))
        return {"active": active,
                "settled": abs(slopes[worst]) < _SETTLE_SLOPE_PCT_PER_10MIN,
                "channel": worst,
                "slope_pct_per_10min": round(slopes[worst], 2),
                "window_min": round(dt_min, 1),
                "checkpoints": len(window)}

    def _pipeline_view(self) -> dict[str, Any]:
        """The pipeline stepper payload: the planned ordered stages, each tagged done / current /
        upcoming, plus 'stage K of N'. ``done`` = a planned stage already entered that isn't the
        current one; ``current`` = the running stage; the rest are ``upcoming``. Resilient to a flow
        that skips a planned stage (it just never flips to done) and to an unplanned stage (it's
        surfaced as current with no index). Empty plan ⇒ a minimal view from the live stage only."""
        plan = self.stage_plan
        started = set(self.stages_started)
        cur = self.stage
        terminal = self.run_status in _TERMINAL_BY_STATUS.values()
        steps: list[dict[str, Any]] = []
        index = None
        for i, p in enumerate(plan):
            key = p.get("key")
            if key == cur and not terminal:
                status = "current"
                index = i
            elif key in started:
                status = "done"
            else:
                status = "upcoming"
            steps.append({"key": key, "label": p.get("label") or key,
                          "long": bool(p.get("long")), "status": status})
        # If the run finished, everything entered is done (no current).
        if terminal:
            for s in steps:
                if s["key"] in started:
                    s["status"] = "done"
        return {"steps": steps, "total": len(plan),
                "index": (index + 1) if index is not None else None,
                "current": cur}

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
        # Honest range: the point ETA assumes every read runs at the median pace; the upper
        # bound scales by the observed slow-read ratio. The ETA covers the CURRENT STAGE only
        # (the client labels it so and states what remains after it from the stage plan).
        spread = self._read_spread_ratio()
        eta_hi = eta * spread if (eta is not None and spread is not None) else None

        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            # The server's clock at snapshot time — lets the browser tick timers/ages locally
            # (1 Hz, between the 2 s pushes) without trusting the client clock's absolute value.
            "now_iso": now.isoformat(timespec="seconds"),
            "header": self.header,
            "phase": self.phase,
            "stage": self.stage,
            "run_status": self.run_status,
            "pipeline": self._pipeline_view(),
            "warmup": self._warmup_view(),
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
                "eta_hi_s": round(eta_hi, 1) if eta_hi is not None else None,
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
