> document-reader ｜ manifest: plugins/document-reader/manifest.json

## 定位与形态

- 职责：递归扫描配置的文档根目录并把它们聚合成书架，按格式选择阅读方式 —— txt 走 `TxtParser` 的章节偏移切片，md 与 epub 走后端白名单转换器产出的 HTML 片段，pdf 与其余格式交给系统默认程序；当前章正文可交给 edge-tts、OpenAI 兼容端点或系统离线音色朗读。
- 形态：独立页插件，`frontend.route` 为 `/document-reader`；不承载 companion，也不内嵌进其它插件，`dependencies` 为空数组。


## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/document-reader/backend/main.py` | 插件类骨架、设置 schema、API 注册、章节与封面读取、外部打开 | `DocumentReaderPlugin`、`settings_schema`、`register_api`、`KIND_BY_EXT`、`IN_APP_KINDS`、`CACHE_FILE`、`PROGRESS_FILE`、`CACHE_VERSION`、`CACHE_DIR_NAME`、`on_load`、`get_chapters`、`get_content`、`get_full_content`、`document_cover`、`open_external`、`tts_open_cache_dir`、`_document`、`_image_url`、`_extract_dir`、`_cache_path` |
| `plugins/document-reader/backend/library.py` | 书架扫描、缓存与进度读写、统一刷新声明 | `LibraryMixin`、`_iter_documents`、`list_documents`、`_scan_document_meta`、`update_progress`、`_load_cache`、`_save_cache`、`_load_progress`、`_save_progress`、`_drop_book_cache`、`_kind_of`、`_base_document_key`、`freshness_spec`、`_document_key_for`、`_freshness_derive`、`_freshness_prune`、`_freshness_audit`、`_freshness_pass_end`、`_freshness_rebuild` |
| `plugins/document-reader/backend/roots.py` | 文档根目录解析与切换、状态目录布局、旧状态与旧设置迁移 | `RootsMixin`、`CACHE_DIR_NAME`、`LEGACY_CACHE_DIR`、`LEGACY_CACHE_FILE`、`LEGACY_PROGRESS_FILE`、`LEGACY_PLUGIN_NAME`、`get_data_root`、`get_file_roots`、`_parse_roots`、`_set_roots`、`_migrate_legacy_state`、`_apply_root_dir`、`on_settings_changed` |
| `plugins/document-reader/backend/marks.py` | 书签存储与增删查 | `MarksMixin`、`MARKS_FILE`、`_load_marks`、`_save_marks`、`marks_list`、`marks_add`、`marks_remove` |
| `plugins/document-reader/backend/parser.py` | TXT 编码探测、全文解码、章节识别与偏移切分 | `TxtParser`、`CHAPTER_PATTERNS`、`detect_encoding`、`_decode_strict`、`read_full_content`、`parse_txt`、`_split_by_word_count` |
| `plugins/document-reader/backend/documents.py` | Markdown 与 EPUB 转白名单 HTML、EPUB 解包、系统程序打开 | `DocError`、`open_document`、`open_with_system`、`MarkdownDocument`、`EpubDocument`、`_clean`、`_HtmlCleaner`、`_TextProbe`、`_AnchorCollector`、`_decode`、`_safe_href`、`_local`、`_is_safe_member`、`_zip_join`、`_read_member`、`_find_opf`、`_parse_opf`、`_toc_titles`、`MAX_MEMBER_BYTES`、`MAX_EXTRACT_BYTES` |
| `plugins/document-reader/backend/tts.py` | 朗读设置读取、切句接口、按内容缓存音频与缓存管理 | `TtsMixin`、`TTS_CACHE_LIMIT`、`TTS_RESULT_LIMIT`、`_AUDIO_EXTS`、`_CACHE_KEY_RE`、`_tts_cache_subdir`、`_tts_cache_dir`、`_tts_cache_dir_for_write`、`_tts_params`、`tts_status`、`tts_voices`、`_voice_tables`、`_edge_table`、`_endpoint_table`、`tts_segment`、`tts_speak`、`_tts_cache_prefix`、`_tts_cache_path`、`_tts_hit`、`_tts_meta_marks`、`_tts_prune`、`tts_cache_info`、`tts_clear_cache` |
| `plugins/document-reader/backend/tts_engine.py` | 三档引擎的合成、降级链、音色清单与句切分 | `ENGINES`、`ENGINE_LABELS`、`ENGINE_ATTEMPTS`、`MAX_CHARS`、`DEFAULT_VOICE`、`VOICE_CACHE_TTL`、`VOICE_LIST_TIMEOUT`、`_RUNNERS`、`_SAPI_SCRIPT`、`split_text`、`is_edge_voice`、`edge_synthesize`、`openai_synthesize`、`system_synthesize`、`system_available`、`edge_voices`、`endpoint_voices`、`engine_voice_names`、`engine_has_voice`、`engine_available`、`available_engines`、`synthesize`、`status`、`clear_voice_cache` |
| `plugins/document-reader/frontend/index.html` | 唯一入口页：静态 DOM 骨架与脚本装载顺序 | `#app`、`.view-sub-sidebar`、`.nr-sidebar`、`.obx-glass`、`#nr-reader-tools`、`#nr-open-voice`、`#nr-extensions`、`#nr-status`、`#nr-back-to-shelf`、`#nr-view-title`、`#nr-view-sub`、`#nr-search`、`#nr-search-clear`、`#nr-freshness`、`#btn-settings`、`#nr-shelf-view`、`#nr-grid`、`#nr-reader-view`、`#document-content-area`、`#document-progress-fill`、`#nr-voice-page`、`#nr-toc-modal`、`#nr-read-settings-modal` |
| `plugins/document-reader/frontend/document-reader.css` | 全部样式：自有变量、布局、组件、主题与正文底色档位 | `--nr-radius`、`--nr-radius-sm`、`--nr-gradient`、`--nr-accent-soft`、`--reader-font-size`、`--reader-line-height`、`--reader-letter-spacing`、`--reader-bg-color`、`--reader-text-color`、`--reader-body-bg`、`--reader-highlight`、`.nr-grid`、`.nr-card`、`.document-content-area`、`.chapter-content`、`.theme-dark`、`.body-bg-show`、`::highlight(nr-reading)`、`@keyframes nrPulse` |
| `plugins/document-reader/frontend/js/utils.js` | 段落切分、体积格式化、生成式 SVG 封面 | `DocumentUtils`、`splitParagraphs`、`formatContent`、`formatSize`、`_hashHue`、`coverColors`、`coverDataUrl` |
| `plugins/document-reader/frontend/js/reader-engine.js` | 双模式阅读引擎：翻页、连续滚动窗口、字符偏移坐标系 | `DocumentReaderEngine`、`setMode`、`reset`、`goToChapter`、`nextPage`、`prevPage`、`handleScroll`、`_fillViewport`、`_loadAdjacent`、`_appendChapterDiv`、`_prependChapterDiv`、`_trimTop`、`_trimBottom`、`_syncChapterFromScroll`、`chapterFraction`、`chapterElement`、`getChapterContext`、`_textNodes`、`_pointAt`、`_rangeFor`、`_charAtViewportTop`、`sentenceStart`、`anchorAtViewportTop`、`chapterIndexForNode`、`anchorFromRange`、`scrollToChar` |
| `plugins/document-reader/frontend/js/settings.js` | 阅读偏好的后端持久化、设置弹窗保存后的重读与旧 localStorage 迁移 | `ReaderSettingsStore`、`READER_PREF_KEYS`、`BODY_BG_MODES`、`DEFAULT_BODY_BG_MODE`、`LEGACY_SETTINGS_KEY`、`LEGACY_SETTINGS_KEY_OLD`、`loaded`、`load`、`reload`、`_assign`、`_migrateLegacy`、`apply`、`_applyBodyBackground`、`save` |
| `plugins/document-reader/frontend/js/reader-tts.js` | 朗读控制器：切句、请求、预取、播放、句子高亮与浮动卡 | `ReaderTts`、`_createAudio`、`_buildCard`、`_syncCard`、`toggleCollapse`、`isPlaying`、`refreshStatus`、`_selectionAnchor`、`startFromSelection`、`startFromAnchor`、`startFromViewport`、`_startAt`、`_split`、`_requestSegment`、`_cacheKey`、`_playCurrent`、`_prefetchNext`、`_trace`、`_onEnded`、`_advanceChapter`、`toggle`、`pause`、`resume`、`stop`、`_highlight`、`_scrollIntoView`、`_clearHighlight` |
| `plugins/document-reader/frontend/js/reader-voice-page.js` | 朗读设置页：引擎、端点、音色表与试听、缓存管理 | `ReaderVoicePage`、`EDGE_VOICE_FALLBACK`、`_dom`、`open`、`close`、`_bind`、`_load`、`_loadVoices`、`_cacheTable`、`_buildTables`、`_edgeTable`、`_endpointTable`、`_collect`、`_save`、`_renderStatus`、`_renderVoices`、`_markActiveVoice`、`_renderCache`、`_clearCache`、`_openCacheDir`、`_test` |
| `plugins/document-reader/frontend/js/app.js` | 主控制器：DOM 缓存、事件绑定、书架与阅读态切换、进度保存与还原 | `DocumentReader`、`openNrModal`、`closeNrModal`、`init`、`_cacheDom`、`_bindEvents`、`_bindModalDismiss`、`_bindReaderSettings`、`_bindKeyboard`、`_bindPageTurn`、`_bindScroll`、`_bindLifecycle`、`_mountFreshness`、`_autoSyncFreshness`、`_onFreshnessChanged`、`_loadDocuments`、`_loadMarks`、`loadExtensions`、`switchView`、`returnToShelf`、`_visibleDocuments`、`_updateStatus`、`openDocument`、`_enterReaderMode`、`_openNonChapter`、`_openExternal`、`_afterPageRender`、`_updateProgressBar`、`_clampFraction`、`_contentScrollTop`、`_saveCurrentProgress`、`_nowStamp`、`reloadDocument`、`_showLoading`、`getChapterContext`、`destroy` |
| `plugins/document-reader/frontend/js/app-shelf.js` | 分片：封面网格、书签视图、空态模板 | `_renderShelf`、`_renderMarks`、`_removeMark`、`_emptyHtml`、`_cardHtml` |
| `plugins/document-reader/frontend/js/app-toc.js` | 分片：目录弹窗渲染与筛选 | `openToc`、`_renderTocItems`、`_updateTocSub` |
| `plugins/document-reader/frontend/js/app-menu.js` | 分片：正文右键菜单、书签增删、正文书签标记层 | `_bindContextMenu`、`_selectionText`、`_buildContextMenuItems`、`_resolveAnchor`、`_onContextAction`、`_debugEnabled`、`_openDevTools`、`_anchorFromViewport`、`addBookmark`、`goToMark`、`_flashMarks`、`_markLayer` |

