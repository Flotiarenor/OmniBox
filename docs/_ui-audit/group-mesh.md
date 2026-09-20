# group-mesh 插件 UI 现状取证

> 只读审计。契约基准：`shell/frontend/public/shell/{variables.css, base.css, effects.css, base.js}`
> 与 `docs/plugin-guide.md` §4.1 / §4.3 / §4.4。所有结论附 `文件路径:行号`。

## 1. 概览

**前端文件清单**

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `plugins/group-mesh/frontend/index.html` | 338 | 唯一页面入口（`manifest.json:20` `frontend.entry`），5 个面板 + 6 个弹窗的静态骨架 |
| `plugins/group-mesh/frontend/group-mesh.css` | 905 | 全部插件样式（无第二份 CSS） |
| `plugins/group-mesh/frontend/js/app.js` | 603 | 状态读取、面板切换、身份/名单/共享项/节点/路线图渲染、弹窗开合、`Toast`/`confirmDialog` 封装 |
| `plugins/group-mesh/frontend/js/remote.js` | 755 | 「远端共享」分片：设备 → 共享项 → 目录 → 取回/上传，含上传轮询与生命周期绑定 |
| `plugins/group-mesh/frontend/network-location.html` | 290 | **第二个 HTML 页面**：被壳共享目录组件 FolderPicker 嵌进 iframe 的「网络位置」提供方（`network-location.html:8-17`） |

- **页面形态**：单页多视图。`#app` 下 5 个 `section.gm-panel`（`index.html:102/124/137/158/203`）靠 `data-active` 纯显隐切换（`group-mesh.css:281-287`、`app.js:116-140`），不销毁 DOM。
- **iframe 常驻**：`manifest.json:8` `"keepAlive": true`，因此 `remote.js:726-736` 必须靠壳的 `onShow`/`onHide` 停/起 15s 轮询（`remote.js:702` `VIEW_POLL_MS = 15000`），只把 `visibilitychange` 当壳缺失时的退路。
- **脚本顺序契约**：`index.html:335-336` `remote.js` 在 `app.js` 之前；`app.js:582-595` 用依赖注入把 `call/toast/escapeHtml/openModal/closeModal` 交给分片，两者装载期都不碰 DOM（`app.js:599-602`）。
- **是否用 Shell 布局类**：是，且是本仓库最完整的一个——`.view-sub-sidebar`、`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.obx-nav-item`、`.modal/.modal-box/.modal-body/.modal-footer`、`.btn/.btn-sm/.btn-primary/.btn-danger`、`.obx-glass`、`.obx-scroll`、`.obx-card-lift`、`.obx-stagger`、`.obx-anim-scale`、`.obx-skeleton`、`.obx-ease` 全部来自壳。
- **壳注入顺序（影响后面所有「谁赢」的判断）**：`shell/backend/file_server.py:797-798` 把 `variables.css / base.css / effects.css / base.js / motion.js` 注入到 `</head>` **之前**，所以插件自己的 `<link href="group-mesh.css">`（`index.html:7`）实际排在壳样式**之后**，插件同优先级规则天然胜出。

## 2. 布局骨架

顶层容器结构树（`index.html:22-222`）：

- `div#app`（`group-mesh.css:53-59`）
  - `aside.gm-side.view-sub-sidebar.obx-glass`（`index.html:30`）
    - `div.gm-brand` → `.gm-brand-icon` / `.gm-brand-text`（`index.html:31-37`）
    - `nav.gm-nav.obx-scroll#gm-nav`（`index.html:39`）：4 个 `.gm-nav-label` 分组标题 + 5 个 `button.obx-nav-item.gm-nav-item[data-panel]`（`index.html:41/46/49/55/61`），其中两项带 `.gm-nav-count` 角标（`index.html:51/57`）
    - `div.gm-side-foot.sub-sidebar-footer#gm-side-summary`（`index.html:66`）：`.gm-foot-dot[data-state]` + `.gm-foot-text`（`app.js:395-426` 写）
  - `div.view-body.gm-main`（`index.html:72`）
    - `div.view-toolbar.gm-toolbar`（`index.html:73`）：`.toolbar-group.gm-heading`（图标 + `#gm-panel-title` 15px/700 + `#gm-panel-sub` 11px）+ `.toolbar-group.gm-toolbar-right`（`margin-left:auto`，`group-mesh.css:100-102`）内两个 `.btn.btn-sm`
    - `div#kernel-missing.gm-banner.gm-banner-error[hidden]`（`index.html:90`，`app.js:148` 控制）
    - `div.view-content.gm-content.obx-scroll#gm-content`（`index.html:96`）
      - `div.gm-panels.obx-stagger#gm-panels`（`index.html:97`）
        - `section.gm-panel[data-panel][data-title][data-sub]` ×5
          - `div.gm-grid`（两列，`group-mesh.css:290-294`）→ `section.gm-card.obx-card-lift` ×2（仅 machine 面板）/ 单卡 ×1（其余面板）
  - `div.modal` ×6（`index.html:228/242/256/275/291/316`）挂在 `body` 下，与 `#app` 平级

