# 网易云音乐插件（netease-music）设计文档

> 版本：v0.1（已实装）
> 目标形态：**Companion 插件**（宿主 `media-player`）
> 本文档介绍插件**当前实现**的设计与数据流；§5 收录本插件 UI 现状取证的基线原文，§6 记录复核时与 HEAD 的差异。

## 1. 定位

`netease-music` 是 `media-player` 的伴侣插件，提供**网易云在线音乐**能力：每日推荐、推荐歌单（含歌单搜索）、红心歌曲、创建 / 收藏的歌单、歌单曲目、歌曲搜索、播放地址、歌词、登录状态。它不保存也不解码音频：播放、进度、歌词渲染都在 media-player，插件自己不碰 `<audio>`。

### 1.1 与宿主的关系

- `manifest.json:7` `dependencies: ["media-player"]`，由 `PluginManager` 保证宿主先加载。
- 后端 `NeteaseMusicPlugin`（`plugins/netease-music/backend/main.py:10`）继承 `PluginBase`，只做两件事：`get_extensions()`（`:20-32`）把 5 个入口注册到 media-player 侧栏，`register_api()`（`:34-48`）暴露 12 个方法；ncm-cli 封装在同级 `backend/netease_music_api.py` 的 `NeteaseMusicAPI`，由 `load_sibling` 加载（`main.py:6-7`）。
- 不修改 media-player 宿主代码；宿主侧的消费链见 §4。
- `manifest.json:16` `"hidden": true`：不出现在 Shell 主导航，也**不产生壳内可达的插件页面**（见 §1.3）。

### 1.2 ncm-cli 包装（CLI wrapper）

- 后端自身不发任何 HTTP 请求：所有数据来自本机 `ncm-cli`（npm 全局包 `@music163/ncm-cli`）的 `--output json` 输出，由 `_run_command()`（`backend/netease_music_api.py:126-163`）执行，`_parse_songs()` / `_parse_playlists()` / `_load_json()` / `_extract_list()`（`:592-730`）解析。
- Windows 上优先定位包内 node 入口直连执行（`_ncm_argv_prefix()` `:71-82`，入口 `node_modules/@music163/ncm-cli/dist/index.js`），拿不到才退化到 `.cmd/.ps1` shim，并在退化路径上拒绝全部 cmd 元字符（`:85-89`、`:139-142`）。
- 播放地址靠**假 mpv 截获**：启动时把 `~/.config/ncm-cli/config.json` 的 `player` 写成 `mpv`（`:108-124`），再把假 mpv 脚本写进私有临时目录 `<tempdir>/omnibox-ncm-fake-mpv`（POSIX 上 chmod 0700，`_private_bin_dir()` `:403-418`）；脚本（正文 `:420-513`）起 Unix socket 接 `--input-ipc-server`，把 `loadfile` 的参数写进同目录的 `ncm-captured-url.txt`（等待与回退逻辑 `:336-401`）；截获不到就回退公共外链 `https://music.163.com/song/media/outer/url?id=<original_id>.mp3`。URL 按加密 id 短期缓存，默认上限 100 条（`:95-101`）。
- `Song.url` / `Playlist.url` 只是 `https://music.163.com/#/song?id=…` / `#/playlist?id=…` 网页链接（`:33-35`、`:47-49`），不是播放地址，播放链路不使用它们。
- 依赖只有宿主 venv 的标准库（`subprocess` / `json` / `re` / `shlex` / `shutil` / `tempfile` 等）：没有 `backend/libs`，没有 vendored 第三方代码，也没有 `runtime` / 独立 venv。
- **无设置项**：后端没有 `settings_schema`，`register_api()` 也没有登记 `get_settings` / `save_settings`；配置与登录只能走终端 `ncm-cli configure` / `ncm-cli login`（详见 §5.5、§5.7 第 6 条）。

### 1.3 本插件的 `frontend/` 在壳里不可达（本插件最重要的事实）

- `manifest.json:12-15` 虽然声明了 `frontend.entry` / `frontend.route`，但 `manifest.json:16` 是 `"hidden": true`；`PluginManager.get_frontend_manifests()`（`shell/backend/plugin_manager.py:252-265`）在 `:264` 用 `if not m.get('hidden')` 把 hidden 插件整条排除出前端清单，Shell 前端的导航与 iframe 只按这份清单渲染（`shell/frontend/src/App.vue:36`、`:270-321`）。
- 全仓库没有任何代码构造这份前端要读的 `?view=` URL（grep `view=ncm` 在源码里 0 命中，只在 `docs/` 的说明文字中出现）。
- 界面上真正存在的 5 个视图（每日推荐 / 推荐歌单 / 我的喜欢 / 我的歌单 / 登录）**由 media-player 原生渲染**：扩展条目只带 `view`、宿主传 `onOpen`，壳的 `renderExtensions()` 走「原生视图型扩展」分支（`shell/frontend/public/shell/base.js:271-276`），不会开 iframe——与 image-cleaner 的 `embedUrl` + `onEmbed` 是两条不同路径。消费链见 §4。
- 因此 `plugins/netease-music/frontend/`（`index.html` + `js/app.js`）是同一功能的**第二份实现**，只有手动敲 `/plugins/netease-music/frontend/index.html` 才能打开（该路由存在并注入壳资源：`shell/backend/file_server.py:875-902`）。**在这份前端上做的任何 UI 改动都不会出现在界面里**；反之，界面上的网易云视图要改，改的是 media-player。
- 门禁口径：`tests/test_plugin_frontend_assets_js.py:1-15` 点名 image-cleaner / netease-music「各 1 个完全没有门禁」，现在由 `tests/js/plugin_asset_contract.mjs` 按「声明与磁盘一致 + 按顺序装载」的资源契约至少覆盖装载期错误。

