"""2.0 迁移脚本：把仓库里最后一层 omnibox 旧名清干净。

为什么单独成一个脚本：这次迁移同时动协议族、鉴权 Cookie 与请求头、密钥存储标识、
构建产物白名单、历史数据迁移代码、服务器侧脚本与规范文档里的历史引文。散着改必然
漏，逐条 grep 又分不清"该改"与"该留"。脚本把每一步写成一个可复核的编辑：默认只
预演，`--apply` 才落盘，最后自己再扫一遍残留。

它只处理**仓库内的文本文件**。用户机器上的 `%APPDATA%/OmniBox` 旧目录、服务器上的
`/root/OmniBox` 目录与 `omnibox-web.service` 单元不在能力范围内：仓库里的引用会被改，
那两处本身要单独迁移（目录改名、单元改名、SSH 别名改名）。

用法::

    python tools/migrate_cubby_2.py                    # 预演，列出每一步
    python tools/migrate_cubby_2.py --apply            # 执行
    python tools/migrate_cubby_2.py --check            # 只查残留
    python tools/migrate_cubby_2.py --apply --version 2.0.0

执行完后仍应手动跑一遍提交前清单（ruff、pyright、unittest、shell_e2e_smoke）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF_REL = 'tools/migrate_cubby_2.py'
BOM = b'\xef\xbb\xbf'
# 超过这个大小的文件不当作文本处理：仓库里的大家伙是图标 sprite 与截图。
TEXT_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class Edit:
    """一处精确编辑：`old` 必须命中 `count` 次，命中 0 次且 `new` 已在文件里视为已迁移。"""

    path: str
    old: str
    new: str
    why: str
    regex: bool = False
    count: int = 1


@dataclass(frozen=True)
class GlobalReplace:
    """跨文件的字节级替换，保留 BOM 与行尾风格。"""

    old: bytes
    new: bytes
    why: str


EDITS: list[Edit] = [
    # ---- shell/backend/paths.py：删掉 %APPDATA% 一次性迁移 ----
    Edit(
        path='shell/backend/paths.py',
        old=r'\n\n\ndef _migrate_legacy_user_data_dir\(fallback: Path\) -> Path:\n[\s\S]*?\n    return target',
        new='',
        why='删除 %APPDATA%/OmniBox 迁移函数（2.0 视为全新安装）',
        regex=True,
    ),
    Edit(
        path='shell/backend/paths.py',
        old='import logging\n',
        new='',
        why='迁移函数删除后不再需要 logging',
    ),
    Edit(
        path='shell/backend/paths.py',
        old='\nlog = logging.getLogger(__name__)\n',
        new='',
        why='迁移函数删除后不再需要模块级 logger',
    ),
    Edit(
        path='shell/backend/paths.py',
        old='return _migrate_legacy_user_data_dir(fallback).resolve()',
        new="return (fallback / 'Cubby').resolve()",
        why='回退目录直接用 %APPDATA%/Cubby，不再读旧目录',
    ),
    # ---- shell/groupmesh/secret_store.py：存储标识一并换新 ----
    Edit(
        path='shell/groupmesh/secret_store.py',
        old='"format": "omnibox-secret-v1",',
        new='"format": "cubby-secret-v1",',
        why='docstring 里的信封格式示例',
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old="FORMAT = 'omnibox-secret-v1'",
        new="FORMAT = 'cubby-secret-v1'",
        why='密文信封格式标识',
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old="ENTROPY = b'omnibox.group-mesh.secret.v1'",
        new="ENTROPY = b'cubby.group-mesh.secret.v1'",
        why='DPAPI 附加熵',
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old="KEYRING_SERVICE = 'omnibox-group-mesh'",
        new="KEYRING_SERVICE = 'cubby-group-mesh'",
        why='OS keyring 服务名',
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old="_KEYRING_PROBE_USER = '__omnibox_probe__'",
        new="_KEYRING_PROBE_USER = '__cubby_probe__'",
        why='keyring 探测用条目名',
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old="descr = ctypes.c_wchar_p('omnibox group-mesh secret')",
        new="descr = ctypes.c_wchar_p('cubby group-mesh secret')",
        why='DPAPI 描述串',
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old=r'\n# 旧环境变量：\*\*不读取\*\*[\s\S]*?_legacy_warned: set\[str\] = set\(\)\n',
        new='',
        why='删除旧环境变量告警表（2.0 不再检测旧名）',
        regex=True,
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old=r'\n\ndef warn_legacy_env\(\) -> None:\n[\s\S]*?请改用 %s\', legacy, current\)\n',
        new='',
        why='删除旧环境变量告警函数',
        regex=True,
    ),
    Edit(
        path='shell/groupmesh/secret_store.py',
        old='    warn_legacy_env()\n',
        new='',
        why='删除两处告警调用',
        count=2,
    ),
    # ---- shell/backend/auth.py：Cookie 换名、旧头停收 ----
    Edit(
        path='shell/backend/auth.py',
        old='- Cookie: `omnibox_token=<token>`',
        new='- Cookie: `cubby_token=<token>`',
        why='docstring 里的 Cookie 名',
    ),
    Edit(
        path='shell/backend/auth.py',
        old="""\n旧请求头 `X-Omnibox-Token` 仍然接受（存量脚本与反代配置不必同步改），但不作为
