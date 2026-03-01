#!/usr/bin/env python3
"""DesktopLUT framepacer.csv analysis tool.

Parses framepacer CSV logs and produces a comprehensive summary:
segments, phases, drops, lock behavior, idle patterns, and offset statistics.

Usage: python tools/analyze_framepacer.py [path/to/framepacer.csv]
       Default: bin/Release/framepacer.csv
"""

import csv
import sys
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Frame:
    line: int
    frame_num: int
    measured: float
    ema: float
    shadow: float
    locked_offset: float
    state: str  # 'U' or 'L'
    divergence: float
    threshold: float
    spread: float
    stable: int
    alpha: float
    variance: float
    buffer: bool
    drops: int
    dwm_drops: int
    idle_ms: Optional[int]
    outlier: str = ""  # '' = normal, 'D' = DComp drop, 'H' = high, 'L' = low


@dataclass
class PhaseStats:
    name: str
    start_frame: int = 0
    end_frame: int = 0
    start_line: int = 0
    end_line: int = 0
    count: int = 0
    dd_drops: int = 0
    dwm_drops: int = 0
    ema_min: float = float('inf')
    ema_max: float = float('-inf')
    ema_sum: float = 0.0
    measured_min: float = float('inf')
    measured_max: float = float('-inf')
    measured_sum: float = 0.0
    measured_valid: int = 0  # count of measured > 0.5 (non-outlier)
    spread_min: float = float('inf')
    spread_max: float = float('-inf')
    max_stable: int = 0
    low_outliers: int = 0  # measured < 0.5ms
    high_outliers: int = 0  # measured > 2x ema

    def update(self, f: Frame, prev_drops: int, prev_dwm: int):
        self.count += 1
        self.end_frame = f.frame_num
        self.end_line = f.line
        if self.count == 1:
            self.start_frame = f.frame_num
            self.start_line = f.line
        self.ema_min = min(self.ema_min, f.ema)
        self.ema_max = max(self.ema_max, f.ema)
        self.ema_sum += f.ema
        if f.measured > 0.5:
            self.measured_min = min(self.measured_min, f.measured)
            self.measured_max = max(self.measured_max, f.measured)
            self.measured_sum += f.measured
            self.measured_valid += 1
        if f.measured < 0.5 and f.measured >= 0:
            self.low_outliers += 1
        if f.ema > 0 and f.measured > f.ema * 2.5:
            self.high_outliers += 1
        if f.spread >= 0:
            self.spread_min = min(self.spread_min, f.spread)
            self.spread_max = max(self.spread_max, f.spread)
        self.max_stable = max(self.max_stable, f.stable)
        if f.drops > prev_drops:
            self.dd_drops += f.drops - prev_drops
        if f.dwm_drops > prev_dwm:
            self.dwm_drops += f.dwm_drops - prev_dwm

    @property
    def ema_avg(self):
        return self.ema_sum / self.count if self.count else 0

    @property
    def measured_avg(self):
        return self.measured_sum / self.measured_valid if self.measured_valid else 0

    @property
    def duration_s(self):
        return self.count * 0.02085  # approximate at 48Hz, overridden by caller


@dataclass
class Segment:
    index: int
    start_line: int
    end_line: int = 0
    frame_count: int = 0
    idle_at_start: Optional[int] = None
    phases: list = field(default_factory=list)
    frames: list = field(default_factory=list)


@dataclass
class Event:
    line: int
    frame: int
    event_type: str  # 'lock', 'unlock', 'buffer_on', 'buffer_off', 'dd_drop', 'idle_reset'
    detail: str = ""


def parse_csv(path: str) -> list[Frame]:
    frames = []
    with open(path, 'r') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            try:
                idle = int(row['idle_ms']) if 'idle_ms' in row and row['idle_ms'] else None
            except (ValueError, KeyError):
                idle = None
            outlier = row.get('outlier', '').strip() if 'outlier' in row else ''
            frames.append(Frame(
                line=i,
                frame_num=int(row['frame']),
                measured=float(row['measured']),
                ema=float(row['ema']),
                shadow=float(row['shadow']),
                locked_offset=float(row['locked']),
                state=row['state'].strip(),
                divergence=float(row['divergence']),
                threshold=float(row['threshold']),
                spread=float(row['spread']),
                stable=int(row['stable']),
                alpha=float(row['alpha']),
                variance=float(row['var']),
                buffer=row['buffer'].strip() == '1',
                drops=int(row['drops']),
                dwm_drops=int(row['dwm_drops']),
                idle_ms=idle,
                outlier=outlier,
            ))
    return frames


