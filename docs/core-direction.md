# OmniBox 主程序方向：Companion 插件与独立运行环境

> 版本：**v1.0（方向定稿）**
> 日期：2026-08-21
> 状态：**设计阶段**
> 关联文档：`docs/image-tagger-design.md`、`docs/plugin-guide.md`

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
| 跨插件后端访问 | `PluginBase.get_dependency(name)` 已实装 | 依赖实例可被访问 | 已满足 |
| 跨插件前端调用 | `Bridge.callPlugin(plugin, method, ...)` 已实装 | 跨插件 API 调用 | 已满足 |
| 扩展注册 | `system_get_plugin_extensions(host, placement)` 已实装 | 宿主侧扩展入口渲染 | 已满足 |
| 独立 venv | 无；全部插件共享主 venv | manifest `runtime` + 部署脚本按插件建 env | 中 |
| 子进程生命周期 | 未实装（adapter-spec 为 RFC） | `shell/backend/adapter_process.py` 最小实装 | 中 |
| 长驻 Worker 通信 | 无 | stdio JSON-lines 协议 | 中 |
| 长任务进度/取消/续跑 | 无统一约定 | 插件级约定 + Shell 进度组件 | 小 |
| 权限控制 | `permissions` 仅记录 | 定位为「知情明示」：记录、不强制（最终立场，非临时收窄） | 无 |

---

## 3. 主程序需要做的最小改造

> **本节记录的是当初的改造草案，这些改造后来全部已实装**（见 §2 的状态表）。
> 下面的代码片段保持历史原样，**不是待办**；真实实现以代码为准：
> `PluginBase.get_dependency`（`shell/backend/plugin_base.py`）、
> `Bridge.callPlugin`（`shell/frontend/public/shell/base.js`）、
> `PluginManager.get_plugin_extensions(host, placement)`（`shell/backend/plugin_manager.py`）。
> 编码时请以 `docs/plugin-guide.md` 为准 —— 那份是给插件作者看的契约。

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
  "label": "打标",
  "method": "tag_album",
  "scope": "album"
}
```

Shell 聚合 API：

```python
'system_get_plugin_extensions': lambda host=None, placement=None:
    manager.get_plugin_extensions(host, placement)
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

> 权限立场说明：插件后端运行在主进程内，任何 API 层权限检查都能被直接的 Python 系统调用绕过，属于「假安全、真成本」。因此 **`permissions` 定位为知情明示，永不做运行时强制**；真正的隔离只来自独立的 runtime 进程/venv（§3.4、§3.5）。

| 项 | 决定 |
|----|------|
| 设置存储 | 仍用 SettingsStore，每插件一 JSON |
| 文件服务 | 复用 `/files`、`/thumbs`、`get_file_roots` |
| 主题/动效 | 复用 shell/effects.css、motion.js |
| 权限 | 仅记录、不强制：定位为「知情明示」而非沙箱（运行时无法真正拦截进程内插件，见 §4 说明） |
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
| 本文档 §9–§10 | 外部程序接入规范与接入实例（原 `adapter-spec.md` / `adapter-guide.md`，已并入本文） |
| `image-tagger-design.md` | 本方向的首个消费者规格 |

---

## 9. 外部程序接入规范（Adapter Spec）

> 版本：v1.1（设计定稿，尚未实装）。本节是外部大型程序接入的**通用规范**，与
> §10 的接入实例、`docs/image-tagger-design.md` 的 stdio-worker 规格合为一份。
>
> **状态：规划中。** 当前代码中没有 `shell/backend/adapter_process.py`，`PluginBase`
> 没有 `adapter_*` 方法，`PluginManager` 也不处理 `kind: "local-adapter"`。
>
> 适用对象：任何希望作为 OmniBox 插件被接入的**外部大型程序**（独立项目、独立 venv、
> 独立可运行，如 ALAS、战报识别系统、本地模型推理工具）。参考原型是
> **AzurLaneAutoScript（ALAS）**——控制台 + 独立引擎进程，以「文件/数据库契约 +
> 进程生命周期 + 日志流 + 状态轮询」弱耦合，验证了管理类重系统接入无需协议握手。
>
> **编号约定：保留族号。** `§9.2` 与 `§9.3` 下的 `#### 2.x` / `#### 3.x` 沿用规范原有
> 编号（`§2.1` 启动、`§3.2` DB 直连、`§3.4` stdio-worker），因为 `docs/image-tagger-design.md`
> 等文档按这些号引用；不要重排或重编号。

