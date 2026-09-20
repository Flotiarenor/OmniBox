# 插件 UI 现状取证 · media-player（媒体播放器）

- 取证范围：`plugins/media-player/frontend/`（只读审计，未改动任何插件文件）。
- 对照契约：`shell/frontend/public/shell/variables.css`、`base.css`、`effects.css`、`base.js`、`docs/plugin-guide.md` §4。
- 计数口径：均在 PowerShell 下用 `Select-String -AllMatches` 对该插件目录执行，正则见各节。

---

## 1. 概览

### 1.1 前端文件清单

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `frontend/index.html` | 277 | 唯一入口；全部面板/弹窗的静态骨架 + 脚本装载顺序 |
| `frontend/media-player.css` | 2047（45.7 KB） | 全部样式；无外部 CSS 依赖 |
| `frontend/js/app.js` | 187 | `MediaPlayerApp` 类骨架：构造、`init()`、扫描轮询、扩展入口 |
| `frontend/js/app-ui-events.js` | 227 | `_bindUI` / `_bindContentDelegation` / `_bindThumbPrefetch` |
| `frontend/js/app-views.js` | 438 | 视图切换、扫描、设置弹窗、网易云视图数据 |
| `frontend/js/app-render.js` | 755 | 加载/空态、专辑卡、行列表、详情页、歌单渲染 |
| `frontend/js/app-playback.js` | 418 | 播放栏状态、队列、歌单增删、歌单右键菜单 |
| `frontend/js/app-stage.js` | 367 | 舞台/全屏/宽屏、键盘快捷键、进度条与音量 |
| `frontend/js/player-core.js` | 751 | `MediaPlayerCore`：播放内核、队列、模式、进度持久化 |
| `frontend/js/playlist-manager.js` | 150 | `MediaPlaylistManager`：歌单 CRUD 与侧栏渲染 |
| `frontend/js/lyrics-parser.js` | 331 | `MediaLyrics`：歌词解析、沉浸页、canvas 频谱 |
| `frontend/js/progress-store.js` | 65 | 本地续播位置存储 |
| `frontend/js/frame-extractor.js` | 305 | 前端 canvas 视频抽帧补封面 |
| `frontend/js/utils.js` | 299 | `MPUtils` + `VolumeMapper` + 网易云/本地匹配 |

`app-ui-events/app-views/app-render/app-playback/app-stage` 五个文件都用
`Object.assign(MediaPlayerApp.prototype, {...})` 回挂同一原型（`app-render.js:12`），
装载顺序是硬约束并写在 `index.html:264-271`（"漏挂或错序会在装载期抛
`MediaPlayerApp is not defined`"），由 `tests/js/media_player_app_split.mjs` 把关。

### 1.2 页面形态

单个 iframe 内的**多视图单页**：左侧栏固定，右侧 `view-body` 内按 `currentView`
（`recent` / `audio-albums` / `all-audio` / `video-albums` / `all-video` / `favorites` /
`album-detail` / `playlist:*` / `ncm-*`）整体重渲染 `#media-content`
（`app-views.js:89-99`、`app-render.js`）。另有三个覆盖层：歌词沉浸页
（`#lyrics-page`，`index.html:159`）、队列/均衡器浮层（`#queue-popup`、`#eq-panel`）、
三个弹窗（`#modal-playlist`、`#modal-add-to-playlist`、`#modal-eq-name`，`index.html:206/223/240`）。

**无内嵌 companion iframe**。跨插件协作走 `renderExtensions`：把 `netease-music`
的入口渲染进侧栏 `#mp-extensions`（`app.js:159-177`），点击后切到同 iframe 内的
`ncm-*` 视图（`app.js:179-186`），再用 `Bridge.callPlugin('netease-music', ...)`
取数据（`app-views.js:173/223/249/315`、`app-render.js:193/292/354/515`）。

### 1.3 Shell 布局类使用情况

