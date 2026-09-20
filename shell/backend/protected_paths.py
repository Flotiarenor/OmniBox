r'''Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

受保护路径的**唯一判定入口**（Shell 凭据 + 插件申报），读与写共用同一份实现。

为什么必须是一个共享模块，而不是留在 file_server 里的私有函数：
    - 读侧：/file、/files、/thumbs、插件前端路由由 Shell 拦；
    - 写侧：delete_folder / delete_files / move_files 这类**不可逆**操作是插件
      自己实现的（/api/<插件>__<方法>），Shell 拦不到 —— 插件必须能问到同一份
      清单（PluginBase.is_protected_path）。
两处只要有一处自己拼规则，"读挡得住、删挡不住"就会出现。

归一化（本模块存在的第二个理由）：Windows 上 Path(r'\\?\D:\x').resolve()
**保留** `\\?\` 前缀，于是 `\\?\D:\…\.config\auth_token.txt` 与
`D:\…\.config\auth_token.txt` 既不相等、也不 is_relative_to —— 纯词法比较会被
一次前缀变换绕过（实测：同一文件用扩展形式取回 200 + 令牌明文，普通形式 403）。
所以比较前统一剥离 `\\?\` / `\\?\UNC\` 前缀，并用 os.path.samefile 兜住
"路径不同、文件同一个"的形态（硬链接）。
'''

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import List, Optional

from shell.backend.auth import get_token_file
from shell.backend.paths import get_config_dir
from shell.backend.principal import principals_file

log = logging.getLogger(__name__)

# Windows 扩展长度前缀：\\?\C:\x 与 \\?\UNC\server\share 两种形态都要还原
_EXTENDED_PREFIX = '\\\\?\\'
_EXTENDED_UNC_PREFIX = '\\\\?\\UNC\\'


def strip_extended_prefix(text: str) -> str:
    r"""把 `\\?\` / `\\?\UNC\` 前缀还原成普通形式（其余原样返回）。"""
    if text.startswith(_EXTENDED_UNC_PREFIX):
        return '\\\\' + text[len(_EXTENDED_UNC_PREFIX):]
    if text.startswith(_EXTENDED_PREFIX):
        return text[len(_EXTENDED_PREFIX):]
    return text


def normalize(path) -> Path:
    """受保护判定与根包含判定共用的路径归一：先剥扩展前缀，再 resolve()。

    不抛异常：resolve() 在极端路径上会 OSError（超长路径、畸形 UNC），
    此时退回未解析的形态 —— 调用方的包含判定会因此不成立（偏保守）。
    """
    raw = strip_extended_prefix(str(path))
    candidate = Path(raw)
    try:
        return candidate.resolve()
    except (OSError, ValueError):
        return candidate


def matches(target, protected: Iterable[Path]) -> bool:
    """target 是否等于某个受保护路径、落在其下，或是它的**上级目录**。

    两侧都先归一（调用方已经归一过也不会出错，resolve 幂等）。
    除词法比较外再用 os.path.samefile 做一次"同一个文件"判定：硬链接的两条
    路径彼此不相等、也不互相包含，只有文件标识能认出它们。

    反向包含（`item` 在 `target` 之内）也判 True：删除/移动这类操作以**目录**为
    单位（image-viewer 的 delete_folder 走 shutil.rmtree），只判"target 在受保护
    路径之内"会漏掉"受保护文件在 target 之内"这一形态 —— 把插件根设成包含
    `<config>` 的目录再删该目录，auth_token.txt 与 principals.json 会被连带删除
    （实测通过；tests/test_protected_paths.py 锁住这条）。文件不可能是另一个路径的
    上级目录，因此该分支只会命中目录（或不存在/待创建的路径），不会误伤正常读取。
    """
    try:
        normalized_target = normalize(target)
    except (TypeError, ValueError):
        return False
    for item in protected:
        if normalized_target == item or normalized_target.is_relative_to(item):
            return True
        if item.is_relative_to(normalized_target):
            return True
        try:
            if item.is_file() and normalized_target.is_file() \
                    and os.path.samefile(normalized_target, item):
                return True
        except OSError:
            continue
    return False


def collect(plugin_manager=None, config_dir: Optional[Path] = None) -> List[Path]:
    """当前的受保护路径清单：壳自己的凭据（硬编码）+ 插件申报的路径。

    每次都现算，不做缓存：插件支持运行期卸载与重新加载，快照会让新加载插件
    申报的路径静默失效 —— 静默失效的防护比没有防护更危险。

    插件侧任何异常都只记 warning 并跳过：一个写坏的申报不该让文件路由整体 500。
    """
    paths: List[Path] = []
    try:
        paths.append(normalize(get_token_file(config_dir or get_config_dir())))
    except (OSError, TypeError, ValueError) as exc:
        log.warning(f'[ProtectedPaths] 解析访问令牌路径失败，壳凭据防护未生效: {exc}')

    # 主体凭据表（`<config>/principals.json`）：里面是各主体令牌的 SHA-256。
    # 虽然摘要不能直接当令牌用，但它是"哪些主体存在、各自什么角色"的完整清单，
    # 而离线爆破短令牌的入口正是它 —— 与 auth_token.txt 同级保护，不单独放行。
    try:
        paths.append(normalize(principals_file(config_dir or get_config_dir())))
    except (OSError, TypeError, ValueError) as exc:
        log.warning(f'[ProtectedPaths] 解析主体凭据表路径失败，该文件防护未生效: {exc}')

    if plugin_manager is not None:
        try:
            declared = plugin_manager.get_protected_paths()
        except Exception as exc:
            log.warning(f'[ProtectedPaths] 读取插件申报的受保护路径失败: {exc}')
            declared = []
        for raw in declared or []:
            try:
                protected = normalize(raw)
            except (OSError, TypeError, ValueError):
                log.warning(f'[ProtectedPaths] 忽略无法解析的受保护路径申报: {raw!r}')
                continue
            if protected not in paths:
                paths.append(protected)
    return paths


def is_protected(target, plugin_manager=None, config_dir: Optional[Path] = None) -> bool:
    """便捷入口：现算清单并判定 target（插件写/删前的自查走这里）。"""
    return matches(target, collect(plugin_manager, config_dir))
