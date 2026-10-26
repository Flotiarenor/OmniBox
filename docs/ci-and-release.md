# CI、打包与发布

本文说明 OmniBox 的质量门禁、打包链路与发布流程，以及"下一步该怎么收紧"。
配套改动见同批提交；命令都在本机 Windows + venv 环境实测过。

---

## 1. 门禁一览

| 门禁 | 命令 | 现状 |
| --- | --- | --- |
| 静态检查（全量） | `python -m ruff check .` | **0 错误**（硬门禁） |
| 插件规范 | `python tools/check_plugins.py` | **exit 0**（硬门禁） |
| 打包规则（spec/收集） | `python tools/check_packaging.py` | **exit 0**（硬门禁） |
| 版本一致性 | `python tools/check_version.py` | **exit 0**（硬门禁） |
| 测试文件可运行性 | `python tools/check_tests.py` | **exit 0**（硬门禁；`tests/test_*.py` 必须能被 unittest 收集到用例，手工脚本按约定命名 `tests/debug_*.py`） |
| 文档引用一致性 | `python tools/check_docs.py` | **exit 0**（硬门禁；每个 `AGENTS.md` 反引号里的名字/路径、围栏命令里的路径、Markdown 链接都必须还存在。插件界面文档只查后两类，理由见脚本头部） |
| 本地隐私门禁 | `python tools/check_private_paths.py` | **开发机硬门禁，不进入 CI**：runner 没有本地配置，必然 SKIP。见 §1.1 |
| 类型检查（内核） | `python -m pyright main.py shell tools` | **0 错误**（硬门禁） |
| 类型检查（插件+测试） | `python -m pyright plugins tests` | **本地按需**；历史基线不再作为 CI 步骤（见 §6） |
| 单元测试 | `python tools/plugin_ci.py --core` + `--plugin <name>` | **failures=0 / errors=0**（硬门禁；CI 按插件矩阵拆分，见 §2；本地仍可跑全量 `unittest discover`） |
| 运行时禁止 print | `python -m unittest tests.test_no_print_in_runtime` | 通过（硬门禁） |
| 前端转义一致性 | `node tools/check_frontend_escape.cjs` | **OK**（硬门禁） |
| 前端依赖漏洞 | `python tools/check_npm_audit.py` | **OK**（硬门禁；脚本内固定官方 registry，见下） |
| 前端类型+构建 | `npm --prefix shell/frontend run build` | 通过（硬门禁） |
| 图标 sprite 一致性 | `python tools/build_icons.py --check` | **OK**（硬门禁；校验 `res/icons/icons.svg` 与 `res/icons/icon_data.json` 同步，且模板/manifest 引用的图标全部已冻结） |
| 打包冒烟 + 产物校验 | `python tools/check_build_tree.py <dist>/OmniBox --expect-exe OmniBox.exe` | 通过（硬门禁；CI 里只在 push main / 手动触发 / 打包路径变更时跑） |

