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
set "ARCH="
if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" set "ARCH=x64"
if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "ARCH=x64"
rem Windows on ARM runs the x64 build through the system's emulation (a native build is planned).
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=x64"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "ARCH=x64"
if not defined ARCH goto :unsupported
if not exist "%SYS32%\tar.exe" goto :unsupported
if not exist "%SYS32%\certutil.exe" goto :unsupported

call :job_alive
if errorlevel 1 if exist "%B%\failed" goto :failed_status
call :job_alive
if not errorlevel 1 goto :progress_status

if not exist "%B%" mkdir "%B%" 2>nul
( >"%B%\.write-test" echo x ) 2>nul
if not exist "%B%\.write-test" goto :not_writable
del /q "%B%\.write-test" >nul 2>&1

if not defined THEODORE_EPHEMERAL goto :start_job
call :jget stage1_client_url "%CONF%"
"%SYS32%\curl.exe" -sS -I -L --connect-timeout 5 -m 10 -o nul "%JV%" >nul 2>&1
if errorlevel 1 goto :connector_required

:start_job
if not exist "%HOME_DIR%\logs" mkdir "%HOME_DIR%\logs" 2>nul
rem The job must not inherit this command's output: a caller that reads to the end of our output (every assistant
rem does) would otherwise wait for the whole download. "start /b" passes every inheritable handle on; PowerShell's
rem Start-Process goes through ShellExecute, which passes none. "start /b" stays as the fallback.
set "THEODORE_JOB_LAUNCHER=%LAUNCHER%"
set "PWSH=%SYS32%\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PWSH%" goto :start_plain
"%PWSH%" -NoProfile -NonInteractive -Command "Start-Process -WindowStyle Hidden -FilePath $env:THEODORE_JOB_LAUNCHER -ArgumentList __bootstrap" <nul >nul 2>&1
if not errorlevel 1 goto :started
:start_plain
start "" /b cmd /d /c ""%LAUNCHER%" __bootstrap" <nul >nul 2>&1
:started
set /a WAITED=0
set /a LIMIT=20
if defined THEODORE_BOOTSTRAP_WAIT set /a LIMIT=THEODORE_BOOTSTRAP_WAIT
:wait_loop
if %WAITED% GEQ %LIMIT% goto :wait_done
"%SYS32%\ping.exe" -n 2 127.0.0.1 >nul 2>&1
set /a WAITED+=1
call :check_ready
if defined READY goto :run_client
if not exist "%B%\failed" goto :wait_loop
call :job_alive
if errorlevel 1 goto :failed_status
goto :wait_loop
:wait_done
call :check_ready
if defined READY goto :run_client
goto :progress_status

:run_client
set "THEODORE_HOME=%HOME_DIR%"
set "THEODORE_LAUNCHER=%LAUNCHER%"
"%PY%" -I "%HOME_DIR%\client\%CV%" %* & call exit /b %%ERRORLEVEL%%

:handover
"%STABLE%" %*

rem =====================================================================================================================
rem Subroutines
rem =====================================================================================================================

:jget
rem %1 key, %2 file. launcher.json is flat on purpose: one "key": value per line. Result in JV.
set "JV="
for /f "usebackq tokens=1,* delims=:" %%A in (`%SYS32%\findstr.exe /c:"\"%~1\"" "%~2" 2^>nul`) do if not defined JV set "JV=%%B"
if not defined JV exit /b 0
set "JV=%JV:"=%"
if "%JV:~-1%"=="," set "JV=%JV:~0,-1%"
if "%JV:~0,1%"==" " set "JV=%JV:~1%"
exit /b 0

:self_copy
if not exist "%HOME_DIR%\bin" mkdir "%HOME_DIR%\bin" 2>nul
if not exist "%HOME_DIR%\bin" exit /b 0
for %%F in (theodore theodore.cmd launcher.json) do if exist "%SELF_DIR%\%%F" copy /y "%SELF_DIR%\%%F" "%HOME_DIR%\bin\%%F" >nul 2>&1
exit /b 0

:check_ready
set "READY="
set "RT="
set "CV="
if not exist "%HOME_DIR%\runtime\current.txt" exit /b 0
if not exist "%HOME_DIR%\client\current.txt" exit /b 0
set /p RT=<"%HOME_DIR%\runtime\current.txt"
set /p CV=<"%HOME_DIR%\client\current.txt"
if not defined RT exit /b 0
if not defined CV exit /b 0
set "PY=%HOME_DIR%\runtime\%RT%\python\python.exe"
if not exist "%PY%" exit /b 0
if not exist "%HOME_DIR%\client\%CV%\__main__.py" exit /b 0
set "READY=1"
exit /b 0

