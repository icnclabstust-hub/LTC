import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, LtcPoint, Message


def _normalize(text: str) -> str:
    """政府資料地址欄位一律使用「臺」，但使用者慣用「台」，需正規化後再比對。"""
    return text.replace("台", "臺")


_CITIES = (
    "臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市",
    "新竹縣", "新竹市", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
    "嘉義縣", "嘉義市", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣",
    "澎湖縣", "金門縣", "連江縣", "基隆市",
)


# 系統服務地區：僅服務嘉義市＋嘉義縣，使用者未指明縣市時亦以此查詢
SERVICE_CITIES = ("嘉義市", "嘉義縣")


def extract_cities(query_text: str) -> tuple[str, ...]:
    """從使用者自由文字問題中找出提及的縣市，供 LTC_POINTS 意圖查詢過濾用；未提及則回傳服務地區。"""
    normalized = _normalize(query_text)
    for city in _CITIES:
        if city in normalized:
            return (city,)
    # 外縣市只說簡稱（如「台南」）也要辨識，否則會被當成未提及而誤回嘉義資料
    for city in _CITIES:
        if city not in SERVICE_CITIES and city[:2] in normalized:
            return (city,)
    # 未提及縣市、或只說「嘉義」未帶市／縣時，皆以服務地區查詢
    return SERVICE_CITIES


def is_in_service_area(cities: tuple[str, ...]) -> bool:
    return all(city in SERVICE_CITIES for city in cities)


async def query_ltc_points(
    session: AsyncSession,
    city: str | None,
    district: str | None,
    cities: tuple[str, ...] = (),
) -> list[LtcPoint]:
    stmt = select(LtcPoint)
    if city:
        stmt = stmt.where(LtcPoint.address.contains(_normalize(city)))
    if cities:
        stmt = stmt.where(or_(*(LtcPoint.address.contains(_normalize(c)) for c in cities)))
    if district:
        stmt = stmt.where(LtcPoint.address.contains(_normalize(district)))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def log_conversation(
    session: AsyncSession,
    query_text: str,
    answer_text: str,
    cited_chunk_ids: list[str],
) -> None:
    """記錄一問一答，供召回率評測取樣用（見回饋迴圈設計文件）。

    MVP 階段每次請求各自建立一筆 conversation（單輪記錄），
    多輪對話串接留待之後有實際需求再設計。
    結構化答案的出處（ltc: 開頭，見 ltc_graph）不是 catalog.chunks，不列入召回率取樣。
    """
    cited_chunk_ids = [cid for cid in cited_chunk_ids if not cid.startswith("ltc:")]
    conversation = Conversation()
    session.add(conversation)
    await session.flush()

    session.add(Message(conversation_id=conversation.conversation_id, role="user", content=query_text))
    session.add(
        Message(
            conversation_id=conversation.conversation_id,
            role="assistant",
            content=answer_text,
            cited_chunk_ids=[uuid.UUID(cid) for cid in cited_chunk_ids],
        )
    )
    await session.commit()
