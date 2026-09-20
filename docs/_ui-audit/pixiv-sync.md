# pixiv-sync 插件 UI 现状取证

> 只读审计。契约基准：`shell/frontend/public/shell/{variables.css, base.css, effects.css, base.js}`
> 与 `docs/plugin-guide.md` §4.1 / §4.3 / §4.4。所有结论附 `文件路径:行号`。

## 1. 概览

**前端文件清单**

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `plugins/pixiv-sync/frontend/index.html` | 461（24716 B） | 唯一文件：`<style>` 1 块（`index.html:7-60`，54 行）+ 内联 `<script>` 1 块（`index.html:198-459`，262 行）+ 静态骨架 |
| `plugins/pixiv-sync/frontend/*.css` | — | **不存在**：全部样式写在 `index.html:7-60` 的单个 `<style>` 里 |
| `plugins/pixiv-sync/frontend/**/*.js` | — | **不存在**：全部逻辑内联在 `index.html:198-459`，1 个 IIFE，无模块拆分 |

- **页面形态**：单页单视图。页面只有一个「状态栏 + 按钮组 + 进度条 + 设置面板」的控制面板，没有视图切换、没有作品列表、没有分页（`index.html:63-172` 是全部结构）。
- **嵌入形态（二级 iframe）**：插件通过 `get_extensions()` 的 `embedUrl` 挂到 image-viewer 侧栏（`plugins/pixiv-sync/backend/main.py:736-750`，`embedUrl` 在 `:746`，`host: image-viewer` 在 `:740`）；宿主把 `ext.embedUrl` 塞进自己的 `#extension-frame`（`plugins/image-viewer/frontend/js/app.js:157-165`），该 iframe 满尺寸（`plugins/image-viewer/frontend/image-viewer.css:395-404`，`.extension-view-body{flex:1;min-height:0}` + iframe `width/height:100%`），关闭时 `frame.src='about:blank'` 销毁文档（`image-viewer/frontend/js/app.js:168-173`）。
- **manifest 事实**：`plugins/pixiv-sync/manifest.json:27` `"hidden": true`、`:23-26` `frontend.entry/route`、**未声明 `keepAlive`**（无该键）。
- **Shell 资源注入**：壳把 `variables.css + base.css + folder-picker.css + effects.css + base.js + folder-picker.js + motion.js` 注入到 `</head>` 之前（`shell/backend/file_server.py:59-66`、`:771-798`）。因此本插件的 `<style>`（7-60）**排在壳样式之前**，同优先级规则壳胜；插件要覆盖只能靠更高的选择器特异性（它确实这么做了，见 §2）。
- **是否使用 Shell 布局类**：用了 `.view-body`（63）、`.view-toolbar`（66、73）、`.toolbar-group`（67、70）、`.view-content`（75）、`.obx-scroll`（75）、`.btn/.btn-sm/.btn-danger`（71、92-102、160-162、184、192-193）；未用 `.view-sub-sidebar` / `.sub-sidebar-header` / `.sub-sidebar-footer` / `.obx-nav-item` / `.modal` / `.toast` / `.empty-state` / `.pagination-bar` / `.settings-form`。

## 2. 布局骨架

顶层结构树（`index.html:62-196`）：

- `div#app.view-body.psync-main`（`index.html:63`；`.psync-main` 见 `index.html:10`）
  - `div.view-toolbar`（`index.html:66`，来自 `base.css:239-248`）
    - `div.toolbar-group`（`:67`）→ `span.psync-title[style=font-weight:600]`（`:68`，内联）
    - `div.toolbar-group[style=margin-left:auto]`（`:70`，内联）→ `button#btn-cancel.btn.btn-sm[style=display:none]`（`:71`，内联）
  - `div.view-content.obx-scroll`（`index.html:75`，来自 `base.css:277-282`）
    - `div.psync-status` ×2（`:77`、`:83`）→ 状态点 `.dot` ×1 + 文本 span ×N（`:78-87`）
    - `div.psync-btns` ×3（`:91`、`:95`、`:99`）→ 同步/刷新/校验按钮共 7 个（`:92-102`）
    - `div.psync-progress#progress-box[style=display:none]`（`:106`）→ `div.psync-bar > div#bar`（`:107`）+ `div.meta`（`:108-111`）
    - `div.psync-current#p-current`（`:113`）
    - `div.psync-settings`（`:116`）→ `h4` + 8 个 `label>input`（`:118-155`）+ `.psync-options`（`:128-156`）+ `.save-row`（`:157-164`）
    - `div.psync-tip` ×2（`:166`、`:170`）
