# Linux 分发：install.sh、systemd 与容器镜像

> 状态：计划中（设计定稿，无代码）。
>
> 已由 Lead 拍板的三条：① 本期只发 amd64 镜像，arm64 单独立项，前置条件是"依赖在 arm64 上全部有
> wheel"与"CI 有原生 arm64 runner"；② glibc 基线列为 **2.0 发布阻断项**，机制用容器化构建而不是换
> runner 标签（`ubuntu-22.04` 的 `python3` 是 3.10，低于 `requires-python`），详见 §9；③
> `scripts/install.sh` 作为 Release 资产随版本发布，`release.yml` 的 publish job 增一步上传。

本文定 Linux 的分发形态与可执行细节；数据根与安装目录为什么分离见 [install-and-update.md](./install-and-update.md)，构建链与 glibc 基线见 [ci-and-release.md](./ci-and-release.md)，Windows 侧的渠道包装（winget / Scoop）见 [distribution-channels.md](./distribution-channels.md)，应用内检查更新见 [update-check.md](./update-check.md)。仓库里目前**没有** `scripts/install.sh`、unit 文件、Dockerfile 或镜像工作流步骤，本文是它们的定稿，且不产生新的二进制产物：`install.sh` 消费现有 tar.gz，镜像从源码构建，unit 由安装脚本生成。

## 1. 目标与形态

Linux 的定位是**服务器 / NAS 上常驻的 `--web-only` 服务**，不是桌面应用。已发布形态只有 `Cubby-linux-x64.tar.gz`（内含 `Cubby/` 与可执行文件 `Cubby`，由 [cubby-linux.spec](./Releases/cubby-linux.spec) 与 [build-release.sh](./Releases/build-release.sh) 产出），默认 `http://127.0.0.1:18080`。

桌面窗口在 Linux 上不可用：[requirements.txt](../requirements.txt) 没有 GTK/PyGObject（也没有 Qt），而 [main.py](../main.py) 的非 `--web-only` 分支依赖 pywebview 的窗口后端。因此所有分发形态都**固定**带 `--web-only`，写进启动器、unit 与镜像入口，不留给用户选。

| 形态 | 输入 | 定位 | 状态 |
| --- | --- | --- | --- |
| tar.gz | PyInstaller 产物 | 底座，打包链不变 | 已实装 |
| `install.sh` | tar.gz（下载 + `.sha256`） | 不装 Python / Node，一条命令起服务 | 本文定稿 |
| 容器镜像 | 源码 + 依赖安装（§4.1） | NAS / 家庭服务器常驻 | 本文定稿 |
| systemd unit | 由 `install.sh` 生成 | 跟着安装位置走，不是发布资产 | 本文定稿 |

## 2. `install.sh`

放 `scripts/install.sh`（bash 4+）并作为 Release 资产发布；脚本自身只用 `curl`（缺则 `wget`）、`sha256sum`（缺则 `shasum -a 256`）、`tar`、`systemctl`。unit 由脚本内 heredoc 生成而不另存模板：模板与脚本分家后，改了路径忘改模板就是"服务指向不存在的目录"。

### 2.1 安装布局

| 内容 | `--user`（默认） | `--system` |
| --- | --- | --- |
| 版本目录 | `~/.local/lib/cubby/versions/<版本>/` | `/usr/local/lib/cubby/versions/<版本>/` |
| 当前版本 | `~/.local/lib/cubby/current` | `/usr/local/lib/cubby/current` |
| 启动器 / PATH 入口 | `~/.local/lib/cubby/bin/cubby`，`~/.local/bin/cubby` → 它 | `/usr/local/lib/cubby/bin/cubby`，`/usr/local/bin/cubby` → 它 |
| unit | `~/.config/systemd/user/cubby.service` | `/etc/systemd/system/cubby.service` |
| 数据根 | `$XDG_DATA_HOME/cubby`（默认 `~/.local/share/cubby`） | `/var/lib/cubby` |

- **版本目录 + `current` 链接**：升级是"加一个目录 + 换一个链接"，旧版本原样留在磁盘上，回滚就是切回去；Windows 侧只能覆盖同一安装目录（运行中的 onedir 被系统锁定），Linux 允许留多份、用链接选择。
- **启动器固定加 `--web-only`，程序目录运行期只读**：直接暴露 `Cubby` 会让用户落到必然失败的窗口模式，启动器因此是 `#!/bin/sh` 包装 `exec "$ROOT/current/Cubby" --web-only "$@"`，参数原样透传；冻结产物只往数据根写（[paths.py](../shell/backend/paths.py) 的 `get_user_data_dir()`），故程序目录 `755`、属主为 root（system）或安装用户（user），不必对服务用户可写。

