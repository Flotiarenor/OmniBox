# 团体组网插件（group-mesh）设计

> 文档性质：**当前实现说明**（与 `media-player-design.md`、`image-viewer-design.md`
> 同一体例），只写"现在是什么样"，不写版本变更流水。
> 实现进度、已知漏洞、易错项与测试工具见
> [实现路径与现状](./group-mesh-implementation-path.md)。
> 定位：**核心插件**（供消费型 Companion 插件依赖）+ 壳侧配套改造。
> 代码位置：协议内核 `shell/groupmesh/`，插件 `plugins/group-mesh/`。
> 关联文档：[Companion 子插件（房间/语音/游戏面）](./group-mesh-companions.md)、
> [插件开发指南](./plugin-guide.md)、[主程序方向](./core-direction.md)。
>
> **章节号约定**：`shell/groupmesh/**` 与 `plugins/group-mesh/backend/main.py`
> 的注释直接引用本文的 §1.3 / §3.3 / §4.x / §5.x / §6.x / §7.x / §10 / §11.2 /
> §12 / §13。整理文档时保持 §1–§17 的编号与语义不变，避免代码注释指错位置。

---

## 0. 阅读指引与实现概览

### 0.1 这份文档回答什么

本文回答"group-mesh 现在由哪些部件组成、各自怎么工作、边界在哪"。它不回答
"下一步先做什么"——那在[实现路径与现状](./group-mesh-implementation-path.md)；
也不回答"踩过哪些坑"——同样在那份文档里按现象逐条列出。

一句话概括：**无中心的团体组网插件**。它给互相信任的一组使用者提供身份、
团体名单、共享节点发现、共享项的读写与加密传输；房间、语音、老游戏联机
由未来的 Companion 子插件复用它的身份与通道来实现，不在本插件范围内。

### 0.2 实现状态总览

| 能力 | 状态 | 主要位置 |
| --- | --- | --- |
| 主体/设备身份、签名与落盘 | **[已实现]** 私钥由 DPAPI / OS keyring / 口令 AES-256-GCM 保护；三者都不可用时如实回落明文并在状态里标红 | `shell/groupmesh/identity.py`、`secret_store.py` |
| 团体名单：结构、规则 1–6、并发收敛、宽限提示 | **[已实现]** | `shell/groupmesh/roster.py` |
| 名单分发：`op=roster` + 名单历史准入 + 后台同步 | **[已实现]** 定时轮询 + 变更时 push，间隔见设置 | `shell/groupmesh/node.py`、`roster_history.py`、`plugins/group-mesh/backend/main.py` |
| 共享项声明、只读/可上传两档 ACL | **[已实现]** | `shell/groupmesh/shares.py` |
| 共享读取：offset/length 分块 | **[已实现]** | `shell/groupmesh/node.py` |
| 共享写入：探询、暂存、提交、覆盖策略、续传 | **[已实现]** | `node.py`、`client.py` |
| 注册记录与注册表（seq 单调、防重放） | **[已实现]** 整表快照 + 按 seq 合并，无 `since` 游标 | `shell/groupmesh/registry.py` |
| 加密传输：Noise_XX + 协商 + 授权确认 | **[已实现]** 状态机由 `noiseprotocol` 提供；官方 cacophony 向量在用例里逐字节校验；**仍无 Rekey** | `noise.py`、`transport.py` |
| 远端物化、按需取字节、一次性镜像、网络位置扩展 | **[已实现]** | `plugins/group-mesh/backend/main.py` |
| 上传任务：进度、取消、断点续传、任务表落盘 | **[已实现]** | 同上 |
| 壳侧主体上下文（凭据→主体、ContextVar 注入） | **[部分]** 壳侧已落地；`/file`、`/thumbs` 尚无主体级检查点，group-mesh 插件自身也还未调用 | `shell/backend/principal.py`、`plugin_base.py` |
| `minShellVersion` 运行时校验 | **[未实现 / 决定不做]** 当前只由 `tools/check_plugins.py` 门禁校验 | `tools/check_plugins.py` |
| 内容寻址分块传输 / 做种 | **[未实现]** 现为 offset/length 分块 | `node.py` |
| Android 轻客户端 / 本地网关 | **[未实现]** 仅保留设计位 | — |
| 房间 / 语音 / 游戏面 | **不在本插件范围** | [Companion 草案](./group-mesh-companions.md) |

### 0.3 分层

```
┌──────────────────────────────────────────────────────────────┐
│ shell/backend/        壳：Flask 路由、令牌/主体、设置、文件服务 │
│   principal.py        凭据 → 主体；ContextVar 受信注入           │
└───────────────────────────┬──────────────────────────────────┘
                            │ PluginBase / register_api
┌───────────────────────────▼──────────────────────────────────┐
│ plugins/group-mesh/   插件层：数据目录、API、节点线程、前端      │
│   backend/main.py                                              │
│   frontend/index.html + js/{app,remote}.js                     │
│   frontend/network-location.html（壳共享目录组件的扩展提供方）   │
└───────────────────────────┬──────────────────────────────────┘
                            │ import shell.groupmesh（普通包导入）
┌───────────────────────────▼──────────────────────────────────┐
│ shell/groupmesh/      协议内核：不 import Flask / pywebview     │
│   crypto_prims records identity roster shares registry         │
│   noise transport node client cli selftest roster_history      │
└──────────────────────────────────────────────────────────────┘
```

内核放在 `shell/groupmesh/` 而不是插件私有目录，有三个结构性理由：

1. 它是壳层能力：设计文档 §12 要求的"凭据→主体映射"本身就是壳的职责；
2. 它要被多个 Companion 插件复用，不能依赖另一个插件的内部结构；
3. 它必须能脱离 GUI 独立跑（`python -m shell.groupmesh.cli selftest`），
   这样协议错误与插件集成错误可以分开定位。

放在 `shell/backend/` 下面不行：`shell/backend/__init__.py` 会 eagerly import
`file_server`，从而把整个 Flask 栈拖进来，"独立跑"这条就没了。因此它是
`backend/` 的**兄弟**而不是子目录。

---

## 1. 定位

### 1.1 目标

为若干互相信任的使用者提供一套无中心的组网能力，使各自设备在公网 IPv6 上：

1. 互相发现并完成身份认证；
2. 按授权共享文件，允许成员把自己的设备接入团体；
3. 为上层 Companion 插件提供统一的加密传输通道（谁在用、传什么由子插件决定）。

老游戏联机（二层虚拟局域网）与语音**不属于本插件目标**，由 Companion 子插件
复用本插件的身份与通道实现。

### 1.2 与仓库现有插件的关系

group-mesh 是**核心插件**：自身不直接面向最终用途，只提供身份、团体、注册、
发现与传输。文件传输面板、游戏联机、语音等以 Companion 插件出现，通过
`manifest.dependencies: ["group-mesh"]` 声明依赖，经
`PluginBase.get_dependency('group-mesh')` 与 `Bridge.callPlugin` 使用其能力。
这与 `docs/core-direction.md` 的 Companion 形态一致。

`manifest.json` 的 `dependencies` 目前为空数组——"核心插件"还没有专门的
`kind` 字段表达，这是当前的已知表达缺口。

