"""group-mesh 协议内核（MVP）。

本包是 `docs/group-mesh-design.md` 的**可运行验证内核**，不依赖 Shell、不依赖 GUI，
Windows 与 Linux 上可直接用同一个 Python 跑。它只实现设计文档中"应用面"的骨架：

    身份（Ed25519）→ 团体名单（群主/管理员签发）→ 共享项声明（属主签发）
        → 注册记录（设备自签）→ Noise_XX 握手 → 加密会话 → 请求/应答

**不在本包范围内的**：房间 / 语音 / 游戏面（由 Companion 子插件承担，见
`docs/group-mesh-companions.md`）、Android 轻客户端、内容寻址分块传输、
壳侧主体上下文（见设计文档 §12）。
"""

__version__ = '0.1.0'

PROTO_VERSION = 1
DEFAULT_SUITE = 'noise-XX-25519-chacha20poly1305-blake2s'
KNOWN_SUITES = (DEFAULT_SUITE,)
