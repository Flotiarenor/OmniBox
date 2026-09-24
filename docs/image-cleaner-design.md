# 相册清理插件（image-cleaner）设计文档

> 版本：v0.1（已实装）
> 目标形态：**Companion 插件**

## 1. 定位

`image-cleaner` 是 `image-viewer` 的伴侣插件，提供**全部相册范围**的重复 / 相似图片扫描与清理，不再把清理逻辑塞进宿主插件。

### 1.1 与宿主的关系

- 使用 `manifest.dependencies: ["image-viewer"]` 声明依赖，由 `PluginManager` 保证宿主先加载。
- 后端通过 `PluginBase.get_dependency('image-viewer')` 获取宿主实例，复用：
  - `get_data_root()` / `get_file_roots()`：定位相册根目录与文件服务安全根目录
  - `thumb_dir` / `ensure_thumb()`：让本插件前端也能直接使用 `/thumbs`
  - `delete_files()`：复用宿主的路径安全校验与缓存清理
- `image-viewer` 不再包含 `duplicate_scan` / `similar_scan` / 清理 UI。
- `image-cleaner` 通过 `get_extensions()` 注册到 Shell，`image-viewer` 左侧栏使用通用 `renderExtensions()` 渲染入口。
- `image-cleaner` 在 manifest 中声明 `hidden: true`，不会出现在外部 Shell 主导航；点击 image-viewer 左侧栏入口后，以 iframe 内嵌方式在 image-viewer 内部打开。

### 1.2 扫描范围

扫描范围为**全部相册**，即递归扫描宿主数据根目录下的所有图片文件：

- 跳过隐藏目录和 `.cache`
- 跨目录检测重复 / 相似图片
- 支持嵌套相册，不限于当前打开的一个目录

## 2. 目录结构

```
plugins/
└── image-cleaner/
    ├── manifest.json
    ├── backend/
    │   └── main.py
    └── frontend/
        ├── index.html
        ├── image-cleaner.css
        └── js/
            └── app.js
```

## 3. API

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `duplicate_scan` | 无 | `{groups, scanned}` | 全相册完全重复扫描 |
| `similar_scan` | `threshold` | `{groups, scanned}` | 全相册视觉相似扫描 |
| `delete_files` | `rel_paths` | `{deleted, errors}` | 复用宿主的删除接口 |
| `get_status` | 无 | `{host, root_dir, scope}` | 查看当前清理范围与宿主信息 |

## 4. 前端

`/plugins/image-cleaner/frontend/index.html` 作为内嵌页面，通过 `get_extensions()` 在 `image-viewer` 左侧栏挂载“相册清理”入口；点击后由 image-viewer 用 iframe 加载。页面包含：

- 完全重复 / 相似图片两个 Tab
- 全选组、勾选删除
- 缩略图通过 `Bridge.thumbUrl()` 展示，文件服务由插件代理到 image-viewer 根目录

## 5. UI 现状取证（前端与壳契约对照）

> 只读审计。契约基准：`shell/frontend/public/shell/{variables.css, base.css, effects.css, base.js}`
> 与 `docs/plugin-guide.md` §2.1 / §4.1 / §4.3。所有结论附 `文件路径:行号`。
> 本插件是 **Companion（内嵌型）**，宿主是 `image-viewer`：见 §5.1 与 §5.7 的前两条。

### 5.1 概览

**前端文件清单**

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `plugins/image-cleaner/frontend/index.html` | 43 | 入口（由扩展条目 `embedUrl` 指向），工具栏 + 两个 tab + 结果区 + 底部操作条的静态骨架，**全部元素都有 id** |
| `plugins/image-cleaner/frontend/image-cleaner.css` | 145 | 全部插件样式 |
| `plugins/image-cleaner/frontend/js/app.js` | 265 | 单个 `class ImageCleaner`：扫描（带缓存）、分页显示、勾选、删除 |

