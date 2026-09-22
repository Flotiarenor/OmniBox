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
        ├── app.js              # 类骨架：构造函数、初始化、扫描轮询
        ├── app-ui-events.js    # MediaPlayerApp 分片：UI 事件绑定、视频封面预取
        ├── app-views.js        # MediaPlayerApp 分片：扫描 / 设置 / 视图切换与加载
        ├── app-render.js       # MediaPlayerApp 分片：空状态、专辑、媒体列表与详情
        ├── app-playback.js     # MediaPlayerApp 分片：播放状态反馈、队列、歌单交互
        └── app-stage.js        # MediaPlayerApp 分片：均衡器、歌词 / 视频、全屏、快捷键
```

`MediaPlayerApp` 的 83 个成员原本集中在一个 1993 行的 `app.js` 里，按类体里已有的分节
注释拆成上面 6 个文件。分片用 `Object.assign(MediaPlayerApp.prototype, {...})` 扩回**同一个**
原型，因此成员与调用点没变、行为不变；**代价是 `index.html` 的 `<script>` 顺序变成硬约束**
（分片必须排在 `app.js` 之后、实例化之前），漏挂或错序会在装载期抛
`MediaPlayerApp is not defined`。这条契约由 `tests/js/media_player_app_split.mjs` 把关：
它按 `index.html` 的声明顺序装载全部脚本，断言无孤立脚本、无重复定义、82 个成员仍在。

## 媒体索引与扫描

### 索引模型

- `MediaItem` 为统一条目（`kind: audio | video`），id 为绝对路径的 md5 前 16 位；
- 索引缓存 `root_dir/.cache/media_index.json`，带 `INDEX_VERSION`（当前 v4），结构升级时自动
  重建；增量扫描以 mtime/size 未变复用缓存条目；
- 状态文件 `media_state.json`：`favorites / recent / playlists / playback` 四段（见「状态持久化」）；
- 任务文件 `scan_task.json`：后台扫描任务的断点持久化（见下）。

### 扫描任务（BackgroundTask）

复用 Shell 共享基建 `shell/backend/tasks.py`（线程 + 进度 + 取消 + 原子持久化）：

- `media_scan(force)` 启动后台任务；「扫描」按钮（`Icons.html('icon:refresh-cw')`）为增量（默认，只处理新增/变更文件），
  「深度扫描」按钮（`Icons.html('icon:zap')`）为全量（`force=True` 重读全部标签与时长）；已运行时返回 error；
- **断点续传**：worker 每完成一个根目录，将「部分索引 + `completed_roots`」落盘检查点；
  进程中断后重启任务恢复为 `paused`，再次增量扫描自动跳过已完成根目录；
- 多根目录：设置项 `media_roots`（**媒体文件夹**列表，多行路径）逐根扫描，第一行
  既是数据根（缓存 / 索引 / 缩略图库落点）又是扫描根，其余只作额外扫描根，各根以
  目录名作 `namespace` 前缀聚合；旧配置的 `root_dir` + `media_dirs` 仍读得出来
  （`_configured_roots()`：新键优先，为空才回退旧键）；
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
`get_file_roots()` 声明 `media_roots` 列表里的全部根目录，Shell 逐根做路径安全检查，
支持跨磁盘 / 多目录媒体库。封面不落散文件，统一走 `/thumbs/<item_id>?plugin=media-player`。

设置页的「媒体文件夹」列表是 Shell 共享组件（`settings_schema` 的
`type:"directory"`，实现见 `docs/plugin-guide.md` §7.2），与图片相册的
「图片文件夹」是同一份代码，插件本身不含目录选择器实现。

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
  视为不可用，不静默回落）；留空才检测 PATH；探测结果缓存，`media_ffmpeg_status`
  （**无参数**；调用即内部 `force=True` 重新探测，供用户验证）;
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
- **随机播放**不是每次取随机下标，而是整条队列一份随机排列（`_shuffleOrder`）加游标
  （`_shuffleCursor`）：`next/prev` 沿同一份排列进退，因此「上一首」按已播路径原路返回、
  不跳到任意位置；走到排列末尾重新生成一份并避开刚播完的条目，已在排列开头再后退则停在
  首位；直接选曲只把游标移到该条目在排列中的位置，排列不变（同一份顺序下进退一致）；
  队列内容变化、切换播放模式、停止、恢复播放时排列失效并按下一条目重建；
- 视频支持画面 / 仅声音切换（`videoMode`），切换时在新元素上续播原位置；
- 换曲有 `loadSeq` 序号守卫，快速连点只生效最后一次；加载失败同曲重试一次，仍失败按本次
  导航方向继续找相邻可播放条目：**下一首 / 直接选曲向后**、**上一首向前**（`_loadNavDir`，
  由 `playIndex(index, autoplay, navDir)` 传入），该方向已无可播放条目则停止；
  自动跳转定时器（600ms）在用户任何操作
  （播放 / 暂停 / seek / 切歌 / 停止 / 队列点击）时解除，防止延迟定时器覆盖用户选择；
- 网易云网络流：播放前按需解析 URL（`get_song_url`），解析失败不跳歌不报错，等待下次触发。

### 进度记忆与恢复

- 前端 `MediaProgressStore`（localStorage `omniboxMediaProgress`）每 2s 记录当前曲进度
  （>2s 才记），播放结束时清除；兼容旧插件 `musicProgress / videoProgress` 键的一次性迁移；
- **播放起点**由设置项 `resume_mode` 决定：`restart`（默认「从头播放」，每次点击曲目都从 0
  开始）/ `resume`（「保留播放进度，低于5s从头开始」）。两种模式下**剩余不足 5s 一律从头**：
  写入侧剩余不足 5s 不再落盘并清除旧记录（`el.ended` 时同样拒绝写回），读取侧在
  `loadedmetadata` 拿到时长后再次判定越界/剩余不足。历史缺陷：`ended` 时元素 currentTime
  停在 duration，紧随其后的 `_loadItem` 会先 `_saveProgress()` 把"已播完"位置写回存储、覆盖
  `ended` 里的 clear()，下次点击该曲目续播到末尾，元素停在末尾时 `play()` 无声（音乐、视频
  都表现为"无法播放"）；`togglePlay` 另在元素 `ended` / 距末尾 0.25s 内时先归零再播；
- 后端 `playback` 状态：`item_id / loop_mode / shuffle / volume / video_mode / queue_index /
  queue_ids / queue_truncated`，切歌 / 停止 / 音量 / 模式变更时保存播放参数，队列内容变化时
  经 `media_save_queue` 保存 id 列表（`QUEUE_PERSIST_LIMIT = 1000` 截断并置 `queue_truncated`）；
  启动时 `_restorePlayback` 无条件恢复音量与播放模式，条目仍存在时用 `media_get_items` 按
  `queue_ids` 批量取回**整条队列**并定位到当前条目（取不到时回落为单条队列），恢复的续播
  位置同样受 `resume_mode` 与"剩余不足 5s 从头"约束（视频画面模式优先取持久化的
  `video_mode`，回落 `default_video_mode` 设置）。
  历史缺陷：恢复只写 `queue = [item]`，重载后未点选条目时「下一首/上一首」在单条队列内打转，
  与可见列表不符。

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
  **空结果不入缓存**（缓存命中要求 `results` 非空）—— 否则一次失败（如未登录）会把空列表
  缓存住，之后即使登录成功也一直显示空；接口返回 `success:false` 时直接给出失败原因；
- 歌单详情「播放全部」：立即开播当前曲，其余曲目在后台逐个预解析播放地址，不阻塞 UI。

## 网易云 → 本地（可选能力）

- 匹配（`MPUtils.matchNeteaseToLocal`，纯前端）：两侧数据只能在前端汇合 —— 在线曲目来自
  `netease-music`，本地条目 id 来自 `media-player` 索引。归一化歌名必须一致，再用歌手交集
  或时长 ±3s 确认；两者都不满足时只有本地库该歌名唯一才接受，宁缺勿错配；同一本地条目
  只被认领一次；
- 「重建为本地歌单」（歌单详情）：分页读取在线歌单全部曲目（上限 1000 首），只写入本地
  命中项，生成/覆盖 `网易云 · <歌单名>` 镜像歌单 —— 前缀 + 同名即更新，不碰用户自己的同名歌单；
- 来源区分：「我的歌单」把创建 / 收藏分成两段（段头带数量、创建在前），歌单详情页的标签也跟着
  写「创建的歌单 / 收藏的歌单」（搜索、推荐进来的别人的歌单不标来源）；
- 「全量同步所有歌单」（登录页）：**默认只同步自己创建的歌单** —— 收藏的多是别人的几千首大歌单，
  全量镜像又慢又没意义；勾选「也包含收藏的歌单」才拉收藏列表（不勾选时连 `get_collected_playlists`
  都不请求）。创建 + 收藏合并去重后逐个镜像；本地全无命中的歌单不建空歌单；按钮即进度条
  （`3/30：歌单名`），结束时汇总「N 个歌单 → 写入 M 个（命中 X / 缺失 Y）」；
- 「导入喜欢到我的喜欢」（登录页，仅登录后提供）：红心歌曲匹配后经 `media_add_favorites`
  一次写盘；该接口幂等（只增不减、忽略索引外的 id），重复导入不会取消已有的喜欢；
- 缺失曲目清单（登录页勾选框，默认开，存 localStorage）：同步/重建时把本地缺失的在线曲目
  按歌单分组写成清单（`media_export_missing`）。落点取**音频条目最多**的媒体根 —— 数据根是
  多根配置的第一行，当前部署里那是视频盘；文件名由标题生成并剥掉路径字符，内容逐行清洗，
  只写进自己的媒体根。全量同步写 `网易云缺失曲目.txt`，单个歌单重建写
  `网易云缺失曲目 · <歌单名>.txt`（单歌单的结果不覆盖全量清单）；
- 重复同步的语义：本地歌单**有则整体覆盖、无则新建**（按 `网易云 · <名字>` 同名判定；
  重命名过的镜像会被当成新歌单重建一个，这是刻意的 —— 不引入 id 映射表）；本次匹配到
  0 首的歌单直接跳过，不建空歌单也不清空旧歌单（宁可留着旧内容，也不把用户的歌单清空）；
  缺失清单**每次整体重写**，没有缺失也写一份「（本次同步没有缺失曲目）」，不留旧清单。

---

## UI 现状取证（前端与壳契约对照）

- 取证范围：`plugins/media-player/frontend/`（只读审计，未改动任何插件文件）。
- 对照契约：`shell/frontend/public/shell/variables.css`、`base.css`、`effects.css`、`base.js`、`docs/plugin-guide.md` §4。
- 计数口径：均在 PowerShell 下用 `Select-String -AllMatches` 对该插件目录执行，正则见各节。

---

### 1. 概览

#### 1.1 前端文件清单

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `frontend/index.html` | 277 | 唯一入口；全部面板/弹窗的静态骨架 + 脚本装载顺序 |
| `frontend/media-player.css` | 2047（45.7 KB） | 全部样式；无外部 CSS 依赖 |
| `frontend/js/app.js` | 187 | `MediaPlayerApp` 类骨架：构造、`init()`、扫描轮询、扩展入口 |
| `frontend/js/app-ui-events.js` | 227 | `_bindUI` / `_bindContentDelegation` / `_bindThumbPrefetch` |
| `frontend/js/app-views.js` | 438 | 视图切换、扫描、设置弹窗、网易云视图数据 |
| `frontend/js/app-render.js` | 755 | 加载/空态、专辑卡、行列表、详情页、歌单渲染 |
| `frontend/js/app-playback.js` | 418 | 播放栏状态、队列、歌单增删、歌单右键菜单 |
| `frontend/js/app-stage.js` | 367 | 舞台/全屏/宽屏、键盘快捷键、进度条与音量 |
| `frontend/js/player-core.js` | 751 | `MediaPlayerCore`：播放内核、队列、模式、进度持久化 |
| `frontend/js/playlist-manager.js` | 150 | `MediaPlaylistManager`：歌单 CRUD 与侧栏渲染 |
| `frontend/js/lyrics-parser.js` | 331 | `MediaLyrics`：歌词解析、沉浸页、canvas 频谱 |
| `frontend/js/progress-store.js` | 65 | 本地续播位置存储 |
| `frontend/js/frame-extractor.js` | 305 | 前端 canvas 视频抽帧补封面 |
| `frontend/js/utils.js` | 299 | `MPUtils` + `VolumeMapper` + 网易云/本地匹配 |

`app-ui-events/app-views/app-render/app-playback/app-stage` 五个文件都用
`Object.assign(MediaPlayerApp.prototype, {...})` 回挂同一原型（`app-render.js:12`），
装载顺序是硬约束并写在 `index.html:264-271`（"漏挂或错序会在装载期抛
`MediaPlayerApp is not defined`"），由 `tests/js/media_player_app_split.mjs` 把关。

#### 1.2 页面形态

单个 iframe 内的**多视图单页**：左侧栏固定，右侧 `view-body` 内按 `currentView`
（`recent` / `audio-albums` / `all-audio` / `video-albums` / `all-video` / `favorites` /
`album-detail` / `playlist:*` / `ncm-*`）整体重渲染 `#media-content`
（`app-views.js:89-99`、`app-render.js`）。另有三个覆盖层：歌词沉浸页
（`#lyrics-page`，`index.html:159`）、队列/均衡器浮层（`#queue-popup`、`#eq-panel`）、
三个弹窗（`#modal-playlist`、`#modal-add-to-playlist`、`#modal-eq-name`，`index.html:206/223/240`）。

