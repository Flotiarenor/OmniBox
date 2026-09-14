# 内核契约与门禁修复方案（P0 三项）

| 项目 | 内容 |
| --- | --- |
| 版本 | v1.0 |
| 日期 | 2026-09-14 |
| 基线 | `pyproject.toml` version = 1.2.0 |
| 状态 | 第 1 项已实施（2026-09-14）；第 2、3 项待实施 |
| 范围 | `tools/check_plugins.py`、`shell/backend/plugin_base.py`、`shell/backend/file_server.py`、`shell/frontend/public/shell/base.js`、`shell/frontend/src/App.vue`、`docs/plugin-guide.md` |
| 关联 | `docs/code-review.md`、`docs/plugin-guide.md`、`docs/core-direction.md`、`docs/image-tagger-design.md` |

本文所述三项均为内核级缺陷，不涉及插件业务逻辑。每项均给出问题陈述、逐条证据（含 `file:line`）、影响、修复要求与可执行的验收标准。

---

## 0. 摘要

| 序 | 事项 | 性质 | 阻断对象 | 预估 |
| --- | --- | --- | --- | --- |
| 1 | `runtime` 字段与字段读取方门禁矛盾 | 门禁缺陷 | 独立运行环境插件轴（`image-tagger`） | < 1 天 | 已实施 |
| 2 | 宿主↔附属插件契约未声明、未测试 | 契约缺陷 | Companion 体系的双向兼容 | 1–2 天 |
| 3 | 插件生命周期契约缺失 | 契约缺陷 | 全部插件的前端资源回收 | 1–2 天 |

---

## 0.1 第 1 项实施记录（2026-09-14）

| 改动 | 文件 |
| --- | --- |
| 摊平键保留父键语义：`_check_manifest_fields()` 查表未命中时逐级回退父键（`_parent_field()`），回退只在父键**确有登记**时生效（`_match_registered_ancestor()`） | `tools/check_plugins.py` |
| 新增 `CHECKER_ONLY_FIELDS`，登记仅由检查器消费的字段；命中的字段产出含"该字段运行时无效果"的 warning | `tools/check_plugins.py` |
| `frontend.entry` / `version` / `minShellVersion` / `kind` 自 `RUNTIME_FIELD_READERS` 移入 `CHECKER_ONLY_FIELDS`；`kind` 的重复告警分支删除，由字段级 warning 统一产出 | `tools/check_plugins.py` |
| §2.2 顶部标注"尚未实装，填入不生效"；字段表 `version` / `minShellVersion` / `runtime` / `kind` 行补齐"仅检查器读取"；三张登记表的含义与"表里如一"要求写成表格 | `docs/plugin-guide.md` |
| 追加 5 条用例：runtime 块无 error、回退不过宽、CHECKER_ONLY 字段告警含指定文本、`runtime` 未被登记为运行时读取方（原有的两条用例按新告警文本与分类同步更新） | `tests/test_plugin_spec.py` |

验证：

```bash
python tools/check_plugins.py                    # 退出码 0（31 个警告）
python -m unittest tests.test_plugin_spec -v     # 19 例通过
python -m unittest discover -s tests             # 全绿
python -m ruff check .                           # All checks passed
```

对 §1.2(a) 探针的回归结果：修复前 `_check_manifest_fields()` 对完整 `runtime` 块产出 6 条 error，
修复后为 0 条 error；`runtimeFoo` / `mystery` / `frontend.typo` / 两层以上嵌套的 `a.b.c` 仍产出 error。

---

## 1. `runtime` 字段与字段读取方门禁矛盾

### 1.1 问题陈述

`docs/plugin-guide.md` §2.2 与 `docs/image-tagger-design.md` §3 指导插件作者在 `manifest.json` 中声明 `runtime` 块（`kind` / `entry` / `venv` / `requirements` / `startup` / `timeoutSeconds`）。按该指导书写的 manifest **无法通过项目自身的规范检查器**：`tools/check_plugins.py` 对其报 6 条 error。

同时，`RUNTIME_FIELD_READERS` 中存在"检查器读取自身"的登记项，使"每个字段都必须有运行时读取方"这一规则可被空转满足。

### 1.2 证据

#### (a) 摊平结果与查表键不匹配

