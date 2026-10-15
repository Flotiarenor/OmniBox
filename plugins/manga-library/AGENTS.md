> manga-library ｜ manifest: plugins/manga-library/manifest.json

## 定位与形态

- 职责：把 `root_dir` 下含图片的一级子目录聚合成书架，按章节与图片分层浏览并单页阅读，并在同一数据根内承载 jmcomic 下载任务管理。
- 形态：独立页插件，`frontend.route` 为 `/manga-library`；不承载 companion，也不内嵌进其他宿主。


## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/manga-library/backend/main.py` | 插件主类、设置 schema、API 注册、书架缓存与刷新钩子、下载任务编排 | `MangaLibraryPlugin`、`settings_schema`、`SHELF_VERSION`、`register_api`、`get_data_root`、`get_settings`、`on_settings_changed`、`_apply_root_dir`、`_reset_shelf`、`freshness_spec`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_rebuild`、`_load_state`、`_save_state`、`_scan_manga`、`list_manga`、`search`、`get_state`、`toggle_favorite`、`_is_valid_folder_name`、`update_recent`、`get_detail`、`get_pages`、`_get_download_state_dir`、`download_list`、`download_submit`、`download_pause`、`download_resume`、`download_retry`、`download_delete`、`download_start_all`、`download_pause_all`、`download_clear_completed`、`download_get_album_info`、`_download_start`、`_download_worker`、`_save_album_info` |
| `plugins/manga-library/backend/scanner.py` | 目录扫描与封面选择的纯函数 | `IMAGE_EXTS`、`natural_sorted`、`visible_subdirs`、`direct_images`、`has_images`、`load_album_info`、`find_cover`、`scan_manga`、`resolve_safe_path`、`list_pages` |
| `plugins/manga-library/backend/download_models.py` | 下载任务数据类与两种序列化 | `DownloadTask`、`to_api_dict`、`to_state_dict`、`from_state_dict`、`albumId`、`thumbUrl`、`totalImages`、`completedImages`、`downloadDir`、`startTime`、`completeTime` |
| `plugins/manga-library/backend/download_state.py` | 任务表 JSON 落盘与恢复 | `save_tasks`、`load_tasks` |
| `plugins/manga-library/backend/downloader.py` | jmcomic 下载执行、进度回调与 `album_info` 组装 | `execute_download`、`ProgressCallback`、`before_album`、`before_photo`、`after_image`、`_build_album_info`、`_save_album_info_local`、`_save_state_locked`、`ProgressDownloader`、`dir_rule`、`Bd / Aid`、`Bd / Aid / Pindextitle` |
| `plugins/manga-library/frontend/index.html` | 页面骨架、阅读器与两个弹窗、脚本装载 | `#app`、`.ml-sidebar`、`.ml-nav`、`#ml-stats`、`.view-body`、`.ml-toolbar`、`#ml-view-title`、`#ml-view-sub`、`#ml-search-wrap`、`#manga-search`、`#ml-search-clear`、`#ml-freshness`、`#ml-content`、`#manga-reader-container`、`#ml-add-modal`、`#ml-detail-modal`、`#ml-detail-content` |
| `plugins/manga-library/frontend/manga-library.css` | 插件全部样式 | `--ml-radius`、`--ml-radius-sm`、`--ml-gradient`、`--ml-accent-soft`、`--ml-hero-bg`、`#app`、`.ml-sidebar`、`.ml-brand`、`.ml-nav`、`.ml-nav-label`、`.ml-nav-item`、`.ml-sidebar-footer`、`.ml-toolbar`、`.ml-view-title`、`.ml-view-sub`、`.ml-content`、`.ml-section`、`.ml-section-title`、`.ml-grid`、`.ml-card`、`.ml-cover`、`.ml-cover-fallback`、`.ml-badge`、`.ml-info`、`.ml-card-title`、`.ml-card-sub`、`.ml-fav-star`、`.ml-detail-hero`、`.ml-detail-cover`、`.ml-detail-info`、`.ml-detail-label`、`.ml-detail-title`、`.ml-detail-author`、`.ml-detail-actions`、`.ml-hero-back`、`.ml-info-box`、`.ml-info-item`、`.ml-tag`、`.ml-task`、`.ml-task-thumb`、`.ml-task-main`、`.ml-task-title`、`.ml-task-meta`、`.ml-status`、`.ml-task-progress`、`.ml-progress-track`、`.ml-progress-fill`、`.ml-task-percent`、`.ml-task-actions`、`.ml-no-anim`、`.ml-field`、`.ml-field-row`、`.ml-check`、`.manga-reader`、`.reader-toolbar`、`.reader-close`、`.reader-info`、`.reader-image-wrapper`、`.reader-arrow`、`.reader-prev`、`.reader-next`、`.btn-icon` |
| `plugins/manga-library/frontend/js/app.js` | 应用主类：视图分发与渲染、下载中心、弹窗、轮询与生命周期 | `MangaLibraryApp`、`currentView`、`downloadFilter`、`tasks`、`_tasksKey`、`reader`、`viewLevel`、`_chapterPath`、`init`、`_bindPluginLifecycle`、`_startPoll`、`_stopPoll`、`_bindUI`、`switchView`、`_mountFreshness`、`_autoSyncFreshness`、`loadView`、`_emptyHtml`、`_renderGrid`、`_ensureGrid`、`toggleFav`、`openMangaDetail`、`renderChapters`、`renderImages`、`_detailInfoHtml`、`loadTasks`、`updateStats`、`renderDownloads`、`showAddModal`、`closeAddModal`、`confirmAdd`、`showTaskDetail`、`closeDetailModal` |
| `plugins/manga-library/frontend/js/reader.js` | 沉浸式单页阅读器 | `MangaReader`、`open`、`close`、`renderPage`、`prev`、`next`、`_bindEvents` |
| `plugins/manga-library/frontend/js/utils.js` | 转义与封面降级、下载文案格式化 | `MangaUtils`、`escapeHtml`、`coverImg`、`DownloadUtils`、`getStatusText`、`formatSpeed`、`formatTime` |

