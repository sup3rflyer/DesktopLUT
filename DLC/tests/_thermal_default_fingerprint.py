"""The DEFAULT-path fingerprint for ``--thermal-state`` (dlc.viewing_thermal): what a run without the flag
commands and records, in two sim scenarios — a measure-loop preheat soak and a verify-only flow.

``tests/data/thermal_default_path_main.json`` holds this fingerprint RECORDED FROM ``main`` before the
viewing-thermal branch (commit fe890a9); ``test_viewing_thermal.test_default_path_matches_the_recorded_main_
fingerprint`` asserts the branch's default reproduces it exactly (same reads in the same order, same digests
except the new ``thermal_state`` key). This module imports only APIs that exist on that commit, so the
golden can be regenerated against any source tree::

    git archive fe890a9 DLC/src DLC/tests | tar -x -C <dir>
    PYTHONPATH=<dir>/DLC/src;<dir>/DLC/tests python DLC/tests/_thermal_default_fingerprint.py > golden.json

(regenerate only when a deliberate change to the default measure/verify path lands on main).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# Digest keys whose values are wall-clock / host / run-dir dependent (never a behaviour difference).
_VOLATILE = {"started", "ended", "t", "at", "elapsed_s", "duration_s", "elapsed_min", "seconds", "wall_s",
             "ti3", "ndjson", "ti3_path", "ndjson_path", "path", "run_dir", "results_dir", "report_path",
             "source", "created", "updated", "timestamp", "minutes", "eta_s", "rate"}


def _scrub(obj: Any, root: str) -> Any:
    if isinstance(obj, dict):
        return {k: _scrub(v, root) for k, v in sorted(obj.items()) if k not in _VOLATILE}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, root) for v in obj]
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, str) and root and root in obj:
        return obj.replace(root, "<root>")
    return obj


def _recorder(inner):
    reads: list[list[Any]] = []

    def measure(patch, _inner=inner):
        reads.append([patch.role, [int(c) for c in patch.rgb]])
        return _inner(patch)
    return measure, reads


def measure_loop_scenario() -> dict[str, Any]:
    """A forced (preheat=always) soak on a load-thermal synthetic panel + a small balanced set."""
    from dlc.engine.patches import Transfer
    from dlc.measure_loop import MeasureLoopConfig, SyntheticPanel, run_measure_loop

    pq = Transfer.pq(10)

    def grey(n: float) -> tuple[int, int, int]:
        cv = pq.nits_to_cv(n)
        return (cv, cv, cv)

    block = [grey(n) for n in (1.0, 3.0, 9.0, 30.0, 110.0, 300.0)]
    patches = []
    for i in range(6):
        patches += list(reversed(block)) if i % 2 == 0 else block
    panel = SyntheticPanel(transfer=pq, load_thermal=True, start_temp=0.1)
    measure, reads = _recorder(panel)
    res = run_measure_loop(patches=patches, transfer=pq, measure=measure,
                           config=MeasureLoopConfig(preheat="always"))
    digest = {k: v for k, v in res.digest.items() if k != "thermal_state"}
    return {"reads": reads, "digest": _scrub(digest, ""),
            "needs_adjudication": res.needs_adjudication, "question": res.question}


def verify_only_scenario(root: Path) -> dict[str, Any]:
    """The verify-only HDR sim flow over the synthetic content file, no --thermal-state."""
    from dlc.controller import CalibrationController
    from dlc.stages.simulate import write_synthetic_patches_file
    from test_verify_only_flow import _cube, _hdr_panel, _make, _seed_stack

    pf = write_synthetic_patches_file(root / "patches_hdr.json", mode="HDR", bit_depth=10)
    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, mode="HDR", cube=_cube(root / "fp.cube"))
    measure, reads = _recorder(_hdr_panel())
    calib = _make(root, "fp", mode="HDR", controller=ctrl, bit_depth=10, panel=measure, verify_patches_file=pf)
    result = calib.run("verify-only")
    stages = calib.calib["stages"]
    run_root = str(root)
    out: dict[str, Any] = {"status": result.status, "reads": reads,
                           "stages": sorted(stages),
                           "decisions": {k: v.get("choice") for k, v in sorted(calib.calib["decisions"].items())},
                           "calib_keys": sorted(calib.calib)}
    for key in ("measure:verify", "verify"):
        digest = {k: v for k, v in (stages[key].get("digest") or {}).items() if k != "thermal_state"}
        out[key] = _scrub(digest, run_root)
    return out


def fingerprint(root: Path) -> dict[str, Any]:
    return {"measure_loop": measure_loop_scenario(), "verify_only": verify_only_scenario(root)}


if __name__ == "__main__":  # pragma: no cover - golden regeneration
    import os

    os.environ.setdefault("DLC_RUNS_DIR", tempfile.mkdtemp(prefix="dlc_fp_runs_"))
    with tempfile.TemporaryDirectory() as d:
        json.dump(fingerprint(Path(d)), sys.stdout, indent=None, sort_keys=True)
