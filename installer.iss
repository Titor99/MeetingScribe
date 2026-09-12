; MeetingScribe 会议实时转写系统 - Inno Setup 安装脚本
#define AppName "会议转写 MeetingScribe"
#define AppVersion "1.3.1"
#define AppExeName "Python\pythonw.exe"

[Setup]
AppId={{8F3A2B71-4C6D-4E2F-9E1A-1A5B8C3D7E90}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=MeetingScribe
DefaultDirName={localappdata}\Programs\MeetingScribe
DefaultGroupName=会议转写
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer-output
OutputBaseFilename=MeetingScribe-Setup-1.3.1
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\assets\icon.ico
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"

[Files]
Source: "dist\MeetingScribe\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.json"
; 用户配置：已存在则不覆盖（重装保留），卸载时不删除
Source: "dist\MeetingScribe\config.json"; DestDir: "{app}"; Flags: ignoreversion uninsneveruninstall onlyifdoesntexist

[Icons]
Name: "{autodesktop}\会议转写"; Filename: "{app}\{#AppExeName}"; Parameters: "desktop_app.py"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon
Name: "{group}\会议转写"; Filename: "{app}\{#AppExeName}"; Parameters: "desktop_app.py"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"
Name: "{group}\卸载 会议转写"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "desktop_app.py"; WorkingDir: "{app}"; Description: "启动 会议转写"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\browser-profile"
