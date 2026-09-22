# 漫画库插件（manga-library）设计文档

> 版本：v2.1.0（`plugins/manga-library/manifest.json` 当前实现）
> 形态：独立宿主插件；Shell 主导航入口 `/manga-library`，`keepAlive: true`（常驻 iframe）
> 适用范围：后端 `plugins/manga-library/backend/`、前端 `plugins/manga-library/frontend/`

## 1. 定位

`manga-library`（显示名「漫画中心」）是 OmniBox 的本地漫画库与下载中心**合并插件**：把磁盘上的漫画目录组织成书架，提供章节/图片浏览、沉浸式单页阅读、收藏与最近阅读，并内置基于 `jmcomic` 的下载任务管理。`manifest.json` 的 `description` 即此意：「漫画库与下载中心合并插件：书架浏览、章节阅读、收藏、最近阅读与 jmcomic 下载任务管理。」

- **漫画库**：数据根目录 = 设置项 `root_dir`；未配置时回落 `PluginBase.get_data_root()`（全局配置 `directories.data_root`）。根目录下**每个一级子目录即一部漫画**，跳过隐藏目录与名为 `ai` 的目录；标题/作者/标签/总页数/专辑号取自该目录的 `album_info.json`，缺失时用目录名与「未知」兜底。
- **浏览层级**：一级视图 `all` / `favorites` / `recent` / `downloads`，详情二级页 `chapters` → `images`；前端用 `viewLevel`（`home` / `chapters` / `images`）驱动，入口只有一个 `loadView()`。
- **阅读**：图片页点击任意缩略图打开全屏阅读器 `MangaReader`（`frontend/js/reader.js`），支持点击翻页、左右箭头与键盘（`←/→/↑/↓/a/w/d/s`、空格、Esc）；进入图片页即调用 `manga_update_recent` 记录最近阅读。
- **下载中心**：与漫画库合并在同一插件、同一数据根目录——`download_*` 系列 API 管理 jmcomic 下载任务（提交/暂停/继续/重试/删除/全部开始/全部暂停/清除已完成/详情），任务状态因此可无缝继承旧 `download-center` 的落盘文件。
- **文件服务**：插件覆写 `get_data_root()` 返回漫画根目录，Shell `/file` 路由以它作为允许根；前端统一用 `Bridge.originalUrl(...)` 把后端返回的相对路径拼成文件 URL。
- **权限与依赖**：`filesystem:read`、`network:download`；`dependencies: []` —— 既不依赖其他插件，也不通过 `get_extensions()` 提供 Companion 入口（`renderExtensions` / `callPlugin` 在该插件 0 处调用）。
- **写盘边界**：漫画库部分只读扫描；唯一写盘路径在下载中心（下载图片、`album_info.json`、任务状态与进度），提交与执行两处都有越界防护（见 §3.2）。

### 1.1 设置项（settings_schema）

| key | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `root_dir` | directory | 空（回落 `get_data_root()`） | 漫画根目录；`admin_only: true`（它决定 `/file` 的允许根），保存后经 `on_settings_changed` 重建目录、缓存与任务表 |
| `recent_count` | number | 10（1–50） | 「最近阅读」展示数量，`manga_get_state` 用它截断 |

设置由 Shell 的 `SettingsStore` 持久化（`.config/plugins/manga-library.json`）。插件覆写了 `get_settings()`，直接返回 `{root_dir, recent_count}`；本插件没有 `secret` 类键，不涉及基类的掩码逻辑。

## 2. 目录结构

### 2.1 插件目录

```
plugins/manga-library/
├── manifest.json                  # name/displayName/icon/keepAlive/permissions，backend.entry=backend/main.py，route=/manga-library
├── backend/
│   ├── main.py                    # MangaLibraryPlugin（PluginBase 子类）：settings_schema、API 注册、扫描与状态、下载任务管理
│   ├── scanner.py                 # 无实例状态纯函数：scan_manga / find_cover / list_pages / resolve_safe_path / natural_sorted
│   ├── download_models.py         # DownloadTask 数据类 + to_api_dict / to_state_dict / from_state_dict
│   ├── download_state.py          # 任务状态 JSON 读写：save_tasks / load_tasks
│   └── downloader.py              # execute_download：jmcomic 下载执行、进度回调、album_info 组装
└── frontend/
    ├── index.html                 # 唯一入口：侧栏、工具栏、内容区、沉浸阅读器、2 个弹窗
    ├── manga-library.css          # 全部样式
    └── js/
        ├── utils.js               # MangaUtils（转义 + 封面 HTML 降级）、DownloadUtils（状态文案/速度/剩余时间）
        ├── reader.js              # MangaReader：沉浸式单页阅读器（翻页、键盘、工具栏）
        └── app.js                 # MangaLibraryApp：视图切换、书架/章节/图片渲染、下载中心、弹窗、轮询与生命周期
```

后端 4 个分片模块不在 `main.py` 里直接 import，而是用 `shell.backend.plugin_utils.load_sibling(__file__, '<模块名>', '<命名空间>')` 显式加载（插件入口由 PluginManager 用 importlib 直接加载，普通相对导入不可靠）；对应的模块名是 `manga_library_scanner`、`manga_library_download_download_models`、`manga_library_download_download_state`、`manga_library_download_downloader`，加载后 `main.py` 把 `DownloadTask` / `load_tasks` / `save_tasks` / `execute_download` 与 scanner 的纯函数重新绑定成模块级名字。`backend/__pycache__/` 是运行时产物，不进提交。

### 2.2 运行时数据（相对于漫画根目录）

```
<漫画根目录>/                      # = root_dir / get_data_root()，也是 /file 的允许根
├── <专辑号>/                      # 一部漫画一个一级子目录（下载目录即 <漫画根>/<纯数字专辑号>）
│   ├── album_info.json            # 下载时写入：oname/album_id/actors/title/author/tags/chapter_count/total_page_count/chapters/download_time
│   ├── 00001.jpg …                # 单章漫画：图片直接平铺（jmcomic dir_rule = Bd / Aid）
│   └── <章节标题>/                # 多章漫画：按章节分子目录（dir_rule 换成 Bd / Aid / Pindextitle）
├── .cache/
│   ├── manga_state.json           # 收藏与最近阅读：{favorites: [folder_name…], recent: [{id, page, time}…]}
│   └── covers/                    # 封面缓存目录（__init__ 中创建，当前实现未向其中写文件）
└── .jmcomic_state/                # 下载中心状态目录（与旧 download-center 完全一致，任务可继承）
    ├── download_state.json        # 任务表（DownloadTask.to_state_dict 的字典）
    └── progress_<task_id>.json    # 进度快照，每下载 5 张图片刷新一次
```

