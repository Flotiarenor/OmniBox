# 团体组网（group-mesh）实现路径与现状

> 日期：2026-09-17
> 事实基线：`feat/group-mesh` @ `7c2110e` + 本轮工作区改动
> （私钥保护与 Noise 替换已落地，见 §4.1、§4.2）
> 定位：**进度、漏洞、易错项、测试工具**。设计文档（§1–§17）说明"现在是什么样"，
> 本文说明"做到哪了、哪里危险、怎么验证、下一步做什么"。
> 关联：[设计文档](./group-mesh-design.md)、
> [Companion 子插件（房间/语音/游戏面）](./group-mesh-companions.md)、
> [插件开发指南](./plugin-guide.md)、[主程序方向](./core-direction.md)。
>
> **章节号约定**：`shell/groupmesh/**.py` 的注释直接引用本文 §3、§5.1–§5.11、§7。
> 整理时保持这些编号与语义不变。

---

## 1. 这份文档要解决什么

设计文档定义"要做成什么样"。本文补三件事：

1. **做到哪了**：按 P0–P4 列出已完成、部分完成、明确不做、未开始；
2. **哪里有危险**：实现与设计之间的偏离、安全漏洞、稳健性缺口，逐条给等级与收敛路径；
3. **怎么验证**：每条命令覆盖什么、需要什么前置条件、哪些是门禁哪些是演示。

一句话概括：**协议内核（身份/名单/共享/注册/Noise/传输/节点）已可运行并在
Windows↔Linux 跨机验证；插件层已接通双向传输、物化与网络位置；主体上下文
壳侧已落地。本轮已收敛"私钥明文落盘"、"Noise 自写且无官方向量"、"手动刷新设备
（改为定时轮询 + 变更时 push）"三项。按当前决定，下一步暂不做 HTTP / 插件层的
按主体限权（§4.8、§4.9），优先处理 Rekey、连接上限、解散/设备授权/稳定地址等
剩余项。**

---

## 2. 分层与边界

```
┌──────────────────────────────────────────────────────────────┐
│ shell/backend/        壳：Flask 路由、令牌/主体、设置、文件服务 │
│   principal.py        凭据 → 主体；ContextVar 受信注入           │
└───────────────────────────┬──────────────────────────────────┘
                            │ PluginBase / register_api
┌───────────────────────────▼──────────────────────────────────┐
│ plugins/group-mesh/   插件层：API、数据目录、节点线程、前端      │
└───────────────────────────┬──────────────────────────────────┘
                            │ import shell.groupmesh（普通包导入）
┌───────────────────────────▼──────────────────────────────────┐
│ shell/groupmesh/      协议内核：不 import Flask / pywebview     │
│   crypto_prims records identity roster shares registry         │
│   noise transport node client cli selftest roster_history      │
└──────────────────────────────────────────────────────────────┘
```

内核归属壳层、不归插件私有，理由见设计文档 §0.3：它要被多个 Companion 复用，
且必须能脱离 GUI 独立跑。插件层只做三件事：解析数据目录、把内核能力转成 API、
管理监听/上传线程。打包由 `docs/Releases/spec_common.py` 的 `HIDDEN_IMPORTS`
保证内核随包分发，缺失会在 `check_packaging` 门禁暴露。

---

## 3. 阶段划分与当前进度

每个阶段以**可复核的命令**收尾，不用"看起来能跑"作为完成标准。

### P0 —— 协议内核与跨机验证（已完成）

| # | 内容 | 位置 |
| --- | --- | --- |
| 1 | 密码学原语封装（X25519 / Ed25519 / ChaCha20-Poly1305 / BLAKE2s / HKDF） | `crypto_prims.py` |
| 2 | 规范化序列化与签名（跨平台字节一致） | `records.py` |
| 3 | 主体/设备身份、设备凭据、落盘与权限 | `identity.py` |
| 4 | 团体名单：规则 1–6、并发收敛、宽限提示 | `roster.py` |
| 5 | 共享项声明与 ACL 判定 | `shares.py` |
| 6 | 注册记录与注册表（seq 单调、防重放） | `registry.py` |
| 7 | Noise_XX 握手与传输态 | `noise.py` |
| 8 | 协商、授权、帧协议、请求/应答 | `transport.py` |
| 9 | 节点服务端（路径安全、分块读写、暂存提交） | `node.py` |
| 10 | 命令行入口与自检 | `cli.py`、`selftest.py` |
| 11 | 插件骨架（API + 状态页） | `plugins/group-mesh/` |

已实测：密码学 fixture 跨平台一致；纯 IPv6 端到端取 700 KB 文件两侧 sha256 一致；
越权上传与路径越界被拒；名单 v1→v2 通过且运行中的节点无需重启即认新成员。

### P1 —— 壳侧主体上下文与权限档位（大部分完成）

| # | 项 | 状态 |
| --- | --- | --- |
| 1 | 凭据到主体的映射（`principals.json`，只存 SHA-256） | **已完成** |
| 2 | `ContextVar` 注入 + `PluginBase.current_principal()` / `require_principal()` | **已完成**（含插件层验收） |
| 3 | `/file`、`/thumbs` 主体级检查点 | **待做**（见 §4.8） |
| 4 | 权限档位落地（只保留 `read` / `write` + 上传） | **已完成** |
| 5 | 设置写入限权（`system_*` 限 owner/admin） | **已完成** |
| 6 | `minShellVersion` 运行时校验 | **决定不做**（当前只支持与最新壳配套；门禁仍校验格式，见 §4.12） |

向后兼容：凭据表为空时，壳把既有 `auth_token.txt` 自举成 owner，老部署升级后
令牌继续可用。验收用例见 `tests/test_shell_principal.py`，其中
`PluginPrincipalInjectionTest` 走真实 `/api/<插件>__<方法>` 通路，断言请求体里的
`principal` / `principal_id` / `role` 字段影响不了 `current_principal()`。

### P2 —— 把偏离收敛掉（上生产前必须）

| 优先级 | 项 | 状态 |
| --- | --- | --- |
| 高 | 私钥改 DPAPI / keyring / 口令 AES-GCM（设计 §4.1） | **已完成**（`secret_store.py`；三者都不可用时如实回落明文并标红，见 §4.1） |
| 高 | Noise 换 vetted 实现 + 官方握手向量 | **已完成**（`noiseprotocol` + cacophony 向量，见 §4.2） |
| 中 | 长连接 Rekey（设计 §4.4） | **待做**（§4.3；库已提供 CipherState.rekey，缺应用层协商） |
| 中 | 设备授权（配对/二维码）与凭据进名单（设计 §4.2） | **待做** |
| 中 | 解散通告（设计 §5.5） | **待做** |
| 低 | 注册表 `since` 游标 | **待做**（当前整表快照 + seq 合并） |
| 低 | 稳定地址判定（设计 §7.4） | **待做**（§4.6.1） |
| 低 | 群主转移/任免管理员进界面 + 使用说明 | **待做** |
| — | ~~名单自动分发~~ | **已完成**（`op=roster` + 名单历史） |
| — | ~~主动 push 新名单~~ | **已完成**：变更时 push + 后台定时轮询（间隔见设置 `sync_interval_seconds`） |
| — | ~~容量上限进设置面板~~ | **不做**：判定挡不住并发，做成设置项只会让人以为有防线（§4.11） |

