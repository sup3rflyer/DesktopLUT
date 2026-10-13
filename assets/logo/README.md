# DesktopLUT mark

A hazy cloud of mixed red, green and blue light inside a black-framed glass box: the RGB colour cube, seen from its white corner.

- **Geometry.** The view elevation is 26.5°, the old PNG's view (its upper outline slopes at 0.446 = sin e). The three visible faces are the cube's G=1 (top), B=1 (left) and R=1 (right) planes. The black frame is the mitred outer hexagon plus the three front edges, all the same width.
- **Cloud.** It is built in three steps:
  1. **Flat RGB mix.** Three hard-edged circles of light are added channel by channel: green up, blue lower left, red lower right. Each pair overlaps in a lens pointing at the box's C, Y or M corner, giving cyan, yellow and magenta. The three lenses form an upside-down triquetra, and all three circles overlap in a white middle, so the secondaries touch each other only through that middle. The cloud is the circles' union, not a disc.
  2. **Wrapped onto a sphere.** The mix is wrapped onto the front of a sphere inside the box and seen straight on, so the middle bulges and the rim compresses. The wrap is partial (`SPHERE_BULGE` 0.6), so it reads as a roundish cloud, not a ball. The cloud sits slightly above the cube centre (`SPHERE_RAISE`), so its light middle spills a little onto the green face.
  3. **Blurred into haze.** A Gaussian blur on premultiplied colour does the mixing. Every boundary becomes a plain additive blend, and the middle, where all six colours meet, becomes a light grey-white. Blends are lifted halfway back toward full value (`CLOUD_VALUE`), so they do not turn muddy, and the haze is made less transparent (`CLOUD_DENSITY`).
- **Empty box.** The cloud stops short of the box's corners and seams, which stay transparent. The haze is the only softness in the mark: there is no glass rim, highlight or edge clearing.
- **Back edges.** The three hidden edges from the black corner are crisp black lines behind the cloud. They are sharp where the cloud does not reach and fade only under the haze.
- **Inverted variant** (`logo_inv_*`, `DesktopLUT-active_new.ico`): colour-inverted cloud, with a white frame and white back edges. It is used for the "active" tray icon.

## Files

| File | |
|---|---|
| `logo_{16,20,24,32,40,48,64,96,128,256,512,1024}.png` | master, RGBA with straight alpha |
| `logo_inv_{…}.png` | inverted variant |
| `DesktopLUT_new.ico`, `DesktopLUT-active_new.ico` | 16/20/24/32/40/48/64 as 32-bit BMP entries, 256 as PNG |
| `logo.svg` | vector frame over the cloud as an embedded 512 px PNG. The cloud is soft by nature, and its additive mixing cannot be expressed with SVG compositing |
| `preview.png` | old vs new over #0b0b0c, #808080 and #f2f2f2, OKLab-lightness greyscale, small sizes at 1x and 4x |

## Regenerate

```
python tools/logo/render_logo.py            # rewrites everything in assets/logo/
python tools/logo/render_logo.py --metrics  # also prints the colour numbers
```

The generator needs only Pillow and numpy, and its output is deterministic. Every pixel is computed from the cube geometry, never traced from the old bitmap. The cloud is computed at the canvas resolution from 4x4 samples per pixel, then blurred. The frame and back edges use 16x supersampling up to 64 px, 8x up to 256 px and 4x above that, box-filtered in premultiplied space and stored un-premultiplied.

## Parameters (top of `render_logo.py`)

| Parameter | Value | Meaning |
|---|---|---|
| `ELEVATION_DEG` | 26.5 | view elevation |
| `FRAME_WIDTH` | 0.046 | frame width / logo height (12 px at 256) |
| `MIX_ANGLE` | 120 | blue and red circles at -/+ this angle from green |
| `MIX_OFFSET`, `MIX_RADIUS` | 0.40, 0.57 | circle centre distance and radius, in sphere radii |
| `SPHERE_RADIUS` | 0.94 | sphere radius, as a fraction of R (cube centre to top vertex) |
| `SPHERE_BULGE` | 0.6 | 0 = flat mix, 1 = full equidistant wrap |
| `SPHERE_RAISE` | 0.08 | cloud centre above the cube centre, fraction of R |
| `CLOUD_BLUR` | 0.18 | Gaussian sigma of the haze, fraction of R |
| `CLOUD_DENSITY` | 2.5 | haze opacity: alpha' = 1 - (1 - alpha)^density |
| `CLOUD_VALUE` | 0.5 | 0 = blends are the plain average, 1 = lifted to full value |
| `BACK_EDGE_WIDTH` | 0.82 | back-edge line width, in frame widths |
| `SMALL` | per size | hinting for 16–64 px |

## Colour

Mixing is plain additive light in the signal. There is no perceptual remap: a hue-corrected blend had to bend differently on each side of every primary, which left hard lines where blends met.

The circles are mirror-symmetric, so cyan and yellow are drawn the same size. Green still reads larger in its blends: a green-red mix still looks green at red 0.74. So, measured by nearest OKLab hue, yellow comes out larger than cyan. `--metrics` prints each colour's measured share, for both the old PNG and the new mark, and its share x L** (lightness with the Helmholtz-Kohlrausch term, against the black frame).

## Small sizes

All frame widths are whole pixels and the vertical strokes sit on pixel boundaries. Where the stroke parity does not fit the canvas (1 px strokes on 16/20/24, 3 px on 64), the mark sits half a pixel left of centre; it stays mirror-symmetric about its own axis. Above 64 px the frame width is rounded to an even number, so the mark is exactly centred.

At 16–24 px the frame is 1 px and the slanted strokes are drawn 1.4–1.5x heavier, so they don't anti-alias to grey. The back edges are dropped up to 32 px. The cloud scales with the mark, so its haze stays proportional at every size.
