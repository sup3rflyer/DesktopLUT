"""The command line of the DIP knowledge layer (stage S2): ``python -m dlc.knowledge <command>``.

``show | add | amend | add-observation | set-status | match | validate | render``. Every write goes through
``KnowledgeStore.apply_ops`` (validation, append-only, lock + CAS, the view re-rendered); evidence paths are hashed
when written and must exist; ``--display`` takes a DLC display name, a DIP key (``name:MODE``), a unit id, an EDID id
or a device path. ``match`` validates an observation context; the matchers arrive in S3.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from .knowledge import (MODES, STATUSES, KnowledgeError, KnowledgeStore, _entry, _norm_name, evidence_file, render,
                        sha256_dir, sha256_file, unit_from_header, validate_observation_context)
from .paths import atomic_write_text

def _read_json_file(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KnowledgeError(f"{path}: not readable JSON ({exc})") from exc


def _store_from_args(args: Any) -> KnowledgeStore:
    if args.dir:
        return KnowledgeStore(args.dir, base_dir=args.base_dir)
    return KnowledgeStore.beside(args.dip)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    from .paths import PROJECT_DIR
    ap = argparse.ArgumentParser(prog="python -m dlc.knowledge",
                                 description="DIP knowledge: what DLC knows about each physical display. Read the "
                                             "view ('show') before judging an anomaly on a display.")
    ap.add_argument("--dip", default=str(PROJECT_DIR / "dip_store.json"),
                    help="the DIP store; the knowledge folder is dip_knowledge/ beside it (default: DLC's)")
    ap.add_argument("--dir", default=None, help="the knowledge folder itself (overrides --dip)")
    ap.add_argument("--base-dir", default=None, help="where relative evidence paths resolve (default: the "
                                                     "folder's parent)")
    ap.add_argument("--today", default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def disp(p: Any, required: bool = True) -> None:
        p.add_argument("--display", required=required, help="DLC display name, DIP key, unit id, EDID id or "
                                                            "device path")

    def who(p: Any) -> None:
        p.add_argument("--by", required=True, help="'owner', 'flow:<name>' or 'llm:<who>'")
        p.add_argument("--why", required=True)
        p.add_argument("--base-rev", type=int, default=None, help="the rev your decision was based on (CAS)")

    p = sub.add_parser("show", help="print the rendered view")
    disp(p)
    p.add_argument("--mode", choices=MODES)
    p.add_argument("--id")
    p = sub.add_parser("add", help="create entries from a JSON file {by, why, unit?, entries: [...]}")
    disp(p, required=False)
    p.add_argument("--file", required=True)
    p.add_argument("--create-unit", action="store_true", help="create the unit from the file's 'unit' header")
    p.add_argument("--allow-second-unit", default=None, metavar="REASON",
                   help="create a unit that shares a name, device path or hardware id with an existing one, saying why "
                        "it is another physical display")
    p.add_argument("--base-rev", type=int, default=None)
    p = sub.add_parser("amend", help="apply ops from a JSON file {by?, why?, ops: [...]} as one write")
    disp(p)
    p.add_argument("--file", required=True)
    p.add_argument("--base-rev", type=int, default=None)
    p = sub.add_parser("add-observation", help="append one observation (a JSON file) to an entry")
    disp(p)
    p.add_argument("--id", required=True)
    p.add_argument("--file", required=True)
    who(p)
    p = sub.add_parser("set-status", help="change an entry's status (with a reason)")
    disp(p)
    p.add_argument("--id", required=True)
    p.add_argument("--status", required=True, choices=STATUSES)
    p.add_argument("--successor", default=None, help="for superseded: the entry that replaces it")
    who(p)
    p = sub.add_parser("match", help="dry run: which entries explain / resemble an observation context")
    disp(p)
    p.add_argument("--file", required=True)
    p = sub.add_parser("validate", help="validate every unit file (or one); --evidence re-hashes evidence files")
    disp(p, required=False)
    p.add_argument("--evidence", action="store_true")
    p = sub.add_parser("render", help="regenerate the .md views")
    disp(p, required=False)
    args = ap.parse_args(argv)
    store = _store_from_args(args)
    try:
        rc = _run(args, store)
    except KnowledgeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        rc = EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"CRASHED (a defect of dlc.knowledge, not of your input): {type(exc).__name__}: {exc}\n"
              f"{traceback.format_exc(limit=4)}", file=sys.stderr)
        return EXIT_CRASHED
    for uid, why in sorted(store.broken.items()):
        print(f"warning: {store.path(uid)} is broken and was skipped: {why}", file=sys.stderr)
    return rc


# exit codes: 0 done; 1 invalid files found by validate; 2 refused (a bad op, name or file); 3 crashed (a defect)
EXIT_OK, EXIT_INVALID, EXIT_REFUSED, EXIT_CRASHED = 0, 1, 2, 3


def _run(args: Any, store: KnowledgeStore) -> int:
    today = args.today
    if args.cmd == "show":
        uid = store.resolve(args.display)
        doc = store.read_raw(uid)
        print(render(doc, source=store.path(uid).name, today=today, mode=args.mode, only=args.id), end="")
        return 0
    if args.cmd == "add":
        payload = _read_json_file(args.file)
        if not isinstance(payload, dict):
            raise KnowledgeError("add: the file holds {by, why, unit?, entries: [...]}")
        bad = sorted(set(payload) - {"by", "why", "unit", "entries", "unit_ops"})
        if bad:
            raise KnowledgeError(f"add: unknown keys {bad}")
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise KnowledgeError("add: 'entries' must be a non-empty list")
        by, why = payload.get("by"), payload.get("why")
        create = None
        if args.create_unit:
            if not payload.get("unit"):
                raise KnowledgeError("add --create-unit: the file needs a 'unit' header")
            create = unit_from_header(payload["unit"])
            uid = create["unit_id"]
            if store.path(uid).exists():
                create = None          # it exists: --create-unit is a no-op, the entries go into it
        else:
            if not args.display:
                raise KnowledgeError("add: give --display (or --create-unit with a unit header)")
            uid = store.resolve(args.display)
        if args.display and args.create_unit:
            # the given name must be one of the header's names (no silent write to another unit)
            hdr = payload["unit"]
            names = {_norm_name(a) for a in hdr.get("aliases") or []} | {_norm_name(uid)}
            if _norm_name(args.display) not in names:
                raise KnowledgeError(f"--display {args.display!r} is not a name of the unit in the file ({uid})")
        ops = [dict(o, **{k: v for k, v in (("by", by), ("why", why)) if k not in o})
               for o in (payload.get("unit_ops") or [])]
        for e in entries:
            if not isinstance(e, dict):
                raise KnowledgeError("add: each entry must be an object")
            e = dict(e)
            op = {"op": "create_entry", "by": e.pop("by", by), "why": e.pop("why", why), "entry": e}
            if "id_prefix" in e:
                op["id_prefix"] = e.pop("id_prefix")
            ops.append(op)
        doc = store.apply_ops(uid, ops, base_rev=args.base_rev, create=create, today=today,
                              allow_second_unit=args.allow_second_unit)
        print(f"written {store.path(uid)} rev {doc['rev']} ({len(entries)} entries); view {store.md_path(uid)}")
        return 0
    if args.cmd == "amend":
        payload = _read_json_file(args.file)
        if not isinstance(payload, dict) or not isinstance(payload.get("ops"), list):
            raise KnowledgeError("amend: the file holds {by?, why?, ops: [...]}")
        bad = sorted(set(payload) - {"by", "why", "ops"})
        if bad:
            raise KnowledgeError(f"amend: unknown keys {bad}")
        uid = store.resolve(args.display)
        ops = [dict(o, **{k: v for k, v in (("by", payload.get("by")), ("why", payload.get("why")))
                          if k not in o and v is not None}) for o in payload["ops"]]
        doc = store.apply_ops(uid, ops, base_rev=args.base_rev, today=today)
        print(f"written {store.path(uid)} rev {doc['rev']} ({len(ops)} ops); view {store.md_path(uid)}")
        return 0
    if args.cmd == "add-observation":
        uid = store.resolve(args.display)
        obs = _read_json_file(args.file)
        doc = store.apply_ops(uid, [{"op": "add_observation", "id": args.id, "observation": obs, "by": args.by,
                                     "why": args.why}], base_rev=args.base_rev, today=today)
        e = _entry(doc, args.id)
        print(f"written {store.path(uid)} rev {doc['rev']}: {args.id} now has {len(e['observations'])} "
              f"observations in full")
        return 0
    if args.cmd == "set-status":
        uid = store.resolve(args.display)
        op = {"op": "set_status", "id": args.id, "status": args.status, "by": args.by, "why": args.why}
        if args.successor:
            op["successor"] = args.successor
        doc = store.apply_ops(uid, [op], base_rev=args.base_rev, today=today)
        print(f"written {store.path(uid)} rev {doc['rev']}: {args.id} -> {args.status}")
        return 0
    if args.cmd == "match":
        uid = store.resolve(args.display)
        store.read_raw(uid)
        ctx = _read_json_file(args.file)
        errs = validate_observation_context(ctx)
        if errs:
            raise KnowledgeError("match: the observation context is not valid", errs)
        print(f"observation context valid ({len(ctx['observations'])} observations, unit {uid}). "
              f"Matchers arrive in S3: no verdict yet; read the view ('show') and judge.")
        return 0
    if args.cmd == "validate":
        uids = [store.resolve(args.display)] if args.display else store.unit_ids()
        bad = 0
        for uid in uids:
            try:
                doc = store.read_raw(uid)
            except KnowledgeError as exc:
                print(f"INVALID {uid}: {exc}")
                bad += 1
                continue
            problems = _evidence_problems(doc, store.base_dir) if args.evidence else []
            for pr in problems:
                print(f"EVIDENCE {uid}: {pr}")
            print(f"ok {uid} rev {doc['rev']}: {len(doc['entries'])} entries"
                  + (f", {len(problems)} evidence problem(s)" if problems else ""))
            bad += 1 if problems else 0
        if not uids:
            print(f"no unit files in {store.root}")
        return EXIT_INVALID if bad else EXIT_OK
    if args.cmd == "render":
        if args.display:
            uid = store.resolve(args.display)
            doc = store.read_raw(uid)
            atomic_write_text(store.md_path(uid), render(doc, source=store.path(uid).name, today=today))
            print(store.md_path(uid))
        else:
            for p in store.render_all(today=today):
                print(p)
        return 0
    raise KnowledgeError(f"unknown command {args.cmd}")


def _evidence_problems(doc: dict, base_dir: Optional[Path]) -> list[str]:
    out = []
    for e in doc.get("entries") or []:
        for o in e.get("observations") or []:
            src = o.get("source") or {}
            if not src.get("path"):
                continue
            p = evidence_file(src["path"], base_dir)
            if not p.exists():
                out.append(f"{e['id']} {o['id']}: {src['path']} is gone")
                continue
            now = sha256_file(p) if p.is_file() else sha256_dir(p)
            if now != src.get("sha256"):
                out.append(f"{e['id']} {o['id']}: {src['path']} changed since it was recorded")
    return out


if __name__ == "__main__":
    raise SystemExit(_main())
