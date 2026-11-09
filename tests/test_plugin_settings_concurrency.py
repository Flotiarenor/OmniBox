"""`update_setting` 与 `save_settings` 并发写同一份设置时不得互相覆盖。

审计项 A1（P0）：`update_setting` 曾是 `get()` → 改 → `set()` 两段写。它与
`save_settings`（前端的设置弹窗保存、插件运行期的状态回写）并发时，后写者会把
对方刚写的键整段覆盖掉 —— 现象是"运行期状态偶发丢失"，不报错、也没有日志。
两条路径现在都走 `SettingsStore.update()`（同一把插件锁内的读-改-写），本文件把
这条链路的并发语义与失败路径固定下来。

为什么用真实的 `SettingsStore` 而不是替身：要验收的正是"那把锁覆盖了整段读-改-写"，
把锁换成 mock 之后，结论就只与 mock 的写法有关，不再约束真实实现。这里只在真实存储
上把"读完自己改"的那一步放慢（见 `_SlowMutateStore`），以便必然命中竞态窗口 ——
锁、原子写、JSON 落盘全都还是真实实现。

运行：
    python -m unittest tests.test_plugin_settings_concurrency -v
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_base import PluginBase
from shell.backend.settings_store import SettingsStore


class _StubPlugin(PluginBase):
    """最小宿主：声明两个可写设置项，另有一个不来自 schema 的运行期状态键。"""

    settings_schema: ClassVar[List[Dict]] = [
        {'key': 'per_page', 'label': '每页数量', 'type': 'number', 'default': 40},
        {'key': 'sort_by', 'label': '排序方式', 'type': 'text', 'default': 'name'},
    ]

    def register_api(self):   # pragma: no cover - 用例不经过 API 注册
        return {}


class _FailingStore(SettingsStore):
    """`update()` 必失败的存储：用来验收失败路径留了日志，而不是静默返回 False。"""

    def update(self, plugin_name: str, values: Dict) -> Dict[str, Any]:
        raise OSError('设置目录只读（用例注入）')


class _SlowSetItemDict(dict):
    """逐键赋值时变慢的字典：把"读-改-写没有同一把锁"的窗口放大到必然命中。"""

    def __setitem__(self, key, value):
        time.sleep(0.005)
        super().__setitem__(key, value)


class _SlowMutateStore(SettingsStore):
    """`get()` 返回的字典在**调用方自己改**时变慢。

    真正的两段写是"`get()` → 逐键赋值 → `set()`"：中间那一步是裸的 Python 赋值，
    窗口只有几条字节码，靠线程调度碰它只能概率命中（实测几十轮里才会丢一次，测不出
    稳定的红）。这里让那一步慢 5ms，于是只要实现退回两段写，另一个线程必然在这个
    窗口里写完，丢更新从"偶发"变成"必然"。

    `SettingsStore.update()` 内部用的是 `dict.update()`（C 快路径，不经过被重写的
    `__setitem__`），因此**正确实现**在这条用例里没有额外开销 —— 放大只作用于
    "拿 get() 的结果自己改"的写法，不会把正确实现一起拖慢。
    """

    def get(self, plugin_name: str):
        return _SlowSetItemDict(super().get(plugin_name))


class UpdateSettingConcurrencyTests(unittest.TestCase):
    """两个线程各自反复写：一个写运行期状态键，一个走设置保存。"""

    ROUNDS = 40

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _SlowMutateStore(str(Path(self._tmp.name) / 'plugins'))
        self.plugin = _StubPlugin(
            manifest={'name': 'stub'},
            config={'directories': {'data_root': self._tmp.name}})
        self.plugin._settings_store = self.store

    def _stored(self) -> Dict:
        # 用基类 get() 读回落盘内容：_SlowMutateStore 的 get() 只是把窗口放大，
        # 断言要看的是文件里的真实数据。
        return SettingsStore.get(self.store, 'stub')

    def test_concurrent_update_setting_and_save_settings_keep_every_key(self):
        errors: List[str] = []
        start = threading.Barrier(2)

        def write_runtime_state():
            try:
                start.wait()
                for i in range(self.ROUNDS):
                    if not self.plugin.update_setting(f'runtime_{i}', i):
                        errors.append(f'update_setting(runtime_{i}) 返回 False')
            except Exception as e:   # pragma: no cover - 失败时给出线索
                errors.append(f'update_setting 抛异常: {e!r}')

        def save_user_settings():
            try:
                start.wait()
                for i in range(self.ROUNDS):
                    result = self.plugin.save_settings(
                        {'per_page': i + 1, 'sort_by': f'v{i}'})
                    if not result.get('success'):
                        errors.append(f'save_settings 第 {i} 轮失败: {result}')
            except Exception as e:   # pragma: no cover - 失败时给出线索
                errors.append(f'save_settings 抛异常: {e!r}')

        threads = [threading.Thread(target=write_runtime_state),
                   threading.Thread(target=save_user_settings)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        stored = self._stored()
        missing = [f'runtime_{i}' for i in range(self.ROUNDS)
                   if stored.get(f'runtime_{i}') != i]
        self.assertEqual(missing, [], f'并发写入覆盖掉了运行期状态键: {missing}')
        # 用户设置的最后一个值必须留下：被运行期回写覆盖同样是丢更新
        self.assertEqual(stored.get('per_page'), self.ROUNDS)
        self.assertEqual(stored.get('sort_by'), f'v{self.ROUNDS - 1}')


class UpdateSettingFailureTests(unittest.TestCase):
    """失败路径不能静默：返回值仍是 False，但必须留下可查的日志。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plugin = _StubPlugin(
            manifest={'name': 'stub'},
            config={'directories': {'data_root': self._tmp.name}})

    def test_write_failure_returns_false_and_is_logged(self):
        self.plugin._settings_store = _FailingStore(str(Path(self._tmp.name) / 'plugins'))
        with self.assertLogs('shell.backend.plugin_base', level='WARNING') as captured:
            self.assertFalse(self.plugin.update_setting('runtime_state', 1))
        records = '\n'.join(captured.output)
        self.assertIn('runtime_state', records, '日志里应带上设置键')
        self.assertIn('设置目录只读', records, '日志里应带上失败原因')

    def test_missing_store_still_returns_false(self):
        """契约保持：没有设置存储时返回 False（不抛异常）。"""
        self.assertFalse(self.plugin.update_setting('runtime_state', 1))


if __name__ == '__main__':
    unittest.main()
