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

- 文件夹名使用 `kebab-case`（如 `image-viewer`），与 `manifest.json` 中的 `name` 字段一致，缺省为文件夹名；（不一致告警，不阻断加载）。
- 后端入口文件固定为 `backend/main.py`（可在 manifest 中自定义）。
- 前端入口文件固定为 `frontend/index.html`（可在 manifest 中自定义）。

---

## 2. manifest.json 规范

> **推荐用脚手架生成，而不是手写 manifest**：运行 `python tools/new_plugin.py my-tool` 一键生成目录结构、manifest 与示例前后端。手写时多数字段有缺省值（见下表）。

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

> 标记说明：`必填` 必需；`默认` 表示理论上应填写、但省略时运行时取缺省值（`name`→文件夹名、`backend.entry`→`backend/main.py`、`backend.class`→`Plugin`、`frontend.route`→`/<name>`，`name` 与文件夹名不一致仅告警）；`可选` 可完全省略。`tools/check_plugins.py` 仍做严格校验（用于发布前自检）。

| 字段                | 要求 | 说明                                                                            |
| ------------------- | ---- | ------------------------------------------------------------------------------- |
| `version`         | 必填 | 语义化版本号                                                                    |
| `displayName`     | 必填 | 在导航栏显示的名称                                                              |
| `icon`            | 必填 | 导航栏图标（Emoji 或文字）                                                      |
| `frontend.entry`  | 必填 | 前端入口 HTML 文件路径，相对于插件根目录                                        |
| `name`            | 默认 | 插件唯一标识，缺省为文件夹名；与文件夹名不一致告警                              |
| `backend.entry`   | 默认 | 后端入口文件路径，默认`backend/main.py`，相对于插件根目录                     |
| `backend.class`   | 默认 | 后端插件类名，默认`Plugin`，须继承 `PluginBase`                             |
| `frontend.route`  | 默认 | 前端路由，默认`/<name>`，须以 `/` 开头                                      |
| `dependencies`    | 可选 | 依赖的其他插件名称列表                                                          |
| `libs`            | 可选 | 插件本地附加库目录列表，默认`["backend/libs"]`，加载后端前会加入 `sys.path` |
| `permissions`     | 可选 | 权限声明（仅作知情明示，供设置页展示，不做运行时强制）                          |
| `minShellVersion` | 可选 | 要求的最低 Shell 版本                                                           |
| `destroyOnLeave`  | 可选 | `true` 时离开页面销毁 iframe 重新加载（默认保持存活）                         |
| `hidden`          | 可选 | `true` 时不显示在 Shell 主导航，但仍可被宿主内嵌或通过插件 URL 访问           |
| `kind`            | 可选 | `local-adapter`：声明本插件管理独立运行环境（重依赖插件使用）                 |
| `runtime`         | 可选 | 独立运行环境声明（venv / 入口 / requirements），见 §2.2                        |

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

如果插件只需要少量外部的纯 Python 依赖，可以把依赖放在插件目录下，例如：

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

### 3.4 共享基建：后台任务与缩略图缓存

媒体类插件经常需要两类通用能力，Shell 已抽出共享模块（`shell/backend/`），**不要各自重复实现**：

- **`tasks.py` → `BackgroundTask`**（控制面）：后台线程 + 状态机 + 取消 + 进度 + 可选断点持久化。适用于任何长任务：全量重建、同步、批量下载、导入扫描。
- **`thumb_cache.py` → `ThumbCache`**（数据面）：SQLite 缩略图缓存（WAL + mtime/size 失效校验 + 并行批量生成）。适用于任何「本地媒体 → 缩略图」：图片、视频封面抽帧、音频内嵌封面。

#### 后台任务 BackgroundTask

