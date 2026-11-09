# 安装与更新

本文是 2.0 的安装与更新设计：**为什么这样设计**，以及**以后照这个做**。
它只写方案与判据；门禁与流水线细节见 [CI、打包与发布](./ci-and-release.md)，
本地运行见[开发文档](./development.md)，面向使用者的下载与安装见 [readme.md](../readme.md)。

三份专题设计承接本文没有展开的细节：[update-check.md](./update-check.md)（应用内检查更新）、
[distribution-channels.md](./distribution-channels.md)（winget / Scoop）、
[linux-distribution.md](./linux-distribution.md)（`install.sh` / systemd / 容器镜像）。

> 范围声明：本文描述 2.0 的目标形态。已实装与未实装在文末的「落地状态表」逐项标注；
> 正文提到"后续"的条目都是**计划，不是现状**。

2.0 被当作**全新安装**处理：1.x 的数据不再被读取（提交 `adfca90` 删除了
`%APPDATA%/OmniBox` 的迁移代码及其用例，见 `git show --stat adfca90`），
因此安装方式可以重新设计，不必背"从旧目录搬数据"的包袱。

---

## 1. 问题与根因

1.x 把可写数据放在**可执行文件旁边**：打包模式下 [paths.py](../shell/backend/paths.py) 的
`get_user_data_dir()` 先看可执行文件所在目录能否写入，能写就把 `.config/`、
`data/` 一起放在那里，不能写才回退到用户目录。开发模式则直接用项目根目录。

这个决定在"解压即用"的场景里很自然，代价有三条：

1. **换版本等于换目录**。新版解压到新文件夹后，旧文件夹里的 `.config/` 与
   `data/` 不会被新版本看见 —— 用户的相册、书架、播放记录在界面上"消失"，
   只能手工把两个目录拷过去。搬运步骤没有任何提示，出错时表现为"设置全丢"。
2. **同一台机器上数据位置取决于运行方式**。在 Program Files 之类只读位置运行时
   数据落在用户目录，在可写目录运行时落在程序旁边；两个副本互不相认。
3. **"运行过的目录"从此不能分发**。程序目录里混进了本机的插件设置、访问令牌、
   路径与缩略图缓存，压缩包里因此可能带着开发机或上一位使用者的状态。

根因是一条：**把"程序文件"与"用户数据"放进了同一个可写目录，再用"这个目录能否
写入"去决定它是什么**。只要这个前提在，安装、升级、卸载就都是搬数据。

## 2. 2.0 的决定：数据根与安装目录分离

2.0 只做一件事：**安装目录在运行时只有只读资源，用户数据一律落在独立的数据根**。
两边由 [paths.py](../shell/backend/paths.py) 的 `get_user_data_dir()` 一处解析。

### 2.1 数据根的解析顺序

按顺序命中即停，第一条决定整个进程的用户数据根：

| 顺序 | 条件 | 数据根 | 用途 |
| --- | --- | --- | --- |
| 1 | 环境变量 `CUBBY_HOME`（`USER_DATA_DIR_ENV`）非空 | 该变量的值 | 多实例、端到端测试、把状态放到任意位置 |
| 2 | 开发模式（`is_frozen()` 为假，从源码运行） | 项目根目录 | 开发者直接看到 `data/`、`.config/` |
| 3 | 便携模式：`is_portable()` 为真（可执行文件旁存在 `PORTABLE_MARKER`，即 `portable.txt`） | 可执行文件所在目录；该目录不可写时退回第 4 条并记一条告警 | 绿色使用、U 盘、带着数据走 |
| 4 | 其余情况 | 平台默认数据根（见下） | 安装器的默认形态 |

平台默认数据根（`default_user_data_dir()`）：