### P2.5 —— 共享面收尾

| 项 | 状态 |
| --- | --- |
| 上传入口 `upload_remote` + 界面 | **已完成** |
| 上传进度 / 取消 / 断点续传 | **已完成** |
| 上传任务表落盘、重载标记 interrupted | **已完成** |
| 遗留 `<目标>.part` 的可见性 | **待做**：取消/中断留下的暂存会一直占属主磁盘，协议里没有远程删除 |
| `add_device()` 暴露（多设备） | **待做**：内核已实现，CLI/插件都没有调用方 |

### P3 —— 用途插件（房间 / 语音 / 游戏面）

不在本插件路线里，由 Companion 子插件承担；前置实测项见
[Companion 子插件](./group-mesh-companions.md) §4。

### P4 —— Android 轻客户端

按设计 §11.2 只预留角色与能力字段，不预留接口；当前未开始。

### 本轮基线验证（可复核，2026-09-17 实跑）

```bash
# 内核自检：6/6 通过
venv/Scripts/python.exe -m shell.groupmesh.cli selftest

# 内核 + 插件 + 物化 + 网络位置 + 多实例夹具 + 壳主体 + Noise 向量 + 私钥保护：
# 268 例通过，1 例跳过
venv/Scripts/python.exe -m unittest -q tests.test_group_mesh_mvp tests.test_group_mesh_plugin \
    tests.test_group_mesh_materialize tests.test_group_mesh_network_location \
    tests.test_multi_instance_fixture tests.test_shell_principal \
    tests.test_group_mesh_noise_vectors tests.test_group_mesh_secrets

# 前端两层（需要本机有可用的无头浏览器）：39 例通过
venv/Scripts/python.exe -m unittest -q tests.test_group_mesh_frontend_e2e tests.test_group_mesh_shell_e2e

# 全量门禁：617 例通过，1 例跳过；lint / 类型 / 插件 / 打包检查全绿
venv/Scripts/python.exe -m unittest discover -s tests -q
venv/Scripts/python.exe -m ruff check .
venv/Scripts/python.exe -m pyright main.py shell tools
venv/Scripts/python.exe tools/check_version.py
venv/Scripts/python.exe tools/check_plugins.py
venv/Scripts/python.exe tools/check_packaging.py
```

跨机联调（主路径纯 IPv6 / 兜底同网段 IPv4）需要 `~/.ssh/config` 的
`omnibox-linux` 别名：

```bash
pwsh -File shell/groupmesh/tools/ipv6-test.ps1
pwsh -File shell/groupmesh/tools/lan-test.ps1
```

---

## 4. 已知偏离、漏洞与风险清单

**这一节是本文最重要的部分。** 先给等级汇总，再逐条展开。等级按"当前部署形态
（单机单使用者）下的实际影响"与"多使用者/公网部署下的影响"综合判断。

| # | 项 | 等级 | 一句话 | 收敛路径 |
| --- | --- | --- | --- | --- |
| 4.1 | 私钥保护 | **已收敛**（plain 回落为残余） | DPAPI / keyring / 口令 AES-GCM；旧明文单向升级 | headless 可设 `OMNIBOX_SECRET_KEY` |
| 4.2 | Noise 实现 | **已收敛**（无 Rekey、帧长超规范为残余） | `noiseprotocol` + cacophony 官方向量逐字节校验 | Rekey 见 §4.3 |
| 4.8 | `/file`、`/thumbs` 无主体级检查点 | **中高**（多使用者下高） | 物化缓存对任何持令牌者全开 | `authorize_file()` 钩子 |
| 4.9 | 插件有副作用的方法未按主体限权 | **中高** | 任持令牌者可加成员、上传任意本机文件（含 `.config` 凭据）、向任意目录写远端内容 | 插件调 `require_principal()` + 收窄上传来源 |
| 4.3 | 长连接无 Rekey | 中 | 单条连接的传输密钥泄露即可解全连接内容 | Noise §11.3 Rekey / 限时重握手 |
| 4.10 | 节点自动监听 + 每连接一线程，无连接上限 | 中 | 暴露面大、可被半开连接占用线程 | 连接上限 / 超时收紧 / 显式开关 |
| 4.6.1 | 临时地址也会进注册记录 | 中低 | 地址轮换后端点短暂失效 | `GetAdaptersAddresses(Temporary)` |
| 4.11 | 容量上限挡不住并发 | 低（已默认关闭） | 显式设置也超限 | 保持"默认不限制 + 不暴露设置" |
| 4.12 | `minShellVersion` 运行时不校验 | 低 | 门禁通过、运行时静默降级 | 已决定不做；靠版本纪律 |
| 4.13 | 显式下载 / 镜像无总量上限 | 低 | 用户主动操作可写满磁盘 | 交互确认 / 总量预估 |
| 4.7 | 解散、注册表游标、稳定地址等 | 功能缺口 | 见各条 | 按 P2 排期 |

### 4.1 私钥保护（已收敛，仍有 plain 回落）

- 设计 §4.1：设备私钥不导出，由操作系统密钥存储保存。
- 现状（2026-09-17）：`shell/groupmesh/secret_store.py` 把 `principal.json` 与
  `devices/<id>.json` 的**整个 JSON 内容**先保护再落盘，按可用性自动选择：
  * Windows：**DPAPI**（`CryptProtectData` / `CryptUnprotectData`，ctypes 调用，
    绑定当前用户）；
  * 桌面 Linux / macOS：**OS keyring**（随机 32 字节密钥存 keyring，文件用
    AES-256-GCM）；
  * 设置 `OMNIBOX_SECRET_KEY`：**scrypt 口令派生的 AES-256-GCM**；
  * 都不可用：明文回落，`describe()` / 插件状态页明确标红。
- 迁移：旧版明文文件在首次读取时**单向升级**；已受保护的文件不会被降级重写；
  keyring / 口令失效时抛 `SecretError` 并转成 `RecordError`，绝不把密文当明文解析。
- 残余风险：`plain` 回落仍是明文（headless Linux 没有 keyring 且未设口令）；
  keyring 后端依赖桌面会话（锁屏 / 未解锁时可能取不到）；`identity/` 的
  "受保护路径"仍依赖壳的路径校验。
- 验收：`tests/test_group_mesh_secrets.py` 覆盖 DPAPI（Windows）、keyring 替身、
  口令、篡改拒绝、旧明文升级与"不降级重写"。

### 4.2 Noise 实现（已收敛为 vetted 实现 + 官方向量）

- 设计 §4.4：握手使用 Noise 框架的现成实现，不自行设计密码学组合。
- 现状（2026-09-17）：`noise.py` 包装 **`noiseprotocol==0.3.1`** 的
  `Noise_XX_25519_ChaChaPoly_BLAKE2s`；传输态直接使用它的 CipherState
  （同一套密钥、nonce 推进与 AEAD）。`noise.py` 只做角色 / 静态密钥 / prologue
  翻译、异常统一与接口兼容。
