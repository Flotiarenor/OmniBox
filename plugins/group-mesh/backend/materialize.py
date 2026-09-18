"""远端物化（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh import client as mesh_client
from shell.groupmesh.transport import RemoteError, TransportError

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts
_mtime_ns = _common._mtime_ns

class MaterializeMixin:
    """把对端共享项在本地物化成目录树 + 索引（字节按需取），含远端遍历与对账删除。"""

    # ── 远端：物化与按需取字节 ────────────────────────────────────────────
    # 「物化」= 在本地 `.cache/remote/<设备ID>/<共享标识>/…` 造出与远端同形的目录树：
    # 目录真的建出来、文件先放 0 字节占位，真实大小记在 <共享标识>.index.json 里；
    # 字节在**第一次真去读这个文件**时才取回（由 `ensure_file` 钩子驱动）。
    #
    # 为什么必须这样分两步：消费方插件（image-viewer / media-player 等）是按
    # "本地路径"工作的 —— 它们 `os.scandir` 建索引、按路径取字节。远端目录如果
    # 不落地，它们连"有这个文件"都看不到；而如果订阅时就把字节全拉下来，看一个
    # 共享相册要先下几百张图。

    def _remote_index_file(self, device_id: str, share_id: str) -> Path:
        return self.remote_cache_dir / device_id / f'{share_id}.index.json'

    def _load_remote_index(self, device_id: str, share_id: str) -> Dict[str, Any]:
        path = self._remote_index_file(device_id, share_id)
        if not path.is_file():
            return {'share': share_id, 'device': device_id, 'entries': {}}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'share': share_id, 'device': device_id, 'entries': {}}
        if not isinstance(data.get('entries'), dict):
            data['entries'] = {}
        return data

    def _cached_remote_index(self, device_id: str, share_id: str) -> Dict[str, Any]:
        """带缓存的索引读取。

        `is_content_placeholder()` 会在**每个** `/file` 请求上被调用，而索引文件
        可能不小（几千条就是几百 KB），逐次解析会让每个文件请求都多一次磁盘 I/O +
        JSON 解析。缓存的失效点有三处：物化（重写索引）、取字节成功、清理缓存 ——
        所以只在这三处清空，不做 TTL。
        """
        key = (device_id, share_id)
        cached = self._remote_index_cache.get(key)
        if cached is not None:
            return cached
        index = self._load_remote_index(device_id, share_id)
        self._remote_index_cache[key] = index
        # 简单的上界，免得浏览很多共享项后缓存无限增长
        if len(self._remote_index_cache) > 32:
            self._remote_index_cache.pop(next(iter(self._remote_index_cache)))
        return index

    def _invalidate_remote_index(self, device_id: str = '', share_id: str = '') -> None:
        if not device_id:
            self._remote_index_cache.clear()
            return
        if not share_id:
            for key in [k for k in self._remote_index_cache if k[0] == device_id]:
                del self._remote_index_cache[key]
            return
        self._remote_index_cache.pop((device_id, share_id), None)

    def _save_remote_index(self, device_id: str, share_id: str, index: Dict[str, Any]) -> None:
        path = self._remote_index_file(device_id, share_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def materialize_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                           path: str = '.', max_depth: int = 4,
                           max_entries: int = 5000) -> Dict[str, Any]:
        """把对端共享项在本地物化成目录树（目录 + 0 字节占位文件 + 索引）。

        限制深度与条目数是刻意的：远端可能是一个几十万文件的目录，全量遍历既慢又会
        把内存和索引文件撑爆。默认 4 层 / 5000 条，调用方可传更大的值。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        try:
            max_depth = int(options.get('max_depth', max_depth))
            max_entries = int(options.get('max_entries', max_entries))
        except (TypeError, ValueError):
            return {'success': False, 'error': 'max_depth / max_entries 必须是整数'}
        if not share_id:
            return {'success': False, 'error': '必须给出 share_id'}

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}
        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False, 'error': '还没有可用的对端地址（见"添加对端"）'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            peer_id = connection.peer.device_key.hex()
            # 物化目录必须按**真实设备 ID**建：`ensure_file()` 之后要从路径反解出
            # 设备 ID 去取字节，若这里写成调用方传入的（可能为空）就会落到别的目录，
            # 取字节时找不到对应共享项。
            root = self.remote_cache_dir / peer_id / share_id
            index = self._load_remote_index(peer_id, share_id)
            previous: Dict[str, Any] = dict(index.get('entries') or {})
            try:
                walked = self._walk_remote(connection, share_id, path, max_depth, max_entries)
            except RemoteError as e:
                return {'success': False, 'error': str(e), 'device_id': peer_id,
                        'share_id': share_id, 'path': path}
            except (TransportError, OSError) as e:
                last_error = f'遍历中断: {e}'
                continue
            finally:
                self._release(connection)

            # 落盘：目录建出来，文件写 0 字节占位。
            # 占位而不是"什么都不建"：`os.scandir` 能看到条目、消费方插件的索引因此
            # 完整；`ensure_file` 用"实际大小 != 索引里的真实大小"判定哪些还没取回。
            created_dirs = 0
            created_files = 0
            invalidated = 0
            entries: Dict[str, Any] = {}
            for rel, info in walked.items():
                target = root / rel
                if info['dir']:
                    target.mkdir(parents=True, exist_ok=True)
                    created_dirs += 1
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    old = previous.get(rel)
                    # 对端的内容换过了（大小或 mtime 变了）：本机若已经把**真字节**
                    # 取回来了，必须丢掉它 —— 否则"同大小的替换"会永远读到旧内容，
                    # 而缩略图/列表缓存又都以本地 (size, mtime) 为键，看不出任何异常。
                    # 丢掉之后它重新变回占位文件，下次读取按需取回。
                    if old is not None and self._content_changed(old, info) \
                            and self._is_fetched(target):
                        target.unlink()
                        invalidated += 1
                    if not target.is_file():
                        target.touch()
                        created_files += 1
                    # 占位与已取回的文件都写成对端的 mtime：消费方的缓存失效读的是
                    # **本地**元数据，留着"物化时刻"的时间会让"对端换了图"看起来没变。
                    self._apply_remote_mtime(target, info.get('mtime_ns'))
                entries[rel] = {'dir': info['dir'], 'size': info['size'],
                                'mtime_ns': info.get('mtime_ns')}

            truncated = len(walked) >= max_entries
            # 对账（对端已删/改名 → 本地也删）**只在遍历完整时做**：达到条目上限时
            # `walked` 只是一个子集，照着它删会把没遍历到的内容整片抹掉。
            removed = 0 if truncated else self._prune_absent(root, previous, entries)

            index.update({'share': share_id, 'device': peer_id, 'entries': entries,
                          'root': str(root),
                          'materialized_at': int(time.time()),
                          'truncated': truncated})
            self._save_remote_index(peer_id, share_id, index)
            self._invalidate_remote_index(peer_id, share_id)
            return {'success': True, 'device_id': peer_id, 'name': label,
                    'share_id': share_id, 'root': str(root),
                    'dirs': created_dirs, 'files': created_files,
                    'invalidated': invalidated, 'removed': removed,
                    'entries': len(entries), 'truncated': truncated}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

    @staticmethod
    def _content_changed(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        """对端这一条的内容是否变了（大小或 mtime 任一不同）。

        对端没给 mtime（老内核）时只看大小：这正是加入 mtime 之前的能力，
        不假装能发现同大小的替换。
        """
        if int(old.get('size') or 0) != int(new.get('size') or 0):
            return True
        old_mtime, new_mtime = old.get('mtime_ns'), new.get('mtime_ns')
        if old_mtime is None or new_mtime is None:
            return False
        return int(old_mtime) != int(new_mtime)

    @staticmethod
    def _is_fetched(target: Path) -> bool:
        """本机这个物化条目是否已经持有**真字节**（而不是 0 字节占位）。

        判据只有"大小非 0"：0 字节的远端文件本来就永远停在占位状态（`ensure_file`
        对 remote_size==0 直接返回，见 `.dsh/group-mesh-materialize.md` §2.2），
        对它做"丢弃重取"没有任何意义。
        """
        try:
            return target.is_file() and target.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _apply_remote_mtime(target: Path, mtime_ns: Optional[int]) -> None:
        """把对端的 mtime 写到本地这份文件上（占位或已取回的真字节都要写）。

        写不进去不是错误：老对端没有该字段、文件系统可能不支持、值可能越界。
        这里**不**回读比对 —— 全仓的远端判定只比较"对端报过的值"与"对端现在报的值"，
        从不拿本地 stat 去比远端值（本地文件系统会量化，比了只会永远不相等）。
        """
        if mtime_ns is None:
            return
        try:
            os.utime(target, ns=(mtime_ns, mtime_ns))
        except (OSError, OverflowError, ValueError) as e:
            log.info(f'[group-mesh] 无法把对端 mtime 写到 {target}: {e}')

    def _prune_absent(self, root: Path, previous: Dict[str, Any],
                      keep: Dict[str, Any]) -> int:
        """删掉"索引里有、本次遍历里没有"的条目，返回删掉的文件数。

        改动前这里只增不减：对端删掉或改名之后，本地那份会永远留着（界面上表现为
        "对方已经删了的文件我还看得到"）。目录按深度倒序处理，先删文件再删因此变空的
        目录；非空目录不删（里面还可能有本次没遍历到的内容）。
        """
        removed = 0
        gone = sorted(set(previous) - set(keep), key=lambda rel: rel.count('/'), reverse=True)
        for rel in gone:
            target = root / rel
            try:
                if target.is_dir():
                    target.rmdir()
                elif target.exists():
                    target.unlink()
                    removed += 1
            except OSError:
                # 目录非空、或已被并发的取字节线程删掉：都不是错误
                continue
        return removed

    def _walk_remote(self, connection: Any, share_id: str, path: str,
                     max_depth: int, max_entries: int) -> Dict[str, Dict[str, Any]]:
        """广度优先遍历远端目录，返回 `{相对路径: {dir, size, mtime_ns}}`（不含根自身）。

        `mtime_ns` 是对端 `stat` 的真实值（纳秒整数），也是本机判定"对端换过没有"
        的**唯一依据**：本机自己那份 mtime 会被文件系统量化，拿它去比远端值只会
        得出"永远不相等"。老对端不带该字段时为 None。
        """
        import collections

        found: Dict[str, Dict[str, Any]] = {}
        pending = collections.deque([(path or '.', 0)])
        while pending:
            current, depth = pending.popleft()
            result = mesh_client.list_directory(connection, share_id, current)
            if not result.get('dir'):
                # 调用方给的是个文件路径：直接当作单个条目
                name = str(result.get('path') or current).lstrip('./')
                found[name] = {'dir': False, 'size': int(result.get('size') or 0),
                               'mtime_ns': _mtime_ns(result)}
                continue
            for entry in result.get('entries') or []:
                name = str(entry.get('name') or '')
                if not name or name in ('.', '..'):
                    continue
                rel = name if current in ('.', '') else f"{current.strip('/')}/{name}"
                if rel in found:
                    continue
                if len(found) >= max_entries:
                    return found
                found[rel] = {'dir': bool(entry.get('dir')),
                              'size': int(entry.get('size') or 0),
                              'mtime_ns': _mtime_ns(entry)}
                if entry.get('dir') and depth + 1 < max_depth:
                    pending.append((rel, depth + 1))
        return found
