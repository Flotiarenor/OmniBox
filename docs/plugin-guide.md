## 🤖 Assistant

## OmniBox v3 插件开发指南

本指南将带你从零开始创建一个完整的 OmniBox v3 插件，并说明如何将现有的 `image-viewer` 插件迁移到新架构。

---

### 1. 插件目录结构

每个插件是一个独立文件夹，放置在项目根目录的 `plugins/` 下。结构如下：

```

plugins/

└── <plugin-name>/

    ├── manifest.json          # 插件声明（必填）

    ├── backend/               # Python 后端代码（必填）

    │   ├── __init__.py

    │   └── main.py            # 插件入口，必须包含 Plugin 类

    └── frontend/              # 前端静态文件（必填）

        ├── index.html         # 插件入口 HTML

        ├── assets/            # JS、CSS、图片等

        └── ...

```

**命名约定**：

- 文件夹名使用 `kebab-case`（如 `image-viewer`），与 `manifest.json` 中的 `name` 字段一致。
- 后端入口文件固定为 `backend/main.py`（可在 manifest 中自定义）。
- 前端入口文件固定为 `frontend/index.html`（可在 manifest 中自定义）。

---

### 2. manifest.json 规范

```json

{

  "name": "image-viewer",

  "version": "1.0.0",

  "displayName": "图片浏览器",

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

| 字段 | 必填 | 说明 |

|------|------|------|

| `name` | ✅ | 插件唯一标识，必须与文件夹名一致，使用 `kebab-case` |

| `version` | ✅ | 语义化版本号 |

| `displayName` | ✅ | 在导航栏显示的名称 |

| `icon` | ✅ | 导航栏图标（Emoji 或文字） |

| `backend.entry` | ✅ | 后端入口文件路径，相对于插件根目录 |

| `backend.class` | ✅ | 后端插件类名，必须继承 `PluginBase` |

| `frontend.entry` | ✅ | 前端入口 HTML 文件路径，相对于插件根目录 |

| `frontend.route` | ✅ | 前端路由路径，必须以 `/` 开头 |

| `dependencies` | ❌ | 依赖的其他插件名称列表 |

| `permissions` | ❌ | 权限声明（当前仅做记录，未强制执行） |

| `minShellVersion` | ❌ | 要求的最低 Shell 版本 |

---

### 3. 后端开发

#### 3.1 插件基类

所有后端插件必须继承 `shell.backend.plugin_base.PluginBase`，并实现 `register_api()` 方法。

```python

# shell/backend/plugin_base.py（已由框架提供，无需修改）

from abc import ABC, abstractmethod


classPluginBase(ABC):

    def__init__(self, manifest: dict, config: dict):

        self.manifest = manifest

        self.config = config          # 全局配置（config.yaml 内容）

        self.name = manifest['name']


    @abstractmethod

    defregister_api(self) -> dict:

        """返回暴露给前端的 API 字典，格式：{'method_name': callable}"""

        pass


    defon_load(self):

        """插件加载后调用（可选）"""

        pass


    defon_unload(self):

        """插件卸载前调用（可选）"""

        pass

```

#### 3.2 编写插件类

```python

# plugins/image-viewer/backend/main.py

from shell.backend.plugin_base import PluginBase


classImageViewerPlugin(PluginBase):

    def__init__(self, manifest, config):

        super().__init__(manifest, config)

        # 从全局配置中获取图片目录

        self.image_dir = config['directories'].get('data_root', './data')

        # 可在此处进行初始化操作


    defregister_api(self):

        return {

            'list_images': self.list_images,

            'get_image_info': self.get_image_info,

        }


    deflist_images(self, folder: str = ''):

        """返回指定文件夹下的图片列表"""

        import os

        target = os.path.join(self.image_dir, folder)

        ifnot os.path.exists(target):

            return []

        files = []

        for f in os.listdir(target):

            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):

                files.append({

                    'name': f,

                    'url': f'/files/{folder}/{f}'if folder elsef'/files/{f}',

                    'size': os.path.getsize(os.path.join(target, f))

                })

        return files


    defget_image_info(self, path: str):

        """获取单张图片信息（示例）"""

        import os

        full_path = os.path.join(self.image_dir, path)

        ifnot os.path.exists(full_path):

            returnNone

        return {

            'name': os.path.basename(path),

            'size': os.path.getsize(full_path)

        }

```

**关键点**：

- 所有方法自动获得命名空间前缀 `插件名__`，前端调用时使用 `image-viewer__list_images`。
- 方法参数和返回值必须是 JSON 可序列化的类型（dict、list、str、int、float、bool、None）。
- 文件访问应通过 `/files/` 路由（由 Shell 提供），避免直接返回本地绝对路径。

---

### 4. 前端开发

#### 4.1 技术选型

插件前端可以是**任何静态网页技术**（纯 HTML/JS、Vue、React 等），只要最终构建产物是一个 `index.html` 及其依赖的静态资源。

**推荐方案**：

- 简单插件：纯 HTML + Vanilla JS
- 复杂插件：Vue 3 + Vite（独立项目，构建后复制到 `frontend/`）

#### 4.2 纯 HTML 示例（最小可行插件）

```html

<!-- plugins/image-viewer/frontend/index.html -->

<!DOCTYPEhtml>

<htmllang="zh">