- `div#oauth-modal.psync-modal > div.box`（`index.html:175-196`）与 `#app` **平级**，直接挂在 `body` 下

**尺寸与滚动来源**

| 部位 | 数值 | 来源 |
| --- | --- | --- |
| 高度链（二级 iframe 必需） | `html, body { height: 100%; margin: 0 }` | 插件自定义（`index.html:9`），注释在 `:8` 写明「iframe 内 html/body 必须有高度，滚动才生效」 |
| 主容器 | `display:flex; flex-direction:column; height:100%; min-height:0` | 插件自定义 `.psync-main`（`index.html:10`）**补足** `base.css:237` 的 `.view-body`（壳版没有 `height`） |
| 工具栏高度 | `var(--toolbar-height, 48px)` | `base.css:240`，插件未覆盖 |
| 内容区滚动 | `flex:1 1 auto; min-height:0; overflow-y:auto` | 插件自定义 `.psync-main .view-content`（`index.html:11`）**重写** `base.css:277-282`（壳版是 `flex:1`、无 `min-height`）；`padding:16px` 沿用壳 |
| 内容区滚动条 | `.obx-scroll` | `effects.css:140-183`（悬停渐显 6px 窄条）——本插件唯一使用的壳动效/工具类 |
| 导航侧栏宽度 | 无侧栏 | 未使用 `--sub-sidebar-width` / `--nav-width` |
| 状态栏 | `padding:8px 16px; font-size:12px; gap:12px; flex-wrap:wrap` | 插件自定义 `.psync-status`（`index.html:12-13`） |
| 状态点 | `width/height:8px; border-radius:50%` | 插件自定义 `.psync-status .dot`（`index.html:14`） |
| 按钮区 | `gap:10px; padding:10px 16px`，`.btn{flex:1}` | 插件自定义 `.psync-btns`（`index.html:18-19`）——把壳的 `.btn` 拉成等宽（`base.css:14-24` 无 `flex`） |
| 进度条 | 槽 `height:10px; border-radius:5px`；填充 `linear-gradient(90deg,#4caf50,#8bc34a)`；`transition:width .3s ease` | 插件自定义 `.psync-bar`（`index.html:21-24`） |
| 设置卡 | `margin:8px 16px; padding:12px; border:1px solid …; border-radius:8px; gap:10px` | 插件自定义 `.psync-settings`（`index.html:29-30`） |
| 选项组 | `border:1px dashed …; border-radius:6px; padding:10px; gap:8px` | 插件自定义 `.psync-options`（`index.html:38-39`） |
| 弹窗 | `width:min(560px,92vw); max-height:86vh; border-radius:10px; padding:16px 18px` | 插件自定义 `.psync-modal .box`（`index.html:47-48`） |
| 弹窗 textarea | `min-height:120px; font-size:11px; font-family:Consolas,monospace` | 插件自定义（`index.html:53`）；`min-height:120px` 与壳 `.field textarea`（`base.css:201`）数值相同但各写一份 |

## 3. 设计 token 使用

- **实际用到的 `--*` 变量（全部清单）**：只有 4 个名字，共 27 处（执行正则 `var\((--[a-z0-9-]+)` 后按名字计数）：
  `--color-text` ×17、`--color-border` ×6、`--color-bg-input` ×3、`--color-bg` ×1。
