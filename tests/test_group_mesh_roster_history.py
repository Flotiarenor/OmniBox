"""名单历史的并发写入：两名成员同时 push 不能把对方那一份从历史里盖掉。

背景（审计项 D1）：`RosterHistory.record()` 是"读文件 → 合并 → 写回"，原先既没有锁，
临时名也固定成 `roster-history.json.tmp`。两个调用方来自不同线程（后台同步收到对端
push、界面侧采纳名单）时，后写的那次用自己读到的旧表覆盖先写的那次；固定临时名还会
让两次写盘抢同一个文件，`os.replace` 之后留下的可能是一份谁都没写完的内容。

这两类问题都不会报错：`record()` 的落盘失败只记 warning（历史只是准入旁证，缺了
不影响通信）。所以症状是"刚采纳的名单重启后不见了"和"落后几版的设备突然取不到新
名单"，只有在并发下偶发，靠手测抓不到 —— 因此这里用真实文件系统（临时目录）、
真实多线程锁住它，不 mock。

断言四件事：

1. 两个线程并发 push（每次调用都新建实例，即 `record_history()` 的用法）不丢条目，
   落盘文件仍是合法 JSON；
2. 插件侧那种"一个长驻实例被多线程调用"的用法同样不丢；
3. 并发写完之后身份目录里没有残留的临时文件；
4. 竞态是否命中取决于调度，所以同一场景重复若干轮，避免某一轮侥幸躲过就判绿。

运行：
    python -m unittest tests.test_group_mesh_roster_history -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Callable, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.groupmesh import crypto_prims as cp
from shell.groupmesh.identity import Principal
from shell.groupmesh.roster import Roster, founding_roster, next_roster
from shell.groupmesh.roster_history import HISTORY_FILE, RosterHistory, record_history

# 历史深度取默认值（12）：两个线程各推 6 个版本，正好占满且不触发裁剪
VERSION_COUNT = 12
# 竞态是否命中取决于调度，单轮可能侥幸躲过；多轮是为了让"旧实现"稳定地红
ROUNDS = 3


class ConcurrentRecordTest(unittest.TestCase):
    """并发 `record()`：不丢历史、不抢临时文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.owner = Principal.create('owner')
        self.rosters = self._signed_chain(VERSION_COUNT)

    def _signed_chain(self, count: int) -> List[Roster]:
        """一条已签名的名单链，版本 1..count。

        必须**先签名再入历史**：`content_hash` 是现算的，先入历史后签名会让同一份
        名单在链里以两个哈希各占一格（`Roster.history_chain` 里写明了这个坑）。
        """
        rosters = [founding_roster('history', self.owner, [cp.generate_sign_keypair()[1]],
                                   ttl_seconds=3600)]
        for _ in range(count - 1):
            nxt = next_roster(rosters[-1], 'history', rosters[-1].owner_key,
                              list(rosters[-1].members), ttl_seconds=3600)
            nxt.sign(self.owner.private_key)
            rosters.append(nxt)
        return rosters

    def _run_concurrently(self, workers: List[Callable[[], None]]) -> None:
        """让所有 worker 尽量同时起跑；线程里的异常带回测试线程断言。"""
        errors: List[BaseException] = []
        barrier = threading.Barrier(len(workers))

        def run(worker: Callable[[], None]) -> None:
            try:
                barrier.wait(timeout=10)
                worker()
            except BaseException as exc:      # 线程里的失败必须回到主线程，否则用例会假绿
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(worker,), name=f'hist-{index}')
                   for index, worker in enumerate(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([thread.name for thread in threads if thread.is_alive()], [],
                         '并发 record() 超时仍未结束')
        self.assertEqual(errors, [])

    def _two_writers(self, record: Callable[[Roster], object]) -> List[Callable[[], None]]:
        """两个写者各推一半版本（前半、后半），最终两份都必须在历史里。"""
        halves = [self.rosters[:VERSION_COUNT // 2], self.rosters[VERSION_COUNT // 2:]]

        def writer(part: List[Roster]) -> None:
            for roster in part:
                record(roster)

        return [lambda part=part: writer(part) for part in halves]

    def _assert_full_history(self, root: Path, label: str) -> None:
        expected = list(range(VERSION_COUNT, 0, -1))
        self.assertEqual(RosterHistory(root).versions(), expected,
                         f'{label}: 历史丢了条目（后写的把先写的盖掉了）')
        payload = json.loads((root / HISTORY_FILE).read_text(encoding='utf-8'))
        self.assertEqual(len(payload['rosters']), VERSION_COUNT,
                         f'{label}: 落盘条目数与历史不符，可能写进了半份内容')

    def test_record_history_calls_do_not_lose_history(self):
        """每次调用新建实例（`record_history()` 的用法）：锁必须挂在文件上而不是实例上。"""
        for round_index in range(ROUNDS):
            with self.subTest(round=round_index):
                round_root = self.root / f'round-{round_index}'
                self._run_concurrently(
                    self._two_writers(lambda roster, root=round_root: record_history(root, roster)))
                self._assert_full_history(round_root, f'round-{round_index}')

    def test_long_lived_instance_is_safe_under_threads(self):
        """插件只持有一个实例（`Storage.roster_history()`）：多线程调同一实例也要互斥。"""
        for round_index in range(ROUNDS):
            with self.subTest(round=round_index):
                round_root = self.root / f'shared-{round_index}'
                store = RosterHistory(round_root)
                self._run_concurrently(self._two_writers(store.record))
                self._assert_full_history(round_root, f'shared-{round_index}')
                # 同一实例的内存副本与落盘内容一致（读-改-写在锁内完成）
                self.assertEqual(store.versions(), list(range(VERSION_COUNT, 0, -1)))

    def test_concurrent_writes_leave_no_temp_files(self):
        """临时名唯一且用完即删：固定名 `.json.tmp` 在并发下会被两个写者抢。"""
        round_root = self.root / 'temps'
        self._run_concurrently(
            self._two_writers(lambda roster: record_history(round_root, roster)))
        self.assertEqual(sorted(path.name for path in round_root.glob('*.tmp')), [],
                         '并发写盘留下了临时文件')
        self.assertTrue((round_root / HISTORY_FILE).is_file(), '并发写完之后历史文件不见了')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