`.jmcomic_state` 建不出来时（`PermissionError` / `OSError`）回落到 `~/.jmcomic_state`。`.cache/manga_state.json` 缺失或损坏时按 `{favorites: [], recent: []}` 处理；`download_state.json` 缺失或损坏时任务表为空，且恢复时把 `downloading` 任务统一降级为 `paused`。

## 3. 后端 API 契约

### 3.1 注册表（`register_api()`）

| API | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `manga_list` | 无 | `List[dict]` | 强制重新扫描漫画根目录，返回全部漫画对象 |
| `manga_search` | `keyword` | `List[dict]` | 在 `title` / `author` 上做小写子串匹配；空关键字等价 `manga_list` |
| `manga_get_state` | 无 | `{recent, favorites}` | 按 state 里的 id 取回完整漫画对象；`recent` 截断到 `recent_count`，两者都按 `folder_name` 索引 |
| `manga_toggle_favorite` | `folder_name` | `bool` | 切换收藏，返回切换后的状态；同时清空扫描缓存 |
| `manga_update_recent` | `folder_name`, `page=0` | `{status: 'ok'}` | 去重后插到队首，最多保留 20 条 |
| `manga_get_detail` | `folder_name` | `dict`（路径非法或不存在时 `{}`） | `folder_name` / `title` / `author` / `is_fav` / `is_multi_chapter` / `info` / `chapters` |
| `manga_get_pages` | `folder_name`, `chapter_path=''` | `List[str]` | 相对漫画根的 posix 路径；越界或目录不存在返回 `[]` |
| `download_submit` | `album_id`, `concurrency=3`, `priority='normal'`, `auto_start=True` | `{taskId, success: True}` / `{success: False, error}` | `album_id` 必须纯数字；`concurrency` 夹到 1–10 |
| `download_list` | 无 | `{tasks: [dict…]}` | 任务摘要列表，按优先级（high/normal/low）再按 `startTime` 排序 |
| `download_pause` | `task_id` | `{success[, error]}` | 仅 `downloading` 可暂停（置 `_stop_event` 并落盘） |
| `download_resume` | `task_id` | `{success[, error]}` | 仅 `paused` 可恢复：清 `_stop_event`、置 `queued` 并起线程 |
| `download_retry` | `task_id` | `{success[, error]}` | 仅 `failed` 可重试：清错误、`completed_images` 归零、重新排队 |
| `download_delete` | `task_id` | `{success[, error]}` | 下载中的任务先置 `_stop_event`，再删除记录（不删已下载文件） |
| `download_start_all` | 无 | `{success: True}` | 把所有 `paused` 任务恢复为 `queued` 并起线程 |
| `download_pause_all` | 无 | `{success: True}` | 把所有 `downloading` 任务置为 `paused` |
| `download_clear_completed` | 无 | `{success: True, deleted}` | 删除全部 `completed` 任务记录 |
| `download_get_album_info` | `album_id` | `album_info.json` 内容 + `exists: True` / `{error, exists: False}` | 越界返回「非法路径」，缺文件返回「未找到漫画信息文件」 |
| `get_settings` | 无 | `{root_dir, recent_count}` | 插件覆写基类实现，直接返回当前生效值 |
| `save_settings` | `settings` | `{success[, error]}` | 基类实现（校验 + 写 SettingsStore + 触发 `on_settings_changed`） |

### 3.2 对象与语义

- **漫画对象**（`scanner.scan_manga`）：`comic_id`（`album_info.album_id`，缺失时用目录名）、`title`、`author`（默认「未知」）、`tags`、`page_count`（`total_page_count`，默认 `0`）、`cover_url`（相对漫画根的 posix 路径，可能为 `''`）、`folder_name`、`is_fav`。
- **封面选择**（`scanner.find_cover`）：多章漫画先进入自然序第一个子章节目录；在每个候选目录内按「文件名含 `cover` 或 `封面`」→「纯数字命名的第一页」→「自然序第一张」依次取第一张图片，返回相对漫画根的 posix 路径。
- **章节判定**（`manga_get_detail`）：`is_multi_chapter = 存在非隐藏且名字不为 ai 的一级子目录`；多章时 `chapters` 为自然序子目录列表，每项 `{name, cover_url, path}`（`path` 即子目录名，供 `manga_get_pages` 的 `chapter_path` 使用）。
- **详情 `info`**：直接读 `<漫画目录>/album_info.json` 原文（读取失败或缺失时为 `{}`），前端据此渲染 ID/原名/作者/下载时间/演员/标签。
- **页面列表**（`scanner.list_pages`）：只收 `*.jpg` / `*.jpeg` / `*.png` / `*.webp`，自然序（`natsort`，缺库时用等宽数字填充的退化实现），返回相对漫画根的 posix 路径。
- **路径安全**（`scanner.resolve_safe_path`）：`Path.resolve()` 后要求目标落在漫画根目录内，指定 `chapter_path` 时还要求落在该漫画目录内，否则返回 `None`；`manga_get_detail` / `manga_get_pages` 因此返回空结果而不是抛错。
- **任务对象**（`DownloadTask.to_api_dict`）：`id` / `albumId` / `title` / `thumbUrl` / `status` / `totalImages` / `completedImages` / `speed`（bytes/s）/ `eta`（秒）/ `priority` / `downloadDir` / `error` / `startTime` / `completeTime`；`detail=True` 时额外带 `chapters` 与 `concurrency`。
- **提交越界防护**：`download_submit` 先 `strip()` 再要求 `album_id.isdigit()`（注释明确：否则 `os.path.join` 会折叠出漫画根之外，`os.makedirs(exist_ok=True)` 就在外部建目录写图片）；`downloader.execute_download` 另有一层 `realpath` 前缀校验，越界直接抛错。单章 `dir_rule` 为 `Bd / Aid`，`len(album) > 1` 时在 `before_album` 换成 `Bd / Aid / Pindextitle`。
- **总页数口径**：jmcomic api 客户端的漫画 `page_count` 恒为 0，因此下载器在 `before_photo` 里按章节累加真实页数（`total_images = max(漫画声明页数, 章节累加值)`）；落盘 `album_info.json` 时 `total_page_count = task.total_images or task.completed_images`，避免续传把总数写小。
- **`get_settings` 覆写**：返回当前生效的 `{root_dir, recent_count}`；`on_settings_changed` 在 `root_dir` 变化且新目录存在时重建 `manga_dir` / `cover_dir` / `state_file` / 扫描缓存 / 下载任务表，`recent_count` 会被夹到 1–50。