- **这 4 个名字在 `variables.css` 里一个都不存在**（`variables.css:4-52` 浅色、`:55-86` 深色，只有 `--bg-* / --text-* / --border / --accent* / --danger* / --success / --warning / --nav-width / --sub-sidebar-width / --toolbar-height / --radius* / --shadow-* / --transition-*`）。同一正则的结果里**没有任何一个真实 token**：本插件对 Shell 主题 token 的使用次数 = **0**。
- 后果：每个 `var()` 都取 fallback，浅/深两套 token 对它完全无效。深色主题下正文字仍是 `#666`/`#999`（`index.html:13/26/27/32/41/42`），压在 `--bg-app` 深色底（`variables.css:56` `#0d1117`）上。
- **覆盖 `:root` / 自有 `--xxx`**：0 处（grep `:root` 与 `^\s*--[a-z-]+\s*:` 均无命中）。它的「默认值」全部写在 `var()` 的第二个参数里，等于给每个位置内联了一份字面色。
- **`data-theme`**：插件 0 处处理（grep `data-theme` = 0）。主题同步完全由壳注入的脚本完成——读父窗口 `documentElement` 的 `data-theme` 写到本 iframe `<html>`，并用 MutationObserver 跟随（`shell/backend/file_server.py:69-76`）。属性同步是好的，但 token 名错，同步了没有效果。
- **硬编码颜色字面量**（在 `index.html` 上执行）：
  - 正则 `#[0-9a-fA-F]{3,8}\b` → **34 处 / 13 个不同值**：`#999`×9、`#333`×4、`#ddd`×4、`#666`×3、`#fff`×3、`#4caf50`×2、`#d33`×2、`#e0e0e0`×2、`#888`、`#8bc34a`、`#9e9e9e`、`#f44336`、`#fafafa`。
  - 正则 `rgba?\(` → **2 处**：`rgba(0,0,0,.45)`（`:45` 弹窗遮罩）、`rgba(0,0,0,.25)`（`:49` 弹窗阴影）。
  - 内联 `style="` → **18 处**（行号 68,70,71,83,95,99,106,130,138,142,146,150,154,157,158,159,170,178），其中 12 处是纯排版（`padding-top:0` / `font-size:11px` / `display:flex;gap:8px`），5 处重复写 `color:var(--color-text,#999)`。
  - 最典型的 5 例：`:13` `color: var(--color-text, #666)`（状态栏）；`:15-17` `.dot.ok{#4caf50}` / `.bad{#f44336}` / `.idle{#9e9e9e}`；`:24` 进度填充 `linear-gradient(90deg,#4caf50,#8bc34a)`（固定绿，与 `--accent`/`--success` 无关）；`:45` 遮罩 `rgba(0,0,0,.45)`（浅色 `--bg-overlay` 的同值，深色下仍是 0.45 黑）；`:170` 内联 `color:#d33`（限流冷却提示，未用 `--danger`）。

## 4. 组件与命名约定

- **自有类名前缀**：`psync-`（12 个类，定义行见下），另有 3 个无前缀短名 `.dot`（`:14`）、`.ok/.bad/.idle`（`:15-17`）、`.box/.row`（`:47`、`:59`，弹窗内部，与壳 `.modal-box` 不同名但语义重叠）。
- **清单（类名 → 定义行 → 用途）**：
  - `.psync-main` `:10` 主容器；`.psync-status` `:12` 状态栏；`.psync-btns` `:18` 按钮行；`.psync-progress` `:20` 进度区；`.psync-bar` `:21` 进度槽/填充；`.psync-current` `:27` 当前处理项；`.psync-settings` `:29` 设置卡；`.psync-options` `:38` 选项组；`.psync-tip` `:41` 提示文案；`.psync-empty` `:42` 空态（**死类**，见下）；`.psync-modal` `:45` 弹窗。
  - 按钮：不自建，直接用壳的 `.btn` / `.btn-sm` / `.btn-danger`（`:71`、`:92-102`、`:160-162`、`:184`、`:192-193`）——**这点合规**。
- **Shell 已提供但插件又自己实现了一遍**：

