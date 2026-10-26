"""tools/plugin_ui_audit.py 的纯逻辑用例。

这个文件刻意不启动浏览器、也不起壳：目标发现、参数解析与结果汇总都在
``tools/plugin_ui_audit.py`` 的纯函数段里，可以脱离 Selenium 覆盖。浏览器侧
（壳服务、driver、DOM 断言）由 ``tools/plugin_ui_audit.py`` 自己用真实壳验证。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import plugin_ui_audit
from tools.plugin_ui_audit import (
    _CLICK_NAV_ITEM_JS,
    _FIND_FRAME_JS,
    _IFRAME_CONTENT_JS,
    _IFRAME_DOCUMENT_JS,
    _SHELL_ACTIVE_VIEW_JS,
    _SHELL_SNAPSHOT_JS,
    _SHELL_TITLE_JS,
    DEFAULT_TIMEOUT,
    EXIT_UNVERIFIED,
    FAIL,
    HOME_ENV,
    PASS,
    SKIP,
    AuditReport,
    NavItem,
    PluginReport,
    PluginTarget,
    _check_iframe_content,
    _check_nav_switch,
    _display_width,
    _fresh_console_entries,
    _nav_active_result,
    _pixel_width,
    _read_iframe_content,
    _splitter_hover_ok,
    alternate_range_value,
    audit_exit_code,
    discover_chrome_plugins,
    eq_render_ok,
    format_number,
    format_summary,
    format_table,
    parse_args,
    parse_number,
    select_targets,
    settings_field_id,
)


def _target(
    name: str,
    *,
    display_name: str | None = None,
    nav: tuple[NavItem, ...] = (),
    freshness: bool = False,
) -> PluginTarget:
    return PluginTarget(
        name=name,
        display_name=display_name or name,
        route=f'/{name}',
        nav_items=nav,
        toolbar_ids=(),
        has_freshness=freshness,
        manifest_path=Path('plugins') / name / 'manifest.json',
    )


def _write_manifest(root: Path, folder: str, manifest: object) -> None:
    target = root / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')


def _parse(*argv: str):
    with contextlib.redirect_stderr(io.StringIO()):
        return parse_args(list(argv))


class DiscoverChromePluginsTests(unittest.TestCase):
    def test_only_manifests_with_chrome_are_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_manifest(
                root,
                'alpha',
                {
                    'name': 'alpha',
                    'displayName': '甲',
                    'frontend': {
                        'route': '/alpha',
                        'chrome': {
                            'brand': {'title': '甲'},
                            'nav': [
                                {
                                    'section': '一',
                                    'items': [
                                        {'id': 'one', 'label': '第一'},
                                        {'id': 'two'},
                                    ],
                                },
                                {'items': [{'id': 'three', 'label': '第三'}]},
                            ],
                            'toolbar': [{'id': 'refresh', 'label': '刷新'}],
                            'freshness': {'unit': '项'},
                        },
                    },
                },
            )
            _write_manifest(root, 'beta', {'name': 'beta', 'frontend': {'route': '/beta'}})
            _write_manifest(root, 'delta', {'name': 'delta', 'frontend': {'chrome': None}})
            # chrome 是空对象：JS 里为真（壳会渲染一个没有导航的骨架），必须算目标。
            _write_manifest(root, 'epsilon', {'frontend': {'chrome': {}}})
            (root / 'gamma').mkdir()
            (root / 'gamma' / 'manifest.json').write_text('{ not json', encoding='utf-8')
            (root / 'eta').mkdir()
            (root / 'eta' / 'manifest.json').write_text('[1, 2]', encoding='utf-8')

            discovery = discover_chrome_plugins(root)

        self.assertEqual([target.name for target in discovery.targets], ['alpha', 'epsilon'])
        self.assertEqual(len(discovery.problems), 2)
        self.assertTrue(any(problem.startswith('gamma') for problem in discovery.problems))
        self.assertTrue(any(problem.startswith('eta') for problem in discovery.problems))

        alpha = discovery.targets[0]
        self.assertEqual(alpha.display_name, '甲')
        self.assertEqual(alpha.route, '/alpha')
        self.assertEqual([item.id for item in alpha.nav_items], ['one', 'two', 'three'])
        # 缺少 label 时回退到 id，缺 id 的项直接忽略。
        self.assertEqual([item.label for item in alpha.nav_items], ['第一', 'two', '第三'])
        self.assertEqual(alpha.toolbar_ids, ('refresh',))
        self.assertTrue(alpha.has_freshness)

        epsilon = discovery.targets[1]
        # name / displayName 都缺失时回退到目录名。
        self.assertEqual(epsilon.name, 'epsilon')
        self.assertEqual(epsilon.display_name, 'epsilon')
        self.assertEqual(epsilon.nav_items, ())
        self.assertFalse(epsilon.has_freshness)

    def test_nav_and_toolbar_tolerate_malformed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_manifest(
                root,
                'broken',
                {
                    'name': 'broken',
                    'frontend': {
                        'chrome': {
                            'nav': [
                                'not-a-group',
                                {'items': 'not-a-list'},
                                {'items': [None, {}, {'id': ''}, {'id': 'ok'}]},
                            ],
                            'toolbar': {'id': 'not-a-list'},
                        },
                    },
                },
            )
            discovery = discover_chrome_plugins(root)
        self.assertEqual(len(discovery.targets), 1)
        target = discovery.targets[0]
        self.assertEqual(target.nav_items, (NavItem('ok', 'ok'),))
        self.assertEqual(target.toolbar_ids, ())

    def test_missing_root_is_a_problem_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'nope'
            discovery = discover_chrome_plugins(missing)
        self.assertEqual(discovery.targets, ())
        self.assertEqual(len(discovery.problems), 1)
        self.assertIn('nope', discovery.problems[0])


class SelectTargetsTests(unittest.TestCase):
    def test_empty_request_selects_everything_in_manifest_order(self) -> None:
        alpha = _target('alpha')
        beta = _target('beta')
        selected, unknown = select_targets([alpha, beta], [])
        self.assertEqual(selected, [alpha, beta])
        self.assertEqual(unknown, [])

    def test_request_order_is_preserved_and_duplicates_dropped(self) -> None:
        alpha = _target('alpha')
        beta = _target('beta')
        selected, unknown = select_targets([alpha, beta], ['beta', 'alpha', 'beta'])
        self.assertEqual([target.name for target in selected], ['beta', 'alpha'])
        self.assertEqual(unknown, [])

    def test_unknown_names_are_reported_not_silently_skipped(self) -> None:
        alpha = _target('alpha')
        selected, unknown = select_targets([alpha], ['alpha', 'alpha-typo', ' '])
        self.assertEqual(selected, [alpha])
        self.assertEqual(unknown, ['alpha-typo'])


class ParseArgsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        options = _parse()
        self.assertEqual(options.plugins, ())
        self.assertIsNone(options.base_url)
        self.assertIsNone(options.json_path)
        self.assertEqual(options.timeout, DEFAULT_TIMEOUT)

    def test_plugin_is_repeatable_and_comma_separated(self) -> None:
        options = _parse('--plugin', 'alpha, beta', '--plugin', 'gamma', '--plugin', 'alpha')
        self.assertEqual(options.plugins, ('alpha', 'beta', 'gamma'))

    def test_base_url_is_normalised(self) -> None:
        options = _parse('--base-url', 'http://127.0.0.1:18080/')
        self.assertEqual(options.base_url, 'http://127.0.0.1:18080')

    def test_base_url_rejects_other_shapes(self) -> None:
        for value in ('127.0.0.1:18080', 'ftp://127.0.0.1:18080', 'http://127.0.0.1:18080/shell'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                _parse('--base-url', value)

    def test_timeout_must_be_positive(self) -> None:
        self.assertEqual(_parse('--timeout', '2.5').timeout, 2.5)
        for value in ('0', '-1', 'nan', 'abc'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                _parse('--timeout', value)

    def test_json_path(self) -> None:
        options = _parse('--json', '.build/audit.json')
        self.assertEqual(options.json_path, Path('.build/audit.json'))


class ReportAggregationTests(unittest.TestCase):
    def test_plugin_status_prefers_fail_then_pass_then_skip(self) -> None:
        report = PluginReport('alpha', '甲')
        report.add('a', PASS)
        report.add('b', SKIP)
        self.assertEqual(report.status, PASS)
        report.add('c', FAIL)
        self.assertEqual(report.status, FAIL)

        only_skips = PluginReport('beta', '乙')
        only_skips.add('a', SKIP)
        self.assertEqual(only_skips.status, SKIP)

    def test_audit_report_counts_and_failed(self) -> None:
        passing = PluginReport('alpha', '甲')
        passing.add('a', PASS)
        failing = PluginReport('beta', '乙')
        failing.add('a', FAIL)
        skipped = PluginReport('gamma', '丙')
        skipped.add('a', SKIP)

        report = AuditReport(base_url='http://127.0.0.1:1')
        report.reports.extend([passing, skipped])
        self.assertFalse(report.failed)
        self.assertEqual(report.counts(), {PASS: 1, FAIL: 0, SKIP: 1})

        report.reports.append(failing)
        self.assertTrue(report.failed)
        self.assertEqual(report.counts(), {PASS: 1, FAIL: 1, SKIP: 1})

    def test_as_dict_is_json_serialisable(self) -> None:
        report = PluginReport('alpha', '甲')
        report.add('nav.panel', PASS, '切换成功')
        audit = AuditReport(base_url='http://127.0.0.1:1', reports=[report])
        payload = audit.as_dict()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['exit_code'], 0)
        self.assertEqual(payload['check_counts'], {PASS: 1, FAIL: 0, SKIP: 0})
        self.assertEqual(
            payload['plugins'][0]['checks'][0],
            {'check': 'nav.panel', 'status': PASS, 'detail': '切换成功'},
        )
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn('切换成功', text)

    def test_skipped_checks_are_counted_and_fail_the_run(self) -> None:
        """一个插件 PASS 不代表它的每一项都验证过：SKIP 必须进汇总并改变退出码。"""
        report = PluginReport('alpha', '甲')
        report.add('shell.chrome', PASS)
        report.add('nav.panel', SKIP, 'iframe 内没有状态标记')
        audit = AuditReport(reports=[report])

        self.assertEqual(report.status, PASS)
        self.assertEqual(audit.counts(), {PASS: 1, FAIL: 0, SKIP: 0})
        self.assertEqual(audit.check_counts(), {PASS: 1, FAIL: 0, SKIP: 1})
        self.assertTrue(audit.unverified)
        self.assertEqual(audit_exit_code(audit), EXIT_UNVERIFIED)
        payload = audit.as_dict()
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['exit_code'], EXIT_UNVERIFIED)
        self.assertIn('SKIP 1', format_summary(audit))

    def test_fail_wins_over_skip_in_exit_code(self) -> None:
        report = PluginReport('alpha', '甲')
        report.add('a', SKIP)
        report.add('b', FAIL)
        self.assertEqual(audit_exit_code(AuditReport(reports=[report])), 1)


class ConsoleCollectionTests(unittest.TestCase):
    """ChromeDriver 的 ``get_log('browser')`` 读取即清空缓冲（实测），不能再按条数切片。

    旧实现记下"打开插件前读到 N 条"，再取 ``entries[N:]``。实测第二次读取只返回
    增量（3 条旧记录之后产生 1 条 SEVERE 时，读回的是长度 1 的列表），切片结果恒为
    空 —— 控制台错误被静默吞掉并报 PASS。这里锁住"读取即清空"与"累积型"两种语义。
    """

    def test_drained_driver_returns_new_entries(self) -> None:
        before = [{'level': 'INFO', 'message': f'old-{i}'} for i in range(3)]
        after = [{'level': 'SEVERE', 'message': 'boom'}]
        self.assertEqual(_fresh_console_entries(before, after), after)

    def test_cumulative_driver_drops_the_known_prefix(self) -> None:
        before = [{'level': 'INFO', 'message': 'old'}]
        after = [*before, {'level': 'SEVERE', 'message': 'boom'}]
        self.assertEqual(_fresh_console_entries(before, after), after[1:])

    def test_empty_before_keeps_everything(self) -> None:
        after = [{'level': 'INFO', 'message': 'a'}]
        self.assertEqual(_fresh_console_entries([], after), after)
        self.assertEqual(_fresh_console_entries(None, after), after)

    def test_unreadable_log_returns_none(self) -> None:
        self.assertIsNone(_fresh_console_entries([{'level': 'INFO'}], None))

    def test_check_console_sees_severe_from_a_drained_driver(self) -> None:
        before = [{'level': 'INFO', 'message': f'old-{i}'} for i in range(3)]
        fresh = [{'level': 'SEVERE', 'message': 'Uncaught TypeError'}]
        with mock.patch.object(plugin_ui_audit, '_console_entries', return_value=fresh):
            status, detail = plugin_ui_audit._check_console(object(), before)
        self.assertEqual(status, FAIL)
        self.assertIn('SEVERE 1 条', detail)
        self.assertIn('Uncaught TypeError', detail)

    def test_check_console_passes_without_severe(self) -> None:
        before = [{'level': 'INFO', 'message': f'old-{i}'} for i in range(3)]
        with mock.patch.object(plugin_ui_audit, '_console_entries',
                               return_value=[{'level': 'INFO', 'message': 'ok'}]):
            status, _detail = plugin_ui_audit._check_console(object(), before)
        self.assertEqual(status, PASS)


class _FakeSwitchTo:
    def frame(self, frame: object) -> None:
        pass

    def default_content(self) -> None:
        pass


class _FakeDriver:
    """只实现审计工具用到的 ``execute_script`` / ``switch_to``。"""

    def __init__(self, handlers: dict[str, object]) -> None:
        self._handlers = handlers
        self.switch_to = _FakeSwitchTo()

    def execute_script(self, script: str, *args: object) -> object:
        return self._handlers.get(script)


def _snapshot(
    ids: tuple[str, ...],
    *,
    active_index: int = 0,
    title: str = '旧标题',
    frame_src: str = '/plugins/alpha/frontend/index.html?content=1',
) -> dict[str, object]:
    return {
        'frameFound': True,
        'pageFound': True,
        'sidebarFound': True,
        'navCount': len(ids),
        'nav': [
            {'text': f'标签{index}', 'active': index == active_index, 'viewId': view_id}
            for index, view_id in enumerate(ids)
        ],
        'title': title,
        'freshness': True,
        'visible': True,
        'frameSrc': frame_src,
        'activeView': ids[active_index],
    }


class ChromeDeclarationTests(unittest.TestCase):
    def test_hidden_nav_items_and_empty_freshness_object(self) -> None:
        """`visible:false` 的组/项不参与应渲染计数；`freshness: {}` 是声明了。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_manifest(
                root,
                'delta',
                {
                    'name': 'delta',
                    'frontend': {
                        'route': '/delta',
                        'chrome': {
                            'freshness': {},
                            'nav': [
                                {
                                    'id': 'main',
                                    'items': [
                                        {'id': 'a'},
                                        {'id': 'b', 'visible': False},
                                    ],
                                },
                                {'id': 'tools', 'visible': False, 'items': [{'id': 'c'}]},
                            ],
                        },
                    },
                },
            )
            _write_manifest(
                root,
                'epsilon',
                {'name': 'epsilon', 'frontend': {'chrome': {'freshness': False}}},
            )
            discovery = discover_chrome_plugins(root)

        delta = next(target for target in discovery.targets if target.name == 'delta')
        self.assertTrue(delta.has_freshness, 'freshness: {} 在 JS 里为真，不算未声明')
        self.assertEqual([item.id for item in delta.nav_items], ['a', 'b', 'c'])
        self.assertEqual([item.id for item in delta.visible_nav_items], ['a'])
        epsilon = next(target for target in discovery.targets if target.name == 'epsilon')
        self.assertFalse(epsilon.has_freshness, 'freshness: false 是显式关闭')