### 1.3 非目标

| 项 | 说明 |
| --- | --- |
| 无公网 IPv6 的使用者 | 不提供 IPv4 NAT 穿透或公共中继兜底；同网段 IPv4 只作地址族兜底（§4.6） |
| Android 完整节点 | 首版不实现，仅预留轻客户端设计位（§11.2） |
| 内容寻址 / 做种 | 当前是 offset/length 分块（§10） |
| 自动更新 | 不做后台轮询与静默替换 |
| 集中式账号服务 | 不设中心服务器；团体之间默认完全隔离 |
| 房间 / 语音 / 游戏面 | 由 Companion 子插件承担（§8、§9） |

### 1.4 插件层形态

目录结构：

```
plugins/group-mesh/
├── manifest.json
├── backend/
│   ├── main.py            # GroupMeshPlugin：类骨架、settings_schema、生命周期与 API 注册
│   ├── common.py          # 各分片共用的常量与纯函数（_opts / _mtime_ns）
│   ├── storage.py         # 路径、受保护路径与身份 / 名单 / 共享项的本机读写
│   ├── status.py          # get_status 首屏状态汇总
│   ├── group.py           # 身份、建团 / 加入、邀请串、成员增减
│   ├── shares.py          # 共享根挂载 / 移除 / 状态与用量
│   ├── peers.py           # 对端发现、手动登记、端点解析、list_peers
│   ├── sync.py            # 注册表 / 名单后台同步与出站连接
│   ├── remote.py          # 列远端目录与取回单文件
│   ├── upload.py          # 上传任务表、续传记录与上传线程
│   ├── materialize.py     # 远端目录树物化与索引对账
│   ├── mirror.py          # 共享项镜像到用户目录（「网络位置」）
│   ├── fetch.py           # 占位判定 / ensure_file、并发去重、缓存清理
│   └── node.py            # 节点启停、注册记录发布、自动启动
└── frontend/
    ├── index.html           # 主页面：状态、身份、名单、共享项、共享节点、远端共享
    ├── network-location.html# "网络位置"扩展页面（壳共享目录组件内嵌）
    ├── group-mesh.css
    └── js/
        ├── app.js           # 状态读取与渲染、主页面事件绑定
        └── remote.js        # 远端共享分片：设备→共享项→目录→下载/上传/物化
```

后端按方法职责拆成 12 个 mixin 分片，各分片由 `main.py` 用
`shell.backend.plugin_utils.load_sibling` 加载后混入 `GroupMeshPlugin`：分片不能互相
import `main.py`（后端入口由 PluginManager 用 importlib 直接加载，会成环），共用的
常量与纯函数放在 `common.py`；mixin 必须排在 `PluginBase` 之前，否则
`get_data_root` / `get_protected_paths` / `on_settings_changed` 等覆写会被基类实现盖掉。

数据布局（全部在 `get_data_root()` = `<全局数据根>/group-mesh` 之下）：

| 路径 | 内容 | 可见性 |
| --- | --- | --- |
| `identity/principal.json`、`identity/devices/<id>.json` | 主体与设备私钥 | **受保护**：`get_protected_paths()` 申报，不经 `/file`、`/files`、`/thumbs` 外泄 |
| `identity/roster.json` | 当前团体名单 | 本机 |
| `identity/roster-history.json` | 最近 12 份名单（准入判定） | 本机 |
| `identity/shares.json` | 共享项**声明**（签名协议对象） | 本机，声明会发给成员 |
| `identity/share_roots.json` | 共享项的**本机路径/容量**（本机事实，从不发出） | 本机 |
| `identity/registry.json` | 注册表快照 | 本机 |
| `downloads/` | 显式取回的文件（`download_dir` 可改，设置里**只接受本机目录**：该字段声明 `local_only`，不显示「网络位置」入口 —— 它是取回的落点，不能当自己的来源） | 经 `/file?plugin=group-mesh` |
| `uploads.json` | 上传续传记录（每 4 MiB 落盘） | 本机 |
| `upload-tasks.json` | 上传任务历史（最近 20 条） | 本机 |
| `.cache/remote/<设备ID>/<共享标识>/` | 物化目录（占位 + 已取字节）与索引 | 经 `/file` 可读，属本机可信域 |
| `.cache/staging/` | 按需取字节的暂存（`os.replace` 前） | 不对外 |
| `.cache/peers.json` | 手工登记的对端地址 | 本机 |

插件 API（`register_api()`，25 个方法）按用途分组：

| 分组 | 方法 |
| --- | --- |
| 状态 | `get_status`、`get_node_status` |
| 身份与团体 | `init_identity`、`get_device_keys`、`create_group`、`join_group`、`get_invite`、`add_member` |
| 共享项 | `add_share`、`remove_share`、`refresh_share_roots` |
| 对端与发现 | `peers`（list/add/update/remove）、`my_endpoint`、`list_peers` |
| 远端读写 | `list_remote`、`download_remote`、`upload_remote`、`upload_status`、`cancel_upload` |
| 物化/镜像/缓存 | `materialize_remote`、`mirror_share`、`remote_cache`、`clear_remote_cache` |
| 节点 | `start_node`、`stop_node` |

前端要点：

- `index.html` 把界面分成**常驻侧栏 + 面板**两栏，结构与仓库其它插件完全一致
  （image-viewer / manga-library / media-player）：
  `#app > (aside.gm-side.view-sub-sidebar + .view-body > .view-toolbar + .view-content)`。
  侧栏是 `#app` 的**直接子元素**、跑满全高；工具栏属于右侧主区（不是横跨整宽）。
  侧栏按「本机 / 团体 / 传输 / 关于」分组导航并在底部常驻节点与名单摘要，
  主区是五个**纯显隐**面板 —— 身份与节点（本机身份 + 共享节点）、名单与成员、
  共享项、远端共享、路线图（原「尚未实现」列为可折叠区块）。
  面板切换只改 `data-active`、不销毁 DOM，与壳给保活插件用的"iframe 常驻"策略一致；
  面板的标题/副标题写在 `section[data-panel]` 的 `data-title` / `data-sub` 上，
  新增一个面板 = 侧栏多一个 `button[data-panel]` + 一个 `section[data-panel]`。
  所有判定在后端，前端只读状态、渲染、调 Bridge。
- `remote.js` 单独承载"远端共享"分片（设备列表、目录浏览、下载、上传、
  物化缓存），由 `app.js` 在 `DOMContentLoaded` 后注入回调并调用
  `GroupMeshRemote.init()`。`index.html` 里 `remote.js` 必须排在 `app.js`
  之前；`tests/js/plugin_asset_contract.mjs` 把关"无孤立脚本、装载期不碰 DOM"。
  该分片的读回轮询（15 秒）挂在壳的 `onShow` / `onHide` 上：本插件声明
  `"keepAlive": true`（状态要持续上报，切走不该停），因此 iframe 常驻、切走只会收到
  `onHide`，必须靠它停表（`docs/plugin-guide.md` §4.4）。
