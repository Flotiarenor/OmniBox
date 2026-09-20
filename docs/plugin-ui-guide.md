# OmniBox 插件 UI 统一设计参考

本文不是新提案，而是一次取证的结论：**统一层早就存在**——壳通过 `shell/backend/file_server.py`
把 `variables.css`、`base.css`、`effects.css`、`base.js`、`folder-picker.*`、`motion.js` 注入
每个插件 iframe。8 个插件各自采纳了不同比例，于是"看起来不像一个软件"。

要统一的是**骨架、词汇与交互契约**；页面里长什么样（相册栅格、漫画墙、播放舞台、阅读器排版）
仍然由插件自己决定。

最值得先记住的一条结论：用户在壳设置页可以调**圆角、动效、导航栏宽度**，这些值会同步进
每个插件 iframe；插件只要写死数值，用户的设置就对它失效——"同一个软件里有的页面跟随、
有的不跟随"，这就是不统一的根因（详见 §4.2）。

- 取证对象：`plugins/` 下 8 个插件前端（不含壳自身的 Vue 页面 `App.vue` /
  `views/SettingsView.vue` / `views/StatusView.vue`——它们是宿主 chrome，不是插件）。
- 逐插件原始记录：`docs/_ui-audit/<plugin>.md`；量化统计与命令见各文件与本文 §9。
- 相关既有文档：`docs/plugin-guide.md` §4（前端契约）、§5（主题同步）；
  `docs/group-mesh-implementation-path.md` §5.19（UI 改造踩坑记录）。

---

## 1 三层分工（职责边界）

| 层 | 载体 | 谁维护 | 插件的可动性 |
| --- | --- | --- | --- |
| 壳 chrome | `shell/frontend/src/styles/shell.css` 的 `.nav-sidebar` / `.nav-item` / `.logo` | 壳 | 插件在 iframe 里，看不见也不该管 |
| 注入契约 | `variables.css`（token）、`base.css`（骨架 + 组件）、`effects.css`（动效）、`base.js`（`Bridge`/`Toast`/组件）、`folder-picker.*`、`motion.js` | 壳 | **只引用，不重定义** |
| 插件自有 | `plugins/<name>/frontend/<name>.css`、`js/*.js` | 插件 | 只写"与别人的差异" |

注入点与内容：`shell/backend/file_server.py:59-93`（`_PLUGIN_BOOTSTRAP_SCRIPT`）。
它同时做三件事：注入样式脚本、`Bridge.setPrefix('<name>')`、把父窗口的
`data-theme` 与 `data-custom-colors` 用 `MutationObserver` 同步到插件 `<html>`。

**结论**：插件里出现主题同步代码、出现 `--color-*` 这类自造主题变量、出现 base.css 已有的类
被重新定义，都属于越层。

---

## 2 十一条硬性规则（可直接当 review 清单）

1. **样式里不写颜色字面量**。`color: #333`、`background: #fff`、`border: 1px solid #e0e0e0`
   一律改为 `var(--text-primary)` / `var(--bg-surface)` / `var(--border)`。
2. **透明派生用 `color-mix()`**，不要另算 `rgba()` 常量：
   `color-mix(in srgb, var(--accent) 9%, transparent)`（先例：`base.css:301`）。
3. **只引用真实存在的 token，且不跨插件引用私有 token**：`var(--color-text, #666)`、
   `var(--text-danger, #e5484d)`、`var(--mp-glass, …)` 这类写法不会报错、只是静默失效，
   是最难发现的一类缺陷（见 §4.6）。
4. **骨架只用壳的四件套**：`.view-body` / `.view-toolbar` / `.view-sub-sidebar` / `.view-content`，
   且结构与 §3.1 一致。
5. **二级导航一律 `.obx-nav-item`**（+ `--obx-nav-*` 调差异）；选中态用 `.active` /
   `.is-active` / `data-active="true"` 任一即可，**不要自己再实现一遍 hover/选中态**。
6. **弹窗只用 `.modal` 结构**；设置弹窗首选 `openSettingsModal()`（schema 驱动）。
7. **通知只用 `Toast.*`，确认只用 `confirmDialog()`**；禁止 `window.alert()` /
   `window.confirm()`。
8. **每个会滚动的容器加 `.obx-scroll`**（否则退回又粗又白的系统滚动条）。
9. **动效只用壳的类**：`.obx-anim-*` / `.obx-stagger` + `Motion.stagger()` / `.obx-card-lift` /
   `.obx-glass*` / `.obx-skeleton`。它们已经处理了 `prefers-reduced-motion`；
   自造关键帧要自己补 reduced-motion 覆盖。
10. **状态三态齐备**：空态 `.empty-state`、加载 `.obx-skeleton`（长列表）或 `.loading`、
    错误就地提示 + 重试入口（不用 alert）。
11. **覆盖壳的布局类必须提高特异性**（`.mp-side.view-sub-sidebar`，0,2,0），不能依赖
    注入顺序；不用 `!important` 与 `*` 选择器。

---

## 3 页面骨架

### 3.1 范式 A：侧栏型（image-viewer / manga-library / media-player / group-mesh / document-reader）

规范结构（现状范本：`plugins/image-viewer/frontend/index.html:10-63`）：

```html
<div id="app">                                    <!-- 横向 flex -->
  <aside class="iv-sidebar view-sub-sidebar obx-glass">   <!-- 全高，只画 border-right -->
    <div class="iv-brand">…</div>
    <nav class="iv-nav obx-scroll">
      <div class="iv-nav-label">浏览</div>
      <button class="obx-nav-item iv-nav-item active" data-view="albums">…</button>
      <div id="iv-extensions"></div>              <!-- Companion 入口 -->
    </nav>
    <div class="iv-nav-footer">…</div>
    <div class="iv-sidebar-footer">正在读取…</div>
  </aside>

  <div class="view-body">
    <div class="view-toolbar iv-toolbar">          <!-- 高 = --toolbar-height(48) -->
      <div class="toolbar-group iv-view-heading">…</div>
      <div class="toolbar-group" style="margin-left:auto;">…</div>
    </div>
    <div class="view-content iv-content obx-scroll">…</div>
    <div id="pagination" class="pagination-bar"></div>   <!-- 需要分页才要 -->
  </div>
</div>
```

硬约束（仓库已有回归用例守着，见 `tests/debug_group_mesh_ui.py`）：

- 侧栏是 `#app` 的直接子元素、`y=0`、跑满全高；**工具栏在 `.view-body` 内**，
  不与侧栏同级（否则工具栏横跨整宽、侧栏从工具栏下方才开始，一眼就不一样）。
- 工具栏高 = `--toolbar-height`(48)；侧栏宽 = `--sub-sidebar-width`(240)。
  现状：5 个带侧栏的插件**全部跟随这个 token**（document-reader 一直如此，另外四个在本轮
  收敛时删掉了写死值）。窄窗口的目标宽度要写 `.xx-sidebar.view-sub-sidebar`（0,2,0）才生效
  —— 媒体查询不改变特异性，裸 `.xx-sidebar` 会被壳的 240px 覆盖（`group-mesh.css:135` 记录了
  实测："设成 236 实际 240"）。
- 分隔线分工：侧栏只画 `border-right`、主区只画 `border-bottom`，两边都画会在交角叠成 2px。
- 工具栏左侧放"当前视图标题 + 副标题"，右侧放操作（容器固定
  `style="margin-left:auto;"`，各插件一致但没有类名——见 §10.1 的补件建议）。
- 窄窗口的折叠行为（侧栏折到顶部、卡片改单列）各自实现，但**阈值与顺序**要写注释：
  `max-width` 规则必须降序相邻，同优先级时后者胜出。

### 3.2 范式 B：工具条 + 内容（image-cleaner / pixiv-sync）

```html
<div id="app" class="view-body">
  <div class="view-toolbar cleaner-toolbar">…</div>
  <div class="view-content cleaner-content obx-scroll">…</div>
  <div class="cleaner-footer">批量操作（固定底栏）</div>
</div>
```

范本：`plugins/image-cleaner/frontend/index.html:10-35`。它的**固定底栏放批量操作**
（"已选 N 张" + "每组只留一张" + "删除选中"）值得推广：批量操作不随内容滚走，
也不必挤进工具栏。

反例：`plugins/pixiv-sync/frontend/index.html`（461 行单页）把状态、按钮、进度、设置
全部纵向平铺在 `.view-content` 里，没有分区容器，也没有 `.empty-state`。

