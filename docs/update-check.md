# 应用内检查更新（设计）

> 状态：计划中（设计定稿，无代码）。
>
> 已由 Lead 拍板的三条前置决定，本设计据此定稿：① 检查更新由后端执行，运行期版本由前端把
> `__APP_VERSION__` 作为入参传入 —— 不为后端另造一份构建期版本副本；② 2.0 只做 A（检查与提示）
> 与 B（下载与校验），C（调起安装器）与 D（zip 自替换）留到 2.1，先在真机验证覆盖安装与目录改名
> 再决定；③ 便携形态（可执行文件旁有 `portable.txt`）只给手动解压步骤，不做自替换。

本文定"点一下能不能知道自己是不是最新版、要不要让它替你装"的方案与判据。数据根与安装目录的
关系、安装器、资产清单见 [安装与更新](./install-and-update.md)；发布链路与门禁见
[CI、打包与发布](./ci-and-release.md)，已有的段落不在此复述。现状（供对照）：后端没有运行期
版本接口（[shell_info.py](../shell/backend/shell_info.py) 的 `get_info` 只返回路径、日志与缓存占用）；
「关于」段只有版本、许可与两个链接，文案写的是"应用本身不会联网检查或自动更新"，本文落地前
保持原样。

## 1. 目标与不做的事

**决定**：只做"用户点击 → 检查 → 提示 → 用户确认后执行"。不做静默自动升级，不在启动时检查，
不做后台轮询。

1. **壳与内置插件同版本**：发行包是一个整体（壳、`plugins/` 里的内置插件、前端产物一起构建）。
   只换可执行文件而插件还是旧版，表现为插件加载失败或壳调插件时字段对不上，所以升级只能是
   "整个发行包"的一次动作，并由用户确认。
2. **匿名额度有限**：API 每小时 60 次、按来源 IP 计。启动检查与轮询会把额度花在没人看的请求上，
   还会让用户真想看时恰好被限流。
3. **失败与"能不能用"无关**：网络不可达、限流、JSON 形状变化都只影响这一次点击。
4. **数据根分离让升级可逆**：安装目录只放只读资源、用户数据在独立数据根，升级只动安装目录，
   失败的结果最多是"版本没变"。唯一例外是放了 `portable.txt` 的便携目录（§5）。

## 2. 版本号来源

**决定**：检查更新在后端执行；运行期版本由前端把 `__APP_VERSION__` 作为入参传给后端，后端
**不**新增"读自己版本"的接口。

理由：前端常量已经存在（构建期由 [vite.config.ts](../shell/frontend/vite.config.ts) 的 `define`
注入）、已经显示在「关于」段（[SettingsView.vue](../shell/frontend/src/views/SettingsView.vue) 的
`appVersion`）。反过来，打包产物里没有 `pyproject.toml`（spec 的 `collect_data_files()` 只收前端
产物、`res/` 与 `plugins/`），后端要在运行期拿到版本就得再加一份构建期
注入，等于给"唯一来源 + [check_version.py](../tools/check_version.py) 校验两处副本"添第三个副本，
而那个脚本并不校验这样一份新副本。

边界：传参是界面提示的依据、不是权限依据（被改坏的版本串最多让提示不准）；开发模式
（`is_frozen()` 为假）直接返回不检查，那时 `__APP_VERSION__` 可能回退成 `'dev'`
（[env.d.ts](../shell/frontend/src/env.d.ts) 的声明与 `SettingsView.vue` 的兜底）；版本串形状固定
`X.Y.Z`，`[project].version` 由 `check_version.py` 强制等于 `package.json`、`package-lock.json`
两处副本，tag 必须是 `v<version>`（[ci-and-release.md](./ci-and-release.md) §3），比较前对
`tag_name` 只做一件事 —— 去掉**一个**前导 `v`，不去多个 `v`、不剪 `+` 后缀。

## 3. GitHub API 契约

