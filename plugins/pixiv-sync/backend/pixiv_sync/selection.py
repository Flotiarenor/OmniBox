"""画师名单（selected_artists.txt）：只同步指定画师。

状态自持：名单缓存与其失效签名（mtime_ns + size）都在这里。状态轮询频率很高，
每 8 秒读一次文件没必要；换根后由入口调用 reset() 让缓存跟着根走。

文件不存在或为空 = 同步全部关注画师。
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

#: 名单模板：open_config 每次重写它，用户始终能在文件里看到用法说明。
_SELECTED_TEMPLATE = (
    "# Pixiv 同步画师名单\n"
    "# 每行一个画师：填画师名字或 Pixiv 用户 id（# 开头为注释，空行忽略）\n"
    "# 示例:\n"
    "#   柠檬静静静静\n"
    "#   66477791\n"
    "#\n"
    "# 本文件存在且非空时，同步画师只处理名单中的画师；\n"
    "# 删除本文件或清空内容 = 同步全部关注画师\n"
)


def read_text_robust(file: Path) -> str:
    """读取文本文件，兼容 UTF-8 / UTF-8 BOM / GBK（记事本默认 ANSI 编码）。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return file.read_text(encoding=enc)
        except (UnicodeDecodeError, OSError):
            continue
    return ""


class ArtistSelection:
    def __init__(self, paths) -> None:
        self._paths = paths
        #: (mtime_ns, size) → 上次解析出的名单；签名不变就不重读文件。
        self._cache: Optional[Tuple[Tuple[int, int], Set[str]]] = None

    def selected(self) -> Set[str]:
        """名单内容：每行一个画师（名字或 Pixiv 用户 id），# 注释、空行忽略。"""
        file = self._paths.selected_file()
        try:
            stat = file.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._cache = None
            return set()

        if self._cache is not None:
            cached_sig, cached_value = self._cache
            if cached_sig == signature:
                return set(cached_value)

        selected: Set[str] = set()
        try:
            text = read_text_robust(file)
        except Exception as e:
            log.error(f"[pixiv-sync] 读取画师名单失败: {e}")
            self._cache = (signature, selected)
            return selected
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            selected.add(line)
        self._cache = (signature, selected)
        return selected

    def reset(self) -> None:
        """换根后丢弃名单缓存（缓存连着旧根的文件签名）。"""
        self._cache = None

    def open_config(self) -> Dict:
        """打开画师名单文件所在文件夹。

        每次调用都会重写注释模板（保留用户填写的画师行，非注释行），
        方便用户始终能看到用法说明。
        """
        try:
            file = self._paths.selected_file()
            file.parent.mkdir(parents=True, exist_ok=True)
            artists: List[str] = []
            if file.exists():
                for line in read_text_robust(file).splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        artists.append(line)
            body = _SELECTED_TEMPLATE
            if artists:
                body += "\n" + "\n".join(artists) + "\n"
            file.write_text(body, encoding="utf-8")
            self._open_folder(file.parent)
            return {"ok": True, "file": str(file)}
        except Exception as e:
            return {"ok": False, "error": f"打开失败: {e}"}

    @staticmethod
    def _open_folder(folder: Path) -> None:
        if sys.platform == "win32":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(folder)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["xdg-open", str(folder)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
