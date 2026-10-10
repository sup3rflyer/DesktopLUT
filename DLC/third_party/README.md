# Third-Party Tools

This directory is for contained calibration tools used by DesktopLUT Calibrator.

Expected layout:

```text
third_party/argyll/3.3.0/bin/   the ArgyllCMS executables
third_party/argyll/3.3.0/ref/   Argyll's reference ICCs — DLC reads sRGB.icm and
                                Rec2020.icm (calibration-mode dummy profiles) and
                                Rec709.icm / Rec2020.icm (default 3D-LUT source)
third_party/dogegen/dogegen.exe
```

Where to get them (each under its own licence — read it before use):

- **ArgyllCMS 3.3.0** — https://www.argyllcms.com/ (Windows 64-bit build). Copy its `bin/` and
  `ref/` folders to `third_party/argyll/3.3.0/`. Install the colorimeter driver it documents if
  your meter needs one.
- **dogegen** (patch generator) — https://github.com/ledoge/dogegen (releases). Put `dogegen.exe`
  in `third_party/dogegen/`. DLC drives it through `python -m dlc.dogegen_server` (see
  `docs/operator-guide.md`).

The tool binaries are intentionally ignored by git. Place the ArgyllCMS and Dogegen
builds at the layout above (copied by hand, or via the helpers in `dlc.vendor`:
`plan_vendor_tools` / `copy_vendor_tools` / `write_vendor_manifest`).

`dlc.vendor` writes `third_party/vendor_manifest.json` with the source, destination,
action, file count, size, and SHA-256 fingerprints for the contained tool files (a
`manifest-existing`-style write — `copied=false` — fingerprints binaries already
present here without copying from any external source). Keep that manifest with run
evidence when checking which contained toolchain was used.

The preflight stage (`PYTHONPATH=src python -m dlc.stages.preflight --run <RUN>`)
reports whether this manifest is present and has usable fingerprints through
`vendor_manifest_ready`, and writes its full tool-readiness payload to
`preflight/tool_preflight.json`.

