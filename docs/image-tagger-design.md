# 动漫图像自动打标插件（image-tagger）设计文档

> 版本：**v1.0（设计定稿）**
> 日期：2026-08-21
> 状态：**设计阶段，尚未实装**
> 目标形态：**Companion 插件 + 自带 venv / 独立运行环境**

---

## 1. 定位与目标

`image-tagger` 是 `image-viewer` 的**伴侣插件（Companion Plugin）**，为本地动漫插图库提供离线自动打标与标签检索能力。

### 1.1 核心目标

| 目标 | 说明 |
|------|------|
| 不污染宿主 | `image-viewer` 代码零改动或只增加泛化扩展点，绝不引入 torch/onnx 依赖 |
| 自带环境 | 插件目录内携带独立 venv（torch / onnxruntime / 模型），与主程序 venv 完全隔离 |
| 支持换模型 | 本地 `.onnx` 模型可配置、可切换，兼容 WD 系列与 JoyTag |
| 超大库可用 | 面向 20 万+ 张图设计：断点续扫、进度可查、低优先级 IO、可取消 |
| 文件即契约 | 结果写 sidecar / 索引 JSON，不移动、不改名用户图片 |

### 1.2 非目标（第一版明确不做）

- 不做在线 API 打标（完全离线）
- 不做自动移动 / 重命名文件（只提供建议与虚拟相册）
- 不做人脸识别 / 人物身份聚类（WD character 标签可覆盖主要场景）
- 不在主进程内 import torch

---

## 2. 架构形态

```
plugins/
├── image-viewer/                 # 宿主插件（保持纯净）
│   └── 仅增加一个泛化「扩展」渲染点（可选，见 §5）
└── image-tagger/                 # Companion 插件
    ├── manifest.json
    ├── backend/
    │   ├── main.py               # 瘦控制器：任务管理 + 进度 + 结果索引
    │   ├── protocol.py           # stdio JSON-lines 协议封装
    │   └── runtime/
    │       ├── venv/             # 独立 Python 环境（不提交二进制，部署时创建）
    │       ├── requirements.txt  # onnxruntime / numpy / pillow / joytag(可选)
    │       ├── models/           # 用户放入 .onnx 模型（manifest 忽略 / 部署时下载）
    │       └── worker.py         # 长驻 Worker：加载模型、接收任务、输出标签
    └── frontend/                 # 打标设置 / 进度 / 标签筛选界面
```

### 2.1 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 插件关系 | 顶层 Companion（`dependencies: ["image-viewer"]`） | 现有依赖排序已支持，隔离最彻底 |
| 重依赖承载 | 独立 venv + 子进程 Worker | torch 不能与主进程共享；模型需要常驻避免反复加载 |
| 通信协议 | **stdio JSON-lines**（每行一个 JSON 对象） | 不占端口、无 CORS、可跨平台、天然串行 |
| 进度上报 | Worker 通过 stdout 输出进度行；控制器写 `<cache>/tasks.json` | 前端轮询即可，不新增推送机制 |
| 结果存储 | `xxx.txt`（Danbooru 标签文本）+ `<cache>/tags_index.json` 摘要索引 | sidecar 通用；索引加速检索 |
| 数据访问 | 通过 `get_dependency('image-viewer').get_file_roots()` 读取图片 | 复用现有 /files、/thumbs 安全体系 |

---

## 3. manifest 设计

