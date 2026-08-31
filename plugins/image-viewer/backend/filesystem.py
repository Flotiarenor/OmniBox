"""图片浏览器的文件系统与图片处理工具。"""

import hashlib
import io
import os
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.media_catalog import (
    is_safe_path,  # noqa: F401
    list_directory as _catalog_list_directory,
)

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}

_NUM_RE = re.compile(r'(\d+)')


def natural_sort_key(text: Any) -> Any:
    """自然排序键：p0 < p1 < p2 < ... < p10。优先使用 venv 的 natsort，缺失时回退内置实现。"""
    try:
        from natsort import natsort_keygen
        return natsort_keygen()(str(text))
    except Exception:
        return [int(part) if part.isdigit() else part.lower()
                for part in _NUM_RE.split(str(text))]


def pixiv_number(name: Any) -> int | None:
    """提取名称前导数字（Pixiv 作品 ID / 图片编号）；无前导数字返回 None。"""
    m = _NUM_RE.match(str(name))
    return int(m.group(1)) if m else None


def drop_image_meta(meta_cache: dict, abs_path: str):
    """删除某张图片的尺寸元数据缓存（文件被替换/重新生成缩略图时调用）。"""
    key = hashlib.md5(abs_path.encode()).hexdigest()
    meta_cache.pop(key, None)


def stat_mtime(root: Path, rel_path: str) -> float:
    try:
        return os.stat(root / rel_path).st_mtime
    except Exception:
        return 0.0


def get_image_size(abs_path: str, mtime: float, meta_cache: dict) -> tuple:
    """从元数据缓存读取图片尺寸，未命中时用 Pillow 读取并回写缓存。

    读取失败时不写缓存：临时不可读（文件被占用/写入中）的文件不会被
    永久缓存成 0×0，下次扫描会重试。
    """
    key = hashlib.md5(abs_path.encode()).hexdigest()
    if key in meta_cache:
        data = meta_cache[key]
        if data.get('mtime') == mtime:
            return data.get('width', 0), data.get('height', 0)
    try:
        from PIL import Image
        with Image.open(abs_path) as img:
            width, height = img.size
    except Exception:
        return 0, 0
    meta_cache[key] = {'mtime': mtime, 'width': width, 'height': height}
    return width, height


_THUMB_SIZE = (300, 300)

# 保护「清空+收缩」段的进程级锁：VACUUM 需要独占数据库，
# 避免与 /thumbs 按需生成的连接交错导致静默失败。
_THUMB_DB_LOCK = threading.Lock()

_MIME_BY_EXT = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
    '.webp': 'image/webp',
}


def _connect_thumb_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15)
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


def _mime_for_rel_path(rel_path: str) -> str:
    return _MIME_BY_EXT.get(Path(rel_path).suffix.lower(), 'application/octet-stream')


def generate_thumb_bytes(src_path: Path) -> Optional[Tuple[bytes, str]]:
    """生成 300px 缩略图字节；失败时返回 None，不再把原图复制成“假缩略图”。"""
    try:
        from PIL import Image
        with Image.open(src_path) as img:
            img.thumbnail(_THUMB_SIZE)
            fmt = (img.format or 'JPEG').upper()
            if fmt in ('JPG',):
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
            mime = _MIME_BY_EXT.get('.' + fmt.lower(), 'image/jpeg')
            if fmt == 'JPG':
                mime = 'image/jpeg'
            return data, mime
    except Exception:
        return None


def get_thumb_from_cache(db_path: Path, rel_path: str,
                         source_mtime: float, source_size: int) -> Optional[Tuple[bytes, str]]:
    """从 SQLite 读取有效缩略图；源文件 mtime/size 变化时视为失效。"""
    try:
        conn = _connect_thumb_db(db_path)
        try:
            row = conn.execute(
                'SELECT data, mime, source_mtime, source_size FROM thumbs WHERE path = ?',
                (rel_path,),
            ).fetchone()
            if row and abs(float(row[2]) - float(source_mtime)) < 0.5 and int(row[3]) == int(source_size):
                return row[0], row[1]
        finally:
            conn.close()
    except Exception:
        pass
    return None