- 落盘产物：数据根为主目录（`root_dir` 第一行），其下 `.document_state/` 存放 `.document_cache.json`、`.document_progress.json`、`.document_marks.json`，以及 `extract/` 里的 EPUB 封面与正文图片，读写符号为 `_cache_path`、`_load_cache`、`_save_cache`、`_load_progress`、`_save_progress`、`_load_marks`、`_save_marks`、`_extract_dir`。
- 落盘产物：朗读音频与同名 `.json` 时间戳落在 `get_plugins_config_dir` 下的 `_tts_cache_subdir` 与 `tts` 两级目录里，读写符号为 `_tts_cache_dir`、`_tts_cache_dir_for_write`、`_tts_cache_path`、`_tts_hit`、`_tts_prune`。

## 后端 API 契约

`register_api` 暴露的方法如下。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `document_list` | 无 | `documents` 数组，条目键为 `id`、`title`、`author`、`kind`、`dir`、`root`、`file_path`、`file_size`、`mtime`、`chapter_count`、`last_read_time`、`last_read_chapter`、`progress`、`scroll_position`、`encoding` | 列书架并检查文件是否变更 |
| `document_get_chapters` | `document_id`、`encoding` | `chapters` 数组，条目键为 `index`、`title`、`word_count`；失败时返回 `error` 与空 `chapters` | 取章节目录 |
| `document_get_content` | `document_id`、`chapter_index`、`encoding` | `content` 与 `format`，`format` 取 `text` 或 `html`；失败时返回 `error` | 取单章正文 |
| `document_update_progress` | `document_id`、`chapter_index`、`scroll_position`、`encoding` | `success` | 写阅读进度 |
| `document_open_external` | `document_id` | `success`；失败时返回 `error` | 用系统默认程序打开文档 |
| `document_cover` | `document_id` | `cover`，无封面时为空串 | 取 EPUB 封面图 URL |
| `marks_list` | `document_id`，可省略 | `marks` 数组，条目键为 `chapter`、`char`、`snippet`、`label`、`time`；省略 id 时每条附 `document_id` | 列书签 |
| `marks_add` | `document_id`、`chapter`、`char`、`snippet`、`label` | `success` 与 `marks`；失败时返回 `error` | 加书签 |
| `marks_remove` | `document_id`、`chapter`、`char` | `success` 与 `marks` | 删书签 |
| `get_settings` | 无 | 掩码后的设置项，键名同 `settings_schema` 的 `key` | 读插件设置 |
| `save_settings` | 设置项字典 | `success`；失败时返回 `error` | 写插件设置 |
| `tts_segment` | `text`、`max_chars` | `segments` 数组 | 把正文切成句 |
| `tts_speak` | `text`、`cache_key`、`segment_index`、`engine` | `url`、`mtime`、`cached`、`engine`、`marks`、`segmentIndex`；失败时返回 `error` | 合成一段并返回同源可播放 URL |
| `tts_status` | 无 | `engines` 数组（条目键 `name`、`label`、`available`、`marks`）、`order`、`mode`、`voice`、`rate` | 引擎可用性一览 |
| `tts_voices` | 无 | `mode` 与 `tables` 数组，表条目键为 `source`、`title`、`voices`、`unavailable`、`note` | 按引擎列音色候选 |
| `tts_cache_info` | 无 | `path`、`files`、`bytes`、`limitBytes` | 朗读缓存位置与占用 |
| `tts_clear_cache` | 无 | `success`、`removed`、`bytes` | 清空朗读缓存 |
| `tts_open_cache_dir` | 无 | `success` 与 `path`；失败时返回 `error` | 打开朗读缓存目录 |