- `tools/check_plugins.py:232-241` `_flatten_manifest_fields()`：对 dict 值摊平一层，`runtime: {...}` 被展开为 `runtime.kind`、`runtime.entry`、`runtime.venv`、`runtime.requirements`、`runtime.startup`、`runtime.timeoutSeconds`。
- `tools/check_plugins.py:66-71` `DOC_ONLY_FIELDS`：仅登记裸键 `runtime`（`:70`），与上述摊平键永不匹配。
- `tools/check_plugins.py:266-279` `_check_manifest_fields()`：逐个摊平键先查 `RUNTIME_FIELD_READERS`、再查 `DOC_ONLY_FIELDS`，两表皆无则记 error。

实测（`_check_manifest_fields` 签名为 `(data: dict, where: str)`）：

```
_check_manifest_fields({'runtime': {'kind': …, 'entry': …, 'venv': …,
                                    'requirements': …, 'startup': …, 'timeoutSeconds': …}}, 'probe')
→ 6 条 error：
  manifest.runtime.kind / runtime.entry / runtime.venv /
  runtime.requirements / runtime.startup / runtime.timeoutSeconds
```

#### (b) 读取方登记表自我引用

`tools/check_plugins.py:59-62` 将下列字段的"读取方"登记为检查器自身：

| 字段 | 登记的读取方 |
| --- | --- |
| `frontend.entry` | `tools/check_plugins.py`（`frontend_entry`） |
| `version` | `tools/check_plugins.py`（`data.get('version')`） |
| `minShellVersion` | `tools/check_plugins.py`（`minShellVersion`） |
| `kind` | `tools/check_plugins.py`（`local-adapter`） |

`_check_reader_registry()`（`:244-259`）仅验证被登记的片段仍存在于读取方源码中，因此"检查器读自己"可稳定通过自检。

该机制的后果实例：`minShellVersion` 在全仓库仅出现于 `tools/check_plugins.py`（5 处），**运行时不读取**；而 `plugins/media-player/manifest.json` 已声明该字段，旧版 shell 加载该插件时既无告警，也不进入 `/status`。

### 1.3 影响

1. 阻断 `docs/core-direction.md` M2/M3 规划的独立运行环境插件轴：`image-tagger` 按设计文档实装后无法通过门禁。
2. 门禁规则（"字段必须有读取方"）可被检查器自身满足，`minShellVersion` 一类字段继续静默失效，与 `docs/code-review.md` §5 已修复的同类问题同源。

### 1.4 修复要求

#### 1.4.a 摊平须保留父键

`_flatten_manifest_fields()` 在展开嵌套 dict 的同时，应在结果中保留父键（或父键的存在性标记），使两张登记表既能按 `runtime` 匹配整块，也能按 `runtime.kind` 匹配单字段。

等价方案：查表未命中时逐级回退父键（`runtime.kind` → `runtime`）。**采用回退方案时须有测试保证回退不会过宽**（见 1.5）。

#### 1.4.b 决策点：`runtime` 的定位（二选一，须记录结论）

| 方案 | 内容 | 适用前提 |
| --- | --- | --- |
| A（推荐，最小改动） | 维持 `runtime` 为 DOC_ONLY，并在 `docs/plugin-guide.md` §2.2 顶部显式标注"尚未实装，填入不生效" | 近期不实装 runtime |
| B | 实装 `manifest.runtime` 的读取方（`docs/core-direction.md` §3.4、§3.5），随后登记进 `RUNTIME_FIELD_READERS` | 与第 2 项一并推进 |

**结论（2026-09-14，已实施）：采用方案 A。** 理由是本次修复的目标是"让按指南书写的 manifest 能过门禁"，
实装 `runtime` 读取方会把门禁修复与 `docs/core-direction.md` §3.4 / §3.5 的适配器实装绑在一起，扩大改动面。
`docs/plugin-guide.md` §2.2 顶部与字段表 `runtime` 行均已标注"尚未实装，填入不生效"；
实装后应把 `runtime` 从 `DOC_ONLY_FIELDS` 移入 `RUNTIME_FIELD_READERS`，并把 §2.2 的标注改为实装说明。

#### 1.4.c 登记表须区分"运行时读取方"与"检查器读取方"

