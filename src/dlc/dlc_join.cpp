#include "dlc/dlc_join.h"

#include <cctype>
#include <cwctype>

namespace dlc {

namespace {

bool SameId(const std::string& a, const std::string& b) {
    if (a.empty() || a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++)
        if (toupper((unsigned char)a[i]) != toupper((unsigned char)b[i])) return false;
    return true;
}

const char* ModeName(int m) { return m == 1 ? "HDR" : "SDR"; }

void AddUnique(std::vector<std::string>& v, const std::string& s) {
    for (const std::string& x : v) if (x == s) return;
    v.push_back(s);
}

}  // namespace

std::string HardwareIdOf(const std::wstring& edidId, const std::wstring& devicePath) {
    if (!edidId.empty()) {
        const size_t dash = edidId.find(L'-');
        return WideToUtf8(edidId.substr(0, dash));
    }
    std::wstring up;
    for (wchar_t c : devicePath) up += (wchar_t)towupper(c);
    const size_t at = up.find(L"DISPLAY#");
    if (at == std::wstring::npos) return {};
    const size_t start = at + 8, end = devicePath.find(L'#', start);
    if (end == std::wstring::npos) return {};
    return WideToUtf8(devicePath.substr(start, end - start));
}

namespace {

// Does what DLC knows of `name`'s hardware id (the profile's quirks.hardware_id, the identities its
// verify-only reports recorded) contradict the live display's?
bool IdContradicts(const Data& d, const std::string& name, const std::string& hw) {
    if (hw.empty()) return false;
    bool known = false;
    for (const ProfileDisplay& p : d.profile.displays) {
        if (p.name != name || p.hardwareId.empty()) continue;
        if (SameId(p.hardwareId, hw)) return false;
        known = true;
    }
    for (const Report& r : d.reports) {
        if (r.display != name || r.hardwareId.empty()) continue;
        if (SameId(r.hardwareId, hw)) return false;
        known = true;
    }
    return known;
}

// DLC display names whose recorded stack is what this display runs (file evidence), minus names whose
// known hardware id says it is another panel.
std::vector<std::string> StackEvidence(const Data& d, const LiveDisplay& live) {
    std::vector<std::string> names;
    for (const StackRecord& s : d.stacks)
        for (int k = 0; k < 2; k++)
            if ((SamePath(s.baseLut, live.mhcSource[k]) || SamePath(s.cubePath, live.lut[k])) &&
                !IdContradicts(d, s.display, live.hardwareId))
                AddUnique(names, s.display);
    return names;
}

bool Contains(const std::vector<std::string>& v, const std::string& s) {
    for (const std::string& x : v) if (x == s) return true;
    return false;
}

}  // namespace

DisplayMatch MatchDisplay(const Data& d, const LiveDisplay& live) {
    DisplayMatch m;
    const std::vector<std::string> byFiles = StackEvidence(d, live);
    // 1. Profile hardware id. Identical twins (two profile entries with this id, or two live panels of
    //    this model) are told apart by desktoplut_monitor, then by file evidence, never guessed.
    if (!live.hardwareId.empty()) {
        std::vector<const ProfileDisplay*> hits;
        for (const ProfileDisplay& p : d.profile.displays)
            if (SameId(p.hardwareId, live.hardwareId)) hits.push_back(&p);
        if (!hits.empty() && (hits.size() > 1 || live.sameModelCount > 1)) {
            std::vector<const ProfileDisplay*> byIndex, byFile;
            for (const ProfileDisplay* p : hits) {
                if (p->monitor == live.monitor) byIndex.push_back(p);
                if (Contains(byFiles, p->name)) byFile.push_back(p);
            }
            if (byIndex.size() == 1) hits = byIndex;
            else if (byFile.size() == 1) hits = byFile;
            else {
                m.ambiguous = true;
                return m;
            }
        }
        if (hits.size() == 1) {
            m.name = hits[0]->name;
            m.by = MatchBy::HardwareId;
            return m;
        }
    }
    // 2. What DLC recorded applying is what this display runs.
    if (byFiles.size() == 1) {
        m.name = byFiles[0];
        m.by = MatchBy::StackEvidence;
        return m;
    }
    if (byFiles.size() > 1) {
        m.ambiguous = true;
        return m;
    }
    // 3. A verify-only report recorded this display's hardware id.
    if (!live.hardwareId.empty()) {
        std::vector<std::string> names;
        for (const Report& r : d.reports)
            if (SameId(r.hardwareId, live.hardwareId)) AddUnique(names, r.display);
        if (names.size() == 1 && live.sameModelCount <= 1) {
            m.name = names[0];
            m.by = MatchBy::ReportIdentity;
            return m;
        }
        if (!names.empty()) m.ambiguous = true;
    }
    return m;
}

bool ReportVerifiesLive(const Report& r, const StackRecord* stack, int modeIndex, const LiveDisplay& live) {
    if (!r.verification.present) return false;
    if (!r.contentMode.empty() && r.contentMode != r.mode) return false;   // SDR content checked on an HDR display
    const std::wstring& lut = live.lut[modeIndex];
    const std::wstring& mhc = live.mhcSource[modeIndex];
    if (!lut.empty()) {
        if (!SamePath(lut, r.runtimeCube) && !SamePath(lut, r.cubePath) && !SamePath(lut, r.deliveredCube)) return false;
        // ...measured over the MHC profile that is installed now.
        if (!r.verifiedMhcSource.empty()) return SamePath(mhc, r.verifiedMhcSource);
        if (stack && (!mhc.empty() || !stack->baseLut.empty())) return SamePath(mhc, stack->baseLut);
        return true;
    }
    // No cube: a report without one, from the run that installed the MHC profile in use.
    if (!r.runtimeCube.empty() || !r.cubePath.empty()) return false;
    if (!stack || stack->runId.empty() || !SamePath(stack->baseLut, mhc)) return false;
    return RunIdFromPath(r.runDir) == stack->runId;
}

ModeRecord RecordFor(const Data& d, const std::string& name, int modeIndex, const LiveDisplay& live, int reminderDays) {
    ModeRecord rec;
    const char* mode = ModeName(modeIndex);
    for (size_t i = 0; i < d.stacks.size(); i++)
        if (d.stacks[i].display == name && d.stacks[i].mode == mode) { rec.stack = (int)i; break; }
    const StackRecord* st = rec.stack >= 0 ? &d.stacks[rec.stack] : nullptr;

    auto layer = [](const std::wstring& running, const std::wstring& recorded) {
        if (running.empty()) return LayerState::Off;
        if (recorded.empty()) return LayerState::Unrecorded;
        return SamePath(running, recorded) ? LayerState::Matches : LayerState::Differs;
    };
    rec.mhc = layer(live.mhcSource[modeIndex], st ? st->baseLut : std::wstring());
    rec.cube = layer(live.lut[modeIndex], st ? st->cubePath : std::wstring());

    for (size_t i = 0; i < d.reports.size(); i++) {   // newest first
        const Report& r = d.reports[i];
        if (r.display != name || r.mode != mode) continue;
        // A check of SDR content shown on an HDR display measures another pipeline than the mode's own.
        if (!r.contentMode.empty() && r.contentMode != r.mode) continue;
        if (rec.latest < 0) rec.latest = (int)i;
        if (rec.current < 0 && ReportVerifiesLive(r, st, modeIndex, live)) rec.current = (int)i;
    }
    if (rec.current >= 0) rec.verifiedDaysAgo = DaysBetween(d.reports[rec.current].date, d.today);

    // The meter correction (DLC's correction_staleness): the active file is the store's record for this
    // display and mode, else the profile's; no file, no correction (and nothing to go stale). Its date is
    // the store's, else the profile's.
    const CorrectionRecord* store = nullptr;
    for (const CorrectionRecord& c : d.corrections)
        if (c.display == name && c.mode == mode) { store = &c; break; }
    const std::string file = store ? WideToUtf8(store->file) : d.profile.correctionFile;
    if (!file.empty()) {
        rec.correctionMade = store && !store->made.empty() ? store->made : d.profile.correctionMade;
        if (!rec.correctionMade.empty()) {
            rec.correctionDaysAgo = DaysBetween(rec.correctionMade, d.today);
            rec.correctionStale = rec.correctionDaysAgo > d.profile.correctionMaxAgeDays;
        }
    }
    rec.reminderDue = (reminderDays > 0 && rec.verifiedDaysAgo > reminderDays) || rec.correctionStale;
    return rec;
}

}  // namespace dlc
