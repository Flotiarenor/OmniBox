# OmniBox 主程序方向：Companion 插件与独立运行环境

> 版本：**v1.0（方向定稿）**
> 日期：2026-08-21
> 状态：**设计阶段**
> 关联文档：`docs/image-tagger-design.md`、`docs/plugin-guide.md`、`docs/adapter-spec.md`

---

## 1. 目标

让 OmniBox 在「轻量内嵌插件」之外，原生支持两类新能力：

1. **Companion 插件**：独立插件作为另一个插件的功能扩展，宿主保持纯净。
2. **自带 venv / 独立运行环境**：重依赖（torch、onnxruntime 等）在插件自己的环境与进程中运行。

首个落地场景：**image-tagger（动漫图像打标）**。

---

## 2. 现状 → 目标差距

| 能力 | 现状 | 目标 | 缺口 |
|------|------|------|------|
| 插件依赖声明 | `manifest.dependencies` + 拓扑排序已实装 | 依赖实例可被访问 | 小 |
| 跨插件后端访问 | `PluginManager.get_plugin_instance(name)` 存在，但未注入插件 | `PluginBase.get_dependency(name)` | 小 |
| 跨插件前端调用 | 前端只能调本插件 API | `Bridge.callPlugin(plugin, method, ...)` | 小 |
| 扩展注册 | 无 | `system_get_plugin_extensions(host)` | 小 |
| 独立 venv | 无；全部插件共享主 venv | manifest `runtime` + 部署脚本按插件建 env | 中 |
| 子进程生命周期 | 未实装（adapter-spec 为 RFC） | `shell/backend/adapter_process.py` 最小实装 | 中 |
| 长驻 Worker 通信 | 无 | stdio JSON-lines 协议 | 中 |
| 长任务进度/取消/续跑 | 无统一约定 | 插件级约定 + Shell 进度组件 | 小 |
| 权限控制 | `permissions` 仅记录 | 继续记录，暂不强制 | 无 |

---

## 3. 主程序需要做的最小改造

### 3.1 PluginManager / PluginBase

```python
# PluginManager._load_plugin 末尾
instance._plugin_manager = self

# PluginBase 新增
def get_dependency(self, name: str):
    """返回已加载依赖插件实例；未加载返回 None。"""
    mgr = getattr(self, '_plugin_manager', None)
    return mgr.get_plugin_instance(name) if mgr else None
```

约束：
- 只允许访问 `manifest.dependencies` 声明的插件（未声明时打印告警并返回 None）
- 不引入循环依赖（现有拓扑排序已兜底）

### 3.2 前端跨插件调用

在 `shell/frontend/public/shell/base.js` 的 `Bridge` 增加：

```js
async function callPlugin(plugin, method, ...args) {
  const api = parent.pywebview && parent.pywebview.api;
  if (!api) throw new Error('PyWebView API 不可用');
  return await api[`${plugin}__${method}`](...args);
}
```

### 3.3 扩展注册表

```python
class PluginBase:
    def get_extensions(self) -> List[dict]:
        """宿主前端可渲染的动作。默认空。"""
        return []
```

```json
{
  "host": "image-viewer",
  "id": "tag-selected",
  "label": "🏷️ 打标",
  "method": "tag_album",
  "scope": "album"
}
```

Shell 聚合 API：

```python
'system_get_plugin_extensions': lambda host=None: manager.get_plugin_extensions(host)
```

宿主前端只写一个泛化循环，**不知道任何扩展的名字**。

### 3.4 manifest.runtime 与独立 venv

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

要求：
- 主程序启动**不加载** runtime（`startup: "lazy"`）
- `deploy.ps1` / `setup-venv.ps1` 发现 `runtime` 后：
  - 用**主程序 Python** 创建 `<plugin>/<runtime.venv>`
  - 执行 `pip install -r <runtime.requirements>`
  - 失败不阻塞主程序，插件显示「运行环境未就绪」
- 打包发布：runtime/venv 与模型不进主程序 exe；作为可选离线包分发

### 3.5 adapter 最小实装（把 RFC 落到代码）

创建 `shell/backend/adapter_process.py`，范围只取当前需要：

```python
class AdapterProcessManager:
    def find_venv_python(self, project_root) -> Path | None
    def start(self, root, entry_args, env_extra=None) -> Popen
    def stop(self, root, timeout=30) -> None
    def is_alive(self, root) -> bool
    def read_log(self, root, since=0) -> list
    def cleanup_all(self) -> None
```

第一版**不做**：HTTP 常驻、端口分配、反向代理、DB 直连、SIGTERM 优雅退出。

### 3.6 长驻 Worker 协议（stdio JSON-lines）

见 `docs/image-tagger-design.md` §7。Shell 不解析协议内容，只负责：
- 进程生命周期
- stdout/stderr 分路与日志持久化
- 退出时 `cleanup_all()`

---

## 4. 主程序不动什么

| 项 | 决定 |
|----|------|
| 设置存储 | 仍用 SettingsStore，每插件一 JSON |
| 文件服务 | 复用 `/files`、`/thumbs`、`get_file_roots` |
| 主题/动效 | 复用 shell/effects.css、motion.js |
| 权限 | 继续只记录不强制，避免本期范围膨胀 |
| 插件发现 | 仍只扫描 `plugins/` 顶层；**不做**嵌套插件目录 |

---

## 5. 兼容性

- 现有插件完全不受影响：新字段、新方法全部可选
- 无 `runtime` 的插件行为与现在一致
- `get_dependency` 未声明依赖时仅告警，不抛异常
- adapter 实装前，image-tagger 可先以「懒 import + 主 venv」原型跑通，再切独立 venv

---

## 6. 里程碑

| 阶段 | 主程序交付 | 插件侧 |
|------|-----------|--------|
| M0 | 本方向文档 + 更新 plugin-guide / adapter-spec | image-tagger 设计文档 |
| M1 | `get_dependency`、`Bridge.callPlugin`、`system_get_plugin_extensions` | Companion 骨架加载成功 |
| M2 | `manifest.runtime` + 部署脚本建独立 venv | Worker 在独立 venv 跑通 1 张 |
| M3 | `adapter_process.py` 最小实装 + 退出清理 | 千张目录任务可用 |
| M4 | 可选：进度 Toast/面板通用组件 | image-viewer 扩展按钮 + 标签虚拟相册 |

---

## 7. 风险与对策

| 风险 | 对策 |
|------|------|
| 独立 venv 打包体积巨大（torch 数 GB） | 模型与 venv 作为可选包，不随主程序分发 |
| Windows 子进程硬杀导致 sidecar 半写 | sidecar 先写临时文件再 rename；任务 JSON 每张一写 |
| 多个重插件同时运行内存爆 | `runtime.startup: lazy` + 设置页显示内存提示；首版串行任务 |
| 跨插件 API 耦合 | 只暴露方法契约，禁止直接 import 宿主代码；扩展注册表为唯一界面 |
| 主进程被 Worker 阻塞 | Worker 全异步；主进程只轮询状态文件 |

---

## 8. 与现有文档的关系

| 文档 | 调整 |
|------|------|
| `plugin-guide.md` | 新增 Companion / 跨插件调用 / runtime 章节 |
| `adapter-spec.md` | RFC 收窄为首个落地范围；新增 stdio-worker 形态 |
| `adapter-guide.md` | 保持 ALAS / 战报识别实例；后续补 image-tagger 实例 |
| `image-tagger-design.md` | 本方向的首个消费者规格 |
