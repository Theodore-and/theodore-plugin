# The Windows half of ci/run.sh, run over SSH from the Mac in C:\theodore-ci\repo (a copy of the tracked files).
# Args: <python-build-standalone release> <file> <sha256> of the pinned CPython for win32-x64 (from versions.json).
# Writes C:\theodore-ci\out\{shim,runtime}.log and .result and bundle\ (the runtime bundle to publish).
# The order follows the old workflow: the Windows test suites need the real bundle, so it is built first. Then the
# suites run side by side, each in its own process with its own TEMP, LOCALAPPDATA and APPDATA: the runtime job's
# launcher and installer suites, and the shim job's sync check and every suite (one process for each of the two slow
# suites, one for the rest). A 4th argument 'serial' runs them one after the other as before.
# C:\theodore-ci-cache, when it exists (ci/run.sh makes it on a throwaway Azure machine), holds the pinned CPython and
# the bundle download cache instead of C:\theodore-ci: it outlives the run, so a prepared image (ci/azure-windows.sh
# capture) carries them (the CPython archive's sha256 was checked when it was put there).
param([string]$Release, [string]$File, [string]$Sha256, [string]$Mode = '')
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$base = 'C:\theodore-ci'
$repo = "$base\repo"
$out = "$base\out"
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
$keep = 'C:\theodore-ci-cache'
$cacheBase = if (Test-Path $keep) { $keep } else { $base }
$null = New-Item -ItemType Directory -Force -Path $out, "$cacheBase\cache", "$base\t"
function Result([string]$job, [bool]$ok) { $(if ($ok) { 'pass' } else { 'fail' }) | Out-File -Encoding ascii "$out\$job.result" }
& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\ci\windows-prepare.ps1" 2>&1 | Out-File -Encoding utf8 "$out\prepare.log"
if ($LASTEXITCODE -ne 0) { Result shim $false; Result runtime $false; exit 1 }

# The pinned CPython, checked against its sha256 before it runs.
$py = "$cacheBase\py\python\python.exe"
if (-not (Test-Path $py)) {
    $archive = "$cacheBase\cache\$File"
    $name = [Uri]::EscapeDataString($File)
    & "$env:SystemRoot\System32\curl.exe" --proto =https -fsSL -o $archive "https://github.com/astral-sh/python-build-standalone/releases/download/$Release/$name"
    if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne $Sha256) { Result shim $false; Result runtime $false; exit 1 }
    $null = New-Item -ItemType Directory -Force -Path "$cacheBase\py"
    & "$env:SystemRoot\System32\tar.exe" -xzf $archive -C "$cacheBase\py"
}
# Short temporary paths; Git Bash's own folder after System32 (bash.exe only: its usr\bin tar.exe stays off PATH).
$env:TEMP = "$base\t"; $env:TMP = "$base\t"; $env:TMPDIR = "$base\t"
$env:PATH = "$env:PATH;C:\Program Files\Git\bin"
$env:PYTHONDONTWRITEBYTECODE = '1'
Set-Location $repo

# Each step through cmd, so the log is the program's own bytes (PowerShell 5.1 would re-encode it as UTF-16).
function Step([string]$log, [string]$arguments) {
    & "$env:SystemRoot\System32\cmd.exe" /d /s /c "`"`"$py`" $arguments >> `"$log`" 2>&1`""
    return $LASTEXITCODE -eq 0
}
Remove-Item -Recurse -Force "$out\bundle", "$base\work" -ErrorAction SilentlyContinue
$runtime = (Step "$out\runtime.log" "runtime\build\build_bundle.py --platform win32-x64 --out `"$out\bundle`" --work `"$base\work`" --cache `"$cacheBase\cache\bundle`"") -and
           (Step "$out\runtime.log" "runtime\build\smoke_test.py --bundle `"$out\bundle`" --platform win32-x64")
$env:THEODORE_TEST_BUNDLE = "$out\bundle"
$shim = $false
if ($Mode -eq 'serial') {
    if ($runtime) {
        $runtime = Step "$out\runtime.log" "-m unittest -v tests.test_launcher tests.test_installer"
    }
    if (Test-Path "$out\bundle") {
        $shim = (Step "$out\shim.log" "tools\sync_launchers.py --check") -and (Step "$out\shim.log" "-m unittest discover -s tests -v")
    }
} else {
    # One process per group, each with its own temporary and application-data folders; the logs are joined in order.
    $slow = @('tests.test_launcher', 'tests.test_installer')
    $rest = @(Get-ChildItem "$repo\tests" -Filter 'test_*.py' | ForEach-Object { 'tests.' + $_.BaseName } | Where-Object { $slow -notcontains $_ } | Sort-Object)
    $groups = [ordered]@{}
    if ($runtime) { $groups['runtime-launcher'] = 'tests.test_launcher'; $groups['runtime-installer'] = 'tests.test_installer' }
    $bundled = Test-Path "$out\bundle"
    if ($bundled) {
        $groups['shim-launcher'] = 'tests.test_launcher'; $groups['shim-installer'] = 'tests.test_installer'
        $groups['shim-rest'] = $rest -join ' '
    }
    $shimSync = $bundled -and (Step "$out\shim.log" "tools\sync_launchers.py --check")
    $procs = [ordered]@{}
    $t0 = Get-Date
    foreach ($g in $groups.Keys) {
        $own = "$base\p\$g"
        foreach ($d in 't', 'lad', 'ad') { $null = New-Item -ItemType Directory -Force -Path "$own\$d" }
        $cmd = "set `"TEMP=$own\t`" & set `"TMP=$own\t`" & set `"TMPDIR=$own\t`" & set `"LOCALAPPDATA=$own\lad`" & set `"APPDATA=$own\ad`" & `"$py`" -m unittest -v $($groups[$g]) > `"$out\$g.part`" 2>&1"
        $procs[$g] = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList '/d', '/s', '/c', "`"$cmd`"" -WorkingDirectory $repo -WindowStyle Hidden -PassThru
    }
    foreach ($p in $procs.Values) { $p.WaitForExit() }
    $ok = @{}
    foreach ($g in $procs.Keys) {
        $ok[$g] = ($procs[$g].ExitCode -eq 0)
        $job = $g.Split('-')[0]
        [IO.File]::AppendAllText("$out\$job.log", "===== $g ($($groups[$g])): exit $($procs[$g].ExitCode)`r`n")
        if (Test-Path "$out\$g.part") {  # the program's own bytes, as Step keeps them
            $bytes = [IO.File]::ReadAllBytes("$out\$g.part"); $fs = [IO.File]::Open("$out\$job.log", 'Append')
            try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Close() }
            Remove-Item "$out\$g.part"
        }
    }
    [IO.File]::AppendAllText("$out\timing.txt", "suites side by side: $([int]((Get-Date) - $t0).TotalSeconds) s`r`n")
    if ($runtime) { $runtime = $ok['runtime-launcher'] -and $ok['runtime-installer'] }
    if ($bundled) { $shim = $shimSync -and $ok['shim-launcher'] -and $ok['shim-installer'] -and $ok['shim-rest'] }
    Remove-Item -Recurse -Force "$base\p" -ErrorAction SilentlyContinue
}
Result shim $shim
Result runtime $runtime
Remove-Item -Recurse -Force "$base\work", "$base\t" -ErrorAction SilentlyContinue