| Shell 类 | 使用位置 | 次数 |
| --- | --- | --- |
| `.view-body` | `index.html:60` | 1 |
| `.view-toolbar` | `index.html:61`（+ `.mp-toolbar`） | 1 |
| `.toolbar-group` | `index.html:62,66` | 2 |
| `.view-sub-sidebar` | `index.html:12`（+ `.mp-sidebar`） | 1 |
| `.view-content` | `index.html:154`（+ `.mp-content`） | 1 |
| `.obx-nav-item` | `index.html:22-39` | 6 |
| `.obx-scroll` | `index.html:48,186,200,212,229` | 5 |
| `.btn` | `index.html:72,73,74,216,217,233,234,250,251` | 9（其中 `.btn-primary` 3） |
| `.hidden` | `index.html:70,146,181,190,234` | 5 |

**完全未用**：`.sub-sidebar-header`、`.sub-sidebar-footer`、`.empty-state`、`.context-menu`、
`.pagination-bar`、`.settings-form`（类名本身）、`.toast*`、`.modal*`、`.loading`，以及
`effects.css` 的全部 `.obx-anim-*` / `.obx-glass` / `.obx-card-lift` / `.obx-stagger` /
`.obx-skeleton`（正则 `obx-[a-z-]+` 在该插件目录共 16 处命中，全部是 `obx-nav-item` /
`obx-scroll` / `obx-extension`，见 §4.7）。

---

## 2. 布局骨架

### 2.1 顶层容器结构树

```
#app                                    media-player.css:25-32（display:flex; height:100vh; overflow:hidden）
├─ aside.mp-sidebar.view-sub-sidebar    index.html:12 | css:37-48
│  ├─ .mp-brand                         index.html:13-19
│  ├─ nav.mp-nav                        index.html:21-41
│  │  ├─ button.obx-nav-item.mp-nav-item ×6   index.html:22-39（data-view）
│  │  └─ #mp-extensions                  index.html:40（renderExtensions 注入）
│  ├─ .mp-playlist-section              index.html:43-49
│  │  ├─ .mp-section-label > #btn-new-playlist
│  │  └─ #mp-playlist-list.obx-scroll    index.html:48
│  └─ .mp-sidebar-footer#mp-stats        index.html:51-56
└─ .view-body                            index.html:60
   ├─ .view-toolbar.mp-toolbar           index.html:61-76
   │  ├─ .toolbar-group.mp-view-heading   （#mp-view-title / #mp-view-sub）
   │  └─ .toolbar-group.mp-toolbar-right  （.mp-search / 扫描 / 深度扫描 / 设置）
   └─ .mp-main                            css:325-330（flex:1; flex-direction:column）
      ├─ section.mp-stage#mp-stage        css:332-345（height: calc(224px + 76px)）
      │  ├─ .mp-stage-backdrop / .mp-stage-scrim
      │  ├─ .mp-stage-viewport
      │  │  ├─ .mp-stage-art（封面 + 信息 + .mp-stage-lyrics）
      │  │  ├─ .mp-stage-empty
      │  │  ├─ video#video-player
      │  │  ├─ .mp-video-hint
      │  │  └─ #btn-stage-play.mp-stage-big-btn
      │  └─ .mp-playerbar#player-bar       css:649-662（左/中/右三段）
      └─ .view-content.mp-content#media-content   index.html:154
```

### 2.2 关键尺寸与来源

| 项 | 数值 | 来源 |
| --- | --- | --- |
| 侧栏宽度 | `252px` | **插件自定义**：`.mp-sidebar{width:252px}`（`css:38`）覆盖 `base.css:252` 的 `var(--sub-sidebar-width)`（`variables.css:38` = `240px`） |
| 侧栏响应式 | `210px`（≤860px） | 插件自定义（`css:2029`） |
| 工具栏高度 | 未覆盖 → `var(--toolbar-height)` = `48px` | `base.css:240` + `variables.css:39`；`.mp-toolbar` 只改 `gap:16px` 与 `background:transparent`（`css:242-246`） |
| 舞台高度 | `calc(224px + 76px)` = 300px；视频模式 `300+76` = 376px | 插件自定义：`--mp-stage-vh:224px`（`css:12`）、`--mp-pb-height:76px`（`css:13`）、`.mp-stage{height:calc(var(--mp-stage-vh) + var(--mp-pb-height))}`（`css:336`）、`.mp-stage.video-on{--mp-stage-vh:300px}`（`css:348`） |
| 播放栏高度 | `76px` | `css:652` |
| 内容区内边距 | `16px` | `css:872` 二次声明，与 `base.css:280` 同值 |
| 卡片网格 | `repeat(auto-fill, minmax(168px,1fr))`，gap `16px`；≤860px 时 `140px` | `css:994-995`、`css:2033` |
| 滚动方式 | 页面整体不滚动（`#app{overflow:hidden}` `css:28`）；`#media-content` 由 `.view-content` 的 `overflow-y:auto`（`base.css:279`）滚动 | — |
| 浮层层级 | `.mp-pop` 260 / `.mp-lyrics-page` 400 / `.mp-modal` 500 / `.mp-context-menu` 520 | `css:1393/1725/1590/1688` |