- 前端只复用壳注入的基建，不重复造：主题 token（`variables.css`）、通用类
  （`base.css` 的 `.btn` 系列 / `.view-*`）、动效类（`effects.css` 的 `.obx-glass` /
  `.obx-scroll` / `.obx-card-lift` / `.obx-skeleton` / `.obx-stagger`）、
  `.modal` + `confirmDialog`、`FolderPicker`、以及 `PluginLifecycle`。
  插件前缀 token（`--gm-*`）只承接尺寸与软色，颜色一律走壳 token，
  用户在外观设置里改色与深浅主题都能跟随。级联优先级与两个前端用例坑见
  [实现路径与现状](./group-mesh-implementation-path.md) §5.19。
- `network-location.html` 通过 `get_extensions()` 注册为壳共享目录组件的
  `placement: network-location` 提供方，选中后 `postMessage` 回填一个**本地目录**
  （见 §15.3）。

---

## 2. 术语

| 术语 | 标识 | 含义 |
| --- | --- | --- |
| 主体 | principal | 一个人。持有长期 Ed25519 密钥，可以拥有多台设备 |
| 设备 | device | 一台机器。持有自己的 Ed25519 签名密钥与派生 X25519 握手密钥 |
| 团体 | group | 身份与授权的边界：一名群主 + 成员集合 |
| 群主 | owner | 团体创建者。唯一可签发任意名单、任命管理员、转移群主的主体 |
| 管理员 | admin | 由群主任命。只能增删普通成员 |
| 成员 | member | 按共享项 ACL 读写数据，无成员管理权 |
| 共享节点 | share node | 把自身地址与共享清单注册进团体的设备 |
| 共享项 | share | 某设备上的一个目录及其 ACL |
| 注册 | registration | 设备自签的端点与共享清单记录 |
| 团体名单 | roster | 群主/管理员签名的成员与角色清单 |
| 房间 | room | 一次联机/语音活动，独立于团体；由 Companion 负责 |

---

## 3. 形态与角色

### 3.1 无中心

系统内不存在必须常驻的服务器。任何设备都可以创建团体、把地址注册为共享节点
（开机即可被访问、关机即退出）。"共享区"不是集中存储，而是各成员主动共享出的
内容的聚合视图。

### 3.2 角色

| 角色 | 权限 |
| --- | --- |
| 群主 | 增删成员、任命/撤销管理员、签发任意名单、转移群主、解散团体 |
| 管理员 | 增删普通成员；不能改群主或管理员集合 |
| 成员 | 无成员管理权；按各共享项 ACL 读、写 |

角色由名单推导（`roster.py` 的 `role_of()`），规则 4/5 在签发时强制。
**当前插件与 GUI 只提供增删普通成员**；转移群主/任免管理员仍只有 CLI
（§5.6）。

### 3.3 两个正交的权威面

| 权威面 | 持有者 | 作用范围 | 载体 |
| --- | --- | --- | --- |
| 团体成员权威 | 群主与管理员 | 名单里有谁 | 团体名单 |
| 设备资源权威 | 每台设备所属主体 | 该主体设备上的共享项与 ACL | 共享项声明 |

**群主与管理员对成员设备上的数据没有任何特权**：访问他人共享内容时与普通成员
走同一套 ACL。资源属主按**主体**判定、不按设备判定，因此同一主体的手机访问
自己的主力机共享项时按属主处理（`shares.py` 的 `Authorizer`）。

### 3.4 平面划分

本插件只做**应用面**：自有协议、直接使用 IPv6，承载身份、注册、发现、共享与
文件传输。老游戏的二层虚拟局域网是**游戏面**，由 Companion 子插件用 SoftEther
实现；两个平面共用同一套身份、名单与注册表，传输层各自独立。

---

## 4. 身份与密钥

### 4.1 主体与设备分离

一个主体可以拥有多台设备；权限绑定在主体上，因此新增设备不需要重新配置权限。
目录布局为 `identity/principal.json` + `identity/devices/<id>.json`，加载时：
单设备目录可省略 `device_id`，多设备必须显式指定（`identity.py` 的
`Identity.load()`）。

**私钥保护**（`secret_store.py`）：`principal.json` 与 `devices/<id>.json` 的整个
JSON 内容先保护再落盘，按可用性自动选择——Windows 用当前用户的 **DPAPI**；
桌面 Linux / macOS 用 **OS keyring**（随机 32 字节密钥存 keyring，文件用
AES-256-GCM）；设置 `OMNIBOX_SECRET_KEY` 时用 scrypt 口令派生的 AES-256-GCM；
三者都不可用时才回落到旧版明文，并在插件状态页标红。旧版明文文件在首次读取时
**单向升级**成当前可用的最强保护；已受保护的文件在 keyring / 口令不可用时
报错，而不是把密文当明文继续解析。`identity/` 仍整个申报为受保护路径
（`get_protected_paths()`），`/file`、`/files`、`/thumbs` 对它一律拒绝。

多设备入口缺失：`Identity.add_device()` 已实现但**无任何调用方**（CLI 与插件
都没有暴露），所以"新增设备"目前在数据层可用、在操作层不可达。设计中的
"局域网配对或二维码授权"完全没有实现；设备凭据由**本机主体私钥自签**，
不是由已有设备签发，也不进名单、握手时不校验，只是本机完整性标记。

### 4.2 设备注册

设计流程：新设备生成密钥 → 已有设备授权（配对/二维码）→ 已有设备签发绑定
`principal + device_pubkey` 的凭据 → 凭据进名单并分发。

当前实现只完成第 1 步与第 3 步的"结构"：`Device.create()` 生成密钥，
`Device._credential_*` 能自签/验签；第 2、4 步没有实现。因此实际的安全边界是：
**设备公钥只要被群主写进名单，就代表该主体**；凭据本身不参与认证。

### 4.3 身份统一

同一份身份材料同时用于应用面握手与共享节点注册，但不维护第二套账号。
实现上是**同一份 32 字节种子派生两把用途分离的密钥**：

- Ed25519 密钥对负责签名（名单、共享项声明、注册记录、设备凭据）；
- X25519 密钥对由同一份种子确定性派生（`crypto_prims.dh_keypair_from_sign_seed()`），
  负责 Noise 握手的静态密钥；
- 握手时附一份**设备绑定证明**（`identity.encode_binding_payload()`）把两者绑在一起，
  否则名单里存的是 Ed25519 公钥、握手拿到的是 X25519 公钥，无法对应。

这不是实现偏好，而是必要约束：Ed25519 私钥是种子经 SHA-512 派生的标量，
不是合法 X25519 标量，直接当 DH 用会让双方算出不同共享密钥且都不报错，
直到 AEAD 才以 MAC 校验失败暴露。

### 4.4 传输层

采用**名单 + Noise 握手**，模式 `Noise_XX`：

- 两端均持有名单，握手过程互相认证并交换静态公钥；
- 默认套件 `25519 / ChaCha20-Poly1305 / BLAKE2s`；
- 数据使用 AEAD，密钥由握手结果经 HKDF 派生；
- 握手结束后由响应方回一帧 `ok` / `deny:<原因>` 作为**授权确认**：
  XX 的第 3 条消息才把发起方的静态公钥与绑定负载交给响应方，不确认的话
  被拒的发起方会以为连接成功（详见实现路径文档 §5.4）。

