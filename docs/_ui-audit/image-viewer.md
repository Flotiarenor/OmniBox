# image-viewer 插件 UI 现状取证

审计范围：`plugins/image-viewer/frontend/`（只读取证，未改动任何文件）。
对照基线：`shell/frontend/public/shell/variables.css`（86 行）、`base.css`（432 行）、
`effects.css`（201 行）、`base.js`、`docs/plugin-guide.md` §4。

---

## 1. 概览

### 1.1 前端文件清单

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `index.html` | 205 | 唯一入口；含侧栏/工具栏/网格/3 个弹窗/扩展视图/重建进度卡的静态 DOM |
| `image-viewer.css` | 404 | 全部样式；无内部分段注释体系（顶部只说明依赖三个注入 CSS，`css:1-4`） |
| `js/justified-layout.js` | 47 | `JustifiedLayout.compute(images, containerWidth, targetHeight, gap=5)`：行高归一 + 末行不拉伸 |
| `js/app.js` | 280 | `ImageViewer` 类骨架：构造、`init()`、`_bindPluginLifecycle`、`loadExtensions/openExtensionView`、`_bindUI` |
| `js/app-albums.js` | 380 | `prototype` 分片：相册树过滤/折叠/提升、时间线、卡片渲染、二次排序、导航栈 |
| `js/app-nav.js` | 206 | `prototype` 分片：返回栈、**相册自绘右键菜单**、统计行、`FolderPicker` 接管、新建相册 |
| `js/app-grid.js` | 342 | `prototype` 分片：`loadImages`、瀑布布局渲染、搜索过滤、幻灯片、多选/移动/删除 |
| `js/app-refresh.js` | 176 | `prototype` 分片：刷新、全量/单相册重建、轮询进度、取消、重生成缩略图 |
| `js/app-settings.js` | 125 | `prototype` 分片：设置弹窗读写（per-folder 与全局两种作用域） |
| `js/app-utils.js` | 64 | `prototype` 分片：时长/月份/相对时间格式化、`_emptyHtml`、`_escapeHtml`/`_escapeAttr` |

分片机制：`app.js` 定义类，六个分片用 `Object.assign(ImageViewer.prototype, {...})` 回挂；
`index.html:191-199` 注释「顺序是硬约束」，实例化在 `:200-203`，契约由
`tests/js/image_viewer_app_split.mjs` 把关。

### 1.2 页面形态

单页 + 三态视图切换（`mode`：`albums|children|images`，`currentView`：`albums|timeline|latest`），
无路由，但**内含一个 companion 内嵌视图**：`#extension-view` 用 `<iframe id="extension-frame">`
承载扩展插件（`index.html:65-73`，实现 `app.js:157-173`，由 `renderExtensions(..., {onEmbed})` 触发，
`app.js:141-144`）。三个弹窗：新建相册（`index.html:76-90`）、移动（`:92-101`）、设置（`:103-171`）。
全量重建进度卡常驻右下角（`:173-186`）。

### 1.3 Shell 布局类 / 组件使用情况（用）

布局类：`view-sub-sidebar`（`index.html:11`）、`view-body`（`:32`）、`view-toolbar`（`:33`）、
`toolbar-group`（`:34,41`）、`view-content`（`:59`）、`pagination-bar`（`:63`）、`modal`/`modal-box`/`modal-body`/`modal-footer`
（`:76-171`）、`obx-nav-item`（`:21-23,27`）、`btn`/`btn-sm`/`btn-primary`/`btn-danger`（`:35,48-55,68,86-87…`）、
`hidden`、`obx-scroll`（`:19,59,106`）、`obx-glass`（`:11`）、`obx-anim-scale`（`:77,93,104`）、
`tree-container`（`:95`）、`search-input`（`:82`）、`iv-dirbrowser-list`（壳组件内部）。

Shell 组件：`createLightbox`（`app.js:54`）、`createPagination`（`:55-57`）、`createContextMenu`（`:58-66`）、
`createTree`（`:266-270`）、`confirmDialog`（4 处：`app-refresh.js:43,49,164`；`app-grid.js:301`）、
`Toast`（约 30 处）、`Utils.debounce/escapeHtml`（`app.js:213`、`app-utils.js:49-51`）、
`renderExtensions`（`app.js:141`）、`window.FolderPicker`（`app-nav.js:158`）、
`window.PluginLifecycle` 三钩子（`app.js:92-119`）。

---

## 2. 布局骨架

### 2.1 顶层容器树

