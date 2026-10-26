> group-mesh ｜ manifest: plugins/group-mesh/manifest.json

## 定位与形态

- 职责：在本机建立主体与设备密钥、用签名名单维护团体，把本机目录挂成共享项并由节点在 Noise 加密连接上对外提供，同时把团体成员共享项里的文件取回、物化或镜像到本地。
- 形态：独立页插件，路由 `frontend.route` 为 `/group-mesh`；manifest 声明 `frontend.chrome`（`brand`、`nav`、`toolbar`、`status`、`freshness`），品牌块、导航、顶栏、状态栏与侧栏分隔条由壳的 `PluginChrome.vue` 渲染。壳加载内容页时追加 `?content=1`，iframe 内只有五个面板与六个弹窗；声明里没有 `search`，壳不渲染搜索框。顶栏「设置」由壳的 `PluginSettingsModal` 打开，保存后内容页收到 `settings-saved`；统一刷新控件由壳挂载（`freshness.unit` 为「项」）。
- 同时是扩展提供方：`get_extensions()` 以 placement 值 `network-location` 注册，供 `/shell/folder-picker.js` 的目录组件发现并使用（`loadNetworkProviders` 按 placement 取提供方，`openNetworkPicker` 嵌入 `embedUrl` 并追加 `?embed=1`；`openDirBrowser` 负责本地目标目录）。壳的 `PluginSettingsModal` 目录字段（`SettingsDirectoryField`）与内容页 `openSettingsModal`（如 image-viewer）都走同一个 `FolderPicker`：字段未声明 `local_only` 时，目录行上的「网络位置」入口即发现本提供方；`local_only: true` 的字段不显示该入口。嵌入页为 `/plugins/group-mesh/frontend/network-location.html`。
- 复用方式：其它插件通过 `PluginBase.get_dependency('group-mesh')` 取本插件实例；`dependencies` 为空数组，本插件不依赖任何插件。

## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/group-mesh/manifest.json` | 插件声明与壳渲染的页面 chrome | `name`、`keepAlive`、`permissions`、`backend`、`frontend.route`、`frontend.chrome`（`brand`、`nav`、`toolbar`、`status`、`freshness`）、`icon`、`minShellVersion` |
| `plugins/group-mesh/backend/main.py` | 类骨架、设置 schema、API 注册、扩展注册、生命周期 | `GroupMeshPlugin`、`settings_schema`、`register_api`、`get_extensions`、`on_load`、`on_unload`、`DEFAULT_PORT`、`load_sibling`、`_common`、`_storage`、`_status`、`_group`、`_shares`、`_peers`、`_sync`、`_remote`、`_upload`、`_materialize`、`_mirror`、`_fetch`、`_node` |
| `plugins/group-mesh/backend/common.py` | 分片共用常量与纯函数 | `_opts`、`safe_rel`、`within_root`、`_mtime_ns`、`DEFAULT_PORT`、`AUTO_START_RETRY_SECONDS`、`DEFAULT_MAX_FETCH_BYTES`、`PROBE_TIMEOUT_SECONDS`、`PROBE_OVERALL_SECONDS`、`SYNC_OVERALL_SECONDS`、`UPLOAD_TIMEOUT_SECONDS`、`UPLOAD_KEEP`、`UPLOAD_TASK_FILE`、`MIRROR_MTIME_WINDOW_NS` |
| `plugins/group-mesh/backend/storage.py` | 路径属性、受保护路径、身份与名单的落盘 | `StorageMixin`、`get_data_root`、`identity_dir`、`cache_dir`、`downloads_dir`、`remote_cache_dir`、`staging_dir`、`get_protected_paths`、`_load_identity`、`_load_roster`、`_save_roster`、`_load_shares`、`_save_shares`、`_load_share_roots`、`_save_share_roots`、`_share_roots_file`、`_tree_usage`、`_roster_history`、`_record_roster_history`、`_load_roster_history` |
| `plugins/group-mesh/backend/status.py` | 首屏状态汇总 | `StatusMixin`、`get_status` |
| `plugins/group-mesh/backend/group.py` | 身份初始化、团体创建与加入、邀请串、成员增减 | `GroupMixin`、`init_identity`、`create_group`、`join_group`、`get_invite`、`get_device_keys`、`add_member`、`_invite_string` |
| `plugins/group-mesh/backend/shares.py` | 共享项挂载与移除、用量与条目数、统一刷新声明 | `ShareMixin`、`get_share_roots`、`add_share`、`remove_share`、`refresh_share_roots`、`freshness_spec`、`_root_state`、`_share_roots_report`、`_scan_share_usage`、`_count_entries`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild` |
| `plugins/group-mesh/backend/peers.py` | 对端设备与可读共享项、手工登记地址、本机地址串 | `PeerMixin`、`peers`、`my_endpoint`、`list_peers`、`_peers_file`、`_load_manual_peers`、`_save_manual_peers`、`_parse_endpoint`、`_peer_names`、`_device_names`、`_peer_endpoints`、`_pick_endpoint`、`_bootstrap_endpoints` |
| `plugins/group-mesh/backend/sync.py` | 注册表与名单的后台轮询与变更 push、出站连接池、候选端点 | `SyncMixin`、`get_sync_status`、`_sync_interval_seconds`、`_request_sync`、`_clear_sync_dirty`、`_start_sync`、`_stop_sync`、`_sync_loop`、`_sync_once`、`_sync_candidates`、`_push_pending_changes`、`_push_roster`、`_push_registry`、`_ensure_registry`、`_fetch_registries_parallel`、`_fetch_registry_from`、`_adopt_remote_roster`、`_connect`、`_release`、`_open_dedicated`、`_manual_endpoints`、`PROBE_TIMEOUT_SECONDS`、`PROBE_OVERALL_SECONDS`、`SYNC_OVERALL_SECONDS` |
| `plugins/group-mesh/backend/remote.py` | 列对端共享项与目录、取回单个文件 | `RemoteMixin`、`list_remote`、`download_remote`、`_fetch_to` |
| `plugins/group-mesh/backend/upload.py` | 上传任务、进度与取消、续传记录 | `UploadMixin`、`upload_remote`、`upload_status`、`cancel_upload`、`_register_upload`、`_prune_uploads`、`_persist_uploads`、`_restore_uploads`、`_upload_view`、`_cancel_all_uploads`、`_upload_task_file`、`_upload_record_file`、`_upload_key`、`_resume_offset`、`_upload_worker`、`UPLOAD_KEEP`、`UPLOAD_TASK_FILE`、`UPLOAD_TIMEOUT_SECONDS` |
| `plugins/group-mesh/backend/materialize.py` | 远端目录物化、索引与对账 | `MaterializeMixin`、`materialize_remote`、`_remote_index_file`、`_load_remote_index`、`_cached_remote_index`、`_invalidate_remote_index`、`_save_remote_index`、`_walk_remote`、`_prune_absent`、`_content_changed`、`_is_fetched`、`_apply_remote_mtime` |
| `plugins/group-mesh/backend/mirror.py` | 把共享项完整取到用户指定目录 | `MirrorMixin`、`mirror_share`、`_mirror_files`、`_mirror_file_current`、`MIRROR_MTIME_WINDOW_NS` |
| `plugins/group-mesh/backend/fetch.py` | 占位判定、按需取字节、远端缓存用量与清理 | `FetchMixin`、`is_content_placeholder`、`ensure_file`、`max_fetch_bytes`、`remote_cache`、`clear_remote_cache`、`_fetch_lock`、`_discard_staging`、`DEFAULT_MAX_FETCH_BYTES` |
| `plugins/group-mesh/backend/node.py` | 共享节点启停、注册发布、监听失败提示 | `NodeMixin`、`start_node`、`stop_node`、`get_node_status`、`_publish_registration`、`_refresh_own_registration`、`_republish_registration`、`_auto_start_node`、`_listen_error`、`_close_connections`、`on_settings_changed`、`DEFAULT_PORT`、`AUTO_START_RETRY_SECONDS` |
| `plugins/group-mesh/frontend/index.html` | 内容页入口：五个面板、六个弹窗与脚本装载（页面骨架由壳渲染，不在这里） | `#gm-panels`、`#gm-content`、`#kernel-missing`、`data-panel`、`data-title`、`data-sub`、`data-active` |
| `plugins/group-mesh/frontend/network-location.html` | 网络位置提供方页面（弹窗 iframe） | `nl-peers`、`nl-shares`、`nl-dest-input`、`nl-dest`、`nl-note`、`nl-pick`、`nl-run`、`nl-cancel`、`nl-item`、`nl-sub`、`nl-path`、`nl-step`、`nl-list`、`nl-actions`、`nl-pick-row`、`is-active`、`MESSAGE_TYPE`、`omnibox:network-location`、`loadPeers`、`selectPeer`、`selectShare`、`pickDestination`、`run`、`reply`、`destinationProblem`、`syncFromInput`、`normalizeDestination`、`applyPickedDestination`、`updateHint`、`updateRunButton`、`FolderPicker.openDirBrowser` |
| `plugins/group-mesh/frontend/group-mesh.css` | 插件自有样式：面板栅格、卡片、表格、徽章、远端两栏 | `--gm-radius`、`--gm-radius-sm`、`--gm-accent-soft`、`--gm-success-soft`、`--gm-warn-soft`、`--gm-danger-soft`、`--gm-peers-width`、`.gm-content`、`.gm-panel`、`.gm-grid`、`.gm-card`、`.gm-card-ok`、`.gm-card-warn`、`.gm-card-error`、`.gm-card-muted`、`.gm-card-wide`、`.gm-body`、`.gm-kv`、`.gm-actions`、`.gm-form`、`.gm-table`、`.gm-badge`、`.gm-badge-owner`、`.gm-badge-admin`、`.gm-badge-member`、`.gm-badge-ok`、`.gm-badge-warn`、`.gm-hint`、`.gm-empty`、`.gm-skeleton-line`、`.gm-list`、`.gm-details`、`.gm-address`、`.gm-address-row`、`.gm-address-label`、`.gm-address-value`、`.gm-remote`、`.gm-remote-tools`、`.gm-remote-peers`、`.gm-remote-files`、`.gm-remote-head`、`.gm-remote-peer`、`.gm-remote-peer-head`、`.gm-remote-peer-name`、`.gm-remote-peer-ep`、`.gm-remote-row`、`.gm-remote-item`、`.gm-remote-item-active`、`.gm-remote-badge`、`.gm-remote-note`、`.gm-remote-crumb`、`.gm-remote-progress`、`.gm-remote-progress-error`、`.gm-remote-cache`、`.gm-upload-progress`、`.gm-check`、`.gm-toast`、`.gm-toast-error`、`.gm-banner`、`.gm-banner-error` |
| `plugins/group-mesh/frontend/js/app.js` | 状态读取、面板切换、五个面板渲染、弹窗与动作绑定 | `state`、`el`、`call`、`toast`、`escapeHtml`、`stagger`、`cardOf`、`openModal`、`closeModal`、`roleBadge`、`shortKey`、`button`、`fillActions`、`setPanel`、`render`、`protectionBadge`、`renderIdentity`、`renderRoster`、`renderShares`、`renderNode`、`renderUnsupported`、`renderSideSummary`、`renderLoading`、`showError`、`afterAction`、`refresh`、`showInvite`、`bind`、`init` |
| `plugins/group-mesh/frontend/js/remote.js` | 「远端共享」分片的取数与渲染、上传轮询、可见性轮询 | `window.GroupMeshRemote`、`state`、`gmIcon`、`el`、`esc`、`renderPeers`、`renderPath`、`renderEntries`、`formatSize`、`renderProgress`、`refreshMyEndpoint`、`copyMyEndpoint`、`updatePeerEndpoint`、`refreshPeers`、`reportPeers`、`refreshCache`、`renderCacheSummary`、`materialize`、`listDirectory`、`download`、`openUpload`、`setUploadRunning`、`renderUploadProgress`、`uploadText`、`stopUploadPolling`、`pollUpload`、`doUpload`、`cancelUpload`、`bind`、`startViewPolling`、`stopViewPolling`、`bindLifecycle`、`init`、`VIEW_POLL_MS`、`viewTimer` |

