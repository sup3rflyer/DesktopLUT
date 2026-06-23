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

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .decisions import MetricThresholds, metric_thresholds_from_policy

__all__ = [
    "CorrectionInfo",
    "MeterConfig",
    "PanelInfo",
    "ProbeMatchSpec",
    "DisplayConfig",
    "WhiteSpec",
    "TargetSpec",
    "QualityTargets",
    "Profile",
    "StalenessVerdict",
    "WhitePointResolution",
    "WhiteFn",
    "load_profile",
    "DEFAULT_PROFILE_PATH",
    "D65_XY",
]

# A white-point resolver seam: ``(spd_file, *, strength, observer, anchor) -> dict``
# with at least ``{"xy": (x, y)}`` (optionally ``cct``/``duv``/``observer``/``anchor``).
# The default lazy-imports the engine; tests inject a deterministic stand-in.
WhiteFn = Callable[..., dict]

# Numeric (textbook) D65 under the CIE 1931 2° observer — the correction-strength-0
# default. The SPD-derived "CRT-like" D65 (whitepoint.py) is the strength→1 path and
# is promoted as its own stage later (HANDOFF backlog item 7); v1 SDR defaults to
# numeric D65 (the colour-critical-safe anchor, per the profile comments).
D65_XY: tuple[float, float] = (0.3127, 0.3290)

# Where the orchestrator looks for the profile when none is passed: alongside the
# DLC package root (i.e. DLC/calibration_profile.yaml), not the cwd.
DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "calibration_profile.yaml"

# Sentinel for correction_staleness(file_override=...): distinguishes "caller did not pass a
# file, fall back to the profile YAML" (legacy callers/tests) from an explicit ``None`` meaning
# "the resolved active correction is genuinely none". A bare default of None can't tell these
# apart, and the preflight tell needs to pass the store-resolved file (which may be None).
_USE_PROFILE_FILE = object()


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
class ProbeMatchSpec:
    """Per-display recipe for building a colorimeter correction via Argyll ``ccxxmake``
    (HANDOFF item 9). Drives the exact command so the core never guesses. The proven
    PA32UCXR values: ``display_tech='s'`` (Argyll "LCD PFS Phosphor IPS" = QD mini-LED),
    ``colorimeter_display_type='n'`` (non-refresh LCD), the ColorChecker Studio spectro on
    ``spectro_port``. CCMX (a 3×3 matrix from a spectrometer reference) is the right kind
    when correcting *this* exact meter+display; CCSS is for sharing a spectral sample."""

    display_tech: str = "u"                           # ccxxmake -t (Argyll tech id; 'u'=unknown)
    colorimeter_display_type: Optional[str] = None    # ccxxmake -y (e.g. 'n'=non-refresh LCD)
    spectro_port: int = 2                             # spectrometer Argyll instrument port
    display_name: Optional[str] = None                # ccxxmake -I (defaults to the display name)
    high_res: bool = True                             # ccxxmake -H (spectro high-res spectrum)
    kind: str = "ccmx"                                # 'ccmx' (spectro reference) | 'ccss'
    patch_scale: Optional[float] = None               # ccxxmake -P scale; large ⇒ ~fullscreen patch
                                                      # (mini-LED: keep every local-dimming zone lit)
    settle_seconds: Optional[float] = None            # per-patch settle before each read (via ccxxmake -C)