```
body
└─ #app                        display:flex; height:100vh; overflow:hidden; 背景两层渐变  (css:13-20)
   ├─ aside.view-sub-sidebar.iv-sidebar.obx-glass        (index.html:11)
   │  ├─ .iv-brand              padding 18px 16px 14px; gap 12px  (css:31)
   │  │  ├─ .iv-brand-icon      40×40; radius 12px; 渐变底  (css:32-37)
   │  │  └─ .iv-brand-text → .iv-brand-title(15px/700) / .iv-brand-sub(11px)
   │  ├─ nav.iv-nav.obx-scroll   flex:1; overflow-y:auto; padding 8px 10px; gap 3px  (css:41)
   │  │  ├─ .iv-nav-label       padding 12px 10px 5px; 11px/700; uppercase  (css:204-208)
   │  │  ├─ button.obx-nav-item.iv-nav-item ×3（全部相册/时间线/最近添加）  (index.html:21-23)
   │  │  └─ #iv-extensions      renderExtensions 容器  (index.html:24)
   │  ├─ .iv-nav-footer         padding 8px 10px; border-top  (css:209-210)
   │  │  └─ button.obx-nav-item.iv-nav-item（新建相册入口 <svg class="obx-icon"><use href="#plus"></use></svg>新建相册，width:100%）  (index.html:27)
   │  └─ .iv-sidebar-footer#iv-stats   padding 11px 14px; 11px  (css:50)
   └─ .view-body                (index.html:32)  ← 插件重定义：position:relative (css:368-370)
      ├─ .view-toolbar.iv-toolbar      48px（壳）; gap:12px  (css:53)
      │  ├─ .toolbar-group.iv-view-heading   ←返回 / 标题(15px/700) / 副标题(11px)
      │  └─ .toolbar-group[style=margin-left:auto]   行内样式  (index.html:41)
      │     └─ .iv-search + #iv-selection-count + 7 个 .btn（幻灯片/多选/删除/移动/更新缩略图/刷新/全量重建/设置）
      ├─ .view-content.iv-content.obx-scroll#iv-content   padding 16px; background:transparent  (css:70)
      │  ├─ #iv-albums             相册网格容器（.iv-grid 由 JS 包一层，css:72-76）
      │  └─ #image-grid.iv-image-grid   position:relative; 子元素全 absolute  (css:119)
      ├─ #pagination.pagination-bar  壳组件，高 48px（base.css:81-86）
      └─ #extension-view.extension-view     absolute; inset:0; z-index:20  (css:371-381)
         ├─ .extension-view-header   padding 8px 14px; border-bottom
         └─ .extension-view-body → iframe 100%×100%

   （.modal ×3 与 .rebuild-progress-card 挂在 #app 内、.view-body 外）  (index.html:76-186)
```

### 2.2 关键尺寸

| 区域 | 值 | 来源 |
| --- | --- | --- |
| 侧栏宽 | `240px`（`css:24`）显式写死，**未用** `var(--sub-sidebar-width)`；`max-width:860px` 时降到 `200px`（`css:198-201`） | 插件 |
| 工具栏高 | 48px（壳 `base.css:240`）；插件只加 `gap:12px`（`css:53`） | 壳 |
| 内容区 padding | `16px`（`css:70`） | 插件（覆盖壳 `.view-content` 的 16px，值相同） |
| 相册网格 | `minmax(170px,1fr)`；gap 16px（`css:72-76`）；窄屏 `minmax(140px,1fr)`（`css:200`） | 插件 |
| 相册卡 | `aspect-ratio:1` 封面（`css:94`）；radius `var(--iv-radius)`=14px；`contain-intrinsic-size:260px` | 插件 |
| 图片瓦片 | 绝对定位，尺寸由 `JustifiedLayout.compute` 算出：目标行高默认 `200px`（`app.js:27`，设置项 100–400），gap 固定 `5`（`app-grid.js:72`） | 插件 |
| 重建进度卡 | `position:fixed; right:16px; bottom:16px; width:320px`；进度条高 `6px`（`css:284-297,330-335`） | 插件 |
| 扩展视图头 | `padding:8px 14px`（`css:382-390`） | 插件 |

### 2.3 滚动方式

- 唯一滚动容器是壳的 `.view-content#iv-content`（`overflow-y:auto`），并显式挂 `.obx-scroll`（`index.html:59`）。
- 相册网格与图片网格**不各自滚动**：`#image-grid` 由 JS 设 `style.height = totalHeight`（`app-grid.js:74`），
  滚动全部交给外层；离开相册时把高度重置为 `0`（`app-albums.js:50`）。
