# Phone capture framework — quick start, backends, findings, backlog

Code: `src/dlc/phone/` · tests: `tests/test_phone_{framework,rig,mcpro_mode}.py` (84, offline) · created 2026-10-04.
Owner brief: "make the surface easy to use when we get to the actual testing and measurements." One call takes what a
test wants (fps / iso / shutter / wb / focus / lens …), applies it, **verifies it against the camera HAL**, records,
pulls the clip, probes the file and writes a manifest. A test never names an app.
Superseded (coordinate-based, untracked): `agent_phonecam.py`, `agent_phonevid*.py`, … (facts live in memory
`s25-adb-camera-control`).

## Quick start

```bash
cd /h/Projects/DesktopLUT/DLC && export PYTHONPATH=src
python -m dlc.phone status                                   # device, storage, which backends are usable
python -m dlc.phone set --fps 60 --iso 400 --shutter 1/120 --wb 5600 --focus 0.3 --lens main
python -m dlc.phone capture flat 5 --fps 60 --iso 400 --shutter 1/120 --out results/my_run
# record AROUND an action (separate calls are fine - state lives in <out>/.rec_active.json):
python -m dlc.phone rec start ladder --fps 60 --iso 400 --shutter 1/120 --out results/my_run
python -m dlc.phone rec mark patch_64 level=64 --out results/my_run     # host time + the clip's own timecode
python -m dlc.phone rec stop --out results/my_run                       # stop, pull, verify, write ladder.json
```
Exit 0 = ok, 2 = clip verified **with problems** (read the JSON), 1 = error (`{"error","message"}` — the message lists
the supported options). Everything prints JSON.

```python
from dlc.phone import PhoneRig
rig = PhoneRig("results/my_run")
rig.set(fps=60, iso=400, shutter_s=1/120, wb_k=5600, focus=0.3)          # applied + HAL-verified, else Unachievable
with rig.recording("ladder", fps=60, iso=400, shutter_s=1/120) as rec:   # fps<=60 -> Blackmagic REST; >60 -> mcpro
    show_pattern(64);  rec.mark("patch=64", level=64)                    # leaving the block: stop+pull+verify+manifest
cap = rec.capture            # cap.ok, cap.problems, cap.local (clip), cap.manifest, cap.marks
cap = rig.capture("hs", 3, fps=120, iso=800, shutter_s=1/240)            # fixed length, HS 120 fps via mcpro
```
An exception inside the `with` still stops the camera and pulls the clip (flagged `interrupted`), then re-raises — the
phone is never left recording. `rig.use("mcpro")` pins a backend; `rig.status()` is the pre-test checklist.

## Which backend (chosen automatically by `fps`)

