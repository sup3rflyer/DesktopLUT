"""Dogegen patch display wrapper."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


RGBW_SDR = {
    "red": (242, 0, 0),
    "green": (0, 242, 0),
    "blue": (0, 0, 242),
    "white": (242, 242, 242),
}

RGBW_HDR = {
    "red": (712, 0, 0),
    "green": (0, 712, 0),
    "blue": (0, 0, 712),
    "white": (712, 712, 712),
}


@dataclass
class DogegenPatchDisplay:
    executable: Path
    mode: str

    @property
    def startup_mode(self) -> str:
        return "mode 8" if self.mode.upper() == "SDR" else "mode 10_hdr"

    def rgbw_commands(self, patch_size: int = 100) -> list[str]:
        patches = RGBW_SDR if self.mode.upper() == "SDR" else RGBW_HDR
        commands = [self.startup_mode]
        commands.extend(
            f"window {patch_size} {r} {g} {b}"
            for r, g, b in patches.values()
        )
        commands.append("quit")
        return commands

    def start(self) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [str(self.executable), self.startup_mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        time.sleep(1.0)
        return proc

    @staticmethod
    def send(proc: subprocess.Popen[str], command: str, settle_seconds: float = 0.3) -> None:
        if proc.stdin is None:
            raise RuntimeError("Dogegen stdin is not available")
        proc.stdin.write(command + "\n")
        proc.stdin.flush()
        time.sleep(settle_seconds)

    @classmethod
    def send_many(cls, proc: subprocess.Popen[str], commands: Iterable[str], settle_seconds: float = 0.3) -> None:
        for command in commands:
            cls.send(proc, command, settle_seconds)


