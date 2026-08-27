"""全局请求速率控制与限流异常。"""

import random
import threading
import time


class RateLimitError(Exception):
    """Pixiv 限流（HTTP 429）：任务应立即停止，等待冷却后重试。"""


class RateLimiter:
    """全局请求速率控制（令牌桶），避免触发 pixiv app-api 的 429 限流。

    rate 为每秒请求数（可配置）；间隔带随机抖动（-20% ~ +40%），
    避免固定节律、更接近自然请求模式。
    """

    def __init__(self, rate: float = 3.0):
        self._lock = threading.Lock()
        self._last = 0.0
        self._rate = max(0.5, min(10.0, rate))

    def set_rate(self, rate: float):
        with self._lock:
            self._rate = max(0.5, min(10.0, float(rate)))

    def wait(self):
        with self._lock:
            now = time.time()
            base = 1.0 / self._rate
            interval = base * (1 + random.uniform(-0.2, 0.4))
            wait = max(0.0, self._last + interval - now)
            if wait:
                time.sleep(wait)
            self._last = time.time()