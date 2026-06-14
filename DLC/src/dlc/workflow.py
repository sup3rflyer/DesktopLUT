"""Workflow descriptions and stage names."""

from __future__ import annotations


UNATTENDED_STAGES = [
    "preflight",
    "snapshot_desktoplut",
    "optional_spectro_reference",
    "colorimeter_ready",
    "raw_measurement",
    "build_mhc_baseline",
    "apply_mhc_baseline",
    "mhc_verification_loop",
    "post_mhc_measurement",
    "build_3dlut",
    "apply_3dlut",
    "3dlut_verification_loop",
    "verification",
    "final_report",
]


def describe_unattended_pipeline(mode: str) -> str:
    mode = mode.upper()
    lines = [
        f"DesktopLUT Calibrator unattended {mode} pipeline",
        "=" * 52,
        "",
        "Human setup:",
        "  1. Optional: place spectrometer for probe matching.",
        "  2. Place colorimeter at screen center.",
        "",
        "Automated stages:",
    ]
    for index, stage in enumerate(UNATTENDED_STAGES, start=1):
        lines.append(f"  {index:02d}. {stage}")
    lines.extend(
        [
            "",
            "ColourSpace is not required in the primary path.",
            "MHC and 3D LUT phases are iterative loops with metric-based stop decisions.",
            "Agent supervision uses manifest.json plus events.jsonl.",
            "DesktopLUT is controlled through the local API boundary.",
            "ArgyllCMS is the initial instrument layer.",
        ]
    )
    return "\n".join(lines)

