# OmniBox 插件开发指南

本指南将带你从零开始创建一个完整的 OmniBox 插件，并说明如何将现有的 `image-viewer` 插件迁移到新架构。

插件分为两类：**常规内嵌插件**（本指南主体）与**独立运行环境插件 / Companion 插件**（见 §2.1、§2.2 及 `docs/adapter-spec.md`）。

---

## 1. 插件目录结构

每个插件是一个独立文件夹，放置在项目根目录的 `plugins/` 下。结构如下：

```
plugins/
└── <plugin-name>/
 ├── manifest.json # 插件声明（必填）
 ├── backend/ # Python 后端代码（必填）
 │ ├── __init__.py
 │ └── main.py # 插件入口，必须包含 Plugin 类
 └── frontend/ # 前端静态文件（必填）
 ├── index.html # 插件入口 HTML
 ├── *.css # 插件专属样式
 ├── *.js # 插件专属脚本
 └── assets/ # 其他静态资源（可选）
```

**命名约定**：

- 文件夹名使用 `kebab-case`（如 `image-viewer`），与 `manifest.json` 中的 `name` 字段一致。
- 后端入口文件固定为 `backend/main.py`（可在 manifest 中自定义）。
- 前端入口文件固定为 `frontend/index.html`（可在 manifest 中自定义）。

---

## 2. manifest.json 规范

```json
{
"name": "image-viewer",
"version": "1.0.0",
"displayName": "图片浏览",
"description": "浏览本地图片，支持缩略图和灯箱",
"icon": "🖼️",
"author": "Your Name",
"dependencies": [],
"permissions": ["filesystem:read", "filesystem:write"],
"backend": {
 "entry": "backend/main.py",
 "class": "ImageViewerPlugin"
},
"frontend": {
 "entry": "frontend/index.html",
 "route": "/image-viewer"
},
"minShellVersion": "3.0.0"
}
```

**字段说明**：

| 字段                | 必填 | 说明                                                                            |
| ------------------- | ---- | ------------------------------------------------------------------------------- |
| `name`            | ✅   | 插件唯一标识，必须与文件夹名一致，使用`kebab-case`                            |
| `version`         | ✅   | 语义化版本号                                                                    |
| `displayName`     | ✅   | 在导航栏显示的名称                                                              |
| `icon`            | ✅   | 导航栏图标（Emoji 或文字）                                                      |
| `backend.entry`   | ✅   | 后端入口文件路径，相对于插件根目录                                              |
| `backend.class`   | ✅   | 后端插件类名，必须继承`PluginBase`                                            |
| `frontend.entry`  | ✅   | 前端入口 HTML 文件路径，相对于插件根目录                                        |
| `frontend.route`  | ✅   | 前端路由路径，必须以`/` 开头                                                  |
| `dependencies`    | ❌   | 依赖的其他插件名称列表                                                          |
| `libs`            | ❌   | 插件本地附加库目录列表，默认`["backend/libs"]`，加载后端前会加入 `sys.path` |
| `permissions`     | ❌   | 权限声明（仅作知情明示，供设置页展示，不做运行时强制）                     |
| `minShellVersion` | ❌   | 要求的最低 Shell 版本                                                           |
| `destroyOnLeave`  | ❌   | `true` 时离开页面销毁 iframe 重新加载（默认保持存活）                         |
| `hidden`          | ❌   | `true` 时不显示在 Shell 主导航，但仍可被宿主内嵌或通过插件 URL 访问           |
| `kind`            | ❌   | `local-adapter`：声明本插件管理独立运行环境（重依赖插件使用）                 |
| `runtime`         | ❌   | 独立运行环境声明（venv / 入口 / requirements），见 §2.2                        |

---

## 2.1 Companion 插件（跨插件协作，防污染宿主）

需要扩展另一个插件功能时，**不要修改宿主插件**，而是创建独立的 Companion 插件：

```json
{
  "name": "image-tagger",
  "dependencies": ["image-viewer"],
  ...
}
```

`PluginManager` 已按依赖拓扑排序加载，`image-viewer` 一定先于 `image-tagger` 初始化。

**后端访问依赖插件**（已实装，见 `docs/core-direction.md`）：

