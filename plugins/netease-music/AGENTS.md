> netease-music ｜ manifest: plugins/netease-music/manifest.json

## 定位与形态

- 职责：经本机 `ncm-cli` 命令行取网易云在线音乐数据（搜索、每日推荐、歌单、红心歌曲、歌词、播放地址），把五个入口注册进宿主 `media-player` 的侧栏，音频播放与歌词渲染由宿主承担。
- 形态：内嵌 Companion 插件，宿主是 `media-player`；入口来自后端 `get_extensions` 返回条目的 `view` 字段。宿主 manifest 的 `frontend.chrome.extensions` 让壳的 `PluginExtensions.vue` 拉取这五个条目并渲染到宿主侧栏，点击后壳向宿主内容页发 `omnibox:view`（`ncm-*`），由宿主内容页渲染对应视图。本插件没有 `frontend.chrome`，也不提供 `native` 面板；`frontend/index.html` 只在本插件被直接打开时使用。
- 本插件不是宿主：`get_extensions` 返回条目的 `host` 全部指向 `media-player`，本插件不承载其他插件的条目。
- `manifest.json` 的 `hidden` 为 `true`，壳的 `get_frontend_manifests` 把 hidden 插件排除出前端清单，因此 `frontend/index.html` 只在直接打开时渲染；界面上的五个网易云视图由宿主按 `view` 原生渲染。

## 文件地图

| 文件 | 职责 | 关键符号 |
| --- | --- | --- |
| `plugins/netease-music/manifest.json` | 声明依赖宿主、后端入口与类、前端入口与路由、隐藏自身 | `name`、`dependencies`、`backend.entry`、`backend.class`、`frontend.entry`、`frontend.route`、`hidden` |
| `plugins/netease-music/backend/main.py` | 插件类、扩展条目、API 注册、统一刷新声明、ncm-cli 探活 | `NeteaseMusicPlugin`、`NETEASE_TTL_SECONDS`、`_get_api`、`freshness_spec`、`_invalidate_caches`、`get_extensions`、`register_api`、`get_status`、`search_song`、`search_playlist`、`get_daily_recommend`、`get_liked_songs`、`get_created_playlists`、`get_collected_playlists`、`get_playlist_tracks`、`get_song_url`、`get_lyric`、`check_login`、`login`、`_song`、`_playlist` |
| `plugins/netease-music/backend/netease_music_api.py` | ncm-cli 调用与输出解析、假 mpv 取流、播放地址缓存 | `Song`、`Playlist`、`Album`、`NeteaseMusicAPI`、`_NCM_PACKAGE_ENTRY`、`_SHELL_META_CHARS`、`_ncm_argv_prefix`、`_reject_shell_meta`、`_ensure_player_mpv_config`、`_run_command`、`_command_success`、`_check_installation`、`check_login`、`clear_url_cache`、`login`、`set_config`、`get_config`、`get_commands`、`run_cli_command`、`search_song`、`search_playlist`、`search_album`、`create_playlist`、`add_to_playlist`、`get_playlist_detail`、`get_playlist_tracks`、`get_created_playlists`、`get_collected_playlists`、`get_my_playlists`、`get_daily_recommend`、`get_user_info`、`get_liked_songs`、`_get_favorite_playlist_id`、`get_song_url`、`resolve_song_url`、`_url_cache`、`_url_cache_size`、`_resident_initialized`、`_private_bin_dir`、`_ensure_fake_mpv`、`play`、`pause`、`resume`、`next_track`、`previous_track`、`set_volume`、`get_playback_status`、`get_lyric`、`analyze_preference`、`_calculate_preference`、`_parse_songs`、`_parse_playlists`、`_parse_albums`、`_parse_single_playlist`、`_playlist_from_dict`、`_parse_json_output`、`_load_json`、`_extract_list` |
| `plugins/netease-music/frontend/index.html` | 单页骨架、内联样式块、装载 `js/app.js` 与引导脚本 | `#title`、`#ncm-freshness`、`#search-row`、`#keyword`、`#search-btn`、`#content`、`.wrap`、`.item`、`.t`、`.s`、`.empty-state`、`.btn`、`.btn-primary`、`--bg-app`、`--bg-surface`、`--border`、`--text-primary`、`--text-secondary` |
| `plugins/netease-music/frontend/js/app.js` | `NeteaseApp`：视图分发、Bridge 调用、列表渲染、HTML 转义、挂载统一刷新控件 | `NeteaseApp`、`icon`、`init`、`call`、`_mountFreshness`、`_reloadView`、`loadDaily`、`searchSongs`、`loadLiked`、`loadPlaylists`、`searchPlaylists`、`renderLogin`、`renderSongs`、`renderPlaylists`、`esc`、`Bridge.call`、`Toast.error`、`Freshness.mount`、`Freshness.autoSync`、`parent.mediaPlayerApp`、`media.core.setQueue` |

