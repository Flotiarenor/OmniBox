"""文档引用门禁：AGENTS.md 里点到的名字、路径与链接必须真的存在。

为什么需要
----------
AGENTS.md 体系的价值全在"指向哪里"：根文件列目录、模块文件列关键符号、模块文件
再指向更细的文档。而引用会腐烂 —— 删掉一个函数、给一份文档改名、把目录挪个位置，
引用仍然指着旧名字，读的人（和人以外的 agent）比完全没有文档时更糊涂。此前没有
任何东西会为此报警：编译器不读 Markdown，单测不读 AGENTS.md。

两类文档，两套判据
------------------
`plugins/<name>/AGENTS.md` 按 `docs/plugin-doc-spec.md` 描述的是**界面契约**：
CSS 类名、DOM 的 `data-*` 属性、浏览器全局对象（`window.Icons`…）、运行时数据文件
（`identity/state.json`、`.cache/*`），以及相对于插件前端根的路径（`js/app.js`）。
这些都不是仓库里的符号，用"引用即存在"去卡它们会得到大量误报 —— 本门禁第一次
接上仓库时共报 418 条，其中 415 条来自这 8 份插件文档，没有一条是真腐烂。

因此判据按文档类型分开：

1. 所有 AGENTS.md：Markdown 相对链接必须存在；围栏代码块里出现的路径必须存在；
   声明的额外作用域（EXTRA_SCOPE）必须存在。
2. 除插件界面文档之外的 AGENTS.md（根、`shell/`、`tools/`、`tests/`、`docs/Releases/`、
   `plugins/AGENTS.md`）：反引号里的名字、路径与短横线目录名必须存在。

插件侧的同名契约由 `tools/check_plugins.py` 在代码里校验（manifest 字段、前端资源、
freshness 契约、UI 契约），不需要文档来兜。

刻意不做
--------
不要求"新增的函数必须写进 AGENTS.md"。那会把"地图"变成"清单"：清单会膨胀到没人
维护，然后被整段删掉。门禁只保证"已经写下的引用仍然成立" —— 这是腐烂的反面，也是
这份文档敢说可信的前提。

作用域
------
默认是文档所在目录（递归）加上插件界面文档规则；只有当文档确实引用了目录之外的
东西时，才在 EXTRA_SCOPE 里补一行 —— 每补一行都要写清"谁在那边"，否则这里会变成
逃生舱。

用法：
    python tools/check_docs.py             # 扫全仓 AGENTS.md
    python tools/check_docs.py --root DIR  # 扫指定根（用例里构造正反例用）

退出码 0 = 引用全部命中，1 = 有引用指向了已不存在的东西。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_NAME = 'AGENTS.md'

for _stream in (sys.stdout, sys.stderr):
    # 与 check_version.py 同一处理：Windows 控制台默认不是 UTF-8，直接 print 中文会乱码。
    _reconfigure = getattr(_stream, 'reconfigure', None)
    if callable(_reconfigure):
        _reconfigure(encoding='utf-8', errors='replace')

# 名字必须落在这些后缀的文件里才算数。刻意不含 .md：否则另一份文档里的旧名字会替
# 被删掉的符号背书，"引用即存在"就退化成"某处出现过"。
CODE_SUFFIXES = frozenset({'.py', '.js', '.mjs', '.cjs', '.ts', '.vue', '.json', '.css', '.sh', '.ps1'})
# 路径引用允许的后缀比上面宽：文档也会引用 .md / .toml / .yml / .spec。
PATH_SUFFIXES = CODE_SUFFIXES | {'.md', '.toml', '.yml', '.yaml', '.txt', '.svg', '.spec', '.html'}

# 产物与运行期文件：本来就可能不在仓库里（`OmniBox.zip` 在 .gitignore 中，
# `*.db` 是运行期生成的），一律不当作"引用"。
ARTIFACT_SUFFIXES = frozenset({'.zip', '.7z', '.db', '.log', '.png', '.jpg', '.webp', '.mp4', '.ico'})

# 不进索引的目录：生成物、依赖、运行期数据、本机缓存。
SKIP_DIRS = frozenset({
    '.git', '.build', '.build-linux', '.build-work', '.build-dist', '.ruff_cache',
    '.dsh', 'venv', 'node_modules', 'dist', 'chromedriver', '__pycache__',
})

# 生成物 / 运行期目录：文档引用它们是说明"产物落在哪"，不是"仓库里有这个文件"。
GENERATED_PREFIXES = (
    'dist/', 'node_modules/', 'venv/', '.build', '.config/', 'data/', 'logs/',
    'chromedriver/', '__pycache__/',
    # 前端构建产物：`shell/frontend/dist/` 只在 `npm run build` 之后存在，而 lint 与 docs
    # 两个 job 不构建前端 —— 文档引用它说的是"首页从哪来"，不是"仓库里有这个文件"。
    'shell/frontend/dist/',
)
# 精确到某一份产物（不按前缀匹配）：`OmniBox/` 是 PyInstaller 的输出树，
# `docs/Releases/AGENTS.md` 的产物表按该目录内的相对路径写它，真跑过构建才有。
GENERATED_NAMES = frozenset({'__pycache__', 'se-metadata.json', 'OmniBox/'})

# 插件自己的文档：按 docs/plugin-doc-spec.md 描述界面契约，不做名字/路径检查。
PLUGIN_INTERFACE_DOC = re.compile(r'^plugins/[^/]+/AGENTS\.md$')

# 文档 → 额外的作用域根（相对仓库根）；默认作用域是文档所在目录。
EXTRA_SCOPE: dict[str, tuple[str, ...]] = {
    # HIDDEN_IMPORTS 与 spec_common.py 在打包目录里，插件后端规则要引用它们
    'shell/backend/AGENTS.md': ('docs/Releases',),
    # serve_shell_assets / serve_res_assets 是壳后端提供的静态资源解析顺序
    'shell/frontend/AGENTS.md': ('shell/backend',),
    # RUNTIME_FIELD_READERS 是插件 manifest 字段的读取方登记处
    'plugins/AGENTS.md': ('tools/check_plugins.py',),
}

# 不是本仓库的符号，而是外部工具 / 外部约定的名字。每加一个都要写清它属于谁 ——
# 这里是全仓唯一允许"跳过名字检查"的地方，不要拿它当逃生舱。
EXTERNAL_NAMES = frozenset({
    'git_commit',   # DSH 的提交工具，不在本仓库
    'main',         # 分支名（根文件「收尾」一节的分支约束）
    'develop',      # 同上
})

_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')
# 形如 document-reader 的短横线名字：插件与示例目录都是这个形状
_SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)+')
# x.y.z 这类占位符（不是标识符，也不是路径）
_PLACEHOLDER = re.compile(r'[a-z](?:\.[a-z])+')


class Index(NamedTuple):
    """仓库里的路径索引：路径引用靠它兜住"不带斜杠的名字"。"""

    rel_paths: set[str]
    by_name: dict[str, list[str]]
    dir_names: set[str]


class ScanResult(NamedTuple):
    errors: list[str]
    docs: int
    refs: int


def _walk(root: Path):
    """按目录剪枝遍历：venv / node_modules 不进索引，也不拖慢扫描。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        yield Path(dirpath), dirnames, filenames