### 3.3 范式 C：沉浸态（播放舞台 / 阅读器 / 灯箱）

- 沉浸 = 临时收起自己的骨架，**不是**隐藏壳的导航；隐藏壳导航走
  `parent.document.documentElement` 的 `data-video-fullscreen`（`plugin-guide.md` §5.3）。
- 约束：沉浸态必须留一条可见的退出路径；`Esc` 必须能退出；进出用 `.obx-anim-fade`；
  退出后回到范式 A/B 的原状态（滚动位置、选中项）。
- 图片类预览一律用壳的 `createLightbox()`（已支持滚轮缩放、拖拽、键盘、
  信息面板），别自造。
- 现状：media-player 的 `mp-stage-*`、document-reader 的阅读器与整页朗读页
  （`js/reader-voice-page.js`）、image-viewer 的灯箱 + `#extension-view` 内嵌视图。

### 3.4 内嵌/扩展视图（宿主里的二级页面）

两种挂载方式（键的语义见 `plugin-guide.md` §2.1）：

| 方式 | 宿主渲染点 | 现状 |
| --- | --- | --- |
| 侧栏入口 `embedUrl` | `renderExtensions(container, host, placement)` | image-cleaner、pixiv-sync 挂 image-viewer；group-mesh 的 network-location 挂"网络位置" |
| 面板内嵌 | `.extension-view > .extension-view-header(title+返回) + .extension-view-body > iframe` | image-viewer `index.html:65-73` |
| 原生视图 `view` | 宿主自己渲染（无 iframe） | netease-music 的 5 个 `ncm-*` 视图由 **media-player** 渲染（`app-views.js:152-201`、`app-render.js:190-233`） |

**被内嵌页的附加契约**：

- 宿主在 URL 上追加 `?embed=1`（image-viewer 的扩展面板 `_embedUrl()`、
  `folder-picker.js` 的 `openNetworkPicker()` 都已经这么做）；页面据此把**自己的工具栏
  降级为普通操作行**——不搬按钮、不删 DOM，只收起"标题 + 48px 底边横条"这些与宿主
  header 重复的 chrome：

  ```html
  <!-- 页面 <head>：早于正文渲染，避免先闪一下完整工具栏 -->
  <script>
    if (new URLSearchParams(location.search).has('embed')) {
      document.documentElement.classList.add('is-embedded');
    }
  </script>
  ```
  ```css
  /* html.is-embedded 是 0,1,1，压得过 base.css 的 .view-toolbar(0,1,0)，与注入顺序无关 */
  html.is-embedded .view-toolbar {
    height: auto; padding: 2px 16px 0; border-bottom: none; background: transparent;
  }
  html.is-embedded .<prefix>-title { display: none; }   /* 标题交给宿主 header */
  ```
- 页面级操作（"重新扫描""取消任务"）可以留在工具栏里：内嵌时工具栏只是被降级为普通
  操作行，按钮仍然可见可点；但**不要把自己的标题、说明文字或侧栏塞进工具栏**，
  那些与宿主 header 重复的部分才是要收掉的。
- 视觉上必须与宿主同族（同一个 token 体系、同一套按钮/卡片），因为用户看到的是
  宿主面板里的一块，不是另一个应用。
- `hidden: true` 的插件（image-cleaner / netease-music / pixiv-sync）不出现在壳导航，
  入口只能来自宿主扩展或 URL——不要把"设置""刷新"这类全局操作塞进内嵌页。
- **`view` 型扩展的 UI 代码属于宿主**：netease-music 自己那份
  `frontend/index.html` + `js/app.js`（154 行）就是重复实现——它 `hidden: true`
  不可达，且全仓库没有任何地方构造 `?view=ncm-*` 的 URL 交给它加载；
  它还用 `parent.mediaPlayerApp`（`app.js:89`）去拿宿主的全局对象，而那个全局只在
  media-player 自己的 iframe 里存在（`media-player/frontend/index.html:273`），
  一旦这份页面被壳直接加载，父窗口是壳 ⇒ 点歌 100% 落到错误分支。
  声明 `view:` 的插件，前端只应保留状态/说明页，或者干脆没有前端页面。

---

## 4 设计 token 与主题

### 4.1 主题与自定义色由壳负责

- 插件**不写**任何主题同步代码；`data-theme` 与 `data-custom-colors` 由注入脚本同步
  （`file_server.py:67-91`、`docs/plugin-guide.md` §5.1-5.2）。
- 用户可以在壳设置页把任意 token 改成任意颜色，所以**不能假设某个 token 是深色或浅色**；
  选中态不要用实心强调色填充 + 白字（用户可能把 `--accent` 设成浅黄）。

### 4.2 用户可调的三组 token：写死数值 = 让用户的设置失效

壳设置页（`shell/frontend/src/views/SettingsView.vue`）把下面这些 token 做成了用户可调，
值通过 `data-custom-colors` 一路同步进每个插件 iframe：

| 用户可调 | 设置页控件 | 插件写死数值的后果 |
| --- | --- | --- |
| `--radius` / `--radius-sm` / `--radius-lg` | 「外观 → 形状」预设：直角 / 小圆角 / 默认 / 大圆角（`SettingsView.vue:164-169`） | 用户选"直角"，写死 12px/14px/999px 的插件纹丝不动 |
| `--transition-fast` / `--transition-normal` | 「外观 → 动效」预设：正常 / 精简 / 关闭（`:170-174`） | 用户选"关闭"，自造 `transition: .22s` 的插件照旧有动画 |
| `--nav-width` | 「导航栏宽度」滑杆（`:192-195`） | 只影响壳自己的侧栏；插件内层侧栏走 `--sub-sidebar-width`，**不在可调列表里**（见 §3.1） |

这就是"看起来不像一个软件"的根因：**同一个用户设置，有的插件跟随、有的插件不跟随**。
现状里圆角在 5 个插件里各自定义或写死、`--transition-*` 只在壳组件与少数插件里生效、
`--obx-*`（玻璃/阴影/缓动/骨架屏）几乎没有插件使用。

### 4.3 token 清单（唯一来源：`shell/frontend/public/shell/variables.css`）

| 组 | token | 浅色 | 深色 |
| --- | --- | --- | --- |
| 背景 | `--bg-app` / `--bg-surface` / `--bg-sub-sidebar` | `#f3f3f3` / `#ffffff` / `#ffffff` | `#0d1117` / `#161b22` / `#161b22` |
| 背景 | `--bg-hover` / `--bg-active` | `#e9ecef` / `#cfe6fa` | `rgba(255,255,255,.08)` / `rgba(0,120,212,.2)` |
| 背景 | `--bg-overlay` / `--bg-overlay-strong` | `rgba(0,0,0,.45)` / `rgba(0,0,0,.92)` | `rgba(0,0,0,.6)` / `rgba(0,0,0,.95)` |
| 文本 | `--text-primary` / `--text-secondary` / `--text-muted` | `#1a1a1a` / `#6c757d` / `#adb5bd` | `#c9d1d9` / `#8b949e` / `#6e7681` |
| 文本 | `--text-on-accent` | `#ffffff` | `#ffffff` |
| 边框 | `--border` / `--border-dark` | `#dee2e6` / `#ced4da` | `#30363d` / `#21262d` |
| 强调 | `--accent` / `--accent-hover` | `#0078d4` / `#0056b3` | `#2f81f7` / `#58a6ff` |
| 语义 | `--danger` `--danger-hover` `--danger-soft` `--success` `--warning` | — | — |
| 尺寸 | `--nav-width` 200 / `--sub-sidebar-width` 240 / `--toolbar-height` 48 | | |
| 圆角 | `--radius` 6 / `--radius-sm` 4 / `--radius-lg` 10 | | |
| 阴影 | `--shadow-sm` / `--shadow-md` / `--shadow-lg` | | |
| 动效 | `--transition-fast` .15s / `--transition-normal` .25s | | |

动效层另有 `effects.css:13-22` 的 `--obx-*`：`--obx-radius` 14 / `--obx-radius-sm` 10 /
`--obx-ease` / `--obx-ease-spring` / `--obx-glass-blur` 18 / `--obx-shadow-1` /
`--obx-shadow-2` / `--obx-accent-soft`。**现状是插件几乎不用它们**
（采纳计数：group-mesh 7 处、image-viewer 3 处、manga-library 1 处，其余为 0），
各自另写了一套阴影/玻璃/缓动值。最直接的证据是 media-player 把 `effects.css` 的值
原样抄成了自己的名字（`media-player.css:9-23`）：

