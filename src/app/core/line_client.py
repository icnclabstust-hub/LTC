import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LineClient:
    """LINE Messaging API 收發封裝。Channel Access Token 未設定前（憑證尚未取得，
    見 xiaozhao-phase2 設計文件第 9 節），reply/push 會略過實際呼叫並回傳 False，
    不會讓流程因為缺憑證而炸掉。呼叫失敗一律記錄 log 並回傳 False，由呼叫端決定是否改用 push。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(base_url=settings.line_api_base_url, timeout=10.0)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {settings.line_channel_access_token}", "Content-Type": "application/json"}

    async def _post(self, path: str, body: dict) -> bool:
        if not settings.line_channel_access_token:
            return False
        try:
            response = await self._client.post(path, headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            logger.warning(f"LINE API {path} 呼叫失敗：{exc!r}")
            return False
        if response.status_code != 200:
            logger.warning(f"LINE API {path} 回應 {response.status_code}：{response.text[:200]}")
            return False
        return True

    async def reply(self, reply_token: str, text: str) -> bool:
        return await self._post(
            "/v2/bot/message/reply", {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
        )

    async def push(self, to: str, text: str) -> bool:
        return await self._post("/v2/bot/message/push", {"to": to, "messages": [{"type": "text", "text": text}]})
