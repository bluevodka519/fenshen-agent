@echo off
rem One-click recording layout (README section 10). Double-click it; Ctrl+C in this window ends it.
rem   left: your phone screen (live, view only) | middle: the agent display | right: the steps
rem One-time setup, in any terminal (then open a new window):
rem   setx ANDROID_SDK_ROOT "<Android SDK folder>"
rem   setx FENSHEN_SCRCPY "<path to scrcpy.exe>"
rem   setx ZHIPU_API_KEY "<your Zhipu API key>"
rem With more than one device attached (e.g. the emulator is running too):
rem   setx FENSHEN_SERIAL "<serial from adb devices>"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
if not defined ANDROID_SDK_ROOT goto missing
if not defined FENSHEN_SCRCPY goto missing
if not defined ZHIPU_API_KEY goto missing
rem A fresh adb server owned by this window: one started from another tool (an IDE, an assistant's
rem terminal) dies when that tool restarts, and takes the phone link and the agent display with it.
"%ANDROID_SDK_ROOT%\platform-tools\adb.exe" kill-server >nul 2>&1
"%ANDROID_SDK_ROOT%\platform-tools\adb.exe" start-server >nul 2>&1
cd /d "%~dp0brain"
python -m fenshen web --llm autoglm --demo %*
if errorlevel 1 pause
exit /b

:missing
echo Missing one-time settings. Run these in a terminal, then start demo.cmd again:
if not defined ANDROID_SDK_ROOT echo   setx ANDROID_SDK_ROOT "<Android SDK folder>"
if not defined FENSHEN_SCRCPY echo   setx FENSHEN_SCRCPY "<path to scrcpy.exe>"
if not defined ZHIPU_API_KEY echo   setx ZHIPU_API_KEY "<your Zhipu API key>"
pause
