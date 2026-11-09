"""`/file` 与 `/thumbs` 的按需取内容钩子：失败要留日志、状态码要分清。

审计项 A2（P1）：`instance.ensure_file()` / `ensure_thumb()` 失败原先一律
`except Exception: pass`。"插件/远端取不到内容"与"文件确实不存在"都退化成 404，
服务端一行日志都没有 —— 用户只看到一张破图，排障时无从下手。

现在的口径：

  * 插件抛 `FileNotFoundError` = 内容确实不存在 → 404（按 info 记录，不当故障；
    文件真不存在是常态，浏览器会请求已删除的缩略图）；
  * 其它异常 = 失败原因在插件或远端 → 5xx，并记 warning（含插件名与路径）。

夹具复用 `tests.test_file_server_paths` 的真实 Flask app 与插件管理器替身：那边已经
定义了越界、受保护路径等路由语义，这里只关心"钩子失败怎么映射"，不再抄第二份路由夹具。

运行：
    python -m unittest tests.test_file_server_plugin_hooks -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import List
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_file_server_paths import _FileRouteFixture, _StubInstance


class _FailingFetchInstance(_StubInstance):
    """`ensure_file` / `ensure_thumb` 必然失败的插件：失败类型由用例指定。"""

    def __init__(self, root, error, thumb_dir=None):
        super().__init__([root], thumb_dir=thumb_dir)
        self.error = error
        self.file_calls: List[str] = []
        self.thumb_calls: List[str] = []

    def ensure_file(self, path):
        self.file_calls.append(str(path))
        raise self.error

    def ensure_thumb(self, rel_path):
        self.thumb_calls.append(rel_path)
        raise self.error


class EnsureFileFailureTests(_FileRouteFixture, unittest.TestCase):
    """`/file` 的按需取内容钩子：5xx 与 404 必须各归其位。"""

    def _register(self, error):
        root = Path(self._tmp.name) / 'remote-cache'
        root.mkdir(exist_ok=True)
        instance = _FailingFetchInstance(root, error)
        self.manager._instances['broken'] = instance
        return root, instance

    def test_plugin_failure_is_5xx_and_logged_with_plugin_and_path(self):
        """插件抛异常（远端不可达一类）：5xx，且日志带上插件名与路径。"""
        root, instance = self._register(RuntimeError('远端不可达'))
        target = root / 'share' / 'photo.bin'
        with self.assertLogs('shell.backend.file_server', level='WARNING') as captured:
            resp = self._get('/file?path=' + quote(str(target)) + '&plugin=broken')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(len(instance.file_calls), 1, '应当只回调一次')
        records = '\n'.join(captured.output)
        self.assertIn('broken', records, '日志里应带上插件名')
        self.assertIn('photo.bin', records, '日志里应带上路径')
        self.assertIn('远端不可达', records, '日志里应带上失败原因')

    def test_plugin_failure_in_relative_path_branch_is_also_5xx(self):
        """默认相对路径分支（`/file?path=<相对路径>`）同样不能静默成 404。"""
        _root, instance = self._register(RuntimeError('取回失败'))
        with self.assertLogs('shell.backend.file_server', level='WARNING') as captured:
            resp = self._get('/file?path=' + quote('share/missing.bin') + '&plugin=broken')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(len(instance.file_calls), 1)
        self.assertIn('missing.bin', '\n'.join(captured.output))

    def test_plugin_file_not_found_is_404_not_5xx(self):
        """`FileNotFoundError` = 插件明确表示内容不存在：仍是 404，且不当故障记。"""
        root, instance = self._register(FileNotFoundError('远端已删除'))
        target = root / 'share' / 'gone.bin'
        with self.assertLogs('shell.backend.file_server', level='INFO') as captured:
            resp = self._get('/file?path=' + quote(str(target)) + '&plugin=broken')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(instance.file_calls), 1)
        records = '\n'.join(captured.output)
        self.assertIn('gone.bin', records)
        self.assertNotIn('WARNING', records, '内容不存在是常态，不该按故障级别记录')


class EnsureThumbFailureTests(_FileRouteFixture, unittest.TestCase):
    """`/thumbs` 的两条取字节路径（插件字节 / 散文件生成）同口径。"""

    def _register(self, error):
        root = Path(self._tmp.name) / 'thumb-root'
        root.mkdir(exist_ok=True)
        thumb_dir = Path(self._tmp.name) / 'thumb-cache'
        thumb_dir.mkdir(exist_ok=True)
        instance = _FailingFetchInstance(root, error, thumb_dir=thumb_dir)
        self.manager._instances['broken-thumbs'] = instance
        return thumb_dir, instance

    def test_ensure_thumb_failure_is_5xx_and_logged(self):
        _thumb_dir, instance = self._register(RuntimeError('缩略图生成崩溃'))
        with self.assertLogs('shell.backend.file_server', level='WARNING') as captured:
            resp = self._get('/thumbs/a.jpg?plugin=broken-thumbs')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(instance.thumb_calls, ['a.jpg'])
        records = '\n'.join(captured.output)
        self.assertIn('broken-thumbs', records, '日志里应带上插件名')
        self.assertIn('a.jpg', records, '日志里应带上路径')
        self.assertIn('缩略图生成崩溃', records)

    def test_ensure_thumb_file_not_found_is_404(self):
        _thumb_dir, instance = self._register(FileNotFoundError('没有这张缩略图'))
        resp = self._get('/thumbs/b.jpg?plugin=broken-thumbs')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(instance.thumb_calls, ['b.jpg'])

    def test_byte_source_failure_without_scattered_file_is_5xx(self):
        """插件字节分支失败、散文件也没有：失败在插件侧，不能报成"缩略图不存在"。"""
        _thumb_dir, _instance = self._register(RuntimeError('字节读取失败'))
        with self.assertLogs('shell.backend.file_server', level='WARNING') as captured:
            resp = self._get('/thumbs/c.jpg?plugin=broken-thumbs')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('c.jpg', '\n'.join(captured.output))

    def test_byte_source_failure_with_scattered_file_still_serves(self):
        """散文件布局能兜住时照常 200：字节分支失败不等于这次请求失败。"""
        thumb_dir, _instance = self._register(RuntimeError('字节读取失败'))
        (thumb_dir / 'd.jpg').write_bytes(b'THUMB-FROM-DISK')
        resp = self._get('/thumbs/d.jpg?plugin=broken-thumbs')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'THUMB-FROM-DISK')


class MissingContentStill404Tests(_FileRouteFixture, unittest.TestCase):
    """对照组：插件什么都不做（也不抛异常）时，行为与改造前一致仍是 404。"""

    def test_plugin_without_fetch_yields_404(self):
        root = Path(self._tmp.name) / 'empty-cache'
        root.mkdir(exist_ok=True)
        self.manager._instances['no-fetch'] = _StubInstance([root])
        resp = self._get('/file?path=' + quote(str(root / 'nope.bin')) + '&plugin=no-fetch')
        self.assertEqual(resp.status_code, 404)

    def test_missing_thumb_without_generation_yields_404(self):
        root = Path(self._tmp.name) / 'thumb-root'
        root.mkdir(exist_ok=True)
        thumb_dir = Path(self._tmp.name) / 'thumb-cache'
        thumb_dir.mkdir(exist_ok=True)
        self.manager._instances['thumb-plugin'] = _StubInstance([root], thumb_dir=thumb_dir)
        resp = self._get('/thumbs/none.jpg?plugin=thumb-plugin')
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main()
