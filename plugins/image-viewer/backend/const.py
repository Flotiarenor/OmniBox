"""image-viewer 内部的纯数据：设置项 schema、常量、上限与硬默认值。

为什么单独一个文件：插件后端不是包，组件之间不能互相 import（分层门禁的 R3），
所以"多个组件都要用"的东西只能由入口加载一次、再按构造参数分发下去。这里放三类：

* 纯常量：允许的图片扩展名、相册索引的 schema 版本号、设置项 schema 与硬默认值；
* `Limits`：成组的数值上限。它们总是一起被同一批组件读取，逐个当构造参数传会让
  每个组件的签名都变成一长串数字，而且"改一个 TTL 要改五处调用点"；
* `SETTINGS_SCHEMA`：插件对壳声明的设置项。它是**数据**不是装配，因此不放在入口 ——
  入口的行数预算要留给装配、API 绑定表与壳钩子。

`Limits` 还持有唯一的截断策略（`limit_all_images`）：连续浏览序列的 5000 条上限
同时被列表与灯箱序列用到，放在这里才有唯一实现。

只有入口 `main.py` load_sibling 本文件；组件通过构造参数拿到 `Limits` 与扩展名集合。
"""

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Tuple

#: 本插件认的图片扩展名。扫描、删除时的"目录内还有图片"判定、刷新基建的
#: `include` 都用这一份，避免三处各写一份后漂移。
IMAGE_EXTENSIONS: FrozenSet[str] = frozenset(
    {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'})


@dataclass(frozen=True)
class Limits:
    """相册索引与列表的性能上限。

    这些数值都来自实测（见各组件的 docstring）：调小会在慢盘上重新变卡，调大则
    吃内存或让一帧变慢，因此集中在一处、由入口一次注入。
    """

    #: `list_albums` 全树扫描结果的进程内复用时长。
    albums_ttl: float = 30.0
    #: 全树目录枚举（os.walk）结果的复用时长。目录增删由统一刷新基建的被动同步
    #: 发现并立刻作废本缓存，因此这里可以放宽到 10 分钟，避免每次进相册页都走盘。
    dirs_ttl: float = 600.0
    #: 相册索引缓存的 schema 版本。封面挑选规则变更时必须 +1，否则旧缓存会让新
    #: 规则不生效（目录 mtime 没变 → 增量扫描直接复用旧的封面）。
    album_cache_version: int = 4
    #: 列表缓存最大条目数，超出后淘汰最旧一半。
    max_list_cache: int = 200
    #: 连续浏览序列最大返回条数，超出截断。
    max_all_images: int = 5000
    #: 扫描 / 尺寸读取的并行线程数。
    scan_workers: int = 8

    def limit_all_images(self, images: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
        """截断连续浏览序列，返回 (序列, 是否截断)。

        超大相册不全量下发，防止内存与带宽放大；调用方据此给前端"已截断"提示。
        """
        if len(images) > self.max_all_images:
            return images[:self.max_all_images], True
        return images, False


LIMITS = Limits()

#: 按目录设置缺失时的硬默认值。字段与 `settings_schema` 的视图类设置项一一对应；
#: `root_dir` / `extra_roots` 不在其中 —— 它们决定 `/file` 的允许根，永远只从
#: 插件级设置读，不参与按目录继承。
HARD_DEFAULTS: Dict[str, Any] = {
    'row_height': 200,
    'per_page': 40,
    'sort_by': 'mtime',
    'sort_order': 'desc',
    'pixiv_fuzzy': False,
    'album_sort_by': 'mtime',
    'album_sort_order': 'desc',
}

#: 改这些键要重算覆盖规则（排序 / 封面挑选依据），因此必须立刻作废列表与索引缓存，
#: 而不是等 TTL 过期。
SORT_KEYS: FrozenSet[str] = frozenset(
    {'sort_by', 'sort_order', 'pixiv_fuzzy', 'album_sort_by', 'album_sort_order'})

#: 改这些键要换根：派生路径、缩略图库、尺寸元数据、相册索引全部重来。
ROOT_KEYS: FrozenSet[str] = frozenset({'root_dir', 'extra_roots'})


#: 插件的设置项声明（纯数据，不是装配）。放在这里而不是入口：入口的行数预算要留给
#: 装配、API 绑定表与壳钩子，而 schema 是"插件对外声明什么设置项"的数据——把它挪进
#: 入口只会把装配挤到行数上限附近，让下一次加 API 键的人被迫去删注释。
#: 入口以 `settings_schema = SETTINGS_SCHEMA` 引用它，`PluginBase` 的读写与壳的设置
#: 弹窗都从类属性读取，行为与声明在入口里完全一致。
SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    # root_dir / extra_roots 决定 `/file`、`/thumbs` 的允许根，改动它们等于改动本机
    # 可读/可删/可移的文件范围 —— 由 PluginBase 的 admin_only 判定统一限权。
    {"key": "root_dir", "label": "数据根目录", "type": "text", "admin_only": True,
     "placeholder": "默认: ./data", "help": "图片浏览的数据根目录（改动需管理员）"},
    {"key": "extra_roots", "label": "额外图片目录", "type": "textarea", "admin_only": True,
     "placeholder": "每行一个目录（也可在插件设置面板里增删）",
     "help": "与主根目录一起浏览：每个目录在相册树里显示为顶层节点（改动需管理员）"},
    {"key": "row_height", "label": "图片行高", "type": "range",
     "min": 100, "max": 400, "default": 200, "help": "Justified 布局的每行目标高度"},
    {"key": "per_page", "label": "每页图片数", "type": "number",
     "min": 10, "max": 200, "default": 40},
    {"key": "lightbox_cache", "label": "图片缓存", "type": "range",
     "min": 0, "max": 64, "default": 8,
     "help": "缓存最近看过的图，再打开时直接显示，不用等它一行行刷出来。"
             "数值越大越流畅，占内存也越多；0 = 关闭"},
    {"key": "sort_by", "label": "排序方式", "type": "select",
     "default": "mtime",
     "options": [{"label": "修改时间", "value": "mtime"},
                 {"label": "文件名", "value": "name"},
                 {"label": "Pixiv 排序支持", "value": "time_name"}]},
    {"key": "sort_order", "label": "排序方向", "type": "select",
     "default": "desc",
     "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
    {"key": "pixiv_fuzzy", "label": "模糊匹配", "type": "checkbox",
     "default": False,
     "help": "仅「Pixiv 排序支持」生效：作品名不以前导数字开头时按"
             "「先数字再文字」排序，并让该文件夹套用 Pixiv 的浏览效果"},
    {"key": "album_sort_by", "label": "作者视图排序方式", "type": "select",
     "default": "mtime",
     "options": [{"label": "更新时间", "value": "mtime"},
                 {"label": "文件名", "value": "name"},
                 {"label": "图片数量", "value": "count"}]},
    {"key": "album_sort_order", "label": "作者视图排序方向", "type": "select",
     "default": "desc",
     "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
]