| media-player 私有 token | 值 | 等价物（已存在） |
| --- | --- | --- |
| `--mp-glass` | `color-mix(in srgb, var(--bg-surface) 84%, transparent)` | `.obx-glass`（`effects.css:94-98`） |
| `--mp-glass-strong` | 92% 版本 | `.obx-glass-strong`（`:100-104`） |
| `--mp-shadow-1` | `0 6px 24px rgba(0,0,0,0.10)` | `--obx-shadow-1`（`:19`） |
| `--mp-shadow-2` | `0 14px 44px rgba(0,0,0,0.18)` | `--obx-shadow-2`（`:20`） |
| `--mp-scroll-thumb` / `-hover` | `color-mix(… --text-secondary 38% / 62% …)` | `.obx-scroll` 的滚动条规则（`:176-182`） |

**规范**：这些值一律改为引用 `--obx-*` / 壳类；插件私有 token 只用于真正的插件差异。

### 4.4 已存在的跨插件视觉母题（应当固化，而不是各自重写）

4 个插件的样式表开头是同一段"母题"（image-viewer.css:6-18、manga-library.css:6-18、
media-player.css:9-29、document-reader.css:18-36）；group-mesh（`group-mesh.css:28-59`）
只共享了 token 块与 `#app` 的 flex 行骨架，背景是平涂的 `--bg-app`：

```css
:root {                     /* 或 #app {（document-reader 的写法，作用域更小） */
  --xx-radius: 14px;                                   /* 页面级圆角 */
  --xx-gradient: linear-gradient(135deg, var(--accent) 0%,
                   color-mix(in srgb, var(--accent) 55%, <第二色>) 100%);
  --xx-accent-soft: color-mix(in srgb, var(--accent) 14%, transparent);
}
#app {
  display: flex; height: 100vh; overflow: hidden;
  background: radial-gradient(1000px 420px at 92% -12%, var(--xx-accent-soft), transparent 62%),
              var(--bg-app);                            /* 强调色氛围底 */
}
```

- **氛围底**（右上角径向强调色 + `--bg-app`）是这套 UI 最有辨识度的一处，建议写进规范：
  只能由 `--accent` 派生（用户自定义强调色后必须仍然和谐），不要写死色相。
- **品牌渐变**各自不同的第二色（image-viewer `#0ea5e9`、manga-library `#8b5cf6`、
  media-player `#8b5cf6`、document-reader `#7aa2ff`）是各插件唯一的"个性色"，
  可以保留，但必须走 `color-mix` 派生而不是整条写死。
- **圆角**：已有 4 套数值（`--radius` 4/6/10、`--obx-radius` 10/14、各插件的 12/14、
  以及零散 5/8/9/12/18px）。规范：默认用壳的 `--radius*`；插件级微调**必须赋值壳 token**。
- 反例：`group-mesh.css:29-30` 写的是 `--gm-radius: var(--radius-lg, 12px)`，
  而壳的 `--radius-lg` 实际是 **10px**——兜底值与真值不一致，token 一旦取不到就静默偏大。
  正确写法是 `var(--radius-lg)`，或直接使用 `--radius-lg`。

### 4.5 插件私有 token 的命名

- 私有变量必须带插件前缀：`--nr-*`（document-reader）、`--gm-*`、`--iv-*`、`--ml-*`、
  `--mp-*`、`--cleaner-*`、`--psync-*`。
- 现状里 `document-reader` 有 3 个无前缀变量（`--reader-bg-color` / `--reader-text-color` /
  `--reader-highlight`）——它们由 JS 在运行时写入，命名破坏了前缀约定，需要改名。
  另有 3 个死变量 `--nr-cover-*`（`document-reader.css:23-25`，定义后无任何引用）。
- 4 个插件重复实现 `--<prefix>-accent-soft` 这类派生色 → 直接用 `--obx-accent-soft`
  或就地 `color-mix()`。
- 定义位置现状不一：image-viewer / manga-library / media-player / group-mesh 放 `:root`，
  document-reader 放 `#app`（作用域更小）。两者都可以，但**只放插件私有 token**，
  不得借机重定义壳的主题 token。

### 4.6 明确禁止的写法

```css
/* ✗ 不存在的 token + 字面量兜底：不报错，只是静默退回浅色，深色主题下必然错色 */
color: var(--color-text, #666);
background: var(--color-bg, #fff);

/* ✗ 猜一个相近的名字：壳里只有 --danger，没有 --text-danger */
color: var(--text-danger, #e5484d);

/* ✗ 引用别的插件的私有 token：这个 iframe 里 --mp-glass 恒不存在 */
background: var(--mp-glass, var(--bg-surface));
```

- `--color-text` / `--color-border` / `--color-bg` / `--color-bg-input` 在仓库里**没有任何定义**，
  只有 `plugins/pixiv-sync/frontend/index.html:7-60` 在使用它们——该插件对真实主题 token
  的使用数为 **0**，整页不跟随主题（详见 §9）。
- `--text-danger`（`image-viewer.css:359`）同样是猜的名字，实际永远取 `#e5484d`，
  暗色主题下不跟随。
- `--mp-glass`（`manga-library.css:102`）是 media-player 的私有 token，跨插件引用必然失效。
- 结论：**"看起来生效"和"真的生效"必须能分辨**。凡是 `var(--x, <字面量>)` 里带了颜色兜底，
  就应该先确认 `--x` 真实存在；加兜底色只是把错误藏起来。

---

## 5 组件规范

