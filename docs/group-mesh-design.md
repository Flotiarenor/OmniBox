# 团体组网插件（group-mesh）设计

> 文档性质：**当前实现说明**（与 `media-player-design.md`、`image-viewer-design.md`
> 同一体例），只写"现在是什么样"，不写版本变更流水。
> 实现进度、已知漏洞、易错项与测试工具见 §19–§26（「实现路径与现状」）。
> 定位：**核心插件**（供消费型 Companion 插件依赖）+ 壳侧配套改造。
> 代码位置：协议内核 `shell/groupmesh/`，插件 `plugins/group-mesh/`。
> 关联文档：§29 Companion 子插件草案、
> [插件开发指南](./plugin-guide.md)、[主程序方向](./core-direction.md)。
>
> **章节号约定**：`shell/groupmesh/**` 与 `plugins/group-mesh/backend/main.py`
> 的注释直接引用本文的 §1.3 / §3.3 / §4.x / §5.x / §6.x / §7.x / §10 / §11.2 /
> §12 / §13，以及 §21.x（偏离与风险）与 §22.x（实现踩坑）。整理文档时保持
> §1–§17 与 §19–§29 的编号与语义不变，避免代码注释指错位置。

---

## 0. 阅读指引与实现概览

### 0.1 这份文档回答什么

本文回答"group-mesh 现在由哪些部件组成、各自怎么工作、边界在哪"。它不回答
"下一步先做什么"——那在 §19–§26；
也不回答"踩过哪些坑"——同样在 §22 里按现象逐条列出。

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
| 房间 / 语音 / 游戏面 | **不在本插件范围** | §29 Companion 草案 |

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
  §22.7（前端 UI 改造：面板形态、级联优先级与两个测试坑）。
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
  被拒的发起方会以为连接成功（详见 §22.4）。

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
   拒绝，已记录在 §22.1–§22.3。
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
§29 Companion 子插件 §1。

## 9. 游戏联机引擎（不在本插件范围内）

老游戏的二层虚拟局域网同样由子插件承担（SoftEther 编排、虚拟网卡、实测项），
草案见 §29 §2。

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
| 5 | 壳端点限权 | **[已实现]** | `system_get_config` / `system_get_plugin_status` 与壳自身运维端点（日志级别、清空缩略图缓存、打开日志目录）限 owner 与 admin（403），普通成员仍可调插件 API；插件设置由 `<插件>__save_settings` 写入，是否限权由插件自行判定 |
| 6 | `minShellVersion` 运行时校验 | **[未实现 / 决定不做]** | 当前只由 `tools/check_plugins.py` 门禁校验格式；壳加载时不比较版本。当前只支持与最新壳配套发布，因此不引入运行时拒绝逻辑 |

**第 2 项的注入约束**：插件自起的后台线程不继承请求上下文，涉及主体的后台任务
必须显式携带主体信息（`with shell.backend.principal.use_principal(p):`）。

**插件侧限权**：壳**刻意不把插件名硬编码**进 `_ADMIN_ONLY_API`，需要限权的
插件方法应自行调用 `PluginBase.require_principal()` 并判角色。group-mesh 的
`add_member` / `add_share` / `start_node` / `stop_node` / `upload_remote` /
`clear_remote_cache` 等有明确副作用的方法**目前尚未这样判**：它们仍只受
"有效令牌"保护，任何持令牌者都能调。这是 §3 主体上下文落地后剩下的插件层
收尾项，已在 §21.7 列为待办。

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
§29 §4。

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
§23 的"测试工具"一节。

---

## 16. 实施路线

按**依赖顺序**排列，详细进度、验收与当前阻塞见
§19–§26。摘要：

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

完整表与影响见 §25 的"未回答问题"。
当前最关键的四条：

1. **跨公网**是否真的可达？同网段 IPv6 已实测，运营商是否放行入站高位端口未验证；
2. Windows 上如何区分稳定地址与 RFC 4941 临时地址？（§21.6.1）
3. 群主私钥丢失后团体永久不可管理，是否需要"名单备份 + 冷存储"操作指引？（§22.6）
4. 取消/中断留下的 `<目标>.part` 是否需要属主端可见（列出未完成暂存与占用）？（§22.11）

---

## 18. 章节索引

本文由原三份文档合并而成，章节号分三片，**编号刻意保持稳定**，因为
`shell/groupmesh/**` 与 `plugins/group-mesh/**` 的注释直接按号引用。

| 章节 | 内容 | 来源 |
| --- | --- | --- |
| §0 | 阅读指引与实现概览 | 本文 |
| §1–§13 | 设计：定位、术语、形态、身份、名单、共享项、发现、传输、客户端、壳侧改造、默认参数 | 本文 |
| §14–§17 | 边界：待实测项、已落地能力、实施路线、尚未回答的问题 | 本文 |
| §19–§26 | 实现路径与现状：进度、已知偏离与风险、**实现中踩到的坑**、测试工具、下一步、未决问题、结论摘要 | 原 `group-mesh-implementation-path.md` |
| §28 | UI 现状取证（前端与壳契约对照） | 原逐插件 UI 审计 |
| §29 | Companion 子插件（房间 / 语音 / 游戏面）设计草案 | 原 `group-mesh-companions.md` |

**为什么有断号**：§18（本索引）与 §27 是合并时预留的空号 —— 把实现路径平移到
`§19` 之后、把 UI 取证放到末尾 `§28`，就无需改动代码注释里已有的
`§21.x` / `§22.x` 引用。**引用本文时请按上表，不要按顺序猜号。**

### 18.1 按目的找章节

- "这个插件现在是什么样" → §1–§13；
- "做到哪了、还差什么" → §19、§20；
- "哪里有风险、怎么收敛" → §21；
- "踩过哪些坑（改代码前必读）" → §22；
- "怎么验证" → §23；
- "前端与壳契约的偏差" → §28；
- "房间 / 语音 / 游戏面怎么规划" → §29。

---

## 19. 实现路径与现状（进度 / 漏洞 / 易错项 / 测试工具）

> 本章与 §21–§26 原为独立文档 `docs/group-mesh-implementation-path.md`，已并入本文；
> 日期 2026-09-17，事实基线 `feat/group-mesh` @ `7c2110e` + 本轮工作区改动。
> 定位：**进度、漏洞、易错项、测试工具**。§0–§17 说明"现在是什么样"，
> 本章起说明"做到哪了、哪里危险、怎么验证、下一步做什么"。
>
> **编号约定**：本章起的编号由原文档平移而来（`2`→`20`、`3`→`20.2`、
> `4.x`→`21.x`、`5.x`→`22.x`），因为本文 §4 / §5 已分别用于「身份与密钥」
> 「团体名单」。本章内出现的裸 `§4.x` / `§5.x` 一律指向平移后的 `21.x` / `22.x`。

### 19.1 这份文档要解决什么

设计文档定义"要做成什么样"。本文补三件事：

1. **做到哪了**：按 P0–P4 列出已完成、部分完成、明确不做、未开始；
2. **哪里有危险**：实现与设计之间的偏离、安全漏洞、稳健性缺口，逐条给等级与收敛路径；
3. **怎么验证**：每条命令覆盖什么、需要什么前置条件、哪些是门禁哪些是演示。

一句话概括：**协议内核（身份/名单/共享/注册/Noise/传输/节点）已可运行并在
Windows↔Linux 跨机验证；插件层已接通双向传输、物化与网络位置；主体上下文
壳侧已落地。本轮已收敛"私钥明文落盘"、"Noise 自写且无官方向量"、"手动刷新设备
（改为定时轮询 + 变更时 push）"三项。按当前决定，下一步暂不做 HTTP / 插件层的
按主体限权（§21.8、§21.9），优先处理 Rekey、连接上限、解散/设备授权/稳定地址等
剩余项。**

---

## 20. 分层与阶段

### 20.1 分层与边界

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

### 20.2 阶段划分与当前进度

每个阶段以**可复核的命令**收尾，不用"看起来能跑"作为完成标准。

#### P0 —— 协议内核与跨机验证（已完成）

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

#### P1 —— 壳侧主体上下文与权限档位（大部分完成）

| # | 项 | 状态 |
| --- | --- | --- |
| 1 | 凭据到主体的映射（`principals.json`，只存 SHA-256） | **已完成** |
| 2 | `ContextVar` 注入 + `PluginBase.current_principal()` / `require_principal()` | **已完成**（含插件层验收） |
| 3 | `/file`、`/thumbs` 主体级检查点 | **待做**（见 §21.8） |
| 4 | 权限档位落地（只保留 `read` / `write` + 上传） | **已完成** |
| 5 | 设置写入限权（`system_*` 限 owner/admin） | **已完成** |
| 6 | `minShellVersion` 运行时校验 | **决定不做**（当前只支持与最新壳配套；门禁仍校验格式，见 §21.12） |

向后兼容：凭据表为空时，壳把既有 `auth_token.txt` 自举成 owner，老部署升级后
令牌继续可用。验收用例见 `tests/test_shell_principal.py`，其中
`PluginPrincipalInjectionTest` 走真实 `/api/<插件>__<方法>` 通路，断言请求体里的
`principal` / `principal_id` / `role` 字段影响不了 `current_principal()`。

#### P2 —— 把偏离收敛掉（上生产前必须）

| 优先级 | 项 | 状态 |
| --- | --- | --- |
| 高 | 私钥改 DPAPI / keyring / 口令 AES-GCM（设计 §4.1） | **已完成**（`secret_store.py`；三者都不可用时如实回落明文并标红，见 §21.1） |
| 高 | Noise 换 vetted 实现 + 官方握手向量 | **已完成**（`noiseprotocol` + cacophony 向量，见 §21.2） |
| 中 | 长连接 Rekey（设计 §4.4） | **待做**（§21.3；库已提供 CipherState.rekey，缺应用层协商） |
| 中 | 设备授权（配对/二维码）与凭据进名单（设计 §4.2） | **待做** |
| 中 | 解散通告（设计 §5.5） | **待做** |
| 低 | 注册表 `since` 游标 | **待做**（当前整表快照 + seq 合并） |
| 低 | 稳定地址判定（设计 §7.4） | **待做**（§21.6.1） |
| 低 | 群主转移/任免管理员进界面 + 使用说明 | **待做** |
| — | ~~名单自动分发~~ | **已完成**（`op=roster` + 名单历史） |
| — | ~~主动 push 新名单~~ | **已完成**：变更时 push + 后台定时轮询（间隔见设置 `sync_interval_seconds`） |
| — | ~~容量上限进设置面板~~ | **不做**：判定挡不住并发，做成设置项只会让人以为有防线（§21.11） |

