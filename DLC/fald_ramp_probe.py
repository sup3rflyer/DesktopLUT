"""Gradient (lattice) hardware test for the FALD layer: image frames via mpv, meter reads OFF vs ON.

For every frame in results/fald_native_2026-09-11/sim/ramp/frames.json (ramps shifted through one
cell period) the meter at the registered spot reads the panel with the layer OFF and ON. Expected
value = the ramp's nits at the meter (frames.json). Reports per frame: measured/expected − 1 (OFF, ON)
next to the model's prediction — the lattice amplitude the owner sees on gradients, and whether the
layer removes it. Frames are shown by mpv (HDR passthrough, fullscreen on the ProArt) driven over its
JSON IPC pipe; DesktopLUT stays in DD/overlay mode with the layer toggled over the calibration pipe.

State: refuses unless the stack audit passes (FALD_NATIVE=1 enters the neutral state + identity MHC
and restores on exit; FALD_ALLOW_STACK=1 measures through the owner's stack knowingly — relative
OFF/ON differences are still valid then, absolutes are not).
Env: FALD_METER=x,y (default 1988,1120), FALD_OUT (results dir), FALD_RAMP_KINDS=h,hsteep,v,
FALD_MPV_SCREEN (default 0), FALD_SETTLE (s after a frame change, default 1.5).
Usage: PYTHONPATH="src;." FALD_NATIVE=1 python fald_ramp_probe.py
"""
import json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_fald_leak_probe as P
from dlc.controller import CalibrationController
from dlc.measure_loop import MeasurePatch, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port
from dlc.argyll import Argyll, SpotreadRequest
import dlc.calibration_profile as cp
from dlc.calibrate import active_correction, correction_store_path
from dlc.correction_store import CorrectionStore

SIM = Path(r"H:\Projects\DesktopLUT\DLC\results\fald_native_2026-09-11\sim")
OUT = Path(os.environ.get("FALD_OUT", str(SIM / "ramp")))
PIPE = r"\\.\pipe\mpv-fald"
SETTLE = float(os.environ.get("FALD_SETTLE", "1.5"))
MON = 0


class MpvPresenter:
    """Presenter for make_persistent_spotread_meter: shows ``pending`` (a PNG path) in mpv."""

    def __init__(self, screen: int):
        self.pending = None
        self.proc = subprocess.Popen(["mpv", "--vo=gpu-next", "--target-colorspace-hint=yes", "--fs", f"--screen={screen}",
                                      "--image-display-duration=inf", "--keep-open=yes", "--idle=yes", "--osc=no", "--osd-level=0",
                                      "--no-terminal", "--cursor-autohide=always", f"--input-ipc-server={PIPE}",
                                      "--vf=format=gamma=pq:primaries=bt.2020"])
        self.pipe = None
        for _ in range(50):
            try:
                self.pipe = open(PIPE, "r+b", buffering=0); break
            except OSError:
                time.sleep(0.2)
        if self.pipe is None:
            raise RuntimeError("mpv IPC pipe did not appear")

    def cmd(self, *args):
        self.pipe.write((json.dumps({"command": list(args)}) + "\n").encode())
        try:
            self.pipe.readline()
        except OSError:
            pass

    def show(self, patch: MeasurePatch) -> None:
        self.cmd("loadfile", str(self.pending), "replace")
        time.sleep(SETTLE + patch.settle_bump_s)

    def close(self):
        try: self.cmd("quit")
        except Exception: pass
        try: self.proc.wait(timeout=5)
        except Exception: self.proc.kill()


def main():
    meter_xy = tuple(int(v) for v in os.environ.get("FALD_METER", "1988,1120").split(","))
    kinds = os.environ.get("FALD_RAMP_KINDS", "h,hsteep,v").split(",")
    spec = json.loads((SIM / "ramp" / "frames.json").read_text())
    if tuple(spec["meter"]) != meter_xy:
        P.log(f"[ramp] WARNING frames were generated for meter {spec['meter']}, running with {meter_xy}")
    frames = [f for f in spec["frames"] if f["kind"] in kinds]
    ctrl = CalibrationController.connect()
    native = os.environ.get("FALD_NATIVE") == "1"
    if native:
        P.enter_native(ctrl)
    layers = P.audit_or_refuse(ctrl, allow_stack=os.environ.get("FALD_ALLOW_STACK") == "1")
    hook = P.stack_layers(ctrl)[2]
    if hook.get("active"):
        raise SystemExit("[ramp] DesktopLUT is in DWM-hook mode; the layer runs only in the overlay path")

    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(MON).name)
    P.log(f"[setup] spotread port={port} ccmx={ccmx}")
    presenter = MpvPresenter(int(os.environ.get("FALD_MPV_SCREEN", "0")))
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)

    def set_fald(on: bool):
        ctrl.call("layers.set", {"monitor": MON, "mode": "HDR", "fald": on})
        time.sleep(0.5)

    def read(label, png):
        presenter.pending = png
        patch = MeasurePatch(label=label, rgb=(512, 512, 512), signal=(0.5, 0.5, 0.5), role="measurement", bit_depth=10, seq=0)
        rd = measure(patch)
        return float(rd.xyz[1]) if rd.ok and rd.xyz else None

    results = []
    try:
        set_fald(False)
        P.log(f"[ramp] {len(frames)} frames × OFF/ON at meter {meter_xy}; native={native}")
        for f in frames:
            png = SIM / "ramp" / f"{f['name']}.png"
            y_off = read(f["name"] + " OFF", png)
            set_fald(True)
            y_on = read(f["name"] + " ON", png)
            set_fald(False)
            exp = f["expected_nits"]
            e_off = (y_off / exp - 1) if y_off else None; e_on = (y_on / exp - 1) if y_on else None
            results.append({**f, "y_off": y_off, "y_on": y_on, "err_off": e_off, "err_on": e_on})
            P.log(f"   {f['name']:<16} exp {exp:6.1f}  OFF {y_off if y_off else float('nan'):7.2f} ({100*(e_off or 0):+6.2f} %, model {100*f['pred_err_off']:+6.2f})"
                  f"   ON {y_on if y_on else float('nan'):7.2f} ({100*(e_on or 0):+6.2f} %, model {100*f['pred_err_on']:+6.2f})")
        for kind in kinds:
            rs = [r for r in results if r["kind"] == kind and r["err_off"] is not None and r["err_on"] is not None]
            if not rs: continue
            import statistics as st
            off = [r["err_off"] for r in rs]; on = [r["err_on"] for r in rs]
            P.log(f"[ramp] {kind}: lattice amplitude (p-p across shifts) OFF {100*(max(off)-min(off)):5.2f} %  ON {100*(max(on)-min(on)):5.2f} %"
                  f" | mean offset OFF {100*st.mean(off):+5.2f} %  ON {100*st.mean(on):+5.2f} %")
    finally:
        set_fald(False)
        presenter.close()
        try: meter.close()
        except Exception: pass
        OUT.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        json.dump({"meter": meter_xy, "native": native, "layers_at_start": layers, "results": results},
                  open(OUT / f"ramp_probe_{stamp}.json", "w"), indent=1)
        P.log(f"[ramp] saved {OUT / f'ramp_probe_{stamp}.json'}")
        if native:
            try:
                ctrl.exit_calibration(restore_snapshot=True); P.log("[restore] stack restored")
            except Exception as exc:
                P.log(f"[restore] failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
