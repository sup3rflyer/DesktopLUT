"""The calibration profile — the skill ⊥ user-data boundary (v2-design-notes §2).

The agent/core is **generic, shareable** logic. The user's setup is **private,
local-only** data. ``DLC/calibration_profile.yaml`` (gitignored) holds the known
facts so the scripted core never has to guess hardware, display mapping, or
targets — meter + correction, displays (with the ``desktoplut_monitor`` ⇄
``argyll_display`` mapping + primary flag + per-panel ``quirks``), named targets,
quality defaults, tool paths, output dir.

This module loads that YAML into a typed :class:`Profile` and exposes exactly what
the orchestrator (:mod:`dlc.calibrate`) needs:

* display + target lookup by ``desktoplut_monitor`` / name;
* engine :class:`~dlc.engine.model.Target` and :class:`~dlc.engine.patches.Transfer`
  builders for a named target (so the patch generator and the LUT engines agree on
  "ideal" exactly);
* the SPD-correction **staleness verdict** (``max_age_days`` policy → tell, don't
  ask — see §10);
* a :meth:`Profile.synthetic` builder so tests never touch the local-only YAML.

YAML is the *only* dependency and it is **lazy-imported** inside :func:`load_profile`
— importing this module is free, and :meth:`Profile.synthetic` needs no YAML, so the
spine/test suites that build a synthetic profile stay dependency-light. The engine
``Target``/``Transfer`` builders lazy-import :mod:`dlc.engine` (numpy/colour) only
when called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "CorrectionInfo",
    "MeterConfig",
    "PanelInfo",
    "DisplayConfig",
    "WhiteSpec",
    "TargetSpec",
    "QualityTargets",
    "Profile",
    "StalenessVerdict",
    "load_profile",
    "DEFAULT_PROFILE_PATH",
    "D65_XY",
]

# Numeric (textbook) D65 under the CIE 1931 2° observer — the correction-strength-0
# default. The SPD-derived "CRT-like" D65 (whitepoint.py) is the strength→1 path and
# is promoted as its own stage later (HANDOFF backlog item 7); v1 SDR defaults to
# numeric D65 (the colour-critical-safe anchor, per the profile comments).
D65_XY: tuple[float, float] = (0.3127, 0.3290)

# Where the orchestrator looks for the profile when none is passed: alongside the
# DLC package root (i.e. DLC/calibration_profile.yaml), not the cwd.
DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "calibration_profile.yaml"


# ---------------------------------------------------------------------------
# Leaf models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorrectionInfo:
    """The colorimeter correction (Argyll ``-X`` ``.ccmx``/``.ccss``) + its policy.

    ``made`` drives staleness: a mini-LED backlight ages, so a correction past
    ``max_age_days`` is *told* (not asked) to the human. ``spectrometer_available``
    says whether it can be refreshed on the spot.
    """

    file: Optional[str] = None
    made: Optional[str] = None            # YYYY-MM-DD
    max_age_days: int = 180
    spectrometer_available: Optional[bool] = None

    @property
    def made_date(self) -> Optional[date]:
        if not self.made:
            return None
        try:
            return datetime.strptime(str(self.made), "%Y-%m-%d").date()
        except ValueError:
            return None


@dataclass(frozen=True)
class MeterConfig:
    model: Optional[str] = None
    argyll_port: int = 1
    correction: CorrectionInfo = field(default_factory=CorrectionInfo)


@dataclass(frozen=True)
class PanelInfo:
    tech: Optional[str] = None
    bit_depth: int = 10
    backlight_zones: Optional[int] = None
    resolution: Optional[str] = None
    hdr_peak_nits: Optional[float] = None


@dataclass(frozen=True)
class DisplayConfig:
    """One physical display + its two identities (DesktopLUT monitor index ⇄ Argyll
    display number) and its learned ``quirks`` (the temperamental channel, settle
    tolerance — physical facts the core reads, never hardwires)."""

    name: str
    desktoplut_monitor: int
    argyll_display: int
    primary: bool = False
    panel: PanelInfo = field(default_factory=PanelInfo)
    sdr_target: Optional[str] = None
    hdr_target: Optional[str] = None
    quirks: dict[str, Any] = field(default_factory=dict)

    @property
    def temperamental_channel(self) -> Optional[str]:
        ch = self.quirks.get("temperamental_channel")
        if isinstance(ch, str) and ch[:1].upper() in ("R", "G", "B"):
            return ch[:1].upper()
        return None

    @property
    def settle_delta_de(self) -> Optional[float]:
        v = self.quirks.get("settle_delta_de")
        return float(v) if v is not None else None

    def target_name(self, mode: str) -> Optional[str]:
        return self.sdr_target if mode.upper() == "SDR" else self.hdr_target


@dataclass(frozen=True)
class WhiteSpec:
    intent: str = "D65"
    method: str = "numeric"            # 'numeric' | 'spd_crt_like'
    correction_strength: float = 0.0   # 0 = numeric D65; →1 = SPD observer-corrected


@dataclass(frozen=True)
class TargetSpec:
    """A named calibration target (primaries + transfer + white + luminance).

    The owner's two hard rules live here: ``transfer.type == 'power'`` (pure
    power-law γ, **never** piecewise sRGB) and the white *intent* D65 with an
    explicit method, so the core derives white rather than hardwiring an xy.
    """

    name: str
    colorspace: str = "Rec.709"
    transfer_type: str = "power"
    gamma: float = 2.2
    white: WhiteSpec = field(default_factory=WhiteSpec)
    white_luminance_nits: float = 120.0
    peak_luminance_nits: Optional[float] = None
    white_xy_override: Optional[tuple[float, float]] = None

    @property
    def is_hdr(self) -> bool:
        return self.transfer_type == "pq"

    @property
    def luminance_nits(self) -> float:
        return float(self.peak_luminance_nits if self.is_hdr and self.peak_luminance_nits
                     else self.white_luminance_nits)

    def white_xy(self) -> tuple[float, float]:
        """The target white chromaticity.

        ``correction_strength == 0`` ⇒ numeric D65 (the default, interop-safe).
        A precomputed ``white_xy_override`` (e.g. an eye-verified SPD result) takes
        precedence when present; otherwise the SPD-derived path is deferred to the
        white-point stage (HANDOFF item 7) and this falls back to numeric D65.
        """
        if self.white_xy_override is not None:
            return self.white_xy_override
        return D65_XY


@dataclass(frozen=True)
class QualityTargets:
    """Advisory CIEDE2000 acceptance targets (the human/LLM decides; these only
    inform the default recommendation)."""

    avg_de2000: float = 1.5
    p95_de2000: float = 3.0
    max_de2000: float = 5.0
    white_de2000: float = 2.0

    def as_dict(self) -> dict[str, float]:
        return {"avg_de2000": self.avg_de2000, "p95_de2000": self.p95_de2000,
                "max_de2000": self.max_de2000, "white_de2000": self.white_de2000}


@dataclass(frozen=True)
class StalenessVerdict:
    """The SPD-correction staleness *tell* (never a gate)."""

    has_correction: bool
    made: Optional[str]
    age_days: Optional[int]
    max_age_days: int
    stale: bool
    refreshable: Optional[bool]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"has_correction": self.has_correction, "made": self.made,
                "age_days": self.age_days, "max_age_days": self.max_age_days,
                "stale": self.stale, "refreshable": self.refreshable, "message": self.message}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    meter: MeterConfig
    displays: tuple[DisplayConfig, ...]
    targets: dict[str, TargetSpec]
    quality: QualityTargets = field(default_factory=QualityTargets)
    paths: dict[str, str] = field(default_factory=dict)
    source_path: Optional[str] = None

    # -- lookups ----------------------------------------------------------
    def display_for(self, monitor: int) -> DisplayConfig:
        for d in self.displays:
            if d.desktoplut_monitor == monitor:
                return d
        raise KeyError(f"no display with desktoplut_monitor={monitor} in the profile")

    def primary_display(self) -> DisplayConfig:
        for d in self.displays:
            if d.primary:
                return d
        if self.displays:
            return self.displays[0]
        raise KeyError("profile has no displays")

    def target(self, name: str) -> TargetSpec:
        if name not in self.targets:
            raise KeyError(f"no target named {name!r} in the profile (have: {sorted(self.targets)})")
        return self.targets[name]

    def target_for(self, monitor: int, mode: str) -> TargetSpec:
        name = self.display_for(monitor).target_name(mode)
        if not name:
            raise KeyError(f"display {monitor} has no {mode.upper()} target configured")
        return self.target(name)

    # -- engine builders (lazy-import the numpy/colour engine) ------------
    def engine_target(self, target_name: str):
        """Build the engine :class:`~dlc.engine.model.Target` for a named target."""
        from .engine.model import Target

        spec = self.target(target_name)
        white_xy = spec.white_xy()
        if spec.is_hdr:
            return Target.hdr_rec2020_pq(peak_nits=spec.luminance_nits, white_xy=white_xy)
        return Target.sdr_srgb_power(gamma=spec.gamma, white_nits=spec.luminance_nits,
                                     white_xy=white_xy)

    def transfer_for(self, target_name: str, *, bit_depth: Optional[int] = None):
        """Build the :class:`~dlc.engine.patches.Transfer` for a named target."""
        from .engine.patches import Transfer

        spec = self.target(target_name)
        depth = bit_depth if bit_depth is not None else 10
        if spec.is_hdr:
            return Transfer.pq(bit_depth=depth)
        return Transfer.power(gamma=spec.gamma, peak_nits=spec.luminance_nits, bit_depth=depth)

    # -- SPD-correction staleness (tell, don't ask) -----------------------
    def correction_staleness(self, *, today: Optional[date] = None) -> StalenessVerdict:
        c = self.meter.correction
        today = today or date.today()
        if not c.file:
            return StalenessVerdict(
                has_correction=False, made=c.made, age_days=None, max_age_days=c.max_age_days,
                stale=False, refreshable=c.spectrometer_available,
                message="no colorimeter correction configured — raw meter readings "
                        "(consider building a CCMX/CCSS for a mini-LED/QD panel).")
        made = c.made_date
        if made is None:
            return StalenessVerdict(
                has_correction=True, made=c.made, age_days=None, max_age_days=c.max_age_days,
                stale=False, refreshable=c.spectrometer_available,
                message="correction present but its build date is unknown — cannot judge staleness.")
        age = (today - made).days
        stale = age > c.max_age_days
        if stale:
            msg = (f"colorimeter correction is {age} days old (> {c.max_age_days}d policy for this "
                   f"panel)" + (" — refreshable now." if c.spectrometer_available else " — no spectrometer on hand to refresh."))
        else:
            msg = f"colorimeter correction is {age} days old (within the {c.max_age_days}d policy)."
        return StalenessVerdict(
            has_correction=True, made=c.made, age_days=age, max_age_days=c.max_age_days,
            stale=stale, refreshable=c.spectrometer_available, message=msg)

    # -- construction -----------------------------------------------------
    @classmethod
    def synthetic(cls, *, monitor: int = 0, sdr_nits: float = 120.0,
                  cold_channel: str = "B", correction_made: Optional[str] = None,
                  output_dir: Optional[str] = None) -> "Profile":
        """A deterministic in-memory profile for tests / autonomous rehearsals —
        never touches the local-only YAML. Models the lab's primary mini-LED panel
        (temperamental blue, sRGB/γ2.2/120-nit SDR target). ``output_dir`` (absolute)
        pins where the deliverable folder lands; defaults to the relative ``results``."""
        return cls(
            meter=MeterConfig(model="synthetic meter", argyll_port=1,
                              correction=CorrectionInfo(file="synthetic.ccmx", made=correction_made,
                                                        max_age_days=180, spectrometer_available=True)),
            displays=(DisplayConfig(
                name="Synthetic mini-LED", desktoplut_monitor=monitor, argyll_display=monitor + 1,
                primary=True, panel=PanelInfo(tech="mini-LED IPS", bit_depth=10),
                sdr_target="srgb_g22", hdr_target="rec2020_pq",
                quirks={"temperamental_channel": cold_channel, "settle_delta_de": 0.3}),),
            targets={
                "srgb_g22": TargetSpec(name="srgb_g22", colorspace="Rec.709", transfer_type="power",
                                       gamma=2.2, white=WhiteSpec(), white_luminance_nits=sdr_nits),
                "rec2020_pq": TargetSpec(name="rec2020_pq", colorspace="Rec.2020", transfer_type="pq",
                                         gamma=2.2, white=WhiteSpec(), peak_luminance_nits=1600.0),
            },
            quality=QualityTargets(),
            paths={"output": output_dir or "results"},
            source_path=None,
        )


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def _white_spec(raw: dict[str, Any]) -> WhiteSpec:
    w = raw or {}
    return WhiteSpec(
        intent=str(w.get("intent", "D65")),
        method=str(w.get("method", "numeric")),
        correction_strength=float(w.get("correction_strength", 0.0) or 0.0),
    )


def _target_spec(name: str, raw: dict[str, Any]) -> TargetSpec:
    transfer = raw.get("transfer", {}) or {}
    ttype = str(transfer.get("type", "power"))
    override = raw.get("white_xy")
    white_override = (float(override[0]), float(override[1])) if override else None
    return TargetSpec(
        name=name,
        colorspace=str(raw.get("colorspace", "Rec.709")),
        transfer_type=ttype,
        gamma=float(transfer.get("gamma", 2.2)),
        white=_white_spec(raw.get("white", {})),
        white_luminance_nits=float(raw.get("white_luminance_nits", 120.0)),
        peak_luminance_nits=(float(raw["peak_luminance_nits"]) if raw.get("peak_luminance_nits") else None),
        white_xy_override=white_override,
    )


def _display_config(raw: dict[str, Any]) -> DisplayConfig:
    panel = raw.get("panel", {}) or {}
    return DisplayConfig(
        name=str(raw.get("name", "display")),
        desktoplut_monitor=int(raw["desktoplut_monitor"]),
        argyll_display=int(raw.get("argyll_display", int(raw["desktoplut_monitor"]) + 1)),
        primary=bool(raw.get("primary", False)),
        panel=PanelInfo(
            tech=panel.get("tech"),
            bit_depth=int(panel.get("bit_depth", 10)),
            backlight_zones=panel.get("backlight_zones"),
            resolution=panel.get("resolution"),
            hdr_peak_nits=panel.get("hdr_peak_nits"),
        ),
        sdr_target=raw.get("sdr_target"),
        hdr_target=raw.get("hdr_target"),
        quirks=dict(raw.get("quirks", {}) or {}),
    )


def load_profile(path: Optional[Path | str] = None) -> Profile:
    """Load and validate ``calibration_profile.yaml`` into a :class:`Profile`.

    ``path`` defaults to :data:`DEFAULT_PROFILE_PATH` (``DLC/calibration_profile.yaml``).
    Raises ``FileNotFoundError`` if absent and ``ValueError`` on a malformed profile.
    """
    import yaml  # lazy: only the YAML path needs it; synthetic() does not.

    p = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"calibration profile not found: {p} (it is local-only/gitignored — see "
            "calibration_profile.yaml in the DLC root, or pass a path)")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"profile {p} is not a mapping")

    meter_raw = raw.get("meter", {}) or {}
    corr_raw = meter_raw.get("correction", {}) or {}
    meter = MeterConfig(
        model=meter_raw.get("model"),
        argyll_port=int(meter_raw.get("argyll_port", 1)),
        correction=CorrectionInfo(
            file=corr_raw.get("file"),
            made=corr_raw.get("made"),
            max_age_days=int(corr_raw.get("max_age_days", 180)),
            spectrometer_available=corr_raw.get("spectrometer_available"),
        ),
    )

    displays = tuple(_display_config(d) for d in (raw.get("displays") or []))
    if not displays:
        raise ValueError(f"profile {p} defines no displays")

    targets = {name: _target_spec(name, spec) for name, spec in (raw.get("targets") or {}).items()}
    if not targets:
        raise ValueError(f"profile {p} defines no targets")

    q = raw.get("quality", {}) or {}
    quality = QualityTargets(
        avg_de2000=float(q.get("avg_de2000", 1.5)),
        p95_de2000=float(q.get("p95_de2000", 3.0)),
        max_de2000=float(q.get("max_de2000", 5.0)),
        white_de2000=float(q.get("white_de2000", 2.0)),
    )

    return Profile(meter=meter, displays=displays, targets=targets, quality=quality,
                   paths=dict(raw.get("paths", {}) or {}), source_path=str(p))