新增独立集合（建议 `CHECKER_ONLY_FIELDS`）登记仅由检查器消费的字段；`_check_manifest_fields()` 对这类字段产出 warning 而非直接通过，warning 文本须包含"该字段运行时无效果"。

`frontend.entry` / `version` / `minShellVersion` / `kind` 归入该类。其中 `minShellVersion` 若短期不实装，须在 `docs/plugin-guide.md` 同步标注。

### 1.5 验收标准

1. `python tools/check_plugins.py` 退出码 0；对按 `docs/plugin-guide.md` §2.2 书写的 manifest 不再产出 `runtime.*` 相关 error。
2. 在 `tests/test_plugin_spec.py` 追加用例：
   - 含完整 `runtime` 块的 manifest 传入 `_check_manifest_fields()`，断言无 `runtime.*` error；
   - 含未知字段（如 `runtimeFoo`）的 manifest 仍产出 error（防止回退逻辑过宽导致规则失效）；
   - `CHECKER_ONLY_FIELDS` 中的字段产出 warning，且文本含"运行时无效果"。
3. `python -m unittest discover -s tests` 全绿。

---

## 2. 宿主↔附属插件契约未声明、未测试

### 2.1 问题陈述

Companion 插件通过宿主实例复用宿主能力（`plugins/image-cleaner/backend/main.py:50-58` 代理宿主的 `thumb_dir`、`ensure_thumb()`、`get_thumb_data()`）。这三个成员：

- 未出现在 `shell/backend/plugin_base.PluginBase` 中；
- 未被 `docs/plugin-guide.md` 完整记录（`ensure_thumb` 完全未记录）；
- 仅由 `shell/backend/file_server.py` 以 `getattr` 探针方式隐式定义；
- 无任何测试固定其形状。

因此宿主侧改动可静默破坏附属插件，即"双向兼容"目前无机制保障。

### 2.2 证据

#### (a) `PluginBase` 未声明这三个成员

`shell/backend/plugin_base.py` 的公开成员为：`get_data_root`、`get_file_roots`、`get_dependency`、`get_extensions`、设置系列（`get_settings` / `save_settings` / `setting` / `update_setting` / `clear_settings` / `on_settings_changed`）、生命周期钩子（`on_load` / `on_unload`）。

检索 `thumb_dir` / `ensure_thumb` / `get_thumb_data`：**0 命中**。

#### (b) 契约实际由 `file_server.py` 的探针定义

| 成员 | 探针位置 | 取用方式 |
| --- | --- | --- |
| `get_file_roots` | `shell/backend/file_server.py:312` | `getattr(instance, 'get_file_roots', None)` |
| `get_thumb_data` | `shell/backend/file_server.py:374-377` | `getattr(...)`，返回 `(bytes, mime)` |
| `thumb_dir` | `shell/backend/file_server.py:391-398` | `getattr(...)`，随后 `Path(thumb_dir).resolve()` |
| `ensure_thumb` | `shell/backend/file_server.py:401` | `getattr(...)`，调用即生成 |

同文件 `:310` 与 `:389` 直接读取私有属性 `plugin_manager._instances`，而 `:305` / `:368` 使用的是公开方法 `get_plugin_instance()`。`:390` 另有 `assert`（`python -O` 下会被剥离，不可作为校验手段）。

#### (c) 各插件定义的形状不一致

| 位置 | 定义 |
| --- | --- |
| `plugins/image-viewer/backend/main.py:107`、`:1190` | `self.thumb_dir = self.cache_dir / 'thumbs'`（**实例属性，且在两处赋值**） |
| `plugins/image-viewer/backend/main.py:192`、`:202` | `ensure_thumb()`、`get_thumb_data()` |
| `plugins/image-cleaner/backend/main.py:51`、`:54`、`:57` | 以 `@property` 与转发方法代理宿主同名成员 |
| `plugins/media-player/backend/main.py:110` | 仅 `get_thumb_data()`；无 `thumb_dir`，无 `ensure_thumb` |

#### (d) 文档覆盖不完整

| 成员 | `docs/plugin-guide.md` 中的记录 |
| --- | --- |
| `get_thumb_data` | `:413-414`、`:767-780` 已记录（含推荐模式） |
| `thumb_dir` | `:783-787` 仅作为"旧版散文件模式（兼容）"提及 |
| `ensure_thumb` | **0 命中** |