插件自定义布局规则集中在：`.mp-main`（`css:325-330`）、`.mp-stage`（`css:332-349`）、
`.mp-playerbar`（`css:649-662`）、`.mp-content`（`css:871-874`）。其余容器全部沿用
`base.css` 的 flex 骨架，未复制 `.view-*` 的定义。

---

## 3. 设计 token 使用

### 3.1 实际用到的 `--*` 变量

`Select-String -Pattern 'var\(--[a-zA-Z0-9-]+' -AllMatches` → 命中 **246 处 / 35 个唯一变量名**。

高频（Shell 提供的）：`--transition-fast` ×36、`--accent` ×31、`--text-secondary` ×21、
`--bg-hover` ×20、`--text-primary` ×20、`--border` ×19、`--text-muted` ×16、
`--bg-surface` ×10、`--danger` ×3、`--text-on-accent` ×2、`--danger-soft` ×2、`--bg-app` ×1、
`--bg-overlay` ×1。

插件自有：`--mp-accent-soft` ×12、`--mp-accent-2` ×8、`--range-val` ×6、
`--mp-pb-height` ×4、`--mp-shadow-2` ×4、`--mp-glass-strong` ×4、`--mp-gradient` ×4、
`--mp-radius-sm` ×3、`--mp-stage-vh` ×2、`--mp-scroll-thumb` ×2、`--mp-radius` ×2、
`--mp-glass` ×2、`--i` ×2、`--mp-scroll-thumb-hover` ×1、`--mp-shadow-1` ×1。
另有一组运行时由 JS 设置的歌词变量：`--lyrics-blur` / `--lyrics-brightness` /
`--lyrics-font-color` / `--lyrics-font-color-active` / `--lyrics-font-color-hover` /
`--lyrics-glow-color` / `--hero-bg`。

### 3.2 自有 `:root` 覆盖

`media-player.css:9-23` 在 `:root` 定义 12 个 `--mp-*`，其中 4 个是自建阴影/圆角体系：

```css
--mp-shadow-1: 0 6px 24px rgba(0, 0, 0, 0.10);   /* css:18 */
--mp-radius: 14px;                                /* css:10 */
```

与 `effects.css:13-22` 的 `--obx-shadow-1: 0 6px 24px rgba(0,0,0,0.10)`、`--obx-radius: 14px`、
`--obx-radius-sm: 10px`、`--obx-shadow-2: 0 14px 44px rgba(0,0,0,0.18)` **数值逐字相同**；
`--mp-glass` / `--mp-glass-strong`（`css:16-17`）与 `.obx-glass` / `.obx-glass-strong`
（`effects.css:95/101`）的 `color-mix(... 84%/92%, transparent)` 公式相同。
未覆盖 Shell 的 `--bg-*` / `--text-*` / `--accent` / `--radius` / `--nav-width` /
`--sub-sidebar-width` / `--toolbar-height`。

### 3.3 硬编码颜色字面量

| 正则 | 命中行数 | 命中次数 |
| --- | --- | --- |
| `#[0-9a-fA-F]{3,8}\b` | 22 | 22 |
| `rgba?\(` | 42 | 46 |
| `gradient\(` | 19 | 19 |

典型 5 例：

- `css:341` `.mp-stage{background:#0b0b12}` —— 舞台上/下封面底色，浅色主题下也是深色。
- `css:1728-1729` `.mp-lyrics-page{background:#07070d; color:#fff}` —— 歌词沉浸页整页硬编码。
- `css:761/1287` `.mp-icon-btn.fav-active{color:#f43f5e}` / `.mp-row-action.fav-active{color:#f43f5e}` —— 收藏红心不走 `--danger`。
- `css:14` `--mp-accent-2: color-mix(in srgb, var(--accent) 55%, #8b5cf6)` 与 `css:1754-1755` 极光里的 `#8b5cf6` / `#06b6d4`。
- `css:366` `.mp-stage-scrim{background:linear-gradient(180deg, rgba(8,8,14,.35) ...)}`、`css:1904-1907` 全屏态 `rgba(255,255,255,.85)` / `#fff`。

