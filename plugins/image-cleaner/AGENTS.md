> image-cleaner ｜ manifest: plugins/image-cleaner/manifest.json

## 定位与形态

- 职责：扫描宿主「图片相册」的全部根目录，按快速 / 完整 MD5 聚出完全重复分组、按 dHash 汉明距离聚出相似分组，再把勾选项交给宿主的删除接口。
- 形态：内嵌 companion；宿主插件是 `image-viewer`，入口页面 `frontend/index.html`，由 `get_extensions` 的 `embedUrl` 挂进宿主侧栏。
- 宿主容器：`HostChannel.mountToolbar` 的 `container` 取 `host-toolbar`，`selector` 取 `#cleaner-actions`，宿主把它解析到 `image-viewer` 的 `#extension-view-actions`。

## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/image-cleaner/backend/main.py` | 插件类、设置 schema、API 登记、统一刷新钩子、扫描分组、删除转发 | `ImageCleanerPlugin`、`settings_schema`、`DHASH_VERSION`、`ALLOWED_EXTENSIONS`、`_ScanCancelled`、`register_api`、`get_status`、`get_settings`、`get_extensions`、`freshness_spec`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild`、`_scan_roots`、`_all_album_files`、`duplicate_scan`、`_duplicate_groups`、`similar_scan`、`_dhash_all`、`_similar_groups`、`_image_dhash`、`_file_quick_hash`、`_file_full_hash`、`_resolve_threshold`、`scan_start`、`_scan_worker`、`scan_status`、`scan_cancel`、`get_cached_scan`、`_regroup_all`、`delete_files`、`_get_host`、`get_data_root`、`get_file_roots`、`thumb_dir`、`ensure_thumb`、`get_thumb_data`、`_scan_cache_path`、`_load_scan_cache`、`_save_scan_cache`、`_save_scan_result`、`_mark_results_stale`、`_persist_stale_flags`、`_dhash_cache_path`、`_load_dhash_cache`、`_save_dhash_cache`、`_dhash_cache_key`、`_abs_for_rel`、`_check_cancel`、`on_settings_changed`、`on_unload` |
| `plugins/image-cleaner/frontend/index.html` | 静态骨架、内嵌态标记、脚本装载 | `#app`、`.view-body`、`#cleaner-toolbar`、`.view-toolbar`、`.cleaner-toolbar`、`.toolbar-group`、`.cleaner-meta`、`.cleaner-scope`、`#cleaner-root`、`.cleaner-root`、`#cleaner-actions`、`#btn-settings`、`.btn`、`.btn-sm`、`.view-content`、`.cleaner-content`、`.obx-scroll`、`.cleaner-tabs`、`#tab-dupe`、`#tab-similar`、`#cleaner-scanned`、`.cleaner-scanned`、`#cleaner-freshness`、`.cleaner-freshness`、`#cleaner-results`、`.cleaner-results`、`.cleaner-footer`、`#cleaner-selected`、`.cleaner-selected`、`#btn-keep-one-all`、`#btn-delete`、`.btn-danger`、`is-embedded` |
| `plugins/image-cleaner/frontend/image-cleaner.css` | 插件全部样式 | `#app`、`.cleaner-toolbar`、`.cleaner-meta`、`.cleaner-scope`、`.cleaner-root`、`.cleaner-content`、`.cleaner-freshness`、`.cleaner-tabs`、`.cleaner-tabs .btn.active`、`.cleaner-results`、`.cleaner-scanned`、`.cleaner-more`、`.cleaner-footer`、`.cleaner-selected`、`.cleaner-group`、`.cleaner-group-head`、`.cleaner-group-head span`、`.cleaner-files`、`.cleaner-file`、`.cleaner-file img`、`.cleaner-file span`、`.cleaner-file input`、`html.is-embedded .cleaner-toolbar`、`--bg-hover`、`--bg-surface`、`--text-primary`、`--text-secondary`、`--text-on-accent`、`--border`、`--accent` |
| `plugins/image-cleaner/frontend/js/app.js` | 单类前端：挂载共享控件与宿主工具栏、读缓存渲染、勾选与删除 | `ImageCleaner`、`mode`、`groups`、`selected`、`pageSize`、`visibleCount`、`lightbox`、`freshness`、`init`、`_bind`、`_mountHostToolbar`、`_mountFreshness`、`openSettings`、`_displayPath`、`updateStatus`、`switchMode`、`_loadResults`、`render`、`keepOneInGroup`、`keepOneForAll`、`_syncCheckboxes`、`showMore`、`updateSelected`、`deleteSelected`、`_escapeHtml`、`_escapeAttr` |