### 2.3 影响

1. **形状漂移导致整条路由 500。** `file_server.py:391-393` 以真值判断 `thumb_dir`；若宿主改为方法，`getattr` 得到 bound method（真值），跳过 `:392-393` 的回退分支，在 `:398` 执行 `Path(bound_method)` 抛 `TypeError` → `/thumbs` 返回 500，影响所有插件而非单一插件。
2. **同一宿主内两处赋值可漂移。** `image-viewer/main.py:107` 与 `:1190` 各自赋值 `self.thumb_dir`，改一处而漏另一处将产生两个目录，且无告警。
3. **附属插件依赖未声明的私有约定。** `image-cleaner` 代理的三个成员均非基类契约，宿主内部重构无回归保护。
4. **内核耦合业务约定。** `file_server.py` 依赖 `.cache/thumbs` 布局与鸭子类型成员，`shell/backend` 无法与插件解耦。

### 2.4 修复要求

#### 2.4.a 将四个成员提升为 `PluginBase` 正式成员

在 `PluginBase` 中声明并给出默认实现：

| 成员 | 默认实现 | 返回类型 |
| --- | --- | --- |
| `get_file_roots()` | 保持现状 `[self.get_data_root()]` | `List[Path]` |
| `get_thumb_data(rel_path)` | 返回 `None`（语义：本插件不提供缩略图字节） | `tuple[bytes, str] \| None` |
| `thumb_dir` | `self.get_data_root() / '.cache' / 'thumbs'`（与 `file_server.py:393` 现状一致，消除重复） | `Path`（属性或只读 property，二者择一并写入文档） |
| `ensure_thumb(rel_path)` | 空实现 | `None` |

#### 2.4.b `file_server.py` 移除私有访问与鸭子类型探针

- `:310`、`:389` 改用具名公开方法，不再读取 `plugin_manager._instances`。
- `:312`、`:374`、`:391`、`:401` 的 `getattr` 探针改为直接调用基类成员。
- `:391-393` 的回退分支须显式化：仅在插件使用 `thumb_dir` 默认值时才回退全局根，且回退条件不得依赖真值判断。
- 移除 `:390` 的 `assert`。

#### 2.4.c 决策点：成员优先级

`media-player` 未定义 `thumb_dir`，其 `get_thumb_data()` 直接返回字节。基类默认实现须保证该插件行为不变：**`get_thumb_data()` 命中时优先，未命中或返回 `None` 时才使用 `thumb_dir`**。该优先级须写入 `docs/plugin-guide.md` §7.2。

#### 2.4.d 契约回归测试

新增 `tests/test_plugin_host_contract.py`，以伪宿主覆盖：

- 宿主未覆写任何成员时的默认行为；
- 宿主仅覆写 `get_thumb_data`（`media-player` 形态）；
- 宿主将 `thumb_dir` 定义为 property 与定义为方法两种形态，断言第二种被显式拒绝或归一化，且不产生 500；
- `image-cleaner` 形态（代理宿主三成员）经 `/thumbs` 路由的端到端结果。

### 2.5 验收标准

1. `shell/backend/plugin_base.py` 中存在上述四个成员的声明与默认实现，`thumb_dir` / `get_thumb_data` 的返回类型写入类型注解。
2. `grep -n '_instances' shell/backend/file_server.py` 无命中。
3. `grep -n "getattr(instance, '\(get_file_roots\|get_thumb_data\|thumb_dir\|ensure_thumb\)'" shell/backend/file_server.py` 无命中。
4. `python -m unittest tests.test_plugin_host_contract -v` 全绿。
5. `python -m pyright main.py shell tools` 保持 0 error；`python tools/check_plugins.py` 退出码 0。
6. 手动验证：`image-viewer` 相册页缩略图、`image-cleaner` 扫描页缩略图、`media-player` 列表封面三者正常显示。

---

## 3. 插件生命周期契约缺失

### 3.1 问题陈述