壳按名调用的成员如下。

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `register_api` | 无 | API 名到方法的字典 | 壳装载插件时取 API 表 |
| `settings_schema` | 类属性 | 字段字典列表，字段名含 `key`、`label`、`type`、`default`、`options`、`hidden`、`admin_only`、`secret`、`multi`、`help`、`placeholder`、`emptyText`、`min`、`max`、`step` | 壳渲染设置弹窗，并作为 `save_settings` 的写入白名单 |
| `__init__` | `manifest`、`config` | 无 | 壳实例化插件 |
| `on_load` | 无 | 无 | 壳注入 `_settings_store` 后调用；`main.py` 在此迁移旧插件名下的 `root_dir` |
| `on_settings_changed` | 变更键集合 | 无 | 壳在 `save_settings` 之后调用；`root_dir` 变化时切根并清空缓存 |
| `get_data_root` | 无 | `Path` | 壳取主目录，取值为文档根目录第一行 |
| `get_file_roots` | 无 | `Path` 列表 | 壳判定 `/file` 允许根，含全部文档目录与朗读缓存目录的父目录 |
| `freshness_spec` | 无 | 刷新声明字典，键为 `roots`、`include`、`key_of`、`derive`、`prune`、`on_verified`、`on_pass_end`、`rebuild`、`content_version`、`unit`、`min_sync_interval`、`stat_entries` | 壳的刷新引擎据此同步与校验书架 |
| `setting` | 键、默认值 | 设置值 | 插件读单项设置，由 `PluginBase` 提供 |
| `update_setting` | 键、值 | 是否写入成功 | 插件写单项设置，由 `PluginBase` 提供 |
| `_raw_settings` | 无 | 未脱敏的设置字典 | 插件读回凭据类设置，由 `PluginBase` 提供 |
| `name` | 属性 | 插件名 | 日志前缀与 `/file` 的 `plugin` 参数 |

