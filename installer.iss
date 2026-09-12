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
; 个人数据（会议/声纹/配置）不在安装目录，统一存于 {userdocs}\MeetingScribe，由应用首次启动时生成
Source: "dist\MeetingScribe\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.json"

[Icons]
Name: "{autodesktop}\会议转写"; Filename: "{app}\{#AppExeName}"; Parameters: "desktop_app.py"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon
Name: "{group}\会议转写"; Filename: "{app}\{#AppExeName}"; Parameters: "desktop_app.py"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"
Name: "{group}\卸载 会议转写"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "desktop_app.py"; WorkingDir: "{app}"; Description: "启动 会议转写"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\browser-profile"

[Code]
// 卸载向导：复选框「保留我的个人数据」，默认勾选。
// 个人数据目录为 文档\MeetingScribe（会议存档/声纹库/个人配置）。
// 静默卸载（/SILENT、/VERYSILENT）一律保留；测试删除路径可用 /DELETEUSERDATA。
var
  KeepUserData: Boolean;

procedure InitializeUninstallProgressForm();
var
  UninstallPage: TNewNotebookPage;
  UninstallButton: TNewButton;
  KeepCheckbox: TNewCheckBox;
  InfoText: TNewStaticText;
  ctrl: TWinControl;
  OrigPageName: string;
  OrigPageDesc: string;
  OrigCancelEnabled: Boolean;
  OrigCancelModalResult: Integer;
begin
  KeepUserData := True;  { 默认保留；静默卸载不会弹窗，直接保留 }
  if not UninstallSilent then
  begin
    ctrl := UninstallProgressForm.CancelButton;
    UninstallButton := TNewButton.Create(UninstallProgressForm);
    UninstallButton.Parent := UninstallProgressForm;
    UninstallButton.Left := ctrl.Left - ctrl.Width - ScaleX(10);
    UninstallButton.Top := ctrl.Top;
    UninstallButton.Width := ctrl.Width;
    UninstallButton.Height := ctrl.Height;
    UninstallButton.TabOrder := ctrl.TabOrder;
    UninstallButton.Caption := '卸载';
    UninstallButton.ModalResult := mrOK;
    UninstallProgressForm.CancelButton.TabOrder := UninstallButton.TabOrder + 1;

    UninstallPage := TNewNotebookPage.Create(UninstallProgressForm);
    UninstallPage.Notebook := UninstallProgressForm.InnerNotebook;
    UninstallPage.Parent := UninstallProgressForm.InnerNotebook;
    UninstallPage.Align := alClient;
    UninstallProgressForm.InnerNotebook.ActivePage := UninstallPage;

    ctrl := UninstallProgressForm.StatusLabel;
    InfoText := TNewStaticText.Create(UninstallProgressForm);
    InfoText.Parent := UninstallPage;
    InfoText.Top := ctrl.Top;
    InfoText.Left := ctrl.Left;
    InfoText.Width := ctrl.Width;
    InfoText.Height := ScaleY(60);
    InfoText.AutoSize := False;
    InfoText.WordWrap := True;
    InfoText.ShowAccelChar := False;
    InfoText.Caption := '个人数据位于：' + ExpandConstant('{userdocs}\MeetingScribe') +
      '（会议录音、转写记录、声纹库、个人配置）。卸载程序本身不会删除该目录。';

    KeepCheckbox := TNewCheckBox.Create(UninstallProgressForm);
    KeepCheckbox.Parent := UninstallPage;
    KeepCheckbox.Top := ctrl.Top + ScaleY(66);
    KeepCheckbox.Left := ctrl.Left;
    KeepCheckbox.Width := ctrl.Width;
    KeepCheckbox.Caption := '保留我的个人数据（推荐）';
    KeepCheckbox.Checked := True;

    OrigPageName := UninstallProgressForm.PageNameLabel.Caption;
    OrigPageDesc := UninstallProgressForm.PageDescriptionLabel.Caption;
    OrigCancelEnabled := UninstallProgressForm.CancelButton.Enabled;
    OrigCancelModalResult := UninstallProgressForm.CancelButton.ModalResult;

    UninstallProgressForm.PageNameLabel.Caption := '卸载选项';
    UninstallProgressForm.PageDescriptionLabel.Caption := '是否保留您的会议数据？';
    UninstallProgressForm.CancelButton.Enabled := True;
    UninstallProgressForm.CancelButton.ModalResult := mrCancel;

    if UninstallProgressForm.ShowModal = mrCancel then
      Abort;

    UninstallButton.Visible := False;
    UninstallProgressForm.PageNameLabel.Caption := OrigPageName;
    UninstallProgressForm.PageDescriptionLabel.Caption := OrigPageDesc;
    UninstallProgressForm.CancelButton.Enabled := OrigCancelEnabled;
    UninstallProgressForm.CancelButton.ModalResult := OrigCancelModalResult;
    UninstallProgressForm.InnerNotebook.ActivePage := UninstallProgressForm.InstallingPage;

    KeepUserData := KeepCheckbox.Checked;
  end;
end;

function WantDeleteUserData(): Boolean;
var
  i: Integer;
begin
  Result := not KeepUserData;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), '/DELETEUSERDATA') = 0 then
      Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if WantDeleteUserData() then
      DelTree(ExpandConstant('{userdocs}\MeetingScribe'), True, True, True);
  end;
end;
