import base64
import hashlib
import hmac

from app.core.line_security import verify_line_signature

_SECRET = "test-channel-secret"
_BODY = b'{"events":[]}'


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def test_accepts_correct_signature():
    signature = _sign(_SECRET, _BODY)
    assert verify_line_signature(_SECRET, _BODY, signature)


def test_rejects_wrong_secret():
    signature = _sign("wrong-secret", _BODY)
    assert not verify_line_signature(_SECRET, _BODY, signature)


def test_rejects_tampered_body():
    signature = _sign(_SECRET, _BODY)
    tampered_body = b'{"events":[{"injected":true}]}'
    assert not verify_line_signature(_SECRET, tampered_body, signature)


def test_rejects_garbage_signature():
    assert not verify_line_signature(_SECRET, _BODY, "not-a-real-signature")