```python
from shell.backend.tasks import BackgroundTask

def _sync_worker(task: BackgroundTask, source: str):
    """worker 自行编排业务流程（网络验证 → 本地比对 → 执行）；
    壳只提供线程/状态/取消/进度。"""
    items = fetch_remote(source)                    # 业务逻辑
    task.update(total=len(items))
    for it in items:
        if task.cancelled:                          # 取消点：Event 检查
            break
        ok = process(it)
        task.update(processed=task.status()['processed'] + 1,
                    current=it.name,
                    extra={'downloaded': ...})      # 业务计数放 extra
        task.persist()                              # 每步落盘 → 崩溃/重启可续跑

# 纯内存任务（image-viewer 重建场景）：
task = BackgroundTask(kind='rebuild')
task.start(_sync_worker, args=('...',))
task.status()   # {state, running, done, success, cancelled, total,
                #  processed, current, error_count, errors, extra}
task.cancel()   # 请求取消（Event 置位）

# 可断点持久化（pixiv-sync 下载场景）：
task = BackgroundTask(kind='sync', persist_path=root / '.cache' / 'tasks.json')
task.start(_sync_worker, args=('...',))
# 重启后：
task = BackgroundTask.load(root / '.cache' / 'tasks.json')   # state = 'paused'
task.resume(_sync_worker, args=('...',))                     # 续跑
```

要点：

- 状态机 `queued → running → done / cancelled`；worker 抛异常 → `done` 且 `success=False`（错误进 `errors`，保留最近 200 条）；
- `cancel()` 只置位 Event，worker 内通过 `task.cancelled` / `task.stop_event`（线程池场景）主动退出；
- 业务专属计数（`downloaded`/`skipped` 等）放 `extra` 扩展字典，壳不预定义；
- 前端轮询 `status()` 展示进度即可（image-viewer 的 `rebuild_status` 是封装示例）。

#### 缩略图缓存 ThumbCache

```python
from shell.backend.thumb_cache import ThumbCache

# 实例归属插件，DB 放各自数据根目录 .cache/ 下（数据跟数据走）
cache = ThumbCache(self.root_dir / '.cache' / 'thumbs.db', size=(300, 300))

# 1. 按需生成（/thumbs 路由用）：Shell 优先调用插件的 get_thumb_data()
def get_thumb_data(self, rel_path: str):
    if not self._is_safe(rel_path):
        return None
    return self.thumb_cache.get(rel_path, self.root_dir / rel_path)

# 2. 批量并行生成（全量重建 / 同步任务，配 BackgroundTask 使用）
result = self.thumb_cache.generate_bulk(
    [(rel, self.root_dir / rel) for rel in images],
    progress_cb=lambda p, t, c, e: task.update(processed=p, total=t, current=c, errors=e),
    stop_event=task.stop_event,
)   # → {'processed', 'total', 'errors'}

# 3. 维护
self.thumb_cache.delete(rel)    # 文件删除/移动时同步清缓存
self.thumb_cache.clear()        # 全清 + wal_checkpoint + VACUUM 收缩
```

要点：

- 失效校验：`source_mtime`（0.5s 容差）+ `source_size` 双条件，源文件替换后自动重生成；
- 生成失败返回 None 且不写缓存（不缓存假缩略图）；
- `size`、MIME 映射（`mime_map`）、并发数（`workers`）均可配置，默认 300×300 + 8 线程；
- 已有实现参考：image-viewer（`ThumbCache` 接入 + `BackgroundTask` 重建任务，见 `docs/image-viewer-design.md` §3.2、§6.4）。

##### 自定义生成器（视频抽帧 / 内嵌封面等非图片源）

`ThumbCache` 的生成策略可扩展（三种方式任选）：

1. **构造注入 `generator`（推荐，组合）**：`generator(src_path) -> (bytes, mime) | None`，线程池会**并发调用**，需保证线程安全：