| # | 能力 | Shell 提供 | 插件实现 | 差异 |
| --- | --- | --- | --- | --- |
| 1 | 设置表单 | `.settings-form/.field/.field-label/.field-help`（`base.css:181-206`）+ `createSettingsForm`（`base.js:419-563`）+ `openSettingsModal`（`base.js:566-628`） | `index.html:116-165` 静态 HTML + `loadSettings/saveSettings`（`index.html:324-369`） | 壳版按插件声明的 `settings_schema` 渲染（本插件已声明 8 项，见 `backend/main.py:38-114`）；插件版手写 label>input、无 `.field-help`、无 `required` 标记、数字范围只靠 `min/max` 属性 + `intSetting()` 夹取（`:339-345`）；壳版保存成功后会强制重载页面（`base.js:622`），插件版只 `alert('设置已保存')`（`:363`） |
| 2 | 弹窗 | `.modal/.modal-box/.modal-body/.modal-footer`（`base.css:159-179`），`z-index:1500`、`width:400px`、`radius:var(--radius)`=6px，遮罩 `pointerdown` 关闭（`base.js:602`） | `.psync-modal/.box`（`index.html:45-59`） | `z-index:999`（低于壳弹窗 1500、Toast 3000，`base.css:162/210`）、`width:min(560px,92vw)`、`radius:10px`、`max-height:86vh`、无遮罩点击关闭、无 Esc 关闭；显隐靠内联 `style.display`（`:388`、`:406`、`:414`） |
| 3 | 空状态 | `.empty-state`（`base.css:428-431`：flex 居中、`min-height:300px`、16px） | `.psync-empty`（`index.html:42-43`：12px、`padding:20px 0`） | 插件类是**死代码**——grep `psync-empty` 只命中定义行 `:42`，JS 从未使用；页面「无数据」实际显示为 `已下载 0 张` / `画师名单: 全部` 之类的文本 |
| 4 | Toast 通知 | `.toast*`（`base.css:208-234`）+ `Toast`（`base.js:362-391`，4 种类型、2.6s 自动消失） | 0 处使用；改用原生 `alert()` **22 处**（`:289,295,315,319,361,363,367,379,384,390,402,405,410,422,423,428,433,434,439,444,445,448`） | 原生弹窗阻塞、无类型区分、无自动消失、样式由 OS 决定、切走/最小化时提示丢失 |
| 5 | 确认对话框 | `confirmDialog(message, options)`（`base.js:394-417`，可 `danger` 样式） | 0 处使用 | 破坏性操作 `#btn-retry-failed`（`:102`「🗑 重试失败作品」）点击即执行（`:441-449`），无二次确认 |
| 6 | 进度条 | 无对应组件（`base.css`/`effects.css` 都没有） | `.psync-bar`（`:20-24`） | 自建属合理；但颜色/圆角未 token 化，也没有用 `.obx-skeleton`（`effects.css:118-131`）做加载骨架 |
| 7 | 徽标/状态点 | 无 badge 组件 | `.dot`（`:14-17`） | 自建属合理；三色硬编码 |
| 8 | 未使用的壳能力 | `createCardGrid`（`base.js:915`）、`createPagination`（`:845`）、`createContextMenu`（`:886`）、`createTree`（`:631`）、`createLightbox`（`:708`）、`Utils.escapeHtml/debounce/formatFileSize`（`base.js:321-359`）、`Motion`（`motion.js`） | — | 全部 0 次引用；`effects.css` 里只有 `.obx-scroll` 被用到，`.obx-anim-*`/`.obx-glass`/`.obx-card-lift`/`.obx-stagger` 全部未使用 |

## 5. 交互约定