# The characterize soak measures the warmed channel-balance wander over a SHORT window; a
# full calibration runs far longer and sweeps the whole gamut, so it legitimately wanders
# more than the soak observed (~2x the soak envelope on the PA32UCXR, 2026-06-19). The
# run-time drift watch scales the *measured* band by this headroom so it doesn't re-warm on
# expected long-run wander while still flagging a genuine excursion. Per-display learnable.
DEFAULT_DRIFT_HEADROOM = 2.0


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
    white_spd: Optional[str] = None    # the display's measured white SPD (.sp/.csv);
    #                                    the SPD double-duty source (correction + white)
    probe_match: ProbeMatchSpec = field(default_factory=ProbeMatchSpec)
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

    @property
    def short_name(self) -> str:
        """A short, model-like display identifier for filenames (e.g. ``PA32UCXR`` from
        ``Asus ProArt PA32UCXR``). Prefers an explicit ``quirks['short_name']``; else the
        last whitespace token that carries a digit (model numbers do), else the last token."""
        explicit = self.quirks.get("short_name")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        tokens = self.name.split()
        if not tokens:
            return self.name
        for tok in reversed(tokens):
            if any(c.isdigit() for c in tok):
                return tok
        return tokens[-1]

    @property
    def drift_headroom(self) -> float:
        """Multiplier on the DIP-measured run-time drift threshold (see
        ``DEFAULT_DRIFT_HEADROOM``). Per-display ``quirks['drift_headroom']`` learnable;
        falls back to the default and ignores a malformed/non-positive value."""
        v = self.quirks.get("drift_headroom")
        try:
            f = float(v) if v is not None else DEFAULT_DRIFT_HEADROOM
        except (TypeError, ValueError):
            return DEFAULT_DRIFT_HEADROOM
        return f if f > 0 else DEFAULT_DRIFT_HEADROOM

    def target_name(self, mode: str) -> Optional[str]:
        return self.sdr_target if mode.upper() == "SDR" else self.hdr_target