- 侧栏 `nav.iv-nav`（`css:41`）与设置弹窗正文 `.iv-settings-body`（`css:179`）各自滚动，均挂 `.obx-scroll`。
- 从列表进入图片后返回会恢复 `#iv-content.scrollTop`（`app-albums.js:356-371`，`requestAnimationFrame` 内恢复）。

---

## 3. 设计 token 使用

### 3.1 实际引用的壳 token（`var(--...)` 共 95 处引用 / 404 行 CSS）

背景：`--bg-app`(19,377,403)、`--bg-surface`(82,122,158,163,217,291,387)、`--bg-hover`(94,99,226,281,317,333)。
文本：`--text-primary`(38,55,154,158,163,226,278,296,318,328)、`--text-secondary`(39,50,56,116,169,174,309,350)、
`--text-muted`(181,196,207,221,224,236)、`--text-on-accent`(35)（仅 `.iv-brand-icon`）。
边框：`--border`(27,50,83,91,157,162,175,209,216,270,292,388)。
强调：`--accent`(9,35,36,63,65,91,155,170,220)。
阴影/动效：`--shadow-sm`(122)、`--shadow-md`(125)、`--transition-fast`(123,132,218,279)。
`effects.css` token：`--obx-shadow-2`(92,271,294)。
**未引用的壳 token**：`--bg-sub-sidebar`（靠壳类生效）、`--bg-active`、`--bg-overlay(-strong)`、`--border-dark`、
`--danger`/`--danger-hover`/`--danger-soft`/`--success`/`--warning`、`--nav-width`、`--radius`/`--radius-sm`/`--radius-lg`、
`--transition-normal`。

### 3.2 自有变量与 `:root`

**插件直接改写 `:root`**（`css:6-11`），这是壳 `variables.css:4-52` 的同名层：

```css
:root { --iv-radius: 14px; --iv-radius-sm: 10px; --iv-gradient: …; --iv-accent-soft: …; }
```

`--iv-radius`(14px)/`--iv-radius-sm`(10px) 与 `effects.css:14-15` 的 `--obx-radius:14px` / `--obx-radius-sm:10px`
数值完全相同，属重复声明。另有 3 处元素级内联样式由 JS 写：卡片 `left/top/width/height`（`app-grid.js:99`）、
进度条 `width`（`app-refresh.js:115-119`）、相册菜单 `left/top`（`app-nav.js:99-100`）。

### 3.3 硬编码颜色字面量

grep（限 `image-viewer.css`）：
- 正则 `#[0-9a-fA-F]{3,8}\b` → **9 处**
- 正则 `rgba?\(` → **11 处**
- 正则 `hsla?\(` → **0 处**
- 三者合计命中行数：**17 行 / 404 行（4.2%）**
（`index.html` 内 0 处；插件 JS 内也无颜色字面量）

典型 5 例：

| 位置 | 字面量 | 说明 |
| --- | --- | --- |
| `css:103,143,260` | `rgba(0,0,0,0.55)` / `rgba(0,0,0,0.62)` | 相册数量徽标、圆圈数量角标、相册标签底（三种黑度并存） |
| `css:142-146` | `#fff` + `rgba(255,255,255,0.9)` 边框 + `rgba(0,0,0,0.35)` 阴影 | `.iv-count-badge` 单条规则里叠了 4 种字面量 |
| `css:241-242,259-263` | `#fff` + `rgba(0,0,0,0.25)` | `.iv-time-badge` / `.iv-album-tag`（同一套渐变底 + 黑阴影重复两遍） |
| `css:249,254` | `rgba(0,0,0,0.45)` → hover `rgba(0,0,0,0.65)` | `.iv-album-menu` 悬浮按钮及其 hover |
| `css:113` | `#ffd166` | 相册星标激活色（唯一的实色强调，与 `--warning:#ffc107` 不同源） |
| `css:359` | `var(--text-danger, #e5484d)` | 引用了**不存在的 token 名** `--text-danger`（壳只有 `--danger`），永远走兜底值 |

### 3.4 `data-theme` 处理

插件侧 **零处理**：`grep -r "data-theme" plugins/image-viewer/frontend` 无命中。
主题由壳注入的引导脚本同步到 iframe 的 `<html>`（`shell/backend/file_server.py:70-76`，
含 `MutationObserver` 跟随父窗口变化），自定义颜色同样以 CSS 变量写到 iframe `documentElement`
（`:77-84`）。插件盲依赖这些 token，**未出现** `prefers-color-scheme` 或主题分支。