### 3.3 下载任务状态机

```
download_submit(auto_start=True) --> queued
queued --(起线程)--> downloading
downloading --(成功)--> completed --(download_clear_completed)--> 删除
downloading --(download_pause / download_pause_all)--> paused
paused --(download_resume / download_start_all)--> queued
downloading --(线程异常)--> failed
failed --(download_retry：清错、completed_images=0、重新排队)--> queued
```

- 运行态（`_thread`、`_stop_event`）不落盘；`download_state.json` 只存 `to_state_dict()` 的字段。
- `download_submit(auto_start=False)` 直接落 `paused`；程序退出前仍在 `downloading` 的任务，重启恢复时统一按 `paused` 处理。
- 「取消」语义只有两条：删除任务（`download_delete`）与全局清除已完成（`download_clear_completed`），没有单独的取消按钮。

## 4. 前端

`/manga-library` 路由加载 `frontend/index.html`，**无构建步骤、无框架**，全部为原生 DOM 与模板字符串 `innerHTML`。

| 文件 | 当前行数 | 职责 |
| --- | --- | --- |
| `frontend/index.html` | 130 | 唯一入口：侧栏、工具栏、内容区、沉浸阅读器、2 个弹窗 |
| `frontend/manga-library.css` | 292 | 全部样式 |
| `frontend/js/app.js` | 536 | `MangaLibraryApp`：视图切换、书架/章节/图片渲染、下载中心、弹窗、轮询与生命周期 |
| `frontend/js/reader.js` | 76 | `MangaReader`：沉浸式单页阅读器（翻页、键盘、工具栏） |
| `frontend/js/utils.js` | 62 | `MangaUtils`（转义 + 封面 HTML 降级）、`DownloadUtils`（状态文案/速度/剩余时间） |

- **多视图单页**：侧栏 3 个漫画库视图（`data-view="all|favorites|recent"`）与 6 个下载筛选项（`data-view="downloads"` + `data-dl-filter="all|downloading|queued|paused|completed|failed"`）；内容区分发在 `loadView()`，下载视图内部转到 `renderDownloads()`。
- **数据通路**：读写一律走 `Bridge.call('<api>', ...)`，封面/图片走 `Bridge.originalUrl(...)` 拼 Shell `/file` 路由；不使用 `renderExtensions` / `callPlugin`（无 Companion 内嵌）。
- **复用的壳能力**：布局类 `.view-sub-sidebar` / `.view-body` / `.view-toolbar` / `.toolbar-group` / `.view-content` / `.obx-nav-item` / `.obx-scroll`，组件 `.btn` / `.modal*` / `.empty-state` / `.search-field` / `.obx-anim-scale`，以及 `Toast` / `confirmDialog` / `openSettingsModal` / `Icons.html` / `Motion.retrigger` / `Utils.debounce`。
- **自持组件**：`.ml-*` 前缀的书架卡片与封面、详情 hero 与信息盒、下载任务行、进度条、弹窗表单；阅读器一整套（`.manga-reader` / `.reader-*` / `.btn-icon`）不带前缀，见 §5.4.1 与 §5.7.6。
- **生命周期**：`keepAlive: true` 的常驻 iframe 用 `window.PluginLifecycle` 的 `onHide/onShow/onDispose` 托管 2s 任务轮询（`_bindPluginLifecycle()`），后台停轮询、回前台重启，并带 `if (!window.PluginLifecycle) return;` 兜底。

**本节与 §5 的口径差异**（§5 完整保留审计当时的行号与计数，以下为本次核实到的当前实现差异）：

- `frontend/js/app.js` 现为 536 行（审计记 535），`frontend/manga-library.css` 现为 292 行（审计记 314）。
- 空态已改用壳的 `.empty-state`：`_emptyHtml()` 现在输出 `empty-state` / `empty-state-icon` / `empty-state-text` / `empty-state-hint`（`frontend/js/app.js:186-193`），插件自有的 `.ml-empty*` 样式已删除（`frontend/manga-library.css:250` 有对应注释）。审计 §5.1.3 把 `.empty-state` 列为「未使用」，§5.4.2 / §5.4.3 记的 `.ml-empty` 副本已不存在。
- 搜索框已改用壳的 `.search-field`（`frontend/index.html:47-51`、`frontend/manga-library.css:96-97` 注释说明「原 `.ml-search` 的 250/300/160px 三档宽度已删」），审计 §5.1.3 / §5.4.3 第 7 条记的 `.ml-search` 重写已不存在；弹窗内 3 处仍用 `.search-input` + 内联宽度（`frontend/index.html:83/88/92`），与 §5.7.4 一致。
- `.ml-no-anim` 抑制动画仍然保留，当前选择器是 `.ml-content.ml-no-anim .ml-task` 与 `.ml-content.ml-no-anim .empty-state`（`frontend/manga-library.css:216-217`）。

## 5. UI 现状取证（前端与壳契约对照）

> 本节为 manga-library 前端 UI 现状取证的原文迁移：以壳注入的样式与脚本为对照契约，逐文件、逐行号记录插件前端与壳的偏差，供后续统一 UI 时逐条处理。以下小节保留审计原文的表格、清单、`文件路径:行号` 引用、计数与措辞（只调整章节编号与文档内交叉引用）。

