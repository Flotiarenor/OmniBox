# media-player 迁移说明

本插件由旧 `music-player` 与 `video-player` 两个插件合并而来，是音频 / 视频统一媒体库。

## 迁移对照

| 旧插件功能 | media-player 对应实现 |
|-----------|----------------------|
| 音乐目录扫描（增量缓存） | `backend/scanner.py`，音频 + 视频统一索引（索引版本 v4） |
| 内嵌封面 / 文件夹封面 | 封面统一走 `ThumbCache`（`root_dir/.cache/thumbs.db`，SQLite）：音频内嵌/文件夹封面后端懒生成，视频封面由前端 canvas 抽帧回写（`/thumbs/<item_id>` 提供） |
| 专辑网格、全部歌曲、收藏、最近播放 | 统一视图：音乐专辑 / 全部音乐 / 视频专辑 / 全部视频 / 我的喜欢 / 最近播放 |
| 歌单管理（创建 / 删除 / 加入 / 移除） | 混编歌单（音乐与视频可放同一歌单），支持重命名与更多菜单 |
| 歌词显示与歌词设置 | 沉浸式歌词页：LRC 解析、逐行高亮、频谱可视化、可配置字号 / 对齐 / 发光 / 背景；未全屏时舞台内显示当前句前后各两句迷你歌词 |
| 10 段均衡器与预设 | 内置 EQ 预设 + 自定义预设保存 |
| 播放队列、播放模式、音量、快捷键 | 统一播放栏，支持顺序 / 随机 / 单曲循环、空格 / 方向键 / N/P/M/L/F 快捷键 |
| 视频文件浏览与播放 | 改为「视频专辑」按目录聚合浏览，支持画面 / 仅声音切换 |
| 视频全屏 + 控件自动隐藏 | 舞台全屏，Shell 导航联动隐藏，鼠标静止自动隐藏控件（延迟可配置）；音乐全屏即沉浸式歌词页，封面 / 全屏按钮均可进入 |
| 宽屏模式 | 播放栏 🖥 按钮：隐藏下方媒体列表、舞台铺满主区域，左侧导航保留，再次点击恢复 |
| 视频播放进度记忆 | 统一进度存储，并自动继承旧插件 localStorage 中的播放进度 |
| 旧收藏 / 最近播放 / 歌单 / 播放状态 | 首次扫描后自动从旧 `music_state.json` 迁移（文件重命名为 `.migrated`） |

## 目录结构

```
plugins/media-player/
├── manifest.json
├── eq-presets/            # 均衡器预设
├── backend/
│   ├── main.py            # MediaPlayerPlugin（API 入口 + 状态迁移 + ThumbCache 接入）
│   ├── scanner.py         # 增量扫描（音频按标签聚合专辑，视频按目录聚合）+ cover_generator
│   ├── metadata.py        # 音频标签 / 内嵌封面（extract_cover_bytes / has_embedded_cover）
│   ├── models.py          # MediaItem / MediaAlbum
│   └── video_meta.py      # 视频时长探测（mutagen 轻量解析，mp4/m4v；无解码栈依赖）
└── frontend/
    ├── index.html
    ├── media-player.css
    └── js/
        ├── utils.js            # MPUtils 工具（coverImg 抽帧钩子 / 降级）
        ├── frame-extractor.js  # 视频封面前端 canvas 抽帧器（隐藏 video + 串行队列）
        ├── lyrics-parser.js    # LRC 解析 + 沉浸式歌词页
        ├── progress-store.js   # 播放进度记忆（兼容旧插件）
        ├── player-core.js      # 音频 / 视频播放核心 + EQ
        ├── playlist-manager.js # 歌单管理
        └── app.js              # 主应用（视图、舞台、全屏、快捷键）
```

## 文件访问

媒体文件以「绝对路径 + URL 编码」通过 `/file?path=` 路由访问；
`MediaPlayerPlugin.get_file_roots()` 声明所有媒体根目录，Shell 对每个根目录做路径安全检查，
因此支持跨多个磁盘 / 目录的媒体库（`root_dir` + `media_dirs`）。
封面不再作为散文件落盘，统一走 `/thumbs/<item_id>?plugin=media-player`（见「封面生成」）。

## 后台扫描任务（BackgroundTask）

扫描复用 Shell 共享基建 `shell/backend/tasks.py`（见 `docs/plugin-guide.md` §3.4），
不自行实现线程/状态/持久化：

- `media_scan(force)` 启动后台任务；前端工具栏两个按钮：**「🔄 扫描」增量**（默认，
  只处理新增/变更文件）、**「⚡ 深度扫描」全量**（`force=True` 重读全部标签与时长）；
  已运行时返回 `error`；