## 2. 目录结构

```
plugins/netease-music/
├── manifest.json              # dependencies:["media-player"], hidden:true, version 0.1.0
├── backend/
│   ├── main.py                # NeteaseMusicPlugin：get_extensions() / register_api() / 12 个 API 方法
│   └── netease_music_api.py   # NeteaseMusicAPI：ncm-cli 调用与解析、假 mpv 取流（730 行）
└── frontend/                  # 壳内不可达（manifest.hidden:true），仅手动打开时可用
    ├── index.html             # 单页骨架 + 样式块（:7-15，其中规则 7 行），29 行
    └── js/
        └── app.js             # class NeteaseApp，131 行
```

没有 `backend/libs`（不 vendored 第三方）、没有插件级 `readme.md`、没有构建步骤。

## 3. 后端 API 契约（netease-music__*）

`register_api()`（`backend/main.py:34-48`）登记 12 个方法，Shell 侧以 `netease-music__<方法名>` 暴露（`shell/backend/plugin_manager.py:567-570`），并额外自动登记 `netease-music__get_settings_schema`（`:571-573`）——本插件没有 `settings_schema`，它恒返回 `[]`。失败一律返回 `{'success': False, 'error': <异常文本>, ...空值}`（`main.py:63-136`）。

| API | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `search_song` | `keyword` | `{success, results:[Song]}` | `ncm-cli search song --keyword`（`netease_music_api.py:222-226`） |
| `search_playlist` | `keyword` | `{success, results:[Playlist]}` | `ncm-cli search playlist --keyword`（`:228-232`） |
| `get_daily_recommend` | 无 | `{success, results:[Song]}` | `ncm-cli recommend daily --limit 30`（`:292-296`），固定 30 条 |
| `get_liked_songs` | `limit=100` | `{success, results:[Song]}` | 先 `user favorite` 取「我的喜欢」歌单 id，再 `playlist tracks`（`:307-327`）；取不到歌单 id 直接抛错 |
| `get_created_playlists` | `limit=100` | `{success, results:[Playlist]}` | `ncm-cli playlist created --limit`（`:276-280`） |
| `get_collected_playlists` | `limit=100` | `{success, results:[Playlist]}` | `ncm-cli playlist collected --limit`（`:282-286`） |
| `get_playlist_tracks` | `playlist_id, limit=100, offset=0` | `{success, results:[Song]}` | `ncm-cli playlist tracks`（`:265-274`）；API 层默认 `limit=30`，`main.py` 显式传 100 |
| `get_song_url` | `song_id, original_id=None` | `{success, url}` | `original_id` 缺省时**不解析、直接返回空 url**（`:329-334`）；壳内的调用点都传 `(加密 id, original_id)`，见 §4.3 |
| `get_lyric` | `song_id` | `{success, data}` | `ncm-cli song lyric --songId`（`:549-556`），返回响应 `data` 里的歌词对象 |
| `check_login` | 无 | `{success: bool}` | `ncm-cli login --check`；这里的 `success` 是**登录状态**，不是调用是否成功（`:179-184`） |
| `login` | `background=True` | `{success, message}` | `ncm-cli login --background`，`message` 取 `qrCodeUrl` / `clickableUrl` / `message`；**当前壳内没有调用点**（media-player 的登录视图只做终端指引 + `check_login`，见 §4.2） |
| `get_status` | 无 | `{plugin, ncm_cli_available, ncm_cli_version, error}` | 执行 `ncm-cli --version` 探活（`main.py:50-61`）；media-player 在每个 ncm 视图加载前都会先调它 |

两个条目形状（`main.py:138-160`）：

- `Song`：`{id, original_id, name, artists:[str], album, duration(毫秒), url, cover_url}`，其中 `id` 是加密 id、`original_id` 是明文 id。
- `Playlist`：`{id, original_id, name, track_count, play_count, cover_url}`。

