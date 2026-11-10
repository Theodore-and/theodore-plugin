@echo off
rem Theodore launcher (Windows batch). The same file sits in every Theodore skill folder and in <home>\bin.
rem
rem What it does, in order:
rem   1. finds Theodore's folder (THEODORE_HOME, default %LOCALAPPDATA%\Theodore);
rem   2. hands over to <home>\bin\theodore.cmd when that copy is newer, and keeps that copy current otherwise;
rem   3. when Theodore's Python and client are there, runs the client with the same arguments;
rem   4. otherwise downloads them in the background (stage 1: one Python archive and the client, each checked
rem      against a sha256 pinned in launcher.json beside this file) and reports progress.
rem
rem Everything it prints for the assistant is three lines: THEODORE <STATUS>: ... / NEXT: ... / TELL THE USER: ...
rem Exit codes: 0-63 are the engine's; 64-78 are statuses (67 setup in progress, 68 setup failed, 69 unsupported
rem platform, 74 connector required, 78 internal). It uses only tools that ship with Windows 10 1803 and later:
rem curl.exe, tar.exe, certutil, findstr; PowerShell's Invoke-WebRequest and BITS are download fallbacks.
rem No PowerShell script file is involved, so the execution policy does not matter.
setlocal EnableExtensions DisableDelayedExpansion
rem The inbox tools by full path: a Git or MSYS tar.exe or curl.exe earlier on PATH behaves differently (GNU tar
rem reads C:\... as a remote host name).
set "SYS32=%SystemRoot%\System32"

set "SELF_DIR=%~dp0"
if "%SELF_DIR:~-1%"=="\" set "SELF_DIR=%SELF_DIR:~0,-1%"
set "SELF=%SELF_DIR%\theodore.cmd"
set "HOME_DIR=%LOCALAPPDATA%\Theodore"
if defined THEODORE_HOME set "HOME_DIR=%THEODORE_HOME%"
if "%HOME_DIR%"=="\Theodore" goto :no_home
set "STABLE=%HOME_DIR%\bin\theodore.cmd"
set "CONF=%SELF_DIR%\launcher.json"
set "B=%HOME_DIR%\state\bootstrap"
if not exist "%CONF%" goto :no_keys
echo THEODORE INTERNAL: this copy of the Theodore plugin carries no release signing key (launcher.json has only the placeholder key), so it cannot trust any download.
echo NEXT: do not retry. Tell the user to install Theodore from https://theodoreand.ai/install; this copy is an unstamped development build.
echo TELL THE USER: This copy of Theodore was built without its security keys and cannot be used. Please install it again from theodoreand.ai/install.
exit /b 78

:no_conf
rem A plugin built without its release keys (the placeholder: kid manifest-unset, an all-zero key) trusts any
rem download: it runs nothing but uninstall. The client refuses such keys too, exactly.
if /i "%~1"=="uninstall" goto :keys_ok
"%SYS32%\findstr.exe" /r /c:"\"manifest_key_[0-9]*_kid\"" "%CONF%" 2>nul | "%SYS32%\findstr.exe" /v /c:"manifest-unset" >nul 2>&1 || goto :no_keys
"%SYS32%\findstr.exe" /r /c:"\"manifest_key_[0-9]*_x\"" "%CONF%" 2>nul | "%SYS32%\findstr.exe" /v /c:"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" >nul 2>&1 || goto :no_keys
:keys_ok

rem --- 2. one stable copy in <home>\bin: newer wins ---------------------------------------------------------------
call :jget launcher_generation "%CONF%"
set /a MY_GEN=JV
if /i "%SELF%"=="%STABLE%" goto :copied
set /a ST_GEN=-1
if not exist "%STABLE%" goto :st_done
if not exist "%HOME_DIR%\bin\launcher.json" goto :st_done
call :jget launcher_generation "%HOME_DIR%\bin\launcher.json"
set /a ST_GEN=JV
:st_done
if not defined THEODORE_SKILL_DIR for %%I in ("%SELF_DIR%\..") do set "THEODORE_SKILL_DIR=%%~fI"
if %ST_GEN% GTR %MY_GEN% goto :handover
if %ST_GEN% LSS %MY_GEN% call :self_copy
:copied
set "LAUNCHER=%SELF%"
if exist "%STABLE%" set "LAUNCHER=%STABLE%"

if "%~1"=="__bootstrap" goto :bootstrap_entry

rem --- 3. ready: run the client --------------------------------------------------------------------------------------
call :check_ready
if defined READY goto :run_client

rem --- not installed yet -------------------------------------------------------------------------------------------
if /i "%~1"=="uninstall" goto :uninstall_bare
rem Theodore's Python is not there: do what the client's uninstall does, as far as batch can, and say what is left.
rem The DPAPI credential is a file in state\, which goes with the folders.
set "LEFT="
where claude >nul 2>&1 && (call claude plugin uninstall theodore@theodore >nul 2>&1 & call claude plugin marketplace remove theodore >nul 2>&1)
where codex >nul 2>&1 && (call codex plugin remove theodore@theodore >nul 2>&1 & call codex plugin marketplace remove theodore >nul 2>&1)
if exist "%HOME_DIR%\state\host-settings.json" set "LEFT=two Theodore entries in the Claude Code settings.json (the extraKnownMarketplaces.theodore autoUpdate and the two Bash rules for the launcher under permissions.allow)"
if exist "%LOCALAPPDATA%\pptx-runtime" rmdir /s /q "%LOCALAPPDATA%\pptx-runtime" >nul 2>&1
if defined LEFT goto :uninstall_bare_left
echo THEODORE OK: Theodore was not fully installed; its folders, its registrations and its render cache were removed.
echo NEXT: nothing to do.
echo TELL THE USER: Theodore is uninstalled.
goto :uninstall_bare_remove
:uninstall_bare_left
echo THEODORE RETRY: Theodore's folders were removed, but these could not be: %LEFT%.
echo NEXT: tell the user what is left; it can be removed by hand (see 'Remove it' at https://github.com/Theodore-and/theodore-plugin).
echo TELL THE USER: Theodore's files are removed, but a few pieces could not be: %LEFT%. Your decks were not touched.
set "BARE_RC=75"
:uninstall_bare_remove
if not defined BARE_RC set "BARE_RC=0"
rem Only Theodore's own subfolders: the folder may hold things that are not Theodore's. One line, because this
rem file may be inside one of them.
(for %%D in (client runtime engines packs tools state logs staging plugin bin) do rmdir /s /q "%HOME_DIR%\%%D" >nul 2>&1) & rmdir "%HOME_DIR%" >nul 2>&1 & exit /b %BARE_RC%