```json
{
  "name": "image-tagger",
  "version": "0.1.0",
  "displayName": "图像打标",
  "icon": "🏷️",
  "dependencies": ["image-viewer"],
  "permissions": ["filesystem:read", "filesystem:write", "runtime:subprocess"],
  "backend": { "entry": "backend/main.py", "class": "ImageTaggerPlugin" },
  "frontend": { "entry": "frontend/index.html", "route": "/image-tagger" },
  "kind": "local-adapter",
  "runtime": {
    "kind": "stdio-worker",
    "entry": "backend/runtime/worker.py",
    "venv": "backend/runtime/venv",
    "requirements": "backend/runtime/requirements.txt",
    "modelDir": "backend/runtime/models",
    "startup": "lazy",
    "maxMemoryGB": 8,
    "timeoutSeconds": 3600
  }
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `dependencies` | ✅ | 声明宿主；PluginManager 保证 image-viewer 先加载 |
| `kind` | ✅ | `local-adapter`：声明本插件管理独立运行环境 |
| `runtime.kind` | ✅ | 首版固定 `stdio-worker` |
| `runtime.venv` | ✅ | 相对插件目录的独立 venv |
| `runtime.requirements` | ✅ | 部署脚本据此创建 venv |
| `runtime.modelDir` | ❌ | 模型目录；插件设置里可覆盖 |
| `runtime.startup` | ✅ | `lazy`：应用启动不拉起 Worker，首次任务时拉起 |
| `runtime.maxMemoryGB` | ❌ | 部署/设置页提示用途 |

---

## 4. settings_schema（明确要求）

```python
settings_schema = [
    {"key": "model_path", "label": "打标模型 (.onnx)", "type": "text", "central": True,
     "placeholder": "backend/runtime/models/wd-swinv2-tagger-v2.onnx",
     "help": "支持 WD SwinV2 / EVA02 / ConvNext / ViT；JoyTag 需额外安装 joytag 运行时"},
    {"key": "threshold", "label": "标签置信度阈值", "type": "range",
     "default": 0.35, "min": 0.05, "max": 0.95, "step": 0.05},
    {"key": "general_threshold", "label": "general 标签阈值", "type": "range",
     "default": 0.35, "min": 0.05, "max": 0.95, "step": 0.05},
    {"key": "character_threshold", "label": "角色标签阈值", "type": "range",
     "default": 0.85, "min": 0.1, "max": 1.0, "step": 0.05,
     "help": "角色标签误标代价高，默认更严格"},
    {"key": "character_first", "label": "角色模型优先", "type": "checkbox",
     "default": False, "help": "WD character tagger 模型存在时优先输出角色标签"},
    {"key": "sidecar_enabled", "label": "写入 xxx.txt sidecar", "type": "checkbox", "default": True},
    {"key": "overwrite", "label": "覆盖已有 sidecar", "type": "checkbox", "default": False},
    {"key": "workers", "label": "并发进程数", "type": "number", "default": 1, "min": 1, "max": 4,
     "help": "机械盘建议 1，SSD 可 2-4"},
]
```

---

## 5. 与 image-viewer 的扩展接口（防污染关键）

首版采用 Shell 级扩展注册表，`image-viewer` 只加一个泛化渲染点：

```js
// image-viewer 前端
const extensions = await Bridge.call('system_get_plugin_extensions', 'image-viewer');
extensions.forEach(ext => addToolbarButton(ext.label, () => Bridge.callPlugin(ext.plugin, ext.method)));
```

```json
// image-tagger 后端注册
{
  "host": "image-viewer",
  "id": "tag-selected",
  "label": "🏷️ 打标",
  "method": "tag_album",
  "scope": "album"
}
```

- 宿主完全不 import tagger 代码，也不出现 tagger 字符串
- 未来其他扩展（查重增强、导出、修图）同样接入

---

## 6. 后端 API 契约（image-tagger__*）

| API | 参数 | 返回 | 说明 |
|------|------|------|------|
| `status` | - | `{worker, task, progress, logTail}` | 整体状态轮询 |
| `start_tag` | `{paths[], scope: album\|selection\|library}` | `{taskId}` | 发起打标任务 |
| `task_status` | `taskId` | `{state, done, total, current, eta}` | 进度查询 |
| `task_cancel` | `taskId` | `{ok}` | 取消（Worker 收到控制行） |
| `list_models` | - | `[{name, size, kind}]` | 扫描 modelDir |
| `tags_query` | `{tags[], mode: all\|any, page}` | `{images[], total}` | 标签检索（供虚拟相册） |
| `tag_stats` | `scope` | `{total, tagged, missing}` | 打标覆盖率 |
| `remove_tags` | `paths[]` | `{ok}` | 删除 sidecar（不删图片） |

所有长任务遵循：
- 状态：`queued → running → paused → done | failed | cancelled`
- 断点：`<cache>/tasks/<taskId>.json` 每处理一张写入一次；重启自动恢复 `running → paused`
- 错误：任务级错误继续下一张，任务失败收集前 20 条日志

---

## 7. Worker 协议（stdio JSON-lines）

### 7.1 控制行（controller → worker）

```json
{"type": "config", "model": "path", "threshold": 0.35, "character": false}
{"type": "task", "taskId": "t1", "files": ["rel1.png", "rel2.png"], "sidecar": true, "overwrite": false}
{"type": "cancel", "taskId": "t1"}
{"type": "shutdown"}
```

### 7.2 数据行（worker → controller）

```json
{"type": "ready", "model": "wd-swinv2", "device": "cpu"}
{"type": "progress", "taskId": "t1", "done": 12, "total": 100, "current": "rel.png", "eta": 321}
{"type": "result", "taskId": "t1", "file": "rel.png", "tags": {"character": [...], "general": [...], "copyright": [...]}}
{"type": "task_done", "taskId": "t1", "ok": true, "elapsed": 123.4}
{"type": "task_done", "taskId": "t1", "ok": false, "error": "..."}
{"type": "log", "level": "info|warn|error", "message": "..."}
```

- stderr 仅作崩溃诊断日志，不参与协议
- Worker 崩溃 → 控制器收到 EOF → 状态 `crashed`，重启后按任务断点恢复
- 模型只加载一次；`config` 变化 → Worker 热切换或重启（实现选重启，简单可预期）

---

## 8. 性能与磁盘策略（针对 21 万张图 / 机械盘）

| 策略 | 要求 |
|------|------|
| 顺序扫描 | 按目录树顺序遍历，减少磁头抖动 |
| 增量 | 已存在且 mtime 未变的 sidecar 默认跳过 |
| 断点 | 任务 JSON 每张一写；崩溃/重启只补缺 |
| 缩略图 | 不复用主进程缩略图逻辑，Worker 自行读原图 |
| 后台优先 | Worker 进程设为低 IO 优先级（可选，Windows 需特殊处理） |
| 内存 | 模型常驻内存上限由 `runtime.maxMemoryGB` 提示；图片按张打开/关闭 |
| 写放大 | sidecar 小文件按目录批量 flush，避免每张 fsync |

首版性能目标（CPU、机械盘、SwinV2）：

| 规模 | 目标 |
|------|------|
| 100 张 | < 2 分钟 |
| 1,000 张 | < 15 分钟 |
| 10,000 张 | 可隔夜跑完，支持取消/续跑 |
| 210,000 张 | 分目录/分批次任务，全库可数天增量完成 |

---

## 9. 里程碑

| 阶段 | 内容 | 验收 |
|------|------|------|
| M0 文档 | 本设计文档 + core-direction.md | 本次交付 |
| M1 控制器 | Companion 骨架：dependencies、get_dependency、任务队列 | 不 import torch 即加载成功 |
| M2 Worker | 自带 venv + WD SwinV2 模型跑通 1 张图 | sidecar 内容正确 |
| M3 前端 | 进度/取消/模型选择/标签检索 | 千张目录打标体验可用 |
| M4 宿主集成 | image-viewer 扩展按钮 + 标签虚拟相册 | 宿主无 tagger 代码 |

---

## 10. 开放问题

1. JoyTag 是否第一版内置？倾向：WD 内置，JoyTag 由 `list_models` 检测后实验性支持。
2. sidecar 与统一 JSON 索引二选一还是双写？倾向：sidecar 为真相源，JSON 仅作检索缓存。
3. 角色标签如何处理「多角色同框」？首版取 top-k（k=3）并给阈值。
4. 模型分发：用户自备模型 vs 部署脚本自动下载（注意许可证与体积）。