ncm-cli 缺失不阻塞插件加载：`_get_api()` 用 `NeteaseMusicAPI(check_install=False)` 懒构造（`main.py:15-18`），安装探活只在 `get_status` 里做，界面据此给安装指引。插件自己的前端（不可达那份）用 `Bridge.call('<方法名>')` 按本插件命名空间调用同一张方法表。

## 4. 与 media-player 的集成

### 4.1 扩展条目（后端侧）

`get_extensions()`（`backend/main.py:20-32`）返回 5 条，公共字段是 `host: 'media-player'` / `placement: 'sidebar'` / `scope: 'all'`：

| id | label | icon | view |
| --- | --- | --- | --- |
| `netease-daily` | 每日推荐 | `icon:music` | `ncm-daily` |
| `netease-playlists` | 推荐歌单 | `icon:list-music` | `ncm-playlists` |
| `netease-liked` | 我的喜欢 | `icon:heart` | `ncm-liked` |
| `netease-my-playlists` | 我的歌单 | `icon:library` | `ncm-my-playlists` |
| `netease-login` | 登录 | `icon:user` | `ncm-login` |

这些条目**不带** `embedUrl` / `route` / `method`，只带 `view`；壳的 `renderExtensions()` 按「`view` + `onOpen` → `embedUrl` → `route` → `method`」的顺序判定（`shell/frontend/public/shell/base.js:271-307`），所以必然走第一支：宿主原生渲染，不加载本插件的前端。

### 4.2 宿主侧消费链

- `plugins/media-player/frontend/js/app.js:181-199` `loadExtensions()`：`renderExtensions(container, 'media-player', 'sidebar', {title: '网易云音乐', onOpen: (ext) => this.openNeteaseView(ext)})`。条目没有 `section`，分组标题落到 `options.title`（`base.js:245`、`:254`），侧栏容器是 `#mp-extensions`（`app.js:182`）。
- `openNeteaseView(ext)`（`app.js:201-208`）：`this.currentView = ext.view || 'ncm-daily'`，清空当前专辑 / 歌单与搜索框，然后 `_loadCurrentView()`。
- `_loadCurrentView()`（`app-views.js`）对 `ncm-` 前缀视图的处理：标题映射表 `:190-202`；先 `Bridge.callPlugin('netease-music', 'get_status')` 探 ncm-cli，不可用就 `_renderNeteaseCliMissing()`（`app-render.js:167-188`，给 `npm install -g @music163/ncm-cli` 与「我已安装，重新检测」）；`ncm-login` 走 `_renderNeteaseLogin()`（`app-render.js:190-233`，已登录给全量同步 / 导入喜欢入口，未登录给终端指引 + 「我已登录」）；有搜索关键字时 `ncm-playlists` 走 `search_playlist`、其余走 `search_song`（`app-views.js:222-238`）。
- 四个数据视图分别调 `get_daily_recommend`（`:249`）、`search_playlist('推荐')`（`:274`）、`get_liked_songs(100)`（`:291`）、`get_created_playlists(100)` + `get_collected_playlists(100)`（`:314-317`），并带 localStorage 缓存（`_ncmCacheGet` `:126`；每日推荐按日失效，接口失败的空结果不入缓存）；歌单详情再调 `get_playlist_tracks`（`app-render.js:292`、`:515`）。
- 返回的 Song / Playlist 由 `_neteaseToMediaItem(song)`（`app-render.js:150-165`）映射成 media-player 条目：`id: 'ncm:' + original_id`、`ncm_encrypted_id`、`online: true`，之后进入媒体列表 / 队列。

### 4.3 播放与歌词的归属

- 播放地址：media-player 侧三处调 `get_song_url`，都传 `(加密 id, original_id)`（`player-core.js:201`、`app-views.js:173`、`app-render.js:610`）；歌词由 `lyrics-parser.js:119` 调 `get_lyric(加密 id)`。
- 解码、进度记忆、EQ、队列、歌词渲染全在 media-player（见 `docs/media-player-design.md`）；本插件不产出音频、不持有播放状态。
- 「网易云 → 本地」的匹配与歌单镜像同样只走这几个 API，实现在 media-player 前端（`docs/media-player-design.md`「网易云 → 本地（可选能力）」）。
- 不可达的那份插件前端另有第三条路径：点曲目时直接读 `parent.mediaPlayerApp`（`frontend/js/app.js:96`、`:109-113`），而该全局只定义在 media-player 自己的 iframe 里（`plugins/media-player/frontend/index.html:273-274`），壳窗口没有这个对象——这是 §5.7 第 2 条的成因。

## 5. UI 现状取证（前端与壳契约对照）

> 只读审计。契约基准：`shell/frontend/public/shell/{variables.css, base.css, effects.css, base.js}`
> 与 `docs/plugin-guide.md` §4.1 / §4.3 / §4.4。所有结论附 `文件路径:行号`。
>
> **本插件的前端在正常导航下不可达**（见 §5.1），因此下面既记录它在的写法，也记录「谁在真正渲染这 4 个视图」。
>
> 本节为审计基线原文（只重编号标题、修正文内交叉引用），与复核时 HEAD 的差异见 §6。