| 组件 | 壳提供的入口 | 规范用法 | 现状 |
| --- | --- | --- | --- |
| 按钮 | `base.css:13-36` `.btn` / `-primary` / `-danger` / `-danger-solid` / `-sm` / `.active` | 工具条 `.btn`/`.btn-sm`；主操作 `-primary`；破坏性 `-danger` 且二次确认 | 全部插件在用 |
| 分组切换 | 无专用类 | `.btn.btn-sm.active`（image-cleaner 的 tab）或 `.obx-nav-item` | image-cleaner 用前者 |
| 搜索框 | 两种：单输入框用 `.search-input`（`max-width:400px`）；带图标/清除按钮的搜索框用 `.search-field > .search-field-icon + input + .search-field-clear` | 结构与外观**只有这一份实现**：胶囊外框 + 图标在流内 + 输入无边框，聚焦用 `:focus-within` 描边外框；宽度走 `--obx-search-width`（窄窗口由壳统一收窄）。插件不写搜索样式，**也不给容器再加插件类名** | 已统一：四个插件（document-reader / image-viewer / manga-library / media-player）现在标记完全同构，插件侧搜索样式删净（原先共 4 套类名、7 档宽度、1 处玻璃底、1 处边框重写）。形态取自 document-reader 的紧凑胶囊版本 |
| 侧栏导航 | `base.css:290-347` `.obx-nav-item` + `--obx-nav-*` | 结构/选中态都靠壳；只保留图标栏宽度等差异 | 5 个插件已用；image-cleaner/pixiv-sync 无 |
| 侧栏标题/底栏 | `.sub-sidebar-header` / `.sub-sidebar-footer` | 分组标题用大写小字；底栏放统计 | image-viewer 用 `.iv-sidebar-footer` |
| 卡片/网格 | 无壳类（曾有 `createCardGrid`，因零采用且渲染的 `.manga-*` 无任何样式定义，已删除） | 卡片属插件自己的内容区布局：建议统一"封面 + 标题 + 副标题/徽标"，圆角走 `--radius-lg`，悬浮 `.obx-card-lift`，选中态 = 2px `--accent` 描边 | media-player `.mp-card-grid`、manga-library `.ml-grid`、image-viewer `.iv-image-grid` |
| 弹窗 | `base.css:159-179` `.modal` / `.modal-box` / `.modal-body` / `.modal-footer` | 入场 `.obx-anim-scale`/`-pop`；遮罩关闭用 `pointerdown` 并判 `e.target === overlay`（`base.js:602`，`tests/debug_modal_backdrop_press.py` 守） | image-viewer/manga-library/document-reader 已用；pixiv-sync 自造 `.psync-modal` |
| 设置弹窗 | `base.js:566-628` `openSettingsModal()` + `settings_schema` | 首选；schema 类型 `text`（默认）/ `number` / `range` / `checkbox` / `select` / `textarea` / `directory`；`secret: True` 标记敏感字段（壳负责掩码与"留空即不改"）；`directory` 自动接入 `folder-picker`，`local_only` 可关掉"网络位置"入口 | 7 个插件调用（本轮把 pixiv-sync、image-cleaner 也接上）；仍自建面板的只有 image-viewer（文件夹/行高/排序，需要就地预览）与 document-reader（阅读设置弹窗 + 整页朗读设置） |
| 表单控件 | `base.css:181-206` `.settings-form` / `.field*` / `.field-range*` | 自建设置面板也必须复用这些类 | image-viewer 自造 `.iv-field`/`.iv-setting-item` |
| Toast | `base.js:362-393` `Toast.success/error/info/warning` | 全部反馈走它 | 采纳 134 处；**pixiv-sync 有 22 处 `alert()`** |
| 确认框 | `base.js:394-418` `confirmDialog(msg, {danger})` | 破坏性操作一律用它 | 采纳 12 处，无 `window.confirm` |
| 右键菜单 | `base.js:886` `createContextMenu` | 网格/列表的批量入口 | document-reader 3 处、image-viewer 1 处 |
| 分页 | `base.js:845` `createPagination` + `.pagination-bar` | 服务端分页的列表 | image-viewer 1 处 |
| 目录树 | `base.js:631` `createTree` + `.tree-container` | 目录选择 | document-reader / image-viewer |
| 灯箱 | `base.js:708` `createLightbox` | 图片预览 | image-cleaner 2 处、image-viewer 1 处 |
| 滚动条 | `effects.css:140-183` `.obx-scroll` | 每个滚动容器都要加；**只有沉浸态**（阅读正文、舞台）可以整块隐藏滚动条 | 多数已加；media-player 的**主内容区** `#media-content` 漏加（`index.html:154`，同文件其它 5 个滚动容器都加了）；document-reader 正文区按沉浸语义隐藏（css:216、233-236）；pixiv-sync 只有外层容器 |
| 降低动态效果 | `effects.css:186-201` 覆盖 `.obx-anim-*`、`.empty-state-icon` 与 `.obx-stagger > *` | 用壳的类；**直接引用关键帧名不会被白名单覆盖**，要自己补一条 reduced-motion 覆盖 | media-player 用 `*, *::before, *::after` + 3 处 `!important` 全量压，有效但代价是连壳组件一起压 |
| 骨架屏 | `effects.css:118-131` `.obx-skeleton` | 首屏/长列表 | 采纳 0（都用文字"加载中…"），group-mesh 另有 `.gm-skeleton-line`；属"新增能力"而非缺陷（见 §10.5） |
| 空态 | `base.css`「通用状态」的 `.empty-state` + `.empty-state-icon/-text/-hint`（+ `--obx-empty-min-h`、`.empty-state--inline`、`.empty-state--error`） | 空列表/无结果一律用它；**不要自造 `.xx-empty`**。出错态（加载失败等）加 `.empty-state--error`，但仍要给出"下一步"提示 | 本轮统一：7 个插件约 57 处改用它，各插件的 `.iv-empty/.nr-empty/.ml-empty/.mp-empty-state/.nl-empty/.cleaner-empty/.empty/.psync-empty` 全部删除；`.empty-state--error` 用于 image-cleaner 扫描失败与 media-player 歌单加载失败。保留的插件专属例外见下 |
| 浮层层级 | `base.css` `.modal` 1500 / `base.js` `.toast-container` 3000 | 自绘浮层的 `z-index` 必须 ≥ 1500 且低于 3000，否则会被壳的弹窗/Toast 盖住 | media-player `.mp-modal` 500 / `.mp-context-menu` 520、pixiv-sync 自绘弹窗 999（`index.html:46`）都会被盖住；manga-library 自研阅读器 2000 反过来盖住壳弹窗 |
| 键盘可达性 | 壳的灯箱已支持 Esc / ←→ / 滚轮 / 拖拽 | 至少有：`Esc` 关闭最上层浮层、沉浸态 `←/→` 切换、搜索框 `Esc` 清空 | image-viewer 全插件无键盘处理；自绘右键菜单（`app-nav.js:96-134`）不监听 Esc 与 scroll 重定位 |

补充约定：

- **设置优先走 schema，不要双轨。** 后端 `settings_schema` 声明了什么，前端就该用
  `openSettingsModal()` 渲染同一份；手写第二份字段列表必然漂移。pixiv-sync 就是双轨
  （`backend/main.py:38-114` vs `index.html:116-165`）：字段清单、默认值、范围钳制
  （`intSetting()`）各写一遍，提示文案硬编码成内联 style，而且**旁路了壳的目录选择器**
  —— `download_dir` 在 schema 里声明成 `type: "text"`，用户在插件里只能手打路径。
  （凭据不会因此泄露或被覆盖：壳的 `save_settings` 对掩码回传做了保护，
  见 `shell/backend/plugin_base.py:392-397`——"掩码原样回传 = 不改动"。）
- **长任务进度四件套**：进度条 + "已完成/总数" + "当前项" + 取消按钮。后端统一用
  `shell/backend/tasks.py` 的 `BackgroundTask`（`plugin-guide.md` §3.4），前端轮询
  `status()`。壳目前没有进度条类，pixiv-sync 自造了 `.psync-bar`、media-player 自造了
  播放/加载进度条——建议提取一个壳级进度类，让"同步""扫描""下载"这类长任务长得一样。
- **后端有设置项、前端必须有入口**：image-cleaner 的 `threshold`（`backend/main.py:23-27`，
  `type: range`）此前在前端没有任何入口，用户只能手改配置 —— 已补上工具栏的「⚙ 设置」。
  设了 schema 就要把它接上，否则等于没有这个设置。
- **图标**：导航/按钮统一 Emoji 前缀（现状 8 个插件都这样，无需改）。
- **标题行**：主标题 + 小字副标题两行结构（image-viewer 的 `.iv-view-title`/`.iv-view-sub`、
  group-mesh 的 `data-title`/`data-sub`）；面板切换时标题必须跟着变
  （这是 group-mesh 踩过的坑：`data-title` 忘写导致标题永远不变）。
- **文案**：空态一句话 + 一个下一步动作；错误提示说"发生了什么 + 能做什么"，不贴堆栈。
- **内嵌页不得重复宿主 chrome**：image-cleaner 内嵌进 image-viewer 后，宿主的
  `.extension-view-header`（`image-viewer.css:382-390`）与插件自己的 48px
  `.view-toolbar` 叠成两层横条、两个标题。

---

## 6 CSS 组织与命名

1. **类名前缀**：`<plugin>-`。现有短前缀：`iv-` `ml-` `mp-` `gm-` `nr-` `cleaner-`
   `psync-`，netease 的原生视图由 media-player 渲染故沿用 `mp-`/`ncm-`。
   反例：manga-library 的 `.manga-reader` / `.reader-*`（与 document-reader 的
   `.reader-*` 撞名且都不是自己的前缀）、manga-library 的 `.btn-icon`（`base.css` 里没有）。
2. **壳类不重定义**，只补差异。需要覆盖壳规则时提高特异性而不是改顺序——
   顺序由壳的注入决定：`.gm-side.view-sub-sidebar`（0,2,0）才能压过 `base.css` 的
   `.view-sub-sidebar`（0,1,0）。
   **优先改 token，而不是叠插件类名**：壳组件的尺寸/配色都应暴露成 `--obx-*`（如
   `.search-field { --obx-search-width: 160px; }`、`--obx-nav-*`），插件覆写 token 即为"差异"。
   `.iv-search input { width: 220px }` 这种"壳类 + 插件类 + 选择器"是反例：四个插件四套类名、
   七档宽度，看起来就是四个搜索框 —— 本轮已全部删除。
3. **不要用元素级选择器穿透壳组件**：group-mesh 用 13 个 `.modal-body input/textarea` 之类
   的规则改壳弹窗里的控件外观（`group-mesh.css:375-426`），壳一改结构就失效；
   要改就改自己的类，或者提出新的壳 token。
4. **不要和壳注入的全局函数同名**：image-viewer 的 `app-settings.js:15` 有方法名
   `openSettingsModal`，与壳的全局 `openSettingsModal()`（`base.js:566`）同名——
   限定作用域时容易误调，读代码时更难分辨。
5. **媒体查询**：同断点规则相邻、降序排列，并写注释说明顺序是硬约束
   （`max-width:1040px` 与 `max-width:720px` 都写单列时，后者胜出）。
