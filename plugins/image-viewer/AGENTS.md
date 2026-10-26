> image-viewer ｜ manifest: plugins/image-viewer/manifest.json

## 定位与形态

- 职责：浏览本地图片目录，把文件夹聚合为相册树，并以混合瀑布流与灯箱查看图片。
- 形态：独立页插件（`frontend.route` = `/image-viewer`）；页面 chrome 由 manifest 的 `frontend.chrome` 声明，壳的 `PluginChrome` 渲染品牌块、左栏导航、扩展入口、顶栏动作、搜索框、状态栏与侧栏分隔条，壳加载内容页时追加 `?content=1`，iframe 内只有内容（相册卡片、justified 图片网格、分页、灯箱、弹窗、缩略图重建进度卡）。
- 宿主形态：companion 插件（image-cleaner / pixiv-sync，二者在各自 `manifest.json` 里声明 `dependencies: ["image-viewer"]`）的入口由壳的 `PluginExtensions` 渲染，点击后在宿主页内容槽打开 `NativePanel`（侧栏/顶栏保留，内容 iframe 用 `v-show` 隐藏、关闭后原位恢复）；`#extension-frame` 与 `openExtensionView` 仅给不认 native 的旧壳，当前壳不进入这里。
- `manifest.json` 关键字段：`name` = `image-viewer`；`backend.entry` = `backend/main.py`；`backend.class` = `ImageViewerPlugin`；`frontend.entry` = `frontend/index.html`；`frontend.route` = `/image-viewer`；`frontend.chrome` 含 `brand`、`nav`（`browse` 组）、`toolbar`、`search`、`status`、`freshness`、`extensions`；`keepAlive` = `true`；`permissions` = `filesystem:read`、`filesystem:write`；`dependencies` 为空列表（无依赖）；`icon` = `icon:images`。

## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/image-viewer/manifest.json` | 插件声明、入口指向与壳渲染的页面 chrome | `ImageViewerPlugin`、`backend.entry`、`frontend.route`、`frontend.chrome` |
| `plugins/image-viewer/backend/main.py` | 类骨架、设置 schema、常量、API 注册、诊断 | `ImageViewerPlugin`、`register_api`、`settings_schema`、`diagnose`、`get_root_dir`、`list_roots`、`browse_dir`、`_pixiv_mode` |
| `plugins/image-viewer/backend/common.py` | 无状态共享常量与纯函数 | `NAMESPACE_MARKER`、`pixiv_sort`、`is_pixiv_work_dir`、`pick_cover`、`cover_rank`、`same_path` |
| `plugins/image-viewer/backend/filesystem.py` | 路径安全、自然序、尺寸元数据等纯工具 | `ALLOWED_EXTENSIONS`、`natural_sort_key`、`pixiv_number`、`pixiv_page_index`、`get_image_size`、`drop_image_meta`、`ensure_thumbnail`、`stat_mtime` |
| `plugins/image-viewer/backend/namespace.py` | 多根目录与虚拟命名空间路径 | `NamespaceMixin`、`_split_virtual`、`_virtual_path`、`_namespace_map`、`_roots`、`get_file_roots`、`resolve_file_path`、`_is_namespace_node`、`_is_safe` |
| `plugins/image-viewer/backend/thumbs.py` | 元数据缓存、缩略图字节、尺寸填充 | `ThumbMixin`、`get_data_root`、`get_thumb_data`、`ensure_thumb`、`get_image_info`、`_fill_image_sizes`、`_cache_list`、`_limit_all_images`、`_get_thumb` |
| `plugins/image-viewer/backend/listing.py` | 目录 / 图片列表与子目录聚合扫描 | `ListingMixin`、`list_images`、`list_folder_items`、`list_album_images`、`load_album_tiles`、`list_dir`、`_scan_one_subdir`、`_children_total`、`_all_images_offset` |
| `plugins/image-viewer/backend/albums.py` | 相册树、封面挑选、目录快照与索引缓存 | `AlbumMixin`、`list_albums`、`_build_albums`、`_list_album_dirs`、`_dirs_snapshot`、`_save_dirs_index`、`_load_dirs_index`、`_walk_album_dirs`、`_invalidate_albums_cache`、`get_album_config`、`set_album_config`、`prewarm_album_dirs` |
| `plugins/image-viewer/backend/file_ops.py` | 目录 / 文件增删改与兼容刷新入口 | `FileOpsMixin`、`create_folder`、`delete_folder`、`delete_files`、`move_files`、`regenerate_thumbs`、`refresh`、`_mark_visible`、`_prune_visible_marks` |
| `plugins/image-viewer/backend/rebuild.py` | 缩略图全量 / 定向重建后台任务 | `RebuildMixin`、`rebuild_all`、`rebuild_folder`、`rebuild_status`、`rebuild_cancel`、`_collect_all_images`、`_rebuild_worker`、`on_unload` |
| `plugins/image-viewer/backend/freshness.py` | 接入壳的统一刷新基建 | `FreshnessMixin`、`get_cache_dir`、`freshness_spec`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild`、`_drop_album_nodes` |
| `plugins/image-viewer/backend/settings.py` | 按目录的设置读写与继承 | `SettingsMixin`、`get_settings`、`save_folder_settings`、`clear_folder_settings`、`update_setting`、`on_settings_changed`、`compute_pixiv_flags`、`_compute_settings`、`_settings_source` |
| `plugins/image-viewer/frontend/index.html` | 内容页入口：内容容器、旧 embed 回落、三个弹窗、保存失败错误区、进度卡与脚本装载顺序（页面 chrome 不在这里） | `#app`、`.view-body`、`#iv-content`、`#iv-albums`、`#image-grid`、`#pagination`、`#extension-view`、`#settings-modal`、`#settings-errors`、`#setting-follow-shell`、`#setting-plugin-alpha`、`js/app.js` |
| `plugins/image-viewer/frontend/image-viewer.css` | 插件样式与自有 CSS 变量 | `--iv-radius`、`--iv-gradient`、`.iv-album`、`.iv-image-card`、`.iv-context-menu`、`.extension-view` |
| `plugins/image-viewer/frontend/js/app.js` | `ImageViewer` 类骨架、初始化、生命周期、与壳的 chrome 消息、UI 事件绑定、旧 embed 回落 | `ImageViewer`、`init`、`_bindUI`、`_bindPluginLifecycle`、`_postHost`、`_pushChrome`、`_bindHost`、`_applyHostView`、`_applyHostAction`、`_onQuery`、`_serveHostChannel`、`loadSettings`、`openExtensionView`、`closeExtensionView`、`_applyAlbumSortSettings`、`_embedUrl` |
| `plugins/image-viewer/frontend/js/app-albums.js` | 相册树浏览、渲染与作者网格排序 | `loadAlbums`、`showAlbums`、`_renderAlbumCards`、`_renderTimeline`、`_filterVisibleAlbums`、`_isCollapsed`、`_sortAlbums`、`_gridIsPixiv`、`_gridConfigPath`、`_ensureGrid`、`openAlbum`、`_showFolder`、`_rememberScroll`、`_restoreScroll`、`_popNavStack` |
| `plugins/image-viewer/frontend/js/app-nav.js` | 返回栈、相册右键菜单、状态栏统计（相册数 / 图片数）、新建相册、根目录列表 | `_handleBack`、`showAlbumMenu`、`_closeAlbumMenu`、`_updateStats`、`_resetRootsList`、`_rootPaths`、`openNewAlbumModal`、`createAlbum` |
| `plugins/image-viewer/frontend/js/app-grid.js` | 图片网格、Justified 布局、瓦片二级加载、幻灯片、多选 / 移动 / 删除、选中计数上报 | `loadImages`、`renderJustifiedLayout`、`_showFromCard`、`_upgradeSequence`、`_resetAlbumTiles`、`_watchAlbumTile`、`_flushAlbumTiles`、`_loadAlbumTileBatch`、`_applyAlbumTile`、`filterCurrentImages`、`toggleSlideshow`、`_stopSlideshow`、`toggleMultiSelectMode`、`_updateSelectionCount`、`toggleSelectImage`、`clearSelection`、`handleContextAction`、`deleteSelectedImages`、`openMoveModal`、`confirmMove` |
| `plugins/image-viewer/frontend/js/app-refresh.js` | 缩略图重建进度卡与统一刷新完成后的重载（「校验」控件由壳挂载） | `_onFreshnessChanged`、`refreshView`、`rebuildAll`、`rebuildFolder`、`_startRebuildTask`、`_waitRebuildDone`、`_updateRebuildProgress`、`hideRebuildProgress`、`cancelRebuild`、`refreshSelectedThumbs` |
| `plugins/image-viewer/frontend/js/app-settings.js` | 设置弹窗读写、通用外观块与灯箱缓存下发 | `openSettingsModal`、`closeSettingsModal`、`saveSettings`、`_saveStep`、`_showSettingsErrors`、`_clearSettingsErrors`、`_applyLightboxCache`、`_syncAppearanceControls`、`_bindAppearanceSettings` |
| `plugins/image-viewer/frontend/js/app-utils.js` | 缩略图 URL、转义、空态、时间格式化 | `_thumbUrl`、`_escapeHtml`、`_escapeAttr`、`_emptyHtml`、`_timeAgo`、`_monthKey`、`_formatDuration` |
| `plugins/image-viewer/frontend/js/justified-layout.js` | Justified 行布局计算 | `JustifiedLayout`、`JustifiedLayout.compute` |

