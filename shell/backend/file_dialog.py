'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

系统文件对话框（桌面窗口模式）。

**为什么实现放在这里而不是 main.py**：桌面模式下 Flask 与 pywebview 在同一个进程里
（`main.py` 用线程起 Flask），所以 HTTP 路径同样能打开系统对话框。两条路径注册同一个
函数，才不会出现"前端落到 HTTP shim 时桌面能力整体失效"—— 真实案例：设置页点
「选择图片…」弹出"浏览器访问模式没有系统文件对话框"。那句话的前提（HTTP = 浏览器
模式）本身就是错的：真正决定能不能弹的是**有没有桌面窗口**，而不是请求走了哪条路。

`import webview` 放在函数内部：file_server 会被测试与 `--web-only` 导入，而 webview
在没有 GUI 依赖的环境里导入即失败，不该让整条导入链跟着挂。

另一件容易说反的事：背景图**始终是服务端那台机器**的资源。浏览器访问时页面本身由
服务端提供，用户要选的同样是服务端上的图片 —— 客户端自己的文件属于另一条需求，那条
路要先把图片上传到服务端存一份，与"按原路径读取"是两种设计，当前未实现。

所以本函数在两种访问方式下语义一致：**在服务端进程里弹服务端机器的对话框**。
真正决定成败的只有"这台机器上有没有可用的桌面窗口"（`--web-only` 没有）。由此还
分出一层：本机访问（桌面窗口或本机浏览器）能直接看到对话框；远程浏览器访问时对话框
出现在服务端的桌面上、使用者看不到，那种场景需要的是"在页面里浏览服务端目录"或
"上传客户端图片"，不是这里能解决的。

**待验证的一点（桌面模式点「选择图片…」无反应时先看这里）**：pywebview 6.2.1 的
Windows 实现在**调用者的线程**上直接执行 `dialog.ShowDialog()`（`webview/platforms/
winforms.py` 的 `create_file_dialog`），而本函数的两条通道都在工作线程上执行
（Flask 是 `threaded=True`，pywebview 自己在 `util.js_bridge_call` 的说明里写明
js_api 回调"在独立线程里执行"）。WinForms 的通用文件对话框要求 STA 线程，非 STA
线程上会抛 `ThreadStateException` —— 而 pywebview 把该异常吞进自己的 logger 并
`return None`，本函数只能把它当成"用户取消"（`success=True` + 空 `path`）。
因此这一点在真实窗口里确认一次：桌面模式点「选择图片…」，同时看
`<config_dir>/logs/omnibox.log` 有没有 pywebview 记的 `Error invoking ... dialog`。
若有，需要的是把调用切到 GUI 线程（pywebview 没有公开入口，得走平台层），而不是
继续排查前端的调用链。
'''

import logging
import threading

log = logging.getLogger(__name__)

#: 选图片时的过滤器。另给一个"所有文件"兜底：壁纸的扩展名不总是那几个常见值。
IMAGE_FILE_TYPES = ('图片 (*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp)', '所有文件 (*.*)')

#: `kind` 里表示"选目录"的取值（其余一律按"选文件"处理）
_DIRECTORY_KINDS = ('directory', 'dir', 'folder')

#: 系统对话框同一时刻只允许一个。
#:
#: `create_file_dialog` 会阻塞调用它的线程直到用户作答，而桌面模式下 Flask 是
#: `app.run(threaded=True)`、pywebview 的 js_api 回调也在独立线程里跑（`util.js_bridge_call`
#: 的说明）：并发或连点会叠出多个模态对话框，每个都占住一个线程，用户关掉一个还有
#: 下一个在等。用一把非阻塞锁把并发收敛成一条明确的结果。
_dialog_lock = threading.Lock()


def pick_path(kind: str = 'image') -> dict:
    """打开系统文件对话框，返回 ``{'success': bool, 'path': str, 'error': str}``。

    用户取消**不算失败**：返回 ``success=True`` 且 ``path`` 为空，调用方据此区分
    "用户没选"与"这个环境选不了"—— 两者在界面上的表现完全不同。

    同一时刻只服务一个对话框（见 `_dialog_lock`）：第二个并发调用直接返回失败，
    而不是排队叠出一个新的模态窗口。

    已知的模糊点：pywebview 在"对话框没能弹出来"时也返回空值（异常被它自己吞掉并
    记进它自己的 logger），因此这种情况会被当成"用户取消"（`success=True` + 空
    `path`）。本函数只能补一条日志，无法把它翻成失败。
    """
    try:
        import webview
    except Exception as e:  # pragma: no cover - 取决于运行环境是否有 GUI 依赖
        return {'success': False, 'error': f'当前环境没有桌面窗口组件: {e}'}

    windows = getattr(webview, 'windows', None) or []
    if not windows:
        return {'success': False, 'error': '当前没有桌面窗口，无法打开系统文件对话框'}

    wants_dir = str(kind or '').strip().lower() in _DIRECTORY_KINDS
    if not _dialog_lock.acquire(blocking=False):
        return {'success': False,
                'error': '已有一个系统文件对话框在等待操作，请先处理它'}
    try:
        picked = windows[0].create_file_dialog(
            webview.FileDialog.FOLDER if wants_dir else webview.FileDialog.OPEN,
            allow_multiple=False,
            # 目录对话框不接受 file_types
            file_types=() if wants_dir else IMAGE_FILE_TYPES,
        )
    except Exception as e:
        log.error(f'[FileDialog] 系统文件对话框调用失败: {e}')
        return {'success': False, 'error': f'打开系统文件对话框失败: {e}'}
    finally:
        _dialog_lock.release()

    # 各平台实现的返回形态并不完全一致（文档写的是元组），这里统一归一：不归一的话，
    # 一旦某个实现返回字符串，`picked[0]` 取到的是路径的第一个字符，界面把 `D` 当成
    # 目录去配置，报的却是"目录不存在"。
    if isinstance(picked, (list, tuple)):
        selected = str(picked[0]) if picked else ''
    else:
        selected = str(picked) if picked else ''
    if not selected:
        # pywebview 在"对话框没能弹出来"时同样返回 None（异常记在它自己的 logger 里），
        # 与"用户取消"在返回值上无法区分。补一条日志，让"点了没反应"留下可查的痕迹。
        log.info('[FileDialog] 系统文件对话框返回空：用户取消，或对话框未能弹出')
        return {'success': True, 'path': ''}
    return {'success': True, 'path': selected}