- `plugins/netease-music/frontend` 下没有样式文件：全部样式写在 `plugins/netease-music/frontend/index.html` 的 `<style>` 内；没有单独的依赖目录，也没有构建步骤。
- 落盘产物：数据根与 `cache_dir` 下无产出（未覆写 `get_data_root`、`get_cache_dir`）；写入的是 `ncm-preference.json`（`_preference_file`，读写符号 `analyze_preference`、`_calculate_preference`）、`config.json`（`_ensure_player_mpv_config`，位于 `Path.home` 下的 `ncm-cli` 目录）、`mpv.sock`（`resolve_song_url`）、`omnibox-ncm-fake-mpv` 目录下的 `mpv.py`、`mpv.cmd`、`mpv`（`_ensure_fake_mpv`）、`ncm-captured-url.txt` 与 `ncm-fake-mpv.log`（`resolve_song_url`）；`ncm-history.json`（`_history_file`）与 `ncm-schedule.json`（`_schedule_file`）只赋值，无读写符号。

## 后端 API 契约

`main.py` 的 `register_api` 登记 12 个方法，壳把插件名与方法名拼成 API 名（`plugin_manager.py` 里的 `{name}__{method_name}`）；宿主 `media-player` 用 `Bridge.callPlugin` 按同一张方法表调用。失败时统一返回 `success` 为 `False` 并带 `error`，`results`、`url`、`data` 退化为空值。

| 方法 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `search_song` | `keyword` | `{success, results}` | 关键词搜歌，宿主搜索框命中 |
| `search_playlist` | `keyword` | `{success, results}` | 关键词搜歌单；宿主 `ncm-playlists` 无关键词时传 `推荐` |
| `get_daily_recommend` | 无 | `{success, results}` | 每日推荐，宿主 `ncm-daily` 调用 |
| `get_liked_songs` | `limit`，默认 `100` | `{success, results}` | 红心歌曲，先取「我的喜欢」歌单的 `id` |
| `get_created_playlists` | `limit`，默认 `100` | `{success, results}` | 创建的歌单 |
| `get_collected_playlists` | `limit`，默认 `100` | `{success, results}` | 收藏的歌单 |
| `get_playlist_tracks` | `playlist_id`、`limit` 默认 `100`、`offset` 默认 `0` | `{success, results}` | 歌单曲目 |
| `get_song_url` | `song_id`、`original_id` 默认 `None` | `{success, url}` | 播放地址；缺 `original_id` 时 `url` 为空串 |
| `get_lyric` | `song_id` | `{success, data}` | 歌词对象，宿主 `lyrics-parser.js` 调用 |
| `check_login` | 无 | `{success}` | `success` 表示已登录，非调用是否成功 |
| `login` | `background`，默认 `True` | `{success, message}` | 触发 `login --background`；`message` 取二维码地址或提示文本 |
| `get_status` | 无 | `{plugin, ncm_cli_available, ncm_cli_version, error}` | 对 `ncm-cli` 执行 `--version` 探活，宿主在各 ncm 视图加载前调用 |

条目形状由 `_song`、`_playlist` 归一，字段取值如下。

- 歌曲：`id`、`original_id`、`name`、`artists`、`album`、`duration`、`url`、`cover_url`；`artists` 为字符串数组，`duration` 单位为毫秒，`url` 为 `music.163.com` 网页链接。
- 歌单：`id`、`original_id`、`name`、`track_count`、`play_count`、`cover_url`。

壳按名调用的成员如下。

| 成员 | 入参 | 返回 | 用途 |
| --- | --- | --- | --- |
| `register_api` | 无 | 方法名到可调用对象的字典 | 装载期把方法名登记进 `{name}__{method_name}` 形式的方法表 |
| `get_extensions` | 无 | 扩展条目列表 | 壳用 `get_plugin_extensions` 聚合后交给宿主渲染；条目字段 `host`、`id`、`label`、`icon`、`view`、`placement`、`scope` |
| `freshness_spec` | 无 | 新鲜度声明字典 | 接入统一刷新；字段 `mode`、`ttl_seconds`、`invalidate`、`unit` |
| `settings_schema` | 类属性 | `[]` | 本插件未声明设置项，壳读默认值并登记 `get_settings_schema` 方法名 |

