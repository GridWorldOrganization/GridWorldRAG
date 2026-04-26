; WinServerRAG installer — Inno Setup 6 script.
;
; Layout in $InstallDir (default %ProgramFiles%\WinServerRAG):
;   bin\
;     winserverrag-api\        ← PyInstaller --onedir bundle
;     winserverrag-daemon\
;     winserverrag-dbinit\
;     winserverrag-backup\
;     nssm.exe                 ← Service manager (bundled)
;   mini\                       ← electron-builder output (win-unpacked)
;   docs\
;
; Per-machine config / data lives outside the install dir:
;   %ProgramData%\WinServerRAG\config\config.v2.env
;   %ProgramData%\WinServerRAG\backups\
;   %ProgramData%\WinServerRAG\logs\
;
; Two NSSM-registered Windows services are created at install time:
;   WinServerRAG-API     (auto-start)
;   WinServerRAG-Daemon  (auto-start, depends on API)
;
; PostgreSQL 17 + pgvector + (optional) NVIDIA CUDA driver are PREREQ —
; the installer prompts the user if they're missing but does not bundle
; them. Internal/social distribution (no code signing); Smart Screen
; warning on first run is acceptable.

#define AppName        "WinServerRAG"
; AppVersion can be overridden from the command line via
;   ISCC.exe /DAppVersion=1.3.0 ...
; That's how build.ps1 + the GitHub Actions workflow pass the version.
; The hardcoded value here is the fallback when no -D is provided.
#ifndef AppVersion
  #define AppVersion   "1.3.0"
#endif
#define AppPublisher   "GridJapan"
#define AppURL         "https://github.com/GridWorldOrganization/GridWorldRAG"
#define AppExeMini     "WinServerRAG Mini.exe"
#define ServiceApi     "WinServerRAG-API"
#define ServiceDaemon  "WinServerRAG-Daemon"