### 2.2 参数

| 参数 | 含义 |
| --- | --- |
| `--user` / `--system` | 默认 `--user`；决定上表路径、是否需要 root、unit 层级 |
| `--prefix <目录>` | 安装前缀，默认 user `~/.local`、system `/usr/local`；程序根是 `<prefix>/lib/cubby` |
| `--version <版本>` | 装指定版本（`v` 前缀省写）；缺省取最新 Release |
| `--port <端口>` | 写进 unit 的 `ExecStart`，默认 `18080`；覆盖语义见 §3.3 |
| `--data-dir <路径>` | 数据根；system 默认 `/var/lib/cubby`，user 默认不设（用 XDG 默认） |
| `--uninstall` / `--purge` | 卸载；`--purge` 只与它同用，连数据根一起删（§2.5） |

`--data-dir` 在 system 模式下落成 unit 的 `Environment=CUBBY_HOME=<路径>`，这是应用层唯一的**整根**覆盖（`USER_DATA_DIR_ENV`）。不要用应用的 `--data-root` 代替：它只覆盖 `data/`（索引与缓存），`.config/`（含令牌）仍在数据根，混用会把一份状态分裂到两处。

### 2.3 下载与校验

- 最新：`https://github.com/Flotiarenor/Cubby/releases/latest/download/Cubby-linux-x64.tar.gz` 与同名 `.sha256`；指定版本：`.../releases/download/v<版本>/<同名>`。资产名不含版本号，两条 URL 用同一个文件名。
- 取法：`curl -fsSL --proto '=https' --tlsv1.2 -o`，无 curl 时退 `wget --https-only`，都没有就报错退出、**不做 http 回退**；`--version` 只接受 `[0-9A-Za-z.-]` 以防拼进路径；非 `.tar.gz` 或来自其它主机的输入拒绝。
- 校验：`sha256sum -c`，校验和文件必须是"64 位十六进制 + 空格 + 文件名"，不符即中止；解压前用 `tar -tzf` 校验成员全部以 `Cubby/` 开头、不含 `..` 与绝对路径、且含 `Cubby/Cubby`。解压时去掉压缩包的根目录 `Cubby/`：版本目录就是它的内容，可执行文件因此是 `versions/<版本>/Cubby`（与 unit 的 `ExecStart`、启动器的 `current/Cubby` 对齐）。没有代码签名，HTTPS 与 `.sha256` 是唯一完整性凭据。

### 2.4 升级与回滚

1. 解到 `versions/.staging-<版本>`，结构校验通过后 `chmod +x`。
2. **启动冒烟**：用独立 `CUBBY_HOME=<mktemp -d>` 起 `.staging/Cubby --web-only --port <随机高位端口>`，用 bash 的 `/dev/tcp` 轮询 `GET /health`（`_OPEN_ENDPOINTS` 里的开放路由，不需要令牌），10 秒内拿到 `200` 才通过，随后停进程、删临时数据根 —— 新版本若坏，不能让它往正在用的数据根里写东西。
3. 通过才 `mv` 成 `versions/<版本>`；切换先建 `current.tmp` 再 `mv -T current.tmp current`（原子 rename，避免 `ln -sfn` 的中间态）。`systemctl --user restart cubby`（system 模式去掉 `--user`）后再探一次 `/health`，失败则切回上一版并 `restart`；最后 `versions/` 只留新版本与上一版。

失败语义：第 1–3 步任一步失败 → 删 `.staging-*`，`current` 不动，**服务不重启**，退出码非 0。手动回滚是 `ln -sfn versions/<上一版> current.tmp && mv -T current.tmp current && systemctl --user restart cubby`。不自动回滚：`Restart=on-failure` 会自己反复试新版本，脚本再插一次判定只会与 systemd 打架，脚本保证的是"旧版本与旧链接还在"。切换时刻安全：已打开的可执行文件按 inode 保留，换链接不影响运行中的进程，生效点是 `restart` —— "什么时候重启"属于服务管理者，这也是应用内不做自动升级的原因（§7）。

### 2.5 卸载、运行用户与权限

