# OmniBox 插件开发指南

本指南将带你从零开始创建一个完整的 OmniBox 插件，并说明如何将现有的 `image-viewer` 插件迁移到新架构。

插件分为两类：**常规内嵌插件**（本指南主体）与**独立运行环境插件 / Companion 插件**（见 §2.1、§2.2 及 `docs/core-direction.md` §9 的「外部程序接入规范」）。

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
"icon": "icon:images",
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
"minShellVersion": "1.2.0"
}
```

> 示例里没有 `author`：代码不读这个字段（且 `tools/check_plugins.py` 会把它标为"运行时无效果"）。
> 需要署名请写在 `description` 或 README 里。

**字段说明**：

> 标记说明：`必填` 必需；`默认` 表示理论上应填写、但省略时运行时取缺省值（`name`→文件夹名、`backend.entry`→`backend/main.py`、`backend.class`→`Plugin`、`frontend.route`→`/<name>`，`name` 与文件夹名不一致仅告警）；`可选` 可完全省略。`tools/check_plugins.py` 仍做严格校验（用于发布前自检）。

| 字段                | 要求 | 说明                                                                            |
| ------------------- | ---- | ------------------------------------------------------------------------------- |
| `version`         | 必填 | 语义化版本号（`x.y.z`，`tools/check_plugins.py` 会强制校验；运行时只记录不判断兼容性） |
| `displayName`     | 必填 | 在导航栏显示的名称                                                              |
| `icon`            | 必填 | 导航栏图标，写壳图标集的 `icon:<名字>`（名字见 `res/icons/icon_data.json`；`tools/check_plugins.py` 拒绝非 `icon:` 前缀的值） |
| `frontend.entry`  | 必填 | 前端入口 HTML 路径。**壳目前固定加载 `frontend/index.html`**，此字段只被 `tools/check_plugins.py` 用于校验入口及其引用资源存在；写别的值不会改变壳实际加载的文件 |
| `name`            | 默认 | 插件唯一标识，缺省为文件夹名；与文件夹名不一致告警                              |
| `backend.entry`   | 默认 | 后端入口文件路径，默认`backend/main.py`，相对于插件根目录                     |
| `backend.class`   | 默认 | 后端插件类名，默认`Plugin`，须继承 `PluginBase`                             |
| `frontend.route`  | 默认 | 前端路由，默认`/<name>`，须以 `/` 开头                                      |
| `frontend.chrome` | 可选 | 页面骨架声明：`brand`、`nav`、`toolbar`、`search`、`status`、`freshness`、`extensions`。声明后由壳的 `PluginChrome.vue` 渲染品牌块、导航、顶栏、搜索框、状态栏与分隔条，iframe 加载时追加 `?content=1` 且只装内容；未声明则维持整页模式（旧插件兼容）。字段形状见 [插件页面架构](./plugin-page-architecture.md) |
| `frontend.chrome.extensions` | 可选 | `{placement: "sidebar", title}`：壳的 `PluginExtensions.vue` 从 `system_get_plugin_extensions` 取入口并渲染到导航区，宿主内容页不再自建扩展容器 |
| `dependencies`    | 可选 | 依赖的其他插件名称列表                                                          |
| `libs`            | 可选 | 插件本地附加库目录列表，默认`["backend/libs"]`，加载后端前会加入 `sys.path` |
| `permissions`     | 可选 | 权限声明，**仅作知情明示**：运行时不做强制、也不在设置页展示（代码里没有任何读取方） |
| `minShellVersion` | 可选 | 要求的最低 Shell 版本。**仅 `tools/check_plugins.py` 读取**：发布前自检时会拒绝高于当前 shell 的声明，但运行时既不告警也不拒绝加载（旧版 shell 会照常载入该插件） |
| `keepAlive`       | 可选 | `true` 时离开页面**不**卸载 iframe（`v-show` 隐藏，DOM 与 JS 状态保留、播放与朗读不中断）；**默认离开即卸载，回来重新加载**。见「前端生命周期」一节 |
| `hidden`          | 可选 | `true` 时不显示在 Shell 主导航，但仍可被宿主内嵌或通过插件 URL 访问           |
| `kind`            | 可选 | `local-adapter`：声明本插件管理独立运行环境（重依赖插件使用；**尚未实装，只告警不阻断**，当前声明不生效） |
| `runtime`         | 可选 | **规划中，尚未实装：填入不生效**，运行时没有任何读取方；独立运行环境声明（venv / 入口 / requirements），见 §2.2 |
| `description`     | 可选 | 仅作文档/说明：运行时与壳都不读（界面用 `displayName` + `icon`）              |

> **字段必须"有读取方"**：`tools/check_plugins.py` 会核对每个 manifest 字段是否真的被代码读取，
> 没有读取方且未登记的字段直接报错。新增字段时请同步更新 `tools/check_plugins.py` 里的三张表 ——
> 这条规则来自一个真实教训：指南曾经教了 4 个代码根本不读的字段，照文档写的作者只会得到"填了没效果"。
>
> | 表 | 含义 | 检查器行为 |
> | --- | --- | --- |
> | `RUNTIME_FIELD_READERS` | 运行时代码真的会读（登记读取方文件与源码片段，会自检过期） | 通过 |
> | `CHECKER_ONLY_FIELDS` | 只有检查器读：能拦住写错，但运行时无任何效果（如 `version` / `frontend.entry` / `minShellVersion` / `kind`） | warning |
> | `DOC_ONLY_FIELDS` | 纯文档字段，连检查器都不读（如 `description` / `author` / `permissions` / 未实装的 `runtime`） | warning |
>
> 登记必须"表里如一"：把检查器自己填进 `RUNTIME_FIELD_READERS` 会让这条规则空转满足
> （`minShellVersion` 曾长期如此）。整块字段（如 `runtime`）
> 登记一次即可，其子字段由父键回退匹配；未登记的字段（含层级更深的 `a.b.c`）仍按 error 处理。

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

**扩展注册表**：扩展条目由插件的 `get_extensions()` 返回，Shell 会自动补上 `plugin` 字段。
声明了 `frontend.chrome` 的宿主由壳的 `PluginExtensions.vue` 从
`system_get_plugin_extensions(host, 'sidebar')` 拉取并渲染入口，插件与宿主内容页都不需要写
渲染点。共享层已移除 `renderExtensions`（`base.js` 不再提供该函数），它不再是可调用的 API；
旧页面/旧壳如需回落，走条目的 `embedUrl`。真实键见 `plugins/image-cleaner/backend/main.py`：

```python
class ImageCleanerPlugin(PluginBase):
    def get_extensions(self):
        return [{
            'host': 'image-viewer',                            # 挂到哪个宿主
            'id': 'image-cleaner',
            'label': '相册清理',
            'icon': 'icon:brush-cleaning',
            'description': '扫描全部相册中的重复 / 相似图片',
            'section': '相册清理',                              # 侧边栏分组标题
            'native': {                                        # 新壳：壳的 NativePanel 渲染
                'url': '/plugins/image-cleaner/frontend/native.html?native=1',
                'title': '相册清理',
            },
            'embedUrl': '/plugins/image-cleaner/frontend/index.html',  # 旧壳回落
            'placement': 'sidebar',                            # 宿主渲染点位置
            'scope': 'all',
        }]
```

点击行为由条目里出现的键决定：

| 键 | 行为 |
| --- | --- |
| `native` | 原生面板型：壳的 `NativePanel.vue` 打开 `{url, title}`，隐藏逻辑页提交 schema blocks（新壳优先走这条） |
| `view` | 视图型：壳向宿主内容页发 `omnibox:view`，由内容页渲染对应视图 |
| `embedUrl` | 内嵌回落型：在宿主面板里打开 iframe；只给不认 `native` 的旧壳使用 |
| `route` | 独立路由型：跳转到插件自己的页面 |
| `method` | 纯后端方法型：`Bridge.callPlugin(ext.plugin, ext.method)` 跨插件调用 |

> 声明了 `frontend.chrome` 的宿主**不用写扩展渲染代码**：`PluginChrome.vue` 里的
> `PluginExtensions.vue` 自己拉取 `system_get_plugin_extensions(host, 'sidebar')` 并渲染入口，
> 点击后由壳决定打开 `NativePanel` 还是下发 `omnibox:view`。插件只需要在 `get_extensions()`
> 里声明条目，不要自己渲染入口或另建扩展容器。`renderExtensions` 已从
> `shell/frontend/public/shell/base.js` 删除，不再是可调用的共享 API；旧页面/旧壳如需回落，
> 走条目里的 `embedUrl`。

> **内嵌回落时必须追加 `?embed=1`**（`embedUrl` 与"网络位置"提供方页同理，见
> `shell/frontend/public/shell/folder-picker.js` 的 `openNetworkPicker`）：插件页据此知道
> "我正被嵌在宿主面板里，宿主已经给了标题与返回"，从而收起自己的工具栏/标题。只看
> `window.self !== window.top` 分辨不出来 —— 壳按路由加载插件时也在 iframe 里。
> 约定与示例见 `docs/plugin-ui-guide.md`。

> Shell **没有** `addToolbarButton(...)` 这个函数（历史文档里出现过，属于笔误）。

**扩展入口的"同一时刻只有一个选中项"由壳维护**：`PluginExtensions.vue` 只负责渲染与上报点击，
选中态由 `PluginChrome.activeExtensionId` 统一持有 —— 点击扩展入口时点亮该入口并清掉主导航
高亮，点击主导航时清空扩展选中态，native 面板关闭时同样清空；`view` 型入口高亮期间主导航
不叠加高亮。插件侧不需要维护按钮状态；旧实现返回的 `{ buttons, activate, clearActive }`
控制器已随 `renderExtensions` 一起删除。

> 目前已落地的 Companion 插件示例：`image-cleaner`（全相册重复/相似清理）与 `pixiv-sync`
> （Pixiv 同步），都走 `native` 面板 + `embedUrl` 旧壳回落。

## 2.2 重型依赖与独立运行环境（runtime）

> **本节的 `runtime` 块尚未实装，填入不生效。** 运行时（`shell/backend/`）没有任何代码读取它，
> 壳也不会按 `venv` 拉起独立环境；`tools/check_plugins.py` 会对应地报一条"运行时无效果"的 warning。
> 下面描述的是 `docs/core-direction.md` §3.4 / §3.5 的规划方案，实装前请按第 5 条用懒 import 兜底，
> 不要依赖 `runtime` 生效。

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
3. **通信**：stdio JSON-lines，每行一个 JSON 对象；控制行下行、进度/结果上行（协议见 `docs/core-direction.md` §9.4）。
4. **部署**：`deploy.ps1` / `setup-venv.ps1` 按 `runtime.requirements` 创建插件独立 venv；失败不阻塞主程序。
5. **原型阶段**允许懒 import 兜底：首次调用时才 `import torch`，并把任务放进后台线程；正式版切独立环境。

---

## 2.3 页面骨架、内容归属与 native 面板

页面的品牌块、侧栏导航、顶栏、搜索框、状态栏与分隔条属于**壳的 chrome**，不归插件前端。manifest
声明 `frontend.chrome` 后：

- 壳用 `PluginChrome.vue` 渲染骨架，插件 iframe 加载 `frontend/index.html` 时由壳追加 `?content=1`；
- 内容页只保留内容 DOM，不得出现 `.view-sub-sidebar`、`.view-toolbar`、`.sub-sidebar-brand`、
  `.obx-nav-item`（`tools/plugin_ui_audit.py` 的 `iframe.content-only` 会检查）；
- 导航点击、顶栏动作、搜索输入由壳以 `omnibox:view` / `omnibox:action` / `omnibox:query` 下发；
  内容页用 `omnibox:chrome` 上报标题、视图、动作、导航、搜索与状态；
- 设置弹窗、统一刷新控件与扩展入口也归壳（见下文与 `docs/plugin-page-architecture.md`）。

`frontend.chrome` 的字段形状（`brand` / `nav` / `toolbar` / `search` / `status` / `freshness` /
`extensions`）与运行期补丁语义以 [插件页面架构](./plugin-page-architecture.md) 为准；
手写前可对照 `plugins/media-player/manifest.json` 的完整声明。

`frontend.chrome.content` 字段已移除（`canvas` / `native` / `hybrid` 不再有运行时效果）：壳按
"是否声明 `frontend.chrome`"决定是否渲染页面骨架，按附属插件 `get_extensions()` 是否返回
`native` 决定是否渲染 native 面板。native 内容来自附属插件 `get_extensions()` 返回的条目：

```python
{
    'host': 'image-viewer',
    'id': 'image-cleaner',
    'label': '相册清理',
    'section': '相册清理',
    'native': {'url': '/plugins/image-cleaner/frontend/native.html?native=1', 'title': '相册清理'},
    'embedUrl': '/plugins/image-cleaner/frontend/index.html',  # 旧壳回落
    'placement': 'sidebar',
}
```

新壳按 `native` 打开 `NativePanel.vue`：逻辑页在隐藏 iframe 里只提交 schema blocks 与处理动作，
可见界面由 `SchemaRenderer.vue` 渲染；`embedUrl` 只给不认 `native` 的旧壳做整页回落。附属插件
通常同时声明 `hidden: true`，不进入壳的主导航。设置能由 `settings_schema` 完整表达的插件，顶栏
「设置」由壳的 `PluginSettingsModal.vue` 打开（`group-mesh`、`document-reader`、`manga-library`、
`media-player`，以及 `image-cleaner` / `pixiv-sync` 的 native 顶栏设置动作）；image-viewer 的设置
带按目录作用域与根目录列表控件，仍由内容页的 `openSettingsModal()` 渲染。

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

    # ===== 文件与缩略图契约（Shell 的 /file、/thumbs 路由直接调用，见 §7.3） =====

    def get_file_roots(self) -> List[Path]:
        """允许 /file 访问的根目录列表。跨多根插件覆写（如 media-player）"""

    def get_thumb_data(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        """返回 (bytes, mime) 供 /thumbs 直接响应；默认 None = 不提供字节"""

    @property
    def thumb_dir(self) -> Path:
        """缩略图散文件目录，默认 数据根目录/.cache/thumbs"""
        # 兼容旧写法：self.thumb_dir = <path>（property 带 setter，读取始终走 property）

    def ensure_thumb(self, rel_path: str) -> None:
        """按需生成缩略图（/thumbs 找不到文件时调用）。默认空实现"""
```

