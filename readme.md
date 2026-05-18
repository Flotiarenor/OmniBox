# 个人数字中心

<p align="center">
<strong>一个基于 PyWebView + 原生 JavaScript 构建的本地多媒体管理应用</strong><br>
集图片浏览、漫画库管理与阅读、在线漫画下载、**小说阅读**于一体。采用极简解耦设计，后端 Python 负责核心逻辑与文件操作，前端负责交互与展示。
</p>

<p align="center">
<img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python">
<img src="https://img.shields.io/badge/License-Apache%202.0-green.svg" alt="License">
<img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Platform">
</p>

---

## ✨ 核心特性

- **🖼️ Justified Layout 瀑布流**：仿 Google Photos 的自适应行高排版，基于图片真实宽高比动态计算，无裁切展示。
- **🌉 极简 RPC 通信**：前端通过统一的 `bridge.call()` 调用后端方法，无需关心 HTTP 请求细节。
- **🔒 路径安全沙箱**：后端所有文件操作强制校验路径前缀，杜绝路径穿越攻击（`../`）。
- **📚 多级视图状态机**：漫画库采用 `home -> chapters -> images` 视图栈管理，支持无限层级返回。
- **📄 元数据驱动**：漫画信息完全依赖本地 `album_info.json`，离线可用，无需联网即可浏览详情。
- **📖 小说阅读器**：支持 TXT 格式小说，自动章节分割、编码检测、阅读进度保存、自定义阅读设置。

---

## 🏗 系统架构设计

应用采用 **C/S 双进程混合架构**，确保性能与安全：

```text
┌─────────────────────────────────────────────────────────────┐
│                        WebView 前端                         │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ ImageViewer  │  │ MangaLibrary │  │ DownloadCenter│      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                 │               │
│         └─────────────────┼─────────────────┘               │
│                           │                                 │
│                     bridge.call()                           │
└───────────────────────────┬─────────────────────────────────┘
                            │ RPC 调用
┌───────────────────────────┼─────────────────────────────────┐
│        PyWebView 宿主进程│                                 │
│                           ▼                                 │
│                    ┌─────────────┐                          │
│                    │   AppAPI    │                          │
│                    └──────┬──────┘                          │
│           ┌──────────────┼──────────────┐                  │
│           ▼              ▼              ▼                  │
│    ┌────────────┐ ┌────────────┐ ┌────────────┐           │
│    │FileModule  │ │ImageModule │ │MangaModule │           │
│    └─────┬──────┘ └─────┬──────┘ └─────┬──────┘           │
│          │              │              │                    │
│          └──────────────┼──────────────┘                    │
│                         ▼                                   │
│                   本地文件系统                               │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│              Flask 文件服务器 (独立线程 :18080)              │
│                                                             │
│  前端通过 <img src="http://127.0.0.1:18080/thumbs/...">     │
│  直接访问缩略图和封面，利用浏览器原生缓存与懒加载机制        │
└─────────────────────────────────────────────────────────────┘
```

---

## 📁 目录结构

```text
项目根目录/
├── main.py                     # 应用入口，初始化 PyWebView 与 Flask
├── backend/
│   ├── api.py                  # RPC 接口层，聚合各 Module
│   ├── file_server.py          # Flask 静态文件服务器
│   └── modules/
│       ├── file_module.py      # 文件操作（移动/删除/目录树）
│       ├── image_module.py     # 图片扫描与尺寸缓存
│       ├── manga_module.py     # 漫画库逻辑与状态管理
│       ├── novel_module.py     # 小说管理模块
│       ├── novel_parser.py     # 小说解析器（章节分割、编码检测）
│       └── jm_tool.py          # jmcomic 库封装
├── frontend/
│   ├── index.html              # 单页应用入口
│   ├── css/                    # 样式文件
│   │   └── views/
│   │       ├── download-center.css # 下载样式
│   │       ├── image-viewer.css # 图片浏览器样式
│   │       ├── manga-library.css # 漫画库首页样式
│   │       └── novel-reader.css # 小说阅读器样式
│   └── js/
│       ├── bridge.js           # 通信桥接器
│       ├── view-manager.js     # 视图路由管理
│       ├── components/         # 通用组件（Tree, Lightbox, ContextMenu 等）
│       └── views/              # 视图业务逻辑
│           ├── image-viewer.js
│           ├── manga-library.js
│           ├── manga-reader.js
│           └── novel-reader.js # 小说阅读器逻辑
└── [图库根目录]/
    ├── .cache/                 # 缩略图与状态缓存
    │   └── thumbs/             # 图片缩略图
    ├── [图片子目录]/            # 图片浏览模块管理
    ├── [本子根目录]/
    │   ├── .cache/             # 缩略图与状态缓存
    │   │   ├── covers/         # 漫画封面
    │   │   └── manga_state.json# 收藏与阅读历史
    │   └── 123456/             # 以漫画ID命名
    │       ├── album_info.json # 漫画元数据
    │       ├── 00001.jpg       # 漫画图片
    │       └── ...
    └── [小说根目录]/
        ├── .novel_state/       # 小说状态缓存
        │   ├── .novel_cache.json    # 章节信息缓存
        │   └── .novel_progress.json # 阅读进度缓存
        ├── 小说名-作者.txt     # 小说文件（命名格式：标题-作者.txt）
        └── ...
```

