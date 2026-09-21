"""Suite-wide pytest configuration — SCHEDULING ONLY.

Nothing here defines a fixture or changes what a test asserts. It exists because of how
pytest-xdist hands work out, and that is what sets this suite's wall time.

Measured 2026-09-20 on a 32-thread box (``-n auto`` = 16 workers, the physical core count):
the suite is ~2000 CPU-seconds of work and its single longest test (the FALD profiling stage
chain) is ~205 s, so a perfect packing finishes in ~205 s. The actual run took 406 s — 200 s
of workers sitting idle. Startup is not the cause: collection + worker bring-up is ~5 s, and
the whole numpy/scipy/colour import stack is ~2.5 s per worker, paid once, in parallel.

2026-09-21 changed the shape of that: the FALD model's meter path no longer forms a whole frame
to average an aperture disc, so the stage chain fell to ~29 s and the longest test is now the
engine's constrained-RBF case at ~120 s, with total CPU down ~40 %. The run is closer to
CPU-bound than to critical-path-bound, which makes the ordering below matter LESS than it did
— but the failure mode it prevents (two heavy tests inside one 23-item chunk) is unchanged, and
it is nearly free, so it stays.

The cause is xdist's dispatch granularity. ``--dist load`` (the default) hands each worker a
CONSECUTIVE chunk of the collected list up front::

    node_chunksize = max(min(len(collection) // len(nodes) // 4, maxschedchunk), 2)

which is 23 items here — and it refills a drained worker with another consecutive block of up
to ~36. Collection order is alphabetical by file, so whether a 200 s test lands early or late,
and whether two of them land in the SAME chunk, is pure luck. Two heavy tests in one chunk run
back to back on one worker while the other fifteen finish and idle.

So this hook SPREADS the known-heavy tests evenly through the collected list — heaviest first,
then every ``stride`` items — so that no initial chunk and no refill block can contain two of
them, and the longest test is item 0 (it starts at t=0 on worker gw0).

The two obvious-looking alternatives were measured and are WORSE; do not "simplify" this into
either of them:

    heaviest-first, no spreading, --dist load        880 s  (worker gw0 got a 23-item chunk
                                                            that was ALL the heavy tests)
    heaviest-first, no spreading, --dist worksteal   377 s  (all heavy tests queue behind the
                                                            205 s test on gw0, and a worker can
                                                            only answer a steal request between
                                                            tests — so the other fifteen idle
                                                            until it finishes)

Keeping the table current is optional: an entry that no longer matches any test is ignored, and
a new slow test missing from it only loses the scheduling benefit, never correctness. Refresh
with ``python -m pytest -q --durations=0`` and copy the calls above ~10 s.
"""
from __future__ import annotations

# nodeid tail (file::function, without any ``[param]`` suffix) -> measured seconds.
# Parametrised entries are listed once by base name; every parameter inherits the cost.
# Only the ORDER of these numbers matters, never their absolute value.
#
# REFRESHED 2026-09-21 on a 4-core container (the 2026-09-20 numbers were a 16-worker box), after the
# FALD model's meter path stopped forming whole frames to average an aperture disc: the profiling
# stage chain went 196 s -> 29 s and is no longer the list's head, so the order below is genuinely
# different, not rescaled. Re-measure on the 16-worker box when convenient — a wrong order costs wall
# time, never correctness, and --dist worksteal absorbs it.
_HEAVY_SECONDS = {
    "test_engine_v2.py::test_constrained_rbf_caps_off_channel_lift_at_saturated_blue": 121,
    "test_fald_boost_gpu.py::test_emulator_two_round_boost_matches_correct_image_pa32ucxr_frame": 87,
    "test_fald_starfield_gpu.py::test_pa32ucxr_frame_star_lattice_with_outliers_matches_the_reference": 69,
    "test_fald_profile.py::test_the_fit_recovers_the_hidden_estimate": 51,
    "test_fald_fit_rules.py::test_synthetic_sdr_fit_recovers_drive_k_and_flags_an_unidentified_tmin": 35,
    "test_engine_v2.py::test_physical_cube_reduces_model_error_and_pins_neutral": 31,
    "test_optimize.py::test_physical_engine_is_opt_in_and_reports_info": 30,
    "test_fald_profile.py::test_stage_chain_sdr_to_export_and_verify": 29,
    "test_fald_glowfill.py::test_the_fill_fades_out_continuously_as_the_content_gets_brighter": 22,
    "test_engine_v2.py::test_build_cube_reduces_error_and_is_mostly_monotonic": 13,
    "test_fald_starfield.py::test_protection_is_mirror_symmetric_and_a_mid_drive_object_protects_partially": 12,
    "test_engine_v2.py::test_sdr_wide_gamut_maps_inward_and_is_consistent": 10,
    "test_hook_routing.py::test_readiness_stage_refuses_when_the_hook_paints_nothing": 10,
    "test_hook_routing.py::test_readiness_stage_swaps_a_crossed_twin_and_raises_the_anomaly": 10,
    "test_fald_starfield_gpu.py::test_a_star_stepping_away_from_a_window_in_8_px_steps": 9,
    "test_fald_starfield.py::test_a_star_leaving_solid_content_gains_weight_continuously": 6,
}


def _cost(nodeid: str) -> float:
    key = nodeid.replace("\\", "/").split("[")[0]
    for tail, secs in _HEAVY_SECONDS.items():
        if key.endswith(tail):
            return secs
    return 0.0


def pytest_collection_modifyitems(session, config, items):
    """Heaviest test first, then the rest of the heavy ones evenly spaced through the list."""
    heavy = [it for it in items if _cost(it.nodeid) > 0.0]
    if not heavy or len(heavy) == len(items):
        return
    heavy.sort(key=lambda it: -_cost(it.nodeid))          # stable: ties keep collection order
    rest = [it for it in items if _cost(it.nodeid) == 0.0]
    stride = max(1, len(items) // len(heavy))             # >= 2x xdist's 23-item initial chunk
    out, cut = [], 0
    for item in heavy:
        out.append(item)
        out.extend(rest[cut:cut + stride - 1])
        cut += stride - 1
    out.extend(rest[cut:])
    assert len(out) == len(items)
    items[:] = out