| | **Blackmagic Camera** (`blackmagic.py`) | **mcpro24fps** (`mcpro_backend.py`) |
|---|---|---|
| control | REST/JSON `https://<wifi-ip>:4444/control/api/v1` (+WebSocket) — real read-back | UI automation (uiautomator) + settings-profile import |
| fps | 24/25/30/48/50/**60** (off-speed 15–60) | **120 / 240 HS**, 60 …, RAW video (untested) |
| codec | H.264 / H.265 (8-bit seen; Log HDR via preset/UI) | AVC / HEVC (10-bit, PQ-tagged) / RAW |
| sets | fps, size, codec, iso, shutter, wb, tint, focus (AF off), lens (main/uw/tele), AE | mode (camera/codec/bits/fps/size) via profile import; iso, shutter by stepping; **not** wb/focus yet |
| clip time for marks | **yes** (`/transports/0/timecode`, ±1 frame) | no (host time only) |
| speed | ~1 s per `set` | `set_mode` ≈ 1.5 min (export→edit→import→restart); ISO/shutter steps ~1 s each |
| needs | Wi-Fi (same LAN), HTTP server on (one-time tap; auto by `ensure_server`) | app in front, volume keys = "Recording Control" |

Rule of thumb: anything ≤ 60 fps (flat fields, ladders, tone-curve/meter bridging, still-scene photometry) → Blackmagic.
FALD LED-tick / motion work needs ≥ 120 fps (the dimming tick is 30 Hz on a 60 Hz panel) → mcpro HS.

## What is verified, and what the manifest holds

`apply()` never trusts the app: it reads `dumpsys media.camera` (the HAL's last CaptureRequest) and checks ISO (±3 %,
the sensor quantises: 400 → 390/393/396/398), exposure (±1 %), AE off, fps, size, codec, WB, focus/AF; it polls ~5 s for the
HAL to settle, then raises `Unachievable` listing what *is* supported. Values are never silently snapped.
`<label>.json` next to each clip: backend, requested settings, `state_at_start` (API + HAL), marks (`t_host`, `clip_s`),
phone−host clock offset (± RTT/2), preflight (free GB, battery), the file as ffprobe sees it (codec, bits, size, measured fps,
frames, pts min/median/max, **gaps**, app tags), expectations, **problems**. Problems are *reported* for the LLM to judge;
nothing auto-accepts (DLC design law). Frame-accurate sync still needs an on-screen sync patch; marks are ±1 frame (BM) or
±~0.1 s (mcpro).

## Hard-won facts (HW, Galaxy S25 SM-S931B, Android 16, 2026-10-04)

**Blackmagic Camera 3.4.3.0008** — Ktor/Netty HTTPS :4444 with a bundled self-signed cert (client verify off), `GET
/access/status` = `control-and-monitor` (**no password by default** — LAN-open camera+mic control; turn it off or set a
password when not testing). Server refuses to start without a Wi-Fi IP. Routes: `/system/{format,supportedFormats,
codecFormat,videoFormat}`, `/video/{iso,shutter,whiteBalance,autoExposure,supported*}`, `/lens/{focus,focus/autoFocus,zoom,
iris,cameras,cameras/active}`, `/transports/0/{record,timecode}`, `/media/workingset`, `/presets`, `/monitoring/*`, IMU
(`MotionSensorEuler/Horizon` models), clips. PUT shapes that work: `{"iso":400}`, `{"shutterSpeed":120}` (integer 1/N; reads
back "119" — HAL is 1/120.00), `{"whiteBalance":5600}`, `{"whiteBalanceTint":0}`, `{"normalised":0.3}` for `/lens/focus`
(manual focus switches AF off by itself; exact read-back), `{"id":"6"}` for `/lens/cameras/active`, **full-body** PUT of
the GET object for `/system/format` (fps/codec/resolution; a bare `{"frameRate":"30"}` is a 400), `{"recording":true}`.
No REST model for dynamic range / Log HDR / bit depth → use a saved app preset (`/presets`) or Settings; the clip is probed.
**Gotchas:** re-sending `autoExposure Off` while already off makes the app re-run AE (HAL aeMode back to ON, ISO 3182) →
only send it when AE is on; the app's API state can disagree with the HAL at rest (API ISO 400 / HAL 3200) → always PUT
and judge the HAL; the camera HAL session exists only while the app's **Camera tab** is showing; a covered proximity
sensor shows Samsung's "Accidental touch protection" (swipe the ring up — `Adb.unlock()` does it); files land in
`/sdcard/DCIM/Blackmagic Camera/A001_<MMDDhhmm>_C001.mp4`.

**mcpro24fps 043de** — no remote API (the exported `TEST_SET` receiver is an empty stub); ~65 stable view ids; per-camera
settings keys (`INTEGER_nifps_0`, `…_5`, camera 0 = logical main with HS, 5 = physical main, 2 = UW, 6 = 3× tele).
**Settings profile** (Export/Import → system file dialog; base64 + 3-char-chunk rotation, decoded in `profile.py`): the
import applies **camera, codec, bits, fps, size** and flips the session between NORMAL and CONSTRAINED_HIGH_SPEED (one
import: RAW16 60 fps → HEVC 10-bit 120 fps HS and back); it does **not** apply ISO/shutter. Manual exposure reaches the
HAL in HS (aeMode OFF, 1/240, ISO). **The record button (view `video`) is `selected` only while recording — the only
reliable signal; in HS the file is buffered and appears only at stop** (a file-based start check "failed" and left a
runaway 3-minute / 1.1 GB recording, now guarded: start refuses if already recording). Preview of an HS session
screen-captures black. Tele + HS120 → "Camera sensor is not available". Curve presets listed in the string table include
Linear / Linear sRGB / mLog / Rec.709 / "Auto (~PQ)" (default) — selecting one raises an "aggressive curve" dialog; wheel
contents (fps/size/curve) are invisible to uiautomator.
**Clip facts:** 1080p120 HEVC Main10 `yuv420p10le`, limited range, tagged smpte2084/bt709, uniform 8.33 ms pts. A **178 s**
HS clip had **22 timestamp gaps / 44 app-reported dropped frames** (flagged by `clip.check`); a 3 s and an 8 s clip had 0 —
keep HS clips short and analyse with container pts, never frame index × 1/fps.

## Session 2026-10-08 lessons (PA32UCXR, Blackmagic + Samsung Log HDR) → landed 10-09 (`1512666`, `9bdc8e7`)

**Exposure (BlackmagicBackend).** ISO minimum is **40** (25 refused before anything is sent); re-sending an UNCHANGED
shutter stalls the app's ISO → the shutter is PUT only when it changes; large ISO jumps walk the app's 1/3-stop list with a
settle (`iso_settle_s`, default 2 s). The HAL read-back costs ~3 s, so `rig.expose(iso, shutter_s, verify=False)` tracks
exposure in-process (seeded once from the HAL) and verifies at clip stop (a mismatch becomes a clip problem). `apply()`
keeps its API but now reads the real exposure first (~3 s more per call).
**Evidence in every manifest.** `parse_camera_dump` keeps EVERY key of the live request (android.*, vendor tags, unnamed
`name@tagid`, multi-line values; per-physical-camera requests in `physical_requests`); `state()` carries it plus every
REST-readable app setting (`APP_SETTINGS`). Manifests add `state_at_stop`, `changed_vs_prev` (exposure keys excluded),
`changed_during`, `warnings`; `phone_session.jsonl` + `phone_session_summary.json`, `rig.summary()`, CLI `summary`.
Changes are WARNINGS for the overseeing LLM, never gates. (On 10-08 the Log curve changed mid-session and the old manifests
could not say why.)
**Photometry rules.** 1/60 s at 60 fps = one refresh = the working shutter on the PA. 1/240 is photometric to ~1 % on
flats but 4-10 % high in boosted FALD zones; 1/120 alone is NOT photometric on the PA (backlight modulation inside a
refresh) — at 120 fps with a 360-degree shutter consecutive frame pairs tile one refresh. The Samsung Log HDR response
CHANGED at ~18:00 (80 → 68-70 codes/stop, clip 857 → 800) with nothing recorded changing → measure the curve at session
START and END and self-calibrate from in-take exposure pairs. Camera 59.76 fps vs panel 60 Hz → phase drift; record ≥ 4
cycles of any periodic stimulus. A taped phone tips (290 px over 3 h) → register every take/segment from its own content.
**Analysis modules.** `dlc.phone.analysis` (iter_frames on container pts, roi_series, FID2 fiducials, sync edges,
ToneCurve); `dlc.phone.curve` (`fit_camera_curve` from known-ratio `PairSet`s, `CameraCurve` JSON incl. the 10-08
`camera_curve2.json`, `verify_curve` → log-ratio compression factor: a wrong curve reads ~0.87); `dlc.phone.register`
(`register_segment`: NCC patches vs the encoded frame through H, RANSAC + IRLS, seeded by FID2, residuals + flags;
`register_series` / `interpolate_segments` for low-texture segments); `dlc.phone.glare` (`fit_glare_kernel` a·d^-p from
specks, `subtract_veil` = image ⊛ k + planar room floor, per-block veil fractions, `robust()` mask). Import submodules
directly (`__init__` unchanged). Not ported yet: flat-outline registration, disc-centroid fixes, flat-field / colour weights.
**HDR source frames:** decode in numpy from raw `yuv420p10le` (`results/phone_camera_2026-10-08/scripts/extract_frames.py`);
swscale's yuv420p10 (tv) → gbrp16le (pc) scales codes by 255/256 (1-2.4 % dark on PQ).

## Setup checklist (phone on the mount)

Phone and PC on the same LAN (Blackmagic); USB debugging authorised (adb at `C:\platform-tools`); screen kept awake / no PIN;
mcpro: Volume Buttons = Recording Control (default), Blackmagic: Settings → Network Access → HTTP Server on (`ensure_server`
does it, idempotently, if the app is installed); ≥ 3 GB free (the rig refuses below); `rig.status()` shows all of it.

## Verified live vs offline-only

Live: BM `set` (iso/shutter/wb/focus/lens/format/codec) + HAL agreement; BM `capture` and cross-process `rec start/mark/stop`
(marks carry clip timecode: Δ 1.967 s vs host 1.970 s; 347 frames, 59.96 fps, 0 gaps); mcpro `set_mode` (export→edit→import)
both directions, `set_iso`/`set_shutter` stepping with HAL confirmation, `capture` at 120 fps HS (958 frames, 120.00 fps,
0 gaps), `import_profile` (restored the owner's RAW16 state). Offline only: `ensure_server()` (UI path was done by hand once),
failure branches against real hardware (storage full, camera exception), mcpro `set_shutter` direction probe in HS.

## Backlog (ordered)

1. **Same-sitting meter + camera check** (2026-10-09 blocker): the camera reads +2.4 pp vs the 10-06 meter on halo
   profiles, LD-on only, side-dependent; registration / aperture / glare / curve / drift ruled out
   (`results/phone_camera_2026-10-08/halo/dense/offset.md`). Panel-state vs ~6-degree off-axis view.
2. **Rig:** rigid mount (clamp / articulating arm) + a USB-C PD hub so the phone charges while adb runs.
3. **Statics on RAW stills** (linear to 0.3 % on 09-19) instead of Log video for halo maps / real-scene stills; Log video for
   motion with in-take exposure pairs. Curve at session start and end either way.
4. **120 fps HS** (mcpro or BM 1080p120) with a 360-degree shutter: validates the FALD LED tick (~1/3 of the model on 10-08)
   and the anime-pan redo (content filling the phone's view).
5. Port the rest of the 10-08 analysis: flat-outline registration, flat-field / colour weights, a real-scene capture +
   analysis flow on the spine (not a bespoke probe).
6. mcpro wheels/rows: curve preset, WB, manual focus, lens-by-name; faster `set_mode` (skip export when the profile is cached).
7. RAW video (`codec=raw`, Raw12/16 + ZSTD) at 1080p/120: sustained fps, drops, GB/min.
8. Safety: optional password for the Blackmagic server; `rig.close()` to switch the server off; storage reaper for old clips.
(Done: the curve/linearity decision → Samsung Log HDR + `dlc.phone.curve` self-calibration; the analysis helpers →
`dlc.phone.analysis` / `register` / `glare`; the meter bridge is item 1.)