#### P2.5 —— 共享面收尾

| 项 | 状态 |
| --- | --- |
| 上传入口 `upload_remote` + 界面 | **已完成** |
| 上传进度 / 取消 / 断点续传 | **已完成** |
| 上传任务表落盘、重载标记 interrupted | **已完成** |
| 遗留 `<目标>.part` 的可见性 | **待做**：取消/中断留下的暂存会一直占属主磁盘，协议里没有远程删除 |
| `add_device()` 暴露（多设备） | **待做**：内核已实现，CLI/插件都没有调用方 |

#### P3 —— 用途插件（房间 / 语音 / 游戏面）

不在本插件路线里，由 Companion 子插件承担；前置实测项见
§29 §4。

#### P4 —— Android 轻客户端

按设计 §11.2 只预留角色与能力字段，不预留接口；当前未开始。

#### 本轮基线验证（可复核，2026-09-17 实跑）

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

## 21. 已知偏离、漏洞与风险清单

**这一节是本文最重要的部分。** 先给等级汇总，再逐条展开。等级按"当前部署形态
（单机单使用者）下的实际影响"与"多使用者/公网部署下的影响"综合判断。

| # | 项 | 等级 | 一句话 | 收敛路径 |
| --- | --- | --- | --- | --- |
| 4.1 | 私钥保护 | **已收敛**（plain 回落为残余） | DPAPI / keyring / 口令 AES-GCM；旧明文单向升级 | headless 可设 `OMNIBOX_SECRET_KEY` |
| 4.2 | Noise 实现 | **已收敛**（无 Rekey、帧长超规范为残余） | `noiseprotocol` + cacophony 官方向量逐字节校验 | Rekey 见 §21.3 |
| 4.8 | `/file`、`/thumbs` 无主体级检查点 | **中高**（多使用者下高） | 物化缓存对任何持令牌者全开 | `authorize_file()` 钩子 |
| 4.9 | 插件有副作用的方法未按主体限权 | **中高** | 任持令牌者可加成员、上传任意本机文件（含 `.config` 凭据）、向任意目录写远端内容 | 插件调 `require_principal()` + 收窄上传来源 |
| 4.3 | 长连接无 Rekey | 中 | 单条连接的传输密钥泄露即可解全连接内容 | Noise §11.3 Rekey / 限时重握手 |
| 4.10 | 节点自动监听 + 每连接一线程，无连接上限 | 中 | 暴露面大、可被半开连接占用线程 | 连接上限 / 超时收紧 / 显式开关 |
| 4.6.1 | 临时地址也会进注册记录 | 中低 | 地址轮换后端点短暂失效 | `GetAdaptersAddresses(Temporary)` |
| 4.11 | 容量上限挡不住并发 | 低（已默认关闭） | 显式设置也超限 | 保持"默认不限制 + 不暴露设置" |
| 4.12 | `minShellVersion` 运行时不校验 | 低 | 门禁通过、运行时静默降级 | 已决定不做；靠版本纪律 |
| 4.13 | 显式下载 / 镜像无总量上限 | 低 | 用户主动操作可写满磁盘 | 交互确认 / 总量预估 |
| 4.7 | 解散、注册表游标、稳定地址等 | 功能缺口 | 见各条 | 按 P2 排期 |

### 21.1 私钥保护（已收敛，仍有 plain 回落）

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

### 21.2 Noise 实现（已收敛为 vetted 实现 + 官方向量）

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
    * 仍无 Rekey（§21.3）。
- 验收：上述向量用例 + `selftest` 的"Noise_XX 握手与传输态" + 既有 `NoiseXXTest`。

### 21.3 长连接没有密钥轮换（中）

- 现状：握手有前向安全（临时密钥），但长连接的传输密钥在整条连接生命周期内不变，
  也没有消息数上限（除 `2^64` nonce 上限；耗尽时 `noise.py` 直接报错终止，不是 Rekey）。
- 影响：单条超长连接泄露一把传输密钥，该连接内全部内容可解。
- 收敛：按消息数/字节数触发 Noise §11.3 的 `Rekey()`，或限制单连接时长后强制重握手。

### 21.4 写入语义：暂存 + 提交（已按设计实现）

这一条不是风险而是**已收敛的偏离**，留档说明曾经的危险写法：

- 曾经：`_op_write` 直接 `open(target, 'wb')` 覆写，一次断线就把对端已有文件截断成
  半份且无人报错。
- 现在：`part` / `eof` / `overwrite` 三字段；`client.push_file()` 一个文件走一条连接、
  一次基线计量；目标已存在默认拒绝，覆盖需显式 `overwrite`。
- 顺带修掉：配额每分块 `os.walk`（1 GiB / 256 KiB = 4096 次全树遍历，上传慢到不可用）；
  基线把正在写的 `.part` 重复计入。

### 21.5 身份签名与 Noise 静态密钥是两把密钥（必要的措辞修订）

- 设计 §4.3：同一把设备密钥同时用于握手与注册。
- 现状：设备持有 Ed25519 签名密钥 + 由同一份种子确定性派生的 X25519 握手密钥，
  握手时用**设备绑定证明**把两者绑在一起。从"账号数量"看仍是一套身份材料，
  但不满足"同一把密钥"的字面表述。
- 原因：Ed25519 私钥是种子经 SHA-512 派生的标量，不是合法 X25519 标量；
  直接当 DH 用会让双方算出不同共享密钥且都不报错（§22.2）。

### 21.6 IPv4 是可达性兜底，主路径仍是 IPv6（已实测）

- 实测条件：两端都有全局 IPv6。`ipv6-test.ps1` 完成纯 IPv6 联调：Linux 绑全局
  IPv6，Windows 用 `[2409:…]:port` 连接，700 KB 文件 sha256 两侧一致，越权与越界
  被拒，注册记录里写入的是该全局 IPv6 端点。
- IPv4 只是同一可达网段内的地址族兜底，不引入 NAT 穿透或中继，与设计 §1.3 的非目标
  不冲突。
- 仍然成立的边界：**跨公网**能否直连取决于运营商是否放行入站高位端口（§8 问题 1）。
  已验证的是"同网段、IPv6 地址族"，不等于"公网可达"。

### 21.6.1 自动地址选择会把临时地址也当成端点（待修）

设计 §7.4 要求监听与注册使用稳定地址，不使用 Windows 默认的 RFC 4941 临时地址。
`registry.local_addresses()` 做不到：Python 标准库拿不到"是否为临时地址"。
实测 Windows 同时存在稳定地址与临时地址，因此不显式 `--bind` 时两者都会写进
注册记录；临时地址轮换后该端点失效。`_refresh_own_registration()` 会在地址集合
变化时递增 `seq` 重发，代价是旧端点期间白试一次。

补充实测：本机由 RA 下发两个全局 IPv6，另有私有 v4 与一张 Radmin VPN 网卡
（地址对同为该 VPN 成员的设备可用）。因此**不能按网段筛端点**，可达性只能靠
连接侧逐个尝试。收敛路径：显式 `--bind`（当前规避手段）；Windows 用
`GetAdaptersAddresses` 的 `Temporary` 标志识别；或改用 RFC 7217 地址并只发布它。

### 21.7 未实现的次要项

| 项 | 设计 | 现状 |
| --- | --- | --- |
| 解散通告 | §5.5 | 未实现 |
| 内容寻址分块传输 / 做种 | §10 | 未实现（当前 offset/length 分块） |
| 注册表增量同步 | §7.3 | 只有整表 `list`/`push`，无 `since` |
| 稳定地址判定 | §7.4 | 见 §21.6.1 |
| 主动 push 新名单 | §5.7 | **已实现**：名单变更时 push + 后台定时轮询（`sync_interval_seconds`，默认 60 秒） |
| 群主转移/任免管理员进界面 | §5.6 | 只有 CLI |
| 设备配对/二维码、凭据进名单 | §4.2 | 未实现 |
| Android 轻客户端 / 本地网关 | §11.2 / §11.3 | 未实现 |

### 21.8 数据路由没有主体级检查点（中高；多使用者部署下为高）

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

### 21.9 插件有副作用的方法未按主体限权（中高）

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

### 21.10 节点自动监听 + 每连接一线程，无连接上限（中）

- 现状：身份与团体都存在时，插件在 `get_status()` 里自动启动节点，默认绑 `::`；
  服务端每条连接一个 daemon 线程（`_spawn_handler`），`listen(16)`，没有并发上限；
  半开连接可占用线程最多 30 s（`CONNECTION_TIMEOUT_SECONDS`）。
- 影响：暴露面大（公网 IPv6 上任何能到达该端口的主机都可发起 Noise 握手）；
  大量半开连接可耗尽线程/文件描述符。防火墙默认会挡，但用户加过放行规则后就敞开。
- 收敛方向（未定）：引入可配置的连接数上限与握手超时收紧；提供"不自动启动节点"
  的显式开关；或要求用户显式确认监听地址。当前文档只如实标注，不假装已有防线。

### 21.11 共享项容量上限挡不住并发（已处置为默认不限制）

- 旧状：默认 1 GiB，前端还硬编码发送 `max_bytes: 1 GiB`，而设置面板没有该项。
- 实测：`node.py` 的判定是"**每条连接**量一次基线，之后按基线推算"。8 条并发连接在
  "上限 1000 字节"的共享项上各写 400 字节，全部成功，落盘 3200 字节。另有事后估算、
  别的进程写入、先探询再分块三处天然缺口。
