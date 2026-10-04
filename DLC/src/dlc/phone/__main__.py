"""``python -m dlc.phone state | capture LABEL SECONDS [--fps N] [--bits N] [--out DIR]``"""

from __future__ import annotations

import argparse
import json
import sys

from .adb import Adb
from .mcpro import Mcpro
from .session import PhoneRig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dlc.phone")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state", help="print the app + camera-service state as JSON")
    c = sub.add_parser("capture", help="record a verified clip")
    c.add_argument("label")
    c.add_argument("seconds", type=float)
    c.add_argument("--fps", type=float)
    c.add_argument("--bits", type=int)
    c.add_argument("--out", default="results/_phone_captures")
    a = ap.parse_args(argv)
    if a.cmd == "state":
        cam = Mcpro(Adb())
        cam.ensure_running()
        print(json.dumps(cam.state().as_dict(), indent=1))
        return 0
    expect = {k: v for k, v in (("fps", a.fps), ("bits", a.bits)) if v}
    cap = PhoneRig(a.out).capture(a.label, a.seconds, expect=expect)
    print(cap.info.summary())
    print("OK" if cap.ok else "PROBLEMS: " + "; ".join(cap.problems))
    print(cap.local)
    return 0 if cap.ok else 2


if __name__ == "__main__":
    sys.exit(main())
