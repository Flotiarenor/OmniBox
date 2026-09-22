# 文档阅读器重构设计（三态布局 + 朗读）

状态：**已实现**（插件版本 3.5.0）。数据来自真机量测（`main.py --web-only` 隔离实例 +
Selenium 非无头），不是读源码推断的；下文标注"实测""踩过"的地方都是验证过的结论，
改动这一块之前值得先读。

回归用例：`tests/test_document_reader_tts.py`（朗读后端）、
`tests/test_document_reader_formats.py`（封面 / 书签 / 格式）、
`tests/test_document_reader_browser_e2e.py`（真实布局与滚动）。

## 1. 量测事实（重构的地基）

窗口 1600×1000 时：

| 区块 | 宽度 | 说明 |
| --- | --- | --- |
| 壳主导航 `.nav-sidebar` | 200px | 在插件 iframe **之外**，任何插件都拿不到、关不掉 |
| 插件 iframe 视口 | 1376px | 壳给插件的全部空间 |
| 插件侧栏 `.view-sub-sidebar` | 240px | 窄屏 210px |
| 插件主区 `.view-body` | 1136px | |

媒体播放器宽屏模式实测（`.wide-mode`）：

| | 普通 | 宽屏 |
| --- | --- | --- |
| `.mp-sidebar` | 240×861 | **保留 240×861** |
| `.mp-content` | 1136×501 | **`display:none`** |
| `.mp-stage` | 1104×300 | **1104×789**（吃满主区） |
| `.mp-playerbar` | y=284，嵌在舞台内 | **y=772，贴底** |

结论：**"宽屏模式"= 保留两根边栏、藏掉中间列表、主区吃满、播放条贴底**。

## 2. 三态布局

### 书架态

```
壳导航 200 │ 插件侧栏 240 │ 主区
            │ 浏览           │ 工具栏：搜索 + 设置按钮
            │  全部文档      │ ┌───┐┌───┐┌───┐┌───┐┌───┐
            │  最近阅读      │ │封面││封面││封面││封面││封面│  ← .ml-grid 同款
            │  书签          │ └───┘└───┘└───┘└───┘└───┘
            │                │ 书名 · 42%   书名 · 8%   书名
            │ 底部状态一行   │
```

（左侧栏四个入口的图标：`library` 全部文档、`history` 最近阅读、`star` 书签、
`volume-2` 朗读设置；工具栏右侧是搜索输入框与 `settings-2` 设置按钮。）

- 封面卡元素：封面（纵深比 140%）+ 格式角标（EPUB/TXT/MD/PDF）+ 进度条 + 书名 + 作者或章节数。
- 网格：`repeat(auto-fill, minmax(150px, 1fr))`，间距 16px，逐项 `--obx-i` 入场。

### 阅读态

```
壳导航 200 │ 插件侧栏 240 │ 主区 1136
            │ 书名（可点回书架）│ 工具栏：← 返回书架 │ 章节名
            │─────────────────│
            │ 目录           │            正  文
            │ 阅读设置        │        （正在念的那一句：加深 + 加粗）
            │ 朗读设置        │
            │─────────────────│
            │ 底部状态一行    │                        ┌────────┐
            │                 │                       │ 朗读控制 │ ← 浮动朗读卡
            │                 │ ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░
```

（阅读态左栏"当前文档"组：`list` 目录、`A` 阅读设置、"浏览"组底部是 `volume-2` 朗读设置。）

**侧栏是"按钮栏"，不是配置面板**：书名 + 三个入口（目录 / 阅读设置 / 朗读设置）+ 底部状态。
所有配置都进弹窗或独立页 —— 侧栏不承载任何滑杆与下拉框。

- **目录** = 自适应大小的居中弹窗 + 内部滚动（复用壳 `.modal` 那套），入口在侧栏。
- **阅读设置** = 同一种弹窗（字号、行距、字间距、主题、阅读模式），入口在侧栏。
- **朗读设置** = **独立小页面**（引擎、base_url、API Key、音色、语速、试听），
  覆盖主区（同 image-viewer `.extension-view` 的形态），入口在侧栏。理由：试听要宽、且是低频操作。
- **朗读控制** = 右下角浮动小卡（§3），不是整排朗读条。
- **朗读位置标记** = 正在念的那**整句**整体加深加粗（不用进度环、不用底色块），
  该高亮同时是书签的复用点（§6）。

## 3. 朗读控制卡（浮动，不是整排）

沿用 image-viewer 的 `rebuild-progress-card` 那套定位（`position:fixed; right:16px; bottom:16px`），
但做得小：只有一颗圆按钮，展开后三颗。

```
展开态（右下角）                    收起态（贴右边缘）
┌──────────────────┐
│ 放大    暂停   停止 │  36px 圆钮          ┌──┐
└──────────────────┘                      │播放│ 24×48，只有一个图标
  左：收到侧边   中：暂停/播放   右：停止    └──┘ 点击 → 展开并继续
```

（三颗钮对应的图标：`maximize-2` 收到侧边、`pause`/`play` 暂停与播放、`square` 停止；
收起态是 `chevron-right` 展开按钮。）

- 收起后**只剩最左边那一块**（变成展开按钮），正文宽度一分不占。
- 收起态**不带进度环**：读到哪由正文里"加深的那一句"表示（§2 阅读态）。
- 不朗读时整卡不渲染（DOM 都不建）。

## 4. 持久化：进度只记到章，阅读偏好进后端

- **阅读进度只记到章**（`last_read_chapter` + 兼容用的 `scroll_position`）。点封面续读 =
  回到那一章开头。朗读**不写任何状态文件**，所以它与阅读进度不会互相污染。
- **朗读时会保存进度**（早期版本的"朗读中不保存"已按需求撤销）：朗读会带着滚动走，
  而"上次读到哪"正是关掉应用后最想接着看的地方。
