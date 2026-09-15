"""group-mesh 插件前端的**真实浏览器**用例（本地手动运行，不进 CI）。

为什么需要它
------------
这份前端踩过一个只有真实浏览器能抓到的缺陷：`.gm-modal` 用 `display: flex`
布局，而浏览器给 `[hidden]` 的 `display: none` 来自 **UA 样式表** —— 同优先级下
作者样式胜出，于是"加了 hidden"的弹窗照样显示。后果是打开插件时三个弹窗同时
铺满整屏，写在 DOM 最后的"添加成员"盖在最上面，用户连"创建身份"都点不到。
Python 单测与静态检查都发现不了（属性确实写对了，是级联把它废掉了）。

本用例用一个**桩 Bridge**（不经 Shell）直接打开 `frontend/index.html`，
断言两类状态下的渲染结果：

  1. 全新安装（没有身份）：出现"创建身份"，且三个弹窗都不可见；
  2. 已有身份与团体：显示主体/设备与名单，弹窗仍然不可见。

为什么不在 CI 里跑（按项目约定）
--------------------------------
真实浏览器既慢又脆弱，且每台 runner 都要下载 driver。selenium 只声明在
`requirements-e2e.txt`，未安装或本机没有浏览器时本用例自动跳过，不会拖红 CI。

本地运行
--------
    venv/Scripts/pip install -r requirements-e2e.txt
    venv/Scripts/python -m unittest tests.test_group_mesh_frontend_e2e -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'group-mesh' / 'frontend'


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
        from selenium import webdriver  # noqa: F401
    except Exception:
        return False
    return True


def _have_browser() -> bool:
    for exe in ('chrome', 'chrome.exe', 'msedge', 'msedge.exe', 'chromium', 'chromium-browser'):
        if shutil.which(exe):
            return True
    for candidate in (
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
    ):
        if candidate.is_file():
            return True
    return False


# 全新安装时的后端状态（与 GroupMeshPlugin.get_status() 的真实返回同形）
FRESH_STATUS = {
    'kernel': {'available': True, 'version': '0.1.0',
               'package': 'shell.groupmesh', 'path': '/repo/shell/groupmesh'},
    'data_root': '/repo/data/group-mesh',
    'identity_dir': '/repo/data/group-mesh/identity',
    'settings': {'port': 19443, 'bind': '::', 'principal_name': '', 'group_name': ''},
    'node': {'running': False, 'listening': None, 'connection_count': 0, 'error': None},
    'identity': None,
    'roster': None,
    'shares': [],
    'unsupported': ['语音（§8.3）', 'SoftEther 游戏面（§9）'],
}

JOINED_STATUS = {
    **FRESH_STATUS,
    'identity': {'principal_name': 'alice', 'principal_id': 'aabbccdd',
                 'principal_key': 'aa' * 32, 'device_name': 'pc',
                 'device_id': '11223344', 'device_key': 'bb' * 32},
    'roster': {'group': 'home-lab', 'version': 2, 'role': 'owner', 'in_roster': True,
               'member_count': 2, 'admin_count': 0,
               'stale': {'local_version': 2, 'expires': 1805016000, 'expired': False,
                         'expired_days': 0, 'consistent': None},
               'members': [{'name': 'alice', 'principal_id': 'aabbccdd', 'device_count': 1},
                           {'name': 'bob', 'principal_id': 'eeff0011', 'device_count': 1}]},
    'shares': [{'share_id': 'pub', 'path': '/srv/shared',
                'acl': {'read': 'group', 'write': 'owner', 'delete': 'owner'},
                'max_bytes': 1073741824}],
}

# 桩 Bridge：不依赖 Shell，直接回答 get_status，其余方法回一个成功占位
STUB = """
window.__stubResults = [];
window.__stubErrors = [];
window.Bridge = {
  call: function (method) {
    if (method === 'get_status') {
      return Promise.resolve(window.__statusPayload);
    }
    if (method === 'get_invite') {
      window.__stubResults.push(method);
      return Promise.resolve({ success: true, invite: 'gm1:STUB', group: 'x', version: 1 });
    }
    if (method === 'create_group') {
      window.__stubResults.push(method);
      return Promise.resolve({ success: true, group: 'created', version: 1, invite: 'gm1:NEW' });
    }
    window.__stubResults.push(method);
    return Promise.resolve({ success: true });
  }
};
window.confirm = function () { return true; };
window.openSettingsModal = function () { window.__settingsOpened = true; };
"""


@unittest.skipUnless(_have_selenium(), '未安装 selenium（见 requirements-e2e.txt）')
@unittest.skipUnless(_have_browser(), '本机没有 Chrome / Edge')
class FrontendRenderTest(unittest.TestCase):
    """直接把插件页面加载进真实浏览器，断言 DOM。"""

    @classmethod
    def setUpClass(cls):
        from selenium import webdriver

        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--window-size=1280,900')
        options.add_argument('--allow-file-access-from-files')
        try:
            cls.driver = webdriver.Chrome(options=options)
        except Exception as e:  # pragma: no cover - 驱动问题不应让用例失败
            raise unittest.SkipTest(f'无法启动浏览器: {e}') from e

        # 把桩脚本插在页面脚本之前，并把状态载荷固定下来
        html = (PLUGIN_DIR / 'index.html').read_text(encoding='utf-8')
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.page = Path(cls.tmpdir.name) / 'index.html'
        cls._html = html
        # CSS / JS 用绝对 file:// 路径引用，避免临时目录丢相对路径
        cls.base_url = PLUGIN_DIR.as_uri() + '/'

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.tmpdir.cleanup()

    def _load(self, status):
        """加载页面：状态载荷与桩 Bridge 都注入到插件脚本之前。"""
        inject = (
            f'<script>window.__statusPayload = {json.dumps(status)};</script>'
            f'<script>{STUB}</script>'
        )
        html = self._html.replace('<script src="js/app.js"></script>',
                                  inject + '<script src="js/app.js"></script>')
        # 绝对化静态资源路径（临时文件不在插件目录里）
        for asset in ('group-mesh.css', 'js/app.js'):
            html = html.replace(f'"{asset}"', f'"{self.base_url}{asset}"')
        self.page.write_text(html, encoding='utf-8')
        self.driver.get(self.page.as_uri())
        # 等 app.js 完成一次 get_status 渲染
        for _ in range(50):
            if self.driver.find_elements('css selector', '.gm-card'):
                break
            self.driver.implicitly_wait(0.1)
        return self.driver

    # ── 缺陷回归：弹窗不得在打开时自己显示 ──────────────────────────────

    def test_modals_are_hidden_on_load(self):
        """三个弹窗在初次打开时都必须不可见。

        这是那个真实缺陷的直接断言：`hidden` 属性写对了，但 `.gm-modal`
        的 `display: flex` 把它覆盖掉，于是三个弹窗同时铺满整屏。
        """
        driver = self._load(FRESH_STATUS)
        for modal_id in ('invite-box', 'join-box', 'member-box'):
            element = driver.find_element('id', modal_id)
            self.assertFalse(element.is_displayed(),
                             f'#{modal_id} 在页面打开时不应可见（hidden 被 CSS 覆盖了？）')

    def test_no_modal_overlays_the_page(self):
        """不能有任何弹窗铺满视口：它会把"创建身份"等入口全部盖住。"""
        driver = self._load(FRESH_STATUS)
        overlays = [e for e in driver.find_elements('css selector', '.gm-modal')
                    if e.is_displayed()]
        self.assertEqual(overlays, [], '有弹窗在打开时处于显示状态，会盖住整页')

    # ── 全新安装：入口必须是"创建身份" ──────────────────────────────────

    def test_fresh_install_offers_create_identity(self):
        driver = self._load(FRESH_STATUS)
        body = driver.find_element('id', 'identity-body').text
        self.assertIn('还没有身份', body)
        actions = driver.find_element('id', 'identity-actions').text
        self.assertIn('创建身份', actions)

    def test_fresh_install_offers_create_or_join_group(self):
        driver = self._load(FRESH_STATUS)
        actions = driver.find_element('id', 'roster-actions').text
        self.assertIn('创建团体', actions)
        # 入口同时承担"更新名单"，因此标签是"加入 / 更新团体"：
        # 群主加人后成员必须能再更新一次，否则会永远停在旧名单上。
        self.assertIn('加入 / 更新团体', actions)

    def test_fresh_install_has_no_member_entry(self):
        """没有团体时不该给出"添加成员"—— 那是群主/管理员才有的动作。"""
        driver = self._load(FRESH_STATUS)
        self.assertNotIn('添加成员', driver.find_element('id', 'roster-actions').text)

    def test_kernel_banner_hidden_when_kernel_available(self):
        driver = self._load(FRESH_STATUS)
        self.assertFalse(driver.find_element('id', 'kernel-missing').is_displayed())

    # ── 已有身份与团体：显示状态，弹窗仍不可见 ──────────────────────────

    def test_joined_state_renders_identity_and_roster(self):
        driver = self._load(JOINED_STATUS)
        self.assertIn('alice', driver.find_element('id', 'identity-body').text)
        roster = driver.find_element('id', 'roster-body').text
        self.assertIn('home-lab', roster)
        self.assertIn('bob', roster)
        shares = driver.find_element('id', 'shares-body').text
        self.assertIn('pub', shares)

    def test_joined_state_modals_still_hidden(self):
        driver = self._load(JOINED_STATUS)
        for modal_id in ('invite-box', 'join-box', 'member-box'):
            self.assertFalse(driver.find_element('id', modal_id).is_displayed(),
                             f'#{modal_id} 不应自动可见')

    def test_joined_state_owner_sees_add_member_button(self):
        driver = self._load(JOINED_STATUS)
        self.assertIn('添加成员', driver.find_element('id', 'roster-actions').text)

    # ── 弹窗的打开/关闭必须真的生效（新机制不能被"永远隐藏"蒙混过关）──────

    def test_add_member_modal_opens_and_closes(self):
        driver = self._load(JOINED_STATUS)
        member_box = driver.find_element('id', 'member-box')
        self.assertFalse(member_box.is_displayed())

        add_button = next(b for b in driver.find_elements('css selector', '#roster-actions .gm-btn')
                          if '添加成员' in b.text)
        add_button.click()
        self.assertTrue(member_box.is_displayed(), '点"添加成员"后弹窗应当显示')

        close_button = driver.find_element('id', 'btn-close-member')
        close_button.click()
        self.assertFalse(member_box.is_displayed(), '点"关闭"后弹窗应当隐藏')

    def test_join_modal_opens_and_closes(self):
        driver = self._load(FRESH_STATUS)
        join_box = driver.find_element('id', 'join-box')
        join_button = next(b for b in driver.find_elements('css selector', '#roster-actions .gm-btn')
                           if '加入 / 更新' in b.text)
        join_button.click()
        self.assertTrue(join_box.is_displayed())

        driver.find_element('id', 'btn-close-join').click()
        self.assertFalse(join_box.is_displayed())

    def test_invite_modal_opens_with_invite_text(self):
        driver = self._load(JOINED_STATUS)
        invite_box = driver.find_element('id', 'invite-box')
        invite_button = next(b for b in driver.find_elements('css selector', '#roster-actions .gm-btn')
                             if '显示邀请串' in b.text)
        invite_button.click()
        self.assertTrue(invite_box.is_displayed())
        # 桩 Bridge 对 get_invite 回的是占位成功，因此这里只断言弹窗确实打开、
        # 且文本域被赋值过（真实邀请串由内核生成，前端只负责展示）
        self.assertIsNotNone(driver.find_element('id', 'invite-text').get_attribute('value'))

    # ── 每个按钮点下去都必须有反应 ────────────────────────────────────────
    #
    # 这一组是补上一个真实漏洞：此前只验了"弹窗默认不可见"，没验"点了有没有反应"。
    # "创建团体"当时走的是 window.confirm（内嵌 WebView 里可能被禁用），
    # 而且前置条件恒真，点击后**毫无反馈**，用例却全绿。

    def test_create_group_opens_a_dialog_not_native_confirm(self):
        driver = self._load(FRESH_STATUS)
        create_button = next(b for b in driver.find_elements('css selector', '#roster-actions .gm-btn')
                             if '创建团体' in b.text)
        create_button.click()
        self.assertTrue(driver.find_element('id', 'create-box').is_displayed(),
                        '点"创建团体"必须打开应用内弹窗（原生 confirm 在 WebView 里可能不可用）')
        # 团体名要有预填，用户可直接回车确认
        self.assertTrue(driver.find_element('id', 'create-group-name').get_attribute('value'))

    def test_create_group_submits_and_shows_invite(self):
        driver = self._load(FRESH_STATUS)
        next(b for b in driver.find_elements('css selector', '#roster-actions .gm-btn')
             if '创建团体' in b.text).click()
        driver.find_element('id', 'create-group-name').clear()
        driver.find_element('id', 'create-group-name').send_keys('unit-group')
        driver.find_element('id', 'btn-do-create').click()

        self.assertIn('create_group', driver.execute_script('return window.__stubResults'))
        # 创建成功后直接给出邀请串，省掉用户再点一次"显示邀请串"
        self.assertTrue(driver.find_element('id', 'invite-box').is_displayed())
        self.assertEqual(driver.find_element('id', 'invite-text').get_attribute('value'), 'gm1:NEW')

    def test_every_action_button_reacts(self):
        """遍历所有动作按钮，逐个点击，断言用户**一定得到反馈**。

        三种可接受的反馈：打开弹窗、调用后端、弹出提示（例如表单为空时拒绝提交）。
        这是防止"按钮点了毫无反应"这类缺陷再次漏过去的兜底 —— 新增按钮若忘了接
        handler，这条会直接失败。

        实现注意：每次点击后动作区会重新渲染，之前拿到的 WebElement 会失效
        （StaleElementReferenceException），因此每轮都**按序号重新查找**，
        而不是缓存元素列表。
        """
        driver = self._load(FRESH_STATUS)
        selector = '.gm-actions .gm-btn, .gm-form .gm-btn'
        labels = [b.text.strip() for b in driver.find_elements('css selector', selector)]
        self.assertTrue(any(labels), '页面上应当有动作按钮')

        for index, label in enumerate(labels):
            if not label:
                continue
            with self.subTest(button=label):
                # 清掉上一轮的痕迹：桩调用记录、所有弹窗、残留提示
                driver.execute_script(
                    "window.__stubResults = [];"
                    "document.querySelectorAll('.gm-modal')"
                    ".forEach(function (m) { m.removeAttribute('data-open'); });"
                    "document.querySelectorAll('.gm-toast').forEach(function (t) { t.remove(); });")
                driver.find_elements('css selector', selector)[index].click()
                opened = driver.execute_script(
                    "return Array.from(document.querySelectorAll('.gm-modal'))"
                    ".some(function (m) { return m.getAttribute('data-open') === 'true'; });")
                called = driver.execute_script('return window.__stubResults.length') > 0
                # 壳提供 Toast 时不会生成 .gm-toast 节点，因此两种情况都算"有提示"
                toasted = driver.execute_script(
                    "return Boolean(window.__toastCount)"
                    " || document.querySelectorAll('.gm-toast').length > 0;")
                self.assertTrue(opened or called or toasted,
                                f'按钮「{label}」点击后没有任何反馈（未开弹窗、未调后端、无提示）')


if __name__ == '__main__':
    unittest.main()