- **页面形态**：**内嵌 companion**，单视图、无侧栏、无路由。宿主 `image-viewer` 的左侧栏由 `renderExtensions` 渲染出扩展入口（`plugins/image-viewer/frontend/js/app.js:141-151`），点击走 `options.onEmbed`（`:143`）→ `openExtensionView(ext)`（`:157-165`）把 `ext.embedUrl` 塞进 `#extension-frame`。
- **注册声明**（后端）：`plugins/image-cleaner/backend/main.py:77-89` —— `host: 'image-viewer'`、`embedUrl: '/plugins/image-cleaner/frontend/index.html'`、`placement: 'sidebar'`、`section: '相册清理'`、`icon: 'icon:brush-cleaning'`。宿主同时传了 `title: '相册清理'`（`image-viewer/frontend/js/app.js:142`），与 `section` 同名。
- **JS 模块划分**：无模块拆分，单文件单类；依赖壳全局 `Bridge` / `Toast` / `confirmDialog` / `createLightbox` / `Utils`（`app.js:14/33/218/221/253`）。
- **是否使用 Shell 布局类**：**部分使用**——`.view-body`(`index.html:10`)、`.view-toolbar`(`:11`)、`.toolbar-group`(`:12/16`)、`.view-content`(`:21`)、`.btn/.btn-sm/.btn-danger`(`:17/23/24/32/33`)、`.active`(`:23`)、`.obx-scroll`(`:21`)。**未使用** `.view-sub-sidebar` / `.sub-sidebar-header` / `.sub-sidebar-footer` / `.pagination-bar` / `.modal` / `.empty-state` / `.obx-nav-item` / 任何 `.obx-anim-*`。
- **与壳的注入顺序**：壳把 `variables.css / base.css / effects.css / base.js / motion.js` 注入到 `</head>` **之前**（`shell/backend/file_server.py:797-798`），插件自己的 `<link href="image-cleaner.css">`(`index.html:7`) 排在其后，同优先级规则由插件胜出。

### 5.2 布局骨架

顶层容器结构树（`index.html:9-35`）：

- `body`（`base.css:6-11`：`background:var(--bg-app)`、`overflow:hidden`、字体栈 `-apple-system, …`）
  - `div#app.view-body`（`index.html:10`；壳类给 `flex:1; display:flex; flex-direction:column; overflow:hidden`，`base.css:237`；插件补 `display:flex; height:100vh; overflow:hidden`，`image-cleaner.css:1-5`）
    - `div.view-toolbar.cleaner-toolbar`（`index.html:18`；高度 `var(--toolbar-height,48px)` 来自 `base.css:239-248`，插件只改 `gap:12px`，`:7-11`）
      - `div.toolbar-group.cleaner-meta`（`index.html:19`）→ `span.cleaner-scope`「全部相册」+ `span.cleaner-root#cleaner-root`（`:20-21`）
      - `div.toolbar-group[style="margin-left:auto"]`（`index.html:23`）→ `span#cleaner-actions`（`:27`）包 `button#btn-rescan.btn.btn-sm` + `button#btn-settings.btn.btn-sm`。**内嵌进宿主时这一整组会被 `#cleaner-actions` 收起**，两个按钮同时挂到宿主扩展面板头部（见 §5.5「设置入口」与 `plugin-ui-guide.md` §3.5 的 `mountToolbar`）；脱离宿主打开本页时留在原位
    - `div.view-content.cleaner-content.obx-scroll`（`index.html:21`；壳给 `overflow-y:auto; padding:16px`，`base.css:277-282`；插件重复声明 `flex:1; min-height:0; overflow-y:auto; padding:16px`，`:50-55`）
      - `div.cleaner-tabs`（`index.html:22`）→ `button#tab-dupe.btn.btn-sm.active`、`button#tab-similar.btn.btn-sm`、`span#cleaner-scanned.cleaner-scanned`（`:23-25`）
      - `div#cleaner-results.cleaner-results`（`index.html:27`）→ 动态 `.cleaner-group` ×n（`app.js:99-114`）
    - `div.cleaner-footer`（`index.html:30`）→ `span#cleaner-selected`、`button#btn-keep-one-all.btn`、`button#btn-delete.btn.btn-danger`（`:31-33`）

**尺寸与滚动来源**

