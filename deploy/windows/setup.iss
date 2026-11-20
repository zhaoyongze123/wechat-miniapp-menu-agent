; Windows 每用户安装。版本文件和持久化数据分开，安装与升级可以共用同一个入口。
#define AppName "菜单采集工作台"

[Setup]
AppId={{021AE847-5C41-4E58-9C1B-53CC758713BA}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\MenuCapture
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
DisableDirPage=auto
OutputBaseFilename=MenuCapture-Setup-{#AppVersion}-win-x64
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
UninstallDisplayName={#AppName}
SetupLogging=yes

[Languages]
Name: "zh"; MessagesFile: "compiler:Default.isl,{#LanguageFile}"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}\releases\{#AppVersion}"; Excludes: "Launch.ps1"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist
Source: "{#BundleDir}\Launch.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\菜单采集工作台"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\Launch.ps1"" -Action Open"; WorkingDir: "{app}"
Name: "{autodesktop}\菜单采集工作台"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\Launch.ps1"" -Action Open"; WorkingDir: "{app}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Launch.ps1"" -Action Open"; Description: "打开本机工作台"; Flags: postinstall skipifsilent nowait runhidden

[UninstallDelete]
Type: files; Name: "{app}\current-version"

[Code]
function RunLauncher(const Action: String; var ResultCode: Integer): Boolean;
var
  Params: String;
begin
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\Launch.ps1') + '" -Action ' + Action;
  if Action = 'Register' then
    Params := Params + ' -Version "{#AppVersion}"';
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(Step: TSetupStep);
var
  ResultCode: Integer;
begin
  if Step = ssPostInstall then
  begin
    if (not RunLauncher('Register', ResultCode)) or (ResultCode <> 0) then
      RaiseException('无法设置登录自动启动。请查看本机菜单采集日志或联系管理员。');
  end;
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Result := RunLauncher('Unregister', ResultCode) and (ResultCode = 0);
  if not Result then
    SuppressibleMsgBox('请先在工作台停止采集、结束人工标注，再卸载。配置和离线结果会保留。', mbError, MB_OK, IDOK);
end;
