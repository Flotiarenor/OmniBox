#!/usr/bin/env python3
"""media-player 媒体库维护工具：专辑标签 / 重复文件 / 歌词对照。

为什么放在插件目录里：它读的缓存（<媒体库根>/.cache/media_index.json 的 album_key）
与标签读取实现（backend/metadata.py）都属于本插件，缓存格式一变就在同一个提交里改完。
插件运行时不会加载本文件，它只在命令行被调用，因此不需要登记 HIDDEN_IMPORTS。

子命令一律「默认预演、加 --apply 才动手」：

    albums [--list] [--apply]
        按插件缓存列出「同名却被拆成多张」的专辑（口径与播放器界面完全一致），
        再按 ALBUM_ARTIST_FIXES / FILE_FIXES 两张表补 ALBUM / ALBUMARTIST / TRACK。
        预演打印将要改写的字段；--apply 写完后回读校验。

    dups [--apply] [--ignore-content]
        同一目录下「名字只多一对括号后缀」(xxx) 且大小 + MD5 相同的文件视为重复，
        保留没有括号的那个（移植自原 Remove-DupTracks.ps1）。--apply 才真删。

    lrc [--apply]
        歌词与音频对照：成对 / 缺少 lrc / 多余 lrc 三项计数，写 缺少lrc.txt 与
        多余lrc.txt（沿用旧 .bat 的格式）。--apply 时把多余的 .lrc 移到
        <根目录>\\_多余lrc\\，**不删除任何文件**。

    --selftest
        在临时目录造用例，自检 dups 与 lrc 的判定（不碰媒体库）。

媒体库根目录的解析顺序（命中即打印来源）：--root、OMNIBOX_HOME、脚本上三级
（开发模式 = 仓库根）、exe 同级（冻结模式）、%APPDATA%\\OmniBox。注意冻结模式下
用户数据在 exe 旁边而不是打包解包目录，所以不能只按脚本位置推断。

缓存只读、不写回：索引 / 缩略图 / 收藏与播放列表都是插件的职责，外部脚本改它会让
界面状态错乱。改完标签靠文件 mtime 变化，回播放器点一次「扫描」即可。

缓存不止一份：插件把「这一次扫描的合并索引」写在当时那个媒体根目录下的
.cache/media_index.json（root_dir 与 media_roots 的每个根都可能有），内容却是所有
根的合并结果。所以工具会把所有媒体根的缓存列出来、挑 updated 最新的那份；缓存与磁盘
对不上（音频条目数不符，或有条目的 mtime/size 变了）时自动改用直接读标签，并说明原因。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from mutagen import File as MutagenFile

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
METADATA_PY = PLUGIN_ROOT / 'backend' / 'metadata.py'
CONFIG_REL = Path('.config') / 'plugins' / 'media-player.json'
CACHE_REL = Path('.cache') / 'media_index.json'

# 插件认识的音频扩展名，并上旧 .bat 里的 .oga（插件不认它，但歌词配对必须认）
AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.aac', '.ogg', '.oga', '.wma', '.m4a', '.opus', '.ape', '.wv'}
LRC_QUARANTINE = '_多余lrc'
MISSING_LRC_TXT = '缺少lrc.txt'
ORPHAN_LRC_TXT = '多余lrc.txt'
UNMOVED_LRC_TXT = '未能移动.txt'
DUP_SUFFIX_RE = re.compile(r'^(.+?)\s*[\(（][^\)）]*[\)）]\s*$')

# ===== 专辑标签修正表 =====
# 取值依据（核对 iTunes / Apple Music、MusicBrainz、网易云音乐）：
#   AD:PIANO VII -Alternative-：iTunes 专辑艺人 = Various Artists（16 首，2019-12-31）；
#     网易云记作 Diverse System —— 想跟下载源一致就改这一行的值
#   Neko Hacker：iTunes/网易云 = Neko Hacker（12 首，2020-01-15），#1 From Zero (feat. Rika)、
#     #3 Home Sweet Home (feat. Kmnz Liz)，与库里 TRACK=1/3 吻合
#   Floating Star：iTunes = Floating Star - Single，Kirara Magic（3 首：#1 Floating Star、
#     #2 Aurora VIP、#3 Eternal Snow），与库里 TRACK=1/2/3 吻合
#   ウマ娘 ANIMATION DERBY Season 2 vol.3 OST：iTunes 与网易云专辑艺人均为 UTAMARO Movement（33 首）
#   天気の子：网易云专辑艺人 = RADWIMPS（31 首）
#   花鳥風月：iTunes = Guiano（12 首），#5 Flower feat. KAF、#8 Moon feat. Isekaijoucho 与库里吻合
#   すずめの戸締まり：网易云 = RADWIMPS（29 首）；iTunes 写作 RADWIMPS & 陣内一真
#   リゼロ Stay Alive EP（歌：エミリア（CV：高橋李依））：4 首，#1 Stay Alive、#2 Wishing，
#     两个文件标签都对，只是缺 ALBUMARTIST 才被拆开
#   Myths & Legends：The Score 是 2017 年 4 首单曲（#3 Revolution）、Fox Sailor 是 2021 年
#     16 首专辑 —— 同名不同作，故意不合并
ALBUM_ARTIST_FIXES = {
    'AD:PIANO VII -Alternative-': 'V.A.',
    'Neko Hacker': 'Neko Hacker',
    'Floating Star': 'Kirara Magic',
    'TVアニメ『ウマ娘 プリティーダービー』ANIMATION DERBY Season 2 vol.3 Original Sound Track': 'UTAMARO Movement',
    '天気の子': 'RADWIMPS',
    '花鳥風月': 'Guiano',
    'すずめの戸締まり': 'RADWIMPS',
    'TVアニメ「Re：ゼロから始める異世界生活」後期エンディングテーマ「Stay Alive」 歌：エミリア（CV：高橋李依）': '高橋李依',
}

# 允许覆盖已有值的专辑：早期按 iTunes 写成 V.A.，联网核对后改成两家都署名的
# UTAMARO Movement。平时已有别的 ALBUMARTIST 的文件一律跳过并报告。
OVERWRITE_ALBUMS = {
    'TVアニメ『ウマ娘 プリティーダービー』ANIMATION DERBY Season 2 vol.3 Original Sound Track',
}

# 文件名 -> 要补齐的字段（只写列出的键；track 用于专辑内排序）
FILE_FIXES = {
    'SawanoHiroyuki[nZk],ReoNa - time.flac': {
        'album': 'iv', 'albumartist': 'SawanoHiroyuki[nZk]', 'track': 7},
    'しぐれうい - 粛聖!! ロリ神レクイエム.flac': {
        'album': 'まだ雨はやまない', 'albumartist': 'しぐれうい', 'track': 6},
    '稲葉曇,歌愛ユキ - ラグトレイン.flac': {
        'album': 'ウェザーステーション', 'albumartist': '稲葉曇', 'track': 4},
    '香椎モイミ,可不 - キャットラビング.flac': {
        'album': '絢爛のアシンメトリー', 'albumartist': 'V.A.', 'track': 1},
    # 原作单曲的官方署名是 PIKASONIC & Tatsunoshin（NEONA 只是 featuring），VIP 版才把
    # NEONA 算作艺人；两首仍各是一张独立单曲，不合并。
    'PIKASONIC,Tatsunoshin,NEONA - Lockdown (feat. NEONA).flac': {
        'albumartist': 'PIKASONIC/Tatsunoshin'},
    # 鹿乃 这两首都是歌ってみた 翻唱（この世界を愛したい cover.鹿乃 / 花に亡霊 cover 鹿乃），
    # iTunes、MusicBrainz、网易云都没有收录进官方专辑，合成一张自制合集避免落进假专辑。
    '鹿乃 - 花に亡霊.mp3': {'album': '鹿乃 歌ってみた', 'albumartist': '鹿乃'},
    '鹿乃 - この世界を愛したい.mp3': {'album': '鹿乃 歌ってみた', 'albumartist': '鹿乃'},
}

TAG_KEYS = ('album', 'albumartist', 'tracknumber')


def _utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass


@lru_cache(maxsize=1)
def metadata_reader():
    """复用插件的标签读取实现，保证脚本看到的分组与插件一致。"""
    if not METADATA_PY.is_file():
        sys.exit(f'找不到插件的标签模块：{METADATA_PY}（插件目录结构变了？）')
    spec = importlib.util.spec_from_file_location('media_player_tool_metadata', METADATA_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MetadataReader


# ===== 媒体库根目录：读 OmniBox 自己的配置，不靠脚本位置猜 =====

def config_candidates() -> list[Path]:
    candidates = []
    home = os.environ.get('OMNIBOX_HOME', '').strip()
    if home:
        candidates.append(Path(home).expanduser() / CONFIG_REL)
    # 开发模式：plugins/media-player/tool/ 往上三级就是仓库根
    candidates.append(PLUGIN_ROOT.parent.parent / CONFIG_REL)
    # 冻结模式：用户数据在 exe 旁边（打包解包目录是临时目录，不能用来找配置）
    candidates.append(Path(sys.executable).resolve().parent / CONFIG_REL)
    appdata = os.environ.get('APPDATA') or str(Path.home())
    candidates.append(Path(appdata) / 'OmniBox' / CONFIG_REL)
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def resolve_library(explicit: str | None) -> dict:
    """返回 {'root': 音频库根, 'roots': 所有媒体根, 'config': 用了哪份配置}。"""
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            sys.exit(f'媒体库目录不存在：{root}')
        print(f'媒体库根：{root}（来自 --root）')
        return {'root': root, 'roots': [root], 'config': None}

    for config in config_candidates():
        if not config.is_file():
            continue
        try:
            data = json.loads(config.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(f'  ! 配置无法解析，跳过 {config}：{exc}')
            continue
        raw = str(data.get('root_dir') or '').strip()
        if not raw:
            listed = [line.strip() for line in str(data.get('media_roots') or '').splitlines() if line.strip()]
            raw = listed[0] if listed else ''
        if not raw:
            continue
        root = Path(raw).expanduser()
        if not root.is_dir():
            print(f'  ! 配置里的 {raw} 不是目录，跳过 {config}')
            continue
        roots = [root]
        for line in str(data.get('media_roots') or '').splitlines():
            candidate = Path(line.strip()).expanduser()
            if line.strip() and candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
        print(f'媒体库根：{root}（来自 {config}）')
        return {'root': root, 'roots': roots, 'config': config}

    looked = '、'.join(str(p) for p in config_candidates())
    sys.exit(f'找不到媒体库目录，依次看过：{looked}\n用 --root <目录> 直接指定。')


# ===== 缓存（只读；多份，挑最新那份） =====

def disk_audio_count(roots: list[Path]) -> int:
    return sum(1 for root in roots for path in walk_files(root) if path.suffix.lower() in AUDIO_EXTS)


def pick_cache(roots: list[Path]) -> tuple[Path | None, dict]:
    """每个媒体根各有一份缓存，取 updated 最新的那份（插件只把最新扫描写进其中一份）。"""
    best_path, best_data, best_updated = None, {}, -1.0
    for root in roots:
        path = root / CACHE_REL
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(f'  ! 缓存无法解析，跳过 {path}：{exc}')
            continue
        updated = float(data.get('updated') or 0)
        if updated > best_updated:
            best_path, best_data, best_updated = path, data, updated
    return best_path, best_data


def load_cache(roots: list[Path]) -> dict:
    """读最新那份缓存，并用磁盘核对它是否还跟得上（条目数 + 每条 mtime/size）。"""
    path, data = pick_cache(roots)
    if path is None:
        print('  ! 没找到任何缓存（先在播放器里扫描一次）——本次直接读标签')
        return {}

    items = [item for item in data.get('items', []) if isinstance(item, dict)]
    audio = [item for item in items if item.get('kind') == 'audio']
    # 只有音频条目影响专辑分组：视频文件被改动不该让本命令退回读标签
    changed = 0
    for item in audio:
        try:
            stat = Path(str(item.get('path') or '')).stat()
        except OSError:
            changed += 1
            continue
        if stat.st_size != item.get('size') or abs(stat.st_mtime - float(item.get('mtime') or 0)) > 1:
            changed += 1

    on_disk = disk_audio_count(roots)
    fresh = changed == 0 and len(audio) == on_disk
    print(f'缓存：{path}（version={data.get("version")}，条目 {len(items)}＝音频 {len(audio)} + 视频 '
          f'{len(items) - len(audio)}；磁盘音频 {on_disk}，缓存内已变/丢失 {changed}）')
    if not fresh:
        print('  ! 缓存与磁盘不一致：下面改用直接读标签（结果仍是插件口径的分组规则）。')
        print('    想让它变快就在播放器里点一次「扫描」。')
    return {'path': path, 'version': data.get('version'), 'items': items, 'fresh': fresh}


def _key_parts(album_key: str) -> tuple[str, str, str]:
    """从 album_key 里取出（媒体根命名空间, 专辑名, 专辑艺术家）：'ns//album::专辑||艺术家'。"""
    namespace, _, body = album_key.partition('//album::')
    name, _, artist = body.partition('||')
    return namespace, name, artist


def cached_groups(cache: dict, namespace: str) -> dict[str, dict[str, list[Path]]]:
    """按插件口径聚合某一个媒体根的音频：album -> {album_artist: [文件]}。"""
    groups: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    for item in cache.get('items', []):
        if item.get('kind') != 'audio':
            continue
        item_namespace, name, artist = _key_parts(str(item.get('album_key') or ''))
        if not name or (namespace and item_namespace != namespace):
            continue
        groups[name][artist or str(item.get('album_artist') or '')].append(Path(str(item.get('path') or '')))
    return {name: dict(artists) for name, artists in groups.items()}


# ===== 直接读标签（缓存缺失时的回退，以及写入前的核对） =====

def walk_files(root: Path) -> list[Path]:
    """递归列文件：跳过隐藏目录与 _多余lrc（隔离区不再参与判定）。"""
    found = []
    for current, dir_names, file_names in os.walk(root):
        dir_names[:] = [d for d in dir_names if not d.startswith('.') and d != LRC_QUARANTINE]
        found.extend(Path(current) / name for name in sorted(file_names))
    return found


def survey(root: Path) -> dict[str, dict[str, list[Path]]]:
    """按插件规则（专辑 + 专辑艺术家，缺 ALBUMARTIST 回退到 artist）分组。"""
    reader = metadata_reader()
    groups: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    for path in walk_files(root):
        if path.suffix.lower() not in AUDIO_EXTS:
            continue
        meta = reader.read(path)
        album = str(meta.get('album') or '').strip() or path.parent.name
        album_artist = str(meta.get('album_artist') or '').strip() or str(meta.get('artist') or '未知艺术家')
        groups[album][album_artist].append(path)
    return {name: dict(artists) for name, artists in groups.items()}


def raw_tags(path: Path) -> dict:
    """文件里实际存在的标签值（没有则为空串），用于判断要改哪些字段。"""
    audio = MutagenFile(str(path), easy=True)
    tags = audio.tags if (audio is not None and audio.tags) else {}
    values = {}
    for key in TAG_KEYS:
        found = tags.get(key) or []
        values[key] = str(found[0]).strip() if found else ''
    return values


def print_splits(groups: dict[str, dict[str, list[Path]]]):
    print('=== 同名却被拆成多张的专辑（播放器口径） ===')
    splits = {name: artists for name, artists in groups.items() if len(artists) > 1}
    if not splits:
        print('  （没有）')
        return
    for name in sorted(splits):
        mark = '  ← 已在 ALBUM_ARTIST_FIXES' if name in ALBUM_ARTIST_FIXES else ''
        print(f'\n[{name}]{mark}')
        for artist in sorted(splits[name]):
            print(f'   album_artist={artist!r}  ({len(splits[name][artist])} 首)')
            for path in splits[name][artist]:
                print(f'      - {path.name}')


# ===== albums：按表补标签 =====

def build_plan(groups: dict[str, dict[str, list[Path]]], root: Path) -> tuple[dict[Path, dict], list]:
    """返回（待写清单 {文件: {标签: 新值}}, 冲突清单）。"""
    items: dict[Path, dict] = {}
    conflicts = []

    for album, target in ALBUM_ARTIST_FIXES.items():
        for paths in groups.get(album, {}).values():
            for path in paths:
                current = raw_tags(path)['albumartist']
                if current and current != target and album not in OVERWRITE_ALBUMS:
                    conflicts.append((path, 'albumartist', current, target))
                elif current != target:
                    items.setdefault(path, {})['albumartist'] = target

    by_name: dict[str, list[Path]] = defaultdict(list)
    for path in walk_files(root):
        by_name[path.name].append(path)
    for name, fields in FILE_FIXES.items():
        matches = by_name.get(name, [])
        if len(matches) != 1:
            reason = '找不到' if not matches else f'同名 {len(matches)} 个，无法确定改哪个'
            print(f'  ! 跳过 {name}：{reason}')
            continue
        path = matches[0]
        current = raw_tags(path)
        for key, want in fields.items():
            tag_key = 'tracknumber' if key == 'track' else key
            if str(want) != current[tag_key]:
                items.setdefault(path, {})[tag_key] = str(want)
    return items, conflicts


def write_tags(path: Path, fields: dict):
    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        raise RuntimeError(f'mutagen 无法解析：{path}')
    for key, value in fields.items():
        audio[key] = [value]
    audio.save()


def verify(written: dict[Path, dict]) -> list[str]:
    """回读刚写过的文件，确认标签真的落盘、且每张目标专辑只剩一组 album_artist。"""
    reader = metadata_reader()
    bad = []
    by_album: dict[str, set[str]] = defaultdict(set)
    for path, fields in written.items():
        meta = reader.read(path)
        got = {'album': str(meta.get('album') or ''), 'albumartist': str(meta.get('album_artist') or ''),
               'tracknumber': str(meta.get('track') or '')}
        for key, want in fields.items():
            if got[key] != want:
                bad.append(f'{path.name}：{key} 期望 {want!r}，实际 {got[key]!r}')
        if 'albumartist' in fields:
            by_album[got['album']].add(got['albumartist'])
    for album, artists in by_album.items():
        if len(artists) > 1:
            bad.append(f'{album}：写入后仍有 {len(artists)} 组 album_artist {sorted(artists)}')
        else:
            print(f'  {album} -> {sorted(artists)}')
    return bad


def cmd_albums(library: dict, apply: bool, list_only: bool) -> int:
    root = library['root']
    cache = load_cache(library['roots'])
    groups = cached_groups(cache, root.name) if cache.get('fresh') else {}
    if groups:
        print('（下面按缓存列，与播放器界面一致）')
    else:
        groups = survey(root)
        print('（下面按磁盘标签列，未使用缓存）')
    print_splits(groups)
    if list_only:
        return 0

    items, conflicts = build_plan(groups, root)
    print(f'\n需改写的文件：{len(items)} 个')
    for path, fields in sorted(items.items()):
        print(f"  + {path.name}  -> {', '.join(f'{k}={v!r}' for k, v in fields.items())}")
    for path, key, current, target in conflicts:
        print(f'  ! 跳过（{key} 已有 {current!r}，目标 {target!r}）：{path.name}')

    if not apply:
        print('\n预览模式，未修改任何文件。确认后加 --apply。')
        return 0

    for path, fields in items.items():
        write_tags(path, fields)
    print(f'\n已写入 {len(items)} 个文件，开始回读校验……')
    bad = verify(items)
    if bad:
        for line in bad:
            print(f'  ! {line}')
        return 1
    print('校验通过。回播放器点一次「扫描」，让索引按新的 mtime 重建这些条目。')
    return 0


# ===== dups：同名括号后缀 + 内容一致 =====

def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def find_dups(paths: list[Path], ignore_content: bool = False) -> list[tuple[Path, list[Path]]]:
    """同一目录 + 同一扩展名，去掉结尾括号后缀后同名；内容（大小 + MD5）也相同才算重复。"""
    originals: dict[tuple[str, str, str], Path] = {}
    for path in paths:
        key = (str(path.parent).lower(), path.stem.lower(), path.suffix.lower())
        originals.setdefault(key, path)

    hashes: dict[Path, str] = {}
    groups: dict[Path, list[Path]] = {}
    for path in paths:
        matched = DUP_SUFFIX_RE.match(path.stem)
        if not matched:
            continue
        base = matched.group(1).strip()
        if not base:
            continue
        original = originals.get((str(path.parent).lower(), base.lower(), path.suffix.lower()))
        if original is None or original == path:
            continue
        if not ignore_content:
            if path.stat().st_size != original.stat().st_size:
                continue
            for candidate in (path, original):
                if candidate not in hashes:
                    hashes[candidate] = _md5(candidate)
            if hashes[path] != hashes[original]:
                continue
        groups.setdefault(original, []).append(path)
    return sorted(groups.items())


def cmd_dups(root: Path, apply: bool, ignore_content: bool) -> int:
    groups = find_dups(walk_files(root), ignore_content)
    if not groups:
        print('没有找到重复文件。')
        return 0

    freed = 0
    total = 0
    for keep, dups in groups:
        print(f'保留 {keep}')
        for dup in dups:
            freed += dup.stat().st_size
            total += 1
            print(f'  删除 {dup}')
            if apply:
                dup.unlink()
    size_mb = round(freed / 1_048_576, 1)
    if apply:
        print(f'已删除 {total} 个重复文件，释放 {size_mb} MB。')
    else:
        print(f'预演：将删除 {total} 个重复文件，释放 {size_mb} MB。加 --apply 才会真正删除。')
    return 0


# ===== lrc：歌词与音频对照 =====

def scan_lyrics(paths: list[Path]) -> dict:
    """成对 / 缺少 lrc / 多余 lrc。判定与旧 .bat 一致（同目录 + 同主名）。"""
    audio = {(str(p.parent).lower(), p.stem.lower()): p for p in paths if p.suffix.lower() in AUDIO_EXTS}
    lrcs = [p for p in paths if p.suffix.lower() == '.lrc']
    lyrics = {(str(p.parent).lower(), p.stem.lower()) for p in lrcs}
    paired = sum(1 for key in audio if key in lyrics)
    missing = [path for key, path in audio.items() if key not in lyrics]
    orphan = [path for path in lrcs if (str(path.parent).lower(), path.stem.lower()) not in audio]
    return {'paired': paired, 'missing': sorted(missing), 'orphan': sorted(orphan)}


def write_listing(path: Path, header: str, items: list[Path]):
    """沿用旧 .bat 的格式：两行 '#' 表头 + 每行一个带引号的完整路径（CRLF、UTF-8）。

    旧 .bat 写出来的是 GBK 编码，这里统一成 UTF-8（插件与仓库其它地方都是 UTF-8）。
    """
    lines = [f'# {header}', '# 每行一个完整路径（带引号，可直接粘到资源管理器地址栏）']
    lines.extend(f'"{item}"' for item in items)
    path.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8')


def cmd_lrc(root: Path, apply: bool) -> int:
    result = scan_lyrics(walk_files(root))
    write_listing(root / MISSING_LRC_TXT, f'没有同名 .lrc 的音频文件（扫描根目录: {root}）', result['missing'])
    write_listing(root / ORPHAN_LRC_TXT, f'没有同名音频文件的 .lrc（扫描根目录: {root}）', result['orphan'])
    print(f'扫描完成: {root}')
    print(f"  成对        {result['paired']} 对")
    print(f"  缺少 lrc    {len(result['missing'])} 首  -> {MISSING_LRC_TXT}")
    print(f"  多余 lrc    {len(result['orphan'])} 个  -> {ORPHAN_LRC_TXT}")

    if not result['orphan']:
        print('\n没有多余的 .lrc，无需清理。')
        return 0
    if not apply:
        print(f'\n预演：{len(result["orphan"])} 个多余的 .lrc 将被移动到 {LRC_QUARANTINE}\\（不删除）。')
        print('加 --apply 才会真正移动。')
        return 0

    quarantine = root / LRC_QUARANTINE
    quarantine.mkdir(exist_ok=True)
    moved, kept = 0, []
    for path in result['orphan']:
        target = quarantine / path.name
        if target.exists():
            kept.append(path)
            continue
        shutil.move(str(path), str(target))
        moved += 1
    if kept:
        write_listing(quarantine / UNMOVED_LRC_TXT,
                      f'未能移动的 .lrc（{LRC_QUARANTINE} 里已有同名文件 / 移动失败）', kept)
    print(f'\n清理完成：移动 {moved} 个，未移动 {len(kept)} 个')
    print(f'  已移动到: {quarantine}（没有删除任何文件）')
    return 0


# ===== 自检 =====

def selftest() -> int:
    tmp = Path(tempfile.mkdtemp(prefix='media_tool_selftest_'))
    try:
        dup_dir = tmp / 'dup'
        dup_dir.mkdir()
        fixtures = {
            'a.flac': 'AAA', 'a (1).flac': 'AAA', 'a（2）.flac': 'AAA',   # 同名同内容 -> 删
            'b.lrc': 'BBB', 'b (1).lrc': 'BBBB',                        # 名字像但内容不同 -> 留
            'c (1).flac': 'CCC',                                        # 没有原文件 -> 留
            'd (Live).flac': 'DDD', 'd (Live) (1).flac': 'DDD',         # 嵌套括号 -> 删
        }
        for name, text in fixtures.items():
            (dup_dir / name).write_text(text, encoding='utf-8')
        groups = find_dups(sorted(dup_dir.iterdir()))
        deleted = sorted(dup.name for _, dups in groups for dup in dups)
        expected = sorted(['a (1).flac', 'a（2）.flac', 'd (Live) (1).flac'])
        if deleted != expected:
            raise AssertionError(f'dups 判定不符：{deleted} != {expected}')
        if len(groups) != 2:
            raise AssertionError(f'dups 分组数不符：{len(groups)} != 2')

        lrc_dir = tmp / 'lrc'
        lrc_dir.mkdir()
        for name in ('x.flac', 'x.lrc', 'y.flac', 'z.lrc'):
            (lrc_dir / name).write_text('data', encoding='utf-8')
        result = scan_lyrics(sorted(lrc_dir.iterdir()))
        if result['paired'] != 1 or [p.name for p in result['missing']] != ['y.flac'] \
                or [p.name for p in result['orphan']] != ['z.lrc']:
            raise AssertionError(f'lrc 判定不符：{result}')

        print('SelfTest OK（dups 3 个重复 / lrc 成对 1、缺 1、多 1）')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='library_tool.py',
        description='media-player 媒体库维护工具：专辑标签 / 重复文件 / 歌词对照',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', help='媒体库根目录；默认按 OmniBox 配置解析')
    parser.add_argument('--selftest', action='store_true', help='临时目录造用例自检，不碰媒体库')
    sub = parser.add_subparsers(dest='command')

    albums = sub.add_parser('albums', help='按插件缓存列被拆开的专辑，并按表补 ALBUM / ALBUMARTIST')
    albums.add_argument('--list', action='store_true', help='只列出被拆开的专辑组')
    albums.add_argument('--apply', action='store_true', help='真的写标签（默认只预览）')

    dups = sub.add_parser('dups', help='清理「名字只多一对括号后缀」且内容相同的重复文件')
    dups.add_argument('--apply', action='store_true', help='真的删除（默认只预览）')
    dups.add_argument('--ignore-content', action='store_true', help='只比名字，不比大小与 MD5')

    lrc = sub.add_parser('lrc', help='歌词与音频对照，并把多余的 .lrc 移到 _多余lrc\\')
    lrc.add_argument('--apply', action='store_true', help='真的移动（默认只预览，且从不删除）')

    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.command:
        parser.print_help()
        return 1

    root = resolve_library(args.root)
    if args.command == 'albums':
        return cmd_albums(root, args.apply, args.list)
    if args.command == 'dups':
        return cmd_dups(root['root'], args.apply, args.ignore_content)
    return cmd_lrc(root['root'], args.apply)


if __name__ == '__main__':
    _utf8_stdout()
    sys.exit(main())
