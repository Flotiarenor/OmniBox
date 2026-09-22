"""SettingsStore 的并发与原子性回归测试。

历史问题：set() 用 open('w') 直接覆写（中断即损坏）、update() 读-改-写无锁
（并发互相覆盖）、get() 把损坏文件静默当成"没有设置"（用户改动悄悄失效且
无任何提示）。这里逐条固化为断言。

运行：
    python -m unittest tests.test_settings_store -v
"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.store = SettingsStore(str(self.dir))

    def _tmp_files(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name.endswith('.tmp'))

    def test_roundtrip(self):
        self.store.set('demo', {'a': 1, '中文': '值'})
        self.assertEqual(self.store.get('demo'), {'a': 1, '中文': '值'})

    def test_missing_file_returns_empty(self):
        self.assertEqual(self.store.get('nope'), {})

    def test_no_temp_files_left_behind(self):
        for i in range(5):
            self.store.set('demo', {'n': i})
        self.assertEqual(self._tmp_files(), [])

    def test_failed_write_keeps_previous_content(self):
        """写入过程中断：旧内容必须完好，且不留临时文件（原子落盘的要点）。"""
        self.store.set('demo', {'a': 1})
        with mock.patch('json.dump', side_effect=RuntimeError('boom')), self.assertRaises(RuntimeError):
            self.store.set('demo', {'a': 2})
        self.assertEqual(self.store.get('demo'), {'a': 1})
        self.assertEqual(self._tmp_files(), [])

    def test_update_merges(self):
        self.store.set('demo', {'a': 1})
        self.store.update('demo', {'b': 2})
        self.assertEqual(self.store.get('demo'), {'a': 1, 'b': 2})

    def test_concurrent_update_does_not_lose_keys(self):
        """N 个线程各写各自的键，全部键都必须留下（读-改-写必须串行）。"""
        rounds = 30
        threads = []
        for i in range(rounds):
            t = threading.Thread(target=self.store.update, args=('demo', {f'k{i}': i}))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = self.store.get('demo')
        missing = [f'k{i}' for i in range(rounds) if f'k{i}' not in data]
        self.assertEqual(missing, [], '并发 update 覆盖掉了别的线程写入的键')

    def test_concurrent_write_across_plugins_is_isolated(self):
        errors = []

        def writer(name):
            try:
                for i in range(10):
                    self.store.update(name, {f'k{i}': i})
            except Exception as e:  # pragma: no cover - 失败时给出线索
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f'p{i}',)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for i in range(4):
            self.assertEqual(len(self.store.get(f'p{i}')), 10)

    def test_corrupt_file_is_quarantined_not_silently_ignored(self):
        bad = self.dir / 'demo.json'
        bad.write_text('{ 这不是合法 JSON', encoding='utf-8')
        self.assertEqual(self.store.get('demo'), {})
        backups = sorted(p.name for p in self.dir.iterdir() if '.corrupt-' in p.name)
        self.assertEqual(len(backups), 1, '损坏文件应改名保底，便于人工恢复')
        # 原始内容必须完整保留在备份里
        self.assertIn('这不是合法 JSON', (self.dir / backups[0]).read_text(encoding='utf-8'))
        # 之后再读不会重复隔离
        self.assertEqual(self.store.get('demo'), {})
        self.assertEqual(len([p for p in self.dir.iterdir() if '.corrupt-' in p.name]), 1)

    def test_non_object_json_is_quarantined(self):
        (self.dir / 'demo.json').write_text('[1, 2, 3]', encoding='utf-8')
        self.assertEqual(self.store.get('demo'), {})
        self.assertEqual(len([p for p in self.dir.iterdir() if '.corrupt-' in p.name]), 1)

    def test_clear_is_safe_and_removes_file(self):
        self.store.set('demo', {'a': 1})
        self.store.clear('demo')
        self.assertEqual(self.store.get('demo'), {})
        self.store.clear('demo')  # 再清一次不应报错

    def test_set_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            self.store.set('demo', ['not', 'a', 'dict'])  # type: ignore[arg-type]

    def test_file_is_utf8_readable_json_object(self):
        self.store.set('demo', {'名字': '值'})
        raw = (self.dir / 'demo.json').read_text(encoding='utf-8')
        self.assertEqual(json.loads(raw), {'名字': '值'})

    # ---------- 文件名安全（settings_store._file）----------

    def test_rejects_plugin_name_that_escapes_settings_dir(self):
        """plugin_name 直接拼文件名，必须拒绝一切能越出配置目录的取值。

        越界的后果是任意 JSON 写入（例如 Linux 的 ~/.config/autostart/、
        Windows 的启动目录）——这是一条真实的写入原语，不只是"参数没校验"。
        """
        outside = self.dir.parent / 'omnibox-escape-probe'
        for bad in (
            '../omnibox-escape-probe',
            '..\\omnibox-escape-probe',
            'a/b',
            'a\\b',
            '..',
            '.',
            '',
            '   ',
            str(outside),
        ):
            with self.subTest(name=bad), self.assertRaises((ValueError, TypeError)):
                self.store.set(bad, {'x': 1})
        self.assertFalse(outside.exists(), '越界目录不应被创建')
        self.assertEqual([p.name for p in self.dir.iterdir()], [])

    def test_get_and_update_also_reject_bad_names(self):
        for bad in ('../evil', 'a/b', ''):
            with self.subTest(name=bad):
                with self.assertRaises((ValueError, TypeError)):
                    self.store.get(bad)
                with self.assertRaises((ValueError, TypeError)):
                    self.store.update(bad, {'x': 1})
                with self.assertRaises((ValueError, TypeError)):
                    self.store.clear(bad)

    def test_rejects_non_string_plugin_name(self):
        with self.assertRaises(TypeError):
            self.store.set(123, {'x': 1})  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.set(None, {'x': 1})  # type: ignore[arg-type]

    def test_accepts_normal_names(self):
        for good in ('demo', 'image-viewer', 'pixiv_sync', 'a.b', '插件名'):
            with self.subTest(name=good):
                self.store.set(good, {'x': 1})
                self.assertEqual(self.store.get(good), {'x': 1})

    def test_update_returns_merged_state(self):
        """update() 要返回合并后的完整设置（调用方据此判断变更）。"""
        self.store.set('demo', {'a': 1})
        merged = self.store.update('demo', {'b': 2})
        self.assertEqual(merged, {'a': 1, 'b': 2})
        self.assertEqual(self.store.get('demo'), {'a': 1, 'b': 2})


if __name__ == '__main__':
    unittest.main()