### 5.1 概览

**前端文件清单**

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `plugins/netease-music/frontend/index.html` | 30（1389 B） | 唯一页面入口（`manifest.json:12-15` `frontend.entry/route`）；含 `<style>` 1 块（`:7-16`，10 行）+ 内联 `<script>` 1 行（`:28`） |
| `plugins/netease-music/frontend/js/app.js` | 124（4753 B） | 单个类 `NeteaseApp`（`:1-124`）：视图分发、4 个 Bridge 调用、列表 innerHTML 渲染、转义。**无模块拆分** |
| `plugins/netease-music/frontend/*.css` | — | **不存在**：全部样式在 `index.html:7-16` |

- **页面形态（名义上）**：单页四视图。`app.js:8-12` 从 `location.search` 读 `?view=` 决定标题与初始加载：`daily`（每日推荐）/ `playlists`（推荐歌单）/ `liked`（我的喜欢）/ `login`（登录），分支见 `app.js:14-28`。
- **页面形态（实际上）**：**没有任何外部入口构造这个 `?view=` URL**——全仓库 grep `view=ncm` 命中 0 处；导航与 iframe 也不会走到它：`manifest.json:16` `"hidden": true`，壳在生成前端清单时把 hidden 插件整条排除（`shell/backend/plugin_manager.py:237-250`，判断在 `:249`），App.vue 的导航与 iframe 渲染都只按这份清单（`shell/frontend/src/App.vue:269-277`、`:293-322`）。只有手动敲 `/plugins/netease-music/frontend/index.html` 才能打开（该路由存在并会注入壳资源，`shell/backend/file_server.py:771-798`）。
- **真正的 4 个视图在宿主里**：netease 通过 `get_extensions()` 把 5 个入口注册到 media-player 侧栏（`backend/main.py:20-32`，`view` 字段为 `ncm-daily/ncm-playlists/ncm-liked/ncm-my-playlists/ncm-login`），media-player 用 `onOpen` **原生渲染**而不是 iframe（`plugins/media-player/frontend/js/app.js:159-186`，`openNeteaseView` 在 `:179-186`），四视图内容见 `plugins/media-player/frontend/js/app-views.js:190-194`、`:240-316`，登录视图见 `plugins/media-player/frontend/js/app-render.js:190-233`。→ 本插件的 `frontend/` 是同一功能的**第二份实现**。
- **不是外部页面 / 不是 WebView**：后端是纯 CLI 包装——`backend/netease_music_api.py:2` 注释「基于 ncm-cli」，`:71-124` 配置 ncm-cli 的 `player=mpv`，`:329-401` 用假 mpv 脚本抓流地址，没有任何 HTML 或页面 URL 产出；模型上的 `url` 只是 `music.163.com/#/song?id=…` 网页链接（`:34-35`、`:48-49`），前端从未使用。音频播放由 media-player 的 core 承担（`plugins/media-player/frontend/js/player-core.js:201`）。
- **是否使用 Shell 布局类**：**0 个布局类**。grep `view-body` / `view-toolbar` / `view-content` / `view-sub-sidebar` / `obx-nav-item` / `obx-scroll` / `empty-state` / `obx-` 均无命中；只用了 `.btn .btn-primary`（`index.html:23`）与自建的 `.wrap/.item/.empty`（`index.html:9/12/15`）。
- **壳资源注入对它的影响**：`</head>` 前注入 `variables.css/base.css/folder-picker.css/effects.css/base.js`（`shell/backend/file_server.py:59-66`、`:797-798`），即插件的 `<style>`（`:7-16`）在壳样式**之前** → 同优先级规则壳胜（例如 `base.css:4` 的 `*{box-sizing:border-box;margin:0;padding:0}` 与 `base.css:6-11` 的 `body{overflow:hidden}` 都生效）。

### 5.2 布局骨架

顶层结构树（`index.html:18-26`）：

- `body`（`index.html:18`）——无 class、无 `height`、无 `overflow`
  - `div.wrap`（`:19`；`index.html:9` `padding:16px`）
    - `h3#title`（`:20`）——四视图共用，`app.js:10-12` 写文本
    - `div#search-row[style="display:none;margin-bottom:10px;"]`（`:21`，**唯一的内联 style**）
      - `input#keyword[placeholder="搜索歌曲..."]`（`:22`）
      - `button.btn.btn-primary#search-btn`（`:23`，`base.css:14-36` + `:28-29`）
    - `div#content.empty`（`:25`，初始文本「加载中...」）

**尺寸与滚动来源**

