"""Phone-camera control + capture framework for DLC measurements (camera-based tests).

Layers (each importable and testable alone):

* :mod:`.adb`        - subprocess adb wrapper (input, files, clock offset).
* :mod:`.camservice` - parse ``dumpsys media.camera``: what the sensor was *actually* asked to do.
* :mod:`.profile`    - decode / edit / encode the mcpro24fps settings export (profile import = declarative setup).
* :mod:`.mcpro`      - the mcpro24fps UI driver (state read-back, closed-loop ISO/shutter, record).
* :mod:`.clip`       - ffprobe the recorded file and check it against what the run asked for.
* :mod:`.session`    - :class:`PhoneRig`: preflight -> record -> pull -> verify -> sidecar manifest.

Design stance (DLC law): the framework gathers evidence and refuses to *assume* - it never trusts a UI label over the
camera service or the file, and it reports problems for the LLM to judge rather than auto-accepting. Old coordinate-
based ``agent_phone*.py`` tools at the repo root are superseded by this for new work.
"""

from .adb import Adb, AdbError
from .clip import ClipInfo, check, probe
from .mcpro import Mcpro, McproError, McState
from .session import Capture, PhoneRig

__all__ = ["Adb", "AdbError", "Capture", "ClipInfo", "Mcpro", "McproError", "McState", "PhoneRig", "check", "probe"]