> - 取证范围：`plugins/manga-library/frontend/`（只读审计，未改动任何插件文件）。
> - 对照契约：`shell/frontend/public/shell/variables.css`、`base.css`、`effects.css`、`base.js`、`docs/plugin-guide.md` §4。
> - 计数口径：均在该插件目录用 `Select-String -AllMatches` 执行，正则见各节。

### 5.1 概览

#### 5.1.1 前端文件清单

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `frontend/index.html` | 130 | 唯一入口：侧栏、工具栏、内容区、沉浸阅读器、2 个弹窗 |
| `frontend/manga-library.css` | 314（12.8 KB） | 全部样式 |
| `frontend/js/app.js` | 535 | `MangaLibraryApp`：视图切换、书架/章节/图片渲染、下载中心、弹窗、轮询与生命周期 |
| `frontend/js/reader.js` | 76 | `MangaReader`：沉浸式单页阅读器（翻页、键盘、工具栏） |
| `frontend/js/utils.js` | 62 | `MangaUtils`（转义 + 封面 HTML 降级）、`DownloadUtils`（状态文案/速度/剩余时间） |

无构建步骤、无框架，全部为原生 DOM + 模板字符串 `innerHTML`。

#### 5.1.2 页面形态

单个 iframe 内的**多视图单页**（书架 / 收藏 / 最近 / 下载中心 + 详情二级页）：

- 三档视图层级由 `viewLevel` 驱动：`'home' → 'chapters' → 'images'`（`app.js:16/132/157-164`），
  加载入口只有一个 `loadView()`（`app.js:125-184`），下载视图内部分流到 `renderDownloads()`（`app.js:143`）。
- 下载中心用 6 个 `data-view="downloads"` 的侧栏项做**状态筛选**（`index.html:28-33`
  的 `data-dl-filter`，`app.js:114` 决定高亮、`app.js:422-424` 决定过滤）。
- 覆盖层：沉浸阅读器 `.manga-reader`（`index.html:64-74`，`position:fixed; inset:0`）
  与 Shell 风格弹窗 `#ml-add-modal` / `#ml-detail-modal`（`index.html:77/111`）。
- **无 companion 内嵌**：`iframe` 在该插件只出现在注释里（`app.js:32`），
  无 `renderExtensions` / `callPlugin` 调用。

#### 5.1.3 Shell 布局类使用情况

| Shell 类 | 使用位置 | 次数 |
| --- | --- | --- |
| `.view-sub-sidebar` | `index.html:12`（+ `.ml-sidebar`，并叠了 `.obx-glass`） | 1 |
| `.view-body` | `index.html:40` | 1 |
| `.view-toolbar` | `index.html:41`（+ `.ml-toolbar`） | 1 |
| `.toolbar-group` | `index.html:42,46` | 2 |
| `.view-content` | `index.html:60`（+ `.ml-content`、`.obx-scroll`） | 1 |
| `.obx-nav-item` | `index.html:23-33` | 9 |
| `.obx-scroll` | `index.html:21`（`.ml-nav`）、`index.html:60`（内容区） | 2 |
| `.btn` | `index.html:52-56,104,105,116` | 8（含 2 处 `.btn-primary`） |
| `.modal` / `.modal-box` / `.modal-body` / `.modal-footer` | `index.html:77-107,111-118` | 2 个弹窗 |
| `.search-input` | `index.html:83,88,92` | 3（均被内联 `width` 覆盖） |
| `.obx-glass` | `index.html:12` | 1 |
| `.obx-anim-scale` | `index.html:78,112`（两个 `.modal-box` 上） | 2 |
| `.no-select` | `index.html:64` | 1 |
| `.hidden` | `index.html:50,52,53,54,55` | 5 |

**未使用**：`.sub-sidebar-header`、`.sub-sidebar-footer`（用自有 `.ml-sidebar-footer`）、
`.empty-state`、`.context-menu`、`.pagination-bar`、`.settings-form`、`.loading`、
`.obx-card-lift`、`.obx-skeleton`、`.obx-anim-fade-up/pop` 等类工具（只借用了关键帧名，见 §5.4.3）。

---

### 5.2 布局骨架

#### 5.2.1 顶层容器结构树

```
#app                                        manga-library.css:13-20（display:flex; height:100vh; overflow:hidden）
├─ aside.ml-sidebar.view-sub-sidebar.obx-glass   index.html:12 | css:23-29
│  ├─ .ml-brand（.ml-brand-icon 40×40 圆角 12px + 标题/副标题）  index.html:13-19
│  ├─ nav.ml-nav.obx-scroll                  index.html:21-34 | css:54-62（flex:1; overflow-y:auto）
│  │  ├─ .ml-nav-label「漫画库」+ 3× button.obx-nav-item.ml-nav-item
│  │  └─ .ml-nav-label「下载中心」+ 6× button.obx-nav-item.ml-nav-item
│  └─ .ml-sidebar-footer#ml-stats             index.html:36 | css:80-85
└─ .view-body                                 index.html:40
   ├─ .view-toolbar.ml-toolbar                index.html:41-58 | css:88
   │  ├─ .toolbar-group.ml-view-heading        （#ml-view-title / #ml-view-sub）
   │  └─ .toolbar-group.ml-toolbar-right       （.ml-search / 5 个下载按钮 / 设置）
   └─ .view-content.ml-content.obx-scroll#ml-content   index.html:60 | css:117
（覆盖层，直接挂在 #app 下）
├─ .manga-reader.no-select#manga-reader-container     index.html:64 | css:281-284
│  ├─ .reader-toolbar（.reader-close.btn-icon / .reader-info）
│  ├─ button.reader-arrow.reader-prev
│  ├─ .reader-image-wrapper > img
│  └─ button.reader-arrow.reader-next
├─ #ml-add-modal.modal > .modal-box.obx-anim-scale    index.html:77-108（内联 width:480px）
└─ #ml-detail-modal.modal > .modal-box.obx-anim-scale index.html:111-119（内联 width:560px）
```

#### 5.2.2 关键尺寸与来源

