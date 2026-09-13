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
| 类型检查（内核） | `python -m pyright main.py shell tools` | **0 错误**（硬门禁） |
| 类型检查（插件+测试） | `python -m pyright plugins tests` | 基线（暂不拦截，见 §6） |
| 单元测试 | `python -m unittest discover -s tests` | **129 passed**（硬门禁；Linux 上 4 项 skip，见 §2） |
| 运行时禁止 print | `python -m unittest tests.test_no_print_in_runtime` | 通过（硬门禁） |
| 前端转义一致性 | `node tools/check_frontend_escape.cjs` | **OK**（硬门禁） |
| 前端类型+构建 | `npm --prefix shell/frontend run build` | 通过（硬门禁） |
| 打包冒烟 + 产物校验 | `python tools/check_build_tree.py <dist>/OmniBox --expect-exe OmniBox.exe` | 通过（硬门禁；CI 里只在 push main / 手动触发 / 打包路径变更时跑） |

本地一次性跑全部（PowerShell）：

```powershell
$py = ".\venv\Scripts\python.exe"
& $py -m ruff check .
& $py tools/check_plugins.py
& $py tools/check_packaging.py
& $py tools/check_version.py
& $py -m pyright main.py shell tools
& $py -m unittest discover -s tests
node tools/check_frontend_escape.cjs
```

---

## 2. 工作流

### `ci.yml` —— 每次 push / PR

| job | runner | 内容 |
| --- | --- | --- |
| `lint` | ubuntu | ruff + 插件规范 + 打包规则 + 版本一致性 |
| `typecheck` | windows | pyright 内核（硬门禁）+ 插件/测试（基线，不拦截） |
| `test` | windows + ubuntu，py3.10 + 3.12 | unittest 全量（netease-music 的 4 项仅 Windows 运行） |
| `frontend` | ubuntu | 转义门禁 + `npm ci` + `vue-tsc --noEmit` + `vite build` |
| `package` | windows + ubuntu | 真实跑 PyInstaller + 校验产物内容；**只在 push `main` / 手动触发时跑**（日常 push 由 `package-smoke` 按路径兜底，产物仅作 artifact） |

**日常 push 实际只跑 7 个 job**：`lint` + `typecheck` + `test`×4 + `frontend`。
`package`×2 被 `if:` 关在 push `main` / 手动触发上（被跳过的 job 仍会列在运行页面
上显示 `skipped`，但不消耗 runner）；打包路径相关的回归由 `package-smoke.yml`
按路径触发兜底，两者跑的是同一套 spec + `check_build_tree.py` 产物校验。

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
- **自动做完**：版本一致性校验（tag ↔ pyproject ↔ package.json）、ruff / 插件规范 /
  打包规则 / 全量单测 / 前端转义门禁、双平台 PyInstaller 构建、`check_build_tree.py`
  产物校验、打 zip / tar.gz、生成 `.sha256`、建**草稿** Release 并挂上产物。
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
- `shell/frontend/package.json` 的 `version` 必须与之相同（`check_version.py` 校验）。
- tag 必须写成 `v<version>`；`release.yml` 会把 tag 与**它指向提交**里的
  pyproject 版本对齐检查，不一致时第一步就失败，**不会**产出名字与版本不符的安装包。
- 打 tag 会**触发构建**（全量门禁 + 双平台产物 + 建草稿 Release），但**不会公开**：
  公开是另一下人工动作（点 Publish）。所以 tag 是"决定发版"，Publish 是"决定对外可见"。
- 取值给脚本用：`python tools/check_version.py --print`（只输出裸版本号）。

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

### ⚠️ 产物目录里绝不能有你自己的用户数据

程序的可写数据（`.config`、`data`、`logs`）就放在**可执行文件旁边**（见
`shell/backend/paths.py`：打包模式下 exe 目录可写就用它）。所以在产物目录里跑过
一次程序做测试之后，那个目录里就是**你本机的状态**：

- `.config/plugins/*.json`：各插件设置，含媒体目录路径（`G:\图库`、`G:\音频\音乐`…）
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

⚠️ **glibc 基线**：PyInstaller **不**打包 glibc，产物只对新版 glibc 前向兼容。
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
＋ `I`（导入顺序）＋ `B/SIM/RET/PIE/FURB/RUF` ＋ `PLW1510`。刻意未开启：

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

## 6. 收紧路线（下一步）

当前唯一"报告但不拦截"的是**插件与测试的 pyright 基线**。收敛顺序建议：

1. `tests/`（约 60 条）：主要是 `importlib.util.spec_from_file_location` 返回
   `ModuleSpec | None`、以及手工构造的假对象缺属性。修法统一为给返回值加断言。
2. `plugins/media-player/backend/metadata.py`（约 14 条）：`try: import mutagen`
   之后的名字在 `except ImportError` 分支未定义 → pyright 报 "possibly unbound"。
   修法是显式初始化为 `None` 并加类型注解。
3. 其余插件按文件逐个清零。
4. 全清零后，把 `typecheck` job 的第二步改成不 `continue-on-error`，并让
   `pyright main.py shell plugins tools tests` 成为一条命令。

其它待办：

- `ruff` 开启 `BLE001`/`S110`/`UP` 前，先做一次专项提交（每类一个 commit，
  便于 review 与回滚）。
- `ruff format` 目前有 67 个文件未格式化；建议单独一个"纯格式化"提交，
  避免与逻辑改动混在一个 diff 里。
- Actions 目前用 `@v4` / `@v5` 主版本标签。若要更强的供应链防护，可改为
  固定 commit SHA（Dependabot 可代为升级）。
- `.gitattributes` 已固定"入库 LF、工作区按平台"，`.ps1` 检出为 CRLF。
  如果历史提交里仍混有 CRLF，可跑一次 `git add --renormalize .` 收敛。

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
   （`download_state.json`、`novel_progress`、`media_set_config` 等），崩溃即丢文件。
   仓库里已有原子写实现（`tasks.py`），应抽成公共工具后统一替换。
2. `pixiv-sync` 的 `collect_bookmarks_pending` / `fetch_artist` 两处分页循环没有
   页数上限（`_fetch_follow_stream` 有 `MAX_FOLLOW_PAGES=40`）；且每页都重写整张
   `works` 表（O(页数 × 条目数)）。
3. `pixiv_purge_non_original.py` 把"长边 == 1200px"当作缩放副本的证据，会删掉
   恰好 1200px 的原图，并造成"删除→重新下载"循环；建议增加 URL 含 `master1200`
   的条件。
4. `novel-reader` 每章都整文件重读并重解码（内容缓存只有 3 条且非 LRU）；
   可用已持久化的 offset 做 `seek/read` 切片。
5. `media-player` 的 `eq-presets` 写在插件目录内，frozen 后位于 `_MEIPASS`
   （只读或被更新覆盖），应迁到 `get_data_root()`。
6. `image-viewer` 的 `ensure_thumbnail` 回退逻辑会把原图复制成"缩略图"；
   该路径当前是死代码。
7. 插件设置文件未 `chmod 0600`（Linux 上权限取决于 umask），而令牌文件是显式
   `0600`；密钥类设置应统一收紧。
8. `docs/plugin-guide.md:751` 建议 `Bridge.originalUrl(encodeURIComponent(path))`，
   与 `base.js` 内部已编码的实现冲突（双重编码）。

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
