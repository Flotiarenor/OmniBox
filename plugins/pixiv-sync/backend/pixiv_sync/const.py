"""pixiv-sync 的纯数据：设置项 schema、缓存子目录、刷新基建认识的扩展名。

为什么单独成文件：`settings_schema` 是"插件向壳声明哪些设置项"的**数据**，
不是装配。留在入口会把装配挤到行数上限附近，下一个加 API 键的人被迫去删注释。
入口以 `settings_schema = SETTINGS_SCHEMA` 引用它，壳从类属性读，行为一致。
"""

from pathlib import Path
from typing import Any, Dict, List

#: 缓存子目录（相对下载根）。下载记录、任务状态、清单库都落在这里。
CACHE_SUBDIR = Path(".cache") / "pixiv-sync"

#: 统一刷新基建认识的图片扩展名：键集合必须能反推出作品页数，
#: 因此与 store.ID_NAME_RE 认识的扩展名保持一致，改动要两边一起改。
FRESHNESS_INCLUDE = ('.jpg', '.jpeg', '.png', '.gif', '.webp')

SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    {
        "key": "refresh_token",
        "label": "Pixiv Refresh Token",
        "section": "credentials",
        "sectionTitle": "账号凭据",
        "sectionDescription": "账号级长期凭据；也可用顶栏「获取 Token」走内置 OAuth。",
        "sectionOrder": 10,
        "order": 10,
        "type": "text",
        # 申请 Shell 文件防护：本插件的设置文件（<config>/plugins/pixiv-sync.json）
        # 会被 /file、/files、/thumbs 无条件拒绝。refresh_token 是账号级长期凭据
        # （可无限换取 access_token 且自动轮换），而插件文件根由设置改写，
        # 默认数据根与配置目录是兄弟目录 —— 没有这条申报，一次同源脚本执行
        # 就能把凭据读走。见 PluginBase.get_protected_paths。
        "secret": True,
        "placeholder": "粘贴 refresh_token（非 access_token）",
        "help": "获取: gppt (github.com/eggplants/get-pixivpy-token) 或插件内 OAuth 向导",
    },
    {
        "key": "proxy",
        "label": "HTTP 代理",
        "section": "network",
        "sectionTitle": "网络",
        "sectionDescription": "中国大陆访问 pixiv 通常需要代理；请求速率与并行拉取也在本组。",
        "sectionOrder": 20,
        "order": 10,
        "type": "text",
        "placeholder": "http://127.0.0.1:7890",
        "help": "中国大陆访问 pixiv 需要代理；留空 = 直连",
    },
    {
        "key": "download_dir",
        "label": "下载目录",
        "section": "download",
        "sectionTitle": "下载",
        "sectionDescription": "下载落点与并发；目录改动需要管理员。",
        "sectionOrder": 30,
        "order": 10,
        # directory 类型由壳的共享组件 folder-picker 渲染（单值字段取首行路径，
        # 见 base.js 的 createSettingsForm）；原先声明成 text 时用户只能手打路径
        "type": "directory",
        # 它决定插件的落盘根（CachePaths.root），也就决定 /file 的允许根之一：
        # 改写它等于让下载内容落到任意目录、并把该目录对界面开放。
        "admin_only": True,
        "placeholder": "默认: image-viewer 数据根目录",
        "help": "留空 = 写入 image-viewer 相册根目录，下载后自动出现在相册（改动需管理员）",
    },
    # 注：原图下载与多图子文件夹为固定行为（默认开启），不提供设置开关；
    #     如需调整请直接改 backend/pixiv_sync/download.py 中 all_image_urls 的
    #     want_original 与 process_illust 的 subfolder 判定。
    {
        "key": "workers",
        "label": "并发下载数",
        "section": "download",
        "sectionTitle": "下载",
        "sectionDescription": "下载落点与并发；目录改动需要管理员。",
        "sectionOrder": 30,
        "order": 20,
        "type": "number",
        "default": 4,
        "min": 1,
        "max": 8,
        "help": "同时下载的画师/作品数；机械盘建议 1-2，SSD 可 4-8",
    },
    {
        "key": "max_download",
        "label": "单次同步上限（条）",
        "section": "limits",
        "sectionTitle": "扫描与上限",
        "sectionDescription": "单次同步与全量扫描的数量上限；0 = 不限。",
        "sectionOrder": 40,
        "order": 10,
        "type": "number",
        "default": 100,
        "min": 0,
        "max": 10000,
        "help": "每次「同步画师/同步喜欢」最多下载的作品数；0 = 不限。想分批下载可设小值，下完再点同步继续",
    },
    {
        "key": "max_artists",
        "label": "单次全量扫描画师数上限",
        "section": "limits",
        "sectionTitle": "扫描与上限",
        "sectionDescription": "单次同步与全量扫描的数量上限；0 = 不限。",
        "sectionOrder": 40,
        "order": 20,
        "type": "number",
        "default": 30,
        "min": 0,
        "max": 500,
        "help": "每次同步最多「全量扫描」（首次入库/新关注画师拉全部历史）的画师数；仅需增量更新的画师不受此限制、会一次扫完。实测约 30 个全量画师可能触发 429，建议保持默认。0 = 不限（有 429 风险）",
    },
    {
        "key": "rate_limit",
        "label": "API 请求速率（次/秒）",
        "section": "network",
        "sectionTitle": "网络",
        "sectionDescription": "中国大陆访问 pixiv 通常需要代理；请求速率与并行拉取也在本组。",
        "sectionOrder": 20,
        "order": 20,
        "type": "number",
        "default": 3,
        "min": 1,
        "max": 10,
        "help": "app-api 请求限速，间隔带随机抖动；pixiv 限流阈值约 3/s，调高有 429 风险",
    },
    {
        "key": "scan_workers",
        "label": "并行拉取画师数（滑动窗口）",
        "section": "network",
        "sectionTitle": "网络",
        "sectionDescription": "中国大陆访问 pixiv 通常需要代理；请求速率与并行拉取也在本组。",
        "sectionOrder": 20,
        "order": 30,
        "type": "number",
        "default": 4,
        "min": 1,
        "max": 8,
        "help": "刷新名单时同时拉取列表的画师数；总速率仍受 rate_limit 限制，此值只是滑动窗口大小",
    },
]


def extensions() -> List[dict]:
    """宿主左栏的扩展入口声明。

    新壳读 `native` 在宿主页内容槽渲染原生面板（`frontend/native.html` 是隐藏的
    逻辑页，只提交 schema blocks 并处理动作）；`embedUrl` 保留给不认 `native`
    的旧壳，继续承载 `frontend/index.html` 的完整页面。
    """
    return [
        {
            "host": "image-viewer",
            "id": "pixiv-sync",
            "label": "Pixiv 同步",
            "icon": "icon:palette",
            "description": "同步下载关注画师新作与收藏画作",
            "section": "Pixiv 同步",
            "native": {
                "url": "/plugins/pixiv-sync/frontend/native.html?native=1",
                "title": "Pixiv 同步",
            },
            "embedUrl": "/plugins/pixiv-sync/frontend/index.html",
            "placement": "sidebar",
            "scope": "all",
        }
    ]