- 处置：`DEFAULT_MAX_BYTES = None`；插件 `add_share()` 不再带 `max_bytes`
  （不传 / 0 / null = 不限制），界面不再发送硬编码值；判定逻辑保留，显式设置上限时
  仍生效。**不做界面设置项**：给不出可信保证的旋钮比没有旋钮更糟。
- 代价：被授予 `write` 的人可以写满属主磁盘。因此"不要给不信任的人 write 权限"是
  唯一有效边界（写权限是可加的，设计 §6.2）。

### 21.12 `minShellVersion` 运行时不校验（已决定不做）

- 现状：`tools/check_plugins.py` 校验格式，`manifest.json` 声明 `1.2.0`，壳加载时
  不比较版本。它是唯一"门禁通过、运行时静默降级"的项。
- 决定：当前只支持与最新壳配套发布，不引入运行时拒绝逻辑；版本纪律保留在
  `PROTO_VERSION`（协议主版本）上。若将来插件需要向后兼容旧壳，再引入运行时校验。

### 21.13 其它缺口与观察

- **显式下载 / 镜像无总量上限**：`download_remote` 与 `mirror_share` 由用户主动触发，
  但可以写满磁盘；`max_fetch_mb` 只约束"浏览时的按需取字节"，不约束这两个入口。
- **`download_dir` 与物化缓存的 HTTP 可见性**：默认下载目录经
  `/file?plugin=group-mesh` 可读，受 §21.8 的同一问题影响。
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

### 21.14 本轮分析发现并已处理/待清理的文档不一致

| 位置 | 原不一致 | 处理 |
| --- | --- | --- |
| `plugins/group-mesh/backend/main.py` 文件头 | 仍写"壳目前没有主体能力"，并称"当前状态（v0.1，骨架）" | **已改**：说明壳侧已落地、本插件尚未接入，指向本文 §21.9 |
| `shell/groupmesh/__init__.py` 文件头 | 把"壳侧主体上下文"列为不在本包范围，容易被误读成"壳还没有" | **已改**：说明它由 `shell/backend/principal.py` 提供，内核不依赖它 |
| `shell/groupmesh/registry.py` 注释 | 引用"设计文档 §4.6.1：IPv4 是可达性兜底"，旧设计文档根本没有 §4.6 | **已改**：引用 §4.6；§4.6.1 留给临时/稳定地址识别 |
| `docs/group-mesh-p1-review.md` | 内容已被本文收编，且"待做/已完成"与最新 HEAD 不一致 | **已改**：本轮已把该文档整体并入本文（§19–§26），独立文件删除 |
| `tests/__pycache__/test_multi_instance_mesh.cpython-312.pyc` | 无对应 `.py`（现名 `test_multi_instance_fixture.py`） | **待清理**：不影响 `unittest discover`，但会误导读仓库的人 |
| `plugins/group-mesh/backend/main.py` 的 `get_status()['unsupported']` | 只列"内容寻址 / Android" | 与事实一致；如要更完整可补"本地网关 / 轻客户端" |

---

## 22. 实现中踩到的坑（务必保留，避免重犯）

这些是真实调试中定位到的问题。共同特征：现象常是"AEAD 校验失败 / MAC check failed"，
根因却各不相同。握手失败时不要只看"密钥对不对"，按下面清单逐项排查。

### 22.1 Noise_XX 的 token 顺序与 nonce 延续（三处错误）

按规范 §22.3 的 `WriteMessage` 规则：**先顺序处理消息的所有 token，最后才追加一次
负载**。由此推出的三条约束，本实现最初全部搞错：

| # | 规则 | 错误写法 | 正确写法 |
| --- | --- | --- | --- |
| 1 | `es` 的第一个字母指**发起方**的密钥类型 | 发起方用 `DH(e, re)` | 发起方 `DH(e, rs)`；响应方 `DH(s, re)`，且在收到消息 3 时才结算 |
| 2 | 负载用最后一个 token 派生的 `k` | 把负载放在 `es`/`se` 之前 | `es`/`se` 的 `MixKey` 之后才加密负载 |
| 3 | 同一个 `k` 的 nonce 不因新消息重置 | 消息 3 的 `s` 从 nonce 0 开始 | 响应方消息 2 已用掉 nonce 0，消息 3 的 `s` 用 nonce 1 |

补充：`CipherState` 在 `k` 为空时 `EncryptWithAd` 原样返回明文并只做 `MixHash`
（XX 的消息 1），因此 `MixHash` 必须由 `HandshakeState` 负责，不能让
`CipherState` 去持有握手哈希。

### 22.2 Ed25519 密钥不能直接当 X25519 用（最危险的一个）

- 现象：双方各自算出的 `DH(e_i, s_r)` 与 `DH(s_r, e_i)` 不相等，两边都不报错，
  直到后面的 AEAD 才以 `MAC check failed` 暴露。
- 根因：Ed25519 私钥是种子经 SHA-512 派生的标量，不是 X25519 标量；原样喂给
  X25519 点乘得到的是一把无关的标量。
- 修正：由同一份身份种子确定性派生 X25519 密钥对
  （`X25519 私钥 = clamp(BLAKE2s(seed || label))`），并在握手中用设备绑定证明
  把 Ed25519 设备公钥与 X25519 握手公钥绑定，否则 `authorize_peer` 会拿 DH 公钥
  去名单里查，永远查不到。

### 22.3 pycryptodome 的两处编码陷阱

| 陷阱 | 表现 | 正确做法 |
| --- | --- | --- |
| `pointQ.x`、`export_key(format='raw')` 与 RFC 7748 三者互不相同 | 用 raw 导出公钥、按 `pointQ.x` 大端当线格式，会与外部实现不互通（本项目 v0.1 的实际缺陷） | 线格式一律取 RFC 7748 §5 的 32 字节小端；`EccXPoint` 要的整数按小端解读 |
| X25519 私钥不能手工转成整数做标量乘 | 普通 int 点乘与库自己的结果不同，两边都不报错 | 私钥一律经 `_dh_private_key()`（内部 `ECC.construct(curve='Curve25519', seed=...)`）还原 |
| Ed25519 的 `int(key.d)` 是派生标量不是种子 | 存 `d` 再重建会得到另一把密钥 | 持久化用 `key.seed`；重建用 `ECC.construct(curve='Ed25519', seed=...)` |

另有两处 API 事实：`ECC.import_key` 不接受 32 字节裸 Ed25519 公钥（需补固定 SPKI
头），也不接受裸种子；`EccXPoint(None, curve)` 是无穷远点而不是基点。

### 22.4 授权必须在握手之后单独确认

XX 的三条消息只完成"互相认证静态公钥"。响应方判定对端资格所需的最后一个输入
（发起方静态公钥与绑定负载）在**消息 3** 才到达，而发起方在收到消息 2 后就已经把
消息 3 发出去了。不加确认帧时，被拒的发起方会认为 `connect()` **成功**（实测现象），
直到后续请求失败或超时才发现。修正：会话建立后的第一帧由响应方回 `ok` 或
`deny:<原因>`，发起方据此决定是否返回连接。

### 22.5 跨机联调的环境坑（与协议无关，但会浪费大量时间）

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

### 22.6 长驻节点的名单必须在每次建连时重读

- 现象：先起 `serve`，再在另一处 `roster add`，新成员连接时被判
  "对端设备 … 不在团体名单里，拒绝连接"。
- 根因：`Node` 只在构造时拿到一份名单快照，而它是长驻进程；名单会变。
- 修正：`Node.current_roster()` 在每条连接上调用 `roster_loader` 重新读取；
  CLI 与插件分别传入"读 roster.json"与"读插件身份目录"的 loader。
  读盘失败退回内存副本，不让一次 IO 错误导致全部连接被拒。
- 附带结论：这让 `tools/ipv6-test.ps1` 的"先起节点、后加成员"顺序成为有效的
  回归用例。

### 22.7 CSS 级联废掉了 `hidden`（前端"一打开就弹添加成员"）

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

### 22.8 状态载荷缺键，导致按钮"点了没反应"

- 现象：「创建团体」点击后毫无反馈。
- 根因：`get_status` 的 `settings` 没给 `group_name`，前端读
  `status.settings.group_name`；缺失时 `undefined !== ''` 恒真，走进
  `window.confirm()`，而内嵌 WebView 里原生对话框可能被禁用。
- 修正：①「创建团体」改成应用内弹窗；②状态载荷补齐前端会读的键，且只给真正读的键。
- 教训：桩测试容易掩盖这类问题——用例对 `get_status` 回的是手写理想载荷，键当然是齐的。
  因此又加了 `tests/test_group_mesh_shell_e2e.py`：起真实壳服务、经导航进入插件
  iframe，用真实后端返回值验证渲染与调用。

### 22.9 前端验证的三个层次（缺一层就会漏）

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

### 22.10 名单只做了"第一次加入"，没做"分发更新"（成员停在旧名单）

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

### 22.11 上传的进度 / 取消 / 续传（五个坑）

| 坑 | 现象 | 处置 |
| --- | --- | --- |
| 复用连接被两个线程同时用 | 上传线程与界面线程共用复用池连接，两次 send 交错把帧拼坏（`MAC check failed` 或卡住） | 上传用**专用连接**（`open_connection`，用完即关），复用池只服务界面 |
| 同步请求挡住进度 | `/api` 是同步请求-应答，一次上传几分钟，占着请求既报不了进度也取消不了 | 后台线程 + `task_id`，界面轮询 `upload_status` |
| 重载让上传"凭空消失" | 任务表只在内存，改设置/升级/重启后界面什么都不剩，而对方 `.part` 还在 | 任务表落盘 `upload-tasks.json`，启动时把未结束任务标为 interrupted |
| 取消没有分块边界 | 在 `request()` 阻塞时才判取消，用户要等一整个分块甚至超时 | `push_file` 在每个分块前检查取消回调；socket 超时 30 秒兜底 |
| 续传起点不可信 | 只按"对端 `.part` 有多大"续传，会在内容已变的本地文件上拼出垃圾且无人报错 | 起点必须同时满足：本机有记录、记录与当前文件 (大小, mtime) 一致、对端暂存数等于记录值；每块带 `offset`，对端核不上回 `offset_mismatch` |

