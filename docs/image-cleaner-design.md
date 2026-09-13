# 相册清理插件（image-cleaner）设计文档

> 版本：v0.1（已实装）
> 目标形态：**Companion 插件**

## 1. 定位

`image-cleaner` 是 `image-viewer` 的伴侣插件，提供**全部相册范围**的重复 / 相似图片扫描与清理，不再把清理逻辑塞进宿主插件。

### 1.1 与宿主的关系

- 使用 `manifest.dependencies: ["image-viewer"]` 声明依赖，由 `PluginManager` 保证宿主先加载。
- 后端通过 `PluginBase.get_dependency('image-viewer')` 获取宿主实例，复用：
  - `get_data_root()` / `get_file_roots()`：定位相册根目录与文件服务安全根目录
  - `thumb_dir` / `ensure_thumb()`：让本插件前端也能直接使用 `/thumbs`
  - `delete_files()`：复用宿主的路径安全校验与缓存清理
- `image-viewer` 不再包含 `duplicate_scan` / `similar_scan` / 清理 UI。
- `image-cleaner` 通过 `get_extensions()` 注册到 Shell，`image-viewer` 左侧栏使用通用 `renderExtensions()` 渲染入口。
- `image-cleaner` 在 manifest 中声明 `hidden: true`，不会出现在外部 Shell 主导航；点击 image-viewer 左侧栏入口后，以 iframe 内嵌方式在 image-viewer 内部打开。

### 1.2 扫描范围

扫描范围为**全部相册**，即递归扫描宿主数据根目录下的所有图片文件：

- 跳过隐藏目录和 `.cache`
- 跨目录检测重复 / 相似图片
- 支持嵌套相册，不限于当前打开的一个目录

## 2. 目录结构

```
plugins/
└── image-cleaner/
    ├── manifest.json
    ├── backend/
    │   └── main.py
    └── frontend/
        ├── index.html
        ├── image-cleaner.css
        └── js/
            └── app.js
```

## 3. API

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `duplicate_scan` | 无 | `{groups, scanned}` | 全相册完全重复扫描 |
| `similar_scan` | `threshold` | `{groups, scanned}` | 全相册视觉相似扫描 |
| `delete_files` | `rel_paths` | `{deleted, errors}` | 复用宿主的删除接口 |
| `get_status` | 无 | `{host, root_dir, scope}` | 查看当前清理范围与宿主信息 |

## 4. 前端

`/plugins/image-cleaner/frontend/index.html` 作为内嵌页面，通过 `get_extensions()` 在 `image-viewer` 左侧栏挂载“相册清理”入口；点击后由 image-viewer 用 iframe 加载。页面包含：

- 完全重复 / 相似图片两个 Tab
- 全选组、勾选删除
- 缩略图通过 `Bridge.thumbUrl()` 展示，文件服务由插件代理到 image-viewer 根目录