**无内嵌 companion iframe**。跨插件协作走 `renderExtensions`：把 `netease-music`
的入口渲染进侧栏 `#mp-extensions`（`app.js:159-177`），点击后切到同 iframe 内的
`ncm-*` 视图（`app.js:179-186`），再用 `Bridge.callPlugin('netease-music', ...)`
取数据（`app-views.js:173/223/249/315`、`app-render.js:193/292/354/515`）。

#### 1.3 Shell 布局类使用情况

| Shell 类 | 使用位置 | 次数 |
| --- | --- | --- |
| `.view-body` | `index.html:60` | 1 |
| `.view-toolbar` | `index.html:61`（+ `.mp-toolbar`） | 1 |
| `.toolbar-group` | `index.html:62,66` | 2 |
| `.view-sub-sidebar` | `index.html:12`（+ `.mp-sidebar`） | 1 |
| `.view-content` | `index.html:154`（+ `.mp-content`） | 1 |
| `.obx-nav-item` | `index.html:22-39` | 6 |
| `.obx-scroll` | `index.html:48,186,200,212,229` | 5 |
| `.btn` | `index.html:72,73,74,216,217,233,234,250,251` | 9（其中 `.btn-primary` 3） |
| `.hidden` | `index.html:70,146,181,190,234` | 5 |