def find_segments(frames: list[Frame]) -> list[Segment]:
    segments = []
    current = None
    for idx, f in enumerate(frames):
        # Split on non-outlier frames with frame_num == 1 (offsetSampleCount reset).
        # Outlier frames (D/H/L) keep their previous frame_num or 0 after baseline
        # reset — they belong to the current segment, not a new one.
        is_new_segment = (f.frame_num == 1 and not f.outlier) or current is None
        if is_new_segment:
            if current is not None:
                current.end_line = frames[idx - 1].line
                current.frame_count = len(current.frames)
            current = Segment(
                index=len(segments) + 1,
                start_line=f.line,
                idle_at_start=f.idle_ms,
            )
            segments.append(current)
        current.frames.append(f)
    if current:
        current.end_line = frames[-1].line
        current.frame_count = len(current.frames)
    return segments


def classify_phase(f: Frame) -> str:
    if f.buffer and f.state == 'L':
        return "buf+lock"
    elif f.buffer:
        return "buf"
    elif f.state == 'L':
        return "lock"
    else:
        return "direct"


PHASE_LABELS = {
    "direct": "Direct (no buffer, no lock)",
    "buf": "Buffer ON (unlocked)",
    "buf+lock": "Buffer ON + Locked",
    "lock": "Locked (no buffer)",
}


def analyze_segment(seg: Segment, period_ms: float) -> tuple[list[PhaseStats], list[Event]]:
    """Analyze phases and events within a segment."""
    phases = []
    events = []
    current_phase_key = None
    current_phase = None
    prev_drops = seg.frames[0].drops if seg.frames else 0
    prev_dwm = seg.frames[0].dwm_drops if seg.frames else 0
    prev_buffer = None
    prev_state = None
    prev_idle = None

    consecutive_outliers = 0
    for f in seg.frames:
        # Track outlier frames as events but don't include in phase stats
        if f.outlier:
            consecutive_outliers += 1
            # Log baseline resets (frame_num goes to 0 after 20+ consecutive outliers)
            if f.frame_num == 0 and consecutive_outliers >= 20:
                events.append(Event(f.line, f.frame_num, 'outlier_reset',
                                    f"baseline reset after {consecutive_outliers}+ outliers, "
                                    f"measured={f.measured:.3f}, reason={f.outlier}"))
            prev_drops = f.drops
            prev_dwm = f.dwm_drops
            continue

        consecutive_outliers = 0
        pk = classify_phase(f)

        # Phase transitions
        if pk != current_phase_key:
            if current_phase is not None:
                phases.append(current_phase)
            current_phase_key = pk
            current_phase = PhaseStats(name=pk)

        current_phase.update(f, prev_drops, prev_dwm)

        # Events
        if prev_buffer is not None:
            if f.buffer and not prev_buffer:
                events.append(Event(f.line, f.frame_num, 'buffer_on',
                                    f"idle={f.idle_ms}ms, ema={f.ema:.3f}"))
            elif not f.buffer and prev_buffer:
                events.append(Event(f.line, f.frame_num, 'buffer_off',
                                    f"idle={f.idle_ms}ms"))

        if prev_state is not None:
            if f.state == 'L' and prev_state == 'U':
                events.append(Event(f.line, f.frame_num, 'lock',
                                    f"ema={f.ema:.3f}, spread={f.spread:.3f}, stable={f.stable}"))
            elif f.state == 'U' and prev_state == 'L':
                events.append(Event(f.line, f.frame_num, 'unlock',
                                    f"ema={f.ema:.3f}, divergence={f.divergence:.3f}"))

        if f.drops > prev_drops:
            n = f.drops - prev_drops
            events.append(Event(f.line, f.frame_num, 'dd_drop',
                                f"+{n}, measured={f.measured:.3f}, ema={f.ema:.3f}, buf={'Y' if f.buffer else 'N'}"))

        # DWM drop bursts
        if f.dwm_drops > prev_dwm:
            n = f.dwm_drops - prev_dwm
            if n >= 100:
                events.append(Event(f.line, f.frame_num, 'dwm_burst',
                                    f"+{n} DWM ID gap (likely mode transition, not real drops)"))
            elif n >= 5:
                events.append(Event(f.line, f.frame_num, 'dwm_burst',
                                    f"+{n} DWM compositor drops"))

        if prev_idle is not None and f.idle_ms is not None:
            if prev_idle >= 2000 and f.idle_ms < 500:
                events.append(Event(f.line, f.frame_num, 'idle_reset',
                                    f"{prev_idle}ms -> {f.idle_ms}ms"))

        prev_drops = f.drops
        prev_dwm = f.dwm_drops
        prev_buffer = f.buffer
        prev_state = f.state
        prev_idle = f.idle_ms

    if current_phase is not None:
        phases.append(current_phase)

    return phases, events


