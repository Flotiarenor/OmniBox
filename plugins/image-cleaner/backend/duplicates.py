"""完全重复分组：按尺寸分桶 → 首尾 64KB 快速指纹 → 整文件 MD5。

无状态：取消点由入口注入（同步入口传 no-op 语义的 None 任务，后台任务传
`BackgroundTask`）。**摘要不可信一律返回 None**，绝不返回"空内容的摘要"——这个插件的
下一步是 `delete_files` → unlink，一组"假重复"被一键删除就是真实照片永久丢失。
"""

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shell.backend import tasks


class DuplicateFinder:
    """完全重复图片分组。"""

    def __init__(self, check_cancel: Callable[[Any], None]) -> None:
        self._check_cancel = check_cancel

    @staticmethod
    def _file_quick_hash(abs_path: str, size: int) -> Optional[str]:
        """先读首尾各 64KB + 文件大小做快速指纹，避免整文件 MD5。

        读取失败返回 None（而不是"空内容的 md5"，见 _file_full_hash 的说明）。
        """
        h = hashlib.md5()
        h.update(str(size).encode())
        try:
            with open(abs_path, 'rb') as f:
                head = f.read(65536)
                if len(head) == 0 and size > 0:
                    # 声明有内容却读不出任何字节：文件正在被写入/被占用/权限不足
                    return None
                h.update(head)
                if size > 131072:
                    f.seek(max(0, size - 65536))
                    h.update(f.read(65536))
        except OSError:
            return None
        return h.hexdigest()

    @staticmethod
    def _file_full_hash(abs_path: str) -> Optional[str]:
        """整文件 MD5；**任何读取失败都返回 None，绝不返回"空内容的摘要"**。

        历史缺陷：失败时 `pass` 后照样返回 hashlib.md5() 的空摘要
        （d41d8cd98f00b204e9800998ecf8427e），于是所有读不到的文件共享同一个
        digest，被当成"完全重复"分组。文件在扫描期间被写入（同步/复制进行中）
        是最容易触发的场景。

        额外做一致性校验：读取过程中文件大小变化、或**实际读到的字节数与
        声明大小不符**（正在被写入、被截断），摘要都不可信，同样返回 None。
        """
        h = hashlib.md5()
        read_bytes = 0
        try:
            with open(abs_path, 'rb') as f:
                before = os.fstat(f.fileno()).st_size
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    read_bytes += len(chunk)
                    h.update(chunk)
                after = os.fstat(f.fileno()).st_size
        except OSError:
            return None
        if before != after or read_bytes != before:
            return None
        return h.hexdigest()

    def groups(self, files: List[Dict[str, Any]],
               task: Optional['tasks.BackgroundTask'] = None) -> List[dict]:
        """完全重复分组（`task` 非空时上报进度并响应取消）。

        进度以**文件**为单位：`total` = 本次扫描的文件数，`processed` = 已定论的文件数
        （尺寸唯一或快速指纹不一致的在早期阶段就定论，指纹一致的必须算完完整摘要才定论），
        因此进度单调、结束时正好走到 `total`。
        """
        by_size: Dict[int, List[Dict[str, Any]]] = {}
        for f in files:
            by_size.setdefault(f['size'], []).append(f)

        progressed = 0
        if task is not None:
            task.update(total=len(files), processed=0, current='')

        groups: List[dict] = []
        for size, batch in by_size.items():
            if len(batch) < 2:
                progressed += len(batch)
                if task is not None:
                    task.update(processed=progressed)
                continue
            by_quick: Dict[str, List[Dict[str, Any]]] = {}
            for f in batch:
                self._check_cancel(task)
                key = self._file_quick_hash(f['abs'], f['size'])
                if key is None:
                    progressed += 1  # 读不到的文件不参与比较，更不能参与删除
                    continue
                by_quick.setdefault(key, []).append(f)
            for candidates in by_quick.values():
                if len(candidates) < 2:
                    progressed += len(candidates)   # 快速指纹已足以定论
                    if task is not None:
                        task.update(processed=progressed)
                    continue
                by_full: Dict[str, List[Dict[str, Any]]] = {}
                for f in candidates:
                    self._check_cancel(task)
                    digest = self._file_full_hash(f['abs'])
                    progressed += 1
                    if task is not None:
                        task.update(processed=progressed, current=Path(f['rel']).name)
                    if digest is None:
                        continue  # 同上：摘要不可信就退出候选集
                    by_full.setdefault(digest, []).append(f)
                for digest, dups in by_full.items():
                    if len(dups) >= 2:
                        groups.append({
                            'hash': digest,
                            'size': size,
                            'files': [d['rel'] for d in dups],
                        })
        groups.sort(key=lambda g: -len(g['files']))
        return groups
