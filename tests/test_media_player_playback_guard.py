"""播放进度只允许被「校验结论」清空，不能被「索引暂缺」清空。

回归对象：`MediaCollections.get_playback` 用 `index.get(item_id) is None` 判定
「条目没了」，命中就 `playback = {}` 并立即落盘。索引在许多正常情形下本来就是空的
或不全的 —— 启动早期还没建索引、外接盘没挂载、首趟扫描预算用尽只跑了一部分 ——
一次读取就把用户的播放参数永久抹掉（落盘之后索引恢复也回不来）。

新判据：索引命中即视为存活；索引未命中时只信统一刷新基建的**整趟校验结论**
（`verified_keys()` 返回非 None 且 id 不在其中才清空）。没有结论时保守保留。

用例分两层：
  * 组件层：直接构造 `MediaCollections` + 替身 state/index，把四种输入
    （未注入 / 返回 None / 返回集合 / 抛异常）逐一钉住；
  * 装配层：真实插件 + 临时媒体根，确认入口注入的 provider 在「未校验」时返回
    None、索引清空后播放参数仍在，并确认拿到权威结论后才清空且**落盘**。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend'
if str(PLUGIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(PLUGIN_BACKEND))

from shell.backend.freshness import FreshnessEngine, engine_for
from shell.backend.plugin_utils import load_sibling

mp = load_sibling(str(PLUGIN_BACKEND / 'main.py'), 'main', 'media_player_playback_guard_test')
collections_module = load_sibling(
    str(PLUGIN_BACKEND / 'collections.py'), 'collections', 'media_player_collections_test')

ITEM_ID = '0123456789abcdef'


class _FakeState:
    """`UserStateStore` 的最小替身：只提供 collections 用到的 data / save。"""

    recent_limit = 50

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {'favorites': [], 'recent': [], 'playlists': [],
                                     'playback': {}}
        self.saves = 0

    def save(self) -> None:
        self.saves += 1


class _FakeIndex:
    """条目索引替身：只有显式登记过的 id 才算命中。"""

    def __init__(self, known: Optional[Set[str]] = None) -> None:
        self.known = set(known or ())

    def get(self, item_id: str) -> Any:
        return object() if item_id in self.known else None


def _collections(provider: Any = None) -> Any:
    state = _FakeState()
    index = _FakeIndex()
    made = collections_module.MediaCollections(state, index, 200, provider)
    return made, state, index


def _save_playback(collections: Any, item_id: str = ITEM_ID) -> None:
    collections.save_playback(item_id, 'one', True, 0.5, 'video', 3)


class PlaybackGuardComponentTests(unittest.TestCase):
    """组件层：判定只看注入的结论，不看索引是否命中。"""

    def test_missing_index_without_provider_keeps_playback(self):
        collections, state, _index = _collections()
        _save_playback(collections)
        before = state.saves

        playback = collections.get_playback()

        self.assertEqual(playback.get('item_id'), ITEM_ID)
        self.assertEqual(state.data['playback'].get('item_id'), ITEM_ID)
        self.assertEqual(state.saves, before, '保留路径不该落盘')

    def test_provider_returning_none_keeps_playback(self):
        collections, state, _index = _collections(lambda: None)
        _save_playback(collections)
        before = state.saves

        self.assertEqual(collections.get_playback().get('item_id'), ITEM_ID)
        self.assertEqual(state.saves, before)

    def test_provider_raising_keeps_playback(self):
        def broken() -> Optional[Set[str]]:
            raise RuntimeError('引擎还没接入')

        collections, state, _index = _collections(broken)
        _save_playback(collections)
        before = state.saves

        self.assertEqual(collections.get_playback().get('item_id'), ITEM_ID)
        self.assertEqual(state.saves, before)

    def test_verified_missing_item_clears_and_persists(self):
        collections, state, _index = _collections(lambda: set())
        _save_playback(collections)
        before = state.saves

        self.assertEqual(collections.get_playback(), {})
        self.assertEqual(state.data['playback'], {})
        self.assertEqual(state.saves, before + 1, '清空必须落盘')

    def test_verified_present_item_keeps_playback(self):
        collections, state, _index = _collections(lambda: {ITEM_ID})
        _save_playback(collections)
        before = state.saves

        self.assertEqual(collections.get_playback().get('item_id'), ITEM_ID)
        self.assertEqual(state.saves, before)

    def test_index_hit_never_consults_provider(self):
        calls: list = []

        def provider() -> Optional[Set[str]]:
            calls.append(1)
            return set()

        collections, _state, _index = _collections(provider)
        collections._index = _FakeIndex({ITEM_ID})      # 替身索引换成"命中"
        _save_playback(collections)

        self.assertEqual(collections.get_playback().get('item_id'), ITEM_ID)
        self.assertEqual(calls, [], '索引命中时不需要问校验结论')


class PlaybackGuardWiringTests(unittest.TestCase):
    """装配层：真实插件 + 临时媒体根，验证入口注入与落盘。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'media'
        self.root.mkdir(parents=True, exist_ok=True)
        mp.MediaPlayerPlugin._resolved_config = {'media_roots': str(self.root)}
        self.addCleanup(setattr, mp.MediaPlayerPlugin, '_resolved_config', {})
        self.plugin: Any = mp.MediaPlayerPlugin(
            {'name': 'media-player'},
            {'directories': {'data_root': str(Path(self._tmp.name) / 'shell-data')}})
        self.addCleanup(self.plugin.on_unload)
        engine = engine_for(self.plugin)
        self.assertIsNotNone(engine, 'media-player 必须声明 freshness_spec()')
        assert engine is not None
        engine.spec.min_sync_interval = 0.0
        #: 引擎在装配里是 Optional；用例内断言过非 None，这里收成 Any 便于直接调用
        self.engine: Any = engine

    def _scan_one_track(self) -> str:
        track = self.root / 'd0' / 'a.mp3'
        track.parent.mkdir(parents=True, exist_ok=True)
        track.write_bytes(b'ID3\x03\x00\x00\x00\x00\x00\x00')
        self.engine.sync('', force=True)
        items = list(self.plugin.index.items.values())
        self.assertEqual(len(items), 1, '临时媒体根里应恰好索引到 1 条')
        return items[0].id

    def _state_file_playback(self) -> Dict[str, Any]:
        with open(self.plugin.paths.state_file, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        return payload.get('playback') or {}

    def test_no_verify_conclusion_before_first_full_pass(self):
        """引擎的权威结论只在整趟校验成功后才有值；此前一律 None。"""
        item_id = self._scan_one_track()
        self.plugin.collections.save_playback(item_id, 'none', False, 1.0, 'video', 0)

        self.assertIsNone(self.plugin._verified_keys(),
                          '只同步过一次时还没有权威结论，播放状态不能据此清空')

    def test_index_gap_keeps_playback_on_disk(self):
        item_id = self._scan_one_track()
        self.plugin.collections.save_playback(item_id, 'one', True, 0.5, 'video', 3)
        self.assertEqual(self._state_file_playback().get('item_id'), item_id)

        # 模拟「索引暂缺」：外接盘未挂载 / 启动早期索引还没建
        self.plugin.index.publish({})

        playback = self.plugin.collections.get_playback()

        self.assertEqual(playback.get('item_id'), item_id,
                         '索引暂缺时不能清空播放参数')
        self.assertEqual(self._state_file_playback().get('item_id'), item_id,
                         '索引暂缺时不该把空 playback 写回状态文件')

    def test_verified_missing_item_clears_after_authoritative_pass(self):
        item_id = self._scan_one_track()
        self.plugin.collections.save_playback(item_id, 'one', True, 0.5, 'video', 3)
        self.plugin.index.publish({})

        # 整趟校验确认条目消失：这里用替身直接给出该结论，只验证 get_playback 的判据，
        # 引擎自己的结论来源由下一条端到端用例覆盖。
        self.plugin._verified_keys = lambda: set()

        self.assertEqual(self.plugin.collections.get_playback(), {})
        self.assertEqual(self._state_file_playback(), {},
                         '确认条目消失后必须把空 playback 落盘')

    def test_real_verify_clears_after_file_is_really_gone(self):
        """端到端：文件真被删掉、整趟校验跑完，才清空播放进度。"""
        self.assertTrue(hasattr(FreshnessEngine, 'verified_keys'),
                        'FreshnessEngine 必须提供 verified_keys：播放状态的清空判据依赖它')
        item_id = self._scan_one_track()
        self.plugin.collections.save_playback(item_id, 'one', True, 0.5, 'video', 3)
        (self.root / 'd0' / 'a.mp3').unlink()

        report = self.engine.verify()

        self.assertFalse(report.get('partial'), report)
        self.assertEqual(report.get('errors'), [], report)
        self.assertNotIn(item_id, self.plugin.index.items)
        self.assertIsNotNone(self.plugin._verified_keys(),
                             '整趟校验成功后结论必须非 None，否则清空是"碰巧"而不是"判定"')
        self.assertEqual(self.plugin.collections.get_playback(), {})
        self.assertEqual(self._state_file_playback(), {})


if __name__ == '__main__':      # pragma: no cover
    unittest.main()
