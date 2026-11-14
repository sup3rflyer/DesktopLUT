"""DIP knowledge: a living per-display record of what the panel and the rig are (stages S1 + S2).

One JSON file per PHYSICAL display unit, ``<profile dir>/dip_knowledge/<unit_id>.json``, beside ``dip_store.json``
(and, like it, local-only), plus a generated ``<unit_id>.md`` view that a person or an LLM reads first. The DIP's
measured sections stay the data; an entry here is the MEANING: a plain-language statement about the panel, the rig
or an unexplained event, the conditions it holds under (``scope``, with what was NOT tested in ``scope.unknown``), a
status, an append-only list of observations with their evidence files (path + sha256), and optionally a machine-
checkable ``signature`` (how a later observation is matched; matchers arrive in S3) and ``facets`` (what a flow
reads before it runs; bounds, never correction values).

Rules this module enforces (the validator names every violation; nothing is written that fails it):
- closed vocabularies: entry kind, status, observation verdict, signature type + parameters, facet name +
  parameters, scope keys, link relations, op names; unknown keys are refused everywhere;
- observations, revisions, status history, unit events and unit revisions are append-only (checked against the
  file on disk at every write); a statement / title / scope edit keeps the old value in its revision;
- older observations fold into ``rollup`` only when their verdict is ``matches`` or ``consistent``: contradicting,
  refining, establishing and inconclusive observations are never folded;
- no per-code table anywhere: no numeric array longer than 16, no number-keyed map outside
  ``observations[].numbers`` (and there at most 16 keys); facet arrays at most 4 numbers (ranges, not tables);
- a measured entry needs observations with numbers, at least one hashed evidence file, and a non-empty
  ``scope.unknown``; two measured entries with the same signature type and overlapping scope need an explicit
  ``refines`` / ``supersedes`` / ``superseded_by`` / ``contradicts`` link;
- evidence paths must exist when the observation is written; their sha256 is stored then;
- a file that fails validation on disk is never overwritten; writes take ``<file>.lock`` and compare-and-swap on
  ``rev`` (one reload + re-apply, then refuse);
- 1 MB hard limit per unit file (256 KB soft: a warning), at most 60 entries with status measured / inferred /
  hypothesis.

Excuse power is never a gate: whether an entry may *explain* an observation (``entry_state(...)["can_explain"]``)
is evidence for the overseeing LLM, nothing here changes a recommendation. Stdlib only, like ``dip.py``.

CLI: ``python -m dlc.knowledge show | add | amend | add-observation | set-status | match | validate | render``.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .file_lock import FileLock
from .paths import atomic_write_text, read_text_shared

SCHEMA = 1
DIR_NAME = "dip_knowledge"
HARD_LIMIT_BYTES = 1024 * 1024
SOFT_LIMIT_BYTES = 256 * 1024
MAX_ACTIVE_ENTRIES = 60
MAX_FULL_OBSERVATIONS = 40
MAX_NUMERIC_ARRAY = 16
MAX_FACET_ARRAY = 4
TITLE_MAX = 90
STATEMENT_MAX = 1200
DEFAULT_MAX_AGE_DAYS = 180          # the DIP's own staleness clock (dip.py DisplayInstrumentProfile.is_stale)
DORMANT_DAYS = 90

# ----------------------------------------------------------------------------- closed vocabularies
KINDS = ("panel", "rig", "event")
# id prefix per kind: F panel, R rig, E event, H a mechanism hypothesis (panel or rig) — the note's §2.3
ID_PREFIXES = {"panel": ("F", "H"), "rig": ("R", "H"), "event": ("E",)}
STATUSES = ("measured", "inferred", "hypothesis", "refuted", "superseded")
ACTIVE_STATUSES = ("measured", "inferred", "hypothesis")
VERDICTS = ("establishes", "consistent", "matches", "contradicts", "refines", "inconclusive")
FOLDABLE_VERDICTS = ("matches", "consistent")
CONFIRMING_VERDICTS = ("establishes", "consistent", "matches", "refines")
MODES = ("SDR", "HDR", "ACM")
DOMAINS = ("sdr_drive_code8", "sdr_drive_code10", "sdr_link_code10", "hdr_drive_code10", "hdr_link_code10", "nits")
TRANSFERABLE = ("no", "candidate")
LINK_RELS = ("explains", "explained_by", "mechanism_hypothesis", "same_family", "refines", "refined_by",
             "supersedes", "superseded_by", "contradicts", "see_field", "related")
CONFLICT_LINK_RELS = ("refines", "refined_by", "supersedes", "superseded_by", "contradicts")
QUANTITIES = ("light", "balance")

# signature type -> (required params, optional params). Each is panel-agnostic: parameters come from the entry.
SIGNATURES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "none": (frozenset(), frozenset()),
    "code_structure": (frozenset({"domain", "code_range", "quantity", "max_abs_dev_pct"}),
                       frozenset({"period_codes", "chroma_dxy_max", "stimulus_area_pct"})),
    "level_ratio": (frozenset({"key", "pair"}),
                    frozenset({"ratio_source", "u_bound", "ratio_bounds_pct", "levels_nits", "codes"})),
    "thermal_offset": (frozenset({"sign", "max_abs"}), frozenset({"tau_min", "precondition"})),
    "session_offset": (frozenset({"max_abs_dxy"}), frozenset({"max_spread_dxy"})),
    "quantization": (frozenset({"domain", "step_pct"}), frozenset({"dither", "link_bpc"})),
    "zero_read": (frozenset(), frozenset({"floor_nits"})),
    "luminance_cap": (frozenset(), frozenset({"level_nits", "stimulus"})),
}

# facet name -> (required, optional, the consumer that reads it). A facet is a bound on a property, never a
# correction value.
FACETS: dict[str, tuple[frozenset[str], frozenset[str], str]] = {
    "grey_light_structure": (frozenset({"mode", "stimulus_area_pct", "code_range", "max_abs_dev_pct"}),
                             frozenset({"domain", "rms_coarse_pct", "period_codes", "balance"}),
                             "base 1D LUT build, grayscale-wb (stage S4)"),
    "refresh_response_key": (frozenset({"mode", "key"}), frozenset({"colour_check_codes"}),
                             "refresh-trim verify (stage S4)"),
    "thermal_load_law": (frozenset({"tau_s"}), frozenset({"exponent", "basis"}), "viewing_thermal (stage S4e)"),
    "session_offset_bound": (frozenset({"dxy"}), frozenset({"spread_dxy"}), "verify, grayscale-wb (stage S4)"),
    "link_step": (frozenset({"bpc", "pct"}), frozenset({"mode"}), "refresh-trim verify (stage S4)"),
    "sleep_timeouts": (frozenset({"display_min"}), frozenset({"app_keepawake_idle_min"}), "probe flows"),
}

UNIT_KEYS = frozenset({"schema", "rev", "unit_id", "identity", "aliases", "model", "panel", "standing_conditions",
                       "unit_events", "revisions", "entries"})
IDENTITY_KEYS = frozenset({"hardware_id", "edid_id", "device_paths"})
PANEL_KEYS = frozenset({"tech", "zones"})
STANDING_KEYS = frozenset({"setting", "value", "source", "verified"})
UNIT_EVENT_KEYS = frozenset({"date", "what", "by"})
UNIT_REVISION_KEYS = frozenset({"rev", "date", "by", "op", "why", "detail"})
ENTRY_KEYS = frozenset({"id", "kind", "title", "statement", "scope", "status", "status_history", "explained",
                        "observations", "rollup", "signature", "facets", "implications", "open_questions", "links",
                        "transferable", "last_confirmed", "created", "rev", "revisions"})
ENTRY_INPUT_KEYS = ENTRY_KEYS - {"status_history", "rollup", "rev", "revisions", "created", "last_confirmed"}
SCOPE_KEYS = frozenset({"mode", "domain", "code_range", "level_nits", "stimulus", "panel_settings", "refresh_hz",
                        "stack", "thermal", "instrument", "link_bpc", "unknown"})
STIMULUS_KEYS = frozenset({"pattern", "area_pct"})
STATUS_HISTORY_KEYS = frozenset({"status", "date", "by", "why"})
OBS_KEYS = frozenset({"id", "date", "by", "verdict", "source", "conditions", "numbers", "independent", "note"})
OBS_INPUT_KEYS = OBS_KEYS - {"id"}
SOURCE_KEYS = frozenset({"kind", "path", "sha256", "hash_of", "ref"})
ROLLUP_KEYS = frozenset({"count", "independent", "first", "last", "folded_ids", "verdicts", "numbers"})
IMPLICATION_KEYS = frozenset({"flows", "text"})
QUESTION_KEYS = frozenset({"id", "q", "test", "owner_action", "status", "answer", "closed", "closed_by"})
LINK_KEYS = frozenset({"rel", "id", "field"})
REVISION_KEYS = frozenset({"rev", "date", "by", "op", "why", "old"})

OPS = ("create_entry", "add_observation", "set_status", "edit_statement", "set_scope", "add_link",
       "add_open_question", "close_open_question", "set_facet", "set_signature", "add_implication",
       "mark_transferable", "add_unit_event", "add_alias", "set_standing_condition")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^([FREH])-(\d{4})$")
_XID_RE = re.compile(r"^[A-Za-z0-9._~-]+/[FREH]-\d{4}$")
_OBS_ID_RE = re.compile(r"^O(\d+)$")
_Q_ID_RE = re.compile(r"^Q(\d+)$")
_BY_RE = re.compile(r"^(owner(:.{1,60})?|flow:[A-Za-z0-9_.:-]{1,60}|llm:.{1,80})$")
_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{3,80}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMKEY_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


class KnowledgeError(ValueError):
    """A refused op or file: ``violations`` names every problem found."""

    def __init__(self, message: str, violations: Optional[Sequence[str]] = None) -> None:
        self.violations = list(violations or [])
        super().__init__(message + ("".join(f"\n  - {v}" for v in self.violations) if self.violations else ""))


class CorruptFile(KnowledgeError):
    """The unit file on disk does not parse or does not validate: it is never overwritten."""


class ConcurrentWrite(KnowledgeError):
    """The file changed between the read and the write twice in a row."""


class UnitNotFound(KnowledgeError):
    pass


# ----------------------------------------------------------------------------- small helpers
def today_iso() -> str:
    return _dt.date.today().isoformat()


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"),
                                                                                             float("-inf"))


def _is_date(v: Any) -> bool:
    if not isinstance(v, str) or not _DATE_RE.match(v):
        return False
    try:
        _dt.date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _range2(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v) and v[0] <= v[1]


def _unknown_keys(d: dict, allowed: Iterable[str], where: str, out: list[str]) -> None:
    for k in sorted(set(d) - set(allowed)):
        out.append(f"{where}: unknown key {k!r}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_dir(path: Path) -> str:
    """A directory's hash: sha256 over ``relpath NUL file-sha256 LF`` of every file, sorted by relpath."""
    h = hashlib.sha256()
    for f in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.relative_to(path).as_posix()):
        h.update(f"{f.relative_to(path).as_posix()}\0{sha256_file(f)}\n".encode("utf-8"))
    return h.hexdigest()


