# OmniBox

**OmniBox** 是一个轻量级、可扩展的桌面应用框架。它本身不包含任何业务功能，所有能力均由可插拔的插件提供。你可以像安装浏览器扩展一样，自由添加、移除功能模块。

---

## ✨ 特性

- **极简核心**：主程序仅提供窗口容器、插件管理和安全沙箱，体积小巧。
- **完全解耦**：插件与主程序独立开发、独立构建，技术栈自由（前端可用 Vue、React 或纯 HTML）。
- **热插拔**：将插件文件夹放入 `plugins/` 目录即可启用，删除即卸载，重启后生效。
- **安全隔离**：插件运行在独立 iframe 中，后端 API 通过命名空间隔离，权限可声明。
- **易于分发**：主程序可打包为便携目录/压缩包，插件可离线安装或通过内置商店获取（规划中）。
- **跨平台**：支持 Windows、Linux，并提供 Web-only 模式，可通过浏览器直接访问。
- **统一外观**：内置浅色/深色主题切换，支持自定义 CSS 变量级颜色配置，全插件自动同步。
- **通用布局**：统一的工具栏、侧边栏、内容区样式（`.view-toolbar`、`.view-sub-sidebar` 等），插件无需重复定义。
- **轻量插件依赖**：插件可通过 `backend/libs` 携带纯 Python 依赖，无需安装进主 venv，删除目录即可卸载。
- **Companion 插件体系**：支持扩展注册、宿主内嵌、跨插件调用，便于在宿主内部集成清理/打标等增强功能。

---

## 🏗️ 架构概览

```mermaid
graph TB
    subgraph Container["🖥️ PyWebView 桌面容器"]
        subgraph Shell["前端壳 — Vue 3 应用壳 (Shell)"]
            Nav["导航栏"]
            Iframe["<iframe> 插件容器"]
        end
        Bridge["🔗 PyWebView JS Bridge"]
        subgraph Backend["后端壳 — Python 后端壳"]
            PM["PluginManager"]
            Flask["Flask 服务器"]
        end
    end

    Nav --> Iframe
    Bridge --> Shell
    Bridge --> Backend
    PM --> Flask
```

- **前端壳**：Vue 3 + Vue Router，动态加载插件清单并生成导航和 iframe 路由。插件 iframe 保持存活（keep-alive），切换时媒体播放不中断。
- **后端壳**：Python 插件管理器，负责插件的发现、依赖解析、加载和 API 聚合。
- **通信**：所有页面同源（`http://127.0.0.1`），插件前端可直接调用 `parent.pywebview.api` 访问后端方法；Web-only 模式下自动使用 HTTP API 桥接。
- **Web-only 模式**：可通过 `python main.py --web-only` 启动，无需桌面窗口，适合 Linux 服务器 / NAS / 浏览器访问。
- **插件**：每个插件是一个独立文件夹，包含 `manifest.json`、`backend/`（Python 代码）和 `frontend/`（静态网页资源）。插件可通过 `backend/libs` 携带纯 Python 依赖。
- **配置**：主程序配置位于 `.config/app.yaml`，插件设置自动持久化到 `.config/plugins/<插件名>.json`（git 忽略）。设置变更后插件自动重载，无需重启应用。

### 插件通用布局

Shell 在 `base.css` 中统一注入以下布局类，**所有插件应直接使用，无需在自己的 CSS 中重复定义**：

| CSS 类 | 用途 |
|--------|------|
| `.view-body` | 主内容区容器（flex 列，自动伸缩） |
| `.view-toolbar` | 顶部工具栏（48px，统一背景/边框） |
| `.toolbar-group` | 工具栏内的按钮组（flex 行，8px 间距） |
| `.view-sub-sidebar` | 左侧子侧边栏（240px） |
| `.sub-sidebar-header` | 侧边栏标题行 |
| `.sub-sidebar-footer` | 侧边栏底部统计区 |
| `.view-content` | 内容滚动区（flex: 1，自动 overflow） |

### 外观主题

