"""图片相册性能探针：在**真实图库**上量 image-viewer 各阶段耗时（只读，不生成缩略图）。

为什么需要它：相册的慢不在"渲染了多少张图"，而在"列一次目录读了几个目录"。
单元测试用临时目录（十几个文件夹）量不出这个量级，只有真实图库能复现 ——
本工具就是那把尺子：同一个命令在改动前后各跑一次，数字直接可比。

设计约束（三条都是为了"可重复、可对比"）：

- **只读**：只调列表类 API，不触发缩略图生成 / 全量校验 / 重建，也不写任何缓存文件
  （除尺寸元数据在列出目录时按既有逻辑补条目外，不主动改磁盘）；
- **不依赖壳**：用 `importlib` 直接加载 `plugins/image-viewer/backend/main.py`，
  与 `tests/test_image_viewer_*.py` 同一套加载方式，因此开发机无需起服务；
- **索引来源明确**：默认从项目根 `.config/plugins/image-viewer.json` 读 `root_dir`
  （真实使用中的索引），也可用 `--root` 直接指定；`--fresh` 会先丢弃进程内缓存，
  量的是"冷启动第一次进相册页"，不带 `--fresh` 量的是"已经用起来之后"。

用法：

    venv/Scripts/python.exe tools/bench_gallery.py                    # 用本地配置的图库
    venv/Scripts/python.exe tools/bench_gallery.py --root D:\\图库      # 指定图库
    venv/Scripts/python.exe tools/bench_gallery.py --json out.json    # 存成对比基线
    venv/Scripts/python.exe tools/bench_gallery.py --repeat 3         # 多次取中位
    venv/Scripts/python.exe tools/bench_gallery.py --targets ,pixiv   # 只测某几层

默认探测范围是**合成根 + 根目录下全部直接子目录**（按直接图片数取前 40 个），
不写死目录名，因此可以直接在任何图库上跑；`--targets` 用来收窄到某几层。

输出分三段：`索引`（缓存规模）、`相册页`（list_albums）、`目录列表`（list_folder_items
+ load_album_tiles，逐个目录列出耗时与响应体大小）。秒数为**冷进程**测量：每次都在
新进程里跑一轮，避免上一次的目录缓存把机械盘的真实开销抹平。
"""

import argparse
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

_T = TypeVar('_T')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MAIN = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
CONFIG_FILE = PROJECT_ROOT / '.config' / 'plugins' / 'image-viewer.json'
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}

# 默认探测范围：合成根 + 全部直接子目录（见 discover_targets），不写死具体目录名
MAX_AUTO_TARGETS = 40