| 部位 | 数值 | 来源 |
| --- | --- | --- |
| `#app` 高度 | `100vh` | 插件（`image-cleaner.css:1-5`），**不是** group-mesh/image-viewer 的 `html,body{height:100%}` + `#app{height:100%}` 链（对比 `plugins/group-mesh/frontend/group-mesh.css:47-59`） |
| 工具栏高度 | `48px` | 壳 `.view-toolbar`（`base.css:239-241`），插件未覆盖 |
| 内容区滚动 | `.view-content{overflow-y:auto}` | 壳（`base.css:277-282`）+ 插件重复声明（`image-cleaner.css:50-55`）；滚动条样式走壳 `.obx-scroll`（`effects.css:140-183`，`index.html:21` 有类） |
| 底部操作条 | `padding:10px 16px`、`border-top:1px solid var(--border)`、右对齐 `gap:12px` | 插件（`image-cleaner.css:74-81`）——**自绘**，壳无对应类（`.sub-sidebar-footer` 是侧栏用的，且 `padding:10px 16px` 巧合相同，`base.css:270-276`） |
| 分组卡 | `border-radius:12px`、`padding:12px`、`margin-bottom:12px` | 插件（`image-cleaner.css:87-93`） |
| 缩略图卡网格 | `repeat(auto-fill, minmax(140px,1fr))`、`gap:8px` | 插件（`image-cleaner.css:105-109`） |
| 缩略图 | `42px × 42px`、`border-radius:6px`、`object-fit:cover` | 插件（`image-cleaner.css:119-126`） |
| tab 徽标 / 根目录徽标 | `border-radius:999px`、`padding:2px 8px`；根目录 `max-width:240px` + 省略号 + 等宽字体 | 插件（`image-cleaner.css:18-42`） |

**内嵌进宿主后的几何（宿主侧，非本插件 CSS）**：`#extension-view` 是 `position:absolute; inset:0; z-index:20`（`plugins/image-viewer/frontend/image-viewer.css:371-378`），其 `.extension-view-header{padding:8px 14px}`（`:382-390`）里有一个 `14px/600` 的标题和一个 `.btn.btn-sm`「返回相册」；iframe 本身 `width:100%;height:100%;border:none;background:var(--bg-app)`（`:399-404`）。**因此内嵌态纵向是「宿主扩展头 + 本插件 48px 工具栏」两条横条**，两者高度/底色不同（宿主头 `8px 14px` 无固定高，本插件 48px）。

### 5.3 设计 token 使用

**实际用到的壳变量（`image-cleaner.css` 全部 16 处 `var(--…)`）**

| token | 出现行 |
| --- | --- |
| `--bg-hover` | 24（带回退）、38（`color-mix` 内）、117（带回退）、124 |
| `--bg-surface` | 88（带回退 `#fff`） |
| `--text-primary` | 25 |
| `--text-secondary` | 41、46、85、129、144 |
| `--text-on-accent` | 64（带回退 `#fff`） |
| `--border` | 80、89 |
| `--accent` | 63（带回退 `#4f6ef7`）、136 |

**插件自有变量 / `:root`**：**零**。全文没有 `:root` 块，没有 `--cleaner-*` 之类的自有 token，全部尺寸与圆角都是字面量（`12px` 卡圆角、`8px` 文件圆角、`6px` 缩略图圆角——三套圆角互不相干，也不等于壳的 `--radius(6px)/--radius-sm(4px)/--radius-lg(10px)`，`variables.css:40-42`）。

**硬编码颜色字面量**：正则 `#[0-9a-fA-F]{3,8}\b|rgba?\(` 对 `image-cleaner.css` 命中 **6 行**，全部是 `var()` 的第二参数（回退值），逐条：

1. `:24` `var(--bg-hover, rgba(128,128,128,.1))`
2. `:38` `color-mix(in srgb, var(--bg-hover, rgba(128,128,128,.1)) 70%, transparent)`
3. `:63` `background: var(--accent, #4f6ef7)` —— **回退色与壳默认 `--accent: #0078d4` 不同**（`variables.css:28`），脱离壳时是另一种蓝
4. `:64` `color: var(--text-on-accent, #fff)`
5. `:88` `background: var(--bg-surface, #fff)` —— 壳的深色主题下 `--bg-surface:#161b22`（`variables.css:57`），回退值 `#fff` 只在变量缺失时生效
6. `:117` `var(--bg-hover, rgba(128,128,128,.08))`

