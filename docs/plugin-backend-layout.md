# 插件后端组件化：目标形态与迁移配方

这份文档给**要动手改一个插件后端的人**（或智能体）：目标目录长什么样、组件怎么命名、
依赖怎么注入、按什么顺序迁移、每一步拿什么命令验收。骨架代码可以直接复制。

判据只有三条，都能机器测量：静态检查能不能用、改一个成员要动几个文件、能不能不启动
插件就测一个能力。迁移前的写法在这三条上都是最差的一档，理由见下；配方的现状是
**8 个插件的后端全部完成组件化**，当前没有任何豁免文件。

## 为什么迁移前的写法不行

插件后端由 `PluginManager` 用 importlib 直接加载**入口文件**（manifest 的
`backend.entry`），插件目录不是包、也不在 `sys.path` 上。为了给一个几千行的类分文件，
迁移前用了两个绕法：

1. **把方法体拆进若干 mixin 文件**，入口类同时继承它们，`self` 仍是同一个对象。
   文件行数是下来了，耦合一点没降：所有分片共享入口 `__init__` 里那块状态，彼此的调用
   关系没有任何声明。实测 `image-viewer`：跨分片引用的成员有 36 个，另有 20 个属性只在
   入口 `__init__` 里赋值、被各分片直接读写 —— 这 56 个成员没有任何地方声明。
2. **兄弟模块自己 `load_sibling`** 去拿共享常量。能跑，但依赖方向变成一张与文件布局
   无关的网：谁依赖谁只能靠在十几个文件里搜名字。

代价是静态检查整体失效：`pyright plugins` 报 664 条错误，其中 **603 条（91%）** 是
`Cannot access attribute "x" for class "ThumbMixin*"` —— 分片里的 `self.x` 在类型检查器
看来都不存在。于是插件被排除在门禁之外，真错误（61 条：参数类型不匹配、override 不兼容、
可能未绑定的变量）没人看。

更贵的一次是 `super()` 陷阱：mixins 排在 `PluginBase` 之前，`super().get_data_root()`
命中了某个 mixin 的实现，读的正是这一行要算的属性，导致**空白实例（新装用户）整插件
加载失败**，连带依赖它的插件一起挂掉；开发机上被历史配置掩盖，只有"从零起一个实例"
才复现。

纯函数化不是答案：实测 `image-viewer` 后端 2464 行里只有 60 行（2%）的函数不碰 `self`。
这套逻辑本质上是有状态的（磁盘缓存、设置继承、锁），要解决的是**让状态显式、有归属、
可声明**，而不是消灭状态。

## 三条规则（门禁是 `tools/check_plugin_layering.py`）

| 规则 | 内容 | 为什么 |
| --- | --- | --- |
| R1 | 后端模块不得定义 Mixin 类，入口类不得继承 Mixin | 这条形状本身就是"把类切成文件"，静态检查与 `super()` 都不可靠 |
| R2 | 单个后端 `.py` ≤ 400 行；入口 ≤ 250 行 | 入口只放装配；越过 400 行通常意味着一个文件担了两种职责 |
| R3 | 只有入口可以调用 `load_sibling`；非入口模块不得 import 兄弟模块 | 依赖必须由入口注入，这既是分层，也是静态可查的前提 |

违反时门禁给 `[error] [<插件>] R<规则> <文件>：<原因 + 怎么改>`。迁移未完成的插件用
`tools/layering_allowlist/<plugin>.txt` 豁免（文件存在即该插件不被拦，但每次跑门禁都会
打印 warning）；**迁移的最后一步是删掉它**，留着不删会被判"豁免文件已过期"并报错。
8 个插件的入口都在 112–233 行之间，R1–R3 对每个插件都是硬门禁，当前没有任何豁免文件；
`tools/layering_allowlist/` 因此不入库（空目录进不了 git）。豁免机制仍留在检查器里，只作为
将来某个插件迁移期间的临时通道 —— 放回 `<插件名>.txt` 时该目录随之重新出现。

## 目标目录布局

```
plugins/<name>/backend/
  main.py          入口（facade）：register_api 绑定表、壳按名调用的钩子、装配
  const.py         纯数据与共享常量：设置项 schema、扩展名、上限、失效键集合（只有入口加载它）
  paths.py         PathLayout：数据根、缓存/缩略图/索引文件的派生路径、虚拟路径与命名空间
  settings.py      SettingsResolver：按目录的设置读取与逐级继承
  thumbs.py        ThumbStore：尺寸元数据缓存、缩略图字节、落盘时机
  scanner.py       目录扫描：策略在构造时确定，逐目录调用
  albums.py        AlbumIndex：相册树、全树目录快照、索引缓存、相册配置
  file_ops.py      FileOps：新建 / 删除 / 移动 / 定向重建
  rebuild.py       RebuildTask：后台全量重建任务的生命周期
  freshness.py     FreshnessProvider：向壳声明新鲜度规格与钩子
```

