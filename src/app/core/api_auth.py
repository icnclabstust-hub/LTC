import logging
import secrets

from fastapi import Header, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


async def require_xiaozhao_token(authorization: str = Header(default="")) -> None:
    """/v1/* 小照端點的服務間驗證（FE 的 BACKEND_TOKEN）。

    權杖未設定時一律拒絕（fail closed），不會因為忘了設定就變成無驗證端點。
    """
    expected = settings.xiaozhao_api_token
    if not expected:
        logger.warning("XIAOZHAO_API_TOKEN 未設定，拒絕所有小照 API 請求")
        raise HTTPException(status_code=401, detail="unauthorized")
    scheme, _, token = authorization.partition(" ")
    # 固定時間比對，避免以回應時間差逐字猜出權杖
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")
