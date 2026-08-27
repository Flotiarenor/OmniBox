# -*- coding: utf-8 -*-
"""Pixiv 已下载记录重置脚本（pixiv-sync 伴侣工具，独立命令行程序）

用途：把「已下载作品」的记录改回「未下载（等待重下）」，只改记录、不动文件。
适合删掉本地图片后想让插件重新下载的场景（例如上一次开了 1200px 大图、现在
想重下原图；或手动删了某段时期的文件想补回）。

为什么需要它：pixiv-sync 用两套独立数据——
  1. downloaded_ids.json（去重记录：作品 id 集合，两同步共用）；
  2. works.db（扫描/待下载清单：每件作品有 done 标记）。
同步下载时以 downloaded_ids.json / failed_ids.json 为准；works.db 的 done
只作为统计快照。如果只删了本地文件而不清这两套记录，再次点「同步」仍会把
它们跳过、不会重下。本脚本负责把目标作品在这些记录里改回「未下载」。

两种目标判定方式：
  A. 时间窗口模式（--hours N / 交互填 N）：
     扫描 pixiv/ 下仍存在的图片文件，按文件修改时间筛出最近 N 小时内的
     作品 id → 将其记录改回未下载。适合图片还在、想重下最近时期作品的情况。
     （注意：记录里没有保存下载时间，图片已删时此模式扫不到目标。）
  B. 清单缺失模式（--missing）：
     直接对照 works.db 清单与本地 pixiv/ 文件——凡是清单里 done=1 的作品，
     但其图片文件在本地已不存在（被删）→ 改回未下载。图片已删时用这个最准。

两种运行方式：
  1. 交互模式：不带参数直接运行（可双击），按提示输入下载根目录、目标模式并确认。
  2. 参数模式：
     --root <相册根目录>           指定根目录
     --hours <N>                  只处理最近 N 小时内（以文件时间）下载的作品
     --missing                    处理「清单已下载但本地文件已不存在」的作品
     --dry-run                    只扫描展示，不修改
     --yes                        跳过确认，直接执行
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Set

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 图片文件名 → 作品 id：123456.jpg / 123456_p0.jpg / 123456p0.png
ID_NAME_RE = re.compile(r"^(\d+)(?:_?p\d+)?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)

CACHE_SUBDIR = Path(".cache") / "pixiv-sync"


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


# ---------- 收集目标作品 id ----------

def collect_ids_in_window(pixiv_root: Path, hours: int) -> Set[int]:
    """扫描 pixiv/ 下仍有文件的图片，取最近 hours 小时内有变动的作品 id。"""
    ids: Set[int] = set()
    if not pixiv_root.exists():
        return ids
    since = None
    if hours:
        since = time.time() - hours * 3600
    for current, dir_names, filenames in os.walk(pixiv_root):
        dir_names[:] = [d for d in dir_names if not d.startswith(".")]
        for name in filenames:
            m = ID_NAME_RE.match(name)
            if not m:
                continue
            p = Path(current) / name
            if since is not None:
                try:
                    if p.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
            ids.add(int(m.group(1)))
    return ids


def collect_missing_ids(db_path: Path, pixiv_root: Path) -> Set[int]:
    """找出 works.db 里 done=1 但本地文件已不存在的作品 id（已删除 → 补重下）。"""
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            done_ids = {int(r[0]) for r in conn.execute("SELECT DISTINCT id FROM works WHERE done=1")}
        finally:
            conn.close()
    except Exception as e:
        print(f"  ! 读取 works.db 失败: {e}")
        return set()
    if not done_ids:
        return set()

    present: Set[int] = set()
    if pixiv_root.exists():
        for current, dir_names, filenames in os.walk(pixiv_root):
            dir_names[:] = [d for d in dir_names if not d.startswith(".")]
            for name in filenames:
                m = ID_NAME_RE.match(name)
                if m:
                    present.add(int(m.group(1)))
    return done_ids - present


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------- 记录修改（不动文件） ----------

def strip_ids(path: Path, ids: Set[int]) -> int:
    """从 json 记录文件中移除作品 id，保留其他字段；返回移除条数。"""
    if not path.exists() or not ids:
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, list):
        data = {"ids": data}
    if not isinstance(data, dict):
        return 0
    cur = data.get("ids") or []
    keep = []
    for x in cur:
        try:
            if int(x) not in ids:
                keep.append(x)
        except (TypeError, ValueError):
            keep.append(x)  # 保留无法解析的历史脏数据，避免误删用户记录
    removed = len(cur) - len(keep)
    if removed:
        data["ids"] = keep
        try:
            _atomic_write_json(path, data)
        except OSError as e:
            print(f"  ! 写入 {path.name} 失败: {e}")
            return 0
    return removed


def reset_done(db_path: Path, ids: Set[int]) -> int:
    """把 works.db 里这些作品的 done 标记改为 0（改回未下载）。"""
    if not db_path.exists() or not ids:
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
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
        print(f"  ! 重置 works.db done 失败: {e}")
        return 0


# ---------- 根目录定位 ----------

def resolve_root(root_arg: str) -> Path:
    """确定下载根目录：--root > 自动探测 > 交互输入。"""
    if root_arg:
        p = Path(root_arg).resolve()
        if not (p / "pixiv").exists():
            print(f"! 目录 {p} 下没有 pixiv/ 相册子目录（确认是否填对了下载根目录）？")
            print("  仍将继续。")
        return p

    cwd = Path.cwd()
    candidates = []
    app_yaml = cwd / ".config" / "app.yaml"
    if app_yaml.exists():
        try:
            root = _data_root_from_yaml(app_yaml, cwd)
            if root:
                candidates.append(("app.yaml data_root", root))
        except Exception:
            pass
    candidates.append(("cwd/data", cwd / "data"))
    for label, p in candidates:
        if (p / "pixiv").exists():
            print(f"自动找到相册根目录: {p}（{label}）")
            return p.resolve()

    print("-" * 60)
    default = str(cwd / "data")
    ans = input(
        f"未找到相册根目录，请输入下载根目录\n"
        f"（目录下应包含 pixiv/ 与 .cache/pixiv-sync/；留空使用 {default}）: "
    ).strip()
    return Path(ans or default).resolve()


def _data_root_from_yaml(yaml_path: Path, base: Path):
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
        description="Pixiv 下载记录重置：把已下载作品改回未下载（只改记录，不动文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  交互:        python pixiv_unmark_recent.py\n"
        "  时间窗口:    python pixiv_unmark_recent.py --root D:\\data --hours 2 --dry-run\n"
        "  清单缺失:    python pixiv_unmark_recent.py --root D:\\data --missing\n"
        "  直接执行:    python pixiv_unmark_recent.py --root D:\\data --missing --yes\n",
    )
    parser.add_argument("--root", help="下载根目录（含 pixiv/ 与 .cache/pixiv-sync/）")
    parser.add_argument("--hours", type=int, default=None,
                        help="只处理最近 N 小时内（以文件时间）的作品（0/留空 = 全部）")
    parser.add_argument("--missing", action="store_true",
                        help="处理「清单已下载但本地文件已不存在」的作品（图片已删时最准）")
    parser.add_argument("--dry-run", action="store_true", help="只扫描展示，不修改")
    parser.add_argument("--yes", action="store_true", help="跳过确认，直接执行")
    args = parser.parse_args()

    print("=" * 60)
    print("Pixiv 下载记录重置工具（只改记录，不删文件）")
    print("同步下载以 downloaded_ids.json / failed_ids.json 为准；works.db done 是统计快照。")
    print("本工具把目标作品从去重/失败记录移出，并把 done 改回 0。")
    print("=" * 60)

    root = resolve_root(args.root)
    cache = root / CACHE_SUBDIR
    pixiv_root = root / "pixiv"
    db_path = cache / "works.db"

    # 目标模式：--missing > --hours > 交互询问
    use_missing = args.missing
    hours = args.hours
    if not use_missing and hours is None and _is_tty():
        ans = input(
            "目标作品如何确定？\n"
            "  1 = 时间窗口（最近 N 小时的图片，需要文件还在）\n"
            "  2 = 清单缺失（清单已下载但文件已被删，推荐图片已删的情况）\n"
            "选择 (1/2，直接回车 = 2): "
        ).strip()
        use_missing = ans != "1"
        if not use_missing:
            ans2 = input("仅处理最近 N 小时内？(留空 = 全部；例如 1 或 2): ").strip()
            if ans2:
                try:
                    hours = max(0, int(ans2))
                except ValueError:
                    hours = None

    if use_missing:
        print("目标模式: 清单缺失（done=1 但本地文件已不存在）")
        print("  （注意: 只有整件作品没有任何文件在本地时才会命中；部分页被删的作品不会。）")
        target = collect_missing_ids(db_path, pixiv_root)
    else:
        scope_text = "全部（不限时间）"
        if hours:
            scope_text = f"最近 {hours} 小时内"
        print(f"目标模式: 时间窗口（{scope_text}，基于文件修改时间）")
        print("  （提示: 若图片已删除，时间窗口扫不到目标，请改用 --missing）")
        target = collect_ids_in_window(pixiv_root, hours)

    print("")
    if not target:
        print("没有命中需要重置的作品，无需操作。")
        if not use_missing:
            print("若你刚删了图片但这里显示 0 个，请改用 --missing 模式（对照清单判定缺失）。")
        return 1
    print(f"命中 {len(target)} 个作品:")

    sample = "、".join(str(i) for i in sorted(target)[:10])
    more = f" 等 {len(target)} 个" if len(target) > 10 else ""
    print(f"  将重置: {sample}{more}")
    print("修改内容（不动任何本地文件）:")
    print("  - downloaded_ids.json 移出这些 id（去重记录）")
    print("  - failed_ids.json 移出这些 id（失败跳过）")
    print("  - works.db 这些作品的 done 改回 0（清单改回待下载）")

    if args.dry_run:
        print("")
        print("[dry-run] 仅展示，未修改任何记录。可去掉 --dry-run 实际执行。")
        return 0

    if not args.yes:
        ans = input("确认执行记录重置？[y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消，未做任何修改。")
            return 1

    ids_removed = strip_ids(cache / "downloaded_ids.json", target)
    failed_removed = strip_ids(cache / "failed_ids.json", target)
    done_reset = reset_done(db_path, target)

    print("")
    print("执行完成:")
    print(f"  去重记录移除: {ids_removed} 条")
    print(f"  失败跳过移除: {failed_removed} 条")
    print(f"  清单 done 重置: {done_reset} 条（其余未在清单中则不适用）")
    print("")
    print("下一步: 「下载原图」现为固定默认行为（默认开启、无开关）。直接点「同步画师」或「同步喜欢」即可；")
    print("        这些作品会自动重新下载（无需先刷新名单）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())