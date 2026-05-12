import os
import shutil
from pathlib import Path
from typing import List, Dict


class FileModule:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()

    def _is_safe(self, rel_path: str) -> bool:
        try:
            target = (self.base_dir / rel_path).resolve()
            return str(target).startswith(str(self.base_dir))
        except Exception:
            return False

    def list_dir(self, rel_path: str) -> List[Dict]:
        """列出子目录，供树组件使用"""
        if not self._is_safe(rel_path):
            return []
        target = self.base_dir / rel_path
        if not target.exists() or not target.is_dir():
            return []
        children = []
        try:
            for entry in os.scandir(target):
                if entry.is_dir() and not entry.name.startswith('.') and entry.name != '.cache':
                    child_path = (Path(rel_path) / entry.name).as_posix()
                    children.append({"name": entry.name, "path": child_path})
        except PermissionError:
            pass
        children.sort(key=lambda x: x['name'])
        return children

    def delete(self, rel_paths: List[str]) -> Dict:
        deleted, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法路径: {rel}")
                continue
            abs_path = self.base_dir / rel
            try:
                if abs_path.exists():
                    abs_path.unlink()
                    deleted.append(rel)
            except Exception as e:
                errors.append(f"删除失败 {rel}: {str(e)}")
        return {"deleted": deleted, "errors": errors}

    def move(self, rel_paths: List[str], dest_rel: str) -> Dict:
        if not self._is_safe(dest_rel):
            return {"moved": [], "errors": ["目标目录非法"]}
        dest_dir = self.base_dir / dest_rel
        if not dest_dir.is_dir():
            return {"moved": [], "errors": ["目标目录不存在"]}

        moved, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法源路径: {rel}")
                continue
            src = self.base_dir / rel
            try:
                if src.exists():
                    dest_file = dest_dir / src.name
                    if dest_file.exists() and src != dest_file:
                        stem, suffix = dest_file.stem, dest_file.suffix
                        counter = 1
                        while dest_file.exists():
                            dest_file = dest_dir / f"{stem}_{counter}{suffix}"
                            counter += 1
                    shutil.move(str(src), str(dest_file))
                    moved.append(rel)
            except Exception as e:
                errors.append(f"移动失败 {rel}: {str(e)}")
        return {"moved": moved, "errors": errors}