> `unittest` 这一行里包含插件前端的脚本契约用例：`tests/test_*_js.py` 包装器调用
> `tests/js/*.mjs`（无 node 时自动 skip），覆盖 `image_viewer_app_split` /
> `media_player_app_split` / `plugin_asset_contract`（7 个插件前端的资源契约）/
> `shell_folder_picker` / `image_viewer_roots_list` / `image_viewer_extension_view` /
> `shell_settings_form` / `image_cleaner_scan`。
>
> 本机 Linux 实测 `skipped=70`，都属"环境不具备时条件跳过"，不是门禁失效：
> 浏览器 e2e（需 selenium + Chrome/Edge，见 `requirements-e2e.txt`，刻意不进 CI）、
> Windows 专属（DPAPI、NTFS 大小写、`\\?\` 扩展前缀、netease-music 的 `.cmd` shim）、
> 无系统离线音色时的 TTS 用例。

本地一次性跑全部（PowerShell）：

```powershell
$py = ".\venv\Scripts\python.exe"
& $py -m ruff check .
& $py tools/check_plugins.py
& $py tools/build_icons.py --check
& $py tools/check_packaging.py
& $py tools/check_version.py
& $py tools/check_tests.py
& $py tools/check_docs.py
& $py tools/check_npm_audit.py
& $py tools/check_private_paths.py
& $py -m pyright main.py shell tools
& $py -m unittest discover -s tests
node tools/check_frontend_escape.cjs
```

### 1.1 本地隐私门禁（`tools/check_private_paths.py`）

拦的是"把开发机上的东西提交上去"：本地运行期配置（`.config/plugins/*.json`、`.config/app.yaml`、
`.config/auth_token.txt`、`data/group-mesh/identity/*.json`）**不在 git 里**（`.gitignore` 覆盖），
但里面的真实路径与令牌很容易在写文档、注释、用例时被顺手复制进去，而这类改动其它门禁完全
看不见（ruff 不扫字符串，`check_plugins` 只看插件契约，打包规则只查数据收集）。

判定：从本机配置提取三类词条，再逐个在 `git ls-files` 的文本文件里找 ——

1. **绝对路径**（含各级父目录与后缀），比较前统一 `\`→`/`、去掉分隔符、小写化，
   因此 `D:\图库`、`D:/图库`、`D:\\图库` 命中同一条词条；回环地址/主机名这类通用值不算词条；
2. **密钥值**：键名匹配 `token|secret|password|api_key` 且长度 ≥ 6 的字符串值，以及
   `auth_token.txt` 的整份内容（按原文匹配）；占位符形状（`SECRET-TOKEN`、`<your-key>`）不算；
3. **用户名**：`%USERPROFILE%` 最后一段，只在路径上下文（`users/<名字>`）里算命中。

实测价值：实装当天用它扫出 8 处真机路径（本文档、一份插件设计文档、
`plugins/group-mesh/backend/shares.py` 的注释、以及 3 个用例文件），全部改成 `D:\…` 占位。
已知边界：按**行文本**比对，把路径拆成多段字符串拼接后检测不到（用例把这条边界固化了）。

> 它只在开发机运行，不再占用 CI：runner 上没有本地配置，必然打印 `SKIP` 并以 0 退出，
> 那个步骤永远不会失败。脚本本身仍由 `tests/test_check_private_paths.py` 和本地提交前
> 检查覆盖，语法/导入错误不会因为不进 CI 而漏掉。

> `check_npm_audit.py` 为什么不能用一句 `npm audit` 代替：本机 `npm config get registry`
> 指向 npmmirror，而它**不实现 audit 端点**，直接跑得到的是
> `[NOT_IMPLEMENTED] /-/npm/v1/security/* not implemented yet` —— 工具在跑、结论为空，
> 这个仓库因此从来没检查过前端依赖漏洞（实装当天查出 5 条）。脚本内部固定
> `--registry=https://registry.npmjs.org`，不依赖使用者配置；registry 不可达时跳过而非判失败。
> 当前有 4 条例外（vite 三条 + esbuild 一条，均为 dev server 场景，只能随 vite 8 消除），
> 登记在 `tools/check_npm_audit.py` 的 `ACCEPTED`；**登记项会自动过期**：上游修好后不删，
> 门禁会因为它"已不在报告里"而报错。Vite 跨大版本升级见 §8。

---

## 2. 工作流

### `ci.yml` —— 每次 push / PR（纯文档改动除外）

| job | runner | 内容 |
| --- | --- | --- |
| `changes` | ubuntu | 变更集判定：算出这次只动了文档还是动了代码，其余 job 靠它的输出决定跑不跑 |
| `plugins` | ubuntu | 从 `plugins/*/manifest.json` 读出插件名，输出 JSON 矩阵；不维护第二份插件清单 |
| `lint` | ubuntu | ruff + 插件规范 + 打包规则 + 版本一致性 + 图标 sprite + 测试文件可运行性 + 文档引用一致性 |
| `docs` | ubuntu | **只在变更集含 `.md` 时跑**：文档引用一致性（`tools/check_docs.py`） |
| `typecheck` | windows | pyright 内核（硬门禁）；不再输出“插件/测试基线”这种不会失败的报告 |
| `test` | windows + ubuntu，py3.10 + 3.12 | 共享测试（`tools/plugin_ci.py --core`，不含插件专属测试） |
| `plugin` | windows + ubuntu，py3.12 | **每个插件一条 job**：`tools/plugin_ci.py --plugin <name>`；失败直接落到插件名 |
| `frontend` | ubuntu | 转义门禁 + `npm ci` + `vue-tsc --noEmit` + `vite build` |
| `package` | windows + ubuntu | 真实跑 PyInstaller + 校验产物内容；**只在 push `main` / 手动触发时跑**（日常 push 由 `package-smoke` 按路径兜底，产物仅作 artifact） |

**按变更集决定跑哪些 job，而不是在 `on:` 上过滤工作流**：

| 变更集 | 跑什么 |
| --- | --- |
| 只有 `.md` | `changes` + `docs`（约 10 秒） |
| `.md` + 代码 | 全跑 |
| 只有代码 | 除 `docs` 外全跑 |

判定规则只有两条分支（`changes` job）：`*.md` 算文档，**其余任何文件都算"动了代码"**——
包括 `docs/Releases/**`（是 PyInstaller spec 与打包资源，属构建输入而非文档）、
`.github/workflows/**`（工作流自身改动必须验证，改坏一次会静默失去所有门禁）、
`tools/**` 与 `tests/**`。`LICENSE` / `.gitignore` / `.gitattributes` 这类少见改动因此
也会跑全量：宁可多跑一次，不要少跑一次。取不到变更集（强推、比对点缺失）时同样按
"动了代码"处理。

**为什么不在 `on:` 上用 `paths` / `paths-ignore`**：被路径过滤掉的工作流根本不启动，
也就不会创建 check run——一旦本工作流被设为必需检查，PR 会永远停在
*Expected — Waiting for status to be reported*（GitHub 没有"缺失的检查算通过"这个开关）。
改用 job 级 `if:` 之后，被跳过的 job 以 `Success` 上报，必需检查不会被卡住。

**纪律**：新增 job 必须同时写 `needs: changes` 与
`if: needs.changes.outputs.code == 'true'`（或 `...docs == 'true'`）——漏写就是
"每次全跑"或"永远不跑"。

「只改注释/格式」的代码改动**无法按路径识别**，只能照常跑全量——这是刻意接受的
代价：判定宁可多跑，不要少跑。

**日常 push 实际跑的 job**：`changes` + `plugins` + `lint` + `typecheck` + `test`×4 +
`plugin`×（插件数 × 2 平台，当前 8 个插件 = 16）+ `frontend`。`package`×2 被 `if:`
关在 push `main` / 手动触发上（被跳过的 job 仍会列在运行页面上显示 `skipped`，但不消耗
runner）；打包路径相关的回归由 `package-smoke.yml` 按路径触发兜底，两者跑的是同一套
spec + `check_build_tree.py` 产物校验。插件矩阵按插件拆分，失败日志只包含该插件的测试；
新增插件只要按 `test_<插件名下划线>_*` 命名测试文件，不需要改 `ci.yml`。

**从 CI 删除的“不可能发生”步骤**：

- `ruff format --check` 曾经带 `continue-on-error: true`，永远不会让 CI 失败；它只是
  输出差异，留在 CI 里会制造“已经检查过格式”的错觉。格式化检查仍在本地按需运行。
- `pyright plugins tests` 曾经带 `continue-on-error: true`，同样永远不会失败；插件与
  测试的类型基线不再作为 CI 步骤输出。
- `check_private_paths.py` 只在开发机上读取 `.config/plugins/*.json`；runner 上这些
  文件不存在，步骤必然 SKIP。它保留在本地提交前检查，不再占用 CI job。

**插件测试如何归属**：`tools/plugin_ci.py` 从 `plugins/*/manifest.json` 读取插件清单，
按 `test_<插件名下划线>_*.py` 前缀划分；`test_reader_*` 是 document-reader 的历史
别名，只在脚本里登记这一条别名。`--core` 跑其余共享测试，`--plugin` 跑单个插件测试。
两份选择器互斥且覆盖全部 `tests/test_*.py`，由 `tests/test_plugin_ci.py` 断言。
**为什么必须有 `windows-latest`**：历史缺陷里有一批是 Windows 专属的——SQLite
临时目录被占用导致清理失败（`WinError 32`）、`PATH` 分隔符写死 `:`、文件锁定。
只跑 Linux runner 会把这些永久掩盖。

**反而要防"只在 Windows 成立"的测试**：`tests/test_netease_music_command.py`
里伪造 npm 布局的那 4 项依赖 `.cmd` shim（插件只支持 Windows），在 Linux 上
`@unittest.skipUnless(os.name == 'nt')` 整体 skip；同一文件里
`_reject_shell_meta` 的纯函数测试各平台都跑。文件路由测试里的越界载荷也用
`os.name` 选分隔符——POSIX 上反斜杠是合法文件名字符，写死 `..\..\x` 会得到
404 而非穿越，从而在 Linux 上假失败。

### `package-smoke.yml` —— 打包链路冒烟

只在 `docs/Releases/**`、`requirements*.txt`、`pyproject.toml` 等打包相关文件
变动时触发，目的是让"打包坏了"在 PR 阶段暴露，而不是等到打 tag。

### `release.yml` —— 半自动发布（自动构建，人工公开）

```
推 v* tag → 校验版本 + 全量门禁 → 双平台构建 → 建【草稿】Release
                                                      │
                                你在 Releases 页面核对 → 点 Publish → 才对外可见
```

- **触发**：`push: tags: ['v*']`。打 tag 就是"我决定发这个版本"的声明；
  `workflow_dispatch` 保留作手动兜底（重跑某次发布、或只跑 `dry_run` 取产物）。
- **自动做完**：版本一致性校验（tag ↔ pyproject ↔ package.json ↔ package-lock.json）、ruff / 插件规范 /
  打包规则 / 图标 sprite 一致性 / 测试文件可运行性 / 全量单测 / 前端转义门禁、
  双平台 PyInstaller 构建、`check_build_tree.py` 产物校验、打 zip / tar.gz、
  生成 `.sha256`、建**草稿** Release 并挂上产物。
- **留给人**：只有"公开"这一下。草稿不进 Releases 列表、不产生 `latest`、不发通知，
  只有对仓库有写权限的人能看到。核对后点 **Publish release**
  即可（或 `gh release edit <tag> --draft=false`）。
- **已发布的不会被静默覆盖**：重跑时若该 tag 的 Release 已发布，publish 直接报错退出；
  草稿状态则覆盖刷新产物与说明。
- **guard**：tag 指向的提交必须已经在 `main` 上，否则 `verify` 第一步就失败 ——
  避免给"还没进主干、没经过 CI"的提交发版。
- **构建源**：工作区会切到该 tag，不会拿分支的代码发 tag 的名。
- **`dry_run`**（仅手动触发时可用）：只构建 + 上传 artifact（7 天过期），
  连草稿都不建，适合先把 Linux 产物取下来自己验证。

> 首次使用需确认仓库 `Settings → Actions → General → Workflow permissions`
> 为 **Read and write permissions**（创建 Release 需要）。
> `workflow_dispatch` 的入口只在**默认分支**（`main`）上渲染，所以这个文件改完
> 要让 `main` 也带上，Actions 页面才看得到 Run workflow 按钮。

---

## 3. 版本号唯一来源

- 唯一来源：`pyproject.toml` 的 `[project].version`。
- `shell/frontend/package.json` 的 `version` 必须与之相同；`shell/frontend/package-lock.json`
  里的两处副本（根 `version` 与 `packages[""].version`）同样必须相同（`check_version.py`
  校验）。`npm ci` 只校验依赖是否与锁文件一致，根 `version` 不在其中 —— 这两处副本没有
  别的守卫。
- tag 必须写成 `v<version>`；`release.yml` 会把 tag 与**它指向提交**里的
  pyproject 版本对齐检查，不一致时第一步就失败，**不会**产出名字与版本不符的安装包。
- 打 tag 会**触发构建**（全量门禁 + 双平台产物 + 建草稿 Release），但**不会公开**：
  公开是另一下人工动作（点 Publish）。所以 tag 是"决定发版"，Publish 是"决定对外可见"。
- 取值给脚本用：`python tools/check_version.py --print`（只输出裸版本号）。

### 何时升哪一位

判断口径只有一句：**别人的代码需要改才能继续用，就是 major；别人一行都不用改、但能用到
新东西，就是 minor；其余是 patch。**

- **major（破坏兼容）**：老的 `.config/` / `shell.json` 读不出来、已装的插件加载失败、
  设置键改名后旧值丢失、插件契约里已有的字段或钩子被移除或改语义。升 major 时必须在
  [docs/Releases/AGENTS.md](Releases/AGENTS.md) 的规则里写清迁移动作（用户要做什么），
  不能只改数字。
- **minor（公开契约变化）**：插件契约（`manifest.json` 字段、`PluginBase` 的钩子、
  `plugin_manager` 暴露给插件的 API）、壳的路由与设置键、前端共享资源的契约
  （`public/shell/**` 的 DOM id、CSS 变量、`data-*` 属性）**新增**或改语义。
  单个插件自己的版本号跟它自己的契约走：插件的公开契约变了，插件 `manifest.json` 的
  `version` 升 minor（插件是独立分发单元，与壳的版本号无关）。
- **patch（不改变契约）**：只改实现、修缺陷、调整界面细节、改文档与注释。
- 拿不准时看**有没有第二方读它**：有别的插件或用户配置依赖这个形状，就按契约变更算；
  只在壳内部调用，按 patch 算。

---

## 4. 打包链路

```
前端构建 → PyInstaller（--onedir）→ 校验产物 → 压缩（zip / tar.gz）→ 上传
```

- **spec 只有一份收集规则**：`docs/Releases/spec_common.py`。
  `omnibox.spec`（Windows）与 `omnibox-linux.spec`（Linux）都从它导入
  `collect_data_files()` 与 `HIDDEN_IMPORTS`，避免两边规则漂移。
- **缺少前端产物必须硬失败**：以前 spec 里是 `if frontend_dist.exists():`，
  干净 clone 上打包会产出一个打开白屏的 exe，而构建日志全绿。现在
  `collect_data_files()` 直接抛 `SystemExit`。
- **不打缓存与用户数据**：跳过 `__pycache__` / `*.pyc` / `.git` / `node_modules`，
  以及 `data/`、`.config/`（后者含 `auth_token.txt`）。
- **`res/` 必须由 spec 单独收集**：`res/`（仓库级共享资源，目前只有图标 sprite
  `res/icons/icons.svg`）**不经过 Vite**，所以既不会被 `shell/frontend/dist` 的收集覆盖，
  也不会被 `publicDir` 复制。漏掉它的表现是：构建全绿、应用能开，但所有图标都不显示
  （`<use>` 指向的资源 404）。`REQUIRED_PAYLOAD`（静态）与 `check_build_tree.py` 的
  `REQUIRED`（产物）各有一条断言兜住。
- **产物校验**：`tools/check_build_tree.py` 断言必需文件存在、禁止内容不出现
  （含字节码与用户数据目录），并且会**解析产物 exe 里的 PYZ**，确认
  `HIDDEN_IMPORTS` 声明的纯 Python 模块真的打进去了 —— 插件是动态加载的，
  PyInstaller 找不到时只打一条 warning 就跳过，必须靠这道检查兜住。
- **CI 里 Linux 构建必须用发行版 Python**（不能用 `actions/setup-python`）：
  后者那份 libpython 与我们的 spec 不兼容，产物启动即报
  `Failed to load Python shared library .../libpython3.12.so.1.0`（它会让
  PyInstaller 把 `_struct`/`_zlib` 嵌进 exe，bootloader 于是按 onefile 去
  `/tmp/_MEIxxx` 找 libpython；即便找到，初始化 `struct` 也会报
  `Module object for struct is NULL!`）。因此 `release.yml` / `ci.yml` /
  `package-smoke.yml` 里**产出二进制**的 job，Linux 分支统一走
  `apt install python3 python3-venv binutils` +
  `python3 -m venv "$RUNNER_TEMP/omnibox-build-venv"`，再把 venv 的 `bin`
  追加到 `GITHUB_PATH`（后续步骤里的 `python` 就是它）。venv 刻意放在工作区
  **外面**：放仓库里会被 ruff 当源码扫（`extend-exclude` 里没有它）。
  Windows 分支照旧用 setup-python；只跑测试/门禁的 job 也不受影响。

本地发布构建（沿用原脚本，已加严格模式）：

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File docs/Releases/build-release.ps1
# Linux
bash docs/Releases/build-release.sh
```

### 产物目录里绝不能有你自己的用户数据

程序的可写数据（`.config`、`data`、`logs`）就放在**可执行文件旁边**（见
`shell/backend/paths.py`：打包模式下 exe 目录可写就用它）。所以在产物目录里跑过
一次程序做测试之后，那个目录里就是**你本机的状态**：

- `.config/plugins/*.json`：各插件设置，含媒体目录路径（`D:\图库`、`D:\音频\音乐`…）
  和 `pixiv-sync` 的 `refresh_token`、代理地址
- `.config/auth_token.txt`：本机访问令牌
- `data/`：缩略图缓存、下载状态等（体积还不小）
- `plugins/**/__pycache__`：跑测试留下的字节码

以前 `build-release.*` 会把这些原样压进 7z/zip/tar.gz（用 `-SkipPyInstaller`
在测过之后重新压缩时最容易中招），用户装完打开设置页看到的就是开发机的路径。
现在两个脚本在压缩前都会把关：

1. 发现 `.config` / `data` / `logs` 残留就**直接中止**，并提示删掉或改用
   `-CleanUserData` / `--clean-user-data` 让脚本自动清理；
2. 跑一遍 `tools/check_build_tree.py`（同时拦 `__pycache__` / `.pyc` 与缺文件）。

CI 侧本来就有这道闸：`ci.yml` 的 `package`、`package-smoke.yml`、`release.yml`
都会跑 `check_build_tree.py`，这也是 CI 产物一直干净的原因。想验证一个现成压缩包：

```bash
unzip -l OmniBox-windows-x64.zip | grep -E '\.config|/data/|auth_token'   # 应为空
tar -tzf OmniBox-linux-x64.tar.gz | grep -E '^OmniBox/(\.config|data)'    # 应为空
```

### 在 Windows 上能构建出 Linux 产物吗？

**不能直接构建。** PyInstaller 官方写明它不是交叉编译器——它必须在构建时运行
*目标平台*的 Python 环境（Linux 产物就得在 Linux 上跑 PyInstaller）。
在 Windows 上执行 `build-release.sh` 只会得到一个 Windows 可执行文件。
参见 [PyInstaller: Building Cross Platform](https://pyinstaller.org/en/stable/building-for-other-platforms.html)（该页也直接把
"虚拟化 / CI"列为官方推荐做法）。

拿到 Linux 产物的三条路，按省事程度排序：

1. **人工触发本仓库 workflow（推荐）**：`release.yml` 勾 `dry_run`，或在
   `package-smoke` 上手动触发 → ubuntu runner 构建 → 下载 artifact。
   不占本地环境，且产物出自干净的 Linux 环境。
2. **WSL2**（Windows 自带）：`wsl --install -d Ubuntu`，在发行版里
   `bash docs/Releases/build-release.sh`（需要 `python3-venv`、`node`/`npm`、`binutils`）。
3. **Docker Desktop**：
   ```bash
   docker run --rm -v "$PWD":/src -w /src ubuntu:22.04 bash -lc \
     "apt-get update && apt-get install -y python3 python3-venv python3-pip nodejs npm binutils && bash docs/Releases/build-release.sh"
   ```

**glibc 基线**：PyInstaller **不**打包 glibc，产物只对新版 glibc 前向兼容。
因此要在"你想支持的最旧发行版"上构建（WSL/Docker 里优先选 `ubuntu:22.04`，
而不是最新版），否则老系统用户会遇到动态链接错误。CI 的 `ubuntu-latest` 同理。

---

## 5. 静态检查配置的两个要点

1. **`include`/`exclude` 必须显式声明**。默认会把 `docs/Releases/OmniBox/`
   （PyInstaller 产物，同一份代码的副本）与 `venv/` 一起分析，产生大量与源码
   无关的重复报错（实测：171 → 96 errors）。
2. **`venvPath`/`venv` 必须声明**。否则独立跑 pyright 会报
   `Import "flask"/"PIL"/"webview" could not be resolved`，把真实错误淹没。

ruff 规则集只开"能抓到真问题"的：`E4/E7/E9/F`（语法、未定义名字、未使用导入）
+ `I`（导入顺序）+ `B/SIM/RET/PIE/FURB/RUF` + `PLW1510`。刻意未开启：

| 规则 | 处数 | 为什么先不开 |
| --- | --- | --- |
| `BLE001` blind-except | ~179 | 需要逐处判断是"该收窄"还是"必须兜底"，属专项清理 |
| `S110` try-except-pass | ~48 | 同上；其中不少是"缓存未命中"的有意忽略 |
| `UP`（注解现代化） | ~562 | 纯风格，`from typing` → 内建泛型，一次专项提交更清楚 |
| `SIM105` | 29 | 语义等价的风格改写 |
| `SIM108` | 2 | 剩余两处都带"为什么这样写"的注释，压成三元表达式会丢注释 |

### 关于 `# noqa: BLE001` 标注的去留

