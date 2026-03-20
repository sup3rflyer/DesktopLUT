// DesktopLUT DWM Hook - hook_log.h
// Logging function, macros, and common defines shared by all hook source files

#pragma once

#include <io.h>
#include <cstdio>

// Common defines
#define DITHER_GAMMA 2.2
#define LUT_FOLDER "%SYSTEMROOT%\\Temp\\DesktopLUT_luts"

#define RELEASE_IF_NOT_NULL(x) { if (x != NULL) { x->Release(); } }
#define _STRINGIFY(x) #x
#define STRINGIFY(x) _STRINGIFY(x)
#define MAX_LOG_FILE_SIZE (20 * 1024 * 1024)

// Log file path (lazily initialized)
inline const char* GetLogFilePath()
{
	static char path[MAX_PATH] = {0};
	if (path[0] == '\0')
	{
		ExpandEnvironmentStringsA("%SYSTEMROOT%\\Temp\\DesktopLUT_dwmhook.log", path, sizeof(path));
	}
	return path;
}

inline void log_to_file(const char* log_buf)
{
	FILE* pFile = fopen(GetLogFilePath(), "a");
	if (pFile == NULL)
	{
		return;
	}
	fseek(pFile, 0, SEEK_END);
	long size = ftell(pFile);
	if (size > MAX_LOG_FILE_SIZE)
	{
		if (_chsize(_fileno(pFile), 0) == -1)
		{
			fclose(pFile);
			return;
		}
	}
	fseek(pFile, 0, SEEK_END);
	fprintf(pFile, "%s\n", log_buf);
	fclose(pFile);
}

// Logging macros
#define __LOG_ONLY_ONCE(x, y) if (static bool first_log_##y = true) { log_to_file(x); first_log_##y = false; }
#define _LOG_ONLY_ONCE(x, y) __LOG_ONLY_ONCE(x, y)
#define LOG_ONLY_ONCE(x) _LOG_ONLY_ONCE(x, __COUNTER__)

#define EXECUTE_WITH_LOG(winapi_func_hr) \
	do { \
		HRESULT hr = (winapi_func_hr); \
		if (FAILED(hr)) \
		{ \
			std::stringstream ss; \
			ss << "ERROR AT LINE: " << __LINE__ << " HR: " << hr << " - DETAILS: "; \
			LPSTR error_message = nullptr; \
			DWORD fmtResult = FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, \
				NULL, hr, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (LPSTR)&error_message, 0, NULL); \
			ss << ((fmtResult > 0 && error_message) ? error_message : "(FormatMessage failed)"); \
			if (error_message) LocalFree(error_message); \
			log_to_file(ss.str().c_str()); \
			throw std::exception(ss.str().c_str()); \
		} \
	} while (false);

#define EXECUTE_D3DCOMPILE_WITH_LOG(winapi_func_hr, error_interface) \
	do { \
		HRESULT hr = (winapi_func_hr); \
		if (FAILED(hr)) \
		{ \
			std::stringstream ss; \
			ss << "ERROR AT LINE: " << __LINE__ << " HR: " << hr << " - DETAILS: "; \
			LPSTR error_message = nullptr; \
			DWORD fmtResult = FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, \
				NULL, hr, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (LPSTR)&error_message, 0, NULL); \
			ss << ((fmtResult > 0 && error_message) ? error_message : "(FormatMessage failed)"); \
			if (error_interface) { \
				ss << " - DX COMPILE ERROR: " << (char*)error_interface->GetBufferPointer(); \
				error_interface->Release(); \
			} \
			if (error_message) LocalFree(error_message); \
			log_to_file(ss.str().c_str()); \
			throw std::exception(ss.str().c_str()); \
		} \
	} while (false);

#define LOG_ADDRESS(prefix_message, address) \
	{ \
		std::stringstream ss; \
		ss << prefix_message << " 0x" << std::setw(sizeof(address) * 2) << std::setfill('0') << std::hex << (UINT_PTR)address; \
		log_to_file(ss.str().c_str()); \
	}
