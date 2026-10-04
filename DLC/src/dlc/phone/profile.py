"""mcpro24fps settings profile (the app's Export / Import JSON), decoded.

The file is ``{"p": "<manufacturer model>", "m01".."m09": <obfuscated blob>}``. Each blob is a JSON object of Android
SharedPreferences (keys prefixed ``BOOLEAN_/INTEGER_/LONG_/FLOAT_/STRING_`` by type), encoded as: UTF-8 -> base64 ->
split into 3-character chunks -> every full chunk except the last is rotated left by one character (``abc`` ->
``bca``). Reverse-engineered from the APK (``ta0.F2`` / ``ta0.D2``, build 043de); round-trips the real export exactly.

``m01`` carries the camera/video settings that matter (``STRING_codec``, ``INTEGER_bits``, ``INTEGER_nifps_0`` ...).
Known limits (HW, 2026-10-04): an import restarts the app; codec / bit depth / curve preset *do* apply from the file,
manual ISO and shutter do NOT (the live values overwrite them) - set those through the UI (``Mcpro.set_iso`` ...).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path


def _chunks(s: str) -> list[str]:
    return [s[i:i + 3] for i in range(0, len(s), 3)]


def decode_blob(blob: str) -> dict:
    ch = _chunks(blob)
    n = len(ch)
    b64 = "".join(c[2:] + c[:2] if len(c) >= 3 and i <= n - 2 else c for i, c in enumerate(ch))
    return json.loads(base64.b64decode(b64).decode())


def encode_blob(obj: dict) -> str:
    b64 = base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()
    ch = _chunks(b64)
    n = len(ch)
    return "".join(c[1:] + c[:1] if len(c) >= 3 and i <= n - 2 else c for i, c in enumerate(ch))


def load_profile(path: str | Path) -> dict:
    """Decode an exported file to ``{"p": model, "m01": {...}, ...}``."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: (v if k == "p" else decode_blob(v)) for k, v in raw.items()}


def dump_profile(profile: dict, path: str | Path) -> Path:
    raw = {k: (v if k == "p" else encode_blob(v)) for k, v in profile.items()}
    path = Path(path)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


_TYPES = {"BOOLEAN": lambda v: bool(v) if isinstance(v, bool) else str(v).lower() == "true",
          "INTEGER": int, "LONG": int, "FLOAT": float, "STRING": str}


def set_pref(profile: dict, key: str, value, section: str = "m01") -> dict:
    """Set one typed pref (``key`` carries its type prefix, e.g. ``INTEGER_bits``); returns the profile."""
    typ = key.split("_", 1)[0]
    if typ not in _TYPES:
        raise KeyError(f"pref key {key!r} has no BOOLEAN_/INTEGER_/LONG_/FLOAT_/STRING_ prefix")
    profile[section][key] = _TYPES[typ](value)
    return profile