def evidence_file(path_text: str, base_dir: Optional[Path]) -> Path:
    """The file (or folder) an evidence path names: the part before any ``#fragment``; a relative path is relative
    to ``base_dir`` (the folder holding ``dip_store.json``)."""
    p = Path(path_text.split("#", 1)[0])
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    return p


def hash_evidence(source: dict[str, Any], base_dir: Optional[Path]) -> dict[str, Any]:
    """Return ``source`` with ``sha256`` + ``hash_of`` filled from the file it names. Refuses a path that does not
    exist and a given sha256 that does not match the file now."""
    src = dict(source)
    path_text = src.get("path")
    if not path_text:
        return src
    p = evidence_file(str(path_text), base_dir)
    if p.is_file():
        digest, kind = sha256_file(p), "file"
    elif p.is_dir():
        digest, kind = sha256_dir(p), "dir"
    else:
        raise KnowledgeError(f"evidence path does not exist: {path_text}")
    given = src.get("sha256")
    if given and given != digest:
        raise KnowledgeError(f"evidence {path_text}: sha256 {given} does not match the file now ({digest})")
    src["path"] = str(path_text).replace("\\", "/")
    src["sha256"] = digest
    src["hash_of"] = kind
    return src


# ----------------------------------------------------------------------------- unit identity
def unit_id_for(*, hardware_id: Optional[str] = None, edid_id: Optional[str] = None,
                device_path: Optional[str] = None) -> str:
    """``edid_id`` when its serial is non-zero (``"AUS322A-335544320"``), else ``hardware_id~`` + the first 8 hex of
    sha1(device path, lower case). A panel swap is a new EDID serial, hence a new unit."""
    if edid_id:
        serial = str(edid_id).rsplit("-", 1)[-1] if "-" in str(edid_id) else ""
        if serial and serial.strip("0") and _UNIT_ID_RE.match(str(edid_id)):
            return str(edid_id)
    if hardware_id and device_path:
        return f"{hardware_id}~{hashlib.sha1(device_path.lower().encode('utf-8')).hexdigest()[:8]}"
    raise KnowledgeError("cannot form a unit id: need an EDID id with a non-zero serial, or a hardware id and a "
                         "device path")


def _norm_name(s: str) -> str:
    return " ".join(str(s).split()).casefold()