可视为「纯字面量」的只有 `:63`/`:64`/`:88` 三个回退值；其余 `:24`/`:117` 用的中性灰是刻意的深浅色通用值。**JS 里没有颜色字面量**，但用内联样式隐藏占位：`app.js:110` `onerror="this.style.display='none'"`。

**data-theme 处理**：**无**（grep `data-theme|color-scheme` 在 `image-cleaner/frontend` 零命中）。主题完全依赖壳注入脚本同步 iframe 的 `data-theme`（`file_server.py:71-76`）；插件 CSS 全部走 token，因此深色**能跟随**——但 `:88` 的回退 `#fff` 与 `:24` 的 `rgba(128,128,128,.1)` 在变量意外缺失时会破色。

### 5.4 组件与命名约定

**前缀**：`cleaner-`。完整清单（16 个顶层选择器，另有 4 个后代选择器，全部定义在 `image-cleaner.css`）：`.cleaner-toolbar`(`:7`)、`.cleaner-meta`(`:12`)、`.cleaner-scope`(`:18`)、`.cleaner-root`(`:31`)、`.cleaner-scanned`(`:43`)、`.cleaner-content`(`:50`)、`.cleaner-tabs`(`:56`)、`.cleaner-tabs .btn.active`(`:62`，唯一复用壳类的选择器)、`.cleaner-results`(`:67`)、`.cleaner-more`(`:70`)、`.cleaner-footer`(`:74`)、`.cleaner-selected`(`:82`)、`.cleaner-group`(`:87`)、`.cleaner-group-head`(`:94`)、`.cleaner-files`(`:105`)、`.cleaner-file`(`:110`)、`.cleaner-empty`(`:141`)；后代选择器 `.cleaner-group-head span`(`:100`)、`.cleaner-file img`(`:119`)、`.cleaner-file span`(`:127`)、`.cleaner-file input`(`:135`)。

按类别：

- **按钮**：无自有按钮类，全部是壳的 `.btn` / `.btn-sm` / `.btn-danger`（`index.html:17/23/24/32/33`）；只覆写 tab 选中态 `.cleaner-tabs .btn.active`(`:62-66`)
- **卡片**：`.cleaner-group`(`:87`) 分组卡、`.cleaner-file`(`:110`) 文件卡（`42px` 缩略图 + 文件名 + checkbox）
- **弹窗**：**无自有实现**，删除确认走壳 `confirmDialog`(`app.js:221`)，渲染的是壳的 `.modal.modal-confirm`（`base.js:398-403`）
- **菜单**：无
- **进度**：**无进度组件**，只有文字「扫描中…请稍候」(`app.js:54`)
- **toast**：无自有实现，走壳 `Toast.warning/error/success`(`app.js:218/226/228/246`)
- **空状态**：`.cleaner-empty`(`:141-145`，`text-align:center; padding:48px 16px`)——**自绘**，未用壳 `.empty-state`
- **骨架屏**：**无**（不用 `.obx-skeleton`，扫描态是纯文字）
- **徽标**：`.cleaner-scope`（「全部相册」胶囊，`--bg-hover` 底 `999px` 圆角，`:18-30`）、`.cleaner-root`（根目录，`color-mix` 底 + 等宽字体 + `max-width:240px` 省略，`:31-42`）、`.cleaner-scanned`（「已扫描 N 张」，`:43-49`）、`.cleaner-selected`（「已选 N 张」，`:82-86`）
- **分页/树/右键菜单/灯箱/卡片网格**：`createPagination` / `createTree` / `createContextMenu` / `createCardGrid` **均未调用**；`createLightbox` 调用了（`app.js:14-16`），但仅在宿主不可用时兜底（见 §5.5）

**「Shell 已提供但插件又实现一遍」的能力**

