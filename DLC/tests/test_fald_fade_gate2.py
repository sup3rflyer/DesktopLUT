"""Gate 2 of the frozen fade-stack port (results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md 5.2, step 2): the HLSL on the
WARP device against the float32 GPU-order twin dlc.fald.fadeemu (on dlc.fald.gpuemu.Emu with WARP's sampler model), with
the tolerances frozen in tests/fald_fade_frozen_tolerances.json before any comparison ran.

Device side: the C++ case "FALD fade ON: WARP dumps for the DLC replay" (tests/test_fald_fade.cpp) — the overlay's layer,
stateless (temporal mode 0), starfield at its defaults (= the owner's settings), the flank fade on; one monitor context per
still (no history) and per sequence (16 content frames). Here every run is replayed frame for frame: output, the base
fields (drive, B_true, B_est, gain), the fade-stack fields (fade, fade_T, s, reach_0 / reach_1, the lit floor) and the T
flags (cut, shift used, advance; the shift).

    $env:FALD_FADE_GATE_CELLS = "<cells>"; $env:FALD_FADE_GATE_ON = "<the C++ case's FALD_TEST_FADE_ON>"
    $env:FALD_FADE_GATE_OUT = "<dir>"     # results JSON per run
    python -m pytest -q -p no:xdist -o "addopts=-p no:cacheprovider" tests/test_fald_fade_gate2.py -k <run>
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
DLC = HERE.parent
sys.path.insert(0, str(DLC / "src"))
CELLS = os.environ.get("FALD_FADE_GATE_CELLS", "")
ON = os.environ.get("FALD_FADE_GATE_ON", "")
OUTD = os.environ.get("FALD_FADE_GATE_OUT", "")
PANEL = Path(os.environ.get("FALD_FADE_PANEL",
                            "H:/Projects/DesktopLUT/DLC/results/fald_candidate_2026-10-09/candidate_C_fade3_only.bin"))
PANEL_SDR = Path(os.environ.get("FALD_FADE_PANEL_SDR",
                                "H:/Projects/DesktopLUT/DLC/results/fald_daily_2026-10-06/pa32ucxr_refit_sdr_fald_panel_blur0.7.bin"))
TOL_FILE = HERE / "fald_fade_frozen_tolerances.json"
TOL_SHA_LF = "9267ba793571f4f1da0eb35986f7ca7ac11a423ee02817c9365bbcc051f1fb25"
TOL_F12A_FILE = HERE / "fald_fade_frozen_tolerances_f12a.json"   # the F12A_B16 port's freeze (the same numbers)
TOL_F12A_SHA_LF = "15dc5ff7e070096c20d3f855147a4ab62236857fe2968d417a33b9d8c1693f1f"
STILLS = ("p03_hl_grey5", "p02_hl_black", "n7_lamp", "n8_chess", "n9_cactus", "k1_held", "o1_held", "p07_dark_bar0",
          "s10_sm_seattle", "s10_ac_firewater", "x_naninf", "x_odd1366", "sdr_n7_lamp",
          "aces_m18", "aces_m52", "s10_ac_redspot")    # + round 12a's three (r12a/PORT_DELTA.md §4)
SEQS = ("s10_sm_seattle__p6n", "s10_ac_firewater__p6", "t_hl0_v16__mv", "t_cutdark__mv")
F_ADVANCE, F_PC_DY, F_PC_DX, F_USED, F_DY, F_DX, F_FRAC, F_EMD, F_CUT = 0, 1, 2, 7, 8, 9, 10, 11, 12

pytestmark = pytest.mark.skipif(not (CELLS and ON), reason="FALD_FADE_GATE_CELLS / FALD_FADE_GATE_ON not set (opt-in gate)")


def _tol():
    b = TOL_FILE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(b).hexdigest() == TOL_SHA_LF, "the frozen tolerance file changed"
    b12 = TOL_F12A_FILE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(b12).hexdigest() == TOL_F12A_SHA_LF, "the F12A_B16 tolerance freeze changed"
    return json.loads(b.decode("utf-8"))["gate2_hlsl_vs_f32"]


def _meta(d: Path) -> dict:
    out = {}
    for line in (d / "meta.txt").read_text().splitlines():
        k, _, v = line.partition(" ")
        out[k] = v
    return out


def _f32(d: Path, name: str, w: int, h: int, c: int = 1) -> np.ndarray:
    a = np.fromfile(d / name, dtype=np.float32)
    return a.reshape(h, w, c)[..., 0] if c == 1 else a.reshape(h, w, c)


def fp16_steps(a16, b16) -> np.ndarray:
    a = np.asarray(a16, np.float16).astype(np.float64)
    b = np.asarray(b16, np.float16)
    ulp = np.spacing(np.abs(b)).astype(np.float64)
    bb = b.astype(np.float64)
    d = np.abs(a - bb) / np.maximum(ulp, 1e-30)
    d = np.where(np.isnan(a) & np.isnan(bb), 0.0, np.where(np.isnan(d), np.inf, d))
    return d.max(axis=-1)


def _close(a, b, rtol, atol, mask=None):
    """max |a - b| - (atol + rtol |b|) over the (masked) elements: <= 0 passes; + the max abs difference."""
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    if mask is not None:
        a, b = a[mask], b[mask]
    if a.size == 0:
        return -1.0, 0.0
    d = np.abs(a - b)
    return float(np.max(d - (atol + rtol * np.abs(b)))), float(np.max(d))


def _cell_frame(rel: str, w: int, h: int) -> np.ndarray:
    return np.fromfile(Path(CELLS) / f"{rel}.rgba16f", dtype=np.float16).reshape(h, w, 4)[..., :3].copy()


def replay(run: str, rels: list) -> dict:
    from dlc.fald import fadeemu as FE
    from dlc.fald.gpuemu import Emu
    from dlc.fald.panelfile import read_panel_file
    from dlc.fald.starfield import StarfieldParams
    try:
        import psutil
        pr = psutil.Process()
        pr.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
    tol = _tol()
    root = Path(ON) / run
    m0 = _meta(root / "t00")
    W, H = int(m0["width"]), int(m0["height"])
    Wq, Hq, W8, H8 = int(m0["wq"]), int(m0["hq"]), int(m0["w8"]), int(m0["h8"])
    star = StarfieldParams() if m0["star"] == "1" else None
    if run == "still_x_odd1366":
        panel = Path(CELLS) / "odd_panel.bin"
    elif run.startswith("still_sdr_"):
        panel = PANEL_SDR
    else:
        panel = PANEL
    o = read_panel_file(panel)
    emu = Emu(o, width=W, height=H, subtexel_bits=8, sampler="warp")
    fe = FE.FadeEmu(emu, hlsl_nan=(run == "still_x_naninf"))
    ts = FE.TState()
    frames, ok = [], True
    for t, rel in enumerate(rels):
        d = root / f"t{t:02d}"
        f16 = _cell_frame(rel, W, H)
        t0 = time.time()
        res = fe.run(f16.astype(np.float64), star=star, tstate=ts)
        rep = {"t": t, "replay_s": time.time() - t0}
        finite = np.all(np.isfinite(f16.astype(np.float32)), axis=-1)
        out = np.fromfile(d / "out.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
        st = fp16_steps(out, res["out"])
        st = np.where(finite, st, 0.0)
        rep["out_max_steps"] = float(st.max())
        rep["out_bit_equal"] = float((st == 0).mean())
        good = rep["out_max_steps"] <= tol["out16_max_steps"]
        # diagnostics only (the verdict above is the frozen per-channel rule): where the pixels over the rule sit — FadePost
        # active or the identity (fade_r = 0), a BT.709 channel cancelled to < 1e-3 of the pixel's largest — and the
        # steps measured at the pixel's largest channel
        o16 = np.asarray(res["out"], np.float16).astype(np.float64)
        big = np.max(np.abs(o16), axis=-1)
        cancelled = np.min(np.abs(o16), axis=-1) < 1e-3 * big
        faded = (res["fade_t"] * (1.0 - res["keep1"])) > 0
        over = st > tol["out16_max_steps"]
        with np.errstate(invalid="ignore", over="ignore"):
            ulp_big = np.spacing(big.astype(np.float16)).astype(np.float64)
            st_big = np.where(finite, (np.abs(out.astype(np.float64) - o16) / np.maximum(ulp_big, 1e-30)[..., None]).max(-1), 0.0)
        rep["diag"] = {"over_px": int(over.sum()), "over_cancelled_px": int((over & cancelled).sum()),
                       "over_faded_px": int((over & faded).sum()),
                       "identity_max_steps": float(st[~faded].max()) if (~faded).any() else None,
                       "max_steps_at_largest_channel": float(st_big.max())}
        bt = tol["base_fields"]
        fs = o["sub"]
        base = {"drive": (_f32(d, "drive.f32", o["cols"], o["rows"]), res["drive1"]),
                "btrue": (_f32(d, "btrue.f32", o["cols"] * fs, o["rows"] * fs), res["bT1"]),
                "best": (_f32(d, "best.f32", o["cols"] * fs, o["rows"] * fs), res["bE1"]),
                "gain": (_f32(d, "gain.rg32f", o["cols"] * fs, o["rows"] * fs, 2)[..., 0], res["gain1"])}
        for k, (dev, twin) in base.items():
            m, mx = _close(dev, twin, bt["rtol"], bt["atol"])
            rep[k] = mx
            good = good and m <= 0
        ft = tol["fade_fields"]
        fade_dev = {"fade": _f32(d, "fade.f32", W, H), "fade_t": _f32(d, "fade_t.f32", W, H),
                    "reach0": _f32(d, "reach0.f32", W, H), "reach1": _f32(d, "reach1.f32", W, H),
                    "s": _f32(d, "s.f32", Wq, Hq)}
        fade_twin = {"fade": res["fade"], "fade_t": res["fade_t"], "reach0": res["reach0"], "reach1": res["reach1"],
                     "s": ts.s if ts.s is not None else res["fade_t"]}
        F_dev = _f32(d, "floor.f32", W, H)
        fade_dev["lit"] = FE.smooth(1.0, 3.0, F_dev)
        fade_twin["lit"] = FE.smooth(1.0, 3.0, res["F"])
        for k in fade_dev:
            m, mx = _close(fade_dev[k], fade_twin[k], ft["rtol"], ft["atol"])
            rep[k] = mx
            good = good and m <= 0
        with np.errstate(divide="ignore", invalid="ignore"):
            rel_F = np.abs(F_dev.astype(np.float64) / res["F"].astype(np.float64) - 1.0)
        rep["F_rel"] = float(np.nanmax(rel_F))
        good = good and rep["F_rel"] <= tol["floor_F_rel"]["tolerance"]
        fl = np.fromfile(d / "flags.u32", dtype=np.uint32)
        rec = ts.log[-1]
        if not rec.get("init"):
            dev_adv = bool(fl[F_ADVANCE])
            dev_cut, dev_used = bool(fl[F_CUT]), bool(fl[F_USED])
            dy, dx = float(fl[F_DY:F_DX + 1].view(np.float32)[0]), float(fl[F_DY:F_DX + 1].view(np.float32)[1])
            rep.update(advance=[dev_adv, bool(rec.get("advance"))], cut=[dev_cut, bool(rec.get("cut"))],
                       used=[dev_used, bool(rec.get("shift_used"))], shift_dev=[dy, dx], shift_twin=rec.get("shift8"),
                       frac=[float(fl[F_FRAC:F_FRAC + 1].view(np.float32)[0]), rec.get("frac_cd")],
                       emd=[float(fl[F_EMD:F_EMD + 1].view(np.float32)[0]), rec.get("emd")])
            flags_ok = dev_adv == bool(rec.get("advance")) and (not dev_adv or (dev_cut == bool(rec.get("cut")) and
                                                                                dev_used == bool(rec.get("shift_used"))))
            if flags_ok and dev_adv and rec.get("shift8") is not None:
                ds = max(abs(dy - rec["shift8"][0]), abs(dx - rec["shift8"][1]))
                rep["shift_diff"] = ds
                flags_ok = ds <= tol["shift_abs_texel"]
            rep["flags_ok"] = bool(flags_ok)
            good = good and flags_ok
        rep["has_history_dev"] = _meta(d)["has_history"] == "1"
        rep["pass"] = bool(good)
        ok = ok and good
        frames.append(rep)
        print(json.dumps(rep, default=float), flush=True)
    rep = {"run": run, "frames": frames, "pass": bool(ok)}
    if OUTD:
        Path(OUTD).mkdir(parents=True, exist_ok=True)
        (Path(OUTD) / f"gate2_{run}.json").write_text(json.dumps(rep, indent=1, default=float), encoding="utf-8")
    return rep


@pytest.mark.parametrize("cell", STILLS)
def test_gate2_still(cell):
    run = f"still_{cell}"
    if not (Path(ON) / run / "t00").exists():
        pytest.skip(f"no device dump for {run}")
    rel = cell[4:] if cell.startswith("sdr_") else cell
    rep = replay(run, [rel])
    assert rep["pass"], rep


@pytest.mark.parametrize("seq", SEQS)
def test_gate2_sequence(seq):
    run = f"seq_{seq}"
    if not (Path(ON) / run / "t00").exists():
        pytest.skip(f"no device dump for {run}")
    n = int(os.environ.get("FALD_FADE_GATE_FRAMES", "16"))       # 16 = the gate; fewer only for the negative control
    missing = [t for t in range(n) if not (Path(ON) / run / f"t{t:02d}").exists()]
    assert not missing, f"{run}: no device dump for frames {missing}"
    rep = replay(run, [f"seq_{seq}/t{t:02d}" for t in range(n)])
    assert rep["pass"], [f for f in rep["frames"] if not f["pass"]]
