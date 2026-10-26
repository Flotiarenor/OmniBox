"""插件页面骨架的浏览器验收 CLI。

为什么需要它
------------
声明了 ``frontend.chrome`` 的插件，页面骨架（品牌块 / 左栏 / 导航 / 顶栏 /
状态栏 / 分隔条）由壳的 ``PluginChrome`` 渲染，插件 iframe 只装内容。这条契约
横跨 manifest、Vue 组件、壳注入的共享脚本与插件前端四层，任何一层退回去自己
画一遍骨架，页面都"看起来正常"，只有读 DOM 才能戳穿：

- 壳没有渲染 ``.plugin-page`` / ``.plugin-chrome-sidebar`` / ``.plugin-title``；
- 内容 iframe 里又冒出 ``.view-sub-sidebar`` / ``.view-toolbar`` 等骨架类名
  （双份 chrome，视觉上往往只差几像素）；
- 点壳侧导航后内容 panel 没切，或切了但壳标题没跟上；
- 分隔条 hover 不出 2px 强调色指示线；
- manifest 声明了 ``chrome.freshness`` 但壳侧没挂上 ``.obx-fresh``；
- 设置弹窗改值保存后重开没回填、搜索框收不到 ``omnibox:query`` 或切视图后不清空、
  ``status.kind=eq-text`` 播放态没渲染四柱 EQ；
- 页面与控制台有 ``SEVERE`` 错误。

``tests/debug_nav_style_ui.py`` 只覆盖四个插件的选中态，上面这些都能在它全绿
时同时存在。group-mesh pilot 用一次性 Selenium 探针验证过这些断言，但探针把
插件名、端口与等待时间写死在代码里。本工具把"查哪些插件、查什么"与"怎么开
浏览器"分开：前者是纯函数，可以用 ``unittest`` 覆盖；后者集中在
``audit_plugin()`` 与 ``ShellServer``。

用法
----
::

    venv/Scripts/python.exe tools/plugin_ui_audit.py
    venv/Scripts/python.exe tools/plugin_ui_audit.py --plugin group-mesh
    venv/Scripts/python.exe tools/plugin_ui_audit.py --base-url http://127.0.0.1:18080
    venv/Scripts/python.exe tools/plugin_ui_audit.py --json .build/plugin-ui-audit.json

默认行为：在空闲端口上启动 ``main.py --web-only``，轮询 ``/health`` 就绪后
用无头 Chrome 审计每个声明了 ``frontend.chrome`` 的插件，结束时杀掉自己起的
服务与 driver。媒体播放器另起一个临时用户数据根的隔离实例，跑设置保存往返、
搜索回填/清空与 mini EQ 三项交互检查。给了 ``--base-url`` 就复用已经启动的壳，
不再起服务（也不跑会写设置的交互检查）。

退出码：0 全部**检查项** PASS；1 有 FAIL；2 环境/参数错误（连不上壳、
缺 Chrome/selenium、没有可审计的插件等）；3 没有 FAIL 但有 SKIP（存在未验证的
检查项 —— SKIP 不等于通过，汇总里单独计数，不再被插件的 PASS 吞掉）。

已知限制
--------
- ``nav.panel`` 只认壳侧 ``.plugin-page[data-active-view]`` 与导航项
  ``data-view-id``（由 ``PluginChrome`` 渲染）；旧 dist 缺少标记时记 SKIP 而不是猜。
  ``nav.title`` 仍按壳标题是否变化判定：视图已切换、标题按设计保持常量时记 SKIP。
- 导航项按壳 DOM 顺序映射回 manifest 的**可见** nav id（声明层 ``visible: false``
  的整组或单项不计数）；两者数量不一致时记 SKIP（运行期动态导航项不猜映射）。
- manifest 的 ``freshness: {}`` 视为已声明（JS 里空对象为真、壳会挂控件），
  ``freshness: false`` 才是显式关闭。
- 分隔条断言依赖壳的 ``splitter.js`` 注入的 ``[data-obx-splitter-handle]``，
  并用 CDP 在真实视口坐标上制造 hover；它验证的是壳的指示线，不是拖动值持久化。
- 需要已构建的 ``shell/frontend/dist``（``npm --prefix shell/frontend run build``），
  壳页面由它提供；dist 缺失时壳首页直接 404，本工具会以环境错误退出。
- 需要 selenium 与 Chrome。浏览器优先用 ``tests/harness/browser_binary.py`` 的
  ``browser_binary()``；首次运行可能由 Selenium Manager 在仓库根生成
  ``chromedriver/`` 与 ``se-metadata.json``，两者都在 .gitignore 里。
- 设置弹窗保存往返、搜索回填/清空与 mini EQ 在**独立隔离实例**（临时用户数据根）
  上检查，见 ``audit_interactive()``；``--base-url`` 复用外部实例时跳过这三项
  （设置往返会写插件设置）。顶栏动作是否"恰好执行一次"需要后端调用计数，仍不在
  本工具范围内。
- 不截图：所有结论来自 DOM、计算样式与 rect，避免仓库里出现会过期的图片。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLUGINS_ROOT = PROJECT_ROOT / 'plugins'
DEFAULT_TIMEOUT = 30.0
POLL_INTERVAL = 0.2
WINDOW_SIZE = (1440, 960)
#: 交互验收（设置往返 / 搜索 / mini EQ）使用的插件：声明了 chrome.search、freshness，
#: 并在壳内设置弹窗白名单里（SHELL_SETTINGS_PLUGINS）。
INTERACTIVE_PLUGIN = 'media-player'
#: 让壳把整个用户数据根放到临时目录，交互验收不污染真实配置。
#: 与 shell/backend/paths.py 的 USER_DATA_DIR_ENV 保持一致（用例会断言两者相等）。
HOME_ENV = 'OMNIBOX_HOME'

PASS = 'PASS'
FAIL = 'FAIL'
SKIP = 'SKIP'
#: 没有 FAIL、但存在 SKIP（未验证项）时的退出码：与"全部通过"区分开。
EXIT_UNVERIFIED = 3


# ===== 纯逻辑：目标发现、参数、结果汇总 =====
# 这一段不 import selenium，也不碰浏览器，供 tests/test_plugin_ui_audit.py 直接覆盖。


@dataclass(frozen=True)
class NavItem:
    """manifest 里 ``frontend.chrome.nav[].items[]`` 的一项。

    ``visible`` 是**声明层**显隐：所属组或该项本身写了 ``"visible": false`` 时壳
    不渲染它，审计也不把它算进"应渲染项数"（document-reader 的「当前文档」组就是
    整组 visible:false）。运行期动态显隐不在这里，靠壳侧 DOM 数量与实际标记判断。
    """

    id: str
    label: str
    visible: bool = True


@dataclass(frozen=True)
class PluginTarget:
    """一个声明了 ``frontend.chrome`` 的插件。"""

    name: str
    display_name: str
    route: str
    nav_items: tuple[NavItem, ...]
    toolbar_ids: tuple[str, ...]
    has_freshness: bool
    manifest_path: Path

    @property
    def visible_nav_items(self) -> tuple[NavItem, ...]:
        """壳按 manifest 实际会渲染的导航项（声明层 visible:false 已剔除）。"""
        return tuple(item for item in self.nav_items if item.visible)


@dataclass(frozen=True)
class Discovery:
    """manifest 扫描结果。

    ``problems`` 只收集"读不了"的 manifest（JSON 坏了、顶层不是对象）。它们不
    参与本次审计，但必须让运行者看见 —— 静默跳过等于把坏 manifest 当成没有
    chrome 的插件。
    """

    targets: tuple[PluginTarget, ...]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    check: str
    status: str
    detail: str = ''

    def as_dict(self) -> dict[str, str]:
        return {'check': self.check, 'status': self.status, 'detail': self.detail}


@dataclass
class PluginReport:
    name: str
    display_name: str
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, check: str, status: str, detail: str = '') -> None:
        self.checks.append(CheckResult(check, status, detail))

    @property
    def status(self) -> str:
        statuses = {check.status for check in self.checks}
        if FAIL in statuses:
            return FAIL
        if PASS in statuses:
            return PASS
        return SKIP

    def as_dict(self) -> dict[str, Any]:
        return {
            'plugin': self.name,
            'display_name': self.display_name,
            'status': self.status,
            'checks': [check.as_dict() for check in self.checks],
        }


@dataclass
class AuditReport:
    base_url: str = ''
    reports: list[PluginReport] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(report.status == FAIL for report in self.reports)

    def counts(self) -> dict[str, int]:
        counts = {PASS: 0, FAIL: 0, SKIP: 0}
        for report in self.reports:
            counts[report.status] += 1
        return counts

    def check_counts(self) -> dict[str, int]:
        """按**检查项**计数。

        插件的状态是"有 PASS 就算 PASS"，只看它会把同一个插件里没验证的 SKIP
        吞掉（实测出现过 5 个插件全部 PASS、若干项检查 SKIP，汇总却显示 SKIP 0）。
        汇总与退出码必须使用这里的计数，否则"没验证"会被当成"通过"。
        """
        counts = {PASS: 0, FAIL: 0, SKIP: 0}
        for report in self.reports:
            for check in report.checks:
                counts[check.status] += 1
        return counts

    @property
    def unverified(self) -> bool:
        """是否存在没有真正执行的检查项。"""
        return self.check_counts()[SKIP] > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            'base_url': self.base_url,
            'ok': not self.failed and not self.unverified,
            'exit_code': audit_exit_code(self),
            'counts': self.counts(),
            'check_counts': self.check_counts(),
            'plugins': [report.as_dict() for report in self.reports],
        }


def audit_exit_code(report: AuditReport) -> int:
    """报告对应的退出码：FAIL > 未验证 > 通过。"""
    if report.failed:
        return 1
    if report.unverified:
        return EXIT_UNVERIFIED
    return 0


@dataclass(frozen=True)
class Options:
    plugins: tuple[str, ...]
    base_url: str | None
    json_path: Path | None
    timeout: float


def _manifest_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('manifest 顶层不是 JSON 对象')
    return data


def _nav_items_of(chrome: dict[str, Any]) -> tuple[NavItem, ...]:
    items: list[NavItem] = []
    groups = chrome.get('nav')
    if not isinstance(groups, list):
        return ()
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_visible = group.get('visible') is not False
        raw_items = group.get('items')
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get('id') or '').strip()
            if not item_id:
                continue
            label = str(raw.get('label') or '').strip() or item_id
            items.append(NavItem(item_id, label, group_visible and raw.get('visible') is not False))
    return tuple(items)


def _has_freshness_declared(chrome: dict[str, Any]) -> bool:
    """manifest 是否声明了 ``freshness``（含空对象 ``{}``，但排除显式 false）。

    JS 里 ``{}`` 为真、``PluginChrome`` 会挂控件；Python 的 ``bool({})`` 为假，直接
    做真值判断会把 ``freshness: {}`` 当成"未声明"而 SKIP。``freshness: false`` 是
    显式关闭，壳不挂控件，审计也不该要求它。
    """
    return 'freshness' in chrome and chrome.get('freshness') is not False


def _toolbar_ids_of(chrome: dict[str, Any]) -> tuple[str, ...]:
    raw_actions = chrome.get('toolbar')
    if not isinstance(raw_actions, list):
        return ()
    ids = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        action_id = str(raw.get('id') or '').strip()
        if action_id:
            ids.append(action_id)
    return tuple(ids)


def discover_chrome_plugins(plugins_root: Path = DEFAULT_PLUGINS_ROOT) -> Discovery:
    """扫描 ``plugins/*/manifest.json``，返回声明了 ``frontend.chrome`` 的插件。

    判定用 ``isinstance(chrome, dict)``，不是真值判断：壳前端看到的是 JS 对象，
    ``chrome: {}`` 在 JS 里为真（会渲染一个没有导航的骨架），在 Python 里为假。
    用 Python 真值判断会把这种插件悄悄漏掉，而它恰恰是最该被审计的一种。
    """
    if not plugins_root.is_dir():
        return Discovery((), (f'插件目录不存在：{plugins_root}',))

    targets: list[PluginTarget] = []
    problems: list[str] = []
    for manifest_path in sorted(plugins_root.glob('*/manifest.json')):
        try:
            manifest = _manifest_object(manifest_path)
        except (OSError, ValueError) as exc:
            problems.append(f'{manifest_path.parent.name}: manifest 读取失败（{exc}）')
            continue
        frontend = manifest.get('frontend')
        if not isinstance(frontend, dict):
            continue
        chrome = frontend.get('chrome')
        if not isinstance(chrome, dict):
            continue
        fallback_name = manifest_path.parent.name
        name = str(manifest.get('name') or fallback_name).strip() or fallback_name
        display_name = str(manifest.get('displayName') or name).strip() or name
        route = str(frontend.get('route') or '').strip()
        targets.append(
            PluginTarget(
                name=name,
                display_name=display_name,
                route=route,
                nav_items=_nav_items_of(chrome),
                toolbar_ids=_toolbar_ids_of(chrome),
                has_freshness=_has_freshness_declared(chrome),
                manifest_path=manifest_path,
            )
        )
    targets.sort(key=lambda target: target.name)
    return Discovery(tuple(targets), tuple(problems))