| 能力 | Shell 提供 | 插件实现 | 差异 |
| --- | --- | --- | --- |
| 空状态 | `.empty-state`（`base.css:428-431`：`display:flex; align-items:center; justify-content:center; height:100%; min-height:300px; font-size:16px`） | `.cleaner-empty`(`:141-145`) | 插件是 `text-align:center` 块 + `padding:48px 16px`，**不带 `min-height:300px`、不垂直居中、字号继承 13px**；三处文案现已改用壳的 `.empty-state`，图标走 `.empty-state-icon` + 图标集（`app.js:92` 的 `#triangle-alert`、`:104` 的 `#sparkles`） |
| tab / 选中态按钮 | `.btn.active`（`base.css:34`：`background:var(--accent); color:var(--text-on-accent); border-color:var(--accent)`） | `.cleaner-tabs .btn.active`(`:62-66`) | 视觉等价，唯一差异是 `border-color: transparent`（壳用 `--accent` 描边）——**同优先级下插件注入在后所以插件胜**，但只是为了改一根描边就整条重写 |
| 底部统计条 | `.sub-sidebar-footer`（`base.css:270-276`：`padding:10px 16px; font-size:12px; color:var(--text-secondary); border-top:1px solid var(--border)`） | `.cleaner-footer`(`:74-86`) + `.cleaner-selected`(`:82`) | 数值高度重合（10px 16px / border-top / `--text-secondary` 但字号 13 而非 12），却是横条 + 右对齐按钮组，**语义不同不能直接换**；真实缺口是「壳没有主区底部操作条」这一类 |
| 内容区滚动容器 | `.view-content`（`base.css:277-282`） | `.cleaner-content` 又写了一遍 `flex:1; min-height:0; overflow-y:auto; padding:16px`(`:50-55`) | 纯重复（值相同），风险是将来壳改 `.view-content` 的内边距时插件这一份会顶掉 |
| 灯箱 | `createLightbox`（契约 `plugin-guide.md` §4.3） | 调用了，但**优先用宿主的**：`parent.imageViewer.lightbox.show(...)` | 见 §5.5「交互」——这是刻意的宿主协作，不是重复实现 |
| 分页 | `createPagination`（`base.css:81-101`） | 「显示更多（还有 N 组）」按钮(`app.js:95-97/206-209`) | 行为更简单（只增不减、不显示页码）；与壳分页的视觉/语义都不同，属**未采用**而非重复 |
| 设置表单 | `createSettingsForm` + `openSettingsModal`（`base.css:181-206`）+ `HostChannel.requestSettings`（`base.js`，契约见 `plugin-ui-guide.md` §3.5） | 已接入（`app.js` 的 `openSettings`，工具栏 `#btn-settings`）；**优先请宿主 image-viewer 渲染**，宿主不表态时回落本地 `openSettingsModal` | 弹窗由宿主文档渲染才有整页遮罩：本页嵌在 `#extension-frame` 里，`.modal{position:fixed}` 只相对该 iframe。schema/values/保存仍全归本插件 |

### 5.5 交互约定

- **设置入口**：内嵌时挂在宿主扩展面板头部（`#btn-settings` 声明给 `HostChannel.mountToolbar`，宿主用它的样式渲染）→ `ImageCleaner.openSettings()`（`app.js`）→ `HostChannel.requestSettings('相册清理设置')`；脱离宿主时按钮留在本页工具栏。后端 `settings_schema` 声明 `threshold`（「相似判定阈值」，`range 0-16`，默认 8，`plugins/image-cleaner/backend/main.py:23-27`）。两条路径的差别只在**谁来画**：宿主 image-viewer `_serveHostChannel()` 表态后用宿主文档渲染（`HostChannel.serve` 的默认 `ui` 处理器），否则回落本页 `openSettingsModal`；保存一律由本页 `Bridge.call('save_settings')` 落盘（宿主替它存会写错插件）。**「重新扫描」与「设置」一起挂**（`#cleaner-actions` 整组收起）：只搬一个的话，用户看到的仍是"一半在顶栏、一半在下面"。
- **保存后的反馈方式**：无保存动作；唯一的「状态写回」是删除后本地过滤 `this.groups` 并重渲染（`app.js:233-244`），**不自动重扫**（`:231-232` 注释明说由用户点「重新扫描」）。
- **错误提示方式**：
  - 壳 `Toast.error`：`app.js:226`「部分删除失败: …」、`:246`「删除请求失败」
  - 壳 `Toast.warning`：`:218`「请先勾选要删除的图片」（**未选任何图时点删除**）
  - 结果区内联错误：`:83` `icon:triangle-alert` + 「扫描失败，请确认 image-viewer 已加载且相册目录可访问」，`:54` 扫描中改文案
  - `console.error(e)` 只记日志（`:82`）；`updateStatus()` 静默兜底（`:37-40`）
  - 注意 `:83` 的错误文案**只在 `runScan` 的 catch 里**；`get_cached_scan` 失败被 `try/catch` 吞掉后回退到真扫（`:63-71`），因此「image-viewer 未加载」时用户看到的是这条扫描失败文案