### 9.1 词汇与定位

| 术语 | 含义 |
|------|------|
| 外部程序 / Server | 被接入的大型程序。独立项目、独立 venv、独立可运行。 |
| OmniBox / Client | 桌面壳，接入方 |
| 插件 | OmniBox 内承载外部程序管理控制 UI 的插件 |

**核心原则（吸收 ALAS 的经验）**：

1. **接入方式跟着外部程序形态走**，不强制一套协议。自带 Web UI 就嵌 UI；只有 DB 就直连 DB；只有 CLI 才考虑进程桥。
2. **文件/数据即契约**：外部程序保持单一数据源，OmniBox 只读不改（需要改走外部程序自己的机制）。
3. **无 HTTP 常驻服务约定、无协议握手、无 RPC**。轻量、弱耦合、可随时断开。
4. **外部程序保持完全独立可运行**；OmniBox 只做「启动/停止 + 读取 + 展示 + 控制」。

### 9.2 总体模式

```
┌── OmniBox (Client) ────────────────────────────────┐
│  Vue 3 Shell ← Bridge.call → 插件后端               │
│    插件后端 → Adapter 三件套：                       │
│    ├─ 形态适配器（iframe 嵌入 / DB 直连 / 进程桥）    │
│    ├─ 进程管理（启动 / 停止 / 存活检测）              │
│    └─ 数据读取（DB / 文件 / 日志）                   │
└──────────────────┬──────────────────────────────────┘
                   │ 视形态而定（§3）
                   ▼
┌── 外部程序 (Server，独立可运行) ────────────────────┐
│  自带 Web UI？  → iframe 直接嵌（ALAS/Jellyfin 类）  │
│  只有 DB/文件？ → sqlite/文件直连（战报识别系统类）    │
│  只有 CLI？    → 子进程桥（兜底）                    │
└─────────────────────────────────────────────────────┘
```

- 无 HTTP 常驻服务约定、无端口分配、无 CORS、无 /health。
- 所有相对路径基于外部程序项目根（下称 `root`）。
- 外部程序可通过 **Companion 插件**接入：宿主插件零改动，接入插件声明 `dependencies`，通过 `PluginBase.get_dependency()` 复用宿主数据根与文件服务。

---

### 9.3 通用：进程生命周期与日志（所有形态可复用）

借鉴 ALAS `ProcessManager`，OmniBox 侧提供统一的子进程管理，解决「启动、停止、崩溃检测、日志流、退出清理」——这五件事与形态无关。

#### 2.1 启动

| 项 | 约定 |
|----|------|
| 入口发现 | 项目根扫描 `venv` / `.venv` / `env` / `.env`，以存在 `pyvenv.cfg` 判定，取第一个命中 |
| 可执行文件 | `<root>/<venv>/Scripts/python.exe`（Windows） |
| 工作目录 | `cwd = <root>`，相对路径基于它 |
| 编码 | env 恒含 `PYTHONIOENCODING=utf-8`；外部程序如需输出 UTF-8 自行 `sys.stdout.reconfigure` |

#### 2.2 停止与崩溃

