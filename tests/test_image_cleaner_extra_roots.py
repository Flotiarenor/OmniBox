"""image-cleaner 的扫描范围覆盖「额外图片目录」的回归用例。

背景：图片相册（image-viewer）把额外图片目录当**相册**展示（顶层是 `__<目录名>` 命名空间
节点），但相册清理原先只 `os.walk(host.get_data_root())` —— 额外图库里的重复图永远找不出来，
"主目录一份 + 额外图库一份"这种**跨根重复**更是永远发现不了，而清重复最该抓的正是它。

修法不是让插件自己拼物理路径：宿主的 `/file`、`/thumbs`、`delete_files` 都按**虚拟路径**
（`__<命名空间>/<根内相对路径>`）解释，命名空间的取名规则（重名加序号、避开第一根的一级
子目录）只有一份。所以宿主公开 `list_roots()` 给出 `(根, 前缀)`，插件按根遍历并带上前缀。

本用例直接用真实的两个插件实例（不经过 Shell），锁住：
  1. 扫描结果同时包含主目录与额外根的图，额外根的相对路径带正确前缀；
  2. 跨根重复被分到同一组；
  3. 设置弹窗那一行列出全部根；
  4. 宿主没有 `list_roots()`（旧版本）时退回单根，行为不变；
  5. 宿主有 `list_roots()` 但项里没有 `prefix`（早于该字段的版本）时同样退回单根 ——
     把"前缀缺失"当空串会让额外根的文件按主根解释，`delete_files()` 可能删错文件；
  6. 额外根落在主根之内（宿主只排除完全相同的路径，不排除嵌套）时，同一个物理文件
     只收一条：否则它会被判成"自己是自己的重复"，删除会对同一文件 unlink 两次。

运行：
    python -m unittest tests.test_image_cleaner_extra_roots -v
"""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(PROJECT_ROOT / rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path, color=(10, 120, 200)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (24, 24), color).save(path)


class ExtraRootScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer_module = _load('plugins/image-viewer/backend/main.py', 'iv_main_extra_roots_test')
        cls.cleaner_module = _load('plugins/image-cleaner/backend/main.py', 'ic_main_extra_roots_test')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 先规范化一次：插件把根目录统统 `resolve()`（image-viewer/main.py 的 root_dir、
        # namespace.py 的额外根），而 Windows runner 的 `%TEMP%` 是 8.3 短名
        # （`C:\Users\RUNNER~1\…`）。不规范化的话，本用例拿短名去比插件的长名，
        # 在 CI 上必红、在关掉 8.3 的开发机上必绿。
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / 'data'
        self.extra = self.tmp / '外部' / '额外图库'
        self.extra2 = self.tmp / '另一个' / '额外图库'
        self._settings_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._settings_tmp.cleanup)
        self.settings_dir = Path(self._settings_tmp.name)

    # ---------- 组装两个真实插件实例 ----------

    def _viewer(self, extra_roots: str = ''):
        module = self.viewer_module
        config = {'root_dir': str(self.root), 'extra_roots': extra_roots}
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = config
        try:
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original
        plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    def _cleaner(self, host):
        plugin = self.cleaner_module.ImageCleanerPlugin(
            {'name': 'image-cleaner'},
            {'directories': {'data_root': str(self.root)}},
        )
        plugin._host = host
        return plugin

    # ---------- 1. 扫描范围 ----------

    def test_scan_covers_extra_roots_with_prefix(self):
        _make_image(self.root / '作者A' / '作品1' / '1.jpg', (10, 120, 200))
        _make_image(self.extra / '作者B' / '作品9' / '1.jpg', (200, 30, 30))
        cleaner = self._cleaner(self._viewer(str(self.extra)))

        rels = sorted(f['rel'] for f in cleaner._all_album_files())
        self.assertEqual(rels, ['__额外图库/作者B/作品9/1.jpg', '作者A/作品1/1.jpg'])

        root = self._viewer(str(self.extra))
        namespace = next(item for item in root.list_roots() if not item['is_primary'])['prefix']
        self.assertEqual(namespace, '__额外图库')
        # 前缀就是宿主自己解析得了的那段：反过来解一次必须落回额外根
        physical, _inner = root._resolve_path('__额外图库/作者B/作品9/1.jpg')
        self.assertEqual(physical, self.extra / '作者B' / '作品9' / '1.jpg')

    # ---------- 2. 跨根重复 ----------

    def test_cross_root_duplicates_are_grouped(self):
        _make_image(self.root / 'a.jpg', (7, 7, 7))
        _make_image(self.extra / '备份' / 'a.jpg', (7, 7, 7))       # 与主目录那张逐字节相同
        _make_image(self.extra / '备份' / 'b.jpg', (9, 9, 9))       # 唯一，不该进组
        cleaner = self._cleaner(self._viewer(str(self.extra)))

        result = cleaner.duplicate_scan()
        groups = [sorted(g['files']) for g in result['groups']]
        self.assertIn(['__额外图库/备份/a.jpg', 'a.jpg'], groups)
        self.assertEqual(len(groups), 1, f'只应有一组重复：{groups}')

    # ---------- 3. 设置里那一行列出全部根 ----------

    def test_settings_line_lists_all_roots(self):
        _make_image(self.root / 'a.jpg')
        _make_image(self.extra / 'a.jpg')
        _make_image(self.extra2 / 'a.jpg')
        cleaner = self._cleaner(self._viewer(f'{self.extra}\n{self.extra2}'))

        settings = cleaner.get_settings()
        self.assertEqual(settings['root_dir'].splitlines(),
                         [str(self.root), str(self.extra), str(self.extra2)])
        self.assertEqual(cleaner.get_status()['roots'],
                         [str(self.root), str(self.extra), str(self.extra2)])

    # ---------- 4. 旧宿主（没有 list_roots）退回单根 ----------

    def test_falls_back_to_single_root_without_list_roots(self):
        _make_image(self.root / 'a.jpg')
        _make_image(self.extra / 'a.jpg')
        host = self._viewer(str(self.extra))
        host.list_roots = None          # 模拟旧版 image-viewer
        cleaner = self._cleaner(host)

        rels = [f['rel'] for f in cleaner._all_album_files()]
        self.assertEqual(rels, ['a.jpg'], '旧宿主上只扫主目录，行为与改动前一致')
        self.assertEqual(cleaner.get_settings()['root_dir'], str(self.root))

    def test_falls_back_when_host_lacks_prefix(self):
        """宿主有 `list_roots()` 但项里没有 `prefix`（早于该字段的版本）：退回单根。

        不能把"前缀缺失"当成空串 —— 那会把额外根的文件按**主根**去解释：缩略图与原图
        404，`delete_files()` 更会删掉主根下的另一个同名文件。
        """
        _make_image(self.root / '作者A' / 'a.jpg')
        _make_image(self.extra / '作者A' / 'a.jpg')
        host = self._viewer(str(self.extra))
        real = host.list_roots
        host.list_roots = lambda: [
            {k: v for k, v in item.items() if k != 'prefix'} for item in real()
        ]
        cleaner = self._cleaner(host)

        rels = [f['rel'] for f in cleaner._all_album_files()]
        self.assertEqual(rels, ['作者A/a.jpg'], '没有前缀就只扫主目录，不能带错前缀去删')
        self.assertEqual(cleaner.get_settings()['root_dir'], str(self.root))

    # ---------- 5. 根目录嵌套：同一物理文件只算一条 ----------

    def test_overlapping_roots_count_each_file_once(self):
        """额外根落在主根之内时，同一个文件会有两条虚拟路径，只能收一条。

        宿主 `_extra_roots()` 只排除完全相同的路径、不排除嵌套，而 `os.walk(root)`
        本来就会走到 `root/备份`。不去重的话这一个文件会被判成"两张完全重复的图片"，
        "只留一张"之后的删除会对它 unlink 两次，"全选组"删除会删掉唯一副本。
        """
        nested = self.root / '备份'
        _make_image(self.root / 'a.jpg', (7, 7, 7))
        nested.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.root / 'a.jpg', nested / 'a.jpg')   # 逐字节相同
        cleaner = self._cleaner(self._viewer(str(nested)))

        rels = sorted(f['rel'] for f in cleaner._all_album_files())
        self.assertEqual(rels, ['a.jpg', '备份/a.jpg'],
                         '主根那次扫描已经收过它，命名空间那次要丢掉')

        groups = [sorted(g['files']) for g in cleaner.duplicate_scan()['groups']]
        self.assertEqual(groups, [['a.jpg', '备份/a.jpg']],
                         f'跨根重复照常抓到，但不该出现同一物理文件的两条路径：{groups}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