> 四个文件/缩略图成员是**基类契约**，宿主侧不再用 `getattr` 探针读取。宿主与附属插件
> 之间的这部分约定由 `tests/test_plugin_host_contract.py` 固定形状（见 §7.3）。

### 3.2 编写插件类

```python
# plugins/my-plugin/backend/main.py
from pathlib import Path
from shell.backend.plugin_base import PluginBase

class MyPlugin(PluginBase):
    # 声明设置项：声明 frontend.chrome 的插件由壳的 PluginSettingsModal 渲染，
    # 保存后内容页收到 omnibox:action: settings-saved；SettingsStore 持久化由框架自动处理
    settings_schema = [
        {"key": "root_dir", "label": "数据根目录", "type": "text",
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

### 3.4 共享基建：后台任务、缩略图缓存与统一刷新

媒体类插件经常需要三类通用能力，Shell 已抽出共享模块（`shell/backend/`），**不要各自重复实现**：

- **`tasks.py` → `BackgroundTask`**（控制面）：后台线程 + 状态机 + 取消 + 进度 + 可选断点持久化。适用于任何长任务：全量重建、同步、批量下载、导入扫描。
- **`thumb_cache.py` → `ThumbCache`**（数据面）：SQLite 缩略图缓存（WAL + mtime/size 失效校验 + 并行批量生成）。适用于任何「本地媒体 → 缩略图」：图片、视频封面抽帧、音频内嵌封面。
- **`freshness.py` → `FreshnessEngine`**（决策面）：什么时候扫、哪些算变化、什么算幽灵。适用于任何"本地资源 → 派生数据"的插件。**接入方式见「统一刷新（同步 / 校验）」**。

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

前端回写（如浏览器 canvas 抽帧生成视频封面）用 `put()` 直接入库，不经过生成器；
源文件 mtime/size 一并记录，失效校验语义与 `get()` 一致（源文件替换后条目自动失效）：

```python
ok = self.thumb_cache.put(rel, jpeg_bytes, 'image/jpeg', self.root_dir / rel)
```

要点：

- 失效校验：`source_mtime`（0.5s 容差）+ `source_size` 双条件，源文件替换后自动重生成；
- 生成失败返回 None 且不写缓存（不缓存假缩略图）；
- `size`、MIME 映射（`mime_map`）、并发数（`workers`）均可配置，默认 300×300 + 8 线程；
- 已有实现参考：image-viewer（`ThumbCache` 接入 + `BackgroundTask` 重建任务）。

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

#### 统一刷新（同步 / 校验）

**任何「本地资源 → 派生数据」的插件都不该自己写刷新逻辑。** 判据与编排——去抖、单飞、
增量短路、幽灵清理、进度、状态——收在 `shell/backend/freshness.py`，界面与轮询收在
共享组件 `shell/frontend/public/shell/freshness.js`。全壳只有两个动作、两个词：

| 动作 | 触发 | 成本 | 谁负责 |
| --- | --- | --- | --- |
| **同步**（sync） | 被动：进视图 / 切目录 | 目录级短路，稳态每目录一次 `os.scandir`、零 `stat` | 壳（`system_freshness_sync`） |
| **校验**（verify） | 手动：工具栏唯一按钮 | 全量遍历 + 逐项指纹校验 + 幽灵清理 + 派生缓存对账 | 壳（后台任务，有进度可取消） |

改动前的实际状况是同一件事被各写了一遍、判据互不相同：目录 mtime、30 秒 TTL、
"永不失效"、"只增不减"（删除的文件永久留在索引里）、"每次全盘对比"。名字也散成
刷新 / 全量重建 / 重新扫描 / 扫描 / 深度扫描 / 刷新记录 / 校验内容。另外壳的 `/thumbs`
带 `Cache-Control: max-age=86400` 而 URL 没有版本号 —— 重建了缩略图，浏览器一天不回源
（media-player 曾单独在 URL 上打 `&v=<mtime>` 绕开）。这些现在都由基建统一处理。

**插件只回答三个问题**（`PluginBase.freshness_spec()`；不声明 = 不参与，行为不变）：

```python
# plugins/my-plugin/backend/freshness.py（分片，与 main.py 同目录，与 §3.1 的类骨架同形）
class FreshnessMixin:
    def get_cache_dir(self):          # 可选：默认 <数据根>/.cache/<插件名>
        return self.cache_dir         # 指纹库放这里：freshness.db

    def freshness_spec(self):
        return {
            'roots': lambda: self._roots(),   # ① 管哪些文件（可调用 = 跟着设置走）
            'prefixes': lambda: (...),        #    多根时的虚拟前缀（默认第一根 ''、其余 __<目录名>）
            'include': ('.jpg', '.png'),      #    关心的后缀（空 = 全部常规文件）
            'content_version': 4,             # ② 解析规则版本：变了就整体作废派生数据
            'derive': self._on_changed,       # ③ 条目新增/变化时做什么（只对变化项调用）
            'prune': self._on_removed,        #    条目消失时清派生缓存
            'on_verified': self._on_verified, #    全量校验后按"有效键集合"对账孤儿
            'rebuild': self._clear_derived,   #    逃生门：丢弃派生缓存（设置页入口，管理员）
            'stat_entries': True,             #    目录不大且"替换要立刻反映"时开（见下）
            'unit': '张',
        }
```

条目键 = `前缀/根内相对路径`，与插件对外用的路径同形 —— 派生缓存的键（缩略图键、
索引键）可以直接复用，不需要额外映射表。三个钩子都**只做缓存失效/清理，不做重活**：

```python
def _on_changed(self, items):     # items: [{key, path, mtime, size, dir}, ...]
    self._list_cache.clear()      # 派生数据按需重算才是最快路径
def _on_removed(self, keys):
    for rel in keys:
        self.thumb_cache.delete(rel)
def _on_verified(self, keys):     # keys: 当前全部有效条目键（整趟无错时才调用）
    return {'rows': self.thumb_cache.prune(keys)}
