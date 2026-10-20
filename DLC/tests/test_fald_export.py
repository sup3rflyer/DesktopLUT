"""The binary panel-parameter file for the DesktopLUT FALD shader round-trips the reference tables; its zone lattice
(whole-pixel cells or, since 2026-10-19, the extent of a fractional pitch — docs/fald-fractional-pitch.md)."""
import struct
import numpy as np
import pytest
from dlc.fald.model import FaldModel, FaldParams
from dlc.fald.export import export_panel_params, kernel_tables, drive_curve_lut, MAGIC, CURVE_LOG_MIN, CURVE_LOG_MAX


def _model():
    return FaldModel(FaldParams(est_phase_px=-18.0, est_phase_py=-24.5, est_aniso=0.85, est_support_cells=5,
                                kernel_pnorm=1.75, core_mm=10.8, tail_mm=32.4, tail_frac=0.45))


def test_export_roundtrip(tmp_path):
    m = _model()
    info = export_panel_params(m, tmp_path / "panel.bin", (0.25, 4.0))
    b = (tmp_path / "panel.bin").read_bytes()
    assert len(b) == info["bytes"]
    ints = struct.unpack("<13I", b[:52]); floats = struct.unpack("<13f", b[52:104])
    assert ints[0] == MAGIC and ints[1:4] == (m.p.cols, m.p.rows, m.p.sub)
    assert ints[4:6] == (80, 45)
    assert abs(floats[0] - m.p.white_nits) < 1e-3 and abs(floats[2] - m.p.stat_area0_px2) < 1e-3
    kt, ke = kernel_tables(m)
    rt_c, rt_r, re_c, re_r, n = ints[8], ints[9], ints[10], ints[11], ints[12]
    assert kt.shape == (m.p.sub, m.p.sub, 2 * rt_r + 1, 2 * rt_c + 1)
    assert ke.shape == (m.p.sub, m.p.sub, 2 * re_r + 1, 2 * re_c + 1)
    off = 128
    curve = np.frombuffer(b[off:off + 4 * n], np.float32); off += 4 * n
    assert np.allclose(curve, drive_curve_lut(m))
    kt2 = np.frombuffer(b[off:off + 4 * kt.size], np.float32).reshape(kt.shape); off += 4 * kt.size
    ke2 = np.frombuffer(b[off:], np.float32).reshape(ke.shape)
    assert np.array_equal(kt2, kt) and np.array_equal(ke2, ke)
    # kernels are the model's own: unit mean sum over sub-offsets (a full field gives B = 1)
    assert abs(kt.sum(axis=(2, 3)).mean() - 1.0) < 1e-5
    assert abs(ke.sum(axis=(2, 3)).mean() - 1.0) < 1e-5


def test_curve_lut_matches_drive_of():
    m = _model()
    curve = drive_curve_lut(m)
    ln = np.linspace(CURVE_LOG_MIN, CURVE_LOG_MAX, len(curve))
    for nits in (0.3, 2.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 1842.0):
        lut = float(np.interp(np.log(nits), ln, curve))
        ref = float(m.drive_of(np.array([nits]))[0])
        assert abs(lut - ref) < 2e-3, (nits, lut, ref)