:job_alive
rem Errorlevel 0 when a bootstrap job is running: the job keeps job.lock open, so a second writer is refused.
if not exist "%B%\job.lock" exit /b 1
2>nul ( >>"%B%\job.lock" (call ) ) && exit /b 1
exit /b 0

:sha256
rem %1 file. Result in HASH (lower or upper case, no spaces).
set "HASH="
for /f "skip=1 tokens=* delims=" %%H in ('%SYS32%\certutil.exe -hashfile "%~1" SHA256 2^>nul') do if not defined HASH set "HASH=%%H"
if defined HASH set "HASH=%HASH: =%"
exit /b 0

:fetch
rem %1 url, %2 destination. Errorlevel 0 on success; otherwise CAUSE and DETAIL are set.
set "CAUSE=NETWORK"
set "DETAIL=no download tool worked"
set "HTTP=000"
if not exist "%SYS32%\curl.exe" goto :fetch_powershell
"%SYS32%\curl.exe" -sS -L --fail --retry 2 --connect-timeout 20 -o "%~2" -w "%%{http_code}" "%~1" >"%B%\http.txt" 2>"%B%\fetch.err"
set "RC=%ERRORLEVEL%"
set /p HTTP=<"%B%\http.txt"
if "%RC%"=="0" exit /b 0
set "DETAIL=curl exit %RC%, HTTP %HTTP%"
if "%RC%"=="23" set "CAUSE=DISK"
if "%RC%"=="23" exit /b 1
for %%C in (35 51 53 58 59 60 77 83 90 91) do if "%RC%"=="%%C" set "CAUSE=TLS_INTERCEPT"
if "%HTTP%"=="407" set "CAUSE=PROXY_AUTH"
"%SYS32%\findstr.exe" /c:"407" "%B%\fetch.err" >nul 2>&1 && set "CAUSE=PROXY_AUTH"
:fetch_powershell
rem Second try: Windows' own HTTP stack with the signed-in user's proxy credentials.
set "PWSH=%SYS32%\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PWSH%" goto :fetch_bits
"%PWSH%" -NoProfile -NonInteractive -Command "$ProgressPreference='SilentlyContinue'; try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -ProxyUseDefaultCredentials -Uri $env:THEODORE_FETCH_URL -OutFile $env:THEODORE_FETCH_OUT; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 if exist "%~2" exit /b 0
exit /b 1
:fetch_bits
rem Only where PowerShell is missing: the Background Intelligent Transfer Service. BITS retries a transient error
rem for days, so the job gets 60 seconds without progress and 30 minutes in all, then is cancelled.
if not exist "%SYS32%\bitsadmin.exe" exit /b 1
set "BJ=theodore-setup-%RANDOM%%RANDOM%"
"%SYS32%\bitsadmin.exe" /create /download %BJ% >nul 2>&1 || exit /b 1
"%SYS32%\bitsadmin.exe" /addfile %BJ% "%~1" "%~2" >nul 2>&1
"%SYS32%\bitsadmin.exe" /setnoprogresstimeout %BJ% 60 >nul 2>&1
"%SYS32%\bitsadmin.exe" /setpriority %BJ% FOREGROUND >nul 2>&1
"%SYS32%\bitsadmin.exe" /resume %BJ% >nul 2>&1
set /a BITS_LEFT=1800
:bits_wait
set "BSTATE="
for /f "delims=" %%S in ('%SYS32%\bitsadmin.exe /rawreturn /getstate %BJ% 2^>nul') do set "BSTATE=%%S"
if /i "%BSTATE%"=="TRANSFERRED" goto :bits_done
if /i "%BSTATE%"=="ERROR" goto :bits_cancel
if /i "%BSTATE%"=="CANCELLED" goto :bits_cancel
if not defined BSTATE goto :bits_cancel
set /a BITS_LEFT-=1
if %BITS_LEFT% LEQ 0 goto :bits_cancel
"%SYS32%\ping.exe" -n 2 127.0.0.1 >nul 2>&1
goto :bits_wait
:bits_done
"%SYS32%\bitsadmin.exe" /complete %BJ% >nul 2>&1
if exist "%~2" exit /b 0
exit /b 1
:bits_cancel
"%SYS32%\bitsadmin.exe" /cancel %BJ% >nul 2>&1
exit /b 1