原仓库在 pixiv-sync 等文件里写了 10 处 `except Exception as e:  # noqa: BLE001`，
用来标注"这里宽泛捕获是有意的"。由于本项目 `select` 里**没有** `BLE001`，
RUF100（未使用的 noqa）会把这些标注判为冗余并删除——它们表达的是意图，不是
冗余，但保留下来又会被 RUF100 继续报错。

本次的处理是**不保留**这些标注，并在上表把它记为待办。推荐的收敛方式（二选一）：

1. **开启 BLE001 + 逐处 noqa**：把 `BLE001` 加进 `select`，然后为确实有意的位置
   写 `# noqa: BLE001`，其余收窄异常类型。这样"宽泛捕获"从默认改成需要理由。
   代价是当前约 179 处都要过一遍。
2. **只对特定子系统开启**：在该子目录放一份 `ruff.toml`（`extend` 根配置 +
   `extend-select = ["BLE001"]`）。注意 `per-file-ignores` 只能"关闭"规则，
   不能"开启"，所以必须用嵌套配置而不是 pyproject 里的 per-file-ignores。

无论选哪种，都应先做一次独立的专项提交，便于 review 与回滚。

---

## 6. 收紧路线（不进入 CI）

- **`pyright plugins tests` 不再占用 CI**：本机 pyright 1.1.411 仍有约 794 条错误
  （plugins 552 / tests 242），主要是 mixin 属性声明、可选成员访问与测试假对象缺属性。
  这条基线曾经带 `continue-on-error`，永远不会让 CI 失败；现在只在本地按需运行，
  等错误清零后再考虑作为硬门禁。收敛顺序：先补 mixin 属性声明，再处理
  `reportOptionalMemberAccess` / `reportPossiblyUnboundVariable`，最后处理测试假对象。