| 场景 | 约定 |
|------|------|
| 正常停止 | 进程管理器发停止信号（`terminate`/`kill`，Windows 为硬杀） |
| 崩溃检测 | 存活轮询（`poll()`/`is_alive()`）发现退出，UI 标记状态并允许重启 |
| 优雅停止 | 外部程序若支持，通过其自身机制（如 ALAS 的 `stop_event`、识别系统的配置开关）触发，然后等待退出 |
| 兜底 | 超时未退 → `kill()` |
| 应用退出 | `cleanup_all()`：杀全部子进程，防止僵尸 |

#### 2.3 日志流

- 子进程 stderr / 日志文件 → 插件后端读 → 前端日志面板轮询渲染（ALAS 用 `multiprocessing.Queue` + 轮询，已证明够用）。
- 状态（运行/停止/异常）由存活轮询 + 最近日志判定。

---

### 9.4 形态适配（外部程序按自身形态选一种，可组合）

#### 3.1 形态 A：自带 Web UI → iframe 嵌入（推荐，最省）

适用于自带完整 Web 管理界面的外部程序（ALAS、Jellyfin、Portainer、各种自托管服务）。

```
插件 iframe 加载 → http://127.0.0.1:<外部端口>  （或经 OmniBox 反代）
插件后端        → subprocess 拉起外部程序 Web 服务
```

| 项 | 约定 |
|----|------|
| 端口来源 | 外部程序自己的配置（ALAS 默认 22267）；插件设置项可选填 |
| 同源问题 | 跨源 iframe 能显示但不能通信；如需通信，OmniBox Flask 加反向代理路径（注意 WebSocket 需要反代支持） |
| 生命周期 | 插件后端负责拉起/停止外部程序 Web 服务 |
| 前置条件 | 外部程序能独立启动其 Web 服务（Docker 或本地进程皆可） |

#### 3.2 形态 B：只有 DB / 文件 → 直连（最稳，只读为主）

适用于数据落盘在本地 DB/文件的外部程序（战报识别系统）。

```
插件后端 → sqlite3 / 文件读取 → root 下的 data/*.db 或产物文件
```

| 项 | 约定 |
|----|------|
| 连接 | 直接 `sqlite3` 连 `<root>/data/*.db`，不经协议 |
| 只读为主 | 读任意自由；写操作需外部程序开启 WAL + 短事务，并处理 `SQLITE_BUSY`（500ms × 5 次重试），避开其长任务运行期间 |
| 表结构冻结 | 只允许新增列，不得改名/删列/改类型；列名与语义由外部程序契约文档声明 |
| 文件服务 | 产物文件（xlsx、png 等）经 OmniBox `/files/`、`/thumbs/` 基于 root 做路径安全服务；DB 文件不走 `/files/` |

#### 3.3 形态 C：只有 CLI → 子进程桥（兜底）

适用于无 UI、无 DB、仅提供命令行接口的外部程序。

```
插件后端 → subprocess 运行 <root>/<venv>/Scripts/python.exe -m <entry> <cmd>
```

| 项 | 约定 |
|----|------|
| 调用 | 每次任务一个子进程，`cwd = root`，env 含 `PYTHONIOENCODING=utf-8` |
| 输出 | stdout = 结构化 JSON（单对象或 JSON 行流）；stderr = 人类日志 |
| 退出码 | 0 成功 / 1 运行时错误 / 2 用法错误（外部程序自定） |
| 超时 | 单次调用超时（默认 120s）→ 强杀并报 OmniBox 侧错误 |
| 长任务 | 若需进度/停止：进度走 stdout JSON 行流；停止走外部程序自身机制（如 `--stop`、配置文件标志），**禁止依赖 SIGTERM 优雅退出**（Windows 为硬杀） |

#### 3.4 形态 D：长驻 Worker（stdio-worker，本地模型推理工具推荐）

适用于需要**常驻内存模型**、任务频繁的本地工具（自动打标、OCR、图像增强）。该形态是本规范 v1.1 的首个落地目标，协议要求最严格。

