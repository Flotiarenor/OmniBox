# 插件 UI 规范

> 页面骨架由壳渲染，插件只写内容；需要复杂自绘的页面保留 content iframe，文字/表单/列表类附属页面用 native 面板。

## 职责边界

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| 壳 `PluginChrome` | 品牌块、导航、顶栏动作、搜索框、状态栏、侧栏分隔条、freshness | 插件业务视图、内容滚动 |
| 壳 `PluginSettingsModal` | 页面级设置弹窗（schema 驱动的四个侧栏插件） | 按目录作用域的 image-viewer 设置（仍在内容页） |
| 壳 `PluginExtensions` | 扩展入口；native 入口打开面板，view 型入口下发视图 | 宿主自己拼扩展侧栏 |
| 壳 `NativePanel` / `SchemaRenderer` | native 内容的遮罩、表单、列表、预览 | 任意 HTML、宿主 DOM |
| content iframe | 相册网格、阅读器、播放舞台、歌词、灯箱等内容 | 品牌、侧栏、顶栏、搜索、状态栏 |

manifest 用 `frontend.chrome` 声明骨架；壳加载内容页时追加 `?content=1`。content iframe 里不得出现
`.view-sub-sidebar`、`.view-toolbar`、`.sub-sidebar-brand`、`.obx-nav-item`；这些节点的宿主是壳。

## 页面契约

完整字段与消息表在 `docs/plugin-page-architecture.md`；这里只列 UI 侧必须遵守的部分。

- 导航项 id、顶栏动作 id、视图 id 由插件定义，壳只透传；复合 id（如 `downloads:all`）由内容页解释。
- 内容页用 `omnibox:chrome` 上报 `title` / `subtitle` / `view` / `actions` / `nav` / `search` / `status`；
  壳只合并，不解释业务。
- `settings` 动作由壳接管（声明 `frontend.chrome` 且 schema 可表达的插件）；内容页不要重新实现设置弹窗，
  保存后处理 `settings-saved` 即可。
- 搜索由壳持有输入框。内容页切视图、进二级页时发 `search.clear`（递增序号）复位；需要程序化回填时发
  `search.value`，不要另建搜索框。
- native 内容用 `omnibox:schema` 提交 blocks；动作只回传标识，payload 发送前做 JSON 克隆。

## 组件规范

### 按钮

- 主操作 `.btn.btn-primary`；普通 `.btn`；危险 `.btn.btn-danger`；小号 `.btn.btn-sm`。
- 顶栏动作不要自己写：在 manifest 的 `toolbar` 声明 `id` / `label` / `icon` / `variant` / `kind`，
  运行期用 `actions` 补 `visible` / `disabled` / `active` / `label`。
- 图标按钮必须有 `aria-label`；纯图标按钮不要只靠颜色表达状态。

### 弹窗

- 内容页自己的弹窗用 `.modal` / `.modal-box` / `.modal-body` / `.modal-footer`，遮罩只覆盖内容 iframe，
  **不要**假装它能盖住壳侧栏。
- 页面级设置、跨插件确认、需要盖住导航的弹窗，走壳的 `PluginSettingsModal` 或 native 面板。
- Esc、点遮罩、点取消都应关闭；打开时焦点进入弹窗，关闭后还给触发元素。
- 四个壳内设置页与 image-viewer 的内容设置页顶部都有一个「外观」块：背景不透明度滑块 +
  默认勾选的「跟随壳透明度」。它属于壳偏好 `--plugin-glass-<插件名>`，不写进插件
  `settings_schema`；image-viewer 的内容页设置通过 `omnibox:get-plugin-appearance` /
  `omnibox:set-plugin-alpha` 使用同一份值。只覆盖页面骨架与内容页公共区域，插件自绘的
  卡片仍按各自样式绘制。

### 状态与空态

- 统一用 `.empty-state` + `.empty-state-icon` / `.empty-state-text` / `.empty-state-hint`；小容器加
  `.empty-state--inline`。不要自建 `.xx-empty`。
- 加载态用 `.obx-anim-spin` 或骨架 `.obx-skeleton`；错误态给出可重试的动作，不要只留一行红字。
- 列表为空、搜索无结果、扩展未配置是三种不同空态，文案要区分。

### 图标、滚动与动效

- 图标走 `Icons.html(name)`，颜色继承 `currentColor`；不要内联 SVG 路径。
- 每个会滚动的容器加 `.obx-scroll`；弹窗正文已由壳样式覆盖。
- 入场/常驻动画用 `.obx-anim-*`；交错列表用 `.obx-stagger` + `--obx-i`。所有动画都必须尊重
  `prefers-reduced-motion`，壳的 `effects.css` 已处理，插件自定义动画要自己处理。
- 分隔条：侧栏/内容之间由壳持有；内容内部（如媒体舞台高度）用 `data-obx-splitter*` 声明，
  由 `splitter.js` 接管，不要手写拖拽。

### 表格、卡片与表单