- **`ruff format` 不进入门禁**：仓库从未按 ruff format 排版，直接开启会重排 173 个文件。
  如果要做，单独一个“纯格式化”提交，避免与逻辑改动混在同一个 diff 里。
- **`ruff` 的 `BLE001` / `S110` / `UP`** 仍按专项提交逐类开启，便于 review 与回滚。
- **Actions 版本**目前用 `@v4` / `@v5` 主版本标签；若要更强的供应链防护，可改为
  固定 commit SHA（Dependabot 可代为升级）。
- `.gitattributes` 已固定“入库 LF、工作区按平台”，`.ps1` 检出为 CRLF；
  历史提交若仍混有 CRLF，可跑一次 `git add --renormalize .` 收敛。

---

## 7. 本批次修掉的缺陷（与审查对应）

安全 / 数据安全：

| 缺陷 | 位置 | 后果 |
| --- | --- | --- |
| 读取失败的图片被算成"空内容摘要"，两个坏文件被判为"完全重复" | `plugins/image-cleaner/backend/main.py` | 点一次"删除重复"会永久删掉本不重复的真实照片（下一步就是 `unlink`）。现改为返回 `None` 并在分组时跳过 |
| `album_id` 未校验直接拼下载目录 | `plugins/manga-library/backend/{main,downloader}.py` | `os.path.join(root, "../../..")` 折叠越界 + `makedirs` 在漫画根目录外建目录写文件。现在要求纯数字，并在下载器再做一次 realpath 越界校验 |
| 设置文件名由 `plugin_name` 直接拼接 | `shell/backend/settings_store.py` | 可越出配置目录写 JSON（如 Linux `~/.config/autostart/`）。现在强制"裸文件名"，拒绝分隔符与 `..` |
| 缩略图属性未转义 | `plugins/manga-library/frontend/js/app.js` | 该文件唯一漏转义的插值，且落在 `src="${...}"` 属性里 → 注入 `onerror=`，插件与宿主同源且带令牌 Cookie |
| 假 mpv 脚本写在全局可写的 temp 根目录、固定文件名 | `plugins/netease-music/backend/netease_music_api.py` | 本地其它用户可预占/替换该脚本（它会被本进程执行）。现在落在私有子目录并 `chmod 0700` |
| `PATH` 用 `:` 拼接 | 同上 | Windows 上整个 PATH 被当成一个路径，假 mpv 永远找不到 → 取 URL 静默退化为 403 外链 |