**完全未用**：`.sub-sidebar-header`、`.sub-sidebar-footer`、`.empty-state`、`.context-menu`、
`.pagination-bar`、`.settings-form`（类名本身）、`.toast*`、`.modal*`、`.loading`，以及
`effects.css` 的全部 `.obx-anim-*` / `.obx-glass` / `.obx-card-lift` / `.obx-stagger` /
`.obx-skeleton`（正则 `obx-[a-z-]+` 在该插件目录共 16 处命中，全部是 `obx-nav-item` /
`obx-scroll` / `obx-extension`，见 §4.7）。

---

### 2. 布局骨架

#### 2.1 顶层容器结构树

```
#app                                    media-player.css:25-32（display:flex; height:100vh; overflow:hidden）
├─ aside.mp-sidebar.view-sub-sidebar    index.html:12 | css:37-48
│  ├─ .mp-brand                         index.html:13-19
│  ├─ nav.mp-nav                        index.html:21-41
│  │  ├─ button.obx-nav-item.mp-nav-item ×6   index.html:22-39（data-view）
│  │  └─ #mp-extensions                  index.html:40（renderExtensions 注入）
│  ├─ .mp-playlist-section              index.html:43-49
│  │  ├─ .mp-section-label > #btn-new-playlist
│  │  └─ #mp-playlist-list.obx-scroll    index.html:48
│  └─ .mp-sidebar-footer#mp-stats        index.html:51-56
└─ .view-body                            index.html:60
   ├─ .view-toolbar.mp-toolbar           index.html:61-76
   │  ├─ .toolbar-group.mp-view-heading   （#mp-view-title / #mp-view-sub）
   │  └─ .toolbar-group.mp-toolbar-right  （.mp-search / 扫描 / 深度扫描 / 设置）
   └─ .mp-main                            css:325-330（flex:1; flex-direction:column）
      ├─ section.mp-stage#mp-stage        css:332-345（height: calc(224px + 76px)）
      │  ├─ .mp-stage-backdrop / .mp-stage-scrim
      │  ├─ .mp-stage-viewport
      │  │  ├─ .mp-stage-art（封面 + 信息 + .mp-stage-lyrics）
      │  │  ├─ .mp-stage-empty
      │  │  ├─ video#video-player
      │  │  ├─ .mp-video-hint
      │  │  └─ #btn-stage-play.mp-stage-big-btn
      │  └─ .mp-playerbar#player-bar       css:649-662（左/中/右三段）
      └─ .view-content.mp-content#media-content   index.html:154
```

