# The Windows half of ci/run.sh, run over SSH from the Mac in C:\theodore-ci\repo (a copy of the tracked files).
# Args: <python-build-standalone release> <file> <sha256> of the pinned CPython for win32-x64 (from versions.json).
# Writes C:\theodore-ci\out\{shim,runtime}.log and .result and bundle\ (the runtime bundle to publish).
# The order follows the old workflow: the Windows test suites need the real bundle, so it is built first. Then the
# suites run side by side, each test once (see below); a 4th argument 'serial' runs them one after the other as before.
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
    & "$env:SystemRoot\System32\curl.exe" --proto =https -fsSL --retry 5 --retry-all-errors --retry-delay 2 -C - -o $archive "https://github.com/astral-sh/python-build-standalone/releases/download/$Release/$name"
    if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne $Sha256) { Result shim $false; Result runtime $false; exit 1 }
    $null = New-Item -ItemType Directory -Force -Path "$cacheBase\py"
    & "$env:SystemRoot\System32\tar.exe" -xzf $archive -C "$cacheBase\py"
}
# Short temporary paths; Git Bash's own folder after System32 (bash.exe only: its usr\bin tar.exe stays off PATH).
$lad0 = $env:LOCALAPPDATA; $ad0 = $env:APPDATA
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
} elseif (Test-Path "$out\bundle") {
    # Side by side, each test once: the launcher suite's tests in several groups, the installer suite, and the other
    # suites. On Windows both jobs ran the launcher and installer suites against the same bundle in the same
    # environment, so each now runs once and its result counts for both jobs. Every group is its own process
    # (started directly, not through the shell) with its own TEMP, LOCALAPPDATA and APPDATA; logs are joined in order.
    $lister = "$base\t\list_tests.py"
    Set-Content -Encoding ascii -Path $lister -Value @'
import os, sys, unittest
sys.path.insert(0, os.getcwd())  # the repository (a script's own folder is what Python puts first)
def ids(suite):
    for t in suite:
        yield from (ids(t) if isinstance(t, unittest.TestSuite) else [t.id()])
print("\n".join(ids(unittest.defaultTestLoader.loadTestsFromName(sys.argv[1]))))
'@
    $launcherIds = @(& "$env:SystemRoot\System32\cmd.exe" /d /s /c "`"`"$py`" `"$lister`" tests.test_launcher 2>nul`"" | Where-Object { $_ -match '^tests\.test_launcher\.[A-Za-z0-9_.]+$' })
    # (CPUs / 2) - 1 launcher groups: with 6 on 8 vCPU, the first-run tests' 60 s wait for the background stage 2
    # ran out under the load (4 failures); with 5 processes in all, none did.
    $n = [Math]::Max(1, [Math]::Floor([Environment]::ProcessorCount / 2) - 1)
    $groups = [ordered]@{}
    for ($i = 0; $i -lt $n; $i++) { $groups["launcher-$i"] = @() }
    for ($i = 0; $i -lt $launcherIds.Count; $i++) { $groups["launcher-$($i % $n)"] += $launcherIds[$i] }
    if (-not $launcherIds.Count) { $groups = [ordered]@{ 'launcher-0' = @('tests.test_launcher') } }  # could not list: the suite whole
    $groups['installer'] = @('tests.test_installer')
    $slow = @('tests.test_launcher', 'tests.test_installer')
    $groups['rest'] = @(Get-ChildItem "$repo\tests" -Filter 'test_*.py' | ForEach-Object { 'tests.' + $_.BaseName } | Where-Object { $slow -notcontains $_ } | Sort-Object)
    $shimSync = Step "$out\shim.log" "tools\sync_launchers.py --check"
    $procs = [ordered]@{}
    $t0 = Get-Date
    foreach ($g in $groups.Keys) {
        if (-not $groups[$g].Count) { continue }
        $own = "$base\p\$g"
        foreach ($d in 't', 'lad', 'ad') { $null = New-Item -ItemType Directory -Force -Path "$own\$d" }
        $env:TEMP = "$own\t"; $env:TMP = "$own\t"; $env:TMPDIR = "$own\t"; $env:LOCALAPPDATA = "$own\lad"; $env:APPDATA = "$own\ad"
        $procs[$g] = Start-Process -FilePath $py -ArgumentList (@('-m', 'unittest', '-v') + $groups[$g]) -WorkingDirectory $repo -NoNewWindow -PassThru `
            -RedirectStandardOutput "$out\$g.out" -RedirectStandardError "$out\$g.part"
        $null = $procs[$g].Handle  # kept, or Windows PowerShell 5.1 loses the exit code
    }
    $env:TEMP = "$base\t"; $env:TMP = "$base\t"; $env:TMPDIR = "$base\t"; $env:LOCALAPPDATA = $lad0; $env:APPDATA = $ad0
    foreach ($p in $procs.Values) { $p.WaitForExit() }
    $ok = @{}
    foreach ($g in $procs.Keys) {
        $ok[$g] = ($procs[$g].ExitCode -eq 0)
        $log = if ($g -eq 'rest') { "$out\shim.log" } else { "$out\runtime.log" }
        [IO.File]::AppendAllText($log, "===== $g ($($groups[$g].Count) names): exit $($procs[$g].ExitCode)`r`n")
        foreach ($part in "$out\$g.out", "$out\$g.part") {  # the program's own bytes, as Step keeps them
            if (-not (Test-Path $part)) { continue }
            $bytes = [IO.File]::ReadAllBytes($part); $fs = [IO.File]::Open($log, 'Append')
            try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Close() }
            Remove-Item $part
        }
    }
    [IO.File]::AppendAllText("$out\timing.txt", "suites side by side ($($procs.Count) processes): $([int]((Get-Date) - $t0).TotalSeconds) s`r`n")
    $suites = -not @($ok.Keys | Where-Object { $_ -ne 'rest' -and -not $ok[$_] }).Count
    [IO.File]::AppendAllText("$out\shim.log", "===== the launcher and installer suites ran once, in runtime.log: $(if ($suites) { 'pass' } else { 'fail' })`r`n")
    $runtime = $runtime -and $suites
    $shim = $shimSync -and $ok['rest'] -and $suites
    Remove-Item -Recurse -Force "$base\p" -ErrorAction SilentlyContinue
}
Result shim $shim
Result runtime $runtime
Remove-Item -Recurse -Force "$base\work", "$base\t" -ErrorAction SilentlyContinue