```

前端挂载共享组件（**不要**自己写按钮与轮询）：

```html
<div id="mp-freshness"></div>
```
```javascript
const ctl = Freshness.mount({
    plugin: 'my-plugin',              // 必须与 manifest.name 一致（门禁会拦）
    container: document.getElementById('mp-freshness'),
    unit: '首',
    onChange: () => this.reload(),    // 同步发现变化 / 校验完成后重载自己的视图
});
ctl.autoSync('');                     // 进视图 / 切目录时调用（组件去抖，壳侧再节流）
```

两条硬约束：

1. 派生资源（缩略图等）的 URL **必须带版本号**，否则壳的 `/thumbs` 一天不回源：
   `Freshness.assetUrl(path, Math.round(item.mtime))`。版本号取源文件 mtime 即可
   （文件被替换 → mtime 变 → URL 变）；指纹没变的强制重建请传单调递增值。
2. 三种静默失败由 `tools/check_plugins.py` 静态拦下：只声明 `freshness_spec()` 而没挂
   组件（界面上没有入口）、挂了组件却没声明 spec（点了没反应）、`plugin` 名字与
   manifest 不一致（壳按名字找不到实例，同样"点了没反应"）。

内部设计要点（写插件时用得上的部分）：

- **同步**：每个目录一次 `os.scandir`，目录 mtime 与"纳入索引的名字个数"都没变就整目录
  跳过（零 `stat`）；变了才逐条比对指纹，只把新增/变化的条目交给 `derive`。
- **边界**：原地写入（同名、新内容）不改目录 mtime 也不改名字个数，被动同步**看不见**它。
  派生缓存自己按 mtime/size 失效兜住多数场景，权威结论由校验给出。目录不大、
  且"文件被替换必须立刻反映"的插件（如 image-viewer 的一个相册几十张）开
  `stat_entries: True` 换准确度；十几万条目的大媒体库保持默认 `False`。
- **校验**：全量遍历（**不做目录短路**，否则原地写入永远查不出来）+ 逐项 stat；
  消失的条目与目录 → `prune`；整趟走完且**没有读取错误**时 → `on_verified(有效键集合)`，
  用于清理"插件没运行时就被删掉的文件"留下的历史孤儿（缩略图库防膨胀）。
- **`content_version` 收敛**：把散落的 `INDEX_VERSION` / `_ALBUM_CACHE_VERSION` /
  `parser_version` 传进来，改解析规则时改它，基建负责整体失效 —— 不再依赖"记得手动 +1"。
- **并发与开销**：单飞（同一插件同时只有一个同步/校验）、去抖（`min_sync_interval`，
  默认 5 秒；设置变更后调 `engine.reset_throttle()` 让下次同步立刻真扫）、预算
  （单次被动同步最多 `max_dirs_per_sync` 个目录，超出返回 `partial`，前端会转成一次
  后台校验续完，用户不必自己点）。
- **`partial` 的两种原因**：被动同步有预算（`max_dirs_per_sync`）。预算按
  **真正干了活的目录**算，走过的短路目录不算 —— 按走过的算会让"目录数 > 预算"的库
  每次进视图都报 `partial`，界面每次都提示"同步未覆盖全部目录"，而稳态下一个目录都
  没变。报告里的 `reason` 区分两种收尾：`budget`（还有目录要处理，值得替用户起一次
  校验续跑）与 `dir_cap`（硬上限 8×预算，只是没走完，不代表还有活 —— 不该反复起任务）。
- **进度必须对每个走过的目录推进**：只在"条目有增删改"的分支里更新任务计数，会让
  稳态下的校验整趟停在 `0 / 0`（进度条不动，看起来像卡死）。
- **状态查询不建库**：指纹库不存在时 `system_freshness_state` 直接返回零值，
  "看一眼状态"不会凭空造出数据库（同 `ThumbCache.stats` 的理由）。
- **完成 = 立即可读**：钩子把派生数据"标记为过期"是惰性的，过期后的第一次读取
  才是真正花钱的地方。它若留在任务外，用户看到的就是"进度条已经满了、界面还要
  卡几秒再出内容"（image-viewer 实测：6000 张 / 1400 目录上，校验结束后第一帧
  重建相册索引 2.7s）。因此**手动校验的收尾重活必须在 `on_pass_end(report,
  verified=True)` 里做完**：宁可让"校验中"多显示这几秒，也不要让"完成"之后再卡。
  钩子里拿不到任务对象，需要提示当前阶段时调 `engine.progress('重建相册索引')`。
  被动**同步**没有任务可挂（它是一次同步请求），那边的要求相反：同步路径必须便宜，
  重活留给校验或按需重算。
- **`stat_entries` 只在小目录树打开**：它把"目录级短路"降级成"每个文件 stat 一次"，
  也就是把"进一次页面"变成"给整库每个文件 stat 一次"。实测 6000 个文件的整树同步
  0.11s → 0.29s（真实磁盘冷缓存下差距更大）。它换来的是"原地改写文件也能被同步
  发现"，而这件事本来就该由校验负责（audit 逐项比对指纹）。只有"文件数是几十到
  几百量级"的插件（如 document-reader 的书库）才值得打开。
- **远端来源**（`mode: 'remote'`）：数据不在本地文件系统里（netease-music 的歌曲 /
  歌单就是这一类），**没有指纹可用，判据只能是时间**。此时声明 `ttl_seconds` 与
  `invalidate`（丢弃插件自己的本地缓存，例如短期播放地址），不需要 `roots`：
  「同步」= 新鲜期内跳过、过期才 `invalidate`；「校验」= 忽略 TTL 立刻 `invalidate`；
  状态行按"新鲜期剩余"显示而不是"已索引 N 项"。注意语义边界：`sync` 是"到时重取"，
  不是"发现变化" —— 远端有没有变，只有真去请求才知道，而取数仍由插件在用户进入
  相应视图时按需发起，`invalidate` 只负责让那次取数不再命中旧缓存。

壳级端点（插件**不注册**自己的刷新 API）：

| 端点 | 语义 | 权限 |
| --- | --- | --- |
| `system_freshness_state(plugin)` | 状态投影（不扫盘） | 普通 |
| `system_freshness_sync(plugin, scope)` | 被动增量（去抖 + 单飞） | 普通 |
| `system_freshness_verify(plugin)` | 起后台全量校验 | 普通 |
| `system_freshness_cancel(plugin)` | 取消校验 | 普通 |
| `system_freshness_rebuild(plugin, kind)` | 丢弃派生缓存后重算（逃生门） | 管理员 |
| `system_freshness_overview()` | 全部参与插件的状态快照 | 管理员 |

`scope` 来自前端，按不可信输入处理：`..`、绝对路径、未知命名空间一律拒绝，解析结果
还要再确认落在该根之内。方法表只有一处定义（`plugin_manager.freshness_api_methods()`），
HTTP 与桌面 `js_api` 两条通道共用。已接入的实现参考：image-viewer
（`plugins/image-viewer/backend/freshness.py`）。

### 3.5 完整 API 列表

（以 image-viewer 为例）

| API 方法                  | 参数                                              | 返回值                                                                                      | 说明                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_images`           | `rel_path, page, per_page, sort_by, sort_order` | `{images, page, total, has_next, has_prev, settings}`                                     | 获取图片列表（含尺寸缓存）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `list_folder_items`     | `rel_path, page, per_page, sort_by, sort_order` | `{items, all_images, all_truncated, all_offset, page, total, image_total, has_next, has_prev, settings}` | 混合瀑布流列表：直接图片 + 直接子相册 p0 瓦片（只处理一层嵌套）；`all_images` 为按瀑布流顺序展开的完整连续浏览序列（上限 5000 条，超出返回 `all_truncated: true`），每个子文件夹内部按**它自己生效的设置**排序（自己有设置用自己，否则逐级继承父级），不受当前视图排序影响；`all_offset` 为当前页首项在完整序列中的起始偏移（分页对齐，防止灯箱错位）；卡片 `use_time_name` 标记该瓦片是否启用「Pixiv 排序支持」（控制圆圈数量角标显示）；`sort_by=time_name`（Pixiv 排序支持）时：顶层作品/单图按**前导数字**（作品 ID）排序且方向生效（倒序 = 新作品在前），多图作品（子文件夹）与单图（平铺文件）在同一个序列里混排（不是先文件夹后单图），无数字名排最后，作品内部多图片仍按 p0→p1（`is_pixiv_work_dir` 判定当前目录自身是作品时整层按页序） |
| `list_albums`           | 无                                                | `{albums, config, changed, cached?}`                                                      | 相册全量索引（增量扫描 + 30s TTL 内存缓存，`cached` 标记命中）；`config` 为 `{collapsed, promoted}` 收纳/提升配置                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `create_folder`         | `rel_path`                                      | `{success, path \| error}`                                                                  | 根目录（或指定相对目录）下新建相册文件夹（路径安全校验）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `get_album_config`      | 无                                                | `{collapsed, promoted}`                                                                    | 相册收纳/提升配置                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `set_album_config`      | `rel_path, action`                              | `{success, config}`                                                                        | `action ∈ collapse/expand/promote/unpromote`；变更后失效相册 TTL 缓存                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `get_image_info`        | `rel_path`                                      | `{success, rel_path, size, width, height \| error}`                                        | 单图存储大小与分辨率（全屏查看器右侧信息面板）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `regenerate_thumbs`     | `[rel_paths]`                                   | `{regenerated, errors}`                                                                   | 重新生成选中图片的缩略图（删除缓存并重建，修复坏缩略图）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `refresh`               | 无                                                | `{success, ...report}`                                                                    | **兼容入口**：转发到壳统一刷新的「同步」（重新扫描目录，新增/替换图片立即生效）；新代码用 `system_freshness_sync` / `system_freshness_verify` |
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

## 4. 前端开发

### 4.1 Shell 注入的资源

插件前端运行在 iframe 中。声明了 `frontend.chrome` 的插件，壳加载内容页时追加 `?content=1`，
iframe 只装内容；没有 chrome 声明的旧插件仍是整页模式。Shell 对两种形态都会自动注入以下资源：

- **CSS 变量**：`/shell/variables.css`（提供主题颜色、间距等）
- **共用组件样式**：`/shell/shared.css`（按钮、表单控件、Toast、图标与壳侧导航项 —— 壳页面与
  插件页同一份，见 `docs/plugin-ui-guide.md`）
- **基础样式**：`/shell/base.css`（内容页的布局基线与插件侧独有的组件；页面骨架样式在壳的 `plugin-page.css` 里）
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
  - 现代窄滚动条（鼠标悬停渐显、深浅色兼容）：`.obx-scroll` —— **每个会滚动的容器都要加**。
    壳自己的界面里滚动条是全局隐藏的（`src/styles/shell.css` 的
    `*::-webkit-scrollbar { display: none }`），插件 iframe 不在那份样式的作用域内，
    插件不写就退回系统默认滚动条（又粗又白）。弹窗正文 `.modal-body` 与共享目录组件的
    `.iv-dirbrowser-list` 由壳渲染、插件加不上类，已在 `effects.css` 里一并覆盖。
- `/shell/motion.js` — `window.Motion`：
  - `Motion.stagger(container, selector?)` 为子元素写入交错延迟
  - `Motion.retrigger(el, className?)` 重新触发动画（默认 `obx-anim-heart`）
  - `Motion.show(el, className?)` 淡入上移显示

另外两个共享**组件**（不只是资源，见「全局 UI 组件」）：

- `/shell/folder-picker.js|css` — `window.FolderPicker`：多位置文件夹列表与目录选择弹窗；
- `/shell/freshness.js|css` — `window.Freshness`：统一的「同步 / 校验」控件（状态行 +
  校验按钮 + 进度卡），接入方式见「统一刷新（同步 / 校验）」。

所有动画均遵循 `prefers-reduced-motion`，用户开启减少动态效果时自动降级。

> 提交前 `python tools/check_plugins.py` 会校验这些契约：引用未声明的 CSS 变量、使用原生
> `alert()`/`confirm()` 是 error；`!important`、与壳逐值相同的关键帧、用 `obx-` 前缀命名
> 插件自有动画是 warning。逐条依据见 `docs/plugin-ui-guide.md`。

