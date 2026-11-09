"""媒体根与派生路径：扫描根、索引 / 封面库 / 用户状态文件的落点。

状态自持：扫描根与全部派生路径都在这里。换根走 `rebind()`，由入口编排；入口不再随手
改自己身上的缓存路径（那正是"某个分片顺手改另一个分片状态"的来源）。

两类落点刻意分开：

- **索引与封面库跟着媒体根**（`cache_dir` 下的 `media_index.json` / `thumbs.db`）：
  它们描述的就是那些文件，换根就该重建；
- **用户数据（歌单 / 喜欢 / 播放进度）落在壳数据根下**（`state_file`）：用户换一个媒体
  文件夹不该把整份播放记录换到别处，详见 `_state_dir()`。
"""

import logging
from pathlib import Path
from typing import Callable, List, Optional

from shell.backend.ports import SettingsPort

log = logging.getLogger(__name__)


class PathLayout:
    """扫描根与派生路径的唯一来源。"""

    def __init__(self, settings: SettingsPort, default_root: Path,
                 shell_data_root: Callable[[], Path]) -> None:
        self._settings = settings
        self._default_root = default_root
        self._shell_data_root = shell_data_root
        self.scan_roots: List[Path] = [default_root]
        self.root_dir: Path = default_root
        self.cache_dir: Path = default_root / '.cache'
        self.cache_file: Path = self.cache_dir / 'media_index.json'
        self.thumb_db: Path = self.cache_dir / 'thumbs.db'
        self.task_file: Path = self.cache_dir / 'scan_task.json'
        self.legacy_state_file: Path = self.cache_dir / 'music_state.json'
        self.state_file: Path = self._state_dir() / 'media_state.json'
        self.rebind()

    # ===== 换根 =====

    def rebind(self) -> None:
        """按当前设置重算扫描根与全部派生路径（入口在 `on_settings_changed` 里调用）。"""
        self.scan_roots = self._configured_roots() or [self._default_root]
        self.root_dir = self.scan_roots[0]
        self.cache_dir = self.root_dir / '.cache'
        self.cache_file = self.cache_dir / 'media_index.json'
        self.thumb_db = self.cache_dir / 'thumbs.db'
        self.task_file = self.cache_dir / 'scan_task.json'
        self.legacy_state_file = self.cache_dir / 'music_state.json'
        self.state_file = self._state_dir() / 'media_state.json'
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _configured_roots(self) -> List[Path]:
        """把「媒体文件夹」列表解析成有序根目录。

        第一行是主目录（数据根 + 扫描根），其余是额外扫描根。
        兼容旧配置：早期是 `root_dir` + `media_dirs`（每行一个）两个字段，
        用户没重新保存过设置时按旧字段原样读出来，界面上合并成同一个列表。
        """
        entries = self._settings.setting('media_roots')
        if not str(entries or '').strip():
            legacy = [self._settings.setting('root_dir') or '']
            legacy += str(self._settings.setting('media_dirs') or '').splitlines()
            entries = '\n'.join(legacy)
        roots: List[Path] = []
        for line in str(entries or '').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                path = Path(line).expanduser().resolve()
            except Exception:
                continue
            if path not in roots:
                roots.append(path)
        return roots

    def _state_dir(self) -> Path:
        """用户数据的目录：**壳数据根**下，而不是媒体文件夹里。

        背景：`media_state.json` 原先落在「媒体文件夹」第一行的 `.cache` 下，而那一行
        同时是数据根。用户在设置里换一个文件夹（或调整顺序）就等于把整个状态文件换到
        别处：插件在**新位置写一份空状态**，旧的那份留在原地再也没人读 —— 用户视角
        是"我的歌单被删了"。实际发生过一次：旧媒体根下的 `media_state.json` 里整份
        播放记录（几十个歌单、上百个喜欢）完好无损，而插件已经在新媒体根下用一份空
        状态跑了一整天。因此用户数据的落点改到壳数据根下（与指纹库同级），媒体文件夹
        怎么改都不再影响它；老位置的旧文件由 `UserStateStore.adopt_orphan()` 收养回来。
        """
        try:
            base = Path(self._shell_data_root())
        except (KeyError, TypeError, OSError):
            # 壳没给 `directories.data_root`（脚本/用例直接构造插件）时退回媒体根，
            # 保证插件仍可用，只是失去"换文件夹不丢用户数据"的保护
            base = self.cache_dir.parent
        return base / '.cache' / 'media-player'

    # ===== 根 =====

    def file_roots(self) -> List[Path]:
        """跨主目录与额外媒体目录提供文件访问（壳的 `/file` 允许根）。"""
        return list(self.scan_roots) or [self.root_dir]

    def root_for(self, path: Path) -> Path:
        """该文件属于哪个扫描根（多根时按最长前缀匹配；找不到时退回第一根）。"""
        best: Optional[Path] = None
        for root in self.scan_roots:
            try:
                if path.is_relative_to(root) and (best is None or len(str(root)) > len(str(best))):
                    best = root
            except (OSError, ValueError):
                continue
        return best or (self.scan_roots[0] if self.scan_roots else path.parent)

    def namespace_for(self, path: Path) -> str:
        """该文件的命名空间前缀（= 所属扫描根的目录名，专辑键的第一段）。"""
        root = self.root_for(path)
        return root.name or str(root)

    def rel_dir_for(self, path: Path) -> str:
        """该文件在所属扫描根内的相对目录（posix 形式，根内直接下级为空串）。"""
        root = self.root_for(path)
        try:
            rel = path.parent.relative_to(root).as_posix()
        except ValueError:
            return ''
        return '' if rel == '.' else rel

    # ===== 旧版残留 =====

    def drop_stale_scan_task(self) -> None:
        """旧版任务文件的清理钩子（已废弃）。

        根级断点（`scan_task.json` + `completed_roots`）由统一刷新基建的目录指纹取代，
        残留的任务文件不再有读取方，留着只会让"上次扫描被中断"这类状态永远挂在界面上。
        只在启动时清一次，不参与换根。
        """
        try:
            if self.task_file.exists():
                self.task_file.unlink()
                log.info('[MediaPlayer] 已清理旧版扫描断点文件 scan_task.json')
        except OSError:
            pass
