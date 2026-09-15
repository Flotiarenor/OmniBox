"""image-viewer 后端各分片共用的模块级常量与纯函数。

为什么单独一个文件：后端入口由 PluginManager 用 importlib 直接加载，分片之间不能
import main.py（会成环），所以共用的常量与纯函数需要一个中立的落点。main.py 把这些
名字重新绑定成原来的名字，于是 main.py 的既有引用、以及测试对 `module._pick_cover`
的引用都不用改。

本文件全部为**无实例状态**的模块级定义，与同目录的 filesystem.py 同类 —— 后者是更
底层的纯函数工具（路径安全、自然序、尺寸元数据），由本文件通过 load_sibling 转出。
"""

import logging
import os
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

# 虚拟命名空间路径的保留前缀：第二个及以后的根目录在相册树里以
# `__<目录名>` 作为顶层节点。真实目录名几乎不可能以它开头，解析时也不会
# 与第一根的物理目录混淆（`__` 开头的路径段一律按命名空间解释）。
NAMESPACE_MARKER = '__'

_fs = load_sibling(__file__, 'filesystem', 'image_viewer')
ALLOWED_EXTENSIONS = _fs.ALLOWED_EXTENSIONS
drop_image_meta = _fs.drop_image_meta
ensure_thumbnail = _fs.ensure_thumbnail
get_image_size = _fs.get_image_size
is_safe_path = _fs.is_safe_path
list_directory = _fs.list_directory
natural_sort_key = _fs.natural_sort_key
pixiv_number = _fs.pixiv_number
stat_mtime = _fs.stat_mtime


def pixiv_sort(entries, name_fn, reverse, fuzzy=False):
    """Pixiv 排序：前导数字条目按数字大小排（方向生效），
    无前导数字条目按自然名排并始终位于最后。

    `fuzzy`（模糊匹配）只改**同号/无号条目之间**的比较键：关掉时同号内按
    文件名字符串自然序，打开时按整名的自然序（数字段比数字、其余段比文字），
    于是 `2024-05-10` < `2024-05-24 日富美` 这类「先数字再文字」的条目也能排对。
    """
    numeric, other = [], []
    for it in entries:
        name = name_fn(it)
        num = pixiv_number(name)
        key = (num, natural_sort_key(name if fuzzy else Path(name).name))
        (numeric if num is not None else other).append((key, it))
    numeric.sort(key=lambda t: t[0], reverse=reverse)
    other.sort(key=lambda t: t[0], reverse=reverse)
    return [it for _, it in numeric] + [it for _, it in other]


def pick_cover(images: List[Dict], pixiv: bool, name_fn=lambda img: img['rel']) -> str:
    """目录封面挑选，返回选中图片的 name_fn 值（`_scan_dir_direct` 里即 rel 相对路径）。

    Pixiv 树（`sort_by == 'time_name'`）取**作品号最大**的那张 —— 画师目录里
    平铺着 `<pid>.jpg` / `<pid>_p0.jpg`（pixiv-sync 的落盘规则），按自然序第一张
    取封面等于永远展示该画师最老的作品；同一作品（同 pid）内再取文件名自然序
    第一张，即 p0 而不是 p1/p10。非 Pixiv 目录保持原有行为：文件名自然序第一张。
    """
    if not images:
        return ''
    if pixiv:
        numbered = [(pixiv_number(Path(name_fn(img)).name), img) for img in images]
        known = [num for num, _ in numbered if num is not None]
        if known:
            newest = max(known)
            same_work = [img for num, img in numbered if num == newest]
            return name_fn(min(same_work, key=lambda img: natural_sort_key(Path(name_fn(img)).name)))
    return name_fn(min(images, key=lambda img: natural_sort_key(Path(name_fn(img)).name)))


def cover_rank(cover_rel: str, mtime: float, pixiv: bool) -> tuple:
    """封面优先级键（可直接 max() 比较）。

    Pixiv 树按作品号大者优先（= 画师最近的作品），非 Pixiv 目录沿用
    「mtime 更新者优先」；Pixiv 条目整体优先于无号的普通条目。

    作品号一律取**封面文件名的前导数字**：pixiv-sync 落盘的是
    `<pid>.jpg` / `<pid>_pN.jpg`，嵌在目录里的 `.../800_title/800_p0.png`
    同样以作品号开头，所以看文件名既覆盖平铺也覆盖嵌套结构。
    """
    num = pixiv_number(Path(cover_rel).name) if cover_rel else None
    if pixiv and num is not None:
        return (1, num, 0.0)
    return (0, 0.0, mtime)


def same_path(a, b) -> bool:
    """两个路径是否指向同一位置（大小写/短名/软链接无关）。

    不能只比 `Path` 相等：Windows 上 `Path('C:\\Users\\ADMINI~1\\…').resolve()`
    不一定展开 8.3 短名，而同一位置的另一份写法可能已展开成长名，直接比较会把
    主根目录误判成「另一个根」。
    """
    if a is None or b is None:
        return False
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return Path(a) == Path(b)
