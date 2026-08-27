"""pixiv-sync 手工联调脚本（不是 unittest；仅 `python tests/test_parallel_tmp.py` 时执行）。

需要环境变量 PIXIV_REFRESH_TOKEN。v0.3 起不再有 download_original /
multi_page_subfolder 开关。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from shell.backend.plugin_manager import PluginManager

    tmp = Path(tempfile.mkdtemp(prefix="pixiv_pl_"))
    dl = (tmp / "downloads").resolve()
    dl.mkdir(parents=True, exist_ok=True)

    mgr = PluginManager(
        plugins_dirs=[ROOT / "plugins"],
        config={"directories": {"data_root": str(tmp / "data")}},
    )
    mgr.load_all()
    inst = mgr.get_plugin_instance("pixiv-sync")
    inst.save_settings({
        "refresh_token": os.environ["PIXIV_REFRESH_TOKEN"],
        "proxy": "http://127.0.0.1:7890",
        "download_dir": str(dl),
        "workers": 4,
    })

    inst.sync_following()
    t0 = time.time()
    last_done = 0
    while time.time() - t0 < 40:
        time.sleep(4)
        s = inst.get_status()
        t = s.get("task") or {}
        rate = (t.get("done", 0) - last_done) / 4
        last_done = t.get("done", 0)
        print(f"  {time.time()-t0:5.1f}s state={t.get('state')} total={t.get('total')} done={t.get('done')} "
              f"dl={t.get('downloaded')} skip={t.get('skipped')} fail={t.get('failed')} 最近速率~{rate:.1f}/s")
        if not s.get("running"):
            break
    inst.cancel_task()
    time.sleep(1.5)
    s = inst.get_status()
    t = s.get("task") or {}
    print(f"最终: state={t.get('state')} done={t.get('done')} dl={t.get('downloaded')} skip={t.get('skipped')} fail={t.get('failed')}")

    # 完整性校验：下载的文件都能打开
    import PIL.Image

    files = list(dl.rglob("*.jpg")) + list(dl.rglob("*.png"))
    bad = 0
    for f in files[:60]:
        try:
            PIL.Image.open(f).verify()
        except Exception:
            bad += 1
    print(f"文件完整性: 抽查 {min(60, len(files))} 个, 损坏 {bad}")
    pixiv_root = dl / "pixiv"
    artist_dirs = (
        [d for d in pixiv_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if pixiv_root.exists()
        else []
    )
    print(f"画师目录数: {len(artist_dirs)}")


if __name__ == "__main__":
    main()