| 项 | 数值 | 来源 |
| --- | --- | --- |
| 侧栏宽度 | `248px` | **插件自定义**：`.ml-sidebar{width:248px}`（`css:24`）覆盖 `base.css:252` 的 `var(--sub-sidebar-width)`（`variables.css:38` = 240px） |
| 侧栏响应式 | `210px`（≤860px） | 插件自定义（`css:310`） |
| 工具栏高度 | 未覆盖 → `var(--toolbar-height)` = `48px` | `base.css:240` + `variables.css:39`；`.ml-toolbar` 只改 `gap:16px`（`css:88`） |
| 搜索框宽度 | `250px` → focus `300px`（≤860px 时 `160px`）；圆角 `999px` | 插件自定义（`css:97-108`、`css:311`） |
| 内容区内边距 | `16px` | `css:117` 二次声明，与 `base.css:280` 同值 |
| 书架网格 | `repeat(auto-fill, minmax(150px,1fr))`，gap `16px`；≤860px 时 `130px` | `css:125-129`、`css:312` |
| 封面比例 | `padding-top:140%`（固定 5:7） | `css:149` |
| 详情封面 | `122px × 164px`（圆角 12px） | `css:199-204` |
| 任务缩略图 | `54px × 72px`（圆角 8px） | `css:234` |
| 进度条 | 轨道 `height:6px`，宽 `150px`（≤860px 时 `100px`） | `css:246-249`、`css:313` |
| 阅读器 | `position:fixed; inset:0; z-index:2000`；工具栏高 `52px`；箭头字号 `42px` | `css:281-306` |
| 滚动方式 | 页面整体不滚动（`#app{overflow:hidden}` `css:16`）；`.ml-nav` 与 `.ml-content` 各自 `overflow-y:auto`（`css:57`、`base.css:279`），两者都带 `.obx-scroll` | — |
| 浮层层级 | 阅读器 2000（`css:282`）> Shell `.modal` 1500（`base.css:162`）> 内容 | — |

自定义布局规则全部集中在侧栏/工具栏/网格/任务行/阅读器五块（`css:23-117`、`css:224-306`），
`.view-body` 及其 flex 语义完全沿用 `base.css`，插件没有重定义任何 `.view-*` 类。

---

### 5.3 设计 token 使用

#### 5.3.1 实际用到的 `--*` 变量

`Select-String -Pattern 'var\(--[a-zA-Z0-9-]+' -AllMatches` → **85 处 / 21 个唯一变量名**。

Shell 提供的（全部命中）：`--text-secondary` ×12、`--accent` ×9、`--bg-hover` ×9、
`--border` ×8、`--text-primary` ×8、`--bg-surface` ×5、`--transition-fast` ×5、
`--text-muted` ×4、`--success` ×2、`--danger` ×2、`--danger-soft` ×2、`--warning` ×2、
`--text-on-accent` ×1、`--bg-app` ×1。
`effects.css` 提供的：`--obx-i` ×2（`css:140/229`）、`--obx-shadow-2` ×1（`css:146`）。

插件自有：`--ml-radius` ×4、`--ml-accent-soft` ×4、`--ml-gradient` ×2、`--ml-hero-bg` ×1，
外加 1 处**跨插件变量** `--mp-glass` ×1（见 §5.7.1）。

#### 5.3.2 自有 `:root` 覆盖

`css:6-11` 定义 4 个变量，未覆盖任何 Shell token：

```css
--ml-radius: 14px;                    /* css:7 */
--ml-gradient: linear-gradient(135deg, var(--accent) 0%, color-mix(in srgb, var(--accent) 55%, #8b5cf6) 100%);
```

`--ml-radius: 14px` / `--ml-radius-sm: 10px` 与 `effects.css:14-15` 的
`--obx-radius: 14px` / `--obx-radius-sm: 10px` 同值（未复用后者）。

#### 5.3.3 硬编码颜色字面量

| 正则 | 命中行数 | 说明 |
| --- | --- | --- |
| `#[0-9a-fA-F]{3,8}\b` | **7** | 5 处为 `#fff`/`#000`（都在阅读器与卡片角标），2 处为装饰色 |
| `rgba?\(` | **6** | 全部在深色阅读器或封面角标上 |
| `gradient\(` | 2 | `css:9`（`--ml-gradient` 里的 `#8b5cf6`）、`css:287`（阅读器工具栏遮罩） |

典型 5 例：

- `css:9` `--ml-gradient` 里的 `#8b5cf6`（纯装饰紫）。
- `css:161` `.ml-badge{color:#fff; background:rgba(0,0,0,0.55)}` —— 封面右下角页数徽标。
- `css:171/175` `.ml-fav-star{color:rgba(255,255,255,0.65)}` / `.active{color:#ffd166}` —— 收藏星。
- `css:282-283` `.manga-reader{background:#000; color:#fff}` —— 阅读器整页黑底（与主题无关，属有意为之）。
- `css:293/301/304` `.reader-info{color:rgba(255,255,255,0.75)}`、`.reader-arrow{color:rgba(255,255,255,0.28)}`、`:hover{color:#fff}`。

对比：分布高度集中——6 个 `rgba(` 中 5 个在阅读器（`css:287-301`），
`#hex` 中 4 个也在阅读器（`css:282/283/292/304`），即**主题外的硬编码几乎只出现在阅读器**。

#### 5.3.4 `data-theme` 处理

`Select-String -Pattern 'data-theme'` 对该插件 `*.css` / `*.html` / `js/*.js` 执行 →
**0 命中**（CSS 内 `:root|data-theme|@media|@keyframes` 只命中 `:root`@6 与 1 处 `@media`@309）。
主题完全依赖壳注入脚本同步；除阅读器黑底外，所有颜色都走 token，因此深浅主题切换不需要插件代码。

---

### 5.4 组件与命名约定

#### 5.4.1 自有类名前缀

