"""The mission-control DASHBOARD — the human-eyes consumer of the run spine.

This is the third corner of the v2 three-consumer model (§1): the *core* owns the
mechanics, the *LLM* judges digests at seams, and **mission control** (this package)
is the live readout a human watches. It tails the one spine — ``events.jsonl`` — and
renders the whole run: status bar, phase header, the event-log firehose, liveness,
progress/timers, and the dE big-numbers.

Design contract (so it stays robust and decoupled):

* **Dumb browser, smart server.** All colour math (CCT/Duv from a patch's xy) and all
  aggregation (counters, timers, rolling rates, the liveness verdict) happen in Python;
  the browser only renders the state the server pushes. No colour science in JS.
* **Stdlib only.** Like the spine itself, the dashboard depends on nothing beyond the
  Python standard library — it can run on a bare monitoring box without the engine
  extras. The authoritative dE/white still come from the scoring stage (on the spine
  as ``metrics_scored``); the dashboard's live CCT/Duv is a Robertson readout, clearly
  a monitoring aid, never the calibration's source of truth.
* **Survives the run.** It judges liveness from *data freshness* (the age of the last
  event), not from a live socket, and it keeps showing the last run after it ends —
  following ``runs/active.json`` to the next run when one starts.
"""

from __future__ import annotations
