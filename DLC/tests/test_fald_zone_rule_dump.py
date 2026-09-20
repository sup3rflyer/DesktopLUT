"""A panel file with the boost zone rule 1 (LIT-or-MEAN, work guide C12b) on a DesktopLUT build from BEFORE C12b is not
refused — the old loader never read the then-reserved word 53 — and the layer silently applies the legacy LIT-or-DIM rule.
The only evidence is the dump: such a build writes no ``boost_rule`` line. dlc/fald/panelfile.py dump_zone_rule_reason
flags it; the verify path (dlc/stages/fald_profile.py zone_rule_dump_check) takes one dump on the first ON read."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dlc.fald.export import export_panel_params
from dlc.fald.model import FaldModel, FaldParams
from dlc.fald.panelfile import dump_zone_rule_reason, read_dump_meta, read_panel_file
from dlc.stages import fald_profile as FP

LUT = ((0.0, 1.165), (0.2, 1.08), (0.4, 1.0))
_C12B_DUMP = ("width 960\nheight 540\ncols 12\nrows 12\nboost_in_file 1\nboost_steps 3\nzones_total 144\nboost_lit_nits 0.35\n"
              "boost_rule 1 (0 = LIT-or-DIM, 1 = LIT-or-MEAN)\nboost_mean_gamma 0.55\nboost_mean_thresh 0.08\n"
              "active_zones_r0 4\nparams C:\\p.bin\nframes_run 12\n")
_OLD_DUMP = "".join(line + "\n" for line in _C12B_DUMP.splitlines() if not line.startswith(("boost_rule", "boost_mean_")))


def _panel(tmp_path: Path, rule: str, lut=LUT) -> Path:
    p = FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, tmin=1.5e-3)
    p = replace(p, boost_lut=lut, boost_rule=rule, boost_mean_gamma=0.55, boost_mean_thresh=0.08)
    path = tmp_path / f"panel_{rule}_{len(lut)}.bin"
    export_panel_params(FaldModel(p), path)
    return path


def test_a_rule_1_file_with_a_dump_that_has_no_boost_rule_line_is_flagged(tmp_path):
    mean, dim, none = (read_panel_file(_panel(tmp_path, r, l)) for r, l in (("mean", LUT), ("dim", LUT), ("mean", ())))
    assert mean["boostRule"] == 1 and dim["boostRule"] == 0 and not none["hasBoost"]
    (tmp_path / "fald_dump.txt").write_text(_OLD_DUMP, encoding="utf-8")
    old = read_dump_meta(tmp_path)                                    # a directory or the file itself
    assert old == read_dump_meta(tmp_path / "fald_dump.txt") and old["cols"] == "12" and "boost_rule" not in old
    why = dump_zone_rule_reason(mean, old)
    assert why and "no `boost_rule` line" in why and "before C12b" in why and "legacy" in why
    assert dump_zone_rule_reason(dim, old) is None                    # the legacy rule IS what an old exe applies
    assert dump_zone_rule_reason(none, old) is None                   # no boost LUT: no zone is ever counted
    (tmp_path / "fald_dump.txt").write_text(_C12B_DUMP, encoding="utf-8")
    new = read_dump_meta(tmp_path)
    assert new["boost_rule"].startswith("1 ") and dump_zone_rule_reason(mean, new) is None
    other = dict(new, boost_rule="0 (0 = LIT-or-DIM, 1 = LIT-or-MEAN)")
    assert "reports boost_rule 0" in dump_zone_rule_reason(mean, other)
    assert "boost_mean_gamma 0.62" in dump_zone_rule_reason(mean, dict(new, boost_mean_gamma="0.62"))   # another file ran
    assert "no readable `boost_mean_thresh`" in dump_zone_rule_reason(mean, {k: v for k, v in new.items() if k != "boost_mean_thresh"})
    assert "unreadable" in dump_zone_rule_reason(mean, dict(new, boost_rule="?"))


class _DumpingCtl:
    """runtime.fald_dump answered like the app: the render thread writes fald_dump.txt a moment later (here: at once)."""

    def __init__(self, text: str | None, refuse: bool = False):
        self.text, self.refuse, self.calls = text, refuse, []

    def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        assert method == "runtime.fald_dump", method
        if self.refuse:
            raise RuntimeError("unknown method: runtime.fald_dump")
        if self.text is not None:
            (Path(params["dir"]) / "fald_dump.txt").write_text(self.text, encoding="utf-8")
        return {"dir": params["dir"]}


def test_verify_path_takes_one_dump_and_classifies_it(tmp_path):
    vc = FP.VirtualClock()
    kw = {"sleep": vc.sleep, "now": vc.now}
    mean, dim = str(_panel(tmp_path, "mean")), str(_panel(tmp_path, "dim"))
    ctl = _DumpingCtl(_C12B_DUMP)
    assert FP.zone_rule_dump_check(ctl, 0, "HDR", mean, tmp_path / "zr", **kw) == ("ok", None)
    assert ctl.calls == [("runtime.fald_dump", {"monitor": 0, "mode": "HDR", "dir": str(tmp_path / "zr")})]
    status, why = FP.zone_rule_dump_check(_DumpingCtl(_OLD_DUMP), 0, "HDR", mean, tmp_path / "zr", **kw)
    assert status == "legacy" and "before C12b" in why
    ctl = _DumpingCtl(_OLD_DUMP)
    assert FP.zone_rule_dump_check(ctl, 0, "HDR", dim, tmp_path / "zr", **kw) == ("n/a", None) and ctl.calls == []   # nothing to check
    # a stale dump of an earlier check must not answer for this one; no dump / a refused verb = UNCONFIRMED (the LLM judges)
    status, why = FP.zone_rule_dump_check(_DumpingCtl(None), 0, "HDR", mean, tmp_path / "zr", **kw)
    assert status == "unconfirmed" and "no fald_dump.txt" in why and not (tmp_path / "zr" / "fald_dump.txt").exists()
    status, why = FP.zone_rule_dump_check(_DumpingCtl(None, refuse=True), 0, "HDR", mean, tmp_path / "zr", **kw)
    assert status == "unconfirmed" and "refused" in why
    status, why = FP.zone_rule_dump_check(_DumpingCtl(_C12B_DUMP), 0, "HDR", str(tmp_path / "missing.bin"), tmp_path / "zr", **kw)
    assert status == "unconfirmed" and "cannot read" in why
    # a half-written dump (the `params` line is the last one DumpFields writes) is not read as "no boost_rule line"
    half = _C12B_DUMP[:_C12B_DUMP.index("boost_rule")]
    status, why = FP.zone_rule_dump_check(_DumpingCtl(half), 0, "HDR", mean, tmp_path / "zr", **kw)
    assert status == "unconfirmed"