不是每个插件都需要全部文件：只放该插件真正拥有的职责，宁可少建也不要建空壳。
`document-reader` 的 `tts` / `marks` / `library`、`group-mesh` 的 `sync` / `peers` / `node`
各自是独立组件，命名按职责（名词），不按"第几片"。

## 组件能 import 什么

组件允许 import 的只有：标准库、第三方库、`shell.backend.*`。**不 import 兄弟模块，
也不 `load_sibling`。** 插件内部共享的东西按三选一处理：

- 只有入口用 → 留在 `main.py`；
- 多个组件都要用、且是纯常量/纯函数 → 放 `const.py`，由入口读出来当构造参数传下去；
- 通用、与插件业务无关 → 用 `shell.backend` 里现成的：

| 需要什么 | 用什么 |
| --- | --- |
| 扫目录、自然序、路径安全 | `media_catalog.scan_directory`、`media_catalog.natural_sort_key`、`media_catalog.is_hidden_name`、`media_catalog.is_safe_path` |
| JSON 缓存的原子写 + 版本/指纹校验 | `json_cache.read_cached_json`、`json_cache.write_cached_json`、`json_cache.read_json`、`json_cache.write_json` |
| 缩略图库 | `thumb_cache.ThumbCache` |
| 组件的构造参数类型（读设置 / 写设置） | `ports.SettingsPort`、`ports.PersistPort` |

## 端口：组件只看得到它需要的宿主能力

端口已经在共享基建里：`plugins` 里的组件直接 `from shell.backend.ports import SettingsPort`，
**不要在插件目录里再定义一份**（插件之间不能互相 import，写 8 份只会得到 8 个漂移的版本）。

```python
from typing import Any, Mapping, Protocol

class SettingsPort(Protocol):
    """读单个设置项（由 PluginBase.setting 满足）。"""
    def setting(self, key: str, default: Any = None) -> Any: ...

class PersistPort(Protocol):
    """写设置（由 PluginBase.update_setting / save_settings 满足）。"""
    def update_setting(self, key: str, value: Any) -> bool: ...
    def save_settings(self, values: Mapping[str, Any]) -> dict: ...
```

上面是形状说明；真正的定义在 `shell/backend/ports.py`，请 import 而不是复制。`PluginBase`
结构化地满足这两个端口，所以入口直接把 `self` 传进去；用例里传一个 dict 支撑的假对象
就能单独测组件。

## 组件骨架

组件的状态**自持**：原本散在入口 `__init__` 里的缓存、锁、计数器跟着它保护的数据走。

```python
# plugins/<name>/backend/paths.py
"""数据根与派生路径、虚拟路径解析。

状态自持：根目录、额外根、命名空间映射都在这里。换根走 rebind()，由入口编排；
不再由任何一处随手改 self 上的属性。
"""

from pathlib import Path
from typing import List

from shell.backend.media_catalog import is_safe_path
from shell.backend.ports import SettingsPort


class PathLayout:
    def __init__(self, root: Path, settings: SettingsPort) -> None:
        self._settings = settings
        self.rebind(root)

    def rebind(self, root: Path) -> None:
        self.root = root
        self.cache_dir = root / '.cache'
        self.thumb_dir = self.cache_dir / 'thumbs'
        self.thumb_db_path = self.cache_dir / 'thumbs.db'
        self.meta_file = self.cache_dir / 'image_meta.json'
        self.album_cache_file = self.cache_dir / 'albums_index.json'
        self.dirs_cache_file = self.cache_dir / 'dirs_index.json'
        self.album_config_file = self.cache_dir / 'albums_config.json'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self._extra_cache = None          # 额外根：按设置代次缓存

    def extra_roots(self) -> List[Path]:
        """额外数据根（设置项 extra_roots，每行一个）。"""
        if self._extra_cache is None:
            raw = self._settings.setting('extra_roots') or ''
            self._extra_cache = [Path(line.strip()).expanduser().resolve()
                                 for line in str(raw).splitlines() if line.strip()]
        return list(self._extra_cache)

    def roots(self) -> List[Path]:
        """全部数据根：第一根在前。"""
        return [self.root, *self.extra_roots()]

    def resolve_file_path(self, rel_path: str):
        """虚拟路径 → 物理路径；越界或命名空间未知返回 None。

        单根插件就是一次带安全检查的拼接；多根插件在这里解析 `__<命名空间>` 前缀。
        壳的 `/file` 用它，返回 None 表示"按默认规则处理"。
        """
        target = (self.root / (rel_path or '')).resolve()
        if not target.is_relative_to(self.root.resolve()):
            return None
        return target
```

`paths.py` 的关键收益是**缓存**：额外根与命名空间映射是"按设置算出来的派生值"，算一次
存起来、设置变了才重算（由入口在 `on_settings_changed` 里作废）。现在的实现每次解析都
重新算，实测 402 个目录触发 603 次 `os.scandir`、其中第一根目录被重复扫描 202 次；额外
根下上万目录时这个数字同样上万。

