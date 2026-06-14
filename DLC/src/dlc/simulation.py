"""Synthetic artifacts for no-hardware unattended pipeline rehearsals."""

from __future__ import annotations

from pathlib import Path


SRGB_TO_XYZ_D65 = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)


def _target_xyz(rgb: tuple[float, float, float], *, luminance: float = 100.0, gamma: float = 2.2) -> tuple[float, float, float]:
    linear = tuple(max(0.0, min(1.0, channel)) ** gamma for channel in rgb)
    return tuple(luminance * sum(row[i] * linear[i] for i in range(3)) for row in SRGB_TO_XYZ_D65)  # type: ignore[return-value]


def write_synthetic_ti3(path: Path, *, luminance: float = 100.0, gamma: float = 2.2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rgb_rows = [
        (0.0, 0.0, 0.0),
        (0.25, 0.25, 0.25),
        (0.5, 0.5, 0.5),
        (0.75, 0.75, 0.75),
        (1.0, 1.0, 1.0),
        (0.25, 0.0, 0.0),
        (0.5, 0.0, 0.0),
        (0.75, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.25, 0.0),
        (0.0, 0.5, 0.0),
        (0.0, 0.75, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 0.25),
        (0.0, 0.0, 0.5),
        (0.0, 0.0, 0.75),
        (0.0, 0.0, 1.0),
        (1.0, 1.0, 0.0),
        (1.0, 0.0, 1.0),
        (0.0, 1.0, 1.0),
    ]
    rows = []
    for rgb in rgb_rows:
        xyz = _target_xyz(rgb, luminance=luminance, gamma=gamma)
        rows.append(
            " ".join(
                [
                    f"{rgb[0] * 100:.6f}",
                    f"{rgb[1] * 100:.6f}",
                    f"{rgb[2] * 100:.6f}",
                    f"{xyz[0]:.6f}",
                    f"{xyz[1]:.6f}",
                    f"{xyz[2]:.6f}",
                ]
            )
        )
    path.write_text(
        "\n".join(
            [
                "CTI3",
                "# Synthetic DesktopLUT Calibrator rehearsal measurement",
                "BEGIN_DATA_FORMAT",
                "RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z",
                "END_DATA_FORMAT",
                f"NUMBER_OF_SETS {len(rows)}",
                "BEGIN_DATA",
                *rows,
                "END_DATA",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def write_placeholder_icc(path: Path, *, description: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Synthetic ICC placeholder for {description}\n", encoding="utf-8")
    return path


def write_placeholder_ti1(path: Path, *, description: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Synthetic TI1 placeholder for {description}\n", encoding="utf-8")
    return path


def write_identity_cube(path: Path, *, size: int = 17, title: str = "DLC synthetic identity") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    denominator = max(1, size - 1)
    lines = [
        f'TITLE "{title}"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    for r in range(size):
        for g in range(size):
            for b in range(size):
                lines.append(f"{r / denominator:.8f} {g / denominator:.8f} {b / denominator:.8f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

