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
| `plugins/pixiv-sync/backend/main.py` | 入口：设置 schema 引用、API 表、壳按名调用的钩子、装配，以及既有模块与用例按实例取用的短委派 | `PixivSyncPlugin`、`settings_schema`、`register_api`、`get_extensions`、`get_cache_dir`、`freshness_spec`、`on_unload`、`on_settings_changed`、`start_oauth`、`finish_oauth`、`_get_host`、`_invalidate_host_derived`、`_host_data_root`、`_engine`、`_root`、`_ids_file`、`_tasks_file`、`_artists_file`、`_client`、`_db`、`_load_ids`、`_load_selected_artists`、`_scan_existing_ids`、`_max_download`、`_max_artists`、`_workers`、`_scan_workers`、`_pixiv_client`、`_rate_limiter`、`_thread`、`_cancel_flag`、`_cancel_event` |
| `plugins/pixiv-sync/backend/pixiv_sync/const.py` | 纯数据：设置项 schema、扩展入口声明、缓存子目录、刷新基建的扩展名 | `CACHE_SUBDIR`、`FRESHNESS_INCLUDE`、`SETTINGS_SCHEMA`、`extensions` |
| `plugins/pixiv-sync/backend/pixiv_sync/paths.py` | 下载根与派生路径；任务运行期间固定根 | `CachePaths`、`root`、`begin_task`、`finish_task`、`reset`、`pixiv_root`、`cache_dir`、`ids_file`、`failed_file`、`tasks_file`、`artists_file`、`works_file`、`selected_file` |
| `plugins/pixiv-sync/backend/pixiv_sync/session.py` | Pixiv 客户端、认证与令牌轮换回写、全局限速与 429 冷却 | `PixivSession`、`client`、`cached_client`、`reset_client`、`authenticate`、`note_rate_limited`、`cooldown_seconds`、`apply_rate_setting` |
| `plugins/pixiv-sync/backend/pixiv_sync/records.py` | 去重集合与永久跳过集合的内存态与落盘时机 | `IdRecords`、`downloaded`、`save_downloaded`、`failed`、`save_failed`、`clear_failed`、`reset` |
| `plugins/pixiv-sync/backend/pixiv_sync/limits.py` | 设置项里的数量上限与并发数 | `SettingsLimits`、`max_download`、`max_artists`、`scan_workers`、`workers` |
| `plugins/pixiv-sync/backend/pixiv_sync/workspace.py` | 清单 SQLite 连接、任务状态文件、下载根的固定 | `TaskWorkspace`、`db`、`close_db`、`tasks_file`、`load_task`、`persist_task`、`begin`、`finish` |
| `plugins/pixiv-sync/backend/pixiv_sync/library.py` | 本地图片树 → 已下载作品集合（统一刷新基建的钩子与按清单对账）；`state()` 投影带 `version_stale` / `stale_dirs` | `LocalLibrary`、`cache_dir`、`engine`、`spec`、`state`、`derive`、`prune`、`audit`、`pass_end`、`rebuild`、`engine_keys`、`local_pages`、`sync_ids_from_keys`、`scan_existing_ids` |
| `plugins/pixiv-sync/backend/pixiv_sync/selection.py` | 画师名单读取（带 mtime 缓存）与模板重写 | `ArtistSelection`、`selected`、`reset`、`open_config`、`read_text_robust` |
| `plugins/pixiv-sync/backend/pixiv_sync/runner.py` | 同步/刷新任务线程、取消、重试与设置变更编排 | `SyncRunner`、`invalidate_host`、`_notify_host_written`、`sync_following`、`sync_bookmarks`、`refresh_following_lists`、`refresh_bookmarks_lists`、`cancel_task`、`cancel_event`、`task_snapshot`、`is_running`、`reset_root_state`、`apply_settings_changed`、`shutdown` |
| `plugins/pixiv-sync/backend/pixiv_sync/maintenance.py` | 显式维护动作：刷新已下载记录、校验本地内容、停止校验；同步任务运行中两者都拒绝执行 | `Maintenance`、`refresh_downloaded`、`verify_downloaded`、`cancel_verify` |
| `plugins/pixiv-sync/backend/pixiv_sync/status.py` | `get_status` 聚合：任务、清单统计、账号与本地校验进度 | `StatusReport`、`report` |
| `plugins/pixiv-sync/backend/pixiv_sync/limiter.py` | 全局请求限速与限流异常 | `RateLimitError`、`RateLimiter`、`RateLimiter.set_rate`、`RateLimiter.wait` |
| `plugins/pixiv-sync/backend/pixiv_sync/tasks.py` | 任务状态机：创建、原子落盘、重启恢复 | `new_task`、`persist_task`、`load_task` |
| `plugins/pixiv-sync/backend/pixiv_sync/store.py` | 去重与失败跳过记录的读写、本地图片扫描、应有页数判据 | `ID_NAME_RE`、`expected_pages`、`load_ids`、`save_ids`、`load_failed_ids`、`save_failed_ids`、`scan_local`、`_parse_ids`、`_raw_ids` |
| `plugins/pixiv-sync/backend/pixiv_sync/db.py` | 待下载清单 SQLite（`works` / `tags` / `work_tags` / `meta` 表）与完整性判据 | `WorksDB`、`conn`、`close`、`save_pending`、`save_pending_preserve`、`replace_artist`、`load_pending`、`counts`、`mark_done`、`reset_done_ids`、`work_page_counts`、`incomplete_ids`、`importable_ids`、`_insert_work_row`、`_work_to_item`、`_item_to_work_value`、`_save_scan` |
| `plugins/pixiv-sync/backend/pixiv_sync/artist.py` | 画师目录解析：名字清洗、id→名字缓存、改名迁移与同名消歧 | `sanitize`、`load_cache`、`save_cache`、`artist_dir`、`_disambiguate`、`_other_dir_names`、`_WINDOWS_RESERVED` |
| `plugins/pixiv-sync/backend/pixiv_sync/scan.py` | 关注清单：画师级断点（`done_uids`）+ `illust_follow` 聚合增量 + 逐画师兜底 | `fetch_following`、`fetch_artist`、`collect_following_pending`、`_scan_artist_window`、`_fetch_follow_stream`、`MAX_FOLLOW_PAGES` |
| `plugins/pixiv-sync/backend/pixiv_sync/scan_items.py` | 清单条目构造：列表响应 → 待下载 item（关注与收藏共用） | `safe_int`、`extract_all_urls`、`build_item`、`uid_of`、`flatten_items`、`group_items` |
| `plugins/pixiv-sync/backend/pixiv_sync/bookmarks.py` | 收藏清单：`next_qs` 翻页断点与增量尾巴 | `collect_bookmarks_pending` |
| `plugins/pixiv-sync/backend/pixiv_sync/download.py` | 按清单并行下载、404 永久跳过、ugoira 分流、画师目录归属 | `download_pending`、`process_illust`、`process_ugoira`、`all_image_urls`、`_work_id`、`_safe_int`、`_safe_ext` |
| `plugins/pixiv-sync/backend/pixiv_sync/ugoira.py` | ugoira zip 帧序列 → 动画 WebP：帧时序解析与体积上限 | `parse_metadata`、`convert`、`_load_pil_image`、`DEFAULT_DELAY_MS`、`WEBP_QUALITY`、`MAX_FRAMES`、`MAX_FILE_BYTES`、`MAX_TOTAL_PIXELS` |
| `plugins/pixiv-sync/backend/pixiv_sync/oauth.py` | 内置 OAuth PKCE 向导：生成登录地址与用授权码换 token | `start`、`finish` |
| `plugins/pixiv-sync/tool/pixiv_purge_non_original.py` | 伴侣命令行工具：删除 1200px 缩放图并清除记录以便重下原图 | `THRESHOLD`、`ID_NAME_RE`、`CACHE_SUBDIR`、`image_size`、`scan_works`、`classify`、`prune_empty_dirs`、`main` |
| `plugins/pixiv-sync/tool/pixiv_unmark_recent.py` | 伴侣命令行工具：把已下载记录改回未下载（只改记录） | `ID_NAME_RE`、`CACHE_SUBDIR`、`collect_ids_in_window`、`collect_missing_ids`、`main` |
| `plugins/pixiv-sync/tool/cli_support.py` | 两个工具共用：下载根定位、原子改 JSON 记录、重置清单 done | `is_tty`、`data_root_from_json`、`data_root_from_yaml`、`resolve_root`、`strip_ids`、`reset_done_in_db`、`write_json` |
| `plugins/pixiv-sync/backend/libs/pixiv_mini.py` | 精简的 Pixiv 客户端（vendored 依赖，移植自 pixivpy 3.13.0，The Unlicense）：refresh_token 认证、关注新作、关注列表、用户作品、收藏、下载与翻页 | `PixivClient`、`PixivError`、`PixivCancelled`、`JsonDict`、`stop_event`、`set_stop_event`、`generate_pkce`、`login_url`、`auth`、`auth_with_code`、`set_auth`、`illust_follow`、`user_following`、`user_illusts`、`user_bookmarks_illust`、`illust_detail`、`ugoira_metadata`、`download`、`parse_qs` |
| `plugins/pixiv-sync/frontend/native.html` | 原生逻辑页骨架：无样式、无 chrome，只装载统一客户端与业务脚本 | `/shell/native-client.js`、`js/native.js` |
| `plugins/pixiv-sync/frontend/js/native.js` | 原生面板逻辑：拉状态、按文档式版式编译 schema blocks 与顶栏 toolbar、处理动作、按任务状态轮询 | `state`、`STATUS_IDLE_MS`、`STATUS_RUNNING_MS`、`STATUS_COOLDOWN_MS`、`ACTIVE_STATES`、`TASK_STATES`、`TASK_KINDS`、`client`、`render`、`buildBlocks`、`buildToolbar`、`signature`、`text`、`collect`、`metaText`、`statsBlock`、`alertBlocks`、`taskSection`、`actionsSection`、`accountSection`、`librarySection`、`verifySummary`、`verifyLibrary`、`cancelVerify`、`mainBlocks`、`failureBanner`、`pendingBlock`、`confirmBlock`、`oauthBlocks`、`loadStatus`、`schedulePoll`、`restartPoll`、`tick`、`sentNotice`、`dispose`、`init`、`startTask`、`refreshNow`、`cancelTask`、`retryFailed`、`startOauth`、`finishOauth`、`clearToken`、`openConfig`、`askRetryFailed`、`askLogout`、`handleAction`、`open-settings`、`settings-saved` |
| `plugins/pixiv-sync/frontend/index.html` | 旧壳回落页：内联 `<style>` 与内联 IIFE 脚本，承载状态栏、操作按钮、进度条、OAuth 弹窗 | 见「前端结构」一节 |

