# document-reader 插件 UI 现状取证

审计范围：`plugins/document-reader/frontend/`（只读取证，未改动任何文件）。
对照基线：`shell/frontend/public/shell/variables.css`（86 行）、`base.css`（432 行）、
`effects.css`（201 行）、`base.js`、`docs/plugin-guide.md` §4。

---

## 1. 概览

### 1.1 前端文件清单

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

### 1.2 页面形态

单页 + 多视图切换，无路由、无多 HTML 文件、无 companion 内嵌页：

- 书架态：左栏导航（全部/最近/书签/朗读设置），主区封面网格（`index.html:79-81`）。
- 阅读态：主区换成正文容器（`index.html:84-86`），左栏多出「当前文档」组（`index.html:43-51`）。
- 朗读设置：**覆盖主区的独立面板**，`position: absolute; inset: 48px 0 0 0`（`document-reader.css:709-717`），
  注释明确「不遮挡左侧栏」（`index.html:91`）。
- 两个弹窗：目录（`index.html:175-186`）、阅读设置（`index.html:189-239`）复用壳的 `.modal` 类，
  开合自实现（`app.js:598-608`，只 add/remove `active`）。
- 朗读控制卡：`document.body` 上的 `position: fixed` 浮动卡（`reader-tts.js:64-79`）。

### 1.3 Shell 布局类使用情况（用）

`view-sub-sidebar`（`index.html:14`）、`sub-sidebar-footer`（`index.html:57`）、`view-body`（`index.html:61`）、
`view-toolbar`（`index.html:62`）、`toolbar-group`（`index.html:63,68`）、`view-content`（`index.html:79`）、
`obx-nav-item`（`index.html:28,32,35,39,45,48`）、`btn`/`btn-sm`/`btn-primary`/`btn-danger`（`index.html:64,95,136,157,236`）、
`modal`/`modal-box`/`modal-body`/`modal-footer`/`search-input`（`index.html:175-186,189-239`）、`hidden`（多处）、
`obx-scroll`（`index.html:26,79,97`）、`obx-glass`（`index.html:14`）。

---

## 2. 布局骨架

### 2.1 顶层容器树

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

### 2.2 关键尺寸

| 区域 | 值 | 来源 |
| --- | --- | --- |
| 导航侧栏宽 | `var(--sub-sidebar-width)` = 240px | 壳 `base.css:251-259`（插件未覆盖宽度） |
| 工具栏高 | `var(--toolbar-height, 48px)` = 48px | 壳 `base.css:239-248`；`css:712` 的 `inset:48px` 是硬编码同值 |
| 内容区 padding | 书架 `18px`（`css:194-196`），正文 `24px clamp(24px,8vw,140px) 40px`（`css:219`） | 插件 |
| 正文行宽上限 | `.chapter-content{max-width:760px}`（`css:229-231`） | 插件，注释给出「一行 45~50 字」理由（`css:217-219`） |
| 封面卡比例 | `aspect-ratio: 3/4`（`css:281`） | 插件 |
| 进度条高 | `4px`（`css:246-252`） | 插件 |
| 朗读浮动卡 | `right:16px; bottom:18px`，按钮 `34×34` 圆形（`css:639-672`） | 插件 |

### 2.3 滚动方式

- 书架：滚动落在壳的 `.view-content`（`overflow-y:auto`，`base.css:277-282`）。
- 阅读：**壳的 `.view-content` 不参与**，改为 `.document-content-area` 自滚（`css:210-226`），
  显式 `scrollbar-width:none` + `::-webkit-scrollbar{display:none}`（`css:216,233-236`）——正文区刻意无滚动条。
- 侧栏 `nav.nr-nav`（`css:82-90`）、朗读页 `.nr-voice-body`（`css:742-749`）各自滚动并挂 `.obx-scroll`。
- 进度条通栏、不随正文缩进，注释为有意设计（`index.html:83`）。

---

## 3. 设计 token 使用

### 3.1 实际引用的壳 token（`var(--...)` 共 120 处引用 / 893 行 CSS）

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

### 3.2 自有变量与 `:root`

**未改写 `:root`**：自有变量定义在 `#app` 上（`css:18-26`），作用域比 `:root` 更窄：

```css
#app { --nr-radius: 12px; --nr-radius-sm: 8px; --nr-accent-soft: color-mix(...); }
```

另有 6 个阅读区变量由 JS 写在元素内联样式上：`--reader-font-size`、`--reader-line-height`、
`--reader-letter-spacing`、`--reader-bg-color`、`--reader-text-color`、`--reader-highlight`
（`settings.js:97-109`；CSS 侧默认值见 `css:220-224,503`）。

**死变量**：`--nr-cover-frame` / `--nr-cover-spine` / `--nr-cover-bars`（`css:23-25`）全仓无引用
（grep 仅命中定义处 3 行）；实际封面配色由 `utils.js:46-53` 的 `hsl(hue 26% 62%)` 等运行时算。