落盘产物（全部在 `get_data_root()` 值为 `<全局数据根>/group-mesh` 之下）：

| 路径 | 读写符号 |
| --- | --- |
| `identity/principal.json`、`identity/` 下的设备密钥文件 | `_load_identity`、`Identity.init`、`Identity.load`、`get_protected_paths` |
| `identity/roster.json` | `_load_roster`、`_save_roster` |
| `identity/roster-history.json` | `_load_roster_history`、`_record_roster_history`、`RosterHistory` |
| `identity/shares.json` | `_load_shares`、`_save_shares` |
| `identity/share_roots.json` | `_load_share_roots`、`_save_share_roots`、`_share_roots_file` |
| `identity/registry.json` | `_ensure_registry`、`_publish_registration`、`_fetch_registry_from` |
| `identity/state.json` | `_refresh_own_registration`、`_publish_registration` |
| `downloads/`（`download_dir` 设置项可改） | `downloads_dir`、`download_remote`、`_fetch_to` |
| `.cache/remote/<设备ID>/<共享标识>/` 与 `<共享标识>.index.json` | `remote_cache_dir`、`_walk_remote`、`_save_remote_index`、`ensure_file` |
| `.cache/staging/<设备ID>/<共享标识>/` | `staging_dir`、`_fetch_to`、`_discard_staging`、`clear_remote_cache` |
| `.cache/peers.json` | `_peers_file`、`_load_manual_peers`、`_save_manual_peers` |
| `upload-tasks.json` | `_upload_task_file`、`_persist_uploads`、`_restore_uploads` |
| `uploads.json` | `_upload_record_file`、`_load_upload_records`、`_save_upload_record`、`_clear_upload_record` |