:fail
rem %1 cause; DETAIL holds the detail. Writes state\bootstrap\failed (cause on line 1, detail on line 2).
>"%B%\failed" echo %~1
>>"%B%\failed" echo %DETAIL%
del /q "%B%\phase" >nul 2>&1
exit /b 1

:bootstrap_entry
if not exist "%B%" mkdir "%B%" 2>nul
if not exist "%B%" exit /b 1
if not exist "%HOME_DIR%\logs" mkdir "%HOME_DIR%\logs" 2>nul
rem The job writes its own log: it is started with no output of its caller's.
call :bootstrap_main >>"%HOME_DIR%\logs\bootstrap.log" 2>&1
exit /b %ERRORLEVEL%
:bootstrap_main
set "ARCH=x64"
call :bootstrap_job 9>"%B%\job.lock"
if errorlevel 1 exit /b 1
del /q "%B%\job.lock" >nul 2>&1
rem Stage 2 (the rest of the runtime, checked against the signed manifest) is the client's job.
call :check_ready
if not defined READY exit /b 1
set "THEODORE_HOME=%HOME_DIR%"
set "THEODORE_LAUNCHER=%LAUNCHER%"
"%PY%" -I "%HOME_DIR%\client\%CV%" setup --job & call exit /b %%ERRORLEVEL%%

:bootstrap_job
for %%D in (staging runtime client logs) do if not exist "%HOME_DIR%\%%D" mkdir "%HOME_DIR%\%%D" 2>nul
del /q "%B%\failed" >nul 2>&1
call :jget stage1_python_win32_%ARCH%_url "%CONF%"
set "PY_URL=%JV%"
call :jget stage1_python_win32_%ARCH%_sha256 "%CONF%"
set "PY_SHA=%JV%"
call :jget stage1_runtime_id "%CONF%"
set "S_RT=%JV%"
call :jget stage1_client_version "%CONF%"
set "S_CV=%JV%"
call :jget stage1_client_url "%CONF%"
set "C_URL=%JV%"
call :jget stage1_client_sha256 "%CONF%"
set "C_SHA=%JV%"
set "DETAIL=launcher.json has no stage-1 entry for win32-%ARCH%"
if not defined PY_URL goto :job_internal
if not defined PY_SHA goto :job_internal
if not defined S_RT goto :job_internal
if not defined S_CV goto :job_internal
if not defined C_URL goto :job_internal
if not defined C_SHA goto :job_internal
set "RT_DIR=%HOME_DIR%\runtime\%S_RT%"
set "PY_TAR=%HOME_DIR%\staging\stage1-python.tar.gz"
set "C_TAR=%HOME_DIR%\staging\stage1-client.tar"
if exist "%RT_DIR%\python\python.exe" goto :job_client
>"%B%\phase" echo download
set "THEODORE_FETCH_URL=%PY_URL%"
set "THEODORE_FETCH_OUT=%PY_TAR%"
set "WHAT=the Python runtime"
call :fetch "%PY_URL%" "%PY_TAR%"
if errorlevel 1 goto :job_fail_cause
>"%B%\phase" echo verify
call :sha256 "%PY_TAR%"
set "DETAIL=the Python runtime did not match its pinned checksum"
if /i not "%HASH%"=="%PY_SHA%" del /q "%PY_TAR%" >nul 2>&1
if /i not "%HASH%"=="%PY_SHA%" goto :job_hash
>"%B%\phase" echo unpack
if exist "%RT_DIR%.stage1" rmdir /s /q "%RT_DIR%.stage1" >nul 2>&1
mkdir "%RT_DIR%.stage1" 2>nul
if not exist "%RT_DIR%\.parts" mkdir "%RT_DIR%\.parts" 2>nul
set "DETAIL=the Python runtime could not be unpacked into the runtime folder"
"%SYS32%\tar.exe" -xzf "%PY_TAR%" -C "%RT_DIR%.stage1" 2>>"%B%\fetch.err"
if errorlevel 1 goto :job_disk
if exist "%RT_DIR%\python" rmdir /s /q "%RT_DIR%\python" >nul 2>&1
move /y "%RT_DIR%.stage1\python" "%RT_DIR%\python" >nul 2>&1
if not exist "%RT_DIR%\python\python.exe" goto :job_disk
>"%RT_DIR%\.parts\python" echo %PY_SHA%
rmdir /s /q "%RT_DIR%.stage1" >nul 2>&1
del /q "%PY_TAR%" >nul 2>&1
:job_client
if exist "%HOME_DIR%\client\%S_CV%\__main__.py" goto :job_pointers
>"%B%\phase" echo client
set "THEODORE_FETCH_URL=%C_URL%"
set "THEODORE_FETCH_OUT=%C_TAR%"
set "WHAT=the Theodore client"
call :fetch "%C_URL%" "%C_TAR%"
if errorlevel 1 goto :job_fail_cause
call :sha256 "%C_TAR%"
set "DETAIL=the Theodore client did not match its pinned checksum"
if /i not "%HASH%"=="%C_SHA%" del /q "%C_TAR%" >nul 2>&1
if /i not "%HASH%"=="%C_SHA%" goto :job_hash
if exist "%HOME_DIR%\client\%S_CV%.partial" rmdir /s /q "%HOME_DIR%\client\%S_CV%.partial" >nul 2>&1
mkdir "%HOME_DIR%\client\%S_CV%.partial" 2>nul
set "DETAIL=the Theodore client could not be unpacked into the client folder"
"%SYS32%\tar.exe" -xf "%C_TAR%" -C "%HOME_DIR%\client\%S_CV%.partial" 2>>"%B%\fetch.err"
if errorlevel 1 goto :job_disk
if exist "%HOME_DIR%\client\%S_CV%" rmdir /s /q "%HOME_DIR%\client\%S_CV%" >nul 2>&1
move /y "%HOME_DIR%\client\%S_CV%.partial\%S_CV%" "%HOME_DIR%\client\%S_CV%" >nul 2>&1
if not exist "%HOME_DIR%\client\%S_CV%\__main__.py" goto :job_disk
rmdir /s /q "%HOME_DIR%\client\%S_CV%.partial" >nul 2>&1
del /q "%C_TAR%" >nul 2>&1
:job_pointers
if not exist "%HOME_DIR%\runtime\current.txt" >"%HOME_DIR%\runtime\current.txt" echo %S_RT%
if not exist "%HOME_DIR%\client\current.txt" >"%HOME_DIR%\client\current.txt" echo %S_CV%
del /q "%B%\phase" "%B%\fetch.err" "%B%\http.txt" >nul 2>&1
exit /b 0
:job_internal
call :fail INTERNAL
exit /b 1
:job_fail_cause
rem In a .cmd file SET resets ERRORLEVEL, so the cause is decided by labels, never by a second errorlevel test.
set "DETAIL=%WHAT% could not be downloaded, %DETAIL%"
call :fail %CAUSE%
exit /b 1
:job_hash
call :fail HASH_MISMATCH
exit /b 1
:job_disk
call :fail DISK
exit /b 1