**决定**：请求 `GET https://api.github.com/repos/Flotiarenor/Cubby/releases/latest`，读一个响应
就够，不查 `/releases` 列表。只用四个字段：`tag_name`（去 `v` 后比较）、`html_url`（「打开发布
页」）、`assets[].name`（与发布链产出的资产名比较，§5）、`assets[].browser_download_url`
（下载地址，也是 `.sha256` 的地址来源），外加 `assets[].size` 做下载前预检与进度（不参与完整性
判断）。

**草稿与预发布**：`draft` 与 `prerelease` 不会出现在 `/releases/latest` 里 —— 发布流程"自动建
草稿、人工 Publish"因此天然挡住了"被草稿提示升级"。据此**不要**自己拉 `/releases` 再挑一条：
那会拿到预发布，也会打破"草稿只有有权限的人可见"。

**匿名限流与 `User-Agent`**：匿名请求每小时 60 次、按来源 IP 计；GitHub 要求请求带
`User-Agent`，不带会被拒。`X-RateLimit-Remaining` 与 `X-RateLimit-Reset`（epoch 秒）既用于区分
失败原因，也用于算"还需等多久"。检查由用户点击触发、同一次运行内 60 秒不重复请求，60 次对
正常使用宽裕。失败分类与用户可见结果（一律只写一行日志，不抛到 `api_proxy` 的 500 分支）：

| 情况 | 判据 | 后端 | 界面 |
| --- | --- | --- | --- |
| 网络不可达 / DNS 失败 / 超时 | `requests.ConnectionError`、`Timeout` | `network` | 「检查失败：连不上 GitHub」+ 重试 |
| 限流 | 403 且 `X-RateLimit-Remaining` 为 `0` | `rate_limited`、`retry_after_seconds` | 「GitHub 限流，请 N 分钟后重试」，按钮禁用至到期 |
| 其它 401/403 | 状态码 401/403 但不是限流 | `forbidden` | 「检查失败」，日志带状态码 |
| 找不到发布 | 404 | `not_found` | 「没找到发布信息」 |
| JSON 形状变化 | 200 但 `tag_name` 非字符串、`assets` 非数组 | `bad_response` | 「检查失败」，日志写清字段与实际类型 |
| 其余异常 | 其它 | `unexpected` | 「检查失败」+ 重试 |

超时取 `timeout=(5, 15)`（连接、读），`Session.max_redirects = 5`。

## 4. 版本比较

**决定**：`X.Y.Z` 三段整数比较，不引入 `packaging` / `semver` 之类依赖。

解析用正则 `^(\d+)\.(\d+)\.(\d+)$`，先去一个前导 `v`，三段转整数后按元组比较：**相等** → 已是
最新；**远端更高** → 有新版；**远端更低**（用户在测试更新版本）→ 视为最新，不提示降级；
**无法解析**（`1.2`、`1.2.3-beta`、`v1.2.3.4`、`nightly`、`dev`）→ `bad_version`，界面只给发布页
链接，不给下载按钮。

理由：合法 tag 的形状由 `check_version.py` 在发版时强制成 `v<X.Y.Z>`，三段比较覆盖全部合法输入；
引通用版本库会把"预发布""本地版本号"这些不打算支持的形状也放进来，等于让 tag 的形状在运行期
被重新解释一遍，而三段比较的代码量小于它自己的测试。

## 5. 资产选择与形态判定

**决定**：先按顺序判定自己的形态，再按形态选资产；形态判定用文件系统，不读注册表。

| 顺序 | 判据 | 形态 | 资产 | 后续动作 |
| --- | --- | --- | --- | --- |
| 1 | `is_portable()` 为真 | 便携（数据在程序目录） | `Cubby-windows-x64.zip` | **只提示**，不下载、不自替换 |
| 2 | `(exe 目录 / 'unins000.exe')` 是文件 | 安装器装的 | `Cubby-Setup-<版本>.exe` | 调安装器（§7.1） |
| 3 | 其余冻结产物 | zip 解压、数据在默认数据根 | `Cubby-windows-x64.zip` | 目录交换（§7.2） |
| 4 | 未冻结 | 开发模式 | 无 | 不检查（§2） |

