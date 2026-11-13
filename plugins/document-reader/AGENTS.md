> document-reader ｜ manifest: plugins/document-reader/manifest.json

## 定位与形态

- 职责：递归扫描配置的文档根目录并把它们聚合成书架，按格式选择阅读方式 —— txt 走 `TxtParser` 的章节偏移切片，md 与 epub 走后端白名单转换器产出的 HTML 片段，pdf 与其余格式交给系统默认程序；当前章正文可交给 edge-tts、OpenAI 兼容端点或系统离线音色朗读。
- 形态：独立页插件，`frontend.route` 为 `/document-reader`；不承载 companion，也不内嵌进其它插件，`dependencies` 为空数组。
- 页面 chrome：manifest 声明 `frontend.chrome`，品牌块、左栏导航、顶栏动作、搜索框、状态栏与侧栏分隔条由壳的 `PluginChrome.vue` 渲染；壳加载内容页时追加 `?content=1`，iframe 内只有内容（书架、阅读器、朗读设置整页、目录与阅读设置弹窗）。
- 库外工具：`tool/tts_speed.py` 在命令行量「合成语音的速度能不能跟上播放速度」，复用本插件的引擎、切句与参数整理，只读设置与文档，不改任何状态。
- 朗读端点契约：自建 OpenAI 兼容端点要实现的路由、请求字段、音色清单与失败语义见 [docs/document-reader-tts-endpoint.md](../../docs/document-reader-tts-endpoint.md)。


## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/document-reader/manifest.json` | 插件声明与壳渲染的页面 chrome | `name`、`backend`、`frontend.route`、`frontend.chrome`（`brand`、`nav`、`toolbar`、`search`、`status`、`freshness`） |
| `plugins/document-reader/backend/main.py` | 入口（facade）：`settings_schema` 引用、API 表、壳按名调用的钩子、装配与换根编排 | `DocumentReaderPlugin`、`settings_schema`、`register_api`、`on_load`、`on_settings_changed`、`get_data_root`、`get_file_roots`、`freshness_spec`、`_image_url`、`_rebind_roots` |
| `plugins/document-reader/backend/const.py` | 设置项 schema、扩展名映射、状态文件名、朗读上限与预取档位（只有入口加载） | `SETTINGS_SCHEMA`、`LAYOUT`、`LayoutNames`、`KIND_BY_EXT`、`IN_APP_KINDS`、`TTS_LIMITS`、`TtsLimits`、`TTS_CACHE_SUBDIR` |
| `plugins/document-reader/backend/roots.py` | 文档根目录解析与切换、状态目录布局、旧状态与旧设置迁移 | `RootLayout`、`parse`、`rebind`、`data_root`、`file_roots`、`cache_path`、`extract_dir`、`migrate_legacy_root_dir` |
| `plugins/document-reader/backend/library.py` | 书架扫描、书架缓存、章节/偏移缓存、阅读进度与脏标记 | `Library`、`iter_documents`、`list_documents`、`scan_document_meta`、`update_progress`、`store_chapters`、`chapters_for`、`offsets_for`、`drop_book`、`clear_derived`、`reset_for_new_root`、`save_cache`、`save_progress`、`document_key_for`、`base_key`、`set_derived_handler` |
| `plugins/document-reader/backend/content.py` | 章节与正文读取、全文缓存、封面、外部打开；按格式分派 | `DocumentContent`、`get_chapters`、`get_content`、`get_full_content`、`document_cover`、`open_external`、`drop_book_cache`、`clear_caches` |
| `plugins/document-reader/backend/freshness.py` | 接入壳的统一刷新基建：规格与同步/校验钩子 | `FreshnessProvider`、`spec`、`derive`、`prune`、`audit`、`pass_end`、`rebuild` |
| `plugins/document-reader/backend/marks.py` | 书签存储与增删查 | `Marks`、`load_marks`、`save_marks`、`reset`、`marks_list`、`marks_add`、`marks_remove` |
| `plugins/document-reader/backend/parser.py` | TXT 编码探测、全文解码、章节识别与偏移切分 | `TxtParser`、`CHAPTER_PATTERNS`、`detect_encoding`、`_decode_strict`、`read_full_content`、`parse_txt`、`_split_by_word_count` |
| `plugins/document-reader/backend/documents.py` | 白名单 HTML 转换、文本解码与格式分派、系统程序打开 | `DocError`、`HtmlTools`、`HTML_TOOLS`、`open_document`、`open_with_system`、`_clean`、`_decode`、`_safe_href`、`_HtmlCleaner`、`_TextProbe` |
| `plugins/document-reader/backend/markdown.py` | Markdown 章节模型：按 1~3 级标题切章并逐章渲染 | `MarkdownDocument`、`chapters`、`chapter_html`、`cover` |
| `plugins/document-reader/backend/epub.py` | EPUB 解析：OPF/manifest/spine、目录标题、解包抽图 | `EpubDocument`、`_parse_opf`、`_toc_titles`、`_read_member`、`_is_safe_member`、`_zip_join`、`MAX_MEMBER_BYTES`、`MAX_EXTRACT_BYTES` |
| `plugins/document-reader/backend/tts.py` | 朗读的插件层：设置→引擎参数、切句、合成与音色表 | `TtsService`、`normalize_params`、`clamp_prefetch`、`params`、`tts_status`、`tts_voices`、`tts_segment`、`tts_speak`、`tts_cache_info`、`tts_clear_cache`、`tts_open_cache_dir`、`file_root` |
| `plugins/document-reader/backend/tts_cache.py` | 朗读缓存：目录、文件名前缀、命中判定、落盘路径与清理 | `TtsCache`、`cache_dir`、`cache_dir_for_write`、`file_root`、`info`、`clear`、`prefix`、`path`、`hit`、`meta_marks`、`prune` |
| `plugins/document-reader/backend/tts_engine.py` | 三档引擎的分派、降级链、句切分与状态 | `SpeechEngine`、`_attempt_engine`、`synthesize`、`status`、`engine_available`、`available_engines`、`engine_has_voice`、`split_text`、`is_edge_voice`、`ENGINES`、`ENGINE_LABELS`、`ENGINE_ATTEMPTS`、`MAX_CHARS`、`DEFAULT_VOICE` |
| `plugins/document-reader/backend/tts_edge.py` | edge-tts 档：流式合成、字级时间戳与中文音色清单；流空闲超过 `STREAM_IDLE_TIMEOUT_SECONDS`（20 秒）没有新数据块即抛 `SynthesisTimeout`，该异常声明 `retryable = False`，引擎据此不原地重试 | `available`、`synthesize`、`voices`、`SynthesisTimeout`、`STREAM_IDLE_TIMEOUT_SECONDS` |
| `plugins/document-reader/backend/tts_endpoint.py` | OpenAI 兼容端点档：合成、地址校验与音色目录 | `available`、`synthesize`、`VoiceCatalog`、`endpoint_voices`、`engine_voice_names`、`clear_cache`、`_validated_endpoint_base` |
| `plugins/document-reader/backend/tts_system.py` | 系统离线音色（SAPI5）档：PowerShell 子进程合成 | `available`、`synthesize` |
| `plugins/document-reader/tool/tts_speed.py` | 库外测速工具：逐句计时并推算播放时间轴，判断连续朗读跟不跟得上 | `main`、`run`、`selftest`、`probe_duration`、`simulate`、`realtime_factor`、`judge`、`build_advice`、`render_report`、`load_backend`、`resolve_params`、`pick_text`、`SAMPLE_TEXT`、`STALL_THRESHOLD_S`、`DEPTH_LADDER`、`DEFAULT_SENTENCES` |
| `plugins/document-reader/frontend/index.html` | 内容页入口：书架、阅读器、朗读设置整页、两个内容弹窗与脚本装载顺序（页面 chrome 不在这里） | `#app`、`.view-body`、`#nr-shelf-view`、`#nr-grid`、`#nr-reader-view`、`#document-content-area`、`#document-progress-fill`、`#nr-voice-page`、`#nr-toc-modal`、`#nr-read-settings-modal` |
| `plugins/document-reader/frontend/document-reader.css` | 全部样式：自有变量、布局、组件、主题与正文底色档位 | `--nr-radius`、`--nr-radius-sm`、`--nr-gradient`、`--nr-accent-soft`、`--reader-font-size`、`--reader-line-height`、`--reader-letter-spacing`、`--reader-bg-color`、`--reader-text-color`、`--reader-body-bg`、`--reader-highlight`、`.nr-grid`、`.nr-card`、`.document-content-area`、`.chapter-content`、`.theme-dark`、`.body-bg-show`、`::highlight(nr-reading)`、`@keyframes nrPulse` |
| `plugins/document-reader/frontend/js/utils.js` | 段落切分、体积格式化、生成式 SVG 封面 | `DocumentUtils`、`splitParagraphs`、`formatContent`、`formatSize`、`_hashHue`、`coverColors`、`coverDataUrl` |
| `plugins/document-reader/frontend/js/reader-engine.js` | 双模式阅读引擎：翻页、连续滚动窗口、字符偏移坐标系 | `DocumentReaderEngine`、`setMode`、`reset`、`goToChapter`、`nextPage`、`prevPage`、`handleScroll`、`_fillViewport`、`_loadAdjacent`、`_appendChapterDiv`、`_prependChapterDiv`、`_trimTop`、`_trimBottom`、`_syncChapterFromScroll`、`chapterFraction`、`chapterElement`、`getChapterContext`、`_textNodes`、`_pointAt`、`_rangeFor`、`_charAtViewportTop`、`sentenceStart`、`anchorAtViewportTop`、`chapterIndexForNode`、`anchorFromRange`、`scrollToChar` |
| `plugins/document-reader/frontend/js/settings.js` | 阅读偏好的后端持久化、设置弹窗保存后的重读与旧 localStorage 迁移 | `ReaderSettingsStore`、`READER_PREF_KEYS`、`BODY_BG_MODES`、`DEFAULT_BODY_BG_MODE`、`LEGACY_SETTINGS_KEY`、`LEGACY_SETTINGS_KEY_OLD`、`loaded`、`load`、`reload`、`_assign`、`_migrateLegacy`、`apply`、`_applyBodyBackground`、`save` |
| `plugins/document-reader/frontend/js/reader-tts.js` | 朗读控制器：切句、请求、按配置深度预取（失败退回 1）、播放、句子高亮与浮动卡 | `ReaderTts`、`NR_MAX_PREFETCH`、`NR_PREFETCH_FALLBACK`、`_createAudio`、`_buildCard`、`_syncCard`、`toggleCollapse`、`isPlaying`、`refreshStatus`、`setPrefetchDepth`、`_selectionAnchor`、`startFromSelection`、`startFromAnchor`、`startFromViewport`、`_startAt`、`_split`、`_requestSegment`、`_cacheKey`、`_playCurrent`、`_resolveSegment`、`_prefetchNext`、`_prefetchAt`、`_degradePrefetch`、`_trace`、`_onEnded`、`_advanceChapter`、`toggle`、`pause`、`resume`、`stop`、`_highlight`、`_scrollIntoView`、`_clearHighlight` |
| `plugins/document-reader/frontend/js/reader-voice-page.js` | 朗读设置页：引擎、端点、音色表与试听、缓存管理 | `ReaderVoicePage`、`EDGE_VOICE_FALLBACK`、`_dom`、`open`、`close`、`_bind`、`_load`、`_loadVoices`、`_cacheTable`、`_buildTables`、`_edgeTable`、`_endpointTable`、`_collect`、`_save`、`_renderStatus`、`_renderVoices`、`_markActiveVoice`、`_renderCache`、`_clearCache`、`_openCacheDir`、`_test` |
| `plugins/document-reader/frontend/js/app.js` | 主控制器：DOM 缓存、事件绑定、书架与阅读态切换、与壳的 chrome 消息、进度保存与还原 | `DocumentReader`、`openNrModal`、`closeNrModal`、`init`、`_cacheDom`、`_bindEvents`、`_bindModalDismiss`、`_bindReaderSettings`、`_bindKeyboard`、`_bindPageTurn`、`_bindScroll`、`_bindLifecycle`、`_postHost`、`_pushChrome`、`_clearSearch`、`_bindHost`、`_applyHostView`、`_applyHostAction`、`_openVoicePage`、`_reloadSettings`、`_reloadShelf`、`_loadDocuments`、`_loadMarks`、`switchView`、`returnToShelf`、`_onQuery`、`_visibleDocuments`、`_updateStatus`、`openDocument`、`_enterReaderMode`、`_openNonChapter`、`_openExternal`、`_afterPageRender`、`_updateProgressBar`、`_clampFraction`、`_contentScrollTop`、`_saveCurrentProgress`、`_nowStamp`、`reloadDocument`、`_showLoading`、`getChapterContext`、`destroy` |
| `plugins/document-reader/frontend/js/app-shelf.js` | 分片：封面网格、书签视图、空态模板，并准备壳顶栏的标题/副标题数据 | `_renderShelf`、`_renderMarks`、`_removeMark`、`_emptyHtml`、`_cardHtml` |
| `plugins/document-reader/frontend/js/app-toc.js` | 分片：目录弹窗渲染与筛选 | `openToc`、`_renderTocItems`、`_updateTocSub` |
| `plugins/document-reader/frontend/js/app-menu.js` | 分片：正文右键菜单、书签增删、正文书签标记层 | `_bindContextMenu`、`_selectionText`、`_buildContextMenuItems`、`_resolveAnchor`、`_onContextAction`、`_debugEnabled`、`_openDevTools`、`_anchorFromViewport`、`addBookmark`、`goToMark`、`_flashMarks`、`_markLayer` |

