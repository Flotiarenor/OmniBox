"""用户数据根解析的用例（`shell/backend/paths.py`）。

背景
----
2.0 之前，冻结产物的数据根是"可执行文件旁边，写不进去才退 %APPDATA%"：于是同一份程序
解压到不同目录就有不同的数据目录，用户升级时表现为"新版本换了目录，历史记录留在旧
文件夹里"，只能手工搬 `.config/` 与 `data/`。

现在的约定是**显式**的，本文件把它钉住：

1. `CUBBY_HOME` 优先于一切（多实例与端到端测试靠它隔离）；
2. 开发模式恒为项目根目录（从源码运行的数据仍在仓库里）；
3. 便携模式只在"冻结产物 + exe 旁有 `portable.txt`"时成立，且目录不可写时退回默认根
   并告警 —— 不再按"目录可写"推断便携；
4. 默认根与安装目录无关：Windows `%LOCALAPPDATA%/Cubby`，其他平台
   `$XDG_DATA_HOME/cubby`（未设置时 `~/.local/share/cubby`）。

这些用例都是进程内的纯路径计算：`is_frozen` 与 `sys.executable` 被打桩，
`_can_write_dir` 也不真去碰磁盘（除了可写用例自己造的临时目录），因此不需要打包产物。
"""

from __future__ import annotations

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
from shell.backend.paths import (
    PORTABLE_MARKER,
    USER_DATA_DIR_ENV,
    get_config_dir,
    get_plugin_search_dirs,
    get_plugins_config_dir,
    get_user_data_dir,
    get_webview_storage_dir,
    is_portable,
)

_ENV_KEYS = (USER_DATA_DIR_ENV, 'LOCALAPPDATA', 'APPDATA', 'XDG_DATA_HOME')


