import httpx

from app.config import settings
from app.core.models import Chunk

_SYSTEM_PROMPT = (
    "你是「小照」，長照法規與SOP問答助理，服務對象是長照照護員。"
    "若被問到你是誰，簡短回答你是小照即可，不要離題介紹自己。"
    "回答問題時僅能依據提供的『參考資料』，不得使用自身知識補充。"
    "先用一兩句直接回答使用者問的事（例如問怎麼申請就先列出申請步驟），再補充必要細節；"
    "與問題無關的參考資料不要提。"
    "若參考資料不足以回答，請明確說明查無依據，"
    "並建議使用者洽詢1966長照專線或原始公告。回答需標示引用的法規/SOP名稱與更新日期。"
)

# 重用同一個 AsyncClient（含連線池），避免每次生成都重新建立 TCP 連線。
# timeout 設 120s：模型冷啟動（尚未載入記憶體時）實測可能超過 30s，30s 太短會讓正常請求被誤判逾時而 500。
_client = httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=120.0)


def _build_prompt(query: str, chunks: list[Chunk]) -> str:
    references = "\n\n".join(
        f"[來源{i+1}] {chunk.law_name}"
        + (f" 第{chunk.article}條" if chunk.article else "")
        + f"（更新日期：{chunk.updated_at.isoformat()}）\n{chunk.content}"
        for i, chunk in enumerate(chunks)
    )
    return f"參考資料：\n{references}\n\n使用者問題：{query}"


async def generate_answer(query: str, chunks: list[Chunk]) -> str:
    prompt = _build_prompt(query, chunks)
    response = await _client.post(
        "/api/generate",
        json={
            "model": settings.ollama_model,
            "system": _SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
        },
    )
    response.raise_for_status()
    return response.json()["response"]
