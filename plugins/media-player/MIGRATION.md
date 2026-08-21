# media-player 迁移说明

本插件由旧 `music-player` 与 `video-player` 两个插件合并而来，是音频 / 视频统一媒体库。

## 迁移对照

| 旧插件功能 | media-player 对应实现 |
|-----------|----------------------|
| 音乐目录扫描（增量缓存） | `backend/scanner.py`，音频 + 视频统一索引（索引版本 v4） |
| 内嵌封面 / 文件夹封面 | 音频内嵌封面抽取到 `.cache/covers`；视频无封面时用 ffmpeg **为每个视频单独抽取一帧**作为自己的封面（视频专辑封面取第一项） |
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
│   ├── main.py            # MediaPlayerPlugin（API 入口 + 状态迁移）
│   ├── scanner.py         # 增量扫描（音频按标签聚合专辑，视频按目录聚合 + 每个视频独立封面抽帧）
│   ├── metadata.py        # 音频标签 / 内嵌封面
│   ├── models.py          # MediaItem / MediaAlbum
│   └── video_meta.py      # 视频时长探测（PyAV → mutagen → ffprobe）
└── frontend/
    ├── index.html
    ├── media-player.css
    └── js/
        ├── utils.js            # MPUtils 工具
        ├── lyrics-parser.js    # LRC 解析 + 沉浸式歌词页
        ├── progress-store.js   # 播放进度记忆（兼容旧插件）
        ├── player-core.js      # 音频 / 视频播放核心 + EQ
        ├── playlist-manager.js # 歌单管理
        └── app.js              # 主应用（视图、舞台、全屏、快捷键）
```

## 文件访问

媒体文件与封面以「绝对路径 + URL 编码」通过 `/files/` 路由访问；
`MediaPlayerPlugin.get_file_roots()` 声明所有媒体根目录，Shell 对每个根目录做路径安全检查，
因此支持跨多个磁盘 / 目录的媒体库（`root_dir` + `media_dirs`）。
