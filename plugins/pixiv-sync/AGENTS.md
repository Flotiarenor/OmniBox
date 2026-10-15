> pixiv-sync ｜ manifest: plugins/pixiv-sync/manifest.json

## 定位与形态

- 职责：拉取 Pixiv 关注画师的全部作品与当前用户的公开收藏，把作品按画师目录下载进下载根，并把作品 id 记入去重集合，供宿主相册直接显示。
- 形态：内嵌 companion 插件，宿主插件名为 `image-viewer`（由 `manifest.json` 的 `dependencies` 声明，`PixivSyncPlugin._get_host` 通过 `get_dependency` 取得实例）；入口是 `PixivSyncPlugin.get_extensions` 返回的扩展项，其 `embedUrl` 为 `/plugins/pixiv-sync/frontend/index.html`，宿主用 `#extension-frame` 承载该内嵌页。

## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/pixiv-sync/manifest.json` | 插件清单：入口、依赖、权限、图标 | `name`、`displayName`、`icon`、`dependencies`、`permissions`、`libs`、`backend.entry`、`backend.class`、`frontend.entry`、`frontend.route`、`hidden` |
| `plugins/pixiv-sync/backend/main.py` | 插件类：设置 schema、任务线程编排、API 挂载、宿主交互、统一刷新钩子 | `PixivSyncPlugin`、`CACHE_SUBDIR`、`settings_schema`、`register_api`、`get_extensions`、`get_status`、`sync_following`、`sync_bookmarks`、`refresh_following_lists`、`refresh_bookmarks_lists`、`refresh_downloaded`、`verify_downloaded`、`retry_failed`、`cancel_task`、`open_config`、`start_oauth`、`finish_oauth`、`_run_sync`、`_run_refresh`、`_start`、`_start_refresh`、`_authenticate`、`_client`、`_scan_existing_ids`、`_db`、`_get_host`、`_root`、`_cache_dir`、`_ids_file`、`_failed_file`、`_tasks_file`、`_artists_file`、`_works_file`、`_selected_file`、`_load_ids`、`_save_ids`、`_load_failed_ids`、`_save_failed_ids`、`_begin_task_runtime`、`_finish_task_runtime`、`_reset_root_state`、`_load_selected_artists`、`_read_text_robust`、`_max_download`、`_max_artists`、`_workers`、`_scan_workers`、`_note_rate_limited`、`_rate_limit_cooldown`、`get_cache_dir`、`freshness_spec`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild`、`_engine_keys`、`_local_pages`、`_sync_ids_from_keys`、`on_unload`、`on_settings_changed` |
| `plugins/pixiv-sync/backend/libs/pixiv_mini.py` | vendored 精简 Pixiv 客户端（仅依赖 `requests`）：认证、关注/作品/收藏列表、ugoira 元数据、流式下载 | `PixivError`、`JsonDict`、`PixivClient`、`generate_pkce`、`login_url`、`auth`、`auth_with_code`、`set_auth`、`illust_follow`、`user_following`、`user_illusts`、`user_bookmarks_illust`、`illust_detail`、`ugoira_metadata`、`download`、`parse_qs`、`_request`、`_call`、`_header_is_image` |
| `plugins/pixiv-sync/backend/pixiv_sync/__init__.py` | 包说明与模块清单 | （仅包文档字符串） |
| `plugins/pixiv-sync/backend/pixiv_sync/limiter.py` | 全局请求限速与限流异常 | `RateLimitError`、`RateLimiter`、`RateLimiter.set_rate`、`RateLimiter.wait` |
| `plugins/pixiv-sync/backend/pixiv_sync/tasks.py` | 任务状态机：创建、原子落盘、重启恢复 | `new_task`、`persist_task`、`load_task` |
| `plugins/pixiv-sync/backend/pixiv_sync/store.py` | 去重与失败跳过记录的读写、本地图片扫描、应有页数判据 | `ID_NAME_RE`、`expected_pages`、`load_ids`、`save_ids`、`load_failed_ids`、`save_failed_ids`、`scan_local`、`_atomic_write_json`、`_parse_ids`、`_raw_ids` |
| `plugins/pixiv-sync/backend/pixiv_sync/db.py` | 待下载清单 SQLite（`works` / `tags` / `work_tags` / `meta` 表）与完整性判据 | `WorksDB`、`conn`、`close`、`save_pending`、`save_pending_preserve`、`replace_artist`、`load_pending`、`counts`、`mark_done`、`reset_done_ids`、`work_page_counts`、`incomplete_ids`、`importable_ids`、`_insert_work_row`、`_work_to_item`、`_item_to_work_value`、`_save_scan` |
| `plugins/pixiv-sync/backend/pixiv_sync/artist.py` | 画师目录解析：名字清洗、id→名字缓存、改名迁移与同名消歧 | `sanitize`、`load_cache`、`save_cache`、`artist_dir`、`_disambiguate`、`_other_dir_names`、`_WINDOWS_RESERVED` |
| `plugins/pixiv-sync/backend/pixiv_sync/scan.py` | 刷新清单：关注（`done_uids` 画师级断点 + `illust_follow` 聚合增量 + 逐画师兜底）与收藏（`next_qs` 翻页断点） | `fetch_following`、`fetch_artist`、`collect_following_pending`、`collect_bookmarks_pending`、`_scan_artist_window`、`_fetch_follow_stream`、`build_item`、`extract_all_urls`、`_flatten_items`、`_group_items`、`_uid_of`、`_safe_int`、`MAX_FOLLOW_PAGES` |
| `plugins/pixiv-sync/backend/pixiv_sync/download.py` | 按清单并行下载、404 永久跳过、ugoira 分流、画师目录归属 | `download_pending`、`process_illust`、`process_ugoira`、`all_image_urls`、`_work_id`、`_safe_int`、`_safe_ext` |
| `plugins/pixiv-sync/backend/pixiv_sync/ugoira.py` | ugoira zip 帧序列 → 动画 WebP：帧时序解析与体积上限 | `parse_metadata`、`convert`、`_load_pil_image`、`DEFAULT_DELAY_MS`、`WEBP_QUALITY`、`MAX_FRAMES`、`MAX_FILE_BYTES`、`MAX_TOTAL_PIXELS` |
| `plugins/pixiv-sync/backend/pixiv_sync/oauth.py` | 内置 OAuth PKCE 向导：生成登录地址与用授权码换 token | `start`、`finish` |
| `plugins/pixiv-sync/backend/pixiv_sync/pixiv_purge_non_original.py` | 伴侣命令行工具：删除 1200px 缩放图并清除记录以便重下原图 | `THRESHOLD`、`ID_NAME_RE`、`CACHE_SUBDIR`、`image_size`、`scan_works`、`classify`、`strip_ids`、`reset_done_in_db`、`prune_empty_dirs`、`resolve_root`、`_data_root_from_yaml`、`_atomic_write_json`、`main` |
| `plugins/pixiv-sync/backend/pixiv_sync/pixiv_unmark_recent.py` | 伴侣命令行工具：把已下载记录改回未下载（只改记录） | `ID_NAME_RE`、`CACHE_SUBDIR`、`collect_ids_in_window`、`collect_missing_ids`、`strip_ids`、`reset_done`、`resolve_root`、`_data_root_from_yaml`、`_atomic_write_json`、`main` |
| `plugins/pixiv-sync/frontend/index.html` | 唯一前端文件：内联 `<style>` 与内联 IIFE 脚本，承载状态栏、操作按钮、进度条、OAuth 弹窗 | 见「前端结构」一节 |

**落盘产物**：作品图片写在 `<root>/pixiv/` 下（多图入 `{作品id}/` 子文件夹，无画师信息的作品入 `未分类`），目录由 `artist.artist_dir` 解析、由 `download.download_pending` 落盘；缓存写在 `<root>/.cache/pixiv-sync/`（`CACHE_SUBDIR`）：`downloaded_ids.json`（`store.load_ids` / `store.save_ids`）、`failed_ids.json`（`store.load_failed_ids` / `store.save_failed_ids`）、`artists.json`（`artist.load_cache` / `artist.save_cache`）、`tasks.json`（`tasks.load_task` / `tasks.persist_task`）、`works.db`（`WorksDB`）、`selected_artists.txt`（`PixivSyncPlugin._load_selected_artists` / `PixivSyncPlugin.open_config`）；壳的指纹库 `freshness.db` 由 `PluginBase.get_cache_dir` 指向同一目录。两个伴侣工具直接改这些文件：`pixiv_purge_non_original.strip_ids` / `pixiv_purge_non_original.reset_done_in_db` 与 `pixiv_unmark_recent.strip_ids` / `pixiv_unmark_recent.reset_done`。

## 后端 API 契约

`register_api` 暴露的方法如下表。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `get_status` | 无 | `{task, root_dir, token_configured, downloaded_total, running, selected_artists, selected_file, following_total, following_done, pending_following, bookmarks_total, bookmarks_done, pending_bookmarks, other_done, failed_skipped, rate_limit_cooldown}` | 状态轮询与界面刷新 |
| `sync_following` | 无 | `{ok, data: {kind}}`，占用中返回 `{ok: false, error}` | 启动「同步画师」任务，内部先刷新关注清单再下载 |
| `sync_bookmarks` | 无 | `{ok, data: {kind}}`，占用中返回 `{ok: false, error}` | 启动「同步喜欢」任务，内部先刷新收藏清单再下载 |
| `refresh_following_lists` | 无 | `{ok}`，占用中返回 `{ok: false, error}` | 只刷新关注清单、不下载 |
| `refresh_bookmarks_lists` | 无 | `{ok}`，占用中返回 `{ok: false, error}` | 只刷新收藏清单、不下载 |
| `refresh_downloaded` | 无 | `{ok, total, zero_removed, stale_removed, failed_cleared, incomplete_skipped}`，基建未接入返回 `{ok: false, error}` | 兼容入口：按本地重建已下载记录 |
| `verify_downloaded` | 无 | `{ok, stale_removed, zero_removed, failed_cleared, total, incomplete}`，基建未接入返回 `{ok: false, error}` | 兼容入口：全量校验已下载内容 |
| `retry_failed` | 无 | `{ok, cleared, kind}`，占用中返回 `{ok: false, error}` | 清空 `failed_ids.json` 并按最近任务来源重新同步 |
| `cancel_task` | 无 | `{ok}` | 请求取消当前任务 |
| `open_config` | 无 | `{ok, file}`，失败返回 `{ok: false, error}` | 重写画师名单模板并打开其所在文件夹 |
| `start_oauth` | 无 | `{ok, url, challenge}`，失败返回 `{ok: false, error}` | OAuth 第一步：生成 PKCE 挑战并打开登录页 |
| `finish_oauth` | `code: str` | `{ok, user_id}`，失败返回 `{ok: false, error}` | OAuth 第二步：用授权码换 token 并回写 `refresh_token` |
| `get_settings` | 无 | `dict`，凭据类键返回掩码 | 继承 `PluginBase`，供设置弹窗回填 |
| `save_settings` | `settings: dict` | `{success, error}` | 继承 `PluginBase`，写入后触发 `on_settings_changed` |

任务对象字段（`tasks.new_task`）：`kind`、`state`、`done`、`total`、`downloaded`、`skipped`、`failed`、`current`、`started_at`、`finished_at`、`error`；`state` 取值为 `queued` / `running` / `done` / `failed` / `cancelled`，`tasks.load_task` 把残留的 `queued` / `running` 归一成 `paused`。

壳与宿主按名调用的成员如下表。

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `settings_schema` | 无 | `List[Dict[str, Any]]` | 壳的设置弹窗按它渲染；键为 `refresh_token`（`secret`）、`proxy`、`download_dir`（`directory`、`admin_only`）、`workers`、`max_download`、`max_artists`、`rate_limit`、`scan_workers` |
| `register_api` | 无 | `Dict[str, Callable]` | 壳把返回的每一项登记进 `PluginManager._api_methods`，键为插件名加 `__` 加方法名 |
| `get_extensions` | 无 | `List[dict]` | 宿主左栏扩展入口；字段 `host`、`id`、`label`、`icon`、`description`、`section`、`embedUrl`、`placement`、`scope` |
| `freshness_spec` | 无 | `dict` | 声明统一刷新的受管根与钩子；键为 `roots`、`include`、`derive`、`prune`、`on_verified`、`on_pass_end`、`rebuild`、`content_version`、`unit`、`min_sync_interval` |
| `get_cache_dir` | 无 | `Path` | 返回 `<root>/.cache/pixiv-sync`；壳的 `freshness.db` 落在其下 |
| `on_unload` | 无 | `None` | 停任务线程、落盘 `tasks.json`、关闭 `WorksDB` 连接 |
| `on_settings_changed` | `changed_keys: set` | `None` | 按 `refresh_token`、`proxy`、`download_dir`、`rate_limit` 重置客户端、下载根与限速 |
| `update_setting` | `key: str`、`value: Any` | `bool` | 插件回写 Pixiv 轮换后的 `refresh_token` |
| `get_settings_schema` | 无 | `List[dict]` | 壳的 `PluginManager` 生成，供 `HostChannel.requestSettings` 读取字段清单 |
| `get_data_root` | 无 | `Path` | 宿主 `image-viewer` 的成员，`PixivSyncPlugin._root` 在未配置 `download_dir` 时取它的返回值 |
| `_freshness_derive` | `items` | `{count}` | 壳引擎经 `derive` 调用，累计本趟本地变化数 |
| `_freshness_prune` | `keys` | `int` | 壳引擎经 `prune` 调用，累计被清理的键 |
| `_freshness_audit` | `keys` | `{ok, total, imported, stale_removed, incomplete, incomplete_skipped, failed_cleared, zero_removed}` | 壳引擎经 `on_verified` 调用，按有效键集合重建记录 |
| `_freshness_pass_end` | `report`、`verified: bool` | 同上，跳过时返回 `{skipped: 'incomplete'}` 或 `{skipped: 'unchanged'}` | 壳引擎经 `on_pass_end` 调用，整趟结束时重算 ids |
| `_freshness_rebuild` | `kind: str` | 同上 | 壳引擎经 `rebuild` 调用，按磁盘重算记录 |

## 前端结构

DOM 结构树如下。

- `#app.view-body.psync-main`
  - `div.view-toolbar`
    - `div.toolbar-group` → `span.psync-title`、`svg.obx-icon`（`use[href="#palette"]`）
    - `div.toolbar-group` → `#btn-cancel.btn.btn-sm`（内联 `display:none`）
  - `div.view-content.obx-scroll`
    - `div.psync-status` → `#st-token.dot.idle`、`#st-root`、`#st-total`、`#st-last`
    - `div.psync-status` → `#st-ft`、`#st-pf`、`#st-fd`、`#st-bt`、`#st-pb`、`#st-bd`、`#st-od`、`#st-failed`
    - `div.psync-btns` → `#btn-following.btn`（`use[href="#download"]`）、`#btn-bookmarks.btn`（`use[href="#heart"]`）
    - `div.psync-btns` → `#btn-refresh-following.btn.btn-sm`、`#btn-refresh-bookmarks.btn.btn-sm`（`use[href="#refresh-cw"]`）
    - `div.psync-btns` → `#psync-freshness`（`Freshness.mount` 的挂载点）、`#btn-retry-failed.btn.btn-sm.btn-danger`（`use[href="#trash-2"]`）
    - `div.psync-progress#progress-box` → `div.psync-bar > div#bar`、`div.meta` → `#p-meta`、`#p-pct`
    - `#p-current.psync-current`
    - `div.psync-options` → `h4`、`div.psync-tip`（固定行为说明文字）
    - `div.psync-status` → `#st-selected.field-help`、`div#psync-global-actions` → `#btn-oauth.btn.btn-sm.obx-toolbar-btn`（`use[href="#key-round"]`）、`#btn-open-config.btn.btn-sm.obx-toolbar-btn`（`use[href="#folder-open"]`）、`#btn-settings.btn.btn-sm.obx-toolbar-btn`（`use[href="#settings"]`）
    - `div.psync-tip`、`#psync-cooldown.psync-tip.psync-danger`