class IframeContentReadinessTests(unittest.TestCase):
    """iframe.content-only 必须等文档 readyState=complete 再判定。"""

    def _driver(self, ready_state: str) -> _FakeDriver:
        return _FakeDriver({
            _FIND_FRAME_JS: object(),
            _IFRAME_DOCUMENT_JS: True,
            _IFRAME_CONTENT_JS: {
                'ready': ready_state,
                'href': 'http://127.0.0.1:1/plugins/alpha/frontend/index.html?content=1',
                'found': [],
            },
            _SHELL_SNAPSHOT_JS: {'frameSrc': '/plugins/alpha/frontend/index.html?content=1'},
        })

    def test_incomplete_document_is_not_read_as_content(self) -> None:
        target = _target('alpha')
        self.assertIsNone(_read_iframe_content(self._driver('interactive'), target, 0.3))

    def test_incomplete_document_fails_content_only(self) -> None:
        target = _target('alpha')
        status, detail = _check_iframe_content(self._driver('interactive'), target, 0.3)
        self.assertEqual(status, FAIL, detail)

    def test_complete_document_is_returned(self) -> None:
        target = _target('alpha')
        data = _read_iframe_content(self._driver('complete'), target, 0.3)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data['ready'], 'complete')


class NavPanelShellMarkerTests(unittest.TestCase):
    """nav.panel 改用壳侧 data-view-id/data-active-view，不再依赖插件内容页标记。"""

    def test_active_result_compares_shell_marker(self) -> None:
        self.assertEqual(_nav_active_result('v2', 'v2', '二')[0], PASS)
        status, detail = _nav_active_result('v1', 'v2', '二')
        self.assertEqual(status, FAIL)
        self.assertIn('v2', detail)

    def test_hidden_declared_items_do_not_block_mapping(self) -> None:
        """manifest 里 visible:false 的项不计数：2 个可见项对 2 个壳侧导航项可映射。"""
        target = _target('alpha', nav=(
            NavItem('v1', '一'),
            NavItem('v2', '二'),
            NavItem('v3', '三', visible=False),
        ))
        driver = _FakeDriver({
            _CLICK_NAV_ITEM_JS: True,
            _SHELL_ACTIVE_VIEW_JS: 'v2',
            _SHELL_TITLE_JS: '新标题',
        })
        result = _check_nav_switch(driver, target, _snapshot(('v1', 'v2')), 0.3)
        self.assertEqual(result['panel'][0], PASS, result['panel'][1])
        self.assertEqual(result['title'][0], PASS, result['title'][1])

    def test_title_kept_constant_is_skip_not_fail_when_view_switched(self) -> None:
        """视图已切换而标题按设计不变：nav.title 记 SKIP，不新增 FAIL。"""
        target = _target('alpha', nav=(NavItem('v1', '一'), NavItem('v2', '二')))
        driver = _FakeDriver({
            _CLICK_NAV_ITEM_JS: True,
            _SHELL_ACTIVE_VIEW_JS: 'v2',
            _SHELL_TITLE_JS: '旧标题',
        })
        result = _check_nav_switch(driver, target, _snapshot(('v1', 'v2')), 0.3)
        self.assertEqual(result['panel'][0], PASS, result['panel'][1])
        self.assertEqual(result['title'][0], SKIP, result['title'][1])