- **断点续传**：worker 每完成一个根目录，把「部分索引（`media_index.json` 检查点）+ 
  `completed_roots`（`scan_task.json`，经 BackgroundTask 原子持久化）」落盘；
  进程中断后重启自动恢复为 `paused`，再次增量扫描跳过已完成根目录；
- `media_scan_status` 前端轮询进度（`processed/total/current/extra`），
  `media_scan_cancel` 请求取消（已完成根目录与检查点保留，可续扫）；
- 增量语义：mtime/size 未变的文件直接复用缓存索引；索引丢失而任务文件残留时自动降级全扫；
- 目录封面图（cover/folder 等）比媒体文件新时强制重建条目，刷新 `has_cover` 标记。

## 封面生成（ThumbCache + 前端 canvas 抽帧）

封面复用 Shell 共享基建 `shell/backend/thumb_cache.py`（见 `docs/plugin-guide.md` §3.4），
SQLite 缓存 + 按需生成：

- **视频**：`has_cover` 恒为 True；`/thumbs/<id>` 未命中（404）时，前端隐藏 `<video>`
  seek 到 10% → canvas 截帧 → JPEG data URL 经 `media_put_thumb` 回写 ThumbCache
  （`frame-extractor.js`：**并发 2 worker** + 按 itemId 去重 + 播放中封面插队优先 +
  `seeked`/双 rAF 防黑帧，所有等待带超时防队列死锁）。
  解码能力与播放严格对齐——系统能放才有封面，放不了的视频两者都无，逻辑自洽；
- **音频**：`cover_generator`（构造注入）读内嵌封面（mutagen）或文件夹封面；
  扫描期只检测 `has_embedded_cover`，不读取/落盘字节；
- 失效校验：`source_mtime`（0.5s 容差）+ `source_size` 双条件，源文件替换自动失效
  （含 `put()` 回写条目——`ThumbCache.put` 为前端回写提供的直入库接口）；
- 前端 `MPUtils.coverUrl(item)` 对 `has_cover` 条目生成 `/thumbs/<item_id>` 地址，
  专辑封面用后端返回的 `cover_item_id`；加载失败先尝试抽帧（`coverImg` 的 itemId 钩子），
  抽帧失败才降级 emoji 占位；
- **按浏览位置后台预载**：渲染完成即对视口内（含 300px 预读边距）的封面立即预取，
  其余交给 IntersectionObserver 滚动触发；进入视口的封面调 `media_thumb_missing`
  批量查询未缓存项，只对缺失的入抽帧队列（按 itemId 去重，与 404 驱动互补不重复抽帧）；
  查询失败时降级为全部尝试；
- 旧索引（`.cache/covers` 散文件）自动失效：条目改走 `/thumbs` 后按需重建，旧文件成为孤儿，
  不影响使用（深度扫描 + 手动清理可移除）。
- DB 位于 `root_dir/.cache/thumbs.db`（数据跟数据走）。

## 深度扫描与封面

- **深度扫描不重建、不删除任何现有封面**：只重读标签/时长，ThumbCache 保持不动；
- 深度扫描（索引完整）会顺带清理**已删除媒体文件**遗留的孤儿封面条目
  （`ThumbCache.prune(existing_keys)`），DB 防膨胀；增量扫描不清理。

## 服务器线程模型

主程序 `app.run(..., threaded=True)`（`main.py` 两处启动分支）。
单线程下大文件媒体流（Werkzeug 流式 200/206 响应）会阻塞 Range 请求与全部 API，
表现为「大视频必须完整读取才能播放、无法拖动进度条、界面卡死」；
`send_file(conditional=True)` 本身已支持 Range/206，多线程后浏览器可只读 moov 尾部
秒开播放并自由跳转。视频文件本身若 moov 在尾部（未 faststart）则任何播放器都难拖动，
属文件问题（可用 `ffmpeg -c copy -movflags +faststart` 重封装）。

## 播放器防乱跳

`player-core.js` 媒体 error 处理按错误码分流：

- `MEDIA_ERR_ABORTED`（换源中止，快速切歌/连点触发）→ 忽略；
- 播放中解码错误 → 停止并提示，不自动跳歌（避免整队列连环跳）；
- 网络/格式不支持 → 同曲重试一次，仍失败才跳，且**只向前**跳下一首（绝不回跳旧曲），
  队列尽头停止；自动跳转定时器（600ms）在用户任何操作（切歌/上一首/下一首/停止/队列点击）
  时解除，防止延迟定时器覆盖用户选择。

## 封面失效

封面缓存由 ThumbCache 的 `source_mtime`（0.5s 容差）+ `source_size` 双条件校验，
源文件替换后自动重生成（见「封面生成」一节）；目录封面图新增/更新时，
增量扫描按「封面 mtime > 媒体文件 mtime」判定并重建条目。