- **阅读偏好（字号/行距/字间距/主题/配色/阅读模式）改存插件设置**，不再用 localStorage：
  跨设备一致，且不受浏览器隐私模式影响。旧 localStorage 里带 `version` 的偏好会迁移一次
  （没有 `version` 的旧文件里，主题/模式是当年自动落盘的旧默认值，按新默认走）。

## 5. 朗读设置页

- 它是**主区的内容切换**（与书架网格、正文同一个位置），不是遮挡页：左栏"朗读设置"
  按钮保持高亮，随时能切回书架或正文。
- 定位参照给在 `.view-body`（`position: relative`）上 —— 给在 `#app` 上会让面板盖住左栏。
- **音色列表问端点**（`tts_voices`）：端点不可达时才退回内置的常用表，并在界面上说明
  "这不是完整列表"。写死列表的问题是微软会下线音色，点一下就报找不到。
- 语速默认 **+100%**（端点允许的倍速上限），滑杆范围 -50 ~ +100。

## 6. 朗读时的自动跟随

- **自动滚动**：句子跑出视口才滚，目标是把它放到视口 35% 处，留 25% 余量。
  每句都硬滚会让页面不停抖动，用户想回看上一句都抓不住。
  算式是 `delta = 句子位移 - 视口高度 * 0.35`；**别再减一次视口高度**，
  那会让 delta 恒为负、scrollTop 被夹在 0（表现是"怎么念都不滚"，已踩过）。
- **自动翻章**：念完本章切下一章继续。朗读自己翻章要置 `app._ttsAdvancing`，
  否则滚动事件里"手动换章就停朗读"的判定会把刚接上的朗读立刻停掉。
- **预取下一句**：合成有 1.5~2.2 秒固定网络开销（与语速无关，实测），
  不等它才连得上。预取结果必须**真的被 `_playCurrent` 读走**，
  只用 `new Image()` 预热也无效（image 请求不了 mp3）。

## 7. 这次要删的代码（重构的收益）

| 现有 | 处理 |
| --- | --- |
| `_switchSidebar()`、`_setChapterTabVisible()`、两套面板显隐 | 删：侧栏不再是"书架/章节"二选一 |
| `_chromeChapterIndex`、`_syncReaderChrome()`、"只有换章才刷目录高亮" | 删：目录改弹窗，打开时按 `engine.currentChapterIndex` 现刷 |
| 侧栏被长书名顶开的 `min-width` 修复 | 删：阅读态侧栏没有长书名行 |
| `_chapterListBuiltFor` 缓存 / 失效 | 保留但简化：每次打开目录弹窗重建 |

## 8. 最终决定一览

| 项 | 决定 |
| --- | --- |
| 目录位置 | 自适应大小的居中弹窗 + 内部滚动；**入口按钮在左侧栏** |
| 阅读设置 | 同一种弹窗；**入口按钮在左侧栏** |
| 朗读设置 | **主区的内容切换**（与书架网格同一位置），引擎 / base_url / API Key / 音色 / 试听全在里面；入口在左侧栏的"浏览"组 |
| 左侧栏定位 | 浏览组（全部文档 / 最近阅读 / 书签 / 朗读设置）+ 阅读时才出现的"当前文档"组（目录 / 阅读设置），**不放任何滑杆与下拉框** |
| 工具栏 | 左：`← 返回` + 书名；右：搜索输入框 + 设置按钮（`Icons.html('icon:settings-2')`，`margin-left:auto` 靠右） |
| 封面卡点击 | 直接进入阅读态，回到上次那一章的**开头** |
| 阅读进度 | **只记到章**；朗读时也保存（朗读本身不写任何状态文件） |
| 阅读偏好 | 存后端设置（非 localStorage）；旧数据按 version 迁移一次 |
| 朗读控制 | 右下角浮动小卡：收起 / 暂停 / 停止；收起后只剩左侧一点、**不带进度环** |
| 朗读位置标记 | 正在念的**整句**加深加粗 + 下划线（不铺底色，见 §11 的说明） |
| 朗读起点 | 右键菜单触发：有选中 → 选中第一个字所在句的开头；无选中 → 视口顶部那一句 |
| 朗读跟随 | 念出视口就自动滚；念完一章自动接下一章（§6） |
| 音色列表 | 问端点（`tts_voices`），端点不可达才退回内置表并显式提示 |
| 语速默认 | **+100%**（端点倍速上限），滑杆 -50 ~ +100 |
| 正文点击 | **沿用现有翻页逻辑，朗读不参与**（左/右 25% 翻页） |
| 右键菜单 | 复用壳的 `createContextMenu`（§9） |
| 上下句 | **不做**。念错就重新右键选一段 |
| 底部状态行 | 一行：`N 本 · 朗读 <引擎>` |

仍留着的小事：书签在正文里的标记目前只在"加书签那一刻 + 当前章"绘制，
换章或刷新后不会重画全部（`_flashMarks` 未挂到换章事件上）。

## 9. 右键菜单（取代点击/选中朗读）

在内容区监听 `contextmenu`，复用壳的 `createContextMenu`（image-viewer 的既有做法）。

| 菜单项 | 启用条件 | 行为 |
| --- | --- | --- |
| 复制 | 有选中文字 | 写剪贴板 |
| 刷新 | 始终 | 重新加载当前文档 |
| 开始朗读（选中处 / 屏幕顶部那一句） | 始终；文案随有无选中变化 | 有选中从选中句开头念，无选中从视口顶部那一句念 |
| 添加书签 | 有选中文字 | 记录 `{chapter, char, snippet}`（字级，唯一需要字级的场景） |
| 检查 | 仅 `debug.status_debug` 为真时出现 | 开发者工具 / 壳内调试视图 |