6. **内联 `style=`**：只允许一次性数值（`style="width:360px;"`、`style="margin-left:auto;"`）；
   禁止内联颜色。现状：pixiv-sync 有 18 处内联样式，image-viewer 有
   `style="width:100%;max-width:none;"` 反制 `.search-input` 的 max-width；
   manga-library 用内联 `style="width:480px"` 改壳 `.modal-box` 的 400px。
7. **`!important` 与 `*` 选择器**：禁止作为常规手段。唯一可接受的场景是像
   media-player 那样兜底 `prefers-reduced-motion`，而正确做法是改用 `.obx-anim-*`
   （壳的白名单只覆盖这些类）。
8. **高度链**：现状两种写法并存——5 个插件用 `#app { height: 100vh }`
   （image-viewer/manga-library/media-player/document-reader/image-cleaner），
   group-mesh 与 pixiv-sync 用 `html, body { height: 100% }` 的链式高度。
   两者在 iframe 里都能用，但**必须显式写一条**：`base.css:11` 给 `body` 设了
   `overflow: hidden`，没有高度链时内层 `.view-content` 的 `overflow-y` 不生效，
   长列表直接不可达（netease-music 的独立页就是这个状态）。
9. **浮层的颜色**：遮罩/黑底只用 `--bg-overlay` / `--bg-overlay-strong`。
   现状 image-viewer 同时出现 0.45 / 0.55 / 0.62 / 0.65 四档黑度，pixiv-sync 自写
   `rgba(0,0,0,.45)`。
10. **浮层的层级**：自绘浮层 `z-index` 必须 ≥ 壳的弹窗（1500）且低于 Toast（3000）。
    现状 media-player `.mp-modal` 500 / `.mp-context-menu` 520、pixiv-sync `.psync-modal` 999
    都会被壳的弹窗/Toast 盖住；manga-library 自研阅读器 2000 反过来盖住壳弹窗。
11. **文件组织**：单 CSS 文件内分区注释：骨架 → 组件 → 状态 → 响应式 → 主题例外。
    `media-player.css`（44KB）/ `group-mesh.css`（21KB）/ `document-reader.css`（21KB）
    已经在做，可作范本。
12. **JS 分片**：现状 `media-player`（9 片）、`document-reader`（9 片）、`image-viewer`
    （8 片）按职责分片，`manga-library`（3 片）、`image-cleaner`（1 片）较少。
    分片是好事，但**共享组件不要随手复制**：`image-viewer/js/app-utils.js` 与
    `media-player/js/utils.js` 这类工具文件应优先收敛到壳的 `Utils`。

---

## 7 状态、生命周期与保活

- `keepAlive` 的申请条件（`plugin-guide.md` §4.4）：播放/朗读不能断、要持续上报状态、
  首屏很贵。现状 5 个插件声明 `true`（document-reader / group-mesh / image-viewer /
  manga-library / media-player），3 个内嵌型不声明。
- 声明保活后必须在 `onHide` 停掉 `setInterval` / rAF 自循环 / 轮询，在 `onShow` 恢复，
  `onDispose` 摘监听器。现状对照：

  | 插件 | 生命周期钩子 | 常驻定时源 | 结论 |
  | --- | --- | --- | --- |
  | media-player | 完整（本轮补，`app.js:69/90`） | `lyrics-parser` 频谱 rAF（`onHide` 停、`onShow` 恢复）、2s 进度保存（有意保留） | 只停视觉类工作，播放与进度保存不受影响 |
  | pixiv-sync | 完整（本轮补，`index.html` 的 `bindLifecycle()`） | 两个 `setInterval`（10s 状态 / 1s 冷却）+ `tick()` 自续轮询 | `onHide` 停定时器并置 `polling=false`，`onShow` 恢复并接着显示长任务 |
  | document-reader | 只有 `onDispose`（`app.js:256-257`） | **无持久定时器**（仅 3 处一次性 `setTimeout`：防抖写入与翻页） | 刻意不绑 `onHide`（`app.js:247-258` 有注释：切走时朗读应当继续）——这是正确做法，不是缺陷 |
  | manga-library | 完整且幂等（`app.js:34-40`） | 2s 任务轮询 | **范本**：`onHide` 停、`onShow` 起、`onDispose` 兜底 |
  | image-viewer | 完整（`app.js:92-114`） | 幻灯片定时器 | 范本：`onHide` 记住位置，`onShow` 原位继续 |
  | group-mesh | 完整（`remote.js:727-730`） | 远端共享读回轮询 | 范本 |
  | image-cleaner / netease-music | 无（不需要，均不保活） | — | — |

- **`onHide` 的语义是"停止视觉与轮询类工作"，不是"停止播放"**（`plugin-guide.md:762`）：
  document-reader 刻意不在 `onHide` 里暂停朗读（`app.js:250-252` 注释说明"一切走朗读就断"
  是曾经的缺陷）是正确做法；media-player 应当只停歌词 rAF、保留进度保存，
  两个插件要停的是各自的"状态刷新"部分。
- 设置保存后**壳会整页重载插件**（`base.js:622`）：不要假设本地状态能保留；
  需要保留的视图状态（当前目录、滚动位置、选中项）自己落 `localStorage`。
- `settings_schema` 的 `directory` 字段默认允许"网络位置"来源；语义上不接受远端来源的
  字段（如"远端下载目录"）要声明 `local_only: True`（`tests/debug_settings_local_only_ui.py`）。

---

## 8 跨插件扩展入口

- 宿主只写一个泛化渲染点：`<div id="…-extensions"></div>` +
  `renderExtensions(container, host, placement)`，不要硬编码某个扩展插件的名字。
- 扩展条目的四种键（`view` / `embedUrl` / `route` / `method`）决定点击行为，
  宿主不要假设只有 `embedUrl`。
- 内嵌 iframe 用壳的渲染器或 `.obx-embed-frame`；宿主提供"返回"路径
  （image-viewer 自建 `#extension-frame` + `#extension-view-close`，没有用壳的
  `renderExtensions` 内嵌分支与 `.obx-embed-frame`）。
- **内嵌页不得跨 iframe 读宿主内部对象**：image-cleaner 直接读
  `parent.imageViewer.lightbox`（`app.js:144-147`）来复用宿主的灯箱——宿主一改名就
  静默退回自建灯箱，属于"契约之外"的耦合。跨插件能力请走
  `Bridge.callPlugin()` 或扩展注册表。
- 被内嵌页遵守 §3.4 的"不再画骨架"约定；视觉与宿主同族。

---

## 9 现状对照与偏差台账

### 9.1 一览（量化口径：`var(--` 次数 / 颜色字面量次数 / 共享组件调用数）

| 插件 | 形态 | 骨架 | `var(--` | `#hex` | `rgba(` | `.obx-*` | alert() | 生命周期 | 共享组件（调用数） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| media-player | A + 沉浸 + ncm 原生视图 | ✅ | 246 | 22 | 46 | 16 | 0 | 完整（仅视觉） | Toast 49 / confirm 2 / settings 1 |
| document-reader | A + 沉浸（阅读/朗读页） | ✅ | 120 | 19 | 4 | 14 | 0 | 仅 onDispose（无定时器） | Toast 40 / confirm 1 / settings 1 / menu 3 / tree 1 |
| group-mesh | A | ✅ | 116 | 5 | 1 | 39 | 0 | 完整 | Toast 3 / confirm 3 / settings 2 |
| image-viewer | A + 内嵌扩展 | ✅ | 95 | 8 | 12 | 17 | 0 | 完整 | Toast 31 / confirm 4 / settings 2 / menu 1 / tree 1 / pager 1 |
| manga-library | A | ✅ | 84 | 7 | 7 | 19 | 0 | 完整（范本） | Toast 5 / confirm 1 / settings 1 |
| image-cleaner | B（内嵌宿主面板） | ✅ | 16 | 3 | 3 | 1 | 0 | 无（不保活） | Toast 4 / confirm 1 / lightbox 2 |
| pixiv-sync | B（内嵌，单页） | ✅ 已用壳骨架与 `.modal` | 31 | **0** | **0** | 1 | **0** | 完整（本轮补） | Toast 22 |
| netease-music | 独立页，`hidden` 不可达 | ⚠️ 无骨架 | 9（全是真 token） | 0 | 0 | 0 | 0 | 无（不保活） | Toast 2 |