| 部位 | 数值 | 来源 |
| --- | --- | --- |
| 页面容器 | `padding:16px`，无高度、无宽度约束 | 插件自定义 `.wrap`（`index.html:9`）；未用 `.view-body`（`base.css:237`） |
| 工具栏高度 | 无工具栏 | 未使用 `--toolbar-height`/`.view-toolbar`（`base.css:239-248`） |
| 导航侧栏宽度 | 无侧栏 | 未使用 `--nav-width`/`--sub-sidebar-width`（`variables.css:37-38`） |
| 内容区滚动方式 | **无滚动容器** | `base.css:11` `body{overflow:hidden}` 生效，而 `html/body` 没有 `height`（对比 pixiv 的 `index.html:9` + 注释）、`.wrap` 也没有 `overflow` → 列表超过视口高度时既无滚动条也不可滚（受影响：`daily`/搜索/`liked` 的歌曲列表，`app.js:79-83` 一次性渲染全部 `results`，无分页） |
| 搜索输入框 | `width:100%; max-width:360px; padding:8px 12px; border-radius:8px` | 插件自定义（`index.html:10`） |
| 按钮 | 除壳 `.btn` 外仅 `button{margin-left:8px}` | 插件自定义**元素选择器**（`index.html:11`，未限定作用域） |
| 列表项 | `display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:8px; margin-bottom:6px` | 插件自定义 `.item`（`index.html:12`） |
| 条目主/副标题 | `.t{font-weight:600}`、`.s{font-size:12px; color:var(--text-secondary)}` | 插件自定义（`index.html:13-14`） |
| 空态 | `padding:40px; text-align:center; color:var(--text-secondary)` | 插件自定义 `.empty`（`index.html:15`） |

### 5.3 设计 token 使用

- **实际用到的 `--*` 变量（全部清单）**：`index.html` 共 9 处（执行正则 `var\((--[a-z0-9-]+)`）：`--bg-app`×1（`:8`）、`--bg-surface`×2（`:10`、`:12`）、`--border`×2（`:10`、`:12`）、`--text-primary`×2（`:8`、`:10`）、`--text-secondary`×2（`:14`、`:15`）。**5 个名字全部真实存在于 `variables.css`**（依次见 `variables.css:6 / 7 / 24 / 16 / 17`，深色版本 `:56 / 57 / 72 / 65 / 66`）。`js/app.js` 里 0 处 `var()`（模板只写类名）→ 颜色集中在 `index.html` 的 10 行样式里。
- **覆盖 `:root` / 自有 `--xxx`**：都没有（`:root` 0 处，`--x:` 声明 0 处）。
- **硬编码颜色字面量**：`#[0-9a-fA-F]{3,8}\b` = **0 处**、`rgba?\(` = **0 处**（`index.html` 与 `js/app.js` 均为 0）。唯一的颜色重复是 `index.html:8` 又写了一遍 `background: var(--bg-app); color: var(--text-primary)`，与 `base.css:8-9` 完全重复（等优先级下壳的后者胜出，无害）。
- **`data-theme`**：插件 0 处处理（grep `data-theme` = 0），完全依赖壳注入的 MutationObserver 把父窗口的 `data-theme` 写到本 iframe（`shell/backend/file_server.py:69-76`）。因为 token 名全部正确，**深浅主题对它天然生效**——这是这份前端里唯一完全合规的一层。
- **内联样式**：`style="` 仅 1 处（`index.html:21`），无行内颜色。
- **逐项数值对照（同一语义、三套写法）**：圆角 `8px`（`.item`:12、`input`:10）vs 壳 `--radius:6px` / `--radius-sm:4px`（`variables.css:40-41`）；输入内边距 `8px 12px`（`:10`）vs `.search-input` `6px 12px`（`base.css:41`）vs `.field input` `6px 10px`（`base.css:191`）；输入最大宽 `360px`（`:10`）vs `400px`（`base.css:40`）；空态 `padding:40px`/12px 字（`:15`）vs `.loading` `padding:50px`/15px（`base.css:427`）vs `.empty-state` `min-height:300px`/16px 且 flex 居中（`base.css:428-431`）。

### 5.4 组件与命名约定

- **类名前缀**：**没有前缀**。实际类名只有 4 个：`.wrap`（`index.html:9`、`:19`）、`.item` `:12` / `.t` `:13` / `.s` `:14`（由 `app.js:80-83`、`:114-118` 的 innerHTML 动态产生）、`.empty`（`index.html:15`、`:25`），另复用壳的 `.btn/.btn-primary`（`:23`）。`.t`/`.s`/`.item` 这类极短通用名与宿主（media-player 的列表项）语义相同、名字不同，也没有命名空间隔离。
- **自有组件清单**：条目卡（`.item`，`app.js:79-83`/`:114-118`）、空态（`.empty`，`app.js:68/72/78/113`）、搜索行（`#search-row`）。**没有**：弹窗、菜单、进度条、徽标、骨架屏、分页、toast（借用壳）、右键菜单。
- **Shell 已提供但插件又自己实现了一遍**：

