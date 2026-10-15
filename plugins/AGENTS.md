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

少数插件另有目录：`media-player/tool/`、`media-player/eq-presets/`。

现有插件：`document-reader` `group-mesh` `image-cleaner` `image-viewer` `manga-library` `media-player` `netease-music` `pixiv-sync`。

## manifest.json

字段：`name` `version` `displayName` `icon` `description` `dependencies` `keepAlive` `permissions` `backend.{entry,class}` `frontend.{entry,route}` `minShellVersion`（实例：`plugins/media-player/manifest.json`）。

**每个字段都必须有读取方**：`tools/check_plugins.py` 用 `RUNTIME_FIELD_READERS` 校验，运行时确实无效果的字段要显式登记。

## 规则

- 新增插件用脚手架，不要手写骨架：`python tools/new_plugin.py my-tool`（模板 `tools/examples/hello-world`）。
- 提交前跑 `python tools/check_plugins.py`。
- 插件后端的动态依赖必须登记：见 [docs/Releases/AGENTS.md](../docs/Releases/AGENTS.md)。

## 先读

- [docs/plugin-guide.md](../docs/plugin-guide.md) —— 插件开发指南：结构、manifest、companion 插件、重型依赖、后端、前端、外观、文件服务、设置持久化、调试与测试。
- [docs/plugin-ui-guide.md](../docs/plugin-ui-guide.md) —— 界面规范：共享组件、外观变量、样式约定。