class InteractiveHelperTests(unittest.TestCase):
    """设置往返 / 搜索 / mini EQ 的纯逻辑（真实壳部分在 tools/plugin_ui_audit.py）。"""

    def test_settings_field_id_matches_modal_rule(self) -> None:
        self.assertEqual(
            settings_field_id('media-player', 'lyrics_font_size'),
            'plugin-settings-media-player-lyrics_font_size',
        )

    def test_parse_and_format_numbers_round_trip(self) -> None:
        self.assertEqual(parse_number('16'), 16)
        self.assertEqual(parse_number('1.6'), 1.6)
        self.assertIsNone(parse_number(''))
        self.assertIsNone(parse_number('abc'))
        self.assertIsNone(parse_number('nan'))
        self.assertIsNone(parse_number('inf'))
        self.assertEqual(format_number(12), '12')
        self.assertEqual(format_number(1.6), '1.6')
        self.assertEqual(format_number(0.5), '0.5')

    def test_alternate_range_value_always_differs_when_possible(self) -> None:
        self.assertEqual(alternate_range_value(12, 40, 16), 12)
        self.assertEqual(alternate_range_value(12, 40, 12), 40)
        self.assertIsNone(alternate_range_value(12, 12, 12))
        self.assertIsNone(alternate_range_value(40, 12, 16))

    def test_eq_render_ok_requires_four_bars(self) -> None:
        self.assertTrue(eq_render_ok({'kind': 'eq-text', 'bars': 4}))
        self.assertFalse(eq_render_ok({'kind': 'eq-text', 'bars': 3}))
        self.assertFalse(eq_render_ok({'kind': 'dot-text', 'bars': 4}))
        self.assertFalse(eq_render_ok(None))

    def test_isolated_home_env_matches_paths_contract(self) -> None:
        from shell.backend.paths import USER_DATA_DIR_ENV

        self.assertEqual(HOME_ENV, USER_DATA_DIR_ENV)