| # | 能力 | Shell 提供 | 插件实现 | 差异 |
| --- | --- | --- | --- | --- |
| 1 | 输入框样式 | `.search-input`（`base.css:39-50`）、`.field input`（`:187-200`） | `input{…}`（`index.html:10`） | padding `8px 12px` vs `6px 12px`；radius `8px` vs `4px`；max-width `360px` vs `400px`；未写 `:focus` 的 `border-color: var(--accent)`（壳版有） |
| 2 | 空态 / 加载态 | `.empty-state`（`base.css:428-431`）、`.loading`（`:427`） | `.empty`（`index.html:15`，`app.js:25` 初值即加载态） | 壳版 flex 居中 + `min-height:300px` + 16px；插件版 `padding:40px` + 12px，无最小高度、无居中布局 |
| 3 | 卡片/列表网格 | `createCardGrid`（`base.js:915-954`，media-player 的媒体卡即此风格） | 手写 `.item` + innerHTML（`app.js:79-83`） | 无封面图（后端其实返回 `cover_url`，`backend/main.py:148`、`:159`）、无徽标、无 hover 反馈（壳侧可用 `.obx-card-lift`，`effects.css:107`）、无键盘焦点 |
| 4 | HTML 转义 | `Utils.escapeHtml`（`base.js:340-348`，同时转义引号、可用于属性） | 自写 `esc()`（`app.js:121-123`） | 行为接近（转 `& < > " '`），但没有 `Utils.jsString`（`base.js:352-358`）对应的「字符串 → 属性」两步转义入口；两处实现并存 |
| 5 | 跨插件播放 | `Bridge.callPlugin`（`base.js:195-199`）可用于后端 API，但没有「把曲目交给播放器」的跨插件接口 | `parent.mediaPlayerApp` 直取全局（`app.js:89`、`:102-104`） | 绕开 Bridge 封装；且该全局只在 media-player 自己的 iframe 里存在（`plugins/media-player/frontend/index.html:273`），见 §5.7 第 2 条 |

### 5.5 交互约定

- **设置入口**：**无**。四个视图里没有设置按钮，0 处 `openSettingsModal`（grep = 0），后端也没有 `settings_schema`（`plugins/netease-music/backend/main.py` 全文无该属性，只有 `get_extensions` `:20` 与 `register_api` `:34`）。登录/配置只能引导用户去终端：`app.js:72` 直接给出 `ncm-cli configure` / `ncm-cli login`。
- **保存后的反馈**：无设置可保存；页面内唯一的反馈是 `Toast.error` 2 处——`:88`「获取播放地址失败」、`:105`「无法访问 media-player 播放器」。**这是本页唯一主动使用壳组件的地方**（`Toast` 来自 `base.js:362-391`）。
- **错误提示方式**：桥调用**没有 try/catch**——`app.js:35-37`（loadDaily）、`:40-45`（searchSongs）、`:47-50`（loadLiked）、`:52-55`（loadPlaylists）都是裸 `await`；全文只有 1 处 `try/catch`（`:65-71`，`renderLogin` 里查登录状态，且 `catch(e){}` 空吞）。后端/桥不可用时表现为 unhandled rejection，页面停在初始的「加载中...」（`index.html:25`）。错误文案没有专属样式类。
- **选择模型**：单击 `.item` 立即播放（`app.js:84-107`），无多选、无复选框、无「加入队列」二级动作；歌单条目 `renderPlaylists`（`:111-119`）**连点击监听都没有**，是纯展示。
- **右键菜单**：无（grep `contextmenu` = 0）。
- **键盘快捷键**：仅 2 处 `keydown`（`app.js:17`、`:26`），都是输入框内 Enter 触发搜索；无 Esc、无上下选曲、无空格播放/暂停。
- **长任务进度与取消**：无。`get_daily_recommend` / `get_liked_songs(100)` / `search_*` 都是一次性 `await`（`:35-55`），期间没有任何 loading 指示，也没有取消入口。
- **空态/加载态/错误态的文案与样式类**：加载 = `#content` 带 `class="empty"` 且文本「加载中...」（`index.html:25`）；空 =「暂无歌曲」（`app.js:78`）/「暂无歌单」（`:113`）；错误 = 未处理。三者共用同一个 `.empty` 类，视觉上不区分。
- **附加缺陷（同一处根因）**：`#content` 的 `class="empty"` **永远不会被移除**——全文 `classList`/`className` 出现 0 次（`index.html` 与 `app.js` 均 0），渲染只是替换 `innerHTML`（`app.js:79`、`:114`）。于是歌曲列表渲染后仍带着 `.empty` 的 `padding:40px; text-align:center; color:var(--text-secondary)`，整个列表被居中并缩进 40px、继承次级文字色。
- **生命周期**：0 处 `onShow`/`onHide`/`onDispose`（grep 三者均 0）；`manifest.json` 未声明 `keepAlive`（默认不保活、离开即卸载），所以目前没有实际的定时器泄漏面。
- **桥调用点**：6 处业务调用，全部经 `this.call()` 包装（包装定义在 `app.js:31-33`）：`:36`、`:43`、`:48`、`:53`、`:66`、`:87`。