```python
class ImageTaggerPlugin(PluginBase):
    def register_api(self):
        return {'tag_album': self.tag_album}

    def tag_album(self, rel_path):
        host = self.get_dependency('image-viewer')   # 未声明依赖时返回 None
        roots = host.get_file_roots()                # 复用宿主的安全文件根目录
        ...
```

**前端跨插件调用**（Shell `base.js` 已实装）：

```javascript
await Bridge.callPlugin('image-tagger', 'tag_album', 'PIXEVAL/画师A');
// 等价于 parent.pywebview.api['image-tagger__tag_album']('PIXEVAL/画师A')
```

**扩展注册表**（宿主只写泛化渲染点）：

```python
class ImageTaggerPlugin(PluginBase):
    def get_extensions(self):
        return [{
            'host': 'image-viewer',
            'id': 'tag-selected',
            'label': '🏷️ 打标',
            'method': 'tag_album',
            'scope': 'album',
            # 如果希望宿主内嵌 iframe 打开，则使用 embedUrl 而不是 route：
            # 'embedUrl': '/plugins/image-cleaner/frontend/index.html',
        }]
```

宿主前端：

```javascript
// 方式一：使用通用渲染器（推荐，Shell base.js 已提供）
renderExtensions(document.getElementById('extensions'), 'image-viewer', 'sidebar');

// 方式二：手动拉取扩展后自行渲染
const exts = await Bridge.callSystem('system_get_plugin_extensions', 'image-viewer', 'sidebar');
exts.forEach(ext => addToolbarButton(ext.label, () => Bridge.callPlugin(ext.plugin, ext.method)));
```

> 目前已落地的 Companion 插件示例：`image-cleaner`（全相册重复/相似清理），设计见 `docs/image-cleaner-design.md`。

## 2.2 重型依赖与独立运行环境（runtime）

torch / onnxruntime 等重依赖**不得**写在插件 `backend/main.py` 顶层 import（会拖慢整个应用启动且与主进程环境冲突）。正确做法：

1. **manifest 声明独立环境**：

```json
"runtime": {
  "kind": "stdio-worker",
  "entry": "backend/runtime/worker.py",
  "venv": "backend/runtime/venv",
  "requirements": "backend/runtime/requirements.txt",
  "startup": "lazy",
  "timeoutSeconds": 3600
}
```

2. **控制器保持轻量**：`backend/main.py` 只做任务管理 / 进度 / 结果索引，通过 `shell.backend.adapter_process`（规划中）拉起 `<runtime.venv>/Scripts/python.exe` 子进程。
3. **通信**：stdio JSON-lines，每行一个 JSON 对象；控制行下行、进度/结果上行（协议见 `docs/image-tagger-design.md`）。
4. **部署**：`deploy.ps1` / `setup-venv.ps1` 按 `runtime.requirements` 创建插件独立 venv；失败不阻塞主程序。
5. **原型阶段**允许懒 import 兜底：首次调用时才 `import torch`，并把任务放进后台线程；正式版切独立环境。

---

## 3. 后端开发

### 3.1 插件基类

所有后端插件必须继承 `shell.backend.plugin_base.PluginBase`，并实现 `register_api()` 方法。

```python
# shell/backend/plugin_base.py（已由框架提供，无需修改）
from abc import ABC, abstractmethod

class PluginBase(ABC):
    def __init__(self, manifest: dict, config: dict):
        self.manifest = manifest
        self.config = config            # 全局配置（.config/app.yaml 内容）
        self.name = manifest['name']
        self._settings_store = None     # 由 PluginManager 注入
        self._resolved_config = {}      # 构造前预加载的已解析设置

    @abstractmethod
    def register_api(self) -> dict:
        """返回暴露给前端的 API 字典，格式：{'method_name': callable}"""
        pass

    # ===== 设置 API（框架统一实现） =====

    def setting(self, key, default=None):
        """读取单个设置。优先级：SettingsStore → _resolved_config → schema.default"""

    def update_setting(self, key, value):
        """写入单个设置（保留其他设置），不校验 schema，用于运行时状态持久化"""

    def save_settings(self, settings: dict) -> dict:
        """批量保存 + 自动检测变更 + 调用 on_settings_changed()，子类一般无需覆写"""

    def on_settings_changed(self, changed_keys: set):
        """设置变更时自动调用。子类覆写此方法以响应变更（如 root_dir 变化后重建缓存）"""

    def get_settings(self) -> dict:
        """读取全部设置（schema 默认值 + 存储值合并），子类可覆写"""

    # ===== 生命周期 =====

    def on_load(self):
        """插件加载后调用（可选）"""

    def on_unload(self):
        """插件卸载前调用（可选）"""

    def get_data_root(self) -> Path:
        """返回该插件使用的数据根目录，默认使用全局配置。
        插件可重写此方法以支持自定义根目录。"""
        return Path(self.config['directories']['data_root']).resolve()
```