#### 2.2 关键尺寸与来源

| 项 | 数值 | 来源 |
| --- | --- | --- |
| 侧栏宽度 | `252px` | **插件自定义**：`.mp-sidebar{width:252px}`（`css:38`）覆盖 `base.css:252` 的 `var(--sub-sidebar-width)`（`variables.css:38` = `240px`） |
| 侧栏响应式 | `210px`（≤860px） | 插件自定义（`css:2029`） |
| 工具栏高度 | 未覆盖 → `var(--toolbar-height)` = `48px` | `base.css:240` + `variables.css:39`；`.mp-toolbar` 只改 `gap:16px` 与 `background:transparent`（`css:242-246`） |
| 舞台高度 | `calc(224px + 76px)` = 300px；视频模式 `300+76` = 376px | 插件自定义：`--mp-stage-vh:224px`（`css:12`）、`--mp-pb-height:76px`（`css:13`）、`.mp-stage{height:calc(var(--mp-stage-vh) + var(--mp-pb-height))}`（`css:336`）、`.mp-stage.video-on{--mp-stage-vh:300px}`（`css:348`） |
| 播放栏高度 | `76px` | `css:652` |
| 内容区内边距 | `16px` | `css:872` 二次声明，与 `base.css:280` 同值 |
| 卡片网格 | `repeat(auto-fill, minmax(168px,1fr))`，gap `16px`；≤860px 时 `140px` | `css:994-995`、`css:2033` |
| 滚动方式 | 页面整体不滚动（`#app{overflow:hidden}` `css:28`）；`#media-content` 由 `.view-content` 的 `overflow-y:auto`（`base.css:279`）滚动 | — |
| 浮层层级 | `.mp-pop` 260 / `.mp-lyrics-page` 400 / `.mp-modal` 500 / `.mp-context-menu` 520 | `css:1393/1725/1590/1688` |

插件自定义布局规则集中在：`.mp-main`（`css:325-330`）、`.mp-stage`（`css:332-349`）、
`.mp-playerbar`（`css:649-662`）、`.mp-content`（`css:871-874`）。其余容器全部沿用
`base.css` 的 flex 骨架，未复制 `.view-*` 的定义。

---

### 3. 设计 token 使用

#### 3.1 实际用到的 `--*` 变量

`Select-String -Pattern 'var\(--[a-zA-Z0-9-]+' -AllMatches` → 命中 **246 处 / 35 个唯一变量名**。

高频（Shell 提供的）：`--transition-fast` ×36、`--accent` ×31、`--text-secondary` ×21、
`--bg-hover` ×20、`--text-primary` ×20、`--border` ×19、`--text-muted` ×16、
`--bg-surface` ×10、`--danger` ×3、`--text-on-accent` ×2、`--danger-soft` ×2、`--bg-app` ×1、
`--bg-overlay` ×1。

插件自有：`--mp-accent-soft` ×12、`--mp-accent-2` ×8、`--range-val` ×6、
`--mp-pb-height` ×4、`--mp-shadow-2` ×4、`--mp-glass-strong` ×4、`--mp-gradient` ×4、
`--mp-radius-sm` ×3、`--mp-stage-vh` ×2、`--mp-scroll-thumb` ×2、`--mp-radius` ×2、
`--mp-glass` ×2、`--i` ×2、`--mp-scroll-thumb-hover` ×1、`--mp-shadow-1` ×1。
另有一组运行时由 JS 设置的歌词变量：`--lyrics-blur` / `--lyrics-brightness` /
`--lyrics-font-color` / `--lyrics-font-color-active` / `--lyrics-font-color-hover` /
`--lyrics-glow-color` / `--hero-bg`。

#### 3.2 自有 `:root` 覆盖

`media-player.css:9-23` 在 `:root` 定义 12 个 `--mp-*`，其中 4 个是自建阴影/圆角体系：

```css
--mp-shadow-1: 0 6px 24px rgba(0, 0, 0, 0.10);   /* css:18 */
--mp-radius: 14px;                                /* css:10 */
```

与 `effects.css:13-22` 的 `--obx-shadow-1: 0 6px 24px rgba(0,0,0,0.10)`、`--obx-radius: 14px`、
`--obx-radius-sm: 10px`、`--obx-shadow-2: 0 14px 44px rgba(0,0,0,0.18)` **数值逐字相同**；
`--mp-glass` / `--mp-glass-strong`（`css:16-17`）与 `.obx-glass` / `.obx-glass-strong`
（`effects.css:95/101`）的 `color-mix(... 84%/92%, transparent)` 公式相同。
未覆盖 Shell 的 `--bg-*` / `--text-*` / `--accent` / `--radius` / `--nav-width` /
`--sub-sidebar-width` / `--toolbar-height`。

