"""document-reader 后端的共享常量与设置项声明。

只有入口加载本模块：`settings_schema`、扩展名映射、状态文件名与朗读上限都是纯数据，
由入口按构造参数分发给各组件。组件之间不互相 import，也就不需要各自加载一份。

`LayoutNames` 把「状态目录里的文件叫什么」收成一个不可变对象：roots 负责目录布局、
library 负责缓存与进度文件、marks 负责书签文件，三者都只认这里的一份定义。改名迁移
用的旧名字也在这里 —— 迁移代码需要同时知道新名与旧名。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# ===== 状态目录与文件名 =====

CACHE_FILE = '.document_cache.json'
PROGRESS_FILE = '.document_progress.json'
# 书签文件名是 marks 那一块的实现细节，入口把它当构造参数传下去
MARKS_FILE = '.document_marks.json'
# 章节缓存键加入格式与编码、文档 id 改为完整文件名（含扩展名）后升版：
# 旧版的章节/偏移缓存与新的键对不上，留着只会变成读不到的垃圾。
# v4：TxtParser 的编码兜底修好后，同一本书解出来的正文长度会变（GBK 文件过去被
# 按 utf-8 + ignore 解成了乱码），旧偏移切片到新正文上就是错位的章节。
CACHE_VERSION = 4

CACHE_DIR_NAME = '.document_state'
# 改名前的名字：只用于把旧数据搬过来，别拿它们当新写入口
LEGACY_CACHE_DIR = '.novel_state'
LEGACY_CACHE_FILE = '.novel_cache.json'
LEGACY_PROGRESS_FILE = '.novel_progress.json'
# 插件改名：旧名下的设置文件里存着用户配置的 root_dir
LEGACY_PLUGIN_NAME = 'novel-reader'


@dataclass(frozen=True)
class LayoutNames:
    """状态目录的布局名：目录名、文件名、旧名与解析器缓存版本。"""

    cache_dir: str = CACHE_DIR_NAME
    cache_file: str = CACHE_FILE
    progress_file: str = PROGRESS_FILE
    marks_file: str = MARKS_FILE
    legacy_dir: str = LEGACY_CACHE_DIR
    legacy_cache_file: str = LEGACY_CACHE_FILE
    legacy_progress_file: str = LEGACY_PROGRESS_FILE
    legacy_plugin: str = LEGACY_PLUGIN_NAME
    cache_version: int = CACHE_VERSION


LAYOUT = LayoutNames()

# ===== 格式 =====

# 扩展名 → 处理方式。
#   txt      原有偏移切片（前端转义成段落，行为逐字节不变）
#   md/epub  documents.py / epub.py 转成白名单 HTML
#   pdf      阅读器内嵌 iframe，交给 WebView 自带的阅览器
#   external 列出来，但只能交给系统默认程序打开
KIND_BY_EXT: Dict[str, str] = {
    '.txt': 'txt',
    '.md': 'md',
    '.markdown': 'md',
    '.epub': 'epub',
    '.pdf': 'pdf',
    '.docx': 'external',
    '.doc': 'external',
    '.odt': 'external',
    '.rtf': 'external',
    '.mobi': 'external',
    '.azw3': 'external',
    '.fb2': 'external',
    '.chm': 'external',
}

# 有章节模型的格式：前端按"目录 + 章节"阅读；其余的走专门分支
IN_APP_KINDS: Tuple[str, ...] = ('txt', 'md', 'epub')

# ===== 朗读 =====

# 朗读缓存目录名：`<config>/plugins/<这个>/tts/`。写死成插件目录名而不是取 manifest
# 的 name —— 改名迁移只搬设置与状态目录，缓存位置变了会让旧音频全部取不到。
TTS_CACHE_SUBDIR = 'document-reader'

# 缓存目录上限：到量就从最旧的开始删（音频 + 时间戳成对删）
TTS_CACHE_LIMIT = 200 * 1024 * 1024
# 单次合成结果的合理上限：超过它说明引擎返回了异常内容，别往磁盘写
TTS_RESULT_LIMIT = 40 * 1024 * 1024

# 朗读预取深度（句）：前端在播放当前句时，同时去要后面几句。
# 一句一合成时的硬指标是"合成下一句的耗时必须小于上一句的音频长度"：短句（五六个字，
# 音频约 1.6 秒）撑不住本地端点一次约 1.9 秒的合成，只预取一句必然在这些地方卡住。
# 深度就是那个旋钮 —— 大了白占端点与磁盘（首句等待也会变长），小了在短句处等。
# 5 按"短句 1.6s 音频 / 单句合成 1.95s"这类实测场景定；tool/tts_speed.py 能按当前
# 设置重算卡顿次数，改过引擎或语速后应当重跑一次。
TTS_PREFETCH_DEFAULT = 5
TTS_PREFETCH_MIN = 1
# 上限 8：再多也只是把请求压在端点上排队，而首句的等待会跟着一起变长
TTS_PREFETCH_MAX = 8


@dataclass(frozen=True)
class TtsLimits:
    """朗读的数值上限与预取深度档位。"""

    cache_bytes: int = TTS_CACHE_LIMIT
    result_bytes: int = TTS_RESULT_LIMIT
    prefetch_default: int = TTS_PREFETCH_DEFAULT
    prefetch_min: int = TTS_PREFETCH_MIN
    prefetch_max: int = TTS_PREFETCH_MAX


TTS_LIMITS = TtsLimits()

# ===== 设置项声明 =====
#
# schema 同时是 `save_settings` 的写入白名单（见 shell/backend/plugin_base.py 的
# allowed），删掉这些键会让朗读设置页保存不上（它走的就是 save_settings）。

SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    {"key": "root_dir", "label": "文档根目录", "type": "directory", "multi": True,
     "admin_only": True,
     "placeholder": "输入目录绝对路径，如 D:\\文档",
     "emptyText": "未添加任何目录，将使用默认数据目录（./data）",
     "help": "可添加多个目录（每行一个，第一行为主目录，缓存与阅读进度存在它下面）；"
             "每个目录都会递归扫描子目录。改动需管理员（它决定 /file 的允许根）",
     "section": "storage", "sectionTitle": "文档目录",
     "sectionDescription": "书架扫描的根目录。多个目录每行一个，第一行为主目录（缓存与阅读进度存在它下面）。",
     "sectionOrder": 10, "order": 10},
    # ===== 朗读（引擎与参数整理见 backend/tts.py、backend/tts_engine.py）=====
    # 这一组已迁到左栏的「朗读设置」页（`frontend/js/reader-voice-page.js`）：
    # 引擎三档、端点、Key、模型名、音色与语速、音色候选表与试听、缓存管理都在
    # 那一页。它们在 schema 里标 `hidden: True`，壳的插件设置弹窗不再重复列一遍 ——
    # 同一批配置摆两处时，用户改了一处却看到另一处还是旧值，无法判断哪一份生效。
    #
    # 这正是本仓库原来的 `central: False` 语义（见 1a4cb1d：朗读项与阅读偏好都标
    # `central: False`，"集中设置面板只剩 root_dir"）。`central` 标记随
    # 2e8917d 一起删掉了，现在的等价物就是 `hidden`。
    #
    # 保留在 schema 里是**必须**的：schema 同时是 `save_settings` 的写入白名单
    # （见 shell/backend/plugin_base.py 的 allowed），删掉这些键会让朗读设置页
    # 保存不上（它走的就是 save_settings）。
    #
    # `admin_only` 与 `secret` 语义不受影响：朗读设置页上的端点与 Key 仍然要求
    # 管理员主体、仍然不回显明文 —— 判定在 PluginBase 里，与面板显示与否无关。
    {"key": "tts_engine", "label": "朗读引擎", "type": "select", "default": "auto",
     "hidden": True,
     "options": [
         {"label": "自动（按下面的顺序降级）", "value": "auto"},
         {"label": "edge-tts（神经音色，联网，支持逐字跟随）", "value": "edge"},
         {"label": "OpenAI 兼容端点（本地模型/云服务）", "value": "openai"},
         {"label": "系统离线音色（SAPI5，兜底）", "value": "system"},
     ],
     "section": "tts", "sectionTitle": "朗读", "sectionOrder": 40, "order": 10},
    {"key": "tts_order", "label": "自动模式顺序", "type": "text", "default": "openai,edge,system",
     "help": "逗号分隔，留空用默认顺序。音色属于哪一档就用哪一档念，其余档跳过；"
             "想省钱只用微软音色：edge,system",
     "section": "tts", "sectionTitle": "朗读",
     "sectionDescription": "朗读引擎顺序与端点参数；更多音色、语速与缓存选项在左栏「朗读设置」页。",
     "sectionOrder": 40, "order": 20},
    # 端点是"进程会带着 Authorization: Bearer <tts_api_key> 去请求的地址"：
    # 允许任意主体改写它，等于把 owner 配置的凭据交给任意主机（并让本进程成为
    # 可读回内网响应的 SSRF 跳板）。两个键都声明 admin_only，由 PluginBase 统一判定。
    {"key": "tts_base_url", "label": "OpenAI 兼容端点", "type": "text", "default": "",
     "admin_only": True, "hidden": True,
     "placeholder": "http://127.0.0.1:8880",
     "help": "只填根地址，程序会请求 <它>/v1/audio/speech。插件不含模型，"
             "模型由你自己的服务提供（Kokoro-FastAPI / GPT-SoVITS / 云 API 均可）；"
             "该地址会收到 Authorization 头，改动需管理员",
     "section": "tts", "sectionTitle": "朗读", "sectionOrder": 40, "order": 30},
    {"key": "tts_api_key", "label": "端点 API Key", "type": "text", "default": "",
     "secret": True, "admin_only": True, "hidden": True,
     "help": "本地服务通常不需要；云服务填这里（保存后不明文回显，改动需管理员）",
     "section": "tts", "sectionTitle": "朗读", "sectionOrder": 40, "order": 40},
    {"key": "tts_model", "label": "端点模型名", "type": "text", "default": "",
     "hidden": True,
     "placeholder": "留空则不发送 model 字段",
     "section": "tts", "sectionTitle": "朗读", "sectionOrder": 40, "order": 50},
    {"key": "tts_voice", "label": "朗读音色", "type": "text", "default": "zh-CN-XiaoxiaoNeural",
     "hidden": True,
     "help": "edge-tts 用 ShortName（如 zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural），"
             "OpenAI 兼容端点用端点自己的音色名（如 vivian、alloy）；"
             "两边的候选表在朗读设置页里按引擎列出",
     "section": "tts", "sectionTitle": "朗读", "sectionOrder": 40, "order": 60},
    {"key": "tts_rate", "label": "朗读语速", "type": "range", "min": -50, "max": 100,
     "step": 5, "default": 100, "hidden": True,
     "help": "百分比：100 是端点允许的倍速上限（约两倍速），听书默认拉满；"
             "-50 最慢。再往上端点会开始吞字，所以上限就到这里",
     "section": "tts", "sectionTitle": "朗读", "sectionOrder": 40, "order": 70},
    # 预取深度：播放当前句时同时合成后面几句。一句一合成时，短句的音频（几秒）
    # 撑不住一次合成（本地端点约两秒），只预取一句必然在短句处卡住；这个窗口用
    # 前几句的播放时间一起吸收那段开销。调小了卡顿回来，调大了首句等待变长。
    # `type: range` 只是声明值域是 1-8 的整数（min/max 由这里的常量生成，测试按它
    # 与后端夹取对齐）；朗读设置页实际用下拉框渲染，8 个候选写在 index.html 里 ——
    # 滑杆看不出档位，也说不出「5 句」意味着什么。hidden 时壳不会渲染这个 type。
    {"key": "tts_prefetch", "label": "预取深度（句）", "type": "range",
     "min": TTS_PREFETCH_MIN, "max": TTS_PREFETCH_MAX, "step": 1,
     "default": TTS_PREFETCH_DEFAULT, "hidden": True,
     "help": "播放当前句时同时合成后面几句。端点跟不上时选 1（退回逐句现取）；"
             "改动下一次朗读生效",
     "section": "tts", "sectionTitle": "朗读", "sectionOrder": 40, "order": 80},
    # ===== 阅读偏好 =====
    # 这一组**显示在设置面板里**：它就是"文档阅读维护的设置"，打开文档后正文怎么
    # 显示（字号 / 行距 / 字间距 / 主题 / 配色 / 正文底色 / 翻页方式）都从这里改。
    # 与朗读设置的分工是"正文怎么显示" vs "怎么念出来"，两组互不重叠。
    # 左栏的「阅读设置」弹窗是同一批值的快捷入口，两条路写同一份 save_settings。
    {"key": "reader_font_size", "label": "正文字号(px)", "type": "number", "default": 16,
     "section": "reading", "sectionTitle": "阅读",
     "sectionDescription": "字号、行距、字间距与翻页方式；与左栏「阅读设置」弹窗写同一份设置。",
     "sectionOrder": 20, "order": 10},
    {"key": "reader_line_height", "label": "行距", "type": "number", "default": 1.8,
     "section": "reading", "sectionTitle": "阅读", "sectionOrder": 20, "order": 20},
    {"key": "reader_letter_spacing", "label": "字间距(px)", "type": "number", "default": 0,
     "section": "reading", "sectionTitle": "阅读", "sectionOrder": 20, "order": 30},
    {"key": "reader_theme", "label": "阅读主题", "type": "select", "default": "auto",
     "options": [
         {"label": "跟随主题", "value": "auto"},
         {"label": "护眼纸", "value": "sepia"},
         {"label": "夜间", "value": "dark"},
         {"label": "绿色", "value": "green"},
         {"label": "蓝色", "value": "blue"},
         {"label": "自定义", "value": "custom"},
     ],
     "section": "appearance", "sectionTitle": "正文外观",
     "sectionDescription": "主题、配色与正文底色。",
     "sectionOrder": 30, "order": 10},
    {"key": "reader_bg_color", "label": "自定义背景色", "type": "text", "default": "#ffffff",
     "section": "appearance", "sectionTitle": "正文外观", "sectionOrder": 30, "order": 20},
    {"key": "reader_text_color", "label": "自定义文字色", "type": "text", "default": "#1a1a1a",
     "section": "appearance", "sectionTitle": "正文外观", "sectionOrder": 30, "order": 30},
    {"key": "reader_mode", "label": "阅读模式", "type": "select", "default": "scroll",
     "options": [
         {"label": "连续滚动", "value": "scroll"},
         {"label": "翻页（章内滚动）", "value": "page"},
     ],
     "section": "reading", "sectionTitle": "阅读", "sectionOrder": 20, "order": 40},
    # 正文底色：theme 跟随主题 / show 透出壳背景 / solid 指定色彩。
    # 与上面的"阅读主题"正交 —— 主题决定配色，这一项只决定底色是否透明。
    {"key": "reader_bg_mode", "label": "正文底色", "type": "select",
     "default": "show",
     "options": [
         {"label": "跟随主题", "value": "theme"},
         {"label": "透出壳背景", "value": "show"},
         {"label": "指定色彩", "value": "solid"},
     ],
     "section": "appearance", "sectionTitle": "正文外观", "sectionOrder": 30, "order": 40},
    {"key": "reader_body_color", "label": "正文底色色彩", "type": "text",
     "default": "#ffffff",
     "section": "appearance", "sectionTitle": "正文外观", "sectionOrder": 30, "order": 50},
]