- 官方向量：`tests/test_group_mesh_noise_vectors.py` 驱动
  `tests/fixtures/noise_XX_25519_ChaChaPoly_BLAKE2s.json`（cacophony 向量），
  逐字节校验 3 条握手消息、`handshake_hash` 与双向传输消息，并覆盖"与原生
  `noiseprotocol` 对端互通"。**替换前的手写状态机也通过同一组向量**（本轮实测
  验证），因此这次替换不改变线格式，`PROTO_VERSION` 保持 2。
- 残余风险：
  * `noiseprotocol` 是 Alpha、未做独立安全审计；它相对手写实现的价值是
    "独立实现 + 官方向量可复核"，不是"已被审计"；
  * 传输帧直接走 CipherState 而非 `NoiseConnection.encrypt/decrypt`，
    因此应用帧可以超过 Noise 规范的 65535 字节单消息上限（256 KiB 分块 /
    目录列表都会超过）；与严格检查该上限的实现对传大帧时会被它们拒绝；
  * 仍无 Rekey（§4.3）。
- 验收：上述向量用例 + `selftest` 的"Noise_XX 握手与传输态" + 既有 `NoiseXXTest`。

### 4.3 长连接没有密钥轮换（中）

- 现状：握手有前向安全（临时密钥），但长连接的传输密钥在整条连接生命周期内不变，
  也没有消息数上限（除 `2^64` nonce 上限；耗尽时 `noise.py` 直接报错终止，不是 Rekey）。
- 影响：单条超长连接泄露一把传输密钥，该连接内全部内容可解。
- 收敛：按消息数/字节数触发 Noise §11.3 的 `Rekey()`，或限制单连接时长后强制重握手。

### 4.4 写入语义：暂存 + 提交（已按设计实现）

这一条不是风险而是**已收敛的偏离**，留档说明曾经的危险写法：

- 曾经：`_op_write` 直接 `open(target, 'wb')` 覆写，一次断线就把对端已有文件截断成
  半份且无人报错。
- 现在：`part` / `eof` / `overwrite` 三字段；`client.push_file()` 一个文件走一条连接、
  一次基线计量；目标已存在默认拒绝，覆盖需显式 `overwrite`。
- 顺带修掉：配额每分块 `os.walk`（1 GiB / 256 KiB = 4096 次全树遍历，上传慢到不可用）；
  基线把正在写的 `.part` 重复计入。

### 4.5 身份签名与 Noise 静态密钥是两把密钥（必要的措辞修订）

- 设计 §4.3：同一把设备密钥同时用于握手与注册。
- 现状：设备持有 Ed25519 签名密钥 + 由同一份种子确定性派生的 X25519 握手密钥，
  握手时用**设备绑定证明**把两者绑在一起。从"账号数量"看仍是一套身份材料，
  但不满足"同一把密钥"的字面表述。
- 原因：Ed25519 私钥是种子经 SHA-512 派生的标量，不是合法 X25519 标量；
  直接当 DH 用会让双方算出不同共享密钥且都不报错（§5.2）。

### 4.6 IPv4 是可达性兜底，主路径仍是 IPv6（已实测）

- 实测条件：两端都有全局 IPv6。`ipv6-test.ps1` 完成纯 IPv6 联调：Linux 绑全局
  IPv6，Windows 用 `[2409:…]:port` 连接，700 KB 文件 sha256 两侧一致，越权与越界
  被拒，注册记录里写入的是该全局 IPv6 端点。
- IPv4 只是同一可达网段内的地址族兜底，不引入 NAT 穿透或中继，与设计 §1.3 的非目标
  不冲突。
- 仍然成立的边界：**跨公网**能否直连取决于运营商是否放行入站高位端口（§8 问题 1）。
  已验证的是"同网段、IPv6 地址族"，不等于"公网可达"。

### 4.6.1 自动地址选择会把临时地址也当成端点（待修）

设计 §7.4 要求监听与注册使用稳定地址，不使用 Windows 默认的 RFC 4941 临时地址。
`registry.local_addresses()` 做不到：Python 标准库拿不到"是否为临时地址"。
实测 Windows 同时存在稳定地址与临时地址，因此不显式 `--bind` 时两者都会写进
注册记录；临时地址轮换后该端点失效。`_refresh_own_registration()` 会在地址集合
变化时递增 `seq` 重发，代价是旧端点期间白试一次。

补充实测：本机由 RA 下发两个全局 IPv6，另有私有 v4 与一张 Radmin VPN 网卡
（地址对同为该 VPN 成员的设备可用）。因此**不能按网段筛端点**，可达性只能靠
连接侧逐个尝试。收敛路径：显式 `--bind`（当前规避手段）；Windows 用
`GetAdaptersAddresses` 的 `Temporary` 标志识别；或改用 RFC 7217 地址并只发布它。

### 4.7 未实现的次要项

| 项 | 设计 | 现状 |
| --- | --- | --- |
| 解散通告 | §5.5 | 未实现 |
| 内容寻址分块传输 / 做种 | §10 | 未实现（当前 offset/length 分块） |
| 注册表增量同步 | §7.3 | 只有整表 `list`/`push`，无 `since` |
| 稳定地址判定 | §7.4 | 见 §4.6.1 |
| 主动 push 新名单 | §5.7 | **已实现**：名单变更时 push + 后台定时轮询（`sync_interval_seconds`，默认 60 秒） |
| 群主转移/任免管理员进界面 | §5.6 | 只有 CLI |
| 设备配对/二维码、凭据进名单 | §4.2 | 未实现 |
| Android 轻客户端 / 本地网关 | §11.2 / §11.3 | 未实现 |

### 4.8 数据路由没有主体级检查点（中高；多使用者部署下为高）

- 现状：`/file`、`/thumbs` 只做令牌 + 路径安全 + 受保护判定
  （`shell/backend/file_server.py` 的 `serve_media_file` / `serve_thumb`），
  没有按主体判权。
- 为什么现在必须做：group-mesh 的**物化缓存**（`.cache/remote/<设备>/<共享>/…`）
  会经 `/file` 端给界面。远端共享内容进入本机 HTTP 之后，任何持令牌者都能按路径
  读走，而"谁能看哪个共享项"的 ACL 判定在协议侧，HTTP 侧完全不知道。
- 影响范围：单机单使用者部署下，"持令牌者"就是本机用户，影响有限；一旦通过
  反向代理/局域网多使用者共用同一个壳进程，就是跨主体读。
- 收敛：新增 `PluginBase.authorize_file(principal, path) -> bool` 钩子（默认放行，
  只收紧不放松）；group-mesh 覆写为"物化缓存只有属主与共享项 ACL 允许的主体可读"；
  壳在路径安全之后、返回文件之前调用。

### 4.9 插件有副作用的方法未按主体限权（中高）

- 现状：壳侧只有 `_ADMIN_ONLY_API` 守**壳自己的**端点；插件方法
  （`/api/group-mesh__<方法>`）仍只受"有效令牌"保护。group-mesh 也没有调用
  `require_principal()`，因此主体上下文对它等于不存在。
