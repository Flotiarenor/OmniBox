"""`tools/check_npm_audit.py` 的判定逻辑（不需要网络、不跑 npm）。

为什么单独有用例
----------------
这条门禁的意义全在两侧都管：报告里有未登记的公告要拦住；登记表里有报告里**已经不存在**
的公告也要拦住 —— 后者才是关键，否则"例外"会随着上游修复悄悄烂成永久豁免，而门禁
永远显示 OK（这类"工具在跑、但已经不检查任何东西"的失效最难发现）。

判定逻辑抽成了 `collect()` / `judge()` 两个纯函数，所以这里可以直接构造 audit 报告
来验，不依赖 npm 与网络。
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

from tools.check_npm_audit import ACCEPTED, _run_audit, accepted_summary, collect, judge


def audit_report(*entries) -> dict:
    """按 npm audit --json 的形状构造报告。

    每个 entry 是 (包名, 严重度, ghsa, 标题)。**同一个包的多条公告要合进同一个
    `via` 数组** —— npm 的 `vulnerabilities` 以包名为键，一个包只会出现一次，
    多条公告是同一个键下的多个 `via` 项（这里最早写成"每个 entry 一个包"，
    结果同一包的多条公告互相覆盖，把用例本身测错了）。
    另外每条都塞一个字符串项，模拟"由别的包传入"的传递影响，collect() 应忽略。
    """
    vulnerabilities: dict = {}
    for name, severity, ghsa, title in entries:
        vuln = vulnerabilities.setdefault(name, {'severity': severity, 'via': []})
        vuln['severity'] = severity
        vuln['via'].append({'title': title, 'url': f'https://github.com/advisories/{ghsa}'})
        vuln['via'].append(name)
    return {'vulnerabilities': vulnerabilities}


CURRENT_VITE = ('vite', 'high', 'GHSA-4w7w-66w2-5vf9', 'vite: path traversal')
CURRENT_ESBUILD = ('esbuild', 'moderate', 'GHSA-67mh-4wv8-2f99', 'esbuild: dev server')
FAKE = ('some-pkg', 'high', 'GHSA-0000-0000-0000', 'some new vulnerability')

# 与真实 ACCEPTED 表逐条对应的"当前报告"：vite 三条 + esbuild 一条。
# 判定是两侧都管的，所以凡是断言"应当通过"的地方都必须用这份完整报告 ——
# 少一条就会触发"登记项过期"那一侧的报错（最初只造了一条 vite，用例因此失败）。
CURRENT_REPORT = (
    ('vite', 'high', 'GHSA-4w7w-66w2-5vf9', 'vite: optimized deps .map path traversal'),
    ('vite', 'high', 'GHSA-v6wh-96g9-6wx3', 'launch-editor: NTLMv2 hash disclosure'),
    ('vite', 'high', 'GHSA-fx2h-pf6j-xcff', 'vite: server.fs.deny bypass on Windows'),
    ('esbuild', 'moderate', 'GHSA-67mh-4wv8-2f99', 'esbuild: dev server request'),
)


class CollectTests(unittest.TestCase):
    def test_string_via_entries_are_ignored(self):
        """via 里的字符串是"由别的包传入"，不单独计，随来源包处理。"""
        rows = collect(audit_report(CURRENT_VITE))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], ('vite', 'high', 'GHSA-4w7w-66w2-5vf9'))

    def test_empty_report_yields_no_rows(self):
        self.assertEqual(collect({'vulnerabilities': {}}), [])
        self.assertEqual(collect({}), [])

    def test_multiple_advisories_for_one_package(self):
        report = {
            'vulnerabilities': {
                'vite': {
                    'severity': 'high',
                    'via': [
                        {'title': 'A', 'url': 'https://github.com/advisories/GHSA-aaaa-aaaa-aaaa'},
                        {'title': 'B', 'url': 'https://github.com/advisories/GHSA-bbbb-bbbb-bbbb'},
                    ],
                },
            },
        }
        self.assertEqual(len(collect(report)), 2)


class JudgeTests(unittest.TestCase):
    def test_new_advisory_is_blocked(self):
        errors = judge(collect(audit_report(*CURRENT_REPORT, FAKE)))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('GHSA-0000-0000-0000', errors[0])
        self.assertIn('some-pkg', errors[0])

    def test_fully_accepted_report_passes(self):
        errors = judge(collect(audit_report(*CURRENT_REPORT)))
        self.assertEqual(errors, [])

    def test_real_accepted_ids_cover_the_current_report(self):
        """真实登记表必须与"当前报告"逐条对应（否则门禁在真报告上要么红、要么白放行）。"""
        self.assertEqual(judge(collect(audit_report(*CURRENT_REPORT))), [])
        self.assertEqual({entry[2] for entry in CURRENT_REPORT}, set(ACCEPTED))

    def test_stale_accepted_entry_is_blocked(self):
        """上游修好后，登记项必须删掉 —— 这是"例外烂成永久豁免"的唯一防线。"""
        # 只报一条：其余登记项全部失效
        errors = judge(collect(audit_report(CURRENT_VITE)))
        self.assertEqual(len(errors), len(ACCEPTED) - 1, errors)
        for error in errors:
            self.assertIn('已不在 audit 报告里', error)

    def test_empty_report_with_entries_is_blocked(self):
        """全修好时（or 网络插件不再报告）也必须报错提醒清表，不能静默 OK。"""
        errors = judge([])
        self.assertEqual(len(errors), len(ACCEPTED))
        self.assertTrue(all('已不在 audit 报告里' in e for e in errors))

    def test_empty_report_with_empty_allowlist_passes(self):
        self.assertEqual(judge([], accepted={}), [])

    def test_accepts_custom_allowlist(self):
        rows = collect(audit_report(FAKE))
        self.assertEqual(judge(rows, accepted={FAKE[2]: (FAKE[0], '理由', '1.0')}), [])
        self.assertEqual(len(judge(rows, accepted={})), 1)


class AllowlistShapeTests(unittest.TestCase):
    def test_every_entry_states_reason_and_removal_version(self):
        """每条例外都要写清"为什么接受"与"升级到哪个版本才能消"。"""
        for ghsa, entry in ACCEPTED.items():
            with self.subTest(ghsa=ghsa):
                self.assertRegex(ghsa, r'^GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$')
                self.assertEqual(len(entry), 3, f'{ghsa} 应为 (包名, 理由, 何时能消)')
                name, reason, fixed_in = entry
                self.assertTrue(name)
                self.assertGreater(len(reason), 8, f'{ghsa} 的理由太短，等于没写')
                self.assertTrue(fixed_in, f'{ghsa} 未写明升级到哪个版本才能消')

    def test_summary_lists_the_offending_packages(self):
        summary = accepted_summary()
        self.assertIn('vite', summary)
        self.assertIn('esbuild', summary)


class RunAuditCommandTests(unittest.TestCase):
    """`_run_audit()` 真的把 `audit --json --registry=...` 传给了 npm。

    回归背景：原先用 `subprocess.run(['npm', 'audit', ...], shell=True)`。POSIX 下
    `shell=True` 会把列表拼成 `sh -c npm audit ...`，只有第一个元素是命令、其余变成
    位置参数，于是只执行 `npm`（无参数）打印 usage，拿不到 JSON；CI 从门禁实装起
    就一直是红的，而 `collect()` / `judge()` 的用例全绿。这里用 PATH 里的假 npm
    记录真实 argv，绕开对 npm 与网络的依赖。
    """

    @unittest.skipIf(os.name == 'nt', '用 POSIX shell 伪造 npm；Windows 走 .cmd 分支')
    def test_audit_arguments_reach_npm_through_the_shell(self):
        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td)
            args_file = bindir / 'args.txt'
            fake_npm = bindir / 'npm'
            fake_npm.write_text(
                '#!/bin/sh\n'
                f'printf \'%s\\n\' "$@" > "{args_file}"\n'
                'printf \'{"vulnerabilities": {}}\'\n',
                encoding='utf-8',
            )
            fake_npm.chmod(0o755)

            env = dict(os.environ)
            env['PATH'] = str(bindir) + os.pathsep + env.get('PATH', '')
            with mock.patch.dict(os.environ, env, clear=True):
                report = _run_audit()

            self.assertEqual(report, {'vulnerabilities': {}}, '假 npm 的 JSON 应被解析')
            argv = args_file.read_text(encoding='utf-8').splitlines()
            self.assertIn('audit', argv, f'npm 未收到 audit 子命令：{argv}')
            self.assertIn('--json', argv, f'npm 未收到 --json：{argv}')
            self.assertIn('--registry=https://registry.npmjs.org', argv)


if __name__ == '__main__':
    unittest.main()
