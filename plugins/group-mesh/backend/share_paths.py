"""共享根路径的纯函数：规范形式、嵌套归属、根目录指纹与用量占位。

为什么单独一个文件：`shares.py` 已经承担挂载 / 移除 / 用量 / 刷新声明四件事，单文件
行数贴到后端上限；这些函数不持有状态、不碰存储，拆出来由入口装配一次注入给组件。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional


class SharePaths:
    """共享根路径的纯函数集合（无状态，入口装配一次注入）。"""

    @staticmethod
    def norm(path: str) -> str:
        """路径比较用的规范形式：绝对化 + 大小写折叠（Windows 上两者等价）。"""
        return os.path.normcase(os.path.abspath(path)) if path else ''

    @staticmethod
    def prefix(root_path: str) -> str:
        """根路径的"目录前缀"形式：保证以分隔符结尾，盘符根目录本身也适用。"""
        return root_path if root_path.endswith(os.sep) else root_path + os.sep

    def share_of(self, path: str, paths: Dict[str, str]) -> Optional[str]:
        """绝对路径落在哪个共享根下：嵌套共享时归最内层（最长匹配）。"""
        target = self.norm(path)
        if not target:
            return None
        best: Optional[str] = None
        best_len = -1
        for share_id, root in paths.items():
            root_path = self.norm(root)
            if not root_path:
                continue
            if target != root_path and not target.startswith(self.prefix(root_path)):
                continue
            if len(root_path) > best_len:
                best, best_len = share_id, len(root_path)
        return best

    @staticmethod
    def missing_usage() -> Dict[str, Any]:
        """用量还没算过（或共享根不可用）时的占位值。"""
        return {'used_bytes': None, 'entries': None, 'truncated': False,
                'usage_at': None, 'usage_stale': True}

    @staticmethod
    def root_stamp(path: Path) -> Optional[List[int]]:
        """共享根目录的廉价指纹：目录 mtime 与大小。

        只 stat 根目录本身（一次系统调用），不遍历：首屏只读缓存时用它判断"用量还是
        按这个目录算的吗"。目录里直接子项增删会改 mtime，因此这一类变化立刻可见；
        更深层的变化由被动同步的目录事件与校验负责。
        """
        try:
            st = path.stat()
        except OSError:
            return None
        return [int(st.st_mtime_ns), int(st.st_size)]