### 3.4 `data-theme` 处理

`Select-String -Pattern 'data-theme'` 对该插件 `*.css` / `*.html` / `js/*.js` 执行 →
**0 命中**（`css` 内 `:root|data-theme|@media` 只命中 `:root`@9 与 3 处 `@media`）。
插件完全依赖壳的 iframe 注入脚本同步主题属性；暗色下不变形的只有走 token 的部分，
硬编码深色面板（`#0b0b12` / `#07070d` / `#000`）在两个主题下都是深色，
`rgba(255,255,255,*)` 也只在深色面板内使用（`css:1585/1904/1905`）。

---

## 4. 组件与命名约定

### 4.1 自有类名前缀

`.mp-*` 唯一类名 **137 个**（正则 `\.mp-[a-z0-9-]+`，去重）。子前缀分组：
`mp-stage-*`（14）、`mp-lyrics-*`（9）、`mp-pb-*`（7）、`mp-eq-*`（7）、`mp-row-*`（8）、
`mp-card-*`（4）、`mp-detail-*`（7）、`mp-pop*` / `mp-modal*` / `mp-nav*` / `mp-toolbar*` 等。

**无前缀的例外**（散落在 JS 模板里，与 Shell 类名风格混在一起）：
`.cover-fallback`（`utils.js:80`）、`.img-broken`（`utils.js:138`）、
`.empty-icon` / `.empty-text` / `.empty-hint`（`app-render.js:33-36`）、
`.q-index` / `.q-title` / `.q-kind` / `.q-remove`（`app-playback.js:271-272`）、
`.pl-name`（`playlist-manager.js:130`）、`.active` / `.hidden`（Shell 既有语义）。

### 4.2 自有组件清单

| 类别 | 类名 | 位置 |
| --- | --- | --- |
| 按钮 | `.mp-icon-btn`(34×34 圆形)、`.mp-play-btn`、`.mp-stage-big-btn`、`.mp-tool-btn`(圆角 999px)、`.mp-ghost-btn`(5px 9px / 8px 圆角) | `css:737-758 / 765-783 / 613-641 / 318-320 / 1569-1581` |
| 卡片 | `.mp-card-grid`、`.mp-card`、`.mp-card-badge`、`.mp-card-play` | `css:992-1009 / 1091 / 1046` |
| 行/列表 | `.mp-row`、`.mp-row-cover`、`.mp-row-tag`、`.mp-list-group`、`.mp-queue-item` | `css:1155 / 1194 / 1235 / 1142 / 1438` |
| 弹窗 | `.mp-modal`、`.mp-modal-box`、`.mp-modal-head/body/foot`、`.mp-modal-sm` | `css:1587-1642 / 1615` |
| 浮层 | `.mp-pop`、`.mp-pop-head`、`.mp-pop-body`、`.mp-queue-pop`、`.mp-eq-pop` | `css:1391-1423 / 1425 / 1487` |
| 菜单 | `.mp-context-menu`（`button` 而非 `li`） | `css:1686-1717` |
| 进度 | `.mp-range` + `--range-val`（渐变填充轨道）、`.mp-progress`、`.mp-volume-bar` | `css:820-866` |
| Toast | **无自有实现**，调用 Shell `Toast.success/error/info/warning` | `app.js:118`、`app-stage.js:94/101/109/112/162/170/177/181/207` 等 |
| 空状态 | `.mp-empty-state` + `.empty-icon`/`.empty-text`/`.empty-hint` | `css:976-989`；调用 6 处：`app-render.js:33,103,173,197,226,527` |
| 加载 | `.mp-loading` + `.mp-spinner`(34px, 0.8s) | `css:949-974`；入口 `app-render.js:17-28` |
| 骨架屏 | **无**（`obx-skeleton` 0 命中） | — |
| 徽标 | `.mp-card-badge`、`.mp-row-tag.video`、`.mp-thumb-pending`、`.mp-mini-eq` | `css:1091 / 1245 / 1073 / 213` |
| 表单控件 | `.mp-input`、`.mp-select`、`.mp-add-pl-item` | `css:1644-1659 / 1496 / 1668-1681` |

