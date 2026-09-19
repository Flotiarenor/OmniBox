"""media-player 维护工具（plugins/media-player/tool/library_tool.py）的判定与安全网。

工具里 `dups --apply` 会删文件、`lrc --apply` 会搬文件，所以这里守两件事：

- 判定规则与旧脚本一致：同名括号后缀 + 大小/MD5 相同才算重复；同目录同主名才算歌词成对；
- 「脏 .lrc 只搬进 _多余lrc、永不删除」这条承诺不被改掉。

负载模块用 spec_from_file_location：插件目录名带连字符，不能当包 import。
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = PROJECT_ROOT / 'plugins' / 'media-player' / 'tool' / 'library_tool.py'


def _load_tool():
    spec = importlib.util.spec_from_file_location('media_player_library_tool', str(TOOL_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class _TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mp_tool_')
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, text: str = 'data'):
        (self.root / name).write_text(text, encoding='utf-8')


class FindDupsTest(_TempTree):
    """用例移植自 Remove-DupTracks.ps1 -SelfTest：规则既不能放松也不能收紧。"""

    def test_only_same_name_and_same_content_counts(self):
        fixtures = {
            'a.flac': 'AAA', 'a (1).flac': 'AAA', 'a（2）.flac': 'AAA',   # 同名同内容 -> 重复
            'b.lrc': 'BBB', 'b (1).lrc': 'BBBB',                        # 名字像但内容不同 -> 不算
            'c (1).flac': 'CCC',                                        # 没有原文件 -> 不算
            'd (Live).flac': 'DDD', 'd (Live) (1).flac': 'DDD',         # 嵌套括号 -> 重复
        }
        for name, text in fixtures.items():
            self._write(name, text)

        groups = tool.find_dups(sorted(self.root.iterdir()))
        self.assertEqual(['a (1).flac', 'a（2）.flac', 'd (Live) (1).flac'],
                         sorted(dup.name for _, dups in groups for dup in dups))
        self.assertEqual(2, len(groups))
        self.assertEqual('a.flac', groups[0][0].name)
        self.assertEqual('d (Live).flac', groups[1][0].name)

    def test_apply_deletes_only_the_duplicate(self):
        self._write('a.flac', 'AAA')
        self._write('a (1).flac', 'AAA')

        self.assertEqual(0, tool.cmd_dups(self.root, apply=True, ignore_content=False))
        self.assertTrue((self.root / 'a.flac').is_file())
        self.assertFalse((self.root / 'a (1).flac').exists())


class LyricsTest(_TempTree):
    def setUp(self):
        super().setUp()
        for name in ('x.flac', 'x.lrc', 'y.flac', 'z.lrc'):
            self._write(name)

    def test_pairing_rules(self):
        result = tool.scan_lyrics(sorted(self.root.iterdir()))
        self.assertEqual(1, result['paired'])
        self.assertEqual(['y.flac'], [p.name for p in result['missing']])
        self.assertEqual(['z.lrc'], [p.name for p in result['orphan']])

    def test_apply_moves_orphans_and_never_deletes(self):
        self.assertEqual(0, tool.cmd_lrc(self.root, apply=True))

        self.assertTrue((self.root / '_多余lrc' / 'z.lrc').is_file(), '多余的 .lrc 必须被搬到隔离区')
        self.assertFalse((self.root / 'z.lrc').exists())
        self.assertTrue((self.root / 'x.lrc').is_file(), '成对的 .lrc 不能被动')
        self.assertTrue((self.root / 'x.flac').is_file())

    def test_quarantine_is_skipped_on_rescan(self):
        tool.cmd_lrc(self.root, apply=True)
        result = tool.scan_lyrics(tool.walk_files(self.root))
        self.assertEqual([], [p.name for p in result['orphan']], '隔离区里的文件不该再被当成多余歌词')

    def test_listings_are_written(self):
        tool.cmd_lrc(self.root, apply=False)
        for name in (tool.MISSING_LRC_TXT, tool.ORPHAN_LRC_TXT):
            text = (self.root / name).read_text(encoding='utf-8')
            self.assertTrue(text.startswith('# '), f'{name} 必须保留旧 .bat 的 # 表头')


class CacheViewTest(unittest.TestCase):
    """缓存是唯一能给出「播放器界面口径」的来源：从 album_key 还原分组。"""

    def test_album_key_is_split_and_filtered_by_namespace(self):
        cache = {'items': [
            {'kind': 'audio', 'album_key': '音乐//album::同名||甲', 'path': 'C:/m/1.flac'},
            {'kind': 'audio', 'album_key': '音乐//album::同名||乙', 'path': 'C:/m/2.flac'},
            {'kind': 'audio', 'album_key': '视频//album::同名||丙', 'path': 'C:/v/3.flac'},
            {'kind': 'video', 'album_key': '视频/某片', 'path': 'C:/v/4.mp4'},
        ]}

        groups = tool.cached_groups(cache, '音乐')
        self.assertEqual({'同名': {'甲': [Path('C:/m/1.flac')], '乙': [Path('C:/m/2.flac')]}}, groups)

    def test_stale_cache_is_not_used(self):
        """条目数与磁盘不符 / 有条目 mtime 变了，都必须判定为不新鲜。"""
        self._tmp = tempfile.TemporaryDirectory(prefix='mp_tool_cache_')
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / 'a.flac').write_text('data', encoding='utf-8')
        cache_dir = root / '.cache'
        cache_dir.mkdir()
        stat = (root / 'a.flac').stat()
        payload = {'version': 4, 'updated': 1, 'items': [
            {'kind': 'audio', 'album_key': '音乐//album::A||B', 'path': str(root / 'a.flac'),
             'size': stat.st_size, 'mtime': stat.st_mtime}]}
        # 必须走 json.dumps：Windows 路径里的反斜杠直接拼进 JSON 字符串是非法转义
        (cache_dir / 'media_index.json').write_text(json.dumps(payload), encoding='utf-8')

        fresh = tool.load_cache([root])
        self.assertTrue(fresh['fresh'])

        (root / 'a.flac').write_text('changed-size', encoding='utf-8')
        self.assertFalse(tool.load_cache([root])['fresh'], '文件变了以后缓存必须判定为过期')


class ResolveLibraryTest(unittest.TestCase):
    def test_explicit_root_wins(self):
        with tempfile.TemporaryDirectory(prefix='mp_tool_root_') as tmp:
            library = tool.resolve_library(tmp)
        self.assertEqual(Path(tmp).resolve(), library['root'])
        self.assertEqual([Path(tmp).resolve()], library['roots'])
        self.assertIsNone(library['config'], '--root 时不该去读 OmniBox 配置')

    def test_missing_dir_exits(self):
        with self.assertRaises(SystemExit):
            tool.resolve_library(str(Path(tempfile.gettempdir()) / 'mp-tool-not-exist-xyz'))


class SelfTestFlagTest(unittest.TestCase):
    def test_cli_selftest_passes(self):
        self.assertEqual(0, tool.selftest())


if __name__ == '__main__':
    unittest.main()