## 后端 API 契约

对外方法（`register_api` 暴露，调用形态为 `Bridge.call('<方法>', args)`）：

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `get_status` | 无 | `kernel`（`available`、`version`、`package`、`path`）、`data_root`、`identity_dir`、`locations`（`identity`、`downloads`、`downloads_custom`、`cache`、`remote_cache`）、`settings`（`port`、`bind`、`group_name`、`principal_name`、`download_dir`、`sync_interval_seconds`）、`node`、`sync`、`identity`、`roster`、`shares`、`share_roots`、`unsupported` | 首屏状态汇总；必要时自动启动节点 |
| `init_identity` | `name` | `success`、`identity`（`name`、`id`、`device_id`） | 创建主体与首台设备 |
| `get_device_keys` | 无 | `success`、`principal`、`device`、`name` | 导出本机公钥供群主登记 |
| `create_group` | `group` | `success`、`group`、`version`、`invite` | 创建团体并成为群主 |
| `join_group` | `invite` | `success`、`updated`、`previous_version`、`group`、`version`、`role`、`in_roster`、`warning` | 用邀请串加入团体或更新名单 |
| `get_invite` | 无 | `success`、`invite`、`group`、`version` | 导出当前名单的邀请串 |
| `add_member` | `principal`、`device`、`name` | `success`、`action`、`version`、`invite` | 群主或管理员增添成员与设备 |
| `add_share` | `share_id`、`path`、`read`、`write`、`max_bytes` | `success`、`share_id`、`path`、`acl`、`max_bytes` | 挂载本机共享项 |
| `remove_share` | `share_id` | `success`、`share_id` | 取消本机共享项 |
| `refresh_share_roots` | 无 | `success`、`roots`、`scanned_at`、`action`、`changed` | 重扫共享根，返回用量与条目数 |
| `peers` | `action`（`list`、`add`、`update`、`remove`）、`endpoint`、`name`、`device_id` | `success`、`peers`、`existed`、`message`、`replaced`、`removed` | 手工登记、更新或移除对端地址 |
| `my_endpoint` | 无 | `success`、`text`、`endpoints`、`device_id`、`name`、`running`、`published` | 生成可粘贴给对方的本机地址串 |
| `list_peers` | `refresh` | `success`、`peers`（`device_id`、`name`、`endpoints`、`endpoint`、`shares`、`seq`、`last_seen`、`online`、`note`）、`errors`、`registry_enabled`、`registry_size`、`roster_versions`、`roster_adopted` | 列出对端设备与可读共享项 |
| `list_remote` | `device_id`、`share_id`、`path` | `success`、`device_id`、`peer_device_id`、`name`、`endpoint`、`shares`（`share_id` 为空时）或 `share_id`、`path`、`dir`、`entries` | 列对端共享项或目录 |
| `download_remote` | `device_id`、`share_id`、`path`、`overwrite` | `success`、`device_id`、`peer_device_id`、`name`、`share_id`、`path`、`local_path`、`bytes`、`size`、`skipped`、`reason`、`offline` | 取回对端一个文件到下载目录 |
| `upload_remote` | `device_id`、`share_id`、`local_path`、`remote_path`、`overwrite` | `success`、`task_id`、`state`、`size`、`share_id`、`path`、`local_path` | 启动一次后台上传 |
| `upload_status` | `task_id` | `success`、`task`（`task_id`、`state`、`device_id`、`share_id`、`local_path`、`remote_path`、`overwrite`、`total`、`sent`、`resumed_from`、`peer_device_id`、`peer_name`、`error`、`started_at`、`finished_at`、`percent`）或 `tasks` | 读上传进度或全部任务 |
| `cancel_upload` | `task_id` | `success`、`task`、`state`、`error` | 在分块边界取消上传 |
| `materialize_remote` | `device_id`、`share_id`、`path`、`max_depth`、`max_entries` | `success`、`device_id`、`name`、`share_id`、`root`、`dirs`、`files`、`invalidated`、`removed`、`entries`、`truncated`、`offline` | 把对端目录物化为本地占位树与索引 |
| `mirror_share` | `device_id`、`share_id`、`destination`、`max_entries`、`max_bytes` | `success`、`device_id`、`name`、`share_id`、`root`、`entries`、`truncated`、`dirs`、`fetched`、`unchanged`、`bytes`、`truncated_bytes`、`errors`、`error_count`、`offline` | 把共享项完整取到用户指定目录 |
| `remote_cache` | 无 | `success`、`root`、`items`（`device_id`、`share_id`、`entries`、`files`、`fetched`、`pending`、`bytes`、`materialized_at`、`truncated`、`root`）、`total_bytes` | 读远端物化缓存用量 |
| `clear_remote_cache` | `device_id`、`share_id` | `success`、`removed`、`freed_bytes` | 清理物化缓存与暂存目录 |
| `start_node` | 无 | `success`、`node`、`error` | 启动共享节点监听 |
| `stop_node` | 无 | `success`、`node`、`error` | 停止监听并等待线程退出 |
| `get_node_status` | 无 | `running`、`listening`、`connection_count`、`error`、`published_seq`、`published`、`auto_start_error` | 读节点状态 |
| `get_settings` | 无 | 设置键值 | 壳的统一设置弹窗读取 |
| `save_settings` | 设置键值 | `success`、`error` | 壳的统一设置弹窗落盘 |