```python
# plugins/<name>/backend/settings.py
"""按目录的设置读取与逐级继承。

状态自持：设置来源缓存与按路径的结果缓存都在这里，写入路径负责让它们失效。
"""

from typing import Dict

from shell.backend.ports import PersistPort, SettingsPort


class SettingsResolver:
    def __init__(self, paths, settings: SettingsPort, persist: PersistPort) -> None:
        self._paths = paths
        self._settings = settings
        self._persist = persist
        self._by_path: Dict[str, dict] = {}

    def invalidate(self) -> None:
        """设置变了：结果缓存整份作废（由入口在 on_settings_changed 里调用）。"""
        self._by_path.clear()

    def for_path(self, rel_path: str = '') -> Dict:
        """该目录生效的设置（含逐级继承）。

        返回拷贝：调用方会往结果里补字段（例如 root_dir），共享同一份缓存会被改坏。
        """
        hit = self._by_path.get(rel_path)
        if hit is None:
            hit = self._compute(rel_path)
            self._by_path[rel_path] = hit
        return dict(hit)

    def save_folder(self, rel_path: str = '', settings=None) -> Dict:
        """写入设置：全局走宿主校验，文件夹级存进 folders 子树。

        文件夹级写入的形状（哪些键允许落进 folders）属于本插件的业务规则，
        按迁移前的实现原样搬过来，不要在这一步改变语义。
        """
        result = self._persist.save_settings(settings or {})
        if result.get('success'):
            self.invalidate()
        return result
```

```python
# plugins/<name>/backend/thumbs.py
"""尺寸元数据与缩略图字节。

状态自持：meta 缓存、脏标记、锁、ThumbCache 都在这里；落盘时机由 flush() 统一。
"""

import threading

from shell.backend.json_cache import read_json
from shell.backend.thumb_cache import ThumbCache


class ThumbStore:
    def __init__(self, paths) -> None:
        self._paths = paths
        self._lock = threading.Lock()
        self._meta: dict = read_json(paths.meta_file, default={}) or {}
        self._dirty = False
        self._cache = ThumbCache(paths.thumb_db_path)

    def rebind(self) -> None:
        """换根后重开缩略图库并重读元数据（由入口编排调用）。"""
        self._cache = ThumbCache(self._paths.thumb_db_path)
        self._meta = read_json(self._paths.meta_file, default={}) or {}
        self._dirty = False

    def flush(self) -> None:
        """把新增的尺寸元数据落盘一次（列表/重建的收尾调用）。"""
        if not self._dirty:
            return
        with self._lock:
            self._dirty = False

    def get_thumb_data(self, rel_path: str):
        """供壳的 `/thumbs` 路由取缩略图字节；不可用返回 None（壳按 404 处理）。"""
        target = self._paths.resolve_file_path(rel_path)
        if target is None:
            return None
        try:
            return self._cache.get(rel_path, target)
        except Exception:
            return None
```

```python
# plugins/<name>/backend/scanner.py
"""目录扫描：把"怎么读一个目录"和"扫到的结果怎么用"分开。

无状态：允许的扩展名在构造时确定，扫描策略因此可以被单独验证。
"""

from pathlib import Path

from shell.backend import media_catalog


class DirScanner:
    def __init__(self, allowed_extensions) -> None:
        self._allowed = frozenset(allowed_extensions)

    def scan(self, directory: Path) -> media_catalog.ScanResult:
        """扫一个目录的直接下级（子目录 + 媒体文件），按自然序返回。"""
        return media_catalog.scan_directory(directory, self._allowed)
```

```python
# plugins/<name>/backend/albums.py
"""相册树与全树索引。

状态自持：索引缓存、目录快照、锁、TTL、诊断计数都在这里；对外只暴露"要什么"，
不暴露"缓存里长什么样"。
"""

import threading


class AlbumIndex:
    def __init__(self, paths, scanner, settings, thumbs) -> None:
        self._paths = paths
        self._scanner = scanner
        self._settings = settings
        self._thumbs = thumbs
        self._lock = threading.Lock()
        self._dirs_snapshot = None
        self._albums_cache = None

    def reset(self) -> None:
        """换根或设置变更后丢弃全部缓存（由入口编排调用）。"""
        with self._lock:
            self._dirs_snapshot = None
            self._albums_cache = None
```

## 入口（facade）：只做装配与委派

入口是**唯一**可以 `load_sibling` 的地方，也是唯一知道"谁依赖谁"的地方。

### 入口只放什么