#### 3.3 硬编码颜色字面量

| 正则 | 命中行数 | 命中次数 |
| --- | --- | --- |
| `#[0-9a-fA-F]{3,8}\b` | 22 | 22 |
| `rgba?\(` | 42 | 46 |
| `gradient\(` | 19 | 19 |

典型 5 例：

- `css:341` `.mp-stage{background:#0b0b12}` —— 舞台上/下封面底色，浅色主题下也是深色。
- `css:1728-1729` `.mp-lyrics-page{background:#07070d; color:#fff}` —— 歌词沉浸页整页硬编码。
- `css:761/1287` `.mp-icon-btn.fav-active{color:#f43f5e}` / `.mp-row-action.fav-active{color:#f43f5e}` —— 收藏红心不走 `--danger`。
- `css:14` `--mp-accent-2: color-mix(in srgb, var(--accent) 55%, #8b5cf6)` 与 `css:1754-1755` 极光里的 `#8b5cf6` / `#06b6d4`。
- `css:366` `.mp-stage-scrim{background:linear-gradient(180deg, rgba(8,8,14,.35) ...)}`、`css:1904-1907` 全屏态 `rgba(255,255,255,.85)` / `#fff`。

#### 3.4 `data-theme` 处理

`Select-String -Pattern 'data-theme'` 对该插件 `*.css` / `*.html` / `js/*.js` 执行 →
**0 命中**（`css` 内 `:root|data-theme|@media` 只命中 `:root`@9 与 3 处 `@media`）。
插件完全依赖壳的 iframe 注入脚本同步主题属性；暗色下不变形的只有走 token 的部分，
硬编码深色面板（`#0b0b12` / `#07070d` / `#000`）在两个主题下都是深色，
`rgba(255,255,255,*)` 也只在深色面板内使用（`css:1585/1904/1905`）。

---

### 4. 组件与命名约定

#### 4.1 自有类名前缀

`.mp-*` 唯一类名 **137 个**（正则 `\.mp-[a-z0-9-]+`，去重）。子前缀分组：
`mp-stage-*`（14）、`mp-lyrics-*`（9）、`mp-pb-*`（7）、`mp-eq-*`（7）、`mp-row-*`（8）、
`mp-card-*`（4）、`mp-detail-*`（7）、`mp-pop*` / `mp-modal*` / `mp-nav*` / `mp-toolbar*` 等。

**无前缀的例外**（散落在 JS 模板里，与 Shell 类名风格混在一起）：
`.cover-fallback`（`utils.js:80`）、`.img-broken`（`utils.js:138`）、
`.empty-icon` / `.empty-text` / `.empty-hint`（`app-render.js:33-36`）、
`.q-index` / `.q-title` / `.q-kind` / `.q-remove`（`app-playback.js:271-272`）、
`.pl-name`（`playlist-manager.js:130`）、`.active` / `.hidden`（Shell 既有语义）。

#### 4.2 自有组件清单

| 类别 | 类名 | 位置 |
| --- | --- | --- |
| 按钮 | `.mp-icon-btn`(34×34 圆形)、`.mp-play-btn`、`.mp-stage-big-btn`、`.mp-tool-btn`(圆角 999px)、`.mp-ghost-btn`(5px 9px / 8px 圆角) | `css:737-758 / 765-783 / 613-641 / 318-320 / 1569-1581` |
| 卡片 | `.mp-card-grid`、`.mp-card`、`.mp-card-badge`、`.mp-card-play` | `css:992-1009 / 1091 / 1046` |
| 行/列表 | `.mp-row`、`.mp-row-cover`、`.mp-row-tag`、`.mp-list-group`、`.mp-queue-item` | `css:1155 / 1194 / 1235 / 1142 / 1438` |
| 弹窗 | `.mp-modal`、`.mp-modal-box`、`.mp-modal-head/body/foot`、`.mp-modal-sm` | `css:1587-1642 / 1615` |
| 浮层 | `.mp-pop`、`.mp-pop-head`、`.mp-pop-body`、`.mp-queue-pop`、`.mp-eq-pop` | `css:1391-1423 / 1425 / 1487` |
| 菜单 | `.mp-context-menu`（`button` 而非 `li`） | `css:1686-1717` |
| 进度 | `.mp-range` + `--range-val`（渐变填充轨道）、`.mp-progress`、`.mp-volume-bar` | `css:820-866` |
| Toast | **无自有实现**，调用 Shell `Toast.success/error/info/warning` | `app.js:118`、`app-stage.js:94/101/109/112/162/170/177/181/207` 等 |
| 空状态 | `.mp-empty-state` + `.empty-icon`/`.empty-text`/`.empty-hint` | `css:976-989`；调用 6 处：`app-render.js:33,103,173,197,226,527` |
| 加载 | `.mp-loading` + `.mp-spinner`(34px, 0.8s) | `css:949-974`；入口 `app-render.js:17-28` |
| 骨架屏 | **无**（`obx-skeleton` 0 命中） | — |
| 徽标 | `.mp-card-badge`、`.mp-row-tag.video`、`.mp-thumb-pending`、`.mp-mini-eq` | `css:1091 / 1245 / 1073 / 213` |
| 表单控件 | `.mp-input`、`.mp-select`、`.mp-add-pl-item` | `css:1644-1659 / 1496 / 1668-1681` |

#### 4.3 Shell 已提供、插件又实现了一遍的能力

