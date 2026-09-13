# AGENTS.md —— OmniBox 仓库内的 Agent 工作约定

本文件由支持 `AGENTS.md` 的工具（含 DSH）在工作区根目录自动加载；细则文档见
[提交信息规范](./docs/commit-convention.md)、[CI、打包与发布](./docs/ci-and-release.md)。

## 项目要点

- 桌面应用框架：Python 后端（`shell/backend/`）+ Vue 3 前端（`shell/frontend/`），
  运行时可选 PyInstaller 冻结；插件在 `plugins/<name>/`（`manifest.json` +
  `backend/` + `frontend/`）。
- 版本号唯一来源是 `pyproject.toml` 的 `[project].version`，
  `shell/frontend/package.json` 必须与之一致（`tools/check_version.py` 把关）。
- 打包产物里**不允许**出现用户数据（`data/`、`.config/`、`logs/`、`__pycache__`）；
  程序的用户数据写在可执行文件旁边，所以在产物目录里跑过程序后不要直接压缩发布。
- 插件后端是运行时 `importlib` 动态加载的：它 import 的每个 `shell.backend.*` 与
  第三方模块都必须列进 `docs/Releases/spec_common.py` 的 `HIDDEN_IMPORTS`，
  且第三方包必须在 `requirements.txt` 里声明。

## 提交前必须做的事

1. 跑与改动相关的门禁（全量约 30 秒）：

   ```bash
   venv/bin/python -m ruff check .
   venv/bin/python tools/check_version.py
   venv/bin/python tools/check_plugins.py
   venv/bin/python tools/check_packaging.py
   venv/bin/python -m unittest discover -s tests
   ```

   改到打包相关文件（`docs/Releases/**`、`requirements*.txt`、`pyproject.toml`、
   `tools/check_packaging.py`、`tools/check_build_tree.py`）时，再加一次真实构建
   与 `tools/check_build_tree.py`。

2. `git status` 确认没有把构建产物、`data/`、`.config/`、日志带进提交。
3. 不要对 `main` / `develop` 做 force push；不要移动或删除已发布的 tag。

## 提交信息规范（摘要，细则见 docs/commit-convention.md）

格式：`<type>(<scope>): <subject>` + 空行 + 正文（可省）+ footer（可省）。

- `type` 取值：`feat` `fix` `refactor` `docs` `test` `ci` `build` `chore` `perf` `revert`。
- `scope`：受影响的模块或插件名，小写、连字符分隔，如 `shell` `file-server`
  `media-player` `release` `packaging` `deps`；跨模块改动可省略。
- `subject`：中文，≤ 30 字（硬上限 40），说明**这次改动是什么**，不加句号；
  不写现象描述、不写"修了个 bug"这类空话、不写"我"。
- 正文写**背景 / 改动 / 验证**，客观陈述可复核的事实（命令、文件、结果）；
  过程流水账（试了 A 又试了 B）属于 PR 或 issue，不写进 commit。
- 措辞要求：不用情绪化、不确定或未量化的表达（"炸了""搞定""顺手""彻底解决""大幅提升"）；
  项目既有术语（如"幽灵 API"）可以用。
- 破坏性变更用 footer `BREAKING CHANGE: <说明>`；关联 issue 用 `Refs #<编号>`。
- 一条提交只做一件事；`git log --oneline` 里单独看一行也要能读懂。

示例：

```
fix(file-server): /thumbs 越界返回 403 而不是 400

背景：/thumbs 的 except 分支把 abort(403) 当普通异常吞掉，越界访问被改写成 400，
掩盖了真实拒绝原因。
改动：还原 HTTPException 的 e.code；新增 tests/test_file_server_paths.py 覆盖
越界、缺文件、正常读取三种情况。
验证：python -m unittest tests.test_file_server_paths -v（6 例通过）。
```
