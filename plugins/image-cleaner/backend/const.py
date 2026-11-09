"""image-cleaner 的常量与纯数据：设置项 schema、扫描扩展名、扩展入口条目。

只有入口加载本模块（分层门禁 R3）；组件需要的东西一律由入口按构造参数注入。
设置项 schema 与扩展条目是"插件对壳声明什么"的**数据**，不是装配，所以不放在入口：
入口的行数预算留给装配与壳按名调用的钩子。
"""

import copy
from typing import Any, Dict, List

from shell.backend.media_catalog import IMAGE_EXTENSIONS

#: 扫描范围：与宿主「图片相册」同源的图片扩展名集合。
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS

#: dHash 缓存条目的版本标记（`DHashStore` 写入的 `'v'`）。改哈希算法或归一化规则时 +1：
#: 旧条目一律重算，同时作为统一刷新的 `content_version` 整体作废指纹库。
DHASH_VERSION = 2

#: 结果缓存的两个模式：分组结果按它分键，`get_cached_scan` 也按它取值。
SCAN_MODES = ('dupe', 'similar')

SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    # 只读信息行（`type: "info"`）：扫描的就是宿主「图片相册」的数据根目录，
    # 值由 get_settings() 现取，前端只展示、不提交（见 plugin_base.save_settings）。
    # 原先这一行显示在内嵌页自己的工具栏上，那一排已整条去掉。
    {"key": "root_dir", "label": "相册根目录", "type": "info",
     "section": "host", "sectionTitle": "宿主目录",
     "sectionDescription": "扫描范围来自「图片相册」的根目录；多根时逐行列出，本插件只读。",
     "sectionOrder": 10, "order": 10,
     "help": "扫描范围取自「图片相册」的根目录；要换目录请到图片相册的设置里改"},
    {"key": "threshold", "label": "相似判定阈值", "type": "range",
     "section": "threshold", "sectionTitle": "阈值",
     "sectionDescription": "dHash 汉明距离越小越严格；0 表示只有完全一致的 dHash 才判为相似。",
     "sectionOrder": 20, "order": 10,
     "min": 0, "max": 16, "default": 8,
     "help": "汉明距离越小越严格，0 表示只有完全一致的 dHash 才判为相似"},
]

#: 注册到 image-viewer 左侧栏的扩展入口（壳按字段取值，纯数据）。
#: 新壳按 `native` 声明在宿主页内容槽渲染原生面板：逻辑页隐藏，只提交 schema
#: blocks 并处理动作。`embedUrl` 保留给不认识 `native` 的旧壳，继续走内嵌页回落。
EXTENSION_ENTRIES: List[Dict[str, Any]] = [{
    'host': 'image-viewer',
    'id': 'image-cleaner',
    'label': '相册清理',
    'icon': 'icon:brush-cleaning',
    'description': '扫描全部相册中的重复 / 相似图片',
    'section': '相册清理',  # 侧边栏独立分组标题（不与其他扩展挤在一个标题下）
    'native': {
        'url': '/plugins/image-cleaner/frontend/native.html?native=1',
        'title': '相册清理',
    },
    'embedUrl': '/plugins/image-cleaner/frontend/index.html',
    'placement': 'sidebar',
    'scope': 'all',
}]


def extension_entries() -> List[Dict[str, Any]]:
    """壳要的扩展条目：每次返回一份新对象（调用方可能就地补字段）。"""
    return copy.deepcopy(EXTENSION_ENTRIES)