1. **模态框**：Shell `.modal` / `.modal-box`（`base.css:159-179`，宽 400px、`border-radius: var(--radius)`=6px、
   `z-index:1500`、`slideUp 0.2s`）↔ 插件 `.mp-modal` / `.mp-modal-box`
   （`css:1587-1613`，宽 **360px**（`.mp-modal-sm` 320px）、圆角 **18px**、`backdrop-filter: blur(4px)`、
   `z-index:500`、`mpScaleIn 0.24s`）。**3 个弹窗**全部用自有版（`index.html:206/223/240`）。
2. **右键菜单**：Shell `createContextMenu` + `.context-menu`（`base.js:886`、`base.css:104-116`，
   `min-width:150px`、圆角 `var(--radius)`、`z-index:2000`、`li` 结构）↔ 插件手工建
   `.mp-context-menu`（`app-playback.js:380-389`，`min-width:140px`、圆角 12px、`z-index:520`、
   `button` 结构、`mpPopIn 0.18s`）。仅歌单一个场景（重命名/删除）。
3. **空状态**：Shell `.empty-state`（`base.css:428-431`，居中、`min-height:300px`、16px 单行文案）
   ↔ 插件 `.mp-empty-state`（`css:976-989`，`min-height:240px`、图标 44px + 15px 标题 + 12px 提示）。
4. **卡片网格**：Shell `createCardGrid`（`base.js:915`）↔ 插件 `.mp-card-grid` + `.mp-card`
   （`css:992-1009`，`minmax(168px,1fr)`、`mpFadeUp` 入场 + `--i*26ms` 交错）。
5. **滚动条**：Shell `.obx-scroll`（`effects.css:140-183`）↔ 插件 `css:879-947` 的同一套
   `scrollbar-width: thin` + `::-webkit-scrollbar{width:6px}` + 悬停渐显规则，
   颜色 token `--mp-scroll-thumb` 38% / hover 62%（`css:21-22`）与 effects.css:150/176/182 同值。
6. **动效关键帧**：`effects.css:25-74` 的 `obxSpin/obxFadeUp/obxPopIn/obxScaleIn/obxFade/obxHeart/obxFloat/obxShimmer`
   ↔ 插件 `mpSpin/mpFadeUp/mpPopIn/mpScaleIn/mpFade/mpHeart/mpFloat/mpShimmer`
   （`css:1965-2015`），逐条数值相同（如 `mpFadeUp` 与 `obxFadeUp` 都是
   `translateY(10px)`、`mpHeart` 都是 `40%{scale(1.45)}`）。交错延迟用自有 `--i`（`css:1008`，
   26ms），而 `effects.css:90` 用 `--obx-i`（30ms）。
7. **玻璃拟态 / 抬升**：Shell `.obx-glass` / `.obx-glass-strong` / `.obx-card-lift` ↔ 插件
   `.mp-sidebar{background:var(--mp-glass); backdrop-filter:blur(18px)}`（`css:39-41`）与
   `.mp-card:hover`（`css:1006` 自带 transition，未用 `.obx-card-lift`）。
8. **按钮基础态**：Shell `.btn`（`base.css:14-36`）↔ 工具栏按钮确实复用 `.btn`
   （`index.html:72-74`，9 处），但另有 `.mp-ghost-btn`（`css:1569-1581`）与
   `.mp-icon-btn`（`css:737-758`）两套自有按钮体系，交互细节（`active{scale(0.94)}`）与
   Shell 的 `.btn:active{scale(0.97)}`、`.btn:hover{background:var(--bg-hover)}` 不一致。
9. **设置表单**：**复用** Shell `openSettingsModal`（`app-views.js:75`）+ Shell `.settings-form`
   （弹窗由壳渲染），后端 `settings_schema` 含 `text/number/checkbox/range/select/directory` 类型。
10. **分页 / 灯箱 / 目录树**：**均未实现也未复用**（`createPagination` / `createLightbox` /
    `createTree` 在该插件 0 命中），列表全量渲染（`app-render.js` 整段 `innerHTML`）。
11. **进度条**：自有 `.mp-range` + `--range-val`（`css:820-866`、`utils.js:169-173`），
    Shell 无对应组件 → 不算重复实现。

---

### 5. 交互约定

- **设置入口**：工具栏 `.btn#btn-settings` → `_openSettings()`（`app-ui-events.js:25` →
  `app-views.js:74-84`），传 `title: '媒体播放器设置'`、`successMessage: '设置已保存'`、
  `onSave: Bridge.call('save_settings', values)`；歌词页工具栏设置入口（`<svg class="obx-icon"><use href="#settings"></use></svg>`）打开同一个壳弹窗但换标题
  （`app-ui-events.js:94-96`，`'歌词与播放设置'`）。**保存后的反馈**完全交给壳：
  `Toast.success` + `setTimeout(() => location.href = ... + '?_t=' + Date.now(), 400)` 整页重载
  （`base.js:621-622`），插件没有自定义保存后行为。
- **错误提示**：统一 `Toast.error` + `console.error`，无内联错误条/字段级报错。
  例：`app.js:122` `console.error('媒体索引初始化失败:', e)`、`app-views.js:63` `Toast.error('扫描失败')`、
  `player-core.js:699/743` 解码/加载失败、`app-render.js:527` 在内容区渲染
  `.mp-empty-state` + `icon:triangle-alert` + 「歌单加载失败」。
- **选择模型**：**单选**（点击专辑卡/行即播放或进详情，`app-render.js:136`）；
  无多选、无框选、无长按；无 `selectionMode` / `selectedIds`（grep 0 命中）。
  唯一的状态切换是收藏：`icon:heart` 按钮（`index.html:121`，两种状态由 `fav-active` 类区分）→ `media_toggle_favorite`
  （`app-playback.js:212`、`app-render.js:629`）。