**实现来源（v0.2 已替换）**：

1. **状态机由 `noiseprotocol` 提供**（纯 Python，MIT，标准
   `Noise_XX_25519_ChaChaPoly_BLAKE2s`）。`noise.py` 只做角色 / 静态密钥 /
   prologue 的翻译、异常统一与接口兼容，不再自己维护握手状态机。
2. **官方测试向量逐字节校验**：`tests/fixtures/noise_XX_25519_ChaChaPoly_BLAKE2s.json`
   取自 cacophony 官方向量，用例验证 3 条握手消息、`handshake_hash`、双向传输
   消息全部字节一致；另有"与原生 `noiseprotocol` 对端互通"的用例。替换前的
   手写状态机也通过了同一组向量，因此这次替换**不改变线格式**，协议版本保持 2。
3. **DH 公钥编码符合 RFC 7748 §5**。传输态直接使用 `noiseprotocol` 的
   **CipherState**（同一套密钥、nonce 推进与 AEAD），不套它高层 API 的
   65535 字节单消息上限——本项目的应用帧是整条 JSON 请求/应答，目录列表可能
   超过该上限；这是有意的帧长选择，与只实现规范长度检查的实现对传大帧时会被
   拒绝，已记录在实现路径文档。
   `noiseprotocol` 自身是 Alpha 状态、未做独立安全审计，它相对手写实现的价值是
   "独立实现 + 官方向量可复核"，不是"已被审计"。

**没有密钥轮换**：长连接的传输密钥在整条连接生命周期内不变，唯一上限是
nonce 计数器耗尽（`noise.py` 直接报错终止，不是 Rekey）。单条超长连接泄露一把
传输密钥，该连接内全部内容可解。`noiseprotocol` 的 CipherState 自带
`rekey_inbound_cipher()` / `rekey_outbound_cipher()`，但应用层还没有协商
"何时轮换"的帧，因此尚未启用。

### 4.5 协议协商字段

握手开始时双方各发一条明文消息，其原始字节并入被认证的 prologue：

```json
{
  "proto": 2,
  "suites": ["noise-XX-25519-chacha20poly1305-blake2s"],
  "features": ["roster-v1", "registration-v1", "share-read", "share-range"]
}
```

| 字段 | 含义 | 变更语义 |
| --- | --- | --- |
| `proto` | 协议主版本（当前 `PROTO_VERSION = 2`） | 不兼容的线上字节变更必须递增；不一致直接断开并提示升级 |
| `suites` | 支持的加密套件，按偏好排序 | 取交集后选本地优先级最高者；无交集断开 |
| `features` | 可选能力开关 | 未声明按旧行为处理 |

两条实现约束：协商结果必须并入被认证的 prologue（防降级）；该消息在认证前
处理，解析必须做长度与取值边界检查。

已知局限：`KNOWN_SUITES` 目前只有一套套件，"可协商替换"尚无第二套可切。
**写能力不参与协商**：`features` 里没有 `share-write`，`write` 请求的语义本身
就是 `op=write`，服务端按 ACL 判定——不懂该扩展的对端不会主动发写请求。

### 4.6 地址族与可达性

设计目标地址族是 IPv6；监听默认 `::`，同时接受 IPv6 与 IPv4。IPv4 只作
同一可达网段内的地址族兜底，不提供 NAT 穿透或中继，因此不与 §1.3 的非目标
冲突。端点可达性靠连接侧**逐个尝试全部候选端点**判定，不按网段筛——实测
Radmin VPN 等网卡地址对同为该 VPN 成员的设备可用，按网段筛会误删合法端点。

#### 4.6.1 稳定地址尚未判定

§7.4 要求监听与注册使用稳定地址（EUI-64 / RFC 7217），不使用 Windows 默认的
RFC 4941 临时地址。当前 `registry.local_addresses()` 做不到：Python 标准库拿不到
"是否为临时地址"。实测 Windows 会同时存在稳定地址与临时地址，因此不显式
`--bind` 时两者都会写进注册记录，临时地址轮换后该端点失效（`_refresh_own_registration()`
会在地址集合变化时递增 `seq` 重发，代价是旧端点期间白试一次）。
收敛路径：显式 `--bind`（当前规避手段）；Windows 用 `GetAdaptersAddresses` 的
`Temporary` 标志识别；或改用 RFC 7217 地址并只发布它。

---

## 5. 团体名单

### 5.1 结构

```json
{
  "group":   "<团体标识>",
  "version": 12,
  "prev":    "<前一份名单的 content_hash>",
  "expires": 1767225600,
  "owner":   "<群主主体公钥>",
  "admins":  ["<主体公钥>"],
  "members": [
    {"name": "alice", "pubkey": "<主体公钥>", "devices": ["<设备公钥>"]}
  ],
  "sig": "<签名>"
}
```

`roster.py` 的 `Roster` 实现该结构，`expires` 只是提示字段（§5.4）。

### 5.2 验证规则

验证相对本地当前名单 `cur` 执行：

| # | 规则 | 作用 |
| --- | --- | --- |
| 1 | `new.version > cur.version` | 防回滚 |
| 2 | `new.prev == hash(cur)` | 只接受直接后继 |
| 3 | 签名者属于 `{cur.owner} ∪ cur.admins` | 只有群主与管理员可签发 |
| 4 | 签名者为 `cur.owner` 时无附加限制 | 群主全权，含转移群主、变更管理员集合 |
| 5 | 管理员签发时要求 `new.owner == cur.owner` 且 `set(new.admins) == set(cur.admins)` | 管理员只能增删普通成员，不能自我提权 |
| 6 | 本地名单为空时直接信任带外分发的创始名单 | 信任锚起点 |

六条均有自检（`selftest.py`）与单测覆盖。规则 3 的签名验证用**新名单**声明的
`owner`/`admins`，再对 `cur` 复核签发资格；因规则 5 强制管理员集合相等，
两条路径等价。

### 5.3 并发签名与冲突

规则 2 保证一份名单只有一个直接后继。两名管理员同时签发不同后继时二者
`prev` 与 `version` 相同；`pick_concurrent()` 按名单哈希字典序取较小者，
各节点无需通信即可收敛。当前**没有运行时调用方**：本地只维护一份
`roster.json`，该函数只被自检与单测使用；等出现"同时持有两份并发候选"的
真实路径后才会启用。

### 5.4 宽限策略

**软件不做版本强制**：

- `expires` 到点不阻断通信；
- 版本号单调递增，节点不因对方名单较旧而拒绝连接；
- 界面提示"你的名单已过期 N 天"，由团体自行协调。

后果：**移除成员的有效窗口没有上界**。被移除者与已更新名单的节点无法握手，
但与尚未更新的节点仍可通信，直到后者更新。该窗口由团体协调解决，软件不额外
施加机制。`staleness_report()` 把"过期 / 版本"信息转成界面提示；当前对端名单
恒为 `None`（§5.7 的拉取不改变这一点），因此"与对方名单不一致"这个分支要等
界面把 `peer_versions` 用起来才有意义。

### 5.5 解散

