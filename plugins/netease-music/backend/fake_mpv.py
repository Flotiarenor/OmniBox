"""假 mpv：截获 ncm-cli 播放时交给 mpv 的播放地址。

ncm-cli 只把真实播放地址交给播放器（而网易云的外链带时效，直接拼公共外链多数曲目 403），
所以这里在临时目录放一个同名的假 mpv：它按 mpv 的 IPC 协议接收 `loadfile` 命令，把 URL
写进 `ncm-captured-url.txt`，父进程轮询这个文件拿地址。

目录必须是**只有当前用户可写**的私有目录：假 mpv 是会被本进程执行的脚本，放在全局可写
目录里用固定名字会让本地其它用户预占/替换它（或以本进程权限覆盖任意文件）。

状态自持：首次准备时清理一次旧 daemon 与残留文件（之后保持常驻）；脚本内容与目录布局都在
这里，命令执行与缓存留在 `NeteaseMusicAPI`。
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional


class FakeMpv:
    """假 mpv 脚本的落点、残留清理与 URL 截获。"""

    def __init__(self) -> None:
        self._initialized = False

    # ===== 路径 =====

    def bin_dir(self) -> Path:
        """假 mpv 的目录（跨调用稳定：常驻 daemon 与 ncm-cli 用路径找这个 shim）。"""
        return Path(tempfile.gettempdir()) / 'cubby-ncm-fake-mpv'

    def captured_path(self) -> Path:
        return self.bin_dir() / 'ncm-captured-url.txt'

    def log_path(self) -> Path:
        return self.bin_dir() / 'ncm-fake-mpv.log'

    def socket_path(self) -> Path:
        return Path.home() / '.config' / 'ncm-cli' / 'mpv.sock'

    # ===== 准备 =====

    def prepare(self) -> Path:
        """确保假 mpv 就位并清掉上一轮残留；返回假 mpv 的可执行入口。"""
        bin_root = self.bin_dir()
        fake_bin = self._private_bin_dir(bin_root)
        fake_mpv = self._ensure_fake_mpv(fake_bin)
        if not self._initialized:
            self._reset_resident(fake_mpv)
            self._initialized = True
        return fake_mpv

    def environment(self) -> Dict[str, str]:
        """带假 mpv 目录的进程环境。

        必须用 `os.pathsep`：Windows 的 PATH 分隔符是 ';'，写死 ':' 会让整个 PATH 被当成
        一个路径，mpv.cmd 永远找不到 → 假 mpv 策略静默失效，每次取 URL 都退化成公共外链
        （多数曲目返回 403），且没有任何报错。
        """
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([str(self.bin_dir()), env.get("PATH", "")])
        return env

    def wait_url(self, timeout: int) -> Optional[str]:
        """轮询截获文件，最多等 `min(timeout, 10)` 秒；没拿到返回 None。"""
        captured = self.captured_path()
        deadline = time.time() + min(timeout, 10)
        while time.time() < deadline:
            if captured.exists():
                url = captured.read_text(encoding='utf-8').strip()
                if url:
                    return url
            time.sleep(0.2)
        return None

    # ===== 落盘与清理 =====

    def _reset_resident(self, fake_mpv: Path) -> None:
        """首次初始化时清理一次旧 daemon / 真实 mpv，之后保持常驻。"""
        for pat in ("ncm-cli play --song", "ncm-cli play --playlist",
                    str(fake_mpv), "mpv --no-video"):
            try:
                subprocess.run(["pkill", "-f", pat], capture_output=True, check=False)
            except Exception:
                pass
        for path in (self.captured_path(), self.log_path(), self.socket_path()):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _private_bin_dir(bin_dir: Path) -> Path:
        """返回只有当前用户可写的假 mpv 目录（不存在则创建并收紧权限）。

        假 mpv 是一个**会被本进程执行的脚本**，绝不能放在全局可写目录里用固定名字：
        Linux 的 /tmp 允许任何本地用户预先创建同名文件或符号链接，从而把脚本内容
        换成自己的代码（或以本进程权限覆盖任意文件）。这里在 tempdir 下开一个
        用户专属子目录，POSIX 上显式 0700。
        """
        bin_dir.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            try:
                bin_dir.chmod(0o700)
            except OSError:
                pass
        return bin_dir

    def _ensure_fake_mpv(self, bin_dir: Path) -> Path:
        """把假 mpv 脚本写进（已私有化的）bin_dir，返回可执行入口。

        目录必须**跨调用稳定**：常驻 daemon 与 ncm-cli 会用路径去查这个 shim，
        换目录就等于换了一个不存在的 mpv。私有化由 `_private_bin_dir()` 负责。
        """
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "mpv.py"
        script.write_text(
            r"""#!/usr/bin/env python3
import json, os, socket, sys, time
from pathlib import Path
# 与父进程共用同一个私有目录：以脚本自身位置为基准，避免两边各自
# 调用 gettempdir() 后指向不同位置（TMPDIR 被改写时会不一致）。
_BASE = Path(__file__).resolve().parent
LOG = _BASE / "ncm-fake-mpv.log"
CAP = _BASE / "ncm-captured-url.txt"
def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
def main():
    log("=== fake mpv called ===")
    log("ARGS=" + json.dumps(sys.argv[1:], ensure_ascii=False))
    if "--version" in sys.argv:
        print("mpv 0.37.0 Copyright © 2000-2023 mpv/MPlayer/mplayer2 projects")
        return 0
    sock_path = None
    for arg in sys.argv[1:]:
        if arg.startswith("--input-ipc-server="):
            sock_path = arg.split("=", 1)[1]
    if not sock_path:
        log("NO_IPC_SOCKET_ARG")
        time.sleep(30)
        return 0
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(5)
    log("IPC_LISTENING=" + sock_path)
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        with conn:
            f = conn.makefile("r", encoding="utf-8")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                log("IPC_RECV=" + line)
                try:
                    req = json.loads(line)
                except Exception:
                    continue
                cmd = req.get("command") or []
                name = cmd[0] if cmd else ""
                if name == "loadfile" and len(cmd) >= 2:
                    url = cmd[1]
                    CAP.write_text(url + "\n", encoding="utf-8")
                    log("CAPTURED_URL=" + url)
                resp = {"error": "success", "request_id": req.get("request_id")}
                try:
                    f.write(json.dumps(resp) + "\n")
                    f.flush()
                except Exception:
                    pass
                if name == "quit":
                    server.close()
                    return 0
        time.sleep(0.01)
if __name__ == "__main__":
    sys.exit(main())
""",
            encoding="utf-8",
        )
        if os.name == 'nt':
            fake = bin_dir / "mpv.cmd"
            fake.write_text(
                '@echo off\r\npython "%~dp0mpv.py" %*\r\n',
                encoding="utf-8",
            )
        else:
            fake = bin_dir / "mpv"
            # Unix 直接软链/复制 Python 脚本为可执行的 mpv
            try:
                fake.symlink_to(script.name)
            except OSError:
                fake.write_bytes(script.read_bytes())
            fake.chmod(0o755)
        return fake
