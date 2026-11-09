"""团体名单的历史（自动分发的准入凭据）。

## 为什么需要它

名单分发的准入判据是"对端手里的那份，本机**认得**"（见 `Node._peer_knows_history`）。
`content_hash` 无法伪造，因此"本机认得这一份"才是真凭据 —— 而"认得"必须有地方
记着：本机当前名单只说明"我见过这一份"，说明不了"我见过它前面的那些"。

典型场景：群主连着加了两个人（v1 → v2 → v3），某台设备一直离线、手里只有 v1。
它连上来时本机是 v3，`v3.prev` 指向 v2 而不是 v1 —— 只看当前名单的话，"本机认得
v1"无从判断，于是这台设备取不到新名单，只能回去找群主要邀请串。存下历史就解决了。

## 形态

    <身份目录>/roster-history.json
    {"rosters": [<最新一份>, <次新>, …]}     # 新→旧，去重，最多 keep 份

为什么是单一 JSON 而不是"每份一个文件"：名单是小对象（几 KB），历史深度以十计，
一次性读写最简单，也不会在磁盘上留下需要回收的一堆小文件。写盘走唯一临时名
（`tempfile.mkstemp`）+ `fsync` + `os.replace`：这个文件被反复重写，直接覆盖时被
中断会留下半截 JSON，而固定的临时名会让两个并发写者抢同一个文件、`os.replace`
之后磁盘上可能是一份谁都没写完的内容。同一文件的读改写另有**进程内每文件一把锁**
（见 `_lock_for`）：`record()` 是读-改-写，两个成员并发 push 时后写的会把先写的
那一份从历史里整段盖掉。

## 与 `roster.json` 的分工

`roster.json` 是**当前生效**的那一份，鉴权路径只读它；本文件只是准入判定的旁证，
缺失或损坏都不影响通信（代价是对端可能被要求补交历史，见 `_local_roster_chain`）。
因此这里的读盘失败一律降级为空历史 + warning，绝不抛异常。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .records import RecordError
from .roster import Roster

log = logging.getLogger(__name__)

HISTORY_FILE = 'roster-history.json'

# 保留多少份历史。取 12 是因为名单变更频率很低（成员增删），而准入只需要覆盖
# "离线一段时间"的场景；再深的链对判定没有额外价值，只会把文件撑大。
DEFAULT_KEEP = 12

# ── 每文件一把锁 ──────────────────────────────────────────────────────────
#
# 锁挂在**文件路径**上而不是实例上：`record_history()` / `load_history()` 每次调用
# 都新建一个 `RosterHistory`，而插件侧只持有一个长驻实例，两种用法必须互斥。
# 键的数量等于本进程用过的身份目录数（一个），不需要回收。
_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    """取该历史文件专属的可重入锁（`record()` 内部会再次进入 `load()`）。"""
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


class RosterHistory:
    """一份去重、有界的名单历史（新→旧）。"""

    def __init__(self, root: Path, keep: int = DEFAULT_KEEP) -> None:
        self.path = Path(root) / HISTORY_FILE
        self.keep = max(1, int(keep))
        self._rosters: List[Roster] = []
        self._loaded = False
        self._lock = _lock_for(self.path)

    # ── 读 ────────────────────────────────────────────────────────────────

    def load(self) -> List[Roster]:
        """读取历史（新→旧）。文件不存在返回空表，损坏时降级为空表 + warning。"""
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> List[Roster]:
        """`load()` 的实现（调用方已持有该文件的锁）。"""
        if self._loaded:
            return list(self._rosters)
        self._loaded = True
        self._rosters = []
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            items = payload.get('rosters') if isinstance(payload, dict) else None
            if not isinstance(items, list):
                items = []
        except (OSError, ValueError) as exc:
            log.warning(f'[RosterHistory] {self.path} 无法解析，按空历史处理: {exc}')
            return []
        for item in items[:self.keep]:
            try:
                self._rosters.append(Roster.from_dict(item))
            except RecordError as exc:
                # 单条坏记录不让整份历史作废：历史只是准入旁证，少一份的代价是
                # "某个落后版本要对端补交历史"，而不是功能不可用。
                log.warning(f'[RosterHistory] 跳过一次无法解析的历史名单: {exc}')
        # 统一按新→旧排列：`record()` 是"新的排最前"，但文件可以被手工改、
        # 也可能由旧版本写下别的顺序，而调用方（准入判定）依赖这个顺序。
        self._rosters = self._normalize(self._rosters)
        return list(self._rosters)

    def _normalize(self, rosters: List[Roster]) -> List[Roster]:
        """去重、按新→旧排序、裁到 `keep` 份。"""
        return Roster.history_chain(*rosters)[:self.keep]

    def to_dict(self) -> Dict[str, Any]:
        return {'rosters': [roster.to_dict() for roster in self.load()]}

    # ── 写 ────────────────────────────────────────────────────────────────

    def record(self, roster: Roster) -> List[Roster]:
        """把一份名单并入历史（去重、裁剪、落盘），返回并入后的历史。

        去重键是 `content_hash`（只对参与签名的字段求值，因此与"谁转发"无关）。
        **传入的名单必须已经签名完毕**：`content_hash` 是现算的，先入历史再签名会
        让同一份名单以两个哈希各占一格（`Roster.history_chain` 的说明里有同样的坑）。

        整个读-改-写在同一把每文件锁里完成：`record()` 的两个调用方（后台同步收到
        对端 push、界面侧采纳名单）可能来自不同线程，锁外的"读旧表 → 改 → 写回"
        会让后写的把先写的那一份从历史里盖掉，表现成"刚采纳的名单下次重启不见了"。
        """
        with self._lock:
            current = self._load_locked()
            # 已有同名（同 content_hash）的先移除：重新并入会把它排到最前面，
            # 这样"最近被采纳/使用过的"始终在前面；随后统一按版本排序，
            # 使**内存与磁盘随时一致**（否则 `versions()` 会给出并入顺序而不是版本序）。
            keep = [item for item in current if item.content_hash != roster.content_hash]
            merged = self._normalize([roster, *keep])
            self._rosters = merged
            self._loaded = True
            try:
                self._write(merged)
            except OSError as exc:
                # 落盘失败不回滚内存状态：本进程内仍能用新历史判定准入，
                # 只是重启后丢掉 —— 比"连内存里都不认"好。
                log.warning(f'[RosterHistory] 历史写盘失败（本次运行内仍有效）: {exc}')
            return list(merged)

    def _write(self, rosters: List[Roster]) -> None:
        """原子落盘：唯一临时名 + `fsync` + `os.replace`。

        临时名由 `tempfile.mkstemp` 生成而不是固定的 `roster-history.json.tmp`：
        插件进程与 CLI 可能同时写同一个身份目录，固定临时名会让两个写者先后写进
        同一个文件，`os.replace` 之后留下的是一份谁都没写完的内容。锁只管本进程，
        mkstemp 管跨进程。
        """
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({'rosters': [roster.to_dict() for roster in rosters]},
                             ensure_ascii=False, indent=2) + '\n'
        fd, tmp_name = tempfile.mkstemp(prefix=f'{self.path.name}.', suffix='.tmp',
                                        dir=str(directory))
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload.encode('utf-8'))
                handle.flush()
                # 不 fsync 的话"已写入"只到页缓存：掉电后 os.replace 之后仍可能是
                # 半份或旧内容，而这份文件正是准入判定的依据。
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            # 失败时删掉临时文件：`record()` 只把写盘失败当 warning 继续跑，
            # 留下的临时文件会一直堆在身份目录里，没有人回收。
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ── 查询 ──────────────────────────────────────────────────────────────

    def chain(self, *extra: Optional[Roster]) -> List[Roster]:
        """历史 + `extra` 组成的链（新→旧、去重）。

        给 `Node._local_roster_chain()` 用：判定"本机认不认得对端那份"时，
        把本机当前名单一并纳入（它可能还没被 `record()` 过）。
        `extra` 允许 None（调用方常有一个"可选的当前名单"），由 `history_chain` 跳过。
        """
        return Roster.history_chain(*(list(extra) + self.load()))

    def contains(self, roster: Roster) -> bool:
        """本机历史里是否有这一份（按 content_hash 比）。"""
        return any(item.content_hash == roster.content_hash
                   and item.version == roster.version
                   and item.group == roster.group
                   for item in self.load())

    def versions(self) -> List[int]:
        return [roster.version for roster in self.load()]


def load_history(root: Path) -> List[Roster]:
    """便捷入口：读某个身份目录下的名单历史（新→旧）。"""
    return RosterHistory(root).load()


def record_history(root: Path, roster: Roster) -> List[Roster]:
    """便捷入口：把一份**已签名**的名单并入某个身份目录的历史。"""
    return RosterHistory(root).record(roster)


def history_of(rosters: Iterable[Roster], keep: int = DEFAULT_KEEP) -> List[Roster]:
    """把若干份名单整理成历史用的形态（去重、裁剪、新→旧）。不落盘。"""
    return Roster.history_chain(*rosters)[:max(1, int(keep))]