**为什么用 `unins000.exe`**：它是安装器自己写进 `{app}` 的卸载器，
[release.yml](../.github/workflows/release.yml) 的静默安装冒烟就断言它存在；zip 产物在编译安装器
**之前**打出来，所以 zip 里没有它。两者互斥，判据稳定。**注册表查得到，但不用它做判据**：
安装器用固定 AppId `{8C6E4A4F-3B1D-4A7E-9F52-0D2C7B5A6E13}`（[cubby.iss](Releases/cubby.iss)），
会在 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall` 下建卸载项（Inno 按 `<AppId>_is1`
命名），`InstallLocation` 就是安装目录；但读它要 `winreg`（Windows-only，测试里得造假注册表）、
键名依赖 Inno 的内部约定、目录被整体搬走后 `InstallLocation` 与实际位置不一致。而 `portable.txt`
与 `unins000.exe` 就在身边，与发布链的冒烟断言是同一件事。

**便携形态为什么拒绝自动升级**：便携模式的数据根就是程序目录 —— `.config/`、`data/`、
`.config/logs/`、用户自装 `plugins/` 全在 `Cubby\` 里（[paths.py](../shell/backend/paths.py) 的
`is_portable` 与 [install-and-update.md](./install-and-update.md) §2.2）。发布产物**禁止**含用户数据
（[check_build_tree.py](../tools/check_build_tree.py) 的 `FORBIDDEN_RELATIVE`），解压出的新树里没有
这些目录，于是"替换目录"等于把数据搬走，且程序与数据被绑在同一次磁盘操作里，搬到一半失败最难
恢复。所以便携形态只给手动步骤：解压新 zip **覆盖到同一个目录** —— 数据不在 zip 里，覆盖不会
删掉它们，旧版本删掉的文件会留成可接受的残留。

## 6. 下载与完整性

**决定**：下载到 `<数据根>/updates/`（可随时删除的临时区），校验通过才保留；没有代码签名时，
`.sha256` 是唯一完整性凭据。不落系统 temp 的理由：系统 temp 可能被清理程序删掉，而"已下载待
安装"要跨重启存活；放在数据根下也符合"运行期只写数据根"这条既有约定
（[shell/backend/AGENTS.md](../shell/backend/AGENTS.md)）。

- **权威来源**：同一个 Release 里的 `<资产名>.sha256` 资产，内容 `<十六进制摘要> <文件名>`
  （[release.yml](../.github/workflows/release.yml) 的生成命令就是 `print(hexdigest, name)`），不依赖
  响应里的其它摘要字段：`.sha256` 由发布链保证一定在，其它字段不保证。
- **校验步骤**：先下 `.sha256`（上限 4 KiB）→ 解析摘要与文件名 → 文件名必须等于资产名 → 分块算
  SHA-256 → 比较（忽略大小写）；不一致即删除文件并返回 `digest_mismatch`，不留半成品。
- **上限、超时**：`MAX_UPDATE_BYTES = 512 MiB`，同时对 `assets[].size` 预检；每写一块检查累计字节，
  超出即中止并删除。下载 `timeout=(10, 60)`，另加累计墙钟上限 900 秒，避免慢速滴流把界面永远
  卡在"下载中"。
- **HTTPS 与重定向**：起始地址必须 `https://`；跟随重定向但最终地址也必须是 `https://`，主机只能
  是 `github.com` 或 `*.githubusercontent.com`；跳去 `http://` / `file://` 判 `bad_redirect` 并删除
  已下载部分。
- **清理与单动作**：失败、取消、安装结束（成功或失败）都清 `updates/` 下的对应文件；`Cubby.new`
  与 `Cubby.old` 只在交换流程内出现（§7.2）；下载与安装共用进程内锁，第二个请求直接返回"已有
  升级动作在进行"。