设计上解散是一条独立于名单的通告记录：只有当前 `owner` 可签、携带高于当前
名单的版本号、成员收到后停止使用该团体但**保留本地数据不删除**。
**当前未实现**：记录类型、签名校验、处理路径、API 与界面都没有。

### 5.6 群主密钥边界

群主密钥丢失等于团体永久无法再管理（不能加人/删人/解散）。无中心系统没有恢复
机制，唯一缓解手段是群主在密钥有效时签发包含新 `owner` 的名单完成转移。
内核与 CLI 支持 `roster transfer/promote/demote`，但**插件与 GUI 没有这些操作**：
`add_member()` 写死当前 `owner` 与 `admin_keys`，界面既不能转移群主也不能任免
管理员；使用说明也还没有写。该限制直接影响团体可维护性。

### 5.7 名单分发

名单用带外邀请串（`gm1:`）建立信任锚（规则 6），之后的更新通过协议分发：

- **协议**：`op=roster` 的 `action=list`（取名单）与 `action=push`（送名单）。
  入站名单一律跑规则 1–6，中转方无法篡改内容——因此"分发通道不必可信"成立。
- **未入名单的设备只放行 `roster`**：握手不再按"在不在名单里"拒绝连接（否则新
  成员永远收不到新名单），改在 `Node.handle()` 按 op 收敛——不在名单者只能拉名单，
  其余 op 一律 `not-in-roster`。少了这一步，任何能完成握手的设备都能直接读共享项。
- **准入（"谁能取走名单"）**：对端给出的最新名单必须**本机认得**——即出现在
  本机当前名单或 `roster-history.json` 历史链里。只验"同团体、版本不更新"不够：
  团体名是用户起的字符串，构造成本几乎为零；`content_hash` 伪造不了，
  "本机认得这一份"才是真凭据。判定只读本机掌握的内容，**绝不读对端送来的字段**。
- **名单历史**（`roster_history.py`，`identity/roster-history.json`）：保留最近
  12 份（新→旧、按 `content_hash` 去重）。群主连加两人（v1→v2→v3）而某设备只有
  v1 时，`v3.prev` 指向 v2，靠历史才认得出 v1，那台设备才能不经邀请串拿到 v3。
  插件与 CLI 的每个名单落盘入口都会并入历史。
- **采纳**：对端版本不高于本机时跳过；否则验签后落盘并记录对端版本，
  `get_status()` 的 `roster.adopted_notice` / `peer_versions` 供界面做对照。
- **后台同步（定时轮询 + 变更时 push）**：插件加载后启动后台循环，每
  `sync_interval_seconds`（设置项，默认 60 秒）拉一次注册表并顺带拉名单；
  本机名单变化（建团、加入、加成员、采纳新名单）时立即标记待推送并唤醒循环，
  用 `op=roster push` 把新名单推给已知对端。已经不比本机旧的端点会跳过，
  因此正常情况每个版本只推一次。CLI `serve` 也接了 `roster_saver` 与历史读取。

已知限制：

1. **push 需要已知端点**：没有手工登记、注册表里也没有对方地址时推不出去，
   只能等对方先轮询过来；这是"首次发现需要一次带外地址交换"的延续（§7.4）。
2. **后台同步只在插件加载期间运行**：插件未加载时节点通常也没运行，此时不做
   轮询；这不是常驻服务。
3. **界面尚未展示** `adopted_notice` / `peer_versions`，字段已经在状态载荷里。

---

## 6. 共享项与权限

### 6.1 结构

协议声明（签名、可发给成员）：

```json
{
  "node":     "<设备公钥>",
  "owner":    "<属主主体公钥>",
  "share_id": "<该设备内的共享标识>",
  "seq":      3,
  "acl": {
    "read":  "group",
    "write": ["<主体公钥>"]
  },
  "sig": "<设备私钥签名>"
}
```

`read` / `write` 取值均为 `"owner"`、`"group"` 或主体公钥数组。
**`path` 不在签名声明里**：把宿主机目录结构发给全体成员等于免费泄露；本机路径
只存在 `identity/share_roots.json`，与声明在 `LocalShare` 里合成，不参与签名也
不对外发送。

### 6.2 权限档位

只有两档：

| 档位 | `read` | `write` |
| --- | --- | --- |
| 只读 | 授予对象 | `"owner"`（除属主外无人可写） |
| 可上传 | 授予对象 | 授予对象 |

`write: "owner"` 的语义是"除属主外无人可写"；属主在自己机器上写入不经过协议。
**写权限的唯一语义是"只能新增"**（可加性）：

- 共享项内容只增不减；磁盘边界不靠协议侧容量上限（默认不限制，且实测挡不住
  并发），"不要给不信任的人 write 权限"是唯一有效边界；
- 授权判定只看 ACL，**不记录 `created_by`**：写权限是"能不能新增"，与"谁传的"无关。

### 6.3 判定位置

判定在**属主设备**上执行。客户端隐藏按钮不构成授权；判定依据是发起方主体的
公钥，该公钥来自握手，不接受请求参数中的自称身份（`node.py` 的
`_authorizer()` 用 `peer.principal_key` 与名单构造 `Authorizer`）。

**本机 HTTP 侧没有主体**：`/api` 已有主体上下文，但 `/file`、`/thumbs` 仍只做
令牌 + 路径安全校验，没有按主体判权。因此插件里所有涉及主体的判定都以**本机
主体**为准（例如 `add_member()` 用本机主体私钥签名，再由名单规则复核角色），
"本机身份即操作者"是当前的权宜之计。物化到本机的远端字节落在本机 HTTP 数据
路由上，**任何持令牌者都能按路径读走**；在 §12 补齐前，物化缓存属于"本机可信域"，
不应被当作跨主体隔离来依赖。

### 6.4 写入语义（上传）

上传一个文件分五步：

1. **定位**：发起方给出共享标识与相对路径，对端解析成本机绝对路径，越界与
   符号链接逃逸一律拒绝（§6.5）；
2. **探询**：先问对端"这个目标已收下多少字节"（`probe`，走 write 权限——
   上传方可能只有写权限），据此决定续传起点；
3. **暂存**：分块写进 `<目标>.part`，不碰目标文件；每个分块带"已收下的字节数"，
   对端核对（不符回 `offset_mismatch`）；
4. **提交**：最后一块带 `eof`，对端才把 `.part` 原子改名成目标文件；
5. **覆盖策略**：目标已存在时默认拒绝，只有显式 `overwrite=true` 才替换。

界面必须提供**进度**（`upload_remote` 立即返回 `task_id`，轮询 `upload_status`）
与**取消**（`cancel_upload` 在分块边界生效；取消不清除对端已收字节——它就是下次
续传起点，协议里没有远程删除）。续传正确性的三条缺一不可：本机有该目标的记录、
记录与**当前**本地文件的 (大小, mtime) 一致、对端暂存字节数与记录一致。

由此产生的行为：断线只会在属主磁盘留下 `<目标>.part`；容量上限默认不设
（每条连接的基线只量一次，实测 8 条并发连接能在"上限 1000 字节"的共享项上写进
3200 字节且全部成功，因此不做成设置项）；落点只由属主设备决定，不提供远程
重命名与移动。

