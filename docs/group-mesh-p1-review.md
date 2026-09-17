# 组网插件：实现顺序复核与 P1 方案

> 日期：2026-09-17
> 状态：顺序结论与缺陷清单已实测复核；P1 第 1/2 项已实施，其余待做
> 关联：[设计文档](./group-mesh-design.md) §12/§16、[实现路径](./group-mesh-implementation-path.md)
> 基线：`feat/group-mesh` @ `5ad8a33`（本轮已提交 `85b55b3` / `3e1aaa7` / `2513bb4`）

---

## 1. 顺序复核：结论

原提议的顺序是

> P1 壳侧主体上下文 → 私钥保护（DPAPI/keyring） → 噪声加固（先 RFC 7748 编码，
> 再换 vetted 实现 + 官方向量） → 名单自动分发 → 内容寻址

**这个顺序基本正确，只有一处该提前**：噪声加固里的"DH 编码改到 RFC 7748"
不是加固的第三步，而是**已经可以立刻做完、并且是本轮唯一能用官方向量验收的一项**。
理由：

1. 它与壳、与多用户、与私钥存储**零耦合**（只动 `crypto_prims.py`）；
2. 它是"换 vetted 实现"的**前置条件**：编码不对，官方向量必然跑不通，
   而"换成 snow/noiseprotocol"会以失败告终（实现路径文档 §7 原本就写明了这条顺序）；
3. 实测它确实是个真缺陷，且现有用例发现不了（见 §2.1）。

已于本轮完成（`85b55b3`）。其余顺序维持原提议，理由分别是：

| 顺序 | 为什么必须在这个位置 |
| --- | --- |
| P1 壳侧主体上下文最先 | 它是"授权"有意义的前提：没有"谁在调用"，插件只能退化为"本机身份即操作者"，而所有 Companion 插件都要复用它 |
| 私钥保护第二 | 当前明文落盘是最高危偏离，且它决定了 P1 之后"凭据"能存成什么形态（DPAPI 保护的文件 vs keyring 条目） |
| Noise 换 vetted 实现第三 | 在把协议暴露给更多插件之前；编码前置已完成，这一步现在可以独立推进 |
| 名单自动分发第四 | 它要顺带承载"已签名名单的中转"，而那需要一个能被信任的中转方 —— P1 的主体上下文正是"谁能中转"的判据 |
| 内容寻址最后 | 它是传输层特性，与上述四项无依赖；当前 offset/length 分块在功能性上够用 |

**一处顺序提醒**：如果你想把"名单自动分发"提前，它会**依赖 P1**。
分配名单的载体是注册记录（设计文档 §7.3），而"谁能写注册记录、谁的中转可信"
在没有主体上下文时无法判定，只能做成"谁都能发"。

---

## 2. 缺陷与文档不一致清单（实测复核）

### 2.1 已修：X25519 编码不是 RFC 7748（高危，静默）

* **现象**：`docs/group-mesh-design.md` §4.4 自己就标注了这一点，但它比"不互通"更严重：
  `dh_public_bytes()` 用 `pointQ.x` 大端当线格式，`dh()` 又按大端把字节读回来，
  **两处错误互相抵消** —— 于是 `test_dh_agrees_both_directions` 这类"两端一致"
  的用例全绿，而 RFC 7748 §6.1 的两组官方向量**全部失败**。
* **附带的第二个错误**：`_raw_to_x25519_point()` 没有按 RFC 7748 §5 屏蔽 u 坐标
  最高位。不屏蔽时本端对 `u + 2^255` 做点乘、按规范的对端对 `u mod p` 做点乘，
  结果不同且两边都不报错。
* **修复**（`85b55b3`）：线格式统一为 §5 的 32 字节小端；解码屏蔽最高位；
  私钥一律经 `ECC.construct(seed=...)` 还原。
* **修复中发现的第三件事**（原文档没有）：私钥**不能**手工转成整数做标量乘。
  库里 `d` 是按曲线阶 ℓ 归约的 `ClampedInteger`，普通 `int` 走另一条路径，
  同一对密钥会算出**不同**的共享秘密且不报错。三条都写进了
  `crypto_prims.py` 文件头与实现路径文档 §5.3。
* **验收**：`tests/test_group_mesh_mvp.py::X25519Rfc7748VectorTest`（5 例，
  期望值逐字节抄自 RFC，不来自本实现输出）。

### 2.2 已修：陈旧 `dh_public` 会让老身份目录读不出来（高危，升级即触发）