### 3.2 编写插件类

```python
# plugins/my-plugin/backend/main.py
from pathlib import Path
from shell.backend.plugin_base import PluginBase

class MyPlugin(PluginBase):
    # 声明设置项：集中设置面板 + SettingsStore 持久化由框架自动处理
    settings_schema = [
        {"key": "root_dir", "label": "数据根目录", "type": "text", "central": True,
         "help": "存放数据的根目录"},
        {"key": "per_page", "label": "每页数量", "type": "number",
         "default": 40, "min": 10, "max": 200},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        # _resolved_config 由 PluginManager 在构造前预加载（含用户已保存的设置）
        root = self._resolved_config.get('root_dir') or str(super().get_data_root())
        self.root_dir = Path(root).resolve()

    def on_settings_changed(self, changed_keys):
        """设置变更时自动调用。无需覆写 save_settings，只需响应变更。"""
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')  # 读取最新值（SettingsStore）
            if new_dir and Path(new_dir).is_dir():
                self.root_dir = Path(new_dir).resolve()
                self._list_cache.clear()  # 清缓存，让下次请求扫描新目录

    def register_api(self) -> dict:
        return {
            'list_items': self.list_items,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    def list_items(self):
        # 使用 self.root_dir 扫描...
        pass
```

**关键点**：

- 所有方法自动获得命名空间前缀 `插件名__`，前端调用时使用 `Bridge.call('method_name', ...)`（无需手动加前缀，`Bridge` 会自动处理）。
- 方法参数和返回值必须是 JSON 可序列化的类型（dict、list、str、int、float、bool、None）。
- 文件访问应通过 `/files/` 和 `/thumbs/` 路由（由 Shell 提供），避免直接返回本地绝对路径。
- 插件可通过重写 `get_data_root()` 方法支持自定义数据根目录，Flask 路由会根据 `?plugin=插件名` 动态获取根目录。
- **不要覆写 `save_settings()`**——基类已统一实现「校验 → 持久化 → 变更检测 → 调用 `on_settings_changed()`」。需要响应设置变更时，覆写 `on_settings_changed()` 即可。

### 3.3 插件本地附加库（libs）

如果插件只需要少量纯 Python 依赖，可以把依赖放在插件目录下，例如：

```text
plugins/my-plugin/
├── manifest.json
└── backend/
    ├── main.py
    └── libs/
        └── pyradios/
```

默认情况下，`PluginManager` 会在加载插件后端前把：

```text
<plugin>/backend/libs
```

加入 `sys.path`。插件代码里可以直接：

```python
from pyradios import RadioBrowser
```

也可以在 `manifest.json` 中自定义目录：

```json
{
  "name": "my-plugin",
  "libs": ["backend/libs", "vendor"]
}
```

适合：

- 纯 Python 库
- 不想安装进主 venv 的小依赖
- 希望随插件灵活分发/卸载的依赖

不适合：

- 需要编译的 C 扩展
- 跨平台二进制库
- 大型重依赖（建议走独立 runtime）

### 3.4 完整 API 列表

（以 image-viewer 为例）