---

## 💻 核心模块详解

### 🖼️ 前端：ImageViewer (图片浏览器)

**文件**: `js/views/image-viewer.js`

 **Justified Layout (合理布局)**：

1. 设定目标行高 `targetHeight`（用户可在设置中调节）。
2. 将图片按原始宽高比换算为在 `targetHeight` 下的等比宽度。
3. 逐张填入当前行，当累计宽度超过容器宽度时，锁定该行。
4. **按比例缩放**：计算当前行的实际宽度与容器宽度的差值，动态拉伸该行所有图片的高度和宽度，确保最后一像素完美贴边。
5. 最后一行如果未填满，使用原始 `targetHeight` 渲染，避免过度拉伸。

**交互设计**：

- **多选模式**：通过状态机 `isMultiSelectMode` 切换。开启后，点击图片变为选中/取消，工具栏滑出批量操作按钮（删除、移动）。
- **右键菜单**：统一使用 `ContextMenu` 组件，根据当前模式动态显示菜单项。

### 📚 前端：MangaLibrary (漫画库)

**文件**: `js/views/manga-library.js`

**视图层级状态机**：

```text
[Home 首页] 
   ├── 点击单章节漫画 ──────> [Images 图片列表]
   │                            └── 返回 -> [Home]
   └── 点击多章节漫画 ──────> [Chapters 章节列表]
                                └── 点击章节 ──> [Images 图片列表]
                                                   └── 返回 -> [Chapters]
```

通过 `currentViewLevel` 和动态绑定 `detail-back` 按钮的 `onclick` 事件，实现了一套 HTML 模板复用于三种视图状态。

**性能优化**：

- 收藏状态切换时，不重新拉取全量列表，而是直接通过 DOM 查询 `document.querySelectorAll('.fav-star')` 局部更新 UI，避免页面闪烁。

### 📖 前端：MangaReader (沉浸式阅读器)

**文件**: `js/views/manga-reader.js`

- **全局单例**：挂载在 `window.mangaReader` 上，整个应用生命周期只初始化一次 DOM。
- **键盘导航**：监听 `ArrowLeft`/`A` 上一页，`ArrowRight`/`D` 下一页，`Escape` 退出。
- **URL 智能拼接**：自动判断后端返回的 URL 是否包含 `http`，决定是否拼接 `FILE_SERVER` 前缀。

### 📖 前端：NovelReader (小说阅读器)

**文件**: `js/views/novel-reader.js` | `css/views/novel-reader.css`

**功能特性**：

- **自动章节分割**：支持多种章节标题格式（第X章、第X节、数字序号等），自动识别并分割章节。
- **编码自动检测**：使用 `chardet` 库自动检测文件编码，支持 UTF-8、GBK、GB2312 等常见编码，也可手动切换。
- **平滑阅读体验**：
  - 滚动到章节末尾时自动加载下一章（无缝衔接）
  - 键盘快捷键导航（← 上一章、→ 下一章、↑↓ 滚动、Esc 返回）
  - 章节选择器快速跳转
- **阅读进度保存**：自动保存当前阅读位置（章节 + 滚动位置），下次打开自动恢复。
- **个性化设置**：
  - 字号调节（12px - 32px）
  - 行距调节（1.2 - 2.5）
  - 字间距调节（0px - 5px）
  - 多主题切换（明亮/暗黑/护眼/绿色/蓝色/自定义）
  - 自定义背景色和文字色
- **文本选择**：阅读区域支持文本选择和右键复制，方便摘录。

**后端架构**：

```python
# backend/modules/novel_parser.py
class NovelParser:
    """
    小说文件解析器
    - 使用 chardet 自动检测编码
    - 支持正则匹配章节标题
    - 按段落边界分割，避免截断突兀
    """
  
# backend/modules/novel_module.py
class NovelModule:
    """
    小说管理模块
    - 列表只返回元信息（标题、作者、进度）
    - 章节信息延迟加载（点击后才解析）
    - 内容缓存（LRU策略，缓存最近5章）
    - 进度持久化（JSON文件）
    """
```

**交互流程**：

```text
[小说列表] 
   ├── 显示所有 TXT 文件
   ├── 显示阅读进度百分比
   └── 点击进入阅读器
        ├── [阅读器视图]
        │   ├── 工具栏（返回、章节选择、上下章）
        │   ├── 设置栏（字号、行距、字间距、主题、编码）
        │   ├── 内容区（支持文本选择）
        │   └── 进度条
        └── 键盘快捷键
            ├── ← → 切换章节
            ├── ↑↓ 滚动页面
            ├── Space 翻页
            └── Esc 返回列表
```

---

### 🔒 后端：FileModule (文件系统安全网关)

**文件**: `backend/modules/file_module.py`

**核心机制 - 路径安全校验**：

```python
def _is_safe(self, rel_path: str) -> bool:
    target = (self.base_dir / rel_path).resolve()
    return str(target).startswith(str(self.base_dir))
```