入参形态：上面除无参方法外都接受一个 `opts` 字典，或同名的逐个位置参数（由 `_opts` 归一）。

壳按名调用的成员：

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `is_content_placeholder` | `path` | `bool` | 判定物化占位文件，决定是否回调 `ensure_file` |
| `ensure_file` | `path` | `None` | 读取占位文件时按需取回真字节 |
| `get_protected_paths` | 无 | `List[Path]` | 申报 `identity_dir` 为受保护路径 |
| `get_extensions` | 无 | `List[dict]` | 注册 placement 值 `network-location` 的扩展 |
| `freshness_spec` | 无 | `Dict`（`roots`、`include`、`derive`、`prune`、`on_verified`、`on_pass_end`、`rebuild`、`content_version`、`unit`、`min_sync_interval`、`max_dirs_per_sync`） | 参与统一刷新基建 |
| `on_settings_changed` | `changed_keys` | `None` | 设置变更后丢弃连接与自动启动结论 |
| `on_load` | 无 | `None` | 建数据根、恢复上传任务表、启动后台同步 |
| `on_unload` | 无 | `None` | 停同步、取消上传、关连接、停节点 |
| `get_data_root` | 无 | `Path` | 插件数据目录 |
| `cache_dir` | 无 | `Path` | 可重建派生数据目录 |
| `downloads_dir` | 无 | `Path` | 取回文件的落点 |

