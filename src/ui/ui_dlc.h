#pragma once
// The DLC page: DLC's run in progress, the record of a display's past runs, and the run's seams. Calibration is
// LLM-driven: Claude starts runs and decides their seams; this page shows them and offers PAUSE / RESUME / CANCEL
// (DLC's control.json). It reads only DLC's files (dlc_live for the run, dlc_scan for the record) and draws what
// DLC computed — no colour maths of its own.

#include "ui_views.h"
#include <string>

namespace ui {

void DrawDlcPage(const Snapshot& snap, ViewState& vs);

// The run's light for the DLC tab: none when no run is in progress.
Light DlcRunLight(const Snapshot& snap);
// The sidebar's badge for the display a run measures ("MEASURING 59%", "WAITING", ...); false for any other.
bool DlcRunBadge(const Snapshot& snap, const DisplayRow& r, std::string& text, ImU32& col);

}  // namespace ui
