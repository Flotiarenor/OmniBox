"""全库扫描：按宿主的每个根目录（含虚拟前缀）递归收集图片。

无状态：扩展名在构造时确定，返回的是"当前磁盘事实"；缓存由结果层
（`ScanResults`）与指纹层（`DHashStore`）各自负责，失效判据不同，不能混在一起。

**同一个物理文件只收一条**：额外根可以落在主根之内（宿主的 `_extra_roots()` 只排除完全
相同的路径，不排除嵌套），那样一个文件会有两条虚拟路径（`备份/a.jpg` 与
`__备份 (2)/a.jpg`）。不收手的话它们会被判成"两张完全重复的图片"——"只留一张"之后的删除
会对同一个文件 unlink 两次（第二次报"部分删除失败"），"全选组"删除更会把唯一副本一起删掉。
保留先出现的条目，即主根的虚拟路径。
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Set


class AlbumScanner:
    """递归收集全部相册里的图片（跳过隐藏目录与 `.cache`）。"""

    def __init__(self, host: Any, extensions: Any) -> None:
        self._host = host
        self._extensions = extensions

    def all_files(self) -> List[Dict[str, Any]]:
        """每个根的相对路径都带上宿主的虚拟前缀（额外图库要被扫到）。

        图片相册把额外图库当相册展示，清理必须扫到它，否则额外图库里的重复、以及
        "主目录一份 + 额外图库一份"这种跨根重复永远找不出来。
        """
        files: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for root, prefix in self._host.roots():
            try:
                if not root.exists() or not root.is_dir():
                    continue
                for current, dir_names, filenames in os.walk(root):
                    dir_names[:] = [d for d in dir_names
                                    if not d.startswith('.') and d != '.cache']
                    current_path = Path(current)
                    try:
                        rel_dir = current_path.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    for name in filenames:
                        if name.startswith('.'):
                            continue
                        if Path(name).suffix.lower() not in self._extensions:
                            continue
                        abs_path = current_path / name
                        # 去重键：规范化大小写与 . / .. 之后的绝对路径。两个根在宿主侧
                        # 都已 resolve()（root_dir 与 _extra_roots），嵌套时两种走法得到
                        # 同一字符串，因此这里不需要再碰一次文件系统。
                        key = os.path.normcase(os.path.abspath(str(abs_path)))
                        if key in seen:
                            continue
                        try:
                            stat = abs_path.stat()
                        except OSError:
                            continue
                        seen.add(key)
                        rel_in_root = (Path(rel_dir) / name).as_posix() if rel_dir else name
                        files.append({
                            'rel': f'{prefix}/{rel_in_root}' if prefix else rel_in_root,
                            'abs': str(abs_path),
                            'size': stat.st_size,
                            'mtime': stat.st_mtime,
                        })
            except OSError:
                continue
        return files