| 放 | 不放 |
| --- | --- |
| 装配（构造组件、注入依赖） | 任何业务逻辑（方法体超过"一行委派"就说明放错了） |
| `register_api()` 绑定表 | 每个 API 键的转发方法（26 个键 = 26 个方法，直接吃掉行数预算） |
| 壳按名调用的钩子（一行委派） | 设置项 schema、常量、纯函数（这些是**数据**，放 `const.py`） |
| 需要在插件自身身份上完成的操作（诊断聚合、壳引擎按实例挂取） | 组件可以自己完成的事 |

**设置项 schema 这类纯数据放 `const.py`，入口只保留装配、API 绑定表与壳钩子。** schema
是"插件对外声明什么设置项"的数据，不是装配；把它留在入口会把装配挤到行数上限附近，让
下一个加 API 键的人被迫去删注释。入口以 `settings_schema = SETTINGS_SCHEMA` 引用它，
`PluginBase` 与壳的设置弹窗都从类属性读，行为与写在入口里完全一致。

行数口径：**目标是 ≤ 200 行，R2 的硬上限是 250 行**。目标比硬上限低 50 行，是给"下一个
加 API 键的人"留的余量——踩到硬上限时正确的做法是把数据或职责挪进组件，不是压缩可读性。

```python
# plugins/<name>/backend/main.py
"""插件入口：类骨架、设置 schema、API 表、壳钩子与装配。

本文件只做三件事：声明插件对壳暴露什么、把组件装配起来、把壳按名调用的钩子
一行委派给组件。业务逻辑不写在这里（门禁 R2 把入口限制在 250 行以内）。

接缝说明：load_sibling 返回 ModuleType，装配出来的组件属性在静态检查里是 Any。
这是有意的取舍 —— 入口足够短，需要检查的东西都在组件里；为了让接缝有类型而把插件
目录塞进 sys.path 会换来模块名冲突，代价更大。
"""

from pathlib import Path
from typing import Any, ClassVar, Dict, List

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_paths = load_sibling(__file__, 'paths', '<name>')
_settings = load_sibling(__file__, 'settings', '<name>')
_thumbs = load_sibling(__file__, 'thumbs', '<name>')
_scanner = load_sibling(__file__, 'scanner', '<name>')
_albums = load_sibling(__file__, 'albums', '<name>')
_const = load_sibling(__file__, 'const', '<name>')


class XxxPlugin(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        # 与迁移前逐字一致：迁移不改设置契约
    ]

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        # 先算根、再建组件：其余组件的路径全部从 PathLayout 派生。
        # 这里显式写 PluginBase.get_data_root(self)，不用 super()：即使组件化之后
        # 已经没有 mixin，这个习惯本身也是"不依赖 MRO 顺序"的保证。
        root = Path(self.setting('root_dir') or str(PluginBase.get_data_root(self))).resolve()
        self.paths = _paths.PathLayout(root, self)
        self.thumb_dir = self.paths.thumb_dir      # 壳 /thumbs 的兜底目录
        self.settings = _settings.SettingsResolver(self.paths, self, self)
        self.thumbs = _thumbs.ThumbStore(self.paths)
        self.scanner = _scanner.DirScanner(_const.ALLOWED_EXTENSIONS)
        self.albums = _albums.AlbumIndex(
            self.paths, self.scanner, self.settings, self.thumbs)

    def register_api(self) -> dict:
        return {
            'list_albums': self.albums.list_albums,
            'get_settings': self.settings.for_path,
            'save_settings': self.settings.save_folder,
        }

    # ===== 壳按名调用的钩子：一行委派，名字与迁移前完全一致 =====
    def get_data_root(self) -> Path:
        return self.paths.root

    def get_file_roots(self) -> List[Path]:
        return self.paths.roots()

    def resolve_file_path(self, rel_path: str):
        return self.paths.resolve_file_path(rel_path)

    def get_thumb_data(self, rel_path: str):
        return self.thumbs.get_thumb_data(rel_path)

    def get_cache_dir(self) -> Path:
        return self.paths.cache_dir

    def on_settings_changed(self, changed_keys) -> None:
        if {'root_dir', 'extra_roots'} & set(changed_keys):
            self.paths.rebind(Path(self.setting('root_dir')))
            self.thumbs.rebind()
            self.albums.reset()
        self.settings.invalidate()

    def on_unload(self) -> None:
        self.rebuild.shutdown()
```

组件按插件实际拥有的职责增删（见上面的布局表）；`file_ops` / `rebuild` / `freshness`
只在需要时装配，形状与 `albums.py` 一致：构造收依赖、对外暴露动词。骨架展开到"能看出
形状"为止 —— `albums.list_albums`、`rebuild.shutdown`、`freshness.spec` 这类方法体属于
各插件原有逻辑，按第 5–7 步原样搬进组件，不要在这一步重写。

三条必须守住的约束：

- **`register_api` 的键名与迁移前逐字一致**（`save_settings` 映射到 `save_folder_settings`
  这类也保持不变）。前端与壳按名字调用，改名就是静默 404。