```python
def video_frame_generator(src_path: Path):
    """视频抽帧（PyAV/ffmpeg 等）：失败返回 None 即不缓存。"""
    if src_path.suffix.lower() not in ('.mp4', '.mkv', ...):
        return None
    frame = extract_frame(src_path)          # → PIL Image / bytes
    out = io.BytesIO()
    frame.save(out, format='JPEG', quality=80)
    return out.getvalue(), 'image/jpeg'

video_cache = ThumbCache(root / '.cache' / 'thumbs.db',
                         size=(320, 180), generator=video_frame_generator)
```

2. **子类覆写 `_generate()`（继承）**：完全接管生成逻辑；覆写中调用 `super()._generate(src)` 可复用「注入优先」逻辑（适合音频内嵌封面等需要读取非图片容器的场景）：

```python
class AudioCoverCache(ThumbCache):
    def _generate(self, src_path):
        try:
            apic = read_embedded_cover(src_path)   # mutagen APIC 帧 → bytes
            return apic, 'image/jpeg'
        except Exception:
            return None
```

3. **默认**：Pillow 图片缩放（`size` 可配），图片类插件无需任何额外配置。

> 生成器返回的 MIME 由生成器自行决定（视频帧 `image/jpeg`、内嵌封面按实际格式）；
> `mime_map` 仅用于默认 Pillow 路径的扩展名推断。

### 3.5 完整 API 列表

（以 image-viewer 为例，完整版见 `docs/image-viewer-design.md` §6）