`get_extensions` 的五个条目共享 `host` 为 `media-player`、`placement` 为 `sidebar`、`scope` 为 `all`，逐条取值如下。

| `id` | `label` | `icon` | `view` |
| --- | --- | --- | --- |
| `netease-daily` | `每日推荐` | `icon:music` | `ncm-daily` |
| `netease-playlists` | `推荐歌单` | `icon:list-music` | `ncm-playlists` |
| `netease-liked` | `我的喜欢` | `icon:heart` | `ncm-liked` |
| `netease-my-playlists` | `我的歌单` | `icon:library` | `ncm-my-playlists` |
| `netease-login` | `登录` | `icon:user` | `ncm-login` |

## 前端结构

本插件不声明 `frontend.chrome`，`frontend/index.html` 是**直接打开时才用**的单页（壳的正常入口是宿主侧栏的五个 `view` 条目）。单页 DOM 结构树如下。

- `body`
  - `div.wrap`
    - `h3#title`，`init` 按 `view` 参数写标题
    - `div#ncm-freshness`，`_mountFreshness` 把 `Freshness.mount` 的节点挂在这里
    - `div#search-row`，初始内联 `display:none`，`init` 按视图改为 `block`
      - `input#keyword`
      - `button.btn.btn-primary#search-btn`
    - `div#content`，初始为 `.empty-state`「加载中…」，随后被脚本整体替换
  - 外链 `js/app.js`
  - 内联引导脚本：创建 `window.neteaseApp`，调用 `NeteaseApp` 的 `init`

脚本装载顺序：`js/app.js` 在前，`index.html` 的内联引导脚本在后；`app.js` 不引用任何其他插件脚本。

`view` 参数的取值与标题映射：`daily`、`playlists`、`liked`、`login`。

脚本拼接的类名与属性：`.item`、`.t`、`.s`、`.empty-state`、`#refresh`、`data-idx`，出现在 `renderSongs`、`renderPlaylists`、`renderLogin` 写入的 `innerHTML` 里。

`index.html` 的 `<style>` 定义了 `.wrap`、`.item`、`.t`、`.s` 与未限定作用域的元素选择器 `input`、`button`；颜色与边框全部取自壳变量 `--bg-app`、`--bg-surface`、`--border`、`--text-primary`、`--text-secondary`。

## 与壳的契约

- 壳类与全局：`PluginBase`（`shell/backend/plugin_base.py`）、`Bridge.call`、`Bridge.callPlugin`、`window.Icons.html`、`Toast.error`。
- 壳 CSS 变量：`--bg-app`、`--bg-surface`、`--border`、`--text-primary`、`--text-secondary`（`shell/frontend/public/shell/variables.css`）。
- 壳类：`.btn`、`.btn-primary`、`.empty-state`（`shell/frontend/public/shell/shared.css`、`shell/frontend/public/shell/base.css`）。
- 插件自有 CSS 变量前缀：没有，颜色与边框全部直接取壳变量。
- 共享前端组件：`Freshness`（`shell/frontend/public/shell/freshness.js`），`_mountFreshness` 传入 `plugin`、`container`、`unit`、`onChange`，随后调用 `Freshness.autoSync`；未使用 `FolderPicker`、`Splitter`、`createLightbox`、`createPagination`。
- 跨插件接口：`parent.mediaPlayerApp`（定义在 `plugins/media-player/frontend/index.html`）、`media.core.setQueue`（`MediaPlayerCore.setQueue`，`plugins/media-player/frontend/js/player-core.js`）。
- 共享后端基建：`PluginBase`、`load_sibling`（`shell/backend/plugin_utils.py`）、`FreshnessEngine` 的 `mode` 为 `remote` 分支（`shell/backend/freshness.py`），`invalidate` 指向 `clear_url_cache`。
- 宿主集成：壳的 `PluginExtensions.vue` 用 `system_get_plugin_extensions('media-player', 'sidebar')` 取本插件的五条 `view` 条目并渲染入口；点击后壳发 `omnibox:view`（`ncm-daily` / `ncm-playlists` / `ncm-liked` / `ncm-my-playlists` / `ncm-login`），宿主内容页的 `openNeteaseView` 渲染对应视图。宿主前端按名调用 `register_api` 登记的方法，并把歌曲条目映射为 `ncm:` 前缀的媒体条目。旧宿主的 `renderExtensions` / `#mp-extensions` 容器不再使用。
- 前端样式、共享组件与统一刷新的完整约定见 `docs/plugin-ui-guide.md`。