两条附带约束：进度回写要节流（每 4 MiB 落一次盘）；空文件与"整个文件已在对方
暂存里"是同一类，都要补一次提交（`push_file` 里用 `pushed` 标志判断）。

### 22.12 陈旧的 `dh_public` 让老身份目录升级即失败（已修）

- 现象：X25519 编码改成 RFC 7748 后，`Device.from_dict()` 把"落盘 `dh_public`
  与派生值不符"判为文件被改动并抛 `RecordError`，磁盘上所有旧身份都读不出来。
  实测表现：插件加载正常、节点永远起不来、界面停在"正在读取设备"。
- 更麻烦的一点：仓库 `data/group-mesh` 里那份 `dh_public` 既不是现行编码，也不是
  大端遗留（实测与 `pointQ.x` 大端互不相同），"加一条大端兼容分支"不够。
- 修正：该字段只是派生结果的冗余副本、不参与任何密码学计算，**派生值才是权威**；
  不符时记 warning 并采用派生值；只有长度不是 32 字节才按结构性错误拒绝。
  完整性由 Ed25519 私钥↔公钥一致性检查负责。

### 22.13 改了不兼容的线格式却没递增 `PROTO_VERSION`（已修）

- 现象：DH 编码改为 RFC 7748 时忘了递增协议主版本，新旧节点都自报"版本 1"。
  协商层本来会给出"协议主版本不一致（本地 X，对端 Y），请升级后再连"，
  版本号没变就轮不到它，用户看到的只是一句 MAC 校验失败。
- 修正：`PROTO_VERSION = 2`；新增"本机 hello 必须报出 `PROTO_VERSION` 本身"的用例。

### 22.14 测试复制被测常量，版本号漂移仍全绿（已修）

- 现象：`NegotiationTest` 把 `proto: 1` 写死在三处，因此版本号与实现可以在测试
  全绿的情况下漂移（§22.13 的温床）。
- 修正：协商用例的 `proto` 改为引用 `PROTO_VERSION` 常量；注释写明这类
  "测试复制被测常量"的写法是同一个坑。教训：**测试里的期望值如果来自被测实现，
  就只能验证自洽，验证不了正确性**；X25519 官方向量的期望值因此逐字节抄自 RFC，
  不来自本实现输出。

### 22.15 前端脚本顺序是硬约束（remote.js → app.js）

- `index.html` 先引 `js/remote.js`、后引 `js/app.js`。`remote.js` 只定义
  `GroupMeshRemote` 与内部函数、装载期不碰 DOM；`app.js` 在 `DOMContentLoaded`
  后调用 `GroupMeshRemote.init()`。顺序颠倒或漏挂会在装载期报
  `GroupMeshRemote is not defined`。
- `tests/js/plugin_asset_contract.mjs` 把关：`js/` 下不得有未被 `index.html`
  引用的脚本、装载期不得触碰 DOM。这与 media-player 的分片契约是同一类问题
  （见 `docs/media-player-design.md`）。

### 22.16 `noiseprotocol` 的三个反直觉点

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
  256 KiB 分块 / 目录列表会超过它，因此传输态直接用其 `CipherState`（见 §21.2 的
  残余风险），而不是 `NoiseConnection.encrypt`。

### 22.17 私钥保护的两个坑

- **DPAPI 的 `DATA_BLOB` 必须设 argtypes/restype**：64 位下不设会把指针截断，
  `CryptProtectData` 可能失败或写出错乱结果；输出 blob 的 `LocalFree` 也必须调，
  否则每次读写泄漏一块系统内存。
- **keyring 必须做一次 set/get 往返探测**：没有桌面会话时
  `keyring.get_keyring()` 返回 fail backend，直接写会得到一个"看似成功、
  实际读不回"的密钥；探测失败就选下一个 protector，并缓存结果，避免每个文件
  都访问 keyring。
- **迁移只能单向**：已受保护的文件在 keyring 临时不可用时要报错，不能"回落到
  明文重写"——那等于一次 keyring 故障就把私钥降级；`migrate_file()` 只升级不降级。

### 22.18 后台同步踩到的共享连接坑（已修）

- 现象：加后台同步后，`test_placeholder_mtime_comes_from_peer` 失败，报
  `收到的帧过大（3987843650 > 4194304）`——一个正常的目录遍历请求被解析成垃圾帧。
- 根因：后台同步线程用 `_connect()` 从 **UI 复用连接池**里取了一条正在被
  `materialize_remote()` 使用的 Noise 连接；一条连接上的请求严格串行，两个线程
  同时 `send` 把字节流交错，对端读到的帧头就是垃圾。这与 §22.11 上传的第一个坑同源。
- 修正：后台同步、显式探测、注册表/名单 push 全部走 `_open_dedicated()`（每次新开
  一条连接，用完即关）；只有 UI 的浏览/取文件继续使用复用池。
- 教训：**连接复用池的并发语义必须显式**。只要出现第二个后台线程访问对端，就必须
  问"它是否和 UI 共用连接"；共用就得加锁或改用专用连接，不能假设调用是串行的。

### 22.19 前端 UI 改造：面板形态、级联优先级与两个测试坑

界面从"一张 6 卡片栅格"改成**常驻侧栏 + 五个纯显隐面板**（形态与 manga-library /
media-player / image-cleaner 一致，见设计文档 §1.4）。改造中踩到三件事，都属于
"看代码看不出来、只有真跑才发现"：

| # | 现象 | 根因 | 修正 |
| --- | --- | --- | --- |
| 0 | 布局与其它插件**一眼不同**：工具栏横跨整宽、侧栏从工具栏下方才开始 | 把 `.view-toolbar` 放在 `#app` 下当兄弟节点，而 image-viewer / manga-library / media-player 的结构是 `#app > (侧栏 + .view-body > 工具栏 + 内容区)` —— 侧栏是 `#app` 的直接子元素、跑满全高，工具栏属于右侧主区 | 按同一结构重排；分隔线跟着分工：侧栏只画 `border-right`、主区只画 `border-bottom`（两边都画会在交角叠成 2px）。`tests/debug_group_mesh_ui.py` 增了三条结构断言守着 |
| 1 | 侧栏宽度设 236px 实际是 240px；窄窗口下侧栏不横向折叠（仍是竖排） | 壳的 `base.css` 用**同一个类** `.view-sub-sidebar`（0,1,0）写死 `width: var(--sub-sidebar-width)` 与 `flex-direction: column`，与本插件规则**同优先级**；两者都是作者样式，后注入的壳样式胜出 | 改选择器为 `.gm-side.view-sub-sidebar`（0,2,0），并在 CSS 里写清"不能靠改顺序，注入顺序由壳决定" |
| 2 | 工具栏标题永远停在"团体组网" | `setPanel()` 读 `section[data-panel]` 的 `data-title` / `data-sub`，而 `index.html` 里这两个属性**当时忘了写** | 五个面板补齐 `data-title` / `data-sub`（它们是标题的单一来源） |
| 3 | 窄窗口下卡片仍是两列 | 媒体查询顺序：`max-width: 1040px` 与 `max-width: 720px` 里都写了 `.gm-grid` 单列，两者同优先级时**后者胜出**，顺序不能对调 | 两条规则相邻并加注释说明顺序是硬约束 |

测试侧两个坑（都会把排查引向错误方向）：

- **Selenium 的 `.text` 会间歇性返回空串**。本插件新加的卡片/按钮带入场动画
  （`effects.css` 的 `obxFadeUp` + `--obx-i` 交错延迟），实测同一个已渲染、已可见的
  按钮连续取三次：`['', ''] / innerText=['创建团体','加入 / 更新团体'] / ['', '']`。
  失败信息是"按钮文本为空"，看着像"没渲染"。两份浏览器用例因此改用
  `text_of()` / `text_for()`（走 JS `innerText`），点击也改走 JS `.click()`
  （Selenium 的"可交互"判定同样会在动画期间间歇失败）。
- **等待条件必须是"渲染完成"而不是"元素存在"**。原用例轮询 `.gm-card` —— 那是静态
  骨架，第一次检查就命中，于是"等待"等于没等，`get_status` 的桩 Promise 只要晚一个
  微任务就读到空字符串（表现为整份用例随机红）。现在等**容器文本**出现期望内容。
- 面板是纯显隐切换，隐藏面板里的按钮 `.text` 取不到、点不动；用例新增
  `show_panel(driver, name)`（切面板并**校验切换生效**），任何 `.text`/点击之前先切。

界面本身的几何自检（不截图，直接断言布局与配色）见 §6.2 的
`tests/debug_group_mesh_ui.py`：24 项覆盖**结构归属**（侧栏是 `#app` 直接子元素、
工具栏在主区内、侧栏与主区等高）、并排/堆叠、面板互斥、卡片底色与圆角来自 token、
深浅主题对比、交错延迟是否真的写在卡片上。

---

## 23. 验证与测试工具一览

> 下文命令里的 `python` 指仓库虚拟环境：Windows `venv/Scripts/python.exe`，
> Linux `venv/bin/python`（在仓库根目录执行）。

#### 1 门禁（提交前必跑）

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

#### 2 内核与插件测试

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
| 前端（纯渲染） | `python -m unittest tests.test_group_mesh_frontend_e2e` | 桩 Bridge + 无头浏览器：侧栏面板切换（含"切了必须生效"的校验）、弹窗默认不可见、全新安装入口正确、按钮点击有反馈、**不再有"刷新设备"按钮、前端只读后端同步好的状态** | 本机浏览器 |
| 前端（真实壳） | `python -m unittest tests.test_group_mesh_shell_e2e` | 起真实壳服务 + 导航 + iframe：令牌链路、真实 Bridge、`<插件>__<方法>` 前缀 | 本机浏览器 |
| 前端脚本契约 | `python -m unittest tests.test_plugin_frontend_assets_js`（内部调 `node tests/js/plugin_asset_contract.mjs`） | 所有插件前端：`index.html` 与 `js/` 一致、按序装载、装载期不报错 | Node |
| 前端几何/配色自检 | `python tests/debug_group_mesh_ui.py` | 20 项断言取代"看截图"：宽窗口并排 / 窄窗口堆叠、面板互斥、卡片底色与圆角来自壳 token、深浅主题对比、交错延迟写到卡片上；失败时非零退出 | 本机浏览器 |

