"""Run-folder invariants (dlc.runs).

Regression guard for the first-3D-LUT-run bug: the run root must be ABSOLUTE even when the
caller passes a relative ``--run`` dir, because paths derived from it (the generated 3D-LUT
cube) are sent over the IPC pipe to DesktopLUT.exe — a separate process with its own working
directory, where a relative path resolves against the wrong cwd and "does not exist".
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from dlc.paths import RUNS_DIR, RUNS_DIR_ENV, runs_dir
from dlc.runs import create_run, make_run_name, open_run


def test_create_run_root_is_absolute_for_relative_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = create_run("SDR", display="x", run_dir=Path("relrun"))
    assert ctx.root.is_absolute()
    # the generated-cube path (the one sent over the pipe) is therefore absolute too
    assert (ctx.root / "generated" / "final_sdr.cube").is_absolute()


def test_open_run_root_is_absolute_for_relative_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create_run("SDR", display="x", run_dir=Path("relrun"))
    reopened = open_run(Path("relrun"))
    assert reopened.root.is_absolute()


def test_create_run_default_dir_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(RUNS_DIR_ENV, "relruns")   # even a RELATIVE runs-root override
    ctx = create_run("SDR", display="x")   # no run_dir → runs_dir() / name
    assert ctx.root.is_absolute()
    assert ctx.root.parent == (tmp_path / "relruns").resolve()


# --- the runs root is resolved at call time, and the suite never resolves the real one -------
# Every full-suite run used to leave a runs/<ts>_sdr_x folder in the owner's real runs/ and
# repoint runs/active.json (the live dashboard's pointer) at a pytest tmp run: the root was
# bound at import (`from .paths import RUNS_DIR`), so nothing could redirect it.

def test_runs_dir_honours_the_override_at_call_time_and_defaults_to_the_project_runs(tmp_path,
                                                                                      monkeypatch):
    monkeypatch.setenv(RUNS_DIR_ENV, str(tmp_path / "elsewhere"))
    assert runs_dir() == (tmp_path / "elsewhere").resolve()
    monkeypatch.delenv(RUNS_DIR_ENV)
    assert runs_dir() == RUNS_DIR and RUNS_DIR.is_absolute()


def test_the_suite_sandboxes_the_runs_root():
    # conftest points $DLC_RUNS_DIR at a tmp dir for every test. If this fails, every test that
    # creates a run or publishes active.json is aimed at the owner's real runs/.
    assert os.environ.get(RUNS_DIR_ENV)
    assert runs_dir() != RUNS_DIR
    assert RUNS_DIR not in runs_dir().parents


def test_a_write_into_the_real_runs_root_is_blocked_and_reported(tmp_path, real_runs_root_tripwire):
    # The conftest tripwire must hold even for code that bypasses runs_dir(). The probe's
    # parent never exists, so even a BROKEN tripwire writes nothing (FileNotFoundError instead).
    probe = RUNS_DIR / "__pytest_tripwire_probe__" / "sub"
    with pytest.raises(real_runs_root_tripwire.Blocked):
        (probe / "active.json").write_text("{}", encoding="utf-8")
    with pytest.raises(real_runs_root_tripwire.Blocked):
        probe.mkdir()
    src = tmp_path / "staged.json"
    src.write_text("{}", encoding="utf-8")
    with pytest.raises(real_runs_root_tripwire.Blocked):
        os.replace(src, probe / "active.json")      # the atomic writer's last step
    with pytest.raises(real_runs_root_tripwire.Blocked):
        shutil.copy2(src, probe / "active.json")    # Windows: _winapi.CopyFile2, never "open"
    # production code that swallows the error (the active.json publisher does) is still caught
    try:
        (probe / "active.json").write_text("{}", encoding="utf-8")
    except Exception:  # noqa: BLE001 - deliberately mimicking the swallowing publisher
        pass
    assert len(real_runs_root_tripwire.take()) == 5
    assert not probe.parent.exists() and src.exists()


def test_generated_run_names_are_unique_and_slugged():
    a = make_run_name("SDR", "Asus ProArt PA32UCXR / Lab")
    b = make_run_name("SDR", "Asus ProArt PA32UCXR / Lab")
    assert a != b
    assert a.endswith("_sdr_asus_proart_pa32ucxr_lab")
    assert "/" not in a and " " not in a


def test_run_manifest_save_uses_atomic_writer(tmp_path):
    ctx = create_run("SDR", display="x", run_dir=tmp_path / "run")
    assert not list(ctx.root.glob(".manifest.json.*.tmp"))
    assert ctx.manifest_path.exists()
    reopened = open_run(ctx.root)
    assert reopened.manifest.name == "run"


def test_create_run_adopts_a_half_created_run_dir(tmp_path):
    # fable Phase 7a: a crash between the root mkdir and the first manifest save used to
    # brick the dir — open_run refuses it (no manifest.json) and create_run's
    # mkdir(exist_ok=False) raised. A manifest-less dir is adoptable by construction
    # (every caller checks for manifest.json before choosing create_run).
    from dlc.runs import create_run

    half = tmp_path / "half_created"
    half.mkdir()
    (half / "measurements").mkdir()      # some subdirs may exist too
    ctx = create_run("SDR", display="adopt", run_dir=half)
    assert ctx.manifest_path.exists()
    assert (half / "generated").is_dir() and (half / "reports").is_dir()


def test_create_run_refuses_a_populated_foreign_directory(tmp_path):
    # fable Phase 7a finding F7a-A-runs: exist_ok=True must not scatter run files into an
    # arbitrary populated folder (e.g. --run ~/Documents). A dir with entries outside the
    # run scaffolding is refused; a half-created run (only our subdirs) is still adopted.
    from dlc.runs import create_run

    foreign = tmp_path / "my_documents"
    foreign.mkdir()
    (foreign / "resume.pdf").write_text("mine", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_run("SDR", display="x", run_dir=foreign)
    assert (foreign / "resume.pdf").exists() and not (foreign / "manifest.json").exists()

    # a half-created run dir (only scaffolding) is still adoptable
    half = tmp_path / "half"
    half.mkdir()
    (half / "measurements").mkdir()
    ctx = create_run("SDR", display="x", run_dir=half)
    assert ctx.manifest_path.exists()