## 前端结构

DOM 结构树如下。

- `body`
  - `#app`
    - `aside.view-sub-sidebar.nr-sidebar.obx-glass`
      - `.nr-brand` → `.nr-brand-icon`、`.nr-brand-text`（含 `.nr-brand-title`、`.nr-brand-sub`）
      - `nav.nr-nav.obx-scroll`
        - `.nr-nav-label`（浏览）与 `button.obx-nav-item.nr-nav-item`（全部文档 / 最近阅读 / 书签）
        - `.nr-nav-count`：`#nr-count-all`、`#nr-count-marks`
        - `#nr-open-voice`
        - `#nr-reader-tools`（阅读态显示）：`.nr-nav-label`（当前文档）、`#nr-open-toc`、`#nr-open-read-settings`
        - `#nr-extensions`
      - `.sub-sidebar-footer.nr-sidebar-footer#nr-status`
    - `.view-body`
      - `.view-toolbar.nr-toolbar`
        - `.toolbar-group.nr-heading`：`#nr-back-to-shelf`、`#nr-view-title`、`#nr-view-sub`
        - `.toolbar-group.nr-toolbar-right`：`.search-field`（`#nr-search`、`#nr-search-clear`）、`#nr-freshness`、`#btn-settings`
      - `.view-content.nr-content.obx-scroll#nr-shelf-view` → `.nr-grid#nr-grid`
      - `.nr-reader-view#nr-reader-view` → `.document-content-area#document-content-area`
      - `.document-progress-bar` → `.document-progress-fill#document-progress-fill`
      - `.nr-voice-page#nr-voice-page`
        - `.nr-voice-head`：`.nr-voice-title`、`#nr-voice-refresh`
        - `.nr-voice-body.obx-scroll`
          - `.nr-voice-card`（引擎）：`#nr-voice-status`、`#nr-voice-engine`、`#nr-voice-base`、`#nr-voice-key`、`#nr-voice-model`
          - `.nr-voice-card`（音色与语速）：`#nr-voice-name`、`#nr-voice-rate`、`#nr-voice-rate-value`、`#nr-voice-test`、`#nr-voice-test-result`
          - `.nr-voice-card`（朗读缓存）：`#nr-voice-cache-path`、`#nr-voice-cache-size`、`#nr-voice-cache-open`、`#nr-voice-cache-clear`
          - `.nr-voice-card#nr-voice-card-edge` → `.nr-voice-list#nr-voice-list-edge`
          - `.nr-voice-card#nr-voice-card-endpoint` → `.nr-voice-list#nr-voice-list-endpoint`
  - `#nr-toc-modal.modal` → `.modal-box.nr-modal-box`：`.nr-modal-sub#nr-toc-sub`、`.modal-body`（含 `#nr-toc-filter.search-input.nr-toc-filter`、`.document-chapter-list#nr-toc-list`）、`.modal-footer`
  - `#nr-read-settings-modal.modal` → `.modal-box.nr-modal-box.nr-modal-sm`：`.modal-body`（含 `.nr-setting-row`、`#document-font-size`、`#document-font-size-value`、`#document-line-height`、`#document-line-height-value`、`#document-letter-spacing`、`#document-letter-spacing-value`、`#document-theme-select`、`#custom-color-row`、`#document-bg-color`、`#custom-text-row`、`#document-text-color`、`#document-bg-mode`、`#body-color-row`、`#document-body-color`、`#document-mode-select`）、`.modal-footer`

