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
- 内容页用 `cubby:chrome` 上报 `title` / `subtitle` / `view` / `actions` / `nav` / `search` / `status`；
  壳只合并，不解释业务。
- `settings` 动作由壳接管（声明 `frontend.chrome` 且 schema 可表达的插件）；内容页不要重新实现设置弹窗，
  保存后处理 `settings-saved` 即可。
- 搜索由壳持有输入框。内容页切视图、进二级页时发 `search.clear`（递增序号）复位；需要程序化回填时发
  `search.value`，不要另建搜索框。
- native 内容用 `cubby:schema` 提交 blocks；动作只回传标识，payload 发送前做 JSON 克隆。

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
- 后三条由壳统一提供，插件不必自己实现：`dialogs.js` 监听 `.modal` 元素的 `active` 类变化，
  打开时补 `role="dialog"` / `aria-modal="true"` / `aria-labelledby`（取 `.modal-box h3` 的文本）、
  记住并移入焦点，关闭时撤掉这些属性并把焦点还给打开前持有焦点的元素；Esc、Tab 循环、
  点遮罩关闭都在同一处处理。因此**不要**再给 `.modal` 手写 `role="dialog"`——带了该属性的元素
  视为"自己管无障碍"，壳会跳过它。关闭时模态元素上会派发 `modal:close`（可冒泡），
  插件需要清理自己的状态时监听它；也可以用 `Shell.dialog.open(el)` / `Shell.dialog.close(el)` 直接开关。
- 用 `.mp-*` 这类自建弹窗骨架的插件拿不到上述行为：要么改用 `.modal` 骨架，要么自己补齐
  `role="dialog"` / `aria-modal` / Esc / 焦点归还。
- 四个壳内设置页与 image-viewer 的内容设置页顶部都有一个「外观」块：背景不透明度滑块 +
  默认勾选的「跟随壳透明度」。它属于壳偏好 `--plugin-glass-<插件名>`，不写进插件
  `settings_schema`；image-viewer 的内容页设置通过 `cubby:get-plugin-appearance` /
  `cubby:set-plugin-alpha` 使用同一份值。只覆盖页面骨架与内容页公共区域，插件自绘的
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

- 内容页首次加载先发 `cubby:ready`，再发一次 `cubby:chrome`；壳收到 ready 后会补发当前视图。
- 视图切换、搜索、动作处理必须幂等；壳可能在路由恢复时重发同一视图。
- `PluginLifecycle.onHide` 停轮询/停动画/暂停媒体；`onShow` 恢复；`onDispose` 取消未完成请求与定时器。
- keepAlive 插件离开页面不销毁 iframe：不要假设 `onDispose` 会随路由切换触发。
- 长任务要有进度、取消与失败重试；页面隐藏后不要继续高频轮询。

## Native 内容

- 只提交结构化 blocks：`text` / `heading` / `stats` / `section` / `banner` / `code` / `confirm` / `kv` /
  `table` / `list` / `cards` / `checkbox-list` / `form` / `actions` / `progress` / `empty` / `image-grid`。
  字段定义在 `shell/frontend/src/core/plugin-content.ts`，`SchemaRenderer` 与 `NativePanel` 是唯一渲染方。

### 反馈分三档，按“用户要不要做决定”选，不按“是不是出错”选

| 档位 | 用法 | 实现 |
| --- | --- | --- |
| **Toast**（瞬时、不阻断、不占版面） | **成功**与中性结果：已请求取消、已清除、已打开目录 | `render(blocks, toolbar, { toast: { text, tone } })` 的消息级字段；壳与插件页共用 `/shell/toast.js` 这一份实现（DOM 结构、`.toast*` 样式与同一段文本的去重都在那里） |
| **Banner**（就地、不阻断） | 持续为真的状态与**所有失败**：限流冷却、状态读取失败、操作失败、可重来的二次确认 | `banner` 块 + `actions` 块；失败横幅给一个「知道了」`action` 让用户显式关闭 |
| **Confirm**（阻断） | 不可逆且破坏用户数据：清除凭据、删除文件、覆盖 | `confirm` 块，由 `NativePanel` 画成模态层 |

