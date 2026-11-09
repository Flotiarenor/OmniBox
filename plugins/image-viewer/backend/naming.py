"""Pixiv 命名约定下的排序与封面策略。

为什么是一个类而不是散函数：这里的规则（作品号、页序号、作品分组、封面优先级）**只由
文件名决定**，是同一个业务概念的三四种问法；组件需要其中不止一种（扫描要挑封面与数更新
单元、相册树要比较封面优先级、列表要排序、时间线要按作品合并），而插件后端不是包、组件
之间不能互相 import。把这几条规则收成一个可以整体注入的对象，比往每个组件的构造函数里
各塞几个函数干净。

无状态：不含缓存、不碰磁盘、不读设置。调用方（入口）只构造一次。

规则出处：pixiv-sync 的落盘命名是 `<作品id>.jpg` / `<作品id>_p<页号>.jpg`，
多图作品在相册树里既可能是平铺的单图集合，也可能是一层作品目录。
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from shell.backend.media_catalog import natural_sort_key

#: 名称里的连续数字段：作品号取第一段，页序号取 `作品号_p页号` 里的第二段。
_NUM_RE = re.compile(r'(\d+)')
_PAGE_RE = re.compile(r'^(\d+)_p(\d+)')


class PixivNaming:
    """按前导作品号组织排序与封面。"""

    def number(self, name: Any) -> Optional[int]:
        """提取名称前导数字（Pixiv 作品 ID / 图片编号）；无前导数字返回 None。"""
        match = _NUM_RE.match(str(name))
        return int(match.group(1)) if match else None

    def page_index(self, name: Any) -> Optional[int]:
        """提取「作品号_p页号」命名里的页号（`800_p0.png` → 0）；不是该命名返回 None。

        pixiv-sync 的多图作品落盘规则是 `<作品id>_p<页号><扩展名>`，页序与排序方向
        无关；`800.jpg`、`2024-05-10 京都` 这类名字返回 None，由调用方按整名比较。
        """
        match = _PAGE_RE.match(str(name))
        return int(match.group(2)) if match else None

    def pick_cover(self, images: List[Dict], pixiv: bool,
                   name_fn: Callable[[Dict], str] = lambda img: img['rel']) -> str:
        """目录封面挑选，返回选中图片的 name_fn 值（扫描结果里即 rel 相对路径）。

        Pixiv 树（`sort_by == 'time_name'`）取**作品号最大**的那张 —— 画师目录里
        平铺着 `<pid>.jpg` / `<pid>_p0.jpg`，按自然序第一张取封面等于永远展示该画师
        最老的作品；同一作品（同 pid）内再取文件名自然序第一张，即 p0 而不是 p1/p10。
        非 Pixiv 目录保持原有行为：文件名自然序第一张。
        """
        if not images:
            return ''
        if pixiv:
            numbered = [(self.number(Path(name_fn(img)).name), img) for img in images]
            known = [num for num, _ in numbered if num is not None]
            if known:
                newest = max(known)
                same_work = [img for num, img in numbered if num == newest]
                return name_fn(min(same_work,
                                   key=lambda img: natural_sort_key(Path(name_fn(img)).name)))
        return name_fn(min(images, key=lambda img: natural_sort_key(Path(name_fn(img)).name)))

    def work_units(self, names: List[Any]) -> List[List[str]]:
        """把一个目录里的文件名按「作品」分组（扫描与时间线共用这一份实现）。

        多页作品在时间线里只占一张卡，分组必须与"打开作品目录看到的"一致：同一目录里
        `page_index` 认得的页按**作品号**归组，组内按页序号升序（同页序号再按名字自然
        序）；其余文件各成一组。分组边界是目录 —— 不同目录下同号的作品不会被并成一张卡。

        组顺序稳定：多页作品组按作品号升序在前，单文件组按名字自然序在后。单页的
        `800_p0` 也是长度为 1 的组，调用方按组大小决定它显示成作品还是单图。
        """
        pages: Dict[int, List[str]] = {}
        singles: List[str] = []
        for name in names:
            text = str(name)
            page = self.page_index(text)
            num = self.number(text) if page is not None else None
            if num is None:
                singles.append(text)
                continue
            pages.setdefault(num, []).append(text)
        units: List[List[str]] = []
        for num in sorted(pages):
            members = pages[num]
            members.sort(key=lambda item: (self.page_index(item) or 0,
                                           natural_sort_key(item)))
            units.append(members)
        singles.sort(key=natural_sort_key)
        units.extend([name] for name in singles)
        return units

    def cover_rank(self, cover_rel: str, mtime: float, pixiv: bool) -> Tuple:
        """封面优先级键（可直接 max() 比较）。

        Pixiv 树按作品号大者优先（= 画师最近的作品），非 Pixiv 目录沿用
        「mtime 更新者优先」；Pixiv 条目整体优先于无号的普通条目。

        作品号一律取**封面文件名的前导数字**：落盘的是 `<pid>.jpg` / `<pid>_pN.jpg`，
        嵌在目录里的 `.../800_title/800_p0.png` 同样以作品号开头，所以看文件名既
        覆盖平铺也覆盖嵌套结构。
        """
        num = self.number(Path(cover_rel).name) if cover_rel else None
        if pixiv and num is not None:
            return (1, num, 0.0)
        return (0, 0.0, mtime)

    def sort(self, entries: List[Any], name_fn: Callable[[Any], str],
             reverse: bool, fuzzy: bool = False) -> List[Any]:
        """Pixiv 作品排序：前导数字条目按作品号排（方向生效），
        无前导数字条目按自然名排并始终位于最后。

        **方向只作用于作品号**，同一作品号内部另按两类定序：

        - 页序号（`page_index` 认得的 `800_p0` / `800_p1` / `800_p10`）：
          恒按页号升序 —— 倒序决定作品先后，不翻转作品内部的页序；
        - 其余条目：整名参与比较并跟随方向 —— 模糊匹配下同号只说明前导数字相同
          （`2024-05-10` 与 `2024-05-24 日富美` 是两个作品），要按整名倒序。

        `fuzzy`（模糊匹配）只改**同号/无号条目之间**的比较键：关掉时按文件名字符串
        自然序，打开时按整名的自然序（数字段比数字、其余段比文字），于是
        `2024-05-10` < `2024-05-24 日富美` 这类「先数字再文字」的条目也能排对。
        """
        numeric: List[Tuple] = []
        other: List[Tuple] = []
        for item in entries:
            name = name_fn(item)
            num = self.number(name)
            name_key = natural_sort_key(name if fuzzy else Path(name).name)
            if num is None:
                other.append((name_key, item))
            else:
                numeric.append((num, name_key, self.page_index(name), item))
        # 先按作品号定作品先后（方向生效），再逐个同号组定组内顺序
        numeric.sort(key=lambda t: t[0], reverse=reverse)
        ordered: List[Any] = []
        start = 0
        while start < len(numeric):
            end = start + 1
            while end < len(numeric) and numeric[end][0] == numeric[start][0]:
                end += 1
            ordered.extend(self._same_work_order(numeric[start:end], reverse))
            start = end
        other.sort(key=lambda t: t[0], reverse=reverse)
        return [item for *_head, item in ordered] + [item for _key, item in other]

    def _same_work_order(self, group: List[Tuple], reverse: bool) -> List[Tuple]:
        """同一个作品号内的条目顺序：页序号升序在前，其余条目按名字（方向生效）。"""
        pages = sorted((item for item in group if item[2] is not None), key=lambda t: t[2])
        named = sorted((item for item in group if item[2] is None),
                       key=lambda t: t[1], reverse=reverse)
        return pages + named

    def is_work_dir(self, dir_rel_path: str, entries: List[Any],
                    name_fn: Callable[[Any], str], has_cards: bool = False) -> bool:
        """目录自身是不是一个「作品」（里面是页序号，而不是作品列表）。

        Pixiv 树是两层：作者目录（名字不带作品号，里面每个条目是一个作品）→ 作品
        目录（名字就是作品号，里面是 `800_p0` / `800_p1` 这样的页）。判据取目录名的
        前导数字，再用里面有没有子目录、条目号是什么校正：

        - 纯图片目录（`has_cards` 为假）：目录名带作品号 → 这一层是页；整层没有
          作品号 → 普通图片相册，两种都按名字升序展示；只有「目录名无号、条目有号」
          才是平铺的单图作品列表（`pixiv/艺术家/<作品id>.jpg`）。
        - 有子目录：条目号全等于目录号才算「同一个作品的页」，其余都是作品列表 ——
          `2024 旅行`（里面有 800 / 900 两个作品）、`96neko`、`40原`（画师名恰好
          以数字开头，子目录是作品）因此仍按作品列表处理。
        """
        own = self.number(Path(dir_rel_path).name)
        nums = {num for num in (self.number(name_fn(e)) for e in entries) if num is not None}
        if not has_cards:
            return own is not None or not nums
        return own is not None and nums == {own}

    def image_sort_key(self, sort_by: str) -> Callable[[Dict], Any]:
        """图片排序键：time_name 下图片一律按文件名自然序（p0 → p1），

        时间维度只作用于作品/相册卡片之间的顶层排序；其余按设置。
        """
        if sort_by == 'time_name':
            return lambda i: natural_sort_key(Path(i['url']).name)
        if sort_by == 'mtime':
            return lambda i: i.get('mtime', 0.0)
        return lambda i: natural_sort_key(Path(i['url']).name)
