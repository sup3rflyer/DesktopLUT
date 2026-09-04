"""SPD resolution-sensitivity + observer-metamerism analysis for the two rig panels.

Picks up the 2026-09-04 ChatGPT thread ("LG C6 SPD Readings") with the REAL data:

  PA32UCXR (QD mini-LED LCD)  H:\\Projects\\ColorCalibration\\spectral_HDR_20260619_213224\\{red,green,blue,white}.sp
  LG C6 42 WOLED              H:\\Projects\\ColorCalibration\\spectral_HDR_20260902_204421\\{red,green,blue,white}.sp
                              (RGB @ code 712 = saturated sub-peak, W subpixel off; white @ 1023, 25 % window)

Part 1 — resolution sensitivity: blur each measured SPD with an extra Gaussian
         (5..30 nm FWHM) and shift it ±2 nm, recompute CIE 1931 xy / u'v' / dE00.
         Bounds how much a ColorMunki spectral error can move the CCMX reference.
Part 2 — observer metamerism: for each panel solve the RGB mixture whose CIE 2006
         cone response (2° and 10°) equals D65's, and report the CIE 1931 xy that
         mixture has — i.e. what DesktopLUT's MHC-WB should target if the goal is a
         physiological-observer D65 rather than a CIE-1931 D65.  Also the reverse
         (how far off a 1931-calibrated white is under the 2006 observer) and the
         cross-panel mismatch.  A white-anchored second basis checks that the WOLED
         RGBW pipeline does not change the answer.

Run:  python analysis_spd_observer.py [--out DIR]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter1d

import colour
from colour import MSDS_CMFS, SDS_ILLUMINANTS, SpectralShape

PA_DIR = Path(r"H:\Projects\ColorCalibration\spectral_HDR_20260619_213224")
C6_DIR = Path(r"H:\Projects\ColorCalibration\spectral_HDR_20260902_204421")

GRID = SpectralShape(380, 730, 1)          # display SPD grid
WL = GRID.wavelengths
D65_XY_1931 = np.array([0.3127, 0.3290])
LORE = {
    "LG WOLED/CRT match (Light Illusion) 0.308/0.313": (0.308, 0.313),
    "Technicolor xenon match 0.300/0.327": (0.300, 0.327),
}


# ----------------------------------------------------------------------------- IO
def parse_sp(path: Path) -> np.ndarray:
    """Argyll CGATS .sp -> values on the 1 nm GRID (Sprague interpolation)."""
    txt = path.read_text(encoding="utf-8", errors="replace").splitlines()
    fields, data, start, end = None, None, None, None
    for i, line in enumerate(txt):
        s = line.strip()
        if s.startswith("SPECTRAL_START_NM"):
            start = float(s.split('"')[1])
        elif s.startswith("SPECTRAL_END_NM"):
            end = float(s.split('"')[1])
        elif s == "BEGIN_DATA_FORMAT":
            fields = txt[i + 1].split()
        elif s == "BEGIN_DATA":
            data = [float(v) for v in txt[i + 1].split()]
    assert fields and data and start and end, path
    spec_idx = [k for k, f in enumerate(fields) if f.startswith("SPEC_")]
    vals = np.array([data[k] for k in spec_idx])
    wl = np.linspace(start, end, len(spec_idx))
    sd = colour.SpectralDistribution(dict(zip(wl, vals)), name=path.stem)
    sd = sd.interpolate(GRID)                 # uniform 3.333 nm -> Sprague
    return np.clip(sd.values, 0, None)        # negative dark-floor wiggles -> 0


def load_panel(d: Path) -> dict[str, np.ndarray]:
    return {k: parse_sp(d / f"{k}.sp") for k in ("red", "green", "blue", "white")}


# ----------------------------------------------------------------------- observers
def cmfs_on(name: str, shape: SpectralShape) -> np.ndarray:
    m = MSDS_CMFS[name].copy()
    m = m.align(shape, extrapolator_kwargs={"method": "Constant", "left": 0, "right": 0})
    return m.values  # (n, 3)


OBS = {
    "CIE 1931 2°": "CIE 1931 2 Degree Standard Observer",
    "CIE 2015 2°": "CIE 2015 2 Degree Standard Observer",
    "CIE 2015 10°": "CIE 2015 10 Degree Standard Observer",
}
LMS = {
    "CIE 2015 2°": "Stockman & Sharpe 2 Degree Cone Fundamentals",
    "CIE 2015 10°": "Stockman & Sharpe 10 Degree Cone Fundamentals",
}
CMF = {k: cmfs_on(v, GRID) for k, v in OBS.items()}
CONE = {k: cmfs_on(v, GRID) for k, v in LMS.items()}

# ---- CIE 170-1:2006 physiological observer for an arbitrary field size (age 32) --------------
# Components (0.1 nm, 390-830): log10 low-density absorbance L/M/S, ocular-media density (32 y),
# macular density (2° template, peak 0.35).  From the CIE TC1-97 reference implementation
# (github.com/ifarup/ciefunctions, tc1_97/data/absorbances0_1nm.csv); formulas per CIE 170-1.
COMPONENTS = Path(r"H:\Projects\ColorCalibration\spd_observer_analysis_2026-09-04\cie170_components\absorbances0_1nm.csv")


def cie2006_lms_energy(field_deg: float, shape: SpectralShape) -> np.ndarray:
    raw = np.genfromtxt(COMPONENTS, delimiter=",")
    wl = raw[:, 0]
    logA = raw[:, 2:5]
    logA = np.where(np.isnan(logA), -np.inf, logA)   # blank S-cone cells >615 nm = zero absorbance
    docul = raw[:, 5]
    mac_rel = raw[:, 6] / 0.35
    d_mac = 0.485 * np.exp(-field_deg / 6.132)
    d_LM = 0.38 + 0.54 * np.exp(-field_deg / 1.333)
    d_S = 0.30 + 0.45 * np.exp(-field_deg / 1.333)
    dens = np.array([d_LM, d_LM, d_S])
    absorpt = 1 - 10 ** (-dens[None, :] * 10 ** logA)
    lms_q = absorpt * (10 ** (-d_mac * mac_rel - docul))[:, None]
    lms_e = lms_q * wl[:, None]                      # quantal -> energy
    lms_e /= lms_e.max(axis=0, keepdims=True)
    out = np.zeros((len(shape.wavelengths), 3))
    for i in range(3):
        out[:, i] = np.interp(shape.wavelengths, wl, lms_e[:, i], left=0.0, right=0.0)
    return out


def validate_cie2006() -> list[str]:
    """Reconstruct the tabulated 2° and 10° fundamentals from the components; report max error."""
    notes = []
    for f, name in ((2.0, LMS["CIE 2015 2°"]), (10.0, LMS["CIE 2015 10°"])):
        ref = cmfs_on(name, GRID)
        got = cie2006_lms_energy(f, GRID)
        m = ref > 0.05
        rel = np.abs(got[m] - ref[m]) / ref[m]
        notes.append(f"CIE 170-1 model reconstruction vs tabulated {f:.0f}° fundamentals: "
                     f"max abs error {np.abs(got - ref).max():.2e}, max rel error (where >0.05) {rel.max():.2e}")
    return notes


FIELD_OBS = {"CIE 2006 4°": 4.0, "CIE 2006 7°": 7.0}
for k, f in FIELD_OBS.items():
    CONE[k] = cie2006_lms_energy(f, GRID)

# D65 reference, integrated over its own full range (390-780 covers >99.99 % of the CMF weight)
D65_SHAPE = SpectralShape(390, 780, 1)
D65 = SDS_ILLUMINANTS["D65"].copy().align(D65_SHAPE).values
D65_XYZ = {k: D65 @ cmfs_on(v, D65_SHAPE) for k, v in OBS.items()}
D65_LMS = {k: D65 @ cmfs_on(v, D65_SHAPE) for k, v in LMS.items()}
for k, f in FIELD_OBS.items():
    D65_LMS[k] = D65 @ cie2006_lms_energy(f, D65_SHAPE)

CONE_ORDER = ("CIE 2015 2°", "CIE 2006 4°", "CIE 2006 7°", "CIE 2015 10°")


def xyz(S: np.ndarray, obs="CIE 1931 2°") -> np.ndarray:
    return S @ CMF[obs]


def lms(S: np.ndarray, obs: str) -> np.ndarray:
    return S @ CONE[obs]


def xy(XYZ):
    return XYZ[..., :2] / XYZ.sum(-1, keepdims=True)


def upvp(XYZ):
    X, Y, Z = XYZ
    d = X + 15 * Y + 3 * Z
    return np.array([4 * X / d, 9 * Y / d])


def de00_white(XYZ_a, XYZ_b, obs="CIE 1931 2°") -> float:
    """CIEDE2000 between two whites at equal luminance (chroma/hue difference only),
    with Lab referenced to D65 *under the same observer*."""
    wp = D65_XYZ[obs] / D65_XYZ[obs][1]
    La = colour.XYZ_to_Lab(XYZ_a / XYZ_a[1], illuminant=xy(wp))
    Lb = colour.XYZ_to_Lab(XYZ_b / XYZ_b[1], illuminant=xy(wp))
    return float(colour.delta_E(La, Lb, method="CIE 2000"))


def cct_duv(XYZ):
    uv = colour.UCS_to_uv(colour.XYZ_to_UCS(XYZ))
    cct, duv = colour.uv_to_CCT(uv, method="Ohno 2013")
    return cct, duv


# ------------------------------------------------------------------ part 1: blur
def blur(S, fwhm_nm):
    if fwhm_nm <= 0:
        return S
    return gaussian_filter1d(S, fwhm_nm / 2.354820045, mode="nearest")


def shift(S, nm):
    return np.interp(WL - nm, WL, S)


def fwhm_of(S):
    i = int(np.argmax(S))
    half = S[i] / 2
    lo = i
    while lo > 0 and S[lo] > half:
        lo -= 1
    hi = i
    while hi < len(S) - 1 and S[hi] > half:
        hi += 1
    return WL[i], WL[hi] - WL[lo]


def part1(panels, out):
    lines = ["## Part 1 — how much can spectrometer resolution move the CIE 1931 chromaticity?", ""]
    lines.append("Peaks as measured (3.33 nm sampling, ~10 nm optical resolution already inside the numbers). "
                 "Note: the ChatGPT thread quoted the PA32UCXR red/green peaks at 643/537–540 nm; the raw "
                 "arrays put them at 630/530 nm (argmax indices 75/45 of 106) — that was an index slip there.")
    lines.append("")
    lines.append("| panel | channel | peak nm | measured FWHM nm | 1931 x | 1931 y |")
    lines.append("|---|---|---|---|---|---|")
    for pname, P in panels.items():
        for ch in ("red", "green", "blue", "white"):
            pk, fw = fwhm_of(P[ch])
            x, y = xy(xyz(P[ch]))
            lines.append(f"| {pname} | {ch} | {pk:.0f} | {fw:.0f} | {x:.4f} | {y:.4f} |")
    lines.append("")
    lines.append("Extra Gaussian blur ADDED to the measured spectra (so '10 nm' here ≈ 14 nm total effective "
                 "resolution), and a wavelength-calibration shift. Δ = vs the unblurred measurement, "
                 "Δu'v' ×1000, dE00 at equal luminance.")
    lines.append("")
    perturb = [("blur", f) for f in (5, 10, 15, 20, 25, 30)] + [("shift", s) for s in (-2, +2)]
    for pname, P in panels.items():
        lines.append(f"### {pname}")
        lines.append("")
        lines.append("| perturbation | " + " | ".join(f"{ch}: Δx Δy Δu'v'×1e3 dE00" for ch in ("white", "red", "green", "blue")) + " |")
        lines.append("|---|" + "---|" * 4)
        for kind, amt in perturb:
            cells = []
            for ch in ("white", "red", "green", "blue"):
                S0 = P[ch]
                S1 = blur(S0, amt) if kind == "blur" else shift(S0, amt)
                X0, X1 = xyz(S0), xyz(S1)
                dxy = xy(X1) - xy(X0)
                duv = np.hypot(*(upvp(X1) - upvp(X0))) * 1e3
                de = de00_white(X0, X1)
                cells.append(f"{dxy[0]:+.4f} {dxy[1]:+.4f} {duv:.2f} {de:.2f}")
            label = f"+{amt} nm Gaussian" if kind == "blur" else f"{amt:+d} nm λ shift"
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


# ------------------------------------------------------- part 2: observer metamerism
def solve_mixture(basis: np.ndarray, target_vec: np.ndarray, resp) -> np.ndarray:
    """basis: (3, n) SPDs; resp(S)->3-vector; returns weights w s.t. resp(w@basis) ∝ target."""
    A = np.stack([resp(b) for b in basis], axis=1)  # 3x3, columns = basis responses
    return np.linalg.solve(A, target_vec)


def white_anchored(W, basis, obs_resp, target_vec):
    """S = W + Σ δ_i P_i with resp(S) = k·target and Y1931(S) = Y1931(W). Returns S."""
    # unknowns: δR δG δB k
    A = np.zeros((4, 4))
    b = np.zeros(4)
    for r in range(3):
        for c in range(3):
            A[r, c] = obs_resp(basis[c])[r]
        A[r, 3] = -target_vec[r]
        b[r] = -obs_resp(W)[r]
    for c in range(3):
        A[3, c] = xyz(basis[c])[1]
    b[3] = 0.0
    sol = np.linalg.solve(A, b)
    return W + sol[:3] @ basis, sol


def part2(panels, out):
    lines = ["## Part 2 — panel-specific 'perceptual D65' targets (observer metamerism)", ""]
    lines.append("Method: with the measured R/G/B SPDs as the mixing basis, solve the mixture whose cone "
                 "response under the CIE 2006 observer (Stockman & Sharpe fundamentals; identical to matching "
                 "in CIE 2015 XYZ) equals D65's, then report that mixture's ordinary CIE 1931 xy — the number "
                 "DesktopLUT's MHC-WB would target. Reference D65 = CIE D65 SPD, 390–780 nm.")
    lines.append("")
    d65 = {k: xy(v) for k, v in D65_XYZ.items()}
    lines.append("D65 SPD chromaticity under each observer (for orientation): " +
                 "; ".join(f"{k}: x={v[0]:.4f} y={v[1]:.4f}" for k, v in d65.items()))
    lines.append("")
    lines.append("The 4° and 7° observers come from the CIE 170-1 field-size model (age 32) rebuilt from its "
                 "component tables; self-check against the tabulated 2°/10° fundamentals:")
    for n in validate_cie2006():
        lines.append(f"- {n}")
    lines.append("")

    results = {}
    lines.append("| panel | observer matched | target CIE 1931 x | y | Δx Δy vs 0.3127/0.3290 | Δu'v'×1e3 | dE00 | CCT / Duv | RGB weights (rel. to 1931-D65 mix) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for pname, P in panels.items():
        basis = np.stack([P["red"], P["green"], P["blue"]])
        # the CIE-1931 D65 metamer (what a standard calibration produces)
        w1931 = solve_mixture(basis, D65_XYZ["CIE 1931 2°"], xyz)
        S1931 = w1931 @ basis
        results[(pname, "1931")] = S1931
        for obs in CONE_ORDER:
            w = solve_mixture(basis, D65_LMS[obs], lambda S, o=obs: lms(S, o))
            S = w @ basis
            results[(pname, obs)] = S
            X = xyz(S)
            x_, y_ = xy(X)
            dxy = np.array([x_, y_]) - D65_XY_1931
            duv = np.hypot(*(upvp(X) - upvp(xyz(S1931)))) * 1e3
            de = de00_white(xyz(S1931), X)
            cct, duv_k = cct_duv(X / X[1])
            rel = w / w1931
            lines.append(f"| {pname} | {obs} | **{x_:.4f}** | **{y_:.4f}** | {dxy[0]:+.4f} {dxy[1]:+.4f} | {duv:.2f} | {de:.2f} "
                         f"| {cct:.0f} K / {duv_k:+.4f} | R {rel[0]:.4f} G {rel[1]:.4f} B {rel[2]:.4f} |")
    lines.append("")

    # reverse view: the 1931-calibrated white as seen by the 2006 observer
    lines.append("### Reverse view — a white calibrated to CIE 1931 D65, judged by the CIE 2006 observer")
    lines.append("")
    lines.append("| panel | observer | Δu'v'×1e3 from true D65 | dE00 | direction (Δx, Δy in that observer's chromaticity) |")
    lines.append("|---|---|---|---|---|")
    for pname in panels:
        S1931 = results[(pname, "1931")]
        for obs in ("CIE 2015 2°", "CIE 2015 10°"):
            X = xyz(S1931, obs)
            duv = np.hypot(*(upvp(X) - upvp(D65_XYZ[obs]))) * 1e3
            de = de00_white(D65_XYZ[obs], X, obs)
            dxy = xy(X) - xy(D65_XYZ[obs])
            lines.append(f"| {pname} | {obs} | {duv:.2f} | {de:.2f} | {dxy[0]:+.4f}, {dxy[1]:+.4f} |")
    lines.append("")

    # cross-panel mismatch
    lines.append("### Cross-panel mismatch — both panels calibrated to the SAME CIE 1931 D65, seen side by side")
    lines.append("")
    lines.append("| observer | Δu'v'×1e3 PA vs C6 | dE00 |")
    lines.append("|---|---|---|")
    for obs in ("CIE 1931 2°", "CIE 2015 2°", "CIE 2015 10°"):
        Xa = xyz(results[("PA32UCXR", "1931")], obs)
        Xb = xyz(results[("LG C6 42", "1931")], obs)
        lines.append(f"| {obs} | {np.hypot(*(upvp(Xa) - upvp(Xb))) * 1e3:.2f} | {de00_white(Xa, Xb, obs):.2f} |")
    lines.append("")

    # robustness: white-anchored basis (WOLED RGBW check)
    lines.append("### Robustness — white-anchored basis (S = measured white + small R/G/B trims, luminance held)")
    lines.append("")
    lines.append("Checks that the WOLED's RGBW rendering (white subpixel carries most of the white) does not change "
                 "the target: the first-order answer is a property of the white SPD, not of the mixing basis.")
    lines.append("")
    lines.append("| panel | observer | RGB-basis target x y | white-anchored target x y | Δ×1e4 | trims δR δG δB (rel. to white) |")
    lines.append("|---|---|---|---|---|---|")
    for pname, P in panels.items():
        basis = np.stack([P["red"], P["green"], P["blue"]])
        for obs in CONE_ORDER:
            S_rgb = results[(pname, obs)]
            S_wa, sol = white_anchored(P["white"], basis, lambda S, o=obs: lms(S, o), D65_LMS[obs])
            a, b = xy(xyz(S_rgb)), xy(xyz(S_wa))
            trims = sol[:3] * np.array([xyz(bb)[1] for bb in basis]) / xyz(P["white"])[1]
            lines.append(f"| {pname} | {obs} | {a[0]:.4f} {a[1]:.4f} | {b[0]:.4f} {b[1]:.4f} | "
                         f"{(b[0]-a[0])*1e4:+.1f} {(b[1]-a[1])*1e4:+.1f} | {trims[0]:+.3f} {trims[1]:+.3f} {trims[2]:+.3f} (Y-share) |")
    lines.append("")

    # native whites + lore
    lines.append("### Context — native whites and the community lore points")
    lines.append("")
    for pname, P in panels.items():
        x_, y_ = xy(xyz(P["white"]))
        cct, duv = cct_duv(xyz(P["white"]))
        lines.append(f"- {pname} native white (spectro): x={x_:.4f} y={y_:.4f}, {cct:.0f} K, Duv {duv:+.4f}")
    for k, (x_, y_) in LORE.items():
        d = np.hypot(*(upvp(colour.xy_to_XYZ((x_, y_))) - upvp(colour.xy_to_XYZ(D65_XY_1931)))) * 1e3
        lines.append(f"- lore: {k} → Δu'v'×1e3 from D65 = {d:.1f}")
    lines.append("")
    return lines, results


# ------------------------------------------------------------------------- plots
def plots(panels, results, out: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(11, 9), sharex=True)
    cols = {"red": "#d62728", "green": "#2ca02c", "blue": "#1f77b4", "white": "#444444"}
    for ax, (pname, P) in zip(axes, panels.items()):
        for ch in ("white", "red", "green", "blue"):
            S = P[ch] / P["white"].max()
            ax.plot(WL, S, color=cols[ch], lw=1.6, label=f"{ch} (measured)")
            ax.plot(WL, blur(S, 10), color=cols[ch], lw=0.9, ls="--", alpha=0.8)
            ax.plot(WL, blur(S, 20), color=cols[ch], lw=0.9, ls=":", alpha=0.8)
        for ch in ("red", "green", "blue"):
            pk, fw = fwhm_of(P[ch])
            ax.annotate(f"{pk:.0f} nm\nFWHM {fw:.0f}", (pk, (P[ch] / P['white'].max()).max()),
                        textcoords="offset points", xytext=(6, -2), fontsize=8, color=cols[ch])
        ax.set_title(f"{pname} — measured (solid), +10 nm Gaussian (dashed), +20 nm (dotted); ColorMunki 3.33 nm sampling")
        ax.set_ylabel("relative radiance (white peak = 1)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, ncol=4, loc="upper right")
    axes[-1].set_xlabel("wavelength (nm)")
    fig.tight_layout()
    fig.savefig(out / "spd_pa32ucxr_vs_c6_blur.png", dpi=140)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 9.5))
    # Planckian locus segment for orientation
    Ts = np.linspace(4500, 9000, 200)
    xyP = np.array([colour.CCT_to_xy(T, method="Kang 2002") for T in Ts])
    ax.plot(xyP[:, 0], xyP[:, 1], color="#999", lw=1, label="Planckian locus")
    ax.plot(*D65_XY_1931, "k+", ms=12, mew=2, label="CIE 1931 D65 (0.3127, 0.3290)")
    mk = {"PA32UCXR": "o", "LG C6 42": "s"}
    for pname in panels:
        pts = []
        for obs, c in (("CIE 2015 2°", "#e6550d"), ("CIE 2006 4°", "#fd8d3c"), ("CIE 2006 7°", "#c51b8a"), ("CIE 2015 10°", "#756bb1")):
            x_, y_ = xy(xyz(results[(pname, obs)]))
            pts.append((x_, y_))
            ax.plot(x_, y_, mk[pname], color=c, ms=9, label=f"{pname}: {obs}-matched D65 → 1931 ({x_:.4f}, {y_:.4f})")
        ax.plot(*zip(*pts), color="#bbb", lw=0.8, zorder=0)
        x_, y_ = xy(xyz(panels[pname]["white"]))
        ax.plot(x_, y_, mk[pname], mfc="none", color="#333", ms=9, label=f"{pname} native white ({x_:.4f}, {y_:.4f})")
    for k, (x_, y_) in LORE.items():
        ax.plot(x_, y_, "x", color="#31a354", ms=9, mew=2, label=f"lore: {k}")
    ax.set_xlim(0.295, 0.325)
    ax.set_ylim(0.305, 0.340)
    ax.set_xlabel("CIE 1931 x")
    ax.set_ylabel("CIE 1931 y")
    ax.set_title("Where each panel's physiological-observer D65 lands in CIE 1931 xy")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7.5, loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(out / "perceptual_d65_targets_xy.png", dpi=140)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"H:\Projects\ColorCalibration\spd_observer_analysis_2026-09-04")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    panels = {"PA32UCXR": load_panel(PA_DIR), "LG C6 42": load_panel(C6_DIR)}

    lines = ["# SPD resolution sensitivity + observer-metamerism targets (2026-09-04)", "",
             f"Sources: PA32UCXR `{PA_DIR}`, LG C6 42 `{C6_DIR}` (ColorMunki i1Studio via Argyll spotread, "
             "106 bands 380–730 nm). Script: `DLC/analysis_spd_observer.py`.", ""]
    lines += part1(panels, out)
    p2, results = part2(panels, out)
    lines += p2
    plots(panels, results, out)
    lines.append(f"Plots: `{out / 'spd_pa32ucxr_vs_c6_blur.png'}`, `{out / 'perceptual_d65_targets_xy.png'}`")
    report = "\n".join(lines)
    (out / "report.md").write_text(report, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