- `--uninstall`：停用并 `disable --now` unit（不存在则跳过）→ 删 unit、PATH 入口与整个程序根 → **默认保留数据根**并打印其路径与删除命令；`--purge` 才删数据根，且要 `--yes` 才静默。默认保留与 Windows 安装器同判据：卸载常是"重装"或"换位置"的前一步。
- system 模式：`useradd --system --home-dir /var/lib/cubby --shell /usr/sbin/nologin cubby`，再 `install -d -o cubby -g cubby -m 0700 /var/lib/cubby`；系统用户默认不删 —— 它的家目录就是数据根，`userdel -r` 会连数据一起删，`--purge` 时先删数据再 `userdel`。`--user` 全程不需要提权。
- 数据根 `0700`：里面是 `.config/auth_token.txt`。应用自己把文件写成 `0600`（[auth.py](../shell/backend/auth.py) 的 `chmod`），但目录若为 `0755`，同机其它用户仍能列出它；`--data-dir` 指向共享目录时脚本告警。程序目录 `755`、文件 `644`/`755`，与"运行时只读"一致。

## 3. systemd

### 3.1 用户级与系统级的取舍

| 层级 | 文件 | 启停 | 适用 |
| --- | --- | --- | --- |
| 用户级（默认） | `~/.config/systemd/user/cubby.service` | `systemctl --user` | 单用户机器，不需要 root |
| 系统级 | `/etc/systemd/system/cubby.service` | `systemctl` | NAS / 常驻服务、开机即起 |

用户级 unit 默认在**用户登录后**才启动，重启无人登录时服务不起来；要开机即起必须 `loginctl enable-linger <用户>`，`install.sh --user` 只打印这一条而不自动执行（它改的是该用户之外的登录策略）。用户级 unit 也不能写 `User=` / `Group=`，不能加 `ProtectHome=yes`（数据根在家目录里）。

### 3.2 unit 内容（系统级）

```ini
[Unit]
Description=Cubby (格子铺) web service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cubby
Group=cubby
Environment=CUBBY_HOME=/var/lib/cubby
ExecStart=/usr/local/lib/cubby/current/Cubby --web-only --port 18080
WorkingDirectory=/usr/local/lib/cubby/current
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/cubby

[Install]
WantedBy=multi-user.target
```

用户级差异：去掉 `User=` / `Group=` / `Environment=`（数据根走 XDG 默认）与 `ProtectHome=`，`WantedBy=default.target`，`ExecStart` 指向 `~/.local/lib/cubby/current/Cubby`。三条取舍：`Type=simple` 因为 `--web-only` 的 `app.run()` 前台阻塞、应用不发 `sd_notify`；`Restart=on-failure` + `RestartSec=3` 让端口被占、配置读坏这类启动失败重试且不打满日志，无限重启由 systemd 默认的 `StartLimitBurst` 兜住；`ReadWritePaths` 只需数据根，配合 `ProtectSystem=strict` 反过来验证了"安装目录只放只读资源"，而 `ExecStart` 指向 `current/Cubby` 而非 PATH 启动器，少一层包装。

### 3.3 端口、host 与 trusted_hosts

- `--port` 的优先级（[main.py](../main.py) 的 `_resolve_port`）：值是整数时**覆盖** `app.json` 的 `server.port`，不是整数时打 warning 并按配置端口启动 —— unit 与 `app.json` 不一致时 unit 赢，但**写错值不会失败**，验收要确认日志里的 `Web-only 模式启动: http://<host>:<port>`。
- `host` **没有**命令行覆盖：`_argv_value` 只服务 `--port` / `--user-data-dir` / `--data-root`，host 只能改 `app.json` 的 `server.host`，默认 `127.0.0.1`。
- 不能直接把 host 改成 `0.0.0.0`：`--web-only` 是明文 HTTP，令牌（Cookie 或 `X-Cubby-Token`）在局域网上可被截获；而 [file_server.py](../shell/backend/file_server.py) 的 `_build_trusted_hosts` 在 host 为通配地址时只放行"本机各网卡 IP"、**不放行域名**，绑通配 + 用域名访问会得到 400。推荐：unit 保持回环，前面放 Caddy / nginx 终止 TLS 再反代到 `127.0.0.1:<port>`。
- 反代要点：把 `Host` 改写成 `127.0.0.1:<port>`（`proxy_set_header Host 127.0.0.1:18080;`），这样不必把域名加进 `server.trusted_hosts`，DNS rebinding 防护保持"只认回环与本机 IP"的强度；把域名写进 `trusted_hosts` 也能通，但等于额外声明"这个域名可以带令牌访问"。确要直连局域网则 `server.host` 改 `0.0.0.0` 并把访问用的 IP/域名追加到 `server.trusted_hosts` —— 留空不是"不校验"，未列入的 Host 一律 400（`_err_400` 的标记页会写出原因）。