除 `on_load()` / `on_unload()` 外，插件前端无法获知自身 iframe 的可见性变化。默认情况下所有插件 iframe 常驻（keep-alive，以 `v-show` 隐藏），因此插件切换到后台后，其定时器、`requestAnimationFrame` 自循环与轮询继续运行。同时插件前端无统一的销毁钩子，导致事件监听器只增不减。

### 3.2 证据

#### (a) 全部插件默认常驻，且无插件声明 `destroyOnLeave`

- `shell/frontend/src/App.vue:129-130` 按 `destroyOnLeave` 分流。
- `:165-176` 常驻组以 `v-show="activePlugin === p.name"` 渲染（DOM 保留）。
- `:177-188` 销毁组以 `v-if` 渲染。
- 检索 `destroyOnLeave` 于 `plugins/*/manifest.json`：**无命中** → 7 个插件全部属于常驻组。
- `readme.md:44` 明确记载常驻为有意设计（"切换时媒体播放不中断"）。**因此修复方向不是"离开即销毁"，而是提供可见性通知。**

#### (b) 内核→插件仅一种消息，且行为为强制重载

`shell/frontend/public/shell/base.js:7-12`：唯一的 `window.addEventListener('message', …)` 处理 `omnibox:settings-changed`，行为是整页重载：

```javascript
window.location.href = window.location.href.split('?')[0] + '?_t=' + Date.now();
```

不存在"隐藏 / 显示"通知。该 handler 亦未校验 `event.origin` / `event.source`。

#### (c) 插件侧无销毁钩子

- `shell/backend/plugin_base.py` 仅提供 `on_load` / `on_unload`。
- `shell/frontend/public/shell/base.js` 未提供任何 `dispose` / `onHide` / `onShow` 注册入口。

#### (d) 监听器只增不减

实测 `plugins/**/*.js`（排除 vendor）中 `addEventListener` 与 `removeEventListener` 计数：

| 文件 | add | remove |
| --- | --- | --- |
| `media-player/js/app.js` | 60 | 2 |
| `image-viewer/js/app.js` | 35 | 0 |
| `manga-library/js/app.js` | 20 | 0 |
| `novel-reader/js/app.js` | 20 | 0 |
| `image-cleaner/js/app.js` | 10 | 0 |
| `media-player/js/player-core.js` | 7 | 0 |
| `netease-music/js/app.js` | 6 | 0 |
| `manga-library/js/reader.js` | 5 | 0 |
| `media-player/js/frame-extractor.js` | 3 | 2 |
| `media-player/js/lyrics-parser.js` | 3 | 0 |
| `media-player/js/playlist-manager.js` | 2 | 0 |
| **合计** | **171** | **4** |

#### (e) 常驻期间持续运行的实例

| 位置 | 内容 |
| --- | --- |
| `plugins/image-viewer/frontend/js/app.js:863` | `setInterval(() => this.lightbox.navigate(1), 3000)`（幻灯片） |
| `plugins/manga-library/frontend/js/app.js:27` | `setInterval(() => this.loadTasks(), 2000)`（任务轮询） |
| `plugins/media-player/frontend/js/player-core.js:329` | `setInterval(() => this._saveProgress(), 2000)` |
| `plugins/media-player/frontend/js/lyrics-parser.js:247`、`:250` | `requestAnimationFrame(draw)` 自循环 |

### 3.3 影响

1. 切换到其他插件后，上述定时器与自循环继续消耗 CPU 并持续发起桥接调用（`manga-library` 为每 2 秒一次）。
2. 监听器随插件存活期累积（171 : 4），每次改动都在增加后续成本；对更新频率最高的 `image-viewer` 影响最大。
3. 插件自身的清理逻辑依赖作者自觉，无机制约束；新增插件将继承同一问题。

### 3.4 修复要求

#### 3.4.a 前端生命周期钩子（`base.js`）

在 `base.js` 中新增注册入口：

| 钩子 | 触发时机 |
| --- | --- |
| `onShow()` | iframe 由隐藏转为显示 |
| `onHide()` | iframe 由显示转为隐藏（常驻组） |
| `onDispose()` | iframe 即将销毁（销毁组，或页面卸载） |

#### 3.4.b 宿主侧通知来源

