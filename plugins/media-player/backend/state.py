"""用户数据（歌单 / 喜欢 / 最近播放 / 播放进度）的落点与原子写入。

状态自持：状态字典、读写锁、状态文件路径、旧版迁移标记都在这里。

原子写的机制**只有一份**，在 `shell.backend.json_cache.write_json`（同目录临时文件 +
`flush` + `fsync` + `os.replace`）：本模块不再自己拼临时文件，避免同一台机器上两种
"原子写"各自漂移。

为什么还要串行化写入：前端一次换曲会在同一个 tick 里连发 `media_save_playback` 与
`media_update_recent`，桥调用由 pywebview 各起一条线程执行。两个写者共用一个固定临时
文件名时，一个还在 `fsync`、另一个已经 `os.replace`，Windows 就报 WinError 32
（"另一个程序正在使用此文件"）并把这一次写入丢掉 —— 表现为每次换曲偶发一条"保存状态
失败"。共用一把锁 + `mkstemp` 生成的一次性临时名解决并发写者；另外对"改名撞上外部
实时扫描/索引的瞬时占用"重试几次，那一条不能靠临时名规避。
"""

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from shell.backend.json_cache import write_json

log = logging.getLogger(__name__)

#: 「最近播放」保留条数（状态文件按写入顺序截断）。
RECENT_LIMIT = 50


def _defaults() -> Dict[str, Any]:
    """状态文件的初始形状（缺键按它补齐，读到的未知键原样保留）。

    每次返回**新的**容器：模块级常量里的 list/dict 会被 `playlist_save` 之类的操作
    就地修改，共享一份会把上一个实例的歌单带进下一个（用例里立刻能看出来）。
    """
    return {"favorites": [], "recent": [], "playlists": [], "playback": {}}


