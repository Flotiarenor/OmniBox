"""多实例夹具：在同一台机器上起若干个**空白** OmniBox 实例，用端口模拟不同网络。

## 为什么需要它

`tests/test_group_mesh_shell_e2e.py` 已经能起真实 Shell，但它用的是**仓库的数据根**
（没有实例隔离）—— 于是同一台机器上只能存在一个实例，真要验"两台设备互相发现 /
取字节"就必须搬来一台 Linux 机器，代码同步与联调成本很高。

这里补上缺的那一环：`OMNIBOX_HOME`（见 `shell/backend/paths.py`）把每个实例的
`.config`、`data`、日志、插件设置、身份私钥、派生缓存全部落到自己的目录里。
于是 n 个实例 = n 台设备，"不同端口"就是模拟出来的"不同网络"。实例默认全空：
没有身份、没有名单、没有共享项、没有任何历史缓存。

## 三条必须遵守的约束（都读过代码确认）

1. **必须多进程。** `paths.get_user_data_dir()` 是进程级缓存，`file_server._SHELL_DIR`、
   `PluginManager._instances`、日志 handler 与 `atexit` 注册也都是进程级状态 ——
   同一个进程里起不了两个状态不同的实例。
2. **回环地址不会被自动注册。** 内核 `registry.local_addresses()` 显式过滤回环，
   所以两个实例只能靠**手工对端**（`peers(action='add')`）互相发现；实例自己监听
   到的真实地址从 `get_node_status()['listening']` 取（设置 `port=0` 时由内核分配）。
3. **web 端口 ≠ 节点端口。** 前者是 `server.port`（命令行 `--port`），后者是
   group-mesh 的插件设置项 —— 两者都要按实例分配，否则第二个实例必然撞端口。
4. **必须杀整棵进程树，不能只 `terminate()`。** Windows 上 `venv\\Scripts\\python.exe`
   是个启动器，它会再起一个真正的解释器去跑 `main.py`：只杀启动器会留下**孤儿服务**
   继续占端口、SQLite 与日志文件（实测：演示脚本退出码 0，两个实例仍在跑，17 个临时
   目录删不掉）。`_terminate_tree()` 用 `taskkill /F /T`（Windows）或独立进程组 +
   `killpg`（POSIX）整棵杀掉。**测这条千万别用带 `SO_REUSEADDR` 的探测** —— Windows 上
   它对着正在监听的孤儿也能绑成功，检测直接失效。

## 用法

```python
with MeshCluster(root, names=('owner', 'member')) as cluster:
    cluster.form_group()                       # 建团 + 加成员 + 名单逐级推进
    endpoint = cluster.start_nodes()           # 起共享节点，拿到真实监听地址
    cluster.link(cluster.members[0], endpoint) # 手工登记对端
    result = cluster.members[0].call('group-mesh', 'list_remote', {'device_id': ''})
```
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER
from shell.backend.paths import USER_DATA_DIR_ENV

STARTUP_TIMEOUT = 90.0
SHUTDOWN_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0
# 停掉实例后等端口被回收的上限：Windows 在进程退出后**异步**回收句柄，
# 零容忍会把正常的几毫秒拆卸期误报成"有孤儿进程"。
PORT_RELEASE_TIMEOUT = 10.0
_POLL_INTERVAL = 0.2

# 真实应用启动时会 import 的模块（都在 requirements.txt 里）。缺任何一个就说明
# 这台机器跑不动应用 —— 典型是 headless Linux 没有 pywebview 的 GUI 后端。
# 用例据此 skip，而不是把"环境不具备"报成失败。
_REQUIRED_MODULES = ('webview', 'flask', 'yaml', 'requests', 'PIL', 'Crypto')


class AppStartFailure(RuntimeError):
    """实例没能起来。消息里带上启动日志尾部，免得只看到一句"未就绪"。"""


def free_port() -> int:
    """取一个当前空闲的本地端口。

    探测与实际绑定之间天然有竞态（探测完就关掉了套接字），测试场景可接受：
    真撞上时实例会启动失败并给出日志，重跑一次即可。
    """
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def port_is_free(port: int) -> bool:
    """端口现在能不能重新绑定 —— 孤儿实例还占着它时绑不上。

    **刻意不设 `SO_REUSEADDR`**：Windows 上它会允许绑到一个**正在被监听**的端口
    （实测：孤儿在 11515 上监听时，带 REUSEADDR 的探测照样成功），于是这条判据在
    "有没有孤儿"这件事上完全失效。不设它才会拿到 WSAEADDRINUSE(10048)。
    """
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def _process_group_kwargs() -> Dict[str, Any]:
    """让实例自成进程组 / 会话，便于整棵杀掉。

    Windows：`CREATE_NEW_PROCESS_GROUP` 让 `taskkill /T` 之外的信号操作也成为可能，
    并避免我们的 Ctrl+C 直接打到子进程上（收尾交给 `stop()`）。
    POSIX：`start_new_session=True` 让子进程成为会话/进程组首领，`os.killpg` 才能
    覆盖"插件自己又起了子进程"的情况。
    """
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def boot_prerequisites() -> tuple[bool, str]:
    """本机能不能真起一个应用实例。返回 `(可以, 原因)`，供 `skipUnless` 使用。

    刻意只查"能不能 import"，不查"能不能起服务"：把环境问题和真实缺陷分开 ——
    环境不具备时 skip，环境具备而服务起不来则必须失败。
    """
    entry = PROJECT_ROOT / 'main.py'
    if not entry.is_file():
        return False, f'找不到应用入口: {entry}'
    code = 'import ' + ', '.join(_REQUIRED_MODULES)
    proc = subprocess.run([sys.executable, '-c', code], cwd=str(PROJECT_ROOT),
                          capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return True, ''
    detail = (proc.stderr or proc.stdout or '').strip().splitlines()
    return False, f'无法导入运行时依赖: {detail[-1] if detail else proc.returncode}'


def wait_until(predicate: Callable[[], Any], timeout: float, interval: float = _POLL_INTERVAL) -> Any:
    """轮询直到 `predicate()` 返回真值；超时返回最后一次的假值（不抛异常）。"""
    deadline = time.time() + timeout
    result: Any = None
    while time.time() < deadline:
        try:
            result = predicate()
        except Exception:
            result = None
        if result:
            return result
        time.sleep(interval)
    return result


class AppInstance:
    """一个空白实例：独立用户数据目录 + 独立 web 端口 + 独立节点端口。"""

    def __init__(self, name: str, root: str | Path, *, node_port: int = 0,
                 node_bind: str = '127.0.0.1',
                 plugin_settings: Dict[str, Dict[str, Any]] | None = None,
                 extra_args: Sequence[str] = (),
                 startup_timeout: float = STARTUP_TIMEOUT) -> None:
        self.name = name
        self.root = Path(root)
        self.user_data = self.root / name
        self.config_dir = self.user_data / '.config'
        self.data_root = self.user_data / 'data'
        self.node_port_setting = node_port
        self.node_bind = node_bind
        self.plugin_settings = dict(plugin_settings or {})
        # 追加到命令行的入口参数（如 `--data-root`）；夹具只负责传，不解释。
        self.extra_args = list(extra_args)
        self.startup_timeout = startup_timeout
        self.port = free_port()
        self.base_url = f'http://127.0.0.1:{self.port}'
        self.token = ''
        self._proc: subprocess.Popen | None = None
        self._log_path = self.root / f'{name}.boot.log'

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self) -> AppInstance:
        self.root.mkdir(parents=True, exist_ok=True)
        # "空白"是硬要求，不是默认值：夹具的全部价值就在于每个实例都从零开始。
        # 目录已存在说明上一次运行留下了状态（身份私钥、注册表、缓存），此时
        # 静默复用会让用例在"有历史"的前提下变绿，掩盖真正的初始化缺陷。
        if self.user_data.exists():
            raise RuntimeError(
                f'实例目录不是空的，夹具拒绝复用: {self.user_data}'
                f'（请让每次运行使用全新的临时根目录）')
        self._write_plugin_settings()

        env = {**os.environ, USER_DATA_DIR_ENV: str(self.user_data),
               # 启动日志必须即时落盘：实例起不来时它就是唯一的诊断来源。
               'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
        # `Popen` 拿到的是子进程自己的句柄，父进程这一份可以在返回后立刻关掉 ——
        # 所以不必把文件对象挂到实例上（SIM115）。
        with open(self._log_path, 'w', encoding='utf-8') as log:
            self._proc = subprocess.Popen(
                [sys.executable, 'main.py', '--web-only', '--port', str(self.port),
                 *self.extra_args],
                cwd=str(PROJECT_ROOT), env=env,
                stdout=log, stderr=subprocess.STDOUT, **_process_group_kwargs())

        if not self._wait_health():
            tail = self.boot_log_tail()
            self.stop()
            raise AppStartFailure(
                f'实例 {self.name} 未在 {self.startup_timeout:.0f}s 内就绪'
                f'（端口 {self.port}，日志 {self._log_path}）\n{tail}')
        self.token = self._read_token()
        return self

    def stop(self) -> None:
        """结束实例（整棵进程树），并自检端口确实被释放。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            self._terminate_tree(proc)
        # 端口不是"杀掉就立刻可用"：Windows 在进程退出后异步回收句柄，因此给一个
        # 短暂的回落窗口再判定（零容忍会把正常的几毫秒拆卸期报成"有孤儿"）。
        if not wait_until(lambda: port_is_free(self.port), PORT_RELEASE_TIMEOUT):
            print(f'[harness] 实例 {self.name} 停止后端口 {self.port} 仍被占用，'
                  f'很可能有孤儿进程（pid={proc.pid}）')

    @staticmethod
    def _terminate_tree(proc: subprocess.Popen) -> None:
        """杀掉实例**及其子进程**。

        为什么不能只 `terminate()`：Windows 上 `venv\\Scripts\\python.exe` 是启动器，
        它会再起一个真正的解释器去跑 `main.py` —— 杀掉启动器只留下孤儿服务继续占着
        端口、SQLite 与日志文件。实测：演示脚本退出码 0，两个实例的进程仍在跑，
        17 个临时目录删不掉；而同一现象我最初误判成"杀毒软件短暂占用文件"，
        给清理加了重试 —— 那只是盖住了症状。
        """
        if os.name == 'nt':
            # /T 连子进程一起杀（实测输出会明确打印被终止的子进程 PID）。
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, check=False)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        try:
            proc.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                print(f'[harness] 进程 {proc.pid} 未能停止')

    def __enter__(self) -> AppInstance:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def restart(self) -> AppInstance:
        """用**同一份数据目录**重启：验持久化与"重启后仍然正确"才需要它。

        与 `start()` 的区别只有一条：不检查目录是否为空（那正是本方法要保留的状态）。
        """
        self.stop()
        env = {**os.environ, USER_DATA_DIR_ENV: str(self.user_data),
               'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
        with open(self._log_path, 'a', encoding='utf-8') as log:
            self._proc = subprocess.Popen(
                [sys.executable, 'main.py', '--web-only', '--port', str(self.port),
                 *self.extra_args],
                cwd=str(PROJECT_ROOT), env=env,
                stdout=log, stderr=subprocess.STDOUT, **_process_group_kwargs())
        if not self._wait_health():
            tail = self.boot_log_tail()
            self.stop()
            raise AppStartFailure(f'实例 {self.name} 重启失败\n{tail}')
        return self

    def boot_log_tail(self, lines: int = 30) -> str:
        try:
            content = self._log_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return '(没有启动日志)'
        return '\n'.join(content.strip().splitlines()[-lines:])

    # ── HTTP ──────────────────────────────────────────────────────────────

    def system(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """调壳自己的 `/api/<method>`（如 `system_get_plugins`）。"""
        return self._post(f'/api/{method}', args, kwargs)

    def call(self, plugin: str, method: str, opts: Any = None, **kwargs: Any) -> Any:
        """调插件方法：`POST /api/<plugin>__<method>`。

        `opts` 作为**单个位置参数**传，只适用于按 `_opts()` 约定写的插件
        （group-mesh）；其余插件（image-viewer 等）的参数是普通形参，必须用
        `**kwargs` —— 传 dict 会让它去 `dict.replace(...)`。
        """
        args: List[Any] = [] if kwargs else ([opts] if opts is not None else [])
        return self._post(f'/api/{plugin}__{method}', args, kwargs)

    def get(self, path: str, timeout: float = CALL_TIMEOUT) -> tuple[int, bytes, Dict[str, str]]:
        """带令牌直接 GET（`/file`、`/thumbs`、`/health` 这类非 JSON 路由）。

        返回 `(status, body, headers)`；HTTP 错误码不抛异常 —— 对 `/file` 而言
        403/404 正是要被断言的正常结果。
        """
        url = path if path.startswith('http') else f'{self.base_url}{path}'
        request = urllib.request.Request(url, headers={TOKEN_HEADER: self.token})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})

    def file_url(self, path: str | Path, plugin: str = 'group-mesh') -> str:
        """`/file?path=…&plugin=…` 的完整 URL（与 `Bridge.originalUrl` 同一形状）。"""
        query = urllib.parse.urlencode({'path': str(path), 'plugin': plugin})
        return f'/file?{query}'

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _write_plugin_settings(self) -> None:
        """在实例启动**之前**写插件设置文件 —— 这样节点端口/绑定的设置才生效。

        位置是 `<user_data>/.config/plugins/<插件名>.json`（`SettingsStore` 的
        路径规则，`shell/backend/settings_store.py`）。直接写文件而不是走
        `<插件>__save_settings`：后者要求插件实例已启动并注册好 API，而这里要的
        正是"实例启动时就带着正确端口"，用它会让这件事退化成"启动后再改、再重启节点"。
        """
        payload: Dict[str, Dict[str, Any]] = {}
        for plugin_name, values in self.plugin_settings.items():
            payload[plugin_name] = dict(values)
        node = payload.setdefault('group-mesh', {})
        node.setdefault('port', self.node_port_setting)
        node.setdefault('bind', self.node_bind)
        node.setdefault('principal_name', self.name)
        node.setdefault('group_name', self.name)

        settings_dir = self.config_dir / 'plugins'
        settings_dir.mkdir(parents=True, exist_ok=True)
        for plugin_name, values in payload.items():
            target = settings_dir / f'{plugin_name}.json'
            target.write_text(json.dumps(values, ensure_ascii=False, indent=2) + '\n',
                              encoding='utf-8')

    def _wait_health(self) -> bool:
        found = wait_until(lambda: self.get('/health', timeout=1.0)[0] == 200,
                           self.startup_timeout)
        return bool(found)

    def _read_token(self) -> str:
        """读本实例的访问令牌文件（`<config>/auth_token.txt`，启动时已生成）。"""
        token_file = self.config_dir / 'auth_token.txt'
        token = wait_until(lambda: token_file.read_text(encoding='utf-8').strip()
                           if token_file.is_file() else '', 10.0)
        if not token:
            raise AppStartFailure(f'实例 {self.name} 没有生成访问令牌: {token_file}')
        return str(token)

    def _post(self, path: str, args: Iterable[Any], kwargs: Dict[str, Any]) -> Any:
        body = json.dumps({'args': list(args), 'kwargs': dict(kwargs)},
                          ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(
            f'{self.base_url}{path}', data=body, method='POST',
            headers={'Content-Type': 'application/json', TOKEN_HEADER: self.token})
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'{path} 返回 HTTP {e.code}: {raw[:400]}') from None
        if isinstance(payload, dict) and 'error' in payload:
            raise RuntimeError(f'{path} 调用失败: {payload["error"]}')
        return payload.get('result') if isinstance(payload, dict) else payload


