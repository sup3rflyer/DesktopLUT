// Quick symbol resolver for DWM hook repair.
// Resolves function RVAs from dwmcore.pdb using DbgHelp.
// Build: cl /EHsc resolve_symbols.cpp /link dbghelp.lib
// Run:   resolve_symbols.exe

#include <windows.h>
#include <dbghelp.h>
#include <cstdio>
#include <cstring>

#pragma comment(lib, "dbghelp.lib")

struct EnumCtx {
    const char** targets;
    int numTargets;
    DWORD64 moduleBase;
};

static BOOL CALLBACK EnumCallback(PSYMBOL_INFO si, ULONG, PVOID ctx) {
    auto* c = (EnumCtx*)ctx;
    for (int i = 0; i < c->numTargets; i++) {
        if (strstr(si->Name, c->targets[i])) {
            DWORD64 rva = si->Address - c->moduleBase;
            printf("FOUND: %s  RVA=0x%llX  size=%lu\n", si->Name, rva, si->Size);
        }
    }
    return TRUE;
}

int main() {
    const char* dllPath = "C:\\Windows\\System32\\dwmcore.dll";
    const char* symPath = "C:\\Windows\\Temp\\DesktopLUT_symbols";

    HANDLE hProc = (HANDLE)0xBEEF;
    SymSetOptions(SYMOPT_UNDNAME | SYMOPT_LOAD_LINES | SYMOPT_DEBUG);

    if (!SymInitialize(hProc, symPath, FALSE)) {
        printf("SymInitialize failed: %lu\n", GetLastError());
        return 1;
    }

    DWORD64 base = SymLoadModuleEx(hProc, NULL, dllPath, NULL, 0x10000000, 0, NULL, 0);
    if (!base) {
        printf("SymLoadModuleEx failed: %lu\n", GetLastError());
        SymCleanup(hProc);
        return 1;
    }
    printf("Module loaded at base 0x%llX\n\n", base);

    IMAGEHLP_MODULE64 modInfo = {};
    modInfo.SizeOfStruct = sizeof(modInfo);
    if (SymGetModuleInfo64(hProc, base, &modInfo)) {
        printf("Module: %s\n", modInfo.ModuleName);
        printf("SymType: %d (1=COFF, 3=PDB, 4=Export, 5=Deferred, 6=SYM, 7=DIA)\n", modInfo.SymType);
        printf("PDB: %s\n\n", modInfo.LoadedPdbName);
    } else {
        printf("SymGetModuleInfo64 failed: %lu\n\n", GetLastError());
    }

    // Search broadly for DirectFlip, IndependentFlip, Promotion, and overlay-related
    const char* targets[] = {
        "DirectFlip",
        "IndependentFlip",
        "Promotion",
        "CWindowContext",
        "CCompSwapChain",
        "CCompVisual",
        "OverlayTestMode",
        "COverlayContext",
        "Flip",
    };
    int numTargets = sizeof(targets) / sizeof(targets[0]);

    EnumCtx ctx = { targets, numTargets, base };

    printf("=== Enumerating matching symbols ===\n");
    if (!SymEnumSymbols(hProc, base, "*", EnumCallback, &ctx)) {
        printf("SymEnumSymbols failed: %lu\n", GetLastError());
    }

    // Also try specific resolution for key functions
    printf("\n=== Direct resolution attempts ===\n");
    const char* directNames[] = {
        "COverlayContext::Present",
        "COverlayContext::IsCandidateDirectFlipCompatible",
        "CWindowContext::IsCandidateDirectFlipCompatible",
        "CCompSwapChain::IsCandidateDirectFlipCompatible",
        "CCompSwapChain::IsCandidateIndependentFlipCompatible",
        "CCompVisual::IsCandidateForPromotion",
        "COverlayContext::OverlaysEnabled",
    };

    for (auto name : directNames) {
        BYTE buf[sizeof(SYMBOL_INFO) + MAX_SYM_NAME];
        SYMBOL_INFO* sym = (SYMBOL_INFO*)buf;
        sym->SizeOfStruct = sizeof(SYMBOL_INFO);
        sym->MaxNameLen = MAX_SYM_NAME;

        if (SymFromName(hProc, name, sym)) {
            DWORD64 rva = sym->Address - base;
            printf("OK   %-60s RVA=0x%llX  size=%lu\n", sym->Name, rva, sym->Size);
        } else {
            printf("FAIL %-60s error=%lu\n", name, GetLastError());
        }
    }

    SymCleanup(hProc);
    return 0;
}
