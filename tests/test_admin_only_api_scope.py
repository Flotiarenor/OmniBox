"""壳级目录枚举端点 `system_browse_dir` 只对管理员开放。

审计项 A3（P1）：该端点返回宿主文件系统的任意绝对路径树（盘符、目录名、"含图片 /
视频 / 音乐"提示），原先只校验令牌 —— 任何持局域网令牌的成员都能逐级枚举宿主任意
目录。它是"这台机器上有什么"的直接暴露面，与同样管理员专属的 `system_pick_path`
（在服务端桌面弹窗并返回绝对路径）属于同一类。

取舍（改前端流程前必须知道）：非管理员仍可**手输**绝对路径保存 `type: "directory"`
设置项，只是用不了 `FolderPicker` 的「浏览…」目录树 ——
`shell/frontend/public/shell/folder-picker.js` 用 `Bridge.callSystem` 调本端点，
拿到 403 后对话框显示"目录读取失败"、只能取消（该文件不改：它是共享组件，
限权是按"这台机器的事实"划的，不是按某个插件的界面划的）。
桌面模式的 pywebview `js_api` 通道不经过 Flask 的 `before_request`、执行上下文里没有
主体，按既有设计不做角色判定（理由在 `main.py` 的 `_run_app` 里），因此本机窗口
（owner 令牌的持有者）仍可浏览 —— 受限的只有"别的设备上持非 owner 令牌的人"。

运行：
    python -m unittest tests.test_admin_only_api_scope -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import _ADMIN_ONLY_API, create_app
from shell.backend.principal import ROLE_ADMIN, ROLE_MEMBER, PrincipalStore
from tests.test_file_server_paths import _StubPluginManager


class SystemBrowseDirAdminOnlyTests(unittest.TestCase):
    """真实 Flask app + 真实凭据表：按令牌区分管理员与普通成员。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self.config_dir = home / '.config'
        self.data_root = home / 'data'
        self.data_root.mkdir()
        # 可枚举的目录：浏览器里点「浏览…」看到的正是这一族内容
        self.browse_root = home / '图库'
        (self.browse_root / '相册').mkdir(parents=True)

        # 隔离配置目录：用例会在里面写凭据表，不能落到工作区的 .config
        patch = mock.patch('shell.backend.file_server.get_config_dir',
                           return_value=self.config_dir)
        patch.start()
        self.addCleanup(patch.stop)

        config = {'server': {'host': '127.0.0.1', 'port': 18083},
                  'directories': {'data_root': str(self.data_root)}}
        self.app = create_app(config, _StubPluginManager())
        self.client = self.app.test_client()
        self.owner_token = get_or_create_token(self.config_dir)
        self.store = PrincipalStore(self.config_dir)

    def _post(self, token, path):
        payload = {'args': [], 'kwargs': {'path': str(path)}}
        headers = {} if token is None else {TOKEN_HEADER: token}
        return self.client.post('/api/system_browse_dir', json=payload, headers=headers)

    def test_member_is_forbidden(self):
        """验收项：非管理员令牌枚举宿主机目录 → 403。"""
        self.store.add('alice', 'alice-token', role=ROLE_MEMBER, name='Alice')
        resp = self._post('alice-token', self.browse_root)
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('相册', resp.get_data(as_text=True),
                         '被拒的响应里不该带目录内容')

    def test_owner_sees_the_directory_tree(self):
        """本机使用者（自举成 owner）不受影响：鉴权通过并真的返回子目录。"""
        resp = self._post(self.owner_token, self.browse_root)
        self.assertEqual(resp.status_code, 200)
        result = resp.get_json()['result']
        names = [entry['name'] for entry in result['entries']]
        self.assertIn('相册', names)

    def test_admin_role_is_allowed(self):
        self.store.add('carol', 'carol-token', role=ROLE_ADMIN, name='Carol')
        resp = self._post('carol-token', self.browse_root)
        self.assertEqual(resp.status_code, 200)

    def test_missing_token_is_still_401_not_403(self):
        """未认证是 401（前端会引导取令牌），已认证但权限不够才是 403。"""
        self.assertEqual(self._post(None, self.browse_root).status_code, 401)

    def test_endpoint_is_registered_in_the_admin_only_list(self):
        """结构守卫：从清单里删掉这一项就等于限权消失，行为用例不会替它报警。"""
        self.assertIn('system_browse_dir', _ADMIN_ONLY_API)


if __name__ == '__main__':
    unittest.main()