### 3.4 日志

- 应用日志：数据根 `.config/logs/cubby.log`，滚动 2 MiB × 3 个备份（[app_logging.py](../shell/backend/app_logging.py) 的 `_MAX_BYTES` / `_BACKUP_COUNT`），单实例上限约 8 MiB，是**权威日志**，`--web-only` 下一定存在。
- 分工与体积：`journalctl -u cubby` 看 systemd 记录的启停、退出码、重启原因与进程 stderr，业务日志看文件。冻结产物按 `console=False` 打包，stderr 通道不保证存在（`app_logging` 已按"stderr 可能是 None"处理），journal 里可能只有 systemd 自己那几行；默认 `StandardOutput=journal` 会让 stderr 一直进 journal 而文件日志已滚动，需要上限时加 `/etc/systemd/journald.conf.d/cubby.conf` 设 `SystemMaxUse=`，并用 `journalctl -u cubby --since -1h | wc -l` 确认它不随运行时间线性增长。

## 4. 容器镜像

### 4.1 两条路的结论

**用源码 + 依赖安装**（多阶段：Node 构建前端 → Python 运行时镜像），不复用 PyInstaller 产物：

1. **glibc 与基镜像耦合**：PyInstaller 不打包 glibc，产物要求运行环境的 glibc ≥ 构建机的 glibc；CI 在 `ubuntu-latest` 上构建，基线偏高，塞进 `debian:bookworm-slim` 这类基镜像会动态链接失败，而源码镜像里解释器与基镜像同源。
2. **多架构与体量**：PyInstaller 不能交叉编译，产物只有 x64 一份，而源码镜像同一份 Dockerfile 能出 arm64（§4.5）；`python:3.12-slim` 装的依赖与产物里那批是同一批，"用产物更小"不成立，源码镜像还能给出真实 traceback，与服务器定位一致。

代价：镜像不经过 `tools/check_build_tree.py` 的冻结产物校验，验收只能靠"起得来 + `/health` + 页面能开"；前端产物必须显式 COPY —— 源码路径下缺 `shell/frontend/dist` 的表现是白屏。构建上下文另需 `.dockerignore`（排除 `venv/`、`shell/frontend/node_modules`、`shell/frontend/dist`、`.build-*`、`data/`、`.config/`），否则 `COPY shell/frontend/` 会把本机 `node_modules` 带进镜像。

### 4.2 Dockerfile（仓库根）

```dockerfile
FROM node:20-bookworm-slim AS frontend
WORKDIR /src
COPY shell/frontend/ /src/
RUN npm ci && npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CUBBY_HOME=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY ["main.py", "shell", "plugins", "res", "./"]
COPY --from=frontend /src/dist shell/frontend/dist
COPY scripts/docker-entrypoint.sh /app/
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin cubby && mkdir -p /data && chown cubby:cubby /data && chmod +x /app/docker-entrypoint.sh
USER cubby
VOLUME ["/data"]
EXPOSE 18080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18080/health', timeout=2).status==200 else 1)"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
```

- **直接装 [requirements.txt](../requirements.txt)**（含 Linux 用不到的 `pywebview` / `pythonnet`）：依赖声明只有一份，"镜像与 PyInstaller 产物装同一批依赖"比省几十 MB 重要；瘦身要新增 `requirements-linux.txt` 并纳入 `tools/check_packaging.py`，属独立改动。
- **`HEALTHCHECK` 打 `/health`**：它在 `_OPEN_ENDPOINTS` 里、**不需要令牌**、也不依赖前端产物；`/status` 是壳内的 SPA 路由不是接口，不能当探针。容器内请求的 Host 是 `127.0.0.1:18080`，在 `_build_trusted_hosts` 的默认白名单里，不会被 400；不用 `curl` 探活是因为 slim 基镜像没有它，镜像里已有 python。
- 非 root（uid 10001），`/data` 是它的家目录：命名卷首次挂载时 Docker 把镜像里 `/data` 的属主复制进空卷，无需额外 chown；**bind mount 要手工 `chown -R 10001:10001`**，否则首次启动写不了令牌。