**尺寸与滚动来源**

| 部位 | 数值 | 来源 |
| --- | --- | --- |
| 侧栏宽度 | `236px` | 插件自定义 `--gm-side-width`（`group-mesh.css:35`）→ `.gm-side.view-sub-sidebar{width:var(--gm-side-width)}`（`:141-145`），覆盖壳的 `240px`（`base.css:251-259`） |
| 侧栏方向 | `flex-direction: row`（≤720px 时折成横向导航） | 插件（`group-mesh.css:840-850`） |
| 工具栏高度 | `48px` | 壳 `.view-toolbar`（`base.css:239-248`），插件只改 `gap`（`group-mesh.css:67-69` `gap:12px`） |
| 主内容滚动 | `.view-content{overflow-y:auto}` | 壳（`base.css:277-282`）；插件补 `padding:16px / background:transparent`（`group-mesh.css:271-277`） |
| 高度链 | `html,body{height:100%}` + `#app{display:flex;height:100%;overflow:hidden}` | **插件自定义**（`group-mesh.css:47-59`），因为壳 `base.css:6-11` 只给 `body` 设了 `overflow:hidden` |
| 卡片栅格 | `repeat(2,minmax(0,1fr))`、`gap:16px`（≤1040px 单列） | 插件（`group-mesh.css:290-294`、`:901-905`） |
| 卡圆角 / 内边距 | `--gm-radius`=12px、`padding:16px` | 插件（`group-mesh.css:296-305`） |
| 远端两栏 | 设备栏 `272px` + 文件栏 `flex:1`，各自 `overflow-y:auto` | 插件（`--gm-peers-width`，`group-mesh.css:36/633-645`） |
| 弹窗宽度 | `480 / 560 / 560 / 520 / 560 / 520 px` 内联 | 插件（`index.html:229/243/257/276/292/317`），壳 `.modal-box` 默认 `400px`（`base.css:165-169`）被内联宽度覆盖 |

`network-location.html` 是**独立文档**，不复用任何布局类：`body{padding:14px 16px}`（`:25`）+ 三步 `.nl-step`（`:64-79`），列表 `.nl-list{max-height:150px;overflow-y:auto}`（`:31`）——**没有加 `.obx-scroll`**。

## 3. 设计 token 使用

**实际用到的壳变量（`group-mesh.css` 内计数，112 处 `var(--…)`）**

- 背景：`--bg-app`(58/535/574/740/765/782) `--bg-surface`(301/383/818) `--bg-hover`(122/229/449/705)
- 文本：`--text-primary`(88/115/171/321/334/384/506/595/697/769/784/797/819) `--text-secondary`(94/177/230/245/346/454/486/500/527/543/587/655/742) `--text-muted`(199/253/260/403/680/747) `--text-on-accent`(710)
- 边框/强调/语义：`--border`(152/297/381/440/533/549/572/636/739/764/783/849/888) `--accent`(31/395/477/709/804) `--success`(32/257/308/478/479/726/727/817) `--warning`(33/258/309/476) `--danger`(34/113/259/310/773/774/828) `--danger-soft`(34)
- 尺寸/动效：`--radius`(30) `--radius-lg`(29) `--radius-sm`(121/382) `--transition-fast`(387/701) `--shadow-md`(820)
- effects.css / motion.js：`--obx-ease`(303/444/824) `--obx-i`(304/445；`app.js:236/296/389` 内联写入) `--obx-shadow-2`(820)