```
插件后端 ── Popen ──▶ <root>/<runtime.venv>/Scripts/python.exe <runtime.entry>
                          │
                          │ stdin  = 控制行（JSON-lines）
                          │ stdout = 数据行（JSON-lines）
                          │ stderr = 人类诊断日志
```

| 项 | 约定 |
|----|------|
| 拉起 | `startup: "lazy"`：应用启动不拉起；首次任务时拉起，模型加载完成后输出 `{"type":"ready"}` |
| 协议 | 每行一个 JSON 对象；控制行下行、进度/结果上行；字段见 `docs/image-tagger-design.md` §7 |
| 进度 | Worker 输出 `progress` 行；控制器持久化到任务文件，前端轮询 |
| 取消 | 控制行 `{"type":"cancel","taskId":...}`；Worker 在下一个检查点响应 |
| 崩溃 | EOF → `ERR_WORKER_CRASH`；重启后按任务断点恢复 |
| 环境 | `<runtime.venv>` 由部署脚本按 `<runtime.requirements>` 创建；模型文件不进 OmniBox 主包 |
| 超时 | `runtime.timeoutSeconds` 为任务软上限；超时提示但不强制杀（可取消） |

manifest 扩展：

```json
{
  "kind": "local-adapter",
  "runtime": {
    "kind": "stdio-worker",
    "entry": "backend/runtime/worker.py",
    "venv": "backend/runtime/venv",
    "requirements": "backend/runtime/requirements.txt",
    "startup": "lazy",
    "timeoutSeconds": 3600
  }
}
```

---

### 9.5 错误处理（OmniBox 侧补充，不进外部程序错误码表）

| code | 含义 | 触发 |
|------|------|------|
| `ERR_VENV_NOT_FOUND` | 未找到外部程序 Python 环境 | §2.1 扫描失败 |
| `ERR_SESSION_CRASH` | 子进程异常退出 | 存活轮询发现 |
| `ERR_TIMEOUT` | 单次调用超时 | §3.3 |
| `ERR_DB_NOT_FOUND` | 直连的 DB 不存在 | §3.2（透传外部程序语义） |
| `ERR_PROCESS_BUSY` | 已有实例在运行 | 串行约束 |
| `ERR_WORKER_CRASH` | stdio-worker 异常退出 | EOF / 存活轮询发现 |
| `ERR_RUNTIME_NOT_READY` | 独立 venv 或模型未就绪 | 部署脚本未完成 / modelDir 为空 |

**插件后端 API 返回统一结构**：

```json
{"ok": true, "data": { ... }}
{"ok": false, "error": {"code": "ERR_*", "message": "中文提示", "detail": {}}}
```

前端 `Bridge.call` 后统一判断 `ok`，失败走 `Toast.error(message)`。

---

### 9.6 OmniBox 侧基建接口（shell/backend/adapter_process.py）

> **状态：未实装。** 本节的 `adapter_process.py`、`PluginBase.adapter_*` 均为规划接口；只有真正开始做进程桥时再创建对应文件。


```python
class AdapterProcessError(Exception):
    def __init__(self, code: str, message: str, detail: dict = None): ...

class AdapterProcessManager:
    """外部程序子进程管理（多实例，按 root 复用）"""

    @staticmethod
    def find_venv_python(project_root: str | Path) -> Path | None:
        """扫描 <root>/{venv,.venv,env,.env}，以存在 pyvenv.cfg 判定，返回 python.exe"""

    def start(self, root, entry_args: list, env_extra: dict = None) -> subprocess.Popen:
        """启动外部程序（cwd=root，env 含 PYTHONIOENCODING=utf-8），登记实例"""

    def stop(self, root, timeout: int = 30) -> None:
        """停止：优先触发外部程序自身优雅机制（传入的回调）→ 超时 kill"""

    def is_alive(self, root) -> bool
    def status(self, root) -> str            # running / stopped / crashed

    def run_once(self, root, args: list, timeout: int = 120) -> dict:
        """形态 C：单次子进程调用，捕获 stdout → 解析 JSON → 返回信封；失败抛 AdapterProcessError"""

    def read_log(self, root, since: int = 0) -> list
    def cleanup_all(self) -> None            # 应用退出钩子，杀全部子进程
```