脚本装载顺序：`js/utils.js` → `js/reader-engine.js` → `js/settings.js` → `js/reader-tts.js` → `js/reader-voice-page.js` → `js/app.js` → `js/app-shelf.js` → `js/app-toc.js` → `js/app-menu.js` → 内联脚本实例化 `DocumentReader` 并调用 `init`。

动态渲染的类名清单（由脚本写入 class 属性，静态 HTML 里没有这些类名）：`theme-auto`、`theme-sepia`、`theme-dark`、`theme-green`、`theme-blue`、`theme-custom`、`body-bg-theme`、`body-bg-show`、`body-bg-solid`、`is-read`、`is-collapsed`、`chapter-content`、`chapter-separator`、`document-chapter-item`、`nr-toc-title`、`nr-toc-meta`、`nr-toc-mark`、`chapter-words`、`nr-card`、`nr-cover`、`nr-cover-img`、`nr-badge`、`nr-mark-count`、`nr-cover-progress`、`nr-card-body`、`nr-card-title`、`nr-card-sub`、`nr-empty-span`、`nr-mark-group`、`nr-mark-head`、`nr-mark-book`、`nr-mark-clear`、`nr-mark-item`、`nr-mark-text`、`nr-mark-meta`、`nr-mark-del`、`nr-mark-layer`、`nr-mark-pin`、`nr-menu-sep`、`nr-tts-card`、`nr-tts-btn`、`nr-tts-collapse`、`nr-tts-toggle`、`nr-tts-stop`、`nr-voice-item`、`nr-voice-name`、`nr-voice-id`、`document-pdf-frame`、`document-pdf-open`、`document-loading`、`spinner`、`empty-state`、`empty-state-icon`、`empty-state-text`、`empty-state-hint`。