**落盘产物**：作品图片写在 `<root>/pixiv/` 下（多图入 `{作品id}/` 子文件夹，无画师信息的作品入 `未分类`），目录由 `artist.artist_dir` 解析、由 `download.download_pending` 落盘；缓存写在 `<root>/.cache/pixiv-sync/`（`CACHE_SUBDIR`）：`downloaded_ids.json`（`store.load_ids` / `store.save_ids`）、`failed_ids.json`（`store.load_failed_ids` / `store.save_failed_ids`）、`artists.json`（`artist.load_cache` / `artist.save_cache`）、`tasks.json`（`tasks.load_task` / `tasks.persist_task`）、`works.db`（`WorksDB`）、`selected_artists.txt`（`ArtistSelection.selected` / `ArtistSelection.open_config`）；壳的指纹库 `freshness.db` 由 `PluginBase.get_cache_dir` 指向同一目录。JSON 落盘统一走 `shell.backend.json_cache.write_json`（临时文件 + fsync + `os.replace`）。两个伴侣工具（`plugins/pixiv-sync/tool/`）直接改这些文件：共用的 `cli_support.strip_ids` / `cli_support.reset_done_in_db`。

## 后端 API 契约

`register_api` 暴露的方法如下表。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `get_status` | 无 | `{task, root_dir, token_configured, downloaded_total, running, selected_artists, selected_file, following_total, following_done, pending_following, bookmarks_total, bookmarks_done, pending_bookmarks, other_done, failed_skipped, rate_limit_cooldown, library}` | 状态轮询与界面刷新；`library` 是本地内容索引的投影（`busy` / `task` / `entries` / `dirs` / `last_verify_at` / `version_stale` / `stale_dirs`），面板据此画「已索引 N 项 · 上次校验 … · 索引待更新」
| `sync_following` | 无 | `{ok, data: {kind}}`，占用中返回 `{ok: false, error}` | 启动「同步画师」任务，内部先刷新关注清单再下载 |
| `sync_bookmarks` | 无 | `{ok, data: {kind}}`，占用中返回 `{ok: false, error}` | 启动「同步喜欢」任务，内部先刷新收藏清单再下载 |
| `refresh_following_lists` | 无 | `{ok}`，占用中返回 `{ok: false, error}` | 只刷新关注清单、不下载 |
| `refresh_bookmarks_lists` | 无 | `{ok}`，占用中返回 `{ok: false, error}` | 只刷新收藏清单、不下载 |
| `refresh_downloaded` | 无 | `{ok, total, imported, stale_removed, zero_removed, failed_cleared, incomplete, incomplete_skipped}` | 只导入：把手动放入的作品并入已下载记录（不重置 `done`）；面板不单独提供按钮，由下面的 `verify_downloaded` 覆盖 |
| `verify_downloaded` | 无 | `{ok, started, running}`，占用中返回 `{ok: false, error}` | **启动**本地内容校验（后台任务，立即返回）：导入、移除失效、清 0 字节与失败记录，并把缺页作品的 `done` 重置以便下次同步补齐。进度经 `get_status.library` 回传；旧壳无刷新基建时同步走盘并返回 `{finished: true, ...}` |
| `cancel_verify` | 无 | `{ok}` | 停止正在跑的本地内容校验 |
| `retry_failed` | 无 | `{ok, cleared, kind}`，占用中返回 `{ok: false, error}` | 清空 `failed_ids.json` 并按最近任务来源重新同步 |
| `cancel_task` | 无 | `{ok}` | 立刻取消：任务当场定稿为 `cancelled` 并落盘，不等后台线程；线程的写回随后被取消令牌挡住 |
| `open_config` | 无 | `{ok, file}`，失败返回 `{ok: false, error}` | 重写画师名单模板并打开其所在文件夹 |
| `start_oauth` | 无 | `{ok, url, challenge}`，失败返回 `{ok: false, error}` | OAuth 第一步：生成 PKCE 挑战并打开登录页 |
| `finish_oauth` | `code: str` | `{ok, user_id}`，失败返回 `{ok: false, error}` | OAuth 第二步：用授权码换 token 并回写 `refresh_token` |
| `get_settings` | 无 | `dict`，凭据类键返回掩码 | 继承 `PluginBase`，供壳设置弹窗回填 |
| `save_settings` | `settings: dict` | `{success, error}` | 继承 `PluginBase`，写入后触发 `on_settings_changed`；`refresh_token` 传空字符串即清除凭据 |

