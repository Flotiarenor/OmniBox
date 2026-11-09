> image-viewer ｜ manifest: plugins/image-viewer/manifest.json

## 定位与形态

- 职责：浏览本地图片目录，把文件夹聚合为相册树，并以混合瀑布流与灯箱查看图片。
- 形态：独立页插件（`frontend.route` = `/image-viewer`）；页面 chrome 由 manifest 的 `frontend.chrome` 声明，壳的 `PluginChrome` 渲染品牌块、左栏导航、扩展入口、顶栏动作、搜索框、状态栏与侧栏分隔条，壳加载内容页时追加 `?content=1`，iframe 内只有内容（相册卡片、justified 图片网格、分页、灯箱、弹窗、缩略图重建进度卡）。
- 宿主形态：companion 插件（image-cleaner / pixiv-sync，二者在各自 `manifest.json` 里声明 `dependencies: ["image-viewer"]`）的入口由壳的 `PluginExtensions` 渲染，点击后在宿主页内容槽打开 `NativePanel`（侧栏/顶栏保留，内容 iframe 用 `v-show` 隐藏、关闭后原位恢复）；`#extension-frame` 与 `openExtensionView` 仅给不认 native 的旧壳，当前壳不进入这里。
- `manifest.json` 关键字段：`name` = `image-viewer`；`backend.entry` = `backend/main.py`；`backend.class` = `ImageViewerPlugin`；`frontend.entry` = `frontend/index.html`；`frontend.route` = `/image-viewer`；`frontend.chrome` 含 `brand`、`nav`（`browse` 组）、`toolbar`、`search`、`status`、`freshness`、`extensions`；`keepAlive` = `true`；`permissions` = `filesystem:read`、`filesystem:write`；`dependencies` = `["manga-library"]`（读它的漫画根来界定「一本书」，见 `backend/books.py`；对方未安装或未配置时整体退化成普通图片）；`icon` = `icon:images`。

## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/image-viewer/manifest.json` | 插件声明、入口指向与壳渲染的页面 chrome | `ImageViewerPlugin`、`backend.entry`、`frontend.route`、`frontend.chrome` |
| `plugins/image-viewer/backend/main.py` | 入口（facade）：`register_api` 绑定表、壳按名调用的钩子、附属插件契约成员、装配与诊断（`settings_schema` 引用 `const.SETTINGS_SCHEMA`） | `ImageViewerPlugin`、`settings_schema`、`register_api`、`diagnose`、`get_file_roots`、`resolve_file_path`、`get_data_root`、`get_thumb_data`、`ensure_thumb`、`get_cache_dir`、`freshness_spec`、`on_settings_changed`、`on_unload`、`list_roots`、`delete_files`、`invalidate_derived` |
| `plugins/image-viewer/backend/const.py` | 设置项 schema、扩展名、性能上限、硬默认值与失效键集合（只有入口加载）；相册索引 schema 版本 `Limits.album_cache_version` 当前为 8（条目形状变化必须 +1） | `SETTINGS_SCHEMA`、`IMAGE_EXTENSIONS`、`Limits`、`LIMITS`、`HARD_DEFAULTS`、`SORT_KEYS`、`ROOT_KEYS` |
| `plugins/image-viewer/backend/naming.py` | Pixiv 命名约定下的排序、作品分组与封面策略（无状态） | `PixivNaming`、`number`、`page_index`、`work_units`、`pick_cover`、`cover_rank`、`sort`、`is_work_dir`、`image_sort_key` |
| `plugins/image-viewer/backend/paths.py` | 数据根、派生路径、虚拟路径与命名空间（含命名空间映射缓存）；「配置里的根」与「当前存在的根」分两套：统一刷新用前者，`/file` 允许根与 `is_safe` 用后者 | `PathLayout`、`NAMESPACE_MARKER`、`same_path`、`roots`、`freshness_roots`、`extra_roots`、`token_map`、`roots_index`、`namespace_nodes`、`namespace_for`、`split_virtual`、`virtual_path`、`resolve_dir`、`resolve_path`、`is_namespace_node`、`is_safe`、`resolve_file_path`、`list_roots`、`browse_dir`、`virtual_dir_exists` |
| `plugins/image-viewer/backend/settings.py` | 按目录的设置读取与逐级继承、Pixiv 标记 | `SettingsResolver`、`for_path`、`save_folder`、`clear_folder`、`invalidate`、`pixiv_mode`、`pixiv_fuzzy_mode`、`compute_pixiv_flags`、`stats` |
| `plugins/image-viewer/backend/thumbs.py` | 尺寸元数据缓存、缩略图字节与落盘时机；另发强制重建的单调版本号 | `ThumbStore`、`get_image_size`、`fill_image_sizes`、`flush`、`clear_meta`、`get_thumb_data`、`ensure_thumb`、`get_image_info`、`drop`、`delete_key`、`prune_keys`、`bump_rebuild_version` |
| `plugins/image-viewer/backend/scanner.py` | 单目录裸扫描（无状态，封面规则由调用方给出） | `DirScanner`、`scan`、`direct_units` |
| `plugins/image-viewer/backend/dirs.py` | 全树目录枚举与落盘快照（快照带 `walked_at` 与各根 mtime，读回按那次走盘计 TTL） | `DirSnapshot`、`_load`、`_restore`、`_save`、`ensure`、`walk`、`snapshot`、`record`、`roots_fingerprint`、`roots_mtime`、`prewarm`、`reset`、`diagnostics` |
| `plugins/image-viewer/backend/albums.py` | 相册索引的生命周期（TTL、schema 版本、落盘、重建代次） | `AlbumIndex`、`list_albums`、`invalidate`（`keep_snapshot`）、`reset_index`、`_finish`、`_build`、`_load_index`、`_drop_index_file`、`_save_index`、`cache_version`、`dir_entries`、`diagnostics` |
| `plugins/image-viewer/backend/tree.py` | 相册树的一次全树构建：目录集合 + 目录级条目 → 相册列表（计数、单元数、封面与封面版本） | `AlbumTreeBuilder`、`build` |
| `plugins/image-viewer/backend/books.py` | 与漫画阅读插件对齐的书范围：漫画根下的一级子目录是一本书，给出整本的页序、封面与标题 | `MangaBooks`、`kind`、`is_book`、`book_flags`、`book_pages`、`book_title`、`invalidate` |
| `plugins/image-viewer/backend/recent.py` | 时间线「按图片」的跨目录最近更新分页：以上界做优先队列展开，只扫可能进入前 K 名的目录；一个更新单元 = 一本书（漫画根界定）、一个作品（同作品号的多页）或一张单图 | `RecentImages`、`list_recent_images`、`_expand`、`_push_book`、`_work_name`、`_public_unit`、`_Candidate` |
| `plugins/image-viewer/backend/entries.py` | 目录级条目存储：带 mtime 校验的扫描与递归聚合（聚合结果带封面版本 `total_cover_mtime`） | `DirEntryCache`、`scan`、`children_total`、`aggregate`、`cached_total`、`drop_nodes`、`misses` |
| `plugins/image-viewer/backend/album_config.py` | 相册可见性配置与「保留可见的空目录」标记 | `AlbumConfig`、`get`、`set_action`、`mark_visible`、`replace_visible_marks`、`drop_marks_under`、`prune` |
| `plugins/image-viewer/backend/tiles.py` | 子目录瓦片、按批补齐与灯箱连续浏览序列 | `AlbumTiles`、`scan_items`、`resolve_pending`、`load_album_tiles`、`list_album_images`、`card_cover_size` |
| `plugins/image-viewer/backend/listing.py` | 目录内的分页列表与列表缓存 | `Listing`、`list_images`、`list_folder_items`、`list_dir`、`clear_cache`、`cache_entries` |
| `plugins/image-viewer/backend/file_ops.py` | 目录与文件的新增、删除、移动、定向重建缩略图；删除 / 移动只改文件时保留目录快照，按动过的目录精确丢条目 | `FileOps`、`create_folder`、`delete_folder`、`delete_files`、`move_files`、`regenerate_thumbs`、注入的 `invalidate_derived` |
| `plugins/image-viewer/backend/rebuild.py` | 缩略图全量 / 增量重建的后台任务；重建不改源文件 mtime，任务领一个单调版本号交给前端换 URL | `RebuildTask`、`rebuild_all`、`rebuild_folder`、`collect_all_images`、`status`（含 `version`）、`cancel`、`shutdown` |
| `plugins/image-viewer/backend/freshness.py` | 接入壳的统一刷新基建（同步 / 校验）、目录集合信号、派生数据失效入口与兼容刷新入口；`derive` 区分纯 `restamp` 批次 | `FreshnessProvider`、`spec`、`derive`、`prune`、`dirs_changed`、`invalidate_derived`、`keep_snapshot`、`dirs`、`audit`、`pass_end`、`rebuild`、`refresh` |
| `plugins/image-viewer/frontend/index.html` | 内容页入口：内容容器、旧 embed 回落、三个弹窗、保存失败错误区、进度卡与脚本装载顺序（页面 chrome 不在这里） | `#app`、`.view-body`、`#iv-content`、`#iv-albums`、`#image-grid`、`#pagination`、`#extension-view`、`#settings-modal`、`#settings-errors`、`#setting-follow-shell`、`#setting-plugin-alpha`、`js/app.js` |
| `plugins/image-viewer/frontend/image-viewer.css` | 插件样式与自有 CSS 变量 | `--iv-radius`、`--iv-gradient`、`.iv-album`、`.iv-image-card`、`.iv-context-menu`、`.extension-view` |
| `plugins/image-viewer/frontend/js/app.js` | `ImageViewer` 类骨架、初始化、生命周期、与壳的 chrome 消息、UI 事件绑定、旧 embed 回落 | `ImageViewer`、`init`、`_bindUI`、`_bindPluginLifecycle`、`_postHost`、`_pushChrome`、`_freshnessScope`、`_bindHost`、`_applyHostView`、`_applyHostAction`、`_onQuery`、`_serveHostChannel`、`loadSettings`、`openExtensionView`、`closeExtensionView`、`_applyAlbumSortSettings`、`_embedUrl` |
| `plugins/image-viewer/frontend/js/app-albums.js` | 相册树浏览、渲染、作者网格排序与时间线展示切换 | `loadAlbums`、`showAlbums`、`_renderAlbumCards`、`_renderTimeline`、`setTimelineMode`、`_renderTimelineToggle`、`_filterVisibleAlbums`、`_isCollapsed`、`_sortAlbums`、`_gridIsPixiv`、`_gridConfigPath`、`_ensureGrid`、`openAlbum`、`_showFolder`、`_rememberScroll`、`_restoreScroll`、`_popNavStack` |
| `plugins/image-viewer/frontend/js/app-nav.js` | 返回栈、相册右键菜单、状态栏统计（相册数 / 图片数）、新建相册、根目录列表 | `_handleBack`、`showAlbumMenu`、`_closeAlbumMenu`、`_updateStats`、`_resetRootsList`、`_rootPaths`、`openNewAlbumModal`、`createAlbum` |
| `plugins/image-viewer/frontend/js/app-grid.js` | 图片网格、Justified 布局、瓦片二级加载、幻灯片、多选 / 移动 / 删除、选中计数上报 | `loadImages`、`loadRecentImages`、`_reloadPage`、`_reloadCurrentView`、`renderJustifiedLayout`、`_showFromCard`、`_upgradeSequence`、`_resetAlbumTiles`、`_watchAlbumTile`、`_flushAlbumTiles`、`_loadAlbumTileBatch`、`_applyAlbumTile`、`filterCurrentImages`、`toggleSlideshow`、`_stopSlideshow`、`toggleMultiSelectMode`、`_updateSelectionCount`、`toggleSelectImage`、`clearSelection`、`handleContextAction`、`deleteSelectedImages`、`openMoveModal`、`confirmMove` |
| `plugins/image-viewer/frontend/js/app-refresh.js` | 缩略图重建进度卡、统一刷新完成后的重载与重新可见时的作用域同步 | `_syncFreshness`、`_onFreshnessChanged`、`refreshView`、`rebuildAll`、`rebuildFolder`、`_startRebuildTask`、`_waitRebuildDone`、`_updateRebuildProgress`、`hideRebuildProgress`、`cancelRebuild`、`refreshSelectedThumbs` |
| `plugins/image-viewer/frontend/js/app-settings.js` | 设置弹窗读写、通用外观块与灯箱缓存下发 | `openSettingsModal`、`closeSettingsModal`、`saveSettings`、`_saveStep`、`_showSettingsErrors`、`_clearSettingsErrors`、`_applyLightboxCache`、`_syncAppearanceControls`、`_bindAppearanceSettings` |
| `plugins/image-viewer/frontend/js/app-utils.js` | 缩略图 URL（mtime 毫秒版本 + 强制重建的单调版本）、转义、空态、时间格式化 | `_thumbUrl`、`_forcedThumbVersion`、`_rememberThumbVersions`、`_escapeHtml`、`_escapeAttr`、`_emptyHtml`、`_timeAgo`、`_monthKey`、`_formatDuration` |
| `plugins/image-viewer/frontend/js/justified-layout.js` | Justified 行布局计算 | `JustifiedLayout`、`JustifiedLayout.compute` |