def select_targets(
    targets: Sequence[PluginTarget],
    requested: Iterable[str],
) -> tuple[list[PluginTarget], list[str]]:
    """按 ``--plugin`` 过滤；返回（选中的目标, 未知名字）。

    请求顺序被保留（同名只取一次），未点名时按 manifest 顺序。未知名字交给调用
    方报错，而不是静默忽略：``--plugin group-mesh,group_mesh`` 里的笔误如果只是
    少跑一个插件，工具的"全绿"就成了假象。
    """
    wanted = [name.strip() for name in requested if name and name.strip()]
    if not wanted:
        return list(targets), []
    by_name = {target.name: target for target in targets}
    picked: list[PluginTarget] = []
    unknown: list[str] = []
    for name in dict.fromkeys(wanted):
        target = by_name.get(name)
        if target is None:
            unknown.append(name)
        else:
            picked.append(target)
    return picked, unknown


def _split_plugin_arg(values: Iterable[str]) -> tuple[str, ...]:
    names: list[str] = []
    for value in values:
        for part in value.split(','):
            name = part.strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _base_url_arg(value: str) -> str:
    url = value.strip().rstrip('/')
    if not url.startswith(('http://', 'https://')) or '/' in url.split('://', 1)[1]:
        raise argparse.ArgumentTypeError(f'不是可用的壳地址：{value!r}（示例 http://127.0.0.1:18080）')
    return url


