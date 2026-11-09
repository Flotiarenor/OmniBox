# 分发渠道：winget 与 Scoop

状态：计划中（设计定稿，无代码）

本文只决定三件事：**要不要进包管理器、进了以后清单写什么、由谁在什么时候更新它**。安装器、数据根与发
布链的既有约定见[安装与更新](./install-and-update.md)与[CI、打包与发布](./ci-and-release.md)，本文不
重复它们、也不改其中任何一条。

## 1. 目标与边界

- **winget** 解决"Windows 用户找不到安装包、也升不了级"，受益的是习惯命令行、按机器清单部署、不愿在
  浏览器里过一次 SmartScreen 的人。它复用安装器 `Cubby-Setup-<版本>.exe`，装完的系统状态与双击安装器
  完全一致（per-user、数据在默认数据根、卸载默认保留数据）。
- **Scoop** 解决"要与其它命令行工具一起被脚本管理"的场景（开发者）。它复用便携 zip
  `Cubby-windows-x64.zip`，装进 `~\scoop\apps\cubby`，数据靠 `persist` 在升级中活下来（§4.3）。
- **两者都不新增第二条安装逻辑**：产物、AppId、数据根解析顺序（[paths.py](../shell/backend/paths.py)
  的 `get_user_data_dir()`）全部照旧。硬边界：**不为包管理器改数据根、目录布局或产物名**；清单只能如
  实描述既有行为，描述不了就不进这个渠道。

| 通道 | 产物 | 数据根 | 升级方式 | 自动化 |
| --- | --- | --- | --- | --- |
| 安装器（现状） | `Cubby-Setup-<版本>.exe` | `%LOCALAPPDATA%\Cubby` | 覆盖安装，同 AppId | 手动下载 |
| winget | 同一个安装器 | 同上 | `winget upgrade` | 每版本一个清单 PR |
| Scoop | `Cubby-windows-x64.zip` | `~\scoop\persist\cubby\` | `scoop update cubby` | bucket 自动更新 |

## 2. winget 清单

### 2.1 标识与命名

`PackageIdentifier` 取 **`Flotiarenor.Cubby`**：`<Publisher>.<Package>`，只含字母数字与 `.`、`-`，不
带空格；Publisher 段必须是产物发布者（这里是 GitHub 所有者 `Flotiarenor`），借用他人名字会在审核阶段
被拒。`PackageVersion` 是**裸版本号**（`1.4.0`，不带 `v`），取自 [pyproject.toml](../pyproject.toml)
的 `[project].version`，与 release tag 去掉 `v` 后逐字符相同（
[check_version.py](../tools/check_version.py) 保证）；三份文件都声明 `ManifestVersion: 1.6`。

### 2.2 文件布局与 version 清单

源文件放仓库内 `packaging/winget/Flotiarenor/Cubby/1.4.0/`，由工作流（§6）提交到
`microsoft/winget-pkgs` 的同一路径；目录首字母取标识首字母小写，三份文件是 `Flotiarenor.Cubby.yaml`
（version）、`.installer.yaml`、`.locale.zh-CN.yaml`。

version 清单只放标识、版本与默认区域，描述性字段（`Publisher` / `PackageName`）属于 locale 清单：
`PackageIdentifier: Flotiarenor.Cubby`、`PackageVersion: 1.4.0`、`DefaultLocale: zh-CN`、
`ManifestType: version`、`ManifestVersion: 1.6`。`Platform` / `MinimumOSVersion` 可省略；写时取
`Windows.Desktop` 与 `10.0.0.0`（WebView2 是运行期依赖，Windows 10 以下不在范围内）。

### 2.3 installer 清单

| 字段 | 取值 | 依据 |
| --- | --- | --- |
| `InstallerType`、`Architecture` | `inno`、`x64` | `.iss` 由 Inno Setup 6 编译，`ArchitecturesAllowed=x64compatible` |
| `Scope` | `user` | `PrivilegesRequired=lowest`、装到 `{localappdata}` |
| `InstallModes` | `interactive` / `silent` / `silentWithProgress` | 三种都由 `/SILENT` 系列支持 |
| `UpgradeBehavior` | `install` | 同 AppId 覆盖安装，不需要先卸旧版 |
| `InstallerSwitches.Silent` | `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` | 与 [cubby.iss](./Releases/cubby.iss) 的 `WizardSilent` / `UninstallSilent` 分支一致；`SilentWithProgress` 同理，首个开关换成 `/SILENT` |
| `InstallerUrl` | `https://github.com/Flotiarenor/Cubby/releases/download/v<版本>/Cubby-Setup-<版本>.exe` | 资产名带版本号，不能用 `latest/download` |
| `InstallerSha256` | `.sha256` 首列（64 位十六进制） | [release.yml](../.github/workflows/release.yml) 的「生成安装器校验和」 |