- 具体危险（任一持令牌者即可触发，动作以**本机主体**身份执行）：
  - `add_member`：把任意主体/设备公钥写进名单（当本机主体是 owner/admin 时）；
  - `upload_remote`：把本机任意文件读取并发送到对端共享项，只排除 `identity/`、
    远端缓存与 staging——**没有排除 `.config/`（`auth_token.txt`、`principals.json`、
    插件设置）**。持令牌者因此可以把壳自身的凭据外带出去，这是本文分析中除
    "私钥明文"外最直接的提权链。收敛时除了限权，还应把上传来源收窄到用户显式
    选择的目录（或在 `PluginBase` 层提供"受保护路径不可读"的统一排除）；
  - `mirror_share`：把远端共享项内容写到**任意**本地目录（仅排除插件数据根与身份
    目录），等于任人投放文件；
  - `start_node` / `stop_node` / `add_share` / `remove_share` / `clear_remote_cache`：
    改变节点的对外行为或删除缓存。
- 收敛：group-mesh 在涉及主体/副作用的方法上调用 `self.require_principal()`，
  并按下述规则判断：改设置/节点开关/成员管理要求 `is_admin`；读取只要求主体存在。
  拒绝返回 403（插件侧返回 `{'success': False, 'error': ...}` 或抛
  `PermissionError`，由壳统一转错误响应）。**注意后台线程天然没有主体**，
  上传线程等后台任务需要显式携带发起主体（`use_principal`），否则无法判权。

### 4.10 节点自动监听 + 每连接一线程，无连接上限（中）

- 现状：身份与团体都存在时，插件在 `get_status()` 里自动启动节点，默认绑 `::`；
  服务端每条连接一个 daemon 线程（`_spawn_handler`），`listen(16)`，没有并发上限；
  半开连接可占用线程最多 30 s（`CONNECTION_TIMEOUT_SECONDS`）。
- 影响：暴露面大（公网 IPv6 上任何能到达该端口的主机都可发起 Noise 握手）；
  大量半开连接可耗尽线程/文件描述符。防火墙默认会挡，但用户加过放行规则后就敞开。
- 收敛方向（未定）：引入可配置的连接数上限与握手超时收紧；提供"不自动启动节点"
  的显式开关；或要求用户显式确认监听地址。当前文档只如实标注，不假装已有防线。

### 4.11 共享项容量上限挡不住并发（已处置为默认不限制）

- 旧状：默认 1 GiB，前端还硬编码发送 `max_bytes: 1 GiB`，而设置面板没有该项。
- 实测：`node.py` 的判定是"**每条连接**量一次基线，之后按基线推算"。8 条并发连接在
  "上限 1000 字节"的共享项上各写 400 字节，全部成功，落盘 3200 字节。另有事后估算、
  别的进程写入、先探询再分块三处天然缺口。
- 处置：`DEFAULT_MAX_BYTES = None`；插件 `add_share()` 不再带 `max_bytes`
  （不传 / 0 / null = 不限制），界面不再发送硬编码值；判定逻辑保留，显式设置上限时
  仍生效。**不做界面设置项**：给不出可信保证的旋钮比没有旋钮更糟。
- 代价：被授予 `write` 的人可以写满属主磁盘。因此"不要给不信任的人 write 权限"是
  唯一有效边界（写权限是可加的，设计 §6.2）。

### 4.12 `minShellVersion` 运行时不校验（已决定不做）

- 现状：`tools/check_plugins.py` 校验格式，`manifest.json` 声明 `1.2.0`，壳加载时
  不比较版本。它是唯一"门禁通过、运行时静默降级"的项。
- 决定：当前只支持与最新壳配套发布，不引入运行时拒绝逻辑；版本纪律保留在
  `PROTO_VERSION`（协议主版本）上。若将来插件需要向后兼容旧壳，再引入运行时校验。

### 4.13 其它缺口与观察

- **显式下载 / 镜像无总量上限**：`download_remote` 与 `mirror_share` 由用户主动触发，
  但可以写满磁盘；`max_fetch_mb` 只约束"浏览时的按需取字节"，不约束这两个入口。
- **`download_dir` 与物化缓存的 HTTP 可见性**：默认下载目录经
  `/file?plugin=group-mesh` 可读，受 §4.8 的同一问题影响。
- **无审计**：不记录"谁在何时写了什么"；设计上靠小团体相互约定，写权限与
  `created_by` 无关。
- **身份目录保护是路径级的**：`_reject_protected_file` 依赖路径字符串与
  `protected_paths` 的一致性；不是加密保护。
- **崩溃/断电后的 `.part` 与暂存**：上传取消会留下 `<目标>.part`；取字节失败会留下
  `.cache/staging` 下的 `.part`（`_discard_staging` 会清自己那一次，但进程被杀时
  可能残留）。
- **旧 pyc**：`tests/__pycache__/test_multi_instance_mesh.cpython-312.pyc` 存在，
  但对应的 `tests/test_multi_instance_mesh.py` 已不存在（现名
  `tests/test_multi_instance_fixture.py`）。`unittest discover` 不受影响，
  但会误导读仓库的人，建议清理。

### 4.14 本轮分析发现并已处理/待清理的文档不一致

| 位置 | 原不一致 | 处理 |
| --- | --- | --- |
| `plugins/group-mesh/backend/main.py` 文件头 | 仍写"壳目前没有主体能力"，并称"当前状态（v0.1，骨架）" | **已改**：说明壳侧已落地、本插件尚未接入，指向本文 §4.9 |
| `shell/groupmesh/__init__.py` 文件头 | 把"壳侧主体上下文"列为不在本包范围，容易被误读成"壳还没有" | **已改**：说明它由 `shell/backend/principal.py` 提供，内核不依赖它 |
| `shell/groupmesh/registry.py` 注释 | 引用"设计文档 §4.6.1：IPv4 是可达性兜底"，旧设计文档根本没有 §4.6 | **已改**：引用 §4.6；§4.6.1 留给临时/稳定地址识别 |
| `docs/group-mesh-p1-review.md` | 内容已被本文收编，且"待做/已完成"与最新 HEAD 不一致 | **已改**：改为指向本文的存档说明 |
| `tests/__pycache__/test_multi_instance_mesh.cpython-312.pyc` | 无对应 `.py`（现名 `test_multi_instance_fixture.py`） | **待清理**：不影响 `unittest discover`，但会误导读仓库的人 |
| `plugins/group-mesh/backend/main.py` 的 `get_status()['unsupported']` | 只列"内容寻址 / Android" | 与事实一致；如要更完整可补"本地网关 / 轻客户端" |

---

## 5. 实现中踩到的坑（务必保留，避免重犯）

这些是真实调试中定位到的问题。共同特征：现象常是"AEAD 校验失败 / MAC check failed"，
根因却各不相同。握手失败时不要只看"密钥对不对"，按下面清单逐项排查。

### 5.1 Noise_XX 的 token 顺序与 nonce 延续（三处错误）

按规范 §5.3 的 `WriteMessage` 规则：**先顺序处理消息的所有 token，最后才追加一次
负载**。由此推出的三条约束，本实现最初全部搞错：

