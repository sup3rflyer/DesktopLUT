"""Run-folder invariants (dlc.runs).

Regression guard for the first-3D-LUT-run bug: the run root must be ABSOLUTE even when the
caller passes a relative ``--run`` dir, because paths derived from it (the generated 3D-LUT
cube) are sent over the IPC pipe to DesktopLUT.exe — a separate process with its own working
directory, where a relative path resolves against the wrong cwd and "does not exist".
"""

from __future__ import annotations

from pathlib import Path

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
    ctx = create_run("SDR", display="x")   # no run_dir → RUNS_DIR / name
    assert ctx.root.is_absolute()


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