| 平台 | 位置 | 缺失时的回退 |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\Cubby` | `%APPDATA%\Cubby`；两者都缺时 `~/AppData/Local/Cubby` |
| Linux | `$XDG_DATA_HOME/cubby` | `~/.local/share/cubby` |

三条设计说明：

- **`CUBBY_HOME` 放在第一位**，因此它高于"开发模式"与"便携模式"。它是进程级覆盖
  （值在 `get_user_data_dir()` 首次调用时读入并缓存），`main()` 必须在任何
  `get_config_dir()` 调用之前处理它；测试里改完要 `get_user_data_dir.cache_clear()`。
- **便携模式需要显式 marker**。只有 `PORTABLE_MARKER`（`portable.txt`）存在才把数据
  放在程序目录旁，不再用"这个目录正好可写"来推断 —— 推断的失败模式正是 §1 的问题
  2 与 3。目录不可写时（只读介质、受保护目录）不进入便携模式，落到默认数据根，
  记一条告警，并在启动日志里打印实际使用的数据根。
- **默认数据根不用漫游目录**（`%APPDATA%`）：它只在 `LOCALAPPDATA` 缺失时作为回退。
  理由见 §7「`%LOCALAPPDATA%` 不进漫游」。

### 2.2 数据根下的布局

数据根之下的全部内容都可写、都可删除、都不进发行产物：

| 位置 | 内容 | 生成方 |
| --- | --- | --- |
| `.config/` | 主配置 `app.json`、壳偏好 `shell.json`、访问令牌 `auth_token.txt` | `get_config_dir()` 定位置；`app.json` 由 [main.py](../main.py) 的配置加载写、`shell.json` 由 [settings_store.py](../shell/backend/settings_store.py) 写、`auth_token.txt` 由 [auth.py](../shell/backend/auth.py) 写 |
| `.config/plugins/` | 每个插件一个设置文件 `<插件名>.json` | `get_plugins_config_dir()` |
| `.config/webview/` | WebView2 的 UserDataFolder（浏览器 profile 与 HTTP 缓存） | `get_webview_storage_dir()` |
| `.config/logs/` | 滚动日志 `cubby.log` | [app_logging.py](../shell/backend/app_logging.py) 的 `get_log_dir()` / `setup_logging()` |
| `data/` | 索引、缩略图缓存等运行期数据，对应配置里的 `directories.data_root` | `resolve_data_root()`（相对路径锚定到数据根） |
| `plugins/` | 用户自装插件，搜索优先级高于内置插件 | `get_plugin_search_dirs()` |

`data_root` 的相对路径（默认 `./data`）锚定到数据根，不再锚定启动时的工作目录，
因此快捷方式、计划任务、`--web-only` 从任意目录启动都得到同一个 `data/`。

### 2.3 安装目录只放只读资源

打包模式下，内置插件与前端产物从 PyInstaller 的 `_MEIPASS`（`get_project_root()`）
读取；程序运行期间不向安装目录写任何文件。于是：

- 升级 = 覆盖安装目录，**数据不动**；
- 卸载 = 删除安装目录，数据默认保留（见 §3）；
- 安装目录被设为只读（例如系统管理员统一部署）也不影响运行。

需要用户放入的插件不再要求放进安装目录，而是放进数据根的 `plugins/`：
`get_plugin_search_dirs()` 先返回 `<数据根>/plugins`，再返回内置插件目录。

### 2.4 版本号从构建到「关于」

版本号的唯一来源仍是 `pyproject.toml` 的 `[project].version`，安装器产物名、
Release tag 与界面上显示的版本都从这一处派生。链路分三步：

1. [check_version.py](../tools/check_version.py) 校验 `shell/frontend/package.json` 的
   `version` 与 `package-lock.json` 里两处副本都等于 `[project].version`；发布时同一个
   脚本再校验 tag 与它一致（不一致就在第一步失败，不会产出名字与版本不符的安装包）。
2. [vite.config.ts](../shell/frontend/vite.config.ts) 在构建期读 `package.json`，用
   `define` 把版本注入前端常量 `__APP_VERSION__` —— 打包后仓库文件不在产物里，
   运行期只能读这份常量。
3. 「关于」段（[SettingsView.vue](../shell/frontend/src/views/SettingsView.vue)）显示
   `格子铺 Cubby v{{ appVersion }}`，`appVersion` 来自 `__APP_VERSION__`。

后端目前**没有**运行期版本接口：`system_get_shell_info` 返回的是数据目录、日志与
缓存占用，不含版本号。检查更新时直接读前端的 `__APP_VERSION__`、由前端把版本传给
后端（不再另造一份后端版本副本），理由与接口契约见
[update-check.md](./update-check.md)。两种取法拿到的是同一个数，因为来源只有
`pyproject.toml` 一处。

「关于」段当前写着"更新需要手动下载安装，应用本身不会联网检查或自动更新"。在检查
更新落地（§5）之前这句话是准确的，不要提前改掉。

## 3. Windows 安装方式

### 3.1 主路径：Inno Setup 6 per-user 安装器

| 项 | 值 |
| --- | --- |
| 安装器 | Inno Setup 6.3 或更新，脚本 [cubby.iss](Releases/cubby.iss) |
| 安装范围 | 当前用户（per-user，`PrivilegesRequired=lowest`），不需要管理员权限 |
| 默认安装目录 | `%LOCALAPPDATA%\Programs\Cubby` |
| AppId | 固定 GUID，升级时覆盖同一个安装目录 |
| 产物名 | `Cubby-Setup-<版本>.exe`，同批上传 `.sha256` |
| 卸载 | 默认**保留** `%LOCALAPPDATA%\Cubby` 下的数据；询问时选"是"才删除 |
| 前置依赖 | 安装时检测 WebView2 Runtime，缺失时提示并打开官方下载页 |

为什么 per-user 装到 `%LOCALAPPDATA%\Programs`：不需要 UAC 提权（安装、升级、卸载
都不会弹管理员确认），与"用户数据在 `%LOCALAPPDATA%\Cubby`"同属一个用户空间；
卸载时只动这个用户的东西。

升级沿用同一个固定 AppId 与同一个安装目录，安装程序走覆盖安装，用户数据根不变 ——
这就是"换版本不用搬数据"的落地方式。安装器同时设了
`CloseApplications=yes` / `RestartApplications=no`：升级时提示关闭正在运行的旧实例，
装完不自动把它拉起来（否则旧进程还占着刚被覆盖的文件）。

WebView2 Runtime 在 Windows 10/11 上通常已经随系统或 Edge 安装；检测的意义是
在没有它的机器上提前给出可操作的提示，避免"装完打不开、也没有报错"。检测**只提示、
不阻塞安装**：静默安装（`/VERYSILENT`）下没有界面，改为只记日志，不下载任何东西。

### 3.2 便携版：zip 继续保留

`Cubby-windows-x64.zip` 仍然发布，解压即用（不写注册表、不建快捷方式）。数据要跟着
目录走时，在可执行文件旁放一个空的 `portable.txt`，数据就落在该目录（§2.1 第 3 条），
整个目录拷走即完成迁移。不放 marker 时，解压目录里的程序也按默认数据根读写数据 ——
zip 不再是"数据一定跟着目录走"的保证，而是"可以这样用"。

zip 与安装器的分工：

| 形态 | 适合 | 数据位置 | 升级方式 |
| --- | --- | --- | --- |
| 安装器 | 日常使用 | 默认数据根 | 覆盖安装，数据不动 |
| zip 便携版 | 免安装、U 盘、单机多份副本 | 默认数据根；放了 `portable.txt` 则在目录旁 | 解压新版本覆盖/替换程序目录，数据仍按同样的规则解析 |

### 3.3 winget / Scoop

两者都依赖安装器产物，因此排在安装器之后：**设计已定稿、尚未实装**。要做的事是把
`Cubby-Setup-<版本>.exe` 与校验值登记进清单与 bucket，不改变本文的数据根与目录约定。
清单字段、ARP 对齐、凭据与自动更新时序见
[distribution-channels.md](./distribution-channels.md)。

## 4. Linux 分发

### 4.1 为什么这次不做 AppImage

打包链里没有 GTK / PyGObject 依赖，PyInstaller 只收集 Python 与前端产物；而
`main.py` 的桌面窗口在 Linux 上实际走不通。把 `--web-only` 的服务器形态塞进
AppImage，得到的只是一个"需要浏览器的桌面应用"，不如直接给压缩包与说明。

Linux 上的真实用法是服务器 / NAS：

```bash
python main.py --web-only        # 默认 http://127.0.0.1:18080
```

### 4.2 现在：tar.gz

`Cubby-linux-x64.tar.gz`（带 `.sha256`）仍是 Linux 的唯一产物：解压后执行
`Cubby/Cubby --web-only`（压缩包根目录是 `Cubby/`，可执行文件是其中的 `Cubby`），
用浏览器访问。它与 1.x 的差别只在数据位置（§2.1 的 Linux 默认数据根）。

### 4.3 后续：`install.sh` 与容器镜像（设计已定稿，尚未实装）

布局、参数、升级回滚、systemd unit、镜像与多架构的完整设计见
[linux-distribution.md](./linux-distribution.md)；本节只留方向。

- **`install.sh`**：解压到 `~/.local/lib/cubby`，在 `~/.local/bin` 放启动器，
  附一份 systemd unit 模板（用户级服务，`--web-only`），卸载脚本删除程序目录、
  保留数据根。目标是"不装 Python、不装 Node，一条命令起服务"。
- **容器镜像**：`ghcr.io` 上发布 `--web-only` 镜像，数据根挂载到卷；适合
  家庭服务器与 NAS 的常驻部署。

两者都还没有仓库内的脚本或工作流步骤，状态见 §8。暂不做 AppImage 的理由同 §4.1。

## 5. 更新检查（未实装，先定边界）

应用内检查更新尚未实装。完整的接口契约、失败分类、形态判定、状态机与落地拆分见
[update-check.md](./update-check.md)；本节只定不能越过的边界，避免实现时顺手做成
"静默自动升级"。

**接口**：`GET https://api.github.com/repos/Flotiarenor/Cubby/releases/latest`。
GitHub 的匿名调用限流是**每小时 60 次**（按来源 IP），这个数值决定了下面几条。

