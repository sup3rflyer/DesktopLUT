"""The DEFAULT-path fingerprint of the MHC closed-loop refine for ``--thermal-state`` (the viewing-refine
policy): what a run WITHOUT the flag commands and records through the HDR and SDR ``mhc-only`` sim flows —
the refine stages (``refine-mhc-cube`` / ``refine-mhc-grayscale``) and the measure/verify stages around them.

``tests/data/refine_default_path_main.json`` holds this fingerprint RECORDED FROM ``main`` before the
viewing-refine branch (commit e0a559e); ``test_viewing_refine.test_default_refine_path_matches_main`` asserts
the branch's default reproduces it exactly (same reads in the same order, same digests, same decisions and
run-record keys). This module imports only APIs that exist on that commit, so the golden can be regenerated
against any source tree::

    git archive e0a559e DLC/src DLC/tests | tar -x -C <dir>
    PYTHONPATH=<dir>/DLC/src;<dir>/DLC/tests python <dir>/DLC/tests/_refine_default_fingerprint.py > golden.json

(with THIS file copied into <dir>/DLC/tests; regenerate only when a deliberate change to the default refine
path lands on main).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from _thermal_default_fingerprint import _recorder, _scrub

_STAGES = ("measure:raw", "refine-mhc-cube", "refine-mhc-grayscale", "measure:verify", "verify")


def _mhc_only(root: Path, name: str, mode: str) -> dict[str, Any]:
    from test_calibrate import _make, _perfect_hdr_panel, _perfect_panel

    panel = _perfect_hdr_panel() if mode == "HDR" else _perfect_panel()
    measure, reads = _recorder(panel)
    calib = _make(root, name, mode=mode, panel=measure, **({"bit_depth": 10} if mode == "HDR" else {}))
    result = calib.run("mhc-only")
    stages = calib.calib["stages"]
    out: dict[str, Any] = {"status": result.status, "reads": reads, "stages": sorted(stages),
                           "decisions": {k: v.get("choice") for k, v in sorted(calib.calib["decisions"].items())},
                           "calib_keys": sorted(calib.calib)}
    for key in _STAGES:
        if key in stages:
            out[key] = _scrub(stages[key].get("digest") or {}, str(root))
    return out


def fingerprint(root: Path) -> dict[str, Any]:
    return {"hdr_mhc_only": _mhc_only(root, "fp_hdr", "HDR"), "sdr_mhc_only": _mhc_only(root, "fp_sdr", "SDR")}


if __name__ == "__main__":  # pragma: no cover - golden regeneration
    import os

    os.environ.setdefault("DLC_RUNS_DIR", tempfile.mkdtemp(prefix="dlc_fp_runs_"))
    with tempfile.TemporaryDirectory() as d:
        json.dump(fingerprint(Path(d)), sys.stdout, indent=None, sort_keys=True)