`ml-*` 共 **59 个唯一类名**（正则 `\.ml-[a-z0-9-]+` 去重）；连同无前缀的一组
（`.manga-reader` + 6 个 `.reader-*` + `.btn-icon`）合计 67 个。
分组：基础骨架 `ml-sidebar / ml-sidebar-footer / ml-nav / ml-nav-item / ml-nav-label /
ml-brand* / ml-toolbar* / ml-content / ml-view-* / ml-search*`；
书架 `ml-grid / ml-card* / ml-cover* / ml-badge / ml-info / ml-fav-star`；
详情 `ml-detail-* / ml-info-box / ml-info-item / ml-tag / ml-hero-back / ml-section*`；
下载 `ml-task* / ml-status / ml-progress-*`；
表单与空态 `ml-field* / ml-check / ml-empty* / ml-no-anim`。

**前缀不一致**：阅读器一整套与 `.btn-icon` 不带 `ml-`（`css:281-306`：`.manga-reader`、
`.reader-toolbar`、`.reader-close`、`.reader-info`、`.reader-image-wrapper`、`.reader-arrow`、
`.reader-prev`、`.reader-next`、`.btn-icon`），其中 `.btn-icon` 形态像 Shell 类，但
`base.css` 全文只有 `.btn` / `.btn-primary` / `.btn-danger` / `.btn-danger-solid` / `.btn-sm`，
**没有 `.btn-icon`**。

#### 5.4.2 自有组件清单

| 类别 | 类名 | 位置 |
| --- | --- | --- |
| 按钮 | `.btn-icon`（无边框文字按钮）、`.ml-task-actions button`（30×30，圆角 9px）、`.ml-fav-star` | `css:292 / 257-263 / 168-175` |
| 卡片 | `.ml-card`、`.ml-cover`、`.ml-cover-fallback`、`.ml-card-title/sub` | `css:131-166` |
| 徽标 | `.ml-badge`（页数）、`.ml-status`（任务状态 5 色）、`.ml-tag`（标签） | `css:159-163 / 238-245 / 218-221` |
| 进度 | `.ml-progress-track` + `.ml-progress-fill`（`--ml-gradient` 填充，`transition width .4s`） | `css:247-255` |
| 弹窗 | **复用 Shell** `.modal` / `.modal-box` / `.modal-body` / `.modal-footer` | `index.html:77-119` |
| Toast | **复用 Shell** `Toast`：`Toast.error`（`app.js:254/528`）、`Toast.warning`（`app.js:495`）、`Toast.success`（`app.js:505`） | — |
| 确认框 | **复用 Shell** `confirmDialog(..., {danger:true})` | `app.js:471` |
| 空状态 | `.ml-empty` + `.ml-empty-icon` / `.ml-empty-text` / `.ml-empty-hint` | `css:266-272`；调用 5 处：`app.js:182,197,354,366,427` |
| 加载 | 无独立类；图片页在 `#ml-pages` 里内联一段 `obx-anim-spin` + 内联 `border/border-top-color` | `app.js:350` |
| 骨架屏 | **无**（`obx-skeleton` 0 命中） | — |
| 表单 | `.ml-field`、`.ml-field-row`、`.ml-check` | `css:274-278` |
| 阅读器 | `.manga-reader` 及其 `.reader-*` 子件 | `css:281-306` |
| 工具函数 | `MangaUtils.escapeHtml/coverImg`、`DownloadUtils.getStatusText/formatSpeed/formatTime` | `utils.js:2-62` |

#### 5.4.3 Shell 已提供、插件又实现了一遍的能力

1. **卡片网格**：Shell `createCardGrid`（`base.js:915`）↔ 插件 `.ml-grid` + `.ml-card`
   （`css:125-147`，`minmax(150px,1fr)`、封顶交错入场 `--obx-i` 26ms）。
   两处网格：书架（`app.js:201` `_ensureGrid`）与章节/图片（`app.js:298/338`）。
2. **空状态**：Shell `.empty-state`（`base.css:428-431`，居中、`min-height:300px`、单行 16px 文案）
   ↔ 插件 `.ml-empty`（`css:266-272`，`min-height:260px`，图标 44px 带 `obxFloat` 浮动 +
   15px 标题 + 12px 提示），5 个调用点。
3. **表单字段**：Shell `.field` / `.field-label` / `.field-checkbox` / `.field input`
   （`base.css:183-206`，含 focus 态与 checkbox 尺寸 16px）↔ 插件 `.ml-field` / `.ml-check`
   （`css:274-278`，自写 `accent-color`、无 focus 态、无 `required`/`help` 支持）。
   添加任务弹窗 4 个控件（`index.html:81-101`：漫画 ID / 并发数 / 优先级 / 自动开始）与详情弹窗 7 行展示（`app.js:517-525`）全部走自有版。
4. **灯箱/查看器**：Shell `createLightbox`（`base.js:708`，含缩放/拖拽/方向键/信息面板）
   ↔ 插件自研 `.manga-reader` + `MangaReader`（`reader.js:1-76` + `css:281-306`）：
   黑底、无缩放、无信息面板、工具栏 hover 才显形。**0 处**调用 `createLightbox`。
5. **滚动条**：**已正确复用** `.obx-scroll`（`index.html:21/60`），未复制规则 —— 与
   media-player 的做法相反（该插件复制了整段）。
6. **动效**：部分复用 —— `.obx-anim-scale`（`index.html:78/112`）、
   `Motion.retrigger(el,'obx-anim-heart')`（`app.js:249`）、`obx-anim-spin`（`app.js:350`）
   都来自壳；但另有 4 处直接引用**关键帧名**而非工具类：`animation: obxFadeUp`
   （`css:139/189/229/268`）、`animation: obxFloat`（`css:270`），交错延迟手写
   `animation-delay: calc(var(--obx-i,0) * 26ms)`（`css:140/229`），未用 `.obx-stagger`。
7. **搜索框**：复用 Shell `.search-input` 类名 3 处，但外层 `.ml-search`（`css:95-109`）
   重写了 `padding`、`border-radius:999px`、focus 宽度动画，并覆盖 `max-width`。
8. **进度条**：自有 `.ml-progress-track/fill`（`css:247-255`），Shell 无对应组件 → 不算重复实现。
9. **分页 / 目录树 / 右键菜单**：未实现也未复用（`createPagination` / `createTree` /
   `createContextMenu` / `contextmenu` 在该插件 0 命中），列表全量渲染，任务行操作走
   行内按钮（`app.js:450-456`）。