**落盘产物**：数据根为 `root_dir`（`get_data_root` 返回 `self.manga_dir`）。`<漫画根>/.cache/manga_state.json` 由 `_load_state`、`_save_state` 读写；`<漫画根>/.jmcomic_state/download_state.json` 由 `load_tasks`、`save_tasks` 读写，状态目录下 `progress_` 前缀的进度快照由 `_save_state_locked` 写入，状态目录建不出来时回落到用户主目录下的 `.jmcomic_state`；`album_info.json` 写在漫画目录内，写入符号为 `_save_album_info`、`_save_album_info_local`。

## 后端 API 契约

**对外方法**（`register_api` 暴露）：

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `get_settings` | 无 | `{root_dir, recent_count}` | 返回当前生效的设置值 |
| `save_settings` | `settings` | `{success}` / `{success, error}` | 基类实现：写设置存储并触发 `on_settings_changed` |
| `manga_list` | 无 | `List[Dict]` | 书架全部漫画，读内存缓存 |
| `manga_search` | `keyword` | `List[Dict]` | 在 `title`、`author` 上做小写子串匹配，空关键字等价 `manga_list` |
| `manga_get_state` | 无 | `{recent, favorites}` | 按 `folder_name` 取回漫画对象，`recent` 截断到 `recent_count` |
| `manga_toggle_favorite` | `folder_name` | `bool` | 切换收藏并返回切换后的状态，非法目录名返回 `False` |
| `manga_update_recent` | `folder_name`、`page` | `{status}` / `{status, error}` | 去重后插到队首，最多保留 20 条 |
| `manga_get_detail` | `folder_name` | `{folder_name, title, author, is_fav, is_multi_chapter, info, chapters}` / `{}` | 路径非法或不存在时返回空字典，`chapters` 每项为 `{name, cover_url, path}` |
| `manga_get_pages` | `folder_name`、`chapter_path` | `List[str]` | 相对漫画根的 posix 图片路径，越界或不存在返回 `[]` |
| `download_submit` | `album_id`、`concurrency`、`priority`、`auto_start` | `{taskId, success}` / `{success, error}` | `album_id` 必须纯数字，`concurrency` 夹到 1 至 10 |
| `download_list` | 无 | `{tasks}` | 任务摘要，先按 `priority` 再按 `startTime` 排序 |
| `download_pause` | `task_id` | `{success}` / `{success, error}` | 仅 `downloading` 状态可暂停 |
| `download_resume` | `task_id` | `{success}` / `{success, error}` | 仅 `paused` 状态可恢复并起线程 |
| `download_retry` | `task_id` | `{success}` / `{success, error}` | 仅 `failed` 状态可重试，清 `error` 与 `completed_images` |
| `download_delete` | `task_id` | `{success}` / `{success, error}` | 删除任务记录，不删已下载文件 |
| `download_start_all` | 无 | `{success}` | 全部 `paused` 任务转 `queued` 并起线程 |
| `download_pause_all` | 无 | `{success}` | 全部 `downloading` 任务转 `paused` |
| `download_clear_completed` | 无 | `{success, deleted}` | 删除全部 `completed` 记录 |
| `download_get_album_info` | `album_id` | `album_info.json` 字段 + `exists` / `{error, exists: False}` | 读取已下载漫画的 `album_info.json`，越界返回 `非法路径` |