为什么摘要是唯一凭据：仓库没有代码签名（[install-and-update.md](./install-and-update.md) §7），OS
层面没有东西能证明"这个 exe 来自本仓库"；HTTPS 只保护传输，换不了"发布页上挂的是什么"。而
`.sha256` 与资产同批生成、同一个 Release 发布，校验通过至少排除了下载过程中被替换或截断。

## 7. Windows 升级执行

共同前提：动作开始前用户已在界面上确认过目标版本与资产名（§9）。

### 7.1 安装器形态：调起已下载的 `Cubby-Setup-<版本>.exe`

**决定**：应用在**退出阶段**启动安装器，然后自行退出；装完由用户重新打开。参数
`/SILENT /NORESTART /LOG=<数据根>/logs/setup-<版本>.log`，`cwd` 设为 `updates/`，
`creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP`，使子进程在父
进程退出后继续。不用 `/VERYSILENT`：用户刚点过"立即升级"，看不到窗口会被当成没反应；不加
`/SUPPRESSMSGBOXES`：真失败时让安装器把原因报出来。

时序三段：① 写 journal `<数据根>/updates/pending.json`（目标版本、资产名、摘要、时间），用
[json_cache.py](../shell/backend/json_cache.py) 的 `write_json` 原子写；② 触发退出 ——
`webview.windows[0].destroy()` 关窗，`webview.start()` 返回后走 [main.py](../main.py) 既有收尾
（`finally` 的 `_unload_plugins`、`atexit` 的 `_commit_last_route`）；③ 安装器由 `atexit` 回调启动。
用 `atexit` 而不是"先起安装器再退出"，是为了让顺序确定：所有插件 `on_unload` 跑完（SQLite/WAL
关闭、插件线程停止）才让安装器开始覆盖文件。

为什么必须先退出：onedir 产物的 exe 与 DLL 在运行期被映射，Windows 不允许覆盖写。安装器的
`CloseApplications=yes`（[cubby.iss](Releases/cubby.iss)）是"用户忘了退出"的兜底，不是正常路径；
`RestartApplications=no` 是发布链的既有决定，因此界面必须提前写清"安装完成后请重新打开 Cubby"。

失败可见性：下次启动读 `pending.json` 与运行版本比较 —— 运行版本已不低于目标版本则删除 journal
并记一行"升级完成"；否则保留，并在「关于」段显示"上次升级未完成"，附发布页链接与「打开日志
目录」（`system_open_log_dir` 已存在，见 [shell_info.py](../shell/backend/shell_info.py)）。全程不弹
阻塞框。

### 7.2 zip 形态：退出后的目录交换

**决定**：只对"未安装、且没有 `portable.txt`"的 zip 形态做自替换；辅助进程是同一份 `Cubby.exe`
的新开关，交换失败必须能回滚。不新增第二个可执行文件、不加第二份 PyInstaller spec：辅助进程就是
`<安装目录>\Cubby.exe --apply-update <journal>`，走 [main.py](../main.py) `_argv_value` 的同一套解析；
该分支放在日志与配置加载之前，只做文件操作（不加载插件、不起 Flask）。

前置动作（应用仍在运行）：① 下载并校验 `Cubby-windows-x64.zip`（§6）；② 解压到**安装目录旁边**
的 `Cubby.new`（必须同卷，跨卷 `rename` 会失败），解压时逐条目校验路径 ——
`PurePosixPath(name).parts` 里出现 `..`、绝对路径或盘符一律拒绝（`zipfile` 的 `filter='data'` 要到
Python 3.12，或 3.11.4 及之后的 3.11 才有，而 `requires-python` 下探到 3.11，不能假定它存在）；③ 断言 `Cubby.new\Cubby.exe` 存在，写
journal `<数据根>/updates/apply.json`（安装目录、`Cubby.new`、`Cubby.old`、父进程 PID、目标版本）；
④ detached 启动辅助进程，然后退出。

