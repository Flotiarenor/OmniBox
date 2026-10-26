# plugins

每个插件是 `plugins/<name>/` 一个独立目录，由壳在运行时加载。
根 [AGENTS.md](../AGENTS.md) 的全局约定仍然适用。

## 结构

```
plugins/<name>/
├── manifest.json      # 唯一清单
├── backend/           # Python，入口类由 manifest.backend 指定
└── frontend/          # 静态页面，入口由 manifest.frontend 指定
```

少数插件另有目录：`media-player/tool/`、`media-player/eq-presets/`、`document-reader/tool/`。

现有插件：`document-reader` `group-mesh` `image-cleaner` `image-viewer` `manga-library` `media-player` `netease-music` `pixiv-sync`。

## manifest.json

字段：`name` `version` `displayName` `icon` `description` `dependencies` `keepAlive` `permissions` `backend.{entry,class}` `frontend.{entry,route,chrome}` `minShellVersion`（实例：`plugins/media-player/manifest.json`）。

`frontend.chrome` 声明壳渲染的页面骨架：`brand`、`nav`、`sidebarOrder`、`toolbar`、`search`、`status`、`freshness`、
`extensions`。`sidebarOrder` 用 `nav:<组id>` 与 `extensions` 指定侧栏 section 顺序；缺省按 nav
数组序、扩展收尾。声明后壳的 `PluginChrome.vue` 渲染品牌/导航/顶栏/搜索/状态/分隔条，插件
iframe 以 `?content=1` 只装内容（插件页禁止 `.view-sub-sidebar`、`.view-toolbar`、
`.sub-sidebar-brand`、`.obx-nav-item`）。旧的 `content` 字段（内容归属枚举 canvas / native / hybrid）
已移除，不再有运行时效果：内容形态由"是否声明 `frontend.chrome`"与 `get_extensions()` 的 `native` 声明决定。
附属插件不写 `frontend.chrome`，而由 `get_extensions()` 返回 `native: {url, title}` 让壳的
`NativePanel` 渲染，`embedUrl` 只给旧壳回落。

**每个字段都必须有读取方**：`tools/check_plugins.py` 用 `RUNTIME_FIELD_READERS` 校验，运行时确实无效果的字段要显式登记。

## 页面归属

| 归属 | 谁渲染 | 插件侧做什么 |
| --- | --- | --- |
| 品牌 / 导航 / 顶栏 / 搜索 / 状态 / 分隔条 | 壳的 `PluginChrome.vue` | 在 `frontend.chrome` 声明；运行期用 `omnibox:chrome` 上报标题、视图、徽标、动作、搜索词（回填与清空）与状态 |
| 视图切换 / 顶栏动作 / 搜索输入 | 壳 | 监听 `omnibox:view`、`omnibox:action`、`omnibox:query`；就绪发 `omnibox:ready` |
| 设置弹窗 | 壳的 `PluginSettingsModal.vue`（`group-mesh`、`document-reader`、`manga-library`、`media-player`，以及 `image-cleaner` / `pixiv-sync` 的 native 面板） | 后端声明 `settings_schema` 并登记 `get_settings` / `save_settings`；保存成功后壳向内容页发 omnibox:action settings-saved（native 目标发给隐藏逻辑页）。image-viewer 例外，设置仍在内容页由 `openSettingsModal()` 渲染 |
| 插件背景不透明度 | 壳偏好 `--plugin-glass-<插件名>` + 设置页顶部「外观」块 | 默认跟随壳 `--glass-alpha`；image-viewer 用 `omnibox:get-plugin-appearance` / `omnibox:set-plugin-alpha` 读写同一份值。覆盖只作用于页面骨架与内容页公共区域 |
| native 内容 | 壳的 `NativePanel.vue` + `SchemaRenderer.vue` | `get_extensions()` 声明 `native`；逻辑页装载 `/shell/native-client.js`，用 `NativeExtension.create({ onInit, onAction, onDispose })`，只收发 schema blocks 与业务动作；页面级动作通过 `render(blocks, toolbar)` 进宿主顶栏；不自己画标题/关闭 |
| 旧壳扩展回落 | 宿主内容页 | `embedUrl` 指向完整页面，打开时追加 `?embed=1` |

