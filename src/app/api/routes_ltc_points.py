from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.queries import query_ltc_points
from app.db.session import get_session

router = APIRouter(prefix="/api")


@router.get("/ltc-points")
async def ltc_points(
    city: str | None = Query(default=None),
    district: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    points = await query_ltc_points(session, city, district)
    return [
        {
            "機構代碼": p.institution_code,
            "機構名稱": p.name,
            "機構種類": p.category,
            "地址全址": p.address,
            "機構電話": p.phone,
        }
        for p in points
    ]