## 前端结构

壳渲染的 chrome（`manifest.json` 的 `frontend.chrome`，由 `PluginChrome.vue` 落 DOM）：

- 品牌块：`icon:network`、标题「团体组网」、副标题「无中心 · IPv6」、`accent` 为 `#22d3ee`。
- 导航四组：`本机`（`machine` 身份与节点）、`团体`（`group` 名单与成员、`shares` 共享项）、`传输`（`remote` 远端共享）、`关于`（`roadmap` 路线图）。
- 顶栏动作：`refresh`（刷新状态）、`settings`（设置）；设置由壳的 `PluginSettingsModal` 打开。
- 搜索：manifest 没有 `search` 声明，壳不渲染搜索框，内容页也不处理 `omnibox:query`。
- 状态栏与统一刷新：`status: true`，文本由内容页按节点/团体/名单状态上报；统一刷新由壳挂载，`freshness.unit` 为「项」，校验完成后经 `refresh` 动作重读。

DOM 结构树（`plugins/group-mesh/frontend/index.html`，内容 iframe 不含页面骨架）：

- `#app` → `.view-body.gm-main`
  - `#kernel-missing.gm-banner.gm-banner-error`
  - `.view-content.gm-content.obx-scroll#gm-content` → `.gm-panels.obx-stagger#gm-panels`
    - `section.gm-panel#panel-machine[data-panel="machine"]` → `.gm-grid` → `section.gm-card.obx-card-lift`（`#identity-body`、`#identity-actions`）与 `section.gm-card.obx-card-lift`（`#node-body`、`#node-actions`）
    - `section.gm-panel#panel-group[data-panel="group"]` → `section.gm-card`（`#roster-body`、`#roster-actions`）
    - `section.gm-panel#panel-shares[data-panel="shares"]` → `section.gm-card`（`#shares-body`、`.gm-form`、`#share-id`、`#share-path`、`#share-write`、`#btn-add-share`）
    - `section.gm-panel#panel-remote[data-panel="remote"]` → `section.gm-card`（`.gm-hint`、`.gm-address`、`.gm-address-row`、`.gm-address-label`、`#my-endpoint`、`#btn-copy-my-endpoint`、`#peer-endpoint`、`#peer-device`、`#btn-update-peer`、`.gm-remote-tools`、`#btn-peer-add`、`#remote-cache`、`.gm-remote`、`.gm-remote-peers.obx-scroll`、`.gm-remote-head`、`#peers-body`、`.gm-remote-files.obx-scroll`、`#remote-path`、`#remote-body`、`#remote-progress`）
    - `section.gm-panel#panel-roadmap[data-panel="roadmap"]` → `section.gm-card` → `.gm-details` → `#unsupported-list.gm-list`