| API 方法                  | 参数                                              | 返回值                                                                                      | 说明                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_images`           | `rel_path, page, per_page, sort_by, sort_order` | `{images, page, total, has_next, has_prev, settings}`                                     | 获取图片列表（含尺寸缓存）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `list_folder_items`     | `rel_path, page, per_page, sort_by, sort_order` | `{items, all_images, all_truncated, all_offset, page, total, image_total, has_next, has_prev, settings}` | 混合瀑布流列表：直接图片 + 直接子相册 p0 瓦片（只处理一层嵌套）；`all_images` 为按瀑布流顺序展开的完整连续浏览序列（上限 5000 条，超出返回 `all_truncated: true`），每个子文件夹内部按**它自己生效的设置**排序（自己有设置用自己，否则逐级继承父级），不受当前视图排序影响；`all_offset` 为当前页首项在完整序列中的起始偏移（分页对齐，防止灯箱错位）；卡片 `use_time_name` 标记该瓦片是否启用「Pixiv 排序支持」（控制圆圈数量角标显示）；`sort_by=time_name`（Pixiv 排序支持）时：顶层作品/单图按**前导数字**（作品 ID）排序且方向生效（倒序 = 新作品在前），无数字名排最后，作品内部多图片仍按 p0→p1 |
| `list_albums`           | 无                                                | `{albums, config, changed, cached?}`                                                      | 相册全量索引（增量扫描 + 30s TTL 内存缓存，`cached` 标记命中）；`config` 为 `{collapsed, promoted}` 收纳/提升配置                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `create_folder`         | `rel_path`                                      | `{success, path \| error}`                                                                  | 根目录（或指定相对目录）下新建相册文件夹（路径安全校验）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `get_album_config`      | 无                                                | `{collapsed, promoted}`                                                                    | 相册收纳/提升配置                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `set_album_config`      | `rel_path, action`                              | `{success, config}`                                                                        | `action ∈ collapse/expand/promote/unpromote`；变更后失效相册 TTL 缓存                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `get_image_info`        | `rel_path`                                      | `{success, rel_path, size, width, height \| error}`                                        | 单图存储大小与分辨率（全屏查看器右侧信息面板）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `regenerate_thumbs`     | `[rel_paths]`                                   | `{regenerated, errors}`                                                                   | 重新生成选中图片的缩略图（删除缓存并重建，修复坏缩略图）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `refresh`               | 无                                                | `{success}`                                                                               | 清空内存缓存并作废旧相册索引，新增/替换图片立即生效                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `list_dir`              | `rel_path`                                      | `[{name, path}]`                                                                          | 列出子目录                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `delete_files`          | `[rel_paths]`                                   | `{deleted, errors}`                                                                       | 批量删除文件（同步清理缩略图缓存与尺寸元数据）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `move_files`            | `[rel_paths], dest_rel`                         | `{moved, errors}`                                                                         | 批量移动文件（重名自动递增后缀）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `rebuild_all`           | `rel_path='', force=True`                       | `{started, running, total \| error}`                                                      | 全量/指定文件夹重建缩略图：空路径 + force 清空全部缓存重建；空路径 + force=False 全库增量补齐；非空路径只重建该文件夹                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `rebuild_folder`        | `rel_path`                                      | 同 `rebuild_all(force=False)`                                                             | 只重建指定相册（增量补齐，相册菜单入口）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `rebuild_status`        | 无                                                | `{running, done, success, cancelled, rebuild_path, total, processed, current, error_count, errors}` | 后台重建任务进度（前端轮询；错误保留最近 200 条）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `rebuild_cancel`        | 无                                                | `{success \| error}`                                                                        | 请求取消后台重建（已生成缩略图保留）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `get_settings`          | `rel_path`                                      | `{row_height, per_page, sort_by, sort_order, root_dir, pixiv_explicit}`                   | 获取文件夹生效设置（逐级继承 + 全局回退）；`pixiv_explicit` 标记 Pixiv 排序配置点                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `save_settings`         | 两种形态：`save_settings(settings_dict)` 或 `save_settings(rel_path, settings)` | `{success}`                                                                               | 保存全局（schema 过滤）或文件夹级设置（进 `folders[rel_path]`）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `get_root_dir`          | 无                                                | `str`                                                                                     | 获取当前使用的根目录                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `clear_folder_settings` | `rel_path`                                      | `{success}`                                                                               | 清除文件夹独立设置，回退到全局                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |

> 说明：所有 API 的 `rel_path` 均经过 `is_safe_path` 校验；`per_page` 后端封顶 200；`list_folder_items` 的 `all_images` 截断上限 5000。

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

**访问令牌（v3.1+）**：`/api`、`/file`、`/files`、`/thumbs` 均为**令牌保护路由**：

- 令牌在首次启动时生成并持久化到 `.config/auth_token.txt`（重启不变）；
- 页面加载时服务端自动种下 HttpOnly Cookie（`omnibox_token`），插件 iframe 与 `<img>` 等**同源请求自动携带，插件前端无需任何改动**；
- 外部脚本 / curl / nginx 注入用请求头：`X-Omnibox-Token: <token>`；
- 未携带令牌返回 `401`（`/api` 前缀为 JSON，浏览器路径为标记页）；越权路径返回 `403`；不存在的资源返回 `404`；
- `/health`（200 JSON）与页面/静态资源不要求令牌；
- 错误页在浏览器顶层打开时自动跳转到壳内 `/status?code=…` 视图统一展示（见 §7.3）。

### 7.2 插件如何生成缩略图

**推荐模式（image-viewer v2.4.3+）**：基于共享基建 `ThumbCache`（见 §3.4），后端提供 `get_thumb_data(rel_path)`，从 SQLite 缓存读取缩略图字节，未命中时生成并回写：

```python
# __init__ 中创建实例（DB 放各自数据根目录 .cache/ 下）
self.thumb_cache = ThumbCache(self.root_dir / '.cache' / 'thumbs.db')

def get_thumb_data(self, rel_path: str):
    """供 Shell /thumbs 路由使用：返回 (bytes, mime)；无效路径返回 None → 404"""
    if not self._is_safe(rel_path):
        return None
    return self.thumb_cache.get(rel_path, self.root_dir / rel_path)
