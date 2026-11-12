; Inno Setup script for DiskReport-Setup-<version>.exe. Run build.py: it builds dist\Disk Report first, then this.
; Installs for the current account only (no administrator rights needed), in %LOCALAPPDATA%\Programs\Disk Report.

#ifndef AppVersion
  #define AppVersion "2.0"
#endif
#define AppName "Disk Report"
#define AppExe "Disk Report.exe"

[Setup]
AppId={{8F3D2A61-6C3B-4E2A-9B7E-2D7C9E5A1B44}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=disk-report
AppPublisherURL=https://github.com/znissou/disk-report
AppSupportURL=https://github.com/znissou/disk-report/issues
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=DiskReport-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
; app.py holds this mutex while it runs: Setup and the uninstaller ask to close the app first, so they never
; replace or remove its files while it's open
AppMutex=DiskReportApp

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\Disk Report\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the app's scans, settings, action log and browser data: an uninstall leaves nothing behind
Type: filesandordirs; Name: "{localappdata}\{#AppName}"
