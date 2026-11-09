"""主配置落点与旧版迁移的用例（`.config/app.json`）。

背景
----
主配置的落点换过两次：`<user_data>/config.yaml` → `<config>/app.yaml` → `<config>/app.json`
（最后一次是为了与仓库其余持久化同一种格式，并把 PyYAML 从运行依赖与打包收集清单里去掉）。

迁移只有一次机会，因此这里钉住三件事：

* 旧版 YAML 读得对 —— 包括 PyYAML 默认输出的块状列表（`trusted_hosts:` 后跟 `- 值`，
  `-` 与键同缩进）、引号、行尾注释；
* 旧文件读不出来时**不猜**：保留旧文件、本次用默认值，下次启动再试。读错值比不迁移更糟，
  `data_root` 读成半截字符串会把数据根悄悄换到别处；
* 手改配置写坏（不是 JSON、类型不对）不能拦住启动：回退默认值或就地纠正，且不覆写文件。

规模说明：这些用例直接调 `main.load_config`，用 `CUBBY_HOME` 把用户数据目录指到临时
目录。`get_user_data_dir()` 是 `lru_cache` 的进程级缓存，所以每个用例都要 `cache_clear()`，
顺序反过来（缓存命中旧目录）会让断言看起来莫名其妙地失败。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.paths import USER_DATA_DIR_ENV, get_user_data_dir

try:
    import main
    _MAIN_IMPORT_ERROR = ''
except Exception as _exc:   # 缺 GUI 后端的机器上 `import webview` 会在这里失败
    main = None             # type: ignore[assignment]
    _MAIN_IMPORT_ERROR = f'{type(_exc).__name__}: {_exc}'


class ConfigLoadTests(unittest.TestCase):

    def setUp(self):
        if main is None:
            self.skipTest(f'无法导入入口模块（缺 pywebview 等运行时依赖）: {_MAIN_IMPORT_ERROR}')
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._previous_env = os.environ.get(USER_DATA_DIR_ENV)
        os.environ[USER_DATA_DIR_ENV] = str(self.home)
        get_user_data_dir.cache_clear()
        self.config_dir = self.home / '.config'
        self.config_file = self.config_dir / 'app.json'

    def tearDown(self):
        if self._previous_env is None:
            os.environ.pop(USER_DATA_DIR_ENV, None)
        else:
            os.environ[USER_DATA_DIR_ENV] = self._previous_env
        get_user_data_dir.cache_clear()
        self._tmp.cleanup()

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')

    # ------------------------------------------------------------------ 新建

    def test_creates_app_json_with_defaults(self):
        """全新用户数据目录：建出 app.json，内容就是默认值（相对 data_root 不落盘绝对路径）。"""
        config = main.load_config()

        self.assertTrue(self.config_file.is_file(), '没有创建 .config/app.json')
        self.assertFalse((self.config_dir / 'app.yaml').exists(), '不应再落 YAML 文件')
        written = json.loads(self.config_file.read_text(encoding='utf-8'))
        self.assertEqual(written, main.DEFAULT_CONFIG)
        self.assertEqual(written['directories']['data_root'], './data',
                         '落盘的是相对路径，绝对值只出现在返回值里')
        self.assertEqual(config['server']['host'], '127.0.0.1')
        self.assertEqual(config['server']['port'], 18080)
        self.assertEqual(Path(config['directories']['data_root']),
                         (self.home / 'data').resolve())

    # ------------------------------------------------------------------ 读取

    def test_partial_file_is_filled_with_defaults(self):
        """缺键、端口写成字符串：补齐默认值并把端口归一成整数。"""
        self._write(self.config_file, '{"server": {"port": "19000"}}')

        config = main.load_config()

        self.assertEqual(config['server']['port'], 19000)
        self.assertIsInstance(config['server']['port'], int)
        self.assertEqual(config['server']['host'], '127.0.0.1')
        self.assertEqual(config['server']['trusted_hosts'], [])
        self.assertEqual(Path(config['directories']['data_root']),
                         (self.home / 'data').resolve())
        # 读取不回报文：补齐只发生在内存里，用户文件保持原样
        self.assertEqual(json.loads(self.config_file.read_text(encoding='utf-8')),
                         {'server': {'port': '19000'}})

    def test_invalid_field_types_are_corrected(self):
        """端口不是数字、白名单写成单个字符串、host 为空：就地纠正而不是让启动失败。"""
        self._write(self.config_file, json.dumps({
            'server': {'host': '  ', 'port': 'abc', 'trusted_hosts': 'cubby.lan'},
            'directories': {'data_root': ''},
        }))

        config = main.load_config()

        self.assertEqual(config['server']['host'], '127.0.0.1')
        self.assertEqual(config['server']['port'], 18080)
        self.assertEqual(config['server']['trusted_hosts'], ['cubby.lan'])
        self.assertEqual(Path(config['directories']['data_root']),
                         (self.home / 'data').resolve())

    def test_broken_json_falls_back_without_overwriting(self):
        """手改写坏（不是 JSON）：本次用默认值，但**不覆写**文件（可能只是改到一半）。"""
        broken = '{"server": {"port": 19000,'
        self._write(self.config_file, broken)

        config = main.load_config()

        self.assertEqual(config['server']['port'], 18080)
        self.assertEqual(self.config_file.read_text(encoding='utf-8'), broken,
                         '配置文件被覆写了，用户的半截改动会丢')

    # ------------------------------------------------------------------ 迁移

    def test_migrates_legacy_app_yaml(self):
        """上一版落点 `<config>/app.yaml`：值搬过去，旧文件改名保留。"""
        self._write(self.config_dir / 'app.yaml', (
            'server:\n'
            '  host: 0.0.0.0\n'
            '  port: 19080\n'
            '  trusted_hosts:\n'
            '  - 192.168.1.10\n'
            '  - cubby.lan\n'
            'directories:\n'
            '  data_root: ./媒体库\n'
        ))

        config = main.load_config()

        self.assertEqual(config['server']['host'], '0.0.0.0')
        self.assertEqual(config['server']['port'], 19080)
        self.assertEqual(config['server']['trusted_hosts'], ['192.168.1.10', 'cubby.lan'])
        self.assertEqual(Path(config['directories']['data_root']),
                         (self.home / '媒体库').resolve())
        self.assertTrue(self.config_file.is_file(), '迁移没有写出 app.json')
        migrated = json.loads(self.config_file.read_text(encoding='utf-8'))
        self.assertEqual(migrated['server']['port'], 19080)
        self.assertEqual(migrated['directories']['data_root'], './媒体库',
                         '落盘保持用户写的相对路径，不要写死成绝对路径')
        self.assertFalse((self.config_dir / 'app.yaml').exists(), '旧文件没有改名')
        self.assertTrue((self.config_dir / 'app.yaml.migrated').is_file(),
                        '旧文件改名后应保留内容')

    def test_migrates_oldest_config_yaml_location(self):
        """更早的落点 `<user_data>/config.yaml` 同样能迁移（它在数据根，不在 .config 下）。"""
        self._write(self.home / 'config.yaml', (
            'server:\n'
            '  host: 127.0.0.1\n'
            '  port: 18081\n'
        ))

        config = main.load_config()

        self.assertEqual(config['server']['port'], 18081)
        self.assertTrue(self.config_file.is_file())
        self.assertTrue((self.home / 'config.yaml.migrated').is_file())

    def test_legacy_migrated_file_is_not_overwritten_on_second_rename(self):
        """已经存在 `.migrated` 时改用带序号的名字，不覆盖上一次的备份。"""
        self._write(self.config_dir / 'app.yaml.migrated', 'server:\n  port: 1\n')
        self._write(self.config_dir / 'app.yaml', 'server:\n  port: 19001\n')

        config = main.load_config()

        self.assertEqual(config['server']['port'], 19001)
        self.assertEqual((self.config_dir / 'app.yaml.migrated').read_text(encoding='utf-8'),
                         'server:\n  port: 1\n',
                         '上一次的备份被覆盖了')
        self.assertTrue((self.config_dir / 'app.yaml.migrated-2').is_file())

    def test_unparsable_legacy_file_is_kept_for_retry(self):
        """旧文件读不出来：留原文件、不写 app.json（下次启动再试），本次用默认值。"""
        self._write(self.config_dir / 'app.yaml', (
            'server:\n'
            '\thost: 127.0.0.1\n'          # 制表符缩进：YAML 本身也不接受
            '  port: 19080\n'
        ))

        config = main.load_config()

        self.assertEqual(config['server']['port'], 18080)
        self.assertTrue((self.config_dir / 'app.yaml').is_file(), '旧文件被删/改名，用户设置没了')
        self.assertFalse(self.config_file.exists(), '解析失败时不应写出半份 app.json')

    # ------------------------------------------------------------------ 旧版读法

    def test_legacy_yaml_scalar_forms(self):
        """旧版读法支持的标量形态：引号、行尾注释、行内列表，以及值里出现的 `#`。"""
        cases = {
            'host: 127.0.0.1': '127.0.0.1',
            'host: "192.168.0.2"': '192.168.0.2',
            "data_root: './data'": './data',
            'data_root: ./data # 数据根': './data',
            'data_root: ./a#b': './a#b',
            'trusted_hosts: []': [],
            'trusted_hosts: [a.lan, b.lan]': ['a.lan', 'b.lan'],
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                key, _, _ = line.partition(':')
                parsed = main._read_legacy_config(self._block_file(line))
                assert parsed is not None, f'这行没被读出来：{line}'
                self.assertEqual(parsed['block'][key.strip()], expected)

    def _block_file(self, line: str) -> Path:
        """把一行配置包进一个块写到临时文件，返回路径。"""
        path = self.home / 'legacy-scalar.yaml'
        self._write(path, f'block:\n  {line.strip()}\n')
        return path


if __name__ == '__main__':
    unittest.main()