---

## 4. 组件与命名约定

### 4.1 自有类名前缀清单

主前缀 `iv-`，另有 4 个 `extension-view*`、6 个不再带前缀的 `rebuild-progress*`：

- 侧栏/品牌：`.iv-sidebar .iv-brand .iv-brand-icon .iv-brand-text .iv-brand-title .iv-brand-sub .iv-nav .iv-nav-label .iv-nav-item .iv-nav-footer .iv-sidebar-footer`
- 工具栏：`.iv-toolbar .iv-view-heading .iv-view-title .iv-view-sub .iv-search .iv-search-ico .iv-search-clear .iv-selection-count`
- 卡片/网格：`.iv-grid .iv-album .iv-album-cover .iv-cover-fallback .iv-album-badge .iv-album-star .iv-album-info .iv-album-name .iv-album-count .iv-album-menu .iv-album-tag .iv-album-tag-hot .iv-count-badge .iv-image-grid .iv-image-card`
- 时间线：`.iv-timeline-section .iv-time-label .iv-time-badge`
- 菜单：`.iv-context-menu`（自绘）
- 设置：`.iv-setting-item .iv-setting-section .iv-setting-section-first .iv-setting-note .iv-setting-note-after-input .iv-settings-body .iv-check .iv-field .iv-range`
- 空态：`.iv-empty .iv-empty-icon .iv-empty-text .iv-empty-hint`
- 进度：`.rebuild-progress-card .rebuild-progress-header .rebuild-progress-hide .rebuild-progress-body .rebuild-progress-text .rebuild-progress .rebuild-progress-bar .rebuild-progress-current .rebuild-progress-speed .rebuild-errors` + 关键帧 `obxRebuildSlide`（`css:343-346`，**借用壳 `obx` 命名空间**）
- 扩展视图：`.extension-view .extension-view-header .extension-view-body`
- 徽标：`.iv-album-badge`（张数）、`.iv-count-badge`（圆圈数字）、`.iv-album-tag(-hot)`（已收纳/已提升/空相册）、`.iv-time-badge`、`.iv-album-star`
- Toast **无自有实现**，全量用壳 `Toast.*`；骨架屏**无**（`.obx-skeleton` 未用）

### 4.2 「壳已提供但插件又实现一遍」

| 能力 | 壳实现 | 插件实现 | 差异 |
| --- | --- | --- | --- |
| 右键菜单（图片） | `createContextMenu`，4 项一次传入（`app.js:58-66`） | **已用**，条目固定不重建；右键时只改变选中集再 `contextMenu.show(x, y, {url})`（`app.js:254-264`） | 符合契约（`document-reader` 是重建 `<li>` 列表的写法，两者不同） |
| 右键菜单（相册） | 同上 | **完全自绘** `.iv-context-menu`（`css:266-281` + `app-nav.js:96-134`）：`position:fixed` 但**无 z-index 之外的定位修正**，手算 `Math.min(e.clientX, innerWidth-160)`；条目 `<button>` 而非壳的 `<li>` | 视觉不同：壳 `radius:var(--radius)`=6px / `padding:4px 0` / 仅 `box-shadow`；插件 `radius:12px` / `padding:5px` / `backdrop-filter:blur(18px)` / 有 `obxPopIn` 入场动画。行为不同：插件菜单**不随滚动或窗口尺寸变化重定位**，且只有 `document.click` 关闭（`app.js:278`），无 `Escape` |
| 设置表单 | `createSettingsForm` + `.field*`（`base.js:419-563`） | 自绘 `.iv-setting-item`（flex + `justify-content:space-between`，`css:150-171`）+ `.iv-check` + `.iv-setting-section` 分隔标题 | 布局逻辑不同（壳是 label 在上、纵向 flex；插件是 label 左值右）；壳的 range 数值显示（`.field-range-value`）由插件改为 label 内联 `<span id="setting-row-height-val">`（`index.html:114`） |
| 统一设置弹窗 | `openSettingsModal`（自带保存 + 刷新 + Toast） | 自绘 `#settings-modal` + `openSettingsModal()/closeSettingsModal()/saveSettings()`（`app-settings.js:15-124`） | **命名冲突风险**：插件方法名与壳全局函数 `openSettingsModal` 同名（`base.js:566`）；插件保存后自行 `Toast.success('设置已保存')` + 条件 `Bridge.call('refresh')` + 重载视图（`app-settings.js:112-120`），未走壳的整页刷新 |
| 相册卡片网格 | `createCardGrid` | 未用；自绘 `.iv-album` + `_renderAlbumCards`（`app-albums.js:126-182`） | 卡片结构差异大（封面 `aspect-ratio:1` + 悬浮 ⋯ 按钮 + 三种标签 + 可选星标） |
| 空状态 | `.empty-state`（`base.css:428-431`） | `.iv-empty`（`css:190-196`）：`min-height:260px` + 44px 图标（`obxFloat` 无限浮动）+ 三行文案 | 多出图标与提示行；4 处调用（`app-albums.js:93`、`app-grid.js:50,62,200`） |
| 加载态 | `.obx-skeleton` | 复用壳 `.loading`（`app-grid.js:18` 写 `<div class="loading">图片加载中…</div>`） | 符合契约；但无骨架屏 |
| 分页 | `createPagination` + `.pagination-bar` | **已用**（`app.js:55-57`，`app-grid.js:60`） | 符合契约 |
| 灯箱 | `createLightbox` | **已用**（`app.js:54`，`app-grid.js:143,160,218,288`） | 符合契约 |
| 目录树 | `createTree` | **已用**（移动弹窗，`app.js:266-270`） | 符合契约；但容器 `#move-tree` 用 `.tree-container` 且行内 `min-height:200px`（`index.html:95`） |
| 目录选择器 | `FolderPicker`（`shell/folder-picker.js`） | **已迁走**（`app-nav.js:150-165`），设置弹窗只保留写回 `root_dir/extra_roots` | 符合契约（`css:185-188` 注释明确不再保留副本） |
| 工具栏右侧靠右 | `.toolbar-group` 自身无 `margin-left:auto` | 用行内样式 `style="margin-left:auto;"`（`index.html:41`） | 与 document-reader 的 `.nr-toolbar-right{margin-left:auto}`（CSS 类）做法不一致 |
| 滚动条 | `.obx-scroll` | 已挂 3 处（`index.html:19,59,106`） | 符合契约 |

