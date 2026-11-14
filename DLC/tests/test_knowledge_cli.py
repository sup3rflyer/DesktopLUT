"""DIP knowledge stage S2: the command line end to end on temp folders (``python -m dlc.knowledge``).

Nothing here touches DLC's own dip_store.json or a dip_knowledge folder beside it."""

from __future__ import annotations

import json
from pathlib import Path

from dlc import knowledge_cli as cli
from test_knowledge import BY, TODAY, _header, _hyp, _measured, ev  # noqa: F401  (ev is a fixture)


def _cli(tmp_path: Path, *argv: str) -> int:
    return cli._main(["--dir", str(tmp_path / "kn"), "--base-dir", str(tmp_path), "--today", TODAY, *argv])


def test_cli_end_to_end(tmp_path: Path, ev: Path, capsys):
    payload = {"by": BY, "why": "first entries", "unit": _header(), "entries": [_measured(ev), _hyp()]}
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    assert _cli(tmp_path, "add", "--file", str(f), "--display", "Somebody Else", "--create-unit") == 2
    assert "is not a name of the unit" in capsys.readouterr().err
    assert _cli(tmp_path, "add", "--file", str(f)) == 2                  # no display, no --create-unit
    capsys.readouterr()
    assert _cli(tmp_path, "add", "--file", str(f), "--create-unit") == 0
    assert "rev 1 (2 entries)" in capsys.readouterr().out
    assert _cli(tmp_path, "show", "--display", "Test Panel 32:SDR") == 0
    out = capsys.readouterr().out
    assert out.startswith("<!-- generated from TST1234-12345.json rev 1") and "### F-0001" in out
    assert _cli(tmp_path, "show", "--display", "Test Panel 32", "--id", "H-0001") == 0
    assert "### F-0001" not in capsys.readouterr().out

    obs = tmp_path / "obs.json"
    obs.write_text(json.dumps({"date": "2026-11-15", "verdict": "matches", "independent": True,
                               "source": {"kind": "window read", "path": str(ev)}, "numbers": {"max_dev_pct": 3.9}}),
                   encoding="utf-8")
    assert _cli(tmp_path, "add-observation", "--display", "Test Panel 32", "--id", "F-0001", "--file", str(obs),
                "--by", "llm:coordinator", "--why", "window read agrees") == 0
    assert "F-0001 now has 3 observations" in capsys.readouterr().out
    assert _cli(tmp_path, "set-status", "--display", "Test Panel 32", "--id", "H-0001", "--status", "refuted",
                "--by", "llm:coordinator", "--why", "teeth unchanged in a window", "--base-rev", "2") == 0
    capsys.readouterr()
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({"by": "owner", "why": "told the LLM", "ops": [
        {"op": "add_unit_event", "event": {"date": "2026-11-16", "what": "OSD factory reset"}}]}), encoding="utf-8")
    assert _cli(tmp_path, "amend", "--display", "Test Panel 32", "--file", str(ops)) == 0
    capsys.readouterr()
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"mode": "SDR", "domain": "sdr_drive_code10", "codes": [130, 179],
                               "stimulus": {"area_pct": 100},
                               "observations": [{"code": 148, "quantity": "light", "dev_pct": -7.6,
                                                 "dxy": [0.0021, -0.0053], "sigma_pct": 0.3}]}), encoding="utf-8")
    assert _cli(tmp_path, "match", "--display", "Test Panel 32", "--file", str(ctx)) == 0
    assert "Matchers arrive in S3" in capsys.readouterr().out
    ctx.write_text(json.dumps({"mode": "SDR", "observations": [{"quantity": "light", "hue": 3}]}), encoding="utf-8")
    assert _cli(tmp_path, "match", "--display", "Test Panel 32", "--file", str(ctx)) == 2
    err = capsys.readouterr().err
    assert "unknown key 'hue'" in err and "needs a 'code' or a 'level_nits'" in err
    assert _cli(tmp_path, "validate") == 0
    assert "ok TST1234-12345 rev 4" in capsys.readouterr().out
    assert _cli(tmp_path, "validate", "--evidence") == 0
    capsys.readouterr()
    ev.write_text("changed", encoding="utf-8")
    assert _cli(tmp_path, "validate", "--evidence") == 1
    assert "changed since it was recorded" in capsys.readouterr().out
    md = tmp_path / "kn" / "TST1234-12345.md"
    md.unlink()
    assert _cli(tmp_path, "render") == 0
    view = md.read_text(encoding="utf-8")
    assert "UNCONFIRMED" in view and "OSD factory reset" in view
    assert _cli(tmp_path, "show", "--display", "Nobody") == 2
    assert "no unit known as 'Nobody'" in capsys.readouterr().err


def test_cli_default_folder_is_beside_the_dip_store(tmp_path: Path, ev: Path, capsys):
    payload = {"by": BY, "why": "x", "unit": _header(), "entries": [_hyp(links=[])]}
    f = tmp_path / "p.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    dip = tmp_path / "profile" / "dip_store.json"
    assert cli._main(["--dip", str(dip), "--today", TODAY, "add", "--file", str(f), "--create-unit"]) == 0
    assert (tmp_path / "profile" / "dip_knowledge" / "TST1234-12345.json").exists()
    assert (tmp_path / "profile" / "dip_knowledge" / "TST1234-12345.md").exists()
    assert not (tmp_path / "profile" / "dip_store.json").exists()     # the DIP store itself is not touched