**锚点在右键那一刻就算好并缓存**（`_menuAnchor`）：菜单从打开到被点击之间选区可能
消失（失焦、点击落点、浏览器策略），那时再去读 `getSelection()` 就是空的 ——
表现正是"我明明选中了，点朗读却没反应"。

- 无选中文字时只亮"刷新（+检查）"，其余置灰。
- 菜单项的显隐由选中状态决定 —— 这正好绕开与"点击翻页"的冲突：**朗读只从菜单进，不从点击进**。
- `debug.status_debug` 取法：`Bridge.callSystem('system_get_config')` 读 `debug.status_debug`
  （`--status-debug` 启动才有），与 `StatusView.vue` 判定调试面板用的是同一个开关。

## 10. 封面（txt / md / 无封面格式）

- EPUB 封面由后端三条声明路径提取（EPUB3 `properties="cover-image"`、EPUB2
  `<meta name="cover">`、`<guide><reference type="cover">`），解包到
  `.document_state/extract/<哈希>/cover.*`，经 `/file` 提供。
- 没有封面图的书用**生成式 SVG 封面**：外框 + 书脊 + 4 条文字条，底色由**书名哈希**
  决定（同一本书颜色稳定，不同书可区分）。原素材是 `res/未标题-1.svg`（900×1200），
  砍掉了会糊的细描边与顶部字形 —— 缩到 150×200 时那些细节本来也看不见。
- **封面上不排书名**：书名放封面卡下方（与 manga-library 的 `.ml-card-title` 同位置）。
- 格式角标（TXT/MD/PDF/EPUB）放封面右下角。
- `res/` 定位：**跨插件的统一组件与图标库**（后续补齐）。目前 `res/` 还**不能被插件前端
  访问** —— `file_server` 只服务 `frontend_dist`、`/shell/`、`/plugins/<名>/frontend/`；
  本期生成式封面是前端内联 SVG，不依赖它。等 `res/` 攒够东西再给壳加 `/res/` 只读路由
  （属公共基建改动，另开一轮并配测试）。

## 11. 交互链路

```
书架态  侧栏：全部文档 / 最近阅读 / 书签 / 朗读设置
        主区：封面网格 ──点击封面──▶ 阅读态（回到上次那一章开头）

阅读态  侧栏：浏览组 + 当前文档（目录 / A 阅读设置）；工具栏左侧 ← 返回
        朗读起点：右键「开始朗读」→ 选中第一个字所在句的开头（无选中则视口顶部那句）
        自动跟随：念出视口就滚到 35% 处；念完一章自动接下一章
        位置标记：正在念的整句加深加粗 + 下划线（CSS Custom Highlight，不动 DOM）
        念错重念：重新选一段 → 右键「开始朗读」
        进度保存：滚动/换章时保存（朗读时同样保存）
```

**高亮为什么不铺底色**：半透明底色会盖住浏览器原生的文字选区（蓝色），
用户"选中想看哪里"时看到的是灰扑扑一片，分不清是"我选中了"还是"系统在读"。
用加粗 + 前景色加深 + 下划线标记，永远不会与"选中"混淆。

## 12. 不在这一轮

- 全文搜索（现在只搜书架标题）
- 笔记 / 划线导出
- 书架按目录、格式筛选
- 壳的 `/res/` 只读路由（等 res 里攒够跨插件组件）
- 书签标记在换章/刷新后的全量重画

## 13. UI 现状取证（前端与壳契约对照）

审计范围：`plugins/document-reader/frontend/`（只读取证，未改动任何文件）。
对照基线：`shell/frontend/public/shell/variables.css`（86 行）、`base.css`（432 行）、
`effects.css`（201 行）、`base.js`、`docs/plugin-guide.md` §4。

---

### 13.1 概览

#### 13.1.1 前端文件清单

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `index.html` | 257 | 唯一入口，含全部静态 DOM；按硬顺序引 9 个 JS |
| `document-reader.css` | 1040 | 全部样式，自述按「变量/布局/组件/状态」四段组织（`document-reader.css:4-14`） |
| `js/utils.js` | 68 | `DocumentUtils`：段落切分、体积格式化、生成式 SVG 封面（`coverDataUrl`，`utils.js:56-67`） |
| `js/reader-engine.js` | 489 | `DocumentReaderEngine`：翻页/连续滚动双模式、3 章窗口裁剪、字符偏移坐标系（朗读与书签共用） |
| `js/settings.js` | 151 | `ReaderSettingsStore`：阅读偏好后端持久化 + 旧 localStorage 迁移（`settings.js:57-90`） |
| `js/reader-tts.js` | 445 | `ReaderTts`：切句、合成请求、预取、播放、CSS Custom Highlight 高亮、浮动卡 |
| `js/reader-voice-page.js` | 313 | `ReaderVoicePage`：朗读设置独立页（引擎/端点/Key/音色/缓存） |
| `js/app.js` | 608 | `DocumentReader` 类骨架 + 生命周期 + DOM 缓存 + 事件绑定；另有 `openModal/closeModal`（`app.js:600-608`） |
| `js/app-shelf.js` | 169 | `prototype` 分片：封面网格、书签视图、空态模板（`app-shelf.js:139-145`） |
| `js/app-toc.js` | 70 | `prototype` 分片：目录弹窗渲染与筛选 |
| `js/app-menu.js` | 254 | `prototype` 分片：正文右键菜单、书签增删、正文书签标记层 |

分片机制：`app.js` 定义类，`app-shelf/app-toc/app-menu` 用 `Object.assign(DocumentReader.prototype, {...})`
回挂；`index.html:247-251` 注释明确「顺序是硬约束」，实例化在 `index.html:252-255`。

#### 13.1.2 页面形态

单页 + 多视图切换，无路由、无多 HTML 文件、无 companion 内嵌页：