| # | 规则 | 错误写法 | 正确写法 |
| --- | --- | --- | --- |
| 1 | `es` 的第一个字母指**发起方**的密钥类型 | 发起方用 `DH(e, re)` | 发起方 `DH(e, rs)`；响应方 `DH(s, re)`，且在收到消息 3 时才结算 |
| 2 | 负载用最后一个 token 派生的 `k` | 把负载放在 `es`/`se` 之前 | `es`/`se` 的 `MixKey` 之后才加密负载 |
| 3 | 同一个 `k` 的 nonce 不因新消息重置 | 消息 3 的 `s` 从 nonce 0 开始 | 响应方消息 2 已用掉 nonce 0，消息 3 的 `s` 用 nonce 1 |

补充：`CipherState` 在 `k` 为空时 `EncryptWithAd` 原样返回明文并只做 `MixHash`
（XX 的消息 1），因此 `MixHash` 必须由 `HandshakeState` 负责，不能让
`CipherState` 去持有握手哈希。

### 5.2 Ed25519 密钥不能直接当 X25519 用（最危险的一个）

- 现象：双方各自算出的 `DH(e_i, s_r)` 与 `DH(s_r, e_i)` 不相等，两边都不报错，
  直到后面的 AEAD 才以 `MAC check failed` 暴露。
- 根因：Ed25519 私钥是种子经 SHA-512 派生的标量，不是 X25519 标量；原样喂给
  X25519 点乘得到的是一把无关的标量。
- 修正：由同一份身份种子确定性派生 X25519 密钥对
  （`X25519 私钥 = clamp(BLAKE2s(seed || label))`），并在握手中用设备绑定证明
  把 Ed25519 设备公钥与 X25519 握手公钥绑定，否则 `authorize_peer` 会拿 DH 公钥
  去名单里查，永远查不到。

### 5.3 pycryptodome 的两处编码陷阱

| 陷阱 | 表现 | 正确做法 |
| --- | --- | --- |
| `pointQ.x`、`export_key(format='raw')` 与 RFC 7748 三者互不相同 | 用 raw 导出公钥、按 `pointQ.x` 大端当线格式，会与外部实现不互通（本项目 v0.1 的实际缺陷） | 线格式一律取 RFC 7748 §5 的 32 字节小端；`EccXPoint` 要的整数按小端解读 |
| X25519 私钥不能手工转成整数做标量乘 | 普通 int 点乘与库自己的结果不同，两边都不报错 | 私钥一律经 `_dh_private_key()`（内部 `ECC.construct(curve='Curve25519', seed=...)`）还原 |
| Ed25519 的 `int(key.d)` 是派生标量不是种子 | 存 `d` 再重建会得到另一把密钥 | 持久化用 `key.seed`；重建用 `ECC.construct(curve='Ed25519', seed=...)` |

另有两处 API 事实：`ECC.import_key` 不接受 32 字节裸 Ed25519 公钥（需补固定 SPKI
头），也不接受裸种子；`EccXPoint(None, curve)` 是无穷远点而不是基点。

### 5.4 授权必须在握手之后单独确认

XX 的三条消息只完成"互相认证静态公钥"。响应方判定对端资格所需的最后一个输入
（发起方静态公钥与绑定负载）在**消息 3** 才到达，而发起方在收到消息 2 后就已经把
消息 3 发出去了。不加确认帧时，被拒的发起方会认为 `connect()` **成功**（实测现象），
直到后续请求失败或超时才发现。修正：会话建立后的第一帧由响应方回 `ok` 或
`deny:<原因>`，发起方据此决定是否返回连接。

### 5.5 跨机联调的环境坑（与协议无关，但会浪费大量时间）

| 现象 | 根因 | 做法 |
| --- | --- | --- |
| `bash: $'\r': command not found` | PowerShell 管道喂 bash 时行尾是 CRLF | 喂之前 `-replace "\`r\`n","\`n"` |
| `ssh` 卡满 120 秒超时，服务其实已启动 | `nohup ... &` 仍继承 ssh 的 stdout 管道 | 用 `systemd-run --unit=...` 起临时 unit |
| `ssh $alias "a" + "b"` | ssh 是原生命令，参数被拆开，远端收到孤立的 `+` | 先把整条命令拼进一个变量 |
| 控制台中文乱码导致断言假命中 | 代码页不是 UTF-8 | 断言用**退出码**，不用输出文本 |
| IPv6 地址直接拼进 `host:port` | 地址自带冒号，`rpartition(':')` 会切错 | 写成 `[2409:…]:port`，解析时去掉方括号 |
| "没有 IPv6"的错误结论 | 引号嵌套失败，把报错当成了探测结果 | 探测类命令**必须看退出码**，别只看输出 |

最后一条值得单独强调：本次就因为一条引号写坏的 `ip -6 addr`，得出了"目标机没有
全局 IPv6"的错误结论，并据此写进了文档。**探测结果与预期不符时，先确认那条探测
命令真的跑成功了。**

### 5.6 长驻节点的名单必须在每次建连时重读

- 现象：先起 `serve`，再在另一处 `roster add`，新成员连接时被判
  "对端设备 … 不在团体名单里，拒绝连接"。
- 根因：`Node` 只在构造时拿到一份名单快照，而它是长驻进程；名单会变。
- 修正：`Node.current_roster()` 在每条连接上调用 `roster_loader` 重新读取；
  CLI 与插件分别传入"读 roster.json"与"读插件身份目录"的 loader。
  读盘失败退回内存副本，不让一次 IO 错误导致全部连接被拒。
- 附带结论：这让 `tools/ipv6-test.ps1` 的"先起节点、后加成员"顺序成为有效的
  回归用例。

### 5.7 CSS 级联废掉了 `hidden`（前端"一打开就弹添加成员"）

- 现象：打开插件，三个弹窗同时铺满整屏，DOM 里最后的"添加成员"盖在最上面。
- 根因：浏览器给 `[hidden]` 的 `display: none` 来自 UA 样式表，而
  `.gm-modal { display: flex }` 是作者样式，优先级相同时作者样式胜出。
- 修正：改成属性选择器驱动默认态：

  ```css
  .gm-modal { display: none; }                    /* 默认态没有任何规则命中 */
  .gm-modal[data-open="true"] { display: flex; }  /* 显式打开 */
  ```

- 教训：**前端"接上了"不等于"看得到"，也不等于"点得动"**。此前只做后端 API 测试
  （全过），前端一行没验；而后端全对、界面全错完全可能。

### 5.8 状态载荷缺键，导致按钮"点了没反应"

- 现象：「创建团体」点击后毫无反馈。
- 根因：`get_status` 的 `settings` 没给 `group_name`，前端读
  `status.settings.group_name`；缺失时 `undefined !== ''` 恒真，走进
  `window.confirm()`，而内嵌 WebView 里原生对话框可能被禁用。
- 修正：①「创建团体」改成应用内弹窗；②状态载荷补齐前端会读的键，且只给真正读的键。
- 教训：桩测试容易掩盖这类问题——用例对 `get_status` 回的是手写理想载荷，键当然是齐的。
  因此又加了 `tests/test_group_mesh_shell_e2e.py`：起真实壳服务、经导航进入插件
  iframe，用真实后端返回值验证渲染与调用。