#### 内容页继续使用的通用类

页面骨架类不再属于插件：`.view-sub-sidebar`、`.view-toolbar`、`.toolbar-group`、
`.sub-sidebar-brand`、`.obx-nav-item` 由壳的 `PluginChrome.vue` 渲染，内容页出现它们会被
`tools/plugin_ui_audit.py` 的 `iframe.content-only` 判为失败（双份 chrome）。内容页仍可继续
使用壳注入的以下类，但**不要**在自己的 CSS 中重复定义：

- `.view-body` / `.view-content`：内容 iframe 自身的布局与滚动区；
- `.empty-state`（含 `.empty-state-icon` / `.empty-state-text` / `.empty-state-hint`，小容器加
  `.empty-state--inline`）：空态，不要自建 `.xx-empty`；
- `.obx-scroll`：现代窄滚动条，**每个会滚动的容器都要加**；
- `.obx-anim-*` / `.obx-glass*` / `.obx-skeleton` / `.obx-card-lift`：通用动效与玻璃/骨架/
  悬浮类（`effects.css`），已处理 `prefers-reduced-motion`；
- `.obx-extension*` / `.obx-embed-frame`：旧壳回落页的历史类，当前壳（`PluginExtensions` /
  `NativePanel`）不再产出；新页面不要使用。

### 4.2 页面 chrome 与内容 iframe

manifest 的 `frontend.chrome` 声明品牌、导航、顶栏、搜索、状态与扩展入口。壳据此用
`shell/frontend/src/components/PluginChrome.vue` 渲染整页骨架，并把内容 iframe 放进内容槽：

- 壳加载内容页时追加 `?content=1`（`App.vue` 的 `pluginSrc`）。内容页只需处理自己的内容，
  例如 group-mesh 的 `.view-content` 面板、media-player 的舞台与播放条。
- 内容页**不得**再出现 `.view-sub-sidebar`、`.view-toolbar`、`.sub-sidebar-brand`、
  `.obx-nav-item`，也不要在插件 CSS 里复刻品牌块与导航。
- 标题、副标题、视图高亮、动作显隐、导航徽标、搜索开关与状态行都通过
  `omnibox:chrome` 上报；壳只合并声明与补丁，不解释业务。
- `PluginChrome` 还负责挂载统一刷新控件（`freshness`）、扩展入口（`extensions`）与侧栏分隔条。

### 4.3 设置弹窗由壳渲染

设置是页面级弹窗：内容 iframe 里的 `.modal` 只相对自己的视口，盖不住壳的侧栏。因此：

- `group-mesh`、`document-reader`、`manga-library`、`media-player` 的顶栏 `settings` 动作由
  壳拦截（`App.vue` 的 `SHELL_SETTINGS_PLUGINS`），用
  `shell/frontend/src/components/PluginSettingsModal.vue` 渲染；
- 弹窗的 schema、初值与保存全部走 `<插件>__get_settings_schema`、`<插件>__get_settings`、
  `<插件>__save_settings`，壳不持有设置语义；保存成功后壳发
  `omnibox:action`（`settings-saved`），内容页据此重读设置并刷新当前视图；
- `settings_schema` 的 `type: "directory"` 字段在壳弹窗里渲染成「路径行 + 选择目录…」，由壳内共享
  `window.FolderPicker`（`shell/frontend/public/shell/folder-picker.js`）提供：可手输绝对路径，
  「浏览…」走 `system_browse_dir`（共享基建 `media_catalog.list_subdirectories`，可看盘符与
  「含图片 / 视频 / 音乐」）；`local_only: true` 的字段不显示「网络位置」按钮，其余字段可由声明了
  network-location 能力的插件把远端共享项取到本地目录后回填。插件目录字段不再使用
  `system_pick_path('directory')` 系统对话框 —— 该接口只剩壳自己的设置页选图片；
- image-viewer 仍是例外：它的设置按目录作用域读写、还带根目录列表控件，由内容页调用
  `openSettingsModal()` 渲染。其余插件不要再调用 `openSettingsModal()`，也不要自己拼设置弹窗；
- `image-cleaner` / `pixiv-sync` 的 native 设置也由壳的 `PluginSettingsModal` 接管：原生面板顶栏
  的「设置」动作由壳拦截，弹窗按插件 `settings_schema` 渲染，保存成功后向隐藏逻辑页发
  `settings-saved`。附属插件的弹窗不显示外观块（它们没有自己的页面骨架）。
- 四个壳内设置页与 image-viewer 的内容设置页顶部都有通用「外观」块：背景不透明度 +
  「跟随壳透明度」。值存在壳偏好 `--plugin-glass-<插件名>`，不写进插件 `settings_schema`；
  image-viewer 通过 `omnibox:get-plugin-appearance` / `omnibox:set-plugin-alpha` 读写同一份值。
  默认跟随壳的 `--glass-alpha`，覆盖范围是页面骨架与内容页公共区域，不改变插件自绘卡片。

> 覆写了 `get_settings()` 的插件必须把它登记进 `register_api()`（连同 `save_settings`），
> 否则壳弹窗只会显示 schema 默认值、保存也会失败。

### 4.4 前端生命周期（onShow / onHide / onDispose）

插件 iframe **默认随路由卸载**：切到别的插件时壳把 iframe 整棵卸掉，再进来是一次干净的重新
加载，前端 JS 状态不保留。需要"切走不中断、回来接着用"的插件在 manifest 里声明
`"keepAlive": true` —— 壳改用 `v-show` 隐藏，DOM 与 JS 状态都保留，媒体播放与 TTS 朗读
不会因为切走而中断。申请它要有一份说得清的代价理由：播放/朗读不能断、要持续上报状态、
首屏加载很贵（仓库里的 `media-player`、`document-reader`、`group-mesh`、`image-viewer`、
`manga-library` 都属于这几类）；代价是切走以后插件的定时器、`requestAnimationFrame`
自循环与轮询仍在运行，而且插件前端无从知道自己的 iframe 是否可见。

壳因此在可见性变化时通知插件，`base.js` 提供三个注册入口：

| 入口 | 触发时机 | 典型用途 |
| --- | --- | --- |
| `onShow(fn)` | iframe 由隐藏转为显示（含注册时已可见的情况，会立即补一次） | 恢复轮询 / 继续动画 / 暂停过的进度刷新 |
| `onHide(fn)` | iframe 由显示转为隐藏（保活插件切走、窗口最小化、切换标签页） | 停 `setInterval`、停 rAF 自循环、停轮询 |
| `onDispose(fn)` | iframe 即将销毁（不保活的插件离开时、任何插件在页面卸载时） | 摘掉 `window`/`document` 上的监听器、释放资源 |

```javascript
// 三个入口同时挂在 window 上，也等价于 window.PluginLifecycle.onShow 等
onHide(() => {
  clearInterval(this._pollTimer);   // 后台没有必要继续轮询
  this._pollTimer = null;
});
onShow(() => {
  if (!this._pollTimer) this._pollTimer = setInterval(() => this.poll(), 2000);
});
onDispose(() => {
  window.removeEventListener('resize', this._onResize);
});
```

**约定与注意事项**：

- **`onHide` 的语义是"停止视觉与轮询类工作"，不是"停止播放"。** 申请 `keepAlive` 正是为了让
  媒体播放在切换时不中断，因此 `media-player` 在 `onHide` 里只停 `lyrics-parser` 的 rAF 自循环，
  保留 `player-core` 的播放进度保存；是否暂停由插件自行决定。
- **只发状态真变化的通知**，重复的 `shown` / `hidden` 不会重复触发；钩子抛异常只记日志，
  不会影响其它钩子与宿主。
- **注册顺序无关紧要**：`base.js` 先于插件脚本注入，插件在脚本顶层注册即可；
  若注册时 iframe 已经可见，`onShow` 会立即执行一次（常见写法"进入即恢复"无需额外处理）。
- **消息来源经过校验**：只接受父窗口直接发来的消息（仅校验 `origin` 不足以拦截同源嵌套
  frame）。插件不需要自己监听 `message`。
- **`keepAlive` 与生命周期是互补的**：默认（不声明）离开即卸载重载，壳会在卸载前发
  `onDispose`，这类插件只会收到 `dispose`；`"keepAlive": true` 的插件则会在切走 / 最小化时
  收到 `onHide`、回来收到 `onShow`，`onDispose` 只在页面卸载时到达。
- **迁移范例**：`plugins/image-viewer/frontend/js/app.js` 的 `_bindPluginLifecycle()`
  在 `onHide` 里停掉幻灯片定时器并记住位置、`onShow` 原位继续（这两条覆盖窗口最小化 /
  切标签页；切插件离开时 iframe 直接卸载，收尾走 `onDispose`）、`onDispose` 摘掉 `resize`
  监听器（该文件此前是 `addEventListener` 35 : `removeEventListener` 0）。
  回归用例见 `tests/js/image_viewer_lifecycle.mjs` 与 `tests/js/plugin_lifecycle.mjs`。

### 4.5 搜索框

搜索框由壳持有，插件不另建搜索输入：

- 壳的输入经 `omnibox:query` 下发（250ms 防抖）；
- 内容页在 `omnibox:chrome` 里用 `search.value` 回填输入框、用 `search.clear`（**递增序号**）
  清空。切分类、进二级页、打开整页设置等场景要清空，否则上次搜的词留在框里与列表不一致；
- `search.enabled` 控制是否显示搜索框；group-mesh 的 manifest 没有 `search` 声明，因此壳不渲染
  搜索框，内容页也不要自己补一个。

### 4.6 chrome 消息

壳与内容 iframe 之间只通过同源 `postMessage` 通信：

| 方向 | 消息 | 用途 |
| --- | --- | --- |
| 壳 → 内容 | `omnibox:view` | 切换视图；复合 id（如 `downloads:all`）由内容自行解释 |
| 壳 → 内容 | `omnibox:action` | 顶栏动作，可带 `payload`；声明 `frontend.chrome` 且设置由壳接管的插件不会收到 `settings` |
| 壳 → 内容 | `omnibox:action`（`settings-saved`） | 壳内设置弹窗保存成功 |
| 壳 → 内容 | `omnibox:query` | 搜索输入 |
| 内容 → 壳 | `omnibox:ready` | 握手，壳补发当前视图 |
| 内容 → 壳 | `omnibox:chrome` | `title` / `subtitle` / `view` / `actions` / `nav` / `search` / `status` / `statusItems` 补丁 |
| 内容 → 壳 | `omnibox:open-settings` | 内容页自己的设置入口请求壳开弹窗（可选 `title`） |

`omnibox:chrome` 的 `search` 支持 `value`（回填）与 `clear`（递增序号清空）；`status.kind =
eq-text` 且内容页上报 `state: "ok"` 时，壳把状态点换成四柱迷你 EQ（media-player 使用）。消息的
完整字段与壳端合并规则见 [插件页面架构](./plugin-page-architecture.md)。