文档口径：新写的东西一律用 `X-Cubby-Token`。Cookie 名保持 `omnibox_token` 不动。'''""",
        new="'''",
        why='删除“旧头仍接受 / Cookie 名不变”的说明',
    ),
    Edit(
        path='shell/backend/auth.py',
        old="TOKEN_COOKIE = 'omnibox_token'",
        new="TOKEN_COOKIE = 'cubby_token'",
        why='Cookie 名换新',
    ),
    Edit(
        path='shell/backend/auth.py',
        old="# 旧请求头：只作为被接受的别名保留，不被新代码写入任何响应或文档示例。\n"
            "LEGACY_TOKEN_HEADER = 'X-Omnibox-Token'\n",
        new='',
        why='删除旧请求头常量',
    ),
    # ---- shell/backend/file_server.py：只认新头 ----
    Edit(
        path='shell/backend/file_server.py',
        old='    LEGACY_TOKEN_HEADER,\n',
        new='',
        why='删除旧头导入',
    ),
    Edit(
        path='shell/backend/file_server.py',
        old="""        supplied = (request.cookies.get(TOKEN_COOKIE, '')
                    or request.headers.get(TOKEN_HEADER, '')
                    or request.headers.get(LEGACY_TOKEN_HEADER, ''))""",
        new="""        supplied = (request.cookies.get(TOKEN_COOKIE, '')
                    or request.headers.get(TOKEN_HEADER, ''))""",
        why='凭据只从 Cookie 与新头取',
    ),
    Edit(
        path='shell/backend/file_server.py',
        old="""        1. 校验凭据（Cookie 或请求头二选一，请求头认 `X-Cubby-Token`，旧
           `X-Omnibox-Token` 作为别名继续接受）；""",
        new="        1. 校验凭据（Cookie 或请求头二选一，请求头认 `X-Cubby-Token`）；",
        why='鉴权 docstring 去掉旧头说明',
    ),
    # ---- tests/test_file_server_host.py：旧头由“接受”翻成“拒绝” ----
    Edit(
        path='tests/test_file_server_host.py',
        old='"""鉴权头：新名 `X-Cubby-Token` 生效，旧名 `X-Omnibox-Token` 仍被接受。"""',
        new='"""鉴权头：`X-Cubby-Token` 是唯一被接受的请求头。"""',
        why='用例类 docstring',
    ),
    Edit(
        path='tests/test_file_server_host.py',
        old="""    def test_legacy_header_still_accepted(self):
        \"\"\"存量 curl 脚本与反代配置不必同步改：旧头只作为被接受的别名保留。\"\"\"
        resp = self._post({'X-Omnibox-Token': self.token})
        self.assertEqual(resp.status_code, 200)""",
        new="""    def test_legacy_header_rejected(self):
        \"\"\"旧头在 2.0 不再接受：新旧版本混用时应当立刻显形，而不是静默通过。\"\"\"
        resp = self._post({'X-Omnibox-Token': self.token})
        self.assertEqual(resp.status_code, 401)""",
        why='旧头行为反转',
    ),
    Edit(
        path='tests/test_file_server_host.py',
        old="""    def test_wrong_token_rejected_on_both_headers(self):
        for header in (TOKEN_HEADER, 'X-Omnibox-Token'):
            with self.subTest(header=header):
                self.assertEqual(self._post({header: 'bad-token'}).status_code, 401)""",
        new="""    def test_wrong_token_rejected(self):
        self.assertEqual(self._post({TOKEN_HEADER: 'bad-token'}).status_code, 401)""",
        why='错令牌用例只剩新头',
    ),
    # ---- tools/check_docs.py：门禁不再认旧名 ----
    Edit(
        path='tools/check_docs.py',
        old="""# `OmniBox` / `omnibox` 与 `Cubby` / `cubby` 都是发布构建的输出树（docs/Releases/）：
# 它带着 plugins/ 的副本，其中包含插件的 AGENTS.md。那些副本的引用是相对**源码树**
# 写的（../AGENTS.md、../docs/…），放进索引后会被当成损坏引用报出来 —— 门禁因此
# 变成"工作区没构建过才通过"，与它自己的用途相反。
# 改名过渡期两套名字都保留：本机还留着旧名的构建树，删早了门禁就红。""",
        new="""# `Cubby` / `cubby` 是发布构建的输出树（docs/Releases/）：它带着 plugins/ 的副本，
# 其中包含插件的 AGENTS.md。那些副本的引用是相对**源码树**写的（../AGENTS.md、
# ../docs/…），放进索引后会被当成损坏引用报出来 —— 门禁因此变成"工作区没构建过才
# 通过"，与它自己的用途相反。""",
        why='SKIP_DIRS 注释去掉过渡期两套名字',
    ),
    Edit(
        path='tools/check_docs.py',
        old="    'OmniBox', 'omnibox', 'Cubby', 'cubby',",
        new="    'Cubby', 'cubby',",
        why='SKIP_DIRS 只留新名',
    ),
    Edit(
        path='tools/check_docs.py',
        old="""# 精确到某一份产物（不按前缀匹配）：`Cubby/`（旧名 `OmniBox/`）是 PyInstaller 的输出树，
# `docs/Releases/AGENTS.md` 的产物表按该目录内的相对路径写它，真跑过构建才有。
# 过渡期两套名字都保留，理由同 SKIP_DIRS。
GENERATED_NAMES = frozenset({'__pycache__', 'se-metadata.json', 'OmniBox/', 'Cubby/'})""",
        new="""# 精确到某一份产物（不按前缀匹配）：`Cubby/` 是 PyInstaller 的输出树，
# `docs/Releases/AGENTS.md` 的产物表按该目录内的相对路径写它，真跑过构建才有。
GENERATED_NAMES = frozenset({'__pycache__', 'se-metadata.json', 'Cubby/'})""",
        why='GENERATED_NAMES 只留新名',
    ),
    # ---- tests/test_check_docs.py：同步门禁用例 ----
    Edit(
        path='tests/test_check_docs.py',
        old="""        否则同一条门禁在本机（构建过）通过、在 CI（没构建）红色 —— 它的价值反过来变成噪声。
        两套名字都在：改名过渡期旧名的构建树还留在本机工作区里。
        \"\"\"
        for token in ('shell/frontend/dist/index.html', 'OmniBox/', 'Cubby/'):""",
        new="""        否则同一条门禁在本机（构建过）通过、在 CI（没构建）红色 —— 它的价值反过来变成噪声。
        \"\"\"
        for token in ('shell/frontend/dist/index.html', 'Cubby/'):""",
        why='生成物引用用例去掉旧名',
    ),
    Edit(
        path='tests/test_check_docs.py',
        old="""        \"\"\"发布构建的输出树（docs/Releases/OmniBox/ 与 Cubby/）带插件文档副本，不参与门禁。

        真实事故：本机构建过一次 release 后，这个目录里的 plugins/*/AGENTS.md 被
        当作仓库文档扫描，报出 488 条引用缺失；同一棵树在 CI 上不存在，于是门禁在
        本机与 CI 上给出相反结论。改名过渡期两套名字都要兜住。
        \"\"\"
        for tree in ('OmniBox', 'Cubby'):
            self._write(f'docs/Releases/{tree}/plugins/AGENTS.md',
                        '# 打包副本\\n\\n`RUNTIME_FIELD_READERS`\\n\\n见 [根](../AGENTS.md)。\\n')
            self._write(f'docs/Releases/{tree}/plugins/alpha/backend/main.py', 'x = 1\\n')""",
        new="""        \"\"\"发布构建的输出树（docs/Releases/Cubby/）带插件文档副本，不参与门禁。

        真实事故：本机构建过一次 release 后，这个目录里的 plugins/*/AGENTS.md 被
        当作仓库文档扫描，报出 488 条引用缺失；同一棵树在 CI 上不存在，于是门禁在
        本机与 CI 上给出相反结论。
        \"\"\"
        self._write('docs/Releases/Cubby/plugins/AGENTS.md',
                    '# 打包副本\\n\\n`RUNTIME_FIELD_READERS`\\n\\n见 [根](../AGENTS.md)。\\n')
        self._write('docs/Releases/Cubby/plugins/alpha/backend/main.py', 'x = 1\\n')""",
        why='跳过构建树用例去掉旧名分支',
    ),
    # ---- .gitignore：产物与一键脚本只留新名 ----
    Edit(
        path='.gitignore',
        old="""# 发布构建的输出树：改名过渡期满工作区里两套名字都可能存在（旧名构建树还没删），
# 大小写两套都忽略，收尾提交时再删旧名。
docs/Releases/omnibox/
docs/Releases/cubby/""",
        new="""# 发布构建的输出树（PyInstaller 的 onedir 产物，构建时才有）
docs/Releases/cubby/""",
        why='忽略项只留新名',
    ),
    Edit(
        path='.gitignore',
        old="""#linux 启动脚本（过渡期旧名与新名都忽略）
OmniBox-OneClick.ps1
Cubby-OneClick.ps1""",
        new="""#linux 启动脚本
Cubby-OneClick.ps1""",
        why='一键脚本忽略项只留新名',
    ),
    Edit(
        path='.gitignore',
        old="""docs/Releases/Cubby/
docs/Releases/OmniBox.bin
docs/Releases/Cubby.bin""",
        new="""docs/Releases/Cubby/
docs/Releases/Cubby.bin""",
        why='Linux 产物忽略项去掉旧名 exe',
    ),
    # ---- docs：历史说明与引文 ----
    Edit(
        path='docs/commit-convention.md',
        old='| `fix(release): Linux 产物校验用重命名后的 OmniBox，修复首次发布就挂的 bug` |',
        new='| `fix(release): Linux 产物校验用重命名后的可执行文件，修复首次发布就挂的 bug` |',
        why='反例表格改用不含旧名的真实写法',
    ),
    Edit(
        path='readme.md',
        old='\n旧的 `X-Omnibox-Token` 请求头仍被接受，已部署的脚本无需立刻改动；Cookie 名也沿用历史命名 `omnibox_token`。\n',
        new='',
        why='删除“旧头仍接受、Cookie 沿用旧名”一段',
    ),
    Edit(
        path='docs/development.md',
        old='> 旧的 `X-Omnibox-Token` 请求头仍被接受，既有脚本无需立刻改动。\n',
        new='',
        why='删除旧头仍接受的说明',
    ),
    Edit(
        path='docs/plugin-guide.md',
        old='消息类型统一带 `omnibox:` 前缀；协议前缀沿用历史命名 omnibox，与品牌无关。',
        new='消息类型统一带 `omnibox:` 前缀。',
        why='删除“协议前缀沿用历史命名”一句',
    ),
    Edit(
        path='docs/plugin-guide.md',
        old='（`omnibox_token`）',
        new='（`cubby_token`）',
        why='插件指南里的 Cookie 名',
    ),
    Edit(
        path='docs/plugin-guide.md',
        old='`X-Cubby-Token: <token>`（旧头 `X-Omnibox-Token` 仍被接受）；',
        new='`X-Cubby-Token: <token>`；',
        why='插件指南去掉旧头说明',
    ),
    Edit(
        path='docs/plugin-guide.md',
        old='curl 测试需带 `X-Cubby-Token` 头（旧头 `X-Omnibox-Token` 仍接受）或先访问首页拿 Cookie',
        new='curl 测试需带 `X-Cubby-Token` 头，或先访问首页拿 Cookie',
        why='插件指南调试一节去掉旧头说明',
    ),
    Edit(
        path='docs/plugin-page-architecture.md',
        old='消息类型统一带 `omnibox:` 前缀；协议前缀沿用历史命名\nomnibox，与品牌无关。',
        new='消息类型统一带 `omnibox:` 前缀。',
        why='页面架构文档去掉“沿用历史命名”一句',
    ),
    Edit(
        path='plugins/manga-library/frontend/js/app.js',
        old='// UI 绑定（chrome 的监听全部换成 omnibox 消息，这里只剩内容弹窗）',
        new='// UI 绑定（chrome 的监听全部换成 cubby 消息，这里只剩内容弹窗）',
        why='插件注释里的协议说法',
    ),
    Edit(
        path='tools/check_packaging.py',
        old='# 直接判失败，旧名（omnibox*.spec）已被 git mv 掉，登记进来只会让门禁必红；',
        new='# 直接判失败，spec 文件名变更时这里必须同步，否则门禁必红；',
        why='打包门禁注释去掉旧 spec 名',
    ),
]