**落盘产物**：宿主 `get_data_root` 下 `.cache` 子目录 `image-cleaner` 中的 `scan_cache.json`，条目键 `groups`、`scanned`、`saved_at`、`stale`，读写符号 `_scan_cache_path`、`_load_scan_cache`、`_save_scan_cache`、`_save_scan_result`、`_persist_stale_flags`；同一目录中的 `dhash.json`，条目键 `v`、`mtime`、`hash`，读写符号 `_dhash_cache_path`、`_load_dhash_cache`、`_save_dhash_cache`、`_image_dhash`、`_dhash_cache_key`。

## 后端 API 契约

`register_api` 登记的方法如下。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `duplicate_scan` | 无 | `{groups, scanned}`，组含 `hash`、`size`、`files` | 全库完全重复分组（同步入口） |
| `similar_scan` | `threshold`，可为 `None` | `{groups, scanned}`，组含 `files` | 全库视觉相似分组（同步入口） |
| `scan_start` | `mode` 取 `dupe` / `similar`，`threshold` | `{started, running, mode}`；已有任务时 `{started, running}` 叠加 `scan_status` 字段 | 起后台扫描任务 |
| `scan_status` | 无 | `{running, done, success, cancelled, mode, total, processed, current, groups}` | 后台任务状态投影 |
| `scan_cancel` | 无 | `{success}`；无任务时 `{success, error}` | 取消插件任务或统一校验 |
| `get_cached_scan` | `mode` | `{groups, scanned, cached, stale, saved_at}` | 读结果缓存 |
| `delete_files` | `rel_paths` | `{deleted, errors}` | 转发宿主的删除实现 |
| `get_status` | 无 | `{host, root_dir, roots, scope}` | 扫描范围与宿主名 |
| `get_settings` | 无 | `threshold`、`root_dir` | 设置值与只读信息行 |
| `save_settings` | `settings` | `{success}` 或 `{success, error}` | 基类写入设置存储 |

壳按名调用的成员如下。

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `get_extensions` | 无 | 条目含 `host`、`id`、`label`、`icon`、`description`、`section`、`embedUrl`、`placement`、`scope` | 注册宿主侧栏入口 |
| `freshness_spec` | 无 | 规格键 `roots`、`prefixes`、`include`、`derive`、`prune`、`on_verified`、`on_pass_end`、`rebuild`、`content_version`、`unit`、`min_sync_interval` | 声明统一刷新规格 |
| `_freshness_derive` | `items` | `{count, errors}` | 为变化条目补算 dHash |
| `_freshness_prune` | `keys` | 删除条目数 | 清掉已消失文件的指纹 |
| `_freshness_audit` | `keys` | `{dropped_hashes, valid_entries}` | 全量校验后对账指纹库 |
| `_freshness_pass_end` | `report`、`verified` | `{hashes, verified}`，校验趟改回 `_regroup_all` 的结果 | 落盘指纹并按需重分组 |
| `_freshness_rebuild` | `kind` | `{kind, cleared}` | 丢弃指纹与结果缓存 |
| `on_settings_changed` | `changed_keys` | 无 | 阈值变更作废相似分组 |
| `on_unload` | 无 | 无 | 取消在跑的扫描任务 |

## 前端结构

DOM 结构树如下。

