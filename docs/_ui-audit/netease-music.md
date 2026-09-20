# netease-music 插件 UI 现状取证

> 只读审计。契约基准：`shell/frontend/public/shell/{variables.css, base.css, effects.css, base.js}`
> 与 `docs/plugin-guide.md` §4.1 / §4.3 / §4.4。所有结论附 `文件路径:行号`。
>
> **本插件的前端在正常导航下不可达**（见 §1），因此下面既记录它在的写法，也记录「谁在真正渲染这 4 个视图」。

## 1. 概览

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

## 2. 布局骨架

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

## 3. 设计 token 使用

- **实际用到的 `--*` 变量（全部清单）**：`index.html` 共 9 处（执行正则 `var\((--[a-z0-9-]+)`）：`--bg-app`×1（`:8`）、`--bg-surface`×2（`:10`、`:12`）、`--border`×2（`:10`、`:12`）、`--text-primary`×2（`:8`、`:10`）、`--text-secondary`×2（`:14`、`:15`）。**5 个名字全部真实存在于 `variables.css`**（依次见 `variables.css:6 / 7 / 24 / 16 / 17`，深色版本 `:56 / 57 / 72 / 65 / 66`）。`js/app.js` 里 0 处 `var()`（模板只写类名）→ 颜色集中在 `index.html` 的 10 行样式里。
- **覆盖 `:root` / 自有 `--xxx`**：都没有（`:root` 0 处，`--x:` 声明 0 处）。
- **硬编码颜色字面量**：`#[0-9a-fA-F]{3,8}\b` = **0 处**、`rgba?\(` = **0 处**（`index.html` 与 `js/app.js` 均为 0）。唯一的颜色重复是 `index.html:8` 又写了一遍 `background: var(--bg-app); color: var(--text-primary)`，与 `base.css:8-9` 完全重复（等优先级下壳的后者胜出，无害）。
- **`data-theme`**：插件 0 处处理（grep `data-theme` = 0），完全依赖壳注入的 MutationObserver 把父窗口的 `data-theme` 写到本 iframe（`shell/backend/file_server.py:69-76`）。因为 token 名全部正确，**深浅主题对它天然生效**——这是这份前端里唯一完全合规的一层。
- **内联样式**：`style="` 仅 1 处（`index.html:21`），无行内颜色。
- **逐项数值对照（同一语义、三套写法）**：圆角 `8px`（`.item`:12、`input`:10）vs 壳 `--radius:6px` / `--radius-sm:4px`（`variables.css:40-41`）；输入内边距 `8px 12px`（`:10`）vs `.search-input` `6px 12px`（`base.css:41`）vs `.field input` `6px 10px`（`base.css:191`）；输入最大宽 `360px`（`:10`）vs `400px`（`base.css:40`）；空态 `padding:40px`/12px 字（`:15`）vs `.loading` `padding:50px`/15px（`base.css:427`）vs `.empty-state` `min-height:300px`/16px 且 flex 居中（`base.css:428-431`）。

## 4. 组件与命名约定

- **类名前缀**：**没有前缀**。实际类名只有 4 个：`.wrap`（`index.html:9`、`:19`）、`.item` `:12` / `.t` `:13` / `.s` `:14`（由 `app.js:80-83`、`:114-118` 的 innerHTML 动态产生）、`.empty`（`index.html:15`、`:25`），另复用壳的 `.btn/.btn-primary`（`:23`）。`.t`/`.s`/`.item` 这类极短通用名与宿主（media-player 的列表项）语义相同、名字不同，也没有命名空间隔离。
- **自有组件清单**：条目卡（`.item`，`app.js:79-83`/`:114-118`）、空态（`.empty`，`app.js:68/72/78/113`）、搜索行（`#search-row`）。**没有**：弹窗、菜单、进度条、徽标、骨架屏、分页、toast（借用壳）、右键菜单。
- **Shell 已提供但插件又自己实现了一遍**：

| # | 能力 | Shell 提供 | 插件实现 | 差异 |
| --- | --- | --- | --- | --- |
| 1 | 输入框样式 | `.search-input`（`base.css:39-50`）、`.field input`（`:187-200`） | `input{…}`（`index.html:10`） | padding `8px 12px` vs `6px 12px`；radius `8px` vs `4px`；max-width `360px` vs `400px`；未写 `:focus` 的 `border-color: var(--accent)`（壳版有） |
| 2 | 空态 / 加载态 | `.empty-state`（`base.css:428-431`）、`.loading`（`:427`） | `.empty`（`index.html:15`，`app.js:25` 初值即加载态） | 壳版 flex 居中 + `min-height:300px` + 16px；插件版 `padding:40px` + 12px，无最小高度、无居中布局 |
| 3 | 卡片/列表网格 | `createCardGrid`（`base.js:915-954`，media-player 的媒体卡即此风格） | 手写 `.item` + innerHTML（`app.js:79-83`） | 无封面图（后端其实返回 `cover_url`，`backend/main.py:148`、`:159`）、无徽标、无 hover 反馈（壳侧可用 `.obx-card-lift`，`effects.css:107`）、无键盘焦点 |
| 4 | HTML 转义 | `Utils.escapeHtml`（`base.js:340-348`，同时转义引号、可用于属性） | 自写 `esc()`（`app.js:121-123`） | 行为接近（转 `& < > " '`），但没有 `Utils.jsString`（`base.js:352-358`）对应的「字符串 → 属性」两步转义入口；两处实现并存 |
| 5 | 跨插件播放 | `Bridge.callPlugin`（`base.js:195-199`）可用于后端 API，但没有「把曲目交给播放器」的跨插件接口 | `parent.mediaPlayerApp` 直取全局（`app.js:89`、`:102-104`） | 绕开 Bridge 封装；且该全局只在 media-player 自己的 iframe 里存在（`plugins/media-player/frontend/index.html:273`），见 §7.2 |

