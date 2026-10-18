import base64
import hashlib
import hmac


def verify_line_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """LINE webhook 簽章驗證：HMAC-SHA256(channel_secret, body) 的 base64 應與
    X-Line-Signature header 相符，避免偽造請求（見 xiaozhao-phase2 設計文件第 6 節）。"""
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)
