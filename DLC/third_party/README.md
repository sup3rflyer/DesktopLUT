# Third-Party Tools

This directory is for contained calibration tools used by DesktopLUT Calibrator.

Expected layout:

```text
third_party/argyll/3.3.0/bin/
third_party/dogegen/dogegen.exe
```

The tool binaries are intentionally ignored by git. Use `dlc vendor-tools` to
inspect the current plan, and `dlc vendor-tools --copy` to copy the known local
ArgyllCMS and Dogegen builds into this directory.

If the binaries are already present here, use `dlc vendor-tools
--manifest-existing` to fingerprint the contained files and write the manifest
without copying from any external source path.

After `--copy`, DLC writes `third_party/vendor_manifest.json` with the source,
destination, action, file count, size, and SHA-256 fingerprints for the copied
tool files. `--manifest-existing` writes the same manifest with
`copied=false`. Keep that manifest with run evidence when checking which
contained toolchain was used. `dlc preflight` reports whether this manifest is present and
has usable fingerprints through `vendor_manifest_ready`, and writes its full tool readiness payload to
`preflight/tool_preflight.json`.