<head>

  <metacharset="UTF-8">

  <metaname="viewport"content="width=device-width, initial-scale=1.0">

  <title>图片浏览器</title>

  <style>

    body { font-family: sans-serif; padding: 20px; background: #fff; }

    .image-list { display: flex; flex-wrap: wrap; gap: 10px; }

    .image-item { width: 150px; text-align: center; }

    img { max-width: 100%; height: auto; }

  </style>

</head>

<body>

  <h1>🖼️ 图片浏览器</h1>

  <divid="app">

    <buttononclick="loadImages()">加载图片</button>

    <divid="image-container"class="image-list"></div>

  </div>


  <script>

    // 直接访问父窗口的 pywebview.api（因为同源）

    constapi = parent.pywebview.api;


    asyncfunctionloadImages() {

      try {

        constimages = awaitapi['image-viewer__list_images']('');

        constcontainer = document.getElementById('image-container');

        container.innerHTML = images.map(img=>`

          <div class="image-item">

            <img src="${img.url}" alt="${img.name}" />

            <p>${img.name}</p>

          </div>

        `).join('');

      } catch (e) {

        alert('加载失败: ' + e.message);

      }

    }

  </script>

</body>

</html>

```

**关键点**：

- 通过 `parent.pywebview.api` 调用后端 API，方法名格式为 `插件名__方法名`。
- 图片 URL 使用 Shell 提供的 `/files/` 路由，例如 `/files/subdir/photo.jpg`。

#### 4.3 Vue 3 插件示例（独立构建）

如果插件较复杂，建议使用 Vue 3 独立项目：

1. 在插件目录外创建 Vue 项目：

```bash

   npmcreatevite@latestimage-viewer-frontend----templatevue-ts

   cdimage-viewer-frontend

   npminstall

```

2. 开发完成后构建：

   ```bash

   npm run build

   ```
3. 将 `dist/` 目录下的所有文件复制到 `plugins/image-viewer/frontend/`。
4. 在 Vue 组件中调用后端 API：

   ```typescript

   // 在 Vue 组件中

   constapi = (window.parentasany).pywebview.api;

   constimages = awaitapi['image-viewer__list_images']('');

   ```

---

### 5. 迁移现有 image_viewer 插件

#### 5.1 目录调整

原结构：

```

plugins/image_viewer/

├── manifest.json

├── backend/

│ └── module.py

└── frontend/

 ├── Index.vue

 ├── store.ts

 └── components/

```

新结构：

```

plugins/image-viewer/

├── manifest.json # 更新字段

├── backend/

│ ├── __init__.py

│ └── main.py # 原 module.py 重命名，类名保持一致

└── frontend/ # 将原 Vue 项目构建产物放入

 ├── index.html

 ├── assets/

 └── ...

```

#### 5.2 manifest.json 更新

```json

{

"name": "image-viewer",

"version": "1.0.0",

"displayName": "图片浏览",

"icon": "🖼️",

"dependencies": [],

"backend": {

 "entry": "backend/main.py",

 "class": "ImageViewerPlugin"

},

"frontend": {

 "entry": "frontend/index.html",

 "route": "/image-viewer"

},

"permissions": ["file:read", "file:delete", "file:move"]

}

```

#### 5.3 后端适配

- 将 `module.py` 重命名为 `main.py`。
- 确保类继承 `PluginBase`（路径改为 `from shell.backend.plugin_base import PluginBase`）。
- 所有文件路径操作改为基于 `self.config['directories']['data_root']`，并通过 `/files/` 路由返回 URL。
- 移除对 `safe_path` 的依赖（或保留，但路径基目录改为 `data_root`）。

#### 5.4 前端适配

- 将原 Vue 项目构建为静态文件，放入 `frontend/`。
- 修改 API 调用方式：不再使用 `bridge.call('image_viewer__list_images')`，改为 `parent.pywebview.api['image-viewer__list_images'](...)`。
- 图片 URL 改为 `/files/...` 格式。

---

### 6. 调试与测试

1.**查看插件是否被加载**：启动主程序，控制台会输出 `[PluginManager] ✅ 加载成功: <name>`。

2.**检查前端资源**：在浏览器中直接访问 `http://127.0.0.1:18080/plugins/<name>/frontend/index.html`，确认能正常打开。

3.**检查 API 调用**：在插件前端控制台执行 `parent.pywebview.api['<name>__<method>'](...)`，观察返回结果。

4.**查看 Flask 日志**：所有文件请求都会显示在控制台，便于排查 404 错误。

---

### 7. 常见问题

| 问题 | 原因 | 解决 |

|------|------|------|

| 导航栏不显示插件 | `manifest.json` 格式错误或缺少必填字段 | 检查 JSON 语法，确保 `name`、`frontend.route` 等字段存在 |

| 点击导航无反应 | 前端路由未正确注入 | 检查 `frontend.route` 是否以 `/` 开头，且不与其他插件冲突 |

| iframe 白屏 | 前端入口文件不存在或路径错误 | 确认 `frontend/entry` 指向的文件存在，且 Flask 能访问 |

| API 调用失败 | 方法名拼写错误或后端未注册 | 检查方法名是否与 `register_api` 返回的键一致，调用时使用 `插件名__方法名` |

| 图片无法显示 | URL 路径错误或文件不存在 | 确保图片通过 `/files/` 路由访问，且文件在 `data_root` 下 |

---

### 8. 最佳实践

-**插件命名**：使用 `kebab-case`，避免与 Python 模块名冲突。

-**前端资源**：尽量轻量，避免引入大型框架（除非必要）。

-**权限声明**：如实填写 `permissions`，未来版本将强制执行。

-**版本管理**：遵循语义化版本，方便依赖解析。

-**错误处理**：后端方法应捕获异常并返回有意义的错误信息，避免前端收到 Python 堆栈。

---

按照本指南，你可以快速创建新插件，或将现有功能迁移到 OmniBox v3 架构中。如有疑问，请参考 `hello-world` 示例插件。
