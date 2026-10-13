# PA32UCXR in-monitor hardware calibration — interop ticket (2026-10-09)

**Status:** PARKED 2026-10-09 (owner: "we'll come back to this later"). Nothing built. Static analysis only so far
(no app run, no USB capture, no device I/O).
**Scope:** **PRIVATE add-on only — never part of public DesktopLUT/DLC.** It depends on ASUS's proprietary DLLs
and one monitor model. Any code lives outside the public tree (or in a gitignored path), and nothing here goes into
README/CHANGELOG or `origin/main`.
**Goal:** let DLC drive the PA32UCXR's own colour pipeline (and its pattern generator / panel
temperature / local-dimming switch) through ASUS's existing calibration path, so a correction can live
*in the monitor* (every input, no DWM hook, no Windows CMS) — without custom firmware.

## Why not custom firmware (closed)
`PA32UCXR_FW_MCM105.zip` = scaler image (MediaTek **MT9810**, MStar-lineage code; 8051 boot stub +
~1.5 MB compressed main image) + `PA32UCXR_M.bin` (Coretronic Cortex-M self-cal MCU, EFM32-family).
Closed NDA SDK, no register docs, brick risk on the rig's reference display, and the dimming logic may
sit in a separate controller not in the package. Not pursued.

## What the vendor stack exposes (ProArt Calibration 4.1.8.8, `C:\Program Files (x86)\ProArt Calibration`)
- **`ProArtMApi.dll`** (x86 native, 231 exports) = the monitor API. Pipeline:
  **PreGamma (1D, `_1024` variant) → Matrix (range −4…3.9999) → 3D LUT (+ gain) → PostGamma (1D, `_1024`)**,
  plus `SetRgbGain`, `SetBrightnessPWM`, `SetIndEnable{PreGamma,Matrix,3DLUT,PostGamma,Uniformity}`,
  `Get/SetLocalDimming`, `AsusVcpGetDisplayPanelTemperature`, `Show{FullScr,Display,Osd}Pattern`
  (internal pattern generator), `Sensor*`/`AsusSensor*` (built-in sensor raw XYZ; CREG1/CREG2/divider/
  gain/time — looks like an AMS AS73211-class XYZ sensor), user-mode save/load/rename.
- Sizes are **negotiated at runtime** (`ProArtM_GetFeatureInfo` → pre/post-gamma, matrix, 3D-LUT info);
  setters check range + `matrixSize == DATA_ENTRIES` and wrap writes in `SetBlock(1) … SetAndCheckData …
  SetBlock(0)` (read-back verified).
- **Transport** (from the plaintext `ProArtM_TraceLog_*.txt`): PC → USB HID → Coretronic MCU
  (`ELF32_*`, `mcuUsbBufferSize = 280`) → I²C → scaler, using DDC/CI-framed packets with ASUS opcodes
  (`0x82 0xF1 <op> …`, seen `0xBF`, `0xDA`). `ProArtMApi` also imports `SLABHIDtoSMBus` (CP2112) — the
  path for other models. Needs the monitor's **USB upstream cable** to the PC (not connected 2026-10-09).
- **Persisting = raw SPI-flash write in ISP mode:** `FlashOpen` (enter ISP, pause scaler CPU, drop write
  protection; flash ID `0xC22018`, 16 MB Macronix) → `FlashSave3DLUT` / `FlashSavePostGammaAndBrightnessPWM`
  → `FlashClose` (scaler reboot, ~33 s). **DLC never reimplements this** — vendor DLL only, after a full
  read-back backup, owner-approved per write.
- The HIT trace logs and `CalData.ini` are encrypted by the app's own crypto DLLs — **out of scope; do not
  decrypt.** Work only from the plaintext ProArtM trace, DLL exports/strings, and our own calls.
- Jan-2026 trace: FW rejected VCP `0xED` (proximity) and `0x6B` — "ask for latest FW". Check the installed
  scaler/MCU FW vs MCM105 before relying on any VCP.

## Plan (each step gated; HW steps need the owner + the probe-work neutral-state audit)
1. **Signatures (offline).** Recover C signatures for the volatile ops first: `Open/Close`, `GetFeatureInfo`,
   `Get/Set{Matrix,PreGamma,PostGamma,3DLUT,3DLUTGain,RgbGain}`, `SetIndEnable*`, `ShowFullScrPattern`,
   `GetDisplayPanelTemperature`, `Get/SetLocalDimming`, `GetFwVersion/GetMCUFwVersion`. Sources: call sites
   in `CalibrationMainFlow.dll` (loads `ProArtMApi` via GetProcAddress), the trace-log format strings, the
   MCU image's port of the same lib. Re-check against 4.3.4.3 once installed (export diff).
2. **32-bit bridge (offline).** `ProArtMApi.dll` is x86 → a tiny 32-bit helper process (Python-x86 ctypes
   or a C shim) with a JSON-over-pipe surface; DLC (x64) talks to it. Mock-backed tests.
3. **Read-only HW bring-up.** Connect USB upstream; `Open`, FW versions, `GetFeatureInfo` (→ real LUT grid
   + bit depths), panel temperature, read back current pipeline blocks → save as the rollback snapshot.
4. **Volatile write A/B.** Identity-then-known matrix / 1D write, meter it, then power-cycle and re-read to
   learn whether live writes persist. Only then decide whether the flash path is ever needed.
5. **Uses, in value order:** (a) internal pattern generator for native characterisation (patches that never
   touch Windows/GPU); (b) panel temperature into the thermal-state work; (c) `SetLocalDimming` for
   stack-config measurement; (d) in-monitor MHC+cube delivery (needs grid/bit-depth from step 3 and an HDR-mode
   answer); (e) built-in sensor as a between-run drift monitor (cross-check vs the i1D3 first).

Alternative to the DLL bridge: USB capture of the HID tunnel while the official app runs, then a native
implementation of the *volatile* opcodes only. More work; consider only if the bridge proves fragile.