### 4.7 native 内容协议

附属插件（`hidden: true`）的 `get_extensions()` 返回 `native: {url, title}` 时，壳用
`shell/frontend/src/components/NativePanel.vue` 打开面板：`url` 指向一个**不产生可见界面**的
逻辑页（通常叫 `frontend/native.html`），可见内容由壳的
`shell/frontend/src/components/SchemaRenderer.vue` 按 blocks 渲染。

| 方向 | 消息 | 用途 |
| --- | --- | --- |
| 逻辑页 → 壳 | `omnibox:native-ready` | 就绪 |
| 壳 → 逻辑页 | `omnibox:native-init` | 开始取数并提交 blocks |
| 逻辑页 → 壳 | `omnibox:schema` | `{blocks}` |
| 壳 → 逻辑页 | `omnibox:native-action` | `{action, payload}`，payload 发送前做 JSON 克隆 |
| 逻辑页 → 壳 | `omnibox:native-error` | 内容加载失败 |

blocks 的组件词汇（`shell/frontend/src/core/plugin-content.ts`）：`text`、`heading`、`kv`、
`table`、`list`、`cards`、`checkbox-list`、`form`、`actions`、`progress`、`empty`、`image-grid`。
规则：

- native 内容不接收任意 HTML；交互只回传动作标识，壳不解释业务；
- 逻辑页统一装载 `/shell/native-client.js`，用 `NativeExtension.create({ onInit, onAction, onDispose })`
  处理 ready/init/action/schema/error 与 pagehide；插件只提供业务取数与 `buildBlocks()`，
  不再各自手写消息骨架；
- 页面级动作用 `render(blocks, toolbar)` 上报，壳在宿主顶栏渲染；内容区不再放第二排工具栏；
- `cards` / `image-grid` 的 `image` 使用插件给的**缩略图 URL**（如 `Bridge.thumbUrl(...)`），
  壳不另取原图；`NativePanel` 的缩略图预览只放大该 URL；
- 面板打开期间宿主内容 iframe 用 `v-show` 隐藏但实例保留，`NativePanel` 只占宿主内容槽，
  侧栏/顶栏与扩展入口继续可见；关闭后原位恢复；
- 需要富文本、画布或视频的内容继续用 content iframe；附属插件不读宿主窗口上的对象，宿主能力
  走 `Bridge`（`HostChannel` 仅旧壳的 embed 回落页使用）；
- 旧壳不认 `native` 时回落 `embedUrl` 的整页内嵌；内嵌 URL 记得带 `?embed=1`。

### 4.8 全局 Bridge 对象

`Bridge` 对象挂载在 `window` 上，提供以下方法：

| 方法                                           | 说明                                                                    |
| ---------------------------------------------- | ----------------------------------------------------------------------- |
| `Bridge.call(method, ...args)`               | 调用后端 API（自动添加插件名前缀）                                      |
| `Bridge.callSystem(method, ...args)`         | 调用 Shell 系统 API（不加插件前缀，如`system_get_plugin_extensions`） |
| `Bridge.callPlugin(plugin, method, ...args)` | 跨插件调用其他插件后端 API                                              |
| `Bridge.thumbUrl(relPath)`                   | 获取缩略图 URL（自动附加`?plugin=插件名`，路径逐段百分号编码，`%`/`#`/空格等字符安全） |
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

### 4.9 全局 UI 组件

Shell 还注入了以下可复用的 UI 组件函数（无需引入，直接使用）：

| 函数                                                                | 说明                                      |
| ------------------------------------------------------------------- | ----------------------------------------- |
| `createTree(container, options)`                                  | 创建目录树组件                            |
| `createLightbox(options)`                                         | 创建灯箱组件                              |
| `createPagination(container, options)`                            | 创建分页组件                              |
| `createContextMenu(options)`                                      | 创建右键菜单组件                          |
| `createSettingsForm(container, schema, values)`                   | 按 schema 渲染设置表单                    |
| `Freshness.mount({plugin, container, unit, onChange})`             | 挂载统一的「同步 / 校验」控件（接入见「统一刷新（同步 / 校验）」）；`autoSync(scope)` / `verify()` / `cancel()` |
| `Freshness.assetUrl(path, version)`                               | 派生资源 URL + 版本号（绕开 `/thumbs` 的一天强缓存） |
| `openSettingsModal(options)`                                      | 在**内容页**打开设置弹窗。image-viewer 用它渲染按目录作用域的设置；其余声明 `frontend.chrome` 的插件由壳的 `PluginSettingsModal` 渲染，不需要调用它 |
| `HostChannel.requestSettings(title)` / `HostChannel.request(action, data)` | 内嵌页请**上层**用它的文档渲染 UI（设置/确认），详见 `docs/plugin-ui-guide.md`；仅旧壳的 embed 回落页使用 |
| `HostChannel.mountToolbar(buttons, {container, selector})` | 内嵌页把按钮挂到上层的工具栏/头部；宿主不表态时按钮留在本页。仅旧壳的 embed 回落页使用 |
| `confirmDialog(message, options)`                                 | 替代原生 confirm                          |
| `Toast.success(msg)` / `Toast.error(msg)` / `Toast.info(msg)` | 显示 Toast 通知                           |
| `Utils.formatFileSize(bytes)`                                     | 格式化文件大小                            |
| `Utils.debounce(func, wait)`                                      | 防抖函数                                  |

> 扩展入口没有共享层 API：声明 `frontend.chrome` 的宿主由壳的 `PluginExtensions.vue` 从
> `system_get_plugin_extensions` 拉取并渲染，插件只需用 `get_extensions()` 声明条目。
> `renderExtensions` 已从 `base.js` 删除；旧页面/旧壳走条目的 `embedUrl` 回落。
> `HostChannel.*` 只服务旧壳的 embed 回落页，新壳内容页/native 逻辑页不经过它。

**示例**：

```javascript
// 仅内容页自绘设置时使用（当前只有 image-viewer）。声明 frontend.chrome 的插件由壳的
// PluginSettingsModal 打开设置，保存后壳发 omnibox:action: settings-saved，不需要调用它。
// 省略 onSave 时壳的默认实现走 `Bridge.call('save_settings', values)`。
// **保存后必须自己收尾**：壳只弹一条 Toast。收尾二选一：整页重载，或就地重渲染。
// 不收尾时设置已经落盘，但界面上仍是旧值。
openSettingsModal({
  title: '图片相册设置',
  successMessage: '设置已保存',
}).then((saved) => { if (saved) location.reload(); });

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

// 统一「同步 / 校验」：空容器 + 一处挂载，轮询/进度/取消都由组件负责（见「统一刷新（同步 / 校验）」）
const ctl = Freshness.mount({
  plugin: 'my-plugin',                    // 必须与 manifest.name 一致
  container: document.getElementById('my-freshness'),
  unit: '首',
  onChange: () => reloadMyView(),
});
ctl.autoSync('');                         // 进视图 / 切目录时调用
```

### 4.10 插件专属文件

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

**页面骨架跟随壳 token**：声明了 `frontend.chrome` 的插件，品牌、导航、顶栏、搜索、状态与
分隔条由壳渲染，尺寸和颜色全部取壳的 CSS 变量（`--accent`、`--bg-surface`、`--border`、
`--radius*`、`--sub-sidebar-width`、`--transition-fast` 等）。品牌渐变的第二色取 manifest 的
`frontend.chrome.brand.accent`，由壳写到 `.plugin-page` 的 `--plugin-brand-2`；内容页不要覆盖
这些变量，也不要给 chrome 类写样式。用户调整主题、强调色、圆角、动效或侧栏宽度时，骨架与内容
一起生效。

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
const src = Bridge.originalUrl(encodeURIComponent('D:/音乐/cover.jpg'));
// 相对路径（普通单根插件）
const src = Bridge.originalUrl('subdir/photo.jpg');
```

**虚拟路径（多根 / 命名空间）**：如果插件用 `__<名字>/…` 这类**虚拟路径**表示"另一个
根下的文件"（如 `image-viewer` 的「图片文件夹」列表），覆写
`PluginBase.resolve_file_path(rel_path)` 即可 —— Shell 在解析**相对路径**时先问它，
返回 `None` 时按上面的老规矩用 `get_file_roots()[0]` 拼。没有这个钩子时，虚拟路径在
第一根下并不存在，表现是"列表与缩略图都正常，点开原图 404"。

```python
def resolve_file_path(self, rel_path):
    """虚拟路径 → 物理路径；答不上来返回 None（Shell 回退默认解析）。"""
    root, inner = self._split_virtual(rel_path)      # 插件自己的映射
    return (root / inner) if root and inner else None
```

三条约束由 Shell 执行，插件侧无法绕过：解析结果必须落在 `get_file_roots()` 的某一根
之内（否则 `403`）；受保护路径判定照旧优先（返回 `403`）；返回值不是 `Path`、返回
`None` 或实现抛错时一律**回退默认解析**，不会让路由 500 —— 与 `get_thumb_data()`
那种"返回值不可信、Shell 先校验形状"的既有做法一致。

**访问令牌（v3.1+）**：`/api`、`/file`、`/files`、`/thumbs` 均为**令牌保护路由**：

- 令牌在首次启动时生成并持久化到 `.config/auth_token.txt`（重启不变）；
- 页面加载时服务端自动种下 HttpOnly Cookie（`omnibox_token`），插件 iframe 与 `<img>` 等**同源请求自动携带，插件前端无需任何改动**；
- 外部脚本 / curl / nginx 注入用请求头：`X-Omnibox-Token: <token>`；
- 未携带令牌返回 `401`（`/api` 前缀为 JSON，浏览器路径为标记页）；越权路径返回 `403`；不存在的资源返回 `404`；
- `/health`（200 JSON）与页面/静态资源不要求令牌；
- 错误页在浏览器顶层打开时自动跳转到壳内 `/status?code=…` 视图统一展示（见 §7.4）。

**凭据文件不在文件服务范围内**：`.config/auth_token.txt` 是长期有效的进程外凭据，
**任何**数据路由都不会把它作为媒体资源返回 —— 即使某个插件的根目录（`get_file_roots()`
或 `thumb_dir`）恰好覆盖了 `.config`，请求该文件也只得到 `403`。这条判定由 Shell
独立执行，**不参与**「是否在允许根之内」的判断。

插件自己的凭据同样可以受这道防护：在设置项上声明 `"secret": True`（见 §8.2），
Shell 就会拒绝把该插件的设置文件当媒体资源返回；敏感内容存在别处时，覆写
`PluginBase.get_protected_paths()` 申报具体路径即可。**申报是插件主动发起、Shell 执行**：
插件只说明"什么是敏感的"，执行点始终在 Shell，插件侧没有关闭或绕过它的接口。

Shell **不维护系统路径黑名单**：把整个盘符当媒体根（一整块媒体盘）是正当用法，
因此壳不会因为「根看起来太宽」而拒绝；插件读写哪些路径由插件作者与安装者自负其责
（见 §11 权限声明一节）。

**安全响应头**：所有响应（含 `/api`、`/file`、`/thumbs`、错误页）统一带
`X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、
`Referrer-Policy: no-referrer`，以及一条只限制 `object-src` / `base-uri` /
`frame-ancestors` 的 `Content-Security-Policy`。完整的收紧目标（`default-src` /
`script-src` / `style-src` 等）以 `Content-Security-Policy-Report-Only` 先行上报，
插件无需为它做任何适配；**但请注意插件前端的对外资源必须能在这份目标策略下落位**
（远程图片要落在 `img-src`，远程音频/视频要落在 `media-src`，远程 iframe 要落在
`frame-src`），否则将来它转为强制执行时会被拦掉。新增此类来源时，同步更新
`shell/backend/file_server.py` 的 `_SECURITY_HEADERS` 与对应测试。

