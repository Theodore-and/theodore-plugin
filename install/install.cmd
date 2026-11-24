@echo off
rem Theodore installer for Windows. No admin rights, no package manager, no PowerShell script; everything goes
rem into %USERPROFILE%\.theodore (or THEODORE_HOME). Written to be run by an AI assistant or by a person:
rem
rem   install.cmd --code ABCD-1234        sign this computer in with a one-time code and set everything up
rem   install.cmd                         the same, ending with a sign-in link instead of a code (no browser opens)
rem   install.cmd --no-settings ...       still accepted and changes nothing, as --accept-settings: Theodore writes no
rem                                       permission rule into an assistant's settings (in Claude Code its skills carry
rem                                       the permissions they need)
rem   install.cmd --uninstall             remove Theodore from this computer
rem
rem What it does, in order: prints its own SHA-256; redeems the one-time code (it lasts an hour); downloads
rem Theodore's Python and client and checks each against the SHA-256 pinned below; with them, fetches the signed
rem plugin release document, checks its signature against the keys pinned below, then fetches the plugin archive
rem it names and checks its SHA-256 and size; starts the rest of the runtime and the engine (about 100 to 130 MB in all
rem before the first deck) as a background job and waits for it a few minutes at most, so that a host's time limit on
rem the assistant's command never stops it; registers the plugin in Claude Code and Codex when they are installed;
rem starts the asset packs (about 95 MB) in the background; ends with "theodore doctor", or with SETUP_IN_PROGRESS while
rem the background download goes on. Running it again is safe: a code that was already used never removes the sign-in
rem an earlier run saved.
rem
rem Trust: this file is the root. Everything it runs was checked first: Python and the client against the pinned
rem SHA-256 values (certutil), the plugin against a document signed with a pinned key (verified by that Python:
rem Windows has no inbox Ed25519 tool), the runtime and engine by the client against signed manifests. Downloads
rem never follow a redirect. Lines starting with THEODORE are statuses for the assistant: follow the NEXT line.
setlocal EnableExtensions DisableDelayedExpansion
rem Before any SHIFT: SHIFT moves %0 too.
set "SELF=%~f0"
rem The download folder of the install line goes when the installer ends, also when the agent ran the line split and
rem dropped its cleanup (Codex does): only a folder named theodore-* that holds nothing but this file. The installer runs
rem as a call of itself, so the file stays on disk while it runs; then this outer run deletes the file and the folder
rem from memory ((goto) ends the batch context first) and passes the exit code on.
rem install.md carries a one-time code in its first line: a copy the agent saved in this folder or directly in %TEMP%
rem goes, only a file whose first line is install.md's own.
for %%F in ("%~dp0install.md" "%TEMP%\install.md") do if exist "%%~F" "%SystemRoot%\System32\findstr.exe" /b /c:"Read this file as downloaded, not through a tool that summarises web pages" "%%~F" >nul 2>&1 && del /f /q "%%~F" >nul 2>&1
if defined THEODORE_INSTALL_INNER goto :installer_body
set "IDIR=%~dp0"
if not defined IDIR goto :installer_body
set "IDIR=%IDIR:~0,-1%"
set "INAME="
for %%I in ("%IDIR%") do set "INAME=%%~nxI"
if /i not "%INAME:~0,9%"=="theodore-" goto :installer_body
if /i not "%~nx0"=="install.cmd" goto :installer_body
set "ICOUNT=0"
for /f %%N in ('dir /b /a "%IDIR%" 2^>nul ^| "%SystemRoot%\System32\find.exe" /c /v ""') do set "ICOUNT=%%N"
if not "%ICOUNT%"=="1" goto :installer_body
set "THEODORE_INSTALL_INNER=1"
call "%%SELF%%" %*
set "IRC=%ERRORLEVEL%"
set "THEODORE_INSTALL_INNER="
(goto) 2>nul & del /f /q "%IDIR%\install.cmd" >nul 2>&1 & rmdir "%IDIR%" >nul 2>&1 & "%SystemRoot%\System32\cmd.exe" /d /c exit %IRC%
:installer_body
rem The inbox tools by full path: a Git or MSYS tar.exe or curl.exe earlier on PATH behaves differently (GNU tar
rem reads C:\... as a remote host name).
set "SYS32=%SystemRoot%\System32"
set "CURL=%SYS32%\curl.exe"
set "TAR=%SYS32%\tar.exe"

rem --- pins: stamped by tools/sync_launchers.py from launcher.json; do not edit by hand ---------------------------
set "PIN_API_BASE=https://api.theodoreand.ai"
set "PIN_DL_BASE=https://dl.theodoreand.ai"
set "PIN_MANIFEST_KEYS=man-production-20261111-7792324a=LxgBIQ0n5McEVIMmiY-96H-YQErYK-OHpLBLLuscsbk"
set "PIN_RUNTIME_ID=b77f7d4ca2d43955"
set "PIN_CLIENT_VERSION=0.4.34"
set "PIN_CLIENT=client/client-0.4.34.tar 91eca206c9d5246ef79d956b09e705f09b2ffa4b73c70823ccbd7048a3508565"
set "PIN_PYTHON_win32_x64=runtime/b77f7d4ca2d43955/cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz 1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149"
rem --- end of pins --------------------------------------------------------------------------------------------------

set "API_BASE=%PIN_API_BASE%"
set "DL_BASE=%PIN_DL_BASE%"
rem Another mirror is safe: every file is checked against the pins.
if defined THEODORE_DL_BASE set "DL_BASE=%THEODORE_DL_BASE%"
rem Theodore's folder, one rule wherever it is found (both launchers, install.cmd, the short command and the client, each
rem with literally the same logic): THEODORE_HOME when set; else %USERPROFILE%\.theodore when it holds an installation
rem (bin\launcher.json); else %LOCALAPPDATA%\Theodore when that holds one (made by client 0.4.14 or earlier: it keeps
rem working, and updating, where it is); else %USERPROFILE%\.theodore (a new installation). Inside an MSIX-packaged app
rem (the Claude desktop app) Windows redirects %LOCALAPPDATA% into the app's own folder, unseen by every other program,
rem but not %USERPROFILE%\.theodore: an installation made there is the same folder for everyone. An install always goes
rem into %USERPROFILE%\.theodore (or THEODORE_HOME); see :env_check.
set "HOME_DIR=%USERPROFILE%\.theodore"
if not exist "%USERPROFILE%\.theodore\bin\launcher.json" if exist "%LOCALAPPDATA%\Theodore\bin\launcher.json" set "HOME_DIR=%LOCALAPPDATA%\Theodore"
if defined THEODORE_HOME set "HOME_DIR=%THEODORE_HOME%"
set "CODE="
set "UNINSTALL="
set "RATE_WAIT="
set "LIMIT_REFUSED="
set "WORK="
set "RC=0"
rem HTTPS only (plain HTTP only for a loopback test server); no -L, so no redirect is ever followed; 20 s to connect,
rem abort below 1 KB/s for 60 s, 30 minutes at most; three tries (:curl_host, which also tries each address in turn).
set "PROTO==https"
if /i "%API_BASE:~0,17%"=="http://127.0.0.1:" set "PROTO==https,http"
set "CURL_OPTS=-q -fsS --connect-timeout 20 --speed-limit 1024 --speed-time 60 -m 1800"
set "CURL_BODY="
set "CH_MEMO="

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--code" goto :arg_code
if /i "%~1"=="--uninstall" set "UNINSTALL=1"
if /i "%~1"=="--uninstall" goto :next_arg
if /i "%~1"=="--accept-settings" goto :next_arg
if /i "%~1"=="--no-settings" goto :next_arg
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