**版本比较**：取响应的 `tag_name`，去掉前缀 `v`，与运行期能拿到的版本比较。运行期
版本只在前端（`__APP_VERSION__`，见 §2.4），决定由前端把它传给后端、由后端执行检查
（不另造后端版本副本）。tag 由 `tools/check_version.py` 在发布时强制与 `pyproject.toml`
一致，所以比较两串版本号就够，不需要再查文件。

**四条边界**：

1. **只在用户点击时查**（入口放在「关于」段）。不轮询、不在启动时查：既没有必要，
   也会把匿名额度耗在后台请求上。
2. **只提示，不静默升级**。壳与内置插件是"必须同版本"的契约：只替换可执行文件
   而插件还是旧版，会出现壳调用插件时字段或钩子对不上。升级必须以"整个发行包"
   为单位，并由用户确认。
3. **下载后校验 `.sha256`**。发布产物附带的 `.sha256` 是唯一的完整性凭据 ——
   没有代码签名（§7），校验摘要是不被中间人替换成另一个安装包的最低要求。
4. **失败只记日志**。网络不可达、限流、JSON 形状变化都只写一行日志并在界面上
   保留"检查失败"的结果，不弹阻塞对话框、不影响启动。

**Windows 上不能原地替换**：运行中的 onedir 可执行文件被系统锁定，安装器也无法
覆盖。因此真正升级需要**独立的 updater**：先关闭壳、由外部进程替换安装目录、
再启动新版本。这一步属于后续实现，不在 2.0 范围内。