数据丢失 / 正确性：

| 缺陷 | 位置 | 后果 |
| --- | --- | --- |
| 保存设置用整文件覆盖 | `shell/backend/plugin_base.py` | 插件写在同一个 JSON 里的运行期状态（`refresh_token`、`folders`、`media_set_config`）被用户在设置面板点一次保存就静默抹掉 |
| `finally: del cls._resolved_config` | `shell/backend/plugin_manager.py` | 类上没有该属性时抛 `AttributeError`，把插件真正的加载失败原因顶掉（`/status` 只显示"No attribute"）；也会删除子类同名类属性 |
| `_failed` 在锁外写、`status()` 在锁内读 | `shell/backend/tasks.py` | 能读到 `state=done` 但 `_failed` 仍为 `False`，把失败任务报成 `success: true` |
| `shutdown(wait=False)` 后立刻关连接 | `shell/backend/thumb_cache.py` | 函数在连接关闭后即返回，池线程仍在解码；与紧随其后的 `clear()`（checkpoint + VACUUM）抢同一份 DB |
| `atexit` 在 `load_all()` 之后注册 | `main.py` | 插件在 `on_load` 里 `sys.exit()` / Ctrl+C 打断启动时，已加载插件永远收不到 `on_unload`（SQLite/WAL 与后台线程泄漏） |
| `get_api_methods()` 返回活字典 | `shell/backend/plugin_manager.py` | 卸载/加载与请求线程并发时 `dictionary changed size during iteration`（关闭瞬间 500） |
| `/thumbs` 解包插件返回值不校验形状 | `shell/backend/file_server.py` | 返回非二元组时 `ValueError` → 500 |

