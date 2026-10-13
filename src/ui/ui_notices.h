#pragma once
// Third-party notices shown in Settings > About (MIT and BSD both require the notice to travel with the
// binary). DwmHook.dll links MinHook (with HDE); the interface links Dear ImGui.

namespace ui {

struct Notice {
    const char* name;
    const char* license;
    const char* text;
};

extern const Notice kNotices[];
extern const int kNoticeCount;

}  // namespace ui
