"""身分驗證與權限（設計文件 2.1 節「身分與綁定」第 6 項）。

每個 API 先以 LIFF ID token 向 LINE 驗證 userId，再檢查「目前使用的身分」。
只有 .env 的 LINE_ADMIN_USER_IDS 能以 X-Acting-Role 宣告其他身分。
"""

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from . import services
from .config import Settings
from .db import Database

logger = logging.getLogger(__name__)

VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
ROLES = ("admin", "supervisor", "caregiver")


class Unauthorized(services.ServiceError):
    status_code = 401


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str | None  # admin | supervisor | caregiver；None 表示未綁定
    display_name: str
    is_admin: bool
    available_roles: tuple[str, ...]


class IdTokenVerifier:
    """向 LINE 驗證 ID token，結果快取到 token 到期為止。"""

    def __init__(self, channel_id: str) -> None:
        self._channel_id = channel_id
        self._cache: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def verify(self, id_token: str) -> tuple[str, str]:
        with self._lock:
            cached = self._cache.get(id_token)
        if cached and cached[2] > time.time():
            return cached[0], cached[1]
        response = httpx.post(VERIFY_URL, data={"id_token": id_token, "client_id": self._channel_id}, timeout=10)
        if response.status_code != 200:
            raise Unauthorized("LINE 登入已失效，請重新登入")
        claims = response.json()
        user_id, name, exp = claims["sub"], claims.get("name", ""), float(claims["exp"])
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if v[2] > time.time()}
            self._cache[id_token] = (user_id, name, exp)
        return user_id, name


def resolve_principal(
    db: Database, settings: Settings, user_id: str, line_name: str, acting_role: str | None,
) -> Principal:
    is_admin = user_id in settings.admin_user_ids
    account = services.active_account(db, user_id)
    if is_admin:
        role = acting_role if acting_role in ROLES else "admin"
        name = account["display_name"] if account else (line_name or "管理員")
        return Principal(user_id, role, name, True, ROLES)
    if account is None:
        return Principal(user_id, None, line_name, False, ())
    # 非管理員只能使用自己綁定的身分，忽略 X-Acting-Role
    return Principal(user_id, account["role"], account["display_name"], False, (account["role"],))


def require(principal: Principal, *roles: str) -> None:
    if principal.role is None:
        raise services.Forbidden("尚未綁定，請先輸入綁定碼")
    if principal.role not in roles:
        raise services.Forbidden("目前的身分沒有這個權限")