- 落盘产物：数据根为主目录（`root_dir` 第一行），其下 `.document_state/` 存放 `.document_cache.json`、`.document_progress.json`、`.document_marks.json`，以及 `extract/` 里的 EPUB 封面与正文图片。路径派生在 `RootLayout.cache_path` 与 `RootLayout.extract_dir`，读写符号为 `Library.load_cache`、`Library.save_cache`、`Library.load_progress`、`Library.save_progress`、`Marks.load_marks`、`Marks.save_marks`。
- 落盘产物：朗读音频与同名 `.json` 时间戳落在 `get_plugins_config_dir` 下的 `TTS_CACHE_SUBDIR` 与 `tts` 两级目录里，读写符号为 `TtsCache.cache_dir`、`TtsCache.cache_dir_for_write`、`TtsCache.path`、`TtsCache.hit`、`TtsCache.prune`。

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
| `tts_status` | 无 | `engines` 数组（条目键 `name`、`label`、`available`、`marks`）、`order`、`mode`、`voice`、`rate`、`prefetch` | 引擎可用性一览与当前预取深度 |
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

壳渲染的 chrome（`manifest.json` 的 `frontend.chrome`，由 `PluginChrome.vue` 落 DOM）：

- 品牌块：`icon:book-open`、标题「文档阅读」、副标题「Reader」。
- 浏览组（`browse`）：`all` 全部文档、`recent` 最近阅读、`marks` 书签、`voice` 朗读设置；运行期徽标 `all` / `marks` 走 `cubby:chrome.nav`。
- 「当前文档」组（`reader-tools`，初始 `visible:false`）：`toc` 目录、`read-settings` 阅读设置；阅读态由内容页把该组显隐上报给壳。
- 顶栏动作：`back`（返回，初始 `visible:false`，阅读态显示）、`settings`（设置）。
- 搜索框：占位「搜索书名、作者…」；阅读态与朗读设置页由内容页隐藏。
- 状态栏：`N 本 · 朗读 <引擎>`；统一刷新控件由壳挂载（`freshness.unit` 为「本」）。