- **设置入口**：页面内嵌面板（`index.html:116-165`），不是弹窗、不是路由、不是壳的 `openSettingsModal`。保存按钮 `#btn-save`（`:162`）→ `saveSettings()`（`:347-369`）→ `Bridge.call('save_settings', values)`（`:359`）；失败 `alert(r.error || '保存失败')`（`:361`），成功 `alert('设置已保存')` + `refreshStatus()`（`:363-364`）。数字项统一走 `intSetting()` 夹取（`:339-345`）。
- **其它设置入口**：`📂 画师名单文件`（`:161`）→ `Bridge.call('open_config')`（`:378-380`，后端 `backend/main.py:542` 打开系统文件管理器）；`🔑 获取 Token`（`:160`）→ `start_oauth`（`:383`）后自建 OAuth 引导弹窗（`:381-415`），完成走 `finish_oauth`（`:403`）。
- **保存后的反馈**：仅 `alert`（`:363`、`:405`），不重载、不 Toast。
- **错误提示方式**：22 处 `alert`；桥不可用时把 `#st-last` 文本改成「Bridge 不可用」（`:245`），无错误样式类；任务错误 `$('st-last').textContent = '错误: ' + task.error`（`:273`）；限流冷却用 `#psync-cooldown`（`:170`，内联 `color:#d33`）+ `⏳ Pixiv 限流冷却参考：mm:ss`（`:213`）。
- **选择模型**：页面内没有任何多选/复选框（grep `type="checkbox"` = 0）；「要同步哪些画师」由外部文本文件决定（后端 `backend/main.py:495-541`），前端只显示 `画师名单: N 位/全部`（`:158`、`:240`）。
- **右键菜单**：无（grep `contextmenu` = 0）。
- **键盘快捷键**：**无**（grep `keydown` = 0）。OAuth 弹窗既不能 Esc 关闭，也不能回车提交 code（`:400-412` 只监听按钮 click）。
- **长任务进度与取消**：`startSync`/`startRefresh`（`:285-321`）→ 轮询 `tick()` 每 1500ms（`:304`）；另有常态 `setInterval(refreshStatus, 10000)`（`:453`）与 `setInterval(renderCooldown, 1000)`（`:454`）。取消按钮仅在 `running/queued` 时显示（`:280`），点击 `Bridge.call('cancel_task')`（`:376`）。进度宽度 `$('bar').style.width = pct + '%'`（`:265`），状态文案 6 态映射：`queued/running/done/failed/cancelled/paused`（`:258-261`）。
- **空态/加载态/错误态**：无加载态（首屏直接显示 0 值与「…」，无骨架）；空态类 `.psync-empty` 未被使用；错误态只有文本，无 `--danger` 配色（除内联 `#d33`）。
- **生命周期**：0 处 `onShow`/`onHide`/`onDispose`（grep 三者均 0），但有两个常驻定时器（`:453`、`:454`）——见 §7.6。
- **桥调用点**：`Bridge.call` 共 12 处（`:223,287,314,326,359,376,379,383,403,421,432,443`）。

## 6. 特色设计（值得其他插件吸收）

1. **状态栏双行 + 语义分组**：第一行凭据/根目录/已下载总量/上次结束时间（`index.html:77-82`），第二行「关注 / 喜欢 / 其他 / 失败跳过」四组「共 · 待下 · 已下」（`:83-88`），每个 span 都带 `title` 解释口径（`:84-87`）。解决「同步类插件只给一条进度条，用户不知道总量、待办量和失败量」。
2. **429 冷却做成显式倒计时**：后端返回剩余冷却秒数（`backend/main.py:715`），前端 `cooldownUntil` + 1s 定时器渲染 `mm:ss`（`index.html:204-218`、`:225`），并在文案里直接给建议（`:213`）。解决限流后用户继续点按钮、把冷却越拖越长。
3. **「其他」桶让统计自洽**：`other_done = max(0, len(ids) - following_done - bookmarks_done)`（`backend/main.py:711`），前端单独一行显示（`:86`）。解决旧图导入/手动放置导致的「下载总量比名单加起来还多」。
4. **破坏性修复操作独立成组、后果写进 title**：`刷新记录 / 校验内容 / 重试失败作品`（`index.html:99-103`），每组 `title` 说明做什么（`:100-102`），执行后用一次 `alert` 汇总增删条数（`:423-426`、`:434-437`、`:445`）。解决「同步记录与磁盘不一致时用户无从下手」。
5. **OAuth 向导内嵌在插件里**：`start_oauth` 返回可直接导航的完整 URL（`:385` 注释说明为何不能在控制台 fetch——会被 CORS 拦），textarea 展示 + 复制按钮先 `navigator.clipboard` 后 `execCommand('copy')` 兜底（`:393-399`），并前置「不要再点一次获取 Token，否则验证码失效」的警告（`:178`）。解决 token 过期后必须跳出应用、装 gppt 的断链。
6. **常态轮询与任务轮询分离**：常态 10s 一次（`:453`），需要进度时切到 1.5s（`:303-308`），任务结束自动降频（`:306-307`）。解决长任务期间把桥调用打满。

