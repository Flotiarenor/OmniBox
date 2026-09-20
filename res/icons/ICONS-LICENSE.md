# Lucide 图标（图标源与授权说明）

本目录是**图标这件事的唯一落点**：源数据 `icon_data.json`、生成物 `icons.svg`、
以及本授权说明同处一目录。

- `icon_data.json` 从 [Lucide](https://lucide.dev) 官方发布的 `lucide-static` 包中抽取，
  冻结进仓库后**运行期不再联网**。
- 来源包：`lucide-static`（见 `icon_data.json` 的 `package` 字段记录的版本）
- 授权：**ISC License**（https://lucide.dev/license）—— 允许商用、允许修改与再分发，
  要求保留版权声明与许可声明。本文件与 `icons.svg` 头部的注释共同承担该声明义务。
- 每个图标源文件顶部自带 `@license lucide-static vX.Y.Z - ISC`，抽取时保留在
  `icon_data.json` 的 `source` 字段里，便于追溯到具体版本。

## 为什么不做成依赖

`sprite` 是运行时静态资源，不参与 JS 构建。把它做成 `shell/frontend/package.json`
的依赖只会连带影响打包收集规则（`docs/Releases/spec_common.py`、
`tools/check_packaging.py`），而收益为零。所以采用"取一次、冻结进仓库"：
构建期由 `tools/fetch_lucide_icons.py` 拉取，运行期零依赖、完全离线。

## 为什么生成器在 tools/ 而不在本目录

`tools/` 是开发期门禁与脚本目录（其中没有任何文件被运行时或打包引用）。
生成器属于脚本，所以留在 `tools/`；数据与授权属于资产，放在这里。

## 发布与打包

本目录由 `shell/backend/file_server.py` 的 `/res/<path:filename>` 路由发布（免令牌），
公开 URL 为 `/res/icons/icons.svg`。**不被 Vite 处理**，所以源文件就是发布文件，
改完立即生效；但打包时必须由 `docs/Releases/spec_common.py` 单独收集，否则冻结后
所有图标 404（`tools/check_packaging.py` 与 `tools/check_build_tree.py` 各有一条断言兜住）。

## 增删图标

```bash
venv/Scripts/python tools/fetch_lucide_icons.py --add <kebab-case 名字>
venv/Scripts/python tools/build_icons.py
```

图标名去 https://lucide.dev/icons 搜。改这里**不需要** `npm run build`。