**插件自定义变量**：`:root` 下 8 个（`group-mesh.css:28-37`）`--gm-radius`、`--gm-radius-sm`、`--gm-accent-soft`、`--gm-success-soft`、`--gm-warn-soft`、`--gm-danger-soft`、`--gm-side-width`、`--gm-peers-width`。其中 `--gm-success-soft`(32) 与 `--gm-warn-soft`(33) 定义后**零引用**（死 token）。插件**没有**覆盖 `:root` 里任何一个壳 token，只做「壳 token → `--gm-*` 派生」。`.gm-card-wide`(`:313-315`) 同样零引用。

**硬编码颜色字面量**

- `group-mesh.css` 的正则 `#[0-9a-fA-F]{3,8}\b|rgba?\(` 命中 **3 行**，其中 2 行在注释里（`:16` 记录旧版写死的 `#f59e0b / #8b5cf6`、`:463` 同一记录），**唯一的生效字面量**是 `:820` `box-shadow: var(--shadow-md, var(--obx-shadow-2, 0 10px 30px rgba(0,0,0,0.2)));` —— 三级回退链的最末兜底。
- 对比 `network-location.html`：同一正则命中 **15 行**，全是「变量 + 字面量回退」写法，典型 4 条：`:27` `var(--bg, #17181c)`、`:38` `var(--accent, #4c8dff)`、`:60` `color: #fff`（`.btn-primary` 的纯字面量）、`:214` `'var(--danger, #e5484d)'`。根因是 `:23` `:root { color-scheme: light dark; }` 明确按「可能脱离壳单独打开」设计。

**data-theme 处理**：三个文件全部**没有** `data-theme` 代码（grep `data-theme|color-scheme` 只命中 `network-location.html:23` 的 `color-scheme`）。深色完全靠壳注入脚本（`file_server.py:71-76` 同步 `data-theme`）+ token 生效；`network-location.html:24-28` 甚至给 body 写了「深色优先」的回退底色。

## 4. 组件与命名约定

**前缀**：`gm-`（group-mesh）。按类别清单：

- 按钮：**无自有按钮类**，全靠壳 `.btn/.btn-sm/.btn-primary/.btn-danger`；只覆写一个尺寸 `.gm-table .btn-danger{padding:3px 10px;font-size:12px}`（`:559-562`）
- 卡片：`.gm-card`(`:296`)、状态边 `.gm-card-ok/warn/error/muted`(`:308-311`)、`.gm-card-wide`(`:313`，未用)
- 弹窗：**零自有弹窗类**，用壳 `.modal`（`index.html:228` 等 6 处），仅给 `.modal-body input/textarea` 加尺寸（`:414-426`）
- 菜单/分页/树/灯箱/卡片网格：`createContextMenu` / `createPagination` / `createTree` / `createLightbox` / `createCardGrid` 一个都没调用；表格是手写 `<table class="gm-table">`（`app.js:258/306`、`remote.js:178`）
- 进度：`.gm-remote-progress`(`:760`)、错误态 `.gm-remote-progress-error`(`:772`)、弹窗内 `.gm-upload-progress`(`:778`)——**纯文本进度，无 `<progress>`/百分比条**，百分比只出现在文案里（`remote.js:527-528`）
- toast：`.gm-toast` / `.gm-toast-error`(`:811-829`)，只在壳 `Toast` 缺失时启用（`app.js:29-40`）
- 空状态：`.gm-empty`(`:495-508`)，shell 的 `.empty-state` 未使用
- 骨架屏：`.gm-skeleton-line`(`:511-520`) + 壳 `.obx-skeleton`（`app.js:433-434`）
- 徽标：`.gm-badge` + `-owner/-admin/-member/-ok/-warn`(`:466-480`)、`.gm-remote-badge`(`:721`)、`.gm-nav-count`(`:225`)、`.gm-foot-dot[data-state]`(`:249-260`)
- 其他：`.gm-kv`(`:339` 定义列表)、`.gm-hint`(`:484`)、`.gm-details`(`:532`)、`.gm-list`(`:522`)、`.gm-check`(`:789`)、`.gm-address*`(`:566-607`)、`.gm-remote-*`(`:611-775`)、`.gm-banner-error`(`:112`)、`.gm-brand*`(`:147-179`)、`.gm-nav*`(`:181-233`)

**「Shell 已提供但插件又实现一遍」的能力**