内容页 DOM（`plugins/document-reader/frontend/index.html`，iframe 内只有内容）：

- `body`
  - `#app`
    - `.view-body`
      - `.view-content.nr-content.obx-scroll#nr-shelf-view` → `.nr-grid#nr-grid`
      - `.nr-reader-view#nr-reader-view` → `.document-content-area#document-content-area`
      - `.document-progress-bar` → `.document-progress-fill#document-progress-fill`
      - `.nr-voice-page#nr-voice-page`
        - `.nr-voice-head`：`.nr-voice-title`、`#nr-voice-refresh`
        - `.nr-voice-body.obx-scroll`
          - `.nr-voice-card`（引擎）：`#nr-voice-status`、`#nr-voice-engine`、`#nr-voice-base`、`#nr-voice-key`、`#nr-voice-model`
          - `.nr-voice-card`（音色与语速）：`#nr-voice-name`、`#nr-voice-rate`、`#nr-voice-rate-value`、`#nr-voice-prefetch`、`#nr-voice-test`、`#nr-voice-test-result`
          - `.nr-voice-card`（朗读缓存）：`#nr-voice-cache-path`、`#nr-voice-cache-size`、`#nr-voice-cache-open`、`#nr-voice-cache-clear`
          - `.nr-voice-card#nr-voice-card-edge` → `.nr-voice-list#nr-voice-list-edge`
          - `.nr-voice-card#nr-voice-card-endpoint` → `.nr-voice-list#nr-voice-list-endpoint`
  - `#nr-toc-modal.modal` → `.modal-box.nr-modal-box`：`.nr-modal-sub#nr-toc-sub`、`.modal-body`（含 `#nr-toc-filter.search-input.nr-toc-filter`、`.document-chapter-list#nr-toc-list`）、`.modal-footer`
  - `#nr-read-settings-modal.modal` → `.modal-box.nr-modal-box.nr-modal-sm`：`.modal-body`（含 `.nr-setting-row`、`#document-font-size`、`#document-font-size-value`、`#document-line-height`、`#document-line-height-value`、`#document-letter-spacing`、`#document-letter-spacing-value`、`#document-theme-select`、`#custom-color-row`、`#document-bg-color`、`#custom-text-row`、`#document-text-color`、`#document-bg-mode`、`#body-color-row`、`#document-body-color`、`#document-mode-select`）、`.modal-footer`

