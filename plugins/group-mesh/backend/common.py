"""group-mesh 插件后端各分片共用的模块级常量与纯函数。

为什么单独一个文件：后端入口由 PluginManager 用 importlib 直接加载，分片之间不能
import main.py（会成环），所以共用的常量与纯函数需要一个中立的落点。main.py 把这些
名字重新绑定成原来的名字，于是 main.py 的既有引用与测试对模块级名字的引用都不用改。
"""

from typing import Any, Dict, Optional

# 默认监听端口。与设计文档 §13 的默认参数一致，可被设置项覆盖。
DEFAULT_PORT = 19443

# 自动启动节点失败后的冷却秒数。取 30 秒是因为最常见的失败原因是"旧进程还占着
# 端口"（服务重启后的窗口期），而启动流程自身还有 8 秒的 ready 等待。
AUTO_START_RETRY_SECONDS = 30

# 按需取回单个文件的默认上限（1 GiB）。浏览远端目录时不该因为点开一个目录就把
# 磁盘写满；超过上限的文件不自动取，界面会提示。
DEFAULT_MAX_FETCH_BYTES = 1024 * 1024 * 1024

# 探测端点的连接超时（秒）。刷新设备时要试多个端点，而设备常同时发布 IPv6 与
# IPv4 —— IPv6 在本机没有路由时每次都要等满超时。内核默认 20 秒，实测 5 个端点
# 串行等下来是 80 秒，界面看起来就是"卡住不动"。探测用短超时，真正的文件传输
# 仍走内核默认值。
PROBE_TIMEOUT_SECONDS = 3.0

# 一次刷新的候选探测**总**等待上限（秒）。到点就带着已有结果返回，不再等剩下的
# 端点 —— 界面上这是一次点击，不能因为几条死地址把用户按在那里。
PROBE_OVERALL_SECONDS = 4.0

# 后台同步一轮的总等待上限（秒）。后台不面向"点一下要立刻看到"，因此等所有候选
# 都试一遍，尽量从多个对端合并到最新注册表/名单；但也不能让一轮无限拖住下一轮。
SYNC_OVERALL_SECONDS = 10.0

# 上传的 socket 超时（秒）。比探测用的 3 秒宽得多：一个分块要走 Noise 加密 + 落盘，
# 慢盘或大分块下 3 秒会误判成"传输失败"。取消不靠超时兜底 —— 客户端在分块边界
# 检查取消标志（见 _upload_worker）。
UPLOAD_TIMEOUT_SECONDS = 30.0

# 上传任务表里保留多少个**已结束**的任务。界面要能看到最后一次的结果（成功/失败原因），
# 但也不能无限攒 —— 每个任务只是一小段字典，留 20 个足够翻看。
UPLOAD_KEEP = 20

# 上传任务表落盘的文件名（在插件数据根下）。与 uploads.json（续传记录）分开：
# 那份每次上传进度更新都可能要写，这份只记状态与快照，混在一起会让"进度写坏"牵连到
# "任务历史"。
UPLOAD_TASK_FILE = 'upload-tasks.json'
def _opts(value: Any) -> Dict[str, Any]:
    """把"结构化参数"归一成字典。

    `/api/<plugin>__<method>` 会把 payload 的 `args`/`kwargs` 原样展开成位置/关键字
    参数（`shell/backend/file_server.py`），因此前端既可以传一个对象、也可以逐个传。
    两种形态都接受，避免以后前端或桥接实现改动时**参数静默错位**
    （例如 write 的值跑进 max_bytes，只在运行时才炸）。
    """
    return value if isinstance(value, dict) else {}


# 镜像（"网络位置"）判定"这个文件已经一致"时 mtime 的容差（纳秒）。
# 取 2 秒是因为 FAT/exFAT 的 mtime 粒度就是 2 秒，而本地文件系统会量化 os.utime
# 写下的值：要求逐 ns 相等会把同一份文件判成"需要重下"，每次执行都全量重传。
MIRROR_MTIME_WINDOW_NS = 2_000_000_000


def _mtime_ns(payload: Any) -> Optional[int]:
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