GLOBAL_REPLACES: list[GlobalReplace] = [
    GlobalReplace(b'omnibox:', b'cubby:', why='协议族 omnibox:* → cubby:*'),
    GlobalReplace(b'omnibox_token', b'cubby_token', why='鉴权 Cookie 名'),
    GlobalReplace(b'/root/OmniBox', b'/root/Cubby', why='服务器检出目录'),
    GlobalReplace(b'omnibox-linux', b'cubby-linux', why='SSH 主机别名'),
    GlobalReplace(b'omnibox-web.service', b'cubby-web.service', why='systemd 单元名'),
    GlobalReplace(b'OmniBox-OneClick.ps1', b'Cubby-OneClick.ps1', why='一键脚本文件名'),
]

DELETIONS: list[tuple[str, str]] = [
    ('tests/test_user_data_migration.py', '2.0 不再迁移 %APPDATA%/OmniBox，该用例组随之删除'),
]

LOCAL_RENAMES: list[tuple[str, str]] = [
    ('OmniBox-OneClick.ps1', 'Cubby-OneClick.ps1'),
]


def _tracked() -> list[str]:
    proc = subprocess.run(
        ['git', 'ls-files'], cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f'git ls-files 失败：{proc.stderr.strip()}')
    return [line for line in proc.stdout.splitlines() if line]


