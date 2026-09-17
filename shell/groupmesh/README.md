# group-mesh 协议内核

> 位置：shell/groupmesh（与 shell/backend、shell/frontend 并列的壳层包）
> 依赖：仅标准库 + pycryptodome；**不 import Flask / pywebview**

`docs/group-mesh-design.md` 的**可运行验证内核**。它不依赖 Shell、不依赖 GUI，
Windows 与 Linux 上可以直接跑，用来把协议先跑通、把接口试出来。

实现路径、与设计文档的偏离、以及调试中踩到的坑，见
[`docs/group-mesh-implementation-path.md`](../../docs/group-mesh-implementation-path.md)。

## 跑起来

在**本目录**下执行（`groupmesh` 是这里的包，靠当前目录进入 `sys.path`）：

```bash
# 自检：密码学、Noise 握手、名单状态机、真实 TCP 端到端
python -m shell.groupmesh.cli selftest
```

依赖只有 `pycryptodome`（已在仓库 `requirements.txt` 中）。若使用仓库的 venv：

```bash
venv/Scripts/python.exe -m shell.groupmesh.cli selftest        # Windows（仓库根）
/root/OmniBox/venv/bin/python -m shell.groupmesh.cli selftest   # Linux（/root/OmniBox）
```

## 两个节点的最小联调

```bash
# 节点 A（群主）
python -m shell.groupmesh.cli init   --dir ./a --name alice
python -m shell.groupmesh.cli create --dir ./a --group demo
python -m shell.groupmesh.cli share  add --dir ./a --share-id pub --path /srv/shared
python -m shell.groupmesh.cli serve  --dir ./a --bind :: --port 19443

# 节点 B（成员）：用 A 打印的 gm1: 邀请串加入
python -m shell.groupmesh.cli init --dir ./b --name bob
python -m shell.groupmesh.cli join --dir ./b --invite 'gm1:...'

# 群主把 B 的设备写进名单（B 自己 get_device_keys 拿到公钥）
python -m shell.groupmesh.cli roster add --dir ./a --principal <B主体公钥> --dev <B设备公钥> --name bob

# B 取文件（IPv6 地址含冒号，--target 必须写成 [地址]:端口）
python -m shell.groupmesh.cli peers --dir ./b --target '[2409:8a60:2abf:f5c0:94b8:7fff:fe0a:e9f0]:19443'
python -m shell.groupmesh.cli get   --dir ./b --target '[2409:…]:19443' \
       --share-id pub --path big.bin --output ./big.bin
```

`serve` 默认绑 `::`（同时接受 IPv6 与 IPv4）。要只监听某个地址就 `--bind <地址>`；
**建议显式指定稳定地址**，原因见实现路径文档 §4.6.1。

## Windows ↔ Linux 跨机联调

设计文档 §1.1 的目标是公网 IPv6，因此 **IPv6 那条脚本才是主路径**：

```bash
# 主路径：Linux 绑全局 IPv6，Windows 用 [2409:…]:port 连接
pwsh -File shell/groupmesh/tools/ipv6-test.ps1

# 兜底：同一网段内的 IPv4（不提供任何 NAT 穿透，只是地址族兜底）
pwsh -File shell/groupmesh/tools/lan-test.ps1
```

两者都会一条命令跑完整链路：Linux 侧建团体与共享项、Windows 加入、群主登记成员设备、
启动共享节点、取 700 KB 文件并**比对 sha256**、验证越权与越界被拒。
`ipv6-test.ps1` 还额外验证两件事：

* **节点运行期间签发的名单更新会被立即接受**（不需要重启节点）——这覆盖了一个
  真实缺陷：长驻节点若只在启动时读一次名单，新成员会被判为"不在名单里"而拒绝；
* 注册记录里写入的端点是那个全局 IPv6 地址。

两个脚本都依赖 `~/.ssh/config` 里的 `omnibox-linux` 别名（由 `OmniBox-OneClick.ps1` 维护）。

`shell/groupmesh/tools/interop_fixture.py` 验证两端的确定性：同一种子必须算出相同的密钥与签名。

```bash
python shell/groupmesh/tools/interop_fixture.py --out fixture.json     # 一端生成
python shell/groupmesh/tools/interop_fixture.py --check fixture.json   # 另一端比对
```

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `crypto_prims.py` | 原语封装：X25519 / Ed25519 / ChaCha20-Poly1305 / BLAKE2s / HKDF |
| `records.py` | 规范化序列化与签名（保证跨平台字节一致） |
| `identity.py` | 主体、设备、设备凭据、密钥绑定证明与落盘 |
| `roster.py` | 团体名单：验证规则 1–6、并发收敛、宽限提示 |
| `shares.py` | 共享项声明与 ACL 判定 |
| `registry.py` | 注册记录与注册表（seq 单调、防重放） |
| `noise.py` | `Noise_XX_25519_ChaChaPoly_BLAKE2s` 握手与传输态 |
| `transport.py` | 协商、授权、帧协议、请求应答 |
| `node.py` | 节点服务端（路径安全、分块读写、暂存提交） |
| `client.py` | 客户端封装（分块下载循环在这一层） |
| `cli.py` / `selftest.py` | 命令行入口与自检 |

## 明确的未实现项

内容寻址分块传输、Android 轻客户端、壳侧主体上下文。
逐项的状态与收敛路径见[设计文档](../../docs/group-mesh-design.md) §0、§16 与实现路径文档 §3、§4。

房间 / 语音 / 游戏面**不属于本插件**，由未来的 Companion 子插件承担，
草案见 [group-mesh 的 Companion 子插件](../../docs/group-mesh-companions.md)。