- `#create-box.modal` → `.modal-box.obx-anim-scale` → `.modal-body`（`#create-group-name`）、`.modal-footer`（`#btn-close-create`、`#btn-do-create`）
- `#invite-box.modal` → `.modal-body`（`#invite-text`）、`.modal-footer`（`#btn-close-invite`、`#btn-copy-invite`）
- `#join-box.modal` → `.modal-body`（`#join-text`）、`.modal-footer`（`#btn-close-join`、`#btn-do-join`）
- `#member-box.modal` → `.modal-body`（`#member-principal`、`#member-device`、`#member-name`）、`.modal-footer`（`#btn-close-member`、`#btn-do-add-member`）
- `#upload-box.modal` → `.modal-body`（`#upload-target`、`#upload-local`、`#upload-name`、`#upload-overwrite`、`#upload-progress`）、`.modal-footer`（`#btn-close-upload`、`#btn-cancel-upload`、`#btn-do-upload`）
- `#peer-box.modal` → `.modal-body`（`#peer-manual-endpoint`、`#peer-name`）、`.modal-footer`（`#btn-close-peer`、`#btn-do-add-peer`）

动态渲染的 id：`#btn-remote-refresh`（由 `renderPath` 写入）、`#btn-remote-up`、`#btn-remote-upload`、`#btn-clear-cache`。

网络位置页 `plugins/group-mesh/frontend/network-location.html`：

- `.nl-step` → `.nl-list#nl-peers`、`.nl-list#nl-shares`
- `.nl-step` → `.nl-pick-row`（`#nl-dest-input`、`#nl-pick`）、`.nl-path#nl-dest`
- `.nl-actions` → `#nl-run`、`#nl-cancel`
- `.nl-note#nl-note`

脚本装载顺序（`index.html` 末尾，两条都是同步脚本，顺序是硬约束）：

1. `js/remote.js`：定义 `window.GroupMeshRemote`，装载期不触碰 DOM
2. `js/app.js`：`init` 在 `DOMContentLoaded` 里调用 `GroupMeshRemote.init(deps)` 注入 `call`、`toast`、`escapeHtml`、`openModal`、`closeModal`、`onPeersLoaded`

动态渲染的类名清单（由脚本拼接，静态搜索 `.gm-*` 规则时看不到）：

- `gm-toast`、`gm-toast-error`：`toast` 的脱离壳回退节点
- `gm-card-ok`、`gm-card-warn`、`gm-card-error`、`gm-card-muted`：`cardOf` 给卡片加的顶边色修饰
- `gm-badge`、`gm-badge-ok`、`gm-badge-warn`、`gm-badge-owner`、`gm-badge-admin`、`gm-badge-member`：`roleBadge` 与 `protectionBadge` 生成的角色与保护状态徽章
- `gm-skeleton-line`：`renderLoading` 的首屏骨架
- `gm-remote-item-active`、`gm-remote-progress-error`：远端分片的选中态与错误态
- `data-state` 取值 `ok`、`warn`、`error`、`idle`：`renderSideSummary` 写进 `state.statusState` 并经 `omnibox:chrome` 上报，壳据此画状态点
- `data-remove`、`data-enter`、`data-download`、`data-materialize`、`data-device`、`data-share`：列表项上的动作标记，由 `bind` 与 `renderEntries` 的事件委托读取

## 与壳的契约

- **chrome 消息**（只在壳与内容 iframe 之间，同源 `postMessage`）：
  - 壳 → 内容 `omnibox:view`：导航项 id（`machine` / `group` / `shares` / `remote` / `roadmap`），内容页 `setPanel` 切面板。
  - 壳 → 内容 `omnibox:action`：`refresh` 重读 `get_status`；`settings-saved` 在壳内设置弹窗保存成功后重读状态。设置弹窗由壳的 `PluginSettingsModal` 打开，壳不下发 `settings`。
  - 内容 → 壳 `omnibox:ready`：握手。
  - 内容 → 壳 `omnibox:chrome`：`title`、`subtitle`、`badges`（`shares` / `remote` 计数）、`status`（文本与 `ok` / `warn` / `error` / `idle`）。本插件不声明搜索，也不上报 `view` / `nav` / `actions`；导航高亮由壳维护。