| 能力 | Shell 提供 | 插件实现 | 差异 |
| --- | --- | --- | --- |
| Toast | `.toast*`（`base.css:208-234`）+ `Toast.*` | `.gm-toast`(`:811-829`) | 仅作脱离壳的兜底（`app.js:31-39`）。位置相反：壳 `top:16px;right:16px`，插件 `right:20px;bottom:20px`；插件用 `border:1px solid var(--success)` 表语义，壳用 `.toast-success::before{content:'✓ '}` 前缀 |
| 空状态 | `.empty-state`（`base.css:428-431`，`min-height:300px` 居中 16px） | `.gm-empty`(`:495-503`，`13px` 左对齐多行 + `<strong>`) | 语义化更强（可带标题+说明），但视觉与壳不一处 |
| 骨架屏 | `.obx-skeleton`（`effects.css:118-131`） | `.gm-skeleton-line`(`:511-520`) 再叠一层 | 壳只管配色与 shimmer，插件补 `height:12px` / `margin:6px 0` / 圆角 6px；即壳的骨架尺寸仍需插件给 |
| 表单控件 | `.field input/select/textarea`（`base.css:187-206`，含 `:focus` 强调色、`min-height:120px` textarea） | `.gm-form/.gm-address/.modal-body` 三组选择器(`:375-426`) | 视觉接近（6px 10px、`--radius-sm`），但选择器是**元素级**且覆盖 `.modal-body`（见 §7）；textarea 无 `min-height`，只 `resize:vertical` |
| 表格 / 卡片 / 徽标 / 定义列表 | **壳无对应类** | `.gm-table`(`:430-460`)、`.gm-card`(`:296-322`)、`.gm-badge`(`:466`)、`.gm-kv`(`:339`) | 真实缺口，不是重复实现 |
| 弹窗 | `.modal` 全家桶 | 无自有弹窗类（`:20-23` 注释记录曾自绘 `.gm-modal` 已删除） | **已对齐**，是本仓库的迁移范例 |

## 5. 交互约定

- **设置入口**：工具栏 `#btn-settings`（`index.html:83`）→ `openSettingsModal({ title: '团体组网设置' })`（`app.js:486-492`），**不传 `schema`/`values`/`onSave`**，完全依赖壳去 `Bridge.call('get_settings_schema'|'get_settings')`（`base.js:572-577`）；缺失壳时只弹一条错误 toast（`app.js:488`）。保存反馈由壳给：`Toast.success('设置已保存')` + 400ms 后 `location.href = …?_t=` 整页重载（`base.js:621-622`）。
- **错误提示**：统一 `window.Toast.error`（`app.js:29-40`、`remote.js:43`）；两类「非 toast」错误——首屏 `get_status` 失败写侧栏 `#gm-foot-text = '读取状态失败'` + `data-state="error"`（`app.js:465-471`），内核缺失走常驻横幅 `.gm-banner-error`（`app.js:148`）。弹窗内错误留在弹窗里（`remote.js:612`）。
- **选择模型**：**单选**（远端设备/共享项点击 `remote.js:105-109`；目录项「进入/取回」逐行按钮 `remote.js:159-168`）。**无多选、无框选、无长按、无拖拽**，`app.js:504` 的 `read: 'group'` 是硬编码 ACL。
- **右键菜单**：**无**（无 `contextmenu` 监听，未调用 `createContextMenu`）。
- **键盘快捷键**：**无**（全前端 grep `keydown|keyup` 零命中）；只有程序化 `focus()`（`app.js:227/228`、`remote.js:479/659`）。
- **长任务进度与取消**：文本行 `.gm-remote-progress`，六种状态全覆盖——「正在物化 X…」(`remote.js:389`)、「正在读取对端目录…」(`:410`)、「正在取回 X…」(`:432`)、上传百分比「续传中/正在上传 1.2 MB / 8.0 MB（15%）」(`:523-529`)、「已取消，已传 …（再点「上传」会从这里续传）」(`:570-571`)、「上传被中断，已传 …」(`:574-580`)。轮询间隔 `300ms`(`:584`)；传输中 `#btn-do-upload` 置灰并改名「上传中…」、`#btn-close-upload` 锁死、`#btn-cancel-upload` 显形（`remote.js:497-513` ← `index.html:310`），取消后文案变「正在取消…」(`:630`)。
- **空态/加载态/错误态文案与类**：
  - 加载：内容区骨架两行 `.gm-skeleton-line.obx-skeleton`（`app.js:433-434`）+ 侧栏「正在读取状态…」（`index.html:68`）；远端「正在读取设备…」（`remote.js:52/304`）
  - 空态：`<p class="gm-empty">`，文案如「本机还没有身份。」（`app.js:179`）、「本机还没有团体名单。」（`:218`）、「**本机还没有共享项。**」(`:283`)、「还没有可用的对端设备。」(`remote.js:57`)、「这个目录是空的。」(`:154`)
  - 错误态：`.gm-badge.gm-badge-warn` 内联在提示里（`app.js:339/366`）、`.gm-remote-progress-error`(`remote.js:393/437`)、`.gm-banner-error`(`app.js:148`)、`network-location.html` 用 `.nl-empty` 写「读取设备失败：…」(`:127`)