> 上表为**本轮修复后**的实测值；修复前 pixiv-sync 是 **34 hex / 2 rgba / 22 alert / 0 真 token**，
> image-viewer 多 1 处 `--text-danger`、manga-library 多 1 处 `--mp-glass`（见 §9.5）。
> `var(--` 含壳 token 与插件私有 token；`#hex`/`rgba(` 含 `<style>` 与内联样式。
> 计数命令与逐文件明细见 `docs/_ui-audit/*.md`。

### 9.2 偏差分级

**P0（功能上就是坏的，或用户设置直接失效）** —— 第 1-4 项已在本轮修复，
下面保留修复前的描述用于对照，改动清单见 §9.5。

1. `pixiv-sync` 主题全线失效：27 处 `var()` 全部是仓库里不存在的
   `--color-text` / `--color-border` / `--color-bg` / `--color-bg-input`，
   真实主题 token 使用数 **0**，34 处 hex 兜底 ⇒ 深色主题下整页不换肤；
   叠加 22 处 `alert()`、18 处内联样式、自造 `.psync-modal`（z-index 999）。
2. `pixiv-sync` 的生命周期完全没接：0 处 `onShow`/`onHide`/`onDispose`，却有
   `setInterval(refreshStatus, 10000)`（每 10s 打一次后端 `get_status`）与
   `setInterval(renderCooldown, 1000)`（`index.html:453-454`）。它的宿主 image-viewer
   是 `keepAlive`，用户切走后这两个定时器继续跑。
3. `media-player` 声明 `keepAlive` 却同样 0 处钩子，而 `plugin-guide.md:763-764` 已经把
   "media-player 在 onHide 里停 lyrics-parser 的 rAF" 写成了事实——**文档与实现不一致**。
   实际代价有限（rAF 在隐藏 iframe 里会被浏览器节流；2s 进度保存是有意保留的），
   但缺钩子会让后续任何"只在可见时做的工作"没有落点。
4. `image-viewer.css:359` 用不存在的 `--text-danger`，永远取 `#e5484d`；
   `manga-library.css:102` 引用 media-player 的私有 `--mp-glass`，永远取兜底值。

**P1（口径不一，用户能感觉到）**

5. 用户可调的 `--radius*` / `--transition-*` 被大量写死数值绕过（§4.2）：5 个插件各自
   定义 `--<prefix>-radius`（12/14px 等），反而只有 group-mesh 去引用壳 token（且兜底值写错）。
6. `media-player` 大规模重复造壳：8 个关键帧逐值复制成 `mpSpin`/`mpFadeUp`/…（css:1965-2015）、
   `--mp-shadow-*`/`--mp-glass*` 与 `--obx-*` 数值逐字相同、整段滚动条规则（css:879-947）重复
   `.obx-scroll`，另有 `.mp-modal`（z-index 500）对 `.modal`、`.mp-context-menu` 对
   `createContextMenu`、`.mp-empty-state` 对 `.empty-state`。
   同时它的主内容区 `#media-content` 反而漏了 `.obx-scroll`。
7. 设置入口四套并存：`openSettingsModal`（7 个插件，本轮把 pixiv-sync、image-cleaner 接上）、
   自建设置弹窗（image-viewer `#settings-modal`）、自绘阅读设置弹窗 + 整页朗读设置
   （document-reader，同一插件两套）；表单控件类也各不相同（`.field` vs `.iv-field`
   vs `.iv-setting-item`）。pixiv-sync 的双轨已消除：字段清单与范围钳制现在只有
   `backend/main.py` 的 `settings_schema` 一份，`download_dir` 也改成了 `type: "directory"`
   （由壳的 folder-picker 渲染，单值取首行路径）。
8. 空态已统一到壳的 `.empty-state`（本轮，见 §9.6）；骨架屏 `.obx-skeleton` 采纳数仍为 **0**
   —— 它是"新增能力"而非契约违背，已降级到 §10.5。
9. 搜索框已统一为**一个**组件：四个插件标记完全同构（`.search-field > .search-field-icon +
   input + .search-field-clear`），插件侧不再有搜索样式或插件类名；宽度与窄窗口行为由壳的
   `--obx-search-width` 与两条媒体查询统一给。形态取自 document-reader 原先那版
   （胶囊外框 + 图标在流内 + 无"聚焦变宽"动画）。
10. 内嵌页 image-cleaner / pixiv-sync 自带工具栏与标题，与宿主 header 重复（双层横条）。
    `network-location.html` **不属于这一类**：它没有工具栏，是"内嵌提供方页"的正面样例
    （见 §3.4）；它的问题是另一处 —— 曾经的 `var(--bg, #17181c)` 未定义 token（P0-4 已修）
    与 19 处深色兜底字面量。
11. 侧栏宽度已统一（本轮）：4 个插件的写死值与 group-mesh 的 236px 覆盖全部删除，
    统一跟随 `--sub-sidebar-width`；≤860px 的窄屏覆写改为 0,2,0 后**首次真正生效**。
12. 键盘可达性缺失：image-viewer 全插件无键盘处理；自绘右键菜单不响应 Esc、
    不随 scroll/resize 重定位；image-viewer 内还有两套右键菜单（图片走壳的
    `createContextMenu`，相册走自绘 `.iv-context-menu`）。
13. `image-cleaner` 的后端设置项 `threshold` 已接上工具栏「⚙ 设置」（本轮）；设置项有 schema
    就必须有前端入口，门禁暂未自动校验这一点。

**P2（细节与规范）**

14. 批量操作位置：image-cleaner 固定底栏（好），image-viewer 塞工具栏（拥挤）。
15. 工具栏右侧靠 `style="margin-left:auto;"` 而不是类。
16. 遮罩黑度 4 档（0.45/0.55/0.62/0.65）未走 `--bg-overlay`。
17. 死代码：`--nr-cover-*` 3 个变量（document-reader.css:23-25）；`.psync-empty` 已随本轮
    空态统一删除。
18. `createCardGrid` 已删除（零采用，且它渲染的 `.manga-*` 在壳与任何插件样式里都没有定义，
    谁用谁拿到无样式 DOM）；`createSettingsForm`（0 直接采用，仅被 `openSettingsModal`
    间接使用）、`createPagination`（1 处）、`.obx-skeleton`（0）仍需决定推广还是废弃。
19. `netease-music` 的独立页（154 行）无人可达且与 media-player 的原生视图重复，
    其 `parent.mediaPlayerApp` 在壳直接加载时必然失败；`#content` 的 `class="empty"`
    已随本轮空态统一移除（改为内容区里的 `.empty-state`）。

### 9.3 各插件值得吸收的做法

> 逐插件详表见 `docs/_ui-audit/*.md` 第 6 节。以下是跨插件复用的候选：

| 做法 | 出处 | 解决的问题 |
| --- | --- | --- |
| 搜索框：胶囊外框 + 图标在流内 + 输入无边框，聚焦用 `:focus-within` | document-reader 的原 `.nr-search` | 不需要 30px 内边距给绝对定位的图标让位，也没有"聚焦变宽"导致的工具栏抖动；已成为壳的 `.search-field`，另外三个插件向它收敛 |
| 固定底栏承载批量操作与选中计数 | image-cleaner `index.html:30-34` | 批量操作不随内容滚走 |
| 扩展面板内嵌范式（header 标题 + 返回 / body iframe） | image-viewer `index.html:65-73` | 宿主与 Companion 插件解耦 |
| 幂等的轮询托管 | manga-library `app.js:34-40` | `onHide` 停 / `onShow` 起 / `onDispose` 兜底，不会重复起定时器 |
| 导航项"差异只在 token" | `base.css:290-347` 的落地方式 | 四个插件的选中态可逐像素一致（`tests/debug_nav_style_ui.py`） |
| 几何自检代替截图 | `tests/debug_group_mesh_ui.py`（24 条断言） | 结构归属、并排/堆叠、token 取色、深浅主题、交错延迟都能断言 |
| 高亮用 `::highlight()` 不铺底色 | document-reader `css:498-509` | 朗读高亮不破坏正文排版 |
| 短文档主动补满视口 | document-reader `reader-engine.js:175-189` | 内容不足一屏时滚动区不空荡 |
| 沉浸态走 `data-video-fullscreen` | media-player | 全屏联动比插件自己隐藏壳导航可靠 |
| 设置项全部走 schema | manga-library（`openSettingsModal({title})` 最薄调用） | 不写第二份字段列表，不产生双轨漂移 |
| token 用法正面样例 | netease-music 独立页（9 处 `var()` 全为真 token、0 hex） | 说明"只写 token"并不需要复杂工程 |

### 9.4 真实存在的陷阱（不要"顺手改"）

