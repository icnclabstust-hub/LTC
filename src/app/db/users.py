import uuid
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserIdentity

ROLES = ("caregiver", "supervisor")
_PSEUDONYM_PREFIX = {"caregiver": "CG", "supervisor": "SV"}


async def create_user(session: AsyncSession, name: str, role: str) -> User:
    # 代號取自資料庫 sequence，多個請求同時建帳號也不會撞號
    n = (await session.execute(text("SELECT nextval('biz.user_pseudonym_seq')"))).scalar_one()
    user = User(name=name, role=role, pseudonym=f"{_PSEUDONYM_PREFIX[role]}-{n:04d}")
    session.add(user)
    await session.commit()
    return user


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def disable_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """停用＝解除所有通道綁定；之後以 LINE userId 查身分會查無此人，FE 會當成未綁定。"""
    await session.execute(
        update(UserIdentity)
        .where(UserIdentity.user_id == user_id, UserIdentity.unbound_at.is_(None))
        .values(unbound_at=datetime.utcnow())
    )
    await session.commit()


async def find_user_by_identity(session: AsyncSession, channel: str, external_user_id: str) -> User | None:
    result = await session.execute(
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.user_id)
        .where(
            UserIdentity.channel == channel,
            UserIdentity.external_user_id == external_user_id,
            UserIdentity.unbound_at.is_(None),
        )
    )
    return result.scalar_one_or_none()