| API 方法                  | 参数                                              | 返回值                                                  | 说明                            |
| ------------------------- | ------------------------------------------------- | ------------------------------------------------------- | ------------------------------- |
| `list_images`           | `rel_path, page, per_page, sort_by, sort_order` | `{images, page, total, has_next, has_prev, settings}` | 获取图片列表（含尺寸缓存）      |
| `list_dir`              | `rel_path`                                      | `[{name, path}]`                                      | 列出子目录                      |
| `delete_files`          | `[rel_paths]`                                   | `{deleted, errors}`                                   | 批量删除文件                    |
| `move_files`            | `[rel_paths], dest_rel`                         | `{moved, errors}`                                     | 批量移动文件                    |
| `get_settings`          | `rel_path`                                      | `{row_height, per_page, sort_by, sort_order}`         | 获取文件夹设置（含全局回退）    |
| `save_settings`         | `rel_path, settings`                            | `{success}`                                           | 保存文件夹设置（支持 root_dir） |
| `get_root_dir`          | 无                                                | `str`                                                 | 获取当前使用的根目录            |
| `clear_folder_settings` | `rel_path`                                      | `{success}`                                           | 清除文件夹独立设置，回退到全局  |

---

## 4. 前端开发

### 4.1 Shell 注入的资源

插件前端运行在 iframe 中，Shell 会自动注入以下资源：

- **CSS 变量**：`/shell/variables.css`（提供主题颜色、间距等）
- **基础样式**：`/shell/base.css`（按钮、布局、滚动条等）
- **基础脚本**：`/shell/base.js`（提供全局 `Bridge` 对象和通用 UI 组件）

**你无需在 HTML 中手动引入这些文件**，Shell 会在加载插件时自动注入到 `<head>` 中。

另外 Shell 还自动注入通用动效资源，所有插件可直接使用：

- `/shell/effects.css` — 通用动效样式类：
  - 入场动画：`.obx-anim-fade-up` / `.obx-anim-fade-down` / `.obx-anim-fade` / `.obx-anim-pop` / `.obx-anim-scale`
  - 常驻动画：`.obx-anim-float` / `.obx-anim-spin` / `.obx-anim-pulse` / `.obx-anim-heart`
  - 交错列表：容器加 `.obx-stagger`，子元素设置 `--obx-i`（JS 可调用 `Motion.stagger(container)` 自动编号）
  - 玻璃面板：`.obx-glass` / `.obx-glass-strong`
  - 卡片悬浮抬升：`.obx-card-lift`
  - 骨架屏：`.obx-skeleton`
  - 现代窄滚动条（鼠标悬停渐显、深浅色兼容）：`.obx-scroll`
- `/shell/motion.js` — `window.Motion`：
  - `Motion.stagger(container, selector?)` 为子元素写入交错延迟
  - `Motion.retrigger(el, className?)` 重新触发动画（默认 `obx-anim-heart`）
  - `Motion.show(el, className?)` 淡入上移显示

所有动画均遵循 `prefers-reduced-motion`，用户开启减少动态效果时自动降级。

#### 通用布局类（base.css 已提供，插件无需重复定义）

以下 CSS 类已由 Shell 统一注入，插件 HTML 直接使用即可，**不应在自己 CSS 中重复定义**（否则将不兼容未来主题变更）：

| CSS 类                  | 用途                                                                  |
| ----------------------- | --------------------------------------------------------------------- |
| `.view-body`          | 主内容区容器（`flex: 1; flex-direction: column; overflow: hidden`） |
| `.view-toolbar`       | 顶部工具栏（高 48px，`var(--bg-surface)` 背景，底部边框）           |
| `.toolbar-group`      | 工具栏内的按钮组（`flex; align-items: center; gap: 8px`）           |
| `.view-sub-sidebar`   | 左侧子侧边栏（宽`var(--sub-sidebar-width)`，默认 240px）            |
| `.sub-sidebar-header` | 侧边栏标题行（大写标签，底部边框）                                    |
| `.sub-sidebar-footer` | 侧边栏底部统计区（小字体，顶部边框）                                  |
| `.view-content`       | 内容滚动区（`flex: 1; overflow-y: auto; padding: 16px`）            |

**示例 HTML 结构**：