任务对象字段（`tasks.new_task`）：`kind`、`state`、`done`、`total`、`downloaded`、`skipped`、`failed`、`current`、`started_at`、`phase_started_at`、`finished_at`、`error`；`state` 取值为 `queued` / `running` / `done` / `failed` / `cancelled`，`tasks.load_task` 把残留的 `queued` / `running` 归一成 `paused`。`current` 是阶段文案（"正在登录 Pixiv…" / "正在更新关注作品清单…" / "已扫描画师 a/b 位"），由 `SyncRunner._set_phase` 写入并记日志（含上一阶段耗时）；线程一起来就置 `running`，`queued` 只覆盖线程启动前的瞬间。**同步任务不再做本地核对**（每次全盘遍历要几秒），手动放入的作品由 `verify_downloaded` 显式导入，缺页作品的 `done` 也由它重置。取消是"立刻"的：`SyncRunner.cancel_task` 当场把任务定稿为 `cancelled`、置位本次任务的取消令牌并落盘，`is_running` 随即变为 false；后台线程可能还卡在 socket 上，但它拿到令牌后的全部写回都被丢弃，等它自行退出即可（不需要杀进程）。每次任务一个新令牌，所以取消后可以立刻开始下一次同步，旧线程不会污染新任务。

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
| `get_data_root` | 无 | `Path` | 宿主 `image-viewer` 的成员，`CachePaths.root` 在未配置 `download_dir` 时取它的返回值 |
| `LocalLibrary.derive` | `items` | `{count}` | 壳引擎经 `freshness_spec` 的 `derive` 调用，累计本趟本地变化数 |
| `LocalLibrary.prune` | `keys` | `int` | 壳引擎经 `prune` 调用，累计被清理的键 |
| `LocalLibrary.audit` | `keys` | `{ok, total, imported, stale_removed, incomplete, incomplete_skipped, failed_cleared, zero_removed}` | 壳引擎经 `on_verified` 调用，按有效键集合重建记录 |
| `LocalLibrary.pass_end` | `report`、`verified: bool` | 同上，跳过时返回 `{skipped: 'incomplete'}` 或 `{skipped: 'unchanged'}` | 壳引擎经 `on_pass_end` 调用，整趟结束时重算 ids |
| `LocalLibrary.rebuild` | `kind: str` | 同上 | 壳引擎经 `rebuild` 调用，按磁盘重算记录 |