```

Shell 的 `/thumbs` 路由会优先调用插件的 `get_thumb_data()`（不存在时回退到散文件模式），
前端仍通过 `Bridge.thumbUrl()` 获取 URL，无需感知差异。

**旧版散文件模式（兼容）**：保存到 `self.thumb_dir`（通常为 `数据根目录/.cache/thumbs/`），示例：

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

### 7.3 状态页与壳内错误视图

- **状态码语义**：`401`（未授权）/ `403`（越权）/ `404`（不存在）由后端返回，`/api` 前缀为 JSON（`{error, detail}`），浏览器路径为本体风格标记页；
- **壳内统一展示**：插件 iframe 加载到错误标记页时（标记页 `<html>` 带 `data-status-page="<code>"` 属性），Vue 壳检测后自动跳转壳内建视图 `/status?code=…&from=…` 显示错误卡片（含重试）；浏览器顶层直接访问错误 URL 时标记页 JS 自动跳转同一视图；
- **调试面板**：`python main.py --status-debug` 启动后，`/status` 视图显示状态调试面板（健康检查 / API 鉴权演示 / 错误跳转演示）；一键调试环境见 `python tests/debug_status_pages.py`；
- **API 错误 Toast**：HTTP 桥收到 401/403/404/5xx 时派发 `omnibox:api-error` 事件，壳内弹出本体 Toast（插件前端无需处理）。

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

| 字段                         | 要求 | 说明                                                                                      |
| ---------------------------- | ---- | ----------------------------------------------------------------------------------------- |
| `key`                      | 必填 | 设置键名                                                                                  |
| `label`                    | 必填 | 设置面板显示名                                                                            |
| `type`                     | 必填 | `text` / `number` / `range` / `select` / `checkbox` / `textarea` / `folder` |
| `default`                  | 可选 | 默认值（未保存过时使用）                                                                  |
| `help`                     | 可选 | 悬浮`?` 提示文本（鼠标悬停显示）                                                        |
| `central`                  | 可选 | `True` 在集中设置面板显示；默认仅显示 `root_dir` 或有 `central` 标记的字段          |
| `min` / `max` / `step` | 可选 | number/range 类型约束                                                                     |
| `options`                  | 可选 | select 类型的选项列表                                                                     |

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

image-viewer 需要在不同文件夹应用不同设置（如行高、排序），这类设置不在 `settings_schema` 中，而是作为 `folders` 键（结构 `{__global__: {...}, "pixiv": {...}, ...}`）经 `update_setting('folders', ...)` 存入 SettingsStore（`.config/plugins/image-viewer.json`）。

前端调用的 `save_settings` API 实际注册为 `save_folder_settings`，支持两种形态（`save_folder_settings(settings_dict)` 保存全局，走 `super().save_settings()` 保持 schema 过滤与变更检测；`save_folder_settings(rel_path, settings)` 保存文件夹级，写入 `folders[rel_path]` 并剥离 `root_dir`）。

**逐级向上继承**：`get_settings(rel_path)` 按「当前文件夹 → 父文件夹 → … → 全局 → 硬默认」合并设置。在父文件夹（如 `pixiv`）上启用 `time_name` 排序后，其下**所有子文件夹自动继承**同一排序，除非某个子文件夹被单独修改（`folders[该路径]` 存在）——单独修改的子文件夹以自己为准，并继续向其子文件夹传播。

**Pixiv 排序只考虑两层嵌套**：`get_settings` 返回 `pixiv_explicit`（当前文件夹自身是否显式设置了 Pixiv 排序，即配置点）。配置点（如 pixiv 主文件夹）显示其子相册网格（作者卡片），继承 Pixiv 排序的子文件夹（作者层）才显示混合瀑布流（作品 p0 瓦片 + 连续浏览）。

### 8.6 运行时状态 vs 用户设置

| 类型       | 存放位置                           | 示例                         |
| ---------- | ---------------------------------- | ---------------------------- |
| 用户设置   | `.config/plugins/{name}.json`    | root_dir、字号、排序方式     |
| 运行时状态 | 数据目录`.cache/` 或插件状态文件 | 播放进度、收藏列表、扫描缓存 |

用户设置跟程序走（换机器拷贝程序即携带），运行时状态跟数据走（换数据目录自动重建）。

---

## 9. 调试与测试

1. **查看插件是否被加载**：启动主程序，控制台会输出 `[PluginManager] 加载成功: <name>`。
2. **检查前端资源**：在浏览器中直接访问 `http://127.0.0.1:18080/plugins/<name>/frontend/index.html`，确认能正常打开。
3. **检查 API 调用**：在插件前端控制台执行 `Bridge.call('method', ...)`，观察返回结果。
4. **查看 Flask 日志**：所有文件请求都会显示在控制台，便于排查 404 错误。
5. **检查注入是否成功**：在控制台输入 `typeof Bridge`，应返回 `"object"`；输入 `typeof createTree`，应返回 `"function"`。
6. **状态调试（--status-debug）**：以 `python main.py --web-only --status-debug` 启动后，访问 `/status` 显示壳内调试面板（健康检查 200 / API 鉴权 401 / 错误跳转演示），用于验证鉴权与错误页行为。
7. **一键调试环境**：`python tests/debug_status_pages.py` 自动起独立端口调试服务器（注入「坏插件」演示 iframe 404 → 壳内错误卡片链路），并用 requests 打印 11 个 HTTP 场景触发表，浏览器打开 `/status` 即调试面板；Ctrl+C 自动清理。
8. **HTTP 直连注意**：`/api`、`/file`、`/thumbs` 受令牌保护（见 §7.1），curl 测试需带 `X-Omnibox-Token` 头或先访问首页拿 Cookie；无 Cookie 客户端访问 `/thumbs/x.png` 会得到 401 而非 404。

