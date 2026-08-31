'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

"""通用 SQLite 缩略图缓存（媒体插件共享基建·数据面）。

从 image-viewer 的散文件缩略图实现泛化而来：任何「本地媒体 → 缩略图」的
插件（图片相册、视频封面抽帧、音频内嵌封面等）都可复用：

- 每个插件持有自己的 `ThumbCache` 实例，DB 文件放在各自数据根目录
  `.cache/thumbs.db`（数据跟数据走，换数据目录自动重建）；
- 键为相对路径，`source_mtime`（0.5s 容差）+ `source_size` 双条件失效校验，
  源文件替换后自动重生成；
- 单图按需生成（/thumbs 路由），批量并行生成（全量重建/同步任务）；
- `clear()` 先 `wal_checkpoint(TRUNCATE)` 再 `VACUUM`，并加进程级锁，
  避免与按需生成的连接交错导致收缩静默失败；
- 生成失败返回 None 且不写缓存（不缓存假缩略图）。

典型用法：
    cache = ThumbCache(root / '.cache' / 'thumbs.db', size=(300, 300))
    data, mime = cache.get('a/b.png', root / 'a/b.png')     # 按需生成
    cache.generate_bulk([('a/b.png', root/'a/b.png'), ...],  # 批量重建
                        progress_cb=..., stop_event=...)
    cache.delete('a/b.png')                                  # 文件删除/移动时
    cache.clear()                                            # 全量清空 + 收缩
"""

import io
import os
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_MIME_MAP = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
    '.webp': 'image/webp',
}

# 保护「清空+收缩」段的进程级锁：VACUUM 需要独占数据库。
_CLEAR_LOCK = threading.Lock()