def _read_text(path: Path) -> tuple[str, str, bool]:
    raw = path.read_bytes()
    has_bom = raw.startswith(BOM)
    if has_bom:
        raw = raw[3:]
    text = raw.decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    return text.replace('\r\n', '\n'), newline, has_bom


def _write_text(path: Path, text: str, newline: str, has_bom: bool) -> None:
    data = text.replace('\n', newline).encode('utf-8')
    path.write_bytes((BOM if has_bom else b'') + data)


def _apply_edits(apply: bool) -> list[str]:
    report: list[str] = []
    grouped: dict[str, list[Edit]] = {}
    for edit in EDITS:
        grouped.setdefault(edit.path, []).append(edit)
    for rel, group in grouped.items():
        path = ROOT / rel
        if not path.is_file():
            report.append(f'[缺文件] {rel}')
            continue
        text, newline, has_bom = _read_text(path)
        changed = False
        for edit in group:
            if edit.regex:
                pattern = re.compile(edit.old)
                found = len(pattern.findall(text))
                if found == 0:
                    report.append(f'[已迁移/未命中] {rel}: {edit.why}')
                    continue
                if found != edit.count:
                    report.append(f'[命中数不符] {rel}: {edit.why}（{found} != {edit.count}）')
                    continue
                text = pattern.sub(lambda _m, replacement=edit.new: replacement, text)
            else:
                found = text.count(edit.old)
                if found == 0:
                    report.append(f'[已迁移/未命中] {rel}: {edit.why}')
                    continue
                if found != edit.count:
                    report.append(f'[命中数不符] {rel}: {edit.why}（{found} != {edit.count}）')
                    continue
                text = text.replace(edit.old, edit.new)
            changed = True
            report.append(f'[编辑] {rel}: {edit.why}')
        if changed and apply:
            _write_text(path, text, newline, has_bom)
    return report


