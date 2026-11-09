@echo off
rem Theodore installer for Windows. No admin rights, no package manager, no PowerShell script; everything goes
rem into %LOCALAPPDATA%\Theodore (or THEODORE_HOME). Written to be run by an AI assistant or by a person:
rem
rem   install.cmd --code ABCD-1234        sign this computer in with a one-time code and set everything up
rem   install.cmd                         the same, with a browser sign-in instead of a code
rem   install.cmd --accept-settings ...   also turn on plugin auto-update and allow the launcher in Claude Code
rem   install.cmd --uninstall             remove Theodore from this computer
rem
rem What it does, in order: prints its own SHA-256; downloads the Theodore plugin (a few small text files) into
rem <home>\plugin and checks its checksum; redeems the code before any large download; downloads Theodore's
rem Python runtime and engine (about 120 MB); registers the plugin in Claude Code and Codex when they are
rem installed; starts the asset packs (about 95 MB) in the background; ends with "theodore doctor".
rem Running it again is safe. Lines starting with THEODORE are statuses for the assistant: follow the NEXT line.
setlocal EnableExtensions DisableDelayedExpansion
rem Before any SHIFT: SHIFT moves %0 too.
set "SELF=%~f0"
rem The inbox tools by full path: a Git or MSYS tar.exe or curl.exe earlier on PATH behaves differently (GNU tar
rem reads C:\... as a remote host name).
set "SYS32=%SystemRoot%\System32"
set "CURL=%SYS32%\curl.exe"
set "TAR=%SYS32%\tar.exe"

set "DL_BASE=https://dl.theodoreand.ai"
if defined THEODORE_DL_BASE set "DL_BASE=%THEODORE_DL_BASE%"
set "HOME_DIR=%LOCALAPPDATA%\Theodore"
if defined THEODORE_HOME set "HOME_DIR=%THEODORE_HOME%"
set "CODE="
set "UNINSTALL="
set "ACCEPT="

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--code" goto :arg_code
if /i "%~1"=="--uninstall" set "UNINSTALL=1"
if /i "%~1"=="--uninstall" goto :next_arg
if /i "%~1"=="--accept-settings" set "ACCEPT=1"
if /i "%~1"=="--accept-settings" goto :next_arg
goto :bad_option
:next_arg
shift
goto :parse
:arg_code
set "CODE=%~2"
shift
shift
goto :parse
:parsed