def put_thumb_to_cache(db_path: Path, rel_path: str, data: bytes, mime: str,
                       source_mtime: float, source_size: int):
    try:
        conn = _connect_thumb_db(db_path)
        try:
            conn.execute(
                'INSERT OR REPLACE INTO thumbs(path, source_mtime, source_size, mime, data, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (rel_path, source_mtime, source_size, mime, data, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def delete_thumb_cache(db_path: Path, rel_path: str):
    try:
        conn = _connect_thumb_db(db_path)
        try:
            conn.execute('DELETE FROM thumbs WHERE path = ?', (rel_path,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def clear_thumb_cache(db_path: Path):
    """清空 SQLite 缩略图表，用于全量重建；同时收缩数据库文件，避免删除后体积不变。

    先 checkpoint 把 WAL 合并回主库再 VACUUM，提高收缩成功率；
    整个「清空+收缩」段加锁，避免与按需生成的连接交错。
    """
    try:
        conn = _connect_thumb_db(db_path)
        try:
            with _THUMB_DB_LOCK:
                conn.execute('DELETE FROM thumbs')
                conn.commit()
                # 让 thumbs.db 文件真正变小，而不是只删除行。
                try:
                    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                except Exception:
                    pass
                conn.execute('VACUUM')
        finally:
            conn.close()
    except Exception:
        pass


def ensure_thumbnail_bytes(root: Path, rel_path: str, thumb_db_path: Path) -> Optional[Tuple[bytes, str]]:
    """从 SQLite 取缩略图；未命中时生成并写回。"""
    src_path = root / rel_path
    try:
        st = src_path.stat()
    except OSError:
        return None
    cached = get_thumb_from_cache(thumb_db_path, rel_path, st.st_mtime, st.st_size)
    if cached:
        return cached
    generated = generate_thumb_bytes(src_path)
    if generated is None:
        return None
    data, mime = generated
    put_thumb_to_cache(thumb_db_path, rel_path, data, mime, st.st_mtime, st.st_size)
    return data, mime


def _generate_one_thumb(payload):
    """供并行线程池调用的模块级 worker。"""
    root_str, rel = payload
    src_path = Path(root_str) / rel
    try:
        st = src_path.stat()
    except OSError:
        return {'rel': rel, 'data': None, 'mime': None, 'mtime': 0.0, 'size': 0, 'error': 'stat failed'}
    generated = generate_thumb_bytes(src_path)
    if generated is None:
        return {'rel': rel, 'data': None, 'mime': None, 'mtime': st.st_mtime, 'size': st.st_size, 'error': 'generate failed'}
    data, mime = generated
    return {'rel': rel, 'data': data, 'mime': mime, 'mtime': st.st_mtime, 'size': st.st_size, 'error': None}


def generate_thumbs_bulk(root: Path, rel_paths, db_path: Path,
                         progress_callback=None, stop_event=None,
                         workers: int | None = None) -> Dict[str, Any]:
    """批量生成缩略图到 SQLite。

    默认使用多线程并行生成；workers<=1 时退化为顺序处理。
    progress_callback(processed, total, current, errors) 用于上报进度。
    """
    rel_paths = list(rel_paths)
    total = len(rel_paths)
    processed = 0
    errors: List[str] = []
    if workers is None:
        workers = min(8, max(1, os.cpu_count() or 4))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_thumb_db(db_path)

    def commit_progress(rel):
        nonlocal processed
        if processed % 50 == 0:
            conn.commit()
        if progress_callback:
            progress_callback(processed, total, rel, errors)

    try:
        if workers <= 1:
            for rel in rel_paths:
                if stop_event is not None and stop_event.is_set():
                    break
                src_path = root / rel
                try:
                    st = src_path.stat()
                except OSError:
                    errors.append(rel)
                    processed += 1
                    commit_progress(rel)
                    continue

                row = conn.execute(
                    'SELECT data, mime, source_mtime, source_size FROM thumbs WHERE path = ?',
                    (rel,),
                ).fetchone()
                if row and abs(float(row[2]) - float(st.st_mtime)) < 0.5 and int(row[3]) == int(st.st_size):
                    processed += 1
                    commit_progress(rel)
                    continue

                generated = generate_thumb_bytes(src_path)
                if generated is None:
                    errors.append(rel)
                else:
                    data, mime = generated
                    conn.execute(
                        'INSERT OR REPLACE INTO thumbs(path, source_mtime, source_size, mime, data, created_at) '
                        'VALUES (?, ?, ?, ?, ?, ?)',
                        (rel, st.st_mtime, st.st_size, mime, data, time.time()),
                    )
                processed += 1
                commit_progress(rel)
            conn.commit()
            return {'processed': processed, 'total': total, 'errors': errors}

        # 多线程并行：先统计并跳过已有有效缩略图，再提交给线程池。
        pending = []
        for rel in rel_paths:
            if stop_event is not None and stop_event.is_set():
                break
            src_path = root / rel
            try:
                st = src_path.stat()
            except OSError:
                errors.append(rel)
                processed += 1
                commit_progress(rel)
                continue
            row = conn.execute(
                'SELECT data, mime, source_mtime, source_size FROM thumbs WHERE path = ?',
                (rel,),
            ).fetchone()
            if row and abs(float(row[2]) - float(st.st_mtime)) < 0.5 and int(row[3]) == int(st.st_size):
                processed += 1
                commit_progress(rel)
                continue
            pending.append((str(root), rel, st.st_mtime, st.st_size))

        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {}
        pending_iter = iter(pending)

        def submit_next():
            try:
                root_str, rel, mtime, size = next(pending_iter)
            except StopIteration:
                return None
            fut = executor.submit(_generate_one_thumb, (root_str, rel))
            futures[fut] = (rel, mtime, size)
            return fut

        try:
            for _ in range(min(workers * 2, len(pending))):
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
                    if result.get('error'):
                        errors.append(rel)
                    else:
                        conn.execute(
                            'INSERT OR REPLACE INTO thumbs(path, source_mtime, source_size, mime, data, created_at) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            (rel, mtime, size, result['mime'], result['data'], time.time()),
                        )
                    processed += 1
                    commit_progress(rel)
                    submit_next()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        conn.commit()
    finally:
        conn.close()
    return {'processed': processed, 'total': total, 'errors': errors}


def ensure_thumbnail(root: Path, rel_path: str, thumb_dir: Path) -> Path:
    """旧版文件式缩略图入口，保留给其他兼容代码使用；新代码请用 ensure_thumbnail_bytes。"""
    thumb_path = thumb_dir / rel_path
    if thumb_path.exists():
        return thumb_path
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        img = Image.open(root / rel_path)
        img.thumbnail(_THUMB_SIZE)
        img.save(thumb_path)
    except Exception:
        try:
            shutil.copy(root / rel_path, thumb_path)
        except Exception:
            pass
    return thumb_path


def list_directory(root: Path, rel_path: str) -> List[Dict]:
    return _catalog_list_directory(root, rel_path, allowed_extensions=ALLOWED_EXTENSIONS, include_files=False)['dirs']