脚本装载顺序：`js/utils.js` → `js/reader-engine.js` → `js/settings.js` → `js/reader-tts.js` → `js/reader-voice-page.js` → `js/app.js` → `js/app-shelf.js` → `js/app-toc.js` → `js/app-menu.js` → 内联脚本实例化 `DocumentReader` 并调用 `init`。

动态渲染的类名清单（由脚本写入 class 属性，静态 HTML 里没有这些类名）：`theme-auto`、`theme-sepia`、`theme-dark`、`theme-green`、`theme-blue`、`theme-custom`、`body-bg-theme`、`body-bg-show`、`body-bg-solid`、`is-read`、`is-collapsed`、`chapter-content`、`chapter-separator`、`document-chapter-item`、`nr-toc-title`、`nr-toc-meta`、`nr-toc-mark`、`chapter-words`、`nr-card`、`nr-cover`、`nr-cover-img`、`nr-badge`、`nr-mark-count`、`nr-cover-progress`、`nr-card-body`、`nr-card-title`、`nr-card-sub`、`nr-empty-span`、`nr-mark-group`、`nr-mark-head`、`nr-mark-book`、`nr-mark-clear`、`nr-mark-item`、`nr-mark-text`、`nr-mark-meta`、`nr-mark-del`、`nr-mark-layer`、`nr-mark-pin`、`nr-menu-sep`、`nr-tts-card`、`nr-tts-btn`、`nr-tts-collapse`、`nr-tts-toggle`、`nr-tts-stop`、`nr-voice-item`、`nr-voice-name`、`nr-voice-id`、`document-pdf-frame`、`document-pdf-open`、`document-loading`、`spinner`、`empty-state`、`empty-state-icon`、`empty-state-text`、`empty-state-hint`。

## 与壳的契约