`PluginBase` 增加 adapter 辅助方法：

```python
def adapter_venv_python(self) -> Path | None          # 委托 find_venv_python
def adapter_process(self) -> AdapterProcessManager     # 获取管理器（root = self.setting('root_dir')）
def adapter_run_once(self, *args, timeout=120) -> dict  # 形态 C
def adapter_root(self) -> Path                          # 校验过的外部程序根目录
```

插件 manifest 扩展（`plugin_manager.py` 透传）：

```json
{"name": "battle-report-manager", "kind": "local-adapter", "projectRootKey": "root_dir"}
```

- `kind: "local-adapter"`：声明本插件接入外部项目（信息性，供设置页提示）。
- `projectRootKey`：指向插件 settings_schema 中代表外部项目路径的设置键（默认 `"root_dir"`）。

---

### 9.7 外部程序接入清单（你要做什么）

#### 形态 A（自带 Web UI）
- [ ] 能独立启动 Web 服务（本地进程或 Docker）
- [ ] 提供端口/地址配置方式
- [ ] 可选：支持被反代（相对路径/WebSocket）

#### 形态 B（DB/文件）
- [ ] 数据落盘在 `<root>/data/*.db` 或 `<root>/<产物目录>`
- [ ] 开启 `PRAGMA journal_mode=WAL` + 短事务
- [ ] 冻结表结构（只增列），声明列名与语义

#### 形态 C（CLI）
- [ ] 提供模块入口：`<root>/<venv>/Scripts/python.exe -m <entry> <cmd>`
- [ ] stdout = 结构化 JSON；stderr = 人类日志；退出码 0/1/2
- [ ] 长任务支持进度输出 + 自身停止机制（不用 SIGTERM）

#### 形态 D（stdio-worker）
- [ ] 提供独立 `runtime/venv` 与 `runtime/requirements.txt`
- [ ] 提供 `runtime.entry`，stdin/stdout 走 JSON-lines 协议
- [ ] 支持 `ready` / `progress` / `result` / `task_done` 数据行
- [ ] 支持 `config` / `task` / `cancel` / `shutdown` 控制行
- [ ] 模型只加载一次；崩溃可重启并按任务断点恢复

#### 所有形态
- [ ] 保持自身独立可运行（旧入口不受影响）
- [ ] 不依赖 OmniBox 的任何常驻服务

---

### 9.8 验收用例

| # | 场景 | 期望 |
|---|------|------|
| 1 | 形态 A：插件 iframe 加载外部 Web UI 地址 | 正常显示；启停按钮控制外部进程 |
| 2 | 形态 A：外部进程被 kill | 存活轮询发现 → 状态标 crashed → 可重启 |
| 3 | 形态 B：插件直连 `<root>/data/*.db` | 表结构与契约一致；WAL 下读写不冲突 |
| 4 | 形态 B：DB 不存在 | `ERR_DB_NOT_FOUND`，UI 提示先运行外部程序 |
| 5 | 形态 C：单次调用 | 收到信封 `{ok, data\|error}`，退出码符合约定 |
| 6 | 形态 C：超时 | `ERR_TIMEOUT`，进程被清理 |
| 7 | 日志流 | 外部进程日志持续进入插件日志面板 |
| 8 | 应用退出 | `cleanup_all()` 杀全部子进程，无僵尸 |
| 9 | 两个插件共享同一 root | 进程管理按 root 复用/隔离正确 |
| 10 | stdio-worker 首次任务 | 懒拉起 → `ready` → 任务进度持续输出 |
| 11 | stdio-worker 被 kill | `ERR_WORKER_CRASH`，重启后断点续跑 |
| 12 | 独立 venv 未安装 | `ERR_RUNTIME_NOT_READY`，UI 引导运行部署脚本 |
| 13 | Companion 访问宿主 | `get_dependency('image-viewer')` 拿到实例与文件根目录 |
| 14 | 应用退出 | worker 随 `cleanup_all()` 全部退出，无残留进程 |

