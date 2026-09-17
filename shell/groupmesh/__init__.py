"""group-mesh 协议内核（MVP）。

本包是 `docs/group-mesh-design.md` 的**可运行验证内核**，不依赖 Shell、不依赖 GUI，
Windows 与 Linux 上可直接用同一个 Python 跑。它只实现设计文档中"应用面"的骨架：

    身份（Ed25519）→ 团体名单（群主/管理员签发）→ 共享项声明（属主签发）
        → 注册记录（设备自签）→ Noise_XX 握手 → 加密会话 → 请求/应答

**不在本包范围内的**：房间 / 语音 / 游戏面（由 Companion 子插件承担，见
`docs/group-mesh-companions.md`）、Android 轻客户端、内容寻址分块传输。
壳侧主体上下文（见设计文档 §12）由 `shell/backend/principal.py` 提供，
本包不依赖它，因此内核可以脱离壳单独跑。
"""

__version__ = '0.1.0'

# 协议主版本。不兼容的线上字节变更必须递增它 —— 递增的收益是**错误信息可读**：
# `Negotiation.resolve()` 会直接回一句"协议主版本不一致（本地 X，对端 Y），请升级
# 后再连"，而不是让两端在握手末尾报一句 MAC 校验失败。
#
# v1 → v2（2026-09-17）：X25519 的公钥与共享秘密编码改为 RFC 7748 §5 的 32 字节
# 小端（v1 用的是 pycryptodome `pointQ.x` 的大端整数，两者互为字节序反转）。
# v1 与 v2 节点**无法**互通，因此必须靠这个字段把"版本不同"与"握手出错"分开。
PROTO_VERSION = 2
DEFAULT_SUITE = 'noise-XX-25519-chacha20poly1305-blake2s'
KNOWN_SUITES = (DEFAULT_SUITE,)
