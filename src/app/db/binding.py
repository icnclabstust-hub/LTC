import secrets
import string
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BindingCode, UserIdentity

CODE_TTL_MINUTES = 10


def generate_code() -> str:
    # 綁定碼等同一次性憑證，須用密碼學安全亂數（random 模組可被預測）
    return "".join(secrets.choice(string.digits) for _ in range(6))


async def _is_code_active(session: AsyncSession, code_str: str) -> bool:
    result = await session.execute(
        select(BindingCode.code_id).where(
            BindingCode.code == code_str,
            BindingCode.used_at.is_(None),
            BindingCode.expires_at > datetime.utcnow(),
        )
    )
    return result.first() is not None


async def create_binding_code(session: AsyncSession, created_by: uuid.UUID, target_user_id: uuid.UUID) -> BindingCode:
    # 不可與其他有效綁定碼重複，否則核銷時無法判斷要綁給哪位照護員
    code_str = generate_code()
    while await _is_code_active(session, code_str):
        code_str = generate_code()

    code = BindingCode(
        code=code_str,
        created_by=created_by,
        target_user_id=target_user_id,
        expires_at=datetime.utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
    )
    session.add(code)
    await session.commit()
    return code


async def redeem_binding_code(
    session: AsyncSession, code_str: str, channel: str, external_user_id: str
) -> UserIdentity | None:
    """核銷綁定碼；成功回傳新建立的 UserIdentity，失敗（過期/不存在/已使用）回傳 None。"""
    result = await session.execute(
        select(BindingCode).where(
            BindingCode.code == code_str,
            BindingCode.used_at.is_(None),
            BindingCode.expires_at > datetime.utcnow(),
        )
    )
    code = result.scalar_one_or_none()
    if code is None:
        return None

    identity = UserIdentity(user_id=code.target_user_id, channel=channel, external_user_id=external_user_id)
    session.add(identity)
    await session.flush()

    code.used_at = datetime.utcnow()
    code.used_by_identity_id = identity.identity_id
    await session.commit()
    return identity


async def find_identity(session: AsyncSession, channel: str, external_user_id: str) -> UserIdentity | None:
    result = await session.execute(
        select(UserIdentity).where(
            UserIdentity.channel == channel,
            UserIdentity.external_user_id == external_user_id,
            UserIdentity.unbound_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def unbind_identity(session: AsyncSession, identity: UserIdentity) -> None:
    identity.unbound_at = datetime.utcnow()
    await session.commit()