### 4.3 Shell 已提供、插件又实现了一遍的能力

1. **模态框**：Shell `.modal` / `.modal-box`（`base.css:159-179`，宽 400px、`border-radius: var(--radius)`=6px、
   `z-index:1500`、`slideUp 0.2s`）↔ 插件 `.mp-modal` / `.mp-modal-box`
   （`css:1587-1613`，宽 **360px**（`.mp-modal-sm` 320px）、圆角 **18px**、`backdrop-filter: blur(4px)`、
   `z-index:500`、`mpScaleIn 0.24s`）。**3 个弹窗**全部用自有版（`index.html:206/223/240`）。
2. **右键菜单**：Shell `createContextMenu` + `.context-menu`（`base.js:886`、`base.css:104-116`，
   `min-width:150px`、圆角 `var(--radius)`、`z-index:2000`、`li` 结构）↔ 插件手工建
   `.mp-context-menu`（`app-playback.js:380-389`，`min-width:140px`、圆角 12px、`z-index:520`、
   `button` 结构、`mpPopIn 0.18s`）。仅歌单一个场景（重命名/删除）。
3. **空状态**：Shell `.empty-state`（`base.css:428-431`，居中、`min-height:300px`、16px 单行文案）
   ↔ 插件 `.mp-empty-state`（`css:976-989`，`min-height:240px`、图标 44px + 15px 标题 + 12px 提示）。
4. **卡片网格**：Shell `createCardGrid`（`base.js:915`）↔ 插件 `.mp-card-grid` + `.mp-card`
   （`css:992-1009`，`minmax(168px,1fr)`、`mpFadeUp` 入场 + `--i*26ms` 交错）。
5. **滚动条**：Shell `.obx-scroll`（`effects.css:140-183`）↔ 插件 `css:879-947` 的同一套
   `scrollbar-width: thin` + `::-webkit-scrollbar{width:6px}` + 悬停渐显规则，
   颜色 token `--mp-scroll-thumb` 38% / hover 62%（`css:21-22`）与 effects.css:150/176/182 同值。
6. **动效关键帧**：`effects.css:25-74` 的 `obxSpin/obxFadeUp/obxPopIn/obxScaleIn/obxFade/obxHeart/obxFloat/obxShimmer`
   ↔ 插件 `mpSpin/mpFadeUp/mpPopIn/mpScaleIn/mpFade/mpHeart/mpFloat/mpShimmer`
   （`css:1965-2015`），逐条数值相同（如 `mpFadeUp` 与 `obxFadeUp` 都是
   `translateY(10px)`、`mpHeart` 都是 `40%{scale(1.45)}`）。交错延迟用自有 `--i`（`css:1008`，
   26ms），而 `effects.css:90` 用 `--obx-i`（30ms）。
7. **玻璃拟态 / 抬升**：Shell `.obx-glass` / `.obx-glass-strong` / `.obx-card-lift` ↔ 插件
   `.mp-sidebar{background:var(--mp-glass); backdrop-filter:blur(18px)}`（`css:39-41`）与
   `.mp-card:hover`（`css:1006` 自带 transition，未用 `.obx-card-lift`）。
8. **按钮基础态**：Shell `.btn`（`base.css:14-36`）↔ 工具栏按钮确实复用 `.btn`
   （`index.html:72-74`，9 处），但另有 `.mp-ghost-btn`（`css:1569-1581`）与
   `.mp-icon-btn`（`css:737-758`）两套自有按钮体系，交互细节（`active{scale(0.94)}`）与
   Shell 的 `.btn:active{scale(0.97)}`、`.btn:hover{background:var(--bg-hover)}` 不一致。
9. **设置表单**：**复用** Shell `openSettingsModal`（`app-views.js:75`）+ Shell `.settings-form`
   （弹窗由壳渲染），后端 `settings_schema` 含 `text/number/checkbox/range/select/directory` 类型。
10. **分页 / 灯箱 / 目录树**：**均未实现也未复用**（`createPagination` / `createLightbox` /
    `createTree` 在该插件 0 命中），列表全量渲染（`app-render.js` 整段 `innerHTML`）。