```html
<div id="app">
  <div class="view-sub-sidebar">
    <div class="sub-sidebar-header">浏览目录</div>
    <!-- 目录树等 -->
    <div class="sub-sidebar-footer">共 0 项</div>
  </div>
  <div class="view-body">
    <div class="view-toolbar">
      <div class="toolbar-group">...</div>
      <div class="toolbar-group" style="margin-left:auto;">⚙ 设置</div>
    </div>
    <div class="view-content">
      <!-- 主内容区 -->
    </div>
  </div>
</div>
```

### 4.2 全局 Bridge 对象

`Bridge` 对象挂载在 `window` 上，提供以下方法：

| 方法                                           | 说明                                                                    |
| ---------------------------------------------- | ----------------------------------------------------------------------- |
| `Bridge.call(method, ...args)`               | 调用后端 API（自动添加插件名前缀）                                      |
| `Bridge.callSystem(method, ...args)`         | 调用 Shell 系统 API（不加插件前缀，如`system_get_plugin_extensions`） |
| `Bridge.callPlugin(plugin, method, ...args)` | 跨插件调用其他插件后端 API                                              |
| `Bridge.thumbUrl(relPath)`                   | 获取缩略图 URL（自动附加`?plugin=插件名`）                            |
| `Bridge.originalUrl(relPath)`                | 获取原图 URL（自动附加`?plugin=插件名`）                              |
| `Bridge.setPrefix(prefix)`                   | 设置 API 前缀（Shell 在加载插件时自动调用）                             |

**示例**：

```javascript
// 调用后端 list_images 方法
const data = await Bridge.call('list_images', '', 1, 40, 'mtime', 'desc');

// 获取缩略图 URL
const thumbSrc = Bridge.thumbUrl('subdir/photo.jpg');
// 返回：/thumbs/subdir/photo.jpg?plugin=image-viewer

// 获取原图 URL
const originalSrc = Bridge.originalUrl('subdir/photo.jpg');
// 返回：/files/subdir/photo.jpg?plugin=image-viewer
```

### 4.3 全局 UI 组件

Shell 还注入了以下可复用的 UI 组件函数（无需引入，直接使用）：

| 函数                                                                | 说明                                      |
| ------------------------------------------------------------------- | ----------------------------------------- |
| `createTree(container, options)`                                  | 创建目录树组件                            |
| `createLightbox(options)`                                         | 创建灯箱组件                              |
| `createPagination(container, options)`                            | 创建分页组件                              |
| `createContextMenu(options)`                                      | 创建右键菜单组件                          |
| `createCardGrid(container, options)`                              | 创建卡片网格组件                          |
| `createSettingsForm(container, schema, values)`                   | 按 schema 渲染设置表单                    |
| `renderExtensions(container, host, placement, options?)`          | 渲染注册到宿主侧边栏/工具栏的扩展插件入口 |
| `openSettingsModal(options)`                                      | 打开统一设置弹窗                          |
| `confirmDialog(message, options)`                                 | 替代原生 confirm                          |
| `Toast.success(msg)` / `Toast.error(msg)` / `Toast.info(msg)` | 显示 Toast 通知                           |
| `Utils.formatFileSize(bytes)`                                     | 格式化文件大小                            |
| `Utils.debounce(func, wait)`                                      | 防抖函数                                  |

**示例**：

```javascript
// 创建卡片网格
const grid = createCardGrid(document.getElementById('grid'), {
  cardRenderer: (item) => ({
    image: item.cover_url,
    title: item.title,
    subtitle: item.author,
    badge: `${item.count} 项`,
  }),
  onClick: (item, index) => openDetail(item),
});
grid.render(items);

// 打开设置弹窗
openSettingsModal({
  title: '媒体库设置',
  successMessage: '设置已保存',
  onSave: async (values) => {
    return await Bridge.call('save_settings', values);
  },
});
// 注意：保存成功后框架会自动刷新页面以应用新设置（无需手动处理）
// 若插件需要自定义保存后行为，使用 onSave 回调并在其中完成刷新

// 确认对话框
const ok = await confirmDialog('删除后不可恢复，确定？', { danger: true });

// 目录树
createTree(document.getElementById('folder-tree'), {
  data: [{ name: '根目录', path: '' }],
  onLoadChildren: async (path) => await Bridge.call('list_dir', path),
  onClick: (item) => console.log('选中', item.path)
});

// 灯箱
const lightbox = createLightbox({
  getImageUrl: (item) => item.url
});
lightbox.show(images, 0);
```

