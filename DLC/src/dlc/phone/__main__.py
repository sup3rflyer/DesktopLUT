"""``python -m dlc.phone`` - drive the measurement phone from a shell (JSON in/out, built for the LLM).

    status                                   device, storage, which backends are usable, current camera state
    set      [settings]                      apply + verify against the camera HAL, print what the camera reports
    capture  LABEL SECONDS [settings]        fixed-length verified clip
    rec start LABEL [settings]               start a clip (state kept in <out>/.rec_active.json) ...
    rec mark  NAME [key=value ...]           ... timestamp an event against it (host time + clip timecode) ...
    rec stop                                 ... stop, pull, verify, write <label>.json
    rec status                               is a recording active?

settings: --fps N  --size WxH  --codec h264|hevc  --iso N  --shutter 1/120  --wb K  --tint N  --focus 0..1
          --lens main|uw|tele|<id>  --backend blackmagic|mcpro      (fps <= 60 -> Blackmagic REST, above -> mcpro)
Exit status: 0 ok, 2 clip verified with PROBLEMS (see JSON), 1 error (JSON {"error": ...} on stdout).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .session import PhoneRig
from .settings import BackendError, parse_shutter, parse_size

DEFAULT_OUT = "results/_phone_captures"


def _add_settings(p: argparse.ArgumentParser) -> None:
    p.add_argument("--fps", type=float)
    p.add_argument("--size", type=parse_size)
    p.add_argument("--codec", choices=["h264", "hevc"])
    p.add_argument("--iso", type=int)
    p.add_argument("--shutter", type=parse_shutter, help="exposure time, e.g. 1/120 or 0.008333")
    p.add_argument("--wb", type=int, dest="wb_k", help="white balance, Kelvin")
    p.add_argument("--tint", type=int)
    p.add_argument("--focus", type=float, help="0..1 (near..far); also switches autofocus off")
    p.add_argument("--lens", help="main | uw | tele | camera id")
    p.add_argument("--backend", choices=["blackmagic", "mcpro"])
    p.add_argument("--out", default=DEFAULT_OUT)


def _settings(a: argparse.Namespace) -> dict:
    kw = dict(fps=a.fps, size=a.size, codec=a.codec, iso=a.iso, shutter_s=a.shutter, wb_k=a.wb_k, tint=a.tint,
              focus=a.focus, lens=a.lens)
    return {k: v for k, v in kw.items() if v is not None}


def _rig(a: argparse.Namespace) -> PhoneRig:
    rig = PhoneRig(a.out)
    if getattr(a, "backend", None):
        rig.use(a.backend)
    return rig


def _out(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def _capture_json(cap) -> dict:
    return dict(ok=cap.ok, problems=cap.problems, clip=str(cap.local), manifest=str(cap.manifest),
                summary=cap.info.summary(), marks=[dict(label=m.label, clip_s=m.clip_s, t_host=m.t_host)
                                                   for m in cap.marks])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dlc.phone", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "set"):
        _add_settings(sub.add_parser(name))
    c = sub.add_parser("capture")
    c.add_argument("label")
    c.add_argument("seconds", type=float)
    _add_settings(c)
    r = sub.add_parser("rec")
    rs = r.add_subparsers(dest="rec_cmd", required=True)
    st = rs.add_parser("start")
    st.add_argument("label")
    _add_settings(st)
    mk = rs.add_parser("mark")
    mk.add_argument("name")
    mk.add_argument("data", nargs="*", help="key=value pairs stored with the mark")
    mk.add_argument("--out", default=DEFAULT_OUT)
    for n in ("stop", "status"):
        rs.add_parser(n).add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "status":
            _out(_rig(a).status())
            return 0
        if a.cmd == "set":
            _out(_rig(a).set(**_settings(a)))
            return 0
        if a.cmd == "capture":
            cap = _rig(a).capture(a.label, a.seconds, **_settings(a))
            _out(_capture_json(cap))
            return 0 if cap.ok else 2
        state_file = Path(a.out) / ".rec_active.json"
        if a.rec_cmd == "start":
            if state_file.exists():
                raise BackendError(f"{state_file} exists - a recording may be running; 'rec stop' it (or delete the file)")
            rig = _rig(a)
            rec = rig.begin(a.label, **_settings(a))
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(rec.to_json()), encoding="utf-8")
            _out(dict(recording=True, label=a.label, backend=rec.backend.name, state=rec.state))
            return 0
        if a.rec_cmd == "status":
            _out(json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else dict(recording=False))
            return 0
        if not state_file.exists():
            raise BackendError(f"no active recording ({state_file} missing)")
        rig = PhoneRig(a.out)
        rec = rig.resume(json.loads(state_file.read_text(encoding="utf-8")))
        if a.rec_cmd == "mark":
            data = dict(kv.split("=", 1) for kv in a.data)
            m = rec.mark(a.name, **data)
            state_file.write_text(json.dumps(rec.to_json()), encoding="utf-8")
            _out(dict(label=m.label, clip_s=m.clip_s, t_host=m.t_host))
            return 0
        cap = rig.end(rec)                                  # rec stop
        state_file.unlink()
        _out(_capture_json(cap))
        return 0 if cap.ok else 2
    except BackendError as e:
        _out(dict(error=type(e).__name__, message=str(e)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