def iter_docs(root: Path) -> list[Path]:
    docs: list[Path] = []
    for dirpath, _dirnames, filenames in _walk(root):
        if DOC_NAME in filenames:
            docs.append(dirpath / DOC_NAME)
    return sorted(docs)


def build_index(root: Path) -> Index:
    rel_paths: set[str] = set()
    by_name: dict[str, list[str]] = {}
    dir_names: set[str] = set()
    for dirpath, dirnames, filenames in _walk(root):
        dir_names.update(dirnames)
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            rel_paths.add(rel)
            by_name.setdefault(name, []).append(rel)
    return Index(rel_paths, by_name, dir_names)


def _is_generated(token: str) -> bool:
    return token in GENERATED_NAMES or token.startswith(GENERATED_PREFIXES)


def _path_like(token: str) -> bool:
    if not token or token.startswith(('-', '#', '@', '~', '/')):
        # 前导斜杠是运行时 URL（`/shell/base.js`、`/plugins/<name>/...`），不是仓库路径
        return False
    if any(ch in token for ch in ' \t<>(){}[]*"\'|,;:'):
        return False
    if token.endswith('/'):
        return True
    return Path(token).suffix in PATH_SUFFIXES


def _label(path: Path, root: Path) -> str:
    """消息里一律用仓库相对路径：绝对路径既长又与本机绑定，读的人用不上。"""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
    return '.' if rel == '.' else rel