- 页面 chrome 的消息（只在壳与内容 iframe 之间，同源 `postMessage`）：
  - 壳 → 内容 `cubby:view`：左栏导航项 id。`all` / `recent` / `marks` 切书架分类，`voice` 切朗读设置整页，`toc` / `read-settings` 是阅读态入口（打开内容弹窗）。
  - 壳 → 内容 `cubby:action`：`back` 返回书架、`refresh` 在统一刷新（同步/校验）完成后重读书架与书签、`settings-saved` 在壳内设置弹窗保存成功后重读阅读偏好并重扫书架。设置弹窗由壳的 `PluginSettingsModal` 打开，不再发 `settings`。
  - 壳 → 内容 `cubby:query`：搜索输入（壳侧 250ms 防抖），内容页按关键词过滤书架并在副标题上报「匹配 N / M 本」。
  - 内容 → 壳 `cubby:ready`：握手。壳收到后补发当前视图；内容页在 `init` 末尾主动发一次。
  - 内容 → 壳 `cubby:chrome`：`title` / `subtitle` / `view`（阅读态发空串清掉导航高亮、朗读设置页发 `voice`）、`actions`（`back.visible`、`settings`）、`nav`（`reader-tools.visible`、`all` / `marks` 徽标）、`search.enabled` 与 `search.value` / `search.clear`（切分类、进阅读态、开朗读设置时清空壳输入框）、`status`（`N 本 · 朗读 <引擎>`）。
- 状态栏的引擎名取冒号/左括号前那一段（如 `edge`），因为壳的侧栏状态行只有一行宽。
- **统一刷新控件由壳的 `PluginChrome` 挂载**（`shell/frontend/public/shell/freshness.js` 的 `Freshness.mount`，`plugin` 为 `document-reader`、`unit` 为「本」），内容页不再挂 `#nr-freshness`；壳在挂载后与 `activeView` / `freshness_scope` 变化时调 `autoSync`，内容页在 `plugin-shown` 时调 `Freshness.sync('document-reader', scope)`。同步/校验结论先到 `cubby:freshness`；紧跟的旧 `cubby:action: refresh` 带同一份报告作 `payload`，`_applyHostAction(action, payload)` 据此判回声、跳过（没有 `payload` 的 `refresh` 来自旧壳，照常重读）。阅读态只重读文档与书签、不重绘书架（`_reloadShelf` 的阅读态守卫）：重绘会用书架分类的标题/统计覆盖壳顶栏的书名。
- 壳布局与组件类（内容 iframe 继续使用）：`.view-body`、`.view-content`、`.obx-scroll`、`.obx-icon`、`.btn`、`.btn-sm`、`.btn-primary`、`.btn-danger`、`.modal`、`.modal-box`、`.modal-body`、`.modal-footer`、`.search-input`、`.hidden`、`.empty-state`、`.empty-state-icon`、`.empty-state-text`、`.empty-state-hint`。
- 内容页不得再出现 chrome 标记：`.view-sub-sidebar`、`.view-toolbar`、`.sub-sidebar-brand`、`.obx-nav-item`（`tools/plugin_ui_audit.py` 的 `iframe.content-only` 会检查）。
- 壳 CSS 变量：`--accent`、`--bg-app`、`--bg-surface`、`--bg-hover`、`--border`、`--danger`、`--text-primary`、`--text-secondary`、`--text-muted`、`--shadow-sm`、`--shadow-md`、`--transition-fast`、`--obx-i`。
- 插件自有 CSS 变量前缀：`--nr-` 用于圆角、渐变与强调色，`--reader-` 用于正文字号、行距、字间距、配色与高亮。
- 共享前端组件：`Bridge.call`、`Bridge.originalUrl`、`Toast`、`Utils.escapeHtml`、`Icons.html`、`Motion.stagger`、`createContextMenu`、`confirmDialog`、`PluginLifecycle.onDispose`。
- 设置项 `root_dir` 的 `type` 取值 `directory` 由壳的 `PluginSettingsModal` 渲染：每个路径一行 +「选择目录…」，目录列表用壳内共享 `FolderPicker`（`shell/frontend/public/shell/folder-picker.js`）浏览本机目录；该字段未声明 `local_only`，因此「网络位置」入口也可见（来源由安装了 network-location 能力的插件提供）。
- 共享后端基建：`shell/backend/plugin_base.py` 的 `PluginBase`（`setting`、`update_setting`、`_raw_settings`、`save_settings`、`settings_schema`）、`shell/backend/plugin_utils.py` 的 `load_sibling`、`shell/backend/paths.py` 的 `get_plugins_config_dir`、`shell/backend/freshness.py` 的 `FreshnessEngine`、`shell/backend/file_server.py` 的 `/file` 路由。
- 壳契约与覆盖规则见 `docs/plugin-ui-guide.md`。