- **壳按名调用的钩子必须留在入口类上**（`get_file_roots` / `resolve_file_path` /
  `get_data_root` / `get_thumb_data` / `ensure_thumb` / `get_cache_dir` / `freshness_spec` /
  `on_settings_changed` / `on_unload`）。它们由壳按名字取，不经过 `register_api`。
- **不要在入口里覆写 `PluginBase.get_settings()`**。带参数的"按目录读设置"是组件的
  `settings.for_path(rel_path)`；覆写基类同名方法会改变它的签名，基类的脱敏与默认值语义
  随之失效（现状里 `image-viewer` 就有这个冲突，只是它今天没有 `secret` 键才没出事）。
- **附属插件按实例调用的成员必须在门面保留短委派**。壳按名取的九个钩子不是唯一的实例级
  契约：声明了 `dependencies: ["<宿主>"]` 的插件会直接读宿主实例（`getattr(host,
  'list_roots')`、`host.delete_files(...)`）。这些成员如果把实现只放在组件里，宿主实例上
  就没有了——而调用方通常带 `getattr` 兜底，**漏了不报错**，只是静默退化成单根或某个功能
  失效。正确做法是在门面写两三行委派，并在 API 表用例里钉住"这些成员按实例可调"。

## 八步迁移顺序

每步都保持行为不变、可独立提交。顺序按依赖图的拓扑序排：先建底层的路径与状态，再逐片
下沉；两个方向相反的环在中间两步断掉，否则后面每一步都要在两个文件之间来回改。

| 步 | 做什么 | 断掉什么 | 验收 |
| --- | --- | --- | --- |
| 1 | 建 `const.py`：把入口里的常量与纯函数收拢进来 | 分片 → 入口的常量边 | 本插件测试全绿 |
| 2 | `namespace` 分片 → `paths.py`，入口保留同名委派 | — | `pyright plugins/<name>` 该文件伪影归零 |
| 3 | `thumbs` 分片 → `thumbs.py`；错位的列表缓存还给它的使用者 | — | 同上 |
| 4 | `settings` 分片 → `settings.py`，`get_settings(rel)` 改名 `for_path` | 与基类同名方法的冲突 | `register_api` 键名不变，设置往返测试绿 |
| 5 | `_scan_dir_direct` → `scanner.py`；带缓存的那层归 `albums.py` | listing ↔ albums 环 | 相册树与列表测试绿 |
| 6 | `albums` 分片 → `albums.py`；`_prune_visible_marks` 从 file_ops 移入 | albums ↔ file_ops 环 | 相册配置与可见性测试绿 |
| 7 | `file_ops` / `rebuild` / `freshness` → 各自组件，入口只剩委派 | — | 删除 / 移动 / 重建测试绿 |
| 8 | 删除 `tools/layering_allowlist/<plugin>.txt` | 门禁对该插件正式生效 | 下面三条命令全绿 |

每步之后跑：

```bash
venv/Scripts/python.exe tools/check_plugin_layering.py
venv/Scripts/python.exe -m pyright plugins/<plugin>
venv/Scripts/python.exe tools/plugin_ci.py --plugin <plugin>
```

`pyright plugins/<plugin>` 的条数可以当进度条：伪影（`for class "XxxMixin*"`）应当一步步
降到 0，剩下的都是真错误。门禁对该插件的 warning 会一直存在，直到第 8 步删掉豁免文件。

## 参考实现：image-viewer 迁移后的实际形态

`image-viewer` 是这份配方的参考实现（迁移后 `pyright plugins/image-viewer` 0 错误、
分层门禁无警告）。它的组件比上面那张布局表更细，原因是相册类插件的索引逻辑本身接近
900 行，按下面的职责切开之后每个文件都在 400 行以内：