### 5.9 前端验证的三个层次（缺一层就会漏）

| 层次 | 用例 | 能抓到什么 | 抓不到什么 |
| --- | --- | --- | --- |
| 纯渲染 | `test_group_mesh_frontend_e2e.py`（桩 Bridge + 无头浏览器） | 弹窗可见性、按钮是否开弹窗/调后端、布局 | 真实鉴权、`Bridge` 来源、`<插件>__<方法>` 前缀 |
| 真实壳 | `test_group_mesh_shell_e2e.py`（起服务 + 导航 + iframe） | 上面三项，以及"插件在壳里到底能不能用" | 需要本机有浏览器（CI 上跳过） |
| 后端 | `test_group_mesh_plugin.py` | API 语义、契约（受保护路径）、共享根约束 | 界面 |

易踩前提：打开首页时壳会自己种下 HttpOnly 令牌 Cookie
（`file_server._attach_token_cookie`），因此 e2e 不需要手动准备令牌；反过来，**直接**
打开 `/plugins/<name>/frontend/index.html` 既没有 Cookie 上下文，也没有壳注入的
`window.pywebview.api` 垫片，`Bridge` 会一直报"PyWebView API 不可用"。
排查"插件一片空白"时先确认走的是壳的 URL。

### 5.10 名单只做了"第一次加入"，没做"分发更新"（成员停在旧名单）

- 现象：群主在 A 机把自己与 B 都加进名单（v2），B 机界面里却始终只有 v1、看不到
  自己、也连不上任何人。
- 排查结论：**不是"进不了名单"，是名单没送到**。A 的 `prev` 正好等于 B 的名单哈希，
  B 只要拿到 v2 就能接上。
- 根因：名单是带外分发（规则 6 的信任锚），而实现里只有"首次加入"一个入口。
- 修正：①"加入"与"更新"合并为同一路径；②界面入口改名「加入 / 更新团体」；
  ③给旧邀请串时错误信息带版本号；④**（2026-09-17）自动分发 `op=roster`**：
  成员直接从对端拉名单，不再依赖手工贴串；未入名单者只放行 `roster`；准入按
  "本机认得这一份"（含 `roster-history.json`，保留 12 版）。
- 验证：`RosterDistributionTest`（内核 + 插件两侧）覆盖 v1→v2、旧邀请串被拒、
  跳版被规则 2 拒绝、陌生设备拿不到名单、有旧名单的待入成员能拿到新名单。
- 遗留：**已实现"变更时 push + 定时轮询"**（间隔见插件设置 `sync_interval_seconds`，
  默认 60 秒）；界面仍未展示双方版本对照。

### 5.11 上传的进度 / 取消 / 续传（五个坑）

| 坑 | 现象 | 处置 |
| --- | --- | --- |
| 复用连接被两个线程同时用 | 上传线程与界面线程共用复用池连接，两次 send 交错把帧拼坏（`MAC check failed` 或卡住） | 上传用**专用连接**（`open_connection`，用完即关），复用池只服务界面 |
| 同步请求挡住进度 | `/api` 是同步请求-应答，一次上传几分钟，占着请求既报不了进度也取消不了 | 后台线程 + `task_id`，界面轮询 `upload_status` |
| 重载让上传"凭空消失" | 任务表只在内存，改设置/升级/重启后界面什么都不剩，而对方 `.part` 还在 | 任务表落盘 `upload-tasks.json`，启动时把未结束任务标为 interrupted |
| 取消没有分块边界 | 在 `request()` 阻塞时才判取消，用户要等一整个分块甚至超时 | `push_file` 在每个分块前检查取消回调；socket 超时 30 秒兜底 |
| 续传起点不可信 | 只按"对端 `.part` 有多大"续传，会在内容已变的本地文件上拼出垃圾且无人报错 | 起点必须同时满足：本机有记录、记录与当前文件 (大小, mtime) 一致、对端暂存数等于记录值；每块带 `offset`，对端核不上回 `offset_mismatch` |

两条附带约束：进度回写要节流（每 4 MiB 落一次盘）；空文件与"整个文件已在对方
暂存里"是同一类，都要补一次提交（`push_file` 里用 `pushed` 标志判断）。

### 5.12 陈旧的 `dh_public` 让老身份目录升级即失败（已修）

- 现象：X25519 编码改成 RFC 7748 后，`Device.from_dict()` 把"落盘 `dh_public`
  与派生值不符"判为文件被改动并抛 `RecordError`，磁盘上所有旧身份都读不出来。
  实测表现：插件加载正常、节点永远起不来、界面停在"正在读取设备"。
- 更麻烦的一点：仓库 `data/group-mesh` 里那份 `dh_public` 既不是现行编码，也不是
  大端遗留（实测与 `pointQ.x` 大端互不相同），"加一条大端兼容分支"不够。
- 修正：该字段只是派生结果的冗余副本、不参与任何密码学计算，**派生值才是权威**；
  不符时记 warning 并采用派生值；只有长度不是 32 字节才按结构性错误拒绝。
  完整性由 Ed25519 私钥↔公钥一致性检查负责。

### 5.13 改了不兼容的线格式却没递增 `PROTO_VERSION`（已修）

- 现象：DH 编码改为 RFC 7748 时忘了递增协议主版本，新旧节点都自报"版本 1"。
  协商层本来会给出"协议主版本不一致（本地 X，对端 Y），请升级后再连"，
  版本号没变就轮不到它，用户看到的只是一句 MAC 校验失败。
- 修正：`PROTO_VERSION = 2`；新增"本机 hello 必须报出 `PROTO_VERSION` 本身"的用例。

### 5.14 测试复制被测常量，版本号漂移仍全绿（已修）

- 现象：`NegotiationTest` 把 `proto: 1` 写死在三处，因此版本号与实现可以在测试
  全绿的情况下漂移（§5.13 的温床）。
- 修正：协商用例的 `proto` 改为引用 `PROTO_VERSION` 常量；注释写明这类
  "测试复制被测常量"的写法是同一个坑。教训：**测试里的期望值如果来自被测实现，
  就只能验证自洽，验证不了正确性**；X25519 官方向量的期望值因此逐字节抄自 RFC，
  不来自本实现输出。

### 5.15 前端脚本顺序是硬约束（remote.js → app.js）

- `index.html` 先引 `js/remote.js`、后引 `js/app.js`。`remote.js` 只定义
  `GroupMeshRemote` 与内部函数、装载期不碰 DOM；`app.js` 在 `DOMContentLoaded`
  后调用 `GroupMeshRemote.init()`。顺序颠倒或漏挂会在装载期报
  `GroupMeshRemote is not defined`。
- `tests/js/plugin_asset_contract.mjs` 把关：`js/` 下不得有未被 `index.html`
  引用的脚本、装载期不得触碰 DOM。这与 media-player 的分片契约是同一类问题
  （见 `docs/media-player-design.md`）。