## 6. 发布链改动

### 6.1 `release.yml` 的 Windows build

Windows 构建（[release.yml](../.github/workflows/release.yml) 的 Windows build job）在原有
"PyInstaller → 校验产物 → 压缩"之后增加安装器步骤：

1. `choco install innosetup` 并定位 `ISCC.exe`（PATH 与默认安装目录都找不到就失败，
   不静默跳过），再编译 [cubby.iss](Releases/cubby.iss)：

   ```text
   iscc /DAppVersion=<版本> /DSourceDir=<仓库>\.build-dist\Cubby
        /O<仓库>\.build-dist docs/Releases/cubby.iss
   ```

   版本号取自 `tools/check_version.py --print`，产物是 `.build-dist\Cubby-Setup-<版本>.exe`；
   随后用与 zip 相同的命令生成同名 `.sha256`。安装器排在依赖快照之后编译，
   因此它铺的文件与 zip 里的内容一致。
2. **安装冒烟**：以 `/VERYSILENT` 装到一个临时目录（`/DIR` 覆盖默认位置，不污染
   runner 的默认安装位置），确认 `Cubby.exe` 与 `unins000.exe` 就位；再用卸载器静默
   卸载，并等安装目录真的消失。冒烟失败让 job 直接红，而不是只打印日志。
