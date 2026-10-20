#pragma once
// Reads the DLC folder off the GUI thread. A scan runs only when asked (the interface asks when it is
// shown and every 60 s while visible), files are parsed again only when their size or write time
// changed, and each scan is published as an immutable snapshot (Current) that frames read for free.

#include "dlc/dlc_join.h"

#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace dlc {

// A folder holding DLC's files: its profile (calibration_profile.yaml) or stack registry (stack_registry.json), or (a
// DLC not set up yet) its sources beside the example profile (src\dlc, calibration_profile.example.yaml).
bool LooksLikeDlcFolder(const std::wstring& dir);
// Where an unconfigured DesktopLUT looks for DLC, best first: next to the exe (<exe>\DLC, or the exe's own folder),
// beside the exe's folder (<exe>\..\DLC), a source checkout's layout (<exe>\..\..\DLC: bin\Release), then `lastUsed`
// (the folder a scan last found it in). Full paths, no duplicates; no file is touched.
std::vector<std::wstring> FolderCandidates(const std::wstring& exeDir, const std::wstring& lastUsed);
// The folder to read: the configured one (authoritative: as it is, even when it does not exist), else the first
// candidate that holds DLC's files; "" none. `detected` (optional): true when it came from the candidates. Touches the
// file system (the scan worker calls it, never the GUI thread).
std::wstring ResolveFolder(const std::wstring& configured, const std::vector<std::wstring>& candidates,
                           bool* detected = nullptr);
// The newest verification_iterNN_patch_metrics.json of a run ("" none).
std::wstring PatchMetricsPath(const std::wstring& runDir);

// Parsed files kept between scans, keyed by path; reused while size and write time are unchanged.
struct ScanCache {
    struct Stamp { uint64_t size = 0, mtime = 0; bool operator==(const Stamp& o) const { return size == o.size && mtime == o.mtime; } };
    struct CachedReport { Stamp stamp; bool ok = false; Report report; };   // ok false: unreadable at this stamp
    std::map<std::wstring, CachedReport> reports;
    std::map<std::wstring, std::pair<Stamp, std::vector<std::wstring>>> runDone;   // events.jsonl -> results dirs
};

// One synchronous scan (the worker's body; tests call it on fixtures). `today` is YYYY-MM-DD. `cancel`
// set: returns early with what it has (shutdown).
std::shared_ptr<Data> ScanFolder(const std::wstring& folder, ScanCache& cache, const std::string& today,
                                 const std::atomic<bool>* cancel = nullptr);

// ---- the worker ------------------------------------------------------------------------------------
void SetScanListener(std::function<void()> onChange);   // called on the worker after each publish / load
// Any thread; coalesced; starts the worker. The worker resolves the folder (ResolveFolder) and scans it. Returns the
// request's number: Data::request of the scan that answers it.
uint64_t RequestScan(const std::wstring& configured, const std::vector<std::wstring>& candidates);
void StopScanner();                                       // GUI thread, at shutdown
std::shared_ptr<const Data> Current();                    // any thread; never null
// Patch metrics of a run: the cached list, or null while it loads (the listener fires when it is in).
std::shared_ptr<const std::vector<Patch>> Patches(const std::wstring& runDir);

}  // namespace dlc
