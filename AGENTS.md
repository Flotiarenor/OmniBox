# AGENTS.md —— OmniBox 仓库内的 Agent 工作约定

本文件由支持 `AGENTS.md` 的工具（含 DSH）在工作区根目录自动加载，是本仓库对
agent 的完整工作约定；细则与判定依据见 [提交信息规范](./docs/commit-convention.md)、
[CI、打包与发布](./docs/ci-and-release.md)。

## 项目要点

- 桌面应用框架：Python 后端（`shell/backend/`）+ Vue 3 前端（`shell/frontend/`），
  运行时可选 PyInstaller 冻结；插件在 `plugins/<name>/`（`manifest.json` +
  `backend/` + `frontend/`）。
- 版本号唯一来源是 `pyproject.toml` 的 `[project].version`，
  `shell/frontend/package.json` 必须与之一致（由 `tools/check_version.py` 校验）。
- 打包产物中**不允许**出现用户数据（`data/`、`.config/`、`logs/`、`__pycache__`）；
  程序的用户数据写入可执行文件所在目录，因此在产物目录运行过程序后，不可直接压缩发布。
- 插件后端由运行时 `importlib` 动态加载：它 import 的每个 `shell.backend.*` 与
  第三方模块都必须列入 `docs/Releases/spec_common.py` 的 `HIDDEN_IMPORTS`，
  且第三方包必须在 `requirements.txt` 中声明。

## 提交前必须执行的检查

1. 运行与改动相关的门禁（全量约 30 秒）：

   ```bash
   venv/bin/python -m ruff check .
   venv/bin/python tools/check_version.py
   venv/bin/python tools/check_plugins.py
   venv/bin/python tools/build_icons.py --check
   venv/bin/python tools/check_packaging.py
   venv/bin/python tools/check_npm_audit.py
   venv/bin/python -m unittest discover -s tests
   ```

   改动涉及打包相关文件（`docs/Releases/**`、`requirements*.txt`、`pyproject.toml`、
   `tools/check_packaging.py`、`tools/check_build_tree.py`）时，需追加一次真实构建
   与 `tools/check_build_tree.py`。

   改动涉及壳注入给插件的共享资源（`shell/frontend/public/shell/**`：`variables.css`、
   `base.css`、`effects.css`、`base.js`、`folder-picker.*`、`motion.js`、`icons.svg`）时，
   必须重新构建，否则改动不生效：

   ```bash
   npm --prefix shell/frontend run build
   ```

   `/shell/<file>` 路由**优先返回 `shell/frontend/dist/shell/`**，仅在 dist 中不存在时
   回落到 `public/shell/`（`shell/backend/file_server.py:762-769`；`dist/` 在 `.gitignore` 中，
   构建产物不纳入提交）。未构建时插件侧得到的是「新标记 + 旧样式」，表现为源码已改而
   界面未变，易被误判为 CSS 错误。

2. 用 `git status` 确认未将构建产物、`data/`、`.config/`、日志纳入提交。
3. 禁止对 `main` / `develop` 执行 force push；禁止移动或删除已发布的 tag。

## 提交信息规范

格式：`<type>(<scope>): <subject>` + 空行 + 正文（可省）+ footer（可省）。

- `type` 取值：`feat` `fix` `refactor` `docs` `test` `ci` `build` `chore` `perf` `revert`。
- `scope`：受影响的模块或插件名，小写、连字符分隔，如 `shell` `file-server`
  `media-player` `release` `packaging` `deps`；跨模块改动可省略。
- `subject`：中文，≤ 30 字（硬上限 40），陈述**本次改动的内容**，不加句号；
  不写现象、不写过程、不写自我评价（如"完善了一下"），不出现"我"。
- 正文写**背景 / 改动 / 验证**，客观陈述可复核的事实（命令、文件、结果）；
  过程记录（先试 A 再试 B）属于 PR 或 issue，不写入提交信息。
- 破坏性变更用 footer `BREAKING CHANGE: <说明>`；关联 issue 用 `Refs #<编号>`。
- 一条提交只做一件事；`git log --oneline` 中单独看一行也要能读懂。

### 措辞排除表（写完逐条自查）

| 类别 | 排除 | 改为 |
| --- | --- | --- |
| 情绪与口语 | 炸了、搞定、顺手、翻车、踩坑、不许、才公开、转绿、全绿、单跑、写全了、到底、其实、裸奔 | 客观陈述事实与结果 |
| 未量化的夸张 | 彻底解决、大幅提升、完全避免 | 给出数字；无法量化时写明作用范围与限制 |
| 不确定的断言 | 应该没问题、可能是这里 | "未验证"或"待确认" |
| 叙事腔 | "读者需要跨文件拼装""信息散在多个文件里" | "同一插件的信息分布在 2-4 个文件中" |

项目既有术语（如"幽灵 API"）沿用原词，不另造说法。

### 对照（不合格 → 合格）

| 不合格 | 问题 | 合格 |
| --- | --- | --- |
| `fix(release): 本地打包脚本不许把开发机的用户数据打进发行包` | "不许"是情绪 | `fix(release): 本地打包脚本在压缩前拦截用户数据目录` |
| `test: 修正只在 Windows 成立的测试，让 ubuntu test job 转绿` | "转绿"是口语 | `test: 按平台修正依赖 Windows 语义的断言` |
| `ci: Linux 构建改用发行版 Python，修 CI 产物启动即失败` | 一行两件事，后半句写现象 | `ci(build): Linux 构建改用发行版 Python 以修复产物无法启动` |

正文对照：

```
不合格：现象式开头、口语、把顺带改动写成"顺手"
现象：装好的包只显示漫画和小说两个插件……
顺手把 chardet 也补上。

合格：客观、可复核、分段
背景：image-viewer 与 media-player 在冻结产物中加载失败，插件清单只剩
manga-library 与 novel-reader；原因是这两个插件依赖的 shell.backend.tasks 与
shell.backend.thumb_cache 未列入 HIDDEN_IMPORTS。
改动：HIDDEN_IMPORTS 补入上述两个模块；requirements.txt 补 chardet。
验证：解析产物 exe 的 PYZ 确认三个模块已进入；Linux 产物实跑，7 个插件全部加载。
```

### 提交前检查清单

- [ ] `subject` 单独看能读懂，且 ≤ 30 字
- [ ] 正文中每条断言都可复核（命令、文件、数字）
- [ ] 无情绪化、不确定、未量化的措辞
- [ ] 正文无以"我"为主语的叙述
- [ ] `git status` 中无构建产物、`data/`、`.config/`、日志
- [ ] 相关门禁已运行（清单见上）
- [ ] 破坏性变更已用 `BREAKING CHANGE:` 标注

示例：

```
fix(file-server): /thumbs 越界访问返回 403 而非 400

背景：/thumbs 的 except 分支将 abort(403) 按普通异常吞掉，越界访问被改写成
400，掩盖了真实拒绝原因。
改动：还原 HTTPException 的 e.code；新增 tests/test_file_server_paths.py 覆盖
越界、缺文件、正常读取三种情况。
验证：python -m unittest tests.test_file_server_paths -v（6 例通过）。
```
