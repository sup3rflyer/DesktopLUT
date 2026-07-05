"""Phase 2 §0 artifact: where the patches go (luminance band x saturation band)."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3] / "src"))
from dlc.calibrate import (PatchSizes, build_ramp_set, build_volumetric_set,
                           build_neutral_set, build_verify_set, flow_patch_counts)
from dlc.engine.patches import Transfer

PS = PatchSizes()

def sat_of(p):
    mx, mn = max(p), min(p)
    if mx == 0:
        return 0.0
    return (mx - mn) / mx

def lum_band(nits, is_hdr):
    if nits < 0.1: return "<0.1"
    if nits < 1: return "0.1-1"
    if nits < 10: return "1-10"
    if nits < 100: return "10-100"
    if not is_hdr: return "100-peak"
    if nits < 203: return "100-203"
    return ">203(frontier)"

def sat_band(s):
    if s == 0.0: return "neutral"
    if s <= 0.20: return "near-neutral"
    if s <= 0.60: return "mid"
    return "edge"

LUM = ["<0.1", "0.1-1", "1-10", "10-100", "100-peak", "100-203", ">203(frontier)"]
SAT = ["neutral", "near-neutral", "mid", "edge"]

def table(name, patches, transfer, is_hdr):
    grid = {}
    for p in patches:
        # approximate patch luminance: Rec.709 weights on per-channel nits
        r, g, b = p
        nits = (0.2126 * transfer.cv_to_nits(r) + 0.7152 * transfer.cv_to_nits(g)
                + 0.0722 * transfer.cv_to_nits(b))
        key = (lum_band(nits, is_hdr), sat_band(sat_of(p)))
        grid[key] = grid.get(key, 0) + 1
    n = len(patches)
    print(f"\n### {name}  (n={n})")
    cols = [s for s in SAT]
    print(f"{'lum band':>15} | " + " | ".join(f"{c:>12}" for c in cols) + " |    row%")
    for lb in LUM:
        row = [grid.get((lb, sb), 0) for sb in cols]
        if sum(row) == 0: continue
        pct = 100.0 * sum(row) / n
        print(f"{lb:>15} | " + " | ".join(f"{v:>12}" for v in row) + f" | {pct:6.1f}%")
    for sb in cols:
        tot = sum(grid.get((lb, sb), 0) for lb in LUM)
        print(f"   {sb:>13} total: {tot:5d} ({100.0*tot/n:5.1f}%)")

# ---- SDR default preset ----
tr_sdr = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=8)
table("SDR raw ramp (MHC foundation)", build_ramp_set(PS, tr_sdr), tr_sdr, False)
table("SDR volumetric (tube, no projection)", build_volumetric_set(PS, tr_sdr), tr_sdr, False)
table("SDR verify", build_verify_set(PS, tr_sdr), tr_sdr, False)
table("SDR neutral refine", build_neutral_set(PS, tr_sdr), tr_sdr, False)

# ---- HDR default preset, 1600-nit peak cap ----
tr_hdr = Transfer.pq(bit_depth=10)
cap = tr_hdr.nits_to_cv(1600.0)
table("HDR raw ramp (peak-capped)", build_ramp_set(PS, tr_hdr, max_cv=cap), tr_hdr, True)
table("HDR volumetric (no gamut projection)", build_volumetric_set(PS, tr_hdr, max_cv=cap), tr_hdr, True)

# gamut-aware volumetric with a P3-ish panel on a Rec.2020 target
from dlc.engine.model import Target
native = {"R": [0.680, 0.320], "G": [0.265, 0.690], "B": [0.150, 0.060]}
vol_aware = build_volumetric_set(PS, tr_hdr, max_cv=cap,
                                 target=Target.hdr_rec2020_pq(),
                                 reachable_primaries=native)
table("HDR volumetric (gamut-aware, P3-ish panel)", vol_aware, tr_hdr, True)
table("HDR verify (uncapped hues)", build_verify_set(PS, tr_hdr, max_cv=cap), tr_hdr, True)
table("HDR neutral refine", build_neutral_set(PS, tr_hdr, max_cv=cap), tr_hdr, True)

# ---- alternate volumetric modes: does neutral/low-light density survive? ----
for mode in ("cube", "gamut"):
    ps2 = PatchSizes(volumetric_mode=mode)
    table(f"SDR volumetric ({mode} mode)", build_volumetric_set(ps2, tr_sdr), tr_sdr, False)

# flow totals
for flow in ("full", "mhc-only", "3dlut-only"):
    print(f"\nflow {flow}: SDR {flow_patch_counts(flow, PS, tr_sdr)['stages']}",
          f"HDR {flow_patch_counts(flow, PS, tr_hdr, max_cv=cap)['stages']}")