## 6. 特色设计（值得吸收）

1. **面板标题/副标题只写在 HTML 的 `data-title`/`data-sub` 上，工具栏标题是渲染产物**（`index.html:102-104` → `app.js:131-137`）。解决：新增面板不再需要在 JS 里再维护一份标题表，两处文案永不漂移。
2. **卡片顶边 2px 表达后端状态，而不是让用户逐行读文字**（`group-mesh.css:296-311`；`app.js:191/249/351-356`）。解决：`身份/名单/节点` 三张卡「一眼看健康度」；`gm-card-warn` 还刻意区分「名单过期只是提示，不阻断通信」（`app.js:248`）。
3. **文本进度区把结论留下（含落地路径、字节数、断点位置），而不是进度条走完就消失**（`remote.js:397-401/446-447/570-571`）。解决：跨进程/跨弹窗的长任务失败后用户仍能知道「传到哪了、能不能续传」。
4. **上传状态机把 `interrupted` / `cancelled` 当一等状态并明说「再点上传会续传」**（`remote.js:482-492/566-581`；后端任务表见 `index.html:302-306`）。解决：插件重载/进程被杀后用户不会误以为白传了。
5. **侧栏底部常驻「节点运行 + 是否已发布 + 团体/名单版本」三件事**，并用 `data-state` 圆点编码 OK/warn/error/idle（`group-mesh.css:249-260`、`app.js:395-426`）。解决：本插件的核心问题「我到底能不能被别人连上」有了唯一常驻答案。
6. **壳缺失时的降级是显式设计的**：`Toast` 缺失走 `.gm-toast`（`app.js:31-39`）、`confirmDialog` 缺失走原生 `confirm`（`app.js:315-318`）、`Bridge` 缺失返回带说明的 reject（`app.js:44-46`）、`Motion` 缺失安静跳过（`app.js:58-62`）。解决：页面可脱离壳直接打开调试，且降级路径不静默失败（`network-location.html:104-107` 的 `postMessage` try/catch 同思路）。
7. **`network-location.html` 对「输入框回写」的取舍被写成注释留档**（`:192-203`）：`input` 事件里只同步 `state`、**绝不回写输入框**，只在 `blur` 时回写规范化结果（`:281-284`），并在 `run()` 里以输入框当前值为准（`:236`）。解决：逐字符输入时反斜杠被规范化吃掉导致 `C:UsersADMINI~1...` 的难查 bug。

## 7. 与 Shell 契约的偏差