def estimate_refresh_rate(frames: list[Frame]) -> float:
    """Estimate refresh period from the activeDivergence (threshold) column.

    activeDivergence for direct mode = max(0.5, period * 0.035)
    activeDivergence for buffer mode = max(1.0, period * 0.06)
    We can extract period when threshold is above the floor value.
    """
    if not frames:
        return 16.67

    # Direct mode: threshold = max(0.5, period*0.035). Above 0.5 when period > 14.3ms (70Hz)
    direct_thresholds = [f.threshold for f in frames if not f.buffer and f.threshold > 0.6]
    if direct_thresholds:
        avg_t = sum(direct_thresholds) / len(direct_thresholds)
        estimated = avg_t / 0.035
        if 4.0 < estimated < 42.0:
            return estimated

    # Buffer mode: threshold = max(1.0, period*0.06). Above 1.0 when period > 16.7ms (60Hz)
    buf_thresholds = [f.threshold for f in frames if f.buffer and f.threshold > 1.1]
    if buf_thresholds:
        avg_t = sum(buf_thresholds) / len(buf_thresholds)
        estimated = avg_t / 0.06
        if 4.0 < estimated < 42.0:
            return estimated

    return 16.67  # Default 60Hz


def format_phase_table(phases: list[PhaseStats], period_ms: float) -> str:
    lines = []
    header = f"  {'Phase':<30s} {'Frames':>6s} {'~Secs':>6s} {'DD':>4s} {'DWM':>5s} {'EMA range':>14s} {'Meas range':>14s} {'Spread':>12s} {'MaxStb':>6s}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for p in phases:
        dur = p.count * period_ms / 1000.0
        ema_r = f"{p.ema_min:.2f}-{p.ema_max:.2f}" if p.count else "N/A"
        meas_r = f"{p.measured_min:.2f}-{p.measured_max:.2f}" if p.measured_valid else "N/A"
        spr_r = f"{p.spread_min:.2f}-{p.spread_max:.2f}" if p.spread_min != float('inf') else "N/A"
        lines.append(f"  {PHASE_LABELS.get(p.name, p.name):<30s} {p.count:>6d} {dur:>5.1f}s {p.dd_drops:>4d} {p.dwm_drops:>5d} {ema_r:>14s} {meas_r:>14s} {spr_r:>12s} {p.max_stable:>6d}")
    return "\n".join(lines)


def format_events(events: list[Event], max_events: int = 30) -> str:
    lines = []
    for e in events[:max_events]:
        tag = {
            'buffer_on': 'BUF ON ',
            'buffer_off': 'BUF OFF',
            'lock': 'LOCK   ',
            'unlock': 'UNLOCK ',
            'dd_drop': 'DD DROP',
            'idle_reset': 'IDLE   ',
            'dwm_burst': 'DWM    ',
            'outlier_reset': 'OUT RST',
        }.get(e.event_type, e.event_type.upper())
        lines.append(f"  line {e.line:>5d} f={e.frame:>4d}  [{tag}]  {e.detail}")
    if len(events) > max_events:
        lines.append(f"  ... and {len(events) - max_events} more events")
    return "\n".join(lines)