### 4.4 插件专属文件

插件只需提供自己的 HTML、CSS 和 JS 文件，并在 HTML 中引用它们：

```html
<!-- plugins/image-viewer/frontend/index.html -->
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>图片浏览</title>
<link rel="stylesheet" href="image-viewer.css">
</head>
<body>
<div id="app">
 <!-- 插件界面 -->
</div>
<script src="image-viewer.js"></script>
<script>
 const imageViewer = new ImageViewer();
 imageViewer.init();
</script>
</body>
</html>
```

**注意**：

- 不要引入 `/shell/` 下的文件，它们已自动注入。
- 所有路径相对于插件 `frontend/` 目录。
- 使用 CSS 变量（如 `var(--bg-surface)`）以适配主题。

---

## 5. 外观主题与颜色同步

### 5.1 主题切换

Shell 通过 `<html>` 元素上的 `data-theme` 属性控制主题（`"light"` / `"dark"`）。插件 iframe 的注入脚本自动监听父窗口的 `data-theme` 变化，并同步设置自身的主题属性。

**插件无需编写任何主题同步代码**。只需在 CSS 中使用 CSS 变量（如 `var(--bg-surface)`），主题切换会自动生效。

### 5.2 自定义颜色同步

用户在设置页的「外观设置」面板中修改颜色（如强调色、背景色等）时，Shell 会将修改后的 CSS 变量值序列化为 JSON 写入父文档的 `data-custom-colors` 属性。插件 iframe 的注入脚本自动监听该属性变化，并应用 `document.documentElement.style.setProperty()` 更新所有对应变量。

**对插件开发的影响**：

- 所有 CSS 变量均可被用户覆盖，插件设计时不应依赖特定颜色值。
- 若需要在 JS 中读取当前有效颜色，使用 `getComputedStyle(document.documentElement).getPropertyValue('--accent')`。

### 5.3 全屏联动

需要全屏的插件（如视频播放器）可设置 `parent.document.documentElement` 的 `data-video-fullscreen` 属性（`"true"` / `"false"`）来通知 Shell 隐藏/显示左侧导航栏：

```javascript
// 进入全屏
parent.document.documentElement.setAttribute('data-video-fullscreen', 'true');
// 退出全屏
parent.document.documentElement.removeAttribute('data-video-fullscreen');
```

Shell 端的 `App.vue` 通过 MutationObserver 监听此属性变化并自动控制导航栏显示。

---

## 7. 文件服务与动态根目录

### 7.1 文件路由

Shell 提供两个文件服务路由：

- `/files/<path:filepath>?plugin=插件名`：提供原始文件
- `/thumbs/<path:filepath>?plugin=插件名`：提供缩略图

这两个路由会根据 `plugin` 参数动态获取对应插件的 `get_data_root()` 返回值作为根目录，并进行路径安全检查。

需要跨多个媒体根目录提供文件的插件（如 `media-player`）可以覆写 `get_file_roots()`，
返回所有允许访问的根目录列表；`/files/` 路由会对每个根目录做安全检查。
此时前端应使用 URL 编码后的绝对路径访问文件：

```javascript
// 绝对路径（media-player 跨根场景）
const src = Bridge.originalUrl(encodeURIComponent('G:/音乐/cover.jpg'));
// 相对路径（普通单根插件）
const src = Bridge.originalUrl('subdir/photo.jpg');
```

### 7.2 插件如何生成缩略图

插件后端应在 `list_images` 等方法中按需生成缩略图，保存到 `self.thumb_dir`（通常为 `数据根目录/.cache/thumbs/`）。前端通过 `Bridge.thumbUrl()` 获取正确的 URL。

**示例**：

```python
def _get_thumb(self, rel_path: str) -> Path:
 thumb_path = self.thumb_dir / rel_path
 if thumb_path.exists():
 return thumb_path
 thumb_path.parent.mkdir(parents=True, exist_ok=True)
 try:
 from PIL import Image
 img = Image.open(self.root_dir / rel_path)
 img.thumbnail((300, 300))
 img.save(thumb_path)
 except Exception:
 shutil.copy(self.root_dir / rel_path, thumb_path)
 return thumb_path
```