3. 安装器与 zip / tar.gz 分别上传 artifact，publish job 用 `merge-multiple: true`
   合并后按 `dist/*` 挂进草稿 Release。

冒烟与 `tools/check_build_tree.py` 的分工：后者校验的是 PyInstaller 产物目录
（必需文件、禁止内容、`HIDDEN_IMPORTS`），安装器打的是同一棵树，因此
`check_build_tree.py` 本身不需要为安装器改动。

### 6.2 资产清单

每次发布（草稿 Release，人工 Publish）同时提供：

| 资产 | 说明 | 校验文件 |
| --- | --- | --- |
| `Cubby-windows-x64.zip` | Windows 便携版 | `Cubby-windows-x64.zip.sha256` |
| `Cubby-linux-x64.tar.gz` | Linux 服务端形态（`--web-only`） | `Cubby-linux-x64.tar.gz.sha256` |
| `Cubby-Setup-<版本>.exe` | Windows per-user 安装器 | `Cubby-Setup-<版本>.exe.sha256` |

zip 与 tar.gz 的名字**不含版本号**，因此有稳定入口：

```text
https://github.com/Flotiarenor/Cubby/releases/latest/download/<资产名>
```

例如 `.../latest/download/Cubby-windows-x64.zip`。安装器的名字带版本号，
`latest/download` 同样可用，但要先知道当前版本（Release 页面、API 或
`tools/check_version.py --print`）才能拼出完整文件名。

### 6.3 门禁脚本的配合

- [check_build_tree.py](../tools/check_build_tree.py) **不变**：判据仍是"产物目录里必需
  文件齐全、不得含用户数据与字节码"。
- [check_docs.py](../tools/check_docs.py) 已支持两类新引用：`.iss` 作为仓库路径（安装器
  脚本要能写进 `AGENTS.md` 的文件表），`.exe` 作为产物名（安装器与发布文档会按名字提到
  `Cubby.exe`，它只存在于构建输出目录）。它只扫 `AGENTS.md`，因此这两条改动不改变
  文档链接的检查范围。

## 7. 取舍与风险

**没有代码签名 → SmartScreen 仍会提示。** 代码签名证书有持续成本与私钥保管责任，
且 OV 证书也不能立刻消除 SmartScreen 警告（要积累下载信誉）。现阶段能做的是：
提供 `.sha256`、在发布说明与 readme 里写清"点「更多信息」→「仍要运行」"的步骤。
签名作为后续项，不在 2.0 承诺。

