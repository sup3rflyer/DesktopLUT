"""Suite-wide pytest configuration — two independent parts.

1. RUNS-ROOT SANDBOX (bottom of the file). No test may touch the real ``DLC/runs``: it holds the
   owner's calibration runs, and ``runs/active.json`` is the pointer the live dashboard follows.
   The suite used to leave a ``runs/<ts>_sdr_x`` folder behind on every run and repoint
   ``active.json`` at a pytest tmp run — during a hardware calibration, that yanks the
   dashboard off the live run. Three layers, each catching what the one before cannot:

   * ``$DLC_RUNS_DIR`` points at a throwaway dir from conftest import on, and at a fresh tmp
     dir for every test; :func:`dlc.paths.runs_dir` honours it and any subprocess inherits it;
   * an audit hook BLOCKS every in-process create/write/rename/copy/delete under the real runs
     root and fails the test that made it — even when production code swallows the error, as
     the ``active.json`` publisher deliberately does. This is what catches a new
     ``from dlc.paths import RUNS_DIR``;
   * the controller snapshots the real root's folders and ``active.json`` target at session
     start: the session FAILS if ``active.json`` ends up aimed at a temp dir, and any other
     change is reported (not failed — it is most likely another session's real run).

2. SCHEDULING (``pytest_collection_modifyitems``). Changes no assertion. It exists because of how
   pytest-xdist hands work out, and that is what sets this suite's wall time.

Measured 2026-09-20 on a 32-thread box (``-n auto`` = 16 workers, the physical core count):
the suite is ~2000 CPU-seconds of work and its single longest test (the FALD profiling stage
chain) is ~205 s, so a perfect packing finishes in ~205 s. The actual run took 406 s — 200 s
of workers sitting idle. Startup is not the cause: collection + worker bring-up is ~5 s, and
the whole numpy/scipy/colour import stack is ~2.5 s per worker, paid once, in parallel.

2026-09-21 changed the shape of that: the FALD model's meter path no longer forms a whole frame
to average an aperture disc, so the stage chain fell to ~29 s and the longest test is now the
engine's constrained-RBF case at ~120 s, with total CPU down ~40 %. The run is closer to
CPU-bound than to critical-path-bound, which makes the ordering below matter LESS than it did
— but the failure mode it prevents (two heavy tests inside one 23-item chunk) is unchanged, and
it is nearly free, so it stays.

The cause is xdist's dispatch granularity. ``--dist load`` (the default) hands each worker a
CONSECUTIVE chunk of the collected list up front::

    node_chunksize = max(min(len(collection) // len(nodes) // 4, maxschedchunk), 2)

which is 23 items here — and it refills a drained worker with another consecutive block of up
to ~36. Collection order is alphabetical by file, so whether a 200 s test lands early or late,
and whether two of them land in the SAME chunk, is pure luck. Two heavy tests in one chunk run
back to back on one worker while the other fifteen finish and idle.

So this hook SPREADS the known-heavy tests evenly through the collected list — heaviest first,
then every ``stride`` items — so that no initial chunk and no refill block can contain two of
them, and the longest test is item 0 (it starts at t=0 on worker gw0).

The two obvious-looking alternatives were measured and are WORSE; do not "simplify" this into
either of them:

    heaviest-first, no spreading, --dist load        880 s  (worker gw0 got a 23-item chunk
                                                            that was ALL the heavy tests)
    heaviest-first, no spreading, --dist worksteal   377 s  (all heavy tests queue behind the
                                                            205 s test on gw0, and a worker can
                                                            only answer a steal request between
                                                            tests — so the other fifteen idle
                                                            until it finishes)

Keeping the table current is optional: an entry that no longer matches any test is ignored, and
a new slow test missing from it only loses the scheduling benefit, never correctness. Refresh
with ``python -m pytest -q --durations=0`` and copy the calls above ~10 s.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

from dlc.paths import RUNS_DIR, RUNS_DIR_ENV

# nodeid tail (file::function, without any ``[param]`` suffix) -> measured seconds.
# Parametrised entries are listed once by base name; every parameter inherits the cost.
# Only the ORDER of these numbers matters, never their absolute value.
#
# REFRESHED 2026-09-21 on a 4-core container (the 2026-09-20 numbers were a 16-worker box), after the
# FALD model's meter path stopped forming whole frames to average an aperture disc: the profiling
# stage chain went 196 s -> 29 s and is no longer the list's head, so the order below is genuinely
# different, not rescaled. Re-measure on the 16-worker box when convenient — a wrong order costs wall
# time, never correctness, and --dist worksteal absorbs it.
_HEAVY_SECONDS = {
    "test_engine_v2.py::test_constrained_rbf_caps_off_channel_lift_at_saturated_blue": 121,
    "test_fald_boost_gpu.py::test_emulator_two_round_boost_matches_correct_image_pa32ucxr_frame": 87,
    "test_fald_starfield_gpu.py::test_pa32ucxr_frame_star_lattice_with_outliers_matches_the_reference": 69,
    "test_fald_profile.py::test_the_fit_recovers_the_hidden_estimate": 51,
    "test_fald_fit_rules.py::test_synthetic_sdr_fit_recovers_drive_k_and_flags_an_unidentified_tmin": 35,
    "test_engine_v2.py::test_physical_cube_reduces_model_error_and_pins_neutral": 31,
    "test_optimize.py::test_physical_engine_is_opt_in_and_reports_info": 30,
    "test_fald_profile.py::test_stage_chain_sdr_to_export_and_verify": 29,
    "test_fald_glowfill.py::test_the_fill_fades_out_continuously_as_the_content_gets_brighter": 22,
    "test_engine_v2.py::test_build_cube_reduces_error_and_is_mostly_monotonic": 13,
    "test_fald_starfield.py::test_protection_is_mirror_symmetric_and_a_mid_drive_object_protects_partially": 12,
    "test_engine_v2.py::test_sdr_wide_gamut_maps_inward_and_is_consistent": 10,
    "test_hook_routing.py::test_readiness_stage_refuses_when_the_hook_paints_nothing": 10,
    "test_hook_routing.py::test_readiness_stage_swaps_a_crossed_twin_and_raises_the_anomaly": 10,
    "test_fald_starfield_gpu.py::test_a_star_stepping_away_from_a_window_in_8_px_steps": 9,
    "test_fald_starfield.py::test_a_star_leaving_solid_content_gains_weight_continuously": 6,
}


def _cost(nodeid: str) -> float:
    key = nodeid.replace("\\", "/").split("[")[0]
    for tail, secs in _HEAVY_SECONDS.items():
        if key.endswith(tail):
            return secs
    return 0.0


def pytest_collection_modifyitems(session, config, items):
    """Heaviest test first, then the rest of the heavy ones evenly spaced through the list."""
    heavy = [it for it in items if _cost(it.nodeid) > 0.0]
    if not heavy or len(heavy) == len(items):
        return
    heavy.sort(key=lambda it: -_cost(it.nodeid))          # stable: ties keep collection order
    rest = [it for it in items if _cost(it.nodeid) == 0.0]
    stride = max(1, len(items) // len(heavy))             # >= 2x xdist's 23-item initial chunk
    out, cut = [], 0
    for item in heavy:
        out.append(item)
        out.extend(rest[cut:cut + stride - 1])
        cut += stride - 1
    out.extend(rest[cut:])
    assert len(out) == len(items)
    items[:] = out


# ---------------------------------------------------------------------------------------------
# Runs-root sandbox (part 1 of the module docstring)
# ---------------------------------------------------------------------------------------------

# The process-wide default, set at IMPORT so collection, hooks, session/module fixtures and any
# daemon thread outliving the last test resolve a throwaway root too (each test then gets its
# own, below). It deliberately overrides a $DLC_RUNS_DIR the shell carried in: that one may
# point at real runs.
_PROCESS_RUNS_ROOT = tempfile.mkdtemp(prefix="dlc_pytest_runs_")
os.environ[RUNS_DIR_ENV] = _PROCESS_RUNS_ROOT
atexit.register(shutil.rmtree, _PROCESS_RUNS_ROOT, ignore_errors=True)

_REAL_RUNS_ROOT = os.path.normcase(os.path.abspath(RUNS_DIR))


class RealRunsRootWrite(RuntimeError):
    """The tripwire's block. Deliberately NOT an ``OSError``: on Windows ``tempfile.mkstemp``
    (the atomic writer's first step) retries ``PermissionError`` ~forever, and
    ``mkdir(exist_ok=True)`` / ``except OSError`` paths would absorb it."""


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# audit event -> positions of the args it creates, replaces or deletes. "open" is handled apart
# (only its write modes count). _winapi.CopyFile2 is how shutil.copy2/copytree write on Windows —
# it never raises "open".
_PATH_EVENTS = {"os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.truncate": (0,),
                "shutil.rmtree": (0,), "os.rename": (0, 1), "os.link": (1,), "os.symlink": (1,),
                "_winapi.CopyFile2": (1,), "shutil.copyfile": (1,), "shutil.copytree": (1,),
                "shutil.move": (0, 1), "shutil.unpack_archive": (1,)}
_violations: list[str] = []
_reported = 0


def _under_real_runs_root(path: object) -> bool:
    if isinstance(path, int):          # an fd: its path was checked when it was opened
        return False
    norm = os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(path))))
    if norm.startswith("\\\\?\\"):     # a Win32 extended-length spelling of the same path
        norm = norm[4:]
    return norm == _REAL_RUNS_ROOT or norm.startswith(_REAL_RUNS_ROOT + os.sep)


def _write_targets(event: str, args: tuple) -> tuple:
    if event == "open":
        path, mode, flags = args       # builtin open: mode str; os.open: mode None + flags
        writes = any(c in mode for c in "wax+") if isinstance(mode, str) else bool(flags & _WRITE_FLAGS)
        return (path,) if writes else ()
    return tuple(args[i] for i in _PATH_EVENTS[event])


def _real_runs_root_tripwire(event: str, args: tuple) -> None:
    if event != "open" and event not in _PATH_EVENTS:
        return
    try:
        hits = [t for t in _write_targets(event, args) if _under_real_runs_root(t)]
    except Exception:  # noqa: BLE001 - an odd third-party event shape must never break the call
        return
    for target in hits:
        where = os.environ.get("PYTEST_CURRENT_TEST", "outside any test")
        _violations.append(f"{event} {os.fsdecode(os.fspath(target))} [during {where}]")
        raise RealRunsRootWrite(
            f"a test tried to write the REAL runs root ({event} {target!r}). Resolve the runs "
            "root with dlc.paths.runs_dir() at call time - never import RUNS_DIR - so the "
            f"suite's ${RUNS_DIR_ENV} sandbox applies.")


sys.addaudithook(_real_runs_root_tripwire)    # armed from here (collection) to process exit


def _take_violations() -> list[str]:
    """The violations recorded since the last take — each one is reported exactly once."""
    global _reported
    new = _violations[_reported:]
    _reported = len(_violations)
    return new


def _fail_on_violations() -> None:
    new = _take_violations()
    if new:
        pytest.fail("wrote to the real runs root (blocked):\n  " + "\n  ".join(new), pytrace=False)


@pytest.fixture(scope="session", autouse=True)
def _report_late_violations():
    """Reports what the tripwire recorded after the last test's check (module/session fixture
    teardown); anything before a test is reported by that test."""
    yield
    _fail_on_violations()


@pytest.fixture(autouse=True)
def _sandboxed_runs_root(tmp_path_factory, monkeypatch):
    """A fresh runs root per test: new run folders and ``active.json`` land here, and one
    test's ``active.json`` can never steer another's ``latest_run()``."""
    monkeypatch.setenv(RUNS_DIR_ENV, str(tmp_path_factory.mktemp("runs_root")))
    yield
    _fail_on_violations()


class _Tripwire:
    Blocked = RealRunsRootWrite

    @staticmethod
    def take() -> list[str]:
        return _take_violations()


@pytest.fixture
def real_runs_root_tripwire() -> _Tripwire:
    """For the tripwire's own tests: ``Blocked`` is what it raises, ``take()`` consumes what it
    recorded (so the test that trips it on purpose is not failed for it)."""
    return _Tripwire()


_guard_before: tuple | None = None
_guard_failures: list[str] = []
_guard_notes: list[str] = []


def _real_runs_root_state() -> tuple[frozenset[str], object]:
    """The real root's folders + what ``active.json`` points at. Files are not compared (other
    sessions drop logs into ``runs/``), nor is the pointer's ``updated`` stamp (a paused real
    run rewrites it on every resume)."""
    try:
        folders = frozenset(p.name for p in RUNS_DIR.iterdir() if p.is_dir())
    except OSError:
        folders = frozenset()
    try:
        raw = json.loads((RUNS_DIR / "active.json").read_text(encoding="utf-8"))
        target = (raw.get("run"), raw.get("events"))
    except (OSError, ValueError, AttributeError):
        target = None
    return folders, target


def _is_temp_path(path: object, config) -> bool:
    """A pytest (or other throwaway) location — never where a real run lives."""
    if not isinstance(path, str) or not path:
        return False
    norm = os.path.normcase(os.path.abspath(path))
    roots = [tempfile.gettempdir(), config.option.basetemp]
    return any(norm.startswith(os.path.normcase(os.path.abspath(str(r))) + os.sep) for r in roots if r)


def pytest_sessionstart(session):
    global _guard_before
    if not hasattr(session.config, "workerinput"):   # the xdist controller, or a -n0 run
        _guard_before = _real_runs_root_state()
        _guard_failures.clear()
        _guard_notes.clear()


def pytest_sessionfinish(session, exitstatus):
    """FAILS only on a change that is pytest's by construction: ``active.json`` now aimed at a
    temp dir (the reported hazard — a live dashboard following it leaves the real run). Other
    changes are NOTED, not failed: in-process test writes are already blocked by the tripwire,
    so a new or vanished folder is almost always another session's real run starting or being
    pruned while the suite ran — failing on it would cry wolf during hardware work."""
    if _guard_before is None:
        return
    (folders0, target0), (folders1, target1) = _guard_before, _real_runs_root_state()
    if target1 != target0:
        line = f"active.json now points at {target1 and target1[0]} (was {target0 and target0[0]})"
        if target1 and any(_is_temp_path(p, session.config) for p in target1):
            _guard_failures.append(line)
        else:
            _guard_notes.append(line)
    if folders1 - folders0:
        _guard_notes.append(f"new folders: {sorted(folders1 - folders0)}")
    if folders0 - folders1:
        _guard_notes.append(f"folders no longer there (pytest deletes nothing): {sorted(folders0 - folders1)}")
    if _guard_failures and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if _guard_failures:
        terminalreporter.write_sep("=", f"the suite repointed the REAL {RUNS_DIR / 'active.json'}", red=True)
        for line in _guard_failures:
            terminalreporter.write_line(line)
        terminalreporter.write_line(f"A test escaped the ${RUNS_DIR_ENV} sandbox (a subprocess with a "
                                    "scrubbed env?). Re-point active.json at the real run.")
    if _guard_notes:
        terminalreporter.write_sep("=", f"the real runs root changed during the suite ({RUNS_DIR})", yellow=True)
        for line in _guard_notes:
            terminalreporter.write_line(line)
        terminalreporter.write_line("Not failed: most likely another session's real run. If nothing "
                                    f"else was running, a test escaped the ${RUNS_DIR_ENV} sandbox.")
