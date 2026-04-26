#requires -Version 7
<#
.SYNOPSIS
    WinServerRAG installer build pipeline.

.DESCRIPTION
    Produces dist\WinServerRAG-Daemon-Installer-<VERSION>.exe by running:
      1. PyInstaller --onedir for 4 entry points (api, daemon, dbinit, backup)
      2. electron-builder --dir for the mini-monitor
      3. NSSM download (cached after first run)
      4. Inno Setup compile

    Run from the repo root:

        pwsh installer\build.ps1

    Or with switches:

        pwsh installer\build.ps1 -SkipPython     # only re-pack mini + installer
        pwsh installer\build.ps1 -SkipElectron   # only re-pack python + installer
        pwsh installer\build.ps1 -CleanFirst     # wipe dist/ + build/ first

    Prerequisites (one-time, on the build machine):
        - Python 3.12 with .venv set up (pip install -r requirements.txt && pip install pyinstaller)
        - Node.js 20+
        - Inno Setup 6 (https://jrsoftware.org/isdl.php) — installs to "C:\Program Files (x86)\Inno Setup 6\"

    Run output: dist\WinServerRAG-Daemon-Installer-<VERSION>.exe (~600MB).
#>

param(
    [switch]$SkipPython,
    [switch]$SkipElectron,
    [switch]$CleanFirst,
    [string]$Version = "1.2.0"
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

# Resolve repo root from this script location (installer/ → repo root).
$RepoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$InstallerDir = $PSScriptRoot
$DistDir    = Join-Path $RepoRoot "dist"
$BuildCache = Join-Path $RepoRoot "build"
$NssmDir    = Join-Path $InstallerDir "inno\assets"
$NssmExe    = Join-Path $NssmDir "nssm.exe"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# ISCC.exe lives in different places depending on whether the user
# installed Inno Setup machine-wide ($Program Files) or user-mode
# (LocalAppData via winget). Probe both.
$IsccCandidates = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
)
$IsccExe = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

function Section { param($msg) Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok      { param($msg) Write-Host "  [ok] $msg" -ForegroundColor Green }
function Fail    { param($msg) Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }

# ----------------------------------------------------------------------
# Step 0 — environment checks
# ----------------------------------------------------------------------
Section "Environment checks"

if (-not (Test-Path $VenvPython)) {
    Fail "$VenvPython not found. Run 'python -m venv .venv && .venv\Scripts\pip install -r requirements.txt && .venv\Scripts\pip install pyinstaller' first."
}
Ok "venv: $VenvPython"

if (-not $IsccExe) {
    Fail @"
Inno Setup 6 not found. Tried:
$(($IsccCandidates | ForEach-Object { "  - $_" }) -join "`n")

Install from https://jrsoftware.org/isdl.php, or:
    winget install --id JRSoftware.InnoSetup --silent
"@
}
Ok "Inno Setup: $IsccExe"

if (-not $SkipElectron) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) { Fail "node.js not on PATH. Install Node 20+ first." }
    Ok "node: $($node.Source)"
}

# ----------------------------------------------------------------------
# Step 1 — clean (optional)
# ----------------------------------------------------------------------
if ($CleanFirst) {
    Section "Clean"
    if (Test-Path $DistDir)    { Remove-Item -Recurse -Force $DistDir }
    if (Test-Path $BuildCache) { Remove-Item -Recurse -Force $BuildCache }
    Ok "wiped dist/ and build/"
}

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DistDir "exe") | Out-Null

# ----------------------------------------------------------------------
# Step 2 — NSSM bundle (cached)
# ----------------------------------------------------------------------
Section "NSSM"

if (-not (Test-Path $NssmExe)) {
    New-Item -ItemType Directory -Force -Path $NssmDir | Out-Null

    # Source 1: winget cache (preferred — nssm.cc has been flaky).
    $WingetCache = Get-ChildItem -ErrorAction SilentlyContinue -Recurse `
        -Path (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages") `
        -Filter "nssm.exe" |
        Where-Object { $_.FullName -match "win64" } |
        Select-Object -First 1

    if ($WingetCache) {
        Write-Host "  using NSSM from winget cache: $($WingetCache.FullName)"
        Copy-Item -Path $WingetCache.FullName -Destination $NssmExe -Force
    } else {
        # Source 2: try nssm.cc download. Falls through to a clear error if
        # the site is down (it occasionally returns 503).
        Write-Host "  no winget NSSM found — falling back to https://nssm.cc/release/..."
        $NssmZip = Join-Path $NssmDir "nssm.zip"
        try {
            Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $NssmZip -ErrorAction Stop
        } catch {
            Fail @"
NSSM download failed: $_

Install via winget instead and re-run:
    winget install --id NSSM.NSSM --silent
"@
        }
        Expand-Archive -Path $NssmZip -DestinationPath $NssmDir -Force
        Copy-Item -Path (Join-Path $NssmDir "nssm-2.24\win64\nssm.exe") -Destination $NssmExe -Force
        Remove-Item -Recurse -Force (Join-Path $NssmDir "nssm-2.24")
        Remove-Item -Force $NssmZip
    }
}
Ok "nssm.exe present ($([math]::Round((Get-Item $NssmExe).Length / 1KB)) KB)"

# ----------------------------------------------------------------------
# Step 3 — PyInstaller (4 specs)
# ----------------------------------------------------------------------
if (-not $SkipPython) {
    Section "PyInstaller"
    $Specs = @("api", "daemon", "dbinit", "backup")
    foreach ($name in $Specs) {
        $spec = Join-Path $InstallerDir "pyinstaller\$name.spec"
        Write-Host "  building $name.exe..."
        Push-Location $RepoRoot
        try {
            & $VenvPython -m PyInstaller --noconfirm `
                --distpath (Join-Path $DistDir "exe") `
                --workpath (Join-Path $BuildCache "pyinstaller-$name") `
                $spec 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed for $name" }
        } finally { Pop-Location }
        Ok "winserverrag-$name.exe built"
    }
}

# ----------------------------------------------------------------------
# Step 4 — Electron mini-monitor
# ----------------------------------------------------------------------
if (-not $SkipElectron) {
    Section "Electron mini-monitor"
    Push-Location (Join-Path $RepoRoot "desktop")
    try {
        if (-not (Test-Path "node_modules")) {
            Write-Host "  npm install..."
            npm install --no-audit --no-fund 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { Fail "npm install failed" }
        }
        # Make sure electron-builder is installed (added to devDeps in v1.2).
        $hasBuilder = (Test-Path "node_modules\electron-builder")
        if (-not $hasBuilder) {
            Write-Host "  installing electron-builder..."
            npm install --save-dev electron-builder@25 --no-audit --no-fund 2>&1 | Out-Host
        }
        Write-Host "  npm run pack..."
        npm run pack 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { Fail "electron-builder failed" }
    } finally { Pop-Location }
    Ok "mini-monitor packaged"
}

# ----------------------------------------------------------------------
# Step 5 — Inno Setup compile
# ----------------------------------------------------------------------
Section "Inno Setup"
$ISS = Join-Path $InstallerDir "inno\WinServerRAG.iss"
& $IsccExe "/DAppVersion=$Version" $ISS 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup compile failed" }

$Output = Get-ChildItem (Join-Path $DistDir "WinServerRAG-Daemon-Installer-*.exe") | Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $Output) { Fail "Installer not produced" }

Section "Done"
Write-Host "  Installer: $($Output.FullName)" -ForegroundColor Green
Write-Host "  Size:      $([math]::Round($Output.Length / 1MB, 1)) MB" -ForegroundColor Green
