"""Clip-to-clip camera evidence: flatten a backend state into comparable keys, diff two snapshots.

Why: on 2026-10-08 the phone camera's response curve changed between two clips (~18:00) and the manifests could not
say why - they kept a handful of HAL fields, not the request's processing modes, vendor tags or the app's settings.
Every manifest now carries the full state at clip start and stop; this module turns two of them into the list of keys
that changed. The result is EVIDENCE for the overseeing LLM (manifest ``changed_vs_prev`` / ``changed_during`` and a
session-summary warning) - never a gate: nothing is refused, retried or auto-accepted on it (DLC design law).

The requested exposure (ISO / exposure time, in the HAL request and the app's ``/video/iso`` / ``/video/shutter``) is
excluded: it changes on purpose between and within clips and is recorded elsewhere (marks, ``state.iso``).
"""

from __future__ import annotations

import json

EXPOSURE_REQUEST_KEYS = ("sensor.sensitivity", "sensor.exposureTime")
EXPOSURE_APP_PATHS = ("/video/iso", "/video/shutter")


def flatten(state: dict | None) -> dict[str, object]:
    """Comparable ``key -> scalar`` view of a backend :meth:`state` (its ``hal`` block and, if any, ``app`` block).

    Keys: ``hal.open|client|opmode|streams``, ``hal.request.<request key>``, ``hal.physical.<camera id>.<request key>``,
    ``app.<REST path>.<field>[.<field>...]``; lists become canonical JSON strings."""
    out: dict[str, object] = {}
    st = state or {}
    hal = st.get("hal") or {}
    for k in ("open", "client", "opmode"):
        if k in hal:
            out[f"hal.{k}"] = hal[k]
    if "streams" in hal:
        out["hal.streams"] = json.dumps(hal["streams"], sort_keys=True)
    for k, v in (hal.get("request") or {}).items():
        out[f"hal.request.{k}"] = v
    for cid, req in (hal.get("physical_requests") or {}).items():
        for k, v in (req or {}).items():
            out[f"hal.physical.{cid}.{k}"] = v

    def walk(prefix: str, obj) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(f"{prefix}.{k}", v)
        elif isinstance(obj, (list, tuple)):
            out[prefix] = json.dumps(obj, sort_keys=True)
        else:
            out[prefix] = obj

    for path, body in (st.get("app") or {}).items():
        walk(f"app.{path}", body)
    return out


def is_exposure_key(key: str) -> bool:
    """True for the keys that carry the *requested* exposure (excluded from the clip-to-clip diff)."""
    if key.startswith("hal.request."):
        return key[len("hal.request."):] in EXPOSURE_REQUEST_KEYS
    if key.startswith("hal.physical."):
        parts = key.split(".", 3)
        return len(parts) == 4 and parts[3] in EXPOSURE_REQUEST_KEYS
    return any(key == f"app.{p}" or key.startswith(f"app.{p}.") for p in EXPOSURE_APP_PATHS)


def diff(prev: dict[str, object] | None, now: dict[str, object] | None) -> dict[str, list]:
    """``{key: [prev, now]}`` for every non-exposure key whose value differs (missing = ``None``).

    HAL keys are compared only when the camera HAL was open in both snapshots (a closed session has no request - that
    shows as one ``hal.open`` change, not a hundred vanished keys); app keys only when both snapshots have them."""
    prev, now = prev or {}, now or {}
    both_hal = "hal.open" in prev and "hal.open" in now
    both_open = prev.get("hal.open") is True and now.get("hal.open") is True
    both_app = any(k.startswith("app.") for k in prev) and any(k.startswith("app.") for k in now)
    out: dict[str, list] = {}
    for k in sorted(set(prev) | set(now)):
        if is_exposure_key(k):
            continue
        if k == "hal.open" and not both_hal:
            continue
        if k.startswith("hal.") and k != "hal.open" and not both_open:
            continue
        if k.startswith("app.") and not both_app:
            continue
        a, b = prev.get(k), now.get(k)
        if a != b:
            out[k] = [a, b]
    return out


def describe(changes: dict[str, list], limit: int = 8, width: int = 48) -> str:
    """One line for a warning: ``key: a -> b; ...`` (values shortened; the manifest has them in full)."""
    def short(v) -> str:
        t = "-" if v is None else str(v)
        return t if len(t) <= width else t[:width - 3] + "..."

    items = [f"{k.removeprefix('hal.request.')}: {short(a)} -> {short(b)}" for k, (a, b) in changes.items()]
    more = len(items) - limit
    return "; ".join(items[:limit]) + (f"; (+{more} more)" if more > 0 else "")