**壳按名调用的成员**：

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `MangaLibraryPlugin` | `manifest`、`config` | 实例 | 壳按 `backend.class` 实例化的插件类 |
| `register_api` | 无 | `Dict[str, Callable]` | 壳据此注册全部 `manga_*` 与 `download_*` 方法 |
| `settings_schema` | 无 | `List[Dict]` | 设置面板字段：`root_dir`、`recent_count` |
| `get_data_root` | 无 | `Path` | `/file` 路由的允许根 |
| `get_settings` | 无 | `Dict` | 设置面板回填值 |
| `on_settings_changed` | `changed_keys` | 无 | `root_dir` 换根、`recent_count` 夹到 1 至 50 |
| `freshness_spec` | 无 | `Dict` | 声明 `roots`、`include`、`derive`、`prune`、`on_verified`、`rebuild`、`content_version`、`unit`、`min_sync_interval`、`stat_entries` |

## 前端结构

**DOM 结构树**：

- `#app`（`div`）
  - `aside`，类 `.ml-sidebar`、`.view-sub-sidebar`、`.obx-glass`，属性 `data-obx-splitter`、`data-obx-splitter-min`、`data-obx-splitter-reserve`、`data-obx-splitter-default`
    - `.ml-brand` → `.ml-brand-icon`（内嵌 `svg.obx-icon`）、`.ml-brand-text` → `.ml-brand-title`、`.ml-brand-sub`
    - `nav`，类 `.ml-nav`、`.obx-scroll`
      - `.ml-nav-label`（漫画库），后接三个 `button`，类 `.obx-nav-item`、`.ml-nav-item`，属性 `data-view` 取 `all`、`favorites`、`recent`
      - `.ml-nav-label`（下载中心），后接六个 `button`，类 `.obx-nav-item`、`.ml-nav-item`，属性 `data-view` 固定为 `downloads`，属性 `data-dl-filter` 取 `all`、`downloading`、`queued`、`paused`、`completed`、`failed`
    - `.ml-sidebar-footer`，id `#ml-stats`
  - `.view-body`
    - `.view-toolbar`，类 `.ml-toolbar`
      - `.toolbar-group`，类 `.ml-view-heading` → `.ml-view-title`（id `#ml-view-title`）、`.ml-view-sub`（id `#ml-view-sub`）
      - `.toolbar-group`，类 `.ml-toolbar-right`
        - `.search-field`（id `#ml-search-wrap`）→ `.search-field-icon`、`input#manga-search`、`button#ml-search-clear`（类 `.search-field-clear`、`.hidden`）
        - `button#ml-add-task`（类 `.btn`、`.btn-primary`、`.hidden`）、`button#ml-start-all`（类 `.btn`、`.hidden`）、`button#ml-pause-all`（类 `.btn`、`.hidden`）、`button#ml-clear-completed`（类 `.btn`、`.hidden`）
        - `div#ml-freshness`
        - `button#btn-settings`，类 `.btn`
    - `.view-content`，类 `.ml-content`、`.obx-scroll`，id `#ml-content`
  - `.manga-reader`，类 `.no-select`，id `#manga-reader-container`
    - `.reader-toolbar` → `button.reader-close`（类 `.btn-icon`）、`.reader-info`
    - `button.reader-arrow`，类 `.reader-prev`
    - `.reader-image-wrapper` → `img`
    - `button.reader-arrow`，类 `.reader-next`
  - `.modal`（id `#ml-add-modal`）→ `.modal-box`（类 `.obx-anim-scale`）、`h3`、`.modal-body` → `.ml-field`（`input#ml-input-id`，类 `.search-input`）、`.ml-field-row` → `.ml-field`（`input#ml-input-concurrency`，类 `.search-input`）、`.ml-field`（`select#ml-input-priority`，类 `.search-input`）、`.ml-check`（`input#ml-input-auto-start`）、`.modal-footer` → `button#ml-cancel-add`（类 `.btn`）、`button#ml-confirm-add`（类 `.btn`、`.btn-primary`）
  - `.modal`（id `#ml-detail-modal`）→ `.modal-box`（类 `.obx-anim-scale`）、`h3`、`.modal-body`（id `#ml-detail-content`）、`.modal-footer` → `button#ml-close-detail`（类 `.btn`）