set "HASH="
for /f "skip=1 tokens=* delims=" %%H in ('%SYS32%\certutil.exe -hashfile "%SELF%" SHA256 2^>nul') do if not defined HASH set "HASH=%%H"
if defined HASH echo theodore-install: installer sha256 %HASH: =% (compare with https://theodoreand.ai/install)

if not defined UNINSTALL goto :install
if exist "%HOME_DIR%\bin\theodore.cmd" "%HOME_DIR%\bin\theodore.cmd" uninstall
echo THEODORE OK: Theodore is not installed on this computer.
echo NEXT: nothing to do.
echo TELL THE USER: Theodore was not installed on this computer.
exit /b 0

:install
if not defined CODE goto :code_ok
echo %CODE%| %SYS32%\findstr.exe /r /i "^[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]-[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]$" >nul || goto :bad_code
:code_ok
if not exist "%CURL%" goto :no_curl
if not exist "%TAR%" goto :no_curl
if not exist "%HOME_DIR%\staging" mkdir "%HOME_DIR%\staging" 2>nul
if not exist "%HOME_DIR%\state" mkdir "%HOME_DIR%\state" 2>nul
if not exist "%HOME_DIR%\staging" goto :not_writable

rem --- 1. the plugin (stub skills and launcher: small text files) ------------------------------------------------
echo theodore-install: downloading the Theodore plugin
set "WHAT=the plugin version"
"%CURL%" -fsSL --retry 2 --connect-timeout 20 -o "%HOME_DIR%\staging\latest.txt" "%DL_BASE%/plugin/latest.txt" 2>nul
if errorlevel 1 goto :download_failed
set "P_VERSION="
set "P_SHA="
for /f "usebackq tokens=1,2" %%A in ("%HOME_DIR%\staging\latest.txt") do if not defined P_VERSION set "P_VERSION=%%A" & set "P_SHA=%%B"
if not defined P_SHA goto :hash_failed
set "WHAT=the plugin"
"%CURL%" -fsSL --retry 2 --connect-timeout 20 -o "%HOME_DIR%\staging\plugin.tar.gz" "%DL_BASE%/plugin/theodore-plugin-%P_VERSION%.tar.gz" 2>nul
if errorlevel 1 goto :download_failed
set "HASH="
for /f "skip=1 tokens=* delims=" %%H in ('%SYS32%\certutil.exe -hashfile "%HOME_DIR%\staging\plugin.tar.gz" SHA256 2^>nul') do if not defined HASH set "HASH=%%H"
if defined HASH set "HASH=%HASH: =%"
if /i not "%HASH%"=="%P_SHA%" goto :hash_failed
if exist "%HOME_DIR%\plugin.partial" rmdir /s /q "%HOME_DIR%\plugin.partial" >nul 2>&1
mkdir "%HOME_DIR%\plugin.partial" 2>nul
"%TAR%" -xzf "%HOME_DIR%\staging\plugin.tar.gz" -C "%HOME_DIR%\plugin.partial"
if errorlevel 1 goto :not_writable
if exist "%HOME_DIR%\plugin" rmdir /s /q "%HOME_DIR%\plugin" >nul 2>&1
move /y "%HOME_DIR%\plugin.partial" "%HOME_DIR%\plugin" >nul 2>&1
del /q "%HOME_DIR%\staging\plugin.tar.gz" "%HOME_DIR%\staging\latest.txt" >nul 2>&1
set "SCRIPTS=%HOME_DIR%\plugin\plugins\theodore\skills\pptx\scripts"
set "LAUNCHER=%SCRIPTS%\theodore.cmd"
if not exist "%LAUNCHER%" goto :no_launcher
echo theodore-install: plugin %P_VERSION% is in %HOME_DIR%\plugin

rem --- 2. redeem the one-time code first: it lasts ten minutes, the downloads may take longer -----------------------
if not defined CODE goto :runtime
call :jget api_base
set "API_BASE=%JV%"
call :jget stage1_client_version
set "C_VERSION=%JV%"
set "BODY=%HOME_DIR%\staging\activate.json"
>"%BODY%" echo {"code":"%CODE%","device_name":"%COMPUTERNAME%","platform":"win32","arch":"x64","host":"installer","client_version":"%C_VERSION%"}
set "HTTP=000"
"%CURL%" -sS --connect-timeout 20 -m 60 -o "%HOME_DIR%\state\activation.pending" -w "%%{http_code}" -H "Content-Type: application/json" --data-binary "@%BODY%" "%API_BASE%/v1/device/activate" >"%HOME_DIR%\staging\http.txt" 2>nul
set /p HTTP=<"%HOME_DIR%\staging\http.txt"
del /q "%BODY%" "%HOME_DIR%\staging\http.txt" >nul 2>&1
if "%HTTP%"=="200" echo theodore-install: this computer is signed in (the one-time code was accepted)
if "%HTTP%"=="200" goto :runtime
del /q "%HOME_DIR%\state\activation.pending" >nul 2>&1
echo theodore-install: the one-time code was not accepted (it may have expired or been used); a browser sign-in will be offered instead
set "CODE="

:runtime
rem --- 3. runtime and engine now, so the first deck request does not wait ------------------------------------------
echo theodore-install: downloading Theodore's runtime (about 120 MB); this is the long step
set "THEODORE_HOME=%HOME_DIR%"
set "THEODORE_BOOTSTRAP_WAIT=1800"
call "%LAUNCHER%" setup --wait 1800
if errorlevel 1 exit /b %ERRORLEVEL%
set "THEODORE_BOOTSTRAP_WAIT="
set "T=%HOME_DIR%\bin\theodore.cmd"
if not exist "%T%" set "T=%LAUNCHER%"

if defined CODE goto :hosts
call "%T%" whoami >nul 2>&1
if errorlevel 1 call "%T%" login --start

:hosts
rem --- 4. the assistants on this computer ------------------------------------------------------------------------------
echo theodore-install: registering Theodore in the assistants found on this computer
call "%T%" hosts register
if defined ACCEPT call "%T%" hosts settings --apply
if not defined ACCEPT call "%T%" hosts settings

rem --- 5. background downloads -------------------------------------------------------------------------------------------
call "%T%" whoami >nul 2>&1
if errorlevel 1 goto :doctor
call "%T%" guide pptx >nul 2>&1
echo theodore-install: asset packs (icons, flags, maps, backgrounds; about 95 MB) are downloading in the background
call "%T%" packs install --all --background >nul 2>&1

:doctor
rem --- 6. the check the assistant reports ---------------------------------------------------------------------------------
echo theodore-install: the stable launcher is %T%
call "%T%" doctor
exit /b %ERRORLEVEL%

:jget
set "JV="
for /f "usebackq tokens=1,* delims=:" %%A in (`%SYS32%\findstr.exe /c:"\"%~1\"" "%SCRIPTS%\launcher.json" 2^>nul`) do if not defined JV set "JV=%%B"
if not defined JV exit /b 0
set "JV=%JV:"=%"
if "%JV:~-1%"=="," set "JV=%JV:~0,-1%"
if "%JV:~0,1%"==" " set "JV=%JV:~1%"
exit /b 0

:bad_option
echo THEODORE USAGE: unknown installer option %~1.
echo NEXT: run the installer with --code ABCD-1234, or with no option for a browser sign-in.
echo TELL THE USER: I called the Theodore installer with a wrong option; I am correcting it.
exit /b 64

:bad_code
echo THEODORE USAGE: the activation code must look like ABCD-1234.
echo NEXT: use the code exactly as it appears in the sentence the user pasted.
echo TELL THE USER: The activation code did not look right. Please copy the sentence again from theodoreand.ai.
exit /b 64

:no_curl
echo THEODORE UNSUPPORTED_PLATFORM: this Windows has no curl.exe or tar.exe; Theodore needs 64-bit Windows 10 version 1803 or later.
echo NEXT: do not retry. Register the plugin directly instead (step 2 of the install page).
echo TELL THE USER: Theodore's installer needs Windows 10 version 1803 or later.
exit /b 69

:not_writable
echo THEODORE SETUP_FAILED: cause DISK: Theodore's folder is not writable.
echo NEXT: free disk space or set THEODORE_HOME to a writable folder, then run the installer again.
echo TELL THE USER: Theodore could not write its files. The disk may be full or the folder protected.
exit /b 68

:download_failed
echo THEODORE SETUP_FAILED: cause NETWORK: %WHAT% could not be downloaded from %DL_BASE%.
echo NEXT: if the download host is blocked, register the plugin directly instead (step 2 of the install page); otherwise check the connection and run the installer again.
echo TELL THE USER: Theodore's installer could not download its files. I will try the direct route.
exit /b 68

:hash_failed
echo THEODORE SETUP_FAILED: cause HASH_MISMATCH: the plugin did not match its published checksum.
echo NEXT: run the installer again; if it fails twice, tell the user to write to support@theodoreand.ai.
echo TELL THE USER: A Theodore download arrived damaged. I am trying again.
exit /b 68

:no_launcher
echo THEODORE INTERNAL: the plugin archive has no launcher.
echo NEXT: tell the user to write to support@theodoreand.ai.
echo TELL THE USER: Theodore's download is incomplete.
exit /b 78