def overall_stats(frames: list[Frame]) -> dict:
    total = len(frames)
    non_outlier = [f for f in frames if not f.outlier]
    outliers = [f for f in frames if f.outlier]
    buf = sum(1 for f in non_outlier if f.buffer)
    locked = sum(1 for f in non_outlier if f.state == 'L')
    dd_start = frames[0].drops if frames else 0
    dd_end = frames[-1].drops if frames else 0
    dwm_start = frames[0].dwm_drops if frames else 0
    dwm_end = frames[-1].dwm_drops if frames else 0
    idle_vals = [f.idle_ms for f in frames if f.idle_ms is not None]
    outlier_d = sum(1 for f in outliers if f.outlier == 'D')
    outlier_h = sum(1 for f in outliers if f.outlier == 'H')
    outlier_l = sum(1 for f in outliers if f.outlier == 'L')
    return {
        'total': total,
        'non_outlier': len(non_outlier),
        'outlier_total': len(outliers),
        'outlier_d': outlier_d,
        'outlier_h': outlier_h,
        'outlier_l': outlier_l,
        'buf_count': buf,
        'buf_pct': buf * 100 / len(non_outlier) if non_outlier else 0,
        'lock_count': locked,
        'lock_pct': locked * 100 / len(non_outlier) if non_outlier else 0,
        'dd_drops': dd_end - dd_start,
        'dwm_drops': dwm_end - dwm_start,
        'idle_max': max(idle_vals) if idle_vals else None,
        'idle_min': min(idle_vals) if idle_vals else None,
        'has_idle': len(idle_vals) > 0,
    }


def analyze_lock_failure(frames: list[Frame], lock_jitter_ms: float) -> str:
    """Why didn't lock engage? Analyze spread vs threshold."""
    buf_unlocked = [f for f in frames if f.buffer and f.state == 'U' and f.spread >= 0]
    if not buf_unlocked:
        return "  No buffer-on unlocked frames to analyze."

    below = sum(1 for f in buf_unlocked if f.spread < lock_jitter_ms)
    above = sum(1 for f in buf_unlocked if f.spread >= lock_jitter_ms)
    max_stable = max(f.stable for f in buf_unlocked)
    spreads = [f.spread for f in buf_unlocked if f.spread >= 0]

    lines = []
    lines.append(f"  lockJitterMs threshold: {lock_jitter_ms:.3f} ms")
    lines.append(f"  Spread below threshold: {below}/{len(buf_unlocked)} frames ({below*100/len(buf_unlocked):.0f}%)")
    lines.append(f"  Spread above threshold: {above}/{len(buf_unlocked)} frames ({above*100/len(buf_unlocked):.0f}%)")
    lines.append(f"  Spread range: {min(spreads):.3f} - {max(spreads):.3f} ms")
    lines.append(f"  Max stableFrameCount: {max_stable} (need 20 for lock)")

    # Find the longest run of stable frames
    max_run = 0
    current_run = 0
    for f in buf_unlocked:
        if f.stable > 0 and (current_run == 0 or f.stable > prev_stable):
            current_run = f.stable
        else:
            max_run = max(max_run, current_run)
            current_run = f.stable
        prev_stable = f.stable
    max_run = max(max_run, current_run)
    lines.append(f"  Longest stable run: {max_run} consecutive frames")

    if max_stable < 20 and above > 0:
        lines.append(f"  Diagnosis: Spread keeps spiking above {lock_jitter_ms:.2f}ms, resetting stable counter")
    elif max_stable >= 20:
        lines.append(f"  Diagnosis: Lock should have engaged (stable reached {max_stable})")

    return "\n".join(lines)


