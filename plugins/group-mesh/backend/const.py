"""group-mesh 后端的共享常量、设置项 schema 与跨组件纯函数。

只有入口加载本文件：`DEFAULT_PORT` 与 `RULES` 在装配时作为构造参数注入各组件，
`SETTINGS_SCHEMA` 由入口的类属性引用。这样组件之间不需要互相 import（分层门禁
R3），也不需要在每个组件里各写一份同样的归一规则。

只被单个组件使用的常量（探测超时、上传上限、镜像 mtime 容差等）写在那个组件自己的
文件里：一处定义、一处读取，比"放到公共文件再传下去"更不容易漂移。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

# 默认监听端口，可被设置项覆盖。默认值同时用于设置项 schema 与节点的"端口没配"回落。
DEFAULT_PORT = 19443


class MeshRules:
    """跨组件共享的纯函数：调用形态归一、对端输入归一。无状态，入口构造一次。

    为什么收成一个对象而不是往每个组件的构造参数里各塞两个函数：需要它们的组件有
    八个，逐个传参会让签名不可读；它们又都是无状态的纯函数，收成一个注入对象既
    保住了"组件之间不互相 import"，也让签名只剩一个参数。
    """

    @staticmethod
    def opts(value: Any) -> Dict[str, Any]:
        """把"结构化参数"归一成字典。

        `/api/<plugin>__<method>` 会把 payload 的 `args`/`kwargs` 原样展开成位置/关键字
        参数（`shell/backend/file_server.py`），因此前端既可以传一个对象、也可以逐个传。
        两种形态都接受，避免以后前端或桥接实现改动时**参数静默错位**
        （例如 write 的值跑进 max_bytes，只在运行时才炸）。
        """
        return value if isinstance(value, dict) else {}

    @staticmethod
    def mtime_ns(payload: Any) -> Optional[int]:
        """从对端应答的一条记录里取 mtime（纳秒整数）；缺失或非法时返回 None。

        None 的语义是"对端没给"（老版本内核不带这个字段），此时物化的变更判定退化为
        **只看大小** —— 与加入这个字段之前的能力一致，不假装自己能发现同大小的替换。
        显式排除 bool：`True` 是 int 的子类，放过去会变成一个 mtime=1 的荒谬值。
        """
        if not isinstance(payload, dict):
            return None
        value = payload.get('mtime_ns')
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def safe_rel(value: Any) -> Optional[str]:
        """把对端给的条目名/路径归一成本机可用的相对路径；不可用时返回 None。

        对端是**不可信输入**：协议允许它回任何字符串，而调用方会把它拼成本机路径
        （`root / rel`）。Python 的 `Path` 除法在 rel 是绝对路径时**直接返回该路径**，
        `..` 段也不被拒绝 —— 于是一个 `../../../tmp/x` 或 `/tmp/x` 就能让"物化 /
        镜像 / 对账删除"作用到根之外（审计项 P1-5；实测可把根外文件覆盖为对端内容并
        删除根外文件）。这里是**唯一**的一处归一，物化与镜像两条路径共用：

          * 分隔符统一成正斜杠（Windows 上 `\\` 同样是分隔符，不能只挡 `/`）；
          * 拒绝空值、含 NUL、绝对路径（`/x`、`C:\\x`、`\\\\server\\share`）；
          * 拒绝任何一段是 `..`；
          * 丢弃空段与 `.` 段（`a//b` 与 `a/./b` 归一成 `a/b`）。

        合法输入（`img.jpg`、`作者/作品/1.jpg`）原样返回（分隔符已归一）。
        """
        if not isinstance(value, str):
            return None
        if '\x00' in value:
            return None                      # NUL 会让底层路径调用截断，直接拒绝而不是清洗
        text = value.replace('\\', '/')
        if not text.strip():
            return None
        if text.startswith('/'):
            return None
        if len(text) >= 2 and text[1] == ':' and text[0].isalpha():
            return None                      # C:/x 这类盘符绝对路径
        segments = [seg for seg in text.split('/') if seg not in ('', '.')]
        if not segments or any(seg == '..' for seg in segments):
            return None
        return '/'.join(segments)

    @staticmethod
    def within_root(root: Any, rel: str) -> Optional[Path]:
        """`root / rel` 解析后的绝对路径；不在 root 之内时返回 None。

        纵深防御：`safe_rel` 已经挡过一轮，这一层保证"即使以后有人放宽了名字规则，
        落盘/删除目标也仍在根内"，并顺带覆盖符号链接把路径引到根外的情形
        （resolve() 之后比较，因此链接目标也必须在根内）。
        """
        try:
            base = Path(root).resolve()
            target = (base / rel).resolve()
        except (OSError, ValueError):
            return None
        return target if target.is_relative_to(base) else None


#: 唯一实例：入口把它按构造参数注入需要的组件。
RULES = MeshRules()

#: 设置项 schema。纯数据，`PluginBase` 与壳的设置弹窗都从入口的类属性读。
SETTINGS_SCHEMA: List[Dict[str, Any]] = [
    {'key': 'port', 'label': '监听端口', 'type': 'number',
     'default': DEFAULT_PORT, 'min': 1024, 'max': 65535,
     'section': 'node', 'sectionTitle': '节点与网络',
     'sectionDescription': '节点监听地址、同步间隔；修改端口后需要重启节点。',
     'sectionOrder': 30, 'order': 10,
     'help': '共享节点对外提供服务的 TCP 端口。修改后需要重启节点。'},
    {'key': 'bind', 'label': '监听地址', 'type': 'text', 'default': '::',
     'section': 'node', 'sectionTitle': '节点与网络',
     'sectionDescription': '节点监听地址、同步间隔；修改端口后需要重启节点。',
     'sectionOrder': 30, 'order': 20,
     'help': '默认 :: 同时接受 IPv6 与 IPv4。以公网 IPv6 为目标，'
             '只有单个 IPv6 地址时填写该地址。'},
    {'key': 'group_name', 'label': '团体名称', 'type': 'text', 'default': '',
     'section': 'identity', 'sectionTitle': '身份',
     'sectionDescription': '团体名称与本机显示名；加入团体后修改团体名称不会改名。',
     'sectionOrder': 10, 'order': 10,
     'help': '创建团体时使用的标识。已加入团体后修改此项不会改名。'},
    {'key': 'principal_name', 'label': '本机主体名称', 'type': 'text', 'default': '',
     'section': 'identity', 'sectionTitle': '身份',
     'sectionDescription': '团体名称与本机显示名；加入团体后修改团体名称不会改名。',
     'sectionOrder': 10, 'order': 20,
     'help': '界面显示用的名字。留空时取主机名。'},
    {'key': 'ttl_days', 'label': '名单有效期（天）', 'type': 'number',
     'default': 180, 'min': 1, 'max': 3650,
     'section': 'roster', 'sectionTitle': '名单',
     'sectionDescription': '团体名单的提示有效期；到期不阻断通信，界面会标注已过期。',
     'sectionOrder': 20, 'order': 10,
     'help': '仅作提示：到期不阻断通信，界面会标注已过期。'},
    # 位置之四：从对端取回的文件落点。留空时回落到 <数据根>/group-mesh/downloads。
    # 由 Shell 的 FolderPicker 渲染（`directory` 类型），值仍是字符串，
    # 因此这里不需要为它写任何解析代码。
    # `local_only`：本字段**只接受本机目录**，不显示「网络位置」按钮 ——
    # 那是"把远端共享项取回本地"的入口，而这里正是取回后的落点，语义上不能用它
    # 自己当自己的来源（而且取回的中间目录不是用户想要的下载目录）。
    {'key': 'download_dir', 'label': '远端下载目录', 'type': 'directory',
     'local_only': True, 'admin_only': True,
     'default': '', 'placeholder': '默认：数据根/group-mesh/downloads',
     'section': 'transfer', 'sectionTitle': '远端取回',
     'sectionDescription': '从成员取回文件时的落点与单文件上限；下载目录改动需管理员。',
     'sectionOrder': 40, 'order': 10,
     'help': '从团体成员那里取回的文件保存在这里。该目录经 /file 对界面可读，'
             '但不对团体共享 —— 要共享它请单独挂一个共享项（改动需管理员）。'},
    {'key': 'max_fetch_mb', 'label': '按需取回单文件上限（MiB）', 'type': 'number',
     'default': 1024, 'min': 0, 'max': 102400,
     'section': 'transfer', 'sectionTitle': '远端取回',
     'sectionDescription': '从成员取回文件时的落点与单文件上限；下载目录改动需管理员。',
     'sectionOrder': 40, 'order': 20,
     'help': '浏览远端目录时，超过这个大小的文件不会自动取回（0 表示不限制）。'
             '远端目录先物化成目录结构与占位文件，字节在第一次取回时才取 —— '
             '这个上限用来避免"点开一个目录就把磁盘写满"。'},
    {'key': 'sync_interval_seconds', 'label': '后台同步间隔（秒）', 'type': 'number',
     'default': 60, 'min': 5, 'max': 3600,
     'section': 'node', 'sectionTitle': '节点与网络',
     'sectionDescription': '节点监听地址、同步间隔；修改端口后需要重启节点。',
     'sectionOrder': 30, 'order': 30,
     'help': '节点运行期间每隔这么久拉取一次注册表与团体名单，并把本机变更推给对端。'
             '默认 60 秒；调小更及时，但会更频繁地连接对端。'},
]
