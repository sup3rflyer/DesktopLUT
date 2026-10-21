"""The cell set of the frozen fade-stack port gates (ASF10 + T11_TMTC; the frozen brief
results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md §5): writes every input frame as the FP16 scRGB frame the FALD layer
receives (RGBA half, alpha 1, the fald_frame.rgba16f layout) plus a cells.txt index, for the C++ WARP cases
(tests/test_fald_fade.cpp, FALD_TEST_FADE_DIR) and the DLC replays (tests/test_fald_fade_*.py).

    python tests/fald_fade_cells.py stills [<dir>]    # CPU only: the still cells, the NaN / Inf cell, the odd-size cell + its panel
    python tests/fald_fade_cells.py seqs [<dir>]      # the four motion sequences (CUDA: the study's grain is drawn by a
                                                      # CUDA generator, so the frames equal the study's frame for frame)
    python tests/fald_fade_cells.py f12a [<dir>]      # CPU only: round 12a's three stills (F12A_B16, r12a/PORT_DELTA.md §4)
    python tests/fald_fade_cells.py f12b [<dir>]      # CPU only: round 12b's ten halo stills (F12B_V8d, r12b/PORT_DELTA.md §4)

<dir> defaults to H:/scratch/DesktopLUT/fald_asf10_port/cells. Inputs (read only): the study's cached frames under
H:/scratch/fald_nearfield_r10|r11 and the frozen study sources under the main checkout's DLC/results (the frozen twin)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np

HERE = Path(__file__).resolve().parent
DLC = HERE.parent
sys.path.insert(0, str(DLC / "src"))

OUT = Path("H:/scratch/DesktopLUT/fald_asf10_port/cells")
STUDY = Path("H:/Projects/DesktopLUT/DLC/results/fald_nearfield_2026-10-17")   # the frozen twin (main checkout, read only)
SCR10 = Path("H:/scratch/fald_nearfield_r10/inputs")
SCR11 = Path("H:/scratch/fald_nearfield_r11/inputs")

# PORT_PLAN §5.2 still cells (cached tm16.npy = the hook-tone-mapped FP16 frame the layer receives)
STILLS = ("p03_hl_grey5", "p02_hl_black", "n7_lamp", "n8_chess", "n9_cactus", "k1_held", "o1_held", "p07_dark_bar0",
          "s10_sm_seattle", "s10_ac_firewater")
# PORT_PLAN §5.2 sequence cells: (study sequence, test) of r11/T/motion11.py, 16 content frames each
SEQS = (("s10_sm_seattle", "p6n"), ("s10_ac_firewater", "p6"), ("t_hl0_v16", "mv"), ("t_cutdark", "mv"))
ODD_W, ODD_H = 1366, 768
# round 12a (F12A_B16, r12a/PORT_DELTA.md §4): the owner's ACES markers 18 / 52 (A's and B16's target frames) and A's largest
# effect (a caption 50-100 px above the bottom border). NOT listed in cells.txt: cells.txt is the OFF gate's cell set, frozen
# with its manifest (tests/fixtures/fald_fade_off_main.hashes, recorded on main 6273464)
F12A_STILLS = ("aces_m18", "aces_m52", "s10_ac_redspot")
ACES = Path("H:/scratch/DesktopLUT/fald_fade_lines")   # m18 / m52_tm16.npy (results/fald_fade_lines_2026-10-20/make_inputs.py)
# round 12b (F12B_V8d, r12b/PORT_DELTA.md §4): its held-out halo frames (dark objects next to big glows: ACES frames decoded
# by r12b/aces12b.py, the s10 real frames) and s10_ac_rec2020 (an R-2 band frame, where V moves the band most). Like
# F12A_STILLS, NOT listed in cells.txt (the OFF gate's frozen set)
F12B_STILLS = ("ax_552", "ax_412", "ax_422", "ax_442", "ax_492", "ax_522",
               "s10_sm_windmill", "s10_sm_cows", "s10_sm_snowfence", "s10_ac_rec2020")
R12B_INPUTS = Path("H:/scratch/DesktopLUT/fald_r12b/inputs")   # ax_<s>/tm16.npy (r12b/run12b.py pair_input)
ODD_LATTICE = (17, 9)                  # 80 x 85 px zones = 1360 x 765 px: 6 columns + 3 rows outside the lattice
ODD_SRC = ("n9_cactus", 700, 1200)     # the odd cell = this still cropped at (y0, x0)


def rgba16(tm16: np.ndarray) -> np.ndarray:
    tm16 = np.asarray(tm16, np.float16)
    out = np.ones(tm16.shape[:2] + (4,), dtype=np.float16)
    out[..., :3] = tm16
    return out


def cached_still(name: str) -> np.ndarray:
    for d in (SCR11 / name, SCR10 / name):
        p = d / "tm16.npy"
        if p.exists():
            return np.load(p)
    raise FileNotFoundError(f"no cached tm16.npy for {name} (rebuild it with r11/run11.py pair_input)")


def naninf_frame() -> np.ndarray:
    """n7_lamp with the values the fade's sanitise step exists for (fade9: nan -> 0, +inf -> 1e4, -inf -> 0, then [0, 1e4]):
    NaN blocks (one per channel and all three), +Inf / -Inf pixels and blocks, mixed +/-Inf (the 2020 matrix makes NaN of
    it), and > 1e4-nit blocks (scRGB 250 / 600 = 20 000 / 48 000 nits)."""
    f = np.array(cached_still("n7_lamp"), dtype=np.float16)
    nan, pinf, ninf = np.float16(np.nan), np.float16(np.inf), np.float16(-np.inf)
    f[300:340, 500:540, :] = nan                      # 40 x 40 NaN block, all channels
    f[300:340, 600:640, 0] = nan                      # ... red only
    f[1200:1203, 1900:1903, :] = pinf                 # 3 x 3 +Inf
    f[1500:1530, 2500:2530, :] = pinf                 # 30 x 30 +Inf (a whole flank)
    f[900, 3000, :] = ninf                            # single -Inf pixels
    f[901, 3010, 1] = ninf
    f[1700:1702, 800:802, 0] = pinf; f[1700:1702, 800:802, 1] = ninf   # mixed: NaN after the 2020 matrix
    f[600:610, 1000:1010, :] = np.float16(600.0)      # 48 000 nits
    f[1800:1850, 3300:3350, :] = np.float16(250.0)    # 20 000 nits next to dark content
    return f


def odd_frame() -> np.ndarray:
    name, y0, x0 = ODD_SRC
    return np.array(cached_still(name)[y0:y0 + ODD_H, x0:x0 + ODD_W], dtype=np.float16)


def write_odd_panel(path: Path) -> None:
    """A PQ panel file for the 1366 x 768 cell (the owner's PA32 files need 3840 x 2160): a 17 x 9 mean-rule lattice with a
    boost LUT, the exp estimate of the C14 / C16 synthetic WARP cases."""
    from dlc.fald.export import export_panel_params
    from dlc.fald.model import FaldModel, FaldParams
    cols, rows = ODD_LATTICE
    # the model needs whole zones at its reduced scale: it is built on the lattice itself (1360 x 765); the file's lattice
    # then sits at the origin of the 1366 x 768 frame (FaldLatticeFits), the 6 + 3 px outside it pass through
    p = FaldParams(width=cols * 80, height=rows * 85, cols=cols, rows=rows, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                   tmin=1.5e-3, boost_lut=((0.0, 1.17), (0.30, 1.10), (0.60, 1.0)), boost_rule="mean",
                   lum_fade_lo=0.5, lum_fade_hi=3.0)
    export_panel_params(FaldModel(p), path)


def write_cell(root: Path, index: list, name: str, frame16: np.ndarray) -> None:
    f = rgba16(frame16)
    f.tofile(root / f"{name}.rgba16f")
    index.append(f"{name} {f.shape[1]} {f.shape[0]}")


def stills(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    index: list = []
    for name in STILLS:
        write_cell(root, index, name, cached_still(name))
    write_cell(root, index, "x_naninf", naninf_frame())
    write_cell(root, index, "x_odd1366", odd_frame())
    write_odd_panel(root / "odd_panel.bin")
    (root / "cells.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"{len(index)} still cells -> {root}")


def f12a_still(name: str) -> np.ndarray:
    if name.startswith("aces_m"):
        return np.load(ACES / f"m{name[6:8]}_tm16.npy")       # r12a/run12a.py pair_input
    return cached_still(name)


def f12a(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in F12A_STILLS:
        rgba16(f12a_still(name)).tofile(root / f"{name}.rgba16f")
        print(f"{name} -> {root}")


def f12b_still(name: str) -> np.ndarray:
    if name.startswith("ax_"):
        return np.load(R12B_INPUTS / name / "tm16.npy")
    return cached_still(name)


def f12b(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in F12B_STILLS:
        rgba16(f12b_still(name)).tofile(root / f"{name}.rgba16f")
        print(f"{name} -> {root}")


def seqs(root: Path) -> None:
    """The four sequences through r11/T/motion11.py's own frame builders and FP16 encode (motion11.to16), so frame t here
    is the frame the study's TEmu.run saw at t (grain from the same CUDA generator seeds)."""
    import torch
    for p_ in (str(STUDY), str(STUDY / "r9"), str(STUDY / "r10"), str(STUDY / "r11"), str(STUDY / "r11" / "T")):
        if p_ not in sys.path:
            sys.path.insert(0, p_)
    import temu as TM
    import motion11 as M
    import run11 as R
    assert torch.cuda.is_available(), "the study's grain is drawn by a CUDA generator"
    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(min(1.0, 3.0 * 2 ** 30 / total))
    te = TM.TEmu(R.panel_o("s10_x", {}))
    for name, test in SEQS:
        d = root / f"seq_{name}__{test}"
        d.mkdir(parents=True, exist_ok=True)
        n = 0
        for t, X, _al in M.frames(name, test):
            f16 = TM.npy(M.to16(te, X))
            rgba16(f16).tofile(d / f"t{t:02d}.rgba16f")
            n += 1
            del X
            torch.cuda.synchronize()
        (d / "cells.txt").write_text(f"{name} {test} {n} 3840 2160\n", encoding="utf-8")
        torch.cuda.empty_cache()
        print(f"{name} {test}: {n} frames -> {d}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "stills"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    {"stills": stills, "seqs": seqs, "f12a": f12a, "f12b": f12b}[what](root)