### 4.3 入口脚本（`scripts/docker-entrypoint.sh`）与参数化

```sh
#!/bin/sh
set -eu
python - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ.get('CUBBY_HOME', '/data')) / '.config' / 'app.json'
if not p.is_file():
    hosts = [h.strip() for h in os.environ.get('CUBBY_TRUSTED_HOSTS', '').split(',') if h.strip()]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'server': {'host': '0.0.0.0', 'trusted_hosts': hosts}}, indent=2), encoding='utf-8')
PY
exec python /app/main.py --web-only --port "${CUBBY_PORT:-18080}"
```

- **只在 `app.json` 不存在时写**：之后用户改卷里这份文件（补 `trusted_hosts`、换 `data_root`）永久生效，入口脚本不覆盖人工配置。
- **`host` 必须写 `0.0.0.0`**：默认是 `127.0.0.1`，那样容器只监听自己的 lo，`-p` 端口映射得到的是"连不上"而不是 400 —— 容器化最常误判的一条；应用没有 `--host` 参数，只能走配置。端口走**既有**的 `--port`（覆盖优先级见 §3.3）；`CUBBY_PORT` 与 `CUBBY_TRUSTED_HOSTS`（逗号分隔）是**本镜像的**环境变量名，不是应用参数，后者只在首次启动写进 `trusted_hosts`。

### 4.4 `docker run` 与 compose

A. 只绑宿主机回环、前面放反代 —— 令牌不出本机，`Host` 默认就在白名单里：

```bash
docker run -d --name cubby --restart unless-stopped -p 127.0.0.1:18080:18080 -v cubby-data:/data ghcr.io/flotiarenor/cubby:1.4.0
```

B. compose 直连局域网 —— 必须给 `trusted_hosts`，否则是 400 标记页：

```yaml
services:
  cubby:
    image: ghcr.io/flotiarenor/cubby:1.4.0
    restart: unless-stopped
    ports: ["18080:18080"]
    environment: {CUBBY_TRUSTED_HOSTS: "127.0.0.1,localhost,nas.local,192.168.1.20"}
    volumes: ["cubby-data:/data"]
volumes: {cubby-data: {}}
```

令牌不需要注入：首次启动由 `get_or_create_token()` 生成在卷里的 `.config/auth_token.txt`（`0600`，属主 10001），`docker compose exec cubby cat /data/.config/auth_token.txt` 取；浏览器访问首页时自动种 Cookie，脚本用 `X-Cubby-Token` 头。升级是 `docker compose pull && docker compose up -d`，数据在卷里不动；`app.json` 已存在时入口脚本不改它 —— 改 `trusted_hosts` 要直接改卷里的文件，改环境变量不生效。

### 4.5 多架构

- **amd64**：与现有 tar.gz 一致，本期就做。**arm64 不走 PyInstaller**：那要在 `buildx` + QEMU 里跑一遍完整 PyInstaller（分析 + 打包），耗时无实用价值，且产物的 glibc 基线跟着模拟环境走。
- **arm64 走源码镜像**：`docker buildx build --platform linux/amd64,linux/arm64 --push`。风险在 wheel 覆盖：`pillow` / `cryptography` / `pycryptodome` 有 aarch64 wheel，`pythonnet` 在 Linux/arm64 是否可用要实测（没有就走源码构建，需要 .NET 工具链）。更好的位置是 GitHub 的 arm64 托管 runner（`ubuntu-24.04-arm`）native 跑同一条 Dockerfile，比 QEMU 快且无模拟误差，启用前先确认它对公开仓库可用。
- **推荐：本期只发 amd64 镜像**，arm64 单独立项，前置条件是"依赖在 arm64 上全部有 wheel"与"CI 有 arm64 runner"。

## 5. ghcr 发布