落盘缓存产物位于 `cache_dir`，其路径由 `PathLayout.rebind` 派生：`image_meta.json`（`ThumbStore.flush` / `clear_meta`）、`albums_index.json`（`AlbumIndex` 的加载与落盘）、`dirs_index.json`（`DirSnapshot` 的加载与落盘）、`albums_config.json`（`AlbumConfig`）、`thumbs.db`（`ThumbCache`）、`freshness.db`（壳的 `FreshnessStore`）。

## 后端 API 契约

`register_api` 暴露的方法由前端经 `Bridge.call` 调用，壳把它映射到以插件名与方法名拼接的 HTTP 名。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `list_images` | `rel_path`、`page`、`per_page`、`sort_by`、`sort_order` | `{images, page, total, has_next, has_prev, settings}` | 单目录图片分页列表 |
| `list_folder_items` | `rel_path`、`page`、`per_page`、`sort_by`、`sort_order` | `{items, all_images, all_truncated, all_capped, all_offset, page, total, image_total, pending_count, sequence_pending, has_next, has_prev, settings}` | 混合列表：直接图片 + 子相册 p0 瓦片 |
| `load_album_tiles` | `paths` | `{tiles: {path: {cover, cover_mtime, total_count, image_count, width, height, use_time_name, mtime}}}` | 按批补齐进入视口的瓦片封面与递归计数；`cover_mtime` 是封面文件自身的 mtime，作缩略图版本号 |
| `list_album_images` | `rel_path` | `{images, offset, capped}` | 灯箱用的连续浏览序列；每个作品块恒按文件名自然序 `p0 → pN`，块首即该瓦片的封面（排序设置只决定瓦片顺序，不翻转块内页序） |
| `list_dir` | `rel_path` | `[{name, path}]` | 目录树（移动目标选择） |
| `list_albums` | 无 | `{albums, config, changed}` | 相册全树展开；每个相册带 `cover` 与 `cover_mtime`（封面文件自身的 mtime，前端拼成缩略图版本号） |
| `list_recent_images` | `page`、`per_page` | `{items, all_images, page, per_page, total, reach, image_total, has_next, has_prev, scanned_dirs, partial}` | 跨目录按最近更新倒序的**更新单元**分页（时间线「按图片」）；单元与 `list_folder_items` 的条目同形：同一作品号的多页、漫画根界定的整本（漫画 / 本子，含多章）合并成 `type: 'album'` 卡（`cover` 为封面、`cover_mtime` 为封面文件自身的 mtime、`image_count` / `total_count` 为页数、`mtime` 为组内最新页），其余文件是 `type: 'image'`；`all_images` 是本页单元按序展开的连续浏览序列（封面恒在首位，章按自然序、p0 → pN），第 i 个单元的封面下标 = 前面各单元张数累加；`total` 是整库单元数、`reach` 是本次扫描预算内排列出的单元数、`image_total` 是图片张数；`partial` 表示排序依据不完整（未展开的子树里可能有更靠前的单元），`has_next` 只在这次没把本页填满时为假，前端按 `has_next` 给下一页、按 `reach` 提示「仅列出最近 N 项」 |
| `create_folder` | `rel_path` | `{success, path}` / `{success, error}` | 新建相册目录 |
| `delete_folder` | `rel_path` | `{success}` / `{success, error}` | 删除递归无图片的空目录 |
| `get_album_config` | 无 | `{collapsed, promoted, expanded, visible_empty_dirs}` | 读取相册可见性配置 |
| `set_album_config` | `rel_path`、`action`（`collapse` / `expand` / `promote` / `unpromote`） | `{success, config}` | 收纳 / 展开 / 提升相册 |
| `get_image_info` | `rel_path` | `{success, rel_path, size, width, height}` / `{success, error}` | 灯箱信息面板的尺寸与体积 |
| `delete_files` | `rel_paths` | `{deleted, errors}` | 删除图片；有成功删除时作废相册索引、列表缓存与漫画根解析，但**保留目录快照**，只按动过的目录精确丢条目 |
| `move_files` | `rel_paths`、`dest_rel` | `{moved, errors}` | 移动图片到目标目录；有成功移动时同样保留目录快照，按源目录与目标目录精确丢条目 |
| `regenerate_thumbs` | `rel_paths` | `{regenerated, errors, version}` | 定向重建选中图片的缩略图；`version` 是这次强制重建的单调毫秒版本号（0 = 没有实际重建），前端据此换缩略图 URL |
| `refresh` | 无 | `{success, ...report}` | 兼容入口，转发到壳的统一刷新同步 |
| `rebuild_all` | `rel_path`、`force` | `{started, running, total, version}` | 全量 / 指定文件夹缩略图重建；`version` 是这次重建的单调毫秒版本号（与 `rebuild_status` 同值） |
| `rebuild_folder` | `rel_path` | `{started, running, total, version}` | 指定文件夹增量补齐 |
| `rebuild_status` | 无 | `{running, done, success, cancelled, rebuild_path, total, processed, current, error_count, errors, version}` | 重建任务进度；`version` 供前端在完成后换缩略图 URL |
| `rebuild_cancel` | 无 | `{success}` / `{success, error}` | 取消重建任务 |
| `get_settings` | `rel_path` | `row_height`、`per_page`、`sort_by`、`sort_order`、`pixiv_fuzzy`、`album_sort_by`、`album_sort_order`、`root_dir`、`pixiv_explicit` | 按目录读取生效设置（含逐级继承） |
| `save_settings`（实现为 `SettingsResolver.save_folder`） | `rel_path`、`settings` | `{success}` | 保存全局设置或文件夹级设置 |
| `get_root_dir` | 无 | 第一根目录的绝对路径 | 读取主数据根 |
| `list_roots` | 无 | `[{path, label, is_primary, exists, namespace, prefix}]` | 全部根目录，供 folder-picker 与附属插件遍历 |
| `browse_dir` | `path` | 共享基建 `list_subdirectories` 结果，条目带 `is_image_dir` | 设置页浏览本机目录 |
| `clear_folder_settings` | `rel_path` | `{success}` | 清除文件夹级设置，回退全局 |
| `diagnose` | `method` | 缓存与走盘计数（`album_dirs_walk_count`、`list_albums_stages_ms`、`last_build_scans`、`build_breakdown`） | 诊断钩子 |

