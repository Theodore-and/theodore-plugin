# Prepares a stock Windows (Server 2022 or Windows 10/11 x64) for ci/run.sh windows-x64. Idempotent.
# It installs only Git for Windows (for the Git Bash tests), pinned by version and sha256: nothing a user's
# machine would not have. curl.exe, tar.exe, certutil and PowerShell 5.1 ship with Windows. Run as an
# administrator, or let ci/windows-run.ps1 run it.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$version = '2.56.0.4'
$file = "Git-$version-64-bit.exe"
$url = "https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/$file"
$sha256 = '4961a4a8cc778d8b6e00a5819e5fe4d078b754555aff7aedc2d77a5d74c2de80'
$bash = 'C:\Program Files\Git\bin\bash.exe'
# A fresh server image fills its root store only on demand (CryptoAPI fetches a root when a chain needs it), and
# OpenSSL-based tools such as Python's urllib only see what is already there: load Microsoft's current root list
# once, with the inbox certutil, as a desktop that has browsed for a day already has it.
$roots = Join-Path $env:TEMP 'theodore-ci-roots.sst'
& "$env:SystemRoot\System32\certutil.exe" -generateSSTFromWU $roots | Out-Null
if ($LASTEXITCODE -eq 0) { Import-Certificate -FilePath $roots -CertStoreLocation Cert:\LocalMachine\Root | Out-Null; Remove-Item $roots }
if (Test-Path $bash) { Write-Output "ci: Git for Windows is installed ($bash)"; exit 0 }
$dir = Join-Path $env:TEMP ("theodore-ci-" + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $dir
try {
    $installer = Join-Path $dir $file
    & "$env:SystemRoot\System32\curl.exe" --proto =https -fsSL --retry 5 --retry-all-errors --retry-delay 2 -C - -o $installer $url
    if ($LASTEXITCODE -ne 0) { throw "download of $file failed" }
    $got = (Get-FileHash -Algorithm SHA256 $installer).Hash.ToLower()
    if ($got -ne $sha256) { throw "$file has sha256 $got, not the pinned $sha256" }
    $p = Start-Process -FilePath $installer -ArgumentList '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES' -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "the Git installer exited $($p.ExitCode)" }
    if (-not (Test-Path $bash)) { throw "Git for Windows did not install $bash" }
    Write-Output "ci: installed Git for Windows $version"
} finally {
    Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
}