class UserStateStore:
    """`media_state.json` 的读取与原子写入（用户可见集合的语义在 collections.py）。"""

    #: 整次写调用的重试次数与单次等待。只针对"目标文件被外部实时扫描/索引短暂占用"
    #: 这类瞬时失败（WinError 5 / 32）：等待总计 ≈300ms，代价远低于"用户这次改动没落盘"。
    replace_attempts = 3
    replace_delay = 0.05
    #: 「最近播放」保留条数（集合操作按它截断，见 collections.MediaCollections）。
    recent_limit = RECENT_LIMIT

    def __init__(self, paths: Any, index: Any,
                 item_id: Callable[[str], str]) -> None:
        self._paths = paths
        self._index = index
        # 旧版相对路径 id → 新 id 的换算由入口注入（与索引用的是同一个算法，
        # 不在本组件里再抄一份）
        self._item_id = item_id
        self._lock = threading.RLock()
        self._legacy_migrated = False
        self.data: Dict[str, Any] = _defaults()

    # ===== 落点与读写 =====

    @property
    def state_file(self) -> Path:
        return self._paths.state_file

    def load(self) -> None:
        """读状态文件。

        与 `save()` 共用一把锁：Windows 上 `os.replace` 要求目标文件的所有打开句柄都
        允许删除，读者在改名那一瞬间开着它，写者就会拿到 WinError 32。备份损坏文件
        也在锁内做（`shutil.copy2` 同样要打开它）。
        """
        with self._lock:
            if self.state_file.exists():
                try:
                    with open(self.state_file, 'r', encoding='utf-8') as handle:
                        self.data = {**_defaults(), **json.load(handle)}
                        return
                except Exception as error:
                    # 解析失败**先备份**再退默认值：旧实现直接返回默认值，而下一次
                    # `save()`（播放中每 2 秒一次）会把默认值写回去 —— 一次瞬时读取
                    # 失败（例如读到写入尚未完成的截断文件）就等于永久丢数据。
                    self._backup_broken(error)
            self.data = _defaults()

    def save(self) -> None:
        """原子写用户状态：一把锁串行化 + 复用 `json_cache` 的写入口。

        重试是**整次写调用**重来，而不是只重试改名：`write_json` 面向"随时可以重算的
        缓存"，把底层异常吞成 False（缓存损失的后果只是重算，用户数据不是）。因此在
        这里按返回值重试，并在全部失败后补一条带文件路径的错误日志。
        """
        with self._lock:
            for attempt in range(self.replace_attempts + 1):
                if write_json(self.state_file, self.data, indent=2, create_parent=True):
                    return
                if attempt < self.replace_attempts:
                    time.sleep(self.replace_delay * (attempt + 1))
            log.error(f"[MediaPlayer] 保存状态失败: {self.state_file}"
                      f"（重试 {self.replace_attempts} 次仍失败）")

    def _backup_broken(self, error: Exception) -> None:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup = self.state_file.with_name(f'media_state.corrupt-{stamp}.json')
        try:
            shutil.copy2(self.state_file, backup)
            log.error(f'[MediaPlayer] 播放记录解析失败（{error}），已备份到 {backup}')
        except OSError as copy_error:
            log.error(f'[MediaPlayer] 播放记录解析失败（{error}），备份也失败: {copy_error}')

    # ===== 旧落点收养与旧插件迁移 =====

    def adopt_orphan(self) -> None:
        """新落点还没有状态文件时，从各媒体根的旧位置挑最完整的一份收养。

        评分只数**用户自己攒出来的东西**（歌单 + 喜欢）：`recent` 谁都是满的（上限
        50 条），拿它当权重会把"刚换过根、什么都没有"的那份也算得一样高。
        复制而不是移动：旧文件留在原地可回溯，收养来的这份此后是唯一写入方。
        """
        try:
            if self.state_file.exists():
                return
        except OSError:
            return
        best: Optional[Path] = None
        best_score = 0
        for root in list(self._paths.scan_roots):
            candidate = Path(root) / '.cache' / 'media_state.json'
            try:
                if not candidate.exists():
                    continue
                data = json.loads(candidate.read_text(encoding='utf-8'))
            except Exception:
                continue
            score = len(data.get('playlists') or []) + len(data.get('favorites') or [])
            if score > best_score:
                best, best_score = candidate, score
        if best is None or best_score <= 0:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(best, self.state_file)
            log.info(f'[MediaPlayer] 已收养旧的播放记录 {best} → {self.state_file}'
                     f'（歌单+喜欢 {best_score} 条）')
        except OSError as error:
            log.error(f'[MediaPlayer] 收养旧播放记录失败 {best}: {error}')

    def migrate_legacy(self) -> None:
        """把旧 music-player 的收藏 / 最近播放 / 歌单 / 播放状态迁入统一状态文件。

        旧插件使用「相对路径」作为歌曲 id，新插件使用绝对路径的 md5 摘要，这里在索引
        可用时做一次映射，成功后把旧文件改名保留。
        """
        if self._legacy_migrated:
            return
        self._legacy_migrated = True
        legacy_file = self._paths.legacy_state_file
        if not legacy_file.exists():
            return
        try:
            legacy = json.loads(legacy_file.read_text(encoding='utf-8'))
        except Exception:
            return
        if not isinstance(legacy, dict):
            return

        if not self._index.items:
            # 索引尚未建立：保留旧文件，等待首次扫描后再迁移
            self._legacy_migrated = False
            return

        mapping: Dict[str, str] = {}
        raw_ids = set()
        raw_ids.update(str(x) for x in legacy.get('favorites', []) if x)
        raw_ids.update(str(x.get('id', '')) for x in legacy.get('recent', []) if x.get('id'))
        raw_ids.update(str(x) for pl in legacy.get('playlists', []) for x in pl.get('song_ids', []))
        if legacy.get('playback', {}).get('song_id'):
            raw_ids.add(str(legacy['playback']['song_id']))

        for raw_id in raw_ids:
            try:
                legacy_path = Path(raw_id)
                if not legacy_path.is_absolute():
                    legacy_path = self._paths.cache_dir.parent / legacy_path
                new_id = self._item_id(str(legacy_path.resolve()))
                if new_id in self._index.items:
                    mapping[raw_id] = new_id
            except Exception:
                pass

        favorites = [mapping[x] for x in legacy.get('favorites', []) if str(x) in mapping]
        playlists = []
        for pl in legacy.get('playlists', []):
            item_ids = [mapping[x] for x in pl.get('song_ids', []) if str(x) in mapping]
            playlists.append({
                'id': str(pl.get('id') or uuid.uuid4().hex[:12]),
                'name': pl.get('name') or '迁移歌单',
                'item_ids': item_ids,
                'created_at': pl.get('created_at', ''),
                'updated_at': pl.get('updated_at', ''),
            })
        playback: Dict[str, Any] = {}
        old_pb = legacy.get('playback', {})
        old_song = old_pb.get('song_id')
        if old_song and str(old_song) in mapping:
            playback = {
                'item_id': mapping[str(old_song)],
                'loop_mode': old_pb.get('loop_mode', 'none'),
                'shuffle': bool(old_pb.get('shuffle', False)),
                'volume': old_pb.get('volume', 1.0),
            }

        if favorites:
            self.data['favorites'] = list(dict.fromkeys(self.data.get('favorites', []) + favorites))

        legacy_recent = [
            {'id': mapping[entry.get('id')], 'played_at': entry.get('played_at', '')}
            for entry in legacy.get('recent', []) if str(entry.get('id', '')) in mapping
        ]
        if legacy_recent:
            merged_recent = {entry.get('id'): entry for entry in legacy_recent}
            for entry in self.data.get('recent', []):
                merged_recent.setdefault(entry.get('id'), entry)
            self.data['recent'] = sorted(
                merged_recent.values(),
                key=lambda entry: entry.get('played_at', ''),
                reverse=True,
            )[:RECENT_LIMIT]

        if playlists and not self.data.get('playlists'):
            self.data['playlists'] = playlists
        if playback and not self.data.get('playback'):
            self.data['playback'] = playback
        self.save()

        try:
            legacy_file.replace(legacy_file.with_name('music_state.json.migrated'))
            log.info(f"[MediaPlayer] 已迁移旧 music-player 状态 → {self.state_file.name}")
        except Exception:
            pass
