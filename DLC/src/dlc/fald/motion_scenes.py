"""The standard MOTION stimuli (2026-10-04) — one list for the offline baseline, the frozen predictions and the TPG session.

Full-resolution PA32UCXR pixels (3840 × 2160, zones 80 × 45 px). Objects start near the screen centre, never on a zone
row boundary, and cross 2–4 zone borders. With ``aids=True`` every scene carries the camera aids of
:class:`dlc.fald.motion.Scene` far (≥ 15 zones) from the path: a sync patch on the object's rows at the left edge
(10 ↔ 30 nit), the Gray-coded present counter and the readable 7-segment counter at the bottom left (2 ↔ 12 nit).
A prediction for a HARDWARE gate must be made with the same ``aids`` setting the TPG showed.
"""
from __future__ import annotations

import math

from .motion import MovingShape, Scene, grey

Y_MID = 1102.5          # not on a zone row boundary (rows every 45 px)


def camera_aids(object_y: float = Y_MID) -> dict:
    return {"sync": (200.0, object_y - 60.0, 120.0, 120.0, 10.0, 30.0),
            "code": (200.0, 1960.0, 40.0, 12, 2.0, 12.0),
            "digits": (200.0, 1820.0, 80.0, 6, 2.0, 12.0)}


def standard_scenes(aids: bool = False) -> list[Scene]:
    out = []

    def add(name, bg, shapes, move, y=Y_MID, **kw):
        extra = camera_aids(y) if aids else {}
        out.append(Scene(name, bg, shapes, move=move, **extra, **kw))

    for v in (2, 4, 8, 16):
        add(f"bar_v{v}", grey(5), (MovingShape("rect", 1790.3, Y_MID, grey(1000), w=40, h=270, vx=v),),
            int(math.ceil(240 / v)), note="40x270 bar, 1000 nit on 5 nit, horizontal")
    for v in (4, 12):
        add(f"block_v{v}", grey(5), (MovingShape("rect", 1900.3, Y_MID, grey(1000), w=240, h=135, vx=v),),
            int(math.ceil(240 / v)), note="240x135 (3x3 zones) block, 1000 on 5 - the b3 step block, moving")
    for v in (4, 12):
        add(f"fog_v{v}", grey(20), (MovingShape("disc", 1830.3, Y_MID, grey(200), r=60, vx=v),),
            int(math.ceil(240 / v)), note="r60 disc 200 nit in 20-nit fog (owner's panning light in grey fog)")
    for v in (2, 8):
        add(f"star_v{v}", grey(5), (MovingShape("rect", 1830.3, Y_MID, grey(1842), w=8, h=8, vx=v),),
            int(math.ceil(160 / v)), note="8x8 star 1842 nit on 5 nit (the border-law probe object)")
    add("barvert_v3", grey(5), (MovingShape("rect", 1900.3, 1060.3, grey(1000), w=270, h=40, vy=3),), 45, y=1060.3,
        note="270x40 bar moving DOWN 3 px/refresh across zone rows")
    add("bar_24p", grey(5), (MovingShape("rect", 1790.3, Y_MID, grey(1000), w=40, h=270, vx=10),), 24,
        pre=8, post=10, cadence=(3, 2), note="the 40x270 bar as 24p content (10 px per content frame = 240 px/s, 3:2 cadence)")
    add("barblack_v4", grey(0), (MovingShape("rect", 1790.3, Y_MID, grey(1000), w=40, h=270, vx=4),), 60,
        note="40x270 bar on BLACK (boost + deep-dark fade)")
    return out


def scene_by_name(name: str, aids: bool = False) -> Scene:
    for s in standard_scenes(aids):
        if s.name == name:
            return s
    raise KeyError(f"no standard motion scene {name!r}")
