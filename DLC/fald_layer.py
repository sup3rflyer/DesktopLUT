"""Drive the DesktopLUT FALD correction layer over the calibration pipe (HDR, monitor 0 by default).

  python fald_layer.py status                    layers + fald params path for monitor 0 HDR
  python fald_layer.py params <panel.bin>        set the per-panel parameter file (dlc.fald.export output)
  python fald_layer.py on | off                  toggle the layer
  python fald_layer.py debug <0..4>              0 correct, 1 show gain-1 (grey 0.5 = no change), 2 B_true, 3 B_est,
                                                 4 identity passthrough (the A/B baseline — not OFF: the awake overlay
                                                 itself dips 0.5-2.4 % vs the sleeping one)
  python fald_layer.py ped <0|1>                 pedestal colour: 0 white (default, pre-2026-09-13), 1 per-channel
                                                 (the FLD2 panel file's measured leak colour; the GUI checkbox)
  python fald_layer.py dump <dir>                next frame dumps drive/B_true/B_est/frame (input) + fald_out (output)
                                                 to <dir>; with debug 4 fald_out must equal fald_frame bit for bit
  python fald_layer.py ab <seconds> [n]          alternate off/on every <seconds>, n cycles (default 6)
Options: --monitor N (default 0). Needs DesktopLUT running with the calibration pipe armed, in DD
(overlay) mode with HDR on for the monitor — the layer does not run in DWM-hook mode.
Static desktop (paused video / test pattern): DesktopLUT builds from 2026-09-13 re-process the last
frame after params / debug / dump / on / off, and `params` with an UNCHANGED path (or a file re-exported
in place) rebuilds the GPU tables. Older builds show nothing until the next desktop frame — frame-step.
"""
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from dlc.controller import CalibrationController


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "params", "on", "off", "debug", "ped", "dump", "ab"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("n", nargs="?", type=int, default=6)
    ap.add_argument("--monitor", type=int, default=0)
    a = ap.parse_args(argv)
    c = CalibrationController.connect()
    mon, mode = a.monitor, "HDR"
    if a.cmd == "status":
        st = c.state()
        key = f"{mon}:{mode}"
        print(json.dumps({"layers": st.get("layers", {}).get(key), "hook": st.get("hook")}, indent=1))
    elif a.cmd == "params":
        path = str(Path(a.arg).resolve())
        print(json.dumps(c.call("runtime.set_fald_params", {"monitor": mon, "mode": mode, "params_path": path}), indent=1))
    elif a.cmd in ("on", "off"):
        print(json.dumps(c.call("layers.set", {"monitor": mon, "mode": mode, "fald": a.cmd == "on"}), indent=1))
    elif a.cmd == "debug":
        print(json.dumps(c.call("runtime.fald_debug", {"monitor": mon, "mode": mode, "debug_mode": int(a.arg)}), indent=1))
    elif a.cmd == "ped":
        print(json.dumps(c.call("runtime.fald_debug", {"monitor": mon, "mode": mode, "ped_mode": int(a.arg)}), indent=1))
    elif a.cmd == "dump":
        d = Path(a.arg).resolve(); d.mkdir(parents=True, exist_ok=True)
        print(json.dumps(c.call("runtime.fald_dump", {"monitor": mon, "mode": mode, "dir": str(d)}), indent=1))
    elif a.cmd == "ab":
        period = float(a.arg or 3.0)
        for i in range(a.n):
            for on in (False, True):
                c.call("layers.set", {"monitor": mon, "mode": mode, "fald": on})
                print(f"cycle {i + 1}/{a.n}: fald {'ON ' if on else 'OFF'}", flush=True)
                time.sleep(period)
        c.call("layers.set", {"monitor": mon, "mode": mode, "fald": False})
        print("left OFF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