交换步骤（每步都写日志）：① 轮询父进程 PID 直到消失，上限 60 秒，超时判失败；② 确认 `Cubby` 与
`Cubby.new` 都在、`Cubby.exe` 可改名；③ `Cubby` → `Cubby.old`（重试若干次：杀软与索引服务的句柄
是毫秒级的）；④ `Cubby.new` → `Cubby`；⑤ 第 ④ 步失败则回滚 `Cubby.old` → `Cubby`，用旧路径启动
程序并在 journal 记失败；⑥ 成功则用 `Cubby\Cubby.exe` 启动新版本，journal 记完成。

旧目录的保留与清理：`Cubby.old` 由**新版本下次启动**删除（此时没有进程在它里面运行），删不掉就
保留到下一次，只记日志、不阻塞启动。辅助进程自己运行在 `Cubby.old` 里，删不了自己所在的目录
—— 这就是"下次启动清理"的直接原因。

为什么不能原地覆盖运行中的 onedir：[install-and-update.md](./install-and-update.md) §5 末段已说明；
这里补一条可执行判据：Windows 允许对运行中映像所在的文件/目录**改名**，不允许覆盖写或删除，所以
"两次改名 + 启动新 exe"可行，"把 zip 解开覆盖过去"必然在一半文件上失败。本机实测（Windows 11，
最小复现：把 `cmd.exe` 复制到临时目录并运行 `cmd /c timeout /t 30`）：该目录用 `Move-Item` 整体
改名成功，而向改名后的同名 exe 覆盖写报 `The process cannot access the file ... because it is being
used by another process`。

失败可见性：journal 保留 `state: 'failed'` 与失败步骤，下次启动在「关于」段显示"上次升级未完成
（当前仍是旧版本）"，仍可重新检查。数据不受影响：这种形态的数据根是默认数据根，`Cubby` 目录里
本来就没有用户数据。

## 8. Linux

**决定**：2.0 **不**纳入应用内检查更新。Linux 只有 `Cubby-linux-x64.tar.gz`（`--web-only` 服务器
形态）：容器部署的升级动作是拉镜像、发生在应用进程之外；tar.gz 部署的升级动作是解包与重启服务、
时机由运维决定 —— 应用内"检查"给出的结论没有可执行的后续动作。更实际的一点：`--web-only` 通常挂
在反代后面常驻运行，让应用自己重启会断掉正在用的连接，而它由谁托管（systemd、容器）应用并不知道。

2.0 只做两件事：发布说明与 [readme.md](../readme.md) 写清下载与 `sha256sum -c` 校验；Linux 上后端
返回 `disabled`（原因 `platform`），「关于」段不出现检查按钮。将来若纳入，只做"提示 + 给出资产名
与校验命令"，不做自替换。

## 9. 界面与状态机

**决定**：入口在设置页「关于」段，结果一律写在段内状态行，不弹 modal。新增元素（计划中）：按钮
`id="settings-check-update"`（沿用 `.btn`）、状态行 `id="settings-update-status"`（沿用
`.field-help`）；现有 `RELEASES_URL`（`https://github.com/Flotiarenor/Cubby/releases/latest`）继续
作为手动下载出口，不新增第二个常量。

| 状态 | 进入条件 | 界面 | 可用动作 |
| --- | --- | --- | --- |
| `idle` | 打开设置页 | 现有版本行 + 「检查更新」 | 检查 |
| `checking` | 点击后 | 「正在检查…」，按钮禁用 | 无 |
| `up_to_date` | `latest == current` | 「已是最新版本（X.Y.Z）」 | 60 秒后再检查 |
| `update_available` | `latest > current` 且有对应资产 | 目标版本、资产名、大小；安装器/zip 形态给「下载并升级」，便携与无资产形态给发布页链接 | 下载 / 取消 |
| `downloading` | 下载中 | 进度百分比（按 `assets[].size`） | 取消 |
| `ready` | 下载并通过 `.sha256` | 「已下载并通过校验」+「立即退出并安装」+「稍后」 | 安装 / 稍后 |
| `error` | 任一类失败 | 一行原因 + 「重试」；`rate_limited` 显示"请 N 分钟后重试"并禁用按钮至到期 | 重试 |
| `disabled` | 开发模式 / Linux | 说明为什么不检查 | 打开发布页 |

