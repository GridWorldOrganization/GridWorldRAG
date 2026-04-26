#requires -Version 5
<#
.SYNOPSIS
    Register / unregister WinServerRAG-API and WinServerRAG-Daemon as
    NSSM-backed Windows services, with relaxed service ACLs so the
    mini-monitor can start them without UAC.

.DESCRIPTION
    Called in two contexts:
      1. Inno Setup installer [Run] block — `-InstallRoot ... -DataRoot ...`
      2. Mini-monitor "repair" path (future) — same args, idempotent

    Idempotent: every step either creates a new resource or updates the
    existing one in place. Re-running on a working install is a no-op.

    Why a separate PS1 (Codex review):
      - Inno Setup [Run] does not loop, so 25+ NSSM commands are a
        copy-paste mess that's hard to keep idempotent. Bash-style
        scripting is much cleaner here.
      - Future mini-monitor "repair" button can re-invoke this same
        script via UAC, single source of truth.

    Why the SDDL relaxation:
      - Default service DACL grants SERVICE_START to admins only.
      - We grant SERVICE_START + SERVICE_QUERY_STATUS to a local group
        `WinServerRAG Operators` (NOT Authenticated Users — Microsoft
        explicitly calls out granting service rights to AU as a common
        security mistake; it can include domain principals on a domain-
        joined machine).
      - SERVICE_STOP is NOT granted to the operator group. The
        mini-monitor's button never service-stops the daemon (it
        pauses via the API instead). Granting STOP would expand the
        attack surface for no functional benefit.

.PARAMETER InstallRoot
    The folder where the v1.2 installer placed bin\ (e.g.,
    "C:\Program Files\WinServerRAG"). The script expects:
      $InstallRoot\bin\nssm.exe
      $InstallRoot\bin\winserverrag-api\winserverrag-api.exe
      $InstallRoot\bin\winserverrag-daemon\winserverrag-daemon.exe

.PARAMETER DataRoot
    Where logs / backups / config live (e.g., "C:\ProgramData\WinServerRAG").
    Logs go to $DataRoot\logs\{api,daemon}.{out,err}.log.

.PARAMETER OperatorUser
    A user to add to the local "WinServerRAG Operators" group on
    install. Pass the original (non-elevated) user from Inno Setup's
    {username} constant. Defaults to $env:USERNAME if not provided
    (works when the script is run interactively as a normal admin).

.PARAMETER Uninstall
    If set, stop+remove both services and delete the local Operators
    group. Used by the installer's [UninstallRun] block.
#>

param(
    [Parameter(Mandatory=$true)] [string] $InstallRoot,
    [Parameter(Mandatory=$true)] [string] $DataRoot,
    [string] $OperatorUser = "",
    [switch] $Uninstall
)

$ErrorActionPreference = "Stop"

$ServiceApi    = "WinServerRAG-API"
$ServiceDaemon = "WinServerRAG-Daemon"
$OperatorGroup = "WinServerRAG Operators"

$NssmExe   = Join-Path $InstallRoot "bin\nssm.exe"
$ApiExe    = Join-Path $InstallRoot "bin\winserverrag-api\winserverrag-api.exe"
$DaemonExe = Join-Path $InstallRoot "bin\winserverrag-daemon\winserverrag-daemon.exe"
$ConfigDir = Join-Path $DataRoot "config"
$LogDir    = Join-Path $DataRoot "logs"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
function Log($msg) { Write-Host "[install-services] $msg" }

function Service-Exists($name) {
    $r = & sc.exe query $name 2>&1
    return $LASTEXITCODE -eq 0
}

function Group-Exists($name) {
    $r = & net.exe localgroup $name 2>&1
    return $LASTEXITCODE -eq 0
}

function Stop-IfRunning($name) {
    if (Service-Exists $name) {
        Log "Stopping $name (if running)..."
        & sc.exe stop $name 2>&1 | Out-Null
        # Best-effort: don't error if already stopped.
    }
}


# ---------------------------------------------------------------------
# Uninstall path
# ---------------------------------------------------------------------
if ($Uninstall) {
    Log "Uninstall mode."
    Stop-IfRunning $ServiceDaemon
    Stop-IfRunning $ServiceApi
    if (Service-Exists $ServiceDaemon) {
        Log "Removing $ServiceDaemon..."
        & $NssmExe remove $ServiceDaemon confirm 2>&1 | Out-Null
    }
    if (Service-Exists $ServiceApi) {
        Log "Removing $ServiceApi..."
        & $NssmExe remove $ServiceApi confirm 2>&1 | Out-Null
    }
    if (Group-Exists $OperatorGroup) {
        Log "Removing local group $OperatorGroup..."
        & net.exe localgroup $OperatorGroup /delete 2>&1 | Out-Null
    }
    Log "Uninstall complete."
    exit 0
}


# ---------------------------------------------------------------------
# Install / repair path
# ---------------------------------------------------------------------
Log "Install mode. InstallRoot=$InstallRoot DataRoot=$DataRoot"

# 1. Pre-flight — verify the binaries the installer dropped are reachable.
foreach ($p in @($NssmExe, $ApiExe, $DaemonExe)) {
    if (-not (Test-Path $p)) {
        throw "Required binary not found: $p (did the installer's [Files] section run first?)"
    }
}