def _scope_roots(doc_rel: str, root: Path) -> list[Path]:
    roots = [(root / doc_rel).parent]
    for extra in EXTRA_SCOPE.get(doc_rel, ()):
        roots.append(root / extra)
    unique: list[Path] = []
    for scope in roots:
        if scope not in unique:
            unique.append(scope)
    return unique


def _code_files(scope: Path) -> list[Path]:
    if scope.is_file():
        return [scope] if scope.suffix in CODE_SUFFIXES else []
    files: list[Path] = []
    for dirpath, _dirnames, filenames in _walk(scope):
        for name in filenames:
            if Path(name).suffix in CODE_SUFFIXES:
                files.append(Path(dirpath) / name)
    return files


def _scope_text(doc_rel: str, root: Path, cache: dict[str, tuple[str, int]]) -> tuple[str, int]:
    """返回（作用域内所有代码的正文, 文件数）；同一个文档只读一次。"""
    if doc_rel in cache:
        return cache[doc_rel]
    chunks: list[str] = []
    count = 0
    for scope in _scope_roots(doc_rel, root):
        for path in _code_files(scope):
            try:
                chunks.append(path.read_text(encoding='utf-8', errors='replace'))
            except OSError:
                continue
            count += 1
    cache[doc_rel] = ('\n'.join(chunks), count)
    return cache[doc_rel]


def _split_fences(text: str) -> tuple[str, str]:
    """把围栏代码块与散文分开：围栏里是"照抄去执行"的内容（带参数与占位符），
    只查其中的路径，不查名字。"""
    prose: list[str] = []
    fenced: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            inside = not inside
            continue
        (fenced if inside else prose).append(line)
    return '\n'.join(prose), '\n'.join(fenced)


def _check_name(token: str, text: str, files: int, scope: str, root: Path, index: Index) -> str | None:
    if token in EXTERNAL_NAMES or _PLACEHOLDER.fullmatch(token):
        return None
    if Path(token).suffix in ARTIFACT_SUFFIXES:
        return None
    if re.search(r'\b' + re.escape(token) + r'\b', text):
        return None
    if '.' in token:
        # 点分形态再试一次模块路径：tests.test_foo → tests/test_foo.py
        dotted = token.replace('.', '/')
        for candidate in (f'{dotted}.py', f'{dotted}.mjs', f'{dotted}/__init__.py', dotted):
            if candidate in index.rel_paths or (root / candidate).exists():
                return None
    return (f'引用的名字 `{token}` 在作用域里找不到（作用域 {scope}，{files} 个代码文件）；'
            f'改了名字就同步文档，删了就把这行删掉')


def _check_path(token: str, doc_dir: Path, root: Path, index: Index, where: str) -> str | None:
    if _is_generated(token) or Path(token).suffix in ARTIFACT_SUFFIXES:
        return None
    clean = token.rstrip('/')
    for base in (doc_dir, root):
        if (base / clean).exists():
            return None
    if '/' not in clean:
        if clean in index.by_name or clean in index.dir_names:
            return None
    elif clean in index.rel_paths:
        return None
    return f'{where} `{token}` 不存在（找过文档同级与仓库根）；挪了位置就同步文档'


def _check_slug(token: str, doc_dir: Path, root: Path, index: Index) -> str | None:
    if (doc_dir / token).exists() or (root / token).exists() or token in index.dir_names:
        return None
    return f'引用的目录名 `{token}` 不存在（找过文档同级、仓库根与全仓目录名）'


