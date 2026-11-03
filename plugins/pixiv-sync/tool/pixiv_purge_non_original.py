# -*- coding: utf-8 -*-
"""Pixiv 非原图清理脚本（pixiv-sync 伴侣工具，独立命令行程序）

背景：旧版曾可关闭「下载原图」，关闭期间下载的作品保存的是 1200px 大图
（master1200，长边被缩放到 1200px）。本地已下载记录（downloaded_ids.json）
按作品 id 去重，因此这些旧图不会被自动升级成原图。

本脚本扫描本地相册，按 Pixiv 缩放规则判断：
  - 作品任一页 max(宽,高) > 1200 → 已经是原图（master1200 最大只能 1200），保留；
  - 作品最大边 == 1200 → 这些 1200 页判定为历史缩放图，需重下原图；
  - 图片 max(宽,高) < 1200 → 原图本来就小于 1200，重下结果相同 → 保留。
对需重下作品：删除其长边正好 1200px 的文件，并从 downloaded_ids.json（去重记录）
与 failed_ids.json（失败跳过记录）中移除该作品 id，同时把 works.db 清单中这些作品的
done 重置为 0。之后直接点「同步画师 / 同步喜欢」（下载原图现为固定默认行为，
无需任何勾选），这些作品会自动重新下载原图。如需回退成 1200px 大图，
请改 backend/pixiv_sync/download.py 中 all_image_urls 的 want_original = True。
（已保留的小于 1200px 页面会因文件已存在而自动跳过，不会重复下载。）

时间窗口（可选）：误下载通常发生在最近一两个小时内，不必扫描全部相册。
指定 --hours N（或交互模式按提示填写）后，只扫描「最近 N 小时内有文件变动」
的作品；窗口外的文件只做一次快速 mtime 判断即跳过，扫描很快。

两种运行方式：
  1. 交互模式：不带参数直接运行（可双击），按提示输入下载根目录、时间范围并确认删除。
  2. 参数模式：
     --root <相册根目录>           指定根目录
     --hours <N>                  只处理最近 N 小时内下载的作品（0/留空 = 全部）
     --dry-run                    只扫描展示，不删除
     --yes                        跳过确认，直接执行
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from cli_support import is_tty, reset_done_in_db, resolve_root, strip_ids

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from PIL import Image
except ImportError:
    Image = None

# master1200 长边上限；>= 此值视为被缩放的大图（需重下原图）
THRESHOLD = 1200

# 图片文件名 → 作品 id：123456.jpg / 123456_p0.jpg / 123456p0.png
ID_NAME_RE = re.compile(r"^(\d+)(?:_?p\d+)?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)

CACHE_SUBDIR = Path(".cache") / "pixiv-sync"


def image_size(path: Path) -> Optional[Tuple[int, int]]:
    """读取图片尺寸；不可读返回 None。"""
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def scan_works(pixiv_root: Path, since: Optional[float] = None) -> Dict[int, dict]:
    """扫描 pixiv 根目录下符合命名规则的图片，按作品 id 分组。

    返回 {id: {"dir": 画师相对目录路径, "pages": [Path 图片文件]}}。
    """
    works: Dict[int, dict] = {}
    if not pixiv_root.exists():
        return works
    for current, dir_names, filenames in os.walk(pixiv_root):
        dir_names[:] = [d for d in dir_names if not d.startswith(".")]
        for name in filenames:
            m = ID_NAME_RE.match(name)
            if not m:
                continue
            iid = int(m.group(1))
            p = Path(current) / name
            if since is not None:
                try:
                    if p.stat().st_mtime < since:
                        continue  # 时间窗口外：只读一次 mtime 跳过
                except OSError:
                    continue
            rel = p.relative_to(pixiv_root)
            parent_rel = rel.parent
            entry = works.setdefault(iid, {"dir": parent_rel, "pages": []})
            entry.setdefault("dir", parent_rel)  # 首次进入时保证 dir 准确
            entry["pages"].append(p)
    return works


def classify(works: Dict[int, dict]) -> Tuple[Dict[int, dict], Dict[int, dict]]:
    """判断每个作品是否需要重下原图。

    Pixiv 的 master1200 缩略图长边**最多等于 1200**；原图可以大于 1200。
    因此：
    - 作品任一页长边 > 1200 → 说明已经是原图，全部保留；
    - 作品最大边正好等于 1200 → 这些 1200 页判定为历史缩放图，删除重下；
      小于 1200 的页保留（原图本来就这么大）。
    - 无法读取尺寸的文件不删除。
    """
    upgrade: Dict[int, dict] = {}
    keep: Dict[int, dict] = {}
    for iid, info in works.items():
        del_files: List[Path] = []
        keep_files: List[Path] = []
        max_dim = 0
        sized: List[Tuple[Path, int]] = []
        for p in info["pages"]:
            sz = image_size(p)
            if sz is None:
                keep_files.append(p)  # 无法读取尺寸的图片不要冒险删除
                continue
            dim = max(sz)
            max_dim = max(max_dim, dim)
            sized.append((p, dim))

        has_larger_than_threshold = any(dim > THRESHOLD for _, dim in sized)
        if has_larger_than_threshold:
            # 已经是原图（master1200 不可能超过 1200）。
            keep_files.extend(p for p, _ in sized)
        else:
            for p, dim in sized:
                if dim == THRESHOLD:
                    del_files.append(p)
                else:
                    keep_files.append(p)

        entry = {
            "dir": info["dir"],
            "delete": del_files,
            "keep": keep_files,
            "total": len(info["pages"]),
            "max": max_dim,
        }
        if del_files:
            upgrade[iid] = entry
        else:
            keep[iid] = entry
    return upgrade, keep

def prune_empty_dirs(pixiv_root: Path):
    """自底向上清理 pixiv 根目录下的空目录（多图子文件夹/整画师目录）。

    只处理"我们确实会创建"的目录：跳过隐藏目录（.cache 等由其它模块管理，
    删掉会破坏缓存/状态），避免把用户自己放在 pixiv/ 下的非本工具目录一并清空。
    """
    for dirpath, _dir_names, _filenames in os.walk(pixiv_root, topdown=False):
        d = Path(dirpath)
        if d.name.startswith('.'):
            continue
        if d.resolve() == pixiv_root.resolve():
            continue
        try:
            d.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pixiv 非原图清理：扫描并删除 1200px 大图、清除去重记录以便重下原图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  交互:      python pixiv_purge_non_original.py\n"
        "  参数:      python pixiv_purge_non_original.py --root D:\\data\n"
        "  预览:      python pixiv_purge_non_original.py --root D:\\data --dry-run\n"
        "  直接执行:  python pixiv_purge_non_original.py --root D:\\data --yes\n",
    )
    parser.add_argument("--root", help="下载根目录（含 pixiv/ 与 .cache/pixiv-sync/）")
    parser.add_argument("--hours", type=int, default=None,
                        help="只处理最近 N 小时内下载的文件（0 或留空 = 全部）")
    parser.add_argument("--dry-run", action="store_true", help="只扫描展示，不删除")
    parser.add_argument("--yes", action="store_true", help="跳过确认，直接执行")
    args = parser.parse_args()

    print("=" * 60)
    print("Pixiv 非原图清理工具")
    print("判断规则: 作品任一页长边 > %dpx = 已是原图，保留" % THRESHOLD)
    print("          作品最大边 == %dpx = 历史 1200px 缩放图，重下" % THRESHOLD)
    print("          图片长边 <  %dpx = 原图本来就小，保留" % THRESHOLD)
    print("=" * 60)

    root = resolve_root(args.root)
    cache = root / CACHE_SUBDIR
    pixiv_root = root / "pixiv"

    # 时间窗口：只处理最近 N 小时内下载的文件（不用扫描全部相册）
    hours = args.hours
    if hours is None and is_tty():
        ans = input("仅处理最近 N 小时内下载的作品？(留空 = 全部；例如 1 或 2): ").strip()
        if ans:
            try:
                hours = max(0, int(ans))
            except ValueError:
                hours = None  # 非法输入 = 全部
    since = None
    scope_text = "全部（不限时间）"
    if hours:
        since = time.time() - hours * 3600
        scope_text = f"最近 {hours} 小时内"
    print(f"扫描范围: {scope_text}")
    if since:
        print("  （时间窗口外的文件只读一次 mtime 即跳过，不打开图片，扫描很快）")

    if Image is None:
        print("! 未检测到 Pillow，无法读取图片尺寸。请先安装：pip install pillow")
        print("  未做任何修改。")
        return 1


    works = scan_works(pixiv_root, since=since)
    if not works:
        print(f"! {pixiv_root} 下没有按规则命名的图片（{ID_NAME_RE.pattern}）")
        print("  未做任何修改。")
        return 1
    upgrade, keep = classify(works)

    print()
    print(f"扫描完成: 共 {len(works)} 个作品 / 需重下原图 {len(upgrade)} 个 / 无需处理 {len(keep)} 个")
    if upgrade:
        print("-" * 60)
        print("%-10s %-40s %6s %6s %5s" % ("作品ID", "画师目录", "总页数", "待删页", "最大边"))
        print("-" * 60)
        for iid in sorted(upgrade):
            e = upgrade[iid]
            print("%-10d %-40s %6d %6d %5d" % (iid, str(e["dir"]), e["total"], len(e["delete"]), e["max"]))
        if keep:
            print(f"（另有 {len(keep)} 个作品无需处理：已是原图，或所有页面均小于 {THRESHOLD}px）")

    total_del = sum(len(e["delete"]) for e in upgrade.values())
    print("-" * 60)
    print(f"待删除大图文件: {total_del} 个 / 涉及作品: {len(upgrade)} 个")

    if args.dry_run:
        print()
        print("[dry-run] 仅展示，未删除任何文件。可去掉 --dry-run 实际执行。")
        return 0

    if not upgrade:
        print("没有需要重下的作品，无需操作。")
        return 0

    if not args.yes:
        ids_sample = "、".join(str(i) for i in sorted(upgrade)[:5])
        more = f" 等 {len(upgrade)} 个" if len(upgrade) > 5 else ""
        print(f"将删除: {ids_sample}{more}")
        print("同时从去重记录 downloaded_ids.json 与失败跳过 failed_ids.json 移除这些作品 id")
        print("提示: 删除不可恢复，请确认。")
        ans = input("确认执行删除与记录清除？[y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消，未做任何修改。")
            return 1

    # ---------- 执行 ----------
    removed_ids: Set[int] = set()
    deleted_files = 0
    for iid in sorted(upgrade):
        e = upgrade[iid]
        for p in e["delete"]:
            try:
                p.unlink()
                deleted_files += 1
            except OSError as err:
                print(f"  ! 删除失败 {p}: {err}")
        removed_ids.add(iid)
    prune_empty_dirs(pixiv_root)

    ids_removed = strip_ids(cache / "downloaded_ids.json", removed_ids)
    failed_removed = strip_ids(cache / "failed_ids.json", removed_ids)
    done_reset = reset_done_in_db(cache / "works.db", removed_ids)

    print()
    print("执行完成:")
    print(f"  删除大图文件: {deleted_files} 个")
    print(f"  去重记录移除: {ids_removed} 条")
    print(f"  失败跳过移除: {failed_removed} 条")
    print(f"  清单 done 重置: {done_reset} 条")
    if ids_removed < len(removed_ids):
        print(f"  (!!) downloaded_ids.json 中仅移除 {ids_removed}/{len(removed_ids)} 条，"
              "其余可能没有对应记录或文件写入失败")
    print()
    print("下一步: 「下载原图」现为固定默认行为（默认开启、无开关）。直接点「同步画师」或「同步喜欢」即可；")
    print("        这些作品会自动重新下载原图（已保留的 <1200px 页面会自动跳过）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())