| 文件 | 组件 | 职责 | 依赖 |
| --- | --- | --- | --- |
| `main.py` | `ImageViewerPlugin` | `settings_schema` 引用、API 表、壳钩子、装配、诊断、附属契约成员（200 行） | 全部（唯一装配点） |
| `const.py` | — | 设置项 schema、扩展名、上限、硬默认值、失效键集合 | 无 |
| `naming.py` | `PixivNaming` | 作品号 / 页序号 / 封面挑选 / 排序规则（纯） | 无 |
| `paths.py` | `PathLayout` | 根、派生路径、虚拟路径、命名空间（含映射缓存） | `SettingsPort` |
| `settings.py` | `SettingsResolver` | 按目录设置与继承、Pixiv 标记 | `paths`、两个端口、入口的读取闭包 |
| `thumbs.py` | `ThumbStore` | 尺寸元数据、`ThumbCache`、落盘时机 | `paths` |
| `scanner.py` | `DirScanner` | 单目录裸扫描 | `paths`、`naming` |
| `dirs.py` | `DirSnapshot` | 全树目录枚举与落盘快照 | `paths` |
| `entries.py` | `DirEntryCache` | 目录条目存储：带 mtime 的扫描与递归聚合 | `paths`、`scanner`、`settings`、`naming` |
| `albums.py` | `AlbumIndex` | 相册树、全树聚合、索引缓存生命周期 | `paths`、`dirs`、`entries`、`settings`、`config`、`naming` |
| `album_config.py` | `AlbumConfig` | 可见性配置与「保留可见」标记 | `paths` |
| `tiles.py` | `AlbumTiles` | 子目录瓦片、按批补齐、灯箱序列 | `paths`、`entries`、`settings`、`thumbs`、`naming` |
| `listing.py` | `Listing` | 分页列表与列表缓存 | `paths`、`tiles`、`settings`、`thumbs`、`naming` |
| `file_ops.py` | `FileOps` | 增删改与定向重建 | `paths`、`thumbs`、`config`、`albums`、`listing`、宿主的受保护判定 |
| `rebuild.py` | `RebuildTask` | 后台重建任务 | `paths`、`thumbs`、`albums`、`listing` |
| `freshness.py` | `FreshnessProvider` | 刷新规格与钩子、兼容刷新入口 | `paths`、`thumbs`、`albums`、`entries`、`listing`、引擎闭包 |

装配顺序就是依赖顺序，两个方向相反的环在这一步断掉：

- **扫描 ↔ 索引**：裸扫描（`scanner`）与"带缓存的那层"（`entries`）分开，`albums` 只
  用它们，两者都不再回头调 `albums`；
- **索引 ↔ 文件操作**：「保留可见」标记（`album_config`）从 `file_ops` 移出，两边都只
  依赖它，不再互调。

## 配方修正（来自参考实现）

上面几节的骨架是方向，不是逐字可抄的成品。参考实现踩到的五点写成规则：

1. **入口不要给每个 API 方法写一行委派。** 26 个 API 键各写一行委派会直接吃掉 R2 的
   250 行预算，剩下的装配与 schema 就放不下了。正确做法是：`register_api` 直接绑组件
   方法（`'list_albums': self.albums.list_albums`），入口只保留壳按名调用的九个钩子、
   `diagnose`，以及**确实需要插件身份**的兼容入口（例如壳的刷新引擎按插件实例挂载，
   取它的操作要经过入口或由入口注入闭包）。
2. **共享纯函数超过三个时收成一个注入对象。** 往三个组件的构造参数里各塞两三个函数会
   让签名不可读。参考实现把 Pixiv 命名规则收成 `naming.PixivNaming` 一个对象；成组的
   数值上限收成 `const.Limits`（连带唯一的截断策略 `limit_all_images`）。两者都只由入口
   构造一次，再按构造参数注入 —— 仍然满足"组件之间不互相 import"。
3. **不要写 `self.thumb_dir = self.paths.thumb_dir`。** 基类的 `thumb_dir` 已经从
   `get_data_root()` 派生，赋值只会把值钉进 `_thumb_dir_override`，换根后必须记得重新
   赋值，否则陈旧。参考实现直接不覆写，靠 `get_data_root() → paths.root` 跟随。
4. **用例要跟着改，并补一条 API 表用例。** 迁移前用例直接调插件实例上的 API 方法，
   等于顺带验证了 `register_api` 的绑定；迁移后调用点改成组件路径（参考实现改了 128 处），
   绑定本身就没有覆盖了。用一条小用例锁住"键与顺序不变 + 每个键绑到哪个组件 + 九个钩子
   仍在入口类上 + 入口没有覆盖 `PluginBase.get_settings`"。

5. **附属插件按实例调用的宿主成员要在门面留短委派。** 配方原先只列了"壳按名调用的九个
   钩子"，但声明 `dependencies: ["<宿主>"]` 的插件会直接读宿主实例：`image-cleaner` 用
   `getattr(host, 'list_roots')` 拿额外根前缀、用 `host.delete_files(...)` 复用宿主的删除
   与缓存清理。迁移时如果只把实现留在组件里，宿主实例上就没有这两个成员了 —— 调用方带
   `getattr` 兜底，所以**不报错**，只是静默退回单根（表现为"额外图库里的文件不参与扫描"）。
   正确做法：门面保留两三行委派，并在 `register_api` 表用例里断言这些成员按实例可调。
   推广：判断"哪些成员必须留在实例上"的依据是**谁按名字从实例上取**（壳、附属插件、测试），
   不是"它是不是 API 键"。

另外一条与行为等价有关的发现：`ensure_thumb` 的旧实现返回缩略图路径，而基类契约是返回
`None` —— 这是个被 mixin 噪声淹没的真错误，壳与附属插件都不消费这个返回值。迁移时按
基类契约改成返回 `None`，顺手补上了这条类型错误。

## 另外三个插件的实际形态（计划里没料到的三点）