---

### 5.5 交互约定

- **设置入口**：工具栏 `.btn#btn-settings` → `openSettingsModal({ title: '漫画中心设置' })`
  （`app.js:63-65`）。这是**最薄的用法**：不传 `schema` / `values` / `onSave`，全部由壳补齐
  （`base.js:572-577` 调 `get_settings_schema` + `get_settings`，`base.js:615` 默认
  `Bridge.call('save_settings', values)`）。保存反馈由壳完成：`Toast.success` + 400ms 后
  `location.href + '?_t='` 重载（`base.js:621-622`）。插件没有自定义设置面板或保存后逻辑。
- **错误提示**：三级并用——`Toast.error`（`app.js:254` 收藏失败、`app.js:507` 添加任务失败、
  `app.js:528` 详情失败）、`Toast.warning`（`app.js:495` 空输入）、
  `console.error`（`app.js:181/275/401`）、内容区空态 + `icon:triangle-alert`（`app.js:182` 「加载失败」、
  `app.js:366` 图片加载失败、`app.js:525` 详情里用内联 `style="color:var(--danger)"` 显示错误字段）。
- **选择模型**：**单选**。点击卡片即进详情/阅读（`app.js:217-222`），章节卡进图片页
  （`app.js:315-321`），图片卡打开阅读器（`app.js:362-364`）；
  收藏是唯一的二态切换（星形按钮 `app.js:208`，`toggleFav` `app.js:242-256`）。
  无多选、无框选、无长按（无 `selectionMode` / `selectedIds`）。
- **右键菜单**：**无**。所有批量/单条操作都是可见按钮：工具栏 4 个（全部开始/全部暂停/
  清除已完成/添加任务，`app.js:82-93`），任务行内 4 类（暂停/继续/重试/详情/删除，
  `app.js:451-455`）。删除经 `confirmDialog(..., {danger:true})`（`app.js:471`）。
- **键盘快捷键**：只在阅读器内，`reader.js:64-74` 注册在 `document` 上并用
  `container.style.display !== 'flex'` 自守（`reader.js:65`）：
  `←/↑/a/w` 上一页、`→/↓/d/s` 下一页（`reader.js:67-68`）、`Space` 下一页（并 `preventDefault`，
  69-72）、`Esc` 关闭（73）。**主界面（书架/下载中心）没有任何键盘快捷键**，
  搜索框也没有 Esc 清空或 Enter 提交（对比 media-player 的做法）。
- **鼠标翻页**：点击图片区任意处即下一页，箭头按钮除外（`reader.js:58-62`）；
  左右箭头（`.reader-prev/next`，`reader.js:54-55`）。
- **长任务进度与取消**：下载任务由**轮询**驱动，`setInterval(loadTasks, 2000)`（`app.js:44`）。
  进度展示 = `completedImages/totalImages` 百分比条 + `N/M 页` + 速度 + 剩余时间
  （`app.js:432`、`441-443`）；状态徽标 5 态（`DownloadUtils.getStatusText`，`utils.js:31-40`）。
  取消语义 = 删除任务（`download_delete`，`app.js:473`）+ 全局「清除已完成」
  （`download_clear_completed`，`app.js:91`）；没有"暂停后保留进度再继续"以外的取消按钮。
  轮询在 `onHide` 停、`onShow` 重启（`app.js:34-51`）。
- **空 / 加载 / 错误三态**：
  - 空态：`_emptyHtml(icon, text, hint)`（`app.js:186-192`），5 个调用点覆盖书架为空
    「暂无漫画」+ 提示「可在设置中调整漫画根目录」、无图片、加载失败、无下载任务
    「点击右上角「添加任务」开始下载漫画」。
  - 加载态：仅图片页有（`app.js:350`，内联 spinner + 「图片加载中…」）；
    书架/下载中心**没有加载态**——首帧先渲染空态，等 `Bridge.call` 返回后替换
    （下载中心首个 2s 轮询到来前也是空态）。
  - 错误态：复用空态样式 + `icon:triangle-alert`（`app.js:182/366`），无重试按钮；轮询失败只 `console.error`
    （`app.js:401`），界面上不会出现错误（任务行会保留上一次成功快照）。

---

### 5.6 特色设计（值得其他插件吸收）

1. **轮询刷新用快照比对 + `.ml-no-anim` 抑制重播动画**（`app.js:397`、`app.js:420-421`、
   `css:231-233`）：`JSON.stringify(tasks) !== this._tasksKey` 才重渲染，且重渲染时给容器加
   `.ml-no-anim` 关掉 `.ml-task` / `.ml-empty` 的入场动画。解决"2s 轮询导致整张列表每 2 秒
   闪一遍"——这是轮询式进度界面最容易踩的观感坑。
2. **交错入场延迟在 JS 侧封顶**：`style="--obx-i:${Math.min(i, 32)}"`（`app.js:207/311/359`，
   任务行是 `Math.min(i, 24)`，`app.js:435`），CSS 只用 `--obx-i * 26ms`（`css:140`）。
   解决"长列表末尾卡片要等十几秒才出现"。
3. **hero 背景交给 CSS 变量而不是内联背景图**：`--ml-hero-bg` 写在行内（`app.js:283`），
   实际绘制在 `.ml-detail-hero::before`（`css:191-197`，`filter: blur(30px) saturate(1.2)`、
   `opacity:.25`、`transform: scale(1.15)`）。解决"详情页要用封面做氛围背景，但不能糊掉
   封面本身和文字"。
4. **封面失败就地降级为占位块**：`MangaUtils.coverImg` 的 `onerror` 把 `<img>` 换成
   `.ml-cover-fallback` div（`utils.js:24-26`），并给 fallback 图标先做
   `Utils.jsString` 再拼进内联处理器。解决"封面 404 时出现浏览器裂图图标"，
   同时避免内联处理器参数注入。
5. **阅读器完全自持、零依赖**：76 行的 `MangaReader` 同时提供点击翻页、箭头、键盘、
   hover 显隐工具栏（`css:289-291`），关闭时把 `img.src` 置空（`reader.js:22-23`）
   以释放大图内存。解决"沉浸阅读需要全屏无 chrome，又要能一键退出"。
