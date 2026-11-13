# Starts ci/windows-run.ps1 outside the SSH session (ci/run.sh windows-x64), so a dropped connection cannot stop it:
# OpenSSH for Windows ends every process of a session when the session ends, and a process created through WMI
# (Win32_Process.Create, as the same user) is not one of them. ci/run.sh then polls for out\runtime.result over short
# SSH connections and copies the results. Args: the same as windows-run.ps1 (the 4th, 'serial', is optional).
param([string]$Release, [string]$File, [string]$Sha256, [string]$Mode = '')
$ErrorActionPreference = 'Stop'
$run = 'C:\theodore-ci\repo\ci\windows-run.ps1'
# Started once: a retried SSH call after a connection that dropped late must not start a second run.
$marker = 'C:\theodore-ci\started'
if (Test-Path $marker) { Write-Output "ci: windows-run.ps1 was started already"; exit 0 }
$line = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$run`" $Release `"$File`" $Sha256 $Mode"
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $line; CurrentDirectory = 'C:\theodore-ci' }
if ($r.ReturnValue -ne 0) { Write-Output "ci: Win32_Process.Create returned $($r.ReturnValue)"; exit 1 }
Set-Content -Path $marker -Value $r.ProcessId -Encoding ascii
Write-Output "ci: windows-run.ps1 started, process $($r.ProcessId)"
