"""宿主适配：image-viewer 的根目录、虚拟路径前缀与文件服务能力。

状态自持：宿主实例由入口解析并缓存，这里只拿一个"问宿主在哪"的闭包；扫描根（物理根 +
虚拟前缀）每次按宿主的根清单现算 —— 命名空间 token 由宿主生成，写死或长期缓存都会与宿主
的实际解析结果脱节（指纹库的键按虚拟路径算，对不上就整体错位）。

`list_roots()` 的两种兜底判据只与宿主版本有关：旧版宿主没有这个方法，或提供了但条目里
没有 `prefix` 字段。后一种**不能**把"前缀缺失"当成空串 —— 那会把额外根里的文件按主根
解释，除了缩略图 404，`delete_files()` 还会删到主根下的另一个同名文件。两种兜底都退回
单根，与加额外图库支持之前的行为完全一致。取根清单的动作留在入口：它是按名字从宿主实例
上取成员，属于宿主的兼容契约，放在入口才能被 `tools/check_plugins.py` 的契约检查覆盖。
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast


class HostBridge:
    """把宿主实例的能力收敛成组件看得到的几个动词。"""

    def __init__(self, provider: Callable[[], Any],
                 root_lister: Callable[[], Optional[List[Dict[str, Any]]]]) -> None:
        self._provider = provider
        # 返回 None 表示宿主没有 `list_roots()`（旧版本），调用方按单根处理。
        self._root_lister = root_lister

    def host(self) -> Any:
        return self._provider()

    def name(self) -> str:
        return str(getattr(self.host(), 'name', ''))

    def data_root(self) -> Path:
        return cast(Path, self.host().get_data_root())

    def file_roots(self) -> List[Path]:
        return cast(List[Path], self.host().get_file_roots())

    def ensure_thumb(self, rel_path: str) -> None:
        """复用宿主的缩略图入口；基类契约是"无返回值"，壳也不消费它。"""
        self.host().ensure_thumb(rel_path)

    def thumb_data(self, rel_path: str):
        return self.host().get_thumb_data(rel_path)

    def roots(self) -> List[Tuple[Path, str]]:
        """扫描范围：宿主的全部根目录 + 各自的虚拟路径前缀（主目录在前）。

        结果是 `(物理根目录, 前缀)`：前缀取自宿主的 `list_roots()`（第一根为空串，额外根是
        `__<命名空间>`），扫描出的相对路径必须带上它，否则缩略图 / 原图 / 删除三条路由都会
        按**主目录**去解释，落到不存在的路径上。
        """
        listings = self._root_lister()
        if listings is None:
            return [(self.data_root(), '')]
        roots: List[Tuple[Path, str]] = []
        for index, item in enumerate(listings or []):
            item = item or {}
            path = str(item.get('path') or '')
            if not path:
                continue
            prefix = str(item.get('prefix') or '')
            if index and not prefix:
                return [(self.data_root(), '')]
            roots.append((Path(path), prefix))
        return roots or [(self.data_root(), '')]

    def abs_for_rel(self, rel: str) -> Optional[str]:
        """虚拟相对路径 → 绝对路径（指纹缓存的键是绝对路径的摘要）。

        命名空间前缀先匹配（额外根），再回落到第一根 —— 顺序反了会把
        `__额外图库/a.jpg` 当成主根下的同名目录。
        """
        rel = str(rel or '').strip('/')
        if not rel:
            return None
        roots = self.roots()
        for root, prefix in roots:
            if prefix and (rel == prefix or rel.startswith(prefix + '/')):
                inner = rel[len(prefix):].lstrip('/')
                return str(root / inner) if inner else None
        for root, _prefix in roots:
            return str(root / rel)
        return None