判据：**不可逆 + 破坏用户数据**才用 `confirm`；派生数据（可重算、可重来）用就地确认；状态与失败用
`banner`；只有成功与中性结果用 `toast`。**不要用 Toast 报错**——它几秒后自己消失，用户来不及读，
也没有留下痕迹；失败必须常驻到用户关闭或下一次成功。

- `confirm` 的契约：`{ type: 'confirm', title, text, tone, confirm, cancel }`；壳负责遮罩、Esc 与点遮罩
  等于取消、焦点进入弹窗并关闭后归还、Tab 不跑出弹窗。**遮罩画在 `.native-panel` 内部而不是 body**：
  面板被隐藏或卸载时模态随之消失，不会留下孤儿弹窗。弹窗开关由 blocks 派生——插件每次提交都带着
  这个块，确认后去掉它；不要另发“打开/关闭弹窗”的消息。
- `toast` 不是块：它不占 blocks，也不会留在页面上等下一次轮询覆盖。逻辑页不渲染可见 Toast（它是
  隐藏 iframe），所以只能交给壳。
- content 页自己要弹提示时，用壳注入的 `window.Toast`（唯一实现是 `/shell/toast.js`，壳页面与
  插件 iframe 引的是同一份）：它带 `role="status"` / `aria-live="polite"`、同文本去重与悬停暂停。
  不要另拼 `.toast` 节点或自绘一套提示，否则两份实现的样式与行为会各自漂移。
- 版式：`client.render(blocks, toolbar, { layout: 'document' })` 把正文收窄到 960px 居中、组间放到 24px，
  适合以文字与指标为主的页面；不写 `layout` 时维持既有的整宽排版与 14px 间距，旧插件不受影响。
- 层级优先用块与间距表达，不要用字号和颜色硬堆：一组同级数字用 `stats`，一块内容用 `section`，
  一条提示用 `banner`；`text` 只写句子。
- `stats` 的 `metrics` 里 `value` 由插件格式化（千分位、单位、百分号），壳只负责排版与 `<dl>` 语义。
- `section` 支持嵌套 blocks，只做静态分组，没有折叠态。
- `banner` 的 `tone` 取 `default` / `muted` / `accent` / `success` / `warning` / `danger`，
  自带 `role="status"` 与 `aria-live="polite"`；限流、失败、冷却这类反馈用它，不要写成红字正文。
- `code` 默认带复制按钮（`copyable: false` 关掉），复制成功后就地显示「已复制」。
- `actions` 的 `variant` 有 `default` / `primary` / `accent` / `danger` / `link`：
  `primary` 是页面唯一主按钮；两个同权动作并列时用 `accent`（强调但非实心，不制造主次暗示）；
  第三级入口（重试、清空）用 `link`；`size: 'sm'` 压到小号档。
- `text` 可带 `dot`（语义色圆点，取值同 `tone`）表示一行状态（已连接 / 未配置 / 冷却中）；
  圆点只是辅助，文字必须自己说清状态。
- `progress` 在总数未知时传 `indeterminate: true`（不要用 0/0 假装百分比）；`detail` 放计数。
- `empty` 可带 `icon`（如 `icon:key-round`），图标走 sprite，缺 `Icon.vue` 的尺寸规则由
  `native.css` 补齐。
- 图片缩略图可点击预览；预览用的是插件给的 `image` URL，需要原图时在 schema 里补字段，不要读宿主 DOM。
- 表单的 `directory` 字段用壳的目录选择；用户取消不报错，失败就地提示。
- 表单字段的 `section` / `sectionTitle` / `sectionDescription` / `sectionOrder` 与 `order` 由
  壳弹窗和 native `form` 共同消费（两组渲染现在都按同一规则分组）。
- 不在 native 逻辑页里渲染可见弹窗/Toast；确认与提示用 blocks 与消息级 `toast` 表达。
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