文案要求：成功与失败都用段内状态行、不用 toast（用户是来看结果的，toast 会飘走；`toastError` 只
用于"按钮动作抛错"，与设置页既有写法一致）；失败不弹阻塞框、不影响其它设置项，只 `log.warning`
一行；「有新版」必须写清将要发生什么 —— 安装器形态写"安装器会关闭 Cubby 并覆盖安装，数据不受
影响"，zip 形态写"将替换程序目录，数据在默认位置不受影响"，便携形态写"请解压覆盖到同一目录，
数据在程序目录里"；落地时把那句"应用本身不会联网检查或自动更新"改成"由你点击触发检查，只提示，
不自动安装"。

状态只在内存里，离开设置页即重置；只有 `pending.json` / `apply.json` 落盘（§7）。限流时后端把
`retry_after_seconds`（由 `X-RateLimit-Reset` 算出）一并返回供前端倒计时；同一次运行内 60 秒内的
重复点击直接复用上一次结果，不发第二次请求。权限：这些端点做的是"对这台机器动手"（下载、启动
安装器、换目录），与 `system_pick_path`、`system_open_log_dir` 同类，**必须**同时登记进
[file_server.py](../shell/backend/file_server.py) 的 `_ADMIN_ONLY_API`；非管理员调用返回 403，界面
提示"只有管理员可以检查更新"。

## 10. 配置与隐私

**决定**：2.0 不新增任何设置项，也不做镜像配置；目标 URL 与资产 URL 都是模块级常量。理由是镜像
把"从哪里下载"变成用户可改的安全面 —— 摘要来自镜像时，摘要只证明"两个文件一致"，不再证明"文件
来自本仓库"，且镜像本身还要长期维护 `.sha256` 同步与可用性。将来确实需要（企业内网预先镜像）时
只认环境变量 `CUBBY_UPDATE_MIRROR`：默认空 = 直连；生效范围仅"替换 `browser_download_url` 的
前缀，`.sha256` 从同一前缀取"；界面不暴露、不写进 `shell.json`；启用时「关于」段必须显示"通过
镜像下载"。这条不进 2.0。

隐私边界：只访问 `api.github.com`，以及 `github.com` / `*.githubusercontent.com` 上的资产地址；
`User-Agent` 写成 `Cubby/<版本> (+https://github.com/Flotiarenor/Cubby)`；不带访问令牌、不带主机名 /
用户名 / 数据根路径，不上报任何使用统计；日志只记 URL 与状态码。用户机器上的 `HTTP(S)_PROXY`
照常生效（不像本机探活那条要显式禁用代理），这是用户自己的网络选择。

## 11. 测试策略

**决定**：新增 `tests/test_update_check.py`（不以插件前缀开头，自动落到 `tools/plugin_ci.py --core`，
进 CI 的 test job、两个平台都跑），用本地假 HTTP 服务覆盖分支；不真下载发布资产，不在 CI 里跑
安装器。假服务沿用仓库已有写法 `ThreadingHTTPServer(('127.0.0.1', 0), handler)` +
`BaseHTTPRequestHandler`，见 [tests/test_document_reader_tts.py](../tests/test_document_reader_tts.py)；
被测函数接受"API 基地址"参数（默认是常量），这是为可测性留的唯一注入点。