6. **keepAlive 插件的生命周期写法可以直接抄**：`_bindPluginLifecycle()`（`app.js:34-40`）
   用 `window.PluginLifecycle` 的 `onHide/onShow/onDispose` 三件套把 2s 轮询完整托管，
   并带 `if (!window.PluginLifecycle) return;` 兜底；`_startPoll` 自带幂等
   （`if (this.pollTimer) return;`，`app.js:43`）。它声明了 `keepAlive: true`
   （`manifest.json`）并且真的实现了对应钩子——这正是同批插件里 media-player 缺的那部分。

---

### 5.7 与 Shell 契约的偏差（后续统一 UI 必须处理的点）

1. **引用了另一个插件的私有变量 `--mp-glass`。**
   证据：`css:102` `background: var(--mp-glass, var(--bg-surface));` ——
   `--mp-glass` 定义在 `plugins/media-player/frontend/media-player.css:16`，
   在 manga-library 的 iframe 里**永远不存在**，因此该处实际恒为 `var(--bg-surface)`。
   影响面：1 处（搜索框）；后果是"看着有玻璃质感、实际没有"，
   且这是一条跨插件 CSS 耦合，media-player 改名就会静默改变这里的外观。
2. **侧栏宽度脱离 `--sub-sidebar-width`，且与 media-player 不一致。**
   证据：`css:24` `.ml-sidebar{width:248px}` 覆盖 `base.css:252`，断点 `css:310` 改 `210px`；
   `--sub-sidebar-width` 在该插件 CSS 中 0 命中。对照 media-player 是 252px / 210px。
   影响：壳在设置页调子侧栏宽度对两个插件都无效，且两个插件并排时侧栏宽度不同。
3. **弹窗宽度用内联样式改掉 Shell 的 `.modal-box`。**
   证据：`index.html:78` `style="width:480px;"`、`index.html:112` `style="width:560px;"`
   覆盖 `base.css:167` 的 `width:400px`；两处 `.modal-box` 还叠了 `.obx-anim-scale`
   （`index.html:78/112`），与 Shell 自带的 `slideUp 0.2s`（`base.css:168`）形成双动画。
   影响：2 个弹窗；统一弹窗宽度体系（如加 `size` 变体）时必须改这两处 HTML。
4. **表单控件与 Shell `.field` 并存，且输入框尺寸靠内联覆盖。**
   证据：`.ml-field` / `.ml-check`（`css:274-278`）复用不到 Shell 的 `.field` 约定
   （`base.css:182-206`：focus 边框、`required` 星号、`field-help`）；
   弹窗输入框用 Shell `.search-input` 但内联覆盖宽度
   （`index.html:83` `width:100%;max-width:none`、`index.html:88` `width:80px`、
   `index.html:92` `width:120px`）。影响：添加任务弹窗（4 控件）与详情弹窗（7 行）。
5. **自研阅读器替代 `createLightbox`。**
   证据：`createLightbox` 在该插件 0 命中；`reader.js` 76 行 + `css:281-306` 自建。
   层级上 `.manga-reader{z-index:2000}`（`css:282`）**高于** Shell `.modal{z-index:1500}`
   （`base.css:162`）——阅读器打开时壳渲染的弹窗（设置、确认框）会被压在下面，
   而 Shell `.toast-container` 是 3000，提示仍在最上层。影响：1 个全屏组件 +
   与壳弹窗/Toast 三层的相对顺序。
6. **类名前缀约定被阅读器打破。**
   证据：`.manga-reader` / `.reader-*` / `.btn-icon`（`css:281-306`）无 `ml-` 前缀；
   `base.css` 中不存在 `.btn-icon`，因此它既不是 Shell 类也不是规范的插件类。
   影响：6 个类名；用类名前缀做统一扫描/替换（例如批量加 `obx-` 或插件前缀）时会漏掉这一组。
7. **`prefers-reduced-motion` 覆盖不完整。**
   证据：`manga-library.css` 中 0 处 `@media (prefers-reduced-motion)`
   （该文件唯一的 `@media` 是 `css:309` 的宽度断点）；而 `css:139/189/229/268/270`
   直接使用 `obxFadeUp` / `obxFloat` 关键帧，`effects.css:186-201` 的降级白名单只列
   `.obx-anim-*` 与 `.obx-stagger > *`，**不覆盖** `.ml-card` / `.ml-task` / `.ml-empty` /
   `.ml-empty-icon`。影响：4 类元素在"减少动态效果"下仍会动画（其中
   `.ml-empty-icon` 的 `obxFloat` 是无限循环，`css:270`）。
8. **无分页、无虚拟滚动、无骨架屏。**
   证据：`createPagination` 0 命中；书架 `manga_list` 全量一次性 `innerHTML`
   （`app.js:170` + `app.js:202`），下载中心每次变化整段重建（`app.js:431`）；
   `obx-skeleton` 0 命中。影响：大漫画库（数千文件夹）与长任务列表的渲染成本全部落在
   一次同步 `innerHTML` 上；统一列表/分页组件时这里是最主要的接入点。
9. **空态图标的插值口径已收敛**（本条为审计时的差异，现已消除）。
   现状：`_emptyHtml(icon, text, hint)` 把图标名交给壳的 `Icons.html('icon:名字')` 生成标记
   （`app.js:186-193`），text/hint 仍经 `MangaUtils.escapeHtml`；5 个调用点全部传
   `icon:` 常量（`app.js:182/198/355/367/428`），不存在外部数据进入图标插值的路径。
10. **主界面零键盘交互，与同批插件的习惯不一致。**
    证据：键盘只在阅读器内注册（`reader.js:64-74`），书架/下载中心无 `keydown` 监听；
    搜索框也没有 Esc 清空/Enter 提交（`app.js:67-78` 只绑 `input` 与清除按钮点击）。
    影响：搜索（`#manga-search`）与下载筛选两组高频操作在统一键盘方案（如全局 `/` 聚焦搜索）
    落地时需要补挂点。