### 5.16 `noiseprotocol` 的三个反直觉点

- `NoiseConnection.from_name()` 的 `name` 参数虽然标了 `Union[str, bytes]`，
  但传 `str` 会在 `NoiseProtocol` 里抛 `NoiseProtocolNameError`；必须传 bytes。
- `HandshakeState.read_message()` 在完成握手时先 `self.rs = None` 再删 handshake
  state，而 `NoiseProtocol.keypairs['rs']` 自始至终没被赋值。因此对端静态公钥
  必须在 `handshake_done()` 之前抄下来（`noise.py` 的 `_capture_remote_static()`），
  否则 `transport.authorize_peer()` 拿不到设备公钥，全部连接会被判"不在名单里"。
- 握手期的 AEAD 失败由底层 `cryptography` 直接抛 `InvalidTag`，库没有包装；
  必须把它和库自己的异常一起映射，否则 prologue 不一致、握手密文被篡改都会漏成
  `InvalidTag`。
- 另外，`NoiseConnection.encrypt/decrypt` 强制 65535 字节单消息上限；本项目的
  256 KiB 分块 / 目录列表会超过它，因此传输态直接用其 `CipherState`（见 §4.2 的
  残余风险），而不是 `NoiseConnection.encrypt`。

### 5.17 私钥保护的两个坑

- **DPAPI 的 `DATA_BLOB` 必须设 argtypes/restype**：64 位下不设会把指针截断，
  `CryptProtectData` 可能失败或写出错乱结果；输出 blob 的 `LocalFree` 也必须调，
  否则每次读写泄漏一块系统内存。
- **keyring 必须做一次 set/get 往返探测**：没有桌面会话时
  `keyring.get_keyring()` 返回 fail backend，直接写会得到一个"看似成功、
  实际读不回"的密钥；探测失败就选下一个 protector，并缓存结果，避免每个文件
  都访问 keyring。
- **迁移只能单向**：已受保护的文件在 keyring 临时不可用时要报错，不能"回落到
  明文重写"——那等于一次 keyring 故障就把私钥降级；`migrate_file()` 只升级不降级。

### 5.18 后台同步踩到的共享连接坑（已修）

- 现象：加后台同步后，`test_placeholder_mtime_comes_from_peer` 失败，报
  `收到的帧过大（3987843650 > 4194304）`——一个正常的目录遍历请求被解析成垃圾帧。
- 根因：后台同步线程用 `_connect()` 从 **UI 复用连接池**里取了一条正在被
  `materialize_remote()` 使用的 Noise 连接；一条连接上的请求严格串行，两个线程
  同时 `send` 把字节流交错，对端读到的帧头就是垃圾。这与 §5.11 上传的第一个坑同源。
- 修正：后台同步、显式探测、注册表/名单 push 全部走 `_open_dedicated()`（每次新开
  一条连接，用完即关）；只有 UI 的浏览/取文件继续使用复用池。
- 教训：**连接复用池的并发语义必须显式**。只要出现第二个后台线程访问对端，就必须
  问"它是否和 UI 共用连接"；共用就得加锁或改用专用连接，不能假设调用是串行的。

---

## 6. 验证与测试工具一览

> 下文命令里的 `python` 指仓库虚拟环境：Windows `venv/Scripts/python.exe`，
> Linux `venv/bin/python`（在仓库根目录执行）。

### 6.1 门禁（提交前必跑）

| 命令 | 覆盖 | 本机实测 |
| --- | --- | --- |
| `venv/Scripts/python.exe -m ruff check .` | 风格与静态错误 | 通过 |
| `venv/Scripts/python.exe -m pyright main.py shell tools` | 类型（内核零错误是 CI 硬门禁） | 0 errors |
| `venv/Scripts/python.exe tools/check_version.py` | `pyproject.toml` 与前端 `package.json` 版本一致 | OK（1.2.0） |
| `venv/Scripts/python.exe tools/check_plugins.py` | manifest 规范、`minShellVersion` 格式 | OK（37 个警告，均为"字段运行时无效果"类） |
| `venv/Scripts/python.exe tools/check_packaging.py` | spec / HIDDEN_IMPORTS 覆盖 | OK |
| `venv/Scripts/python.exe -m unittest discover -s tests -q` | 全量单测 | **617 例通过，1 例跳过**，约 2–3 分钟（实测 113–157 s） |

改到打包相关文件（`docs/Releases/**`、`requirements*.txt`、`pyproject.toml`、
`tools/check_packaging.py`、`tools/check_build_tree.py`）时，再加真实构建与
`tools/check_build_tree.py`。

### 6.2 内核与插件测试

| 层次 | 命令 | 覆盖 | 前置 |
| --- | --- | --- | --- |
| 内核自检 | `python -m shell.groupmesh.cli selftest` | 原语、Noise 握手、名单规则 1–6、ACL、注册表、真实 TCP 端到端（含上传、越界拒绝），6/6 | 无 |
| 内核单测 | `python -m unittest tests.test_group_mesh_mvp` | 上述各模块的边界与拒绝路径；含 RFC 7748 官方向量、名单分发、上传 | 无 |
| Noise 官方向量 | `python -m unittest tests.test_group_mesh_noise_vectors` | 用 cacophony 夹具逐字节校验 3 条握手消息、`handshake_hash`、双向传输；另覆盖与原生 `noiseprotocol` 对端互通 | 已装 `noiseprotocol` |
| 私钥保护 | `python -m unittest tests.test_group_mesh_secrets` | DPAPI（仅 Windows）、keyring 替身、口令 AES-GCM 的往返/篡改拒绝；旧明文单向升级与"不降级重写" | 无 |
| 插件单测 | `python -m unittest tests.test_group_mesh_plugin` | manifest、契约（受保护路径）、API 形态、共享根约束、上传参数校验、节点启停、名单分发、自动发现、**后台同步状态/变更标记/push** | 无 |
| 物化与占位 | `python -m unittest tests.test_group_mesh_materialize` | 物化目录树、占位/真字节切换、与 image-viewer 的占位兼容 | 无 |
| 网络位置 | `python -m unittest tests.test_group_mesh_network_location` | `placement: network-location` 契约、`mirror_share` 落真字节 | 无 |
| 多实例端到端 | `python -m unittest tests.test_multi_instance_fixture` | 一个进程内两台真实实例：互相发现、物化/取字节、上传（含进度/取消/续传）、ACL 与覆盖策略、**后台同步变更传播（新增共享项无需手动刷新）** | 无 |
| 壳主体上下文 | `python -m unittest tests.test_shell_principal` | 凭据→主体、伪造参数被忽略、后台线程无主体、管理员端点 403、**插件层**受信注入 | 无 |
| 前端（纯渲染） | `python -m unittest tests.test_group_mesh_frontend_e2e` | 桩 Bridge + 无头浏览器：弹窗默认不可见、全新安装入口正确、按钮点击有反馈、**不再有"刷新设备"按钮、前端只读后端同步好的状态** | 本机浏览器 |
| 前端（真实壳） | `python -m unittest tests.test_group_mesh_shell_e2e` | 起真实壳服务 + 导航 + iframe：令牌链路、真实 Bridge、`<插件>__<方法>` 前缀 | 本机浏览器 |
| 前端脚本契约 | `python -m unittest tests.test_plugin_frontend_assets_js`（内部调 `node tests/js/plugin_asset_contract.mjs`） | 所有插件前端：`index.html` 与 `js/` 一致、按序装载、装载期不报错 | Node |