`AppsAndFeaturesEntries` 只写一条：`DisplayName: Cubby`、`Publisher: Flotiarenor`、
`DisplayVersion: <版本>`（见 §2.4 —— `.iss` 现在固定了 `AppVerName=Cubby`，所以显示名不随版本变化；
`ProductCode` 的取舍见同一节）。

### 2.4 与 `cubby.iss` 的对齐（最容易出错的一节；下表是真机实测值）

Inno 的卸载项写在
`HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C6E4A4F-3B1D-4A7E-9F52-0D2C7B5A6E13}_is1`
（`<AppId>_is1`，`AppId` 自带花括号）。在 Windows 11 + Inno Setup 6.7.3 上装一次后，键里的值与清单字段的
对应关系是：

| 卸载项里的值（实测） | 来源 | 清单字段 |
| --- | --- | --- |
| `DisplayName = Cubby` | `AppVerName=Cubby`（缺省时 Setup 会写成 `Cubby 1.4.0`，每个版本都变） | `AppsAndFeaturesEntries.DisplayName` |
| `DisplayVersion = 1.4.0` | `AppVersion={#AppVersion}` | `DisplayVersion`（必须等于 `PackageVersion`） |
| `Publisher = Flotiarenor` | `AppPublisher=Flotiarenor` | `AppsAndFeaturesEntries.Publisher` 与 locale 的 `Publisher` |
| `InstallLocation = <安装目录>\` | `DefaultDirName` | 不写 |
| `UninstallString` / `QuietUninstallString` | Setup 生成 | 不写；winget 直接用 ARP 项卸载 |
| `URLInfoAbout` / `HelpLink` / `URLUpdateInfo` | `AppPublisherURL` / `AppSupportURL` / `AppUpdatesURL` | locale 的 `PublisherUrl` / `PublisherSupportUrl` / `PackageUrl` |
| `NoModify=1` / `NoRepair=1` | Inno 默认（没有修改与修复入口） | 不写 |
| **没有 `ProductCode`** | Inno 不写这个值 | **不写** `AppsAndFeaturesEntries.ProductCode` |

**`ProductCode` 的结论：不写。** 卸载项里没有名为 `ProductCode` 的值，把 AppId 填进去等于让 winget 去匹配
一个不存在的值，比省略更糟；`inno` 类型靠 `DisplayName` + `Publisher`（必要时加 `DisplayVersion`）与 ARP
项匹配，这正是 `AppsAndFeaturesEntries` 存在的目的。

（实测方式：`iscc /DAppVersion=1.4.0` 编译后 `/VERYSILENT` 装到临时目录，逐值读出 ARP 项再卸载。
`cubby.iss` 的 `AppVerName` / `AppPublisher` / `VersionInfo*` 就是按这张表补上的。）

### 2.5 locale 清单

| 字段 | 取值 |
| --- | --- |
| `PackageLocale` / `DefaultLocale` | `zh-CN` |
| `Publisher` / `PackageName` | `Flotiarenor` / `Cubby`（安装器名，与 `.iss` 的 `AppName` 同值） |
| `PublisherUrl` / `PackageUrl` / `PublisherSupportUrl` | 仓库地址 / 仓库地址 / 仓库地址 + `/issues` |
| `License` / `LicenseUrl` | `Apache-2.0` / [LICENSE](../LICENSE) 的 GitHub 链接 |
| `ShortDescription` / `Description` | 一句话（≤ 256 字符）/ 两到三句：壳 + 插件、"数据在用户目录、升级不用搬数据" |
| `Tags` / `ReleaseNotesUrl` | `desktop`、`framework`、`plugins` / `.../releases/tag/v<版本>` |

## 3. 首次提交与后续版本更新

1. **首次**（人工，每包一次）：`wingetcreate new` 或手写三份 YAML → 在 fork 上开 PR → 自动校验（
   Schema、URL 可达、哈希一致）→ 人工审核（`New package` 标签、安装验证结果）→ 合并后进入 winget 源。
   这步**必须**在 Release 已 Publish 之后，否则 `InstallerUrl` 是 404。
2. **后续版本**（每版本一次）：新增 `Flotiarenor/Cubby/<新版本>/` 三份文件并开 PR。
   `wingetcreate update Flotiarenor.Cubby --version <版本> --urls <安装器 URL> --submit` 会读远端哈
   希填 `InstallerSha256`；也可由工作流按同一形状生成（§6）。
3. **凭据**：本仓库的 `GITHUB_TOKEN` 只能操作本仓库，**不能**给 `microsoft/winget-pkgs` 开 PR。需要
   一次 fork 加一个 classic PAT（`public_repo` 范围，owner 为 `Flotiarenor`），存成 secret
   `WINGET_TOKEN`；`wingetcreate` 推到该账号名下的 fork 再开 PR。
4. **失败与撤回**：PR 被关就改清单重开（哈希不符最常见，说明 `.sha256` 与产物不同步）。已合并的错误
   版本只能**另开一个删除该版本目录的 PR**并写明原因；已发布 Release 的资产要保留——清单 URL 直接指向
   它，删资产等于让已安装用户无法回装同版本。下架顺序：先删清单版本，再决定是否 `gh release delete`。

## 4. Scoop

### 4.1 bucket 形态：自建 bucket

推荐建 `Flotiarenor/scoop-cubby`（`scoop bucket add cubby <仓库地址>`），而不是往 `extras` 提：
`extras` 的收录标准与审核周期不由本项目决定，而本包的 `pre_install` 写便携标记、`persist` 接管数据目
录（§4.3）需要向审核者解释；自建 bucket 一次配置、之后自动更新。用户规模变大后再评估是否另提
`extras`，判据是"值不值得维护两条清单"。

### 4.2 manifest 字段

bucket 里 `bucket/cubby.json`。发布 zip 用 `Compress-Archive -Path .build-dist/Cubby/*` 打，
`Cubby.exe` 就在压缩包根目录（`tar.gz` 才有顶层 `Cubby/`），因此 **`extract_dir` 不填**。

| 字段 | 取值 / 取舍 |
| --- | --- |
| `version` | 裸版本号 `1.4.0`，与 `PackageVersion` 同源 |
| `architecture.64bit.url` / `.hash` | `.../releases/download/v1.4.0/Cubby-windows-x64.zip` / `.sha256` 首列 |
| `pre_install` | 在 `$dir` 写空的 `portable.txt`（§4.3） |
| `persist` | `[".config", "data", "plugins"]` |
| `shortcuts` | `[["Cubby.exe", "格子铺 Cubby"]]` |
| `bin` | **不填**：GUI 应用，PATH 里的 `cubby` shim 只会拉起带控制台的实例；要命令行入口时再加并重新验收 |
| `checkver` | `{"github": "<仓库地址>", "regex": "v?([\\d.]+)"}`（§5） |
| `autoupdate` | `url` 为 `.../releases/download/v$version/Cubby-windows-x64.zip`；`hash.url` 为同版本 `.sha256`（§5） |

### 4.3 数据目录：便携标记 + `persist` 联接

Scoop 的 `apps\cubby\current` 是指向 `apps\cubby\<版本>` 的联接，**升级会换掉整个版本目录**，数据不
能放在版本目录里裸奔。方案分两步：`pre_install` 在 `$dir` 写 `portable.txt`，让
[paths.py](../shell/backend/paths.py) 的 `is_portable()` 成立、数据根解析到"可执行文件所在目录"（
`%LOCALAPPDATA%` 那条不再生效）；`persist` 让 Scoop 在版本目录建三个指向 `~\scoop\persist\cubby\` 的
联接，数据实际落在 persist，升级时重建联接、内容不动，`scoop uninstall` 默认保留该目录，与安装器"卸
载默认保留数据"一致。

`pre_install` / `post_install` 只是安装阶段执行一次的钩子，写下的东西属于当前版本目录、**升级时随目
录一起消失**；只有 `persist` 声明过的路径会被搬到 persist 并重建联接。所以钩子只负责补那个标记文件，
不承担存数据。

**为什么不用 `CUBBY_HOME`**：Scoop 只能用 `env_set` 写用户级环境变量，那会让这台机器上**所有** Cubby
启动都改道——包括安装器装的那份和从源码运行的开发副本——副作用溢出到进程边界之外，卸载后还会残留。
`--user-data-dir` 同理：包管理器不给用户点击的入口注入参数。

| 场景 | 数据在哪 | 是否还在 |
| --- | --- | --- |
| `scoop update cubby` / `scoop reset cubby` | `~\scoop\persist\cubby\` | 在；版本目录换了，联接重建 |
| `scoop uninstall cubby` | 同上 | 在（默认保留 persist）；手删 persist 才等于安装器选"是" |
| 与安装器版本并存 | 安装器版走 `%LOCALAPPDATA%\Cubby` | 各一份，互不干扰 |

## 5. 自动更新的联动

- **`checkver`**：Scoop 请求 `GET https://api.github.com/repos/Flotiarenor/Cubby/releases/latest`，
  取 `tag_name`（形如 `v1.4.0`），`regex` 去 `v` 得 `1.4.0`。`latest` 不含草稿与 prerelease，响应里
  `draft` 恒为 `false`——**草稿阶段取不到版本**。
- **`autoupdate.hash` 只给 `url`**，指向同版本的 `Cubby-windows-x64.zip.sha256`。该文件是
  `<十六进制> <文件名>` 一行，Scoop 按文件名在文本里找摘要，正好匹配，不需要 `regex`/`jsonpath`。
- **草稿时序**：`/releases/download/<tag>/<资产>` 在草稿下是 404，所以**不能**挂
  `push: tags: ['v*']` 或 `release: created`，那会在人工 Publish 前抓一次空资产。正确触发是 **
  `release: types: [published]`**——人点 Publish 才发，此刻资产已可下载；bucket 侧的每日 `schedule`
  只作兜底。这条对 winget 同样成立（§3）。

## 6. CI 与权限

`release.yml` **保持原样**：仍然只构建、只建草稿，公开要人点 Publish。新增两个工作流，全部以"已发布
的 Release"为输入，不碰发布主流程：

| 工作流 | 触发 | 权限 | 内容 |
| --- | --- | --- | --- |
| `distribution-check.yml` | `push`/`pull_request` 涉及 `packaging/**`、`workflow_dispatch` | `contents: read` | 清单版本 == `pyproject.toml`、YAML/JSON 结构、`winget validate --manifest`（`windows-latest`；镜像不带 winget 时降级为结构校验并记 notice）、URL 形状（含版本号、指向 `releases/download/v<版本>/`） |
| `distribution-publish.yml` | `release: types: [published]`、`workflow_dispatch`（输入 tag，供补发） | `contents: read` + secret `WINGET_TOKEN`、`SCOOP_TOKEN` | 用已发布 Release 的 `.sha256` 生成三份 YAML 与 `cubby.json`；`wingetcreate update --submit` 开 PR，提交 `cubby.json` 到 bucket |

- **必须人工确认的一步**：`distribution-publish.yml` 挂 `environment: distribution` 并设 required
  reviewers——Publish 之后还要有人在环境审批里过一眼才对外提交，这是"发版仍由人决定"的第二次确认。
- **为什么不用 `GITHUB_TOKEN`**：跨仓库 PR 与推 bucket 都不在它的授权范围内，只能靠 PAT，且只给最小
  范围（`public_repo` + bucket 写权限）。PAT 过期时工作流失败，不静默降级；两个工作流都留
  `workflow_dispatch` 兜底。若把清单校验塞进 `ci.yml`，必须按该文件的纪律写 `needs: changes` 与
  `if:`。

## 7. 验收清单

**阶段 1：清单自身**（用普通用户终端试；提权终端下 `{localappdata}` 归谁由 Setup 与 Inno 版本决定，
不作为验收环境）

```powershell
winget validate --manifest packaging/winget/Flotiarenor/Cubby/1.4.0
(Get-FileHash .\Cubby-Setup-1.4.0.exe -Algorithm SHA256).Hash.ToLower()   # 与 .sha256 首列、InstallerSha256 三处一致
```

**阶段 2：winget 试装**（临时机器）：
`winget install --manifest packaging/winget/Flotiarenor/Cubby/1.4.0 --silent`，再
`winget list --id Flotiarenor.Cubby` 与
`reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall" /s /f Cubby`。观察：
`%LOCALAPPDATA%\Programs\Cubby\Cubby.exe` 与 `unins000.exe` 就位；卸载项的 `DisplayName` /
`DisplayVersion` / `Publisher` 与 `AppsAndFeaturesEntries` 逐字段相同；全程不弹 WebView2 对话框。

**阶段 3：Scoop 试装**

```powershell
scoop bucket add cubby <bucket 仓库地址>
scoop install cubby
Test-Path "$env:USERPROFILE\scoop\apps\cubby\current\portable.txt"
Get-Item "$env:USERPROFILE\scoop\apps\cubby\current\.config" | Select-Object LinkType, Target
```

观察：`Test-Path` 为 True；`.config` / `data` / `plugins` 三项是指向 `~\scoop\persist\cubby\` 的联接；
启动一次后 persist 下出现 `.config\app.json` 与 `data\`。

**阶段 4：静默、卸载与升级**

| 项 | 命令 | 观察 |
| --- | --- | --- |
| 静默参数 | `.\Cubby-Setup-1.4.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=$env:TEMP\cubby.log` | 退出码 0、全程无窗口；日志含 `WebView2 Runtime 未检测到：静默安装不提示` |
| 卸载保留数据 | 启动一次后 `& "$env:LOCALAPPDATA\Programs\Cubby\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART` | 安装目录消失，`Test-Path "$env:LOCALAPPDATA\Cubby"` 仍为 True |
| 安装器升级 | 装 1.3.2 → 装 1.4.0 | `winget list` 只有一条且为 1.4.0；`.config` 里 `app.json` 的修改时间不变 |
| Scoop 升级 | `scoop update cubby` | persist 内容在，`current` 指向新版本目录，联接仍在 |
| Scoop 卸载 | `scoop uninstall cubby` | `apps\cubby` 消失，`persist\cubby` 仍在 |

**阶段 5：自动更新联动**：Publish 一个版本、等 `distribution-publish.yml` 跑完，确认 bucket 的
`version` 与 `hash` 已变、winget PR 已开；再到旧版本机器上跑 `scoop update cubby` 与
`winget upgrade`，两个渠道都能升到新版本且数据还在。

## 8. 风险与取舍

- **无签名**：清单审核不检查 Authenticode，签名缺失不阻止收录；用户从浏览器下载仍会碰到 SmartScreen，
  这条通道只绕过浏览器的下载路径，不是签名的替代。验收里如实记录 `winget install` 时是否出现拦截，不
  预先承诺"不会提示"。签名仍是后续项。
- **ARP 字段与清单不一致 → `winget upgrade` 失效**：ProductCode 写错会让 winget 认为没装、再装一份；
  ARP 的 `DisplayName` 比清单少一个版本号、或 `DisplayVersion` 与 `PackageVersion` 不一致，则表现为
  永远"有新版本"或升完仍提示。靠阶段 2 的 `reg query` 与阶段 4 的升级用例兜住；改 `.iss` 必须同步
  改清单。
- **`persist` 与便携标记冲突**：用户自己在版本目录放 `portable.txt`、或设了 `CUBBY_HOME`，数据会绕过
  persist 落到别处，而 Scoop 以为在管数据。只在 bucket README 写一句"用 Scoop 装的不要再手动放
  marker"；技术上不加第二套判断，否则两条规则互相猜。
- **Scoop 升级时必须关闭应用**：Scoop 先删旧版本目录，运行中的 `Cubby.exe` 会让删除失败并留下半个版
  本目录；bucket 描述写明"升级前关闭 Cubby"，与安装器 `CloseApplications=yes` 对齐。
- **维护成本**：每版本一次 winget PR（外部审核、可能因哈希或 URL 失败重开）+ 一次 bucket 提交。若反
  复被拒或下载量证明没人用，就停掉 winget 只留自建 bucket——包管理器是渠道，不是承诺。
- **该放弃的情况**：任何方案要求改数据根解析顺序、在安装目录写数据、或为某个渠道改产物名与 AppId，一
  律不做。这条优先于本文件其余内容。
- **已拍板的三条**：① `.iss` 已加 `AppVerName=Cubby` 与 `AppPublisher=Flotiarenor`（只改卸载项显示，
  不动安装目录、AppId 与数据根），清单按 §2.4 如实描述；② Scoop 只维护自建 bucket，`extras` 等用户规模
  证明值得维护两条清单时再评估；③ `distribution-publish.yml` 的 `environment: distribution` 由仓库所有者
  `Flotiarenor` 担任 required reviewer —— 这是 GitHub 仓库设置（Settings → Environments），不在仓库文件里，
  落地时要手工建一次。