---

## 5. 交互约定

### 5.1 设置入口与保存反馈

单入口：工具栏设置入口（静态 HTML `<svg class="obx-icon"><use href="#settings"></use></svg> 设置`，`index.html:55`）→ 自绘弹窗（`app.js:202` → `app-settings.js:15`）。
保存按钮 `保存并刷新`（`index.html:168`）→ `saveSettings()`：

- 作用域分支：勾选「仅应用于当前文件夹」时写 `Bridge.call('save_settings', this.currentPath, {...})`
  并单独写全局的二次排序；否则写全局并 `clear_folder_settings(currentPath)`（`app-settings.js:96-105`）。
- 反馈：`Toast.success('设置已保存')`（`:113`），随后若非文件夹级则 `await Bridge.call('refresh')`
  作废后端索引并 `loadAlbums()`，最后按当前模式 `loadImages`/`showAlbums`（`:114-120`）。
- 失败：`Toast.error('保存设置失败')`（`:122`）。
- 「图片文件夹」列表由 `FolderPicker` 持有，保存时 `roots[0]` → `root_dir`、其余 → `extra_roots`；
  列表为空时**显式写空串**以清掉旧值（`app-settings.js:88-95`）。

### 5.2 错误提示方式

全量 Toast + 两处内联兜底，无错误页：

- 删除/移动部分失败：`Toast.error(\`部分删除失败: ${result.errors.join('; ')}\`)`（`app-grid.js:305`）、
  `部分移动失败`（`:333`）、`部分失败`（`app-refresh.js:168`）。
- 重建失败：`Toast.error(\`${label}失败：${e.message || e}\`)`（`app-refresh.js:80`），错误摘要同时写进
  `.rebuild-errors`（`css:355-362`，`max-height:60px; overflow-y:auto`，色 `var(--text-danger, #e5484d)`）。
- 列表/设置加载失败**静默**：`loadAlbums()` 与 `loadSettings()` 的 catch 均为空（`app-albums.js:20-22`、
  `app.js:126`），`openSettingsModal` 的 catch 也是空（`app-settings.js:61`）——失败时弹窗停在默认值。
- 图片加载失败有**内联重试**：`onerror` 先加 `?r=<时间戳>` 重试一次，再失败替换为
  `.iv-cover-fallback`（相册卡 `app-albums.js:157` 的行内 `onerror`；图片瓦片 `app-grid.js:111-120`）。

### 5.3 选择模型