两条前端用例的排查经验（`.text` 空串、等待条件要看"渲染完成"）见 §22.7。

#### 3 跨平台与跨机联调

| 工具 | 命令 | 覆盖 | 前置 |
| --- | --- | --- | --- |
| 跨平台确定性 | `python shell/groupmesh/tools/interop_fixture.py --out fixture.json`，另一端 `--check fixture.json` | 同一种子在两平台必须算出相同密钥与签名（11 个字段） | 两端都能跑内核 |
| 纯 IPv6 主路径 | `pwsh -File shell/groupmesh/tools/ipv6-test.ps1` | Linux 绑全局 IPv6、Windows 用 `[2409:…]:port` 连接；700 KB sha256 一致；越权/越界被拒；注册记录写入全局 IPv6；节点热认名单 | `~/.ssh/config` 的 `omnibox-linux` 别名 |
| IPv4 兜底 | `pwsh -File shell/groupmesh/tools/lan-test.ps1` | 同网段 IPv4 完整链路与负向验证 | 同上 |
| Linux 侧准备 | `shell/groupmesh/tools/lan-test-linux-prep.sh`、`lan-test-linux-add-member.sh` | 建团体、共享项、加成员、起服务 | Linux 主机 |
| 非无头演示 | `python tests/debug_connection.py`、`debug_materialized_gallery.py`、`debug_network_location.py` | 逐步截图回答"肉眼才能回答"的问题；`debug_connection.py` 专做连接与共享访问（程序化登记地址、sha256 对比） | 桌面环境 |

#### 4 怎么选

- 改协议内核：先 `selftest`，再 `test_group_mesh_mvp`，最后跨平台 fixture；
- 改插件 API / 数据布局：`test_group_mesh_plugin` + 相关物化/网络位置用例；
- 改前端：`test_group_mesh_frontend_e2e`（快）→ `test_group_mesh_shell_e2e`（真）；
- 改名单/注册/上传的端到端行为：`test_multi_instance_fixture`；
- 改壳鉴权/主体：`test_shell_principal`；
- 对外联调或发布前：两个 PowerShell 脚本至少跑 `ipv6-test.ps1`。

---

## 24. 下一步建议顺序

按依赖关系排列，每一步都应有可复核的验收命令。本轮已完成私钥保护（§21.1）与
Noise vetted 实现 + 官方向量（§21.2）。

1. **P2：Rekey 与连接上限**（§21.3、§21.10）——`noiseprotocol` 已提供
   `CipherState.rekey()`，需要应用层协商"何时轮换"（消息数/字节数/时长）；
   同时给节点加连接上限与"不自动监听"开关，收敛 DoS 面。
2. **P2：解散通告、设备配对/多设备、稳定地址判定**（§21.7、§21.6.1）——
   解散记录类型与处理路径；`add_device()` 的配对入口；Windows
   `GetAdaptersAddresses(Temporary)` 识别临时地址。
3. **P2.5：遗留 `.part` 的属主端可见性**（§21.7）——列出未完成暂存与占用，
   让属主能清理。
4. **P1 主体限权（暂缓）**：本轮按决定不做 §21.8、§21.9；方案与验收已写在
   §21.8 / §21.9，恢复时按原条目推进（壳侧 `authorize_file()` 钩子 + 插件
   `require_principal()` + 收窄 `upload_remote` 的来源）。
5. **P3：房间/语音/游戏面 Companion 子插件**——不在本插件路线，前置实测见
   §29 §4。
6. **P4：Android 轻客户端 / 本地网关**（仅保留设计位）。

---

## 25. 仍然没有答案的问题

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | **跨公网**是否真的可达？同网段 IPv6 直连已实测，运营商是否放行入站高位端口未验证 | 决定设计 §1.1 的"公网 IPv6"前提能否成立 |
| 2 | Windows 上如何区分稳定地址与 RFC 4941 临时地址？ | 决定注册记录写入哪个地址（§21.6.1） |
| 3 | 群主私钥一旦丢失，团体永久不可管理；是否需要"名单备份 + 冷存储"的操作指引？ | 决定是否需要额外用户引导（设计 §5.6） |
| 4 | 插件前端是否需要在没有全局 IPv6 时给出明确的"仅局域网可用"提示？ | 决定设计 §1.3 的边界在界面上如何表达 |
| 5 | 取消/中断留下的 `<目标>.part` 是否需要属主端可见（列出未完成暂存与占用）？ | 决定 §21.7 的遗留暂存由谁、怎么清理 |
| 6 | 多使用者部署下，物化缓存与下载目录的"主体级授权"用什么模型？ | 决定 §21.8 的 `authorize_file()` 接口形态与 group-mesh 的 ACL 映射 |

---

## 26. 本轮分析结论摘要

**做得好的地方**：协议级授权（ACL 按握手验证过的主体判定）、名单规则链与准入
（"本机认得这一份"而非"对端自证"）、上传的暂存/提交/续传语义、物化与网络位置
对消费型插件的兼容，是本项目最扎实的部分；内核与插件的分层使协议可以脱离 GUI
验证，跨机脚本与多层测试把关键拒绝路径都覆盖了。

**本轮已收敛三项**：

1. 私钥保护（§21.1）：DPAPI / keyring / 口令 AES-GCM；旧明文首次读取即单向升级，
   已受保护的文件不降级；插件状态页显示当前保护级别。
2. Noise 换 vetted 实现（§21.2）：`noiseprotocol` + cacophony 官方向量逐字节校验；
   替换前的手写状态机也通过同一组向量，因此线格式不变、`PROTO_VERSION` 保持 2。
3. 后台同步（§21.7、设计 §5.7 / §7.3）：移除"刷新设备"手动入口，改为可配置间隔
   （`sync_interval_seconds`，默认 60 秒）的定时轮询 + 名单/注册变更时 push；
   同步线程用专用连接，避免与 UI 复用连接池并发（§22.18）。

**仍需优先处理**：

1. 主体限权（§21.8、§21.9，**本轮按决定暂缓**）：多使用者部署下仍是跨主体
   读/写/外带面，`upload_remote` 还能带出 `.config` 凭据；
2. 节点自动监听 + 每连接一线程、无连接上限（§21.10）；
3. 长连接 Rekey（§21.3）；
4. `plain` 回落（§21.1）：headless Linux 没有 keyring 且未设 `OMNIBOX_SECRET_KEY`
   时私钥仍是明文，状态页会标红——这是残余风险，不是"已静默忽略"。

**可以晚一点但必须记录**：解散、设备配对、稳定地址、内容寻址、Android 轻客户端；
`minShellVersion` 已决定不做运行时校验。

**文档卫生**：§21.14 列出的过期文件头与错误交叉引用已在本轮一并修正，p1-review
已改为存档指针；剩下无源 pyc 等仓库卫生项建议顺手清理。

---

---

## 28. UI 现状取证（前端与壳契约对照）

> 只读审计。契约基准：`shell/frontend/public/shell/{variables.css, base.css, effects.css, base.js}`
> 与 `docs/plugin-guide.md` §4.1 / §4.3 / §4.4。所有结论附 `文件路径:行号`。

### 28.1 概览

**前端文件清单**

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `plugins/group-mesh/frontend/index.html` | 338 | 唯一页面入口（`manifest.json:20` `frontend.entry`），5 个面板 + 6 个弹窗的静态骨架 |
| `plugins/group-mesh/frontend/group-mesh.css` | 905 | 全部插件样式（无第二份 CSS） |
| `plugins/group-mesh/frontend/js/app.js` | 603 | 状态读取、面板切换、身份/名单/共享项/节点/路线图渲染、弹窗开合、`Toast`/`confirmDialog` 封装 |
| `plugins/group-mesh/frontend/js/remote.js` | 755 | 「远端共享」分片：设备 → 共享项 → 目录 → 取回/上传，含上传轮询与生命周期绑定 |
| `plugins/group-mesh/frontend/network-location.html` | 290 | **第二个 HTML 页面**：被壳共享目录组件 FolderPicker 嵌进 iframe 的「网络位置」提供方（`network-location.html:8-17`） |

- **页面形态**：单页多视图。`#app` 下 5 个 `section.gm-panel`（`index.html:102/124/137/158/203`）靠 `data-active` 纯显隐切换（`group-mesh.css:281-287`、`app.js:116-140`），不销毁 DOM。
- **iframe 常驻**：`manifest.json:8` `"keepAlive": true`，因此 `remote.js:726-736` 必须靠壳的 `onShow`/`onHide` 停/起 15s 轮询（`remote.js:702` `VIEW_POLL_MS = 15000`），只把 `visibilitychange` 当壳缺失时的退路。
- **脚本顺序契约**：`index.html:335-336` `remote.js` 在 `app.js` 之前；`app.js:582-595` 用依赖注入把 `call/toast/escapeHtml/openModal/closeModal` 交给分片，两者装载期都不碰 DOM（`app.js:599-602`）。
- **是否用 Shell 布局类**：是，且是本仓库最完整的一个——`.view-sub-sidebar`、`.view-body`、`.view-toolbar`、`.toolbar-group`、`.view-content`、`.obx-nav-item`、`.modal/.modal-box/.modal-body/.modal-footer`、`.btn/.btn-sm/.btn-primary/.btn-danger`、`.obx-glass`、`.obx-scroll`、`.obx-card-lift`、`.obx-stagger`、`.obx-anim-scale`、`.obx-skeleton`、`.obx-ease` 全部来自壳。
- **壳注入顺序（影响后面所有「谁赢」的判断）**：`shell/backend/file_server.py:797-798` 把 `variables.css / base.css / effects.css / base.js / motion.js` 注入到 `</head>` **之前**，所以插件自己的 `<link href="group-mesh.css">`（`index.html:7`）实际排在壳样式**之后**，插件同优先级规则天然胜出。

