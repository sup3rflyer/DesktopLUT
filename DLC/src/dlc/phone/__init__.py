"""Phone-camera control + capture framework for DLC measurements (camera-based tests).

Layers (each importable and testable alone):

* :mod:`.settings`   - :class:`Settings`: what a test asks of the camera (fps, size, iso, shutter, wb, focus, lens ...).
* :mod:`.adb`        - subprocess adb wrapper (input, files, clock offset, unlock).
* :mod:`.camservice` - parse ``dumpsys media.camera``: what the sensor was *actually* asked to do.
* :mod:`.profile`    - decode / edit / encode the mcpro24fps settings export (profile import = declarative setup).
* :mod:`.blackmagic` - Blackmagic Camera REST backend (<= 60 fps; deterministic, preferred).
* :mod:`.mcpro` / :mod:`.mcpro_backend` - mcpro24fps UI driver / backend (120-240 fps constrained high speed).
* :mod:`.clip`       - ffprobe the recorded file and check it against what the run asked for.
* :mod:`.session`    - :class:`PhoneRig`: ``set()`` / ``recording()`` / ``capture()`` -> verified clip + manifest.
* :mod:`.analysis`   - clip -> numbers: frames on container pts, ROI series, mean frames, fiducial homography,
  sync edges, tone curves (needs numpy; imported lazily so the control surface stays dependency-free).

Design stance (DLC law): the framework gathers evidence and refuses to *assume* - it never trusts a UI label over the
camera service or the file, and it reports problems for the LLM to judge rather than auto-accepting. Old coordinate-
based ``agent_phone*.py`` tools at the repo root are superseded by this for new work.
"""

from .adb import Adb, AdbError
from .clip import ClipInfo, check, probe
from .mcpro import Mcpro, McproError, McState
from .session import Capture, Mark, PhoneRig, Recording
from .settings import BackendError, Settings, Unachievable, Unsupported

# dlc.phone.analysis needs numpy (+ scipy for geometry): exported lazily so `import dlc.phone` stays dependency-free.
_ANALYSIS = ("FID2", "Edge", "FiducialError", "FiducialFit", "FiducialLayout", "Folded", "IDENTITY", "Levels",
             "PIX_FMTS", "RoiSeries", "Series", "ToneCurve", "apply_h", "clip_rotation", "epochs", "find_blobs",
             "fit_fiducials", "fold_by_period", "frame_slots", "homography", "iter_frames", "mean_frame", "roi",
             "roi_series", "sync_edges", "two_levels")


def __getattr__(name):
    if name in _ANALYSIS:
        from . import analysis
        return getattr(analysis, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Adb", "AdbError", "BackendError", "Capture", "ClipInfo", "Mark", "Mcpro", "McproError", "McState",
           "PhoneRig", "Recording", "Settings", "Unachievable", "Unsupported", "check", "probe", *_ANALYSIS]
