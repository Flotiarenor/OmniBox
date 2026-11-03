"""pixiv-sync 内置 OAuth 向导：token 没落盘时不能对调用方报"授权成功"。

`finish()` 用授权码换到新 token 后回写 `refresh_token`。原先它不检查
`update_setting` 的返回值，一路 `return {"ok": True, "user_id": ...}` —— 设置文件写不
进去时，界面显示"Token 获取成功并已保存"，重启后设置里仍是旧值/空值，用户得重新
授权，且没有任何提示。

本文件锁两条路径（离线假 client，不联网、不 mock 调用次数）：

* 写入失败：`ok=False`、error 含"未能保存"、**不重置**缓存的客户端（重置是"新 token
  已生效"的收尾动作），授权码仍被清理（一次性）；
* 写入成功：返回结构仍是 `{ok, user_id}`（前端 app.js / native.js 依赖），token 落进
  设置，并按"新 token 生效"丢弃缓存的客户端。

运行：
    venv\\Scripts\\python.exe -m unittest tests.test_pixiv_sync_oauth -v
"""

import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'pixiv-sync' / 'backend'
PLUGIN_LIBS = PLUGIN_BACKEND / 'libs'
for _path in (str(PLUGIN_BACKEND), str(PLUGIN_LIBS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pixiv_sync import oauth

CODE = 'AUTH-CODE'
VERIFIER = 'PKCE-VERIFIER'
NEW_TOKEN = 'NEW-REFRESH-TOKEN'
USER_ID = '4242'


class _FakeClient:
    """离线假 Pixiv 客户端：只实现向导真正用到的那一步。"""

    def __init__(self, token: str = NEW_TOKEN, user_id: str = USER_ID) -> None:
        self._issued_token = token
        self.user_id = user_id
        self.refresh_token = ''
        self.last_auth: Optional[tuple] = None

    def auth_with_code(self, code: str, verifier: str) -> None:
        # 记下壳实际送进来的入参，供"去空白、带 PKCE verifier"断言使用
        self.last_auth = (code, verifier)
        self.refresh_token = self._issued_token


class _FakePlugin:
    """只提供 `oauth.finish()` 需要的那几个成员，写入结果可控。"""

    def __init__(self, persist_ok: bool) -> None:
        self._lock = threading.RLock()
        self._thread = None
        self._oauth_verifier: Optional[str] = VERIFIER
        self._persist_ok = persist_ok
        self._persisted: Dict[str, Any] = {}
        self.client_resets = 0
        self._client_obj: Optional[_FakeClient] = _FakeClient()

    def _client(self) -> _FakeClient:
        assert self._client_obj is not None
        return self._client_obj

    def update_setting(self, key: str, value: Any) -> bool:
        self._persisted[key] = value
        return self._persist_ok

    @property
    def _pixiv_client(self) -> Optional[_FakeClient]:
        return self._client_obj

    @_pixiv_client.setter
    def _pixiv_client(self, value: Optional[_FakeClient]) -> None:
        self.client_resets += 1
        self._client_obj = value

    def persisted(self) -> Dict[str, Any]:
        return dict(self._persisted)


class OAuthFinishTests(unittest.TestCase):
    def test_token_not_persisted_reports_failure(self):
        plugin = _FakePlugin(persist_ok=False)

        result = oauth.finish(plugin, f'  {CODE}  ')

        self.assertFalse(result.get('ok'), f'写失败却回了成功: {result}')
        self.assertIn('未能保存', result.get('error', ''))
        self.assertNotIn('user_id', result, '失败时不该带成功字段')
        # 授权动作本身发生了（Pixiv 已发出新 token），但内存态不得被当成"已生效"
        self.assertEqual(plugin.client_resets, 0, 'token 没落盘时不该重置缓存客户端')
        self.assertIsNotNone(plugin._pixiv_client)
        self.assertIn('refresh_token', plugin.persisted())
        # 授权码是一次性的：失败路径同样要清掉，避免重放
        self.assertIsNone(plugin._oauth_verifier)

    def test_token_persisted_keeps_success_shape(self):
        plugin = _FakePlugin(persist_ok=True)

        result = oauth.finish(plugin, CODE)

        self.assertIs(result.get('ok'), True)
        self.assertEqual(result.get('user_id'), USER_ID)
        self.assertEqual(plugin.persisted().get('refresh_token'), NEW_TOKEN)
        self.assertIsNone(plugin._oauth_verifier)
        # 新 token 已落盘：按"新 token 生效"丢弃缓存客户端，下一次调用重新认证
        self.assertEqual(plugin.client_resets, 1)
        self.assertIsNone(plugin._pixiv_client)

    def test_code_and_verifier_are_passed_to_the_client(self):
        """入参形状不变：授权码去空白、PKCE verifier 照旧送进去。"""
        plugin = _FakePlugin(persist_ok=True)
        client = plugin._client()

        oauth.finish(plugin, f' {CODE}\n')

        self.assertEqual(client.last_auth, (CODE, VERIFIER))


if __name__ == '__main__':
    unittest.main()