落盘缓存产物位于 `cache_dir`，其路径由 `_rebuild_paths` 派生：`image_meta.json`（`_load_meta` / `_save_meta`）、`albums_index.json`（`_load_album_cache` / `_save_album_cache`）、`dirs_index.json`（`_load_dirs_index` / `_save_dirs_index`）、`albums_config.json`（`_load_album_config` / `_save_album_config`）、`thumbs.db`（`ThumbCache`）、`freshness.db`（壳的 `FreshnessStore`）。

## 后端 API 契约

`register_api` 暴露的方法由前端经 `Bridge.call` 调用，壳把它映射到以插件名与方法名拼接的 HTTP 名。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `list_images` | `rel_path`、`page`、`per_page`、`sort_by`、`sort_order` | `{images, page, total, has_next, has_prev, settings}` | 单目录图片分页列表 |
| `list_folder_items` | `rel_path`、`page`、`per_page`、`sort_by`、`sort_order` | `{items, all_images, all_truncated, all_capped, all_offset, page, total, image_total, pending_count, sequence_pending, has_next, has_prev, settings}` | 混合列表：直接图片 + 子相册 p0 瓦片 |
| `load_album_tiles` | `paths` | `{tiles: {path: {cover, total_count, image_count, width, height, use_time_name, mtime}}}` | 按批补齐进入视口的瓦片封面与递归计数 |
| `list_album_images` | `rel_path` | `{images, offset, capped}` | 灯箱用的连续浏览序列 |
| `list_dir` | `rel_path` | `[{name, path}]` | 目录树（移动目标选择） |
| `list_albums` | 无 | `{albums, config, changed}` | 相册全树展开 |
| `create_folder` | `rel_path` | `{success, path}` / `{success, error}` | 新建相册目录 |
| `delete_folder` | `rel_path` | `{success}` / `{success, error}` | 删除递归无图片的空目录 |
| `get_album_config` | 无 | `{collapsed, promoted, expanded, visible_empty_dirs}` | 读取相册可见性配置 |
| `set_album_config` | `rel_path`、`action`（`collapse` / `expand` / `promote` / `unpromote`） | `{success, config}` | 收纳 / 展开 / 提升相册 |
| `get_image_info` | `rel_path` | `{success, rel_path, size, width, height}` / `{success, error}` | 灯箱信息面板的尺寸与体积 |
| `delete_files` | `rel_paths` | `{deleted, errors}` | 删除图片 |
| `move_files` | `rel_paths`、`dest_rel` | `{moved, errors}` | 移动图片到目标目录 |
| `regenerate_thumbs` | `rel_paths` | `{regenerated, errors}` | 定向重建选中图片的缩略图 |
| `refresh` | 无 | `{success, ...report}` | 兼容入口，转发到壳的统一刷新同步 |
| `rebuild_all` | `rel_path`、`force` | `{started, running, total}` | 全量 / 指定文件夹缩略图重建 |
| `rebuild_folder` | `rel_path` | `{started, running, total}` | 指定文件夹增量补齐 |
| `rebuild_status` | 无 | `{running, done, success, cancelled, rebuild_path, total, processed, current, error_count, errors}` | 重建任务进度 |
| `rebuild_cancel` | 无 | `{success}` / `{success, error}` | 取消重建任务 |
| `get_settings` | `rel_path` | `row_height`、`per_page`、`sort_by`、`sort_order`、`pixiv_fuzzy`、`album_sort_by`、`album_sort_order`、`root_dir`、`pixiv_explicit` | 按目录读取生效设置（含逐级继承） |
| `save_settings`（实现为 `save_folder_settings`） | `rel_path`、`settings` | `{success}` | 保存全局设置或文件夹级设置 |
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
| `ensure_thumb` | `rel_path` | 缩略图文件路径；不可用时返回空串 | 旧版文件式缩略图入口 |
| `get_cache_dir` | 无 | `Path`（返回 `cache_dir`） | 统一刷新指纹库的落点 |
| `freshness_spec` | 无 | 新鲜度规格 dict | 声明根、前缀与钩子 |
| `on_settings_changed` | `changed_keys` | 无 | `root_dir` / `extra_roots` / 排序类键变更后重建缓存路径 |
| `on_unload` | 无 | 无 | 进程退出时取消重建任务 |