### 5.6 特色设计（值得其他插件吸收）

1. **一个 HTML + 一个 `?view=` 参数承载四视图**（`app.js:8-12`）：标题映射表 + 三个加载分支就覆盖 daily/playlists/liked/login，省掉 4 个页面与路由，适合「视图之间只差数据源」的宿主扩展。
2. **后端返回结构直接当视图模型**：`backend/main.py:138-160` 的 `_song()/_playlist()` 已经裁到前端需要的字段，前端不做 DTO 转换、直接拼串（`app.js:79-83`、`:114-118`）。
3. **在只有 10 行样式的极简壳里仍复用宿主反馈组件**：`Toast.error`（`app.js:88`、`:105`）而不是 `alert`——比同仓库的 pixiv-sync（22 处 alert）合规得多。
4. **职责边界清楚**：点歌链路只做「取播放地址 → 交给播放器队列」（`app.js:87-106`），解码/进度/歌词全部留给 media-player（`plugins/media-player/frontend/js/player-core.js:201`、`lyrics-parser.js:118`），插件自己不碰 `<audio>`。
5. **列表渲染的注入面收得较窄**：主副标题都过 `esc()`（`app.js:82`），并用 `data-idx` + 闭包回取原始对象（`:80`、`:86`），没有把 JSON 塞进 HTML 属性。

### 5.7 与 Shell 契约的偏差

1. **整份前端是同一功能的第二实现，且不可达**：`manifest.json:16` `hidden:true` → `plugin_manager.py:249` 排除出前端清单 → `App.vue:269-277`、`:293-322` 不会渲染它；同一批视图由 media-player 原生实现（`app-views.js:190-194`、`:240-316`；`app-render.js:190-233`）。影响面：`frontend/` 全部 154 行，以及任何后续在这份前端上做的 UI 改动都不生效。门禁口径：`tests/test_plugin_frontend_assets_js.py:6` 明确点名「image-cleaner / netease-music 各 1 个完全没有门禁」，现由 `tests/js/plugin_asset_contract.mjs:1-7`、`:30-32` 的 `runAssetContract` 至少覆盖装载期错误。
2. **`parent.mediaPlayerApp` 在真实壳路径下必然失败**：`app.js:89` 直接取父窗口全局，而该全局只在 media-player 自己的 iframe 内定义（`plugins/media-player/frontend/index.html:273-274`）；本页若由壳加载，`parent` 是壳窗口（`App.vue`），没有该对象 → 点击必然落到 `Toast.error('无法访问 media-player 播放器')`（`app.js:105`）。影响面：`renderSongs` 的全部点击行为（`:84-108`），即这页唯一的核心功能。
3. **零 Shell 布局类**：`.wrap`（`index.html:9`）取代了 `.view-body`/`.view-content`（`base.css:237`、`:277`），没有 48px 工具栏、没有 `padding:16px` 内容区之外的对齐约定 → 后续统一 UI 在这页没有任何可复用的锚点；同类极简壳还有另一个后果：`input`/`button`（`:10-11`）是**元素选择器、未限定作用域**，会波及壳在本 iframe 内渲染的控件（如 `.field input` 被追加 `width:100%; max-width:360px`，`.modal-footer .btn` 被追加 `margin-left:8px`）。
4. **滚动契约缺失**：`base.css:11` 的 `body{overflow:hidden}` 生效，而本页没有 `html/body{height:100%}`、也没有任何 `overflow-y:auto` 容器（对比 pixiv-sync `index.html:8-11` 的显式高度链）→ 超过视口的歌曲列表不可达；而 shell 早就为这件事准备了 `.view-content`（`base.css:277-282`）+ `.obx-scroll`（`effects.css:140`）。
5. **输入框/空态各写一套**：`input`（`index.html:10`）、`.empty`（`:15`）与 `base.css` 的 `.search-input`（`:39-50`）、`.empty-state`（`:428-431`）、`.loading`（`:427`）三套并存，圆角 8px vs 4px/6px、空态无 `min-height` 与居中。附带：`#content` 的 `class="empty"` 永不移除（`classList` 全文 0 次），渲染后的列表仍吃空态样式（40px padding + 居中 + 次级文字色）。
6. **没有设置契约**：既没有 `settings_schema`、也没有 `openSettingsModal` 入口，与 `readme.md:68`「插件设置项在插件页面里通过工具栏的「设置」按钮打开：壳的 `openSettingsModal()` 按该插件的 `settings_schema` 渲染表单」不一致；配置只能靠终端（`app.js:72`）。
7. **异常与生命周期约定未接**：6 处桥调用无 try/catch（`app.js:35-55`）→ 失败静默停在「加载中...」；0 处 `onShow/onHide/onDispose`（若这份前端将来被复活并声明 `keepAlive`，就没有钩子可停轮询/复播）；键盘只有 Enter（`:17`、`:26`），没有 Esc 关弹窗/播放控制的统一约定。
8. **与宿主重复的登录流程，文案已漂移**：`app.js:63-74` 与 `media-player/frontend/js/app-render.js:190-233` 是同一段「查登录 → 未登录则请去终端执行 ncm-cli → 点『我已登录』重新检测」的两份实现；提示文案已经不一致（前者 `ncm-cli configure` + `ncm-cli login` 两行，后者 `ncm-cli configure 和 ncm-cli login` 一行），改一处不会同步另一处。

