#include "shell_open.h"

#include <windows.h>
#include <exdisp.h>
#include <servprov.h>
#include <shellapi.h>
#include <shldisp.h>
#include <shlguid.h>
#include <shlobj.h>

namespace {

template <class T> void SafeRelease(T*& p) {
    if (p) { p->Release(); p = nullptr; }
}

bool IsWebUrl(const std::wstring& url) {
    return _wcsnicmp(url.c_str(), L"https://", 8) == 0 || _wcsnicmp(url.c_str(), L"http://", 7) == 0;
}

// The desktop shell's ShellExecute: the desktop window's folder view → its Shell.Application → ShellExecute. The
// process it starts is explorer's child, with the signed-in user's (unelevated) token.
bool DesktopShellExecute(const std::wstring& file) {
    IShellWindows* windows = nullptr;
    IDispatch* desktopDisp = nullptr;
    IServiceProvider* provider = nullptr;
    IShellBrowser* browser = nullptr;
    IShellView* view = nullptr;
    IDispatch* backgroundDisp = nullptr;
    IShellFolderViewDual* folderView = nullptr;
    IDispatch* appDisp = nullptr;
    IShellDispatch2* shell = nullptr;
    bool ok = false;

    VARIANT loc, root;
    VariantInit(&loc);
    VariantInit(&root);
    V_VT(&loc) = VT_I4;
    V_I4(&loc) = CSIDL_DESKTOP;
    long hwnd = 0;
    if (SUCCEEDED(CoCreateInstance(CLSID_ShellWindows, nullptr, CLSCTX_LOCAL_SERVER, IID_PPV_ARGS(&windows))) &&
        windows->FindWindowSW(&loc, &root, SWC_DESKTOP, &hwnd, SWFO_NEEDDISPATCH, &desktopDisp) == S_OK && desktopDisp &&
        SUCCEEDED(desktopDisp->QueryInterface(IID_PPV_ARGS(&provider))) &&
        SUCCEEDED(provider->QueryService(SID_STopLevelBrowser, IID_PPV_ARGS(&browser))) &&
        SUCCEEDED(browser->QueryActiveShellView(&view)) &&
        SUCCEEDED(view->GetItemObject(SVGIO_BACKGROUND, IID_PPV_ARGS(&backgroundDisp))) &&
        SUCCEEDED(backgroundDisp->QueryInterface(IID_PPV_ARGS(&folderView))) &&
        SUCCEEDED(folderView->get_Application(&appDisp)) && appDisp &&
        SUCCEEDED(appDisp->QueryInterface(IID_PPV_ARGS(&shell)))) {
        // The browser explorer starts may take the foreground (this window has it now).
        DWORD shellPid = 0;
        if (hwnd) GetWindowThreadProcessId((HWND)(LONG_PTR)hwnd, &shellPid);
        if (shellPid) AllowSetForegroundWindow(shellPid);
        BSTR bFile = SysAllocString(file.c_str());
        VARIANT args, dir, op, show;
        VariantInit(&args);
        VariantInit(&dir);
        VariantInit(&op);
        VariantInit(&show);
        V_VT(&show) = VT_I4;
        V_I4(&show) = SW_SHOWNORMAL;
        ok = bFile && SUCCEEDED(shell->ShellExecute(bFile, args, dir, op, show));   // op empty: the default verb
        SysFreeString(bFile);
    }
    SafeRelease(shell);
    SafeRelease(appDisp);
    SafeRelease(folderView);
    SafeRelease(backgroundDisp);
    SafeRelease(view);
    SafeRelease(browser);
    SafeRelease(provider);
    SafeRelease(desktopDisp);
    SafeRelease(windows);
    return ok;
}

}  // namespace

bool ProcessIsElevated() {
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return true;   // unknown: assume the worst
    TOKEN_ELEVATION e = {};
    DWORD got = 0;
    const bool ok = GetTokenInformation(token, TokenElevation, &e, sizeof(e), &got) != FALSE;
    CloseHandle(token);
    return !ok || e.TokenIsElevated != 0;
}

bool OpenUrlAsUser(const std::wstring& url) {
    if (!IsWebUrl(url)) return false;
    if (!ProcessIsElevated())
        return (INT_PTR)ShellExecuteW(nullptr, L"open", url.c_str(), nullptr, nullptr, SW_SHOWNORMAL) > 32;
    return DesktopShellExecute(url);   // never a direct ShellExecute here: that browser would run elevated
}
