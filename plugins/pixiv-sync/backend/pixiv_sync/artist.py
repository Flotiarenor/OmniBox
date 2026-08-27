"""画师目录解析：名字命名 + id→名字缓存 + 改名迁移。"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict


# Windows 保留设备名；目录名会跨平台使用，避免在 Windows 上变成非法目录。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip().rstrip(" .")
    if not cleaned:
        return "unknown"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def load_cache(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_cache(path: Path, cache: Dict[str, str]):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[pixiv-sync] 保存画师缓存失败: {e}")


def _other_dir_names(cache: Dict[str, str], uid: int) -> set:
    """其他画师当前占用的目录名集合。"""
    key = str(uid)
    return {v for k, v in cache.items() if k != key and v}


def _disambiguate(base_name: str, uid: int, used: set) -> str:
    """为新画师/改名画师生成不与现有目录冲突的名字：`名字 (uid)`。"""
    if base_name not in used:
        return base_name
    candidate = f"{base_name} ({uid})"
    n = 2
    while candidate in used:
        candidate = f"{base_name} ({uid})_{n}"
        n += 1
    return candidate


def artist_dir(
    root: Path,
    cache_file: Path,
    uid: int,
    name: str,
    cache: Dict[str, str] | None = None,
    persist: bool = True,
) -> Path:
    """返回画师作品目录（以名字命名，统一放在 <root>/pixiv/ 下）。

    - 画师 id → 名字 记入本地缓存，画师改名后仍能识别为同一人；
    - 检测到改名时把旧名字目录迁移合并到新名字目录，避免历史作品"分家"。
    - 两个不同 id 但同名（sanitize 后相同）的新画师会消歧为 `名字 (uid)`；
      历史版本已经共用的目录保持不动，避免迁移时把另一位画师的文件一起搬走。
    - 批量解析目录时可由调用方传入同一个 cache 并设置 persist=False，
      最后再统一 save_cache，避免每个作品都读写一次 artists.json。
    """
    base = root / "pixiv"
    if cache is None:
        cache = load_cache(cache_file)
    key = str(uid)
    base_name = sanitize(name) or str(uid)
    old_name = cache.get(key)

    # 数字名保护：名字缺失（回退成 uid 数字）时沿用缓存名，
    # 避免缓存被数字覆盖、目录在名字/数字间来回改名抖动。
    if base_name == str(uid) and old_name:
        base_name = old_name

    used = _other_dir_names(cache, uid)
    if old_name and old_name == base_name:
        # 历史目录已按该名字落盘：不因同名冲突去搬目录，保持幂等。
        new_name = old_name
    else:
        new_name = _disambiguate(base_name, uid, used)

    if old_name and old_name != new_name:
        old_dir = base / old_name
        new_dir = base / new_name
        old_name_shared = any(v == old_name for k, v in cache.items() if k != key)
        if old_name_shared:
            # 旧目录被多个 uid 共用，无法安全判断哪些文件属于当前画师；
            # 只让新作品进入消歧后的目录，旧目录保持原样。
            print(
                f"[pixiv-sync] 画师 {uid} 目录名 {old_name!r} 被多个 id 共用，"
                f"已跳过迁移；新作品将写入 {new_name!r}"
            )
        elif old_dir.exists() and old_dir != new_dir:
            try:
                if not new_dir.exists():
                    old_dir.rename(new_dir)
                else:  # 新名字目录已存在 → 合并（不覆盖同名文件）
                    for item in old_dir.iterdir():
                        dest = new_dir / item.name
                        if item.is_dir():
                            if dest.exists():
                                for f in item.iterdir():
                                    if not (dest / f.name).exists():
                                        shutil.move(str(f), str(dest / f.name))
                                item.rmdir()
                            else:
                                item.rename(dest)
                        elif not dest.exists():
                            shutil.move(str(item), str(dest))
                    old_dir.rmdir()
                print(f"[pixiv-sync] 画师 {uid} 改名: {old_name} → {new_name}，目录已迁移")
            except OSError as e:
                print(f"[pixiv-sync] 画师改名目录迁移失败 {uid}: {e}")

    cache[key] = new_name
    if persist:
        save_cache(cache_file, cache)
    return base / new_name