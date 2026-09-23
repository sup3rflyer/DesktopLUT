"""Neutral-state audit: is the panel REALLY neutral before the first raw read?

HW-proven 2026-09-03 (docs/pa32ucxr-plan-2026-09-03.md item 1): Windows keeps the LAST
associated MHC2 transform after DesktopLUT removes a profile, so ``calibration.enter``
(the C++ ``DoEnterNeutral``: clears WB / GS / tonemap / Desktop-Gamma for the mode and
removes the ICM) does NOT neutralize the panel — every raw stage before this fix measured
through the previously applied stack. A TRUE neutral requires ASSOCIATING an identity MHC2
profile through the normal path (``set_primaries`` + ``set_white(D65)`` + ``apply``), and
the GUI layers (tonemap, Desktop Gamma, white balance, grayscale) are invisible over the
pipe — they live only in the live ``DesktopLUT.ini``.

This module is the dependency-free spine-side audit of both halves:

* :func:`identity_primaries` — the display primaries ``P`` that make the baked MHC2
  matrix exactly identity for the mode (see the docstring: the C++ source primaries
  differ by mode, so ``P`` does too).
* :func:`parse_ini_flags` / :func:`read_ini_flags` — the monitor's ``<MODE>_*`` GUI-layer
  flags from the ini (tolerates a missing/unreadable file → ``{}``). The section is resolved
  by :func:`resolve_ini_section`: identity-keyed ``[Display<slot>]`` (DesktopLUT 2026-09-14+)
  via the pipe's ``windows.query_monitors`` identity, or the pre-identity ``[Monitor<N>]``.
* :func:`neutral_state_audit` — one dict for the seam digest: calibration status, the
  ``state()`` mhc/runtime entries for the key, the ini flags, and the derived verdicts.
* :func:`neutral_violations` — the mechanical (100 % deterministic) refusal reasons the
  hardware-readiness stage raises on. Anything softer stays evidence for the LLM.

Ported from the one-off HW probe helper (``agent_probe_common.py``: ``ini_flags`` /
``audit_state`` / ``require``) into the spine proper.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

from . import gamut

__all__ = [
    "D65_XY", "GUI_LAYER_KEYS", "identity_primaries", "monitor_identity", "resolve_ini_section",
    "parse_ini_flags", "load_ini_flags", "read_ini_flags", "resolve_desktoplut_ini",
    "neutral_state_audit", "neutral_violations", "ini_true",
]

D65_XY: tuple[float, float] = (0.3127, 0.3290)

# The per-mode GUI-layer keys in a DesktopLUT.ini monitor section (``<MODE>_`` prefix stripped).
GUI_LAYER_KEYS: tuple[str, ...] = (
    "TonemapEnabled", "TonemapDynamic", "MaxTmlEnabled", "MaxTmlPeak",
    "MHCDesktopGamma", "MHCWhiteBalanceEnabled", "MHCCorrGSEnabled",
    "MHCEnabled", "MHCProfilePath", "MHCSourceFile",
    "FaldEnabled", "FaldParamsPath",
)

# ini key → human label for the refusal message. Every one of these is a layer the C++
# DoEnterNeutral is supposed to clear for the calibrated mode.
# The FALD compensation layer (2026-09-12, HDR overlay path) is context-dependent: a patch read
# through it depends on the surround, so DLC must never measure with it on (fald-lessons items 7/8).
_PIPE_LAYER_LABELS = {"tonemap": "HDR tonemap", "desktop_gamma": "Desktop Gamma",
                      "white_balance": "GUI white balance", "grayscale": "GUI grayscale correction",
                      "fald": "FALD compensation layer"}
_LAYER_LABELS: dict[str, str] = {
    "TonemapEnabled": "HDR tonemap",
    "MHCDesktopGamma": "Desktop Gamma",
    "MHCWhiteBalanceEnabled": "GUI white balance",
    "MHCCorrGSEnabled": "GUI grayscale correction",
    "FaldEnabled": "FALD compensation layer",
}

_BOOTSTRAP_COLORSPACE = {"HDR": "Rec.2020", "SDR": "Rec.709"}


def ini_true(value: Any) -> bool:
    """DesktopLUT ini booleans are ``true``/``false`` (also accept 1/yes/on)."""
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def identity_primaries(mode: str, native_primaries: Optional[Mapping[str, Any]] = None,
                       ) -> tuple[dict[str, float], str]:
    """The display primaries ``P`` (``{rx, ry, gx, gy, bx, by}``) to associate for an
    IDENTITY MHC2 matrix, plus the source tag (``'dip'`` | ``'bootstrap'``).

    The baked matrix is ``inv(displayToXYZ(P, W)) · srcToXYZ`` (mhc_icc.cpp
    ``GenerateMHC2Profile``), so identity needs ``P == src`` and ``W == src white (D65)``:

    * **HDR** — the C++ sets ``src = P`` itself (native targeting, ``hdrNativeSrc``), so ANY
      ``P`` yields identity; the DIP's measured ``native_primaries`` are used when present
      (HW-verified 2026-09-03: the identity leg read the panel's native white 0.3149) and
      Rec.2020 bootstraps a display that has not been characterized.
    * **SDR** — the C++ pins ``src = sRGB`` (``g_srgbPrimaries``), so identity REQUIRES
      ``P = Rec.709``; pushing the DIP's native primaries there would bake a real
      native→sRGB gamut matrix — the opposite of neutral. SDR therefore always uses the
      Rec.709 bootstrap and ignores ``native_primaries`` (source ``'bootstrap'``).
    """
    m = str(mode).upper()
    if m not in _BOOTSTRAP_COLORSPACE:
        raise ValueError(f"mode must be SDR or HDR, got {mode!r}")
    if m == "HDR" and native_primaries:
        try:
            native = {ch: (float(native_primaries[ch][0]), float(native_primaries[ch][1]))
                      for ch in ("R", "G", "B")}
        except (KeyError, TypeError, ValueError, IndexError):
            native = None
        if native is not None:
            return _as_primaries(native), "dip"
    std = gamut.target_primaries(_BOOTSTRAP_COLORSPACE[m]) or {}
    return _as_primaries(std), "bootstrap"


def _as_primaries(xy: Mapping[str, tuple[float, float]]) -> dict[str, float]:
    return {"rx": float(xy["R"][0]), "ry": float(xy["R"][1]),
            "gx": float(xy["G"][0]), "gy": float(xy["G"][1]),
            "bx": float(xy["B"][0]), "by": float(xy["B"][1])}


# DesktopLUT.ini monitor sections (C++ settings.h kDisplaySectionPrefix / kLegacySectionPrefix):
#   [Display<slot>]  identity-keyed (2026-09-14, parent 04d4150): DevicePath= / EdidId= + the keys.
#                    The slot is a STORAGE id (never reused, assigned in first-seen order) — it is
#                    NOT the DesktopLUT monitor index; only the running DesktopLUT knows which slot
#                    live monitor N is attached to (windows.query_monitors ``settings_slot``).
#   [Monitor<N>]     pre-identity, keyed by enumeration index. The C++ adopts one by index only
#                    when the display at N matches no identity entry, then migrates it to a
#                    [Display<slot>] and deletes it on the next save (ReattachMonitorSettings
#                    saves at once) — so a [Monitor<N>] beside [Display*] sections is an
#                    unclaimed leftover, not the live monitor N's settings.
_SECTION_RE = re.compile(r"^(display|monitor)(0|[1-9][0-9]{0,5})$", re.I)
_MAX_SAVED_SECTIONS = 256   # settings.h kMaxSavedMonitorSections: larger indices are ignored


def _ini_sections(text: str) -> dict[str, dict[str, str]]:
    """``{lower-case section name: {key: value}}``. Win32 profile semantics: section names are
    case-insensitive and the FIRST occurrence of a section / key wins; keys are exact (DesktopLUT
    writes them verbatim); ``;``/``#`` comment lines are skipped."""
    out: dict[str, dict[str, str]] = {}
    cur: Optional[dict[str, str]] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line[0] == "[" and "]" in line:
            name = line[1:line.index("]")].strip().lower()
            if name in out:
                cur = None   # a repeated section is ignored (the first one wins)
            else:
                cur = out[name] = {}
            continue
        if cur is None or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cur.setdefault(k.strip(), v.strip())
    return out


def _numbered_sections(sections: Mapping[str, Mapping[str, str]], prefix: str) -> dict[int, Mapping[str, str]]:
    out: dict[int, Mapping[str, str]] = {}
    for name, keys in sections.items():
        m = _SECTION_RE.match(name)
        if m and m.group(1) == prefix and int(m.group(2)) < _MAX_SAVED_SECTIONS:
            out[int(m.group(2))] = keys
    return out


def _as_slot(value: Any) -> Optional[int]:
    """A settings slot from the pipe (a JSON number — the C++ JNum is a double); ``None`` if absent/junk."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
        return None
    return int(value)


def monitor_identity(monitors: Any, monitor: int) -> Optional[dict[str, Any]]:
    """The settings identity the pipe reports for DesktopLUT monitor ``monitor`` — from a
    ``windows.query_monitors`` result (or its ``monitors`` list): ``{settings_slot, edid_id,
    device_path}``. ``settings_slot`` is the ``[Display<slot>]`` the running DesktopLUT keeps the
    monitor's settings in (-1 = unidentified, settings attached by index and not persisted;
    ``None`` = not reported — builds before 2026-09-14); ``edid_id`` is the stored identity's
    EdidId, ``device_path`` the live DisplayConfig path. ``None`` when the monitor is not listed."""
    entries = monitors.get("monitors") if isinstance(monitors, Mapping) else monitors
    for e in entries or []:
        if not isinstance(e, Mapping):
            continue
        try:
            if int(e.get("index")) != int(monitor):
                continue
        except (TypeError, ValueError):
            continue
        return {"settings_slot": _as_slot(e.get("settings_slot")),
                "edid_id": str(e.get("edid_id") or "").strip() or None,
                "device_path": str(e.get("device_path") or "").strip() or None}
    return None


def _same(a: Optional[str], b: Optional[str]) -> Optional[bool]:
    """Case-insensitive equality (the C++ EqualsNoCase); ``None`` when either side is empty."""
    if not a or not b:
        return None
    return a.strip().lower() == b.strip().lower()


def _describe_displays(displays: Mapping[int, Mapping[str, str]]) -> str:
    return ", ".join(f"[Display{s}] {displays[s].get('EdidId') or displays[s].get('DevicePath') or '(no identity)'}"
                     for s in sorted(displays))


def resolve_ini_section(text: str | Mapping[str, Mapping[str, str]], monitor: int,
                        identity: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Which ini section holds DesktopLUT monitor ``monitor``'s settings:
    ``{"section": "Display0" | "Monitor1" | None, "how": str, "note": str | None}`` — ``note`` is
    set when the section could not be resolved (why) or was resolved with a caveat.

    ``identity`` is :func:`monitor_identity` (the pipe's view). Mirrors the C++ matcher
    (monitor_identity.cpp ``MatchMonitorSettings`` + settings.cpp ``LoadMonitorSettingsPool``):

    * **pre-identity ini** (no ``[Display*]`` section): ``[Monitor<N>]`` — the C++ adopts it by
      index, so this is exact; identity is not needed (old inis / old builds keep working).
    * **identity-keyed ini**, pipe ``settings_slot`` ≥ 0: ``[Display<slot>]``, cross-checked
      against the section's ``DevicePath`` / ``EdidId`` (either one agreeing verifies it — the
      path changes when the panel moves connector, the EDID id is shared by twins). A section
      that names ANOTHER display (the ini on disk is out of sync with the running DesktopLUT) is
      refused. Slot not on disk yet → the ``[Monitor<N>]`` it was just adopted from, if any.
    * ``settings_slot`` -1 (unidentified display, attached by index) → ``[Monitor<N>]`` if
      present; otherwise its settings are not in the ini at all.
    * no ``settings_slot`` but a device path / EDID id → a UNIQUE ``DevicePath`` match, then a
      unique ``EdidId`` match (twins without a slot are ambiguous), then ``[Monitor<N>]``.
    * no identity → unresolved: a slot number is not a monitor index, and a leftover
      ``[Monitor<N>]`` beside identity sections is an unclaimed pre-identity section. Never a
      guess — a wrong section would put another display's flags in the evidence."""
    sections = _ini_sections(text) if isinstance(text, str) else text
    n = int(monitor)
    legacy_name = f"Monitor{n}"
    has_legacy = n in _numbered_sections(sections, "monitor")
    displays = _numbered_sections(sections, "display")

    def unresolved(why: str) -> dict[str, Any]:
        return {"section": None, "how": "unresolved", "note": why}

    if not displays:
        if has_legacy:
            return {"section": legacy_name, "how": f"legacy [{legacy_name}] (pre-identity ini, index-keyed)",
                    "note": None}
        return unresolved(f"neither a [{legacy_name}] nor any [Display*] section in the ini")

    slot = _as_slot(identity.get("settings_slot")) if identity else None
    pipe_path = identity.get("device_path") if identity else None
    pipe_edid = identity.get("edid_id") if identity else None
    who = f"monitor {n} ({pipe_edid or pipe_path or 'no identity'})"

    if slot is not None and slot >= 0:
        sec = displays.get(slot)
        if sec is None:
            if has_legacy:
                return {"section": legacy_name, "how": f"legacy [{legacy_name}] (pipe slot {slot} not saved yet)",
                        "note": (f"DesktopLUT keys {who} as [Display{slot}], which is not in the ini yet — read "
                                 f"the legacy [{legacy_name}] it was adopted from (migrated on the next save)")}
            return unresolved(f"DesktopLUT keys {who} as [Display{slot}], which is not in the ini "
                              f"(not saved yet, or the ini is not the running DesktopLUT's); ini has "
                              f"{_describe_displays(displays)}")
        by_path = _same(sec.get("DevicePath"), pipe_path)
        by_edid = _same(sec.get("EdidId"), pipe_edid)
        if by_path or by_edid:
            return {"section": f"Display{slot}",
                    "how": f"[Display{slot}] = pipe settings_slot, verified by "
                           + ("device path" if by_path else "EDID id"), "note": None}
        if by_path is None and by_edid is None:
            return {"section": f"Display{slot}", "how": f"[Display{slot}] = pipe settings_slot (unverified)",
                    "note": (f"[Display{slot}] resolved by the pipe's settings_slot alone for {who} — no "
                             "DevicePath/EdidId pair to cross-check")}
        return unresolved(f"DesktopLUT keys {who} as [Display{slot}], but that section in the ini is "
                          f"{sec.get('EdidId') or sec.get('DevicePath')} — the ini is out of sync with the "
                          "running DesktopLUT")

    if slot is not None:   # -1: identity query failed, settings attached by index, never persisted
        if has_legacy:
            return {"section": legacy_name, "how": f"legacy [{legacy_name}] (display unidentified, index-attached)",
                    "note": (f"DesktopLUT could not identify monitor {n} (settings_slot -1): its settings are "
                             f"attached by index from [{legacy_name}] and are not persisted")}
        return unresolved(f"DesktopLUT could not identify monitor {n} (settings_slot -1): its settings are "
                          "attached by index and not persisted in the ini")

    if pipe_path or pipe_edid:
        for field, value, label in (("DevicePath", pipe_path, "device path"), ("EdidId", pipe_edid, "EDID id")):
            hits = [s for s, sec in displays.items() if _same(sec.get(field), value)]
            if len(hits) == 1:
                return {"section": f"Display{hits[0]}", "how": f"[Display{hits[0]}] matched by {label}", "note": None}
            if len(hits) > 1:
                return unresolved(f"{who}: {label} matches {len(hits)} sections "
                                  f"({', '.join(f'[Display{s}]' for s in sorted(hits))}) and the pipe reports "
                                  "no settings_slot to tell twins apart")
        if has_legacy:   # the C++ adopts [Monitor<N>] by index when no identity entry matches
            return {"section": legacy_name, "how": f"legacy [{legacy_name}] (no identity section matches)",
                    "note": (f"no [Display*] section matches {who}; read the legacy [{legacy_name}] the "
                             "C++ would adopt by index")}
        return unresolved(f"no [Display*] section matches {who}; ini has {_describe_displays(displays)}")

    leftover = (f"; the [{legacy_name}] beside them is an unclaimed pre-identity section, not trusted"
                if has_legacy else "")
    return unresolved(f"the ini keys displays by identity ({_describe_displays(displays)}) and the pipe "
                      f"reported no identity for monitor {n} — a slot number is not a monitor index{leftover}")