* **现象**：`Device.from_dict()` 把"落盘 `dh_public` 与派生值不符"判为"文件被改动"
  并抛 `RecordError`。编码一改，**磁盘上所有旧身份文件都不符**。
* **实测后果**（不是推演，本轮真实踩到）：`tests/test_group_mesh_shell_e2e.py`
  两例失败，报 `{'ms': 7, 'ok': False, 'peers': 0}` 与「我的地址」渲染不出来 ——
  真实表现是"插件加载正常、节点永远起不来、界面停在正在读取设备"。
* **更值得记的一点**：本仓库 `data/group-mesh` 里那份 `dh_public`
  **既不是现行编码，也不是大端遗留**（实测与 `pointQ.x` 大端互不相同），
  因此"加一条大端兼容分支"是不够的。
* **修复**：该字段只是派生结果的冗余副本、不参与任何密码学计算，因此**派生值
  才是权威** —— 不符时记 warning 并采用派生值；只有长度不对（32 字节以外）
  仍按结构性错误拒绝。完整性由 Ed25519 私钥↔公钥一致性检查负责。

### 2.3 待修：共享项容量上限在界面上是写死的 1 GiB

* **文档**：设计文档 §13 第 2 项写"由属主在设置面板中设定，留空表示不限制"，
  并自标 `[部分]`：`settings_schema` 没有该项，界面把 1 GiB 写死。
* **代码**：标签说的是真的 —— `plugins/group-mesh/frontend/js/app.js:348` 直接
  `max_bytes: 1024 * 1024 * 1024`；`settings_schema` 里确实没有该项。
* **后果**：后端支持 `0 = 不限制`，但用户没有任何入口；共享列表把 1 GiB 显示成
  "容量上限"，用户会以为这是协议上限。
* **收敛**：加一个 `share_max_mb` 设置项（0 = 不限制），前端读它而不是写死常量。
  改动小，属于 P1 的"权限档位落地"同一片区域。

### 2.4 待修：`minShellVersion` 只在门禁里读，运行时不校验

* **文档**：设计文档 §12 第 6 项自标 `[未实现]` —— 与代码一致，不是不一致，
  但它是**唯一一个"门禁通过、运行时静默降级"**的项：
  `tools/check_plugins.py` 校验格式，`manifest.json` 声明 `1.2.0`，
  而壳加载时不比较版本，因此"插件声明需要新壳"这件事在运行时没有任何效果。
* **影响面**：随 Companion 插件出现会立刻变成真实问题（子插件要的新壳能力
  不存在时只会以 AttributeError 暴露）。

### 2.5 待修：`/file` 与 `/thumbs` 没有主体级检查点（P1 第 3 项）

* **现状**：两条路由只做"令牌 + 路径安全 + 受保护判定"，没有任何主体概念
  （`shell/backend/file_server.py` 的 `serve_media_file` / `serve_thumb`）。
* **为什么现在必须做**：group-mesh 的**物化缓存**（`<cache>/remote/<设备>/<共享>/…`）
  会经 `/file` 端给界面。也就是说，远端共享内容进入本机 HTTP 之后，
  任何持令牌者都能按路径读走 —— 而"谁能看哪个共享项"的判定（ACL）在协议侧，
  HTTP 侧完全不知道。这是 P1 里最实在的一条，不是形式主义。

### 2.6 待修：`settings_save` 不限权（P1 第 5 项）

* **现状**：`system_settings_save` 只校验令牌。设计文档 §12 第 5 项自标 `[未实现]`。
* **与文档一致**，但值得单列：group-mesh 的 `bind` / `port` / `download_dir`
  被任何持令牌者改掉就等于改掉节点的对外行为。

### 2.7 文档不一致：`/thumbs` 的"令牌"措辞

* **文档/注释**：`auth.py` 文件头写"`/api`、`/file`、`/thumbs` 等数据路由会携带
  用户数据，必须持有令牌才能访问"。
* **代码**：`/thumbs` **在**令牌路由内（`_OPEN_ENDPOINTS` 只有
  `health` / `serve_shell` / `serve_shell_assets` / `serve_plugin_frontend`），
  因此这条措辞是对的。
* **真正不一致的是另一条**：`serve_plugin_frontend`（`/plugins/<name>/frontend/*`）
  **免令牌**，而它会把插件前端目录里的任意文件端出去。当前靠
  `_reject_protected_file` 挡住凭据文件，因此**不算漏洞**，但"插件前端是免令牌路由"
  这一事实没有写在 `auth.py` 的说明里，排查时容易误判。

