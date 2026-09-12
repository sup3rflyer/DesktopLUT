"""Controlled reproduction of the 2026-09-12 GUI-thread freeze: neutral entry/exit with the FALD layer off,
then on. Every pipe call is time-boxed; the trace file next to the exe records where the GUI thread went.
Usage: PYTHONPATH="src;." python fald_hang_repro.py [off|on|both]"""
import sys, time, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_fald_leak_probe as P
from dlc.controller import CalibrationController

TRACE = Path(r"H:\Projects\DesktopLUT\bin\Release\fald_trace.log")


def timed(label, fn, limit=25.0):
    out = {}
    def run():
        try: out["r"] = fn()
        except Exception as e: out["e"] = e
    t = threading.Thread(target=run, daemon=True); t0 = time.time(); t.start(); t.join(limit)
    dt = time.time() - t0
    if t.is_alive():
        print(f"  {label:<28} HUNG (> {limit:.0f} s)"); return False
    print(f"  {label:<28} {dt:5.1f} s  {'ERROR ' + str(out['e']) if 'e' in out else 'ok'}")
    return "e" not in out


def tail_trace(n=25):
    if TRACE.exists():
        lines = TRACE.read_text(errors="ignore").splitlines()
        print("--- fald_trace.log tail ---"); print("\n".join(lines[-n:]))


def main(argv):
    which = argv[1] if len(argv) > 1 else "both"
    ctrl = CalibrationController.connect()
    if TRACE.exists(): TRACE.write_text("")
    for state in (["off", "on"] if which == "both" else [which]):
        print(f"== layer {state}")
        if not timed(f"layers.set fald={state=='on'}", lambda: ctrl.call("layers.set", {"monitor": 0, "mode": "HDR", "fald": state == "on"})): tail_trace(); return 1
        time.sleep(2.0)
        if not timed("calibration.enter (native)", lambda: P.enter_native(ctrl), limit=60): tail_trace(); return 1
        time.sleep(2.0)
        if not timed("calibration.exit (restore)", lambda: ctrl.exit_calibration(restore_snapshot=True), limit=60): tail_trace(); return 1
        time.sleep(2.0)
    tail_trace(); print("no hang"); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
