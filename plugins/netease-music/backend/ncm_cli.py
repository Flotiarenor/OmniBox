"""ncm-cli 的命令定位与执行。

为什么单独成组件：这一段处理的全是"外部命令怎么起"——Windows 上 npm 全局包只有
`.cmd` / `.ps1` shim，shim 会把参数交给 cmd.exe 二次解析，所以优先定位包内的 node 入口
直接执行。它与音乐数据解析、播放地址缓存没有关系，判断标准只有一条：命令注入面或平台
行为变了，改这里。

`NcmRunner.run` **绝不使用 shell=True**：keyword 直接来自前端搜索框，一个引号就能逃出
引号让 `&` 变成命令分隔符。
"""

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

# ncm-cli 是 npm 全局包，Windows 上只有 .cmd/.ps1 shim。shim 会把参数交给
# cmd.exe 二次解析，因此优先定位包内的 node 入口直连执行（不经 cmd.exe）。
_NCM_PACKAGE_ENTRY = ('node_modules', '@music163', 'ncm-cli', 'dist', 'index.js')
_SHELL_META_CHARS = '"&|<>^%!\r\n'


def ncm_argv_prefix() -> List[str]:
    """返回 ncm-cli 的命令前缀（形如 [node, index.js, '--output', 'json']）。"""
    exe = shutil.which('ncm-cli')
    node = shutil.which('node')
    if exe and node:
        entry = Path(exe).resolve().parent
        for part in _NCM_PACKAGE_ENTRY:
            entry = entry / part
        if entry.is_file():
            return [node, str(entry), '--output', 'json']
    # 退化：直接用 shim（调用方会对参数做元字符拒绝）
    return [exe or 'ncm-cli', '--output', 'json']


def reject_shell_meta(arg: str) -> str:
    """拒绝会被 cmd.exe 当作语法解释的字符（仅在必须经 shim 时使用）。"""
    if any(ch in _SHELL_META_CHARS for ch in arg):
        raise ValueError(f"参数包含 cmd 元字符，已拒绝执行: {arg!r}")
    return arg


class NcmRunner:
    """执行 ncm-cli 命令（参数数组，不经 shell）。"""

    def run(self, cmd, timeout: int = 30, env: dict | None = None) -> Dict[str, Any]:
        """执行 ncm-cli 命令。

        cmd 支持两种形式：
          - 常量命令字符串（内部用 shlex.split 拆分，如 "--version"）；
          - list[str] 参数数组 —— **含用户输入时必须用这种形式**。
        """
        args = shlex.split(cmd) if isinstance(cmd, str) else [str(item) for item in cmd]
        argv = ncm_argv_prefix()
        if argv[0].lower().endswith(('.cmd', '.bat', '.ps1')):
            # 退化路径：没能定位到 node 入口，参数要经过 cmd.exe 二次解析，
            # 因此拒绝一切 cmd 元字符（宁可不执行，也不给注入留缝）。
            args = [reject_shell_meta(item) for item in args]
        try:
            result = subprocess.run(
                argv + args,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                env=env,
                check=False,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": "命令执行超时", "returncode": -1}
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": str(e), "returncode": -1}
