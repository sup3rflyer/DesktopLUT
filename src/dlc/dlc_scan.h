#pragma once
// Reads the DLC folder off the GUI thread. A scan runs only when asked (the interface asks when it is
// shown and every 60 s while visible), files are parsed again only when their size or write time
// changed, and each scan is published as an immutable snapshot (Current) that frames read for free.

#include "dlc/dlc_join.h"

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace dlc {

// A folder holding DLC's files (calibration_profile.yaml or stack_registry.json).
bool LooksLikeDlcFolder(const std::wstring& dir);
// The folder to read: the configured one, else <exeDir>\..\..\DLC when it holds DLC's files; "" none.
std::wstring ResolveFolder(const std::wstring& configured, const std::wstring& exeDir);
// The newest verification_iterNN_patch_metrics.json of a run ("" none).
std::wstring PatchMetricsPath(const std::wstring& runDir);

// Parsed files kept between scans, keyed by path; reused while size and write time are unchanged.
struct ScanCache {
    struct Stamp { uint64_t size = 0, mtime = 0; bool operator==(const Stamp& o) const { return size == o.size && mtime == o.mtime; } };
    std::map<std::wstring, std::pair<Stamp, Report>> reports;
    std::map<std::wstring, std::pair<Stamp, std::vector<std::wstring>>> runDone;   // events.jsonl -> results dirs
};

// One synchronous scan (the worker's body; tests call it on fixtures). `today` is YYYY-MM-DD.
std::shared_ptr<Data> ScanFolder(const std::wstring& folder, ScanCache& cache, const std::string& today);

// ---- the worker ------------------------------------------------------------------------------------
void SetScanListener(std::function<void()> onChange);   // called on the worker after each publish / load
void RequestScan(const std::wstring& folder);            // any thread; coalesced; starts the worker
void StopScanner();                                       // GUI thread, at shutdown
std::shared_ptr<const Data> Current();                    // any thread; never null
// Patch metrics of a run: the cached list, or null while it loads (the listener fires when it is in).
std::shared_ptr<const std::vector<Patch>> Patches(const std::wstring& runDir);

}  // namespace dlc