参考实现只覆盖了"相册类"一个形状。document-reader、media-player、group-mesh 三个插件
暴露出配方原本没想到的情况，下面按实测写：

| 插件 | 入口行数 | 后端文件数 | 拆分时真正起作用的判据 |
| --- | --- | --- | --- |
| `document-reader` | 147 | 17 | 原来 4 个 mixin 只占少数错误，**主要问题是两个 700/600 行的非 mixin 大文件** |
| `media-player` | 170 | 18 | 一个文件里同时住着"文件原子写"与"集合语义"，两种变化原因 |
| `group-mesh` | 233 | 19 | 聚合器（状态汇总）必须放最上层，否则它自己变成新的环 |

**入口行数目标 ≤200。** R2 的硬上限是 250 行，但四个插件实测 147–233，其中三个都在 200
以内。把目标定在 200 而不是踩着 250：入口只该放 `settings_schema` 引用、`register_api`
绑定表、壳按名调用的钩子与装配，一旦超过 200 行，几乎总能找到"某个业务方法被顺手放回来"
的证据。250 的余量留给插件特有的兼容入口（壳的刷新引擎按实例挂载这类）。

### document-reader：非 mixin 的大文件同样要拆

`document-reader` 的 mixin 只有 4 个（`marks` / `tts` / `library` / `roots`），但它在
R2 上的违规主要来自两个本来就不是 mixin 的文件：

- `documents.py`（712 行）同时装着"按后缀分派解析器"、"Markdown 归一化"与"EPUB 容器解析"
  三件事，拆成 `content.py`（按格式分派的入口）、`markdown.py`（纯文本归一化）、
  `epub.py`（EPUB 的容器与 spine 解析）；
- `tts_engine.py`（633 行）同时装着"引擎选择"、"edge-tts 在线合成"与"系统离线音色"，
  拆成 `tts_engine.py`（选择与参数整理）、`tts_edge.py`、`tts_endpoint.py`、`tts_system.py`。

规则：**R2 是所有后端文件的上限，不是"mixin 文件"的上限。** 判断一个文件该不该拆的依据
不是它有多长，而是"这个文件里有几种互不相同的失败原因"——解析格式的失败、网络合成的失败、
读取本机音色的失败，排障时看的是不同的东西。

### media-player：存储机制与数据语义分开

`media-player` 迁移后的 `state.py`（269 行）与 `collections.py`（200 行）来自同一个原文件。
拆法的依据是：一边是"这份状态怎么原子落盘"（临时文件、`os.replace`、损坏时的回退），
另一边是"订阅、播放列表、最近播放这些集合语义"。两者变化的理由完全不同——换存储格式时
不该重读集合逻辑，改集合语义时也不该碰落盘。这两件事放进一个 400 行以内的文件本来放得下，
但放进来之后每次改动都在两个关注点之间来回跳。

### group-mesh：聚合器放最上层，入口只留一行委派

`group-mesh` 有 18 个组件，装配顺序是依赖顺序（脉冲 → 对端事实 → 存储 → 注册表 →
连接池 → 节点 → 发现 → 对端 / 团体 / 共享项 → 状态汇总 → 远端链 → 上传）。**不要机械
照搬"底层放前面"**：`status.StatusReport` 是聚合器，它把节点、发现、共享项、对端事实汇总成
首屏状态，必须放在最上层，入口只保留 `'get_status': self.report.get_status` 一行绑定。
把聚合器按"它依赖很多组件、所以应该靠前"的顺序往下放，就等于让聚合器变成被多方依赖的
中间层，把已经断开的环重新接上。

它断掉的四个环，和各自归位的方式：

1. **探测 ↔ 节点**：后台同步轮次、候选端点、并行探测与推送整体挪进 `discovery.Discovery`，
   只依赖 node / registry / connections / pulse / facts；`node` 不再回头调 discovery。
2. **注册表派生视图**：`registry.MeshRegistry` 既存对象、也存"由注册表算出来的视图"
   （`peer_endpoints` / `candidates_for` / `bootstrap_endpoints` / `device_names`）。
   这些视图原先散在对端与发现两处、各自重算并互相调用，收进 registry 之后两边都只读它。
3. **脉冲**：`pulse.SyncPulse` 只保留脏标志、唤醒 / 停止事件与间隔。它原先兼做状态汇总，
   于是"想触发同步的组件"和"想展示状态的组件"都在改它；拆开后触发方只调 `request()`，
   展示方只读 `status()`。
4. **对端事实**：`peer_facts.PeerFacts` 收"连不上的端点、名单版本、采纳提示"这类运行期事实，
   从 node 与 shares 的状态里剥出来。discovery / peers / status 都只依赖它，彼此不再互调。

一条刻意没动的：连接池、注册表合并、共享项读改写共用装配期创建的那**一把锁**。迁移前
它们就是同一把，拆成多把会改变并发行为（后台探测与界面请求的互斥范围），所以结构拆开、
锁不拆 —— 这是"结构可以拆、并发语义不能顺手改"的典型例子。