以下成员由壳按名调用，不经 `register_api`。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `get_file_roots` | 无 | `[Path]` | `/file` 允许访问的根列表 |
| `resolve_file_path` | `rel_path` | 物理 `Path` / `None` | 带 `NAMESPACE_MARKER` 前缀的虚拟路径 → 物理路径 |
| `get_data_root` | 无 | `Path` | 数据根，供元数据与缓存路径派生 |
| `get_thumb_data` | `rel_path` | `(bytes, mime)` / `None` | `/thumbs` 响应的缩略图字节（取自 `ThumbCache.get`） |
| `ensure_thumb` | `rel_path` | 无（与基类契约一致；现场生成散文件缩略图） | 旧版文件式缩略图入口，仅供兼容代码 |
| `get_cache_dir` | 无 | `Path`（返回 `cache_dir`） | 统一刷新指纹库的落点 |
| `freshness_spec` | 无 | 新鲜度规格 dict | 声明根、前缀与钩子 |
| `on_settings_changed` | `changed_keys` | 无 | `root_dir` / `extra_roots` / 排序类键变更后重建缓存路径 |
| `on_unload` | 无 | 无 | 进程退出时取消重建任务 |

## 前端结构

- `#app`
  - `.view-body`（旧 embed 回落的内嵌视图定位基准）
    - `.view-content.iv-content.obx-scroll#iv-content`
      - `#iv-albums`（相册网格、时间线与时间线顶部的展示切换控件的容器）
      - `#image-grid.iv-image-grid`（Justified 绝对定位卡片容器）
    - `.pagination-bar#pagination`（`createPagination` 的容器）
    - `.extension-view#extension-view.hidden`（仅给不认 native 的旧壳，当前壳不进入这里）
      - `.extension-view-header`
        - `.extension-view-heading`：`.iv-view-title#extension-view-title`、`.iv-view-sub#extension-view-sub`
        - `.extension-view-actions#extension-view-actions`（`HostChannel` 的 `host-toolbar` 容器）
      - `.extension-view-body`：`#extension-frame`