**脚本装载顺序**：`index.html` 依次引入 `js/utils.js`、`js/reader.js`、`js/app.js`，随后内联脚本实例化 `MangaLibraryApp` 并调用 `init`，把实例挂到 `window.mangaLibraryApp`。壳注入的 `/shell/base.js`、`/shell/freshness.js`、`/shell/splitter.js` 等引导脚本先于以上三个脚本执行。

**动态渲染的类名清单**：`.ml-grid`、`.ml-card`、`.ml-fav-star`、`.ml-cover`、`.ml-cover-fallback`、`.ml-cover-fallback-parent`、`.ml-badge`、`.ml-info`、`.ml-card-title`、`.ml-card-sub`、`.ml-detail-hero`、`.ml-hero-back`、`.ml-detail-cover`、`.ml-detail-info`、`.ml-detail-label`、`.ml-detail-title`、`.ml-detail-author`、`.ml-detail-actions`、`.ml-info-box`、`.ml-info-item`、`.ml-tag`、`.ml-section`、`.ml-section-title`、`.ml-task`、`.ml-task-thumb`、`.ml-task-main`、`.ml-task-title`、`.ml-task-meta`、`.ml-status`、`.ml-task-progress`、`.ml-progress-track`、`.ml-progress-fill`、`.ml-task-percent`、`.ml-task-actions`、`.ml-field`、`.ml-no-anim`、`.empty-state`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`、`.obx-anim-spin`、`.btn-primary`、`.active`、`.hidden`、`.danger`，以及按任务状态拼接在 `.ml-status` 上的 `downloading`、`queued`、`paused`、`completed`、`failed`。

## 与壳的契约

- **壳类**：`PluginBase`（`shell/backend/plugin_base.py`），本插件用到的成员为 `setting`、`save_settings`、`settings_schema`、`get_data_root`、`on_settings_changed`。
- **壳基建**：`engine_for`（`shell/backend/freshness.py`）返回 `FreshnessEngine`，本插件调用其 `reset_throttle`；分片模块用 `load_sibling`（`shell/backend/plugin_utils.py`）加载。
- **壳 CSS 变量**：`--accent`、`--danger`、`--danger-soft`、`--success`、`--warning`、`--bg-app`、`--bg-surface`、`--bg-hover`、`--border`、`--text-primary`、`--text-secondary`、`--text-muted`、`--text-on-accent`、`--sub-sidebar-width`、`--transition-fast`、`--obx-shadow-2`、`--obx-i`。
- **插件自有 CSS 变量前缀**：`--ml-`，具体为 `--ml-radius`、`--ml-radius-sm`、`--ml-gradient`、`--ml-accent-soft`、`--ml-hero-bg`。
- **共享前端组件**：`Freshness.mount`（`shell/frontend/public/shell/freshness.js`）返回对象的 `autoSync`；`ShellSplitter` 的 `data-obx-splitter` 属性族（`shell/frontend/public/shell/splitter.js`）；`Icons.html`（`shell/frontend/public/shell/icons.generated.js`）；`settings_schema` 中 `type` 为 `directory` 的字段由 `FolderPicker.createList` 渲染（`shell/frontend/public/shell/folder-picker.js`）。
- **壳运行时函数**：`Bridge.call`、`Bridge.originalUrl`、`Utils.debounce`、`Utils.escapeHtml`、`Utils.jsString`、`Toast.success`、`Toast.error`、`Toast.warning`、`confirmDialog`、`openSettingsModal`、`Motion.retrigger`、`PluginLifecycle.onShow`、`PluginLifecycle.onHide`、`PluginLifecycle.onDispose`。
- **壳样式类**：`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.view-sub-sidebar`、`.obx-scroll`、`.obx-glass`、`.no-select`、`.obx-nav-item`、`.obx-icon`、`.btn`、`.btn-primary`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`、`.active`、`.obx-anim-scale`、`.obx-anim-spin`、`.obx-anim-heart`、`.empty-state`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`、`.search-field`、`.search-field-icon`、`.search-field-clear`、`.search-input`、`.hidden`。
- UI 约定见 `docs/plugin-ui-guide.md`。
