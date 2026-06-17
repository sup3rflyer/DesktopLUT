"""Unit tests for the pure helpers of :mod:`dlc.dogegen_window`.

The Win32 placement itself needs a live window, so it is exercised on hardware, not here. These
tests cover the logic that decides *what* to do: the borderless-style math, render-window
selection, and rect parsing/resolution.
"""

from __future__ import annotations

from dlc import dogegen_window as dw


def test_borderless_placement_strips_only_overlapped_bits():
    # A typical resizable window style (WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_CLIPSIBLINGS).
    WS_VISIBLE = 0x10000000
    WS_CLIPSIBLINGS = 0x04000000
    style = dw.WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_CLIPSIBLINGS
    new_style, x, y, w, h = dw.compute_borderless_placement(style, (100, 200, 1920, 1080))
    # The frame bits are gone...
    assert not (new_style & dw.WS_OVERLAPPEDWINDOW)
    # ...but unrelated bits are preserved (matches dogegen's `g_wndStyle & ~WS_OVERLAPPEDWINDOW`).
    assert new_style & WS_VISIBLE
    assert new_style & WS_CLIPSIBLINGS
    assert (x, y, w, h) == (100, 200, 1920, 1080)


def test_rect_tuple_parses_query_monitors_shape():
    assert dw.rect_tuple({"x": 0, "y": 0, "width": 3840, "height": 2160}) == (0, 0, 3840, 2160)
    # Float-ish values coerce to int.
    assert dw.rect_tuple({"x": -1920.0, "y": 0.0, "width": 1920.0, "height": 1080.0}) == (-1920, 0, 1920, 1080)


def test_rect_tuple_rejects_missing_or_malformed():
    assert dw.rect_tuple(None) is None
    assert dw.rect_tuple({}) is None
    assert dw.rect_tuple({"x": 0, "y": 0, "width": 1920}) is None          # missing height
    assert dw.rect_tuple({"x": "n/a", "y": 0, "width": 1, "height": 1}) is None


def test_resolve_monitor_rect_picks_by_index():
    monitors = [
        {"index": 0, "rect": {"x": 0, "y": 0, "width": 2560, "height": 1440}, "primary": False},
        {"index": 1, "rect": {"x": 2560, "y": 0, "width": 1920, "height": 1080}, "primary": True},
    ]
    assert dw.resolve_monitor_rect(monitors, 0) == (0, 0, 2560, 1440)
    assert dw.resolve_monitor_rect(monitors, 1) == (2560, 0, 1920, 1080)


def test_resolve_monitor_rect_absent_index_or_empty():
    assert dw.resolve_monitor_rect([], 0) is None
    assert dw.resolve_monitor_rect(None, 0) is None
    assert dw.resolve_monitor_rect([{"index": 0, "rect": {"x": 0, "y": 0, "width": 1, "height": 1}}], 9) is None


def test_pick_render_window_prefers_visible_resizable_non_console():
    console = (1, dw.CONSOLE_CLASS, True, dw.WS_OVERLAPPEDWINDOW)
    hidden = (2, "DogeGenClass", False, dw.WS_OVERLAPPEDWINDOW)
    render = (3, "DogeGenClass", True, dw.WS_OVERLAPPEDWINDOW)
    # Skips the console even though it is visible+overlapped; skips the hidden window.
    assert dw.pick_render_window([console, hidden, render]) == 3


def test_pick_render_window_falls_back_to_any_visible_non_console():
    # A window that has already been stripped of WS_OVERLAPPEDWINDOW is still selectable.
    popup = (7, "DogeGenClass", True, 0x80000000)  # WS_POPUP, no overlapped bits
    assert dw.pick_render_window([popup]) == 7


def test_pick_render_window_none_when_only_console_or_hidden():
    assert dw.pick_render_window([(1, dw.CONSOLE_CLASS, True, dw.WS_OVERLAPPEDWINDOW)]) is None
    assert dw.pick_render_window([(2, "DogeGenClass", False, dw.WS_OVERLAPPEDWINDOW)]) is None
    assert dw.pick_render_window([]) is None


def test_place_dogegen_best_effort_when_no_window():
    # A pid with no top-level windows must not raise — it returns ok=False with a reason so the
    # caller can fall back to the manual Alt+Enter path. (Uses a tiny timeout to stay fast.)
    res = dw.place_dogegen(0x7FFFFFFF, rect=(0, 0, 100, 100), fullscreen=True, timeout=0.2)
    assert res["ok"] is False
    assert res["reason"]
    assert res["fullscreen"] is True