- `.view-sub-sidebar` 的 `width` / `flex-direction` 写在壳样式里，与插件规则同优先级，
  后注入的壳胜出 ⇒ 必须用 `.gm-side.view-sub-sidebar` 这种 0,2,0 选择器。
- 侧栏与主区分隔线两边都画 = 交角 2px。
- `.view-body` 上的 `position: relative` 是插件自己加的（document-reader.css:704-706，
  注释说明给在 `#app` 上会让面板盖住左栏），多个插件各写一遍；改动前先确认没有第二个插件依赖。
- `[hidden]` 在作者样式存在时可能不生效（group-mesh 因此改用内联 `display`，
  `remote.js:501-508`）。
- 元素级选择器（`.modal-body input`）会连壳渲染的控件一起改，壳改结构即失效。
- 面板纯显隐切换时，隐藏面板里的按钮点击/取值都会失败；自动化用例必须先切面板并
  校验切换生效。
- 入场动画（`obxFadeUp` + 交错延迟）会让 Selenium 的 `.text` 与"可交互"判定间歇性失败；
  用例要走 `innerText` 与 JS 点击，并等"渲染完成"而不是"元素存在"。
- 嵌套 iframe（宿主内嵌 Companion）里，高度链与滚动容器必须由**被内嵌页自己**补齐
  （pixiv-sync `index.html:9-11` 有注释），宿主不会替它传下去。
- `--color-mix()` 在不同插件页里的序列化形式不同（oklab / color(srgb)），
  跨插件做颜色断言要按像素比而不是字符串比（`tests/debug_nav_style_ui.py:44-60`）。

### 9.5 本轮 P0 修复记录（已实施）

| # | 改动 | 落点与验证 |
| --- | --- | --- |
| 1 | **pixiv-sync 换肤**：整个 `<style>` 块改为壳 token（颜色字面量 36 → 0），6 处内联提示改 `class="field-help"`，`#d33` 改 `.psync-danger` / `var(--danger)`，进度条渐变由 `--success` + `color-mix` 派生 | `plugins/pixiv-sync/frontend/index.html`；`#hex|rgba()` 计数 0，全仓未定义 token 扫描无命中 |
| 2 | **pixiv-sync 交互口径**：22 处 `alert()` → `Toast.error/success/warning/info`，长汇总用 `Toast.show(msg,'info',6000)` | 同文件；`alert(` 22 → 0，`Toast.` 0 → 22 |
| 3 | **pixiv-sync 弹窗**：自造 `.psync-modal`（z-index 999）→ 壳 `.modal` / `.modal-box` / `.modal-body` / `.modal-footer`；`style.display` 切换 → `classList.toggle('active')`；宽度用 `.modal-box.psync-oauth-box`（0,2,0）压过 base.css | 同文件；`base.js` 的 Toast 与 `base.css` 的 `.modal.active` 均已核对存在 |
| 4 | **pixiv-sync 生命周期**：两个常驻 `setInterval` 与 `tick()` 自续轮询改由 `PluginLifecycle` 托管（`onShow` 起 / `onHide` 停 / `onDispose` 收尾），无壳上下文时退回旧行为 | 同文件 `bindLifecycle()`；宿主 image-viewer 是 keepAlive，切走即停 |
| 5 | **media-player 生命周期**：新增 `_bindPluginLifecycle()`（`app.js:69`、`:90`）；`lyrics-parser` 增 `suspend()` / `resume()`，`show()` 在挂起时不启动频谱 rAF；`onDispose` 落一次进度并停定时器 | `plugins/media-player/frontend/js/{app.js,lyrics-parser.js}`；装载契约（82 成员）通过 |
| 6 | **未定义 token**：`image-viewer.css:359` `--text-danger` → `var(--danger)`；`manga-library.css:102` `--mp-glass` → `var(--bg-surface)`；`group-mesh/network-location.html` `var(--bg, #17181c)` → `var(--bg-app)`，并把写死的 `color-scheme: light dark` 改为跟随 `data-theme` | 三个文件；扫描后仍被标出的其余变量均为 JS 运行时 `setProperty` 写入（`--obx-i` / `--lyrics-*` / `--reader-*` / `--hero-bg` / `--i` / `--range-val`），属正常 |

**尚未实施**（建议下一步）：

- §10.3 的静态门禁（未定义 token、`alert(`、`!important`）还没进 `tools/check_plugins.py`；
  本轮只做了人工扫描，下一次仍可能重新写进 `--color-*` 这类名字。
- 缺一条"主题一致性"的真实渲染用例（切父页 `data-theme=dark` 后断言插件页取色变化）。
- `pixiv-sync` 的 `--color-*` 是整块重写的，视觉细节（间距/字号）建议在宿主面板里目视复核一次。

### 9.6 本轮 P1 修复记录（第一批 / 第二批已实施，第三批待做）

| 批 | 改动 | 落点与验证 |
| --- | --- | --- |
| 1 | **media-player 收敛**：`#media-content` 补 `.obx-scroll`、删除自带滚动条段、删除 8 段重复关键帧（保留插件专属的 `mpEq` / `mpAurora`）、13 处 `animation` 改指壳的关键帧名、7 个派生 token 改引 `--obx-*`；`effects.css` 新增 `--obx-glass-bg(-strong)` 作为 `.obx-glass` 的唯一来源 | `media-player.css` 2047 → 1940 行；`@keyframes mp` 只剩 2 个；grep 确认无 `mpShimmer` 之类残留 |
| 1 | **侧栏宽度统一**：iv/ml/mp 删除无效的写死值，窄屏覆写改为 `.xx-sidebar.view-sub-sidebar`(0,2,0) 并统一 200px；gm 的 `--gm-side-width` 改引 `--sub-sidebar-width` | grep 只剩三处 0,2,0 窄屏覆写；**行为变化**：≤860px 下侧栏 200px 首次真正生效 |
| 1 | **删除死组件 `createCardGrid`**：零采用，且它渲染的 `.manga-*` 在壳与任何插件样式里都没有定义 | `plugin-guide.md` §4.3 与本文件 §5/§9.2/§10 同步 |
| 2 | **壳空态升级**：`.empty-state` 改为四段结构（+ `.empty-state--inline`、`--obx-empty-min-h`）；`effects.css` 的 reduced-motion 白名单补上 `.empty-state-icon` | `base.css`「通用状态」一节；壳内既有使用者（`base.js` 的设置弹窗）无需改动 |
| 2 | **7 个插件迁移**到壳类并删除各自实现：`.iv-empty*`、`.nr-empty*`、`.ml-empty*`、`.mp-empty-state*`、`.cleaner-empty`、`.empty`（netease）、`.nl-empty`、`.psync-empty`（死类） | 全仓 `class="empty-state"` 约 57 处；`grep '(iv\|nr\|ml\|mp\|cleaner\|psync\|nl)-empty'` 只剩注释与 `.nr-empty-span` |
| 3 | **内嵌页信号**：image-viewer 的扩展面板与 `folder-picker.js` 的提供方 iframe 在 URL 上追加 `?embed=1`；image-cleaner / pixiv-sync 在 `<head>` 里据此打上 `html.is-embedded`，把工具栏降级为普通操作行并隐藏与宿主重复的标题 | `app.js` 的 `_embedUrl()`、`folder-picker.js:207-215`、两个页面的 `<head>` 与 CSS（0,2,1 选择器） |
| 4 | **静态门禁落地**：`tools/check_plugins.py` 新增前端 UI 契约检查（未定义变量与原生 `alert`/`confirm` 为 error，`!important` 与重复/越权关键帧为 warning），并在 `tests/test_plugin_spec.py` 补 `FrontendUiContractTests`（8 例） | 全仓运行：0 error、46 warning（40 基线 + 6 处 `!important`）；实装当天抓到 image-viewer 的 `obxRebuildSlide` 越权前缀，已改名 `iv-rebuild-slide` |
| 5 | **设置入口与搜索框收敛**：pixiv-sync 删除手写设置表单（字段/默认值/范围钳制/保存逻辑），改走 `openSettingsModal`，后端 `download_dir` 由 `text` 改为 `directory`；image-cleaner 补上工具栏「⚙ 设置」（此前 `threshold` 无入口）；搜索框按 document-reader 的构造（胶囊外框 + 图标在流内 + 无聚焦变宽）做成壳的 `.search-field` 唯一实现，四个插件标记同构、插件侧样式删净，宽度走 `--obx-search-width`、窄窗口在壳里统一收窄；壳新增 `.empty-state--error`，用于扫描失败与歌单加载失败 | pixiv-sync 前端 521 → 441 行；插件侧共删约 130 行搜索样式（4 套类名 / 7 档宽度）；`grep` 确认无 `loadSettings/saveSettings/intSetting/set-token/btn-save` 与 `*-search` 规则残留；检查器 0 error |

