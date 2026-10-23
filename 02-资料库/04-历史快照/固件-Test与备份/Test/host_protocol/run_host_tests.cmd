@echo off
rem ===========================================================================
rem Host-side self test for the line protocol.
rem NOTE: keep this file pure ASCII. cmd.exe parses .cmd as GBK; non-ASCII
rem       characters break line boundaries (learned the hard way).
rem   1) compile the real firmware sources with zig cc
rem      (host_protocol_inline.h + control_inline.h, hardware/USB stubbed)
rem   2) run the byte-level protocol tests
rem   3) re-parse every frame the firmware emitted with host protocol.py
rem   4) compile the same translation unit with the real ARMCC V5 toolchain
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/4] compile C tests (zig cc)
"%PY%" -m ziglang cc -std=c99 -Wall -Wextra -Wno-unused-parameter -I..\..\Drive\Include -I. test_host_protocol.c -o test_host_protocol.exe
if errorlevel 1 goto fail

echo [2/4] run C tests
test_host_protocol.exe
if errorlevel 1 goto fail

echo [3/4] cross-check frames.txt with host protocol.py
"%PY%" -X utf8 check_frames.py
if errorlevel 1 goto fail

echo [4/4] compile with ARMCC V5 (real firmware toolchain)
set "ARMCC=D:\study\keil\ARM\ARMCC\Bin\armcc.exe"
if not exist "%ARMCC%" (
  echo    skipped: armcc not found
  goto pass
)
"%ARMCC%" --c99 -c --cpu Cortex-M4 -O0 -I..\..\Drive\Include -I. test_host_protocol.c -o armcc_test.o
if errorlevel 1 goto fail
del /q armcc_test.o

:pass
echo.
echo ALL PASS
exit /b 0

:fail
echo.
echo FAILED (errorlevel=%errorlevel%)
exit /b 1