def find_best_segment(segments: list[Segment], min_frames: int = 50) -> Optional[Segment]:
    """Find the longest segment with at least min_frames, preferring later ones."""
    candidates = [s for s in segments if s.frame_count >= min_frames]
    if not candidates:
        return max(segments, key=lambda s: s.frame_count) if segments else None
    # Prefer the longest segment in the later half of the file
    midpoint = len(segments) // 2
    later = [s for s in candidates if s.index > midpoint]
    pool = later if later else candidates
    return max(pool, key=lambda s: s.frame_count)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("bin", "Release", "framepacer.csv")
    if not os.path.exists(path):
        print(f"File not found: {path}")
        sys.exit(1)

    frames = parse_csv(path)
    if not frames:
        print("No frames found in CSV.")
        sys.exit(1)

    segments = find_segments(frames)
    period_ms = estimate_refresh_rate(frames)
    refresh_hz = 1000.0 / period_ms if period_ms > 0 else 0
    stats = overall_stats(frames)

    # Estimate lockJitterMs from period
    lock_jitter_ms = max(0.7, period_ms * 0.07)

    print("=" * 80)
    print(f"FRAMEPACER ANALYSIS — {path}")
    print(f"  {os.path.getsize(path):,} bytes, {len(frames)} frames, {len(segments)} segments")
    print("=" * 80)

    # Overall stats
    print(f"\n--- OVERVIEW ---")
    print(f"  Total frames:  {stats['total']}")
    if stats['outlier_total'] > 0:
        print(f"    Normal:      {stats['non_outlier']}")
        print(f"    Outliers:    {stats['outlier_total']} "
              f"(D={stats['outlier_d']}, H={stats['outlier_h']}, L={stats['outlier_l']})")
    print(f"  Refresh rate:  {refresh_hz:.3f} Hz ({period_ms:.3f} ms)")
    print(f"  Buffer active: {stats['buf_count']} ({stats['buf_pct']:.1f}%)")
    print(f"  Cadence locked:{stats['lock_count']} ({stats['lock_pct']:.1f}%)")
    print(f"  DD drops:      {stats['dd_drops']}")
    print(f"  DWM drops:     {stats['dwm_drops']}")
    if stats['has_idle']:
        print(f"  Idle range:    {stats['idle_min']}-{stats['idle_max']} ms")

    # Segment map
    print(f"\n--- SEGMENTS ({len(segments)}) ---")
    for seg in segments:
        idle_str = f"idle={seg.idle_at_start}ms" if seg.idle_at_start is not None else ""
        seg_period = estimate_refresh_rate(seg.frames)
        seg_hz = 1000.0 / seg_period if seg_period > 0 else 0
        non_outlier = [f for f in seg.frames if not f.outlier]
        outlier_count = sum(1 for f in seg.frames if f.outlier)
        buf_count = sum(1 for f in non_outlier if f.buffer)
        lock_count = sum(1 for f in non_outlier if f.state == 'L')
        dd = seg.frames[-1].drops - seg.frames[0].drops if seg.frames else 0
        markers = []
        if buf_count > 0:
            markers.append(f"buf={buf_count}")
        if lock_count > 0:
            markers.append(f"lock={lock_count}")
        if dd > 0:
            markers.append(f"dd={dd}")
        if outlier_count > 0:
            markers.append(f"outlier={outlier_count}")
        marker_str = "  " + ", ".join(markers) if markers else ""
        hz_str = f"{seg_hz:.1f}Hz" if seg.frame_count >= 3 else "?"
        print(f"  Seg {seg.index:>2d}: lines {seg.start_line:>5d}-{seg.end_line:>5d}  "
              f"{seg.frame_count:>5d} frames  {hz_str:>8s}  {idle_str:<14s}{marker_str}")

    # Analyze best segment in detail
    best = find_best_segment(segments)
    if best:
        best_period = estimate_refresh_rate(best.frames)
        best_hz = 1000.0 / best_period if best_period > 0 else 0
        best_lock_jitter = max(0.7, best_period * 0.07)
        print(f"\n{'=' * 80}")
        print(f"DETAILED ANALYSIS — Segment {best.index} ({best.frame_count} frames, "
              f"lines {best.start_line}-{best.end_line}, {best_hz:.3f} Hz)")
        print("=" * 80)

        phases, events = analyze_segment(best, best_period)

        # Phase breakdown
        print(f"\n--- PHASES ---")
        print(format_phase_table(phases, period_ms))

        # Totals
        total_dd = sum(p.dd_drops for p in phases)
        total_dwm = sum(p.dwm_drops for p in phases)
        if total_dd > 0 or total_dwm > 0:
            print(f"\n  Totals: {total_dd} DD drops, {total_dwm} DWM drops")

        # Per-phase drop rates
        print(f"\n--- DROP RATES ---")
        for p in phases:
            if p.count == 0:
                continue
            dd_rate = p.dd_drops / p.count * 100
            dwm_rate = p.dwm_drops / p.count * 100
            dur = p.count * period_ms / 1000.0
            label = PHASE_LABELS.get(p.name, p.name)
            dd_per_min = p.dd_drops / dur * 60 if dur > 0 else 0
            print(f"  {label:<30s}  DD: {dd_rate:.1f}% ({dd_per_min:.0f}/min)  "
                  f"DWM: {dwm_rate:.1f}%  Low outliers: {p.low_outliers}")

        # Lock analysis
        any_locked = any(p.name in ('lock', 'buf+lock') for p in phases)
        any_buf_unlocked = any(p.name == 'buf' for p in phases)
        if not any_locked and any_buf_unlocked:
            print(f"\n--- LOCK FAILURE ANALYSIS ---")
            print(analyze_lock_failure(best.frames, best_lock_jitter))
        elif any_locked:
            lock_phases = [p for p in phases if 'lock' in p.name]
            total_locked = sum(p.count for p in lock_phases)
            print(f"\n--- LOCK ANALYSIS ---")
            print(f"  Lock active for {total_locked} frames")
            for e in events:
                if e.event_type in ('lock', 'unlock'):
                    tag = 'LOCK' if e.event_type == 'lock' else 'UNLOCK'
                    print(f"  [{tag}] line {e.line} f={e.frame}: {e.detail}")

        # Events timeline
        if events:
            print(f"\n--- EVENTS ({len(events)}) ---")
            print(format_events(events))

        # EMA oscillation analysis
        buf_frames = [f for f in best.frames if f.buffer]
        if len(buf_frames) > 10:
            ema_vals = [f.ema for f in buf_frames]
            diffs = [abs(ema_vals[i+1] - ema_vals[i]) for i in range(len(ema_vals)-1)]
            avg_diff = sum(diffs) / len(diffs) if diffs else 0
            max_diff = max(diffs) if diffs else 0
            print(f"\n--- EMA STABILITY (buffer-on) ---")
            print(f"  Frame-to-frame EMA change: avg={avg_diff:.3f}ms, max={max_diff:.3f}ms")
            print(f"  EMA range: {min(ema_vals):.3f} - {max(ema_vals):.3f}ms")
            print(f"  Note: In buffer mode, EMA oscillation doesn't affect visual quality")
            print(f"        (present is VBlank-locked regardless of EMA offset)")

        # Outlier analysis (only when outlier data is present)
        seg_outliers = [f for f in best.frames if f.outlier]
        if seg_outliers:
            d_count = sum(1 for f in seg_outliers if f.outlier == 'D')
            h_count = sum(1 for f in seg_outliers if f.outlier == 'H')
            l_count = sum(1 for f in seg_outliers if f.outlier == 'L')
            non_outlier_count = sum(1 for f in best.frames if not f.outlier)
            print(f"\n--- OUTLIER ANALYSIS ---")
            print(f"  Total outliers: {len(seg_outliers)} ({len(seg_outliers)*100/(len(seg_outliers)+non_outlier_count):.1f}% of all frames)")
            print(f"    DComp drops (D): {d_count}")
            print(f"    High (H):        {h_count}")
            print(f"    Low (L):         {l_count}")
            # Outlier measured value distribution
            d_vals = [f.measured for f in seg_outliers if f.outlier == 'D']
            h_vals = [f.measured for f in seg_outliers if f.outlier == 'H']
            l_vals = [f.measured for f in seg_outliers if f.outlier == 'L']
            if d_vals:
                print(f"    D measured range: {min(d_vals):.3f} - {max(d_vals):.3f}ms")
            if h_vals:
                print(f"    H measured range: {min(h_vals):.3f} - {max(h_vals):.3f}ms")
            if l_vals:
                print(f"    L measured range: {min(l_vals):.3f} - {max(l_vals):.3f}ms")
            # Consecutive outlier streaks
            max_streak = 0
            current_streak = 0
            for f in best.frames:
                if f.outlier:
                    current_streak += 1
                    max_streak = max(max_streak, current_streak)
                else:
                    current_streak = 0
            resets = sum(1 for e in events if e.event_type == 'outlier_reset')
            print(f"  Max consecutive outlier streak: {max_streak}")
            if resets > 0:
                print(f"  Baseline resets (20+ consecutive): {resets}")

    # Also show a brief analysis of ALL segments > 50 frames
    long_segs = [s for s in segments if s.frame_count >= 50 and s is not best]
    if long_segs:
        print(f"\n{'=' * 80}")
        print(f"OTHER SIGNIFICANT SEGMENTS")
        print("=" * 80)
        for seg in long_segs:
            seg_p = estimate_refresh_rate(seg.frames)
            seg_hz = 1000.0 / seg_p if seg_p > 0 else 0
            phases, events = analyze_segment(seg, seg_p)
            total_dd = sum(p.dd_drops for p in phases)
            total_dwm = sum(p.dwm_drops for p in phases)
            print(f"\n  Segment {seg.index}: {seg.frame_count} frames "
                  f"(lines {seg.start_line}-{seg.end_line}), "
                  f"{seg_hz:.1f} Hz, DD={total_dd}, DWM={total_dwm}")
            print(format_phase_table(phases, seg_p))

    print()


if __name__ == "__main__":
    main()