- **右键菜单**：仅歌单条目。`app-playback.js:378-408` 动态建 `.mp-context-menu`，
  两项 `data-menu-act="rename" | "delete"`（删除带 `danger` 样式并走 `confirmDialog`，
  `app-playback.js:399`）。定位用 `Math.min(e.clientX, innerWidth-150)`（同上 386-387）。
  卡片/行/任务**无**右键菜单。
- **键盘快捷键**（`app-stage.js:303-366`，`INPUT/TEXTAREA/SELECT` 内不拦截，见 305）：
  `Space` 播放/暂停、`←/→` ±5s（`seekDelta`）、`↑/↓` 音量 ±0.05、`n/N` 下一曲、`p/P` 上一曲、
  `m/M` 静音、`l/L` 歌词页、`f/F` 全屏、`Esc` 逐级退出（全屏 → 歌词 → 关队列/均衡器/弹窗/菜单，
  353-363）。弹窗输入框内另有 `Enter` 提交：`app-ui-events.js:107-108`、`122-123`；
  搜索框 `Esc` 清空（`app-ui-events.js:33-36`）。
- **长任务进度与取消**：扫描是唯一长任务。`media_scan(deep)` 启动后 500ms 轮询
  `media_scan_status`，上限 1200 轮（≈10 分钟，`app.js:87-96`）；进度文案
  `` `正在扫描媒体库… ${s.processed}/${s.total}${s.current ? ' · ' + s.current : ''}` ``
  （`app-views.js:53`）走 `.mp-loading` 转义 + `white-space: pre-line`（`css:962-965`）；
  扫描期间两个按钮 `disabled`（`app-views.js:65-69`）。**前端没有取消入口**，
  只在轮询发现后端 `state === 'cancelled'` 时提示"扫描已取消，已完成部分已保留"
  （`app-views.js:57`）。断点续扫在 `init` 阶段自动触发（`app.js:104-113`）。
- **空/加载/错误三态**：加载 = `.mp-loading`+`.mp-spinner`（`app-render.js:17-28`，
  文案 `正在准备媒体库…` / `首次使用，正在扫描媒体库…` / `继续上次未完成的扫描…` 见
  `app.js:99/106/110`）；空 = `.mp-empty-state` + 图标/标题/提示（`app-render.js:30-38`，
  调用点 6 处）；错误 = 同款空态 + `icon:triangle-alert`（`app-render.js:527`、`app-views.js:182-183`）。
  无骨架屏。

---

### 6. 特色设计（值得其他插件吸收）

1. **歌词沉浸页 = 覆盖层 + 位移过渡，而不是新路由**
   （`css:1722-1738`、`lyrics-parser.js:243-262`）：整页 `position:fixed; inset:0;
   transform:translateY(102%)` → `.active{translateY(0)}`，配 `pointer-events` 切换与
   canvas 频谱（`requestAnimationFrame` 自循环，`_startViz/_stopViz` 成对）。
   解决"要给音频一个大屏、又不能让 iframe 重新加载、也不能拦掉下层交互"。
2. **视频封面缺失时前端 canvas 抽帧并回写缓存**
   （`frame-extractor.js:149-196`、`utils.js:292-298`、`utils.js:57-62`）：
   封面 404 → 借 `<video>` 抽一帧 → `media_put_thumb` 入库；同时给 `/thumbs` URL 挂
   文件 mtime 版本号 `&v=`，强制绕开 1 天缓存。解决"老视频无内嵌封面"与
   "换了文件旧封面最长展示 24h"两个具体问题。抽帧还做了 `document.hidden` 判断与
   3s rAF 超时（`frame-extractor.js:9,245,264`），避免后台空转。
3. **非线性音量映射 `VolumeMapper(2.5)`**（`utils.js:269-290`）：
   滑块位置与实际音量之间只在"赋值元素 volume"处做 `pow(x, 2.5)`，存储/传输仍是线性 0~1。
   解决"低音量区间在滑块上挤成一格、调不准"，且不改变后端存档语义。
4. **首用自动扫描 + 断点续扫 + 结果汇总 Toast**（`app.js:98-124`）：
   `stats.total === 0` 时自动全扫；`state === 'paused'` 时提示"继续上次未完成的扫描…
   已完成部分自动跳过"；结束后 `Toast.success('扫描完成：音乐 N · 视频 M')`
   （`app.js:118`、`app-views.js:48/58`）。把"第一次打开是空白页"变成一次带反馈的自动流程。
5. **网易云曲目 ↔ 本地库的保守匹配打分**（`utils.js:199-252`）：
   NFKC + 抹平标点空白归一化歌名，歌手 token 交集得 3 分、时长差 ≤3s 得 2 分、
   同名唯一得 1 分，同一本地条目只被认领一次，未命中项导出补档清单
   （`app-render.js:470` `media_export_missing`）。解决"在线歌单落到本地时错配别人的歌"。
6. **扩展入口与自建导航的选中态联动**（`app.js:159-177`）：
   `renderExtensions(container, 'media-player', 'sidebar', {title, onOpen})` 拿到壳渲染的
   `.obx-extension` 后，再补一次点击处理，让扩展项与 `.mp-nav-item` 的 `active` 互斥。
   解决"壳渲染的扩展入口和插件自己的侧栏高亮各亮一个"。

---

### 7. 与 Shell 契约的偏差（后续统一 UI 必须处理的点）

