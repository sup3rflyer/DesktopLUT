@echo off
rem Build vblank_truth.exe into tools\vblank_truth\bin (VS2022 x64, single file). Usage: build.cmd
setlocal
set HERE=%~dp0
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
if not exist "%HERE%bin" mkdir "%HERE%bin"
cl /nologo /O2 /EHsc /std:c++20 /W4 /DUNICODE /D_UNICODE "%HERE%vblank_truth.cpp" /Fo"%HERE%bin\\" /Fe"%HERE%bin\vblank_truth.exe"
exit /b %ERRORLEVEL%