11. **进度条**：自有 `.mp-range` + `--range-val`（`css:820-866`、`utils.js:169-173`），
    Shell 无对应组件 → 不算重复实现。

---

## 5. 交互约定

- **设置入口**：工具栏 `.btn#btn-settings` → `_openSettings()`（`app-ui-events.js:25` →
  `app-views.js:74-84`），传 `title: '媒体播放器设置'`、`successMessage: '设置已保存'`、
  `onSave: Bridge.call('save_settings', values)`；歌词页工具栏 ⚙ 打开同一个壳弹窗但换标题
  （`app-ui-events.js:94-96`，`'歌词与播放设置'`）。**保存后的反馈**完全交给壳：
  `Toast.success` + `setTimeout(() => location.href = ... + '?_t=' + Date.now(), 400)` 整页重载
  （`base.js:621-622`），插件没有自定义保存后行为。
- **错误提示**：统一 `Toast.error` + `console.error`，无内联错误条/字段级报错。
  例：`app.js:122` `console.error('媒体索引初始化失败:', e)`、`app-views.js:63` `Toast.error('扫描失败')`、
  `player-core.js:699/743` 解码/加载失败、`app-render.js:527` 在内容区渲染
  `.mp-empty-state` + `⚠️ 歌单加载失败`。
- **选择模型**：**单选**（点击专辑卡/行即播放或进详情，`app-render.js:136`）；
  无多选、无框选、无长按；无 `selectionMode` / `selectedIds`（grep 0 命中）。
  唯一的状态切换是收藏：`♡/❤️` 按钮（`index.html:121`）→ `media_toggle_favorite`
  （`app-playback.js:212`、`app-render.js:629`）。
- **右键菜单**：仅歌单条目。`app-playback.js:378-408` 动态建 `.mp-context-menu`，
  两项 `data-menu-act="rename" | "delete"`（删除带 `danger` 样式并走 `confirmDialog`，
  `app-playback.js:399`）。定位用 `Math.min(e.clientX, innerWidth-150)`（同上 386-387）。
  卡片/行/任务**无**右键菜单。
- **键盘快捷键**（`app-stage.js:303-366`，`INPUT/TEXTAREA/SELECT` 内不拦截，见 305）：
  `Space` 播放/暂停、`←/→` ±5s（`seekDelta`）、`↑/↓` 音量 ±0.05、`n/N` 下一曲、`p/P` 上一曲、
  `m/M` 静音、`l/L` 歌词页、`f/F` 全屏、`Esc` 逐级退出（全屏 → 歌词 → 关队列/均衡器/弹窗/菜单，
  353-363）。弹窗输入框内另有 `Enter` 提交：`app-ui-events.js:107-108`、`122-123`；
  搜索框 `Esc` 清空（`app-ui-events.js:33-36`）。
- **长任务进度与取消**：扫描是唯一长任务。`media_scan(deep)` 启动后 500ms 轮询
  `media_scan_status`，上限 1200 轮（≈10 分钟，`app.js:87-96`）；进度文案
  `` `正在扫描媒体库… ${s.processed}/${s.total}${s.current ? ' · ' + s.current : ''}` ``
  （`app-views.js:53`）走 `.mp-loading` 转义 + `white-space: pre-line`（`css:962-965`）；
  扫描期间两个按钮 `disabled`（`app-views.js:65-69`）。**前端没有取消入口**，
  只在轮询发现后端 `state === 'cancelled'` 时提示"扫描已取消，已完成部分已保留"
  （`app-views.js:57`）。断点续扫在 `init` 阶段自动触发（`app.js:104-113`）。
- **空/加载/错误三态**：加载 = `.mp-loading`+`.mp-spinner`（`app-render.js:17-28`，
  文案 `正在准备媒体库…` / `首次使用，正在扫描媒体库…` / `继续上次未完成的扫描…` 见
  `app.js:99/106/110`）；空 = `.mp-empty-state` + 图标/标题/提示（`app-render.js:30-38`，
  调用点 6 处）；错误 = 同款空态 + `⚠️`（`app-render.js:527`、`app-views.js:182-183`）。
  无骨架屏。

---

## 6. 特色设计（值得其他插件吸收）

