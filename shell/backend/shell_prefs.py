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

壳偏好（启动页 / 启动全屏 / 上次页面 / 主题 / 自定义色 / 背景 / 玻璃 / 分隔条布局）
统一落在 `<config_dir>/shell.json` 的 `prefs` 段。

为什么不放 localStorage：桌面窗口的 localStorage 在 WebView2 profile 的 LevelDB 里，
日志一旦被强杀截断，Chromium 会在坏记录处停止回放，其后的全部写入在下次启动都读不到
（表现为"设置存得进、重启就忘"，且没有任何报错）。壳偏好的读路径因此改为服务端注入
（`file_server.serve_shell` 返回 index.html 时写 `window.__OMNIBOX_SHELL_STATE__`），
写路径走 `system_set_shell_prefs`。

校验分三层：

- 文件级：JSON 坏或不是对象 —— 交给 `SettingsStore._quarantine`，改名留档 + 回退默认；
- 键级：类型或取值非法 —— 该键回退默认并记 WARNING，其余键照常生效；
- 语义级：`startup.route` 指向已删除的插件 —— 判在前端（插件清单只有前端知道），
  这里只存与校验形状。

`_VAR_NAME_RE` / `_VAR_VALUE_MAX` 必须与前端 `shell/frontend/src/core/appearance.ts`
的 `isValidVarName` / `MAX_VALUE_LEN` 一致：外观是**开放式键集**（壳的颜色变量、
分隔条布局变量、插件贡献的变量混在同一张表里），后端不能写成固定键的 schema，
只能校验"名字形状 + 取值形状"。两处一致性由 `tests/test_shell_prefs.py` 与
`tests/js/shell_prefs.mjs` 各锁一份用例。
'''

import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from shell.backend.shell_info import SHELL_SETTINGS_NAME, _store

log = logging.getLogger(__name__)

#: `shell.json` 里存放壳偏好的键。日志级别与 WebView2 高性能开关是同一文件里的平级键
#: （`log_level` / `webview_high_performance`），因此这里只做**局部更新**，绝不整份覆写。
PREFS_KEY = 'prefs'

THEMES = ('light', 'dark')
START_PAGES = ('last', 'first', 'settings', 'plugin')
FITS = ('cover', 'contain', 'fill', 'none', 'free')

#: CSS 自定义属性名：与前端 appearance.ts 的 VAR_NAME_RE 同一个正则。
_VAR_NAME_RE = re.compile(r'^--[a-z0-9-]{1,40}$')
#: 取值上限：与前端 MAX_VALUE_LEN 一致。
_VAR_VALUE_MAX = 64
#: 变量表条数上限：键集开放，不设上限时一份畸形请求就能让 shell.json 无限增长。
_MAX_VARS = 400
#: 路由与路径的长度上限：只防畸形数据。
_MAX_ROUTE_LEN = 512
#: 一次 patch 的 JSON 规模上限：正常的局部更新远小于它。
_MAX_PATCH_BYTES = 262144

#: 玻璃两个变量的单位与取值范围（与前端 GLASS_LIMITS 的 0-40px / 30-100% 对齐）。
_GLASS_UNITS = {'--glass-blur': 'px', '--glass-alpha': '%'}
_GLASS_VAR_RANGES = {'--glass-blur': (0, 40), '--glass-alpha': (30, 100)}
_INT_VALUE_RE = re.compile(r'^-?\d{1,5}$')

BG_LIMITS = {'zoom': (10, 400), 'posX': (0, 100), 'posY': (0, 100)}

#: `last_route` 只记**安全退出**的：前端每次路由变更只把值放内存
#: （`set_last_route`），由 `commit_last_route()` 在正常退出路径或空闲兜底时落盘。
_pending_routes: Dict[str, str] = {}
_pending_lock = threading.Lock()
#: 落盘的串行化锁：`commit_last_route` 可能被关窗退出与空闲定时器同时触发
_commit_lock = threading.Lock()

#: `--web-only` / 浏览器访问没有"关窗事件"，靠空闲兜底提交：最后一次上报之后静置
#: 这么久就落盘（浏览器模式的历史因此会更新，而每次切路由都不必写一次 fsync）。
IDLE_COMMIT_SECONDS = 20.0


def _log_bad(key: str, value: Any, reason: str) -> None:
    """键级校验未通过：该键回退默认，其余键继续生效。"""
    log.warning(f'[ShellPrefs] 忽略非法取值 {key}={value!r}（{reason}），该键回退默认值')


def is_valid_var_name(name: Any) -> bool:
    """CSS 自定义属性名（与前端 `isValidVarName` 同一条正则）。"""
    return isinstance(name, str) and _VAR_NAME_RE.match(name) is not None


def is_valid_var_value(value: Any) -> bool:
    """外观变量的取值形状。

    只拦畸形数据（空串、超长、控制字符），**不**按单位白名单过滤：设置页写的是
    `0.15s ease` 这类带空格的值、颜色写的是 `#2f81f7`，两者与尺寸值在同一张表里。
    过严的校验会把用户已经生效的颜色或动效在落盘时丢掉，而界面上看不出来。
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _VAR_VALUE_MAX:
        return False
    return all(ch >= ' ' and ch != '\x7f' for ch in value)


def _glass_number(name: str, value: Any) -> bool:
    """玻璃变量的取值必须是**它自己**区间内的整数（`--glass-blur` 0-40px、`--glass-alpha` 30-100%）。

    取值带单位（`18px` / `84%`）：这两个变量是统一外观表里的普通成员，插件 iframe
    直接消费它们的 CSS 文本，"把单位剥掉再存"会让插件侧拿到一个无单位的数值。
    """
    if not isinstance(value, str):
        return False
    unit = _GLASS_UNITS[name]
    if not value.endswith(unit) or not _INT_VALUE_RE.match(value[:-len(unit)]):
        return False
    low, high = _GLASS_VAR_RANGES[name]
    return low <= int(value[:-len(unit)]) <= high


def _clamp_number(value: Any, low: float, high: float) -> Any:
    """把数字收敛到区间内（整数保持整数）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'{type(value).__name__} 不是数字')
    number = float(value)
    clamped = min(float(high), max(float(low), number))
    if clamped == int(clamped) and number == int(number):
        return int(clamped)
    return round(clamped, 3)


def _bool_or(default: bool, value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    _log_bad(key, value, '必须是布尔值')
    return default


def _normalize_theme(value: Any, current: str) -> str:
    if value in THEMES:
        return value
    _log_bad('theme', value, f'只接受 {" / ".join(THEMES)}')
    return current


def _normalize_vars(value: Any, current: Dict[str, str]) -> Dict[str, str]:
    """外观变量表：读-改-写合并（前端只发变动的那几个键）。

    `null` 表示删除该变量：设置页的"恢复默认"要能真的清掉用户值，否则
    variables.css 的默认值会被一条内联覆盖永久压住。
    """
    if not isinstance(value, dict):
        _log_bad('colors', value, '必须是 JSON 对象')
        return dict(current)
    merged = dict(current)
    for name, item in value.items():
        if item is None:
            merged.pop(name, None)
            continue
        if not is_valid_var_name(name):
            _log_bad(str(name), item, '不是合法的 CSS 自定义属性名')
            continue
        if not is_valid_var_value(item):
            _log_bad(name, item, '取值必须是 64 字符以内的单行字符串')
            continue
        # 玻璃那两个变量另有区间判据：类型正确但越界的值同样拒收，否则前端滑块的
        # min/max 一旦漂移，落盘的越界值会让整个界面错版。
        if name in _GLASS_VAR_RANGES and not _glass_number(name, item):
            _log_bad(name, item, '玻璃参数必须是区间内的整数')
            continue
        merged[name] = item
    if len(merged) > _MAX_VARS:
        _log_bad('colors', len(merged), f'变量数超过 {_MAX_VARS}，仅保留前 {_MAX_VARS} 项')
        merged = dict(list(merged.items())[:_MAX_VARS])
    return merged


def _normalize_background(value: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    """背景显示方式：逐键校验，非法键单独回退（不整段丢弃）。"""
    if not isinstance(value, dict):
        _log_bad('background', value, '必须是 JSON 对象')
        return copy.deepcopy(current)
    out = copy.deepcopy(current)
    for key, item in value.items():
        if key == 'path':
            if isinstance(item, str) and len(item) <= _MAX_ROUTE_LEN:
                out['path'] = item
            else:
                _log_bad('background.path', item, '必须是路径字符串')
        elif key == 'fit':
            if item in FITS:
                out['fit'] = item
            else:
                _log_bad('background.fit', item, f'只接受 {" / ".join(FITS)}')
        elif key in BG_LIMITS:
            low, high = BG_LIMITS[key]
            try:
                out[key] = _clamp_number(item, low, high)
            except TypeError:
                _log_bad(f'background.{key}', item, '必须是数字')
        elif key in ('flipX', 'flipY'):
            out[key] = _bool_or(bool(current.get(key)), item, f'background.{key}')
        elif key == 'version':
            if not isinstance(item, bool) and isinstance(item, int) and item >= 0:
                out['version'] = item
            else:
                _log_bad('background.version', item, '必须是非负整数')
        else:
            _log_bad(f'background.{key}', item, '不是已知的显示方式字段')
    return out


def _normalize_startup(value: Any, current: Dict[str, Any]) -> Dict[str, Any]:
    """启动偏好：`page` / `route` / `fullscreen` 逐键校验。

    `route` 只校验形状：它指向的插件还在不在只有前端知道（语义级校验放在前端，
    非法目标在首屏回退到第一个插件）。
    """
    if not isinstance(value, dict):
        _log_bad('startup', value, '必须是 JSON 对象')
        return copy.deepcopy(current)
    out = copy.deepcopy(current)
    for key, item in value.items():
        if key == 'page':
            if item in START_PAGES:
                out['page'] = item
            else:
                _log_bad('startup.page', item, f'只接受 {" / ".join(START_PAGES)}')
        elif key == 'route':
            if isinstance(item, str) and len(item) <= _MAX_ROUTE_LEN:
                out['route'] = item
            else:
                _log_bad('startup.route', item, '必须是路由字符串')
        elif key == 'fullscreen':
            out['fullscreen'] = _bool_or(
                bool(current.get('fullscreen')), item, 'startup.fullscreen')
        else:
            _log_bad(f'startup.{key}', item, '不是已知的启动偏好字段')
    return out


def default_prefs() -> Dict[str, Any]:
    """首次运行（或 `prefs` 段整段损坏）时的默认偏好。"""
    return {
        'theme': 'dark',
        'colors': {},
        'background': {'path': '', 'version': 0, 'fit': 'cover',
                       'posX': 50, 'posY': 50, 'zoom': 100, 'flipX': False, 'flipY': False},
        'startup': {'page': 'last', 'route': '', 'fullscreen': False},
        'last_route': '',
    }


class ShellPrefs:
    """壳偏好的读写。

    写入复用 `shell_info._store()` 的 `SettingsStore`：`shell.json` 的写者只有壳自己
    （插件写各自的 `<插件名>.json`），共用同一个实例即共用同一把锁，`prefs` 段与
    `log_level` 等平级键不会互相覆盖。
    """

    def __init__(self, config_dir: Optional[str] = None):
        base = Path(config_dir) if config_dir else None
        self._config_dir = str(base) if base else None
        self._store = _store(self._config_dir)
        # 空闲兜底提交的定时器（浏览器访问模式没有关窗事件可依赖）
        self._idle_timer: Optional[threading.Timer] = None

    # ===== 读 =====

    def read(self) -> Dict[str, Any]:
        """当前生效的偏好（已归一化）。深层副本：调用方拿到的是快照。"""
        return copy.deepcopy(self._read_stored())

    def has_stored(self) -> bool:
        """盘上是否已经有偏好段。

        给首屏注入用：前端据此判断"要不要把旧 localStorage 的值导进来"，而不是
        拿注入值里的键去猜 —— 注入的默认值与"读不到"长得一样，猜错就会把用户的
        旧偏好整份丢掉。
        """
        return isinstance(
            self._store.get(SHELL_SETTINGS_NAME).get(PREFS_KEY), dict)

    def _read_stored(self) -> Dict[str, Any]:
        """读盘并归一化。

        `SettingsStore.get()` 对损坏文件已经做了隔离与回退，这里只把**键级**非法值
        收敛回默认值 —— 一个键坏掉不该让整份偏好作废。
        """
        stored = self._store.get(SHELL_SETTINGS_NAME).get(PREFS_KEY)
        if not isinstance(stored, dict):
            if stored is not None:
                _log_bad(PREFS_KEY, stored, '必须是 JSON 对象')
            prefs = default_prefs()
        else:
            prefs = self._normalize_stored(stored)
        with _pending_lock:
            pending = _pending_routes.get(self._key())
        if pending is not None:
            prefs['last_route'] = pending
        return prefs

    def _normalize_stored(self, stored: Dict[str, Any]) -> Dict[str, Any]:
        """把盘上的 `prefs` 段归一化成完整形态（读与写回共用同一套规则）。"""
        prefs = default_prefs()
        prefs['theme'] = _normalize_theme(stored.get('theme', prefs['theme']), prefs['theme'])
        prefs['colors'] = _normalize_vars(stored.get('colors'), {})
        prefs['background'] = _normalize_background(
            stored.get('background'), prefs['background'])
        prefs['startup'] = _normalize_startup(stored.get('startup'), prefs['startup'])
        route = stored.get('last_route')
        if isinstance(route, str) and len(route) <= _MAX_ROUTE_LEN:
            prefs['last_route'] = route
        elif route is not None:
            _log_bad('last_route', route, '必须是路由字符串')
        return prefs

    # ===== 写 =====

    def apply_patch(self, patch: Any = None, kind: str = 'update') -> Dict[str, Any]:
        """局部更新并返回**生效后**的完整偏好。

        `kind='import'` 是旧 localStorage 值的一次性迁移：只在后端还没有 `prefs` 段
        时接受，否则拒绝（返回 `imported: False`）—— 否则一份过期的导入请求会把
        用户刚改的设置覆盖掉。

        返回值里带 `prefs`（生效值）：被丢弃的键由设置页显示**生效值**，
        否则界面显示的是坏值，用户会以为保存成功。
        """
        if kind not in ('update', 'import'):
            return {'success': False, 'error': f'不支持的写入方式: {kind!r}'}
        if patch is not None and not isinstance(patch, dict):
            return {'success': False,
                    'error': f'偏好补丁必须是 JSON 对象，实际是 {type(patch).__name__}'}
        try:
            size = len(json.dumps(patch or {}, ensure_ascii=False).encode('utf-8'))
        except (TypeError, ValueError) as e:
            return {'success': False, 'error': f'偏好补丁无法序列化: {e}'}
        if size > _MAX_PATCH_BYTES:
            return {'success': False, 'error': f'偏好补丁过大（{size} 字节）'}
        patch = patch or {}

        state: Dict[str, Any] = {}

        def _transform(stored: Dict[str, Any]) -> Dict[str, Any]:
            current = stored.get(PREFS_KEY)
            if kind == 'import' and isinstance(current, dict):
                # 后端已经有偏好：这份导入是过期的，不覆盖，也不落盘
                state['imported'] = False
                return stored
            state['imported'] = True
            base = self._normalize_stored(current) if isinstance(current, dict) else default_prefs()
            stored[PREFS_KEY] = self._merge(base, patch)
            return stored

        self._store.transform(SHELL_SETTINGS_NAME, _transform)
        effective = self.read()
        if patch.get('last_route') is not None:
            # 上报的路由只进内存，等安全退出或空闲兜底再落盘（见 commit_last_route）。
            # 顺序必须在 read() 之后：read() 会把内存里的待提交值并进返回值，
            # 这样同一进程内立刻回读也能看到刚上报的页面。
            self.set_last_route(patch['last_route'])
            effective = self.read()
        effective = self.read()
        if state['imported']:
            log.info(f'[ShellPrefs] 偏好已写入 {len(patch)} 个键（方式 {kind}）：'
                     f'{sorted(patch)} theme={patch.get("theme", "-")}')
        return {'success': True, 'imported': state['imported'], 'prefs': effective}

    def _merge(self, current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        """逐键合并：`colors` 是读-改-写，其余键整段替换。"""
        merged = copy.deepcopy(current)
        if 'colors' in patch or 'vars' in patch:
            incoming = patch.get('colors', patch.get('vars'))
            merged['colors'] = _normalize_vars(incoming, merged.get('colors') or {})
        if 'background' in patch:
            merged['background'] = _normalize_background(
                patch['background'], merged['background'])
        if 'startup' in patch:
            merged['startup'] = _normalize_startup(patch['startup'], merged['startup'])
        if 'theme' in patch:
            merged['theme'] = _normalize_theme(patch['theme'], merged['theme'])
        # `last_route` **不在这里写**：它只记安全退出的值，前端上报时进内存
        # （apply_patch 末尾调 set_last_route），落盘只由 commit_last_route 完成。
        # 在这里顺手写一次就等于"每次切路由都落盘"，强杀留下的会是最后那次路由。
        for key in patch:
            if key not in ('colors', 'vars', 'background', 'startup', 'theme', 'last_route'):
                _log_bad(key, patch[key], '不是已知的偏好字段')
        return merged

    # ===== last_route：只记安全退出 =====

    def set_last_route(self, route: Any) -> bool:
        """记录当前路由（**只进内存**），并安排空闲兜底提交。"""
        if not isinstance(route, str) or len(route) > _MAX_ROUTE_LEN:
            _log_bad('last_route', route, '必须是路由字符串')
            return False
        with _pending_lock:
            _pending_routes[self._key()] = route
        self.settle_later()
        return True

    def commit_last_route(self) -> bool:
        """把内存里的 `last_route` 落盘，返回是否真的发生了一次写入。

        调用点必须是**安全退出**（`webview.start()` 正常返回、关窗退出）或空闲兜底，
        不能在 `finally` 里无条件调用：异常展开与强杀不算安全退出，那时应当保留
        上一次安全退出的值。
        """
        with _pending_lock:
            route = _pending_routes.pop(self._key(), None)
            self._cancel_idle_timer_locked()
        if route is None:
            return False
        with _commit_lock:
            self._write_last_route(route)
        log.info(f'[ShellPrefs] 上次页面已记录: {route}')
        return True

    def _write_last_route(self, route: str) -> None:
        def _transform(stored: Dict[str, Any]) -> Dict[str, Any]:
            current = stored.get(PREFS_KEY)
            base = self._normalize_stored(current) if isinstance(current, dict) else default_prefs()
            base['last_route'] = route
            stored[PREFS_KEY] = base
            return stored

        self._store.transform(SHELL_SETTINGS_NAME, _transform)

    def settle_later(self, delay: float = IDLE_COMMIT_SECONDS) -> None:
        """安排一次空闲兜底提交（重复调用只保留最后一个定时器）。"""
        with _pending_lock:
            self._cancel_idle_timer_locked()
            if self._key() not in _pending_routes:
                return
            timer = threading.Timer(delay, self._settle)
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    def _settle(self) -> None:
        try:
            self.commit_last_route()
        except Exception as e:  # 定时器线程里的异常没有人接
            log.error(f'[ShellPrefs] 空闲提交上次页面失败: {e}')

    def _cancel_idle_timer_locked(self) -> None:
        timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()

    def _key(self) -> str:
        """pending 表按配置目录分隔：同进程内的多实例夹具各记各的值。"""
        return self._config_dir or str(self._store.settings_dir)
