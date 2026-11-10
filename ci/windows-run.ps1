# The Windows half of ci/run.sh, run over SSH from the Mac in C:\theodore-ci\repo (a copy of the tracked files).
# Args: <python-build-standalone release> <file> <sha256> of the pinned CPython for win32-x64 (from versions.json).
# Writes C:\theodore-ci\out\{shim,runtime}.log and .result and bundle\ (the runtime bundle to publish).
# The order follows the old workflow: the Windows test suites need the real bundle, so it is built first.
param([string]$Release, [string]$File, [string]$Sha256)
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$base = 'C:\theodore-ci'
$repo = "$base\repo"
$out = "$base\out"
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
$null = New-Item -ItemType Directory -Force -Path $out, "$base\cache", "$base\t"
function Result([string]$job, [bool]$ok) { $(if ($ok) { 'pass' } else { 'fail' }) | Out-File -Encoding ascii "$out\$job.result" }
& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\ci\windows-prepare.ps1" 2>&1 | Out-File -Encoding utf8 "$out\prepare.log"
if ($LASTEXITCODE -ne 0) { Result shim $false; Result runtime $false; exit 1 }

# The pinned CPython, checked against its sha256 before it runs.
$py = "$base\py\python\python.exe"
if (-not (Test-Path $py)) {
    $archive = "$base\cache\$File"
    $name = [Uri]::EscapeDataString($File)
    & "$env:SystemRoot\System32\curl.exe" --proto =https -fsSL -o $archive "https://github.com/astral-sh/python-build-standalone/releases/download/$Release/$name"
    if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne $Sha256) { Result shim $false; Result runtime $false; exit 1 }
    $null = New-Item -ItemType Directory -Force -Path "$base\py"
    & "$env:SystemRoot\System32\tar.exe" -xzf $archive -C "$base\py"
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
$runtime = (Step "$out\runtime.log" "runtime\build\build_bundle.py --platform win32-x64 --out `"$out\bundle`" --work `"$base\work`" --cache `"$base\cache\bundle`"") -and
           (Step "$out\runtime.log" "runtime\build\smoke_test.py --bundle `"$out\bundle`" --platform win32-x64")
$env:THEODORE_TEST_BUNDLE = "$out\bundle"
if ($runtime) {
    $runtime = Step "$out\runtime.log" "-m unittest -v tests.test_launcher tests.test_installer"
}
$shim = $false
if (Test-Path "$out\bundle") {
    $shim = (Step "$out\shim.log" "tools\sync_launchers.py --check") -and (Step "$out\shim.log" "-m unittest discover -s tests -v")
}
Result shim $shim
Result runtime $runtime
Remove-Item -Recurse -Force "$base\work", "$base\t" -ErrorAction SilentlyContinue