if not defined UNINSTALL goto :env_check
rem The launcher's uninstall stops Theodore's processes and starts the cleanup that removes the folder once the last
rem files are released; its status is the answer (checking the folder now would race that cleanup).
if exist "%HOME_DIR%\bin\theodore.cmd" (call "%%HOME_DIR%%\bin\theodore.cmd" uninstall & exit /b)
rem No launcher left (a removal already ran, or was interrupted): remove what is left of every path the uninstall is
rem documented to remove (Theodore's own folders and the engine's render cache) and say whether anything remains.
set "LEFTOVER="
call :remove_specimens
if defined SPLEFT set "LEFTOVER=%SPLEFT%"
rem In state\, the records a later uninstall still needs stay: settings to take back (they need Theodore's runtime),
rem specimen folders that could not be removed.
set "SETTINGS_LEFT="
set "REC=%HOME_DIR%\state\host-settings.json"
if exist "%REC%" "%SYS32%\findstr.exe" /c:"\"rules\":" /c:"\"marketplace\":" /c:"\"created_marketplace\":" /c:"\"set_auto_update\":" /c:"\"created_keys\":" /c:"\"git_bash\":" /c:"\"instructions\":" "%REC%" >nul 2>&1 && set "SETTINGS_LEFT=1"
if defined SETTINGS_LEFT set "LEFTOVER=%LEFTOVER%; Theodore's changes to the Claude Code settings.json and its marked lines in CLAUDE.md or AGENTS.md (theodore:begin to theodore:end), which need Theodore's runtime to take back exactly: what it added and the values before are kept in %REC%. To take them back, install Theodore again and run its uninstall, or edit that settings file by that record"
set "OWNED_DIRS=client runtime engines packs tools state logs staging plugin plugin.partial bin"
if defined SETTINGS_LEFT set "OWNED_DIRS=client runtime engines packs tools logs staging plugin plugin.partial bin"
if defined SPLEFT set "OWNED_DIRS=client runtime engines packs tools logs staging plugin plugin.partial bin"
if not "%OWNED_DIRS%"=="%OWNED_DIRS: state =%" goto :left_state_done
for /d %%D in ("%HOME_DIR%\state\*") do rmdir /s /q "%%~fD" >nul 2>&1
for %%F in ("%HOME_DIR%\state\*") do call :del_state_file "%%~fF"
:left_state_done
for %%D in (%OWNED_DIRS%) do if exist "%HOME_DIR%\%%D" rmdir /s /q "%HOME_DIR%\%%D" >nul 2>&1
if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\pptx-runtime" rmdir /s /q "%LOCALAPPDATA%\pptx-runtime" >nul 2>&1
if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Theodore\Cache" rmdir /s /q "%LOCALAPPDATA%\Theodore\Cache" >nul 2>&1
for %%D in (%OWNED_DIRS%) do if exist "%HOME_DIR%\%%D" set "LEFTOVER=%LEFTOVER%; Theodore's folders in %HOME_DIR%"
if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\pptx-runtime" set "LEFTOVER=%LEFTOVER%; the render cache %LOCALAPPDATA%\pptx-runtime"
if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Theodore\Cache" set "LEFTOVER=%LEFTOVER%; the render cache %LOCALAPPDATA%\Theodore\Cache"
call :old_engine_caches
if defined OCLEFT set "LEFTOVER=%LEFTOVER%%OCLEFT%"
if exist "%HOME_DIR%" rmdir "%HOME_DIR%" >nul 2>&1
if defined LEFTOVER goto :uninstall_left
echo THEODORE OK: Theodore is not installed on this computer.
echo NEXT: nothing to do.
echo TELL THE USER: Theodore was not installed on this computer.
exit /b 0

:env_check
rem An install always goes into %USERPROFILE%\.theodore (or THEODORE_HOME); an earlier one elsewhere is retired below.
if not defined THEODORE_HOME set "HOME_DIR=%USERPROFILE%\.theodore"
if "%HOME_DIR%"=="\.theodore" goto :not_writable
rem An uninstall's cleanup may still be running in this folder (an install right after an uninstall): its marker goes
rem first, which stops it, then the cleanup's answer is awaited before anything is written (see :stop_cleanup).
if exist "%HOME_DIR%\.theodore-uninstalling" call :stop_cleanup
rem A Theodore folder belongs to one service, the one it was set up for (state\environment): this installer does not
rem reuse a folder of another. Uninstalling is the way out, so it is allowed.
set "MY_API=%API_BASE%"
if "%MY_API:~-1%"=="/" set "MY_API=%MY_API:~0,-1%"
call :read_home_api
if not defined FOREIGN goto :install
:foreign_install
echo THEODORE USAGE: the Theodore folder "%HOME_DIR%" belongs to the Theodore service at %HOME_API%, but this installer is for %MY_API%; a folder is never shared between two services.
echo NEXT: do not retry. Tell the user: to install this build, set THEODORE_HOME to a separate folder for it and run the installer again, or first remove the other installation with: "%HOME_DIR%\bin\theodore.cmd" uninstall
echo TELL THE USER: Theodore found an installation for another Theodore service (%HOME_API%) in "%HOME_DIR%". To install this one, give it its own folder (THEODORE_HOME) or uninstall the other first.
exit /b 64

:install
if not defined CODE goto :code_ok
echo %CODE%| %SYS32%\findstr.exe /r /i "^[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]-[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]$" >nul || goto :bad_code
:code_ok
rem A build without its release keys (the repository's placeholder) would trust any plugin: it stops here.
echo.%PIN_MANIFEST_KEYS%| %SYS32%\findstr.exe /c:"manifest-unset" /c:"=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" >nul && goto :no_keys
if not exist "%CURL%" goto :no_curl
if not exist "%TAR%" goto :no_curl
set "ARCH="
if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" set "ARCH=x64"
if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "ARCH=x64"
rem Windows on ARM runs the x64 build through the system's emulation.
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=x64"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "ARCH=x64"
if not defined ARCH goto :no_curl
rem An earlier Theodore in %LOCALAPPDATA%\Theodore (client 0.4.14 or earlier), as this process sees it, is retired first
rem by its own uninstall, so that no second copy, stale registration or doubled disk use remains; then this one installs
rem afresh. Nothing of the old folder is moved or reused: inside the Claude desktop app it is that app's redirected view.
rem A failed retirement does not stop the install. Not with THEODORE_HOME, a folder the person chose.
if defined THEODORE_HOME goto :retired
if not defined LOCALAPPDATA goto :retired
set "OLD_HOME=%LOCALAPPDATA%\Theodore"
if not exist "%OLD_HOME%\bin\launcher.json" goto :retired
if not exist "%OLD_HOME%\bin\theodore.cmd" goto :retired
rem THEODORE_HOME names that folder for the old launcher, which then removes it and no other.
set "THEODORE_HOME=%OLD_HOME%"
call "%%OLD_HOME%%\bin\theodore.cmd" uninstall <nul >nul 2>&1
set "RETIRE_RC=%ERRORLEVEL%"
set "THEODORE_HOME="
if "%RETIRE_RC%"=="0" echo theodore-install: an earlier Theodore in "%OLD_HOME%" was removed; Theodore now lives in "%HOME_DIR%"
if not "%RETIRE_RC%"=="0" echo theodore-install: an earlier Theodore in "%OLD_HOME%" could not be removed completely (its uninstall answered %RETIRE_RC%); Theodore now lives in "%HOME_DIR%", and "%OLD_HOME%\bin\theodore.cmd" uninstall removes what is left there
:retired
if not exist "%HOME_DIR%\staging" mkdir "%HOME_DIR%\staging" 2>nul
if not exist "%HOME_DIR%\state" mkdir "%HOME_DIR%\state" 2>nul
if not exist "%HOME_DIR%\staging" goto :not_writable
rem Claim the folder for this service in one exclusive step, then check: of two installers of different services
rem racing on one empty folder, one wins and the other refuses.
call :claim_env
call :read_home_api
if defined FOREIGN goto :foreign_install
rem state\ holds the activation answer (the installation secret) for a minute: only this user may read it, whatever
rem folder THEODORE_HOME points at. Inheritance is cut and the current user alone gets access, before anything is written.
set "SID="
for /f "tokens=2 delims=," %%S in ('%SYS32%\whoami.exe /user /fo csv /nh 2^>nul') do set "SID=%%~S"
if defined SID "%SYS32%\icacls.exe" "%HOME_DIR%\state" /inheritance:r /grant:r "*%SID%:(OI)(CI)F" >nul 2>&1
if not defined SID "%SYS32%\icacls.exe" "%HOME_DIR%\state" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" >nul 2>&1
rem The installer's own files: one folder of its own (mkdir fails when the name exists, so it is never shared),
rem removed at every exit.
for /l %%N in (1,1,5) do if not defined WORK call :make_work
if not defined WORK goto :not_writable

rem --- 1. redeem the one-time code first: it lasts an hour, the downloads may take longer ---------------------------
if not defined CODE goto :stage1
rem The answer holds the installation secret. state\ may carry explicit grants of its own (THEODORE_HOME can point
rem anywhere), so the answer is written into a new folder of this installer whose only entry is the current user,
rem then moved: a file keeps its own ACL when it moves on the same volume. If the ACL cannot be set, no code is
rem redeemed (the browser sign-in follows instead).
set "SAFE="
for /l %%N in (1,1,5) do if not defined SAFE call :make_safe
if not defined SAFE goto :no_safe_folder
if "%SAFE%"=="none" goto :no_safe_folder
set "HTTP=000"
rem The body goes through a pipe: no file holds the code.
set "CURL_BODY={"code":"%CODE%","device_name":"%COMPUTERNAME%","platform":"win32","arch":"x64","host":"installer","client_version":"%PIN_CLIENT_VERSION%"}"
set "CURL_ARGS=-q -sS --proto %PROTO% --connect-timeout 20 -m 60 -o "%SAFE%\activation.pending" -H "Content-Type: application/json""
set "CURL_ERR=nul"
call :curl_host 1 "%API_BASE%/v1/device/activate"
set "CURL_BODY="
if not "%HTTP%"=="200" goto :code_refused
move /y "%SAFE%\activation.pending" "%HOME_DIR%\state\activation.pending" >nul 2>&1 || goto :code_not_saved
rmdir /s /q "%SAFE%" >nul 2>&1
echo theodore-install: this computer is signed in (the one-time code was accepted)
goto :stage1
:code_not_saved
rmdir /s /q "%SAFE%" >nul 2>&1
echo THEODORE SETUP_FAILED: cause DISK: the one-time code was accepted but this computer could not save the login in "%HOME_DIR%\state".
echo NEXT: the one-time code was accepted but this computer could not save the login (cause DISK); fix the folder's permissions and run the same line again within 30 minutes: the same code works again on this computer until it has been used.
echo TELL THE USER: Theodore could not save its login on this computer. Its folder may be protected; once that is fixed, the same install line works again for 30 minutes.
exit /b 68
:code_refused
rem The answer was written into the private folder only: a refused code (spent by an earlier run of the same line) never
rem touches the sign-in that run saved in state\activation.pending.
rem W-16: HTTP 429 rate_limited is the service's limit on sign-ins per account (or network) in an hour: the code was not
rem used and is not wrong. Its message is "Too many requests. Try again in N seconds." (the 7th word, N).
if "%HTTP%"=="429" "%SYS32%\findstr.exe" /c:"rate_limited" "%SAFE%\activation.pending" >nul 2>&1 && call :rate_wait
rem W-17: HTTP 409 computer_limit: the account already has its maximum number of computers logged in. The code was not
rem used (it works until it expires) and a browser sign-in would be refused the same way: no link. The answer holds no
rem secret; the client turns it into the last status (login --refused), with the service's own message. As install.sh.
if "%HTTP%"=="409" "%SYS32%\findstr.exe" /c:"computer_limit" "%SAFE%\activation.pending" >nul 2>&1 && move /y "%SAFE%\activation.pending" "%WORK%\activation.refused" >nul 2>&1 && set "LIMIT_REFUSED=%WORK%\activation.refused"
rmdir /s /q "%SAFE%" >nul 2>&1
set "CODE="
if defined RATE_WAIT echo theodore-install: too many log-ins on this Theodore account in the last hour, so this computer is not signed in yet; the one-time code was not used (try again in %RATE_WAIT% minutes)
if defined RATE_WAIT goto :stage1
if defined LIMIT_REFUSED echo theodore-install: the Theodore service did not log this computer in: this account already has its maximum number of computers logged in (the one-time code was not used); what to do follows at the end
if defined LIMIT_REFUSED goto :stage1
rem No answer (the network) or a failure of the service (5xx): the code was not used, and it is never called wrong.
if "%HTTP%"=="000" echo theodore-install: the Theodore service could not be reached, so the one-time code was not used; if this computer is not logged in already, a login link follows at the end
if "%HTTP%"=="000" goto :stage1
if "%HTTP:~0,1%"=="5" echo theodore-install: the Theodore service answered HTTP %HTTP%, so the one-time code was not used; if this computer is not logged in already, a login link follows at the end
if "%HTTP:~0,1%"=="5" goto :stage1
if not exist "%HOME_DIR%\state\activation.pending" goto :code_refused_new
echo theodore-install: the one-time code was not accepted (it was used already, by an earlier run of this installer); the login that run saved is kept
goto :stage1
:code_refused_new
echo theodore-install: the one-time code was not accepted (it may have expired or been used); if this computer is not logged in already, a login link follows at the end
goto :stage1
:no_safe_folder
echo theodore-install: the sign-in answer could not be given a private folder (icacls failed), so the one-time code was not used; a browser sign-in will be offered instead
set "CODE="

:stage1
rem --- 2. Python and the client, each against its pinned SHA-256 (the launcher's stage 1, the same files) -----------
for /f "tokens=1,2" %%A in ("%PIN_PYTHON_win32_x64%") do set "PY_PATH=%%A" & set "PY_SHA=%%B"
for /f "tokens=1,2" %%A in ("%PIN_CLIENT%") do set "C_PATH=%%A" & set "C_SHA=%%B"
set "RT=%PIN_RUNTIME_ID%"
set "CV=%PIN_CLIENT_VERSION%"
set "RT_DIR=%HOME_DIR%\runtime\%RT%"
set "PY=%RT_DIR%\python\python.exe"
set "HAVE="
if exist "%RT_DIR%\.parts\python" set /p HAVE=<"%RT_DIR%\.parts\python"
if exist "%PY%" if /i "%HAVE%"=="%PY_SHA%" goto :client
echo theodore-install: downloading Theodore's Python (checked against its pinned SHA-256)
set "WHAT=Theodore's Python"
call :fetch_checked "%DL_BASE%/%PY_PATH%" "%WORK%\python.tar.gz" "%PY_SHA%"
if errorlevel 2 goto :hash_failed
if errorlevel 1 goto :download_failed
if exist "%RT_DIR%\python" rmdir /s /q "%RT_DIR%\python" >nul 2>&1
mkdir "%WORK%\py" 2>nul
if not exist "%RT_DIR%\.parts" mkdir "%RT_DIR%\.parts" 2>nul
"%TAR%" -xzf "%WORK%\python.tar.gz" -C "%WORK%\py"
if errorlevel 1 goto :not_writable
move /y "%WORK%\py\python" "%RT_DIR%\python" >nul 2>&1
if not exist "%PY%" goto :not_writable
>"%RT_DIR%\.parts\python" echo %PY_SHA%
del /q "%WORK%\python.tar.gz" >nul 2>&1
:client
if exist "%HOME_DIR%\client\%CV%\__main__.py" goto :pointers
set "WHAT=the Theodore client"
call :fetch_checked "%DL_BASE%/%C_PATH%" "%WORK%\client.tar" "%C_SHA%"
if errorlevel 2 goto :hash_failed
if errorlevel 1 goto :download_failed
mkdir "%WORK%\client" 2>nul
if not exist "%HOME_DIR%\client" mkdir "%HOME_DIR%\client" 2>nul
"%TAR%" -xf "%WORK%\client.tar" -C "%WORK%\client"
if errorlevel 1 goto :not_writable
if exist "%HOME_DIR%\client\%CV%" rmdir /s /q "%HOME_DIR%\client\%CV%" >nul 2>&1
move /y "%WORK%\client\%CV%" "%HOME_DIR%\client\%CV%" >nul 2>&1
if not exist "%HOME_DIR%\client\%CV%\__main__.py" goto :not_writable
:pointers
if not exist "%HOME_DIR%\runtime\current.txt" >"%HOME_DIR%\runtime\current.txt" echo %RT%
if not exist "%HOME_DIR%\client\current.txt" >"%HOME_DIR%\client\current.txt" echo %CV%

rem --- 3. the plugin: the signed release document, then the archive it names ---------------------------------------
echo theodore-install: downloading the Theodore plugin (checked against its signed release document)
set "THEODORE_HOME=%HOME_DIR%"
"%PY%" -I "%HOME_DIR%\client\%CV%" install-plugin --dl-base "%DL_BASE%" --keys "%PIN_MANIFEST_KEYS%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :finish
set "SCRIPTS=%HOME_DIR%\plugin\plugins\theodore\skills\pptx\scripts"
set "LAUNCHER=%SCRIPTS%\theodore.cmd"
if not exist "%LAUNCHER%" goto :no_launcher
rem Until step 5 has registered the plugin, this marker says so: if this installer is stopped before then, the next
rem Theodore command registers it (as "theodore hosts register" does, which removes the marker).
if not exist "%HOME_DIR%\state\hosts.pending" type nul >"%HOME_DIR%\state\hosts.pending"

rem --- 4. runtime and engine: a background job, so the first deck request does not wait ----------------------------
rem The job outlives this installer. It is waited for a few minutes at most: an assistant's command has a time limit
rem (Claude Code stops a command after 10 minutes), and the job must not be what that limit stops.
rem Codex (CODEX_THREAD_ID: its CLI, the ChatGPT app's Codex mode, ChatGPT's Work mode) lets a command run longer and asks
rem before each command of the session that runs this installer (its rules load at the next session): wait longer here,
rem so the assistant has nothing to poll. As install.sh.
set "SETUP_WAIT_DEFAULT=180"
if defined CODEX_THREAD_ID set "SETUP_WAIT_DEFAULT=540"
set "SETUP_WAIT=%SETUP_WAIT_DEFAULT%"
if defined THEODORE_INSTALL_WAIT set "SETUP_WAIT=%THEODORE_INSTALL_WAIT%"
echo.%SETUP_WAIT%| "%SYS32%\findstr.exe" /r /x "[0-9][0-9]*" >nul || set "SETUP_WAIT=%SETUP_WAIT_DEFAULT%"
echo theodore-install: downloading the rest of Theodore's runtime and its engine (about 100 to 130 MB in all) in the background; this is the long step
if defined CODEX_THREAD_ID goto :setup_codex_start
set "THEODORE_BOOTSTRAP_WAIT=%SETUP_WAIT%"
call "%%LAUNCHER%%" setup --wait %SETUP_WAIT% >"%WORK%\setup.txt"
set "RC=%ERRORLEVEL%"
goto :setup_waited
:setup_codex_start
rem Codex shows a long command's output as it comes: a line a minute says the download is moving, so the assistant has
rem no reason to inspect processes (each such command asks first in this session). The wait goes in steps of a minute.
set /a SETUP_LEFT=SETUP_WAIT
:setup_codex_step
set /a SETUP_STEP=60
if %SETUP_LEFT% LSS 60 set /a SETUP_STEP=SETUP_LEFT
set "THEODORE_BOOTSTRAP_WAIT=%SETUP_STEP%"
call "%%LAUNCHER%%" setup --wait %SETUP_STEP% >"%WORK%\setup.txt"
set "RC=%ERRORLEVEL%"
set /a SETUP_LEFT-=SETUP_STEP
if not "%RC%"=="67" goto :setup_waited
if %SETUP_LEFT% LEQ 0 goto :setup_waited
set "SETUP_NOW= downloading."
for /f "tokens=1,* delims=:" %%A in ('%SYS32%\findstr.exe /b /c:"THEODORE SETUP_IN_PROGRESS:" "%WORK%\setup.txt"') do set "SETUP_NOW=%%B"
setlocal EnableDelayedExpansion
set "SETUP_NOW=!SETUP_NOW:~1!"
if "!SETUP_NOW:~-1!"=="." set "SETUP_NOW=!SETUP_NOW:~0,-1!"
echo theodore-install: still downloading (!SETUP_NOW!); this installer waits at most !SETUP_LEFT! s more, then the download goes on by itself
endlocal
goto :setup_codex_step
:setup_waited
set "THEODORE_BOOTSTRAP_WAIT="
set "SETUP_FAILED="
if "%RC%"=="0" goto :setup_done
if "%RC%"=="67" goto :setup_background
rem A failed download (68) is reported after the plugin is registered: the next command resumes it.
if "%RC%"=="68" goto :setup_failed_later
type "%WORK%\setup.txt"
goto :finish
:setup_background
echo theodore-install: the download goes on in the background after this installer has ended
goto :setup_done
:setup_failed_later
set "SETUP_FAILED=1"
:setup_done
set "RC=0"
set "T=%HOME_DIR%\bin\theodore.cmd"
if not exist "%T%" set "T=%LAUNCHER%"


rem --- 5. the assistants on this computer ------------------------------------------------------------------------------
echo theodore-install: registering Theodore in the assistants found on this computer
rem Its lines say what it registered; its own status block is left out (lane L3), as install.sh.
call "%%T%%" hosts register >"%WORK%\hosts.txt"
set "HOSTS_RC=%ERRORLEVEL%"
"%SYS32%\findstr.exe" /v /b /c:"THEODORE " /c:"NEXT: " /c:"TELL THE USER: " "%WORK%\hosts.txt"
rem Claude Code on Windows with no Git Bash cannot load the skills: PortableGit into Theodore's folder, Claude Code pointed at it.
call "%%T%%" tools install git --if-needed
rem No permission rule goes into an assistant's settings (lane L3, 24 November 2026: the assistant running this declined an
rem installer that changes its own permissions, in all three real set-ups). Theodore's skills carry the permissions they
rem need; "theodore hosts settings --apply" adds rules only when the user asks for it. As install.sh.
if not defined SETUP_FAILED goto :background
echo theodore-install: the stable launcher is "%T%"
type "%WORK%\setup.txt"
set "RC=68"
goto :finish
:background

rem --- 6. background downloads -------------------------------------------------------------------------------------------
call "%%T%%" whoami >nul 2>&1
if errorlevel 1 goto :doctor
call "%%T%%" guide pptx >nul 2>&1
echo theodore-install: asset packs (icons, flags, maps, backgrounds; about 95 MB), when published for this channel, download in the background
call "%%T%%" packs install --all --background >nul 2>&1
rem Nothing to turn slides into images (no LibreOffice, no PowerPoint): fetch LibreOffice, as on macOS.
call "%%T%%" tools install libreoffice --background --if-needed >nul 2>&1

:doctor
rem --- 7. the check the assistant reports: the last line says what failed, if anything did -------------------------
echo theodore-install: the stable launcher is "%T%"
rem Not signed in (no code, or it was not accepted): the last block carries the sign-in link for the assistant to
rem relay. The installer never opens a browser; only "theodore login --open", asked for by the user, does.
call "%%T%%" whoami >nul 2>&1
if errorlevel 1 goto :sign_in_last
call "%%T%%" doctor >"%WORK%\doctor.txt"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="67" goto :doctor_shown
"%SYS32%\findstr.exe" /b /c:"NEXT: run the same command again" "%WORK%\doctor.txt" >nul || goto :doctor_shown
rem Still downloading in the background: doctor's checks, then a status that names doctor itself (its own status says
rem "the same command", which here would be this installer).
"%SYS32%\findstr.exe" /v /b /c:"THEODORE " /c:"NEXT: " /c:"TELL THE USER: " "%WORK%\doctor.txt"
rem The last status is the client's welcome (lanes ONBOARD and L3), as install.sh: a few short lines for the assistant to
rem give in this conversation, with Studio's state and the asset packs' state, and what this host needs (a new session,
rem the download going on); run by a person, plain words, and the welcome left owed for the assistant they go back to.
set "WELCOME_ARG=--downloading"
call :welcome
if not errorlevel 1 goto :finish
rem In Codex every command of this session asks first: nothing to poll. The download finishes by itself, and the first
rem deck request in a new session waits for it. As install.sh.
if not defined CODEX_THREAD_ID goto :doctor_in_progress
if not "%HOSTS_RC%"=="0" goto :doctor_in_progress
set "WHERE= downloading."
for /f "tokens=1,* delims=:" %%A in ('%SYS32%\findstr.exe /b /c:"THEODORE SETUP_IN_PROGRESS:" "%WORK%\doctor.txt"') do set "WHERE=%%B"
setlocal EnableDelayedExpansion
set "WHERE=!WHERE:~1!"
if "!WHERE:~-1!"=="." set "WHERE=!WHERE:~0,-1!"
echo THEODORE OK: Theodore is installed and signed in; the rest of its runtime and its engine finish downloading by themselves in the background (!WHERE!).
echo NEXT: run nothing more now, neither doctor nor this installer: the download needs no command. Tell the user to start a new session and ask for a deck. If they ask for a deck in this session anyway, ask them to start a new session first: Codex loads Theodore's tool and permissions only when a session starts, so here each Theodore command would ask for approval.
echo TELL THE USER: Theodore is installed. It finishes a download by itself in the background for a few minutes; start a new session and ask for a deck.
endlocal
set "RC=0"
goto :finish
:doctor_in_progress
echo THEODORE SETUP_IN_PROGRESS: Theodore is installed; the rest of its runtime and its engine are still downloading in the background, which goes on after this installer has ended.
echo NEXT: run "%T%" status --wait 100 (it waits for the download and returns as soon as it is done), again while it says SETUP_IN_PROGRESS (not this installer), then report its last line.
echo TELL THE USER: Theodore is installed and is finishing its download in the background. This takes a few minutes; I will check on it.
set "RC=0"
goto :finish
:doctor_shown
if not "%RC%"=="0" type "%WORK%\doctor.txt"
if not "%RC%"=="0" goto :finish
rem Ready: doctor's checks, then the welcome as the only status (doctor's own ready line would come before it). As install.sh.
"%SYS32%\findstr.exe" /v /b /c:"THEODORE " /c:"NEXT: " /c:"TELL THE USER: " "%WORK%\doctor.txt"
set "WELCOME_ARG="
call :welcome
if not errorlevel 1 goto :finish
"%SYS32%\findstr.exe" /b /c:"THEODORE " /c:"NEXT: " /c:"TELL THE USER: " "%WORK%\doctor.txt"
if not "%HOSTS_RC%"=="0" goto :hosts_left
if not defined CODEX_THREAD_ID goto :finish
rem Codex reads plugins, MCP servers and rules when a session starts: the deck belongs in a new session. As install.sh.
echo THEODORE OK: Theodore is ready (the check above).
echo NEXT: run nothing more now. Tell the user to start a new session and ask for a deck. If they ask for a deck in this session anyway, ask them to start a new session first: Codex loads Theodore's tool and permissions only when a session starts, so here each Theodore command would ask for approval.
echo TELL THE USER: Theodore is ready. Start a new session and ask for your deck there.
goto :finish
:hosts_left
echo THEODORE RETRY: Theodore is installed, but it could not be added to every assistant on this computer (see the claude-code and codex lines above).
echo NEXT: register by hand with the commands under 'Other ways' at https://theodoreand.ai/install, then start a new session.
echo TELL THE USER: Theodore is installed, but I could not add it to one of your assistants automatically. I will add it by hand.
set "RC=75"
goto :finish

