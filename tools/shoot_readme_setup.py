"""README 截图的**配置台**：起一个隔离实例，让你进去手工把界面调成要拍的样子。

两个脚本的分工
--------------
- 本文件（`shoot_readme_setup.py`）：**只负责配置**。建一份隔离的用户数据目录、起实例、
  打开浏览器；你在里面改的东西（背景图、主题、内边栏宽度、各插件的根目录、团体组网的
  共享项……）全部落在 `<home>` 里，成为**拍摄基线**。关掉就结束，不产图。
- `shoot_readme.py`：**只负责拍摄**。每次运行把基线复制一份到临时目录再跑，基线不被改写；
  按 `tools/readme_scenes.json` 的路由、点击顺序与文案断言逐个槽位出图。

为什么要隔离：截图里不允许出现本机真实数据（真实图库、账号、令牌），而手工配置又必须
改配置。隔离之后，`<home>/.config` 与 `data/` 都在 `.build/` 下（已被 gitignore），
仓库自己的 `.config/` 一个字节都不动。

它会替你做的四件事
------------------
1. 从仓库 `.config` 复制 `app.yaml` / `shell.json` / `plugins/*.json` 作为起点；
2. 把图片相册根指向 `--library`、漫画根指向 `--manga`；
3. pixiv-sync 换成**占位** refresh_token（真实令牌绝不进基线）并把下载根指到 `--library`，
   于是「设置 → 背景」与 pixiv 页都有东西可看；
4. 把 `--wallpapers` 目录里的壁纸复制进 `<home>/.config/backgrounds`，图片库直接是满的
   （12/13 两张要拍背景与玻璃，不用手工导入）。

用法：

    python tools/shoot_readme_setup.py                    # 打开配置窗口（复用现有基线）
    python tools/shoot_readme_setup.py --reset             # 重置成空白基线再来一遍（旧配置自动存档）
    python tools/shoot_readme_setup.py --web-only          # 不开窗口，用浏览器打开
    python tools/shoot_readme_setup.py --prepare-only      # 只铺基线 / 导壁纸，不起实例

基线是**空白**的：不从仓库 `.config` 复制任何东西 —— 那份配置带着你真实的图库、音乐库、
文档库路径与账号令牌，混进来之后"哪项是我配的、哪项是继承的"就说不清了。库路径、主题、
背景、宽度全部由你在窗口里配一次，之后每次拍摄复用它（复用分支不写任何配置文件）。

默认开的是**真正的桌面窗口**（`main.py` 不带 `--web-only`）：窗口态、背景图、玻璃效果、
「启动与窗口」里那些开关，在窗口里看才和平时用的一致。注意窗口固定 1400×900
（`main.py` 的 `create_window`），而拍摄视口是 1692×946 —— 绝对值类的项（视频舞台高度、
两个宽度）按取值对齐即可，两边的观感会略有差异。

窗口里要配的（拍摄基线只需要配一次）：

- 设置 → 背景：从已导入的示例壁纸里选一张，拉一下玻璃滑块；
- 设置 → 外观：主题、导航栏宽度、各插件内边栏宽度；
- 图片相册 → 设置：图片文件夹（示例库根）；
- 漫画中心 / 媒体播放器 / 文档阅读：各自的库根目录；
- 图片相册 → Pixiv 同步 → 设置：下载目录（11 那张要显示它）；
- 团体组网 → 共享项：挂一个示例目录并发布（07 那张要拍这一屏）。

关掉窗口（或 `Ctrl+C`）即结束，然后：

    python tools/shoot_readme.py                 # 13 张一起拍
    python tools/shoot_readme.py --check         # 只断言文案，不出图
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = PROJECT_ROOT / '.build' / 'shoot' / 'home'
DEFAULT_WALLPAPERS = PROJECT_ROOT / '.build' / 'shoot' / 'assets' / 'wallpapers'
DEFAULT_PORT = 18099

# 复制 / 存档基线时剔除的东西：WebView2 profile（几十 MB）、日志与凭据。
EXCLUDE_NAMES = frozenset({'webview', 'logs', 'auth_token.txt', 'principals.json'})


def seed_home(home: Path) -> None:
    """铺一份**空白**基线：只建 `.config` 目录，其余一概不写。

    为什么是空白，而不是从仓库 `.config` 复制：那份配置带着你真实的图库 / 音乐库 /
    文档库路径、账号令牌与歌单记录，复制过来之后"哪一项是我在这里配的、哪一项是继承来
    的"就说不清了；而截图红线本身就要求基线里不出现真实路径。空白起步，配置全部由你
    在窗口里做一次，之后每次拍摄都复用它。

    启动时应用会自己补齐 `app.yaml`、`shell.json`、各插件的设置文件与访问令牌
    （`main.py` 的首次运行路径），因此这里不需要预置任何东西。
    """
    (home / '.config').mkdir(parents=True, exist_ok=True)


def import_wallpapers(home: Path, source: Path) -> int:
    """把壁纸复制进 `<home>/.config/backgrounds`（幂等：已存在的跳过）。"""
    if not source.is_dir():
        return 0
    target = home / '.config' / 'backgrounds'
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in sorted(source.iterdir()):
        if item.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            continue
        destination = target / item.name
        if not destination.is_file():
            shutil.copy2(item, destination)
            copied += 1
    return copied


def clear_home(home: Path) -> None:
    """清空基线（保留目录本身）。"""
    for item in home.iterdir():
        if item.name in EXCLUDE_NAMES:
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def snapshot_home(home: Path) -> Optional[Path]:
    """把现有 `.config` 复制一份存档 —— `--reset` 会把里面的配置清掉。

    基线在 `.build/` 下（gitignore），手工配置的成本又不低，所以销毁前留档是必须的：
    存到 `<home 同级>/baseline-<时间戳>`，要回滚就把里面的内容拷回 `<home>/.config`。
    """
    config = home / '.config'
    if not config.is_dir():
        return None
    target = home.parent / f'baseline-{time.strftime("%Y%m%d-%H%M%S")}'
    shutil.copytree(config, target, ignore=shutil.ignore_patterns(*sorted(EXCLUDE_NAMES)))
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='起一个可手动配置的 README 截图基线实例')
    parser.add_argument('--home', default=str(DEFAULT_HOME), help=f'基线目录（默认 {DEFAULT_HOME}）')
    parser.add_argument('--wallpapers', default=str(DEFAULT_WALLPAPERS),
                        help='要导入「设置 → 背景」图片库的壁纸目录')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--reset', action='store_true', help='丢弃现有基线，从仓库配置重铺')
    parser.add_argument('--prepare-only', action='store_true',
                        help='只铺基线并导入壁纸，不起实例（给脚本化流程用）')
    parser.add_argument('--web-only', action='store_true',
                        help='用浏览器打开（默认开桌面窗口）')
    parser.add_argument('--no-open', action='store_true',
                        help='--web-only 时不自动打开浏览器')
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser().resolve()
    fresh = not (home / '.config').exists()
    if fresh:
        home.mkdir(parents=True, exist_ok=True)
        seed_home(home)
        print(f'[setup] 新建**空白**基线：{home}（库路径、主题、背景都由你在窗口里配）')
    elif args.reset:
        backup = snapshot_home(home)
        clear_home(home)
        seed_home(home)
        print(f'[setup] 已按 --reset 重置为空白基线：{home}')
        if backup is not None:
            print(f'[setup] 旧基线已存档：{backup}（要回滚就把里面内容拷回 {home / ".config"}）')
    else:
        print(f'[setup] 复用现有基线：{home}（要按空白重来请加 --reset，旧配置会自动存档）')

    copied = import_wallpapers(home, Path(args.wallpapers).expanduser())
    if copied:
        print(f'[setup] 导入壁纸 {copied} 张 → {home / ".config" / "backgrounds"}')
    if args.prepare_only:
        print('[setup] --prepare-only：未启动实例。')
        return 0

    env = {**os.environ, 'OMNIBOX_HOME': str(home),
           'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
    url = f'http://127.0.0.1:{args.port}/'
    if args.web_only:
        print(f'[setup] 浏览器模式：{url}   （Ctrl+C 结束）')
        if not args.no_open:
            webbrowser.open(url)
        command = [sys.executable, 'main.py', '--web-only', '--port', str(args.port)]
    else:
        # 桌面窗口：不传 --web-only，main.py 会 create_window('OmniBox', ...)。
        # 隔离靠 OMNIBOX_HOME —— 窗口里改的一切都落在 <home>，仓库 .config 不动。
        print(f'[setup] 桌面窗口已启动（隔离实例，内部服务端口 {args.port}）。'
              f'关掉窗口或 Ctrl+C 结束。')
        command = [sys.executable, 'main.py', '--port', str(args.port)]
    print('[setup] 窗口里配这些：设置 → 背景（选壁纸 + 玻璃）、设置 → 外观（主题 / 两个宽度）；'
          '图片相册 / 漫画中心 / 媒体播放器 / 文档阅读的库根目录；'
          'Pixiv 同步 → 设置里的下载目录；团体组网 → 共享项。')

    # 前台运行：日志直接打到终端，关窗 / Ctrl+C 即结束。这里不自己收尾 —— 交互式会话里
    # 让信号走默认路径比再写一套进程树清理更可靠。
    return subprocess.call(command, cwd=str(PROJECT_ROOT), env=env)


if __name__ == '__main__':
    raise SystemExit(main())
