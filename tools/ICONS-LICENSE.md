# Lucide 图标（图标源与授权说明）

本目录下的 `icon_data.json` 是从 [Lucide](https://lucide.dev) 官方发布的
`lucide-static` 包中抽取的图标图形，冻结进仓库后**运行期不再联网**。

- 来源包：`lucide-static`（见 `icon_data.json` 的 `package` 字段记录的版本）
- 授权：**ISC License**（https://lucide.dev/license）—— 允许商用、允许修改与再分发，
  要求保留版权声明与许可声明。本文件与 `shell/frontend/public/shell/icons.svg`
  头部的注释共同承担该声明义务。
- 每个图标源文件顶部自带 `@license lucide-static vX.Y.Z - ISC`，抽取时保留在
  `icon_data.json` 的 `source` 字段里，便于追溯到具体版本。

## 为什么不做成依赖

`sprite` 是运行时静态资源，不参与 JS 构建。把它做成 `shell/frontend/package.json`
的依赖只会连带影响打包收集规则（`docs/Releases/spec_common.py`、
`tools/check_packaging.py`），而收益为零。所以采用"取一次、冻结进仓库"：
构建期由 `tools/fetch_lucide_icons.py` 拉取，运行期零依赖、完全离线。

## 增删图标

```bash
venv/Scripts/python tools/fetch_lucide_icons.py --add <kebab-case 名字>
venv/Scripts/python tools/build_icons.py
npm --prefix shell/frontend run build
```

图标名去 https://lucide.dev/icons 搜。
