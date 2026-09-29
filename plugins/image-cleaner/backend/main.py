"""image-viewer 的 Companion 插件：全相册重复 / 相似图片清理。

设计参考 docs/plugin-guide.md §2.1 与 docs/image-cleaner-design.md。
该插件不修改 image-viewer 宿主，只通过依赖实例复用其数据根目录与删除能力。
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from shell.backend.media_catalog import IMAGE_EXTENSIONS
from shell.backend.plugin_base import PluginBase
from shell.backend.tasks import BackgroundTask

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS


class _ScanCancelled(Exception):
    """扫描被取消（后台 worker 的取消点抛出，由 `_scan_worker` 吞掉并放弃写结果）。"""


class ImageCleanerPlugin(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        # 只读信息行（`type: "info"`）：扫描的就是宿主「图片相册」的数据根目录，
        # 值由 get_settings() 现取，前端只展示、不提交（见 plugin_base.save_settings）。
        # 原先这一行显示在内嵌页自己的工具栏上，那一排已整条去掉。
        {"key": "root_dir", "label": "相册根目录", "type": "info",
         "help": "扫描范围取自「图片相册」的根目录；要换目录请到图片相册的设置里改"},
        {"key": "threshold", "label": "相似判定阈值", "type": "range",
         "min": 0, "max": 16, "default": 8,
         "help": "汉明距离越小越严格，0 表示只有完全一致的 dHash 才判为相似"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._host = None
        self._dhash_cache = {}
        self._dhash_cache_file = None
        # 后台扫描任务（共享基建 BackgroundTask）；同时只允许一个在跑
        self._scan_task: Optional[BackgroundTask] = None

    # ---------- 宿主访问与文件路由复用 ----------

    def _get_host(self):
        if self._host is None:
            self._host = self.get_dependency('image-viewer')
            if self._host is None:
                raise RuntimeError('image-cleaner 需要 image-viewer 插件已加载并声明依赖')
        return self._host

    def get_data_root(self) -> Path:
        return self._get_host().get_data_root()

    def get_file_roots(self) -> List[Path]:
        return self._get_host().get_file_roots()

    @property
    def thumb_dir(self) -> Path:
        return self._get_host().thumb_dir

    def ensure_thumb(self, rel_path: str) -> str:
        return self._get_host().ensure_thumb(rel_path)

    def get_thumb_data(self, rel_path: str):
        return self._get_host().get_thumb_data(rel_path)

    def register_api(self) -> dict:
        # `get_settings` / `save_settings` **必须登记**：壳的设置弹窗（宿主渲染那条与
        # 本页回落那条）走的都是 `Bridge.call('get_settings')` / `Bridge.call('save_settings')`
        # → `POST /api/image-cleaner__<方法>`，而 `PluginManager` 只会额外登记
        # `<插件>__get_settings_schema`（plugin_manager.py:566-572）。此前两个都漏了，
        # 表现是"弹窗打得开、root_dir 信息行空白、阈值恒为默认 8、保存必然失败"，
        # 且前端全程无报错。回归守卫见 tests/test_image_cleaner_settings.py。
        return {
            'duplicate_scan': self.duplicate_scan,
            'similar_scan': self.similar_scan,
            'scan_start': self.scan_start,
            'scan_status': self.scan_status,
            'scan_cancel': self.scan_cancel,
            'get_cached_scan': self.get_cached_scan,
            'delete_files': self.delete_files,
            'get_status': self.get_status,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    def get_status(self) -> Dict:
        host = self._get_host()
        roots = [str(root) for root, _prefix in self._scan_roots()]
        return {
            'host': host.name,
            'root_dir': roots[0] if roots else str(host.get_data_root()),
            'roots': roots,
            'scope': 'all',
        }

    def get_settings(self) -> Dict:
        """统一设置 + 只读信息行「相册根目录」。

        根目录不落在本插件的设置里 —— 它就是宿主 image-viewer 的根目录（主目录 + 额外
        图片目录），所以每次现取：用户在图片相册里改了根，设置弹窗里这一行跟着变。
        多根时逐行列出（`.field-info` 允许换行）。宿主不可用时留空字符串（信息行空着，
        但不能因此让整个设置弹窗打不开）。
        """
        settings = super().get_settings()
        try:
            settings['root_dir'] = '\n'.join(str(root) for root, _prefix in self._scan_roots())
        except RuntimeError:
            settings['root_dir'] = ''
        return settings

    def _scan_roots(self) -> list[tuple[Path, str]]:
        """扫描范围：宿主的全部根目录 + 各自的虚拟路径前缀（主目录在前）。

        结果是 `(物理根目录, 前缀)`：前缀取自宿主的 `list_roots()`（第一根为空串，额外根是
        `__<命名空间>`），扫描出的相对路径必须带上它，否则缩略图 / 原图 / 删除三条路由都会
        按**主目录**去解释，落到不存在的路径上。

        宿主没提供 `list_roots()`（旧版本），或**提供了但项里没有前缀**（早于 `prefix`
        字段的版本）时一律退回单根：后一种情况不能"前缀缺失就当空串"——那会把额外根里的
        文件按主根解释，除了 404，`delete_files()` 还会落到主根下的**另一个同名文件**上。
        退回单根的行为与加额外图库支持之前完全一致。
        """
        host = self._get_host()
        lister = getattr(host, 'list_roots', None)
        if not callable(lister):
            return [(host.get_data_root(), '')]
        roots: list[tuple[Path, str]] = []
        for index, item in enumerate(lister() or []):
            item = item or {}
            path = str(item.get('path') or '')
            if not path:
                continue
            prefix = str(item.get('prefix') or '')
            if index and not prefix:
                return [(host.get_data_root(), '')]
            roots.append((Path(path), prefix))
        return roots or [(host.get_data_root(), '')]

    def get_extensions(self) -> List[dict]:
        """注册到 image-viewer 左侧栏的通用扩展入口。"""
        return [{
            'host': 'image-viewer',
            'id': 'image-cleaner',
            'label': '相册清理',
            'icon': 'icon:brush-cleaning',
            'description': '扫描全部相册中的重复 / 相似图片',
            'section': '相册清理',  # 侧边栏独立分组标题（不与其他扩展挤在一个标题下）
            'embedUrl': '/plugins/image-cleaner/frontend/index.html',
            'placement': 'sidebar',
            'scope': 'all',
        }]

    # ---------- 扫描结果缓存 ----------

    def _scan_cache_path(self) -> Path:
        return self._get_host().get_data_root() / '.cache' / 'image-cleaner' / 'scan_cache.json'

    def _load_scan_cache(self) -> dict:
        try:
            path = self._scan_cache_path()
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_scan_cache(self, cache: dict):
        try:
            path = self._scan_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception as e:
            log.error(f"[{self.name}] 保存扫描结果缓存失败: {e}")

    def _save_scan_result(self, mode: str, groups: list, scanned: int):
        cache = self._load_scan_cache()
        cache[mode] = {
            'groups': groups,
            'scanned': scanned,
            'saved_at': __import__('time').time(),
        }
        self._save_scan_cache(cache)

    def get_cached_scan(self, mode: str) -> Dict:
        """返回上次扫描结果；没有缓存时返回空结果。"""
        if mode not in ('dupe', 'similar'):
            return {'groups': [], 'scanned': 0, 'cached': False}
        cache = self._load_scan_cache()
        item = cache.get(mode)
        if not item or not isinstance(item, dict):
            return {'groups': [], 'scanned': 0, 'cached': False}
        return {
            'groups': item.get('groups', []),
            'scanned': item.get('scanned', 0),
            'cached': True,
        }

    # ---------- 全相册文件收集 ----------

    def _all_album_files(self) -> list:
        """递归扫描全部相册（宿主的主目录 + 额外图片目录），跳过隐藏目录和 .cache。

        每个根的相对路径都要带上宿主的虚拟前缀（见 `_scan_roots`）：图片相册把额外图库
        当相册展示，清理必须扫到它，否则额外图库里的重复、以及"主目录一份 + 额外图库
        一份"这种跨根重复永远找不出来。

        **同一个物理文件只收一条**：额外根可以落在主根之内（宿主的 `_extra_roots()`
        只排除完全相同的路径，不排除嵌套），那样一个文件会有两条虚拟路径
        （`备份/a.jpg` 与 `__备份 (2)/a.jpg`）。不收手的话它们会被判成"两张完全重复的
        图片"——"只留一张"之后的删除会对同一个文件 unlink 两次（第二次报"部分删除失败"），
        "全选组"删除更会把唯一副本一起删掉。保留先出现的条目，即主根的虚拟路径。
        """
        files = []
        seen: set[str] = set()
        for root, prefix in self._scan_roots():
            try:
                if not root.exists() or not root.is_dir():
                    continue
                for current, dir_names, filenames in os.walk(root):
                    dir_names[:] = [d for d in dir_names
                                    if not d.startswith('.') and d != '.cache']
                    current_path = Path(current)
                    try:
                        rel_dir = current_path.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    for name in filenames:
                        if name.startswith('.'):
                            continue
                        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
                            continue
                        abs_path = current_path / name
                        # 去重键：规范化大小写与 . / .. 之后的绝对路径。两个根在宿主侧
                        # 都已 resolve()（root_dir 与 _extra_roots），嵌套时两种走法得到
                        # 同一字符串，因此这里不需要再碰一次文件系统。
                        key = os.path.normcase(os.path.abspath(str(abs_path)))
                        if key in seen:
                            continue
                        try:
                            stat = abs_path.stat()
                        except OSError:
                            continue
                        seen.add(key)
                        rel_in_root = (Path(rel_dir) / name).as_posix() if rel_dir else name
                        files.append({
                            'rel': f'{prefix}/{rel_in_root}' if prefix else rel_in_root,
                            'abs': str(abs_path),
                            'size': stat.st_size,
                            'mtime': stat.st_mtime,
                        })
            except OSError:
                continue
        return files

    # ---------- 完全重复 ----------

    @staticmethod
    def _file_quick_hash(abs_path: str, size: int) -> Optional[str]:
        """先读首尾各 64KB + 文件大小做快速指纹，避免整文件 MD5。

        读取失败返回 None（而不是"空内容的 md5"，见 _file_full_hash 的说明）。
        """
        h = hashlib.md5()
        h.update(str(size).encode())
        try:
            with open(abs_path, 'rb') as f:
                head = f.read(65536)
                if len(head) == 0 and size > 0:
                    # 声明有内容却读不出任何字节：文件正在被写入/被占用/权限不足
                    return None
                h.update(head)
                if size > 131072:
                    f.seek(max(0, size - 65536))
                    h.update(f.read(65536))
        except OSError:
            return None
        return h.hexdigest()

    @staticmethod
    def _file_full_hash(abs_path: str) -> Optional[str]:
        """整文件 MD5；**任何读取失败都返回 None，绝不返回"空内容的摘要"**。

        历史缺陷：失败时 `pass` 后照样返回 hashlib.md5() 的空摘要
        （d41d8cd98f00b204e9800998ecf8427e），于是所有读不到的文件共享同一个
        digest，被 duplicate_scan 当成"完全重复"分组。而这个插件的下一步是
        delete_files → unlink：一组"假重复"被一键删除 = 真实照片永久丢失。
        文件在扫描期间被写入（同步/复制进行中）是最容易触发的场景。

        额外做一致性校验：读取过程中文件大小变化、或**实际读到的字节数与
        声明大小不符**（正在被写入、被截断），摘要都不可信，同样返回 None。
        """
        h = hashlib.md5()
        read_bytes = 0
        try:
            with open(abs_path, 'rb') as f:
                before = os.fstat(f.fileno()).st_size
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    read_bytes += len(chunk)
                    h.update(chunk)
                after = os.fstat(f.fileno()).st_size
        except OSError:
            return None
        if before != after or read_bytes != before:
            return None
        return h.hexdigest()

    def duplicate_scan(self) -> Dict:
        """扫描全部相册，返回跨相册的完全重复图片分组（同步入口：后台任务与用例共用分组逻辑）。"""
        files = self._all_album_files()
        if not files:
            return {'groups': [], 'scanned': 0}

        scanned = len(files)
        groups = self._duplicate_groups(files)
        self._save_scan_result('dupe', groups, scanned)
        return {'groups': groups, 'scanned': scanned}

    # ---------- 分组计算（同步入口与后台 worker 共用同一份实现） ----------

    @staticmethod
    def _check_cancel(task: Optional['BackgroundTask']) -> None:
        """后台任务的取消点：取消时抛出 `_ScanCancelled`，由 worker 吞掉并放弃写结果。"""
        if task is not None and task.cancelled:
            raise _ScanCancelled()

    def _duplicate_groups(self, files: list,
                          task: Optional['BackgroundTask'] = None) -> List[dict]:
        """完全重复分组（`task` 非空时上报进度并响应取消）。

        进度以**文件**为单位：`total` = 本次扫描的文件数，`processed` = 已定论的文件数
        （尺寸唯一或快速指纹不一致的在早期阶段就定论，指纹一致的必须算完完整摘要才定论），
        因此进度单调、结束时正好走到 `total`。
        """
        by_size: Dict[int, list] = {}
        for f in files:
            by_size.setdefault(f['size'], []).append(f)

        progressed = 0
        if task is not None:
            task.update(total=len(files), processed=0, current='')

        groups = []
        for size, batch in by_size.items():
            if len(batch) < 2:
                progressed += len(batch)
                if task is not None:
                    task.update(processed=progressed)
                continue
            by_quick: Dict[str, list] = {}
            for f in batch:
                self._check_cancel(task)
                key = self._file_quick_hash(f['abs'], f['size'])
                if key is None:
                    progressed += 1  # 读不到的文件不参与比较，更不能参与删除
                    continue
                by_quick.setdefault(key, []).append(f)
            for candidates in by_quick.values():
                if len(candidates) < 2:
                    progressed += len(candidates)   # 快速指纹已足以定论
                    if task is not None:
                        task.update(processed=progressed)
                    continue
                by_full: Dict[str, list] = {}
                for f in candidates:
                    self._check_cancel(task)
                    digest = self._file_full_hash(f['abs'])
                    progressed += 1
                    if task is not None:
                        task.update(processed=progressed, current=Path(f['rel']).name)
                    if digest is None:
                        continue  # 同上：摘要不可信就退出候选集
                    by_full.setdefault(digest, []).append(f)
                for digest, dups in by_full.items():
                    if len(dups) >= 2:
                        groups.append({
                            'hash': digest,
                            'size': size,
                            'files': [d['rel'] for d in dups],
                        })
        groups.sort(key=lambda g: -len(g['files']))
        return groups

    # ---------- 视觉相似 ----------

    def _dhash_cache_path(self) -> Path:
        if self._dhash_cache_file is None:
            self._dhash_cache_file = self._get_host().get_data_root() / '.cache' / 'image-cleaner' / 'dhash.json'
        return self._dhash_cache_file

    def _load_dhash_cache(self):
        if self._dhash_cache:
            return
        try:
            path = self._dhash_cache_path()
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._dhash_cache = data
        except Exception:
            self._dhash_cache = {}

    def _save_dhash_cache(self):
        try:
            path = self._dhash_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._dhash_cache, f, ensure_ascii=False)
        except Exception as e:
            log.error(f"[{self.name}] 保存 dHash 缓存失败: {e}")

    def _image_dhash(self, abs_path: str, mtime: float) -> Optional[int]:
        """64-bit 差异哈希（dHash），结果写入 image-cleaner 自己的缓存。

        **读图失败返回 `None`，与"哈希值恰好是 0"分开**：纯色/近纯色图的 dHash 本来
        就是 0，而旧实现让失败也返回 0、调用方又用 `if h:` 过滤 —— 两种含义混在一起，
        纯色图（以及任何 dHash 为 0 的图）永远进不了相似分组。失败同样**不写缓存**，
        否则一次读失败会被缓存钉住（mtime 不变就永远不再重算）。

        缓存条目带 `'v': 2`：旧版把"读图失败"也作为 hash 0 存进缓存，缺版本号的条目
        一律重算，避免那些 0 被当成合法哈希继续参与分组。
        """
        key = 'dhash:' + hashlib.md5(abs_path.encode()).hexdigest()
        self._load_dhash_cache()
        cached = self._dhash_cache.get(key)
        if cached and cached.get('v') == 2 and cached.get('mtime') == mtime:
            return int(cached.get('hash', 0))

        try:
            from PIL import Image
            with Image.open(abs_path) as img:
                small = img.convert('L').resize((9, 8))
            pixels = list(small.getdata())
        except Exception:
            return None

        value = 0
        for row in range(8):
            for col in range(8):
                value <<= 1
                if pixels[row * 9 + col] > pixels[row * 9 + col + 1]:
                    value |= 1
        self._dhash_cache[key] = {'v': 2, 'mtime': mtime, 'hash': value}
        return value

    def similar_scan(self, threshold: int | None = None) -> Dict:
        """扫描全部相册，返回跨相册的视觉相似图片分组（同步入口：后台任务与用例共用分组逻辑）。"""
        files = self._all_album_files()
        if len(files) < 2:
            return {'groups': [], 'scanned': len(files)}

        hashes, valid = self._dhash_all(files)
        groups = self._similar_groups(hashes, valid, threshold)
        self._save_dhash_cache()
        self._save_scan_result('similar', groups, len(valid))
        return {'groups': groups, 'scanned': len(valid)}

    def _resolve_threshold(self, threshold: Optional[int]) -> int:
        if threshold is None:
            threshold = self.setting('threshold', 8)
        return max(0, min(16, int(threshold)))

    def _dhash_all(self, files: list, task: Optional['BackgroundTask'] = None) -> tuple:
        """算出每张图的 dHash，返回 `(hashes, valid)`；读不了的图不进 `valid`。

        `task` 非空时上报第一段进度：`total` = 本次扫描的文件数、`processed` = 已算完的
        文件数。第二段（两两比较）由 `_similar_groups` 另开分母与阶段文案。
        """
        if task is not None:
            task.update(total=max(1, len(files)), processed=0, current='计算相似指纹')
        hashes: List[int] = []
        valid: List[dict] = []
        for index, item in enumerate(files):
            self._check_cancel(task)
            digest = self._image_dhash(item['abs'], item['mtime'])
            if task is not None:
                task.update(processed=index + 1, current=Path(item['rel']).name)
            if digest is None:
                continue          # 读不了的不参与相似判定（0 是合法哈希，不能兼表失败）
            hashes.append(digest)
            valid.append(item)
        return hashes, valid

    def _similar_groups(self, hashes: List[int], valid: list,
                        threshold: Optional[int],
                        task: Optional['BackgroundTask'] = None) -> List[dict]:
        """dHash 并查集聚类（`task` 非空时上报进度并响应取消）。

        两两比较是 O(n²)，进度按**外层下标**推进。这是扫描的第二段，分母是有效图片数
        （与第一段"文件数"不同），前端按 `current` 里的阶段文字区分，不需要把两段
        凑成一个百分比。
        """
        threshold = self._resolve_threshold(threshold)
        n = len(valid)
        if task is not None:
            task.update(total=max(1, n), processed=0, current='比较相似度')
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            self._check_cancel(task)
            hi = hashes[i]
            for j in range(i + 1, n):
                # 尺寸差异过大不可能是相似图
                if max(valid[i]['size'], valid[j]['size']) > min(valid[i]['size'], valid[j]['size']) * 6:
                    continue
                if (hi ^ hashes[j]).bit_count() <= threshold:
                    union(i, j)
            if task is not None:
                task.update(processed=i + 1)

        clusters: Dict[int, List[str]] = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(valid[i]['rel'])
        groups = [{'files': sorted(paths)} for paths in clusters.values() if len(paths) >= 2]
        groups.sort(key=lambda g: -len(g['files']))
        return groups

    # ---------- 删除 ----------

    def delete_files(self, rel_paths: List[str]) -> Dict:
        """复用 image-viewer 的删除接口，保持路径安全与宿主缓存清理一致。"""
        return self._get_host().delete_files(rel_paths)

    # ---------- 后台扫描任务（共享基建 shell.backend.tasks） ----------

    def scan_start(self, mode: str = 'dupe', threshold: Optional[int] = None) -> Dict:
        """把整库扫描放进后台任务（共享基建 `BackgroundTask`）。

        为什么需要：全库扫描要为每张图算指纹（相似模式还要两两比较），大图库上是分钟级
        的；放在同步 API 里既没有进度也不能取消，用户只能看着"正在扫描…"。image-viewer
        的缩略图重建（`rebuild_all` / `rebuild_status` / `rebuild_cancel`）用的是同一套骨架。

        结果仍写进既有的扫描缓存（`_save_scan_result`），前端在任务结束后用
        `get_cached_scan(mode)` 取 —— 与"重进页面直接读缓存"是同一条路径，不额外搬运数据。
        任务状态**不落盘**：本插件没有"断点续跑"语义，重启后重新扫描即可（结果缓存仍在）。
        """
        mode = 'similar' if str(mode) == 'similar' else 'dupe'
        if self._scan_task is not None and self._scan_task.state == 'running':
            return {'started': False, 'running': True, **self.scan_status()}
        task = BackgroundTask(kind='image-cleaner-scan', extra={'mode': mode, 'groups': 0})
        task.start(self._scan_worker, args=(mode, threshold))
        self._scan_task = task
        return {'started': True, 'running': True, 'mode': mode}

    def _scan_worker(self, task: 'BackgroundTask', mode: str, threshold: Optional[int]) -> None:
        """后台扫描 worker：算分组 → 写扫描缓存；取消时不写任何结果。"""
        try:
            files = self._all_album_files()
            self._check_cancel(task)
            if mode == 'similar':
                hashes, valid = self._dhash_all(files, task)
                groups = self._similar_groups(hashes, valid, threshold, task)
                scanned = len(valid)
            else:
                groups = self._duplicate_groups(files, task)
                scanned = len(files)
        except _ScanCancelled:
            return
        self._save_scan_result(mode, groups, scanned)
        task.update(current='', extra={'groups': len(groups), 'scanned': scanned})

    def scan_status(self) -> Dict:
        """后台扫描任务的进度（`BackgroundTask.status()` 的投影）。"""
        task = self._scan_task
        if task is None:
            return {'running': False, 'done': False, 'success': False, 'cancelled': False,
                    'mode': '', 'total': 0, 'processed': 0, 'current': '', 'groups': 0}
        status = task.status()
        return {
            'running': status['running'],
            'done': status['done'],
            'success': status['success'],
            'cancelled': status['cancelled'],
            'mode': status['extra'].get('mode', ''),
            'total': status['total'],
            'processed': status['processed'],
            'current': status['current'],
            'groups': int(status['extra'].get('groups', 0) or 0),
        }

    def scan_cancel(self) -> Dict:
        """请求取消当前扫描；取消的任务不写结果缓存（没有"半份结果"）。"""
        if self._scan_task is not None and self._scan_task.state == 'running':
            self._scan_task.cancel()
            return {'success': True}
        return {'success': False, 'error': '没有正在运行的扫描任务'}

    def on_unload(self) -> None:
        """进程退出收尾：取消正在跑的扫描任务（已写出的结果缓存保留）。"""
        if self._scan_task is not None and self._scan_task.state == 'running':
            self._scan_task.cancel()
