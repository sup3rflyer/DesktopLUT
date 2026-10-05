// vblank_truth — ground truth for a display's vblank count (DLC soak of FALD stage 3, 2026-10-05).
//
// Windows exposes an output's vblank counter (DXGI SyncRefreshCount + SyncQPCTime) only to a process that PRESENTS to it
// (IDXGIOutput::GetFrameStatistics returns DXGI_ERROR_INVALID_CALL windowed). So this tool shows a 1 x 1 BLACK, STATIC
// window on the target output's bottom-right pixel (inside the DWM hook's corner kick zone: not counted as content),
// presents it once per --interval-ms, and logs one (count, QPC) pair per present:
//     <log>: qpc_now, present_count, present_refresh, sync_refresh, sync_qpc, hr, out_left, out_top
// out_left / out_top = the output that present actually went to (IDXGISwapChain::GetContainingOutput): a display in
// standby can leave the desktop, and then the window would land on ANOTHER display and log its count — the analysis keeps
// only rows on the target. Every interval the window is re-pinned to the bottom-right pixel of the monitor that contains
// --at (MonitorFromPoint); while no monitor contains it (the target is gone) nothing is presented ("absent" rows).
// Nothing on screen ever changes (no toggling stimulus: an LCD must never see a polarity-locked toggle). DWM composes
// once per interval. Stop: "quit" on stdin, stdin EOF, or the process killed.
//
//     vblank_truth.exe --at X,Y --log PATH [--interval-ms 1000]
// X,Y = any desktop point on the target monitor (physical pixels). Prints "ready output=... rect=..." then runs.
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include <atomic>
#include <cstdio>
#include <share.h>
#include <string>
#include <thread>
#include <climits>
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "user32.lib")
using Microsoft::WRL::ComPtr;

static std::atomic<bool> g_quit{ false };

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == WM_NCHITTEST) return HTTRANSPARENT;     // clicks pass through
    return DefWindowProcW(h, m, w, l);
}