## 7. 与 Shell 契约的偏差

1. **主题 token 名不存在（27 处）**：`--color-text/--color-border/--color-bg-input/--color-bg` 全部不在 `variables.css` 中（`variables.css:4-86`），只有 fallback 生效。影响面：13 条 `psync-*` 规则 + 18 处内联样式中的 5 处 → 状态栏文字、设置卡边框与输入框、进度槽、OAuth 弹窗、提示文案，即**整页配色**；深色主题下文字对比度不足。
2. **颜色字面量 34 hex + 2 rgba + 18 内联 style**：`#4caf50/#8bc34a`（进度）、`#f44336/#4caf50/#9e9e9e`（状态点）、`#d33`（冷却）在深浅两套主题下都不变；影响换肤能力与 `--danger/--success/--accent` 的统一。
3. **设置双轨**：`backend/main.py:38-114` 声明 8 项 `settings_schema`（其中 `refresh_token` 是 `"secret": True`，`:48`），`index.html:116-165` 又复刻同样 8 项 → schema 任何变更都要改两处。附带影响：`get_settings` 对 secret 键返回掩码 `********`（`shell/backend/plugin_base.py:377`、`:70-87`），插件把它回填进密码框（`index.html:327`），用户在框里看到一串星号、无法判断「已配置 / 未配置」（掩码回传不会覆盖真值，由 `plugin_base.py:395-397` 兜住，因此只是 UX 问题）；另外这里的保存不触发壳的整页重载（壳版见 `base.js:622`）。
4. **22 处原生 `alert` 取代 Toast / confirmDialog**：涉及 12 个桥调用点的全部用户反馈路径（`Bridge.call` 12 处），以及 7 个按钮（`:92-102`、`:160-162`）；破坏性操作无确认。
5. **弹窗未走 `.modal`**：`z-index:999` 低于壳弹窗 1500 与 Toast 3000（`base.css:162`、`:210`）；OAuth 弹窗与壳弹窗同时出现时会被压在下面；缺遮罩点击关闭（壳语义见 `base.js:601-602`）与 Esc 关闭。
6. **生命周期契约未接（有实际代价）**：插件 0 处 `onShow/onHide/onDispose`，却注册了 2 个常驻定时器（`index.html:453`、`:454`）。宿主 image-viewer 声明了 `keepAlive`，切走时只 `v-show` 隐藏 iframe（`shell/frontend/src/App.vue:293-307`），插件 iframe 不会被卸载（只有点「返回相册」才 `src='about:blank'` 销毁，`image-viewer/frontend/js/app.js:168-173`）→ 用户切到别的插件后，pixiv 的 10s `refreshStatus` 与 1s `renderCooldown` 仍在持续打后端。对应 `docs/plugin-guide.md:740-744` 的 `onShow`/`onHide` 用途表。
7. **空态/加载态未落地**：`.psync-empty` 仅定义（`index.html:42`）、0 处使用；全页没有 `.empty-state`（`base.css:428`）、`.loading`（`base.css:427`）或 `.obx-skeleton`（`effects.css:118`）；首屏是「0 值 + …」而不是加载指示。
8. **二级 iframe 的高度链要插件自己补**：壳的 `.view-body` 没有 `height`（`base.css:237`），嵌入型插件必须自写 `html,body{height:100%}` + `min-height:0`（`index.html:9-11`）才能滚动；同类嵌入面板（image-viewer 扩展面板，`image-viewer.css:395-404`）都在各自重复这条，没有共享的容器契约。