- 书架态：左栏导航（全部/最近/书签/朗读设置），主区封面网格（`index.html:79-81`）。
- 阅读态：主区换成正文容器（`index.html:84-86`），左栏多出「当前文档」组（`index.html:43-51`）。
- 朗读设置：**覆盖主区的独立面板**，`position: absolute; inset: 48px 0 0 0`（`document-reader.css:709-717`），
  注释明确「不遮挡左侧栏」（`index.html:91`）。
- 两个弹窗：目录（`index.html:175-186`）、阅读设置（`index.html:189-239`）复用壳的 `.modal` 类，
  开合自实现（`app.js:598-608`，只 add/remove `active`）。
- 朗读控制卡：`document.body` 上的 `position: fixed` 浮动卡（`reader-tts.js:64-79`）。

#### 13.1.3 Shell 布局类使用情况（用）

`view-sub-sidebar`（`index.html:14`）、`sub-sidebar-footer`（`index.html:57`）、`view-body`（`index.html:61`）、
`view-toolbar`（`index.html:62`）、`toolbar-group`（`index.html:63,68`）、`view-content`（`index.html:79`）、
`obx-nav-item`（`index.html:28,32,35,39,45,48`）、`btn`/`btn-sm`/`btn-primary`/`btn-danger`（`index.html:64,95,136,157,236`）、
`modal`/`modal-box`/`modal-body`/`modal-footer`/`search-input`（`index.html:175-186,189-239`）、`hidden`（多处）、
`obx-scroll`（`index.html:26,79,97`）、`obx-glass`（`index.html:14`）。

---

### 13.2 布局骨架

#### 13.2.1 顶层容器树

```
body
└─ #app                                    display:flex; height:100vh; overflow:hidden; position:relative  (css:30-36)
   ├─ aside.view-sub-sidebar.nr-sidebar.obx-glass            (index.html:14)
   │  ├─ .nr-brand            (css:44-51   padding 14px 14px 12px, border-bottom)
   │  │  ├─ .nr-brand-icon    34×34, radius var(--nr-radius-sm)=8px  (css:53-63)
   │  │  └─ .nr-brand-text → .nr-brand-title / .nr-brand-sub
   │  ├─ nav.nr-nav.obx-scroll   flex:1; min-height:0; overflow-y:auto; padding:6px 10px 12px; gap:3px  (css:82-90)
   │  │  ├─ .nr-nav-label ×2    padding 12px 10px 5px; font-size 11px; --text-muted  (css:93-99)
   │  │  ├─ button.obx-nav-item.nr-nav-item ×4（全部/最近/书签/朗读设置）  (index.html:28-41)
   │  │  ├─ #nr-reader-tools（阅读态才显示）→ 目录 / 阅读设置  (index.html:43-51)
   │  │  └─ #nr-extensions      renderExtensions 容器  (index.html:53)
   │  └─ .sub-sidebar-footer.nr-sidebar-footer  padding 11px 14px; font-size 11px  (css:135-144)
   └─ .view-body                            (index.html:61)  ← 插件重定义：position:relative (css:704-706)
      ├─ .view-toolbar.nr-toolbar           48px（壳）  (index.html:62)
      │  ├─ .toolbar-group.nr-heading       ← 返回 / 标题 / 副标题
      │  └─ .toolbar-group.nr-toolbar-right  margin-left:auto（css:146-151）→ .nr-search + 设置入口（<svg class="obx-icon"><use href="#settings-2"></use></svg> 设置）
      ├─ .view-content.nr-content.obx-scroll#nr-shelf-view   padding 18px（css:194-196）
      │  └─ .nr-grid   grid auto-fill minmax(150px,1fr); gap 18px  (css:264-269)
      ├─ .nr-reader-view#nr-reader-view      flex:1; column; min-height:0  (css:199-204)
      │  └─ .document-content-area#document-content-area   自身滚动（见 2.3）
      ├─ .document-progress-bar              高 4px; radius 999px  (css:246-252)
      │  └─ .document-progress-fill          width 由 JS 写 (app.js:537)
      └─ .nr-voice-page#nr-voice-page        absolute; inset:48px 0 0 0; z-index:30  (css:709-717)
         ├─ .nr-voice-head                   padding 12px 18px
         └─ .nr-voice-body.obx-scroll         flex:1; overflow-y:auto; padding 16px 18px 32px
            └─ .nr-voice-card ×4             border 1px; radius var(--nr-radius)=12px  (css:751-756)
```

#### 13.2.2 关键尺寸

| 区域 | 值 | 来源 |
| --- | --- | --- |
| 导航侧栏宽 | `var(--sub-sidebar-width)` = 240px | 壳 `base.css:251-259`（插件未覆盖宽度） |
| 工具栏高 | `var(--toolbar-height, 48px)` = 48px | 壳 `base.css:239-248`；`css:712` 的 `inset:48px` 是硬编码同值 |
| 内容区 padding | 书架 `18px`（`css:194-196`），正文 `24px clamp(24px,8vw,140px) 40px`（`css:219`） | 插件 |
| 正文行宽上限 | `.chapter-content{max-width:760px}`（`css:229-231`） | 插件，注释给出「一行 45~50 字」理由（`css:217-219`） |
| 封面卡比例 | `aspect-ratio: 3/4`（`css:281`） | 插件 |
| 进度条高 | `4px`（`css:246-252`） | 插件 |
| 朗读浮动卡 | `right:16px; bottom:18px`，按钮 `34×34` 圆形（`css:639-672`） | 插件 |

#### 13.2.3 滚动方式

- 书架：滚动落在壳的 `.view-content`（`overflow-y:auto`，`base.css:277-282`）。
- 阅读：**壳的 `.view-content` 不参与**，改为 `.document-content-area` 自滚（`css:210-226`），
  显式 `scrollbar-width:none` + `::-webkit-scrollbar{display:none}`（`css:216,233-236`）——正文区刻意无滚动条。