:sign_in_last
if defined LIMIT_REFUSED goto :limit_last
if not defined RATE_WAIT goto :sign_in_link
echo THEODORE RETRY: Theodore is installed, but this computer is not signed in: there have been too many log-ins on this Theodore account in the last hour, and the one-time code was not used. Try again in %RATE_WAIT% minutes.
echo NEXT: do not retry now. Tell the user, and run the same install line again in %RATE_WAIT% minutes (with a fresh sentence from the Theodore site if the code has expired by then).
echo TELL THE USER: There have been too many Theodore log-ins on your account in the last hour. Please try again in about %RATE_WAIT% minutes; your code was not used.
set "RC=75"
goto :finish
:sign_in_link
call "%%T%%" login --start
set "RC=%ERRORLEVEL%"
goto :finish
:limit_last
rem W-17: the computer limit, never a sign-in link (it would be refused the same way). As install.sh.
call "%%T%%" login --refused "%%LIMIT_REFUSED%%"
set "RC=%ERRORLEVEL%"
goto :finish

rem =====================================================================================================================
rem Subroutines

:rate_wait
set "RW="
for /f "usebackq tokens=7 delims= " %%S in ("%SAFE%\activation.pending") do if not defined RW set "RW=%%S"
set /a RATE_WAIT=(RW+59)/60 >nul 2>&1
if not defined RATE_WAIT set "RATE_WAIT=60"
if %RATE_WAIT% LEQ 0 set "RATE_WAIT=60"
exit /b 0
rem =====================================================================================================================