卫生与规范：

- 6 个内核文件把"许可证块 + 模块 docstring"写成两段顶层字符串字面量 →
  `__doc__` 是许可证文本、真正的说明被丢弃，并让其后所有 import 被判为
  "在代码之后"（E402 共 62 处误报）。已合并为一段。
- `main.py` 的裸 `except:` 会吞掉 `KeyboardInterrupt`；已收窄为
  `requests.RequestException`。
- `image-cleaner` 同族缺陷：`_file_quick_hash` 同样返回"空摘要"；一并改为 `None`。

新增回归测试（`tests/`）：

- `test_image_cleaner_hash.py`（新）：坏文件必须返回 `None`、不可信摘要不得成组、
  读取期间文件变化要判定不可信。
- `test_settings_store.py`：越界插件名一律拒绝（含 Windows 分隔符、绝对路径、
  非字符串）；`update()` 返回合并后状态。
- `test_plugin_lifecycle.py`：保存设置不得抹掉运行期状态。

---

## 8. 尚未处理（已在审查中确认，按优先级排列）

这些是**已确认但未在本次修改**的项，留作后续提交：

1. `plugins/image-cleaner` / `image-viewer` 等插件的状态文件仍是 `open(w)` 直写
   （`download_state.json`、`document_progress`、`media_set_config` 等），崩溃即丢文件。
   仓库里已有原子写实现（`tasks.py`），应抽成公共工具后统一替换。