---

## 8. 设置持久化

### 8.1 存储位置

所有插件设置统一存储在 `.config/plugins/<插件名>.json`（由 `SettingsStore` 管理），**不要**在插件目录下创建 `settings.json` 文件。

```
.config/
├── app.yaml                    ← 主程序配置
└── plugins/
    ├── media-player.json       ← 各插件设置（git 忽略）
    └── image-viewer.json
```

### 8.2 声明设置项

在插件类中通过 `settings_schema` 声明设置项，框架自动完成持久化 + 集中设置面板集成：

```python
class MyPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "数据根目录", "type": "text", "central": True},
        {"key": "per_page", "label": "每页数量", "type": "number", "default": 40},
        {"key": "sort_by", "label": "排序方式", "type": "select",
         "options": [{"label": "修改时间", "value": "mtime"}, {"label": "文件名", "value": "name"}]},
    ]
```

字段说明：

| 字段                         | 必填 | 说明                                                                                      |
| ---------------------------- | ---- | ----------------------------------------------------------------------------------------- |
| `key`                      | ✅   | 设置键名                                                                                  |
| `label`                    | ✅   | 设置面板显示名                                                                            |
| `type`                     | ✅   | `text` / `number` / `range` / `select` / `checkbox` / `textarea` / `folder` |
| `default`                  | ❌   | 默认值（未保存过时使用）                                                                  |
| `help`                     | ❌   | 悬浮`?` 提示文本（鼠标悬停显示）                                                        |
| `central`                  | ❌   | `True` 在集中设置面板显示；默认仅显示 `root_dir` 或有 `central` 标记的字段          |
| `min` / `max` / `step` | ❌   | number/range 类型约束                                                                     |
| `options`                  | ❌   | select 类型的选项列表                                                                     |

### 8.3 读取与写入

| 场景                   | 方法                                                        |
| ---------------------- | ----------------------------------------------------------- |
| `__init__` 中读取    | `self._resolved_config.get('root_dir')`（构造前已预加载） |
| 运行时读取单个         | `self.setting('root_dir')`                                |
| 读取全部（合并默认值） | `self.get_settings()`                                     |
| 保存全部               | `self.save_settings({...})`                               |
| 保存单个（运行时状态） | `self.update_setting('last_volume', 0.8)`                 |

### 8.4 响应设置变更

**不要覆写 `save_settings()`**。设置变更时，基类会自动调用 `on_settings_changed(changed_keys)`，只需覆写它：

```python
def on_settings_changed(self, changed_keys):
    if 'root_dir' in changed_keys:
        new_dir = self.setting('root_dir')
        if new_dir and Path(new_dir).is_dir():
            self.root_dir = Path(new_dir).resolve()
            self._list_cache.clear()
```

保存设置后前端会自动刷新（集中面板 → postMessage → iframe reload；插件弹窗 → 保存后自动 reload），无需手动实现刷新逻辑。

### 8.5 特殊案例：image-viewer 的 per-folder 设置

image-viewer 需要在不同文件夹应用不同设置（如行高、排序），这类设置不在 `settings_schema` 中，保留在插件自己的状态文件里。它覆写了 `save_settings()` 但**必须调用 `super().save_settings(settings)`** 以保持框架的持久化 + 变更检测链路。

### 8.6 运行时状态 vs 用户设置

| 类型       | 存放位置                           | 示例                         |
| ---------- | ---------------------------------- | ---------------------------- |
| 用户设置   | `.config/plugins/{name}.json`    | root_dir、字号、排序方式     |
| 运行时状态 | 数据目录`.cache/` 或插件状态文件 | 播放进度、收藏列表、扫描缓存 |

用户设置跟程序走（换机器拷贝程序即携带），运行时状态跟数据走（换数据目录自动重建）。

---

## 9. 调试与测试

