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
'''

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional, Tuple

from shell.backend.protected_paths import is_protected

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from shell.backend.plugin_manager import PluginManager
    from shell.backend.settings_store import SettingsStore

# 设置项 schema 的字段类型：text / number / range / select / checkbox / textarea / directory
#   directory：目录列表（主要/额外 + 浏览…），由 Shell 共享组件 window.FolderPicker
#   渲染（shell/frontend/public/shell/folder-picker.js，与 image-viewer 同一份实现）；
#   值仍是字符串（multi 字段换行分隔），插件侧不用写代码，见 docs/plugin-guide.md §8.2
# 每个设置项示例：
#   {"key": "per_page", "label": "每页数量", "type": "number",
#    "default": 40, "min": 1, "max": 500, "help": "说明文字"}
#   {"key": "sort_by", "label": "排序方式", "type": "select",
#    "options": [{"label": "修改时间", "value": "mtime"}, {"label": "文件名", "value": "name"}]}
# 可选字段：
#   "central": False   — 不在集中设置面板显示（默认显示）
#   "help": "..."      — 设置面板悬浮提示
#   "secret": True     — 凭据类设置项。两件事一起生效：
#                        ① get_settings() 对外返回掩码（本方法会经 register_api
#                           直接暴露给 HTTP，返回明文等于把凭据交给任何同源脚本）；
#                        ② 该插件的设置文件不参与文件服务
#                           （PluginBase.get_protected_paths）。
#                        见 docs/plugin-guide.md §8.2
#
# 注意：docs/adapter-spec.md 中描述的 adapter_* 方法目前处于规划阶段，
# 尚未在本基类实现；adapter_process.py 也不应提前引入。

# 凭据类设置项对外返回时的掩码。选一个用户不会真的输入、且一眼能看出是占位符的
# 值：前端把 get_settings() 的结果回填进输入框后，用户看到它就知道"已配置，
# 但不明文显示"。把它原样提交回来表示"不改动"，见 save_settings()。
SECRET_MASK = '********'


def secret_keys(schema) -> set:
    """schema 里声明了 `"secret": True` 的设置键（凭据类）。"""
    return {
        str(item['key']) for item in (schema or [])
        if isinstance(item, dict) and item.get('secret') and item.get('key')
    }


def mask_secrets(schema, values):
    """把 schema 声明的凭据类键替换成 `SECRET_MASK`（非字典原样返回）。

    **Shell 侧也调用这个函数**（`PluginManager` 在把 `<插件>__get_settings` 与设置
    面板的返回值交给前端之前再掩一次）：插件可以覆写 `get_settings()`，
    基类的掩码就整个失效了（image-viewer / manga-library 都覆写了）。掩码只写在
    基类等于把"不泄露凭据"寄托在每个插件的实现细节上。
    """
    if not isinstance(values, dict):
        return values
    keys = secret_keys(schema)
    if not keys:
        return values
    masked = dict(values)
    for key in keys:
        if masked.get(key):
            masked[key] = SECRET_MASK
    return masked


class PluginBase(ABC):
    # 子类覆盖：声明该插件的统一设置项。
    # ClassVar 是必要的：它是类级共享的常量声明，不是每个实例各自持有的可变状态
    # （不加 ClassVar 会被 RUF012 判为"把可变对象当类属性默认值"的隐患）。
    settings_schema: ClassVar[List[Dict[str, Any]]] = []

    def __init__(self, manifest: dict, config: dict) -> None:
        self.manifest = manifest
        self.config = config
        self.name = manifest.get('name', 'unknown')
        # 由 PluginManager 注入
        self._settings_store: SettingsStore | None = None
        self._plugin_manager: PluginManager | None = getattr(self.__class__, '_plugin_manager', None)
        # PluginManager 在构造前预加载的已解决设置
        self._resolved_config: Dict[str, Any] = getattr(self.__class__, '_resolved_config', {})
        # thumb_dir 的显式覆盖值：None 表示"跟随 get_data_root()"（见 thumb_dir property）。
        # 必须在 __init__ 里显式初始化：image-viewer 的 get_data_root() 读 self.root_dir，
        # 而 self.thumb_dir 的 getter 会回落到 get_data_root() —— 不在构造期先建好这个
        # 字段，插件 __init__ 里首次访问 thumb_dir 就可能读到尚不存在的属性。
        self._thumb_dir_override: Path | None = None

    @abstractmethod
    def register_api(self) -> Dict[str, Callable]:
        pass

    def get_data_root(self) -> Path:
        """返回该插件使用的数据根目录，默认使用全局配置"""
        return Path(self.config['directories']['data_root']).resolve()

    def get_file_roots(self) -> List[Path]:
        """返回文件服务允许访问的根目录列表。

        需要跨多个媒体目录提供文件的插件（如 media-player）可覆写本方法，
        返回所有已配置的根目录。`/files/` 路由会依次进行路径安全检查。
        """
        return [self.get_data_root()]

    def resolve_file_path(self, rel_path: str) -> Path | None:
        """把 `/file?plugin=X&path=<相对路径>` 的相对路径解释成本机物理路径。

        默认返回 `None`，表示"按老规矩办"：Shell 用 `get_file_roots()[0]` 拼出路径。
        需要**虚拟路径**的插件（多根目录、命名空间前缀）可以覆写本方法，让 Shell
        按插件自己的映射去解析 —— 否则诸如 `__额外图库/作者B/图.jpg` 这种路径在
        第一根下根本不存在，表现就是"网格与缩略图正常，点开原图 404"。

        约束（Shell 侧执行，插件无法绕过）：

        - 返回值**必须**落在 `get_file_roots()` 的某一根之内，否则 403（与越界同一语义）；
        - 仍然先过受保护路径判定，插件申报的凭据文件即使被解析出来也返回 403；
        - 返回 `None`、抛异常或返回非 `Path` 时一律回退到默认解析，**不得**让整条
          路由 500（形状不可信的返回值由 Shell 归一化）。

        `/thumbs` 早就是这条契约（路径交给插件解释），本方法是 `/file` 与之对齐。
        """
        return None

    # ===== 受保护路径契约（插件"申请"、Shell 执行）=====
    #
    # 定位：插件**自己申报**哪些路径是敏感内容，Shell 在文件路由上无条件拒绝把
    # 它们端出去。申报是可选能力 —— 不申报的插件行为完全不变。
    #
    # 为什么需要"申报"这一步：文件路由的放行依据是插件自己给出的根
    # （get_file_roots / thumb_dir），而根可以由插件设置改写。于是"根一旦覆盖了
    # 某个凭据文件，凭据就落在合法范围内"是结构性的，Shell 无法靠猜来避免。
    # 唯一可靠的信息来源就是插件自己说清楚 —— 但它只说"是什么"，不说"怎么防"，
    # 执行点始终在 Shell，插件侧没有任何绕过或关闭防护的接口。
    #
    # 与"限制插件怎么写"的区别：这**不**约束插件读写哪些文件（插件后端与 Shell
    # 同进程，任何声明都拦不住它直接读文件）。它只保证"插件自己不想要的泄露，
    # Shell 保证不会发生"。

    def get_protected_paths(self) -> List[Path]:
        r"""申请 Shell 文件防护：返回不得被 `/file`、`/files`、`/thumbs` 返回的路径。

        默认实现：`settings_schema` 里任何一项声明了 `"secret": True` 时，返回本
        插件的统一设置文件（凭据就存在那里）。因此**常见情况下一行都不用写** ——
        只需给对应的设置项加 `"secret": True`。

        插件若把敏感内容放在别处（自己的令牌文件、cookie jar、加密密钥等），覆写
        本方法返回那些路径即可，Shell 侧无需改动：

            def get_protected_paths(self):
                return [*super().get_protected_paths(), self.get_cache_dir() / 'token.json']

        约束与注意事项：

        - 只应返回**本插件自己的数据根目录或 `<config>/plugins` 之下**的路径。
          Shell 会校验这一边界，越界的申报被忽略并记 warning（防误用：一个笔误
          不该把整个文件服务钉死）。
        - 声明**目录**表示"该目录及其下全部内容"都受保护。
        - **不要**声明 `get_data_root()` 这样的宽目录：媒体的数据根往往就是媒体根，
          一起挡掉会让本插件自己的封面/缩略图也 404。只声明凭据文件。
        - 被拒绝的请求返回 `403`（与越界访问同一语义），所以插件前端不需要为它写
          特殊处理 —— 与已有的"403 就显示占位图"路径自然衔接。
        - 这条防护同时作用于**写/删**：插件在 `delete_files` / `move_files` 这类
          不可逆操作前应调用 `self.is_protected_path(...)` 自查（Shell 拦不到
          `/api` 下的插件方法）。
        """
        if not any(isinstance(item, dict) and item.get('secret') for item in self.settings_schema):
            return []
        if self._settings_store is None:
            # 静默失效比没有防护更危险：凭据已声明 secret，却没有可申报的设置文件
            log.warning(f"[{self.name}] 声明了 secret 但没有设置存储，凭据防护未生效")
            return []
        try:
            return [self._settings_store.path_for(self.name)]
        except (TypeError, ValueError) as e:
            log.warning(f"[{self.name}] 无法解析设置文件路径，凭据防护未生效: {e}")
            return []

    # ===== 缩略图契约（Shell 的 /thumbs 路由消费）=====
    #
    # 这三个成员曾经只由 file_server.py 以 getattr 探针隐式定义，既不在本基类、
    # 也没有测试固定形状：宿主侧把 thumb_dir 改成方法就能让 /thumbs 整体 500
    # （探针拿到 bound method 是真值 → 跳过回退 → Path(bound_method) 抛 TypeError），
    # 而 Companion 插件（image-cleaner）代理的正是这些未声明成员。
    # 现在它们是显式契约：形状、默认值与优先级都在这里定义，file_server 直接调用，
    # 不再做鸭子类型探测。

    def get_thumb_data(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        """返回缩略图字节，供 `/thumbs` 路由直接响应。默认返回 None（本插件不提供）。

        返回 `(data, mime)` 二元组；返回 None 时 `/thumbs` 回退到 `thumb_dir` 散文件布局。
        **优先级：本方法命中优先，未命中或返回 None 时才使用 thumb_dir。** 因此
        media-player 这类"只覆写 get_thumb_data、不定义 thumb_dir"的插件行为不变。
        """
        return None

    @property
    def thumb_dir(self) -> Path:
        """缩略图散文件目录，默认 `数据根目录/.cache/thumbs`。

        只读 property（契约要求二者择一，这里选 property）：默认值与 get_data_root()
        的派生关系固定，不允许各插件各拼一份路径。需要换目录的插件改 get_data_root()；
        也兼容旧写法 `self.thumb_dir = <path>` —— 赋值意味着"就用这个目录"，读取始终
        走本 property，宿主与插件不会看到两个不同的路径（见 setter）。
        """
        if self._thumb_dir_override is not None:
            return self._thumb_dir_override
        return self.get_data_root() / '.cache' / 'thumbs'

    @thumb_dir.setter
    def thumb_dir(self, value: Path | str | None) -> None:
        # 兼容旧插件的 `self.thumb_dir = <path>` 写法：image-viewer 在 __init__ 与
        # 切换 root_dir 时都会赋值。值按"完整目录路径"理解（赋什么就读到什么），
        # 传 None 表示恢复默认派生（跟随 get_data_root()）。
        self._thumb_dir_override = Path(value).resolve() if value is not None else None

    def ensure_thumb(self, rel_path: str) -> None:  # noqa: B027 - 有意的非抽象空钩子
        """按需生成缩略图（`/thumbs` 找不到文件时调用）。默认什么都不做。

        插件可覆写为"现场生成并落盘"；返回值被忽略，生成失败不应抛异常。
        """

    def is_content_placeholder(self, path: Path | str) -> bool:
        r"""该路径是否"存在但内容还没真正取到本地"（`/file` 每次请求都会问一次）。

        为什么需要它，而不是只靠"文件不存在"来判断：**物化出来的占位文件是存在的**
        （0 字节）。实测踩到的正是这一点 —— `/file` 只在"文件不存在"时回调
        `ensure_file`，于是占位文件被当作正常文件直接返回，客户端拿到 200 + 0 字节，
        远端取回永远不会发生。所以调用方必须能区分"真的没有"与"有壳没内容"。

        返回 True 时 Shell 会接着调用 `ensure_file`。默认返回 False（内容都在本地）。

        实现约定：这个方法会在**每个** `/file` 请求上被调用，必须廉价（一次
        `stat` + 路径归属判断，别在这里做网络或全目录扫描）。
        """
        return False

    def ensure_file(self, path: Path | str) -> None:  # noqa: B027 - 有意的非抽象空钩子
        r"""按需把内容取到本地（`/file` 找不到文件时调用）。默认什么都不做。

        与 `ensure_thumb` 是同一形状的钩子，区别在于服务的路由不同：这个作用于
        `/file`（原图/媒体/任意文件），那个作用于 `/thumbs`。

        为什么需要它：插件的内容不必都在本地。group-mesh 这类"另一台机器上的库"
        的插件，把远端共享项**物化**成本地目录（目录结构与文件占位先落地，字节按需
        取回）—— 消费方插件扫描时看到的是真实目录，读到某个文件时由这里现取。
        没有这个钩子，"浏览远端目录"就只能做成插件私有的浏览界面，image-viewer /
        media-player 这些按"本地路径"工作的插件永远接不进来。

        实现约定：
          * `path` 是**已通过根校验与受保护判定**的绝对路径（Shell 先校验再回调，
            顺序与 `ensure_thumb` 一致）—— 插件不需要再判越界；
          * 放不进本地或取不到时**直接返回**，不要抛异常：调用方会继续走它自己的
            404 分支，异常只会把一个正常的"没有这个文件"变成 500；
          * 这个方法会在请求线程上被调用，实现方自己负责超时与限额（远端不可达时
            不能让请求线程无限等待）。
        """

    def get_dependency(self, name: str) -> PluginBase | None:
        """返回已加载依赖插件实例；未声明依赖或未加载时返回 None。"""
        dependencies = self.manifest.get('dependencies', []) or []
        if name not in dependencies:
            log.info(f"[{self.name}] 尝试访问未声明依赖的插件: {name}")
            return None
        if self._plugin_manager is None:
            return None
        return self._plugin_manager.get_plugin_instance(name)

    def get_extensions(self) -> List[dict]:
        """宿主前端可渲染的动作。默认空。"""
        return []

    def _default_settings(self) -> Dict[str, Any]:
        return {str(item["key"]): item.get("default") for item in self.settings_schema if item.get("key")}

    def _secret_keys(self) -> set:
        """schema 里声明了 `"secret": True` 的设置键（凭据类）。"""
        return secret_keys(self.settings_schema)

    def is_protected_path(self, path) -> bool:
        r"""该路径是否属于 Shell 的受保护清单（壳凭据 + 全插件申报）。

        供插件在**不可逆**操作（删除 / 移动 / 覆盖）之前自查：读路由由 Shell 拦，
        但 `/api/<插件>__<方法>` 里的删除移动是插件自己实现的，Shell 拦不到 ——
        不查就会绕过同一份清单（image-viewer 的 `delete_folder` 曾能删掉
        `<config>/plugins` 与 `auth_token.txt`，而"读"是挡住的）。

        判定实现与 Shell 的读路由共用 `shell/backend/protected_paths.py`
        （归一化 + samefile，见那里的说明）。**判定失败时返回 True**：这类操作
        不可逆，宁可拒绝也不能在异常路径上放行。
        """
        try:
            return is_protected(path, self._plugin_manager)
        except Exception as exc:
            log.warning(f"[{self.name}] 受保护路径判定失败，按受保护处理: {exc}")
            return True

    def _raw_settings(self) -> Dict[str, Any]:
        """未脱敏的设置（合并默认值）。

        **仅供内部使用**：变更检测与插件自身的凭据读取都走这里。对外的
        `get_settings()` 会把凭据类键替换成掩码 —— 两者不能混用，否则要么凭据
        泄露，要么每次保存都把凭据判成"已变更"。
        """
        stored = self._settings_store.get(self.name) if self._settings_store else {}
        return {**self._default_settings(), **stored}

    def get_settings(self) -> Dict[str, Any]:
        """从统一设置存储读取设置（合并默认值，凭据类键已脱敏）。

        **schema 里声明 `"secret": True` 的键在这里被替换成 `SECRET_MASK`。**
        这一步必须做在基类、而不是让各插件自己小心：本方法的返回值会经
        `register_api()` 直接暴露成 `POST /api/<插件>__get_settings`，而插件 iframe
        与壳同源 —— 返回明文就等于把长期凭据交给任何一段同源脚本，它也就绕开了
        "壳拒绝把设置文件当媒体资源返回"那层防护（直接问 API 即可）。

        掩码只在**值非空**时替换：未配置的凭据保持 schema 的 default（未声明时是
        None），前端据此显示"未配置"，不需要为脱敏另加一条协议（"是否已配置"另有
        token_configured 之类只读信号）。

        子类可覆盖，但必须调用 super() 以保证 on_settings_changed 检测正确；
        即便忘了调用，Shell 侧在 `<插件>__get_settings` 的出口还会再掩一次
        （`PluginManager`，见 `mask_secrets`），覆写不会造成明文泄露。
        """
        return mask_secrets(self.settings_schema, self._raw_settings())

    def save_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """校验、写入 SettingsStore、检测变更、调用 on_settings_changed。
        子类一般不需要覆盖此方法——覆盖 on_settings_changed() 即可响应设置变更。"""
        if not isinstance(settings, dict):
            return {"success": False, "error": "设置必须是字典"}

        # 变更检测必须用未脱敏的值：拿掩码与真值比较会把凭据每次都判成"已变更"，
        # 于是每次保存都触发 on_settings_changed（pixiv-sync 会据此重建客户端）。
        old = self._raw_settings()
        allowed = {item['key'] for item in self.settings_schema if item.get('key')}
        # 一律复制：不要与调用方传入的对象共享引用（下面会 pop）
        clean = {k: v for k, v in settings.items() if k in allowed} if allowed else dict(settings)

        # 掩码原样回传 = "不改动"。前端会把 get_settings() 的值回填进输入框再整体
        # 提交，若把掩码当新值写入，凭据就被静默覆盖成一串星号（用户下次同步时
        # 才发现要重新登录）。传空字符串仍然是"清除"——撤销凭据把输入框清空即可。
        for key in self._secret_keys():
            if clean.get(key) == SECRET_MASK:
                clean.pop(key)

        if self._settings_store:
            try:
                # 必须"合并写入"而不是"整文件覆盖"：插件会用 update_setting() 把
                # 运行期状态（pixiv-sync 的 refresh_token、image-viewer 的 folders、
                # media-player 的 media_set_config 等）写进**同一个** JSON，这些键
                # 不在 settings_schema 里。整文件覆盖会让用户在设置面板点一次保存，
                # 就静默抹掉这些状态（不报错、无日志）——必须保留。
                merged = self._settings_store.update(self.name, clean)
            except Exception as e:
                return {"success": False, "error": f"保存失败: {e}"}
        else:
            merged = clean

        # 检测变更并通知插件：只上报本次提交涉及的键，但取值来自合并后的最终状态
        # （jsonify 之类的钩子可能补写额外键，旧实现同样会把它们算进 changed）。
        changed = set()
        for k in clean:
            new_val = merged.get(k)
            old_val = old.get(k)
            if isinstance(old_val, float) and isinstance(new_val, (int, float)):
                if abs(old_val - new_val) > 0.001:
                    changed.add(k)
            elif old_val != new_val:
                changed.add(k)
        for k in set(merged) - set(clean):
            if old.get(k) != merged[k]:
                changed.add(k)

        if changed:
            try:
                self.on_settings_changed(changed)
            except Exception as e:
                log.error(f"[{self.name}] on_settings_changed 异常: {e}")

        return {"success": True}

    def on_settings_changed(self, changed_keys: set) -> None:  # noqa: B027 - 可选钩子
        """设置变更时由 save_settings 自动调用。子类覆盖此方法以响应特定设置变更。

        示例:
            def on_settings_changed(self, changed_keys):
                if 'root_dir' in changed_keys:
                    self._reinit(self.setting('root_dir'))
        """

    def setting(self, key: str, default: Any = None) -> Any:
        """读取单个设置项。SettingsStore（运行时）→ _resolved_config（启动时预设）→ schema.default → 传入 default"""
        if self._settings_store:
            stored = self._settings_store.get(self.name)
            if key in stored:
                return stored[key]
        if key in self._resolved_config:
            return self._resolved_config[key]
        for item in self.settings_schema:
            if item.get('key') == key:
                return item.get('default')
        return default

    def update_setting(self, key: str, value: Any) -> bool:
        """更新单个设置项（保留其他设置不变），不校验 schema 以支持运行时状态持久化"""
        if self._settings_store:
            current = self._settings_store.get(self.name) or {}
            current[key] = value
            try:
                self._settings_store.set(self.name, current)
                return True
            except Exception:
                return False
        return False

    def clear_settings(self) -> Dict[str, Any]:
        """清空统一设置存储中的该插件设置"""
        if self._settings_store:
            self._settings_store.clear(self.name)
        return {"success": True}

    # on_load / on_unload 是**可选**钩子，因此刻意不加 @abstractmethod：
    # 插件只实现自己需要的那一个（只读插件没有收尾需求），强制实现反而会
    # 让最小插件多写两个空方法。
    def on_load(self) -> None:  # noqa: B027 - 有意的非抽象空钩子
        pass

    def on_unload(self) -> None:  # noqa: B027 - 有意的非抽象空钩子
        pass