### 7.2 共享基建：浏览本机媒体目录

需要「让用户挑一个/多个媒体目录」的插件（image-viewer 的图片文件夹、media-player 的
媒体文件夹等）不要各写一套文件浏览器。这张表分两层，**先看第 2 层**：

**① 后端枚举** —— `shell/backend/media_catalog.py` 提供通用实现：

```python
from shell.backend.media_catalog import (
    DRIVES_SENTINEL,       # 「我的电脑」层的哨兵路径（前后端共用同一个常量）
    classify_file,         # 扩展名 → 'image' / 'video' / 'audio' / 'other'
    find_kinds,            # 目录**直接下级**命中的媒体类型，如 ['image', 'video']
    list_subdirectories,   # 浏览绝对路径：{path, parent, entries:[{name, path, kinds}]}
    list_system_roots,     # 盘符（Windows）/ ['/']（其他平台）
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS,
)

# 插件 API 里直接转出去即可（image-viewer 的 browse_dir 就是这么做的）
def browse_dir(self, path: str = ''):
    return list_subdirectories(path)
```

要点：

- 空路径或 `DRIVES_SENTINEL` → 「我的电脑」层（列盘符），`parent` 为 `None`，
  前端据此禁用「上级」；从任意目录都能一步跳回该层，不必逐级退回盘符；
- `kinds` 用于在目录选择器里标出「含图片 / 视频 / 音乐」，让用户一眼看出哪些目录
  值得添加；传 `kinds={'image'}` 可只看某一类；
- 目录超过 2000 个时截断（浏览整个盘符根时不至于卡住）。

**② 前端界面（大多数插件只需要这一层）** —— `system_browse_dir` 已把上面的
`list_subdirectories` 注册成宿主接口，而「主要 / 额外 + 浏览…」这套目录列表是
**Shell 共享组件**，实现只有一份：

| 资源 | 位置 | 谁在用 |
| --- | --- | --- |
| `window.FolderPicker` | `shell/frontend/public/shell/folder-picker.js` | `settings_schema` 里 `type:"directory"` 的字段（由 `base.js` 调用），以及 image-viewer 的设置页 |
| `.iv-root-*` / `.iv-dirbrowser-*` 样式 | `shell/frontend/public/shell/folder-picker.css` | 同上 |

这套组件原本长在 image-viewer 里（`app.js` 的 `_renderRoots` / `openDirBrowser`），
因为媒体播放器 / 漫画 / 文档阅读也要同一套「多位置文件夹」界面，才**整体搬**到 Shell：
类名与数值都没改，image-viewer 改成引用回来。所以：

- **插件侧声明 `type:"directory"` 即可**，不需要写任何前端代码（见 §8.2）；
- 需要自己控制布局时才直接用 `window.FolderPicker.createList({paths, placeholder, emptyText, labels, onBeforeOpen})`，
  返回 `{element, getPaths, setPaths, addPath, render}`；
- 需要单独弹一次目录选择器用 `await window.FolderPicker.openDirBrowser(startPath)`（取消返回 `null`）。
- 两个文件都由 `file_server` 的插件页注入模板（`_PLUGIN_BOOTSTRAP_SCRIPT`）带进插件的
  **每个 HTML 页面**（不只是 `index.html`：插件可以有子页面），插件自己的 HTML
  不用声明 `<script>` / `<link>`。

#### 7.2.1 「网络位置」：把远端共享项变成一个本地目录（供方契约）

目录列表里的「网络位置」按钮按 **placement = `network-location`** 发现提供方，因此
**壳不认识任何具体插件、提供方与宿主互不声明依赖**（没有 `dependencies`）：

| 环节 | 约定 |
| --- | --- |
| 提供方声明 | `get_extensions()` 返回 `{'placement': 'network-location', 'label', 'icon', 'embedUrl'}`；**不写 `host`**（组件出现在任意插件的设置里，提供方应对所有宿主可用） |
| 宿主发现 | `system_get_plugin_extensions(null, 'network-location')`；无提供方时按钮提示"需要安装提供该能力的插件" |
| 提供方界面 | 组件把 `embedUrl` 嵌进弹窗 iframe；子页面同样被注入 BootStrap（`Bridge` / `Utils` / `Toast` / `FolderPicker`） |
| 回填 | 提供方向父窗口 `postMessage({type:'omnibox:network-location', action:'picked', path, label})`；组件**校验 `event.source` 必须是它嵌的那个 iframe**（否则任何同源页面都能往用户的文件夹列表里塞路径），并校验 `path` 非空。取消用 `action:'cancelled'` |
| 产物 | 回填的 `path` 必须是**本地绝对目录**：所有消费方的后端只认本地路径，`getPaths()` 语义因此完全不变 |

两条实现约束（踩过才知道）：

1. **提供方只负责"把远端内容取到那里"**，不写宿主的设置 —— 它把目录回填给组件，
   由宿主插件自己的保存流程写进 `root_dir` / `extra_roots`。于是提供方不需要知道
   宿主是谁，也不需要跨插件后端调用。
2. **取回必须是"完整取回"而不是占位**。镜像目标是普通本地目录，消费方按本地文件工作、
   **没有** `ensure_file` 钩子可依赖：留 0 字节占位就是宽高 0×0 与整片 404 缩略图
   （见 `plugins/group-mesh` 的 `mirror_share` 实现）。

参考实现：`plugins/group-mesh`（`get_extensions()` + `frontend/network-location.html` +
后端 `mirror_share`）。

### 7.3 插件如何生成缩略图

`/thumbs` 消费的四个成员都是 `shell/backend/plugin_base.py` 里 **`PluginBase` 的正式契约**，
不是"宿主碰巧 getattr 得到"的隐式约定（历史上它们是探针式的，宿主把 `thumb_dir` 改成方法
就能让 `/thumbs` 整体 500）：

| 成员 | 默认实现 | 说明 |
| --- | --- | --- |
| `get_thumb_data(rel_path)` | 返回 `None` | 返回 `(bytes, mime)`，或 `None` 表示本插件不提供字节 |
| `thumb_dir` | `数据根目录/.cache/thumbs` | 只读 property（`Path`）。可赋值覆盖（旧写法兼容），也可以什么都不做 |
| `ensure_thumb(rel_path)` | 空实现 | `/thumbs` 找不到文件时调用，插件可现场生成并落盘；返回值被忽略 |
| `get_file_roots()` | `[get_data_root()]` | 跨多根插件覆写（如 `media-player`） |
| `resolve_file_path(rel_path)` | 返回 `None` | 让插件解释 `/file` 的**相对路径**（虚拟路径 / 命名空间）；`None` 表示按 `get_file_roots()[0]` 解析 |

**优先级**：`get_thumb_data()` 命中（返回二元组）时直接响应；返回 `None` 或形状不对时，
才回退到 `thumb_dir` 散文件布局。因此"只覆写 `get_thumb_data()`、不定义 `thumb_dir`"
（`media-player` 形态）与"只用散文件布局"（默认形态）都成立。

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

前端仍通过 `Bridge.thumbUrl()` 获取 URL，无需感知后端用哪种模式。

**旧版散文件模式（兼容）**：保存到 `self.thumb_dir`（默认为 `数据根目录/.cache/thumbs/`），示例：

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

> **Companion 插件可直接代理宿主成员**：`image-cleaner` 就是把自己的
> `thumb_dir` / `ensure_thumb` / `get_thumb_data` 转发给 `self.get_dependency('image-viewer')`，
> 因此它复用宿主的缩略图而不复制一份缓存。这四个成员既然是基类契约，宿主侧改动
> 就受 `tests/test_plugin_host_contract.py` 的回归保护。

### 7.4 状态页与壳内错误视图

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

在插件类中通过 `settings_schema` 声明设置项，框架自动完成持久化，壳按 schema 渲染表单：
声明 `frontend.chrome` 的插件由壳的 `PluginSettingsModal` 在顶栏「设置」里打开，保存后内容页
收到 `omnibox:action: settings-saved`；内容页不再自己渲染设置弹窗。image-viewer 是例外：它的设置
按目录作用域读写，仍在内容页调用 `openSettingsModal()`。`image-cleaner` / `pixiv-sync` 的
native 设置同样由 `PluginSettingsModal` 接管（入口是 native 顶栏的「设置」动作），保存后向
隐藏逻辑页发 `settings-saved`。

字段的唯一前端类型是 `shell/frontend/src/core/plugin-content.ts` 的 `SchemaField`：壳的
`PluginSettingsModal`、壳的 `SchemaRenderer`（native `form`）与内容页 `base.js` 的
`createSettingsForm` 都消费同一份 JSON。三者对个别字段的处理并不完全一致，见本节末尾。