:remove_specimens
rem All of them (uninstall). Result in SPLEFT.
set "SPLEFT="
call :ps_specimens all
if defined LOCALAPPDATA rmdir "%LOCALAPPDATA%\Theodore\Cache\specimens" >nul 2>&1
if defined LOCALAPPDATA rmdir "%LOCALAPPDATA%\Theodore\Cache" >nul 2>&1
exit /b 0

:ps_specimens
rem %1 all or expired. Specimen folders: every one the client made is a line "<created epoch> <path>" in
rem state\specimens.txt (after a first line "#v2" the paths are encoded: %%25, %%0D, %%0A for "%%", CR and LF; a record
rem without it is read as it is). Batch cannot decode that (a "%%" in a value is expanded again by CALL, and a
rem substitution cannot search for "%%"), so PowerShell reads the record, as the client does, and removes the folders it
rem names and those in today's roots: only real folders named theodore-specimens-*, never through a junction or link.
rem What could not be removed is added to SPLEFT. Without PowerShell: plain paths only, and a record holding an encoded
rem path is kept and named (Theodore's runtime, or PowerShell, removes it).
set "PSOUT=%TEMP%\theodore-specimens-left-%RANDOM%%RANDOM%.txt"
set "PWSH=%SYS32%\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PWSH%" goto :ps_specimens_none
set "THEODORE_SWEEP_HOME=%HOME_DIR%"
set "THEODORE_SWEEP_MODE=%~1"
set "THEODORE_SWEEP_OUT=%PSOUT%"
"%PWSH%" -NoProfile -NonInteractive -Command "$now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); $all=($env:THEODORE_SWEEP_MODE -eq 'all'); $d=@(); $r=Join-Path $env:THEODORE_SWEEP_HOME 'state\specimens.txt'; if(Test-Path -LiteralPath $r){$lines=@(Get-Content -LiteralPath $r -Encoding UTF8); $enc=($lines.Count -gt 0 -and $lines[0] -eq '#v2'); foreach($l in $lines){if($l -eq '#v2'){continue}; $p=$l.Split(' ',2); $t=0; if($p.Count -eq 2 -and [long]::TryParse($p[0],[ref]$t)){$x=$p[1]; if($enc){$x=[regex]::Replace($x,'%%(25|0[AaDd])',{param($m) [string][char][Convert]::ToInt32($m.Groups[1].Value,16)})}; if($all -or ($now-$t) -gt 3600 -or ($t-$now) -gt 60){$d+=$x}}}}; $roots=@($env:TEMP); if($env:LOCALAPPDATA){$roots+=Join-Path $env:LOCALAPPDATA 'Theodore\Cache\specimens'}; foreach($root in $roots){if($root -and (Test-Path -LiteralPath $root)){Get-ChildItem -LiteralPath $root -Directory -Filter 'theodore-specimens-*' -ErrorAction SilentlyContinue | Where-Object {$all -or $_.LastWriteTime -lt (Get-Date).AddHours(-1)} | ForEach-Object {$d+=$_.FullName}}}; $left=@(); foreach($x in $d){$i=Get-Item -LiteralPath $x -Force -ErrorAction SilentlyContinue; if($i -and $i.PSIsContainer -and $i.Name -like 'theodore-specimens-?*' -and -not ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)){Remove-Item -LiteralPath $x -Recurse -Force -ErrorAction SilentlyContinue; if(Test-Path -LiteralPath $x){$left+=$x}}}; Set-Content -LiteralPath $env:THEODORE_SWEEP_OUT -Value $left -Encoding Default" <nul >nul 2>&1
set "THEODORE_SWEEP_HOME="
set "THEODORE_SWEEP_MODE="
set "THEODORE_SWEEP_OUT="
if exist "%PSOUT%" for /f "usebackq delims=" %%L in ("%PSOUT%") do (set "ONE=%%L" & call :specimen_left)
if exist "%PSOUT%" del /q "%PSOUT%" >nul 2>&1
exit /b 0
:ps_specimens_none
if /i not "%~1"=="all" exit /b 0
if exist "%HOME_DIR%\state\specimens.txt" for /f "usebackq tokens=1,*" %%A in ("%HOME_DIR%\state\specimens.txt") do (set "ONE=%%B" & call :remove_one)
if defined TEMP for /d %%D in ("%TEMP%\theodore-specimens-*") do (set "ONE=%%~fD" & call :remove_one)
if defined LOCALAPPDATA for /d %%D in ("%LOCALAPPDATA%\Theodore\Cache\specimens\theodore-specimens-*") do (set "ONE=%%~fD" & call :remove_one)
if exist "%HOME_DIR%\state\specimens.txt" "%SYS32%\findstr.exe" /c:"%%25" /c:"%%0A" /c:"%%0D" "%HOME_DIR%\state\specimens.txt" >nul 2>&1 && set "SPLEFT=%SPLEFT%; specimen folders recorded under an encoded path in %HOME_DIR%\state\specimens.txt (removing them needs PowerShell or Theodore's runtime)"
exit /b 0
:specimen_left
set "SPLEFT=%SPLEFT%; the specimen folder %ONE%"
exit /b 0
:remove_one
rem ONE: a path from the record or a root (read through a variable: CALL would expand a "%%" in it again).
for %%N in ("%ONE%") do set "SPN=%%~nxN"
if not "%SPN:~0,19%"=="theodore-specimens-" exit /b 0
if "%SPN%"=="theodore-specimens-" exit /b 0
if not exist "%ONE%\" exit /b 0
for %%N in ("%ONE%") do set "SPA=%%~aN"
if not "%SPA:l=%"=="%SPA%" exit /b 0
rmdir /s /q "%ONE%" >nul 2>&1
if exist "%ONE%\" set "SPLEFT=%SPLEFT%; the specimen folder %ONE%"
exit /b 0

if "%SPN%"=="theodore-specimens-" exit /b 0
if not exist "%~1\" exit /b 0
set "SPA=%~a1"
if not "%SPA:l=%"=="%SPA%" exit /b 0
rmdir /s /q "%~1" >nul 2>&1
if exist "%~1\" set "SPLEFT=%SPLEFT%; the specimen folder %~1"
exit /b 0

:read_home_api
rem The service this folder belongs to (state\environment; a folder from before the record: its stable launcher.json).
rem Sets HOME_API, and FOREIGN when it is not this installer's.
set "HOME_API="
set "FOREIGN="
if exist "%HOME_DIR%\state\environment" for /f "usebackq delims=" %%L in ("%HOME_DIR%\state\environment") do if not defined HOME_API set "HOME_API=%%L"
if exist "%HOME_DIR%\state\environment" goto :read_home_api_compare
if not exist "%HOME_DIR%\bin\launcher.json" exit /b 0
for /f "usebackq tokens=1,* delims=:" %%A in (`%SYS32%\findstr.exe /c:"\"api_base\"" "%HOME_DIR%\bin\launcher.json" 2^>nul`) do if not defined HOME_API set "HOME_API=%%B"
if not defined HOME_API exit /b 0
set "HOME_API=%HOME_API:"=%"
if "%HOME_API:~-1%"=="," set "HOME_API=%HOME_API:~0,-1%"
if "%HOME_API:~0,1%"==" " set "HOME_API=%HOME_API:~1%"
:read_home_api_compare
if not defined HOME_API exit /b 0
if "%HOME_API:~-1%"=="/" set "HOME_API=%HOME_API:~0,-1%"
if /i not "%HOME_API%"=="%MY_API%" set "FOREIGN=1"
exit /b 0

:claim_env
rem The record is written whole beside its place and renamed into it; a rename never replaces an existing file.
if exist "%HOME_DIR%\state\environment" exit /b 0
set "ENVTMP=%HOME_DIR%\state\.environment.%RANDOM%%RANDOM%"
(>"%ENVTMP%" echo %MY_API%) 2>nul
if exist "%ENVTMP%" ren "%ENVTMP%" environment >nul 2>&1
if exist "%ENVTMP%" del /q "%ENVTMP%" >nul 2>&1
exit /b 0

:del_state_file
if /i "%~nx1"=="host-settings.json" if defined SETTINGS_LEFT exit /b 0
if /i "%~nx1"=="specimens.txt" if defined SPLEFT exit /b 0
del /f /q "%~1" >nul 2>&1
exit /b 0

:make_work
set "TRY=%HOME_DIR%\staging\install-%RANDOM%%RANDOM%"
mkdir "%TRY%" 2>nul && set "WORK=%TRY%"
exit /b 0

:stop_cleanup
rem The marker goes, then this waits for .theodore-cleanup-safe: the cleanup writes it only where nothing it does can
rem touch what an installation writes, and looks for the marker before each such step, so from then on it stops before
rem touching anything. A fixed five-second pause did not do: on 2026-11-23 a pass that had begun before it was still
rem removing staging\ while this installer wrote there. At most a minute (a cleanup that was killed never answers),
rem then on in any case; CLEANUP_BUSY then lets a failure to write say what it most likely is.
set /a CLEANUP_WAITED=0
del /f /q "%HOME_DIR%\.theodore-uninstalling" >nul 2>&1
:stop_cleanup_wait
if exist "%HOME_DIR%\.theodore-cleanup-safe" goto :stop_cleanup_done
if %CLEANUP_WAITED% GEQ 60 set "CLEANUP_BUSY=1"
if %CLEANUP_WAITED% GEQ 60 goto :stop_cleanup_done
"%SYS32%\ping.exe" -n 2 127.0.0.1 >nul 2>&1
set /a CLEANUP_WAITED+=1
goto :stop_cleanup_wait
:stop_cleanup_done
del /f /q "%HOME_DIR%\.theodore-cleanup-safe" >nul 2>&1
exit /b 0

:make_safe
rem A new folder under state\ (mkdir fails when the name exists) whose ACL is exactly: the current user, full control.
set "TRY=%HOME_DIR%\state\incoming-%RANDOM%%RANDOM%"
mkdir "%TRY%" 2>nul || exit /b 0
if not defined SID goto :make_safe_failed
"%SYS32%\icacls.exe" "%TRY%" /inheritance:r /grant:r "*%SID%:(OI)(CI)F" >nul 2>&1 || goto :make_safe_failed
set "SAFE=%TRY%"
exit /b 0
:make_safe_failed
rmdir /s /q "%TRY%" >nul 2>&1
set "SAFE=none"
exit /b 0

:fetch
rem %1 url, %2 destination. Errorlevel 0 when the server answered 200 with the file; DETAIL says why not.
set "HTTP=000"
set "CURL_ARGS=%CURL_OPTS% --proto %PROTO% -o "%~2""
set "CURL_ERR=%WORK%\err.txt"
type nul >"%WORK%\err.txt"
call :curl_host 3 "%~1"
if "%CRC%"=="0" if "%HTTP%"=="200" exit /b 0
set "DETAIL=curl exit %CRC%, HTTP %HTTP%"
if "%HTTP:~0,1%"=="3" set "DETAIL=it answered with a redirect, which is not followed"
del /q "%~2" >nul 2>&1
exit /b 1

:curl_host
rem %1 tries, %2 url. CURL_ARGS: curl's options (the URL, -w and the address are added here); CURL_BODY: a body sent on
rem stdin, if any; CURL_ERR: where curl's messages go. Sets HTTP (000 when nothing answered) and CRC (curl's exit status).
rem Theodore's hosts resolve to several addresses, and from some networks one of them does not answer, or completes the
rem TCP connection but never the TLS handshake (or resets it), while another works. curl moves to the next address only
rem when the TCP connection fails: after a handshake that hangs or is reset it gives up (exit 28 or 35), and --retry
rem starts again from the same first address. So, without a proxy, each address is tried in turn (curl --resolve: the
rem name, its SNI and its certificate check stay the host's), 4 s each to connect and finish the handshake; the address
rem that worked is tried first for the rest of this run; when every address only ran out of time (a slow link), the first
rem is tried once more with the caller's own connect timeout. With a proxy (which chooses the address itself), for a loopback
rem test server, or when nslookup lists no address for the name, curl runs as before.
set "CH_TRIES=%~1"
set "CH_URL=%~2"
set "CH_HOST="
set "CH_PORT="
set "CH_ADDRS="
set "CH_FIRST="
if /i not "%CH_URL:~0,8%"=="https://" goto :curl_plain
if defined HTTPS_PROXY goto :curl_plain
if defined ALL_PROXY goto :curl_plain
for /f "tokens=1 delims=/" %%H in ("%CH_URL:~8%") do set "CH_HP=%%H"
for /f "tokens=1,2 delims=:" %%H in ("%CH_HP%") do set "CH_HOST=%%H" & set "CH_PORT=%%I"
if not defined CH_PORT set "CH_PORT=443"
if not defined CH_HOST goto :curl_plain
echo %CH_HOST%| "%SYS32%\findstr.exe" /r /x /i "[a-z0-9.-]*" >nul || goto :curl_plain
call :host_addresses "%CH_HOST%"
if not defined CH_ADDRS goto :curl_plain
set "CH_GOOD="
for %%M in (%CH_MEMO%) do for /f "tokens=1,2 delims=#" %%P in ("%%M") do if /i "%%P"=="%CH_HOST%" set "CH_GOOD=%%Q"
set "CH_LIST=%CH_GOOD%"
for %%A in (%CH_ADDRS%) do if /i not "%%A"=="%CH_GOOD%" call set "CH_LIST=%%CH_LIST%% %%A"
set "CH_ROUND=1"
:curl_round
set "CH_DONE="
set "CH_SLOW=1"
for %%A in (%CH_LIST%) do if not defined CH_DONE call :curl_address %%A
if %CH_ROUND% geq %CH_TRIES% goto :curl_slow
rem what --retry retries: 408, 429, 5xx, and no answer at all
set "CH_AGAIN="
if "%HTTP%"=="408" set "CH_AGAIN=1"
if "%HTTP%"=="429" set "CH_AGAIN=1"
if "%HTTP:~0,1%"=="5" set "CH_AGAIN=1"
if "%HTTP%"=="000" for %%C in (7 18 28 35 52 55 56) do if "%CRC%"=="%%C" set "CH_AGAIN=1"
if not defined CH_AGAIN goto :curl_slow
set /a CH_WAIT=CH_ROUND+1
ping -n %CH_WAIT% 127.0.0.1 >nul
set /a CH_ROUND+=1
goto :curl_round
:curl_slow
rem every address only ran out of its 4 s: a slow link (not a dead address), given the caller's own time once
if not defined CH_SLOW exit /b 0
for %%A in (%CH_LIST%) do if not defined CH_FIRST set "CH_FIRST=%%A"
set "CH_R=%CH_FIRST%"
if not "%CH_FIRST::=%"=="%CH_FIRST%" set "CH_R=[%CH_FIRST%]"
call :curl_once --retry 0 --resolve "%CH_HOST%:%CH_PORT%:%CH_R%"
if "%CRC%"=="0" set "CH_MEMO=%CH_MEMO% %CH_HOST%#%CH_FIRST%"
set "CH_FIRST="
exit /b 0
:curl_plain
set /a CH_RETRY=CH_TRIES-1
call :curl_once --retry %CH_RETRY%
exit /b 0

:curl_address
rem %1 one address. CH_DONE once it answered (and it is remembered); not when it did not connect or finish the handshake.
set "CH_A=%~1"
set "CH_R=%~1"
if not "%CH_A::=%"=="%CH_A%" set "CH_R=[%~1]"
call :curl_once --retry 0 --connect-timeout 4 --resolve "%CH_HOST%:%CH_PORT%:%CH_R%"
if not "%CRC%"=="28" set "CH_SLOW="
if not "%CRC%"=="0" if "%HTTP%"=="000" if "%CH_APPC%"=="0.000000" for %%C in (7 28 35 52 55 56) do if "%CRC%"=="%%C" exit /b 0
set "CH_SLOW="
set "CH_DONE=1"
set "CH_MEMO=%CH_MEMO% %CH_HOST%#%CH_A%"
exit /b 0

:curl_once
rem one curl: CURL_ARGS, then this call's own options, the status line and the URL
set "HTTP=000"
set "CH_APPC="
if defined CURL_BODY goto :curl_once_body
"%CURL%" %CURL_ARGS% %* -w "%%{http_code} %%{time_appconnect}" "%CH_URL%" >"%WORK%\http.txt" 2>>"%CURL_ERR%"
set "CRC=%ERRORLEVEL%"
goto :curl_once_read
:curl_once_body
echo %CURL_BODY%| "%CURL%" %CURL_ARGS% %* --data-binary @- -w "%%{http_code} %%{time_appconnect}" "%CH_URL%" >"%WORK%\http.txt" 2>>"%CURL_ERR%"
set "CRC=%ERRORLEVEL%"
:curl_once_read
for /f "usebackq tokens=1,2" %%P in ("%WORK%\http.txt") do set "HTTP=%%P" & set "CH_APPC=%%Q"
exit /b 0

:host_addresses
rem %1 host: CH_ADDRS = its addresses, IPv4 first, from the inbox nslookup; empty when it lists none. nslookup's labels
rem are in the system's language, so the answer is found by the name itself: the addresses after the line that names it.
set "CH_V4="
set "CH_V6="
set "CH_IN="
for /f "tokens=1,2" %%P in ('%SYS32%\nslookup.exe %~1 2^>nul') do call :address_line "%~1" "%%P" "%%Q"
set "CH_ADDRS=%CH_V4% %CH_V6%"
if "%CH_ADDRS%"==" " set "CH_ADDRS="
exit /b 0
:address_line
rem %1 host, %2 and %3 the line's first two words
if /i "%~3"=="%~1" set "CH_IN=1" & exit /b 0
if /i "%~3"=="%~1." set "CH_IN=1" & exit /b 0
if not defined CH_IN exit /b 0
call :address_word "%~2"
call :address_word "%~3"
exit /b 0
:address_word
if "%~1"=="" exit /b 0
echo %~1| "%SYS32%\findstr.exe" /r /x "[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*" >nul && set "CH_V4=%CH_V4% %~1" && exit /b 0
echo %~1| "%SYS32%\findstr.exe" /r /x /i "[0-9a-f:]*:[0-9a-f:]*" >nul && set "CH_V6=%CH_V6% %~1"
exit /b 0

:fetch_checked
rem %1 url, %2 file, %3 expected sha256. A damaged download (a flaky network) is fetched afresh, three times at most.
rem Errorlevel 0 when it matches, 1 when the download failed, 2 when it arrived damaged three times.
set "FTRY=0"
:fetch_checked_again
set /a FTRY+=1
call :fetch "%~1" "%~2"
if errorlevel 1 exit /b 1
call :check "%~2" "%~3"
if not errorlevel 1 exit /b 0
if %FTRY% geq 3 exit /b 2
echo theodore-install: %WHAT% arrived damaged; downloading it again
ping -n 3 127.0.0.1 >nul
goto :fetch_checked_again

:check
rem %1 file, %2 expected sha256. Errorlevel 0 when they match; the file is deleted when they do not.
set "HASH="
for /f "skip=1 tokens=* delims=" %%H in ('%SYS32%\certutil.exe -hashfile "%~1" SHA256 2^>nul') do if not defined HASH set "HASH=%%H"
if defined HASH set "HASH=%HASH: =%"
if /i "%HASH%"=="%~2" exit /b 0
del /q "%~1" >nul 2>&1
exit /b 1

:finish
if defined WORK if exist "%WORK%" rmdir /s /q "%WORK%" >nul 2>&1
exit /b %RC%

:welcome
rem The client's welcome as the last status (lane ONBOARD), only when every assistant was registered. Sets RC and
rem returns 0 when it printed one; 1 leaves the ending to the caller. Its answer is 0 when ready, and 67 only for the
rem still-downloading form; any other answer (a launcher that says 67 for a ready set-up included) leaves the ending as
rem it was. The installer itself exits 0 for both: a download still going on in the background, after every step of
rem the installer succeeded, is not a failure (a host shows any other exit code as a failed command).
if not "%HOSTS_RC%"=="0" exit /b 1
call "%%T%%" welcome %WELCOME_ARG% >"%WORK%\welcome.txt"
set "WRC=%ERRORLEVEL%"
if "%WRC%"=="0" goto :welcome_given
if not "%WRC%"=="67" exit /b 1
if not "%WELCOME_ARG%"=="--downloading" exit /b 1
:welcome_given
type "%WORK%\welcome.txt"
set "RC=0"
exit /b 0

rem =====================================================================================================================
rem Statuses (three lines each, then the exit code)
rem =====================================================================================================================

:uninstall_left
echo THEODORE RETRY: some of Theodore could not be deleted yet (a program may still be using it): %LEFTOVER:~2%.
echo NEXT: close the assistant and any Theodore window, then run the same command again.
echo TELL THE USER: A few of Theodore's files are still in use; I will remove them once the assistant is closed.
exit /b 75

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

:no_keys
echo THEODORE INTERNAL: this installer carries no release signing key (it was built without its keys), so it cannot trust any download.
echo NEXT: do not retry. Tell the user to download the installer again from https://theodoreand.ai/install.
echo TELL THE USER: This copy of the Theodore installer is incomplete. Please download it again from theodoreand.ai/install.
exit /b 78

:no_curl
echo THEODORE UNSUPPORTED_PLATFORM: this Windows has no curl.exe or tar.exe, or is not 64-bit; Theodore needs 64-bit Windows 10 version 1803 or later.
echo NEXT: do not retry. Register the plugin directly instead (step 2 of the install page).
echo TELL THE USER: Theodore's installer needs Windows 10 version 1803 or later.
exit /b 69

:not_writable
if defined CLEANUP_BUSY goto :cleanup_busy
echo THEODORE SETUP_FAILED: cause DISK: Theodore's folder is not writable.
echo NEXT: free disk space or set THEODORE_HOME to a writable folder, then run the installer again.
echo TELL THE USER: Theodore could not write its files. The disk may be full or the folder protected.
set "RC=68"
goto :finish

:cleanup_busy
rem The uninstall's cleanup did not answer within a minute of this installer stopping it: the files it could not
rem write are most likely still being removed, not a full or protected disk.
echo THEODORE RETRY: an uninstall that ran just before is still removing Theodore's previous files in "%HOME_DIR%", so this installation could not write there yet.
echo NEXT: run the same installer command again in a minute.
echo TELL THE USER: Theodore is still finishing the removal of its previous installation; I will run the installer again in a minute.
set "RC=75"
goto :finish

:download_failed
echo THEODORE SETUP_FAILED: cause NETWORK: %WHAT% could not be downloaded from %DL_BASE% (%DETAIL%).
echo NEXT: if the download host is blocked, register the plugin directly instead (step 2 of the install page); otherwise check the connection and run the installer again.
echo TELL THE USER: Theodore's installer could not download its files. I will try the direct route.
set "RC=68"
goto :finish

:hash_failed
echo THEODORE SETUP_FAILED: cause HASH_MISMATCH: %WHAT% did not match the SHA-256 this installer carries.
echo NEXT: run the installer again; if it fails twice, tell the user to write to support@theodoreand.ai.
echo TELL THE USER: A Theodore download arrived damaged. I am trying again.
set "RC=68"
goto :finish

:no_launcher
echo THEODORE INTERNAL: the plugin archive has no launcher.
echo NEXT: tell the user to write to support@theodoreand.ai.
echo TELL THE USER: Theodore's download is incomplete.
set "RC=78"
goto :finish

:old_engine_caches
rem The engine's caches before 1.0.0-rc.7 (the client's _remove_old_engine_caches): %USERPROFILE%\.cache\pptx-slide-raster
rem (images of the user's slides), pptx-icons and pptx-flags beside it, and %TEMP%\pptx-icons. Only files of the names
rem the engine writes go (names listed by dir and filtered by findstr, never echoed); a folder goes once nothing else is
rem in it. Results in OCLEFT (what stays) and OCDONE (the folders removed).
set "OCLEFT="
set "OCDONE="
if not defined USERPROFILE exit /b 0
set "OCR=%USERPROFILE%\.cache\pptx-slide-raster"
if exist "%OCR%\" for /f "delims=" %%S in ('dir /b /ad "%OCR%" 2^>nul ^| "%SYS32%\findstr.exe" /r /i /x "[0-9a-f]*"') do call :old_raster_dir "%OCR%\%%S"
call :old_cache_dir "%OCR%"
call :old_icon_files "%USERPROFILE%\.cache\pptx-icons"
call :old_icon_files "%USERPROFILE%\.cache\pptx-flags"
if defined TEMP call :old_icon_files "%TEMP%\pptx-icons"
exit /b 0

:old_raster_dir
for /f "delims=" %%N in ('dir /b /a-d "%~1" 2^>nul ^| "%SYS32%\findstr.exe" /r /i /x "slide-[0-9]*-[0-9]*\.jpg"') do del /f /q "%~1\%%N" >nul 2>&1
rmdir "%~1" >nul 2>&1
exit /b 0

:old_icon_files
if not exist "%~1\" exit /b 0
for /f "delims=" %%N in ('dir /b /a-d "%~1" 2^>nul ^| "%SYS32%\findstr.exe" /r /i /x "[0-9a-f]*\.png [0-9a-f]*\.uri"') do del /f /q "%~1\%%N" >nul 2>&1
call :old_cache_dir "%~1"
exit /b 0

:old_cache_dir
if not exist "%~1\" exit /b 0
rmdir "%~1" >nul 2>&1
if exist "%~1\" set "OCLEFT=%OCLEFT%; the folder %~1, a cache of the engine before 1.0.0-rc.7: its files of the engine's were removed, and what else it holds is not Theodore's and was left"
if not exist "%~1\" set "OCDONE=%OCDONE%, %~1"
exit /b 0
