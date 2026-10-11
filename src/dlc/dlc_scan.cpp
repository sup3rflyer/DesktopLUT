#include "dlc/dlc_scan.h"
#include "dlc/dlc_yaml.h"   // kMaxYamlBytes

#include <windows.h>
#include <algorithm>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>

namespace dlc {

namespace {

// Size caps per kind of file (DLC's real ones: profile ~10 KB, stores and reports ~40 KB, patch metrics
// ~120 KB): anything far larger is not DLC's, and parsing it would cost memory for nothing.
constexpr uint64_t kProfileBytes = kMaxYamlBytes;
constexpr uint64_t kStoreBytes = 4ull << 20;
constexpr uint64_t kReportBytes = 4ull << 20;
constexpr uint64_t kPatchBytes = 16ull << 20;
constexpr uint64_t kEventsTailBytes = 256u << 10;   // run_done sits near the end of events.jsonl

bool Stat(const std::wstring& path, ScanCache::Stamp& st, bool* isDir = nullptr) {
    WIN32_FILE_ATTRIBUTE_DATA a;
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &a)) return false;
    st.size = ((uint64_t)a.nFileSizeHigh << 32) | a.nFileSizeLow;
    st.mtime = ((uint64_t)a.ftLastWriteTime.dwHighDateTime << 32) | a.ftLastWriteTime.dwLowDateTime;
    if (isDir) *isDir = (a.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
    return true;
}

bool IsFile(const std::wstring& path) {
    ScanCache::Stamp st;
    bool dir = false;
    return Stat(path, st, &dir) && !dir;
}

bool IsDir(const std::wstring& path) {
    ScanCache::Stamp st;
    bool dir = false;
    return Stat(path, st, &dir) && dir;
}

// Reads a whole file (or its last `tail` bytes) with every share mode: DLC may be writing or replacing it.
bool ReadShared(const std::wstring& path, std::string& out, uint64_t maxBytes, uint64_t tail = 0) {
    out.clear();
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size = {};
    bool ok = GetFileSizeEx(h, &size) != 0;
    uint64_t want = ok ? (uint64_t)size.QuadPart : 0;
    if (ok && tail && want > tail) {
        LARGE_INTEGER off;
        off.QuadPart = (LONGLONG)(want - tail);
        ok = SetFilePointerEx(h, off, nullptr, FILE_BEGIN) != 0;
        want = tail;
    }
    if (ok && want > maxBytes) ok = false;
    if (ok && want) {
        out.resize((size_t)want);
        DWORD got = 0;
        ok = ReadFile(h, out.data(), (DWORD)want, &got, nullptr) != 0;
        out.resize(got);
    }
    CloseHandle(h);
    return ok;
}

std::vector<std::wstring> ListDirs(const std::wstring& dir) {
    std::vector<std::wstring> out;
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileExW((dir + L"\\*").c_str(), FindExInfoBasic, &fd, FindExSearchLimitToDirectories, nullptr, 0);
    if (h == INVALID_HANDLE_VALUE) return out;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        const std::wstring n = fd.cFileName;
        if (n == L"." || n == L"..") continue;
        out.push_back(n);
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    std::sort(out.begin(), out.end());
    return out;
}

std::string Today() {
    SYSTEMTIME st;
    GetLocalTime(&st);
    char b[16];
    snprintf(b, sizeof(b), "%04u-%02u-%02u", st.wYear, st.wMonth, st.wDay);
    return b;
}

// "..._verify-only_20261003_232912_382704" / "..._refine-mhc_20260925_160838" -> the run-id prefix.
std::wstring FolderStamp(const std::wstring& folderName) {
    for (const wchar_t* tag : { L"_verify-only_", L"_refine-mhc_" }) {
        const size_t at = folderName.rfind(tag);
        if (at != std::wstring::npos) return folderName.substr(at + wcslen(tag));
    }
    return {};
}

}  // namespace

bool LooksLikeDlcFolder(const std::wstring& dir) {
    return !dir.empty() && (IsFile(dir + L"\\calibration_profile.yaml") || IsFile(dir + L"\\stack_registry.json"));
}

std::wstring ResolveFolder(const std::wstring& configured, const std::wstring& exeDir) {
    if (!configured.empty()) return configured;
    if (exeDir.empty()) return {};
    wchar_t full[MAX_PATH] = {};
    const std::wstring guess = exeDir + L"\\..\\..\\DLC";
    if (!GetFullPathNameW(guess.c_str(), MAX_PATH, full, nullptr)) return {};
    return LooksLikeDlcFolder(full) ? std::wstring(full) : std::wstring();
}