- **选择模型**：**复选框多选**（`input[type=checkbox][data-file]`，`app.js:108-112/154-160`），三种批量操作：
  - 单组「全选组」(`app.js:116-129`)：只勾该组、**不取消已勾的其它组**
  - 单组「只留一张」(`:131-133/166-183`)：保留 `group.files[0]`（**总是遍历顺序第一个**，无「保留最大/最新」策略）
  - 全局「每组只留一张」(`:185-196`)：改 `this.selected` 后 `render()` + `_syncCheckboxes()` 回写勾选态
  - **无右键菜单、无键盘快捷键、无框选/长按**（grep `keydown|keyup|contextmenu` 零命中）；`this.selected` 是内存态，切走（iframe 默认随路由卸载）即清空
- **右键菜单 / 键盘快捷键**：均**无**。
- **长任务进度与取消**：扫描是唯一长任务，**只有文本「扫描中…请稍候」(`app.js:54`)，无百分比、无取消按钮**；`#btn-rescan` 在扫描期间**不禁用**（`_bind()` 只挂 click，`:22`），连点会并发发请求。缩略图用 `loading="lazy"`(`:110`) 铺开，`onerror` 直接 `display:none` 隐藏破图。
- **空态/加载态/错误态的文案与样式类**：
  - 加载：`.cleaner-empty` +「扫描中…请稍候」（`app.js:54`）；工具栏 `#cleaner-root` 初值「读取中…」(`index.html:14`)、失败后固定「默认相册目录」(`app.js:38/35`)
  - 空态：`.cleaner-empty` + `icon:sparkles` + 「未发现完全重复图片 / 未发现相似图片」（`app.js:91`）
  - 错误态：`.cleaner-empty` + `icon:triangle-alert` + 「扫描失败，…」（`app.js:83`）；**没有独立的错误配色**（沿用空态的 `--text-secondary` 灰）
  - 计数：`#cleaner-scanned`「已扫描 N 张」(`:79`)、`#cleaner-selected`「已选 N 张」(`:212`)—— 每次重扫都重置为「已选 0 张」(`:56`)

### 5.6 特色设计（值得吸收）

1. **缩略图点击优先借用宿主灯箱，宿主缺失才用壳的 `createLightbox`**（`app.js:144-150`）：`parent.imageViewer.lightbox.show(items, index)`。解决：内嵌 companion 里点图能得到**带图片信息的全屏查看器**（宿主那套），而不是功能更弱的自建灯箱；同时对「脱离宿主单独打开」保持可用。
2. **扫描结果缓存 + 「非强制时先读缓存」**（`app.js:62-74`，后端 `get_cached_scan`）。解决：退出重进不必全量重扫（相册大时这一条决定体验），并把「重新扫描」按钮的语义变成显式强制刷新（`:22` 传 `true`）。
3. **删除后本地增量维护结果集，不触发重扫**（`app.js:231-244`：按 `result.deleted` 过滤、`files.length >= 2` 的组保留）。解决：删完立刻能看到剩余分组，且避免「重扫 → 又被判成重复」的循环。
4. **状态信息分三处低干扰展示而不是堆在标题里**：`.cleaner-scope`（作用域「全部相册」）、`.cleaner-root`（根目录，`max-width:240px` 省略 + 等宽字体，`image-cleaner.css:31-42`）、`.cleaner-scanned`（`margin-left:auto` 靠右，`:43-49`）。解决：内嵌进宿主时横向空间只有宿主面板那么宽，用胶囊/省略号而不是让路径撑破工具栏。
5. **`_escapeHtml` 先走内核 `Utils.escapeHtml` 再回退自实现，且 `null` 归一为空串**（`app.js:250-260`）。解决：属性插值（`data-file` / `title`）与文本插值共用同一条转义路径，`String(null)` 不再渲染出字面 `"null"`。
6. **勾选态与 `Set` 双向同步的两个入口分清楚**：`render()` 后逐个绑 `change`(`:154-160`)；批量修改后用 `_syncCheckboxes()` 统一回写(`:198-204`)。解决：批量操作（「每组只留一张」）不必重排 DOM 就与 `selected` 一致，重渲染与状态更新不会打架。