# 2. Make sure the writable dirs exist.
foreach ($d in @($ConfigDir, $LogDir)) {
    if (-not (Test-Path $d)) {
        Log "Creating $d"
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
}

# 3. Local Operators group — create-if-missing, idempotent.
if (-not (Group-Exists $OperatorGroup)) {
    Log "Creating local group: $OperatorGroup"
    & net.exe localgroup $OperatorGroup /add /comment:"Members can start/query the WinServerRAG services without UAC." 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create local group $OperatorGroup (exit $LASTEXITCODE)"
    }
} else {
    Log "Local group already exists: $OperatorGroup"
}

# 4. Add the operator user (Inno Setup's {username}, or $env:USERNAME).
if (-not $OperatorUser) { $OperatorUser = $env:USERNAME }
if ($OperatorUser) {
    Log "Adding user '$OperatorUser' to '$OperatorGroup' (if not already)..."
    # `net localgroup ... /add` returns 2 if the user is already a member;
    # not an error in our context. Suppress.
    & net.exe localgroup $OperatorGroup $OperatorUser /add 2>&1 | Out-Null
}

# 5. Install API service — idempotent. If it exists, fall through to
#    `nssm set` calls below to re-apply the params (Codex: blind
#    `nssm install` on existing service mismatches the repair semantics).
function Install-NssmService($name, $exe) {
    if (Service-Exists $name) {
        Log "Service exists, will update params: $name"
    } else {
        Log "Installing service: $name -> $exe"
        & $NssmExe install $name $exe 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "nssm install $name failed: $LASTEXITCODE" }
    }
}

Install-NssmService $ServiceApi    $ApiExe
Install-NssmService $ServiceDaemon $DaemonExe

# 6. Apply / re-apply NSSM params for both services.
function Set-NssmParams($name, $appDir, $logBase, $env, $depends) {
    & $NssmExe set $name AppDirectory       $appDir         | Out-Null
    & $NssmExe set $name AppStdout          "$LogDir\$logBase.out.log" | Out-Null
    & $NssmExe set $name AppStderr          "$LogDir\$logBase.err.log" | Out-Null
    & $NssmExe set $name AppRotateFiles     1               | Out-Null
    & $NssmExe set $name AppRotateBytes     10485760        | Out-Null
    & $NssmExe set $name AppEnvironmentExtra "WINSERVERRAG_CONFIG_DIR=$ConfigDir" | Out-Null
    & $NssmExe set $name Start              SERVICE_AUTO_START | Out-Null
    if ($depends) {
        & $NssmExe set $name DependOnService $depends | Out-Null
    }
}

Set-NssmParams $ServiceApi    (Split-Path $ApiExe)    "api"    $ConfigDir $null
Set-NssmParams $ServiceDaemon (Split-Path $DaemonExe) "daemon" $ConfigDir $ServiceApi

# 7. Set descriptions (cosmetic, services.msc).
& $NssmExe set $ServiceApi    Description "WinServerRAG control API + web monitor (FastAPI on :17600)" | Out-Null
& $NssmExe set $ServiceDaemon Description "WinServerRAG indexer daemon (rag_daemon multi-threaded)"   | Out-Null

# 8. SDDL relaxation — the v1.3 piece. Grant SERVICE_QUERY_STATUS +
#    SERVICE_START to the Operators group, NOT to Authenticated Users.
#
# The default DACL we want to keep:
#   D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)         SYSTEM full
#   (A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)   Builtin\Administrators full
#   (A;;CCLCSWLOCRRC;;;IU)                 Interactive Users default
#   (A;;CCLCSWLOCRRC;;;SU)                 Service principals default
#   S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)  audit
# Plus our ACE:
#   (A;;LCRP;;;<SID-of-WinServerRAG-Operators>)
#     LC = LIST_OBJECT
#     RP = SERVICE_START
# We deliberately omit WP (SERVICE_STOP).
function Get-LocalGroupSid($name) {
    $obj = New-Object System.Security.Principal.NTAccount($env:COMPUTERNAME, $name)
    return $obj.Translate([System.Security.Principal.SecurityIdentifier]).Value
}
$OperatorSid = Get-LocalGroupSid $OperatorGroup

$Sddl = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)" `
      + "(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)" `
      + "(A;;CCLCSWLOCRRC;;;IU)" `
      + "(A;;CCLCSWLOCRRC;;;SU)" `
      + "(A;;LCRP;;;$OperatorSid)" `
      + "S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)"

foreach ($svc in @($ServiceApi, $ServiceDaemon)) {
    Log "Applying SDDL to $svc (Operators get LIST + START only)..."
    $r = & sc.exe sdset $svc $Sddl 2>&1
    if ($LASTEXITCODE -ne 0) {
        # Don't crash — the service still works for admins. Just warn.
        # Common cause: SDDL syntax mismatch on an oddly-configured host.
        Write-Warning "sc sdset $svc failed: $r (exit $LASTEXITCODE) — Operators group will not have non-admin start rights"
    }
}

Log "Install complete. Services registered with auto-start."
Log "Operator group '$OperatorGroup' has SERVICE_START + SERVICE_QUERY_STATUS only (no SERVICE_STOP)."
Log "Members: $((& net.exe localgroup $OperatorGroup) -join ', ')"
exit 0
