"""ArgyllCMS wrappers."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class Instrument:
    port: int
    description: str


@dataclass(frozen=True)
class SpotreadRequest:
    port: int
    output_sp: Path | None = None
    logfile: Path | None = None
    emissive: bool = True
    yxy: bool = True
    skip_calibration: bool = True
    high_res: bool = False
    display_type: str | None = None
    ccmx_or_ccss: Path | None = None


class Argyll:
    """Small wrapper around ArgyllCMS command line tools."""

    def __init__(self, spotread: Path) -> None:
        self.spotread = spotread

    def enumerate_instruments(self) -> list[Instrument]:
        """Return instruments reported by `spotread -?`.

        Argyll prints the attached USB/serial instruments in its usage text.
        The exact wording varies between versions and instruments, so this
        parser intentionally accepts a few common shapes.
        """

        result = subprocess.run(
            [str(self.spotread), "-?"],
            text=True,
            capture_output=True,
            check=False,
        )
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        return parse_spotread_instruments(text)

    def spotread_command(self, request: SpotreadRequest) -> list[str]:
        cmd = [str(self.spotread), "-c", str(request.port)]
        if request.emissive:
            cmd.append("-e")
        if request.yxy:
            cmd.append("-x")
        if request.high_res:
            cmd.append("-H")
        if request.display_type:
            cmd.extend(["-y", request.display_type])
        if request.ccmx_or_ccss:
            cmd.extend(["-X", str(request.ccmx_or_ccss)])
        if request.skip_calibration:
            cmd.append("-N")

        if request.output_sp:
            cmd.extend(["-O", str(request.output_sp)])
            if request.logfile:
                cmd.append(str(request.logfile))
        else:
            cmd.append("-O")
            if request.logfile:
                cmd.append(str(request.logfile))
        return cmd

    def run_spotread_once(self, request: SpotreadRequest, timeout_seconds: int = 180) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.spotread_command(request),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )


def parse_spotread_instruments(text: str) -> list[Instrument]:
    instruments: list[Instrument] = []
    seen: set[int] = set()

    patterns = [
        re.compile(r"^\s*(\d+)\s*=\s*(.+?)\s*$"),
        re.compile(r"^\s*port\s+(\d+)\s*[:=-]\s*(.+?)\s*$", re.IGNORECASE),
        re.compile(r"^\s*(\d+)\)\s*(.+?)\s*$"),
    ]

    for line in text.splitlines():
        lowered = line.lower()
        if not any(token in lowered for token in ["i1", "color", "munki", "display", "spectro", "x-rite", "xrite"]):
            continue
        for pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue
            port = int(match.group(1))
            if port in seen:
                break
            desc = match.group(2).strip()
            instruments.append(Instrument(port=port, description=desc))
            seen.add(port)
            break

    return instruments


FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def parse_yxy(text: str) -> tuple[float, float, float] | None:
    match = re.search(rf"Yxy:\s*({FLOAT_RE})\s+({FLOAT_RE})\s+({FLOAT_RE})", text)
    if not match:
        return None
    return tuple(float(part) for part in match.groups())  # type: ignore[return-value]


def parse_xyz(text: str) -> tuple[float, float, float] | None:
    match = re.search(rf"XYZ:\s*({FLOAT_RE})[,\s]+({FLOAT_RE})[,\s]+({FLOAT_RE})", text)
    if not match:
        return None
    return tuple(float(part) for part in match.groups())  # type: ignore[return-value]


def command_for_log(command: Sequence[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])