### 28.2 布局骨架

顶层容器结构树（`index.html:22-222`）：

- `div#app`（`group-mesh.css:53-59`）
  - `aside.gm-side.view-sub-sidebar.obx-glass`（`index.html:30`）
    - `div.gm-brand` → `.gm-brand-icon` / `.gm-brand-text`（`index.html:31-37`）
    - `nav.gm-nav.obx-scroll#gm-nav`（`index.html:39`）：4 个 `.gm-nav-label` 分组标题 + 5 个 `button.obx-nav-item.gm-nav-item[data-panel]`（`index.html:41/46/49/55/61`），其中两项带 `.gm-nav-count` 角标（`index.html:51/57`）
    - `div.gm-side-foot.sub-sidebar-footer#gm-side-summary`（`index.html:66`）：`.gm-foot-dot[data-state]` + `.gm-foot-text`（`app.js:395-426` 写）
  - `div.view-body.gm-main`（`index.html:72`）
    - `div.view-toolbar.gm-toolbar`（`index.html:73`）：`.toolbar-group.gm-heading`（图标 + `#gm-panel-title` 15px/700 + `#gm-panel-sub` 11px）+ `.toolbar-group.gm-toolbar-right`（`margin-left:auto`，`group-mesh.css:100-102`）内两个 `.btn.btn-sm`
    - `div#kernel-missing.gm-banner.gm-banner-error[hidden]`（`index.html:90`，`app.js:148` 控制）
    - `div.view-content.gm-content.obx-scroll#gm-content`（`index.html:96`）
      - `div.gm-panels.obx-stagger#gm-panels`（`index.html:97`）
        - `section.gm-panel[data-panel][data-title][data-sub]` ×5
          - `div.gm-grid`（两列，`group-mesh.css:290-294`）→ `section.gm-card.obx-card-lift` ×2（仅 machine 面板）/ 单卡 ×1（其余面板）
  - `div.modal` ×6（`index.html:228/242/256/275/291/316`）挂在 `body` 下，与 `#app` 平级

**尺寸与滚动来源**

| 部位 | 数值 | 来源 |
| --- | --- | --- |
| 侧栏宽度 | `236px` | 插件自定义 `--gm-side-width`（`group-mesh.css:35`）→ `.gm-side.view-sub-sidebar{width:var(--gm-side-width)}`（`:141-145`），覆盖壳的 `240px`（`base.css:251-259`） |
| 侧栏方向 | `flex-direction: row`（≤720px 时折成横向导航） | 插件（`group-mesh.css:840-850`） |
| 工具栏高度 | `48px` | 壳 `.view-toolbar`（`base.css:239-248`），插件只改 `gap`（`group-mesh.css:67-69` `gap:12px`） |
| 主内容滚动 | `.view-content{overflow-y:auto}` | 壳（`base.css:277-282`）；插件补 `padding:16px / background:transparent`（`group-mesh.css:271-277`） |
| 高度链 | `html,body{height:100%}` + `#app{display:flex;height:100%;overflow:hidden}` | **插件自定义**（`group-mesh.css:47-59`），因为壳 `base.css:6-11` 只给 `body` 设了 `overflow:hidden` |
| 卡片栅格 | `repeat(2,minmax(0,1fr))`、`gap:16px`（≤1040px 单列） | 插件（`group-mesh.css:290-294`、`:901-905`） |
| 卡圆角 / 内边距 | `--gm-radius`=12px、`padding:16px` | 插件（`group-mesh.css:296-305`） |
| 远端两栏 | 设备栏 `272px` + 文件栏 `flex:1`，各自 `overflow-y:auto` | 插件（`--gm-peers-width`，`group-mesh.css:36/633-645`） |
| 弹窗宽度 | `480 / 560 / 560 / 520 / 560 / 520 px` 内联 | 插件（`index.html:229/243/257/276/292/317`），壳 `.modal-box` 默认 `400px`（`base.css:165-169`）被内联宽度覆盖 |

`network-location.html` 是**独立文档**，不复用任何布局类：`body{padding:14px 16px}`（`:25`）+ 三步 `.nl-step`（`:64-79`），列表 `.nl-list{max-height:150px;overflow-y:auto}`（`:31`）——**没有加 `.obx-scroll`**。

### 28.3 设计 token 使用

**实际用到的壳变量（`group-mesh.css` 内计数，112 处 `var(--…)`）**

- 背景：`--bg-app`(58/535/574/740/765/782) `--bg-surface`(301/383/818) `--bg-hover`(122/229/449/705)
- 文本：`--text-primary`(88/115/171/321/334/384/506/595/697/769/784/797/819) `--text-secondary`(94/177/230/245/346/454/486/500/527/543/587/655/742) `--text-muted`(199/253/260/403/680/747) `--text-on-accent`(710)
- 边框/强调/语义：`--border`(152/297/381/440/533/549/572/636/739/764/783/849/888) `--accent`(31/395/477/709/804) `--success`(32/257/308/478/479/726/727/817) `--warning`(33/258/309/476) `--danger`(34/113/259/310/773/774/828) `--danger-soft`(34)
- 尺寸/动效：`--radius`(30) `--radius-lg`(29) `--radius-sm`(121/382) `--transition-fast`(387/701) `--shadow-md`(820)
- effects.css / motion.js：`--obx-ease`(303/444/824) `--obx-i`(304/445；`app.js:236/296/389` 内联写入) `--obx-shadow-2`(820)

**插件自定义变量**：`:root` 下 8 个（`group-mesh.css:28-37`）`--gm-radius`、`--gm-radius-sm`、`--gm-accent-soft`、`--gm-success-soft`、`--gm-warn-soft`、`--gm-danger-soft`、`--gm-side-width`、`--gm-peers-width`。其中 `--gm-success-soft`(32) 与 `--gm-warn-soft`(33) 定义后**零引用**（死 token）。插件**没有**覆盖 `:root` 里任何一个壳 token，只做「壳 token → `--gm-*` 派生」。`.gm-card-wide`(`:313-315`) 同样零引用。

**硬编码颜色字面量**

- `group-mesh.css` 的正则 `#[0-9a-fA-F]{3,8}\b|rgba?\(` 命中 **3 行**，其中 2 行在注释里（`:16` 记录旧版写死的 `#f59e0b / #8b5cf6`、`:463` 同一记录），**唯一的生效字面量**是 `:820` `box-shadow: var(--shadow-md, var(--obx-shadow-2, 0 10px 30px rgba(0,0,0,0.2)));` —— 三级回退链的最末兜底。
- 对比 `network-location.html`：同一正则命中 **15 行**，全是「变量 + 字面量回退」写法，典型 4 条：`:27` `var(--bg, #17181c)`、`:38` `var(--accent, #4c8dff)`、`:60` `color: #fff`（`.btn-primary` 的纯字面量）、`:214` `'var(--danger, #e5484d)'`。根因是 `:23` `:root { color-scheme: light dark; }` 明确按「可能脱离壳单独打开」设计。

**data-theme 处理**：三个文件全部**没有** `data-theme` 代码（grep `data-theme|color-scheme` 只命中 `network-location.html:23` 的 `color-scheme`）。深色完全靠壳注入脚本（`file_server.py:71-76` 同步 `data-theme`）+ token 生效；`network-location.html:24-28` 甚至给 body 写了「深色优先」的回退底色。

### 28.4 组件与命名约定

**前缀**：`gm-`（group-mesh）。按类别清单：

- 按钮：**无自有按钮类**，全靠壳 `.btn/.btn-sm/.btn-primary/.btn-danger`；只覆写一个尺寸 `.gm-table .btn-danger{padding:3px 10px;font-size:12px}`（`:559-562`）
- 卡片：`.gm-card`(`:296`)、状态边 `.gm-card-ok/warn/error/muted`(`:308-311`)、`.gm-card-wide`(`:313`，未用)
- 弹窗：**零自有弹窗类**，用壳 `.modal`（`index.html:228` 等 6 处），仅给 `.modal-body input/textarea` 加尺寸（`:414-426`）
- 菜单/分页/树/灯箱/卡片网格：`createContextMenu` / `createPagination` / `createTree` / `createLightbox` / `createCardGrid` 一个都没调用；表格是手写 `<table class="gm-table">`（`app.js:258/306`、`remote.js:178`）
- 进度：`.gm-remote-progress`(`:760`)、错误态 `.gm-remote-progress-error`(`:772`)、弹窗内 `.gm-upload-progress`(`:778`)——**纯文本进度，无 `<progress>`/百分比条**，百分比只出现在文案里（`remote.js:527-528`）
- toast：`.gm-toast` / `.gm-toast-error`(`:811-829`)，只在壳 `Toast` 缺失时启用（`app.js:29-40`）
- 空状态：`.gm-empty`(`:495-508`)，shell 的 `.empty-state` 未使用
- 骨架屏：`.gm-skeleton-line`(`:511-520`) + 壳 `.obx-skeleton`（`app.js:433-434`）
- 徽标：`.gm-badge` + `-owner/-admin/-member/-ok/-warn`(`:466-480`)、`.gm-remote-badge`(`:721`)、`.gm-nav-count`(`:225`)、`.gm-foot-dot[data-state]`(`:249-260`)
- 其他：`.gm-kv`(`:339` 定义列表)、`.gm-hint`(`:484`)、`.gm-details`(`:532`)、`.gm-list`(`:522`)、`.gm-check`(`:789`)、`.gm-address*`(`:566-607`)、`.gm-remote-*`(`:611-775`)、`.gm-banner-error`(`:112`)、`.gm-brand*`(`:147-179`)、`.gm-nav*`(`:181-233`)

**「Shell 已提供但插件又实现一遍」的能力**