std::wstring PatchMetricsPath(const std::wstring& runDir) {
    if (runDir.empty()) return {};
    const std::wstring dir = runDir + L"\\reports";
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW((dir + L"\\verification_iter*_patch_metrics.json").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return {};
    std::wstring best;
    do {
        const std::wstring n = fd.cFileName;
        if (best.empty() || n > best) best = n;   // iterNN is zero-padded: the largest name is the last iteration
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return best.empty() ? std::wstring() : dir + L"\\" + best;
}

std::shared_ptr<Data> ScanFolder(const std::wstring& folder, ScanCache& cache, const std::string& today,
                                 const std::atomic<bool>* cancel) {
    auto stop = [cancel] { return cancel && cancel->load(std::memory_order_relaxed); };
    auto d = std::make_shared<Data>();
    d->today = today;
    if (folder.empty() || !IsDir(folder)) return d;
    d->folder = folder;
    std::string text;

    const std::wstring profilePath = folder + L"\\calibration_profile.yaml";
    if (ReadShared(profilePath, text, kProfileBytes)) {
        d->profileFound = true;
        d->profile = ParseProfile(text, folder);
    } else {
        d->profile.outputDir = folder + L"\\results";
    }
    if (ReadShared(folder + L"\\stack_registry.json", text, kStoreBytes)) d->stacks = ParseStackRegistry(text);
    if (ReadShared(folder + L"\\correction_store.json", text, kStoreBytes)) d->corrections = ParseCorrectionStore(text);

    // Runs: results folder -> run folder, from each run's run_done event (the reliable link).
    const std::wstring runsDir = folder + L"\\runs";
    std::vector<std::wstring> runNames;
    std::map<std::wstring, std::wstring> runOfResults;   // NormPath(results dir) -> run dir
    for (const std::wstring& n : ListDirs(runsDir)) {
        if (stop()) return d;   // shutting down: the snapshot is discarded anyway
        if (!IsRunFolderName(n)) continue;
        runNames.push_back(n);
        const std::wstring events = runsDir + L"\\" + n + L"\\events.jsonl";
        ScanCache::Stamp st;
        if (!Stat(events, st)) continue;
        auto it = cache.runDone.find(events);
        if (it == cache.runDone.end() || !(it->second.first == st)) {
            std::vector<std::wstring> dirs;
            if (ReadShared(events, text, kEventsTailBytes, kEventsTailBytes)) dirs = RunDoneResultsDirs(text);
            it = cache.runDone.insert_or_assign(events, std::make_pair(st, std::move(dirs))).first;
        }
        for (const std::wstring& r : it->second.second) runOfResults[NormPath(r)] = runsDir + L"\\" + n;
    }

    // Reports: <output>\<folder>\report.json (folders starting with '_' or '.' are DLC's scratch).
    const std::wstring out = d->profile.outputDir;
    for (const std::wstring& n : ListDirs(out)) {
        if (stop()) return d;
        if (n.empty() || n[0] == L'_' || n[0] == L'.') continue;
        const std::wstring dir = out + L"\\" + n;
        const std::wstring path = dir + L"\\report.json";
        ScanCache::Stamp st;
        if (!Stat(path, st)) continue;
        auto it = cache.reports.find(path);
        if (it == cache.reports.end() || !(it->second.stamp == st)) {
            ScanCache::CachedReport c;
            c.stamp = st;
            c.ok = ReadShared(path, text, kReportBytes) && ParseReport(text, c.report);
            if (!c.ok) {   // caught mid-write (DLC writes report.json in place): once more
                Sleep(150);
                c.report = Report();
                c.ok = ReadShared(path, text, kReportBytes) && ParseReport(text, c.report);
            }
            // A failure is remembered too: an unreadable file is not re-read until it changes.
            it = cache.reports.insert_or_assign(path, std::move(c)).first;
        }
        if (!it->second.ok) continue;
        Report r = it->second.report;
        r.path = path;
        r.folder = dir;
        r.mtime = st.mtime;
        // The run that wrote it: run_done, else the cube's runs\<id>, else the folder's stamp.
        if (auto f = runOfResults.find(NormPath(dir)); f != runOfResults.end()) {
            r.runDir = f->second;
        } else if (const std::string id = RunIdFromPath(r.cubePath); !id.empty() && IsDir(runsDir + L"\\" + Utf8ToWide(id))) {
            r.runDir = runsDir + L"\\" + Utf8ToWide(id);
        } else if (const std::wstring stamp = FolderStamp(n); !stamp.empty()) {
            for (const std::wstring& rn : runNames)
                if (rn.compare(0, stamp.size(), stamp) == 0) r.runDir = runsDir + L"\\" + rn;
        }
        d->reports.push_back(std::move(r));
    }
    // Newest first: by date, then by run id (it starts with the run's start time), then by file time
    // (a report can be rewritten after a later run's).
    // One lexicographic key (a strict weak order): date, then reports with a run id before those
    // without, then the run id, then the file time; all newest first.
    struct Key { std::string date; bool hasId; std::string id; uint64_t mtime; };
    auto keyOf = [](const Report& r) {
        const std::string id = RunIdFromPath(r.runDir);
        const bool valid = IsRunFolderName(Utf8ToWide(id));
        return Key{ r.date, valid, valid ? id : std::string(), r.mtime };
    };
    std::stable_sort(d->reports.begin(), d->reports.end(), [&](const Report& a, const Report& b) {
        const Key ka = keyOf(a), kb = keyOf(b);
        if (ka.date != kb.date) return ka.date > kb.date;
        if (ka.hasId != kb.hasId) return ka.hasId;
        if (ka.id != kb.id) return ka.id > kb.id;
        return ka.mtime > kb.mtime;
    });
    return d;
}

// ---- worker ----------------------------------------------------------------------------------------

namespace {

std::mutex g_mutex;
std::condition_variable g_cv;
std::thread g_thread;
bool g_stop = false;
std::atomic<bool> g_cancel{ false };   // StopScanner: an in-flight scan returns early
bool g_scanWanted = false;
std::wstring g_folder;
std::deque<std::wstring> g_patchQueue;
std::shared_ptr<const Data> g_current = std::make_shared<Data>();
std::map<std::wstring, std::shared_ptr<const std::vector<Patch>>> g_patches;   // run dir -> patches
std::function<void()> g_listener;

void Notify() {
    std::function<void()> fn;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        fn = g_listener;
    }
    if (fn) fn();
}

void WorkerMain() {
    ScanCache cache;
    for (;;) {
        std::wstring folder, patchRun;
        bool scan = false;
        {
            std::unique_lock<std::mutex> lock(g_mutex);
            g_cv.wait(lock, [] { return g_stop || g_scanWanted || !g_patchQueue.empty(); });
            if (g_stop) return;
            if (g_scanWanted) {
                scan = true;
                g_scanWanted = false;
                folder = g_folder;
            } else {
                patchRun = g_patchQueue.front();
                g_patchQueue.pop_front();
            }
        }
        if (scan) {
            std::shared_ptr<const Data> d = ScanFolder(folder, cache, Today(), &g_cancel);
            if (g_cancel.load()) return;
            {
                std::lock_guard<std::mutex> lock(g_mutex);
                g_current = d;
            }
            Notify();
        } else {
            std::vector<Patch> patches;
            const std::wstring path = PatchMetricsPath(patchRun);
            std::string text;
            if (!path.empty() && ReadShared(path, text, kPatchBytes)) patches = ParsePatchMetrics(text);
            {
                std::lock_guard<std::mutex> lock(g_mutex);
                g_patches[NormPath(patchRun)] = std::make_shared<const std::vector<Patch>>(std::move(patches));
            }
            Notify();
        }
    }
}

void EnsureWorkerLocked() {
    if (!g_thread.joinable() && !g_stop) g_thread = std::thread(WorkerMain);
}

}  // namespace

void SetScanListener(std::function<void()> onChange) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_listener = std::move(onChange);
}

void RequestScan(const std::wstring& folder) {
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (g_stop) return;
        if (NormPath(folder) != NormPath(g_folder)) g_patches.clear();   // another folder: other runs
        g_folder = folder;
        g_scanWanted = true;
        EnsureWorkerLocked();
    }
    g_cv.notify_one();
}

void StopScanner() {
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_stop = true;
        g_listener = nullptr;
        g_cancel.store(true);
    }
    g_cv.notify_one();
    if (g_thread.joinable()) g_thread.join();
}

std::shared_ptr<const Data> Current() {
    std::lock_guard<std::mutex> lock(g_mutex);
    return g_current;
}

std::shared_ptr<const std::vector<Patch>> Patches(const std::wstring& runDir) {
    if (runDir.empty()) return nullptr;
    const std::wstring key = NormPath(runDir);
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (auto it = g_patches.find(key); it != g_patches.end()) return it->second;
        if (g_stop) return nullptr;
        for (const std::wstring& q : g_patchQueue) if (NormPath(q) == key) return nullptr;
        g_patchQueue.push_back(runDir);
        EnsureWorkerLocked();
    }
    g_cv.notify_one();
    return nullptr;
}

}  // namespace dlc