### 3.3 硬编码颜色字面量

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

### 3.4 `data-theme` 处理

插件侧 **零处理**：`grep -r "data-theme" plugins/document-reader/frontend` 无命中。
主题由壳在注入引导脚本里同步（`shell/backend/file_server.py:70-76`：读 `parent.document.documentElement`
的 `data-theme`，写到插件 iframe 的 `<html>`，并用 `MutationObserver` 跟随）。插件的「阅读主题」
（auto/sepia/dark/green/blue/custom）是**内容阅读配色**，与壳的浅/暗主题是两套正交概念，
`theme-auto` 才落到 `var(--bg-surface)/var(--text-primary)`（`css:986-990`）。

---

## 4. 组件与命名约定

### 4.1 自有类名前缀清单

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

### 4.2 「壳已提供但插件又实现一遍」

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

## 5. 交互约定

### 5.1 设置入口与保存反馈

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

### 5.2 错误提示方式

全量 Toast（壳组件），无内联错误条、无错误页：

- 打开失败：`Toast.error('打开文档失败')`（`app.js:471`）
- 打开外部程序：`Toast.error(result.error)` / `Toast.success('已交给系统程序打开')`（`app.js:519-520`）
- 翻页到边界：`Toast.info('已经是最后一章了'|'已经是第一章了')`（`app.js:290`）
- 朗读失败：`Toast.error(\`朗读失败：${...}\`)`（`reader-tts.js:272`）
- 静默失败（只 console，无用户可见提示）：文档列表加载（`app.js:339-342`）、书签列表（`app.js:354-356`）、
  进度保存（`app.js:558-560`）、阅读偏好保存（`settings.js:141-143`）。

### 5.3 选择模型

- **无框选、无长按**（grep `mousedown/mousemove/touchstart` 零命中）。
- 文本选择：`user-select: text !important`（`css:225,238-240`），选区语义只用于朗读/加书签
  （`app-menu.js:8-17` 限制选区必须落在 `.document-content-area` 内）。
- 书签视图无多选：逐条点删除按钮（`nrShelfIcon('icon:x')`，`app-shelf.js:98-105`）或整本「清空」（`:106-117`，循环 `_removeMark` 后
  `Toast.success('已清空这本书的书签')`）。

### 5.4 右键菜单

仅正文区（`app-menu.js:29-43`）。有选区时 4 项（复制 / 开始朗读（选中处）/ 添加书签 / 刷新），
无选区时 2 项（开始朗读（屏幕顶部那一句）/ 刷新），`debug.status_debug` 为真时追加「检查」
（`app-menu.js:61-73`，读 `Bridge.callSystem('system_get_config')`，结果缓存于 `_debugFlag`，`:94-104`）。
锚点在**右键那一刻**就解析并存入 `_menuAnchor`（`app-menu.js:36-42`），避免点击时选区已消失。

### 5.5 键盘快捷键（唯一一处 keydown：`app.js:220`）

仅阅读态生效，且 `INPUT/TEXTAREA/SELECT` 聚焦或 `.modal.active` 存在时直接返回（`app.js:221-224`）：

| 键 | 行为 |
| --- | --- |
| `ArrowUp` / `PageUp` | `_scrollArea(-0.8)` 上滚 0.8 屏（`app.js:226-230`） |
| `ArrowDown` / `PageDown` / `Space` | `_scrollArea(0.8)` 下滚（`app.js:231-236`） |
| `Escape` | `returnToShelf()` 退出阅读（`app.js:237-240`） |

另有鼠标翻页：翻页模式下点正文左 25% / 右 25%（`app.js:271-285`），有选区时不动（`:278-279`）。

### 5.6 长任务进度与取消

- 章节加载：`#document-loading` 胶囊（sticky 底部），`_showLoading(true/false)`（`app.js:570-584`）。
- 阅读进度：顶部通栏 4px 进度条，`_updateProgressBar()` 按 `(章索引 + 章内比例)/总章数` 写宽度
  （`app.js:532-538`）。
- 朗读进度：浮动卡三键（收起到侧边 `nrIcon('icon:maximize-2')` / 暂停 `nrIcon('icon:pause')` / 停止 `nrIcon('icon:square')`，`reader-tts.js:64-79`），
  `data-state="loading"` 时切换键 `nrPulse` 呼吸（`css:679-681`）；无进度百分比，只有句级高亮。
- 取消：朗读有停止；**章节加载无取消**（靠 `_token` 自增作废过期响应，`reader-engine.js:95-99`）。

### 5.7 空态 / 加载 / 错误态文案与样式类

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

## 6. 特色设计（值得其他插件吸收）

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

## 7. 与 Shell 契约的偏差

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