| 能力 | Shell 提供 | 插件实现 | 差异 |
| --- | --- | --- | --- |
| Toast | `.toast*`（`base.css:208-234`）+ `Toast.*` | `.gm-toast`(`:811-829`) | 仅作脱离壳的兜底（`app.js:31-39`）。位置相反：壳 `top:16px;right:16px`，插件 `right:20px;bottom:20px`；插件用 `border:1px solid var(--success)` 表语义，壳用 `.toast-icon` 里的 `Utils.iconHtml('icon:circle-check')`（`base.js:390-400`） |
| 空状态 | `.empty-state`（`base.css:428-431`，`min-height:300px` 居中 16px） | `.gm-empty`(`:495-503`，`13px` 左对齐多行 + `<strong>`) | 语义化更强（可带标题+说明），但视觉与壳不一处 |
| 骨架屏 | `.obx-skeleton`（`effects.css:118-131`） | `.gm-skeleton-line`(`:511-520`) 再叠一层 | 壳只管配色与 shimmer，插件补 `height:12px` / `margin:6px 0` / 圆角 6px；即壳的骨架尺寸仍需插件给 |
| 表单控件 | `.field input/select/textarea`（`base.css:187-206`，含 `:focus` 强调色、`min-height:120px` textarea） | `.gm-form/.gm-address/.modal-body` 三组选择器(`:375-426`) | 视觉接近（6px 10px、`--radius-sm`），但选择器是**元素级**且覆盖 `.modal-body`（见 §28.7）；textarea 无 `min-height`，只 `resize:vertical` |
| 表格 / 卡片 / 徽标 / 定义列表 | **壳无对应类** | `.gm-table`(`:430-460`)、`.gm-card`(`:296-322`)、`.gm-badge`(`:466`)、`.gm-kv`(`:339`) | 真实缺口，不是重复实现 |
| 弹窗 | `.modal` 全家桶 | 无自有弹窗类（`:20-23` 注释记录曾自绘 `.gm-modal` 已删除） | **已对齐**，是本仓库的迁移范例 |

### 28.5 交互约定

- **设置入口**：工具栏 `#btn-settings`（`index.html:83`）→ `openSettingsModal({ title: '团体组网设置' })`（`app.js:486-492`），**不传 `schema`/`values`/`onSave`**，完全依赖壳去 `Bridge.call('get_settings_schema'|'get_settings')`（`base.js:572-577`）；缺失壳时只弹一条错误 toast（`app.js:488`）。保存反馈由壳给：`Toast.success('设置已保存')` + 400ms 后 `location.href = …?_t=` 整页重载（`base.js:621-622`）。
- **错误提示**：统一 `window.Toast.error`（`app.js:29-40`、`remote.js:43`）；两类「非 toast」错误——首屏 `get_status` 失败写侧栏 `#gm-foot-text = '读取状态失败'` + `data-state="error"`（`app.js:465-471`），内核缺失走常驻横幅 `.gm-banner-error`（`app.js:148`）。弹窗内错误留在弹窗里（`remote.js:612`）。
- **选择模型**：**单选**（远端设备/共享项点击 `remote.js:105-109`；目录项「进入/取回」逐行按钮 `remote.js:159-168`）。**无多选、无框选、无长按、无拖拽**，`app.js:504` 的 `read: 'group'` 是硬编码 ACL。
- **右键菜单**：**无**（无 `contextmenu` 监听，未调用 `createContextMenu`）。
- **键盘快捷键**：**无**（全前端 grep `keydown|keyup` 零命中）；只有程序化 `focus()`（`app.js:227/228`、`remote.js:479/659`）。
- **长任务进度与取消**：文本行 `.gm-remote-progress`，六种状态全覆盖——「正在物化 X…」(`remote.js:389`)、「正在读取对端目录…」(`:410`)、「正在取回 X…」(`:432`)、上传百分比「续传中/正在上传 1.2 MB / 8.0 MB（15%）」(`:523-529`)、「已取消，已传 …（再点「上传」会从这里续传）」(`:570-571`)、「上传被中断，已传 …」(`:574-580`)。轮询间隔 `300ms`(`:584`)；传输中 `#btn-do-upload` 置灰并改名「上传中…」、`#btn-close-upload` 锁死、`#btn-cancel-upload` 显形（`remote.js:497-513` ← `index.html:310`），取消后文案变「正在取消…」(`:630`)。
- **空态/加载态/错误态文案与类**：
  - 加载：内容区骨架两行 `.gm-skeleton-line.obx-skeleton`（`app.js:433-434`）+ 侧栏「正在读取状态…」（`index.html:68`）；远端「正在读取设备…」（`remote.js:52/304`）
  - 空态：`<p class="gm-empty">`，文案如「本机还没有身份。」（`app.js:179`）、「本机还没有团体名单。」（`:218`）、「**本机还没有共享项。**」(`:283`)、「还没有可用的对端设备。」(`remote.js:57`)、「这个目录是空的。」(`:154`)
  - 错误态：`.gm-badge.gm-badge-warn` 内联在提示里（`app.js:339/366`）、`.gm-remote-progress-error`(`remote.js:393/437`)、`.gm-banner-error`(`app.js:148`)、`network-location.html` 用 `.nl-empty` 写「读取设备失败：…」(`:127`)

### 28.6 特色设计（值得吸收）

1. **面板标题/副标题只写在 HTML 的 `data-title`/`data-sub` 上，工具栏标题是渲染产物**（`index.html:102-104` → `app.js:131-137`）。解决：新增面板不再需要在 JS 里再维护一份标题表，两处文案永不漂移。
2. **卡片顶边 2px 表达后端状态，而不是让用户逐行读文字**（`group-mesh.css:296-311`；`app.js:191/249/351-356`）。解决：`身份/名单/节点` 三张卡「一眼看健康度」；`gm-card-warn` 还刻意区分「名单过期只是提示，不阻断通信」（`app.js:248`）。
3. **文本进度区把结论留下（含落地路径、字节数、断点位置），而不是进度条走完就消失**（`remote.js:397-401/446-447/570-571`）。解决：跨进程/跨弹窗的长任务失败后用户仍能知道「传到哪了、能不能续传」。
4. **上传状态机把 `interrupted` / `cancelled` 当一等状态并明说「再点上传会续传」**（`remote.js:482-492/566-581`；后端任务表见 `index.html:302-306`）。解决：插件重载/进程被杀后用户不会误以为白传了。
5. **侧栏底部常驻「节点运行 + 是否已发布 + 团体/名单版本」三件事**，并用 `data-state` 圆点编码 OK/warn/error/idle（`group-mesh.css:249-260`、`app.js:395-426`）。解决：本插件的核心问题「我到底能不能被别人连上」有了唯一常驻答案。
6. **壳缺失时的降级是显式设计的**：`Toast` 缺失走 `.gm-toast`（`app.js:31-39`）、`confirmDialog` 缺失走原生 `confirm`（`app.js:315-318`）、`Bridge` 缺失返回带说明的 reject（`app.js:44-46`）、`Motion` 缺失安静跳过（`app.js:58-62`）。解决：页面可脱离壳直接打开调试，且降级路径不静默失败（`network-location.html:104-107` 的 `postMessage` try/catch 同思路）。
7. **`network-location.html` 对「输入框回写」的取舍被写成注释留档**（`:192-203`）：`input` 事件里只同步 `state`、**绝不回写输入框**，只在 `blur` 时回写规范化结果（`:281-284`），并在 `run()` 里以输入框当前值为准（`:236`）。解决：逐字符输入时反斜杠被规范化吃掉导致 `C:UsersADMINI~1...` 的难查 bug。

### 28.7 与 Shell 契约的偏差

