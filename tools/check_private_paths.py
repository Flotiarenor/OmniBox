"""本地隐私门禁：把开发机配置里的真实路径 / 密钥与仓库内容比对。

为什么需要它
------------
本地运行期配置（`.config/plugins/*.json`、`.config/app.yaml`、`data/group-mesh/identity/*`
与 `.config/auth_token.txt`）**不在 git 里**（`.gitignore` 覆盖），但里面的值很容易在写
文档、注释、用例时被顺手复制进去：真机路径、共享目录、令牌。一旦推送就等于公开，而且
这类改动在其它门禁里完全看不见 —— ruff 不扫字符串，check_plugins 只看插件契约，
打包规则只查数据收集。

实测发生过一次：插件注释里写进了开发机的两个媒体根路径（未推送前发现并清掉）。本工具
把这件事变成机器判定：读**本机**配置提取"私密词条"，再扫 `git ls-files` 里的文本文件。

判定口径
--------
从本地配置里提取三类词条，再逐个在**被跟踪的**文本文件里找：

1. **绝对路径**：`D:\\图库`、`D:/音频/音乐`、`D:\\\\视频\\\\.cache` 这类写法在源码里
   转义各不相同，因此比较时统一把 `\\` 归一成 `/`、再去掉分隔符与小写化 ——
   `D:\\图库`、`D:/图库`、`D:\\\\图库` 都会命中同一条词条。路径的**后缀**（去掉盘符的
   `音频/音乐`）也作为词条，用于"只抄了后半段"的情况。回环地址、主机名这类通用值不算
   词条（它们本来就该出现在代码里）。
2. **密钥值**：JSON 里键名匹配 `token|secret|password|api_key|access_key` 且长度 ≥ 6 的
   字符串值，以及 `.config/auth_token.txt` 的整份内容；这类按**原文**精确匹配（不归一化）。
3. **用户名**：`%USERPROFILE%` 的最后一段，只在路径上下文（`users/<名字>`、`home/<名字>`）
   里才算命中，避免把常见单词当隐私。

用法
----
    venv/Scripts/python tools/check_private_paths.py            # 门禁：命中即失败
    venv/Scripts/python tools/check_private_paths.py --list     # 只看提取到哪些词条（打码）

CI 上没有本地配置，脚本打印 SKIP 并以 0 退出 —— 它是**开发机**门禁：拦的是"把你机器上
的东西提交上去"，而不是"校验远端仓库"。仓库侧还有一层保护：`.config/`、`data/`、
`logs/` 在 `.gitignore` 里，本工具会核对它们确实没被跟踪。

已知边界
--------
按**行文本**比对，因此：路径被拆成多段字符串拼接（`"D:" + "\\" + "图库"`）、
或经编码/压缩后写入的，检测不到 —— 那也不是"顺手复制粘贴"的形态。用例
`tests/test_check_private_paths.py` 把这条边界固化了。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 本地运行期配置（相对仓库根；glob）。这些文件不该被跟踪，内容才是隐私来源。
LOCAL_CONFIG_GLOBS = (
    '.config/app.yaml',
    '.config/auth_token.txt',
    '.config/principals.json',
    '.config/plugins/*.json',
    'data/group-mesh/identity/*.json',
)

#: 这些路径必须**没有**被 git 跟踪（忽略生效才谈得上"只在本地"）
MUST_BE_IGNORED = ('.config/', 'data/', 'logs/')

_SECRET_KEY_RE = re.compile(r'(?:token|secret|password|passwd|api_?key|access_?key)', re.IGNORECASE)
#: 盘符路径；`(?<![A-Za-z0-9])` 是为了不把 URL 里的 `http://…` 当成 `p:` 盘
_PATH_RE = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s"\'`,;:)\]}\r\n]*')
#: 通用值：回环地址、主机名、通配绑定 —— 它们出现在代码里是正常的，不是隐私
_GENERIC_HOST_RE = re.compile(
    r'^(?:\d{1,3}(?:\.\d{1,3}){3}|localhost|0\.0\.0\.0|::1)(?::\d+)?$', re.IGNORECASE)
#: 占位符形状的"密钥"：测试夹具与文档示例，不是真密钥
_PLACEHOLDER_SECRET_RE = re.compile(
    r'^(?:secret|token|changeme|placeholder|example|dummy|fake|test|your[-_]?|xxx|<.*>|\.\.\.)',
    re.IGNORECASE)
_TEXT_SUFFIXES = {
    '.py', '.js', '.mjs', '.cjs', '.ts', '.html', '.css', '.json', '.md', '.txt',
    '.yml', '.yaml', '.toml', '.cfg', '.ini', '.ps1', '.sh', '.spec', '.svg',
}
_MAX_FILE_BYTES = 512 * 1024
_MIN_PATH_CHARS = 4        # 盘符 + 短目录名也算（`d:图库` 正好 4 个字符）
_MIN_SUFFIX_CHARS = 6      # 后缀词条要求更长，否则"音乐/视频"这种通用词会乱报
_MIN_SECRET_CHARS = 6


def _normalize(text: str) -> str:
    """路径归一：`\\`→`/`、折叠重复分隔符、去尾分隔符、小写。"""
    text = text.replace('\\\\', '/').replace('\\', '/')
    text = re.sub(r'/{2,}', '/', text)
    return text.rstrip('/').lower()


def _compact(normalized: str) -> str:
    """再去掉分隔符：源码里的转义形式与文档里的写法因此能对上同一个词条。"""
    return normalized.replace('/', '')


def _mask(token: str) -> str:
    """打码后再打印：终端输出可能被贴进 issue / 聊天，别把词条再散一次。"""
    if len(token) <= 6:
        return token[0] + '*' * (len(token) - 1)
    return f'{token[:3]}{"*" * (len(token) - 6)}{token[-3:]}'


def _iter_local_sources(root: Path):
    for pattern in LOCAL_CONFIG_GLOBS:
        yield from sorted(root.glob(pattern))


def _is_generic(token: str) -> bool:
    """通用值（回环、主机名）与纯端口不算隐私词条。"""
    return bool(_GENERIC_HOST_RE.match(token)) or token.isdigit()


def _path_tokens(text: str) -> set:
    """从一段文本里提取路径词条（完整路径 + 后缀），返回 compact 形式。"""
    tokens = set()
    for raw in _PATH_RE.findall(text):
        normalized = _normalize(raw).strip('.,;:')
        if not normalized or len(_compact(normalized)) < _MIN_PATH_CHARS:
            continue
        if _is_generic(normalized):
            continue
        tokens.add(_compact(normalized))
        parts = normalized.split('/')
        for index in range(1, len(parts)):      # 去掉盘符后的各段后缀
            suffix = '/'.join(parts[index:])
            if len(_compact(suffix)) >= _MIN_SUFFIX_CHARS and not _is_generic(suffix):
                tokens.add(_compact(suffix))
        for index in range(2, len(parts)):      # 各级父目录：只抄了上层目录也算命中
            prefix = '/'.join(parts[:index])
            if len(_compact(prefix)) >= _MIN_PATH_CHARS:
                tokens.add(_compact(prefix))
    return tokens


def _secret_tokens(text: str) -> set:
    """JSON 里键名像密钥的值；解析不了（例如 yaml）就当纯文本看。"""
    tokens = set()
    try:
        data = json.loads(text)
    except Exception:
        return tokens
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and len(value.strip()) >= _MIN_SECRET_CHARS \
                        and _SECRET_KEY_RE.search(str(key)) \
                        and not _PLACEHOLDER_SECRET_RE.match(value.strip()):
                    tokens.add(value.strip())
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return tokens


def collect_tokens(root: Path) -> dict:
    """本机配置 → `{'path': {compact 词条}, 'secret': {原文词条}, 'user': [正则]}`。"""
    paths: set = set()
    secrets: set = set()
    for source in _iter_local_sources(root):
        try:
            text = source.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if source.name == 'auth_token.txt':
            value = text.strip()
            if len(value) >= _MIN_SECRET_CHARS:
                secrets.add(value)
            continue
        paths |= _path_tokens(text)
        secrets |= _secret_tokens(text)

    users = []
    home = os.environ.get('USERPROFILE') or os.environ.get('HOME') or ''
    name = Path(home).name if home else ''
    # 只在路径上下文里认用户名：`C:\Users\<名字>\…`、`/home/<名字>/…`
    if len(name) >= 4:
        users.append(re.compile(r'(?:users|home)[\\/]+' + re.escape(name), re.IGNORECASE))
    return {'path': paths, 'secret': secrets, 'user': users}


def _tracked_files(root: Path) -> list:
    """`git ls-files`：只扫**会被推送**的文件（忽略规则里的运行期数据不在其中）。"""
    try:
        out = subprocess.run(['git', '-C', str(root), 'ls-files'],
                             capture_output=True, text=True, encoding='utf-8',
                             errors='replace', check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [root / line for line in out.splitlines() if line.strip()]


def scan(root: Path, files, tokens: dict) -> list:
    """在给定文件里找词条；返回 `[(相对路径, 行号, 词条类型, 打码词条, 命中行)]`。"""
    hits = []
    for path in files:
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if '\0' in text[:2048]:
            continue
        rel = path.relative_to(root).as_posix() if root in path.parents else str(path)
        for number, line in enumerate(text.splitlines(), start=1):
            normalized = _normalize(line)
            compact = _compact(normalized)
            for token in tokens['path']:
                if token in compact:
                    hits.append((rel, number, '路径', _mask(token), line.strip()[:120]))
                    break
            else:
                for token in tokens['secret']:
                    if token in line:
                        hits.append((rel, number, '密钥', _mask(token), line.strip()[:120]))
                        break
                else:
                    for pattern in tokens['user']:
                        if pattern.search(normalized):
                            hits.append((rel, number, '用户名',
                                         _mask(pattern.pattern), line.strip()[:120]))
                            break
    return hits


def _check_ignored(root: Path) -> list:
    """核对运行期数据没有被跟踪 —— 本工具的前提是"它们只在本地"。"""
    problems = []
    try:
        out = subprocess.run(['git', '-C', str(root), 'ls-files',
                              *[p.rstrip('/') for p in MUST_BE_IGNORED]],
                             capture_output=True, text=True, encoding='utf-8',
                             errors='replace', check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return problems
    for line in out.splitlines():
        if line.strip():
            problems.append(f'{line.strip()} 被 git 跟踪了（应只在本地）')
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='本地隐私门禁：配置内容不得被复制进仓库')
    parser.add_argument('--root', default=str(PROJECT_ROOT), help='仓库根（默认本文件上级）')
    parser.add_argument('--list', action='store_true', help='只打印提取到的词条（打码）')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    tokens = collect_tokens(root)
    total = len(tokens['path']) + len(tokens['secret']) + len(tokens['user'])
    # SKIP 只看**路径与密钥**：用户名在任何机器上都有，拿它当"有本地配置"会让 CI
    # 永远走不到跳过分支（CI 上也有 `C:\Users\runneradmin`）。
    has_local_config = bool(tokens['path'] or tokens['secret'])
    if args.list:
        print(f'路径词条 {len(tokens["path"])} 条、密钥 {len(tokens["secret"])} 条、'
              f'用户名 {len(tokens["user"])} 条')
        for kind in ('path', 'secret'):
            for token in sorted(tokens[kind]):
                print(f'  {kind}: {_mask(token)}')
        return 0
    if not has_local_config:
        print('check_private_paths: SKIP（无本地配置，本门禁只在开发机生效）')
        return 0

    hits = scan(root, _tracked_files(root), tokens)
    problems = _check_ignored(root)
    if hits or problems:
        print(f'check_private_paths: FAIL（本地词条 {total} 条，命中 {len(hits)} 处）')
        for rel, number, kind, masked, line in hits:
            print(f'  {rel}:{number}: {kind} {masked} ← {line}')
        for problem in problems:
            print(f'  {problem}')
        print('  改法：示例一律写成 D:\\… 占位；确需真实路径时放本地配置，不要进代码。')
        return 1
    print(f'check_private_paths: OK（本地词条 {total} 条，未出现在被跟踪文件里）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
