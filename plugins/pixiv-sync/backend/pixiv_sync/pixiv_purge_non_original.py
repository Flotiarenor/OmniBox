# -*- coding: utf-8 -*-
"""Pixiv 非原图清理脚本（pixiv-sync 伴侣工具，独立命令行程序）

背景：旧版曾可关闭「下载原图」，关闭期间下载的作品保存的是 1200px 大图
（master1200，长边被缩放到 1200px）。本地已下载记录（downloaded_ids.json）
按作品 id 去重，因此这些旧图不会被自动升级成原图。

本脚本扫描本地相册，按 Pixiv 缩放规则判断：
  - 图片 max(宽,高) >= 1200  → 顶到 1200 上限，必然是被缩放的大图，
    原图一定更大 → 判定「需重下原图」；
  - 图片 max(宽,高) < 1200   → 原图本来就小于 1200，重下结果相同 → 不处理。
对需重下作品：删除其 >=1200px 的文件，并从 downloaded_ids.json（去重记录）
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
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from PIL import Image
except ImportError:
    Image = None

# master1200 长边上限；>= 此值视为被缩放的大图（需重下原图）
THRESHOLD = 1200

# 图片文件名 → 作品 id：123456.jpg / 123456_p0.jpg / 123456_p0.png
ID_NAME_RE = re.compile(r"^(\d+)(?:_p\d+)?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)

CACHE_SUBDIR = Path(".cache") / "pixiv-sync"


def _e(s: str) -> str:
    return s


def image_size(path: Path) -> Optional[Tuple[int, int]]:
    """读取图片尺寸；不可读返回 None。"""
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


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
    """按页判断每个作品是否需要重下原图。

    返回 (upgrade, keep)：
    - upgrade: {id: {"dir", "delete": [Path], "keep": [Path], "total": n, "max": dim}}
    - keep:    {id: ...} 仅用于输出统计
    """
    upgrade: Dict[int, dict] = {}
    keep: Dict[int, dict] = {}
    for iid, info in works.items():
        del_files: List[Path] = []
        keep_files: List[Path] = []
        max_dim = 0
        for p in info["pages"]:
            sz = image_size(p)
            if sz is None:
                continue
            dim = max(sz)
            max_dim = max(max_dim, dim)
            if dim >= THRESHOLD:
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


def strip_ids(path: Path, ids: Set[int]) -> int:
    """从 json 记录文件中移除作品 id，保留其他字段；返回移除条数。"""
    if not path.exists() or not ids:
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, list):  # 兼容纯数组格式
        data = {"ids": data}
    if not isinstance(data, dict):
        return 0
    cur = data.get("ids") or []
    keep = [x for x in cur if int(x) not in ids]
    removed = len(cur) - len(keep)
    if removed:
        data["ids"] = keep
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"  ! 写入 {path.name} 失败: {e}")
            return 0
    return removed


def reset_done_in_db(cache_dir: Path, ids: Set[int]) -> int:
    """把 works.db 清单里这些作品的 done 标记重置为 0。

    下载清单（扫描记录）与去重记录是分开的两套数据：仅删除本地文件 + 移除去重 id
    还不够——清单里的 done 仍是 1，直接点「同步」会被 not done 过滤跳过。
    重置后无需先刷新名单，直接同步即可按清单重新下载原图。
    """
    db_path = cache_dir / "works.db"
    if not db_path.exists() or not ids:
        return 0
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE works SET done=0 WHERE id IN ({placeholders})",
                tuple(sorted(ids)),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception as e:
        print(f"  ! 重置 works.db done 标记失败: {e}")
        return 0


def prune_empty_dirs(pixiv_root: Path):
    """自底向上清理 pixiv 根目录下的空目录（多图子文件夹/整画师目录）。"""
    for dirpath, dirnames, filenames in os.walk(pixiv_root, topdown=False):
        d = Path(dirpath)
        if d.resolve() == pixiv_root.resolve():
            continue
        try:
            d.rmdir()
        except OSError:
            pass


def resolve_root(root_arg: Optional[str]) -> Path:
    """确定下载根目录：--root > 自动探测 > 交互输入。"""
    if root_arg:
        p = Path(root_arg).resolve()
        if not (p / "pixiv").exists():
            print(f"! 目录 {p} 下没有 pixiv/ 相册子目录（确认是否填对了下载根目录）？")
            print(f"  仍将继续（找不到图片时会输出 0 条）。")
        return p

    # 自动探测
    cwd = Path.cwd()
    candidates = []
    # 1) 当前目录有 .config/app.yaml → 按 data_root
    app_yaml = cwd / ".config" / "app.yaml"
    if app_yaml.exists():
        try:
            root = _data_root_from_yaml(app_yaml, cwd)
            if root:
                candidates.append(("app.yaml data_root", root))
        except Exception:
            pass
    # 2) cwd/data
    candidates.append(("cwd/data", cwd / "data"))
    for label, p in candidates:
        if (p / "pixiv").exists():
            print(f"自动找到相册根目录: {p}（{label}）")
            return p.resolve()

    # 交互输入
    print("-" * 60)
    default = str(cwd / "data")
    ans = input(
        f"未找到相册根目录，请输入下载根目录\n"
        f"（目录下应包含 pixiv/ 与 .cache/pixiv-sync/；留空使用 {default}）: "
    ).strip()
    p = Path(ans or default).resolve()
    return p


def _data_root_from_yaml(yaml_path: Path, base: Path) -> Optional[Path]:
    import re as _re

    body = yaml_path.read_text(encoding="utf-8")
    m = _re.search(r"^\s*data_root\s*:\s*(.+)$", body, _re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip().strip("'\"")
    p = Path(val)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


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
    print("判断规则: 图片长边 >= %dpx = 被缩放的大图(1200px)，原图更大 → 重下" % THRESHOLD)
    print("          图片长边 <  %dpx = 原图本来就小，重下结果相同 → 不处理" % THRESHOLD)
    print("=" * 60)

    root = resolve_root(args.root)
    cache = root / CACHE_SUBDIR
    pixiv_root = root / "pixiv"

    # 时间窗口：只处理最近 N 小时内下载的文件（不用扫描全部相册）
    hours = args.hours
    if hours is None and _is_tty():
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

    works = scan_works(pixiv_root, since=since)
    if not works:
        print(f"! {pixiv_root} 下没有按规则命名的图片（{ID_NAME_RE.pattern}）")
        print("  未做任何修改。")
        return 1
    upgrade, keep = classify(works)

    print("")
    print(f"扫描完成: 共 {len(works)} 个作品 / 需重下原图 {len(upgrade)} 个 / 无需处理 {len(keep)} 个")
    if upgrade:
        print("-" * 60)
        print("%-10s %-40s %6s %6s %5s" % ("作品ID", "画师目录", "总页数", "待删页", "最大边"))
        print("-" * 60)
        for iid in sorted(upgrade):
            e = upgrade[iid]
            print("%-10d %-40s %6d %6d %5d" % (iid, str(e["dir"]), e["total"], len(e["delete"]), e["max"]))
        if keep:
            print(f"（另有 {len(keep)} 个作品无需处理：所有页面均小于 {THRESHOLD}px，重下结果相同）")

    total_del = sum(len(e["delete"]) for e in upgrade.values())
    print("-" * 60)
    print(f"待删除大图文件: {total_del} 个 / 涉及作品: {len(upgrade)} 个")

    if args.dry_run:
        print("")
        print("[dry-run] 仅展示，未删除任何文件。可去掉 --dry-run 实际执行。")
        return 0

    if not upgrade:
        print("没有需要重下的作品，无需操作。")
        return 0

    if not args.yes:
        ids_sample = "、".join(str(i) for i in sorted(upgrade)[:5])
        more = f" 等 {len(upgrade)} 个" if len(upgrade) > 5 else ""
        print(f"将删除: {ids_sample}{more}")
        print(f"同时从去重记录 downloaded_ids.json 与失败跳过 failed_ids.json 移除这些作品 id")
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
    done_reset = reset_done_in_db(cache, removed_ids)

    print("")
    print(f"执行完成:")
    print(f"  删除大图文件: {deleted_files} 个")
    print(f"  去重记录移除: {ids_removed} 条")
    print(f"  失败跳过移除: {failed_removed} 条")
    print(f"  清单 done 重置: {done_reset} 条")
    if ids_removed < len(removed_ids):
        print(f"  (!!) downloaded_ids.json 中仅移除 {ids_removed}/{len(removed_ids)} 条，"
              "其余可能没有对应记录或文件写入失败")
    print("")
    print("下一步: 「下载原图」现为固定默认行为（默认开启、无开关）。直接点「同步画师」或「同步喜欢」即可；")
    print("        这些作品会自动重新下载原图（已保留的 <1200px 页面会自动跳过）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())