"""画师目录解析：名字命名 + id→名字缓存 + 改名迁移。"""

import json
import re
import shutil
from pathlib import Path
from typing import Dict


def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip() or "unknown"


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
        path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[pixiv-sync] 保存画师缓存失败: {e}")


def artist_dir(root: Path, cache_file: Path, uid: int, name: str) -> Path:
    """返回画师作品目录（以名字命名，统一放在 <root>/pixiv/ 下）。

    - 画师 id → 名字 记入本地缓存，画师改名后仍能识别为同一人；
    - 检测到改名时把旧名字目录迁移合并到新名字目录，避免历史作品"分家"。
    - 关注画师与收藏画师共用同一目录：作品归属画师，来源不再区分。
    """
    base = root / "pixiv"
    cache = load_cache(cache_file)
    key = str(uid)
    new_name = sanitize(name) or str(uid)
    old_name = cache.get(key)

    # 数字名保护：名字缺失（回退成 uid 数字）时沿用缓存名，
    # 避免缓存被数字覆盖、目录在名字/数字间来回改名抖动。
    if new_name == str(uid) and old_name:
        new_name = old_name

    if old_name and old_name != new_name:
        old_dir = base / old_name
        new_dir = base / new_name
        if old_dir.exists() and old_dir != new_dir:
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
    save_cache(cache_file, cache)
    return base / new_name