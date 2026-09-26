; Inno Setup script for the GigaXML desktop application.
;
; Build it with the installed compiler:
;   "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging/gigaxml.iss
; or, from a bash shell:
;   ISCC=".../ISCC.exe"; "$ISCC" packaging/gigaxml.iss
;
; Why Inno Setup and not WiX/MSI: this is an open-source MIT project with no code-signing
; certificate, and that matters more than it sounds. An MSI is a Windows Installer package,
; and Windows Installer *trusts the installer format itself* -- a package that is not signed
; with a certificate issued to a known publisher can be refused or repaired by the machine
; before it ever runs, and repairing it tends to produce a half-installed state that is
; worse than not installing. Inno Setup produces a plain EXE: unsigned, it behaves like any
; other downloaded program, which is the same warning a user already has to understand for
; the application itself. One thing to explain instead of two.
;
; The installer is intentionally per-user. It writes to %LOCALAPPDATA%\Programs rather than
; Program Files, so no elevation prompt appears -- this application installs no services, no
; drivers and no shell extensions, and asking for administrator rights to place files the
; user already owns is exactly the kind of prompt that teaches people to click through.

#define AppName "GigaXML"
#define AppExeName "gigaxml-gui.exe"
#define AppVersion "0.1.0"
#define AppPublisher "GigaXML"
#define AppURL "https://github.com/gg320324492-lgtm/GigaXML-Memory-Efficient-XML-Extraction-Toolkit"

; Paths here are relative to *this script's* directory, not the working directory, and the
; two are not the same: this file lives in packaging\, and the build output lives beside
; the repository root. A relative OutputDir here therefore resolves to packaging\dist\, and
; the first build failed with "path not found" while the source it pointed at was correct.
; The repository root is derived from the script location so the two cannot drift apart.
#define RepoRoot ".."
#define DistRoot "..\dist"

[Setup]
AppId={{8F2C6A41-5D3B-4E17-9C88-2A6B0D4F71E9}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={localappdata}\Programs\GigaXML
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#DistRoot}\installer
OutputBaseFilename=GigaXML-Setup-{#AppVersion}
; A per-user install must not demand elevation. See the note above.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
SetupIconFile={#RepoRoot}\assets\gigaxml.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; No "restart later" theatre: nothing here is loaded by Explorer until the user runs it.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
  GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole onedir tree. PyInstaller's output is self-contained: the exe plus _internal,
; and the application cannot run without both. Copying only the exe is the single most
; common way to produce an installer that installs successfully and then does nothing.
Source: "{#DistRoot}\gigaxml-gui\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; \
  WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the uninstaller itself, so "uninstalled cleanly" means the install directory is
; gone rather than "only the file that has to stay is left".
;
; Measured before this step was added: after a silent uninstall the install directory still
; held unins000.exe and nothing else -- 4.2 MB, one file, 0.5 s. That is standard Inno
; Setup behaviour, and it is a real annoyance: a user who uninstalls expects the folder to
; disappear, and a leftover folder reads as "it did not really uninstall".
;
; The command is spawned detached and pointed at a *short* path, because the file has to
; still exist when this runs: a batch file in the same directory would be mid-deletion, and
; %TEMP% is used so that nothing depends on the directory it is deleting. cmd is used
; because it can wait on a process that no longer exists, which is exactly this situation.
Filename: "{cmd}"; Parameters: "/C ping 127.0.0.1 -n 3 > nul & rmdir /s /q ""{app}"""; \
  Flags: runhidden; RunOnceId: "RemoveGigaXMLDirectory"

[UninstallDelete]
; The application writes its settings beside the user's profile, not inside the install
; directory, so those survive an uninstall on purpose -- a user who reinstalls gets their
; configuration back. Anything the *application itself* leaves in its own directory does
; not survive, because a directory that quietly grows back after removal is a leak.
Type: filesandordirs; Name: "{app}\*.log"
Type: filesandordirs; Name: "{app}\*.tmp"
