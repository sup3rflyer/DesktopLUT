"""Packaging metadata smoke checks for advertised DLC entry points/assets."""

from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_advertised_console_scripts_are_registered() -> None:
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["dlc-dashboard"] == "dlc.dashboard.__main__:main"
    assert scripts["dlc-digest"] == "dlc.digest:main"
    assert scripts["dlc-dogegen-server"] == "dlc.dogegen_server:main"


def test_dashboard_assets_are_included_as_package_data() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert "assets/*" in package_data["dlc.dashboard"]


def test_engine_extra_includes_yaml_profile_loader_dependency() -> None:
    engine = _pyproject()["project"]["optional-dependencies"]["engine"]
    assert any(dep.lower().startswith("pyyaml") for dep in engine)


def test_test_extra_covers_the_baked_in_addopts() -> None:
    # The pytest addopts hardwire `-n auto`; a box that installs bare pytest gets an
    # argument-parsing error, not a verdict. The `test` extra must therefore carry
    # xdist (and the rest of the advertised suite tooling) so `pip install -e .[test]`
    # is sufficient to run the suite anywhere.
    project = _pyproject()
    assert "-n auto" in project["tool"]["pytest"]["ini_options"]["addopts"]
    test_extra = project["project"]["optional-dependencies"]["test"]
    names = {dep.split(">")[0].split("<")[0].split("=")[0].strip().lower() for dep in test_extra}
    assert {"pytest", "pytest-xdist", "pytest-cov"} <= names