# ----------------------------------------------------------------------------- the per-code-table rule
def _numeric_list(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(_is_num(x) or _numeric_list(x) for x in v)


def _table_violations(node: Any, path: str, *, in_numbers: bool, in_facet: bool, out: list[str]) -> None:
    if isinstance(node, list):
        if _numeric_list(node):
            limit = MAX_FACET_ARRAY if in_facet else MAX_NUMERIC_ARRAY
            if len(node) > limit:
                out.append(f"{path}: a numeric array of {len(node)} values (limit {limit}): raw tables go in an "
                           f"evidence file (path + sha256), not in the knowledge file")
        for i, x in enumerate(node):
            _table_violations(x, f"{path}[{i}]", in_numbers=in_numbers, in_facet=in_facet, out=out)
    elif isinstance(node, dict):
        keys = list(node)
        if keys and all(isinstance(k, str) and _NUMKEY_RE.match(k) for k in keys):
            if not in_numbers:
                out.append(f"{path}: a number-keyed map (per-code/per-level table) outside observations[].numbers")
            elif len(keys) > MAX_NUMERIC_ARRAY:
                out.append(f"{path}: a number-keyed map of {len(keys)} entries (limit {MAX_NUMERIC_ARRAY})")
        for k, x in node.items():
            _table_violations(x, f"{path}.{k}", in_numbers=in_numbers, in_facet=in_facet, out=out)


# ----------------------------------------------------------------------------- validation
def _check_by(v: Any, where: str, out: list[str]) -> None:
    if not isinstance(v, str) or not _BY_RE.match(v):
        out.append(f"{where}: 'by' must be 'owner', 'flow:<name>' or 'llm:<who>' (got {v!r})")


def _check_text(v: Any, where: str, out: list[str], *, max_len: int, min_len: int = 1) -> None:
    if not isinstance(v, str) or len(v.strip()) < min_len:
        out.append(f"{where}: must be a non-empty string")
    elif len(v) > max_len:
        out.append(f"{where}: {len(v)} characters (limit {max_len})")


def _check_scope(scope: Any, where: str, out: list[str]) -> None:
    if not isinstance(scope, dict):
        out.append(f"{where}: must be an object")
        return
    _unknown_keys(scope, SCOPE_KEYS, where, out)
    if "unknown" not in scope:
        out.append(f"{where}.unknown: required (list what was NOT tested; it stops a fact from excusing a read "
                   f"taken under other conditions)")
    elif not isinstance(scope["unknown"], list) or not all(isinstance(x, str) and x.strip()
                                                           for x in scope["unknown"]):
        out.append(f"{where}.unknown: must be a list of strings")
    if "mode" in scope and (not isinstance(scope["mode"], list) or not scope["mode"]
                            or any(m not in MODES for m in scope["mode"])):
        out.append(f"{where}.mode: a non-empty list from {MODES}")
    if "domain" in scope and scope["domain"] not in DOMAINS:
        out.append(f"{where}.domain: one of {DOMAINS}")
    for k in ("code_range", "level_nits"):
        if k in scope and not _range2(scope[k]):
            out.append(f"{where}.{k}: [lo, hi] numbers, lo <= hi")
    if "stimulus" in scope:
        st = scope["stimulus"]
        if not isinstance(st, dict):
            out.append(f"{where}.stimulus: must be an object")
        else:
            _unknown_keys(st, STIMULUS_KEYS, f"{where}.stimulus", out)
            if "area_pct" in st and not (_is_num(st["area_pct"]) and 0 < st["area_pct"] <= 100):
                out.append(f"{where}.stimulus.area_pct: a number in (0, 100]")
            if "pattern" in st and not isinstance(st["pattern"], str):
                out.append(f"{where}.stimulus.pattern: a string")
    if "panel_settings" in scope and (not isinstance(scope["panel_settings"], dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in scope["panel_settings"].items())):
        out.append(f"{where}.panel_settings: an object of strings")
    if "refresh_hz" in scope and (not isinstance(scope["refresh_hz"], list) or not scope["refresh_hz"]
                                  or not all(_is_num(x) and x > 0 for x in scope["refresh_hz"])):
        out.append(f"{where}.refresh_hz: a non-empty list of positive numbers")
    for k in ("stack", "thermal", "instrument"):
        if k in scope and not isinstance(scope[k], str):
            out.append(f"{where}.{k}: a string")
    if "link_bpc" in scope and scope["link_bpc"] not in (8, 10, 12):
        out.append(f"{where}.link_bpc: 8, 10 or 12")


def _check_signature(sig: Any, where: str, out: list[str]) -> None:
    if not isinstance(sig, dict) or "type" not in sig:
        out.append(f"{where}: an object with a 'type' (use {{\"type\": \"none\"}} when nothing can be matched)")
        return
    t = sig["type"]
    if t not in SIGNATURES:
        out.append(f"{where}.type: {t!r} is not one of {tuple(SIGNATURES)}")
        return
    req, opt = SIGNATURES[t]
    _unknown_keys(sig, req | opt | {"type"}, where, out)
    for k in sorted(req - set(sig)):
        out.append(f"{where}: {t} needs {k!r}")
    if "domain" in sig and sig["domain"] not in DOMAINS:
        out.append(f"{where}.domain: one of {DOMAINS}")
    for k in ("code_range", "period_codes", "tau_min", "step_pct", "level_nits", "levels_nits", "ratio_bounds_pct"):
        if k in sig and not _range2(sig[k]):
            out.append(f"{where}.{k}: [lo, hi] numbers, lo <= hi")
    if "quantity" in sig and sig["quantity"] not in QUANTITIES:
        out.append(f"{where}.quantity: one of {QUANTITIES}")
    for k in ("max_abs_dev_pct", "chroma_dxy_max", "max_abs_dxy", "max_spread_dxy", "floor_nits",
              "stimulus_area_pct"):
        if k in sig and not (_is_num(sig[k]) and sig[k] >= 0):
            out.append(f"{where}.{k}: a non-negative number")
    if t == "level_ratio":
        if sig.get("key") not in ("drive_code", "light"):
            out.append(f"{where}.key: 'drive_code' or 'light'")
        if not (isinstance(sig.get("pair"), list) and len(sig["pair"]) == 2
                and all(isinstance(x, str) for x in sig["pair"])):
            out.append(f"{where}.pair: two condition names")
        if "codes" in sig and not (isinstance(sig["codes"], list) and all(_is_num(x) for x in sig["codes"])):
            out.append(f"{where}.codes: a list of numbers")
    if t == "thermal_offset":
        sign = sig.get("sign")
        if not isinstance(sign, dict) or not sign or not all(v in ("+", "-") for v in sign.values()):
            out.append(f"{where}.sign: an object axis -> '+' / '-'")
        mx = sig.get("max_abs")
        if not isinstance(mx, dict) or not mx or not all(_is_num(v) and v >= 0 for v in mx.values()):
            out.append(f"{where}.max_abs: an object axis -> non-negative number")
    if "dither" in sig and not isinstance(sig["dither"], bool):
        out.append(f"{where}.dither: true / false")
    if "link_bpc" in sig and sig["link_bpc"] not in (8, 10, 12):
        out.append(f"{where}.link_bpc: 8, 10 or 12")
    for k in ("u_bound", "ratio_source", "precondition", "stimulus"):
        if k in sig and not isinstance(sig[k], str):
            out.append(f"{where}.{k}: a string")


def _check_facets(facets: Any, where: str, out: list[str]) -> None:
    if not isinstance(facets, dict):
        out.append(f"{where}: must be an object")
        return
    for name, val in facets.items():
        w = f"{where}.{name}"
        if name not in FACETS:
            out.append(f"{w}: unknown facet (known: {tuple(FACETS)})")
            continue
        if not isinstance(val, dict):
            out.append(f"{w}: must be an object")
            continue
        req, opt, _ = FACETS[name]
        _unknown_keys(val, req | opt, w, out)
        for k in sorted(req - set(val)):
            out.append(f"{w}: needs {k!r}")
        if "mode" in val and val["mode"] not in MODES:
            out.append(f"{w}.mode: one of {MODES}")
        for k, x in val.items():
            if isinstance(x, (dict,)):
                out.append(f"{w}.{k}: facet values are numbers, short strings or short number lists")
            elif isinstance(x, str) and len(x) > 120:
                out.append(f"{w}.{k}: a string of {len(x)} characters (limit 120)")
            elif isinstance(x, list) and not _numeric_list(x):
                out.append(f"{w}.{k}: a list must hold numbers")


def _check_numbers(nums: Any, where: str, out: list[str]) -> None:
    if not isinstance(nums, dict) or not nums:
        out.append(f"{where}: an observation needs numbers (a non-empty object of summary statistics)")
        return
    for k, v in nums.items():
        w = f"{where}.{k}"
        if _is_num(v) or isinstance(v, bool):
            continue
        if isinstance(v, str):
            if len(v) > 80:
                out.append(f"{w}: a string of {len(v)} characters (limit 80)")
            continue
        if isinstance(v, list):
            if not _numeric_list(v):
                out.append(f"{w}: a list must hold numbers")
            continue
        if isinstance(v, dict):
            for kk, vv in v.items():
                if not (_is_num(vv) or (isinstance(vv, list) and _numeric_list(vv))):
                    out.append(f"{w}.{kk}: a number or a short number list")
            continue
        out.append(f"{w}: numbers hold numbers, short strings, number lists or small maps")


def _check_source(src: Any, where: str, out: list[str]) -> None:
    if not isinstance(src, dict):
        out.append(f"{where}: must be an object")
        return
    _unknown_keys(src, SOURCE_KEYS, where, out)
    _check_text(src.get("kind"), f"{where}.kind", out, max_len=120)
    if not src.get("path") and not src.get("ref"):
        out.append(f"{where}: needs a 'path' (an evidence file, hashed) or a 'ref' (a pointer without a file)")
    if src.get("path"):
        if not isinstance(src["path"], str):
            out.append(f"{where}.path: a string")
        if not (isinstance(src.get("sha256"), str) and _SHA_RE.match(src["sha256"])):
            out.append(f"{where}.sha256: an evidence path needs the sha256 taken when it was written")
        if src.get("hash_of") not in ("file", "dir"):
            out.append(f"{where}.hash_of: 'file' or 'dir'")
    elif "sha256" in src or "hash_of" in src:
        out.append(f"{where}: sha256 / hash_of without a path")
    if "ref" in src:
        _check_text(src["ref"], f"{where}.ref", out, max_len=300)


def _check_observation(o: Any, where: str, out: list[str]) -> None:
    if not isinstance(o, dict):
        out.append(f"{where}: must be an object")
        return
    _unknown_keys(o, OBS_KEYS, where, out)
    if not (isinstance(o.get("id"), str) and _OBS_ID_RE.match(o["id"])):
        out.append(f"{where}.id: 'O<n>'")
    if not _is_date(o.get("date")):
        out.append(f"{where}.date: YYYY-MM-DD")
    _check_by(o.get("by"), where, out)
    if o.get("verdict") not in VERDICTS:
        out.append(f"{where}.verdict: one of {VERDICTS}")
    _check_source(o.get("source"), f"{where}.source", out)
    _check_numbers(o.get("numbers"), f"{where}.numbers", out)
    if not isinstance(o.get("independent"), bool):
        out.append(f"{where}.independent: true only for a different run AND session (day or re-placed meter)")
    if "conditions" in o and (not isinstance(o["conditions"], dict) or not all(
            isinstance(v, (str, int, float, bool)) for v in o["conditions"].values())):
        out.append(f"{where}.conditions: an object of scalars")
    if "note" in o:
        _check_text(o["note"], f"{where}.note", out, max_len=600)


def _check_links(links: Any, where: str, out: list[str]) -> None:
    if not isinstance(links, list):
        out.append(f"{where}: must be a list")
        return
    for i, ln in enumerate(links):
        w = f"{where}[{i}]"
        if not isinstance(ln, dict):
            out.append(f"{w}: must be an object")
            continue
        _unknown_keys(ln, LINK_KEYS, w, out)
        rel = str(ln.get("rel", ""))
        if rel.rstrip("?") not in LINK_RELS:
            out.append(f"{w}.rel: one of {LINK_RELS} (a trailing '?' marks a hypothesised link)")
        if rel.rstrip("?") == "see_field":
            if not isinstance(ln.get("field"), str) or "id" in ln:
                out.append(f"{w}: see_field takes a 'field' (a DIP record field), no 'id'")
        else:
            lid = ln.get("id")
            if not (isinstance(lid, str) and (_ID_RE.match(lid) or _XID_RE.match(lid))) or "field" in ln:
                out.append(f"{w}.id: an entry id ('F-0001', or '<unit_id>/F-0001' across units)")


def validate_entry(e: Any, where: str = "entry") -> list[str]:
    out: list[str] = []
    if not isinstance(e, dict):
        return [f"{where}: must be an object"]
    _unknown_keys(e, ENTRY_KEYS, where, out)
    eid, kind = e.get("id"), e.get("kind")
    if kind not in KINDS:
        out.append(f"{where}.kind: one of {KINDS}")
    m = _ID_RE.match(eid) if isinstance(eid, str) else None
    if not m:
        out.append(f"{where}.id: '<prefix>-NNNN' (F panel, R rig, E event, H mechanism hypothesis)")
    elif kind in ID_PREFIXES and m.group(1) not in ID_PREFIXES[kind]:
        out.append(f"{where}.id: prefix {m.group(1)} does not fit kind {kind} (allowed {ID_PREFIXES[kind]})")
    _check_text(e.get("title"), f"{where}.title", out, max_len=TITLE_MAX)
    _check_text(e.get("statement"), f"{where}.statement", out, max_len=STATEMENT_MAX, min_len=20)
    _check_scope(e.get("scope"), f"{where}.scope", out)
    status = e.get("status")
    if status not in STATUSES:
        out.append(f"{where}.status: one of {STATUSES}")
    hist = e.get("status_history")
    if not isinstance(hist, list) or not hist:
        out.append(f"{where}.status_history: a non-empty list")
    else:
        for i, h in enumerate(hist):
            w = f"{where}.status_history[{i}]"
            if not isinstance(h, dict):
                out.append(f"{w}: must be an object")
                continue
            _unknown_keys(h, STATUS_HISTORY_KEYS, w, out)
            if h.get("status") not in STATUSES:
                out.append(f"{w}.status: one of {STATUSES}")
            if not _is_date(h.get("date")):
                out.append(f"{w}.date: YYYY-MM-DD")
            _check_by(h.get("by"), w, out)
            _check_text(h.get("why"), f"{w}.why", out, max_len=600)
        if isinstance(hist[-1], dict) and hist[-1].get("status") != status:
            out.append(f"{where}.status_history: the last entry ({hist[-1].get('status')}) is not the status "
                       f"({status})")
    if kind == "event":
        if not isinstance(e.get("explained"), bool):
            out.append(f"{where}.explained: an event says whether an entry explains it (true / false)")
    elif "explained" in e:
        out.append(f"{where}.explained: only events carry it")
    obs = e.get("observations")
    if not isinstance(obs, list):
        out.append(f"{where}.observations: a list")
        obs = []
    seen_o: set[str] = set()
    for i, o in enumerate(obs):
        _check_observation(o, f"{where}.observations[{i}]", out)
        oid = o.get("id") if isinstance(o, dict) else None
        if oid in seen_o:
            out.append(f"{where}.observations[{i}].id: {oid} used twice")
        seen_o.add(oid)
    roll = e.get("rollup")
    if roll is not None:
        if not isinstance(roll, dict):
            out.append(f"{where}.rollup: null or an object")
        else:
            _unknown_keys(roll, ROLLUP_KEYS, f"{where}.rollup", out)
            if not (isinstance(roll.get("count"), int) and roll["count"] > 0):
                out.append(f"{where}.rollup.count: a positive integer")
            fids = roll.get("folded_ids")
            if not isinstance(fids, list) or len(fids) != roll.get("count"):
                out.append(f"{where}.rollup.folded_ids: one id per folded observation")
            elif seen_o & set(fids):
                out.append(f"{where}.rollup.folded_ids: {sorted(seen_o & set(fids))} are also kept in full")
            for k, v in (roll.get("verdicts") or {}).items():
                if k not in FOLDABLE_VERDICTS:
                    out.append(f"{where}.rollup.verdicts: a {k!r} observation was folded (only "
                               f"{FOLDABLE_VERDICTS} fold)")
    _check_signature(e.get("signature"), f"{where}.signature", out)
    if "facets" in e:
        _check_facets(e["facets"], f"{where}.facets", out)
    if "implications" in e:
        if not isinstance(e["implications"], list):
            out.append(f"{where}.implications: a list")
        else:
            for i, im in enumerate(e["implications"]):
                w = f"{where}.implications[{i}]"
                if not isinstance(im, dict):
                    out.append(f"{w}: must be an object")
                    continue
                _unknown_keys(im, IMPLICATION_KEYS, w, out)
                if not (isinstance(im.get("flows"), list) and im["flows"]
                        and all(isinstance(x, str) for x in im["flows"])):
                    out.append(f"{w}.flows: a non-empty list of flow names")
                _check_text(im.get("text"), f"{w}.text", out, max_len=600)
    if "open_questions" in e:
        if not isinstance(e["open_questions"], list):
            out.append(f"{where}.open_questions: a list")
        else:
            qids: set[str] = set()
            for i, q in enumerate(e["open_questions"]):
                w = f"{where}.open_questions[{i}]"
                if not isinstance(q, dict):
                    out.append(f"{w}: must be an object")
                    continue
                _unknown_keys(q, QUESTION_KEYS, w, out)
                if not (isinstance(q.get("id"), str) and _Q_ID_RE.match(q["id"])) or q.get("id") in qids:
                    out.append(f"{w}.id: a unique 'Q<n>'")
                qids.add(q.get("id"))
                _check_text(q.get("q"), f"{w}.q", out, max_len=400)
                _check_text(q.get("test"), f"{w}.test", out, max_len=400)
                if not isinstance(q.get("owner_action"), bool):
                    out.append(f"{w}.owner_action: true when the test needs the owner (a monitor-menu change, "
                               f"the meter re-placed)")
                if q.get("status") not in ("open", "closed"):
                    out.append(f"{w}.status: 'open' or 'closed'")
                if q.get("status") == "closed":
                    _check_text(q.get("answer"), f"{w}.answer", out, max_len=600)
                    if not _is_date(q.get("closed")):
                        out.append(f"{w}.closed: YYYY-MM-DD")
                    _check_by(q.get("closed_by"), w, out)
    if "links" in e:
        _check_links(e["links"], f"{where}.links", out)
    if e.get("transferable", "no") not in TRANSFERABLE:
        out.append(f"{where}.transferable: one of {TRANSFERABLE}")
    if e.get("last_confirmed") is not None and not _is_date(e.get("last_confirmed")):
        out.append(f"{where}.last_confirmed: YYYY-MM-DD or null")
    if not _is_date(e.get("created")):
        out.append(f"{where}.created: YYYY-MM-DD")
    revs = e.get("revisions")
    if not isinstance(revs, list) or not revs:
        out.append(f"{where}.revisions: a non-empty list (the create is revision 1)")
    else:
        for i, r in enumerate(revs):
            w = f"{where}.revisions[{i}]"
            if not isinstance(r, dict):
                out.append(f"{w}: must be an object")
                continue
            _unknown_keys(r, REVISION_KEYS, w, out)
            if r.get("rev") != i + 1:
                out.append(f"{w}.rev: {i + 1} expected")
            if r.get("op") not in OPS:
                out.append(f"{w}.op: one of {OPS}")
            if not _is_date(r.get("date")):
                out.append(f"{w}.date: YYYY-MM-DD")
            _check_by(r.get("by"), w, out)
            _check_text(r.get("why"), f"{w}.why", out, max_len=600)
        if e.get("rev") != len(revs):
            out.append(f"{where}.rev: {len(revs)} expected (one per revision)")
    # status-specific rules
    if status == "measured":
        all_obs = [o for o in obs if isinstance(o, dict)]
        rolled = (roll or {}).get("count", 0) if isinstance(roll, dict) else 0
        if not all_obs and not rolled:
            out.append(f"{where}: a measured entry needs observations with numbers")
        if not any(isinstance(o.get("source"), dict) and o["source"].get("path") for o in all_obs):
            out.append(f"{where}: a measured entry needs at least one observation with a hashed evidence file")
        sc = e.get("scope") if isinstance(e.get("scope"), dict) else {}
        if isinstance(sc.get("unknown"), list) and not sc["unknown"]:
            out.append(f"{where}.scope.unknown: a measured entry must name what was not tested (it is never "
                       f"nothing)")
    if status == "inferred" and not obs and not e.get("links"):
        out.append(f"{where}: an inferred entry needs the measured data it follows from (observations or links)")
    if status == "superseded" and not any(isinstance(ln, dict) and str(ln.get("rel", "")).rstrip("?")
                                          == "superseded_by" for ln in (e.get("links") or [])):
        out.append(f"{where}: a superseded entry names its successor (a 'superseded_by' link)")
    _table_violations({k: v for k, v in e.items() if k not in ("observations", "facets", "revisions")}, where,
                      in_numbers=False, in_facet=False, out=out)
    for i, o in enumerate(obs):
        if isinstance(o, dict):
            _table_violations({k: v for k, v in o.items() if k != "numbers"}, f"{where}.observations[{i}]",
                              in_numbers=False, in_facet=False, out=out)
            _table_violations(o.get("numbers"), f"{where}.observations[{i}].numbers", in_numbers=True,
                              in_facet=False, out=out)
    if isinstance(e.get("facets"), dict):
        _table_violations(e["facets"], f"{where}.facets", in_numbers=False, in_facet=True, out=out)
    return out


def _scopes_overlap(a: dict, b: dict) -> bool:
    ma, mb = set(a.get("mode") or MODES), set(b.get("mode") or MODES)
    if not ma & mb:
        return False
    if a.get("domain") and b.get("domain") and a["domain"] != b["domain"]:
        return False
    for k in ("code_range", "level_nits"):
        ra, rb = a.get(k), b.get(k)
        if _range2(ra) and _range2(rb) and (ra[1] < rb[0] or rb[1] < ra[0]):
            return False
    return True


def validate_unit(doc: Any) -> list[str]:
    """Every violation in a unit document (an empty list = valid)."""
    out: list[str] = []
    if not isinstance(doc, dict):
        return ["the file must hold a JSON object"]
    _unknown_keys(doc, UNIT_KEYS, "unit", out)
    if doc.get("schema") != SCHEMA:
        out.append(f"unit.schema: {SCHEMA} expected (got {doc.get('schema')!r})")
    if not (isinstance(doc.get("rev"), int) and doc["rev"] >= 0):
        out.append("unit.rev: a non-negative integer")
    uid = doc.get("unit_id")
    if not (isinstance(uid, str) and _UNIT_ID_RE.match(uid)):
        out.append("unit.unit_id: letters, digits and . _ ~ - (3-80 characters)")
    ident = doc.get("identity")
    if not isinstance(ident, dict):
        out.append("unit.identity: an object")
    else:
        _unknown_keys(ident, IDENTITY_KEYS, "unit.identity", out)
        if not isinstance(ident.get("hardware_id"), str) or not ident.get("hardware_id"):
            out.append("unit.identity.hardware_id: required")
        if "edid_id" in ident and not isinstance(ident["edid_id"], (str, type(None))):
            out.append("unit.identity.edid_id: a string or null")
        if not isinstance(ident.get("device_paths", []), list) or not all(
                isinstance(x, str) for x in ident.get("device_paths", [])):
            out.append("unit.identity.device_paths: a list of strings")
    if not isinstance(doc.get("aliases"), list) or not all(isinstance(a, str) and a.strip()
                                                           for a in doc.get("aliases") or [None]):
        out.append("unit.aliases: a non-empty list of names (the DLC display names it was seen under)")
    for k in ("model",):
        if k in doc and not isinstance(doc[k], str):
            out.append(f"unit.{k}: a string")
    if "panel" in doc:
        if not isinstance(doc["panel"], dict):
            out.append("unit.panel: an object")
        else:
            _unknown_keys(doc["panel"], PANEL_KEYS, "unit.panel", out)
    for i, s in enumerate(doc.get("standing_conditions") or []):
        w = f"unit.standing_conditions[{i}]"
        if not isinstance(s, dict):
            out.append(f"{w}: must be an object")
            continue
        _unknown_keys(s, STANDING_KEYS, w, out)
        for k in ("setting", "value", "source"):
            _check_text(s.get(k), f"{w}.{k}", out, max_len=200)
        if not isinstance(s.get("verified"), bool):
            out.append(f"{w}.verified: true only when DLC read the setting itself")
    for i, ev in enumerate(doc.get("unit_events") or []):
        w = f"unit.unit_events[{i}]"
        if not isinstance(ev, dict):
            out.append(f"{w}: must be an object")
            continue
        _unknown_keys(ev, UNIT_EVENT_KEYS, w, out)
        if not _is_date(ev.get("date")):
            out.append(f"{w}.date: YYYY-MM-DD")
        _check_text(ev.get("what"), f"{w}.what", out, max_len=400)
        _check_by(ev.get("by"), w, out)
    for i, r in enumerate(doc.get("revisions") or []):
        w = f"unit.revisions[{i}]"
        if not isinstance(r, dict):
            out.append(f"{w}: must be an object")
            continue
        _unknown_keys(r, UNIT_REVISION_KEYS, w, out)
        if r.get("op") not in OPS:
            out.append(f"{w}.op: one of {OPS}")
        _check_by(r.get("by"), w, out)
    entries = doc.get("entries")
    if not isinstance(entries, list):
        out.append("unit.entries: a list")
        return out
    ids: dict[str, dict] = {}
    for i, e in enumerate(entries):
        eid = e.get("id") if isinstance(e, dict) else None
        out.extend(validate_entry(e, f"entries[{eid or i}]"))
        if eid in ids:
            out.append(f"entries[{eid}]: id used twice")
        if isinstance(eid, str):
            ids[eid] = e
    if [e.get("id") for e in entries if isinstance(e, dict)] != sorted(ids):
        out.append("unit.entries: must be sorted by id")
    for eid, e in ids.items():
        for ln in e.get("links") or []:
            lid = ln.get("id") if isinstance(ln, dict) else None
            if isinstance(lid, str) and _ID_RE.match(lid) and lid not in ids:
                out.append(f"entries[{eid}].links: {lid} is not an entry of this unit")
    active = [e for e in ids.values() if e.get("status") in ACTIVE_STATUSES]
    if len(active) > MAX_ACTIVE_ENTRIES:
        out.append(f"unit.entries: {len(active)} measured / inferred / hypothesis entries (limit "
                   f"{MAX_ACTIVE_ENTRIES}): supersede or merge one first")
    # two measured entries with the same signature type and overlapping scope need an explicit link
    measured = [e for e in ids.values() if e.get("status") == "measured"
                and isinstance(e.get("signature"), dict) and e["signature"].get("type") not in (None, "none")]
    for i, a in enumerate(measured):
        for b in measured[i + 1:]:
            if a["signature"]["type"] != b["signature"]["type"]:
                continue
            if not _scopes_overlap(a.get("scope") or {}, b.get("scope") or {}):
                continue

            def linked(x: dict, y: dict) -> bool:
                return any(isinstance(ln, dict) and ln.get("id") == y["id"]
                           and str(ln.get("rel", "")).rstrip("?") in CONFLICT_LINK_RELS
                           and not str(ln.get("rel", "")).endswith("?") for ln in x.get("links") or [])
            if not (linked(a, b) or linked(b, a)):
                out.append(f"entries[{a['id']}] and entries[{b['id']}]: both measured, signature "
                           f"{a['signature']['type']}, overlapping scope: one must 'refines' / 'supersedes' / "
                           f"'contradicts' the other")
    return out


def check_append_only(old: dict, new: dict) -> list[str]:
    """What ``new`` lost or rewrote of ``old`` (both valid unit documents)."""
    out: list[str] = []

    def prefix(a: list, b: list, where: str) -> None:
        if b[:len(a)] != a:
            out.append(f"{where}: append-only, an earlier item was changed or removed")

    prefix(old.get("unit_events") or [], new.get("unit_events") or [], "unit.unit_events")
    prefix(old.get("revisions") or [], new.get("revisions") or [], "unit.revisions")
    for a in old.get("aliases") or []:
        if a not in (new.get("aliases") or []):
            out.append(f"unit.aliases: {a!r} was removed")
    if old.get("unit_id") != new.get("unit_id"):
        out.append("unit.unit_id: never changes (a panel swap is a new unit)")
    new_by_id = {e.get("id"): e for e in new.get("entries") or []}
    for oe in old.get("entries") or []:
        eid = oe.get("id")
        ne = new_by_id.get(eid)
        if ne is None:
            out.append(f"entries[{eid}]: removed (entries are never deleted: refute or supersede them)")
            continue
        w = f"entries[{eid}]"
        if ne.get("kind") != oe.get("kind"):
            out.append(f"{w}.kind: never changes")
        prefix(oe.get("revisions") or [], ne.get("revisions") or [], f"{w}.revisions")
        prefix(oe.get("status_history") or [], ne.get("status_history") or [], f"{w}.status_history")
        old_roll = oe.get("rollup") or {}
        new_roll = ne.get("rollup") or {}
        prefix(old_roll.get("folded_ids") or [], new_roll.get("folded_ids") or [], f"{w}.rollup.folded_ids")
        kept = {o.get("id"): o for o in ne.get("observations") or []}
        folded = set(new_roll.get("folded_ids") or [])
        for o in oe.get("observations") or []:
            oid = o.get("id")
            if oid in kept:
                if kept[oid] != o:
                    out.append(f"{w}.observations[{oid}]: changed (observations are append-only)")
            elif oid in folded:
                if o.get("verdict") not in FOLDABLE_VERDICTS:
                    out.append(f"{w}.observations[{oid}]: a {o.get('verdict')!r} observation was folded")
            else:
                out.append(f"{w}.observations[{oid}]: removed")
        oq = {q.get("id"): q for q in oe.get("open_questions") or []}
        nq = {q.get("id"): q for q in ne.get("open_questions") or []}
        for qid, q in oq.items():
            if qid not in nq:
                out.append(f"{w}.open_questions[{qid}]: removed (close it instead)")
            elif nq[qid].get("q") != q.get("q") or nq[qid].get("test") != q.get("test"):
                out.append(f"{w}.open_questions[{qid}]: its text changed")
            elif q.get("status") == "closed" and nq[qid] != q:
                out.append(f"{w}.open_questions[{qid}]: a closed question changed")
        if ne != oe and len(ne.get("revisions") or []) <= len(oe.get("revisions") or []):
            out.append(f"{w}: changed without a revision")
        new_revs = (ne.get("revisions") or [])[len(oe.get("revisions") or []):]
        for fld, op in (("statement", "edit_statement"), ("title", "edit_statement"), ("scope", "set_scope")):
            if ne.get(fld) != oe.get(fld):
                if not any(r.get("op") == op and isinstance(r.get("old"), dict) and fld in r["old"]
                           and r["old"][fld] == oe.get(fld) for r in new_revs):
                    out.append(f"{w}.{fld}: changed without a revision holding the old value")
    return out


# ----------------------------------------------------------------------------- derived state
def _parse(d: Optional[str]) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(d) if d else None
    except ValueError:
        return None


def entry_state(doc: dict, e: dict, today: Optional[str] = None,
                *, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict[str, Any]:
    """Derived, never stored: ``unconfirmed`` (a unit event after ``last_confirmed``, or older than the DIP's own
    staleness clock), ``dormant`` (a hypothesis with no observation and no open test for 90 days), the counts, and
    ``can_explain`` + why not (status measured, >= 2 independent observations, not unconfirmed). Excuse power is
    evidence for the LLM, never a gate."""
    t = _parse(today or today_iso()) or _dt.date.today()
    obs = e.get("observations") or []
    roll = e.get("rollup") or {}
    n_obs = len(obs) + int(roll.get("count") or 0)
    n_ind = sum(1 for o in obs if o.get("independent")) + int(roll.get("independent") or 0)
    reasons: list[str] = []
    last = _parse(e.get("last_confirmed"))
    for ev in doc.get("unit_events") or []:
        ed = _parse(ev.get("date"))
        if ed and (last is None or ed > last) and e.get("status") == "measured":
            reasons.append(f"unit event {ev.get('date')} ({ev.get('what')}) after the last confirmation")
    if last and (t - last).days > max_age_days:
        reasons.append(f"not re-observed for {(t - last).days} days (limit {max_age_days})")
    unconfirmed = bool(reasons)
    open_tests = [q for q in e.get("open_questions") or [] if q.get("status") == "open"]
    created = _parse(e.get("created"))
    dormant = (e.get("status") == "hypothesis" and not obs and not roll and not open_tests
               and created is not None and (t - created).days > DORMANT_DAYS)
    why_not: list[str] = []
    if e.get("status") != "measured":
        why_not.append(f"status {e.get('status')}")
    if n_ind < 2:
        why_not.append(f"{n_ind} independent observation(s), 2 needed")
    if unconfirmed:
        why_not.append("unconfirmed")
    if (e.get("signature") or {}).get("type") in (None, "none"):
        why_not.append("no signature")
    return {"observations": n_obs, "independent": n_ind, "unconfirmed": unconfirmed, "unconfirmed_why": reasons,
            "dormant": dormant, "can_explain": not why_not, "cannot_explain_why": why_not}


# ----------------------------------------------------------------------------- ops
def _fold(e: dict) -> None:
    """Fold the oldest ``matches`` / ``consistent`` observations into ``rollup`` while more than 40 are kept."""
    obs = e.get("observations") or []
    while len(obs) > MAX_FULL_OBSERVATIONS:
        idx = next((i for i, o in enumerate(obs) if o.get("verdict") in FOLDABLE_VERDICTS), None)
        if idx is None:
            break                               # nothing foldable: contradictions / refinements stay in full
        o = obs.pop(idx)
        r = e.get("rollup") or {"count": 0, "independent": 0, "first": o["date"], "last": o["date"],
                                "folded_ids": [], "verdicts": {}, "numbers": {}}
        r["count"] += 1
        r["independent"] += 1 if o.get("independent") else 0
        r["first"] = min(r["first"], o["date"])
        r["last"] = max(r["last"], o["date"])
        r["folded_ids"].append(o["id"])
        r["verdicts"][o["verdict"]] = r["verdicts"].get(o["verdict"], 0) + 1
        for k, v in (o.get("numbers") or {}).items():
            if _is_num(v):
                lo, hi = v, v
            elif _range2(v):
                lo, hi = v
            else:
                continue
            cur = r["numbers"].get(k)
            r["numbers"][k] = [min(cur[0], lo), max(cur[1], hi)] if cur else [lo, hi]
        e["rollup"] = r
    e["observations"] = obs


def _entry(doc: dict, eid: Any) -> dict:
    for e in doc.get("entries") or []:
        if e.get("id") == eid:
            return e
    raise KnowledgeError(f"no entry {eid!r} in unit {doc.get('unit_id')}")


def _next_id(doc: dict, prefix: str) -> str:
    n = 0
    for e in doc.get("entries") or []:
        m = _ID_RE.match(str(e.get("id", "")))
        if m and m.group(1) == prefix:
            n = max(n, int(m.group(2)))
    return f"{prefix}-{n + 1:04d}"


def _revise(e: dict, *, op: str, by: str, why: str, date: str, old: Optional[dict] = None) -> None:
    revs = e.setdefault("revisions", [])
    rec = {"rev": len(revs) + 1, "date": date, "by": by, "op": op, "why": why}
    if old is not None:
        rec["old"] = old
    revs.append(rec)
    e["rev"] = len(revs)


def _confirm(e: dict, o: dict) -> None:
    if o.get("verdict") in CONFIRMING_VERDICTS:
        e["last_confirmed"] = max(filter(None, [e.get("last_confirmed"), o["date"]]))


def _add_obs(e: dict, obs_in: dict, *, by: str, base_dir: Optional[Path], default_date: str) -> dict:
    if not isinstance(obs_in, dict):
        raise KnowledgeError("an observation must be an object")
    bad = sorted(set(obs_in) - OBS_INPUT_KEYS - {"id"})
    if bad:
        raise KnowledgeError(f"observation: unknown keys {bad}")
    o = copy.deepcopy(obs_in)
    used = [int(m.group(1)) for x in (e.get("observations") or []) for m in [_OBS_ID_RE.match(str(x.get("id")))]
            if m] + [int(m.group(1)) for x in ((e.get("rollup") or {}).get("folded_ids") or [])
                     for m in [_OBS_ID_RE.match(str(x))] if m]
    o["id"] = f"O{max(used, default=0) + 1}"
    o.setdefault("date", default_date)
    o.setdefault("by", by)
    if isinstance(o.get("source"), dict):
        o["source"] = hash_evidence(o["source"], base_dir)
    e.setdefault("observations", []).append({k: o[k] for k in _OBS_ORDER if k in o})
    _confirm(e, o)
    _fold(e)
    return o


_OBS_ORDER = ["id", "date", "by", "verdict", "independent", "source", "conditions", "numbers", "note"]
_ENTRY_ORDER = ["id", "kind", "title", "statement", "scope", "status", "explained", "status_history",
                "observations", "rollup", "signature", "facets", "implications", "open_questions", "links",
                "transferable", "created", "last_confirmed", "rev", "revisions"]


def _ordered_entry(e: dict) -> dict:
    return {k: e[k] for k in _ENTRY_ORDER if k in e}


def apply_op(doc: dict, op: dict, *, base_dir: Optional[Path] = None, today: Optional[str] = None) -> dict:
    """Apply one op to ``doc`` (in place; also returned). Every op carries ``op``, ``by`` and ``why``; ``date``
    defaults to today. Raises :class:`KnowledgeError` naming the problem; the caller validates the whole document
    afterwards."""
    if not isinstance(op, dict):
        raise KnowledgeError("an op must be an object")
    name, by, why = op.get("op"), op.get("by"), op.get("why")
    if name not in OPS:
        raise KnowledgeError(f"unknown op {name!r} (known: {OPS})")
    errs: list[str] = []
    _check_by(by, f"op {name}", errs)
    _check_text(why, f"op {name}.why", errs, max_len=600)
    if errs:
        raise KnowledgeError(f"op {name} refused", errs)
    date = op.get("date") or today or today_iso()
    if not _is_date(date):
        raise KnowledgeError(f"op {name}: date {date!r} is not YYYY-MM-DD")
    allowed = {"op", "by", "why", "date"} | _OP_ARGS[name]
    bad = sorted(set(op) - allowed)
    if bad:
        raise KnowledgeError(f"op {name}: unknown keys {bad} (allowed {sorted(allowed)})")
    unit_log = doc.setdefault("revisions", [])

    def log(detail: str) -> None:
        unit_log.append({"rev": int(doc.get("rev", 0)) + 1, "date": date, "by": by, "op": name, "why": why,
                         "detail": detail})

    if name == "create_entry":
        src = op.get("entry")
        if not isinstance(src, dict):
            raise KnowledgeError("create_entry: 'entry' must be an object")
        bad = sorted(set(src) - ENTRY_INPUT_KEYS)
        if bad:
            raise KnowledgeError(f"create_entry: keys the store sets or does not know: {bad}")
        e = copy.deepcopy(src)
        kind = e.get("kind")
        if kind not in KINDS:
            raise KnowledgeError(f"create_entry: kind must be one of {KINDS}")
        eid = e.get("id")
        if eid is None:
            prefix = op.get("id_prefix") or ID_PREFIXES[kind][0]
            if prefix not in ID_PREFIXES[kind]:
                raise KnowledgeError(f"create_entry: prefix {prefix} does not fit kind {kind}")
            eid = _next_id(doc, prefix)
        elif any(x.get("id") == eid for x in doc.get("entries") or []):
            raise KnowledgeError(f"create_entry: {eid} exists (ids are never reused)")
        else:
            m = _ID_RE.match(str(eid))
            if m and int(m.group(2)) <= int(_next_id(doc, m.group(1))[2:]) - 1:
                raise KnowledgeError(f"create_entry: {eid} is below the unit's highest {m.group(1)} id "
                                     f"(ids are never reused; leave 'id' out to take the next one)")
        e["id"] = eid
        status = e.get("status")
        e["status_history"] = [{"status": status, "date": date, "by": by, "why": why}]
        e["created"] = date
        e.setdefault("transferable", "no")
        e.setdefault("signature", {"type": "none"})
        e.setdefault("links", [])
        e.setdefault("implications", [])
        qs = []
        for i, q in enumerate(e.get("open_questions") or []):
            if not isinstance(q, dict):
                raise KnowledgeError("create_entry: an open question must be an object")
            qs.append({"id": f"Q{i + 1}", "status": "open", **{k: v for k, v in q.items() if k != "id"}})
        e["open_questions"] = qs
        obs_in = e.pop("observations", None) or []
        e["observations"] = []
        e["rollup"] = None
        e["last_confirmed"] = None
        for o in obs_in:
            _add_obs(e, o, by=by, base_dir=base_dir, default_date=date)
        e["revisions"] = []
        _revise(e, op=name, by=by, why=why, date=date)
        doc.setdefault("entries", []).append(_ordered_entry(e))
        doc["entries"].sort(key=lambda x: str(x.get("id")))
        log(eid)
        return doc

    if name in ("add_unit_event", "add_alias", "set_standing_condition"):
        if name == "add_unit_event":
            ev = op.get("event")
            if not isinstance(ev, dict) or set(ev) - {"date", "what"}:
                raise KnowledgeError("add_unit_event: 'event' = {date, what}")
            doc.setdefault("unit_events", []).append({"date": ev.get("date") or date, "what": ev.get("what"),
                                                      "by": by})
            log(str(ev.get("what"))[:120])
        elif name == "add_alias":
            ident = doc.setdefault("identity", {})
            if op.get("alias"):
                if _norm_name(op["alias"]) not in {_norm_name(a) for a in doc.get("aliases") or []}:
                    doc.setdefault("aliases", []).append(str(op["alias"]))
            if op.get("device_path"):
                dps = ident.setdefault("device_paths", [])
                if op["device_path"] not in dps:
                    dps.append(str(op["device_path"]))
            if not op.get("alias") and not op.get("device_path"):
                raise KnowledgeError("add_alias: give 'alias' and/or 'device_path'")
            log(str(op.get("alias") or op.get("device_path")))
        else:
            cond = op.get("condition")
            if not isinstance(cond, dict):
                raise KnowledgeError("set_standing_condition: 'condition' = {setting, value, source, verified}")
            conds = doc.setdefault("standing_conditions", [])
            conds[:] = [c for c in conds if _norm_name(c.get("setting", "")) != _norm_name(cond.get("setting", ""))]
            conds.append(dict(cond))
            log(f"{cond.get('setting')} = {cond.get('value')}")
        return doc

    e = _entry(doc, op.get("id"))
    if name == "add_observation":
        o = _add_obs(e, op.get("observation"), by=by, base_dir=base_dir, default_date=date)
        _revise(e, op=name, by=by, why=why, date=date)
        log(f"{e['id']} {o['id']} {o.get('verdict')}")
    elif name == "set_status":
        st = op.get("status")
        if st not in STATUSES:
            raise KnowledgeError(f"set_status: one of {STATUSES}")
        if st == e.get("status"):
            raise KnowledgeError(f"set_status: {e['id']} is already {st}")
        if op.get("successor"):
            e.setdefault("links", []).append({"rel": "superseded_by", "id": op["successor"]})
        if "explained" in op:
            if e.get("kind") != "event":
                raise KnowledgeError("set_status: 'explained' is for events")
            e["explained"] = bool(op["explained"])
        e["status"] = st
        e["status_history"].append({"status": st, "date": date, "by": by, "why": why})
        _revise(e, op=name, by=by, why=why, date=date)
        log(f"{e['id']} -> {st}")
    elif name == "edit_statement":
        old: dict[str, Any] = {}
        if "statement" in op:
            old["statement"] = e.get("statement")
            e["statement"] = op["statement"]
        if "title" in op:
            old["title"] = e.get("title")
            e["title"] = op["title"]
        if not old:
            raise KnowledgeError("edit_statement: give 'statement' and/or 'title'")
        _revise(e, op=name, by=by, why=why, date=date, old=old)
        log(e["id"])
    elif name == "set_scope":
        _revise(e, op=name, by=by, why=why, date=date, old={"scope": copy.deepcopy(e.get("scope"))})
        e["scope"] = copy.deepcopy(op.get("scope"))
        log(e["id"])
    elif name == "add_link":
        e.setdefault("links", []).append(dict(op.get("link") or {}))
        _revise(e, op=name, by=by, why=why, date=date)
        log(f"{e['id']} {op.get('link')}")
    elif name == "add_open_question":
        qs = e.setdefault("open_questions", [])
        n = max([int(m.group(1)) for q in qs for m in [_Q_ID_RE.match(str(q.get("id")))] if m], default=0)
        q = {"id": f"Q{n + 1}", "q": op.get("q"), "test": op.get("test"),
             "owner_action": bool(op.get("owner_action", False)), "status": "open"}
        qs.append(q)
        _revise(e, op=name, by=by, why=why, date=date)
        log(f"{e['id']} {q['id']}")
    elif name == "close_open_question":
        q = next((q for q in e.get("open_questions") or [] if q.get("id") == op.get("qid")), None)
        if q is None:
            raise KnowledgeError(f"close_open_question: {e['id']} has no question {op.get('qid')!r}")
        if q.get("status") == "closed":
            raise KnowledgeError(f"close_open_question: {e['id']} {q['id']} is closed already")
        q.update({"status": "closed", "answer": op.get("answer"), "closed": date, "closed_by": by})
        _revise(e, op=name, by=by, why=why, date=date)
        log(f"{e['id']} {q['id']}")
    elif name == "set_facet":
        fname = op.get("name")
        if fname not in FACETS:
            raise KnowledgeError(f"set_facet: unknown facet {fname!r} (known: {tuple(FACETS)})")
        facets = e.setdefault("facets", {})
        old_f = copy.deepcopy(facets.get(fname))
        if op.get("value") is None:
            facets.pop(fname, None)
        else:
            facets[fname] = copy.deepcopy(op["value"])
        _revise(e, op=name, by=by, why=why, date=date, old={"facets": {fname: old_f}})
        log(f"{e['id']} {fname}")
    elif name == "set_signature":
        _revise(e, op=name, by=by, why=why, date=date, old={"signature": copy.deepcopy(e.get("signature"))})
        e["signature"] = copy.deepcopy(op.get("signature"))
        log(e["id"])
    elif name == "add_implication":
        e.setdefault("implications", []).append(copy.deepcopy(op.get("implication")))
        _revise(e, op=name, by=by, why=why, date=date)
        log(e["id"])
    elif name == "mark_transferable":
        if op.get("value") not in TRANSFERABLE:
            raise KnowledgeError(f"mark_transferable: one of {TRANSFERABLE}")
        e["transferable"] = op["value"]
        _revise(e, op=name, by=by, why=why, date=date)
        log(f"{e['id']} {op['value']}")
    i = doc["entries"].index(e)
    doc["entries"][i] = _ordered_entry(e)
    return doc


_OP_ARGS: dict[str, set[str]] = {
    "create_entry": {"entry", "id_prefix"},
    "add_observation": {"id", "observation"},
    "set_status": {"id", "status", "successor", "explained"},
    "edit_statement": {"id", "statement", "title"},
    "set_scope": {"id", "scope"},
    "add_link": {"id", "link"},
    "add_open_question": {"id", "q", "test", "owner_action"},
    "close_open_question": {"id", "qid", "answer"},
    "set_facet": {"id", "name", "value"},
    "set_signature": {"id", "signature"},
    "add_implication": {"id", "implication"},
    "mark_transferable": {"id", "value"},
    "add_unit_event": {"event"},
    "add_alias": {"alias", "device_path"},
    "set_standing_condition": {"condition"},
}


def new_unit(*, unit_id: str, hardware_id: str, aliases: Sequence[str], edid_id: Optional[str] = None,
             device_paths: Sequence[str] = (), model: Optional[str] = None,
             panel: Optional[dict] = None) -> dict:
    doc: dict[str, Any] = {"schema": SCHEMA, "rev": 0, "unit_id": unit_id,
                           "identity": {"hardware_id": hardware_id, "edid_id": edid_id,
                                        "device_paths": list(device_paths)},
                           "aliases": list(aliases)}
    if model:
        doc["model"] = model
    if panel is not None:
        doc["panel"] = dict(panel)
    doc.update({"standing_conditions": [], "unit_events": [], "revisions": [], "entries": []})
    return doc


def unit_from_header(h: dict) -> dict:
    """A new unit document from a header ``{unit_id?, identity{hardware_id, edid_id?, device_paths?}, aliases,
    model?, panel?}``; the unit id is derived from the identity when absent, and must match it when given."""
    if not isinstance(h, dict):
        raise KnowledgeError("unit header must be an object")
    bad = sorted(set(h) - {"unit_id", "identity", "aliases", "model", "panel"})
    if bad:
        raise KnowledgeError(f"unit header: unknown keys {bad}")
    ident = h.get("identity") or {}
    dps = list(ident.get("device_paths") or [])
    derived = unit_id_for(hardware_id=ident.get("hardware_id"), edid_id=ident.get("edid_id"),
                          device_path=dps[0] if dps else None)
    if h.get("unit_id") and h["unit_id"] != derived:
        raise KnowledgeError(f"unit header: unit_id {h['unit_id']!r} is not the id its identity gives ({derived!r})")
    doc = new_unit(unit_id=derived, hardware_id=ident.get("hardware_id"), edid_id=ident.get("edid_id"),
                   device_paths=dps, aliases=h.get("aliases") or [], model=h.get("model"), panel=h.get("panel"))
    errs = validate_unit(doc)
    if errs:
        raise KnowledgeError("unit header refused", errs)
    return doc


# ----------------------------------------------------------------------------- the store
def _dump(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


class KnowledgeStore:
    """The ``dip_knowledge/`` folder: one ``<unit_id>.json`` + generated ``<unit_id>.md`` per physical unit.

    ``base_dir`` (default: the folder's parent, where ``dip_store.json`` lives) anchors relative evidence paths."""

    def __init__(self, root: Path | str, *, base_dir: Optional[Path | str] = None) -> None:
        self.root = Path(root)
        self.base_dir = Path(base_dir) if base_dir is not None else self.root.parent

    @classmethod
    def beside(cls, dip_store: Path | str) -> "KnowledgeStore":
        p = Path(dip_store)
        return cls(p.parent / DIR_NAME, base_dir=p.parent)

    def path(self, unit_id: str) -> Path:
        if not _UNIT_ID_RE.match(unit_id):
            raise KnowledgeError(f"not a unit id: {unit_id!r}")
        return self.root / f"{unit_id}.json"

    def md_path(self, unit_id: str) -> Path:
        return self.root / f"{unit_id}.md"

    def unit_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.json") if _UNIT_ID_RE.match(p.stem))

    def read_raw(self, unit_id: str) -> Optional[dict]:
        """The parsed document, ``None`` when absent; :class:`CorruptFile` when it does not parse or validate."""
        p = self.path(unit_id)
        if not p.exists():
            return None
        try:
            doc = json.loads(read_text_shared(p))
        except (OSError, ValueError) as exc:
            raise CorruptFile(f"{p}: does not parse ({exc}); it is never overwritten: repair it by hand or move "
                              f"it aside") from exc
        errs = validate_unit(doc)
        if errs:
            raise CorruptFile(f"{p}: does not validate; it is never overwritten", errs)
        if doc.get("unit_id") != unit_id:
            raise CorruptFile(f"{p}: holds unit {doc.get('unit_id')!r}")
        return doc

    def load(self, unit_id: str) -> Optional[dict]:
        return self.read_raw(unit_id)

    def load_tolerant(self, unit_id: str) -> Optional[dict]:
        """For readers (digests, the tab): a missing or broken file is no knowledge, never a failure."""
        try:
            return self.read_raw(unit_id)
        except KnowledgeError:
            return None

    # -- resolution
    def resolve(self, name: str) -> str:
        """A unit id from a DLC display name (an alias), a DIP key (``name:MODE``), a unit id, an EDID id or a
        device path. Refuses an ambiguous or unknown name, listing what is known."""
        want = _norm_name(name)
        bare = _norm_name(name.rsplit(":", 1)[0]) if name.rsplit(":", 1)[-1].upper() in MODES else None
        hits: list[str] = []
        known: list[str] = []
        for uid in self.unit_ids():
            doc = self.load_tolerant(uid)
            names = {_norm_name(uid)}
            if doc:
                names |= {_norm_name(a) for a in doc.get("aliases") or []}
                ident = doc.get("identity") or {}
                if ident.get("edid_id"):
                    names.add(_norm_name(ident["edid_id"]))
                names |= {_norm_name(d) for d in ident.get("device_paths") or []}
                known.append(f"{uid} ({', '.join(doc.get('aliases') or [])})")
            else:
                known.append(f"{uid} (unreadable)")
            if want in names or (bare is not None and bare in names):
                hits.append(uid)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise KnowledgeError(f"{name!r} names more than one unit: {hits}; use the unit id")
        raise UnitNotFound(f"no unit known as {name!r} in {self.root}" + (f"; known: {'; '.join(known)}"
                                                                           if known else " (the folder is empty)"))

    # -- writes
    def write(self, unit_id: str, mutate: Callable[[dict], None], *, base_rev: Optional[int] = None,
              create: Optional[dict] = None, today: Optional[str] = None) -> dict:
        """Read -> ``mutate(doc)`` -> validate (whole file + append-only + size) -> CAS on ``rev`` under the lock ->
        atomic write -> re-render the view. ``base_rev`` = the rev the caller's decision was based on (None = the
        file as it is now). A changed file reloads and re-applies once, then :class:`ConcurrentWrite`."""
        p = self.path(unit_id)
        attempts = 2
        last_seen: Optional[int] = None
        while attempts > 0:
            current = self.read_raw(unit_id)
            if current is None:
                if create is None:
                    raise UnitNotFound(f"no knowledge file for unit {unit_id} ({p}); create it with a unit header")
                if create.get("unit_id") != unit_id:
                    raise KnowledgeError(f"create: header is for {create.get('unit_id')!r}, not {unit_id!r}")
            seen_rev = int(current.get("rev", 0)) if current is not None else 0
            if attempts == 2 and base_rev is not None and seen_rev != base_rev:
                attempts = 1               # the caller's base is stale: this application IS the one re-apply
            attempts -= 1
            new = copy.deepcopy(current if current is not None else create)
            mutate(new)
            new["rev"] = seen_rev + 1
            new = self._finish(current, new)
            with FileLock(p):
                on_disk = self.read_raw(unit_id)        # CorruptFile propagates: a broken file is never replaced
                last_seen = int(on_disk.get("rev", 0)) if on_disk is not None else None
                if (on_disk is None and current is None) or \
                        (on_disk is not None and current is not None and last_seen == seen_rev):
                    self.root.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(p, _dump(new))
                    atomic_write_text(self.md_path(unit_id), render(new, source=p.name, today=today))
                    return new
        raise ConcurrentWrite(f"{p}: changed under this write (now rev {last_seen}); re-read and decide again")

    def _finish(self, old: Optional[dict], new: dict) -> dict:
        errs = validate_unit(new)
        if old is not None:
            errs += check_append_only(old, new)
        if errs:
            raise KnowledgeError(f"unit {new.get('unit_id')}: refused, nothing written", errs)
        size = len(_dump(new).encode("utf-8"))
        if size > HARD_LIMIT_BYTES:
            raise KnowledgeError(f"unit {new.get('unit_id')}: {size} bytes over the hard limit {HARD_LIMIT_BYTES}")
        if size > SOFT_LIMIT_BYTES:
            print(f"warning: {new.get('unit_id')} is {size} bytes (soft limit {SOFT_LIMIT_BYTES}): fold or "
                  f"supersede entries", file=sys.stderr)
        return new

    def apply_ops(self, unit_id: str, ops: Sequence[dict], *, base_rev: Optional[int] = None,
                  create: Optional[dict] = None, today: Optional[str] = None) -> dict:
        """Validate and apply ``ops`` as one write (all or nothing)."""
        if not isinstance(ops, (list, tuple)) or not ops:
            raise KnowledgeError("no ops")

        def mutate(doc: dict) -> None:
            for i, op in enumerate(ops):
                try:
                    apply_op(doc, op, base_dir=self.base_dir, today=today)
                except KnowledgeError as exc:
                    raise KnowledgeError(f"op {i + 1} ({op.get('op') if isinstance(op, dict) else op}): {exc}",
                                         exc.violations) from exc
        return self.write(unit_id, mutate, base_rev=base_rev, create=create, today=today)

    def render_all(self, *, today: Optional[str] = None) -> list[Path]:
        out = []
        for uid in self.unit_ids():
            doc = self.read_raw(uid)
            if doc is not None:
                p = self.md_path(uid)
                atomic_write_text(p, render(doc, source=self.path(uid).name, today=today))
                out.append(p)
        return out


def validate_ops(doc: dict, ops: Sequence[dict], *, base_dir: Optional[Path] = None,
                 today: Optional[str] = None) -> list[str]:
    """Dry run (the S3 seam-payload check): the violations ``ops`` would cause on ``doc``; [] = acceptable."""
    new = copy.deepcopy(doc)
    try:
        for op in ops:
            apply_op(new, op, base_dir=base_dir, today=today)
    except KnowledgeError as exc:
        return [str(exc).splitlines()[0]] + exc.violations
    new["rev"] = int(doc.get("rev", 0)) + 1
    return validate_unit(new) + check_append_only(doc, new)


# ----------------------------------------------------------------------------- observation context (match input)
OBS_CONTEXT_KEYS = frozenset({"mode", "domain", "codes", "levels_nits", "stimulus", "stack", "refresh_hz",
                              "instrument", "thermal", "observations"})
OBS_ITEM_KEYS = frozenset({"code", "level_nits", "channel", "quantity", "dev_pct", "dxy", "sigma_pct", "reference",
                           "ratio", "u_ln", "nits", "x", "y"})


def validate_observation_context(ctx: Any) -> list[str]:
    """The ``observation_context`` shape a flow declares at a seam (design note §4.1)."""
    out: list[str] = []
    if not isinstance(ctx, dict):
        return ["observation context: must be an object"]
    _unknown_keys(ctx, OBS_CONTEXT_KEYS, "context", out)
    if ctx.get("mode") not in MODES:
        out.append(f"context.mode: one of {MODES}")
    if "domain" in ctx and ctx["domain"] not in DOMAINS:
        out.append(f"context.domain: one of {DOMAINS}")
    for k in ("codes", "levels_nits"):
        if k in ctx and not _range2(ctx[k]):
            out.append(f"context.{k}: [lo, hi]")
    if "stimulus" in ctx:
        if not isinstance(ctx["stimulus"], dict):
            out.append("context.stimulus: an object")
        else:
            _unknown_keys(ctx["stimulus"], STIMULUS_KEYS, "context.stimulus", out)
    if "refresh_hz" in ctx and not _is_num(ctx["refresh_hz"]):
        out.append("context.refresh_hz: a number")
    if "thermal" in ctx and not isinstance(ctx["thermal"], dict):
        out.append("context.thermal: an object")
    obs = ctx.get("observations")
    if not isinstance(obs, list) or not obs:
        out.append("context.observations: a non-empty list")
        obs = []
    for i, o in enumerate(obs):
        w = f"context.observations[{i}]"
        if not isinstance(o, dict):
            out.append(f"{w}: an object")
            continue
        _unknown_keys(o, OBS_ITEM_KEYS, w, out)
        if "quantity" in o and o["quantity"] not in QUANTITIES + ("ratio", "offset", "level"):
            out.append(f"{w}.quantity: light, balance, ratio, offset or level")
        if "code" not in o and "level_nits" not in o:
            out.append(f"{w}: needs a 'code' or a 'level_nits'")
        for k in ("code", "level_nits", "dev_pct", "sigma_pct", "ratio", "u_ln", "nits", "x", "y"):
            if k in o and not _is_num(o[k]):
                out.append(f"{w}.{k}: a number")
        if "dxy" in o and not (isinstance(o["dxy"], list) and len(o["dxy"]) == 2 and all(_is_num(x)
                                                                                         for x in o["dxy"])):
            out.append(f"{w}.dxy: [dx, dy]")
    return out


# ----------------------------------------------------------------------------- render
def _fmt(v: Any) -> str:
    if v is None:
        return "unknown"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_fmt(x)}" for k, x in v.items()) + "}"
    return str(v)


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _scope_lines(scope: dict) -> list[str]:
    out = []
    for k in ("mode", "domain", "code_range", "level_nits", "stimulus", "panel_settings", "refresh_hz", "stack",
              "thermal", "instrument", "link_bpc"):
        if k in scope:
            out.append(f"- {k}: {_fmt(scope[k])}")
    unk = scope.get("unknown") or []
    out.append("- **not tested**: " + ("; ".join(unk) if unk else "(none named)"))
    return out


def _entry_md(doc: dict, e: dict, today: str) -> list[str]:
    st = entry_state(doc, e, today)
    lines = [f"### {e['id']} — {e['title']}", ""]
    status = f"**{e['status']}**"
    if e.get("kind") == "event":
        status += " · " + ("explained" if e.get("explained") else "**not explained**")
    if st["unconfirmed"]:
        status += " · **UNCONFIRMED** (" + "; ".join(st["unconfirmed_why"]) + ")"
    if st["dormant"]:
        status += " · dormant"
    status += (f" · seen {st['observations']}× ({st['independent']} independent)"
               f" · last confirmed {e.get('last_confirmed') or '—'} · rev {e.get('rev')}")
    lines.append(status)
    lines.append("")
    lines.append("Can explain an observation: " + ("yes (still only evidence; never a gate)" if st["can_explain"]
                                                    else "no — " + "; ".join(st["cannot_explain_why"])))
    lines.append("")
    lines.append(e["statement"])
    lines.append("")
    lines.append("Scope:")
    lines.extend(_scope_lines(e.get("scope") or {}))
    lines.append("")
    sig = e.get("signature") or {"type": "none"}
    lines.append(f"Signature: `{sig.get('type')}`" + (" " + _fmt({k: v for k, v in sig.items() if k != "type"})
                                                      if len(sig) > 1 else ""))
    for name, val in (e.get("facets") or {}).items():
        lines.append(f"Facet `{name}` (read by {FACETS.get(name, ('', '', '?'))[2]}): {_fmt(val)}")
    lines.append("")
    obs = e.get("observations") or []
    if obs or e.get("rollup"):
        lines.append("| obs | date | verdict | ind. | by | source | numbers |")
        lines.append("|---|---|---|---|---|---|---|")
        for o in obs:
            src = o.get("source") or {}
            where = src.get("path") or src.get("ref") or ""
            sha = f" (sha256 {src['sha256'][:12]}…, {src.get('hash_of')})" if src.get("sha256") else \
                " (no evidence file)"
            cond = f"; conditions {_fmt(o['conditions'])}" if o.get("conditions") else ""
            note = f"; {o['note']}" if o.get("note") else ""
            lines.append(f"| {o['id']} | {o['date']} | {o['verdict']} | {'yes' if o.get('independent') else 'no'} "
                         f"| {_md_cell(o.get('by', ''))} | {_md_cell(src.get('kind', ''))}: `{_md_cell(where)}`"
                         f"{sha} | {_md_cell(_fmt(o.get('numbers') or {}) + cond + note)} |")
        r = e.get("rollup")
        if r:
            lines.append(f"| rollup | {r['first']} … {r['last']} | {_fmt(r.get('verdicts') or {})} | "
                         f"{r.get('independent', 0)} | | {r['count']} folded observations | "
                         f"{_md_cell(_fmt(r.get('numbers') or {}))} |")
        lines.append("")
    for im in e.get("implications") or []:
        lines.append(f"- Implication for {', '.join(im['flows'])}: {im['text']}")
    for ln in e.get("links") or []:
        rel = ln.get("rel", "")
        tgt = ln.get("id") or ln.get("field")
        lines.append(f"- Link: {rel.rstrip('?')} {tgt}" + (" (hypothesised link)" if rel.endswith("?") else ""))
    for q in e.get("open_questions") or []:
        if q.get("status") == "closed":
            lines.append(f"- Closed question {q['id']}: {q['q']} → {q.get('answer')} ({q.get('closed')}, "
                         f"{q.get('closed_by')})")
    if e.get("transferable") and e["transferable"] != "no":
        lines.append(f"- Transferable to other units of this model: {e['transferable']}")
    lines.append(f"- Created {e.get('created')}; history: " + "; ".join(
        f"{h['date']} {h['status']} ({h['by']}: {h['why']})" for h in e.get("status_history") or []))
    lines.append("")
    return lines


def render(doc: dict, *, source: str = "", today: Optional[str] = None, mode: Optional[str] = None,
           only: Optional[str] = None) -> str:
    """The per-unit Markdown view: identity and aliases; standing conditions; open questions first (what is owed);
    then panel, rig and event entries; refuted / superseded entries last. ``mode`` / ``only`` filter entries."""
    t = today or today_iso()
    ident = doc.get("identity") or {}
    entries = [e for e in doc.get("entries") or [] if (only is None or e.get("id") == only)
               and (mode is None or not (e.get("scope") or {}).get("mode") or mode in e["scope"]["mode"])]
    lines = [f"<!-- generated from {source or doc.get('unit_id') + '.json'} rev {doc.get('rev')}, do not edit -->",
             f"# {doc.get('model') or ', '.join(doc.get('aliases') or [])} — unit {doc.get('unit_id')}", "",
             "What DLC knows about this physical display: read this BEFORE judging an anomaly on it. An entry "
             "explains an observation only when it is measured, seen in two independent sessions, still confirmed, "
             "measured under the conditions that apply, and the observation falls inside its signature; otherwise "
             "it is a resemblance at most. Nothing here changes a gate.", "",
             f"- Unit id: `{doc.get('unit_id')}`; hardware id `{ident.get('hardware_id')}`; EDID id "
             f"`{ident.get('edid_id')}`",
             f"- Known as: {', '.join(doc.get('aliases') or [])}"]
    for dp in ident.get("device_paths") or []:
        lines.append(f"- Device path: `{dp}`")
    if doc.get("panel"):
        lines.append(f"- Panel: {_fmt(doc['panel'])}")
    if doc.get("standing_conditions"):
        lines.append("- Standing conditions (the owner's everyday monitor settings; every entry is measured under "
                     "them unless its scope says otherwise):")
        for c in doc["standing_conditions"]:
            lines.append(f"  - {c['setting']}: {c['value']} — {c['source']}"
                         + ("" if c.get("verified") or "unverified" in c["source"] else " (not verified by DLC)"))
    for ev in doc.get("unit_events") or []:
        lines.append(f"- Unit event {ev['date']}: {ev['what']} ({ev['by']}) — older entries are unconfirmed until "
                     f"re-observed")
    lines.append("")
    owed = [(e, q) for e in entries for q in e.get("open_questions") or [] if q.get("status") == "open"
            and e.get("status") not in ("refuted", "superseded")]
    lines.append("## Open questions")
    lines.append("")
    if owed:
        for e, q in sorted(owed, key=lambda eq: (eq[1].get("owner_action", False), eq[0]["id"], eq[1]["id"])):
            lines.append(f"- {e['id']} {q['id']}{' (needs the owner)' if q.get('owner_action') else ''}: {q['q']} "
                         f"Test: {q['test']}")
    else:
        lines.append("- none")
    lines.append("")
    for kind, title in (("panel", "Panel"), ("rig", "Rig"), ("event", "Events")):
        group = [e for e in entries if e.get("kind") == kind and e.get("status") not in ("refuted", "superseded")]
        lines.append(f"## {title}")
        lines.append("")
        if not group:
            lines.append("- none")
            lines.append("")
        for e in group:
            lines.extend(_entry_md(doc, e, t))
    gone = [e for e in entries if e.get("status") in ("refuted", "superseded")]
    lines.append("## Refuted and superseded (kept so nobody re-proposes them)")
    lines.append("")
    if not gone:
        lines.append("- none")
        lines.append("")
    for e in gone:
        lines.extend(_entry_md(doc, e, t))
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    from .knowledge_cli import _main

    raise SystemExit(_main())
