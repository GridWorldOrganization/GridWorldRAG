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
#define AppVersion     "1.2.0"
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
; -ExecutionPolicy Bypass: signed scripts not in scope; script ships
; alongside nssm.exe under {app}\bin which is admin-only writable.
Filename: "{cmd}"; \
  Parameters: "/C powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\install-services.ps1"" -InstallRoot ""{app}"" -DataRoot ""{commonappdata}\{#AppName}"" -OperatorUser ""{username}"""; \
  Flags: runhidden; StatusMsg: "Registering services + relaxing service ACL..."

; -- DB init (manual / not registered as a service) --
;
; The user runs db_init once after edit­ing config.v2.env. We do NOT
; auto-run it during install because PostgreSQL might not be ready yet
; on a fresh box (newly installed, not yet started).

; -- Optional: launch mini-monitor at the end --
Filename: "{app}\mini\{#AppExeMini}"; \
  Description: "{cm:LaunchProgram,{#AppName} Mini Monitor}"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; v1.3: delegate to install-services.ps1 -Uninstall, which stops both
; services, removes them, and deletes the local Operators group.
; Single source of truth, mirrors the [Run] block above.
Filename: "{cmd}"; \
  Parameters: "/C powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\install-services.ps1"" -InstallRoot ""{app}"" -DataRoot ""{commonappdata}\{#AppName}"" -Uninstall"; \
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