## 前端结构

原生逻辑页（`frontend/native.html`）不产生可见界面：

- `<body>` 只装载 `/shell/native-client.js` 与 `js/native.js`，不引入样式表、不建 DOM；壳的 `NativePanel` 负责可见界面，`SchemaRenderer` 渲染 blocks。
- 消息与生命周期：逻辑页装载 `/shell/native-client.js`，用 `NativeExtension.create({ onInit, onAction, onDispose })`；客户端统一处理 `omnibox:native-ready`（含重试）、`omnibox:native-init`、`omnibox:native-action`、`omnibox:schema` 与 pagehide / beforeunload 清理，本页只实现 `init`、`handleAction` 与 blocks 构造。
- 提交的 blocks 类型：`stats`（四格指标：已下载 / 关注待下 / 收藏待下 / 其他作品；`failed_skipped > 0` 时第 4 格换成失败计数）、`section`（同步任务 / 本地内容 / 操作 / 账号与维护，可嵌套）、`banner`（限流冷却、状态失败与可重来的就地确认）、`confirm`（清除 Token 的阻断确认，壳画成面板内模态层）、`code`（OAuth 登录地址，带复制）、`text`（状态行带 `dot` 语义点与说明，tone 取 `default` / `muted` / `danger`）、`empty`（错误态与未配置 Token 的空态，带 `icon`）、`progress`（同步任务与本地校验的进度，总数未知时 `indeterminate`，带取消/停止动作）、`actions`（两个同权 `accent` 同步按钮、失败时的 `link` 重试、账号区 `size: 'sm'` 的「校验本地内容」与 `danger` 的「清除 Token」）、`form`（仅 OAuth 授权码）。本地校验的进度来自 `get_status.library`（引擎后台任务），面板据此画「本地内容」进度块与「停止校验」。反馈分三档：成功与中性结果走消息级 `toast`（`render` 的第三个参数），失败与持续状态走常驻 `banner`（失败横幅带 `notice-dismiss`「知道了」），不可逆的「清除 Token」用 `confirm` 块。失败绝不走 Toast：它自己会消失，错误信息留不下来。提示与确认都不在逻辑页里画：它是隐藏 iframe，用户看不见。
- 版式：`render(blocks, toolbar, { layout: 'document' })` 声明文档式，壳据此把正文收窄到 960px 居中、组间 24px。页面结构固定为「状态行 → 指标 →（运行中或上次失败时）同步任务 →（校验在跑时）本地内容 → 操作 → 账号与维护 → 说明」；上次任务正常结束时任务区整块不出现，结束时间只留在状态行里。
- 两个同步方向同权：`同步关注作品` 与 `同步收藏作品` 都是 `accent` 档，不设页面主按钮；「只更新名单不下载」的独立入口已移除，名单刷新由同步任务内置完成。
- 动作标识：`reload`、`refresh-status`、`sync-following`、`sync-bookmarks`、`cancel-task`、`retry-failed`、`retry-failed-confirm`、`pending-cancel`、`notice-dismiss`、`verify-library`、`cancel-verify`、`oauth-start`、`oauth-restart`、`oauth-finish`、`oauth-close`、`logout`、`logout-confirm`、`confirm-cancel`、`open-settings`、`settings-saved`、`open-config`。
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

