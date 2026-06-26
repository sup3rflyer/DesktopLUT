"""Keep the box (and panel) awake for the duration of a calibration run.

A DLC run drives the meter for minutes to hours. Windows' idle timers will
otherwise fire mid-run on an aggressive power plan — display off, then system
sleep — blanking the panel and corrupting the in-flight measurement with dark /
garbage reads. Up to now a run only survived because the fullscreen presenter
(DaVinci Resolve, the DogeGen backend) *presumably* held a display-required lock
as the foreground app; that is unconfirmed and fragile, and it does nothing
during the compute-bound 3D-LUT build phase where no patch is being presented.

This module makes the spine own its own keep-awake instead. On Windows it asserts
``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)``
for the duration of a :func:`keep_awake` context and clears it
(``SetThreadExecutionState(ES_CONTINUOUS)``) on exit — including on exceptions and
at a seam abort, so the request never leaks past the run. ``ES_CONTINUOUS`` makes
the request sticky (no per-60s re-assertion needed; the manual stopgap re-asserted
only because it could not rely on the flag persisting), so a single assertion holds
the system and display awake until released or the asserting thread exits.

Cross-platform safe: a no-op (and harmless) on non-Windows. Reentrant: nested
``keep_awake`` contexts share one OS-level assertion via a refcount, so wrapping
both the whole run *and* each measure stage is safe — only the outermost exit
releases the request.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from typing import Iterator

# winbase.h SetThreadExecutionState flags.
ES_CONTINUOUS = 0x80000000        # the request stays in effect until the next call resets it
ES_SYSTEM_REQUIRED = 0x00000001   # forbid system sleep
ES_DISPLAY_REQUIRED = 0x00000002  # forbid the display turning off

_KEEP_AWAKE_FLAGS = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

_lock = threading.Lock()
_depth = 0


def _set_execution_state(flags: int) -> bool:
    """Call ``SetThreadExecutionState(flags)``; return True iff the OS accepted it.

    A no-op returning ``False`` on non-Windows, or if the call is unavailable /
    raises (keep-awake is best-effort — its failure must never break a run)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        # Returns the previous execution-state (a non-zero bitmask) on success,
        # NULL (0) on failure. c_uint keeps the 0x80000000 flag from being
        # mis-sized as a negative c_int.
        previous = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
        return previous != 0
    except Exception:  # noqa: BLE001 — best-effort; a meter run must not die on this
        return False


def _acquire() -> bool:
    """Assert the system+display keep-awake request. Best-effort (see module doc)."""
    return _set_execution_state(_KEEP_AWAKE_FLAGS)


def _release() -> bool:
    """Clear the request — ``ES_CONTINUOUS`` alone resets the idle timers to normal."""
    return _set_execution_state(ES_CONTINUOUS)


def is_active() -> bool:
    """True while at least one :func:`keep_awake` context is held (test/introspection)."""
    with _lock:
        return _depth > 0


@contextlib.contextmanager
def keep_awake(*, reason: str = "") -> Iterator[bool]:
    """Hold a system + display keep-awake request for the duration of the context.

    Reentrant and refcounted: the OS request is asserted on the outermost enter and
    released on the outermost exit, so it is safe to wrap the whole run AND each
    measure stage — the inner contexts are cheap no-ops. The request is released in
    a ``finally`` so an exception (a seam abort, a stalled measure) never leaks it.

    ``reason`` is accepted for call-site readability only (it has no effect on the
    OS call). Yields ``True`` when an OS-level keep-awake is in force (Windows, call
    accepted), ``False`` when this is a no-op (non-Windows / call unavailable)."""
    global _depth
    with _lock:
        outermost = _depth == 0
        active = _acquire() if outermost else (_depth > 0)
        _depth += 1
    try:
        yield active
    finally:
        with _lock:
            _depth -= 1
            if _depth == 0:
                _release()