rem =====================================================================================================================
rem Statuses (three lines each, then the exit code)
rem =====================================================================================================================

:no_home
echo THEODORE INTERNAL: neither THEODORE_HOME nor LOCALAPPDATA is set.
echo NEXT: set THEODORE_HOME to a folder the user can write to and run the same command again.
echo TELL THE USER: Theodore could not find your user folder.
exit /b 78

:no_conf
echo THEODORE INTERNAL: launcher.json is missing beside the launcher.
echo NEXT: reinstall the Theodore plugin, then run the same command again.
echo TELL THE USER: Theodore's plugin files are incomplete; it needs to be reinstalled.
exit /b 78

:unsupported
echo THEODORE UNSUPPORTED_PLATFORM: Theodore's engine has no build for this Windows (%PROCESSOR_ARCHITECTURE%); it needs 64-bit Windows 10 version 1803 or later.
echo NEXT: do not retry. Use the deck-design skill with the host's own slide tools, or the Theodore connector if one is connected.
echo TELL THE USER: Theodore's PowerPoint engine does not run on this kind of computer yet (it supports macOS, Windows x64 and Linux x64 or arm64 with glibc).
exit /b 69

:not_writable
setlocal EnableDelayedExpansion
echo THEODORE SETUP_FAILED: cause DISK: !HOME_DIR! is not writable.
echo NEXT: make !HOME_DIR! writable (or set THEODORE_HOME to a writable folder), then run the same command again.
echo TELL THE USER: Theodore could not create its folder. It may be protected.
exit /b 68

:connector_required
echo THEODORE CONNECTOR_REQUIRED: this environment cannot reach Theodore's download host and does not keep files between sessions.
echo NEXT: call the Theodore connector's guide tool instead (skill deck-design). If no Theodore connector is available, say so; do not retry this command here.
echo TELL THE USER: In this environment Theodore works through its connector. You can connect it at Customize ^> Plugins ^> Theodore ^> Connectors.
exit /b 74