---

### 9.9 版本记录

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 方向定稿 | 收敛自 LSP 会话草案：按外部程序形态分 A/B/C 三路接入；吸收 ALAS 的「进程生命周期 + 日志流 + 文件契约」模式；去除协议握手/进度/取消/RPC |
| v1.1 | 2026-08-21 | 目标收敛为 Companion 插件 + 自带 venv / 独立环境；新增形态 D stdio-worker；明确 image-tagger 为首个落地规格 |

---

## 10. 外部程序接入实例（Adapter Guide）

> 版本：v1.1（设计稿）。状态：**规划中，尚未实装**——本节涉及的 `adapter_process.py`、
> `PluginBase.adapter_*`、`kind: "local-adapter"` 当前均未实现。
> 三个实例：**ALAS**（形态 A，自带 Web UI → iframe 嵌入）、
> **战报识别系统**（形态 B，只有 DB → 直连）、**image-tagger**（形态 D，stdio-worker）
> → 独立设计文档 `docs/image-tagger-design.md`。
>
> **编号约定：保留族号。** 实例小节沿用其原有编号（`#### 4.x`–`#### 7.x`、
> `#### 8.`–`#### 10.`），因为识别系统侧的 `adapter-contract.md` 按这些号互相引用。

### 10.0 数据契约的归属

| 文件 | 归属 | 内容 |
|------|------|------|
| 本文档（`docs/core-direction.md` §9–§10） | OmniBox 项目 | 通用接入规范 + 外部程序的接入实例：插件设计、后端 API、设置项 |
| `docs/image-tagger-design.md` | OmniBox 项目 | stdio-worker（形态 D）的首个落地规格 |
| `adapter-contract.md` | 战报识别系统项目 | 识别系统侧数据契约（DB schema 等），由识别系统仓库维护，不在本仓库 |

### 10.1 实例一：ALAS（形态 A — iframe 嵌入）

#### 1. 总体架构

```
┌── OmniBox 插件 alas-manager ──────────────────────────────┐
│  ┌────────────────────────────────────────────────────┐   │
│  │ 插件前端（index.html）                                │   │
│  │  ┌────────────────────────────────────────────┐    │   │
│  │  │ <iframe src="http://127.0.0.1:22267">      │    │   │  ← ALAS Web UI（PyWebIO）
│  │  │  （或经 OmniBox 反代 /alas/ 同源嵌入）        │    │   │
│  │  └────────────────────────────────────────────┘    │   │
│  └────────────────────────────────────────────────────┘   │
│  └ 插件后端（PluginBase）                                   │
│     ├─ subprocess 拉起/停止 ALAS Web 服务（uvicorn）         │
│     ├─ 存活轮询 + 状态                                    │
│     └─ 设置项：root_dir、port、key 密码、是否反代            │
└────────────────────────────────────────────────────────────┘
```

**ALAS 侧**：`gui.py` 启动 uvicorn 服务 `module.webui.app:app`，PyWebIO 渲染全部管理界面（多实例、任务配置、日志、启停、更新、远程访问）——**OmniBox 不需要重造任何功能，纯嵌入**。

#### 2. 插件后端 API

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `start()` | 无 | `{ok}` | 拉起 ALAS Web 服务（`python -m gui` 或经 venv python），非已运行才启动 |
| `stop()` | 无 | `{ok}` | 停止 ALAS Web 服务进程 |
| `status()` | 无 | `{running, port}` | 存活轮询 |
| `get_settings` / `save_settings` | 设置 dict | 统一结构 | 框架提供 |

**设置项**（settings_schema）：

