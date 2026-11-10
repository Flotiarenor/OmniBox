# docs/Releases

PyInstaller 打包、产物校验与发布脚本。根 [AGENTS.md](../../AGENTS.md) 的全局约定仍然适用。

## 文件

| 文件 | 用途 |
| --- | --- |
| `build-release.ps1` `build-release.sh` | 本地打包脚本（Windows / Linux） |
| `cubby.spec` `cubby-linux.spec` | 两份 PyInstaller 规格 |
| `spec_common.py` | 两份 spec 共享的唯一一份收集逻辑与 `HIDDEN_IMPORTS` |
| `cubby.iss` | Windows 安装器（Inno Setup 6）：把 `.build-dist/Cubby` 打成 per-user 安装包；CI 传 `/DAppVersion=<版本>` `/DSourceDir=<产物目录>` `/O<输出目录>`，产物是 `Cubby-Setup-<版本>.exe` |
| `Cubby*.7z` `Cubby.zip` `Cubby/` | 产物，均在 `.gitignore` 中，不提交 |

## 规则

- **前端产物必须存在**：`spec_common.py` 在前端缺失时抛异常。不要改回 `if frontend_dist.exists():` 静默跳过——那会产出一个打开白屏的 exe，而构建过程全绿。
- **不打 `__pycache__` / `.pyc`**：不要用 `rglob('*')` 收文件。
- **不打运行时数据**：`data/`、`.config/`、`logs/` 属于用户数据，不进发行包。
- **`HIDDEN_IMPORTS` 必须齐全**：插件源码里出现的每一个 `shell.backend.*` 与第三方模块都要登记，第三方包同时写进 `requirements.txt`。漏登记的表现是"构建全绿、装好后那个插件整个不出现"；门禁是 `python tools/check_packaging.py`，设计理由见 `spec_common.py` 顶部注释。
- **用户数据不在安装目录**：非便携安装的数据根是 `%LOCALAPPDATA%\Cubby`（Linux 上是 `$XDG_DATA_HOME/cubby`），安装目录只放只读资源；便携模式由可执行文件旁的 portable.txt 显式声明，此时数据才落在可执行文件所在目录，便携产物运行过后不要直接压缩发布。
- **安装器只在 CI 里编译**：`cubby.iss` 由 iscc 编译，本地不需要安装 Inno Setup。编译、校验和与静默安装/卸载冒烟抽在 [`.github/actions/windows-installer`](../../.github/actions/windows-installer/action.yml) 里，`release.yml` 与 `package-smoke.yml` 共用同一份：改 `docs/Releases/**` 时打包冒烟就会真编一次，不必等到打 tag。升级靠固定 AppId 覆盖同一安装目录，用户数据在 `%LOCALAPPDATA%\Cubby`（不在安装目录里），因此升级不迁移数据，卸载默认保留它。

## 何时必须追加一次真实构建

改动涉及 `docs/Releases/**`、`requirements*.txt`、`pyproject.toml`、`tools/check_packaging.py`、`tools/check_build_tree.py` 时：

```bash
python tools/check_packaging.py     # 检查收集规则
# …真实跑一次 PyInstaller…
python tools/check_build_tree.py    # 检查产物目录内容
```

`check_packaging.py` 查"收集规则对不对"，`check_build_tree.py` 查"真正跑完之后，产物里到底有没有该有的东西"。发布流程见 [docs/ci-and-release.md](../ci-and-release.md)。
