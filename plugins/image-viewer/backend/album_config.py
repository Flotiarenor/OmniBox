"""相册的可见性配置（收纳 / 展开 / 提升 / 保留可见的空目录）。

状态自持：配置字典与它的落盘。这个文件回答的是"前端该显示哪些相册节点"，
与相册索引（有多少图片、封面是什么）是两件事，但两者互相影响 —— 所以写入之后
由本组件调入口给的 `on_changed` 让索引重来，而不是让调用方各自记得。

「保留可见的空目录」这条规则值得单独说明：新建的空目录、以及只含空子目录的目录
默认不显示（否则相册树会被一堆空壳撑满）。新建时把目录及其各级父目录记进
`visible_empty_dirs`，等它进了图片或目录被删掉，标记由 `prune()` 清理 —— 否则
配置文件会随着用户来回建删无限增长。
"""

import logging
from typing import Any, Callable, Dict, List

from shell.backend.json_cache import read_json, write_json

log = logging.getLogger(__name__)

#: 配置文件的字段默认值。`collapsed` / `promoted` / `expanded` 是三个前端可见性白名单，
#: `visible_empty_dirs` 是"保留可见"标记。
_DEFAULTS: Dict[str, List[str]] = {
    'collapsed': [], 'promoted': [], 'expanded': [], 'visible_empty_dirs': [],
}


class AlbumConfig:
    """相册树的可见性配置与它的落盘。"""

    def __init__(self, paths: Any, on_changed: Callable[[], None]) -> None:
        self._paths = paths
        self._on_changed = on_changed
        self._data: Dict[str, Any] = self._load()

    # ===== 落盘 =====

    def _load(self) -> Dict[str, Any]:
        data = read_json(self._paths.album_config_file, default=None)
        if isinstance(data, dict):
            return {**_DEFAULTS, **data}
        return {**{key: list(value) for key, value in _DEFAULTS.items()}}

    def save(self) -> None:
        """原子落盘；父目录不存在时按 `json_cache` 的约定返回 False（静默跳过）。"""
        if not write_json(self._paths.album_config_file, self._data, indent=2,
                          create_parent=True):
            log.error('[ImageViewer] 保存相册配置失败: %s', self._paths.album_config_file)

    @property
    def data(self) -> Dict[str, Any]:
        """当前配置（只读用途：`list_albums` 的返回值与 API 的 `get_album_config`）。"""
        return self._data

    def get(self) -> Dict[str, Any]:
        """API：读取相册可见性配置。"""
        return self._data

    def _write(self, **changes: Any) -> None:
        self._data = {**self._data, **changes}
        self.save()

    # ===== 可见性动作 =====

    def set_action(self, rel_path: str, action: str) -> Dict:
        """album 层级控制：collapse/expand（收纳/展开子相册）、promote/unpromote（提升到全部相册）。

        子相册**默认折叠**，所以「展开」要落进 `expanded` 白名单；「收纳」则撤销展开并
        记进 `collapsed`，覆盖历史配置。
        """
        rel_path = (rel_path or '').strip().strip('/')
        collapsed = set(self._data.get('collapsed', []))
        promoted = set(self._data.get('promoted', []))
        expanded = set(self._data.get('expanded', []))
        if action == 'collapse':
            collapsed.add(rel_path)
            expanded.discard(rel_path)
        elif action == 'expand':
            collapsed.discard(rel_path)
            expanded.add(rel_path)
        elif action == 'promote':
            promoted.add(rel_path)
        elif action == 'unpromote':
            promoted.discard(rel_path)
        else:
            return {'success': False, 'error': f'未知操作: {action}'}
        self._data = {
            **self._data,
            'collapsed': sorted(collapsed),
            'promoted': sorted(promoted),
            'expanded': sorted(expanded),
        }
        self.save()
        self._on_changed()
        return {'success': True, 'config': self._data}

    # ===== 「保留可见」标记 =====

    def visible_marks(self) -> List[str]:
        """当前的「保留可见」标记。"""
        marks = self._data.get('visible_empty_dirs') or []
        return [str(mark) for mark in marks]

    def mark_visible(self, rel_path: str) -> None:
        """把新建的空目录加入「保留可见」标记（同时保留其上级，便于逐层进入）。"""
        current = set(self.visible_marks())
        parts = [part for part in (rel_path or '').split('/') if part]
        for index in range(1, len(parts) + 1):
            current.add('/'.join(parts[:index]))
        self._write(visible_empty_dirs=sorted(current))

    def replace_visible_marks(self, marks: List[str]) -> Dict:
        """覆盖式写入「保留可见」标记（设置页/清理时用）。"""
        cleaned = sorted({str(mark).strip('/') for mark in (marks or []) if str(mark).strip('/')})
        self._write(visible_empty_dirs=cleaned)
        self._on_changed()
        return {'success': True, 'config': self._data}

    def drop_marks_under(self, rel_path: str) -> None:
        """删掉某目录及其下级的「保留可见」标记（目录被删除时调用）。"""
        kept = [path for path in self.visible_marks()
                if path != rel_path and not path.startswith(f'{rel_path}/')]
        self._write(visible_empty_dirs=kept)

    def prune(self) -> bool:
        """清理已消失的「保留可见」标记，避免配置文件无限增长。

        返回是否真的做了清理：调用方据此决定是否丢弃相册索引缓存（标记影响的是前端
        的空目录过滤，与封面聚合无关，但仍让索引重来更简单）。
        """
        marks = self.visible_marks()
        if not marks:
            return False
        kept = [path for path in marks if self._paths.virtual_dir_exists(path)]
        if kept == list(marks):
            return False
        self._write(visible_empty_dirs=kept)
        return True
