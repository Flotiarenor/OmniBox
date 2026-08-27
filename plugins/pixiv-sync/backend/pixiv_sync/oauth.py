"""内置 OAuth PKCE 向导：用授权码重新获取 refresh_token。"""

import webbrowser
from typing import Any, Dict

from pixiv_mini import PixivError


def start(p) -> Dict[str, Any]:
    """生成 PKCE 挑战并打开 Pixiv 登录页（第一步）。

    返回完整登录 URL（含 code_challenge），供前端弹窗展示/复制。
    """
    try:
        verifier, challenge = p._client().generate_pkce()
        p._oauth_verifier = verifier
        url = p._client().login_url(challenge)
        webbrowser.open(url)
        return {"ok": True, "url": url, "challenge": challenge}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"打开登录页失败: {e}"}


def finish(p, code: str) -> Dict[str, Any]:
    """用授权码换取 token 并自动保存 refresh_token（第二步）。"""
    try:
        if not getattr(p, "_oauth_verifier", None):
            return {"ok": False, "error": "请先点击「获取 Token」打开登录页"}
        code = (code or "").strip()
        if not code:
            return {"ok": False, "error": "code 为空，请从浏览器地址栏复制 code 参数值"}
        client = p._client()
        client.auth_with_code(code, p._oauth_verifier)
        p._oauth_verifier = None
        if client.refresh_token:
            p.update_setting("refresh_token", client.refresh_token)
            p._pixiv_client = None  # 新 token，重置客户端
        return {"ok": True, "user_id": client.user_id}
    except PixivError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}