def _timeout_arg(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'不是数字：{value!r}') from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(f'必须大于 0：{value!r}')
    return seconds


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(
        description='审计声明了 frontend.chrome 的插件页面骨架（真实壳 + 无头 Chrome）。',
        epilog=(
            '示例：\n'
            '  python tools/plugin_ui_audit.py\n'
            '  python tools/plugin_ui_audit.py --plugin group-mesh --json build/audit.json\n'
            '  python tools/plugin_ui_audit.py --base-url http://127.0.0.1:18080\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--plugin',
        action='append',
        default=[],
        metavar='NAME',
        help='只审计指定插件；可重复，也可用逗号分隔（默认：所有声明 frontend.chrome 的插件）',
    )
    parser.add_argument(
        '--base-url',
        type=_base_url_arg,
        default=None,
        metavar='URL',
        help='复用已经启动的壳（不再自己起服务），例如 http://127.0.0.1:18080',
    )
    parser.add_argument(
        '--json',
        dest='json_path',
        type=Path,
        default=None,
        metavar='PATH',
        help='把结构化结果写到 PATH',
    )
    parser.add_argument(
        '--timeout',
        type=_timeout_arg,
        default=DEFAULT_TIMEOUT,
        metavar='SECONDS',
        help=f'单项等待上限（默认 {DEFAULT_TIMEOUT:g} 秒）',
    )
    namespace = parser.parse_args(argv)
    return Options(
        plugins=_split_plugin_arg(namespace.plugin),
        base_url=namespace.base_url,
        json_path=namespace.json_path,
        timeout=namespace.timeout,
    )


def _display_width(text: str) -> int:
    """按终端显示宽度计列：CJK 全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + ' ' * max(0, width - _display_width(text))


def _one_line(text: str) -> str:
    return ' '.join(text.split())


def format_table(report: AuditReport) -> str:
    header = ('插件', '状态', '检查', '结果', '说明')
    rows: list[tuple[str, ...]] = [header]
    for plugin in report.reports:
        if not plugin.checks:
            rows.append((plugin.name, plugin.status, '-', plugin.status, '没有检查项'))
            continue
        for check in plugin.checks:
            rows.append((plugin.name, plugin.status, check.check, check.status, _one_line(check.detail)))
    widths = [max(_display_width(row[index]) for row in rows) for index in range(len(header))]
    lines = ['  '.join(_pad(cell, widths[index]) for index, cell in enumerate(row)).rstrip() for row in rows]
    return '\n'.join(lines)


def format_summary(report: AuditReport) -> str:
    plugins = report.counts()
    checks = report.check_counts()
    total = sum(checks.values())
    return (
        f'共 {len(report.reports)} 个插件：PASS {plugins[PASS]} / '
        f'FAIL {plugins[FAIL]} / SKIP {plugins[SKIP]}；'
        f'检查项 {total} 项：PASS {checks[PASS]} / FAIL {checks[FAIL]} / SKIP {checks[SKIP]}'
        '（SKIP = 未验证，不算通过）'
    )


# ===== 交互验收的纯逻辑（不碰浏览器，供单测覆盖） =====


def settings_field_id(plugin: str, key: str) -> str:
    """壳设置弹窗里字段输入框的 DOM id（``PluginSettingsModal.fieldId`` 的规则）。"""
    return f'plugin-settings-{plugin}-{key}'


def parse_number(value: object) -> int | float | None:
    """把 DOM 属性字符串收敛成有限数字；``''`` / 非法 / 布尔形状返回 None。"""
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def format_number(value: int | float) -> str:
    """按 DOM ``value`` 的写法格式化数字（12.0 -> '12'，1.6 -> '1.6'）。"""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def alternate_range_value(
    minimum: int | float,
    maximum: int | float,
    current: int | float,
) -> int | float | None:
    """在 [min, max] 里选一个与当前值不同的回滚目标；无法选择时返回 None。"""
    if minimum > maximum:
        return None
    for value in (minimum, maximum):
        if float(value) != float(current):
            return value
    return None


def eq_render_ok(state: dict[str, Any] | None) -> bool:
    """mini EQ 的壳侧契约：``data-status-kind=eq-text`` 且四根柱子都在。"""
    return bool(state) and str(state.get('kind')) == 'eq-text' and int(state.get('bars') or 0) == 4


# ===== 浏览器驱动 =====


class UiAuditError(RuntimeError):
    """环境/参数错误（退出码 2），与插件的 FAIL（退出码 1）区分开。"""


_SHELL_SNAPSHOT_JS = r"""
const name = arguments[0];
const frames = Array.from(document.querySelectorAll('iframe.plugin-iframe'));
const frame = frames.find(f => f.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
const page = container ? container.querySelector('.plugin-page') : null;
const navItems = page ? Array.from(page.querySelectorAll('.plugin-nav-item:not(.plugin-extension-item)')) : [];
const titleEl = page ? page.querySelector('.plugin-title') : null;
const freshnessEl = page ? page.querySelector('.plugin-freshness .obx-fresh') : null;
const rect = container ? container.getBoundingClientRect() : null;
return {
  frameFound: !!frame,
  pageFound: !!page,
  sidebarFound: !!(page && page.querySelector('.plugin-chrome-sidebar')),
  navCount: navItems.length,
  nav: navItems.map(item => ({
    text: item.textContent.trim(),
    active: item.classList.contains('active'),
    viewId: item.getAttribute('data-view-id') || '',
  })),
  activeView: page ? (page.getAttribute('data-active-view') || '') : '',
  title: titleEl ? titleEl.textContent.trim() : '',
  freshness: !!freshnessEl,
  visible: !!(rect && rect.width > 1 && rect.height > 1),
  frameSrc: frame ? (frame.getAttribute('src') || '') : '',
};
"""

_SHELL_HAS_NAV_JS = r"""
return document.querySelectorAll('.nav-item').length > 0;
"""

_CLICK_SHELL_NAV_JS = r"""
const label = arguments[0];
const hit = Array.from(document.querySelectorAll('.nav-item')).find(item => {
  const text = item.querySelector('.text');
  return (text || item).textContent.trim() === label;
});
if (!hit) return false;
hit.click();
return true;
"""

_FIND_FRAME_JS = r"""
const name = arguments[0];
const frames = Array.from(document.querySelectorAll('iframe.plugin-iframe'));
return frames.find(frame => frame.getAttribute('data-plugin-name') === name) || null;
"""

_SHELL_TITLE_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
const titleEl = container ? container.querySelector('.plugin-title') : null;
return titleEl ? titleEl.textContent.trim() : '';
"""

_SHELL_FRESHNESS_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
return !!(container && container.querySelector('.plugin-freshness .obx-fresh'));
"""

_SHELL_ACTIVE_VIEW_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
const page = container ? container.querySelector('.plugin-page') : null;
return page ? (page.getAttribute('data-active-view') || '') : null;
"""

_CLICK_NAV_ITEM_JS = r"""
const name = arguments[0];
const index = arguments[1];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
if (!container) return false;
const items = Array.from(container.querySelectorAll('.plugin-nav-item:not(.plugin-extension-item)'));
if (!items[index]) return false;
items[index].click();
return true;
"""

_IFRAME_DOCUMENT_JS = r"""
return document.readyState === 'complete' && !!document.body;
"""

# ---- 交互验收：设置弹窗 / 搜索 / mini EQ ----

_SETTINGS_TRIGGER_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
if (!container) return false;
const actions = container.querySelector('.plugin-actions:not(.plugin-actions-measure)');
if (!actions) return false;
const direct = Array.from(actions.querySelectorAll('button'))
  .find(button => (button.textContent || '').trim() === '设置');
if (direct) { direct.click(); return true; }
const more = actions.querySelector('.plugin-more-btn');
if (!more) return false;
more.click();
const item = Array.from(document.querySelectorAll('.plugin-more-item'))
  .find(button => (button.textContent || '').trim() === '设置');
if (!item) return false;
item.click();
return true;
"""

_SETTINGS_FIELD_JS = r"""
const field = document.getElementById(arguments[0]);
if (!field) return null;
return {
  value: String(field.value),
  min: field.getAttribute('min'),
  max: field.getAttribute('max'),
};
"""

_SETTINGS_FIELD_SET_JS = r"""
const field = document.getElementById(arguments[0]);
if (!field) return false;
field.value = String(arguments[1]);
field.dispatchEvent(new Event('input', { bubbles: true }));
return true;
"""

_SETTINGS_SAVE_JS = r"""
const button = document.querySelector('.settings-modal-foot .btn-primary');
if (!button || button.disabled) return false;
button.click();
return true;
"""

_SETTINGS_CLOSE_JS = r"""
const button = document.querySelector('.settings-modal-foot .btn:not(.btn-primary)')
  || document.querySelector('.settings-modal-close');
if (!button) return false;
button.click();
return true;
"""

_SETTINGS_OPEN_JS = "return !!document.querySelector('.settings-modal');"

_SEARCH_INPUT_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
const page = container ? container.querySelector('.plugin-page') : null;
const input = page ? page.querySelector('.plugin-search input') : null;
return input ? String(input.value) : null;
"""

_SEARCH_TYPE_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
const page = container ? container.querySelector('.plugin-page') : null;
const input = page ? page.querySelector('.plugin-search input') : null;
if (!input) return false;
input.focus();
input.value = String(arguments[1]);
input.dispatchEvent(new Event('input', { bubbles: true }));
return true;
"""

_QUERY_SPY_INSTALL_JS = r"""
window.__auditQueries = [];
if (window.__auditQuerySpy) window.removeEventListener('message', window.__auditQuerySpy);
window.__auditQuerySpy = (event) => {
  const data = event.data;
  if (data && data.type === 'omnibox:query') {
    window.__auditQueries.push(String(data.query == null ? '' : data.query));
  }
};
window.addEventListener('message', window.__auditQuerySpy);
return true;
"""

_QUERY_SPY_READ_JS = r"""
return Array.isArray(window.__auditQueries) ? window.__auditQueries.slice() : null;
"""

_EQ_STATUS_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
const page = container ? container.querySelector('.plugin-page') : null;
const status = page ? page.querySelector('.plugin-status') : null;
if (!status) return null;
return {
  kind: status.getAttribute('data-status-kind') || '',
  bars: status.querySelectorAll('.plugin-eq i').length,
};
"""

_EQ_INJECT_JS = r"""
window.parent.postMessage({
  type: 'omnibox:chrome',
  status: { text: String(arguments[0]), state: String(arguments[1]) },
}, window.location.origin);
return true;
"""

_IFRAME_CONTENT_JS = r"""
const banned = ['.view-sub-sidebar', '.view-toolbar', '.sub-sidebar-brand', '.obx-nav-item'];
return {
  ready: document.readyState,
  href: location.href,
  found: banned.filter(selector => !!document.querySelector(selector)),
};
"""

_SPLITTER_RECT_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
if (!container) return null;
const handle = Array.from(container.querySelectorAll('[data-obx-splitter-handle]')).find(candidate =>
  candidate.previousElementSibling && candidate.previousElementSibling.classList.contains('plugin-chrome-sidebar'));
if (!handle) return null;
const rect = handle.getBoundingClientRect();
return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
"""

_SPLITTER_HOVER_JS = r"""
const name = arguments[0];
const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))
  .find(item => item.getAttribute('data-plugin-name') === name) || null;