```python
[
    {"key": "root_dir", "label": "ALAS 项目根目录", "type": "directory"},
    {"key": "port", "label": "Web 端口", "type": "number", "default": 22267},
    {"key": "password", "label": "访问密码（--key）", "type": "text"},
    {"key": "use_proxy", "label": "经 OmniBox 反代同源嵌入", "type": "checkbox", "default": False},
]
```

**启动命令**（复用 §9.3 的 `2.1` 启动约定）：

```powershell
<root>/<venv>/Scripts/python.exe gui.py -p <port> [-k <password>]
```

**前端要点**：

- `use_proxy=false`：iframe 直指 `http://127.0.0.1:<port>`（跨源，纯展示，PyWebIO 自管 session）。
- `use_proxy=true`：OmniBox Flask 增加 `/alas/` 反代到 `127.0.0.1:<port>`，同源嵌入；**注意 PyWebIO 走 WebSocket，反代需支持 WS**（如 Flask-Sock）。
- 插件前端只渲染一个全屏 iframe + 顶部工具条（开始/停止/状态）。

**依赖**：无（不依赖 OmniBox 任何协议）。

---

### 10.2 实例二：战报识别系统（形态 B — DB 直连）

#### 3. 总体架构

```
┌── OmniBox 插件 battle-report-manager ────────────────┐
│  插件前端（表格/图表/详情弹窗）                          │
│  插件后端（PluginBase）                                │
│    ├─ sqlite3 直连 <root>/data/battle_reports.db     │
│    └─ 文件读取 <root> 产物（xlsx / dashboard_*.png）    │
└──────────────────────┬────────────────────────────────┘
                       ▼
┌── 战报识别系统（独立可运行，无 HTTP 服务）────────────────┐
│  data/battle_reports.db  (WAL)                          │
│  data/player_teams.db     (WAL)                          │
│  scripts/ / tools/ / dev/  旧入口保持可用                  │
└─────────────────────────────────────────────────────────┘
```

#### 4. DB 直连契约（数据契约单一事实源在 adapter-contract.md）

#### 4.1 `data/battle_reports.db` → 表 `battle_records`

```sql
CREATE TABLE battle_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE,
    type TEXT,
    location TEXT,
    time TEXT,
    result TEXT,
    our_team TEXT,
    enemy_team TEXT,
    our_player TEXT,
    enemy_player TEXT,
    our_alliance TEXT,
    enemy_alliance TEXT,
    enemy_team_img BLOB
);
```

#### 4.2 `data/player_teams.db` → 表 `player_teams`

```sql
CREATE TABLE player_teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enemy_player TEXT NOT NULL,
    enemy_alliance TEXT,
    our_alliance TEXT,
    team_key TEXT NOT NULL,
    team_image BLOB,
    usage_count INTEGER DEFAULT 1,
    last_used_time TEXT,
    UNIQUE(enemy_player, team_key, our_alliance)
);
```

**冻结规则**：列名与语义不变；演进只允许新增列并登记于 `adapter-contract.md`。

#### 4.3 读写边界

- **读**：任意自由（含 BLOB 图片）。
- **写**：识别系统开 `PRAGMA journal_mode=WAL` + 短事务；插件如需编辑/删除，必须处理 `SQLITE_BUSY`（500ms × 5 次重试），并避开识别系统长任务运行期间。
- 图片字段为 PNG BLOB，插件转 base64 供前端 data URL，**不走 `/files/`**。

#### 5. 文件边界

- 插件 `root_dir` = 识别系统项目根。
- OmniBox `/files/`、`/thumbs/` 基于 root 做路径安全，可服务：`export` 产物 xlsx、`dashboard_*.png`、`debug/` 等。
- DB 文件**不走** `/files/`。

#### 6. 分析数据来源（如果做分析插件）