```python
class MyPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "数据根目录", "type": "directory",
         "placeholder": "输入目录绝对路径，如 D:\\音乐",
         "help": "第一行即生效根目录，保存后生效"},
        # 多值列表：每行一个目录，第一行是主要目录，其余是额外目录
        {"key": "media_roots", "label": "媒体文件夹", "type": "directory", "multi": True,
         "emptyText": "未添加任何目录，将使用默认数据目录（./data）",
         "help": "第一行为主目录，其余目录一起扫描"},
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
| `type`                     | 必填 | `text` / `number` / `range` / `select` / `checkbox` / `textarea` / `directory` / `info` |
| `value`                    | 可选 | 字段初始值：`info` 的只读值、native `form` 初值；壳弹窗在 `get_settings` 未给该键时回退，内容页旧表单只对 `info` 使用 |
| `default`                  | 可选 | 默认值（未保存过时使用）                                                                  |
| `help` / `hint`            | 可选 | 字段说明；`hint` 是 native 侧别名，渲染时与 `help` 取一                                    |
| `min` / `max`              | 可选 | number/range 的边界：同时设控件属性，并在 `save_settings` 写盘前校验                      |
| `step`                     | 可选 | 只设前端控件步长；后端不校验                                                              |
| `options`                  | 可选 | select 类型的选项列表                                                                     |
| `multi`                    | 可选 | 仅 `directory`：多值字段，列表可增删多行                                                  |
| `placeholder` / `emptyText` | 可选 | 目录字段的占位符 / 空列表提示（`SchemaRenderer` 不读 `emptyText`）                       |
| `local_only`               | 可选 | 仅 `directory`：三个入口的 `FolderPicker` 都不显示「网络位置」，只接受本机目录 |
| `secret`                   | 可选 | `True` 表示**凭据类**设置项：对外掩码、设置文件受保护；空串清除（消费方差异见下）        |
| `admin_only`               | 可选 | `True` 表示**改动该键需要管理员主体**；只由后端判定，前端不置灰/不隐藏                   |
| `hidden`                   | 可选 | `True` 表示面板不渲染但键仍可写；壳弹窗与 `base.js` 跳过，`SchemaRenderer` 不判断         |
| `required`                 | 可选 | 只给标签加必填样式，前端与后端都不做提交校验                                              |
| `condition` / `save_target` | 可选 | 类型中的预留字段，当前没有消费方                                                          |

#### 三个渲染路径的差异与未覆盖项

同一个 `SchemaField` 被三处消费，不能假设字段在所有入口表现一致：

- `PluginSettingsModal`：支持 `min`/`max`/`step`、`secret`（password、打开置空、勾选
  「清除已保存的凭据」提交空串）、`hidden`（读取时过滤）、`directory`（壳内 `FolderPicker`）、
  `multi`/`emptyText`/`local_only`、`required`（仅标签样式）、`info`；显示后端逐字段 `errors`
  到对应输入框，`admin_only` 不置灰、不隐藏；不消费 `condition`、`save_target`。
- `SchemaRenderer`（native `form`）：支持 `min`/`max`/`step`、`secret`（password；没有专用清除
  控件）、`directory`（自绘路径行 + `FolderPicker` 浏览/网络位置，`local_only` 隐藏「网络位置」）、
  `multi`、`info`；不判断 `hidden`、`required`、`emptyText`、`admin_only`、`condition`、`save_target`。
  `form` block 仍可用，但 `image-cleaner` / `pixiv-sync` 已不通过它渲染插件设置。
- `base.js` 的 `createSettingsForm`：支持 `min`/`max`/`step`、`directory`（`FolderPicker.createList`）/
  `multi`/`local_only`/`emptyText`、`hidden`、`required`（仅标签样式）、`info`；**没有 secret
  专用渲染**，凭据以掩码文本回填，原样提交由后端按"不改动"处理，清空输入框提交空串则由后端清除。

`condition` 与 `save_target` 只存在于类型，当前没有任何消费方；`required` 没有提交校验；
`step` 只在前端生效；`admin_only` 只由后端判定。`image-cleaner` / `pixiv-sync` 的 native 设置
已由 `PluginSettingsModal` 接管，native `form` block 只用于非设置输入（pixiv-sync 的 OAuth 授权码）。

#### 键要能存但不想显示：`"hidden": True`

`settings_schema` 同时是 `save_settings` 的**写入白名单**（见 `shell/backend/plugin_base.py`
的 `allowed`）。插件把某项配置挪到自己的界面上（自绘弹窗、独立的设置页）之后，**不能**
直接把键从 schema 里删掉 —— 删掉等于连保存也一起废掉，用户在新界面里的改动会被
`save_settings` 静默丢弃、下次打开又回到旧值。

正确做法是保留声明并标 `hidden`：

```python
{"key": "reader_font_size", "label": "正文字号(px)", "type": "number",
 "default": 16, "hidden": True},
```

面板不渲染它（`createSettingsForm` 直接跳过），`get_settings()` / `save_settings()`
照常收它。document-reader 的朗读项就是这么处理的：入口在插件左栏的「朗读设置」页，
因此 `tts_engine` / `tts_base_url` / `tts_api_key` / `tts_model` / `tts_voice` /
`tts_rate` 都标 `hidden`，而该插件的阅读偏好留在面板里。

> 历史：这个标记替代的是早先 `settings_schema` 上的 `central: False`（"不显示在集中
> 设置面板"，见 `1a4cb1d`）。`central` 随集中设置面板一起在 `2e8917d` 删除，之后
> "显示在哪"只由 `hidden` 表达。

#### 能力边界类设置项：`"admin_only": True`

决定"这个插件能碰本机哪些文件、能连哪些地址"的设置项要声明 `"admin_only": True`：

```python
{"key": "media_roots", "label": "媒体文件夹", "type": "directory", "multi": True,
 "admin_only": True},
```

原因：`/file`、`/files`、`/thumbs` 的允许范围**就是**插件自己返回的根
（`get_file_roots()` / `thumb_dir`），而根由设置项决定。于是"谁都能改根"等价于
"谁都能读、删、移本机任意文件"——越权点不在文件路由，而在设置入口。同类还包括
"进程会带着凭据去请求的地址"（如朗读端点），它同时是 SSRF 跳板与凭据外发通道。

声明之后：

- `save_settings()` 里改动该键**需要管理员主体**（`PrincipalContext.is_admin`，
  即壳设置页里的 `owner` / `admin`），否则返回
  `{"success": False, "error": "设置项 … 需要管理员权限（…）"}`，且不写入；
- `update_setting()` 写同一个键时同样判定，返回 `False` —— 否则运行期回写可以绕过
  设置面板的限权，重启后仍会生效；
- 判定做在 `PluginBase`，**覆写 `save_settings()` 不会绕过**（image-viewer 的
  `save_folder_settings` 就调用的基类实现）；
- 未声明的键（每页数量、字号、排序一类）行为完全不变，任何主体都能改；
- 没有主体的调用（用例、CLI、插件自身的运行期回写）不受限：判据是**凭据**而不是
  "有没有主体"，网络请求路径上主体必然存在（壳在 `before_request` 里鉴权后注入）。

判据必须是"改这个键要不要管理员"，不要图省事把整个插件的 `save_settings` 都限权：
那会让普通使用者连字号都改不了。

#### 凭据类设置项：`"secret": True`

存放令牌、密码、API Key 这类值的设置项**必须**声明 `"secret": True`。原因不是风格：
设置值统一落在 `<config>/plugins/<插件名>.json`，而该文件（或它所在的目录）完全可能
落在某个插件的媒体根之内 —— 于是它会被 `/file`、`/files`、`/thumbs` 当成普通媒体
文件正常返回。这是结构性的，Shell 无法靠猜避免，只能由插件申报：

```python
{"key": "refresh_token", "label": "API 令牌", "type": "text", "secret": True},
```

声明之后：

- Shell 把该插件的设置文件加入受保护清单，`/file`、`/files`、`/thumbs` 与插件前端
  路由一律返回 `403`（优先级高于"是否在允许根之内"的判定）；
- **`get_settings()` 的返回值里该键被替换成掩码 `********`**。这一步同样必须做：
  `get_settings` 通常经 `register_api()` 暴露成 `POST /api/<插件>__get_settings`，
  而插件 iframe 与壳同源 —— 返回明文等于让任何一段同源脚本直接问 API 拿到凭据，
  根本不用碰文件；
- 脱敏**不依赖你覆写 get_settings 时记得调 super()**：Shell 在
  `<插件>__get_settings` 这个出口还会按 schema 再掩一次
  （`PluginBase.mask_secrets`）。覆写只影响你自己看到的形状，不影响对外安全；
- 插件侧**不需要写任何代码**：`setting()` 读到的仍是明文（插件自己要用），
  只有对外的 `get_settings()` 脱敏；
- 请求被拒时返回的是 `403`，与越界访问同一语义，前端按既有路径处理即可。

**受保护清单也管写和删。** `/api/<插件>__<方法>` 是插件自己实现的方法，Shell 拦不到
里面的 `unlink` / `rmtree` / `move`：根一旦被设置改写成包含 `<config>` 的目录
（例如把 `extra_roots` 指向仓库根），`<config>/plugins` 就只是"根内一个没有图片的
普通目录"，只做根内校验就会把凭据文件删掉。凡是会**不可逆地改文件系统**的方法，
动手前都要自查一次：

```python
def delete_files(self, rel_paths):
    for rel in rel_paths:
        abs_path, _ = self._resolve_path(rel)
        if abs_path is None or self.is_protected_path(abs_path):
            continue          # 受保护路径（壳凭据 + 全插件申报）
        ...
```

`is_protected_path()` 与 Shell 的读路由共用同一份判定（含路径归一化：Windows 上
`resolve()` 会保留 `\\?\` 扩展前缀，只做字符串比较会被
`\\?\D:\…\.config\auth_token.txt` 这种形态绕过），判定失败时按"受保护"处理 ——
这类操作不可逆，宁可不做。

掩码的读写语义（前端把 `get_settings()` 的值回填进输入框、再整体提交时依赖它）：

| 提交的值 | 行为 |
| --- | --- |
| `********`（掩码原样回传） | **不改动**，保留存储里的真凭据 |
| 空字符串 | 清除该凭据 |
| 其它任意值 | 作为新凭据写入 |

三个入口都能完成清除：`PluginSettingsModal` 打开时把 secret 输入框置空，并提供「清除已保存的
凭据」勾选框 —— 勾选后提交空字符串；未勾选且输入框仍为空时省略该键（按上表"不改动"），填写
新值则覆盖。内容页 `base.js` 的旧表单与 native `form` 没有专用清除控件，清空输入框提交空串即
按上表清除。`admin_only` 也只在后端判定，三个入口都不置灰、不隐藏；`PluginSettingsModal` 会把
后端返回的逐字段 `errors` 显示到对应输入框，同时显示汇总 `error`。

未配置的凭据在 `get_settings()` 里保持空字符串（不打掩码），前端据此显示"未配置"
即可；需要"是否已配置"的只读信号时，另建一个非 schema 的返回字段（如
`token_configured`），不要把凭据本身放出去。

检查器会拦住遗漏：设置项键名里含 `token` / `secret` / `password` / `credential` /
`api_key` / `cookie` / `session` 等词却没有声明 `secret`（或声明成 `"false"` 这类
非布尔值）时，`tools/check_plugins.py` 直接报错。匹配**与大小写和分隔符无关**：
`API_TOKEN`、`Api_Token`、`refreshToken`、`privateKey` 同样命中（早期只认小写
下划线形态，这几种写法全都漏过）。键名确实不是凭据时，改成一个不含这些词的键名。

**敏感内容不在设置文件里**时（自己的令牌文件、cookie jar、加密密钥等），覆写
`PluginBase.get_protected_paths()` 申报那些路径：

```python
def get_protected_paths(self):
    return [*super().get_protected_paths(), self.get_data_root() / '.cache' / 'token.json']
