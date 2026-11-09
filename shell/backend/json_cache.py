"""插件共享的 JSON 落盘基建：原子写 + 带版本号与指纹的缓存读取。

为什么需要（真实问题，不是洁癖）
--------------------------------
插件后端把缩略图元数据、相册索引、目录快照、下载状态一类缓存写成 JSON，各写各的，
于是同一件事在仓库里有 20 多处实现，口径却不一致：

* **原子性不一致**：一部分用「写临时文件 + `os.replace`」，另一部分直接
  `open(path, 'w')`。后者在写到一半时进程被杀（宿主退出、测试清理临时目录）会留下
  半截 JSON，下次启动读它抛 `JSONDecodeError`，缓存从此永久损坏 —— 而缓存损坏的
  表现是"打开很慢"，没人会把它和一次强杀联系起来。
* **失效判据不一致**：缓存文件都靠 dict 里几个隐含字段判断"这份还能不能用"
  （版本号、根目录集合、目录 mtime）。判据散落在各插件的读取函数里，改一处漏一处，
  后果是"换了数据根之后仍然读到旧库的索引"这类错误结果，而不是报错。
* **父目录语义不一致**：有的实现会顺手 `mkdir(parents=True)`，于是"缓存目录已经被
  删掉"的场景被静默重新创建出来（Windows 上 rmtree 会因此报 `WinError 145`）。

所以这里只收两件事：**怎么写才不坏**（原子）与**怎么判才可信**（版本 + 指纹）。
业务形状（有哪些键、键里放什么）仍然由插件自己决定。

契约
----
* 所有写操作都原子：先写同目录下的临时文件、`flush` + `fsync`，再 `os.replace`。
* 所有写操作**默认不创建父目录**：父目录不存在表示"这份缓存的目标位置已经不在了"
  （换过根、进程正在退出、测试在清理），此时返回 False 让调用方自己决定是否忽略。
  需要创建目录的场景（首次运行）显式传 `create_parent=True`，把意图写在调用点上。
* 读操作不抛异常：文件缺失、内容不是 JSON、不是 dict 一律当作"没有缓存"。
  这些情况的正确处理都是同一条 —— 重新算一遍。
* 版本化缓存把 `version` 与指纹字段**写进同一份 JSON**，读取时逐项核对；
  不匹配就整份作废（返回 None），而不是尝试修补。

用法::

    from shell.backend.json_cache import read_cached_json, write_cached_json

    payload = read_cached_json(path, version=4, fingerprint={'roots': roots})
    if payload is None:
        payload = rebuild()
        write_cached_json(path, payload, version=4, fingerprint={'roots': roots})
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

#: 版本化缓存里记录 schema 版本的键名。与业务键分开，避免插件自己用到同名字段。
CACHE_VERSION_KEY = 'version'


def read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON 文件；缺失或无法解析时返回 `default`。

    不抛异常是刻意设计：这些文件全部是缓存，读不出来的正确处理就是重新生成。
    把 `JSONDecodeError` 抛给调用方只会让每个插件各写一遍 `try/except`。
    """
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(
    path: Path,
    payload: Any,
    *,
    indent: Optional[int] = 2,
    create_parent: bool = False,
    fsync: bool = True,
) -> bool:
    """原子写入 JSON 文件；成功返回 True。

    原子性来自「同目录临时文件 + `os.replace`」：`os.replace` 在 Windows 与 POSIX 上
    都是原子替换，读者要么看到旧文件、要么看到完整的新文件，不会看到半截内容。

    父目录不存在时**默认失败**（返回 False）而不是创建它，理由见模块 docstring：
    父目录消失通常意味着目标位置已经不该再写，静默重建会把刚删掉的目录又建出来。

    `fsync=False` 只给"随时可以重算、且调用频繁"的缓存用：省掉一次刷盘，代价是
    断电可能丢掉最后一次写入。
    """
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        return False

    tmp_name: Optional[str] = None
    try:
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
        with os.fdopen(handle_fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except (OSError, TypeError, ValueError):
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False
    return True


def read_cached_json(
    path: Path,
    *,
    version: int,
    fingerprint: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    """读取版本化缓存；版本或指纹不匹配、文件不可用时返回 None。

    `fingerprint` 是"这份缓存描述的是哪份输入"的标识，例如根目录集合、数据根路径。
    只比版本号不够：同一版本下换过数据根，旧缓存依然通过版本检查，结果是把旧库的
    索引用在新的库上 —— 表现是内容错乱而不是报错，比缓存缺失难查得多。

    指纹的值必须能 JSON 往返（字符串、数字、布尔、列表、字典）。浮点时间戳建议由
    调用方先归一（例如 `round(mtime, 3)` 或 `repr(...)`）：这里用精确相等比较，
    精度取舍属于调用方的业务判断，放进基建只会造成"有时命中有时不命中"的怪现象。
    """
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return None
    if data.get(CACHE_VERSION_KEY) != version:
        return None
    if fingerprint is not None:
        for key, expected in fingerprint.items():
            if data.get(key) != expected:
                return None
    return data


def write_cached_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    version: int,
    fingerprint: Optional[Mapping[str, Any]] = None,
    indent: Optional[int] = None,
    create_parent: bool = False,
    fsync: bool = True,
) -> bool:
    """写入版本化缓存：把 `version` 与指纹字段并进 `payload` 后原子落盘。

    默认不缩进：这类文件只由机器读，省下的体积对"上万条目录索引"是实打实的。
    """
    document: dict = dict(payload)
    document[CACHE_VERSION_KEY] = version
    if fingerprint is not None:
        document.update(fingerprint)
    return write_json(
        path,
        document,
        indent=indent,
        create_parent=create_parent,
        fsync=fsync,
    )
