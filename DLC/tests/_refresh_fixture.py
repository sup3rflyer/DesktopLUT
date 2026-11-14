"""Today's real REFRESH TRIM data (PA32UCXR, HDR, as viewed, 2026-11-14) as verify rows, for the thermal / light-class
tests and the replay. The fixture ``fixtures/refresh_verify_pa32_2026-11-14.json`` freezes (grey rows only):

* ``record``: the 2026-10-23 refresh-trim run the trim is built from (its DIP section and its rows; it stores no
  thermometer: records before 2026-11-14 do not);
* ``runs``: the two morning verify runs (08:07 warming, 08:46 hot) - their trim_rows, trim read-backs, anomalies;
* ``warm``: seven A-B-A cycles (60 / 47.952 trim on / 60 Hz) cooling down after 12 min of a 500-nit full field;
* ``ladder``: codes 515-525 at 60 Hz, 47.952 trim on, 47.952 trim OFF, 60 Hz, on a cool panel.

The A-B-A experiments were not verify runs: their rows are rebuilt here with each table's time (legs 40 s apart in a
cycle). Source: DLC/results/pa32_knowledge_2026-11-14/refresh_trim (README, warm.log, ladder.log)."""
from __future__ import annotations

import json
import math
from pathlib import Path

from dlc import refresh_trim as rt

FIXTURE = Path(__file__).parent / "fixtures" / "refresh_verify_pa32_2026-11-14.json"
OK_CHECKS = [{"rate": "60", "ok": True, "rule": "reference"}, {"rate": "47.952", "ok": True, "rule": "stamp"}]
# What dlc.refresh_verify.thermal_knowledge returns for the display's F-0006 (its signature: thermometer axis x, a
# cooler panel reads higher x and leaves a positive leftover of up to about 0.9 %); built from a real knowledge store
# in test_refresh_thermal.py.
KNOWLEDGE_F0006 = {"unit": "AUS322A-335544320", "entry": "F-0006",
                   "title": "The HDR refresh-rate response is larger on a cool panel than on a hot one",
                   "axis": "x", "cooler_sign": 1, "cooler_from": "observation O1: x_100nit_cool 0.3127 vs x_100nit_hot 0.3067",
                   "leftover_sign_when_cooler": "+", "leftover_max_pct": 0.9, "thermometer_max_abs": 0.006}


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def record_th(fx: dict) -> tuple[dict, str]:
    th = rt.reference_thermometer(fx["record"]["rows"], "60")
    th["run_id"] = fx["record"]["run_id"]
    return th, "derived from the record run's trim_rows.jsonl (frozen)"


def _row(leg: int, rate: str, t: float, code: str, Y: float, xy) -> dict:
    x, y = xy
    return {"leg": leg, "pass": "A", "rate": rate, "rate_hz": rt._rate_hz_nominal(rate), "patch": f"g{code}",
            "kind": "grey", "code": [int(code)] * 3, "nominal_nits": None, "t_s": t, "y": Y,
            "xyz": [x / y * Y, Y, (1 - x - y) / y * Y], "sd_y": None, "n_kept": 3, "settled": True,
            "implausible": None, "source": "mode"}


def _secs(hms: str) -> float:
    h, m, s = (int(v) for v in hms.split(":"))
    return 3600.0 * h + 60.0 * m + s


def warm_cycle_rows(fx: dict, k: int, *, context: bool = True) -> list[dict]:
    """Cycle ``k`` as a verify: its 60 / 47.952 / 60 Hz legs (40 s apart); with ``context`` the other cycles' 60-Hz
    reads too (the session's reference legs: the chromaticity repeatability needs more than one bracket)."""
    t0 = _secs(fx["warm"][0]["t"])
    out = []
    for c in fx["warm"]:
        base = _secs(c["t"]) - t0
        legs = (("a60", "60", 0.0), ("b48", "47.952", 40.0), ("c60", "60", 80.0))
        for i, (key, rate, dt) in enumerate(legs):
            if c["cycle"] != k and (rate != "60" or not context):
                continue
            for code, (Y, xy) in c[key].items():
                out.append(_row(3 * c["cycle"] + i, rate, base + dt, code, Y, xy))
    return out


def ladder_rows(fx: dict, rate_leg: str = "b48on") -> list[dict]:
    """The one-code ladder as a verify: 60 Hz, the 47.952 Hz table (``b48on`` = trim on, ``c48off`` = the trim
    NOT applied), 60 Hz, each table at its own time."""
    lad = fx["ladder"]
    t0 = _secs(lad["legs"]["a60"]["t"])
    out = []
    for i, key in enumerate(("a60", rate_leg, "d60")):
        leg = lad["legs"][key]
        for code, (Y, xy) in lad["tables"][key].items():
            out.append(_row(i, leg["rate"], _secs(leg["t"]) - t0, code, Y, xy))
    return out


def with_leftover(rows: list[dict], leftover: float, reference: str = "60") -> list[dict]:
    """The rows with every rate read's light set to ``1 + leftover`` times its time-bracketed reference (the trim
    not applied, as a synthetic): the chromaticity as measured."""
    br = {(r["patch"], r["t_s"]): r for r in rt.bracket_ratios([r for r in rows if not r.get("implausible")], reference)
          if r.get("ratio")}
    out = []
    for r in rows:
        r2 = dict(r)
        b = br.get((r["patch"], r["t_s"]))
        if r.get("rate") != reference and b is not None:
            k = (1.0 + leftover) / b["ratio"]
            r2["y"] = r["y"] * k
            r2["xyz"] = [v * k for v in r["xyz"]]
        out.append(r2)
    return out


def finite(v) -> bool:
    return v is not None and math.isfinite(v)