- `#oauth-modal.modal`（与 `#app` 平级）
  - `div.modal-box.psync-oauth-box` → `h3`、`div.modal-body`（`p.psync-note.psync-danger`、`ol.psync-oauth-steps`、`#oauth-url.psync-oauth-url`、`div.psync-modal-row` → `#btn-copy-url.btn.btn-sm`、`ol.psync-oauth-steps`、`#oauth-code.psync-oauth-input`）、`div.modal-footer` → `#btn-oauth-finish.btn.btn-sm.btn-primary`、`#btn-oauth-close.btn.btn-sm`

插件自有类名前缀为 `psync-`，另有状态点短类 `dot`、`ok`、`bad`、`idle`。

脚本装载顺序如下。

- `index.html` 的 `<head>` 内联脚本：读 `location.search` 的 `embed` 参数，命中时给 `document.documentElement` 加 `is-embedded`。
- `index.html` 的 `</body>` 前内联脚本：单个 IIFE，在 `DOMContentLoaded` 里依次执行 `mountFreshness`、各按钮的 `addEventListener`、`HostChannel.mountToolbar`、`refreshStatus`、`bindLifecycle`。
- 壳注入（`</head>` 之前，来自 `shell/backend/file_server.py` 的 `_PLUGIN_BOOTSTRAP_SCRIPT`）：`/shell/variables.css`、`/shell/shared.css`、`/shell/base.css`、`/shell/folder-picker.css`、`/shell/freshness.css`、`/shell/effects.css`、`/shell/splitter.css`，随后 `/shell/base.js`、`/shell/folder-picker.js`、`/shell/freshness.js`、`/shell/motion.js`、`/shell/splitter.js`、`/shell/icons.generated.js` 与 `Bridge.setPrefix('pixiv-sync')`；本页的内联 `<style>` 排在壳样式之前。