| # | 偏差 | 证据 | 影响面 |
| --- | --- | --- | --- |
| 1 | **侧栏宽度必须靠提高优先级才能覆盖壳**：壳用 `.view-sub-sidebar`(0,1,0) 写死 `--sub-sidebar-width`(240px)，插件改 236px 只能写成 `.gm-side.view-sub-sidebar`(0,2,0) | `group-mesh.css:135-145`（注释自陈「同优先级时后注入的壳样式胜出…实测：设成 236 实际 240」）；壳侧 `base.css:251-259` | 1 处规则 / 侧栏整条布局；窄窗口 ≤720px 的横向折叠同规则内（`:840-850`）。任何「统一侧栏宽度」的动作会立刻把这里的选择器策略打回 240px |
| 2 | **元素级表单样式覆盖壳的弹窗正文**：`.modal-body input, .modal-body textarea`（两组共 10 个选择器）不受插件前缀约束 | `group-mesh.css:375-404/414-426`；壳侧 `.field input…`(`base.css:187-200`)、`.modal-body`(`base.css:174`) | 6 个弹窗的全部输入框 / 1 处宽选择器；将来壳给 `.modal-body input` 加统一类时会被这 13px/6px 10px 的规则顶掉（同优先级下插件在壳之后注入，插件赢，见 `file_server.py:797-798`） |
| 3 | **`[hidden]` 不可用，改用内联 `style.display`**（作者样式的 `display` 会盖掉 UA 给 `[hidden]` 的 `display:none`） | `remote.js:501-502` 与 `:508/519`；`index.html:301/310` 初始就是 `style="display:none"` | 上传弹窗 3 个按钮 / 1 处历史事故（见 §22.7）。统一 UI 若给 `.btn` 加 `display` 声明，会重演同类问题 |
| 4 | **高度链仍需插件自己给全**：壳只给 `body{overflow:hidden}`，`html,body{height:100%}` 由插件写 | `group-mesh.css:39-59`；壳侧 `base.css:6-11` | 1 个插件 / 全部内容的滚动；若统一布局把 `#app` 换成壳提供的容器类，`html,body` 这两行必须一起搬走，否则 `.view-content` 的 `overflow-y` 静默失效（内容被裁） |
| 5 | **响应式断点顺序与常规相反**：`@media (max-width:720px)` 写在 `@media (max-width:1040px)` **之前**，两条都改 `.gm-grid`，靠源码顺序让宽的那条兜底 | `group-mesh.css:834` 与 `:901`（`:898-900` 注释自陈「顺序不能对调」） | 2 条媒体查询 / 卡片栅格与远端两栏；统一断点体系时极易被格式化或合并工具重排而反向生效 |
| 6 | **自有 toast / 空态 / 骨架三件套与壳并存** | `.gm-toast`(`:811-829`)、`.gm-empty`(`:495`)、`.gm-skeleton-line`(`:511`) vs 壳 `.toast*`/`.empty-state`/`.obx-skeleton` | 3 组类 / 全站空态与提示观感：同一屏里「壳的 toast 在右上、插件的 toast 在右下」是可见差异（`base.css:209-213` vs `:811-815`） |
| 7 | **`--gm-success-soft` / `--gm-warn-soft` / `.gm-card-wide` 定义未用** | `group-mesh.css:32-33`、`:313-315`（grep `--gm-success-soft` 仅命中定义行） | 3 个符号 / 无功能影响；但会让「统一 token 表」误以为它们仍在被消费 |
| 8 | **`network-location.html` 完全绕开壳的类体系**：内联 `<style>` 里重定义 `.btn`/`.btn-primary`，并把 `.view-*` 布局类一并弃用 | `network-location.html:54-60`（`.btn{padding:4px 10px;font-size:12px;border-radius:6px}`）；注入点在 `</head>` 前（`file_server.py:798`）意味着**壳的 `.btn`(`base.css:14-24`, 6px 14px/13px/4px) 在本页会覆盖插件这份**，实际渲染是壳的尺寸 | 1 个页面 / 三步选择器全部按钮；该页是「网络位置」提供方契约（`docs/plugin-guide.md` §7.2.1）的实现，改壳的 `.btn` 会**直接改变这个页面的按钮尺寸**，而页面作者以为自己在用自定义样式 |
| 9 | **进度只用文本，不消费壳的任何进度组件** | `remote.js:214-225` 只写 `textContent`；百分比仅文案（`:527-528`） | 4 类长任务（物化/列目录/取回/上传）；统一进度组件时必须同时改前端 4 条链路的后端返回（`result.fetched/unchanged/error_count/truncated`，`network-location.html:260-263`） |
| 10 | **设置弹窗依赖壳的「保存后整页重载」隐式行为** | `app.js:491` 不传 `onSave`；壳 `base.js:621-622` 重载带 `?_t=` | 1 个入口；若统一 UI 去掉重载语义，group-mesh 的设置项（后端 `get_settings_schema`）将不会在界面上生效 |
| 11 | **`visibilitychange` 兜底与壳生命周期并存** | `remote.js:726-736`：有 `onShow/onHide` 用壳的，否则监听 `document.visibilitychange` | 1 处 / 15s 轮询；`keepAlive` 下 `document.hidden` 恒 false（`:720-724` 注释），统一生命周期时这段兜底是必须一起收编的死角 |

---

## 29. Companion 子插件（房间 / 语音 / 游戏面）设计草案

> 版本：草案 v0.1。状态：**尚未开始**。本节只定目标形态与前置实测项，编码前先补待实测项。
> 归属：这些能力由**未来的 Companion 子插件**实现，不属于核心插件 group-mesh。
> 依赖：`manifest.dependencies: ["group-mesh"]`，复用内核 `shell/groupmesh/` 的
> 身份、团体名单、注册发现与加密传输。原为独立文档 `docs/group-mesh-companions.md`，已并入本文。
>
> **编号约定**：下面的 `### 0.`–`### 5.` 沿用原草案编号。

### 0. 定位与依赖契约

核心插件 group-mesh 提供身份、名单、注册、发现与加密通道；子插件在此之上做具体用途：

| 子插件 | 提供 | 复用内核的什么 |
| --- | --- | --- |
| 房间/语音 | 房间声明、成员集合、语音与文字通道 | 身份与名单（谁是谁）、注册表（谁在哪）、加密传输（点对点会话） |
| 游戏面 | 老游戏的二层虚拟局域网 | 同上 + 房间成员集合 |

契约要点：

- 子插件**不重复实现**身份与信任：成员资格一律来自内核的团体名单，不引入第二套账号；
- 房间是**独立于团体**的临时集合：团体是身份与授权边界，房间是一次具体活动；
- 内核侧不假设每个参与者都承担存储与转发（见核心插件设计 §11.2 的角色预留）。

---

### 1. 房间

#### 1.1 房间对象

```json
{
  "room":    "<房间标识>",
  "group":   "<所属团体>",
  "owner":   "<房主主体公钥>",
  "mode":    "mesh",
  "relays":  [{"device": "<设备公钥>", "endpoint": ["240e:xxxx::1", 18443]}],
  "members": ["<主体公钥>"],
  "expires": 1767225600,
  "sig":     "<房主签名>"
}
```

房间是一次具体活动的集合，独立于团体：团体是身份与授权边界，房间是联机或语音的临时 L2 域，拥有自己的虚拟 IPv4 网段。

#### 1.2 模式

| 模式 | 拓扑 | 适用 |
| --- | --- | --- |
| `mesh` | 成员之间直连 | 语音建议 6 人以内；游戏 10 人以内 |
| `relay` | 经由一个或多个中继节点转发 | 人数超过上述范围，或直连失败时 |

中继节点可以由任一成员承担，包括在机房租用高带宽机器的成员。

#### 1.3 语音

- 采用 WebRTC + Opus；
- 网状模式每人上行 = (N−1) × 约 40 kbps：5 人约 160 kbps，10 人约 360 kbps，50 人约 1.96 Mbps；
- 移动网络上行通常为 1～5 Mbps，因此人数较多时需要中继；
- 中继模式下每人只发一路给中继，中继上行约 N × 40 kbps（50 人约 2 Mbps）；
- **不设硬上限**；界面按当前模式标出可承载范围，并在超过 6 人时提示可以切换中继模式。该提示仅为建议，不阻断加入。

#### 1.4 游戏

- 二层虚拟局域网中每条广播与组播帧必须复制给房间内其他每个节点，复制量由本项目承担，与游戏自身的服务端负载无关；
- 网状模式下发送方复制 N−1 份；改为星形中继后为发送方发 1 份、中继复制 N−1 份，上行压力从各节点集中到中继；
- 10 人房间每条广播复制 9 份，为实际支撑目标；**不设硬上限**；
- 整个团体不得置于同一 L2 广播域，房间必须独立划分。

#### 1.5 知情同意

加入房间即接受房间声明（含模式与中继），不需要投票机制。两点必须如实标注：

- 中继可见通信元数据（谁与谁通信、流量大小）；
- 语音中继需要解密音频，因此**可见内容**；需要端到端加密的语音只能使用网状模式。

---

### 2. 游戏联机引擎

#### 2.1 选型

选用 **SoftEther VPN**（虚拟 Hub 提供二层能力）。

| 项 | 说明 |
| --- | --- |
| 许可 | Apache License 2.0（[官方手册 1.3](https://www.softether.org/4-docs/1-manual/1/1.3_SoftEther_VPN_is_Open_Source)），与本仓库许可一致 |
| 分发 | **不随包分发**，由用户自行从官方渠道下载安装 |
| 插件职责 | 只做编排：生成配置、启停、状态探测 |

许可义务在用户自行下载的前提下不触发；界面仍标注来源与许可，便于用户知情。若将来改为随包分发，需履行 Apache-2.0 的四项义务：附许可证副本、保留版权与署名声明、标注修改过的文件、不使用其商标作背书，并核对 `src/THIRD_PARTY.TXT` 中第三方组件的许可。

#### 2.2 外部依赖的编排方式

参照 `netease-music` 的既有范式（`plugins/netease-music/backend/netease_music_api.py`）：外部程序由用户安装，插件负责定位、探测与调用。

| 项 | 设计 |
| --- | --- |
| 定位 | 优先取设置项中的用户指定目录（`settings_schema` 的 `directory` 类型，由 Shell 的 `FolderPicker` 渲染），其次探测系统 PATH |
| 探测 | 调用 `vpncmd` 获取版本；状态区显示「未安装 / 已安装 版本 X」 |
| 调用 | 参数一律以数组形式传入，**不使用 `shell=True`**（Windows 上 `.cmd` 会被 `cmd.exe` 二次解析） |
| 版本要求 | 设置最低版本，低于则提示 |
| 权限 | 虚拟网卡驱动安装需要管理员权限，属一次性操作，插件只提示，不代替提权 |

#### 2.3 待实测项

编码前必须先拿到这四项数据：

- 虚拟 Hub 对广播与组播帧的转发行为；
- 虚拟网卡驱动的安装流程与静默安装可行性；
- IPv6 下节点与虚拟 Hub 的连通性；
- 实际吞吐。

---

### 3. 默认参数

| # | 参数 | 取值 |
| --- | --- | --- |
| 1 | 语音切换中继的提示阈值 | 6 人；仅作提示，不阻断加入 |
| 2 | 游戏房间的实际支撑目标 | 10 人（每条广播复制 9 份） |
| 3 | 语音编码 | WebRTC + Opus |

---

### 4. 待实测项

| # | 事项 | 影响 |
| --- | --- | --- |
| 1 | SoftEther 虚拟 Hub 的广播与组播转发表现 | 决定游戏联机是否可用 |
| 2 | 虚拟网卡驱动的安装流程 | 决定首次使用的操作步骤 |
| 3 | IPv6 下节点与虚拟 Hub 的连通性 | 决定是否需要在同一网段内使用 |
| 4 | 实际吞吐 | 决定房间人数的实际上限 |

---

### 5. 落地时要带上的清单

子插件与主程序同进程运行，因此新增的每个 `shell.backend.*` 与第三方模块都必须登记：

- `docs/Releases/spec_common.py` 的 `HIDDEN_IMPORTS`（漏了会被静默跳过，表现为运行时
  `ImportError`）；
- 第三方包写进 `requirements.txt`；
- `manifest.json` 声明 `dependencies: ["group-mesh"]` 与 `minShellVersion`；
- 前端脚本按 `docs/plugin-guide.md` 的装载契约组织，并让
  `tools/check_frontend_escape.cjs` 的转义登记表覆盖新增插值。