> 识别系统分析计算（`core/analytics.py`）若输出 JSON 到 `<root>/data/` 或产物文件，插件直接读取即可，无需 CLI 调用。
> 若识别系统只有 CLI 形式提供分析，则走 §9.4 的形态 C（`adapter_run_once`）作为兜底。

| 数据 | 位置 | 消费方式 |
|------|------|---------|
| 战报明细 | `data/battle_reports.db` | sqlite 直连 |
| 玩家队伍 | `data/player_teams.db` | sqlite 直连 |
| 分析统计 | `data/analytics_*.json`（若识别系统产出） | 文件读取 |
| 导出产物 | `同服敌方阵容统计.xlsx` 等 | `/files/` |

#### 7. 插件设计（可选，按需拆）

> **下表全部是规划中的插件名，一个都还不存在**：`plugins/` 当前只有 7 个真实插件
> （`image-viewer`、`media-player`、`manga-library`、`document-reader`、`image-cleaner`、
> `pixiv-sync`、`netease-music`）。本表用于描述未来形态，不要当作可用的插件清单。

| 插件 | 形态 | 后端 API | 前端 |
|------|------|---------|------|
| `battle-report-manager`（第一批） | B | `list(page,per,alliance,player)` / `detail(id)` / `search(player)` / `get_image(id)` | 表格 + 分页 + 筛选 + 详情弹窗 |
| `battle-report-analysis`（第二批） | B/C | `analyze(alliance,days)` → 读分析 JSON 或形态 C 兜底 | ECharts 图表 |
| `player-teams-export`（第二批） | B | `list_export_tasks()` / `open_output(rel)` | 任务选择 + 文件列表下载 |
| `siege-calculator`（第一批，纯前端） | 无依赖 | 无（或空 PluginBase） | 三 Tab 计算器 |

**设置项**（公共）：

```python
[{"key": "root_dir", "label": "识别系统项目根目录", "type": "directory"}]
```

---

### 10.3 公共部分

#### 8. 错误处理矩阵

| 场景 | 用户可见提示 | 插件 API 返回 |
|------|-------------|--------------|
| venv 未找到 | 「未找到外部程序 Python 环境，请检查 root_dir」 | `ERR_VENV_NOT_FOUND` |
| ALAS Web 服务未启动 | 「ALAS 服务未运行，请点击启动」 | `{ok:false,error:{code:'ERR_NOT_RUNNING'}}` |
| 子进程异常退出 | 「进程异常退出，可重启」 | `ERR_SESSION_CRASH` |
| DB 不存在 | 「数据库不存在，请先运行识别系统」 | `ERR_DB_NOT_FOUND` |
| 单次调用超时 | 「请求超时，请稍后重试」 | `ERR_TIMEOUT` |
| 应用退出 | 静默清理全部子进程 | `cleanup_all()` |

#### 9. 验收与联调顺序

> 本节第 2~3 步涉及 `alas-manager` / `battle-report-manager`，它们**尚未实装**
> （见 §7 的说明）。当前可执行的只有第 1 步的通用规范部分与第 4 步的回归检查
> （`tools/check_plugins.py` + `python -m unittest discover -s tests`）。

1. **OmniBox 单测**：`adapter_process.py` 对假项目（含 venv + 假脚本）测启动/停止/存活/`run_once`
2. **实例一联调**：`alas-manager` 拉起 ALAS Web UI，iframe 显示，启停可控
3. **实例二联调**：`battle-report-manager` 直连 `battle_reports.db`，列表/详情/图片正常
4. **回归**：外部程序旧入口（ALAS `gui.py`、识别系统 `pipeline.py`/`web_ui.py`）不受影响

#### 10. 版本记录

| 版本 | 日期 | 说明 |
|------|------|------|
| v0.2 | 草稿 | 一次性 CLI 桥接方案（已弃用） |
| v1.0 | 重写 | 对齐本节通用规范 v1.0：多形态接入；实例一 ALAS=iframe 嵌入，实例二 战报识别系统=DB 直连；去除 LSP 会话协议 |