## 前端结构

- `#app`
  - `.view-body`（旧 embed 回落的内嵌视图定位基准）
    - `.view-content.iv-content.obx-scroll#iv-content`
      - `#iv-albums`（相册网格与时间线的容器）
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
- 搜索框：占位在相册层为「搜索相册…」、图片层为「搜索当前相册…」；输入经 `omnibox:query` 下发。
- 状态栏：相册层 `N 个相册 · M 张图片`，图片层 `共 N 张图片` 或 `共 N 项 · M 张图片`。
- 扩展入口（`extensions`）：`PluginExtensions` 读取 image-cleaner / pixiv-sync 的 `get_extensions` 渲染；点击由壳打开 `NativePanel`。
- 统一刷新控件：`freshness.unit` 为「张」，由壳挂载；校验完成的 `omnibox:action: refresh` 回到内容页重载。

脚本装载顺序（`index.html` 末尾，`ImageViewer.prototype` 的分片顺序是硬约束，由 `tests/js/image_viewer_app_split.mjs` 把关）：`js/justified-layout.js` → `js/app.js` → `js/app-albums.js` → `js/app-nav.js` → `js/app-grid.js` → `js/app-refresh.js` → `js/app-settings.js` → `js/app-utils.js`，最后由 `index.html` 实例化 `ImageViewer` 并调用 `init`。

各分片脚本渲染出的节点如下。

