# DesktopLUT mark

The RGB colour cube as a glass cube, seen from its white corner and framed in black.

- **Geometry.** The default view is true isometric (`ELEVATION_DEG = 35.264`). The neutral black–white axis points at the viewer, so the outline is a regular hexagon: the hue hexagon. Black hides exactly behind white.
  - The old PNG used a lower view, about 26°. `preview.png` shows that as the "alt" tile; set `ELEVATION_DEG = 26.4` to use it.
- **Faces.** The three visible faces are the cube's G=1 (top), B=1 (left) and R=1 (right) planes. Each corner is the exact primary or secondary: green, yellow, red, magenta, blue and cyan around the rim. Every face colour lies on its face's cube plane.
- **Highlight.** A soft white highlight sits on the front (white) vertex. It is radial, with no hard edge, and identical on all three faces.
- **Glass.** The faces are semi-transparent (more opaque toward the front vertex), with a fine light rim along the front edges and slightly clearer glass against the outer frame. The three hidden back edges, from black out to G, R and B, show faintly through the faces.
- **Frame.** Black, mitred outer hexagon plus the three front edges, all the same width.
- **Inverted variant** (`logo_inv_*`, `DesktopLUT-active_new.ico`): colour-inverted faces with a white frame, used for the "active" tray icon. Its highlight, which inverts to a dark core, is drawn at 75 % radius (`INVERTED_HIGHLIGHT_SCALE`) so the core stays an accent.

## Files

| File | |
|---|---|
| `logo_{16,20,24,32,40,48,64,96,128,256,512,1024}.png` | master, RGBA with straight alpha |
| `logo_inv_{…}.png` | inverted variant |
| `DesktopLUT_new.ico`, `DesktopLUT-active_new.ico` | 16/20/24/32/40/48/64 as 32-bit BMP entries, 256 as PNG |
| `logo.svg` | vector approximation for print and web (linear and radial gradients, mask, blur); leaves out the rim and edge clearing |
| `preview.png` | old vs new over #0b0b0c, #808080 and #f2f2f2, OKLab-lightness greyscale, small sizes at 1x and 4x |

## Regenerate

```
python tools/logo/render_logo.py            # rewrites everything in assets/logo/
python tools/logo/render_logo.py --metrics  # also prints the balance numbers
```

The generator needs only Pillow and numpy, and its output is deterministic. Every pixel is computed from the cube geometry, never traced from the old bitmap. Rendering uses 16x supersampling up to 64 px, 8x up to 256 px and 4x above that, box-filtered in premultiplied space and stored un-premultiplied.

## Parameters (top of `render_logo.py`)

| Parameter | Value | Meaning |
|---|---|---|
| `ELEVATION_DEG` | 35.264 | view elevation (isometric) |
| `FRAME_WIDTH` | 0.046 | frame width / logo height (12 px at 256) |
| `GLASS_ALPHA`, `GLASS_ALPHA_GAMMA` | 0.82, 1.7 | face opacity at the outer corners, and its falloff |
| `EDGE_CLEAR`, `EDGE_CLEAR_WIDTH` | 0.10, 1.4 | clearer glass against the outer frame |
| `RIM_STRENGTH`, `RIM_WIDTH` | 0.30, 0.18 | light rim along the front edges |
| `HIGHLIGHT_STRENGTH`, `HIGHLIGHT_RADIUS`, `HIGHLIGHT_SOFTNESS` | 1.0, 0.34, 1.7 | white at the front vertex; 50 % radius as a fraction of the edge length; falloff exponent |
| `BACK_EDGE_STRENGTH`, `BACK_EDGE_WIDTH` | 0.22, 0.60 | hidden back edges |
| `FACE_SHAPING` | see below | per-face colour distribution (balance) |
| `SMALL` | per size | hinting for 16–64 px |

## Colour balance

At equal RGB the primaries carry very different weight (OKLab L: G .866, R .628, B .452). The faces are balanced only by how each face's gradient is distributed:

- `radial` sets how fast the primary takes over going outwards.
- `upper` and `lower` set the width of each secondary seam.

The corners are never desaturated, and the highlight stays identical on all faces.

| Face | Shaping | Effect |
|---|---|---|
| top | `radial 3.0`, seams `0.5 / 0.5` | green is the darkest colour on its face, so it gets most of the face |
| left | `radial 1.3`, cyan `2.4`, magenta `1.1` | the blue face gives its lighter cyan seam the most room |
| right | `radial 1.3`, yellow `0.8`, magenta `1.1` | the red face keeps its yellow seam narrow |

Magenta is shaped the same on both side faces, so the bottom vertex is mirror-symmetric.

Mean OKLab L per face (256 px master, straight colour):

| | top | left | right | spread | left–right gap |
|---|---|---|---|---|---|
| old PNG | 0.852 | 0.545 | 0.673 | 0.307 | 0.128 |
| new | 0.874 | 0.650 | 0.702 | **0.223** | **0.052** |

Over #0b0b0c the spread goes from 0.213 to 0.203. Each face still reads as its primary: the primary outweighs its secondaries on 96 % of the top face, 58 % of the left face and 76 % of the right face.

The top face cannot go below pure green's own L of 0.866 without darkening green. Narrowing the gap further would mean letting cyan and magenta take over the blue face, which then stops reading as blue. The remaining spread is the cube's intrinsic green/blue gap.

## Small sizes

All frame widths are whole pixels and the vertical strokes sit on pixel boundaries. Where the stroke parity does not fit the canvas (1 px strokes on 16/20/24, 3 px on 64), the mark sits half a pixel left of centre; it stays mirror-symmetric about its own axis. Above 64 px the frame width is rounded to an even number, so the mark is exactly centred.

At 16–24 px the frame is 1 px and the slanted strokes are drawn 1.4–1.5x heavier, so they don't anti-alias to grey. The back edges, rim and edge clearing are dropped there, the glass is more solid and the faces lean further toward their primaries. 32 px uses a 2 px frame. The back edges and rim fade back in from 40 px.
