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
# Transcript log — silent failures during Inno Setup [Run] are the
# operator's number-one diagnostic problem. Capture every line of
# stdout/stderr to disk so a post-mortem is one `notepad` away.
#
# Why we set this up before any other code: Start-Transcript itself can
# fail if $LogDir doesn't exist yet (this is a fresh install path), so
# we mkdir-best-effort first, then start the transcript. If even that
# fails (e.g. ProgramData ACL surprise), we fall through and rely on
# stderr — the hard error is still propagated to Inno Setup.
# ---------------------------------------------------------------------
try {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    }
    $logStamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $mode = if ($Uninstall) { "uninstall" } else { "install" }
    $TranscriptPath = Join-Path $LogDir "install-services-$mode-$logStamp.log"
    Start-Transcript -Path $TranscriptPath -Force | Out-Null
    Write-Host "[install-services] transcript: $TranscriptPath"
} catch {
    Write-Warning "Could not start transcript: $_"
}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
function Log($msg) { Write-Host "[install-services] $msg" }

# Run a native command (sc.exe / net.exe / nssm.exe / icacls) without
# letting its stderr abort the script.
#
# PowerShell 5.1 wraps non-zero native exits with NON-EMPTY STDERR as a
# `NativeCommandError` ErrorRecord on the error stream. With
# $ErrorActionPreference = "Stop" (set at the top of this script), that
# record becomes a TERMINATING error — even when the script writer
# clearly intended to handle the case via $LASTEXITCODE.
#
# `2>$null` redirects the OUTPUT, but PowerShell still detects "had
# stderr" before the redirect takes effect, so the Stop preference
# fires anyway. The only reliable suppression is to lower
# ErrorActionPreference for the duration of the call.
#
# v1.3.2: PR #37 changed `2>&1` to `2>$null > $null` thinking that fixed
# it. It only fixed the sc.exe case (sc writes "service not found" to
# STDOUT, not stderr). Group-Exists kept throwing on net.exe's
# "システム エラー 1376". This helper closes both holes.
function Invoke-Native {
    param([scriptblock]$Cmd)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Cmd 2>$null | Out-Null
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Service-Exists($name) {
    return (Invoke-Native { sc.exe query $name }) -eq 0
}

function Group-Exists($name) {
    return (Invoke-Native { net.exe localgroup $name }) -eq 0
}

function Stop-IfRunning($name) {
    if (Service-Exists $name) {
        Log "Stopping $name (if running)..."
        $null = Invoke-Native { sc.exe stop $name }
        # Best-effort: don't error if already stopped (1062).
    }
}


# ---------------------------------------------------------------------
# Main work — wrapped in try/catch so the slightest failure produces a
# diagnosable on-disk sentinel (`install-services-FAILED.txt`) and a
# non-zero exit code Inno Setup can react to. v1.3.0 had this whole
# block silently swallow `throw` because Inno Setup [Run] was passing
# everything through cmd.exe with $ErrorActionPreference="Stop" — the
# exit code reached cmd, not Inno Setup. Direct powershell.exe + this
# wrapper gives us both halves of the safety net.
# ---------------------------------------------------------------------
try {

# ---------------------------------------------------------------------
# Uninstall path
# ---------------------------------------------------------------------
if ($Uninstall) {
    Log "Uninstall mode."
    Stop-IfRunning $ServiceDaemon
    Stop-IfRunning $ServiceApi
    if (Service-Exists $ServiceDaemon) {
        Log "Removing $ServiceDaemon..."
        $null = Invoke-Native { & $NssmExe remove $ServiceDaemon confirm }
    }
    if (Service-Exists $ServiceApi) {
        Log "Removing $ServiceApi..."
        $null = Invoke-Native { & $NssmExe remove $ServiceApi confirm }
    }
    if (Group-Exists $OperatorGroup) {
        Log "Removing local group $OperatorGroup..."
        $null = Invoke-Native { net.exe localgroup $OperatorGroup /delete }
    }
    Log "Uninstall complete."
    try { Stop-Transcript | Out-Null } catch {}
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

# 1.0a v1.3.2: detect Chocolatey shim masquerading as nssm.exe.
# The shim (~60KB) delegates to a real binary in choco's lib subtree
# that doesn't exist on the operator's machine — every nssm call would
# return exit -1 with "Cannot find file at '..\lib\NSSM\tools\...'".
# Real nssm.exe 2.24 win64 is ~370KB. If we ever ship a shim again,
# this throws fast with a clear message instead of failing mid-install.
$nssmSize = (Get-Item $NssmExe).Length
$nssmDesc = (Get-Item $NssmExe).VersionInfo.FileDescription
if ($nssmSize -lt 200000 -or $nssmDesc -match "Shim") {
    throw "nssm.exe at $NssmExe looks like a Chocolatey shim (size=$nssmSize, desc='$nssmDesc'). Real nssm-2.24 win64 is ~370KB and FileDescription does NOT contain 'Shim'. The CI workflow's `Install NSSM via choco` step is supposed to copy the REAL binary from C:\ProgramData\chocolatey\lib\NSSM\tools\win64\nssm.exe, not the shim from C:\ProgramData\chocolatey\bin\nssm.exe. Re-build the installer."
}
Log "nssm.exe verified ($nssmSize bytes, '$nssmDesc')"

# 1.1 v1.3.2: clear any stale FAILED.txt sentinel from a prior install.
# Inno Setup's CurStepChanged hook checks for this file's presence (NOT
# its age — Inno Pascal Script doesn't expose FileAge). Removing it
# here means: if the catch block at the bottom doesn't fire, no
# sentinel = success path. Catch-block writes a fresh FAILED.txt with
# the actual error if anything throws below.
$staleFail = Join-Path $LogDir "install-services-FAILED.txt"
if (Test-Path $staleFail) {
    Log "Clearing stale FAILED.txt from prior install: $staleFail"
    Remove-Item -Force -ErrorAction SilentlyContinue $staleFail
}

# 1.5 v1.3.2: upgrade-clean re-register.
#
# If we're running on top of an older WinServerRAG, the services already
# exist with the prior version's NSSM config in the registry. Inno
# Setup's PrepareToInstall (in the .iss [Code] section) has already
# stopped them so [Files] could overwrite the bundled exes — but the
# SCM entries themselves still point at the OLD AppDirectory and may
# carry stale AppParameters / AppEnvironmentExtra from the previous
# version. `nssm set` updates fields in-place but cannot remove a
# field that the new install no longer needs.
#
# Cleanest fix: remove the services here, fall through to the
# `Install-NssmService` calls below which re-create them fresh.
# Falls back to update-in-place if `nssm remove` doesn't take.
foreach ($svc in @($ServiceDaemon, $ServiceApi)) {
    if (Service-Exists $svc) {
        Log "Existing service detected, removing for clean re-register: $svc"
        # PrepareToInstall already stopped these, but be defensive — a
        # user invoking this script manually as a "repair" step might
        # not have stopped them first.
        Stop-IfRunning $svc
        Start-Sleep -Seconds 2
        $null = Invoke-Native { & $NssmExe remove $svc confirm }
        if (Service-Exists $svc) {
            Log "Warning: $svc still exists after nssm remove; install path will update params in place instead."
        } else {
            Log "Removed $svc."
        }
    }
}

# 2. Make sure the writable dirs exist.
foreach ($d in @($ConfigDir, $LogDir)) {
    if (-not (Test-Path $d)) {
        Log "Creating $d"
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
}

# 2.5 v1.3.2: lock down the config directory ACL.
#
# config.v2.env contains GOOGLE_OAUTH_CLIENT_SECRET, PGPASSWORD, and
# (optionally) API_BEARER_TOKEN. Inno Setup's [Dirs] only ADDS explicit
# ACEs — it does not break inherited ACEs from %ProgramData% which by
# default grants Authenticated Users read access to subdirectories.
# Without this step, any local user can `type config.v2.env` and harvest
# the credentials.
#
# After this:
#   Administrators : Full control (operators editing config)
#   SYSTEM         : Full control (services run as LocalSystem by default)
#   <nobody else>  : no access
#
# Logs and backup dirs stay user-readable/writable (no secrets there).
Log "Hardening config dir ACL: $ConfigDir"
$ec = Invoke-Native { icacls.exe $ConfigDir /inheritance:r /Q }
if ($ec -ne 0) {
    Write-Warning "icacls /inheritance:r failed (exit $ec) — config dir may still allow Users:read"
}
$ec = Invoke-Native { icacls.exe $ConfigDir /grant:r 'Administrators:(OI)(CI)F' /Q }
if ($ec -ne 0) {
    Write-Warning "icacls grant Administrators failed (exit $ec)"
}
$ec = Invoke-Native { icacls.exe $ConfigDir /grant:r 'SYSTEM:(OI)(CI)F' /Q }
if ($ec -ne 0) {
    Write-Warning "icacls grant SYSTEM failed (exit $ec)"
}
# v1.3.3: also grant the operator user direct access. The Setup Wizard
# (WinServerRAG Setup.exe) runs as the operator without UAC elevation,
# so it needs write access to drop config.v2.env. Without this grant,
# the wizard's Apply step fails with EPERM. Other local users still
# can't read secrets — only the explicitly named operator account.
if ($OperatorUser) {
    $ec = Invoke-Native { icacls.exe $ConfigDir /grant:r "${OperatorUser}:(OI)(CI)F" /Q }
    if ($ec -ne 0) {
        Write-Warning "icacls grant operator '$OperatorUser' failed (exit $ec) — wizard may need elevation"
    }
}

# 3. Local Operators group — create-if-missing, idempotent.
if (-not (Group-Exists $OperatorGroup)) {
    Log "Creating local group: $OperatorGroup"
    $ec = Invoke-Native { net.exe localgroup $OperatorGroup /add /comment:"Members can start/query the WinServerRAG services without UAC." }
    if ($ec -ne 0) {
        throw "Failed to create local group $OperatorGroup (exit $ec)"
    }
} else {
    Log "Local group already exists: $OperatorGroup"
}

# 4. Add the operator user (Inno Setup's {username}, or $env:USERNAME).
if (-not $OperatorUser) { $OperatorUser = $env:USERNAME }
if ($OperatorUser) {
    Log "Adding user '$OperatorUser' to '$OperatorGroup' (if not already)..."
    # `net localgroup ... /add` returns 2 if the user is already a member;
    # not an error in our context.
    $null = Invoke-Native { net.exe localgroup $OperatorGroup $OperatorUser /add }
}

# 5. Install API service — idempotent. If it exists, fall through to
#    `nssm set` calls below to re-apply the params (Codex: blind
#    `nssm install` on existing service mismatches the repair semantics).
function Install-NssmService($name, $exe) {
    if (Service-Exists $name) {
        Log "Service exists, will update params: $name"
    } else {
        Log "Installing service: $name -> $exe"
        $ec = Invoke-Native { & $NssmExe install $name $exe }
        if ($ec -ne 0) { throw "nssm install $name failed: $ec" }
    }
}

Install-NssmService $ServiceApi    $ApiExe
Install-NssmService $ServiceDaemon $DaemonExe

# 6. Apply / re-apply NSSM params for both services.
#
# v1.3.2: stopTimeoutMs param. NSSM's default stop sequence is
# Console (1.5s) → Window (1.5s) → Threads (1.5s) → Process kill ≈ 4.5s.
# The daemon needs much longer to drain in-flight workers (PDF extract,
# embedding batch, DB commit can take 10-30s each). Without this, NSSM
# TerminateProcess's the daemon mid-task → stale worker rows in PG,
# leaked advisory locks, half-committed builds.
#
# stopTimeoutMs is the Console (Ctrl+C / SIGBREAK) budget. We give 30s
# to daemon, 5s to API (the FastAPI lifespan unwinds quickly). Window /
# Threads phases stay short (5s each) — by the time we're there, the
# graceful path failed and we want to escalate.
function Set-NssmParams($name, $appDir, $logBase, $env, $depends, $stopTimeoutMs) {
    $null = Invoke-Native { & $NssmExe set $name AppDirectory       $appDir }
    $null = Invoke-Native { & $NssmExe set $name AppStdout          "$LogDir\$logBase.out.log" }
    $null = Invoke-Native { & $NssmExe set $name AppStderr          "$LogDir\$logBase.err.log" }
    $null = Invoke-Native { & $NssmExe set $name AppRotateFiles     1 }
    $null = Invoke-Native { & $NssmExe set $name AppRotateBytes     10485760 }
    $null = Invoke-Native { & $NssmExe set $name AppRotateOnline    1 }  # rotate without restart
    $null = Invoke-Native { & $NssmExe set $name AppEnvironmentExtra "WINSERVERRAG_CONFIG_DIR=$ConfigDir" }
    $null = Invoke-Native { & $NssmExe set $name Start              SERVICE_AUTO_START }
    # Stop sequence — graceful shutdown budget.
    $null = Invoke-Native { & $NssmExe set $name AppStopMethodSkip    0 }  # try all 4 phases
    $null = Invoke-Native { & $NssmExe set $name AppStopMethodConsole $stopTimeoutMs }
    $null = Invoke-Native { & $NssmExe set $name AppStopMethodWindow  5000 }
    $null = Invoke-Native { & $NssmExe set $name AppStopMethodThreads 5000 }
    $null = Invoke-Native { & $NssmExe set $name AppKillProcessTree   1 }  # kill children with main
    if ($depends) {
        $null = Invoke-Native { & $NssmExe set $name DependOnService $depends }
    }
}

# API: 5s console budget (FastAPI lifespan unwinds fast).
# Daemon: 30s console budget (workers may be mid-PDF / mid-embedding).
Set-NssmParams $ServiceApi    (Split-Path $ApiExe)    "api"    $ConfigDir $null         5000
Set-NssmParams $ServiceDaemon (Split-Path $DaemonExe) "daemon" $ConfigDir $ServiceApi  30000

# 7. Set descriptions (cosmetic, services.msc).
$null = Invoke-Native { & $NssmExe set $ServiceApi    Description "WinServerRAG control API + web monitor (FastAPI on :17600)" }
$null = Invoke-Native { & $NssmExe set $ServiceDaemon Description "WinServerRAG indexer daemon (rag_daemon multi-threaded)" }

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
    # We WANT stderr text here for the warning. `2>&1` would re-engage
    # ErrorActionPreference=Stop on a sc.exe non-zero exit. Drop the
    # preference for this one call so the warning path can fire instead
    # of throwing.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $r = & sc.exe sdset $svc $Sddl 2>&1
        $ec = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
    if ($ec -ne 0) {
        # Don't crash — the service still works for admins. Just warn.
        # Common cause: SDDL syntax mismatch on an oddly-configured host.
        Write-Warning "sc sdset $svc failed: $r (exit $ec) — Operators group will not have non-admin start rights"
    }
}

Log "Install complete. Services registered with auto-start."
Log "Operator group '$OperatorGroup' has SERVICE_START + SERVICE_QUERY_STATUS only (no SERVICE_STOP)."
# Member listing is cosmetic; if it errors (rare), don't fail the whole install.
$savedAEP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $members = (& net.exe localgroup $OperatorGroup 2>$null) -join ', '
} catch { $members = "(could not list)" }
$ErrorActionPreference = $savedAEP
Log "Members: $members"

}
catch {
    # Fatal — surface the error to disk so the operator can find it
    # without re-running with /LOG=. The sentinel filename is fixed so
    # the mini-monitor or a future "Repair" button can detect it.
    $err = $_
    Write-Host "FATAL: $err"
    Write-Host $err.ScriptStackTrace
    try {
        $errPath = Join-Path $LogDir "install-services-FAILED.txt"
        $stamp = Get-Date -Format "s"
        "[$stamp] mode=$mode error=$err`r`n$($err.ScriptStackTrace)" |
            Out-File -FilePath $errPath -Append -Encoding utf8
    } catch {
        # Even the error log failed — print to stderr at least.
        [Console]::Error.WriteLine("FATAL (could not write error log): $err")
    }
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

try { Stop-Transcript | Out-Null } catch {}
exit 0