上传任务表落盘 `upload-tasks.json`：插件重载（改设置、升级、壳重启）会丢内存
线程，启动时把未结束任务标为 `interrupted`，界面提示"上次传到哪、同一文件再传
会续传"。

### 6.5 安全边界

共享面是数据投放面：被授予写权限的主体可以在属主目录内创建任意文件。硬约束：

| 约束 | 位置 |
| --- | --- |
| 越界与符号链接逃逸 | `node.resolve_in_share()`：拒绝 `..`、绝对路径，`realpath` + `commonpath` 复核 |
| 容量上限 | `node._op_write()` 写入前按投影占用判定；默认 `DEFAULT_MAX_BYTES = None`（不限制），**显式设置也挡不住并发** |
| 共享根与程序/身份目录分离 | 插件 `add_share()` 拒绝 data_root 与身份目录之内的路径 |
| 无重命名/移动 | 协议 op 表里没有这类操作 |
| 单次写入大小 | `MAX_WRITE_BYTES = 8 MiB`（配合 256 KiB 正常分块，主要防超大单帧） |
| 目录枚举上限 | `MAX_LIST_ENTRIES = 2000`，避免一次请求拉回整棵树 |
| 身份目录不出去 | `get_protected_paths()` 把 `identity/` 整个申报为受保护 |

**写权限是可加的**，因此"给不可信的人 write"等于允许他占满属主磁盘；
容量上限的判定是事后近似，挡不住并发、别的进程写入或先探询再分块。

---

## 7. 注册与发现

### 7.1 注册记录

```json
{
  "device":    "<设备公钥>",
  "seq":       7,
  "endpoints": [["240e:xxxx::1", 18443]],
  "shares":    ["<共享标识>"],
  "ts":        1767225600,
  "sig":       "<设备私钥签名>"
}
```

`registry.py` 的 `Registration`；单个记录端点上限 16 项。注册表按设备逐条比对
`seq`（严格大于才采纳，防回滚与重放）。

### 7.2 与名单解耦

| | 签名者 | 变更频率 | 内容 |
| --- | --- | --- | --- |
| 团体名单 | 群主/管理员 | 低频（成员增删） | 主体、角色、设备公钥 |
| 注册记录 | 设备自己 | 高频（开关机、地址变化） | 设备公钥、端点、共享清单、序号 |

注册是**成员自助**行为，不需要管理员参与。插件在节点启动后自动发布注册记录，
端点集合变化时递增 `seq` 重发。

### 7.3 同步方式

注册记录是小数据集（200 台设备约 20–40 KB），因此不使用 DHT：

- 任一在线共享节点都能提供完整快照；
- 新上线设备拉一次快照，之后按 `seq` 比对合并；
- 记录带设备自身签名，中继方无法伪造；
- **后台同步**：插件加载后每 `sync_interval_seconds` 拉一次；本机注册记录
  （端点或共享清单）变化时立即 `op=registry push` 整表快照给已知对端，
  对端按 `seq` 合并。共享项增删也会立刻重发自己的注册记录（`add_share` /
  `remove_share` → `_republish_registration`），不再依赖"下次刷新顺手带"。

当前实现是**整表快照 + 本地按 seq 去重**：`op=registry` 的 `list`/`push` 传输
整张表，没有 `since` / `from_seq` 游标。按 §7.5 的规模估算这不成问题，但措辞
不应说成"请求侧增量"。

### 7.4 地址变化与稳定地址

- 监听与注册应使用**稳定地址**；当前尚未实现临时地址识别（§4.6.1）；
- 定期比对本机全球单播地址集合，变化则递增 `seq` 并重新发布（已实现）；
- Windows 防火墙默认阻止入站，需要为监听端口加一次规则（需管理员权限）；
  插件只回报失败原因，不代替用户提权、不代加规则；
- 首次发现没有自动途径：注册表为空时用户必须手工登记一个对端地址
  （`peers add`）或带外交换地址串（`my_endpoint`），之后靠注册表扩散。

### 7.5 容量核算

| 对象 | 规模估算 | 结论 |
| --- | --- | --- |
| 成员记录 | 50 人约 4 KB | 无压力 |
| 设备记录 | 200 台约 20 KB | 无压力 |
| 名单全量 JSON | 约 25 KB | 无压力 |
| 一次成员变更的全量分发 | 25 KB × 200 节点约 5 MB | 可忽略 |
| 全互联连接 | 200 台设备 19900 对 | **不可行**，以星形为主、成员间按需直连 |

---

## 8. 房间（不在本插件范围内）

房间（联机、语音的临时集合）由未来的 Companion 子插件实现：核心插件只提供
身份、名单、注册与加密传输通道。草案见
[group-mesh 的 Companion 子插件](./group-mesh-companions.md) §1。

## 9. 游戏联机引擎（不在本插件范围内）

老游戏的二层虚拟局域网同样由子插件承担（SoftEther 编排、虚拟网卡、实测项），
草案见 [Companion 子插件](./group-mesh-companions.md) §2。

---

## 10. 传输与内容寻址

### 10.1 分块与寻址

当前实现是 **offset/length 分块**（默认 256 KiB）+ 远端物化（§15.1），
不是内容寻址。目标形态是：文件切分为固定大小的块，每块以哈希标识，接收方可从
任意持有者获取任意块，并复用 §7 的注册表作为来源目录。当前不存在"任意持有者
提供任意块"与"做种"。

### 10.2 做种

目标形态下任何成员可以把已获取内容保留并继续提供，用于实现"只要有一台机器
没关机，资源仍可访问"；默认行为是内容仅由属主设备提供，属主离线即不可访问。
**当前未实现**。

### 10.3 与 BitTorrent 的差异

不集成完整 BitTorrent：peer 发现已由 §7 提供，tracker 与 DHT 属重复建设；
引入 `libtorrent` 还会带来打包体积与 `HIDDEN_IMPORTS` 的额外成本。
这些理由只解释"为什么不选 BitTorrent"，不代表内容寻址已经实现。

---

## 11. 客户端形态

### 11.1 完整节点

Windows 与 Linux 上运行完整的 OmniBox 实例，本身即"本地网关 + 节点"，
不需要额外组件。**当前已实现**（插件层）。

### 11.2 轻客户端（Android，预留）

| 角色 | 能力 | 平台 |
| --- | --- | --- |
| 完整节点 | 持有名单，可作为共享节点提供服务 | Windows、Linux |
| 轻客户端 | 只有身份密钥；可读取共享内容、传输文件 | Android（首版不实现） |

首版只需在协议中预留该角色：角色字段 + 能力声明，不预留具体接口。
**当前未实现**：注册记录、共享项声明与握手 hello 都没有角色或能力字段
（`transport.py` 里的 `role` 是名单角色 owner/admin/member，不是节点形态）。

### 11.3 本地网关

轻客户端的形态是在客户端内部运行一个仅监听 `127.0.0.1` 的 HTTP 服务，WebView
加载 `http://127.0.0.1:<端口>/`，页面内的 `/api`、`/file`、`/thumbs` 由网关经
加密通道转发。三点作用：环回地址属于 potentially trustworthy origin（WebView
无需证书即为安全上下文，麦克风可用）；前缀变化、证书重签、`iPAddress` 匹配均
不涉及；不存在第二套账号。代价是网关必须正确转发 Range 请求，否则媒体播放退化
为整文件下载；Android 明文流量策略需按目标版本确认。
**当前未实现**：壳自己的 `/file` 支持 Range 与按需取字节，但那是桌面端本机
Flask 服务，不是"WebView → 127.0.0.1 网关 → 加密通道"这一层。

