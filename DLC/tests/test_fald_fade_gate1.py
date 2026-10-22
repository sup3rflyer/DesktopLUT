"""Gate 1 of the frozen fade-stack port (results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md 5.2, step 1): the float32
GPU-order port (dlc.fald.fadeemu) against the frozen float64 study twin, on the plan's cells, with the tolerances frozen in
tests/fald_fade_frozen_tolerances.json before any comparison ran.

The twin = the study's own sources: since FREEZE 2026-10-21 F12B_V8d (r12b/PORT_DELTA.md) r12b/cands12b.build of variant
F12B_V8d (stills: still=True; sequences: with the F10_TMTC TL11State) on r12a/cands12a.py, r10/temu.py TEmu and the
2026-10-19 sources, imported read-only from the main checkout's results folder and hash-checked against FROZEN_hashlog.txt;
dlc.fald.gpuemu is the frozen file (content hash checked). It runs on the CPU (float64; CUDA hidden): only the reduction
order of torch's sums differs from the study's GPU runs. Zone drives and boosts are frozen to the twin's own stateless round
drives (fadeemu.FrozenDrives = the r10 FixedMaps pattern), so the gate tests the per-pixel stack. Sequences also check the
twin run against the study's per-frame logs (r12b/out/motion12b/*.json, F12B_V8d cutlog). The tolerances: the 2026-10-20
file's numbers, re-frozen unchanged for this port in tests/fald_fade_frozen_tolerances_f12b.json (all three hashes checked).
V8d's gate V itself is reported (twin cands12b.floor_valley8 vs fadeemu), not gated.

Opt-in (minutes per 4K frame):
    $env:FALD_FADE_GATE_CELLS = "H:/scratch/DesktopLUT/fald_asf10_port/cells"   # tests/fald_fade_cells.py stills + seqs
    $env:FALD_FADE_GATE_OUT = "<dir>"                                             # results JSON per cell
    python -m pytest -q -p no:xdist tests/test_fald_fade_gate1.py -k <cell>
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
DLC = HERE.parent
CELLS = os.environ.get("FALD_FADE_GATE_CELLS", "")
OUTD = os.environ.get("FALD_FADE_GATE_OUT", "")
STUDY = Path(os.environ.get("FALD_FADE_STUDY", "H:/Projects/DesktopLUT/DLC/results/fald_nearfield_2026-10-17"))
PANEL = Path(os.environ.get("FALD_FADE_PANEL",
                            "H:/Projects/DesktopLUT/DLC/results/fald_candidate_2026-10-09/candidate_C_fade3_only.bin"))
TOL_FILE = HERE / "fald_fade_frozen_tolerances.json"
TOL_SHA_LF = "9267ba793571f4f1da0eb35986f7ca7ac11a423ee02817c9365bbcc051f1fb25"
TOL_F12A_FILE = HERE / "fald_fade_frozen_tolerances_f12a.json"   # the F12A_B16 port's freeze (the same numbers)
TOL_F12A_SHA_LF = "15dc5ff7e070096c20d3f855147a4ab62236857fe2968d417a33b9d8c1693f1f"
TOL_F12B_FILE = HERE / "fald_fade_frozen_tolerances_f12b.json"   # the F12B_V8d port's freeze (the same numbers)
TOL_F12B_SHA_LF = "3f87bae6403168534b17c5b0f38d86ca8af5900c8b5624b30a1acc97f58d372d"
FROZEN = {                                   # FROZEN_hashlog.txt, "FREEZE 2026-10-19" + "... 2026-10-20 F12A_B16" + "... 2026-10-21 F12B_V8d"
    "r12b/cands12b.py": "89bfcdb597cfdd146f23c24d8a93fc6fb7bb343c3d2ec7e6fcc5af96f8b08c0b",
    "r12a/cands12a.py": "08eb919a0574a69c5ab05ca2a486a3c5d2844aea2bab7c97bef718abb106df61",
    "r9/cands9.py": "cd6097afe3ffe6848b4375d1b43a580dc27e93f8cb547e610c867a6602e0ba9c",
    "r10/cands10.py": "b738f3b9871568eacb0efdf61b6c7c695308fed21bf08977b7f5bab3279ffbda",
    "r10/temu.py": "7e43465df74b076f2054b3177c9b8e9a793533c86033ca6d91906b5e70c7fa04",
    "r11/cands11.py": "989cb34b9426a2a88f2b284b767803378d93485b0f32a2869f3bbafa18572fbd",
    "r11/T/tl11.py": "756cf2f842b5a6f0c5fd93b599994a5b5be32e914b86a8d0f2ebc2fa5e08a7bd",
    "r11/T/motion11.py": "4934216b577c5b17a6dd2615f183f5e5a4d67632ecb3a0ac0b2738689da95150",
    "r11/run11.py": "44278c1348d2c091d8f697e3c36bc2760d14edc3853620042412693f10556a0c",
    "cands.py": "6ee0c1bb7a833621ca889a1b8ae6c6c270fa1667fed08ff32008d53930e0e987",
    "nlib.py": "6e65528af986316fbb71ccbef52005123ef7962b4e8767b46392169ecc26c5ee",
}
GPUEMU_SHA_LF = "906ba21638e90d354ddd4c33712df48b94b99060387f1790fd7ec8a782c2b0bc"
GPUEMU_FRACTIONAL_GUARD_SHA_LF = "d79bd33b9e9171af84f18c94c75ea1cf10e4559c9048206288d6741c3a6f5ee1"
PANEL_SHA = "f6e51a6a86fe1c0dc39ea3391176dec35c64f68bb4ad780421810d41926ef25c"
STILLS = ("p03_hl_grey5", "p02_hl_black", "n7_lamp", "n8_chess", "n9_cactus", "k1_held", "o1_held", "p07_dark_bar0",
          "s10_sm_seattle", "s10_ac_firewater",
          "aces_m18", "aces_m52", "s10_ac_redspot",    # + round 12a's three (r12a/PORT_DELTA.md §4)
          "ax_552", "ax_412", "ax_422", "ax_442", "ax_492", "ax_522",          # + round 12b's ten halo stills
          "s10_sm_windmill", "s10_sm_cows", "s10_sm_snowfence", "s10_ac_rec2020")   # (r12b/PORT_DELTA.md §4)
SEQS = ("s10_sm_seattle__p6n", "s10_ac_firewater__p6", "t_hl0_v16__mv", "t_cutdark__mv")
VAR = "F12B_V8d"                             # the frozen variant (cands12b.VARIANTS)
STUDY_LOG = {"s10_sm_seattle__p6n": "s10_sm_seattle__p6n", "s10_ac_firewater__p6": "s10_ac_firewater__p6",
             "t_hl0_v16__mv": "t_hl0_v16__mv", "t_cutdark__mv": "t_cutdark__mv"}

pytestmark = pytest.mark.skipif(not CELLS, reason="FALD_FADE_GATE_CELLS not set (opt-in gate, minutes per 4K frame)")


def _sha(path: Path, lf: bool = False) -> str:
    b = path.read_bytes()
    if lf:
        b = b.replace(b"\r\n", b"\n")
    return hashlib.sha256(b).hexdigest()


_ENV: dict = {}


def env():
    """The frozen twin + the port, imported once per process (CPU only, BELOW_NORMAL, CPUs 0-27)."""
    if _ENV:
        return _ENV
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    try:
        import psutil
        pr = psutil.Process()
        pr.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        aff = pr.cpu_affinity()
        pr.cpu_affinity([c for c in aff if c < 28] or list(range(28)))
    except Exception:
        pass
    assert _sha(TOL_FILE, lf=True) == TOL_SHA_LF, "the frozen tolerance file changed"
    assert _sha(TOL_F12A_FILE, lf=True) == TOL_F12A_SHA_LF, "the F12A_B16 tolerance freeze changed"
    assert _sha(TOL_F12B_FILE, lf=True) == TOL_F12B_SHA_LF, "the F12B_V8d tolerance freeze changed"
    for rel, sha in FROZEN.items():
        assert _sha(STUDY / rel) == sha, f"frozen twin source changed: {rel}"
    # gpuemu.py = the frozen file, or the frozen file + exactly the fractional-pitch refusal (merged 2026-10-20 from
    # 429cffa: a ValueError for a fractional panel file, which no gate cell uses): with that block cut out, the frozen hash
    emu = (DLC / "src" / "dlc" / "fald" / "gpuemu.py").read_bytes().replace(b"\r\n", b"\n")
    i = emu.find(b'        if o.get("fractional"):\n')
    if i >= 0:
        j = emu.index(b'        self.white = float(o["white"])', i)
        assert hashlib.sha256(emu[i:j]).hexdigest() == GPUEMU_FRACTIONAL_GUARD_SHA_LF, "gpuemu.py's fractional guard changed"
        emu = emu[:i] + emu[j:]
    assert hashlib.sha256(emu).hexdigest() == GPUEMU_SHA_LF, "gpuemu.py is not the frozen file"
    assert _sha(PANEL) == PANEL_SHA, "not the live panel file candidate_C_fade3_only.bin"
    import torch
    torch.set_num_threads(int(os.environ.get("FALD_FADE_TORCH_THREADS", "2")))
    sys.path.insert(0, str(DLC / "src"))
    from dlc.fald import fadeemu as FE                         # this checkout's dlc first (its gpuemu = the frozen file)
    from dlc.fald.gpuemu import Emu
    from dlc.fald.panelfile import read_panel_file
    for p in (STUDY / "r12b", STUDY / "r12a", STUDY / "r11" / "T", STUDY / "r11", STUDY / "r10", STUDY / "r9", STUDY):
        if str(p) not in sys.path:
            sys.path.insert(1, str(p))
    import temu as TM
    TM.DEV = torch.device("cpu")
    TM.sync = lambda: None
    import cands10 as K10
    import cands11 as K11
    import cands12a as K12   # F12A_B16 (floor_lit12, P9 / PO10: cands12b builds on it)
    import cands12b as K12B  # F12B_V8d (it calls cands10.knee_reach and its floor_valley8 through the modules: the
    import tl11 as TL        # captures below see them)
    from nlib import C
    cap = {"reach": [], "V": []}
    orig = K10.knee_reach
    orig_fv = K12B.floor_valley8

    def rec(kc, D, mi=None, lam=None):                       # records the twin's reach, changes nothing
        r = orig(kc, D, mi, lam)
        cap["reach"].append(TM.npy(r))
        return r

    def rec_fv(mi, X):                                       # records the twin's V (a diagnostic), changes nothing
        v = orig_fv(mi, X)
        cap["V"].append(TM.npy(v))
        return v
    K10.knee_reach = rec
    K12B.floor_valley8 = rec_fv
    _ENV.update(FE=FE, Emu=Emu, read_panel_file=read_panel_file, TM=TM, K11=K11, K12=K12, K12B=K12B, TL=TL, C=C,
                torch=torch, cap=cap, tol=json.loads(TOL_FILE.read_text(encoding="utf-8")), o=read_panel_file(PANEL))
    return _ENV


def v_diag(E, port) -> float:
    """max |V_port - V_twin| (full res; V_port = clamp(up8(v8), 0, 1)), the last V the twin formed — reported, not gated."""
    FE = E["FE"]
    if not E["cap"]["V"] or port.get("v8") is None:
        return float("nan")
    vt = E["cap"]["V"][-1]
    vp = FE.hclamp(FE.up(port["v8"], FE.FV_Q, vt.shape), 0.0, 1.0)
    return float(np.max(np.abs(vp.astype(np.float64) - vt)))


def load16(path: Path, w=3840, h=2160) -> np.ndarray:
    return np.fromfile(path, dtype=np.float16).reshape(h, w, 4)[..., :3].copy()


def fp16_steps(a16, b16) -> np.ndarray:
    """per pixel: the max over channels of |a - b| / spacing(|b|) in FP16 (both NaN = 0, one NaN = inf)."""
    a = np.asarray(a16, np.float16).astype(np.float64)
    b = np.asarray(b16, np.float16)
    ulp = np.spacing(np.abs(b)).astype(np.float64)
    bb = b.astype(np.float64)
    d = np.abs(a - bb) / np.maximum(ulp, 1e-30)
    both = np.isnan(a) & np.isnan(bb)
    d = np.where(both, 0.0, np.where(np.isnan(d), np.inf, d))
    return d.max(axis=-1)


def twin_frame(E, te, f16, star=True):
    TM, torch = E["TM"], E["torch"]
    src, bal, bal_s, scale, plan = TM.balanced_source(te, torch.as_tensor(f16), star)
    img = te.panel_nits(bal_s)
    te.last.clear()
    E["cap"]["reach"].clear()
    E["cap"]["V"].clear()
    r = te.run(img, temporal=TM.FixedMaps(), frame16=bal_s)
    L = te.last
    out = {"fade": TM.npy(L.get("fade_inst", L["fade0"])), "fade_t": TM.npy(L["fade0"]), "F": TM.npy(L["F"]),
           "reach0": E["cap"]["reach"][0], "reach1": E["cap"]["reach"][1], "out": TM.npy(r["out"]),
           "drive0": r["drive0"], "drive1": r["drive1"], "boost0": r["boost0"], "boost1": r["boost1"],
           "img": TM.npy(img)}    # the frame the twin's layer worked on (Balance through its scRGB round trip): the port
                                  # gets the same input (gate 1 = same inputs)
    del src, bal, bal_s, img, r
    te.last.clear()
    return out


def compare(E, twin, port, s_twin=None, s_port=None) -> dict:
    FE, tol = E["FE"], E["tol"]["gate1_f32_vs_f64"]
    rep = {}
    for k in ("fade", "fade_t", "reach0", "reach1"):
        rep[k] = float(np.max(np.abs(port[k].astype(np.float64) - twin[k])))
    if s_twin is not None:
        rep["s"] = float(np.max(np.abs(s_port.astype(np.float64) - s_twin)))
    lit64 = np.clip((twin["F"] - 1.0) / 2.0, 0.0, 1.0)
    lit64 = lit64 * lit64 * (3.0 - 2.0 * lit64)
    rep["lit"] = float(np.max(np.abs(FE.smooth(1.0, 3.0, port["F"]).astype(np.float64) - lit64)))
    rep["F_rel"] = float(np.max(np.abs(port["F"].astype(np.float64) / twin["F"] - 1.0)))
    st = fp16_steps(port["out"], twin["out"])
    rep["out_max_steps"] = float(st.max())
    rep["out_share_le2"] = float((st <= 2.0).mean())
    rep["out_bit_equal"] = float((st == 0.0).mean())
    ok = all(rep[k] <= tol["fields_abs"]["tolerance"] for k in ("fade", "fade_t", "reach0", "reach1") + (("s",) if s_twin is not None else ()))
    ok = ok and rep["lit"] <= tol["floor_F"]["lit_abs"] and rep["F_rel"] <= tol["floor_F"]["F_rel"]
    ok = ok and rep["out_max_steps"] <= tol["out16"]["max_steps"] and rep["out_share_le2"] >= tol["out16"]["share_le_2_steps_min"]
    rep["pass"] = bool(ok)
    return rep


def save(name: str, rep: dict) -> None:
    if OUTD:
        Path(OUTD).mkdir(parents=True, exist_ok=True)
        (Path(OUTD) / f"gate1_{name}.json").write_text(json.dumps(rep, indent=1, default=float), encoding="utf-8")


@pytest.mark.parametrize("cell", STILLS)
def test_gate1_still(cell):
    E = env()
    FE, K12B = E["FE"], E["K12B"]
    f16 = load16(Path(CELLS) / f"{cell}.rgba16f")
    te, _post, _tl = K12B.build(E["o"], VAR, still=True)   # the frozen variant on a still (TL dropped)
    t0 = time.time()
    tw = twin_frame(E, te, f16)
    t1 = time.time()
    fz = FE.FrozenDrives(tw["drive0"], tw["drive1"], tw["boost0"], tw["boost1"])
    port = FE.FadeEmu(E["Emu"](E["o"])).run(f16.astype(np.float64), star=E["C"].OWNER_STAR, frozen=fz, img=tw["img"])
    t2 = time.time()
    rep = compare(E, tw, port)
    rep.update(cell=cell, twin_s=t1 - t0, port_s=t2 - t1, V_diag=v_diag(E, port))
    save(cell, rep)
    print(json.dumps(rep))
    assert rep["pass"], rep


def test_gate1_naninf_fade():
    """x_naninf: the sanitise step — the fade field only, both sides given the same max channel (the HLSL's PanelNits:
    max(M scrgb, 0) drops a NaN to 0; +inf stays inf)."""
    E = env()
    FE, TM = E["FE"], E["TM"]
    from dlc.fald.gpuemu import BT709_TO_BT2020
    f16 = load16(Path(CELLS) / "x_naninf.rgba16f")
    nits = np.fmax(f16.astype(np.float64) @ BT709_TO_BT2020.T, 0.0) * 80.0
    mi = nits.max(axis=-1)                                      # (no NaN left: fmax dropped it; +inf kept)
    K12, K12B = E["K12"], E["K12B"]
    f64 = TM.npy(K12B.fade12b(TM.T(mi), K12.P9(), K12B.VARIANTS[VAR]["X"]))   # F12B_V8d (fade9_t + A + V8d)
    f32_ = FE.fade9(mi.astype(np.float32))
    d = float(np.max(np.abs(f32_.astype(np.float64) - f64)))
    tol = E["tol"]["gate1_f32_vs_f64"]["fields_abs"]["tolerance"]
    rep = {"cell": "x_naninf", "fade": d, "nonfinite_px": int((~np.isfinite(mi)).sum()), "over_1e4_px": int((mi > 1e4).sum()),
           "pass": d <= tol}
    save("x_naninf", rep)
    print(json.dumps(rep))
    assert rep["pass"], rep


def test_gate1_odd_fade_informative():
    """x_odd1366 (not in the frozen gate-1 list: the study's TEmu is 4K-only): fade9 / floor / T-free reach pieces on the
    odd size, against the twin's functions — the padding of down / up / push-pull. Reported, asserted at gate 1's field
    tolerance."""
    E = env()
    FE, TM = E["FE"], E["TM"]
    import cands10 as K10
    from dlc.fald.gpuemu import BT709_TO_BT2020
    f16 = load16(Path(CELLS) / "x_odd1366.rgba16f", 1366, 768)
    mi = (np.fmax(f16.astype(np.float64) @ BT709_TO_BT2020.T, 0.0) * 80.0).max(axis=-1)
    K12, K12B = E["K12"], E["K12B"]
    X = K12B.VARIANTS[VAR]["X"]                                                # F12B_V8d (BASE_X = A + B16, + V8d)
    d_fade = float(np.max(np.abs(FE.fade9(mi.astype(np.float32)).astype(np.float64) - TM.npy(K12B.fade12b(TM.T(mi), K12.P9(), X)))))
    F64 = TM.npy(K12.floor_lit12(TM.T(mi), K12.PO10(), X))                     # B16
    d_F = float(np.max(np.abs(FE.floor_lit(mi.astype(np.float32)) / F64 - 1.0)))
    kc = np.clip(mi / max(float(mi.max()), 1e-9), 0, 1)
    r64 = TM.npy(K10.knee_reach(TM.T(kc), 96.0, TM.T(mi), 2.0))
    r32 = FE.reach_of(kc.astype(np.float32), FE.reach_dec(mi.astype(np.float32)), mi.shape)
    d_r = float(np.max(np.abs(r32 - r64)))
    tol = E["tol"]["gate1_f32_vs_f64"]
    rep = {"cell": "x_odd1366", "fade": d_fade, "F_rel": d_F, "reach": d_r,
           "pass": d_fade <= tol["fields_abs"]["tolerance"] and d_r <= tol["fields_abs"]["tolerance"] and d_F <= tol["floor_F"]["F_rel"]}
    save("x_odd1366", rep)
    print(json.dumps(rep))
    assert rep["pass"], rep


@pytest.mark.parametrize("seq", SEQS)
def test_gate1_sequence(seq):
    E = env()
    FE, K12B, TL, TM = E["FE"], E["K12B"], E["TL"], E["TM"]
    d = Path(CELLS) / f"seq_{seq}"
    spec = TL.spec_of("F10_TMTC")
    tl = TL.tl_state(spec)
    te, _post, _tl = K12B.build(E["o"], VAR, tl_state=tl, still=False)
    ts = FE.TState()
    fe = FE.FadeEmu(E["Emu"](E["o"]))
    tol = E["tol"]["gate1_f32_vs_f64"]
    frames = []
    ok = True
    for t in range(16):
        f16 = load16(d / f"t{t:02d}.rgba16f")
        t0 = time.time()
        tw = twin_frame(E, te, f16)
        s_twin = TM.npy(tl.s)
        t1 = time.time()
        fz = FE.FrozenDrives(tw["drive0"], tw["drive1"], tw["boost0"], tw["boost1"])
        port = fe.run(f16.astype(np.float64), star=E["C"].OWNER_STAR, tstate=ts, frozen=fz, img=tw["img"])
        t2 = time.time()
        rep = compare(E, tw, port, s_twin, ts.s)
        a, b = tl.log[-1], ts.log[-1]
        rep.update(t=t, twin_s=t1 - t0, port_s=t2 - t1, V_diag=v_diag(E, port), cut=[bool(a.get("cut")), bool(b.get("cut"))],
                   used=[a.get("shift_used"), b.get("shift_used")], shift=[a.get("shift8"), b.get("shift8")],
                   pc=[[a.get("pc_dy"), a.get("pc_dx")], [b.get("pc_dy"), b.get("pc_dx")]],
                   frac=[a.get("frac_cd"), b.get("frac_cd")], emd=[a.get("emd"), b.get("emd")])
        dec_ok = rep["cut"][0] == rep["cut"][1] and (a.get("shift_used") == b.get("shift_used"))
        if dec_ok and a.get("shift_used") and a.get("shift8") is not None:
            ds = max(abs(a["shift8"][0] - b["shift8"][0]), abs(a["shift8"][1] - b["shift8"][1]))
            rep["shift_diff"] = ds
            dec_ok = ds <= tol["shift"]["abs_texel"]
        rep["decisions_ok"] = bool(dec_ok)
        rep["pass"] = bool(rep["pass"] and dec_ok)
        ok = ok and rep["pass"]
        frames.append(rep)
        print(json.dumps(rep), flush=True)
    # the twin run here must be the study's: its per-frame log (F12B_V8d, round 12b) reproduced
    study = json.loads((STUDY / "r12b" / "out" / "motion12b" / f"{STUDY_LOG[seq]}.json").read_text())[VAR]["cutlog"]
    log_ok = len(study) == len(tl.log)
    worst = 0.0
    for a, s in zip(tl.log, study):
        log_ok = log_ok and bool(a.get("cut")) == bool(s.get("cut")) and a.get("shift_used") == s.get("shift_used")
        if a.get("shift8") is not None and s.get("shift8") is not None:
            worst = max(worst, abs(a["shift8"][0] - s["shift8"][0]), abs(a["shift8"][1] - s["shift8"][1]))
    log_ok = log_ok and worst <= 1e-6
    rep = {"seq": seq, "frames": frames, "pass": bool(ok), "study_log_reproduced": bool(log_ok), "study_shift_worst": worst}
    save(seq, rep)
    assert log_ok, f"the twin run does not reproduce the study's log (worst shift {worst})"
    assert ok, [f for f in frames if not f["pass"]]
