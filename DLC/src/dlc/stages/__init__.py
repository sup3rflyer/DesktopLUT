"""DLC stage tools — the assistant's instruments.

Each module in this package is a thin command-line tool the *arbitrating
assistant* invokes (``python -m dlc.stages.<tool> --run <dir> --json``). A tool
does deterministic work — drive the meter, run Argyll, build a cube, install a
profile via the DesktopLUT controller — and then emits exactly one
:class:`dlc.stage.StageResult` JSON object describing what it saw at the start
(``preconditions``) and the end (``metrics``/``deltas``/``anomalies``) of the
stage. A tool never decides "good enough"; at most it surfaces an advisory
``advice.default_policy_verdict`` the assistant is free to override.

See ``docs/v1-rebuild-plan.md`` §1, §6 for the contract these tools implement.
"""

from __future__ import annotations

__all__ = [
    "preflight",
    "probe_match",
    "enter_neutral",
    "measure",
    "build_mhc",
    "install_mhc",
    "refine_grayscale",
    "build_3dlut",
    "check_cube",
    "install_3dlut",
    "score",
    "state",
    "report",
    "simulate",
]