1. **歌词沉浸页 = 覆盖层 + 位移过渡，而不是新路由**
   （`css:1722-1738`、`lyrics-parser.js:243-262`）：整页 `position:fixed; inset:0;
   transform:translateY(102%)` → `.active{translateY(0)}`，配 `pointer-events` 切换与
   canvas 频谱（`requestAnimationFrame` 自循环，`_startViz/_stopViz` 成对）。
   解决"要给音频一个大屏、又不能让 iframe 重新加载、也不能拦掉下层交互"。
2. **视频封面缺失时前端 canvas 抽帧并回写缓存**
   （`frame-extractor.js:149-196`、`utils.js:292-298`、`utils.js:57-62`）：
   封面 404 → 借 `<video>` 抽一帧 → `media_put_thumb` 入库；同时给 `/thumbs` URL 挂
   文件 mtime 版本号 `&v=`，强制绕开 1 天缓存。解决"老视频无内嵌封面"与
   "换了文件旧封面最长展示 24h"两个具体问题。抽帧还做了 `document.hidden` 判断与
   3s rAF 超时（`frame-extractor.js:9,245,264`），避免后台空转。
3. **非线性音量映射 `VolumeMapper(2.5)`**（`utils.js:269-290`）：
   滑块位置与实际音量之间只在"赋值元素 volume"处做 `pow(x, 2.5)`，存储/传输仍是线性 0~1。
   解决"低音量区间在滑块上挤成一格、调不准"，且不改变后端存档语义。
4. **首用自动扫描 + 断点续扫 + 结果汇总 Toast**（`app.js:98-124`）：
   `stats.total === 0` 时自动全扫；`state === 'paused'` 时提示"继续上次未完成的扫描…
   已完成部分自动跳过"；结束后 `Toast.success('扫描完成：音乐 N · 视频 M')`
   （`app.js:118`、`app-views.js:48/58`）。把"第一次打开是空白页"变成一次带反馈的自动流程。
5. **网易云曲目 ↔ 本地库的保守匹配打分**（`utils.js:199-252`）：
   NFKC + 抹平标点空白归一化歌名，歌手 token 交集得 3 分、时长差 ≤3s 得 2 分、
   同名唯一得 1 分，同一本地条目只被认领一次，未命中项导出补档清单
   （`app-render.js:470` `media_export_missing`）。解决"在线歌单落到本地时错配别人的歌"。
6. **扩展入口与自建导航的选中态联动**（`app.js:159-177`）：
   `renderExtensions(container, 'media-player', 'sidebar', {title, onOpen})` 拿到壳渲染的
   `.obx-extension` 后，再补一次点击处理，让扩展项与 `.mp-nav-item` 的 `active` 互斥。
   解决"壳渲染的扩展入口和插件自己的侧栏高亮各亮一个"。

---

## 7. 与 Shell 契约的偏差（后续统一 UI 必须处理的点）

1. **声明了 `keepAlive: true`，却一个生命周期钩子都没注册。**
   证据：`plugins/media-player/manifest.json:8` `"keepAlive": true`；
   对 `plugins/media-player/frontend/**` 执行 `onShow\(|onHide\(|onDispose\(|PluginLifecycle`
   → **0 命中**（对 `plugins/media-player/` 整目录执行同一正则只命中 manifest 这一处）。而 `docs/plugin-guide.md:763-764`
   明确写着"`media-player` 在 `onHide` 里只停 `lyrics-parser` 的 rAF 自循环"。
   受影响面：2 个常驻定时源——`lyrics-parser.js:246-250` 的 canvas rAF 自循环
   （只在 `_stopViz()` 被显式调用时停，`lyrics-parser.js:253`）、
   `player-core.js:417` 的 `setInterval(_, 2000)` 进度保存；另加 `frame-extractor.js` 的
   抽帧队列。是否真的空转取决于浏览器对 `display:none` iframe 里 rAF 的实现，
   插件侧没有任何可控点。
2. **侧栏宽度脱离 `--sub-sidebar-width`。**
   证据：`css:38` `.mp-sidebar{width:252px}` 覆盖 `base.css:252`；
   `--sub-sidebar-width` 在该插件 CSS 中 0 命中；响应式断点自行改宽度
   `css:2029` `.mp-sidebar{width:210px}`。影响：壳在设置页调整子侧栏宽度对该插件无效，
   与 manga-library（248px，见另一份）也不一致。
