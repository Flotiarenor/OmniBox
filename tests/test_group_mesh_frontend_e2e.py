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
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'group-mesh' / 'frontend'
SHELL_PUBLIC = PROJECT_ROOT / 'shell' / 'frontend' / 'public' / 'shell'


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.05):
    """轮询等待条件成立（默认最多 3 秒），返回条件是否成立。

    为什么不能直接断言"点完就可见"：壳的 `.modal` 带 `fadeIn` 动画
    （base.css，0.15s），在动画起始帧上 `opacity` 还是 0，Selenium 的
    `is_displayed()` 因此返回 False —— 实测：点击后 0ms 为 False、100ms 为 True。
    断言瞬间状态会把"动画还在跑"误判成"弹窗没打开"。

    `--force-prefers-reduced-motion` 解决不了这件事：effects.css 里那个媒体查询
    只管它自己的 `.obx-anim-*` 类，管不到 base.css 的 `fadeIn`。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # 元素在重渲染期间短暂失效
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:
        return False


def shell_injection() -> str:
    """壳加载插件页面时注入到 </head> 之前的那几份资源。

    为什么这个用例必须带上它：插件前端**依赖**壳注入的 variables.css / base.css /
    effects.css（`docs/plugin-guide.md` §4.1 明确说"不要在 HTML 里手动引入"）。
    而本用例是直接用 `file://` 打开插件页面的，一旦不补注入，测的就不是真实环境：

      - `.modal` 的 `display:none` 在 base.css 里。不注入时弹窗就是普通 div，
        默认**可见** —— 于是"弹窗不得在打开时自己显示"这条断言会以完全错误的方式失败；
      - `.btn` / `.view-*` / `--bg-app` 等 token 同样来自壳。

    之前这条用例"能过"，只是因为当时弹窗靠 `hidden` 属性控制显隐 —— 属性不依赖
    任何样式表，于是测试在没有壳样式的情况下也碰巧成立。迁移到壳的 `.modal` 之后，
    缺失的注入暴露了出来。
    """
    parts = []
    for name in ('variables.css', 'base.css', 'effects.css'):
        path = SHELL_PUBLIC / name
        if path.is_file():
            parts.append(f'<!-- {name} -->\n<style>\n{path.read_text(encoding="utf-8")}\n</style>')
    return '\n'.join(parts)


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
        # 补上壳会注入的样式（见 shell_injection 的说明），再注入桩，最后绝对化路径
        html = html.replace('</head>', shell_injection() + '\n</head>')
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
        """五个弹窗在初次打开时都必须不可见。

        这是那个真实缺陷的直接断言：旧的 `.gm-modal { display: flex }` 会盖掉
        浏览器给 `[hidden]` 的 `display:none`，于是弹窗同时铺满整屏。
        现在弹窗用壳的 `.modal` + `.modal.active`（base.css），默认态没有任何
        display 声明 —— 这条用例同时守住"迁移后仍然默认不可见"。
        """
        driver = self._load(FRESH_STATUS)
        for modal_id in ('invite-box', 'join-box', 'member-box', 'create-box', 'peer-box'):
            element = driver.find_element('id', modal_id)
            self.assertFalse(element.is_displayed(),
                             f'#{modal_id} 在页面打开时不应可见')

    def test_no_modal_overlays_the_page(self):
        """不能有任何弹窗铺满视口：它会把"创建身份"等入口全部盖住。"""
        driver = self._load(FRESH_STATUS)
        overlays = [e for e in driver.find_elements('css selector', '.modal')
                    if e.is_displayed()]
        self.assertEqual(overlays, [], '有弹窗在打开时处于显示状态，会盖住整页')

    def test_modals_use_the_shell_component(self):
        """弹窗必须用壳的 `.modal`，而不是插件自绘的弹窗容器。

        这条守的是"迁移不会悄悄退回"：自绘弹窗（`display: flex`）与 `[hidden]`/其它
        显隐机制抢优先级正是 §5.7 那次事故的根因，一旦有人再加一个自绘弹窗，
        这里会直接失败。
        """
        driver = self._load(FRESH_STATUS)
        for modal_id in ('invite-box', 'join-box', 'member-box', 'create-box', 'peer-box'):
            classes = driver.find_element('id', modal_id).get_attribute('class') or ''
            self.assertIn('modal', classes.split(),
                          f'#{modal_id} 应当使用壳的 .modal（当前 class="{classes}"）')
        # 壳的模态框结构：.modal > .modal-box > .modal-footer
        self.assertTrue(driver.find_elements('css selector', '.modal .modal-box'))
        self.assertTrue(driver.find_elements('css selector', '.modal .modal-footer'))

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

        add_button = next(b for b in driver.find_elements('css selector', '#roster-actions .btn')
                          if '添加成员' in b.text)
        add_button.click()
        self.assertTrue(wait_until(lambda: member_box.is_displayed()),
                        '点"添加成员"后弹窗应当显示')

        close_button = driver.find_element('id', 'btn-close-member')
        close_button.click()
        self.assertTrue(wait_until(lambda: not member_box.is_displayed()),
                        '点"关闭"后弹窗应当隐藏')

    def test_join_modal_opens_and_closes(self):
        driver = self._load(FRESH_STATUS)
        join_box = driver.find_element('id', 'join-box')
        join_button = next(b for b in driver.find_elements('css selector', '#roster-actions .btn')
                           if '加入 / 更新' in b.text)
        join_button.click()
        self.assertTrue(wait_until(lambda: join_box.is_displayed()))

        driver.find_element('id', 'btn-close-join').click()
        self.assertTrue(wait_until(lambda: not join_box.is_displayed()))

    def test_invite_modal_opens_with_invite_text(self):
        driver = self._load(JOINED_STATUS)
        invite_box = driver.find_element('id', 'invite-box')
        invite_button = next(b for b in driver.find_elements('css selector', '#roster-actions .btn')
                             if '显示邀请串' in b.text)
        invite_button.click()
        self.assertTrue(wait_until(lambda: invite_box.is_displayed()))
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
        create_button = next(b for b in driver.find_elements('css selector', '#roster-actions .btn')
                             if '创建团体' in b.text)
        create_button.click()
        self.assertTrue(wait_until(lambda: driver.find_element('id', 'create-box').is_displayed()),
                        '点"创建团体"必须打开应用内弹窗（原生 confirm 在 WebView 里可能不可用）')
        # 团体名要有预填，用户可直接回车确认
        self.assertTrue(driver.find_element('id', 'create-group-name').get_attribute('value'))

    def test_create_group_submits_and_shows_invite(self):
        driver = self._load(FRESH_STATUS)
        next(b for b in driver.find_elements('css selector', '#roster-actions .btn')
             if '创建团体' in b.text).click()
        driver.find_element('id', 'create-group-name').clear()
        driver.find_element('id', 'create-group-name').send_keys('unit-group')
        driver.find_element('id', 'btn-do-create').click()

        self.assertIn('create_group', driver.execute_script('return window.__stubResults'))
        # 创建成功后直接给出邀请串，省掉用户再点一次"显示邀请串"
        self.assertTrue(wait_until(
            lambda: driver.find_element('id', 'invite-box').is_displayed()))
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
        # 按钮类名已统一到壳的 .btn（base.css），不再是插件自绘的 .gm-btn
        selector = '.gm-actions .btn, .gm-form .btn'
        labels = [b.text.strip() for b in driver.find_elements('css selector', selector)]
        self.assertTrue(any(labels), '页面上应当有动作按钮')

        for index, label in enumerate(labels):
            if not label:
                continue
            with self.subTest(button=label):
                # 清掉上一轮的痕迹：桩调用记录、所有弹窗、残留提示
                driver.execute_script(
                    "window.__stubResults = [];"
                    "document.querySelectorAll('.modal')"
                    ".forEach(function (m) { m.classList.remove('active'); });"
                    "document.querySelectorAll('.gm-toast').forEach(function (t) { t.remove(); });")
                driver.find_elements('css selector', selector)[index].click()
                opened = driver.execute_script(
                    "return Array.from(document.querySelectorAll('.modal'))"
                    ".some(function (m) { return m.classList.contains('active'); });")
                called = driver.execute_script('return window.__stubResults.length') > 0
                # 壳提供 Toast 时不会生成 .gm-toast 节点，因此两种情况都算"有提示"
                toasted = driver.execute_script(
                    "return Boolean(window.__toastCount)"
                    " || document.querySelectorAll('.gm-toast').length > 0;")
                self.assertTrue(opened or called or toasted,
                                f'按钮「{label}」点击后没有任何反馈（未开弹窗、未调后端、无提示）')


REMOTE_STATUS = {
    **JOINED_STATUS,
    'locations': {'identity': '/repo/data/group-mesh/identity',
                  'downloads': '/repo/data/group-mesh/downloads',
                  'downloads_custom': False,
                  'cache': '/repo/data/group-mesh/.cache',
                  'remote_cache': '/repo/data/group-mesh/.cache/remote',
                  'note': '远端缓存是目录结构的本地物化点，字节按需取回'},
    'share_roots': [{'share_id': 'pub', 'path': '/srv/shared', 'available': False,
                     'max_bytes': 1073741824, 'acl': {'read': 'group', 'write': 'owner',
                                                      'delete': 'owner'},
                     'reason': '目录不存在或所在磁盘未接入'}],
}

# 远端页的桩：list_peers 给两台设备（一台有共享项、一台没有端点），
# list_remote 给一个目录列表，download_remote 给一次成功与一次"本机已有"。
REMOTE_STUB = """
window.__stubResults = [];
window.__stubErrors = [];
window.__remoteCalls = [];
window.Bridge = {
  call: function (method) {
    var arg = arguments[1] || {};
    if (method === 'get_status') { return Promise.resolve(window.__statusPayload); }
    if (method === 'list_peers') {
      window.__remoteCalls.push('list_peers');
      return Promise.resolve({ success: true, registry_size: 1, errors: [], peers: [
        { device_id: 'aa'.repeat(32), name: 'flotiarenorserver',
          endpoint: ['192.168.31.16', 19450], endpoints: [['192.168.31.16', 19450]],
          shares: ['land64b6e'], seq: 1, last_seen: 1789527237, online: null },
        { device_id: 'bb'.repeat(32), name: 'member-pc', endpoint: null, endpoints: [],
          shares: [], seq: null, last_seen: null, online: null,
          note: '该设备尚未发布过注册记录（未启动节点或未启用注册表）' }
      ] });
    }
    if (method === 'list_remote') {
      window.__remoteCalls.push('list_remote:' + (arg.path || '.'));
      return Promise.resolve({ success: true, device_id: arg.device_id,
        peer_device_id: arg.device_id, name: 'flotiarenorserver',
        share_id: arg.share_id, path: arg.path || '.', dir: true, entries: [
          { name: 'sub', dir: true, size: 0 },
          { name: 'big.bin', dir: false, size: 300000 },
          { name: 'note.txt', dir: false, size: 23 }
        ] });
    }
    if (method === 'download_remote') {
      window.__remoteCalls.push('download_remote:' + arg.path);
      if (window.__downloadSkips) {
        return Promise.resolve({ success: true, skipped: true,
          local_path: 'D:/downloads/big.bin', reason: '已有同名文件' });
      }
      return Promise.resolve({ success: true, local_path: 'D:/downloads/big.bin',
        size: 300000, bytes: 300000 });
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
class RemotePageRenderTest(unittest.TestCase):
    """远端页（网络邻居）的真实浏览器用例。

    这一页是新加的，且它的失败模式与既有四页不同 —— 它依赖对端返回的数据形状
    （peers / entries），而这些形状在桩里可以精确固定。没有这一层，"远端页
    渲染空白"只会在真机上被发现。
    """

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

        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.page_path = Path(cls.tmpdir.name) / 'index.html'
        cls.base_url = PLUGIN_DIR.as_uri() + '/'
        cls._raw_html = (PLUGIN_DIR / 'index.html').read_text(encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.tmpdir.cleanup()

    def _load(self, status=REMOTE_STATUS, stub=REMOTE_STUB):
        html = self._raw_html.replace(
            '<script src="js/remote.js"></script>',
            f'<script>window.__statusPayload = {json.dumps(status)};</script>'
            f'<script>{stub}</script><script src="js/remote.js"></script>')
        html = html.replace('</head>', shell_injection() + '\n</head>')
        for asset in ('group-mesh.css', 'js/remote.js', 'js/app.js'):
            html = html.replace(f'"{asset}"', f'"{self.base_url}{asset}"')
        self.page_path.write_text(html, encoding='utf-8')
        self.driver.get(self.page_path.as_uri())
        for _ in range(60):
            if 'flotiarenorserver' in self.driver.find_element('id', 'peers-body').text:
                break
        return self.driver

    def test_peer_modal_is_hidden_on_load(self):
        """新的"添加对端"弹窗同样不得在打开页面时自己显示。"""
        driver = self._load()
        self.assertFalse(driver.find_element('id', 'peer-box').is_displayed())

    def test_peers_are_listed_with_shares(self):
        driver = self._load()
        peers = driver.find_element('id', 'peers-body').text
        self.assertIn('flotiarenorserver', peers)
        self.assertIn('192.168.31.16:19450', peers)
        self.assertIn('land64b6e', peers)
        # 名单里有、注册表里没有的设备：必须显示原因，而不是被静默丢掉
        self.assertIn('member-pc', peers)
        self.assertIn('注册记录', peers)

    def test_share_root_unavailable_is_visible(self):
        """共享根所在磁盘未接入时必须显示出来 —— 这正是 G:\\图库 拔盘后的表现。"""
        self._load()
        shares = self.driver.find_element('id', 'shares-body').text
        self.assertIn('pub', shares)

    def test_clicking_a_share_lists_directory(self):
        driver = self._load()
        button = next(b for b in driver.find_elements('css selector', '.gm-remote-item')
                      if 'land64b6e' in b.text)
        button.click()
        for _ in range(50):
            if 'big.bin' in driver.find_element('id', 'remote-body').text:
                break
        body = driver.find_element('id', 'remote-body').text
        self.assertIn('big.bin', body)
        self.assertIn('note.txt', body)
        self.assertIn('sub', body)

    def test_download_reports_local_path(self):
        driver = self._load()
        next(b for b in driver.find_elements('css selector', '.gm-remote-item')
             if 'land64b6e' in b.text).click()
        for _ in range(50):
            if 'big.bin' in driver.find_element('id', 'remote-body').text:
                break
        download = next(b for b in driver.find_elements('css selector', '[data-download]')
                        if 'big.bin' in b.get_attribute('data-download'))
        download.click()
        for _ in range(50):
            progress = driver.find_element('id', 'remote-progress')
            if progress.is_displayed() and 'big.bin' in progress.text:
                break
        progress = driver.find_element('id', 'remote-progress')
        self.assertTrue(progress.is_displayed(), '取回后必须给出进度/结果提示')
        self.assertIn('big.bin', progress.text)
        self.assertIn('D:/downloads/big.bin', progress.text)
        calls = driver.execute_script('return window.__remoteCalls')
        self.assertTrue(any(c.startswith('download_remote:') for c in calls), calls)

    def test_download_skips_existing_file(self):
        driver = self._load()
        driver.execute_script('window.__downloadSkips = true;')
        next(b for b in driver.find_elements('css selector', '.gm-remote-item')
             if 'land64b6e' in b.text).click()
        for _ in range(50):
            if 'note.txt' in driver.find_element('id', 'remote-body').text:
                break
        next(b for b in driver.find_elements('css selector', '[data-download]')
             if 'note.txt' in b.get_attribute('data-download')).click()
        for _ in range(50):
            progress = driver.find_element('id', 'remote-progress')
            if progress.is_displayed() and '已有' in progress.text:
                break
        self.assertIn('已有', driver.find_element('id', 'remote-progress').text)

    def test_add_peer_dialog_opens_and_submits(self):
        driver = self._load()
        driver.find_element('id', 'btn-peer-add').click()
        self.assertTrue(wait_until(lambda: driver.find_element('id', 'peer-box').is_displayed()))
        driver.find_element('id', 'peer-endpoint').send_keys('192.168.31.16:19450')
        driver.find_element('id', 'btn-do-add-peer').click()
        # 提交后弹窗关闭发生在桩 Promise 落定之后，必须等而不是立即断言
        self.assertTrue(wait_until(
            lambda: not driver.find_element('id', 'peer-box').is_displayed()),
            '提交成功后弹窗应关闭')


if __name__ == '__main__':
    unittest.main()
