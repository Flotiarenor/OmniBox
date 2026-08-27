"""去重记录与永久跳过（404/已删除作品）的本地存取 + 已有图片扫描。

文件：
- <root>/.cache/pixiv-sync/downloaded_ids.json  已下载作品 id 集合
- <root>/.cache/pixiv-sync/failed_ids.json      404/已删除、永久跳过的 id 集合
"""

import json
import os
import re
from pathlib import Path
from typing import Set, Tuple

# 图片文件名 → 作品 id：123456.jpg / 123456_p0.jpg / 123456_p0.png ...
ID_NAME_RE = re.compile(r"^(\d+)(?:_p\d+)?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)


def load_ids(path: Path) -> Set[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("ids", []) if isinstance(data, dict) else []
        return set(int(x) for x in raw)
    except Exception:
        return set()


def save_ids(path: Path, ids: Set[int]):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ids": sorted(ids)}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def load_failed_ids(path: Path) -> Set[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("ids", []) if isinstance(data, dict) else []
        return set(int(x) for x in raw)
    except Exception:
        return set()


def save_failed_ids(path: Path, failed: Set[int]):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ids": sorted(failed)}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def collect_existing_ids(root: Path, on_image=None) -> Set[int]:
    """扫描 <root>/pixiv/ 下按规则命名的图片，提取作品 id。

    用于识别用户手动放入的旧图：文件名符合 `{id}.jpg` / `{id}_p0.jpg` 规则即可被识别，
    之后全量更新会直接跳过该作品。
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
                ids.add(iid)
                if on_image:
                    on_image(iid, Path(current) / name)
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
        for current, dir_names, filenames in os.walk(root):
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
    return existing, zero