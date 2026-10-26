# tools

仓库自己的门禁与构建期工具，不是产品代码。`python -m pyright main.py shell tools` 把它们算作内核硬门禁，因此**这里的脚本也要保持零类型错误**。

## 门禁脚本

| 脚本 | 检查什么 | 何时必须跑 |
| --- | --- | --- |
| `check_version.py` | `pyproject.toml` / `shell/frontend/package.json` / `package-lock.json`（根 `version` 与 `packages[""].version`）/ git tag 同源（`--print` 打印当前版本） | 改版本号、打 tag |
| `check_plugins.py` | manifest 可解析、`name` 与目录名一致、前端/后端声明完整、每个字段都有读取方或已登记为无效果 | 改任何插件 |
| `check_packaging.py` | PyInstaller spec 的数据收集规则（不需要真跑 PyInstaller） | 改 spec、收集规则或依赖 |
| `check_build_tree.py` | 真正跑完 PyInstaller 之后，产物目录里有没有该有的东西 | 真跑过构建之后 |
| `build_icons.py` | 由 `res/icons/icon_data.json` 生成 sprite 与注入函数，`--check` 校验一致性 | 改图标或图标相关代码 |
| `check_tests.py` | `tests/test_*.py` 必须真的产出 `unittest` 用例 | 新增测试文件 |
| `check_docs.py` | 每个 `AGENTS.md` 里引用的名字、路径与 Markdown 链接都还存在（插件界面文档只查链接与命令里的路径，理由见脚本头部） | 改任何 `AGENTS.md`、函数改名、文件挪位置 |
| `check_private_paths.py` | 从**未跟踪**的本地配置提取真实路径与令牌，扫被跟踪文件里有没有被复制进去 | 开发机上每次提交前 |
| `check_npm_audit.py` | 前端依赖漏洞（脚本内部固定官方源） | 改前端依赖 |
| `check_frontend_escape.cjs` | 前端属性注入的转义一致性（用 node 运行） | 改前端脚本或样式 |
| `plugin_ci.py` | 按插件拆分 CI 测试：`--list` 从 manifest 生成矩阵，`--core` 跑共享测试，`--plugin <name>` 跑单插件测试；测试归属按 `test_<插件名下划线>_*` 前缀 | CI 的 `test` 与 `plugin` job；改测试归属规则时 |

`check_private_paths.py` 读 `.config/plugins/*.json`、`.config/app.yaml`、`.config/auth_token.txt`、`data/group-mesh/identity/*.json`（均未跟踪）。CI runner 上不存在这些配置，因此它不再是 CI 步骤，只在开发机提交前运行；在本地没有配置时会打印 SKIP 并以 0 退出。

→ 因此：注释、文档、用例里的示例路径一律写成 `D:\…` 占位，不要使用开发机真实路径。

## 一次性 / 手工工具

| 脚本 | 用途 |
| --- | --- |
| `new_plugin.py` | 插件脚手架，从 `tools/examples/hello-world` 生成可运行的新插件 |
| `plugin_ui_audit.py` | 插件页面骨架的浏览器验收：起真实壳（或 `--base-url` 复用实例）逐插件检查壳 chrome、内容 iframe 无 `.view-sub-sidebar` / `.view-toolbar` 等标记、导航与标题同步、分隔条 hover、`freshness` 挂载与控制台错误；另起隔离实例检查设置弹窗保存往返、搜索回填/清空与 mini EQ（`--base-url` 时跳过这三项）。用法：`venv/Scripts/python.exe tools/plugin_ui_audit.py [--plugin <name>] [--base-url <url>] [--json <path>]`。退出码 `0` 全部**检查项** PASS、`1` 有 FAIL、`2` 环境或参数错误（缺已构建的 `shell/frontend/dist`、Chrome 或 selenium）、`3` 没有 FAIL 但存在 SKIP（未验证项单独计数，SKIP 不算通过）；需要 `requirements-e2e.txt` 的 selenium，不进 CI |
| `fetch_lucide_icons.py` | 把 Lucide 图标冻结进仓库（构建期联网，运行时不联网） |
| `bench_gallery.py` | 相册性能探针：在真实图库上量 image-viewer 各阶段耗时（只读，不生成缩略图） |
| `shoot_readme.py` `shoot_readme_setup.py` | README 配图抓取（隔离实例 + 有头 Chrome，截视口后转 WebP） |
| `test_fake_mpv.py` | 验证"假 mpv 截获 ncm-cli 播放 URL"的方案 |

`examples/` 是插件模板，`scenes/` 与 `readme_scenes.json` 是配图场景表。

## 约定

- **新增门禁要同时接进 `.github/workflows/ci.yml` 和根 [AGENTS.md](../AGENTS.md) 的提交前清单**，否则没有人会跑到它。
- **设计理由写在脚本自身**：每个脚本顶部写清"为什么需要它"，不要只写在文档里。