`App.vue` 在 `activePlugin` 变化时，向"刚变为非活动"的 iframe 发送 `postMessage`（建议 `omnibox:plugin-hidden`），向"刚变为活动"的发送 `omnibox:plugin-shown`。须同时处理窗口 `visibilitychange`（最小化 / 切换标签页）与 `beforeunload`。

#### 3.4.c 消息须校验来源

`base.js:8-12` 现有 handler 未校验来源。新增的隐/显通知与现有 `omnibox:settings-changed` 均须校验 `event.source === parent`（同源场景下 `origin` 校验不足以防嵌套 frame 伪造）。

#### 3.4.d 决策点：媒体播放的例外语义

常驻是 `media-player` 的既定需求（`readme.md:44`）。因此 `onHide` 的语义应定义为"停止视觉与轮询类工作，不强制停止播放"，由插件自行决定。`media-player` 应在 `onHide` 中停止 `lyrics-parser` 的 rAF 自循环，保留 `player-core` 的进度保存。

#### 3.4.e 至少迁移一个插件作为范式

优先改造 `image-viewer`（更新频率最高、监听器最多、存在幻灯片定时器不清理的已知缺陷），并在 `docs/plugin-guide.md` 新增"前端生命周期"章节，说明三个钩子与 `destroyOnLeave` 的关系。

### 3.5 验收标准

1. `base.js` 导出上述三个注册入口，并有对应 JSDoc 说明触发时机。
2. `App.vue` 在插件切换、窗口 `visibilitychange`、`beforeunload` 三种情形下均发出正确通知；测试可置于 `tests/js/`，参照 `tests/js/media_player_playback.mjs` 的 vm + stub 范式。
3. `image-viewer` 在 `onHide` 中停止幻灯片定时器；实测：启动幻灯片后切换到其他插件，3 秒间隔的换图行为停止。
4. `image-viewer/frontend/js/app.js` 的 `addEventListener` 与 `removeEventListener` 计数比不再是 35 : 0。
5. `python -m unittest discover -s tests` 全绿；`node tools/check_frontend_escape.cjs` 通过。

---

## 4. 实施顺序与依赖

1. **第 1 项先行。** 改动仅限 `tools/check_plugins.py` 与文档，无运行时风险，且解除对独立运行环境插件轴的阻断。
2. **第 2、3 项可并行，但建议同一人实施。** 两者均涉及 `PluginBase` 与前端注入层（`file_server.py` / `base.js` / `App.vue`），并行改易冲突。
3. **第 2 项完成前不应新增 Companion 插件。** 契约未固定时新增即固化风险。
4. **第 3 项完成前不应扩大插件数量。** 监听器与定时器问题随插件数等比放大。

## 5. 明确排除的范围

| 事项 | 排除理由 |
| --- | --- |
| `plugins/netease-music/frontend/js/app.js:87` 的 `get_song_url` 参数错位 | 仅影响该插件的独立页面；日常路径（`media-player` 原生视图，`media-player/frontend/js/app.js:558`、`:1087` 传两参数）不经过此调用 |
| `plugins/image-cleaner` 的 `similar_scan` 阻塞请求线程 | 属插件内实现问题，非内核契约 |
| `plugins/novel-reader`、`plugins/manga-library` 的测试与原子写 | 两插件变更频率低、依赖低，暂不投入 |
| 大文件拆分（如 `media-player/frontend/js/app.js` 1990 行） | 属独立议题，不在本次三项之内 |
| 宿主实例的其它私有访问（`system_get_config` 等） | 属 `docs/code-review.md` 既有条目，另行处理 |

## 附录 A：事实核对命令

```bash
# 第 1 项
python tools/check_plugins.py
python -m unittest tests.test_plugin_spec -v

# 第 2 项
grep -n '_instances' shell/backend/file_server.py
grep -rn 'def thumb_dir\|def ensure_thumb\|def get_thumb_data' shell/backend/ plugins/*/backend/*.py
grep -n 'thumb_dir\|ensure_thumb\|get_thumb_data' docs/plugin-guide.md

# 第 3 项
grep -rn 'setInterval\|requestAnimationFrame(' plugins/*/frontend/js/*.js
grep -c 'addEventListener' plugins/*/frontend/js/*.js
grep -rn 'destroyOnLeave' plugins/*/manifest.json shell/frontend/src/App.vue
```