2. `pixiv-sync` 的 `collect_bookmarks_pending` / `fetch_artist` 两处分页循环没有
   页数上限（`_fetch_follow_stream` 有 `MAX_FOLLOW_PAGES=40`）；且每页都重写整张
   `works` 表（O(页数 × 条目数)）。
3. `pixiv_purge_non_original.py` 把"长边 == 1200px"当作缩放副本的证据，会删掉
   恰好 1200px 的原图，并造成"删除→重新下载"循环；建议增加 URL 含 `master1200`
   的条件。
4. `document-reader` 每章都整文件重读并重解码（内容缓存只有 3 条且非 LRU）；
   可用已持久化的 offset 做 `seek/read` 切片。
5. `media-player` 的 `eq-presets` 写在插件目录内，frozen 后位于 `_MEIPASS`
   （只读或被更新覆盖），应迁到 `get_data_root()`。
6. `image-viewer` 的 `ensure_thumbnail` 回退逻辑会把原图复制成"缩略图"；
   该路径当前是死代码。
7. 插件设置文件未 `chmod 0600`（Linux 上权限取决于 umask），而令牌文件是显式
   `0600`；密钥类设置应统一收紧。
8. `docs/plugin-guide.md` 建议 `Bridge.originalUrl(encodeURIComponent(path))`，
   与 `base.js` 内部已编码的实现冲突（双重编码）。
