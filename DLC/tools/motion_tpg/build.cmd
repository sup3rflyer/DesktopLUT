@echo off
rem Build motion_tpg.exe into tools\motion_tpg\bin (VS2022 x64, single file). Usage: build.cmd
setlocal
set HERE=%~dp0
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
if not exist "%HERE%bin" mkdir "%HERE%bin"
cl /nologo /O2 /EHsc /std:c++20 /W4 /DUNICODE /D_UNICODE "%HERE%motion_tpg.cpp" /Fo"%HERE%bin\\" /Fe"%HERE%bin\motion_tpg.exe" ^
   /link d3d11.lib dxgi.lib d3dcompiler.lib user32.lib gdi32.lib avrt.lib winmm.lib dwmapi.lib
exit /b %ERRORLEVEL%
