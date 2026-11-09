# shell/frontend

Vue 3 + TypeScript + Vite。`src/` 是壳的界面（`components/` `core/` `router/` `styles/` `views/`、
`App.vue`、`main.ts`），`public/shell/` 是注入给插件的共享资源。
根 [AGENTS.md](../../AGENTS.md) 的全局约定仍然适用。

## 共享资源有两份副本

`public/shell/*` 会被 Vite 复制进 `dist/shell/`。

- `/shell/<file>` 优先发 `dist/shell/`，dist 中不存在时回落 `public/shell/`（`serve_shell_assets`）。
- **改完 `public/shell/` 下任何文件必须重新构建**：`npm --prefix shell/frontend run build`。只改源文件会继续发旧副本，症状是"新标记 + 旧样式"，容易被误判为 CSS 写错。
- 壳首页（`/`、`/settings`、插件路由）由 `dist/index.html` 提供，dist 缺失时直接 404；CI 在跑测试前会先构建一次。
- `dist/` 不提交（`.gitignore`）。

`public/shell/` 注入给插件的资源按用途分四类：

| 类别 | 文件 |
| --- | --- |
| 样式 | `variables.css` `shared.css` `base.css` `effects.css` |
| 组件 | `folder-picker.{css,js}` `freshness.{css,js}` `splitter.{css,js}` `content-ui.{css,js}` |
| 共享运行时 | `base.js`（`PluginLifecycle` / `Bridge` / `Utils`）、`dialogs.js`（`confirmDialog`、`Shell.dialog`）、`settings.js`（`createSettingsForm`、`openModal`、`openSettingsModal`）、`host-channel.js`（`HostChannel`）、`tree.js`（`createTree`）、`lightbox.js`（`createLightbox`）、`pagination.js`（`createPagination`）、`context-menu.js`（`createContextMenu`）、`chrome-client.js`（`ChromeClient`） |
| 脚本 | `toast.js` `native-client.js` `motion.js` `icons.generated.js` |

共享运行时的 9 个脚本是**同一个全局作用域里的普通脚本，顺序即依赖顺序**：`base.js` 在它们最前（其余分段按全局名读 `window.Utils` / `window.Bridge`），`toast.js` 又必须排在 `base.js` 之前，`chrome-client.js` 在 `context-menu.js` 之后、`folder-picker.js` 之前（它只用 `window` / `location`，不依赖 `base.js`）。注入顺序的唯一真源是 `shell/backend/file_server.py` 的 `_PLUGIN_BOOTSTRAP_SCRIPT`；新增或调整分段只改那里，不要另抄一份清单。

`freshness.js` 的触发归属是硬契约：`mount` 只渲染控件、不扫盘；内容页自绘控件时自己调
`.autoSync(scope)`（拿不到句柄时用模块级 `Freshness.sync(plugin, scope)`），壳渲染骨架时由
`PluginChrome.vue` 的 `freshnessSyncTarget` 在挂载后与 `watch([activeView, freshnessScope])`
里调，native 面板打开期间跳过（内容 iframe 被 v-show 隐藏）。插件门禁
`tools/check_plugins.py` 按插件分流：自绘型查插件自己的 `.autoSync(` / `Freshness.sync(`，
壳挂载型查 `PluginChrome.vue` 的 `.autoSync(` 与那条 watch；匹配前先去掉 JS 注释、HTML
注释与字符串内容，注释掉的调用、只定义未调用、模板字符串都不算接线。

同步/校验有结论时壳发两条消息：`cubby:freshness`（`{plugin, scope, report}`）与紧跟其后的
旧 `cubby:action: refresh`（`payload` 同报告）。内容页只处理一条即可；两条都处理的页面
按 `payload` 判回声（带 payload 的 `refresh` 跳过）。`App.vue` 用 `buildActionEvent` 转发
`@action` 的全部 emit 参数，`refresh` 才真的带 payload；事件作用域取
`freshnessEventScope(report, fallback)`（报告自己的 `scope` 优先）。`normalizeFreshnessScope`
与后端 `scope_start` 的口径对齐：去首尾 `/`、折叠重复 `/`、丢掉 `.` 段，非法值落成空串（不
保留旧值；后端仍会自己再判一次）。控件在没有任务时每 30 秒重读一次状态投影（`idlePollMs`
可调，0 关闭）；窗口隐藏或控件所在页被 v-show 隐藏（`offsetParent` 为 null）时不读，切回来
靠低频轮询自己恢复。被动同步被跳过（去抖 / 正忙 / 作用域无效 / 远端新鲜期内）时状态行在
已有文案之后补一句原因，有不可达根时显示「部分目录不可达（N）· 挂载后自动同步」。壳设置页
的「数据与缓存」用 `system_freshness_overview` 列出各插件状态并提供重建入口；有任务在跑时
1.5 秒轮询概览、离开设置页即停，`freshnessOverviewErrorText` 把 403 / 权限错误与"旧内核不
提供"分开表述；网络抖动、后端重启这类瞬时失败按可重试处理（保留已有概览、有限次退避重试、
行内给重试按钮），不清空表格也不停轮询。

`toast.js` 是壳页面与插件 iframe 共用的 Toast 唯一实现：两边的引导片段都引它，
`src/core/toast.ts` 只是给它加类型入口，`base.js` 不再自己定义 `window.Toast`。

`.modal` 骨架（`.modal` / `.modal-box` / `.modal-body` / `.modal-footer`）在 `shared.css`：
壳页面不加载 `base.css`，模态骨架只能放在两边都加载的那一份里。`dialogs.js` 的
`Shell.dialog` 为插件页的 `.modal` 补无障碍与键盘行为（`role="dialog"` / `aria-modal="true"`、
焦点进入与归还、Esc、Tab 循环、点遮罩关闭、`modal:close` 事件），壳文档自己的弹窗
（`PluginSettingsModal`、folder-picker）由 `src/core/use-dialog-focus.ts` 与组件自己负责。

## 与 `res/` 的区别

`res/` 不经 Vite，只有一份，改完立即生效、不需要构建（`serve_res_assets`）。
图标 sprite 放在 `res/icons/icons.svg`，由 `tools/build_icons.py` 生成。

## 门禁

```bash
node tools/check_frontend_escape.cjs      # 属性注入的转义一致性
python tools/check_npm_audit.py           # 依赖漏洞：脚本内部固定官方源（本机 registry 不实现 audit 端点）
npm --prefix shell/frontend run build     # vue-tsc --noEmit + vite build
```

前端纯逻辑用例在 `tests/js/*.mjs`，由 `tests/test_*_js.py` 用 node 驱动，见 [tests/AGENTS.md](../../tests/AGENTS.md)。