9. **前端工具链落后一个大版本以上，且 Vite 5 已停止接收安全补丁。** 核实结论：
   - Vite 的支持政策是"当前 minor 常规修复 / 前两个 major 只发安全补丁"（映射
     `{5:'5.4', 6:'6.4', 7:'7.3'}`）。当前 8.3 → **5.4 什么都不发**，而
     `tools/check_npm_audit.py` 里 9 条 vite / esbuild 公告的受影响范围都落在当前大版本，
     唯一的修复路径就是跨大版本（这正是它们被登记为例外的原因）。
   - 最小耦合集：`vite ^8.3.0` + `@vitejs/plugin-vue ^6.0.9` + `vue-tsc ^3.3.11`，
     **`typescript` 留在 5.9.3**（vue-tsc 3.x 的 peer 只有 `>=5.0.0`，且不依赖 Vite）。
     `vue` / `vue-router` / `pinia` 可不动。
   - **不要把 TypeScript 升到 7.0**：7.0 是 Go 原生端口，不提供编程 API，微软明确说
     Volar 这类"把 TS 嵌进自己编译器"的工具目前只能用 6.0；本仓 `package.json` 的
     `build` 是 `vue-tsc --noEmit && vite build`，升了会直接断在门禁上。
   - `vue-tsc 3.x` 与 `plugin-vue 6.x` 的硬要求是 Node 20.19+/22.12+（本机 24.16 满足）。
   - 升级前必须实测：Vite 8 的默认 `build.target` 抬到 Chrome 111+ / Safari 16.4，
     需与桌面 WebView 的内核版本核对；另需复核 Oxc / Lightning CSS 压缩后的产物差异。
   - 低风险依据：`shell/frontend/vite.config.ts` 只有 17 行，无 `rollupOptions` /
     `manualChunks` / `publicDir` 覆盖 / CSS 预处理器 / postcss 配置，纯 ESM 且无 CJS 依赖，
     所以 Vite 8 的 Rolldown 相关破坏性改动基本碰不到。
   - 顺带纠正一处传闻：**Vite 5/6/7/8 的 `publicDir` 都是 `string | false`，没有数组形式**，
     多静态目录只能合并进一个 `public/` 或自己加拷贝步骤。

---

## 9. 发布清单（半自动流程）

1. 把 `pyproject.toml` 与 `shell/frontend/package.json` 的版本号改成同一个值，
   提交并推到 `main`（这一步只是普通 push，走 `ci.yml` 日常门禁）。
2. **打 tag 并推送**：
   ```bash
   git tag v1.2.1 && git push origin v1.2.1
   ```
   触发 `release.yml`：版本一致性 + 全量门禁 + 双平台构建 + 产物校验 + 建**草稿** Release。
   校验版本没对上、或 tag 不在 `main` 上，这一步就会失败，什么都不会产出。
3. 等运行结束，去 **Releases** 页面找到那份 **Draft**：
   - 想先在自己机器上验证，就从该次运行页面下载 artifact（Windows `zip` / Linux `tar.gz`，
     7 天内有效）。Windows 本机造不出 Linux 产物，见 §4。
   - 只想构建不想建草稿的场景，用手动触发 + 勾 `dry_run`。
4. 核对产物与说明无误后点 **Publish release** —— 这一刻才对外可见。
5. 发布后如需修正：重新构建用 `release.yml` 重跑（草稿状态会覆盖刷新；
   已发布状态会拒绝覆盖），要换产物就先 `gh release delete <tag>`。

回滚：`gh release delete <tag>`（可加 `--cleanup-tag` 一并删 tag）。