旧内嵌页的脚本装载顺序：`index.html` 的 `<head>` 内联脚本读 `location.search` 的 `embed` 参数，命中时给 `document.documentElement` 加 `is-embedded`；`</body>` 前的内联 IIFE 在 `DOMContentLoaded` 里依次执行 `mountFreshness`、各按钮的 `addEventListener`、`HostChannel.mountToolbar`、`refreshStatus`、`bindLifecycle`。壳注入的引导资源（`</head>` 之前，清单与顺序的唯一真源是 `shell/backend/file_server.py` 的 `_PLUGIN_BOOTSTRAP_SCRIPT`）：样式表先于脚本；共享运行时 9 段同作用域脚本按「`base.js` → `dialogs.js` → `settings.js` → `host-channel.js` → `tree.js` / `lightbox.js` / `pagination.js` / `context-menu.js` → `chrome-client.js`」的顺序执行，顺序即依赖顺序；随后是 `folder-picker` / `freshness` / `motion` / `splitter` / `content-ui` / `icons` 与 `Bridge.setPrefix('pixiv-sync')`。旧内嵌页的内联 `<style>` 排在壳样式之前；原生逻辑页没有自有样式表，只装载 `/shell/native-client.js` 与 `js/native.js`——壳注入的 `/shell/*.css` / `/shell/*.js` 仍会进它的文档（`Bridge.setPrefix` 需要），但本页不使用共享 UI 组件。