class SplitterHoverLogicTests(unittest.TestCase):
    """2px 强调色线的判定是纯逻辑，不需要浏览器也能覆盖。"""

    ACCENT = 'rgb(0, 120, 212)'

    def test_pixel_width_parses_computed_style_values(self) -> None:
        self.assertEqual(_pixel_width('2px'), 2.0)
        self.assertEqual(_pixel_width('1px'), 1.0)
        self.assertIsNone(_pixel_width(None))
        self.assertIsNone(_pixel_width('auto'))

    def test_hover_requires_two_pixel_accent_line(self) -> None:
        good = {'hovered': True, 'width': '2px', 'background': self.ACCENT, 'accent': self.ACCENT}
        self.assertTrue(_splitter_hover_ok(good))
        self.assertFalse(_splitter_hover_ok({**good, 'hovered': False}))
        self.assertFalse(_splitter_hover_ok({**good, 'width': '1px'}))
        self.assertFalse(_splitter_hover_ok({**good, 'background': 'rgba(0, 0, 0, 0)'}))
        self.assertFalse(_splitter_hover_ok({**good, 'background': 'rgb(1, 2, 3)'}))


class TableTests(unittest.TestCase):
    def test_display_width_counts_cjk_as_two_columns(self) -> None:
        self.assertEqual(_display_width(''), 0)
        self.assertEqual(_display_width('abc'), 3)
        self.assertEqual(_display_width('中a'), 3)
        self.assertEqual(_display_width('Ａ'), 2)

    def test_table_lists_every_check_and_plugin(self) -> None:
        alpha = PluginReport('alpha', '甲')
        alpha.add('shell.chrome', PASS, '.plugin-page 已渲染')
        alpha.add('nav.title', SKIP, '只有一个导航项')
        beta = PluginReport('beta', '乙')
        beta.add('console', FAIL, '新增 SEVERE 1 条')

        table = format_table(AuditReport(reports=[alpha, beta]))
        lines = table.splitlines()

        self.assertIn('插件', lines[0])
        self.assertIn('说明', lines[0])
        self.assertEqual(len(lines), 4)
        for token in ('alpha', 'beta', 'shell.chrome', 'nav.title', 'console', PASS, SKIP, FAIL):
            self.assertIn(token, table)

    def test_plugin_without_checks_still_gets_a_row(self) -> None:
        empty = PluginReport('empty', '空')
        table = format_table(AuditReport(reports=[empty]))
        self.assertIn('empty', table)
        self.assertIn('没有检查项', table)

    def test_summary_counts_plugin_statuses(self) -> None:
        passing = PluginReport('alpha', '甲')
        passing.add('a', PASS)
        skipped = PluginReport('beta', '乙')
        skipped.add('a', SKIP)
        summary = format_summary(AuditReport(reports=[passing, skipped]))
        self.assertEqual(
            summary,
            '共 2 个插件：PASS 1 / FAIL 0 / SKIP 1；'
            '检查项 2 项：PASS 1 / FAIL 0 / SKIP 1（SKIP = 未验证，不算通过）',
        )


if __name__ == '__main__':
    unittest.main()