### 2.8 文档不一致：设计文档 §15.1 引用的壳侧缺陷已修

* **文档**：§15.1 末尾写"额外根的虚拟相对路径在 `/file` 里只按第一根解析，
  是壳侧既有缺陷"。
* **代码**：`c60eedd`（feat(shell): /file 交给插件解析虚拟路径，修额外根原图 404）
  已经把 `/file` 接到 `PluginBase.resolve_file_path()`，该缺陷已修；
  文档那句停留在修之前。**需要改文档**（group-mesh 本身不覆写
  `resolve_file_path`，多根的是 image-viewer）。

### 2.9 文档遗漏：§4.5 的 `features` 表没有说"写权限无需协商"

* **代码**：`shell/groupmesh/transport.py` 的 `FEATURES` 是
  `roster-v1 / registration-v1 / share-read / share-range`，**没有** `share-write`。
* **事实**：写能力已经落地（§6.2 的 `write` ACL + §6.4 的暂存/提交/上传），
  但它**不做能力协商** —— 一个只实现 `share-read` 的对端照样会被服务端按 ACL
  判定为"可写"。文档 §4.5 只说"原示例里的 `share-write` 尚未实现"，
  读者会以为写能力整体没落地。
* **收敛**：要么在文档里写清"写能力不参与协商，按服务端 ACL 判定"，
  要么给 `write` 加一个 feature 门（我倾向前者：能力协商解决的是"对端懂不懂
  这个协议扩展"，而写请求的语义本身就是 `op=write`，不懂的对端不会发）。

### 2.10 文档遗漏：`add_device()` 无调用方（§16 P2.5 已列，但 §4.1 没交叉引用）

* **代码**：`identity.py` 的 `add_device()` 没有任何调用方（CLI 也没有），
  设计文档 §0.3 的 §4.2 行写的是"`add_device()` 无调用方"，是对的；
  但 §4.1 的"主体与设备分离 `[已实现]`"会让人以为多设备可用。
* **建议**：§4.1 的状态改成 `[部分]`，指向 §4.2/§16 的 P2.5。

### 2.11 未定为缺陷、但值得决策的两项

* **`op=hello` 从不被调用**：`node.py` 注册了 `hello` 并返回内核版本、
  共享项清单，但 `client.py` 没有对应函数，插件层也不发。它是"内核版本协商"
  的现成挂点 —— P1 的 `minShellVersion` 运行时校验可以顺带用它（对端内核
  版本不满足时给出明确理由，而不是让后续请求失败）。
* **`import_x25519_public_key` 会拒绝合法公钥**：pycryptodome 的导入路径对
  "不在曲线上的 u"（twist 上的点）返回错误，而 RFC 7748 允许。本项目
  `_point_from_wire()` 走的是同一条底层构造，因此**可能**拒绝对端合法公钥。
  本次联调两端都是本实现、都是自己的密钥，因此没暴露。需要一条用例确认
  （用 RFC 7748 §6.1 的 u 之外的取值构造），确认后再决定是否容忍。

---

## 3. P1 实施方案（按主体颁发令牌）

### 3.1 凭据 → 主体的映射

现状：壳只有一枚**进程级全局令牌**（`<config>/auth_token.txt`），
`_require_token()` 只问"令牌对不对"，因此"谁在调用"在结构上不可知。
方案取"按主体颁发令牌"：

```
<config>/principals.json
{
  "principals": [
    {
      "id": "owner",
      "name": "本机",
      "role": "owner",                                  // owner / admin / member
      "source": "enrolled",                             // 见下
      "secret_sha256": "<hex>",                         // 令牌的 SHA-256，不存明文
      "public_key": "<base64 Ed25519 公钥>" | null,      // 有则与 group-mesh 主体对齐
      "created_at": 1767225600
    }
  ]
}
```

* `source` 三值：`auth-token`（由现有全局令牌自动登记，向后兼容）、
  `enrolled`（在设置面板里登记新主体）、`imported`（从 group-mesh 身份导入公钥）。
* 令牌**只存哈希**：`principals.json` 与 `auth_token.txt` 同级、同权限（0600），
  且必须加进受保护清单（`protected_paths.py`）。
* 判定恒定时间比较（复用 `auth.token_matches` 的写法）。

### 3.2 壳侧注入