- `#app`、`.view-body`
  - `#cleaner-toolbar`、`.view-toolbar`、`.cleaner-toolbar`
    - `.toolbar-group`、`.cleaner-meta`
      - `.cleaner-scope`
      - `#cleaner-root`、`.cleaner-root`
    - `#cleaner-actions`、`.toolbar-group`
      - `#btn-settings`、`.btn`、`.btn-sm`
  - `.view-content`、`.cleaner-content`、`.obx-scroll`
    - `.cleaner-tabs`
      - `#tab-dupe`、`.btn`、`.btn-sm`、`active`
      - `#tab-similar`、`.btn`、`.btn-sm`
      - `#cleaner-scanned`、`.cleaner-scanned`
      - `#cleaner-freshness`、`.cleaner-freshness`
    - `#cleaner-results`、`.cleaner-results`
  - `.cleaner-footer`
    - `#cleaner-selected`、`.cleaner-selected`
    - `#btn-keep-one-all`、`.btn`
    - `#btn-delete`、`.btn`、`.btn-danger`

脚本装载顺序：`<head>` 内的内联脚本先按 `?embed` 参数给文档根元素加 `is-embedded` 类，随后引入 `<link rel="stylesheet" href="image-cleaner.css">`；`</body>` 前先加载 `js/app.js`，再由内联脚本执行 `new ImageCleaner` 与 `init`。

`#cleaner-freshness` 内的节点由共享控件渲染，插件不拼接：`.obx-fresh`、`.obx-fresh-status`、`.obx-fresh-verify`、`.obx-fresh-card`。

动态渲染的类名与属性：`.cleaner-group`、`.cleaner-group-head`、`.cleaner-files`、`.cleaner-file`、`.cleaner-more`、`.empty-state`、`.empty-state--error`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`；`id="cleaner-more"`、`data-select-group`、`data-keep-one`、`data-view-file`、`data-group-index`、`data-file`；`#tab-dupe` 与 `#tab-similar` 上切换 `active`。

## 与壳的契约

- 壳基类 `PluginBase`（`shell/backend/plugin_base.py`）：`settings_schema`、`register_api`、`get_settings`、`save_settings`、`setting`、`get_dependency`、`get_extensions`、`freshness_spec`、`on_settings_changed`、`on_unload`、`get_data_root`、`get_file_roots`、`thumb_dir`、`ensure_thumb`、`get_thumb_data`。
- 宿主成员（`image-viewer` 实例）：`name`、`get_data_root`、`get_file_roots`、`thumb_dir`、`ensure_thumb`、`get_thumb_data`、`delete_files`、`list_roots`，其中 `list_roots` 的条目键是 `path`、`prefix`。
- 共享后端基建：`shell/backend/tasks.py` 的 `BackgroundTask`（`kind`、`extra`、`start`、`update`、`status`、`cancel`、`cancelled`、`state`）；`shell/backend/freshness.py` 的 `engine_for`、`request_cancel`、`reset_throttle` 与规格键 `content_version`、`min_sync_interval`；`shell/backend/media_catalog.py` 的 `IMAGE_EXTENSIONS`。
- 壳 CSS 变量：`--bg-surface`、`--bg-hover`、`--text-primary`、`--text-secondary`、`--text-on-accent`、`--border`、`--accent`。
- 壳布局与组件类：`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.obx-scroll`、`.btn`、`.btn-sm`、`.btn-danger`、`.btn.active`、`.empty-state`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`、`.empty-state--error`、`.obx-icon`、`.field-info`。
- 插件自有 CSS 变量前缀：无。
- 共享前端组件（`shell/frontend/public/shell/base.js`、`shell/frontend/public/shell/freshness.js`）：`Bridge.call`、`Bridge.thumbUrl`、`Utils.escapeHtml`、`Toast.warning`、`Toast.error`、`Toast.success`、`confirmDialog`、`openSettingsModal`、`createLightbox`、`HostChannel.mountToolbar`、`HostChannel.requestSettings`、`HostChannel.onCallback`、`Freshness.mount`（返回 `autoSync`）。
- 跨 iframe 读取宿主前端：`parent.imageViewer` 上的 `lightbox` 与它的 `show`，宿主不可用时回落 `createLightbox`。

壳前端契约见 `docs/plugin-ui-guide.md`。
