"""The still cells of the FALD layer's C++ WARP gates (tests/test_fald_layer_gate.cpp, test_fald_clock_gate.cpp,
test_fald_change.cpp, test_fald_hook.cpp, ...; FALD_TEST_FADE_DIR): writes every input frame as the FP16 scRGB frame the
FALD layer receives (RGBA half, alpha 1, the fald_frame.rgba16f layout) plus a cells.txt index. The cells were cut for
the flank fade's port gates (results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md §5) and are the layer's identity gates'
inputs since; the fade's own sets (its motion sequences, rounds 12a / 12b) went with it on 2026-11-12 (tag
archive/fald-flank-fade).

    python tests/fald_fade_cells.py stills [<dir>]    # CPU only: the still cells, the NaN / Inf cell, the odd-size cell + its panel

<dir> defaults to H:/scratch/DesktopLUT/fald_asf10_port/cells. Inputs (read only): the study's cached frames under
H:/scratch/fald_nearfield_r10|r11."""
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
SCR10 = Path("H:/scratch/fald_nearfield_r10/inputs")
SCR11 = Path("H:/scratch/fald_nearfield_r11/inputs")

# PORT_PLAN §5.2 still cells (cached tm16.npy = the hook-tone-mapped FP16 frame the layer receives)
STILLS = ("p03_hl_grey5", "p02_hl_black", "n7_lamp", "n8_chess", "n9_cactus", "k1_held", "o1_held", "p07_dark_bar0",
          "s10_sm_seattle", "s10_ac_firewater")
ODD_W, ODD_H = 1366, 768
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


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "stills"
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    {"stills": stills}[what](root)
