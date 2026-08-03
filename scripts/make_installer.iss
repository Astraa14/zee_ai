; Inno Setup script for ZEE — compile with the Inno Setup 6 compiler:
;   https://jrsoftware.org/isinfo.php
;   iscc scripts\make_installer.iss
;
; Expects a built bundle in ..\dist\Zee\ (see scripts\build_windows.bat and
; zee.spec). Output goes to ..\artifacts\Zee-Setup-<version>.exe.

#define MyAppName "ZEE"
#define MyAppVersion "1.0.0"
#define MyAppExeName "Zee.exe"

[Setup]
AppId={{7F3E0B2A-9C4D-4E8B-9A2D-A1B2C3D4E5F6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\ZEE
DefaultGroupName=ZEE
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\artifacts
OutputBaseFilename=Zee-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The data dir (logs, token, certs, memory) is %APPDATA%\Zee, so uninstalling
; the app does not delete the user's memory/approval history.
UninstallDisplayName=ZEE Assistant

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"
Name: "autostart"; Description: "Start ZEE when I log in"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\Zee\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The Vosk model is downloaded separately (see docs/packaging_windows.md).
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ZEE"; Filename: "{app}\{#MyAppExeName}"; Parameters: "start"; WorkingDir: "{app}"
Name: "{group}\ZEE (daemon only)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "daemon"; WorkingDir: "{app}"
Name: "{autodesktop}\ZEE"; Filename: "{app}\{#MyAppExeName}"; Parameters: "start"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Optional autostart via the Run key when the user ticks the task.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "ZEE"; \
    ValueData: """{app}\{#MyAppExeName}"" start"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "start"; WorkingDir: "{app}"; \
    Flags: nowait postinstall skipifsilent; Description: "Launch ZEE now"
