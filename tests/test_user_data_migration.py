"""`%APPDATA%` 用户数据目录从 OmniBox 到 Cubby 的一次性迁移。

为什么必须有这组用例：迁移发生在 `get_user_data_dir()` 内部（冻结模式且可执行
文件目录不可写时才走那条回退分支），而它被 `lru_cache` 包着、又是全仓所有可写
路径的派生根。一旦这几个分支写错，用户看到的不是报错，而是"设置、插件、身份
全没了"或"一会儿用旧目录、一会儿用新目录"。因此这里把三条语义分别钉住：

- 旧目录存在、新目录不存在 → 改名迁移，数据跟着走；
- 新目录已存在 → 直接用新目录，旧目录一个字节都不碰；
- 改名失败（被占用 / 跨卷 / 权限）→ 本次继续用旧目录，不复制、不删除。

运行：
    python -m unittest tests.test_user_data_migration -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend import paths


class UserDataDirMigrationTests(unittest.TestCase):
    """三条迁移语义都用真实的临时目录跑，只把"冻结 + 可执行目录只读"这一前提替身掉。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # 回退分支由环境变量决定目标盘；CUBBY_HOME 置空保证不会走进程级覆盖。
        env = mock.patch.dict(os.environ, {'APPDATA': str(self.root), 'CUBBY_HOME': ''})
        env.start()
        self.addCleanup(env.stop)
        frozen = mock.patch.object(paths, 'is_frozen', return_value=True)
        frozen.start()
        self.addCleanup(frozen.stop)
        readonly = mock.patch.object(paths, '_can_write_app_dir', return_value=False)
        readonly.start()
        self.addCleanup(readonly.stop)
        paths.get_user_data_dir.cache_clear()
        self.addCleanup(paths.get_user_data_dir.cache_clear)
        self.legacy = self.root / 'OmniBox'
        self.target = self.root / 'Cubby'

    def test_migrates_legacy_dir_with_its_files(self):
        self.legacy.mkdir()
        (self.legacy / 'principal.json').write_text('{"name": "本机"}', encoding='utf-8')

        resolved = paths.get_user_data_dir()

        self.assertEqual(resolved, self.target.resolve())
        self.assertTrue((self.target / 'principal.json').is_file(), '旧目录里的文件应跟着迁到新目录')
        self.assertFalse(self.legacy.exists(), '改名迁移后旧目录不应继续存在')
        # lru_cache：整进程只算一次，第二次调用不再碰磁盘
        self.assertEqual(paths.get_user_data_dir(), resolved)

    def test_keeps_existing_new_dir_untouched(self):
        self.legacy.mkdir()
        (self.legacy / 'legacy-only.txt').write_text('old', encoding='utf-8')
        self.target.mkdir()
        (self.target / 'new-only.txt').write_text('new', encoding='utf-8')

        resolved = paths.get_user_data_dir()

        self.assertEqual(resolved, self.target.resolve())
        self.assertTrue((self.target / 'new-only.txt').is_file())
        self.assertFalse((self.target / 'legacy-only.txt').exists(), '新目录已存在时不应把旧内容并进来')
        self.assertTrue((self.legacy / 'legacy-only.txt').is_file(), '新目录已存在时旧目录应原样保留')

    def test_falls_back_to_legacy_dir_when_rename_fails(self):
        self.legacy.mkdir()
        (self.legacy / 'principal.json').write_text('{"name": "本机"}', encoding='utf-8')

        with mock.patch.object(paths.os, 'replace', side_effect=OSError('磁盘被占用')), \
                self.assertLogs('shell.backend.paths', level='WARNING') as logs:
            resolved = paths.get_user_data_dir()

        self.assertEqual(resolved, self.legacy.resolve(), '迁移失败时本次必须继续用旧目录')
        self.assertTrue((self.legacy / 'principal.json').is_file(), '迁移失败不能删除或复制用户数据')
        self.assertFalse(self.target.exists(), '迁移失败不应留下半成品新目录')
        self.assertTrue(any('迁移失败' in line for line in logs.output), f'迁移失败必须告警: {logs.output}')


if __name__ == '__main__':
    unittest.main()