def _apply_global(apply: bool) -> list[str]:
    report: list[str] = []
    for rel in _tracked():
        if rel == SELF_REL:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if len(raw) > TEXT_LIMIT or b'\x00' in raw[:4096]:
            continue
        new = raw
        hits: list[str] = []
        for rep in GLOBAL_REPLACES:
            count = new.count(rep.old)
            if count:
                hits.append(f'{rep.why} ×{count}')
                new = new.replace(rep.old, rep.new)
        if not hits:
            continue
        report.append(f'[替换] {rel}: ' + '，'.join(hits))
        if apply:
            path.write_bytes(new)
    return report


def _delete_files(apply: bool) -> list[str]:
    report: list[str] = []
    for rel, why in DELETIONS:
        path = ROOT / rel
        if not path.is_file():
            report.append(f'[已删除] {rel}')
            continue
        report.append(f'[删除] {rel}: {why}')
        if apply:
            path.unlink()
    return report


def _rename_local(apply: bool) -> list[str]:
    report: list[str] = []
    for old, new in LOCAL_RENAMES:
        source = ROOT / old
        target = ROOT / new
        if not source.is_file():
            report.append(f'[本地无需改名] {old}')
            continue
        if target.exists():
            report.append(f'[本地改名跳过] {old}: 目标 {new} 已存在')
            continue
        report.append(f'[本地改名] {old} → {new}（未跟踪的运维脚本，不进提交）')
        if apply:
            source.rename(target)
    return report


