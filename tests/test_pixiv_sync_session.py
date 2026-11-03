"""pixiv-sync 认证时轮换 refresh_token 的回写：没落盘不能报"已回写"。

`PixivSession.authenticate()` 在 Pixiv 轮换 `refresh_token` 后把它回写设置。原先它不
检查 `_persist_setting` 的返回值，随后无条件 `log.info('[pixiv-sync] refresh_token 已
轮换，自动回写设置')` —— token 写不进设置时本会话还能用（client 已带着新 token），
但设置文件里还是旧值，重启后拿它去认证多半已被判无效；而日志里两次都写着"已回写"，
"为什么过一段时间就要重新授权"就查不到了。

本文件用真实 `SettingsStore`（临时目录）+ 真实 `PixivSyncPlugin` / `PixivSession`，
只把网络客户端换成不发请求的假实现（`auth()` 直接模拟 Pixiv 轮换出新 token）：

* 写入成功：设置里是新 token、日志是 INFO "已轮换，自动回写设置"；
* 写入失败：设置里仍是旧 token、日志是 WARNING "未能保存…本会话仍可用，重启后需要
  重新授权"、不打"已回写"，且**不抛异常**（认证本身是成功的，只是没持久化）。

运行：
    venv\\Scripts\\python.exe -m unittest tests.test_pixiv_sync_session -v
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'pixiv-sync' / 'backend'
PLUGIN_LIBS = PLUGIN_BACKEND / 'libs'
for _path in (str(PLUGIN_BACKEND), str(PLUGIN_LIBS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from shell.backend.settings_store import SettingsStore

PLUGIN_NAME = 'pixiv-sync'
OLD_TOKEN = 'OLD-REFRESH-TOKEN'
NEW_TOKEN = 'ROTATED-REFRESH-TOKEN'


def _load_plugin_class():
    spec = importlib.util.spec_from_file_location(
        'pixiv_sync_main_session_test', str(PLUGIN_BACKEND / 'main.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PixivSyncPlugin


class _WriteBlockedStore(SettingsStore):
    """真实存储，只让本插件的落盘失败（模拟设置目录只读）。"""

    def _atomic_write(self, file: Path, values: Dict) -> None:
        if file.name == f'{PLUGIN_NAME}.json':
            raise OSError('设置目录只读（用例注入）')
        super()._atomic_write(file, values)


class _RotatingClient:
    """离线假客户端：`auth()` 不发请求，只模拟 Pixiv 轮换出新的 refresh_token。"""

    def __init__(self, rotated: Optional[str]) -> None:
        self._rotated = rotated
        self.refresh_token: Optional[str] = None
        self.auth_tokens: List[str] = []

    def auth(self, refresh_token: str) -> Dict[str, Any]:
        self.auth_tokens.append(refresh_token)
        self.refresh_token = self._rotated
        return {}


class _HostStub:
    """离线宿主替身：只提供 `image-viewer` 的 `get_data_root()`。

    插件构造时会按下载根恢复任务状态（`SyncRunner` → `load_task`），未配置
    `download_dir` 时根来自宿主插件。本文件只装配 pixiv-sync 自己，所以把这一处
    宿主访问换成临时目录 —— 被测的令牌回写路径不经过它。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def get_data_root(self) -> Path:
        return self._root


class AuthenticateRotationWriteBackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.settings_dir = self.home / '.config' / 'plugins'
        self.data_root = self.home / 'data'
        self.data_root.mkdir()
        # 下载根写进设置：未配置时插件会去问宿主 image-viewer，而本用例只装配了
        # 本插件，没有宿主可问
        self.downloads = self.home / 'downloads'
        self.downloads.mkdir()

    def _seed(self) -> None:
        """用**普通** store 把旧 token 播进真实设置文件。

        写失败用例里的 store 只对 `pixiv-sync.json` 拒绝写入，播种必须走另一条路，
        否则准备阶段就先失败了。
        """
        SettingsStore(str(self.settings_dir)).set(PLUGIN_NAME, {
            'refresh_token': OLD_TOKEN,
            'download_dir': str(self.downloads),
        })

    def _plugin(self, store: SettingsStore):
        cls = _load_plugin_class()
        # 构造期还没有宿主插件可问，用临时下载根顶上（见 _HostStub 的说明）
        cls._get_host = lambda self: _HostStub(self.config['directories']['data_root'])
        plugin = cls({'name': PLUGIN_NAME},
                     {'directories': {'data_root': str(self.data_root)}})
        plugin._settings_store = store
        return plugin

    def _seeded_plugin(self, store: SettingsStore):
        self._seed()
        plugin = self._plugin(store)
        client = _RotatingClient(NEW_TOKEN)
        plugin.session.cached_client = client
        return plugin, client

    def test_rotated_token_is_persisted_and_logged(self):
        store = SettingsStore(str(self.settings_dir))
        plugin, client = self._seeded_plugin(store)

        with self.assertLogs('pixiv_sync.session', level='INFO') as captured:
            result = plugin.session.authenticate()

        self.assertIsNone(result, 'authenticate 的返回值语义不变（成功返回 None）')
        self.assertEqual(store.get(PLUGIN_NAME).get('refresh_token'), NEW_TOKEN,
                         '轮换后的 token 必须落盘')
        self.assertEqual(client.auth_tokens, [OLD_TOKEN],
                         '认证应当用设置里的旧 token 发起')
        records = '\n'.join(captured.output)
        self.assertIn('INFO:pixiv_sync.session', records)
        self.assertIn('已轮换，自动回写设置', records)

    def test_write_failure_is_reported_without_claiming_write_back(self):
        store = _WriteBlockedStore(str(self.settings_dir))
        plugin, client = self._seeded_plugin(store)

        with self.assertLogs('pixiv_sync.session', level='INFO') as captured:
            result = plugin.session.authenticate()

        self.assertIsNone(result, '回写失败不改变 authenticate 的返回语义（不抛异常）')
        self.assertEqual(store.get(PLUGIN_NAME).get('refresh_token'), OLD_TOKEN,
                         '写失败时设置里应保持旧 token')
        self.assertEqual(client.refresh_token, NEW_TOKEN,
                         '本会话的 client 仍带着新 token（认证本身是成功的）')
        records = '\n'.join(captured.output)
        self.assertIn('WARNING:pixiv_sync.session', records)
        self.assertIn('未能保存', records)
        self.assertIn('本会话仍可用', records)
        self.assertIn('重启后需要重新授权', records)
        self.assertNotIn('已回写', records, '没落盘就不能说"已回写"')

    def test_unchanged_token_does_not_write_or_claim_rotation(self):
        """Pixiv 没轮换（返回同一个 token）时不该回写，也不该说"已轮换"。"""
        self._seed()
        store = SettingsStore(str(self.settings_dir))
        plugin = self._plugin(store)
        plugin.session.cached_client = _RotatingClient(OLD_TOKEN)

        plugin.session.authenticate()

        self.assertEqual(store.get(PLUGIN_NAME).get('refresh_token'), OLD_TOKEN)


if __name__ == '__main__':
    unittest.main()