- `.modal#new-album-modal`：`.modal-box.obx-anim-scale`、`.modal-body`、`.iv-field`、`#iv-new-album-name`、`.modal-footer`、`#iv-new-album-cancel`、`#iv-new-album-confirm`
- `.modal#move-modal`：`.tree-container.move-tree#move-tree`（`createTree` 的容器）、`#move-cancel`、`#move-confirm`
- `.modal#settings-modal`：`.iv-settings-body.obx-scroll`、`.iv-setting-section`、`.iv-setting-section-first`、`.iv-setting-item`、`.iv-setting-note`、`.iv-setting-note-after-input`、`.iv-check`、`#setting-follow-shell`、`#setting-plugin-alpha`、`#setting-plugin-alpha-val`、`#setting-shell-alpha`、`#setting-roots`（`FolderPicker.createList` 的容器）、`#setting-row-height`、`#setting-row-height-val`、`#setting-per-page`、`#setting-lightbox-cache`、`#setting-lightbox-cache-val`、`#setting-sort-by`、`#setting-sort-order`、`#setting-pixiv-fuzzy-section`、`#setting-pixiv-fuzzy`、`#setting-album-sort-section`、`#setting-album-sort-by`、`#setting-album-sort-order`、`#setting-apply-to-folder`、`#setting-current-folder-name`、`#settings-cancel`、`#settings-save`
- `.rebuild-progress-card#rebuild-progress.hidden`：`.rebuild-progress-header`、`#rebuild-progress-hide`、`.rebuild-progress-body`、`#rebuild-progress-text`、`.rebuild-progress`、`.rebuild-progress-bar#rebuild-progress-bar`、`#rebuild-progress-current`、`#rebuild-progress-speed`、`#rebuild-errors`、`#rebuild-progress-cancel`