- 通过 `document.documentElement` 上的 `data-theme="dark"` / `data-theme="light"` 属性切换主题。
- 设置页提供 🎨 **外观设置面板**：浅色/深色切换 + 14 项 CSS 变量颜色自定义（下拉框选择预设色）。
- 自定义颜色通过 `data-custom-colors` 属性 **自动同步到所有插件 iframe**，插件前端的 CSS 变量实时更新。

### 全屏联动

视频播放器等需要全屏的插件可通过设置 `parent.document.documentElement` 的 `data-video-fullscreen` 属性隐藏导航栏，Shell 端通过 MutationObserver 监听并自动处理。

---

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+
- npm 9+
- Windows：PowerShell 5.1+
- Linux：bash / Python venv

### Windows 一键安装与运行

项目提供了 `deploy.ps1` 脚本，可自动完成虚拟环境创建、Python 依赖安装以及前端壳的构建。环境与依赖的统一入口为 `setup-venv.ps1`（`deploy.ps1` 及发布构建脚本都会复用该入口，不再各自内联依赖管理）。

```powershell
git clone https://github.com/Flotiarenor/OmniBox.git
cd OmniBox
.\deploy.ps1
```

如需单独准备 Python 环境（或使用交互式 pip 管理台）：

```powershell
.\setup-venv.ps1 -Install     # 非交互：确保 venv 并安装 requirements.txt
.\setup-venv.ps1              # 交互式 pip 管理台
```

### Linux / Web-only 运行

```bash
git clone https://github.com/Flotiarenor/OmniBox.git
cd OmniBox

bash setup-venv.sh           # 统一环境入口：创建 venv + 安装依赖

cd shell/frontend
npm install
npm run build
cd ../..

python main.py --web-only    # 或 venv/bin/python main.py --web-only
```

然后浏览器访问：

```text
http://127.0.0.1:18081
```

### 安装插件

1. 将插件文件夹放入项目根目录下的 `plugins/` 目录。
2. 重启应用，插件将自动出现在导航栏中。
3. 打包版还可以把插件放入可执行文件旁的 `plugins/` 目录，或用户数据目录下的 `plugins/`。

### 配置文件

主程序配置位于 `.config/app.yaml`：

```yaml
server:
  host: "127.0.0.1"
  port: 18081
directories:
  data_root: "./data"
```

插件设置自动保存到 `.config/plugins/` 目录，修改后无需重启应用即可生效。

## 🧩 插件开发

完整的插件开发指南请参阅 [插件开发文档](./docs/plugin-guide.md)。
该文档包含插件结构、`manifest.json` 规范、后端与前端开发示例、调试技巧以及最佳实践。

插件生态方向（Companion 插件 / 独立运行环境）见
[主程序方向文档](./docs/core-direction.md)；
图像自动打标插件设计见 [image-tagger 设计文档](./docs/image-tagger-design.md)；
相册清理插件设计见 [image-cleaner 设计文档](./docs/image-cleaner-design.md)。

---

## 📦 打包发布

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File docs/Releases/build-release.ps1
```

- 会自动构建前端
- 自动通过 `setup-venv.ps1 -Install` 准备虚拟环境与依赖，再用项目 venv 的 Python 执行 PyInstaller
- 输出到 `docs/Releases/`

### Linux

```bash
bash docs/Releases/build-release.sh
```

- 会自动构建前端
- 自动通过 `setup-venv.sh` 准备虚拟环境与依赖，再用项目 venv 的 Python 执行 PyInstaller
- 输出到 `docs/Releases/OmniBox/`
- 并生成 `OmniBox_日期.tar.gz`

> 构建脚本与部署脚本共享统一环境入口（`setup-venv.ps1` / `setup-venv.sh`），依赖管理不再各自内联。

## 📄 许可证

本项目基于 **Apache License 2.0** 开源。详见 [LICENSE](./LICENSE) 文件。

```
Copyright 2025 OmniBox Contributors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

 http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！请确保遵循项目的代码规范，并通过现有测试。

---

## 📧 联系方式

- 项目主页：[https://github.com/Flotiarenor/OmniBox](https://github.com/Flotiarenor/OmniBox)
- 问题反馈：[Issues](https://github.com/Flotiarenor/OmniBox/issues)