页面骨架、消息与已知边界的完整说明见 [docs/plugin-page-architecture.md](../docs/plugin-page-architecture.md)。

### 兼容路径（仅旧壳）

新壳只走 native 面板：入口来自 `get_extensions()` 的 `native: {url, title}`，由 `PluginExtensions` +
`NativePanel` 渲染，宿主内容 iframe 用 v-show 隐藏。`embedUrl` 与插件自己的 `frontend.entry` 页面
（含 `?embed=1`、`HostChannel` 工具栏、内容页自绘设置弹窗）只为**不认 `native` 的旧壳**保留，新壳
不会进入这些路径；旧壳的 `openSettingsModal()` / `HostChannel.requestSettings` 设置入口也一样。
插件逻辑页（`native.html`）不再调用宿主页面上的对象，只通过 `Bridge` / `postMessage` 与壳交互。

## 设置字段契约

- 插件设置与 native 设置的字段只有一个前端类型：[plugin-content.ts](../shell/frontend/src/core/plugin-content.ts)
  的 SchemaField；不要在插件里另造一份字段形状。
- 最终校验在后端 `save_settings`：number/range 按 min/max 校验并把数字字符串归一成数字；
  secret 的掩码回传表示不改、空串表示清除、其它非空值表示覆盖；admin_only 按当前主体判定；
  info 只读、不接受写入。frontend.chrome 插件的顶栏「设置」由壳的
  [PluginSettingsModal](../shell/frontend/src/components/PluginSettingsModal.vue) 打开
  （image-viewer 例外），保存成功后内容页收到 settings-saved。
- `type: "directory"` 的字段（壳弹窗、native form、内容页旧表单）统一由壳内共享 `FolderPicker`
  （`shell/frontend/public/shell/folder-picker.js`）渲染：`浏览…` 走宿主目录枚举接口
  （system_browse_dir），`local_only: true` 不显示「网络位置」，其余字段可由 network-location
  插件提供远端来源。不再使用 `system_pick_path('directory')` 系统对话框（该接口只剩壳设置页选图片）。
- 三个前端入口对字段的支持并不完全一致：壳弹窗支持 min/max/step、secret（含显式清除）、
  hidden、directory/local_only、multi、emptyText、required（仅样式）、info，并消费逐字段 `errors`；
  native form 支持 min/max/step、secret（仅 password 样式）、directory/multi/local_only、info；
  内容页旧表单支持 min/max/step、directory/multi/local_only、hidden、info。condition 与 save_target
  是类型里的预留字段，当前没有消费方；required 不做提交校验；step 只在前端生效。
- image-cleaner / pixiv-sync 的 native 设置已由壳的 `PluginSettingsModal` 接管：native 顶栏的
  「设置」动作由壳拦截，保存成功后向隐藏逻辑页发 settings-saved；native `form` block 只用于
  非设置输入（pixiv-sync 的 OAuth 授权码）。详细的消费差异与未覆盖项见
  [plugin-page-architecture.md](../docs/plugin-page-architecture.md) 与
  [plugin-guide.md](../docs/plugin-guide.md) 的设置章节。

## 规则

- 新增插件用脚手架，不要手写骨架：`python tools/new_plugin.py my-tool`（模板 `tools/examples/hello-world`）。
- 提交前跑 `python tools/check_plugins.py`。
- 插件后端的动态依赖必须登记：见 [docs/Releases/AGENTS.md](../docs/Releases/AGENTS.md)。

## 先读

- [docs/plugin-guide.md](../docs/plugin-guide.md) —— 插件开发指南：结构、manifest、companion 插件、重型依赖、后端、前端、外观、文件服务、设置持久化、调试与测试。
- [docs/plugin-page-architecture.md](../docs/plugin-page-architecture.md) —— 壳渲染的页面骨架、content iframe / native 面板归属、设置与搜索归属。
- [docs/plugin-ui-guide.md](../docs/plugin-ui-guide.md) —— 界面规范：共享组件、外观变量、样式约定。

改插件页面后跑 `venv/Scripts/python.exe tools/plugin_ui_audit.py`：它用真实壳检查页面骨架与内容页契约（见 [tools/AGENTS.md](../tools/AGENTS.md)）。