---

## 12. 壳侧配套改造

本插件依赖下列壳侧能力，插件层无法单独完成。当前状态：

| # | 项 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | 调用者身份 | **[已实现]** | `shell/backend/principal.py` 维护 `principals.json`（凭据只存 SHA-256）；`file_server` 的 `before_request` 按凭据解析主体；凭据表为空时把既有全局令牌自举为 owner，老部署不受影响 |
| 2 | 插件读取主体 | **[已实现]** | `PluginBase.current_principal()` / `require_principal()`；壳在鉴权通过后写入 `ContextVar`，插件无法从请求参数影响它；后台线程读不到主体 |
| 3 | 数据路由授权 | **[未实现]** | `/file`、`/thumbs` 仍只做令牌 + 路径安全 + 受保护判定，没有主体级检查点；物化缓存因此对本机持令牌者全开 |
| 4 | 文件根支持远端来源 | **[部分 / 旁路达成]** | `get_file_roots()` 仍只返回本机路径；等价能力由 `ensure_file()` 钩子 + 远端物化 + 网络位置扩展达成（§15.1、§15.3） |
| 5 | 设置写入限权 | **[已实现]** | `system_settings_save` / `system_get_config` / `system_get_plugin_status` 限 owner 与 admin（403），普通成员仍可调插件 API |
| 6 | `minShellVersion` 运行时校验 | **[未实现 / 决定不做]** | 当前只由 `tools/check_plugins.py` 门禁校验格式；壳加载时不比较版本。当前只支持与最新壳配套发布，因此不引入运行时拒绝逻辑 |

**第 2 项的注入约束**：插件自起的后台线程不继承请求上下文，涉及主体的后台任务
必须显式携带主体信息（`with shell.backend.principal.use_principal(p):`）。

**插件侧限权**：壳**刻意不把插件名硬编码**进 `_ADMIN_ONLY_API`，需要限权的
插件方法应自行调用 `PluginBase.require_principal()` 并判角色。group-mesh 的
`add_member` / `add_share` / `start_node` / `stop_node` / `upload_remote` /
`clear_remote_cache` 等有明确副作用的方法**目前尚未这样判**：它们仍只受
"有效令牌"保护，任何持令牌者都能调。这是 §3 主体上下文落地后剩下的插件层
收尾项，已在实现路径文档中列为待办。

第 1/2/5 项的验收用例见 `tests/test_shell_principal.py`，其中包含**插件层**的
验收：真实壳加载真实插件、经 `/api/<插件>__<方法>` 调用，伪造 `principal`
字段影响不了 `PluginBase.current_principal()`。

---

## 13. 默认参数

设计确定的参数：

| # | 参数 | 取值 | 位置 |
| --- | --- | --- | --- |
| 1 | Noise 模式与套件 | `XX`；`25519 / ChaCha20-Poly1305 / BLAKE2s` | `noise.py`、`shell/groupmesh/__init__.py` |
| 2 | 共享项容量上限 | 默认**不限制**（`DEFAULT_MAX_BYTES = None`）；不做成界面设置项 | `shares.py` |
| 3 | 轻客户端协议预留 | 角色字段 + 能力声明（未实现） | — |

实现中确定下来的默认值（便于排障与联调）：

| # | 参数 | 取值 | 位置 |
| --- | --- | --- | --- |
| 4 | 协议主版本 | `PROTO_VERSION = 2` | `shell/groupmesh/__init__.py` |
| 5 | 监听端口 / 绑定地址 | `19443` / `::`（同时接受 IPv6 与 IPv4）；`0` = 由内核分配 | `plugins/group-mesh/backend/main.py` |
| 6 | 单次写入上限 | 8 MiB | `node.py` 的 `MAX_WRITE_BYTES` |
| 7 | 目录枚举上限 | 2000 项 | `node.py` 的 `MAX_LIST_ENTRIES` |
| 8 | 读写分块大小 / 帧上限 | 256 KiB / 4 MiB | `transport.py` 的 `DEFAULT_CHUNK_BYTES` / `MAX_FRAME_BYTES` |
| 9 | 端点探测超时 / 整次刷新预算 | 3.0 s / 4.0 s，并行探测取首个成功结果 | `main.py` 的 `PROBE_*` |
| 10 | 连接超时 | 服务端 30 s（每条连接独立线程）；客户端 socket 20 s | `node.py` / `transport.py` |
| 11 | 自动启动失败冷却 | 30 s 后重试，不永久放弃 | `main.py` 的 `AUTO_START_RETRY_SECONDS` |
| 12 | 远端物化遍历上限 | 深度 4、条目 5000；达到条目上限时标记 `truncated` 并**跳过对账** | `main.py` 的 `materialize_remote()` |
| 13 | 单文件按需取回上限 | 1024 MiB（`0` = 不限制，等于放弃防线） | `main.py` 的 `max_fetch_mb` |
| 14 | 上传任务保留条数 | 最近 20 个（`upload-tasks.json`） | `main.py` 的 `UPLOAD_KEEP` |
| 15 | 上传进度落盘间隔 | 每 4 MiB | `main.py` 的 `_upload_worker` |
| 16 | 名单历史保留数 | 12 份 | `roster_history.py` 的 `DEFAULT_KEEP` |
| 17 | 单条注册记录端点数 | 16 | `registry.py` 的 `MAX_ENDPOINTS` |
| 18 | Noise 实现 | `noiseprotocol==0.3.1`；官方 cacophony 向量逐字节验收 | `noise.py`、`requirements.txt` |
| 19 | 私钥保护 | DPAPI / keyring / `OMNIBOX_SECRET_KEY` 口令 AES-GCM；都不可用时回落明文并在状态页标红 | `shell/groupmesh/secret_store.py` |
| 20 | 后台同步间隔 | 默认 60 秒（设置项 `sync_interval_seconds`，范围 5–3600）；变更时立即唤醒一次 | `plugins/group-mesh/backend/main.py` |

---

## 14. 待实测项

| # | 事项 | 影响 | 状态 |
| --- | --- | --- | --- |
| 1 | 运营商是否放行入站高位端口 | 决定共享节点能否被公网直连（§1.1 前提） | **未验证**：同网段 IPv6 直连已实测，跨公网未验证 |
| 2 | 前缀租期与 PPPoE 重拨后的地址变化 | 决定注册记录刷新频率 | **未实测** |
| 3 | Windows 稳定地址与临时地址的实际行为 | 决定监听与注册用哪个地址（§4.6.1） | **部分实测**：已记录本机由 RA 下发两个全局 IPv6、Radmin VPN 地址可用；"哪个是 RFC 4941"仍未确认 |
| 4 | Android 明文流量策略对 `127.0.0.1` 的约束 | 决定轻客户端网关实现形式（§11.3） | **未实测** |
| 5 | 端点探测耗时与并行收益 | 决定显式刷新与后台同步一轮的耗时 | **已实测**：串行 21.1 s / 并行等待全部 6.0 s / 存在可达端点 0.05 s |

