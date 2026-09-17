# 测试夹具来源与许可

本目录存放第三方测试向量，供单元测试使用。第三方内容一律在此登记来源、提取方式
与许可；新增夹具时必须补充条目。

## noise_XX_25519_ChaChaPoly_BLAKE2s.json

- **内容**：Noise 官方测试向量中 `Noise_XX_25519_ChaChaPoly_BLAKE2s` 一项的精简
  子集，只保留 `tests/test_group_mesh_noise_vectors.py` 使用的字段
  （`protocol_name` / 双方 prologue、static、ephemeral / `handshake_hash` /
  `messages`）。
- **原始来源**：cacophony 项目的 `vectors/cacophony.txt`
  （<https://github.com/centromere/cacophony/blob/master/vectors/cacophony.txt>）。
  该文件是 Noise 协议测试向量的生成来源之一。
- **获取路径**：从 `noiseprotocol` 仓库测试目录中的副本下载
  （<https://github.com/plizonczyk/noiseprotocol/blob/master/tests/vectors/cacophony.txt>），
  其 `tests/vectors/README.md` 注明该文件来自上面的 cacophony 仓库。
- **提取方式**：下载 `cacophony.txt`（JSON 数组），取出
  `protocol_name == "Noise_XX_25519_ChaChaPoly_BLAKE2s"` 的那一项，保留上述字段。
  **所有十六进制值保持原样，未做任何重算或截断**；`messages` 的 6 条消息
  （3 条握手 + 3 条传输）完整保留。
- **提取日期**：2026-09-18。
- **许可**：cacophony 以 **Unlicense** 发布，内容属于公有领域
  （<https://github.com/centromere/cacophony/blob/master/LICENSE>）。可自由复制、
  修改、再分发，无需署名；本说明仍保留来源以便复核。
- **为什么不能现算**：这份夹具提供的是**独立于本项目实现**的期望值。若改用
  `noiseprotocol` 现算的输出做断言，就变成"用被测实现验证被测实现"，无法发现
  包装层接错密钥、prologue 或 nonce 的问题。

## 重新生成

需要更新向量时，按上面的"获取路径"下载 `cacophony.txt`，用任意 JSON 工具筛选并
保留相同字段即可；不要手工改动十六进制值。
