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
$null = New-Item -ItemType Directory -Force -Path $out, "$base\cache", "$base\t"
& powershell -NoProfile -ExecutionPolicy Bypass -File "$repo\ci\windows-prepare.ps1" *> "$out\prepare.log"
if ($LASTEXITCODE -ne 0) { 'fail' | Set-Content "$out\shim.result"; 'fail' | Set-Content "$out\runtime.result"; exit 1 }

# The pinned CPython, checked against its sha256 before it runs.
$py = "$base\py\python\python.exe"
if (-not (Test-Path $py)) {
    $archive = "$base\cache\$File"
    $name = [Uri]::EscapeDataString($File)
    & "$env:SystemRoot\System32\curl.exe" --proto =https -fsSL -o $archive "https://github.com/astral-sh/python-build-standalone/releases/download/$Release/$name"
    if ((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower() -ne $Sha256) { 'fail' | Set-Content "$out\shim.result"; 'fail' | Set-Content "$out\runtime.result"; exit 1 }
    $null = New-Item -ItemType Directory -Force -Path "$base\py"
    & "$env:SystemRoot\System32\tar.exe" -xzf $archive -C "$base\py"
}
# Short temporary paths; Git Bash's own folder after System32 (bash.exe only: its usr\bin tar.exe stays off PATH).
$env:TEMP = "$base\t"; $env:TMP = "$base\t"; $env:TMPDIR = "$base\t"
$env:PATH = "$env:PATH;C:\Program Files\Git\bin"
$env:PYTHONDONTWRITEBYTECODE = '1'
Set-Location $repo

function Step([string]$log, [scriptblock]$body) {
    & $body *>> $log
    return $LASTEXITCODE -eq 0
}
Remove-Item -Recurse -Force "$out\bundle", "$base\work" -ErrorAction SilentlyContinue
$runtime = (Step "$out\runtime.log" { & $py runtime\build\build_bundle.py --platform win32-x64 --out "$out\bundle" --work "$base\work" --cache "$base\cache\bundle" }) -and
           (Step "$out\runtime.log" { & $py runtime\build\smoke_test.py --bundle "$out\bundle" --platform win32-x64 })
$env:THEODORE_TEST_BUNDLE = "$out\bundle"
if ($runtime) {
    $runtime = Step "$out\runtime.log" { & $py -m unittest -v tests.test_launcher tests.test_installer }
}
$shim = $false
if (Test-Path "$out\bundle") {
    $shim = (Step "$out\shim.log" { & $py tools\sync_launchers.py --check }) -and (Step "$out\shim.log" { & $py -m unittest discover -s tests -v })
}
$(if ($shim) { 'pass' } else { 'fail' }) | Set-Content "$out\shim.result"
$(if ($runtime) { 'pass' } else { 'fail' }) | Set-Content "$out\runtime.result"
Remove-Item -Recurse -Force "$base\work", "$base\t" -ErrorAction SilentlyContinue
