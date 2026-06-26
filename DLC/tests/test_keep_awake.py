"""Unit tests for the cross-platform keep-awake context manager.

These exercise the refcount/acquire/release contract directly (no spine, no
display) by recording the flags handed to the OS call, so they pass on any
platform — the Windows ``SetThreadExecutionState`` is the only OS-specific bit
and it lives behind ``_set_execution_state``, which we stub here.
"""

from __future__ import annotations

import pytest

from dlc import keep_awake as ka


@pytest.fixture
def recorder(monkeypatch):
    """Record every flags value passed to the OS call; report 'success' to the CM."""
    calls: list[int] = []

    def fake(flags: int) -> bool:
        calls.append(flags)
        return True  # pretend Windows accepted it so the CM yields True

    monkeypatch.setattr(ka, "_set_execution_state", fake)
    # Guard against cross-test refcount leakage.
    monkeypatch.setattr(ka, "_depth", 0)
    return calls


def test_acquire_and_release_flags(recorder):
    assert not ka.is_active()
    with ka.keep_awake() as active:
        assert active is True
        assert ka.is_active()
    assert not ka.is_active()
    # Exactly one acquire (system+display) and one release (continuous-only).
    assert recorder == [
        ka.ES_CONTINUOUS | ka.ES_SYSTEM_REQUIRED | ka.ES_DISPLAY_REQUIRED,
        ka.ES_CONTINUOUS,
    ]


def test_releases_on_exception(recorder):
    with pytest.raises(RuntimeError):
        with ka.keep_awake():
            assert ka.is_active()
            raise RuntimeError("seam abort mid-measure")
    # The request must not leak past the exception.
    assert not ka.is_active()
    assert recorder[-1] == ka.ES_CONTINUOUS


def test_reentrant_single_os_assertion(recorder):
    # Nested contexts (whole-run wrap + per-stage wrap) share ONE OS assertion:
    # acquire on the outermost enter, release only on the outermost exit.
    with ka.keep_awake():
        with ka.keep_awake():
            assert ka.is_active()
        # inner exit must NOT have released — still held by the outer context
        assert ka.is_active()
        assert recorder == [ka.ES_CONTINUOUS | ka.ES_SYSTEM_REQUIRED | ka.ES_DISPLAY_REQUIRED]
    assert not ka.is_active()
    assert recorder == [
        ka.ES_CONTINUOUS | ka.ES_SYSTEM_REQUIRED | ka.ES_DISPLAY_REQUIRED,
        ka.ES_CONTINUOUS,
    ]


def test_no_op_yields_false_when_os_call_unavailable(monkeypatch):
    # Non-Windows / unavailable call: a clean no-op that still tracks depth so the
    # CM is safe to wrap around anything.
    monkeypatch.setattr(ka, "_depth", 0)
    monkeypatch.setattr(ka, "_set_execution_state", lambda flags: False)
    with ka.keep_awake() as active:
        assert active is False
        assert ka.is_active()   # refcount still tracks even when the OS call is a no-op
    assert not ka.is_active()