const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;
if (!container) return null;
const handle = Array.from(container.querySelectorAll('[data-obx-splitter-handle]')).find(candidate =>
  candidate.previousElementSibling && candidate.previousElementSibling.classList.contains('plugin-chrome-sidebar'));
if (!handle) return null;
const style = getComputedStyle(handle, '::before');
const probe = document.createElement('div');
probe.style.cssText =
  'position:absolute;left:-10000px;top:0;width:1px;height:1px;background:var(--accent)';
document.body.appendChild(probe);
const accent = getComputedStyle(probe).backgroundColor;
probe.remove();
return {
  hovered: handle.matches(':hover'),
  width: style.width,
  background: style.backgroundColor,
  accent: accent,
};
"""


def _wait_until(predicate: Callable[[], bool], timeout: float, interval: float = POLL_INTERVAL) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if predicate():
                return True
        except Exception:
            # 页面/iframe 切换瞬间元素可能失效；这类异常在下一轮重试即可。
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _probe_health(base_url: str, timeout: float = 0.5) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url.rstrip('/') + '/health', timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def _spawn_group_kwargs() -> dict[str, Any]:
    """让壳进程自成进程组：POSIX 的 ``killpg`` 与 Windows 的 ``taskkill /T`` 才覆盖子进程。"""
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def _terminate_tree(process: subprocess.Popen[Any]) -> None:
    """杀掉壳服务**及其子进程**。

    不能只 ``terminate()``：Windows 上 ``venv\\Scripts\\python.exe`` 会再起一个真正
    的解释器跑 ``main.py``，杀掉启动器会留下孤儿服务继续占端口与日志文件（实测：
    临时用户数据根删不掉、端口 4028 一直被占用）。``taskkill /T`` / ``killpg``
    连子进程一起停，交互验收的临时目录才能真正清理。
    """
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


class ShellServer:
    """临时壳服务：``main.py --web-only --port <空闲端口>``，退出时 kill。

    ``home`` 非空时通过 ``OMNIBOX_HOME`` 把整个用户数据根（配置、插件设置、data）
    指到该目录，供"会写设置"的交互验收使用；不传则保持与人工运行时一致的数据根。
    """

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        start_timeout: float | None = None,
        home: Path | None = None,
    ) -> None:
        self._root = root
        self._start_timeout = start_timeout if start_timeout is not None else DEFAULT_TIMEOUT
        self._home = home
        self._process: subprocess.Popen[Any] | None = None
        self._stack: contextlib.ExitStack | None = None
        self._log: Any = None
        self.base_url = ''

    def start(self) -> str:
        entry = self._root / 'main.py'
        if not entry.is_file():
            raise UiAuditError(f'找不到壳入口：{entry}')
        port = _free_port()
        self.base_url = f'http://127.0.0.1:{port}'
        self._stack = contextlib.ExitStack()
        self._log = self._stack.enter_context(tempfile.TemporaryFile())  # noqa: SIM115 —— 生命周期由 ExitStack 管，stop() 关闭
        env = None
        if self._home is not None:
            env = dict(os.environ)
            env[HOME_ENV] = str(self._home)
        try:
            process = subprocess.Popen(
                [sys.executable, str(entry), '--web-only', '--port', str(port)],
                cwd=str(self._root),
                env=env,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                **_spawn_group_kwargs(),
            )
            self._process = process
        except OSError as exc:
            self.stop()
            raise UiAuditError(f'无法启动壳服务：{exc}') from exc
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                tail = self._log_tail()
                self.stop()
                raise UiAuditError(f'壳服务启动失败（退出码 {return_code}）：{tail}')
            if _probe_health(self.base_url):
                return self.base_url
            time.sleep(POLL_INTERVAL)
        tail = self._log_tail()
        self.stop()
        raise UiAuditError(f'壳服务在 {self._start_timeout:g} 秒内没有通过 /health 探活：{tail}')

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            _terminate_tree(process)
        self._log = None
        stack = self._stack
        self._stack = None
        if stack is not None:
            stack.close()

    def _log_tail(self, limit: int = 2000) -> str:
        log = self._log
        if log is None:
            return ''
        try:
            log.seek(0)
            text = log.read().decode('utf-8', errors='replace').strip()
        except (OSError, ValueError):
            return ''
        return text[-limit:] or '（服务没有输出）'


def _resolve_browser_binary() -> str | None:
    """优先复用 tests/harness/browser_binary.py 的解析（含 .build/cft）。"""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from tests.harness.browser_binary import browser_binary
    except ImportError:
        return None
    return browser_binary()


def _create_driver() -> Any:
    try:
        # selenium 只在 requirements-e2e.txt 里；CI 的 typecheck 不装它，运行时缺包由下面的
        # except 转成环境错误提示。这里按行忽略缺失导入，不用文件级关闭掩盖其他漏导入。
        from selenium import webdriver  # pyright: ignore[reportMissingImports]
        from selenium.webdriver.chrome.options import Options as ChromeOptions  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise UiAuditError('未安装 selenium；先执行 pip install -r requirements-e2e.txt') from exc

    binary = _resolve_browser_binary()
    if not binary:
        raise UiAuditError('找不到 Chrome 可执行文件（.build/cft 或已安装的 Chrome）')

    options = ChromeOptions()
    options.binary_location = binary
    for argument in (
        '--headless=new',
        '--disable-gpu',
        '--hide-scrollbars',
        f'--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}',
        '--no-first-run',
        '--no-default-browser-check',
    ):
        options.add_argument(argument)
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
    try:
        return webdriver.Chrome(options=options)
    except Exception as exc:
        raise UiAuditError(f'Chrome 启动失败：{exc}') from exc


def _shell_snapshot(driver: Any, plugin_name: str) -> dict[str, Any]:
    data = driver.execute_script(_SHELL_SNAPSHOT_JS, plugin_name)
    if isinstance(data, dict):
        return data
    return {
        'frameFound': False,
        'pageFound': False,
        'sidebarFound': False,
        'navCount': 0,
        'nav': [],
        'activeView': '',
        'title': '',
        'freshness': False,
        'visible': False,
        'frameSrc': '',
    }


def _shell_title(driver: Any, plugin_name: str) -> str:
    return str(driver.execute_script(_SHELL_TITLE_JS, plugin_name) or '').strip()


def _shell_active_view(driver: Any, plugin_name: str) -> str | None:
    """壳侧当前视图 id（`.plugin-page[data-active-view]`）；页面不存在时返回 None。"""
    value = driver.execute_script(_SHELL_ACTIVE_VIEW_JS, plugin_name)
    return None if value is None else str(value)


def _has_freshness(driver: Any, plugin_name: str) -> bool:
    return driver.execute_script(_SHELL_FRESHNESS_JS, plugin_name) is True


def _switch_to_frame(driver: Any, plugin_name: str) -> bool:
    frame = driver.execute_script(_FIND_FRAME_JS, plugin_name)
    if frame is None:
        return False
    driver.switch_to.frame(frame)
    return True


def _frame_ready(driver: Any, timeout: float) -> bool:
    return _wait_until(lambda: driver.execute_script(_IFRAME_DOCUMENT_JS) is True, min(max(timeout, 1.0), 10.0))


def _console_entries(driver: Any) -> list[dict[str, Any]] | None:
    try:
        entries = driver.get_log('browser')
    except Exception:
        return None
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict)]


def _open_plugin(driver: Any, target: PluginTarget, timeout: float) -> bool:
    if not _wait_until(lambda: driver.execute_script(_SHELL_HAS_NAV_JS) is True, timeout):
        raise UiAuditError(
            '壳页面没有就绪：确认 shell/frontend/dist 已构建'
            '（npm --prefix shell/frontend run build），并检查壳服务日志'
        )
    snapshot = _shell_snapshot(driver, target.name)
    if snapshot['frameFound'] and snapshot['pageFound'] and snapshot['visible']:
        return True
    if driver.execute_script(_CLICK_SHELL_NAV_JS, target.display_name) is not True:
        return False

    def ready() -> bool:
        current = _shell_snapshot(driver, target.name)
        return bool(current['frameFound'] and current['pageFound'] and current['visible'])

    _wait_until(ready, timeout)
    return True


def _check_shell_chrome(snapshot: dict[str, Any]) -> tuple[str, str]:
    problems: list[str] = []
    if not snapshot['frameFound']:
        problems.append('找不到 iframe.plugin-iframe')
    elif not snapshot['visible']:
        problems.append('插件容器不可见（路由未激活）')
    if not snapshot['pageFound']:
        problems.append('壳未渲染 .plugin-page')
    elif not snapshot['sidebarFound']:
        problems.append('.plugin-chrome-sidebar 缺失')
    if int(snapshot['navCount']) <= 0:
        problems.append('.plugin-nav-item 数量为 0')
    title = str(snapshot['title']).strip()
    if not title:
        problems.append('.plugin-title 为空')
    if problems:
        return FAIL, '；'.join(problems)
    return PASS, (
        f'.plugin-page / .plugin-chrome-sidebar / .plugin-nav-item×{snapshot["navCount"]} / '
        f'.plugin-title「{title}」'
    )


def _read_iframe_content(driver: Any, target: PluginTarget, timeout: float) -> dict[str, Any] | None:
    """等 iframe 真正导航到插件页（而不是 about:blank）后读它的 DOM。"""
    data: dict[str, Any] | None = None

    def ready() -> bool:
        nonlocal data
        if not _switch_to_frame(driver, target.name):
            return False
        try:
            if not _frame_ready(driver, 1.0):
                return False
            current = driver.execute_script(_IFRAME_CONTENT_JS)
        finally:
            driver.switch_to.default_content()
        if isinstance(current, dict):
            data = current
        if not data:
            return False
        href = str(data.get('href') or '')
        return data.get('ready') == 'complete' and href.startswith(('http://', 'https://'))

    ok = _wait_until(ready, timeout)
    # 只有"等到文档真正 complete"才算读到内容：超时返回最后一份 interactive 快照
    # 会让 _check_iframe_content 在没有完整 DOM 时也能 PASS。
    return data if ok else None


def _check_iframe_content(driver: Any, target: PluginTarget, timeout: float) -> tuple[str, str]:
    snapshot = _shell_snapshot(driver, target.name)
    source = str(snapshot.get('frameSrc') or '')
    data = _read_iframe_content(driver, target, timeout)
    if data is None:
        return FAIL, '找不到插件 iframe 或 iframe 在超时内没有加载完成'
    ready_state = str(data.get('ready') or '')
    if ready_state != 'complete':
        return FAIL, f'iframe 文档未加载完成（readyState={ready_state or "未知"}），拒绝按内容页判定'
    href = str(data.get('href') or '')
    if not href.startswith(('http://', 'https://')):
        return FAIL, f'iframe 没有导航到插件页面（{href or "href 为空"}）'
    problems: list[str] = []
    found = [str(selector) for selector in (data.get('found') or [])]
    if found:
        problems.append('内容页仍含壳骨架类名：' + '、'.join(found))
    if 'content=1' not in source:
        problems.append(f'iframe src 未带 content=1（{source or "src 为空"}）')
    if problems:
        return FAIL, '；'.join(problems)
    return PASS, 'iframe 内无 .view-sub-sidebar/.view-toolbar/.sub-sidebar-brand/.obx-nav-item，src 带 content=1'


def _check_nav_switch(
    driver: Any,
    target: PluginTarget,
    snapshot: dict[str, Any],
    timeout: float,
) -> dict[str, tuple[str, str]]:
    """点一个壳侧导航项，用壳自己的活动标记判断视图是否切换。

    判定不读内容 iframe 的 ``[data-panel]`` / ``[data-view]``：image-viewer /
    manga-library / media-player 的内容页没有这套标记，旧实现只能恒 SKIP。
    壳的 ``.plugin-page[data-active-view]`` 与导航项 ``data-view-id`` 是页面骨架
    的单一事实源；manifest 里声明 ``visible: false`` 的项（含整组）不参与计数。
    """
    nav = [item for item in (snapshot.get('nav') or []) if isinstance(item, dict)]
    expected = target.visible_nav_items
    if len(nav) < 2:
        detail = '插件只有一个导航项，没有可切换的目标'
        return {'panel': (SKIP, detail), 'title': (SKIP, detail)}
    if len(expected) != len(nav):
        detail = (
            f'壳侧导航项 {len(nav)} 个与 manifest 可见项 {len(expected)} 个不一致，'
            '无法把点击映射回视图 id（动态导航项可能多于声明）'
        )
        return {'panel': (SKIP, detail), 'title': (SKIP, detail)}
    if not all(str(item.get('viewId') or '') for item in nav):
        detail = '壳侧导航项没有 data-view-id（旧壳或壳未重建），无法判断视图切换'
        return {'panel': (SKIP, detail), 'title': (SKIP, detail)}

    active_view = str(snapshot.get('activeView') or '')
    current_index = next((index for index, item in enumerate(nav) if item.get('active')), None)
    if current_index is None:
        current_index = next(
            (index for index, item in enumerate(nav) if item.get('viewId') == active_view),
            None,
        )
    target_index = next((index for index in range(len(nav)) if index != current_index), None)
    if target_index is None:
        detail = '找不到可点击的非当前导航项'
        return {'panel': (SKIP, detail), 'title': (SKIP, detail)}

    target_id = str(nav[target_index].get('viewId') or '')
    target_label = str(nav[target_index].get('text') or target_id)
    title_before = str(snapshot.get('title') or '').strip()
    clicked = driver.execute_script(_CLICK_NAV_ITEM_JS, target.name, target_index)
    if clicked is not True:
        detail = f'点击第 {target_index + 1} 个导航项「{target_label}」失败'
        return {'panel': (FAIL, detail), 'title': (FAIL, detail)}

    actual = active_view
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _shell_active_view(driver, target.name)
        if current is not None:
            actual = current
        if actual == target_id:
            break
        time.sleep(POLL_INTERVAL)
    # 壳侧标记在点击当下就更新；标题来自内容页随后的一次 chrome 上报（要过一圈
    # postMessage），所以面板确认后再给一小段等待，别把"还没上报"误判成"按设计不变"。
    title_after = title_before
    if actual == target_id:
        title_deadline = time.monotonic() + min(timeout, 3.0)
        while time.monotonic() < title_deadline:
            title_after = _shell_title(driver, target.name)
            if title_after and title_after != title_before:
                break
            time.sleep(POLL_INTERVAL)
        else:
            title_after = _shell_title(driver, target.name)
    panel = _nav_active_result(actual, target_id, target_label)
    return {
        'panel': panel,
        'title': _nav_title_result(title_before, title_after, view_switched=panel[0] == PASS),
    }


def _nav_active_result(active_view: str, target_id: str, target_label: str) -> tuple[str, str]:
    """壳侧活动视图与期望 id 的比较（纯逻辑，便于单测）。"""
    if active_view == target_id:
        return PASS, f'点击「{target_label}」后壳侧 data-active-view={target_id}'
    return (
        FAIL,
        f'点击「{target_label}」后期望壳侧 data-active-view={target_id}，'
        f'实际 {active_view or "空"}',
    )


def _nav_title_result(
    title_before: str,
    title_after: str,
    *,
    view_switched: bool,
) -> tuple[str, str]:
    if not title_before:
        return SKIP, '点击前 .plugin-title 为空，无法比较变化'
    if title_after and title_after != title_before:
        return PASS, f'壳标题「{title_before}」→「{title_after}」'
    if view_switched:
        # 视图确实换了、标题按设计保持常量（document-reader 的书架分类就是如此）：
        # 没有可验证的标题变化，记 SKIP 而不是伪造一个 FAIL。
        return SKIP, f'壳侧视图已切换，但标题保持「{title_before}」不变（该插件按设计不随视图改标题）'
    return FAIL, f'点击后壳视图未切换，壳标题也未变化（仍为「{title_before}」）'


def _splitter_rect(driver: Any, plugin_name: str) -> dict[str, Any] | None:
    data = driver.execute_script(_SPLITTER_RECT_JS, plugin_name)
    return data if isinstance(data, dict) else None


def _splitter_hover_state(driver: Any, plugin_name: str) -> dict[str, Any] | None:
    data = driver.execute_script(_SPLITTER_HOVER_JS, plugin_name)
    return data if isinstance(data, dict) else None


def _pixel_width(value: Any) -> float | None:
    try:
        return float(str(value).removesuffix('px'))
    except ValueError:
        return None


def _is_visible_color(value: Any) -> bool:
    text = str(value or '').strip()
    return bool(text) and text not in ('transparent', 'rgba(0, 0, 0, 0)')


def _splitter_hover_ok(state: dict[str, Any]) -> bool:
    return (
        bool(state.get('hovered'))
        and _pixel_width(state.get('width')) == 2.0
        and _is_visible_color(state.get('background'))
        and state.get('background') == state.get('accent')
    )


def _check_splitter_hover(driver: Any, plugin_name: str, timeout: float) -> tuple[str, str]:
    if not _wait_until(lambda: _splitter_rect(driver, plugin_name) is not None, min(timeout, 10.0)):
        return FAIL, '.plugin-chrome-sidebar 之后没有 [data-obx-splitter-handle]（splitter.js 未注册）'
    rect = _splitter_rect(driver, plugin_name)
    if rect is None:
        return FAIL, '分隔条把手在读取 rect 时消失'
    if float(rect.get('width') or 0) <= 0 or float(rect.get('height') or 0) <= 0:
        return FAIL, f'分隔条把手没有尺寸（{rect.get("width")}×{rect.get("height")}）'
    x = int(float(rect['x']) + max(1.0, float(rect['width']) / 2))
    y = int(float(rect['y']) + min(200.0, max(1.0, float(rect['height']) / 2)))
    state: dict[str, Any] = {}
    ok = False
    try:
        driver.execute_cdp_cmd('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y, 'buttons': 0})

        def hovered() -> bool:
            current = _splitter_hover_state(driver, plugin_name)
            state.clear()
            if current:
                state.update(current)
            return _splitter_hover_ok(state)

        ok = _wait_until(hovered, min(timeout, 5.0))
    finally:
        # 把指针挪开，避免 hover 状态影响后续检查（例如设置弹窗的点击位置）。
        driver.execute_cdp_cmd('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 2, 'y': 2, 'buttons': 0})
    if ok:
        return PASS, (
            f'hover 后 ::before 宽 {state.get("width")}、背景 {state.get("background")} '
            '等于 --accent'
        )
    return FAIL, (
        f'hover 后 ::before 宽 {state.get("width") or "未读到"}、背景 '
        f'{state.get("background") or "未读到"}、--accent {state.get("accent") or "未读到"}、'
        f'hovered={state.get("hovered")}'
    )


def _check_freshness(driver: Any, target: PluginTarget, timeout: float) -> tuple[str, str]:
    if not target.has_freshness:
        return SKIP, 'manifest 未声明 frontend.chrome.freshness'
    if _wait_until(lambda: _has_freshness(driver, target.name), timeout):
        return PASS, '壳侧 .plugin-freshness 内已挂载 .obx-fresh'
    return FAIL, f'{timeout:g} 秒内 .plugin-freshness 内没有 .obx-fresh'


def _fresh_console_entries(
    before: list[dict[str, Any]] | None,
    after: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """从前后两次读取里挑出本轮的新 console 记录。

    ChromeDriver 的 ``get_log('browser')`` 每次读取都会清空缓冲：实测先读出 1 条，
    再产生 1 条后第二次读取只返回新的 1 条，第三次读是 0 条。因此不能拿"上次读到的
    条数"当切片起点 —— 新记录数一旦少于旧条数，``entries[offset:]`` 就是空列表，
    控制台错误会被静默吞掉、还报 PASS。这里兼容两种实现：若读回的列表以上次结果
    开头（累积型）就切掉前缀，否则整份都是新的。
    """
    if after is None:
        return None
    if before:
        prefix = len(before)
        if after[:prefix] == before:
            return after[prefix:]
    return list(after)


def _check_console(
    driver: Any,
    before: list[dict[str, Any]] | None,
) -> tuple[str, str]:
    if before is None:
        return SKIP, '浏览器不支持 console 日志采集'
    fresh = _fresh_console_entries(before, _console_entries(driver))
    if fresh is None:
        return SKIP, '读取 console 日志失败'
    severe = [entry for entry in fresh if str(entry.get('level')).upper() == 'SEVERE']
    if not severe:
        return PASS, f'新增 {len(fresh)} 条 console 记录，无 SEVERE'
    messages = []
    for entry in severe[:3]:
        message = ' '.join(str(entry.get('message') or '').split())
        messages.append(message[:160])
    more = f'（共 {len(severe)} 条）' if len(severe) > 3 else ''
    return FAIL, f'新增 SEVERE {len(severe)} 条{more}：' + ' | '.join(messages)


# ===== 交互验收：设置弹窗保存往返 / 搜索回填与清空 / mini EQ =====
# 三项都在隔离实例（临时 OMNIBOX_HOME）里跑：设置往返会写插件设置，不能碰用户配置。

def _settings_modal_open(driver: Any) -> bool:
    return driver.execute_script(_SETTINGS_OPEN_JS) is True


def _settings_field_state(driver: Any, field_id: str) -> dict[str, Any] | None:
    data = driver.execute_script(_SETTINGS_FIELD_JS, field_id)
    return data if isinstance(data, dict) else None


def _settings_field_value(driver: Any, field_id: str) -> str | None:
    state = _settings_field_state(driver, field_id)
    return None if state is None else str(state.get('value'))


def _open_settings_modal(
    driver: Any,
    plugin_name: str,
    field_id: str,
    timeout: float,
) -> bool:
    if not _settings_modal_open(driver) and driver.execute_script(_SETTINGS_TRIGGER_JS, plugin_name) is not True:
        return False
    return _wait_until(lambda: _settings_field_state(driver, field_id) is not None, timeout)


def _set_settings_field(driver: Any, field_id: str, value: str) -> bool:
    return driver.execute_script(_SETTINGS_FIELD_SET_JS, field_id, value) is True


def _save_settings_modal(driver: Any, timeout: float) -> bool:
    if driver.execute_script(_SETTINGS_SAVE_JS) is not True:
        return False
    return _wait_until(lambda: not _settings_modal_open(driver), timeout)


def _close_settings_modal(driver: Any) -> None:
    driver.execute_script(_SETTINGS_CLOSE_JS)


def _restore_settings_field(
    driver: Any,
    plugin_name: str,
    field_id: str,
    original: str | None,
    timeout: float,
) -> None:
    """把设置弹窗恢复为原值并关闭；隔离实例里也做，避免中途失败留下改动的草稿。"""
    if not original:
        return
    try:
        if not _settings_modal_open(driver) and not _open_settings_modal(
            driver, plugin_name, field_id, timeout
        ):
            return
        if _settings_field_value(driver, field_id) == original:
            _close_settings_modal(driver)
            return
        _set_settings_field(driver, field_id, original)
        if not _save_settings_modal(driver, timeout):
            _close_settings_modal(driver)
    except Exception:
        # 收尾路径：恢复失败不改变已经得出的检查结论，隔离实例销毁后也不会污染配置。
        pass


def _check_settings_roundtrip(driver: Any, timeout: float) -> tuple[str, str]:
    """壳设置弹窗保存往返：改一个 range → 保存 → 重开断言回填 → 还原。

    选 media-player 的 ``lyrics_font_size``（12–40 的纯展示值，保存只影响歌词字号），
    用隔离实例跑；结束时无论成败都还原原值。
    """
    field_id = settings_field_id(INTERACTIVE_PLUGIN, 'lyrics_font_size')
    original: str | None = None
    try:
        if not _open_settings_modal(driver, INTERACTIVE_PLUGIN, field_id, timeout):
            return FAIL, '打不开壳设置弹窗（顶栏「设置」不可点击或字段未渲染）'
        state = _settings_field_state(driver, field_id)
        if state is None:
            return FAIL, f'设置弹窗里没有字段 lyrics_font_size（{field_id}）'
        original = str(state.get('value') or '')
        current = parse_number(original)
        candidate = alternate_range_value(
            parse_number(state.get('min')) or 0,
            parse_number(state.get('max')) or 0,
            current if current is not None else 0,
        )
        if current is None or candidate is None:
            return FAIL, f'无法从 min/max/当前值选出回滚值：{state}'
        expected = format_number(candidate)
        if not _set_settings_field(driver, field_id, expected):
            return FAIL, '写不进设置字段 lyrics_font_size'
        if not _save_settings_modal(driver, timeout):
            return FAIL, '保存设置失败（弹窗没有在超时内关闭）'
        if not _open_settings_modal(driver, INTERACTIVE_PLUGIN, field_id, timeout):
            return FAIL, '保存后无法重新打开设置弹窗'
        reopened = _settings_field_value(driver, field_id)
        if reopened is None:
            return FAIL, '重开设置弹窗后找不到字段 lyrics_font_size'
        if reopened != expected:
            return FAIL, f'歌词字号保存后回填不一致：期望 {expected!r}，实际 {reopened!r}'
        return PASS, f'歌词字号 {original} → {expected}，重开后回填一致（随后还原）'
    finally:
        _restore_settings_field(driver, INTERACTIVE_PLUGIN, field_id, original, timeout)


def _in_frame(driver: Any, plugin_name: str, script: str, *args: object) -> Any:
    """在插件内容 iframe 里执行脚本后切回默认文档。"""
    if not _switch_to_frame(driver, plugin_name):
        return None
    try:
        return driver.execute_script(script, *args)
    finally:
        driver.switch_to.default_content()


def _shell_search_value(driver: Any, plugin_name: str) -> str | None:
    value = driver.execute_script(_SEARCH_INPUT_JS, plugin_name)
    return None if value is None else str(value)


def _set_shell_search(driver: Any, plugin_name: str, value: str) -> bool:
    return driver.execute_script(_SEARCH_TYPE_JS, plugin_name, value) is True


def _query_spy(driver: Any, plugin_name: str) -> list[str] | None:
    data = _in_frame(driver, plugin_name, _QUERY_SPY_READ_JS)
    if not isinstance(data, list):
        return None
    return [str(item) for item in data]


def _check_search_roundtrip(driver: Any, plugin_name: str, timeout: float) -> tuple[str, str]:
    """搜索回填与清空：内容页收到 ``omnibox:query``，切视图后壳输入框被清空。

    内容页是否"收到"用注入的消息观察器判断（iframe 里追加一个 ``message`` 监听），
    不依赖某条业务列表的渲染结果；清空走 ``search.clear``：内容页切视图时清空，
    壳的输入框必须跟着回空。
    """
    if _in_frame(driver, plugin_name, _QUERY_SPY_INSTALL_JS) is not True:
        return FAIL, '无法在内容页安装消息观察器（iframe 不可达）'
    if _shell_search_value(driver, plugin_name) is None:
        return FAIL, '壳没有渲染搜索框（插件未声明 search 或输入框缺失）'
    if not _set_shell_search(driver, plugin_name, 'audit-probe'):
        return FAIL, '写不进壳搜索框'
    received = _wait_until(
        lambda: 'audit-probe' in (_query_spy(driver, plugin_name) or []),
        timeout,
    )
    if not received:
        return FAIL, '内容页没有收到 omnibox:query（audit-probe）'
    snapshot = _shell_snapshot(driver, plugin_name)
    nav = [item for item in (snapshot.get('nav') or []) if isinstance(item, dict)]
    active = next((index for index, item in enumerate(nav) if item.get('active')), None)
    target_index = next((index for index in range(len(nav)) if index != active), None)
    if target_index is None:
        return FAIL, '没有可切换的导航项，无法验证 search.clear'
    if driver.execute_script(_CLICK_NAV_ITEM_JS, plugin_name, target_index) is not True:
        return FAIL, '点击导航项失败，无法验证 search.clear'
    cleared = _wait_until(lambda: _shell_search_value(driver, plugin_name) == '', timeout)
    if not cleared:
        current = _shell_search_value(driver, plugin_name)
        return FAIL, f'切换视图后壳搜索框没有清空（仍为 {current!r}）'
    return PASS, '内容页收到 omnibox:query=audit-probe；切换视图后壳输入框被 search.clear 清空'


def _status_eq_state(driver: Any, plugin_name: str) -> dict[str, Any] | None:
    data = driver.execute_script(_EQ_STATUS_JS, plugin_name)
    return data if isinstance(data, dict) else None


def _inject_status(driver: Any, plugin_name: str, text: str, state: str) -> bool:
    """从内容 iframe 发一条 ``omnibox:chrome`` 状态补丁（合成播放/暂停态）。"""
    return _in_frame(driver, plugin_name, _EQ_INJECT_JS, text, state) is True


def _check_mini_eq(driver: Any, plugin_name: str, timeout: float) -> tuple[str, str]:
    """mini EQ 的壳侧渲染契约：``kind=eq-text`` + ``state=ok`` 出四柱，``idle`` 收起。

    播放态由内容 iframe 发一条状态补丁合成（不依赖本机有没有可播放媒体）；这验证的是
    ``PluginChrome`` 的渲染契约，插件自身何时上报 ``state=ok`` 仍由播放链路保证。
    """
    state: dict[str, Any] | None = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _inject_status(driver, plugin_name, '播放中', 'ok')
        state = _status_eq_state(driver, plugin_name)
        if eq_render_ok(state):
            break
        time.sleep(POLL_INTERVAL)
    if not eq_render_ok(state):
        return FAIL, f'注入 status.state=ok 后没有渲染迷你 EQ（最后状态 {state!r}）'
    cleared = False
    for _ in range(max(1, int(min(timeout, 5.0) / POLL_INTERVAL))):
        _inject_status(driver, plugin_name, '已暂停', 'idle')
        paused = _status_eq_state(driver, plugin_name)
        if isinstance(paused, dict) and int(paused.get('bars') or 0) == 0:
            cleared = True
            break
        time.sleep(POLL_INTERVAL)
    if not cleared:
        return FAIL, '注入 status.state=idle 后迷你 EQ 没有消失'
    return PASS, 'data-status-kind=eq-text + state=ok 渲染四柱 EQ，state=idle 后消失（壳侧契约）'


class InteractiveChecks:
    """交互验收用的隔离壳：临时 OMNIBOX_HOME，不读写用户的插件设置。"""

    def __init__(self) -> None:
        self._home = tempfile.TemporaryDirectory(prefix='omnibox-ui-audit-', ignore_cleanup_errors=True)
        self._server = ShellServer(home=Path(self._home.name))
        self.driver: Any = None

    def start(self) -> None:
        base_url = self._server.start()
        self.driver = _create_driver()
        self.driver.set_window_size(*WINDOW_SIZE)
        try:
            self.driver.get(base_url)
        except Exception as exc:
            self.close()
            raise UiAuditError(f'隔离实例浏览器启动失败：{exc}') from exc

    def close(self) -> None:
        driver = self.driver
        self.driver = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        self._server.stop()
        # Windows 上刚结束的壳进程会短暂占用日志句柄，rmtree 第一次可能失败；
        # 重试几次再把目录留在系统临时目录里（不写回仓库，也不影响审计结论）。
        for _ in range(5):
            try:
                self._home.cleanup()
                return
            except Exception:
                time.sleep(0.2)


def audit_interactive(driver: Any, timeout: float) -> list[tuple[str, str, str]]:
    """隔离实例上的三项交互检查；返回 (检查名, 状态, 说明)。"""
    results: list[tuple[str, str, str]] = []
    try:
        results.append(('settings.roundtrip', *_check_settings_roundtrip(driver, timeout)))
    except Exception as exc:
        results.append(('settings.roundtrip', FAIL, f'交互检查异常：{exc}'))
    try:
        results.append(('search.query-clear', *_check_search_roundtrip(driver, INTERACTIVE_PLUGIN, timeout)))
    except Exception as exc:
        results.append(('search.query-clear', FAIL, f'交互检查异常：{exc}'))
    try:
        results.append(('eq.mini', *_check_mini_eq(driver, INTERACTIVE_PLUGIN, timeout)))
    except Exception as exc:
        results.append(('eq.mini', FAIL, f'交互检查异常：{exc}'))
    return results


def audit_plugin(driver: Any, target: PluginTarget, timeout: float) -> PluginReport:
    """审计单个插件；返回逐项 PASS/FAIL/SKIP，不抛插件级异常。"""
    report = PluginReport(target.name, target.display_name)
    # 先读一次把既有记录取走（ChromeDriver 读取即清空）；这之后产生的才属于本插件。
    console_before = _console_entries(driver)

    if not _open_plugin(driver, target, timeout):
        report.add('plugin.open', SKIP, f'壳导航中没有「{target.display_name}」：插件未加载或被本地可见性配置隐藏')
        report.add('console', SKIP, '插件未打开，无法归属 console 记录')
        return report

    snapshot = _shell_snapshot(driver, target.name)
    report.add('shell.chrome', *_check_shell_chrome(snapshot))
    report.add('iframe.content-only', *_check_iframe_content(driver, target, timeout))
    nav_results = _check_nav_switch(driver, target, snapshot, timeout)
    report.add('nav.panel', *nav_results['panel'])
    report.add('nav.title', *nav_results['title'])
    report.add('splitter.hover', *_check_splitter_hover(driver, target.name, timeout))
    report.add('freshness.mount', *_check_freshness(driver, target, timeout))
    report.add('console', *_check_console(driver, console_before))
    return report


def run(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    discovery = discover_chrome_plugins()
    for problem in discovery.problems:
        print(f'[警告] {problem}', file=sys.stderr)
    targets, unknown = select_targets(discovery.targets, options.plugins)
    if unknown:
        available = '、'.join(target.name for target in discovery.targets) or '（无）'
        print(f'未知插件：{"、".join(unknown)}\n可用插件：{available}', file=sys.stderr)
        return 2
    if not targets:
        print('没有可审计的插件：plugins/*/manifest.json 里没有声明 frontend.chrome 的项。', file=sys.stderr)
        return 2

    report = AuditReport()
    server: ShellServer | None = None
    driver: Any = None
    interactive: InteractiveChecks | None = None
    try:
        if options.base_url:
            base_url = options.base_url
            if not _wait_until(lambda: _probe_health(base_url), min(options.timeout, 10.0)):
                print(f'--base-url 指向的壳不可用：{base_url}/health', file=sys.stderr)
                return 2
            # 复用别人的实例：设置往返会写插件设置，不能对着外部实例做。
            print('[提示] --base-url 模式跳过设置/搜索/EQ 交互检查（不改动外部实例状态）', file=sys.stderr)
        else:
            server = ShellServer(start_timeout=max(DEFAULT_TIMEOUT, options.timeout))
            base_url = server.start()
            if any(target.name == INTERACTIVE_PLUGIN for target in targets):
                interactive = InteractiveChecks()
                interactive.start()
        report.base_url = base_url
        try:
            driver = _create_driver()
            driver.set_window_size(*WINDOW_SIZE)
            driver.get(base_url)
        except UiAuditError:
            raise
        except Exception as exc:
            raise UiAuditError(f'浏览器会话启动失败：{exc}') from exc
        for target in targets:
            print(f'[审计] {target.name}（{target.display_name}）…', file=sys.stderr)
            try:
                plugin_report = audit_plugin(driver, target, options.timeout)
                if interactive is not None and target.name == INTERACTIVE_PLUGIN:
                    print(f'[审计] {target.name} 交互检查（隔离实例）…', file=sys.stderr)
                    if not _open_plugin(interactive.driver, target, options.timeout):
                        plugin_report.add('settings.roundtrip', SKIP, '隔离实例里打不开插件页')
                        plugin_report.add('search.query-clear', SKIP, '隔离实例里打不开插件页')
                        plugin_report.add('eq.mini', SKIP, '隔离实例里打不开插件页')
                    else:
                        for name, status, detail in audit_interactive(interactive.driver, options.timeout):
                            plugin_report.add(name, status, detail)
                report.reports.append(plugin_report)
            except Exception as exc:
                broken = PluginReport(target.name, target.display_name)
                broken.add('plugin.error', FAIL, f'审计过程中抛出异常：{exc}')
                report.reports.append(broken)
    except UiAuditError as exc:
        print(f'[环境错误] {exc}', file=sys.stderr)
        return 2
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if interactive is not None:
            interactive.close()
        if server is not None:
            server.stop()

    print()
    print(format_table(report))
    print()
    print(format_summary(report))
    print(f'base_url: {report.base_url}')
    if options.json_path is not None:
        options.json_path.parent.mkdir(parents=True, exist_ok=True)
        options.json_path.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        print(f'JSON: {options.json_path}')
    return audit_exit_code(report)


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == '__main__':
    raise SystemExit(main())