```python
# shell/backend/principal.py
CURRENT_PRINCIPAL: ContextVar[PrincipalContext | None] = ContextVar('omnibox_principal', default=None)

@dataclass(frozen=True)
class PrincipalContext:
    id: str
    name: str
    role: str
    source: str
    public_key: bytes | None
```

`file_server.py` 的 `_require_token()` 改为：**先解析凭据 → 再设置 ContextVar**。
三处约束逐条对应设计文档：

1. **只从凭据取主体**：绝不读 `request.args` / JSON 里的 `principal` 字段；
2. **每次请求现设现清**：`before_request` 里 `set()`，`teardown_request` 里
   `reset()` —— 只 set 不 reset 会让主体泄漏到同一执行上下文的后续调用
   （生产环境每请求一个上下文时看不出来，但复用上下文的调用会看到
   "上一个请求的主体还在"，那比没有主体更危险）；
3. **未登记令牌**：401，而不是"降级成匿名主体"。

**实施状态（2026-09-17）**：已落地，见 `2513bb4`。自举采用
"凭据表为空时把既有 `auth_token.txt` 登记为 owner"，因此老部署升级后令牌继续
可用，而"这次调用是谁"从第一天就有答案（已有记录时**不**覆盖用户改过的角色）。

### 3.3 插件侧读取

```python
# shell/backend/plugin_base.py
def current_principal(self) -> PrincipalContext | None:
    """本插件的 API 被调用时的主体（由壳注入）；后台线程里为 None。"""
```

* 返回值是 `None` 而不是抛异常：CLI / 自检 / 后台线程都需要"没有主体"这个状态；
* 但**授权判定**必须显式处理 `None`（默认拒绝），因此 P1 同时提供
  `require_principal()`（`None` 即抛 `PermissionError`）给需要强制的地方。

### 3.4 检查点（P1 第 3/5 项）

| 路由 | 检查 |
| --- | --- |
| `/file`、`/thumbs` | 主体存在才放行；插件可用 `authorize_file(principal, path) -> bool` 钩子按主体判（默认全放行，group-mesh 覆写为"物化缓存只有属主与共享项 ACL 允许的主体可读"） |
| `system_settings_save` | 只允许 `role in (owner, admin)` |
| `minShellVersion` | 加载时比较 `manifest.minShellVersion` 与壳版本，不满足拒绝加载并给出原因（§12 第 6 项） |

`authorize_file()` 的默认实现必须是"放行"，否则 P1 会静默打断现有插件
（它们没有主体概念）；把收紧的责任交给需要它的插件，并且**只收紧不放松**
（壳先做路径安全与受保护判定，钩子只能拒绝）。

### 3.5 验收方式（可复核）

1. **伪造参数被忽略**：带 `{"principal": "<别人的 id>"}` 的请求与不带该字段
   的请求得到**同一个** `current_principal()`；
2. **后台线程无主体**：`threading.Thread` 里读 `current_principal()` 为 `None`；
3. **未登记令牌 401**，已登记令牌拿到对应主体；
4. **非管理员改设置被拒**（403），管理员通过；
5. **`minShellVersion` 高于当前壳的插件被拒绝加载**并给出原因；
6. 两条路由的 `authorize_file()` 钩子拒绝时返回 403，且**不得**影响未覆写该钩子的插件。

新增用例建议放 `tests/test_shell_principal.py`，另在
`tests/test_group_mesh_plugin.py` 里补"物化缓存按主体判权"。

---

## 4. 建议的落地顺序（本轮之后）

| # | 项 | 依赖 | 规模 |
| --- | --- | --- | --- |
| 1 | P1：`principals.json` + ContextVar + `current_principal()` | 无 | 中 |
| 2 | P1：`/file`、`/thumbs` 的 `authorize_file()` 检查点 | 1 | 中 |
| 3 | P1：设置写入限权 + `minShellVersion` 运行时校验 | 1 | 小 |
| 4 | 共享项容量上限进设置面板（§2.3） | 1（限权后才有意义） | 小 |
| 5 | 私钥保护：Windows DPAPI / Linux keyring（§4.1） | 1 | 中大 |
| 6 | Noise 换 vetted 实现 + 官方握手向量（编码前置已完成） | 无 | 大 |
| 7 | 名单自动分发（注册记录承载） | 1 | 中 |
| 8 | 内容寻址分块传输（§10） | 无 | 大 |

第 1–4 项同属 P1，建议一个提交一件事，按上表逐项落地。