壳渲染的 chrome（`manifest.json` 的 `frontend.chrome`，由 `PluginChrome.vue` 落 DOM）：

- 品牌块：`icon:images`、标题「图片相册」、副标题「Gallery」。
- 浏览组（`browse`）：`albums` 全部相册、`timeline` 时间线、`latest` 最近添加。
- 顶栏动作：`back`（返回，初始不可见）、`new-album`（新建相册）、`slideshow`（图片层可见，文案在「幻灯片 / 停止」间切换）、`multi-select`（文案在「开启多选 / 退出多选」间切换）、`selection-count`（`kind: 'text'`，多选且已选时显示「已选 N 项」）、`delete-selected`（`variant: 'danger'`）、`move-selected`、`refresh-thumbs`（三者仅多选态可见，未选中时 `disabled`）、`settings`。
- 搜索框：占位在相册层为「搜索相册…」、图片层为「搜索当前相册…」、时间线「按图片」层为「搜索当前页图片…」；输入经 `omnibox:query` 下发。
- 状态栏：相册层 `N 个相册 · M 张图片`，图片层 `共 N 张图片` 或 `共 N 项 · M 张图片`；时间线「按图片」层按单元数给 `共 N 项 · M 张图片`（全是单图时 `共 N 张图片`），`list_recent_images` 返回 `partial` 时状态点用 `warn`、文案注明「仅列出最近 N 项」。分页在 `has_next` 为真时一次给出后 `IV_RECENT_PAGE_LOOKAHEAD` 页，翻过可达边界时回落到真实最后一页（`reach`），空态分「没有更多图片」与「已到扫描上限」两种。
- 扩展入口（`extensions`）：`PluginExtensions` 读取 image-cleaner / pixiv-sync 的 `get_extensions` 渲染；点击由壳打开 `NativePanel`。
- 统一刷新控件：`freshness.unit` 为「张」，由壳挂载（挂载后与 `activeView` / `freshness_scope` 变化时由 `PluginChrome` 调 `autoSync`）；结论回来时本页只处理兼容的旧动作 `omnibox:action: refresh`（`omnibox:freshness` 走 `onMessage`，本页只在那里解释 `omnibox:appearance`），所以只重载一次、不需要按 `payload` 去重。

脚本装载顺序（`index.html` 末尾，`ImageViewer.prototype` 的分片顺序是硬约束，由 `tests/js/image_viewer_app_split.mjs` 把关）：`js/justified-layout.js` → `js/app.js` → `js/app-albums.js` → `js/app-nav.js` → `js/app-grid.js` → `js/app-refresh.js` → `js/app-settings.js` → `js/app-utils.js`，最后由 `index.html` 实例化 `ImageViewer` 并调用 `init`。

各分片脚本渲染出的节点如下。