SoftEther 相关实测项属于子插件，见
[Companion 子插件](./group-mesh-companions.md) §4。

---

## 15. 已落地但原设计未描述的能力

本节记录实现过程中长出来、v0.1 设计里没有的能力；它们已实现并有测试，
必须写进文档，否则读代码的人会以为跑偏。

### 15.1 远端共享物化与按需取字节

- **物化**（`materialize_remote`）：在本地 `.cache/remote/<真实设备ID>/<共享标识>/`
  造出与远端同形的目录树：目录直接建，文件先落 **0 字节占位**并写入**对端**的
  `mtime`，同时写一份 `<共享标识>.index.json`（记录 `size` / `mtime_ns`）。
- **按需取字节**：读文件就是普通 HTTP 请求。壳的 `/file` 在根校验**之后**回调
  `PluginBase.is_content_placeholder()` / `ensure_file()`，由插件把字节取回本地；
  暂存走 `.cache/staging`，与被替换目标同卷，`os.replace` 原子落位。
- **对账**：重新物化时，对端删掉/改名的条目在本地删除；对端内容变化（`size` 或
  `mtime_ns` 任一不同）且本地已持有真字节时，丢掉本地字节退回占位。**达到条目
  上限（`truncated`）时绝不对账**——那时遍历结果只是子集。
- **为什么这样设计**：消费型插件（图片相册、播放器）按本地路径工作，物化成真实
  本地目录后它们无需理解协议。代价是：要解码源文件的消费方与 0 字节占位不相容，
  必须先用"网络位置"把它拉成真字节（§15.3）。

### 15.2 一次性镜像

`mirror_share` 把远端共享项的内容**取回真字节**并落到用户指定的本地目录
（与物化相反：物化只落目录结构与占位）。用于"我要一份离线拷贝"，也是
"网络位置"的实现手段。幂等：大小与 mtime 都一致的文件直接跳过。

### 15.3 网络位置来源（壳共享组件契约）

设置面板里的目录选择器支持 **`placement: network-location`** 的插件扩展：
提供方声明 `embedUrl`，宿主用 `postMessage` 回填一个**本地目录**。group-mesh 是
参考实现（`get_extensions()` + `frontend/network-location.html`）：用户在消费方
插件的设置里选"网络位置"，即把远端的某个共享项用 `mirror_share` 取回到本地一个
目录，再把那个**本地目录**作为消费方的根。契约见 `docs/plugin-guide.md`。
注意这**不是** §12 第 4 项所说的"`get_file_roots()` 支持远端来源"：壳的文件路由
始终只服务本机路径，两种方案并存时以"物化/镜像成本地"为准。

### 15.4 地址交换

界面可直接复制本机地址（`[2409:…]:19443` 形态），也可粘贴对方地址按设备更新
端点（手工端点登记作为发现失败时的兜底）；端点探测并行化并有整次预算。
`peers` 支持"按地址登记"与"按设备更新"两种高级入口。手工登记是首次发现的
必需起点（§7.4）。

### 15.5 节点自启与注册发布

插件加载后经 `get_status()` 自动启动共享节点并发布注册记录；监听成功**之后**
才发布（用真正绑上的端口，设置里写 `0` 时由内核分配）；启动失败按 30 s 冷却重试
而不是永久放弃；名单在**每条连接**上重读（`roster_loader`），因此群主在别处加人后
运行中的节点立刻认。

同时启动**后台同步循环**（设置项 `sync_interval_seconds`，默认 60 秒）：定时拉
注册表与名单，并在本机名单/注册记录变化时立即 `push` 给已知对端。界面不再有
"刷新设备"按钮，`remote.js` 只每 15 秒把后端同步好的状态读回（页面隐藏时暂停），
因此不会有前端触发的网络探测。

### 15.6 验证设施

- 多实例夹具（`tests/harness/`、`tests/test_multi_instance_fixture.py`）：一个进程内
  跑多个真实插件实例，用于物化/网络位置/取字节/上传的端到端验证；
- 前端三层用例：纯渲染（桩 Bridge + 无头浏览器）、真实壳（起服务 + 导航 + iframe）、
  后端 API；
- 非无头演示脚本（`tests/debug_*.py`）：逐步截图，回答"肉眼才能回答"的问题；
  `debug_connection.py` 专做连接与共享访问（两台实例、程序化登记地址，验证发现、
  列目录、取回与上传，并用 sha256 对比给结论）；
- Noise 官方向量：`tests/test_group_mesh_noise_vectors.py` 驱动
  `tests/fixtures/noise_XX_25519_ChaChaPoly_BLAKE2s.json`（cacophony 向量），
  逐字节校验握手、`handshake_hash` 与传输消息，并覆盖"与原生
  `noiseprotocol` 对端互通"；
- 私钥保护：`tests/test_group_mesh_secrets.py` 覆盖 DPAPI / keyring 替身 /
  口令四种 protector 的往返、篡改拒绝、旧明文单向升级与"不降级重写"。

完整清单与每条命令覆盖什么，见
[实现路径与现状](./group-mesh-implementation-path.md) 的"测试工具"一节。

---

## 16. 实施路线

按**依赖顺序**排列，详细进度、验收与当前阻塞见
[实现路径与现状](./group-mesh-implementation-path.md)。摘要：

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 协议内核（身份/名单/共享/注册/Noise/传输/节点）+ 插件骨架 + 跨机验证 | **已完成** |
| P1 | 壳侧主体上下文、权限档位落地、设置写入限权 | **大部分完成**：凭据→主体、ContextVar、设置写入限权已完成；`/file`、`/thumbs` 主体检查点未做；`minShellVersion` 运行时校验决定不做 |
| P2 | 私钥保护、Noise 换 vetted 实现 + 官方向量、Rekey、解散通告、设备授权/多设备、稳定地址 | **大部分完成**：私钥保护（DPAPI/keyring/口令）与 Noise vetted + 官方向量已完成；Rekey、解散、设备授权、稳定地址待做（名单自动分发已完成） |
| P2.5 | 共享面收尾：上传入口/进度/取消/续传已完成；遗留 `.part` 可见性待做；`add_device()` 暴露待做 | **大部分完成** |
| P3 | 房间/语音/游戏面 Companion 子插件 | 由子插件承担，不在本插件路线 |
| P4 | Android 轻客户端 | 仅预留设计位 |

---

## 17. 尚未回答的问题

完整表与影响见[实现路径与现状](./group-mesh-implementation-path.md) 的"未回答问题"。
当前最关键的四条：

1. **跨公网**是否真的可达？同网段 IPv6 已实测，运营商是否放行入站高位端口未验证；
2. Windows 上如何区分稳定地址与 RFC 4941 临时地址？（§4.6.1）
3. 群主私钥丢失后团体永久不可管理，是否需要"名单备份 + 冷存储"操作指引？（§5.6）
4. 取消/中断留下的 `<目标>.part` 是否需要属主端可见（列出未完成暂存与占用）？（§6.4）