def _bump_version(version: str, apply: bool) -> list[str]:
    report: list[str] = []
    pyproject = ROOT / 'pyproject.toml'
    text, newline, has_bom = _read_text(pyproject)
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if match is None:
        return ['[版本] pyproject.toml 里找不到 version']
    current = match.group(1)
    report.append(f'[版本] {current} → {version}')

    def _replace_pyproject(src: str) -> str:
        start = src.index('[project]')
        end = src.find('\n[', start + 1)
        block = src[start:] if end < 0 else src[start:end]
        new_block = re.sub(
            r'(?m)^(version\s*=\s*")[^"]+(")',
            lambda m: f'{m.group(1)}{version}{m.group(2)}',
            block,
            count=1,
        )
        return src.replace(block, new_block, 1)

    if apply:
        _write_text(pyproject, _replace_pyproject(text), newline, has_bom)

    package_json = ROOT / 'shell' / 'frontend' / 'package.json'
    body, nl, bom = _read_text(package_json)
    new_body = re.sub(
        r'(?m)^(\s*"version":\s*")[^"]+(",)',
        lambda m: f'{m.group(1)}{version}{m.group(2)}',
        body,
        count=1,
    )
    report.append('[版本] shell/frontend/package.json')
    if apply:
        _write_text(package_json, new_body, nl, bom)

    lock = ROOT / 'shell' / 'frontend' / 'package-lock.json'
    lock_text, lock_nl, lock_bom = _read_text(lock)
    occurrences = lock_text.count(f'"version": "{current}"')
    lock_text = lock_text.replace(f'"version": "{current}"', f'"version": "{version}"')
    report.append(f'[版本] shell/frontend/package-lock.json ×{occurrences}')
    if apply:
        _write_text(lock, lock_text, lock_nl, lock_bom)
    return report


def _residual() -> list[str]:
    leftovers: list[str] = []
    for rel in _tracked():
        if rel == SELF_REL:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if len(raw) > TEXT_LIMIT or b'\x00' in raw[:4096]:
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if 'omni' in line.lower():
                leftovers.append(f'{rel}:{lineno}: {line.strip()[:140]}')
    return leftovers


def _print_block(title: str, lines: list[str]) -> None:
    print(f'\n=== {title} ===')
    if not lines:
        print('（无）')
        return
    for line in lines:
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='把仓库里最后一层 omnibox 旧名迁移成 cubby')
    parser.add_argument('--apply', action='store_true', help='真正写入文件（默认只预演）')
    parser.add_argument('--check', action='store_true', help='只扫描残留，不做任何编辑')
    parser.add_argument('--version', default='', help='顺带把版本号改成这个值，例如 2.0.0')
    args = parser.parse_args(argv)

    if args.check:
        leftovers = _residual()
        _print_block('残留扫描', leftovers)
        return 1 if leftovers else 0

    if args.apply and subprocess.run(
        ['git', 'status', '--porcelain', '--untracked-files=no'], cwd=str(ROOT),
        capture_output=True, text=True, check=False,
    ).stdout.strip():
        print('工作区有未提交改动，先提交或 stash 再跑 --apply', file=sys.stderr)
        return 2

    mode = '执行' if args.apply else '预演'
    print(f'迁移脚本（{mode}）。加 --apply 才会写文件。')

    _print_block('1/5 精确编辑', _apply_edits(args.apply))
    _print_block('2/5 跨文件替换', _apply_global(args.apply))
    _print_block('3/5 删除文件', _delete_files(args.apply))
    _print_block('4/5 本地未跟踪脚本改名', _rename_local(args.apply))
    if args.version:
        _print_block('5/5 版本号', _bump_version(args.version, args.apply))
    else:
        _print_block('5/5 版本号', ['未指定 --version，版本号保持不变'])

    leftovers = _residual()
    _print_block('残留扫描', leftovers)
    if not args.apply:
        return 0
    if leftovers:
        print('\n仍有残留：上面每一条都要么补一条编辑，要么写进脚本的排除说明。')
        return 1
    print('\n仓库里已没有 omnibox 残留（本脚本自身的替换表除外）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
