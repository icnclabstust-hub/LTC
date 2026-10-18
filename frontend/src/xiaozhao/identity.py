"""帳號與綁定以後端 PostgreSQL 為準（2026-10-19 需求決議第 1 項；fe-be-integration 設計文件第 5.2 節）。

BACKEND_MODE=http 時，建帳號、發綁定碼、核銷、停用都呼叫後端，本機 SQLite 的 accounts 只當快取；
mock 模式（與既有單元測試）維持本機帳號，不經過本模組。
"""

import logging

import httpx

from . import services

logger = logging.getLogger(__name__)


class IdentityUnavailable(services.ServiceError):
    status_code = 503


class BackendIdentity:
    def __init__(self, base_url: str, token: str, timeout_seconds: float = 10.0, transport=None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds,
                                    transport=transport)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("後端身分 API %s %s 連線失敗：%s", method, path, type(exc).__name__)
            raise IdentityUnavailable("暫時無法連到後端，請稍後再試") from exc

    @staticmethod
    def _check(response: httpx.Response) -> dict:
        if response.status_code in (200, 201):
            return response.json()
        detail = (response.json() or {}).get("detail") if response.headers.get("content-type", "").startswith(
            "application/json") else None
        if response.status_code == 400:
            raise services.ServiceError(detail or "綁定碼錯誤、已使用或已過期")
        if response.status_code == 404:
            raise services.NotFound(detail or "帳號不存在")
        if response.status_code == 409:
            raise services.Conflict(detail or "這個 LINE 帳號已經綁定過")
        if response.status_code == 429:
            error = services.ServiceError(detail or "嘗試次數過多，請稍後再試")
            error.status_code = 429
            raise error
        logger.warning("後端身分 API 回應 %s", response.status_code)
        raise IdentityUnavailable("後端身分服務異常，請稍後再試")

    def create_user(self, name: str, role: str) -> dict:
        return self._check(self._request("POST", "/v1/users", json={"name": name, "role": role}))

    def issue_code(self, user_id: str) -> dict:
        return self._check(self._request("POST", f"/v1/users/{user_id}/binding-codes", json={}))

    def bind(self, code: str, line_user_id: str) -> dict:
        return self._check(self._request("POST", "/v1/bindings", json={"code": code.strip(),
                                                                        "line_user_id": line_user_id}))

    def lookup(self, line_user_id: str) -> dict | None:
        response = self._request("GET", f"/v1/identities/line/{line_user_id}")
        if response.status_code == 404:
            return None
        return self._check(response)

    def disable(self, user_id: str) -> None:
        self._check(self._request("POST", f"/v1/users/{user_id}/disable"))


def build_identity(mode: str, url: str, token: str) -> BackendIdentity | None:
    return BackendIdentity(url, token) if mode == "http" and url else None
