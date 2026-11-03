"""纯数据：设置项 schema 与各处共用的上限、目录名。

只有入口加载本模块（分层门禁的 R3）。组件需要这些值时由入口按构造参数注入，
组件之间不互相 import —— 这是"依赖方向可查"的前提。

设置项 schema 放在这里而不是入口：它是"插件对外声明什么设置项"的数据，不是装配；
留在入口会把装配挤到行数上限附近，让下一个加 API 键的人被迫删注释。`PluginBase`
与壳的设置弹窗都从类属性读，行为与写在入口里完全一致。
"""

from typing import Any, Dict, List

#: 书架派生数据的规则版本：收录哪些顶层目录、封面怎么挑变了就 +1，
#: 由统一刷新基建整体作废（不再依赖"记得手动清缓存"）。
SHELF_VERSION = 1

#: 「最近阅读」保留条数上限：`manga_update_recent` 先按 id 去重再截断。
RECENT_LIMIT = 20

#: `recent_count` 设置项的夹取范围（与 schema 的 min / max 一致）。
RECENT_COUNT_MIN = 1
RECENT_COUNT_MAX = 50

#: 下载状态落点：与旧 download-center 完全一致，任务记录可无缝继承。
DOWNLOAD_STATE_DIRNAME = '.jmcomic_state'
DOWNLOAD_STATE_FILE = 'download_state.json'

SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    {"key": "root_dir", "label": "漫画根目录", "type": "directory",
     "section": "path", "sectionTitle": "路径", "sectionOrder": 10,
     "sectionDescription": "漫画库所在目录；决定书架扫描范围与文件访问的允许根。",
     "order": 10,
     "admin_only": True,
     "placeholder": "输入目录绝对路径，如 D:\\漫画",
     "emptyText": "未添加任何目录，将使用默认数据目录（./data）",
     "help": "存放漫画文件夹的根目录；第一行即生效根目录，保存后生效。"
             "改动需管理员（它决定 /file 的允许根）"},
    {"key": "recent_count", "label": "最近阅读显示数量", "type": "number",
     "section": "browsing", "sectionTitle": "浏览行为", "sectionOrder": 20,
     "sectionDescription": "书架与最近阅读的分类展示方式。",
     "order": 10,
     "default": 10, "min": 1, "max": 50, "help": "首页「最近阅读」展示的漫画数量"},
]
