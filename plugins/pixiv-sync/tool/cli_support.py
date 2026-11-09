"""两个伴侣命令行工具共用的部分：定位下载根、原子改 JSON 记录、重置清单 done。

为什么单独成文件：迁移前两个工具各自带一份「临时文件 + os.replace」的原子写、一份
`resolve_root`、一份 `strip_ids`。同一件事写两遍的下场是两边悄悄漂移（一处 fsync、
一处没有）。现在原子写只有内核 `shell.backend.json_cache` 一个实现，这里只保留两个
工具共用的**入口定位与记录编辑**。

为什么放在 tool/ 而不是 backend/：这两个工具从来不在插件的运行时 import 图里（入口
只加载 backend/），插件后端里的模块之间只写相对导入。把它们放在包外，既能被用户
直接 `python <工具>.py` 运行，也不让"命令行脚本"混进按包加载的后端目录。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Set

# 本文件位于 plugins/pixiv-sync/tool/，往上三级是仓库根。
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shell.backend.json_cache import write_json  # noqa: E402


def is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def data_root_from_json(config_path: Path, base: Path) -> Optional[Path]:
    """从 `.config/app.json` 的 `directories.data_root` 读下载根；无该键返回 None。"""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    directories = data.get("directories")
    if not isinstance(directories, dict):
        return None
    value = directories.get("data_root")
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def data_root_from_yaml(yaml_path: Path, base: Path) -> Optional[Path]:
    """旧版落点 `.config/app.yaml` 的 `data_root`；无该键返回 None。

    主配置已改为 `app.json`（与仓库其余持久化一致），这里保留旧版读法只是为了让
    工具能在"新版本还没启动过、旧文件尚未迁移"的机器上继续工作。
    """
    import re

    body = yaml_path.read_text(encoding="utf-8")
    m = re.search(r"^\s*data_root\s*:\s*(.+)$", body, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip().strip("'\"")
    p = Path(val)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def resolve_root(root_arg: Optional[str]) -> Path:
    """确定下载根目录：--root > 自动探测 > 交互输入。"""
    if root_arg:
        p = Path(root_arg).resolve()
        if not (p / "pixiv").exists():
            print(f"! 目录 {p} 下没有 pixiv/ 相册子目录（确认是否填对了下载根目录）？")
            print("  仍将继续（找不到图片时会输出 0 条）。")
        return p

    cwd = Path.cwd()
    candidates = []
    # 1) 当前目录的 .config 里有主配置 → 按它的 data_root（旧版是 app.yaml）
    for name, reader in (("app.json", data_root_from_json), ("app.yaml", data_root_from_yaml)):
        config_file = cwd / ".config" / name
        if not config_file.exists():
            continue
        try:
            root = reader(config_file, cwd)
        except Exception:
            root = None
        if root:
            candidates.append((f"{name} data_root", root))
            break
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
    return Path(ans or default).resolve()


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
        if not write_json(path, data, indent=None):
            print(f"  ! 写入 {path.name} 失败")
            return 0
    return removed


def reset_done_in_db(db_path: Path, ids: Set[int]) -> int:
    """把清单库里这些作品的 done 标记重置为 0；返回实际修改行数。

    下载清单（扫描记录）与去重记录是分开的两套数据：仅删除本地文件 + 移除去重 id
    还不够 —— 清单里的 done 仍是 1，直接点「同步」会被 not done 过滤跳过。
    """
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
        print(f"  ! 重置 works.db done 标记失败: {e}")
        return 0