- 侧栏 `nav.nr-nav`（`css:82-90`）、朗读页 `.nr-voice-body`（`css:742-749`）各自滚动并挂 `.obx-scroll`。
- 进度条通栏、不随正文缩进，注释为有意设计（`index.html:83`）。

---

### 13.3 设计 token 使用

#### 13.3.1 实际引用的壳 token（`var(--...)` 共 120 处引用 / 893 行 CSS）

背景：`--bg-app`(716)、`--bg-surface`(175,625,650,754,792,849,861,896,947)、`--bg-hover`(125,249,284,448,456,555,664,797,871,915)、
`--bg-sub-sidebar` 未显式引用（由壳 `.view-sub-sidebar` 提供）、`--bg-active`/`--bg-overlay` 未引用。
文本：`--text-primary`(72,160,182,349,549,624,665,735,761,803,831,877,922)、`--text-secondary`(78,126,140,165,190,357,417,527,565,581,610,634,768,821,888,933)、
`--text-muted`(98,586,808,939)。
边框：`--border`(49,138,173,442,467,594,626,651,663,728,752,790,850,859,870,905)。
强调：`--accent`(21,338,416,471,560,615,676,798,972)、`--danger`(897,948)。
尺寸：`--radius`/`--radius-sm`/`--radius-lg` **均未引用**（插件用自己的圆角，见 3.2）；`--toolbar-height`、`--sub-sidebar-width` 间接生效。
阴影：`--shadow-sm`(285,851)、`--shadow-md`(291,652)。
动效：`--transition-fast`(286)。
`effects.css`：`--obx-i`（`css:274`，配合 `DocumentUtils` 外的 `Motion.stagger(grid,'.nr-card')`，`app-shelf.js:40`）。

#### 13.3.2 自有变量与 `:root`

**未改写 `:root`**：自有变量定义在 `#app` 上（`css:18-26`），作用域比 `:root` 更窄：

```css
#app { --nr-radius: 12px; --nr-radius-sm: 8px; --nr-accent-soft: color-mix(...); }
```

另有 6 个阅读区变量由 JS 写在元素内联样式上：`--reader-font-size`、`--reader-line-height`、
`--reader-letter-spacing`、`--reader-bg-color`、`--reader-text-color`、`--reader-highlight`
（`settings.js:97-109`；CSS 侧默认值见 `css:220-224,503`）。

**死变量**：`--nr-cover-frame` / `--nr-cover-spine` / `--nr-cover-bars`（`css:23-25`）全仓无引用
（grep 仅命中定义处 3 行）；实际封面配色由 `utils.js:46-53` 的 `hsl(hue 26% 62%)` 等运行时算。

#### 13.3.3 硬编码颜色字面量

grep（限 `document-reader.css`）：
- 正则 `#[0-9a-fA-F]{3,8}\b` → **18 处**
- 正则 `rgba?\(` → **4 处**
- 正则 `hsla?\(` → **3 处**
- 三者合计命中行数：**25 行 / 1040 行（2.4%）**（另 `index.html` 命中 2 行：`index.html:221,225` 的
  `<input type="color" value="#ffffff"> / value="#1a1a1a">`）

典型 5 例：

| 位置 | 字面量 | 说明 |
| --- | --- | --- |
| `css:311,323` | `rgba(0,0,0,0.45)` | 封面「EPUB/PDF」徽标与书签计数徽标底色，深浅主题同值 |
| `css:310,322` | `#fff` | 同上两个徽标的文字色 |
| `css:993-995` | `#1a1a1a / #cccccc / #9ecbff` | `.theme-dark` 夜间阅读配色，与 `variables.css` 暗色 token 无关联 |
| `css:999-1001` | `#f5e6c8 / #5b4636 / #7a3b12` | `.theme-sepia` 护眼纸配色 |
| `css:989,1017` | `color-mix(... , #000)` | 高亮色加深基准 |

#### 13.3.4 `data-theme` 处理

插件侧 **零处理**：`grep -r "data-theme" plugins/document-reader/frontend` 无命中。
主题由壳在注入引导脚本里同步（`shell/backend/file_server.py:70-76`：读 `parent.document.documentElement`
的 `data-theme`，写到插件 iframe 的 `<html>`，并用 `MutationObserver` 跟随）。插件的「阅读主题」
（auto/sepia/dark/green/blue/custom）是**内容阅读配色**，与壳的浅/暗主题是两套正交概念，
`theme-auto` 才落到 `var(--bg-surface)/var(--text-primary)`（`css:986-990`）。

---

### 13.4 组件与命名约定

#### 13.4.1 自有类名前缀清单

统一前缀 `nr-`（导航/品牌/工具栏/网格）+ `document-`（正文/进度/目录），4 个 `iv-` 风格例外为
`theme-*` 档位类。按类别的真实类名：

- 品牌/侧栏：`.nr-sidebar .nr-brand .nr-brand-icon .nr-brand-text .nr-brand-title .nr-brand-sub .nr-nav .nr-nav-label .nr-nav-icon .nr-nav-count .nr-sidebar-footer`
- 工具栏：`.nr-toolbar .nr-toolbar-right .nr-heading .nr-back-btn .nr-title .nr-sub .nr-search .nr-search-ico .nr-search-clear`
- 卡片/网格：`.nr-grid .nr-card .nr-cover .nr-cover-img .nr-badge .nr-mark-count .nr-cover-progress .nr-card-body .nr-card-title .nr-card-sub`
- 正文：`.chapter-content .chapter-separator .document-content-area .document-pdf-frame .document-pdf-open .document-progress-bar .document-progress-fill`
- 目录：`.document-chapter-list .document-chapter-item .nr-toc-title .nr-toc-meta .nr-toc-filter .chapter-words .nr-toc-mark`
- 按钮（自有样式，非壳 `.btn`）：`.nr-setting-value .nr-mark-clear .nr-mark-del .nr-tts-btn .nr-voice-item .nr-search-clear`
  均为裸 `<button>` + 自绘；壳 `.btn/.btn-sm/.btn-primary/.btn-danger` 用在 8 处（`index.html:64,95,136,157,183,236`）。