def _check_link(target: str, doc_dir: Path, root: Path) -> str | None:
    if target.startswith(('http://', 'https://', 'mailto:', '#')) or '<' in target or '>' in target:
        return None
    clean = target.split('#', 1)[0].split('?', 1)[0].strip()
    if not clean or _is_generated(clean):
        return None
    if (doc_dir / clean).exists() or (root / clean).exists():
        return None
    return f'Markdown 链接 `{target}` 指向的文件不存在'


def _fenced_paths(fenced: str) -> set[str]:
    """围栏块里只挑出路径形状的词：命令的参数、占位符一律不碰。"""
    tokens: set[str] = set()
    for raw in fenced.split():
        token = raw.strip('`"\'()[];,')
        if '=' in token and not token.startswith('-'):
            token = token.split('=', 1)[1]
        if _path_like(token):
            tokens.add(token)
    return tokens


def scan(root: Path) -> ScanResult:
    root = root.resolve()
    docs = iter_docs(root)
    index = build_index(root)
    cache: dict[str, tuple[str, int]] = {}
    errors: list[str] = []
    refs = 0

    for doc in docs:
        doc_rel = doc.relative_to(root).as_posix()
        doc_dir = doc.parent
        prose, fenced = _split_fences(doc.read_text(encoding='utf-8', errors='replace'))
        scopes = _scope_roots(doc_rel, root)
        scope_label = ' + '.join(_label(scope, root) for scope in scopes)
        for scope in scopes:
            if not scope.exists():
                errors.append(f'{doc_rel}: 作用域 `{_label(scope, root)}` 已不存在（EXTRA_SCOPE 该改了）')

        if PLUGIN_INTERFACE_DOC.match(doc_rel):
            # 界面契约文档：只查围栏里的路径与链接，散文里的反引号不是仓库符号
            for token in sorted(_fenced_paths(fenced)):
                refs += 1
                problem = _check_path(token, doc_dir, root, index, '围栏命令里的路径')
                if problem:
                    errors.append(f'{doc_rel}: {problem}')
            for target in sorted(set(_LINK.findall(prose))):
                refs += 1
                problem = _check_link(target, doc_dir, root)
                if problem:
                    errors.append(f'{doc_rel}: {problem}')
            continue

        text, files = _scope_text(doc_rel, root, cache)
        for token in sorted(set(_INLINE_CODE.findall(prose))):
            refs += 1
            if _path_like(token):
                problem = _check_path(token, doc_dir, root, index, '引用的路径')
            elif _SLUG.fullmatch(token):
                problem = _check_slug(token, doc_dir, root, index)
            elif _IDENT.fullmatch(token):
                problem = _check_name(token, text, files, scope_label, root, index)
            else:
                problem = None
            if problem:
                errors.append(f'{doc_rel}: {problem}')

        for token in sorted(_fenced_paths(fenced)):
            refs += 1
            problem = _check_path(token, doc_dir, root, index, '围栏命令里的路径')
            if problem:
                errors.append(f'{doc_rel}: {problem}')

        for target in sorted(set(_LINK.findall(prose))):
            refs += 1
            problem = _check_link(target, doc_dir, root)
            if problem:
                errors.append(f'{doc_rel}: {problem}')

    return ScanResult(errors, len(docs), refs)


def check_docs(root: Path) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    return scan(root).errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='校验 AGENTS.md 里引用的名字、路径与链接都还存在')
    parser.add_argument('--root', type=Path, default=PROJECT_ROOT,
                        help='仓库根（用例里构造临时仓库用）')
    args = parser.parse_args(argv)

    result = scan(args.root)
    if result.errors:
        print(f'check_docs: {len(result.errors)} 个错误', file=sys.stderr)
        for error in result.errors:
            print(f'  - {error}', file=sys.stderr)
        return 1
    print(f'check_docs: OK（{result.docs} 份 {DOC_NAME}、{result.refs} 处引用全部命中）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
