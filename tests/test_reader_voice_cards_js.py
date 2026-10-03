"""朗读设置页「音色分卡」的无头回归（tests/js/reader_voice_cards.mjs）。

缺陷背景：音色候选原先只有一张表，且**永远问 edge** —— 选 OpenAI 兼容端点的人
在面板里看不到一个能用的音色名，把 edge 的 `zh-CN-YunxiNeural` 填进去会被端点
静默换成它自己的说话人（Qwen3-TTS 就退回第一个音色），听到的是别人且没有提示。
现在两档音色各占一张 `.nr-voice-card`：自动模式两张都在，选定引擎只留那一张，
系统离线音色一张都不出；点卡片里的音色要立刻落盘，紧随其后的试听才读得到新音色。

这套渲染是纯字符串拼 HTML + 事后绑事件，Python 侧看不见，静态检查也拦不住
"把两个来源塞进同一张卡""隐藏时还留着上一档的音色"这类错误，因此用 Node +
共享 DOM 替身（tests/js/dom_stub.mjs）跑真实脚本。环境无 node 时跳过。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'reader_voice_cards.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过朗读音色分卡用例')
class ReaderVoiceCardsJsTest(unittest.TestCase):
    def test_cards_follow_engine_and_click_persists(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'朗读音色分卡用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