| 分支 | 假服务怎么造 | 断言 |
| --- | --- | --- |
| 有新版本 | 200 + `tag_name: v9.9.9` + 两个资产 | `update_available`，选中资产名正确 |
| 无新版本 | `tag_name` 等于当前版本 | `up_to_date` |
| 限流 | 403 + `X-RateLimit-Remaining: 0` + `X-RateLimit-Reset` | `rate_limited` 且 `retry_after_seconds` 合理 |
| 超时 | 处理器 sleep 超过读超时 | `network`，一行日志，不抛异常 |
| 摘要不符 | `.sha256` 与文件内容不符 | 文件被删除，`digest_mismatch` |
| 资产缺失 | `assets` 里没有该平台资产，或没有 `.sha256` | `no_asset`，不发起下载 |
| JSON 形状变化 | `{}`、`assets` 是对象、`tag_name` 是数字 | `bad_response`，不抛异常 |
| 版本串不可解析 | `tag_name: 'nightly'` | `bad_version`，不提示升级 |
| 下载超上限 | `Content-Length` 超过上限 | 中止并删除半成品 |
| 重定向到非 https | 302 跳到 `http://` 或 `file://` | `bad_redirect` |

必须真机或 e2e：安装器覆盖安装（N → N+1）—— [release.yml](../.github/workflows/release.yml) 的冒烟
只证明"装得上、卸得掉"，升级要在真机跑一次，确认数据根不变、插件设置保留、`cubby.log` 出现
"升级完成"；zip 自替换 —— 目录改名在真机上的行为（杀软、索引服务、长路径）、回滚后旧版本仍能
启动；便携形态 —— 放一个 `portable.txt` 确认界面不出现「下载并升级」。

不测：不真下载大文件（用几 KB 的假文件）；不在 CI 里启动安装器（需要交互式桌面）；不测 GitHub
对草稿/预发布的过滤（那是 GitHub 的行为，不在我们的代码里）。门禁接入不改 workflow：
`tools/check_tests.py` 会检查新文件真的产出用例；前端状态机不进 `tests/js/*.mjs`，因为它只做展示，
判定逻辑都在后端。

## 12. 落地拆分与验收

| 步骤 | 范围 | 验收（可执行或可观察） | 进 2.0 |
| --- | --- | --- | --- |
| A 检查与提示 | 后端 `shell/backend/update_check.py`（检查 + 版本比较 + 资产选择 + 形态判定）；方法表注册两处（[file_server.py](../shell/backend/file_server.py) 的 `api_proxy` 与 [main.py](../main.py) 的 `shell_methods`）；登记 `_ADMIN_ONLY_API`；前端「关于」段状态机（`idle`/`checking`/`update_available`/`up_to_date`/`error`/`disabled`），不碰下载 | `python -m unittest tests.test_update_check -v` 通过；开发模式点击显示"开发模式不检查更新"；真机点击后 `cubby.log` 出现一行检查结果；「关于」段文案已替换 | 是 |
| B 下载与校验 | `<数据根>/updates/`、`.sha256` 校验、大小与超时上限、失败清理、`ready` 状态 | 假服务用例覆盖摘要不符/超上限/重定向；下载成功后 `updates/` 里只有通过校验的包；取消与失败后不留半成品 | 是 |
| C 安装器路径 | 启动安装器、`atexit` 时序、`pending.json`、下次启动的完成/未完成判定 | 真机 N → N+1 后数据根不变、设置保留、日志出现"升级完成"；中途杀掉安装器后下次启动显示"上次升级未完成" | 否，2.1 |
| D zip 自替换 | `--apply-update` 分支、`Cubby.new` 解压与路径校验、目录交换、回滚、`Cubby.old` 清理 | 真机（zip 解压、无 `portable.txt`）升级成功且数据仍在默认数据根；把 `Cubby.new` 弄成只读制造交换失败，回滚后旧版本仍可启动；有 `portable.txt` 时只给发布页链接 | 否，2.1 |
| E 镜像 | 环境变量 `CUBBY_UPDATE_MIRROR` | 设置该变量后请求前缀指向镜像；未设置时 A、B 的验收完全一致 | 否，按需 |

A 与 B 合起来就是 [install-and-update.md](./install-and-update.md) §8 状态表里"先做检查 + 提示 +
跳转下载"的那一步；C、D 是"updater 另立"的部分，必须在真机上验证过再决定是否进安装器与 zip 两条
路径的默认行为。