**保留的插件专属"空态"（不是漏改）**：

- `.gm-empty`（group-mesh，8 处）：卡片内的**说明段**，可能带 `<strong>` 与后续按钮，左对齐，
  不是居中空态 —— 迁到 `.empty-state` 会把说明文字居中并改变卡片布局。
- `.mp-playlist-empty` / `.mp-stage-empty`（media-player）：前者是侧栏里的 12px 小提示，
  后者是覆盖在封面上的舞台提示（配色跟封面走，用 `--text-secondary` 会失去对比度）。
- `.nr-empty-span`（document-reader）：只负责网格里的 `grid-column: 1 / -1`。

---

## 10 落地路径

### 10.1 先补壳侧的缺件（小改动、覆盖面最大）

| 缺件 | 建议 |
| --- | --- |
| 工具栏右侧分组 | 加 `.toolbar-group.right { margin-left: auto; }`，插件去掉内联 `style` |
| 进度条 | 提取 `.obx-progress` + `.obx-progress-bar`（现在只有 pixiv-sync 有，且是私有类） |
| 卡片/网格 | 已定为"插件自建"（`createCardGrid` 删除，见 §9.5）；若以后要共享，先定义 `.obx-card*` 类与样式再推广 |
| 空/加载/错误态 | 空态与加载态已落地（`.empty-state` 四段结构 + `.loading`，见 §9.6）；仍缺 `.error-state` + 重试按钮样式 |
| 侧栏宽度策略 | 已定：全部跟随 `--sub-sidebar-width`（本轮完成）。是否把它也做成用户可调（现在只暴露 `--nav-width`）仍待决定 |
| 浮层层级与遮罩 | 在 base.css 注释里写死层级约定（modal 1500 / toast 3000），并提供 `.obx-overlay` 使用 `--bg-overlay` |
| 高度链 | 在 base.css 或文档里给一条明确写法（`#app { height: 100vh }` 或 `html,body{height:100%}`），避免第三个变体 |
| token 关系说明 | 在 `variables.css` / `effects.css` 注释里写清 `--radius*` 与 `--obx-radius*` 的分工，以及"哪些 token 用户可调" |
| 改壳侧共享资源后必须重新构建 | `npm --prefix shell/frontend run build`。`/shell/*` 的路由**优先发 `dist/shell/`**（`file_server.py:762-769`），不构建时 `public/shell/*` 的改动在界面上完全不生效 —— 曾因此把"新标记 + 旧样式"误判成 CSS 写错（搜索框胶囊外框消失） |

### 10.2 按分级改插件

1. **P0（已完成，见 §9.5）**
   - `pixiv-sync`：`--color-*` → 真实 token、22 处 `alert` → `Toast`、
     `.psync-modal` → `.modal`、补生命周期钩子管住两个 `setInterval`。
   - `media-player`：补 `onHide`/`onShow`（停歌词频谱 rAF，保留播放与进度保存）。
   - `image-viewer` / `manga-library` / `group-mesh`：三个未定义 token 修正。
2. **P1**
   - 已完成（第一批）：`--<prefix>-radius` / `-shadow` / `-glass` / 重复关键帧收敛到
     `--obx-*` 与壳的关键帧名（media-player）；侧栏宽度统一；删除死组件 `createCardGrid`。
   - 已完成（第二批）：空态统一到 `.empty-state`（7 个插件 57 处），并给壳的
     reduced-motion 白名单补上 `.empty-state-icon`。
   - 已完成（第三批）：内嵌页 `?embed=1` 信号（宿主两处追加参数 + image-cleaner /
     pixiv-sync 把工具栏降级为普通行）。
   - 已完成（第五批）：pixiv-sync 设置改走 `openSettingsModal`（`download_dir` 改为
     `type: "directory"`）、image-cleaner 补设置入口、搜索框统一到 `.search-field`、
     新增 `.empty-state--error`。
   - 待做：document-reader 的两套自建设置面板是否收敛（需要就地预览，暂缓）；
     image-viewer 的 `.iv-field`/`.iv-setting-item` 是否改用壳的 `.field`。
3. **P2**：批量操作向固定底栏收敛；工具栏右侧用类；清理死变量；
   决定其余共享组件（`createSettingsForm` / `createPagination`）的去留（`createCardGrid` 已删除）；
   骨架屏试点；media-player 的 modal / 右键菜单结构替换（风险最高，建议单独立项）。

### 10.3 加可验证项（否则一定会退回去）

- **静态（已实装，见 `tools/check_plugins.py`）**：
  - `var(--<不存在的名字>` → **error**。允许集合 = 壳样式里声明的变量（`variables.css` /
    `base.css` / `effects.css` / `folder-picker.css` / `src/styles/shell.css`）+ 插件自己声明的
    + 插件 JS `setProperty('--x', …)` 写入的 + `FRAMEWORK_SET_VARS`（壳脚本写入、插件只读，
    目前只有 `--obx-i`）。**每加一个框架变量都要在登记表里写清"谁写的"**，否则等于把规则关掉。
    同一文件里同一个名字只报一条，但会带上总处数（27 处 `--color-text` 不会刷屏）。
  - `alert(` / `confirm(` → **error**（`Toast.*` / `confirmDialog()` 不受影响；注释里的示例代码
    不算——检查前会剥掉块注释与 `//` 行注释）。
  - `!important` → **warning**（当前 6 处：document-reader 3 处 `user-select`/`padding`，
    media-player 3 处 reduced-motion 兜底）。
  - 与壳逐值相同的 `@keyframes` → **warning**；插件用壳保留的 `obx-` 前缀命名自己的关键帧
    也 → **warning**（这条实装当天就抓到 image-viewer 的 `obxRebuildSlide`，已改名
    `iv-rebuild-slide`）。
  - 用例：`tests/test_plugin_spec.py` 的 `FrontendUiContractTests`（拦住 + 不误报两侧都有）。
- **渲染**：
  - 把 `tests/debug_nav_style_ui.py` 的 `TARGETS` 从 4 个插件扩到全部带侧栏的插件
    （目前缺 document-reader）；
  - 把 `tests/debug_group_mesh_ui.py` 的结构断言抽成通用 `tests/ui_contract.py`，
    逐插件跑：侧栏归属 / 工具栏高度 = `--toolbar-height` / token 取色 /
    空态存在 / 深浅主题对比 / 无 `alert` 弹窗；
  - 加一条"主题一致性"用例：把父页 `data-theme` 切到 dark，断言插件页面
    `--bg-surface` 与 `body` 计算背景同步变化（能抓住 pixiv-sync / netease 这类失效）。
- **交互**：内嵌页"只有一层横条"用一条 DOM 断言守住（宿主 header 存在时，
  内嵌页不得出现 `.view-toolbar`）。

### 10.4 文档同步

- `docs/plugin-guide.md` §4 增加一句"骨架与词汇见 `docs/plugin-ui-guide.md`"，
  §4.1 的布局类表补上 `.obx-extension*` / `.obx-embed-frame` / `.obx-scroll` / `.obx-anim-*` 的强制要求。
- 修 `plugin-guide.md` §4.4 里与 media-player 实现不符的那句（生命周期钩子）。
- 把本文 §4.2 的"用户可调 token"表复制进 `variables.css` 的注释，改动的人才会先看到。

### 10.5 明确不建议做的（避免过度统一）

- 不要给插件引入前端框架（Vue/React）：插件是 iframe 内的静态页，壳的注入契约已经把
  组件层给足了；引入框架会把"改一行 CSS"变成"改构建"。
- 不要统一各插件内容区的布局（栅格/瀑布流/舞台/阅读器各有语义）。
- 不要把插件设置搬进壳设置页：插件设置需要插件的上下文（目录选择、预览、schema 之外
  的动态项），现状"设置就地 + schema 驱动弹窗"是刻意选择。
- 不要为了统一而重写已经稳定的插件逻辑（`media-player.css` 44KB 里大量是播放器专属样式）；
  media-player 的重复造壳要**逐块替换**（先 `.obx-scroll` 与关键帧，再弹窗/菜单），
  一次全改会把播放器的视觉回归风险放得很大。