3. **自有弹窗层级低于壳弹窗，宽度也不同。**
   证据：`.mp-modal{z-index:500}`（`css:1590`）vs Shell `.modal{z-index:1500}`（`base.css:162`）、
   `.toast-container{z-index:3000}`（`base.css:210`）；宽度 360px（`css:1603`）vs 400px（`base.css:167`）。
   影响 3 个弹窗；同一时序里打开壳设置弹窗会盖住插件弹窗，统一组件后需要重排 z-index 体系。
4. **右键菜单绕过 `createContextMenu`。**
   证据：`app-playback.js:380-389` 手建 `div.mp-context-menu` + `button`，
   样式 `css:1686-1717`（`min-width:140px`、`li` → `button`），而 Shell 约定是
   `.context-menu > li`（`base.css:110`）。影响 1 个功能（歌单重命名/删除），
   统一菜单组件时会同时改动 `app-playback.js` 的 DOM 结构与 CSS。
5. **滚动容器一半加类、一半靠自有规则。**
   证据：`index.html:48/186/200/212/229` 的 5 个容器同时有 `.obx-scroll`，
   但最长的滚动区 `#media-content`（`index.html:154`）**没有** `.obx-scroll`，
   只被插件自有规则 `css:879-947` 覆盖。影响：只按类名统一（例如改 `.obx-scroll` 的
   38%/62% 透明度）会漏掉内容区，观感不一致。
6. **`prefers-reduced-motion` 用通配符全量覆盖。**
   证据：`css:2039-2047` `*, *::before, *::after { animation-duration:.001s !important;
   transition-duration:.001s !important }`（该文件仅有的 3 处 `!important` 都在这里），
   比 `effects.css:186-201` 的类名白名单更宽。影响：会连带压掉壳注入到该 iframe 的
   组件（Toast、设置弹窗、目录选择器）的过渡，统一动效策略时要一起取舍。
7. **`effects.css` 的类工具几乎未被采用。**
   证据：`obx-anim-*` / `obx-glass` / `obx-card-lift` / `obx-stagger` / `obx-skeleton`
   在该插件目录 0 命中（仅 `obx-nav-item` / `obx-scroll` / `obx-extension` 被用到）；
   替代品是 8 个自有关键帧（`css:1965-2015`）与自有 `--mp-glass` / `--mp-shadow-*` / `--mp-radius*`。
   影响：入场动画、玻璃、抬升、交错、骨架五类能力各多一套实现；
   壳改 `effects.css`（例如调 `--obx-shadow-2`）时该插件不会跟随。
8. **面板尺寸用内联 `style` 与自有类混写。**
   证据：`js/*.js` 中共 16 处 `style="` 模板拼接（`Select-String -Pattern 'style="'` 统计
   `plugins/media-player/frontend/js/*.js`；`index.html` 侧为 0 处），典型 `app-render.js:689`
   `style="--hero-bg:${MPUtils.heroBg(coverSrc)}"`（该值已走白名单编码，`utils.js:108-118`）。
   影响：主题/尺寸调整无法只改 CSS，需要同时改 JS 模板。
9. **无分页、无虚拟滚动，列表一次全量渲染。**
   证据：`createPagination` 0 命中；`app-render.js` 以整段 `innerHTML` 重建列表
   （如 `app-render.js:51` `content.innerHTML = '<div class="mp-card-grid"></div>'` 后逐张 append）；
   在线歌单有硬上限 `NCM_PLAYLIST_SONG_CAP = 1000`（`app-render.js:10`）。
   影响：统一列表/分页组件时，媒体库（行/卡/队列/歌单/网易云五套列表）都要接入。
10. **设置保存依赖整页重载。**
    证据：插件 `onSave` 只转发 `save_settings`（`app-views.js:78-82`），
    刷新由 Shell 固定执行（`base.js:622` `location.href + '?_t=' + Date.now()`）。
    影响：与 `keepAlive: true` 组合时，"改设置"必然打断播放（整页重载），
    而声明 keepAlive 的目的正是"播放不中断"，两者在语义上冲突；涉及全部
    `lyrics_*` / `auto_hide_*` / `media_roots` 设置项（`backend/main.py` 的 `settings_schema`）。