def load_plugin_module():
    """按插件管理器的加载方式载入后端入口（不起壳、不注册 API）。"""
    # 插件后端 import 的是 `shell.backend.*`，需要项目根在 sys.path 上
    # （与 tests/test_image_viewer_*.py 的开头同一手法）
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    spec = importlib.util.spec_from_file_location('iv_bench_probe', str(PLUGIN_MAIN))
    if spec is None or spec.loader is None:
        raise RuntimeError(f'无法加载插件入口: {PLUGIN_MAIN}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_targets(root: str) -> List[str]:
    """默认探测范围：合成根 + 图库的全部直接子目录。

    不写死目录名：每个图库的分类层不一样，探针要能直接在任何图库上跑。子目录按
    直接图片数（能免费数到的那一层）倒序取前 `MAX_AUTO_TARGETS` 个 —— 图片多的
    目录才是耗时大头，先测它们。
    """
    targets = ['']
    try:
        with os.scandir(root) as entries:
            subs = [e.name for e in entries
                    if e.is_dir() and not e.name.startswith('.') and e.name != '.cache']
    except OSError:
        return targets
    ranked = []
    for name in subs:
        direct = 0
        try:
            with os.scandir(Path(root) / name) as entries:
                for e in entries:
                    if e.is_file() and Path(e.name).suffix.lower() in IMAGE_SUFFIXES:
                        direct += 1
        except OSError:
            continue
        ranked.append((direct, name))
    ranked.sort(reverse=True)
    targets.extend(name for _n, name in ranked[:MAX_AUTO_TARGETS])
    return targets


def configured_root() -> Optional[str]:
    """本地 `.config/plugins/image-viewer.json` 里的 root_dir（未配置时 None）。"""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    root = data.get('root_dir')
    return str(root) if root else None


def make_plugin(module, root: str):
    """构造插件实例，绕过 SettingsStore 直接注入 root（与单测同一手法）。

    关掉目录枚举预热：探针要量的是**冷路径**（首次进相册页到底等多久），预热会把
    这趟 os.walk 挪到后台线程里，量出来的就不是用户真正会遇到的那一下了。
    """
    module.ImageViewerPlugin._resolved_config = {'root_dir': root, 'dirs_prewarm': False}
    return module.ImageViewerPlugin({'name': 'image-viewer'},
                                    {'directories': {'data_root': root}})


def timed(fn: Callable[[], _T]) -> tuple:
    t0 = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - t0


def payload_kb(value: Any) -> float:
    try:
        return len(json.dumps(value, ensure_ascii=False)) / 1024
    except (TypeError, ValueError):
        return 0.0


def collect(root: str, targets: Optional[List[str]]) -> Dict[str, Any]:
    """跑一轮完整探测，返回结构化结果（子进程里调用，保证冷进程语义）。"""
    if targets is None:
        targets = discover_targets(root)
    module = load_plugin_module()
    out: Dict[str, Any] = {'root': root, 'targets': targets}

    plugin, init_s = timed(lambda: make_plugin(module, root))
    out['init_s'] = init_s
    out['meta_entries'] = len(getattr(plugin, '_meta_cache', {}) or {})
    out['index_dirs'] = len((getattr(plugin, '_album_cache', {}) or {}).get('dirs') or {})
    try:
        stats = plugin.thumb_cache.stats()
        out['thumbs'] = {'count': stats.get('count', 0), 'bytes': stats.get('bytes', 0)}
    except Exception:
        out['thumbs'] = {'count': 0, 'bytes': 0}

    albums, albums_s = timed(lambda: plugin.list_albums())
    out['list_albums_s'] = albums_s
    out['list_albums_count'] = len(albums.get('albums') or [])
    out['list_albums_kb'] = payload_kb(albums)
    # 首次那 11s 里"走盘枚举"占多少：单独量一次全树 os.walk（预置空缓存）
    walker = getattr(plugin, '_walk_album_dirs', None)
    if callable(walker):
        dirs, walk_s = timed(walker)
        out['walk_dirs'] = len(dirs) if dirs is not None else 0
        out['walk_s'] = walk_s
    _, ttl_s = timed(lambda: plugin.list_albums())
    out['list_albums_ttl_s'] = ttl_s
    # 再进一次相册页：TTL 已过、但进程内的全树目录枚举缓存还在。这一项才是
    # "用起来之后"的真实成本 —— 旧实现 30s 一过就重走 os.walk
    plugin._albums_cached_at = 0.0
    plugin._albums_cached = None
    _, warm_s = timed(lambda: plugin.list_albums())
    out['list_albums_warm_s'] = warm_s

    folders = []
    for rel in targets:
        target = Path(root) / rel if rel else Path(root)
        if not target.is_dir():
            continue
        entry: Dict[str, Any] = {'path': rel}
        # 丢掉列表缓存，量"重新列一次目录"的真实成本（相册索引保留，模拟已用起来）
        try:
            plugin._list_cache.clear()
        except AttributeError:
            pass
        entry['sub_dirs'] = sum(1 for e in target.iterdir() if e.is_dir())
        entry['direct_files'] = sum(1 for e in target.iterdir() if e.is_file())
        data, dt = timed(lambda rel=rel: plugin.list_folder_items(rel, 1, 110, 'mtime', 'desc'))
        entry['items_s'] = dt
        entry['items_kb'] = payload_kb(data)
        entry['item_total'] = data.get('total')
        entry['all_images'] = len(data.get('all_images') or [])
        entry['sequence_pending'] = bool(data.get('sequence_pending'))
        entry['pending_count'] = data.get('pending_count')
        # 第二级加载：对前 12 个占位瓦片批量补封面/计数（新接口不存在时跳过）
        loader = getattr(plugin, 'load_album_tiles', None)
        if callable(loader):
            pending = [it.get('path') for it in (data.get('items') or [])
                       if it.get('pending') and it.get('path')][:12]
            if pending:
                tiles, tiles_s = timed(lambda batch=pending, fn=loader: fn(batch))
                entry['tiles_s'] = tiles_s
                entry['tiles_count'] = len(tiles.get('tiles') or {})
                entry['tiles_kb'] = payload_kb(tiles)
            else:
                entry['tiles_s'] = None
        # 第三级加载：打开灯箱取完整连续序列（只在序列被推迟时才付这笔开销）
        seq_loader = getattr(plugin, 'list_album_images', None)
        if callable(seq_loader) and data.get('sequence_pending'):
            seq, seq_s = timed(lambda rel=rel, fn=seq_loader: fn(rel))
            entry['sequence_s'] = seq_s
            entry['sequence_len'] = len(seq.get('images') or [])
            entry['sequence_capped'] = bool(seq.get('capped'))
        folders.append(entry)
    out['folders'] = folders
    return out


def _force_utf8() -> None:
    """控制台按 UTF-8 输出：图库路径与目录名含中文，Windows 默认 GBK 会直接抛错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def run_cold(root: str, targets: Optional[List[str]]) -> Dict[str, Any]:
    """在新进程里跑一轮，避免同进程的目录缓存掩盖机械盘开销。"""
    # 路径经环境变量传：命令行参数走 Windows 的 ANSI 编码，中文图库路径会变问号
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'IV_BENCH_ROOT': root}
    payload = json.dumps(targets) if targets is not None else 'null'
    proc = subprocess.run(
        [sys.executable, str(Path(__file__)), '--_child', payload],
        capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith('{'):
            return json.loads(line)
    raise RuntimeError(f'子进程未返回结果:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}')


def _pick(run: Optional[Dict[str, Any]], path: str, key: str) -> Optional[float]:
    """从一轮结果里取某个目录的某个秒数字段（缺则为 None）。"""
    for folder in (run or {}).get('folders') or []:
        if folder.get('path') == path:
            value = folder.get(key)
            return float(value) if value is not None else None
    return None


def median_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """多轮取中位：秒数字段取中位，其余结构性数据沿用第一轮。"""
    if len(runs) == 1:
        return runs[0]
    base = dict(runs[0])
    for key in ('init_s', 'list_albums_s', 'list_albums_ttl_s'):
        base[key] = statistics.median(r[key] for r in runs)
    folders = []
    for folder in base.get('folders') or []:
        merged = dict(folder)
        for key in ('items_s', 'tiles_s'):
            values = [v for v in (_pick(r, folder['path'], key) for r in runs) if v is not None]
            if values:
                merged[key] = statistics.median(values)
        folders.append(merged)
    base['folders'] = folders
    return base


def report(result: Dict[str, Any]) -> None:
    thumbs = result.get('thumbs') or {}
    print(f'图库: {result["root"]}')
    print(f'索引: 相册索引 {result["index_dirs"]} 个目录 · 尺寸元数据 '
          f'{result["meta_entries"]} 条 · 缩略图 {thumbs.get("count", 0)} 条 / '
          f'{thumbs.get("bytes", 0) / 1024 ** 3:.2f} GB')
    print()
    print('相册页')
    print(f'  __init__(读索引)            {result["init_s"]:7.3f}s')
    print(f'  list_albums(首次)           {result["list_albums_s"]:7.3f}s  '
          f'{result["list_albums_count"]} 个相册 / {result["list_albums_kb"]:.0f} KB')
    if result.get('walk_s') is not None:
        print(f'    其中全树 os.walk          {result["walk_s"]:7.3f}s  '
              f'← {result.get("walk_dirs", 0)} 个目录（只走一次，之后走缓存）')
    print(f'  list_albums(TTL 命中)       {result["list_albums_ttl_s"]:7.3f}s')
    warm = result.get('list_albums_warm_s')
    if warm is not None:
        print(f'  list_albums(TTL 过期再进)   {warm:7.3f}s  '
              f'← 旧实现这里会重走全树 os.walk')
    print()
    print('目录列表（list_folder_items，每页 110）')
    print(f'  {"目录":<14}{"子目录":>6}{"直接图":>7}{"耗时":>10}{"响应":>10}'
          f'{"瓦片":>12}{"序列":>9}{"开灯箱":>10}')
    for folder in result.get('folders') or []:
        name = folder['path'] or '<根>'
        tiles_s = folder.get('tiles_s')
        seq_s = folder.get('sequence_s')
        seq = '待补' if folder.get('sequence_pending') else '完整'
        print(f'  {name:<14}{folder["sub_dirs"]:>6}{folder["direct_files"]:>7}'
              f'{folder["items_s"]:>9.3f}s{folder["items_kb"]:>9.0f}KB'
              f'{(f"{tiles_s:.3f}s" if tiles_s is not None else "—"):>12}'
              f'{seq:>9}'
              f'{(f"{seq_s:.3f}s" if seq_s is not None else "—"):>10}')


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        description='图片相册性能探针（只读；量真实图库上各阶段的冷进程耗时）')
    parser.add_argument('--root', default=None,
                        help='图库根目录；默认读 .config/plugins/image-viewer.json 的 root_dir')
    parser.add_argument('--targets', default=None,
                        help='只探测这些目录（相对根路径，逗号分隔；空串表示根）。'
                             '默认遍历根目录下全部直接子目录')
    parser.add_argument('--repeat', type=int, default=1, help='重复轮数，取中位（默认 1）')
    parser.add_argument('--json', dest='json_out', default=None, help='把结果写入 JSON 文件')
    parser.add_argument('--_child', nargs=1, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args._child:                     # 子进程模式：跑一轮，把结果打到 stdout
        root = os.environ.get('IV_BENCH_ROOT', '')
        print(json.dumps(collect(root, json.loads(args._child[0])), ensure_ascii=False))
        return 0

    root = args.root or configured_root()
    if not root:
        print('未指定图库：用 --root 指定，或在 .config/plugins/image-viewer.json 里配置 '
              'root_dir', file=sys.stderr)
        return 2
    if not Path(root).is_dir():
        print(f'图库不存在或不可读: {root}', file=sys.stderr)
        return 2

    targets = None
    if args.targets:
        targets = [t.strip() for t in str(args.targets).split(',')]
        if '' not in targets:
            targets.insert(0, '')
    runs = [run_cold(root, targets) for _ in range(max(1, args.repeat))]
    result = median_runs(runs)
    report(result)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n已写入 {args.json_out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
