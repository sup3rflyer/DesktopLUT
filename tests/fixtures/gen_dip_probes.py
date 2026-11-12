"""Generates tests/fixtures/dip_store_probes.json and charts_probes.json from DLC's own code (the probe simulator), so
the C++ readers are tested on exactly what DLC writes, not on a hand-made copy of its format.

    cd DLC && python ../tests/fixtures/gen_dip_probes.py            # regenerate both files
    cd DLC && python ../tests/fixtures/gen_dip_probes.py --check    # do the committed files still load as DLC loads
                                                                    # them, and does DLC still write the same keys?

What it runs (in-process, nothing shown, no meter, no mode switched; a temp folder): DLC's own test harnesses for
``--flow peak-window`` on an OLED-like panel (steep, valid), a mini-LED-like panel (flat, valid) and a drifting panel
(the passes disagree: invalid), and ``--flow refresh-response`` with the VRR rate source (a smooth response, cross-
checked: VRR rates promoted to fixed-mode trims). The DIP sections go into one store under synthetic display names;
the live chart payloads (DashboardState.charts()["probes"]) go into a charts.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DLC = HERE.parent.parent / "DLC"
sys.path.insert(0, str(DLC / "src"))
sys.path.insert(0, str(DLC / "tests"))

STORE = HERE / "dip_store_probes.json"
CHARTS = HERE / "charts_probes.json"


def _peak_runs(tmp: Path) -> tuple[dict, dict]:
    import test_peak_window as t
    from dlc.adjudication import Decision, MappingAdjudicator
    from dlc.dashboard.state import DashboardState
    from dlc.events import read_events
    from dlc.probe_sim import ThermalSag, VirtualClock, mini_led_like, oled_like

    sections, live = {}, {}
    runs = {
        "Probe OLED 42": (oled_like(clock=VirtualClock()), "WOLED", MappingAdjudicator(dict(t._OLED_YES)), None),
        "Probe MiniLED 32": (mini_led_like(clock=VirtualClock(), thermal=ThermalSag(sag=0.01, tau_s=3000.0)),
                             "mini-LED IPS", None, None),
        "Probe Drift 27": (mini_led_like(clock=VirtualClock(), thermal=None, drift_per_hour=-0.6), "mini-LED IPS",
                           MappingAdjudicator({"peak-window:consent": Decision("approve"),
                                               "peak-window:record": Decision("record")}), None),
    }
    for name, (panel, tech, adj, spec) in runs.items():
        cal = t._calib(tmp, name.replace(" ", "_"), panel, tech=tech, adjudicator=adj, spec=spec)
        res = cal.run("peak-window")
        assert res.status == "completed", (name, res.digest)
        sections[name] = t._recorded(cal).as_dict()
        st = DashboardState()
        for ev in read_events(cal.ctx.events_path):
            st.ingest(ev)
        live[name] = st.charts()["probes"]["peak_by_window"]
        if name == "Probe OLED 42":
            from dlc.dashboard.publish import LIVE_SCHEMA
            live["_charts"] = {**st.charts(), "live_schema": LIVE_SCHEMA, "seq": 1}   # as the publisher writes it
    return sections, live


def _refresh_run(tmp: Path) -> tuple[dict, dict]:
    import test_vrr as t
    from dlc import refresh_trim as rt
    from dlc import vrr
    from dlc.dashboard.state import DashboardState
    from dlc.events import read_events

    real = vrr.stack_view

    def view(cal):
        v = real(cal)
        v["hook"] = {"active": False}
        v["overlay"] = {"awake": False}
        return v
    vrr.stack_view = view
    try:
        cal, api, fake, panel = t._calib(tmp, "vrr")
        res = cal.run("refresh-response")
        assert res.status == "completed", res.digest
        resp, _, _ = rt.response_for_run_dir(cal.ctx.root)
        resp.identity = {"hardware_id": "PRB0042", "device_path": None, "edid_id": None}   # synthetic
        st = DashboardState()
        for ev in read_events(cal.ctx.events_path):
            st.ingest(ev)
        return resp.as_dict(), st.charts()["probes"]["refresh_response"]
    finally:
        vrr.stack_view = real


def build() -> tuple[dict, dict]:
    from dlc.dip import DipStore, DisplayInstrumentProfile, PeakByWindow, RefreshResponse
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        sections, live = _peak_runs(tmp)
        refresh, refresh_live = _refresh_run(tmp)
        store = DipStore(tmp / "dip_store.json")
        for name, s in sections.items():
            store.record(DisplayInstrumentProfile(display=name, mode="HDR", made="2026-11-01",
                                                  native_white_nits=1000.0,
                                                  peak_by_window=PeakByWindow.from_dict(s)), save=False)
        store.record(DisplayInstrumentProfile(display="Probe VRR 27", mode="HDR", made="2026-11-01",
                                              native_white_nits=1000.0,
                                              refresh_response=RefreshResponse.from_dict(refresh)), save=False)
        store.save()
        dip = json.loads((tmp / "dip_store.json").read_text(encoding="utf-8"))
    # The OLED run's charts.json as the publisher writes it, the refresh probe's payload beside its own, and every
    # panel's peak-by-window payload under a key DLC does not write (for the UI shots).
    charts = live.pop("_charts")
    charts["probes"] = {"peak_by_window": live["Probe OLED 42"], "refresh_response": refresh_live}
    charts["probes_by_panel"] = live
    return dip, charts


def check() -> int:
    from dlc.dip import DipStore, PeakByWindow, RefreshResponse
    problems = []
    store = DipStore.load(STORE)
    if store.corrupt or store.dropped:
        problems.append(f"store: corrupt={store.corrupt} dropped={store.dropped}")
    raw = json.loads(STORE.read_text(encoding="utf-8"))
    for key, rec in raw["displays"].items():
        r = store.get(key)
        if r is None:
            problems.append(f"{key}: not loaded")
            continue
        if rec.get("peak_by_window"):
            if r.peak_by_window is None:
                problems.append(f"{key}: peak_by_window dropped ({r.notes})")
            elif PeakByWindow.from_dict(rec["peak_by_window"]).as_dict() != rec["peak_by_window"]:
                problems.append(f"{key}: peak_by_window no longer round-trips (DLC changed the section: regenerate)")
        if rec.get("refresh_response"):
            if r.refresh_response is None:
                problems.append(f"{key}: refresh_response dropped ({r.notes})")
            elif RefreshResponse.from_dict(rec["refresh_response"]).as_dict() != rec["refresh_response"]:
                problems.append(f"{key}: refresh_response no longer round-trips (regenerate)")
    # DLC still writes the same keys (new or removed keys = regenerate and review the C++ readers).
    fresh_dip, fresh_charts = build()
    old = json.loads(CHARTS.read_text(encoding="utf-8"))

    def keys(x, pre=""):
        out = set()
        if isinstance(x, dict):
            for k, v in x.items():
                out |= {pre + k} | keys(v, pre + k + ".")
        elif isinstance(x, list) and x and isinstance(x[0], (dict, list)):
            out |= keys(x[0], pre + "[]")
        return out
    for what, a, b in (("dip", raw, fresh_dip), ("charts", old, fresh_charts)):
        if keys(a) != keys(b):
            problems.append(f"{what}: keys differ: +{sorted(keys(b) - keys(a))} -{sorted(keys(a) - keys(b))}")
    for p in problems:
        print("DRIFT:", p)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check()
    dip, charts = build()
    STORE.write_text(json.dumps(dip, indent=1) + "\n", encoding="utf-8")
    CHARTS.write_text(json.dumps(charts, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {STORE.name} ({len(dip['displays'])} records), {CHARTS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