@dataclass(frozen=True)
class WhiteSpec:
    intent: str = "D65"
    method: str = "numeric"            # 'numeric' | 'spd_crt_like'
    correction_strength: float = 0.0   # 0 = numeric D65; →1 = SPD observer-corrected
    observer: str = "2015_2"           # modern observer for the SPD correction (whitepoint.py)
    anchor: str = "reference"          # 'reference' | 'legacy' anchor for the correction


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
class QualityTargets(MetricThresholds):
    """Advisory CIEDE2000 acceptance targets (the human/LLM decides; these only
    inform the default recommendation)."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    # The RESOLVED active correction file the meter will actually use (store overrides the
    # profile YAML — see active_correction), and whether it exists where the meter looks:
    # True (found), False (a known location was checked and it is absent → configured-but-
    # missing), None (no correction configured, or no resolvable location to check).
    file: Optional[str] = None
    present: Optional[bool] = None

    def as_dict(self) -> dict[str, Any]:
        return {"has_correction": self.has_correction, "made": self.made,
                "age_days": self.age_days, "max_age_days": self.max_age_days,
                "stale": self.stale, "refreshable": self.refreshable, "message": self.message,
                "file": self.file, "present": self.present}


@dataclass(frozen=True)
class WhitePointResolution:
    """The resolved calibration-target white **and how it was derived** — the
    provenance the report, the deliverable, and the cross-run correction store all
    carry (HANDOFF item 7). ``provenance`` is one of ``override`` / ``spd_crt_like``
    / ``numeric``; ``note`` explains the choice in plain language."""

    xy: tuple[float, float]
    provenance: str
    method: str
    strength: float
    observer: Optional[str] = None
    anchor: Optional[str] = None
    spd_file: Optional[str] = None
    cct: Optional[float] = None
    duv: Optional[float] = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"xy": [self.xy[0], self.xy[1]], "provenance": self.provenance,
                "method": self.method, "strength": self.strength, "observer": self.observer,
                "anchor": self.anchor, "spd_file": self.spd_file, "cct": self.cct,
                "duv": self.duv, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WhitePointResolution":
        xy = d["xy"]
        return cls(xy=(float(xy[0]), float(xy[1])), provenance=d["provenance"],
                   method=d.get("method", "numeric"), strength=float(d.get("strength", 0.0)),
                   observer=d.get("observer"), anchor=d.get("anchor"),
                   spd_file=d.get("spd_file"), cct=d.get("cct"), duv=d.get("duv"),
                   note=d.get("note", ""))


def _default_white_fn(spd_file: str, *, strength: float, observer: str, anchor: str) -> dict:
    """Default SPD→white resolver. Lazy-imports the engine (numpy/colour) so the
    profile module stays dependency-light unless the SPD path is actually taken."""
    from .engine.whitepoint import white_from_spd_file

    return white_from_spd_file(spd_file, strength=strength, observer=observer, anchor=anchor)


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
    # Durable per-profile patch-sequence defaults (the user's run-size preference). A raw
    # dict mirroring PatchSizes fields — kept as a dict here (not a PatchSizes) so the
    # profile module stays free of the orchestrator import. The CLI's patch flags override it.
    patches: dict[str, Any] = field(default_factory=dict)
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
    def engine_target(self, target_name: str, *, white_xy: Optional[tuple[float, float]] = None):
        """Build the engine :class:`~dlc.engine.model.Target` for a named target.

        ``white_xy`` overrides the target's own white (the orchestrator passes the
        run's :meth:`resolve_white` result so the 3D-LUT correction targets the same
        white the MHC/GS+WB stages do); without it, falls back to ``spec.white_xy()``.
        """
        from .engine.model import Target

        spec = self.target(target_name)
        wxy = white_xy if white_xy is not None else spec.white_xy()
        if spec.is_hdr:
            # NB: the HDR PQ container is fixed at 10000 nits; the display peak (spec.luminance_nits)
            # bounds the patch set elsewhere, not the engine Target — so it is not passed here.
            return Target.hdr_rec2020_pq(white_xy=wxy)
        return Target.sdr_srgb_power(gamma=spec.gamma, white_nits=spec.luminance_nits,
                                     white_xy=wxy)

    def resolve_hdr_target(self, target_name: str, *, dip: Any = None,
                           white_xy: Optional[tuple[float, float]] = None):
        """Resolve the chosen :class:`~dlc.hdr_target.HdrTarget` for a named PQ target
        from the measured DIP (``docs/hdr-target-design.md``).

        The DIP describes how the panel *deviates* from PQ; this turns it into a chosen
        target: a sustained peak off the 200-step ladder (the profile's
        ``peak_luminance_nits`` pins it; else conservative 1600 until a warm capture),
        the first-order ``eotf_undershoot`` gain + its knee, and the single fixed white.
        ``white_xy`` is the run's resolved target white (from :meth:`resolve_white`); it
        falls back to the target's own white. Raises ``ValueError`` for a non-HDR target
        (the caller should not be resolving an HDR target for an SDR run).
        """
        from .hdr_target import resolve_from_dip

        spec = self.target(target_name)
        if not spec.is_hdr:
            raise ValueError(f"target {target_name!r} is not an HDR (PQ) target")
        wxy = white_xy if white_xy is not None else spec.white_xy()
        return resolve_from_dip(dip, white_xy=wxy, pinned_peak_nits=spec.peak_luminance_nits)

    # -- white-point resolution (HANDOFF item 7) --------------------------
    def resolve_white(self, monitor: int, target_name: str, *,
                      white_fn: Optional[WhiteFn] = None,
                      spd_override: Optional[str] = None) -> WhitePointResolution:
        """Resolve the calibration-target white chromaticity + its provenance.

        Precedence:

        1. a ``white_xy`` override pinned on the target → used verbatim
           (provenance ``override`` — e.g. an eye-verified SPD result).
        2. ``white.method == 'spd_crt_like'`` with ``correction_strength > 0`` **and**
           a readable white SPD → the SPD-derived observer-corrected "CRT-like" white
           (provenance ``spd_crt_like``), computed by ``white_fn`` (defaults to
           :func:`_default_white_fn` → the engine; injectable for tests).
        3. otherwise → numeric (textbook) D65 (provenance ``numeric``), with a note
           saying why (strength 0, method numeric, or no SPD on hand). The graceful
           fallback means a missing SPD never crashes a run — it just stays on D65.

        ``spd_override`` (the persistent correction store's freshly-captured ``white.sp``)
        takes precedence over the profile's ``display.white_spd`` — so an SPD captured by
        a probe-match build (item 9) feeds the white-point without editing the YAML.
        """
        spec = self.target(target_name)
        white = spec.white
        if spec.white_xy_override is not None:
            return WhitePointResolution(
                xy=spec.white_xy_override, provenance="override", method=white.method,
                strength=float(white.correction_strength), observer=white.observer,
                anchor=white.anchor,
                note="white_xy_override pinned in the profile (e.g. an eye-verified result)")
        display = self.display_for(monitor)
        strength = float(white.correction_strength)
        if white.method == "spd_crt_like" and strength > 0.0:
            spd = spd_override or display.white_spd
            spd_path = Path(spd) if spd else None
            if spd_path is not None and spd_path.exists():
                fn = white_fn or _default_white_fn
                res = fn(str(spd_path), strength=strength, observer=white.observer,
                         anchor=white.anchor)
                rx, ry = res["xy"]
                return WhitePointResolution(
                    xy=(float(rx), float(ry)), provenance="spd_crt_like", method=white.method,
                    strength=strength, observer=res.get("observer", white.observer),
                    anchor=res.get("anchor", white.anchor), spd_file=str(spd_path),
                    cct=res.get("cct"), duv=res.get("duv"),
                    note=f"SPD-derived CRT-like white at strength {strength:g} "
                         f"({white.observer}/{white.anchor} anchor)")
            note = (f"spd_crt_like requested but no white SPD configured for display "
                    f"{display.name!r} → numeric D65" if spd is None
                    else f"white SPD {spd!r} not found → numeric D65")
            return WhitePointResolution(
                xy=D65_XY, provenance="numeric", method=white.method, strength=strength,
                observer=white.observer, anchor=white.anchor, spd_file=spd, note=note)
        note = ("spd_crt_like method but correction_strength 0 → numeric D65"
                if white.method == "spd_crt_like" else "numeric (textbook) D65")
        return WhitePointResolution(
            xy=D65_XY, provenance="numeric", method=white.method, strength=strength,
            observer=white.observer, anchor=white.anchor, note=note)

    def transfer_for(self, target_name: str, *, bit_depth: Optional[int] = None):
        """Build the :class:`~dlc.engine.patches.Transfer` for a named target."""
        from .engine.patches import Transfer

        spec = self.target(target_name)
        depth = bit_depth if bit_depth is not None else 10
        if spec.is_hdr:
            return Transfer.pq(bit_depth=depth)
        return Transfer.power(gamma=spec.gamma, peak_nits=spec.luminance_nits, bit_depth=depth)

    # -- SPD-correction staleness (tell, don't ask) -----------------------
    def _correction_file_present(self, file: str) -> Optional[bool]:
        """Does the configured correction file exist *where the meter will look* —
        the raw path, or relative to the Argyll bin dir (how spotread resolves a bare
        ``-X`` name)? Tri-state so the tell never cries wolf: ``True`` (found), ``False``
        (a known location WAS checked and it is absent → configured-but-missing), ``None``
        (a bare name with no Argyll dir configured → no resolvable location, presence
        unknown). Mirrors ``calibrate.active_correction`` → ``Path(correction)`` wiring."""
        p = Path(file)
        if p.is_absolute():
            return p.exists()
        if p.exists():            # relative to cwd
            return True
        argyll = self.paths.get("argyll")
        if argyll:
            return (Path(argyll) / file).exists()
        return None               # bare name, no Argyll dir → cannot tell

    def correction_staleness(self, *, today: Optional[date] = None,
                             made_override: Optional[str] = None,
                             file_override: Any = _USE_PROFILE_FILE) -> StalenessVerdict:
        """The SPD-correction staleness *tell* (never a gate).

        ``made_override`` lets the caller supply the correction's build date from the
        persistent per-display correction store (§10) instead of the profile YAML, so
        a correction refreshed since the profile was written ages from its real date.

        ``file_override`` lets the caller supply the RESOLVED active correction file
        (``calibrate.active_correction`` — the store overrides the profile YAML), so the
        tell consults the same correction the meter is actually wired to instead of the
        (possibly empty) profile YAML. Omitting it keeps the legacy profile-YAML source.
        The resolved file is also existence-checked so a configured-but-missing correction
        is surfaced distinctly from none-configured.
        """
        c = self.meter.correction
        today = today or date.today()
        file = c.file if file_override is _USE_PROFILE_FILE else file_override
        if not file:
            return StalenessVerdict(
                has_correction=False, made=made_override or c.made, age_days=None,
                max_age_days=c.max_age_days, stale=False, refreshable=c.spectrometer_available,
                file=None, present=None,
                message="no colorimeter correction configured — raw meter readings "
                        "(consider building a CCMX/CCSS for a mini-LED/QD panel).")
        present = self._correction_file_present(file)
        made_str = made_override if made_override is not None else c.made
        try:
            made = datetime.strptime(str(made_str), "%Y-%m-%d").date() if made_str else None
        except ValueError:
            made = None
        if present is False:
            # Configured but the file is gone from where the meter looks: distinct from "no
            # correction" — the meter will silently fall back to RAW readings. Age is moot when
            # the file is missing, so this is its own tell, not a staleness verdict.
            return StalenessVerdict(
                has_correction=True, made=made_str,
                age_days=((today - made).days if made else None),
                max_age_days=c.max_age_days, stale=False, refreshable=c.spectrometer_available,
                file=file, present=False,
                message=(f"colorimeter correction {Path(file).name!r} is configured but the file is "
                         f"MISSING on disk ({file}) — the meter will fall back to raw readings; "
                         f"rebuild via `--flow build-correction`."))
        if made is None:
            return StalenessVerdict(
                has_correction=True, made=made_str, age_days=None, max_age_days=c.max_age_days,
                stale=False, refreshable=c.spectrometer_available, file=file, present=present,
                message="correction present but its build date is unknown — cannot judge staleness.")
        age = (today - made).days
        stale = age > c.max_age_days
        if stale:
            msg = (f"colorimeter correction is {age} days old (> {c.max_age_days}d policy for this "
                   f"panel)" + (" — refreshable now." if c.spectrometer_available else " — no spectrometer on hand to refresh."))
        else:
            msg = f"colorimeter correction is {age} days old (within the {c.max_age_days}d policy)."
        return StalenessVerdict(
            has_correction=True, made=made_str, age_days=age, max_age_days=c.max_age_days,
            stale=stale, refreshable=c.spectrometer_available, file=file, present=present, message=msg)

    # -- construction -----------------------------------------------------
    @classmethod
    def synthetic(cls, *, monitor: int = 0, sdr_nits: float = 120.0,
                  cold_channel: str = "B", correction_made: Optional[str] = None,
                  output_dir: Optional[str] = None, white_method: str = "numeric",
                  white_strength: float = 0.0, white_spd: Optional[str] = None) -> "Profile":
        """A deterministic in-memory profile for tests / autonomous rehearsals —
        never touches the local-only YAML. Models the lab's primary mini-LED panel
        (temperamental blue, sRGB/γ2.2/120-nit SDR target). ``output_dir`` (absolute)
        pins where the deliverable folder lands; defaults to the relative ``results``.

        ``white_method``/``white_strength``/``white_spd`` opt the SDR target into the
        SPD-derived "CRT-like" white path (item 7) for exercising the resolver; the
        default keeps numeric D65."""
        return cls(
            meter=MeterConfig(model="synthetic meter", argyll_port=1,
                              correction=CorrectionInfo(file="synthetic.ccmx", made=correction_made,
                                                        max_age_days=180, spectrometer_available=True)),
            displays=(DisplayConfig(
                name="Synthetic mini-LED", desktoplut_monitor=monitor, argyll_display=monitor + 1,
                primary=True, panel=PanelInfo(tech="mini-LED IPS", bit_depth=10),
                sdr_target="srgb_g22", hdr_target="rec2020_pq", white_spd=white_spd,
                probe_match=ProbeMatchSpec(display_tech="s", colorimeter_display_type="n",
                                           spectro_port=2, display_name="Synthetic mini-LED"),
                quirks={"temperamental_channel": cold_channel, "settle_delta_de": 0.3}),),
            targets={
                "srgb_g22": TargetSpec(name="srgb_g22", colorspace="Rec.709", transfer_type="power",
                                       gamma=2.2, white_luminance_nits=sdr_nits,
                                       white=WhiteSpec(method=white_method,
                                                       correction_strength=white_strength)),
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

# Valid CIE observer ids (mirrors engine.whitepoint.OBSERVERS — kept here so profile load
# stays engine-free). The digit-stripped map repairs the YAML 1.1 landmine: an UNQUOTED
# `observer: 2015_2` parses as the int 20152 (underscore = digit separator), whose str() would
# otherwise silently reach the white solver as a bogus observer.
_KNOWN_OBSERVERS = ("1931_2", "2015_2", "1964_10")
_OBSERVER_BY_DIGITS = {o.replace("_", ""): o for o in _KNOWN_OBSERVERS}


def _normalize_observer(value: Any) -> str:
    s = str(value).strip()
    if s in _KNOWN_OBSERVERS:
        return s
    if s in _OBSERVER_BY_DIGITS:           # e.g. unquoted YAML 2015_2 -> int 20152 -> "2015_2"
        return _OBSERVER_BY_DIGITS[s]
    raise ValueError(
        f"unknown CIE observer {value!r}; expected one of {list(_KNOWN_OBSERVERS)}. "
        'Quote it in YAML (observer: "2015_2") so YAML 1.1 does not read the underscore as a '
        "numeric digit separator.")


def _white_spec(raw: dict[str, Any]) -> WhiteSpec:
    w = raw or {}
    return WhiteSpec(
        intent=str(w.get("intent", "D65")),
        method=str(w.get("method", "numeric")),
        correction_strength=float(w.get("correction_strength", 0.0) or 0.0),
        observer=_normalize_observer(w.get("observer", "2015_2")),
        anchor=str(w.get("anchor", "reference")),
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


def _probe_match_spec(raw: dict[str, Any]) -> ProbeMatchSpec:
    pm = raw or {}
    return ProbeMatchSpec(
        display_tech=str(pm.get("display_tech", "u")),
        colorimeter_display_type=(str(pm["colorimeter_display_type"])
                                  if pm.get("colorimeter_display_type") is not None else None),
        spectro_port=int(pm.get("spectro_port", 2)),
        display_name=pm.get("display_name"),
        high_res=bool(pm.get("high_res", True)),
        kind=str(pm.get("kind", "ccmx")),
        patch_scale=(float(pm["patch_scale"]) if pm.get("patch_scale") is not None else None),
        settle_seconds=(float(pm["settle_seconds"]) if pm.get("settle_seconds") is not None else None),
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
        white_spd=raw.get("white_spd"),
        probe_match=_probe_match_spec(raw.get("probe_match", {}) or {}),
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
    quality = QualityTargets(**metric_thresholds_from_policy(q, "default").as_dict())

    return Profile(meter=meter, displays=displays, targets=targets, quality=quality,
                   paths=dict(raw.get("paths", {}) or {}),
                   patches=dict(raw.get("patches", {}) or {}), source_path=str(p))