class MeshCluster:
    """n 个空白实例 + 把它们连成一个团体的最短路径。

    `names` 的第一项是群主（签发名单的那台），其余是普通成员。
    """

    def __init__(self, root: str | Path, names: Sequence[str] = ('owner', 'member'),
                 *, node_bind: str = '127.0.0.1',
                 plugin_settings: Dict[str, Dict[str, Any]] | None = None) -> None:
        self.root = Path(root)
        self.instances: List[AppInstance] = [
            AppInstance(name, self.root, node_bind=node_bind, plugin_settings=plugin_settings)
            for name in names
        ]

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self) -> MeshCluster:
        started: List[AppInstance] = []
        try:
            for instance in self.instances:
                instance.start()
                started.append(instance)
        except Exception:
            # 后一个起不来时把前面的收掉：否则子进程会活到测试进程结束，
            # 端口与 SQLite 文件都被占着，下一次运行报"目录不为空"。
            for instance in started:
                instance.stop()
            raise
        return self

    def stop(self) -> None:
        for instance in self.instances:
            instance.stop()

    def __enter__(self) -> MeshCluster:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def owner(self) -> AppInstance:
        return self.instances[0]

    @property
    def members(self) -> List[AppInstance]:
        return self.instances[1:]

    # ── 组网 ──────────────────────────────────────────────────────────────

    def call(self, instance: AppInstance, method: str, opts: Any = None) -> Any:
        """调 group-mesh 的插件方法，并断言 `success`（失败时把原样结果塞进异常）。"""
        result = instance.call('group-mesh', method, opts)
        if not isinstance(result, dict) or not result.get('success'):
            raise AssertionError(f'{instance.name}.{method} 失败: {result}')
        return result

    def form_group(self, group: str = 'harness') -> str:
        """建团 →（群主）逐个加成员 → 分发邀请串，返回最终名单的邀请串。

        **为什么不能"先加完所有人、再分发一份最终名单"**：`join_group` 只接受
        **直接后继**（设计文档 §5.2 规则 2 要求 `prev == hash(当前名单)`）。一次加
        两个人会得到 v3，而成员手里是 v1，`prev` 指向 v2 而不是 v1 —— 直接被拒。
        所以按版本逐级推进：每加一名成员，所有非群主实例都更新到该版本。
        """
        for instance in self.instances:
            self.call(instance, 'init_identity', {'name': instance.name})

        created = self.call(self.owner, 'create_group', {'group': group})
        invite = created['invite']
        for member in self.members:
            self.call(member, 'join_group', {'invite': invite})

        for member in self.members:
            keys = self.call(member, 'get_device_keys')
            added = self.call(self.owner, 'add_member',
                              {'principal': keys['principal'], 'device': keys['device'],
                               'name': member.name})
            invite = added['invite']
            for peer in self.members:
                self.call(peer, 'join_group', {'invite': invite})
        return invite

    def start_nodes(self) -> List[str]:
        """每台实例都起共享节点，返回各自的真实监听地址（`host:port`）。

        节点端口在设置里写 0 时由内核分配，因此**必须**从 `get_node_status()`
        读真实值 —— 拿设置里的值会指向一个没人听的端口（插件注释里记着这条）。
        """
        endpoints: List[str] = []
        for instance in self.instances:
            result = instance.call('group-mesh', 'start_node')
            if not isinstance(result, dict) or not result.get('success'):
                raise AssertionError(f'{instance.name}.start_node 失败: {result}')
            status = wait_until(
                lambda inst=instance: (inst.call('group-mesh', 'get_node_status') or {}).get('listening'),
                30.0)
            if not status:
                raise AssertionError(f'{instance.name} 的节点没有进入监听状态')
            endpoints.append(str(status))
        return endpoints

    def link(self, consumer: AppInstance, endpoint: str, name: str = '') -> None:
        """把 `consumer` 指向某个已起节点的实例（手工对端）。

        回环地址不会被 `registry.local_addresses()` 注册，所以同机多实例之间**只能**
        走这条路 —— 它同时也是设计文档 §7.4 承认的引导手段（带外填写地址）。
        """
        self.call(consumer, 'peers', {'action': 'add', 'endpoint': endpoint,
                                      'name': name or endpoint})