1. **声明了 `keepAlive: true`，却一个生命周期钩子都没注册。**
   证据：`plugins/media-player/manifest.json:8` `"keepAlive": true`；
   对 `plugins/media-player/frontend/**` 执行 `onShow\(|onHide\(|onDispose\(|PluginLifecycle`
   → **0 命中**（对 `plugins/media-player/` 整目录执行同一正则只命中 manifest 这一处）。而 `docs/plugin-guide.md:763-764`
   明确写着"`media-player` 在 `onHide` 里只停 `lyrics-parser` 的 rAF 自循环"。
   受影响面：2 个常驻定时源——`lyrics-parser.js:246-250` 的 canvas rAF 自循环
   （只在 `_stopViz()` 被显式调用时停，`lyrics-parser.js:253`）、
   `player-core.js:417` 的 `setInterval(_, 2000)` 进度保存；另加 `frame-extractor.js` 的
   抽帧队列。是否真的空转取决于浏览器对 `display:none` iframe 里 rAF 的实现，
   插件侧没有任何可控点。
2. **侧栏宽度脱离 `--sub-sidebar-width`。**
   证据：`css:38` `.mp-sidebar{width:252px}` 覆盖 `base.css:252`；
   `--sub-sidebar-width` 在该插件 CSS 中 0 命中；响应式断点自行改宽度
   `css:2029` `.mp-sidebar{width:210px}`。影响：壳在设置页调整子侧栏宽度对该插件无效，
   与 manga-library（248px，见另一份）也不一致。
3. **自有弹窗层级低于壳弹窗，宽度也不同。**
   证据：`.mp-modal{z-index:500}`（`css:1590`）vs Shell `.modal{z-index:1500}`（`base.css:162`）、
   `.toast-container{z-index:3000}`（`base.css:210`）；宽度 360px（`css:1603`）vs 400px（`base.css:167`）。
   影响 3 个弹窗；同一时序里打开壳设置弹窗会盖住插件弹窗，统一组件后需要重排 z-index 体系。
4. **右键菜单绕过 `createContextMenu`。**
   证据：`app-playback.js:380-389` 手建 `div.mp-context-menu` + `button`，
   样式 `css:1686-1717`（`min-width:140px`、`li` → `button`），而 Shell 约定是
   `.context-menu > li`（`base.css:110`）。影响 1 个功能（歌单重命名/删除），
   统一菜单组件时会同时改动 `app-playback.js` 的 DOM 结构与 CSS。
5. **滚动容器一半加类、一半靠自有规则。**
   证据：`index.html:48/186/200/212/229` 的 5 个容器同时有 `.obx-scroll`，
   但最长的滚动区 `#media-content`（`index.html:154`）**没有** `.obx-scroll`，
   只被插件自有规则 `css:879-947` 覆盖。影响：只按类名统一（例如改 `.obx-scroll` 的
   38%/62% 透明度）会漏掉内容区，观感不一致。
6. **`prefers-reduced-motion` 用通配符全量覆盖。**
   证据：`css:2039-2047` `*, *::before, *::after { animation-duration:.001s !important;
   transition-duration:.001s !important }`（该文件仅有的 3 处 `!important` 都在这里），
   比 `effects.css:186-201` 的类名白名单更宽。影响：会连带压掉壳注入到该 iframe 的
   组件（Toast、设置弹窗、目录选择器）的过渡，统一动效策略时要一起取舍。
7. **`effects.css` 的类工具几乎未被采用。**
   证据：`obx-anim-*` / `obx-glass` / `obx-card-lift` / `obx-stagger` / `obx-skeleton`
   在该插件目录 0 命中（仅 `obx-nav-item` / `obx-scroll` / `obx-extension` 被用到）；
   替代品是 8 个自有关键帧（`css:1965-2015`）与自有 `--mp-glass` / `--mp-shadow-*` / `--mp-radius*`。
   影响：入场动画、玻璃、抬升、交错、骨架五类能力各多一套实现；
   壳改 `effects.css`（例如调 `--obx-shadow-2`）时该插件不会跟随。
8. **面板尺寸用内联 `style` 与自有类混写。**
   证据：`js/*.js` 中共 16 处 `style="` 模板拼接（`Select-String -Pattern 'style="'` 统计
   `plugins/media-player/frontend/js/*.js`；`index.html` 侧为 0 处），典型 `app-render.js:689`
   `style="--hero-bg:${MPUtils.heroBg(coverSrc)}"`（该值已走白名单编码，`utils.js:108-118`）。
   影响：主题/尺寸调整无法只改 CSS，需要同时改 JS 模板。
9. **无分页、无虚拟滚动，列表一次全量渲染。**
   证据：`createPagination` 0 命中；`app-render.js` 以整段 `innerHTML` 重建列表
   （如 `app-render.js:51` `content.innerHTML = '<div class="mp-card-grid"></div>'` 后逐张 append）；
   在线歌单有硬上限 `NCM_PLAYLIST_SONG_CAP = 1000`（`app-render.js:10`）。
   影响：统一列表/分页组件时，媒体库（行/卡/队列/歌单/网易云五套列表）都要接入。
10. **设置保存依赖整页重载。**
    证据：插件 `onSave` 只转发 `save_settings`（`app-views.js:78-82`），
    刷新由 Shell 固定执行（`base.js:622` `location.href + '?_t=' + Date.now()`）。
    影响：与 `keepAlive: true` 组合时，"改设置"必然打断播放（整页重载），
    而声明 keepAlive 的目的正是"播放不中断"，两者在语义上冲突；涉及全部
    `lyrics_*` / `auto_hide_*` / `media_roots` 设置项（`backend/main.py` 的 `settings_schema`）。