:progress_status
set "PHASE=starting"
if exist "%B%\phase" set /p PHASE=<"%B%\phase"
call :jget stage1_python_win32_%ARCH%_size "%CONF%"
set /a TOTAL_K=JV/1024
set /a HAVE_K=0
for %%S in ("%HOME_DIR%\staging\stage1-python.tar.gz") do if exist "%%~S" set /a HAVE_K=%%~zS/1024
set /a PCT=0
if %TOTAL_K% GTR 0 set /a PCT=HAVE_K*100/TOTAL_K
if %PCT% GTR 99 set /a PCT=99
if /i "%PHASE%"=="verify" set /a PCT=99
if /i "%PHASE%"=="unpack" set /a PCT=99
if /i "%PHASE%"=="client" set /a PCT=99
echo THEODORE SETUP_IN_PROGRESS: Theodore is downloading its runtime (first use on this computer, about 120 MB in all); the first part is at %PCT%%% (%PHASE%).
echo NEXT: run the same command again in 20 seconds. Do not use another slide tool meanwhile.
echo TELL THE USER: Theodore is setting itself up for the first time. This takes a few minutes; I will continue when it is ready.
exit /b 67

:failed_status
set "F_CAUSE="
set "F_DETAIL="
set /p F_CAUSE=<"%B%\failed"
for /f "usebackq skip=1 delims=" %%L in ("%B%\failed") do if not defined F_DETAIL set "F_DETAIL=%%L"
del /q "%B%\failed" >nul 2>&1
if /i "%F_CAUSE%"=="NETWORK" goto :failed_network
if /i "%F_CAUSE%"=="TLS_INTERCEPT" goto :failed_tls
if /i "%F_CAUSE%"=="PROXY_AUTH" goto :failed_proxy
if /i "%F_CAUSE%"=="DISK" goto :failed_disk
if /i "%F_CAUSE%"=="HASH_MISMATCH" goto :failed_hash
echo THEODORE INTERNAL: setup failed: %F_DETAIL%
echo NEXT: reinstall the Theodore plugin, then run the same command again.
echo TELL THE USER: Theodore's setup hit an internal error.
exit /b 78
:failed_network
echo THEODORE SETUP_FAILED: cause NETWORK: %F_DETAIL%.
echo NEXT: check the internet connection, then run the same command again.
echo TELL THE USER: Theodore could not download its files. Please check the internet connection.
exit /b 68
:failed_tls
echo THEODORE SETUP_FAILED: cause TLS_INTERCEPT: %F_DETAIL%.
echo NEXT: this network inspects HTTPS with a certificate this computer does not trust. Ask the user to have their IT team install the company root certificate, or to try another network; then run the same command again.
echo TELL THE USER: This network inspects secure connections and blocked Theodore's download. Your IT team can fix it by installing the company certificate.
exit /b 68
:failed_proxy
echo THEODORE SETUP_FAILED: cause PROXY_AUTH: %F_DETAIL%.
echo NEXT: the network proxy asks for a sign-in. Ask the user to sign in to the proxy (opening any website in a browser usually does it), then run the same command again.
echo TELL THE USER: The network proxy needs you to sign in before Theodore can download its files.
exit /b 68
:failed_disk
echo THEODORE SETUP_FAILED: cause DISK: %F_DETAIL%.
echo NEXT: free disk space or make Theodore's folder writable, then run the same command again.
echo TELL THE USER: Theodore could not write its files. The disk may be full or the folder protected.
exit /b 68
:failed_hash
echo THEODORE SETUP_FAILED: cause HASH_MISMATCH: %F_DETAIL%.
echo NEXT: run the same command again. If it fails twice, stop and tell the user to write to support@theodoreand.ai.
echo TELL THE USER: A Theodore download arrived damaged. I am trying again.
exit /b 68

:uninstall_bare
echo THEODORE OK: Theodore was not fully installed; its folders were removed.
echo NEXT: nothing to do.
echo TELL THE USER: Theodore is uninstalled.
rem Only Theodore's own subfolders: the folder may hold things that are not Theodore's. One line, because this
rem file may be inside one of them.
(for %%D in (client runtime engines packs tools state logs staging plugin bin) do rmdir /s /q "%HOME_DIR%\%%D" >nul 2>&1) & rmdir "%HOME_DIR%" >nul 2>&1 & exit /b 0