| # | 偏差 | 证据 | 影响面 |
| --- | --- | --- | --- |
| 1 | **侧栏宽度必须靠提高优先级才能覆盖壳**：壳用 `.view-sub-sidebar`(0,1,0) 写死 `--sub-sidebar-width`(240px)，插件改 236px 只能写成 `.gm-side.view-sub-sidebar`(0,2,0) | `group-mesh.css:135-145`（注释自陈「同优先级时后注入的壳样式胜出…实测：设成 236 实际 240」）；壳侧 `base.css:251-259` | 1 处规则 / 侧栏整条布局；窄窗口 ≤720px 的横向折叠同规则内（`:840-850`）。任何「统一侧栏宽度」的动作会立刻把这里的选择器策略打回 240px |
| 2 | **元素级表单样式覆盖壳的弹窗正文**：`.modal-body input, .modal-body textarea`（两组共 10 个选择器）不受插件前缀约束 | `group-mesh.css:375-404/414-426`；壳侧 `.field input…`(`base.css:187-200`)、`.modal-body`(`base.css:174`) | 6 个弹窗的全部输入框 / 1 处宽选择器；将来壳给 `.modal-body input` 加统一类时会被这 13px/6px 10px 的规则顶掉（同优先级下插件在壳之后注入，插件赢，见 `file_server.py:797-798`） |
| 3 | **`[hidden]` 不可用，改用内联 `style.display`**（作者样式的 `display` 会盖掉 UA 给 `[hidden]` 的 `display:none`） | `remote.js:501-502` 与 `:508/519`；`index.html:301/310` 初始就是 `style="display:none"` | 上传弹窗 3 个按钮 / 1 处历史事故（`docs/group-mesh-implementation-path.md:463-472`）。统一 UI 若给 `.btn` 加 `display` 声明，会重演同类问题 |
| 4 | **高度链仍需插件自己给全**：壳只给 `body{overflow:hidden}`，`html,body{height:100%}` 由插件写 | `group-mesh.css:39-59`；壳侧 `base.css:6-11` | 1 个插件 / 全部内容的滚动；若统一布局把 `#app` 换成壳提供的容器类，`html,body` 这两行必须一起搬走，否则 `.view-content` 的 `overflow-y` 静默失效（内容被裁） |
| 5 | **响应式断点顺序与常规相反**：`@media (max-width:720px)` 写在 `@media (max-width:1040px)` **之前**，两条都改 `.gm-grid`，靠源码顺序让宽的那条兜底 | `group-mesh.css:834` 与 `:901`（`:898-900` 注释自陈「顺序不能对调」） | 2 条媒体查询 / 卡片栅格与远端两栏；统一断点体系时极易被格式化或合并工具重排而反向生效 |
| 6 | **自有 toast / 空态 / 骨架三件套与壳并存** | `.gm-toast`(`:811-829`)、`.gm-empty`(`:495`)、`.gm-skeleton-line`(`:511`) vs 壳 `.toast*`/`.empty-state`/`.obx-skeleton` | 3 组类 / 全站空态与提示观感：同一屏里「壳的 toast 在右上、插件的 toast 在右下」是可见差异（`base.css:209-213` vs `:811-815`） |
| 7 | **`--gm-success-soft` / `--gm-warn-soft` / `.gm-card-wide` 定义未用** | `group-mesh.css:32-33`、`:313-315`（grep `--gm-success-soft` 仅命中定义行） | 3 个符号 / 无功能影响；但会让「统一 token 表」误以为它们仍在被消费 |
| 8 | **`network-location.html` 完全绕开壳的类体系**：内联 `<style>` 里重定义 `.btn`/`.btn-primary`，并把 `.view-*` 布局类一并弃用 | `network-location.html:54-60`（`.btn{padding:4px 10px;font-size:12px;border-radius:6px}`）；注入点在 `</head>` 前（`file_server.py:798`）意味着**壳的 `.btn`(`base.css:14-24`, 6px 14px/13px/4px) 在本页会覆盖插件这份**，实际渲染是壳的尺寸 | 1 个页面 / 三步选择器全部按钮；该页是「网络位置」提供方契约（`docs/plugin-guide.md` §7.2.1）的实现，改壳的 `.btn` 会**直接改变这个页面的按钮尺寸**，而页面作者以为自己在用自定义样式 |
| 9 | **进度只用文本，不消费壳的任何进度组件** | `remote.js:214-225` 只写 `textContent`；百分比仅文案（`:527-528`） | 4 类长任务（物化/列目录/取回/上传）；统一进度组件时必须同时改前端 4 条链路的后端返回（`result.fetched/unchanged/error_count/truncated`，`network-location.html:260-263`） |
| 10 | **设置弹窗依赖壳的「保存后整页重载」隐式行为** | `app.js:491` 不传 `onSave`；壳 `base.js:621-622` 重载带 `?_t=` | 1 个入口；若统一 UI 去掉重载语义，group-mesh 的设置项（后端 `get_settings_schema`）将不会在界面上生效 |
| 11 | **`visibilitychange` 兜底与壳生命周期并存** | `remote.js:726-736`：有 `onShow/onHide` 用壳的，否则监听 `document.visibilitychange` | 1 处 / 15s 轮询；`keepAlive` 下 `document.hidden` 恒 false（`:720-724` 注释），统一生命周期时这段兜底是必须一起收编的死角 |
