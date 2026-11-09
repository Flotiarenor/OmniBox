"""统一新鲜度基建（shell/backend/freshness.py）的回归测试。

固化的行为契约（每条都对应旧实现里真实出过的问题）：

- 冷启动同步 = 全部条目算新增，派生数据只对"变化项"重算（对比现状：media-player
  的深度扫描 `merged = {}` 会把整库标签重读一遍）；
- 稳态短路 = 每个目录只 scandir 一次、零 stat（`derive` 不被调用）；
- 去抖与单飞：连续调用只真扫一次，重入返回 busy（现状：image-cleaner 的
  "查-建-赋值"无锁，双击按钮就是两趟全盘 walk）；
- 幽灵清理：文件/目录消失后条目与派生缓存一起被清（现状：media-player 的增量
  扫描只增不减，"删除的媒体永久留在索引里"）；
- 原地覆盖写（同名、新内容、目录 mtime 与名字个数都不变）被动同步看不见，
  由**校验**兜住 —— 这是"同步便宜 / 校验彻底"分工的边界，必须显式固化；
- `content_version` 升级 = 落后目录重算派生数据（替代散落各插件的 INDEX_VERSION 常量），
  版本章**按目录**推进：预算用尽时下一趟接着盖没盖过的目录，不会每趟从头重算同一批，
  且版本升级的条目不算"新增"；
- `verified_keys()` 只在整趟成功校验之后给出集合，之前与之后（同步发现增删改）都是 None；
- 旧指纹库（`dirs` 表没有 `version` 列）就地补列，已有目录指纹不丢；
- 状态查询不建库（"看一眼占用"不该凭空造出数据库，同 ThumbCache.stats 的理由）。

运行：
    venv/bin/python -m unittest tests.test_freshness -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.freshness import (
    DEFAULT_MAX_DIRS,
    DEFAULT_MIN_SYNC_INTERVAL,
    FreshnessEngine,
    FreshnessSpec,
    FreshnessStore,
    FsSource,
    all_engines,
    engine_for,
)
from shell.backend.plugin_manager import PluginManager
from shell.backend.tasks import BackgroundTask


class _FakePlugin:
    """只装配引擎需要的两件事：缓存目录与 freshness_spec()。"""

    def __init__(self, cache_dir: Path, spec: dict | None):
        self.name = 'fake-plugin'
        self._cache_dir = cache_dir
        self._spec = spec
        #: `engine_for` 挂在这里；用例把它清成 None 以强制重建引擎
        self._freshness_engine: Any = None
        self.derived = []        # 每次 derive 收到的条目键
        self.pruned = []         # 每次 prune 收到的键
        self.audits = []         # 每次 on_verified 收到的有效键集合
        self.rebuilt = []

    def get_cache_dir(self) -> Path:
        return self._cache_dir

    def freshness_spec(self) -> dict | None:
        return self._spec


def _spec(plugin: _FakePlugin, root: Path, **over) -> dict:
    spec = {
        'roots': lambda: [root],
        'content_version': 1,
        'derive': lambda items: plugin.derived.append([i['key'] for i in items]),
        'prune': lambda keys: plugin.pruned.extend(keys),
        'on_verified': lambda keys: plugin.audits.append(set(keys)),
        'unit': '项',
        'min_sync_interval': 0.0,
    }
    spec.update(over)
    return spec


def _require_engine(plugin: _FakePlugin) -> FreshnessEngine:
    """取引擎并断言已装配（`engine_for` 的返回类型是 Optional）。"""
    engine = engine_for(plugin)
    assert engine is not None, '插件必须能建出引擎'
    return engine


class FreshnessEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'media'
        self.root.mkdir()
        self.cache = Path(self._tmp.name) / '.cache' / 'fake-plugin'
        self.plugin = _FakePlugin(self.cache, None)
        self.plugin._spec = _spec(self.plugin, self.root)
        self.engine = engine_for(self.plugin)
        self.assertIsNotNone(self.engine)

    # ----- 工具 -----

    def _write(self, rel: str, data: bytes = b'x' * 8) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _sync(self, scope: str = '') -> dict:
        return self.engine.sync(scope, force=True)

    def _fresh_engine(self):
        """本用例的引擎，断言已装配（`engine_for` 的返回类型是 Optional）。"""
        assert self.engine is not None
        return self.engine

    def _derive_keys(self) -> list:
        return [k for batch in self.plugin.derived for k in batch]

    # ----- 契约 -----

    def test_cold_sync_indexes_everything_and_derives_once(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        report = self._sync()

        self.assertEqual(report['action'], 'sync')
        self.assertEqual(report['added'], 2)
        self.assertEqual(report['changed'], 0)
        self.assertEqual(report['dirs'], 2)          # 根本身 + sub
        self.assertEqual(sorted(self._derive_keys()), ['a.jpg', 'sub/b.jpg'])
        self.assertEqual(report['skipped_dirs'], 0)   # 冷启动时无可短路

    def test_steady_state_short_circuits_without_stat(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.plugin.derived.clear()

        report = self._sync()

        self.assertEqual(report['added'], 0)
        self.assertEqual(report['changed'], 0)
        self.assertEqual(report['dirs'], 2)
        self.assertEqual(report['skipped_dirs'], 2, '目录级短路失效，稳态仍在逐条 stat')
        self.assertEqual(self.plugin.derived, [], '稳态不该重算派生数据')

    def test_sync_is_throttled_and_can_be_forced(self):
        self._write('a.jpg')
        self.engine.spec.min_sync_interval = 30.0
        self.engine.sync('')                        # 第一次真扫
        self.assertEqual(self.engine.sync('')['reason'], 'throttled')
        self.assertEqual(self.engine.sync('', force=True)['action'], 'sync')

    def test_new_file_is_caught_even_if_dir_mtime_is_restored(self):
        """目录 mtime 被外部工具还原时，靠"纳入索引的名字个数"仍然发现新增。"""
        self._write('a.jpg')
        self._sync()
        stamp = self.root.stat()
        self._write('b.jpg')
        os.utime(self.root, (stamp.st_atime, stamp.st_mtime))   # 还原目录 mtime
        self.plugin.derived.clear()

        report = self._sync()

        self.assertEqual(report['added'], 1)
        self.assertEqual(self._derive_keys(), ['b.jpg'])

    def test_removed_file_is_pruned(self):
        keep = self._write('a.jpg')
        gone = self._write('b.jpg')
        self._sync()
        gone.unlink()
        os.utime(self.root, (keep.stat().st_atime, time.time()))   # 确保目录 mtime 变化

        report = self._sync()

        self.assertEqual(report['removed'], 1)
        self.assertEqual(self.plugin.pruned, ['b.jpg'])

    def test_verify_finds_size_changing_in_place_write(self):
        """长度变了的原地写入：校验逐条比 (mtime, size)，必然发现它。"""
        path = self._write('a.jpg', b'x' * 16)
        self._write('b.jpg')
        self._sync()
        stamp = path.stat()
        path.write_bytes(b'x' * 64)                 # 长度变了，但目录 mtime 没变
        os.utime(path, (stamp.st_atime, stamp.st_mtime))
        self.plugin.derived.clear()

        report = self._fresh_engine().verify()      # 校验必须逐项 stat 出来
        self.assertEqual(report['action'], 'verify')
        self.assertEqual(sorted(self._derive_keys()), ['a.jpg'])

    def test_equal_length_in_place_write_is_invisible_to_both_actions(self):
        """边界固化①：判据是 (mtime, size)，两者都没变的改写连校验也发现不了。

        同长度 + 还原 mtime 是最难发现的一种：目录 mtime、名字个数、文件大小之和、
        条目指纹全都不变。要覆盖它只能靠重建派生缓存，或插件自己在读文件时核对内容。
        """
        path = self._write('a.jpg', b'x' * 16)
        self._sync()
        stamp = path.stat()
        path.write_bytes(b'y' * 16)                 # 长度不变，只有内容变了
        os.utime(path, (stamp.st_atime, stamp.st_mtime))
        self.plugin.derived.clear()

        self.assertEqual(self._sync()['changed'], 0, '同步不该声称发现了它')
        self.assertEqual(self._fresh_engine().verify()['changed'], 0,
                         '校验的判据也只有 mtime + size')
        self.assertEqual(self.plugin.derived, [])

    def test_short_circuit_compares_size_sum_where_the_scan_provides_it(self):
        """目录枚举顺带给出文件大小的平台上，短路还要比大小之和。

        补的是两条原判据的盲区：原地写入换了长度、同一秒里"删一个加一个"（个数不变）。
        POSIX 上取大小要真 stat 每个文件，与"稳态零 stat"冲突，因此只在枚举顺带
        给出时启用 —— 这里显式打开开关来验证判据本身。
        """
        path = self._write('a.jpg', b'x' * 16)
        self._write('b.jpg')
        self._sync()
        stamp = path.stat()
        self.plugin.derived.clear()
        path.write_bytes(b'x' * 64)                 # 长度变了，但目录 mtime 没变
        os.utime(path, (stamp.st_atime, stamp.st_mtime))

        with mock.patch('shell.backend.freshness._STAT_SIZE_IN_SCANDIR', True):
            report = self._sync()

        self.assertEqual(report['changed'], 1, '大小之和变了，同步要发现它')
        self.assertEqual(self._derive_keys(), ['a.jpg'])

    def test_short_circuit_skips_size_sum_when_the_scan_does_not_provide_it(self):
        """取不到目录内文件大小的平台上，判据保持"目录 mtime + 个数"两条。"""
        path = self._write('a.jpg', b'x' * 16)
        self._sync()
        stamp = path.stat()
        path.write_bytes(b'x' * 64)
        os.utime(path, (stamp.st_atime, stamp.st_mtime))

        with mock.patch('shell.backend.freshness._STAT_SIZE_IN_SCANDIR', False):
            report = self._sync()

        self.assertEqual(report['changed'], 0)
        self.assertEqual(report['skipped_dirs'], report['dirs'])

    def test_steady_state_sync_is_not_partial_when_dirs_exceed_budget(self):
        """目录数超过 `max_dirs` 时，稳态同步**不得**报 partial。

        回归对象：预算原先按"走过的目录数"算，于是任何目录数 > max_dirs 的库
        **每次**进视图都会报 partial，界面每次都弹"同步未覆盖全部目录"并轮询 ——
        而稳态下一个目录都没变。预算要按"真正干了活的目录"算。
        """
        for i in range(5):
            self._write(f'album{i}/a.jpg')
        self.engine.spec.max_dirs_per_sync = 2

        first = self._sync()
        self.assertTrue(first['partial'])
        self.assertEqual(first['reason'], 'budget', ' 还有目录要处理 → 预算型 partial')

        self.engine.verify()                      # 全量走完，指纹齐了

        report = self._sync()

        self.assertEqual(report['action'], 'sync', f'稳态不该是 partial: {report}')
        self.assertGreater(report['dirs'], 2, '这趟确实走过了超过预算的目录数')
        self.assertEqual(report['skipped_dirs'], report['dirs'], '全部走的是短路快路')

    def test_progress_advances_on_every_visited_dir(self):
        """进度要对每个走过的目录推进：只在"条目有增删改"的分支更新，稳态校验会停在 0/0。"""
        for i in range(4):
            self._write(f'album{i}/a.jpg')
        self.engine.verify()                      # 铺底（这一趟有新增）
        seen = []

        def audit(keys):
            seen.append(self.engine.state()['task'])

        self.engine.spec.on_verified = audit
        self.engine.start_verify()
        for _ in range(300):
            if not self.engine.state()['busy']:
                break
            time.sleep(0.01)

        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0]['processed'], 0, '稳态校验期间进度必须前进')
        self.assertEqual(seen[0]['total'], seen[0]['processed'])

    def test_pass_end_can_report_progress_inside_the_task(self):
        """收尾钩子要能把"正在重建索引"写进任务进度。

        否则界面停在"进度满了"却仍未结束的状态 —— 收尾重活（重建索引之类）可能
        耗时数秒，看起来就是卡住。钩子拿不到任务对象，只能通过 `engine.progress()`。
        """
        seen = []
        engine_holder = {}

        def pass_end(report, verified):
            engine_holder['engine'].progress('重建索引')
            seen.append(engine_holder['engine'].state()['task']['current'])
            return {'post': 'index'}

        self._write('a.jpg')
        self.plugin._spec = _spec(self.plugin, self.root, on_pass_end=pass_end)
        self.plugin._freshness_engine = None
        engine = engine_for(self.plugin)
        engine_holder['engine'] = engine

        engine.start_verify()
        for _ in range(300):
            if not engine.state()['busy']:
                break
            time.sleep(0.01)

        self.assertEqual(seen, ['重建索引'], '钩子里的进度文本要落到当前任务上')
        self.assertEqual(engine.state()['last_report']['pass_end'], {'post': 'index'})

    def test_progress_without_a_task_is_a_no_op(self):
        self.assertIsNone(self.engine.progress('无所谓'))       # 不抛错即可

    def test_stat_entries_opt_in_makes_sync_see_in_place_writes(self):
        """边界固化②：目录不大、且要求"替换立刻反映"的插件用 stat_entries 换准确度。"""
        self.plugin._spec = _spec(self.plugin, self.root, stat_entries=True)
        self.plugin._freshness_engine = None
        engine = engine_for(self.plugin)
        path = self._write('a.jpg', b'x' * 16)
        engine.sync('', force=True)
        self.plugin.derived.clear()

        stamp = path.stat()
        path.write_bytes(b'x' * 64)
        os.utime(path, (stamp.st_atime, stamp.st_mtime))
        report = engine.sync('', force=True)

        self.assertEqual(report['changed'], 1)
        self.assertEqual(self._derive_keys(), ['a.jpg'])

    def test_verify_drops_vanished_directory_and_audits_keys(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.plugin.pruned.clear()

        (self.root / 'sub' / 'b.jpg').unlink()
        (self.root / 'sub').rmdir()
        report = self.engine.verify()

        self.assertEqual(report['action'], 'verify')
        self.assertIn('sub/b.jpg', self.plugin.pruned)
        self.assertEqual(self.plugin.audits[-1], {'a.jpg'})

    def test_content_version_bump_recomputes_everything(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.plugin.derived.clear()

        self.engine.spec.content_version = 2
        report = self.engine.verify()

        self.assertTrue(report['force_all'])
        self.assertEqual(sorted(self._derive_keys()), ['a.jpg', 'sub/b.jpg'])
        self.assertEqual(self.engine.state()['content_version'], 2)
        self.assertFalse(self.engine.state()['version_stale'])

    # ----- 版本章按目录推进（超预算的库） -----

    def _library(self, count: int) -> None:
        """建 `count` 个子目录、每个目录一张图（索引里的目录总数 = count + 1）。"""
        for index in range(count):
            self._write(f'd{index:03d}/a.jpg')

    def test_version_bump_advances_per_directory_across_budgeted_syncs(self):
        """预算用尽时版本章**按目录**推进：同一批目录不会被反复重算。

        落后章记在目录上：450 个目录 / 预算 400 的库上，第一趟只盖 400 个
        （partial/budget，剩下 51 个仍落后），第二趟跳过已盖章的目录、把剩下的 51 个
        盖完并写整趟的 meta 章，第三趟完全稳态（零重算）。
        """
        engine = self._fresh_engine()
        self._library(450)
        engine.spec.max_dirs_per_sync = 400
        self.assertTrue(self._sync()['partial'], '铺底这一趟必然超预算')
        engine.verify()                            # 全量走完：版本 1 的章齐了
        state = engine.state()
        self.assertEqual(state['stale_dirs'], 0)
        self.assertFalse(state['version_stale'])
        self.plugin.derived.clear()

        engine.spec.content_version = 2
        state = engine.state()
        self.assertTrue(state['version_stale'], '还有目录没按新版本重算')
        self.assertEqual(state['stale_dirs'], 451, '合成根 + 450 个子目录都落后')

        first = self._sync()
        first_batch = [k for batch in self.plugin.derived for k in batch]
        self.plugin.derived.clear()
        self.assertTrue(first['partial'])
        self.assertEqual(first['reason'], 'budget')
        self.assertEqual(first['added'], 0, '版本升级不是"新增"，不能污染变化计数')
        self.assertEqual(first['restamped'], 400, '预算内只盖 400 个目录')
        self.assertLessEqual(len(first_batch), 400, '只重算预算内的目录')
        state = engine.state()
        self.assertEqual(state['stale_dirs'], 51, '盖过章的目录不再算落后')
        self.assertTrue(state['version_stale'])

        second = self._sync()
        second_batch = [k for batch in self.plugin.derived for k in batch]
        self.plugin.derived.clear()
        self.assertFalse(second['partial'], f'余下目录在预算内，应当走完: {second}')
        self.assertEqual(second['restamped'], 51)
        self.assertEqual(len(second_batch), 51)
        self.assertEqual(set(first_batch) & set(second_batch), set(),
                         '第二趟不该重算第一趟已经盖过章的目录')
        self.assertEqual(len(set(first_batch) | set(second_batch)), 450, '两趟合起来覆盖全部条目')
        state = engine.state()
        self.assertEqual(state['stale_dirs'], 0)
        self.assertFalse(state['version_stale'], '全部盖章后不该再提示需要校验')
        self.assertEqual(state['content_version'], 2)

        third = self._sync()
        self.assertEqual(third['added'], 0)
        self.assertEqual(third['restamped'], 0)
        self.assertEqual(third['skipped_dirs'], third['dirs'], '稳态全部走短路')
        self.assertEqual(self.plugin.derived, [], '稳态不该再重算派生数据')

    def test_version_bump_does_not_turn_every_entry_into_an_added_one(self):
        """版本升级只让插件"重算派生数据"，`added`/`changed` 仍只报真实变化。"""
        engine = self._fresh_engine()
        self._write('a.jpg')
        self._sync()
        self.plugin.pruned.clear()
        self.plugin.derived.clear()

        engine.spec.content_version = 2
        report = self._sync()

        self.assertEqual(report['added'], 0)
        self.assertEqual(report['changed'], 0)
        self.assertEqual(report['removed'], 0, '版本升级不该被当成"文件消失"')
        self.assertEqual(self.plugin.pruned, [])
        self.assertEqual(self._derive_keys(), ['a.jpg'], '落后目录的条目仍交给插件重算')

    def test_restamp_only_entries_carry_the_marker_for_derive(self):
        """只因为 content_version 升级被重算的条目带 `restamp: True`；真实变化不带。

        插件（image-viewer）据此区分"按新规则重算派生数据"与"文件真的变了"：纯
        restamp 批次只作废目录节点，不能反复删整份相册索引。预算用尽 + 版本升级
        是最容易踩到的组合（一部分批次全是 restamp，另一批才混进真实变化）。

        目录枚举顺序由文件系统决定（NTFS / ext4 不同），因此不绑定"第一批是哪个
        目录"：先写入 d1 的真实变化，再按预算连续同步到整趟走完，逐批检查标记。
        """
        engine = self._fresh_engine()
        for name in ('d0', 'd1', 'd2'):
            self._write(f'{name}/a.jpg', b'x' * 8)
            self._write(f'{name}/b.jpg', b'x' * 8)
        self._sync()
        engine.verify()                       # 版本 1 全量盖章
        batches: list = []
        engine.spec.derive = lambda items: batches.append([dict(i) for i in items])
        engine.spec.content_version = 2
        engine.spec.max_dirs_per_sync = 2
        # 目录枚举顺序随文件系统（NTFS / ext4）而异，所以不假设"第一趟轮到 d0"：
        # 升级前先写下 d1 的真实变化，再按预算连续同步到整趟走完，逐批检查标记。
        (self.root / 'd1' / 'a.jpg').write_bytes(b'y' * 16)

        reports: list = []
        for _ in range(6):
            reports.append(self._sync())
            if not reports[-1]['partial']:
                break
        else:
            self.fail(f'预算内应能走完全部目录: {reports}')
        self.assertTrue(reports[0]['partial'], reports[0])
        self.assertEqual(reports[0]['reason'], 'budget', reports[0])
        self.assertEqual(sum(report['added'] for report in reports), 0,
                         '版本升级不是"新增"，不能污染变化计数')
        self.assertEqual(sum(report['changed'] for report in reports), 1,
                         '只有 d1/a.jpg 是真实变化')
        self.assertEqual(sum(report['restamped'] for report in reports), 4,
                         '合成根 + d0/d1/d2 各因版本落后被重算一次')

        items = [item for batch in batches for item in batch]
        self.assertEqual({item['dir'] for item in items}, {'d0', 'd1', 'd2'}, items)
        real = [item for item in items if item['key'] == 'd1/a.jpg']
        self.assertEqual(len(real), 1, items)
        self.assertNotIn('restamp', real[0], '真实 changed 不能标 restamp')
        restamped = [item for item in items if item['key'] != 'd1/a.jpg']
        self.assertEqual(len(restamped), 5, items)
        self.assertTrue(all(item.get('restamp') is True for item in restamped), restamped)

        # ③ 版本已全部盖章后，真实变化批次完全不带 restamp。
        # 用"新增文件"而不是原文件改内容：原地写入不改目录 mtime，POSIX 上被动同步
        # 按设计看不见它（只有校验能发现），换成新增文件后两个平台口径一致。
        batches.clear()
        engine.spec.max_dirs_per_sync = DEFAULT_MAX_DIRS
        self._write('d2/c.jpg', b'y' * 32)
        third = self._sync()
        self.assertEqual(third['added'], 1, third)
        self.assertEqual(third['restamped'], 0, third)
        self.assertTrue(batches, '真实变化必须交给 derive')
        self.assertTrue(all('restamp' not in item for batch in batches for item in batch),
                        batches)
        self.assertEqual([item['key'] for batch in batches for item in batch], ['d2/c.jpg'])

    def test_state_exposes_stale_dirs_count(self):
        """`stale_dirs` 是诊断"还有多少目录没按当前规则重算"的直接读数。"""
        engine = self._fresh_engine()
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        state = engine.state()
        self.assertEqual(state['stale_dirs'], 0)
        self.assertFalse(state['version_stale'])

        engine.spec.content_version = 3
        state = engine.state()
        self.assertEqual(state['stale_dirs'], 2)
        self.assertTrue(state['version_stale'])

        engine.verify()
        state = engine.state()
        self.assertEqual(state['stale_dirs'], 0)
        self.assertFalse(state['version_stale'])

    # ----- verified_keys：只在整趟成功校验后有值 -----

    def test_verified_keys_only_after_a_clean_full_pass(self):
        """给破坏性判断用的权威结论：同步不给、部分遍历不给、有读取错误也不给。

        媒体的播放参数就是按它清理的：索引暂时不完整时不能把"索引里没有"当成
        "文件消失"。
        """
        engine = self._fresh_engine()
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.assertIsNone(engine.verified_keys(), '同步不构成"整趟确认"')

        report = engine.verify()
        self.assertFalse(report['partial'])
        self.assertEqual(engine.verified_keys(), {'a.jpg', 'sub/b.jpg'})

        keys = engine.verified_keys()
        assert keys is not None
        keys.add('mutated')
        again = engine.verified_keys()
        assert again is not None
        self.assertNotIn('mutated', again, '返回的必须是副本')

        self._write('c.jpg')                       # 同步发现新增 → 结论作废
        self._sync()
        self.assertIsNone(engine.verified_keys())

    def test_verified_keys_is_none_after_a_cancelled_verify(self):
        """取消的校验不构成整趟确认，而且报告与正常路径同形（`action='verify'`）。

        先跑一趟成功的校验把结论建起来，再取消一趟 —— 否则"取消后是 None"是空洞的
        断言（它本来就是 None，删掉取消分支的实现也照样过）。
        """
        engine = self._fresh_engine()
        self._write('a.jpg')
        engine.verify()
        self.assertEqual(engine.verified_keys(), {'a.jpg'})
        self.assertEqual(engine.state()['last_report']['action'], 'verify')

        task = BackgroundTask(kind='freshness.test')
        task.cancel()
        report = engine.verify(task)

        self.assertTrue(report['cancelled'])
        self.assertTrue(report['partial'])
        self.assertEqual(report['action'], 'verify', '取消路径的报告也要能判分支')
        self.assertEqual(report['content_version'], engine.spec.content_version)
        self.assertIsNone(engine.verified_keys())
        self.assertEqual(engine.state()['last_report']['action'], 'verify')

    def test_verified_keys_is_none_when_the_pass_had_read_errors(self):
        engine = self._fresh_engine()
        self._write('a.jpg')
        engine.verify()
        self.assertIsNotNone(engine.verified_keys())

        with mock.patch('shell.backend.freshness.os.scandir',
                        side_effect=OSError('boom')):
            report = engine.verify()

        self.assertFalse(report['partial'])
        self.assertTrue(report['errors'])
        self.assertIsNone(engine.verified_keys(), '读不完的一趟不能给出权威键集合')

    def test_pass_end_writes_a_closing_progress_text(self):
        """收尾钩子跑在"进度已满"之后：基建先写一句通用文案，界面才不会像卡死。"""
        seen = []
        holder: dict = {}
        # 用独立插件实例（引擎挂在实例上）：`on_pass_end` 在 spec 归一化时读一次
        plugin = _FakePlugin(self.cache.parent / 'pass-end', None)
        plugin._spec = _spec(plugin, self.root, on_pass_end=lambda report, verified: (
            seen.append(holder['engine'].state()['task']['current']), {})[1])
        self._write('a.jpg')
        engine = engine_for(plugin)
        assert engine is not None
        holder['engine'] = engine

        engine.start_verify()
        for _ in range(300):
            if not engine.state()['busy']:
                break
            time.sleep(0.01)

        self.assertEqual(seen, ['收尾：更新插件派生数据'],
                         '钩子开始时进度已满，基建要先写收尾文案')

    def test_budget_marks_partial_and_leaves_no_half_state(self):
        self._write('d1/a.jpg')
        self._write('d2/b.jpg')
        self.engine.spec.max_dirs_per_sync = 1

        report = self._sync()

        self.assertTrue(report['partial'])
        self.assertEqual(report['action'], 'partial')
        # 落库的目录数不超过这一趟处理的目录数（不写"半份指纹"）
        state = self.engine.state()
        self.assertLessEqual(state['dirs'], 1)

    def test_scope_limits_walk_and_rejects_escape(self):
        self._write('sub/a.jpg')
        self._write('other/b.jpg')
        self._sync()
        self.plugin.derived.clear()

        # 只走 sub：other 下的文件不应出现在派生数据里
        report = self._sync('sub')
        self.assertEqual(report['scope'], 'sub')
        self.assertTrue(all(k.startswith('sub/') for k in self._derive_keys()), self._derive_keys())
        self.assertEqual(report['added'], 0)

        for bad in ('../outside', '/etc', 'a/../../b'):
            self.assertEqual(self.engine.sync(bad, force=True)['reason'], 'bad_scope', bad)

    def test_reentrant_sync_reports_busy(self):
        """单飞：同步过程中重入必须被挡（现状里双击按钮会并发两趟全盘 walk）。"""
        self._write('a.jpg')
        seen = []

        def derive(items):
            seen.append(self.engine.sync('', force=True))

        self.engine.spec.derive = derive
        self._sync()

        self.assertEqual(seen[0]['reason'], 'busy', seen)

    def test_key_of_lets_plugins_use_their_own_entry_keys(self):
        """插件用自己的 id 做条目键（media-player 的 md5(path) 形态）。

        这样派生缓存的键（缩略图库按 item id 存）不需要再维护一层映射表，
        `prune` / `on_verified` 收到的也就是插件自己认的键。
        """
        def _id(abs_path: str) -> str:
            return 'id-' + os.path.basename(abs_path)

        self.plugin._spec = _spec(self.plugin, self.root, key_of=_id)
        self.plugin._freshness_engine = None
        engine = engine_for(self.plugin)
        self._write('sub/a.jpg')
        report = engine.sync('', force=True)

        self.assertEqual(report['added'], 1)
        self.assertEqual(self._derive_keys(), ['id-a.jpg'])

        (self.root / 'sub' / 'a.jpg').unlink()
        engine.spec.stat_entries = True
        os.utime(self.root / 'sub', (time.time(), time.time()))
        after = engine.verify()
        self.assertEqual(after['removed'], 1)
        self.assertEqual(self.plugin.pruned, ['id-a.jpg'])

    def test_pass_end_hook_runs_once_per_pass(self):
        """单文件索引的插件靠它落盘（不能每处理一个目录就整体重写一遍）。"""
        calls = []

        def _end(report, verified):
            calls.append((report['dirs'], verified))
            return {'saved': True}

        self.plugin._spec = _spec(self.plugin, self.root, on_pass_end=_end)
        self.plugin._freshness_engine = None
        engine = engine_for(self.plugin)
        self._write('a.jpg')
        self._write('sub/b.jpg')

        synced = engine.sync('', force=True)
        self.assertEqual(calls, [(2, False)])
        self.assertEqual(synced['pass_end'], {'saved': True})

        engine.verify()
        self.assertEqual(calls[-1], (2, True))

    def test_renamed_directory_is_reported_as_removed_by_sync(self):
        """回归：目录改名/整目录删除时，"旧条目消失"必须在同步里就能报出来。

        条目在旧目录下、新目录的条目算"新增"，如果只有校验做目录级清理，
        被动同步就会报"新增 3 个"而把旧的 3 个永远留在库里。
        """
        self._write('sub/a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.plugin.pruned.clear()

        (self.root / 'sub').rename(self.root / 'renamed')
        report = self._sync()

        self.assertEqual(report['added'], 2)
        self.assertEqual(report['removed'], 2, '旧目录的条目必须一起清掉')
        self.assertEqual(sorted(self.plugin.pruned), ['sub/a.jpg', 'sub/b.jpg'])

    def test_partial_pass_never_drops_unseen_directories(self):
        """部分遍历（预算用尽）不得清理：没见到不等于没了。"""
        self._write('d1/a.jpg')
        self._write('d2/b.jpg')
        self.engine.spec.max_dirs_per_sync = 1

        report = self._sync()

        self.assertTrue(report['partial'])
        self.assertEqual(report['removed'], 0)
        self.assertEqual(self.plugin.pruned, [])

    def test_valid_keys_exposes_the_full_entry_set(self):
        """派生数据不按条目算的插件靠它反推（pixiv-sync 的"已下载作品集合"）。"""
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()

        self.assertEqual(self.engine.valid_keys(), {'a.jpg', 'sub/b.jpg'})

        (self.root / 'sub' / 'b.jpg').unlink()
        os.utime(self.root / 'sub', (time.time(), time.time()))
        self._sync()

        self.assertEqual(self.engine.valid_keys(), {'a.jpg'})

    def _remote_engine(self, invalidate, **over):
        """远端来源的引擎：判据只有时间，没有条目与目录。"""
        plugin = _FakePlugin(Path(self._tmp.name) / '.cache' / 'remote', None)
        spec = {'mode': 'remote', 'ttl_seconds': 60.0, 'unit': '首',
                'min_sync_interval': 0.0, 'invalidate': invalidate}
        spec.update(over)
        plugin._spec = spec
        engine = _require_engine(plugin)
        self.assertEqual(engine.spec.mode, 'remote')
        return engine

    def test_remote_mode_uses_ttl_and_invalidates(self):
        """远端来源：新鲜期内跳过，过期或手动校验才让插件丢弃本地缓存。"""
        cleared = []
        engine = self._remote_engine(lambda: cleared.append('x') or {'rows': 3})
        self.assertEqual(engine.spec.mode, 'remote')

        # 首次：没有时间戳 → 视为过期，丢一次缓存
        first = engine.sync()
        self.assertEqual(first['action'], 'sync')
        self.assertEqual(len(cleared), 1)
        state = engine.state()
        self.assertEqual(state['mode'], 'remote')
        self.assertGreater(state['ttl_remaining'], 0)

        # TTL 内：跳过，不再丢缓存
        second = engine.sync()
        self.assertEqual(second['reason'], 'fresh')
        self.assertEqual(len(cleared), 1)

        # 手动校验：忽略 TTL
        verified = engine.verify()
        self.assertEqual(verified['action'], 'verify')
        self.assertEqual(len(cleared), 2)
        self.assertEqual(verified['cleared'], {'rows': 3})

    def test_remote_sync_reports_changed_and_state_uses_fresh_until(self):
        """远端没有条目级判据：丢缓存本身就是"变化"，状态按新鲜期表达。"""
        reasons = []
        engine = self._remote_engine(lambda reason: reasons.append(reason) or {'rows': 1})

        state = engine.state()
        self.assertFalse(state['version_stale'], '远端没有版本章，恒真的提示会误导用户')
        self.assertEqual(state['fresh_until'], 0.0)

        synced = engine.sync()
        self.assertTrue(synced['changed'], '丢掉本地缓存后前端要重载视图')
        self.assertTrue(synced['invalidated'])
        self.assertEqual(reasons, ['ttl'], 'sync 的失效原因是时间到期')

        state = engine.state()
        self.assertFalse(state['version_stale'])
        self.assertGreater(state['fresh_until'], time.time(), 'fresh_until = 上次取数 + TTL')

        verified = engine.verify()
        self.assertTrue(verified['changed'])
        self.assertEqual(reasons, ['ttl', 'verify'], '手动校验的失效原因是 verify')

        forced = engine.sync(force=True, reason='identity')
        self.assertEqual(reasons[-1], 'identity', '登录态变化这类原因由调用方给')
        self.assertTrue(forced['changed'])
        self.assertEqual(forced['invalidate_reason'], 'identity')

    def test_remote_sync_honors_throttle_and_single_flight(self):
        """远端同样受去抖与单飞约束：两趟并发只会重复丢缓存。"""
        engine = self._remote_engine(lambda: {'rows': 0}, min_sync_interval=30.0)
        self.assertEqual(engine.sync()['action'], 'sync')
        throttled = engine.sync()
        self.assertEqual(throttled['reason'], 'throttled')
        self.assertTrue(engine.sync(force=True)['changed'], '强制同步忽略 TTL')

        # 丢缓存进行中再请求一次同步：走单飞，返回 busy 而不是再丢一遍
        seen = []
        engine.spec.min_sync_interval = 0.0
        engine.spec.invalidate = lambda: seen.append(engine.sync()['reason']) or {}
        engine.sync(force=True)
        self.assertEqual(seen, ['busy'])

    def test_multi_root_prefixes_map_to_virtual_keys(self):
        extra = Path(self._tmp.name) / 'extra'
        extra.mkdir()
        (extra / 'c.jpg').write_bytes(b'x' * 8)
        self.plugin._spec = _spec(self.plugin, self.root, roots=lambda: [self.root, extra],
                                  prefixes=('', '__extra'))
        self.plugin._freshness_engine = None
        engine = engine_for(self.plugin)

        report = engine.sync('', force=True)

        self.assertEqual(report['added'], 1)
        self.assertEqual(self._derive_keys(), ['__extra/c.jpg'])

    def test_key_of_entries_carry_the_root_namespace(self):
        """`key_of` 只保证根内唯一：两个根下同相对路径的文件不能抢同一行。

        没有命名空间时，后一根的行会覆盖前一根的：每次同步都把两者判成一增一减，
        `derive` 被反复调用（图片相册是每次整棵相册树重算），而且 `prune` 会删掉
        另一根还在的派生缓存。
        """
        extra = Path(self._tmp.name) / 'extra'
        (extra / 'sub').mkdir(parents=True)
        (extra / 'sub' / 'a.txt').write_bytes(b'x' * 8)
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'a.txt').write_bytes(b'x' * 8)

        def _key(abs_path: str) -> str:
            # 与 document-reader 的 `document_key_for` 同形：根内相对路径
            return Path(abs_path).name

        self.plugin._spec = _spec(self.plugin, self.root, roots=lambda: [self.root, extra],
                                  prefixes=('', '__extra'), key_of=_key)
        self.plugin._freshness_engine = None
        engine = _require_engine(self.plugin)

        first = engine.sync('', force=True)
        self.assertEqual(first['added'], 2, '两个根下各一个文件，都是新增')
        self.assertEqual(sorted(self._derive_keys()), ['a.txt', 'a.txt'],
                         '交给插件的键是插件键（两份同名条目）')

        conn = engine.store.connect()
        try:
            stored = engine.store.entry_keys(conn)
        finally:
            conn.close()
        self.assertEqual(stored, {'a.txt', '__extra/a.txt'}, '存储键带根命名空间')

        self.plugin.derived.clear()
        steady = engine.sync('', force=True)
        self.assertEqual((steady['added'], steady['changed'], steady['removed']), (0, 0, 0),
                         '稳态不该再判出一增一减')
        self.assertEqual(self.plugin.derived, [])

        # prune 收到的也是插件键：删掉额外根下的那份，不能出现 `__extra/a.txt`
        self.plugin.pruned.clear()
        (extra / 'sub' / 'a.txt').unlink()
        os.utime(extra / 'sub', (time.time(), time.time()))
        engine.sync('', force=True)
        self.assertEqual(self.plugin.pruned, ['a.txt'])

        # on_verified 同理
        self.plugin.audits.clear()
        engine.verify()
        self.assertEqual(self.plugin.audits[-1], {'a.txt'})
        self.assertEqual(engine.valid_keys(), {'a.txt'})

    def test_rebuild_clears_fingerprints_and_reruns_verify(self):
        """逃生门：清插件派生缓存 + 清指纹库，然后重算一遍（不是"只清不建"）。"""
        self._write('a.jpg')
        self._sync()
        self.plugin.derived.clear()
        calls = []
        self.engine.spec.rebuild = lambda kind: (calls.append(kind), {'cleared': True})[1]

        out = self.engine.rebuild('derived')

        self.assertTrue(out['started'], out)
        self.assertEqual(calls, ['derived'])
        deadline = time.time() + 5
        while time.time() < deadline and self.engine.state()['busy']:
            time.sleep(0.02)
        self.assertFalse(self.engine.state()['busy'])
        self.assertIn('a.jpg', self._derive_keys(), '重建后必须重算，而不是停在空指纹库')

    def test_new_empty_directory_triggers_on_dirs_changed(self):
        """目录集合变化要有独立信号：新建空目录没有任何条目变化。

        `derive` / `prune` 都只对条目回调，而按目录聚合的缓存（相册树之类）必须
        在"多了一个目录"时作废 —— 没有一个条目进来，两个钩子都不会被调用。
        """
        calls = []

        def _dirs(report):
            calls.append((list(report['new_dirs']), list(report['vanished_dirs'])))

        self.plugin._spec = _spec(self.plugin, self.root, on_dirs_changed=_dirs)
        self.plugin._freshness_engine = None
        engine = _require_engine(self.plugin)
        self._write('a.jpg')
        engine.sync('', force=True)          # 冷启动：根目录本身也是"新目录"
        self.assertEqual(calls, [([''], [])])
        calls.clear()
        self.plugin.derived.clear()

        (self.root / 'empty').mkdir()
        report = engine.sync('', force=True)

        self.assertEqual(calls, [(['empty'], [])], '空目录也要触发目录集合信号')
        self.assertEqual(report['added'], 0, '空目录没有条目变化')
        self.assertEqual(self.plugin.derived, [], '条目级钩子不该被调用')
        self.assertEqual(report['dirs_changed'], {})

        calls.clear()
        engine.sync('', force=True)
        self.assertEqual(calls, [], '稳态不该重复触发')

    def test_vanished_directory_triggers_on_dirs_changed(self):
        """整个目录被移走：条目级 prune 与目录级信号都要出现。"""
        calls = []
        self._write('sub/a.jpg')
        self.plugin._spec = _spec(
            self.plugin, self.root,
            on_dirs_changed=lambda report: calls.append(
                (list(report['new_dirs']), list(report['vanished_dirs']))))
        self.plugin._freshness_engine = None
        engine = _require_engine(self.plugin)
        engine.sync('', force=True)
        calls.clear()
        self.plugin.pruned.clear()

        (self.root / 'sub' / 'a.jpg').unlink()
        (self.root / 'sub').rmdir()
        report = engine.sync('', force=True)

        self.assertEqual(calls, [([], ['sub'])])
        self.assertEqual(self.plugin.pruned, ['sub/a.jpg'])
        self.assertEqual(report['removed'], 1)

    def test_start_verify_reports_not_started_while_a_sync_runs(self):
        """同步与校验互斥：同步正在改指纹库与派生缓存时不再起一趟全量校验。

        `running` 必须是 False：壳的刷新控件按它判断"任务是不是已经起来了"。报成
        True 会让它等这一趟同步结束、再按"校验完成"收尾 —— 用户点了一次校验，实际
        什么都没校验，界面却提示成功。
        """
        engine = self._fresh_engine()
        self._write('a.jpg')
        seen = []

        def derive(items):
            seen.append(engine.start_verify())

        engine.spec.derive = derive
        self._sync()

        self.assertFalse(seen[0]['started'], seen)
        self.assertEqual(seen[0]['reason'], 'busy')
        self.assertFalse(seen[0]['running'], seen)
        self.assertTrue(seen[0]['sync_active'], seen)

        started = engine.start_verify()
        self.assertTrue(started['started'], started)
        deadline = time.time() + 5
        while time.time() < deadline and engine.state()['busy']:
            time.sleep(0.02)
        self.assertFalse(engine.state()['busy'])

    def test_state_does_not_create_database(self):
        fresh = _FakePlugin(Path(self._tmp.name) / '.cache' / 'untouched',
                            None)
        fresh._spec = _spec(fresh, self.root)
        engine = engine_for(fresh)
        db = engine.store.db_path

        state = engine.state()

        self.assertFalse(db.exists(), '看一眼状态不该建库')
        self.assertFalse(state['busy'])
        self.assertEqual(state['entries'], 0)

    # ----- 报告给插件的作用域（A8） -----

    def test_report_carries_scope_before_plugin_hooks_run(self):
        """`scope` / `scoped` 在任何插件钩子之前就写进报告。

        media-player 的 `on_pass_end` 用它判断"这一趟是否覆盖了全部根"：子目录作用域
        的同步如果被当成整趟，插件会把一份不完整的索引宣布成权威。
        """
        seen: list = []
        plugin = _FakePlugin(Path(self._tmp.name) / '.cache' / 'pass-end-scope', None)
        plugin._spec = _spec(plugin, self.root,
                             on_pass_end=lambda report, verified: (
                                 seen.append((report.get('scope'),
                                              report.get('scoped'), verified)), {})[1])
        self._write('sub/a.jpg')
        self._write('other/b.jpg')
        engine = _require_engine(plugin)

        engine.sync('', force=True)
        self.assertEqual(seen[-1], ('', False, False), '全根同步不是子目录作用域')

        engine.sync('sub', force=True)
        self.assertEqual(seen[-1], ('sub', True, False), '子目录同步要标 scoped=True')

        engine.verify()
        self.assertEqual(seen[-1], ('', False, True), '校验永远是整趟')

    # ----- rebuild 的单飞与 meta 章（A5） -----

    def test_rebuild_holds_single_flight_and_does_not_prestamp(self):
        """rebuild 全程占单飞：钩子/清库期间的同步被拒；校验没起来时不预写 meta 章。"""
        engine = self._fresh_engine()
        self._write('a.jpg')
        self._sync()
        refused = []

        def rebuild_hook(kind):
            refused.append(engine.sync('', force=True))
            return {'cleared': True}

        engine.spec.rebuild = rebuild_hook
        out = engine.rebuild('derived')

        self.assertTrue(out['started'], out)
        self.assertEqual(refused[0]['reason'], 'busy', refused)
        deadline = time.time() + 5
        while time.time() < deadline and engine.state()['busy']:
            time.sleep(0.02)
        self.assertIn('a.jpg', self._derive_keys(), '清库后必须重算回来')
        self.assertFalse(engine.state()['version_stale'])

        # 校验没能起来时：库是空的，就必须仍显示"待校验"，不能预先盖章
        engine.spec.rebuild = lambda kind: {'cleared': True}
        with mock.patch.object(engine, 'start_verify',
                               return_value={'started': False, 'running': True}):
            failed = engine.rebuild('derived')
        self.assertFalse(failed['started'], failed)
        state = engine.state()
        self.assertEqual(state['entries'], 0)
        self.assertTrue(state['version_stale'], '清库后不能显示"不待校验"')
        # 单飞已释放：下一趟同步/校验还能跑
        self.assertEqual(engine.sync('', force=True)['action'], 'sync')
        self.assertEqual(engine.verify()['action'], 'verify')

    # ----- 去抖窗口只被真扫消耗（A6①） -----

    def test_bad_scope_does_not_consume_the_throttle_window(self):
        """被拒的同步（以及引擎自身失败）不该让下一次合法同步落进节流窗口。"""
        engine = self._fresh_engine()
        self._write('a.jpg')
        engine.spec.min_sync_interval = 30.0

        self.assertEqual(engine.sync('../outside')['reason'], 'bad_scope')
        self.assertEqual(engine.sync('')['action'], 'sync', '刚才没真扫，不该被节流')
        self.assertEqual(engine.sync('')['reason'], 'throttled', '真扫之后才进入窗口')

        engine.reset_throttle()
        with mock.patch.object(engine.store, 'connect',
                               side_effect=sqlite3.OperationalError('locked')):
            failed = engine.sync('')
        self.assertEqual(failed['reason'], 'error', failed)
        self.assertEqual(engine.sync('')['action'], 'sync', '失败的一趟也没真扫')

    # ----- 单文件 stat 失败不是"文件消失"（A6②） -----

    def test_failed_file_stat_keeps_the_entry_and_the_directory_short_circuits(self):
        """stat 失败不判成消失（不 prune），指纹的名字数用枚举到的个数而不是成功数。"""
        engine = self._fresh_engine()
        self._write('a.jpg')
        self._write('b.jpg')
        self._sync()
        self.plugin.pruned.clear()
        engine.spec.content_version = 2      # 强制这一趟重算该目录
        real_stat = Path.stat

        def fake_stat(path_self, *args, **kwargs):
            if Path(path_self).name == 'b.jpg':
                raise PermissionError('locked')
            return real_stat(path_self, *args, **kwargs)

        with mock.patch('pathlib.Path.stat', fake_stat):
            report = engine.sync('', force=True)
            self.assertTrue(report['errors'], report)
            self.assertEqual(report['removed'], 0, '读不到 ≠ 没了')
            self.assertEqual(self.plugin.pruned, [])
            conn = engine.store.connect()
            try:
                self.assertIn('b.jpg', engine.store.entries_of(conn, ''),
                              '读不到的文件要沿用旧指纹')
                self.assertEqual(engine.store.dirs(conn)[''][1], 2,
                                 '指纹的名字数用枚举到的名字数，不用 stat 成功数')
            finally:
                conn.close()
            steady = engine.sync('', force=True)
            self.assertEqual(steady['skipped_dirs'], steady['dirs'], steady)
            self.assertEqual(steady['errors'], [])

    # ----- 插件键本身以别根前缀开头（A6③） -----

    def test_prefix_column_keeps_plugin_keys_that_look_like_root_prefixes(self):
        """首根的插件键恰好以另一根的前缀开头时，不能被剥掉那一段。"""
        extra = Path(self._tmp.name) / 'extra'
        extra.mkdir()
        (extra / 'b.jpg').write_bytes(b'x' * 8)
        (self.root / 'files').mkdir()
        (self.root / 'files' / 'a.jpg').write_bytes(b'x' * 8)

        def _key(abs_path: str) -> str:
            path = Path(abs_path)
            if path.parent.name == 'files':
                # 插件键恰好以另一个根的前缀开头（外观与"根内相对路径"同形）
                return f'__extra/{path.name}'
            return path.name

        self.plugin._spec = _spec(self.plugin, self.root,
                                  roots=lambda: [self.root, extra],
                                  prefixes=('', '__extra'), key_of=_key)
        self.plugin._freshness_engine = None
        engine = _require_engine(self.plugin)

        report = engine.sync('', force=True)
        self.assertEqual(report['added'], 2, report)
        self.assertEqual(sorted(self._derive_keys()), ['__extra/a.jpg', 'b.jpg'])

        engine.verify()
        self.assertEqual(self.plugin.audits[-1], {'__extra/a.jpg', 'b.jpg'})

        self.plugin.pruned.clear()
        (self.root / 'files' / 'a.jpg').unlink()
        os.utime(self.root / 'files', (time.time(), time.time()))
        engine.sync('', force=True)
        self.assertEqual(self.plugin.pruned, ['__extra/a.jpg'], 'prune 收到的也是插件键')

    # ----- 报告里的目录列表截断（A6④） -----

    def test_directory_lists_are_truncated_in_the_report(self):
        """报告要走 HTTP：new_dirs / vanished_dirs 截断到 50，其余只给计数。"""
        self._fresh_engine()
        for index in range(60):
            self._write(f'd{index:02d}/a.jpg')

        first = self._sync()
        self.assertEqual(len(first['new_dirs']), 50, first['new_dirs'])
        self.assertEqual(first['new_dirs_truncated'], 11, '根 + 60 个子目录，共 61 个新目录')

        for index in range(60):
            (self.root / f'd{index:02d}' / 'a.jpg').unlink()
            (self.root / f'd{index:02d}').rmdir()
        os.utime(self.root, (time.time(), time.time()))
        second = self._sync()
        self.assertEqual(len(second['vanished_dirs']), 50)
        self.assertEqual(second['vanished_dirs_truncated'], 10)

    # ----- 远端跳过的报告（A6⑤） -----

    def test_remote_fresh_skip_updates_last_report(self):
        engine = self._remote_engine(lambda: {'rows': 1})
        self.assertEqual(engine.sync()['action'], 'sync')
        skipped = engine.sync()
        self.assertEqual(skipped['reason'], 'fresh')
        self.assertEqual(engine.state()['last_report']['reason'], 'fresh')

    # ----- track_entries=False：只维护目录指纹（A7） -----

    def _track_engine(self):
        calls: dict = {'derive': [], 'prune': [], 'verified': [], 'dirs': []}
        plugin = _FakePlugin(Path(self._tmp.name) / '.cache' / 'track', None)
        plugin._spec = _spec(
            plugin, self.root,
            track_entries=False,
            derive=lambda items: calls['derive'].append(list(items)),
            prune=lambda keys: calls['prune'].append(list(keys)),
            on_verified=lambda keys: calls['verified'].append(set(keys)),
            on_dirs_changed=lambda report: calls['dirs'].append(report))
        return _require_engine(plugin), calls

    def test_track_entries_false_only_keeps_directory_fingerprints(self):
        """只维护目录指纹：不写 entries、不调条目级钩子，目录变化仍能被发现。"""
        engine, calls = self._track_engine()
        self.assertFalse(engine.spec.track_entries)
        self._write('a.jpg')

        first = engine.sync('', force=True)
        self.assertEqual(first['added'], 0)
        self.assertEqual(first['dirs'], 1)
        self.assertEqual(len(calls['dirs']), 1, '冷启动的根目录也是新目录')
        self.assertEqual(list(calls['dirs'][0]['new_dirs']), [''])
        calls['dirs'].clear()
        conn = engine.store.connect()
        try:
            self.assertEqual(engine.store.entry_keys(conn), set(), '不写 entries 行')
            self.assertEqual(engine.store.dirs(conn)[''][1], 1)
        finally:
            conn.close()
        self.assertEqual(engine.valid_keys(), set())
        state = engine.state()
        self.assertEqual(state['entries'], 0)
        self.assertEqual(state['dirs'], 1)

        # 目录内容变化仍被发现（名字数从 1 变 2），但不产生条目事件
        self._write('b.jpg')
        changed = engine.sync('', force=True)
        self.assertEqual(changed['skipped_dirs'], 0, '目录变了就不能短路')
        conn = engine.store.connect()
        try:
            self.assertEqual(engine.store.dirs(conn)[''][1], 2, '目录指纹要更新')
            self.assertEqual(engine.store.entry_keys(conn), set())
        finally:
            conn.close()
        self.assertEqual(calls['derive'], [])
        self.assertEqual(calls['prune'], [])
        self.assertEqual(calls['verified'], [])

        steady = engine.sync('', force=True)
        self.assertEqual(steady['skipped_dirs'], steady['dirs'])
        self.assertEqual(calls['dirs'], [], '稳态不重复触发目录集合信号')

        # 新建子目录 → 目录集合信号；校验不调 on_verified
        (self.root / 'sub').mkdir()
        engine.sync('', force=True)
        self.assertEqual(len(calls['dirs']), 1)
        self.assertEqual(list(calls['dirs'][0]['new_dirs']), ['sub'])

        report = engine.verify()
        self.assertNotIn('audited', report)
        self.assertEqual(calls['verified'], [], '空键集不能交给 on_verified')
        self.assertIsNone(engine.verified_keys(), '没有条目就没有权威键集合')
        self.assertEqual(engine.state()['entries'], 0)


class FreshnessSpecTests(unittest.TestCase):
    def test_normalize_rejects_bad_declarations(self):
        plugin = _FakePlugin(Path('.'), None)
        self.assertIsNone(FreshnessSpec.normalize(plugin, None))
        self.assertIsNone(FreshnessSpec.normalize(plugin, {}))
        with self.assertRaises(ValueError):
            FreshnessSpec.normalize(plugin, {'roots': ['.'], 'derive': 'not-callable'})
        with self.assertRaises(ValueError):
            FreshnessSpec.normalize(plugin, {'roots': ['.'], 'prefixes': ['a', 'b']})

    def test_numeric_limits_keep_an_explicit_zero_and_default_on_garbage(self):
        """数值上限的两种边界：显式 0 是有效声明，无法解析的值退回默认。

        `max_dirs_per_sync: 0` 表示"被动同步不处理任何目录"（全部交给校验），
        与没声明这个键（用默认预算）是两回事，不能被默认值顶掉。
        """
        plugin = _FakePlugin(Path('.'), None)
        zero = FreshnessSpec.normalize(plugin, {
            'roots': ['.'],
            'max_dirs_per_sync': 0,
            'min_sync_interval': 0,
            'ttl_seconds': 0,
        })
        assert zero is not None
        self.assertEqual(zero.max_dirs_per_sync, 0)
        self.assertEqual(zero.min_sync_interval, 0.0)
        self.assertEqual(zero.ttl_seconds, 0.0)

        valid = FreshnessSpec.normalize(plugin, {
            'roots': ['.'],
            'max_dirs_per_sync': 7,
            'min_sync_interval': 2.5,
            'ttl_seconds': 60,
        })
        assert valid is not None
        self.assertEqual(valid.max_dirs_per_sync, 7)
        self.assertEqual(valid.min_sync_interval, 2.5)
        self.assertEqual(valid.ttl_seconds, 60.0)

        garbage = FreshnessSpec.normalize(plugin, {
            'roots': ['.'],
            'max_dirs_per_sync': 'many',
            'min_sync_interval': 'soon',
            'ttl_seconds': 'later',
        })
        assert garbage is not None
        self.assertEqual(garbage.max_dirs_per_sync, DEFAULT_MAX_DIRS)
        self.assertEqual(garbage.min_sync_interval, DEFAULT_MIN_SYNC_INTERVAL)
        self.assertEqual(garbage.ttl_seconds, 0.0)

    def test_empty_roots_still_yield_an_engine(self):
        """根为空 = "已接入、当前没有受管目录"，不是"不参与"。

        根往往由运行时配置（group-mesh 默认没有任何共享目录）；把它当"不支持"，
        界面就只能显示"本插件未参与统一刷新"，而正确状态是空索引。
        """
        with tempfile.TemporaryDirectory() as td:
            # 缓存目录必须落在临时目录：给 `.` 会让指纹库写进当前工作目录
            plugin = _FakePlugin(Path(td) / '.cache', None)
            plugin._spec = _spec(plugin, Path(td), roots=list)

            engine = engine_for(plugin)

            self.assertIsNotNone(engine)
            report = engine.sync('', force=True)
            self.assertEqual(report['dirs'], 0)
            self.assertEqual(report['added'], 0)
            self.assertTrue(engine.state()['available'])

    def test_engine_for_requires_a_spec(self):
        plugin = _FakePlugin(Path('.'), None)
        self.assertIsNone(engine_for(plugin))

    def test_engine_is_cached_on_the_plugin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'media'
            root.mkdir()
            plugin = _FakePlugin(Path(td) / '.cache', None)
            plugin._spec = _spec(plugin, root)
            first = engine_for(plugin)
            self.assertIs(first, engine_for(plugin))
            self.assertIn('fake-plugin', [e.name for e in all_engines()])

    def test_source_wants_and_skips(self):
        source = FsSource(roots=(Path('/tmp'),), include=('.jpg',))
        self.assertTrue(source.wants('a.JPG'))
        self.assertFalse(source.wants('a.png'))
        self.assertFalse(source.wants('.hidden.jpg'))
        self.assertTrue(source.skips('.cache'))
        self.assertTrue(source.skips('.git'))
        self.assertFalse(source.skips('normal'))


class FreshnessStoreTests(unittest.TestCase):
    def test_replace_and_drop_dir_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            store = FreshnessStore(Path(td) / 's.db')
            conn = store.connect()
            try:
                store.replace_dir(conn, 'd', (1.0, 2, 20, 9.0),
                                  [('d/a.jpg', 1.0, 8), ('d/b.jpg', 2.0, 12)])
                self.assertEqual(store.entry_keys(conn), {'d/a.jpg', 'd/b.jpg'})
                self.assertEqual(store.counts(conn), {'entries': 2, 'dirs': 1})

                # 整目录替换：旧条目不能残留
                store.replace_dir(conn, 'd', (2.0, 1, 8, 3.0), [('d/c.jpg', 3.0, 8)])
                self.assertEqual(store.entry_keys(conn), {'d/c.jpg'})

                removed = store.drop_dir(conn, 'd')
                self.assertEqual(removed, ['d/c.jpg'])
                self.assertEqual(store.counts(conn), {'entries': 0, 'dirs': 0})
            finally:
                conn.close()

    def test_old_database_gains_new_columns_without_losing_fingerprints(self):
        """旧库迁移：就地 `ALTER TABLE` 补 `version` / `prefix` 列，已有指纹与条目都保留。

        为什么不能删库重建：旧库里是整库的目录指纹，删了会让升级后的第一趟把每个
        目录都当新目录（派生数据全量重算、相册树重建）。补列后旧行取版本 0、前缀
        空串（等价于"第一根 / 还没按任何 content_version 盖过章"），升级后各被重算一次。
        """
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'old.db'
            conn = sqlite3.connect(str(db))
            try:
                conn.executescript('''
CREATE TABLE dirs (
    key TEXT PRIMARY KEY, dir_mtime REAL NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0, size_sum INTEGER NOT NULL DEFAULT 0,
    max_mtime REAL NOT NULL DEFAULT 0
);
CREATE TABLE entries (
    key TEXT PRIMARY KEY, dir_key TEXT NOT NULL,
    mtime REAL NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0
);
''')
                conn.execute('INSERT INTO dirs(key, dir_mtime, file_count, size_sum, max_mtime)'
                             ' VALUES (?,?,?,?,?)', ('d', 1.0, 1, 8, 1.0))
                conn.execute('INSERT INTO entries(key, dir_key, mtime, size) VALUES (?,?,?,?)',
                             ('d/a.jpg', 'd', 1.0, 8))
                conn.commit()
            finally:
                conn.close()

            store = FreshnessStore(db)
            conn = store.connect()
            try:
                self.assertEqual(store.dirs(conn)['d'], (1.0, 1, 8, 1.0, 0, ''),
                                 '旧行按版本 0、空前缀读回，指纹不丢')
                self.assertEqual(store.entry_keys(conn), {'d/a.jpg'})
                self.assertEqual(store.entry_prefix_pairs(conn), [('d/a.jpg', '')])
                self.assertEqual(store.stale_dirs(conn, 1), 1, '旧行算"还没盖章"')

                store.replace_dir(conn, 'd', (2.0, 1, 8, 2.0), [('d/a.jpg', 2.0, 8)],
                                  version=1, prefix='__extra')
                self.assertEqual(store.dirs(conn)['d'], (2.0, 1, 8, 2.0, 1, '__extra'))
                self.assertEqual(store.entry_prefix_pairs(conn), [('d/a.jpg', '__extra')])
                self.assertEqual(store.stale_dirs(conn, 1), 0)
            finally:
                conn.close()

            # 二次建连不再 ALTER（幂等）
            conn = store.connect()
            try:
                self.assertEqual(store.dirs(conn)['d'][4:], (1, '__extra'))
            finally:
                conn.close()


class FreshnessApiTests(unittest.TestCase):
    """壳级端点走真实通道（PluginManager + /api）的端到端用例。

    为什么需要：其余用例只验引擎本体，`freshness_api_methods()` 这张表与
    `api_proxy` 的分发没有任何覆盖 —— 而这两处的错误形态都是"点了没反应"
    （方法名写错、参数顺序写反、表漏一项），单测引擎是发现不了的。
    """

    PLUGIN = '''
from pathlib import Path

from shell.backend.plugin_base import PluginBase


class Plugin(PluginBase):
    def register_api(self):
        return {}

    def get_data_root(self):
        return Path(self.config['directories']['data_root'])

    def freshness_spec(self):
        return {
            'roots': lambda: [self.get_data_root()],
            'include': ('.jpg',),
            'derive': lambda items: {'count': len(items)},
            'prune': lambda keys: len(keys),
            'content_version': 1,
            'unit': '张',
            'min_sync_interval': 0.0,
        }
'''

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        plugins_dir = tmp / 'plugins'
        plugins_dir.mkdir()
        self.data_root = tmp / 'data'
        self.data_root.mkdir()
        plugin_dir = plugins_dir / 'fresh-probe'
        (plugin_dir / 'backend').mkdir(parents=True)
        (plugin_dir / 'manifest.json').write_text(json.dumps({
            'name': 'fresh-probe',
            'version': '1.0.0',
            'displayName': 'fresh-probe',
            'dependencies': [],
            'backend': {'entry': 'backend/main.py', 'class': 'Plugin'},
            'frontend': {'entry': 'frontend/index.html', 'route': '/fresh-probe'},
        }, ensure_ascii=False), encoding='utf-8')
        (plugin_dir / 'backend' / 'main.py').write_text(self.PLUGIN, encoding='utf-8')

        config = {'server': {'host': '127.0.0.1', 'port': 18080},
                  'directories': {'data_root': str(self.data_root)}}
        self.manager = PluginManager([str(plugins_dir)], config=config)
        self.manager.load_all()
        self.assertIn('fresh-probe', self.manager.get_plugin_status()['loaded'])
        with mock.patch('shell.backend.file_server.get_config_dir',
                        return_value=tmp / '.config'):
            app = create_app(config, self.manager)
        self.client = app.test_client()
        self.headers = {TOKEN_HEADER: get_or_create_token(tmp / '.config')}

    def _call(self, method: str, *args):
        resp = self.client.post(f'/api/{method}', headers=self.headers,
                                json={'args': list(args)})
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()['result']

    def _verify_and_wait(self, timeout=10.0):
        """起一次全量校验并等到结束；顺带断言任务本身没有失败。

        任务失败（例如 worker 签名不对）时它照样会走到 done，只看 busy 会漏掉 ——
        这里显式校验 success 与 errors，正是为了让那类缺陷在测试里现形。
        """
        started = self._call('system_freshness_verify', 'fresh-probe')
        self.assertTrue(started.get('started'), started)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._call('system_freshness_state', 'fresh-probe')
            if not state.get('busy'):
                task = state.get('task') or {}
                self.assertFalse(task.get('errors'), f'校验任务失败: {task.get("errors")}')
                self.assertTrue(task.get('success'), task)
                return state
            time.sleep(0.05)
        self.fail(f'校验超时未结束: {state}')
        return None

    def test_sync_state_verify_over_real_api(self):
        (self.data_root / 'a.jpg').write_bytes(b'x' * 8)
        (self.data_root / 'b.jpg').write_bytes(b'x' * 8)

        synced = self._call('system_freshness_sync', 'fresh-probe', '')
        self.assertEqual(synced['action'], 'sync')
        self.assertEqual(synced['added'], 2)

        state = self._call('system_freshness_state', 'fresh-probe')
        self.assertTrue(state['available'])
        self.assertEqual(state['entries'], 2)
        self.assertEqual(state['unit'], '张')
        self.assertFalse(state['version_stale'])

        after = self._verify_and_wait()
        self.assertFalse(after['busy'])
        self.assertEqual(after['entries'], 2)

        # 删掉一个文件：全量校验必须把幽灵条目清掉（现状里 media-player 的
        # 增量扫描永远做不到这件事）
        (self.data_root / 'b.jpg').unlink()
        after = self._verify_and_wait()
        self.assertEqual(after['entries'], 1)

        overview = self._call('system_freshness_overview')
        self.assertEqual([item['plugin'] for item in overview], ['fresh-probe'])

    def test_unknown_plugin_reports_unsupported(self):
        state = self._call('system_freshness_state', 'no-such-plugin')
        self.assertTrue(state['available'] is False)
        self.assertEqual(self._call('system_freshness_sync', 'no-such-plugin', '')['reason'],
                         'unsupported')
        self.assertFalse(self._call('system_freshness_verify', 'no-such-plugin')['started'])

    def test_scope_escape_is_rejected(self):
        (self.data_root / 'a.jpg').write_bytes(b'x' * 8)
        report = self._call('system_freshness_sync', 'fresh-probe', '../../etc')
        self.assertEqual(report['reason'], 'bad_scope')


if __name__ == '__main__':
    unittest.main()
