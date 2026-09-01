# media-player 设计

> 本文档介绍插件**当前实现**的设计与数据流，非变更记录；不包含已移除的历史方案。

## 概述

统一音频 / 视频媒体库插件（`plugins/media-player`），单索引管理两类媒体：

- 视图：最近播放、音乐专辑、全部音乐、视频专辑、全部视频、我的喜欢、歌单详情；
- 播放：底部播放栏 + 舞台（音乐封面 / 视频画面）、顺序 / 随机 / 单曲循环、画面 / 仅声音切换、
  宽屏模式、全屏（视频全屏 + 控件自动隐藏；音乐全屏即沉浸式歌词页）、快捷键；
- 扩展：混编歌单（音视频同单）、沉浸式歌词页（LRC + 频谱可视化）、10 段均衡器与预设、
  网易云音乐在线歌单 / 每日推荐（经 `netease-music` 插件桥接）。

## 目录结构

```
plugins/media-player/
├── manifest.json
├── eq-presets/            # 均衡器内置预设（JSON）
├── backend/
│   ├── main.py            # MediaPlayerPlugin：API 入口、状态持久化、ThumbCache 接入
│   ├── scanner.py         # 增量扫描（音频按标签聚合专辑，视频按目录聚合）+ cover_generator
│   ├── metadata.py        # 音频标签 / 内嵌封面（mutagen）
│   ├── models.py          # MediaItem / MediaAlbum
│   ├── video_meta.py      # 视频时长探测（mutagen 轻量解析，mp4/m4v；mkv 等返回 None）
│   └── video_ffmpeg.py    # 视频封面 ffmpeg 后端抽取（可选通道，见「封面生成」）
└── frontend/
    ├── index.html
    ├── media-player.css
    └── js/
        ├── utils.js            # MPUtils：封面 URL（含 mtime 版本号）、降级、格式化
        ├── frame-extractor.js  # 视频封面前端 canvas 抽帧器（并发 2 worker）
        ├── lyrics-parser.js    # LRC 解析 + 沉浸式歌词页
        ├── progress-store.js   # 播放进度记忆（localStorage，兼容旧键迁移）
        ├── player-core.js      # 音频/视频播放核心 + EQ + 状态保存
        ├── playlist-manager.js # 歌单管理
        └── app.js              # 主应用（视图、舞台、全屏、预取、快捷键）
```

## 媒体索引与扫描

### 索引模型

- `MediaItem` 为统一条目（`kind: audio | video`），id 为绝对路径的 md5 前 16 位；
- 索引缓存 `root_dir/.cache/media_index.json`，带 `INDEX_VERSION`（当前 v4），结构升级时自动
  重建；增量扫描以 mtime/size 未变复用缓存条目；
- 状态文件 `media_state.json`：`favorites / recent / playlists / playback` 四段（见「状态持久化」）；
- 任务文件 `scan_task.json`：后台扫描任务的断点持久化（见下）。

### 扫描任务（BackgroundTask）

复用 Shell 共享基建 `shell/backend/tasks.py`（线程 + 进度 + 取消 + 原子持久化）：

- `media_scan(force)` 启动后台任务；「🔄 扫描」为增量（默认，只处理新增/变更文件），
  「⚡ 深度扫描」为全量（`force=True` 重读全部标签与时长）；已运行时返回 error；
- **断点续传**：worker 每完成一个根目录，将「部分索引 + `completed_roots`」落盘检查点；
  进程中断后重启任务恢复为 `paused`，再次增量扫描自动跳过已完成根目录；
- 多根目录：`root_dir` + `media_dirs` 逐根扫描，各根以目录名作 `namespace` 前缀聚合；
- 增量语义：mtime/size 未变直接复用；目录封面图比媒体文件新时强制重建条目（刷新 `has_cover`）；
  索引文件丢失而任务文件残留时，断点信息失效、降级全扫；
- 深度扫描完成后清理孤儿封面条目（`ThumbCache.prune`，仅删除已从索引消失的文件对应缓存）。

### 专辑聚合

- 音频：按「专辑 + 专辑艺术家」标签聚合（键 `namespace//album::<album>||<artist>`）；
  无标签回退到目录名；专辑内按曲目号排序；
- 视频：按相对目录聚合（键 `namespace/<rel_dir>`）；专辑内按标题排序；
- 专辑响应仅含元信息（名称 / 艺术家 / 数量 / 总时长 / `cover_item_id`），**不携带条目列表**
  ——大媒体库下避免每次专辑视图传输全库 JSON；详情由 `media_album_items` 单独获取。

### 状态迁移

首次扫描完成后，若存在旧版状态文件 `music_state.json`（旧插件收藏 / 最近播放 / 歌单 /
播放状态），按「相对路径 → 新 md5 id」映射后并入 `media_state.json`，成功后旧文件改名为
`.migrated`；索引尚未建立时推迟到首次扫描完成。

## 文件访问

