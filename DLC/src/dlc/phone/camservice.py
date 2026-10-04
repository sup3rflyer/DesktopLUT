"""Parse ``dumpsys media.camera`` into what the camera HAL is *actually* doing right now.

The camera service keeps the last CaptureRequest of the live session, so this is the ground truth for exposure
time / ISO / frame duration / processing modes - independent of what an app's UI claims. It also tells a normal
session from a CONSTRAINED_HIGH_SPEED one and which physical camera each stream reads.

Quirks handled: CRLF; a trailing ``Dumpsys from previous open session`` block (the *previous* session - ignored);
the request block is only present while a device is open.
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


@dataclass
class CameraSession:
    open: bool = False
    client: str = ""
    opmode: str = ""
    streams: list[dict] = field(default_factory=list)  # {w, h, format, dataspace, physical}
    request: dict[str, str] = field(default_factory=dict)

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


def parse_camera_dump(text: str) -> CameraSession:
    text = text.replace("\r", "")
    cut = text.find("Dumpsys from previous open session")
    live = text[:cut] if cut >= 0 else text
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
    start = live.find("Logical request settings")
    block = live[start:start + 60000] if start >= 0 else ""
    for key in REQUEST_KEYS:
        km = re.search(r"android\." + re.escape(key) + r" \([0-9a-f]+\): \S+\n\s+\[([^\]]*)\]", block)
        if km:
            sess.request[key] = km.group(1).strip()
    return sess