- **权限**：[release.yml](../.github/workflows/release.yml) 现在只有 `contents: write`；推镜像要 `packages: write`，放在容器工作流自己的顶层，不给整个发布流程放宽。
- **登录**（不引入第三方 action，与 `publish` job 用 `gh` 的取向一致）：`echo "$GITHUB_TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin`。
- **tag**：`ghcr.io/<owner>/cubby:<版本>`（版本取自 `python tools/check_version.py --print`）；`:latest` **只给稳定版**、跳过预发布，不推 `main` 与分支名，避免"未发布的代码可以被拉"。
- **与 release.yml 的关系：独立 workflow，不要塞进 release.yml。** 后者的语义是"自动构建、人工公开"，草稿 Release 对外不可见；而 ghcr 包一旦推送就能被拉。所以镜像在 **Release 已发布之后**再推：`on: release: types: [released]`，条件 `if: github.event.release.prerelease == false`，另留 `workflow_dispatch` 兜底重推。
- **可见性**：公开仓库首次推送后包默认继承仓库可见性，仍要在仓库 Packages 页确认一次；public 包 `docker pull` 无需登录，private 需 `docker login ghcr.io`（令牌带 `read:packages`）。

## 6. AppImage 的再评估

**结论：不做，且不作为 2.x 的目标。** 三条理由：

1. 没有 GTK/PyGObject（[requirements.txt](../requirements.txt) 无 `pygobject` / `PyQt`），[main.py](../main.py) 的桌面窗口在 Linux 上起不来；AppImage 的价值是"双击即用的桌面应用"，这里兑现不了 —— 里面只能是一个需要浏览器访问的 `--web-only` 服务。
2. 形态与体积错配：AppImage 要自带运行时，而服务形态已有更合适的容器与 `install.sh` 两条路，都不需要 root，也不需要打成自解压单文件。
3. 更新方式：AppImage 的升级是"下载新的替换旧的"，对常驻服务不如 `systemctl restart` 与 `compose pull` 清楚。

值得做的条件（要先有实现，不是先有包装）：[requirements.txt](../requirements.txt) 引入 `PyGObject`（或 Qt）且 Linux 上真能建窗；CI 能跑 xvfb 的真实窗口冒烟（现有 `tools/shell_e2e_smoke.py` 验的是壳在 Chrome 里的行为，不等于桌面窗口能开）；有用户明确要"无 root、无 Docker、无浏览器"的桌面捆绑包。

## 7. 与检查更新的关系

应用内"检查更新"的设计见 [update-check.md](./update-check.md)：它的「Linux」一节已定 **2.0 不把 Linux 纳入应用内检查** —— 后端在 Linux 上返回 `disabled`（原因 `platform`），「关于」段不出现检查按钮，只做"发布说明 + [readme.md](../readme.md) 里写清下载与 `sha256sum -c` 校验"。本节据此收敛，不新增规则：

- **容器形态不参与应用内检查**：版本由镜像 tag 决定，升级是 `docker compose pull && docker compose up -d`，发生在应用进程之外；容器里的应用也不该知道镜像之外的"最新版本"。
- **`install.sh` 形态的升级同理不由应用内触发**：动作是"解包到新版本目录 + 切 `current` + `systemctl restart`"，时机在运维手里（§2.4）。将来若真把 Linux 纳入检查，按 [update-check.md](./update-check.md) 的结论也只能做"提示 + 给出资产名与校验命令"、不做自替换 —— 应用不知道谁在托管它（systemd 还是容器），自重启会断掉正在用的连接。
- 三条形态的升级路径因此分开：Windows 安装器覆盖安装（[install-and-update.md](./install-and-update.md)）、`install.sh` 切 `current`、容器换 tag；共同点只有"数据根不动"，这一点由 [paths.py](../shell/backend/paths.py) 保证，不由任何升级器保证。

## 8. 验收清单

除 E / F 外都能在 WSL 或一台临时 Linux 里跑；每段以注释标出阶段，`# 通过：` 后面是判据。