def _mode_flags(keys_in_section: Mapping[str, str], mode: str, keys: tuple[str, ...]) -> dict[str, str]:
    prefix = str(mode).upper() + "_"
    wanted = set(keys)
    return {k[len(prefix):]: v for k, v in keys_in_section.items()
            if k.startswith(prefix) and k[len(prefix):] in wanted}


def parse_ini_flags(text: str, monitor: int, mode: str,
                    keys: tuple[str, ...] = GUI_LAYER_KEYS, *,
                    identity: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
    """The monitor section's ``<MODE>_<key>`` values (prefix stripped, values as raw strings)
    from ini text; the section is resolved by :func:`resolve_ini_section` (``identity`` = the
    pipe's :func:`monitor_identity`). ``{}`` when the section is absent or unresolved."""
    sections = _ini_sections(text)
    name = resolve_ini_section(sections, monitor, identity)["section"]
    return _mode_flags(sections.get(name.lower(), {}), mode, keys) if name else {}


def load_ini_flags(path: Optional[Path | str], monitor: int, mode: str, *,
                   identity: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """``{"flags", "section", "resolution", "note"}`` for the ini at ``path`` (never raises: the
    audit must degrade gracefully on the mock / a profile without ``paths.desktoplut_ini``).
    ``flags`` is ``{}`` with a ``note`` when there is no path, the file is unreadable, the
    monitor's section cannot be resolved, or it has no ``<MODE>_*`` keys; a section resolved
    with a caveat returns its flags AND a note."""
    out: dict[str, Any] = {"flags": {}, "section": None, "resolution": None, "note": None}
    if not path:
        out["note"] = "no DesktopLUT.ini configured (profile paths.desktoplut_ini) — GUI-layer flags unknown"
        return out
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        out["note"] = f"DesktopLUT.ini unreadable ({p}): {type(exc).__name__}: {exc}"
        return out
    sections = _ini_sections(text)
    res = resolve_ini_section(sections, monitor, identity)
    out["section"], out["resolution"] = res["section"], res["how"]
    if not res["section"]:
        out["note"] = (f"DesktopLUT.ini section for monitor {int(monitor)} could not be resolved: {res['note']} "
                       f"— GUI-layer flags unknown ({p})")
        return out
    flags = _mode_flags(sections.get(res["section"].lower(), {}), mode, GUI_LAYER_KEYS)
    if not flags:
        out["note"] = f"no [{res['section']}] {str(mode).upper()}_* keys in {p}"
        return out
    out["flags"] = flags
    out["note"] = res["note"]
    return out


def read_ini_flags(path: Optional[Path | str], monitor: int, mode: str, *,
                   identity: Optional[Mapping[str, Any]] = None) -> tuple[dict[str, str], Optional[str]]:
    """``(flags, note)`` — :func:`load_ini_flags` without the section bookkeeping: ``({}, reason)``
    when there is no path, the file is unreadable, or the monitor section is missing/unresolved;
    ``(flags, caveat)`` when the section was resolved with a caveat."""
    r = load_ini_flags(path, monitor, mode, identity=identity)
    return r["flags"], r["note"]


def resolve_desktoplut_ini(paths: Mapping[str, Any], *, cwd: Optional[Path] = None) -> Optional[Path]:
    """Locate the live ``DesktopLUT.ini`` from a profile's ``paths``: ``desktoplut_ini``
    (absolute, or relative to ``cwd``), else the sibling of ``desktoplut_exe`` when that is
    set. ``None`` when unset/missing — the same resolution the run's settings backup uses."""
    base = cwd or Path.cwd()
    candidates: list[Path] = []
    configured = paths.get("desktoplut_ini")
    if configured:
        p = Path(configured)
        candidates.append(p if p.is_absolute() else base / p)
    exe = paths.get("desktoplut_exe")
    if exe:
        e = Path(exe)
        e = e if e.is_absolute() else base / e
        candidates.append(e.parent / "DesktopLUT.ini")
    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return None


def neutral_state_audit(controller: Any, monitor: int, mode: str, *,
                        ini_path: Optional[Path | str] = None) -> dict[str, Any]:
    """One evidence dict for the seam digest (pipe + ini), never raising on a degraded
    source — a dead pipe or missing ini is REPORTED (``notes``), the deterministic verdicts
    are derived from whatever was readable:

    * ``calibration_status`` — ``calibration.status`` (``{}`` + note on failure)
    * ``mhc`` / ``runtime`` — the ``state()`` entries for ``<monitor>:<MODE>``
    * ``profile_name`` / ``mhc_associated`` — is an MHC profile associated for the key
    * ``flags`` — the ini GUI-layer flags (``{}`` + note when unavailable)
    * ``ini_section`` / ``ini_resolution`` / ``monitor_identity`` — which ini section the flags
      came from and how it was resolved (:func:`resolve_ini_section`), and the pipe identity
      (``windows.query_monitors``, read only when an ini is configured) it was resolved with
    * ``gui_layers_enabled`` — the labels of layers the ini says are ON (subset of
      tonemap / Desktop Gamma / WB / GS / FALD); empty when the ini is unavailable
    * ``hook`` / ``overlay`` — which path renders the frame the meter sees: the DWM hook's
      ``{active, needs_check}`` and the overlay's ``{awake, dwm_hook_mode}`` (``None`` on a build
      that does not report them). Evidence, not a verdict: the awake overlay reads 0.5-2.4 %
      below the sleeping one at low levels (fald-lessons item 5), so the run record must say
      which path the calibration was measured through.
    """
    key = f"{int(monitor)}:{str(mode).upper()}"
    out: dict[str, Any] = {"key": key, "notes": []}
    try:
        out["calibration_status"] = dict(controller.calibration_status() or {})
    except Exception as exc:  # noqa: BLE001 - advisory read; the verdict below says what was readable
        out["calibration_status"] = {}
        out["notes"].append(f"calibration.status unavailable: {type(exc).__name__}: {exc}")
    mhc_entry: dict[str, Any] = {}
    runtime_entry: dict[str, Any] = {}
    pipe_layers: Optional[dict[str, Any]] = None
    try:
        state = controller.state() or {}
        mhc_entry = dict((state.get("mhc") or {}).get(key) or {})
        runtime_entry = dict((state.get("runtime") or {}).get(key) or {})
        pl = (state.get("layers") or {}).get(key)
        pipe_layers = dict(pl) if isinstance(pl, dict) else None
        hook = state.get("hook")
        out["hook"] = ({"active": bool(hook.get("active")), "needs_check": bool(hook.get("needs_check"))}
                       if isinstance(hook, dict) else None)
        overlay = state.get("overlay")
        out["overlay"] = dict(overlay) if isinstance(overlay, dict) else None
        out["state_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["state_ok"] = False
        out["notes"].append(f"state.get unavailable: {type(exc).__name__}: {exc}")
    out["mhc"] = mhc_entry
    out["runtime"] = runtime_entry
    profile_name = mhc_entry.get("profile_name")
    out["profile_name"] = profile_name
    # C++ reports {applied, profile_name} (enabled on older builds); the mock mirrors applied.
    out["mhc_associated"] = bool(profile_name or mhc_entry.get("applied") or mhc_entry.get("enabled"))
    # The ini keys displays by identity ([Display<slot>], DesktopLUT 2026-09-14+): only the running
    # DesktopLUT knows which slot monitor N is attached to, so ask the pipe (read-only).
    identity: Optional[dict[str, Any]] = None
    identity_note: Optional[str] = None
    if ini_path:
        try:
            identity = monitor_identity(controller.query_monitors() or {}, monitor)
            if identity is None:
                identity_note = f"windows.query_monitors does not list monitor {int(monitor)}"
        except Exception as exc:  # noqa: BLE001 - old build / mock / dead pipe: the resolver says what it lacked
            identity_note = f"windows.query_monitors unavailable: {type(exc).__name__}: {exc}"
    ini = load_ini_flags(ini_path, monitor, mode, identity=identity)
    flags, note = ini["flags"], ini["note"]
    if note and ini["resolution"] == "unresolved" and identity_note:
        note = f"{note} [{identity_note}]"
    out["ini_path"] = str(ini_path) if ini_path else None
    out["ini_section"] = ini["section"]
    out["ini_resolution"] = ini["resolution"]
    out["monitor_identity"] = identity
    out["flags"] = flags
    if note:
        out["notes"].append(note)
    ini_on = [label for k, label in _LAYER_LABELS.items() if ini_true(flags.get(k, "false"))]
    out["pipe_layers"] = pipe_layers
    if pipe_layers is not None:
        # The live server reports the layers (2026-09-03 `layers` in state.get): that is the
        # truth — the ini is written on change but can lag or belong to a previous session.
        pipe_on = [label for name, label in _PIPE_LAYER_LABELS.items() if pipe_layers.get(name)]
        out["gui_layers_enabled"] = pipe_on
        out["gui_layers_source"] = "pipe"
        if flags and sorted(pipe_on) != sorted(ini_on):
            out["notes"].append(f"ini layer flags ({', '.join(ini_on) or 'none'} ON) disagree with the "
                                f"pipe ({', '.join(pipe_on) or 'none'} ON) — the pipe is authoritative")
    else:
        out["gui_layers_enabled"] = ini_on
        out["gui_layers_source"] = "ini"
    if flags:
        out["ini_profile"] = (flags.get("MHCProfilePath") or "").replace("\\", "/").split("/")[-1] or None
    return out


def neutral_violations(audit: Mapping[str, Any], *, require_profile: bool = True) -> list[str]:
    """The MECHANICAL refusal reasons (each a provable yes/no from the audit): a GUI layer
    still ON for the calibrated mode after enter-neutral, or (``require_profile``) no MHC
    profile associated — i.e. the identity association did not land. An unreadable ini is
    NOT a violation (it is a note the LLM weighs); an unreadable pipe with
    ``require_profile`` IS (the association cannot be confirmed)."""
    bad: list[str] = []
    for label in audit.get("gui_layers_enabled") or []:
        bad.append(f"{label} is still ON for {audit.get('key')} in DesktopLUT.ini after enter-neutral")
    if require_profile and not audit.get("mhc_associated"):
        if audit.get("state_ok", True):
            bad.append(f"no MHC profile is associated for {audit.get('key')} — the identity "
                       "association did not land (Windows keeps the last MHC2 transform; "
                       "the panel is NOT neutral)")
        else:
            bad.append(f"cannot confirm the identity MHC association for {audit.get('key')}: "
                       + "; ".join(audit.get("notes") or ["pipe unreadable"]))
    return bad
