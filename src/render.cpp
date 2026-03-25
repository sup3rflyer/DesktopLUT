// DesktopLUT - render.cpp
// Frame rendering, render loop, and overlay window procedure
// (Initialization code in render_init.cpp)

#include "render.h"
#include "globals.h"
#include "gpu.h"
#include "color.h"
#include "capture.h"
#include "osd.h"
#include "analysis.h"
#include "displayconfig.h"
#include "processing.h"
#include "mhc.h"
#include "gui_mhc.h"
#include <dwmapi.h>
#include <avrt.h>
#include <iostream>
#include <iomanip>
#include <algorithm>

#ifndef STATUS_GRAPHICS_PRESENT_OCCLUDED
#define STATUS_GRAPHICS_PRESENT_OCCLUDED ((DWORD)0xC01E05A1)
#endif

#ifndef WM_DWMCOMPOSITIONCHANGED
#define WM_DWMCOMPOSITIONCHANGED 0x031E
#endif

// ============================================================================
// SECTION: Render Monitor
// ============================================================================

void RenderMonitor(MonitorContext* ctx, FramePacer* fp, bool bufferActive) {
    // Entry validation - skip if monitor is disabled
    if (!ctx || !ctx->enabled) return;

    // If resources are missing (failed reinit), try to recover with non-blocking backoff.
    // No Sleep() — returns immediately so other monitors keep rendering at full frame rate.
    if (!ctx->duplication) {
        if (g_displayOff.load()) {
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return;
        }

        // Non-blocking backoff: skip this monitor until enough time has passed
        auto now = std::chrono::steady_clock::now();
        if (ctx->recoveryBackoffMs > 0) {
            auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - ctx->lastRecoveryAttempt);
            if (elapsed.count() < ctx->recoveryBackoffMs) {
                // Keep watchdog alive during recovery — thread is actively recovering,
                // not stuck. Without this, display-off causes watchdog timeout when
                // GUID_CONSOLE_DISPLAY_STATE notification is delayed or absent.
                g_lastSuccessfulFrame = now;
                return;  // Not time yet — skip, don't block
            }
        }

        ctx->lastRecoveryAttempt = now;
        ctx->consecutiveFailures++;
        ctx->recoveryBackoffMs = (std::min)(50 * (1 << (std::min)(ctx->consecutiveFailures - 1, 7)), 5000);

        if (ctx->consecutiveFailures > MAX_RECOVERY_RETRIES) {
            std::cerr << "Monitor " << ctx->index << " exceeded " << MAX_RECOVERY_RETRIES
                      << " recovery attempts, disabling" << std::endl;
            ctx->enabled = false;
            return;
        }

        if (ctx->consecutiveFailures % 10 == 0) {
            std::cout << "Monitor " << ctx->index << " attempting recovery, attempt "
                      << ctx->consecutiveFailures << "..." << std::endl;
        }

        if (ReinitDesktopDuplication(ctx)) {
            std::cout << "Monitor " << ctx->index << " reinit success" << std::endl;
            ctx->consecutiveFailures = 0;
            ctx->recoveryBackoffMs = 0;
            ctx->lastCaptureTexture = nullptr;  // Invalidate SRV cache after reinit
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            // ReinitDesktopDuplication handles HDR change + swapchain recreation internally.
            // Apply additional mode-switch steps not covered by reinit:
            if (ctx->isHDREnabled != ctx->wasHDREnabled) {
                ApplyMaxTmlSettings();
                ctx->cbDirty = true;
                ReapplyMhcProfilesOnModeSwitch(ctx);
            }
            ctx->wasHDREnabled = ctx->isHDREnabled;
        }
        return;
    }

    if (!ctx->swapchain || !ctx->rtv) return;

    bool useFrameBuffer = bufferActive && ctx->bufferTexture && ctx->bufferRTV;

    // preAcquireQpc: taken just before AcquireNextFrame for clean composition offset
    // measurement. Buffer present has been hoisted to RenderAll (immediately after VBlank
    // wake), so there's no Present() blocking between this QPC and Acquire.
    // Blocking fallback re-takes QPC on success for accurate late-delivery measurement.
    LARGE_INTEGER preAcquireQpc;
    QueryPerformanceCounter(&preAcquireQpc);

    // Acquire next frame from desktop duplication.
    // Two-phase: instant try first (AcquireNextFrame(0)), then short blocking fallback
    // (2-3ms) if the pacer's predicted offset was slightly early. This catches frames
    // where variable DWM composition time shifts DD delivery past the prediction without
    // blocking for a full period. If both fail, desktop is genuinely static and
    // DirectComposition holds the last presented buffer (overlay stays visible).
    DXGI_OUTDUPL_FRAME_INFO frameInfo;
    IDXGIResource* desktopResource = nullptr;

    HRESULT hr = ctx->duplication->AcquireNextFrame(0, &frameInfo, &desktopResource);

    // Blocking fallback: catches frames where DD delivery is slightly later than
    // the pacer's predicted offset (e.g., variable DWM composition time under GPU load).
    // Without this, AcquireNextFrame(0) misses frames that arrive even 0.1ms late,
    // turning a clean 2:2 cadence into irregular 1:3 gaps → visible judder.
    // Direct mode: short timeout (period×0.15, cap 3ms) — blocking delays Present.
    // Buffer mode: longer timeout (period×0.40, cap 10ms) — previous frame already
    // presented at VBlank, we're filling the buffer for next cycle with no visual cost.
    // NOTE: preAcquireQpc is NOT re-taken on blocking success. The old approach (re-take
    // QPC to measure actual delivery time) created a positive feedback loop: inflated
    // offset measurements pushed the EMA up, causing more blocking fallbacks, causing
    // more inflation. At non-standard rates like 47.952Hz this produced ~1 missed frame
    // per 33 cycles (3.6ms jitter settling). The near-miss counter in RecordAcquisition
    // provides gentler upward pressure instead.
    bool blockingFallbackUsed = false;
    if (hr == DXGI_ERROR_WAIT_TIMEOUT && fp && fp->strategy != FramePacerStrategy::DwmFlushOnly) {
        // Buffer mode: previous frame already presented at VBlank, we're filling the buffer
        // for next cycle. Safe to wait longer — up to 40% of frame period (8.3ms at 48Hz).
        // Direct mode: keep short — blocking delays Present, missing DWM's sampling window.
        float fallbackFraction = useFrameBuffer ? 0.40f : 0.15f;
        float fallbackCap = useFrameBuffer ? 10.0f : 3.0f;
        UINT fallbackMs = (UINT)(std::min)(fallbackCap, fp->refreshPeriodMs * fallbackFraction);
        if (fallbackMs > 0) {
            hr = ctx->duplication->AcquireNextFrame(fallbackMs, &frameInfo, &desktopResource);
            if (SUCCEEDED(hr)) {
                blockingFallbackUsed = true;
            }
        }
    }

    if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
        // Truly no new content this cycle (content static or frame rate < display rate).
        if (fp && ctx->index == 0) FramePacerNotifyTimeout(fp);
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        // Still need to handle initial visibility even without new frames
        if (ctx->dcompCommitted && ctx->hwnd && !IsWindowVisible(ctx->hwnd)
            && !g_vrrWhitelistActive.load() && !g_overlayAutoSleep.load()) {
            ctx->framesAfterCommit++;
            if (ctx->framesAfterCommit >= 1) {
                SetLayeredWindowAttributes(ctx->hwnd, 0, 255, LWA_ALPHA);
                ShowWindow(ctx->hwnd, SW_SHOWNA);
            }
        }
        return;
    } else if (hr == DXGI_ERROR_ACCESS_LOST || FAILED(hr)) {
        // Desktop duplication lost — hide overlay, release, set up non-blocking recovery.
        // NO Sleep() here: return immediately so other monitors keep rendering.
        // Recovery will be attempted on next RenderMonitor call via the !ctx->duplication path.
        if (ctx->hwnd && IsWindowVisible(ctx->hwnd)) {
            ShowWindow(ctx->hwnd, SW_HIDE);
        }
        if (ctx->duplication) {
            ctx->duplication->Release();
            ctx->duplication = nullptr;
        }
        ctx->lastCaptureTexture = nullptr;  // Invalidate SRV cache

        if (g_displayOff.load()) {
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            ctx->consecutiveFailures = 0;
            return;
        }

        ctx->consecutiveFailures++;
        ctx->recoveryBackoffMs = (std::min)(50 * (1 << (std::min)(ctx->consecutiveFailures - 1, 7)), 5000);
        ctx->lastRecoveryAttempt = std::chrono::steady_clock::now();

        if (ctx->consecutiveFailures > MAX_RECOVERY_RETRIES) {
            std::cerr << "Monitor " << ctx->index << " exceeded " << MAX_RECOVERY_RETRIES
                      << " recovery attempts after ACCESS_LOST, disabling" << std::endl;
            ctx->enabled = false;
            return;
        }

        if (ctx->consecutiveFailures == 1 || ctx->consecutiveFailures % 10 == 0) {
            std::cout << "Monitor " << ctx->index << " duplication lost (0x" << std::hex << hr << std::dec
                      << "), attempt " << ctx->consecutiveFailures << "..." << std::endl;
        }

        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        return;
    }

    // Skip mouse-only frames (no desktop pixels changed, just cursor position update).
    // Desktop Duplication delivers a frame for every cursor update even when no desktop
    // pixels changed. No additional sync needed — next iteration starts with compositor sync.
    if (frameInfo.LastPresentTime.QuadPart == 0 && frameInfo.AccumulatedFrames == 0) {
        desktopResource->Release();
        ctx->duplication->ReleaseFrame();
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        return;
    }

    // Reset consecutive failures on successful frame acquisition
    ctx->consecutiveFailures = 0;

    // Update frame pacer composition offset EMA.
    // Pass LastPresentTime as the preferred measurement point — it is the exact QPC when DWM
    // finished compositing, eliminating the variable latency between DD availability and our
    // AcquireNextFrame call. Falls back to preAcquireQpc when LastPresentTime is zero
    // (cursor-only updates with no desktop pixel change).
    if (fp && ctx->index == 0)
        FramePacerRecordAcquisition(fp, preAcquireQpc.QuadPart, blockingFallbackUsed,
                                     frameInfo.LastPresentTime.QuadPart);

    // Got a new frame - get the texture
    ID3D11Texture2D* frameTexture = nullptr;
    hr = desktopResource->QueryInterface(IID_PPV_ARGS(&frameTexture));
    desktopResource->Release();

    if (FAILED(hr)) {
        ctx->duplication->ReleaseFrame();
        return;
    }

    // Check if size changed
    D3D11_TEXTURE2D_DESC texDesc;
    frameTexture->GetDesc(&texDesc);

    if ((int)texDesc.Width != ctx->width || (int)texDesc.Height != ctx->height) {
        ctx->width = texDesc.Width;
        ctx->height = texDesc.Height;
        if (!ResizeSwapChain(ctx, ctx->width, ctx->height)) {
            frameTexture->Release();
            ctx->duplication->ReleaseFrame();
            g_forceReinit = true;
            return;
        }

        // Also resize window
        SetWindowPos(ctx->hwnd, nullptr, 0, 0, ctx->width, ctx->height,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
    }

    // Check if capture format changed (Windows HDR toggle can change format without ACCESS_LOST)
    if (texDesc.Format != ctx->captureFormat) {
        std::cout << "Monitor " << ctx->index << " capture format changed, forcing full reinit..." << std::endl;
        frameTexture->Release();
        ctx->duplication->ReleaseFrame();
        if (ctx->duplication) {
            ctx->duplication->Release();
            ctx->duplication = nullptr;
        }
        ctx->lastCaptureTexture = nullptr;  // Invalidate SRV cache
        if (ReinitDesktopDuplication(ctx)) {
            // HDR state changed - update swapchain and settings
            bool hasApplicableLUT = ctx->isHDREnabled
                ? (ctx->lutSRV_HDR != nullptr)
                : (ctx->lutSRV_SDR != nullptr);
            ctx->usePassthrough = !hasApplicableLUT;
            RecreateSwapchain(ctx);
            ApplyMaxTmlSettings();
            // Reapply MHC ICC profiles only if display mode actually changed
            if (ctx->isHDREnabled != ctx->wasHDREnabled) {
                ReapplyMhcProfilesOnModeSwitch(ctx);
            }
            ctx->wasHDREnabled = ctx->isHDREnabled;
            ctx->cbDirty = true;
            std::cout << "Monitor " << ctx->index << " switched to " << (ctx->isHDREnabled ? "HDR" : "SDR") << " mode" << std::endl;
        }
        return;
    }

    // Create SRV for captured frame (reuse if same texture pointer — common case)
    if (frameTexture != ctx->lastCaptureTexture) {
        if (ctx->captureSRV) {
            ctx->captureSRV->Release();
            ctx->captureSRV = nullptr;
        }
        D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
        srvDesc.Format = texDesc.Format;
        srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        srvDesc.Texture2D.MipLevels = 1;
        hr = g_device->CreateShaderResourceView(frameTexture, &srvDesc, &ctx->captureSRV);
        ctx->lastCaptureTexture = frameTexture;  // Weak ref for comparison (not AddRef'd)
    }
    frameTexture->Release();

    if (FAILED(hr)) {
        ctx->duplication->ReleaseFrame();
        return;
    }

    // Check if atomic toggles changed → mark constant buffer dirty
    // Cache values are updated only AFTER successful constant buffer write (below)
    // to ensure retries on Map failure
    bool curGamma = g_desktopGammaMode.load();
    bool curTetrahedral = g_tetrahedralInterp.load();
    if (curGamma != ctx->lastDesktopGamma || curTetrahedral != ctx->lastTetrahedralInterp) {
        ctx->cbDirty = true;
    }

    // Motion bar position changes every frame — force CB update when enabled
    bool motionBar = g_showMotionBar.load();
    if (motionBar) ctx->cbDirty = true;

    // Multi-monitor: always update CB since it's shared across monitors (272-byte Map/Unmap is ~1-5μs)
    if (g_monitors.size() > 1) ctx->cbDirty = true;

    // Update constant buffer only when dirty (avoids Map/Unmap overhead on static frames)
    if (ctx->cbDirty) {
    D3D11_MAPPED_SUBRESOURCE mapped;
    hr = g_context->Map(g_constantBuffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
    if (SUCCEEDED(hr)) {
        float* cbData = (float*)mapped.pData;
        // Row 0: Core settings
        cbData[0] = ctx->isHDREnabled ? 1.0f : 0.0f;
        cbData[1] = g_sdrWhiteNits;
        cbData[2] = ctx->maxDisplayNits;
        cbData[3] = (float)(ctx->isHDREnabled ? ctx->lutSizeHDR : ctx->lutSizeSDR);
        // Row 1: Toggles
        // Suppress shader corrections when MHC ICC profile handles them at GPU scanout
        bool mhcPrim = ctx->isHDREnabled ? ctx->hdrMhcPrimariesActive.load() : ctx->sdrMhcPrimariesActive.load();
        bool mhcGs = ctx->isHDREnabled ? ctx->hdrMhcGrayscaleActive.load() : ctx->sdrMhcGrayscaleActive.load();
        cbData[4] = (ctx->isHDREnabled && g_desktopGammaMode.load() && !mhcGs) ? 1.0f : 0.0f;  // Desktop gamma (HDR-only, MHC bakes into 1D LUT)
        cbData[5] = g_tetrahedralInterp.load() ? 1.0f : 0.0f; // Tetrahedral interpolation
        cbData[6] = ctx->usePassthrough ? 1.0f : 0.0f;  // HDR passthrough (no LUT)
        // Select color correction based on HDR state
        auto& cc = ctx->isHDREnabled ? ctx->hdrColorCorrection : ctx->sdrColorCorrection;
        // Suppress primaries/grayscale/WB when MHC handles them
        // corrGsPreviewActive: correction GS editor open — MHC has base calibration,
        // shader adds correction GS on top. DG stays suppressed (already in MHC LUT).
        bool corrGsPreview = ctx->corrGsPreviewActive.load();
        bool shaderPrimaries = cc.primariesEnabled && !mhcPrim;
        bool shaderGrayscale = cc.grayscale.enabled && (!mhcGs || corrGsPreview);
        bool shaderWhiteBalance = cc.primariesEnabled && !mhcPrim &&
            (cc.whiteBalanceGains[0] != 1.0f || cc.whiteBalanceGains[1] != 1.0f || cc.whiteBalanceGains[2] != 1.0f);
        cbData[7] = (shaderPrimaries || shaderGrayscale || shaderWhiteBalance) ? 1.0f : 0.0f;  // useManualCorrection
        // Row 2: Grayscale control + tonemapping toggles
        cbData[8] = (float)cc.grayscale.pointCount;
        cbData[9] = shaderGrayscale ? 1.0f : 0.0f;
        // Exclude tonemapping when DWM hook handles it (hook applies before DD capture)
        cbData[10] = (ctx->isHDREnabled && cc.tonemap.enabled && !g_dwmHookMode.load()) ? 1.0f : 0.0f;  // tonemapEnabled
        cbData[11] = (float)static_cast<int>(cc.tonemap.curve);  // tonemapCurve
        // Row 3-5: Primaries matrix (xyz) + white balance gains (w)
        // Write identity when MHC handles primaries/WB — useManualCorrection may still be true
        // for grayscale, so the shader would apply non-identity matrix/WB if we wrote them.
        cbData[12] = shaderPrimaries ? cc.primariesMatrix[0] : 1.0f;
        cbData[13] = shaderPrimaries ? cc.primariesMatrix[1] : 0.0f;
        cbData[14] = shaderPrimaries ? cc.primariesMatrix[2] : 0.0f;
        cbData[15] = shaderWhiteBalance ? cc.whiteBalanceGains[0] : 1.0f;
        cbData[16] = shaderPrimaries ? cc.primariesMatrix[3] : 0.0f;
        cbData[17] = shaderPrimaries ? cc.primariesMatrix[4] : 1.0f;
        cbData[18] = shaderPrimaries ? cc.primariesMatrix[5] : 0.0f;
        cbData[19] = shaderWhiteBalance ? cc.whiteBalanceGains[1] : 1.0f;
        cbData[20] = shaderPrimaries ? cc.primariesMatrix[6] : 0.0f;
        cbData[21] = shaderPrimaries ? cc.primariesMatrix[7] : 0.0f;
        cbData[22] = shaderPrimaries ? cc.primariesMatrix[8] : 1.0f;
        cbData[23] = shaderWhiteBalance ? cc.whiteBalanceGains[2] : 1.0f;
        // Row 6: Tonemapping parameters
        // Slot [24]: PQ-encoded source peak (avoids per-pixel pow() in pixel shader)
        if (cc.tonemap.dynamicPeak) {
            // Dynamic: floor ensures detected peak can't drop below a minimum
            // BT.2390/BT.2446A: raised floor guarantees compression headroom on high-nit displays
            //   ratio scales from 1.0× at 400 nits to 1.5× at 4000 nits (smooth highlight rolloff)
            // Other curves: floor at target peak (passthrough when detected ≤ target)
            float floorNits = cc.tonemap.targetPeakNits;
            if (cc.tonemap.curve == TonemapCurve::BT2390 || cc.tonemap.curve == TonemapCurve::BT2446A) {
                float t = (std::clamp)(((std::max)(cc.tonemap.targetPeakNits, 400.0f) - 400.0f) / 3600.0f, 0.0f, 1.0f);
                float ratio = 1.0f + t * 0.5f;
                floorNits = cc.tonemap.targetPeakNits * ratio;
            }
            cbData[24] = LinearToPQScalar(floorNits / 10000.0f);
        } else {
            // Static: PQ of user-specified source peak (fallback 1000 nits)
            float srcPeak = (cc.tonemap.sourcePeakNits > 0.0f) ? cc.tonemap.sourcePeakNits : 1000.0f;
            cbData[24] = LinearToPQScalar(srcPeak / 10000.0f);
        }
        cbData[25] = cc.tonemap.targetPeakNits;
        cbData[26] = cc.tonemap.dynamicPeak ? 1.0f : 0.0f;  // tonemapDynamic
        cbData[27] = (cc.grayscale.use24Gamma && !mhcGs) ? 1.0f : 0.0f;  // grayscale24 (MHC bakes into 1D LUT)
        // Row 7: Grayscale peak + ACM flag
        cbData[28] = cc.grayscale.peakNits;  // grayscalePeakNits (HDR only)
        cbData[29] = ctx->isFP16SDR ? 1.0f : 0.0f;  // ACM: FP16 SDR, input is linear scRGB
        cbData[30] = LinearToPQScalar(cc.tonemap.targetPeakNits / 10000.0f);  // pqTargetPeak (precomputed)
        cbData[31] = LinearToPQScalar((std::max)(cc.grayscale.peakNits, 1.0f) / 10000.0f);  // pqGrayscalePeak (precomputed)
        // Rows 8-15: Red channel grayscale (32 floats)
        // Rows 16-23: Green channel grayscale (32 floats)
        // Rows 24-31: Blue channel grayscale (32 floats)
        // In ICtCp mode: R=deltaI, G=deltaCt, B=deltaCp (additive offsets)
        // In PQ mode: R/G/B = normalized PQ corrections (per-channel gains)
        bool useICtCp = ctx->isHDREnabled && cc.grayscale.enabled &&
                        (cc.tonemap.enabled || ctx->grayscaleICtCp);
        if (ctx->isHDREnabled && cc.grayscale.enabled) {
            if (useICtCp) {
                // ICtCp mode: send precomputed delta offsets
                // Compute if not yet valid (data changed since last compute)
                auto& gsRef = cc.grayscale;
                if (!gsRef.ictcpValid) ComputeGrayscaleICtCpOffsets(gsRef);
                for (int i = 0; i < 32; i++) {
                    if (i < cc.grayscale.pointCount) {
                        cbData[32 + i] = cc.grayscale.ictcpI[i];   // delta I
                        cbData[64 + i] = cc.grayscale.ictcpCt[i];  // delta Ct
                        cbData[96 + i] = cc.grayscale.ictcpCp[i];  // delta Cp
                    } else {
                        cbData[32 + i] = 0.0f;  // identity: zero delta
                        cbData[64 + i] = 0.0f;
                        cbData[96 + i] = 0.0f;
                    }
                }
            } else {
                // PQ mode: send normalized per-channel PQ corrections
                for (int i = 0; i < 32; i++) {
                    float t = (cc.grayscale.pointCount > 1) ? (float)i / (float)(cc.grayscale.pointCount - 1) : 0.0f;
                    auto safeVal = [&](float v) -> float { return (i > 0 && v == 0.0f) ? t : v; };
                    if (i < cc.grayscale.pointCount) {
                        cbData[32 + i] = safeVal(cc.grayscale.pointsR[i]);
                        cbData[64 + i] = safeVal(cc.grayscale.pointsG[i]);
                        cbData[96 + i] = safeVal(cc.grayscale.pointsB[i]);
                    } else {
                        cbData[32 + i] = t;
                        cbData[64 + i] = t;
                        cbData[96 + i] = t;
                    }
                }
            }
        } else {
            // SDR or grayscale disabled: write per-channel values directly
            // Safety: if pointsR/G/B are uninitialized (all zero), use identity ramp
            for (int i = 0; i < 32; i++) {
                float t = (float)i / (float)(cc.grayscale.pointCount > 1 ? cc.grayscale.pointCount - 1 : 31);
                float identity = t * t;  // SDR sqrt distribution identity
                auto safeVal = [&](float v) -> float { return (i > 0 && v == 0.0f) ? identity : v; };
                if (i < cc.grayscale.pointCount) {
                    cbData[32 + i] = safeVal(cc.grayscale.pointsR[i]);
                    cbData[64 + i] = safeVal(cc.grayscale.pointsG[i]);
                    cbData[96 + i] = safeVal(cc.grayscale.pointsB[i]);
                } else {
                    cbData[32 + i] = identity;
                    cbData[64 + i] = identity;
                    cbData[96 + i] = identity;
                }
            }
        }
        // Row 32: Motion bar (UFO test-style judder detection)
        if (motionBar) {
            if (!s_motionBarOriginSet) {
                s_motionBarOrigin = std::chrono::steady_clock::now();
                s_motionBarOriginSet = true;
            }
            float elapsed = std::chrono::duration<float>(
                std::chrono::steady_clock::now() - s_motionBarOrigin).count();
            cbData[128] = 1.0f;  // motionBarEnabled
            cbData[129] = fmodf(elapsed * 0.5f, 1.0f);  // position: 0.5 traversals/sec
        } else {
            cbData[128] = 0.0f;
            cbData[129] = 0.0f;
        }
        cbData[130] = useICtCp ? 1.0f : 0.0f;  // grayscaleICtCp
        cbData[131] = 0.0f;  // reserved
        g_context->Unmap(g_constantBuffer, 0);

        // Only clear dirty flag and update cached atomics AFTER successful write.
        // If Map failed, cbDirty stays true so we retry next frame.
        ctx->cbDirty = false;
        ctx->lastDesktopGamma = curGamma;
        ctx->lastTetrahedralInterp = curTetrahedral;
    }
    } // end if cbDirty

    // shaderCorrActive is evaluated in RenderAll() before the auto-sleep check

    // Select the appropriate LUT based on HDR mode (no fallback - SDR/HDR LUTs are incompatible)
    // If no applicable LUT, usePassthrough is true and shader skips LUT sampling
    ID3D11ShaderResourceView* activeLUT = ctx->isHDREnabled ? ctx->lutSRV_HDR : ctx->lutSRV_SDR;

    // Run peak detection compute shader if dynamic tonemapping enabled
    const auto& cc = ctx->isHDREnabled ? ctx->hdrColorCorrection : ctx->sdrColorCorrection;
    if (ctx->isHDREnabled && cc.tonemap.enabled && cc.tonemap.dynamicPeak &&
        g_peakDetectCS && g_peakCB && ctx->captureSRV) {
        // Create peak resources on first use
        if (!ctx->peakTexture) {
            CreatePeakDetectionResources(ctx);
        }

        if (ctx->peakTexture && ctx->peakUAV) {
            // Update peak constant buffer only when dimensions change (static values stay valid)
            if (ctx->width != ctx->lastPeakCBWidth || ctx->height != ctx->lastPeakCBHeight) {
                D3D11_MAPPED_SUBRESOURCE mapped;
                if (SUCCEEDED(g_context->Map(g_peakCB, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
                    // frameWidth/frameHeight are uint in shader, must write as uint
                    uint32_t* udata = (uint32_t*)mapped.pData;
                    udata[0] = (uint32_t)ctx->width;
                    udata[1] = (uint32_t)ctx->height;
                    float* fdata = (float*)mapped.pData;
                    fdata[2] = 0.3f;    // riseRate - exponential rise (0.3 = 30% per frame)
                    fdata[3] = 0.05f;   // fallRate - exponential fall (0.05 = 5% per frame)
                    fdata[4] = 100.0f;  // maxRisePerFrame - slew limit (nits/frame)
                    fdata[5] = 50.0f;   // maxFallPerFrame - slew limit (nits/frame)
                    fdata[6] = 0.0f;    // padding
                    fdata[7] = 0.0f;    // padding
                    g_context->Unmap(g_peakCB, 0);
                    ctx->lastPeakCBWidth = ctx->width;
                    ctx->lastPeakCBHeight = ctx->height;
                }
            }

            // Dispatch compute shader
            g_context->CSSetShader(g_peakDetectCS, nullptr, 0);
            g_context->CSSetConstantBuffers(0, 1, &g_peakCB);
            g_context->CSSetShaderResources(0, 1, &ctx->captureSRV);
            g_context->CSSetUnorderedAccessViews(0, 1, &ctx->peakUAV, nullptr);
            g_context->Dispatch(1, 1, 1);

            // Unbind UAV to allow SRV binding
            ID3D11UnorderedAccessView* nullUAV = nullptr;
            g_context->CSSetUnorderedAccessViews(0, 1, &nullUAV, nullptr);
            ID3D11ShaderResourceView* nullSRV = nullptr;
            g_context->CSSetShaderResources(0, 1, &nullSRV);

            // Double-buffered peak readback: copy to staging[N], read staging[N-1] from previous frame.
            // This avoids GPU pipeline stalls by reading data that's guaranteed to be ready.
            bool needPeakReadback = g_analysisEnabled.load() || g_logPeakDetection.load();
            if (needPeakReadback) {
                static std::chrono::steady_clock::time_point lastReadback[8] = {};
                auto now = std::chrono::steady_clock::now();
                int idx = ctx->index < 8 ? ctx->index : 0;

                if (std::chrono::duration_cast<std::chrono::milliseconds>(now - lastReadback[idx]).count() >= 500) {
                    // Create both staging textures on first use
                    auto createStaging = [&](ID3D11Texture2D** tex) {
                        if (*tex) return;
                        D3D11_TEXTURE2D_DESC stagingDesc = {};
                        stagingDesc.Width = 1;
                        stagingDesc.Height = 1;
                        stagingDesc.MipLevels = 1;
                        stagingDesc.ArraySize = 1;
                        stagingDesc.Format = DXGI_FORMAT_R32_FLOAT;
                        stagingDesc.SampleDesc.Count = 1;
                        stagingDesc.Usage = D3D11_USAGE_STAGING;
                        stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                        HRESULT hr = g_device->CreateTexture2D(&stagingDesc, nullptr, tex);
                        if (FAILED(hr)) {
                            std::cerr << "Monitor " << ctx->index
                                      << " peak staging texture failed: 0x"
                                      << std::hex << hr << std::dec << std::endl;
                        }
                    };
                    createStaging(&ctx->peakStagingTexture);
                    createStaging(&ctx->peakStagingTexture2);

                    if (ctx->peakStagingTexture && ctx->peakStagingTexture2) {
                        // Write: copy GPU result to current staging buffer
                        ID3D11Texture2D* writeTarget = (ctx->peakStagingReadIndex == 0)
                            ? ctx->peakStagingTexture : ctx->peakStagingTexture2;
                        g_context->CopyResource(writeTarget, ctx->peakTexture);

                        // Read: map the OTHER staging buffer (from previous readback cycle)
                        ID3D11Texture2D* readSource = (ctx->peakStagingReadIndex == 0)
                            ? ctx->peakStagingTexture2 : ctx->peakStagingTexture;
                        D3D11_MAPPED_SUBRESOURCE mapped;
                        if (SUCCEEDED(g_context->Map(readSource, 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &mapped))) {
                            float pqValue = *((float*)mapped.pData);
                            g_context->Unmap(readSource, 0);
                            // Convert PQ-encoded peak back to nits for analysis display
                            ctx->detectedPeakNits = PQToLinearScalar(pqValue) * 10000.0f;
                            if (g_logPeakDetection.load()) {
                                std::cout << "Monitor " << ctx->index << " detected peak: "
                                          << std::fixed << std::setprecision(1) << ctx->detectedPeakNits << " nits" << std::endl;
                            }
                        }
                        // Alternate for next cycle
                        ctx->peakStagingReadIndex = 1 - ctx->peakStagingReadIndex;
                    }
                    lastReadback[idx] = now;
                }
            }
        }
    }

    // Render — choose render target based on buffer mode
    // Buffer mode: render to intermediate texture (presented next cycle for consistent timing)
    // Normal mode: render directly to swapchain backbuffer
    ID3D11RenderTargetView* renderTarget = (useFrameBuffer) ? ctx->bufferRTV : ctx->rtv;

    float clearColor[4] = { 0, 0, 0, 0 };
    g_context->ClearRenderTargetView(renderTarget, clearColor);

    D3D11_VIEWPORT vp = { 0, 0, (float)ctx->width, (float)ctx->height, 0, 1 };
    g_context->RSSetViewports(1, &vp);
    g_context->OMSetRenderTargets(1, &renderTarget, nullptr);

    g_context->VSSetShader(g_vs, nullptr, 0);
    g_context->PSSetShader(g_ps, nullptr, 0);
    g_context->PSSetConstantBuffers(0, 1, &g_constantBuffer);
    g_context->PSSetShaderResources(0, 1, &ctx->captureSRV);
    g_context->PSSetShaderResources(1, 1, &activeLUT);
    g_context->PSSetShaderResources(2, 1, &g_blueNoiseSRV);
    // Bind peak texture for dynamic tonemapping (t3)
    if (ctx->peakSRV) {
        g_context->PSSetShaderResources(3, 1, &ctx->peakSRV);
    }
    // Bind precomputed transfer function LUTs (t4-t6)
    g_context->PSSetShaderResources(4, 1, &g_desktopGammaSRV);   // sRGB->2.2 correction
    g_context->PSSetShaderResources(5, 1, &g_pqOetfSRV);         // PQ OETF (Linear->PQ)
    g_context->PSSetShaderResources(6, 1, &g_pqEotfSRV);         // PQ EOTF (PQ->Linear)
    g_context->PSSetShaderResources(7, 1, &g_srgbOetfSRV);       // sRGB OETF (Linear->sRGB)
    g_context->PSSetShaderResources(8, 1, &g_srgbEotfSRV);       // sRGB EOTF (sRGB->Linear)
    g_context->PSSetShaderResources(9, 1, &g_gammaRatioSRV);     // pow(Y, 1/11) for 2.4 gamma
    g_context->PSSetShaderResources(10, 1, &g_wbGammaSRV);       // pow(gain, 1/2.2) for WB

    ID3D11SamplerState* samplers[] = { g_samplerPoint, g_samplerLinear, g_samplerWrap };
    g_context->PSSetSamplers(0, 3, samplers);

    g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    g_context->Draw(3, 0);

    // Analysis overlay (primary monitor only) — always renders to backbuffer
    if (ctx->index == 0 && g_analysisEnabled.load()) {
        // If buffer mode, switch back to backbuffer for analysis (it's an overlay on the presented frame)
        if (useFrameBuffer) {
            g_context->OMSetRenderTargets(1, &ctx->rtv, nullptr);
        }
        DispatchAnalysisCompute(ctx);
        UpdateAnalysisDisplay(ctx);
    }

    if (useFrameBuffer) {
        // Buffer mode: mark buffer ready for next cycle's present.
        // No Present here — it was already done at the top of this function.
        ctx->bufferReady = true;
    }

    // Normal mode: present immediately — compositor sync at start of RenderAll handles frame pacing
    UINT presentFlags = g_tearingSupported ? DXGI_PRESENT_ALLOW_TEARING : 0;
    HRESULT presentHr = S_OK;
    if (!useFrameBuffer) {
        presentHr = ctx->swapchain->Present(0, presentFlags);
    }

    if (presentHr == DXGI_ERROR_DEVICE_REMOVED || presentHr == DXGI_ERROR_DEVICE_RESET) {
        std::cerr << "Monitor " << ctx->index << " device lost during Present: 0x"
                  << std::hex << presentHr << std::dec << std::endl;
        if (g_device) {
            HRESULT reason = g_device->GetDeviceRemovedReason();
            std::cerr << "  Device removed reason: 0x" << std::hex << reason << std::dec << std::endl;
        }
        // Device is shared — hide all overlays and stop (one removal means all are dead)
        for (auto& m : g_monitors) {
            if (m.hwnd) ShowWindow(m.hwnd, SW_HIDE);
        }
        g_running = false;
    } else {
        // Successful frame - update watchdog timestamp
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        s_watchdogRecoveryAttempts = 0;

        // Track frame timing only when analysis overlay is active (avoid unnecessary work)
        if (g_analysisEnabled.load()) {
            if (ctx->lastFrameTime.time_since_epoch().count() > 0) {
                auto now = std::chrono::steady_clock::now();
                float frameMs = std::chrono::duration<float, std::milli>(now - ctx->lastFrameTime).count();
                ctx->lastFrameTime = now;

                ctx->frameTimeHistory[ctx->frameTimeIndex] = frameMs;
                ctx->frameTimeIndex = (ctx->frameTimeIndex + 1) % 64;
                if (ctx->frameTimeCount < 64) ctx->frameTimeCount++;
            } else {
                ctx->lastFrameTime = std::chrono::steady_clock::now();
            }
            // Feed frame pacer stats into FrameTimingStats for analysis overlay
            if (fp) {
                // Reset pacer diagnostics on analysis overlay toggle
                if (g_resetPacerStats.exchange(false)) {
                    fp->droppedFrameCount = 0;
                    fp->sleepOvershootEma = 0.5f;
                    fp->syncJitterMs = 0.0f;
                    fp->jitterCount = 0;
                    fp->jitterIndex = 0;
                    fp->consecutiveAcquireTimeouts = 0;
                }
                ctx->frameTimingStats.pacerStrategy = fp->strategy;
                ctx->frameTimingStats.syncJitterMs = fp->syncJitterMs;
                ctx->frameTimingStats.compositionOffsetMs =
                    (fp->cadenceLockState == CadenceLockState::Locked)
                    ? fp->lockedOffset : fp->compositionOffsetMs;
                ctx->frameTimingStats.spinWaitMs = fp->lastSpinWaitMs;
                ctx->frameTimingStats.sleepOvershootMs = fp->sleepOvershootEma;
                ctx->frameTimingStats.droppedFrameCount = fp->droppedFrameCount;
                ctx->frameTimingStats.cadenceLocked =
                    (fp->cadenceLockState == CadenceLockState::Locked);
                ctx->frameTimingStats.bufferJitterMs = ctx->bufferJitterEma;
            }
        } else {
            // Reset so we get a fresh baseline when analysis is toggled on
            ctx->lastFrameTime = {};
            ctx->frameTimeCount = 0;
        }

        // Two-phase visibility: first commit DirectComposition, then show window on next frame
        // This prevents black flash by ensuring DirectComposition has processed the visual
        if (!ctx->dcompCommitted && g_dcompDevice) {
            // First successful frame: commit DirectComposition but don't show yet
            g_context->Flush();
            g_dcompDevice->Commit();
            ctx->dcompCommitted = true;
            ctx->framesAfterCommit = 0;  // Start counting frames after commit
        } else if (ctx->dcompCommitted && ctx->hwnd && !IsWindowVisible(ctx->hwnd)
                   && !g_vrrWhitelistActive.load() && !g_overlayAutoSleep.load()) {
            // Wait one frame after commit for DirectComposition to process, then show
            // Skip if VRR whitelist or auto-sleep is hiding overlays
            ctx->framesAfterCommit++;
            if (ctx->framesAfterCommit >= 1) {
                SetLayeredWindowAttributes(ctx->hwnd, 0, 255, LWA_ALPHA);
                ShowWindow(ctx->hwnd, SW_SHOWNA);
            }
        }
    }

    // Release DD frame after rendering — SRV references DD's texture directly (not a copy),
    // so the frame must be held until all GPU draw commands are queued.
    ctx->duplication->ReleaseFrame();
}

// ============================================================================
// SECTION: Render Loop
// ============================================================================

void RenderAll(FramePacer* fp) {
    int activeCount = 0;

    // Device health is checked reactively when Present() or AcquireNextFrame() returns
    // DXGI_ERROR_DEVICE_REMOVED — no need for periodic proactive polling.
    // The watchdog below catches any case where rendering is silently stuck.

    // Watchdog: if no successful frame for N seconds, attempt recovery before exit.
    // Skip watchdog during display sleep — the display-off flag is set by the GUI
    // thread (always responsive) so this is reliable even when CompClock blocks.
    if (g_displayOff.load(std::memory_order_relaxed)) {
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
    } else {
        auto timeSinceLastFrame = std::chrono::steady_clock::now() - g_lastSuccessfulFrame.load();
        if (timeSinceLastFrame > std::chrono::seconds(WATCHDOG_TIMEOUT_SECONDS)) {
            if (s_watchdogRecoveryAttempts < MAX_WATCHDOG_RECOVERY_ATTEMPTS) {
                std::cerr << "Watchdog timeout (attempt " << (s_watchdogRecoveryAttempts + 1)
                          << "/" << MAX_WATCHDOG_RECOVERY_ATTEMPTS
                          << "), attempting recovery..." << std::endl;
                MessageBeep(MB_ICONWARNING);

                // Drop MMCSS priority during heavy recovery work
                if (fp && fp->mmcssHandle) {
                    AvRevertMmThreadCharacteristics(fp->mmcssHandle);
                    fp->mmcssHandle = nullptr;
                }

                // Prevent re-triggering during recovery sleep
                g_lastSuccessfulFrame = std::chrono::steady_clock::now();

                if (AttemptDeviceRecovery()) {
                    std::cout << "Watchdog recovery succeeded, resetting state..." << std::endl;
                    if (fp) {
                        ResetFramePacerState(fp, "watchdog recovery");
                    }
                    for (auto& ctx : g_monitors) {
                        ctx.enabled = true;
                        ctx.consecutiveFailures = 0;
                        ctx.recoveryBackoffMs = 0;
                        ctx.cbDirty = true;
                        ctx.dcompCommitted = false;
                        ctx.lastCaptureTexture = nullptr;
                    }
                    g_forceTopmostReassert.store(true);
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                } else {
                    std::cerr << "Watchdog recovery failed" << std::endl;
                    // Reset watchdog timer for next attempt
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                }

                // Re-acquire MMCSS after recovery
                if (fp && fp->strategy != FramePacerStrategy::DwmFlushOnly) {
                    fp->mmcssTaskIndex = 0;
                    fp->mmcssHandle = AvSetMmThreadCharacteristicsW(L"Pro Audio", &fp->mmcssTaskIndex);
                    if (fp->mmcssHandle) {
                        AvSetMmThreadPriority(fp->mmcssHandle, AVRT_PRIORITY_CRITICAL);
                    }
                }

                s_watchdogRecoveryAttempts++;
                return;  // Skip rendering this cycle
            }

            // Recovery attempts exhausted — exit
            std::cerr << "Watchdog timeout: recovery attempts exhausted, exiting" << std::endl;
            MessageBeep(MB_ICONERROR);
            for (auto& ctx : g_monitors) {
                if (ctx.hwnd) ShowWindow(ctx.hwnd, SW_HIDE);
                ctx.enabled = false;
            }
            g_running = false;
            return;
        }
    }

    // When auto-sleeping, skip rendering entirely — wait for wake event.
    // Must happen before compositor sync: WaitForCompositorClock keeps VBlank
    // interrupts active, wasting power when nobody needs frames.
    if (g_overlayAutoSleep.load()) {
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        if (g_overlayWakeEvent) {
            WaitForSingleObject(g_overlayWakeEvent, 500);
        } else {
            Sleep(50);
        }
        // Fall through if reinit, pending corrections, or analysis state change
        // need processing — all can change whether the overlay needs to render.
        if (!g_forceReinit.load() && !g_hasPendingColorCorrections.load()
            && !g_analysisEnabled.load()) {
            return;
        }
    }

    // Check for forced reinit (e.g., resume from sleep)
    if (g_forceReinit.exchange(false)) {
        // Reset frame pacer EMA and cadence lock (force re-convergence after wake)
        if (fp) {
            ResetFramePacerState(fp, "forced reinit");
        }
        // Temporarily drop MMCSS priority during heavy reinit work
        // (500ms Sleep + system API calls would exceed Pro Audio time quota)
        if (fp && fp->mmcssHandle) {
            AvRevertMmThreadCharacteristics(fp->mmcssHandle);
            fp->mmcssHandle = nullptr;
        }
        std::cout << "Forcing reinit of all monitors..." << std::endl;
        // Give system time to stabilize after wake
        Sleep(500);
        // Check overlay window validity — if destroyed externally, trigger restart
        for (auto& ctx : g_monitors) {
            if (ctx.hwnd && !IsWindow(ctx.hwnd)) {
                std::cerr << "Monitor " << ctx.index
                          << " overlay window destroyed externally, triggering restart" << std::endl;
                g_running = false;
                return;
            }
        }
        // Release all duplication interfaces to force reinit
        for (auto& ctx : g_monitors) {
            if (ctx.duplication) {
                ctx.duplication->Release();
                ctx.duplication = nullptr;
            }
            ctx.enabled = true;           // Re-enable monitors disabled by MAX_RECOVERY_RETRIES
            ctx.consecutiveFailures = 0;  // Reset backoff
            ctx.recoveryBackoffMs = 0;    // Reset non-blocking backoff
            ctx.cbDirty = true;           // Force constant buffer refresh
            ctx.lastCaptureTexture = nullptr;  // Invalidate SRV cache
        }
        // Reset watchdog to avoid timeout during recovery
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        // Reapply MaxTML settings (may be lost after sleep/wake)
        ApplyMaxTmlSettings();
        // Reapply MHC profiles only when specifically requested (sleep/wake, TDR)
        // — not for every reinit source (ImmersiveColorSet, device tree, DWM composition)
        if (g_forceMhcReapply.exchange(false)) {
            ReapplyAllMhcProfiles();
        }
        // Force TOPMOST reassert after wake (z-order most likely disrupted)
        g_forceTopmostReassert.store(true);
        // Re-acquire MMCSS after reinit
        if (fp && fp->strategy != FramePacerStrategy::DwmFlushOnly) {
            fp->mmcssTaskIndex = 0;
            fp->mmcssHandle = AvSetMmThreadCharacteristicsW(L"Pro Audio", &fp->mmcssTaskIndex);
            if (fp->mmcssHandle) {
                AvSetMmThreadPriority(fp->mmcssHandle, AVRT_PRIORITY_CRITICAL);
            }
        }
    }

    // Auto frame buffer: compute idle state BEFORE sync (GetLastInputInfo + GetTickCount
    // are ~0 cost reads from shared memory, no syscall overhead). Must be known before
    // buffer present pre-pass that fires immediately after VBlank wake.
    static bool s_frameBufferActive = false;
    bool frameBufferActive = false;
    if (g_frameBufferEnabled.load(std::memory_order_relaxed)) {
        LASTINPUTINFO lii = { sizeof(LASTINPUTINFO) };
        if (GetLastInputInfo(&lii)) {
            DWORD idleMs = GetTickCount() - lii.dwTime;
            int idleThreshold = (std::max)(0, g_frameBufferIdleMs.load(std::memory_order_relaxed));
            frameBufferActive = (idleThreshold == 0) || (idleMs >= (DWORD)idleThreshold);
            if (fp) fp->lastIdleMs = idleMs;
        }
    }
    // Reset buffer state on transitions
    if (frameBufferActive != s_frameBufferActive) {
        s_frameBufferActive = frameBufferActive;
        for (auto& ctx : g_monitors) {
            ctx.bufferReady = false;
            ctx.bufferJitterEma = 0.0f;
            ctx.lastBufferPresentQpc = {};
        }
    }

    // Communicate buffer state to frame pacer (for lock threshold selection)
    fp->bufferActive = frameBufferActive;

    // ── Frame sync ──
    // Buffer mode: split-phase — VBlank sync → buffer present → DD prediction wait.
    // Buffer present at VBlank + ~0.05ms ensures DWM picks it up for this composition
    // cycle instead of next (fixes 2:2 cadence breaks at 24fps@48Hz).
    // Non-buffer mode: combined VBlank sync + prediction (original behavior).
    if (frameBufferActive) {
        if (!FramePacerSyncToVBlank(fp, g_overlayWakeEvent)) {
            return;
        }

        // ── Buffer present pre-pass: IMMEDIATELY after VBlank wake ──
        // Present previously rendered buffer for all monitors before prediction wait.
        // This is the tightest possible wake-to-present path: only an if + loop + GPU submit.
        for (auto& ctx : g_monitors) {
            if (!ctx.enabled || !ctx.bufferReady || !ctx.swapchain || !ctx.bufferTexture)
                continue;

            ID3D11Texture2D* backBuffer = nullptr;
            HRESULT bufHr = ctx.swapchain->GetBuffer(0, IID_PPV_ARGS(&backBuffer));
            if (SUCCEEDED(bufHr) && backBuffer) {
                g_context->CopyResource(backBuffer, ctx.bufferTexture);
                backBuffer->Release();

                UINT presentFlags = g_tearingSupported ? DXGI_PRESENT_ALLOW_TEARING : 0;
                HRESULT presentHr = ctx.swapchain->Present(0, presentFlags);
                if (presentHr == DXGI_ERROR_DEVICE_REMOVED || presentHr == DXGI_ERROR_DEVICE_RESET) {
                    std::cerr << "Monitor " << ctx.index << " device lost during buffer Present: 0x"
                              << std::hex << presentHr << std::dec << std::endl;
                    for (auto& m : g_monitors) {
                        if (m.hwnd) ShowWindow(m.hwnd, SW_HIDE);
                    }
                    g_running = false;
                    return;
                }

                // Measure buffer present-to-present jitter (EMA of absolute deviation from expected period).
                if (g_analysisEnabled.load()) {
                    LARGE_INTEGER now;
                    QueryPerformanceCounter(&now);
                    if (ctx.lastBufferPresentQpc.QuadPart > 0 && fp) {
                        float intervalMs = (float)(now.QuadPart - ctx.lastBufferPresentQpc.QuadPart)
                                         / (float)fp->qpcFrequency * 1000.0f;
                        float deviation = fabsf(intervalMs - fp->refreshPeriodMs);
                        ctx.bufferJitterEma = ctx.bufferJitterEma * 0.875f + deviation * 0.125f;
                    }
                    ctx.lastBufferPresentQpc = now;
                }
            }
        }

        // Phase 2: prediction wait for DD readiness
        FramePacerWaitForDDReady(fp);
    } else {
        if (!FramePacerWaitForNextFrame(fp, g_overlayWakeEvent)) {
            return;
        }
    }

    // ── Housekeeping: timing no longer critical past this point ──

    // TOPMOST reassert: signal helper thread instead of blocking render loop.
    // The helper thread handles both on-demand (event-driven) and periodic (30s) reasserts.
    // exchange(false) prevents re-signaling from the same WM_WINDOWPOSCHANGING event.
    if (g_forceTopmostReassert.exchange(false, std::memory_order_relaxed)) {
        if (g_topmostEvent) SetEvent(g_topmostEvent);
    }

    // Gamma whitelist is now checked on a separate thread (see GammaWhitelistThreadFunc)
    // The render loop just reads the atomic g_gammaWhitelistActive flag via constant buffer

    // Consume visibility requests from whitelist thread (atomic flags → UI calls on render thread)
    for (auto& ctx : g_monitors) {
        int vis = ctx.requestedVisibility.exchange(0, std::memory_order_relaxed);
        if (vis == 1 && ctx.hwnd && ctx.enabled && ctx.dcompCommitted) {
            SetLayeredWindowAttributes(ctx.hwnd, 0, 255, LWA_ALPHA);
            ShowWindow(ctx.hwnd, SW_SHOWNA);
        } else if (vis == -1 && ctx.hwnd) {
            ShowWindow(ctx.hwnd, SW_HIDE);
        }
    }

    // Apply any pending color correction updates (fast path: skip mutex if no updates)
    // Swap-under-lock pattern: grab pending updates quickly, process without holding lock
    if (g_hasPendingColorCorrections.load(std::memory_order_acquire)) {
        std::vector<PendingColorCorrection> localUpdates;
        {
            std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
            localUpdates.swap(g_pendingColorCorrections);
            g_hasPendingColorCorrections.store(false, std::memory_order_release);
        }
        for (const auto& update : localUpdates) {
            bool matched = false;
            for (auto& ctx : g_monitors) {
                if (ctx.index == update.monitorIndex) {
                    if (update.isHDR) {
                        ctx.hdrColorCorrection = update.data;
                        ctx.hdrColorCorrection.grayscale.ictcpValid = false;  // Force recompute
                    } else {
                        ctx.sdrColorCorrection = update.data;
                    }
                    ctx.grayscaleICtCp = update.ictcpMode;
                    if (update.clearMhcFlags) {
                        ctx.sdrMhcPrimariesActive = false;
                        ctx.sdrMhcGrayscaleActive = false;
                        ctx.hdrMhcPrimariesActive = false;
                        ctx.hdrMhcGrayscaleActive = false;
                    }
                    ctx.cbDirty = true;
                    std::cout << "[Render] Applied CC: mon=" << ctx.index
                              << " isHDR=" << update.isHDR
                              << " mhcPrim=" << (ctx.isHDREnabled ? ctx.hdrMhcPrimariesActive : ctx.sdrMhcPrimariesActive)
                              << " primEn=" << update.data.primariesEnabled
                              << " gsEn=" << update.data.grayscale.enabled
                              << " gsPts=" << update.data.grayscale.pointCount
                              << " clearMhc=" << update.clearMhcFlags
                              << " ictcpMode=" << update.ictcpMode
                              << std::endl;
                    matched = true;
                    break;
                }
            }
            if (!matched) {
                std::cout << "[Render] DROPPED CC: mon=" << update.monitorIndex
                          << " (no matching ctx, g_monitors.size=" << g_monitors.size() << ")" << std::endl;
            }
        }
    }

    // Evaluate shaderCorrActive for all monitors BEFORE auto-sleep decision.
    // Must run here (not inside RenderMonitor) so auto-sleep sees fresh state
    // after pending corrections are applied above.
    for (auto& ctx : g_monitors) {
        if (!ctx.enabled) continue;
        bool mhcP = ctx.isHDREnabled ? ctx.hdrMhcPrimariesActive.load() : ctx.sdrMhcPrimariesActive.load();
        bool mhcG = ctx.isHDREnabled ? ctx.hdrMhcGrayscaleActive.load() : ctx.sdrMhcGrayscaleActive.load();
        const auto& cc2 = ctx.isHDREnabled ? ctx.hdrColorCorrection : ctx.sdrColorCorrection;
        bool hasPrim = cc2.primariesEnabled && !mhcP;
        bool hasGs = cc2.grayscale.enabled && !mhcG;
        bool hasWB = cc2.primariesEnabled && !mhcP &&
            (cc2.whiteBalanceGains[0] != 1.0f || cc2.whiteBalanceGains[1] != 1.0f || cc2.whiteBalanceGains[2] != 1.0f);
        bool hasTonemap = ctx.isHDREnabled && cc2.tonemap.enabled && !g_dwmHookMode.load();
        bool hasDG = ctx.isHDREnabled && g_desktopGammaMode.load() && !mhcG;  // DG is HDR-only
        bool has24 = cc2.grayscale.use24Gamma && !mhcG;
        bool hasAnalysis = (ctx.index == 0) && g_analysisEnabled.load();  // analysis runs on primary only
        bool hasMhcPreview = g_mhcEditDialogOpen.load();                  // MHC/grayscale editor live preview
        ctx.shaderCorrActive = hasPrim || hasGs || hasWB || hasTonemap || hasDG || has24
                               || hasAnalysis || hasMhcPreview;
    }

    // Auto-sleep: hide overlay when no monitor needs processing (e.g., only MHC ICC active)
    {
        bool anyMonitorNeedsOverlay = false;
        for (auto& ctx : g_monitors) {
            if (!ctx.enabled) continue;
            if (!ctx.usePassthrough || ctx.shaderCorrActive) {
                anyMonitorNeedsOverlay = true;
                break;
            }
        }

        if (!anyMonitorNeedsOverlay && !g_overlayAutoSleep.load()) {
            g_overlayAutoSleep.store(true);
            for (auto& ctx : g_monitors) {
                if (ctx.hwnd) ShowWindow(ctx.hwnd, SW_HIDE);
            }
            std::cout << "Auto-sleep: overlay has nothing to do, hiding" << std::endl;
        } else if (anyMonitorNeedsOverlay && g_overlayAutoSleep.load()) {
            g_overlayAutoSleep.store(false);
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            // Reset frame pacer EMA and cadence lock on wake (force re-convergence)
            if (fp) {
                ResetFramePacerState(fp, "auto-sleep wake");
            }
            // Don't force-show here — RenderMonitor's two-phase visibility handles it
            // Just mark dcompCommitted = false so windows go through proper show sequence
            for (auto& ctx : g_monitors) {
                if (ctx.hwnd && ctx.enabled) {
                    ctx.dcompCommitted = false;
                    ctx.framesAfterCommit = 0;
                }
            }
            std::cout << "Auto-sleep: overlay needed again, waking" << std::endl;
        }

        // Update shader-active state from current frame's evaluation
        if (anyMonitorNeedsOverlay != g_shaderCorrectionsActive.load(std::memory_order_relaxed)) {
            g_shaderCorrectionsActive.store(anyMonitorNeedsOverlay, std::memory_order_relaxed);
            if (g_gui.hwndMain) PostMessage(g_gui.hwndMain, WM_SHADER_STATE_CHANGED, 0, 0);
        }
    }

    for (auto& ctx : g_monitors) {
        if (ctx.enabled) {
            RenderMonitor(&ctx, fp, frameBufferActive);
            activeCount++;
        }
    }
    // Only stop if ALL monitors have failed
    if (activeCount == 0 && !g_monitors.empty()) {
        std::cerr << "All monitors failed, stopping" << std::endl;
        g_running = false;
    }
}

// ============================================================================
// SECTION: Overlay Window Procedure
// ============================================================================

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_DESTROY:
        if (hwnd == g_mainHwnd) {
            UnregisterHotKey(hwnd, HOTKEY_GAMMA);  // Gamma toggle
            UnregisterHotKey(hwnd, HOTKEY_ANALYSIS);  // Analysis toggle
            UnregisterHotKey(hwnd, HOTKEY_HDR_TOGGLE); // HDR toggle
            g_running = false;
            PostQuitMessage(0);
        }
        return 0;
    case WM_HOTKEY:
        // Wake auto-sleep on any hotkey (overlay may need to start rendering)
        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
        if (wParam == HOTKEY_GAMMA) {
            // Win+Shift+G - toggle gamma mode (HDR only)
            // Check if any monitor is in HDR mode
            bool anyHDR = false;
            for (const auto& ctx : g_monitors) {
                if (ctx.isHDREnabled) {
                    anyHDR = true;
                    break;
                }
            }
            if (anyHDR) {
                bool newMode = !g_desktopGammaMode.load();
                g_desktopGammaMode.store(newMode);
                // Update user preference to match manual toggle
                // This ensures whitelist respects the user's manual choice
                g_userDesktopGammaMode.store(newMode);
                // If user toggled while whitelist was active, set override flag
                // Override persists until the whitelisted app exits
                if (g_gammaWhitelistActive.load()) {
                    std::wstring overrideProcess;
                    {
                        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                        // Store the matched process name (lowercase) for tracking
                        g_gammaWhitelistOverrideProcess = g_gammaWhitelistMatch;
                        for (wchar_t& c : g_gammaWhitelistOverrideProcess) {
                            c = towlower(c);
                        }
                        overrideProcess = g_gammaWhitelistOverrideProcess;
                        g_gammaWhitelistMatch.clear();
                    }
                    g_gammaWhitelistUserOverride.store(true);
                    g_gammaWhitelistActive.store(false);
                    std::wcout << L"Gamma whitelist: user override active until " << overrideProcess << L" exits" << std::endl;
                }
                // Swap MHC ICC profiles for all HDR monitors
                SwapDgForAllMonitors(newMode);
                std::cout << "Gamma mode: " << (newMode ? "Desktop (2.2)" : "Content (sRGB)") << std::endl;
                RequestShowOSD(newMode ? L"Gamma: 2.2" : L"Gamma: sRGB");
            }
            // Silent ignore if no HDR monitors
        }
        else if (wParam == HOTKEY_ANALYSIS) {
            // Win+Shift+X - toggle analysis overlay
            ToggleAnalysisOverlay();
        }
        else if (wParam == HOTKEY_HDR_TOGGLE) {
            // Win+Shift+H - toggle HDR on focused monitor
            if (ToggleHdrOnFocusedMonitor()) {
                // On ACM displays, HDR→SDR keeps FP16 format so format-change detection
                // doesn't fire. Schedule a delayed reinit to re-detect HDR state after
                // Windows completes the transition (no render thread blocking).
                SetTimer(hwnd, HDR_REINIT_TIMER_ID, HDR_REINIT_DELAY_MS, nullptr);
            }
        }
        return 0;
    case WM_HOTKEY_REGISTER:
        // Handle hotkey register/unregister on the render thread (which owns g_mainHwnd)
        {
            int hotkeyId = (int)wParam;
            bool enable = (lParam != 0);
            UINT vk = 0;
            switch (hotkeyId) {
                case HOTKEY_GAMMA:    vk = g_hotkeyGammaKey; break;
                case HOTKEY_HDR_TOGGLE: vk = g_hotkeyHdrKey; break;
                case HOTKEY_ANALYSIS: vk = g_hotkeyAnalysisKey; break;
            }
            if (vk) {
                if (enable) RegisterHotKey(hwnd, hotkeyId, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, vk);
                else UnregisterHotKey(hwnd, hotkeyId);
            }
        }
        return 0;
    case WM_TIMER:
        if (wParam == HDR_REINIT_TIMER_ID) {
            KillTimer(hwnd, HDR_REINIT_TIMER_ID);
            // Release duplication so render loop re-detects HDR state and reapplies MHC profiles
            for (auto& ctx : g_monitors) {
                if (ctx.duplication) {
                    ctx.duplication->Release();
                    ctx.duplication = nullptr;
                }
                ctx.consecutiveFailures = 0;
            }
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return 0;
        }
        return 0;
    case WM_DWMCOMPOSITIONCHANGED:
        std::cout << "DWM composition changed, forcing reinit..." << std::endl;
        g_forceReinit.store(true);
        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
        return 0;
    case WM_WINDOWPOSCHANGING: {
        // If an external window is changing our z-order, trigger TOPMOST reassertion.
        // Ignore our own reasserts: HWND_TOPMOST check catches direct calls,
        // g_selfReassertInProgress catches indirect z-order changes from our analysis/OSD windows.
        WINDOWPOS* wp = reinterpret_cast<WINDOWPOS*>(lParam);
        if (wp && !(wp->flags & SWP_NOZORDER) && wp->hwndInsertAfter != HWND_TOPMOST
            && !g_selfReassertInProgress.load(std::memory_order_acquire)) {
            g_forceTopmostReassert.store(true);
        }
        break;
    }
    case WM_POWERBROADCAST:
        // Handle power events for sleep/wake recovery
        if (wParam == PBT_APMRESUMEAUTOMATIC || wParam == PBT_APMRESUMESUSPEND) {
            std::cout << "System power resume detected, forcing reinit..." << std::endl;
            g_forceReinit.store(true);
            g_forceMhcReapply.store(true);
            // Wake the render thread in case it is auto-sleeping — g_forceReinit is
            // checked AFTER the auto-sleep wait, so without this signal the thread
            // would miss the reinit until the 500ms timeout expires AND fall through.
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
        }
        // Handle display power state changes (sleep/wake of monitor only)
        else if (wParam == PBT_POWERSETTINGCHANGE) {
            POWERBROADCAST_SETTING* pbs = reinterpret_cast<POWERBROADCAST_SETTING*>(lParam);
            if (pbs && pbs->PowerSetting == GUID_CONSOLE_DISPLAY_STATE_LOCAL) {
                DWORD displayState = *reinterpret_cast<DWORD*>(pbs->Data);
                // 0 = off, 1 = on, 2 = dimmed
                if (displayState == 1) {
                    // Ignore spurious "display on" notification that fires immediately
                    // after registering (display is already on at startup)
                    auto elapsed = std::chrono::steady_clock::now() - g_powerNotifyRegisteredTime;
                    if (elapsed < std::chrono::seconds(3)) {
                        std::cout << "Ignoring initial display power notification" << std::endl;
                        g_displayOff.store(false);
                        return TRUE;
                    }
                    std::cout << "Display waking from sleep, forcing reinit..." << std::endl;
                    g_displayOff.store(false);
                    g_forceReinit.store(true);
                    g_forceMhcReapply.store(true);
                    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                } else if (displayState == 0) {
                    std::cout << "Display entering sleep mode" << std::endl;
                    g_displayOff.store(true);
                    // Reset watchdog to prevent timeout during display sleep
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                }
            }
        }
        return TRUE;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void CleanupMonitorContext(MonitorContext* ctx) {
    // Release D3D resources (keeps hwnd)
    ReleaseMonitorD3DResources(ctx);
    // Also destroy the window for full cleanup
    if (ctx->hwnd) { DestroyWindow(ctx->hwnd); ctx->hwnd = nullptr; }
}
