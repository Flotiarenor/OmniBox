#!/usr/bin/env python3
"""测试“假 mpv 截获 ncm-cli 播放 URL”的方案。

用法:
    python tools/test_fake_mpv.py <加密歌曲ID> <原始歌曲ID>

流程:
    1. 在临时目录创建一个假的 mpv 可执行文件。
    2. 把该目录放到 PATH 最前面，确保 ncm-cli 调用 mpv 时命中我们的脚本。
    3. 运行 ncm-cli play --song ...
    4. 检查假 mpv 是否被调用；如果被调用，把参数写入日志并尝试提取 URL。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

FAKE_MPV_LOG = Path("/tmp/ncm-fake-mpv.log")
CAPTURED_URL_FILE = Path("/tmp/ncm-captured-url.txt")
FAKE_BIN_DIR = Path("/tmp/omnibox-fake-mpv")


def make_fake_mpv(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "mpv"
    fake.write_text(
        r"""#!/usr/bin/env python3
# 假 mpv：模拟 mpv 的 IPC socket，截获 ncm-cli 下发的真实播放 URL
import json
import os
import socket
import sys
import time
from pathlib import Path

LOG = Path("/tmp/ncm-fake-mpv.log")
CAP = Path("/tmp/ncm-captured-url.txt")

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
        time.sleep(60)
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
    fake.chmod(0o755)
    return fake


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python tools/test_fake_mpv.py <加密歌曲ID> <原始歌曲ID>")
        return 2

    enc_id, original_id = sys.argv[1], sys.argv[2]

    # 清理旧日志
    FAKE_MPV_LOG.unlink(missing_ok=True)
    CAPTURED_URL_FILE.unlink(missing_ok=True)

    bin_dir = FAKE_BIN_DIR
    fake_mpv = make_fake_mpv(bin_dir)
    print(f"[test] 假 mpv 已创建: {fake_mpv}")
    print(f"[test] PATH 前置目录: {bin_dir}")
    print(f"[test] 运行: ncm-cli play --song --encrypted-id {enc_id} --original-id {original_id}")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    try:
        proc = subprocess.run(
            [
                "ncm-cli", "play",
                "--song",
                "--encrypted-id", enc_id,
                "--original-id", original_id,
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=20,
            env=env,
        )
        print(f"[test] ncm-cli exit={proc.returncode}")
        if proc.stdout.strip():
            print("[test] stdout:")
            print(proc.stdout.strip()[:2000])
        if proc.stderr.strip():
            print("[test] stderr:")
            print(proc.stderr.strip()[:2000])
    except subprocess.TimeoutExpired:
        print("[test] ncm-cli 执行超时")

    # ncm-cli 可能先返回，实际由 daemon 异步拉起 mpv；等一会儿再检查日志
    print("[test] 等待 daemon 通过 IPC 下发播放 URL ...")
    time.sleep(5)

    print()
    if FAKE_MPV_LOG.exists():
        print("[result] ✅ 假 mpv 被调用了，日志如下:")
        print(FAKE_MPV_LOG.read_text(encoding="utf-8"))
    else:
        print("[result] ❌ 假 mpv 没有被调用。")
        print("[result] 最可能原因是尚未登录，ncm-cli 在拿到播放 URL 之前就退出了。")
        print("[result] 请先完成 ncm-cli login，再重新运行本测试。")

    if CAPTURED_URL_FILE.exists():
        url = CAPTURED_URL_FILE.read_text(encoding="utf-8").strip()
        print(f"[result] 🎯 捕获到播放 URL: {url}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
