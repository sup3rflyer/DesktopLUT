"""What a measurement asks of the camera - one backend-neutral description.

Every field is optional (``None`` = leave as is). Values are *requests*: a backend applies them, reads the result back
from the camera HAL, and either reports exactly what it achieved or raises :class:`Unachievable` - it never silently
snaps to "close enough" (DLC design law: no silent non-trivial decisions; the caller sees the supported options).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


class BackendError(RuntimeError):
    """The camera app / API misbehaved (unreachable, rejected a request, did not record ...)."""


class Unachievable(BackendError):
    """The requested value cannot be set exactly; the message lists what *can* be."""


class Unsupported(BackendError):
    """This backend cannot do what was asked (e.g. 120 fps on the 60 fps Blackmagic app)."""


LENS_ALIASES = {"main": "1x", "wide": "1x", "uw": ".6x", "ultrawide": ".6x", "ultra-wide": ".6x", "tele": "3x",
                "telephoto": "3x"}


@dataclass
class Settings:
    fps: float | None = None
    size: tuple[int, int] | None = None        # recorded resolution (w, h)
    codec: str | None = None                   # "h264" | "hevc"
    iso: int | None = None
    shutter_s: float | None = None             # exposure time, e.g. 1/120
    wb_k: int | None = None                    # white balance, Kelvin
    tint: int | None = None
    focus: float | None = None                 # 0..1 normalised (0 = near, 1 = far); also locks autofocus off
    lens: str | None = None                    # "main" | "uw" | "tele" | camera id ("5")

    def given(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_kwargs(cls, **kw) -> "Settings":
        """Build from keyword args. Friendly forms are accepted: ``shutter`` (alias of ``shutter_s``), strings like
        ``shutter_s="1/120"`` and ``size="1920x1080"``."""
        if "shutter" in kw:
            if "shutter_s" in kw:
                raise TypeError("give shutter or shutter_s, not both")
            kw["shutter_s"] = kw.pop("shutter")
        known = {f.name for f in fields(cls)}
        bad = set(kw) - known
        if bad:
            raise TypeError(f"unknown setting(s) {sorted(bad)}; known: {sorted(known)}")
        if isinstance(kw.get("shutter_s"), str):
            kw["shutter_s"] = parse_shutter(kw["shutter_s"])
        if isinstance(kw.get("size"), str):
            kw["size"] = parse_size(kw["size"])
        return cls(**kw)


def parse_shutter(text: str | float) -> float:
    """``'1/120'`` / ``'1/120s'`` / ``0.008333`` -> seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    t = text.strip().lower().rstrip("s")
    if t.startswith("1/"):
        return 1.0 / float(t[2:])
    return float(t)


def parse_size(text: str) -> tuple[int, int]:
    """``'1920x1080'`` -> (1920, 1080)."""
    w, h = text.lower().split("x")
    return int(w), int(h)