```bash
# A. install.sh（用户级）
bash scripts/install.sh --user --port 18081
readlink -f ~/.local/lib/cubby/current          # 通过：指向 versions/<版本>
systemctl --user is-active cubby                # 通过：active
curl -fsS http://127.0.0.1:18081/health         # 通过：{"status":"ok"}
stat -c %a ~/.local/share/cubby/.config/auth_token.txt   # 通过：600
~/.local/bin/cubby --port 18082 &               # 通过：启动器透传 --port

# B. 起停与重启后自启（系统级换成 root 的 systemctl，is-enabled 为 enabled）
systemctl --user restart cubby && systemctl --user is-active cubby
loginctl enable-linger "$USER"; loginctl show-user "$USER" -p Linger   # 通过：Linger=yes
sudo systemctl reboot && systemctl --user is-active cubby   # 通过：未登录桌面也自行起来

# C. 升级不动数据（另需确认 app.json 的 trusted_hosts / data_root 没被重写）
sha256sum ~/.local/share/cubby/.config/auth_token.txt
bash scripts/install.sh --version <旧版本> && bash scripts/install.sh
readlink -f ~/.local/lib/cubby/current          # 通过：指向新版本
ls ~/.local/lib/cubby/versions                  # 通过：只剩两个目录（新版 + 上一版）
sha256sum ~/.local/share/cubby/.config/auth_token.txt   # 通过：摘要与升级前一致

# D. 卸载与 --purge
bash scripts/install.sh --uninstall; ls -d ~/.local/lib/cubby ~/.local/share/cubby   # 通过：程序根消失、数据根仍在
bash scripts/install.sh --uninstall --purge --yes; ls -d ~/.local/share/cubby        # 通过：数据根消失
systemctl --user status cubby                # 通过：could not be found

# E. 镜像
docker build -t cubby:dev .
docker run -d --name cubby-test -p 127.0.0.1:18080:18080 -v cubby-test:/data cubby:dev
docker inspect --format '{{.State.Health.Status}}' cubby-test      # 通过：healthy
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:18080/api/system_get_config   # 通过：401
docker exec cubby-test cat /data/.config/auth_token.txt            # 通过：有内容

# F. arm64（仅在本期决定做时）：构建日志里 pythonnet / pillow / pycryptodome 都必须来自 wheel
docker buildx build --platform linux/arm64 -t cubby:arm64 .
docker run --rm --platform linux/arm64 cubby:arm64 python -c "import webview, flask; print('ok')"
```

E 段另有两项人工判据：浏览器打开 `http://127.0.0.1:18080` 要能看到界面并列出插件（前端 dist 与 `res/` 都在）；用一个不在白名单的 Host 直连要得到 `400` 标记页（说明白名单生效、需要补 `trusted_hosts`）；`-v` 改成 bind mount 后 `ls -ln /data` 的属主必须是 `10001`，否则先 `chown`。

## 9. 风险与取舍

- **glibc 基线（已实装）**：发布用的 Linux 产物在 `debian:12` 容器里构建（`python3.11`、
  glibc 2.36），覆盖 Debian 12 / Synology DSM 7 / TrueNAS SCALE。原因与证据：在
  `ubuntu-latest`（24.04，glibc 2.39）上构建出的 tar.gz 到那些机器上会报
  `version 'GLIBC_2.36' not found`；改用容器后，产物在 `debian:12` 里启动并通过
  `/health`，在 `ubuntu:22.04`（2.35）里仍失败 —— 该发行版的 `python3` 是 3.10，本来
  也不满足 `requires-python`。构建与冒烟步骤在 `release.yml` 与 `package-smoke.yml` 的
  Linux 分支，机制见 [ci-and-release.md](./ci-and-release.md) 的打包链路一节。
- **无签名**：`.sha256` + HTTPS 是唯一凭据，脚本与镜像都不验 GPG 签名，发布账号被接管时校验值也会一起被换；与 Windows 侧同一风险，脚本无法消除。
- **镜像体积**：slim 基镜像 + 全量 `requirements.txt`（含 Linux 用不到的 `pywebview` / `pythonnet`）+ 前端 dist；瘦身要新增依赖文件，属独立改动。
- **多架构维护成本**：每加一个架构就多一份要验收的运行时与本地无法复现的调试路径；本期只做 amd64。
- **容器默认只监听回环**：`app.json` 默认 `127.0.0.1`，入口脚本不写 `0.0.0.0` 时 `-p` 形同虚设，症状是"连不上"而不是错误页，最容易被当成镜像坏了。
- **令牌与反代的三种误配**（都能从状态标记页读出原因）：`400` = Host 不在 `trusted_hosts`（或反代没改写 Host）；`401` = 请求没带令牌（Cookie 未种，或脚本忘了 `X-Cubby-Token`）；`403` = 请求路径越出允许目录。应用不设 `Secure` Cookie（`same_site='Lax'`，无 `Secure`），TLS 要由反代终止，不要把这套 HTTP 服务直接暴露到公网。
- **journal 里看不到应用日志**：冻结产物 `console=False`，stderr 不保证存在；排障先看 `.config/logs/cubby.log`。