媒体文件以「绝对路径 + URL 编码」经 `/file?path=<path>&plugin=media-player` 访问；
`get_file_roots()` 声明 `root_dir` 与 `media_dirs` 全部根目录，Shell 逐根做路径安全检查，
支持跨磁盘 / 多目录媒体库。封面不落散文件，统一走 `/thumbs/<item_id>?plugin=media-player`。

## 封面生成

### 分层策略

视频封面按成本从低到高三级，前一级不可用时降级到下一级：

```
1. 目录封面图（cover/folder/poster/fanart/thumb/backdrop，零成本）
2. ffmpeg 后端抽帧（可选通道：设置 ffmpeg_path 或 PATH 检出；无需打开页面）
3. 前端 canvas 抽帧兜底（浏览器解码，与播放能力严格对齐）
```

音频封面：内嵌封面（mutagen，mp3/flac/m4a）→ 同目录封面图。扫描期只检测 `has_cover`
标记（`has_embedded_cover` / 目录封面存在性），不读取字节；实际字节由 ThumbCache 按需生成。

### ThumbCache（SQLite 缓存）

复用 Shell 共享基建 `shell/backend/thumb_cache.py`，DB 位于 `root_dir/.cache/thumbs.db`：

- 键为条目 id，`source_mtime`（0.5s 容差）+ `source_size` 双条件失效校验——源文件替换后
  条目自动失效并重新生成；
- 单图按需生成：`/thumbs/<id>` 首次命中时同步调用注入的 `cover_generator`，失败返回 404
  （不缓存假缩略图），前端据此降级；
- `put()`：前端 canvas 抽帧 / ffmpeg 结果的直入库接口（同带源文件 mtime/size 失效语义）；
- `has_many()`：单连接批量判定缺失项（`thumb_missing` 使用，替代逐 id 建连）。

### 通道 A：后端 ffmpeg（video_ffmpeg.py）

- 路径定位语义：配置了 `ffmpeg_path` 就用配置（支持文件 / 目录 / 漏写 `.exe`，无效路径
  视为不可用，不静默回落）；留空才检测 PATH；探测结果缓存，`media_ffmpeg_status`（force）
  可重新探测供用户验证；
- 抽帧：`ffmpeg -ss <t> -i <video> -frames:v 1 -q:v 3 -f image2pipe -` 输出 JPEG；
  `-ss` 前置为关键帧快速 seek（缩略图足够，成本远低于整段解码），30s 超时；
- 取帧位置：时长已知取 10%（钳 [0.5, 120]）；时长未知（如 mkv）依次降级尝试 10s / 3s / 1s，
  覆盖短视频；
- 失败返回 None，由前端抽帧兜底；`cover_generator` 中优先级低于目录封面图。

### 通道 B：前端 canvas 抽帧器（frame-extractor.js）

浏览器侧兜底通道，与播放共用解码器。要点：

- **并发与去重**：2 个 worker（隐藏 `<video>` 元素池）+ 按 itemId 合并（多张图同时 404 只
  抽一次）；队列上限 100，超出丢弃队尾（其封面直接降级）；
- **优先出图**：`priority=true` 请求（播放中视频封面）插入队首；已排队条目收到 priority
  请求时同样移到队首；
- **零额外解码**：目标视频正在画面模式播放时，直接 `drawImage` 主 `<video>` 取帧；
- **页面隐藏暂停**：`document.hidden` 时暂停派发（隐藏时 rAF 不触发、抽帧必失败），
  `visibilitychange` 恢复后继续；
- **失败退避**：硬失败（含重试）后 10 分钟内不再尝试该条目，直接降级，坏文件不反复空转；
- **跨标签页去重**：localStorage 抽帧锁（60s TTL），其他标签页正在抽同一视频时软失败跳过；
  抽帧前先查一次 `thumb_missing`，缓存已被其他来源写入则直接回填、跳过解码；
- **取帧质量**：已知时长 seek 到 10%（钳 [0.5, 120]），未知时长依次尝试 10s / 30s / 60s，
  黑帧检测（64px 采样亮度）自动换位置，避免片头黑场封面；所有等待（loadedmetadata /
  seeked 15s、双 rAF 3s）带超时防队列死锁；
- **回填**：抽帧成功把 JPEG data URL 直接赋给等待的 `<img>`（省掉回读 `/thumbs` 一次往返），
  `media_put_thumb` 异步写库供后续浏览命中缓存；回读路径失败保留 fallback 兜底，不留破图；
- 抽帧失败最终降级为 emoji 占位（`MPUtils.fallbackCover`）。

### 预取流程（app.js）

- 渲染完成后，视口内（含 500px 预读边距）的封面立即预取，其余由 IntersectionObserver
  滚动触发，250ms 防抖合并批量；
- 批量调 `media_thumb_missing`（后端单连接判定，上限 200 条）只对**未缓存**条目入抽帧队列，
  避免重复生成；查询失败降级为全部尝试；
- 去重集合设 3000 上限防无限增长。