- 相册卡片：`.iv-grid`、`.iv-album`、`.iv-album-cover`、`.iv-album-badge`、`.iv-album-info`、`.iv-album-name`、`.iv-album-count`、`.iv-album-menu`、`.iv-album-tag`、`.iv-album-tag-hot`、`.iv-time-badge`、`.iv-cover-fallback`
- 时间线：`.iv-timeline-section`、`.iv-time-label`
- 图片瓦片：`.iv-image-card`、`.iv-image-card.selected`、`.iv-cover-img`、`.iv-count-badge`、`.iv-tile-pending`
- 相册右键菜单：`.iv-context-menu`
- 空态与加载：`.empty-state`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`、`.loading`

## 与壳的契约

壳层行为与组件的完整说明见 `docs/plugin-ui-guide.md`；本插件实际用到的部分如下。

- 页面 chrome 消息（只在壳与内容 iframe 之间，同源 `postMessage`）：
  - 壳 → 内容 `omnibox:view`：左栏导航项 id（`albums` / `timeline` / `latest`）；内容页切视图并重绘相册网格。
  - 壳 → 内容 `omnibox:action`：`back` 返回上一级、`new-album` 打开新建相册弹窗、`slideshow` 起停幻灯片、`multi-select` 切换多选、`delete-selected` / `delete` 删除选中、`move-selected` / `move` 打开移动弹窗、`refresh-thumbs` 定向重建选中缩略图、`settings` 打开内容页设置弹窗、`refresh` 在统一刷新（同步/校验）完成后重载相册树与当前目录。
  - 壳 → 内容 `omnibox:query`：搜索输入（壳侧 250ms 防抖）；相册层过滤相册，图片层过滤当前页。
  - 内容 → 壳 `omnibox:ready`：握手。壳收到后补发当前视图；内容页在 `init` 末尾主动发一次。
  - 内容 → 壳 `omnibox:chrome`：`title` / `subtitle` / `view` / `actions` / `nav` / `search` / `status`。`actions` 控制 `back`、`slideshow`（`active` 与「幻灯片 / 停止」文案）、`multi-select`（`active` 与「开启多选 / 退出多选」文案）、`selection-count`（`kind: 'text'` 的「已选 N 项」）、`delete-selected` / `move-selected` / `refresh-thumbs`（`visible` 与 `disabled`）；`view` 在旧 embed 回落时发空串清掉导航高亮；`search` 改占位符；`status` 为相册数与图片数。
- 统一刷新：`freshness` 由壳的 `PluginChrome` 挂载（`shell/frontend/public/shell/freshness.js` 的 `Freshness.mount`，`plugin` 为 `image-viewer`、`unit` 为「张」），内容页不再挂 `#iv-freshness`；校验完成后的重载经 `omnibox:action: refresh` 回来。图片层刷新只重取当前目录并保留层级：`loadAlbums` 在图片层只更新相册数据，不调用 `showAlbums`（否则 `mode` 被强制切回相册列表，随后的 `loadImages` 不会执行）。
- 扩展入口：`manifest.json` 的 `frontend.chrome.extensions` 让壳的 `PluginExtensions` 读取 image-cleaner / pixiv-sync 的 `get_extensions` 并渲染入口；`sidebarOrder` 把 `extensions` 放在 `nav:browse` 之后，两个附属入口默认位于侧栏底部。native 入口由壳的 `NativePanel` 在 `PluginChrome` 内容槽打开（侧栏/顶栏保留，内容 iframe 用 `v-show` 隐藏），入口选中态与主导航由 `PluginChrome.activeExtensionId` 统一维护。宿主页不再渲染 `#iv-extensions`，也不调用 `renderExtensions`。
- 设置：本插件是壳内设置弹窗的例外。`openSettingsModal` 由内容页渲染，因为设置按目录作用域读写并带根目录列表（`FolderPicker.createList`），单份 `settings_schema` 表达不了；壳对 `image-viewer` 不拦截 `settings` 动作，照常转发给内容页。目录列表与壳内设置弹窗是同一套壳内 `FolderPicker`；本页未传 `localOnly`，因此也提供「网络位置」入口（来源由安装了 network-location 能力的插件提供）。设置页顶部有通用外观块：`#setting-follow-shell` 与 `#setting-plugin-alpha` 通过 `omnibox:get-plugin-appearance` / `omnibox:set-plugin-alpha` 读写壳偏好的 `--plugin-glass-image-viewer`，默认跟随壳的 `--glass-alpha`。保存经 `_saveStep` 逐次检查 `save_settings` 返回值：后端返回 `success:false`（number/range 越界或空值等）时整次不落盘，内容页不更新内存、不关弹窗、不刷新，并把字段级 `errors` 显示在 `#settings-errors`；成功后才应用内存值并 `refresh` + 重载相册。
- 搜索：壳持有输入框；内容页在 `omnibox:chrome` 上报 `search.enabled`、`search.placeholder` 与 `search.clear`（递增序号，进相册层/二级页时由 `app-albums.js` 递增），壳据此清空输入框。本插件不使用 `search.value` 回填。
- 壳类：`.view-body`、`.view-content`、`.obx-scroll`、`.search-input`、`.btn`、`.btn-sm`、`.btn-primary`、`.btn-danger`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`、`.obx-anim-scale`、`.hidden`、`.empty-state`、`.loading`、`.pagination-bar`、`.tree-container`、`.obx-icon`、`.obx-toolbar-btn`、`.obx-host-action`。
- 壳 CSS 变量：`--toolbar-height`、`--accent`、`--bg-app`、`--bg-surface`、`--bg-hover`、`--border`、`--text-primary`、`--text-secondary`、`--text-muted`、`--text-on-accent`、`--danger`、`--shadow-sm`、`--shadow-md`、`--transition-fast`、`--obx-shadow-2`。
- 插件自有 CSS 变量（定义在 `frontend/image-viewer.css` 的 `:root`）：`--iv-radius`、`--iv-radius-sm`、`--iv-gradient`、`--iv-accent-soft`。
- 共享前端组件：`createLightbox`（`show` / `hide` / `navigate` / `setItems` / `setDecodedLimit` / `getIndex`）、`createPagination`（`render`）、`createContextMenu`（`show`）、`createTree`、`Freshness.assetUrl`、`FolderPicker.createList`（`getPaths` / `setPaths`；壳内设置弹窗的 `directory` 字段也用同一组件，`localOnly` 为真时隐藏「网络位置」）、`HostChannel.serve` / `HostChannel.ownFrame`（仅旧壳，当前壳不进入）、`PluginLifecycle.onShow` / `onHide` / `onDispose`、`Bridge.call` / `Bridge.thumbUrl`、`Icons.html`、`Utils.debounce` / `Utils.escapeHtml`、`Toast.success` / `Toast.error` / `Toast.info` / `Toast.warning`、`confirmDialog`。
- 内容页不得再出现 chrome 标记：`.view-sub-sidebar`、`.view-toolbar`、`.sub-sidebar-brand`、`.obx-nav-item`（`tools/plugin_ui_audit.py` 的 `iframe.content-only` 会检查）。
- 共享后端基建：`shell/backend/plugin_base.py` 的 `PluginBase`、`setting`、`save_settings`、`update_setting`、`is_protected_path`；`shell/backend/thumb_cache.py` 的 `ThumbCache.get` / `delete` / `clear` / `prune` / `generate_bulk`；`shell/backend/freshness.py` 的 `engine_for`、`reset_throttle` 与 `freshness_spec` 字段名（`roots`、`prefixes`、`include`、`derive`、`prune`、`on_verified`、`on_pass_end`、`rebuild`、`content_version`、`stat_entries`、`unit`、`min_sync_interval`、`max_dirs_per_sync`）；`shell/backend/tasks.py` 的 `BackgroundTask`；`shell/backend/media_catalog.py` 的 `list_subdirectories`、`is_safe_path`、`list_directory`；`shell/backend/plugin_utils.py` 的 `load_sibling`。