### 6.3 跨平台与跨机联调

| 工具 | 命令 | 覆盖 | 前置 |
| --- | --- | --- | --- |
| 跨平台确定性 | `python shell/groupmesh/tools/interop_fixture.py --out fixture.json`，另一端 `--check fixture.json` | 同一种子在两平台必须算出相同密钥与签名（11 个字段） | 两端都能跑内核 |
| 纯 IPv6 主路径 | `pwsh -File shell/groupmesh/tools/ipv6-test.ps1` | Linux 绑全局 IPv6、Windows 用 `[2409:…]:port` 连接；700 KB sha256 一致；越权/越界被拒；注册记录写入全局 IPv6；节点热认名单 | `~/.ssh/config` 的 `omnibox-linux` 别名 |
| IPv4 兜底 | `pwsh -File shell/groupmesh/tools/lan-test.ps1` | 同网段 IPv4 完整链路与负向验证 | 同上 |
| Linux 侧准备 | `shell/groupmesh/tools/lan-test-linux-prep.sh`、`lan-test-linux-add-member.sh` | 建团体、共享项、加成员、起服务 | Linux 主机 |
| 非无头演示 | `python tests/debug_connection.py`、`debug_materialized_gallery.py`、`debug_network_location.py` | 逐步截图回答"肉眼才能回答"的问题；`debug_connection.py` 专做连接与共享访问（程序化登记地址、sha256 对比） | 桌面环境 |

### 6.4 怎么选

- 改协议内核：先 `selftest`，再 `test_group_mesh_mvp`，最后跨平台 fixture；
- 改插件 API / 数据布局：`test_group_mesh_plugin` + 相关物化/网络位置用例；
- 改前端：`test_group_mesh_frontend_e2e`（快）→ `test_group_mesh_shell_e2e`（真）；
- 改名单/注册/上传的端到端行为：`test_multi_instance_fixture`；
- 改壳鉴权/主体：`test_shell_principal`；
- 对外联调或发布前：两个 PowerShell 脚本至少跑 `ipv6-test.ps1`。

---

## 7. 下一步建议顺序

按依赖关系排列，每一步都应有可复核的验收命令。本轮已完成私钥保护（§4.1）与
Noise vetted 实现 + 官方向量（§4.2）。

1. **P2：Rekey 与连接上限**（§4.3、§4.10）——`noiseprotocol` 已提供
   `CipherState.rekey()`，需要应用层协商"何时轮换"（消息数/字节数/时长）；
   同时给节点加连接上限与"不自动监听"开关，收敛 DoS 面。
2. **P2：解散通告、设备配对/多设备、稳定地址判定**（§4.7、§4.6.1）——
   解散记录类型与处理路径；`add_device()` 的配对入口；Windows
   `GetAdaptersAddresses(Temporary)` 识别临时地址。
3. **P2.5：遗留 `.part` 的属主端可见性**（§4.7）——列出未完成暂存与占用，
   让属主能清理。
4. **P1 主体限权（暂缓）**：本轮按决定不做 §4.8、§4.9；方案与验收已写在
   §4.8 / §4.9，恢复时按原条目推进（壳侧 `authorize_file()` 钩子 + 插件
   `require_principal()` + 收窄 `upload_remote` 的来源）。
5. **P3：房间/语音/游戏面 Companion 子插件**——不在本插件路线，前置实测见
   [Companion 子插件](./group-mesh-companions.md) §4。
6. **P4：Android 轻客户端 / 本地网关**（仅保留设计位）。

---

## 8. 尚未回答的问题

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | **跨公网**是否真的可达？同网段 IPv6 直连已实测，运营商是否放行入站高位端口未验证 | 决定设计 §1.1 的"公网 IPv6"前提能否成立 |
| 2 | Windows 上如何区分稳定地址与 RFC 4941 临时地址？ | 决定注册记录写入哪个地址（§4.6.1） |
| 3 | 群主私钥一旦丢失，团体永久不可管理；是否需要"名单备份 + 冷存储"的操作指引？ | 决定是否需要额外用户引导（设计 §5.6） |
| 4 | 插件前端是否需要在没有全局 IPv6 时给出明确的"仅局域网可用"提示？ | 决定设计 §1.3 的边界在界面上如何表达 |
| 5 | 取消/中断留下的 `<目标>.part` 是否需要属主端可见（列出未完成暂存与占用）？ | 决定 §4.7 的遗留暂存由谁、怎么清理 |
| 6 | 多使用者部署下，物化缓存与下载目录的"主体级授权"用什么模型？ | 决定 §4.8 的 `authorize_file()` 接口形态与 group-mesh 的 ACL 映射 |

---

## 9. 本轮分析结论摘要

**做得好的地方**：协议级授权（ACL 按握手验证过的主体判定）、名单规则链与准入
（"本机认得这一份"而非"对端自证"）、上传的暂存/提交/续传语义、物化与网络位置
对消费型插件的兼容，是本项目最扎实的部分；内核与插件的分层使协议可以脱离 GUI
验证，跨机脚本与多层测试把关键拒绝路径都覆盖了。

**本轮已收敛三项**：

1. 私钥保护（§4.1）：DPAPI / keyring / 口令 AES-GCM；旧明文首次读取即单向升级，
   已受保护的文件不降级；插件状态页显示当前保护级别。
2. Noise 换 vetted 实现（§4.2）：`noiseprotocol` + cacophony 官方向量逐字节校验；
   替换前的手写状态机也通过同一组向量，因此线格式不变、`PROTO_VERSION` 保持 2。
3. 后台同步（§4.7、设计 §5.7 / §7.3）：移除"刷新设备"手动入口，改为可配置间隔
   （`sync_interval_seconds`，默认 60 秒）的定时轮询 + 名单/注册变更时 push；
   同步线程用专用连接，避免与 UI 复用连接池并发（§5.18）。

**仍需优先处理**：

1. 主体限权（§4.8、§4.9，**本轮按决定暂缓**）：多使用者部署下仍是跨主体
   读/写/外带面，`upload_remote` 还能带出 `.config` 凭据；
2. 节点自动监听 + 每连接一线程、无连接上限（§4.10）；
3. 长连接 Rekey（§4.3）；
4. `plain` 回落（§4.1）：headless Linux 没有 keyring 且未设 `OMNIBOX_SECRET_KEY`
   时私钥仍是明文，状态页会标红——这是残余风险，不是"已静默忽略"。

**可以晚一点但必须记录**：解散、设备配对、稳定地址、内容寻址、Android 轻客户端；
`minShellVersion` 已决定不做运行时校验。

**文档卫生**：§4.14 列出的过期文件头与错误交叉引用已在本轮一并修正，p1-review
已改为存档指针；剩下无源 pyc 等仓库卫生项建议顺手清理。