- **单图多选（点选）**：`isMultiSelectMode` + `selectedImages: Set`（`app.js:34-35`），
  卡片加 `.selected` 类（`css:126` `outline:2px solid var(--accent); outline-offset:-2px`），
  工具栏显示 `已选 N 项` 胶囊 `.iv-selection-count`（`app-grid.js:259-264`）。
- **右键即选**：右键未选中的图会先清空并选中它（`app.js:259-262`）。
- **无框选、无长按、无 shift 范围选**（grep `mousedown/mousemove/touchstart` 仅命中弹窗遮罩的
  `pointerdown`，`app.js:273-277`）。
- 多选模式开关改变按钮文案为 `退出多选`（`app-grid.js:250`）。

### 5.4 右键菜单

两套并存，互不复用：

1. 图片（壳组件）：`#image-grid` 的 `contextmenu` 委托到 `.iv-image-card[data-url]`，
   4 项 —— 查看原图 / 多选此图 / 移动到此... / 删除（`danger`）（`app.js:59-64,254-264`）。
   相册卡片不参与（`:256` 因无 `dataset.url` 直接返回）。
2. 相册（自绘）：卡片右上角 `⋯` 按钮（悬浮才 `opacity:1`，`css:246-253`）→ `showAlbumMenu`，
   条目按条件组装（展开/收纳、提升/收回、不再显示此空相册、重建此相册缩略图），点击后 `_closeAlbumMenu()`（`app-nav.js:61-135`）。

### 5.5 键盘快捷键

**无任何键盘处理**：`grep -n "keydown|keyup|keypress|keyCode|Escape|ArrowLeft"` 在
`plugins/image-viewer/frontend/js/*.js` 零命中。幻灯片只能靠工具栏按钮与灯箱内的箭头
（后者由壳的 `createLightbox` 提供，`base.js:806` `onKey`）。多选模式也**没有 Escape 退出**。

### 5.6 长任务进度与取消

- 全量重建：`_startRebuildTask`（`app-refresh.js:54-85`）显示右下角 `#rebuild-progress` 卡，
  `_waitRebuildDone()` **每 500ms 轮询** `Bridge.call('rebuild_status')`（`:87-104`）。
- 卡片内容：`N / M` 计数、进度条（有 total 时按百分比并把 `animation` 置 `none`，无 total 时
  退回 40% 宽 + `obxRebuildSlide` 不确定动画，`app-refresh.js:112-121`）、当前项、速度与剩余时间
  （`张/秒 · 剩余约 …`，`:134`）、错误摘要。
- 取消：`#rebuild-progress-cancel` → `Bridge.call('rebuild_cancel')` + `Toast.info('正在取消全量重建…')`
  （`:152-159`）；另有 `—` 隐藏按钮 `hideRebuildProgress()`（`:147-150`，隐藏**不取消**）。
- 结果：完成 `Toast.success(\`${label}完成\`)`；取消 `Toast.warning(\`${label}已取消\`)`（`:64,78`）。
- 单相册重建、重生成缩略图走同一条 `_startRebuildTask`（`:48-52`，与 `rebuildFolder`/`refreshSelectedThumbs`）。

### 5.7 空态 / 加载 / 错误态文案与样式类

| 态 | 样式类 | 文案 |
| --- | --- | --- |
| 无相册 | `.iv-empty`（`app-albums.js:93`） | `icon:images` + 「暂无相册」；提示行「点击左侧「新建相册」开始整理」/「换个关键词试试」 |
| 相册无图片 | `.iv-empty`（`app-grid.js:50`） | `icon:images` + 「此相册暂无图片」（无 hint） |
| 加载失败 | `.iv-empty`（`app-grid.js:62`） | `icon:triangle-alert` + 「图片加载失败」 |
| 搜索无果 | `.iv-empty`（`app-grid.js:200`） | `icon:search-x` + 「没有匹配的图片」；提示行「换个关键词试试」 |
| 加载中 | 壳 `.loading`（`app-grid.js:18`） | `图片加载中…` |
| 重建中 | `.rebuild-progress-*` | `正在扫描并生成缩略图，请稍候…` / `正在处理：<文件名>`（`app-refresh.js:125`） |
| 侧栏统计加载 | `.iv-sidebar-footer` | `正在读取…`（`index.html:29`），完成后 `N 个相册 · M 张图片`（`app-nav.js:147`） |
| 序列截断警告 | 壳 Toast | `相册较大，连续浏览序列已截断（前 5000 张）`（`app-grid.js:38`） |

---

## 6. 特色设计（值得其他插件吸收）