旧内嵌页动态渲染的短类：`dot ok` / `dot bad`、`active`（`#oauth-modal`）、`is-embedded`（`document.documentElement`）；`#psync-freshness` 内部节点由 `shell/frontend/public/shell/freshness.js` 拼出。

## 与壳的契约

- 原生内容：`shell/frontend/src/components/NativePanel.vue` 在宿主 `PluginChrome` 内容槽承载隐藏的逻辑 iframe（`omnibox:native-ready` / `omnibox:native-init` / `omnibox:native-action` / `omnibox:native-error`），`shell/frontend/src/components/SchemaRenderer.vue` 渲染 `omnibox:schema` 的 blocks；schema 类型见 `shell/frontend/src/core/plugin-content.ts`。`form` 的 `directory` 字段由 `SchemaRenderer` 调壳内 `FolderPicker`（`/shell/folder-picker.js`）选择，按字段的 `local_only` 决定是否提供「网络位置」入口（在壳文档里执行）。面板打开期间宿主侧栏/顶栏保留，内容 iframe 用 `v-show` 隐藏；标题与页面级动作由宿主顶栏负责，退出走导航或再次点击入口。
- 扩展声明：`get_extensions` 同时给 `native` 与 `embedUrl`；扩展入口现在由壳的 `PluginExtensions` 从 `system_get_plugin_extensions` 拉取并渲染。壳侧 `App.vue` 仍接收旧内嵌页发来的 `omnibox:open-native` 并挂载 `NativePanel`（同源 + 已挂载 iframe 来源、插件名形状与 URL 前缀三层校验），但共享运行时已不再提供发起它的入口（`renderExtensions` 已删除）。
- 设置归属：native toolbar 的 `open-settings` 由壳拦截（不下发到隐藏逻辑页），壳用 `nativePanel.host` + `nativePanel.plugin` 打开 `PluginSettingsModal`；弹窗按本插件 `settings_schema` 调 `get_settings_schema` / `get_settings` / `save_settings`。保存成功后壳关闭弹窗并向逻辑页发 `settings-saved`。`secret` 的清除由弹窗显式提交空串；`download_dir` 的目录字段由弹窗的 `FolderPicker` 目录行选择（`SettingsDirectoryField`），不再走系统对话框。
- 不读宿主：原生逻辑页不访问 `parent.imageViewer`、不读写宿主 DOM；后端只经 `Bridge.call` 调用，宿主能力不参与面板渲染。
- 壳类：`PluginBase`（`shell/backend/plugin_base.py`）。本插件覆写 `register_api`、`settings_schema`、`get_cache_dir`、`freshness_spec`、`get_extensions`、`on_unload`、`on_settings_changed`，使用继承的 `setting`、`update_setting`、`get_settings`、`save_settings`、`get_dependency`。
- 统一刷新基建：`engine_for`（`shell/backend/freshness.py`）返回的 `FreshnessEngine`，本插件经 `Maintenance` 调用其 `sync`、`verify`、`state`、`valid_keys`、`reset_throttle`，并经 `freshness_spec`（`LocalLibrary.spec`）交出 `LocalLibrary.derive`、`LocalLibrary.prune`、`LocalLibrary.audit`、`LocalLibrary.pass_end`、`LocalLibrary.rebuild`。
- 共享前端组件（旧回落页使用；原生逻辑页只调用壳注入的 `Bridge`）：`Bridge.call`、`Bridge.callSystem`、`Freshness.mount`、`HostChannel.requestSettings`、`HostChannel.onCallback`、`HostChannel.mountToolbar`、`PluginLifecycle.onShow` / `onHide` / `onDispose`、`openSettingsModal`、`Icons.html`、`Utils.escapeHtml`、`Toast.success` / `error` / `warning` / `info`。原生逻辑页只用 `Bridge.call` 与 `postMessage`，不挂共享 UI 组件。
- 壳 CSS 变量（`shell/frontend/public/shell/variables.css`）：`--text-primary`、`--text-secondary`、`--text-soft`（辅助文字，正文级对比度）、`--text-muted`、`--bg-hover`、`--bg-surface`、`--border`、`--accent`、`--danger`、`--success`、`--radius`、`--radius-sm`；原生面板不写 CSS，视觉全部由壳渲染。
- 共享样式类（旧回落页使用）：`.btn`、`.btn-sm`、`.btn-primary`、`.btn-danger`、`.field-help`、`.obx-icon`、`.obx-toolbar-btn`、`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.obx-scroll`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`。
- 宿主 `image-viewer` 的容器（旧回落页）：`#extension-frame` 承载内嵌页，`#extension-view-actions` 是 `HostChannel` 的 `host-toolbar` 容器；原生面板覆盖宿主内容槽，不再使用这些容器。
- 详见 `docs/plugin-ui-guide.md`。
