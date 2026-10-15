# docs/Releases

PyInstaller 打包、产物校验与发布脚本。根 [AGENTS.md](../../AGENTS.md) 的全局约定仍然适用。

## 文件

| 文件 | 用途 |
| --- | --- |
| `build-release.ps1` `build-release.sh` | 本地打包脚本（Windows / Linux） |
| `omnibox.spec` `omnibox-linux.spec` | 两份 PyInstaller 规格 |
| `spec_common.py` | 两份 spec 共享的唯一一份收集逻辑与 `HIDDEN_IMPORTS` |
| `OmniBox*.7z` `OmniBox.zip` `OmniBox/` | 产物，均在 `.gitignore` 中，不提交 |

## 规则

- **前端产物必须存在**：`spec_common.py` 在前端缺失时抛异常。不要改回 `if frontend_dist.exists():` 静默跳过——那会产出一个打开白屏的 exe，而构建过程全绿。
- **不打 `__pycache__` / `.pyc`**：不要用 `rglob('*')` 收文件。
- **不打运行时数据**：`data/`、`.config/`、`logs/` 属于用户数据，不进发行包。
- **`HIDDEN_IMPORTS` 必须齐全**：插件源码里出现的每一个 `shell.backend.*` 与第三方模块都要登记，第三方包同时写进 `requirements.txt`。漏登记的表现是"构建全绿、装好后那个插件整个不出现"；门禁是 `python tools/check_packaging.py`，设计理由见 `spec_common.py` 顶部注释。
- **用户数据只写在可执行文件所在目录**：在产物目录运行过程序后，不要直接压缩发布。

## 何时必须追加一次真实构建

改动涉及 `docs/Releases/**`、`requirements*.txt`、`pyproject.toml`、`tools/check_packaging.py`、`tools/check_build_tree.py` 时：

```bash
python tools/check_packaging.py     # 检查收集规则
# …真实跑一次 PyInstaller…
python tools/check_build_tree.py    # 检查产物目录内容
```

`check_packaging.py` 查"收集规则对不对"，`check_build_tree.py` 查"真正跑完之后，产物里到底有没有该有的东西"。发布流程见 [docs/ci-and-release.md](../ci-and-release.md)。
