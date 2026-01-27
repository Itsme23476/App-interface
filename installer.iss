; Inno Setup Script for AI File Organizer
; ========================================
; 
; HOW TO USE:
; 1. First run: python build.py (creates dist/AI File Organizer/)
; 2. Download & install Inno Setup from https://jrsoftware.org/isinfo.php
; 3. Open this file in Inno Setup Compiler
; 4. Click "Compile" (or press Ctrl+F9)
; 5. The installer will be created in the "Output" folder
;
; To change version: Update AppVersion below

#define MyAppName "AI File Organizer"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Your Company Name"
#define MyAppURL "https://yourwebsite.com"
#define MyAppExeName "AI File Organizer.exe"

[Setup]
; Basic info
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; Install location
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; Output settings
OutputDir=Output
OutputBaseFilename=AI_File_Organizer_Setup_v{#MyAppVersion}
SetupIconFile=resources\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes

; Windows version requirement
MinVersion=10.0

; Privileges (admin for Program Files, or user for AppData)
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; Visual settings
WizardStyle=modern
WizardResizable=no

; Uninstaller
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked; OnlyBelowVersion: 6.1; Check: not IsAdminInstallMode

[Files]
; Include everything from the PyInstaller output folder
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu shortcut
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"

; Desktop shortcut (if user selected it)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Option to launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
// Custom code can go here if needed
// For example, checking for .NET runtime or other dependencies
