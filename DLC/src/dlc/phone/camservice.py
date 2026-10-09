"""Parse ``dumpsys media.camera`` into what the camera HAL is *actually* doing right now.

The camera service keeps the last CaptureRequest of the live session, so this is the ground truth for exposure
time / ISO / frame duration / processing modes - independent of what an app's UI claims. It also tells a normal
session from a CONSTRAINED_HIGH_SPEED one and which physical camera each stream reads.

The request is kept WHOLE: every entry of the live "Logical request settings" block - ``android.*`` and vendor tags
(``com.samsung.*``, ``samsung.android.*``, ...) alike, multi-value arrays and entries whose values wrap over several
lines - lands in :attr:`CameraSession.request`, so a manifest carries the full processing state (tonemap, colour
correction, noise reduction, edge, vendor knobs) and a later "the camera's response changed" can be traced to a key.
``REQUEST_KEYS`` is only the curated list the convenience accessors / docs care about; parsing is generic.

Key naming: ``android.`` is stripped (``sensor.sensitivity``, as before); vendor tags keep their full dotted name; a
name the service could not resolve (``unknownSection.unknownTag``) or a repeated name gets ``@<hex tag id>`` appended so
no entry is lost. Values are the bracketed text, whitespace-normalised, lines of a multi-line entry joined by a space
(``"120 120"``, ``"OFF"``, ``"(1 / 1) (0 / 1) ..."``).

Quirks handled: CRLF; ``Dumpsys from previous open session`` blocks (a *previous* session of that device - dropped,
up to the next ``== ... ==`` section); the metadata preamble lines; the request block exists only while a device is open.
Per-physical-camera requests (``Physical request settings for camera id N``) are parsed the same way into
:attr:`CameraSession.physical_requests`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REQUEST_KEYS = (
    "sensor.exposureTime", "sensor.sensitivity", "sensor.frameDuration",
    "control.aeMode", "control.mode", "control.aeLock", "control.aeTargetFpsRange",
    "control.awbMode", "control.awbLock", "control.afMode", "control.zoomRatio",
    "control.videoStabilizationMode", "control.postRawSensitivityBoost", "control.aeAntibandingMode",
    "lens.focusDistance", "lens.opticalStabilizationMode",
    "noiseReduction.mode", "edge.mode", "shading.mode", "tonemap.mode", "hotPixel.mode",
    "colorCorrection.mode", "colorCorrection.gains", "blackLevel.lock", "distortionCorrection.mode",
)

# "  android.control.aeMode (10003): byte[1]" / "  com.samsung.android.control.foo (80010002): int32[4]"
_ENTRY = re.compile(r"^\s*(\S+) \(([0-9a-fA-F]+)\): (\w+)\[(\d+)\]\s*$")
_VALUES = re.compile(r"^\s*\[(.*)\]\s*$")
_PREAMBLE = re.compile(r"^\s*(Dumping camera metadata array|Version: )")
_PREVIOUS = "Dumpsys from previous open session"


@dataclass
class CameraSession:
    open: bool = False
    client: str = ""
    opmode: str = ""
    streams: list[dict] = field(default_factory=list)  # {w, h, format, dataspace, physical}
    request: dict[str, str] = field(default_factory=dict)              # every key of the live logical request
    physical_requests: dict[str, dict[str, str]] = field(default_factory=dict)   # camera id -> its request

    @property
    def high_speed(self) -> bool:
        return self.opmode.startswith("CONSTRAINED_HIGH_SPEED")

    @property
    def physical_ids(self) -> set[str]:
        return {s["physical"] for s in self.streams if s.get("physical")}

    # -- convenience, all in SI / plain numbers ---------------------------------------------------------
    def exposure_s(self) -> float | None:
        v = self.request.get("sensor.exposureTime")
        return int(v) * 1e-9 if v and v.lstrip("-").isdigit() else None

    def iso(self) -> int | None:
        v = self.request.get("sensor.sensitivity")
        return int(v) if v and v.isdigit() else None

    def target_fps(self) -> tuple[int, int] | None:
        v = self.request.get("control.aeTargetFpsRange")
        nums = re.findall(r"\d+", v or "")
        return (int(nums[0]), int(nums[1])) if len(nums) >= 2 else None

    def manual_exposure(self) -> bool:
        return self.request.get("control.aeMode") == "OFF"

    def curated(self) -> dict[str, str]:
        """Just the :data:`REQUEST_KEYS` that are present (the short view; :attr:`request` has everything)."""
        return {k: self.request[k] for k in REQUEST_KEYS if k in self.request}


def drop_previous_sessions(text: str) -> str:
    """Remove every ``Dumpsys from previous open session`` block (up to the next ``== ... ==`` section header)."""
    out: list[str] = []
    skipping = False
    for line in text.split("\n"):
        if _PREVIOUS in line:
            skipping = True
            continue
        if skipping and line.startswith("== "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def parse_metadata_block(lines: list[str], start: int) -> dict[str, str]:
    """Parse one ``CameraMetadata::dump`` block whose entries start after ``lines[start]`` (the block's title line).

    Generic: every ``<name> (<hex tag>): <type>[<count>]`` entry followed by one or more ``[ ... ]`` value lines. Stops
    at the first line that is neither (the next section), after skipping the metadata preamble."""
    out: dict[str, str] = {}
    key: str | None = None
    vals: list[str] = []

    def flush():
        if key is not None:
            out[key] = " ".join(" ".join(vals).split())

    for line in lines[start + 1:]:
        em = _ENTRY.match(line)
        if em:
            flush()
            name, tag = em.group(1), em.group(2).lower()
            key = name[len("android."):] if name.startswith("android.") else name
            if key.startswith("unknownSection.") or key in out:
                key = f"{key}@{tag}"
            vals = []
            continue
        vm = _VALUES.match(line)
        if vm and key is not None:
            vals.append(vm.group(1))
            continue
        if key is None and (not line.strip() or _PREAMBLE.match(line)):
            continue
        break
    flush()
    return out


def parse_camera_dump(text: str) -> CameraSession:
    live = drop_previous_sessions(text.replace("\r", ""))
    sess = CameraSession()
    m = re.search(r"Device \d+ is open\. Client instance dump:", live)
    sess.open = m is not None
    cm = re.search(r"Client package: (\S+)", live)
    sess.client = cm.group(1) if cm else ""
    om = re.search(r"Operation mode: (\S+)", live)
    sess.opmode = om.group(1) if om else ""
    dims = list(re.finditer(r"Dims: (\d+) x (\d+), format (0x[0-9a-f]+), dataspace (0x[0-9a-f]+)", live))
    for i, sm in enumerate(dims):
        end = dims[i + 1].start() if i + 1 < len(dims) else sm.end() + 1500
        chunk = live[sm.end():end]
        pm = re.search(r"Physical camera id: (\S+)", chunk)  # absent on high-speed / single-sensor streams
        sess.streams.append(dict(w=int(sm[1]), h=int(sm[2]), format=sm[3], dataspace=sm[4],
                                 physical=pm.group(1) if pm else ""))
    lines = live.split("\n")
    seen_logical = False
    for i, line in enumerate(lines):
        if "Logical request settings" in line:
            if seen_logical:
                break                                   # a second open device: the first one's request is kept
            seen_logical = True
            sess.request = parse_metadata_block(lines, i)
        elif seen_logical and line.startswith("== "):
            break                                       # next device / service section
        elif seen_logical:
            pm = re.search(r"Physical request settings for camera id (\S+?):?\s*$", line)
            if pm and pm.group(1) not in sess.physical_requests:
                sess.physical_requests[pm.group(1)] = parse_metadata_block(lines, i)
    return sess