- 弹窗：`.nr-modal-box .nr-modal-sm .nr-modal-sub .nr-menu-sep`
- 进度：`.document-progress-bar/-fill`、TTS 卡 `data-state="loading"` 驱动 `nrPulse`（`css:679-681`）
- Toast：无自有类，全量用壳 `Toast.*`（`app.js:207,290,322,471,520,522` 等）
- 空状态：`.nr-empty .nr-empty-icon .nr-empty-text .nr-empty-hint`（模板 `app-shelf.js:139-145`）
- 骨架屏：**无**——加载态用 `#document-loading` 胶囊 + `<div class="spinner">`（`app.js:570-584`），
  而 `.spinner` 在本插件 CSS 里没有定义（`document-reader.css` 无 `.spinner` 规则）
- 徽标：`.nr-badge`（格式）、`.nr-mark-count`（书签数，`nrShelfIcon('icon:star')`）、`.nr-nav-count`（导航计数，`css:120-133`）
- 书签标记：`.nr-mark-layer .nr-mark-pin .nr-mark-group .nr-mark-head .nr-mark-book .nr-mark-item .nr-mark-text .nr-mark-meta`

#### 13.4.2 「壳已提供但插件又实现一遍」

| 能力 | 壳实现 | 插件实现 | 差异 |
| --- | --- | --- | --- |
| 弹窗开关 | `openSettingsModal/confirmDialog` 内部建 `.modal`；壳无公开 open/close | `openModal/closeModal`（`app.js:600-608`）只切 `active` 类，遮罩点击关闭自写（`app.js:149-158`，用 `pointerdown`） | 行为等价、命名重合易混；遮罩判定用 `pointerdown` 与壳 `base.js:601-602` 一致 |
| 右键菜单 | `createContextMenu({items})` | 复用其定位/关闭，但每次右键 `menu.innerHTML=''` 重建列表（`app-menu.js:22-24,75-91`） | 壳的 items 构造时固定，插件需要「有无选区」两套菜单；分隔线额外加 `.nr-menu-sep`（`css:590-596`） |
| 设置表单 | `createSettingsForm` + `.settings-form/.field*`（`base.js:419-563`） | 自写 `.nr-setting-row`（`css:599-636`：label 固定 72px、range flex:1、右对齐数值 42px） | 视觉更紧凑、无 schema 驱动 |
| 统一设置弹窗 | `openSettingsModal({title,schema,values,onSave})` | 只传 `{title}`（`app.js:133`）→ 走壳的 `get_settings_schema` | 保存成功后壳会强制 `location.href+?_t=` 刷新（`base.js:622`）；插件另有自绘「阅读设置」「朗读设置」两条入口，同一插件三套设置 UI |
| 分页 | `createPagination` + `.pagination-bar` | 未用（阅读器无分页语义） | 无冲突 |
| 目录树 | `createTree` + `.tree-*` | 未用（目录是章节列表 `.document-chapter-item`） | 章节列表即插件自有 |
| 空状态 | `.empty-state`（`base.css:428-431`） | `.nr-empty`（`css:813-836`），三行式：icon+text+hint | 壳只有一个居中文本；插件多出 40px 浮动图标与提示行 |
| 骨架屏 | `.obx-skeleton` | `#document-loading` 胶囊（`css:838-854`），`position:sticky; bottom:12px` | 无 shimmer；`.spinner` 类无样式定义 |
| 设置页滚动条 | `.obx-scroll` | 已正确挂载（`index.html:26,79,97`） | 符合契约 |
| 导航项 | `.obx-nav-item` + `--obx-nav-*` | 仅保留差异 `gap:9px` + 图标列 18px（`css:102-110`） | 符合 §4.1 范例 |
| 预览灯箱 | `createLightbox` | 未用（无图片浏览需求） | 无冲突 |

---

### 13.5 交互约定

#### 13.5.1 设置入口与保存反馈

三个互不相同的入口：

1. 工具栏设置入口（静态 HTML `<svg class="obx-icon"><use href="#settings-2"></use></svg> 设置`，`index.html:74`）→ 壳的 `openSettingsModal({title:'文档阅读设置'})`（`app.js:132-133`）：
   读后端 `get_settings_schema` 渲染，保存由壳接管 —— **成功后 400ms 整页刷新**（`base.js:622`），
   插件不感知，也没有 `onSave`。
2. 左栏「阅读设置」→ 自绘 `.modal`（`index.html:189-239`）：滑杆 `input` 事件即时预览
   （`app.js:175-208`），落盘走 `ReaderSettingsStore.save()` 的 **400ms 防抖**
   （`settings.js:127-150`），**无任何 Toast 反馈**，失败只 `console.error`（`settings.js:142`）。
3. 左栏「朗读设置」→ 覆盖主区的页面（`reader-voice-page.js:49-66`）：离散控件 `change` 立刻落盘
   （`reader-voice-page.js:96-99`，`immediate=true`），语速滑杆 300ms 防抖（`_save` 的 `setTimeout(write,300)`，
   `reader-voice-page.js:242`）。保存失败弹 `Toast.error('保存朗读设置失败')`（`:238`），成功**不提示**
   （个别动作另外提示，如选音色 `Toast.info(\`已选择 ${...}\`)`，`:278`）。
   设计依据写在 `:220-229`（「防抖会让试听读到旧音色」）。

#### 13.5.2 错误提示方式

全量 Toast（壳组件），无内联错误条、无错误页：