## 5. 交互约定

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

## 6. 特色设计（值得其他插件吸收）

1. **一个 HTML + 一个 `?view=` 参数承载四视图**（`app.js:8-12`）：标题映射表 + 三个加载分支就覆盖 daily/playlists/liked/login，省掉 4 个页面与路由，适合「视图之间只差数据源」的宿主扩展。
2. **后端返回结构直接当视图模型**：`backend/main.py:138-160` 的 `_song()/_playlist()` 已经裁到前端需要的字段，前端不做 DTO 转换、直接拼串（`app.js:79-83`、`:114-118`）。
3. **在只有 10 行样式的极简壳里仍复用宿主反馈组件**：`Toast.error`（`app.js:88`、`:105`）而不是 `alert`——比同仓库的 pixiv-sync（22 处 alert）合规得多。
4. **职责边界清楚**：点歌链路只做「取播放地址 → 交给播放器队列」（`app.js:87-106`），解码/进度/歌词全部留给 media-player（`plugins/media-player/frontend/js/player-core.js:201`、`lyrics-parser.js:118`），插件自己不碰 `<audio>`。
5. **列表渲染的注入面收得较窄**：主副标题都过 `esc()`（`app.js:82`），并用 `data-idx` + 闭包回取原始对象（`:80`、`:86`），没有把 JSON 塞进 HTML 属性。

## 7. 与 Shell 契约的偏差

1. **整份前端是同一功能的第二实现，且不可达**：`manifest.json:16` `hidden:true` → `plugin_manager.py:249` 排除出前端清单 → `App.vue:269-277`、`:293-322` 不会渲染它；同一批视图由 media-player 原生实现（`app-views.js:190-194`、`:240-316`；`app-render.js:190-233`）。影响面：`frontend/` 全部 154 行，以及任何后续在这份前端上做的 UI 改动都不生效。门禁口径：`tests/test_plugin_frontend_assets_js.py:6` 明确点名「image-cleaner / netease-music 各 1 个完全没有门禁」，现由 `tests/js/plugin_asset_contract.mjs:1-7`、`:30-32` 的 `runAssetContract` 至少覆盖装载期错误。
2. **`parent.mediaPlayerApp` 在真实壳路径下必然失败**：`app.js:89` 直接取父窗口全局，而该全局只在 media-player 自己的 iframe 内定义（`plugins/media-player/frontend/index.html:273-274`）；本页若由壳加载，`parent` 是壳窗口（`App.vue`），没有该对象 → 点击必然落到 `Toast.error('无法访问 media-player 播放器')`（`app.js:105`）。影响面：`renderSongs` 的全部点击行为（`:84-108`），即这页唯一的核心功能。
3. **零 Shell 布局类**：`.wrap`（`index.html:9`）取代了 `.view-body`/`.view-content`（`base.css:237`、`:277`），没有 48px 工具栏、没有 `padding:16px` 内容区之外的对齐约定 → 后续统一 UI 在这页没有任何可复用的锚点；同类极简壳还有另一个后果：`input`/`button`（`:10-11`）是**元素选择器、未限定作用域**，会波及壳在本 iframe 内渲染的控件（如 `.field input` 被追加 `width:100%; max-width:360px`，`.modal-footer .btn` 被追加 `margin-left:8px`）。
4. **滚动契约缺失**：`base.css:11` 的 `body{overflow:hidden}` 生效，而本页没有 `html/body{height:100%}`、也没有任何 `overflow-y:auto` 容器（对比 pixiv-sync `index.html:8-11` 的显式高度链）→ 超过视口的歌曲列表不可达；而 shell 早就为这件事准备了 `.view-content`（`base.css:277-282`）+ `.obx-scroll`（`effects.css:140`）。
5. **输入框/空态各写一套**：`input`（`index.html:10`）、`.empty`（`:15`）与 `base.css` 的 `.search-input`（`:39-50`）、`.empty-state`（`:428-431`）、`.loading`（`:427`）三套并存，圆角 8px vs 4px/6px、空态无 `min-height` 与居中。附带：`#content` 的 `class="empty"` 永不移除（`classList` 全文 0 次），渲染后的列表仍吃空态样式（40px padding + 居中 + 次级文字色）。
6. **没有设置契约**：既没有 `settings_schema`、也没有 `openSettingsModal` 入口，与 `readme.md:68`「插件设置项在插件页面里通过『⚙ 设置』打开：壳的 `openSettingsModal()` 按该插件的 `settings_schema` 渲染表单」不一致；配置只能靠终端（`app.js:72`）。
7. **异常与生命周期约定未接**：6 处桥调用无 try/catch（`app.js:35-55`）→ 失败静默停在「加载中...」；0 处 `onShow/onHide/onDispose`（若这份前端将来被复活并声明 `keepAlive`，就没有钩子可停轮询/复播）；键盘只有 Enter（`:17`、`:26`），没有 Esc 关弹窗/播放控制的统一约定。
8. **与宿主重复的登录流程，文案已漂移**：`app.js:63-74` 与 `media-player/frontend/js/app-render.js:190-233` 是同一段「查登录 → 未登录则请去终端执行 ncm-cli → 点『我已登录』重新检测」的两份实现；提示文案已经不一致（前者 `ncm-cli configure` + `ncm-cli login` 两行，后者 `ncm-cli configure 和 ncm-cli login` 一行），改一处不会同步另一处。