1. **Justified 行式瀑布布局（`JustifiedLayout.compute`）而非 CSS 列瀑布**：
   先按 `ratio*targetHeight` 累加宽度切行，再按容器宽度反算该行统一高度，**末行不拉伸**
   （`justified-layout.js:29-33`），grid 容器统一给 `height: totalHeight`（`app-grid.js:74`）。
   解决的问题：CSS `columns` 瀑布的排列顺序是列优先（阅读顺序错乱）、且无法保证末行整齐；
   这里顺序即数据顺序，行高可随设置项（100–400px）实时变。
2. **「连续浏览序列」把分页/搜索/子文件夹统合成一个灯箱索引空间**：
   `currentAllImages` + `currentAllOffset` + `filteredSeqIndexes`（`app.js:22-24`），
   每个瓦片记录它在完整序列中的起点（`app-grid.js:81-90`），点击即 `lightbox.show(currentAllImages, seqIndex)`。
   解决的问题：第 2+ 页的瓦片、被搜索过滤后的瓦片、以及「子文件夹 = 该文件夹 p0 + 后续 p1…」三种偏移，
   用同一个序号空间表达，否则灯箱会错位打开到序列开头（注释 `app-grid.js:76-80`）。
3. **相册树折叠/提升是「视图配置」而非移动文件**：`albumConfig{collapsed,promoted,expanded,visible_empty_dirs}`
   （`app.js:29`），`_filterVisibleAlbums` 用 `readable`（递归可读数）+ 祖先折叠判定可见性
   （`app-albums.js:193-245`）。解决的问题：用户想「只看某个子树」「把深层作者目录拉到顶层」，
   但目录结构本身由磁盘决定、不能改；空目录还能在保留磁盘目录的前提下从视图隐藏（`app-nav.js:84-90`）。
4. **`_rememberScroll/_restoreScroll` + `navStack` 双重返回栈**：前者恢复滚动位置（`app-albums.js:356-371`，
   在 `requestAnimationFrame` 里写 `scrollTop` 以避开渲染重置），后者恢复「混合瀑布流逐层点入」的路径状态
   （`app.js:25`，`app-nav.js:11-59`）。解决的问题：相册树的深层钻取返回后不应回到顶部或根目录。
5. **生命周期钩子把「幻灯片」当作可暂停的视觉工作而非播放**：`onHide` 记住当前张数并停定时器、
   必要时收起灯箱；`onShow` 只在用户没主动停止（`_slideshowWanted`）时**原地续播**
   （`app.js:88-119`，`app-grid.js:206-238`）。解决的问题：常驻插件切到后台后页面不可见，
   继续 `setInterval` 换图纯属浪费；但用户切回来又希望接着看。
6. **缩略图失败双重兜底**：先 `?r=<时间戳>` 重试一次（绕过浏览器缓存与后端负缓存），
   再失败才换成 `Icons.html('icon:image-off')` 占位（`app-albums.js:157`、`app-grid.js:111-120`），并配 `dataset.r` 防重入。
   解决的问题：下载中断/替换后残留的坏缩略图会让整块区域永久空白。
7. **重建进度卡「隐藏 ≠ 取消」**：`—` 只隐藏卡片、任务继续；取消走独立按钮并要求确认
   （`app-refresh.js:42-52,147-159`）。解决的问题：长任务需要一个「先让开界面」的出口，
   而又不能让误点直接中断几分钟的批处理。

---

## 7. 与 Shell 契约的偏差

逐条给证据与影响面（按影响从大到小）：

1. **插件改写 `:root`。**
   证据：`css:6-11` 在顶层 `:root` 定义 `--iv-radius:14px`、`--iv-radius-sm:10px`、`--iv-gradient`、`--iv-accent-soft`。
   影响面：1 个选择器 / 4 个变量；与壳 `variables.css:4-52` 落在同一元素，虽变量前缀不同不至于覆盖壳 token，
   但契约（§4.1「不重复定义壳提供的东西」）要求插件变量收在插件容器上——`document-reader` 就是定义在 `#app`（`css:18-26`），
   两个插件做法不一致。`--iv-radius/-sm` 的值与 `effects.css:14-15` 的 `--obx-radius/-sm` 完全相同，属重复真源。
2. **`.view-body` 被重定义。**
   证据：`css:368-370` `.view-body{position:relative}`（对应 `index.html:32`）。壳 `base.css:237` 已定义同类。
   影响面：1 条规则；`document-reader.css:704-706` 有一模一样的补充，说明这是「弹窗/覆盖层定位」共性需求，
   而两个插件各写一遍且都定义在通用类上。