---

## 10. 常见问题

| 问题             | 原因                                     | 解决                                                                                                |
| ---------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 导航栏不显示插件 | `manifest.json` 格式错误或缺少必填字段 | 检查 JSON 语法，确保`name`、`frontend.route` 等字段存在                                         |
| 点击导航无反应   | 前端路由未正确注入                       | 检查`frontend.route` 是否以 `/` 开头，且不与其他插件冲突                                        |
| iframe 白屏      | 前端入口文件不存在或路径错误             | 确认`frontend/entry` 指向的文件存在，且 Flask 能访问                                              |
| API 调用失败     | 方法名拼写错误或后端未注册               | 检查方法名是否与`register_api` 返回的键一致，调用时使用 `Bridge.call('method')`                 |
| 图片无法显示     | URL 路径错误或文件不存在                 | 确保图片通过`Bridge.thumbUrl()` 或 `Bridge.originalUrl()` 获取 URL，且文件在 `data_root` 下   |
| 缩略图不显示     | 缩略图未生成或路由错误                   | 推荐实现 `get_thumb_data()`（基于共享基建 `ThumbCache`，见 §3.4），由 `/thumbs` 路由按需生成；检查 `Bridge.thumbUrl()` 是否正确携带 `plugin` 参数，以及源文件是否在 `data_root` 下 |
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
- **共享基建**：需要后台长任务或缩略图缓存时，优先复用 `shell/backend/tasks.py`（`BackgroundTask`）与 `shell/backend/thumb_cache.py`（`ThumbCache`，见 §3.4），**不要各自重复实现**；DB / 任务状态文件放各自数据根目录 `.cache/` 下。
- **性能优化**：使用内存缓存（如目录列表缓存、聚合元数据缓存）减少 I/O，提升响应速度；大目录首次扫描可参考 image-viewer 的并行尺寸读取（`ThreadPoolExecutor`）。

---

按照本指南，你可以快速创建新插件，或将现有功能迁移到 OmniBox 架构中。快速起步可运行 `python tools/new_plugin.py my-tool` 一键生成骨架（模板见 `tools/examples/hello-world`）；如有疑问，请参考 `image-viewer` 示例插件。