```

注意两点：

- 只应申报**本插件自己的数据根目录或 `<config>/plugins` 之下**的路径。越界申报会被
  忽略并记一条 warning（这是防误用：一个写错的申报不该把整个文件服务钉死），
  所以申报看起来"没生效"时先看日志。
- 申报**目录**表示"该目录及其下全部内容"都受保护。**不要**申报 `get_data_root()`
  这类宽目录 —— 媒体的数据根往往就是媒体根，一起挡掉会让本插件自己的封面、缩略图
  也变成 403。只申报凭据文件。

`type: "directory"` 的字段在插件设置弹窗里渲染成**与图片相册完全相同的目录列表** ——
不是"样子像"，而是同一个实现（`window.FolderPicker`，见 §7.2；壳弹窗、native form 与内容页
`base.js` 都直接调它）：

- 每行是「主要 / 额外」标签 + 路径 + 方形 `icon:x` 删除按钮；第一行是**位置**属性而不是固定标记，
  所以每行都能删 —— 删掉第一行后下一行自动顶上成为主要目录；
- 下面是「输入框 + 浏览… + 添加」：可以手输绝对路径、回车添加，或点「浏览…」
  弹出宿主目录选择器（`system_browse_dir` → 共享基建
  `media_catalog.list_subdirectories`，见 §7.2），可看盘符、逐级下钻、
  看到每个目录「含图片 / 视频 / 音乐」；
- 重复目录（含只差尾斜杠的写法）与空路径会被拒绝并提示。

> 声明 `frontend.chrome` 的插件与两个附属插件的 native 设置由壳的 `PluginSettingsModal` 渲染
> 同一字段，目录列表也用同一个 `FolderPicker` 组件（`SettingsDirectoryField` 挂载
> `createList`）：路径行、「浏览…」与「网络位置」的表现与内容页一致，`local_only: true` 时
> 不显示「网络位置」。插件目录字段不再使用 `system_pick_path('directory')` 系统对话框。
> 各处写入的值格式相同（单值一行、多值换行分隔）。

**插件侧不需要写任何代码**，值仍是普通字符串：单值字段存一行路径（`str`），
`multi: true` 的字段存换行分隔的多行（与改造前的 `text` / `textarea` 格式完全一致），保存走原来的 `save_settings()` 链路。

> `directory` 只是**渲染方式**，不影响值本身：字段从 `text` / `textarea` 改成
> `directory` 前后，`self.setting('root_dir')` 拿到的都是同一份字符串。反过来说，
> 把多行文本字段合并成一个多值 `directory` 字段时，**记得让后端兼容旧键**，
> 否则老用户配置里的值会看起来"丢了"（media-player 的
> `_configured_roots()` 就是这种兼容：新键优先，没有才回退旧键）。

#### `type: "info"`：只读信息行

有些设置项**只该被看见，不该被改**：典型是"这个插件实际在处理哪个目录/哪个端点"，
它由宿主的设置决定（image-cleaner 的相册根目录就是图片相册的数据根）。这类声明成
`info`，渲染成一行只读文本（`.field-info`），没有输入控件：

```python
{"key": "root_dir", "label": "相册根目录", "type": "info",
 "help": "扫描范围取自「图片相册」的根目录；要换目录请到图片相册的设置里改"},

def get_settings(self):
    settings = super().get_settings()
    settings['root_dir'] = str(self.get_data_root())   # 每次现取，跟着宿主变
    return settings
```

值是每行的一条文本：优先取 `get_settings()` 里的同名键（后端算出来的实时值），
其次取 schema 自带的 `value`（静态说明）。它是**展示**，不是设置：前端
`getValues()` 不会提交它，`PluginBase.save_settings()` 也把它排除在可写键之外
（两层都不认；只声明 `info` 字段的插件，一次手写 API 调用也写不进任何键）。
需要"能改的目录"就用 `directory`，两者别混。

> **覆写了 `get_settings()` 就必须把它登记进 `register_api()`**（连同 `save_settings`）：
> 壳的设置弹窗（顶栏的 `PluginSettingsModal` 与内容页的 `openSettingsModal`）最终都请求
> `<插件>__get_settings` / `<插件>__save_settings`，而 `PluginManager` 只会额外登记
> `<插件>__get_settings_schema`。
> 漏登记的表现**完全是静默的**：弹窗打得开、每个字段显示 schema 默认值（`info` 行空白）、
> 点保存提示"保存失败" —— image-cleaner 与 group-mesh 都这样漏过。可写 schema 的登记
> 形态见 §3.2 的 `register_api()` 示例；`tools/check_plugins.py` 会拦住"前端会开设置弹窗
> 却没登记"的插件，`tests/test_plugin_fresh_install.py` 会在真机上验 `get_settings` /
> `save_settings` 确实可达。

#### `local_only`：该目录只接受本机路径

默认情况下目录列表带一个「网络位置」入口：让用户把某个远端共享项取回本地一个
目录，再把**那个本地目录**加进列表（详见 §7.2.1）。这对"媒体根目录"是正确的 ——
取回来的就是本地文件。

但对**目录本身就是产物**的字段，这个入口是错的：例如 group-mesh 的「远端下载目录」
（`download_dir`）就是"从对端取回的文件落在哪"，在那里选网络位置等于"把取回的中间
目录当作下载目录"，语义不成立。这类字段声明：

```python
{"key": "download_dir", "label": "远端下载目录", "type": "directory",
 "local_only": True,          # 不渲染「网络位置」入口
 "default": "", "placeholder": "默认：数据根/group-mesh/downloads"},
```

判断标准一句话：**这个目录是用来"装结果"的，还是用来"当来源"的？** 装结果的
（下载落点、导出目录）用 `local_only`；当来源的（媒体根、相册根）不要用。

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
9. **真实浏览器端到端（改壳前端后建议跑）**：

   ```bash
   venv/Scripts/pip install -r requirements-e2e.txt     # 只需一次（Linux/macOS 用 venv/bin/pip）
   venv/Scripts/python -m unittest tests.test_shell_browser_e2e -v
   ```

   它自己起 `main.py --web-only` 服务 + 无头 Chrome，断言「接口返回的插件数 == 导航渲染出的插件数」、
   iframe 已挂载、以及生命周期通知真的送达插件 iframe（切到设置页收 `onHide`、切回收 `onShow`）。

   **为什么需要它**：有一类缺陷是"界面静默不更新"——接口数据、status、cookie 全正常，
   但界面不渲染，而且**不抛异常、控制台没有报错**。实测过一次（插件列表不是响应式数据，
   computed 在数据到达前求值并永久缓存空数组），unittest 与 vm 桩都抓不到，只有真实浏览器能抓。
   **这类用例不进 CI**：本地打开应用一眼可见的问题属于本地自测范围，CI 上跑真实浏览器又慢又脆；
   `selenium` 因此只声明在 `requirements-e2e.txt`（CI 装的是 `requirements-dev.txt`），
   未安装时用例自动 skip，不影响 `unittest discover` 结果。

10. **插件页面骨架审计**：`venv/Scripts/python.exe tools/plugin_ui_audit.py`。它起真实壳
    （或复用 `--base-url` 指向的实例）逐插件检查：壳是否渲染了
    `.plugin-page` / `.plugin-chrome-sidebar` / `.plugin-title`，内容 iframe 是否残留 chrome
    标记，壳导航与内容面板、标题是否同步，侧栏分隔条 hover 是否出现指示线，声明了
    `frontend.chrome.freshness` 的插件是否挂上 `.obx-fresh`，以及控制台有没有 `SEVERE`。
    参数：`--plugin <name>` 只查一个插件，`--json <path>` 输出机器可读结果。退出码：`0` 全部
    PASS（含 SKIP）、`1` 有 FAIL、`2` 环境或参数错误（缺 `shell/frontend/dist`、Chrome、
    selenium，或连不上壳）。设置弹窗保存、搜索回填与迷你 EQ 暂不在断言范围内。

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
- **页面骨架归壳**：声明 `frontend.chrome`，让壳渲染品牌、导航、顶栏、搜索、状态与分隔条；内容页只写内容，不出现 `.view-sub-sidebar` / `.view-toolbar` / `.sub-sidebar-brand` / `.obx-nav-item`，也不复刻这些类的样式。内容页的 `.view-body` / `.view-content` / `.obx-scroll` 直接使用壳注入的定义。
- **颜色适配**：所有颜色使用 CSS 变量（如 `var(--bg-surface)`），避免硬编码颜色值。如需在 JS 中获取当前颜色，使用 `getComputedStyle(document.documentElement).getPropertyValue('--accent')`。
- **主题无感知**：插件无需编写主题切换逻辑，Shell 已通过 MutationObserver 自动同步 `data-theme` 和 `data-custom-colors` 到所有 iframe。
- **前端资源**：尽量轻量，避免引入大型框架（除非必要）。
- **权限声明**：如实填写 `permissions`，作为面向用户的知情明示（安装/设置页可见）。框架**不设运行时权限墙**——插件后端运行在主进程内，任何声明都无法拦截 `import os` 之类直接系统访问；隔离与信任依赖进程级 runtime 方案（见 `docs/core-direction.md` §3.4、§3.5）。
- **版本管理**：遵循语义化版本，方便依赖解析。
- **错误处理**：后端方法应捕获异常并返回有意义的错误信息，避免前端收到 Python 堆栈。
- **设置持久化**：使用 `settings_schema` 声明式配置，设置自动存储在 `.config/plugins/<name>.json`。读取用 `setting()`，保存用 `save_settings()`，响应变更用 `on_settings_changed()`。**不要**覆写 `save_settings()`，**不要**在插件目录创建 `settings.json`。
- **运行时状态**：播放进度、收藏、缓存等数据派生状态放在数据目录（如 `data/.cache/`），跟随数据走。
- **共享基建**：需要后台长任务、缩略图缓存或"刷新"时，优先复用 `shell/backend/tasks.py`（`BackgroundTask`）、`shell/backend/thumb_cache.py`（`ThumbCache`）与 `shell/backend/freshness.py`（统一刷新，见 §3.4），**不要各自重复实现**；DB / 任务状态文件放各自数据根目录 `.cache/` 下（用 `get_cache_dir()` 取）。现成的用法：image-viewer 的缩略图重建（`rebuild_all/status/cancel`）与统一刷新接入、pixiv-sync 的下载扫描、image-cleaner 的全库扫描（`scan_start/status/cancel`）—— 三者都是「起任务 → 轮询进度 → 可取消」，结果各自落自己的缓存文件。
- **性能优化**：使用内存缓存（如目录列表缓存、聚合元数据缓存）减少 I/O，提升响应速度；大目录首次扫描可参考 image-viewer 的并行尺寸读取（`ThreadPoolExecutor`）。

---

按照本指南，你可以快速创建新插件，或将现有功能迁移到 OmniBox 架构中。快速起步可运行 `python tools/new_plugin.py my-tool` 一键生成骨架（模板见 `tools/examples/hello-world`）；如有疑问，请参考 `image-viewer` 示例插件。
