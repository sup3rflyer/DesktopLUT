// DesktopLUT - whitelist.h
// Process whitelist checking (gamma, VRR, MHC profile monitoring)

#pragma once

#include <string>
#include <vector>

// Gamma whitelist polling thread control
void StartGammaWhitelistThread();
void StopGammaWhitelistThread();

// The thread's two system reads besides the process snapshot (exposed for the tests; whitelist_logic.h says when
// each is used).
// The pids of every running process, sorted ascending. False = could not be read.
bool ReadSortedProcessIds(std::vector<unsigned long>& pids);
// A handle (SYNCHRONIZE) that signals when process `pid` ends, only if that process is still the program the process
// list named `exeName` (the image file's name, case-insensitive). Null otherwise. The caller closes it.
void* OpenWatchedProcess(unsigned long pid, const std::wstring& exeName);
