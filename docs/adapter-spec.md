# OmniBox 外部程序接入规范（Adapter Spec）

> 版本：**v1.0（方向定稿）**
> 状态：接入规范定稿。外部程序按自身形态选择接入方式，见 §3；各外部程序的接入实例见 `docs/adapter-guide.md`。
> 适用对象：任何希望作为 OmniBox 插件被接入的**外部大型程序**（独立项目、独立 venv、独立可运行，如 ALAS、战报识别系统）。
> 参考原型：**AzurLaneAutoScript（ALAS）**——控制台 + 独立引擎进程，以「文件/数据库契约 + 进程生命周期 + 日志流 + 状态轮询」弱耦合，验证了管理类重系统接入无需协议握手。
> 相关文档：
> - `docs/plugin-guide.md` — 常规（内嵌）插件开发指南，插件的前端/后端/设置规范仍适用
> - `docs/adapter-guide.md` — 接入实例（ALAS = iframe 嵌入；战报识别系统 = DB 直连）

---

## 0. 词汇与定位

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

---

## 1. 总体模式

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

---

## 2. 通用：进程生命周期与日志（所有形态可复用）

借鉴 ALAS `ProcessManager`，OmniBox 侧提供统一的子进程管理，解决「启动、停止、崩溃检测、日志流、退出清理」——这五件事与形态无关。

### 2.1 启动

| 项 | 约定 |
|----|------|
| 入口发现 | 项目根扫描 `venv` / `.venv` / `env` / `.env`，以存在 `pyvenv.cfg` 判定，取第一个命中 |
| 可执行文件 | `<root>/<venv>/Scripts/python.exe`（Windows） |
| 工作目录 | `cwd = <root>`，相对路径基于它 |
| 编码 | env 恒含 `PYTHONIOENCODING=utf-8`；外部程序如需输出 UTF-8 自行 `sys.stdout.reconfigure` |

### 2.2 停止与崩溃

| 场景 | 约定 |
|------|------|
| 正常停止 | 进程管理器发停止信号（`terminate`/`kill`，Windows 为硬杀） |
| 崩溃检测 | 存活轮询（`poll()`/`is_alive()`）发现退出，UI 标记状态并允许重启 |
| 优雅停止 | 外部程序若支持，通过其自身机制（如 ALAS 的 `stop_event`、识别系统的配置开关）触发，然后等待退出 |
| 兜底 | 超时未退 → `kill()` |
| 应用退出 | `cleanup_all()`：杀全部子进程，防止僵尸 |

### 2.3 日志流

- 子进程 stderr / 日志文件 → 插件后端读 → 前端日志面板轮询渲染（ALAS 用 `multiprocessing.Queue` + 轮询，已证明够用）。
- 状态（运行/停止/异常）由存活轮询 + 最近日志判定。

---

## 3. 形态适配（外部程序按自身形态选一种，可组合）

### 3.1 形态 A：自带 Web UI → iframe 嵌入（推荐，最省）

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

### 3.2 形态 B：只有 DB / 文件 → 直连（最稳，只读为主）

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

### 3.3 形态 C：只有 CLI → 子进程桥（兜底）

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

---

## 4. 错误处理（OmniBox 侧补充，不进外部程序错误码表）

| code | 含义 | 触发 |
|------|------|------|
| `ERR_VENV_NOT_FOUND` | 未找到外部程序 Python 环境 | §2.1 扫描失败 |
| `ERR_SESSION_CRASH` | 子进程异常退出 | 存活轮询发现 |
| `ERR_TIMEOUT` | 单次调用超时 | §3.3 |
| `ERR_DB_NOT_FOUND` | 直连的 DB 不存在 | §3.2（透传外部程序语义） |
| `ERR_PROCESS_BUSY` | 已有实例在运行 | 串行约束 |

**插件后端 API 返回统一结构**：

```json
{"ok": true, "data": { ... }}
{"ok": false, "error": {"code": "ERR_*", "message": "中文提示", "detail": {}}}
```

前端 `Bridge.call` 后统一判断 `ok`，失败走 `Toast.error(message)`。

---

## 5. OmniBox 侧基建接口（shell/backend/adapter_process.py）

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

## 6. 外部程序接入清单（你要做什么）

### 形态 A（自带 Web UI）
- [ ] 能独立启动 Web 服务（本地进程或 Docker）
- [ ] 提供端口/地址配置方式
- [ ] 可选：支持被反代（相对路径/WebSocket）

### 形态 B（DB/文件）
- [ ] 数据落盘在 `<root>/data/*.db` 或 `<root>/<产物目录>`
- [ ] 开启 `PRAGMA journal_mode=WAL` + 短事务
- [ ] 冻结表结构（只增列），声明列名与语义

### 形态 C（CLI）
- [ ] 提供模块入口：`<root>/<venv>/Scripts/python.exe -m <entry> <cmd>`
- [ ] stdout = 结构化 JSON；stderr = 人类日志；退出码 0/1/2
- [ ] 长任务支持进度输出 + 自身停止机制（不用 SIGTERM）

### 所有形态
- [ ] 保持自身独立可运行（旧入口不受影响）
- [ ] 不依赖 OmniBox 的任何常驻服务

---

## 7. 验收用例

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

---

## 8. 版本记录

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 方向定稿 | 收敛自 LSP 会话草案：按外部程序形态分 A/B/C 三路接入；吸收 ALAS 的「进程生命周期 + 日志流 + 文件契约」模式；去除协议握手/进度/取消/RPC |