### 5.7 与 Shell 契约的偏差

**内嵌形态专项（本插件最需要注意的部分）**

| # | 偏差 | 证据 | 影响面 |
| --- | --- | --- | --- |
| A | **内嵌态是「宿主的扩展头 + 插件自己的工具栏」双层横条**（已收敛一半：两个操作按钮已挂到宿主头部的 `#extension-view-actions`，本页工具栏只剩作用域信息；宿主头已按插件工具栏的常态对齐字号/按钮高度 —— 标题 15px/700、按钮 13px/28px） | 宿主 `plugins/image-viewer/frontend/image-viewer.css`（`.extension-view-header` 与 `.extension-view-header .btn`）与 `.extension-view-body > iframe`；插件 `index.html:18` 的 `.view-toolbar`（`48px`、`border-bottom`） | 1 个内嵌视图 / 全部 cleaner 界面；内嵌态仍保留两条横条（宿主头 + 内嵌页降级后的操作行），但控件与字号已一致，`border-bottom` 只有内嵌页那条在 `html.is-embedded` 下被去掉 |
| B | **宿主侧本可使用壳的通用内嵌容器类，实际未用**：`renderExtensions` 在无 `onEmbed` 时会给 iframe 加 `.obx-embed-frame`（`base.js:283-286`，`border-radius:12px;background:var(--bg-surface)`），image-viewer 传了 `onEmbed`（`image-viewer/frontend/js/app.js:143`）因此走自己的 `#extension-frame` | `base.js:278-289`；`image-viewer/frontend/js/app.js:157-173`；`image-viewer/frontend/image-viewer.css:399-404` | 1 个宿主渲染点 / image-cleaner 的容器外观（无圆角、`--bg-app` 底 vs 壳的 12px 圆角 + `--bg-surface` 底） |
| C | **宿主分组的 `title` 参数成了死代码**：扩展带 `section` 时，渲染器用 `ext.section` 当分组标题（`base.js:254`），image-viewer 又传了同名的 `title: '相册清理'` | `image-cleaner/backend/main.py:85`、`image-viewer/frontend/js/app.js:142`、`base.js:243-259` | 1 处参数 / 无功能影响；但「分组标题的唯一来源」在契约里没有写清（`plugin-guide.md:169` 的签名只说 `options?`） |
| D | **跨 iframe 读宿主内部对象**：`parent.imageViewer.lightbox`（`app.js:144-147`）是约定外的耦合面——宿主前端没有把它导出成扩展 API | `app.js:144`；宿主 `image-viewer/frontend/js/app.js:1-280` 无 `window.imageViewer` 之外的分支判断；`plugin-guide.md` §2.1 只规定 `Bridge.callPlugin` 这类**后端**跨插件调用 | 1 处调用 / 全屏查看功能；宿主一旦把实例改名或改用 `onEmbed({lightbox})` 传参，这里静默退回自建灯箱（`app.js:148`），**失效无报错** |
| E | **`#app{height:100vh}` 而非壳其它插件统一的高度链** | `image-cleaner.css:1-5` 对比 `plugins/group-mesh/frontend/group-mesh.css:39-59`、`plugins/image-viewer/frontend/image-viewer.css` 的同类写法 | 1 处 / iframe 高度变化（宿主 `.extension-view-body` 是 `flex:1; min-height:0`，`image-viewer.css:395-398`）时 `100vh` 仍等于视口高，**看似等价**；但与「树内高度链」语义不同，宿主改成非满屏面板时会立刻出问题 |
| F | **主题隔离靠壳注入，插件零 `data-theme` 代码**（与 group-mesh 一致）；`--accent` 回退色与壳默认值不同 | `image-cleaner.css:63` `var(--accent, #4f6ef7)` vs `variables.css:28` `--accent:#0078d4` | 1 处 / 仅在壳变量缺失时可见；「外观设置」自定义强调色**能**跟随（壳写 `data-custom-colors`，`plugin-guide.md:823-830`） |

