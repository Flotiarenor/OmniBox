# OmniBox 外部程序接入实例（Adapter Guide）

> 版本：**v1.0（按 adapter-spec v1.0 重写，取代 v0.2）**
> 上游通用规范：[adapter-spec.md](./adapter-spec.md)（v1.0）— 接入方式唯一事实源，本文档不重新定义规范。
> 本文档：两个接入实例的落地设计
> - 实例一：**ALAS**（形态 A，自带 Web UI → iframe 嵌入）
> - 实例二：**战报识别系统**（形态 B，只有 DB → 直连）

---

## 0. 文档定位

| 文件 | 归属 | 内容 |
|------|------|------|
| `docs/adapter-spec.md` | OmniBox 项目 | 通用接入规范：进程生命周期 / 日志流 / 三形态适配（单一事实源） |
| **本文档** | OmniBox 项目 | 两个外部程序的接入实例：插件设计、后端 API、设置项 |
| `adapter-contract.md` | 战报识别系统项目 | 识别系统侧数据契约（DB schema 等），由识别系统仓库维护 |

---

# 实例一：ALAS（形态 A — iframe 嵌入）

## 1. 总体架构

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

## 2. 插件后端 API

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `start()` | 无 | `{ok}` | 拉起 ALAS Web 服务（`python -m gui` 或经 venv python），非已运行才启动 |
| `stop()` | 无 | `{ok}` | 停止 ALAS Web 服务进程 |
| `status()` | 无 | `{running, port}` | 存活轮询 |
| `get_settings` / `save_settings` | 设置 dict | 统一结构 | 框架提供 |

**设置项**（settings_schema）：

```python
[
    {"key": "root_dir", "label": "ALAS 项目根目录", "type": "folder", "central": True},
    {"key": "port", "label": "Web 端口", "type": "number", "default": 22267},
    {"key": "password", "label": "访问密码（--key）", "type": "text"},
    {"key": "use_proxy", "label": "经 OmniBox 反代同源嵌入", "type": "checkbox", "default": False},
]
```

**启动命令**（复用 adapter-spec §2.1）：

```powershell
<root>/<venv>/Scripts/python.exe gui.py -p <port> [-k <password>]
```

**前端要点**：

- `use_proxy=false`：iframe 直指 `http://127.0.0.1:<port>`（跨源，纯展示，PyWebIO 自管 session）。
- `use_proxy=true`：OmniBox Flask 增加 `/alas/` 反代到 `127.0.0.1:<port>`，同源嵌入；**注意 PyWebIO 走 WebSocket，反代需支持 WS**（如 Flask-Sock）。
- 插件前端只渲染一个全屏 iframe + 顶部工具条（开始/停止/状态）。

**依赖**：无（不依赖 OmniBox 任何协议）。

---

# 实例二：战报识别系统（形态 B — DB 直连）

## 3. 总体架构

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

## 4. DB 直连契约（数据契约单一事实源在 adapter-contract.md）

### 4.1 `data/battle_reports.db` → 表 `battle_records`

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

### 4.2 `data/player_teams.db` → 表 `player_teams`

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

### 4.3 读写边界

- **读**：任意自由（含 BLOB 图片）。
- **写**：识别系统开 `PRAGMA journal_mode=WAL` + 短事务；插件如需编辑/删除，必须处理 `SQLITE_BUSY`（500ms × 5 次重试），并避开识别系统长任务运行期间。
- 图片字段为 PNG BLOB，插件转 base64 供前端 data URL，**不走 `/files/`**。

## 5. 文件边界

- 插件 `root_dir` = 识别系统项目根。
- OmniBox `/files/`、`/thumbs/` 基于 root 做路径安全，可服务：`export` 产物 xlsx、`dashboard_*.png`、`debug/` 等。
- DB 文件**不走** `/files/`。

## 6. 分析数据来源（如果做分析插件）

> 识别系统分析计算（`core/analytics.py`）若输出 JSON 到 `<root>/data/` 或产物文件，插件直接读取即可，无需 CLI 调用。
> 若识别系统只有 CLI 形式提供分析，则走 adapter-spec 形态 C（`adapter_run_once`）作为兜底。

| 数据 | 位置 | 消费方式 |
|------|------|---------|
| 战报明细 | `data/battle_reports.db` | sqlite 直连 |
| 玩家队伍 | `data/player_teams.db` | sqlite 直连 |
| 分析统计 | `data/analytics_*.json`（若识别系统产出） | 文件读取 |
| 导出产物 | `同服敌方阵容统计.xlsx` 等 | `/files/` |

## 7. 插件设计（可选，按需拆）

| 插件 | 形态 | 后端 API | 前端 |
|------|------|---------|------|
| `battle-report-manager`（第一批） | B | `list(page,per,alliance,player)` / `detail(id)` / `search(player)` / `get_image(id)` | 表格 + 分页 + 筛选 + 详情弹窗 |
| `battle-report-analysis`（第二批） | B/C | `analyze(alliance,days)` → 读分析 JSON 或形态 C 兜底 | ECharts 图表 |
| `player-teams-export`（第二批） | B | `list_export_tasks()` / `open_output(rel)` | 任务选择 + 文件列表下载 |
| `siege-calculator`（第一批，纯前端） | 无依赖 | 无（或空 PluginBase） | 三 Tab 计算器 |

**设置项**（公共）：

```python
[{"key": "root_dir", "label": "识别系统项目根目录", "type": "folder", "central": True}]
```

---

# 公共

## 8. 错误处理矩阵

| 场景 | 用户可见提示 | 插件 API 返回 |
|------|-------------|--------------|
| venv 未找到 | 「未找到外部程序 Python 环境，请检查 root_dir」 | `ERR_VENV_NOT_FOUND` |
| ALAS Web 服务未启动 | 「ALAS 服务未运行，请点击启动」 | `{ok:false,error:{code:'ERR_NOT_RUNNING'}}` |
| 子进程异常退出 | 「进程异常退出，可重启」 | `ERR_SESSION_CRASH` |
| DB 不存在 | 「数据库不存在，请先运行识别系统」 | `ERR_DB_NOT_FOUND` |
| 单次调用超时 | 「请求超时，请稍后重试」 | `ERR_TIMEOUT` |
| 应用退出 | 静默清理全部子进程 | `cleanup_all()` |

## 9. 验收与联调顺序

1. **OmniBox 单测**：`adapter_process.py` 对假项目（含 venv + 假脚本）测启动/停止/存活/`run_once`
2. **实例一联调**：`alas-manager` 拉起 ALAS Web UI，iframe 显示，启停可控
3. **实例二联调**：`battle-report-manager` 直连 `battle_reports.db`，列表/详情/图片正常
4. **回归**：外部程序旧入口（ALAS `gui.py`、识别系统 `pipeline.py`/`web_ui.py`）不受影响

## 10. 版本记录

| 版本 | 日期 | 说明 |
|------|------|------|
| v0.2 | 草稿 | 一次性 CLI 桥接方案（已弃用） |
| v1.0 | 重写 | 对齐 adapter-spec v1.0：多形态接入；实例一 ALAS=iframe 嵌入，实例二 战报识别系统=DB 直连；去除 LSP 会话协议 |