## 与壳的契约

- 壳布局与组件类：`.view-sub-sidebar`、`.sub-sidebar-footer`、`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.obx-nav-item`、`.obx-scroll`、`.obx-glass`、`.obx-icon`、`.btn`、`.btn-sm`、`.btn-primary`、`.btn-danger`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`、`.search-field`、`.search-field-icon`、`.search-field-clear`、`.search-input`、`.hidden`、`.empty-state`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`。
- 壳 CSS 变量：`--accent`、`--bg-app`、`--bg-surface`、`--bg-hover`、`--border`、`--danger`、`--text-primary`、`--text-secondary`、`--text-muted`、`--shadow-sm`、`--shadow-md`、`--transition-fast`、`--sub-sidebar-width`、`--obx-i`。
- 插件自有 CSS 变量前缀：`--nr-` 用于圆角、渐变与强调色，`--reader-` 用于正文字号、行距、字间距、配色与高亮；`--sub-sidebar-width` 由壳的侧栏分隔条写入（`shell/frontend/public/shell/splitter.js`）。
- 共享前端组件：`Bridge.call`、`Bridge.callSystem`、`Bridge.originalUrl`、`Toast`、`Utils.escapeHtml`、`Utils.debounce`、`Icons.html`、`Motion.stagger`、`Freshness.mount`、`createContextMenu`、`confirmDialog`、`openSettingsModal`、`renderExtensions`、`PluginLifecycle.onDispose`。
- 统一刷新控件由 `shell/frontend/public/shell/freshness.js` 填充 `#nr-freshness`，`Freshness.mount` 的入参为 `plugin`、`container`、`unit`、`onChange`。
- 设置项 `root_dir` 的 `type` 取值 `directory` 由壳的目录选择器渲染（`shell/frontend/public/shell/folder-picker.js`）。
- 共享后端基建：`shell/backend/plugin_base.py` 的 `PluginBase`（`setting`、`update_setting`、`_raw_settings`、`save_settings`、`settings_schema`）、`shell/backend/plugin_utils.py` 的 `load_sibling`、`shell/backend/paths.py` 的 `get_plugins_config_dir`、`shell/backend/freshness.py` 的 `FreshnessEngine`、`shell/backend/file_server.py` 的 `/file` 路由。
- 壳契约与覆盖规则见 `docs/plugin-ui-guide.md`。
