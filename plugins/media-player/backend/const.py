"""media-player 后端的纯数据与共享常量（只有入口加载它）。

设置项 schema 是"插件对外声明什么设置项"的数据，不是装配：留在入口会把装配挤到行数
上限附近，让下一个加 API 键的人被迫去删注释。字段语义见 shell/frontend/src/plugin-content.ts
的 SchemaField，壳的设置弹窗与 PluginBase 都从类属性读。
"""

from typing import Any, Dict, List

#: 媒体文件夹与歌词/视频/播放相关设置。`media_roots` 是目录列表（type="directory"），
#: 由壳的共享 FolderPicker 渲染（与 image-viewer 的「图片文件夹」同一套实现）。
SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    # 目录列表（type:"directory"）由 Shell 共享组件渲染，与图片相册的
    # 「图片文件夹」是同一套实现（shell/frontend/public/shell/folder-picker.js）
    {"key": "media_roots", "label": "媒体文件夹", "type": "directory", "multi": True,
     "admin_only": True,
     "placeholder": "输入目录绝对路径，如 D:\\音乐",
     "emptyText": "未添加任何目录，将使用默认数据目录（./data）",
     "help": "第一行为主目录（数据根 + 扫描根），其余目录作为额外扫描根，"
             "各根以目录名做前缀聚合；保存后生效。改动需管理员（它决定 /file 的允许根）",
     "section": "media", "sectionTitle": "媒体库",
     "sectionDescription": "媒体目录与扫描根；第一行为主目录，其余作为额外扫描根。",
     "sectionOrder": 10, "order": 10},
    {"key": "lyrics_enabled", "label": "启用歌词显示", "type": "checkbox",
     "default": True, "help": "关闭后不显示歌词入口",
     "section": "lyrics", "sectionTitle": "歌词",
     "sectionDescription": "歌词显示、字号、行高与对齐。",
     "sectionOrder": 30, "order": 10},
    {"key": "lyrics_font_size", "label": "歌词字号", "type": "range",
     "default": 16, "min": 12, "max": 40, "help": "未激活行的字号",
     "section": "lyrics", "sectionTitle": "歌词",
     "sectionDescription": "歌词显示、字号、行高与对齐。",
     "sectionOrder": 30, "order": 20},
    {"key": "lyrics_active_size", "label": "当前行字号", "type": "range",
     "default": 24, "min": 16, "max": 52, "help": "当前播放行的字号",
     "section": "lyrics", "sectionTitle": "歌词",
     "sectionDescription": "歌词显示、字号、行高与对齐。",
     "sectionOrder": 30, "order": 30},
    {"key": "lyrics_line_height", "label": "行高倍率", "type": "range",
     "default": 1.6, "min": 1.2, "max": 3.0, "step": 0.1,
     "help": "行间距倍率",
     "section": "lyrics", "sectionTitle": "歌词",
     "sectionDescription": "歌词显示、字号、行高与对齐。",
     "sectionOrder": 30, "order": 40},
    {"key": "lyrics_glow", "label": "文字发光效果", "type": "checkbox",
     "default": True, "help": "当前行文字发光",
     "section": "lyrics", "sectionTitle": "歌词",
     "sectionDescription": "歌词显示、字号、行高与对齐。",
     "sectionOrder": 30, "order": 50},
    {"key": "lyrics_align", "label": "歌词对齐", "type": "select",
     "options": [{"label": "居中", "value": "center"}, {"label": "左对齐", "value": "left"}],
     "default": "center", "help": "歌词文本对齐方式",
     "section": "lyrics", "sectionTitle": "歌词",
     "sectionDescription": "歌词显示、字号、行高与对齐。",
     "sectionOrder": 30, "order": 60},
    {"key": "lyrics_bg_color", "label": "歌词背景色", "type": "text",
     "placeholder": "如 #1a1a2e 留空为默认",
     "help": "纯色背景，支持 #RRGGBB 格式",
     "section": "lyrics-appearance", "sectionTitle": "歌词外观",
     "sectionDescription": "歌词颜色与背景效果。",
     "sectionOrder": 40, "order": 10},
    {"key": "lyrics_font_color", "label": "歌词字体颜色", "type": "text",
      "default": "#ffffff", "placeholder": "#ffffff",
      "help": "歌词文字颜色，支持 #RRGGBB 格式",
      "section": "lyrics-appearance", "sectionTitle": "歌词外观",
      "sectionDescription": "歌词颜色与背景效果。",
      "sectionOrder": 40, "order": 20},
    {"key": "lyrics_bg_blur", "label": "背景模糊度", "type": "range",
      "default": 8, "min": 0, "max": 50, "help": "背景模糊效果强度",
      "section": "lyrics-appearance", "sectionTitle": "歌词外观",
      "sectionDescription": "歌词颜色与背景效果。",
      "sectionOrder": 40, "order": 30},
    {"key": "lyrics_bg_brightness", "label": "背景明亮度", "type": "range",
      "default": 0.25, "min": 0.05, "max": 1.0, "step": 0.05,
      "help": "背景亮度调节",
      "section": "lyrics-appearance", "sectionTitle": "歌词外观",
      "sectionDescription": "歌词颜色与背景效果。",
      "sectionOrder": 40, "order": 40},
    {"key": "auto_hide_enabled", "label": "全屏自动隐藏控件", "type": "checkbox",
     "default": True, "help": "进入全屏后鼠标静止自动隐藏控件",
     "section": "playback", "sectionTitle": "播放",
     "sectionDescription": "视频默认播放模式、播放起点与全屏控件行为。",
     "sectionOrder": 20, "order": 30},
    {"key": "auto_hide_delay", "label": "自动隐藏延迟（秒）", "type": "range",
     "default": 3, "min": 0, "max": 10, "step": 1,
     "section": "playback", "sectionTitle": "播放",
     "sectionDescription": "视频默认播放模式、播放起点与全屏控件行为。",
     "sectionOrder": 20, "order": 40},
    {"key": "default_video_mode", "label": "视频默认播放模式", "type": "select",
     "options": [{"label": "画面模式", "value": "video"}, {"label": "仅声音", "value": "audio"}],
     "default": "video", "help": "视频默认以画面或仅声音播放",
     "section": "playback", "sectionTitle": "播放",
     "sectionDescription": "视频默认播放模式、播放起点与全屏控件行为。",
     "sectionOrder": 20, "order": 10},
    {"key": "resume_mode", "label": "播放起点", "type": "select",
     "options": [{"label": "从头播放", "value": "restart"},
                 {"label": "保留播放进度，低于5s从头开始", "value": "resume"}],
     "default": "restart",
     "help": "从头播放：每次点击曲目都从 0 开始；保留播放进度：续播上次位置，"
             "但剩余不足 5 秒时一律从头开始（音频、视频同样生效）",
     "section": "playback", "sectionTitle": "播放",
     "sectionDescription": "视频默认播放模式、播放起点与全屏控件行为。",
     "sectionOrder": 20, "order": 20},
    {"key": "ffmpeg_path", "label": "ffmpeg 路径（视频封面抽取）", "type": "text",
     "placeholder": "留空自动检测 PATH",
     "help": "可选：填写 ffmpeg 可执行文件路径（如 C:\\ffmpeg\\bin\\ffmpeg.exe 或所在目录）；"
            "留空时自动检测 PATH。检测到 ffmpeg 后视频封面由后端直接抽取（无需打开页面），"
            "检测不到则回退为前端抽帧。",
     "section": "video", "sectionTitle": "视频与封面",
     "sectionDescription": "视频封面抽取使用的 ffmpeg 路径。",
     "sectionOrder": 50, "order": 10},
]

#: 播放队列持久化上限：状态文件按 id 列表存储，只保留前若干条以约束文件体积；
#: 超出时置 queue_truncated，前端恢复的是可恢复的那部分。
QUEUE_PERSIST_LIMIT = 1000
