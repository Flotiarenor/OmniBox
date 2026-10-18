> media-player ｜ manifest: plugins/media-player/manifest.json

## 定位与形态

- 职责：统一音频与视频媒体库：扫描媒体文件夹建立单一索引，按专辑 / 目录聚合后以列表、专辑卡与详情页浏览，并在舞台 + 播放栏内播放，附带混编歌单、歌词沉浸页、均衡器与网易云在线视图。
- 形态：独立页插件，`frontend.route` = `/media-player`。
- 宿主能力：侧栏容器 `#mp-extensions` 经壳的 `renderExtensions` 承载 `netease-music` 的扩展入口；本插件不内嵌其他插件 iframe。


## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/media-player/manifest.json` | 插件元信息 | `name`、`backend.entry`、`backend.class`、`frontend.entry`、`frontend.route`、`keepAlive`、`permissions` |
| `plugins/media-player/backend/main.py` | 类骨架、设置 schema、API 注册、索引与用户状态落盘、封面库接入、ffmpeg 配置、EQ 预设读写 | `MediaPlayerPlugin`、`settings_schema`、`register_api`、`freshness_spec`、`get_data_root`、`get_file_roots`、`get_thumb_data`、`on_settings_changed`、`on_unload`、`_state_dir`、`_adopt_orphan_state`、`_publish_items`、`_load_index`、`_save_index`、`_load_state`、`_save_state`、`_replace_with_retry`、`_migrate_legacy_state`、`_configured_roots`、`_media_dirs`、`_restore_scan_task`、`INDEX_VERSION`、`QUEUE_PERSIST_LIMIT` |
| `plugins/media-player/backend/models.py` | 条目与专辑数据类 | `MediaItem`、`MediaAlbum`、`to_dict`、`from_cache`、`count`、`total_duration` |
| `plugins/media-player/backend/scanner.py` | 扩展名集合与解析规则版本、条目构造、专辑键、封面路径查找、封面字节生成器 | `INDEX_VERSION`、`AUDIO_EXTS`、`VIDEO_EXTS`、`MEDIA_EXTS`、`COVER_NAMES`、`cover_generator`、`item_id`、`find_cover`、`build_item`、`_audio_album_key`、`_video_album_key` |
| `plugins/media-player/backend/metadata.py` | 音频标签读取与内嵌 / 目录封面字节提取 | `MetadataReader`、`read`、`debug_meta`、`extract_cover_bytes`、`has_embedded_cover`、`_read_with_mutagen`、`_read_by_filename`、`_extract_embedded_cover`、`_cover_from_id3`、`_cover_from_flac`、`_cover_from_mp4`、`_find_folder_cover`、`_parse_track`、`_id3_text`、`detect_image_mime`、`SUPPORTED_EXTS`、`HAS_MUTAGEN` |
| `plugins/media-player/backend/video_meta.py` | 视频时长探测 | `probe_duration` |
| `plugins/media-player/backend/video_ffmpeg.py` | ffmpeg 定位与抽帧通道 | `configure`、`find_ffmpeg`、`extract_frame`、`SEEK_DEFAULT`、`SEEK_MIN`、`SEEK_MAX`、`FRAME_TIMEOUT` |
| `plugins/media-player/backend/freshness.py` | 统一刷新基建的插件侧声明与钩子 | `FreshnessMixin`、`freshness_spec`、`COVER_KEY_PREFIX`、`_freshness_key`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild`、`_refresh_covers`、`_namespace_for`、`_root_for`、`_rel_dir_for` |
| `plugins/media-player/eq-presets/*.json` | 内置均衡器预设（`name`、`bands`） | `name`、`bands` |
| `plugins/media-player/tool/library_tool.py` | 库外维护脚本：专辑标签、重复文件、歌词对照 | `main`、`cmd_albums`、`cmd_dups`、`cmd_lrc`、`selftest`、`resolve_library`、`load_cache`、`build_plan`、`write_tags`、`find_dups`、`scan_lyrics`、`LRC_QUARANTINE` |
| `plugins/media-player/frontend/index.html` | 页面骨架与脚本装载顺序 | `#app`、`.mp-sidebar`、`#mp-stage`、`#player-bar`、`#media-content`、`#lyrics-page`、`#queue-popup`、`#eq-panel`、`#modal-playlist`、`#modal-add-to-playlist`、`#modal-eq-name` |
| `plugins/media-player/frontend/media-player.css` | 全部样式与自有 CSS 变量 | `--mp-stage-vh`、`--mp-pb-height`、`--mp-accent-soft`、`--mp-accent-2`、`--mp-glass`、`--mp-glass-strong`、`--mp-gradient`、`--mp-shadow-1`、`--mp-shadow-2`、`--mp-radius`、`--mp-radius-sm`、`--mp-pop-bottom`、`--range-val`、`.mp-stage`、`.mp-playerbar`、`.mp-card`、`.mp-row`、`.mp-lyrics-page`、`.mp-modal`、`.mp-context-menu` |
| `plugins/media-player/frontend/js/utils.js` | 工具层：URL、封面降级、图标、格式化、弹窗开关、网易云 ↔ 本地匹配、音量映射 | `MPUtils`、`formatTime`、`debounce`、`escapeHtml`、`itemIcon`、`mediaUrl`、`coverUrl`、`coverSrc`、`coverImg`、`fallbackCover`、`heroBg`、`icon`、`timeAgo`、`fmtDuration`、`setRangePercent`、`openModal`、`closeModal`、`normNcmText`、`ncmArtistTokens`、`matchNeteaseToLocal`、`VolumeMapper`、`linearToActual`、`actualToLinear`、`MPCoverFail` |
| `plugins/media-player/frontend/js/frame-extractor.js` | 视频封面前端 canvas 抽帧队列 | `MediaFrameExtractor`、`request`、`pump`、`extractOne`、`extractOnce`、`pickSeekPositions`、`frameIsBlack`、`mainPlayerFor`、`isBlocked`、`settle` |
| `plugins/media-player/frontend/js/lyrics-parser.js` | LRC 解析与歌词沉浸页、canvas 频谱 | `LyricsParser`、`parse`、`MediaLyrics`、`applyStyleVars`、`applyBg`、`loadForItem`、`update`、`_findLineIndex`、`_render`、`_highlightLine`、`_startViz`、`_stopViz`、`_drawViz`、`show`、`hide`、`toggle`、`isVisible`、`suspend`、`resume` |
| `plugins/media-player/frontend/js/progress-store.js` | 播放进度的 localStorage 记忆与旧键迁移 | `MediaProgressStore`、`STORAGE_KEY`、`LEGACY_KEYS`、`get`、`save`、`clear`、`_legacyValue` |
| `plugins/media-player/frontend/js/player-core.js` | 播放内核：队列、模式、音视频元素切换、进度保存、Web Audio EQ 链 | `MediaPlayerCore`、`setQueue`、`playIndex`、`playItem`、`_loadItem`、`next`、`prev`、`stop`、`seekTo`、`seekDelta`、`cyclePlayMode`、`togglePlay`、`toggleMute`、`setVideoMode`、`setResumeMode`、`_buildShuffleOrder`、`_stepShuffle`、`_syncShuffleCursor`、`_saveProgress`、`_startProgressSaver`、`_stopProgressSaver`、`saveQueueState`、`restorePlayback`、`ensureAudioGraph`、`getAnalyser`、`setEqBand`、`getEqBands`、`applyEqBands`、`resetEq`、`EQ_FREQS` |
| `plugins/media-player/frontend/js/playlist-manager.js` | 歌单 CRUD 与侧栏渲染 | `MediaPlaylistManager`、`load`、`create`、`rename`、`delete`、`get`、`addItems`、`removeItem`、`updateItemIds`、`renderSidebar` |
| `plugins/media-player/frontend/js/app.js` | `MediaPlayerApp` 类骨架：构造、初始化、生命周期、刷新基建挂载、网易云视图切换 | `MediaPlayerApp`、`init`、`_bindPluginLifecycle`、`_ensureIndex`、`_mountFreshness`、`_autoSyncFreshness`、`_onFreshnessChanged`、`_restorePlayback`、`_updateStats`、`loadExtensions`、`openNeteaseView` |
| `plugins/media-player/frontend/js/app-ui-events.js` | UI 事件绑定、舞台/文档点击、视频封面预取观察 | `_bindUI`、`_onStageClick`、`_onDocumentClick`、`_isClickInside`、`_bindThumbPrefetch`、`_observeThumbImgs` |
| `plugins/media-player/frontend/js/app-views.js` | 视图切换、导航高亮、设置弹窗、网易云视图数据与缓存 | `_doScan`、`_openSettings`、`_setNavActive`、`switchView`、`openAlbum`、`openPlaylist`、`_loadCurrentView`、`_ncmCacheGet`、`_ncmCacheSet`、`_isSameDay`、`_clearNeteaseCache`、`_prepareNeteaseItems`、`_findAlbumName`、`_findAlbumCover` |
| `plugins/media-player/frontend/js/app-render.js` | 加载 / 空态、专辑卡、媒体行列表、详情页、网易云列表渲染 | `_setLoading`、`_renderEmpty`、`_renderAlbums`、`_renderList`、`_buildEmpty`、`_rowHtml`、`_renderDetail`、`_renderNeteasePlaylists`、`_neteaseToMediaItem`、`_renderNeteaseCliMissing`、`_missingLines`、`_syncCollectedEnabled`、`_exportMissingEnabled`、`_bindContentDelegation`、`_refreshRowState`、`NCM_PLAYLIST_SONG_CAP` |
| `plugins/media-player/frontend/js/app-playback.js` | 播放状态回显、队列浮层、歌单弹窗与右键菜单 | `onTrackChange`、`onPlayStateChange`、`onTimeUpdate`、`updatePlayModeUI`、`updateVolumeUI`、`_updateStageCover`、`_updateStageBackdrop`、`_updatePlayerCover`、`_updateFavButton`、`_updateMiniEq`、`_highlightRows`、`_toggleQueue`、`_renderQueue`、`_clearQueue`、`_openPlaylistModal`、`_openAddToPlaylist`、`showPlaylistMenu`、`_closePlaylistMenu` |
| `plugins/media-player/frontend/js/app-stage.js` | 均衡器、歌词 / 视频 / 宽屏 / 全屏、控件自动隐藏、键盘快捷键 | `_toggleEQ`、`_positionPops`、`_hideEQ`、`_buildEQBands`、`_resetEQ`、`updateStageLyrics`、`_toggleWideMode`、`_toggleLyrics`、`_toggleVideoMode`、`_isVideoShowing`、`toggleFullscreen`、`_enterFullscreen`、`_exitFullscreen`、`_bindFsMove`、`_stopAutoHide`、`_scheduleAutoHide`、`_showControls`、`_hideControls`、`_bindKeyboard` |

落盘产物：壳数据根下 `.cache/media-player/media_state.json`（`_load_state` / `_save_state`，另有 `media_state.corrupt-<stamp>.json` 备份与 `mkstemp` 生成的一次性 `.media_state.<随机>.tmp` 临时文件），媒体根下 `.cache/media_index.json`（`_load_index` / `_save_index`）、`.cache/thumbs.db`（`ThumbCache`）、`scan_task.json`（`_restore_scan_task` 清理的旧文件）；`_report_root` 决定缺失清单 `网易云缺失曲目.txt` 的落点（`export_missing`），`_eq_presets_dir` 决定 `save_eq_preset` 写出的 `<名称>.json`，`_migrate_legacy_state` 读取旧文件 `music_state.json`。

## 后端 API 契约

`register_api` 暴露的方法：

| 方法名 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `media_scan` | `force` = `False` | `success`、`started`、`report` 或 `error` | 兼容入口：`False` 触发同步，`True` 触发全量校验 |
| `media_scan_status` | 无 | `state`（`none` / `done` / `running` / `cancelled`）、`processed`、`total`、`current`、`extra.audio`、`extra.video`、`extra.total` | 轮询扫描状态 |
| `media_scan_cancel` | 无 | `success`、`error` | 请求取消在跑的校验 |
| `media_search` | `keyword` = `''` | 条目字典列表 | 在标题 / 艺术家 / 专辑上做不区分大小写的包含匹配 |
| `media_recent` | `limit` = `50` | 条目字典列表 | 按 `mtime` 倒序取最近条目（上限 200） |
| `media_stats` | 无 | `audio`、`video`、`total`、`audio_albums`、`video_albums`、`favorites`、`playlists` | 侧栏统计 |
| `media_audio_albums` | 无 | 专辑字典列表 | 音频专辑聚合 |
| `media_video_albums` | 无 | 专辑字典列表 | 视频专辑聚合 |
| `media_all_audio` | 无 | 条目字典列表 | 全部音频条目 |
| `media_all_video` | 无 | 条目字典列表 | 全部视频条目 |
| `media_album_items` | `album_key`、`kind` = `''` | 条目字典列表 | 按专辑键取条目，`kind` 非空时再按类型过滤 |
| `media_get_item` | `item_id` | 条目字典或 `{}` | 单条详情 |
| `media_get_items` | `item_ids` | 条目字典列表 | 按传入顺序批量取条目（队列恢复），上限 `QUEUE_PERSIST_LIMIT` |
| `media_playlist_list` | 无 | 歌单字典列表（`id`、`name`、`item_ids`、`created_at`、`updated_at`） | 歌单列表 |
| `media_playlist_get` | `playlist_id` | 歌单字典加 `items`，未命中返回 `{}` | 歌单详情 |
| `media_playlist_save` | `name` = `''`、`playlist_id` = `''`、`item_ids` = `None` | `success`、`playlist` 或 `error` | 新建 / 重命名 / 覆盖曲目；未传 `item_ids` 时保留原曲目 |
| `media_playlist_delete` | `playlist_id` | `success` 或 `error` | 删除歌单 |
| `media_toggle_favorite` | `item_id` | `is_fav` | 切换喜欢 |
| `media_add_favorites` | `item_ids` = `None` | `success`、`added` | 批量加入喜欢，幂等，忽略索引外的 id |
| `media_update_recent` | `item_id` | `success` | 写入最近播放（上限 50） |
| `media_get_state` | 无 | `favorites`、`recent` | 收藏与最近播放的可解析条目，附带 `is_fav` / `played_at` |
| `media_save_playback` | `item_id`、`loop_mode`、`shuffle`、`volume`、`video_mode`、`queue_index` | `success` | 保存播放参数 |
| `media_save_queue` | `item_ids` = `None` | `success`、`count`、`truncated` | 保存队列 id 列表，超出 `QUEUE_PERSIST_LIMIT` 截断 |
| `media_get_playback` | 无 | `item_id`、`loop_mode`、`shuffle`、`volume`、`video_mode`、`queue_index`、`queue_ids`、`queue_truncated` | 读取播放参数；条目已消失时清空并返回 `{}` |
| `media_get_lyrics` | `item_id` | `lyrics`、`source`（`lrc` / `none`） | 读同目录 `.lrc`，依次尝试 `utf-8` / `gbk` / `gb2312` |
| `media_export_missing` | `lines` = `None`、`title` = `'网易云缺失曲目'` | `success`、`path`、`count` 或 `error` | 整体重写缺失曲目清单 |
| `media_put_thumb` | `item_id` = `''`、`data_url` = `''` | `success`、`error` | 前端抽帧结果回写 `ThumbCache`，仅接受 `data:image/` 前缀 |
| `media_thumb_missing` | `item_ids` = `None` | `missing` | 批量判定封面未缓存项（上限 200） |
| `media_debug_meta` | `item_id` = `''` | `file`、`mutagen_available`、`parsed` 或 `error` | 标签解析诊断 |
| `media_ffmpeg_status` | 无 | `available`、`path`、`configured` | 重新探测 ffmpeg |
| `media_list_eq_presets` | 无 | 预设字典列表（`name`、`bands`、`id`） | 列出预设 |
| `media_save_eq_preset` | `name` = `''`、`bands` = `None` | `success`、`id`、`name`、`bands` 或 `error` | 保存自定义预设 |
| `media_get_config` | `key`、`default` = `None` | 设置值 | 读取单项设置 |
| `media_set_config` | `key`、`value` | `success` | 写入单项设置 |
| `get_settings` | 无 | 设置字典（密钥掩码） | 插件设置读取 |
| `save_settings` | `values` | 保存结果 | 插件设置写入 |

壳按名调用的成员：

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `settings_schema` | 无 | 设置项字典列表 | 壳渲染设置面板；`media_roots` 声明 `type` = `directory`、`multi`、`admin_only`，由 `shell/frontend/public/shell/folder-picker.js` 渲染 |
| `freshness_spec` | 无 | `roots`、`include`、`include_names`、`key_of`、`derive`、`prune`、`on_verified`、`on_pass_end`、`rebuild`、`content_version`、`unit`、`min_sync_interval`、`max_dirs_per_sync` | 统一刷新基建接管增量与校验 |
| `get_data_root` | 无 | `Path` | 缓存目录 `_cache_dir` 与索引 `_cache_file` 的基准 |
| `get_file_roots` | 无 | `Path` 列表 | `/file` 路由的允许根 |
| `get_thumb_data` | `filepath`（item id） | `(bytes, mime)` 或 `None` | `/thumbs` 路由按 id 取封面 |
| `get_settings` | 无 | 设置字典 | 设置面板初值 |
| `save_settings` | `values` | 保存结果 | 触发 `on_settings_changed` |
| `on_settings_changed` | `changed_keys` | 无 | 刷新 ffmpeg 通道、重建扫描根与索引 |
| `on_unload` | 无 | 无 | 取消在跑的校验并落盘索引 |

## 前端结构

DOM 结构树：

- `#app`
  - `aside.mp-sidebar.view-sub-sidebar`：`data-obx-splitter` = `--sub-sidebar-width`、`data-obx-splitter-min`、`data-obx-splitter-reserve`、`data-obx-splitter-default`
    - `.mp-brand` › `.mp-brand-icon`、`.mp-brand-text` › `.mp-brand-title`、`.mp-brand-sub`
    - `nav.mp-nav`
      - `button.obx-nav-item.mp-nav-item` ×6：`data-view` = `recent` / `audio-albums` / `all-audio` / `video-albums` / `all-video` / `favorites`
      - `#mp-extensions`：`renderExtensions` 注入的扩展入口
    - `.mp-playlist-section` › `.mp-section-label`（`#btn-new-playlist`）、`#mp-playlist-list.mp-playlist-list.obx-scroll`
    - `.mp-sidebar-footer#mp-stats` › `.mp-mini-eq#mp-mini-eq`、`#mp-stats-text`
  - `.view-body`
    - `.view-toolbar.mp-toolbar` › `.toolbar-group.mp-view-heading`（`#mp-view-title`、`#mp-view-sub`）、`.toolbar-group.mp-toolbar-right`（`.search-field` › `#media-search`、`#btn-search-clear`；`#btn-scan.mp-tool-btn`、`#mp-freshness`、`#btn-settings`）
    - `.mp-main`
      - `section.mp-stage#mp-stage`：`data-obx-splitter` = `--mp-stage-vh`、`data-obx-splitter-axis` = `y`、`data-obx-splitter-offset`、`data-obx-splitter-min`
        - `.mp-stage-backdrop#mp-stage-backdrop`、`.mp-stage-scrim`
        - `.mp-stage-viewport`
          - `.mp-stage-art#mp-stage-art` › `.mp-stage-cover#mp-stage-cover`（`#mp-stage-cover-img`）、`.mp-stage-info`（`#mp-stage-kind`、`#mp-stage-title`、`#mp-stage-sub`、`#mp-stage-meta`）、`.mp-stage-lyrics#mp-stage-lyrics`
          - `.mp-stage-empty#mp-stage-empty` › `.mp-stage-empty-icon`、`.mp-stage-empty-text`
          - `video#video-player.mp-video`
          - `.mp-video-hint#mp-video-hint` › `.mp-video-hint-title#mp-video-hint-title`
          - `#btn-stage-play.mp-stage-big-btn`
        - `.mp-playerbar#player-bar`
          - `.mp-pb-left` › `.mp-pb-cover#player-cover`（`#player-cover-img`）、`.mp-pb-info`（`#player-title`、`#player-artist`）、`#btn-toggle-fav.mp-icon-btn`
          - `.mp-pb-center` › `.mp-controls`（`#btn-play-mode`、`#btn-prev`、`#btn-play-pause.mp-play-btn`、`#btn-next`、`#btn-stop`）、`.mp-progress-row`（`#time-current`、`#progress-bar.mp-range.mp-progress`、`#time-duration`）
          - `.mp-pb-right` › `#btn-queue`、`#btn-eq`、`.mp-volume`（`#btn-volume-icon`、`#volume-bar.mp-range.mp-volume-bar`）、`#btn-video-mode`、`#btn-wide`、`#btn-fullscreen`
      - `.view-content.mp-content.obx-scroll#media-content`
  - `.mp-lyrics-page#lyrics-page` ＞ `.mp-lyrics-bg#lyrics-page-bg`、`.mp-lyrics-aurora#lyrics-page-aurora`、`canvas#lyrics-viz-canvas.mp-lyrics-viz`、`.mp-lyrics-toolbar`（`#btn-lyrics-back`、`#lyrics-page-title.mp-lyrics-title`、`#btn-lyrics-settings`）、`.mp-lyrics-body` › `.mp-lyrics-content#lyrics-page-content`、`.mp-lyrics-progress`（`#lyrics-time-current`、`#lyrics-progress-bar`、`#lyrics-time-duration`）
  - `.mp-pop.mp-queue-pop#queue-popup` ＞ `.mp-pop-head`（`#queue-count`、`#btn-clear-queue`）、`.mp-pop-body.mp-queue-list.obx-scroll#queue-list`
  - `.mp-pop.mp-eq-pop#eq-panel` ＞ `.mp-pop-head`（`#eq-preset-select.mp-select`、`#btn-eq-reset`、`#btn-eq-save`、`#btn-eq-close`）、`.mp-pop-body.mp-eq-body.obx-scroll` › `.mp-eq-bands#eq-bands`
  - `.mp-modal#modal-playlist` ＞ `.mp-modal-box` › `.mp-modal-head`（`#playlist-modal-title`、`#btn-playlist-cancel`）、`.mp-modal-body`（`#input-playlist-name.mp-input`）、`.mp-modal-foot`（`#btn-playlist-cancel-2`、`#btn-playlist-confirm`）
  - `.mp-modal#modal-add-to-playlist` ＞ `.mp-modal-box` › `.mp-modal-head`（`#btn-add-pl-close`）、`.mp-modal-body`（`#add-pl-list.mp-add-pl-list`）、`.mp-modal-foot`（`#btn-add-pl-new`、`#btn-add-pl-confirm`）
  - `.mp-modal#modal-eq-name` ＞ `.mp-modal-box.mp-modal-sm` › `.mp-modal-head`（`#btn-eq-name-cancel`）、`.mp-modal-body`（`#input-eq-name.mp-input`）、`.mp-modal-foot`（`#btn-eq-name-cancel-2`、`#btn-eq-name-confirm`）

脚本装载顺序：`js/utils.js` → `js/frame-extractor.js` → `js/lyrics-parser.js` → `js/progress-store.js` → `js/player-core.js` → `js/playlist-manager.js` → `js/app.js` → `js/app-ui-events.js` → `js/app-views.js` → `js/app-render.js` → `js/app-playback.js` → `js/app-stage.js` → 内联脚本实例化 `MediaPlayerApp` 并调 `init`。

动态渲染的类名清单：

- 由 `Object.assign(MediaPlayerApp.prototype, {...})` 扩回原型的成员数：`js/app-ui-events.js` 6 个、`js/app-views.js` 14 个、`js/app-render.js` 17 个、`js/app-playback.js` 18 个、`js/app-stage.js` 19 个。
- `js/app-render.js` 拼接：`.mp-loading`、`.mp-spinner`、`.empty-state`、`.empty-state--error`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`、`.mp-card-grid`、`.mp-card`、`.mp-card-cover`、`.mp-card-badge`、`.mp-card-body`、`.mp-card-title`、`.mp-card-sub`、`.mp-card-meta`、`.mp-card-play`、`.cover-fallback`、`.mp-list`、`.mp-list-group`、`.mp-row`、`.mp-row-index`、`.mp-row-cover`、`.mp-row-info`、`.mp-row-title`、`.mp-row-sub`、`.mp-row-tag`、`.mp-row-duration`、`.mp-row-actions`、`.mp-row-action`、`.mp-detail-hero`、`.mp-detail-cover`、`.mp-detail-info`、`.mp-detail-label`、`.mp-detail-title`、`.mp-detail-sub`、`.mp-detail-meta`、`.mp-detail-actions`、`.mp-hero-back`
- `js/app-playback.js` 拼接：`.mp-queue-item`、`.q-index`、`.q-title`、`.q-kind`、`.q-remove`、`.mp-add-pl-item`、`.mp-context-menu`、`.pl-name`、`.pl-count`、`.pl-more`
- `js/app-stage.js` 拼接：`.mp-eq-band`、`.mp-eq-value`、`.mp-eq-slider`、`.mp-eq-label`、`.mp-lyric-line`
- `js/lyrics-parser.js` 拼接：`.mp-lyric-line`、`.mp-stage-lyric`、`.mp-stage-lyric-empty`、`.mp-lyrics-empty`
- `js/playlist-manager.js` 拼接：`.mp-playlist-item`、`.mp-playlist-empty`、`.pl-name`、`.pl-count`、`.pl-more`
- 由脚本切换的状态类名：`active`、`hidden`、`show`、`playing`、`paused`、`glow`、`no-cover`、`img-broken`、`fav-active`、`wide-active`、`wide-mode`、`video-on`、`fs-on`、`controls-hidden`、`has-content`、`selected`、`mp-thumb-pending`、`spinning`、`pl-more`
- 由脚本设置的内联 CSS 变量：`--i`、`--range-val`、`--hero-bg`、`--mp-pop-bottom`、`--lyrics-blur`、`--lyrics-brightness`、`--lyrics-font-color`、`--lyrics-font-color-active`、`--lyrics-font-color-hover`、`--lyrics-glow-color`
- 脚本使用的 `data-*` 钩子：`data-view`、`data-id`、`data-idx`、`data-key`、`data-kind`、`data-mp-action`、`data-mp-thumb-id`、`data-mp-busy`、`data-fallback`、`data-icon`、`data-hero-action`、`data-lyric-idx`、`data-lyric-time`、`data-eq-idx`、`data-eq-val`、`data-pl-id`、`data-queue-idx`、`data-remove-idx`、`data-menu-act`
- localStorage 键：`omniboxMediaProgress`、`omniboxMediaVolume`、`omniboxMediaEQ`、`ncmCache_`、`ncmSyncCollected`、`ncmExportMissing`；旧键 `musicProgress`、`videoProgress`

## 与壳的契约

- 壳类与工具：`PluginBase`、`ThumbCache`、`Bridge`（`call`、`callPlugin`、`originalUrl`、`thumbUrl`）、`Toast`、`Utils`、`PluginLifecycle`（`onHide` / `onShow` / `onDispose`）、`ShellSplitter`（按 `data-obx-splitter*` 属性自动接管）、`Freshness`（`mount`、`autoSync`、`destroy`）、`renderExtensions`、`MPUtils.openModal` / `closeModal` 复用的壳弹窗约定。
- 消费的壳 CSS 变量：`--accent`、`--bg-app`、`--bg-surface`、`--bg-hover`、`--bg-overlay`、`--border`、`--danger`、`--danger-soft`、`--text-primary`、`--text-secondary`、`--text-muted`、`--text-on-accent`、`--radius-lg`、`--toolbar-height`、`--sub-sidebar-width`、`--transition-fast`、`--obx-accent-soft`、`--obx-glass-bg`、`--obx-glass-bg-strong`、`--obx-radius`、`--obx-radius-sm`、`--obx-shadow-1`、`--obx-shadow-2`。
- 插件自有 CSS 变量前缀：`--mp-*`（`--mp-stage-vh`、`--mp-pb-height`、`--mp-accent-2`、`--mp-accent-soft`、`--mp-glass`、`--mp-glass-strong`、`--mp-gradient`、`--mp-shadow-1`、`--mp-shadow-2`、`--mp-radius`、`--mp-radius-sm`、`--mp-pop-bottom`）与 `--lyrics-*`、`--range-val`、`--i`、`--hero-bg`。
- 共享后端基建：`FreshnessMixin` 的 `freshness_spec` 契约由 `shell/backend/freshness.py` 的 `engine_for` 消费，缩略图读写走 `shell/backend/thumb_cache.py` 的 `ThumbCache`，分片加载走 `shell/backend/plugin_utils.py` 的 `load_sibling`。
- 设置面板的目录列表由壳的 `settings_schema` + `shell/frontend/public/shell/folder-picker.js` 渲染，插件不实现目录选择器。
- 界面与样式的完整约定见 `docs/plugin-ui-guide.md`。