- 卡片圆角、阴影、边框使用壳 token；不要为单个插件写一套新的卡片语言。
- 表格只用于真正需要列对齐的数据；列表行优先，窄屏不要横向滚动。
- 表单字段沿用设置 schema 的类型；native 表单的 `directory` 用壳的目录选择按钮，不要只放只读路径。
- 数字/范围字段显示当前值；必填项有可见标记；错误就地显示在字段下方。

## Token 与主题

- 颜色、背景、边框、文字、圆角、过渡、阴影优先用壳变量：`--accent`、`--bg-app`、`--bg-surface`、
  `--bg-hover`、`--border`、`--text-primary`、`--text-secondary`、`--text-muted`、`--danger`、
  `--success`、`--warning`、`--transition-fast`、`--obx-radius`、`--obx-shadow-1/2`。
- 插件自有 token 用插件前缀（如 `--ml-`、`--mp-`、`--iv-`），不要覆盖壳变量。
- 允许写死的只有插件品牌色/封面渐变这类语义色；文字与交互色必须走 token。
- 用户可调圆角、动效、强调色与侧栏宽度；写死这些值等于让设置失效。

## CSS 组织

- 所有选择器挂在插件内容根节点下（如 `#app` / `#iv-content`），不要写全局 `button`、`div`、`*` 重置。
- 不覆盖壳类：`.view-body`、`.plugin-page`、`.plugin-nav-item`、`.obx-nav-item` 等只使用，不改写。
- 不使用 `!important`；确需提高优先级时增加父级选择器。
- 不用固定 `height` 撑内容；内容区用 `flex` + `min-height: 0`，滚动交给 `.obx-scroll`。
- 窄屏用容器查询或 `@media` 收敛列数；不要让侧栏在内容 iframe 里“复活”。
- 动态样式只写 CSS 变量（`--i`、`--range-val`、`--hero-bg`），不要拼大段内联 `style`。

## 状态与生命周期

- 内容页首次加载先发 `omnibox:ready`，再发一次 `omnibox:chrome`；壳收到 ready 后会补发当前视图。
- 视图切换、搜索、动作处理必须幂等；壳可能在路由恢复时重发同一视图。
- `PluginLifecycle.onHide` 停轮询/停动画/暂停媒体；`onShow` 恢复；`onDispose` 取消未完成请求与定时器。
- keepAlive 插件离开页面不销毁 iframe：不要假设 `onDispose` 会随路由切换触发。
- 长任务要有进度、取消与失败重试；页面隐藏后不要继续高频轮询。

## Native 内容

- 只提交结构化 blocks：`text` / `heading` / `kv` / `table` / `list` / `cards` / `checkbox-list` /
  `form` / `actions` / `progress` / `empty` / `image-grid`。
- 图片缩略图可点击预览；预览用的是插件给的 `image` URL，需要原图时在 schema 里补字段，不要读宿主 DOM。
- 表单的 `directory` 字段用壳的目录选择；用户取消不报错，失败就地提示。
- 不在 native 逻辑页里渲染可见弹窗/Toast；确认与提示用 blocks 表达，或走 `actions` 两步确认。
- 动作按钮的 `visible` / `disabled` 由逻辑页每次提交 blocks 时给出，壳不推断业务。

## 可访问性与键盘

- 所有可点击元素用 `button` / `a` / `input`，不要用 `div` 加 click。
- 图标按钮有 `aria-label`；开关有 `aria-pressed`；进度有 `role="progressbar"` 与 `aria-valuenow`。
- 弹窗 `role="dialog"` + `aria-modal="true"`；打开时焦点进入，Esc 关闭，关闭后焦点归还。
- 键盘可达：Tab 顺序与视觉顺序一致；不要在 `keydown` 里吞掉 Esc/Tab。
- 对比度至少 4.5:1（正文）与 3:1（大字/图标）；不要只用颜色区分选中/错误。

## 验收

```bash
npm --prefix shell/frontend run build
venv/Scripts/python.exe tools/check_plugins.py
venv/Scripts/python.exe tools/check_docs.py
venv/Scripts/python.exe tools/plugin_ui_audit.py
```

- `check_plugins.py` 静态检查 manifest、CSS 变量、`!important`、原生 `alert/confirm` 等契约。
- `plugin_ui_audit.py` 起真实壳逐插件检查：壳 chrome、内容页无 chrome 标记、导航/标题同步、
  分隔条 hover、freshness 与控制台错误；不截图，断言来自 DOM/计算样式/rect。
- 页面级设置、搜索回填、EQ 与 native 预览目前用一次性 Selenium 探针验收；后续应纳入审计工具。

## 禁止清单

- 在 content iframe 里重建品牌块、侧栏、顶栏、搜索框、状态栏、侧栏分隔条。
- 从插件 iframe 读取 `parent.*` 宿主对象或宿主 DOM 节点。
- 用固定像素宽度/高度硬撑壳骨架；用 `!important` 覆盖壳样式。
- 在 native 逻辑页里 `alert` / `confirm` / 自绘 Toast 当作可见反馈。
- 用颜色字面量替代 token，或覆盖用户可调的圆角/动效/侧栏宽度。
