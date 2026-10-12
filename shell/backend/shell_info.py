'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

壳自身的信息与运维入口：本机路径、日志级别、缩略图缓存占用与清理。

这里**不放插件设置**：插件的设置项由插件自己的设置弹窗维护
（`/api/<插件>__get_settings` / `__save_settings`），壳只提供展示与清理所需的
系统级事实（`/api/system_get_shell_info` 等）。

为什么日志级别要落盘：它必须在下一次启动时、界面出现之前生效，所以只能存在后端
读得到的地方 —— `<config_dir>/shell.json`（复用 SettingsStore 的原子写入）。
唯一的写者就是本模块（插件写各自的 `<插件名>.json`），因此不存在"两个
SettingsStore 实例各持一把锁写同一个文件"的时间窗。
'''

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from shell.backend.app_logging import get_log_dir
from shell.backend.media_catalog import classify_file, list_directory
from shell.backend.paths import get_config_dir
from shell.backend.settings_store import SettingsStore
from shell.backend.thumb_cache import cache_stats, clear_all_caches

log = logging.getLogger(__name__)

# 壳自身设置的文件名（SettingsStore 会拼成 <config_dir>/shell.json）
SHELL_SETTINGS_NAME = 'shell'
LOG_FILE_NAME = 'omnibox.log'
# 只留四档：DEBUG 会为每张缩略图记一行，量级与 INFO 差一个数量级，够排查就停。
LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')

# WebView2 高性能 GPU 模式：把 `--force_high_performance_gpu` 交给 WebView2 的 GPU 进程。
# 按可执行文件走的图形首选项（Windows 设置的「高性能」、NvOptimusEnablement）落的是主进程，
# 够不到 WebView2 的 GPU 进程，只能走浏览器参数。上游说明：
# https://github.com/MicrosoftEdge/WebView2Feedback/issues/5072
# 实际下发在 shell.backend.webview_gpu（必须走 pywebview 的 API）。
WEBVIEW_HIGH_PERFORMANCE_SETTING = 'webview_high_performance'
# 逃生口，优先级高于 shell.json：该参数失效时窗口可能无法创建，此时界面不可用，
# 只有启动前就生效的来源能关掉它。
WEBVIEW_HIGH_PERFORMANCE_ENV = 'OMNIBOX_WEBVIEW_HIGH_PERFORMANCE'
_WEBVIEW_HIGH_PERFORMANCE_OFF = ('0', 'false', 'no', 'off')


#: 按配置目录复用的 SettingsStore（见 `_store`）。
_stores: Dict[str, SettingsStore] = {}
_stores_lock = threading.Lock()


def _store(config_dir: Optional[str] = None) -> SettingsStore:
    """取该配置目录的 SettingsStore，同一个目录只建一次。

    构造函数会 `mkdir`，而本模块的读取落在启动路径与每次打开设置页上：复用省掉重复
    的目录创建。复用同一个实例也顺带加强了本模块的写入前提 —— 每次读都新建实例时，
    同一个设置文件可能存在多把锁。
    """
    key = str(Path(config_dir) if config_dir else get_config_dir())
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = SettingsStore(key)
            _stores[key] = store
        return store


def stored_log_level(config_dir: Optional[str] = None) -> int:
    """启动时该用的日志级别：读已保存的值，缺失或非法时回退 INFO。"""
    name = _store(config_dir).get(SHELL_SETTINGS_NAME).get('log_level')
    if isinstance(name, str) and name.upper() in LOG_LEVELS:
        return getattr(logging, name.upper())
    return logging.INFO


def stored_webview_high_performance(config_dir: Optional[str] = None) -> bool:
    """已保存的高性能 GPU 模式；缺失或非布尔时回退**开启**（默认下发该浏览器参数）。

    只认真正的布尔值：手改 shell.json 写成字符串 'false' 不该被 `bool()` 判成真。
    """
    value = _store(config_dir).get(SHELL_SETTINGS_NAME).get(WEBVIEW_HIGH_PERFORMANCE_SETTING)
    if isinstance(value, bool):
        return value
    return True


# 背景图库：用户选中的图片被**复制**到 `<config_dir>/backgrounds`，壳级 `/file`
# 请求把这个目录并入允许根（见 file_server.serve_media_file）。
#
# 为什么是复制而不是引用原路径：引用原路径要求把用户目录配成 `/file` 的允许根，
# 一个写错的条目（尤其 `..`）就等于把这些目录里的文件按原路径开放给**所有**持令牌
# 主体（含局域网成员）；而且原图被移动或删除后背景图会静默失效。复制进库目录之后，
# 放行范围固定为这一个目录，图片也能在设置页里切换与删除。
BACKGROUND_LIBRARY_DIRNAME = 'backgrounds'
# 上限只防"配置失控"：图片库过大时设置页的网格会长到无法使用，而与能力无关。
MAX_BACKGROUND_IMAGES = 300
# 单张上限：选错文件（例如把几十 GB 的视频改名成 .jpg）时不必先整份复制进 .config。
MAX_BACKGROUND_IMAGE_BYTES = 64 * 1024 * 1024
# Windows 文件名的保留字符与控制字符：源文件名直接拿来当库内文件名会写不进去。
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def background_library_dir(config_dir: Optional[str | Path] = None) -> Path:
    """背景图库目录：`<config_dir>/backgrounds`。

    目录不在这里创建：没有导入过图片的运行期不该在 `.config` 下多一个空目录。
    """
    base = Path(config_dir) if config_dir else get_config_dir()
    return base / BACKGROUND_LIBRARY_DIRNAME


def _safe_image_name(name: str) -> str:
    """把源文件名收敛成库内可用的文件名（去掉目录部分、保留小写扩展名）。"""
    raw = Path(str(name)).name
    stem = _UNSAFE_NAME_CHARS.sub('_', Path(raw).stem).strip(' .')[:60] or 'background'
    return f'{stem}{Path(raw).suffix.lower()}'


def _unique_target(folder: Path, name: str) -> Path:
    """库内不重名的目标路径：同名的第二张图片变成 `name-2.jpg`。"""
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = folder / name
    index = 2
    while candidate.exists() and index < 10000:
        candidate = folder / f'{stem}-{index}{suffix}'
        index += 1
    return candidate


def webview_high_performance_enabled(config_dir: Optional[str] = None) -> bool:
    """最终取值：环境变量优先于 shell.json。

    环境变量只用于关闭，不作开启：只覆盖一个方向，两个来源不会互相翻转。
    """
    override = os.environ.get(WEBVIEW_HIGH_PERFORMANCE_ENV)
    if isinstance(override, str) and override.strip().lower() in _WEBVIEW_HIGH_PERFORMANCE_OFF:
        return False
    return stored_webview_high_performance(config_dir)


def configure_webview_environment(config_dir: Optional[str] = None) -> bool:
    """按当前设置决定是否下发 GPU 适配器偏好，返回是否启用。

    这里只管**决策与日志**；真正把参数交给 WebView2 的是
    `shell.backend.webview_gpu.apply_gpu_preference`（必须走 pywebview 的 API，
    环境变量在默认启动路径上会被忽略，理由见该模块的说明）。
    """
    if not webview_high_performance_enabled(config_dir):
        log.info('[OmniBox] WebView2 高性能 GPU 模式已关闭'
                 f'（{WEBVIEW_HIGH_PERFORMANCE_SETTING}=false 或 {WEBVIEW_HIGH_PERFORMANCE_ENV} 置 0）')
        return False
    return True


class ShellInfo:
    """壳自身信息 / 运维动作的实现（端点注册见 file_server 与 main.py）。"""

    def __init__(self, config: Optional[dict] = None, config_dir: Optional[str] = None):
        self._config = config or {}
        self._config_dir = Path(config_dir) if config_dir else get_config_dir()
        self._store = _store(str(self._config_dir))

    def get_info(self) -> dict:
        """集中设置页要展示的系统事实：路径 + 日志级别 + 缓存占用。"""
        log_dir = get_log_dir(self._config_dir)
        caches = cache_stats()
        return {
            'data_root': str(self._config.get('directories', {}).get('data_root', '')),
            'config_dir': str(self._config_dir),
            'log_dir': str(log_dir),
            'log_file': str(log_dir / LOG_FILE_NAME),
            'log_level': logging.getLevelName(logging.getLogger().level),
            # 保存值与**生效值**分开返回：生效值可能被环境变量覆盖为关闭，只回一个
            # 则界面会显示为已开启而实际未生效，且无依据说明原因。
            'webview_high_performance': stored_webview_high_performance(str(self._config_dir)),
            'webview_high_performance_effective': webview_high_performance_enabled(str(self._config_dir)),
            'webview_high_performance_env': WEBVIEW_HIGH_PERFORMANCE_ENV,
            'caches': caches,
            'cache_total': {
                'count': sum(item['count'] for item in caches),
                'bytes': sum(item['bytes'] for item in caches),
            },
        }

    def set_webview_high_performance(self, enabled) -> dict:
        """保存高性能 GPU 模式。**下次启动生效** —— 参数只在 WebView2 控件构造时注入。"""
        if not isinstance(enabled, bool):
            return {'success': False, 'error': f'取值必须是布尔值，实际是 {type(enabled).__name__}'}
        self._store.update(SHELL_SETTINGS_NAME, {WEBVIEW_HIGH_PERFORMANCE_SETTING: enabled})
        log.info(f'[ShellInfo] WebView2 高性能 GPU 模式已保存为 {enabled}，下次启动生效')
        return {
            'success': True,
            'webview_high_performance': enabled,
            'webview_high_performance_effective': webview_high_performance_enabled(str(self._config_dir)),
            'restart_required': True,
        }

    def set_log_level(self, level) -> dict:
        """改日志级别：先落盘再生效，重启后仍是这一档。"""
        name = str(level or '').strip().upper()
        if name not in LOG_LEVELS:
            return {'success': False, 'error': f'不支持的日志级别: {level!r}'}
        self._store.update(SHELL_SETTINGS_NAME, {'log_level': name})
        logging.getLogger().setLevel(getattr(logging, name))
        log.info(f'[ShellInfo] 日志级别已改为 {name}')
        return {'success': True, 'level': name}

    def clear_thumb_caches(self) -> dict:
        """清空所有插件的缩略图缓存（下次浏览时按需重生成）。"""
        result = clear_all_caches()
        log.info(f"[ShellInfo] 已清空 {result['caches']} 个缩略图缓存，释放 {result['freed_bytes']} 字节")
        return {'success': True, **result}

    # ===== 背景图库 =====
    # 选中的图片被复制进 `<config_dir>/backgrounds`：壳级 `/file` 只额外放行这一个
    # 目录（见 file_server.serve_media_file），用户原图所在目录不再进入放行范围。

    def list_background_images(self) -> List[dict]:
        """列出图片库里的图片，最近导入的排在前面。

        目录枚举复用 `/file` 与相册扫描共用的 `media_catalog.list_directory`：
        隐藏文件过滤与"什么算图片"（`classify_file`）只在那一处定义。只列图片与
        `/file` 的图片根判定保持一致（`_match_file_root` 的 image_only_roots）。
        """
        folder = background_library_dir(str(self._config_dir))
        listing = list_directory(folder, '')
        images = [
            {
                'name': entry['name'],
                'path': str(folder / entry['name']),
                'size': entry.get('size', 0),
                'mtime': entry.get('mtime', 0),
            }
            for entry in listing.get('files', [])
            if entry.get('kind') == 'image'
        ]
        images.sort(key=lambda item: (-item['mtime'], item['name'].lower()))
        return images[:MAX_BACKGROUND_IMAGES]

    def import_background_image(self, source) -> dict:
        """把用户选中的图片复制进图片库，返回库内条目。

        只接受**图片**（按扩展名判定，与 `/file` 的图片根同一份 `classify_file`）：
        这个端点在 `_ADMIN_ONLY_API` 里，但"管理员"仍然不该能把任意文件搬进一个
        随后由 `/file` 对外发布的目录。
        """
        if not isinstance(source, str) or not source.strip():
            return {'success': False, 'error': '没有选择图片文件'}
        src = Path(source).expanduser()
        if not src.is_absolute():
            return {'success': False, 'error': f'只接受绝对路径: {source}'}
        if not src.is_file():
            return {'success': False, 'error': f'文件不存在: {source}'}
        if classify_file(src.name) != 'image':
            return {'success': False, 'error': f'不是图片文件: {src.name}'}
        try:
            size = src.stat().st_size
        except OSError as e:
            return {'success': False, 'error': f'读取文件失败: {e}'}
        if size > MAX_BACKGROUND_IMAGE_BYTES:
            limit = MAX_BACKGROUND_IMAGE_BYTES // (1024 * 1024)
            return {'success': False, 'error': f'图片超过 {limit} MB'}
        folder = background_library_dir(str(self._config_dir))
        folder.mkdir(parents=True, exist_ok=True)
        if len(self.list_background_images()) >= MAX_BACKGROUND_IMAGES:
            return {'success': False, 'error': f'最多 {MAX_BACKGROUND_IMAGES} 张背景图'}
        target = _unique_target(folder, _safe_image_name(src.name))
        try:
            shutil.copy2(src, target)
        except OSError as e:
            return {'success': False, 'error': f'复制图片失败: {e}'}
        log.info(f'[ShellInfo] 背景图已导入 {target.name}（{size} 字节）')
        return {
            'success': True,
            'image': {'name': target.name, 'path': str(target), 'size': size},
        }

    def delete_background_image(self, target) -> dict:
        """删除图片库里的一张图片。

        只接受**库目录的直接子项**：入参若能拼到库外（`..\\auth_token.txt`），这个
        端点就成了"删除本机任意文件"的入口。`resolve()` 之后再比较父目录，库内的
        符号链接指向库外时同样被拒。
        """
        if not isinstance(target, str) or not target.strip():
            return {'success': False, 'error': '没有指定要删除的图片'}
        folder = background_library_dir(str(self._config_dir)).resolve()
        candidate = Path(target).expanduser()
        if not candidate.is_absolute():
            candidate = folder / candidate
        try:
            resolved = candidate.resolve()
        except OSError as e:
            return {'success': False, 'error': f'解析路径失败: {e}'}
        if resolved.parent != folder or not resolved.is_file():
            return {'success': False, 'error': f'不在背景图库里: {target}'}
        try:
            resolved.unlink()
        except OSError as e:
            return {'success': False, 'error': f'删除图片失败: {e}'}
        log.info(f'[ShellInfo] 背景图已删除 {resolved.name}')
        return {'success': True, 'name': resolved.name}

    def open_log_dir(self) -> dict:
        """在文件管理器里打开日志目录。

        路径固定为 `<config_dir>/logs`，不接受调用方传入 —— 一旦能打开任意路径，
        这个端点就成了"用受信任的壳去启动任意程序 / 目录"的入口。
        """
        path = get_log_dir(self._config_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            opener = getattr(os, 'startfile', None)  # 仅 Windows 存在
            if opener is not None:
                opener(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as e:
            log.error(f'[ShellInfo] 打开日志目录失败: {e}')
            return {'success': False, 'error': str(e)}
        return {'success': True}