所有传入的相对路径，在转为绝对路径后，必须以 `base_dir` 为前缀。这从根本上杜绝了 `../../etc/passwd` 这类的路径穿越攻击。

**智能重命名移动**：
在 `move` 操作中，如果目标目录已存在同名文件，自动追加 `_1`, `_2` 后缀，而非直接覆盖，保护用户数据。

### ⚡ 后端：ImageModule (图片扫描与缓存引擎)

**文件**: `backend/modules/image_module.py`

Justified Layout 必须预先知道图片的宽高比。使用 PIL 读取图片尺寸极快，但在万级图库下仍会产生 I/O 延迟。

- **缓存策略**：以文件绝对路径的 MD5 为键，缓存 `{mtime, width, height}` 到 `.cache/meta/`。
- **失效机制**：比对文件的 `mtime`（修改时间），若与缓存不一致则重新读取。

### 🗂 后端：MangaModule (漫画元数据与状态管理)

**文件**: `backend/modules/manga_module.py`

模块不依赖任何爬虫 API 运行，完全依赖本地文件系统：

1. 扫描根目录下的文件夹。
2. 尝试读取每个文件夹内的 `album_info.json` 获取标题、作者、标签。
3. 如果没有 JSON，则降级使用文件夹名作为标题。

**封面寻找策略**：
按优先级寻找封面图：`子章节目录/00001.jpg` > `根目录/00001.jpg` > `根目录/cover.jpg`。

### 🌐 后端：JMTool (下载与元数据抓取适配器)

**文件**: `backend/modules/jm_tool.py`

封装第三方库 `jmcomic`，提供元数据抓取与图片下载功能。直接调用 `jmcomic.JmOption.construct()` 构建最基础的配置，不依赖复杂的 YAML 配置文件和插件系统，确保下载流程可控。

---

## 🔄 数据流转与通信机制

前端与后端的一切交互均通过 `bridge.js` 完成：

```javascript
// 1. 前端调用示例
const data = await bridge.call('image_list', path, page);

// 2. bridge.js 核心逻辑映射到 Python 的 AppAPI.image_list
async call(method, ...args) {
    return await this._api[method](...args);
}
```

**图片资源流转**：

1. 前端调用 `image_list`，后端返回 `[ { url: 'path/to/img.jpg' } ]`。
2. 前端将 `url` 传给 `bridge.thumbUrl(url)`，拼接为 `http://127.0.0.1:18080/thumbs/path/to/img.jpg`。
3. 浏览器通过 HTTP GET 请求 Flask 服务器获取缩略图，触发浏览器原生缓存。

---

## 💾 数据持久化方案

应用采用轻量级的文件系统持久化，无需数据库：

| 数据类型      | 存储位置                                     | 格式 | 读写模块             |
| ------------- | -------------------------------------------- | ---- | -------------------- |
| 图片尺寸缓存  | `[图库]/.cache/meta/[hash].json`           | JSON | ImageModule          |
| 漫画收藏/历史 | `[本子]/.cache/manga_state.json`           | JSON | MangaModule          |
| 漫画元数据    | `[本子]/[ID]/album_info.json`              | JSON | JMTool / MangaModule |
| 下载任务状态  | `.jmcomic_state/download_state.json`       | JSON | DownloadModule       |
| 小说章节缓存  | `[小说]/.novel_state/.novel_cache.json`    | JSON | NovelModule          |
| 小说阅读进度  | `[小说]/.novel_state/.novel_progress.json` | JSON | NovelModule          |

---

## 🚀 快速开始

### 1. 环境依赖

- Python 3.8+
- [Microsoft Edge WebView2](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)

### 2. 一键部署（推荐）

> 项目已内置 `requirements.txt` 依赖文件，使用 PowerShell 脚本可自动完成虚拟环境创建和依赖安装。

```powershell
# 在项目根目录下执行
.\deploy.ps1
```

脚本会自动：

- 检测系统已安装的 Python 版本
- 在项目目录下创建 `venv` 虚拟环境
- 从 `requirements.txt` 安装所有依赖

### 3. 手动安装（备选）

如果你希望手动管理环境，也可以按传统方式操作：

```bash
# 创建虚拟环境（可选）
python -m venv venv
.\venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 4. 启动项目

```bash
# 确保虚拟环境已激活
.\venv\Scripts\activate

# 运行主程序
python main.py
```

### 5. 配置目录

修改 `main.py` 顶部的路径常量：

```python
IMAGE_DIR = r"G:\图库"        # 图片浏览模块的根目录
MANGA_DIR = r"G:\图库\本子"   # 漫画库模块的根目录
NOVEL_DIR = r"G:\图库\小说"   # 小说阅读模块的根目录
```

### 6. 启动应用

```bash
python main.py
```

应用将自动：

1. 在 `18080` 端口启动 Flask 文件服务。
2. 创建 PyWebView 窗口加载前端界面。
3. 将 Python API 对象注入到 JS 上下文。

---

## 📄 开源协议

本项目基于 [Apache License 2.0](http://www.apache.org/licenses/LICENSE-2.0) 协议开源。
