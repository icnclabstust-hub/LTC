"""本機假後端：依後端組 API 合約（《小照前後端串接設計》①②③）回應，供 HttpBackend 測試與手機實測。

（2026-10-14 http-backend 設計文件第 5 節第 2 點）

- ① POST /v1/query、② POST /v1/scene：回 202 與 request_id（同一 request_id 重送不重複處理）。
- ③ GET /v1/requests/{id}：前 ANSWER_DELAY_SECONDS 秒回 pending，之後回 answered；
  文字含「藥、醫、病、補助」時回 refused（模擬拒答轉人工，附 help_request_id）。
- 設環境變數 FAKE_BACKEND_TOKEN 時檢查 Authorization: Bearer <token>。

用法：python tools/fake_backend.py [--port 8001]
"""

import argparse
import os
import re
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

ANSWER_DELAY_SECONDS = 1.0
REFUSE = re.compile(r"藥|醫|病|補助")


def create_app(token: str = "") -> FastAPI:
    app = FastAPI(title="小照假後端")
    requests: dict[str, dict] = {}

    def check(authorization: str | None) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "權杖錯誤")

    @app.post("/v1/query", status_code=202)
    async def query(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        body = await request.json()
        rid = body.get("request_id") or uuid.uuid4().hex
        requests.setdefault(rid, {"kind": "query", "body": body, "received": time.monotonic()})
        print(f"① query {rid}：{body.get('text')}（{body.get('client_intent')}）")
        return JSONResponse({"request_id": rid}, status_code=202)

    @app.post("/v1/scene", status_code=202)
    async def scene(request: Request, authorization: str | None = Header(None)):
        check(authorization)
        body = await request.json()
        rid = body.get("request_id") or uuid.uuid4().hex
        requests.setdefault(rid, {"kind": "scene", "body": body, "received": time.monotonic()})
        print(f"② scene {rid}：{body.get('segment_count')} 句，意圖 {body.get('intent_counts')}")
        return JSONResponse({"request_id": rid}, status_code=202)

    @app.get("/v1/requests/{rid}")
    async def result(rid: str, authorization: str | None = Header(None)):
        check(authorization)
        item = requests.get(rid)
        if item is None:
            raise HTTPException(404, "找不到請求")
        if time.monotonic() - item["received"] < ANSWER_DELAY_SECONDS:
            return {"request_id": rid, "status": "pending"}
        text = item["body"].get("text") or ""
        if REFUSE.search(text):
            return {"request_id": rid, "status": "refused", "help_request_id": f"help-{rid[:8]}"}
        answer = f"（假後端回答，非真實內容）已收到：「{text}」"
        return {"request_id": rid, "status": "answered", "answer_spoken": answer[:40], "answer_full": answer,
                "citations": [], "faithfulness": None}

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run(create_app(os.environ.get("FAKE_BACKEND_TOKEN", "")), host="127.0.0.1", port=args.port)
