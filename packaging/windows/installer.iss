; Inno Setup script for the Job Cannon Windows installer (WP9).
;
; Build (after the PyInstaller step has produced dist\JobCannon):
;
;   uv run pyinstaller packaging/windows/job-cannon.spec
;   iscc /DAppVersion=<version> packaging\windows\installer.iss
;
; AppVersion is injected at build time from pyproject.toml (the CI workflow
; reads it; locally pass /DAppVersion=5.0.0 or accept the 0.0.0 dev default).
; Output: dist\JobCannon-Setup-<version>.exe
;
; Design decisions (RELEASE-POLISH-PLAN.md WP9):
;   - Per-user install (PrivilegesRequired=lowest) — no UAC prompt, mirrors
;     how Ollama / VS Code install. Program files land under
;     {localappdata}\Programs\JobCannon.
;   - Optional Desktop shortcut (unchecked) + optional "start at login"
;     keepalive supervisor (unchecked). The supervisor is a per-user Scheduled
;     Task at logon (job-cannon.exe supervisor-install) that also restarts the
;     app on crash — superseding the old bare HKCU Run key. Login launch is
;     safe: the app's pidfile + /__jc_health probe make a second launch focus
;     the existing instance, and `serve` reclaims the port from a crashed one.
;   - Uninstall deregisters the supervisor + removes program files, then PROMPTS before touching
;     user data ({localappdata}\JobCannon — jobs database, config, logs),
;     defaulting to KEEP. NB: the data dir is %LOCALAPPDATA%\JobCannon
;     (platformdirs user_data_dir with roaming=False — see
;     job_finder/web/user_data_dirs.py), distinct from the install dir
;     %LOCALAPPDATA%\Programs\JobCannon.
;   - Ships unsigned at launch (decision #3): SmartScreen shows "Windows
;     protected your PC" — documented in INSTALL.md with the More info →
;     Run anyway flow; SHA-256 checksums published on each release.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{F773B9E8-438D-4C02-BD36-8F53EB6B932D}
AppName=Job Cannon
AppVersion={#AppVersion}
AppPublisher=Senkichi
AppPublisherURL=https://github.com/Senkichi/job-cannon
AppSupportURL=https://github.com/Senkichi/job-cannon/issues
AppUpdatesURL=https://github.com/Senkichi/job-cannon/releases
DefaultDirName={localappdata}\Programs\JobCannon
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=JobCannon-Setup-{#AppVersion}
SetupIconFile=job-cannon.ico
UninstallDisplayIcon={app}\job-cannon.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The app is a 64-bit Python freeze; refuse 32-bit Windows.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; In-place upgrades: CloseApplications asks the user to quit a running
; instance (the tray app holds job-cannon.exe open) instead of failing the
; file copy.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startatlogin"; Description: "Start Job Cannon when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The whole PyInstaller onedir output. Path is relative to this .iss file.
Source: "..\..\dist\JobCannon\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Job Cannon"; Filename: "{app}\job-cannon.exe"
Name: "{userdesktop}\Job Cannon"; Filename: "{app}\job-cannon.exe"; Tasks: desktopicon

[Run]
; Login launch + crash-restart via the per-user keepalive supervisor (a
; Scheduled Task at logon, NO admin). Replaces the old standalone HKCU Run key
; so startup is registered exactly once. Gated on the same "startatlogin" task.
Filename: "{app}\job-cannon.exe"; Parameters: "supervisor-install"; \
  Description: "Register Job Cannon to start automatically"; \
  Tasks: startatlogin; Flags: runhidden
Filename: "{app}\job-cannon.exe"; Description: "Launch Job Cannon"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Deregister the keepalive supervisor before the program files are removed.
; --uninstall is a no-op success if the supervisor was never installed.
Filename: "{app}\job-cannon.exe"; Parameters: "supervisor-install --uninstall"; \
  RunOnceId: "SupervisorUninstall"; Flags: runhidden

[Code]
{ Uninstall: prompt before deleting user data (jobs database, config.yaml,
  logs under %LOCALAPPDATA%\JobCannon). Default is NO — the database survives
  an uninstall/reinstall cycle unless the user explicitly opts in. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}') + '\JobCannon';
    if DirExists(DataDir) then
    begin
      if MsgBox('Also delete your Job Cannon data (jobs database, config)?'
                + #13#10 + DataDir,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
