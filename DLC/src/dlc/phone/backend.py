"""The camera-app backend interface PhoneRig drives.

Two implementations: :mod:`.blackmagic` (REST, <= 60 fps, deterministic - preferred) and :mod:`.mcpro_backend`
(UI automation, the only route to 120/240 fps constrained-high-speed). Both expose the same few verbs, so a test
asks for ``fps=60, iso=400`` and the rig picks - the test never mentions an app.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .settings import Settings


class CameraBackend(ABC):
    name: str = "?"
    max_fps: float = 0.0
    media_dir: str = ""                      # phone folder the app saves clips to (``adb pull`` source)
    supports_clip_time: bool = False         # can report the running clip's own timecode (frame-accurate marks)

    @abstractmethod
    def ready(self) -> tuple[bool, str]:
        """``(usable_now, why_not)`` - cheap probe, no side effects."""

    @abstractmethod
    def prepare(self) -> None:
        """Bring the app/API up (launch, enable server, dismiss screen guards). Raises BackendError."""

    @abstractmethod
    def state(self) -> dict:
        """Backend-neutral snapshot: fps, size, codec, iso, shutter_s, wb_k, focus, lens, recording, ... (best effort)."""

    @abstractmethod
    def apply(self, s: Settings) -> dict:
        """Set what is given; return what the camera *actually* reports afterwards. Raises Unachievable/Unsupported."""

    @abstractmethod
    def start(self) -> dict:
        """Begin recording; returns once the camera confirms it is recording. The returned JSON-serialisable *token*
        carries what ``stop`` needs, so start and stop may happen in different processes (CLI ``rec start`` / ``stop``)."""

    @abstractmethod
    def stop(self, token: dict) -> str:
        """Stop recording; return the new clip's file name inside ``media_dir`` once the file is closed."""

    def clip_time(self, token: dict | None = None) -> float | None:
        """Seconds into the running clip, or None if this backend cannot say."""
        return None

    def describe(self) -> dict:
        return dict(name=self.name, max_fps=self.max_fps, media_dir=self.media_dir,
                    clip_time=self.supports_clip_time)