### 失效与浏览器缓存

- 后端：ThumbCache 按源文件 mtime/size 失效（含 `put` 回写条目），源文件替换后自动重生成；
- 前端：`coverUrl` 生成 `/thumbs/<id>` 地址时携带 `&v=<源文件 mtime>`——文件替换后 URL 变化，
  强制浏览器绕过 `/thumbs` 响应上的 1 天缓存，旧封面不会长期残留。

## 播放核心（player-core.js）

### 元素与模式

- 音频 `Audio()` 与视频 `<video>` 双元素，`mediaElement` 按「当前条目 kind + 画面模式」切换；
- 播放模式 0 顺序 / 1 随机 / 2 单曲循环；`next/prev` 按模式推进，单曲循环 ended 时原地重播；
- 视频支持画面 / 仅声音切换（`videoMode`），切换时在新元素上续播原位置；
- 换曲有 `loadSeq` 序号守卫，快速连点只生效最后一次；加载失败同曲重试一次，仍失败
  **只向前**跳下一首（绝不回跳），队列尽头停止；自动跳转定时器（600ms）在用户任何操作
  （播放 / 暂停 / seek / 切歌 / 停止 / 队列点击）时解除，防止延迟定时器覆盖用户选择；
- 网易云网络流：播放前按需解析 URL（`get_song_url`），解析失败不跳歌不报错，等待下次触发。

### 进度记忆与恢复

- 前端 `MediaProgressStore`（localStorage `omniboxMediaProgress`）每 2s 记录当前曲进度
  （>2s 才记），播放结束时清除；兼容旧插件 `musicProgress / videoProgress` 键的一次性迁移；
- 后端 `playback` 状态：`item_id / loop_mode / shuffle / volume / video_mode`，切歌 / 停止 /
  音量 / 模式变更时保存；启动时 `_restorePlayback` 无条件恢复音量与播放模式，条目仍存在时
  恢复播放队列（视频画面模式优先取持久化的 `video_mode`，回落 `default_video_mode` 设置）。

## 均衡器

- Web Audio 滤波链：音频 / 视频两元素共同汇入 10 段 peaking 滤波器（32Hz–16kHz，Q=1.0）
  → analyser（fftSize 512）→ destination；单元素接管失败不毒化整条链路（逐源容错）；
- 频段增益即时生效并持久化到 localStorage（`omniboxMediaEQ`）；内置预设 + 自定义预设
  （保存到 `eq-presets/`，名称做安全过滤）；
- analyser 同时供歌词页频谱可视化（72 柱，仅页面可见且 AudioGraph 运行时启动）。

## 歌词（lyrics-parser.js）

- LRC 解析：支持一行多时间标签、毫秒精度，按时间排序；当前行定位用二分查找；
- 沉浸页：逐行高亮 + 平滑滚动、可配置字号 / 对齐 / 发光 / 背景（纯色或图片）/ 模糊 /
  亮度；用户手动滚动后 2.6s 内不抢滚动；
- 加载带序号守卫：快速切歌时迟到的旧请求不覆盖新曲歌词；本地读同目录 `.lrc`
  （utf-8/gbk/gb2312 依次尝试），网易云条目经 `get_lyric` 获取；
- 未全屏时舞台内显示迷你歌词（当前句前后各两句）；音乐全屏即歌词页。

## 状态持久化与歌单

- `media_state.json` 四段：收藏（id 列表）、最近播放（id + 时间，上限 50）、歌单、
  播放状态；变更即原子写盘；
- 歌单 API：创建 / 重命名 / 删除 / 获取 / 增删条目；`playlist_save` 在未传 `item_ids`
  （仅重命名）时保留原曲目，避免误清空；
- `media_get_state` 返回收藏与最近播放的**可解析条目**（索引中已删除的 id 自动跳过）。

## 后端 API 与负载控制

- 注册于 `register_api`（`media_*` 前缀）：扫描 / 浏览 / 搜索 / 专辑 / 歌单 / 收藏 /
  最近 / 播放状态 / 歌词 / 封面回写与缺失查询 / EQ 预设 / ffmpeg 探测 / 设置读写；
- 负载控制：专辑响应不含条目列表；`stats` 用 album_key 集合计数（不构建专辑结构）；
  `thumb_missing` 单连接批量 + 200 条上限；`put_thumb` 仅接受 `data:image/` 且 base64
  3MB 上限（约 2MB 图片数据），防滥用。

## 网易云集成

- 经 `netease-music` 插件桥接：每日推荐 / 推荐歌单 / 红心歌曲 / 我的歌单 / 搜索 / 歌单详情
  / 播放地址 / 歌词；`ncm-cli` 不可用时给出安装指引视图；
- 本地缓存：每日推荐按日失效、推荐歌单与歌单详情 2 小时失效，手动刷新全量清理；
- 歌单详情「播放全部」：立即开播当前曲，其余曲目在后台逐个预解析播放地址，不阻塞 UI。
