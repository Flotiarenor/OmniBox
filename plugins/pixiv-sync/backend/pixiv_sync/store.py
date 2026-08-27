"""去重记录与永久跳过（404/已删除作品）的本地存取 + 已有图片扫描。

文件：
- <root>/.cache/pixiv-sync/downloaded_ids.json  已下载作品 id 集合
- <root>/.cache/pixiv-sync/failed_ids.json      404/已删除、永久跳过的 id 集合
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, List, Set, Tuple

# 图片文件名 → 作品 id：123456.jpg / 123456_p0.jpg / 123456p0.png ...
ID_NAME_RE = re.compile(r"^(\d+)(?:_?p\d+)?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)


def _raw_ids(data: Any) -> List[Any]:
    """兼容两种历史格式：{"ids": [...]} 与纯数组 [...]。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("ids") or []
    return []


def _parse_ids(data: Any) -> Set[int]:
    ids: Set[int] = set()
    for x in _raw_ids(data):
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            continue
    return ids


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再原子替换，避免任务中断把 JSON 写坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_ids(path: Path) -> Set[int]:
    try:
        return _parse_ids(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_ids(path: Path, ids: Set[int]) -> bool:
    try:
        _atomic_write_json(path, {"ids": sorted(ids)})
        return True
    except Exception as e:
        print(f"[pixiv-sync] 保存 downloaded_ids.json 失败: {e}")
        return False


def load_failed_ids(path: Path) -> Set[int]:
    try:
        return _parse_ids(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_failed_ids(path: Path, failed: Set[int]) -> bool:
    try:
        _atomic_write_json(path, {"ids": sorted(failed)})
        return True
    except Exception as e:
        print(f"[pixiv-sync] 保存 failed_ids.json 失败: {e}")
        return False


def collect_existing_ids(root: Path, on_image=None) -> Set[int]:
    """扫描 <root>/pixiv/ 下按规则命名的图片，提取作品 id。

    用于识别用户手动放入的旧图：文件名符合 `{id}.jpg` / `{id}_p0.jpg` 规则即可被识别，
    之后全量更新会直接跳过该作品。0 字节文件不视为有效图片。
    """
    ids: Set[int] = set()
    if not root.exists():
        return ids
    try:
        for current, dir_names, filenames in os.walk(root):
            dir_names[:] = [d for d in dir_names if not d.startswith(".")]
            for name in filenames:
                m = ID_NAME_RE.match(name)
                if not m:
                    continue
                iid = int(m.group(1))
                p = Path(current) / name
                try:
                    if p.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                ids.add(iid)
                if on_image:
                    on_image(iid, p)
    except OSError:
        pass
    return ids


def rebuild_existing(root: Path) -> Tuple[Set[int], int]:
    """扫描本地重建有效文件 id 集合（同时删除 0 字节文件）。

    返回 (有效 id 集合, 清理的 0 字节文件数)。
    """
    existing: Set[int] = set()
    zero = 0
    if root.exists():
        try:
            walker = os.walk(root)
            for current, dir_names, filenames in walker:
                dir_names[:] = [d for d in dir_names if not d.startswith(".")]
                for name in filenames:
                    m = ID_NAME_RE.match(name)
                    if not m:
                        continue
                    iid = int(m.group(1))
                    p = Path(current) / name
                    try:
                        if p.stat().st_size == 0:
                            p.unlink()
                            zero += 1
                            continue
                    except OSError:
                        continue
                    existing.add(iid)
        except OSError:
            pass
    return existing, zero