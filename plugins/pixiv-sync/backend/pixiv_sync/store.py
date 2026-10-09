"""去重记录与永久跳过（404/已删除作品）的本地存取 + 已有图片扫描。

文件：
- <root>/.cache/pixiv-sync/downloaded_ids.json  已下载作品 id 集合
- <root>/.cache/pixiv-sync/failed_ids.json      404/已删除、永久跳过的 id 集合
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

log = logging.getLogger(__name__)

# 图片文件名 → 作品 id + 页号：123456.jpg / 123456_p0.jpg / 123456p0.png ...
# 第 2 组是页号（无 _p 后缀时不存在）；单图与动图 WebP 都算第 0 页。
ID_NAME_RE = re.compile(r"^(\d+)(?:_?p(\d+))?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)


def expected_pages(work_type: Any, page_count: Any) -> int:
    """清单里一个作品应有的本地文件页数。

    ugoira（动图）转成单个动画 WebP 保存，固定按 1 页算；其余取 page_count，
    缺失/非法时按 1 页兜底。注意 page_count 是扫描快照，画师后续加页不会反映。
    """
    if str(work_type or "").lower() == "ugoira":
        return 1
    try:
        return max(1, int(page_count))
    except (TypeError, ValueError):
        return 1


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
        log.error(f"[pixiv-sync] 保存 downloaded_ids.json 失败: {e}")
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
        log.error(f"[pixiv-sync] 保存 failed_ids.json 失败: {e}")
        return False


def scan_local(
    root: Path, remove_zero: bool = False
) -> Tuple[Set[int], int, Dict[int, Set[int]]]:
    """扫描 <root>/pixiv/ 下按规则命名的图片，一次遍历同时得到 id 集合与每个作品的页号集合。

    返回 (有效 id 集合, 删除的 0 字节文件数, {作品 id: {页号}})。页号：`{id}_p3.jpg`
    记为 3，`{id}.jpg` / `{id}.webp`（单图、动图 WebP）记为 0。0 字节残片一律不算有效图片；
    remove_zero=True 时顺手删除（刷新记录/校验内容用），False 时只跳过不删。

    「本地页数 < 清单 page_count」是半截下载的判据：按命名规则提取的 id 无法区分
    「整件下完」和「只下到一半」，页号集合才能区分。
    """
    ids: Set[int] = set()
    pages: Dict[int, Set[int]] = {}
    zero = 0
    if not root.exists():
        return ids, zero, pages
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
                    size = p.stat().st_size
                except OSError:
                    continue
                if size <= 0:
                    if remove_zero:
                        try:
                            p.unlink()
                            zero += 1
                        except OSError:
                            pass
                    continue
                ids.add(iid)
                pages.setdefault(iid, set()).add(int(m.group(2)) if m.group(2) else 0)
    except OSError:
        pass
    return ids, zero, pages