**通用偏差**

| # | 偏差 | 证据 | 影响面 |
| --- | --- | --- | --- |
| 1 | **后端有设置项、前端无入口**（已修：工具栏 `#btn-settings` → `openSettings()` → 优先 `HostChannel.requestSettings`，回落 `openSettingsModal`） | 后端 `plugins/image-cleaner/backend/main.py:23-27`（`threshold` range 0-16, 默认 8）；壳能力见 `base.css:181-206` 与 `base.js` 的 `HostChannel`；契约见 `plugin-ui-guide.md` §3.5 | 1 个设置项 / 相似图片判定精度可调；弹窗由宿主 image-viewer 的文档渲染（内嵌 iframe 里自绘没有整页遮罩） |
| 2 | **重复实现 `.view-content`**（同值覆盖，且带更窄的选择器） | `image-cleaner.css:50-55` vs `base.css:277-282` | 1 个容器 / 内容区内边距与滚动；壳改内边距时插件这份会顶掉 |
| 3 | **重写 `.btn.active` 只为改描边** | `image-cleaner.css:62-66` vs `base.css:34` | 2 个 tab / 选中态；统一按钮体系时这 5 行必须一起删，否则 tab 选中态与其它插件不一致 |
| 4 | **空状态已收敛到壳**（本条为审计时的差异，现已消除） | 壳 `.empty-state`（`base.css:428-431`）；插件原 `.cleaner-empty`(`:141-145`) 已删除 | 3 处文案改用壳结构（加载 `app.js:58-60`、空 `:102-108`、错误 `:90-94`）；错误态用 `.empty-state--error` + `#triangle-alert` 图标表达 |
| 5 | **无骨架屏、无真进度、无取消**：扫描期间 UI 只有一行文字，`#btn-rescan` 不禁用、不防并发 | `app.js:22`（只挂 click，无 disabled 逻辑）、`:54`、壳 `.obx-skeleton`(`effects.css:118-131`)未使用 | 1 个长任务 / 大相册扫描期间：用户可在扫描中反复点「重新扫描」，且没有任何「还要多久」的信号 |
| 6 | **分页能力未采用，改「显示更多」** | `app.js:7-8`（`pageSize=20`、`visibleCount=20`）、`:95-97/206-209` vs 壳 `.pagination-bar`(`base.css:81-101`)、`createPagination` | 1 处 / 结果集 >20 组时；`#cleaner-more` 按钮用 id 查询（`app.js:162`），重渲染后重复 id 由 `innerHTML` 整体替换保证唯一，但见下条 |
| 7 | **「显示更多」按钮每次重渲染都会重新绑定，且查询方式依赖 id** | `app.js:95-97`（拼 `id="cleaner-more"`）与 `:162-163`（`box.querySelector('#cleaner-more')`） | 1 个按钮 / 无功能影响；但这是「模板字符串 + id 查询」的组合，与壳组件（返回实例、自带 render）风格不同 |
| 8 | **未使用 `.obx-anim-*` / `.obx-card-lift` / `.obx-stagger`**，列表一次性 `innerHTML` 铺开 | `image-cleaner.css` 全文无 `obx-anim`；`app.js:99-114` 单次拼接 | 全部结果卡 / 无入场动画；effects.css（`effects.css:76-91`）提供的交错入场在这个插件里完全没被消费，分组多时会「一次性闪出」 |
| 9 | **`onerror` 内联脚本隐藏破图** | `app.js:110` `onerror="this.style.display='none'"` | 全部缩略图 / 加载失败时布局会塌一格（元素仍在，display:none）；壳无对应约定 |
| 10 | **`--bg-surface` / `--bg-hover` 的回退值写成了浅色专色** | `image-cleaner.css:88`（`#fff`）、`:24/38/117`（`rgba(128,128,128,.1)`/`.08`） | 3 处 / 变量缺失时深色主题下会出现白底卡片；正常壳内不触发 |
