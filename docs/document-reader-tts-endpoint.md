# 朗读端点契约（OpenAI 兼容）

状态：**已实现**（document-reader 的 `tts_engine.py`）。适用于"自建本地模型服务"这一档朗读：
插件只当客户端，**不含模型**。端点由使用者自行部署（Kokoro-FastAPI、GPT-SoVITS、Qwen3-TTS
一类自定义音色服务，或云 API），地址填在设置项 `tts_base_url`。

回归用例：`tests/test_document_reader_tts.py`（`EngineDispatchTests` / `VoiceTableTests`，
用本地假端点覆盖失败分支）。

## 1. 必需项

### 1.1 路由

```
POST <tts_base_url>/v1/audio/speech
```

`tts_base_url` 是**根地址**，插件无条件在其后拼 `/v1/audio/speech`（`tts_engine.py:199`）。
因此 `/v1` 或完整接口路径都不能填进这一项：前者得到 `/v1/v1/audio/speech`，
后者得到重复路径，两者都只会收到 404。

### 1.2 请求

| 字段 | 是否总是发送 | 说明 |
| --- | --- | --- |
| `input` | 是 | 已切好的句子。前端按 240 字切句（`tts.py` 的 `tts_segment`），后端再限 4000 字（`MAX_CHARS`） |
| `voice` | 是 | 端点自己的音色名，默认 `alloy` |
| `response_format` | 是 | 默认 `mp3` |
| `speed` | 是 | 默认 `1.0`（`params.get('speed') or 1.0`，0 会被兜成 1.0） |
| `model` | 否 | 只有设置项 `tts_model` 非空才发送；留空即由端点选默认模型 |

未实现的字段应当忽略，不要返回 422：插件没有"按端点能力裁剪字段"的协商逻辑。

`speed` 必须被解释为**正数**：端点若不校验，`speed <= 0` 会直接失败（实测 Qwen3-TTS 对
`speed=0.0` 返回 500 `rate must be a positive number`）。

认证头只在设置项 `tts_api_key` 非空时发送：

```
Authorization: Bearer <tts_api_key>
```

### 1.3 回应

- 状态码必须 `< 400`。`>= 400` 一律按失败处理，错误体前 200 字原样进入界面提示。
- 响应体必须非空（空体报"端点返回了空音频"）。
- `Content-Type` 可选，但建议给出：含 `wav` / `ogg` / `opus` / `mpeg` / `mp3` 时据此决定
  缓存文件后缀，无法识别时按请求的 `response_format` 推断（只影响文件名，不影响播放）。

### 1.4 超时

连接 5 秒、读取 120 秒（`timeout=(5, 120)`）。慢于此的端点表现为"点朗读后长时间无响应"，
最终降级到下一档引擎。同一引擎最多尝试 3 次（`ENGINE_ATTEMPTS`），间隔 0.4 秒递增，
耗尽后才降级。调小服务端的分块长度可降低单次合成的耗时。

## 2. 可选项：音色清单

```
GET <tts_base_url>/v1/voices[?model=<tts_model>]
```

这一路由**不是 OpenAI 规范的一部分**，属于扩展。缺它不影响朗读，只影响朗读设置页那张卡片：
有清单时列出音色名供点选，没有时卡片保留并提示可以手填名字。

- 认两种形状：`{"voices": ["vivian", …]}` 与 `{"data": [{"id": "vivian"}, …]}`；
  数组元素为字符串，或含 `id` / `name` 的对象。
- 设置项 `tts_model` 非空时携带 `?model=`；该请求返回 5xx 时摘掉 `model` 再问一次
  （Qwen3-TTS 对未知模型名未接住异常，返回 500）。
- 返回 401/403 只在日志留一行"检查 API Key"，不作为错误上抛。
- 清单按端点缓存 5 分钟（`VOICE_CACHE_TTL`），失败不写缓存。

**清单必须如实列出端点支持的音色名**：自动模式下插件用它与 `order` 求交集来判定
"哪个引擎拥有这个音色"（`tts_engine.py` 的 `engine_has_voice`），只剔除"问到了清单且确实没有
这个音色"的引擎。清单漏列某个可用音色，会导致该音色被跳过。

## 3. 不依赖的能力

- 不要求流式：不使用 `stream_format`，不解析 SSE。
- 不解析时间戳：这一档固定返回空 `marks`，朗读时只有整句高亮；字级跟随仅 edge 档具备。
- 不要求 OpenAI 官方模型名或音色名，`voice` 由端点自行定义。

## 4. 最小实现

```python
@app.post("/v1/audio/speech")
def speech(req: dict):
    audio = my_tts(req["input"], req.get("voice"), req.get("speed", 1.0))
    return Response(content=audio, media_type="audio/mpeg")
```

`plugins/document-reader/backend/tts_engine.py` 中对应的实现为
`openai_synthesize()`（合成）与 `endpoint_voices()`（清单）。
