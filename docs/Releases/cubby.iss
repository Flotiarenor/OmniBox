; ─────────────────────────────────────────────────────────────────────────────
; Cubby Windows 安装器（Inno Setup 6.3 或更新）
;
; 为什么需要这份脚本
;   Windows 用户拿到的是 PyInstaller onedir 产物（整个 .build-dist\Cubby 目录）。
;   直接发 zip 要用户自己解压、找 Cubby.exe、手动建快捷方式，也没有卸载入口。
;   本脚本把它打成单个安装器：per-user 安装（当前用户、不需要管理员权限）、
;   升级靠固定 AppId 覆盖同一安装目录、卸载走系统的"应用和功能"。
;
; 在 CI 的哪一步被编译、参数是什么
;   只有 .github/workflows/release.yml 的 build job（Windows runner）编译它：
;   先 choco install innosetup，再执行
;     iscc /DAppVersion=<pyproject 版本> /DSourceDir=<仓库>\.build-dist\Cubby
;          /O<仓库>\.build-dist docs/Releases/cubby.iss
;   产物是 <仓库>\.build-dist\Cubby-Setup-<版本>.exe；编译后另生成同名 .sha256，
;   两者一起上传，publish job 用 dist/* 收集。本地不需要安装 Inno Setup。
;
;   /D 传入的 AppVersion 与 SourceDir 都带 #ifndef 默认值：只检查或本地试编译
;   这份文件时不传参也能过预处理器，不会先报一串"未定义宏"的噪音。
;
; 其它约定
;   - 产物目录已由 tools/check_build_tree.py 校验干净，[Files] 因此整目录递归
;     铺设，不再维护第二份排除清单。
;   - 用户数据不在安装目录：非便携安装的数据根是 %LOCALAPPDATA%\Cubby，
;     卸载器默认保留它，用户明确选择"是"才删除。
;   - WebView2 Runtime 是 pywebview winforms 后端的运行时依赖：只检测、提示、
;     给出官方下载入口，不阻塞安装，构建期也不下载任何东西。
;   - 文件按 UTF-8 保存：Inno Setup 6.3 起 .iss 的 UTF-8 不再要求 BOM，
;     而中文提示文本必须走 UTF-8 才不会被按 ANSI 解释。
; ─────────────────────────────────────────────────────────────────────────────

#ifndef AppVersion
  ; 默认值只为让这份文件能单独通过预处理器；CI 总是用 /D 覆盖。
  #define AppVersion "0.0.0"
#endif

#ifndef SourceDir
  ; 默认指向本目录旁的构建输出（build-release.ps1 把产物放在 docs/Releases/Cubby），
  ; 方便在仓库里用默认参数试编译；CI 用 /D 指向 <仓库>\.build-dist\Cubby。
  #define SourceDir "..\..\docs\Releases\Cubby"
#endif

[Setup]
; 固定 AppId：同一个 GUID 才会把新版本装进同一目录、被系统的"应用和功能"认成
; 同一个程序。不要改这个值——改了等于让每个用户并存两份，旧的那份不会自动消失。
AppId={{8C6E4A4F-3B1D-4A7E-9F52-0D2C7B5A6E13}
AppName=Cubby
; AppVerName 决定卸载项里的 DisplayName：默认值会写成 "Cubby version 1.4.0"，
; 而"应用和功能"与 winget 清单都按 DisplayName 认这个程序，所以这里固定成产品名。
AppVerName=Cubby
AppVersion={#AppVersion}
; Publisher 会写进卸载项；缺省时"应用和功能"发布者一栏为空，包管理器也无从核对。
AppPublisher=Flotiarenor
AppPublisherURL=https://github.com/Flotiarenor/Cubby
AppSupportURL=https://github.com/Flotiarenor/Cubby/issues
AppUpdatesURL=https://github.com/Flotiarenor/Cubby/releases
DefaultDirName={localappdata}\Programs\Cubby
DefaultGroupName=Cubby
DisableProgramGroupPage=yes
; per-user：装到当前用户的 LocalAppData，安装过程不请求管理员权限。
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=Cubby-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Cubby.exe
; 安装器自身的文件属性：属性页里能看到产品名与版本，而不是空白的内嵌默认值。
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Flotiarenor
VersionInfoDescription=Cubby Windows 安装器
VersionInfoProductName=Cubby
VersionInfoProductVersion={#AppVersion}
; 升级时由 Setup 提示关闭正在运行的应用（默认行为），装完不自动把旧实例拉起来：
; 新版界面由用户自己启动，避免刚覆盖的文件被旧进程继续占用。
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 整目录递归铺设：产物已由 tools/check_build_tree.py 校验（必需文件齐全，
; 不含 __pycache__ / .config / data / logs），这里不再维护第二份排除清单。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; 开始菜单快捷方式是必需的；桌面图标是可选任务，默认不勾。
Name: "{autoprograms}\Cubby"; Filename: "{app}\Cubby.exe"
Name: "{autodesktop}\Cubby"; Filename: "{app}\Cubby.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Cubby.exe"; Description: "{cm:LaunchProgram,Cubby}"; Flags: nowait postinstall skipifsilent

[Code]
function GetUserDataDir: String;
begin
  { 与 shell/backend/paths.py 的默认用户数据根一致；便携模式（exe 旁的
    portable.txt）下数据在安装目录里，安装目录本身由卸载器删除。 }
  Result := ExpandConstant('{localappdata}\Cubby');
end;

function GetWebView2ClientKeyMachine: String;
begin
  { WebView2 Runtime 的系统级安装登记在 32 位注册表视图下；显式写 WOW6432Node，
    这样无论 Setup 处于哪种安装模式都能读到同一处。 }
  Result := 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
end;

function GetWebView2ClientKeyUser: String;
begin
  Result := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
end;

function IsWebView2RuntimeInstalled: Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, GetWebView2ClientKeyMachine, 'pv', Version) then
    if Version <> '' then
      Result := True;
  if not Result then
    if RegQueryStringValue(HKCU, GetWebView2ClientKeyUser, 'pv', Version) then
      if Version <> '' then
        Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  if IsWebView2RuntimeInstalled then
    Exit;

  { 静默安装（CI 的安装/卸载冒烟、以及无人值守部署）没有界面，不能在这里弹框，
    更不能去开浏览器：只记日志，由使用者自行补装运行时。 }
  if WizardSilent then
  begin
    Log('WebView2 Runtime 未检测到：静默安装不提示，也不下载任何东西。');
    Exit;
  end;

  { 只提示、不阻塞：缺运行时表现为 pywebview 的 winforms 后端起不来（没有窗口），
    但安装本身应该是成功的；装好运行时再启动即可。 }
  MsgBox('没有检测到 WebView2 Runtime。' + #13#10#13#10 +
         'Cubby 的界面由 WebView2 渲染，缺少它时程序启动后没有窗口。' + #13#10 +
         '接下来会打开微软官方的下载页，装好运行时再启动 Cubby 即可。',
         mbInformation, MB_OK);
  ShellExec('open', 'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
            '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Answer: Integer;
  DataDir: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;

  DataDir := GetUserDataDir;
  if not DirExists(DataDir) then
    Exit;

  { [Code] 里的 MsgBox 不受 /SUPPRESSMSGBOXES 影响（文档明确列出：它必须换成
    SuppressibleMsgBox 才会被抑制），所以静默卸载必须在这里显式分支，否则
    /VERYSILENT 会卡在一个没人点的对话框上。默认保留数据与文档要求一致：
    静默时等于"否"，只有用户明确选"是"才删。
    数据目录里是插件设置、缩略图缓存与索引，不是安装目录的内容；图库在用户自己
    选的路径下，永远不在这里。 }
  if UninstallSilent then
    Answer := IDNO
  else
    Answer := MsgBox('是否同时删除 Cubby 的用户数据？' + #13#10#13#10 +
                     '数据目录：' + DataDir + #13#10 +
                     '含插件设置、缩略图缓存与索引。' + #13#10#13#10 +
                     '选"否"（默认）保留数据，以后重装可以继续使用。',
                     mbConfirmation, MB_YESNO or MB_DEFBUTTON2);

  if Answer <> IDYES then
    Exit;

  if not DelTree(DataDir, True, True, True) then
    MsgBox('删除数据目录失败：' + DataDir + #13#10 +
           '可以手动删除该目录。', mbError, MB_OK);
end;