3. **相册右键菜单完全自绘，绕过壳的 `.context-menu` 与 `createContextMenu`。**
   证据：`app-nav.js:96-101` 建 `div.iv-context-menu` 挂到 `document.body`；`css:266-281` 自定圆角 12px /
   毛玻璃 18px / `obxPopIn` 动画 / `<button>` 子元素。
   影响面：1 个菜单、最多 4 个条目；同插件内出现两套右键菜单视觉（图片项走壳的 `<li>` 列表）。
   行为缺口：不监听 `scroll`/`resize` 重新定位（`position:fixed` + 一次算好的坐标），
   窗口尺寸变化或页面滚动后菜单会脱离卡片；也不响应 `Escape`。
4. **`--text-danger` 是不存在的 token。**
   证据：`css:359` `color: var(--text-danger, #e5484d)`；壳只有 `--danger`（`variables.css:30`）与 `--danger-hover`。
   影响面：1 处（`.rebuild-errors` 错误文字）；永远落到硬编码兜底 `#e5484d`，
   暗色主题下 `--danger` 应为 `#f85149`，此处**不会**跟随主题，且用户自定义危险色也无效。
5. **设置弹窗自绘，未用 `createSettingsForm`/`openSettingsModal`，且方法与壳全局函数同名。**
   证据：`app-settings.js:15` 定义了 `openSettingsModal()` 方法，壳 `base.js:566` 也定义全局 `openSettingsModal`。
   影响面：1 个弹窗 / 约 12 个控件；同名导致阅读代码时无法判断调用的是哪一个（插件内 `this.openSettingsModal()`
   在 `app.js:202`）；壳的字段样式、`per-folder` 语义约定（`plugin-guide §8.5`）与保存后刷新逻辑都不生效——
   插件改为自己调 `Bridge.call('refresh')`。
6. **硬编码颜色集中在「图上叠加层」，暗色/自定义色下不跟随。**
   证据：`css:103,109,113,130,142-146,241-242,249,254,259-263` 共 17 行含字面量，占全文 4.2%
   （对比 `document-reader.css` 25 行 / 2.4%）；`rgba(0,0,0,0.55)` 出现 3 次不同黑度（0.45/0.55/0.62/0.65）。
   影响面：6 个叠加态元素（相册数量徽标、圆圈角标、时间徽标、相册标签、⋯ 按钮、卡片文件名渐变底）
   + 1 个星标色；这些一律叠在缩略图之上，因此「黑底白字」在浅色主题下尚可接受，
   但同一插件内出现 4 档黑度、星标 `#ffd166` 与 `--warning:#ffc107` 两个真源。
7. **工具栏右侧靠右用行内样式。**
   证据：`index.html:41` `<div class="toolbar-group" style="margin-left:auto;">`；
   `document-reader` 用类 `.nr-toolbar-right{margin-left:auto}`（`document-reader.css:146-151`）。
   影响面：1 处；行内样式无法被主题或统一 CSS 覆盖，且同仓库两种写法并存。
8. **空态未用壳 `.empty-state`，且 4 处模板与 `document-reader` 的 `.nr-empty` 结构重复。**
   证据：`app-utils.js:38-44` 的 `_emptyHtml(icon,text,hint)` 与 `document-reader` 的
   `app-shelf.js:139-145` 同构（仅类名前缀 `iv-empty` / `nr-empty` 不同）。
   影响面：4 个调用点 + 2 组 CSS 规则（`css:190-196` / `document-reader.css:813-836`）。
9. **`.iv-sidebar` 宽度写死 240px，未用 `var(--sub-sidebar-width)`。**
   证据：`css:23-29`，另 `css:198-201` 在 `max-width:860px` 时降为 200px。
   影响面：1 条规则；壳若调整 `--sub-sidebar-width`（现 240px），本插件侧栏不会跟随
   （`document-reader` 未写宽度，直接吃壳的变量，行为不同）。
10. **相册菜单缺少退出路径与重定位。**
    证据：仅 `document.addEventListener('click', () => this._closeAlbumMenu())`（`app.js:278`）
    与菜单内点击（`app-nav.js:105`）；无 `Escape`（见 5.5）、无 `scroll`/`resize` 监听。
    影响面：1 个菜单；键盘用户无法关闭，滚动后菜单悬空在错误位置。另 `⋯` 按钮靠 CSS `opacity:0`
    悬浮才出现（`css:250-253`），触屏设备上无法发现该入口。