### 断环通则

跨组件的环一律用「**把方法移到正确归属**」解决：谁拥有这份数据或状态，方法就归谁；
上层通过构造参数注入。禁止三种"看起来解决了"的做法：

- 函数内延迟导入（`import` 写在方法体里）——依赖方向没变，只是躲过了静态检查；
- `getattr(self, 'x', None)` 兜底——把"缺成员"变成静默退化（上面「配方修正」第 5 条就是
  真实事故：`getattr(host, 'list_roots')` 少一个成员，表现只是"额外图库里的文件不参与扫描"）；
- 在入口加转发方法把环藏起来——环仍在，只是从两个组件挪到了"组件 + 入口"之间。

判断归属的问题只有一个：**这个方法的全部输入，是不是都属于某个组件？** 如果它读的是
注册表加上连接池，那它属于"需要这两者"的那一层，而不是缺谁补谁。

## 顺带该做的（归属到上面某一步，不要另立一次重构）

- **写盘统一走 `json_cache`**（第 1、3 步）：`read_cached_json` / `write_cached_json` 把
  "临时文件 + `os.replace`"与"版本 + 指纹校验"收在一处。迁移前同一插件内两种写法并存
  （有的原子、有的直接 `open(w)`），直接覆写在进程被杀时会留下半截 JSON，缓存从此永久
  损坏，而表现只是"打开变慢"。
- **额外根与命名空间映射改成缓存的派生值**（第 2 步）：见 `paths.py` 的说明与实测数据。
- **`meta` 的写入路径收进 `ThumbStore` 并加锁**（第 3 步）：尺寸填充是多线程的、落盘在
  请求线程上，现状存在竞态窗口。
- **错误处理按组件归口**（第 4、7 步）：删掉"某个分片顺手改另一个分片状态"的写法（例如在
  `on_settings_changed` 里直接给 `thumb_cache` / `_meta_cache` / `_album_cache` 重新赋值），
  改成入口编排、组件各自 `rebind()` / `reset()`。

## 刻意不混在迁移里做的

- 把 `image_meta.json` / `albums_index.json` / `dirs_index.json` 换成 SQLite：这是独立的
  性能立项，混进重构会让 `git bisect` 分不清是重构回归还是存储回归。
- 改动设置 schema、API 形状、前端契约：迁移的验收前提就是"这些一个字都不变"。
- 在 `main.py` 之外新增 `__init__.py`：插件后端不是包，加了也不会被 importlib 使用，
  只会让后来的人以为可以 `import`。

## 验收清单（一个插件迁移完成时）

```bash
venv/Scripts/python.exe tools/check_plugin_layering.py       # 该插件不再有 warning
venv/Scripts/python.exe -m pyright plugins/<plugin>          # 0 条 mixin 伪影
venv/Scripts/python.exe tools/plugin_ci.py --plugin <plugin> # 本插件测试全绿
venv/Scripts/python.exe tools/check_plugins.py               # manifest 与设置 API 契约不变
```

四条同时成立才算完成。第 8 步（删豁免文件）是收尾的一步：豁免文件留着，门禁对该插件就只报
warning、不拦。

## 已知未做

以下三条是迁移收口时**明确知道但刻意没做**的事，写在这里是为了让"为什么没做"和
"什么时候该做"都能被查到，而不是变成某个人的记忆。

- **group-mesh 的本地 JSON 落盘还没统一走 `json_cache`**：`storage.py`、`uploads.py`、
  `materialize.py` 里仍是各自的写盘实现（其中 `uploads.py` 与 `materialize.py` 还是直接
  `write_text`，没有临时文件 + `os.replace`）。没顺手改是因为这些写入与协议对象、上传续传
  记录耦合，换存储机制会与本次结构重构的回归混在同一个 diff 里，`git bisect` 分不清。
  下次动这几个文件时按 `json_cache` 的统一口径重写一遍即可。
- **`Node._connection_count` 恒为 0 的死字段**：`group-mesh` 的 `get_node_status` 对外返回
  `connection_count`，读的就是这个字段；但除了 `Node.__init__` 里赋 0，没有任何地方写过它
  （真实连接数在 `ConnectionPool`）。迁移时保持行为不变，所以既没删字段、也没改成读连接池。
  两种修法都会改变对外返回的形状或语义，属独立改动：要么改成读连接池，要么把字段与对外
  字段一起删掉。
- **image-viewer 的 meta / 索引仍整份 JSON 重写**：`image_meta.json`、`albums_index.json`、
  `dirs_index.json` 每次变更都整份序列化，11 万张图时这是主要 IO 成本。迁到 SQLite 属于
  独立的性能立项（`shell/backend/thumb_cache.py` 的 `ThumbCache` 是现成参照），混进结构
  重构会让性能回归与重构回归无法区分。