class UserDataRootTests(unittest.TestCase):
    """`get_user_data_dir()` 的解析顺序与派生目录。"""

    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        get_user_data_dir.cache_clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_user_data_dir.cache_clear()

    # ------------------------------------------------------------------ 打桩
    def _frozen(self, exe_dir: Path, *, writable: bool = True):
        """模拟冻结产物：exe 位于 `exe_dir`，并指定该目录是否可写。"""
        patchers = [
            mock.patch.object(paths, 'is_frozen', lambda: True),
            mock.patch.object(sys, 'executable', str(Path(exe_dir) / 'Cubby.exe')),
            mock.patch.object(paths, '_can_write_dir', lambda _candidate: writable),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _mark_portable(self, exe_dir: Path) -> None:
        Path(exe_dir).mkdir(parents=True, exist_ok=True)
        (Path(exe_dir) / PORTABLE_MARKER).write_text('', encoding='utf-8')

    def _default_root(self) -> Path:
        """默认数据根（与生产代码同一判据，但不复用它，避免自证）。"""
        if os.name == 'nt':
            return Path(os.environ['LOCALAPPDATA']) / 'Cubby'
        return Path(os.environ['XDG_DATA_HOME']) / 'cubby'

    def test_cubby_home_wins_over_everything_else(self):
        """`CUBBY_HOME` 优先于便携标记与默认根。"""
        exe_dir = self.tmp / 'install'
        self._mark_portable(exe_dir)
        self._frozen(exe_dir)
        os.environ[USER_DATA_DIR_ENV] = str(self.tmp / 'instance')
        os.environ['LOCALAPPDATA'] = str(self.tmp / 'local')
        os.environ['XDG_DATA_HOME'] = str(self.tmp / 'xdg')

        self.assertEqual(get_user_data_dir(), (self.tmp / 'instance').resolve())

    def test_development_mode_uses_project_root(self):
        """开发模式（未冻结）恒为项目根目录：从源码运行的数据仍在仓库里。"""
        os.environ['LOCALAPPDATA'] = str(self.tmp / 'local')
        os.environ['XDG_DATA_HOME'] = str(self.tmp / 'xdg')

        self.assertEqual(get_user_data_dir(), paths.get_project_root())

    def test_frozen_without_marker_uses_default_root(self):
        """没有便携标记时，数据根与安装目录无关。"""
        exe_dir = self.tmp / 'install'
        exe_dir.mkdir()
        self._frozen(exe_dir)
        os.environ['LOCALAPPDATA'] = str(self.tmp / 'local')
        os.environ['XDG_DATA_HOME'] = str(self.tmp / 'xdg')

        self.assertEqual(get_user_data_dir().resolve(), self._default_root().resolve())

    def test_portable_marker_keeps_data_beside_the_executable(self):
        """有标记且目录可写时，数据跟随程序目录（便携版语义）。"""
        exe_dir = self.tmp / 'install'
        self._mark_portable(exe_dir)
        self._frozen(exe_dir, writable=True)
        os.environ['LOCALAPPDATA'] = str(self.tmp / 'local')
        os.environ['XDG_DATA_HOME'] = str(self.tmp / 'xdg')

        self.assertEqual(get_user_data_dir(), exe_dir.resolve())

    def test_portable_marker_in_unwritable_dir_falls_back_and_warns(self):
        """标记写在不可写目录里：退回默认根，并留下一条告警。"""
        exe_dir = self.tmp / 'install'
        self._mark_portable(exe_dir)
        self._frozen(exe_dir, writable=False)
        os.environ['LOCALAPPDATA'] = str(self.tmp / 'local')
        os.environ['XDG_DATA_HOME'] = str(self.tmp / 'xdg')

        with self.assertLogs('shell.backend.paths', level='WARNING'):
            resolved = get_user_data_dir()

        self.assertEqual(resolved.resolve(), self._default_root().resolve())

    def test_portable_marker_is_ignored_in_development_mode(self):
        """开发模式下标记文件不生效：否则在仓库里放一个标记就会改变行为。"""
        exe_dir = self.tmp / 'install'
        self._mark_portable(exe_dir)
        os.environ['LOCALAPPDATA'] = str(self.tmp / 'local')
        os.environ['XDG_DATA_HOME'] = str(self.tmp / 'xdg')

        self.assertFalse(is_portable())
        self.assertEqual(get_user_data_dir(), paths.get_project_root())

    def test_is_portable_requires_marker_file(self):
        """`is_portable()` 只看标记文件是否存在，不看目录可写性。"""
        exe_dir = self.tmp / 'install'
        exe_dir.mkdir()
        self._frozen(exe_dir, writable=False)

        self.assertFalse(is_portable())
        self.assertEqual(PORTABLE_MARKER, 'portable.txt')

    # -------------------------------------------------------------- 平台默认根
    @unittest.skipUnless(os.name == 'nt', 'Windows 专属的 %LOCALAPPDATA% 判据')
    def test_windows_uses_localappdata(self):
        os.environ['LOCALAPPDATA'] = str(self.tmp / 'local')
        self._frozen(self.tmp / 'install')
        self.assertEqual(get_user_data_dir().resolve(), (self.tmp / 'local' / 'Cubby').resolve())

    @unittest.skipUnless(os.name == 'nt', 'Windows 专属的 %APPDATA% 回退')
    def test_windows_falls_back_to_appdata(self):
        os.environ['APPDATA'] = str(self.tmp / 'roaming')
        self._frozen(self.tmp / 'install')
        self.assertEqual(get_user_data_dir().resolve(), (self.tmp / 'roaming' / 'Cubby').resolve())

    @unittest.skipIf(os.name == 'nt', 'XDG 判据只在非 Windows 成立')
    def test_linux_uses_xdg_data_home(self):
        os.environ['XDG_DATA_HOME'] = str(self.tmp / 'xdg')
        self._frozen(self.tmp / 'install')
        self.assertEqual(get_user_data_dir().resolve(), (self.tmp / 'xdg' / 'cubby').resolve())

    @unittest.skipIf(os.name == 'nt', 'XDG 判据只在非 Windows 成立')
    def test_linux_falls_back_to_local_share(self):
        self._frozen(self.tmp / 'install')
        expected = Path.home() / '.local' / 'share' / 'cubby'
        self.assertEqual(get_user_data_dir().resolve(), expected.resolve())

    # ---------------------------------------------------------------- 派生目录
    def test_derived_dirs_all_hang_under_the_user_data_root(self):
        """配置、插件设置、WebView profile 都从数据根派生（没有第二个锚点）。"""
        os.environ[USER_DATA_DIR_ENV] = str(self.tmp / 'instance')

        root = get_user_data_dir()
        self.assertEqual(get_config_dir(), root / '.config')
        self.assertEqual(get_plugins_config_dir(), root / '.config' / 'plugins')
        self.assertEqual(get_webview_storage_dir(), root / '.config' / 'webview')

    def test_frozen_plugin_search_prefers_user_data_plugins(self):
        """用户自己装的插件在数据根下，因此换版本后仍然在。"""
        os.environ[USER_DATA_DIR_ENV] = str(self.tmp / 'instance')
        self._frozen(self.tmp / 'install')

        dirs = get_plugin_search_dirs()
        self.assertEqual(dirs[0], (self.tmp / 'instance' / 'plugins').resolve())
        self.assertEqual(dirs[1], (paths.get_project_root() / 'plugins').resolve())


if __name__ == '__main__':
    unittest.main()
