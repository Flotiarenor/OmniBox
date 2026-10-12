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

把 GPU 适配器偏好送进 WebView2 的 GPU 进程。

WebView2 Runtime 与 NVIDIA 驱动都是闭源二进制，该参数为何能消除拖动卡顿读不到，
因此这里只负责两件可验证的事：请求被送达（`msedgewebview2.exe` 的 browser 与
gpu-process 命令行都会带上它）、以及可被关掉（`shell_info.WEBVIEW_HIGH_PERFORMANCE_ENV`）。

为什么不能设环境变量：WebView2 的 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 只在
**没有**以代码设置 `CoreWebView2EnvironmentOptions.AdditionalBrowserArguments` 时
生效，而 pywebview 无条件设置了它（`platforms/edgechromium.py`）。实测：默认启动
路径上设该环境变量后，browser 与 gpu-process 的命令行都没有对应参数。

为什么要拦**写**而不是拦读：`AdditionalBrowserArguments` 的值存在 .NET 侧的属性里，
只有在 Python 侧发生写入时才会传过去。只拦读（让读到的字符串多出参数）**不足以**
让底层值变化 —— 之所以曾一度生效，是因为 pywebview 随后又做了一次 `+=`
（`platforms/edgechromium.py` 的 `--allow-file-access-from-files` 那行），把带参数的
字符串写了回去。一旦那次写回不执行（例如 `ALLOW_FILE_URLS` 被改成 False），参数就会
静默不下发，且没有任何异常与日志。拦写则不依赖后续任何动作。

为什么要替换 `CoreWebView2CreationProperties` 类而不是事后改实例：
`self.webview.CreationProperties = props` 一旦执行，CoreWebView2 的初始化即开始，
此后改该属性会抛
`InvalidOperationException: CreationProperties cannot be modified after the
initialization of CoreWebView2 has begun`。
'''

import logging
import threading

log = logging.getLogger(__name__)

# Chromium 的 driver bug workaround 开关：把 GPU 适配器偏好强制为高性能。
# 上游说明：https://github.com/MicrosoftEdge/WebView2Feedback/issues/5072
GPU_PREFERENCE_ARG = '--force_high_performance_gpu'

# 待下发的开关状态。apply_gpu_preference() 在建窗口之前调用，真正的注入发生在
# WebView2 控件构造期间，所以状态先暂存在模块级。
_pending: bool = False

# 保护下面那次「临时替换 pywebview 模块属性」：并发建窗口时，两个构造可能各自把对方
# 换上的子类当成原始类，最后完成的那个 finally 会决定模块属性停在谁身上。
_swap_lock = threading.Lock()


def apply_gpu_preference(enabled: bool) -> bool:
    """登记是否需要下发 GPU 偏好，返回登记的状态。

    **必须在建窗口之前调用**：偏好在 WebView2 控件构造时注入，窗口存在之后再调用
    只改登记值，对已经建好的窗口没有作用。
    """
    global _pending
    _pending = bool(enabled)
    if not enabled:
        log.info(f'[OmniBox] WebView2 高性能 GPU 模式已关闭，不下发 {GPU_PREFERENCE_ARG}')
    return _pending


def install(edge_chrome_cls) -> None:
    """包装 `EdgeChrome.__init__`，在它构造 WebView2 参数之前替换参数类（幂等）。"""
    original_init = edge_chrome_cls.__init__
    if getattr(original_init, '_omnibox_gpu_wrapped', False):
        return

    def wrapper(self, *args, **kwargs):
        if not _pending:
            return original_init(self, *args, **kwargs)

        import webview.platforms.edgechromium as ec

        # pywebview 把 WinForms 的 `CoreWebView2CreationProperties` 导进了本模块，
        # 我们要临时换掉它。属性名放在变量里走 getattr/setattr：直接写
        # `ec.CoreWebView2CreationProperties` 会被类型检查器判成访问私有名字
        # （那是转导入的名字，不在 pywebview 的导出里）。
        attr = 'CoreWebView2CreationProperties'
        with _swap_lock:
            original_cls = getattr(ec, attr)
            try:
                setattr(ec, attr, _preferred_props_class(original_cls))
                return original_init(self, *args, **kwargs)
            finally:
                setattr(ec, attr, original_cls)

    wrapper._omnibox_gpu_wrapped = True  # type: ignore[attr-defined]
    edge_chrome_cls.__init__ = wrapper  # type: ignore[method-assign]


def _preferred_props_class(original_cls):
    """构造 `CoreWebView2CreationProperties` 的子类，其参数写入时追加 GPU 偏好。

    为什么用子类而不是包装对象：`WebView2.CreationProperties` 是 .NET 属性，
    它的 setter 要求真正的 `CoreWebView2CreationProperties` 实例；Python 层的
    包装对象会被拒。子类实例则天然满足类型要求。

    为什么在 `__setattr__` 里拼、并用 `super().__setattr__` 写：前者保证每次写入
    都带偏好（pywebview 对该属性既用 `=` 也用 `+=`），后者才真正到达托管 setter ——
    只改 Python 侧读到的值不会改变 .NET 里的后备字段。按空白分词判重，避免同一开关
    出现两次。
    """

    class _Preferred(original_cls):  # type: ignore[misc, valid-type]
        def __setattr__(self, name, value):
            if (name == 'AdditionalBrowserArguments' and isinstance(value, str)
                    and GPU_PREFERENCE_ARG not in value.split()):
                value = f'{value} {GPU_PREFERENCE_ARG}'.strip()
                log.info(f'[OmniBox] WebView2 GPU 偏好已下发: {value}')
            super().__setattr__(name, value)

    return _Preferred