- 打开失败：`Toast.error('打开文档失败')`（`app.js:471`）
- 打开外部程序：`Toast.error(result.error)` / `Toast.success('已交给系统程序打开')`（`app.js:519-520`）
- 翻页到边界：`Toast.info('已经是最后一章了'|'已经是第一章了')`（`app.js:290`）
- 朗读失败：`Toast.error(\`朗读失败：${...}\`)`（`reader-tts.js:272`）
- 静默失败（只 console，无用户可见提示）：文档列表加载（`app.js:339-342`）、书签列表（`app.js:354-356`）、
  进度保存（`app.js:558-560`）、阅读偏好保存（`settings.js:141-143`）。

#### 13.5.3 选择模型

- **无框选、无长按**（grep `mousedown/mousemove/touchstart` 零命中）。
- 文本选择：`user-select: text !important`（`css:225,238-240`），选区语义只用于朗读/加书签
  （`app-menu.js:8-17` 限制选区必须落在 `.document-content-area` 内）。
- 书签视图无多选：逐条点删除按钮（`nrShelfIcon('icon:x')`，`app-shelf.js:98-105`）或整本「清空」（`:106-117`，循环 `_removeMark` 后
  `Toast.success('已清空这本书的书签')`）。

#### 13.5.4 右键菜单

仅正文区（`app-menu.js:29-43`）。有选区时 4 项（复制 / 开始朗读（选中处）/ 添加书签 / 刷新），
无选区时 2 项（开始朗读（屏幕顶部那一句）/ 刷新），`debug.status_debug` 为真时追加「检查」
（`app-menu.js:61-73`，读 `Bridge.callSystem('system_get_config')`，结果缓存于 `_debugFlag`，`:94-104`）。
锚点在**右键那一刻**就解析并存入 `_menuAnchor`（`app-menu.js:36-42`），避免点击时选区已消失。

#### 13.5.5 键盘快捷键（唯一一处 keydown：`app.js:220`）

仅阅读态生效，且 `INPUT/TEXTAREA/SELECT` 聚焦或 `.modal.active` 存在时直接返回（`app.js:221-224`）：

| 键 | 行为 |
| --- | --- |
| `ArrowUp` / `PageUp` | `_scrollArea(-0.8)` 上滚 0.8 屏（`app.js:226-230`） |
| `ArrowDown` / `PageDown` / `Space` | `_scrollArea(0.8)` 下滚（`app.js:231-236`） |
| `Escape` | `returnToShelf()` 退出阅读（`app.js:237-240`） |

另有鼠标翻页：翻页模式下点正文左 25% / 右 25%（`app.js:271-285`），有选区时不动（`:278-279`）。

#### 13.5.6 长任务进度与取消

- 章节加载：`#document-loading` 胶囊（sticky 底部），`_showLoading(true/false)`（`app.js:570-584`）。
- 阅读进度：顶部通栏 4px 进度条，`_updateProgressBar()` 按 `(章索引 + 章内比例)/总章数` 写宽度
  （`app.js:532-538`）。
- 朗读进度：浮动卡三键（收起到侧边 `nrIcon('icon:maximize-2')` / 暂停 `nrIcon('icon:pause')` / 停止 `nrIcon('icon:square')`，`reader-tts.js:64-79`），
  `data-state="loading"` 时切换键 `nrPulse` 呼吸（`css:679-681`）；无进度百分比，只有句级高亮。
- 取消：朗读有停止；**章节加载无取消**（靠 `_token` 自增作废过期响应，`reader-engine.js:95-99`）。

#### 13.5.7 空态 / 加载 / 错误态文案与样式类

| 态 | 样式类 | 文案 |
| --- | --- | --- |
| 书架空 | `.nr-empty`（`app-shelf.js:29-36`） | `icon:book-open` + 「这个分类还是空的」；提示行「把 .txt / .md / .epub 放进文档目录」 |
| 搜索无果 | 同上 | `icon:search` + 「没有匹配的文档」；提示行「换个关键词试试」 |
| 书签空 | 同上（`app-shelf.js:63-68`） | `icon:star` + 「还没有书签」；提示行「在正文里选中一句话 → 右键 → 添加书签」 |
| 目录筛选无果 | 同上（`app-toc.js:43`） | `icon:search` + 「没有匹配的章节」（无 hint） |
| 加载中 | `#document-loading` + `.spinner` | `加载中...`（`app.js:577`） |
| 非渲染格式 | `.nr-empty` | `该格式不在阅读器内渲染，可交给系统默认程序打开`（`app.js:508`） |
| 朗读引擎不可用 | `.nr-voice-hint` | `引擎状态不可用（后端 tts_status 调用失败）`（`reader-voice-page.js:250`） |
| 音色兜底 | `.nr-voice-hint` | `icon:triangle-alert` + 「读不到 edge 端点，下面是内置的常用音色（非完整列表）」（`:263`） |

---

### 13.6 特色设计（值得其他插件吸收）

1. **正文坐标系统一（字符偏移 = `chapterEl.textContent` 下标）**：朗读切句、书签 `char`、
   视口顶部锚点共用一套坐标，不需要任何换算；并显式规定「不缓存 DOM 节点/Range」，
   因为滚动窗口会真的删章（`reader-engine.js:313-323`）。解决的问题：滚动模式下 2~3 章共存时，
   「读到哪 / 书签在哪 / 从哪开始念」三处各自算一遍必然错位。
2. **用 `::highlight()` + CSS Custom Highlight API 做朗读高亮，且刻意不铺底色**：
   `css:498-509` 只改前景色 + 加粗 + 下划线，注释写明「底色会盖住浏览器原生选区，用户分不清
   '我选中了' 还是 '系统在读'」。解决的问题：高亮与用户选区语义冲突；且它不插 DOM，
   滚动裁剪时不会留下孤儿节点。