脚本拼接、静态搜索看不到的类名如下。

- `is-embedded`：`document.documentElement.classList.add`
- `dot ok` / `dot bad`：`refreshStatus` 重写 `#st-token` 的 `className`
- `active`：`#oauth-modal.classList` 的增删
- `#psync-freshness` 内部由 `shell/frontend/public/shell/freshness.js` 拼出 `obx-fresh`、`obx-fresh-compact`、`obx-fresh-status`、`obx-fresh-verify`、`obx-fresh-card`、`obx-fresh-card-head`、`obx-fresh-hide`、`obx-fresh-count`、`obx-fresh-bar`、`obx-fresh-bar-inner`、`obx-fresh-current`、`obx-fresh-errors`、`obx-fresh-card-foot`、`hidden`

## 与壳的契约

- 壳类：`PluginBase`（`shell/backend/plugin_base.py`）。本插件覆写 `register_api`、`settings_schema`、`get_cache_dir`、`freshness_spec`、`get_extensions`、`on_unload`、`on_settings_changed`，使用继承的 `setting`、`update_setting`、`get_settings`、`save_settings`、`get_dependency`。
- 统一刷新基建：`engine_for`（`shell/backend/freshness.py`）返回的 `FreshnessEngine`，本插件调用其 `sync`、`verify`、`state`、`valid_keys`、`reset_throttle`，并经 `freshness_spec` 交出 `_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild`。
- 壳 CSS 变量（`shell/frontend/public/shell/variables.css`）：`--text-primary`、`--text-secondary`、`--text-muted`、`--bg-hover`、`--bg-surface`、`--border`、`--accent`、`--danger`、`--success`、`--radius`、`--radius-sm`。
- 插件自有 CSS 变量前缀：本插件不定义自有 CSS 变量，颜色与圆角全部取壳 token。
- 共享前端组件：`Freshness.mount`（选项 `plugin`、`container`、`unit`、`onChange`）、`HostChannel.requestSettings`、`HostChannel.onCallback('ui', …)`、`HostChannel.mountToolbar`（选项 `container: 'host-toolbar'`、`selector: '#psync-global-actions'`）、`PluginLifecycle.onShow`、`PluginLifecycle.onHide`、`PluginLifecycle.onDispose`、`openSettingsModal`、`Bridge.call`、`Icons.html`、`Utils.escapeHtml`、`Toast.success`、`Toast.error`、`Toast.warning`、`Toast.info`。
- 共享样式类（`shared.css` / `base.css` / `effects.css`）：`.btn`、`.btn-sm`、`.btn-primary`、`.btn-danger`、`.field-help`、`.obx-icon`、`.obx-toolbar-btn`、`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.obx-scroll`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`。
- 宿主 `image-viewer` 的容器：`#extension-frame` 承载本插件内嵌页，`#extension-view-actions` 是 `HostChannel` 的 `host-toolbar` 容器。
- 详见 `docs/plugin-ui-guide.md`。