def test_lattice_fit_matches_the_export_arithmetic():
    """The preflight's lattice check uses the export's own arithmetic (header_lattice_words): since fractional zone pitch
    (docs/fald-fractional-pitch.md) the lattice IS the frame — whole-pixel cells, or the extent of a rational pitch."""
    from dlc.fald.export import lattice_fit
    pa = lattice_fit(3840, 2160, 48, 48)                      # the PA32UCXR: whole-pixel cells, words 4/5 as ever
    assert pa["exact"] and pa["fits"] and pa["cell_px"] == [80, 45] and pa["uncovered_px"] == [0, 0]
    assert not pa["fractional"] and pa["header_cell_px"] == [80, 45] and pa["extent_word"] == 0
    assert pa["model_canvas_px"] == [3840, 2160] and pa["model_aspect_error"] == 0.0
    for (w, h, c, r) in ((2560, 1440, 48, 24),                # 27" 1440p 1152 zones, 53.33 x 60 px (was refused: 2640 px)
                         (2560, 1440, 36, 32),                # its alternative, 71.11 x 45 px (drifted 40 px)
                         (3440, 1440, 48, 24),                # 34" ultrawide, 71.67 x 60 px (drifted 80 px)
                         (1920, 1080, 31, 23),                # an odd TV grid
                         (2560, 1440, 40, 25)):               # an integer 64-px pitch with fractional 57.6-px rows
        lat = lattice_fit(w, h, c, r)
        assert lat["fits"] and lat["exact"] and lat["fractional"] and lat["refused"] is None, (w, h, c, r)
        assert lat["lattice_px"] == [w, h] and lat["uncovered_px"] == [0, 0] and lat["max_drift_px"] == [0.0, 0.0]
        assert lat["header_cell_px"] == [0, 0] and lat["extent_word"] == w | (h << 16)
        assert lat["zone_px"] == [[w // c, -(-w // c)], [h // r, -(-h // r)]]
        assert lat["pitch_px"] == [round(w / c, 4), round(h / r, 4)]
    qhd = lattice_fit(2560, 1440, 48, 24)
    assert qhd["model_canvas_px"] == [2640, 1440] and qhd["model_aspect_error"] == pytest.approx(2640 / 2560 - 1, abs=1e-5)
    for bad, why in (((2560, 1440, 600, 24), "512"), ((300, 200, 400, 24), "fewer pixels"), ((70000, 1440, 48, 24), "65535"),
                     ((2560, 1440, 0, 24), "512")):
        lat = lattice_fit(*bad)
        assert not lat["fits"] and not lat["exact"] and why in lat["refused"], bad


def test_export_writes_the_lattice_for_the_monitor_frame(tmp_path):
    """The run's geometry on a frame choose_scale cannot render exactly (34" UW 3440 x 1440, 48 x 24: a 3360 x 1440
    canvas): with ``frame`` the file carries the frame's lattice extent and A0 in frame px²; without it, the canvas's
    whole-pixel lattice (what the file always carried)."""
    from dlc.fald import profile as P
    from dlc.fald.export import export_panel_params, lattice_fit
    from dlc.fald.model import FaldModel
    from dlc.fald.panelfile import read_panel_file
    g = P.PanelGeometry.from_diagonal(3440, 1440, 48, 24, 34.0, meter=(1720, 720), white_nits=1000.0)
    p = g.base_params(est_phase_px=-21.0, est_phase_py=-12.0)
    assert (p.width, p.height) == (3360, 1440)
    m = FaldModel(p)
    native = export_panel_params(m, tmp_path / "uw.bin", frame=(3440, 1440))
    canvas = export_panel_params(m, tmp_path / "uw_canvas.bin")
    lat = lattice_fit(3440, 1440, 48, 24)
    assert native["header_ints"][4:6] == lat["header_cell_px"] == [0, 0]
    assert native["lattice"] == {"frame_px": [3440, 1440], "canvas_px": [3360, 1440], "header_cell_px": [0, 0],
                                 "extent_word": lat["extent_word"], "fractional": True,
                                 "area0_px2": float(np.float32(p.stat_area0_px2 * (3440 / 3360) * (1440 / 1440)))}
    assert canvas["header_ints"][4:6] == [70, 60] and canvas["lattice"]["extent_word"] == 0
    a, b = (tmp_path / "uw.bin").read_bytes(), (tmp_path / "uw_canvas.bin").read_bytes()
    w = struct.unpack("<32I", a[:128])
    fa, fb = struct.unpack("<32f", a[:128]), struct.unpack("<32f", b[:128])
    assert w[4:6] == (0, 0) and w[31] == 3440 | (1440 << 16) and struct.unpack("<I", b[124:128])[0] == 0
    assert fa[15] == np.float32(p.stat_area0_px2 * (3440 / 3360) * (1440 / 1440))   # A0: canvas px² -> frame px²
    assert fa[24] == np.float32(-21.0 * (3440 / 3360)) and fa[25] == np.float32(-12.0)   # informational phase words, frame px
    assert fb[15] == np.float32(p.stat_area0_px2) and fb[24] == np.float32(-21.0)
    # every other header word and every table is the canvas export's: the kernels are in CELL units
    diff = [i for i in range(32) if a[4 * i: 4 * i + 4] != b[4 * i: 4 * i + 4]]
    assert diff == [4, 5, 15, 24, 31] and a[128:] == b[128:]
    o = read_panel_file(tmp_path / "uw.bin")
    assert (o["latW"], o["latH"], o["cellW"], o["cellH"], o["fractional"]) == (3440, 1440, 72, 60, True)
    oc = read_panel_file(tmp_path / "uw_canvas.bin")
    assert (oc["latW"], oc["latH"], oc["cellW"], oc["cellH"], oc["fractional"]) == (3360, 1440, 70, 60, False)


def test_export_of_an_integer_lattice_is_byte_identical(tmp_path):
    """A frame that divides evenly (the PA32UCXR) keeps the whole-pixel words and a zero word 31: ``frame`` = the canvas
    and no ``frame`` write the same bytes, and the header carries exactly what the pre-2026-10-19 exporter wrote."""
    m = _model()
    a = export_panel_params(m, tmp_path / "a.bin")
    b = export_panel_params(m, tmp_path / "b.bin", frame=(3840, 2160))
    assert (tmp_path / "a.bin").read_bytes() == (tmp_path / "b.bin").read_bytes()
    raw = (tmp_path / "a.bin").read_bytes()
    w, f = struct.unpack("<32I", raw[:128]), struct.unpack("<32f", raw[:128])
    assert w[4:8] == (80, 45, 0, 0) and w[31] == 0
    assert f[15] == np.float32(m.p.stat_area0_px2) and f[24] == np.float32(m.p.est_phase_px) and f[25] == np.float32(m.p.est_phase_py)
    assert a["lattice"]["fractional"] is False and b["lattice"]["header_cell_px"] == [80, 45]


def test_export_refuses_a_lattice_the_loader_would_refuse(tmp_path):
    m = _model()
    for frame in ((40, 2160), (70000, 2160)):                 # fewer pixels than zones; over the 16-bit extent
        with pytest.raises(ValueError):
            export_panel_params(m, tmp_path / "never.bin", frame=frame)
    assert not (tmp_path / "never.bin").exists()


def test_zone_mapping_twins_partition_by_pixel_centre():
    """export.zone_edge / zone_of_pixel = C++ FaldZoneEdge / FaldZoneOfPixel = HLSL ZoneEdgeX / ZoneOfPixel: every pixel's
    zone holds it and its centre; zones are floor / ceil of the pitch; an integer pitch gives c * P and px // P."""
    from fractions import Fraction
    from dlc.fald.export import zone_edge, zone_of_pixel
    for n, e in ((48, 2560), (36, 2560), (24, 1440), (32, 1440), (48, 3440), (31, 1920), (23, 1080), (48, 3840), (7, 373),
                 (24, 2556), (1, 1), (5, 5)):
        edges = [zone_edge(c, n, e) for c in range(n + 1)]
        assert edges[0] == 0 and edges[-1] == e
        assert all(b - a in (e // n, -(-e // n)) for a, b in zip(edges, edges[1:]))
        for c, x0 in enumerate(edges):                        # c * pitch rounded to the nearest edge, ties to the lower one
            t = Fraction(c * e, n)
            assert x0 - Fraction(1, 2) < t <= x0 + Fraction(1, 2)
        for px in range(e):
            z = zone_of_pixel(px, n, e)
            assert edges[z] <= px < edges[z + 1]
            assert z <= Fraction(2 * px + 1, 2) * n / e < z + 1
            if e % n == 0:
                assert z == px // (e // n) and edges[z] == z * (e // n)
    assert zone_edge(1, 24, 2556) == 106 and zone_of_pixel(106, 24, 2556) == 1   # a tie (centre on the boundary): upper zone


def test_panelfile_reads_the_lattice_like_the_cpp_loader(tmp_path):
    """dlc.fald.panelfile (the loader's Python twin) takes the extent word and refuses what ParseFaldPanelParams refuses."""
    from dlc.fald.panelfile import cb, read_panel_file
    m = _model()
    export_panel_params(m, tmp_path / "ok.bin")
    base = bytearray((tmp_path / "ok.bin").read_bytes())

    def patched(words, name):
        b = bytearray(base)
        for i, v in words.items():
            b[4 * i: 4 * i + 4] = struct.pack("<I", v)
        path = tmp_path / name
        path.write_bytes(bytes(b))
        return path

    o = read_panel_file(patched({4: 0, 5: 0, 31: 2560 | (1440 << 16)}, "frac.bin"))   # 48 x 48 zones over 2560 x 1440
    assert (o["latW"], o["latH"], o["cellW"], o["cellH"], o["fractional"]) == (2560, 1440, 54, 30, True)
    assert cb(o)["latWCB"] == 2560 and cb(o)["latHCB"] == 1440
    o = read_panel_file(patched({4: 0, 5: 0, 31: 3840 | (2160 << 16)}, "int_ext.bin"))  # an integer lattice as an extent
    assert (o["latW"], o["latH"], o["cellW"], o["cellH"], o["fractional"]) == (3840, 2160, 80, 45, False)
    for words, why in (({4: 0, 5: 0, 31: 0}, "implausible header"), ({4: 0, 31: 0}, "implausible header"),
                       ({31: 3840 | (2160 << 16)}, "implausible lattice words"),
                       ({4: 0, 31: 3840 | (2160 << 16)}, "implausible lattice words"),
                       ({4: 0, 5: 0, 31: 40 | (2160 << 16)}, "implausible lattice words"),
                       ({4: 0, 5: 0, 31: 3840}, "implausible lattice words"),
                       ({4: 2000}, "implausible lattice words")):           # 48 x 2000 px > 65535
        with pytest.raises(ValueError, match=why):
            read_panel_file(patched(words, "bad.bin"))


def test_gpuemu_refuses_a_fractional_lattice(tmp_path):
    """The GPU-order twin reshapes the lattice into equal blocks: it must refuse a fractional pitch, not mis-sweep it."""
    from dlc.fald.gpuemu import Emu
    from dlc.fald.panelfile import read_panel_file
    p = FaldParams(width=960, height=540, cols=12, rows=12)
    export_panel_params(FaldModel(p), tmp_path / "f.bin", frame=(1000, 540))
    o = read_panel_file(tmp_path / "f.bin")
    assert o["fractional"]
    with pytest.raises(ValueError, match="fractional"):
        Emu(o, width=1000, height=540)
