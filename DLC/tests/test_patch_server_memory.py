"""A run started against a patch server (``--dogegen-server``) remembers it: a resumed step without the flag reuses
it, and a remembered server that does not answer stops the run (never a second, windowed dogegen measuring a
different picture — 2026-11-14, run 143042). ``--keep-dogegen-server`` is remembered too. Mock presenters only."""

from __future__ import annotations

from dlc.calibrate import open_patch_server, remember_patch_server, resolve_patch_server


class _Presenter:
    """Mock SocketPresenter: answers its mode, or raises like an unreachable daemon."""
    made: list = []

    def __init__(self, host, port, *, settle_seconds, alive=True):
        self.host, self.port, self.settle, self.alive, self.closed = host, port, settle_seconds, alive, False
        _Presenter.made.append(self)

    def query_mode(self):
        if not self.alive:
            raise ConnectionRefusedError("[WinError 10061] No connection could be made")
        return {"mode": "SDR", "bit_depth": 10}

    def close(self):
        self.closed = True


def _dead(host, port, *, settle_seconds):
    return _Presenter(host, port, settle_seconds=settle_seconds, alive=False)


def test_first_invocation_is_remembered_and_a_resume_reuses_it():
    state: dict = {}
    ps = resolve_patch_server("127.0.0.1:28930", True, state)
    assert ps == {"address": "127.0.0.1:28930", "keep": True, "remembered": False}
    remember_patch_server(state, ps)
    assert state["patch_server"] == {"address": "127.0.0.1:28930", "keep": True}
    # the resumed step: `--run ... --decide ...` without --dogegen-server / --keep-dogegen-server
    again = resolve_patch_server(None, False, state)
    assert again == {"address": "127.0.0.1:28930", "keep": True, "remembered": True}


def test_a_run_never_started_against_a_server_keeps_today_behaviour():
    assert resolve_patch_server(None, False, {}) == {"address": None, "keep": False, "remembered": False}


def test_naming_another_server_on_resume_wins_and_keep_is_not_inherited_from_another_address():
    state = {"patch_server": {"address": "127.0.0.1:28930", "keep": True}}
    ps = resolve_patch_server("127.0.0.1:28931", False, state)
    assert ps == {"address": "127.0.0.1:28931", "keep": False, "remembered": False}
    same = resolve_patch_server("127.0.0.1:28930", False, state)
    assert same["keep"] is True                                     # same address: the remembered keep stands


def test_a_remembered_server_that_answers_is_used():
    presenter, mode, refusal = open_patch_server("127.0.0.1:28930", 0.5, remembered=True, factory=_Presenter)
    assert refusal is None and mode == {"mode": "SDR", "bit_depth": 10}
    assert (presenter.host, presenter.port, presenter.settle) == ("127.0.0.1", 28930, 0.5)


def test_a_remembered_server_that_does_not_answer_stops_the_run():
    logs = []
    presenter, mode, refusal = open_patch_server("127.0.0.1:28930", 0.5, remembered=True, factory=_dead,
                                                 log=logs.append)
    assert presenter is None and mode is None
    assert "does not answer" in refusal and "second, windowed patch generator" in refusal
    assert _Presenter.made[-1].closed and logs


def test_a_named_server_that_does_not_answer_keeps_today_behaviour():
    presenter, mode, refusal = open_patch_server("127.0.0.1:28930", 0.5, remembered=False, factory=_dead)
    assert refusal is None and mode is None and presenter is not None
