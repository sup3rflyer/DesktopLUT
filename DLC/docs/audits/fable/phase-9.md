# Fable audit — Phase 9: IPC contract and mock fidelity

- **Scope:** `desktoplut_client.py` (243 → 286), `controller.py` (314, audited — no code
  change needed), `desktoplut_api_spec.py` (355 → 472), `desktoplut_mock.py` (285 → 408),
  `stages/enter_neutral.py`, `stages/install_mhc.py`, `stages/install_3dlut.py`,
  `stages/state.py`, `stages/preflight.py` pipe check, `stages/_common.py` controller
  plumbing. C++ conformance touchpoints, **read-only**: `../src/desktoplut_ipc_server.cpp`
  (1,496 — read in full), `../src/mhc.cpp` grayscale evals, `../src/gui.cpp`
  `EnsureProcessingForPreview`, `../shared/dwm_hook_config.h`. C++ changes are the ticket
  list in §5.
- **Method:** read every line of the DLC-side scope and the whole C++ IPC server; for each
  method verify the request/response shape three ways (spec ⇄ mock ⇄ C++); for every mock
  behaviour ask *"could a `--simulate` run pass here where hardware would fail?"* and raise
  fidelity where yes; audit error propagation and timeout/retry semantics end to end.
- **Baseline (pre-phase, this container):** `936 collected: 933 passed, 3 skipped`.
- **Post-phase:** `950 collected: 947 passed, 3 skipped` (+14 tests, all in the new
  `tests/test_ipc_contract.py`). End-to-end rehearsal re-verified:
  `python -m dlc.stages.simulate` reaches report with all stages `ran`.
- **Headline:** the "machine-readable contract" was only checked in one direction — the
  spec was missing **five methods the C++ and the controller both already speak**
  (`windows.set_hdr` + the `mhc.grayscale_live_*` quartet), and nothing compared result
  *shapes* at all. Now pinned three ways (F9-1). A real swallowed-error bug in
  `install-mhc` (`install_ok` could literally never be False — F9-2), the mock's
  `verify_mhc` passing runs hardware would fail (F9-3), the version handshake designed
  and landed client-side (F9-8), and a C++ **snapshot-overwrite hazard on re-enter**
  identified, mirrored in the mock, and surfaced as a tell (F9-9).

## 1. The contract, pinned three ways (`tests/test_ipc_contract.py`)

The existing pin (`test_api_spec_methods_all_have_a_cpp_handler`, Phase 7a) proved every
*advertised* method has a C++ handler — which passes vacuously when the spec simply
doesn't advertise a method. The new module closes the loop:

1. **mock ⇄ spec** — every advertised method is driven against the simulator in one
   realistic order; each response must carry at least the spec's declared result keys,
   and every spec method must be exercised (a spec entry the mock can't serve fails).
2. **spec ⇄ C++** (static, skips cleanly when the C++ tree isn't alongside) —
   *reverse existence* (every C++-dispatched method is advertised — this is the test
   that would have caught the five missing methods), *result-shape conformance* (each
   handler's `result.set("...")` keys must cover the spec's declared keys, with known
   gaps quarantined in `CPP_TICKETED_RESULT_KEYS` so landing the ticket auto-arms the
   test), and *threading conformance* (`gui_thread_required` must mirror the C++
   Dispatch routing — caught `maintenance.verify_mhc`, which the spec wrongly claimed
   needs the GUI thread; the C++ serves it on the pipe thread).
3. **controller ⇄ spec** — every wire method `CalibrationController` drives must be
   advertised (a new controller call now needs a contract entry first).

## 2. Findings and fixes

**F9-1 — Spec completeness + honesty (`desktoplut_api_spec.py`).** Added the five
missing methods with full param/result shapes: `windows.set_hdr`,
`mhc.grayscale_live_begin` / `grayscale_set_live` / `grayscale_commit` /
`grayscale_cancel`. Added a `status` field (`runtime.set_grayscale_tweak` is now
formally `legacy` — kept surface, no current caller). Fixed `maintenance.verify_mhc`'s
`gui_thread_required` (False — pipe-thread read). Corrected `calibration.enter`'s
purpose: the C++ **records** the dummy ICC path but does NOT associate it (association
is deliberately deferred; neutrality comes from the cleared layers + DLC's `dispwin -c`
— the old text claimed association happened). Documented the C++ 32-point grayscale
clamp at the `points` params (DLC always sends ≤32 — verified across calibrate.py,
build_mhc.py, refine_grayscale.py, so the index-resample above 32 is never hit).

**F9-2 — `install-mhc` could never fail its install check (`stages/install_mhc.py`).**
`install_ok = mhc_applied or bool(profile_name) or (isinstance(applied, dict) and
applied.get("ok") is not False)` — the last clause tests the *result payload* for an
envelope key that never reaches it (`controller.call` raises on `ok:false`), so it is
True for **any** dict: `install_ok` was structurally always True. Also `profile_name`
was read from the top level, but the C++ `DoMhcApply` returns it **inside the `mhc`
object** — on hardware the metric was always None. Fixed: applied/profile_name read
from `applied["mhc"]` (top-level fallback kept), the always-true clause removed, and an
ok-but-unconfirmed apply now raises an `apply_unconfirmed` anomaly. Pinned with a stub
controller returning an evasive `{"mhc": {}}` response.

**F9-3 — Mock `verify_mhc` verified staged-but-never-applied state.** The mock passed
`bool(mhc.get(key))` — truthy after a lone `set_primaries` — while the C++ requires
`enabled && !profileName.empty()` (a **baked** profile). A sim run could pass a verify
gate hardware would fail. Now requires `applied`. Pinned staged→False, applied→True,
removed→False.

**F9-4 — Mock accepted phantom cube paths.** The C++ validates up-front
(`DoSet3dlut`: existence; `DoMhcSetBaseLut`: existence + `Load1DCubeLUT` parse) so a
bad path fails with a clear error instead of a silent identity fallback. The mock
accepted any string — exactly how the Phase-5 `apply_3dlut_candidate` cwd-as-cube bug
class survives `--simulate` and dies on hardware. The mock now mirrors both checks
(error texts identical to the C++); six tests that pushed phantom paths were updated to
write real files (they were pinning behaviour hardware rejects).

**F9-5 — Mock accepted any monitor index / mode string.** C++ `ParseMonitorMode`
rejects out-of-range monitors and non-`SDR`/`HDR` modes; the mock uppercased anything
and keyed on any index, so a bad display mapping only failed on hardware. `key()` now
validates both (identical error texts), including `calibration.enter`.

**F9-6 — Gamma-ramp evidence was untestable under sim.** `windows.query_gamma_ramp`
returned `available:false`, so `enter-neutral`'s ramp-identity evidence branch never
ran under `--simulate`. The mock now returns a hardware-shaped healthy readback
(`available:true, gamma_ramp_loaded:false, vcgt_present:false, simulated:true`;
out-of-range monitor mirrors the C++ unavailable path). `windows.query_profiles`
verified **faithful as-is** — the C++ is deliberately thin there (v1 defers the ICC
audit to Argyll; the mock now also carries its `note`).

**F9-7 — Response-shape fidelity.** `mhc.apply` now carries a simulated
`profile_name` inside the `mhc` object (exercises F9-2's plumbing under sim);
`mhc.grayscale_live_begin` returns the C++ `preview:true` key;
`calibration.exit` with restore returns the C++ `{active:false, restored:true}` shape
(was `{snapshot_id, restored}` — missing `active`).

**F9-8 — Version handshake (the roadmap's "no version handshake on the wire").**
Designed as an optional `contract_version` integer in the `state.get` result: absent =
pre-versioning build = v1 (today's C++ — compatible by definition), mismatch = a
preflight-visible "update DLC / update DesktopLUT" instead of `unknown method` mid-run.
Landed: `desktoplut_client.CONTRACT_VERSION` + `contract_version_mismatch()` (the spec's
`version` now derives from it), the mock advertises it, the orchestrator preflight logs
it and carries `contract_mismatch` in the digest, and the stage-tool preflight raises a
high anomaly and drops `ready`. C++ side is ticket T1 (§5); the shape-conformance test's
`CPP_TICKETED_RESULT_KEYS` starts enforcing it the moment it lands. (Note: the roadmap
lead paired this with `events.SCHEMA_VERSION` — that is the *events.jsonl* schema, a
different artifact; it stays independent by design.)

**F9-9 — Re-enter destroys the restore snapshot (C++ hazard, now surfaced).**
`DoEnterNeutral` snapshots unconditionally into a **single slot**. If a previous run
died without `calibration.exit`, the next run's enter re-snapshots the already-cleared
state — `exit(restore_snapshot=true)` then restores *cleared*, not the user's pre-run
setup. The mock already mirrored the observable behaviour (latest enter wins); now it's
pinned (`test_reenter_overwrites_restore_snapshot_hazard`), documented at the mock and
in the spec's `timeout_and_retries` note, ticketed C++-side (T2), and **surfaced as a
tell**: both the orchestrator's `stage_enter_neutral` and the `enter-neutral` stage tool
probe `calibration.status` first and flag `stale_calibration_mode` ("the preflight
settings backup is the authoritative restore") — evidence only, entering proceeds.
This also resolves the timeout-retry audit for the one non-idempotent method: every
`mhc.set_*` / `apply` / `runtime.*` is idempotent (retry-safe after a timeout);
`calibration.enter` is not; `grayscale_commit` retried after a real commit returns
`baked:false`, which calibrate.py already surfaces as a seam (Phase 7a).

**F9-10 — Design-B revert honesty (`grayscale-wb`).** `_snapshot_correction_grayscale`
reads `state.get → mhc[key].correction_grayscale` — a key the C++ `HandleStateGet`
**never returns** (it reports only `applied`/`profile_name`). Under sim the mock's rich
state made the "restore the user's prior correction" path work; on hardware the snapshot
is always None and revert silently degrades to clear-to-identity. Fix has two halves:
C++ ticket T3 (expose `correction_grayscale` in `state.get` — the spec's `state.get`
entry now documents the requirement), and an in-phase honesty tell — the touch-up stage
logs up front when no prior was captured ("a revert of this touch-up will clear to
identity, not restore a prior correction"). Deliberately NOT "fixed" by stripping the
mock's rich state: the mock models the *contracted* target state, and the rich entries
are load-bearing for the Phase-7a live-edit pins; the divergence is now documented at
the contract instead of latent.

**F9-11 — Timeout thread orphan documented (`_call_with_timeout`).** The
daemon worker can't be cancelled (no portable cancel for blocking pipe IO); on timeout
it leaks until the server responds or the process exits. Documented the two operational
consequences (request may still be applied server-side → see F9-9's idempotency table;
the single-instance pipe makes an immediate retry fail pipe-busy until the orphan
drains). The thread name now carries the method label for debuggability. Client 75s
default vs C++ GUI-marshal 60s verified correctly ordered (the server gives up first).

## 3. Verified correct (audited, no change)

- **`controller.call` does NOT swallow the ok/error distinction** — the roadmap lead is
  stale. `client.send` raises `DesktopLutApiError` on `ok:false` by default;
  `controller.call`'s `result or {}` only normalizes an *ok* response with an absent
  result. The one `raise_on_error=False` site (`lut3d.apply_3dlut`) records and breaks
  on the failure explicitly. The Phase-7a aside ("`grayscale_set_live` without a begin
  reads as `{}` through `controller.call`") is likewise wrong — it raises. The *real*
  member of this bug class was F9-2's `install_ok`, now fixed.
- **P10 — the grayscale bridge, re-verified against the shipped C++** (not the
  replica): `mhc.cpp EvalGrayscaleSDR[_Channel]` is sqrt-indexed with **signal-domain**
  slot values (identity `t²`, sqrt-domain interpolation), per-channel deviations
  multiplicative — exactly `to_desktoplut_sdr_grayscale`'s emission; HDR
  (`EvalGrayscaleHDR`) indexes linearly with identity `t` — matching the DLC
  pass-through. `ApplyGrayscalePayload`'s ≤32 clamp/index-resample is unreachable from
  DLC (all senders ≤32). `shared/dwm_hook_config.h` carries no grayscale fields (the
  overlay tweak rides the pending-CC queue, not the hook shm) — nothing to cross-check
  there.
- **F4-12 closed (Phase 4 lead):** the C++ live grayscale editor honours HDR.
  `EnsureProcessingForPreview` gates on the monitor's live mode matching the requested
  mode (`ctx.isHDREnabled == isHDR`); HDR uses the legacy strip-`PERM_GS` preview, SDR
  the realization-A bit-faithful full preview. An HDR `grayscale-wb` therefore requires
  the panel actually in HDR — which `_reject_mode_target_mismatch` (Phase 7a) +
  preflight's mode tells already guarantee upstream.
- **`stages/state.py`** correctly re-labels the wire `corrections_enabled` as
  `overlay_path_enabled` (NAMING.md §4 — false in hook mode even with a live cube) and
  judges liveness from `cube_path`; the C++ `HandleStateGet` carries the matching WIRE
  CONTRACT comment. `install_3dlut`'s confirm-via-`state.get` matches the C++ runtime
  entry shape (`cube_path` only).
- **`_capture_inplace_baseline`** already documents that only the cube is
  auto-revertible from the C++ wire state (its `grayscale_tweak` read is mock-only
  richness, harmless on hardware — None either way).
- **Server security posture** (context for the ticket list, not DLC-actionable): opt-in
  arming (checkbox/env/flag file), DACL = current user + SYSTEM, remote clients
  rejected, 256 KB request guard, single instance, byte-mode NDJSON one-shot per
  connection — matches the spec's transport section.

## 4. Parity

- **P10** — re-verified against the shipped C++ (ledger row annotated).
- **HDR OS-state toggling** — `windows.set_hdr` is now spec-advertised and
  shape-pinned mock ⇄ spec ⇄ C++ (including the bool-or-0/1 `enable`, toggle-on-absent,
  and capability rejection). The live flip (DisplayConfig set + WM_DISPLAYCHANGE MHC
  reapply + `query_monitors` `color_space` tracking) only hardware can confirm → HW-8.
- The grayscale live-edit quartet is mode-shared and now contract-pinned for both modes
  (the mock's monitor 0 is HDR-capable, monitor 1 SDR-only, so both paths are
  exercisable under sim).

## 5. DesktopLUT-side ticket list (C++; read-only for this audit)

| # | Ticket | Where | Why |
|---|---|---|---|
| T1 | Add `contract_version` (int, `= 1`) to the `state.get` result | `HandleStateGet` | Version handshake (F9-8). DLC already checks it at preflight; `CPP_TICKETED_RESULT_KEYS` in `test_ipc_contract.py` starts enforcing the shape the moment it lands — remove the allowlist entry with the change. |
| T2 | Preserve the ORIGINAL snapshot on re-enter | `DoEnterNeutral` | Single snapshot slot is overwritten with the already-cleared state when a crashed run's session is still active — `restore_snapshot` then can't restore the user's setup (F9-9). Keep the existing snapshot when `g_calib.active`. |
| T3 | Expose `correction_grayscale` (`point_count`/`points`/`deviations`) in `state.get` mhc entries | `HandleStateGet` | Makes DLC's Design-B grayscale-wb revert (restore the user's PRIOR correction) real on hardware; today it degrades to clear-to-identity (F9-10). Spec text already documents the requirement. |
| T4 | Remove the unreachable `maintenance.verify_mhc` branch in `HandleCalibrationGuiCommand` | `desktoplut_ipc_server.cpp:1483` | Hygiene: Dispatch serves it on the pipe thread first; the GUI-thread branch is dead and misleads about threading. |

## 6. HW-validation queue additions

- **HW-8:** `windows.set_hdr` live flip on the box — toggle monitor 0 SDR→HDR→SDR over
  the pipe; confirm the OS flip, DesktopLUT's own MHC reapply on WM_DISPLAYCHANGE, and
  `windows.query_monitors` tracking `hdr_active`/`color_space`; then drive one
  `--mode HDR` run end-to-end without touching Windows Settings.

## 7. Needs owner input

- None blocking. T1–T4 are DesktopLUT-side work items for the owner's C++ sessions;
  T2 and T3 are the two with user-visible consequences (restore correctness, revert
  correctness) and are worth scheduling before the Phase-12 hardware campaign so HW-1's
  runs exercise the fixed behaviour.