1. **查看插件是否被加载**：启动主程序，控制台会输出 `[PluginManager] ✅ 加载成功: <name>`。
2. **检查前端资源**：在浏览器中直接访问 `http://127.0.0.1:18080/plugins/<name>/frontend/index.html`，确认能正常打开。
3. **检查 API 调用**：在插件前端控制台执行 `Bridge.call('method', ...)`，观察返回结果。
4. **查看 Flask 日志**：所有文件请求都会显示在控制台，便于排查 404 错误。
5. **检查注入是否成功**：在控制台输入 `typeof Bridge`，应返回 `"object"`；输入 `typeof createTree`，应返回 `"function"`。

---

## 10. 常见问题

| 问题             | 原因                                     | 解决                                                                                                |
| ---------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 导航栏不显示插件 | `manifest.json` 格式错误或缺少必填字段 | 检查 JSON 语法，确保`name`、`frontend.route` 等字段存在                                         |
| 点击导航无反应   | 前端路由未正确注入                       | 检查`frontend.route` 是否以 `/` 开头，且不与其他插件冲突                                        |
| iframe 白屏      | 前端入口文件不存在或路径错误             | 确认`frontend/entry` 指向的文件存在，且 Flask 能访问                                              |
| API 调用失败     | 方法名拼写错误或后端未注册               | 检查方法名是否与`register_api` 返回的键一致，调用时使用 `Bridge.call('method')`                 |
| 图片无法显示     | URL 路径错误或文件不存在                 | 确保图片通过`Bridge.thumbUrl()` 或 `Bridge.originalUrl()` 获取 URL，且文件在 `data_root` 下   |
| 缩略图不显示     | 缩略图未生成或路由错误                   | 检查后端是否在`list_images` 中调用了缩略图生成方法，Flask 路由是否正确传递 `plugin` 参数        |
| 设置不生效       | 前端未传递设置参数或后端未应用           | 确保`loadImages` 传递了 `per_page`、`sort_by` 等参数，后端 `list_images` 接收并应用这些参数 |

---

## 11. 最佳实践

- **插件命名**：使用 `kebab-case`，避免与 Python 模块名冲突。
- **布局复用**：优先使用 Shell 提供的通用布局类（`.view-body`、`.view-toolbar`、`.view-sub-sidebar` 等），**不要在插件 CSS 中重复定义**，以便统一维护和未来主题升级。
- **颜色适配**：所有颜色使用 CSS 变量（如 `var(--bg-surface)`），避免硬编码颜色值。如需在 JS 中获取当前颜色，使用 `getComputedStyle(document.documentElement).getPropertyValue('--accent')`。
- **主题无感知**：插件无需编写主题切换逻辑，Shell 已通过 MutationObserver 自动同步 `data-theme` 和 `data-custom-colors` 到所有 iframe。
- **前端资源**：尽量轻量，避免引入大型框架（除非必要）。
- **权限声明**：如实填写 `permissions`，作为面向用户的知情明示（安装/设置页可见）。框架**不设运行时权限墙**——插件后端运行在主进程内，任何声明都无法拦截 `import os` 之类直接系统访问；隔离与信任依赖进程级 runtime 方案（见 `docs/core-direction.md` §3.4、§3.5）。
- **版本管理**：遵循语义化版本，方便依赖解析。
- **错误处理**：后端方法应捕获异常并返回有意义的错误信息，避免前端收到 Python 堆栈。
- **设置持久化**：使用 `settings_schema` 声明式配置，设置自动存储在 `.config/plugins/<name>.json`。读取用 `setting()`，保存用 `save_settings()`，响应变更用 `on_settings_changed()`。**不要**覆写 `save_settings()`，**不要**在插件目录创建 `settings.json`。
- **运行时状态**：播放进度、收藏、缓存等数据派生状态放在数据目录（如 `data/.cache/`），跟随数据走。
- **性能优化**：使用内存缓存（如目录列表缓存、聚合元数据缓存）减少 I/O，提升响应速度。

---

按照本指南，你可以快速创建新插件，或将现有功能迁移到 OmniBox 架构中。快速起步可运行 `python tools/new_plugin.py my-tool` 一键生成骨架（模板见 `tools/examples/hello-world`）；如有疑问，请参考 `image-viewer` 示例插件。
