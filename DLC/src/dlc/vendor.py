"""Copy third-party calibration tools into the DLC workspace."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import THIRD_PARTY_DIR, dogegen_path
from .tools import FALLBACK_ARGYLL_BIN, FALLBACK_DOGEGEN


MISSING_ARGYLL_SOURCE = Path("__DLC_ARGYLL_BIN_not_configured__")
MISSING_DOGEGEN_SOURCE = Path("__DLC_DOGEGEN_not_configured__")
ARGYLL_SOURCE_ROOT = FALLBACK_ARGYLL_BIN.parent if FALLBACK_ARGYLL_BIN is not None else MISSING_ARGYLL_SOURCE
ARGYLL_DEST_ROOT = THIRD_PARTY_DIR / "argyll" / "3.3.0"
VENDOR_MANIFEST_PATH = THIRD_PARTY_DIR / "vendor_manifest.json"


@dataclass(frozen=True)
class VendorItem:
    name: str
    source: Path
    destination: Path
    kind: str
    source_exists: bool
    destination_exists: bool
    action: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "source": str(self.source),
            "destination": str(self.destination),
            "kind": self.kind,
            "source_exists": self.source_exists,
            "destination_exists": self.destination_exists,
            "action": self.action,
        }


def _hash_file(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size": path.stat().st_size,
    }


def _destination_files(item: VendorItem) -> list[dict[str, Any]]:
    if not item.destination.exists():
        return []
    if item.destination.is_file():
        return [_hash_file(item.destination)]
    files = []
    for path in sorted(item.destination.rglob("*")):
        if path.is_file():
            record = _hash_file(path)
            record["relative_path"] = str(path.relative_to(item.destination))
            files.append(record)
    return files


def _item(name: str, source: Path, destination: Path, kind: str, overwrite: bool) -> VendorItem:
    source_exists = source.exists()
    destination_exists = destination.exists()
    if not source_exists:
        action = "missing-source"
    elif destination_exists and not overwrite:
        action = "skip-existing"
    elif destination_exists and overwrite:
        action = "replace"
    else:
        action = "copy"
    return VendorItem(name, source, destination, kind, source_exists, destination_exists, action)


def plan_vendor_tools(
    *,
    argyll_source: Path = ARGYLL_SOURCE_ROOT,
    dogegen_source: Path = FALLBACK_DOGEGEN or MISSING_DOGEGEN_SOURCE,
    overwrite: bool = False,
) -> list[VendorItem]:
    return [
        _item("argyll", argyll_source, ARGYLL_DEST_ROOT, "directory", overwrite),
        _item("dogegen", dogegen_source, dogegen_path(), "file", overwrite),
    ]


def contained_vendor_tools() -> list[VendorItem]:
    items = []
    for name, destination, kind in [
        ("argyll", ARGYLL_DEST_ROOT, "directory"),
        ("dogegen", dogegen_path(), "file"),
    ]:
        destination_exists = destination.exists()
        items.append(
            VendorItem(
                name=name,
                source=destination,
                destination=destination,
                kind=kind,
                source_exists=destination_exists,
                destination_exists=destination_exists,
                action="record-existing" if destination_exists else "missing-contained",
            )
        )
    return items


def build_vendor_manifest(items: list[VendorItem], *, copied: bool) -> dict[str, Any]:
    item_records = []
    for item in items:
        files = _destination_files(item)
        item_records.append(item.as_dict() | {"file_count": len(files), "files": files})
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "copied": copied,
        "third_party_dir": str(THIRD_PARTY_DIR),
        "items": item_records,
    }


def write_vendor_manifest(items: list[VendorItem], *, copied: bool, output: Path | None = None) -> Path:
    target = output or VENDOR_MANIFEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_vendor_manifest(items, copied=copied), indent=2), encoding="utf-8")
    return target


def vendor_manifest_status(path: Path | None = None) -> dict[str, Any]:
    target = path or VENDOR_MANIFEST_PATH
    if not target.exists():
        return {
            "ok": False,
            "path": str(target),
            "exists": False,
            "reason": "vendor manifest has not been written",
        }
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "path": str(target),
            "exists": True,
            "reason": f"vendor manifest is unreadable: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "path": str(target),
            "exists": True,
            "reason": "vendor manifest is not a JSON object",
        }
    items = payload.get("items")
    item_records = items if isinstance(items, list) else []
    file_count = 0
    missing_fingerprints: list[str] = []
    for item in item_records:
        if not isinstance(item, dict):
            continue
        files = item.get("files")
        file_records = files if isinstance(files, list) else []
        file_count += len(file_records)
        for file_record in file_records:
            if not isinstance(file_record, dict):
                continue
            sha = file_record.get("sha256")
            if not isinstance(sha, str) or len(sha) != 64:
                missing_fingerprints.append(str(file_record.get("path") or file_record.get("relative_path") or "unknown"))
    ok = bool(item_records) and file_count > 0 and not missing_fingerprints
    return {
        "ok": ok,
        "path": str(target),
        "exists": True,
        "reason": "vendor manifest is present with file fingerprints" if ok else "vendor manifest is incomplete",
        "generated_at": payload.get("generated_at"),
        "copied": payload.get("copied"),
        "item_count": len(item_records),
        "file_count": file_count,
        "missing_fingerprints": missing_fingerprints,
    }


def copy_vendor_tools(items: list[VendorItem]) -> list[VendorItem]:
    completed: list[VendorItem] = []
    for item in items:
        if item.action == "missing-source":
            completed.append(item)
            continue
        if item.action == "skip-existing":
            completed.append(item)
            continue
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        if item.destination.exists():
            if item.destination.is_dir():
                shutil.rmtree(item.destination)
            else:
                item.destination.unlink()
        if item.kind == "directory":
            shutil.copytree(item.source, item.destination)
        else:
            shutil.copy2(item.source, item.destination)
        completed.append(
            VendorItem(
                name=item.name,
                source=item.source,
                destination=item.destination,
                kind=item.kind,
                source_exists=item.source.exists(),
                destination_exists=item.destination.exists(),
                action="copied",
            )
        )
    return completed

