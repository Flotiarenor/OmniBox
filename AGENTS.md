# AGENTS.md —— OmniBox 仓库内的 Agent 工作约定

本文件是入口，只写**处处成立**的内容。每个子系统另有自己的 `AGENTS.md`，只写在该目录成立的东西；
工作深入到某个目录后按需读取，去向见下方「按改动选读」。

## 项目要点

- 桌面应用框架：Python 后端（`shell/backend/`）+ Vue 3 前端（`shell/frontend/`），运行时可选 PyInstaller 冻结；插件在 `plugins/<name>/`（`manifest.json` + `backend/` + `frontend/`）。
- 版本号唯一来源是 `pyproject.toml` 的 `[project].version`，`shell/frontend/package.json` 必须与之一致（由 `tools/check_version.py` 校验）；`shell/frontend/package-lock.json` 里的两处副本（根 `version` 与 `packages[""].version`）同样由它校验 —— `npm ci` 只校验依赖，不管这两处。
- 打包产物中不允许出现用户数据（`data/`、`.config/`、`logs/`、`__pycache__`）；插件后端的动态依赖必须登记在 `HIDDEN_IMPORTS`。这两条的判据见 [docs/Releases/AGENTS.md](docs/Releases/AGENTS.md)。
- 不允许注释添加后续无法持续维护的固定引用地址，文字引用，章节号。使用自足陈述描述。

## 提交前必须执行的检查

```bash
venv/bin/python -m ruff check .
venv/bin/python tools/check_version.py
venv/bin/python tools/check_plugins.py
venv/bin/python tools/build_icons.py --check
venv/bin/python tools/check_packaging.py
venv/bin/python tools/check_npm_audit.py
venv/bin/python tools/check_private_paths.py
venv/bin/python tools/check_tests.py
venv/bin/python tools/check_docs.py
venv/bin/python -m pyright main.py shell tools
venv/bin/python -m unittest discover -s tests
```

全量约 30 秒。`pyright` 只扫 `main.py shell tools`（内核零错误硬门禁，与 CI 的 typecheck job 同范围）；
插件与测试目前是基线、不拦截。每个门禁为什么存在、什么时候必须跑，见 [tools/AGENTS.md](tools/AGENTS.md)。

## 按改动选读

| 改动落在 | 先读 |
| --- | --- |
| `shell/backend/**` | [shell/backend/AGENTS.md](shell/backend/AGENTS.md) |
| `shell/frontend/**`（含 `public/shell/**`） | [shell/frontend/AGENTS.md](shell/frontend/AGENTS.md) —— 改共享资源后必须 `npm --prefix shell/frontend run build`，否则改动不生效 |
| `plugins/**` | [plugins/AGENTS.md](plugins/AGENTS.md)；单个插件内部另有 `plugins/<name>/AGENTS.md`（按 [docs/plugin-doc-spec.md](docs/plugin-doc-spec.md) 写） |
| `docs/Releases/**`、`requirements*.txt`、`pyproject.toml`、`tools/check_packaging.py`、`tools/check_build_tree.py` | [docs/Releases/AGENTS.md](docs/Releases/AGENTS.md) —— 追加一次真实构建与 `check_build_tree.py` |
| `tools/**` | [tools/AGENTS.md](tools/AGENTS.md) |
| `tests/**` | [tests/AGENTS.md](tests/AGENTS.md) |
| 写文档（`docs/**`、`readme.md`、各 `AGENTS.md`） | 先读 [docs/plugin-doc-spec.md](docs/plugin-doc-spec.md)：设计文档用**名字**定位（文件路径 / CSS 选择器 / DOM id / 函数名 / API 名），不写行号 |
| 提交信息、CI 与发布流程 | [docs/commit-convention.md](docs/commit-convention.md)、[docs/ci-and-release.md](docs/ci-and-release.md) |

## 提交信息

格式、措辞排除表与对照例子见 [docs/commit-convention.md](docs/commit-convention.md)；机器可执行的键值在
[docs/commit-conventions.md](docs/commit-conventions.md)，由 DSH 的 `git_commit` 工具在写库前逐条校验，
不合规**直接拒绝**并给出改法。改规则时两个文件一起改：键值决定工具拦什么，规范解释为什么。

## 其他文档

项目方向 [docs/core-direction.md](docs/core-direction.md)；本地开发 [docs/development.md](docs/development.md)；CI 与发布 [docs/ci-and-release.md](docs/ci-and-release.md)。

插件页面骨架（壳渲染 chrome、内容 iframe 只装内容、native 面板）见 [docs/plugin-page-architecture.md](docs/plugin-page-architecture.md)；
界面规范见 [docs/plugin-ui-guide.md](docs/plugin-ui-guide.md)。改插件页面后可用
`venv/Scripts/python.exe tools/plugin_ui_audit.py` 做真实壳验收（需已构建的 `shell/frontend/dist`、
Chrome 与 selenium；不进 CI，用法与退出码见 [tools/AGENTS.md](tools/AGENTS.md)）。

## 与 agent 协作

- 需要在这个仓库里找东西时先问，不要靠大范围搜索硬凑。
- 本地检出落后于 `origin/main` 时，先提示拉取再继续。

## 收尾

- 用 `git status` 确认未将构建产物、`data/`、`.config/`、日志纳入提交。
- 禁止对 `main` / `develop` 执行 force push；禁止移动或删除已发布的 tag。