3. **浮动控制卡「收到侧边」态**：`.nr-tts-card.is-collapsed` 把卡贴到右边缘、只留最左一块
   （`css:683-695`）。解决的问题：常驻的播放控件在窄正文区会永久占宽度，收起后正文零侵占。
4. **合成预取 + 用 `fetch` 灌浏览器缓存**：`_prefetchNext()` 提前请求下一句并把结果存
   `_prefetched`，同时 `fetch(result.url)` 预热 HTTP 缓存（`reader-tts.js:300-311`），
   注释点明「只建 Image 对象等于没预热」。解决的问题：edge-tts 每句 1.5~2.2s 固定网络开销
   导致的句间长空白。
5. **`_fillViewport()` 主动补满视图**：小章节（EPUB/Markdown 常见）三章窗口填不满一屏时
   浏览器不产生 `scroll` 事件，加载会彻底停死；这里在 `goToChapter` 与 `handleScroll` 后
   循环补到 `scrollHeight > clientHeight + 120`，`guard<60` 兜底（`reader-engine.js:175-189`）。
6. **生成式 SVG 封面（零请求、零缓存文件）**：书名哈希 → 稳定色相 → data URL SVG
   （`utils.js:36-67`），且决定「不写字，书名放卡下方」。解决的问题：无封面书（txt/md）在网格里
   出现一片灰，同时避免新增封面缓存文件与请求。
7. **挂起式滚动节流（末尾必执行）**：`_bindScroll` 用 `timer` + 60ms 窗口而不是「丢弃旧事件」
   （`app.js:296-324`），注释记录了旧实现「滑到底停住后下一章永远不加载」的坑。
8. **书签标记用覆盖层按 Range 矩形定位**：`.nr-mark-layer` + 绝对定位 `.nr-mark-pin`
   （`app-menu.js:200-241`，`css:956-981`），不往正文插 DOM。

---

### 13.7 与 Shell 契约的偏差

逐条给证据与影响面（应按影响从大到小处理）：

1. **同一插件存在三套设置 UI，其中一套与壳完全重复。**
   证据：壳 `openSettingsModal`（`app.js:133`）+ 自绘阅读设置弹窗（`index.html:189-239`，字段写死）
   + 朗读设置页（`reader-voice-page.js`）。
   影响面：3 个入口 / 约 21 个表单控件；后端 `settings_schema` 与自绘字段两处定义，改一个设置项要动两处；
   且壳入口保存后**强制整页刷新**（`base.js:622`），阅读中的文档会被重载。
2. **`.view-body` 被插件重定义。**
   证据：`css:704-706` `.view-body{position:relative}`（`index.html:61` 同元素）。`base.css:237` 已定义同类。
   影响面：1 条规则，但这是壳的通用布局类；壳后续给 `.view-body` 加 `position` 或 `contain` 会与此冲突。
   两份插件（document-reader / image-viewer）各自做了同一件补充。
3. **自绘设置表单，未用 `createSettingsForm`。**
   证据：`.nr-setting-row`（`css:599-636`）与壳 `.field/.field-label/.field-range`（`base.css:182-206`）功能重叠。
   影响面：阅读设置 8 行 + 朗读设置 6 行 ≈ 14 个字段；壳的统一表单样式/校验升级无法惠及；
   滑杆数值展示（`.nr-setting-value{min-width:42px}` vs 壳 `.field-range-value{min-width:40px}`）为近重复实现。
4. **右键菜单自建列表渲染，分隔线自造类。**
   证据：`app-menu.js:75-91` 清空壳组件的 `element.innerHTML` 后重建 `<li>`；`css:590-596` `.nr-menu-sep`
   用 `padding:0 !important` 覆盖壳 `.context-menu li`。
   影响面：1 个菜单、5 个菜单项；若壳改 `.context-menu` 的 DOM 约定（如改用 `<button>`），此处静默失效。
5. **搜索框自绘，未用壳 `.search-input`。**
   证据：`.nr-search` 胶囊容器（`css:168-192`，`border-radius:999px`）vs `base.css:39-50` 的 `.search-input`。
   影响面：1 个输入框 + 清除按钮；两套搜索框外观并存（同一插件内 `#nr-toc-filter` 用的是壳的 `.search-input`，
   `index.html:179`），同一插件里出现两种搜索框样式。
6. **空态/加载态未用壳类。**
   证据：`.nr-empty`（`css:813-836`）替代 `.empty-state`（`base.css:428-431`）；`#document-loading` 用了
   `<div class="spinner">`（`app.js:577`）但 `document-reader.css` 未定义 `.spinner`，也没有 `.obx-skeleton`。
   影响面：4 处空态模板（书架/搜索/书签/目录）+ 1 处加载胶囊；语义为「空」时样式与壳的其他插件不一致。
7. **`document-reader.css` 内部重复定义。**
   证据：`.chapter-content` 在 `css:228-231` 与 `css:365-369` 各定义一次（后者补 `contain`/`overflow-anchor`）。
   影响面：1 个选择器、2 处；文件自述「不再重定义壳类、按四段归位」（`css:10-13`）但自身仍有重复段。
8. **死 token 与死类。**
   证据：`--nr-cover-frame/-spine/-bars`（`css:23-25`）无任何引用；`#app` 上定义却无人读。
   影响面：3 个变量，0 处使用；后续统一主题时容易被误认为有效色板入口。
9. **`.document-content-area` 完全隐藏滚动条。**
   证据：`css:216` + `css:233-236`。
   影响面：1 个滚动容器；与 `effects.css:133-139` 的 `.obx-scroll`「细滚动条悬停渐显」约定相反
   （该容器未挂 `.obx-scroll`，也不打算显示滚动条）。长文档只能靠键盘/翻页判断位置，进度条是唯一位置反馈。