- 内容页不得出现 chrome 标记：`.view-sub-sidebar`、`.view-toolbar`、`.sub-sidebar-brand`、`.obx-nav-item`（`tools/plugin_ui_audit.py` 的 `iframe.content-only` 会检查）。侧栏分隔条由壳持有，本页不含 `data-obx-splitter*`。

壳类（`shell/frontend/public/shell/base.css`，内容 iframe 实际使用的）：`.view-body`、`.view-content`、`.modal`、`.modal.active`、`.empty-state--inline`、`.btn-sm`、`.btn-danger`。

壳 token（`shell/frontend/public/shell/variables.css`）：`--bg-app`、`--bg-surface`、`--bg-hover`、`--border`、`--accent`、`--success`、`--warning`、`--danger`、`--danger-soft`、`--text-primary`、`--text-secondary`、`--text-muted`、`--text-on-accent`、`--radius`、`--radius-sm`、`--radius-lg`、`--shadow-md`、`--transition-fast`、`--sub-sidebar-width`。

壳动效（`shell/frontend/public/shell/effects.css`）：`.obx-glass`、`.obx-scroll`、`.obx-card-lift`、`.obx-anim-scale`、`.obx-stagger`、`.obx-skeleton`、`--obx-ease`、`--obx-shadow-2`、`--obx-i`。

插件自有 CSS 变量前缀：`--gm-*`（`--gm-radius`、`--gm-radius-sm`、`--gm-accent-soft`、`--gm-success-soft`、`--gm-warn-soft`、`--gm-danger-soft`、`--gm-peers-width`）。

共享前端组件：

- `window.Bridge.call`：`/api/<插件>__<方法>` 通道
- 设置弹窗：由壳的 `PluginSettingsModal` 在顶栏 `settings` 动作里打开；保存成功后壳发 `settings-saved`，本页 `refresh()` 重读状态。内容页不再调用 `window.openSettingsModal`
- 统一刷新：壳的 `PluginChrome` 用 `window.Freshness.mount({plugin, container, unit, onChange})` 挂载同步/校验按钮，完成后经 `refresh` 动作回到本页；内容页不再挂 `#gm-freshness`
- `window.confirmDialog`：移除共享项前的确认
- `window.Toast.success`、`window.Toast.error`：提示
- `window.Icons.html`：取壳图标集里的 SVG 标记，`icons.generated.js` 提供 sprite
- `window.Utils.escapeHtml`：网络位置页的转义
- `window.Motion.stagger`：面板卡片的交错入场
- `window.onShow`、`window.onHide`、`window.onDispose`：远端分片的轮询开关
- `window.FolderPicker.openDirBrowser`：网络位置页的本地目录选择

共享后端基建（`shell/backend/`）：

- `PluginBase`（`plugins/group-mesh/backend/main.py` 的基类）：提供 `setting`、`get_settings`、`save_settings`、`get_protected_paths`、`current_principal`、`require_principal`、`get_dependency`
- `load_sibling`（`shell/backend/plugin_utils.py`）：加载 12 个后端分片
- `engine_for`（`shell/backend/freshness.py`）：`refresh_share_roots` 与 `freshness_spec` 依赖的刷新引擎

协议内核（`shell/groupmesh/`，独立于 Flask 与插件目录）：`GroupMeshPlugin` 使用 `shell.groupmesh.identity` 的 `Identity`、`shell.groupmesh.roster` 的 `Roster`、`RosterEntry`、`founding_roster`、`next_roster`、`staleness_report`、`shell.groupmesh.records` 的 `RecordError`、`shell.groupmesh.shares` 的 `Acl`、`LocalShare`、`new_share`、`shell.groupmesh.registry`、`shell.groupmesh.secret_store`、`shell.groupmesh.roster_history` 的 `RosterHistory`、`shell.groupmesh.client` 作为 `mesh_client`、`shell.groupmesh.transport` 的 `RemoteError` 与 `TransportError`。

界面与交互的整体约定见 `docs/plugin-ui-guide.md`。
