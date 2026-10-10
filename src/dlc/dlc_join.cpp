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

DisplayMatch MatchDisplay(const Data& d, const LiveDisplay& live) {
    DisplayMatch m;
    // 1. Profile hardware id.
    if (!live.hardwareId.empty()) {
        std::vector<const ProfileDisplay*> hits;
        for (const ProfileDisplay& p : d.profile.displays)
            if (SameId(p.hardwareId, live.hardwareId)) hits.push_back(&p);
        if (hits.size() > 1) {   // identical twins: desktoplut_monitor breaks the tie, nothing else
            std::vector<const ProfileDisplay*> byIndex;
            for (const ProfileDisplay* p : hits) if (p->monitor == live.monitor) byIndex.push_back(p);
            if (byIndex.size() == 1) hits = byIndex;
        }
        if (hits.size() == 1) {
            m.name = hits[0]->name;
            m.by = MatchBy::HardwareId;
            return m;
        }
        if (hits.size() > 1) {
            m.ambiguous = true;
            return m;
        }
    }
    // 2. What DLC recorded applying is what this display runs.
    std::vector<std::string> names;
    for (const StackRecord& s : d.stacks) {
        for (int k = 0; k < 2; k++) {
            if (SamePath(s.baseLut, live.mhcSource[k]) || SamePath(s.cubePath, live.lut[k])) AddUnique(names, s.display);
        }
    }
    if (names.size() == 1) {
        m.name = names[0];
        m.by = MatchBy::StackEvidence;
        return m;
    }
    if (names.size() > 1) {
        m.ambiguous = true;
        return m;
    }
    // 3. A verify-only report recorded this display's hardware id.
    if (!live.hardwareId.empty()) {
        for (const Report& r : d.reports)
            if (SameId(r.hardwareId, live.hardwareId)) AddUnique(names, r.display);
        if (names.size() == 1) {
            m.name = names[0];
            m.by = MatchBy::ReportIdentity;
            return m;
        }
        if (names.size() > 1) m.ambiguous = true;
    }
    return m;
}

bool ReportVerifiesLive(const Report& r, const StackRecord* stack, int modeIndex, const LiveDisplay& live) {
    if (!r.verification.present) return false;
    const std::wstring& lut = live.lut[modeIndex];
    if (!lut.empty()) return SamePath(lut, r.runtimeCube) || SamePath(lut, r.cubePath) || SamePath(lut, r.deliveredCube);
    // No cube: a report without one, from the run that installed the MHC profile in use.
    if (!r.runtimeCube.empty() || !r.cubePath.empty()) return false;
    if (!stack || stack->runId.empty() || !SamePath(stack->baseLut, live.mhcSource[modeIndex])) return false;
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
        if (rec.latest < 0) rec.latest = (int)i;
        if (rec.current < 0 && ReportVerifiesLive(r, st, modeIndex, live)) rec.current = (int)i;
    }
    if (rec.current >= 0) rec.verifiedDaysAgo = DaysBetween(d.reports[rec.current].date, d.today);

    rec.correctionMade = d.profile.correctionMade;
    for (const CorrectionRecord& c : d.corrections)
        if (c.display == name && c.mode == mode && !c.made.empty()) { rec.correctionMade = c.made; break; }
    if (!rec.correctionMade.empty()) {
        rec.correctionDaysAgo = DaysBetween(rec.correctionMade, d.today);
        rec.correctionStale = rec.correctionDaysAgo > d.profile.correctionMaxAgeDays;
    }
    rec.reminderDue = (reminderDays > 0 && rec.verifiedDaysAgo > reminderDays) || rec.correctionStale;
    return rec;
}

}  // namespace dlc
