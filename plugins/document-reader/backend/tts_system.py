"""Windows 自带的 System.Speech（SAPI5）这一档：完全离线，是最后一级兜底。

通过 PowerShell 子进程调用，因此不新增任何第三方依赖（win32com 也是同一批音色，
白搭一个依赖）。非 Windows 平台上 `available()` 返回 False，降级链会跳过它。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple


def available(params: Dict[str, Any]) -> bool:
    if sys.platform != 'win32':
        return False
    # powershell.exe 在 Win7+ 恒在；找不到它比"非 Windows"更罕见，但仍当不可用处理
    return bool(shutil.which('powershell.exe') or shutil.which('powershell'))


# 用 -EncodedCommand 传 PowerShell 代码：脚本以 UTF-16LE 内联，无论控制台代码页是
# 多少，中文文本都不会在命令行参数上被编码破坏（这是子进程方案最容易踩的坑）。
_SAPI_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Speech
  $text = [IO.File]::ReadAllText($env:OBX_TTS_TEXT, [Text.Encoding]::UTF8)
  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
  try {
    $want = $env:OBX_TTS_VOICE
    if ($want) {
      $installed = @($synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
      $hit = $installed | Where-Object { $_ -eq $want } | Select-Object -First 1
      if (-not $hit) { $hit = $installed | Where-Object { $_ -like "*$want*" } | Select-Object -First 1 }
      if (-not $hit) { $hit = $installed | Where-Object { $_ -like '*Huihui*' } | Select-Object -First 1 }
      if (-not $hit) { $hit = $installed | Select-Object -First 1 }
      if ($hit) { $synth.SelectVoice($hit) }
    }
    $synth.Rate = [int]$env:OBX_TTS_RATE
    $synth.SetOutputToWaveFile($env:OBX_TTS_OUT)
    $synth.Speak($text)
  } finally {
    $synth.Dispose()
  }
  [Console]::Out.Write('OK')
} catch {
  [Console]::Error.Write('ERR ' + $_.Exception.Message)
  exit 1
}
'''.strip()


def _decode_stderr(raw: bytes) -> str:
    """从 PowerShell 的 stderr 里取出人能看的一句话。

    重定向 stderr 时 PowerShell 会把输出包成 CLIXML（`#< CLIXML` + XML），里面既有
    我们写的 `ERR …`，也有进度条之类的噪音 —— 直接取前 200 字给用户看，就是一段
    XML 乱码。这里剥掉包装，只留 Error 记录（`<S S="Error">`）或原文第一行非空行。
    """
    import re

    text = raw.decode('utf-8', 'replace').strip()
    if text.startswith('#< CLIXML'):
        messages = re.findall(r'<S S="Error">(.*?)</S>', text, re.DOTALL)
        text = ' '.join(re.sub(r'<[^>]+>', '', item) for item in messages).strip() or text
    # CLIXML 里换行是转义的 _x000D__x000A_，还原成空格更好读
    text = text.replace('_x000D_', ' ').replace('_x000A_', ' ')
    return text[:300] or '（没有任何输出）'


def _encode_powershell(script: str) -> str:
    import base64

    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def synthesize(text: str, params: Dict[str, Any]) -> Tuple[bytes, str, List[dict]]:
    if not available(params):
        raise RuntimeError('当前平台没有可用的系统语音合成')
    powershell = shutil.which('powershell.exe') or shutil.which('powershell')
    if not powershell:
        raise RuntimeError('当前平台没有可用的系统语音合成')
    workdir = Path(tempfile.mkdtemp(prefix='cubby-tts-'))
    text_file = workdir / 'text.txt'
    out_file = workdir / 'out.wav'
    try:
        # utf-8 写入：PowerShell 侧用 ReadAllText(UTF8) 读，中文不会经过命令行参数
        text_file.write_text(text, encoding='utf-8')
        env = dict(os.environ)
        env.update({
            'OBX_TTS_TEXT': str(text_file),
            'OBX_TTS_OUT': str(out_file),
            'OBX_TTS_VOICE': str(params.get('voice') or ''),
            # SAPI 的 Rate 是 -10..10 的档位，而 UI 给的是百分比：10% 语速 ≈ 1 档
            'OBX_TTS_RATE': str(max(-10, min(10, round(int(params.get('rate') or 0) / 10.0)))),
        })
        encoded = _encode_powershell(_SAPI_SCRIPT)
        result = subprocess.run(
            [powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-EncodedCommand', encoded],
            capture_output=True, timeout=180, env=env, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode != 0:
            raise RuntimeError(f'系统语音合成失败: {_decode_stderr(result.stderr)}')
        if not out_file.is_file() or out_file.stat().st_size == 0:
            raise RuntimeError('系统语音没有产出音频')
        return out_file.read_bytes(), '.wav', []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