## 6. 复核偏差（写入本文档时对 HEAD 的复核）

§5 是审计基线原文，按约定不改写、不回填。以下差异是撰写本文档时逐条打开对应文件复核（工作区 HEAD = `4157e0a`）得到的，供后续维护者判断该更新哪一侧。

**本插件前端已被后续提交简化，§5.1 的文件清单与若干结论已过期**

- `frontend/index.html` 现 29 行（§5.1 记 30 行 / 1389 B）：样式块在 `:7-15`（§5.1 记 `:7-16`，10 行；现为 7 行规则 + `<style>`/`</style>`），内联脚本在 `:27`（§5.1 记 `:28`）。
- `.empty` 类**已不存在**（§5.1 / §5.4 / §5.5 / §5.7 多处引用它）：`#content` 里改放壳的 `.empty-state`（`index.html:24`），空态统一见提交 `54161d4`（`refactor(plugins,shell): 空态统一到壳的 .empty-state`）。
- 因此 §5.5 的「`#content` 的 `class="empty"` 永远不会被移除」与 §5.7 第 5 条的后半段**已失效**：现在渲染前后都不带这个 class。
- `frontend/js/app.js` 现 131 行（§5.1 记 124 行）：视图分发 `:14-36`、`call()` 包装 `:38-40`、`renderLogin` `:70-81`、`renderSongs` `:83-116`、`renderPlaylists` `:118-126`、`esc` `:128-130`；图标改走 `Icons.html`（`:4-6`，`feat(plugins): 插件前端字形改用图标集` `cd1288f`），文首注释已写明「本插件的 5 个原生视图实际由 media-player 渲染，这份页面只在直接打开时才用到」（`:3`）。
- §5.7 第 1 条的影响面「`frontend/` 全部 154 行」现在是 **160 行**（29 + 131）。
- 复核新发现（不在审计基线里）：这份前端点曲目时调 `get_song_url(song.original_id)`（`frontend/js/app.js:94`），把**明文 id 传给了第一个参数（加密 id）**、`original_id` 留空 → 后端直接返回空 `url`（§3），于是必然先在 `:95` 弹「获取播放地址失败」，走不到 §5.7 第 2 条描述的 `parent.mediaPlayerApp` 分支。壳内真实调用点传的是 `(ncm_encrypted_id, original_id)`（§4.3），media-player 侧不受影响。

**宿主与壳的引用漂移（行号变了，结论不变）**

| 位置 | §5 的引用 | 复核时（HEAD） |
| --- | --- | --- |
| hidden 插件过滤 | `shell/backend/plugin_manager.py:237-250`，判断在 `:249` | `get_frontend_manifests()` `:252-265`，判断在 `:264` |
| media-player 扩展入口 | `plugins/media-player/frontend/js/app.js:159-186`、`openNeteaseView` `:179-186` | `loadExtensions()` `:181-199`（`onOpen` 在 `:187`）、`openNeteaseView()` `:201-208` |
| ncm 视图标题与分支 | `plugins/media-player/frontend/js/app-views.js:190-194`、`:240-316` | 标题表 `:190-202`（ncm 四条在 `:197-200`）、ncm 分支 `:240-339` |
| 插件前端路由 | `shell/backend/file_server.py:771-798` | `serve_plugin_frontend()` `:875-902` |
| 歌词调用点 | `plugins/media-player/frontend/js/lyrics-parser.js:118` | 分支起于 `:117`，真实调用在 `:119` |

**复核后仍逐字成立的引用（抽检）**：`backend/main.py:20-32`（`get_extensions`）、`:34`（`register_api`）、`:138-160`（`_song()` / `_playlist()`）；`backend/netease_music_api.py` 的 `:2`、`:34-35`、`:48-49`、`:71-124`、`:329-401`；`plugins/media-player/frontend/index.html:273`（`window.mediaPlayerApp`）；`plugins/media-player/frontend/js/app-render.js:190-233`（登录视图）；`plugins/media-player/frontend/js/player-core.js:201`；`tests/js/plugin_asset_contract.mjs` 与 `tests/test_plugin_frontend_assets_js.py` 均存在。

**本次独立确认的核心结论**：本插件前端不可达、5 个视图由 media-player 原生渲染（§1.3、§4）——审计里最重要的这条没有被后续提交改变，§5 的全部结论仍然只有「不可达的第二实现」这一个解释。