class ThumbCache:
    """SQLite 缩略图缓存（WAL + mtime/size 失效校验 + 并行批量生成）。"""

    def __init__(self, db_path: Path, size: Tuple[int, int] = (300, 300),
                 mime_map: Optional[Dict[str, str]] = None,
                 workers: Optional[int] = None) -> None:
        self.db_path = Path(db_path)
        self.size = tuple(size)
        self.mime_map = dict(mime_map or DEFAULT_MIME_MAP)
        self.workers = workers or min(8, max(1, os.cpu_count() or 4))
        self._mtime_tolerance = 0.5

    # ===== 内部 =====

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS thumbs (
                path         TEXT PRIMARY KEY,
                source_mtime REAL NOT NULL DEFAULT 0,
                source_size  INTEGER NOT NULL DEFAULT 0,
                mime         TEXT NOT NULL DEFAULT 'image/jpeg',
                data         BLOB NOT NULL,
                created_at   REAL NOT NULL DEFAULT 0
            )
        ''')
        return conn

    def _mime_for(self, rel_path: str) -> str:
        return self.mime_map.get(Path(rel_path).suffix.lower(), 'application/octet-stream')

    def _generate(self, src_path: Path) -> Optional[Tuple[bytes, str]]:
        """生成 size 缩略图字节；失败返回 None（不复制原图当假缩略图）。"""
        try:
            from PIL import Image
            with Image.open(src_path) as img:
                img.thumbnail(self.size)
                fmt = (img.format or 'JPEG').upper()
                if fmt == 'JPG':
                    fmt = 'JPEG'
                if fmt not in ('JPEG', 'PNG', 'WEBP', 'GIF', 'BMP'):
                    fmt = 'JPEG'
                if fmt == 'JPEG' and img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                out = io.BytesIO()
                if fmt == 'JPEG':
                    img.save(out, format='JPEG', quality=85, optimize=True)
                elif fmt == 'WEBP':
                    img.save(out, format='WEBP', quality=82, method=4)
                else:
                    img.save(out, format=fmt, optimize=True)
                data = out.getvalue()
                mime = self.mime_map.get('.' + fmt.lower(), 'image/jpeg')
                return data, mime
        except Exception:
            return None

    def _read_valid(self, conn: sqlite3.Connection, rel_path: str,
                    source_mtime: float, source_size: int) -> Optional[Tuple[bytes, str]]:
        row = conn.execute(
            'SELECT data, mime, source_mtime, source_size FROM thumbs WHERE path = ?',
            (rel_path,),
        ).fetchone()
        if row and abs(float(row[2]) - float(source_mtime)) < self._mtime_tolerance \
                and int(row[3]) == int(source_size):
            return row[0], row[1]
        return None

    def _write(self, conn: sqlite3.Connection, rel_path: str,
               data: bytes, mime: str, source_mtime: float, source_size: int) -> None:
        conn.execute(
            'INSERT OR REPLACE INTO thumbs(path, source_mtime, source_size, mime, data, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (rel_path, source_mtime, source_size, mime, data, time.time()),
        )

    # ===== 单图：按需生成（/thumbs 路由用） =====

    def get(self, rel_path: str, src_path: Path) -> Optional[Tuple[bytes, str]]:
        """从缓存读取有效缩略图；未命中时生成并写回。返回 (data, mime) 或 None。"""
        try:
            st = Path(src_path).stat()
        except OSError:
            return None
        try:
            conn = self._connect()
            try:
                cached = self._read_valid(conn, rel_path, st.st_mtime, st.st_size)
                if cached:
                    return cached
                generated = self._generate(Path(src_path))
                if generated is None:
                    return None
                data, mime = generated
                self._write(conn, rel_path, data, mime, st.st_mtime, st.st_size)
                conn.commit()
                return data, mime
            finally:
                conn.close()
        except Exception:
            return None

    # ===== 批量：并行生成（全量重建 / 同步任务用） =====

    def generate_bulk(self, items: List[Tuple[str, Path]],
                      progress_cb: Optional[Callable] = None,
                      stop_event: Optional[threading.Event] = None) -> Dict[str, Any]:
        """批量生成缩略图到 SQLite。

        items: [(rel_path, src_path), ...]；多线程生成（默认按 CPU 核数），
        主线程单连接写入；progress_cb(processed, total, current, errors)。
        返回 {'processed': n, 'total': n, 'errors': [...]}。
        """
        items = list(items)
        total = len(items)
        processed = 0
        errors: List[str] = []
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()

        def commit_progress(rel):
            nonlocal processed
            if processed % 50 == 0:
                conn.commit()
            if progress_cb:
                progress_cb(processed, total, rel, errors)

        try:
            # 预检：跳过已有有效缩略图
            pending = []
            for rel, src in items:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    st = Path(src).stat()
                except OSError:
                    errors.append(rel)
                    processed += 1
                    commit_progress(rel)
                    continue
                row = self._read_valid(conn, rel, st.st_mtime, st.st_size)
                if row:
                    processed += 1
                    commit_progress(rel)
                    continue
                pending.append((rel, str(src), st.st_mtime, st.st_size))

            # 并行生成（worker 只做 Pillow 解码，无 DB 访问）
            executor = ThreadPoolExecutor(max_workers=self.workers)
            futures: Dict[Any, Tuple[str, float, int]] = {}
            pending_iter = iter(pending)

            def submit_next():
                try:
                    rel, src_str, mtime, size = next(pending_iter)
                except StopIteration:
                    return None
                fut = executor.submit(self._generate_one, src_str)
                futures[fut] = (rel, mtime, size)
                return fut

            try:
                for _ in range(min(self.workers * 2, len(pending))):
                    if submit_next() is None:
                        break

                while futures:
                    if stop_event is not None and stop_event.is_set():
                        for fut in list(futures):
                            fut.cancel()
                        break
                    done, _ = wait(list(futures), timeout=0.2, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for fut in done:
                        rel, mtime, size = futures.pop(fut)
                        try:
                            result = fut.result()
                        except Exception as e:
                            errors.append(f'{rel}: {e}')
                            processed += 1
                            commit_progress(rel)
                            submit_next()
                            continue
                        if result is None:
                            errors.append(rel)
                        else:
                            data, mime = result
                            self._write(conn, rel, data, mime, mtime, size)
                        processed += 1
                        commit_progress(rel)
                        submit_next()
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            conn.commit()
        finally:
            conn.close()
        return {'processed': processed, 'total': total, 'errors': errors}

    def _generate_one(self, src_str: str):
        """供线程池调用的模块内 worker：返回 (data, mime) 或 None。"""
        return self._generate(Path(src_str))

    # ===== 维护 =====

    def delete(self, rel_path: str) -> None:
        """删除某条缓存（源文件删除/移动/重新生成时调用）。"""
        try:
            conn = self._connect()
            try:
                conn.execute('DELETE FROM thumbs WHERE path = ?', (rel_path,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def clear(self) -> None:
        """清空全部缓存并收缩 DB 文件（先 checkpoint 合并 WAL 再 VACUUM）。"""
        try:
            conn = self._connect()
            try:
                with _CLEAR_LOCK:
                    conn.execute('DELETE FROM thumbs')
                    conn.commit()
                    try:
                        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    except Exception:
                        pass
                    conn.execute('VACUUM')
            finally:
                conn.close()
        except Exception:
            pass

    def close(self) -> None:
        """无长连接（每次操作短连接），保留接口便于对称管理。"""
        pass