[Setup]
AppId={{C6F0E8A2-4D52-4F1E-9D4A-WSRG1200V100}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=WinServerRAG-Setup-{#AppVersion}
; SetupIconFile=assets\icon.ico   ; Drop a 256x256 .ico here to brand the installer
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; UninstallDisplayIcon={app}\mini\{#AppExeMini}   ; needs an .ico — Mini Monitor exe is not a .ico
ChangesEnvironment=no
; v1.3.2: graceful upgrade — close any running Mini Monitor before
; [Files] tries to overwrite mini\WinServerRAG Mini.exe. `force` =
; kill if the user ignores the prompt. Daemon/API services are
; stopped separately in PrepareToInstall (Code section) since they
; aren't owned by a foreground window.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english";  MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startmenuicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "desktopicon";   Description: "{cm:CreateDesktopIcon}";    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller --onedir outputs (entire folders).
Source: "..\..\dist\exe\winserverrag-api\*";    DestDir: "{app}\bin\winserverrag-api";    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\dist\exe\winserverrag-daemon\*"; DestDir: "{app}\bin\winserverrag-daemon"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\dist\exe\winserverrag-dbinit\*"; DestDir: "{app}\bin\winserverrag-dbinit"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\dist\exe\winserverrag-backup\*"; DestDir: "{app}\bin\winserverrag-backup"; Flags: ignoreversion recursesubdirs createallsubdirs

; Electron mini-monitor (electron-builder --dir output).
Source: "..\..\dist\desktop\win-unpacked\*"; DestDir: "{app}\mini"; Flags: ignoreversion recursesubdirs createallsubdirs

; NSSM (bundled in installer/inno/assets at build time by build.ps1).
Source: "assets\nssm.exe"; DestDir: "{app}\bin"; Flags: ignoreversion

; Config example + docs.
Source: "..\..\config\config.v2.env.example"; DestDir: "{commonappdata}\{#AppName}\config"; DestName: "config.v2.env.example"; Flags: ignoreversion onlyifdoesntexist
Source: "..\..\README.md";   DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\MCP.md";      DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\CHANGELOG.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\LICENSE";     DestDir: "{app}\docs"; Flags: ignoreversion

; v1.3 service-installer PowerShell script — does the NSSM register +
; SDDL relaxation + Operators group bookkeeping. Lives next to nssm.exe.
Source: "..\install-services.ps1"; DestDir: "{app}\bin"; Flags: ignoreversion

[Dirs]
; Per-machine writable dirs. Permissions: standard users can write logs
; and run backups, but cannot edit config (admin-only).
Name: "{commonappdata}\{#AppName}\config";  Permissions: users-readexec admins-modify
Name: "{commonappdata}\{#AppName}\logs";    Permissions: users-modify
Name: "{commonappdata}\{#AppName}\backups"; Permissions: users-modify
Name: "{commonappdata}\{#AppName}\backups\daily";  Permissions: users-modify
Name: "{commonappdata}\{#AppName}\backups\weekly"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName} Mini Monitor"; Filename: "{app}\mini\{#AppExeMini}"
Name: "{group}\{#AppName} Web Console";  Filename: "http://127.0.0.1:17600/"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName} Mini";   Filename: "{app}\mini\{#AppExeMini}"; Tasks: desktopicon

[Run]
; --- v1.3 service registration: delegated to install-services.ps1 ---
;
; Replaces the long sequence of `nssm install/set` calls that lived
; here in v1.2. The PowerShell script is idempotent (re-run on an
; existing install updates params in place), creates the local
; `WinServerRAG Operators` group, adds the installing user, and
; relaxes the service SDDL so the mini-monitor can `sc start` without
; UAC. Single source of truth for service registration — re-runnable
; from a future "repair" path.
;
; v1.3.1: invoke powershell.exe DIRECTLY (no cmd /C indirection).
; v1.3.0 used `Filename: "{cmd}"; Parameters: "/C powershell.exe ..."`
; which double-parsed the quoting and silently dropped the script
; arguments on some hosts. Direct invocation is simpler and visibly
; correct in services.msc + Inno Setup install logs.
;
; -ExecutionPolicy Bypass: signed scripts not in scope; script ships
; alongside nssm.exe under {app}\bin which is admin-only writable.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\install-services.ps1"" -InstallRoot ""{app}"" -DataRoot ""{commonappdata}\{#AppName}"" -OperatorUser ""{username}"""; \
  Flags: runhidden; StatusMsg: "Registering services + relaxing service ACL..."

; -- DB init (manual / not registered as a service) --
;
; The user runs db_init once after edit­ing config.v2.env. We do NOT
; auto-run it during install because PostgreSQL might not be ready yet
; on a fresh box (newly installed, not yet started).

; -- v1.3.2: Mini Monitor is NOT auto-launched at install end. --
;
; v1.3.0/1 fired Mini via `Flags: nowait postinstall skipifsilent`.
; Two problems with that:
;   1. The installer fires-and-forgets, so a Mini crash during init
;      (e.g. asar missing service-control.js — the PR #34 bug) orphans
;      Electron's GPU/utility helper subprocesses with no cleanup.
;      Repeated install attempts accumulated 3+ orphan processes.
;   2. Showing Mini before the user has confirmed install success is
;      awkward — the Inno Setup "Finish" page should be the success
;      signal, not a popup window racing it.
;
; Mini Monitor is now Start-Menu / desktop-shortcut launch only.
; Defense-in-depth: desktop/main.js also has uncaughtException +
; require-guard handlers (v1.3.2) so any future Mini crash exits cleanly
; instead of leaking helper processes.

[UninstallRun]
; v1.3: delegate to install-services.ps1 -Uninstall, which stops both
; services, removes them, and deletes the local Operators group.
; Single source of truth, mirrors the [Run] block above.
; v1.3.1: same direct-powershell.exe fix as the [Run] block.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\install-services.ps1"" -InstallRoot ""{app}"" -DataRoot ""{commonappdata}\{#AppName}"" -Uninstall"; \
  Flags: runhidden; RunOnceId: "uninstall_services"

[UninstallDelete]
; Logs / backups / config in ProgramData are intentionally PRESERVED on
; uninstall. The user can `rmdir /s "%ProgramData%\WinServerRAG"` if
; they want a clean wipe.
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\mini"
Type: filesandordirs; Name: "{app}\docs"

[Code]
// Pre-install check: PostgreSQL service detection.
function IsPostgresInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  // Check for postgresql-x64-17 service (the PG 17 default service name).
  Exec(ExpandConstant('{cmd}'), '/C sc query postgresql-x64-17 >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := (ResultCode = 0);
end;

function InitializeSetup(): Boolean;
var
  Resp: Integer;
begin
  Result := True;
  if not IsPostgresInstalled() then
  begin
    Resp := MsgBox(
      'PostgreSQL 17 service was not detected on this machine.' + #13#10 + #13#10 +
      'WinServerRAG requires PostgreSQL 17 with the pgvector extension to be ' +
      'installed and running before the services can start. You can:' + #13#10 +
      '  1) Install PostgreSQL 17 from https://www.postgresql.org/download/windows/' + #13#10 +
      '  2) Build pgvector from source (see docs/EVOLUTION.md)' + #13#10 + #13#10 +
      'Continue installation anyway?',
      mbConfirmation, MB_YESNO);
    Result := (Resp = IDYES);
  end;
end;

// v1.3.2: graceful upgrade support.
//
// If a previous WinServerRAG is installed and its services are running,
// the daemon/API exes are locked by NSSM-spawned processes. [Files]
// would fail to overwrite them. We stop the services in PrepareToInstall
// (the documented Inno hook for "do this before [Files]"), wait for
// SCM to confirm STOPPED, and let install proceed. install-services.ps1
// then sees existing services and `nssm remove`s them for a clean
// re-register (drops any stale AppParameters / AppEnvironmentExtra).

function ServiceExists(const SvcName: String): Boolean;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/C sc query "' + SvcName + '" >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := (ResultCode = 0);
end;

function ServiceIsStopped(const SvcName: String): Boolean;
var
  ResultCode: Integer;
begin
  // findstr exit 0 means "STOPPED" line was found in `sc query` output.
  Exec(ExpandConstant('{cmd}'),
       '/C sc query "' + SvcName + '" | findstr /C:"STATE" | findstr /C:"STOPPED" >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := (ResultCode = 0);
end;

function StopServiceAndWait(const SvcName: String): Boolean;
var
  ResultCode, WaitCount: Integer;
begin
  Result := True;
  if not ServiceExists(SvcName) then Exit; // Fresh install — nothing to do.

  // sc stop is best-effort; if already stopped it returns 1062 which is fine.
  Exec(ExpandConstant('{cmd}'),
       '/C sc stop "' + SvcName + '" >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // Poll for STOPPED state, up to 30s. NSSM's stop-method default is
  // graceful-then-kill at 5s, so 30s is plenty even for a stuck daemon.
  WaitCount := 0;
  while WaitCount < 30 do
  begin
    if ServiceIsStopped(SvcName) then Exit;
    Sleep(1000);
    WaitCount := WaitCount + 1;
  end;
  Result := False; // Timeout.
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  // Stop daemon FIRST — it depends on API, so the API stop won't take
  // effect cleanly while the daemon is still hitting it.
  if not StopServiceAndWait('{#ServiceDaemon}') then
  begin
    Result := '{#ServiceDaemon} did not reach STOPPED state within 30 seconds. ' +
              'Stop it manually via services.msc and re-run the installer.';
    Exit;
  end;
  if not StopServiceAndWait('{#ServiceApi}') then
  begin
    Result := '{#ServiceApi} did not reach STOPPED state within 30 seconds. ' +
              'Stop it manually via services.msc and re-run the installer.';
    Exit;
  end;
end;
