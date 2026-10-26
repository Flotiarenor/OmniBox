> pixiv-sync ｜ manifest: plugins/pixiv-sync/manifest.json

## 定位与形态

- 职责：拉取 Pixiv 关注画师的全部作品与当前用户的公开收藏，把作品按画师目录下载进下载根，并把作品 id 记入去重集合，供宿主相册直接显示。
- 形态：内嵌 companion 插件；宿主插件是 `image-viewer`（`manifest.json` 的 `dependencies` 声明依赖，`PixivSyncPlugin._get_host` 通过 `get_dependency` 取得实例）。入口来自 `PixivSyncPlugin.get_extensions`：新壳读 `native` 声明，在宿主页内容槽渲染 `replace` 型原生面板，逻辑页是 `frontend/native.html`（只提交 schema blocks 并处理动作）；不认 `native` 的旧壳回落 `embedUrl` → `frontend/index.html` 的完整页面。
- `manifest.json` 关键字段：`name` = `pixiv-sync`；`backend.entry` = `backend/main.py`；`backend.class` = `PixivSyncPlugin`；`frontend.entry` = `frontend/index.html`；`frontend.route` = `/pixiv-sync`；`permissions` = `filesystem:read`、`filesystem:write`、`network`；`dependencies` = `["image-viewer"]`；`libs` = `["backend/libs", "backend"]`；`icon` = `icon:palette`；`hidden` = `true`。
- 旧内嵌页的宿主容器：`HostChannel.mountToolbar` 的 `container` 取 `host-toolbar`，`selector` 取 `#psync-global-actions`，宿主把它解析到 `image-viewer` 的 `#extension-view-actions`。原生面板不再使用宿主工具栏。
- 设置归属：原生面板顶栏的「设置」发 `open-settings`，由壳拦截并用 `PluginSettingsModal` 打开本插件的 `settings_schema`；保存成功后壳向逻辑页回发 `settings-saved`，逻辑页重读状态。面板不再自绘设置页，`refresh_token` 的清除由弹窗的显式清除能力承担；原生面板仍保留「清除 Token」内容动作作为账号级快捷入口。旧 `index.html` 的设置仍走 `HostChannel.requestSettings` / `openSettingsModal`。

## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/pixiv-sync/manifest.json` | 插件清单：入口、依赖、权限、图标 | `name`、`displayName`、`icon`、`dependencies`、`permissions`、`libs`、`backend.entry`、`backend.class`、`frontend.entry`、`frontend.route`、`hidden` |
| `plugins/pixiv-sync/backend/main.py` | 插件类：设置 schema、任务线程编排、API 挂载、宿主交互、扩展声明、统一刷新钩子 | `PixivSyncPlugin`、`CACHE_SUBDIR`、`settings_schema`、`register_api`、`get_extensions`、`get_status`、`sync_following`、`sync_bookmarks`、`refresh_following_lists`、`refresh_bookmarks_lists`、`refresh_downloaded`、`verify_downloaded`、`retry_failed`、`cancel_task`、`open_config`、`start_oauth`、`finish_oauth`、`_run_sync`、`_run_refresh`、`_start`、`_start_refresh`、`_authenticate`、`_client`、`_scan_existing_ids`、`_db`、`_get_host`、`_root`、`_cache_dir`、`_ids_file`、`_failed_file`、`_tasks_file`、`_artists_file`、`_works_file`、`_selected_file`、`_load_ids`、`_save_ids`、`_load_failed_ids`、`_save_failed_ids`、`_begin_task_runtime`、`_finish_task_runtime`、`_reset_root_state`、`_load_selected_artists`、`_read_text_robust`、`_max_download`、`_max_artists`、`_workers`、`_scan_workers`、`_note_rate_limited`、`_rate_limit_cooldown`、`get_cache_dir`、`freshness_spec`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild`、`_engine_keys`、`_local_pages`、`_sync_ids_from_keys`、`on_unload`、`on_settings_changed` |
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
| `plugins/pixiv-sync/frontend/native.html` | 原生逻辑页骨架：无样式、无 chrome，只装载统一客户端与业务脚本 | `/shell/native-client.js`、`js/native.js` |
| `plugins/pixiv-sync/frontend/js/native.js` | 原生面板逻辑：拉状态、编译 schema blocks 与顶栏 toolbar、处理动作、按任务状态轮询 | `state`、`STATUS_IDLE_MS`、`STATUS_RUNNING_MS`、`STATUS_COOLDOWN_MS`、`ACTIVE_STATES`、`TASK_STATES`、`TASK_KINDS`、`client`、`render`、`buildBlocks`、`buildToolbar`、`signature`、`statusBlocks`、`taskBlocks`、`mainBlocks`、`oauthBlocks`、`pendingBlocks`、`loadStatus`、`schedulePoll`、`restartPoll`、`tick`、`dispose`、`init`、`startTask`、`refreshNow`、`cancelTask`、`retryFailed`、`startOauth`、`finishOauth`、`clearToken`、`openConfig`、`askRetryFailed`、`askLogout`、`handleAction`、`open-settings`、`settings-saved` |
| `plugins/pixiv-sync/frontend/index.html` | 旧壳回落页：内联 `<style>` 与内联 IIFE 脚本，承载状态栏、操作按钮、进度条、OAuth 弹窗 | 见「前端结构」一节 |

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
| `get_settings` | 无 | `dict`，凭据类键返回掩码 | 继承 `PluginBase`，供壳设置弹窗回填 |
| `save_settings` | `settings: dict` | `{success, error}` | 继承 `PluginBase`，写入后触发 `on_settings_changed`；`refresh_token` 传空字符串即清除凭据 |

任务对象字段（`tasks.new_task`）：`kind`、`state`、`done`、`total`、`downloaded`、`skipped`、`failed`、`current`、`started_at`、`finished_at`、`error`；`state` 取值为 `queued` / `running` / `done` / `failed` / `cancelled`，`tasks.load_task` 把残留的 `queued` / `running` 归一成 `paused`。

壳与宿主按名调用的成员如下表。

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `settings_schema` | 无 | `List[Dict[str, Any]]` | 设置字段唯一来源；键为 `refresh_token`（`secret`）、`proxy`、`download_dir`（`directory`、`admin_only`）、`workers`、`max_download`、`max_artists`、`rate_limit`、`scan_workers` |
| `register_api` | 无 | `Dict[str, Callable]` | 壳把返回的每一项登记进 `PluginManager._api_methods`，键为插件名加 `__` 加方法名 |
| `get_extensions` | 无 | `List[dict]` | 宿主左栏扩展入口；字段 `host`、`id`、`label`、`icon`、`description`、`section`、`native`（`url`、`title`）、`embedUrl`、`placement`、`scope` |
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

原生逻辑页（`frontend/native.html`）不产生可见界面：

- `<body>` 只装载 `/shell/native-client.js` 与 `js/native.js`，不引入样式表、不建 DOM；壳的 `NativePanel` 负责可见界面，`SchemaRenderer` 渲染 blocks。
- 消息与生命周期：逻辑页装载 `/shell/native-client.js`，用 `NativeExtension.create({ onInit, onAction, onDispose })`；客户端统一处理 `omnibox:native-ready`（含重试）、`omnibox:native-init`、`omnibox:native-action`、`omnibox:schema` 与 pagehide / beforeunload 清理，本页只实现 `init`、`handleAction` 与 blocks 构造。
- 提交的 blocks 类型：`heading`、`kv`、`text`（tone 取 `default` / `muted` / `danger`）、`empty`（错误态与未配置 Token 的空态）、`progress`（任务进度与取消）、`actions`、`form`（仅 OAuth 授权码）。提示与两步确认都用 `text` + `actions` 表达：隐藏 iframe 里的 Toast / modal 用户看不见。
- 动作标识：`reload`、`refresh-status`、`sync-following`、`sync-bookmarks`、`refresh-following`、`refresh-bookmarks`、`cancel-task`、`retry-failed`、`retry-failed-confirm`、`pending-cancel`、`oauth-start`、`oauth-restart`、`oauth-finish`、`oauth-close`、`logout`、`logout-confirm`、`open-settings`、`settings-saved`、`open-config`。
- 数据来源：`Bridge.call` 的 `get_status`、`sync_following`、`sync_bookmarks`、`refresh_following_lists`、`refresh_bookmarks_lists`、`retry_failed`、`cancel_task`、`open_config`、`start_oauth`、`finish_oauth`、`save_settings`（仅清除 Token）。设置读写由壳的 `PluginSettingsModal` 直接调用后端的 `get_settings_schema` / `get_settings` / `save_settings`。
- 设置：面板不再自绘设置表单；顶栏「设置」发 `open-settings`，壳用 `PluginSettingsModal` 按后端 `settings_schema` 渲染，保存成功后向本页发 `settings-saved`，本页重读 `get_status`（凭据、下载根目录与计数可能变化）。`download_dir` 是 `type: "directory"` 且 `admin_only`；`refresh_token` 是 `secret`，清除由弹窗的显式清除能力承担（提交空串），本页仍保留「清除 Token」两步确认作为账号级快捷入口。
- 轮询幂等：同一时刻只有一个 `setTimeout`（`schedulePoll` 在已有定时器时直接返回），任务排队/运行时 1.5s、限流冷却时 1 s、空闲 8 s；主视图只在状态签名变化时重发 blocks，OAuth 视图在轮询中不重发，避免重建授权码表单值。两步确认（清除失败跳过记录、清除 Token）只改变本地状态，第二次点击才调后端。

旧壳回落页（`frontend/index.html`）结构如下，供不认 `native` 的壳继续使用。

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

旧内嵌页的脚本装载顺序：`index.html` 的 `<head>` 内联脚本读 `location.search` 的 `embed` 参数，命中时给 `document.documentElement` 加 `is-embedded`；`</body>` 前的内联 IIFE 在 `DOMContentLoaded` 里依次执行 `mountFreshness`、各按钮的 `addEventListener`、`HostChannel.mountToolbar`、`refreshStatus`、`bindLifecycle`。壳注入（`</head>` 之前，来自 `shell/backend/file_server.py` 的 `_PLUGIN_BOOTSTRAP_SCRIPT`）：`/shell/variables.css`、`/shell/shared.css`、`/shell/base.css`、`/shell/folder-picker.css`、`/shell/freshness.css`、`/shell/effects.css`、`/shell/splitter.css`，随后 `/shell/base.js`、`/shell/folder-picker.js`、`/shell/freshness.js`、`/shell/motion.js`、`/shell/splitter.js`、`/shell/icons.generated.js` 与 `Bridge.setPrefix('pixiv-sync')`；旧内嵌页的内联 `<style>` 排在壳样式之前；原生逻辑页没有自有样式表，只装载 `/shell/native-client.js` 与 `js/native.js`——壳注入的 `/shell/*.css` / `/shell/*.js` 仍会进它的文档（`Bridge.setPrefix` 需要），但本页不使用共享 UI 组件。

旧内嵌页动态渲染的短类：`dot ok` / `dot bad`、`active`（`#oauth-modal`）、`is-embedded`（`document.documentElement`）；`#psync-freshness` 内部节点由 `shell/frontend/public/shell/freshness.js` 拼出。

## 与壳的契约

- 原生内容：`shell/frontend/src/components/NativePanel.vue` 在宿主 `PluginChrome` 内容槽承载隐藏的逻辑 iframe（`omnibox:native-ready` / `omnibox:native-init` / `omnibox:native-action` / `omnibox:native-error`），`shell/frontend/src/components/SchemaRenderer.vue` 渲染 `omnibox:schema` 的 blocks；schema 类型见 `shell/frontend/src/core/plugin-content.ts`。`form` 的 `directory` 字段由 `SchemaRenderer` 调壳内 `FolderPicker`（`/shell/folder-picker.js`）选择，按字段的 `local_only` 决定是否提供「网络位置」入口（在壳文档里执行）。面板打开期间宿主侧栏/顶栏保留，内容 iframe 用 `v-show` 隐藏；标题与页面级动作由宿主顶栏负责，退出走导航或再次点击入口。
- 扩展声明：`get_extensions` 同时给 `native` 与 `embedUrl`；`shell/frontend/public/shell/base.js` 的扩展入口遇到 `native` 时向壳发 `omnibox:open-native`，壳校验插件名形状与 URL 前缀后挂载 `NativePanel`。
- 设置归属：native toolbar 的 `open-settings` 由壳拦截（不下发到隐藏逻辑页），壳用 `nativePanel.host` + `nativePanel.plugin` 打开 `PluginSettingsModal`；弹窗按本插件 `settings_schema` 调 `get_settings_schema` / `get_settings` / `save_settings`。保存成功后壳关闭弹窗并向逻辑页发 `settings-saved`。`secret` 的清除由弹窗显式提交空串；`download_dir` 的目录字段由弹窗的 `FolderPicker` 目录行选择（`SettingsDirectoryField`），不再走系统对话框。
- 不读宿主：原生逻辑页不访问 `parent.imageViewer`、不读写宿主 DOM；后端只经 `Bridge.call` 调用，宿主能力不参与面板渲染。
- 壳类：`PluginBase`（`shell/backend/plugin_base.py`）。本插件覆写 `register_api`、`settings_schema`、`get_cache_dir`、`freshness_spec`、`get_extensions`、`on_unload`、`on_settings_changed`，使用继承的 `setting`、`update_setting`、`get_settings`、`save_settings`、`get_dependency`。
- 统一刷新基建：`engine_for`（`shell/backend/freshness.py`）返回的 `FreshnessEngine`，本插件调用其 `sync`、`verify`、`state`、`valid_keys`、`reset_throttle`，并经 `freshness_spec` 交出 `_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild`。
- 共享前端组件（旧回落页使用；原生逻辑页只调用壳注入的 `Bridge`）：`Bridge.call`、`Bridge.callSystem`、`Freshness.mount`、`HostChannel.requestSettings`、`HostChannel.onCallback`、`HostChannel.mountToolbar`、`PluginLifecycle.onShow` / `onHide` / `onDispose`、`openSettingsModal`、`Icons.html`、`Utils.escapeHtml`、`Toast.success` / `error` / `warning` / `info`。原生逻辑页只用 `Bridge.call` 与 `postMessage`，不挂共享 UI 组件。
- 壳 CSS 变量（`shell/frontend/public/shell/variables.css`）：`--text-primary`、`--text-secondary`、`--text-muted`、`--bg-hover`、`--bg-surface`、`--border`、`--accent`、`--danger`、`--success`、`--radius`、`--radius-sm`；原生面板不写 CSS，视觉全部由壳渲染。
- 共享样式类（旧回落页使用）：`.btn`、`.btn-sm`、`.btn-primary`、`.btn-danger`、`.field-help`、`.obx-icon`、`.obx-toolbar-btn`、`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.obx-scroll`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`。
- 宿主 `image-viewer` 的容器（旧回落页）：`#extension-frame` 承载内嵌页，`#extension-view-actions` 是 `HostChannel` 的 `host-toolbar` 容器；原生面板覆盖宿主内容槽，不再使用这些容器。
- 详见 `docs/plugin-ui-guide.md`。