int main(int argc, char** argv) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    int px = 0, py = 0, intervalMs = 1000;
    std::string log;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--at" && i + 1 < argc) { if (sscanf_s(argv[++i], "%d,%d", &px, &py) != 2) { puts("fatal bad --at"); return 2; } }
        else if (a == "--log" && i + 1 < argc) log = argv[++i];
        else if (a == "--interval-ms" && i + 1 < argc) intervalMs = atoi(argv[++i]);
    }
    if (log.empty() || intervalMs < 50) { puts("fatal usage: vblank_truth --at X,Y --log PATH [--interval-ms 1000]"); return 2; }

    // the output containing (px, py)
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) { puts("fatal CreateDXGIFactory1"); return 2; }
    ComPtr<IDXGIAdapter1> adapter; ComPtr<IDXGIOutput> output; DXGI_OUTPUT_DESC od{};
    for (UINT ai = 0; !output && factory->EnumAdapters1(ai, &adapter) == S_OK; ai++) {
        ComPtr<IDXGIOutput> o;
        for (UINT oi = 0; adapter->EnumOutputs(oi, &o) == S_OK; oi++) {
            DXGI_OUTPUT_DESC d; o->GetDesc(&d);
            const RECT& r = d.DesktopCoordinates;
            if (d.AttachedToDesktop && px >= r.left && px < r.right && py >= r.top && py < r.bottom) { output = o; od = d; break; }
            o.Reset();
        }
        if (!output) adapter.Reset();
    }
    if (!output) { puts("fatal no output at --at"); return 2; }
    const RECT& R = od.DesktopCoordinates;

    WNDCLASSW wc{}; wc.lpfnWndProc = WndProc; wc.hInstance = GetModuleHandleW(nullptr); wc.lpszClassName = L"DlcVBlankTruth";
    RegisterClassW(&wc);
    HWND hwnd = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT, wc.lpszClassName,
                                L"vblank_truth", WS_POPUP, R.right - 1, R.bottom - 1, 1, 1, nullptr, nullptr, wc.hInstance, nullptr);
    if (!hwnd) { puts("fatal CreateWindowEx"); return 2; }
    ShowWindow(hwnd, SW_SHOWNOACTIVATE);

    ComPtr<ID3D11Device> dev; ComPtr<ID3D11DeviceContext> ctx;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    if (FAILED(D3D11CreateDevice(adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ctx))) {
        puts("fatal D3D11CreateDevice"); return 2;
    }
    ComPtr<IDXGIFactory2> f2; factory.As(&f2);
    DXGI_SWAP_CHAIN_DESC1 sd{};
    sd.Width = 1; sd.Height = 1; sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM; sd.SampleDesc.Count = 1;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; sd.BufferCount = 2; sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    ComPtr<IDXGISwapChain1> sc;
    if (FAILED(f2->CreateSwapChainForHwnd(dev.Get(), hwnd, &sd, nullptr, nullptr, &sc))) { puts("fatal CreateSwapChainForHwnd"); return 2; }
    f2->MakeWindowAssociation(hwnd, DXGI_MWA_NO_ALT_ENTER);
    ComPtr<ID3D11Texture2D> bb; sc->GetBuffer(0, IID_PPV_ARGS(&bb));
    ComPtr<ID3D11RenderTargetView> rtv; dev->CreateRenderTargetView(bb.Get(), nullptr, &rtv);

    FILE* f = _fsopen(log.c_str(), "w", _SH_DENYNO);   // shared: an analysis may read it while the soak runs
    if (!f) { puts("fatal cannot open --log"); return 2; }
    LARGE_INTEGER qf; QueryPerformanceFrequency(&qf);
    fprintf(f, "# qpcfreq=%lld output_left=%ld output_top=%ld interval_ms=%d\n", qf.QuadPart, R.left, R.top, intervalMs);
    fprintf(f, "qpc_now,present_count,present_refresh,sync_refresh,sync_qpc,hr,out_left,out_top\n");
    fflush(f);
    char name[64]{}; WideCharToMultiByte(CP_UTF8, 0, od.DeviceName, -1, name, sizeof name, nullptr, nullptr);
    printf("ready output=%s rect=%ld,%ld,%ld,%ld window=%ld,%ld\n", name, R.left, R.top, R.right, R.bottom, R.right - 1, R.bottom - 1);
    fflush(stdout);

    std::thread([] {                                   // stdin: "quit" or EOF stops
        char line[256];
        while (fgets(line, sizeof line, stdin)) if (strncmp(line, "quit", 4) == 0) break;
        g_quit = true;
    }).detach();

    const float black[4] = { 0.0f, 0.0f, 0.0f, 1.0f };
    ULONGLONG next = GetTickCount64();
    while (!g_quit.load()) {
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
        const ULONGLONG now = GetTickCount64();
        if (now < next) { Sleep((DWORD)(next - now < 20 ? next - now : 20)); continue; }
        next += (ULONGLONG)intervalMs;
        if (next < now) next = now + (ULONGLONG)intervalMs;   // (after a stall: no burst of catch-up presents)
        // the target = the monitor containing --at NOW (it may have left the desktop in standby, or moved)
        HMONITOR hm = MonitorFromPoint(POINT{ px, py }, MONITOR_DEFAULTTONULL);
        MONITORINFO mi{ sizeof(mi) };
        LARGE_INTEGER q0; QueryPerformanceCounter(&q0);
        if (!hm || !GetMonitorInfoW(hm, &mi)) {
            fprintf(f, "%lld,0,0,0,0,absent,0,0\n", q0.QuadPart);
            fflush(f);
            continue;
        }
        SetWindowPos(hwnd, HWND_TOPMOST, mi.rcMonitor.right - 1, mi.rcMonitor.bottom - 1, 1, 1, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        ctx->OMSetRenderTargets(1, rtv.GetAddressOf(), nullptr);
        ctx->ClearRenderTargetView(rtv.Get(), black);      // the same black pixel, every time
        HRESULT hr = sc->Present(1, 0);
        // the statistics of a displayed present: poll briefly until the count moves past the previous sample
        DXGI_FRAME_STATISTICS st{};
        HRESULT sh = E_FAIL;
        for (int k = 0; k < 12; k++) {
            Sleep(8);
            sh = sc->GetFrameStatistics(&st);
            UINT last = 0; sc->GetLastPresentCount(&last);
            if (SUCCEEDED(sh) && st.PresentCount == last) break;
        }
        LARGE_INTEGER q; QueryPerformanceCounter(&q);
        long ol = LONG_MIN, ot = LONG_MIN;
        ComPtr<IDXGIOutput> co;
        if (SUCCEEDED(sc->GetContainingOutput(&co)) && co) {
            DXGI_OUTPUT_DESC cd;
            if (SUCCEEDED(co->GetDesc(&cd))) { ol = cd.DesktopCoordinates.left; ot = cd.DesktopCoordinates.top; }
        }
        fprintf(f, "%lld,%u,%u,%u,%lld,0x%08lx,%ld,%ld\n", q.QuadPart, st.PresentCount, st.PresentRefreshCount, st.SyncRefreshCount,
                st.SyncQPCTime.QuadPart, (unsigned long)(FAILED(hr) ? hr : sh), ol, ot);
        fflush(f);
    }
    fclose(f);
    DestroyWindow(hwnd);
    return 0;
}