- 相册卡片：`.iv-grid`、`.iv-album`、`.iv-album-cover`、`.iv-album-badge`、`.iv-album-info`、`.iv-album-name`、`.iv-album-count`、`.iv-album-menu`、`.iv-album-tag`、`.iv-album-tag-hot`、`.iv-time-badge`、`.iv-cover-fallback`
- 时间线：`.iv-timeline-section`、`.iv-time-label`
- 时间线展示切换：`.iv-mode-switch`、`.iv-mode-btn`、`.iv-mode-btn.active`（`#iv-albums` 内首位，图片层里它是 `#iv-albums` 的唯一内容）
- 图片瓦片：`.iv-image-card`、`.iv-image-card.selected`、`.iv-cover-img`、`.iv-count-badge`（子文件夹瓦片右下角的页数 / 递归张数，数量已知即显示，与排序模式无关）、`.iv-tile-pending`
- 相册右键菜单：`.iv-context-menu`
- 空态与加载：`.empty-state`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`、`.loading`

## 与壳的契约

壳层行为与组件的完整说明见 `docs/plugin-ui-guide.md`；本插件实际用到的部分如下。

- 页面 chrome 消息（只在壳与内容 iframe 之间，同源 `postMessage`）：
  - 壳 → 内容 `omnibox:view`：左栏导航项 id（`albums` / `timeline` / `latest`）；内容页切视图并重绘相册网格。
  - 壳 → 内容 `omnibox:action`：`back` 返回上一级、`new-album` 打开新建相册弹窗、`slideshow` 起停幻灯片、`multi-select` 切换多选、`delete-selected` / `delete` 删除选中、`move-selected` / `move` 打开移动弹窗、`refresh-thumbs` 定向重建选中缩略图、`settings` 打开内容页设置弹窗、`refresh` 在统一刷新（同步/校验）完成后重载相册树与当前目录。
  - 壳 → 内容 `omnibox:query`：搜索输入（壳侧 250ms 防抖）；相册层过滤相册，图片层过滤当前页。
  - 内容 → 壳 `omnibox:ready`：握手。壳收到后补发当前视图；内容页在 `init` 末尾主动发一次。
  - 内容 → 壳 `omnibox:chrome`：`title` / `subtitle` / `view` / `freshness_scope` / `actions` / `nav` / `search` / `status`。`freshness_scope` 是统一刷新的作用域（图片层为当前目录，相册层与时间线「按图片」层为空串），壳据此调 `autoSync(scope)`；`actions` 控制 `back`、`slideshow`（`active` 与「幻灯片 / 停止」文案）、`multi-select`（`active` 与「开启多选 / 退出多选」文案）、`selection-count`（`kind: 'text'` 的「已选 N 项」）、`delete-selected` / `move-selected` / `refresh-thumbs`（`visible` 与 `disabled`）；`view` 在旧 embed 回落时发空串清掉导航高亮；`search` 改占位符；`status` 为相册数与图片数。
- 统一刷新：`freshness` 由壳的 `PluginChrome` 挂载（`shell/frontend/public/shell/freshness.js` 的 `Freshness.mount`，`plugin` 为 `image-viewer`、`unit` 为「张」），壳在挂载后与 `watch([activeView, freshnessScope])` 里调 `autoSync(scope)`；内容页在 `plugin-shown` 时用 `Freshness.sync('image-viewer', scope)` 同步一次，报告里有变化才重载。结论回来时本页只处理兼容的旧动作 `omnibox:action: refresh`（`onMessage` 只解释 `omnibox:appearance`），所以一次同步只重载一次；`_syncFreshness` 的变化判据包含 `new_dirs` / `vanished_dirs`（新建空相册在条目层没有任何增删），`skip` 不重载。`freshness_spec` 的根用 `paths.freshness_roots()`（配置里的全部根，含暂时不可达者）：拔掉外接盘时引擎只记 `unreachable_roots`，不把该根的目录行、缩略图与「保留可见」标记当成目录被删；`/file` 允许根、`is_safe` 与走盘仍只用 `paths.roots()`（存在的根）。图片层刷新只重取当前目录并保留层级：`loadAlbums` 在图片层只更新相册数据，不调用 `showAlbums`（否则 `mode` 被强制切回相册列表，随后的 `loadImages` 不会执行）。缩略图版本号用毫秒（`_thumbUrl` 的 `Math.round(秒 * 1000)`），封面优先 `cover_mtime`；强制重建（「更新缩略图」/「重建」）不改源文件 mtime，由后端 `version`（`ThumbStore.bump_rebuild_version` 的单调毫秒）拼进 URL，否则 `/thumbs` 的一天强缓存会让重建看不到效果。
- 扩展入口：`manifest.json` 的 `frontend.chrome.extensions` 让壳的 `PluginExtensions` 读取 image-cleaner / pixiv-sync 的 `get_extensions` 并渲染入口；`sidebarOrder` 把 `extensions` 放在 `nav:browse` 之后，两个附属入口默认位于侧栏底部。native 入口由壳的 `NativePanel` 在 `PluginChrome` 内容槽打开（侧栏/顶栏保留，内容 iframe 用 `v-show` 隐藏），入口选中态与主导航由 `PluginChrome.activeExtensionId` 统一维护。宿主页不再渲染 `#iv-extensions`，也不调用 `renderExtensions`。
- 设置：本插件是壳内设置弹窗的例外。`openSettingsModal` 由内容页渲染，因为设置按目录作用域读写并带根目录列表（`FolderPicker.createList`），单份 `settings_schema` 表达不了；壳对 `image-viewer` 不拦截 `settings` 动作，照常转发给内容页。目录列表与壳内设置弹窗是同一套壳内 `FolderPicker`；本页未传 `localOnly`，因此也提供「网络位置」入口（来源由安装了 network-location 能力的插件提供）。设置页顶部有通用外观块：`#setting-follow-shell` 与 `#setting-plugin-alpha` 通过 `omnibox:get-plugin-appearance` / `omnibox:set-plugin-alpha` 读写壳偏好的 `--plugin-glass-image-viewer`，默认跟随壳的 `--glass-alpha`。保存经 `_saveStep` 逐次检查 `save_settings` 返回值：后端返回 `success:false`（number/range 越界或空值等）时整次不落盘，内容页不更新内存、不关弹窗、不刷新，并把字段级 `errors` 显示在 `#settings-errors`；成功后才应用内存值并 `refresh` + 重载相册。
- 搜索：壳持有输入框；内容页在 `omnibox:chrome` 上报 `search.enabled`、`search.placeholder` 与 `search.clear`（递增序号，进相册层/二级页时由 `app-albums.js` 递增），壳据此清空输入框。本插件不使用 `search.value` 回填。
- 壳类：`.view-body`、`.view-content`、`.obx-scroll`、`.search-input`、`.btn`、`.btn-sm`、`.btn-primary`、`.btn-danger`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`、`.obx-anim-scale`、`.hidden`、`.empty-state`、`.loading`、`.pagination-bar`、`.tree-container`、`.obx-icon`、`.obx-toolbar-btn`、`.obx-host-action`。
- 壳 CSS 变量：`--toolbar-height`、`--accent`、`--bg-app`、`--bg-surface`、`--bg-hover`、`--border`、`--text-primary`、`--text-secondary`、`--text-muted`、`--text-on-accent`、`--danger`、`--shadow-sm`、`--shadow-md`、`--transition-fast`、`--obx-shadow-2`。
- 插件自有 CSS 变量（定义在 `frontend/image-viewer.css` 的 `:root`）：`--iv-radius`、`--iv-radius-sm`、`--iv-gradient`、`--iv-accent-soft`。
- 共享前端组件：`createLightbox`（`show` / `hide` / `navigate` / `setItems` / `setDecodedLimit` / `getIndex`）、`createPagination`（`render`）、`createContextMenu`（`show`）、`createTree`、`Freshness.assetUrl`、`Freshness.sync`（不经控件句柄的一次同步，重新可见时用）、`FolderPicker.createList`（`getPaths` / `setPaths`；壳内设置弹窗的 `directory` 字段也用同一组件，`localOnly` 为真时隐藏「网络位置」）、`HostChannel.serve` / `HostChannel.ownFrame`（仅旧壳，当前壳不进入）、`PluginLifecycle.onShow` / `onHide` / `onDispose`、`Bridge.call` / `Bridge.thumbUrl`、`Icons.html`、`Utils.debounce` / `Utils.escapeHtml`、`Toast.success` / `Toast.error` / `Toast.info` / `Toast.warning`、`confirmDialog`。
- 内容页不得再出现 chrome 标记：`.view-sub-sidebar`、`.view-toolbar`、`.sub-sidebar-brand`、`.obx-nav-item`（`tools/plugin_ui_audit.py` 的 `iframe.content-only` 会检查）。
- 共享后端基建：`shell/backend/plugin_base.py` 的 `PluginBase`、`setting`、`save_settings`、`update_setting`、`is_protected_path`；`shell/backend/thumb_cache.py` 的 `ThumbCache.get` / `delete` / `clear` / `prune` / `generate_bulk`；`shell/backend/freshness.py` 的 `engine_for`、`reset_throttle`、`verified_keys` 与 `freshness_spec` 字段名（`roots`、`prefixes`、`include`、`derive`、`prune`、`on_dirs_changed`、`on_verified`、`on_pass_end`、`rebuild`、`content_version`、`stat_entries`、`unit`、`min_sync_interval`、`max_dirs_per_sync`）；`shell/backend/tasks.py` 的 `BackgroundTask`；`shell/backend/media_catalog.py` 的 `list_subdirectories`、`is_safe_path`、`list_directory`、`scan_directory`、`natural_sort_key`；`shell/backend/json_cache.py` 的 `read_json` / `write_json` / `read_cached_json` / `write_cached_json`；`shell/backend/ports.py` 的 `SettingsPort` / `PersistPort`；`shell/backend/plugin_utils.py` 的 `load_sibling`。
- 附属插件写入本插件图片根之后，经 `get_dependency('image-viewer')` 调 `invalidate_derived(reason, keep_snapshot=False, dirs=None)`（编排在 `FreshnessProvider.invalidate_derived`）：它丢相册索引、列表缓存与漫画根解析，返回 `{'reason': reason}`，幂等、不重算。只改文件的写入方（删除 / 移动图片）传 `keep_snapshot=True` 与动过的 `dirs`，按目录精确丢条目、保住目录快照；可能增删目录的写入方保持默认 `False`，让目录快照与命名空间映射一起重来。调用方用 `getattr` 兼容旧宿主。