**卸载删数据的风险 → 默认保留，删除需显式选择。** 卸载常常只是"重装"或"换个位置"
的前一步，此时删掉数据不可恢复；而数据根独立于安装目录，保留它不会影响重新安装。
因此默认保留 `%LOCALAPPDATA%\Cubby`，只有在卸载询问里选"是"才删除。

**`%LOCALAPPDATA%` 不进漫游。** 数据根里有缩略图缓存与索引、WebView 的浏览器
profile，以及本机访问令牌与设备身份；漫游目录会在每次登录时被复制到服务器与其它
机器，既放大了登录时间，也把本机凭据带出了这台机器。`%APPDATA%` 只在
`LOCALAPPDATA` 缺失时作为回退。

**不选 onefile。** onefile 每次启动都要把整个运行时解压到临时目录，启动变慢、临时
目录里会出现可写副本，杀软对"自解压到 temp 再执行"的形态也更敏感；而且它无法做
"替换安装目录里的文件"这种原地升级。onedir 的结果是安装目录里只读资源 + 数据根
分离，正好配合 §2。

**不自建更新服务器。** GitHub Releases 已经是唯一的分发渠道，额外一台服务器意味着
域名、证书、可用性与带宽的长期维护；匿名 API 每小时 60 次的额度对"用户点击才查"
绰绰有余。若将来需要灰度或分段发布，再评估。

**暂不做静默自动升级。** 壳与内置插件必须同版本（§5），静默升级一旦中途失败会留下
"壳换了、插件没换"或半个安装目录的状态；Windows 上还需要独立 updater 关闭并替换
正在运行的文件。用户点击 → 提示 → 用户确认，出错只影响这一次安装。

## 8. 落地状态表

| 步骤 | 状态 | 依据 | 下一步 |
| --- | --- | --- | --- |
| M1：数据根与安装目录解耦（`CUBBY_HOME` / 开发模式 / 便携 marker / 默认数据根） | **已实装** | [paths.py](../shell/backend/paths.py)、`tests/test_paths_user_data.py` | 由发布说明告知用户"升级不需要搬数据" |
| M2：Windows Inno Setup 安装器与发布链接入 | **已实装**（本机实测编译、静默安装与卸载） | [cubby.iss](Releases/cubby.iss)、[release.yml](../.github/workflows/release.yml) 的 Windows build（安装 + 冒烟 + `.sha256`） | CI 首跑确认 iscc 定位与冒烟；再做一次跨版本覆盖安装 |
| 应用内检查更新（A 检查与提示 / B 下载与校验） | 设计定稿，未实装 | [update-check.md](./update-check.md)，Lead 已定 2.0 只做 A、B | 按该文档 §12 的 A、B 两步落地 |
| 应用内一键升级（安装器路径 / zip 自替换） | 设计定稿，排到 2.1 | [update-check.md](./update-check.md) §7、§12 的 C、D | 先在真机验证覆盖安装与目录改名 |
| winget 清单 | 设计定稿，未实装 | [distribution-channels.md](./distribution-channels.md) | 先在临时机器用 `winget install --manifest` 试装 |
| Scoop bucket | 设计定稿，未实装 | [distribution-channels.md](./distribution-channels.md) §4 | 建 `Flotiarenor/scoop-cubby` 并试装 |
| Linux `install.sh` / systemd / `ghcr.io` 镜像 | 设计定稿，未实装 | [linux-distribution.md](./linux-distribution.md) | 按该文档 §8 的 A–E 段验收 |
| Linux 产物的 glibc 基线 | **2.0 发布阻断项** | [linux-distribution.md](./linux-distribution.md) §9 | 改为 `debian:12` 容器构建（`python3.11`，glibc 2.36），并在 Debian 12 / Ubuntu 24.04 上验证 |

「已实装」指代码与发布链在同一次改动里落地；「设计定稿」指方案与判据已写进对应文档、
还没有代码；「未实装」指只有约定、没有代码，不要按现状阅读。
