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
| 打包冒烟 + 产物校验 | `python tools/check_build_tree.py <dist>/OmniBox --expect-exe OmniBox.exe` | 通过（硬门禁） |

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
| `package` | windows + ubuntu | 真实跑 PyInstaller + 校验产物内容（产物仅作 artifact） |

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

### `release.yml` —— 打 tag 即发布

推 `v*` tag → 校验版本 → 双平台构建 → 创建 GitHub Release 并上传产物与 `.sha256`。
也可在 Actions 页面手动 `workflow_dispatch` 指定一个已有 tag。

> 首次使用需确认仓库 `Settings → Actions → General → Workflow permissions`
> 为 **Read and write permissions**（创建 Release 需要）。

---

## 3. 版本号唯一来源

- 唯一来源：`pyproject.toml` 的 `[project].version`。
- `shell/frontend/package.json` 的 `version` 必须与之相同（`check_version.py` 校验）。
- tag 必须写成 `v<version>`；不一致时 `release.yml` 第一步就失败，**不会**产出
  名字与版本不符的安装包。
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
  （含字节码与用户数据目录）。

本地发布构建（沿用原脚本，已加严格模式）：

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File docs/Releases/build-release.ps1
# Linux
bash docs/Releases/build-release.sh
```

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
